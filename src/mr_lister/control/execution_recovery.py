"""Bounded recovery for Phase 6 executions whose settlement never became durable.

The recovery boundary is intentionally incapable of dispatching, stopping, redriving, or
invoking provider work.  Candidate inventory is only a hint.  Every decision is rebound to a
strong, paired Job/Work snapshot and an exact Step Functions execution observation before the
existing CAS-protected settlement commands are used.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Annotated, Any, Literal, Protocol

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, StringConstraints, model_validator

from mr_lister.control.commands import (
    RecordWorkerFailureCommand,
    SettleCancellationCommand,
    WorkerFailureCode,
)
from mr_lister.control.dispatch import (
    DispatchConfigurationError,
    deterministic_execution_name,
    execution_arn_for,
    work_input_fingerprint,
)
from mr_lister.control.errors import (
    ConcurrentControlModificationError,
    InvalidControlStateError,
    WorkNotActiveError,
)
from mr_lister.control.models import (
    CONTROL_NEW_WORK_BY_STATE,
    CommandResponse,
    ControlJobRecord,
    ControlJobState,
    WorkRequest,
    WorkRequestStatus,
    WorkType,
)
from mr_lister.control.settlement import (
    PreparationSettlementError,
    PreparationSettlementResult,
)

EXECUTION_RECOVERY_CONTRACT_VERSION = "1.0.0"
ExecutionRecoveryContractVersion = Literal["1.0.0"]

DEFAULT_EXECUTION_STALE_AFTER = timedelta(minutes=20)
DEFAULT_EXECUTION_RECOVERY_BATCH_LIMIT = 25
DEFAULT_EXECUTION_RECOVERY_CAS_RECHECKS = 2
# The deployed workflows have a 900-second execution timeout. Recovery adds a full five-minute
# observation/clock-skew margin so it cannot compete with ordinary task and settlement retries.
MINIMUM_EXECUTION_STALE_AFTER = timedelta(minutes=20)
MAXIMUM_EXECUTION_STALE_AFTER = timedelta(days=1)
MAXIMUM_EXECUTION_RECOVERY_BATCH_LIMIT = 100
MAXIMUM_EXECUTION_RECOVERY_CAS_RECHECKS = 3

EXECUTION_RECOVERY_SWEEP_SOURCE = "stuck-execution-sweeper"
_MAX_CLOCK_SKEW = timedelta(minutes=5)
_SAFE_ID = r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$"
_STATE_MACHINE_ARN = (
    r"^arn:(aws|aws-us-gov|aws-cn):states:[a-z0-9-]+:\d{12}:"
    r"stateMachine:[A-Za-z0-9_-]{1,80}$"
)
_EXECUTION_ARN = (
    r"^arn:(aws|aws-us-gov|aws-cn):states:[a-z0-9-]+:\d{12}:"
    r"execution:[A-Za-z0-9_-]{1,80}:[A-Za-z0-9_-]{1,80}$"
)

SafeId = Annotated[str, StringConstraints(pattern=_SAFE_ID)]
StateMachineArn = Annotated[str, StringConstraints(pattern=_STATE_MACHINE_ARN)]
ExecutionArn = Annotated[str, StringConstraints(pattern=_EXECUTION_ARN)]
CanonicalExecutionInput = Annotated[str, StringConstraints(min_length=2, max_length=1024)]


class ExecutionRecoveryError(RuntimeError):
    """Stable, identifier-free base error for the recovery boundary."""


class ExecutionRecoveryBoundaryInvalidError(ExecutionRecoveryError):
    """A configured or dependency-supplied authority shape is invalid."""


class ExecutionRecoveryDependencyUnavailableError(ExecutionRecoveryError):
    """The bounded candidate inventory could not be read safely."""


class ExecutionRecoveryInvocationError(ExecutionRecoveryError):
    """The Lambda event is outside the exact scheduled invocation contract."""


class ExecutionRecoveryExecutionError(ExecutionRecoveryError):
    """Value-free Lambda failure emitted instead of dependency details."""


class ExecutionStatus(StrEnum):
    """Closed Step Functions Standard execution states accepted from an adapter."""

    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    TIMED_OUT = "TIMED_OUT"
    ABORTED = "ABORTED"
    PENDING_REDRIVE = "PENDING_REDRIVE"


TERMINAL_EXECUTION_STATUSES = frozenset(
    {
        ExecutionStatus.SUCCEEDED,
        ExecutionStatus.FAILED,
        ExecutionStatus.TIMED_OUT,
        ExecutionStatus.ABORTED,
    }
)


class RecoveryDisposition(StrEnum):
    """Mutually exclusive classification for one strongly rebound candidate."""

    ALREADY_SETTLED = "already_settled"
    NOT_DUE = "not_due"
    RUNNING_PAST_BOUND = "running_past_bound"
    RECOVERED_COMPLETION = "recovered_completion"
    FAILURE_SETTLED = "failure_settled"
    RECONCILIATION_ROUTED = "reconciliation_routed"
    CANCELLATION_SETTLED = "cancellation_settled"
    AUTHORITY_CONFLICT = "authority_conflict"
    DEPENDENCY_UNAVAILABLE = "dependency_unavailable"
    SETTLEMENT_EXHAUSTED = "settlement_exhausted"


class ExecutionRecoveryModel(BaseModel):
    """Strict immutable model for recovery-only boundaries."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    contract_version: ExecutionRecoveryContractVersion = EXECUTION_RECOVERY_CONTRACT_VERSION


