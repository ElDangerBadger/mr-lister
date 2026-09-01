from __future__ import annotations

import base64
import json
import os
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path
from typing import Any

import pytest

from mr_lister.control.models import ControlJobRecord, ControlJobState, SourceArtifactRecord
from mr_lister.control.source_artwork import source_artifact_fingerprint
from tools import capture_phase66_upload_integrity_preflight as capture
from tools import phase66_deployed_edge_auth_owner_observation as consumer
from tools import phase66_deployed_upload_integrity_smoke as smoke
from tools import prepare_phase66_edge_revalidation as revalidation
from tools.phase66_live_acceptance import exact_phase66_canary_png

NOW = datetime(2026, 8, 30, 1, 0, tzinfo=UTC)
OWNER = "a" * 64
JOB_A = "job_secret_a"
JOB_B = "job_secret_b"
VERSION_A = "version-secret-a"
VERSION_B = "version-secret-b"


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
        "account_binding_digest": sha256(capture.ACCOUNT_ID.encode("ascii")).hexdigest(),
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
        "region": capture.REGION,
        "source_commit_digest": revalidation.SOURCE_COMMIT_DIGEST,
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
        "stack_name": capture.STACK_NAME,
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
        "deployment_digest": smoke._digest_json(authority),
        "format": revalidation.DEPLOYMENT_AUTHORITY_FORMAT,
    }


def _source(job_id: str, version_id: str, created_at: datetime) -> SourceArtifactRecord:
    material = {
        "job_id": job_id,
        "owner_id": OWNER,
        "bucket": capture.EXPECTED_BUCKET_NAME,
        "object_key": f"private/owners/{OWNER}/jobs/{job_id}/source/source.png",
        "version_id": version_id,
        "content_sha256": capture.PRIMARY_SHA256,
        "size_bytes": capture.PRIMARY_SIZE,
        "media_type": "image/png",
        "product_profile_id": "gildan_64000_swiftpod",
        "product_profile_version": 2,
        "product_profile_fingerprint": _digest("profile"),
        "created_at": created_at,
    }
    return SourceArtifactRecord(
        fingerprint=source_artifact_fingerprint(**material),
        **material,
    )


def _job(source: SourceArtifactRecord, updated_at: datetime) -> ControlJobRecord:
    return ControlJobRecord(
        owner_id=OWNER,
        job_id=source.job_id,
        state=ControlJobState.FAILED_RETRYABLE,
        source_artifact_fingerprint=source.fingerprint,
        failure_id=f"failure_{source.job_id}",
        created_at=source.created_at,
        updated_at=updated_at,
    )


def _state_machine_arns() -> tuple[str, ...]:
    return tuple(
        f"arn:aws:states:{capture.REGION}:{capture.ACCOUNT_ID}:stateMachine:{name}"
        for name in capture._WORKFLOW_OUTPUTS.values()
    )


def _snapshot(*, tied: bool = False) -> capture.BaselineSnapshot:
    source_a = _source(JOB_A, VERSION_A, NOW - timedelta(hours=2))
    source_b = _source(JOB_B, VERSION_B, NOW - timedelta(hours=1))
    job_a = _job(source_a, NOW if not tied else NOW + timedelta(minutes=1))
    job_b = _job(source_b, NOW + timedelta(minutes=1))
    items = (
        ("CONTROL_JOB", job_a.model_dump(mode="json")),
        ("CONTROL_JOB", job_b.model_dump(mode="json")),
        ("SOURCE_ARTIFACT", source_a.model_dump(mode="json")),
        ("SOURCE_ARTIFACT", source_b.model_dump(mode="json")),
        ("REVIEW", {"review_id": "review-secret-identity", "state": "ready"}),
    )
    return capture.BaselineSnapshot(
        table_name=capture.EXPECTED_TABLE_NAME,
        artifact_bucket=capture.EXPECTED_BUCKET_NAME,
        state_machine_arns=_state_machine_arns(),
        items=items,
        table_record_count=len(items),
        table_scanned_count=len(items),
        inventory=(
            capture.InventoryEntry(
                kind="version",
                version_id=VERSION_B,
                is_latest=True,
                last_modified="2026-08-30T00:30:00+00:00",
                size_bytes=capture.PRIMARY_SIZE,
                etag='"etag-secret"',
            ),
        ),
        head_matches={
            "checksum": True,
            "content_type": True,
            "encryption": True,
            "size": True,
            "version": True,
        },
        pinned_tag_matches=True,
        bucket_versioning_enabled=True,
        running_execution_count=0,
    )


