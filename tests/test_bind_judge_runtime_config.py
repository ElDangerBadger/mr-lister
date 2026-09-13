"""Local companion configuration must preserve the existing seller authority."""

from __future__ import annotations

import json
import stat
from hashlib import sha256
from pathlib import Path

import pytest
from test_bind_phase6_runtime_config import (
    _canonical,
    _capture,
    _document,
    _outputs,
    _repository,
    _runtime_config,
)

from tools.bind_judge_runtime_config import (
    JUDGE_OUTPUT_KEYS,
    MANIFEST_FORMAT,
    OBJECT_KEY,
    JudgeRuntimeConfigBindingError,
    render_judge_runtime_config,
    write_judge_runtime_config,
)
from tools.bind_phase6_runtime_config import APPLICATION_ORIGIN, COGNITO_ORIGIN

JUDGE_STACK_NAME = "mr-lister-judge-access-dev"
JUDGE_STACK_ID = (
    "arn:aws:cloudformation:us-west-2:384627057108:stack/"
    f"{JUDGE_STACK_NAME}/e2345678-1234-5678-1234-123456789abc"
)
JUDGE_POOL_ID = "us-west-2_Judge123"
JUDGE_CLIENT_ID = "judgeclient12345678"
JUDGE_ORIGIN = "https://mr-lister-judge-dev.auth.us-west-2.amazoncognito.com"
JOB_ID = "job_359a6a3e3abb578589d79b296b2b5a88"


def _judge_values() -> dict[str, str]:
    return {
        "JudgeUserPoolId": JUDGE_POOL_ID,
        "JudgeBrokerClientId": JUDGE_CLIENT_ID,
        "JudgeBrokerClientSecretArn": (
            "arn:aws:secretsmanager:us-west-2:384627057108:secret:"
            "mr-lister/dev/judge-access/broker-client-AbCd12"
        ),
        "JudgeSignInOrigin": JUDGE_ORIGIN,
        "JudgeIssuer": f"https://cognito-idp.us-west-2.amazonaws.com/{JUDGE_POOL_ID}",
        "PrimaryBrokerCallback": f"{COGNITO_ORIGIN}/oauth2/idpresponse",
        "JudgeApplicationCallback": f"{APPLICATION_ORIGIN}/judge/auth/callback",
        "JudgePrimaryLogoutRelay": f"{APPLICATION_ORIGIN}/judge/signout",
        "JudgeSignedOutDestination": f"{APPLICATION_ORIGIN}/judge/",
        "IdentityProviderName": "MrListerJudge",
    }


def _judge_document(values: dict[str, str] | None = None) -> dict[str, object]:
    return {
        "StackId": JUDGE_STACK_ID,
        "StackName": JUDGE_STACK_NAME,
        "StackStatus": "UPDATE_COMPLETE",
        "Outputs": [
            {"OutputKey": key, "OutputValue": value}
            for key, value in sorted((values or _judge_values()).items())
        ],
    }


@pytest.fixture
def inputs(tmp_path: Path) -> tuple[Path, Path, Path]:
    repository = _repository(tmp_path)
    normal = _capture(repository)
    judge = normal.parent / "judge-stack-outputs.json"
    judge.write_bytes(_canonical(_judge_document()))
    return repository, normal, judge


def _render(inputs: tuple[Path, Path, Path], **kwargs: object) -> tuple[bytes, bytes]:
    repository, normal, judge = inputs
    return render_judge_runtime_config(
        normal,
        judge,
        repository_root=repository,
        expected_judge_stack_id=JUDGE_STACK_ID,
        **kwargs,
    )


