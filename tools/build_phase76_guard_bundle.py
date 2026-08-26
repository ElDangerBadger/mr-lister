"""Build and seal the narrow Phase 7.6 read-only publication-guard Lambda artifact.

The default action creates only a deterministic source bundle and a Linux ARM64 dependency build
request.  Dependency installation remains an explicit controlled-environment step.  Later actions
inspect those target bytes, seal one extracted deployment tree, and emit a deterministic ZIP plus
an S3-version-bound deployment descriptor.  This tool imports no AWS SDK and makes no AWS call.
"""

from __future__ import annotations

import argparse
import ast
import importlib.util
import json
import shutil
import zipfile
from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass
from hashlib import sha256
from io import BytesIO
from pathlib import Path, PurePosixPath
from tempfile import TemporaryDirectory
from typing import cast

from mr_lister.release.phase7 import (
    CAPABILITY_FREE_CLOUD_INIT_BYTES,
    CAPABILITY_FREE_PACKAGE_INIT_PATHS,
    DEPENDENCY_ARTIFACT_FILENAME,
    DEPENDENCY_BUILD_REQUEST_FILENAME,
    DEPLOYMENT_MANIFEST_FILENAME,
    GUARD_ENTRYPOINT,
    GUARD_RELEASE_FINGERPRINT_ENV,
    LINUX_ARM64_TARGET,
    PINNED_GUARD_DISTRIBUTIONS,
    PINNED_GUARD_REQUIREMENTS,
    PINNED_GUARD_WHEELS,
    RELEASE_MANIFEST_FILENAME,
    SHARED_RELEASE_FINGERPRINT_ENV,
    SOURCE_MANIFEST_FILENAME,
    Phase7GuardReleaseAuthorityError,
    inspect_linux_arm64_dependency_artifact,
    inventory,
    render_manifest,
    verify_linux_arm64_dependency_artifact,
    verify_phase7_guard_release,
    verify_source_manifest,
)
from mr_lister.review_profile import FilesystemReviewProductAuthority

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SOURCE_DESTINATION = ROOT / ".mr_lister_private" / "phase7-guard-source"
DEFAULT_DEPENDENCY_DESTINATION = ROOT / ".mr_lister_private" / "linux-arm64-dependencies"
DEFAULT_DEPLOYMENT_DESTINATION = ROOT / ".mr_lister_private" / "phase7-guard-deployment"
DEFAULT_ARTIFACT_DESTINATION = ROOT / ".mr_lister_private" / "phase7-guard-artifact"

GUARD_SOURCE_DIRECTORY_NAME = "phase7-guard-source"
GUARD_DEPLOYMENT_DIRECTORY_NAME = "phase7-guard-deployment"
GUARD_ARTIFACT_DIRECTORY_NAME = "phase7-guard-artifact"
GUARD_DEPENDENCY_DIRECTORY_NAME = "linux-arm64-dependencies"
GUARD_ARCHIVE_FILENAME = "phase7-guard.zip"
GUARD_DESCRIPTOR_FILENAME = "deployment-descriptor.json"
GUARD_PROFILE_RELATIVE_PATH = Path("config/product_profiles/gildan_64000_swiftpod.json")
GUARD_PROFILE_ID = "gildan_64000_swiftpod"
GUARD_PROFILE_VERSION = 2

_ENTRYPOINT_MODULE = "mr_lister.cloud.phase7_guard_entrypoint"
_SOURCE_FORMAT = "phase7-guard-source-v1"
_BUILD_REQUEST_FORMAT = "phase7-guard-dependency-build-request-v1"
_DEPLOYMENT_FORMAT = "phase7-guard-deployment-v1"
_RELEASE_FORMAT = "phase7-guard-release-v1"
_DESCRIPTOR_FORMAT = "phase7-guard-deployment-descriptor-v1"
_COMPONENT = "phase7-guard-lambda"

GUARD_REQUIREMENTS = PINNED_GUARD_REQUIREMENTS
_REQUIRED_DISTRIBUTIONS = sorted(name for name, _version in PINNED_GUARD_DISTRIBUTIONS)
_CAPABILITY_FREE_PACKAGE_INITIALIZERS = frozenset(
    {"mr_lister", "mr_lister.cloud", "mr_lister.release"}
)

