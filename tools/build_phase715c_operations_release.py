"""Build the deterministic Linux ARM64 Phase 7.15C provider-free operations release."""

from __future__ import annotations

import argparse
import shutil
from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import cast

from mr_lister.release.phase6 import (
    DEPENDENCY_ARTIFACT_FILENAME,
    DEPENDENCY_BUILD_REQUEST_FILENAME,
    LINUX_ARM64_TARGET,
    verify_linux_arm64_dependency_artifact,
)
from mr_lister.release.phase715c_operations import (
    APPLICATION_RELEASE_FINGERPRINT_ENV,
    CONTRACT_FINGERPRINT,
    CONTRACT_FINGERPRINT_ENV,
    CONTRACT_PATH,
    CONTRACT_VERSION,
    CONTRACT_VERSION_ENV,
    CURRENT_PUBLICATION_WORKFLOW_ARN,
    CURRENT_REGION,
    CURRENT_STATE_TABLE,
    DEPLOYMENT_MANIFEST_FILENAME,
    DISPATCHER_ENABLED_ENV,
    OPERATIONS_BINDING_FILENAME,
    OPERATIONS_ENTRYPOINTS,
    OPERATIONS_MODE,
    OPERATIONS_MODE_ENV,
    OPERATIONS_RELEASE_FINGERPRINT_ENV,
    OPERATIONS_THIRD_PARTY_IMPORT_ROOTS,
    PROFILE_FILE_FINGERPRINT,
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
    PUBLICATION_WORKFLOW_PATH,
    QUERY_ENABLED_ENV,
    REGION_ENV,
    RELEASE_MANIFEST_FILENAME,
    REQUEST_ENABLED_ENV,
    SOURCE_MANIFEST_FILENAME,
    STATE_TABLE_ENV,
    WORKER_ENABLED_ENV,
    WORKFLOW_ARN_ENV,
    inventory,
    operations_binding_document,
    render_manifest,
    verify_phase715c_operations_release,
    verify_phase715c_operations_source_manifest,
)
from tools.build_phase66_source_bundles import (
    LAMBDA_DEPENDENCY_DIRECTORY_NAME,
)
from tools.build_phase66_source_bundles import (
    build_linux_arm64_dependencies_from_wheelhouse as build_phase6_dependencies,
)
from tools.build_phase66_source_bundles import (
    write_linux_arm64_dependency_manifest as write_phase6_dependency_manifest,
)
from tools.build_phase715_production_disabled_release import (
    _copy_file,
    _copy_tree,
    _file_fingerprint,
    _local_imports,
    _module_path,
    _new_exact_directory,
    _overlay_dependency_tree,
    _parent_packages,
    _third_party_import_roots,
    _verify_archive_members,
    _write_bytes,
    _write_checked_dependency_request,
    render_deterministic_zip,
)

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SOURCE_DESTINATION = ROOT / ".mr_lister_private/phase715c-operations-source"
DEFAULT_DEPENDENCY_DESTINATION = ROOT / ".mr_lister_private/phase715c-operations-dependencies"
DEFAULT_DEPLOYMENT_DESTINATION = ROOT / ".mr_lister_private/phase715c-operations-deployment"
DEFAULT_ARTIFACT_DESTINATION = ROOT / ".mr_lister_private/phase715c-operations-artifact"

OPERATIONS_SOURCE_DIRECTORY_NAME = "phase715c-operations-source"
OPERATIONS_DEPENDENCY_DIRECTORY_NAME = "phase715c-operations-dependencies"
OPERATIONS_DEPLOYMENT_DIRECTORY_NAME = "phase715c-operations-deployment"
OPERATIONS_ARTIFACT_DIRECTORY_NAME = "phase715c-operations-artifact"
OPERATIONS_ARCHIVE_FILENAME = "phase715c-operations.zip"
OPERATIONS_DESCRIPTOR_FILENAME = "phase715c-operations-deployment-descriptor.json"

