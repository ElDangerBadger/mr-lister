"""Credential-free Phase 6 stack-output to browser-runtime binding tests."""

from __future__ import annotations

import json
import stat
from copy import deepcopy
from hashlib import sha256
from pathlib import Path

import pytest

from tools.bind_phase6_runtime_config import (
    APPLICATION_ORIGIN,
    COGNITO_ORIGIN,
    EXPECTED_CLOUDFORMATION_OUTPUT_KEYS,
    RUNTIME_CONFIG_CACHE_CONTROL,
    RUNTIME_CONFIG_CONTENT_TYPE,
    RUNTIME_CONFIG_OBJECT_KEY,
    STACK_ID,
    STACK_NAME,
    STACK_STATUS,
    UPLOAD_MANIFEST_FORMAT,
    Phase6RuntimeConfigBindingError,
    load_phase6_web_stack_outputs,
    render_phase6_runtime_config,
    write_phase6_runtime_config,
)

CLIENT_ID = "4client1234567890abcdef"


def _canonical(value: object) -> bytes:
    return (
        json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    ).encode()


def _runtime_config() -> dict[str, object]:
    return {
        "cognito_authorize_url": f"{COGNITO_ORIGIN}/oauth2/authorize",
        "cognito_token_url": f"{COGNITO_ORIGIN}/oauth2/token",
        "cognito_logout_url": f"{COGNITO_ORIGIN}/logout",
        "client_id": CLIENT_ID,
        "redirect_uri": f"{APPLICATION_ORIGIN}/auth/callback",
        "scopes": ["openid", "mr-lister-api/seller"],
    }


def _cloudformation_runtime(value: dict[str, object]) -> str:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=True,
        separators=(",", ":"),
    )


def _outputs() -> dict[str, str]:
    values = {
        "ArtifactBucketBrowserOrigin": (
            "https://mr-lister-phase6-artifacts-dev-384627057108-us-west-2."
            "s3.us-west-2.amazonaws.com"
        ),
        "ArtifactBucketName": "mr-lister-phase6-artifacts-dev-384627057108-us-west-2",
        "DeploymentReadiness": "WEB_EDGE_ACTIVE_DRAFT_ONLY",
        "OperationalAlarmTopicArn": (
            "arn:aws:sns:us-west-2:384627057108:mr-lister-phase6-dev-operational-alarms"
        ),
        "PrepareStateMachineArn": (
            "arn:aws:states:us-west-2:384627057108:stateMachine:mr-lister-phase6-dev-prepare"
        ),
        "ReconcileProductStateMachineArn": (
            "arn:aws:states:us-west-2:384627057108:stateMachine:"
            "mr-lister-phase6-dev-reconcile-product"
        ),
        "RefreshEconomicsStateMachineArn": (
            "arn:aws:states:us-west-2:384627057108:stateMachine:"
            "mr-lister-phase6-dev-refresh-economics"
        ),
        "SellerApiOrigin": "https://a1b2c3d4.execute-api.us-west-2.amazonaws.com",
        "SellerApplicationOrigin": APPLICATION_ORIGIN,
        "SellerRuntimeConfig": _cloudformation_runtime(_runtime_config()),
        "SellerRuntimeConfigObjectKey": RUNTIME_CONFIG_OBJECT_KEY,
        "SellerSignInOrigin": COGNITO_ORIGIN,
        "SellerUserPoolClientId": CLIENT_ID,
        "SellerUserPoolId": "us-west-2_AbCdEf123",
        "SellerWebAssetBucketName": "mr-lister-phase6-web-dev-384627057108-us-west-2",
        "SellerWebDistributionDomainName": "d1234567890abc.cloudfront.net",
        "SellerWebDistributionId": "E1234567890ABC",
        "StateTableName": STACK_NAME,
        "SynchronizeProductStateMachineArn": (
            "arn:aws:states:us-west-2:384627057108:stateMachine:"
            "mr-lister-phase6-dev-synchronize-product"
        ),
    }
    assert set(values) == EXPECTED_CLOUDFORMATION_OUTPUT_KEYS
    return values


def _document(outputs: dict[str, str] | None = None) -> dict[str, object]:
    values = outputs or _outputs()
    return {
        "Outputs": [
            {"OutputKey": key, "OutputValue": value} for key, value in sorted(values.items())
        ],
        "StackId": STACK_ID,
        "StackName": STACK_NAME,
        "StackStatus": STACK_STATUS,
    }


