from __future__ import annotations

import json
import zipfile
from hashlib import sha256
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

import pytest

from mr_lister.release.phase6 import LINUX_ARM64_TARGET, render_manifest
from tools.restore_phase6_deployment_artifacts import (
    Phase6ArtifactRestorationError,
    restore_phase6_deployment_artifacts,
)

RELEASE = "a" * 64
LAMBDA_ARCHIVE = "phase6-lambda.zip"
AGENTCORE_ARCHIVE = "phase6-agentcore.zip"
DESCRIPTOR = "deployment-descriptor.json"


def _zip_bytes(
    members: tuple[tuple[str, bytes, int], ...] = (("main.py", b"VALUE = 1\n", 0o100644),),
) -> bytes:
    output = BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_STORED) as archive:
        for name, content, mode in members:
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_STORED
            info.create_system = 3
            info.external_attr = mode << 16
            info.flag_bits = 0
            archive.writestr(info, content)
    return output.getvalue()


def _descriptor(lambda_raw: bytes, agentcore_raw: bytes) -> dict[str, object]:
    components: dict[str, object] = {}
    for component, filename, raw, manifest_sha in (
        ("agentcore", AGENTCORE_ARCHIVE, agentcore_raw, "b" * 64),
        ("lambda", LAMBDA_ARCHIVE, lambda_raw, "c" * 64),
    ):
        components[component] = {
            "archive": {
                "path": filename,
                "sha256": sha256(raw).hexdigest(),
                "size_bytes": len(raw),
            },
            "architecture": "arm64",
            "component": component,
            "deployment_manifest_sha256": manifest_sha,
            "package_format": "zip",
            "runtime": "python3.12",
        }
    return {
        "algorithm": "sha256",
        "components": components,
        "format": "phase6-deployment-artifacts-v1",
        "release_fingerprint": RELEASE,
        "target": dict(LINUX_ARM64_TARGET),
    }


def _preserved_release(
    tmp_path: Path,
    *,
    lambda_raw: bytes | None = None,
    agentcore_raw: bytes | None = None,
) -> tuple[Path, Path, Path, dict[str, object]]:
    private = tmp_path / "private"
    private.mkdir()
    preserved = private / f"phase6-artifacts-{RELEASE[:8]}"
    preserved.mkdir()
    lambda_bytes = lambda_raw or _zip_bytes()
    agentcore_bytes = agentcore_raw or _zip_bytes((("agent.py", b"VALUE = 2\n", 0o100644),))
    descriptor = _descriptor(lambda_bytes, agentcore_bytes)
    (preserved / LAMBDA_ARCHIVE).write_bytes(lambda_bytes)
    (preserved / AGENTCORE_ARCHIVE).write_bytes(agentcore_bytes)
    (preserved / DESCRIPTOR).write_bytes(render_manifest(descriptor))
    return (
        preserved,
        private / "phase6-deployment",
        private / "phase6-artifacts",
        descriptor,
    )


def _restore(
    preserved: Path,
    deployment: Path,
    artifacts: Path,
    descriptor: dict[str, object],
):
    verifier = patch(
        "tools.restore_phase6_deployment_artifacts.verify_phase6_deployment_artifacts",
        return_value=descriptor,
    )
    with verifier as verify:
        restored = restore_phase6_deployment_artifacts(
            preserved,
            deployment_destination=deployment,
            artifact_destination=artifacts,
        )
    return restored, verify


def _rewrite_descriptor(preserved: Path, descriptor: dict[str, object]) -> None:
    (preserved / DESCRIPTOR).write_bytes(render_manifest(descriptor))


def _replace_lambda_archive(
    preserved: Path,
    descriptor: dict[str, object],
    raw: bytes,
) -> None:
    (preserved / LAMBDA_ARCHIVE).write_bytes(raw)
    components = descriptor["components"]
    assert isinstance(components, dict)
    record = components["lambda"]
    assert isinstance(record, dict)
    archive = record["archive"]
    assert isinstance(archive, dict)
    archive["sha256"] = sha256(raw).hexdigest()
    archive["size_bytes"] = len(raw)
    _rewrite_descriptor(preserved, descriptor)


