"""Build the deterministic Linux ARM64 Phase 7 production-disabled candidate.

The tool performs local file operations only.  It derives the complete local import closure,
copies the frozen contract and checked product profile, reuses the reviewed Phase 6 14-wheel
Lambda authority, and emits a byte-deterministic archive.  It never resolves packages, opens the
network, calls AWS, creates a trigger, or enables a publication capability.
"""

from __future__ import annotations

import argparse
import ast
import importlib.util
import json
import shutil
import sys
import zipfile
from collections import deque
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from hashlib import sha256
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import cast

from mr_lister.release.phase6 import (
    DEPENDENCY_ARTIFACT_FILENAME,
    DEPENDENCY_BUILD_REQUEST_FILENAME,
    LINUX_ARM64_TARGET,
    LOCKED_BUILD_REQUEST_FORMAT,
    normalize_wheel_authority,
    render_locked_requirements,
    verify_linux_arm64_dependency_artifact,
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
    CONTRACT_PATH,
    CONTRACT_VERSION,
    CONTRACT_VERSION_ENV,
    DEPLOYMENT_MANIFEST_FILENAME,
    PHASE6_LAMBDA_WHEEL_AUTHORITY_FINGERPRINT,
    PRODUCTION_CANDIDATE_ENABLED_ENV,
    PRODUCTION_DISABLED_ENTRYPOINTS,
    PRODUCTION_DISABLED_RELEASE_FINGERPRINT_ENV,
    PRODUCTION_DISABLED_TEMPLATE_FINGERPRINT,
    PRODUCTION_DISABLED_TEMPLATE_PATH,
    PRODUCTION_THIRD_PARTY_IMPORT_ROOTS,
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
    SCAFFOLD_ONLY_ENV,
    SOURCE_MANIFEST_FILENAME,
    STATE_TABLE_ENV,
    TOPOLOGY_BINDING_FILENAME,
    inventory,
    render_manifest,
    verify_phase7_production_disabled_release,
    verify_phase7_production_disabled_source_manifest,
)
from tools.build_phase66_source_bundles import LAMBDA_DEPENDENCY_DIRECTORY_NAME
from tools.build_phase66_source_bundles import (
    build_linux_arm64_dependencies_from_wheelhouse as build_phase6_dependencies,
)
from tools.build_phase66_source_bundles import (
    write_linux_arm64_dependency_manifest as write_phase6_dependency_manifest,
)

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SOURCE_DESTINATION = ROOT / ".mr_lister_private/phase7-production-disabled-source"
DEFAULT_DEPENDENCY_DESTINATION = ROOT / ".mr_lister_private" / LAMBDA_DEPENDENCY_DIRECTORY_NAME
DEFAULT_DEPLOYMENT_DESTINATION = ROOT / ".mr_lister_private/phase7-production-disabled-deployment"
DEFAULT_ARTIFACT_DESTINATION = ROOT / ".mr_lister_private/phase7-production-disabled-artifact"
CHECKED_LAMBDA_WHEEL_AUTHORITY = ROOT / "config/release/phase6/phase6-lambda-wheel-authority.json"

PRODUCTION_DISABLED_SOURCE_DIRECTORY_NAME = "phase7-production-disabled-source"
PRODUCTION_DISABLED_DEPENDENCY_DIRECTORY_NAME = LAMBDA_DEPENDENCY_DIRECTORY_NAME
PRODUCTION_DISABLED_DEPLOYMENT_DIRECTORY_NAME = "phase7-production-disabled-deployment"
PRODUCTION_DISABLED_ARTIFACT_DIRECTORY_NAME = "phase7-production-disabled-artifact"
PRODUCTION_DISABLED_ARCHIVE_FILENAME = "production-disabled.zip"
PRODUCTION_DISABLED_DESCRIPTOR_FILENAME = "deployment-descriptor.json"

