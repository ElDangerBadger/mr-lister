from __future__ import annotations

import json
import os
import stat
from copy import deepcopy
from hashlib import sha256
from pathlib import Path
from typing import Any

import pytest

import tools.render_phase6_application_walkthrough_hotfix as hotfix
from mr_lister.agent.runtime_binding import agentcore_runtime_binding_fingerprint

VERSION = "walkthrough-hotfix-version-id"


def _canonical(value: object) -> bytes:
    return (
        json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=True,
            indent=2,
            separators=(",", ": "),
            sort_keys=True,
        )
        + "\n"
    ).encode()


def _parameter(value: str) -> dict[str, object]:
    return {"AllowedValues": [value], "Default": value, "Type": "String"}


def _predecessor() -> dict[str, Any]:
    resources: dict[str, Any] = {}
    for logical_id in hotfix._FUNCTION_LOGICAL_IDS:
        code_uri, release_override = hotfix._expected_predecessor_function_binding(logical_id)
        variables = (
            {hotfix._RELEASE_ENVIRONMENT_KEY: release_override}
            if release_override is not None
            else {}
        )
        resources[logical_id] = {
            "Properties": {
                "CodeUri": code_uri,
                "Environment": {"Variables": variables},
                "Handler": f"phase6_lambda.{logical_id}",
            },
            "Type": "AWS::Serverless::Function",
        }
    filler_count = hotfix.PREDECESSOR_RESOURCE_COUNT - len(resources)
    for index in range(filler_count):
        resources[f"PreservedResource{index:03d}"] = {
            "Properties": {"Identity": f"preserved-{index:03d}"},
            "Type": "AWS::Test::Preserved",
        }
    return {
        "AWSTemplateFormatVersion": "2010-09-09",
        "Globals": {
            "Function": {
                "Environment": {
                    "Variables": {
                        "MR_LISTER_PHASE6_SCAFFOLD_ONLY": "false",
                        hotfix._RELEASE_ENVIRONMENT_KEY: {"Ref": "ReleaseFingerprint"},
                    }
                }
            }
        },
        "Metadata": {"Existing": {"Mode": "WEB_EDGE_ACTIVE_DRAFT_ONLY"}},
        "Outputs": {
            "DeploymentReadiness": {"Value": "WEB_EDGE_ACTIVE_DRAFT_ONLY"},
            "PreservedOutput": {"Value": "exact"},
        },
        "Parameters": {
            "AgentCoreRuntimeArn": _parameter(hotfix.AGENTCORE_RUNTIME_ARN),
            "AgentCoreRuntimeBindingFingerprint": _parameter(
                hotfix.PREDECESSOR_AGENTCORE_BINDING_FINGERPRINT
            ),
            "AgentCoreRuntimeEndpointArn": _parameter(hotfix.PREDECESSOR_AGENTCORE_ENDPOINT_ARN),
            "AgentCoreRuntimeQualifier": _parameter(hotfix.PREDECESSOR_AGENTCORE_QUALIFIER),
            "AgentCoreRuntimeVersion": _parameter(hotfix.PREDECESSOR_AGENTCORE_RUNTIME_VERSION),
            "ReleaseFingerprint": _parameter(hotfix.BASE_RELEASE_FINGERPRINT),
        },
        "Resources": resources,
        "Transform": "AWS::Serverless-2016-10-31",
    }


def _binding(**overrides: str) -> hotfix.Phase6ApplicationWalkthroughHotfixBinding:
    values = {
        "agentcore_runtime_arn": hotfix.AGENTCORE_RUNTIME_ARN,
        "agentcore_runtime_binding_fingerprint": (hotfix.TARGET_AGENTCORE_BINDING_FINGERPRINT),
        "agentcore_runtime_endpoint_arn": hotfix.TARGET_AGENTCORE_ENDPOINT_ARN,
        "agentcore_runtime_qualifier": hotfix.TARGET_AGENTCORE_QUALIFIER,
        "agentcore_runtime_version": hotfix.TARGET_AGENTCORE_RUNTIME_VERSION,
        "lambda_artifact_bucket": hotfix.LAMBDA_ARTIFACT_BUCKET,
        "lambda_artifact_key": (
            "private/deployments/lambda/releases/"
            f"{hotfix.TARGET_RELEASE_FINGERPRINT}/"
            f"phase6-lambda-{hotfix.TARGET_LAMBDA_ARCHIVE_SHA256}.zip"
        ),
        "lambda_artifact_version": VERSION,
        "release_fingerprint": hotfix.TARGET_RELEASE_FINGERPRINT,
    }
    values.update(overrides)
    return hotfix.Phase6ApplicationWalkthroughHotfixBinding(**values)


