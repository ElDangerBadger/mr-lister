from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from botocore.exceptions import ClientError

from mr_lister.control.commands import RecordWorkerFailureCommand, WorkerFailureCode
from mr_lister.control.dispatch import (
    DispatchConfigurationError,
    DispatchIdentityConflictError,
    DispatchRejectedError,
    WorkDispatcher,
    deterministic_execution_name,
    execution_arn_for,
    work_input_fingerprint,
)
from mr_lister.control.models import (
    CommandReceipt,
    CommandResponse,
    ControlJobRecord,
    ControlJobState,
    DomainEvent,
    WorkRequest,
    WorkRequestStatus,
    WorkType,
)
from mr_lister.control.service import SellerControlService
from mr_lister.control.store import InMemorySellerControlStore

NOW = datetime(2026, 8, 21, 12, 0, tzinfo=UTC)
OWNER_ID = "a" * 64


def state_machine_arns() -> dict[WorkType, str]:
    return {
        work_type: (
            "arn:aws:states:us-west-2:123456789012:"
            f"stateMachine:mr-lister-{work_type.value.replace('_', '-')}"
        )
        for work_type in WorkType
    }


def make_work(
    *,
    work_request_id: str = "work_dispatch_001",
    work_type: WorkType = WorkType.PREPARE,
    status: WorkRequestStatus = WorkRequestStatus.PENDING,
    next_dispatch_at: datetime = NOW,
    attempt_count: int = 0,
    claim_id: str | None = None,
    lease_expires_at: datetime | None = None,
) -> WorkRequest:
    job_id = "job_dispatch_001"
    return WorkRequest(
        work_request_id=work_request_id,
        job_id=job_id,
        owner_id=OWNER_ID,
        receipt_id="receipt_dispatch_001",
        work_type=work_type,
        input_fingerprint=work_input_fingerprint(
            work_type=work_type,
            job_id=job_id,
            work_request_id=work_request_id,
        ),
        execution_name=deterministic_execution_name(work_request_id),
        status=status,
        created_at=NOW - timedelta(minutes=1),
        updated_at=NOW - timedelta(minutes=1),
        next_dispatch_at=next_dispatch_at,
        attempt_count=attempt_count,
        claim_id=claim_id,
        lease_expires_at=lease_expires_at,
    )


class MemoryDispatchStore:
    def __init__(self, *work: WorkRequest) -> None:
        self.work = {request.work_request_id: request for request in work}
        self.release_calls: list[dict[str, Any]] = []
        self.dispatched_calls: list[dict[str, Any]] = []

    def list_due_work(self, *, now: datetime, limit: int) -> tuple[WorkRequest, ...]:
        due = [
            request
            for request in self.work.values()
            if (request.status is WorkRequestStatus.PENDING and request.next_dispatch_at <= now)
            or (
                request.status is WorkRequestStatus.CLAIMED
                and request.lease_expires_at is not None
                and request.lease_expires_at <= now
            )
        ]
        return tuple(sorted(due, key=lambda request: request.work_request_id)[:limit])

    def claim_work(
        self,
        job_id: str,
        work_request_id: str,
        *,
        claim_id: str,
        now: datetime,
        lease_expires_at: datetime,
    ) -> WorkRequest | None:
        request = self.work[work_request_id]
        pending_due = (
            request.status is WorkRequestStatus.PENDING and request.next_dispatch_at <= now
        )
        expired_claim = (
            request.status is WorkRequestStatus.CLAIMED
            and request.lease_expires_at is not None
            and request.lease_expires_at <= now
        )
        if not pending_due and not expired_claim:
            return None
        claimed = request.model_copy(
            update={
                "status": WorkRequestStatus.CLAIMED,
                "claim_id": claim_id,
                "lease_expires_at": lease_expires_at,
                "attempt_count": request.attempt_count + 1,
                "last_error_code": None,
                "updated_at": now,
            }
        )
        self.work[work_request_id] = claimed
        return claimed

    def mark_work_dispatched(
        self,
        job_id: str,
        work_request_id: str,
        *,
        claim_id: str,
        execution_arn: str,
        now: datetime,
    ) -> WorkRequest:
        request = self.work[work_request_id]
        if request.status is WorkRequestStatus.COMPLETED:
            return request
        assert request.status is WorkRequestStatus.CLAIMED
        assert request.claim_id == claim_id
        dispatched = request.model_copy(
            update={
                "status": WorkRequestStatus.DISPATCHED,
                "claim_id": None,
                "lease_expires_at": None,
                "execution_arn": execution_arn,
                "updated_at": now,
            }
        )
        self.work[work_request_id] = dispatched
        self.dispatched_calls.append(
            {"work_request_id": work_request_id, "execution_arn": execution_arn}
        )
        return dispatched

    def defer_claimed_work(
        self,
        job_id: str,
        work_request_id: str,
        *,
        claim_id: str,
        retry_at: datetime,
        now: datetime,
        error_code: str,
    ) -> WorkRequest:
        del job_id
        request = self.work[work_request_id]
        if request.status is WorkRequestStatus.COMPLETED:
            return request
        assert request.status is WorkRequestStatus.CLAIMED
        assert request.claim_id == claim_id
        deferred = WorkRequest.model_validate(
            {
                **request.model_dump(mode="python"),
                "lease_expires_at": retry_at,
                "last_error_code": error_code,
                "updated_at": now,
            }
        )
        self.work[work_request_id] = deferred
        return deferred

    def release_work(
        self,
        job_id: str,
        work_request_id: str,
        *,
        claim_id: str,
        next_dispatch_at: datetime,
        now: datetime,
        error_code: str,
    ) -> WorkRequest:
        del now
        request = self.work[work_request_id]
        assert request.status is WorkRequestStatus.CLAIMED
        assert request.claim_id == claim_id
        released = request.model_copy(
            update={
                "status": WorkRequestStatus.PENDING,
                "claim_id": None,
                "lease_expires_at": None,
                "next_dispatch_at": next_dispatch_at,
                "last_error_code": error_code,
                "updated_at": NOW,
            }
        )
        self.work[work_request_id] = released
        self.release_calls.append(
            {
                "work_request_id": work_request_id,
                "next_dispatch_at": next_dispatch_at,
                "error_code": error_code,
            }
        )
        return released


