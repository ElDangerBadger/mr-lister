"""Adversarial checks for the sealed Phase 7.15C provider-free operations release."""

from __future__ import annotations

import ast
import inspect
import json
import sys
import zipfile
from hashlib import sha256
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

import mr_lister.release.phase715c_operations as release
import tools.build_phase715c_operations_release as builder
from mr_lister.cloud import phase715c_operations_entrypoints as entrypoints
from mr_lister.release.phase6 import DEPENDENCY_ARTIFACT_FILENAME, render_manifest

ROOT = Path(__file__).resolve().parents[1]
APPLICATION_RELEASE_FINGERPRINT = "b" * 64


class _RawDynamoDb:
    def get_item(self, **_request: Any) -> object:
        return {}

    def query(self, **_request: Any) -> object:
        return {}

    def transact_write_items(self, **_request: Any) -> object:
        return {}

    def delete_item(self, **_request: Any) -> object:
        raise AssertionError("delete authority must not escape the wrapper")


class _RawStepFunctions:
    def describe_execution(self, **_request: Any) -> object:
        return {}

    def redrive_execution(self, **_request: Any) -> object:
        return {}

    def start_execution(self, **_request: Any) -> object:
        raise AssertionError("start authority must not escape the wrapper")


def _environment(root: Path, fingerprint: str = "a" * 64) -> dict[str, object]:
    return {
        release.OPERATIONS_RELEASE_FINGERPRINT_ENV: fingerprint,
        release.APPLICATION_RELEASE_FINGERPRINT_ENV: APPLICATION_RELEASE_FINGERPRINT,
        release.CONTRACT_FINGERPRINT_ENV: release.CONTRACT_FINGERPRINT,
        release.CONTRACT_VERSION_ENV: release.CONTRACT_VERSION,
        release.OPERATIONS_MODE_ENV: release.OPERATIONS_MODE,
        release.PROFILE_ID_ENV: release.PROFILE_ID,
        release.PROFILE_VERSION_ENV: str(release.PROFILE_VERSION),
        release.PROFILE_FINGERPRINT_ENV: release.PROFILE_FINGERPRINT,
        release.PROFILE_PATH_ENV: (root / release.PROFILE_PATH).as_posix(),
        release.REGION_ENV: release.CURRENT_REGION,
        release.STATE_TABLE_ENV: release.CURRENT_STATE_TABLE,
        release.WORKFLOW_ARN_ENV: release.CURRENT_PUBLICATION_WORKFLOW_ARN,
        release.QUERY_ENABLED_ENV: "false",
        release.REQUEST_ENABLED_ENV: "false",
        release.PUBLICATION_ENABLED_ENV: "false",
        release.DISPATCHER_ENABLED_ENV: "false",
        release.WORKER_ENABLED_ENV: "false",
    }


def _fake_dependency_verifier(*_args: Any, **_kwargs: Any) -> dict[str, str]:
    return {"format": "phase6-linux-arm64-dependencies-v2"}


def _dependencies(root: Path) -> Path:
    dependency = root / builder.OPERATIONS_DEPENDENCY_DIRECTORY_NAME
    dependency.mkdir(parents=True)
    (dependency / DEPENDENCY_ARTIFACT_FILENAME).write_bytes(
        render_manifest(
            {
                "algorithm": "sha256",
                "files": [],
                "format": "phase6-linux-arm64-dependencies-v2",
            }
        )
    )
    return dependency