def _capture(repository: Path, document: dict[str, object] | None = None) -> Path:
    path = repository / ".mr_lister_private" / "web-stack-outputs.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(_canonical(document or _document()))
    return path


def _repository(tmp_path: Path) -> Path:
    repository = tmp_path / "repository"
    repository.mkdir()
    return repository


def test_canonical_capture_renders_exact_six_field_runtime_and_upload_manifest(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    capture = _capture(repository)

    runtime_raw, manifest_raw = render_phase6_runtime_config(
        capture,
        repository_root=repository,
    )
    runtime = json.loads(runtime_raw)
    manifest = json.loads(manifest_raw)

    assert runtime_raw == _canonical(runtime)
    assert runtime == _runtime_config()
    assert set(runtime) == {
        "cognito_authorize_url",
        "cognito_token_url",
        "cognito_logout_url",
        "client_id",
        "redirect_uri",
        "scopes",
    }
    assert manifest_raw == _canonical(manifest)
    assert manifest == {
        "algorithm": "sha256",
        "cache_control": RUNTIME_CONFIG_CACHE_CONTROL,
        "content_type": RUNTIME_CONFIG_CONTENT_TYPE,
        "format": UPLOAD_MANIFEST_FORMAT,
        "object_key": RUNTIME_CONFIG_OBJECT_KEY,
        "sha256": sha256(runtime_raw).hexdigest(),
        "size_bytes": len(runtime_raw),
    }


def test_write_is_private_create_only_and_verifies_both_files(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    capture = _capture(repository)
    destination = Path(".mr_lister_private/phase6-web-runtime")

    artifact = write_phase6_runtime_config(
        capture,
        destination,
        repository_root=repository,
    )
    runtime_raw, manifest_raw = render_phase6_runtime_config(
        capture,
        repository_root=repository,
    )

    assert artifact.runtime_config_path.read_bytes() == runtime_raw
    assert artifact.upload_manifest_path.read_bytes() == manifest_raw
    assert artifact.sha256 == sha256(runtime_raw).hexdigest()
    assert artifact.size_bytes == len(runtime_raw)
    assert artifact.content_type == RUNTIME_CONFIG_CONTENT_TYPE
    assert artifact.cache_control == RUNTIME_CONFIG_CACHE_CONTROL
    assert artifact.object_key == RUNTIME_CONFIG_OBJECT_KEY
    assert stat.S_IMODE(artifact.runtime_config_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(artifact.upload_manifest_path.stat().st_mode) == 0o600
    with pytest.raises(Phase6RuntimeConfigBindingError):
        write_phase6_runtime_config(capture, destination, repository_root=repository)


def test_existing_companion_prevents_partial_runtime_write(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    capture = _capture(repository)
    destination = repository / ".mr_lister_private" / "phase6-web-runtime"
    destination.mkdir()
    companion = destination / "runtime-config.upload.json"
    companion.write_bytes(b"existing")

    with pytest.raises(Phase6RuntimeConfigBindingError):
        write_phase6_runtime_config(capture, destination, repository_root=repository)
    assert not (destination / "runtime-config.json").exists()
    assert companion.read_bytes() == b"existing"


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("redirect_uri", "https://massskutiny.com/other"),
        ("scopes", ["mr-lister-api/seller", "openid"]),
        ("scopes", ["openid", "mr-lister-api/seller", "admin"]),
        ("cognito_authorize_url", "https://example.com/oauth2/authorize"),
        ("client_id", "REPLACE_ME"),
    ),
)
def test_runtime_callback_scope_authority_and_placeholders_are_rejected(
    tmp_path: Path,
    field: str,
    value: object,
) -> None:
    repository = _repository(tmp_path)
    outputs = _outputs()
    runtime = _runtime_config()
    runtime[field] = value
    outputs["SellerRuntimeConfig"] = _cloudformation_runtime(runtime)
    capture = _capture(repository, _document(outputs))

    with pytest.raises(Phase6RuntimeConfigBindingError):
        render_phase6_runtime_config(capture, repository_root=repository)


def test_runtime_credentials_and_extra_fields_are_rejected(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    outputs = _outputs()
    runtime = _runtime_config()
    runtime["client_secret"] = "must-not-ship"
    outputs["SellerRuntimeConfig"] = _cloudformation_runtime(runtime)
    capture = _capture(repository, _document(outputs))

    with pytest.raises(Phase6RuntimeConfigBindingError):
        render_phase6_runtime_config(capture, repository_root=repository)


@pytest.mark.parametrize(
    ("output", "value"),
    (
        ("DeploymentReadiness", "CORE_RUNTIME_ACTIVE_DRAFT_ONLY"),
        ("SellerApplicationOrigin", "https://www.massskutiny.com"),
        ("SellerSignInOrigin", "https://other.auth.us-west-2.amazoncognito.com"),
        ("SellerRuntimeConfigObjectKey", "other.json"),
        ("SellerUserPoolClientId", "differentclient123"),
        ("SellerWebAssetBucketName", "different-bucket"),
    ),
)
def test_runtime_config_must_cross_bind_exact_stack_outputs(
    tmp_path: Path,
    output: str,
    value: str,
) -> None:
    repository = _repository(tmp_path)
    outputs = _outputs()
    outputs[output] = value
    capture = _capture(repository, _document(outputs))

    with pytest.raises(Phase6RuntimeConfigBindingError):
        load_phase6_web_stack_outputs(capture, repository_root=repository)


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("StackId", STACK_ID + "-other"),
        ("StackName", "other-stack"),
        ("StackStatus", "UPDATE_ROLLBACK_COMPLETE"),
    ),
)
def test_capture_is_bound_to_exact_stack_identity(
    tmp_path: Path,
    field: str,
    value: str,
) -> None:
    repository = _repository(tmp_path)
    document = _document()
    document[field] = value
    capture = _capture(repository, document)

    with pytest.raises(Phase6RuntimeConfigBindingError):
        load_phase6_web_stack_outputs(capture, repository_root=repository)


def test_capture_rejects_extra_duplicate_unsorted_and_noncanonical_data(tmp_path: Path) -> None:
    repository = _repository(tmp_path)

    extra = _document()
    cast_outputs = extra["Outputs"]
    assert isinstance(cast_outputs, list)
    cast_outputs.append({"OutputKey": "Unexpected", "OutputValue": "value"})
    with pytest.raises(Phase6RuntimeConfigBindingError):
        load_phase6_web_stack_outputs(_capture(repository, extra), repository_root=repository)

    duplicate = _document()
    duplicate_outputs = duplicate["Outputs"]
    assert isinstance(duplicate_outputs, list)
    duplicate_outputs[-1] = deepcopy(duplicate_outputs[0])
    with pytest.raises(Phase6RuntimeConfigBindingError):
        load_phase6_web_stack_outputs(
            _capture(repository, duplicate),
            repository_root=repository,
        )

    unsorted = _document()
    unsorted_outputs = unsorted["Outputs"]
    assert isinstance(unsorted_outputs, list)
    unsorted_outputs[0], unsorted_outputs[1] = unsorted_outputs[1], unsorted_outputs[0]
    with pytest.raises(Phase6RuntimeConfigBindingError):
        load_phase6_web_stack_outputs(_capture(repository, unsorted), repository_root=repository)

    noncanonical_path = repository / ".mr_lister_private" / "noncanonical.json"
    noncanonical_path.write_text(json.dumps(_document(), indent=2), encoding="utf-8")
    with pytest.raises(Phase6RuntimeConfigBindingError):
        load_phase6_web_stack_outputs(noncanonical_path, repository_root=repository)


def test_capture_and_destination_cannot_escape_or_use_symlinks(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    capture = _capture(repository)
    outside = tmp_path / "outside.json"
    outside.write_bytes(capture.read_bytes())

    with pytest.raises(Phase6RuntimeConfigBindingError):
        load_phase6_web_stack_outputs(outside, repository_root=repository)
    with pytest.raises(Phase6RuntimeConfigBindingError):
        write_phase6_runtime_config(
            capture,
            repository / "public-runtime",
            repository_root=repository,
        )

    linked = repository / ".mr_lister_private" / "linked.json"
    linked.symlink_to(capture)
    with pytest.raises(Phase6RuntimeConfigBindingError):
        load_phase6_web_stack_outputs(linked, repository_root=repository)