class StrandedExecutionCandidate(ExecutionRecoveryModel):
    """Opaque durable identity discovered from the DISPATCHED-work recovery index."""

    job_id: SafeId
    work_request_id: SafeId


class ExecutionAuthoritySnapshot(ExecutionRecoveryModel):
    """One transactionally consistent Job/Work pair; both absent proves no authority."""

    job: ControlJobRecord | None = None
    work: WorkRequest | None = None

    @model_validator(mode="after")
    def rows_are_both_present_or_both_absent(self) -> ExecutionAuthoritySnapshot:
        if (self.job is None) != (self.work is None):
            raise ValueError("Execution authority rows must be both present or both absent")
        return self


class ExecutionObservation(ExecutionRecoveryModel):
    """Closed, identity-bearing result of DescribeExecution for one exact ARN."""

    execution_arn: ExecutionArn
    state_machine_arn: StateMachineArn
    name: SafeId
    input: CanonicalExecutionInput
    status: ExecutionStatus
    start_date: AwareDatetime
    stop_date: AwareDatetime | None = None

    @model_validator(mode="after")
    def timestamps_match_status(self) -> ExecutionObservation:
        if self.status in TERMINAL_EXECUTION_STATUSES and self.stop_date is None:
            raise ValueError("Terminal execution observations require a stop time")
        if self.status is ExecutionStatus.RUNNING and self.stop_date is not None:
            raise ValueError("Running execution observations cannot carry a stop time")
        # PENDING_REDRIVE is active authority. AWS may retain the prior failed run's stop time
        # while the redrive is pending, so either timestamp shape is accepted and never settled.
        if self.stop_date is not None and self.stop_date < self.start_date:
            raise ValueError("Execution stop time cannot precede its start")
        return self