_FORBIDDEN_MODULE_PREFIXES = (
    "mr_lister.api",
    "mr_lister.production",
    "mr_lister.workflow",
    "mr_lister.publication.execution_dynamodb",
    "mr_lister.publication.execution_service",
    "mr_lister.publication.execution_store",
    "mr_lister.publication.provider_boundary",
    "mr_lister.publication.provider_coordinator",
    "mr_lister.publication.provider_credentials",
    "mr_lister.publication.service",
    "mr_lister.cloud.phase7_provider_credentials",
)
_FORBIDDEN_PATH_PARTS = frozenset(
    {
        ".DS_Store",
        ".env",
        ".git",
        ".mr_lister_private",
        "__pycache__",
        "browser",
        "tests",
    }
)
_FORBIDDEN_SUFFIXES = frozenset({".dylib", ".dll", ".key", ".pem", ".pyc", ".pyd", ".whl"})
_ZIP_TIMESTAMP = (1980, 1, 1, 0, 0, 0)


class Phase76GuardBundleError(RuntimeError):
    """Value-free failure for unsafe, malformed, or drifting build input."""


@dataclass(frozen=True, slots=True)
class Phase76GuardArtifact:
    deployment_root: Path
    archive_path: Path
    descriptor_path: Path
    release_fingerprint: str
    archive_fingerprint: str
    profile_fingerprint: str


def build_guard_source_bundle(
    destination: Path,
    *,
    repository_root: Path = ROOT,
) -> Path:
    """Copy the static local import closure rooted at the guard Lambda entrypoint."""

    try:
        repository = repository_root.resolve(strict=True)
        source_root = _new_exact_directory(destination, GUARD_SOURCE_DIRECTORY_NAME)
        modules = resolve_guard_import_closure(repository)
        for module, source in modules.items():
            relative = source.relative_to(repository / "src")
            if module in _CAPABILITY_FREE_PACKAGE_INITIALIZERS:
                _write_bytes(source_root / relative, CAPABILITY_FREE_CLOUD_INIT_BYTES)
            else:
                _copy_file(source, source_root / relative)

        profile_source = repository / GUARD_PROFILE_RELATIVE_PATH
        profile_authority = FilesystemReviewProductAuthority(
            profile_directory=profile_source.parent
        ).get_exact(
            profile_id=GUARD_PROFILE_ID,
            profile_version=GUARD_PROFILE_VERSION,
        )
        if profile_authority.profile.publish_enabled is not False:
            raise ValueError
        _copy_file(profile_source, source_root / GUARD_PROFILE_RELATIVE_PATH)
        _write_text(source_root / "requirements.txt", GUARD_REQUIREMENTS)
        _write_dependency_build_request(source_root)
        _assert_source_hygiene(source_root, modules=modules)
        source_manifest = {
            "algorithm": "sha256",
            "entrypoint": GUARD_ENTRYPOINT,
            "files": inventory(source_root, excluded=frozenset({SOURCE_MANIFEST_FILENAME})),
            "format": _SOURCE_FORMAT,
            "profile": {
                "fingerprint": profile_authority.fingerprint,
                "path": GUARD_PROFILE_RELATIVE_PATH.as_posix(),
                "profile_id": GUARD_PROFILE_ID,
                "profile_version": GUARD_PROFILE_VERSION,
                "publish_enabled": False,
            },
        }
        _write_bytes(source_root / SOURCE_MANIFEST_FILENAME, render_manifest(source_manifest))
        verify_source_manifest(source_root)
        return source_root
    except (Phase76GuardBundleError, Phase7GuardReleaseAuthorityError):
        raise
    except Exception:
        raise Phase76GuardBundleError("Phase 7 guard source bundle is invalid") from None


def resolve_guard_import_closure(repository_root: Path = ROOT) -> dict[str, Path]:
    """Resolve only local ``mr_lister`` imports, including package initializers."""

    repository = repository_root.resolve(strict=True)
    source_root = repository / "src"
    queue: deque[str] = deque([_ENTRYPOINT_MODULE, "mr_lister.release.phase7"])
    resolved: dict[str, Path] = {}
    while queue:
        module = queue.popleft()
        if module in resolved:
            continue
        _reject_forbidden_module(module)
        source = _module_path(source_root, module)
        if source is None:
            raise Phase76GuardBundleError("Phase 7 guard import closure is incomplete")
        resolved[module] = source
        for parent in _parent_packages(module):
            if parent not in resolved:
                queue.append(parent)
        if module in _CAPABILITY_FREE_PACKAGE_INITIALIZERS:
            continue
        for imported in _local_imports(source_root, module, source):
            if imported not in resolved:
                queue.append(imported)
    return dict(sorted(resolved.items()))