def test_restore_publishes_exact_trees_and_artifacts_then_verifies_current_source(
    tmp_path: Path,
) -> None:
    preserved, deployment, artifacts, descriptor = _preserved_release(tmp_path)
    restored, verify = _restore(preserved, deployment, artifacts, descriptor)

    assert restored.deployment_root == deployment
    assert restored.artifact_root == artifacts
    assert restored.release_fingerprint == RELEASE
    assert (deployment / "lambda/main.py").read_bytes() == b"VALUE = 1\n"
    assert (deployment / "agentcore/agent.py").read_bytes() == b"VALUE = 2\n"
    assert {path.name for path in artifacts.iterdir()} == {
        LAMBDA_ARCHIVE,
        AGENTCORE_ARCHIVE,
        DESCRIPTOR,
    }
    for filename in (LAMBDA_ARCHIVE, AGENTCORE_ARCHIVE, DESCRIPTOR):
        assert (artifacts / filename).read_bytes() == (preserved / filename).read_bytes()
    assert verify.call_count == 2
    for call in verify.call_args_list:
        assert call.args[0].name == "phase6-deployment"
        assert call.kwargs["artifact_root"].name == "phase6-artifacts"
        assert call.kwargs["verify_current_source"] is True
    assert verify.call_args_list[-1].args[0] == deployment
    assert verify.call_args_list[-1].kwargs["artifact_root"] == artifacts


@pytest.mark.parametrize(
    ("source_name", "deployment_name", "artifact_name"),
    (
        ("phase6-preserved-aaaaaaaa", "phase6-deployment", "phase6-artifacts"),
        ("phase6-artifacts-bbbbbbbb", "phase6-deployment", "phase6-artifacts"),
        ("phase6-artifacts-aaaaaaaa", "phase6-deployments", "phase6-artifacts"),
        ("phase6-artifacts-aaaaaaaa", "phase6-deployment", "phase6-artifact"),
    ),
)
def test_restore_rejects_wrong_literal_directory_names(
    tmp_path: Path,
    source_name: str,
    deployment_name: str,
    artifact_name: str,
) -> None:
    preserved, deployment, artifacts, descriptor = _preserved_release(tmp_path)
    if source_name != preserved.name:
        renamed = preserved.with_name(source_name)
        preserved.rename(renamed)
        preserved = renamed
    deployment = deployment.with_name(deployment_name)
    artifacts = artifacts.with_name(artifact_name)

    with (
        patch(
            "tools.restore_phase6_deployment_artifacts.verify_phase6_deployment_artifacts",
            return_value=descriptor,
        ) as verify,
        pytest.raises(Phase6ArtifactRestorationError),
    ):
        restore_phase6_deployment_artifacts(
            preserved,
            deployment_destination=deployment,
            artifact_destination=artifacts,
        )
    verify.assert_not_called()


@pytest.mark.parametrize("destination", ("deployment", "artifacts"))
@pytest.mark.parametrize("nonempty", (False, True))
def test_restore_never_overwrites_an_existing_destination(
    tmp_path: Path,
    destination: str,
    nonempty: bool,
) -> None:
    preserved, deployment, artifacts, descriptor = _preserved_release(tmp_path)
    existing = deployment if destination == "deployment" else artifacts
    existing.mkdir()
    if nonempty:
        (existing / "owned.txt").write_text("preserve", encoding="utf-8")

    with (
        patch(
            "tools.restore_phase6_deployment_artifacts.verify_phase6_deployment_artifacts",
            return_value=descriptor,
        ) as verify,
        pytest.raises(Phase6ArtifactRestorationError),
    ):
        restore_phase6_deployment_artifacts(
            preserved,
            deployment_destination=deployment,
            artifact_destination=artifacts,
        )
    verify.assert_not_called()
    if nonempty:
        assert (existing / "owned.txt").read_text(encoding="utf-8") == "preserve"


@pytest.mark.parametrize("drift", ("noncanonical", "hash", "size", "extra-field"))
def test_restore_rejects_noncanonical_or_drifting_descriptor_authority(
    tmp_path: Path,
    drift: str,
) -> None:
    preserved, deployment, artifacts, descriptor = _preserved_release(tmp_path)
    if drift == "noncanonical":
        (preserved / DESCRIPTOR).write_text(json.dumps(descriptor), encoding="utf-8")
    elif drift == "extra-field":
        descriptor["unexpected"] = True
        _rewrite_descriptor(preserved, descriptor)
    else:
        components = descriptor["components"]
        assert isinstance(components, dict)
        record = components["lambda"]
        assert isinstance(record, dict)
        archive = record["archive"]
        assert isinstance(archive, dict)
        if drift == "hash":
            archive["sha256"] = "f" * 64
        else:
            archive["size_bytes"] = int(archive["size_bytes"]) + 1
        _rewrite_descriptor(preserved, descriptor)

    with pytest.raises(Phase6ArtifactRestorationError):
        restore_phase6_deployment_artifacts(
            preserved,
            deployment_destination=deployment,
            artifact_destination=artifacts,
        )
    assert not deployment.exists()
    assert not artifacts.exists()