class RecordingStepFunctions:
    def __init__(
        self,
        *,
        start_error: str | None = None,
        describe_error: str | None = None,
        start_transport_error: bool = False,
        describe_transport_error: bool = False,
        described: dict[str, Any] | None = None,
        returned_execution_arn: str | None = None,
        on_start: Callable[[dict[str, str]], None] | None = None,
    ) -> None:
        self.start_error = start_error
        self.describe_error = describe_error
        self.start_transport_error = start_transport_error
        self.describe_transport_error = describe_transport_error
        self.described = described
        self.returned_execution_arn = returned_execution_arn
        self.on_start = on_start
        self.start_calls: list[dict[str, str]] = []
        self.describe_calls: list[dict[str, str]] = []

    def start_execution(self, **request: str) -> dict[str, Any]:
        self.start_calls.append(request)
        if self.on_start is not None:
            self.on_start(request)
        if self.start_transport_error:
            raise TimeoutError("private provider detail")
        if self.start_error is not None:
            raise ClientError(
                {"Error": {"Code": self.start_error, "Message": "private provider detail"}},
                "StartExecution",
            )
        execution_arn = self.returned_execution_arn or execution_arn_for(
            request["stateMachineArn"], request["name"]
        )
        return {"executionArn": execution_arn, "startDate": NOW}

    def describe_execution(self, **request: str) -> dict[str, Any]:
        self.describe_calls.append(request)
        if self.describe_transport_error:
            raise TimeoutError("private provider detail")
        if self.describe_error is not None:
            raise ClientError(
                {"Error": {"Code": self.describe_error, "Message": "private provider detail"}},
                "DescribeExecution",
            )
        assert self.described is not None
        return self.described


def make_dispatcher(
    store: MemoryDispatchStore,
    step_functions: RecordingStepFunctions,
    **overrides: Any,
) -> WorkDispatcher:
    return WorkDispatcher(
        store=store,
        step_functions=step_functions,
        state_machine_arns=state_machine_arns(),
        clock=lambda: NOW,
        claim_id_factory=lambda: "claim_dispatch_001",
        **overrides,
    )