def write_linux_arm64_dependency_manifest(
    artifact_root: Path,
    *,
    build_request_path: Path,
) -> Path:
    """Inspect and seal dependency bytes produced by a controlled Linux ARM64 build."""

    artifact = artifact_root.resolve(strict=True)
    manifest_path = artifact / DEPENDENCY_ARTIFACT_FILENAME
    if manifest_path.exists():
        raise Phase76GuardBundleError("Phase 7 guard dependency artifact already exists")
    manifest = inspect_linux_arm64_dependency_artifact(
        artifact,
        build_request_path=build_request_path,
    )
    _write_bytes(manifest_path, render_manifest(manifest))
    verify_linux_arm64_dependency_artifact(
        artifact,
        build_request_path=build_request_path,
    )
    return manifest_path


def build_linux_arm64_dependencies_from_wheelhouse(
    wheelhouse_root: Path,
    *,
    destination: Path,
    build_request_path: Path,
) -> Path:
    """Extract only the exact hash-authorized wheels and seal their trusted byte tree."""

    try:
        wheelhouse = wheelhouse_root.resolve(strict=True)
        if wheelhouse_root.is_symlink() or not wheelhouse.is_dir():
            raise ValueError
        expected = {
            filename: fingerprint for _name, _version, filename, fingerprint in PINNED_GUARD_WHEELS
        }
        candidates = sorted(wheelhouse.iterdir(), key=lambda path: path.name)
        if [path.name for path in candidates] != sorted(expected):
            raise ValueError
        for wheel in candidates:
            if (
                wheel.is_symlink()
                or not wheel.is_file()
                or _file_fingerprint(wheel) != expected[wheel.name]
            ):
                raise ValueError

        dependency_root = _new_exact_directory(
            destination,
            GUARD_DEPENDENCY_DIRECTORY_NAME,
        )
        extracted: set[str] = set()
        total_size = 0
        for wheel in candidates:
            with zipfile.ZipFile(wheel) as archive:
                members = archive.infolist()
                if not members or len({member.filename for member in members}) != len(members):
                    raise ValueError
                for member in members:
                    relative = _safe_wheel_member(member)
                    if member.is_dir():
                        continue
                    total_size += member.file_size
                    if total_size > 128 * 1024 * 1024 or relative in extracted:
                        raise ValueError
                    extracted.add(relative)
                    _write_bytes(dependency_root / relative, archive.read(member))
        if not extracted:
            raise ValueError
        return write_linux_arm64_dependency_manifest(
            dependency_root,
            build_request_path=build_request_path,
        )
    except (Phase76GuardBundleError, Phase7GuardReleaseAuthorityError):
        raise
    except Exception:
        raise Phase76GuardBundleError("Phase 7 guard wheel authority is invalid") from None