def _render(
    monkeypatch: pytest.MonkeyPatch,
    predecessor: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], dict[str, Any], bytes]:
    before = _predecessor() if predecessor is None else predecessor
    raw = _canonical(before)
    monkeypatch.setattr(hotfix, "PREDECESSOR_TEMPLATE_SHA256", sha256(raw).hexdigest())
    rendered = hotfix.render_phase6_application_walkthrough_hotfix(raw, _binding())
    return before, json.loads(rendered), rendered


def test_render_changes_only_two_functions_four_agentcore_parameters_and_provenance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    before, target, rendered = _render(monkeypatch)

    assert rendered == _canonical(target)
    assert len(target["Resources"]) == hotfix.PREDECESSOR_RESOURCE_COUNT
    assert target["Parameters"]["ReleaseFingerprint"] == before["Parameters"]["ReleaseFingerprint"]
    assert (
        target["Parameters"]["AgentCoreRuntimeArn"] == before["Parameters"]["AgentCoreRuntimeArn"]
    )
    expected_parameters = {
        "AgentCoreRuntimeBindingFingerprint": hotfix.TARGET_AGENTCORE_BINDING_FINGERPRINT,
        "AgentCoreRuntimeEndpointArn": hotfix.TARGET_AGENTCORE_ENDPOINT_ARN,
        "AgentCoreRuntimeQualifier": hotfix.TARGET_AGENTCORE_QUALIFIER,
        "AgentCoreRuntimeVersion": hotfix.TARGET_AGENTCORE_RUNTIME_VERSION,
    }
    for name, value in expected_parameters.items():
        assert target["Parameters"][name]["Default"] == value
        assert target["Parameters"][name]["AllowedValues"] == [value]

    target_code = _binding().code_uri
    for logical_id in hotfix._FUNCTION_LOGICAL_IDS:
        function = target["Resources"][logical_id]
        if logical_id in hotfix._TARGET_FUNCTION_IDS:
            assert function["Properties"]["CodeUri"] == target_code
            assert (
                function["Properties"]["Environment"]["Variables"][hotfix._RELEASE_ENVIRONMENT_KEY]
                == hotfix.TARGET_RELEASE_FINGERPRINT
            )
        else:
            assert function == before["Resources"][logical_id]

    assert target["Resources"]["UploadApiFunction"] == before["Resources"]["UploadApiFunction"]
    assert target["Resources"]["UploadApiFunction"]["Properties"]["CodeUri"] == (
        hotfix._code_uri(
            hotfix.ARTWORK_CLOSURE_RELEASE_FINGERPRINT,
            hotfix.ARTWORK_CLOSURE_LAMBDA_ARCHIVE_SHA256,
            hotfix.ARTWORK_CLOSURE_LAMBDA_VERSION_ID,
        )
    )
    for logical_id, resource in before["Resources"].items():
        if logical_id not in hotfix._TARGET_FUNCTION_IDS:
            assert target["Resources"][logical_id] == resource

    assert target["Globals"] == before["Globals"]
    assert target["Outputs"] == before["Outputs"]
    assert target["Transform"] == before["Transform"]
    assert target["Metadata"]["Existing"] == before["Metadata"]["Existing"]
    assert target["Metadata"][hotfix._METADATA_KEY] == hotfix._provenance(_binding())

    expected_paths: set[tuple[str, ...]] = {("Metadata", hotfix._METADATA_KEY)}
    for logical_id in hotfix._TARGET_FUNCTION_IDS:
        expected_paths.update(
            {
                ("Resources", logical_id, "Properties", "CodeUri", "Key"),
                ("Resources", logical_id, "Properties", "CodeUri", "Version"),
                (
                    "Resources",
                    logical_id,
                    "Properties",
                    "Environment",
                    "Variables",
                    hotfix._RELEASE_ENVIRONMENT_KEY,
                ),
            }
        )
    for name in hotfix._AGENTCORE_PARAMETER_NAMES:
        expected_paths.update(
            {
                ("Parameters", name, "AllowedValues", "0"),
                ("Parameters", name, "Default"),
            }
        )
    assert hotfix._changed_paths(before, target) == expected_paths

    restored = deepcopy(target)
    restored["Metadata"].pop(hotfix._METADATA_KEY)
    for logical_id in hotfix._TARGET_FUNCTION_IDS:
        restored["Resources"][logical_id] = deepcopy(before["Resources"][logical_id])
    for name in hotfix._AGENTCORE_PARAMETER_NAMES:
        restored["Parameters"][name] = deepcopy(before["Parameters"][name])
    assert restored == before


