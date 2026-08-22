"""Credential-free proof of the checkpointed Phase 6 Strands PREPARE path."""

from __future__ import annotations

import json
from collections.abc import AsyncGenerator, AsyncIterable
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import BaseModel
from strands.models.model import Model
from strands.types.content import Messages, SystemContentBlock
from strands.types.streaming import StreamEvent
from strands.types.tools import ToolChoice, ToolResult, ToolSpec

from mr_lister.agent.contracts import PreparationDecision, PreparationRequest
from mr_lister.agent.observability import InMemoryAgentAuditSink
from mr_lister.agent.phase6 import (
    Phase6AgentDecisionCompletion,
    Phase6CompleteAgentDecisionCommand,
    Phase6PreparationBackend,
    Phase6PreparationCommand,
    Phase6PreparedReviewCheckpoint,
    Phase6StrandsPreparationRunner,
    PreparedReviewObservation,
    WorkerControlPreparationAdapter,
    create_phase6_agentcore_runtime,
)
from mr_lister.agent.runtime import AgentExecutionError, correlation_id
from mr_lister.contracts import ArtworkAnalysis, ListingIntelligence
from mr_lister.control.agentcore import PreparationAuthorityError, preparation_work_binding
from mr_lister.control.dispatch import deterministic_execution_name, work_input_fingerprint
from mr_lister.control.fingerprints import canonical_fingerprint
from mr_lister.control.models import (
    AgentPreparationEvidence,
    CommandReceipt,
    CommandResponse,
    ControlJobRecord,
    ControlJobState,
    DomainEvent,
    SourceArtifactRecord,
    WorkRequest,
    WorkRequestStatus,
    WorkType,
)
from mr_lister.control.source_artwork import source_artifact_fingerprint
from mr_lister.control.store import InMemorySellerControlStore
from mr_lister.control.worker_commands import (
    BeginPreparationCommand,
    CompletePreparationWithAgentDecisionCommand,
    RecordPreparedReviewCommand,
)
from mr_lister.control.worker_service import WorkerControlService

NOW = datetime(2026, 8, 21, 16, 0, tzinfo=UTC)
OWNER_ID = "d" * 64
JOB_ID = "job_phase6_strands_001"
WORK_ID = "work_phase6_strands_001"
REVIEW_FINGERPRINT = "b" * 64
EVIDENCE_FINGERPRINT = "e" * 64


def _durable_source_material() -> dict[str, object]:
    return {
        "job_id": JOB_ID,
        "owner_id": OWNER_ID,
        "bucket": "mr-lister-phase6-runtime-test",
        "object_key": f"private/owners/{OWNER_ID}/jobs/{JOB_ID}/source/source.png",
        "version_id": "version_phase6_runtime",
        "content_sha256": "7" * 64,
        "size_bytes": 512,
        "media_type": "image/png",
        "product_profile_id": "gildan_5000_test",
        "product_profile_version": 1,
        "product_profile_fingerprint": "9" * 64,
        "created_at": NOW,
    }


DURABLE_SOURCE_FP = source_artifact_fingerprint(**_durable_source_material())


def authority_records() -> tuple[ControlJobRecord, WorkRequest]:
    job = ControlJobRecord(
        owner_id=OWNER_ID,
        job_id=JOB_ID,
        state=ControlJobState.ANALYZING_ARTWORK,
        record_version=2,
        event_sequence=2,
        active_work_request_id=WORK_ID,
        created_at=NOW - timedelta(minutes=3),
        updated_at=NOW - timedelta(minutes=1),
    )
    work = WorkRequest(
        work_request_id=WORK_ID,
        owner_id=OWNER_ID,
        job_id=JOB_ID,
        receipt_id="receipt_phase6_strands_001",
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
            "arn:aws:states:us-west-2:123456789012:execution:mr-lister-prepare:phase6-strands-001"
        ),
        next_dispatch_at=NOW - timedelta(minutes=2),
        created_at=NOW - timedelta(minutes=3),
        updated_at=NOW - timedelta(minutes=1),
    )
    return job, work


