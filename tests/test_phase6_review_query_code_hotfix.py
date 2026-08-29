from __future__ import annotations

import json
import stat
from copy import deepcopy
from hashlib import sha256
from pathlib import Path

import pytest

import tools.render_phase6_review_query_code_hotfix as hotfix


def _canonical(value: object) -> bytes:
    return (
        json.dumps(value, allow_nan=False, indent=2, separators=(",", ": "), sort_keys=True) + "\n"
    ).encode()


def _old_code_uri() -> dict[str, str]:
    return {
        "Bucket": hotfix.PREDECESSOR_LAMBDA_BUCKET,
        "Key": hotfix.PREDECESSOR_LAMBDA_KEY,
        "Version": hotfix.PREDECESSOR_LAMBDA_VERSION,
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
        for logical_id in hotfix._FUNCTION_LOGICAL_IDS
    }
    review_properties = resources["ReviewQueryApiFunction"]["Properties"]
    review_properties["Environment"] = {
        "Variables": {
            "MR_LISTER_APPLICATION_ORIGIN": {"Ref": "ApplicationOrigin"},
            "MR_LISTER_PRODUCT_PROFILE_ID": "gildan_64000_swiftpod",
        }
    }
    resources["UnchangedBucket"] = {
        "Properties": {"BucketEncryption": {"Rule": "unchanged"}},
        "Type": "AWS::S3::Bucket",
    }
    return {
        "AWSTemplateFormatVersion": "2010-09-09",
        "Globals": {
            "Function": {
                "Environment": {
                    "Variables": {
                        "MR_LISTER_ENVIRONMENT": {"Ref": "EnvironmentName"},
                        "MR_LISTER_RELEASE_FINGERPRINT": {"Ref": "ReleaseFingerprint"},
                    }
                }
            }
        },
        "Metadata": {"ExistingAuthority": {"Mode": "web-edge-active-draft-only"}},
        "Outputs": {"Unchanged": {"Value": "exact"}},
        "Parameters": {
            "ReleaseFingerprint": {
                "AllowedPattern": "^[a-f0-9]{64}$",
                "AllowedValues": [hotfix.PREDECESSOR_RELEASE_FINGERPRINT],
                "Default": hotfix.PREDECESSOR_RELEASE_FINGERPRINT,
                "Type": "String",
            },
            "UnchangedParameter": {"Default": "exact", "Type": "String"},
        },
        "Resources": resources,
        "Transform": "AWS::Serverless-2016-10-31",
    }


def _binding(**overrides: str) -> hotfix.Phase6ReviewQueryCodeHotfixBinding:
    values = {
        "lambda_artifact_bucket": hotfix.TARGET_LAMBDA_BUCKET,
        "lambda_artifact_key": hotfix.TARGET_LAMBDA_KEY,
        "lambda_artifact_version": hotfix.TARGET_LAMBDA_VERSION,
        "release_fingerprint": hotfix.TARGET_RELEASE_FINGERPRINT,
    }
    values.update(overrides)
    return hotfix.Phase6ReviewQueryCodeHotfixBinding(**values)


def _expected_target(predecessor: dict[str, object]) -> dict[str, object]:
    target = deepcopy(predecessor)
    review_properties = target["Resources"]["ReviewQueryApiFunction"]["Properties"]
    review_properties["CodeUri"] = {
        "Bucket": hotfix.TARGET_LAMBDA_BUCKET,
        "Key": hotfix.TARGET_LAMBDA_KEY,
        "Version": hotfix.TARGET_LAMBDA_VERSION,
    }
    review_properties["Environment"]["Variables"]["MR_LISTER_RELEASE_FINGERPRINT"] = (
        hotfix.TARGET_RELEASE_FINGERPRINT
    )
    return target


def _render(
    monkeypatch: pytest.MonkeyPatch,
    predecessor: dict[str, object],
) -> bytes:
    predecessor_raw = _canonical(predecessor)
    monkeypatch.setattr(
        hotfix,
        "PREDECESSOR_TEMPLATE_SHA256",
        sha256(predecessor_raw).hexdigest(),
    )
    try:
        expected = _canonical(_expected_target(predecessor))
        target_sha256 = sha256(expected).hexdigest()
    except (KeyError, TypeError):
        target_sha256 = "0" * 64
    monkeypatch.setattr(
        hotfix,
        "REVIEW_QUERY_CODE_HOTFIX_TEMPLATE_SHA256",
        target_sha256,
    )
    return hotfix.render_phase6_review_query_code_hotfix(predecessor_raw, _binding())


