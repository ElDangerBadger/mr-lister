from __future__ import annotations

import json
import os
import stat
from copy import deepcopy
from hashlib import sha256
from pathlib import Path
from typing import Any

import pytest

import tools.render_phase6_artwork_closure as closure
from mr_lister.agent.runtime_binding import agentcore_runtime_binding_fingerprint

RELEASE = "f34ab73042014fccce2cb3733624f005a4ccc10bb065b39c3e20befd3c33923f"
ARCHIVE = "bf5ef1a13329814934f73cef81e7ec52153e508f11ed7945921501927ea58d5e"
VERSION = "closure-version-id"
BINDING_FINGERPRINT = "d8194386435d2f941d0942b102595830c1efc48e9bc4890457b46e17e0df3196"


def _canonical(value: object) -> bytes:
    return (
        json.dumps(value, ensure_ascii=True, indent=2, separators=(",", ": "), sort_keys=True)
        + "\n"
    ).encode()


def _parameter(value: str) -> dict[str, object]:
    return {"AllowedValues": [value], "Default": value, "Type": "String"}


def _predecessor() -> dict[str, Any]:
    resources: dict[str, Any] = {}
    for logical_id in closure._FUNCTION_LOGICAL_IDS:
        review_query = logical_id == "ReviewQueryApiFunction"
        variables = (
            {closure._RELEASE_ENVIRONMENT_KEY: (closure.REVIEW_QUERY_RELEASE_FINGERPRINT)}
            if review_query
            else {}
        )
        resources[logical_id] = {
            "Properties": {
                "CodeUri": closure._predecessor_code_uri(review_query=review_query),
                "Environment": {"Variables": variables},
                "Handler": f"phase6_lambda.{logical_id}",
            },
            "Type": "AWS::Serverless::Function",
        }
    return {
        "AWSTemplateFormatVersion": "2010-09-09",
        "Globals": {
            "Function": {
                "Environment": {
                    "Variables": {
                        "MR_LISTER_PHASE6_SCAFFOLD_ONLY": "false",
                        closure._RELEASE_ENVIRONMENT_KEY: {"Ref": "ReleaseFingerprint"},
                    }
                }
            }
        },
        "Metadata": {"Existing": "preserved"},
        "Outputs": {
            "DeploymentReadiness": {"Value": "WEB_EDGE_ACTIVE_DRAFT_ONLY"},
        },
        "Parameters": {
            "AgentCoreRuntimeArn": _parameter(closure.AGENTCORE_RUNTIME_ARN),
            "AgentCoreRuntimeBindingFingerprint": _parameter(
                closure.PREDECESSOR_AGENTCORE_BINDING_FINGERPRINT
            ),
            "AgentCoreRuntimeEndpointArn": _parameter(closure.PREDECESSOR_AGENTCORE_ENDPOINT_ARN),
            "AgentCoreRuntimeQualifier": _parameter("phase6_v1_dev"),
            "AgentCoreRuntimeVersion": _parameter("1"),
            "ReleaseFingerprint": _parameter(closure.PREDECESSOR_RELEASE_FINGERPRINT),
        },
        "Resources": resources,
        "Transform": "AWS::Serverless-2016-10-31",
    }


def _binding(**overrides: str) -> closure.Phase6ArtworkClosureBinding:
    values = {
        "agentcore_runtime_arn": closure.AGENTCORE_RUNTIME_ARN,
        "agentcore_runtime_binding_fingerprint": BINDING_FINGERPRINT,
        "agentcore_runtime_endpoint_arn": closure.TARGET_AGENTCORE_ENDPOINT_ARN,
        "agentcore_runtime_qualifier": closure.TARGET_AGENTCORE_QUALIFIER,
        "agentcore_runtime_version": closure.TARGET_AGENTCORE_RUNTIME_VERSION,
        "lambda_artifact_bucket": closure.LAMBDA_ARTIFACT_BUCKET,
        "lambda_artifact_key": (
            f"private/deployments/lambda/releases/{RELEASE}/phase6-lambda-{ARCHIVE}.zip"
        ),
        "lambda_artifact_version": VERSION,
        "release_fingerprint": RELEASE,
    }
    values.update(overrides)
    return closure.Phase6ArtworkClosureBinding(**values)


def _render(
    monkeypatch: pytest.MonkeyPatch,
    predecessor: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], dict[str, Any], bytes]:
    before = _predecessor() if predecessor is None else predecessor
    raw = _canonical(before)
    monkeypatch.setattr(closure, "PREDECESSOR_TEMPLATE_SHA256", sha256(raw).hexdigest())
    rendered = closure.render_phase6_artwork_closure(raw, _binding())
    return before, json.loads(rendered), rendered


