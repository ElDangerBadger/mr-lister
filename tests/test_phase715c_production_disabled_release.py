"""Sealed, release-first Phase 7.15C production-disabled candidate checks."""

from __future__ import annotations

import ast
import base64
import csv
import json
import subprocess
import sys
import zipfile
from collections.abc import Iterator, Mapping
from hashlib import sha256
from io import StringIO
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

import mr_lister.release.phase6 as phase6_release
from mr_lister.cloud import phase7_production_entrypoints as entrypoints
from mr_lister.release.phase6 import (
    DEPENDENCY_BUILD_REQUEST_FILENAME,
    render_manifest,
    verify_dependency_build_request,
    wheel_authority_from_build_request,
)
from mr_lister.release.phase7_production_disabled import (
    ACTIVATION_MODE,
    ACTIVATION_MODE_ENV,
    APPLICATION_RELEASE_FINGERPRINT_ENV,
    COGNITO_CLIENT_ID_ENV,
    COGNITO_GROUP_ENV,
    COGNITO_ISSUER_ENV,
    COGNITO_SCOPE_ENV,
    CONTRACT_FINGERPRINT,
    CONTRACT_FINGERPRINT_ENV,
    CONTRACT_VERSION,
    CONTRACT_VERSION_ENV,
    PHASE6_LAMBDA_WHEEL_AUTHORITY_FINGERPRINT,
    PRODUCTION_CANDIDATE_ENABLED_ENV,
    PRODUCTION_DISABLED_ENTRYPOINTS,
    PRODUCTION_DISABLED_RELEASE_FINGERPRINT_ENV,
    PRODUCTION_DISABLED_TEMPLATE_FINGERPRINT,
    PRODUCTION_THIRD_PARTY_IMPORT_ROOTS,
    PROFILE_FINGERPRINT,
    PROFILE_FINGERPRINT_ENV,
    PROFILE_ID,
    PROFILE_ID_ENV,
    PROFILE_PATH,
    PROFILE_PATH_ENV,
    PROFILE_VERSION,
    PROFILE_VERSION_ENV,
    PUBLICATION_ENABLED_ENV,
    PUBLICATION_WORKFLOW_FINGERPRINT,
    QUERY_ENABLED_ENV,
    REGION_ENV,
    REQUEST_ENABLED_ENV,
    SCAFFOLD_ONLY_ENV,
    SOURCE_MANIFEST_FILENAME,
    STATE_TABLE_ENV,
    Phase7ProductionDisabledReleaseAuthorityError,
    inventory,
    verify_phase7_production_disabled_release,
)
from tools.build_phase715_production_disabled_release import (
    PRODUCTION_DISABLED_ARCHIVE_FILENAME,
    PRODUCTION_DISABLED_ARTIFACT_DIRECTORY_NAME,
    PRODUCTION_DISABLED_DEPENDENCY_DIRECTORY_NAME,
    PRODUCTION_DISABLED_DEPLOYMENT_DIRECTORY_NAME,
    PRODUCTION_DISABLED_SOURCE_DIRECTORY_NAME,
    build_production_disabled_source_bundle,
    resolve_production_disabled_import_closure,
    seal_production_disabled_release,
    verify_production_disabled_deployment_artifact,
    write_linux_arm64_dependency_manifest,
)

ROOT = Path(__file__).resolve().parents[1]


class _PoisonEvent(Mapping[str, Any]):
    def __getitem__(self, key: str) -> Any:
        raise AssertionError(f"event accessed: {key}")

    def __iter__(self) -> Iterator[str]:
        raise AssertionError("event iterated")

    def __len__(self) -> int:
        raise AssertionError("event sized")


def _source(tmp_path: Path, name: str) -> Path:
    return build_production_disabled_source_bundle(
        tmp_path / name / PRODUCTION_DISABLED_SOURCE_DIRECTORY_NAME
    )


def _synthetic_arm64_elf() -> bytes:
    value = bytearray(4_096)
    value[:7] = b"\x7fELF\x02\x01\x01"
    value[16:18] = (3).to_bytes(2, "little")
    value[18:20] = (183).to_bytes(2, "little")
    value[20:24] = (1).to_bytes(4, "little")
    value[52:54] = (64).to_bytes(2, "little")
    return bytes(value)