def seal_guard_release(
    source_root: Path,
    *,
    dependencies: Path,
    deployment_destination: Path,
    artifact_destination: Path,
) -> Phase76GuardArtifact:
    """Overlay verified dependencies and emit a reproducible Lambda ZIP and descriptor."""

    try:
        source = source_root.resolve(strict=True)
        if source.name != GUARD_SOURCE_DIRECTORY_NAME:
            raise ValueError
        source_manifest = verify_source_manifest(source)
        _verify_current_repository_source_authority(source)
        dependency_root = dependencies.resolve(strict=True)
        verify_linux_arm64_dependency_artifact(
            dependency_root,
            build_request_path=source / DEPENDENCY_BUILD_REQUEST_FILENAME,
        )
        deployment = _new_exact_directory(
            deployment_destination,
            GUARD_DEPLOYMENT_DIRECTORY_NAME,
        )
        _copy_tree(source, deployment)
        _overlay_dependency_tree(dependency_root, deployment)

        deployment_manifest = {
            "algorithm": "sha256",
            "component": _COMPONENT,
            "entrypoint": GUARD_ENTRYPOINT,
            "files": inventory(
                deployment,
                excluded=frozenset({DEPLOYMENT_MANIFEST_FILENAME, RELEASE_MANIFEST_FILENAME}),
            ),
            "format": _DEPLOYMENT_FORMAT,
            "target": dict(LINUX_ARM64_TARGET),
        }
        deployment_bytes = render_manifest(deployment_manifest)
        _write_bytes(deployment / DEPLOYMENT_MANIFEST_FILENAME, deployment_bytes)
        profile = cast(Mapping[str, object], source_manifest["profile"])
        release_manifest = {
            "algorithm": "sha256",
            "component": _COMPONENT,
            "dependency_manifest_sha256": _file_fingerprint(
                deployment / DEPENDENCY_ARTIFACT_FILENAME
            ),
            "deployment_manifest_sha256": sha256(deployment_bytes).hexdigest(),
            "entrypoint": GUARD_ENTRYPOINT,
            "format": _RELEASE_FORMAT,
            "profile_fingerprint": profile["fingerprint"],
            "source_manifest_sha256": _file_fingerprint(deployment / SOURCE_MANIFEST_FILENAME),
            "target": dict(LINUX_ARM64_TARGET),
        }
        release_bytes = render_manifest(release_manifest)
        release_fingerprint = sha256(release_bytes).hexdigest()
        _write_bytes(deployment / RELEASE_MANIFEST_FILENAME, release_bytes)
        binding = verify_phase7_guard_release(
            {
                GUARD_RELEASE_FINGERPRINT_ENV: release_fingerprint,
                SHARED_RELEASE_FINGERPRINT_ENV: release_fingerprint,
            },
            bundle_root=deployment,
        )
        _verify_current_repository_source_authority(deployment)

        artifact = _new_exact_directory(
            artifact_destination,
            GUARD_ARTIFACT_DIRECTORY_NAME,
        )
        archive_path = artifact / GUARD_ARCHIVE_FILENAME
        archive_bytes = render_deterministic_zip(deployment)
        _write_bytes(archive_path, archive_bytes)
        archive_fingerprint = sha256(archive_bytes).hexdigest()
        descriptor = {
            "algorithm": "sha256",
            "archive": {
                "path": GUARD_ARCHIVE_FILENAME,
                "sha256": archive_fingerprint,
                "size_bytes": len(archive_bytes),
            },
            "architecture": "arm64",
            "component": _COMPONENT,
            "deployment_manifest_sha256": binding.deployment_manifest_fingerprint,
            "entrypoint": GUARD_ENTRYPOINT,
            "format": _DESCRIPTOR_FORMAT,
            "profile_fingerprint": binding.profile_fingerprint,
            "release_fingerprint": release_fingerprint,
            "runtime": "python3.12",
            "s3_binding": {
                "archive_sha256_metadata_key": "mr-lister-archive-sha256",
                "bucket_parameter": "GuardCodeS3Bucket",
                "head_object_version_must_match": True,
                "key_parameter": "GuardCodeS3Key",
                "key_template": "phase7/releases/{release_fingerprint}/guard.zip",
                "null_object_version_forbidden": True,
                "object_version_parameter": "GuardCodeS3ObjectVersion",
                "object_version_required": True,
                "release_fingerprint_metadata_key": "mr-lister-release-fingerprint",
                "release_fingerprint_parameter": "GuardReleaseFingerprint",
                "server_side_encryption": "AES256",
            },
        }
        descriptor_path = artifact / GUARD_DESCRIPTOR_FILENAME
        _write_bytes(descriptor_path, render_manifest(descriptor))
        verify_guard_deployment_artifact(
            deployment,
            archive_path=archive_path,
            descriptor_path=descriptor_path,
        )
        return Phase76GuardArtifact(
            deployment_root=deployment,
            archive_path=archive_path,
            descriptor_path=descriptor_path,
            release_fingerprint=release_fingerprint,
            archive_fingerprint=archive_fingerprint,
            profile_fingerprint=binding.profile_fingerprint,
        )
    except (Phase76GuardBundleError, Phase7GuardReleaseAuthorityError):
        raise
    except Exception:
        raise Phase76GuardBundleError("Phase 7 guard deployment artifact is invalid") from None


def render_deterministic_zip(deployment_root: Path) -> bytes:
    """Render a sorted, uncompressed ZIP with fixed metadata and exact file bytes."""

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


