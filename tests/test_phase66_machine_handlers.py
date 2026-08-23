from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import pytest

from mr_lister.cloud.phase6_machine import (
    Phase6MachineExecutionError,
    Phase6MachineHandlers,
    Phase6MachineInvocationError,
)
from mr_lister.control.commands import WorkerFailureCode
from mr_lister.control.errors import ConcurrentControlModificationError
from mr_lister.control.models import (
    CommandResponse,
    ControlJobRecord,
    ControlJobState,
    WorkRequest,
    WorkRequestStatus,
    WorkType,
)
from mr_lister.control.settlement import PreparationSettlementError

NOW = datetime(2026, 8, 23, 12, 0, tzinfo=UTC)
OWNER = "a" * 64
REVIEW_FP = "b" * 64


class FakeDispatcher:
    def __init__(self) -> None:
        self.due: tuple[WorkRequest, ...] = ()
        self.one_calls: list[tuple[str, str]] = []

    def dispatch_due(self, *, limit: int = 25) -> tuple[WorkRequest, ...]:
        assert limit == 25
        return self.due

    def dispatch_one(self, job_id: str, work_request_id: str) -> WorkRequest | None:
        self.one_calls.append((job_id, work_request_id))
        return _work(job_id=job_id, work_id=work_request_id)


class FakePreparation:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.error: Exception | None = None

    def invoke(self, *, job_id: str, work_request_id: str) -> FakeDump:
        self.calls.append((job_id, work_request_id))
        if self.error is not None:
            raise self.error
        return FakeDump({"framework": "strands-agents", "agent_id": "mr-lister-preparation"})


class FakeProvider:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str]] = []
        self.error: Exception | None = None

    def _run(self, operation: str, job_id: str, work_request_id: str) -> CommandResponse:
        self.calls.append((operation, job_id, work_request_id))
        if self.error is not None:
            raise self.error
        return CommandResponse(
            job_id=job_id,
            state=ControlJobState.AWAITING_APPROVAL,
            record_version=8,
            review_version=2,
        )

    def run_product_sync(self, *, job_id: str, work_request_id: str) -> CommandResponse:
        return self._run("synchronize_product", job_id, work_request_id)

    def run_product_reconciliation(self, *, job_id: str, work_request_id: str) -> CommandResponse:
        return self._run("reconcile_product", job_id, work_request_id)

    def run_economics_refresh(self, *, job_id: str, work_request_id: str) -> CommandResponse:
        return self._run("refresh_economics", job_id, work_request_id)


class FakeStore:
    def __init__(self, job: ControlJobRecord, work: WorkRequest) -> None:
        self.job = job
        self.work = work

    def get_job(self, job_id: str) -> ControlJobRecord:
        assert job_id == self.job.job_id
        return self.job

    def get_work_request(self, job_id: str, work_request_id: str) -> WorkRequest:
        assert job_id == self.work.job_id
        assert work_request_id == self.work.work_request_id
        return self.work


class FakeControl:
    def __init__(self) -> None:
        self.failures: list[Any] = []
        self.cancellations: list[Any] = []
        self.failure_hook: Any = None
        self.cancellation_hook: Any = None

    def record_worker_failure(self, command: Any) -> CommandResponse:
        self.failures.append(command)
        if self.failure_hook is not None:
            self.failure_hook()
        return CommandResponse(
            job_id=command.job_id,
            state=ControlJobState.FAILED_RETRYABLE,
            record_version=5,
            review_version=1,
        )

    def settle_cancellation(self, command: Any) -> CommandResponse:
        self.cancellations.append(command)
        if self.cancellation_hook is not None:
            self.cancellation_hook()
        return CommandResponse(
            job_id=command.job_id,
            state=ControlJobState.CANCELLED,
            record_version=5,
            review_version=1,
        )


