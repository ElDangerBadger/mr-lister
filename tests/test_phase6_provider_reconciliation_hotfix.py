from __future__ import annotations

import json
import os
import stat
import sys
from copy import deepcopy
from hashlib import sha256
from pathlib import Path
from typing import Any

import pytest

import tools.render_phase6_provider_reconciliation_hotfix as hotfix

VERSION = "provider-reconciliation-version_A1"


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
        "Metadata": {key: {"Preserved": key} for key in hotfix._PREDECESSOR_METADATA_KEYS},
        "Outputs": {
            "DeploymentReadiness": {"Value": "WEB_EDGE_ACTIVE_DRAFT_ONLY"},
            "PreservedOutput": {"Value": "exact"},
        },
        "Parameters": {
            name: _parameter(value) for name, value in hotfix._LOCKED_PARAMETER_VALUES.items()
        },
        "Resources": resources,
        "Transform": "AWS::Serverless-2016-10-31",
    }


def _binding(version: str = VERSION) -> hotfix.Phase6ProviderReconciliationHotfixBinding:
    return hotfix.Phase6ProviderReconciliationHotfixBinding(
        lambda_artifact_version=version,
    )


def _render(
    monkeypatch: pytest.MonkeyPatch,
    predecessor: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], dict[str, Any], bytes]:
    before = _predecessor() if predecessor is None else predecessor
    raw = _canonical(before)
    monkeypatch.setattr(hotfix, "PREDECESSOR_TEMPLATE_SHA256", sha256(raw).hexdigest())
    rendered = hotfix.render_phase6_provider_reconciliation_hotfix(raw, _binding())
    return before, json.loads(rendered), rendered


def test_render_changes_only_provider_key_version_release_and_provenance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    before, target, rendered = _render(monkeypatch)

    assert rendered == _canonical(target)
    assert len(target["Resources"]) == hotfix.PREDECESSOR_RESOURCE_COUNT
    assert target["Parameters"] == before["Parameters"]
    assert target["Globals"] == before["Globals"]
    assert target["Outputs"] == before["Outputs"]
    assert target["Transform"] == before["Transform"]

    provider = target["Resources"][hotfix._TARGET_FUNCTION_ID]
    provider_before = before["Resources"][hotfix._TARGET_FUNCTION_ID]
    assert provider["Properties"]["CodeUri"] == _binding().code_uri
    assert (
        provider["Properties"]["CodeUri"]["Bucket"]
        == (provider_before["Properties"]["CodeUri"]["Bucket"])
    )
    assert (
        provider["Properties"]["Environment"]["Variables"][hotfix._RELEASE_ENVIRONMENT_KEY]
        == hotfix.TARGET_RELEASE_FINGERPRINT
    )

    for logical_id in hotfix._FUNCTION_LOGICAL_IDS:
        if logical_id != hotfix._TARGET_FUNCTION_ID:
            assert target["Resources"][logical_id] == before["Resources"][logical_id]
    assert (
        target["Resources"]["PreparationDispatchFunction"]
        == before["Resources"]["PreparationDispatchFunction"]
    )
    assert target["Resources"]["PreparationDispatchFunction"]["Properties"]["CodeUri"] == (
        hotfix._code_uri(
            hotfix.WALKTHROUGH_RELEASE_FINGERPRINT,
            hotfix.WALKTHROUGH_LAMBDA_ARCHIVE_SHA256,
            hotfix.WALKTHROUGH_LAMBDA_VERSION_ID,
        )
    )
    assert provider_before["Properties"]["CodeUri"] == hotfix._code_uri(
        hotfix.PROVIDER_SHIPPING_CATALOG_RELEASE_FINGERPRINT,
        hotfix.PROVIDER_SHIPPING_CATALOG_LAMBDA_ARCHIVE_SHA256,
        hotfix.PROVIDER_SHIPPING_CATALOG_LAMBDA_VERSION_ID,
    )
    assert provider_before["Properties"]["Environment"]["Variables"] == {
        hotfix._RELEASE_ENVIRONMENT_KEY: (hotfix.PROVIDER_SHIPPING_CATALOG_RELEASE_FINGERPRINT)
    }
    for logical_id, resource in before["Resources"].items():
        if logical_id != hotfix._TARGET_FUNCTION_ID:
            assert target["Resources"][logical_id] == resource

    for key in hotfix._PREDECESSOR_METADATA_KEYS:
        assert target["Metadata"][key] == before["Metadata"][key]
    assert target["Metadata"][hotfix._METADATA_KEY] == hotfix._provenance(_binding())

    expected_paths = {
        ("Metadata", hotfix._METADATA_KEY),
        ("Resources", hotfix._TARGET_FUNCTION_ID, "Properties", "CodeUri", "Key"),
        ("Resources", hotfix._TARGET_FUNCTION_ID, "Properties", "CodeUri", "Version"),
        (
            "Resources",
            hotfix._TARGET_FUNCTION_ID,
            "Properties",
            "Environment",
            "Variables",
            hotfix._RELEASE_ENVIRONMENT_KEY,
        ),
    }
    assert hotfix._changed_paths(before, target) == expected_paths

    restored = deepcopy(target)
    restored["Metadata"].pop(hotfix._METADATA_KEY)
    restored["Resources"][hotfix._TARGET_FUNCTION_ID] = deepcopy(
        before["Resources"][hotfix._TARGET_FUNCTION_ID]
    )
    assert restored == before


