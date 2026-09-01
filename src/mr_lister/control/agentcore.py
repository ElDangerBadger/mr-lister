"""Fail-closed Phase 6 bridge from durable PREPARE work to AgentCore.

This module is deliberately a narrow delivery seam.  It verifies application-owned
DynamoDB authority, invokes one configured AgentCore runtime, and validates the
fixed Strands response identity.  It cannot change job state, call a marketplace,
approve a review, or publish a listing.
"""

from __future__ import annotations

import json
import re
import sys
from collections.abc import Mapping
from hashlib import sha256
from typing import IO, Any, Literal, Protocol

from botocore.exceptions import (
    ClientError,
    ConnectionClosedError,
    ConnectTimeoutError,
    EndpointConnectionError,
    ReadTimeoutError,
)
from pydantic import Field, ValidationError, model_validator

from mr_lister.agent.contracts import (
    AGENT_FRAMEWORK,
    PREPARATION_AGENT_ID,
    AgentCoreInvocation,
    AgentFramework,
    PreparationAgentId,
    PreparationDecision,
)
from mr_lister.agent.phase6_contracts import Phase6AgentCoreResponse
from mr_lister.contracts import ContractModel
from mr_lister.control.dispatch import work_input_fingerprint
from mr_lister.control.fingerprints import canonical_fingerprint
from mr_lister.control.models import (
    CONTROL_NEW_WORK_BY_STATE,
    AgentPreparationEvidence,
    ControlJobRecord,
    ControlJobState,
    WorkRequest,
    WorkRequestStatus,
    WorkType,
)

