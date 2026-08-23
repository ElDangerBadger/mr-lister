from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Barrier, Lock
from typing import Any

import pytest
from pydantic import ValidationError

from mr_lister.control.commands import WorkerFailureCode
from mr_lister.control.dispatch import (
    deterministic_execution_name,
    execution_arn_for,
    work_input_fingerprint,
)
from mr_lister.control.errors import ConcurrentControlModificationError
from mr_lister.control.execution_recovery import (
    ExecutionAuthoritySnapshot,
    ExecutionObservation,
    ExecutionRecoveryBoundaryInvalidError,
    ExecutionRecoveryDependencyUnavailableError,
    ExecutionRecoveryExecutionError,
    ExecutionRecoveryHandler,
    ExecutionRecoveryInvocationError,
    ExecutionRecoverySweepResult,
    ExecutionStatus,
    StrandedExecutionCandidate,
    StuckExecutionRecoverySweeper,
)
from mr_lister.control.models import (
    CommandResponse,
    ControlJobRecord,
    ControlJobState,
    WorkRequest,
    WorkRequestStatus,
    WorkType,
)
from mr_lister.control.settlement import PreparationSettlementResult

NOW = datetime(2026, 8, 23, 16, 0, tzinfo=UTC)
OWNER_ID = "a" * 64
REVIEW_FINGERPRINT = "b" * 64
JOB_ID = "job_recovery_001"
WORK_ID = "work_recovery_001"


def _state_machine_arns() -> dict[WorkType, str]:
    return {
        work_type: (
            "arn:aws:states:us-west-2:123456789012:stateMachine:"
            f"mr-lister-phase6-{work_type.value.replace('_', '-')}-dev"
        )
        for work_type in WorkType
    }


def _work(
    *,
    job_id: str = JOB_ID,
    work_id: str = WORK_ID,
    work_type: WorkType = WorkType.SYNCHRONIZE_PRODUCT,
    status: WorkRequestStatus = WorkRequestStatus.DISPATCHED,
    updated_at: datetime = NOW - timedelta(minutes=30),
) -> WorkRequest:
    execution_name = deterministic_execution_name(work_id)
    execution_arn = execution_arn_for(_state_machine_arns()[work_type], execution_name)
    return WorkRequest(
        work_request_id=work_id,
        owner_id=OWNER_ID,
        job_id=job_id,
        receipt_id="receipt_recovery_001",
        work_type=work_type,
        review_version=None if work_type is WorkType.PREPARE else 1,
        input_fingerprint=work_input_fingerprint(
            work_type=work_type,
            job_id=job_id,
            work_request_id=work_id,
        ),
        execution_name=execution_name,
        status=status,
        attempt_count=1,
        next_dispatch_at=updated_at,
        execution_arn=execution_arn,
        last_error_code=(
            WorkerFailureCode.PRODUCTION_UNAVAILABLE.value
            if status is WorkRequestStatus.COMPLETED
            else None
        ),
        created_at=updated_at - timedelta(minutes=1),
        updated_at=updated_at,
    )