def test_binding_hard_binds_source_release_archive_size_and_v4_authority() -> None:
    assert hotfix.SOURCE_COMMIT == "f6a643b19e47f02784e9b590949fddde1cf9c107"
    assert hotfix._SOURCE_COMMIT.fullmatch(hotfix.SOURCE_COMMIT)
    assert hotfix.TARGET_RELEASE_FINGERPRINT == (
        "9bc5e1727cfcf68b40847d1a2e416300640779898c9bf884f6f9e442b0225d9e"
    )
    assert hotfix.TARGET_LAMBDA_ARCHIVE_SHA256 == (
        "db179dc5fb5754619482b13505f7899469e93e820bbe18514953849ac1b959c7"
    )
    assert hotfix.TARGET_LAMBDA_ARCHIVE_SIZE == 62_703_275
    assert hotfix.TARGET_AGENTCORE_BINDING_FINGERPRINT == (
        agentcore_runtime_binding_fingerprint(
            runtime_arn=hotfix.AGENTCORE_RUNTIME_ARN,
            endpoint_arn=hotfix.TARGET_AGENTCORE_ENDPOINT_ARN,
            qualifier=hotfix.TARGET_AGENTCORE_QUALIFIER,
            runtime_version=hotfix.TARGET_AGENTCORE_RUNTIME_VERSION,
            release_fingerprint=hotfix.TARGET_RELEASE_FINGERPRINT,
        )
    )

    invalid = (
        {"lambda_artifact_bucket": "another-bucket"},
        {"lambda_artifact_version": "latest"},
        {"release_fingerprint": hotfix.ARTWORK_CLOSURE_RELEASE_FINGERPRINT},
        {
            "lambda_artifact_key": (
                "private/deployments/lambda/releases/"
                f"{'a' * 64}/phase6-lambda-{hotfix.TARGET_LAMBDA_ARCHIVE_SHA256}.zip"
            )
        },
        {
            "lambda_artifact_key": (
                "private/deployments/lambda/releases/"
                f"{hotfix.TARGET_RELEASE_FINGERPRINT}/phase6-lambda-{'b' * 64}.zip"
            )
        },
        {"agentcore_runtime_binding_fingerprint": "0" * 64},
        {"agentcore_runtime_version": hotfix.PREDECESSOR_AGENTCORE_RUNTIME_VERSION},
        {"agentcore_runtime_qualifier": hotfix.PREDECESSOR_AGENTCORE_QUALIFIER},
        {"agentcore_runtime_endpoint_arn": hotfix.PREDECESSOR_AGENTCORE_ENDPOINT_ARN},
    )
    for override in invalid:
        with pytest.raises(hotfix.Phase6ApplicationWalkthroughHotfixError):
            _binding(**override)