_ENTRYPOINT_MODULE = "mr_lister.cloud.phase7_production_entrypoints"
_RELEASE_MODULE = "mr_lister.release.phase7_production_disabled"
_SOURCE_FORMAT = "phase7-production-disabled-source-v1"
_DEPLOYMENT_FORMAT = "phase7-production-disabled-deployment-v1"
_RELEASE_FORMAT = "phase7-production-disabled-release-v1"
_DESCRIPTOR_FORMAT = "phase7-production-disabled-deployment-descriptor-v1"
_COMPONENT = "phase7-production-disabled-lambda"
_CAPABILITY_FREE_INITIALIZERS = frozenset(
    {
        "mr_lister",
        "mr_lister.cloud",
        "mr_lister.control",
        "mr_lister.production",
        "mr_lister.publication",
        "mr_lister.release",
        "mr_lister.workflow",
    }
)
_PRODUCTION_COMPOSITION_ROOTS = (
    _ENTRYPOINT_MODULE,
    _RELEASE_MODULE,
    "mr_lister.cloud.phase7_composition",
    "mr_lister.cloud.phase7_request_composition",
    "mr_lister.cloud.phase7_worker_composition",
    "mr_lister.cloud.phase7_provider_credentials",
    "mr_lister.cloud.phase7_operations",
    "mr_lister.cloud.phase7_operations_composition",
)
_FORBIDDEN_PATH_PARTS = frozenset(
    {
        ".DS_Store",
        ".env",
        ".git",
        ".mr_lister_private",
        ".playwright-mcp",
        "__pycache__",
        "browser",
        "docs",
        "infra",
        "tests",
        "web",
    }
)
_FORBIDDEN_SUFFIXES = frozenset(
    {".dylib", ".dll", ".key", ".map", ".pem", ".pyc", ".pyd", ".whl", ".zip"}
)
_ZIP_TIMESTAMP = (1980, 1, 1, 0, 0, 0)
_GENERIC_ERROR = "Phase 7 production-disabled release build is invalid"


class Phase715ProductionDisabledReleaseError(RuntimeError):
    """Value-free failure for unsafe, drifting, or incomplete candidate input."""


@dataclass(frozen=True, slots=True)
class Phase715ProductionDisabledArtifact:
    deployment_root: Path
    archive_path: Path
    descriptor_path: Path
    release_fingerprint: str
    archive_fingerprint: str
    contract_fingerprint: str
    profile_fingerprint: str
    topology_binding_fingerprint: str
    production_disabled_template_fingerprint: str
    publication_workflow_fingerprint: str


def build_production_disabled_source_bundle(
    destination: Path,
    *,
    repository_root: Path = ROOT,
) -> Path:
    """Copy exactly the derived refusal closure, contract, profile, and locked request."""

    created: Path | None = None
    try:
        repository = repository_root.resolve(strict=True)
        source_root = _new_exact_directory(
            destination,
            PRODUCTION_DISABLED_SOURCE_DIRECTORY_NAME,
        )
        created = source_root
        modules = resolve_production_disabled_import_closure(repository)
        for module, source in modules.items():
            relative = source.relative_to(repository / "src")
            raw = b"" if module in _CAPABILITY_FREE_INITIALIZERS else source.read_bytes()
            _write_bytes(source_root / relative, raw)

        contract_source = repository / CONTRACT_PATH
        contract_raw = contract_source.read_bytes()
        contract = json.loads(contract_raw)
        if (
            contract_source.is_symlink()
            or not isinstance(contract, Mapping)
            or render_manifest(cast(Mapping[str, object], contract)) != contract_raw
            or sha256(contract_raw).hexdigest() != CONTRACT_FINGERPRINT
            or contract.get("phase") != "7"
            or contract.get("contract_version") != CONTRACT_VERSION
            or contract.get("current_activation_phase") != "offline_implementation"
            or contract.get("publication_enabled") is not False
            or contract.get("status") != "frozen"
        ):
            raise ValueError
        _copy_file(contract_source, source_root / CONTRACT_PATH)

        profile_source = repository / PROFILE_PATH
        profile_raw = profile_source.read_bytes()
        profile = json.loads(profile_raw)
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

        topology_binding = {
            "format": "phase7-production-disabled-topology-v1",
            "production_disabled_template": {
                "path": PRODUCTION_DISABLED_TEMPLATE_PATH,
                "sha256": _repository_file_fingerprint(
                    repository,
                    PRODUCTION_DISABLED_TEMPLATE_PATH,
                    expected=PRODUCTION_DISABLED_TEMPLATE_FINGERPRINT,
                ),
            },
            "publication_workflow": {
                "path": PUBLICATION_WORKFLOW_PATH,
                "sha256": _repository_file_fingerprint(
                    repository,
                    PUBLICATION_WORKFLOW_PATH,
                    expected=PUBLICATION_WORKFLOW_FINGERPRINT,
                ),
            },
        }
        topology_bytes = render_manifest(topology_binding)
        _write_bytes(source_root / TOPOLOGY_BINDING_FILENAME, topology_bytes)

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
            "entrypoints": list(PRODUCTION_DISABLED_ENTRYPOINTS),
            "files": inventory(
                source_root,
                excluded=frozenset({SOURCE_MANIFEST_FILENAME}),
            ),
            "format": _SOURCE_FORMAT,
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
            "topology": {
                "binding_sha256": sha256(topology_bytes).hexdigest(),
                "path": TOPOLOGY_BINDING_FILENAME,
                "production_disabled_template_sha256": (PRODUCTION_DISABLED_TEMPLATE_FINGERPRINT),
                "publication_workflow_sha256": PUBLICATION_WORKFLOW_FINGERPRINT,
            },
        }
        _write_bytes(source_root / SOURCE_MANIFEST_FILENAME, render_manifest(source_manifest))
        verify_phase7_production_disabled_source_manifest(source_root)
        return source_root
    except Exception as error:
        if created is not None and created.exists():
            shutil.rmtree(created)
        if isinstance(error, Phase715ProductionDisabledReleaseError):
            raise
        raise Phase715ProductionDisabledReleaseError(_GENERIC_ERROR) from None