def _dependencies(
    tmp_path: Path,
    source: Path,
    name: str,
    monkeypatch: pytest.MonkeyPatch,
) -> Path:
    root = tmp_path / name / PRODUCTION_DISABLED_DEPENDENCY_DIRECTORY_NAME
    root.mkdir(parents=True)
    request_path = source / DEPENDENCY_BUILD_REQUEST_FILENAME
    request = verify_dependency_build_request(request_path)
    requirements = request["requirements"]
    assert isinstance(requirements, dict)
    wheels = requirements["wheel_artifacts"]
    assert isinstance(wheels, list) and len(wheels) == 14
    import_roots = {
        "annotated-types": "annotated_types",
        "awscrt": "awscrt",
        "boto3": "boto3",
        "botocore": "botocore",
        "jmespath": "jmespath",
        "pillow": "PIL",
        "pydantic": "pydantic",
        "pydantic-core": "pydantic_core",
        "python-dateutil": "dateutil",
        "s3transfer": "s3transfer",
        "six": "six.py",
        "typing-extensions": "typing_extensions.py",
        "typing-inspection": "typing_inspection",
        "urllib3": "urllib3",
    }
    native_names = {"awscrt", "pillow", "pydantic-core"}
    owned: dict[str, list[Path]] = {}
    dist_infos: dict[str, Path] = {}
    for wheel in wheels:
        assert isinstance(wheel, dict)
        distribution = wheel["name"]
        version = wheel["version"]
        assert isinstance(distribution, str) and isinstance(version, str)
        package = root / import_roots[distribution]
        if package.suffix == ".py":
            package.write_text("VERSION = 'test'\n", encoding="utf-8")
            package_files = [package]
        else:
            package.mkdir()
            initializer = package / "__init__.py"
            initializer.write_text("VERSION = 'test'\n", encoding="utf-8")
            package_files = [initializer]
        dist_info = root / f"{distribution.replace('-', '_')}-{version}.dist-info"
        dist_info.mkdir()
        metadata = dist_info / "METADATA"
        metadata.write_text(
            f"Metadata-Version: 2.4\nName: {distribution}\nVersion: {version}\n\n",
            encoding="utf-8",
        )
        wheel_metadata = dist_info / "WHEEL"
        tag = (
            "cp312-cp312-manylinux_2_17_aarch64.manylinux2014_aarch64"
            if distribution in native_names
            else "py3-none-any"
        )
        wheel_metadata.write_text(
            "Wheel-Version: 1.0\nGenerator: phase715c-test\n"
            f"Root-Is-Purelib: {'false' if distribution in native_names else 'true'}\n"
            f"Tag: {tag}\n\n",
            encoding="utf-8",
        )
        owned[distribution] = [*package_files, metadata, wheel_metadata]
        dist_infos[distribution] = dist_info

    native_paths = {
        "awscrt": root / "_awscrt.abi3.so",
        "pillow": root / "PIL/_imaging.cpython-312-aarch64-linux-gnu.so",
        "pydantic-core": root / "pydantic_core/_pydantic_core.cpython-312-aarch64-linux-gnu.so",
    }
    for distribution, native in native_paths.items():
        native.write_bytes(_synthetic_arm64_elf())
        owned[distribution].append(native)

    for distribution in sorted(owned):
        record = dist_infos[distribution] / "RECORD"
        rows: list[list[str]] = []
        for path in sorted(owned[distribution]):
            raw = path.read_bytes()
            encoded = base64.urlsafe_b64encode(sha256(raw).digest()).decode().rstrip("=")
            rows.append([path.relative_to(root).as_posix(), f"sha256={encoded}", str(len(raw))])
        rows.append([record.relative_to(root).as_posix(), "", ""])
        output = StringIO(newline="")
        csv.writer(output, lineterminator="\n").writerows(rows)
        record.write_text(output.getvalue(), encoding="utf-8")

    expected_tree = requirements["dependency_tree_sha256"]
    assert isinstance(expected_tree, str)
    monkeypatch.setattr(
        phase6_release, "_dependency_tree_fingerprint", lambda _files: expected_tree
    )
    write_linux_arm64_dependency_manifest(root, build_request_path=request_path)
    return root


def _sealed(
    tmp_path: Path,
    name: str,
    monkeypatch: pytest.MonkeyPatch,
):  # type: ignore[no-untyped-def]
    source = _source(tmp_path, f"{name}-source")
    dependencies = _dependencies(tmp_path, source, f"{name}-dependencies", monkeypatch)
    return seal_production_disabled_release(
        source,
        dependencies=dependencies,
        deployment_destination=tmp_path / name / PRODUCTION_DISABLED_DEPLOYMENT_DIRECTORY_NAME,
        artifact_destination=tmp_path / name / PRODUCTION_DISABLED_ARTIFACT_DIRECTORY_NAME,
    )


