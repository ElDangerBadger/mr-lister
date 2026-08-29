from __future__ import annotations

import json
import stat
from copy import deepcopy
from hashlib import sha256
from pathlib import Path

import pytest

import tools.render_phase6_shared_lambda_archive_refresh as refresh

NEW_RELEASE_FINGERPRINT = "1" * 64
NEW_ARCHIVE_SHA256 = "2" * 64
NEW_ARCHIVE_KEY = (
    f"private/deployments/lambda/releases/{NEW_RELEASE_FINGERPRINT}/"
    f"phase6-lambda-{NEW_ARCHIVE_SHA256}.zip"
)
NEW_ARCHIVE_VERSION = "newVersion_A1.verified"


def _canonical(value: object) -> bytes:
    return (
        json.dumps(value, allow_nan=False, indent=2, separators=(",", ": "), sort_keys=True) + "\n"
    ).encode()


def _old_code_uri() -> dict[str, str]:
    return {
        "Bucket": refresh.PREDECESSOR_LAMBDA_BUCKET,
        "Key": refresh.PREDECESSOR_LAMBDA_KEY,
        "Version": refresh.PREDECESSOR_LAMBDA_VERSION,
    }


def _predecessor() -> dict[str, object]:
    resources: dict[str, object] = {
        logical_id: {
            "Properties": {
                "CodeUri": _old_code_uri(),
                "Handler": f"phase6_lambda.{logical_id}",
                "Timeout": 30 if logical_id == "ReviewQueryApiFunction" else 15,
            },
            "Type": "AWS::Serverless::Function",
        }
        for logical_id in refresh._FUNCTION_LOGICAL_IDS
    }
    resources["UnchangedBucket"] = {
        "Properties": {"BucketEncryption": {"Rule": "unchanged"}},
        "Type": "AWS::S3::Bucket",
    }
    return {
        "AWSTemplateFormatVersion": "2010-09-09",
        "Metadata": {"ExistingAuthority": {"Mode": "web-edge-active-draft-only"}},
        "Outputs": {"Unchanged": {"Value": "exact"}},
        "Parameters": {
            "ReleaseFingerprint": {
                "AllowedPattern": "^[a-f0-9]{64}$",
                "AllowedValues": [refresh.PREDECESSOR_RELEASE_FINGERPRINT],
                "Default": refresh.PREDECESSOR_RELEASE_FINGERPRINT,
                "Type": "String",
            },
            "UnchangedParameter": {"Default": "exact", "Type": "String"},
        },
        "Resources": resources,
        "Transform": "AWS::Serverless-2016-10-31",
    }


def _binding(**overrides: str) -> refresh.Phase6SharedLambdaArchiveRefreshBinding:
    values = {
        "lambda_artifact_bucket": refresh.PREDECESSOR_LAMBDA_BUCKET,
        "lambda_artifact_key": NEW_ARCHIVE_KEY,
        "lambda_artifact_version": NEW_ARCHIVE_VERSION,
        "release_fingerprint": NEW_RELEASE_FINGERPRINT,
    }
    values.update(overrides)
    return refresh.Phase6SharedLambdaArchiveRefreshBinding(**values)


def _render(
    monkeypatch: pytest.MonkeyPatch,
    predecessor: dict[str, object],
) -> bytes:
    predecessor_raw = _canonical(predecessor)
    monkeypatch.setattr(
        refresh,
        "PREDECESSOR_TEMPLATE_SHA256",
        sha256(predecessor_raw).hexdigest(),
    )
    return refresh.render_phase6_shared_lambda_archive_refresh(predecessor_raw, _binding())