class ExecutionRecoverySweepResult(ExecutionRecoveryModel):
    """Sanitized operational evidence: bounded counters and no durable identifiers."""

    candidates_scanned: int = Field(ge=0, le=MAXIMUM_EXECUTION_RECOVERY_BATCH_LIMIT)
    already_settled: int = Field(ge=0)
    not_due: int = Field(ge=0)
    running_past_bound: int = Field(ge=0)
    recovered_completion: int = Field(ge=0)
    failure_settled: int = Field(ge=0)
    reconciliation_routed: int = Field(ge=0)
    cancellation_settled: int = Field(ge=0)
    authority_conflicts: int = Field(ge=0)
    dependency_unavailable: int = Field(ge=0)
    settlement_exhausted: int = Field(ge=0)
    terminal_executions_observed: int = Field(ge=0)
    executions_missing: int = Field(ge=0)
    batch_limit: int = Field(ge=1, le=MAXIMUM_EXECUTION_RECOVERY_BATCH_LIMIT)
    batch_limit_reached: bool
    alarm_signal_count: int = Field(ge=0)
    requires_operator_attention: bool

    @model_validator(mode="after")
    def counters_are_coherent(self) -> ExecutionRecoverySweepResult:
        classified = (
            self.already_settled
            + self.not_due
            + self.running_past_bound
            + self.recovered_completion
            + self.failure_settled
            + self.reconciliation_routed
            + self.cancellation_settled
            + self.authority_conflicts
            + self.dependency_unavailable
            + self.settlement_exhausted
        )
        if classified != self.candidates_scanned:
            raise ValueError("Recovery result classifications do not cover the batch")
        if self.terminal_executions_observed + self.executions_missing > self.candidates_scanned:
            raise ValueError("Execution observation counters exceed the batch")
        if self.candidates_scanned > self.batch_limit:
            raise ValueError("Recovery batch exceeds its advertised limit")
        if self.batch_limit_reached != (self.candidates_scanned == self.batch_limit):
            raise ValueError("Recovery batch-limit state is inconsistent")
        alarm_signals = (
            self.running_past_bound
            + self.authority_conflicts
            + self.dependency_unavailable
            + self.settlement_exhausted
        )
        if self.alarm_signal_count != alarm_signals:
            raise ValueError("Recovery alarm counter is inconsistent")
        if self.requires_operator_attention != (alarm_signals > 0):
            raise ValueError("Recovery attention state is inconsistent")
        return self


class StrandedExecutionInventory(Protocol):
    """Query only the DISPATCHED-work recovery index at or before one cutoff."""

    def list_stranded_execution_candidates(
        self,
        *,
        dispatched_before: datetime,
        limit: int,
    ) -> tuple[StrandedExecutionCandidate, ...]: ...


class StrongExecutionAuthorityReader(Protocol):
    """Read the exact Job and Work together with DynamoDB TransactGetItems."""

    def read_execution_authority_strong(
        self,
        *,
        job_id: str,
        work_request_id: str,
    ) -> ExecutionAuthoritySnapshot: ...


class ExactExecutionObserver(Protocol):
    """Describe one exact ARN; return ``None`` only for ExecutionDoesNotExist."""

    def describe_exact_execution(self, *, execution_arn: str) -> ExecutionObservation | None: ...


class RecoverySettlementControl(Protocol):
    """The existing application settlement commands; no worker/provider method is exposed."""

    def record_worker_failure(self, command: RecordWorkerFailureCommand) -> CommandResponse: ...

    def settle_cancellation(self, command: SettleCancellationCommand) -> CommandResponse: ...


class RecoveryPreparationSettlement(Protocol):
    def settle_unavailable(
        self,
        *,
        job_id: str,
        work_request_id: str,
    ) -> PreparationSettlementResult: ...


class _ItemResult(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True)

    disposition: RecoveryDisposition
    terminal_observed: bool = False
    execution_missing: bool = False


class _AbsentAuthority:
    """Private sentinel for a transactionally proven missing Job/Work pair."""


_ABSENT_AUTHORITY = _AbsentAuthority()