def _environment(artifact: Any) -> dict[str, object]:
    return {
        PRODUCTION_DISABLED_RELEASE_FINGERPRINT_ENV: artifact.release_fingerprint,
        APPLICATION_RELEASE_FINGERPRINT_ENV: artifact.release_fingerprint,
        CONTRACT_FINGERPRINT_ENV: CONTRACT_FINGERPRINT,
        CONTRACT_VERSION_ENV: CONTRACT_VERSION,
        ACTIVATION_MODE_ENV: ACTIVATION_MODE,
        PROFILE_ID_ENV: PROFILE_ID,
        PROFILE_VERSION_ENV: str(PROFILE_VERSION),
        PROFILE_FINGERPRINT_ENV: PROFILE_FINGERPRINT,
        PROFILE_PATH_ENV: (artifact.deployment_root / PROFILE_PATH).as_posix(),
        SCAFFOLD_ONLY_ENV: "true",
        QUERY_ENABLED_ENV: "false",
        REQUEST_ENABLED_ENV: "false",
        PUBLICATION_ENABLED_ENV: "false",
        PRODUCTION_CANDIDATE_ENABLED_ENV: "false",
        REGION_ENV: "us-west-2",
        STATE_TABLE_ENV: "mr-lister-phase6-dev",
        COGNITO_ISSUER_ENV: ("https://cognito-idp.us-west-2.amazonaws.com/us-west-2_Phase715Test"),
        COGNITO_CLIENT_ID_ENV: "phase715testclient",
        COGNITO_SCOPE_ENV: "mr-lister-api/seller",
        COGNITO_GROUP_ENV: "seller",
    }


def test_source_is_deterministic_full_production_closure_and_topology_bound(
    tmp_path: Path,
) -> None:
    first = _source(tmp_path, "first")
    second = _source(tmp_path, "second")
    closure = resolve_production_disabled_import_closure()
    manifest = json.loads((first / SOURCE_MANIFEST_FILENAME).read_bytes())

    assert (first / SOURCE_MANIFEST_FILENAME).read_bytes() == (
        second / SOURCE_MANIFEST_FILENAME
    ).read_bytes()
    assert len(closure) == 74
    assert manifest["files"] == inventory(first, excluded=frozenset({SOURCE_MANIFEST_FILENAME}))
    assert manifest["third_party_import_roots"] == list(PRODUCTION_THIRD_PARTY_IMPORT_ROOTS)
    assert manifest["topology"]["production_disabled_template_sha256"] == (
        PRODUCTION_DISABLED_TEMPLATE_FINGERPRINT
    )
    assert manifest["topology"]["publication_workflow_sha256"] == (PUBLICATION_WORKFLOW_FINGERPRINT)
    required = {
        "mr_lister.cloud.phase7_composition",
        "mr_lister.cloud.phase7_request_composition",
        "mr_lister.cloud.phase7_worker_composition",
        "mr_lister.cloud.phase7_provider_credentials",
        "mr_lister.cloud.phase7_operations",
        "mr_lister.cloud.phase7_operations_composition",
        "mr_lister.cloud.phase7_production_entrypoints",
        "mr_lister.release.phase7_production_disabled",
    }
    assert required.issubset(closure)
    assert all(
        (first / Path(*module.split(".")) / "__init__.py").read_bytes() == b""
        for module in (
            "mr_lister",
            "mr_lister.cloud",
            "mr_lister.control",
            "mr_lister.production",
            "mr_lister.publication",
            "mr_lister.release",
            "mr_lister.workflow",
        )
    )
    assert (first / "mr_lister/contracts/__init__.py").read_bytes() == (
        ROOT / "src/mr_lister/contracts/__init__.py"
    ).read_bytes()

    authority = wheel_authority_from_build_request(first / DEPENDENCY_BUILD_REQUEST_FILENAME)
    assert len(authority["wheels"]) == 14
    assert sha256(render_manifest(authority)).hexdigest() == (
        PHASE6_LAMBDA_WHEEL_AUTHORITY_FINGERPRINT
    )


def test_six_entrypoints_verify_first_then_refuse_without_event_or_application_access(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    def verify(
        environment: object,
        *,
        expected_entrypoint: str,
        bundle_root: object = None,
    ) -> object:
        del environment, bundle_root
        calls.append(expected_entrypoint)
        return SimpleNamespace(entrypoint=expected_entrypoint)

    release = ModuleType("mr_lister.release.phase7_production_disabled")
    release.verify_phase7_production_disabled_release = verify  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, release.__name__, release)
    monkeypatch.setattr(entrypoints, "_environment", lambda: {})

    for exact in PRODUCTION_DISABLED_ENTRYPOINTS:
        runtime = entrypoints._LazyReleaseVerifiedRefusal(
            lambda exact=exact: entrypoints._build_release_verified_refusal(exact)
        )
        with pytest.raises(entrypoints.Phase7ProductionDisabledEntrypointError) as captured:
            runtime(_PoisonEvent())
        assert str(captured.value) == "Phase 7 production candidate is disabled"
        assert captured.value.__cause__ is None
    assert calls == list(PRODUCTION_DISABLED_ENTRYPOINTS)

    tree = ast.parse(Path(entrypoints.__file__ or "").read_text(encoding="utf-8"))
    top_level = [
        node
        for node in tree.body
        if isinstance(node, (ast.Import, ast.ImportFrom))
        and not (isinstance(node, ast.ImportFrom) and node.module == "__future__")
    ]
    assert not any(
        isinstance(node, ast.ImportFrom)
        and node.module is not None
        and node.module.startswith("mr_lister")
        for node in top_level
    )