def test_render_changes_exactly_all_shared_code_bindings_and_release_parameter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    predecessor = _predecessor()

    rendered = _render(monkeypatch, predecessor)
    target = json.loads(rendered)

    assert rendered == _canonical(target)
    expected_code_uri = {
        "Bucket": refresh.PREDECESSOR_LAMBDA_BUCKET,
        "Key": NEW_ARCHIVE_KEY,
        "Version": NEW_ARCHIVE_VERSION,
    }
    for logical_id in refresh._FUNCTION_LOGICAL_IDS:
        assert target["Resources"][logical_id]["Properties"]["CodeUri"] == expected_code_uri
    assert target["Parameters"]["ReleaseFingerprint"]["Default"] == NEW_RELEASE_FINGERPRINT
    assert target["Parameters"]["ReleaseFingerprint"]["AllowedValues"] == [NEW_RELEASE_FINGERPRINT]
    assert target["Metadata"][refresh._METADATA_KEY] == {
        "ArchiveSha256": NEW_ARCHIVE_SHA256,
        "CodeUri": expected_code_uri,
        "Format": refresh.SHARED_LAMBDA_ARCHIVE_REFRESH_FORMAT,
        "PredecessorTemplateSha256": sha256(_canonical(predecessor)).hexdigest(),
        "ReleaseFingerprint": NEW_RELEASE_FINGERPRINT,
        "Resources": list(refresh._FUNCTION_LOGICAL_IDS),
    }

    expected_paths = {
        ("Metadata", refresh._METADATA_KEY),
        ("Parameters", "ReleaseFingerprint", "AllowedValues", "0"),
        ("Parameters", "ReleaseFingerprint", "Default"),
    }
    for logical_id in refresh._FUNCTION_LOGICAL_IDS:
        expected_paths.add(("Resources", logical_id, "Properties", "CodeUri", "Key"))
        expected_paths.add(("Resources", logical_id, "Properties", "CodeUri", "Version"))
    assert refresh._changed_paths(predecessor, target) == expected_paths

    restored = deepcopy(target)
    restored["Metadata"].pop(refresh._METADATA_KEY)
    restored_release = restored["Parameters"]["ReleaseFingerprint"]
    restored_release["Default"] = refresh.PREDECESSOR_RELEASE_FINGERPRINT
    restored_release["AllowedValues"] = [refresh.PREDECESSOR_RELEASE_FINGERPRINT]
    for logical_id in refresh._FUNCTION_LOGICAL_IDS:
        restored["Resources"][logical_id]["Properties"]["CodeUri"] = _old_code_uri()
    assert restored == predecessor


def test_render_is_deterministic(monkeypatch: pytest.MonkeyPatch) -> None:
    predecessor = _predecessor()
    predecessor_raw = _canonical(predecessor)
    monkeypatch.setattr(
        refresh,
        "PREDECESSOR_TEMPLATE_SHA256",
        sha256(predecessor_raw).hexdigest(),
    )
    binding = _binding()

    first = refresh.render_phase6_shared_lambda_archive_refresh(predecessor_raw, binding)
    second = refresh.render_phase6_shared_lambda_archive_refresh(predecessor_raw, binding)

    assert first == second
    assert sha256(first).hexdigest() == sha256(second).hexdigest()


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("lambda_artifact_bucket", "another-valid-bucket"),
        ("lambda_artifact_bucket", " MR-Lister "),
        (
            "lambda_artifact_key",
            "private/deployments/lambda/releases/"
            f"{'3' * 64}/phase6-lambda-{NEW_ARCHIVE_SHA256}.zip",
        ),
        (
            "lambda_artifact_key",
            f"private/deployments/lambda/releases/{NEW_RELEASE_FINGERPRINT}/latest.zip",
        ),
        (
            "lambda_artifact_key",
            f"private/deployments/lambda/releases/{NEW_RELEASE_FINGERPRINT}/"
            f"phase6-lambda-{'0' * 64}.zip",
        ),
        ("lambda_artifact_version", "latest"),
        ("lambda_artifact_version", " version-with-whitespace "),
        ("lambda_artifact_version", refresh.PREDECESSOR_LAMBDA_VERSION),
        ("release_fingerprint", "A" * 64),
        ("release_fingerprint", "0" * 64),
        ("release_fingerprint", refresh.PREDECESSOR_RELEASE_FINGERPRINT),
    ),
)
def test_binding_rejects_malformed_or_nonmatching_artifact_identity(
    field: str,
    value: str,
) -> None:
    with pytest.raises(refresh.Phase6SharedLambdaArchiveRefreshError):
        _binding(**{field: value})


@pytest.mark.parametrize(
    "mutation",
    (
        "missing_function",
        "extra_function",
        "nonuniform_code_uri",
        "release_default",
        "release_allowed_values",
        "existing_refresh_metadata",
    ),
)
def test_render_rejects_semantically_drifted_predecessor(
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
) -> None:
    predecessor = _predecessor()
    resources = predecessor["Resources"]
    release = predecessor["Parameters"]["ReleaseFingerprint"]
    if mutation == "missing_function":
        resources.pop("UploadApiFunction")
    elif mutation == "extra_function":
        resources["UnexpectedFunction"] = {
            "Properties": {"CodeUri": _old_code_uri()},
            "Type": "AWS::Serverless::Function",
        }
    elif mutation == "nonuniform_code_uri":
        resources["UploadApiFunction"]["Properties"]["CodeUri"]["Version"] = "drifted-version"
    elif mutation == "release_default":
        release["Default"] = "3" * 64
    elif mutation == "release_allowed_values":
        release["AllowedValues"] = ["3" * 64]
    else:
        predecessor["Metadata"][refresh._METADATA_KEY] = {"Format": "unexpected"}

    with pytest.raises(refresh.Phase6SharedLambdaArchiveRefreshError):
        _render(monkeypatch, predecessor)