class FakePreparationSettlement:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.hook: Any = None

    def settle_unavailable(self, *, job_id: str, work_request_id: str) -> FakeSettlement:
        self.calls.append((job_id, work_request_id))
        if self.hook is not None:
            self.hook()
        return FakeSettlement(
            response=CommandResponse(
                job_id=job_id,
                state=ControlJobState.PRODUCT_DRAFT_SYNCING,
                record_version=4,
                review_version=1,
                work_request_id="work_sync_next",
            )
        )


@dataclass(frozen=True)
class FakeDump:
    payload: dict[str, Any]

    def model_dump(self, *, mode: str) -> dict[str, Any]:
        assert mode == "json"
        return self.payload


@dataclass(frozen=True)
class FakeSettlement:
    response: CommandResponse


def _job(
    *,
    job_id: str = "job_1",
    work_id: str | None = "work_1",
    state: ControlJobState = ControlJobState.PRODUCT_DRAFT_SYNCING,
    cancellation: bool = False,
) -> ControlJobRecord:
    return ControlJobRecord(
        owner_id=OWNER,
        job_id=job_id,
        state=(
            ControlJobState.CANCEL_REQUESTED
            if cancellation and state is not ControlJobState.CANCELLED
            else state
        ),
        record_version=4,
        event_sequence=4,
        review_version=1,
        review_fingerprint=REVIEW_FP,
        active_work_request_id=work_id,
        cancellation_requested_at=NOW if cancellation else None,
        created_at=NOW,
        updated_at=NOW,
    )


def _work(
    *,
    job_id: str = "job_1",
    work_id: str = "work_1",
    work_type: WorkType = WorkType.SYNCHRONIZE_PRODUCT,
    status: WorkRequestStatus = WorkRequestStatus.DISPATCHED,
) -> WorkRequest:
    return WorkRequest(
        work_request_id=work_id,
        owner_id=OWNER,
        job_id=job_id,
        receipt_id="receipt_1",
        work_type=work_type,
        review_version=None if work_type is WorkType.PREPARE else 1,
        input_fingerprint="c" * 64,
        execution_name="execution_1",
        status=status,
        attempt_count=1,
        next_dispatch_at=NOW,
        execution_arn=(
            None
            if status is not WorkRequestStatus.DISPATCHED
            else "arn:aws:states:us-west-2:123456789012:execution:machine:execution_1"
        ),
        created_at=NOW,
        updated_at=NOW,
    )


def _handlers(
    *,
    job: ControlJobRecord | None = None,
    work: WorkRequest | None = None,
    store: FakeStore | None = None,
) -> tuple[
    Phase6MachineHandlers,
    FakeDispatcher,
    FakePreparation,
    FakeProvider,
    FakeControl,
    FakePreparationSettlement,
]:
    actual_job = job or (store.job if store is not None else _job())
    actual_work = work or (store.work if store is not None else _work(job_id=actual_job.job_id))
    actual_store = store or FakeStore(actual_job, actual_work)
    dispatcher = FakeDispatcher()
    preparation = FakePreparation()
    provider = FakeProvider()
    control = FakeControl()
    prepare_settlement = FakePreparationSettlement()
    handlers = Phase6MachineHandlers(
        dispatcher=dispatcher,  # type: ignore[arg-type]
        preparation=preparation,  # type: ignore[arg-type]
        provider=provider,  # type: ignore[arg-type]
        store=actual_store,
        control=control,  # type: ignore[arg-type]
        preparation_settlement=prepare_settlement,  # type: ignore[arg-type]
    )
    return handlers, dispatcher, preparation, provider, control, prepare_settlement


def _stream_record(job_id: str, work_id: str, *, event_name: str = "INSERT") -> dict[str, Any]:
    return {
        "eventSource": "aws:dynamodb",
        "eventName": event_name,
        "dynamodb": {
            "Keys": {
                "PK": {"S": f"JOB#{job_id}"},
                "SK": {"S": f"WORK#{work_id}"},
            }
        },
    }


