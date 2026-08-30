"""Deterministic, triggerless Phase 7.9 worker-source release checks."""

from __future__ import annotations

import ast
import json
import zipfile
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

import pytest

from mr_lister.cloud.phase7_worker_composition import (
    build_disabled_publication_worker_handler,
)
from mr_lister.publication.application import Phase7RuntimeDisabledError
from mr_lister.release.phase7 import (
    GUARD_PROFILE_FINGERPRINT,
    LINUX_ARM64_TARGET,
    PINNED_GUARD_DEPENDENCY_TREE_FINGERPRINT,
    PINNED_GUARD_WHEELS,
    inventory,
    render_manifest,
)
from tools.build_phase79_worker_source_release import (
    WORKER_COMPOSITION_ROOT,
    WORKER_SOURCE_MANIFEST_FILENAME,
    Phase79WorkerSourceReleaseError,
    build_worker_source_release,
    render_worker_source_zip,
    resolve_worker_import_closure,
    verify_worker_source_release,
)

ROOT = Path(__file__).resolve().parents[1]
PROFILE_PATH = (ROOT / "config/product_profiles/gildan_64000_swiftpod.json").resolve()
EXPECTED_MANIFEST_FINGERPRINT = "5f6039b5324bf4971419edffaaa0b607194d24b9627841bcc1606dfa124fb015"
EXPECTED_ARCHIVE_FINGERPRINT = "680ed04479e9bc7510efefb8e5aa32d06697c7a82633518a9551d411832b6b3c"
CAPABILITY_FREE_INITIALIZERS = {
    "mr_lister/__init__.py",
    "mr_lister/cloud/__init__.py",
    "mr_lister/control/__init__.py",
    "mr_lister/publication/__init__.py",
    "mr_lister/workflow/__init__.py",
}


def _build(tmp_path: Path, name: str):  # type: ignore[no-untyped-def]
    return build_worker_source_release(
        tmp_path / name / "phase7-worker-source",
        archive_path=tmp_path / name / "phase7-worker-offline.zip",
    )


def _manifest(root: Path) -> dict[str, Any]:
    value = json.loads((root / WORKER_SOURCE_MANIFEST_FILENAME).read_bytes())
    assert isinstance(value, dict)
    return value


def _rewrite_authority(
    root: Path,
    archive: Path,
    *,
    manifest: dict[str, Any],
) -> None:
    manifest["files"] = inventory(
        root,
        excluded=frozenset({WORKER_SOURCE_MANIFEST_FILENAME}),
    )
    (root / WORKER_SOURCE_MANIFEST_FILENAME).write_bytes(render_manifest(manifest))
    archive.write_bytes(render_worker_source_zip(root))


def _environment() -> dict[str, object]:
    region = "us-west-2"
    return {
        "AWS_REGION": region,
        "MR_LISTER_STATE_TABLE": "mr-lister-phase6-dev",
        "MR_LISTER_RELEASE_FINGERPRINT": "a" * 64,
        "MR_LISTER_COGNITO_ISSUER": (
            f"https://cognito-idp.{region}.amazonaws.com/{region}_Phase79Pool"
        ),
        "MR_LISTER_COGNITO_CLIENT_ID": "phase79client123",
        "MR_LISTER_COGNITO_SCOPE": "mr-lister-api/seller",
        "MR_LISTER_COGNITO_GROUP": "seller",
        "MR_LISTER_PRODUCT_PROFILE_ID": "gildan_64000_swiftpod",
        "MR_LISTER_PRODUCT_PROFILE_VERSION": "2",
        "MR_LISTER_PRODUCT_PROFILE_FINGERPRINT": GUARD_PROFILE_FINGERPRINT,
        "MR_LISTER_PRODUCT_PROFILE_PATH": str(PROFILE_PATH),
        "MR_LISTER_PHASE7_SCAFFOLD_ONLY": "true",
        "MR_LISTER_PHASE7_QUERY_ENABLED": "false",
        "MR_LISTER_PHASE7_REQUEST_ENABLED": "false",
        "MR_LISTER_PHASE7_PUBLICATION_ENABLED": "false",
    }