_ENTRYPOINT_MODULE = "mr_lister.cloud.phase715c_operations_entrypoints"
_COMPOSITION_MODULE = "mr_lister.cloud.phase715c_operations_composition"
_HANDLERS_MODULE = "mr_lister.cloud.phase715c_operations_handlers"
_RELEASE_MODULE = "mr_lister.release.phase715c_operations"
_ROOT_MODULES = (
    _ENTRYPOINT_MODULE,
    _COMPOSITION_MODULE,
    _HANDLERS_MODULE,
    _RELEASE_MODULE,
)
_CAPABILITY_FREE_INITIALIZERS = frozenset(
    {
        "mr_lister",
        "mr_lister.cloud",
        "mr_lister.control",
        "mr_lister.publication",
        "mr_lister.release",
    }
)
_SOURCE_FORMAT = "phase715c-operations-source-v1"
_DEPLOYMENT_FORMAT = "phase715c-operations-deployment-v1"
_RELEASE_FORMAT = "phase715c-operations-release-v1"
_DESCRIPTOR_FORMAT = "phase715c-operations-deployment-descriptor-v1"
_COMPONENT = "phase715c-operations-lambda"
_GENERIC_ERROR = "Phase 7.15C operations release build is invalid"
_FORBIDDEN_MODULES = frozenset(
    {
        "mr_lister.cloud.phase7_canary_composition",
        "mr_lister.cloud.phase7_composition",
        "mr_lister.cloud.phase7_guard_composition",
        "mr_lister.cloud.phase7_operations",
        "mr_lister.cloud.phase7_operations_composition",
        "mr_lister.cloud.phase7_production_entrypoints",
        "mr_lister.cloud.phase7_provider_credentials",
        "mr_lister.cloud.phase7_request_composition",
        "mr_lister.cloud.phase7_worker_composition",
        "mr_lister.publication.provider_boundary",
    }
)


class Phase715cOperationsReleaseError(RuntimeError):
    """Value-free failure for unsafe, drifting, or incomplete operations input."""


@dataclass(frozen=True, slots=True)
class Phase715cOperationsArtifact:
    deployment_root: Path
    archive_path: Path
    descriptor_path: Path
    release_fingerprint: str
    application_release_fingerprint: str
    archive_fingerprint: str
    contract_fingerprint: str
    profile_fingerprint: str
    operations_binding_fingerprint: str
    publication_workflow_fingerprint: str


def build_operations_source_bundle(
    destination: Path,
    *,
    repository_root: Path = ROOT,
) -> Path:
    """Copy exactly the operations closure, contract, profile, binding, and locked request."""

    created: Path | None = None
    try:
        repository = repository_root.resolve(strict=True)
        source_root = _new_exact_directory(destination, OPERATIONS_SOURCE_DIRECTORY_NAME)
        created = source_root
        modules = resolve_operations_import_closure(repository)
        for module, source in modules.items():
            relative = source.relative_to(repository / "src")
            raw = b"" if module in _CAPABILITY_FREE_INITIALIZERS else source.read_bytes()
            _write_bytes(source_root / relative, raw)

        contract_source = repository / CONTRACT_PATH
        contract_raw = contract_source.read_bytes()
        contract = __import__("json").loads(contract_raw)
        if (
            contract_source.is_symlink()
            or not isinstance(contract, Mapping)
            or render_manifest(cast(Mapping[str, object], contract)) != contract_raw
            or sha256(contract_raw).hexdigest() != CONTRACT_FINGERPRINT
            or contract.get("contract_version") != CONTRACT_VERSION
            or contract.get("current_activation_phase") != "offline_implementation"
            or contract.get("publication_enabled") is not False
            or contract.get("status") != "frozen"
        ):
            raise ValueError
        _copy_file(contract_source, source_root / CONTRACT_PATH)

        profile_source = repository / PROFILE_PATH
        profile_raw = profile_source.read_bytes()
        profile = __import__("json").loads(profile_raw)
        if (
            profile_source.is_symlink()
            or not isinstance(profile, Mapping)
            or sha256(profile_raw).hexdigest() != PROFILE_FILE_FINGERPRINT
            or profile.get("profile_id") != PROFILE_ID
            or profile.get("profile_version") != PROFILE_VERSION
            or profile.get("publish_enabled") is not False
        ):
            raise ValueError
        _copy_file(profile_source, source_root / PROFILE_PATH)
        workflow = repository / PUBLICATION_WORKFLOW_PATH
        if workflow.is_symlink() or _file_fingerprint(workflow) != PUBLICATION_WORKFLOW_FINGERPRINT:
            raise ValueError

        binding_bytes = render_manifest(operations_binding_document())
        _write_bytes(source_root / OPERATIONS_BINDING_FILENAME, binding_bytes)
        _write_checked_dependency_request(source_root, repository=repository)
        _assert_source_hygiene(source_root, modules=modules)
        source_manifest = {
            "algorithm": "sha256",
            "contract": {
                "contract_version": CONTRACT_VERSION,
                "current_activation_phase": "offline_implementation",
                "path": CONTRACT_PATH,
                "publication_enabled": False,
                "sha256": CONTRACT_FINGERPRINT,
                "status": "frozen",
            },
            "entrypoints": list(OPERATIONS_ENTRYPOINTS),
            "files": inventory(source_root, excluded=frozenset({SOURCE_MANIFEST_FILENAME})),
            "format": _SOURCE_FORMAT,
            "operations_binding": {
                "path": OPERATIONS_BINDING_FILENAME,
                "sha256": sha256(binding_bytes).hexdigest(),
                "state_table": CURRENT_STATE_TABLE,
                "publication_workflow_arn": CURRENT_PUBLICATION_WORKFLOW_ARN,
            },
            "profile": {
                "file_sha256": PROFILE_FILE_FINGERPRINT,
                "fingerprint": PROFILE_FINGERPRINT,
                "path": PROFILE_PATH,
                "profile_id": PROFILE_ID,
                "profile_version": PROFILE_VERSION,
                "publish_enabled": False,
            },
            "target": dict(LINUX_ARM64_TARGET),
            "third_party_import_roots": list(_third_party_import_roots(modules)),
        }
        _write_bytes(source_root / SOURCE_MANIFEST_FILENAME, render_manifest(source_manifest))
        verify_phase715c_operations_source_manifest(source_root)
        return source_root
    except Exception as error:
        if created is not None and created.exists():
            shutil.rmtree(created)
        if isinstance(error, Phase715cOperationsReleaseError):
            raise
        raise Phase715cOperationsReleaseError(_GENERIC_ERROR) from None