def resolve_production_disabled_import_closure(
    repository_root: Path = ROOT,
) -> dict[str, Path]:
    """Derive the complete local closure from release and production composition roots."""

    try:
        repository = repository_root.resolve(strict=True)
        source_root = repository / "src"
        queue: deque[str] = deque(_PRODUCTION_COMPOSITION_ROOTS)
        resolved: dict[str, Path] = {}
        while queue:
            module = queue.popleft()
            if module in resolved:
                continue
            source = _module_path(source_root, module)
            if source is None:
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
        if not set(_PRODUCTION_COMPOSITION_ROOTS).issubset(resolved):
            raise ValueError
        if _third_party_import_roots(resolved) != PRODUCTION_THIRD_PARTY_IMPORT_ROOTS:
            raise ValueError
        return dict(sorted(resolved.items()))
    except Exception:
        raise Phase715ProductionDisabledReleaseError(_GENERIC_ERROR) from None


def build_linux_arm64_dependencies_from_wheelhouse(
    wheelhouse_root: Path,
    *,
    destination: Path,
    build_request_path: Path,
) -> Path:
    """Delegate exact wheel verification and extraction to the Phase 6 authority."""

    try:
        if destination.resolve(strict=False).name != PRODUCTION_DISABLED_DEPENDENCY_DIRECTORY_NAME:
            raise ValueError
        return build_phase6_dependencies(
            wheelhouse_root,
            destination=destination,
            build_request_path=build_request_path,
        )
    except Exception:
        raise Phase715ProductionDisabledReleaseError(_GENERIC_ERROR) from None


def write_linux_arm64_dependency_manifest(
    artifact_root: Path,
    *,
    build_request_path: Path,
) -> Path:
    """Delegate installed-tree inspection and sealing to the Phase 6 verifier."""

    try:
        if artifact_root.resolve(strict=True).name != PRODUCTION_DISABLED_DEPENDENCY_DIRECTORY_NAME:
            raise ValueError
        return write_phase6_dependency_manifest(
            artifact_root,
            build_request_path=build_request_path,
        )
    except Exception:
        raise Phase715ProductionDisabledReleaseError(_GENERIC_ERROR) from None