def verify_guard_deployment_artifact(
    deployment_root: Path,
    *,
    archive_path: Path,
    descriptor_path: Path,
) -> Mapping[str, object]:
    """Verify extracted release bytes, deterministic ZIP bytes, and the S3 descriptor."""

    try:
        deployment = deployment_root.resolve(strict=True)
        descriptor_raw = descriptor_path.read_bytes()
        descriptor = json.loads(descriptor_raw)
        if not isinstance(descriptor, Mapping) or render_manifest(descriptor) != descriptor_raw:
            raise ValueError
        if set(descriptor) != {
            "algorithm",
            "archive",
            "architecture",
            "component",
            "deployment_manifest_sha256",
            "entrypoint",
            "format",
            "profile_fingerprint",
            "release_fingerprint",
            "runtime",
            "s3_binding",
        }:
            raise ValueError
        release = descriptor["release_fingerprint"]
        if not isinstance(release, str):
            raise ValueError
        binding = verify_phase7_guard_release(
            {
                GUARD_RELEASE_FINGERPRINT_ENV: release,
                SHARED_RELEASE_FINGERPRINT_ENV: release,
            },
            bundle_root=deployment,
        )
        _verify_current_repository_source_authority(deployment)
        archive = descriptor["archive"]
        if not isinstance(archive, Mapping) or set(archive) != {"path", "sha256", "size_bytes"}:
            raise ValueError
        raw_archive = archive_path.read_bytes()
        expected_archive = render_deterministic_zip(deployment)
        if (
            descriptor["algorithm"] != "sha256"
            or descriptor["architecture"] != "arm64"
            or descriptor["component"] != _COMPONENT
            or descriptor["deployment_manifest_sha256"] != binding.deployment_manifest_fingerprint
            or descriptor["entrypoint"] != GUARD_ENTRYPOINT
            or descriptor["format"] != _DESCRIPTOR_FORMAT
            or descriptor["profile_fingerprint"] != binding.profile_fingerprint
            or descriptor["runtime"] != "python3.12"
            or archive["path"] != archive_path.name
            or archive["sha256"] != sha256(raw_archive).hexdigest()
            or archive["size_bytes"] != len(raw_archive)
            or raw_archive != expected_archive
        ):
            raise ValueError
        if descriptor["s3_binding"] != {
            "archive_sha256_metadata_key": "mr-lister-archive-sha256",
            "bucket_parameter": "GuardCodeS3Bucket",
            "head_object_version_must_match": True,
            "key_parameter": "GuardCodeS3Key",
            "key_template": "phase7/releases/{release_fingerprint}/guard.zip",
            "null_object_version_forbidden": True,
            "object_version_parameter": "GuardCodeS3ObjectVersion",
            "object_version_required": True,
            "release_fingerprint_metadata_key": "mr-lister-release-fingerprint",
            "release_fingerprint_parameter": "GuardReleaseFingerprint",
            "server_side_encryption": "AES256",
        }:
            raise ValueError
        _verify_archive_members(deployment, raw_archive)
        return descriptor
    except (Phase76GuardBundleError, Phase7GuardReleaseAuthorityError):
        raise
    except Exception:
        raise Phase76GuardBundleError("Phase 7 guard deployment artifact is invalid") from None


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


def _safe_wheel_member(member: zipfile.ZipInfo) -> str:
    value = member.filename
    relative = value[:-1] if member.is_dir() and value.endswith("/") else value
    path = PurePosixPath(relative)
    file_type = (member.external_attr >> 16) & 0o170000
    if (
        not relative
        or not relative.isascii()
        or "\\" in relative
        or path.is_absolute()
        or path.as_posix() != relative
        or any(part in {"", ".", "..", ".git", "__pycache__"} for part in path.parts)
        or member.flag_bits & 0x1
        or member.file_size < 0
        or member.file_size > 64 * 1024 * 1024
        or file_type == 0o120000
        or (not member.is_dir() and value.endswith("/"))
    ):
        raise ValueError
    return relative


def _module_path(source_root: Path, module: str) -> Path | None:
    relative = Path(*module.split("."))
    candidates = (source_root / f"{relative}.py", source_root / relative / "__init__.py")
    existing = [path for path in candidates if path.is_file() and not path.is_symlink()]
    if len(existing) > 1:
        raise Phase76GuardBundleError("Phase 7 guard import closure is ambiguous")
    return existing[0] if existing else None


