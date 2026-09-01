"""Provider-free tests for same-ARN Phase 7 publication workflow recovery."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest

from mr_lister.publication.contract import PublicationPermitState, PublicationState
from mr_lister.publication.execution_models import PublicationExecutionWorkStatus
from mr_lister.publication.orchestration import (
    publication_execution_arn,
    publication_execution_name,
)
from mr_lister.publication.orchestration_recovery import (
    PublicationPreDispatchDeadlineEnvelope,
    PublicationRecoveryBoundaryInvalidError,
    PublicationRecoveryCandidate,
    PublicationRecoveryConflictError,
    PublicationRecoveryDependencyUnavailableError,
    PublicationRecoveryDisposition,
    PublicationRecoveryResult,
    PublicationRecoverySweeper,
    PublicationWorkflowFailureEnvelope,
    PublicationWorkflowRecovery,
)
from tests.test_phase72_publication_execution import Harness as ExecutionHarness

NOW = datetime(2026, 9, 1, 18, 0, tzinfo=UTC)
OWNER_ID = "a" * 64
AGGREGATE_ID = "publication_one"
WORK_ID = "publication_work_one"
EXECUTION_NAME = publication_execution_name(WORK_ID)
MACHINE_ARN = "arn:aws:states:us-west-2:123456789012:stateMachine:mr-lister-phase7-dev-publication"
EXECUTION_ARN = publication_execution_arn(MACHINE_ARN, EXECUTION_NAME)
FINGERPRINT = "b" * 64


class ExplosiveDependency:
    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"dependency accessed during construction: {name}")


class RecordingStepFunctions:
    def __init__(
        self,
        observation: dict[str, Any],
        *,
        redrive_response: dict[str, Any] | None = None,
        describe_error: Exception | None = None,
        redrive_error: Exception | None = None,
    ) -> None:
        self.observation = observation
        self.redrive_response = (
            {"redriveDate": NOW} if redrive_response is None else redrive_response
        )
        self.describe_error = describe_error
        self.redrive_error = redrive_error
        self.describe_calls: list[dict[str, Any]] = []
        self.redrive_calls: list[dict[str, Any]] = []

    def describe_execution(self, **request: Any) -> dict[str, Any]:
        self.describe_calls.append(request)
        if self.describe_error is not None:
            raise self.describe_error
        return self.observation

    def redrive_execution(self, **request: Any) -> dict[str, Any]:
        self.redrive_calls.append(request)
        if self.redrive_error is not None:
            raise self.redrive_error
        return self.redrive_response


class RecordingExecution:
    def __init__(self) -> None:
        self.recover_commands: list[object] = []
        self.deadline_commands: list[object] = []

    def recover_consumed_claim(self, command: object) -> object:
        self.recover_commands.append(command)
        return object()

    def settle_deadline(self, command: object) -> object:
        self.deadline_commands.append(command)
        return object()


def _authority(
    *,
    state: PublicationState = PublicationState.PUBLICATION_REQUESTED,
    permit: PublicationPermitState = PublicationPermitState.AVAILABLE,
    deadline: datetime = NOW + timedelta(minutes=30),
    mutation: bool = False,
    work_status: PublicationExecutionWorkStatus = PublicationExecutionWorkStatus.DISPATCHED,
    updated_at: datetime = NOW - timedelta(minutes=3),
) -> SimpleNamespace:
    return SimpleNamespace(
        snapshot=SimpleNamespace(owner_id=OWNER_ID, verification_deadline=deadline),
        aggregate=SimpleNamespace(
            aggregate_id=AGGREGATE_ID,
            owner_id=OWNER_ID,
            state=state,
            record_version=2,
            fingerprint=FINGERPRINT,
            provider_evidence_record_version=0,
        ),
        attempt=SimpleNamespace(record_version=3),
        permit=SimpleNamespace(status=permit, record_version=1 if mutation else 0),
        work=SimpleNamespace(
            owner_id=OWNER_ID,
            aggregate_id=AGGREGATE_ID,
            work_request_id=WORK_ID,
            execution_name=EXECUTION_NAME,
            verification_deadline=deadline,
            status=work_status,
            updated_at=updated_at,
            record_version=1,
        ),
        mutation_claim=(
            SimpleNamespace(mutation_claim_id="publication_mutation_one", fingerprint=FINGERPRINT)
            if mutation
            else None
        ),
        post_observation=None,
    )


def _observation(
    *,
    status: str = "FAILED",
    error: str = "Lambda.Unknown",
    redrive_count: int = 0,
    redrive_status: str = "REDRIVABLE",
) -> dict[str, Any]:
    return {
        "executionArn": EXECUTION_ARN,
        "stateMachineArn": MACHINE_ARN,
        "name": EXECUTION_NAME,
        "input": json.dumps(
            {"aggregate_id": AGGREGATE_ID, "owner_id": OWNER_ID},
            sort_keys=True,
            separators=(",", ":"),
        ),
        "status": status,
        "error": error,
        "redriveCount": redrive_count,
        "redriveStatus": redrive_status,
    }


def _envelope() -> PublicationWorkflowFailureEnvelope:
    return PublicationWorkflowFailureEnvelope(
        execution_arn=EXECUTION_ARN,
        machine_arn=MACHINE_ARN,
        status="FAILED",
    )


def _deadline_envelope() -> PublicationPreDispatchDeadlineEnvelope:
    return PublicationPreDispatchDeadlineEnvelope(
        owner_id=OWNER_ID,
        aggregate_id=AGGREGATE_ID,
        work_request_id=WORK_ID,
        verification_deadline=NOW,
    )


def _recovery_candidate(
    *,
    aggregate_id: str = AGGREGATE_ID,
    work_request_id: str = WORK_ID,
    indexed_at: datetime = NOW - timedelta(minutes=3),
) -> PublicationRecoveryCandidate:
    return PublicationRecoveryCandidate(
        aggregate_id=aggregate_id,
        work_request_id=work_request_id,
        indexed_at_epoch_second=int(indexed_at.timestamp()),
    )


class RecordingRecoveryInventory:
    def __init__(self, *candidates: object) -> None:
        self.candidates = tuple(candidates)
        self.calls: list[dict[str, object]] = []

    def list_recovery_candidates(
        self,
        *,
        updated_before: datetime,
        limit: int,
    ) -> tuple[Any, ...]:
        self.calls.append({"updated_before": updated_before, "limit": limit})
        return self.candidates


class RecordingScheduledRecovery:
    def __init__(self, *outcomes: object) -> None:
        self.outcomes = list(outcomes)
        self.calls: list[PublicationRecoveryCandidate] = []

    def recover_scheduled(
        self,
        candidate: PublicationRecoveryCandidate,
    ) -> PublicationRecoveryResult:
        self.calls.append(candidate)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        assert isinstance(outcome, PublicationRecoveryResult)
        return outcome


class RecoveryHarness(PublicationWorkflowRecovery):
    def __init__(
        self,
        *authorities: SimpleNamespace,
        step_functions: RecordingStepFunctions,
        execution: RecordingExecution | None = None,
        clock: Any = lambda: NOW,
    ) -> None:
        self.authorities = list(authorities)
        self.loaded_inputs: list[tuple[str, str]] = []
        self.loaded_aggregates: list[str] = []
        self.recording_execution = execution or RecordingExecution()
        super().__init__(
            store=ExplosiveDependency(),
            execution=self.recording_execution,
            step_functions=step_functions,
            state_machine_arn=MACHINE_ARN,
            clock=clock,
        )

    def _load_authority(self, workflow_input: Any) -> Any:
        self.loaded_inputs.append((workflow_input.owner_id, workflow_input.aggregate_id))
        if not self.authorities:
            raise AssertionError("unexpected authority reload")
        return self.authorities.pop(0)

    def _load_authority_by_aggregate(self, aggregate_id: str) -> Any:
        self.loaded_aggregates.append(aggregate_id)
        if not self.authorities:
            raise AssertionError("unexpected authority reload")
        return self.authorities.pop(0)


def test_construction_is_provider_free_and_does_not_touch_dependencies() -> None:
    result = PublicationWorkflowRecovery(
        store=ExplosiveDependency(),
        execution=ExplosiveDependency(),
        step_functions=ExplosiveDependency(),
        state_machine_arn=MACHINE_ARN,
    )
    assert isinstance(result, PublicationWorkflowRecovery)


def test_failed_worker_task_redrives_only_the_same_exact_execution() -> None:
    step_functions = RecordingStepFunctions(_observation())
    recovery = RecoveryHarness(_authority(), step_functions=step_functions)

    result = recovery.recover(_envelope())

    assert result.disposition is PublicationRecoveryDisposition.REDRIVEN
    assert result.redrive_count == 1
    assert step_functions.describe_calls == [{"executionArn": EXECUTION_ARN}]
    assert step_functions.redrive_calls == [
        {
            "executionArn": EXECUTION_ARN,
            "clientToken": step_functions.redrive_calls[0]["clientToken"],
        }
    ]
    assert len(step_functions.redrive_calls[0]["clientToken"]) == 64
    assert recovery.loaded_inputs == [(OWNER_ID, AGGREGATE_ID)]


def test_scheduled_candidate_strongly_rebinds_then_reuses_same_arn_recovery() -> None:
    step_functions = RecordingStepFunctions(_observation())
    recovery = RecoveryHarness(_authority(), step_functions=step_functions)

    result = recovery.recover_scheduled(_recovery_candidate())

    assert result.disposition is PublicationRecoveryDisposition.REDRIVEN
    assert recovery.loaded_aggregates == [AGGREGATE_ID]
    assert step_functions.describe_calls == [{"executionArn": EXECUTION_ARN}]
    assert step_functions.redrive_calls[0]["executionArn"] == EXECUTION_ARN


def test_scheduled_gsi_lag_is_a_stale_hint_and_terminal_lag_is_already_settled() -> None:
    step_functions = RecordingStepFunctions(_observation())
    newer = RecoveryHarness(
        _authority(updated_at=NOW - timedelta(minutes=1)),
        step_functions=step_functions,
    )
    terminal = RecoveryHarness(
        _authority(
            state=PublicationState.PUBLICATION_FAILED,
            work_status=PublicationExecutionWorkStatus.FAILED,
            updated_at=NOW,
        ),
        step_functions=step_functions,
    )

    assert (
        newer.recover_scheduled(
            _recovery_candidate(indexed_at=NOW - timedelta(minutes=3))
        ).disposition
        is PublicationRecoveryDisposition.STALE_HINT
    )
    assert (
        terminal.recover_scheduled(
            _recovery_candidate(indexed_at=NOW - timedelta(minutes=3))
        ).disposition
        is PublicationRecoveryDisposition.TERMINAL
    )
    assert step_functions.describe_calls == []
    assert step_functions.redrive_calls == []


def test_scheduled_candidate_reuses_deadline_settlement_without_start_or_provider() -> None:
    initial = _authority(deadline=NOW)
    terminal = _authority(
        state=PublicationState.PUBLICATION_FAILED,
        deadline=NOW,
        work_status=PublicationExecutionWorkStatus.FAILED,
        updated_at=NOW,
    )
    execution = RecordingExecution()
    step_functions = RecordingStepFunctions(_observation())
    recovery = RecoveryHarness(
        initial,
        terminal,
        step_functions=step_functions,
        execution=execution,
    )

    result = recovery.recover_scheduled(_recovery_candidate())

    assert result.disposition is PublicationRecoveryDisposition.DEADLINE_SETTLED
    assert len(execution.deadline_commands) == 1
    assert step_functions.redrive_calls == []


def test_scheduled_cross_bound_identity_fails_closed_before_describe() -> None:
    step_functions = RecordingStepFunctions(_observation())
    recovery = RecoveryHarness(_authority(), step_functions=step_functions)

    with pytest.raises(PublicationRecoveryConflictError):
        recovery.recover_scheduled(_recovery_candidate(work_request_id="foreign_work"))

    assert step_functions.describe_calls == []
    assert step_functions.redrive_calls == []


def test_scheduled_malformed_execution_status_cannot_trigger_deadline_settlement() -> None:
    execution = RecordingExecution()
    recovery = RecoveryHarness(
        _authority(deadline=NOW),
        step_functions=RecordingStepFunctions(_observation(status="UNKNOWN")),
        execution=execution,
    )

    with pytest.raises(PublicationRecoveryBoundaryInvalidError):
        recovery.recover_scheduled(_recovery_candidate())

    assert execution.deadline_commands == []


def test_bounded_sweep_isolates_candidate_failure_and_surfaces_saturation() -> None:
    first = _recovery_candidate(aggregate_id="publication_first", work_request_id="work_first")
    second = _recovery_candidate(aggregate_id="publication_second", work_request_id="work_second")
    inventory = RecordingRecoveryInventory(first, second)
    recovery = RecordingScheduledRecovery(
        PublicationRecoveryConflictError("private conflicting authority"),
        PublicationRecoveryResult(PublicationRecoveryDisposition.RUNNING, 0),
    )
    sweeper = PublicationRecoverySweeper(
        inventory=inventory,
        recovery=recovery,  # type: ignore[arg-type]
        clock=lambda: NOW,
    )

    result = sweeper.sweep(limit=2)

    assert result.candidate_count == 2
    assert result.batch_limit_reached is True
    assert result.retry_required_count == 1
    assert result.running_count == 1
    assert recovery.calls == [first, second]
    assert inventory.calls == [{"updated_before": NOW - timedelta(minutes=2), "limit": 2}]


def test_sweep_keeps_non_redrivable_work_retryable_until_immutable_deadline() -> None:
    candidate = _recovery_candidate()
    inventory = RecordingRecoveryInventory(candidate)
    recovery = RecordingScheduledRecovery(
        PublicationRecoveryResult(PublicationRecoveryDisposition.NON_REDRIVABLE, 3)
    )
    sweeper = PublicationRecoverySweeper(
        inventory=inventory,
        recovery=recovery,  # type: ignore[arg-type]
        clock=lambda: NOW,
    )

    result = sweeper.sweep()

    assert result.candidate_count == 1
    assert result.batch_limit_reached is False
    assert result.non_redrivable_count == 1
    assert result.retry_required_count == 1
    assert recovery.calls == [candidate]


def test_sweep_rejects_malformed_inventory_as_a_whole_without_recovery() -> None:
    inventory = RecordingRecoveryInventory(object())
    recovery = RecordingScheduledRecovery()
    sweeper = PublicationRecoverySweeper(
        inventory=inventory,
        recovery=recovery,  # type: ignore[arg-type]
        clock=lambda: NOW,
    )

    with pytest.raises(PublicationRecoveryBoundaryInvalidError):
        sweeper.sweep()

    assert recovery.calls == []


@pytest.mark.parametrize(
    ("error", "redrive_count", "redrive_status"),
    [
        ("PublicationWorkflowFailed", 0, "REDRIVABLE"),
        ("PublicationPollBudgetExhausted", 0, "REDRIVABLE"),
        ("Lambda.Unknown", 3, "REDRIVABLE"),
        ("Lambda.Unknown", 0, "NOT_REDRIVABLE"),
    ],
)
def test_fail_states_budget_and_aws_eligibility_are_never_blindly_redriven(
    error: str,
    redrive_count: int,
    redrive_status: str,
) -> None:
    step_functions = RecordingStepFunctions(
        _observation(
            error=error,
            redrive_count=redrive_count,
            redrive_status=redrive_status,
        )
    )

    result = RecoveryHarness(_authority(), step_functions=step_functions).recover(_envelope())

    assert result.disposition is PublicationRecoveryDisposition.NON_REDRIVABLE
    assert step_functions.redrive_calls == []


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        ("RUNNING", PublicationRecoveryDisposition.RUNNING),
        ("PENDING_REDRIVE", PublicationRecoveryDisposition.PENDING_REDRIVE),
    ],
)
def test_stale_failure_event_uses_current_exact_execution_status(
    status: str,
    expected: PublicationRecoveryDisposition,
) -> None:
    step_functions = RecordingStepFunctions(_observation(status=status))
    result = RecoveryHarness(_authority(), step_functions=step_functions).recover(_envelope())
    assert result.disposition is expected
    assert step_functions.redrive_calls == []


def test_durable_terminal_authority_acknowledges_a_stale_failure_event() -> None:
    step_functions = RecordingStepFunctions(_observation())
    result = RecoveryHarness(
        _authority(state=PublicationState.PUBLISHED),
        step_functions=step_functions,
    ).recover(_envelope())
    assert result.disposition is PublicationRecoveryDisposition.TERMINAL
    assert step_functions.redrive_calls == []


def test_elapsed_available_authority_settles_without_redrive_or_provider() -> None:
    initial = _authority(deadline=NOW)
    terminal = _authority(state=PublicationState.PUBLICATION_FAILED, deadline=NOW)
    execution = RecordingExecution()
    step_functions = RecordingStepFunctions(_observation())
    recovery = RecoveryHarness(
        initial,
        terminal,
        step_functions=step_functions,
        execution=execution,
    )

    result = recovery.recover(_envelope())

    assert result.disposition is PublicationRecoveryDisposition.DEADLINE_SETTLED
    assert len(execution.deadline_commands) == 1
    assert execution.recover_commands == []
    assert step_functions.redrive_calls == []


def test_pre_dispatch_expiry_settles_without_describing_or_starting_workflow() -> None:
    initial = _authority(deadline=NOW)
    terminal = _authority(state=PublicationState.PUBLICATION_FAILED, deadline=NOW)
    execution = RecordingExecution()
    step_functions = RecordingStepFunctions(_observation())
    recovery = RecoveryHarness(
        initial,
        terminal,
        step_functions=step_functions,
        execution=execution,
    )

    result = recovery.settle_pre_dispatch_deadline(_deadline_envelope())

    assert result == PublicationRecoveryResult(
        PublicationRecoveryDisposition.DEADLINE_SETTLED,
        0,
    )
    assert len(execution.deadline_commands) == 1
    assert execution.recover_commands == []
    assert step_functions.describe_calls == []
    assert step_functions.redrive_calls == []


def test_pre_dispatch_expiry_replay_acknowledges_terminal_without_mutation() -> None:
    execution = RecordingExecution()
    step_functions = RecordingStepFunctions(_observation())
    recovery = RecoveryHarness(
        _authority(state=PublicationState.PUBLICATION_FAILED, deadline=NOW),
        step_functions=step_functions,
        execution=execution,
    )

    result = recovery.settle_pre_dispatch_deadline(_deadline_envelope())

    assert result == PublicationRecoveryResult(PublicationRecoveryDisposition.TERMINAL, 0)
    assert execution.deadline_commands == []
    assert step_functions.describe_calls == []
    assert step_functions.redrive_calls == []


def test_pre_dispatch_expiry_converges_through_real_execution_service_and_replay() -> None:
    # This path uses the real request service, including its persisted deterministic execution name.
    harness = ExecutionHarness(short_pricing_window=True)
    authority = harness.authority
    harness.clock.now = authority.snapshot.verification_deadline
    recovery = PublicationWorkflowRecovery(
        store=harness.store,
        execution=harness.service,
        step_functions=ExplosiveDependency(),
        state_machine_arn=MACHINE_ARN,
        clock=harness.clock,
    )
    envelope = PublicationPreDispatchDeadlineEnvelope(
        owner_id=authority.snapshot.owner_id,
        aggregate_id=authority.aggregate.aggregate_id,
        work_request_id=authority.work.work_request_id,
        verification_deadline=authority.snapshot.verification_deadline,
    )

    first = recovery.settle_pre_dispatch_deadline(envelope)
    replay = recovery.settle_pre_dispatch_deadline(envelope)
    terminal = harness.authority

    assert first.disposition is PublicationRecoveryDisposition.DEADLINE_SETTLED
    assert replay.disposition is PublicationRecoveryDisposition.TERMINAL
    assert terminal.aggregate.state is PublicationState.PUBLICATION_FAILED
    assert terminal.permit.status is PublicationPermitState.RETIRED
    assert terminal.work.status is PublicationExecutionWorkStatus.FAILED
    assert terminal.work.attempt_count == 0
    assert terminal.work.dispatched_at is None
    assert terminal.work.next_dispatch_at is None


def test_pre_dispatch_expiry_rejects_early_or_cross_bound_work() -> None:
    step_functions = RecordingStepFunctions(_observation())
    with pytest.raises(PublicationRecoveryBoundaryInvalidError):
        RecoveryHarness(
            _authority(deadline=NOW + timedelta(seconds=1)),
            step_functions=step_functions,
        ).settle_pre_dispatch_deadline(
            _deadline_envelope().model_copy(
                update={"verification_deadline": NOW + timedelta(seconds=1)}
            )
        )

    with pytest.raises(PublicationRecoveryConflictError):
        RecoveryHarness(
            _authority(deadline=NOW),
            step_functions=step_functions,
        ).settle_pre_dispatch_deadline(
            _deadline_envelope().model_copy(update={"work_request_id": "foreign_work"})
        )
    assert step_functions.describe_calls == []
    assert step_functions.redrive_calls == []


def test_elapsed_consumed_claim_recovers_then_settles_with_stable_commands() -> None:
    initial = _authority(
        deadline=NOW,
        permit=PublicationPermitState.CONSUMED,
        mutation=True,
    )
    reconciling = _authority(
        state=PublicationState.PUBLICATION_RECONCILING,
        deadline=NOW,
        permit=PublicationPermitState.CONSUMED,
        mutation=True,
    )
    terminal = _authority(
        state=PublicationState.PUBLICATION_OUTCOME_UNKNOWN,
        deadline=NOW,
        permit=PublicationPermitState.CONSUMED,
        mutation=True,
    )
    execution = RecordingExecution()
    recovery = RecoveryHarness(
        initial,
        reconciling,
        terminal,
        step_functions=RecordingStepFunctions(_observation()),
        execution=execution,
    )

    result = recovery.recover(_envelope())

    assert result.disposition is PublicationRecoveryDisposition.DEADLINE_SETTLED
    assert len(execution.recover_commands) == 1
    assert len(execution.deadline_commands) == 1
    recover = execution.recover_commands[0]
    deadline = execution.deadline_commands[0]
    assert recover.operation_id.startswith("phase715b_recover_claim_")
    assert deadline.operation_id.startswith("phase715b_deadline_")


def test_cross_bound_machine_input_and_execution_name_fail_closed() -> None:
    foreign_machine = _envelope().model_copy(
        update={"machine_arn": ("arn:aws:states:us-west-2:123456789012:stateMachine:foreign")}
    )
    with pytest.raises(PublicationRecoveryBoundaryInvalidError):
        RecoveryHarness(
            _authority(),
            step_functions=RecordingStepFunctions(_observation()),
        ).recover(foreign_machine)

    observation = _observation()
    observation["input"] = json.dumps({"aggregate_id": "foreign", "owner_id": OWNER_ID})
    with pytest.raises(PublicationRecoveryBoundaryInvalidError):
        RecoveryHarness(
            _authority(),
            step_functions=RecordingStepFunctions(observation),
        ).recover(_envelope())

    foreign = _authority()
    foreign.work.execution_name = "publication_execution_foreign"
    with pytest.raises(PublicationRecoveryConflictError):
        RecoveryHarness(
            foreign,
            step_functions=RecordingStepFunctions(_observation()),
        ).recover(_envelope())


def test_success_without_terminal_authority_and_bad_redrive_response_fail_closed() -> None:
    with pytest.raises(PublicationRecoveryConflictError):
        RecoveryHarness(
            _authority(),
            step_functions=RecordingStepFunctions(_observation(status="SUCCEEDED")),
        ).recover(_envelope())

    with pytest.raises(PublicationRecoveryBoundaryInvalidError):
        RecoveryHarness(
            _authority(),
            step_functions=RecordingStepFunctions(_observation(), redrive_response={}),
        ).recover(_envelope())

    with pytest.raises(PublicationRecoveryBoundaryInvalidError):
        RecoveryHarness(
            _authority(),
            step_functions=RecordingStepFunctions(
                _observation(),
                redrive_response={"redriveDate": datetime(2026, 9, 1, 18, 0)},
            ),
        ).recover(_envelope())


def test_dependency_failures_are_sanitized() -> None:
    recovery = RecoveryHarness(
        _authority(),
        step_functions=RecordingStepFunctions(
            _observation(),
            describe_error=RuntimeError("private provider payload"),
        ),
    )
    with pytest.raises(PublicationRecoveryDependencyUnavailableError) as captured:
        recovery.recover(_envelope())
    assert captured.value.__cause__ is None
    assert "private provider payload" not in str(captured.value)