def seal_production_disabled_release(
    source_root: Path,
    *,
    dependencies: Path,
    deployment_destination: Path,
    artifact_destination: Path,
) -> Phase715ProductionDisabledArtifact:
    """Overlay verified bytes and emit one deterministic disabled ZIP and descriptor."""

    created: list[Path] = []
    try:
        source = source_root.resolve(strict=True)
        if source.name != PRODUCTION_DISABLED_SOURCE_DIRECTORY_NAME:
            raise ValueError
        verify_phase7_production_disabled_source_manifest(source)
        _verify_current_repository_source_authority(source)
        dependency_root = dependencies.resolve(strict=True)
        if dependency_root.name != PRODUCTION_DISABLED_DEPENDENCY_DIRECTORY_NAME:
            raise ValueError
        verify_linux_arm64_dependency_artifact(
            dependency_root,
            build_request_path=source / DEPENDENCY_BUILD_REQUEST_FILENAME,
        )

        deployment = _new_exact_directory(
            deployment_destination,
            PRODUCTION_DISABLED_DEPLOYMENT_DIRECTORY_NAME,
        )
        created.append(deployment)
        _copy_tree(source, deployment)
        _overlay_dependency_tree(dependency_root, deployment)
        deployment_manifest = {
            "algorithm": "sha256",
            "component": _COMPONENT,
            "entrypoints": list(PRODUCTION_DISABLED_ENTRYPOINTS),
            "files": inventory(
                deployment,
                excluded=frozenset({DEPLOYMENT_MANIFEST_FILENAME, RELEASE_MANIFEST_FILENAME}),
            ),
            "format": _DEPLOYMENT_FORMAT,
            "target": dict(LINUX_ARM64_TARGET),
        }
        deployment_bytes = render_manifest(deployment_manifest)
        _write_bytes(deployment / DEPLOYMENT_MANIFEST_FILENAME, deployment_bytes)
        source_manifest_fingerprint = _file_fingerprint(deployment / SOURCE_MANIFEST_FILENAME)
        dependency_manifest_fingerprint = _file_fingerprint(
            deployment / DEPENDENCY_ARTIFACT_FILENAME
        )
        topology_binding_fingerprint = _file_fingerprint(deployment / TOPOLOGY_BINDING_FILENAME)
        release_manifest = {
            "algorithm": "sha256",
            "component": _COMPONENT,
            "contract_fingerprint": CONTRACT_FINGERPRINT,
            "dependency_manifest_sha256": dependency_manifest_fingerprint,
            "deployment_manifest_sha256": sha256(deployment_bytes).hexdigest(),
            "entrypoints": list(PRODUCTION_DISABLED_ENTRYPOINTS),
            "format": _RELEASE_FORMAT,
            "profile_fingerprint": PROFILE_FINGERPRINT,
            "production_disabled_template_sha256": (PRODUCTION_DISABLED_TEMPLATE_FINGERPRINT),
            "publication_workflow_sha256": PUBLICATION_WORKFLOW_FINGERPRINT,
            "source_manifest_sha256": source_manifest_fingerprint,
            "target": dict(LINUX_ARM64_TARGET),
            "topology_binding_sha256": topology_binding_fingerprint,
        }
        release_bytes = render_manifest(release_manifest)
        release_fingerprint = sha256(release_bytes).hexdigest()
        _write_bytes(deployment / RELEASE_MANIFEST_FILENAME, release_bytes)
        for entrypoint in PRODUCTION_DISABLED_ENTRYPOINTS:
            verify_phase7_production_disabled_release(
                _release_environment(
                    deployment,
                    release_fingerprint=release_fingerprint,
                ),
                expected_entrypoint=entrypoint,
                bundle_root=deployment,
            )
        _verify_current_repository_source_authority(deployment)

        artifact = _new_exact_directory(
            artifact_destination,
            PRODUCTION_DISABLED_ARTIFACT_DIRECTORY_NAME,
        )
        created.append(artifact)
        archive_path = artifact / PRODUCTION_DISABLED_ARCHIVE_FILENAME
        archive_bytes = render_deterministic_zip(deployment)
        _write_bytes(archive_path, archive_bytes)
        archive_fingerprint = sha256(archive_bytes).hexdigest()
        descriptor = {
            "algorithm": "sha256",
            "archive": {
                "path": PRODUCTION_DISABLED_ARCHIVE_FILENAME,
                "sha256": archive_fingerprint,
                "size_bytes": len(archive_bytes),
            },
            "architecture": "arm64",
            "component": _COMPONENT,
            "contract_fingerprint": CONTRACT_FINGERPRINT,
            "deployment_manifest_sha256": sha256(deployment_bytes).hexdigest(),
            "entrypoints": list(PRODUCTION_DISABLED_ENTRYPOINTS),
            "format": _DESCRIPTOR_FORMAT,
            "profile_fingerprint": PROFILE_FINGERPRINT,
            "production_disabled_template_sha256": (PRODUCTION_DISABLED_TEMPLATE_FINGERPRINT),
            "publication_workflow_sha256": PUBLICATION_WORKFLOW_FINGERPRINT,
            "release_fingerprint": release_fingerprint,
            "runtime": "python3.12",
            "s3_binding": _expected_s3_binding(),
            "topology_binding_sha256": topology_binding_fingerprint,
        }
        descriptor_path = artifact / PRODUCTION_DISABLED_DESCRIPTOR_FILENAME
        _write_bytes(descriptor_path, render_manifest(descriptor))
        verify_production_disabled_deployment_artifact(
            deployment,
            archive_path=archive_path,
            descriptor_path=descriptor_path,
        )
        return Phase715ProductionDisabledArtifact(
            deployment_root=deployment,
            archive_path=archive_path,
            descriptor_path=descriptor_path,
            release_fingerprint=release_fingerprint,
            archive_fingerprint=archive_fingerprint,
            contract_fingerprint=CONTRACT_FINGERPRINT,
            profile_fingerprint=PROFILE_FINGERPRINT,
            topology_binding_fingerprint=topology_binding_fingerprint,
            production_disabled_template_fingerprint=(PRODUCTION_DISABLED_TEMPLATE_FINGERPRINT),
            publication_workflow_fingerprint=PUBLICATION_WORKFLOW_FINGERPRINT,
        )
    except Exception as error:
        for path in reversed(created):
            if path.exists():
                shutil.rmtree(path)
        if isinstance(error, Phase715ProductionDisabledReleaseError):
            raise
        raise Phase715ProductionDisabledReleaseError(_GENERIC_ERROR) from None


