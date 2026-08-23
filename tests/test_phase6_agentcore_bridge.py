from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from io import BytesIO
from typing import Any

import pytest
from botocore.exceptions import ClientError

from mr_lister.agent.contracts import (
    AGENT_FRAMEWORK,
    PREPARATION_AGENT_ID,
    PreparationDecision,
    PreparationRequest,
)
from mr_lister.agent.runtime import correlation_id as runtime_correlation_id
from mr_lister.control.agentcore import (
    AgentCorePreparationBridge,
    PreparationAuthorityError,
    PreparationBridgeAuditRecord,
    PreparationBridgeConfigurationError,
    PreparationResponseError,
    PreparationUnavailableError,
    preparation_work_binding,
)
from mr_lister.control.dispatch import deterministic_execution_name, work_input_fingerprint
from mr_lister.control.fingerprints import canonical_fingerprint
from mr_lister.control.models import (
    AgentPreparationEvidence,
    ControlJobRecord,
    ControlJobState,
    WorkRequest,
    WorkRequestStatus,
    WorkType,
)

NOW = datetime(2026, 8, 21, 15, 0, tzinfo=UTC)
OWNER_ID = "a" * 64
JOB_ID = "job_phase6_agentcore_001"
WORK_ID = "work_phase6_agentcore_001"
EVIDENCE_ID = "evidence_phase6_agentcore_001"
EVIDENCE_FINGERPRINT = "e" * 64
REVIEW_FINGERPRINT = "c" * 64
RUNTIME_ARN = (
    "arn:aws:bedrock-agentcore:us-west-2:123456789012:runtime/mr_lister_preparation-AbCd123456"
)
RUNTIME_QUALIFIER = "phase6_v7_dev"
RUNTIME_VERSION = "7"


def make_authority() -> tuple[ControlJobRecord, WorkRequest]:
    job = ControlJobRecord(
        owner_id=OWNER_ID,
        job_id=JOB_ID,
        state=ControlJobState.INTAKE_VALIDATED,
        active_work_request_id=WORK_ID,
        created_at=NOW - timedelta(minutes=2),
        updated_at=NOW - timedelta(minutes=1),
    )
    work = WorkRequest(
        work_request_id=WORK_ID,
        owner_id=OWNER_ID,
        job_id=JOB_ID,
        receipt_id="receipt_phase6_agentcore_001",
        work_type=WorkType.PREPARE,
        input_fingerprint=work_input_fingerprint(
            work_type=WorkType.PREPARE,
            job_id=JOB_ID,
            work_request_id=WORK_ID,
        ),
        execution_name=deterministic_execution_name(WORK_ID),
        status=WorkRequestStatus.DISPATCHED,
        attempt_count=1,
        execution_arn=(
            "arn:aws:states:us-west-2:123456789012:execution:mr-lister-prepare:execution-001"
        ),
        next_dispatch_at=NOW - timedelta(minutes=1),
        created_at=NOW - timedelta(minutes=2),
        updated_at=NOW - timedelta(minutes=1),
    )
    return job, work


class AuthorityStore:
    def __init__(self, job: ControlJobRecord, work: WorkRequest) -> None:
        self.job = job
        self.work: dict[str, WorkRequest] = {work.work_request_id: work}
        self.evidence: dict[str, AgentPreparationEvidence] = {}
        self.reads: list[tuple[str, ...]] = []

    def get_job(self, job_id: str) -> ControlJobRecord:
        self.reads.append(("job", job_id))
        return self.job

    def get_work_request(self, job_id: str, work_request_id: str) -> WorkRequest:
        self.reads.append(("work", job_id, work_request_id))
        return self.work[work_request_id]

    def get_agent_evidence(self, job_id: str, evidence_id: str) -> AgentPreparationEvidence:
        self.reads.append(("evidence", job_id, evidence_id))
        return self.evidence[evidence_id]

    def complete_from_response(self, response: dict[str, Any]) -> None:
        decision = PreparationDecision.model_validate(response["decision"])
        review_version = self.job.review_version or 1
        evidence = AgentPreparationEvidence(
            evidence_id=EVIDENCE_ID,
            job_id=JOB_ID,
            work_request_id=WORK_ID,
            review_version=review_version,
            correlation_id=response["correlation_id"],
            framework="strands-agents",
            agent_id="mr-lister-preparation",
            controller_model_id="deterministic-agentcore-test",
            tool_calls=("record_prepared_review",),
            cycles=2,
            input_tokens=20,
            output_tokens=10,
            total_tokens=30,
            decision_fingerprint=canonical_fingerprint(decision.model_dump(mode="json")),
            fingerprint=response["evidence_fingerprint"],
            created_at=NOW,
        )
        self.evidence[EVIDENCE_ID] = evidence
        self.work[WORK_ID] = rebuild(
            self.work[WORK_ID],
            status=WorkRequestStatus.COMPLETED,
            claim_id=None,
            lease_expires_at=None,
            updated_at=NOW,
        )
        follow_up_id = "work_phase6_sync_001"
        self.work[follow_up_id] = WorkRequest(
            work_request_id=follow_up_id,
            owner_id=OWNER_ID,
            job_id=JOB_ID,
            receipt_id="receipt_phase6_sync_001",
            work_type=WorkType.SYNCHRONIZE_PRODUCT,
            review_version=review_version,
            input_fingerprint=work_input_fingerprint(
                work_type=WorkType.SYNCHRONIZE_PRODUCT,
                job_id=JOB_ID,
                work_request_id=follow_up_id,
            ),
            execution_name=deterministic_execution_name(follow_up_id),
            next_dispatch_at=NOW,
            created_at=NOW,
            updated_at=NOW,
        )
        self.job = rebuild(
            self.job,
            state=ControlJobState.PRODUCT_DRAFT_SYNCING,
            record_version=self.job.record_version + 1,
            event_sequence=self.job.event_sequence + 1,
            review_version=review_version,
            review_fingerprint=self.job.review_fingerprint or REVIEW_FINGERPRINT,
            review_validated=True,
            agent_evidence_id=EVIDENCE_ID,
            agent_evidence_fingerprint=response["evidence_fingerprint"],
            active_work_request_id=follow_up_id,
            updated_at=NOW,
        )


