"""Provider-free orchestration primitives for the Phase 7 publication workflow.

This source-only module owns no persistence mutation, provider client, credential, route, or
runtime registration.  It can deliver one already-persisted publication-work identity to one
fixed Standard Step Functions machine.  Durable publication state remains owned by the worker's
existing execution service.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from hashlib import sha256
from typing import Any, Literal, Protocol

from botocore.exceptions import (
    ClientError,
    ConnectionClosedError,
    ConnectTimeoutError,
    EndpointConnectionError,
    ReadTimeoutError,
)
from pydantic import model_validator

from mr_lister.publication.execution_models import PublicationExecutionWorkStatus
from mr_lister.publication.models import OwnerId, PublicationModel, SafeId, UtcDateTime

_STATE_MACHINE_ARN = re.compile(
    r"^arn:(aws|aws-us-gov|aws-cn):states:[a-z0-9-]+:\d{12}:"
    r"stateMachine:[A-Za-z0-9_-]{1,80}$"
)
_TRANSIENT_STEP_FUNCTIONS_CODES = frozenset(
    {
        "ExecutionDoesNotExist",
        "InternalServerError",
        "InternalServerException",
        "KmsThrottlingException",
        "RequestLimitExceeded",
        "ServiceUnavailable",
        "ServiceUnavailableException",
        "ThrottlingException",
        "TooManyRequestsException",
    }
)
_TRANSIENT_TRANSPORT_ERRORS = (
    ConnectionClosedError,
    ConnectTimeoutError,
    EndpointConnectionError,
    OSError,
    ReadTimeoutError,
    TimeoutError,
)
_MAX_DISPATCH_BATCH = 25


class PublicationDispatchError(RuntimeError):
    """Base value-free publication-dispatch failure."""


class PublicationDispatchConfigurationError(PublicationDispatchError):
    """The fixed machine or persisted work identity is invalid."""


class PublicationDispatchIdentityConflictError(PublicationDispatchError):
    """Step Functions evidence belongs to another execution identity."""


class PublicationDispatchAmbiguousError(PublicationDispatchError):
    """A start outcome could not be resolved through exact readback."""


class PublicationDispatchRejectedError(PublicationDispatchError):
    """Step Functions definitively rejected the fixed dispatch request."""


class PublicationDispatchDependencyUnavailableError(PublicationDispatchError):
    """A required read-only locator or workflow dependency is unavailable."""


class PublicationDispatchCandidate(PublicationModel):
    """Minimum persisted authority needed to deliver one publication workflow."""

    owner_id: OwnerId
    aggregate_id: SafeId
    work_request_id: SafeId
    execution_name: SafeId
    verification_deadline: UtcDateTime
    status: Literal[PublicationExecutionWorkStatus.PENDING] = PublicationExecutionWorkStatus.PENDING

    @model_validator(mode="after")
    def execution_identity_is_deterministic(self) -> PublicationDispatchCandidate:
        if self.execution_name != publication_execution_name(self.work_request_id):
            raise ValueError("Publication execution name is not deterministic")
        return self


class PublicationDispatchDisposition(StrEnum):
    STARTED = "started"
    CONFIRMED_EXISTING = "confirmed_existing"
    DEADLINE_EXPIRED = "deadline_expired"
    RETRY_REQUIRED = "retry_required"


@dataclass(frozen=True, slots=True)
class PublicationDispatchResult:
    disposition: PublicationDispatchDisposition
    owner_id: str
    aggregate_id: str
    work_request_id: str
    verification_deadline: datetime
    execution_arn: str | None


class PublicationDispatchLocator(Protocol):
    """Bounded due-work lookup; implementations must not claim or update work."""

    def list_due_publication_work(
        self,
        *,
        now: datetime,
        limit: int,
    ) -> tuple[PublicationDispatchCandidate, ...]: ...


class PublicationStepFunctions(Protocol):
    def start_execution(self, **request: Any) -> Mapping[str, Any]: ...

    def describe_execution(self, **request: Any) -> Mapping[str, Any]: ...


def publication_execution_name(work_request_id: str) -> str:
    """Reproduce the request service's persisted, Step-Functions-safe execution identity."""

    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", work_request_id) is None:
        raise PublicationDispatchConfigurationError("Publication work identity is invalid")
    digest = sha256(work_request_id.encode("utf-8")).hexdigest()[:40]
    return f"publication_execution_{digest}"


