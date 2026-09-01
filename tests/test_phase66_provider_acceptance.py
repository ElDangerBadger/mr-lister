from __future__ import annotations

import base64
import json
import os
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path
from typing import Any

import pytest

from mr_lister.acceptance.phase6 import (
    AcceptanceEvidenceClass,
    ArtifactFormat,
    ArtifactKind,
    phase66_acceptance_manifest,
    phase66_manifest_digest,
    validate_phase66_evidence,
)
from mr_lister.contracts import ArtworkAnalysis
from mr_lister.control.fingerprints import (
    canonical_fingerprint,
    product_sync_record_fingerprint,
)
from mr_lister.control.models import (
    AgentPreparationEvidence,
    ArtworkAnalysisRecord,
    CancellationDecisionRecord,
    CommandReceipt,
    CommandResponse,
    ControlJobRecord,
    ControlJobState,
    ProductSyncRecord,
    ProductVariantEvidence,
    ProviderCallPermit,
    ProviderCallPermitStatus,
    ReviewDecision,
    ReviewDecisionRecord,
    SourceArtifactRecord,
    WorkRequest,
    WorkRequestStatus,
    WorkType,
)
from mr_lister.production.provider_resources import ProviderRequestAuditRecord
from tools import capture_phase66_agentcore_deployment_authority as agentcore_capture
from tools import capture_phase66_deployment_authority as deployment_capture
from tools import phase66_provider_acceptance as provider
from tools import prepare_phase66_provider_evidence as provider_cli

DEPLOYED_AT = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
AGENTCORE_AT = DEPLOYED_AT + timedelta(minutes=1)
PREREQUISITE_AT = DEPLOYED_AT + timedelta(minutes=2)
GATE_ISSUED_AT = DEPLOYED_AT + timedelta(minutes=3)
OBSERVED_AT = DEPLOYED_AT + timedelta(minutes=4)
GATE_EXPIRES_AT = GATE_ISSUED_AT + timedelta(hours=1)
OWNER = "a" * 64
JOB_ID = "job_provider_acceptance"
PRODUCT_ID = "product_provider_acceptance"
IMAGE_ID = "image_provider_acceptance"
SOURCE_COMMIT_DIGEST = sha256(("f" * 40).encode("ascii")).hexdigest()


def _digest(label: str) -> str:
    return sha256(label.encode("ascii")).hexdigest()


def _timestamp(value: datetime) -> str:
    return value.strftime("%Y-%m-%dT%H:%M:%SZ")


def _write(path: Path, value: object) -> str:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.parent.chmod(0o700)
    payload = provider._canonical(value, pretty=True)
    path.write_bytes(payload)
    path.chmod(0o600)
    return sha256(payload).hexdigest()


def _deployment_document() -> dict[str, object]:
    closure_release_digest = sha256(
        agentcore_capture.EXPECTED_CLOSURE_RELEASE_FINGERPRINT.encode("ascii")
    ).hexdigest()
    predecessor_digest = _digest("predecessor-release")
    lambdas = [
        {
            "code_sha256": _digest(f"code-{logical_id}"),
            "configuration_digest": _digest(f"configuration-{logical_id}"),
            "last_update_status": "Successful",
            "logical_id": logical_id,
            "release_fingerprint_digest": (
                closure_release_digest
                if logical_id in provider._CLOSURE_FUNCTIONS
                else predecessor_digest
            ),
            "state": "Active",
        }
        for logical_id in deployment_capture._FUNCTION_LOGICAL_IDS
    ]
    authority = {
        "account_binding_digest": _digest("account"),
        "cognito": {"configuration_digest": _digest("cognito")},
        "lambdas": lambdas,
        "readiness": deployment_capture.EXPECTED_READINESS,
        "region": deployment_capture.EXPECTED_REGION,
        "source_commit_digest": SOURCE_COMMIT_DIGEST,
        "stack": {"stack_status": "UPDATE_COMPLETE"},
        "stack_name": deployment_capture.EXPECTED_STACK_NAME,
        "web_edge": {"health_passed": True},
    }
    return {
        "authority": authority,
        "captured_at": _timestamp(DEPLOYED_AT),
        "deployment_digest": provider._digest(authority),
        "format": deployment_capture.FORMAT,
    }