def test_render_changes_only_three_functions_and_four_agentcore_parameters(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    before, target, rendered = _render(monkeypatch)

    assert rendered == _canonical(target)
    assert target["Parameters"]["ReleaseFingerprint"] == before["Parameters"]["ReleaseFingerprint"]
    assert (
        target["Parameters"]["AgentCoreRuntimeArn"] == before["Parameters"]["AgentCoreRuntimeArn"]
    )
    expected_parameters = {
        "AgentCoreRuntimeBindingFingerprint": BINDING_FINGERPRINT,
        "AgentCoreRuntimeEndpointArn": closure.TARGET_AGENTCORE_ENDPOINT_ARN,
        "AgentCoreRuntimeQualifier": closure.TARGET_AGENTCORE_QUALIFIER,
        "AgentCoreRuntimeVersion": closure.TARGET_AGENTCORE_RUNTIME_VERSION,
    }
    for name, value in expected_parameters.items():
        assert target["Parameters"][name]["Default"] == value
        assert target["Parameters"][name]["AllowedValues"] == [value]

    changed = set(closure._ARTWORK_CLOSURE_FUNCTION_IDS)
    target_code = _binding().code_uri
    for logical_id in closure._FUNCTION_LOGICAL_IDS:
        function = target["Resources"][logical_id]
        variables = function["Properties"]["Environment"]["Variables"]
        if logical_id in changed:
            assert function["Properties"]["CodeUri"] == target_code
            assert variables[closure._RELEASE_ENVIRONMENT_KEY] == RELEASE
        else:
            assert function == before["Resources"][logical_id]

    assert target["Globals"] == before["Globals"]
    assert target["Metadata"] == before["Metadata"]
    assert target["Outputs"] == before["Outputs"]


def test_binding_derives_and_requires_exact_v3_release_authority() -> None:
    assert BINDING_FINGERPRINT == agentcore_runtime_binding_fingerprint(
        runtime_arn=closure.AGENTCORE_RUNTIME_ARN,
        endpoint_arn=closure.TARGET_AGENTCORE_ENDPOINT_ARN,
        qualifier=closure.TARGET_AGENTCORE_QUALIFIER,
        runtime_version=closure.TARGET_AGENTCORE_RUNTIME_VERSION,
        release_fingerprint=RELEASE,
    )
    assert _binding().lambda_archive_sha256 == ARCHIVE

    invalid = (
        {"agentcore_runtime_binding_fingerprint": "0" * 64},
        {"agentcore_runtime_version": "2"},
        {"agentcore_runtime_qualifier": "DEFAULT"},
        {
            "agentcore_runtime_endpoint_arn": (
                f"{closure.AGENTCORE_RUNTIME_ARN}/runtime-endpoint/phase6_v2_dev"
            )
        },
        {"release_fingerprint": closure.PREDECESSOR_RELEASE_FINGERPRINT},
        {
            "lambda_artifact_key": (
                f"private/deployments/lambda/releases/{'a' * 64}/phase6-lambda-{ARCHIVE}.zip"
            )
        },
    )
    for override in invalid:
        with pytest.raises(closure.Phase6ArtworkClosureError):
            _binding(**override)


@pytest.mark.parametrize(
    "mutation",
    (
        "extra_function",
        "closure_function_mosaic",
        "review_query_mosaic",
        "global_release",
        "agentcore_predecessor",
        "scaffold",
        "readiness",
    ),
)
def test_render_rejects_semantically_drifted_predecessor(
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
) -> None:
    predecessor = _predecessor()
    if mutation == "extra_function":
        predecessor["Resources"]["UnexpectedFunction"] = deepcopy(
            predecessor["Resources"]["UploadApiFunction"]
        )
    elif mutation == "closure_function_mosaic":
        predecessor["Resources"]["UploadApiFunction"]["Properties"]["CodeUri"]["Version"] = (
            "drifted-version"
        )
    elif mutation == "review_query_mosaic":
        predecessor["Resources"]["ReviewQueryApiFunction"]["Properties"]["CodeUri"] = (
            closure._predecessor_code_uri()
        )
    elif mutation == "global_release":
        predecessor["Parameters"]["ReleaseFingerprint"] = _parameter("a" * 64)
    elif mutation == "agentcore_predecessor":
        predecessor["Parameters"]["AgentCoreRuntimeVersion"] = _parameter("2")
    elif mutation == "scaffold":
        predecessor["Globals"]["Function"]["Environment"]["Variables"][
            "MR_LISTER_PHASE6_SCAFFOLD_ONLY"
        ] = "true"
    else:
        predecessor["Outputs"]["DeploymentReadiness"]["Value"] = "PHASE7_ACTIVE"

    with pytest.raises(closure.Phase6ArtworkClosureError):
        _render(monkeypatch, predecessor)


def test_render_rejects_byte_drift_and_duplicate_json() -> None:
    with pytest.raises(closure.Phase6ArtworkClosureError):
        closure.render_phase6_artwork_closure(_canonical(_predecessor()), _binding())

    duplicate = b'{"Globals":{},"Globals":{},"Outputs":{},"Parameters":{},"Resources":{}}\n'
    original = closure.PREDECESSOR_TEMPLATE_SHA256
    try:
        closure.PREDECESSOR_TEMPLATE_SHA256 = sha256(duplicate).hexdigest()
        with pytest.raises(closure.Phase6ArtworkClosureError):
            closure.render_phase6_artwork_closure(duplicate, _binding())
    finally:
        closure.PREDECESSOR_TEMPLATE_SHA256 = original


def _bind_private_write(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> tuple[Path, bytes]:
    repository = tmp_path / "repository"
    predecessor_path = repository / "input/predecessor.json"
    predecessor_path.parent.mkdir(parents=True)
    predecessor_raw = _canonical(_predecessor())
    predecessor_path.write_bytes(predecessor_raw)
    output = repository / ".mr_lister_private/closure/target.json"
    monkeypatch.setattr(closure, "REPOSITORY_ROOT", repository)
    monkeypatch.setattr(closure, "DEFAULT_PREDECESSOR_PATH", predecessor_path)
    monkeypatch.setattr(closure, "PREDECESSOR_TEMPLATE_SHA256", sha256(predecessor_raw).hexdigest())
    rendered = closure.render_phase6_artwork_closure(predecessor_raw, _binding())
    return output, rendered


def test_private_write_is_create_only_owner_only_and_separately_verifiable(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    output, rendered = _bind_private_write(monkeypatch, tmp_path)

    assert closure.write_phase6_artwork_closure(_binding(), output_path=output) == output
    assert output.read_bytes() == rendered
    assert stat.S_IMODE(output.stat().st_mode) == 0o600
    assert stat.S_IMODE(output.parent.stat().st_mode) == 0o700
    assert closure.verify_phase6_artwork_closure(_binding(), output_path=output) == output

    with pytest.raises(closure.Phase6ArtworkClosureError):
        closure.write_phase6_artwork_closure(_binding(), output_path=output)
    output.write_bytes(b"drifted\n")
    with pytest.raises(closure.Phase6ArtworkClosureError):
        closure.verify_phase6_artwork_closure(_binding(), output_path=output)


def test_private_write_rejects_symlinked_private_root(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    output, _ = _bind_private_write(monkeypatch, tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (closure.REPOSITORY_ROOT / ".mr_lister_private").symlink_to(
        outside,
        target_is_directory=True,
    )

    with pytest.raises(closure.Phase6ArtworkClosureError):
        closure.write_phase6_artwork_closure(_binding(), output_path=output)
    assert not (outside / "closure/target.json").exists()


def test_private_write_rejects_symlinked_repository_root(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    output, _ = _bind_private_write(monkeypatch, tmp_path)
    repository = closure.REPOSITORY_ROOT
    linked_repository = tmp_path / "linked-repository"
    linked_repository.symlink_to(repository, target_is_directory=True)
    linked_output = linked_repository / output.relative_to(repository)
    monkeypatch.setattr(closure, "REPOSITORY_ROOT", linked_repository)
    monkeypatch.setattr(
        closure,
        "DEFAULT_PREDECESSOR_PATH",
        linked_repository / "input/predecessor.json",
    )

    with pytest.raises(closure.Phase6ArtworkClosureError):
        closure.write_phase6_artwork_closure(_binding(), output_path=linked_output)
    assert not output.exists()


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

    monkeypatch.setattr(closure.os, "link", swapping_link)
    try:
        with pytest.raises(closure.Phase6ArtworkClosureError):
            closure.write_phase6_artwork_closure(_binding(), output_path=output)
    finally:
        if parent.is_symlink():
            parent.unlink()
        if relocated.exists():
            relocated.rename(parent)

    assert swapped is True
    assert not output.exists()
    assert not (outside / output.name).exists()


def test_fixed_predecessor_and_bounded_resource_authority() -> None:
    assert closure.PREDECESSOR_TEMPLATE_SHA256 == (
        "6a6775a01f7c836ba90efb8f0a9259d389daac32b52c5bf553a4752aeb9f8791"
    )
    assert closure._ARTWORK_CLOSURE_FUNCTION_IDS == (
        "PreparationDispatchFunction",
        "ProviderDraftFunction",
        "UploadApiFunction",
    )
    assert len(closure._FUNCTION_LOGICAL_IDS) == 10
    assert len(set(closure._FUNCTION_LOGICAL_IDS)) == 10