class StuckExecutionRecoverySweeper:
    """Settle old DISPATCHED work without ever replaying an external mutation."""

    def __init__(
        self,
        *,
        inventory: StrandedExecutionInventory,
        authority: StrongExecutionAuthorityReader,
        executions: ExactExecutionObserver,
        control: RecoverySettlementControl,
        preparation_settlement: RecoveryPreparationSettlement,
        state_machine_arns: Mapping[WorkType, str],
        clock: Callable[[], datetime] | None = None,
        stale_after: timedelta = DEFAULT_EXECUTION_STALE_AFTER,
        batch_limit: int = DEFAULT_EXECUTION_RECOVERY_BATCH_LIMIT,
        maximum_cas_rechecks: int = DEFAULT_EXECUTION_RECOVERY_CAS_RECHECKS,
    ) -> None:
        if not MINIMUM_EXECUTION_STALE_AFTER <= stale_after <= MAXIMUM_EXECUTION_STALE_AFTER:
            raise ValueError("Execution stale age is outside its safety bound")
        if not 1 <= batch_limit <= MAXIMUM_EXECUTION_RECOVERY_BATCH_LIMIT:
            raise ValueError("Execution recovery batch limit is outside its safety bound")
        if not 1 <= maximum_cas_rechecks <= MAXIMUM_EXECUTION_RECOVERY_CAS_RECHECKS:
            raise ValueError("Execution recovery CAS rechecks are outside their safety bound")
        self._inventory = inventory
        self._authority = authority
        self._executions = executions
        self._control = control
        self._preparation_settlement = preparation_settlement
        self._state_machine_arns = _validate_state_machine_allowlist(state_machine_arns)
        self._clock = clock or (lambda: datetime.now(UTC))
        self._stale_after = stale_after
        self._batch_limit = batch_limit
        self._maximum_cas_rechecks = maximum_cas_rechecks

    def sweep(self) -> ExecutionRecoverySweepResult:
        """Classify and, where safe, settle one deterministic bounded candidate batch."""

        now = self._now()
        cutoff = now - self._stale_after
        candidates = self._list_candidates(cutoff=cutoff)
        outcomes: list[_ItemResult] = []
        for candidate in candidates:
            outcomes.append(self._recover_one(candidate, now=now, cutoff=cutoff))
        return _summarize(outcomes, batch_limit=self._batch_limit)

    def _list_candidates(self, *, cutoff: datetime) -> tuple[StrandedExecutionCandidate, ...]:
        try:
            raw_candidates = self._inventory.list_stranded_execution_candidates(
                dispatched_before=cutoff,
                limit=self._batch_limit,
            )
        except Exception:
            raise ExecutionRecoveryDependencyUnavailableError(
                "Execution recovery inventory is unavailable"
            ) from None
        if not isinstance(raw_candidates, tuple) or len(raw_candidates) > self._batch_limit:
            raise ExecutionRecoveryBoundaryInvalidError(
                "Execution recovery inventory violated its bounded contract"
            )
        candidates: list[StrandedExecutionCandidate] = []
        identities: set[tuple[str, str]] = set()
        for raw in raw_candidates:
            if not isinstance(raw, StrandedExecutionCandidate):
                raise ExecutionRecoveryBoundaryInvalidError(
                    "Execution recovery inventory returned an invalid candidate"
                )
            identity = (raw.job_id, raw.work_request_id)
            if identity in identities:
                raise ExecutionRecoveryBoundaryInvalidError(
                    "Execution recovery inventory returned a duplicate candidate"
                )
            identities.add(identity)
            candidates.append(raw)
        return tuple(sorted(candidates, key=lambda item: (item.job_id, item.work_request_id)))

    def _recover_one(
        self,
        candidate: StrandedExecutionCandidate,
        *,
        now: datetime,
        cutoff: datetime,
    ) -> _ItemResult:
        snapshot = self._read_authority(candidate)
        if snapshot is None:
            return _ItemResult(disposition=RecoveryDisposition.DEPENDENCY_UNAVAILABLE)
        if snapshot is _ABSENT_AUTHORITY:
            return _ItemResult(disposition=RecoveryDisposition.AUTHORITY_CONFLICT)
        job, work = snapshot
        initial = _classify_durable_authority(
            candidate=candidate,
            job=job,
            work=work,
            cutoff=cutoff,
            state_machine_arns=self._state_machine_arns,
        )
        if initial is not None:
            return _ItemResult(disposition=initial)

        expected_arn = work.execution_arn
        assert expected_arn is not None
        try:
            observation = self._executions.describe_exact_execution(execution_arn=expected_arn)
        except Exception:
            return _ItemResult(disposition=RecoveryDisposition.DEPENDENCY_UNAVAILABLE)
        if observation is not None and not isinstance(observation, ExecutionObservation):
            return _ItemResult(disposition=RecoveryDisposition.AUTHORITY_CONFLICT)
        if observation is None:
            return self._settle(
                candidate,
                expected_work=work,
                execution_missing=True,
            )
        if not _observation_matches(
            observation,
            job=job,
            work=work,
            state_machine_arns=self._state_machine_arns,
            now=now,
        ):
            return _ItemResult(disposition=RecoveryDisposition.AUTHORITY_CONFLICT)
        if observation.status in {ExecutionStatus.RUNNING, ExecutionStatus.PENDING_REDRIVE}:
            return _ItemResult(disposition=RecoveryDisposition.RUNNING_PAST_BOUND)
        return self._settle(
            candidate,
            expected_work=work,
            terminal_observed=True,
        )

    def _read_authority(
        self,
        candidate: StrandedExecutionCandidate,
    ) -> tuple[ControlJobRecord, WorkRequest] | _AbsentAuthority | None:
        try:
            snapshot = self._authority.read_execution_authority_strong(
                job_id=candidate.job_id,
                work_request_id=candidate.work_request_id,
            )
        except Exception:
            return None
        if not isinstance(snapshot, ExecutionAuthoritySnapshot):
            return None
        if snapshot.job is None or snapshot.work is None:
            # Durable work rows are retained. Missing authority is corruption, not absence that
            # may be converted into a replay or mutation.
            return _ABSENT_AUTHORITY
        return snapshot.job, snapshot.work

    def _settle(
        self,
        candidate: StrandedExecutionCandidate,
        *,
        expected_work: WorkRequest,
        terminal_observed: bool = False,
        execution_missing: bool = False,
    ) -> _ItemResult:
        for _ in range(self._maximum_cas_rechecks + 1):
            rebound = self._read_authority(candidate)
            if rebound is None:
                return _ItemResult(
                    disposition=RecoveryDisposition.DEPENDENCY_UNAVAILABLE,
                    terminal_observed=terminal_observed,
                    execution_missing=execution_missing,
                )
            if rebound is _ABSENT_AUTHORITY:
                return _ItemResult(
                    disposition=RecoveryDisposition.AUTHORITY_CONFLICT,
                    terminal_observed=terminal_observed,
                    execution_missing=execution_missing,
                )
            job, work = rebound
            if work.status in {WorkRequestStatus.COMPLETED, WorkRequestStatus.CANCELLED}:
                return _ItemResult(
                    disposition=RecoveryDisposition.ALREADY_SETTLED,
                    terminal_observed=terminal_observed,
                    execution_missing=execution_missing,
                )
            if not _active_settlement_authority(candidate=candidate, job=job, work=work):
                return _ItemResult(
                    disposition=RecoveryDisposition.AUTHORITY_CONFLICT,
                    terminal_observed=terminal_observed,
                    execution_missing=execution_missing,
                )
            if work != expected_work:
                # No legitimate transition rewrites an already-DISPATCHED row in place. A
                # changed row must be observed again rather than settled against stale external
                # evidence.
                return _ItemResult(
                    disposition=RecoveryDisposition.AUTHORITY_CONFLICT,
                    terminal_observed=terminal_observed,
                    execution_missing=execution_missing,
                )
            try:
                response = self._settle_from_authority(job=job, work=work)
            except (ConcurrentControlModificationError, WorkNotActiveError):
                continue
            except PreparationSettlementError:
                continue
            except InvalidControlStateError:
                return _ItemResult(
                    disposition=RecoveryDisposition.AUTHORITY_CONFLICT,
                    terminal_observed=terminal_observed,
                    execution_missing=execution_missing,
                )
            except Exception:
                return _ItemResult(
                    disposition=RecoveryDisposition.DEPENDENCY_UNAVAILABLE,
                    terminal_observed=terminal_observed,
                    execution_missing=execution_missing,
                )
            disposition = _response_disposition(response, job=job, work=work)
            return _ItemResult(
                disposition=disposition,
                terminal_observed=terminal_observed,
                execution_missing=execution_missing,
            )
        return _ItemResult(
            disposition=RecoveryDisposition.SETTLEMENT_EXHAUSTED,
            terminal_observed=terminal_observed,
            execution_missing=execution_missing,
        )

    def _settle_from_authority(
        self,
        *,
        job: ControlJobRecord,
        work: WorkRequest,
    ) -> CommandResponse:
        if job.cancellation_requested_at is not None:
            return self._control.settle_cancellation(
                SettleCancellationCommand(
                    job_id=job.job_id,
                    work_request_id=work.work_request_id,
                    expected_record_version=job.record_version,
                )
            )
        if work.work_type is WorkType.PREPARE:
            result = self._preparation_settlement.settle_unavailable(
                job_id=job.job_id,
                work_request_id=work.work_request_id,
            )
            if not isinstance(result, PreparationSettlementResult):
                raise ExecutionRecoveryBoundaryInvalidError(
                    "Preparation settlement returned an invalid response"
                )
            return result.response
        return self._control.record_worker_failure(
            RecordWorkerFailureCommand(
                job_id=job.job_id,
                work_request_id=work.work_request_id,
                expected_record_version=job.record_version,
                code=_failure_code(work.work_type),
            )
        )

    def _now(self) -> datetime:
        now = self._clock()
        if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
            raise ExecutionRecoveryBoundaryInvalidError(
                "Execution recovery clock must return an aware datetime"
            )
        return now