class AuthorityStore:
    def __init__(self, job: ControlJobRecord, work: WorkRequest) -> None:
        self.job = job
        self.work: dict[str, WorkRequest] = {work.work_request_id: work}
        self.evidence: dict[str, AgentPreparationEvidence] = {}

    def get_job(self, job_id: str) -> ControlJobRecord:
        assert job_id == self.job.job_id
        return self.job

    def get_work_request(self, job_id: str, work_request_id: str) -> WorkRequest:
        assert job_id == self.job.job_id
        return self.work[work_request_id]

    def get_agent_evidence(self, job_id: str, evidence_id: str) -> AgentPreparationEvidence:
        assert job_id == self.job.job_id
        return self.evidence[evidence_id]


class CheckpointPreparationService:
    def __init__(
        self,
        store: AuthorityStore,
        *,
        prepare_error: Exception | None = None,
        checkpoint_override: Phase6PreparedReviewCheckpoint | None = None,
    ) -> None:
        self.store = store
        self.prepare_error = prepare_error
        self.checkpoint_override = checkpoint_override
        self.prepare_calls: list[Phase6PreparationCommand] = []
        self.complete_calls: list[Phase6CompleteAgentDecisionCommand] = []
        self.order: list[str] = []

    def prepare_review(self, command: Phase6PreparationCommand) -> Phase6PreparedReviewCheckpoint:
        self.order.append("prepare_review")
        self.prepare_calls.append(command)
        if self.prepare_error is not None:
            raise self.prepare_error
        checkpoint = self.checkpoint_override or Phase6PreparedReviewCheckpoint(
            owner_id=OWNER_ID,
            job_id=JOB_ID,
            work_request_id=WORK_ID,
            state=ControlJobState.LISTING_DRAFTED,
            record_version=3,
            review_version=1,
            review_fingerprint=REVIEW_FINGERPRINT,
            validation_passed=True,
        )
        self.store.job = rebuild(
            self.store.job,
            state=ControlJobState.LISTING_DRAFTED,
            record_version=checkpoint.record_version,
            event_sequence=3,
            review_version=checkpoint.review_version,
            review_fingerprint=checkpoint.review_fingerprint,
            review_validated=checkpoint.validation_passed,
            updated_at=NOW,
        )
        return checkpoint

    def complete_with_agent_decision(
        self, command: Phase6CompleteAgentDecisionCommand
    ) -> Phase6AgentDecisionCompletion:
        self.order.append("complete_with_agent_decision")
        self.complete_calls.append(command)
        evidence_id = "evidence_phase6_strands_001"
        next_work_id = (
            "work_phase6_product_sync_001"
            if command.decision.next_action == "human_review"
            else None
        )
        state = (
            ControlJobState.PRODUCT_DRAFT_SYNCING
            if next_work_id is not None
            else ControlJobState.NEEDS_REVISION
        )
        evidence = AgentPreparationEvidence(
            evidence_id=evidence_id,
            job_id=command.job_id,
            work_request_id=command.work_request_id,
            review_version=command.review_version,
            correlation_id=command.correlation_id,
            framework=command.framework,
            agent_id=command.agent_id,
            controller_model_id=command.controller_model_id,
            tool_calls=command.tool_calls,
            cycles=command.cycles,
            input_tokens=command.input_tokens,
            output_tokens=command.output_tokens,
            total_tokens=command.total_tokens,
            decision_fingerprint=command.decision_fingerprint,
            fingerprint=EVIDENCE_FINGERPRINT,
            created_at=NOW,
        )
        self.store.evidence[evidence_id] = evidence
        original_work = self.store.work[WORK_ID]
        self.store.work[WORK_ID] = rebuild(
            original_work,
            status=WorkRequestStatus.COMPLETED,
            claim_id=None,
            lease_expires_at=None,
            updated_at=NOW,
        )
        if next_work_id is not None:
            self.store.work[next_work_id] = WorkRequest(
                work_request_id=next_work_id,
                owner_id=OWNER_ID,
                job_id=JOB_ID,
                receipt_id="receipt_phase6_product_sync_001",
                work_type=WorkType.SYNCHRONIZE_PRODUCT,
                review_version=command.review_version,
                input_fingerprint=work_input_fingerprint(
                    work_type=WorkType.SYNCHRONIZE_PRODUCT,
                    job_id=JOB_ID,
                    work_request_id=next_work_id,
                ),
                execution_name=deterministic_execution_name(next_work_id),
                next_dispatch_at=NOW,
                created_at=NOW,
                updated_at=NOW,
            )
        self.store.job = rebuild(
            self.store.job,
            state=state,
            record_version=4,
            event_sequence=4,
            agent_evidence_id=evidence_id,
            agent_evidence_fingerprint=EVIDENCE_FINGERPRINT,
            active_work_request_id=next_work_id,
            updated_at=NOW,
        )
        return Phase6AgentDecisionCompletion(
            owner_id=OWNER_ID,
            job_id=JOB_ID,
            work_request_id=WORK_ID,
            state=state,
            record_version=4,
            review_version=command.review_version,
            review_fingerprint=command.review_fingerprint,
            next_action=command.decision.next_action,
            evidence_id=evidence_id,
            evidence_fingerprint=EVIDENCE_FINGERPRINT,
            correlation_id=command.correlation_id,
            next_work_request_id=next_work_id,
        )


