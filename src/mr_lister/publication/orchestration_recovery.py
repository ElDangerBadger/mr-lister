"""Provider-free, same-execution recovery for the Phase 7 publication workflow.

The recovery boundary can describe and redrive one already-existing Standard workflow, or settle
the existing durable publication authority after its immutable deadline.  It has no operation for
starting a second execution and constructs no provider, credential, route, or transport.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from hashlib import sha256
from typing import Any, Literal, Protocol

from pydantic import Field

from mr_lister.publication.contract import PublicationPermitState, PublicationState
from mr_lister.publication.execution_commands import (
    RecoverConsumedPublicationClaimCommand,
    SettlePublicationDeadlineCommand,
)
from mr_lister.publication.execution_models import PublicationExecutionAuthority
from mr_lister.publication.models import OwnerId, PublicationModel, SafeId, UtcDateTime
from mr_lister.publication.orchestration import (
    PublicationDispatchConfigurationError,
    publication_execution_arn,
    publication_execution_name,
)

_EXECUTION_ARN = re.compile(
    r"^arn:(aws|aws-us-gov|aws-cn):states:[a-z0-9-]+:\d{12}:"
    r"execution:[A-Za-z0-9_-]{1,80}:[A-Za-z0-9_-]{1,80}$"
)
_FAILED_EXECUTION_STATUSES = frozenset({"FAILED", "TIMED_OUT", "ABORTED"})
_KNOWN_FAIL_STATE_ERRORS = frozenset(
    {"PublicationPollBudgetExhausted", "PublicationWorkflowFailed"}
)
_MAX_REDRIVES = 3


class PublicationRecoveryError(RuntimeError):
    """Base value-free workflow recovery failure."""


class PublicationRecoveryBoundaryInvalidError(PublicationRecoveryError):
    """Invocation or dependency evidence did not match one exact publication execution."""


class PublicationRecoveryDependencyUnavailableError(PublicationRecoveryError):
    """A required strong read, durable transition, or workflow operation was unavailable."""


class PublicationRecoveryConflictError(PublicationRecoveryError):
    """Workflow and durable publication authority do not describe the same execution."""


class PublicationWorkflowFailureEnvelope(PublicationModel):
    """The complete sanitized EventBridge-to-SQS workflow-failure envelope."""

    execution_arn: str = Field(pattern=_EXECUTION_ARN.pattern, max_length=256)
    machine_arn: str = Field(max_length=256)
    status: Literal["FAILED", "TIMED_OUT", "ABORTED"]


class PublicationPreDispatchDeadlineEnvelope(PublicationModel):
    """Exact internal request to settle work that was already expired when discovered."""

    kind: Literal["pre_dispatch_deadline_elapsed"] = "pre_dispatch_deadline_elapsed"
    owner_id: OwnerId
    aggregate_id: SafeId
    work_request_id: SafeId
    verification_deadline: UtcDateTime


class PublicationWorkflowInput(PublicationModel):
    """The only data admitted into a publication state-machine execution."""

    owner_id: OwnerId
    aggregate_id: SafeId


class PublicationRecoveryDisposition(StrEnum):
    RUNNING = "running"
    PENDING_REDRIVE = "pending_redrive"
    TERMINAL = "terminal"
    REDRIVEN = "redriven"
    DEADLINE_SETTLED = "deadline_settled"
    NON_REDRIVABLE = "non_redrivable"


@dataclass(frozen=True, slots=True)
class PublicationRecoveryResult:
    disposition: PublicationRecoveryDisposition
    redrive_count: int


class PublicationRecoveryStore(Protocol):
    def load_execution_authority(
        self,
        owner_id: str,
        aggregate_id: str,
    ) -> PublicationExecutionAuthority: ...


class PublicationRecoveryExecutionControl(Protocol):
    def recover_consumed_claim(
        self,
        command: RecoverConsumedPublicationClaimCommand,
    ) -> object: ...

    def settle_deadline(self, command: SettlePublicationDeadlineCommand) -> object: ...


class PublicationRecoveryStepFunctions(Protocol):
    """Deliberately omits StartExecution; recovery can only inspect/redrive one ARN."""

    def describe_execution(self, **request: Any) -> Mapping[str, Any]: ...

    def redrive_execution(self, **request: Any) -> Mapping[str, Any]: ...


class PublicationWorkflowRecovery:
    """Strongly rebind and recover one exact failed publication workflow."""

    __slots__ = (
        "_clock",
        "_execution",
        "_maximum_redrives",
        "_state_machine_arn",
        "_step_functions",
        "_store",
    )

    def __init__(
        self,
        *,
        store: PublicationRecoveryStore,
        execution: PublicationRecoveryExecutionControl,
        step_functions: PublicationRecoveryStepFunctions,
        state_machine_arn: str,
        clock: Callable[[], datetime] | None = None,
        maximum_redrives: int = _MAX_REDRIVES,
    ) -> None:
        try:
            publication_execution_arn(
                state_machine_arn,
                publication_execution_name("configuration_validation"),
            )
        except PublicationDispatchConfigurationError:
            raise PublicationRecoveryBoundaryInvalidError(
                "Publication recovery configuration is invalid"
            ) from None
        if isinstance(maximum_redrives, bool) or not 1 <= maximum_redrives <= _MAX_REDRIVES:
            raise PublicationRecoveryBoundaryInvalidError(
                "Publication recovery configuration is invalid"
            )
        self._store = store
        self._execution = execution
        self._step_functions = step_functions
        self._state_machine_arn = state_machine_arn
        self._clock = clock or (lambda: datetime.now(UTC))
        self._maximum_redrives = maximum_redrives

    def recover(
        self,
        envelope: PublicationWorkflowFailureEnvelope,
    ) -> PublicationRecoveryResult:
        exact_envelope = self._exact_envelope(envelope)
        if exact_envelope.machine_arn != self._state_machine_arn:
            raise PublicationRecoveryBoundaryInvalidError(
                "Publication recovery invocation is invalid"
            )
        observation = self._describe(exact_envelope.execution_arn)
        workflow_input, execution_name, status, error, redrive_count, redrive_status = (
            self._validate_observation(exact_envelope, observation)
        )
        authority = self._load_authority(workflow_input)
        self._bind_authority(
            authority,
            workflow_input=workflow_input,
            execution_name=execution_name,
            execution_arn=exact_envelope.execution_arn,
        )
        if authority.aggregate.state in {
            PublicationState.PUBLISHED,
            PublicationState.PUBLICATION_FAILED,
            PublicationState.PUBLICATION_OUTCOME_UNKNOWN,
        }:
            return PublicationRecoveryResult(
                PublicationRecoveryDisposition.TERMINAL,
                redrive_count,
            )

        now = self._now()
        if now >= authority.snapshot.verification_deadline:
            self._settle_deadline(authority)
            return PublicationRecoveryResult(
                PublicationRecoveryDisposition.DEADLINE_SETTLED,
                redrive_count,
            )
        if status == "RUNNING":
            return PublicationRecoveryResult(PublicationRecoveryDisposition.RUNNING, redrive_count)
        if status == "PENDING_REDRIVE":
            return PublicationRecoveryResult(
                PublicationRecoveryDisposition.PENDING_REDRIVE,
                redrive_count,
            )
        if status == "SUCCEEDED":
            raise PublicationRecoveryConflictError(
                "Successful workflow lacks durable terminal publication authority"
            )
        if status not in _FAILED_EXECUTION_STATUSES:
            raise PublicationRecoveryBoundaryInvalidError(
                "Publication workflow observation is invalid"
            )
        if (
            error in _KNOWN_FAIL_STATE_ERRORS
            or redrive_count >= self._maximum_redrives
            or redrive_status != "REDRIVABLE"
        ):
            return PublicationRecoveryResult(
                PublicationRecoveryDisposition.NON_REDRIVABLE,
                redrive_count,
            )
        token = self._redrive_token(exact_envelope.execution_arn, redrive_count)
        try:
            response = self._step_functions.redrive_execution(
                executionArn=exact_envelope.execution_arn,
                clientToken=token,
            )
        except Exception:
            raise PublicationRecoveryDependencyUnavailableError(
                "Publication workflow redrive is unavailable"
            ) from None
        redrive_date = response.get("redriveDate") if isinstance(response, Mapping) else None
        if (
            not isinstance(redrive_date, datetime)
            or redrive_date.tzinfo is None
            or redrive_date.utcoffset() is None
        ):
            raise PublicationRecoveryBoundaryInvalidError(
                "Publication workflow redrive response is invalid"
            )
        return PublicationRecoveryResult(
            PublicationRecoveryDisposition.REDRIVEN,
            redrive_count + 1,
        )

    def settle_pre_dispatch_deadline(
        self,
        envelope: PublicationPreDispatchDeadlineEnvelope,
    ) -> PublicationRecoveryResult:
        """Settle one expired durable request without creating or inspecting a workflow."""

        exact_envelope = self._exact_deadline_envelope(envelope)
        workflow_input = PublicationWorkflowInput(
            owner_id=exact_envelope.owner_id,
            aggregate_id=exact_envelope.aggregate_id,
        )
        authority = self._load_authority(workflow_input)
        self._bind_pre_dispatch_authority(authority, envelope=exact_envelope)
        if authority.aggregate.state in {
            PublicationState.PUBLISHED,
            PublicationState.PUBLICATION_FAILED,
            PublicationState.PUBLICATION_OUTCOME_UNKNOWN,
        }:
            return PublicationRecoveryResult(
                PublicationRecoveryDisposition.TERMINAL,
                0,
            )
        if self._now() < authority.snapshot.verification_deadline:
            raise PublicationRecoveryBoundaryInvalidError(
                "Publication pre-dispatch deadline has not elapsed"
            )
        self._settle_deadline(authority)
        return PublicationRecoveryResult(
            PublicationRecoveryDisposition.DEADLINE_SETTLED,
            0,
        )

    def _describe(self, execution_arn: str) -> Mapping[str, Any]:
        try:
            observation = self._step_functions.describe_execution(executionArn=execution_arn)
        except Exception:
            raise PublicationRecoveryDependencyUnavailableError(
                "Publication workflow observation is unavailable"
            ) from None
        if not isinstance(observation, Mapping):
            raise PublicationRecoveryBoundaryInvalidError(
                "Publication workflow observation is invalid"
            )
        return observation

    def _validate_observation(
        self,
        envelope: PublicationWorkflowFailureEnvelope,
        observation: Mapping[str, Any],
    ) -> tuple[PublicationWorkflowInput, str, str, str | None, int, str | None]:
        try:
            execution_arn = observation["executionArn"]
            machine_arn = observation["stateMachineArn"]
            execution_name = observation["name"]
            raw_input = observation["input"]
            status = observation["status"]
            redrive_count = observation.get("redriveCount", 0)
            redrive_status = observation.get("redriveStatus")
            error = observation.get("error")
            if (
                execution_arn != envelope.execution_arn
                or machine_arn != self._state_machine_arn
                or not isinstance(execution_name, str)
                or not isinstance(raw_input, str)
                or not isinstance(status, str)
                or isinstance(redrive_count, bool)
                or not isinstance(redrive_count, int)
                or not 0 <= redrive_count <= 1_000
                or (redrive_status is not None and not isinstance(redrive_status, str))
                or (error is not None and not isinstance(error, str))
            ):
                raise ValueError
            decoded = json.loads(raw_input)
            workflow_input = PublicationWorkflowInput.model_validate(decoded, strict=True)
            if raw_input != workflow_input.model_dump_json(exclude={"contract_version"}):
                expected = json.dumps(
                    workflow_input.model_dump(mode="json", exclude={"contract_version"}),
                    sort_keys=True,
                    separators=(",", ":"),
                )
                if raw_input != expected:
                    raise ValueError
            expected_arn = publication_execution_arn(
                self._state_machine_arn,
                execution_name,
            )
            if expected_arn != envelope.execution_arn:
                raise ValueError
            return (
                workflow_input,
                execution_name,
                status,
                error,
                redrive_count,
                redrive_status,
            )
        except Exception:
            raise PublicationRecoveryBoundaryInvalidError(
                "Publication workflow observation is invalid"
            ) from None

    def _load_authority(
        self,
        workflow_input: PublicationWorkflowInput,
    ) -> PublicationExecutionAuthority:
        try:
            candidate = self._store.load_execution_authority(
                workflow_input.owner_id,
                workflow_input.aggregate_id,
            )
            exact = PublicationExecutionAuthority.model_validate(
                candidate.model_dump(mode="python"),
                strict=True,
            )
            if exact != candidate:
                raise ValueError
            return exact
        except PublicationRecoveryError:
            raise
        except Exception:
            raise PublicationRecoveryDependencyUnavailableError(
                "Publication recovery authority is unavailable"
            ) from None

    def _bind_authority(
        self,
        authority: PublicationExecutionAuthority,
        *,
        workflow_input: PublicationWorkflowInput,
        execution_name: str,
        execution_arn: str,
    ) -> None:
        try:
            expected_name = publication_execution_name(authority.work.work_request_id)
            expected_arn = publication_execution_arn(self._state_machine_arn, expected_name)
        except PublicationDispatchConfigurationError:
            raise PublicationRecoveryConflictError(
                "Publication workflow differs from durable authority"
            ) from None
        if (
            authority.snapshot.owner_id != workflow_input.owner_id
            or authority.aggregate.aggregate_id != workflow_input.aggregate_id
            or authority.work.owner_id != workflow_input.owner_id
            or authority.work.aggregate_id != workflow_input.aggregate_id
            or authority.work.execution_name != expected_name
            or execution_name != expected_name
            or execution_arn != expected_arn
        ):
            raise PublicationRecoveryConflictError(
                "Publication workflow differs from durable authority"
            )

    @staticmethod
    def _bind_pre_dispatch_authority(
        authority: PublicationExecutionAuthority,
        *,
        envelope: PublicationPreDispatchDeadlineEnvelope,
    ) -> None:
        try:
            expected_name = publication_execution_name(authority.work.work_request_id)
        except PublicationDispatchConfigurationError:
            raise PublicationRecoveryConflictError(
                "Publication deadline request differs from durable authority"
            ) from None
        if (
            authority.snapshot.owner_id != envelope.owner_id
            or authority.aggregate.owner_id != envelope.owner_id
            or authority.aggregate.aggregate_id != envelope.aggregate_id
            or authority.work.owner_id != envelope.owner_id
            or authority.work.aggregate_id != envelope.aggregate_id
            or authority.work.work_request_id != envelope.work_request_id
            or authority.work.execution_name != expected_name
            or authority.snapshot.verification_deadline != envelope.verification_deadline
            or authority.work.verification_deadline != envelope.verification_deadline
        ):
            raise PublicationRecoveryConflictError(
                "Publication deadline request differs from durable authority"
            )

    def _settle_deadline(self, authority: PublicationExecutionAuthority) -> None:
        try:
            if (
                authority.aggregate.state is PublicationState.PUBLICATION_REQUESTED
                and authority.permit.status is PublicationPermitState.CONSUMED
            ):
                mutation = authority.mutation_claim
                if mutation is None or authority.post_observation is not None:
                    raise ValueError
                operation_id = self._operation_id("recover_claim", authority)
                self._execution.recover_consumed_claim(
                    RecoverConsumedPublicationClaimCommand(
                        **self._command_basis(authority, operation_id),
                        mutation_claim_id=mutation.mutation_claim_id,
                        mutation_claim_fingerprint=mutation.fingerprint,
                    )
                )
                authority = self._load_authority(
                    PublicationWorkflowInput(
                        owner_id=authority.snapshot.owner_id,
                        aggregate_id=authority.aggregate.aggregate_id,
                    )
                )
            operation_id = self._operation_id("deadline", authority)
            self._execution.settle_deadline(
                SettlePublicationDeadlineCommand(
                    **self._command_basis(authority, operation_id),
                )
            )
            current = self._load_authority(
                PublicationWorkflowInput(
                    owner_id=authority.snapshot.owner_id,
                    aggregate_id=authority.aggregate.aggregate_id,
                )
            )
            if current.aggregate.state not in {
                PublicationState.PUBLICATION_FAILED,
                PublicationState.PUBLICATION_OUTCOME_UNKNOWN,
            }:
                raise ValueError
        except PublicationRecoveryError:
            raise
        except Exception:
            raise PublicationRecoveryDependencyUnavailableError(
                "Publication deadline settlement is unavailable"
            ) from None

    @staticmethod
    def _command_basis(
        authority: PublicationExecutionAuthority,
        operation_id: str,
    ) -> dict[str, object]:
        return {
            "owner_id": authority.snapshot.owner_id,
            "aggregate_id": authority.aggregate.aggregate_id,
            "operation_id": operation_id,
            "expected_aggregate_record_version": authority.aggregate.record_version,
            "expected_aggregate_fingerprint": authority.aggregate.fingerprint,
            "expected_provider_evidence_record_version": (
                authority.aggregate.provider_evidence_record_version
            ),
            "expected_attempt_record_version": authority.attempt.record_version,
            "expected_permit_record_version": authority.permit.record_version,
            "expected_work_record_version": authority.work.record_version,
        }

    @staticmethod
    def _operation_id(action: str, authority: PublicationExecutionAuthority) -> str:
        material = ":".join(
            (
                "phase7.15b",
                action,
                authority.aggregate.aggregate_id,
                str(authority.aggregate.record_version),
                str(authority.attempt.record_version),
                str(authority.permit.record_version),
                str(authority.work.record_version),
            )
        )
        return f"phase715b_{action}_{sha256(material.encode()).hexdigest()[:40]}"

    @staticmethod
    def _redrive_token(execution_arn: str, redrive_count: int) -> str:
        return sha256(f"phase7.15b\x00{execution_arn}\x00{redrive_count}".encode()).hexdigest()

    @staticmethod
    def _exact_envelope(
        envelope: PublicationWorkflowFailureEnvelope,
    ) -> PublicationWorkflowFailureEnvelope:
        try:
            if not isinstance(envelope, PublicationWorkflowFailureEnvelope):
                raise ValueError
            exact = PublicationWorkflowFailureEnvelope.model_validate(
                envelope.model_dump(mode="python"),
                strict=True,
            )
            if exact != envelope:
                raise ValueError
            return exact
        except Exception:
            raise PublicationRecoveryBoundaryInvalidError(
                "Publication recovery invocation is invalid"
            ) from None

    @staticmethod
    def _exact_deadline_envelope(
        envelope: PublicationPreDispatchDeadlineEnvelope,
    ) -> PublicationPreDispatchDeadlineEnvelope:
        try:
            if not isinstance(envelope, PublicationPreDispatchDeadlineEnvelope):
                raise ValueError
            exact = PublicationPreDispatchDeadlineEnvelope.model_validate(
                envelope.model_dump(mode="python"),
                strict=True,
            )
            if exact != envelope:
                raise ValueError
            return exact
        except Exception:
            raise PublicationRecoveryBoundaryInvalidError(
                "Publication deadline invocation is invalid"
            ) from None

    def _now(self) -> datetime:
        try:
            value = self._clock()
        except Exception:
            raise PublicationRecoveryDependencyUnavailableError(
                "Publication recovery clock is unavailable"
            ) from None
        if (
            not isinstance(value, datetime)
            or value.tzinfo is None
            or value.utcoffset() != timedelta(0)
        ):
            raise PublicationRecoveryBoundaryInvalidError(
                "Publication recovery clock must return a UTC-aware datetime"
            )
        return value.astimezone(UTC)


__all__ = [
    "PublicationPreDispatchDeadlineEnvelope",
    "PublicationRecoveryBoundaryInvalidError",
    "PublicationRecoveryConflictError",
    "PublicationRecoveryDependencyUnavailableError",
    "PublicationRecoveryDisposition",
    "PublicationRecoveryError",
    "PublicationRecoveryExecutionControl",
    "PublicationRecoveryResult",
    "PublicationRecoveryStepFunctions",
    "PublicationRecoveryStore",
    "PublicationWorkflowFailureEnvelope",
    "PublicationWorkflowInput",
    "PublicationWorkflowRecovery",
]