def test_render_changes_only_review_query_code_and_release_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    predecessor = _predecessor()

    rendered = _render(monkeypatch, predecessor)
    target = json.loads(rendered)

    assert rendered == _canonical(target)
    review_variables = target["Resources"]["ReviewQueryApiFunction"]["Properties"]["Environment"][
        "Variables"
    ]
    assert target["Resources"]["ReviewQueryApiFunction"]["Properties"]["CodeUri"] == {
        "Bucket": hotfix.PREDECESSOR_LAMBDA_BUCKET,
        "Key": hotfix.TARGET_LAMBDA_KEY,
        "Version": hotfix.TARGET_LAMBDA_VERSION,
    }
    assert review_variables["MR_LISTER_RELEASE_FINGERPRINT"] == (hotfix.TARGET_RELEASE_FINGERPRINT)
    assert target["Globals"] == predecessor["Globals"]
    assert target["Parameters"] == predecessor["Parameters"]
    for logical_id in hotfix._FUNCTION_LOGICAL_IDS:
        if logical_id != "ReviewQueryApiFunction":
            assert target["Resources"][logical_id] == predecessor["Resources"][logical_id]
    assert target["Resources"]["UnchangedBucket"] == predecessor["Resources"]["UnchangedBucket"]
    assert target["Metadata"] == predecessor["Metadata"]

    assert hotfix._changed_paths(predecessor, target) == {
        ("Resources", "ReviewQueryApiFunction", "Properties", "CodeUri", "Key"),
        ("Resources", "ReviewQueryApiFunction", "Properties", "CodeUri", "Version"),
        (
            "Resources",
            "ReviewQueryApiFunction",
            "Properties",
            "Environment",
            "Variables",
            "MR_LISTER_RELEASE_FINGERPRINT",
        ),
    }

    restored = deepcopy(target)
    restored_review = restored["Resources"]["ReviewQueryApiFunction"]["Properties"]
    restored_review["CodeUri"] = _old_code_uri()
    restored_review["Environment"]["Variables"].pop("MR_LISTER_RELEASE_FINGERPRINT")
    assert restored == predecessor


def test_render_is_deterministic(monkeypatch: pytest.MonkeyPatch) -> None:
    predecessor = _predecessor()

    first = _render(monkeypatch, predecessor)
    second = _render(monkeypatch, predecessor)

    assert first == second
    assert sha256(first).hexdigest() == sha256(second).hexdigest()


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("lambda_artifact_bucket", "another-valid-bucket"),
        ("lambda_artifact_bucket", f" {hotfix.TARGET_LAMBDA_BUCKET} "),
        (
            "lambda_artifact_key",
            hotfix.TARGET_LAMBDA_KEY.replace(hotfix.TARGET_RELEASE_FINGERPRINT, "3" * 64),
        ),
        (
            "lambda_artifact_key",
            hotfix.TARGET_LAMBDA_KEY.replace(hotfix.TARGET_ARCHIVE_SHA256, "4" * 64),
        ),
        ("lambda_artifact_version", "differentVersion_A1.verified"),
        ("lambda_artifact_version", f" {hotfix.TARGET_LAMBDA_VERSION} "),
        ("release_fingerprint", "5" * 64),
        ("release_fingerprint", hotfix.PREDECESSOR_RELEASE_FINGERPRINT),
    ),
)
def test_binding_rejects_every_nonexact_artifact_identity(field: str, value: str) -> None:
    with pytest.raises(hotfix.Phase6ReviewQueryCodeHotfixError):
        _binding(**{field: value})