@pytest.mark.parametrize(
    "members",
    (
        (("../escape.py", b"escape", 0o100644),),
        (("/absolute.py", b"escape", 0o100644),),
        (("nested\\escape.py", b"escape", 0o100644),),
        (("link.py", b"target.py", 0o120777),),
        (("parent", b"file", 0o100644), ("parent/child.py", b"child", 0o100644)),
        (("Name.py", b"first", 0o100644), ("name.py", b"second", 0o100644)),
    ),
)
def test_restore_rejects_unsafe_or_symlink_zip_members(
    tmp_path: Path,
    members: tuple[tuple[str, bytes, int], ...],
) -> None:
    preserved, deployment, artifacts, descriptor = _preserved_release(tmp_path)
    _replace_lambda_archive(preserved, descriptor, _zip_bytes(members))

    with pytest.raises(Phase6ArtifactRestorationError):
        restore_phase6_deployment_artifacts(
            preserved,
            deployment_destination=deployment,
            artifact_destination=artifacts,
        )
    assert not deployment.exists()
    assert not artifacts.exists()
    assert not (tmp_path / "escape.py").exists()


def test_restore_rejects_symlinked_preserved_files(tmp_path: Path) -> None:
    preserved, deployment, artifacts, _descriptor_value = _preserved_release(tmp_path)
    original = preserved / LAMBDA_ARCHIVE
    backing = tmp_path / "lambda-backing.zip"
    original.rename(backing)
    original.symlink_to(backing)

    with pytest.raises(Phase6ArtifactRestorationError):
        restore_phase6_deployment_artifacts(
            preserved,
            deployment_destination=deployment,
            artifact_destination=artifacts,
        )


def test_restore_rejects_symlinked_grandparent_of_preserved_root(tmp_path: Path) -> None:
    real_root = tmp_path / "real-root"
    real_root.mkdir()
    preserved, deployment, artifacts, _descriptor_value = _preserved_release(real_root)
    alias = tmp_path / "alias"
    alias.symlink_to(real_root, target_is_directory=True)
    aliased_preserved = alias / preserved.relative_to(real_root)
    assert not aliased_preserved.is_symlink()
    assert not aliased_preserved.parent.is_symlink()

    with pytest.raises(Phase6ArtifactRestorationError):
        restore_phase6_deployment_artifacts(
            aliased_preserved,
            deployment_destination=deployment,
            artifact_destination=artifacts,
        )


def test_restore_rejects_symlinked_grandparent_of_custom_destinations(tmp_path: Path) -> None:
    preserved, _deployment, _artifacts, _descriptor_value = _preserved_release(tmp_path)
    real_root = tmp_path / "real-destination-root"
    private = real_root / "private"
    private.mkdir(parents=True)
    alias = tmp_path / "destination-alias"
    alias.symlink_to(real_root, target_is_directory=True)
    deployment = alias / "private/phase6-deployment"
    artifacts = alias / "private/phase6-artifacts"
    assert not deployment.is_symlink()
    assert not deployment.parent.is_symlink()

    with pytest.raises(Phase6ArtifactRestorationError):
        restore_phase6_deployment_artifacts(
            preserved,
            deployment_destination=deployment,
            artifact_destination=artifacts,
        )
    assert not (private / "phase6-deployment").exists()
    assert not (private / "phase6-artifacts").exists()


def test_final_current_source_failure_removes_only_new_destinations(tmp_path: Path) -> None:
    preserved, deployment, artifacts, descriptor = _preserved_release(tmp_path)
    with (
        patch(
            "tools.restore_phase6_deployment_artifacts.verify_phase6_deployment_artifacts",
            side_effect=(descriptor, ValueError("current source drift")),
        ) as verify,
        pytest.raises(Phase6ArtifactRestorationError),
    ):
        restore_phase6_deployment_artifacts(
            preserved,
            deployment_destination=deployment,
            artifact_destination=artifacts,
        )

    assert verify.call_count == 2
    assert not deployment.exists()
    assert not artifacts.exists()
    assert preserved.is_dir()