def _agentcore_document() -> dict[str, object]:
    release = agentcore_capture.EXPECTED_CLOSURE_RELEASE_FINGERPRINT
    runtime_arn = agentcore_capture.EXPECTED_RUNTIME_ARN
    endpoint_arn = agentcore_capture.EXPECTED_ENDPOINT_ARN
    qualifier = agentcore_capture.EXPECTED_RUNTIME_QUALIFIER
    runtime_version = agentcore_capture.EXPECTED_RUNTIME_VERSION
    binding_fingerprint = agentcore_capture.EXPECTED_RUNTIME_BINDING_FINGERPRINT
    dispatcher_digest = agentcore_capture._digest(
        {
            "MR_LISTER_AGENTCORE_RUNTIME_ARN": runtime_arn,
            "MR_LISTER_AGENTCORE_RUNTIME_BINDING_FINGERPRINT": binding_fingerprint,
            "MR_LISTER_AGENTCORE_RUNTIME_ENDPOINT_ARN": endpoint_arn,
            "MR_LISTER_AGENTCORE_RUNTIME_QUALIFIER": qualifier,
            "MR_LISTER_AGENTCORE_RUNTIME_VERSION": runtime_version,
            "MR_LISTER_RELEASE_FINGERPRINT": release,
        }
    )
    archive_sha256 = agentcore_capture.EXPECTED_AGENTCORE_ARCHIVE_SHA256
    bucket = (
        "mr-lister-phase6-artifacts-dev-"
        f"{agentcore_capture.EXPECTED_ACCOUNT_ID}-{agentcore_capture.EXPECTED_REGION}"
    )
    authority = {
        "account_id": agentcore_capture.EXPECTED_ACCOUNT_ID,
        "artifact": {
            "archive_sha256": archive_sha256,
            "bucket": bucket,
            "checksum_sha256_base64": base64.b64encode(bytes.fromhex(archive_sha256)).decode(
                "ascii"
            ),
            "key": (
                f"private/deployments/agentcore/releases/{release}/"
                f"phase6-agentcore-{archive_sha256}.zip"
            ),
            "size_bytes": agentcore_capture.EXPECTED_AGENTCORE_ARCHIVE_SIZE,
            "version_id": "B64_bDuTGgc2a4K1PrLNWdSBqOeJpOo6",
        },
        "deployment_readiness": agentcore_capture.EXPECTED_READINESS,
        "endpoint": {
            "arn": endpoint_arn,
            "live_version": runtime_version,
            "name": qualifier,
            "status": "READY",
            "target_version": runtime_version,
        },
        "environment": agentcore_capture.EXPECTED_ENVIRONMENT,
        "preparation_dispatch_binding_digest": dispatcher_digest,
        "region": agentcore_capture.EXPECTED_REGION,
        "release_topology": {
            "agentcore_release_fingerprint": release,
            "artwork_closure_release_fingerprint": release,
            "mode": agentcore_capture.EXPECTED_RELEASE_TOPOLOGY,
            "preparation_dispatch_override": "EXPLICIT_RESOURCE_LEVEL",
            "preparation_dispatch_release_fingerprint": release,
            "shared_global_release_fingerprint": (
                agentcore_capture.EXPECTED_SHARED_RELEASE_FINGERPRINT
            ),
        },
        "runtime": {
            "arn": runtime_arn,
            "configuration_digest": agentcore_capture._digest(
                agentcore_capture._expected_runtime_configuration()
            ),
            "id": agentcore_capture.EXPECTED_RUNTIME_ID,
            "name": agentcore_capture.EXPECTED_RUNTIME_NAME,
            "role_arn": agentcore_capture.EXPECTED_RUNTIME_ROLE_ARN,
            "status": "READY",
            "version": runtime_version,
        },
        "runtime_binding_fingerprint": binding_fingerprint,
        "stack_name": agentcore_capture.EXPECTED_STACK_NAME,
        "stack_status": "UPDATE_COMPLETE",
    }
    return {
        "authority": authority,
        "authority_digest": agentcore_capture._digest(authority),
        "captured_at": _timestamp(AGENTCORE_AT),
        "format": agentcore_capture.FORMAT,
    }


def _artifact(kind: ArtifactKind, label: str) -> dict[str, object]:
    return {
        "artifact_digest": _digest(label),
        "artifact_format": ArtifactFormat.JSON.value,
        "byte_count": 1,
        "kind": kind.value,
        "redaction_verified": True,
    }


def _provider_summary(gate_id: str) -> dict[str, object]:
    if gate_id == "provider.primary_same_job_canary":
        return {
            "artwork_upload_count": 1,
            "product_post_count": 1,
            "product_put_count": 2,
            "product_get_count": 1,
            "forbidden_attempt_count": 0,
            "publish_attempt_count": 0,
            "order_attempt_count": 0,
            "fulfillment_attempt_count": 0,
            "final_state": "unpublished_unlocked",
        }
    raise AssertionError(gate_id)


def _prerequisite_record(gate_id: str, deployment_digest: str) -> dict[str, object]:
    gate = next(item for item in phase66_acceptance_manifest().gates if item.gate_id == gate_id)
    record: dict[str, object] = {
        "schema_version": "6.6.0",
        "manifest_digest": phase66_manifest_digest(),
        "run_digest": _digest(f"prerequisite-run-{gate_id}"),
        "source_commit_digest": SOURCE_COMMIT_DIGEST,
        "gate_id": gate_id,
        "evidence_class": gate.evidence_class.value,
        "outcome": "passed",
        "recorded_at": _timestamp(PREREQUISITE_AT),
        "job_digest": _digest(f"prerequisite-job-{gate_id}"),
        "work_digest": None,
        "correlation_digest": None,
        "assertions": [
            {
                "assertion_id": assertion_id,
                "passed": True,
                "observation_digest": _digest(f"{gate_id}-{assertion_id}"),
            }
            for assertion_id in gate.required_assertions
        ],
        "artifacts": [
            _artifact(kind, f"{gate_id}-{kind.value}") for kind in gate.required_artifact_kinds
        ],
        "privacy": {
            "sanitizer_contract": "phase6.6-sanitized-evidence-v1",
            "forbidden_field_match_count": 0,
            "sensitive_value_match_count": 0,
            "free_text_value_count": 0,
        },
        "deployment_digest": deployment_digest,
        "actor_digests": [_digest(f"actor-{gate_id}")],
        "provider_gate_attestation": None,
        "provider_call_summary": None,
        "moderated_session": None,
    }
    if gate_id == "deployed.edge_auth_owner_smoke":
        record["actor_digests"] = [_digest("edge-a"), _digest("edge-b")]
    if gate.evidence_class is AcceptanceEvidenceClass.PROVIDER_DESTRUCTIVE:
        record.update(
            {
                "work_digest": _digest("prerequisite-primary-work"),
                "correlation_digest": _digest("prerequisite-primary-correlation"),
                "provider_gate_attestation": {
                    "run_gate_digest": _digest("prerequisite-primary-run-gate"),
                    "provider_write_gate_digest": _digest("prerequisite-primary-write-gate"),
                    "approved_scope": "unpublished_draft_create_update_only",
                    "root_credentials_rejected": True,
                    "publication_capability_absent": True,
                    "approved_max_product_posts": 1,
                    "approved_max_product_puts": 2,
                },
                "provider_call_summary": _provider_summary(gate_id),
            }
        )
    validate_phase66_evidence(record)
    return record


def _required_prerequisites(gate_id: str, deployment_digest: str) -> list[dict[str, object]]:
    gate = next(item for item in phase66_acceptance_manifest().gates if item.gate_id == gate_id)
    return [_prerequisite_record(name, deployment_digest) for name in gate.prerequisites]