def test_release_verification_precedes_composition_and_clients_are_capability_reduced(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    seen: dict[str, object] = {}
    environment = _environment(ROOT)

    release_module = ModuleType("mr_lister.release.phase715c_operations")

    def verify(
        value: object,
        *,
        expected_entrypoint: str,
        bundle_root: object = None,
    ) -> object:
        del bundle_root
        calls.append("verify")
        assert value == environment
        return SimpleNamespace(
            application_release_fingerprint=APPLICATION_RELEASE_FINGERPRINT,
            entrypoint=expected_entrypoint,
            profile_fingerprint=release.PROFILE_FINGERPRINT,
            profile_path=(ROOT / release.PROFILE_PATH).as_posix(),
            publication_workflow_arn=release.CURRENT_PUBLICATION_WORKFLOW_ARN,
            state_table=release.CURRENT_STATE_TABLE,
        )

    release_module.verify_phase715c_operations_release = verify  # type: ignore[attr-defined]
    composition = ModuleType("mr_lister.cloud.phase715c_operations_composition")

    def compose_queue(**values: object) -> Any:
        calls.append("compose_queue")
        seen.update(values)
        return lambda _event, _context=None: {"route": "queue"}

    def compose_sweep(**values: object) -> Any:
        calls.append("compose_sweep")
        seen.update(values)
        return lambda _event, _context=None: {"route": "sweep"}

    def compose_retention(**values: object) -> Any:
        calls.append("compose_retention")
        seen.update(values)
        return lambda _event, _context=None: {"route": "retention"}

    composition.compose_publication_recovery_queue_handler = compose_queue  # type: ignore[attr-defined]
    composition.compose_publication_recovery_sweep_handler = compose_sweep  # type: ignore[attr-defined]
    composition.compose_publication_retention_handler = compose_retention  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, release_module.__name__, release_module)
    monkeypatch.setitem(sys.modules, composition.__name__, composition)
    monkeypatch.setattr(entrypoints, "_environment", lambda: environment)

    raw_dynamodb = _RawDynamoDb()
    raw_step_functions = _RawStepFunctions()

    def factory(service_name: str, *, region_name: str) -> object:
        calls.append(f"client:{service_name}")
        assert region_name == release.CURRENT_REGION
        if service_name == "dynamodb":
            return raw_dynamodb
        if service_name == "stepfunctions":
            return raw_step_functions
        raise AssertionError(service_name)

    handler = entrypoints._build_release_verified_handler(  # type: ignore[attr-defined]
        release.OPERATIONS_ENTRYPOINTS[0],
        client_factory=factory,  # type: ignore[arg-type]
    )

    assert calls[0] == "verify"
    assert calls[1:3] == ["client:dynamodb", "client:stepfunctions"]
    assert calls[3:] == ["compose_queue", "compose_sweep"]
    assert handler({"kind": "publication_recovery_sweep"}) == {"route": "sweep"}
    assert handler({"Records": [{"eventSource": "aws:sqs", "private": "opaque"}]}) == {
        "route": "queue"
    }
    dynamodb = seen["dynamodb"]
    step_functions = seen["step_functions"]
    assert not hasattr(dynamodb, "delete_item")
    assert not hasattr(step_functions, "start_execution")
    assert callable(step_functions.describe_execution)  # type: ignore[attr-defined]
    assert callable(step_functions.redrive_execution)  # type: ignore[attr-defined]

    calls.clear()
    entrypoints._build_release_verified_handler(  # type: ignore[attr-defined]
        release.OPERATIONS_ENTRYPOINTS[1],
        client_factory=factory,  # type: ignore[arg-type]
    )
    assert calls == ["verify", "client:dynamodb", "compose_retention"]


def test_entrypoint_is_stdlib_only_until_release_verification_and_has_no_start_seam() -> None:
    source = inspect.getsource(entrypoints)
    tree = ast.parse(source)
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
    assert "def start_execution" not in source
    assert set(release.OPERATIONS_ENTRYPOINTS) == {
        "mr_lister.cloud.phase715c_operations_entrypoints.publication_recovery_handler",
        "mr_lister.cloud.phase715c_operations_entrypoints.publication_retention_handler",
    }
    assert set(entrypoints.__all__) == {
        "publication_recovery_handler",
        "publication_retention_handler",
    }

    composition_tree = ast.parse(
        (ROOT / "src/mr_lister/cloud/phase715c_operations_composition.py").read_text(
            encoding="utf-8"
        )
    )
    direct_imports = {
        node.module
        for node in ast.walk(composition_tree)
        if isinstance(node, ast.ImportFrom) and node.module is not None
    }
    assert "mr_lister.control.dispatch" not in direct_imports
    assert not any(module.startswith("mr_lister.cloud.phase7_") for module in direct_imports)


def test_operations_source_is_deterministic_and_excludes_capability_modules(
    tmp_path: Path,
) -> None:
    first = builder.build_operations_source_bundle(
        tmp_path / "first" / builder.OPERATIONS_SOURCE_DIRECTORY_NAME
    )
    second = builder.build_operations_source_bundle(
        tmp_path / "second" / builder.OPERATIONS_SOURCE_DIRECTORY_NAME
    )
    assert (first / release.SOURCE_MANIFEST_FILENAME).read_bytes() == (
        second / release.SOURCE_MANIFEST_FILENAME
    ).read_bytes()
    closure = builder.resolve_operations_import_closure()
    assert set(builder._ROOT_MODULES).issubset(closure)  # type: ignore[attr-defined]
    assert not set(closure) & builder._FORBIDDEN_MODULES  # type: ignore[attr-defined]
    assert "mr_lister.publication.orchestration" in closure
    paths = {record["path"] for record in release.inventory(first, excluded=frozenset())}
    for forbidden in release._FORBIDDEN_MODULE_PATHS:  # type: ignore[attr-defined]
        assert forbidden not in paths
    binding = json.loads((first / release.OPERATIONS_BINDING_FILENAME).read_bytes())
    assert binding["state_table"] == release.CURRENT_STATE_TABLE
    assert binding["publication_workflow"]["arn"] == (release.CURRENT_PUBLICATION_WORKFLOW_ARN)
    assert binding["aws_client_allowlist"] == {
        "dynamodb": ["GetItem", "Query", "TransactWriteItems"],
        "stepfunctions": ["DescribeExecution", "RedriveExecution"],
    }