def test_companion_keeps_primary_authority_and_binds_upstream_logout(inputs) -> None:
    runtime_raw, manifest_raw = _render(inputs)
    runtime, manifest = json.loads(runtime_raw), json.loads(manifest_raw)
    expected = _runtime_config()
    expected["redirect_uri"] = f"{APPLICATION_ORIGIN}/judge/auth/callback"
    expected["judge_access"] = {
        "identity_provider": "MrListerJudge",
        "upstream_logout_url": f"{JUDGE_ORIGIN}/logout",
        "upstream_client_id": JUDGE_CLIENT_ID,
    }
    assert runtime == expected
    assert runtime_raw == _canonical(runtime)
    assert manifest_raw == _canonical(manifest)
    assert manifest == {
        "algorithm": "sha256",
        "cache_control": "private, no-store, max-age=0",
        "content_type": "application/json",
        "format": MANIFEST_FORMAT,
        "object_key": OBJECT_KEY,
        "sha256": sha256(runtime_raw).hexdigest(),
        "size_bytes": len(runtime_raw),
        "normal_runtime_config_sha256": sha256(_canonical(_runtime_config())).hexdigest(),
        "judge_stack_capture_sha256": sha256(inputs[2].read_bytes()).hexdigest(),
        "judge_stack_id": JUDGE_STACK_ID,
    }
    assert b"SecretArn" not in runtime_raw + manifest_raw
    assert b"secretsmanager" not in runtime_raw + manifest_raw


def test_optional_prepared_job_is_validated_but_grants_no_authority(inputs) -> None:
    runtime_raw, _ = _render(inputs, prepared_job_id=JOB_ID)
    assert json.loads(runtime_raw)["judge_access"]["prepared_job_id"] == JOB_ID
    assert set(json.loads(runtime_raw)) == set(_runtime_config()) | {"judge_access"}


@pytest.mark.parametrize("value", ["", "other-job", "../job_bad", JOB_ID + "?x=1", 42, True])
def test_invalid_prepared_job_is_rejected(inputs, value) -> None:
    with pytest.raises(JudgeRuntimeConfigBindingError):
        _render(inputs, prepared_job_id=value)


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("JudgeUserPoolId", "us-east-1_Judge123"),
        ("JudgeUserPoolId", _outputs()["SellerUserPoolId"]),
        ("JudgeBrokerClientId", _outputs()["SellerUserPoolClientId"]),
        ("JudgeBrokerClientId", "client-secret with spaces"),
        ("JudgeBrokerClientSecretArn", "private-plaintext-credential"),
        ("JudgeSignInOrigin", COGNITO_ORIGIN),
        ("JudgeSignInOrigin", JUDGE_ORIGIN + "/logout"),
        ("JudgeSignInOrigin", JUDGE_ORIGIN + ":443"),
        ("JudgeSignInOrigin", JUDGE_ORIGIN + "?return_to=elsewhere"),
        ("JudgeSignInOrigin", "https://evil.invalid"),
        ("JudgeIssuer", "https://cognito-idp.us-west-2.amazonaws.com/us-west-2_Wrong"),
        ("PrimaryBrokerCallback", f"{APPLICATION_ORIGIN}/auth/callback"),
        ("JudgeApplicationCallback", f"{APPLICATION_ORIGIN}/auth/callback"),
        ("JudgePrimaryLogoutRelay", f"{APPLICATION_ORIGIN}/judge/"),
        ("JudgeSignedOutDestination", f"{APPLICATION_ORIGIN}/judge/signout"),
        ("IdentityProviderName", "COGNITO"),
    ],
)
def test_each_bound_output_rejects_authority_drift(inputs, key, value) -> None:
    values = _judge_values()
    values[key] = value
    inputs[2].write_bytes(_canonical(_judge_document(values)))
    with pytest.raises(JudgeRuntimeConfigBindingError) as error:
        _render(inputs)
    assert str(error.value) == "Judge runtime configuration binding is invalid"


@pytest.mark.parametrize("key", ["client_secret", "password", "provider_secret", "access_token"])
def test_secret_fields_rejected_without_echoing_value(inputs, key) -> None:
    values = _judge_values()
    values[key] = "never-echo-this-sensitive-value"
    inputs[2].write_bytes(_canonical(_judge_document(values)))
    with pytest.raises(JudgeRuntimeConfigBindingError) as error:
        _render(inputs)
    assert "sensitive" not in str(error.value)


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("StackId", JUDGE_STACK_ID + "other"),
        ("StackName", "other"),
        ("StackStatus", "UPDATE_IN_PROGRESS"),
    ],
)
def test_exact_stack_identity_and_stable_status_are_required(inputs, key, value) -> None:
    document = _judge_document()
    document[key] = value
    inputs[2].write_bytes(_canonical(document))
    with pytest.raises(JudgeRuntimeConfigBindingError):
        _render(inputs)