@dataclass(frozen=True)
class Authorities:
    deployment_path: Path
    deployment_sha256: str
    agentcore_path: Path
    agentcore_sha256: str
    prerequisite_path: Path
    prerequisite_sha256: str
    run_gate_path: Path
    run_gate_sha256: str
    write_gate_path: Path
    write_gate_sha256: str

    def arguments(self) -> dict[str, object]:
        return {
            "deployment_authority_path": self.deployment_path,
            "deployment_authority_sha256": self.deployment_sha256,
            "agentcore_authority_path": self.agentcore_path,
            "agentcore_authority_sha256": self.agentcore_sha256,
            "prerequisite_records_path": self.prerequisite_path,
            "prerequisite_records_sha256": self.prerequisite_sha256,
            "run_gate_path": self.run_gate_path,
            "run_gate_sha256": self.run_gate_sha256,
            "provider_write_gate_path": self.write_gate_path,
            "provider_write_gate_sha256": self.write_gate_sha256,
        }


def _authority_files(
    private: Path,
    gate_id: str,
    *,
    same_nonce: bool = False,
) -> Authorities:
    deployment = _deployment_document()
    deployment_path = private / gate_id / "deployment.json"
    deployment_sha = _write(deployment_path, deployment)
    agentcore_path = private / gate_id / "agentcore.json"
    agentcore_sha = _write(agentcore_path, _agentcore_document())
    prerequisites = _required_prerequisites(gate_id, str(deployment["deployment_digest"]))
    prerequisite_path = private / gate_id / "prerequisites.json"
    prerequisite_sha = _write(prerequisite_path, prerequisites)
    prerequisite_digests = [record["run_digest"] for record in prerequisites]
    limits = provider._EXPECTED_LIMITS[gate_id]

    def gate(kind: str, nonce_label: str) -> dict[str, object]:
        return {
            "format": provider.GATE_AUTHORITY_FORMAT,
            "gate_id": gate_id,
            "gate_kind": kind,
            "issued_at": _timestamp(GATE_ISSUED_AT),
            "expires_at": _timestamp(GATE_EXPIRES_AT),
            "deployment_digest": deployment["deployment_digest"],
            "deployment_authority_sha256": deployment_sha,
            "agentcore_authority_digest": _agentcore_document()["authority_digest"],
            "agentcore_authority_sha256": agentcore_sha,
            "source_commit_digest": SOURCE_COMMIT_DIGEST,
            "principal_digest": provider._EXPECTED_CALLER_DIGEST,
            "prerequisite_run_digests": prerequisite_digests,
            "approved_scope": "unpublished_draft_create_update_only",
            "approved_max_artwork_uploads": limits[0],
            "approved_max_product_posts": limits[1],
            "approved_max_product_puts": limits[2],
            "root_credentials_rejected": True,
            "publication_capability_absent": True,
            "nonce_digest": _digest(nonce_label),
        }

    run_gate_path = private / gate_id / "run-gate.json"
    run_gate_sha = _write(run_gate_path, gate("run_gate", "run-nonce"))
    write_gate_path = private / gate_id / "write-gate.json"
    write_gate_sha = _write(
        write_gate_path,
        gate("provider_write_gate", "run-nonce" if same_nonce else "write-nonce"),
    )
    return Authorities(
        deployment_path,
        deployment_sha,
        agentcore_path,
        agentcore_sha,
        prerequisite_path,
        prerequisite_sha,
        run_gate_path,
        run_gate_sha,
        write_gate_path,
        write_gate_sha,
    )


def _work(work_id: str, work_type: WorkType, *, status: WorkRequestStatus) -> WorkRequest:
    return WorkRequest(
        work_request_id=work_id,
        owner_id=OWNER,
        job_id=JOB_ID,
        receipt_id=f"receipt_{work_id}",
        work_type=work_type,
        review_version=None if work_type is WorkType.PREPARE else 2,
        input_fingerprint=_digest(f"input-{work_id}"),
        execution_name=f"execution_{work_id}",
        status=status,
        attempt_count=1,
        next_dispatch_at=OBSERVED_AT - timedelta(minutes=2),
        created_at=OBSERVED_AT - timedelta(minutes=3),
        updated_at=OBSERVED_AT - timedelta(minutes=1),
    )


def _analysis(work_id: str) -> ArtworkAnalysisRecord:
    analysis = ArtworkAnalysis(
        subject="A geometric badger",
        visual_elements=("compass", "pine trees"),
        styles=("low poly",),
        themes=("woodland adventure",),
        confidence=0.94,
    )
    material = {
        "job_id": JOB_ID,
        "source_artifact_fingerprint": _digest("source-artifact"),
        "analysis": analysis.model_dump(mode="json"),
    }
    fingerprint = canonical_fingerprint(material)
    return ArtworkAnalysisRecord(
        analysis_id="analysis_provider_acceptance",
        job_id=JOB_ID,
        work_request_id=work_id,
        source_artifact_fingerprint=_digest("source-artifact"),
        fingerprint=fingerprint,
        analysis=analysis,
        created_at=OBSERVED_AT - timedelta(minutes=2),
    )


def _source(size_bytes: int) -> SourceArtifactRecord:
    return SourceArtifactRecord(
        job_id=JOB_ID,
        owner_id=OWNER,
        fingerprint=_digest("source-artifact"),
        bucket="private-artifact-bucket",
        object_key=f"private/owners/{OWNER}/jobs/{JOB_ID}/source/source.png",
        version_id="source-version",
        content_sha256=_digest(f"source-content-{size_bytes}"),
        size_bytes=size_bytes,
        media_type="image/png",
        product_profile_id="gildan_64000_swiftpod",
        product_profile_version=2,
        product_profile_fingerprint=_digest("product-profile"),
        created_at=OBSERVED_AT - timedelta(minutes=5),
    )