class FakeBackend:
    def __init__(self, snapshot: capture.BaselineSnapshot | None = None) -> None:
        self.snapshot = _snapshot() if snapshot is None else snapshot
        self.calls = 0

    def capture_baseline(
        self,
        deployment: revalidation._DeploymentAuthorityDocument,
        canary: bytes,
    ) -> capture.BaselineSnapshot:
        self.calls += 1
        assert deployment.deployment_digest == _deployment()["deployment_digest"]
        assert len(canary) == capture.PRIMARY_SIZE
        assert smoke._digest_bytes(canary) == capture.PRIMARY_SHA256
        return self.snapshot


@pytest.fixture(scope="module")
def canary() -> bytes:
    return exact_phase66_canary_png()


@pytest.fixture
def private_workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    repository = tmp_path / "repository"
    private = repository / ".mr_lister_private" / "phase66-acceptance"
    private.mkdir(mode=0o700, parents=True)
    repository.chmod(0o700)
    (repository / ".mr_lister_private").chmod(0o700)
    private.chmod(0o700)
    monkeypatch.setattr(capture, "REPOSITORY_ROOT", repository)
    monkeypatch.setattr(capture, "PRIVATE_ROOT", private)
    monkeypatch.setattr(smoke, "REPOSITORY_ROOT", repository)
    monkeypatch.setattr(smoke, "PRIVATE_ROOT", private)
    monkeypatch.setattr(consumer, "REPOSITORY_ROOT", repository)
    monkeypatch.setattr(consumer, "PRIVATE_ROOT", private)
    return private


def _write(path: Path, value: object) -> str:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.parent.chmod(0o700)
    payload = smoke._canonical_json(value, pretty=True)
    path.write_bytes(payload)
    path.chmod(0o600)
    return smoke._digest_bytes(payload)


def test_capture_writes_one_consumer_valid_sanitized_create_only_baseline(
    private_workspace: Path,
    canary: bytes,
) -> None:
    deployment_path = private_workspace / "inputs" / "deployment-authority.json"
    deployment_sha256 = _write(deployment_path, _deployment())
    output = private_workspace / "run" / "upload-integrity-preflight.json"
    backend = FakeBackend()

    result = capture.capture_phase66_upload_integrity_preflight(
        deployment_authority_path=deployment_path,
        deployment_authority_sha256=deployment_sha256,
        output_path=output,
        backend_factory=lambda: backend,
        canary_factory=lambda: canary,
    )

    assert result == {
        "baseline_sha256": smoke._digest_bytes(output.read_bytes()),
        "byte_count": len(output.read_bytes()),
        "deployment_digest": _deployment()["deployment_digest"],
        "result": "passed",
    }
    assert backend.calls == 1
    assert os.stat(output, follow_symlinks=False).st_mode & 0o777 == 0o600
    document = consumer._baseline(output)
    assert document["baseline_contract"] == consumer.BASELINE_FORMAT
    assert document["existing_job_count"] == 2
    assert document["selected_job_digest"] == smoke._digest_text(JOB_B)
    assert document["selected_pinned_version_digest"] == smoke._digest_text(VERSION_B)
    serialized = output.read_bytes()
    for secret in (
        OWNER,
        JOB_A,
        JOB_B,
        VERSION_A,
        VERSION_B,
        capture.EXPECTED_BUCKET_NAME,
        "review-secret-identity",
        "etag-secret",
    ):
        assert secret.encode() not in serialized

    with pytest.raises(capture.UploadIntegrityPreflightError, match="fresh mode-0600"):
        capture.capture_phase66_upload_integrity_preflight(
            deployment_authority_path=deployment_path,
            deployment_authority_sha256=deployment_sha256,
            output_path=output,
            backend_factory=lambda: backend,
            canary_factory=lambda: canary,
        )
    assert backend.calls == 2


@pytest.mark.parametrize("failure", ["digest", "account", "canary"])
def test_local_authority_failures_happen_before_backend_construction(
    private_workspace: Path,
    canary: bytes,
    failure: str,
) -> None:
    deployment = _deployment()
    if failure == "account":
        authority = deployment["authority"]
        assert isinstance(authority, dict)
        authority["account_binding_digest"] = _digest("wrong-account")
        deployment["deployment_digest"] = smoke._digest_json(authority)
    deployment_path = private_workspace / failure / "deployment.json"
    deployment_sha256 = _write(deployment_path, deployment)
    if failure == "digest":
        deployment_sha256 = "0" * 64
    constructed = False

    def forbidden_factory() -> FakeBackend:
        nonlocal constructed
        constructed = True
        raise AssertionError("backend must not be constructed")

    with pytest.raises(capture.UploadIntegrityPreflightError):
        capture.capture_phase66_upload_integrity_preflight(
            deployment_authority_path=deployment_path,
            deployment_authority_sha256=deployment_sha256,
            output_path=private_workspace / failure / "baseline.json",
            backend_factory=forbidden_factory,
            canary_factory=(lambda: b"wrong") if failure == "canary" else (lambda: canary),
        )
    assert constructed is False