def verify_production_disabled_deployment_artifact(
    deployment_root: Path,
    *,
    archive_path: Path,
    descriptor_path: Path,
) -> Mapping[str, object]:
    """Verify the extracted release, deterministic ZIP, and immutable S3 descriptor."""

    try:
        deployment = deployment_root.resolve(strict=True)
        descriptor_file = descriptor_path.resolve(strict=True)
        archive_file = archive_path.resolve(strict=True)
        if (
            descriptor_path.is_symlink()
            or not descriptor_file.is_file()
            or descriptor_file.name != PRODUCTION_DISABLED_DESCRIPTOR_FILENAME
            or archive_path.is_symlink()
            or not archive_file.is_file()
            or archive_file.name != PRODUCTION_DISABLED_ARCHIVE_FILENAME
        ):
            raise ValueError
        descriptor_raw = descriptor_file.read_bytes()
        descriptor = json.loads(descriptor_raw)
        if (
            not isinstance(descriptor, Mapping)
            or render_manifest(cast(Mapping[str, object], descriptor)) != descriptor_raw
        ):
            raise ValueError
        _require_exact_keys(
            descriptor,
            {
                "algorithm",
                "archive",
                "architecture",
                "component",
                "contract_fingerprint",
                "deployment_manifest_sha256",
                "entrypoints",
                "format",
                "profile_fingerprint",
                "production_disabled_template_sha256",
                "publication_workflow_sha256",
                "release_fingerprint",
                "runtime",
                "s3_binding",
                "topology_binding_sha256",
            },
        )
        release_fingerprint = _fingerprint(descriptor["release_fingerprint"])
        for entrypoint in PRODUCTION_DISABLED_ENTRYPOINTS:
            verified = verify_phase7_production_disabled_release(
                _release_environment(
                    deployment,
                    release_fingerprint=release_fingerprint,
                ),
                expected_entrypoint=entrypoint,
                bundle_root=deployment,
            )
            if verified.entrypoint != entrypoint:
                raise ValueError
        _verify_current_repository_source_authority(deployment)
        archive = descriptor["archive"]
        if not isinstance(archive, Mapping):
            raise ValueError
        _require_exact_keys(archive, {"path", "sha256", "size_bytes"})
        archive_raw = archive_file.read_bytes()
        expected_archive = render_deterministic_zip(deployment)
        if (
            descriptor["algorithm"] != "sha256"
            or descriptor["architecture"] != "arm64"
            or descriptor["component"] != _COMPONENT
            or descriptor["contract_fingerprint"] != CONTRACT_FINGERPRINT
            or descriptor["entrypoints"] != list(PRODUCTION_DISABLED_ENTRYPOINTS)
            or descriptor["format"] != _DESCRIPTOR_FORMAT
            or descriptor["profile_fingerprint"] != PROFILE_FINGERPRINT
            or descriptor["production_disabled_template_sha256"]
            != PRODUCTION_DISABLED_TEMPLATE_FINGERPRINT
            or descriptor["publication_workflow_sha256"] != PUBLICATION_WORKFLOW_FINGERPRINT
            or descriptor["runtime"] != "python3.12"
            or descriptor["topology_binding_sha256"]
            != _file_fingerprint(deployment / TOPOLOGY_BINDING_FILENAME)
            or archive["path"] != PRODUCTION_DISABLED_ARCHIVE_FILENAME
            or archive["sha256"] != sha256(archive_raw).hexdigest()
            or archive["size_bytes"] != len(archive_raw)
            or archive_raw != expected_archive
            or descriptor["s3_binding"] != _expected_s3_binding()
        ):
            raise ValueError
        _verify_archive_members(deployment, archive_raw)
        return descriptor
    except Exception:
        raise Phase715ProductionDisabledReleaseError(_GENERIC_ERROR) from None