def _agent(work_id: str) -> AgentPreparationEvidence:
    evidence = AgentPreparationEvidence(
        evidence_id="evidence_provider_acceptance",
        job_id=JOB_ID,
        work_request_id=work_id,
        review_version=1,
        correlation_id="b" * 24,
        framework="strands-agents",
        agent_id="mr-lister-preparation",
        controller_model_id="us.amazon.nova-2-lite-v1:0",
        tool_calls=("record_prepared_review",),
        cycles=1,
        input_tokens=500,
        output_tokens=100,
        total_tokens=600,
        decision_fingerprint=_digest("agent-decision"),
        fingerprint="0" * 64,
        requires_human_approval=True,
        publication_authorized=False,
        created_at=OBSERVED_AT - timedelta(minutes=2),
    )
    return evidence.model_copy(update={"fingerprint": evidence.authority_fingerprint})


def _sync(review_version: int) -> ProductSyncRecord:
    sync = ProductSyncRecord(
        sync_id=f"sync_provider_{review_version}",
        job_id=JOB_ID,
        review_version=review_version,
        product_id=PRODUCT_ID,
        image_id=IMAGE_ID,
        printify_shop_id=42,
        payload_fingerprint=_digest(f"payload-{review_version}"),
        response_fingerprint=_digest(f"response-{review_version}"),
        fingerprint="0" * 64,
        variants=(
            ProductVariantEvidence(
                variant_id=1000,
                color="Black",
                size="M",
                placement_group_id="front",
                retail_price_cents=2999,
                production_cost_cents=1200,
            ),
        ),
        synchronized_at=OBSERVED_AT - timedelta(seconds=10),
    )
    return sync.model_copy(update={"fingerprint": product_sync_record_fingerprint(sync)})


def _job(
    state: ControlJobState,
    *,
    analysis: ArtworkAnalysisRecord | None = None,
    agent: AgentPreparationEvidence | None = None,
    sync: ProductSyncRecord | None = None,
) -> ControlJobRecord:
    review_version = sync.review_version if sync is not None else 0
    review_fingerprint = _digest(f"review-{review_version}") if review_version else None
    values: dict[str, Any] = {
        "owner_id": OWNER,
        "job_id": JOB_ID,
        "record_version": 12,
        "event_sequence": 12,
        "state": state,
        "review_version": review_version,
        "review_fingerprint": review_fingerprint,
        "review_validated": bool(review_version),
        "source_artifact_fingerprint": _digest("source-artifact"),
        "artwork_analysis_id": analysis.analysis_id if analysis else None,
        "artwork_analysis_fingerprint": analysis.fingerprint if analysis else None,
        "agent_evidence_id": agent.evidence_id if agent else None,
        "agent_evidence_fingerprint": agent.fingerprint if agent else None,
        "product_id": sync.product_id if sync else None,
        "provider_payload_fingerprint": sync.payload_fingerprint if sync else None,
        "product_sync_id": sync.sync_id if sync else None,
        "synchronized_review_version": sync.review_version if sync else None,
        "product_sync_fingerprint": sync.fingerprint if sync else None,
        "pricing_snapshot_id": "pricing_provider" if sync else None,
        "pricing_snapshot_fingerprint": _digest("pricing") if sync else None,
        "provider_upload_attempt_id": "attempt_upload" if sync else None,
        "uploaded_artwork_id": "uploaded_artwork" if sync else None,
        "uploaded_image_id": IMAGE_ID if sync else None,
        "uploaded_artwork_fingerprint": _digest("uploaded-artwork") if sync else None,
        "provider_write_attempt_id": "attempt_put_2" if sync else None,
        "product_create_attempt_id": "attempt_product_post" if sync else None,
        "created_at": OBSERVED_AT - timedelta(minutes=10),
        "updated_at": OBSERVED_AT,
    }
    if state is ControlJobState.APPROVED:
        values.update(
            {
                "approval_decision_id": "decision_approved",
                "approved_review_version": review_version,
                "approved_review_fingerprint": review_fingerprint,
                "approval_fingerprint": _digest("approval"),
            }
        )
    if state is ControlJobState.CANCELLED:
        values["cancellation_requested_at"] = OBSERVED_AT - timedelta(seconds=30)
    return ControlJobRecord(**values)


def _permit(attempt_id: str, work_id: str, *, retired: bool = False) -> ProviderCallPermit:
    return ProviderCallPermit(
        attempt_id=attempt_id,
        job_id=JOB_ID,
        work_request_id=work_id,
        status=(ProviderCallPermitStatus.RETIRED if retired else ProviderCallPermitStatus.CONSUMED),
        created_at=OBSERVED_AT - timedelta(minutes=2),
        consumed_at=None if retired else OBSERVED_AT - timedelta(minutes=1),
        consumed_work_request_id=None if retired else work_id,
        retired_at=OBSERVED_AT - timedelta(minutes=1) if retired else None,
    )