def resolve_operations_import_closure(
    repository_root: Path = ROOT,
) -> dict[str, Path]:
    """Derive the complete local closure from only operations release/runtime roots."""

    try:
        repository = repository_root.resolve(strict=True)
        source_root = repository / "src"
        queue: deque[str] = deque(_ROOT_MODULES)
        resolved: dict[str, Path] = {}
        while queue:
            module = queue.popleft()
            if module in resolved:
                continue
            source = _module_path(source_root, module)
            if source is None or module in _FORBIDDEN_MODULES:
                raise ValueError
            resolved[module] = source
            for parent in _parent_packages(module):
                if parent not in resolved:
                    queue.append(parent)
            if module in _CAPABILITY_FREE_INITIALIZERS:
                continue
            for imported in _local_imports(source_root, module, source):
                if imported not in resolved:
                    queue.append(imported)
        if (
            not set(_ROOT_MODULES).issubset(resolved)
            or set(resolved) & _FORBIDDEN_MODULES
            or _third_party_import_roots(resolved) != OPERATIONS_THIRD_PARTY_IMPORT_ROOTS
        ):
            raise ValueError
        return dict(sorted(resolved.items()))
    except Exception:
        raise Phase715cOperationsReleaseError(_GENERIC_ERROR) from None


def build_linux_arm64_dependencies_from_wheelhouse(
    wheelhouse_root: Path,
    *,
    destination: Path,
    build_request_path: Path,
) -> Path:
    """Build the checked dependency tree under the operations-specific directory name."""

    try:
        target = destination.resolve(strict=False)
        if target.name != OPERATIONS_DEPENDENCY_DIRECTORY_NAME or target.exists():
            raise ValueError
        with TemporaryDirectory(prefix="phase715c-operations-dependencies-") as temporary:
            phase6_root = Path(temporary) / LAMBDA_DEPENDENCY_DIRECTORY_NAME
            build_phase6_dependencies(
                wheelhouse_root,
                destination=phase6_root,
                build_request_path=build_request_path,
            )
            target.mkdir(mode=0o700, parents=True)
            _copy_tree(phase6_root, target)
        verify_linux_arm64_dependency_artifact(target, build_request_path=build_request_path)
        return target
    except Exception:
        if destination.exists():
            shutil.rmtree(destination)
        raise Phase715cOperationsReleaseError(_GENERIC_ERROR) from None