def test_render_is_deterministic(monkeypatch: pytest.MonkeyPatch) -> None:
    before, _, first = _render(monkeypatch)
    _, _, second = _render(monkeypatch, before)

    assert first == second
    assert sha256(first).hexdigest() == sha256(second).hexdigest()


def test_release_source_archive_and_predecessor_are_fixed() -> None:
    assert hotfix.SOURCE_COMMIT == "bd9f3686ef812621f59a5bf031902d8ef5a88208"
    assert hotfix.TARGET_RELEASE_FINGERPRINT == (
        "166ed09faef339c6841d3bf8b7dfe2c0e9c8fd1aeb91b5762fe5999593e85534"
    )
    assert hotfix.TARGET_LAMBDA_ARCHIVE_SHA256 == (
        "4e8f668b5f259f296f65873cc020da6ac385714c110349e298f669e20b465f55"
    )
    assert hotfix.TARGET_LAMBDA_ARCHIVE_SIZE == 62_710_862
    assert hotfix.TARGET_LAMBDA_ARTIFACT_KEY == (
        "private/deployments/lambda/releases/"
        "166ed09faef339c6841d3bf8b7dfe2c0e9c8fd1aeb91b5762fe5999593e85534/"
        "phase6-lambda-4e8f668b5f259f296f65873cc020da6ac385714c110349e298f669e20b465f55.zip"
    )
    assert hotfix.PREDECESSOR_TEMPLATE_SHA256 == (
        "ee2941498cadbaf365c703b1694ec791c93ed9fdb9c2631a8d3117a6b11bd4a3"
    )
    assert hotfix.PREDECESSOR_RESOURCE_COUNT == 102
    assert hotfix.DEPLOYED_PROCESSED_RESOURCE_COUNT == 125
    assert hotfix._TARGET_FUNCTION_ID == "ProviderDraftFunction"
    assert len(hotfix._FUNCTION_LOGICAL_IDS) == 10
    assert len(set(hotfix._FUNCTION_LOGICAL_IDS)) == 10


@pytest.mark.parametrize(
    "version",
    (
        "latest",
        "null",
        "ab",
        " leading",
        "trailing ",
        "bad\\version",
        'bad"version',
        "bad\nversion",
        "<VERSION_ID>",
        "a" * 1025,
    ),
)
def test_binding_rejects_nonliteral_or_moving_version(version: str) -> None:
    with pytest.raises(hotfix.Phase6ProviderReconciliationHotfixError):
        _binding(version)