def _command_evidence(
    name: provider.ProviderCommandName,
    job: ControlJobRecord,
    *,
    work_id: str | None = None,
) -> tuple[CommandReceipt, ReviewDecisionRecord | CancellationDecisionRecord]:
    command_type = {
        provider.ProviderCommandName.REVISE: "revise_listing",
        provider.ProviderCommandName.APPROVE: "approve_review",
        provider.ProviderCommandName.CANCEL: "cancel_job",
    }[name]
    receipt_id = f"receipt_{name.value}"
    receipt = CommandReceipt(
        receipt_id=receipt_id,
        owner_id=OWNER,
        job_id=JOB_ID,
        command_type=command_type,
        idempotency_key_digest=_digest(f"idempotency-{name.value}"),
        request_fingerprint=_digest(f"request-{name.value}"),
        response=CommandResponse(
            job_id=JOB_ID,
            state=job.state,
            record_version=job.record_version,
            review_version=job.review_version,
            work_request_id=work_id,
        ),
        work_request_id=work_id,
        created_at=OBSERVED_AT - timedelta(seconds=5),
    )
    if name is provider.ProviderCommandName.CANCEL:
        decision: ReviewDecisionRecord | CancellationDecisionRecord = CancellationDecisionRecord(
            decision_id="decision_cancel",
            job_id=JOB_ID,
            actor_owner_id=OWNER,
            expected_record_version=0,
            review_version=job.review_version or None,
            review_fingerprint=job.review_fingerprint,
            command_receipt_id=receipt_id,
            decided_at=OBSERVED_AT - timedelta(seconds=5),
        )
    else:
        review_decision = (
            ReviewDecision.REVISE
            if name is provider.ProviderCommandName.REVISE
            else ReviewDecision.APPROVE
        )
        decision = ReviewDecisionRecord(
            decision_id=(
                str(job.approval_decision_id)
                if name is provider.ProviderCommandName.APPROVE
                else "decision_revise"
            ),
            job_id=JOB_ID,
            actor_owner_id=OWNER,
            decision=review_decision,
            review_version=(job.review_version if review_decision is ReviewDecision.APPROVE else 1),
            review_fingerprint=(
                str(job.review_fingerprint)
                if review_decision is ReviewDecision.APPROVE
                else _digest("review-1")
            ),
            approval_fingerprint=(
                job.approval_fingerprint if review_decision is ReviewDecision.APPROVE else None
            ),
            command_receipt_id=receipt_id,
            decided_at=OBSERVED_AT - timedelta(seconds=5),
        )
    return receipt, decision


def _call(
    ordinal: int,
    method: str,
    path: str,
    *,
    attempt_id: str | None = None,
    work_id: str | None = None,
    product_id: str | None = None,
) -> provider.ProviderLiveCall:
    return provider.ProviderLiveCall(
        ordinal=ordinal,
        audit=ProviderRequestAuditRecord(method=method, path=path),
        status_code=201 if method == "POST" else 200,
        attempt_id=attempt_id,
        work_request_id=work_id,
        product_id=product_id,
    )


def _primary_result() -> provider.ProviderLiveResult:
    prepare = _work("work_prepare", WorkType.PREPARE, status=WorkRequestStatus.COMPLETED)
    sync_work = _work(
        "work_sync_create", WorkType.SYNCHRONIZE_PRODUCT, status=WorkRequestStatus.COMPLETED
    )
    update_one = _work(
        "work_sync_update_1", WorkType.SYNCHRONIZE_PRODUCT, status=WorkRequestStatus.COMPLETED
    )
    update_two = _work(
        "work_sync_update_2", WorkType.SYNCHRONIZE_PRODUCT, status=WorkRequestStatus.COMPLETED
    )
    analysis = _analysis(prepare.work_request_id)
    agent = _agent(prepare.work_request_id)
    sync = _sync(2)
    calls = (
        _call(
            1,
            "POST",
            "/v1/uploads/images.json",
            attempt_id="attempt_upload",
            work_id=sync_work.work_request_id,
        ),
        _call(
            2,
            "POST",
            "/v1/shops/{shop_id}/products.json",
            attempt_id="attempt_product_post",
            work_id=sync_work.work_request_id,
            product_id=PRODUCT_ID,
        ),
        _call(
            3,
            "PUT",
            "/v1/shops/{shop_id}/products/{product_id}.json",
            attempt_id="attempt_put_1",
            work_id=update_one.work_request_id,
            product_id=PRODUCT_ID,
        ),
        _call(
            4,
            "PUT",
            "/v1/shops/{shop_id}/products/{product_id}.json",
            attempt_id="attempt_put_2",
            work_id=update_two.work_request_id,
            product_id=PRODUCT_ID,
        ),
        _call(
            5,
            "GET",
            "/v1/shops/{shop_id}/products/{product_id}.json",
            product_id=PRODUCT_ID,
        ),
    )
    permits = (
        _permit("attempt_upload", sync_work.work_request_id),
        _permit("attempt_product_post", sync_work.work_request_id),
        _permit("attempt_put_1", update_one.work_request_id),
        _permit("attempt_put_2", update_two.work_request_id),
    )
    job = _job(ControlJobState.APPROVED, analysis=analysis, agent=agent, sync=sync)
    receipt, decision = _command_evidence(provider.ProviderCommandName.APPROVE, job)
    assert isinstance(decision, ReviewDecisionRecord)
    return provider.ProviderLiveResult(
        gate_id="provider.primary_same_job_canary",
        observed_at=OBSERVED_AT,
        aws_caller_arn=agentcore_capture.EXPECTED_CALLER_ARN,
        seller_actor_owner_id=OWNER,
        seller_command_channel=True,
        job=job,
        source_artifact=_source(provider.PRIMARY_SOURCE_BYTES),
        work_requests=(prepare, sync_work, update_one, update_two),
        artwork_analysis=analysis,
        agent_evidence=agent,
        agentcore_correlation_id=agent.correlation_id,
        gemma_inference_count=1,
        product_sync=sync,
        final_product=provider.FinalProductObservation(
            product_id=PRODUCT_ID,
            payload_fingerprint=sync.payload_fingerprint,
            provider_locked=False,
            provider_published=False,
        ),
        provider_call_permits=permits,
        provider_calls=calls,
        command_outcomes=(),
        command_receipts=(receipt,),
        review_decisions=(decision,),
        cancellation_decisions=(),
        source_size_bytes=provider.PRIMARY_SOURCE_BYTES,
        running_execution_count=0,
        administrator_action_count=0,
        root_action_count=0,
        publication_attempt_count=0,
        order_attempt_count=0,
        fulfillment_attempt_count=0,
        work_inventory_complete=True,
        permit_inventory_complete=True,
        execution_inventory_complete=True,
        command_inventory_complete=True,
    )