class ExecutionRecoveryHandler:
    """Exact scheduled Lambda boundary returning only sanitized operational counters."""

    __slots__ = ("_sweeper",)

    def __init__(self, *, sweeper: StuckExecutionRecoverySweeper) -> None:
        self._sweeper = sweeper

    def __call__(
        self,
        event: Mapping[str, Any],
        _context: object | None = None,
    ) -> dict[str, Any]:
        if (
            not isinstance(event, Mapping)
            or set(event) != {"source"}
            or event.get("source") != EXECUTION_RECOVERY_SWEEP_SOURCE
        ):
            raise ExecutionRecoveryInvocationError("Invalid stuck-execution recovery invocation")
        try:
            return self._sweeper.sweep().model_dump(mode="json")
        except ExecutionRecoveryInvocationError:
            raise
        except Exception:
            raise ExecutionRecoveryExecutionError(
                "Stuck-execution recovery failed safely"
            ) from None


def _validate_state_machine_allowlist(
    configured: Mapping[WorkType, str],
) -> dict[WorkType, str]:
    if not isinstance(configured, Mapping) or len(configured) != len(tuple(WorkType)):
        raise ValueError("Execution recovery requires one ARN for every work type")
    validated: dict[WorkType, str] = {}
    for work_type, arn in configured.items():
        if not isinstance(work_type, WorkType) or not isinstance(arn, str):
            raise ValueError("Execution recovery state-machine allowlist is invalid")
        try:
            execution_arn_for(arn, deterministic_execution_name("recovery-validation"))
        except DispatchConfigurationError:
            raise ValueError("Execution recovery state-machine allowlist is invalid") from None
        validated[work_type] = arn
    if set(validated) != set(WorkType):
        raise ValueError("Execution recovery requires one ARN for every work type")
    return validated