def write_linux_arm64_dependency_manifest(
    artifact_root: Path,
    *,
    build_request_path: Path,
) -> Path:
    """Seal an operations-named installed dependency tree with the shared authority."""

    try:
        if artifact_root.resolve(strict=True).name != OPERATIONS_DEPENDENCY_DIRECTORY_NAME:
            raise ValueError
        return write_phase6_dependency_manifest(
            artifact_root,
            build_request_path=build_request_path,
        )
    except Exception:
        raise Phase715cOperationsReleaseError(_GENERIC_ERROR) from None


def seal_operations_release(
    source_root: Path,
    *,
    application_release_fingerprint: str,
    dependencies: Path,
    deployment_destination: Path,
    artifact_destination: Path,
) -> Phase715cOperationsArtifact:
    """Overlay verified dependencies and emit one deterministic operations ZIP/descriptor."""

    created: list[Path] = []
    try:
        application_release = _nonzero_fingerprint(application_release_fingerprint)
        source = source_root.resolve(strict=True)
        if source.name != OPERATIONS_SOURCE_DIRECTORY_NAME:
            raise ValueError
        verify_phase715c_operations_source_manifest(source)
        _verify_current_repository_source_authority(source)
        dependency_root = dependencies.resolve(strict=True)
        if dependency_root.name != OPERATIONS_DEPENDENCY_DIRECTORY_NAME:
            raise ValueError
        verify_linux_arm64_dependency_artifact(
            dependency_root,
            build_request_path=source / DEPENDENCY_BUILD_REQUEST_FILENAME,
        )
        deployment = _new_exact_directory(
            deployment_destination,
            OPERATIONS_DEPLOYMENT_DIRECTORY_NAME,
        )
        created.append(deployment)
        _copy_tree(source, deployment)
        _overlay_dependency_tree(dependency_root, deployment)
        deployment_manifest = {
            "algorithm": "sha256",
            "component": _COMPONENT,
            "entrypoints": list(OPERATIONS_ENTRYPOINTS),
            "files": inventory(
                deployment,
                excluded=frozenset({DEPLOYMENT_MANIFEST_FILENAME, RELEASE_MANIFEST_FILENAME}),
            ),
            "format": _DEPLOYMENT_FORMAT,
            "target": dict(LINUX_ARM64_TARGET),
        }
        deployment_bytes = render_manifest(deployment_manifest)
        _write_bytes(deployment / DEPLOYMENT_MANIFEST_FILENAME, deployment_bytes)
        binding_fingerprint = _file_fingerprint(deployment / OPERATIONS_BINDING_FILENAME)
        release_manifest = {
            "algorithm": "sha256",
            "application_release_fingerprint": application_release,
            "component": _COMPONENT,
            "contract_fingerprint": CONTRACT_FINGERPRINT,
            "dependency_manifest_sha256": _file_fingerprint(
                deployment / DEPENDENCY_ARTIFACT_FILENAME
            ),
            "deployment_manifest_sha256": sha256(deployment_bytes).hexdigest(),
            "entrypoints": list(OPERATIONS_ENTRYPOINTS),
            "format": _RELEASE_FORMAT,
            "operations_binding_sha256": binding_fingerprint,
            "profile_fingerprint": PROFILE_FINGERPRINT,
            "publication_workflow_arn": CURRENT_PUBLICATION_WORKFLOW_ARN,
            "publication_workflow_sha256": PUBLICATION_WORKFLOW_FINGERPRINT,
            "source_manifest_sha256": _file_fingerprint(deployment / SOURCE_MANIFEST_FILENAME),
            "state_table": CURRENT_STATE_TABLE,
            "target": dict(LINUX_ARM64_TARGET),
        }
        release_bytes = render_manifest(release_manifest)
        release_fingerprint = sha256(release_bytes).hexdigest()
        _write_bytes(deployment / RELEASE_MANIFEST_FILENAME, release_bytes)
        environment = _release_environment(
            deployment,
            application_release_fingerprint=application_release,
            release_fingerprint=release_fingerprint,
        )
        for entrypoint in OPERATIONS_ENTRYPOINTS:
            verify_phase715c_operations_release(
                environment,
                expected_entrypoint=entrypoint,
                bundle_root=deployment,
            )
        _verify_current_repository_source_authority(deployment)

        artifact = _new_exact_directory(
            artifact_destination,
            OPERATIONS_ARTIFACT_DIRECTORY_NAME,
        )
        created.append(artifact)
        archive_path = artifact / OPERATIONS_ARCHIVE_FILENAME
        archive_bytes = render_deterministic_zip(deployment)
        _write_bytes(archive_path, archive_bytes)
        archive_fingerprint = sha256(archive_bytes).hexdigest()
        descriptor = {
            "algorithm": "sha256",
            "archive": {
                "path": OPERATIONS_ARCHIVE_FILENAME,
                "sha256": archive_fingerprint,
                "size_bytes": len(archive_bytes),
            },
            "architecture": "arm64",
            "application_release_fingerprint": application_release,
            "component": _COMPONENT,
            "contract_fingerprint": CONTRACT_FINGERPRINT,
            "deployment_manifest_sha256": sha256(deployment_bytes).hexdigest(),
            "entrypoints": list(OPERATIONS_ENTRYPOINTS),
            "format": _DESCRIPTOR_FORMAT,
            "operations_binding_sha256": binding_fingerprint,
            "profile_fingerprint": PROFILE_FINGERPRINT,
            "publication_workflow_arn": CURRENT_PUBLICATION_WORKFLOW_ARN,
            "publication_workflow_sha256": PUBLICATION_WORKFLOW_FINGERPRINT,
            "release_fingerprint": release_fingerprint,
            "runtime": "python3.12",
            "s3_binding": _expected_s3_binding(),
            "state_table": CURRENT_STATE_TABLE,
        }
        descriptor_path = artifact / OPERATIONS_DESCRIPTOR_FILENAME
        _write_bytes(descriptor_path, render_manifest(descriptor))
        verify_operations_deployment_artifact(
            deployment,
            archive_path=archive_path,
            descriptor_path=descriptor_path,
        )
        return Phase715cOperationsArtifact(
            deployment_root=deployment,
            archive_path=archive_path,
            descriptor_path=descriptor_path,
            release_fingerprint=release_fingerprint,
            application_release_fingerprint=application_release,
            archive_fingerprint=archive_fingerprint,
            contract_fingerprint=CONTRACT_FINGERPRINT,
            profile_fingerprint=PROFILE_FINGERPRINT,
            operations_binding_fingerprint=binding_fingerprint,
            publication_workflow_fingerprint=PUBLICATION_WORKFLOW_FINGERPRINT,
        )
    except Exception as error:
        for path in reversed(created):
            if path.exists():
                shutil.rmtree(path)
        if isinstance(error, Phase715cOperationsReleaseError):
            raise
        raise Phase715cOperationsReleaseError(_GENERIC_ERROR) from None