def _concurrency_result(*, put_count: int = 1) -> provider.ProviderLiveResult:
    setup = _work(
        "work_sync_create", WorkType.SYNCHRONIZE_PRODUCT, status=WorkRequestStatus.COMPLETED
    )
    update = _work(
        "work_sync_update_1", WorkType.SYNCHRONIZE_PRODUCT, status=WorkRequestStatus.COMPLETED
    )
    sync = _sync(2)
    calls = [
        _call(
            1,
            "POST",
            "/v1/uploads/images.json",
            attempt_id="attempt_upload",
            work_id=setup.work_request_id,
        ),
        _call(
            2,
            "POST",
            "/v1/shops/{shop_id}/products.json",
            attempt_id="attempt_product_post",
            work_id=setup.work_request_id,
            product_id=PRODUCT_ID,
        ),
    ]
    permits = [
        _permit("attempt_upload", setup.work_request_id),
        _permit("attempt_product_post", setup.work_request_id),
    ]
    for index in range(put_count):
        attempt = f"attempt_put_{index + 1}"
        calls.append(
            _call(
                len(calls) + 1,
                "PUT",
                "/v1/shops/{shop_id}/products/{product_id}.json",
                attempt_id=attempt,
                work_id=update.work_request_id,
                product_id=PRODUCT_ID,
            )
        )
        permits.append(_permit(attempt, update.work_request_id))
    calls.append(
        _call(
            len(calls) + 1,
            "GET",
            "/v1/shops/{shop_id}/products/{product_id}.json",
            product_id=PRODUCT_ID,
        )
    )
    job = _job(ControlJobState.AWAITING_APPROVAL, sync=sync)
    receipt, decision = _command_evidence(
        provider.ProviderCommandName.REVISE,
        job,
        work_id=update.work_request_id,
    )
    assert isinstance(decision, ReviewDecisionRecord)
    return provider.ProviderLiveResult(
        gate_id="provider.concurrency_canary",
        observed_at=OBSERVED_AT,
        aws_caller_arn=agentcore_capture.EXPECTED_CALLER_ARN,
        seller_actor_owner_id=OWNER,
        seller_command_channel=True,
        job=job,
        source_artifact=_source(1024),
        work_requests=(setup, update),
        product_sync=sync,
        final_product=provider.FinalProductObservation(
            product_id=PRODUCT_ID,
            payload_fingerprint=sync.payload_fingerprint,
            provider_locked=False,
            provider_published=False,
        ),
        provider_call_permits=tuple(permits),
        provider_calls=tuple(calls),
        command_outcomes=(
            provider.CommandRaceObservation(
                name="revise", outcome="won", actor_owner_id=OWNER, expected_record_version=7
            ),
            provider.CommandRaceObservation(
                name="approve", outcome="conflict", actor_owner_id=OWNER, expected_record_version=7
            ),
            provider.CommandRaceObservation(
                name="cancel", outcome="conflict", actor_owner_id=OWNER, expected_record_version=7
            ),
        ),
        command_receipts=(receipt,),
        review_decisions=(decision,),
        cancellation_decisions=(),
        source_size_bytes=1024,
        running_execution_count=0,
        administrator_action_count=0,
        root_action_count=0,
        publication_attempt_count=0,
        order_attempt_count=0,
        fulfillment_attempt_count=0,
        work_inventory_complete=True,
        permit_inventory_complete=True,
        execution_inventory_complete=True,
        command_inventory_complete=True,
    )


def _cancellation_result(*, running: int = 0) -> provider.ProviderLiveResult:
    cancelled_work = _work(
        "work_cancelled",
        WorkType.PREPARE,
        status=WorkRequestStatus.CANCELLED,
    )
    job = _job(ControlJobState.CANCELLED)
    receipt, decision = _command_evidence(provider.ProviderCommandName.CANCEL, job)
    assert isinstance(decision, CancellationDecisionRecord)
    return provider.ProviderLiveResult(
        gate_id="provider.cancellation_canary",
        observed_at=OBSERVED_AT,
        aws_caller_arn=agentcore_capture.EXPECTED_CALLER_ARN,
        seller_actor_owner_id=OWNER,
        seller_command_channel=True,
        job=job,
        work_requests=(cancelled_work,),
        provider_call_permits=(
            _permit("attempt_retired", cancelled_work.work_request_id, retired=True),
        ),
        provider_calls=(),
        command_outcomes=(
            provider.CommandRaceObservation(
                name="cancel", outcome="won", actor_owner_id=OWNER, expected_record_version=0
            ),
        ),
        command_receipts=(receipt,),
        review_decisions=(),
        cancellation_decisions=(decision,),
        running_execution_count=running,
        administrator_action_count=0,
        root_action_count=0,
        publication_attempt_count=0,
        order_attempt_count=0,
        fulfillment_attempt_count=0,
        work_inventory_complete=True,
        permit_inventory_complete=True,
        execution_inventory_complete=True,
        command_inventory_complete=True,
    )


class _Backend:
    def __init__(self, result: provider.ProviderLiveResult) -> None:
        self.result = result
        self.requests: list[provider.ProviderCanaryRequest] = []

    def run(self, request: provider.ProviderCanaryRequest) -> provider.ProviderLiveResult:
        self.requests.append(request)
        return self.result


@pytest.fixture
def private_workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    repository = tmp_path / "repository"
    private = repository / ".mr_lister_private" / "phase66-acceptance"
    private.mkdir(mode=0o700, parents=True)
    repository.chmod(0o700)
    (repository / ".mr_lister_private").chmod(0o700)
    private.chmod(0o700)
    monkeypatch.setattr(provider, "REPOSITORY_ROOT", repository)
    monkeypatch.setattr(provider, "PRIVATE_ROOT", private)
    return private