def test_render_rejects_byte_drift_from_the_sealed_predecessor() -> None:
    with pytest.raises(refresh.Phase6SharedLambdaArchiveRefreshError):
        refresh.render_phase6_shared_lambda_archive_refresh(
            _canonical(_predecessor()),
            _binding(),
        )


def test_render_rejects_noncanonical_or_duplicate_predecessor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    predecessor = _predecessor()
    malformed_values = (
        json.dumps(predecessor, sort_keys=True, separators=(",", ":")).encode(),
        b'{"Metadata":{},"Metadata":{},"Parameters":{},"Resources":{}}',
    )
    for predecessor_raw in malformed_values:
        monkeypatch.setattr(
            refresh,
            "PREDECESSOR_TEMPLATE_SHA256",
            sha256(predecessor_raw).hexdigest(),
        )
        with pytest.raises(refresh.Phase6SharedLambdaArchiveRefreshError):
            refresh.render_phase6_shared_lambda_archive_refresh(
                predecessor_raw,
                _binding(),
            )


def _bind_private_write(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> tuple[Path, bytes]:
    predecessor = _predecessor()
    predecessor_raw = _canonical(predecessor)
    repository = tmp_path / "repository"
    input_root = repository / "input"
    input_root.mkdir(parents=True)
    predecessor_path = input_root / "predecessor.json"
    predecessor_path.write_bytes(predecessor_raw)
    output_path = repository / ".mr_lister_private/refresh/target.json"
    monkeypatch.setattr(refresh, "REPOSITORY_ROOT", repository)
    monkeypatch.setattr(refresh, "DEFAULT_PREDECESSOR_PATH", predecessor_path)
    monkeypatch.setattr(refresh, "DEFAULT_OUTPUT_PATH", output_path)
    monkeypatch.setattr(
        refresh,
        "PREDECESSOR_TEMPLATE_SHA256",
        sha256(predecessor_raw).hexdigest(),
    )
    rendered = refresh.render_phase6_shared_lambda_archive_refresh(predecessor_raw, _binding())
    return output_path, rendered


def test_private_write_is_create_or_identical_and_owner_only(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    output_path, rendered = _bind_private_write(monkeypatch, tmp_path)

    assert refresh.write_phase6_shared_lambda_archive_refresh(_binding()) == output_path
    assert output_path.read_bytes() == rendered
    assert stat.S_IMODE(output_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(output_path.parent.stat().st_mode) == 0o700
    assert refresh.write_phase6_shared_lambda_archive_refresh(_binding()) == output_path

    output_path.write_bytes(b"different\n")
    with pytest.raises(refresh.Phase6SharedLambdaArchiveRefreshError):
        refresh.write_phase6_shared_lambda_archive_refresh(_binding())


def test_private_write_rejects_symlinked_output_parent(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    output_path, _ = _bind_private_write(monkeypatch, tmp_path)
    outside = tmp_path / "outside-output"
    outside.mkdir()
    (refresh.REPOSITORY_ROOT / ".mr_lister_private").symlink_to(
        outside,
        target_is_directory=True,
    )

    with pytest.raises(refresh.Phase6SharedLambdaArchiveRefreshError):
        refresh.write_phase6_shared_lambda_archive_refresh(_binding())
    assert not (outside / "refresh/target.json").exists()
    assert not output_path.exists()


def test_private_write_rejects_symlinked_predecessor_parent(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    output_path, _ = _bind_private_write(monkeypatch, tmp_path)
    outside = tmp_path / "outside-input"
    outside.mkdir()
    external_predecessor = outside / "predecessor.json"
    external_predecessor.write_bytes(refresh.DEFAULT_PREDECESSOR_PATH.read_bytes())
    refresh.DEFAULT_PREDECESSOR_PATH.unlink()
    refresh.DEFAULT_PREDECESSOR_PATH.parent.rmdir()
    refresh.DEFAULT_PREDECESSOR_PATH.parent.symlink_to(outside, target_is_directory=True)

    with pytest.raises(refresh.Phase6SharedLambdaArchiveRefreshError):
        refresh.write_phase6_shared_lambda_archive_refresh(_binding())
    assert not output_path.exists()


def test_predecessor_identity_and_function_count_are_fixed() -> None:
    assert refresh.PREDECESSOR_TEMPLATE_SHA256 == (
        "618fbca8d00b1edbfa7412668a6e7d2a0e4e65e23460ee8b9216f92f19dbdfc2"
    )
    assert len(refresh._FUNCTION_LOGICAL_IDS) == 10
    assert len(set(refresh._FUNCTION_LOGICAL_IDS)) == 10