def test_disabled_federation_duplicate_output_and_noncanonical_json_are_rejected(inputs) -> None:
    values = _judge_values()
    del values["IdentityProviderName"]
    inputs[2].write_bytes(_canonical(_judge_document(values)))
    with pytest.raises(JudgeRuntimeConfigBindingError):
        _render(inputs)
    document = _judge_document()
    document["Outputs"][-1] = document["Outputs"][0]
    inputs[2].write_bytes(_canonical(document))
    with pytest.raises(JudgeRuntimeConfigBindingError):
        _render(inputs)
    inputs[2].write_text(json.dumps(_judge_document(), indent=2))
    with pytest.raises(JudgeRuntimeConfigBindingError):
        _render(inputs)


def test_existing_seller_binder_rejects_normal_capture_drift(inputs) -> None:
    values = _outputs()
    values["SellerApplicationOrigin"] = "https://other.invalid"
    inputs[1].write_bytes(_canonical(_document(values)))
    with pytest.raises(JudgeRuntimeConfigBindingError):
        _render(inputs)


def test_private_create_only_write_preserves_normal_and_static_files(inputs) -> None:
    repository, normal, judge = inputs
    before = normal.read_bytes()
    destination = repository / ".mr_lister_private" / "judge-release"
    artifact = write_judge_runtime_config(
        normal,
        judge,
        destination,
        expected_judge_stack_id=JUDGE_STACK_ID,
        repository_root=repository,
    )
    assert artifact.runtime_config_path == destination / OBJECT_KEY
    assert artifact.sha256 == sha256(artifact.runtime_config_path.read_bytes()).hexdigest()
    assert artifact.size_bytes == artifact.runtime_config_path.stat().st_size
    assert artifact.object_key == OBJECT_KEY
    for path in (artifact.runtime_config_path, artifact.upload_manifest_path):
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert normal.read_bytes() == before
    assert not (repository / "web").exists()
    with pytest.raises(JudgeRuntimeConfigBindingError):
        write_judge_runtime_config(
            normal,
            judge,
            destination,
            expected_judge_stack_id=JUDGE_STACK_ID,
            repository_root=repository,
        )


def test_existing_receipt_prevents_partial_write(inputs) -> None:
    repository, normal, judge = inputs
    destination = normal.parent / "judge-release"
    (destination / "judge").mkdir(parents=True)
    receipt = destination / "judge/runtime-config.upload.json"
    receipt.write_bytes(b"existing")
    with pytest.raises(JudgeRuntimeConfigBindingError):
        write_judge_runtime_config(
            normal,
            judge,
            destination,
            expected_judge_stack_id=JUDGE_STACK_ID,
            repository_root=repository,
        )
    assert receipt.read_bytes() == b"existing"
    assert not (destination / OBJECT_KEY).exists()


def test_destinations_and_input_symlinks_cannot_escape_private_boundary(inputs, tmp_path) -> None:
    repository, normal, judge = inputs
    for destination in (repository / "web/dist", tmp_path / "outside"):
        with pytest.raises(JudgeRuntimeConfigBindingError):
            write_judge_runtime_config(
                normal,
                judge,
                destination,
                expected_judge_stack_id=JUDGE_STACK_ID,
                repository_root=repository,
            )
    alias = normal.parent / "alias"
    alias.symlink_to(normal.parent, target_is_directory=True)
    with pytest.raises(JudgeRuntimeConfigBindingError):
        write_judge_runtime_config(
            normal,
            judge,
            alias / "new-output",
            expected_judge_stack_id=JUDGE_STACK_ID,
            repository_root=repository,
        )
    with pytest.raises(JudgeRuntimeConfigBindingError):
        render_judge_runtime_config(
            normal,
            alias / judge.name,
            expected_judge_stack_id=JUDGE_STACK_ID,
            repository_root=repository,
        )


def test_template_outputs_and_binder_allowlist_remain_aligned() -> None:
    root = Path(__file__).resolve().parents[1]
    template = json.loads((root / "infra/phase6/judge-access/template.json").read_text())
    assert set(template["Outputs"]) == JUDGE_OUTPUT_KEYS
