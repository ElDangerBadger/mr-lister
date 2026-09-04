from __future__ import annotations

import json
import zipfile
from hashlib import sha256
from pathlib import Path

import pytest

import tools.build_phase718_enabled_release as builder
from mr_lister.release.phase6 import DEPENDENCY_ARTIFACT_FILENAME, render_manifest
from mr_lister.release.phase718 import (
    PHASE718_BINDING_FILENAME,
    PHASE718_CONTRACT_FINGERPRINT,
    PHASE718_ENTRYPOINTS,
    PHASE718_RELEASE_MANIFEST_FILENAME,
    PHASE718_SOURCE_MANIFEST_FILENAME,
)
from tools.build_phase718_enabled_release import (
    ENABLED_ARCHIVE_FILENAME,
    ENABLED_ARTIFACT_DIRECTORY_NAME,
    ENABLED_DEPENDENCY_DIRECTORY_NAME,
    ENABLED_DEPLOYMENT_DIRECTORY_NAME,
    ENABLED_SOURCE_DIRECTORY_NAME,
    Phase718EnabledReleaseError,
    build_enabled_source_bundle,
    resolve_enabled_import_closure,
    seal_enabled_release,
    verify_enabled_deployment_artifact,
    verify_enabled_source_bundle,
)

APPLICATION_RELEASE = "a" * 64
CANARY_EVIDENCE = "b" * 64
ENABLEMENT_EVIDENCE = "c" * 64
STATE_TABLE = "mr-lister-phase6-dev"


def _source(tmp_path: Path, name: str) -> Path:
    return build_enabled_source_bundle(
        tmp_path / name / ENABLED_SOURCE_DIRECTORY_NAME,
        application_release_fingerprint=APPLICATION_RELEASE,
        canary_evidence_fingerprint=CANARY_EVIDENCE,
        enablement_evidence_fingerprint=ENABLEMENT_EVIDENCE,
        state_table=STATE_TABLE,
    )


def _synthetic_dependencies(tmp_path: Path, name: str) -> Path:
    root = tmp_path / name / ENABLED_DEPENDENCY_DIRECTORY_NAME
    root.mkdir(parents=True)
    package = root / "synthetic_dependency.py"
    package.write_text("VALUE = 'phase718-test'\n", encoding="utf-8")
    manifest = {
        "algorithm": "sha256",
        "files": [
            {
                "path": package.name,
                "sha256": sha256(package.read_bytes()).hexdigest(),
                "size_bytes": package.stat().st_size,
            }
        ],
        "format": "phase718-test-dependency-v1",
        "target": {
            "architecture": "arm64",
            "implementation": "cpython",
            "platform": "manylinux2014_aarch64",
            "python_abi": "cp312",
            "python_version": "3.12",
        },
    }
    (root / DEPENDENCY_ARTIFACT_FILENAME).write_bytes(render_manifest(manifest))
    return root


def test_source_bundle_is_deterministic_complete_and_evidence_bound(tmp_path: Path) -> None:
    first = _source(tmp_path, "first")
    second = _source(tmp_path, "second")
    first_manifest = (first / PHASE718_SOURCE_MANIFEST_FILENAME).read_bytes()
    assert first_manifest == (second / PHASE718_SOURCE_MANIFEST_FILENAME).read_bytes()
    assert verify_enabled_source_bundle(first)["format"] == "phase718-enabled-source-v1"

    closure = resolve_enabled_import_closure()
    assert {
        "mr_lister.cloud.phase718_entrypoints",
        "mr_lister.cloud.phase718_composition",
        "mr_lister.cloud.phase718_configuration",
        "mr_lister.release.phase718",
    }.issubset(closure)
    manifest = json.loads(first_manifest)
    assert manifest["third_party_import_roots"] == ["PIL", "boto3", "botocore", "pydantic"]
    binding = json.loads((first / PHASE718_BINDING_FILENAME).read_bytes())
    assert binding == {
        "application_release_fingerprint": APPLICATION_RELEASE,
        "canary_evidence_fingerprint": CANARY_EVIDENCE,
        "contract_fingerprint": PHASE718_CONTRACT_FINGERPRINT,
        "contract_version": "7.1.0",
        "enablement_evidence_fingerprint": ENABLEMENT_EVIDENCE,
        "entrypoints": list(PHASE718_ENTRYPOINTS),
        "format": "phase718-enabled-binding-v1",
        "profile_fingerprint": ("5de1257141cfdacb1731f68bb9113712957483b33d5b0f7115afdba86eb7476c"),
        "state_table": STATE_TABLE,
    }