def verify_operations_deployment_artifact(
    deployment_root: Path,
    *,
    archive_path: Path,
    descriptor_path: Path,
) -> Mapping[str, object]:
    """Verify the extracted release, deterministic ZIP, and immutable S3 descriptor."""

    try:
        deployment = deployment_root.resolve(strict=True)
        archive = archive_path.resolve(strict=True)
        descriptor_file = descriptor_path.resolve(strict=True)
        if (
            archive_path.is_symlink()
            or archive.name != OPERATIONS_ARCHIVE_FILENAME
            or not archive.is_file()
            or descriptor_path.is_symlink()
            or descriptor_file.name != OPERATIONS_DESCRIPTOR_FILENAME
            or not descriptor_file.is_file()
        ):
            raise ValueError
        raw_descriptor = descriptor_file.read_bytes()
        descriptor = __import__("json").loads(raw_descriptor)
        if (
            not isinstance(descriptor, Mapping)
            or render_manifest(cast(Mapping[str, object], descriptor)) != raw_descriptor
        ):
            raise ValueError
        release_fingerprint = str(descriptor.get("release_fingerprint", ""))
        application_release_fingerprint = str(descriptor.get("application_release_fingerprint", ""))
        environment = _release_environment(
            deployment,
            application_release_fingerprint=application_release_fingerprint,
            release_fingerprint=release_fingerprint,
        )
        for entrypoint in OPERATIONS_ENTRYPOINTS:
            verify_phase715c_operations_release(
                environment,
                expected_entrypoint=entrypoint,
                bundle_root=deployment,
            )
        raw_archive = archive.read_bytes()
        archive_record = descriptor.get("archive")
        expected = {
            "algorithm": "sha256",
            "archive": {
                "path": OPERATIONS_ARCHIVE_FILENAME,
                "sha256": sha256(raw_archive).hexdigest(),
                "size_bytes": len(raw_archive),
            },
            "architecture": "arm64",
            "application_release_fingerprint": application_release_fingerprint,
            "component": _COMPONENT,
            "contract_fingerprint": CONTRACT_FINGERPRINT,
            "deployment_manifest_sha256": _file_fingerprint(
                deployment / DEPLOYMENT_MANIFEST_FILENAME
            ),
            "entrypoints": list(OPERATIONS_ENTRYPOINTS),
            "format": _DESCRIPTOR_FORMAT,
            "operations_binding_sha256": _file_fingerprint(
                deployment / OPERATIONS_BINDING_FILENAME
            ),
            "profile_fingerprint": PROFILE_FINGERPRINT,
            "publication_workflow_arn": CURRENT_PUBLICATION_WORKFLOW_ARN,
            "publication_workflow_sha256": PUBLICATION_WORKFLOW_FINGERPRINT,
            "release_fingerprint": release_fingerprint,
            "runtime": "python3.12",
            "s3_binding": _expected_s3_binding(),
            "state_table": CURRENT_STATE_TABLE,
        }
        if (
            not isinstance(archive_record, Mapping)
            or descriptor != expected
            or raw_archive != render_deterministic_zip(deployment)
        ):
            raise ValueError
        _verify_archive_members(deployment, raw_archive)
        _verify_current_repository_source_authority(deployment)
        return descriptor
    except Exception:
        raise Phase715cOperationsReleaseError(_GENERIC_ERROR) from None