def _job(
    *,
    job_id: str = JOB_ID,
    work_id: str | None = WORK_ID,
    work_type: WorkType = WorkType.SYNCHRONIZE_PRODUCT,
    cancellation: bool = False,
    settled: bool = False,
) -> ControlJobRecord:
    if settled:
        return ControlJobRecord(
            owner_id=OWNER_ID,
            job_id=job_id,
            record_version=8,
            event_sequence=8,
            state=ControlJobState.FAILED_RETRYABLE,
            review_version=1,
            review_fingerprint=REVIEW_FINGERPRINT,
            failure_id="failure_recovery_001",
            created_at=NOW - timedelta(hours=1),
            updated_at=NOW,
        )
    state = {
        WorkType.PREPARE: ControlJobState.ANALYZING_ARTWORK,
        WorkType.SYNCHRONIZE_PRODUCT: ControlJobState.PRODUCT_DRAFT_SYNCING,
        WorkType.RECONCILE_PRODUCT: ControlJobState.RECONCILIATION_REQUIRED,
        WorkType.REFRESH_ECONOMICS: ControlJobState.PRICING_REFRESHING,
    }[work_type]
    if cancellation:
        state = ControlJobState.CANCEL_REQUESTED
    return ControlJobRecord(
        owner_id=OWNER_ID,
        job_id=job_id,
        record_version=7,
        event_sequence=7,
        state=state,
        review_version=0 if work_type is WorkType.PREPARE else 1,
        review_fingerprint=None if work_type is WorkType.PREPARE else REVIEW_FINGERPRINT,
        active_work_request_id=work_id,
        cancellation_requested_at=NOW - timedelta(minutes=25) if cancellation else None,
        created_at=NOW - timedelta(hours=1),
        updated_at=NOW - timedelta(minutes=30),
    )


def _snapshot(
    *,
    work_type: WorkType = WorkType.SYNCHRONIZE_PRODUCT,
    cancellation: bool = False,
    updated_at: datetime = NOW - timedelta(minutes=30),
) -> ExecutionAuthoritySnapshot:
    return ExecutionAuthoritySnapshot(
        job=_job(work_type=work_type, cancellation=cancellation),
        work=_work(work_type=work_type, updated_at=updated_at),
    )


def _observation(
    work: WorkRequest,
    *,
    status: ExecutionStatus = ExecutionStatus.FAILED,
    input_override: str | None = None,
    state_machine_arn: str | None = None,
) -> ExecutionObservation:
    terminal = status is not ExecutionStatus.RUNNING
    return ExecutionObservation(
        execution_arn=work.execution_arn,
        state_machine_arn=state_machine_arn or _state_machine_arns()[work.work_type],
        name=work.execution_name,
        input=(
            input_override
            if input_override is not None
            else json.dumps(
                {"job_id": work.job_id, "work_request_id": work.work_request_id},
                sort_keys=True,
                separators=(",", ":"),
            )
        ),
        status=status,
        start_date=work.updated_at,
        stop_date=NOW - timedelta(minutes=10) if terminal else None,
    )


class _Inventory:
    def __init__(self, candidates: tuple[StrandedExecutionCandidate, ...] | Exception) -> None:
        self.candidates = candidates
        self.calls: list[tuple[datetime, int]] = []

    def list_stranded_execution_candidates(
        self,
        *,
        dispatched_before: datetime,
        limit: int,
    ) -> tuple[StrandedExecutionCandidate, ...]:
        self.calls.append((dispatched_before, limit))
        if isinstance(self.candidates, Exception):
            raise self.candidates
        return self.candidates


class _Authority:
    def __init__(
        self,
        snapshot: ExecutionAuthoritySnapshot | Exception,
        *,
        sequence: list[ExecutionAuthoritySnapshot | Exception] | None = None,
    ) -> None:
        self.snapshot = snapshot
        self.sequence = sequence or []
        self.calls: list[tuple[str, str]] = []

    def read_execution_authority_strong(
        self,
        *,
        job_id: str,
        work_request_id: str,
    ) -> ExecutionAuthoritySnapshot:
        self.calls.append((job_id, work_request_id))
        value = self.sequence.pop(0) if self.sequence else self.snapshot
        if isinstance(value, Exception):
            raise value
        return value


class _Executions:
    def __init__(self, observation: ExecutionObservation | None | Exception) -> None:
        self.observation = observation
        self.calls: list[str] = []

    def describe_exact_execution(self, *, execution_arn: str) -> ExecutionObservation | None:
        self.calls.append(execution_arn)
        if isinstance(self.observation, Exception):
            raise self.observation
        return self.observation


