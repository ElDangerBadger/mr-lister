"""Build one deterministic Linux ARM64 release for an exact Phase 7.11 canary.

This tool performs local file operations only.  It derives its locked requirements and wheel
selection from the checked Phase 6 Lambda wheel authority, reuses the Phase 6 inspector and safe
wheel extractor, and never resolves a package, opens the network, calls AWS, or creates a trigger.
"""

from __future__ import annotations

import argparse
import ast
import importlib.util
import json
import shutil
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
from mr_lister.release.phase7_canary import (
    APPLICATION_RELEASE_FINGERPRINT_ENV,
    CANARY_BINDING_FILENAME,
    CANARY_BINDING_FINGERPRINT_ENV,
    CANARY_ENTRYPOINT,
    CANARY_PROFILE_FILE_FINGERPRINT,
    CANARY_PROFILE_FINGERPRINT,
    CANARY_PROFILE_ID,
    CANARY_PROFILE_PATH,
    CANARY_PROFILE_VERSION,
    CANARY_RELEASE_FINGERPRINT_ENV,
    DEPLOYMENT_MANIFEST_FILENAME,
    PHASE6_LAMBDA_WHEEL_AUTHORITY_FINGERPRINT,
    RELEASE_MANIFEST_FILENAME,
    SOURCE_MANIFEST_FILENAME,
    inventory,
    render_manifest,
    verify_phase7_canary_release,
    verify_phase7_canary_source_manifest,
)
from mr_lister.review_profile import FilesystemReviewProductAuthority
from tools.build_phase66_source_bundles import (
    LAMBDA_DEPENDENCY_DIRECTORY_NAME,
)
from tools.build_phase66_source_bundles import (
    build_linux_arm64_dependencies_from_wheelhouse as build_phase6_dependencies,
)
from tools.build_phase66_source_bundles import (
    write_linux_arm64_dependency_manifest as write_phase6_dependency_manifest,
)

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SOURCE_DESTINATION = ROOT / ".mr_lister_private" / "phase7-canary-source"
DEFAULT_DEPENDENCY_DESTINATION = ROOT / ".mr_lister_private" / LAMBDA_DEPENDENCY_DIRECTORY_NAME
DEFAULT_DEPLOYMENT_DESTINATION = ROOT / ".mr_lister_private" / "phase7-canary-deployment"
DEFAULT_ARTIFACT_DESTINATION = ROOT / ".mr_lister_private" / "phase7-canary-artifact"
CHECKED_LAMBDA_WHEEL_AUTHORITY = ROOT / "config/release/phase6/phase6-lambda-wheel-authority.json"

CANARY_SOURCE_DIRECTORY_NAME = "phase7-canary-source"
CANARY_DEPENDENCY_DIRECTORY_NAME = LAMBDA_DEPENDENCY_DIRECTORY_NAME
CANARY_DEPLOYMENT_DIRECTORY_NAME = "phase7-canary-deployment"
CANARY_ARTIFACT_DIRECTORY_NAME = "phase7-canary-artifact"
CANARY_ARCHIVE_FILENAME = "phase7-canary.zip"
CANARY_DESCRIPTOR_FILENAME = "deployment-descriptor.json"

