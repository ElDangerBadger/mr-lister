"""Bounded dispatcher for Phase 6 transactional-outbox work.

The dispatcher owns only delivery to an allowlisted Standard Step Functions
state machine.  It cannot choose business state, marketplace operations, or an
arbitrary workflow ARN.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from typing import Any, Protocol
from uuid import uuid4

from botocore.exceptions import (
    ClientError,
    ConnectionClosedError,
    ConnectTimeoutError,
    EndpointConnectionError,
    ReadTimeoutError,
)

from mr_lister.control.fingerprints import canonical_fingerprint
from mr_lister.control.models import WorkRequest, WorkRequestStatus, WorkType

_STATE_MACHINE_ARN = re.compile(
    r"^arn:(aws|aws-us-gov|aws-cn):states:[a-z0-9-]+:\d{12}:"
    r"stateMachine:[A-Za-z0-9_-]{1,80}$"
)
_SAFE_CLAIM_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
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


class DispatchConfigurationError(Exception):
    """The fixed dispatcher configuration or stored work identity is invalid."""


class DispatchRejectedError(Exception):
    """Step Functions rejected dispatch in a way that is not safely retryable."""


class DispatchIdentityConflictError(Exception):
    """An existing execution does not match the immutable work request."""


class DispatchStore(Protocol):
    """Narrow store surface required by the outbox dispatcher."""

    def list_due_work(self, *, now: datetime, limit: int) -> tuple[WorkRequest, ...]: ...

    def claim_work(
        self,
        job_id: str,
        work_request_id: str,
        *,
        claim_id: str,
        now: datetime,
        lease_expires_at: datetime,
    ) -> WorkRequest | None: ...

    def mark_work_dispatched(
        self,
        job_id: str,
        work_request_id: str,
        *,
        claim_id: str,
        execution_arn: str,
        now: datetime,
    ) -> WorkRequest: ...

    def defer_claimed_work(
        self,
        job_id: str,
        work_request_id: str,
        *,
        claim_id: str,
        retry_at: datetime,
        now: datetime,
        error_code: str,
    ) -> WorkRequest: ...


def work_input_fingerprint(
    *,
    work_type: WorkType,
    job_id: str,
    work_request_id: str,
) -> str:
    """Bind the selected work type and exact identifier-only execution input."""

    return canonical_fingerprint(
        {
            "work_type": work_type.value,
            "input": {
                "job_id": job_id,
                "work_request_id": work_request_id,
            },
        }
    )


def deterministic_execution_name(work_request_id: str) -> str:
    """Derive a stable, Step-Functions-safe name from one immutable work ID."""

    digest = sha256(work_request_id.encode()).hexdigest()[:48]
    return f"mr-lister-{digest}"


def execution_arn_for(state_machine_arn: str, execution_name: str) -> str:
    """Derive the Standard execution ARN for an unqualified state-machine ARN."""

    if not _STATE_MACHINE_ARN.fullmatch(state_machine_arn):
        raise DispatchConfigurationError("The state-machine ARN is not an allowed ARN shape")
    parts = state_machine_arn.split(":")
    state_machine_name = parts[-1]
    return ":".join((*parts[:5], "execution", state_machine_name, execution_name))


class WorkDispatcher:
    """Claim due work and dispatch it once to a fixed state-machine allowlist."""

    def __init__(
        self,
        *,
        store: DispatchStore,
        step_functions: Any,
        state_machine_arns: Mapping[WorkType, str],
        clock: Callable[[], datetime] | None = None,
        claim_id_factory: Callable[[], str] | None = None,
        lease_seconds: int = 60,
        base_backoff_seconds: int = 2,
        maximum_backoff_seconds: int = 300,
    ) -> None:
        self._store = store
        self._step_functions = step_functions
        self._state_machine_arns = self._validate_allowlist(state_machine_arns)
        self._clock = clock or (lambda: datetime.now(UTC))
        self._claim_id_factory = claim_id_factory or (lambda: f"claim_{uuid4().hex}")
        if not 5 <= lease_seconds <= 900:
            raise DispatchConfigurationError("Dispatch lease must be between 5 and 900 seconds")
        if base_backoff_seconds < 1:
            raise DispatchConfigurationError("Dispatch backoff must begin at one second or later")
        if maximum_backoff_seconds < base_backoff_seconds:
            raise DispatchConfigurationError("Maximum backoff cannot be below the base backoff")
        self._lease = timedelta(seconds=lease_seconds)
        self._base_backoff_seconds = base_backoff_seconds
        self._maximum_backoff_seconds = maximum_backoff_seconds

    def dispatch_due(self, *, limit: int = 25) -> tuple[WorkRequest, ...]:
        """Attempt a bounded batch and return each claimed request's resulting record."""

        if not 1 <= limit <= 100:
            raise ValueError("Dispatch batch limit must be between 1 and 100")
        now = self._now()
        due = self._store.list_due_work(now=now, limit=limit)
        results: list[WorkRequest] = []
        for request in due:
            result = self.dispatch_one(
                request.job_id,
                request.work_request_id,
                now=now,
            )
            if result is not None:
                results.append(result)
        return tuple(results)

    def dispatch_one(
        self,
        job_id: str,
        work_request_id: str,
        *,
        now: datetime | None = None,
    ) -> WorkRequest | None:
        """Claim and dispatch one due request; return ``None`` if another worker won."""

        dispatch_time = now or self._now()
        claim_id = self._claim_id_factory()
        if not _SAFE_CLAIM_ID.fullmatch(claim_id):
            raise DispatchConfigurationError("Claim ID is outside the bounded identifier contract")
        claimed = self._store.claim_work(
            job_id,
            work_request_id,
            claim_id=claim_id,
            now=dispatch_time,
            lease_expires_at=dispatch_time + self._lease,
        )
        if claimed is None:
            return None
        self._validate_claim(claimed, claim_id=claim_id)
        state_machine_arn = self._state_machine_arns[claimed.work_type]
        payload = self._execution_input(claimed)
        expected_execution_arn = execution_arn_for(
            state_machine_arn,
            claimed.execution_name,
        )
        try:
            response = self._step_functions.start_execution(
                stateMachineArn=state_machine_arn,
                name=claimed.execution_name,
                input=payload,
            )
            execution_arn = response.get("executionArn")
            if execution_arn != expected_execution_arn:
                raise DispatchIdentityConflictError(
                    "Step Functions returned execution evidence for another identity"
                )
        except ClientError as error:
            code = error.response.get("Error", {}).get("Code", "")
            if code == "ExecutionAlreadyExists":
                try:
                    execution_arn = self._verify_existing_execution(
                        state_machine_arn=state_machine_arn,
                        execution_name=claimed.execution_name,
                        expected_execution_arn=expected_execution_arn,
                        expected_input=payload,
                    )
                except ClientError as describe_error:
                    describe_code = describe_error.response.get("Error", {}).get("Code", "")
                    if describe_code in _TRANSIENT_STEP_FUNCTIONS_CODES:
                        return self._defer_ambiguous_start(
                            claimed,
                            claim_id=claim_id,
                            now=dispatch_time,
                        )
                    raise DispatchRejectedError(
                        "Existing execution verification was rejected"
                    ) from None
                except _TRANSIENT_TRANSPORT_ERRORS:
                    return self._defer_ambiguous_start(
                        claimed,
                        claim_id=claim_id,
                        now=dispatch_time,
                    )
            elif code in _TRANSIENT_STEP_FUNCTIONS_CODES:
                return self._defer_ambiguous_start(
                    claimed,
                    claim_id=claim_id,
                    now=dispatch_time,
                )
            else:
                raise DispatchRejectedError("Step Functions rejected bounded dispatch") from None
        except _TRANSIENT_TRANSPORT_ERRORS:
            return self._defer_ambiguous_start(
                claimed,
                claim_id=claim_id,
                now=dispatch_time,
            )

        return self._store.mark_work_dispatched(
            claimed.job_id,
            claimed.work_request_id,
            claim_id=claim_id,
            execution_arn=execution_arn,
            now=dispatch_time,
        )

    @staticmethod
    def _validate_allowlist(state_machine_arns: Mapping[WorkType, str]) -> dict[WorkType, str]:
        if set(state_machine_arns) != set(WorkType):
            raise DispatchConfigurationError(
                "Dispatcher configuration must map every and only known work type"
            )
        validated: dict[WorkType, str] = {}
        for work_type, arn in state_machine_arns.items():
            if not isinstance(work_type, WorkType) or not _STATE_MACHINE_ARN.fullmatch(arn):
                raise DispatchConfigurationError("Dispatcher allowlist contains an invalid entry")
            validated[work_type] = arn
        return validated

    @staticmethod
    def _execution_input(work: WorkRequest) -> str:
        return json.dumps(
            {
                "job_id": work.job_id,
                "work_request_id": work.work_request_id,
            },
            sort_keys=True,
            separators=(",", ":"),
        )

    @staticmethod
    def _validate_claim(work: WorkRequest, *, claim_id: str) -> None:
        if work.status is not WorkRequestStatus.CLAIMED or work.claim_id != claim_id:
            raise DispatchConfigurationError("Store returned work without the requested claim")
        if work.execution_name != deterministic_execution_name(work.work_request_id):
            raise DispatchConfigurationError("Stored execution name is not deterministic")
        if work.input_fingerprint != work_input_fingerprint(
            work_type=work.work_type,
            job_id=work.job_id,
            work_request_id=work.work_request_id,
        ):
            raise DispatchConfigurationError("Stored work input fingerprint does not match")

    def _verify_existing_execution(
        self,
        *,
        state_machine_arn: str,
        execution_name: str,
        expected_execution_arn: str,
        expected_input: str,
    ) -> str:
        evidence = self._step_functions.describe_execution(executionArn=expected_execution_arn)
        if (
            evidence.get("executionArn") != expected_execution_arn
            or evidence.get("stateMachineArn") != state_machine_arn
            or evidence.get("name") != execution_name
            or evidence.get("input") != expected_input
        ):
            raise DispatchIdentityConflictError(
                "Existing Step Functions execution does not match the work request"
            )
        return expected_execution_arn

    def _defer_ambiguous_start(
        self,
        work: WorkRequest,
        *,
        claim_id: str,
        now: datetime,
    ) -> WorkRequest:
        exponent = max(work.attempt_count - 1, 0)
        delay_seconds = min(
            self._base_backoff_seconds * (2**exponent),
            self._maximum_backoff_seconds,
        )
        # Once StartExecution has been attempted, its outcome may be unknown. Keep the
        # exact claim settleable by a fast worker and re-drive only after its bounded
        # lease/backoff. Returning it to PENDING could make an already-running worker
        # fail authority checks and permanently strand the deterministic execution.
        return self._store.defer_claimed_work(
            work.job_id,
            work.work_request_id,
            claim_id=claim_id,
            retry_at=now + timedelta(seconds=delay_seconds),
            now=now,
            error_code="DISPATCH_TRANSIENT",
        )

    def _now(self) -> datetime:
        now = self._clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise DispatchConfigurationError("Dispatcher clock must return a timezone-aware value")
        return now