@pytest.mark.parametrize(
    "mutation",
    (
        "missing_function",
        "extra_function",
        "nonreview_code",
        "review_code",
        "release_default",
        "release_allowed_values",
        "global_release",
        "missing_review_environment",
        "existing_review_release",
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
    elif mutation == "nonreview_code":
        resources["UploadApiFunction"]["Properties"]["CodeUri"]["Version"] = "drifted"
    elif mutation == "review_code":
        resources["ReviewQueryApiFunction"]["Properties"]["CodeUri"]["Version"] = "drifted"
    elif mutation == "release_default":
        release["Default"] = "3" * 64
    elif mutation == "release_allowed_values":
        release["AllowedValues"] = ["3" * 64]
    elif mutation == "global_release":
        predecessor["Globals"]["Function"]["Environment"]["Variables"][
            "MR_LISTER_RELEASE_FINGERPRINT"
        ] = "3" * 64
    elif mutation == "missing_review_environment":
        resources["ReviewQueryApiFunction"]["Properties"].pop("Environment")
    else:
        resources["ReviewQueryApiFunction"]["Properties"]["Environment"]["Variables"][
            "MR_LISTER_RELEASE_FINGERPRINT"
        ] = "3" * 64

    with pytest.raises(hotfix.Phase6ReviewQueryCodeHotfixError):
        _render(monkeypatch, predecessor)


def test_render_rejects_byte_drift_from_the_sealed_predecessor() -> None:
    with pytest.raises(hotfix.Phase6ReviewQueryCodeHotfixError):
        hotfix.render_phase6_review_query_code_hotfix(_canonical(_predecessor()), _binding())


def test_render_rejects_wrong_binding_type(monkeypatch: pytest.MonkeyPatch) -> None:
    predecessor_raw = _canonical(_predecessor())
    monkeypatch.setattr(
        hotfix,
        "PREDECESSOR_TEMPLATE_SHA256",
        sha256(predecessor_raw).hexdigest(),
    )

    with pytest.raises(hotfix.Phase6ReviewQueryCodeHotfixError):
        hotfix.render_phase6_review_query_code_hotfix(predecessor_raw, object())  # type: ignore[arg-type]


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
            hotfix,
            "PREDECESSOR_TEMPLATE_SHA256",
            sha256(predecessor_raw).hexdigest(),
        )
        with pytest.raises(hotfix.Phase6ReviewQueryCodeHotfixError):
            hotfix.render_phase6_review_query_code_hotfix(predecessor_raw, _binding())


def _bind_private_write(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> tuple[Path, bytes]:
    predecessor = _predecessor()
    predecessor_raw = _canonical(predecessor)
    rendered = _render(monkeypatch, predecessor)
    repository = tmp_path / "repository"
    input_root = repository / "input"
    input_root.mkdir(parents=True)
    predecessor_path = input_root / "predecessor.json"
    predecessor_path.write_bytes(predecessor_raw)
    output_path = repository / ".mr_lister_private/hotfix/target.json"
    monkeypatch.setattr(hotfix, "REPOSITORY_ROOT", repository)
    monkeypatch.setattr(hotfix, "DEFAULT_PREDECESSOR_PATH", predecessor_path)
    monkeypatch.setattr(hotfix, "DEFAULT_OUTPUT_PATH", output_path)
    return output_path, rendered


def test_private_write_is_create_or_identical_and_owner_only(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    output_path, rendered = _bind_private_write(monkeypatch, tmp_path)

    assert hotfix.write_phase6_review_query_code_hotfix(_binding()) == output_path
    assert output_path.read_bytes() == rendered
    assert stat.S_IMODE(output_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(output_path.parent.stat().st_mode) == 0o700
    assert hotfix.write_phase6_review_query_code_hotfix(_binding()) == output_path

    output_path.write_bytes(b"different\n")
    with pytest.raises(hotfix.Phase6ReviewQueryCodeHotfixError):
        hotfix.write_phase6_review_query_code_hotfix(_binding())


def test_private_write_rejects_symlinked_output_parent(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    output_path, _ = _bind_private_write(monkeypatch, tmp_path)
    outside = tmp_path / "outside-output"
    outside.mkdir()
    (hotfix.REPOSITORY_ROOT / ".mr_lister_private").symlink_to(
        outside,
        target_is_directory=True,
    )

    with pytest.raises(hotfix.Phase6ReviewQueryCodeHotfixError):
        hotfix.write_phase6_review_query_code_hotfix(_binding())
    assert not (outside / "hotfix/target.json").exists()
    assert not output_path.exists()


def test_private_write_rejects_symlinked_predecessor_parent(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    output_path, _ = _bind_private_write(monkeypatch, tmp_path)
    outside = tmp_path / "outside-input"
    outside.mkdir()
    external_predecessor = outside / "predecessor.json"
    external_predecessor.write_bytes(hotfix.DEFAULT_PREDECESSOR_PATH.read_bytes())
    hotfix.DEFAULT_PREDECESSOR_PATH.unlink()
    hotfix.DEFAULT_PREDECESSOR_PATH.parent.rmdir()
    hotfix.DEFAULT_PREDECESSOR_PATH.parent.symlink_to(outside, target_is_directory=True)

    with pytest.raises(hotfix.Phase6ReviewQueryCodeHotfixError):
        hotfix.write_phase6_review_query_code_hotfix(_binding())
    assert not output_path.exists()


def test_exact_predecessor_and_target_constants_are_fixed() -> None:
    assert hotfix.PREDECESSOR_TEMPLATE_SHA256 == (
        "618fbca8d00b1edbfa7412668a6e7d2a0e4e65e23460ee8b9216f92f19dbdfc2"
    )
    assert hotfix.TARGET_LAMBDA_BUCKET == ("mr-lister-phase6-artifacts-dev-384627057108-us-west-2")
    assert hotfix.TARGET_LAMBDA_KEY == (
        "private/deployments/lambda/releases/"
        "6e32d16ce16371a65815e2931e0a897a34bbbce5526300438d4fc29061813571/"
        "phase6-lambda-122958c1df7ed916de122ca95c5cf9b8a34c385a45b706f396d2907c29cb8f9c.zip"
    )
    assert hotfix.TARGET_LAMBDA_VERSION == "zFS0yxHW0Jm0qZrHjirfQCwYyZwXAeVc"
    assert hotfix.REVIEW_QUERY_CODE_HOTFIX_TEMPLATE_SHA256 == (
        "81ad610ad62fa4ab58017c107c980b9572c4306681264f9565555e77379325e8"
    )
    assert len(hotfix._FUNCTION_LOGICAL_IDS) == 10
    assert len(set(hotfix._FUNCTION_LOGICAL_IDS)) == 10