_ENTRYPOINT_MODULE = "mr_lister.cloud.phase7_canary_entrypoint"
_RELEASE_MODULE = "mr_lister.release.phase7_canary"
_SOURCE_FORMAT = "phase7-canary-source-v1"
_DEPLOYMENT_FORMAT = "phase7-canary-deployment-v1"
_RELEASE_FORMAT = "phase7-canary-release-v1"
_DESCRIPTOR_FORMAT = "phase7-canary-deployment-descriptor-v1"
_COMPONENT = "phase7-canary-lambda"
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
_FORBIDDEN_MODULE_PREFIXES = (
    "mr_lister.agent",
    "mr_lister.api",
    "mr_lister.cloud.api",
    "mr_lister.cloud.artifacts",
    "mr_lister.cloud.browser_contracts",
    "mr_lister.cloud.http",
    "mr_lister.cloud.phase6",
    "mr_lister.cloud.phase7_composition",
    "mr_lister.cloud.phase7_entrypoints",
    "mr_lister.cloud.phase7_guard_composition",
    "mr_lister.cloud.phase7_guard_entrypoint",
    "mr_lister.cloud.phase7_provider_credentials",
    "mr_lister.cloud.phase7_request_composition",
    "mr_lister.cloud.preview",
    "mr_lister.durable",
    "mr_lister.intelligence",
    "mr_lister.production",
    "mr_lister.publication.query_api",
    "mr_lister.publication.request_api",
    "mr_lister.publication.retention",
    "mr_lister.publication.retention_dynamodb",
    "mr_lister.publication.service",
    "mr_lister.workflow.artifacts",
    "mr_lister.workflow.ports",
    "mr_lister.workflow.service",
    "mr_lister.workflow.store",
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
_GENERIC_ERROR = "Phase 7 canary release build is invalid"


class Phase711CanaryReleaseError(RuntimeError):
    """Value-free failure for unsafe, drifting, or incomplete canary release input."""


@dataclass(frozen=True, slots=True)
class Phase711CanaryArtifact:
    deployment_root: Path
    archive_path: Path
    descriptor_path: Path
    release_fingerprint: str
    application_release_fingerprint: str
    archive_fingerprint: str
    binding_fingerprint: str
    binding_mode: str
    profile_fingerprint: str


def build_canary_source_bundle(
    destination: Path,
    *,
    canary_binding_path: Path,
    repository_root: Path = ROOT,
) -> Path:
    """Copy the exact canary closure, sanitized binding, profile, and locked request."""

    created: Path | None = None
    try:
        repository = repository_root.resolve(strict=True)
        source_root = _new_exact_directory(destination, CANARY_SOURCE_DIRECTORY_NAME)
        created = source_root
        binding_raw, binding = _read_and_validate_binding(canary_binding_path)
        modules = resolve_canary_import_closure(repository)
        for module, source in modules.items():
            relative = source.relative_to(repository / "src")
            raw = b"" if module in _CAPABILITY_FREE_INITIALIZERS else source.read_bytes()
            _write_bytes(source_root / relative, raw)

        profile_source = repository / CANARY_PROFILE_PATH
        profile = FilesystemReviewProductAuthority(
            profile_directory=profile_source.parent
        ).get_exact(
            profile_id=CANARY_PROFILE_ID,
            profile_version=CANARY_PROFILE_VERSION,
        )
        if (
            profile.fingerprint != CANARY_PROFILE_FINGERPRINT
            or profile.profile.publish_enabled is not False
            or _file_fingerprint(profile_source) != CANARY_PROFILE_FILE_FINGERPRINT
        ):
            raise ValueError
        _copy_file(profile_source, source_root / CANARY_PROFILE_PATH)
        _write_bytes(source_root / CANARY_BINDING_FILENAME, binding_raw)
        _write_checked_dependency_request(source_root, repository=repository)
        _assert_source_hygiene(source_root, modules=modules)

        source_manifest = {
            "algorithm": "sha256",
            "binding": {
                "fingerprint": binding["fingerprint"],
                "mode": binding["mode"],
                "path": CANARY_BINDING_FILENAME,
                "release_manifest_fingerprint": binding["release_manifest_fingerprint"],
                "sha256": sha256(binding_raw).hexdigest(),
            },
            "entrypoint": CANARY_ENTRYPOINT,
            "files": inventory(
                source_root,
                excluded=frozenset({SOURCE_MANIFEST_FILENAME}),
            ),
            "format": _SOURCE_FORMAT,
            "profile": {
                "file_sha256": CANARY_PROFILE_FILE_FINGERPRINT,
                "fingerprint": CANARY_PROFILE_FINGERPRINT,
                "path": CANARY_PROFILE_PATH,
                "profile_id": CANARY_PROFILE_ID,
                "profile_version": CANARY_PROFILE_VERSION,
                "publish_enabled": False,
            },
            "target": dict(LINUX_ARM64_TARGET),
        }
        _write_bytes(
            source_root / SOURCE_MANIFEST_FILENAME,
            render_manifest(source_manifest),
        )
        verify_phase7_canary_source_manifest(source_root)
        return source_root
    except Exception as error:
        if created is not None and created.exists():
            shutil.rmtree(created)
        if isinstance(error, Phase711CanaryReleaseError):
            raise
        raise Phase711CanaryReleaseError(_GENERIC_ERROR) from None


def resolve_canary_import_closure(repository_root: Path = ROOT) -> dict[str, Path]:
    """Resolve all local imports reachable from the release-first canary entrypoint."""

    try:
        repository = repository_root.resolve(strict=True)
        source_root = repository / "src"
        queue: deque[str] = deque([_ENTRYPOINT_MODULE, _RELEASE_MODULE])
        resolved: dict[str, Path] = {}
        while queue:
            module = queue.popleft()
            if module in resolved:
                continue
            _reject_module(module)
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
        required = {
            _ENTRYPOINT_MODULE,
            _RELEASE_MODULE,
            "mr_lister.release.phase6",
            "mr_lister.publication.canary_runtime",
            "mr_lister.cloud.phase7_canary_composition",
            "mr_lister.publication.provider_coordinator",
        }
        if not required.issubset(resolved):
            raise ValueError
        return dict(sorted(resolved.items()))
    except Exception:
        raise Phase711CanaryReleaseError(_GENERIC_ERROR) from None


def build_linux_arm64_dependencies_from_wheelhouse(
    wheelhouse_root: Path,
    *,
    destination: Path,
    build_request_path: Path,
) -> Path:
    """Delegate exact wheel verification and extraction to the Phase 6 authority."""

    try:
        if destination.resolve(strict=False).name != CANARY_DEPENDENCY_DIRECTORY_NAME:
            raise ValueError
        return build_phase6_dependencies(
            wheelhouse_root,
            destination=destination,
            build_request_path=build_request_path,
        )
    except Exception:
        raise Phase711CanaryReleaseError(_GENERIC_ERROR) from None


def write_linux_arm64_dependency_manifest(
    artifact_root: Path,
    *,
    build_request_path: Path,
) -> Path:
    """Delegate installed-tree inspection and sealing to the Phase 6 verifier."""

    try:
        if artifact_root.resolve(strict=True).name != CANARY_DEPENDENCY_DIRECTORY_NAME:
            raise ValueError
        return write_phase6_dependency_manifest(
            artifact_root,
            build_request_path=build_request_path,
        )
    except Exception:
        raise Phase711CanaryReleaseError(_GENERIC_ERROR) from None


def seal_canary_release(
    source_root: Path,
    *,
    dependencies: Path,
    deployment_destination: Path,
    artifact_destination: Path,
) -> Phase711CanaryArtifact:
    """Overlay verified bytes and emit one deterministic ZIP and local descriptor."""

    created: list[Path] = []
    try:
        source = source_root.resolve(strict=True)
        if source.name != CANARY_SOURCE_DIRECTORY_NAME:
            raise ValueError
        binding = verify_phase7_canary_source_manifest(source)
        _verify_current_repository_source_authority(source)
        dependency_root = dependencies.resolve(strict=True)
        if dependency_root.name != CANARY_DEPENDENCY_DIRECTORY_NAME:
            raise ValueError
        verify_linux_arm64_dependency_artifact(
            dependency_root,
            build_request_path=source / DEPENDENCY_BUILD_REQUEST_FILENAME,
        )

        deployment = _new_exact_directory(
            deployment_destination,
            CANARY_DEPLOYMENT_DIRECTORY_NAME,
        )
        created.append(deployment)
        _copy_tree(source, deployment)
        _overlay_dependency_tree(dependency_root, deployment)
        deployment_manifest = {
            "algorithm": "sha256",
            "component": _COMPONENT,
            "entrypoint": CANARY_ENTRYPOINT,
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
        binding_bytes = (deployment / CANARY_BINDING_FILENAME).read_bytes()
        release_manifest = {
            "algorithm": "sha256",
            "application_release_fingerprint": binding["release_manifest_fingerprint"],
            "binding_fingerprint": binding["fingerprint"],
            "binding_mode": binding["mode"],
            "binding_sha256": sha256(binding_bytes).hexdigest(),
            "component": _COMPONENT,
            "dependency_manifest_sha256": dependency_manifest_fingerprint,
            "deployment_manifest_sha256": sha256(deployment_bytes).hexdigest(),
            "entrypoint": CANARY_ENTRYPOINT,
            "format": _RELEASE_FORMAT,
            "profile_fingerprint": CANARY_PROFILE_FINGERPRINT,
            "source_manifest_sha256": source_manifest_fingerprint,
            "target": dict(LINUX_ARM64_TARGET),
        }
        release_bytes = render_manifest(release_manifest)
        release_fingerprint = sha256(release_bytes).hexdigest()
        _write_bytes(deployment / RELEASE_MANIFEST_FILENAME, release_bytes)
        verified = verify_phase7_canary_release(
            {
                CANARY_RELEASE_FINGERPRINT_ENV: release_fingerprint,
                APPLICATION_RELEASE_FINGERPRINT_ENV: binding["release_manifest_fingerprint"],
                CANARY_BINDING_FINGERPRINT_ENV: binding["fingerprint"],
            },
            bundle_root=deployment,
        )
        _verify_current_repository_source_authority(deployment)

        artifact = _new_exact_directory(
            artifact_destination,
            CANARY_ARTIFACT_DIRECTORY_NAME,
        )
        created.append(artifact)
        archive_path = artifact / CANARY_ARCHIVE_FILENAME
        archive_bytes = render_deterministic_zip(deployment)
        _write_bytes(archive_path, archive_bytes)
        archive_fingerprint = sha256(archive_bytes).hexdigest()
        descriptor = {
            "algorithm": "sha256",
            "application_release_fingerprint": verified.application_release_fingerprint,
            "archive": {
                "path": CANARY_ARCHIVE_FILENAME,
                "sha256": archive_fingerprint,
                "size_bytes": len(archive_bytes),
            },
            "architecture": "arm64",
            "binding_fingerprint": verified.binding_fingerprint,
            "binding_mode": verified.binding_mode,
            "component": _COMPONENT,
            "deployment_manifest_sha256": verified.deployment_manifest_fingerprint,
            "entrypoint": CANARY_ENTRYPOINT,
            "format": _DESCRIPTOR_FORMAT,
            "profile_fingerprint": verified.profile_fingerprint,
            "release_fingerprint": release_fingerprint,
            "runtime": "python3.12",
            "s3_binding": {
                "archive_sha256_metadata_key": "mr-lister-archive-sha256",
                "bucket_parameter": "CanaryCodeS3Bucket",
                "head_object_version_must_match": True,
                "key_parameter": "CanaryCodeS3Key",
                "key_template": "phase7/releases/{release_fingerprint}/canary.zip",
                "null_object_version_forbidden": True,
                "object_version_parameter": "CanaryCodeS3ObjectVersion",
                "object_version_required": True,
                "release_fingerprint_metadata_key": "mr-lister-release-fingerprint",
                "release_fingerprint_parameter": "CanaryReleaseFingerprint",
                "server_side_encryption": "AES256",
            },
        }
        descriptor_path = artifact / CANARY_DESCRIPTOR_FILENAME
        _write_bytes(descriptor_path, render_manifest(descriptor))
        verify_canary_deployment_artifact(
            deployment,
            archive_path=archive_path,
            descriptor_path=descriptor_path,
        )
        return Phase711CanaryArtifact(
            deployment_root=deployment,
            archive_path=archive_path,
            descriptor_path=descriptor_path,
            release_fingerprint=release_fingerprint,
            application_release_fingerprint=verified.application_release_fingerprint,
            archive_fingerprint=archive_fingerprint,
            binding_fingerprint=verified.binding_fingerprint,
            binding_mode=verified.binding_mode,
            profile_fingerprint=verified.profile_fingerprint,
        )
    except Exception as error:
        for path in reversed(created):
            if path.exists():
                shutil.rmtree(path)
        if isinstance(error, Phase711CanaryReleaseError):
            raise
        raise Phase711CanaryReleaseError(_GENERIC_ERROR) from None


def verify_canary_deployment_artifact(
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
            or descriptor_file.name != CANARY_DESCRIPTOR_FILENAME
            or archive_path.is_symlink()
            or not archive_file.is_file()
            or archive_file.name != CANARY_ARCHIVE_FILENAME
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
                "application_release_fingerprint",
                "archive",
                "architecture",
                "binding_fingerprint",
                "binding_mode",
                "component",
                "deployment_manifest_sha256",
                "entrypoint",
                "format",
                "profile_fingerprint",
                "release_fingerprint",
                "runtime",
                "s3_binding",
            },
        )
        verified = verify_phase7_canary_release(
            {
                CANARY_RELEASE_FINGERPRINT_ENV: descriptor["release_fingerprint"],
                APPLICATION_RELEASE_FINGERPRINT_ENV: descriptor["application_release_fingerprint"],
                CANARY_BINDING_FINGERPRINT_ENV: descriptor["binding_fingerprint"],
            },
            bundle_root=deployment,
        )
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
            or descriptor["deployment_manifest_sha256"] != verified.deployment_manifest_fingerprint
            or descriptor["entrypoint"] != CANARY_ENTRYPOINT
            or descriptor["format"] != _DESCRIPTOR_FORMAT
            or descriptor["profile_fingerprint"] != verified.profile_fingerprint
            or descriptor["binding_mode"] != verified.binding_mode
            or descriptor["runtime"] != "python3.12"
            or archive["path"] != CANARY_ARCHIVE_FILENAME
            or archive["sha256"] != sha256(archive_raw).hexdigest()
            or archive["size_bytes"] != len(archive_raw)
            or archive_raw != expected_archive
            or descriptor["s3_binding"] != _expected_s3_binding()
        ):
            raise ValueError
        _verify_archive_members(deployment, archive_raw)
        return descriptor
    except Exception:
        raise Phase711CanaryReleaseError(_GENERIC_ERROR) from None