@pytest.mark.parametrize(
    ("gate_id", "result_factory", "expected_artifacts"),
    [
        ("provider.primary_same_job_canary", _primary_result, 3),
        ("provider.concurrency_canary", _concurrency_result, 2),
        ("provider.cancellation_canary", _cancellation_result, 2),
    ],
)
def test_capture_and_prepare_each_provider_gate_with_no_raw_identifiers(
    private_workspace: Path,
    gate_id: str,
    result_factory: Any,
    expected_artifacts: int,
) -> None:
    authorities = _authority_files(private_workspace, gate_id)
    backend = _Backend(result_factory())
    observation = private_workspace / gate_id / "observation.json"

    captured = provider.capture_phase66_provider_observation(
        gate_id=gate_id,
        **authorities.arguments(),
        output_path=observation,
        backend_factory=lambda: backend,
    )

    assert len(backend.requests) == 1
    assert backend.requests[0].gate_id == gate_id
    assert os.stat(observation, follow_symlinks=False).st_mode & 0o777 == 0o600
    serialized = observation.read_text()
    for raw_value in (
        OWNER,
        JOB_ID,
        PRODUCT_ID,
        IMAGE_ID,
        "attempt_product_post",
        "b" * 24,
        agentcore_capture.EXPECTED_CALLER_ARN,
    ):
        assert raw_value not in serialized
    retry_backend = _Backend(result_factory())
    with pytest.raises(provider.ProviderAcceptanceError, match="fresh"):
        provider.capture_phase66_provider_observation(
            gate_id=gate_id,
            **authorities.arguments(),
            output_path=observation,
            backend_factory=lambda: retry_backend,
        )
    assert retry_backend.requests == []

    output_root = private_workspace / gate_id / "evidence"
    summary = provider.prepare_phase66_provider_evidence(
        gate_id=gate_id,
        **authorities.arguments(),
        observation_path=observation,
        observation_sha256=str(captured["observation_sha256"]),
        output_root=output_root,
    )

    records = json.loads((output_root / "records.json").read_bytes())
    record = validate_phase66_evidence(records[0])
    assert record.gate_id == gate_id
    assert record.provider_gate_attestation.run_gate_digest == authorities.run_gate_sha256
    assert (
        record.provider_gate_attestation.provider_write_gate_digest == authorities.write_gate_sha256
    )
    assert len(record.artifacts) == expected_artifacts
    assert summary["artifact_count"] == expected_artifacts
    bindings = json.loads((output_root / "artifact-files.json").read_bytes())
    by_digest = {binding["artifact_digest"]: binding for binding in bindings}
    forbidden = {
        name.casefold() for name in phase66_acceptance_manifest().forbidden_evidence_field_names
    }

    def assert_sanitized(value: object) -> None:
        if isinstance(value, dict):
            assert not forbidden.intersection(key.casefold() for key in value)
            for nested in value.values():
                assert_sanitized(nested)
        elif isinstance(value, list):
            for nested in value:
                assert_sanitized(nested)

    for artifact in record.artifacts:
        binding = by_digest[artifact.artifact_digest]
        contents = (output_root / binding["relative_path"]).read_bytes()
        assert sha256(contents).hexdigest() == artifact.artifact_digest
        assert len(contents) == artifact.byte_count
        document = json.loads(contents)
        assert document["result"] == "passed"
        assert_sanitized(document)
    combined = b"".join(path.read_bytes() for path in output_root.iterdir())
    assert OWNER.encode() not in combined
    assert JOB_ID.encode() not in combined
    assert PRODUCT_ID.encode() not in combined

    with pytest.raises(provider.ProviderAcceptanceError, match="fresh"):
        provider.prepare_phase66_provider_evidence(
            gate_id=gate_id,
            **authorities.arguments(),
            observation_path=observation,
            observation_sha256=str(captured["observation_sha256"]),
            output_root=output_root,
        )


def test_independent_gate_authority_is_checked_before_backend_construction(
    private_workspace: Path,
) -> None:
    authorities = _authority_files(
        private_workspace,
        "provider.primary_same_job_canary",
        same_nonce=True,
    )
    factory_calls = 0

    def factory() -> _Backend:
        nonlocal factory_calls
        factory_calls += 1
        return _Backend(_primary_result())

    with pytest.raises(provider.ProviderAcceptanceError, match="independent"):
        provider.capture_phase66_provider_observation(
            gate_id="provider.primary_same_job_canary",
            **authorities.arguments(),
            output_path=private_workspace / "bad-observation.json",
            backend_factory=factory,
        )
    assert factory_calls == 0


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("source_commit_digest", _digest("other-source")),
        ("deployment_digest", _digest("other-deployment")),
        ("agentcore_authority_digest", _digest("other-agentcore")),
        ("prerequisite_run_digests", [_digest("other-prerequisite")]),
    ],
)
def test_gate_cannot_drift_from_deployment_source_agentcore_or_prerequisites(
    private_workspace: Path,
    field: str,
    value: object,
) -> None:
    authorities = _authority_files(private_workspace, "provider.primary_same_job_canary")
    run_gate = json.loads(authorities.run_gate_path.read_bytes())
    run_gate[field] = value
    run_gate_sha = _write(authorities.run_gate_path, run_gate)
    drifted = replace(authorities, run_gate_sha256=run_gate_sha)
    factory_calls = 0

    def factory() -> _Backend:
        nonlocal factory_calls
        factory_calls += 1
        return _Backend(_primary_result())

    with pytest.raises(provider.ProviderAcceptanceError, match="authority"):
        provider.capture_phase66_provider_observation(
            gate_id="provider.primary_same_job_canary",
            **drifted.arguments(),
            output_path=private_workspace / "drifted-observation.json",
            backend_factory=factory,
        )
    assert factory_calls == 0