def test_frozen_exactly_two_job_and_unique_latest_prerequisites_are_enforced(
    canary: bytes,
) -> None:
    tied = _snapshot(tied=True)
    with pytest.raises(capture.UploadIntegrityPreflightError, match="ambiguous"):
        capture._baseline_document(tied, canary)

    source_c = _source("job_secret_c", "version-secret-c", NOW)
    job_c = _job(source_c, NOW + timedelta(minutes=2))
    expanded_items = tied.items + (
        ("CONTROL_JOB", job_c.model_dump(mode="json")),
        ("SOURCE_ARTIFACT", source_c.model_dump(mode="json")),
    )
    expanded = replace(
        tied,
        items=expanded_items,
        table_record_count=len(expanded_items),
        table_scanned_count=len(expanded_items),
    )
    with pytest.raises(capture.UploadIntegrityPreflightError, match="exactly two"):
        capture._baseline_document(expanded, canary)


@pytest.mark.parametrize(
    "snapshot",
    [
        replace(
            _snapshot(),
            items=_snapshot().items + (("PROVIDER_DRAFT", {"provider_id": "secret-provider"}),),
            table_record_count=len(_snapshot().items) + 1,
            table_scanned_count=len(_snapshot().items) + 1,
        ),
        replace(_snapshot(), running_execution_count=1),
        replace(_snapshot(), pinned_tag_matches=False),
        replace(
            _snapshot(),
            inventory=_snapshot().inventory
            + (
                capture.InventoryEntry(
                    kind="delete_marker",
                    version_id="delete-marker-secret",
                    is_latest=False,
                    last_modified="2026-08-29T23:00:00+00:00",
                ),
            ),
        ),
        replace(
            _snapshot(),
            head_matches={
                "checksum": False,
                "content_type": True,
                "encryption": True,
                "size": True,
                "version": True,
            },
        ),
    ],
)
def test_unsafe_live_baselines_are_rejected(
    snapshot: capture.BaselineSnapshot,
    canary: bytes,
) -> None:
    with pytest.raises(capture.UploadIntegrityPreflightError):
        capture._baseline_document(snapshot, canary)