def test_due_work_dispatches_to_only_its_allowlisted_state_machine() -> None:
    work = make_work(work_type=WorkType.SYNCHRONIZE_PRODUCT)
    store = MemoryDispatchStore(work)
    step_functions = RecordingStepFunctions()

    results = make_dispatcher(store, step_functions).dispatch_due()

    dispatched = results[0]
    request = step_functions.start_calls[0]
    assert dispatched.status is WorkRequestStatus.DISPATCHED
    assert dispatched.attempt_count == 1
    assert request == {
        "stateMachineArn": state_machine_arns()[WorkType.SYNCHRONIZE_PRODUCT],
        "name": deterministic_execution_name(work.work_request_id),
        "input": ('{"job_id":"job_dispatch_001","work_request_id":"work_dispatch_001"}'),
    }
    assert dispatched.execution_arn == execution_arn_for(
        request["stateMachineArn"], request["name"]
    )


def test_dispatcher_uses_the_phase6_store_claim_contract_end_to_end() -> None:
    work = make_work()
    store = InMemorySellerControlStore()
    job = ControlJobRecord(
        owner_id=OWNER_ID,
        job_id=work.job_id,
        state=ControlJobState.INTAKE_VALIDATED,
        event_sequence=1,
        active_work_request_id=work.work_request_id,
        created_at=NOW - timedelta(minutes=1),
        updated_at=NOW - timedelta(minutes=1),
    )
    response = CommandResponse(
        job_id=job.job_id,
        state=job.state,
        record_version=job.record_version,
        review_version=job.review_version,
        work_request_id=work.work_request_id,
    )
    receipt = CommandReceipt(
        receipt_id=work.receipt_id,
        owner_id=OWNER_ID,
        job_id=job.job_id,
        command_type="complete_upload",
        idempotency_key_digest="b" * 64,
        request_fingerprint="c" * 64,
        response=response,
        work_request_id=work.work_request_id,
        created_at=job.created_at,
    )
    store.create_job(
        job=job,
        event=DomainEvent(
            job_id=job.job_id,
            sequence=1,
            name="JOB_CREATED",
            occurred_at=job.created_at,
        ),
        receipt=receipt,
        work_request=work,
    )
    step_functions = RecordingStepFunctions()

    dispatched = WorkDispatcher(
        store=store,
        step_functions=step_functions,
        state_machine_arns=state_machine_arns(),
        clock=lambda: NOW,
        claim_id_factory=lambda: "claim_dispatch_001",
    ).dispatch_due()[0]

    assert dispatched.status is WorkRequestStatus.DISPATCHED
    assert store.get_work_request(job.job_id, work.work_request_id) == dispatched
    assert dispatched.attempt_count == 1


def test_fast_worker_can_settle_claimed_work_before_start_acknowledgement() -> None:
    work = make_work(work_request_id="work_fast_worker")
    store = InMemorySellerControlStore()
    job = ControlJobRecord(
        owner_id=OWNER_ID,
        job_id=work.job_id,
        state=ControlJobState.INTAKE_VALIDATED,
        event_sequence=1,
        active_work_request_id=work.work_request_id,
        created_at=work.created_at,
        updated_at=work.created_at,
    )
    response = CommandResponse(
        job_id=job.job_id,
        state=job.state,
        record_version=0,
        review_version=0,
        work_request_id=work.work_request_id,
    )
    receipt = CommandReceipt(
        receipt_id=work.receipt_id,
        owner_id=OWNER_ID,
        job_id=job.job_id,
        command_type="complete_upload",
        idempotency_key_digest="b" * 64,
        request_fingerprint="c" * 64,
        response=response,
        work_request_id=work.work_request_id,
        created_at=work.created_at,
    )
    store.create_job(
        job=job,
        event=DomainEvent(
            job_id=job.job_id,
            sequence=1,
            name="UPLOAD_COMPLETED",
            occurred_at=work.created_at,
        ),
        receipt=receipt,
        work_request=work,
    )
    service = SellerControlService(store=store, clock=lambda: NOW)

    def settle_inside_start(_request: dict[str, str]) -> None:
        service.record_worker_failure(
            RecordWorkerFailureCommand(
                job_id=job.job_id,
                work_request_id=work.work_request_id,
                expected_record_version=0,
                code=WorkerFailureCode.INTELLIGENCE_UNAVAILABLE,
            )
        )

    step_functions = RecordingStepFunctions(on_start=settle_inside_start)
    result = WorkDispatcher(
        store=store,
        step_functions=step_functions,
        state_machine_arns=state_machine_arns(),
        clock=lambda: NOW,
        claim_id_factory=lambda: "claim_fast_worker",
    ).dispatch_due()[0]

    assert result.status is WorkRequestStatus.COMPLETED
    assert store.get_job(job.job_id).state is ControlJobState.FAILED_RETRYABLE
    assert store.get_work_request(job.job_id, work.work_request_id).status is (
        WorkRequestStatus.COMPLETED
    )