def _release_environment(
    root: Path,
    *,
    application_release_fingerprint: str,
    release_fingerprint: str,
) -> dict[str, object]:
    return {
        OPERATIONS_RELEASE_FINGERPRINT_ENV: release_fingerprint,
        APPLICATION_RELEASE_FINGERPRINT_ENV: application_release_fingerprint,
        CONTRACT_FINGERPRINT_ENV: CONTRACT_FINGERPRINT,
        CONTRACT_VERSION_ENV: CONTRACT_VERSION,
        OPERATIONS_MODE_ENV: OPERATIONS_MODE,
        PROFILE_ID_ENV: PROFILE_ID,
        PROFILE_VERSION_ENV: str(PROFILE_VERSION),
        PROFILE_FINGERPRINT_ENV: PROFILE_FINGERPRINT,
        PROFILE_PATH_ENV: (root / PROFILE_PATH).as_posix(),
        REGION_ENV: CURRENT_REGION,
        STATE_TABLE_ENV: CURRENT_STATE_TABLE,
        WORKFLOW_ARN_ENV: CURRENT_PUBLICATION_WORKFLOW_ARN,
        QUERY_ENABLED_ENV: "false",
        REQUEST_ENABLED_ENV: "false",
        PUBLICATION_ENABLED_ENV: "false",
        DISPATCHER_ENABLED_ENV: "false",
        WORKER_ENABLED_ENV: "false",
    }


def _nonzero_fingerprint(value: object) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
        or value == "0" * 64
    ):
        raise ValueError
    return value


def _verify_current_repository_source_authority(packaged_root: Path) -> None:
    root = packaged_root.resolve(strict=True)
    with TemporaryDirectory(prefix="mr-lister-phase715c-operations-source-") as temporary:
        current = build_operations_source_bundle(Path(temporary) / OPERATIONS_SOURCE_DIRECTORY_NAME)
        for record in inventory(current, excluded=frozenset()):
            relative = cast(str, record["path"])
            target = root / relative
            if (
                target.is_symlink()
                or not target.is_file()
                or target.read_bytes() != (current / relative).read_bytes()
            ):
                raise ValueError


def _assert_source_hygiene(root: Path, *, modules: Mapping[str, Path]) -> None:
    if (
        not set(_ROOT_MODULES).issubset(modules)
        or set(modules) & _FORBIDDEN_MODULES
        or _third_party_import_roots(modules) != OPERATIONS_THIRD_PARTY_IMPORT_ROOTS
    ):
        raise ValueError
    for path in root.rglob("*"):
        if path.is_symlink():
            raise ValueError
    for module in _CAPABILITY_FREE_INITIALIZERS:
        initializer = root / Path(*module.split(".")) / "__init__.py"
        if initializer.read_bytes() != b"":
            raise ValueError