def test_operations_dependency_wrapper_copies_the_shared_dependency_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wheelhouse = tmp_path / "wheelhouse"
    wheelhouse.mkdir()
    build_request = tmp_path / "dependency-build-request.json"
    build_request.write_text("{}\n", encoding="utf-8")

    def build_shared(
        _wheelhouse: Path,
        *,
        destination: Path,
        build_request_path: Path,
    ) -> Path:
        assert build_request_path == build_request
        destination.mkdir()
        manifest = destination / DEPENDENCY_ARTIFACT_FILENAME
        manifest.write_text("shared-manifest\n", encoding="utf-8")
        return manifest

    def verify_shared(
        root: Path,
        *,
        build_request_path: Path,
        **_kwargs: object,
    ) -> dict[str, str]:
        assert root.name == builder.OPERATIONS_DEPENDENCY_DIRECTORY_NAME
        assert build_request_path == build_request
        assert (root / DEPENDENCY_ARTIFACT_FILENAME).read_text(encoding="utf-8") == (
            "shared-manifest\n"
        )
        return {"format": "phase6-linux-arm64-dependencies-v2"}

    monkeypatch.setattr(builder, "build_phase6_dependencies", build_shared)
    monkeypatch.setattr(builder, "verify_linux_arm64_dependency_artifact", verify_shared)
    destination = tmp_path / builder.OPERATIONS_DEPENDENCY_DIRECTORY_NAME
    assert (
        builder.build_linux_arm64_dependencies_from_wheelhouse(
            wheelhouse,
            destination=destination,
            build_request_path=build_request,
        )
        == destination
    )