def test_pending_future_and_unexpired_claims_are_not_claimed() -> None:
    future = make_work(
        work_request_id="work_future",
        next_dispatch_at=NOW + timedelta(seconds=1),
    )
    held = make_work(
        work_request_id="work_held",
        status=WorkRequestStatus.CLAIMED,
        claim_id="other_claim",
        lease_expires_at=NOW + timedelta(seconds=1),
        attempt_count=1,
    )
    store = MemoryDispatchStore(future, held)
    step_functions = RecordingStepFunctions()

    results = make_dispatcher(store, step_functions).dispatch_due()

    assert results == ()
    assert step_functions.start_calls == []


def test_expired_claim_is_reclaimed_with_the_same_execution_identity() -> None:
    expired = make_work(
        status=WorkRequestStatus.CLAIMED,
        claim_id="crashed_dispatcher",
        lease_expires_at=NOW - timedelta(minutes=1),
        attempt_count=1,
    )
    store = MemoryDispatchStore(expired)
    step_functions = RecordingStepFunctions()

    dispatched = make_dispatcher(store, step_functions).dispatch_due()[0]

    assert dispatched.status is WorkRequestStatus.DISPATCHED
    assert dispatched.attempt_count == 2
    assert step_functions.start_calls[0]["name"] == expired.execution_name


def test_allowlist_must_contain_every_and_only_known_work_type() -> None:
    work = make_work()
    store = MemoryDispatchStore(work)
    step_functions = RecordingStepFunctions()
    incomplete = state_machine_arns()
    incomplete.pop(WorkType.PREPARE)

    with pytest.raises(DispatchConfigurationError, match="every and only"):
        WorkDispatcher(
            store=store,
            step_functions=step_functions,
            state_machine_arns=incomplete,
        )
    with pytest.raises(DispatchConfigurationError, match="invalid entry"):
        WorkDispatcher(
            store=store,
            step_functions=step_functions,
            state_machine_arns={
                **state_machine_arns(),
                WorkType.PREPARE: "https://attacker.example/workflow",
            },
        )

    assert step_functions.start_calls == []


@pytest.mark.parametrize("tamper", ["fingerprint", "execution_name"])
def test_tampered_work_identity_is_rejected_before_step_functions(tamper: str) -> None:
    work = make_work()
    if tamper == "fingerprint":
        work = work.model_copy(update={"input_fingerprint": "f" * 64})
    else:
        work = work.model_copy(update={"execution_name": "caller-selected-execution"})
    store = MemoryDispatchStore(work)
    step_functions = RecordingStepFunctions()

    with pytest.raises(DispatchConfigurationError):
        make_dispatcher(store, step_functions).dispatch_due()

    assert step_functions.start_calls == []
    assert store.dispatched_calls == []


def test_execution_already_exists_is_success_only_after_exact_readback() -> None:
    work = make_work()
    machine_arn = state_machine_arns()[work.work_type]
    execution_arn = execution_arn_for(machine_arn, work.execution_name)
    payload = '{"job_id":"job_dispatch_001","work_request_id":"work_dispatch_001"}'
    store = MemoryDispatchStore(work)
    step_functions = RecordingStepFunctions(
        start_error="ExecutionAlreadyExists",
        described={
            "executionArn": execution_arn,
            "stateMachineArn": machine_arn,
            "name": work.execution_name,
            "input": payload,
            "status": "SUCCEEDED",
        },
    )

    dispatched = make_dispatcher(store, step_functions).dispatch_due()[0]

    assert dispatched.status is WorkRequestStatus.DISPATCHED
    assert dispatched.execution_arn == execution_arn
    assert step_functions.describe_calls == [{"executionArn": execution_arn}]