def publication_execution_arn(state_machine_arn: str, execution_name: str) -> str:
    """Derive the sole execution ARN allowed for a persisted publication work record."""

    if _STATE_MACHINE_ARN.fullmatch(state_machine_arn) is None:
        raise PublicationDispatchConfigurationError("Publication state-machine ARN is invalid")
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}", execution_name) is None:
        raise PublicationDispatchConfigurationError("Publication execution name is invalid")
    parts = state_machine_arn.split(":")
    return ":".join((*parts[:5], "execution", parts[-1], execution_name))


class PublicationWorkDispatcher:
    """Deliver pending publication work to one fixed workflow without writing application state."""

    __slots__ = ("_clock", "_locator", "_state_machine_arn", "_step_functions")

    def __init__(
        self,
        *,
        locator: PublicationDispatchLocator,
        step_functions: PublicationStepFunctions,
        state_machine_arn: str,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        # Construction deliberately performs no dependency call.  The ARN validation is pure.
        publication_execution_arn(
            state_machine_arn,
            publication_execution_name("configuration_validation"),
        )
        self._locator = locator
        self._step_functions = step_functions
        self._state_machine_arn = state_machine_arn
        self._clock = clock or (lambda: datetime.now(UTC))

    def dispatch_due(
        self, *, limit: int = _MAX_DISPATCH_BATCH
    ) -> tuple[PublicationDispatchResult, ...]:
        """Read and attempt no more than 25 due identities with a final per-item clock check."""

        if type(limit) is not int or not 1 <= limit <= _MAX_DISPATCH_BATCH:
            raise ValueError("Publication dispatch limit must be between 1 and 25")
        now = self._now()
        try:
            candidates = self._locator.list_due_publication_work(now=now, limit=limit)
        except PublicationDispatchError:
            raise
        except Exception:
            raise PublicationDispatchDependencyUnavailableError(
                "Publication dispatch lookup is unavailable"
            ) from None
        if not isinstance(candidates, tuple) or len(candidates) > limit:
            raise PublicationDispatchConfigurationError(
                "Publication dispatch lookup exceeded its bounded contract"
            )
        exact = tuple(self._exact_candidate(candidate) for candidate in candidates)
        identities = {(item.owner_id, item.aggregate_id) for item in exact}
        if len(identities) != len(exact):
            raise PublicationDispatchConfigurationError(
                "Publication dispatch lookup returned duplicate authority"
            )
        results: list[PublicationDispatchResult] = []
        for candidate in exact:
            now = self._now()
            try:
                results.append(self._dispatch(candidate, now=now))
            except PublicationDispatchError:
                # One unavailable or conflicting workflow identity must not starve unrelated
                # candidates in the same bounded GSI page.  The handler surfaces one aggregate
                # retry only after it has delivered every independent result.
                results.append(
                    PublicationDispatchResult(
                        disposition=PublicationDispatchDisposition.RETRY_REQUIRED,
                        owner_id=candidate.owner_id,
                        aggregate_id=candidate.aggregate_id,
                        work_request_id=candidate.work_request_id,
                        verification_deadline=candidate.verification_deadline,
                        execution_arn=None,
                    )
                )
        return tuple(results)

    def _dispatch(
        self,
        candidate: PublicationDispatchCandidate,
        *,
        now: datetime,
    ) -> PublicationDispatchResult:
        if now >= candidate.verification_deadline:
            return PublicationDispatchResult(
                disposition=PublicationDispatchDisposition.DEADLINE_EXPIRED,
                owner_id=candidate.owner_id,
                aggregate_id=candidate.aggregate_id,
                work_request_id=candidate.work_request_id,
                verification_deadline=candidate.verification_deadline,
                execution_arn=None,
            )
        payload = json.dumps(
            {
                "aggregate_id": candidate.aggregate_id,
                "owner_id": candidate.owner_id,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        expected_arn = publication_execution_arn(
            self._state_machine_arn,
            candidate.execution_name,
        )
        request = {
            "stateMachineArn": self._state_machine_arn,
            "name": candidate.execution_name,
            "input": payload,
        }
        try:
            response = self._step_functions.start_execution(**request)
        except ClientError as error:
            code = str(error.response.get("Error", {}).get("Code", ""))
            if code == "ExecutionAlreadyExists" or code in _TRANSIENT_STEP_FUNCTIONS_CODES:
                self._require_exact_execution(
                    expected_arn=expected_arn,
                    execution_name=candidate.execution_name,
                    expected_input=payload,
                )
                return PublicationDispatchResult(
                    disposition=PublicationDispatchDisposition.CONFIRMED_EXISTING,
                    owner_id=candidate.owner_id,
                    aggregate_id=candidate.aggregate_id,
                    work_request_id=candidate.work_request_id,
                    verification_deadline=candidate.verification_deadline,
                    execution_arn=expected_arn,
                )
            raise PublicationDispatchRejectedError(
                "Step Functions rejected fixed publication dispatch"
            ) from None
        except _TRANSIENT_TRANSPORT_ERRORS:
            self._require_exact_execution(
                expected_arn=expected_arn,
                execution_name=candidate.execution_name,
                expected_input=payload,
            )
            return PublicationDispatchResult(
                disposition=PublicationDispatchDisposition.CONFIRMED_EXISTING,
                owner_id=candidate.owner_id,
                aggregate_id=candidate.aggregate_id,
                work_request_id=candidate.work_request_id,
                verification_deadline=candidate.verification_deadline,
                execution_arn=expected_arn,
            )
        except Exception:
            raise PublicationDispatchDependencyUnavailableError(
                "Publication dispatch dependency is unavailable"
            ) from None
        if not isinstance(response, Mapping) or response.get("executionArn") != expected_arn:
            raise PublicationDispatchIdentityConflictError(
                "Step Functions returned a different publication execution identity"
            )
        return PublicationDispatchResult(
            disposition=PublicationDispatchDisposition.STARTED,
            owner_id=candidate.owner_id,
            aggregate_id=candidate.aggregate_id,
            work_request_id=candidate.work_request_id,
            verification_deadline=candidate.verification_deadline,
            execution_arn=expected_arn,
        )

    def _require_exact_execution(
        self,
        *,
        expected_arn: str,
        execution_name: str,
        expected_input: str,
    ) -> None:
        try:
            evidence = self._step_functions.describe_execution(executionArn=expected_arn)
        except (ClientError, *_TRANSIENT_TRANSPORT_ERRORS):
            raise PublicationDispatchAmbiguousError(
                "Publication start outcome remains unresolved"
            ) from None
        except Exception:
            raise PublicationDispatchDependencyUnavailableError(
                "Publication dispatch readback is unavailable"
            ) from None
        if not isinstance(evidence, Mapping) or (
            evidence.get("executionArn") != expected_arn
            or evidence.get("stateMachineArn") != self._state_machine_arn
            or evidence.get("name") != execution_name
            or evidence.get("input") != expected_input
        ):
            raise PublicationDispatchIdentityConflictError(
                "Existing publication execution does not match durable authority"
            )

    @staticmethod
    def _exact_candidate(candidate: object) -> PublicationDispatchCandidate:
        try:
            if not isinstance(candidate, PublicationDispatchCandidate):
                raise ValueError
            exact = PublicationDispatchCandidate.model_validate(
                candidate.model_dump(mode="python"),
                strict=True,
            )
            if exact != candidate:
                raise ValueError
            return exact
        except Exception:
            raise PublicationDispatchConfigurationError(
                "Publication dispatch candidate is invalid"
            ) from None

    def _now(self) -> datetime:
        try:
            value = self._clock()
        except Exception:
            raise PublicationDispatchDependencyUnavailableError(
                "Publication dispatch clock is unavailable"
            ) from None
        if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
            raise PublicationDispatchConfigurationError(
                "Publication dispatch clock must return an aware datetime"
            )
        return value.astimezone(UTC)


__all__ = [
    "PublicationDispatchAmbiguousError",
    "PublicationDispatchCandidate",
    "PublicationDispatchConfigurationError",
    "PublicationDispatchDependencyUnavailableError",
    "PublicationDispatchDisposition",
    "PublicationDispatchError",
    "PublicationDispatchIdentityConflictError",
    "PublicationDispatchLocator",
    "PublicationDispatchRejectedError",
    "PublicationDispatchResult",
    "PublicationStepFunctions",
    "PublicationWorkDispatcher",
    "publication_execution_arn",
    "publication_execution_name",
]
