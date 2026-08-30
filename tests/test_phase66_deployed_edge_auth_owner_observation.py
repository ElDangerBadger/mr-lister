from __future__ import annotations

import json
import os
from copy import deepcopy
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import Any

import pytest

from tools import phase66_deployed_edge_auth_owner_observation as observation
from tools import phase66_deployed_upload_integrity_smoke as upload_smoke
from tools import prepare_phase66_edge_revalidation as revalidation


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
        "source_commit_digest": observation.SOURCE_COMMIT_DIGEST,
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
        "captured_at": "2026-08-30T00:57:26Z",
        "deployment_digest": upload_smoke._digest_json(authority),
        "format": "phase6.6-sanitized-deployment-authority-v1",
    }


def _baseline() -> dict[str, object]:
    job_digests = sorted((upload_smoke._digest_text(JOB_A), upload_smoke._digest_text(JOB_B)))
    return {
        "actor_digest": upload_smoke._digest_text(OWNER_A),
        "baseline_contract": observation.BASELINE_FORMAT,
        "bucket_versioning_enabled": True,
        "canary_byte_count": upload_smoke.PRIMARY_SIZE,
        "canary_sha256": upload_smoke.PRIMARY_SHA256,
        "entity_type_counts": {"CONTROL_JOB": 2, "SOURCE_ARTIFACT": 2},
        "existing_job_count": 2,
        "existing_job_digests": job_digests,
        "existing_job_set_digest": upload_smoke._digest_json(job_digests),
        "existing_job_states": ["failed_retryable", "failed_retryable"],
        "provider_record_count": 0,
        "running_execution_count": 0,
        "selected_content_sha256": upload_smoke.PRIMARY_SHA256,
        "selected_inventory_count": 1,
        "selected_inventory_digest": _digest("inventory"),
        "selected_job_digest": upload_smoke._digest_text(JOB_A),
        "selected_job_record_digest": _digest("job-record"),
        "selected_object_coordinate_digest": _digest("coordinate"),
        "selected_pinned_head_matches": {
            "checksum": True,
            "content_type": True,
            "encryption": True,
            "size": True,
            "version": True,
        },
        "selected_pinned_is_latest": True,
        "selected_pinned_tag_matches": True,
        "selected_pinned_version_digest": _digest("version"),
        "selected_source_authority_digest": _digest("source-authority"),
        "selected_source_record_digest": _digest("source-record"),
        "table_record_count": 4,
        "table_scanned_count": 4,
    }