class _Control:
    def __init__(
        self,
        *,
        failure_state: ControlJobState = ControlJobState.FAILED_RETRYABLE,
        failure_errors: list[Exception] | None = None,
    ) -> None:
        self.failure_state = failure_state
        self.failure_errors = failure_errors or []
        self.failures: list[Any] = []
        self.cancellations: list[Any] = []

    def record_worker_failure(self, command: Any) -> CommandResponse:
        self.failures.append(command)
        if self.failure_errors:
            raise self.failure_errors.pop(0)
        return CommandResponse(
            job_id=command.job_id,
            state=self.failure_state,
            record_version=command.expected_record_version + 1,
            review_version=1,
            work_request_id=(
                "work_reconcile_next"
                if self.failure_state is ControlJobState.RECONCILIATION_REQUIRED
                else None
            ),
        )

    def settle_cancellation(self, command: Any) -> CommandResponse:
        self.cancellations.append(command)
        return CommandResponse(
            job_id=command.job_id,
            state=ControlJobState.CANCELLED,
            record_version=command.expected_record_version + 1,
            review_version=1,
        )


class _Preparation:
    def __init__(
        self,
        *,
        state: ControlJobState = ControlJobState.PRODUCT_DRAFT_SYNCING,
    ) -> None:
        self.state = state
        self.calls: list[tuple[str, str]] = []

    def settle_unavailable(
        self,
        *,
        job_id: str,
        work_request_id: str,
    ) -> PreparationSettlementResult:
        self.calls.append((job_id, work_request_id))
        return PreparationSettlementResult(
            outcome=(
                "completed_readback"
                if self.state is ControlJobState.PRODUCT_DRAFT_SYNCING
                else "failure_recorded"
            ),
            response=CommandResponse(
                job_id=job_id,
                state=self.state,
                record_version=8,
                review_version=1,
                work_request_id=(
                    "work_sync_next"
                    if self.state is ControlJobState.PRODUCT_DRAFT_SYNCING
                    else None
                ),
            ),
        )


def _candidate(job_id: str = JOB_ID, work_id: str = WORK_ID) -> StrandedExecutionCandidate:
    return StrandedExecutionCandidate(job_id=job_id, work_request_id=work_id)


def _sweeper(
    *,
    snapshot: ExecutionAuthoritySnapshot | None = None,
    inventory: _Inventory | None = None,
    authority: _Authority | None = None,
    executions: _Executions | None = None,
    control: _Control | None = None,
    preparation: _Preparation | None = None,
    **overrides: Any,
) -> tuple[
    StuckExecutionRecoverySweeper,
    _Inventory,
    _Authority,
    _Executions,
    _Control,
    _Preparation,
]:
    exact = snapshot or _snapshot()
    exact_inventory = inventory or _Inventory((_candidate(),))
    exact_authority = authority or _Authority(exact)
    assert exact.work is not None
    exact_executions = executions or _Executions(_observation(exact.work))
    exact_control = control or _Control()
    exact_preparation = preparation or _Preparation()
    return (
        StuckExecutionRecoverySweeper(
            inventory=exact_inventory,
            authority=exact_authority,
            executions=exact_executions,
            control=exact_control,
            preparation_settlement=exact_preparation,
            state_machine_arns=_state_machine_arns(),
            clock=lambda: NOW,
            **overrides,
        ),
        exact_inventory,
        exact_authority,
        exact_executions,
        exact_control,
        exact_preparation,
    )


def test_terminal_execution_settles_only_through_fixed_failure_command() -> None:
    sweeper, inventory, _authority_reader, executions, control, preparation = _sweeper()

    result = sweeper.sweep()

    assert result.failure_settled == 1
    assert result.terminal_executions_observed == 1
    assert result.alarm_signal_count == 0
    assert inventory.calls == [(NOW - timedelta(minutes=20), 25)]
    assert executions.calls == [_snapshot().work.execution_arn]  # type: ignore[union-attr]
    assert len(control.failures) == 1
    assert control.failures[0].code is WorkerFailureCode.PRODUCTION_UNAVAILABLE
    assert control.failures[0].expected_record_version == 7
    assert control.cancellations == []
    assert preparation.calls == []