def render_deterministic_zip(deployment_root: Path) -> bytes:
    """Render sorted, stored bytes with fixed metadata and no host information."""

    root = deployment_root.resolve(strict=True)
    output = BytesIO()
    with zipfile.ZipFile(
        output, "w", compression=zipfile.ZIP_STORED, strict_timestamps=True
    ) as archive:
        for record in inventory(root, excluded=frozenset()):
            relative = cast(str, record["path"])
            info = zipfile.ZipInfo(relative, date_time=_ZIP_TIMESTAMP)
            info.compress_type = zipfile.ZIP_STORED
            info.create_system = 3
            info.external_attr = 0o100644 << 16
            info.flag_bits = 0
            archive.writestr(info, (root / relative).read_bytes())
    return output.getvalue()


def _release_environment(
    root: Path,
    *,
    release_fingerprint: str,
) -> dict[str, object]:
    return {
        PRODUCTION_DISABLED_RELEASE_FINGERPRINT_ENV: release_fingerprint,
        APPLICATION_RELEASE_FINGERPRINT_ENV: release_fingerprint,
        CONTRACT_FINGERPRINT_ENV: CONTRACT_FINGERPRINT,
        CONTRACT_VERSION_ENV: CONTRACT_VERSION,
        ACTIVATION_MODE_ENV: ACTIVATION_MODE,
        PROFILE_ID_ENV: PROFILE_ID,
        PROFILE_VERSION_ENV: str(PROFILE_VERSION),
        PROFILE_FINGERPRINT_ENV: PROFILE_FINGERPRINT,
        PROFILE_PATH_ENV: (root / PROFILE_PATH).as_posix(),
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


def _write_checked_dependency_request(root: Path, *, repository: Path) -> None:
    authority_path = repository / CHECKED_LAMBDA_WHEEL_AUTHORITY.relative_to(ROOT)
    raw = authority_path.read_bytes()
    value = json.loads(raw)
    if (
        authority_path.is_symlink()
        or not isinstance(value, Mapping)
        or render_manifest(cast(Mapping[str, object], value)) != raw
        or sha256(raw).hexdigest() != PHASE6_LAMBDA_WHEEL_AUTHORITY_FINGERPRINT
    ):
        raise ValueError
    authority = normalize_wheel_authority(value, component="lambda")
    wheels = cast(Sequence[Mapping[str, str]], authority["wheels"])
    if len(wheels) != 14:
        raise ValueError
    requirements = render_locked_requirements(authority)
    _write_text(root / "requirements.txt", requirements)
    request = {
        "algorithm": "sha256",
        "component": "lambda",
        "format": LOCKED_BUILD_REQUEST_FORMAT,
        "requirements": {
            "dependency_tree_sha256": authority["dependency_tree_sha256"],
            "path": "requirements.txt",
            "required_distributions": [record["name"] for record in wheels],
            "sha256": _file_fingerprint(root / "requirements.txt"),
            "wheel_artifacts": [dict(record) for record in wheels],
        },
        "target": dict(LINUX_ARM64_TARGET),
    }
    _write_bytes(root / DEPENDENCY_BUILD_REQUEST_FILENAME, render_manifest(request))


def _verify_current_repository_source_authority(packaged_root: Path) -> None:
    root = packaged_root.resolve(strict=True)
    with TemporaryDirectory(prefix="mr-lister-phase715-current-source-") as temporary:
        current = build_production_disabled_source_bundle(
            Path(temporary) / PRODUCTION_DISABLED_SOURCE_DIRECTORY_NAME
        )
        expected = inventory(current, excluded=frozenset())
        actual: list[dict[str, object]] = []
        for record in expected:
            relative = cast(str, record["path"])
            target = root / relative
            if target.is_symlink() or not target.is_file():
                raise ValueError
            raw = target.read_bytes()
            actual.append(
                {
                    "path": relative,
                    "sha256": sha256(raw).hexdigest(),
                    "size_bytes": len(raw),
                }
            )
        if actual != expected:
            raise ValueError


def _assert_source_hygiene(root: Path, *, modules: Mapping[str, Path]) -> None:
    if (
        not set(_PRODUCTION_COMPOSITION_ROOTS).issubset(modules)
        or _third_party_import_roots(modules) != PRODUCTION_THIRD_PARTY_IMPORT_ROOTS
    ):
        raise ValueError
    for path in root.rglob("*"):
        relative = path.relative_to(root)
        if path.is_symlink() or any(part in _FORBIDDEN_PATH_PARTS for part in relative.parts):
            raise ValueError
        if path.is_file() and path.suffix.casefold() in _FORBIDDEN_SUFFIXES:
            raise ValueError
    for module in _CAPABILITY_FREE_INITIALIZERS:
        initializer = root / Path(*module.split(".")) / "__init__.py"
        if initializer.read_bytes() != b"":
            raise ValueError


def _module_path(source_root: Path, module: str) -> Path | None:
    relative = Path(*module.split("."))
    candidates = (source_root / f"{relative}.py", source_root / relative / "__init__.py")
    existing = [path for path in candidates if path.is_file() and not path.is_symlink()]
    if len(existing) > 1:
        raise ValueError
    return existing[0] if existing else None


def _parent_packages(module: str) -> tuple[str, ...]:
    parts = module.split(".")
    return tuple(".".join(parts[:index]) for index in range(1, len(parts)))


def _local_imports(source_root: Path, module: str, source: Path) -> set[str]:
    tree = ast.parse(source.read_text(encoding="utf-8"), filename=source.as_posix())
    package = module if source.name == "__init__.py" else module.rpartition(".")[0]
    imports: set[str] = set()
    for node in ast.walk(tree):
        candidates: list[str] = []
        if isinstance(node, ast.Import):
            candidates.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                relative = "." * node.level + (node.module or "")
                base = importlib.util.resolve_name(relative, package)
            else:
                base = node.module or ""
            candidates.append(base)
            candidates.extend(f"{base}.{alias.name}" for alias in node.names if alias.name != "*")
        for candidate in candidates:
            if not candidate.startswith("mr_lister"):
                continue
            if _module_path(source_root, candidate) is not None:
                imports.add(candidate)
    return imports


def _third_party_import_roots(modules: Mapping[str, Path]) -> tuple[str, ...]:
    roots: set[str] = set()
    for module, source in modules.items():
        if module in _CAPABILITY_FREE_INITIALIZERS:
            continue
        tree = ast.parse(source.read_text(encoding="utf-8"), filename=source.as_posix())
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                names.append(node.module)
            for name in names:
                root = name.partition(".")[0]
                if root not in {"__future__", "mr_lister"} and root not in sys.stdlib_module_names:
                    roots.add(root)
    return tuple(sorted(roots))


def _repository_file_fingerprint(
    repository: Path,
    relative: str,
    *,
    expected: str,
) -> str:
    path = repository / relative
    if path.is_symlink() or not path.is_file():
        raise ValueError
    raw = path.read_bytes()
    value = json.loads(raw)
    if not isinstance(value, Mapping) or sha256(raw).hexdigest() != expected:
        raise ValueError
    return expected


def _verify_archive_members(deployment: Path, raw_archive: bytes) -> None:
    expected = inventory(deployment, excluded=frozenset())
    with zipfile.ZipFile(BytesIO(raw_archive)) as archive:
        members = archive.infolist()
        if [member.filename for member in members] != [record["path"] for record in expected]:
            raise ValueError
        for member, record in zip(members, expected, strict=True):
            if (
                member.is_dir()
                or member.date_time != _ZIP_TIMESTAMP
                or member.compress_type != zipfile.ZIP_STORED
                or member.create_system != 3
                or member.external_attr != 0o100644 << 16
                or archive.read(member) != (deployment / cast(str, record["path"])).read_bytes()
            ):
                raise ValueError


def _expected_s3_binding() -> dict[str, object]:
    return {
        "archive_sha256_metadata_key": "mr-lister-archive-sha256",
        "bucket_parameter": "CandidateCodeS3Bucket",
        "head_object_version_must_match": True,
        "key_template": "phase7/candidates/{release_fingerprint}/production-disabled.zip",
        "null_object_version_forbidden": True,
        "object_version_parameter": "CandidateCodeS3ObjectVersion",
        "object_version_required": True,
        "release_fingerprint_metadata_key": "mr-lister-release-fingerprint",
        "release_fingerprint_parameter": "CandidateReleaseFingerprint",
        "server_side_encryption": "AES256",
    }


def _new_exact_directory(path: Path, expected_name: str) -> Path:
    destination = path.resolve(strict=False)
    if destination.name != expected_name or destination.exists():
        raise ValueError
    destination.mkdir(mode=0o700, parents=True)
    return destination


def _copy_file(source: Path, destination: Path) -> None:
    if source.is_symlink() or not source.is_file():
        raise ValueError
    _write_bytes(destination, source.read_bytes())


def _copy_tree(source: Path, destination: Path) -> None:
    for path in sorted(source.rglob("*")):
        if path.is_symlink():
            raise ValueError
        if path.is_file():
            _copy_file(path, destination / path.relative_to(source))


def _overlay_dependency_tree(source: Path, destination: Path) -> None:
    for path in sorted(source.rglob("*")):
        if path.is_symlink():
            raise ValueError
        if not path.is_file():
            continue
        target = destination / path.relative_to(source)
        if target.exists():
            raise ValueError
        _copy_file(path, target)


def _write_text(destination: Path, value: str) -> None:
    _write_bytes(destination, value.encode("utf-8"))


def _write_bytes(destination: Path, value: bytes) -> None:
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    destination.write_bytes(value)
    destination.chmod(0o644)


def _file_fingerprint(path: Path) -> str:
    if path.is_symlink() or not path.is_file():
        raise ValueError
    return sha256(path.read_bytes()).hexdigest()


def _is_nonzero_fingerprint(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and value != "0" * 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _fingerprint(value: object) -> str:
    if not _is_nonzero_fingerprint(value):
        raise ValueError
    return cast(str, value)


def _require_exact_keys(value: Mapping[object, object], keys: set[str]) -> None:
    if set(value) != keys:
        raise ValueError


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
    parser.add_argument(
        "--artifact-destination",
        type=Path,
        default=DEFAULT_ARTIFACT_DESTINATION,
    )
    parser.add_argument("--archive", type=Path)
    parser.add_argument("--descriptor", type=Path)
    arguments = parser.parse_args()

    if arguments.verify_source is not None:
        verify_phase7_production_disabled_source_manifest(arguments.verify_source)
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
        artifact = seal_production_disabled_release(
            arguments.seal_source_release,
            dependencies=arguments.dependencies,
            deployment_destination=arguments.deployment_destination,
            artifact_destination=arguments.artifact_destination,
        )
        print(artifact.release_fingerprint)
    elif arguments.verify_deployment is not None:
        if arguments.archive is None or arguments.descriptor is None:
            parser.error("--archive and --descriptor are required")
        verify_production_disabled_deployment_artifact(
            arguments.verify_deployment,
            archive_path=arguments.archive,
            descriptor_path=arguments.descriptor,
        )
    else:
        source = build_production_disabled_source_bundle(arguments.source_destination)
        print(source.name)


if __name__ == "__main__":
    main()


__all__ = [
    "PRODUCTION_DISABLED_ARCHIVE_FILENAME",
    "PRODUCTION_DISABLED_ARTIFACT_DIRECTORY_NAME",
    "PRODUCTION_DISABLED_DEPENDENCY_DIRECTORY_NAME",
    "PRODUCTION_DISABLED_DEPLOYMENT_DIRECTORY_NAME",
    "PRODUCTION_DISABLED_DESCRIPTOR_FILENAME",
    "PRODUCTION_DISABLED_SOURCE_DIRECTORY_NAME",
    "Phase715ProductionDisabledArtifact",
    "Phase715ProductionDisabledReleaseError",
    "build_linux_arm64_dependencies_from_wheelhouse",
    "build_production_disabled_source_bundle",
    "render_deterministic_zip",
    "resolve_production_disabled_import_closure",
    "seal_production_disabled_release",
    "verify_production_disabled_deployment_artifact",
    "write_linux_arm64_dependency_manifest",
]