def _parent_packages(module: str) -> tuple[str, ...]:
    parts = module.split(".")
    return tuple(".".join(parts[:index]) for index in range(1, len(parts)))


def _local_imports(source_root: Path, module: str, source: Path) -> set[str]:
    try:
        tree = ast.parse(source.read_text(encoding="utf-8"), filename=source.as_posix())
    except (OSError, SyntaxError):
        raise Phase76GuardBundleError("Phase 7 guard source could not be parsed") from None
    package = module if source.name == "__init__.py" else module.rpartition(".")[0]
    imports: set[str] = set()
    for node in ast.walk(tree):
        candidates: list[str] = []
        if isinstance(node, ast.Import):
            candidates.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                relative = "." * node.level + (node.module or "")
                try:
                    base = importlib.util.resolve_name(relative, package)
                except (ImportError, ValueError):
                    raise Phase76GuardBundleError(
                        "Phase 7 guard relative import is invalid"
                    ) from None
            else:
                base = node.module or ""
            candidates.append(base)
            candidates.extend(f"{base}.{alias.name}" for alias in node.names if alias.name != "*")
        for candidate in candidates:
            if not candidate.startswith("mr_lister"):
                continue
            _reject_forbidden_module(candidate)
            if _module_path(source_root, candidate) is not None:
                imports.add(candidate)
    return imports


def _reject_forbidden_module(module: str) -> None:
    if any(
        module == forbidden or module.startswith(f"{forbidden}.")
        for forbidden in _FORBIDDEN_MODULE_PREFIXES
    ) or "browser" in module.split("."):
        raise Phase76GuardBundleError("Phase 7 guard import closure contains a forbidden module")


def _assert_source_hygiene(root: Path, *, modules: Mapping[str, Path]) -> None:
    if _ENTRYPOINT_MODULE not in modules or "mr_lister.release.phase7" not in modules:
        raise ValueError
    forbidden_relative = tuple(
        Path(*module.split(".")).with_suffix(".py") for module in _FORBIDDEN_MODULE_PREFIXES
    )
    for path in root.rglob("*"):
        relative = path.relative_to(root)
        if path.is_symlink() or any(part in _FORBIDDEN_PATH_PARTS for part in relative.parts):
            raise ValueError
        if path.is_file() and path.suffix.casefold() in _FORBIDDEN_SUFFIXES:
            raise ValueError
        if path.is_file() and any(relative == candidate for candidate in forbidden_relative):
            raise ValueError
    entrypoint_path = root / "mr_lister/cloud/phase7_guard_entrypoint.py"
    if not entrypoint_path.is_file() or GUARD_ENTRYPOINT.rpartition(".")[2] not in (
        entrypoint_path.read_text(encoding="utf-8")
    ):
        raise ValueError
    for relative in CAPABILITY_FREE_PACKAGE_INIT_PATHS:
        initializer = root / relative
        if (
            initializer.is_symlink()
            or not initializer.is_file()
            or initializer.read_bytes() != CAPABILITY_FREE_CLOUD_INIT_BYTES
        ):
            raise ValueError


def _verify_current_repository_source_authority(packaged_root: Path) -> None:
    """Bind a source stage or deployment to a fresh closure of this reviewed checkout."""

    root = packaged_root.resolve(strict=True)
    with TemporaryDirectory(prefix="mr-lister-phase76-current-source-") as temporary:
        current = build_guard_source_bundle(Path(temporary) / GUARD_SOURCE_DIRECTORY_NAME)
        expected = inventory(current, excluded=frozenset())
        actual: list[dict[str, object]] = []
        for record in expected:
            relative = cast(str, record["path"])
            target = root / relative
            if target.is_symlink() or not target.is_file():
                raise Phase76GuardBundleError(
                    "Phase 7 guard source does not match the current repository authority"
                )
            actual.append(
                {
                    "path": relative,
                    "sha256": _file_fingerprint(target),
                    "size_bytes": target.stat().st_size,
                }
            )
        if actual != expected:
            raise Phase76GuardBundleError(
                "Phase 7 guard source does not match the current repository authority"
            )


