from __future__ import annotations

import json
import os
from copy import deepcopy
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
from tools import capture_phase66_outbox_recovery_baseline as baseline_capture
from tools import phase66_deployed_edge_auth_owner_observation as edge_observation
from tools import phase66_deployed_outbox_recovery_smoke as outbox_smoke
from tools import phase66_deployed_upload_integrity_smoke as private_io
from tools import prepare_phase66_edge_revalidation as revalidation
from tools import prepare_phase66_outbox_recovery_gate_seed as gate_seed
from tools import record_phase66_browser_checkpoint as browser_checkpoint

DEPLOYED_AT = datetime(2026, 8, 31, 20, 0, tzinfo=UTC)
OBSERVED_AT = DEPLOYED_AT + timedelta(minutes=15)
RECORDED_AT = OBSERVED_AT + timedelta(minutes=5)


def _digest(label: str) -> str:
    return sha256(label.encode("ascii")).hexdigest()


def _deployment() -> dict[str, object]:
    lambdas = [
        {
            "code_sha256": _digest(f"code-{logical_id}"),
            "configuration_digest": _digest(f"configuration-{logical_id}"),
            "last_update_status": "Successful",
            "logical_id": logical_id,
            "release_fingerprint_digest": _digest(f"release-{logical_id}"),
            "state": "Active",
        }
        for logical_id in revalidation._LAMBDA_LOGICAL_IDS
    ]
    authority = {
        "account_binding_digest": _digest("account"),
        "cognito": {
            "browser_client_configuration_digest": _digest("browser-client"),
            "browser_client_secret_present": False,
            "confirmed_user_count": 2,
            "enabled_user_count": 2,
            "mfa_configuration": "ON",
            "pool_configuration_digest": _digest("pool"),
            "seller_group_member_count": 2,
            "software_token_mfa_user_count": 2,
            "user_count": 2,
        },
        "lambdas": lambdas,
        "readiness": "WEB_EDGE_ACTIVE_DRAFT_ONLY",
        "region": "us-west-2",
        "source_commit_digest": edge_observation.SOURCE_COMMIT_DIGEST,
        "stack": {
            "incomplete_resource_count": 0,
            "output_count": 19,
            "outputs_digest": _digest("outputs"),
            "resource_count": 125,
            "resource_inventory_digest": _digest("resources"),
            "stack_status": "UPDATE_COMPLETE",
            "tags_digest": _digest("tags"),
            "template_digest": _digest("template"),
            "termination_protection": True,
        },
        "stack_name": "mr-lister-phase6-dev",
        "web_edge": {
            "alias_count": 1,
            "api_configuration_digest": _digest("api"),
            "api_protocol": "HTTP",
            "application_body_digest": _digest("application"),
            "application_status_code": 200,
            "cors_headers_digest": _digest("cors"),
            "cors_passed": True,
            "cors_status_code": 204,
            "distribution_configuration_digest": _digest("distribution"),
            "distribution_enabled": True,
            "distribution_status": "Deployed",
            "health_body_digest": _digest("health"),
            "health_passed": True,
            "health_status_code": 200,
            "origin_count": 2,
            "route_count": 15,
            "security_header_count": 7,
            "security_headers_digest": _digest("security"),
            "security_headers_passed": True,
        },
    }
    return {
        "authority": authority,
        "captured_at": DEPLOYED_AT.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "deployment_digest": private_io._digest_json(authority),
        "format": revalidation.DEPLOYMENT_AUTHORITY_FORMAT,
    }


def _artifact(kind: ArtifactKind, label: str) -> dict[str, object]:
    return {
        "artifact_digest": _digest(label),
        "artifact_format": ArtifactFormat.JSON.value,
        "byte_count": 1,
        "kind": kind.value,
        "redaction_verified": True,
    }