def test_dispatcher_runs_bounded_due_sweep() -> None:
    handlers, dispatcher, *_ = _handlers()
    dispatcher.due = (_work(), _work(job_id="job_2", work_id="work_2"))

    assert handlers.dispatch({"source": "due-work-sweeper"}) == {
        "attempted": 2,
        "dispatched": 2,
    }


def test_dispatcher_uses_exact_stream_keys_and_deduplicates_batch() -> None:
    handlers, dispatcher, *_ = _handlers()
    record = _stream_record("job_1", "work_1")

    result = handlers.dispatch({"Records": [record, record]})

    assert result == {"attempted": 1, "dispatched": 1}
    assert dispatcher.one_calls == [("job_1", "work_1")]


@pytest.mark.parametrize(
    "event",
    [
        {},
        {"source": "other"},
        {"Records": []},
        {"Records": [_stream_record("job_1", "work_1", event_name="REMOVE")]},
        {
            "Records": [
                {
                    **_stream_record("job_1", "work_1"),
                    "dynamodb": {"Keys": {"PK": {"S": "JOB#job_1"}}},
                }
            ]
        },
    ],
)
def test_dispatcher_rejects_every_noncanonical_event(event: dict[str, Any]) -> None:
    handlers, dispatcher, *_ = _handlers()

    with pytest.raises(Phase6MachineInvocationError):
        handlers.dispatch(event)

    assert dispatcher.one_calls == []


def test_preparation_returns_only_bridge_contract_and_masks_dependency_text() -> None:
    handlers, _dispatcher, preparation, *_ = _handlers()
    assert handlers.prepare({"job_id": "job_1", "work_request_id": "work_1"}) == {
        "framework": "strands-agents",
        "agent_id": "mr-lister-preparation",
    }
    preparation.error = RuntimeError("secret runtime detail")

    with pytest.raises(Phase6MachineExecutionError) as captured:
        handlers.prepare({"job_id": "job_1", "work_request_id": "work_1"})

    assert "secret runtime detail" not in str(captured.value)
    assert captured.value.__cause__ is None


@pytest.mark.parametrize(
    "operation",
    ["synchronize_product", "reconcile_product", "refresh_economics"],
)
def test_provider_routes_only_the_exact_allowlisted_operation(operation: str) -> None:
    handlers, _dispatcher, _preparation, provider, *_ = _handlers()

    result = handlers.run_provider(
        {"job_id": "job_1", "work_request_id": "work_1", "operation": operation}
    )

    assert result["state"] == "awaiting_approval"
    assert provider.calls == [(operation, "job_1", "work_1")]


def test_provider_rejects_unknown_or_extra_operation_without_a_call() -> None:
    handlers, _dispatcher, _preparation, provider, *_ = _handlers()

    with pytest.raises(Phase6MachineInvocationError):
        handlers.run_provider(
            {"job_id": "job_1", "work_request_id": "work_1", "operation": "publish"}
        )
    with pytest.raises(Phase6MachineInvocationError):
        handlers.run_provider(
            {
                "job_id": "job_1",
                "work_request_id": "work_1",
                "operation": "synchronize_product",
                "owner_id": OWNER,
            }
        )

    assert provider.calls == []


def test_completed_settlement_ignores_forged_worker_result() -> None:
    work = _work(status=WorkRequestStatus.COMPLETED)
    job = _job(state=ControlJobState.AWAITING_APPROVAL, work_id=None)
    handlers, *_ = _handlers(job=job, work=work)

    result = handlers.settle(
        {
            "job_id": "job_1",
            "work_request_id": "work_1",
            "work_type": "synchronize_product",
            "worker_result": {"state": "approved", "record_version": 999_999},
        }
    )

    assert result["state"] == "awaiting_approval"
    assert result["record_version"] == 4
    assert result["work_request_id"] is None


