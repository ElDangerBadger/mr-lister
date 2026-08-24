"""Restore one preserved sealed Phase 6 release without rebuilding or resealing it.

The preserved directory is an immutable input containing the two canonical deployment ZIPs and
their descriptor.  Restoration validates that input, safely extracts it into private staging,
and publishes only to new ``phase6-deployment`` and ``phase6-artifacts`` directories.  The
existing release verifier remains the final authority and binds the restored bytes to this
checkout's current source.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import zipfile
from collections.abc import Mapping
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path, PurePosixPath
from tempfile import TemporaryDirectory
from typing import BinaryIO, cast

from mr_lister.release.phase6 import LINUX_ARM64_TARGET, render_manifest
from tools.build_phase66_source_bundles import (
    AGENTCORE_ARCHIVE_FILENAME,
    ARTIFACT_DIRECTORY_NAME,
    DEFAULT_ARTIFACT_DESTINATION,
    DEFAULT_DEPLOYMENT_DESTINATION,
    DEPLOYMENT_DESCRIPTOR_FILENAME,
    DEPLOYMENT_DIRECTORY_NAME,
    LAMBDA_ARCHIVE_FILENAME,
    verify_phase6_deployment_artifacts,
)

_DESCRIPTOR_FORMAT = "phase6-deployment-artifacts-v1"
_FINGERPRINT = re.compile(r"^[0-9a-f]{64}$")
_PRESERVED_DIRECTORY = re.compile(r"^phase6-artifacts-([0-9a-f]{8})$")
_ZIP_TIMESTAMP = (1980, 1, 1, 0, 0, 0)
_MAX_DESCRIPTOR_SIZE = 64 * 1024
_MAX_ARCHIVE_SIZE = 768 * 1024 * 1024
_MAX_MEMBER_COUNT = 50_000
_MAX_MEMBER_SIZE = 128 * 1024 * 1024
_MAX_MEMBER_PATH_LENGTH = 1024
_MAX_MEMBER_PATH_DEPTH = 32
_MAX_EXTRACTED_TREE_SIZE = 768 * 1024 * 1024
_COPY_BUFFER_SIZE = 1024 * 1024
_ERROR = "Preserved Phase 6 deployment artifacts are invalid"

_ARTIFACT_FILENAMES = (
    AGENTCORE_ARCHIVE_FILENAME,
    DEPLOYMENT_DESCRIPTOR_FILENAME,
    LAMBDA_ARCHIVE_FILENAME,
)


class Phase6ArtifactRestorationError(RuntimeError):
    """A value-free restoration failure for malformed, stale, or colliding input."""


@dataclass(frozen=True, slots=True)
class RestoredPhase6Deployment:
    """Canonical paths and immutable release identity produced by a restoration."""

    deployment_root: Path
    artifact_root: Path
    release_fingerprint: str


def restore_phase6_deployment_artifacts(
    preserved_artifact_root: Path,
    *,
    deployment_destination: Path = DEFAULT_DEPLOYMENT_DESTINATION,
    artifact_destination: Path = DEFAULT_ARTIFACT_DESTINATION,
) -> RestoredPhase6Deployment:
    """Restore exact sealed trees and artifacts into new canonical directories.

    This function never invokes a build or seal operation.  Both destinations must be absent; an
    empty directory is still an overwrite conflict.  Any failure after destination reservation
    removes only the directories created by this invocation.
    """

    published = False
    try:
        preserved = _preserved_root(preserved_artifact_root)
        deployment = _new_destination(deployment_destination, DEPLOYMENT_DIRECTORY_NAME)
        artifacts = _new_destination(artifact_destination, ARTIFACT_DIRECTORY_NAME)
        if deployment.parent != artifacts.parent:
            raise ValueError

        descriptor = _validated_descriptor(preserved)
        release_fingerprint = cast(str, descriptor["release_fingerprint"])
        _validate_preserved_archives(preserved, descriptor)

        with TemporaryDirectory(prefix=".phase6-restore-", dir=deployment.parent) as temporary:
            staging_root = Path(temporary)
            staged_deployment = staging_root / DEPLOYMENT_DIRECTORY_NAME
            staged_artifacts = staging_root / ARTIFACT_DIRECTORY_NAME
            staged_deployment.mkdir(mode=0o700)
            staged_artifacts.mkdir(mode=0o700)

            for component, filename in (
                ("agentcore", AGENTCORE_ARCHIVE_FILENAME),
                ("lambda", LAMBDA_ARCHIVE_FILENAME),
            ):
                component_root = staged_deployment / component
                component_root.mkdir(mode=0o700)
                _extract_archive(preserved / filename, component_root)
            for filename in _ARTIFACT_FILENAMES:
                _copy_file_exclusive(preserved / filename, staged_artifacts / filename)

            verify_phase6_deployment_artifacts(
                staged_deployment,
                artifact_root=staged_artifacts,
                verify_current_source=True,
            )
            _publish_staged_restore(
                staged_deployment,
                staged_artifacts,
                deployment=deployment,
                artifacts=artifacts,
            )
            published = True

        verified = verify_phase6_deployment_artifacts(
            deployment,
            artifact_root=artifacts,
            verify_current_source=True,
        )
        if verified != descriptor:
            raise ValueError
        return RestoredPhase6Deployment(
            deployment_root=deployment,
            artifact_root=artifacts,
            release_fingerprint=release_fingerprint,
        )
    except Phase6ArtifactRestorationError:
        raise
    except (OSError, TypeError, ValueError, zipfile.BadZipFile):
        if published:
            _remove_created_destination(artifacts)
            _remove_created_destination(deployment)
        raise Phase6ArtifactRestorationError(_ERROR) from None


def _preserved_root(path: Path) -> Path:
    match = _PRESERVED_DIRECTORY.fullmatch(path.name)
    if match is None or any(candidate.is_symlink() for candidate in (path, *path.parents)):
        raise ValueError
    root = path.resolve(strict=True)
    if root.name != path.name or not root.is_dir():
        raise ValueError
    entries = list(root.iterdir())
    if (
        {entry.name for entry in entries} != set(_ARTIFACT_FILENAMES)
        or len(entries) != len(_ARTIFACT_FILENAMES)
        or any(entry.is_symlink() or not entry.is_file() for entry in entries)
    ):
        raise ValueError
    return root


def _new_destination(path: Path, literal_name: str) -> Path:
    if (
        path.name != literal_name
        or any(candidate.is_symlink() for candidate in (path, *path.parents))
        or path.exists()
    ):
        raise ValueError
    destination = path.resolve(strict=False)
    parent = destination.parent.resolve(strict=True)
    if destination.name != literal_name or destination.parent != parent or not parent.is_dir():
        raise ValueError
    return destination


def _validated_descriptor(preserved: Path) -> Mapping[str, object]:
    descriptor_path = preserved / DEPLOYMENT_DESCRIPTOR_FILENAME
    descriptor_size = descriptor_path.stat().st_size
    if not 1 <= descriptor_size <= _MAX_DESCRIPTOR_SIZE:
        raise ValueError
    raw = descriptor_path.read_bytes()
    descriptor = json.loads(raw)
    if not isinstance(descriptor, Mapping) or render_manifest(descriptor) != raw:
        raise ValueError
    if set(descriptor) != {
        "algorithm",
        "components",
        "format",
        "release_fingerprint",
        "target",
    }:
        raise ValueError
    release = descriptor["release_fingerprint"]
    if (
        descriptor["algorithm"] != "sha256"
        or descriptor["format"] != _DESCRIPTOR_FORMAT
        or descriptor["target"] != LINUX_ARM64_TARGET
        or not isinstance(release, str)
        or _FINGERPRINT.fullmatch(release) is None
        or release == "0" * 64
    ):
        raise ValueError
    suffix = _PRESERVED_DIRECTORY.fullmatch(preserved.name)
    if suffix is None or suffix.group(1) != release[:8]:
        raise ValueError

    components = descriptor["components"]
    if not isinstance(components, Mapping) or set(components) != {"agentcore", "lambda"}:
        raise ValueError
    for component, filename in (
        ("agentcore", AGENTCORE_ARCHIVE_FILENAME),
        ("lambda", LAMBDA_ARCHIVE_FILENAME),
    ):
        record = components[component]
        if not isinstance(record, Mapping) or set(record) != {
            "archive",
            "architecture",
            "component",
            "deployment_manifest_sha256",
            "package_format",
            "runtime",
        }:
            raise ValueError
        archive = record["archive"]
        manifest_fingerprint = record["deployment_manifest_sha256"]
        if (
            record["architecture"] != "arm64"
            or record["component"] != component
            or record["package_format"] != "zip"
            or record["runtime"] != "python3.12"
            or not isinstance(manifest_fingerprint, str)
            or _FINGERPRINT.fullmatch(manifest_fingerprint) is None
            or not isinstance(archive, Mapping)
            or set(archive) != {"path", "sha256", "size_bytes"}
            or archive["path"] != filename
            or not isinstance(archive["sha256"], str)
            or _FINGERPRINT.fullmatch(cast(str, archive["sha256"])) is None
            or type(archive["size_bytes"]) is not int
            or not 1 <= cast(int, archive["size_bytes"]) <= _MAX_ARCHIVE_SIZE
        ):
            raise ValueError
    return descriptor


def _validate_preserved_archives(preserved: Path, descriptor: Mapping[str, object]) -> None:
    components = cast(Mapping[str, Mapping[str, object]], descriptor["components"])
    for component, filename in (
        ("agentcore", AGENTCORE_ARCHIVE_FILENAME),
        ("lambda", LAMBDA_ARCHIVE_FILENAME),
    ):
        archive_record = cast(Mapping[str, object], components[component]["archive"])
        archive_path = preserved / filename
        expected_size = cast(int, archive_record["size_bytes"])
        if archive_path.stat().st_size != expected_size:
            raise ValueError
        if _file_sha256(archive_path) != archive_record["sha256"]:
            raise ValueError
        with zipfile.ZipFile(archive_path) as archive:
            _validated_zip_members(archive)


def _validated_zip_members(
    archive: zipfile.ZipFile,
) -> list[tuple[zipfile.ZipInfo, PurePosixPath]]:
    members = archive.infolist()
    if (
        not members
        or len(members) > _MAX_MEMBER_COUNT
        or archive.comment
        or len({member.filename for member in members}) != len(members)
    ):
        raise ValueError
    validated: list[tuple[zipfile.ZipInfo, PurePosixPath]] = []
    folded_paths: set[str] = set()
    total_size = 0
    for member in members:
        relative = member.filename
        path = PurePosixPath(relative)
        folded = relative.casefold()
        file_type = (member.external_attr >> 16) & 0o170000
        total_size += member.file_size
        if (
            not relative
            or not relative.isascii()
            or len(relative) > _MAX_MEMBER_PATH_LENGTH
            or "\x00" in relative
            or "\\" in relative
            or path.is_absolute()
            or path.as_posix() != relative
            or len(path.parts) > _MAX_MEMBER_PATH_DEPTH
            or any(
                part in {"", ".", ".."}
                or part.casefold() in {".git", "__pycache__"}
                or len(part) > 255
                for part in path.parts
            )
            or member.is_dir()
            or member.filename.endswith("/")
            or member.date_time != _ZIP_TIMESTAMP
            or member.compress_type != zipfile.ZIP_STORED
            or member.compress_size != member.file_size
            or member.create_system != 3
            or member.external_attr != 0o100644 << 16
            or file_type == 0o120000
            or member.flag_bits != 0
            or member.extra
            or member.comment
            or member.file_size < 0
            or member.file_size > _MAX_MEMBER_SIZE
            or total_size > _MAX_EXTRACTED_TREE_SIZE
            or folded in folded_paths
        ):
            raise ValueError
        folded_paths.add(folded)
        validated.append((member, path))
    for _, path in validated:
        if any(
            parent.as_posix().casefold() in folded_paths for parent in path.parents if parent.parts
        ):
            raise ValueError
    return validated


def _extract_archive(archive_path: Path, destination: Path) -> None:
    with zipfile.ZipFile(archive_path) as archive:
        members = _validated_zip_members(archive)
        for member, relative in members:
            target = destination.joinpath(*relative.parts)
            target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            with archive.open(member, "r") as source, target.open("xb") as output:
                _copy_stream(source, output)
            target.chmod(0o644)
            if target.stat().st_size != member.file_size:
                raise ValueError


def _copy_stream(source: BinaryIO, destination: BinaryIO) -> None:
    while chunk := source.read(_COPY_BUFFER_SIZE):
        destination.write(chunk)


def _copy_file_exclusive(source: Path, destination: Path) -> None:
    with source.open("rb") as input_file, destination.open("xb") as output_file:
        _copy_stream(input_file, output_file)
    destination.chmod(0o644)


def _publish_staged_restore(
    staged_deployment: Path,
    staged_artifacts: Path,
    *,
    deployment: Path,
    artifacts: Path,
) -> None:
    deployment_created = False
    artifacts_created = False
    try:
        deployment.mkdir(mode=0o700)
        deployment_created = True
        artifacts.mkdir(mode=0o700)
        artifacts_created = True
        for component in ("agentcore", "lambda"):
            (staged_deployment / component).rename(deployment / component)
        for filename in _ARTIFACT_FILENAMES:
            (staged_artifacts / filename).rename(artifacts / filename)
    except Exception:
        if artifacts_created:
            _remove_created_destination(artifacts)
        if deployment_created:
            _remove_created_destination(deployment)
        raise


def _remove_created_destination(path: Path) -> None:
    if path.exists() and not path.is_symlink():
        shutil.rmtree(path)


def _file_sha256(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as source:
        while chunk := source.read(_COPY_BUFFER_SIZE):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("preserved_artifacts", type=Path)
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
    arguments = parser.parse_args()
    try:
        restored = restore_phase6_deployment_artifacts(
            arguments.preserved_artifacts,
            deployment_destination=arguments.deployment_destination,
            artifact_destination=arguments.artifact_destination,
        )
    except Phase6ArtifactRestorationError as error:
        parser.exit(1, f"{error}\n")
    print(restored.deployment_root)
    print(restored.artifact_root)
    print(restored.release_fingerprint)


if __name__ == "__main__":
    main()


__all__ = [
    "Phase6ArtifactRestorationError",
    "RestoredPhase6Deployment",
    "restore_phase6_deployment_artifacts",
]