_AGENTCORE_RUNTIME_ARN = re.compile(
    r"^arn:(aws|aws-us-gov|aws-cn):bedrock-agentcore:[a-z0-9-]+:\d{12}:"
    r"runtime/[A-Za-z][A-Za-z0-9_]{0,47}-[A-Za-z0-9]{10}$"
)
_AGENTCORE_ENDPOINT = re.compile(r"^phase6_v(?P<version>[1-9][0-9]{0,4})_[a-z][a-z0-9_]{1,23}$")
_AGENTCORE_VERSION = re.compile(r"^[1-9][0-9]{0,4}$")
_TRANSIENT_AGENTCORE_CODES = frozenset(
    {
        "InternalServerException",
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
_FIXED_PREPARATION_INSTRUCTION = (
    "Prepare and validate this application-scoped job for human review."
)


class PreparationBridgeConfigurationError(Exception):
    """The fixed AgentCore runtime configuration is invalid."""


class PreparationAuthorityError(Exception):
    """Durable application records do not authorize this PREPARE invocation."""


class PreparationUnavailableError(Exception):
    """The exact AgentCore runtime is temporarily unavailable."""


class PreparationResponseError(Exception):
    """The exact runtime rejected the request or returned an invalid contract."""


class PreparationAuthorityStore(Protocol):
    """Read-only authority needed by the preparation dispatcher."""

    def get_job(self, job_id: str) -> ControlJobRecord: ...

    def get_work_request(self, job_id: str, work_request_id: str) -> WorkRequest: ...

    def get_agent_evidence(
        self,
        job_id: str,
        evidence_id: str,
    ) -> AgentPreparationEvidence: ...


class PreparationBridgeResult(ContractModel):
    """Non-authoritative evidence returned to the machine-work handler."""

    framework: AgentFramework
    agent_id: PreparationAgentId
    correlation_id: str = Field(pattern=r"^[a-f0-9]{24}$")
    runtime_fingerprint: str = Field(pattern=r"^[a-f0-9]{24}$")
    work_binding: str = Field(pattern=r"^[a-f0-9]{64}$")
    evidence_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    decision: PreparationDecision


class PreparationBridgeAuditRecord(ContractModel):
    """Public-safe join record; raw owner, job, work, and runtime IDs are omitted."""

    framework: AgentFramework = AGENT_FRAMEWORK
    agent_id: PreparationAgentId = PREPARATION_AGENT_ID
    correlation_id: str = Field(pattern=r"^[a-f0-9]{24}$")
    runtime_fingerprint: str = Field(pattern=r"^[a-f0-9]{24}$")
    mode: Literal["prepare"] = "prepare"
    status: Literal["succeeded", "failed"]
    error_code: (
        Literal[
            "AGENTCORE_UNAVAILABLE",
            "AGENTCORE_REJECTED",
            "AGENT_RESPONSE_INVALID",
        ]
        | None
    ) = None

    @model_validator(mode="after")
    def error_matches_status(self) -> PreparationBridgeAuditRecord:
        if (self.status == "succeeded") != (self.error_code is None):
            raise ValueError("Only failed bridge audits carry an error code")
        return self


class PreparationBridgeAuditSink(Protocol):
    def write(self, record: PreparationBridgeAuditRecord) -> None: ...


class LoggingPreparationBridgeAuditSink:
    """Emit only the sanitized bridge contract for CloudWatch capture."""

    def __init__(self, stream: IO[str] | None = None) -> None:
        self._stream = stream or sys.stdout

    def write(self, record: PreparationBridgeAuditRecord) -> None:
        print(
            f"preparation_bridge_audit={record.model_dump_json()}",
            file=self._stream,
            flush=True,
        )


class AgentCorePreparationBridge:
    """Invoke the one configured Strands runtime for exact active PREPARE work."""

    def __init__(
        self,
        *,
        store: PreparationAuthorityStore,
        agentcore: Any,
        runtime_arn: str,
        runtime_qualifier: str,
        runtime_version: str,
        audit_sink: PreparationBridgeAuditSink | None = None,
        maximum_response_bytes: int = 1_000_000,
    ) -> None:
        if not _AGENTCORE_RUNTIME_ARN.fullmatch(runtime_arn):
            raise PreparationBridgeConfigurationError(
                "The AgentCore runtime ARN is outside the fixed configuration contract"
            )
        endpoint = _AGENTCORE_ENDPOINT.fullmatch(runtime_qualifier)
        if (
            endpoint is None
            or runtime_qualifier == "DEFAULT"
            or _AGENTCORE_VERSION.fullmatch(runtime_version) is None
            or endpoint.group("version") != runtime_version
        ):
            raise PreparationBridgeConfigurationError(
                "The AgentCore endpoint is outside the immutable version contract"
            )
        if not 1 <= maximum_response_bytes <= 1_000_000:
            raise PreparationBridgeConfigurationError(
                "The AgentCore response limit must be between 1 and 1000000 bytes"
            )
        self._store = store
        self._agentcore = agentcore
        self._runtime_arn = runtime_arn
        self._runtime_qualifier = runtime_qualifier
        self._runtime_version = runtime_version
        self._runtime_fingerprint = sha256(
            f"{runtime_arn}:{runtime_qualifier}:v{runtime_version}".encode()
        ).hexdigest()[:24]
        self._audit_sink = audit_sink or LoggingPreparationBridgeAuditSink()
        self._maximum_response_bytes = maximum_response_bytes

    def invoke(self, *, job_id: str, work_request_id: str) -> PreparationBridgeResult:
        """Verify durable authority and invoke AgentCore with no alternate path."""

        job = self._store.get_job(job_id)
        work = self._store.get_work_request(job_id, work_request_id)
        require_prepare_authority(job, work, job_id, work_request_id)

        session_id = self._session_id(job, work)
        correlation_id = sha256(f"{session_id}:{job.job_id}".encode()).hexdigest()[:24]
        try:
            invocation = AgentCoreInvocation(
                job_id=job.job_id,
                mode="prepare",
                instruction=_FIXED_PREPARATION_INSTRUCTION,
            )
        except ValidationError:
            raise PreparationAuthorityError(
                "Durable application state does not authorize this PREPARE invocation"
            ) from None
        payload = _compact_json(invocation.model_dump(mode="json")).encode()
        try:
            response = self._agentcore.invoke_agent_runtime(
                agentRuntimeArn=self._runtime_arn,
                runtimeSessionId=session_id,
                qualifier=self._runtime_qualifier,
                contentType="application/json",
                accept="application/json",
                payload=payload,
            )
        except ClientError as error:
            code = error.response.get("Error", {}).get("Code", "")
            if code in _TRANSIENT_AGENTCORE_CODES:
                self._audit(correlation_id, status="failed", error_code="AGENTCORE_UNAVAILABLE")
                raise PreparationUnavailableError(
                    "The AgentCore preparation runtime is temporarily unavailable"
                ) from None
            self._audit(correlation_id, status="failed", error_code="AGENTCORE_REJECTED")
            raise PreparationResponseError(
                "The AgentCore preparation request was rejected"
            ) from None
        except _TRANSIENT_TRANSPORT_ERRORS:
            self._audit(correlation_id, status="failed", error_code="AGENTCORE_UNAVAILABLE")
            raise PreparationUnavailableError(
                "The AgentCore preparation runtime is temporarily unavailable"
            ) from None
        except Exception:
            # Provider/client implementation details never cross the worker boundary.
            self._audit(correlation_id, status="failed", error_code="AGENTCORE_REJECTED")
            raise PreparationResponseError(
                "The AgentCore preparation request was rejected"
            ) from None

        if not isinstance(response, Mapping) or response.get("statusCode") != 200:
            self._audit(correlation_id, status="failed", error_code="AGENTCORE_REJECTED")
            raise PreparationResponseError("The AgentCore preparation request was rejected")
        try:
            raw = _read_bounded_response(
                response.get("response"),
                maximum_bytes=self._maximum_response_bytes,
            )
            agent_response = Phase6AgentCoreResponse.model_validate_json(raw)
        except (TypeError, ValueError, ValidationError):
            self._audit(correlation_id, status="failed", error_code="AGENT_RESPONSE_INVALID")
            raise PreparationResponseError(
                "The AgentCore preparation response was outside its contract"
            ) from None

        # These comparisons intentionally duplicate the Literal validation so the exact
        # submission identity remains obvious at this security boundary.
        if (
            agent_response.framework != AGENT_FRAMEWORK
            or agent_response.agent_id != PREPARATION_AGENT_ID
            or agent_response.correlation_id != correlation_id
            or agent_response.work_binding != preparation_work_binding(job, work)
        ):
            self._audit(correlation_id, status="failed", error_code="AGENT_RESPONSE_INVALID")
            raise PreparationResponseError(
                "The AgentCore preparation response was outside its contract"
            )

        try:
            self._require_completed_readback(
                original_job=job,
                original_work=work,
                response=agent_response,
            )
        except PreparationResponseError:
            self._audit(correlation_id, status="failed", error_code="AGENT_RESPONSE_INVALID")
            raise
        except Exception:
            self._audit(correlation_id, status="failed", error_code="AGENT_RESPONSE_INVALID")
            raise PreparationResponseError(
                "The AgentCore preparation response was outside its contract"
            ) from None

        self._audit(correlation_id, status="succeeded")
        return PreparationBridgeResult(
            framework=agent_response.framework,
            agent_id=agent_response.agent_id,
            correlation_id=correlation_id,
            runtime_fingerprint=self._runtime_fingerprint,
            work_binding=agent_response.work_binding,
            evidence_fingerprint=agent_response.evidence_fingerprint,
            decision=agent_response.decision,
        )

    @staticmethod
    def _session_id(job: ControlJobRecord, work: WorkRequest) -> str:
        return f"mr-lister-phase6-{preparation_work_binding(job, work)}"

    def _require_completed_readback(
        self,
        *,
        original_job: ControlJobRecord,
        original_work: WorkRequest,
        response: Phase6AgentCoreResponse,
    ) -> None:
        completed_job = self._store.get_job(original_job.job_id)
        completed_work = self._store.get_work_request(
            original_job.job_id,
            original_work.work_request_id,
        )
        if completed_job.agent_evidence_id is None:
            raise PreparationResponseError(
                "The AgentCore preparation response was outside its contract"
            )
        evidence = self._store.get_agent_evidence(
            completed_job.job_id,
            completed_job.agent_evidence_id,
        )
        expected_state = {
            "human_review": ControlJobState.PRODUCT_DRAFT_SYNCING,
            "revise": ControlJobState.NEEDS_REVISION,
        }.get(response.decision.next_action)
        expected_review_version = original_job.review_version or 1
        exact = (
            expected_state is not None
            and completed_job.owner_id == original_job.owner_id
            and completed_job.job_id == original_job.job_id
            and completed_job.record_version > original_job.record_version
            and completed_job.review_version == expected_review_version
            and completed_job.state is expected_state
            and completed_work.owner_id == original_work.owner_id
            and completed_work.job_id == original_work.job_id
            and completed_work.work_request_id == original_work.work_request_id
            and completed_work.work_type is WorkType.PREPARE
            and completed_work.status is WorkRequestStatus.COMPLETED
            and completed_work.input_fingerprint == original_work.input_fingerprint
            and completed_work.last_error_code is None
            and completed_job.agent_evidence_fingerprint == response.evidence_fingerprint
            and evidence.evidence_id == completed_job.agent_evidence_id
            and evidence.fingerprint == response.evidence_fingerprint
            and evidence.job_id == original_job.job_id
            and evidence.work_request_id == original_work.work_request_id
            and evidence.review_version == completed_job.review_version
            and evidence.correlation_id == response.correlation_id
            and evidence.framework == AGENT_FRAMEWORK
            and evidence.agent_id == PREPARATION_AGENT_ID
            and evidence.tool_calls == ("record_prepared_review",)
            and evidence.decision_fingerprint
            == canonical_fingerprint(response.decision.model_dump(mode="json"))
        )
        if not exact:
            raise PreparationResponseError(
                "The AgentCore preparation response was outside its contract"
            )
        if expected_state is ControlJobState.NEEDS_REVISION:
            if completed_job.active_work_request_id is not None:
                raise PreparationResponseError(
                    "The AgentCore preparation response was outside its contract"
                )
            return
        if (
            completed_job.active_work_request_id is None
            or completed_job.active_work_request_id == original_work.work_request_id
        ):
            raise PreparationResponseError(
                "The AgentCore preparation response was outside its contract"
            )
        follow_up = self._store.get_work_request(
            completed_job.job_id,
            completed_job.active_work_request_id,
        )
        if (
            follow_up.owner_id != completed_job.owner_id
            or follow_up.job_id != completed_job.job_id
            or follow_up.work_type is not WorkType.SYNCHRONIZE_PRODUCT
            or follow_up.review_version != completed_job.review_version
            or follow_up.status
            not in {
                WorkRequestStatus.PENDING,
                WorkRequestStatus.CLAIMED,
                WorkRequestStatus.DISPATCHED,
            }
        ):
            raise PreparationResponseError(
                "The AgentCore preparation response was outside its contract"
            )

    def _audit(
        self,
        correlation_id: str,
        *,
        status: Literal["succeeded", "failed"],
        error_code: Literal[
            "AGENTCORE_UNAVAILABLE",
            "AGENTCORE_REJECTED",
            "AGENT_RESPONSE_INVALID",
        ]
        | None = None,
    ) -> None:
        self._audit_sink.write(
            PreparationBridgeAuditRecord(
                correlation_id=correlation_id,
                runtime_fingerprint=self._runtime_fingerprint,
                status=status,
                error_code=error_code,
            )
        )


def preparation_work_binding(job: ControlJobRecord, work: WorkRequest) -> str:
    """Opaque exact owner/job/work/input binding shared by runtime and dispatcher."""

    return canonical_fingerprint(
        {
            "owner_id": job.owner_id,
            "job_id": job.job_id,
            "work_request_id": work.work_request_id,
            "input_fingerprint": work.input_fingerprint,
        }
    )


def require_prepare_authority(
    job: ControlJobRecord,
    work: WorkRequest,
    requested_job_id: str,
    requested_work_request_id: str,
) -> None:
    """Require one exact application-owned active PREPARE operation."""

    expected_fingerprint = work_input_fingerprint(
        work_type=WorkType.PREPARE,
        job_id=requested_job_id,
        work_request_id=requested_work_request_id,
    )
    authorized = (
        job.job_id == requested_job_id
        and job.active_work_request_id == requested_work_request_id
        and CONTROL_NEW_WORK_BY_STATE.get(job.state) is WorkType.PREPARE
        and work.job_id == job.job_id
        and work.work_request_id == requested_work_request_id
        and work.owner_id == job.owner_id
        and work.work_type is WorkType.PREPARE
        and work.status in {WorkRequestStatus.CLAIMED, WorkRequestStatus.DISPATCHED}
        and work.input_fingerprint == expected_fingerprint
    )
    if not authorized:
        raise PreparationAuthorityError(
            "Durable application state does not authorize this PREPARE invocation"
        )
    if work.review_version is not None and work.review_version != job.review_version:
        raise PreparationAuthorityError(
            "Durable application state does not authorize this PREPARE invocation"
        )


def _compact_json(value: Mapping[str, Any]) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _read_bounded_response(body: Any, *, maximum_bytes: int) -> bytes:
    if body is None:
        raise ValueError("missing response")
    if hasattr(body, "read"):
        content = body.read(maximum_bytes + 1)
    elif isinstance(body, bytes):
        content = body
    else:
        try:
            bounded = bytearray()
            for chunk in body:
                if not isinstance(chunk, bytes):
                    raise ValueError("invalid response")
                bounded.extend(chunk)
                if len(bounded) > maximum_bytes:
                    raise ValueError("invalid response")
            content = bytes(bounded)
        except (TypeError, ValueError):
            raise ValueError("invalid response") from None
    if not isinstance(content, bytes) or not content or len(content) > maximum_bytes:
        raise ValueError("invalid response")
    return content