def _write_dependency_build_request(root: Path) -> None:
    requirements = root / "requirements.txt"
    request = {
        "algorithm": "sha256",
        "component": _COMPONENT,
        "format": _BUILD_REQUEST_FORMAT,
        "requirements": {
            "path": "requirements.txt",
            "required_distributions": _REQUIRED_DISTRIBUTIONS,
            "sha256": _file_fingerprint(requirements),
        },
        "target": dict(LINUX_ARM64_TARGET),
    }
    _write_bytes(root / DEPENDENCY_BUILD_REQUEST_FILENAME, render_manifest(request))


def _new_exact_directory(path: Path, expected_name: str) -> Path:
    destination = path.resolve(strict=False)
    if destination.name != expected_name or destination.exists():
        raise Phase76GuardBundleError("Phase 7 guard destination must be new and exactly named")
    destination.mkdir(mode=0o700, parents=True)
    return destination


def _copy_file(source: Path, destination: Path) -> None:
    if source.is_symlink() or not source.is_file():
        raise ValueError
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    shutil.copyfile(source, destination, follow_symlinks=False)
    destination.chmod(0o644)


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
        verify_source_manifest(arguments.verify_source)
        print(arguments.verify_source.resolve(strict=True))
        return
    if arguments.build_dependencies_from_wheelhouse is not None:
        if arguments.build_request is None:
            parser.error("--build-dependencies-from-wheelhouse requires --build-request")
        print(
            build_linux_arm64_dependencies_from_wheelhouse(
                arguments.build_dependencies_from_wheelhouse,
                destination=arguments.dependency_destination,
                build_request_path=arguments.build_request,
            )
        )
        return
    if arguments.write_dependency_manifest is not None:
        if arguments.build_request is None:
            parser.error("--write-dependency-manifest requires --build-request")
        print(
            write_linux_arm64_dependency_manifest(
                arguments.write_dependency_manifest,
                build_request_path=arguments.build_request,
            )
        )
        return
    if arguments.verify_dependency_artifact is not None:
        if arguments.build_request is None:
            parser.error("--verify-dependency-artifact requires --build-request")
        verify_linux_arm64_dependency_artifact(
            arguments.verify_dependency_artifact,
            build_request_path=arguments.build_request,
        )
        print(arguments.verify_dependency_artifact.resolve(strict=True))
        return
    if arguments.seal_source_release is not None:
        if arguments.dependencies is None:
            parser.error("--seal-source-release requires --dependencies")
        result = seal_guard_release(
            arguments.seal_source_release,
            dependencies=arguments.dependencies,
            deployment_destination=arguments.deployment_destination,
            artifact_destination=arguments.artifact_destination,
        )
        print(result.deployment_root)
        print(result.archive_path)
        print(result.descriptor_path)
        print(result.release_fingerprint)
        return
    if arguments.verify_deployment is not None:
        if arguments.archive is None or arguments.descriptor is None:
            parser.error("--verify-deployment requires --archive and --descriptor")
        verify_guard_deployment_artifact(
            arguments.verify_deployment,
            archive_path=arguments.archive,
            descriptor_path=arguments.descriptor,
        )
        print(arguments.verify_deployment.resolve(strict=True))
        return
    if any(
        value is not None
        for value in (
            arguments.build_request,
            arguments.dependencies,
            arguments.archive,
            arguments.descriptor,
        )
    ):
        parser.error("build inputs require an explicit inspect, seal, or verify action")
    print(build_guard_source_bundle(arguments.source_destination))


if __name__ == "__main__":
    main()


__all__ = [
    "DEFAULT_ARTIFACT_DESTINATION",
    "DEFAULT_DEPENDENCY_DESTINATION",
    "DEFAULT_DEPLOYMENT_DESTINATION",
    "DEFAULT_SOURCE_DESTINATION",
    "GUARD_ARCHIVE_FILENAME",
    "GUARD_DESCRIPTOR_FILENAME",
    "GUARD_REQUIREMENTS",
    "Phase76GuardArtifact",
    "Phase76GuardBundleError",
    "build_linux_arm64_dependencies_from_wheelhouse",
    "build_guard_source_bundle",
    "render_deterministic_zip",
    "resolve_guard_import_closure",
    "seal_guard_release",
    "verify_guard_deployment_artifact",
    "write_linux_arm64_dependency_manifest",
]