@pytest.mark.parametrize(
    "mutation",
    (
        "resource_count",
        "extra_function",
        "target_mosaic",
        "upload_mosaic",
        "review_mosaic",
        "base_mosaic",
        "target_release_override",
        "global_release",
        "release_parameter",
        "agentcore_v3",
        "scaffold",
        "readiness",
        "existing_provenance",
    ),
)
def test_render_rejects_semantically_drifted_predecessor(
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
) -> None:
    predecessor = _predecessor()
    resources = predecessor["Resources"]
    if mutation == "resource_count":
        resources.pop("PreservedResource000")
    elif mutation == "extra_function":
        resources.pop("PreservedResource000")
        resources["UnexpectedFunction"] = deepcopy(resources["UploadApiFunction"])
    elif mutation == "target_mosaic":
        resources["PreparationDispatchFunction"]["Properties"]["CodeUri"]["Version"] = "drifted"
    elif mutation == "upload_mosaic":
        resources["UploadApiFunction"]["Properties"]["CodeUri"]["Version"] = "drifted"
    elif mutation == "review_mosaic":
        resources["ReviewQueryApiFunction"]["Properties"]["CodeUri"] = hotfix._code_uri(
            hotfix.BASE_RELEASE_FINGERPRINT,
            hotfix.BASE_LAMBDA_ARCHIVE_SHA256,
            hotfix.BASE_LAMBDA_VERSION_ID,
        )
    elif mutation == "base_mosaic":
        resources["DispatcherFunction"]["Properties"]["CodeUri"]["Version"] = "drifted"
    elif mutation == "target_release_override":
        resources["ProviderDraftFunction"]["Properties"]["Environment"]["Variables"][
            hotfix._RELEASE_ENVIRONMENT_KEY
        ] = hotfix.BASE_RELEASE_FINGERPRINT
    elif mutation == "global_release":
        predecessor["Globals"]["Function"]["Environment"]["Variables"][
            hotfix._RELEASE_ENVIRONMENT_KEY
        ] = hotfix.BASE_RELEASE_FINGERPRINT
    elif mutation == "release_parameter":
        predecessor["Parameters"]["ReleaseFingerprint"] = _parameter("a" * 64)
    elif mutation == "agentcore_v3":
        predecessor["Parameters"]["AgentCoreRuntimeVersion"] = _parameter("2")
    elif mutation == "scaffold":
        predecessor["Globals"]["Function"]["Environment"]["Variables"][
            "MR_LISTER_PHASE6_SCAFFOLD_ONLY"
        ] = "true"
    elif mutation == "readiness":
        predecessor["Outputs"]["DeploymentReadiness"]["Value"] = "PHASE7_ACTIVE"
    else:
        predecessor["Metadata"][hotfix._METADATA_KEY] = {"Drift": True}

    with pytest.raises(hotfix.Phase6ApplicationWalkthroughHotfixError):
        _render(monkeypatch, predecessor)