def test_packaged_dependency_is_authenticated_under_distinct_operations_manifests(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = tmp_path / "dependency-build-request.json"
    request.write_bytes(b"request\n")
    source_file = tmp_path / "source.py"
    source_file.write_bytes(b"source\n")
    dependency_file = tmp_path / "dependency.py"
    dependency_file.write_bytes(b"dependency\n")
    source_records = [
        {
            "path": request.name,
            "sha256": sha256(request.read_bytes()).hexdigest(),
            "size_bytes": request.stat().st_size,
        },
        {
            "path": source_file.name,
            "sha256": sha256(source_file.read_bytes()).hexdigest(),
            "size_bytes": source_file.stat().st_size,
        },
    ]
    dependency_records = [
        {
            "path": dependency_file.name,
            "sha256": sha256(dependency_file.read_bytes()).hexdigest(),
            "size_bytes": dependency_file.stat().st_size,
        }
    ]
    tree_fingerprint = sha256(render_manifest({"files": dependency_records})).hexdigest()
    wheel = {
        "filename": "dependency-1.0-py3-none-any.whl",
        "name": "dependency",
        "sha256": "d" * 64,
        "version": "1.0",
    }
    dependency = {
        "algorithm": "sha256",
        "build_request_sha256": sha256(request.read_bytes()).hexdigest(),
        "dependency_tree_sha256": tree_fingerprint,
        "distributions": [
            {
                "dist_info": "dependency-1.0.dist-info",
                "name": "dependency",
                "tags": ["py3-none-any"],
                "version": "1.0",
            }
        ],
        "files": dependency_records,
        "format": "phase6-linux-arm64-dependencies-v2",
        "target": dict(release.LINUX_ARM64_TARGET),
        "wheel_artifacts": [wheel],
    }
    manifest_records = [
        {
            "path": release.SOURCE_MANIFEST_FILENAME,
            "sha256": "a" * 64,
            "size_bytes": 1,
        },
        {
            "path": DEPENDENCY_ARTIFACT_FILENAME,
            "sha256": "b" * 64,
            "size_bytes": 1,
        },
    ]
    deployment = {
        "files": sorted(
            source_records + dependency_records + manifest_records,
            key=lambda record: record["path"],
        )
    }
    source = {"files": sorted(source_records, key=lambda record: record["path"])}
    monkeypatch.setattr(
        release,
        "wheel_authority_from_build_request",
        lambda _path: {
            "component": "lambda",
            "dependency_tree_sha256": tree_fingerprint,
            "wheels": [wheel],
        },
    )

    release._verify_packaged_dependency(  # type: ignore[attr-defined]
        tmp_path,
        dependency=dependency,
        deployment=deployment,
        source=source,
    )
    dependency_file.write_bytes(b"tampered\n")
    with pytest.raises(ValueError):
        release._verify_packaged_dependency(  # type: ignore[attr-defined]
            tmp_path,
            dependency=dependency,
            deployment=deployment,
            source=source,
        )


def test_release_zip_descriptor_and_environment_are_exact_and_deterministic(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        builder,
        "verify_linux_arm64_dependency_artifact",
        _fake_dependency_verifier,
    )
    monkeypatch.setattr(release, "_verify_packaged_dependency", lambda *_args, **_kwargs: None)
    first_source = builder.build_operations_source_bundle(
        tmp_path / "first-source" / builder.OPERATIONS_SOURCE_DIRECTORY_NAME
    )
    second_source = builder.build_operations_source_bundle(
        tmp_path / "second-source" / builder.OPERATIONS_SOURCE_DIRECTORY_NAME
    )
    first = builder.seal_operations_release(
        first_source,
        application_release_fingerprint=APPLICATION_RELEASE_FINGERPRINT,
        dependencies=_dependencies(tmp_path / "first-dependencies"),
        deployment_destination=tmp_path / "first" / builder.OPERATIONS_DEPLOYMENT_DIRECTORY_NAME,
        artifact_destination=tmp_path / "first" / builder.OPERATIONS_ARTIFACT_DIRECTORY_NAME,
    )
    second = builder.seal_operations_release(
        second_source,
        application_release_fingerprint=APPLICATION_RELEASE_FINGERPRINT,
        dependencies=_dependencies(tmp_path / "second-dependencies"),
        deployment_destination=tmp_path / "second" / builder.OPERATIONS_DEPLOYMENT_DIRECTORY_NAME,
        artifact_destination=tmp_path / "second" / builder.OPERATIONS_ARTIFACT_DIRECTORY_NAME,
    )

    assert first.release_fingerprint == second.release_fingerprint
    assert first.application_release_fingerprint == APPLICATION_RELEASE_FINGERPRINT
    assert first.archive_fingerprint == second.archive_fingerprint
    assert first.archive_path.read_bytes() == second.archive_path.read_bytes()
    descriptor = builder.verify_operations_deployment_artifact(
        first.deployment_root,
        archive_path=first.archive_path,
        descriptor_path=first.descriptor_path,
    )
    assert descriptor["application_release_fingerprint"] == APPLICATION_RELEASE_FINGERPRINT
    assert descriptor["s3_binding"] == {
        "application_release_fingerprint_parameter": "ApplicationReleaseFingerprint",
        "archive_sha256_metadata_key": "mr-lister-archive-sha256",
        "bucket_parameter": "OperationsCodeS3Bucket",
        "head_object_version_must_match": True,
        "key_template": "phase7/operations/{release_fingerprint}/phase715c-operations.zip",
        "null_object_version_forbidden": True,
        "object_version_parameter": "OperationsCodeS3ObjectVersion",
        "object_version_required": True,
        "release_fingerprint_metadata_key": "mr-lister-release-fingerprint",
        "release_fingerprint_parameter": "OperationsReleaseFingerprint",
        "server_side_encryption": "AES256",
    }
    with zipfile.ZipFile(first.archive_path) as archive:
        assert archive.namelist() == sorted(archive.namelist())
        assert all(member.date_time == (1980, 1, 1, 0, 0, 0) for member in archive.infolist())
        assert all("phase7_provider" not in name for name in archive.namelist())

    environment = builder._release_environment(  # type: ignore[attr-defined]
        first.deployment_root,
        application_release_fingerprint=APPLICATION_RELEASE_FINGERPRINT,
        release_fingerprint=first.release_fingerprint,
    )
    for exact in release.OPERATIONS_ENTRYPOINTS:
        verified = release.verify_phase715c_operations_release(
            environment,
            expected_entrypoint=exact,
            bundle_root=first.deployment_root,
        )
        assert verified.entrypoint == exact
        assert verified.application_release_fingerprint == APPLICATION_RELEASE_FINGERPRINT
    environment[release.APPLICATION_RELEASE_FINGERPRINT_ENV] = "c" * 64
    with pytest.raises(release.Phase715cOperationsReleaseAuthorityError):
        release.verify_phase715c_operations_release(
            environment,
            expected_entrypoint=release.OPERATIONS_ENTRYPOINTS[0],
            bundle_root=first.deployment_root,
        )
    environment[release.APPLICATION_RELEASE_FINGERPRINT_ENV] = APPLICATION_RELEASE_FINGERPRINT
    environment[release.WORKFLOW_ARN_ENV] = release.CURRENT_PUBLICATION_WORKFLOW_ARN + "-drift"
    with pytest.raises(release.Phase715cOperationsReleaseAuthorityError):
        release.verify_phase715c_operations_release(
            environment,
            expected_entrypoint=release.OPERATIONS_ENTRYPOINTS[0],
            bundle_root=first.deployment_root,
        )