class ExplosiveEvent(Mapping[str, Any]):
    def __getitem__(self, _key: str) -> Any:
        raise AssertionError("Disabled worker observed event material")

    def __iter__(self) -> Iterator[str]:
        raise AssertionError("Disabled worker iterated event material")

    def __len__(self) -> int:
        raise AssertionError("Disabled worker measured event material")


def test_triggerless_worker_source_release_is_byte_deterministic(tmp_path: Path) -> None:
    first = _build(tmp_path, "first")
    second = _build(tmp_path, "second")

    assert first.manifest_fingerprint == second.manifest_fingerprint
    assert first.archive_fingerprint == second.archive_fingerprint
    assert first.manifest_fingerprint == EXPECTED_MANIFEST_FINGERPRINT
    assert first.archive_fingerprint == EXPECTED_ARCHIVE_FINGERPRINT
    assert (first.source_root / WORKER_SOURCE_MANIFEST_FILENAME).read_bytes() == (
        second.source_root / WORKER_SOURCE_MANIFEST_FILENAME
    ).read_bytes()
    assert first.archive_path.read_bytes() == second.archive_path.read_bytes()

    raw = (first.source_root / WORKER_SOURCE_MANIFEST_FILENAME).read_bytes()
    assert str(ROOT).encode() not in raw
    assert b"/Users/" not in raw
    manifest = _manifest(first.source_root)
    assert manifest["files"] == inventory(
        first.source_root,
        excluded=frozenset({WORKER_SOURCE_MANIFEST_FILENAME}),
    )

    with zipfile.ZipFile(first.archive_path) as archive:
        members = archive.infolist()
        assert [member.filename for member in members] == sorted(
            member.filename for member in members
        )
        assert all(member.date_time == (1980, 1, 1, 0, 0, 0) for member in members)
        assert all(member.compress_type == zipfile.ZIP_STORED for member in members)
        assert all(member.external_attr == 0o100644 << 16 for member in members)