def test_render_rejects_byte_drift_duplicate_json_and_wrong_binding_type(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(hotfix.Phase6ApplicationWalkthroughHotfixError):
        hotfix.render_phase6_application_walkthrough_hotfix(
            _canonical(_predecessor()),
            _binding(),
        )

    duplicate = b'{"Globals":{},"Globals":{},"Outputs":{},"Parameters":{},"Resources":{}}\n'
    monkeypatch.setattr(hotfix, "PREDECESSOR_TEMPLATE_SHA256", sha256(duplicate).hexdigest())
    with pytest.raises(hotfix.Phase6ApplicationWalkthroughHotfixError):
        hotfix.render_phase6_application_walkthrough_hotfix(duplicate, _binding())

    raw = _canonical(_predecessor())
    monkeypatch.setattr(hotfix, "PREDECESSOR_TEMPLATE_SHA256", sha256(raw).hexdigest())
    with pytest.raises(hotfix.Phase6ApplicationWalkthroughHotfixError):
        hotfix.render_phase6_application_walkthrough_hotfix(
            raw,
            object(),  # type: ignore[arg-type]
        )


def _bind_private_write(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> tuple[Path, bytes]:
    repository = tmp_path / "repository"
    predecessor_path = repository / "input/predecessor.json"
    predecessor_path.parent.mkdir(parents=True)
    predecessor_raw = _canonical(_predecessor())
    predecessor_path.write_bytes(predecessor_raw)
    output = repository / ".mr_lister_private/hotfix/target.json"
    monkeypatch.setattr(hotfix, "REPOSITORY_ROOT", repository)
    monkeypatch.setattr(hotfix, "DEFAULT_PREDECESSOR_PATH", predecessor_path)
    monkeypatch.setattr(
        hotfix,
        "PREDECESSOR_TEMPLATE_SHA256",
        sha256(predecessor_raw).hexdigest(),
    )
    rendered = hotfix.render_phase6_application_walkthrough_hotfix(
        predecessor_raw,
        _binding(),
    )
    return output, rendered


def test_private_write_is_create_only_owner_only_and_separately_verifiable(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    output, rendered = _bind_private_write(monkeypatch, tmp_path)

    assert (
        hotfix.write_phase6_application_walkthrough_hotfix(_binding(), output_path=output) == output
    )
    assert output.read_bytes() == rendered
    assert stat.S_IMODE(output.stat().st_mode) == 0o600
    assert stat.S_IMODE(output.parent.stat().st_mode) == 0o700
    assert (
        hotfix.verify_phase6_application_walkthrough_hotfix(_binding(), output_path=output)
        == output
    )

    with pytest.raises(hotfix.Phase6ApplicationWalkthroughHotfixError):
        hotfix.write_phase6_application_walkthrough_hotfix(_binding(), output_path=output)
    output.write_bytes(b"drifted\n")
    with pytest.raises(hotfix.Phase6ApplicationWalkthroughHotfixError):
        hotfix.verify_phase6_application_walkthrough_hotfix(_binding(), output_path=output)


def test_private_write_rejects_symlinked_private_root(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    output, _ = _bind_private_write(monkeypatch, tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (hotfix.REPOSITORY_ROOT / ".mr_lister_private").symlink_to(
        outside,
        target_is_directory=True,
    )

    with pytest.raises(hotfix.Phase6ApplicationWalkthroughHotfixError):
        hotfix.write_phase6_application_walkthrough_hotfix(_binding(), output_path=output)
    assert not (outside / "hotfix/target.json").exists()


def test_private_write_rejects_parent_identity_swap(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    output, _ = _bind_private_write(monkeypatch, tmp_path)
    parent = output.parent
    relocated = parent.with_name(f"{parent.name}-relocated")
    outside = tmp_path / "outside"
    outside.mkdir(mode=0o700)
    real_link = os.link
    swapped = False

    def swapping_link(
        source: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        destination: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        *,
        src_dir_fd: int | None = None,
        dst_dir_fd: int | None = None,
        follow_symlinks: bool = True,
    ) -> None:
        nonlocal swapped
        if destination == output.name and not swapped:
            assert src_dir_fd is not None and dst_dir_fd is not None
            swapped = True
            parent.rename(relocated)
            parent.symlink_to(outside, target_is_directory=True)
        real_link(
            source,
            destination,
            src_dir_fd=src_dir_fd,
            dst_dir_fd=dst_dir_fd,
            follow_symlinks=follow_symlinks,
        )

    monkeypatch.setattr(hotfix.os, "link", swapping_link)
    try:
        with pytest.raises(hotfix.Phase6ApplicationWalkthroughHotfixError):
            hotfix.write_phase6_application_walkthrough_hotfix(
                _binding(),
                output_path=output,
            )
    finally:
        if parent.is_symlink():
            parent.unlink()
        if relocated.exists():
            relocated.rename(parent)

    assert swapped is True
    assert not output.exists()
    assert not (outside / output.name).exists()


def test_fixed_predecessor_and_bounded_resource_authority() -> None:
    assert hotfix.PREDECESSOR_TEMPLATE_SHA256 == (
        "5ccbe4fdf260faff2d3e6f113f46ad92a7ff2d2ae047e5d6c8d39718afd7bba5"
    )
    assert hotfix.PREDECESSOR_RESOURCE_COUNT == 102
    assert hotfix.DEPLOYED_PROCESSED_RESOURCE_COUNT == 125
    assert hotfix._TARGET_FUNCTION_IDS == (
        "PreparationDispatchFunction",
        "ProviderDraftFunction",
    )
    assert len(hotfix._FUNCTION_LOGICAL_IDS) == 10
    assert len(set(hotfix._FUNCTION_LOGICAL_IDS)) == 10