def render_deterministic_zip(deployment_root: Path) -> bytes:
    """Render sorted, stored bytes with fixed metadata and no host information."""

    root = deployment_root.resolve(strict=True)
    output = BytesIO()
    with zipfile.ZipFile(
        output,
        "w",
        compression=zipfile.ZIP_STORED,
        strict_timestamps=True,
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
    requirements = render_locked_requirements(authority)
    _write_text(root / "requirements.txt", requirements)
    wheels = cast(Sequence[Mapping[str, str]], authority["wheels"])
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
    _write_bytes(
        root / DEPENDENCY_BUILD_REQUEST_FILENAME,
        render_manifest(request),
    )


def _read_and_validate_binding(path: Path) -> tuple[bytes, Mapping[str, object]]:
    source = path.resolve(strict=True)
    if path.is_symlink() or not source.is_file() or source.name != CANARY_BINDING_FILENAME:
        raise ValueError
    raw = source.read_bytes()
    value = json.loads(raw)
    if not isinstance(value, Mapping) or render_manifest(cast(Mapping[str, object], value)) != raw:
        raise ValueError
    from mr_lister.publication.canary_runtime import PublicationCanaryBinding

    exact = PublicationCanaryBinding.model_validate_json(raw)
    payload = exact.model_dump(mode="json")
    if payload != value or render_manifest(payload) != raw:
        raise ValueError
    return raw, cast(Mapping[str, object], value)


def _verify_current_repository_source_authority(packaged_root: Path) -> None:
    root = packaged_root.resolve(strict=True)
    binding_path = root / CANARY_BINDING_FILENAME
    with TemporaryDirectory(prefix="mr-lister-phase711-current-source-") as temporary:
        current = build_canary_source_bundle(
            Path(temporary) / CANARY_SOURCE_DIRECTORY_NAME,
            canary_binding_path=binding_path,
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
    if _ENTRYPOINT_MODULE not in modules or _RELEASE_MODULE not in modules:
        raise ValueError
    for path in root.rglob("*"):
        relative = path.relative_to(root)
        if path.is_symlink() or any(part in _FORBIDDEN_PATH_PARTS for part in relative.parts):
            raise ValueError
        if path.is_file() and path.suffix.casefold() in _FORBIDDEN_SUFFIXES:
            raise ValueError
    if any(module.startswith("mr_lister.production") for module in modules):
        raise ValueError
    release_source = root / "mr_lister/release/phase7_canary.py"
    if not release_source.is_file():
        raise ValueError
    release_tree = ast.parse(release_source.read_text(encoding="utf-8"))
    imports = {
        alias.name
        for node in ast.walk(release_tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    imported_from = {
        node.module
        for node in ast.walk(release_tree)
        if isinstance(node, ast.ImportFrom) and node.module is not None
    }
    if any(name.startswith("mr_lister.publication") for name in imports | imported_from):
        raise ValueError
    for module in _CAPABILITY_FREE_INITIALIZERS.intersection(modules):
        path = root / Path(*module.split(".")) / "__init__.py"
        if path.read_bytes() != b"":
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
            _reject_module(candidate)
            if _module_path(source_root, candidate) is not None:
                imports.add(candidate)
    return imports


def _reject_module(module: str) -> None:
    if any(
        module == forbidden or module.startswith(f"{forbidden}.")
        for forbidden in _FORBIDDEN_MODULE_PREFIXES
    ) or "browser" in module.split("."):
        raise ValueError


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
        "bucket_parameter": "CanaryCodeS3Bucket",
        "head_object_version_must_match": True,
        "key_parameter": "CanaryCodeS3Key",
        "key_template": "phase7/releases/{release_fingerprint}/canary.zip",
        "null_object_version_forbidden": True,
        "object_version_parameter": "CanaryCodeS3ObjectVersion",
        "object_version_required": True,
        "release_fingerprint_metadata_key": "mr-lister-release-fingerprint",
        "release_fingerprint_parameter": "CanaryReleaseFingerprint",
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


def _require_exact_keys(value: Mapping[object, object], keys: set[str]) -> None:
    if set(value) != keys:
        raise ValueError


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-destination", type=Path, default=DEFAULT_SOURCE_DESTINATION)
    parser.add_argument("--canary-binding", type=Path)
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
        verify_phase7_canary_source_manifest(arguments.verify_source)
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
        artifact = seal_canary_release(
            arguments.seal_source_release,
            dependencies=arguments.dependencies,
            deployment_destination=arguments.deployment_destination,
            artifact_destination=arguments.artifact_destination,
        )
        print(artifact.release_fingerprint)
    elif arguments.verify_deployment is not None:
        if arguments.archive is None or arguments.descriptor is None:
            parser.error("--archive and --descriptor are required")
        verify_canary_deployment_artifact(
            arguments.verify_deployment,
            archive_path=arguments.archive,
            descriptor_path=arguments.descriptor,
        )
    else:
        if arguments.canary_binding is None:
            parser.error("--canary-binding is required")
        source = build_canary_source_bundle(
            arguments.source_destination,
            canary_binding_path=arguments.canary_binding,
        )
        print(source.name)


if __name__ == "__main__":
    main()


__all__ = [
    "CANARY_ARCHIVE_FILENAME",
    "CANARY_ARTIFACT_DIRECTORY_NAME",
    "CANARY_DEPENDENCY_DIRECTORY_NAME",
    "CANARY_DEPLOYMENT_DIRECTORY_NAME",
    "CANARY_DESCRIPTOR_FILENAME",
    "CANARY_SOURCE_DIRECTORY_NAME",
    "Phase711CanaryArtifact",
    "Phase711CanaryReleaseError",
    "build_canary_source_bundle",
    "build_linux_arm64_dependencies_from_wheelhouse",
    "render_deterministic_zip",
    "resolve_canary_import_closure",
    "seal_canary_release",
    "verify_canary_deployment_artifact",
    "write_linux_arm64_dependency_manifest",
]