def _upload_records(
    *,
    deployment_digest: str | None = None,
    recorded_at: datetime = DEPLOYED_AT + timedelta(minutes=10),
) -> list[dict[str, object]]:
    gate = next(
        item
        for item in phase66_acceptance_manifest().gates
        if item.gate_id == baseline_capture.PREREQUISITE_GATE_ID
    )
    value = {
        "actor_digests": [_digest("upload-actor")],
        "artifacts": [
            _artifact(ArtifactKind.CANARY_SUMMARY, "upload-canary"),
            _artifact(ArtifactKind.LOG_AUDIT, "upload-log"),
        ],
        "assertions": [
            {
                "assertion_id": assertion_id,
                "observation_digest": _digest(f"upload-{assertion_id}"),
                "observed_count": 0 if assertion_id == "provider_call_count_is_zero" else 1,
                "passed": True,
            }
            for assertion_id in gate.required_assertions
        ],
        "correlation_digest": None,
        "deployment_digest": deployment_digest or str(_deployment()["deployment_digest"]),
        "evidence_class": AcceptanceEvidenceClass.DEPLOYED_NON_DESTRUCTIVE.value,
        "gate_id": baseline_capture.PREREQUISITE_GATE_ID,
        "job_digest": _digest("upload-job"),
        "manifest_digest": phase66_manifest_digest(),
        "moderated_session": None,
        "outcome": "passed",
        "privacy": {
            "forbidden_field_match_count": 0,
            "free_text_value_count": 0,
            "sanitizer_contract": "phase6.6-sanitized-evidence-v1",
            "sensitive_value_match_count": 0,
        },
        "provider_call_summary": None,
        "provider_gate_attestation": None,
        "recorded_at": recorded_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "run_digest": _digest("upload-run"),
        "schema_version": "6.6.0",
        "source_commit_digest": outbox_smoke.SOURCE_AUTHORITY_COMMIT_DIGEST,
        "work_digest": None,
    }
    validate_phase66_evidence(value)
    return [value]


def _operator_observation(**changes: Any) -> dict[str, object]:
    value: dict[str, object] = {
        "format": browser_checkpoint.OPERATOR_OBSERVATION_FORMAT,
        "recorded_at": OBSERVED_AT.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "deployment_digest": _deployment()["deployment_digest"],
        "source_commit_digest": edge_observation.SOURCE_COMMIT_DIGEST,
        "actor_a": {
            "visible_job_count": 2,
            "known_review_ready": True,
            "known_preview_ready": True,
        },
        "actor_b": {"visible_job_count": 0},
        "matrix": {
            "pkce_authorization_passed": True,
            "pkce_callback_passed": True,
            "token_exchange_passed": True,
            "unauthenticated_access_rejected": True,
        },
    }
    value.update(changes)
    return value


@pytest.fixture
def private_workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    repository = tmp_path / "repository"
    private = repository / ".mr_lister_private" / "phase66-acceptance"
    private.mkdir(mode=0o700, parents=True)
    repository.chmod(0o700)
    (repository / ".mr_lister_private").chmod(0o700)
    private.chmod(0o700)
    for module in (baseline_capture, outbox_smoke, private_io, edge_observation):
        monkeypatch.setattr(module, "REPOSITORY_ROOT", repository)
        monkeypatch.setattr(module, "PRIVATE_ROOT", private)
    return private


def _write_private(path: Path, value: object) -> str:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.parent.chmod(0o700)
    payload = private_io._canonical_json(value, pretty=True)
    path.write_bytes(payload)
    path.chmod(0o600)
    return sha256(payload).hexdigest()


def test_outbox_gate_seed_is_exact_consumer_valid_and_create_only(
    private_workspace: Path,
) -> None:
    deployment = private_workspace / "inputs" / "deployment.json"
    prerequisite = private_workspace / "inputs" / "upload-records.json"
    output = private_workspace / "run" / "outbox-gate-seed.json"
    deployment_sha = _write_private(deployment, _deployment())
    prerequisite_sha = _write_private(prerequisite, _upload_records())

    result = gate_seed.prepare_phase66_outbox_recovery_gate_seed(
        deployment_authority_path=deployment,
        deployment_authority_sha256=deployment_sha,
        prerequisite_records_path=prerequisite,
        prerequisite_records_sha256=prerequisite_sha,
        output_path=output,
        entropy=lambda size: b"\x07" * size,
    )

    document = json.loads(output.read_bytes())
    expected_nonce = outbox_smoke._digest_bytes(
        b"\0".join(
            (
                baseline_capture.GATE_SEED_CONTRACT.encode("ascii"),
                str(_deployment()["deployment_digest"]).encode("ascii"),
                _digest("upload-run").encode("ascii"),
                b"\x07" * gate_seed.ENTROPY_BYTES,
            )
        )
    )
    assert document == {
        "authorization_contract": outbox_smoke.GATE_CONTRACT,
        "deployment_digest": _deployment()["deployment_digest"],
        "gate_id": outbox_smoke.GATE_ID,
        "gate_seed_contract": baseline_capture.GATE_SEED_CONTRACT,
        "method_authorization": dict(outbox_smoke._EXPECTED_METHOD_AUTHORIZATION),
        "namespace_nonce": expected_nonce,
        "prerequisite_evidence_run_digest": _digest("upload-run"),
        "source_authority_commit": outbox_smoke.SOURCE_AUTHORITY_COMMIT,
        "source_authority_commit_digest": outbox_smoke.SOURCE_AUTHORITY_COMMIT_DIGEST,
    }
    parsed = baseline_capture._gate_seed(document, result["gate_seed_sha256"])
    assert parsed.deployment_digest == _deployment()["deployment_digest"]
    assert result["gate_seed_sha256"] == sha256(output.read_bytes()).hexdigest()
    assert os.stat(output, follow_symlinks=False).st_mode & 0o777 == 0o600

    with pytest.raises(gate_seed.Phase66OutboxGateSeedError, match="fresh mode-0600"):
        gate_seed.prepare_phase66_outbox_recovery_gate_seed(
            deployment_authority_path=deployment,
            deployment_authority_sha256=deployment_sha,
            prerequisite_records_path=prerequisite,
            prerequisite_records_sha256=prerequisite_sha,
            output_path=output,
            entropy=lambda size: b"\x07" * size,
        )