@pytest.mark.parametrize(
    ("gate_id", "result", "message"),
    [
        (
            "provider.primary_same_job_canary",
            _primary_result().model_copy(update={"publication_attempt_count": 1}),
            "Phase 6 authority",
        ),
        (
            "provider.primary_same_job_canary",
            _primary_result().model_copy(
                update={
                    "aws_caller_arn": (f"arn:aws:iam::{agentcore_capture.EXPECTED_ACCOUNT_ID}:root")
                }
            ),
            "Phase 6 authority",
        ),
        (
            "provider.concurrency_canary",
            _concurrency_result(put_count=2),
            "write gate",
        ),
        (
            "provider.cancellation_canary",
            _cancellation_result(running=1),
            "terminal cancellation",
        ),
    ],
)
def test_gate_specific_failures_never_create_an_observation(
    private_workspace: Path,
    gate_id: str,
    result: provider.ProviderLiveResult,
    message: str,
) -> None:
    authorities = _authority_files(private_workspace, gate_id)
    output = private_workspace / gate_id / "rejected-observation.json"

    with pytest.raises(provider.ProviderAcceptanceError, match=message):
        provider.capture_phase66_provider_observation(
            gate_id=gate_id,
            **authorities.arguments(),
            output_path=output,
            backend_factory=lambda: _Backend(result),
        )
    assert not output.exists()


def test_reused_permit_and_cross_product_put_fail_before_sanitization(
    private_workspace: Path,
) -> None:
    authorities = _authority_files(private_workspace, "provider.primary_same_job_canary")
    result = _primary_result()
    calls = list(result.provider_calls)
    calls[3] = calls[3].model_copy(
        update={"attempt_id": calls[2].attempt_id, "product_id": "other_product"}
    )
    invalid = result.model_copy(update={"provider_calls": tuple(calls)})

    with pytest.raises(provider.ProviderAcceptanceError, match="reused one call permit"):
        provider.capture_phase66_provider_observation(
            gate_id="provider.primary_same_job_canary",
            **authorities.arguments(),
            output_path=private_workspace / "invalid-ledger.json",
            backend_factory=lambda: _Backend(invalid),
        )


def test_cross_product_put_and_two_concurrency_winners_fail_closed(
    private_workspace: Path,
) -> None:
    primary_authorities = _authority_files(
        private_workspace,
        "provider.primary_same_job_canary",
    )
    primary = _primary_result()
    calls = list(primary.provider_calls)
    calls[3] = calls[3].model_copy(update={"product_id": "other_product"})
    with pytest.raises(provider.ProviderAcceptanceError, match="one final unpublished product"):
        provider.capture_phase66_provider_observation(
            gate_id="provider.primary_same_job_canary",
            **primary_authorities.arguments(),
            output_path=private_workspace / "cross-product.json",
            backend_factory=lambda: _Backend(
                primary.model_copy(update={"provider_calls": tuple(calls)})
            ),
        )

    concurrency_authorities = _authority_files(
        private_workspace,
        "provider.concurrency_canary",
    )
    concurrency = _concurrency_result()
    commands = list(concurrency.command_outcomes)
    commands[1] = commands[1].model_copy(update={"outcome": provider.ProviderCommandOutcome.WON})
    with pytest.raises(provider.ProviderAcceptanceError, match="three-command winner"):
        provider.capture_phase66_provider_observation(
            gate_id="provider.concurrency_canary",
            **concurrency_authorities.arguments(),
            output_path=private_workspace / "two-winners.json",
            backend_factory=lambda: _Backend(
                concurrency.model_copy(update={"command_outcomes": tuple(commands)})
            ),
        )


def test_tampered_sanitized_observation_cannot_be_prepared(
    private_workspace: Path,
) -> None:
    gate_id = "provider.cancellation_canary"
    authorities = _authority_files(private_workspace, gate_id)
    observation = private_workspace / gate_id / "observation.json"
    captured = provider.capture_phase66_provider_observation(
        gate_id=gate_id,
        **authorities.arguments(),
        output_path=observation,
        backend_factory=lambda: _Backend(_cancellation_result()),
    )
    value = json.loads(observation.read_bytes())
    value["facts"]["remaining_execution_count"] = 1
    tampered = private_workspace / gate_id / "tampered.json"
    tampered_sha = _write(tampered, value)

    with pytest.raises(provider.ProviderAcceptanceError, match="cancellation"):
        provider.prepare_phase66_provider_evidence(
            gate_id=gate_id,
            **authorities.arguments(),
            observation_path=tampered,
            observation_sha256=tampered_sha,
            output_root=private_workspace / gate_id / "tampered-evidence",
        )
    assert captured["result"] == "passed"
    assert not (private_workspace / gate_id / "tampered-evidence").exists()


def test_offline_preparer_cli_emits_only_sanitized_summary(
    private_workspace: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    gate_id = "provider.cancellation_canary"
    authorities = _authority_files(private_workspace, gate_id)
    observation = private_workspace / gate_id / "cli-observation.json"
    captured = provider.capture_phase66_provider_observation(
        gate_id=gate_id,
        **authorities.arguments(),
        output_path=observation,
        backend_factory=lambda: _Backend(_cancellation_result()),
    )
    output = private_workspace / gate_id / "cli-evidence"

    assert (
        provider_cli.main(
            [
                "--gate-id",
                gate_id,
                "--deployment-authority",
                str(authorities.deployment_path),
                "--deployment-authority-sha256",
                authorities.deployment_sha256,
                "--agentcore-authority",
                str(authorities.agentcore_path),
                "--agentcore-authority-sha256",
                authorities.agentcore_sha256,
                "--prerequisite-records",
                str(authorities.prerequisite_path),
                "--prerequisite-records-sha256",
                authorities.prerequisite_sha256,
                "--run-gate",
                str(authorities.run_gate_path),
                "--run-gate-sha256",
                authorities.run_gate_sha256,
                "--provider-write-gate",
                str(authorities.write_gate_path),
                "--provider-write-gate-sha256",
                authorities.write_gate_sha256,
                "--observation",
                str(observation),
                "--observation-sha256",
                str(captured["observation_sha256"]),
                "--output-root",
                str(output),
            ]
        )
        == 0
    )
    summary = json.loads(capsys.readouterr().out)
    assert summary["gate_id"] == gate_id
    assert summary["result"] == "passed"
    assert set(summary) == {
        "artifact_count",
        "gate_id",
        "record_digest",
        "result",
        "run_digest",
    }