def test_missing_execution_is_settled_without_dispatch_or_redrive_capability() -> None:
    sweeper, _inventory, _authority_reader, _executions, control, _preparation = _sweeper(
        executions=_Executions(None)
    )

    result = sweeper.sweep()

    assert result.executions_missing == 1
    assert result.failure_settled == 1
    assert len(control.failures) == 1
    assert set(vars(sweeper)) == {
        "_inventory",
        "_authority",
        "_executions",
        "_control",
        "_preparation_settlement",
        "_state_machine_arns",
        "_clock",
        "_stale_after",
        "_batch_limit",
        "_maximum_cas_rechecks",
    }


@pytest.mark.parametrize("status", [ExecutionStatus.RUNNING])
def test_running_execution_past_bound_is_alarm_only(status: ExecutionStatus) -> None:
    snapshot = _snapshot()
    assert snapshot.work is not None
    sweeper, _inventory, _authority_reader, _executions, control, _preparation = _sweeper(
        snapshot=snapshot,
        executions=_Executions(_observation(snapshot.work, status=status)),
    )

    result = sweeper.sweep()

    assert result.running_past_bound == 1
    assert result.alarm_signal_count == 1
    assert result.requires_operator_attention is True
    assert control.failures == []
    assert control.cancellations == []


def test_pending_redrive_is_alarm_only_and_never_settled_or_redriven() -> None:
    snapshot = _snapshot()
    assert snapshot.work is not None
    sweeper, _inventory, _authority_reader, _executions, control, _preparation = _sweeper(
        snapshot=snapshot,
        executions=_Executions(_observation(snapshot.work, status=ExecutionStatus.PENDING_REDRIVE)),
    )

    result = sweeper.sweep()

    assert result.running_past_bound == 1
    assert result.failure_settled == 0
    assert result.terminal_executions_observed == 0
    assert result.alarm_signal_count == 1
    assert control.failures == []


def test_exact_execution_identity_and_canonical_input_are_required() -> None:
    snapshot = _snapshot()
    assert snapshot.work is not None
    observation = _observation(
        snapshot.work,
        input_override=json.dumps(
            {"job_id": JOB_ID, "work_request_id": WORK_ID, "operation": "publish"},
            sort_keys=True,
            separators=(",", ":"),
        ),
    )
    sweeper, _inventory, _authority_reader, _executions, control, _preparation = _sweeper(
        snapshot=snapshot,
        executions=_Executions(observation),
    )

    result = sweeper.sweep()

    assert result.authority_conflicts == 1
    assert result.alarm_signal_count == 1
    assert control.failures == []


def test_stored_dispatch_fingerprint_is_rebound_before_describe() -> None:
    snapshot = _snapshot()
    assert snapshot.work is not None and snapshot.job is not None
    invalid_work = WorkRequest.model_validate(
        {**snapshot.work.model_dump(mode="python"), "input_fingerprint": "f" * 64}
    )
    invalid = ExecutionAuthoritySnapshot(job=snapshot.job, work=invalid_work)
    executions = _Executions(_observation(invalid_work))
    sweeper, _inventory, _authority_reader, _executions, control, _preparation = _sweeper(
        snapshot=invalid,
        executions=executions,
    )

    result = sweeper.sweep()

    assert result.authority_conflicts == 1
    assert executions.calls == []
    assert control.failures == []


def test_strong_recheck_prevents_settlement_when_work_became_complete() -> None:
    active = _snapshot()
    assert active.work is not None
    completed = ExecutionAuthoritySnapshot(
        job=_job(settled=True),
        work=_work(status=WorkRequestStatus.COMPLETED, updated_at=NOW),
    )
    authority = _Authority(active, sequence=[active, completed])
    sweeper, _inventory, _authority_reader, _executions, control, _preparation = _sweeper(
        snapshot=active,
        authority=authority,
    )

    result = sweeper.sweep()

    assert result.already_settled == 1
    assert control.failures == []
    assert len(authority.calls) == 2