def test_worker_closure_is_exact_and_has_no_default_aws_or_runtime_registration(
    tmp_path: Path,
) -> None:
    closure = resolve_worker_import_closure()
    artifact = _build(tmp_path, "closure")
    manifest = _manifest(artifact.source_root)
    paths = {record["path"] for record in manifest["files"]}

    assert len(closure) == 47
    assert WORKER_COMPOSITION_ROOT in closure
    assert "mr_lister.cloud.phase7_configuration" in closure
    assert "mr_lister.publication.provider_runtime" in closure
    assert "mr_lister.cloud.phase7_composition" not in closure
    assert "mr_lister.cloud.phase7_provider_credentials" not in closure
    assert not any(
        "entrypoint" in module or module.startswith("mr_lister.production") for module in closure
    )
    assert not any("phase6" in module for module in closure)
    assert not any("entrypoint" in path or "lambda" in path for path in paths)
    assert CAPABILITY_FREE_INITIALIZERS <= paths
    assert all(
        (artifact.source_root / path).read_bytes() == b"" for path in CAPABILITY_FREE_INITIALIZERS
    )

    combined = "\n".join(
        path.read_text(encoding="utf-8")
        for module, path in closure.items()
        if module
        not in {
            "mr_lister.publication.provider_boundary",
            "mr_lister.control.dispatch",
            "mr_lister.publication.execution_dynamodb",
            "mr_lister.publication.dynamodb",
        }
    )
    assert "default_aws_client_factory" not in combined
    assert "boto3.client" not in combined
    assert "boto3.resource" not in combined

    root_tree = ast.parse(closure[WORKER_COMPOSITION_ROOT].read_text(encoding="utf-8"))
    definitions = {
        node.name
        for node in ast.walk(root_tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    assert not {name for name in definitions if "handler" in name and name.startswith("lambda")}
    assert "main" not in definitions


def test_source_release_binds_disabled_profile_contract_and_dependency_status(
    tmp_path: Path,
) -> None:
    artifact = _build(tmp_path, "authority")
    manifest = _manifest(artifact.source_root)

    assert set(manifest) == {
        "activation",
        "algorithm",
        "artifact_kind",
        "component",
        "composition_roots",
        "contract",
        "dependencies",
        "deployable",
        "files",
        "format",
        "profile",
    }
    assert manifest["activation"] == {
        "publication_enabled": False,
        "query_enabled": False,
        "request_enabled": False,
        "scaffold_only": True,
    }
    assert manifest["deployable"] is False
    assert manifest["profile"] == {
        "fingerprint": GUARD_PROFILE_FINGERPRINT,
        "path": "config/product_profiles/gildan_64000_swiftpod.json",
        "profile_id": "gildan_64000_swiftpod",
        "profile_version": 2,
        "publish_enabled": False,
    }
    dependencies = manifest["dependencies"]
    assert dependencies["runtime_bytes_included"] is False
    assert dependencies["target"] == LINUX_ARM64_TARGET
    assert dependencies["required_import_roots"] == ["PIL", "botocore", "pydantic"]
    assert dependencies["additional_unsealed_distributions"] == [
        {"import_root": "PIL", "name": "Pillow"}
    ]
    baseline = dependencies["reviewed_guard_baseline"]
    assert baseline["tree_sha256"] == PINNED_GUARD_DEPENDENCY_TREE_FINGERPRINT
    assert [record["name"] for record in baseline["distributions"]] == [
        name for name, _version, _filename, _fingerprint in PINNED_GUARD_WHEELS
    ]
    assert not {"entrypoint", "events", "handler", "iam", "s3_binding", "triggers"} & set(manifest)

    builds = 0

    def builder():  # type: ignore[no-untyped-def]
        nonlocal builds
        builds += 1
        raise AssertionError("Disabled worker constructed its provider graph")

    handler = build_disabled_publication_worker_handler(_environment(), builder=builder)
    with pytest.raises(Phase7RuntimeDisabledError):
        handler(ExplosiveEvent())
    assert builds == 0


@pytest.mark.parametrize(
    "drift",
    [
        "source",
        "extra-file",
        "activation",
        "dependency",
        "profile",
        "handler-field",
    ],
)
def test_source_release_rejects_self_consistent_drift(tmp_path: Path, drift: str) -> None:
    artifact = _build(tmp_path, drift)
    manifest = _manifest(artifact.source_root)

    if drift == "source":
        source = artifact.source_root / "mr_lister/cloud/phase7_worker_composition.py"
        source.write_bytes(source.read_bytes() + b"\n# unreviewed drift\n")
    elif drift == "extra-file":
        (artifact.source_root / "mr_lister/publication/unreviewed.py").write_text(
            "value = 1\n",
            encoding="utf-8",
        )
    elif drift == "activation":
        manifest["activation"]["publication_enabled"] = True
    elif drift == "dependency":
        manifest["dependencies"]["reviewed_guard_baseline"]["tree_sha256"] = "b" * 64
    elif drift == "profile":
        manifest["profile"]["publish_enabled"] = True
    else:
        manifest["handler"] = "mr_lister.cloud.phase7_worker_composition.handler"

    _rewrite_authority(
        artifact.source_root,
        artifact.archive_path,
        manifest=manifest,
    )
    with pytest.raises(Phase79WorkerSourceReleaseError) as captured:
        verify_worker_source_release(
            artifact.source_root,
            archive_path=artifact.archive_path,
        )
    assert str(captured.value) == "Phase 7 worker source release is invalid"
    assert captured.value.__cause__ is None


def test_source_release_rejects_symlink_even_when_target_bytes_match(tmp_path: Path) -> None:
    artifact = _build(tmp_path, "symlink")
    source = artifact.source_root / "mr_lister/cloud/phase7_worker_composition.py"
    source.unlink()
    source.symlink_to(ROOT / "src/mr_lister/cloud/phase7_worker_composition.py")

    with pytest.raises(Phase79WorkerSourceReleaseError):
        verify_worker_source_release(
            artifact.source_root,
            archive_path=artifact.archive_path,
        )