def test_sealed_release_is_deterministic_single_authority_and_verifies_all_handlers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = _sealed(tmp_path, "first", monkeypatch)
    second = _sealed(tmp_path, "second", monkeypatch)

    assert first.release_fingerprint == second.release_fingerprint
    assert first.archive_fingerprint == second.archive_fingerprint
    assert first.archive_path.read_bytes() == second.archive_path.read_bytes()
    assert first.contract_fingerprint == CONTRACT_FINGERPRINT
    assert first.profile_fingerprint == PROFILE_FINGERPRINT
    assert first.production_disabled_template_fingerprint == (
        PRODUCTION_DISABLED_TEMPLATE_FINGERPRINT
    )
    assert first.publication_workflow_fingerprint == PUBLICATION_WORKFLOW_FINGERPRINT

    environment = _environment(first)
    for exact in PRODUCTION_DISABLED_ENTRYPOINTS:
        verified = verify_phase7_production_disabled_release(
            environment,
            expected_entrypoint=exact,
            bundle_root=first.deployment_root,
        )
        assert verified.entrypoint == exact
        assert verified.release_fingerprint == first.release_fingerprint
        assert verified.application_release_fingerprint == first.release_fingerprint
    descriptor = verify_production_disabled_deployment_artifact(
        first.deployment_root,
        archive_path=first.archive_path,
        descriptor_path=first.descriptor_path,
    )
    assert "application_release_fingerprint" not in descriptor
    assert "key_parameter" not in descriptor["s3_binding"]
    assert descriptor["s3_binding"]["key_template"] == (
        "phase7/candidates/{release_fingerprint}/production-disabled.zip"
    )
    with zipfile.ZipFile(first.archive_path) as archive:
        names = archive.namelist()
        assert names == sorted(names)
        assert "mr_lister/cloud/phase7_worker_composition.py" in names
        assert "mr_lister/cloud/phase7_operations_composition.py" in names
        assert all(member.date_time == (1980, 1, 1, 0, 0, 0) for member in archive.infolist())
        assert all(member.compress_type == zipfile.ZIP_STORED for member in archive.infolist())


@pytest.mark.parametrize(
    ("name", "value"),
    [
        (APPLICATION_RELEASE_FINGERPRINT_ENV, "a" * 64),
        (PUBLICATION_ENABLED_ENV, "true"),
        (STATE_TABLE_ENV, "wrong-table"),
        (COGNITO_SCOPE_ENV, "wrong/scope"),
    ],
)
def test_release_refuses_fingerprint_activation_or_runtime_environment_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    value: str,
) -> None:
    artifact = _sealed(tmp_path, name.replace("_", "-"), monkeypatch)
    environment = _environment(artifact)
    environment[name] = value

    with pytest.raises(Phase7ProductionDisabledReleaseAuthorityError) as captured:
        verify_phase7_production_disabled_release(
            environment,
            expected_entrypoint=PRODUCTION_DISABLED_ENTRYPOINTS[0],
            bundle_root=artifact.deployment_root,
        )
    assert str(captured.value) == "Phase 7 production-disabled release authority is invalid"
    assert captured.value.__cause__ is None


def test_packaged_entrypoint_imports_no_application_module(tmp_path: Path) -> None:
    source = _source(tmp_path, "import-order")
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sys; import mr_lister.cloud.phase7_production_entrypoints; "
                "assert not any(n.startswith('mr_lister.publication') or "
                "n.startswith('mr_lister.production') for n in sys.modules)"
            ),
        ],
        cwd=tmp_path,
        env={"PYTHONPATH": str(source)},
        capture_output=True,
        check=False,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    composition = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "from mr_lister.contracts import ProductProfile; "
                "import mr_lister.cloud.phase7_composition; assert ProductProfile"
            ),
        ],
        cwd=tmp_path,
        env={"PYTHONPATH": str(source)},
        capture_output=True,
        check=False,
        text=True,
    )
    assert composition.returncode == 0, composition.stderr
    assert PRODUCTION_DISABLED_ARCHIVE_FILENAME == "production-disabled.zip"