@pytest.mark.parametrize(
    "mutation",
    (
        "resource_count",
        "extra_function",
        "provider_code",
        "preparation_code",
        "upload_code",
        "review_code",
        "base_code",
        "provider_release",
        "preparation_release",
        "global_release",
        "scaffold",
        "release_parameter",
        "agentcore_parameter",
        "extra_parameter",
        "metadata",
        "existing_target_metadata",
        "readiness",
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
    elif mutation == "provider_code":
        resources["ProviderDraftFunction"]["Properties"]["CodeUri"]["Version"] = "drifted"
    elif mutation == "preparation_code":
        resources["PreparationDispatchFunction"]["Properties"]["CodeUri"]["Version"] = "drifted"
    elif mutation == "upload_code":
        resources["UploadApiFunction"]["Properties"]["CodeUri"]["Version"] = "drifted"
    elif mutation == "review_code":
        resources["ReviewQueryApiFunction"]["Properties"]["CodeUri"]["Version"] = "drifted"
    elif mutation == "base_code":
        resources["DispatcherFunction"]["Properties"]["CodeUri"]["Version"] = "drifted"
    elif mutation == "provider_release":
        resources["ProviderDraftFunction"]["Properties"]["Environment"]["Variables"][
            hotfix._RELEASE_ENVIRONMENT_KEY
        ] = hotfix.BASE_RELEASE_FINGERPRINT
    elif mutation == "preparation_release":
        resources["PreparationDispatchFunction"]["Properties"]["Environment"]["Variables"][
            hotfix._RELEASE_ENVIRONMENT_KEY
        ] = hotfix.BASE_RELEASE_FINGERPRINT
    elif mutation == "global_release":
        predecessor["Globals"]["Function"]["Environment"]["Variables"][
            hotfix._RELEASE_ENVIRONMENT_KEY
        ] = hotfix.BASE_RELEASE_FINGERPRINT
    elif mutation == "scaffold":
        predecessor["Globals"]["Function"]["Environment"]["Variables"][
            "MR_LISTER_PHASE6_SCAFFOLD_ONLY"
        ] = "true"
    elif mutation == "release_parameter":
        predecessor["Parameters"]["ReleaseFingerprint"] = _parameter("a" * 64)
    elif mutation == "agentcore_parameter":
        predecessor["Parameters"]["AgentCoreRuntimeVersion"] = _parameter("3")
    elif mutation == "extra_parameter":
        predecessor["Parameters"]["Unexpected"] = _parameter("unexpected")
    elif mutation == "metadata":
        predecessor["Metadata"].pop("MrListerPhase6WebEdgeTransition")
    elif mutation == "existing_target_metadata":
        predecessor["Metadata"][hotfix._METADATA_KEY] = {"Drift": True}
    else:
        predecessor["Outputs"]["DeploymentReadiness"]["Value"] = "PHASE7_ACTIVE"

    with pytest.raises(hotfix.Phase6ProviderReconciliationHotfixError):
        _render(monkeypatch, predecessor)


def test_render_rejects_byte_drift_duplicate_json_and_wrong_binding_type(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(hotfix.Phase6ProviderReconciliationHotfixError):
        hotfix.render_phase6_provider_reconciliation_hotfix(
            _canonical(_predecessor()),
            _binding(),
        )

    duplicate = b'{"Globals":{},"Globals":{},"Metadata":{},"Parameters":{},"Resources":{}}\n'
    monkeypatch.setattr(hotfix, "PREDECESSOR_TEMPLATE_SHA256", sha256(duplicate).hexdigest())
    with pytest.raises(hotfix.Phase6ProviderReconciliationHotfixError):
        hotfix.render_phase6_provider_reconciliation_hotfix(duplicate, _binding())

    raw = _canonical(_predecessor())
    monkeypatch.setattr(hotfix, "PREDECESSOR_TEMPLATE_SHA256", sha256(raw).hexdigest())
    with pytest.raises(hotfix.Phase6ProviderReconciliationHotfixError):
        hotfix.render_phase6_provider_reconciliation_hotfix(
            raw,
            object(),  # type: ignore[arg-type]
        )


def _bind_private_write(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> tuple[Path, bytes]:
    predecessor_raw = _canonical(_predecessor())
    monkeypatch.setattr(
        hotfix,
        "PREDECESSOR_TEMPLATE_SHA256",
        sha256(predecessor_raw).hexdigest(),
    )
    repository = tmp_path / "repository"
    predecessor_path = repository / "input/predecessor.json"
    predecessor_path.parent.mkdir(parents=True)
    predecessor_path.write_bytes(predecessor_raw)
    output = repository / ".mr_lister_private/hotfix/target.json"
    monkeypatch.setattr(hotfix, "REPOSITORY_ROOT", repository)
    monkeypatch.setattr(hotfix, "DEFAULT_PREDECESSOR_PATH", predecessor_path)
    rendered = hotfix.render_phase6_provider_reconciliation_hotfix(
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
        hotfix.write_phase6_provider_reconciliation_hotfix(
            _binding(),
            output_path=output,
        )
        == output
    )
    assert output.read_bytes() == rendered
    assert stat.S_IMODE(output.stat().st_mode) == 0o600
    assert stat.S_IMODE(output.parent.stat().st_mode) == 0o700
    assert (
        hotfix.verify_phase6_provider_reconciliation_hotfix(
            _binding(),
            output_path=output,
        )
        == output
    )

    with pytest.raises(hotfix.Phase6ProviderReconciliationHotfixError):
        hotfix.write_phase6_provider_reconciliation_hotfix(
            _binding(),
            output_path=output,
        )
    output.write_bytes(b"drifted\n")
    with pytest.raises(hotfix.Phase6ProviderReconciliationHotfixError):
        hotfix.verify_phase6_provider_reconciliation_hotfix(
            _binding(),
            output_path=output,
        )


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

    with pytest.raises(hotfix.Phase6ProviderReconciliationHotfixError):
        hotfix.write_phase6_provider_reconciliation_hotfix(
            _binding(),
            output_path=output,
        )
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
        with pytest.raises(hotfix.Phase6ProviderReconciliationHotfixError):
            hotfix.write_phase6_provider_reconciliation_hotfix(
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


def test_cli_writes_and_verifies_the_exact_version(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    output, _ = _bind_private_write(monkeypatch, tmp_path)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "render_phase6_provider_reconciliation_hotfix",
            "--lambda-artifact-version",
            VERSION,
            "--output",
            str(output),
            "--write",
        ],
    )

    assert hotfix.main() == 0
    written = json.loads(capsys.readouterr().out)
    assert written["lambda_artifact_version"] == VERSION
    assert written["release_fingerprint"] == hotfix.TARGET_RELEASE_FINGERPRINT
    assert written["result"] == "passed"

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "render_phase6_provider_reconciliation_hotfix",
            "--lambda-artifact-version",
            VERSION,
            "--output",
            str(output),
            "--verify",
        ],
    )
    assert hotfix.main() == 0
    verified = json.loads(capsys.readouterr().out)
    assert verified == written


def test_cli_rejects_a_moving_version(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "render_phase6_provider_reconciliation_hotfix",
            "--lambda-artifact-version",
            "latest",
            "--write",
        ],
    )

    assert hotfix.main() == 2
    assert capsys.readouterr().out.strip() == hotfix._GENERIC_ERROR