def test_conflicting_existing_execution_fails_closed_without_marking_dispatch() -> None:
    work = make_work()
    machine_arn = state_machine_arns()[work.work_type]
    execution_arn = execution_arn_for(machine_arn, work.execution_name)
    store = MemoryDispatchStore(work)
    step_functions = RecordingStepFunctions(
        start_error="ExecutionAlreadyExists",
        described={
            "executionArn": execution_arn,
            "stateMachineArn": machine_arn,
            "name": work.execution_name,
            "input": '{"job_id":"different","work_request_id":"work_dispatch_001"}',
        },
    )

    with pytest.raises(DispatchIdentityConflictError, match="does not match"):
        make_dispatcher(store, step_functions).dispatch_due()

    assert store.dispatched_calls == []
    assert store.work[work.work_request_id].status is WorkRequestStatus.CLAIMED


def test_transient_start_failure_keeps_claim_settleable_with_bounded_backoff() -> None:
    work = make_work(attempt_count=20)
    store = MemoryDispatchStore(work)
    step_functions = RecordingStepFunctions(start_error="ThrottlingException")

    deferred = make_dispatcher(
        store,
        step_functions,
        base_backoff_seconds=2,
        maximum_backoff_seconds=30,
    ).dispatch_due()[0]

    assert deferred.status is WorkRequestStatus.CLAIMED
    assert deferred.attempt_count == 21
    assert deferred.lease_expires_at == NOW + timedelta(seconds=30)
    assert deferred.last_error_code == "DISPATCH_TRANSIENT"
    assert store.dispatched_calls == []


def test_ambiguous_start_timeout_reclaims_same_deterministic_execution_after_lease() -> None:
    work = make_work()
    store = MemoryDispatchStore(work)
    timed_out = RecordingStepFunctions(start_transport_error=True)
    first = make_dispatcher(store, timed_out).dispatch_due()[0]
    assert first.status is WorkRequestStatus.CLAIMED
    assert first.lease_expires_at == NOW + timedelta(seconds=2)

    accepted = RecordingStepFunctions()
    second = make_dispatcher(store, accepted).dispatch_one(
        work.job_id,
        work.work_request_id,
        now=NOW + timedelta(seconds=2),
    )

    assert second is not None
    assert second.status is WorkRequestStatus.DISPATCHED
    assert second.attempt_count == 2
    assert accepted.start_calls[0]["name"] == work.execution_name


def test_transient_existing_execution_readback_defers_the_same_claim() -> None:
    work = make_work()
    store = MemoryDispatchStore(work)
    step_functions = RecordingStepFunctions(
        start_error="ExecutionAlreadyExists",
        describe_error="ExecutionDoesNotExist",
    )

    deferred = make_dispatcher(store, step_functions).dispatch_due()[0]

    assert deferred.status is WorkRequestStatus.CLAIMED
    assert deferred.lease_expires_at == NOW + timedelta(seconds=2)
    assert store.dispatched_calls == []


def test_transport_failure_during_existing_execution_readback_defers_claim() -> None:
    work = make_work()
    store = MemoryDispatchStore(work)
    step_functions = RecordingStepFunctions(
        start_error="ExecutionAlreadyExists",
        describe_transport_error=True,
    )

    deferred = make_dispatcher(store, step_functions).dispatch_due()[0]

    assert deferred.status is WorkRequestStatus.CLAIMED
    assert deferred.lease_expires_at == NOW + timedelta(seconds=2)
    assert store.dispatched_calls == []


def test_nonretryable_start_rejection_is_sanitized_and_not_marked_dispatched() -> None:
    work = make_work()
    store = MemoryDispatchStore(work)
    step_functions = RecordingStepFunctions(start_error="AccessDeniedException")

    with pytest.raises(DispatchRejectedError, match="rejected") as rejected:
        make_dispatcher(store, step_functions).dispatch_due()

    assert rejected.value.__cause__ is None
    assert "private provider detail" not in str(rejected.value)
    assert store.release_calls == []
    assert store.dispatched_calls == []


def test_unexpected_execution_arn_is_not_persisted() -> None:
    work = make_work()
    store = MemoryDispatchStore(work)
    step_functions = RecordingStepFunctions(
        returned_execution_arn=("arn:aws:states:us-west-2:123456789012:execution:attacker:wrong")
    )

    with pytest.raises(DispatchIdentityConflictError, match="another identity"):
        make_dispatcher(store, step_functions).dispatch_due()

    assert store.dispatched_calls == []