def _browser_checkpoint() -> dict[str, object]:
    return {
        "format": observation.BROWSER_CHECKPOINT_FORMAT,
        "recorded_at": "2026-08-30T00:59:00Z",
        "deployment_digest": _deployment()["deployment_digest"],
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


SUBJECT_A = "seller-a-subject-secret"
SUBJECT_B = "seller-b-subject-secret"
ISSUER = "https://cognito-idp.us-west-2.amazonaws.com/pool-secret"
OWNER_A = sha256(ISSUER.encode() + b"\0" + SUBJECT_A.encode()).hexdigest()
JOB_A = "job_a_secret"
JOB_B = "job_b_secret"


def _authority() -> upload_smoke.Authority:
    return upload_smoke.Authority(
        owner_id=OWNER_A,
        subject=SUBJECT_A,
        job_id=JOB_A,
        bucket="private-bucket-secret",
        source_key="private/source-secret.png",
        source_version="version-secret",
        issuer=ISSUER,
        client_id="client-secret",
        upload_function="upload-secret",
        review_function="review-secret",
        table_name="table-secret",
        state_machine_arns=("state-machine-secret",),
    )


def _snapshot(*, mutate: bool = False) -> upload_smoke.Snapshot:
    authority = _authority()
    job_a = {
        "job_id": JOB_A,
        "owner_id": OWNER_A,
        "state": "failed_retryable",
        "correlation_id": "correlation-secret",
    }
    job_b = {
        "job_id": JOB_B,
        "owner_id": OWNER_A,
        "state": "failed_retryable",
    }
    source_a = {"job_id": JOB_A, "version": 1 if not mutate else 2}
    source_b = {"job_id": JOB_B, "version": 1}
    inventory = (
        upload_smoke.InventoryVersion(
            version_id="version-secret",
            is_latest=True,
            last_modified="2026-08-29T20:00:00+00:00",
            size_bytes=upload_smoke.PRIMARY_SIZE,
            etag='"etag-secret"',
        ),
    )
    return upload_smoke.Snapshot(
        authority=authority,
        items=(
            ("CONTROL_JOB", job_a),
            ("CONTROL_JOB", job_b),
            ("SOURCE_ARTIFACT", source_a),
            ("SOURCE_ARTIFACT", source_b),
        ),
        selected_job=job_a,
        selected_source=source_a,
        inventory=inventory,
        execution_digests=(_digest("execution"),),
    )


class FakeBackend:
    def __init__(self, *, mutate: bool = False, subjects: tuple[str, ...] | None = None) -> None:
        self.before = _snapshot()
        self.after = _snapshot(mutate=mutate)
        self.subjects = subjects or (SUBJECT_A, SUBJECT_B)
        self.events: list[tuple[str, dict[str, Any]]] = []

    def prepare(self, gate: upload_smoke.RunGate, primary: bytes) -> upload_smoke.Snapshot:
        assert gate.deployment_digest == _deployment()["deployment_digest"]
        assert primary == b"primary"
        return self.before

    def confirmed_seller_subjects(self) -> tuple[str, ...]:
        return self.subjects

    def invoke_review(
        self,
        authority: upload_smoke.Authority,
        event: dict[str, Any],
    ) -> dict[str, Any]:
        self.events.append((authority.owner_id, deepcopy(event)))
        route = event["routeKey"]
        path = event["rawPath"]
        if route == "GET /v1/jobs":
            jobs = [{"state": "failed_retryable"}] * (2 if authority.owner_id == OWNER_A else 0)
            return {"statusCode": 200, "headers": {}, "body": json.dumps({"jobs": jobs})}
        if route == "GET /v1/jobs/{job_id}/review":
            return {
                "statusCode": 200,
                "headers": {"ETag": '"strong-etag-secret"'},
                "body": json.dumps(
                    {
                        "review_authority_etag": "review-authority-secret",
                        "preview": {"readiness": "ready"},
                    }
                ),
            }
        assert route == "GET /v1/jobs/{job_id}"
        assert path.endswith(JOB_A) or path.endswith(observation.UNKNOWN_JOB_ID)
        return {"statusCode": 404, "headers": {}, "body": json.dumps({"error": "absent"})}

    def snapshot(self, authority: upload_smoke.Authority) -> upload_smoke.Snapshot:
        assert authority.owner_id == OWNER_A
        return self.after


@pytest.fixture
def private_workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    repository = tmp_path / "repository"
    private = repository / ".mr_lister_private" / "phase66-acceptance"
    private.mkdir(mode=0o700, parents=True)
    (repository / ".mr_lister_private").chmod(0o700)
    monkeypatch.setattr(observation, "REPOSITORY_ROOT", repository)
    monkeypatch.setattr(observation, "PRIVATE_ROOT", private)
    monkeypatch.setattr(upload_smoke, "REPOSITORY_ROOT", repository)
    monkeypatch.setattr(upload_smoke, "PRIVATE_ROOT", private)
    monkeypatch.setattr(
        observation,
        "exact_canaries",
        lambda: (b"primary", b"wrong", b"overwrite"),
    )
    return private


def _write_private(path: Path, value: object) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.parent.chmod(0o700)
    path.write_text(json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n")
    path.chmod(0o600)


def _inputs(private: Path) -> tuple[Path, Path, Path]:
    deployment = private / "inputs" / "deployment.json"
    baseline = private / "inputs" / "baseline.json"
    checkpoint = private / "inputs" / "browser-checkpoint.json"
    _write_private(deployment, _deployment())
    _write_private(baseline, _baseline())
    _write_private(checkpoint, _browser_checkpoint())
    return deployment, baseline, checkpoint


def _entropy() -> Any:
    values = iter(bytes([ordinal]) * 16 for ordinal in range(1, 6))
    return lambda size: next(values) if size == 16 else b""


def test_capture_emits_exact_private_sanitized_v1(
    private_workspace: Path,
) -> None:
    deployment, baseline, checkpoint = _inputs(private_workspace)
    output = private_workspace / "run" / "edge-observation.json"
    backend = FakeBackend()

    result = observation.capture_phase66_edge_observation(
        deployment_authority_path=deployment,
        baseline_preflight_path=baseline,
        browser_checkpoint_path=checkpoint,
        output_path=output,
        backend=backend,
        clock=lambda: datetime(2026, 8, 30, 1, 0, tzinfo=UTC),
        entropy=_entropy(),
    )

    document = json.loads(output.read_bytes())
    validated = revalidation._EdgeObservation.model_validate(document)
    assert validated.format == observation.OBSERVATION_FORMAT
    assert validated.recorded_at == "2026-08-30T01:00:00Z"
    assert validated.run.authority_digest == observation._digest(
        {
            "contract": "phase6.6-edge-revalidation-browser-binding-v1",
            "browser_checkpoint_digest": observation._digest(_browser_checkpoint()),
            "deployment_digest": _deployment()["deployment_digest"],
        }
    )
    assert result["status"] == "passed"
    assert result["deployment_digest"] == _deployment()["deployment_digest"]
    assert result["observation_sha256"] == sha256(output.read_bytes()).hexdigest()
    assert os.stat(output, follow_symlinks=False).st_mode & 0o777 == 0o600
    assert os.stat(output.parent, follow_symlinks=False).st_mode & 0o777 == 0o700
    serialized = output.read_bytes()
    for secret in (
        SUBJECT_A,
        SUBJECT_B,
        OWNER_A,
        JOB_A,
        JOB_B,
        "private-bucket-secret",
        "client-secret",
        "strong-etag-secret",
    ):
        assert secret.encode() not in serialized
    assert len(backend.events) == 6
    assert all("Authorization" not in event.get("headers", {}) for _owner, event in backend.events)


def test_capture_fails_closed_if_read_only_state_changes(private_workspace: Path) -> None:
    deployment, baseline, checkpoint = _inputs(private_workspace)
    output = private_workspace / "mutated" / "edge-observation.json"

    with pytest.raises(
        observation.Phase66EdgeObservationError,
        match="changed application state",
    ):
        observation.capture_phase66_edge_observation(
            deployment_authority_path=deployment,
            baseline_preflight_path=baseline,
            browser_checkpoint_path=checkpoint,
            output_path=output,
            backend=FakeBackend(mutate=True),
            clock=lambda: datetime(2026, 8, 30, 1, 0, tzinfo=UTC),
            entropy=_entropy(),
        )

    assert not output.exists()


def test_capture_requires_one_distinct_confirmed_seller_b(private_workspace: Path) -> None:
    deployment, baseline, checkpoint = _inputs(private_workspace)

    with pytest.raises(observation.Phase66EdgeObservationError, match="uniquely"):
        observation.capture_phase66_edge_observation(
            deployment_authority_path=deployment,
            baseline_preflight_path=baseline,
            browser_checkpoint_path=checkpoint,
            output_path=private_workspace / "missing-b" / "edge-observation.json",
            backend=FakeBackend(subjects=(SUBJECT_A,)),
            clock=lambda: datetime(2026, 8, 30, 1, 0, tzinfo=UTC),
            entropy=_entropy(),
        )


def test_inputs_are_closed_and_paths_are_confined(private_workspace: Path) -> None:
    deployment, baseline, checkpoint = _inputs(private_workspace)
    invalid = _baseline()
    invalid["access_token"] = "secret"
    _write_private(baseline, invalid)
    with pytest.raises(observation.Phase66EdgeObservationError, match="exact sanitized"):
        observation.capture_phase66_edge_observation(
            deployment_authority_path=deployment,
            baseline_preflight_path=baseline,
            browser_checkpoint_path=checkpoint,
            output_path=private_workspace / "closed" / "edge-observation.json",
            backend=FakeBackend(),
        )

    _write_private(baseline, _baseline())
    with pytest.raises(observation.Phase66EdgeObservationError, match="must stay"):
        observation.capture_phase66_edge_observation(
            deployment_authority_path=deployment,
            baseline_preflight_path=baseline,
            browser_checkpoint_path=checkpoint,
            output_path=private_workspace.parent / "escaped.json",
            backend=FakeBackend(),
        )


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ({"deployment_digest": "f" * 64}, "exact deployment"),
        ({"recorded_at": "2026-08-30T01:01:00Z"}, "outside"),
        ({"matrix.token_exchange_passed": False}, "exact sanitized"),
    ],
)
def test_browser_checkpoint_mismatch_future_and_false_values_fail_closed(
    private_workspace: Path,
    mutation: dict[str, object],
    message: str,
) -> None:
    deployment, baseline, checkpoint = _inputs(private_workspace)
    document = _browser_checkpoint()
    for key, value in mutation.items():
        if key == "matrix.token_exchange_passed":
            matrix = document["matrix"]
            assert isinstance(matrix, dict)
            matrix["token_exchange_passed"] = value
        else:
            document[key] = value
    _write_private(checkpoint, document)

    with pytest.raises(observation.Phase66EdgeObservationError, match=message):
        observation.capture_phase66_edge_observation(
            deployment_authority_path=deployment,
            baseline_preflight_path=baseline,
            browser_checkpoint_path=checkpoint,
            output_path=private_workspace / "checkpoint-failure" / "edge-observation.json",
            backend=FakeBackend(),
            clock=lambda: datetime(2026, 8, 30, 1, 0, tzinfo=UTC),
            entropy=_entropy(),
        )