def _classify_durable_authority(
    *,
    candidate: StrandedExecutionCandidate,
    job: ControlJobRecord,
    work: WorkRequest,
    cutoff: datetime,
    state_machine_arns: Mapping[WorkType, str],
) -> RecoveryDisposition | None:
    if not _identity_matches(candidate=candidate, job=job, work=work):
        return RecoveryDisposition.AUTHORITY_CONFLICT
    if work.status in {WorkRequestStatus.COMPLETED, WorkRequestStatus.CANCELLED}:
        return RecoveryDisposition.ALREADY_SETTLED
    if work.status is not WorkRequestStatus.DISPATCHED:
        return RecoveryDisposition.AUTHORITY_CONFLICT
    if work.updated_at.tzinfo is None or work.updated_at.utcoffset() is None:
        return RecoveryDisposition.AUTHORITY_CONFLICT
    if work.updated_at > cutoff:
        return RecoveryDisposition.NOT_DUE
    if not _active_settlement_authority(candidate=candidate, job=job, work=work):
        return RecoveryDisposition.AUTHORITY_CONFLICT
    state_machine_arn = state_machine_arns[work.work_type]
    expected_name = deterministic_execution_name(work.work_request_id)
    try:
        expected_execution_arn = execution_arn_for(state_machine_arn, expected_name)
    except DispatchConfigurationError:
        return RecoveryDisposition.AUTHORITY_CONFLICT
    if (
        work.execution_name != expected_name
        or work.input_fingerprint
        != work_input_fingerprint(
            work_type=work.work_type,
            job_id=work.job_id,
            work_request_id=work.work_request_id,
        )
        or work.execution_arn != expected_execution_arn
    ):
        return RecoveryDisposition.AUTHORITY_CONFLICT
    return None