def test_cas_loss_rechecks_and_observes_worker_completion() -> None:
    active = _snapshot()
    completed = ExecutionAuthoritySnapshot(
        job=_job(settled=True),
        work=_work(status=WorkRequestStatus.COMPLETED, updated_at=NOW),
    )
    authority = _Authority(active, sequence=[active, active, completed])
    control = _Control(failure_errors=[ConcurrentControlModificationError("private")])
    sweeper, _inventory, _authority_reader, _executions, _control, _preparation = _sweeper(
        snapshot=active,
        authority=authority,
        control=control,
    )

    result = sweeper.sweep()

    assert result.already_settled == 1
    assert len(control.failures) == 1
    assert result.settlement_exhausted == 0


def test_persistent_cas_race_is_bounded_and_alarms() -> None:
    active = _snapshot()
    control = _Control(
        failure_errors=[ConcurrentControlModificationError("private") for _ in range(4)]
    )
    sweeper, _inventory, authority, _executions, _control, _preparation = _sweeper(
        snapshot=active,
        control=control,
        maximum_cas_rechecks=2,
    )

    result = sweeper.sweep()

    assert result.settlement_exhausted == 1
    assert result.alarm_signal_count == 1
    assert len(control.failures) == 3
    assert len(authority.calls) == 4  # initial authority plus three settlement attempts


def test_consumed_provider_uncertainty_response_is_classified_as_reconciliation() -> None:
    control = _Control(failure_state=ControlJobState.RECONCILIATION_REQUIRED)
    sweeper, _inventory, _authority_reader, _executions, _control, _preparation = _sweeper(
        control=control
    )

    result = sweeper.sweep()

    assert result.reconciliation_routed == 1
    assert result.failure_settled == 0
    assert control.failures[0].code is WorkerFailureCode.PRODUCTION_UNAVAILABLE


def test_cancellation_dominates_terminal_execution_settlement() -> None:
    snapshot = _snapshot(cancellation=True)
    sweeper, _inventory, _authority_reader, _executions, control, _preparation = _sweeper(
        snapshot=snapshot
    )

    result = sweeper.sweep()

    assert result.cancellation_settled == 1
    assert len(control.cancellations) == 1
    assert control.failures == []


def test_prepare_uses_strands_aware_preparation_reconciler() -> None:
    snapshot = _snapshot(work_type=WorkType.PREPARE)
    preparation = _Preparation(state=ControlJobState.PRODUCT_DRAFT_SYNCING)
    sweeper, _inventory, _authority_reader, _executions, control, _preparation = _sweeper(
        snapshot=snapshot,
        preparation=preparation,
    )

    result = sweeper.sweep()

    assert result.recovered_completion == 1
    assert preparation.calls == [(JOB_ID, WORK_ID)]
    assert control.failures == []


def test_refresh_economics_uses_fixed_sanitized_code() -> None:
    snapshot = _snapshot(work_type=WorkType.REFRESH_ECONOMICS)
    sweeper, _inventory, _authority_reader, _executions, control, _preparation = _sweeper(
        snapshot=snapshot
    )

    result = sweeper.sweep()

    assert result.failure_settled == 1
    assert control.failures[0].code is WorkerFailureCode.ECONOMICS_UNAVAILABLE


def test_recent_candidate_is_rejected_by_fresh_strong_authority() -> None:
    snapshot = _snapshot(updated_at=NOW - timedelta(minutes=5))
    executions = _Executions(None)
    sweeper, _inventory, _authority_reader, _executions, control, _preparation = _sweeper(
        snapshot=snapshot,
        executions=executions,
    )

    result = sweeper.sweep()

    assert result.not_due == 1
    assert executions.calls == []
    assert control.failures == []