def test_release_seal_is_deterministic_and_runtime_verifiable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        builder, "verify_linux_arm64_dependency_artifact", lambda *_args, **_kw: None
    )
    artifacts = []
    for name in ("first", "second"):
        source = _source(tmp_path, f"{name}-source")
        dependencies = _synthetic_dependencies(tmp_path, f"{name}-dependencies")
        artifacts.append(
            seal_enabled_release(
                source,
                dependencies=dependencies,
                deployment_destination=(tmp_path / name / ENABLED_DEPLOYMENT_DIRECTORY_NAME),
                artifact_destination=tmp_path / name / ENABLED_ARTIFACT_DIRECTORY_NAME,
            )
        )

    first, second = artifacts
    assert first.release_fingerprint == second.release_fingerprint
    assert first.archive_fingerprint == second.archive_fingerprint
    assert first.archive_path.read_bytes() == second.archive_path.read_bytes()
    descriptor = verify_enabled_deployment_artifact(
        first.deployment_root,
        archive_path=first.archive_path,
        descriptor_path=first.descriptor_path,
    )
    assert descriptor["release_fingerprint"] == first.release_fingerprint
    assert descriptor["application_release_fingerprint"] == APPLICATION_RELEASE
    assert descriptor["canary_evidence_fingerprint"] == CANARY_EVIDENCE
    assert descriptor["enablement_evidence_fingerprint"] == ENABLEMENT_EVIDENCE
    assert descriptor["s3_binding"]["key_template"] == (
        "phase7/releases/{release_fingerprint}/enabled.zip"
    )
    assert first.archive_path.name == ENABLED_ARCHIVE_FILENAME

    with zipfile.ZipFile(first.archive_path) as archive:
        names = archive.namelist()
        assert names == sorted(names)
        assert PHASE718_RELEASE_MANIFEST_FILENAME in names
        assert all(member.date_time == (1980, 1, 1, 0, 0, 0) for member in archive.infolist())


def test_source_and_artifact_tampering_fail_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _source(tmp_path, "source")
    binding_path = source / PHASE718_BINDING_FILENAME
    binding = json.loads(binding_path.read_bytes())
    binding["canary_evidence_fingerprint"] = "d" * 64
    binding_path.write_bytes(render_manifest(binding))
    with pytest.raises(Phase718EnabledReleaseError):
        verify_enabled_source_bundle(source)

    source = _source(tmp_path, "clean-source")
    dependencies = _synthetic_dependencies(tmp_path, "dependencies")
    monkeypatch.setattr(
        builder, "verify_linux_arm64_dependency_artifact", lambda *_args, **_kw: None
    )
    artifact = seal_enabled_release(
        source,
        dependencies=dependencies,
        deployment_destination=tmp_path / "sealed" / ENABLED_DEPLOYMENT_DIRECTORY_NAME,
        artifact_destination=tmp_path / "sealed" / ENABLED_ARTIFACT_DIRECTORY_NAME,
    )
    artifact.archive_path.write_bytes(artifact.archive_path.read_bytes() + b"drift")
    with pytest.raises(Phase718EnabledReleaseError):
        verify_enabled_deployment_artifact(
            artifact.deployment_root,
            archive_path=artifact.archive_path,
            descriptor_path=artifact.descriptor_path,
        )


@pytest.mark.parametrize(
    ("application", "canary", "enablement", "table"),
    [
        ("0" * 64, CANARY_EVIDENCE, ENABLEMENT_EVIDENCE, STATE_TABLE),
        (APPLICATION_RELEASE, "not-a-fingerprint", ENABLEMENT_EVIDENCE, STATE_TABLE),
        (APPLICATION_RELEASE, CANARY_EVIDENCE, ENABLEMENT_EVIDENCE, "other-table"),
    ],
)
def test_source_builder_refuses_unbound_identity(
    tmp_path: Path,
    application: str,
    canary: str,
    enablement: str,
    table: str,
) -> None:
    with pytest.raises(Phase718EnabledReleaseError):
        build_enabled_source_bundle(
            tmp_path / ENABLED_SOURCE_DIRECTORY_NAME,
            application_release_fingerprint=application,
            canary_evidence_fingerprint=canary,
            enablement_evidence_fingerprint=enablement,
            state_table=table,
        )