def rebuild(model: Any, **updates: Any) -> Any:
    payload = model.model_dump(mode="python")
    payload.update(updates)
    return type(model).model_validate(payload)


class Phase6ToolCallingModel(Model):
    """Local model double that still drives the installed Strands event loop."""

    def __init__(self, *, forced_action: str | None = None, skip_prepare: bool = False) -> None:
        self._config: dict[str, Any] = {
            "model_id": "deterministic-phase6-strands-test",
            "context_window_limit": 10_000,
        }
        self.stream_calls = 0
        self.tool_result: ToolResult | None = None
        self.action = "human_review"
        self.forced_action = forced_action
        self.skip_prepare = skip_prepare

    def update_config(self, **model_config: Any) -> None:
        self._config.update(model_config)

    def get_config(self) -> dict[str, Any]:
        return dict(self._config)

    async def structured_output(
        self,
        output_model: type[BaseModel],
        prompt: Messages,
        system_prompt: str | None = None,
        **kwargs: Any,
    ) -> AsyncGenerator[dict[str, Any], None]:
        del prompt, system_prompt, kwargs
        yield {"output": output_model.model_validate(decision_payload(self.action))}

    async def stream(
        self,
        messages: Messages,
        tool_specs: list[ToolSpec] | None = None,
        system_prompt: str | None = None,
        *,
        tool_choice: ToolChoice | None = None,
        system_prompt_content: list[SystemContentBlock] | None = None,
        invocation_state: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> AsyncIterable[StreamEvent]:
        del system_prompt, tool_choice, system_prompt_content, invocation_state, kwargs
        self.stream_calls += 1
        tool_names = {spec["name"] for spec in tool_specs or []}
        if self.stream_calls == 1:
            if self.skip_prepare:
                async for event in tool_call_events(
                    PreparationDecision.__name__,
                    "premature-decision",
                    decision_payload(self.action),
                ):
                    yield event
                return
            assert "record_prepared_review" in tool_names
            async for event in tool_call_events(
                "record_prepared_review",
                "phase6-prepare-call",
                {},
            ):
                yield event
            return
        results = [
            block["toolResult"]
            for message in messages
            for block in message["content"]
            if "toolResult" in block and block["toolResult"]["toolUseId"] == "phase6-prepare-call"
        ]
        assert len(results) == 1
        self.tool_result = results[0]
        tool_payload = json.loads(self.tool_result["content"][0]["text"])
        if self.forced_action is not None:
            self.action = self.forced_action
        elif not tool_payload["ok"]:
            self.action = "retry"
        elif tool_payload["validation_passed"]:
            self.action = "human_review"
        else:
            self.action = "revise"
        async for event in tool_call_events(
            PreparationDecision.__name__,
            "phase6-decision-call",
            decision_payload(self.action),
        ):
            yield event


async def tool_call_events(
    name: str, tool_use_id: str, tool_input: dict[str, Any]
) -> AsyncGenerator[StreamEvent, None]:
    yield {"messageStart": {"role": "assistant"}}
    yield {"contentBlockStart": {"start": {"toolUse": {"name": name, "toolUseId": tool_use_id}}}}
    yield {"contentBlockDelta": {"delta": {"toolUse": {"input": json.dumps(tool_input)}}}}
    yield {"contentBlockStop": {}}
    yield {"messageStop": {"stopReason": "tool_use"}}
    yield {
        "metadata": {
            "usage": {"inputTokens": 12, "outputTokens": 6, "totalTokens": 18},
            "metrics": {"latencyMs": 1},
        }
    }


def decision_payload(action: str) -> dict[str, Any]:
    return {
        "summary": "The real Strands loop executed the durable Phase 6 preparation tool.",
        "recommendation": "Continue through application-owned workflow controls.",
        "next_action": action,
        "requires_human_approval": True,
        "publication_authorized": False,
    }


def build_runtime(
    *,
    prepare_error: Exception | None = None,
    model: Phase6ToolCallingModel | None = None,
    checkpoint_override: Phase6PreparedReviewCheckpoint | None = None,
) -> tuple[
    Phase6StrandsPreparationRunner,
    AuthorityStore,
    CheckpointPreparationService,
    Phase6ToolCallingModel,
    InMemoryAgentAuditSink,
]:
    job, work = authority_records()
    store = AuthorityStore(job, work)
    service = CheckpointPreparationService(
        store,
        prepare_error=prepare_error,
        checkpoint_override=checkpoint_override,
    )
    local_model = model or Phase6ToolCallingModel()
    audit = InMemoryAgentAuditSink()
    runner = Phase6StrandsPreparationRunner(
        backend=Phase6PreparationBackend(store=store, service=service),
        model=local_model,
        audit_sink=audit,
    )
    return runner, store, service, local_model, audit


class StaticPreparedReviewProducer:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def prepare_review(self, job_id: str, work_request_id: str) -> PreparedReviewObservation:
        assert job_id == JOB_ID
        assert work_request_id == WORK_ID
        self.calls.append((job_id, work_request_id))
        return PreparedReviewObservation(
            source_artifact_fingerprint=DURABLE_SOURCE_FP,
            artwork_analysis=ArtworkAnalysis(
                subject="A geometric badger",
                visual_elements=("compass", "pine trees"),
                styles=("low poly",),
                themes=("woodland adventure",),
                confidence=0.94,
            ),
            listing=ListingIntelligence(
                title="Geometric Badger Graphic Tee",
                description=(
                    "A geometric woodland badger illustration for an everyday graphic tee."
                ),
                tags=(
                    "badger",
                    "woodland",
                    "compass",
                    "forest",
                    "vintage",
                    "outdoors",
                    "nature",
                    "crescent",
                    "pine",
                    "earthy",
                    "camping",
                    "wildlife",
                    "retro",
                ),
                audience=("woodland art fans",),
                title_rationale="Names the visible subject and product.",
                tag_rationale="Uses thirteen distinct buyer-facing concepts.",
            ),
            product_profile_fingerprint="9" * 64,
        )


class RecordingWorkerBoundary:
    def __init__(self, delegate: WorkerControlService) -> None:
        self.delegate = delegate
        self.order: list[str] = []
        self.commands: list[Any] = []

    def begin_preparation(self, command: BeginPreparationCommand) -> CommandResponse:
        self.order.append("begin_preparation")
        self.commands.append(command)
        return self.delegate.begin_preparation(command)

    def record_prepared_review(self, command: RecordPreparedReviewCommand) -> CommandResponse:
        self.order.append("record_prepared_review")
        self.commands.append(command)
        return self.delegate.record_prepared_review(command)

    def complete_preparation_with_agent_decision(
        self,
        command: CompletePreparationWithAgentDecisionCommand,
    ) -> CommandResponse:
        self.order.append("complete_preparation_with_agent_decision")
        self.commands.append(command)
        return self.delegate.complete_preparation_with_agent_decision(command)


def durable_worker_runtime() -> tuple[
    Phase6StrandsPreparationRunner,
    InMemorySellerControlStore,
    RecordingWorkerBoundary,
    StaticPreparedReviewProducer,
    WorkRequest,
]:
    store = InMemorySellerControlStore()
    job = ControlJobRecord(
        owner_id=OWNER_ID,
        job_id=JOB_ID,
        event_sequence=1,
        state=ControlJobState.INTAKE_VALIDATED,
        source_artifact_fingerprint=DURABLE_SOURCE_FP,
        active_work_request_id=WORK_ID,
        created_at=NOW,
        updated_at=NOW,
    )
    work = WorkRequest(
        work_request_id=WORK_ID,
        owner_id=OWNER_ID,
        job_id=JOB_ID,
        receipt_id="receipt_phase6_runtime_seed",
        work_type=WorkType.PREPARE,
        input_fingerprint=work_input_fingerprint(
            work_type=WorkType.PREPARE,
            job_id=JOB_ID,
            work_request_id=WORK_ID,
        ),
        execution_name=deterministic_execution_name(WORK_ID),
        next_dispatch_at=NOW,
        created_at=NOW,
        updated_at=NOW,
    )
    seed_digest = sha256(b"phase6-runtime-seed").hexdigest()
    receipt = CommandReceipt(
        receipt_id=work.receipt_id,
        owner_id=OWNER_ID,
        job_id=JOB_ID,
        command_type="fixture_phase6_runtime",
        idempotency_key_digest=seed_digest,
        request_fingerprint=seed_digest,
        response=CommandResponse(
            job_id=JOB_ID,
            state=job.state,
            record_version=job.record_version,
            review_version=job.review_version,
            work_request_id=WORK_ID,
        ),
        work_request_id=WORK_ID,
        created_at=NOW,
    )
    source_material = _durable_source_material()
    source = SourceArtifactRecord(
        fingerprint=source_artifact_fingerprint(**source_material),
        **source_material,
    )
    store.create_job(
        job=job,
        event=DomainEvent(
            job_id=JOB_ID,
            sequence=job.event_sequence,
            name="INTAKE_VALIDATED",
            occurred_at=NOW,
        ),
        receipt=receipt,
        work_request=work,
        source_artifact=source,
    )
    claim_id = "claim_phase6_runtime"
    claimed = store.claim_work(
        JOB_ID,
        WORK_ID,
        now=NOW,
        claim_id=claim_id,
        lease_expires_at=NOW + timedelta(minutes=2),
    )
    assert claimed is not None
    dispatched = store.mark_work_dispatched(
        JOB_ID,
        WORK_ID,
        claim_id=claim_id,
        execution_arn=(
            "arn:aws:states:us-west-2:123456789012:execution:mr-lister-phase6:phase6-runtime"
        ),
        now=NOW,
    )
    worker = RecordingWorkerBoundary(WorkerControlService(store=store, clock=lambda: NOW))
    producer = StaticPreparedReviewProducer()
    adapter = WorkerControlPreparationAdapter(
        store=store,
        worker=worker,
        producer=producer,
    )
    runner = Phase6StrandsPreparationRunner(
        backend=Phase6PreparationBackend(store=store, service=adapter),
        model=Phase6ToolCallingModel(),
    )
    return runner, store, worker, producer, dispatched


def test_phase6_agentcore_orders_checkpoint_then_agent_evidence_and_final_route() -> None:
    _runner, store, service, model, audit = build_runtime()
    initial_job, initial_work = authority_records()
    session_id = f"mr-lister-phase6-{preparation_work_binding(initial_job, initial_work)}"
    client = TestClient(
        create_phase6_agentcore_runtime(
            backend=Phase6PreparationBackend(store=store, service=service),
            model=model,
            audit_sink=audit,
        )
    )

    response = client.post(
        "/invocations",
        headers={"X-Amzn-Bedrock-AgentCore-Runtime-Session-Id": session_id},
        json={"job_id": JOB_ID, "mode": "prepare", "instruction": "Prepare safely."},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["framework"] == "strands-agents"
    assert body["agent_id"] == "mr-lister-preparation"
    assert body["correlation_id"] == correlation_id(
        PreparationRequest(
            session_id=session_id,
            job_id=JOB_ID,
            mode="prepare",
            instruction="Prepare safely.",
        )
    )
    assert body["work_binding"] == preparation_work_binding(initial_job, initial_work)
    assert body["evidence_fingerprint"] == EVIDENCE_FINGERPRINT
    assert body["decision"]["next_action"] == "human_review"
    assert service.order == ["prepare_review", "complete_with_agent_decision"]

    [prepare] = service.prepare_calls
    assert prepare.expected_record_version == 2
    assert prepare.expected_review_version == 0
    [complete] = service.complete_calls
    assert complete.expected_record_version == 3
    assert complete.review_version == 1
    assert complete.tool_calls == ("record_prepared_review",)
    assert complete.cycles == 2
    assert complete.total_tokens == complete.input_tokens + complete.output_tokens
    assert complete.decision_fingerprint == canonical_fingerprint(
        complete.decision.model_dump(mode="json")
    )
    assert store.job.state is ControlJobState.PRODUCT_DRAFT_SYNCING
    assert store.work[WORK_ID].status is WorkRequestStatus.COMPLETED

    assert model.tool_result is not None
    tool_payload = json.loads(model.tool_result["content"][0]["text"])
    assert tool_payload["state"] == "listing_drafted"
    assert tool_payload["publication_authorized"] is False
    serialized = json.dumps(tool_payload)
    assert OWNER_ID not in serialized and JOB_ID not in serialized and WORK_ID not in serialized
    [record] = audit.records
    assert record.tool_calls == ("record_prepared_review",)


def test_real_worker_adapter_executes_the_frozen_three_command_sequence() -> None:
    runner, store, worker, producer, initial_work = durable_worker_runtime()
    initial_job = store.get_job(JOB_ID)

    result = runner(
        PreparationRequest(
            session_id="session_phase6_worker_adapter",
            job_id=JOB_ID,
            mode="prepare",
            instruction="Prepare safely.",
        )
    )

    assert worker.order == [
        "begin_preparation",
        "record_prepared_review",
        "complete_preparation_with_agent_decision",
    ]
    assert producer.calls == [(JOB_ID, WORK_ID)]
    begin, record, complete = worker.commands
    assert begin.expected_record_version == 0
    assert record.expected_record_version == 1
    assert complete.expected_record_version == 2
    assert complete.decision.next_action == "human_review"
    assert complete.tool_calls == ("record_prepared_review",)
    assert result.work_binding == preparation_work_binding(initial_job, initial_work)
    assert result.completion.state is ControlJobState.PRODUCT_DRAFT_SYNCING

    completed_job = store.get_job(JOB_ID)
    completed_work = store.get_work_request(JOB_ID, WORK_ID)
    assert completed_job.record_version == 3
    assert completed_job.review_version == 1
    assert completed_work.status is WorkRequestStatus.COMPLETED
    assert completed_job.agent_evidence_id is not None
    evidence = store.get_agent_evidence(JOB_ID, completed_job.agent_evidence_id)
    assert evidence.review_version == completed_job.review_version
    assert evidence.framework == "strands-agents"
    assert evidence.agent_id == "mr-lister-preparation"
    assert evidence.work_request_id == WORK_ID
    assert evidence.fingerprint == result.completion.evidence_fingerprint


@pytest.mark.parametrize(
    ("durable_checkpoint", "remaining_commands"),
    [
        (
            "analyzing_artwork",
            ["record_prepared_review", "complete_preparation_with_agent_decision"],
        ),
        ("listing_drafted", ["complete_preparation_with_agent_decision"]),
    ],
)
def test_real_worker_adapter_resumes_only_uncommitted_commands(
    durable_checkpoint: str,
    remaining_commands: list[str],
) -> None:
    runner, store, worker, producer, initial_work = durable_worker_runtime()
    begun = worker.begin_preparation(
        BeginPreparationCommand(
            job_id=JOB_ID,
            work_request_id=WORK_ID,
            expected_record_version=0,
        )
    )
    if durable_checkpoint == "listing_drafted":
        observation = producer.prepare_review(JOB_ID, WORK_ID)
        worker.record_prepared_review(
            RecordPreparedReviewCommand(
                job_id=JOB_ID,
                work_request_id=WORK_ID,
                expected_record_version=begun.record_version,
                source_artifact_fingerprint=observation.source_artifact_fingerprint,
                artwork_analysis=observation.artwork_analysis,
                listing=observation.listing,
                product_profile_fingerprint=observation.product_profile_fingerprint,
            )
        )
    worker.order.clear()
    worker.commands.clear()
    producer.calls.clear()

    result = runner(
        PreparationRequest(
            session_id=f"session_phase6_resume_{durable_checkpoint}",
            job_id=JOB_ID,
            mode="prepare",
            instruction="Resume safely.",
        )
    )

    assert worker.order == remaining_commands
    assert len(producer.calls) == (1 if durable_checkpoint == "analyzing_artwork" else 0)
    assert result.completion.state is ControlJobState.PRODUCT_DRAFT_SYNCING
    assert store.get_work_request(JOB_ID, initial_work.work_request_id).status is (
        WorkRequestStatus.COMPLETED
    )


def test_tool_failure_never_runs_agent_completion_or_returns_success() -> None:
    marker = "DO_NOT_ECHO_DATABASE_OR_MODEL_SECRET"
    _runner, store, service, model, audit = build_runtime(prepare_error=RuntimeError(marker))
    client = TestClient(
        create_phase6_agentcore_runtime(
            backend=Phase6PreparationBackend(store=store, service=service),
            model=model,
            audit_sink=audit,
        )
    )
    job, work = authority_records()
    session_id = f"mr-lister-phase6-{preparation_work_binding(job, work)}"

    response = client.post(
        "/invocations",
        headers={"X-Amzn-Bedrock-AgentCore-Runtime-Session-Id": session_id},
        json={"job_id": JOB_ID, "mode": "prepare", "instruction": "Prepare safely."},
    )

    assert response.status_code == 502
    assert marker not in response.text
    assert len(service.prepare_calls) == 1
    assert service.complete_calls == []
    assert store.job.record_version == 2
    assert audit.records[-1].status == "failed"


def test_agentcore_rejects_wrong_opaque_session_before_strands_or_tool() -> None:
    _runner, store, service, model, audit = build_runtime()
    client = TestClient(
        create_phase6_agentcore_runtime(
            backend=Phase6PreparationBackend(store=store, service=service),
            model=model,
            audit_sink=audit,
        )
    )

    response = client.post(
        "/invocations",
        headers={"X-Amzn-Bedrock-AgentCore-Runtime-Session-Id": f"mr-lister-phase6-{'f' * 64}"},
        json={"job_id": JOB_ID, "mode": "prepare", "instruction": "Prepare safely."},
    )

    assert response.status_code == 502
    assert response.json()["error"]["code"] == "AGENT_EXECUTION_FAILED"
    assert service.order == []
    assert model.stream_calls == 0
    assert audit.records == []


def test_model_cannot_route_against_checkpoint_validation() -> None:
    model = Phase6ToolCallingModel(forced_action="revise")
    runner, _store, service, _model, audit = build_runtime(model=model)

    with pytest.raises(AgentExecutionError, match="did not match"):
        runner(
            PreparationRequest(
                session_id="session_phase6_strands_003",
                job_id=JOB_ID,
                mode="prepare",
                instruction="Prepare safely.",
            )
        )

    assert service.order == ["prepare_review"]
    assert service.complete_calls == []
    assert audit.records[-1].status == "failed"


def test_model_cannot_bypass_real_phase6_tool() -> None:
    model = Phase6ToolCallingModel(skip_prepare=True)
    runner, _store, service, _model, audit = build_runtime(model=model)
    with pytest.raises(AgentExecutionError, match="was not executed exactly"):
        runner(
            PreparationRequest(
                session_id="session_phase6_no_bypass",
                job_id=JOB_ID,
                mode="prepare",
                instruction="Skip the tool.",
            )
        )
    assert service.order == []
    assert audit.records[-1].status == "failed"


def test_backend_rejects_mismatched_prepared_checkpoint_identity() -> None:
    wrong = Phase6PreparedReviewCheckpoint(
        owner_id="f" * 64,
        job_id=JOB_ID,
        work_request_id=WORK_ID,
        state=ControlJobState.LISTING_DRAFTED,
        record_version=3,
        review_version=1,
        review_fingerprint=REVIEW_FINGERPRINT,
        validation_passed=True,
    )
    _runner, _store, service, _model, _audit = build_runtime(checkpoint_override=wrong)
    with pytest.raises(PreparationAuthorityError, match="did not match"):
        Phase6PreparationBackend(store=service.store, service=service).prepare_review(JOB_ID)


def test_phase6_runtime_rejects_non_prepare_mode_before_model_or_service() -> None:
    runner, _store, service, model, audit = build_runtime()
    with pytest.raises(AgentExecutionError, match="requires prepare mode"):
        runner(
            PreparationRequest(
                session_id="session_phase6_strands_004",
                job_id=JOB_ID,
                mode="review",
                instruction="Expand the runtime capability.",
            )
        )
    assert service.order == []
    assert model.stream_calls == 0
    assert audit.records[-1].status == "failed"
