"""Provider-free tests for the source-only Phase 7.15 publication dispatcher."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from botocore.exceptions import ClientError, EndpointConnectionError
from pydantic import ValidationError

from mr_lister.publication.execution_models import PublicationExecutionWorkStatus
from mr_lister.publication.orchestration import (
    PublicationDispatchCandidate,
    PublicationDispatchConfigurationError,
    PublicationDispatchDependencyUnavailableError,
    PublicationDispatchDisposition,
    PublicationWorkDispatcher,
    publication_execution_arn,
    publication_execution_name,
)
from mr_lister.publication.service import PublicationRequestService

NOW = datetime(2026, 9, 1, 18, 0, tzinfo=UTC)
OWNER_ID = "a" * 64
MACHINE_ARN = "arn:aws:states:us-west-2:123456789012:stateMachine:mr-lister-phase7-dev-publication"


def _client_error(code: str) -> ClientError:
    return ClientError(
        {"Error": {"Code": code, "Message": "private provider detail"}},
        "StartExecution",
    )


def _candidate(
    suffix: str = "one",
    *,
    deadline: datetime = NOW + timedelta(minutes=30),
) -> PublicationDispatchCandidate:
    work_request_id = f"publication_work_{suffix}"
    return PublicationDispatchCandidate(
        owner_id=OWNER_ID,
        aggregate_id=f"publication_{suffix}",
        work_request_id=work_request_id,
        execution_name=publication_execution_name(work_request_id),
        verification_deadline=deadline,
        status=PublicationExecutionWorkStatus.PENDING,
    )


class RecordingLocator:
    def __init__(
        self,
        *candidates: PublicationDispatchCandidate,
        error: Exception | None = None,
    ) -> None:
        self.candidates = tuple(candidates)
        self.error = error
        self.list_calls: list[dict[str, object]] = []

    def list_due_publication_work(
        self,
        *,
        now: datetime,
        limit: int,
    ) -> tuple[PublicationDispatchCandidate, ...]:
        self.list_calls.append({"now": now, "limit": limit})
        if self.error is not None:
            raise self.error
        return self.candidates


class RecordingStepFunctions:
    def __init__(
        self,
        *,
        start_error: Exception | None = None,
        describe_error: Exception | None = None,
        returned_execution_arn: str | None = None,
        described: dict[str, Any] | None = None,
    ) -> None:
        self.start_error = start_error
        self.describe_error = describe_error
        self.returned_execution_arn = returned_execution_arn
        self.described = described
        self.start_calls: list[dict[str, Any]] = []
        self.describe_calls: list[dict[str, Any]] = []

    def start_execution(self, **request: Any) -> dict[str, Any]:
        self.start_calls.append(request)
        if self.start_error is not None:
            raise self.start_error
        execution_arn = self.returned_execution_arn or publication_execution_arn(
            request["stateMachineArn"],
            request["name"],
        )
        return {"executionArn": execution_arn, "startDate": NOW}

    def describe_execution(self, **request: Any) -> dict[str, Any]:
        self.describe_calls.append(request)
        if self.describe_error is not None:
            raise self.describe_error
        assert self.described is not None
        return self.described


class SelectiveStepFunctions(RecordingStepFunctions):
    def __init__(self, *, rejected_name: str) -> None:
        super().__init__()
        self.rejected_name = rejected_name

    def start_execution(self, **request: Any) -> dict[str, Any]:
        if request["name"] == self.rejected_name:
            self.start_calls.append(request)
            raise _client_error("AccessDeniedException")
        return super().start_execution(**request)


class ExplosiveDependency:
    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"construction accessed dependency attribute {name}")


def _dispatcher(
    locator: Any,
    step_functions: Any,
    *,
    clock: Any = lambda: NOW,
) -> PublicationWorkDispatcher:
    return PublicationWorkDispatcher(
        locator=locator,
        step_functions=step_functions,
        state_machine_arn=MACHINE_ARN,
        clock=clock,
    )


def _exact_description(candidate: PublicationDispatchCandidate) -> dict[str, str]:
    payload = json.dumps(
        {"aggregate_id": candidate.aggregate_id, "owner_id": candidate.owner_id},
        sort_keys=True,
        separators=(",", ":"),
    )
    execution_arn = publication_execution_arn(MACHINE_ARN, candidate.execution_name)
    return {
        "executionArn": execution_arn,
        "stateMachineArn": MACHINE_ARN,
        "name": candidate.execution_name,
        "input": payload,
        "status": "RUNNING",
    }


def test_construction_is_pure_and_uses_one_fixed_machine() -> None:
    dispatcher = _dispatcher(ExplosiveDependency(), ExplosiveDependency())

    assert isinstance(dispatcher, PublicationWorkDispatcher)
    with pytest.raises(PublicationDispatchConfigurationError):
        PublicationWorkDispatcher(
            locator=ExplosiveDependency(),
            step_functions=ExplosiveDependency(),
            state_machine_arn="https://attacker.example/workflow",
        )


def test_execution_name_matches_the_request_service_persisted_identity() -> None:
    work_request_id = "publication_work_exact_identity"

    assert publication_execution_name(work_request_id) == PublicationRequestService._stable_id(
        "publication_execution",
        work_request_id,
    )


def test_dispatch_uses_persisted_name_and_exact_identifier_only_input_without_store_write() -> None:
    candidate = _candidate()
    locator = RecordingLocator(candidate)
    step_functions = RecordingStepFunctions()

    result = _dispatcher(locator, step_functions).dispatch_due()

    expected_arn = publication_execution_arn(MACHINE_ARN, candidate.execution_name)
    assert result[0].disposition is PublicationDispatchDisposition.STARTED
    assert result[0].execution_arn == expected_arn
    assert result[0].owner_id == candidate.owner_id
    assert result[0].aggregate_id == candidate.aggregate_id
    assert result[0].work_request_id == candidate.work_request_id
    assert result[0].verification_deadline == candidate.verification_deadline
    assert locator.list_calls == [{"now": NOW, "limit": 25}]
    assert step_functions.start_calls == [
        {
            "stateMachineArn": MACHINE_ARN,
            "name": candidate.execution_name,
            "input": json.dumps(
                {"aggregate_id": candidate.aggregate_id, "owner_id": OWNER_ID},
                sort_keys=True,
                separators=(",", ":"),
            ),
        }
    ]
    assert step_functions.describe_calls == []


def test_exact_existing_execution_resolves_idempotent_replay() -> None:
    candidate = _candidate()
    step_functions = RecordingStepFunctions(
        start_error=_client_error("ExecutionAlreadyExists"),
        described=_exact_description(candidate),
    )

    result = _dispatcher(RecordingLocator(candidate), step_functions).dispatch_due()[0]

    assert result.disposition is PublicationDispatchDisposition.CONFIRMED_EXISTING
    assert step_functions.describe_calls == [
        {"executionArn": publication_execution_arn(MACHINE_ARN, candidate.execution_name)}
    ]


@pytest.mark.parametrize("status", ["FAILED", "TIMED_OUT", "ABORTED"])
def test_exact_failed_existing_execution_routes_same_arn_recovery(status: str) -> None:
    candidate = _candidate()
    described = _exact_description(candidate)
    described["status"] = status
    step_functions = RecordingStepFunctions(
        start_error=_client_error("ExecutionAlreadyExists"),
        described=described,
    )

    result = _dispatcher(RecordingLocator(candidate), step_functions).dispatch_due()[0]

    assert result.disposition is PublicationDispatchDisposition.RECOVERY_REQUIRED
    assert result.execution_arn == publication_execution_arn(
        MACHINE_ARN,
        candidate.execution_name,
    )
    assert result.recovery_status == status


def test_successful_existing_execution_with_pristine_due_work_is_a_conflict_retry() -> None:
    candidate = _candidate()
    described = _exact_description(candidate)
    described["status"] = "SUCCEEDED"
    step_functions = RecordingStepFunctions(
        start_error=_client_error("ExecutionAlreadyExists"),
        described=described,
    )

    result = _dispatcher(RecordingLocator(candidate), step_functions).dispatch_due()[0]

    assert result.disposition is PublicationDispatchDisposition.RETRY_REQUIRED
    assert result.execution_arn is None
    assert result.recovery_status is None


@pytest.mark.parametrize("status", [None, "UNKNOWN"])
def test_existing_execution_readback_requires_one_closed_status(status: object) -> None:
    candidate = _candidate()
    described: dict[str, Any] = _exact_description(candidate)
    described["status"] = status
    step_functions = RecordingStepFunctions(
        start_error=_client_error("ExecutionAlreadyExists"),
        described=described,
    )

    result = _dispatcher(RecordingLocator(candidate), step_functions).dispatch_due()[0]

    assert result.disposition is PublicationDispatchDisposition.RETRY_REQUIRED
    assert result.execution_arn is None


@pytest.mark.parametrize(
    "start_error",
    [
        _client_error("ThrottlingException"),
        EndpointConnectionError(endpoint_url="https://states.us-west-2.amazonaws.com"),
    ],
)
def test_transient_or_transport_start_ambiguity_requires_exact_readback(
    start_error: Exception,
) -> None:
    candidate = _candidate()
    step_functions = RecordingStepFunctions(
        start_error=start_error,
        described=_exact_description(candidate),
    )

    result = _dispatcher(RecordingLocator(candidate), step_functions).dispatch_due()[0]

    assert result.disposition is PublicationDispatchDisposition.CONFIRMED_EXISTING


def test_conflicting_readback_becomes_sanitized_candidate_retry() -> None:
    candidate = _candidate()
    conflicting = _exact_description(candidate)
    conflicting["input"] = json.dumps({"aggregate_id": "foreign", "owner_id": OWNER_ID})
    step_functions = RecordingStepFunctions(
        start_error=_client_error("ExecutionAlreadyExists"),
        described=conflicting,
    )

    result = _dispatcher(RecordingLocator(candidate), step_functions).dispatch_due()[0]
    assert result.disposition is PublicationDispatchDisposition.RETRY_REQUIRED
    assert result.execution_arn is None


def test_unresolved_readback_becomes_sanitized_candidate_retry() -> None:
    candidate = _candidate()
    step_functions = RecordingStepFunctions(
        start_error=_client_error("ExecutionAlreadyExists"),
        describe_error=_client_error("ExecutionDoesNotExist"),
    )

    result = _dispatcher(RecordingLocator(candidate), step_functions).dispatch_due()[0]
    assert result.disposition is PublicationDispatchDisposition.RETRY_REQUIRED
    assert result.execution_arn is None


def test_nontransient_start_rejection_is_retryable_without_readback() -> None:
    candidate = _candidate()
    step_functions = RecordingStepFunctions(start_error=_client_error("AccessDeniedException"))

    result = _dispatcher(RecordingLocator(candidate), step_functions).dispatch_due()[0]
    assert result.disposition is PublicationDispatchDisposition.RETRY_REQUIRED
    assert result.execution_arn is None
    assert step_functions.describe_calls == []


def test_unexpected_start_response_arn_becomes_sanitized_candidate_retry() -> None:
    candidate = _candidate()
    step_functions = RecordingStepFunctions(
        returned_execution_arn=("arn:aws:states:us-west-2:123456789012:execution:attacker:wrong")
    )

    result = _dispatcher(RecordingLocator(candidate), step_functions).dispatch_due()[0]
    assert result.disposition is PublicationDispatchDisposition.RETRY_REQUIRED
    assert result.execution_arn is None


def test_one_rejected_candidate_does_not_starve_later_candidates_in_the_same_page() -> None:
    rejected = _candidate("rejected")
    later = _candidate("later")
    step_functions = SelectiveStepFunctions(rejected_name=rejected.execution_name)

    results = _dispatcher(RecordingLocator(rejected, later), step_functions).dispatch_due()

    assert [result.disposition for result in results] == [
        PublicationDispatchDisposition.RETRY_REQUIRED,
        PublicationDispatchDisposition.STARTED,
    ]
    assert [call["name"] for call in step_functions.start_calls] == [
        rejected.execution_name,
        later.execution_name,
    ]


@pytest.mark.parametrize("offset", [timedelta(), timedelta(seconds=1)])
def test_deadline_or_later_skips_step_functions(offset: timedelta) -> None:
    candidate = _candidate(deadline=NOW)
    step_functions = RecordingStepFunctions()

    result = _dispatcher(
        RecordingLocator(candidate),
        step_functions,
        clock=lambda: NOW + offset,
    ).dispatch_due()[0]

    assert result.disposition is PublicationDispatchDisposition.DEADLINE_EXPIRED
    assert result.execution_arn is None
    assert step_functions.start_calls == []
    assert step_functions.describe_calls == []


def test_final_per_item_clock_check_never_starts_work_that_expires_during_batch() -> None:
    deadline = NOW + timedelta(seconds=1)
    candidate = _candidate(deadline=deadline)
    observations = iter((NOW, deadline))
    step_functions = RecordingStepFunctions()

    result = _dispatcher(
        RecordingLocator(candidate),
        step_functions,
        clock=lambda: next(observations),
    ).dispatch_due()[0]

    assert result.disposition is PublicationDispatchDisposition.DEADLINE_EXPIRED
    assert result.verification_deadline == deadline
    assert step_functions.start_calls == []


def test_due_batch_is_hard_bounded_to_twenty_five() -> None:
    candidates = tuple(_candidate(str(index)) for index in range(25))
    step_functions = RecordingStepFunctions()

    results = _dispatcher(RecordingLocator(*candidates), step_functions).dispatch_due()

    assert len(results) == 25
    assert len(step_functions.start_calls) == 25
    with pytest.raises(ValueError, match="between 1 and 25"):
        _dispatcher(RecordingLocator(), RecordingStepFunctions()).dispatch_due(limit=True)
    with pytest.raises(ValueError, match="between 1 and 25"):
        _dispatcher(RecordingLocator(), RecordingStepFunctions()).dispatch_due(limit=26)
    with pytest.raises(PublicationDispatchConfigurationError, match="bounded"):
        _dispatcher(
            RecordingLocator(*candidates, _candidate("overflow")),
            RecordingStepFunctions(),
        ).dispatch_due()


def test_duplicate_or_tampered_locator_authority_is_rejected_before_start() -> None:
    candidate = _candidate()
    step_functions = RecordingStepFunctions()

    with pytest.raises(PublicationDispatchConfigurationError, match="duplicate"):
        _dispatcher(RecordingLocator(candidate, candidate), step_functions).dispatch_due()

    tampered = candidate.model_copy(update={"execution_name": "caller_selected"})
    with pytest.raises(PublicationDispatchConfigurationError, match="candidate"):
        _dispatcher(RecordingLocator(tampered), step_functions).dispatch_due()
    assert step_functions.start_calls == []


def test_invalid_candidate_clock_and_locator_failure_are_value_free() -> None:
    with pytest.raises(ValidationError):
        PublicationDispatchCandidate(
            owner_id=OWNER_ID,
            aggregate_id="publication_one",
            work_request_id="publication_work_one",
            execution_name="caller_selected",
            verification_deadline=NOW + timedelta(minutes=30),
            status=PublicationExecutionWorkStatus.PENDING,
        )
    with pytest.raises(PublicationDispatchConfigurationError, match="aware"):
        _dispatcher(RecordingLocator(), RecordingStepFunctions(), clock=datetime.now).dispatch_due()
    with pytest.raises(PublicationDispatchDependencyUnavailableError) as captured:
        _dispatcher(
            RecordingLocator(error=RuntimeError("private owner identifier")),
            RecordingStepFunctions(),
        ).dispatch_due()
    assert captured.value.__cause__ is None
    assert "private owner identifier" not in str(captured.value)