@pytest.mark.parametrize(
    ("work_type", "expected_code"),
    [
        (WorkType.SYNCHRONIZE_PRODUCT, WorkerFailureCode.PRODUCTION_UNAVAILABLE),
        (WorkType.RECONCILE_PRODUCT, WorkerFailureCode.PRODUCTION_UNAVAILABLE),
        (WorkType.REFRESH_ECONOMICS, WorkerFailureCode.ECONOMICS_UNAVAILABLE),
    ],
)
def test_active_nonprepare_settlement_records_only_fixed_failure_code(
    work_type: WorkType,
    expected_code: WorkerFailureCode,
) -> None:
    state = {
        WorkType.SYNCHRONIZE_PRODUCT: ControlJobState.PRODUCT_DRAFT_SYNCING,
        WorkType.RECONCILE_PRODUCT: ControlJobState.RECONCILIATION_REQUIRED,
        WorkType.REFRESH_ECONOMICS: ControlJobState.PRICING_REFRESHING,
    }[work_type]
    handlers, _dispatcher, _preparation, _provider, control, _prepare = _handlers(
        job=_job(state=state),
        work=_work(work_type=work_type),
    )

    result = handlers.settle(
        {
            "job_id": "job_1",
            "work_request_id": "work_1",
            "work_type": work_type.value,
            "worker_error_code": "arbitrary.Dependency.SecretText",
        }
    )

    assert result["state"] == "failed_retryable"
    assert len(control.failures) == 1
    assert control.failures[0].code is expected_code


def test_cancellation_intent_dominates_late_worker_failure() -> None:
    handlers, _dispatcher, _preparation, _provider, control, _prepare = _handlers(
        job=_job(cancellation=True),
    )

    result = handlers.settle(
        {
            "job_id": "job_1",
            "work_request_id": "work_1",
            "work_type": "synchronize_product",
            "worker_error_code": "States.Timeout",
        }
    )

    assert result["state"] == "cancelled"
    assert len(control.cancellations) == 1
    assert control.failures == []


def test_prepare_cancellation_uses_cancellation_settlement_not_failure_reconciler() -> None:
    handlers, _dispatcher, _preparation, _provider, control, prepare = _handlers(
        job=_job(cancellation=True),
        work=_work(work_type=WorkType.PREPARE),
    )

    result = handlers.settle(
        {
            "job_id": "job_1",
            "work_request_id": "work_1",
            "work_type": "prepare",
            "worker_error_code": "States.Timeout",
        }
    )

    assert result["state"] == "cancelled"
    assert len(control.cancellations) == 1
    assert control.failures == []
    assert prepare.calls == []


def test_prepare_settlement_rereads_when_cancellation_wins_after_outer_read() -> None:
    store = FakeStore(
        _job(state=ControlJobState.INTAKE_VALIDATED),
        _work(work_type=WorkType.PREPARE),
    )
    handlers, _dispatcher, _preparation, _provider, control, prepare = _handlers(store=store)

    def seller_cancels() -> None:
        store.job = _job(cancellation=True)
        prepare.hook = None
        raise PreparationSettlementError("stale PREPARE read")

    prepare.hook = seller_cancels

    result = handlers.settle(
        {
            "job_id": "job_1",
            "work_request_id": "work_1",
            "work_type": "prepare",
            "worker_error_code": "States.Timeout",
        }
    )

    assert result["state"] == "cancelled"
    assert len(prepare.calls) == 1
    assert len(control.cancellations) == 1


def test_prepare_completed_read_retries_a_stale_precancellation_job_snapshot() -> None:
    store = FakeStore(
        _job(cancellation=True),
        _work(work_type=WorkType.PREPARE, status=WorkRequestStatus.COMPLETED),
    )
    handlers, _dispatcher, _preparation, _provider, control, prepare = _handlers(store=store)

    def cancellation_settled() -> None:
        store.job = _job(
            state=ControlJobState.CANCELLED,
            cancellation=True,
            work_id=None,
        )
        prepare.hook = None
        raise PreparationSettlementError("mixed strong-read snapshot")

    prepare.hook = cancellation_settled

    result = handlers.settle(
        {
            "job_id": "job_1",
            "work_request_id": "work_1",
            "work_type": "prepare",
            "worker_result": {"untrusted": True},
        }
    )

    assert result["state"] == "cancelled"
    assert len(prepare.calls) == 1
    assert control.cancellations == []