def test_observer_dependency_failure_is_sanitized_and_does_not_mutate() -> None:
    secret = "secret-provider-message job_recovery_001"
    sweeper, _inventory, _authority_reader, _executions, control, _preparation = _sweeper(
        executions=_Executions(RuntimeError(secret))
    )

    result = sweeper.sweep()
    rendered = json.dumps(result.model_dump(mode="json"), sort_keys=True)

    assert result.dependency_unavailable == 1
    assert result.alarm_signal_count == 1
    assert secret not in rendered
    assert JOB_ID not in rendered
    assert WORK_ID not in rendered
    assert control.failures == []


def test_missing_paired_authority_is_an_alarm_not_an_implicit_replay() -> None:
    absent = ExecutionAuthoritySnapshot()
    sweeper, _inventory, _authority_reader, executions, control, _preparation = _sweeper(
        authority=_Authority(absent)
    )

    result = sweeper.sweep()

    assert result.authority_conflicts == 1
    assert executions.calls == []
    assert control.failures == []


def test_candidate_inventory_is_bounded_unique_and_deterministic() -> None:
    first_job = "job_recovery_a"
    second_job = "job_recovery_b"
    first = _snapshot()
    second = ExecutionAuthoritySnapshot(
        job=_job(job_id=second_job),
        work=_work(job_id=second_job),
    )

    class OrderedAuthority(_Authority):
        def read_execution_authority_strong(
            self,
            *,
            job_id: str,
            work_request_id: str,
        ) -> ExecutionAuthoritySnapshot:
            self.calls.append((job_id, work_request_id))
            return first if job_id == first_job else second

    # Use already-settled rows to avoid needing an observer per identity.
    first = ExecutionAuthoritySnapshot(
        job=_job(job_id=first_job, settled=True),
        work=_work(job_id=first_job, status=WorkRequestStatus.COMPLETED, updated_at=NOW),
    )
    second = ExecutionAuthoritySnapshot(
        job=_job(job_id=second_job, settled=True),
        work=_work(job_id=second_job, status=WorkRequestStatus.COMPLETED, updated_at=NOW),
    )
    inventory = _Inventory(
        (
            _candidate(second_job, WORK_ID),
            _candidate(first_job, WORK_ID),
        )
    )
    authority = OrderedAuthority(first)
    sweeper, _inventory, _authority_reader, _executions, _control, _preparation = _sweeper(
        inventory=inventory,
        authority=authority,
        batch_limit=2,
    )

    result = sweeper.sweep()

    assert result.already_settled == 2
    assert result.batch_limit_reached is True
    assert authority.calls == [(first_job, WORK_ID), (second_job, WORK_ID)]

    duplicate_inventory = _Inventory((_candidate(), _candidate()))
    duplicate_sweeper, *_ = _sweeper(inventory=duplicate_inventory)
    with pytest.raises(ExecutionRecoveryBoundaryInvalidError):
        duplicate_sweeper.sweep()


def test_inventory_failure_raises_only_identifier_free_error() -> None:
    sweeper, *_ = _sweeper(inventory=_Inventory(RuntimeError("private inventory secret")))

    with pytest.raises(ExecutionRecoveryDependencyUnavailableError) as raised:
        sweeper.sweep()

    assert str(raised.value) == "Execution recovery inventory is unavailable"


