"""Assemble immutable Phase 6.6 evidence fragments into one verified private bundle.

The assembler is deliberately offline.  Each fragment is bound by caller-supplied SHA-256
authorities for its ``records.json`` and ``artifact-files.json`` control files.  The artifact
digests inside those records remain the byte authorities for the referenced files.

Every input is revalidated without following symlinks, copied create-only beneath a gate-specific
directory, and checked again for mutation before success.  Evidence records are never rewritten;
only artifact-index relative paths are changed to describe the assembled layout.  The completed
bundle must pass the authoritative full Phase 6.6 evidence-set verifier.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import stat
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path, PurePosixPath
from typing import Any, Final

from mr_lister.acceptance.evidence_set import (
    EvidenceSetVerificationError,
    Phase66ArtifactFile,
    _declared_artifacts,
    _require_cross_record_bindings,
    _require_manifest_closure,
    _validated_artifact_files,
    _validated_records,
    _verify_artifacts,
    verify_phase66_evidence_set,
)
from mr_lister.acceptance.phase6 import phase66_acceptance_manifest

REPOSITORY_ROOT: Final = Path(__file__).resolve().parents[1]
PRIVATE_WORKSPACE_ROOT: Final = REPOSITORY_ROOT / ".mr_lister_private" / "phase66-acceptance"
RECORDS_FILENAME: Final = "records.json"
ARTIFACT_FILES_FILENAME: Final = "artifact-files.json"
GATE_DIRECTORY: Final = "gates"

_CONTROL_FILENAMES = frozenset({RECORDS_FILENAME, ARTIFACT_FILES_FILENAME})
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_MAX_CONTROL_FILE_BYTES = 100 * 1024 * 1024
_MAX_FRAGMENT_COUNT = 128
_READ_CHUNK_SIZE = 1024 * 1024


class Phase66EvidenceBundleAssemblyError(RuntimeError):
    """A fragment authority, filesystem boundary, or full-set join failed closed."""


@dataclass(frozen=True)
class FragmentAuthority:
    """Caller authority for one already-produced evidence fragment."""

    root: Path
    records_sha256: str
    artifact_files_sha256: str


@dataclass(frozen=True)
class _Metadata:
    device: int
    inode: int
    mode: int
    link_count: int
    byte_count: int
    modified_ns: int
    changed_ns: int

    @classmethod
    def from_stat(cls, value: os.stat_result) -> _Metadata:
        return cls(
            device=value.st_dev,
            inode=value.st_ino,
            mode=value.st_mode,
            link_count=value.st_nlink,
            byte_count=value.st_size,
            modified_ns=value.st_mtime_ns,
            changed_ns=value.st_ctime_ns,
        )

    @property
    def identity(self) -> tuple[int, int]:
        return self.device, self.inode


@dataclass(frozen=True)
class _FileSnapshot:
    relative_path: str
    metadata: _Metadata
    digest: str


@dataclass(frozen=True)
class _TreeSnapshot:
    root: _Metadata
    directories: dict[str, _Metadata]
    files: dict[str, _Metadata]


@dataclass(frozen=True)
class _LoadedFragment:
    authority: FragmentAuthority
    root: Path
    records: tuple[dict[str, Any], ...]
    artifact_files: tuple[Phase66ArtifactFile, ...]
    artifact_gates: dict[str, str]
    file_snapshots: dict[str, _FileSnapshot]
    tree_snapshot: _TreeSnapshot


def _canonical_json(value: object) -> bytes:
    try:
        return (
            json.dumps(
                value,
                sort_keys=True,
                indent=2,
                ensure_ascii=True,
                allow_nan=False,
            )
            + "\n"
        ).encode("utf-8")
    except (OverflowError, RecursionError, TypeError, ValueError):
        raise Phase66EvidenceBundleAssemblyError(
            "Evidence bundle data must be strict JSON"
        ) from None


def _reject_json_constant(_value: str) -> None:
    raise ValueError("non-finite JSON constant")


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, nested in pairs:
        if key in value:
            raise ValueError("duplicate JSON member")
        value[key] = nested
    return value


def _strict_json(payload: bytes) -> object:
    try:
        return json.loads(
            payload,
            parse_constant=_reject_json_constant,
            object_pairs_hook=_unique_json_object,
        )
    except (RecursionError, UnicodeDecodeError, ValueError, json.JSONDecodeError):
        raise Phase66EvidenceBundleAssemblyError(
            "A fragment control file must be strict JSON"
        ) from None


def _require_digest(value: str, label: str) -> str:
    if type(value) is not str or _DIGEST.fullmatch(value) is None:
        raise Phase66EvidenceBundleAssemblyError(f"{label} must be one lowercase SHA-256 digest")
    return value


def _private_path(path: Path, *, allow_workspace: bool = False) -> Path:
    candidate = Path(os.path.abspath(path))
    try:
        relative = candidate.relative_to(PRIVATE_WORKSPACE_ROOT)
    except ValueError:
        raise Phase66EvidenceBundleAssemblyError(
            "Evidence paths must stay in the repository-private Phase 6.6 workspace"
        ) from None
    if not allow_workspace and not relative.parts:
        raise Phase66EvidenceBundleAssemblyError("An evidence path must name a private child")
    return candidate


def _casefolded_path_parts(path: Path) -> tuple[str, ...]:
    return tuple(component.casefold() for component in path.parts)


def _casefolded_ancestor_or_same(left: Path, right: Path) -> bool:
    left_parts = _casefolded_path_parts(left)
    right_parts = _casefolded_path_parts(right)
    return len(left_parts) <= len(right_parts) and right_parts[: len(left_parts)] == left_parts


def _open_repository_root() -> int:
    flags = os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY | os.O_NOFOLLOW
    root = Path(os.path.abspath(REPOSITORY_ROOT))
    descriptor: int | None = None
    try:
        if not root.is_absolute() or root.parts[0] != os.sep:
            raise OSError
        descriptor = os.open(os.sep, flags)
        for component in root.parts[1:]:
            next_descriptor = os.open(component, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = next_descriptor
        if not stat.S_ISDIR(os.fstat(descriptor).st_mode):
            raise OSError
        result = descriptor
        descriptor = None
        return result
    except OSError:
        raise Phase66EvidenceBundleAssemblyError(
            "The repository root is not one stable directory chain"
        ) from None
    finally:
        if descriptor is not None:
            os.close(descriptor)


@contextmanager
def _open_private_directory(path: Path) -> Iterator[int]:
    directory = _private_path(path, allow_workspace=True)
    try:
        repository_relative = directory.relative_to(REPOSITORY_ROOT)
    except ValueError:
        raise Phase66EvidenceBundleAssemblyError(
            "The private workspace is not beneath the repository root"
        ) from None
    flags = os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY | os.O_NOFOLLOW
    descriptor: int | None = None
    try:
        descriptor = _open_repository_root()
        for component in repository_relative.parts:
            next_descriptor = os.open(component, flags, dir_fd=descriptor)
            metadata = os.fstat(next_descriptor)
            if not stat.S_ISDIR(metadata.st_mode) or metadata.st_mode & 0o077:
                os.close(next_descriptor)
                raise OSError
            os.close(descriptor)
            descriptor = next_descriptor
        yield descriptor
    except OSError:
        raise Phase66EvidenceBundleAssemblyError(
            "A private evidence directory chain is not confined"
        ) from None
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _open_relative_file(root_descriptor: int, relative_path: str) -> int:
    components = relative_path.split("/")
    current = os.dup(root_descriptor)
    try:
        directory_flags = os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY | os.O_NOFOLLOW
        for component in components[:-1]:
            next_descriptor = os.open(component, directory_flags, dir_fd=current)
            metadata = os.fstat(next_descriptor)
            if not stat.S_ISDIR(metadata.st_mode) or metadata.st_mode & 0o077:
                os.close(next_descriptor)
                raise OSError
            os.close(current)
            current = next_descriptor
        descriptor = os.open(
            components[-1],
            os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK,
            dir_fd=current,
        )
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or metadata.st_mode & 0o077
            or metadata.st_size < 1
        ):
            os.close(descriptor)
            raise OSError
        return descriptor
    except OSError:
        raise Phase66EvidenceBundleAssemblyError(
            "A fragment input is not one owner-only regular file"
        ) from None
    finally:
        os.close(current)


def _snapshot_open_file(
    descriptor: int,
    relative_path: str,
    *,
    maximum_bytes: int | None = None,
    retain_bytes: bool = False,
) -> tuple[_FileSnapshot, bytes | None]:
    before = os.fstat(descriptor)
    if maximum_bytes is not None and before.st_size > maximum_bytes:
        raise Phase66EvidenceBundleAssemblyError("A fragment control file exceeds its byte bound")
    digest = sha256()
    chunks: list[bytes] | None = [] if retain_bytes else None
    remaining = before.st_size
    while remaining:
        try:
            chunk = os.read(descriptor, min(_READ_CHUNK_SIZE, remaining))
        except OSError:
            raise Phase66EvidenceBundleAssemblyError(
                "A fragment input changed while it was being read"
            ) from None
        if not chunk:
            raise Phase66EvidenceBundleAssemblyError(
                "A fragment input changed while it was being read"
            )
        digest.update(chunk)
        if chunks is not None:
            chunks.append(chunk)
        remaining -= len(chunk)
    after = os.fstat(descriptor)
    if _Metadata.from_stat(before) != _Metadata.from_stat(after):
        raise Phase66EvidenceBundleAssemblyError("A fragment input changed while it was being read")
    return (
        _FileSnapshot(
            relative_path=relative_path,
            metadata=_Metadata.from_stat(before),
            digest=digest.hexdigest(),
        ),
        b"".join(chunks) if chunks is not None else None,
    )


def _read_control(
    root_descriptor: int,
    name: str,
    expected_digest: str,
) -> tuple[_FileSnapshot, bytes]:
    descriptor = _open_relative_file(root_descriptor, name)
    try:
        snapshot, payload = _snapshot_open_file(
            descriptor,
            name,
            maximum_bytes=_MAX_CONTROL_FILE_BYTES,
            retain_bytes=True,
        )
    finally:
        os.close(descriptor)
    if not secrets.compare_digest(snapshot.digest, expected_digest):
        raise Phase66EvidenceBundleAssemblyError(
            f"The caller SHA-256 authority for {name} does not match"
        )
    assert payload is not None
    return snapshot, payload


def _hash_relative_file(root_descriptor: int, relative_path: str) -> _FileSnapshot:
    descriptor = _open_relative_file(root_descriptor, relative_path)
    try:
        snapshot, _ = _snapshot_open_file(descriptor, relative_path)
    finally:
        os.close(descriptor)
    return snapshot


def _scan_tree(root_descriptor: int) -> _TreeSnapshot:
    directories: dict[str, _Metadata] = {}
    files: dict[str, _Metadata] = {}
    directory_identities = {_Metadata.from_stat(os.fstat(root_descriptor)).identity}

    def visit(descriptor: int, prefix: PurePosixPath | None) -> None:
        try:
            names = sorted(os.listdir(descriptor))
        except OSError:
            raise Phase66EvidenceBundleAssemblyError(
                "A fragment directory changed during inspection"
            ) from None
        for name in names:
            relative = name if prefix is None else f"{prefix.as_posix()}/{name}"
            try:
                before = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
            except OSError:
                raise Phase66EvidenceBundleAssemblyError(
                    "A fragment directory changed during inspection"
                ) from None
            metadata = _Metadata.from_stat(before)
            if stat.S_ISLNK(before.st_mode):
                raise Phase66EvidenceBundleAssemblyError("Fragment symlinks are forbidden")
            if stat.S_ISDIR(before.st_mode):
                if before.st_mode & 0o077 or metadata.identity in directory_identities:
                    raise Phase66EvidenceBundleAssemblyError(
                        "Fragment directories must be unique and owner-only"
                    )
                child: int | None = None
                try:
                    child = os.open(
                        name,
                        os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY | os.O_NOFOLLOW,
                        dir_fd=descriptor,
                    )
                    if _Metadata.from_stat(os.fstat(child)) != metadata:
                        raise OSError
                    directory_identities.add(metadata.identity)
                    directories[relative] = metadata
                    visit(child, PurePosixPath(relative))
                except OSError:
                    raise Phase66EvidenceBundleAssemblyError(
                        "A fragment directory changed during inspection"
                    ) from None
                finally:
                    if child is not None:
                        os.close(child)
            elif stat.S_ISREG(before.st_mode):
                if before.st_nlink != 1 or before.st_mode & 0o077 or before.st_size < 1:
                    raise Phase66EvidenceBundleAssemblyError(
                        "Fragment files must be unique owner-only regular files"
                    )
                files[relative] = metadata
            else:
                raise Phase66EvidenceBundleAssemblyError(
                    "A fragment may contain only private directories and regular files"
                )

    root = _Metadata.from_stat(os.fstat(root_descriptor))
    if not stat.S_ISDIR(root.mode) or root.mode & 0o077:
        raise Phase66EvidenceBundleAssemblyError("A fragment root is not owner-only")
    visit(root_descriptor, None)
    after = _Metadata.from_stat(os.fstat(root_descriptor))
    if root != after:
        raise Phase66EvidenceBundleAssemblyError("A fragment directory changed during inspection")
    return _TreeSnapshot(root=root, directories=directories, files=files)


def _expected_directories(paths: set[str]) -> set[str]:
    expected: set[str] = set()
    for relative_path in paths:
        parent = PurePosixPath(relative_path).parent
        while parent != PurePosixPath("."):
            expected.add(parent.as_posix())
            parent = parent.parent
    return expected


def _load_fragment(
    authority: FragmentAuthority,
    *,
    expected_source_commit_digest: str,
    expected_deployment_digest: str,
) -> _LoadedFragment:
    root = _private_path(authority.root)
    records_authority = _require_digest(authority.records_sha256, "Records authority")
    files_authority = _require_digest(
        authority.artifact_files_sha256,
        "Artifact-index authority",
    )
    with _open_private_directory(root) as root_descriptor:
        records_snapshot, records_payload = _read_control(
            root_descriptor,
            RECORDS_FILENAME,
            records_authority,
        )
        files_snapshot, files_payload = _read_control(
            root_descriptor,
            ARTIFACT_FILES_FILENAME,
            files_authority,
        )
        records_value = _strict_json(records_payload)
        files_value = _strict_json(files_payload)
        if not isinstance(records_value, list) or not isinstance(files_value, list):
            raise Phase66EvidenceBundleAssemblyError(
                "Fragment control files must contain JSON arrays"
            )
        if not all(isinstance(value, dict) for value in records_value):
            raise Phase66EvidenceBundleAssemblyError("Every fragment record must be one object")
        try:
            records = _validated_records(records_value)
            declared = _declared_artifacts(records)
            artifact_files = _validated_artifact_files(files_value)
            _verify_artifacts(declared, artifact_files, root)
        except EvidenceSetVerificationError:
            raise Phase66EvidenceBundleAssemblyError(
                "A fragment record, artifact index, or artifact failed validation"
            ) from None

        if any(record.source_commit_digest != expected_source_commit_digest for record in records):
            raise Phase66EvidenceBundleAssemblyError(
                "A fragment is stale for the expected source commit"
            )
        if any(
            record.deployment_digest is not None
            and record.deployment_digest != expected_deployment_digest
            for record in records
        ):
            raise Phase66EvidenceBundleAssemblyError(
                "A fragment is stale for the expected deployment"
            )

        artifact_gates: dict[str, str] = {}
        for record in records:
            for artifact in record.artifacts:
                if artifact.artifact_digest in artifact_gates:
                    raise Phase66EvidenceBundleAssemblyError(
                        "Artifact digests may not be reused across evidence records"
                    )
                artifact_gates[artifact.artifact_digest] = record.gate_id

        referenced_paths = {reference.relative_path for reference in artifact_files}
        if len({path.casefold() for path in referenced_paths}) != len(referenced_paths):
            raise Phase66EvidenceBundleAssemblyError(
                "Fragment artifact paths must not have case-folded aliases"
            )
        if referenced_paths & _CONTROL_FILENAMES:
            raise Phase66EvidenceBundleAssemblyError(
                "Fragment artifacts cannot alias their control files"
            )
        expected_files = {*_CONTROL_FILENAMES, *referenced_paths}
        tree = _scan_tree(root_descriptor)
        if set(tree.files) != expected_files or set(tree.directories) != _expected_directories(
            referenced_paths
        ):
            raise Phase66EvidenceBundleAssemblyError(
                "A fragment contains stale, missing, or extra filesystem entries"
            )

        snapshots = {
            RECORDS_FILENAME: records_snapshot,
            ARTIFACT_FILES_FILENAME: files_snapshot,
        }
        for control_snapshot in (records_snapshot, files_snapshot):
            if tree.files[control_snapshot.relative_path] != control_snapshot.metadata:
                raise Phase66EvidenceBundleAssemblyError(
                    "A fragment control file changed during validation"
                )
        for reference in artifact_files:
            snapshot = _hash_relative_file(root_descriptor, reference.relative_path)
            if tree.files[
                reference.relative_path
            ] != snapshot.metadata or not secrets.compare_digest(
                snapshot.digest, reference.artifact_digest
            ):
                raise Phase66EvidenceBundleAssemblyError(
                    "A fragment artifact changed during validation"
                )
            snapshots[reference.relative_path] = snapshot

    return _LoadedFragment(
        authority=authority,
        root=root,
        records=tuple(records_value),
        artifact_files=artifact_files,
        artifact_gates=artifact_gates,
        file_snapshots=snapshots,
        tree_snapshot=tree,
    )


def _validate_fragment_set(
    fragments: Sequence[FragmentAuthority],
    *,
    output_root: Path,
    expected_source_commit_digest: str,
    expected_deployment_digest: str,
) -> tuple[tuple[_LoadedFragment, ...], frozenset[tuple[int, int]]]:
    if (
        isinstance(fragments, (str, bytes, bytearray))
        or not fragments
        or len(fragments) > _MAX_FRAGMENT_COUNT
    ):
        raise Phase66EvidenceBundleAssemblyError(
            "The fragment authority count is outside the closed bound"
        )
    roots = tuple(_private_path(fragment.root) for fragment in fragments)
    if len(set(roots)) != len(roots) or len({str(root).casefold() for root in roots}) != len(roots):
        raise Phase66EvidenceBundleAssemblyError("Fragment root paths must be unique")
    for index, left in enumerate(roots):
        for right in roots[index + 1 :]:
            if _casefolded_ancestor_or_same(left, right) or _casefolded_ancestor_or_same(
                right, left
            ):
                raise Phase66EvidenceBundleAssemblyError("Fragment roots cannot be nested")
        if _casefolded_ancestor_or_same(output_root, left) or _casefolded_ancestor_or_same(
            left, output_root
        ):
            raise Phase66EvidenceBundleAssemblyError(
                "The output root and fragment roots must be disjoint"
            )

    loaded = tuple(
        _load_fragment(
            fragment,
            expected_source_commit_digest=expected_source_commit_digest,
            expected_deployment_digest=expected_deployment_digest,
        )
        for fragment in fragments
    )
    input_identities: set[tuple[int, int]] = set()
    directory_identities: set[tuple[int, int]] = set()
    input_paths: set[Path] = set()
    for fragment in loaded:
        fragment_directory_identities = {
            fragment.tree_snapshot.root.identity,
            *(metadata.identity for metadata in fragment.tree_snapshot.directories.values()),
        }
        if directory_identities & fragment_directory_identities:
            raise Phase66EvidenceBundleAssemblyError(
                "Fragment directory inodes must be globally unique"
            )
        directory_identities.update(fragment_directory_identities)
        for relative_path, snapshot in fragment.file_snapshots.items():
            path = fragment.root / relative_path
            if path in input_paths or snapshot.metadata.identity in input_identities:
                raise Phase66EvidenceBundleAssemblyError(
                    "Fragment input paths, digests, and inodes must be unique"
                )
            input_paths.add(path)
            input_identities.add(snapshot.metadata.identity)
    return loaded, frozenset(directory_identities)


def _preflight_full_set(
    fragments: tuple[_LoadedFragment, ...],
    *,
    expected_source_commit_digest: str,
    expected_deployment_digest: str,
) -> tuple[tuple[dict[str, Any], ...], dict[str, tuple[_LoadedFragment, Phase66ArtifactFile]]]:
    raw_records = tuple(record for fragment in fragments for record in fragment.records)
    try:
        validated_records = _validated_records(raw_records)
        _require_manifest_closure(validated_records)
        source_digest, deployment_digest, _, _ = _require_cross_record_bindings(validated_records)
        declared = _declared_artifacts(validated_records)
    except EvidenceSetVerificationError:
        raise Phase66EvidenceBundleAssemblyError(
            "Fragments do not form one exact prerequisite-closed Phase 6.6 record set"
        ) from None
    if source_digest != expected_source_commit_digest:
        raise Phase66EvidenceBundleAssemblyError("The assembled source authority drifted")
    if deployment_digest != expected_deployment_digest:
        raise Phase66EvidenceBundleAssemblyError("The assembled deployment authority drifted")

    artifacts: dict[str, tuple[_LoadedFragment, Phase66ArtifactFile]] = {}
    for fragment in fragments:
        for reference in fragment.artifact_files:
            if reference.artifact_digest in artifacts:
                raise Phase66EvidenceBundleAssemblyError(
                    "Fragment artifact digests must be globally unique"
                )
            artifacts[reference.artifact_digest] = (fragment, reference)
    if artifacts.keys() != declared.keys():
        raise Phase66EvidenceBundleAssemblyError(
            "Fragment artifact indexes do not exactly cover the full record set"
        )
    return raw_records, artifacts


def _create_output_root(
    output_root: Path,
    *,
    forbidden_parent_identities: frozenset[tuple[int, int]],
) -> int:
    parent = output_root.parent
    with _open_private_directory(parent) as parent_descriptor:
        if _Metadata.from_stat(os.fstat(parent_descriptor)).identity in forbidden_parent_identities:
            raise Phase66EvidenceBundleAssemblyError(
                "The output parent cannot alias a fragment directory"
            )
        try:
            os.mkdir(output_root.name, mode=0o700, dir_fd=parent_descriptor)
            descriptor = os.open(
                output_root.name,
                os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY | os.O_NOFOLLOW,
                dir_fd=parent_descriptor,
            )
        except OSError:
            raise Phase66EvidenceBundleAssemblyError(
                "The assembled output root must be one fresh private directory"
            ) from None
    metadata = os.fstat(descriptor)
    if not stat.S_ISDIR(metadata.st_mode) or metadata.st_mode & 0o077:
        os.close(descriptor)
        raise Phase66EvidenceBundleAssemblyError("The assembled output root is not owner-only")
    return descriptor


def _output_parent(root_descriptor: int, relative_path: str) -> tuple[int, str]:
    parts = relative_path.split("/")
    current = os.dup(root_descriptor)
    try:
        for component in parts[:-1]:
            try:
                os.mkdir(component, mode=0o700, dir_fd=current)
            except FileExistsError:
                pass
            next_descriptor = os.open(
                component,
                os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY | os.O_NOFOLLOW,
                dir_fd=current,
            )
            metadata = os.fstat(next_descriptor)
            if not stat.S_ISDIR(metadata.st_mode) or metadata.st_mode & 0o077:
                os.close(next_descriptor)
                raise OSError
            os.close(current)
            current = next_descriptor
        result = current
        current = -1
        return result, parts[-1]
    except OSError:
        raise Phase66EvidenceBundleAssemblyError(
            "A gate-specific output directory could not be created safely"
        ) from None
    finally:
        if current >= 0:
            os.close(current)


def _write_create_only(root_descriptor: int, relative_path: str, contents: bytes) -> None:
    parent_descriptor, name = _output_parent(root_descriptor, relative_path)
    temporary = f".{name}.{secrets.token_hex(12)}.tmp"
    descriptor: int | None = None
    try:
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
            0o600,
            dir_fd=parent_descriptor,
        )
        with os.fdopen(descriptor, "wb", closefd=True) as output:
            descriptor = None
            output.write(contents)
            output.flush()
            os.fsync(output.fileno())
        os.link(
            temporary,
            name,
            src_dir_fd=parent_descriptor,
            dst_dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
        os.unlink(temporary, dir_fd=parent_descriptor)
        os.fsync(parent_descriptor)
    except OSError:
        raise Phase66EvidenceBundleAssemblyError(
            "An assembled control file could not be created safely"
        ) from None
    finally:
        if descriptor is not None:
            os.close(descriptor)
        try:
            os.unlink(temporary, dir_fd=parent_descriptor)
        except FileNotFoundError:
            pass
        os.close(parent_descriptor)


def _copy_artifact(
    *,
    fragment: _LoadedFragment,
    reference: Phase66ArtifactFile,
    target_relative_path: str,
    output_descriptor: int,
) -> None:
    snapshot = fragment.file_snapshots[reference.relative_path]
    parent_descriptor, name = _output_parent(output_descriptor, target_relative_path)
    temporary = f".{name}.{secrets.token_hex(12)}.tmp"
    source_descriptor: int | None = None
    target_descriptor: int | None = None
    try:
        with _open_private_directory(fragment.root) as fragment_descriptor:
            source_descriptor = _open_relative_file(
                fragment_descriptor,
                reference.relative_path,
            )
            if _Metadata.from_stat(os.fstat(source_descriptor)) != snapshot.metadata:
                raise Phase66EvidenceBundleAssemblyError(
                    "A fragment input changed before it could be copied"
                )
            target_descriptor = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
                0o600,
                dir_fd=parent_descriptor,
            )
            digest = sha256()
            copied = 0
            while chunk := os.read(source_descriptor, _READ_CHUNK_SIZE):
                digest.update(chunk)
                copied += len(chunk)
                view = memoryview(chunk)
                while view:
                    written = os.write(target_descriptor, view)
                    if written < 1:
                        raise OSError
                    view = view[written:]
            os.fsync(target_descriptor)
            after = _Metadata.from_stat(os.fstat(source_descriptor))
            if (
                after != snapshot.metadata
                or copied != snapshot.metadata.byte_count
                or not secrets.compare_digest(digest.hexdigest(), snapshot.digest)
            ):
                raise Phase66EvidenceBundleAssemblyError(
                    "A fragment input changed while it was being copied"
                )
            os.close(target_descriptor)
            target_descriptor = None
            os.link(
                temporary,
                name,
                src_dir_fd=parent_descriptor,
                dst_dir_fd=parent_descriptor,
                follow_symlinks=False,
            )
            os.unlink(temporary, dir_fd=parent_descriptor)
            os.fsync(parent_descriptor)
    except Phase66EvidenceBundleAssemblyError:
        raise
    except OSError:
        raise Phase66EvidenceBundleAssemblyError(
            "An artifact could not be copied create-only"
        ) from None
    finally:
        if source_descriptor is not None:
            os.close(source_descriptor)
        if target_descriptor is not None:
            os.close(target_descriptor)
        try:
            os.unlink(temporary, dir_fd=parent_descriptor)
        except FileNotFoundError:
            pass
        os.close(parent_descriptor)


def _assert_fragment_unchanged(fragment: _LoadedFragment) -> None:
    with _open_private_directory(fragment.root) as root_descriptor:
        if _scan_tree(root_descriptor) != fragment.tree_snapshot:
            raise Phase66EvidenceBundleAssemblyError("A fragment changed during bundle assembly")
        for relative_path, original in fragment.file_snapshots.items():
            current = _hash_relative_file(root_descriptor, relative_path)
            if current != original:
                raise Phase66EvidenceBundleAssemblyError(
                    "A fragment changed during bundle assembly"
                )


def _verified_output_tree(
    output_root: Path,
    *,
    artifact_paths: set[str],
    records_digest: str,
    artifact_files_digest: str,
) -> _TreeSnapshot:
    expected_files = {*_CONTROL_FILENAMES, *artifact_paths}
    with _open_private_directory(output_root) as output_descriptor:
        tree = _scan_tree(output_descriptor)
        if set(tree.files) != expected_files or set(tree.directories) != _expected_directories(
            artifact_paths
        ):
            raise Phase66EvidenceBundleAssemblyError(
                "The assembled bundle contains a missing or extra filesystem entry"
            )
        for filename, expected_digest in (
            (RECORDS_FILENAME, records_digest),
            (ARTIFACT_FILES_FILENAME, artifact_files_digest),
        ):
            snapshot = _hash_relative_file(output_descriptor, filename)
            if tree.files[filename] != snapshot.metadata or not secrets.compare_digest(
                snapshot.digest, expected_digest
            ):
                raise Phase66EvidenceBundleAssemblyError(
                    "An assembled control file changed after creation"
                )
    return tree


def assemble_phase66_evidence_bundle(
    *,
    output_root: Path,
    fragments: Sequence[FragmentAuthority],
    expected_source_commit_digest: str,
    expected_deployment_digest: str,
) -> dict[str, object]:
    """Create one immutable full bundle from exact private fragment authorities."""

    source_digest = _require_digest(expected_source_commit_digest, "Source commit authority")
    deployment_digest = _require_digest(expected_deployment_digest, "Deployment authority")
    output = _private_path(output_root)
    loaded, fragment_directory_identities = _validate_fragment_set(
        fragments,
        output_root=output,
        expected_source_commit_digest=source_digest,
        expected_deployment_digest=deployment_digest,
    )
    raw_records, artifacts = _preflight_full_set(
        loaded,
        expected_source_commit_digest=source_digest,
        expected_deployment_digest=deployment_digest,
    )

    gate_order = {
        gate.gate_id: index for index, gate in enumerate(phase66_acceptance_manifest().gates)
    }
    ordered_records = tuple(
        sorted(
            raw_records,
            key=lambda record: (
                gate_order[record["gate_id"]],
                _canonical_json(record),
            ),
        )
    )
    output_references: list[dict[str, object]] = []
    copy_plan: list[tuple[_LoadedFragment, Phase66ArtifactFile, str]] = []
    output_paths: set[str] = set()
    folded_output_paths: set[str] = set()
    for artifact_digest in sorted(artifacts):
        fragment, reference = artifacts[artifact_digest]
        gate_id = fragment.artifact_gates[artifact_digest]
        target_path = f"{GATE_DIRECTORY}/{gate_id}/{reference.relative_path}"
        try:
            rewritten = Phase66ArtifactFile.model_validate(
                {
                    **reference.model_dump(),
                    "relative_path": target_path,
                }
            )
        except ValueError:
            raise Phase66EvidenceBundleAssemblyError(
                "A gate-specific artifact path is outside the closed path contract"
            ) from None
        folded_path = rewritten.relative_path.casefold()
        if (
            rewritten.relative_path in output_paths
            or folded_path in folded_output_paths
            or any(
                existing.startswith(f"{folded_path}/") or folded_path.startswith(f"{existing}/")
                for existing in folded_output_paths
            )
        ):
            raise Phase66EvidenceBundleAssemblyError(
                "Gate-specific artifact output paths must be unique"
            )
        output_paths.add(rewritten.relative_path)
        folded_output_paths.add(folded_path)
        output_references.append(rewritten.model_dump(mode="json"))
        copy_plan.append((fragment, reference, rewritten.relative_path))

    records_payload = _canonical_json(ordered_records)
    artifact_files_payload = _canonical_json(output_references)
    output_descriptor = _create_output_root(
        output,
        forbidden_parent_identities=fragment_directory_identities,
    )
    try:
        for fragment, reference, target_path in copy_plan:
            _copy_artifact(
                fragment=fragment,
                reference=reference,
                target_relative_path=target_path,
                output_descriptor=output_descriptor,
            )
        _write_create_only(output_descriptor, RECORDS_FILENAME, records_payload)
        _write_create_only(
            output_descriptor,
            ARTIFACT_FILES_FILENAME,
            artifact_files_payload,
        )
        os.fsync(output_descriptor)
    finally:
        os.close(output_descriptor)

    for fragment in loaded:
        _assert_fragment_unchanged(fragment)
    output_tree = _verified_output_tree(
        output,
        artifact_paths=output_paths,
        records_digest=sha256(records_payload).hexdigest(),
        artifact_files_digest=sha256(artifact_files_payload).hexdigest(),
    )

    try:
        verified = verify_phase66_evidence_set(
            list(ordered_records),
            output_references,
            allowed_artifact_root=output,
        )
    except EvidenceSetVerificationError:
        raise Phase66EvidenceBundleAssemblyError(
            "The assembled bundle failed the authoritative full-set verifier"
        ) from None

    for fragment in loaded:
        _assert_fragment_unchanged(fragment)
    if (
        _verified_output_tree(
            output,
            artifact_paths=output_paths,
            records_digest=sha256(records_payload).hexdigest(),
            artifact_files_digest=sha256(artifact_files_payload).hexdigest(),
        )
        != output_tree
    ):
        raise Phase66EvidenceBundleAssemblyError(
            "The assembled bundle changed during authoritative verification"
        )
    return {
        "result": "passed",
        "fragment_count": len(loaded),
        **verified.model_dump(mode="json"),
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-root", required=True, type=Path)
    parser.add_argument("--source-commit-digest", required=True)
    parser.add_argument("--deployment-digest", required=True)
    parser.add_argument(
        "--fragment",
        action="append",
        nargs=3,
        required=True,
        metavar=("ROOT", "RECORDS_SHA256", "ARTIFACT_FILES_SHA256"),
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    arguments = parser.parse_args(argv)
    fragments = tuple(
        FragmentAuthority(
            root=Path(root),
            records_sha256=records_digest,
            artifact_files_sha256=artifact_files_digest,
        )
        for root, records_digest, artifact_files_digest in arguments.fragment
    )
    try:
        summary = assemble_phase66_evidence_bundle(
            output_root=arguments.output_root,
            fragments=fragments,
            expected_source_commit_digest=arguments.source_commit_digest,
            expected_deployment_digest=arguments.deployment_digest,
        )
    except Phase66EvidenceBundleAssemblyError as error:
        parser.error(str(error))
    print(_canonical_json(summary).decode("utf-8"), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