class MemoryAudit:
    def __init__(self) -> None:
        self.records: list[PreparationBridgeAuditRecord] = []

    def write(self, record: PreparationBridgeAuditRecord) -> None:
        self.records.append(record)


class RecordingAgentCore:
    def __init__(
        self,
        *,
        response_payload: dict[str, Any] | None = None,
        status_code: int = 200,
        error_code: str | None = None,
        unexpected_error: Exception | None = None,
        correlation_override: str | None = None,
        work_binding_override: str | None = None,
        evidence_override: str | None = None,
        on_success: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self.calls: list[dict[str, Any]] = []
        self.response_payload = (
            valid_agent_response() if response_payload is None else response_payload
        )
        self.status_code = status_code
        self.error_code = error_code
        self.unexpected_error = unexpected_error
        self.correlation_override = correlation_override
        self.work_binding_override = work_binding_override
        self.evidence_override = evidence_override
        self.on_success = on_success

    def invoke_agent_runtime(self, **request: Any) -> dict[str, Any]:
        self.calls.append(request)
        if self.unexpected_error is not None:
            raise self.unexpected_error
        if self.error_code is not None:
            raise ClientError(
                {"Error": {"Code": self.error_code, "Message": "private provider detail"}},
                "InvokeAgentRuntime",
            )
        invocation = json.loads(request["payload"])
        expected_correlation = sha256(
            f"{request['runtimeSessionId']}:{invocation['job_id']}".encode()
        ).hexdigest()[:24]
        response = dict(self.response_payload)
        response["correlation_id"] = self.correlation_override or expected_correlation
        response["work_binding"] = self.work_binding_override or request[
            "runtimeSessionId"
        ].removeprefix("mr-lister-phase6-")
        response["evidence_fingerprint"] = self.evidence_override or EVIDENCE_FINGERPRINT
        if self.status_code == 200 and self.on_success is not None:
            self.on_success(response)
        return {
            "statusCode": self.status_code,
            "response": BytesIO(json.dumps(response).encode()),
        }


def valid_agent_response() -> dict[str, Any]:
    return {
        "status": "success",
        "framework": AGENT_FRAMEWORK,
        "agent_id": PREPARATION_AGENT_ID,
        "correlation_id": "0" * 24,
        "work_binding": "0" * 64,
        "evidence_fingerprint": EVIDENCE_FINGERPRINT,
        "decision": PreparationDecision(
            summary="The scoped draft is ready for deterministic application checks.",
            recommendation="Continue to product-draft synchronization before human review.",
            next_action="human_review",
        ).model_dump(mode="json"),
    }


def make_bridge(
    *,
    job: ControlJobRecord | None = None,
    work: WorkRequest | None = None,
    agentcore: RecordingAgentCore | None = None,
) -> tuple[AgentCorePreparationBridge, AuthorityStore, RecordingAgentCore, MemoryAudit]:
    default_job, default_work = make_authority()
    store = AuthorityStore(job or default_job, work or default_work)
    client = agentcore or RecordingAgentCore()
    if client.on_success is None:
        client.on_success = store.complete_from_response
    audit = MemoryAudit()
    return (
        AgentCorePreparationBridge(
            store=store,
            agentcore=client,
            runtime_arn=RUNTIME_ARN,
            runtime_qualifier=RUNTIME_QUALIFIER,
            runtime_version=RUNTIME_VERSION,
            audit_sink=audit,
        ),
        store,
        client,
        audit,
    )


def rebuild(model: Any, **updates: Any) -> Any:
    payload = model.model_dump(mode="python")
    payload.update(updates)
    return type(model).model_validate(payload)


def test_durable_prepare_accepts_only_exact_runtime_and_completed_readback() -> None:
    initial_job, initial_work = make_authority()
    bridge, store, agentcore, audit = make_bridge()

    result = bridge.invoke(job_id=JOB_ID, work_request_id=WORK_ID)

    assert result.framework == "strands-agents"
    assert result.agent_id == "mr-lister-preparation"
    assert result.work_binding == preparation_work_binding(initial_job, initial_work)
    assert result.evidence_fingerprint == EVIDENCE_FINGERPRINT
    assert result.decision.requires_human_approval is True
    assert result.decision.publication_authorized is False
    assert store.job.state is ControlJobState.PRODUCT_DRAFT_SYNCING
    assert store.work[WORK_ID].status is WorkRequestStatus.COMPLETED
    assert store.evidence[EVIDENCE_ID].correlation_id == result.correlation_id
    assert store.reads == [
        ("job", JOB_ID),
        ("work", JOB_ID, WORK_ID),
        ("job", JOB_ID),
        ("work", JOB_ID, WORK_ID),
        ("evidence", JOB_ID, EVIDENCE_ID),
        ("work", JOB_ID, "work_phase6_sync_001"),
    ]

    [request] = agentcore.calls
    assert request["agentRuntimeArn"] == RUNTIME_ARN
    assert request["qualifier"] == RUNTIME_QUALIFIER
    assert OWNER_ID not in request["runtimeSessionId"]
    assert JOB_ID not in request["runtimeSessionId"]
    assert WORK_ID not in request["runtimeSessionId"]
    assert json.loads(request["payload"])["mode"] == "prepare"
    assert result.correlation_id == runtime_correlation_id(
        PreparationRequest(
            session_id=request["runtimeSessionId"],
            job_id=JOB_ID,
            mode="prepare",
            instruction="Prepare and validate this application-scoped job for human review.",
        )
    )
    [record] = audit.records
    assert record.status == "succeeded"
    serialized_audit = record.model_dump_json()
    assert OWNER_ID not in serialized_audit
    assert JOB_ID not in serialized_audit
    assert WORK_ID not in serialized_audit
    assert "123456789012" not in serialized_audit


def test_durable_prepare_accepts_completed_readback_from_listing_checkpoint_resume() -> None:
    initial_job, initial_work = make_authority()
    resumed_job = rebuild(
        initial_job,
        state=ControlJobState.LISTING_DRAFTED,
        record_version=2,
        event_sequence=2,
        review_version=1,
        review_fingerprint=REVIEW_FINGERPRINT,
        review_validated=True,
    )
    bridge, store, agentcore, _audit = make_bridge(job=resumed_job, work=initial_work)

    result = bridge.invoke(job_id=JOB_ID, work_request_id=WORK_ID)

    assert result.decision.next_action == "human_review"
    assert store.job.review_version == 1
    assert store.job.record_version == resumed_job.record_version + 1
    assert store.evidence[EVIDENCE_ID].review_version == 1
    assert len(agentcore.calls) == 1


@pytest.mark.parametrize(
    ("job_change", "work_change"),
    [
        ({"active_work_request_id": "work_other"}, {}),
        ({"state": ControlJobState.PRODUCT_DRAFT_SYNCING}, {}),
        ({}, {"owner_id": "b" * 64}),
        ({}, {"work_type": WorkType.SYNCHRONIZE_PRODUCT}),
        ({}, {"status": WorkRequestStatus.COMPLETED, "execution_arn": None}),
        ({}, {"input_fingerprint": "f" * 64}),
    ],
)
def test_authority_mismatch_rejects_before_agentcore(
    job_change: dict[str, Any], work_change: dict[str, Any]
) -> None:
    job, work = make_authority()
    bridge, _store, agentcore, audit = make_bridge(
        job=rebuild(job, **job_change),
        work=rebuild(work, **work_change),
    )
    with pytest.raises(PreparationAuthorityError, match="does not authorize"):
        bridge.invoke(job_id=JOB_ID, work_request_id=WORK_ID)
    assert agentcore.calls == []
    assert audit.records == []


@pytest.mark.parametrize(
    ("identity_field", "missing"),
    [("framework", False), ("agent_id", False), ("framework", True), ("agent_id", True)],
)
def test_wrong_or_missing_strands_identity_fails_closed(identity_field: str, missing: bool) -> None:
    payload = valid_agent_response()
    if missing:
        payload.pop(identity_field)
    else:
        payload[identity_field] = "wrong-runtime"
    bridge, _store, agentcore, audit = make_bridge(
        agentcore=RecordingAgentCore(response_payload=payload)
    )
    with pytest.raises(PreparationResponseError, match="outside its contract"):
        bridge.invoke(job_id=JOB_ID, work_request_id=WORK_ID)
    assert len(agentcore.calls) == 1
    assert audit.records[-1].error_code == "AGENT_RESPONSE_INVALID"


@pytest.mark.parametrize(
    "client",
    [
        RecordingAgentCore(correlation_override="f" * 24),
        RecordingAgentCore(work_binding_override="f" * 64),
    ],
)
def test_cross_session_or_work_response_is_rejected(client: RecordingAgentCore) -> None:
    bridge, _store, agentcore, audit = make_bridge(agentcore=client)
    with pytest.raises(PreparationResponseError, match="outside its contract"):
        bridge.invoke(job_id=JOB_ID, work_request_id=WORK_ID)
    assert len(agentcore.calls) == 1
    assert audit.records[-1].error_code == "AGENT_RESPONSE_INVALID"


def test_success_response_without_completed_durable_readback_is_rejected() -> None:
    client = RecordingAgentCore(on_success=lambda _response: None)
    bridge, _store, _client, audit = make_bridge(agentcore=client)
    with pytest.raises(PreparationResponseError, match="outside its contract"):
        bridge.invoke(job_id=JOB_ID, work_request_id=WORK_ID)
    assert audit.records[-1].error_code == "AGENT_RESPONSE_INVALID"


def test_agentcore_transient_failure_has_no_fallback_or_private_error_echo() -> None:
    client = RecordingAgentCore(error_code="ThrottlingException")
    bridge, _store, client, audit = make_bridge(agentcore=client)
    with pytest.raises(PreparationUnavailableError, match="temporarily unavailable") as rejected:
        bridge.invoke(job_id=JOB_ID, work_request_id=WORK_ID)
    assert rejected.value.__cause__ is None
    assert "private" not in str(rejected.value)
    assert len(client.calls) == 1
    assert audit.records[-1].error_code == "AGENTCORE_UNAVAILABLE"


def test_unexpected_or_invalid_response_is_sanitized_without_fallback() -> None:
    unexpected = RecordingAgentCore(unexpected_error=RuntimeError("private credential detail"))
    bridge, _store, unexpected, audit = make_bridge(agentcore=unexpected)
    with pytest.raises(PreparationResponseError, match="request was rejected") as rejected:
        bridge.invoke(job_id=JOB_ID, work_request_id=WORK_ID)
    assert "private" not in str(rejected.value)
    assert len(unexpected.calls) == 1
    assert audit.records[-1].error_code == "AGENTCORE_REJECTED"

    rejected = RecordingAgentCore(status_code=502)
    bridge, _store, _client, audit = make_bridge(agentcore=rejected)
    with pytest.raises(PreparationResponseError, match="request was rejected"):
        bridge.invoke(job_id=JOB_ID, work_request_id=WORK_ID)
    assert audit.records[-1].error_code == "AGENTCORE_REJECTED"


@pytest.mark.parametrize(
    "runtime_arn",
    [
        "",
        "arn:aws:bedrock-agentcore:us-west-2:123456789012:runtime/*",
        "arn:aws:bedrock-agentcore:us-west-2:123456789012:runtime/test/endpoint/default",
        "arn:aws:bedrock-agentcore:us-west-2:attacker:runtime/test",
    ],
)
def test_runtime_configuration_must_name_one_exact_runtime(runtime_arn: str) -> None:
    job, work = make_authority()
    with pytest.raises(PreparationBridgeConfigurationError):
        AgentCorePreparationBridge(
            store=AuthorityStore(job, work),
            agentcore=RecordingAgentCore(),
            runtime_arn=runtime_arn,
            runtime_qualifier=RUNTIME_QUALIFIER,
            runtime_version=RUNTIME_VERSION,
            audit_sink=MemoryAudit(),
        )


@pytest.mark.parametrize(
    ("qualifier", "version"),
    [
        ("DEFAULT", "7"),
        ("phase6_v7_dev", "8"),
        ("phase6_v0_dev", "0"),
        ("phase6_v7-dev", "7"),
        ("phase3_v7_dev", "7"),
    ],
)
def test_runtime_configuration_requires_a_version_named_nondefault_endpoint(
    qualifier: str,
    version: str,
) -> None:
    job, work = make_authority()
    with pytest.raises(PreparationBridgeConfigurationError):
        AgentCorePreparationBridge(
            store=AuthorityStore(job, work),
            agentcore=RecordingAgentCore(),
            runtime_arn=RUNTIME_ARN,
            runtime_qualifier=qualifier,
            runtime_version=version,
            audit_sink=MemoryAudit(),
        )