def test_strict_contracts_reject_coercion_and_incoherent_counters() -> None:
    work = _work()
    with pytest.raises(ValidationError):
        ExecutionObservation(
            execution_arn=work.execution_arn,
            state_machine_arn=_state_machine_arns()[work.work_type],
            name=work.execution_name,
            input="{}",
            status="FAILED",  # type: ignore[arg-type]
            start_date=work.updated_at,
            stop_date=NOW,
        )
    with pytest.raises(ValidationError):
        ExecutionRecoverySweepResult(
            candidates_scanned=1,
            already_settled=0,
            not_due=0,
            running_past_bound=0,
            recovered_completion=0,
            failure_settled=0,
            reconciliation_routed=0,
            cancellation_settled=0,
            authority_conflicts=0,
            dependency_unavailable=0,
            settlement_exhausted=0,
            terminal_executions_observed=0,
            executions_missing=0,
            batch_limit=25,
            batch_limit_reached=False,
            alarm_signal_count=0,
            requires_operator_attention=False,
        )


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"stale_after": timedelta(minutes=19)}, "stale age"),
        ({"stale_after": timedelta(days=2)}, "stale age"),
        ({"batch_limit": 0}, "batch limit"),
        ({"batch_limit": 101}, "batch limit"),
        ({"maximum_cas_rechecks": 0}, "CAS rechecks"),
        ({"maximum_cas_rechecks": 4}, "CAS rechecks"),
    ],
)
def test_configuration_is_bounded(overrides: dict[str, Any], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        _sweeper(**overrides)


def test_scheduled_handler_has_exact_event_and_sanitized_error_contract() -> None:
    sweeper, *_ = _sweeper(inventory=_Inventory(()))
    handler = ExecutionRecoveryHandler(sweeper=sweeper)

    response = handler({"source": "stuck-execution-sweeper"})

    assert response["contract_version"] == "1.0.0"
    assert response["candidates_scanned"] == 0
    with pytest.raises(ExecutionRecoveryInvocationError):
        handler({"source": "stuck-execution-sweeper", "limit": 1000})

    failed, *_ = _sweeper(inventory=_Inventory(RuntimeError("private dependency detail")))
    with pytest.raises(ExecutionRecoveryExecutionError) as raised:
        ExecutionRecoveryHandler(sweeper=failed)({"source": "stuck-execution-sweeper"})
    assert str(raised.value) == "Stuck-execution recovery failed safely"


class _ConcurrentAuthority:
    def __init__(self, active: ExecutionAuthoritySnapshot) -> None:
        self.current = active
        self.lock = Lock()

    def read_execution_authority_strong(
        self,
        *,
        job_id: str,
        work_request_id: str,
    ) -> ExecutionAuthoritySnapshot:
        with self.lock:
            return self.current

    def complete(self) -> None:
        with self.lock:
            self.current = ExecutionAuthoritySnapshot(
                job=_job(settled=True),
                work=_work(status=WorkRequestStatus.COMPLETED, updated_at=NOW),
            )


class _ConcurrentControl(_Control):
    def __init__(self, authority: _ConcurrentAuthority) -> None:
        super().__init__()
        self.authority = authority
        self.barrier = Barrier(2)
        self.lock = Lock()
        self.committed = False
        self.commit_count = 0

    def record_worker_failure(self, command: Any) -> CommandResponse:
        self.failures.append(command)
        self.barrier.wait(timeout=5)
        with self.lock:
            if self.committed:
                raise ConcurrentControlModificationError("lost CAS")
            self.committed = True
            self.commit_count += 1
            self.authority.complete()
        return CommandResponse(
            job_id=command.job_id,
            state=ControlJobState.FAILED_RETRYABLE,
            record_version=command.expected_record_version + 1,
            review_version=1,
        )


def test_two_concurrent_sweepers_produce_one_effective_settlement() -> None:
    active = _snapshot()
    assert active.work is not None
    authority = _ConcurrentAuthority(active)
    control = _ConcurrentControl(authority)
    inventory = _Inventory((_candidate(),))
    executions = _Executions(_observation(active.work))
    sweeper, *_ = _sweeper(
        snapshot=active,
        inventory=inventory,
        authority=authority,  # type: ignore[arg-type]
        executions=executions,
        control=control,
    )

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = tuple(pool.map(lambda _index: sweeper.sweep(), range(2)))

    assert control.commit_count == 1
    assert len(control.failures) == 2
    assert sorted((item.failure_settled, item.already_settled) for item in results) == [
        (0, 1),
        (1, 0),
    ]