def _identity_matches(
    *,
    candidate: StrandedExecutionCandidate,
    job: ControlJobRecord,
    work: WorkRequest,
) -> bool:
    return (
        job.job_id == candidate.job_id
        and work.job_id == candidate.job_id
        and work.work_request_id == candidate.work_request_id
        and work.owner_id == job.owner_id
    )


def _active_settlement_authority(
    *,
    candidate: StrandedExecutionCandidate,
    job: ControlJobRecord,
    work: WorkRequest,
) -> bool:
    if not _identity_matches(candidate=candidate, job=job, work=work):
        return False
    if job.active_work_request_id != work.work_request_id:
        return False
    if work.status is not WorkRequestStatus.DISPATCHED:
        return False
    if job.state is ControlJobState.CANCEL_REQUESTED:
        return job.cancellation_requested_at is not None
    return CONTROL_NEW_WORK_BY_STATE.get(job.state) is work.work_type


def _observation_matches(
    observation: ExecutionObservation,
    *,
    job: ControlJobRecord,
    work: WorkRequest,
    state_machine_arns: Mapping[WorkType, str],
    now: datetime,
) -> bool:
    expected_input = json.dumps(
        {"job_id": job.job_id, "work_request_id": work.work_request_id},
        sort_keys=True,
        separators=(",", ":"),
    )
    if observation.start_date > now + _MAX_CLOCK_SKEW:
        return False
    if observation.stop_date is not None and observation.stop_date > now + _MAX_CLOCK_SKEW:
        return False
    return (
        observation.execution_arn == work.execution_arn
        and observation.state_machine_arn == state_machine_arns[work.work_type]
        and observation.name == work.execution_name
        and observation.input == expected_input
    )


def _failure_code(work_type: WorkType) -> WorkerFailureCode:
    if work_type is WorkType.REFRESH_ECONOMICS:
        return WorkerFailureCode.ECONOMICS_UNAVAILABLE
    if work_type in {WorkType.SYNCHRONIZE_PRODUCT, WorkType.RECONCILE_PRODUCT}:
        return WorkerFailureCode.PRODUCTION_UNAVAILABLE
    raise InvalidControlStateError("Unsupported execution recovery work type")