def test_current_consumer_validation_runs_before_output_write(
    private_workspace: Path,
    canary: bytes,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    deployment_path = private_workspace / "consumer" / "deployment.json"
    deployment_sha256 = _write(deployment_path, _deployment())
    wrote = False

    def rejected(_value: object) -> Any:
        raise consumer.Phase66EdgeObservationError("rejected")

    def forbidden_write(_path: Path, _value: object) -> tuple[int, str]:
        nonlocal wrote
        wrote = True
        raise AssertionError("write must not run")

    monkeypatch.setattr(consumer, "_validate_baseline_document", rejected)
    monkeypatch.setattr(capture, "_write_once", forbidden_write)
    with pytest.raises(capture.UploadIntegrityPreflightError, match="current exact consumer"):
        capture.capture_phase66_upload_integrity_preflight(
            deployment_authority_path=deployment_path,
            deployment_authority_sha256=deployment_sha256,
            output_path=private_workspace / "consumer" / "baseline.json",
            backend_factory=FakeBackend,
            canary_factory=lambda: canary,
        )
    assert wrote is False


class _Sts:
    def get_caller_identity(self) -> dict[str, str]:
        return {"Account": capture.ACCOUNT_ID, "Arn": capture.EXPECTED_CALLER_ARN}


class _Dynamo:
    def __init__(self, items: tuple[tuple[str, dict[str, Any]], ...]) -> None:
        midpoint = len(items) // 2
        self.pages = (items[:midpoint], items[midpoint:])
        self.calls: list[dict[str, Any]] = []

    def scan(self, **request: Any) -> dict[str, object]:
        self.calls.append(request)
        page = 1 if "ExclusiveStartKey" in request else 0
        items = [
            {
                "entity_type": {"S": entity},
                "payload": {"S": json.dumps(payload, sort_keys=True, separators=(",", ":"))},
            }
            for entity, payload in self.pages[page]
        ]
        result: dict[str, object] = {
            "Count": len(items),
            "Items": items,
            "ScannedCount": len(items),
        }
        if page == 0:
            result["LastEvaluatedKey"] = {"PK": {"S": "private-page-token"}}
        return result


class _S3:
    def __init__(self) -> None:
        self.inventory_calls: list[dict[str, Any]] = []

    def list_object_versions(self, **request: Any) -> dict[str, object]:
        self.inventory_calls.append(request)
        if "KeyMarker" not in request:
            return {
                "DeleteMarkers": [],
                "IsTruncated": True,
                "NextKeyMarker": "private-next-key",
                "NextVersionIdMarker": "private-next-version",
                "Versions": [],
            }
        return {
            "DeleteMarkers": [],
            "IsTruncated": False,
            "Versions": [
                {
                    "ETag": '"etag-secret"',
                    "IsLatest": True,
                    "Key": request["Prefix"],
                    "LastModified": NOW,
                    "Size": capture.PRIMARY_SIZE,
                    "VersionId": VERSION_B,
                }
            ],
        }

    def head_object(self, **request: Any) -> dict[str, object]:
        return {
            "ChecksumSHA256": base64_sha256(exact_phase66_canary_png()),
            "ContentLength": capture.PRIMARY_SIZE,
            "ContentType": "image/png",
            "ServerSideEncryption": "AES256",
            "VersionId": request["VersionId"],
        }

    def get_object_tagging(self, **_request: Any) -> dict[str, object]:
        return {"TagSet": [{"Key": "mr-lister-state", "Value": "pinned"}]}

    def get_bucket_versioning(self, **_request: Any) -> dict[str, str]:
        return {"Status": "Enabled"}


def base64_sha256(value: bytes) -> str:
    return base64.b64encode(sha256(value).digest()).decode("ascii")


class _StepFunctions:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def list_executions(self, **request: Any) -> dict[str, object]:
        self.calls.append(request)
        if len(self.calls) == 1:
            return {"executions": [], "nextToken": "private-workflow-token"}
        return {"executions": []}


class _Provider:
    def __init__(self, items: tuple[tuple[str, dict[str, Any]], ...]) -> None:
        self.dynamodb = _Dynamo(items)
        self.s3 = _S3()
        self.stepfunctions = _StepFunctions()
        self.services = {
            "cloudformation": object(),
            "dynamodb": self.dynamodb,
            "s3": self.s3,
            "stepfunctions": self.stepfunctions,
            "sts": _Sts(),
        }

    def client(self, service_name: str) -> object:
        return self.services.get(service_name, object())


def test_injected_aws_backend_fully_paginates_all_bounded_inventories(
    canary: bytes,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_snapshot = _snapshot()
    items = tuple((entity, dict(payload)) for entity, payload in source_snapshot.items[:4])
    provider = _Provider(items)
    backend = capture.AwsReadOnlyBackend(provider)
    bindings = {
        "ArtifactBucketName": capture.EXPECTED_BUCKET_NAME,
        "DeploymentReadiness": "WEB_EDGE_ACTIVE_DRAFT_ONLY",
        "StateTableName": capture.EXPECTED_TABLE_NAME,
        **{
            output: f"arn:aws:states:{capture.REGION}:{capture.ACCOUNT_ID}:stateMachine:{name}"
            for output, name in capture._WORKFLOW_OUTPUTS.items()
        },
    }
    monkeypatch.setattr(backend, "_deployment_bindings", lambda _deployment: bindings)
    deployment = revalidation._DeploymentAuthorityDocument.model_validate(_deployment())

    snapshot = backend.capture_baseline(deployment, canary)
    document = capture._baseline_document(snapshot, canary)

    assert document["table_record_count"] == 4
    assert len(provider.dynamodb.calls) == 2
    assert provider.dynamodb.calls[1]["ExclusiveStartKey"] == {"PK": {"S": "private-page-token"}}
    assert len(provider.s3.inventory_calls) == 2
    assert provider.s3.inventory_calls[1]["KeyMarker"] == "private-next-key"
    assert provider.s3.inventory_calls[1]["VersionIdMarker"] == "private-next-version"
    assert len(provider.stepfunctions.calls) == len(capture._WORKFLOW_OUTPUTS) + 1
    assert provider.stepfunctions.calls[1]["nextToken"] == "private-workflow-token"


def test_injected_aws_backend_rejects_root_identity_before_deployment_reads(
    canary: bytes,
) -> None:
    provider = _Provider(tuple())
    provider.services["sts"] = type(
        "RootSts",
        (),
        {
            "get_caller_identity": lambda _self: {
                "Account": capture.ACCOUNT_ID,
                "Arn": f"arn:aws:iam::{capture.ACCOUNT_ID}:root",
            }
        },
    )()
    backend = capture.AwsReadOnlyBackend(provider)
    with pytest.raises(capture.UploadIntegrityPreflightError, match="non-root"):
        backend.capture_baseline(
            revalidation._DeploymentAuthorityDocument.model_validate(_deployment()),
            canary,
        )


def test_tool_exposes_no_mutating_aws_or_lambda_operation() -> None:
    source = Path(capture.__file__).read_text(encoding="utf-8")
    for forbidden in (
        ".delete_object(",
        ".invoke(",
        ".put_object(",
        ".start_execution(",
        ".stop_execution(",
        ".transact_write_items(",
        ".update_item(",
    ):
        assert forbidden not in source