def test_outbox_gate_seed_rejects_cross_deployment_and_bad_entropy(
    private_workspace: Path,
) -> None:
    deployment = private_workspace / "inputs" / "deployment.json"
    prerequisite = private_workspace / "inputs" / "upload-records.json"
    deployment_sha = _write_private(deployment, _deployment())
    prerequisite_sha = _write_private(
        prerequisite,
        _upload_records(deployment_digest=_digest("different-deployment")),
    )
    output = private_workspace / "run" / "seed.json"

    with pytest.raises(gate_seed.Phase66OutboxGateSeedError, match="deployment/source"):
        gate_seed.prepare_phase66_outbox_recovery_gate_seed(
            deployment_authority_path=deployment,
            deployment_authority_sha256=deployment_sha,
            prerequisite_records_path=prerequisite,
            prerequisite_records_sha256=prerequisite_sha,
            output_path=output,
            entropy=lambda size: b"\x08" * size,
        )
    assert not output.exists()

    valid_prerequisite = private_workspace / "inputs" / "valid-upload-records.json"
    valid_sha = _write_private(valid_prerequisite, _upload_records())
    with pytest.raises(gate_seed.Phase66OutboxGateSeedError, match="entropy"):
        gate_seed.prepare_phase66_outbox_recovery_gate_seed(
            deployment_authority_path=deployment,
            deployment_authority_sha256=deployment_sha,
            prerequisite_records_path=valid_prerequisite,
            prerequisite_records_sha256=valid_sha,
            output_path=output,
            entropy=lambda _size: b"short",
        )
    assert not output.exists()

    stale_prerequisite = private_workspace / "inputs" / "stale-upload-records.json"
    stale_sha = _write_private(
        stale_prerequisite,
        _upload_records(recorded_at=DEPLOYED_AT - timedelta(seconds=1)),
    )
    with pytest.raises(gate_seed.Phase66OutboxGateSeedError, match="predates"):
        gate_seed.prepare_phase66_outbox_recovery_gate_seed(
            deployment_authority_path=deployment,
            deployment_authority_sha256=deployment_sha,
            prerequisite_records_path=stale_prerequisite,
            prerequisite_records_sha256=stale_sha,
            output_path=output,
            entropy=lambda size: b"\x09" * size,
        )
    assert not output.exists()


def test_browser_recorder_emits_exact_current_consumer_schema(
    private_workspace: Path,
) -> None:
    deployment = private_workspace / "inputs" / "deployment.json"
    observation = private_workspace / "inputs" / "operator-observation.json"
    output = private_workspace / "run" / "browser-checkpoint.json"
    deployment_sha = _write_private(deployment, _deployment())
    observation_value = _operator_observation()
    observation_sha = _write_private(observation, observation_value)

    result = browser_checkpoint.record_phase66_browser_checkpoint(
        deployment_authority_path=deployment,
        deployment_authority_sha256=deployment_sha,
        operator_observation_path=observation,
        operator_observation_sha256=observation_sha,
        output_path=output,
        clock=lambda: RECORDED_AT,
    )

    emitted = json.loads(output.read_bytes())
    assert emitted == {
        "format": edge_observation.BROWSER_CHECKPOINT_FORMAT,
        "recorded_at": OBSERVED_AT.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "deployment_digest": _deployment()["deployment_digest"],
        "actor_a": observation_value["actor_a"],
        "actor_b": observation_value["actor_b"],
        "matrix": observation_value["matrix"],
    }
    assert edge_observation._BrowserCheckpoint.model_validate(emitted).model_dump(mode="json") == (
        emitted
    )
    assert "source_commit_digest" not in emitted
    assert result["checkpoint_sha256"] == sha256(output.read_bytes()).hexdigest()
    assert os.stat(output, follow_symlinks=False).st_mode & 0o777 == 0o600

    with pytest.raises(browser_checkpoint.Phase66BrowserCheckpointError, match="fresh mode-0600"):
        browser_checkpoint.record_phase66_browser_checkpoint(
            deployment_authority_path=deployment,
            deployment_authority_sha256=deployment_sha,
            operator_observation_path=observation,
            operator_observation_sha256=observation_sha,
            output_path=output,
            clock=lambda: RECORDED_AT,
        )