def _response_disposition(
    response: CommandResponse,
    *,
    job: ControlJobRecord,
    work: WorkRequest,
) -> RecoveryDisposition:
    if not isinstance(response, CommandResponse) or response.job_id != job.job_id:
        return RecoveryDisposition.AUTHORITY_CONFLICT
    if response.state is ControlJobState.RECONCILIATION_REQUIRED:
        return RecoveryDisposition.RECONCILIATION_ROUTED
    if response.state is ControlJobState.CANCELLED:
        return RecoveryDisposition.CANCELLATION_SETTLED
    if response.state in {
        ControlJobState.FAILED_RETRYABLE,
        ControlJobState.FAILED_TERMINAL,
    }:
        return RecoveryDisposition.FAILURE_SETTLED
    if work.work_type is WorkType.PREPARE and response.state in {
        ControlJobState.PRODUCT_DRAFT_SYNCING,
        ControlJobState.NEEDS_REVISION,
    }:
        return RecoveryDisposition.RECOVERED_COMPLETION
    return RecoveryDisposition.AUTHORITY_CONFLICT


def _summarize(
    outcomes: Sequence[_ItemResult],
    *,
    batch_limit: int,
) -> ExecutionRecoverySweepResult:
    counts = {disposition: 0 for disposition in RecoveryDisposition}
    terminal = 0
    missing = 0
    for outcome in outcomes:
        counts[outcome.disposition] += 1
        terminal += int(outcome.terminal_observed)
        missing += int(outcome.execution_missing)
    alarm_signals = (
        counts[RecoveryDisposition.RUNNING_PAST_BOUND]
        + counts[RecoveryDisposition.AUTHORITY_CONFLICT]
        + counts[RecoveryDisposition.DEPENDENCY_UNAVAILABLE]
        + counts[RecoveryDisposition.SETTLEMENT_EXHAUSTED]
    )
    return ExecutionRecoverySweepResult(
        candidates_scanned=len(outcomes),
        already_settled=counts[RecoveryDisposition.ALREADY_SETTLED],
        not_due=counts[RecoveryDisposition.NOT_DUE],
        running_past_bound=counts[RecoveryDisposition.RUNNING_PAST_BOUND],
        recovered_completion=counts[RecoveryDisposition.RECOVERED_COMPLETION],
        failure_settled=counts[RecoveryDisposition.FAILURE_SETTLED],
        reconciliation_routed=counts[RecoveryDisposition.RECONCILIATION_ROUTED],
        cancellation_settled=counts[RecoveryDisposition.CANCELLATION_SETTLED],
        authority_conflicts=counts[RecoveryDisposition.AUTHORITY_CONFLICT],
        dependency_unavailable=counts[RecoveryDisposition.DEPENDENCY_UNAVAILABLE],
        settlement_exhausted=counts[RecoveryDisposition.SETTLEMENT_EXHAUSTED],
        terminal_executions_observed=terminal,
        executions_missing=missing,
        batch_limit=batch_limit,
        batch_limit_reached=len(outcomes) == batch_limit,
        alarm_signal_count=alarm_signals,
        requires_operator_attention=alarm_signals > 0,
    )


__all__ = [
    "DEFAULT_EXECUTION_RECOVERY_BATCH_LIMIT",
    "DEFAULT_EXECUTION_RECOVERY_CAS_RECHECKS",
    "DEFAULT_EXECUTION_STALE_AFTER",
    "EXECUTION_RECOVERY_SWEEP_SOURCE",
    "EXECUTION_RECOVERY_CONTRACT_VERSION",
    "ExactExecutionObserver",
    "ExecutionAuthoritySnapshot",
    "ExecutionObservation",
    "ExecutionRecoveryBoundaryInvalidError",
    "ExecutionRecoveryDependencyUnavailableError",
    "ExecutionRecoveryError",
    "ExecutionRecoveryExecutionError",
    "ExecutionRecoveryHandler",
    "ExecutionRecoveryInvocationError",
    "ExecutionRecoverySweepResult",
    "ExecutionStatus",
    "RecoveryDisposition",
    "RecoveryPreparationSettlement",
    "RecoverySettlementControl",
    "StrandedExecutionCandidate",
    "StrandedExecutionInventory",
    "StrongExecutionAuthorityReader",
    "StuckExecutionRecoverySweeper",
    "TERMINAL_EXECUTION_STATUSES",
]