def test_settlement_rereads_completed_authority_after_worker_wins_cas_race() -> None:
    store = FakeStore(_job(), _work())
    handlers, _dispatcher, _preparation, _provider, control, _prepare = _handlers(store=store)

    def worker_wins() -> None:
        store.job = _job(state=ControlJobState.AWAITING_APPROVAL, work_id=None)
        store.work = _work(status=WorkRequestStatus.COMPLETED)
        raise ConcurrentControlModificationError("worker completed")

    control.failure_hook = worker_wins

    result = handlers.settle(
        {
            "job_id": "job_1",
            "work_request_id": "work_1",
            "work_type": "synchronize_product",
            "worker_error_code": "States.Timeout",
        }
    )

    assert result["state"] == "awaiting_approval"
    assert len(control.failures) == 1


def test_settlement_rereads_and_honors_cancellation_after_cas_race() -> None:
    store = FakeStore(_job(), _work())
    handlers, _dispatcher, _preparation, _provider, control, _prepare = _handlers(store=store)

    def seller_cancels() -> None:
        store.job = _job(cancellation=True)
        control.failure_hook = None
        raise ConcurrentControlModificationError("seller cancelled")

    control.failure_hook = seller_cancels

    result = handlers.settle(
        {
            "job_id": "job_1",
            "work_request_id": "work_1",
            "work_type": "synchronize_product",
            "worker_error_code": "States.Timeout",
        }
    )

    assert result["state"] == "cancelled"
    assert len(control.failures) == 1
    assert len(control.cancellations) == 1


def test_settlement_bounds_repeated_cas_changes_and_detaches_details() -> None:
    handlers, _dispatcher, _preparation, _provider, control, _prepare = _handlers()

    def keep_changing() -> None:
        raise ConcurrentControlModificationError("private changing authority")

    control.failure_hook = keep_changing

    with pytest.raises(Phase6MachineExecutionError) as captured:
        handlers.settle(
            {
                "job_id": "job_1",
                "work_request_id": "work_1",
                "work_type": "synchronize_product",
                "worker_error_code": "States.Timeout",
            }
        )

    assert len(control.failures) == 3
    assert "private changing authority" not in str(captured.value)
    assert captured.value.__cause__ is None


def test_prepare_settlement_always_uses_strong_read_reconciler() -> None:
    handlers, *_prefix, prepare_settlement = _handlers(
        job=_job(state=ControlJobState.INTAKE_VALIDATED),
        work=_work(work_type=WorkType.PREPARE),
    )

    result = handlers.settle(
        {
            "job_id": "job_1",
            "work_request_id": "work_1",
            "work_type": "prepare",
            "worker_result": {"untrusted": True},
        }
    )

    assert result["state"] == "product_draft_syncing"
    assert prepare_settlement.calls == [("job_1", "work_1")]


def test_settlement_rejects_work_type_drift_without_a_command() -> None:
    handlers, _dispatcher, _preparation, _provider, control, _prepare = _handlers()

    with pytest.raises(Phase6MachineInvocationError):
        handlers.settle(
            {
                "job_id": "job_1",
                "work_request_id": "work_1",
                "work_type": "refresh_economics",
                "worker_error_code": "States.Timeout",
            }
        )

    assert control.failures == []
    assert control.cancellations == []


def test_settlement_requires_exactly_one_bounded_terminal_signal() -> None:
    handlers, *_ = _handlers()
    base = {
        "job_id": "job_1",
        "work_request_id": "work_1",
        "work_type": "synchronize_product",
    }

    for event in (
        base,
        {**base, "worker_result": {}, "worker_error_code": "States.Timeout"},
        {**base, "worker_result": "not-an-object"},
        {**base, "worker_error_code": "x" * 257},
    ):
        with pytest.raises(Phase6MachineInvocationError):
            handlers.settle(event)