def _expected_s3_binding() -> dict[str, object]:
    return {
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


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-destination", type=Path, default=DEFAULT_SOURCE_DESTINATION)
    actions = parser.add_mutually_exclusive_group()
    actions.add_argument("--verify-source", type=Path)
    actions.add_argument("--build-dependencies-from-wheelhouse", type=Path)
    actions.add_argument("--write-dependency-manifest", type=Path)
    actions.add_argument("--verify-dependency-artifact", type=Path)
    actions.add_argument("--seal-source-release", type=Path)
    actions.add_argument("--verify-deployment", type=Path)
    parser.add_argument("--build-request", type=Path)
    parser.add_argument("--dependencies", type=Path)
    parser.add_argument("--application-release-fingerprint")
    parser.add_argument(
        "--dependency-destination",
        type=Path,
        default=DEFAULT_DEPENDENCY_DESTINATION,
    )
    parser.add_argument(
        "--deployment-destination",
        type=Path,
        default=DEFAULT_DEPLOYMENT_DESTINATION,
    )
    parser.add_argument("--artifact-destination", type=Path, default=DEFAULT_ARTIFACT_DESTINATION)
    parser.add_argument("--archive", type=Path)
    parser.add_argument("--descriptor", type=Path)
    arguments = parser.parse_args()
    if arguments.verify_source is not None:
        verify_phase715c_operations_source_manifest(arguments.verify_source)
    elif arguments.build_dependencies_from_wheelhouse is not None:
        if arguments.build_request is None:
            parser.error("--build-request is required")
        build_linux_arm64_dependencies_from_wheelhouse(
            arguments.build_dependencies_from_wheelhouse,
            destination=arguments.dependency_destination,
            build_request_path=arguments.build_request,
        )
    elif arguments.write_dependency_manifest is not None:
        if arguments.build_request is None:
            parser.error("--build-request is required")
        write_linux_arm64_dependency_manifest(
            arguments.write_dependency_manifest,
            build_request_path=arguments.build_request,
        )
    elif arguments.verify_dependency_artifact is not None:
        if arguments.build_request is None:
            parser.error("--build-request is required")
        verify_linux_arm64_dependency_artifact(
            arguments.verify_dependency_artifact,
            build_request_path=arguments.build_request,
        )
    elif arguments.seal_source_release is not None:
        if arguments.dependencies is None:
            parser.error("--dependencies is required")
        if arguments.application_release_fingerprint is None:
            parser.error("--application-release-fingerprint is required")
        artifact = seal_operations_release(
            arguments.seal_source_release,
            application_release_fingerprint=arguments.application_release_fingerprint,
            dependencies=arguments.dependencies,
            deployment_destination=arguments.deployment_destination,
            artifact_destination=arguments.artifact_destination,
        )
        print(artifact.release_fingerprint)
    elif arguments.verify_deployment is not None:
        if arguments.archive is None or arguments.descriptor is None:
            parser.error("--archive and --descriptor are required")
        verify_operations_deployment_artifact(
            arguments.verify_deployment,
            archive_path=arguments.archive,
            descriptor_path=arguments.descriptor,
        )
    else:
        print(build_operations_source_bundle(arguments.source_destination).name)


if __name__ == "__main__":
    main()


__all__ = [
    "OPERATIONS_ARCHIVE_FILENAME",
    "OPERATIONS_ARTIFACT_DIRECTORY_NAME",
    "OPERATIONS_DEPENDENCY_DIRECTORY_NAME",
    "OPERATIONS_DEPLOYMENT_DIRECTORY_NAME",
    "OPERATIONS_DESCRIPTOR_FILENAME",
    "OPERATIONS_SOURCE_DIRECTORY_NAME",
    "Phase715cOperationsArtifact",
    "Phase715cOperationsReleaseError",
    "build_linux_arm64_dependencies_from_wheelhouse",
    "build_operations_source_bundle",
    "resolve_operations_import_closure",
    "seal_operations_release",
    "verify_operations_deployment_artifact",
    "write_linux_arm64_dependency_manifest",
]