@pytest.mark.parametrize(
    ("change", "clock", "message"),
    [
        ({"owner_id": "raw-owner-must-not-be-recorded"}, RECORDED_AT, "closed contract"),
        ({"deployment_digest": _digest("other-deployment")}, RECORDED_AT, "deployment/source"),
        ({"source_commit_digest": _digest("other-source")}, RECORDED_AT, "closed contract"),
        (
            {"recorded_at": (DEPLOYED_AT - timedelta(seconds=1)).strftime("%Y-%m-%dT%H:%M:%SZ")},
            RECORDED_AT,
            "window",
        ),
        (
            {
                "recorded_at": (
                    DEPLOYED_AT + browser_checkpoint.MAX_AUTHORITY_WINDOW + timedelta(seconds=1)
                ).strftime("%Y-%m-%dT%H:%M:%SZ")
            },
            DEPLOYED_AT + browser_checkpoint.MAX_AUTHORITY_WINDOW + timedelta(minutes=1),
            "window",
        ),
        (
            {"recorded_at": OBSERVED_AT.strftime("%Y-%m-%dT%H:%M:%SZ")},
            OBSERVED_AT + browser_checkpoint.MAX_AUTHORITY_WINDOW + timedelta(seconds=1),
            "window",
        ),
    ],
)
def test_browser_recorder_rejects_drift_and_stale_authority(
    private_workspace: Path,
    change: dict[str, object],
    clock: datetime,
    message: str,
) -> None:
    deployment = private_workspace / "inputs" / "deployment.json"
    observation = private_workspace / "inputs" / "operator-observation.json"
    output = private_workspace / "run" / "browser-checkpoint.json"
    deployment_sha = _write_private(deployment, _deployment())
    value = _operator_observation()
    value.update(deepcopy(change))
    observation_sha = _write_private(observation, value)

    with pytest.raises(browser_checkpoint.Phase66BrowserCheckpointError, match=message):
        browser_checkpoint.record_phase66_browser_checkpoint(
            deployment_authority_path=deployment,
            deployment_authority_sha256=deployment_sha,
            operator_observation_path=observation,
            operator_observation_sha256=observation_sha,
            output_path=output,
            clock=lambda: clock,
        )
    assert not output.exists()


def test_browser_recorder_requires_exact_input_digest_and_true_matrix(
    private_workspace: Path,
) -> None:
    deployment = private_workspace / "inputs" / "deployment.json"
    observation = private_workspace / "inputs" / "operator-observation.json"
    output = private_workspace / "run" / "browser-checkpoint.json"
    deployment_sha = _write_private(deployment, _deployment())
    value = _operator_observation()
    matrix = dict(value["matrix"])  # type: ignore[arg-type]
    matrix["token_exchange_passed"] = False
    value["matrix"] = matrix
    observation_sha = _write_private(observation, value)

    with pytest.raises(browser_checkpoint.Phase66BrowserCheckpointError, match="SHA-256"):
        browser_checkpoint.record_phase66_browser_checkpoint(
            deployment_authority_path=deployment,
            deployment_authority_sha256=deployment_sha,
            operator_observation_path=observation,
            operator_observation_sha256="0" * 64,
            output_path=output,
            clock=lambda: RECORDED_AT,
        )
    with pytest.raises(browser_checkpoint.Phase66BrowserCheckpointError, match="closed contract"):
        browser_checkpoint.record_phase66_browser_checkpoint(
            deployment_authority_path=deployment,
            deployment_authority_sha256=deployment_sha,
            operator_observation_path=observation,
            operator_observation_sha256=observation_sha,
            output_path=output,
            clock=lambda: RECORDED_AT,
        )
    assert not output.exists()