def test_missing_browser_checkpoint_fails_before_backend_calls(private_workspace: Path) -> None:
    deployment, baseline, checkpoint = _inputs(private_workspace)
    checkpoint.unlink()
    backend = FakeBackend()

    with pytest.raises(observation.Phase66EdgeObservationError, match="browser checkpoint"):
        observation.capture_phase66_edge_observation(
            deployment_authority_path=deployment,
            baseline_preflight_path=baseline,
            browser_checkpoint_path=checkpoint,
            output_path=private_workspace / "missing-checkpoint" / "edge-observation.json",
            backend=backend,
            clock=lambda: datetime(2026, 8, 30, 1, 0, tzinfo=UTC),
            entropy=_entropy(),
        )

    assert backend.events == []


def test_cli_uses_only_the_four_private_paths(
    private_workspace: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    deployment, baseline, checkpoint = _inputs(private_workspace)
    output = private_workspace / "cli" / "edge-observation.json"

    assert (
        observation.main(
            [
                "--deployment-authority",
                str(deployment),
                "--baseline-preflight",
                str(baseline),
                "--browser-checkpoint",
                str(checkpoint),
                "--output",
                str(output),
            ],
            backend_factory=FakeBackend,
            clock=lambda: datetime(2026, 8, 30, 1, 0, tzinfo=UTC),
            entropy=_entropy(),
        )
        == 0
    )
    summary = json.loads(capsys.readouterr().out)
    assert summary["status"] == "passed"
    assert output.is_file()


def test_runner_source_has_no_browser_token_or_mutating_backend_call() -> None:
    source = Path(observation.__file__).read_text(encoding="utf-8")
    for forbidden in (
        ".invoke_upload(",
        ".post_form(",
        ".put_temporary(",
        ".delete_temporary(",
        "playwright",
        "selenium",
        "get_cookies",
        "local_storage",
        "access_token",
        "refresh_token",
    ):
        assert forbidden not in source
