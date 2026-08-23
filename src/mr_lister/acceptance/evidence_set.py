"""Offline authority verifier for a complete Phase 6.6 acceptance evidence set.

The verifier deliberately accepts artifact paths only through a separate, closed index.  Evidence
records therefore remain path-free, while every attested artifact is opened beneath one explicit
root without following symlinks and is bound back to its record by kind, format, size, and SHA-256.
No network, AWS, or provider client is reachable from this module.
"""

from __future__ import annotations

import codecs
import json
import os
import stat
import xml.etree.ElementTree as ElementTree
import zipfile
import zlib
from collections import Counter
from collections.abc import Sequence
from hashlib import sha256
from pathlib import Path, PurePosixPath
from typing import Annotated, Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictInt,
    StringConstraints,
    field_validator,
)

from mr_lister.acceptance.phase6 import (
    PHASE66_CONTRACT_VERSION,
    AcceptanceEvidenceClass,
    AcceptanceOutcome,
    ArtifactFormat,
    ArtifactKind,
    ModeratedUserEvidenceRecord,
    ProviderDestructiveEvidenceRecord,
    phase66_acceptance_manifest,
    phase66_manifest_digest,
    validate_phase66_evidence,
)

type Digest = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
type RelativeArtifactPath = Annotated[
    str,
    StringConstraints(min_length=1, max_length=512, pattern=r"^[A-Za-z0-9_][A-Za-z0-9._/-]*$"),
]

_MAX_EVIDENCE_RECORDS = 128
_MAX_ARTIFACT_FILES = _MAX_EVIDENCE_RECORDS * 24
_MAX_ARCHIVE_MEMBERS = 10_000
_MAX_ARCHIVE_EXPANDED_BYTES = 2_000_000_000
_MAX_TOTAL_ARTIFACT_BYTES = 1_000_000_000_000
_MAX_STRUCTURED_ARTIFACT_BYTES = 100_000_000
_MAX_ZIP_ARTIFACT_BYTES = 1_000_000_000
_MAX_PNG_ARTIFACT_BYTES = 500_000_000
_MAX_PNG_DECODED_BYTES = 500_000_000
_MAX_JSON_DEPTH = 256
_READ_CHUNK_SIZE = 1024 * 1024
_FORMAT_SUFFIX = {
    ArtifactFormat.JSON: ".json",
    ArtifactFormat.JUNIT_XML: ".xml",
    ArtifactFormat.ZIP: ".zip",
    ArtifactFormat.PNG: ".png",
    ArtifactFormat.WEBM: ".webm",
}


class EvidenceSetVerificationError(ValueError):
    """The supplied evidence set cannot close the frozen Phase 6.6 manifest."""


class Phase66ArtifactFile(BaseModel):
    """Closed path-free-record binding to one file below the verifier-owned root."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    artifact_digest: Digest
    kind: ArtifactKind
    artifact_format: ArtifactFormat
    relative_path: RelativeArtifactPath

    @field_validator("relative_path", mode="before")
    @classmethod
    def relative_path_is_exact_text(cls, value: object) -> object:
        if type(value) is not str:
            raise ValueError("Artifact relative paths must be exact strings")
        return value

    @field_validator("relative_path")
    @classmethod
    def relative_path_is_canonical(cls, value: str) -> str:
        components = value.split("/")
        if (
            value.startswith("/")
            or value.endswith("/")
            or "\\" in value
            or any(component in {"", ".", ".."} for component in components)
            or PurePosixPath(value).as_posix() != value
        ):
            raise ValueError("Artifact paths must be canonical root-relative POSIX paths")
        return value


class Phase66EvidenceSetVerification(BaseModel):
    """Sanitized verification result containing only closed counters and digests."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    contract_version: Literal["6.6.0"] = PHASE66_CONTRACT_VERSION
    manifest_digest: Digest
    evidence_set_digest: Digest
    gate_set_digest: Digest
    source_commit_digest: Digest
    run_set_digest: Digest
    deployment_digest: Digest
    record_count: StrictInt = Field(ge=1, le=_MAX_EVIDENCE_RECORDS)
    gate_count: StrictInt = Field(ge=1, le=32)
    blocking_gate_count: StrictInt = Field(ge=1, le=32)
    artifact_count: StrictInt = Field(ge=1, le=_MAX_ARTIFACT_FILES)
    artifact_byte_count: StrictInt = Field(ge=1, le=_MAX_TOTAL_ARTIFACT_BYTES)
    job_binding_count: StrictInt = Field(ge=1, le=_MAX_EVIDENCE_RECORDS)
    run_count: StrictInt = Field(ge=1, le=_MAX_EVIDENCE_RECORDS)


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode()


def _strict_json_payload(value: object) -> bytes:
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    try:
        return _canonical_json(value)
    except (OverflowError, RecursionError, TypeError, ValueError):
        raise EvidenceSetVerificationError("Evidence-set input must be strict JSON") from None


def _require_bounded_json_depth(payload: bytes) -> None:
    depth = 0
    in_string = False
    escaped = False
    for value in payload:
        if in_string:
            if escaped:
                escaped = False
            elif value == 0x5C:
                escaped = True
            elif value == 0x22:
                in_string = False
        elif value == 0x22:
            in_string = True
        elif value in {0x5B, 0x7B}:
            depth += 1
            if depth > _MAX_JSON_DEPTH:
                raise EvidenceSetVerificationError("JSON nesting exceeds the closed depth bound")
        elif value in {0x5D, 0x7D}:
            depth -= 1
            if depth < 0:
                raise EvidenceSetVerificationError("JSON nesting is invalid")


def _validated_records(values: Sequence[object]) -> tuple[Any, ...]:
    if isinstance(values, (str, bytes, bytearray)):
        raise EvidenceSetVerificationError("Evidence-set record count is outside the closed bound")
    try:
        snapshot = tuple(values)
    except TypeError:
        raise EvidenceSetVerificationError(
            "Evidence-set record count is outside the closed bound"
        ) from None
    if not 1 <= len(snapshot) <= _MAX_EVIDENCE_RECORDS:
        raise EvidenceSetVerificationError("Evidence-set record count is outside the closed bound")
    records: list[Any] = []
    record_digests: set[str] = set()
    for value in snapshot:
        payload = _strict_json_payload(value)
        _require_bounded_json_depth(payload)
        try:
            record = validate_phase66_evidence(json.loads(payload))
        except (OverflowError, RecursionError, TypeError, ValueError):
            raise EvidenceSetVerificationError(
                "An evidence record failed Phase 6.6 validation"
            ) from None
        if record.outcome is not AcceptanceOutcome.PASSED:
            raise EvidenceSetVerificationError("Every evidence-set outcome must be passed")
        record_digest = sha256(_canonical_json(record.model_dump(mode="json"))).hexdigest()
        if record_digest in record_digests:
            raise EvidenceSetVerificationError("Duplicate evidence records are forbidden")
        record_digests.add(record_digest)
        records.append(record)
    return tuple(records)


def _validated_artifact_files(values: Sequence[object]) -> tuple[Phase66ArtifactFile, ...]:
    if isinstance(values, (str, bytes, bytearray)):
        raise EvidenceSetVerificationError("Artifact-file count is outside the closed bound")
    try:
        snapshot = tuple(values)
    except TypeError:
        raise EvidenceSetVerificationError(
            "Artifact-file count is outside the closed bound"
        ) from None
    if not 1 <= len(snapshot) <= _MAX_ARTIFACT_FILES:
        raise EvidenceSetVerificationError("Artifact-file count is outside the closed bound")
    files: list[Phase66ArtifactFile] = []
    for value in snapshot:
        payload = _strict_json_payload(value)
        try:
            files.append(Phase66ArtifactFile.model_validate_json(payload))
        except ValueError:
            raise EvidenceSetVerificationError("An artifact-file binding is invalid") from None
    return tuple(files)


def _require_manifest_closure(records: tuple[Any, ...]) -> Counter[str]:
    manifest = phase66_acceptance_manifest()
    gates = {gate.gate_id: gate for gate in manifest.gates}
    counts = Counter(record.gate_id for record in records)

    for gate_id, count in counts.items():
        gate = gates[gate_id]
        if count < gate.minimum_evidence_records:
            raise EvidenceSetVerificationError("A represented gate has insufficient evidence")
        for prerequisite in gate.prerequisites:
            required = gates[prerequisite].minimum_evidence_records
            if counts[prerequisite] < required:
                raise EvidenceSetVerificationError("An evidence gate prerequisite is not closed")

    if any(
        counts[gate_id] < gates[gate_id].minimum_evidence_records
        for gate_id in manifest.phase6_exit_gate_ids
    ):
        raise EvidenceSetVerificationError("The Phase 6 exit evidence set is incomplete")
    return counts


def _require_cross_record_bindings(
    records: tuple[Any, ...],
) -> tuple[str, str, int, tuple[str, ...]]:
    source_commits = {record.source_commit_digest for record in records}
    if len(source_commits) != 1:
        raise EvidenceSetVerificationError("Evidence records do not share one source commit")

    deployed = {
        record.deployment_digest
        for record in records
        if record.evidence_class is not AcceptanceEvidenceClass.OFFLINE
    }
    if len(deployed) != 1:
        raise EvidenceSetVerificationError("Deployed evidence does not share one deployment")

    semantic_record_keys: set[tuple[str, ...]] = set()
    provider_jobs: set[str] = set()
    provider_write_gates: set[str] = set()
    provider_run_gates: set[str] = set()
    moderated_sessions: set[str] = set()
    moderated_participants: set[str] = set()
    moderated_consents: set[str] = set()
    moderated_jobs: set[str] = set()
    primary_bindings: set[tuple[str, str, str]] = set()

    for record in records:
        if isinstance(record, ProviderDestructiveEvidenceRecord):
            semantic_key = (record.gate_id, record.run_digest, record.job_digest)
            if semantic_key in semantic_record_keys or record.job_digest in provider_jobs:
                raise EvidenceSetVerificationError("Provider evidence reuses a job authority")
            semantic_record_keys.add(semantic_key)
            provider_jobs.add(record.job_digest)
            provider_write_gates.add(record.provider_gate_attestation.provider_write_gate_digest)
            provider_run_gates.add(record.provider_gate_attestation.run_gate_digest)
            if record.gate_id == "provider.primary_same_job_canary":
                primary_bindings.add(
                    (record.run_digest, record.deployment_digest, record.job_digest)
                )
        elif isinstance(record, ModeratedUserEvidenceRecord):
            session = record.moderated_session
            semantic_key = (record.gate_id, session.session_record_digest)
            if (
                semantic_key in semantic_record_keys
                or session.session_record_digest in moderated_sessions
                or session.participant_digest in moderated_participants
                or session.consent_record_digest in moderated_consents
                or (record.job_digest is not None and record.job_digest in moderated_jobs)
            ):
                raise EvidenceSetVerificationError("Moderated evidence reuses a session authority")
            semantic_record_keys.add(semantic_key)
            moderated_sessions.add(session.session_record_digest)
            moderated_participants.add(session.participant_digest)
            moderated_consents.add(session.consent_record_digest)
            if record.job_digest is not None:
                moderated_jobs.add(record.job_digest)
        else:
            semantic_key = (record.gate_id, record.run_digest)
            if semantic_key in semantic_record_keys:
                raise EvidenceSetVerificationError("A gate reuses the same evidence authority")
            semantic_record_keys.add(semantic_key)

    if len(provider_write_gates) != sum(
        isinstance(record, ProviderDestructiveEvidenceRecord) for record in records
    ):
        raise EvidenceSetVerificationError("Provider write-gate authority is reused")
    if provider_write_gates & provider_run_gates:
        raise EvidenceSetVerificationError("Provider run and write authorities overlap")

    for record in records:
        if (
            isinstance(record, ModeratedUserEvidenceRecord)
            and record.job_digest is not None
            and (record.run_digest, record.deployment_digest, record.job_digest)
            not in primary_bindings
        ):
            raise EvidenceSetVerificationError(
                "Moderated job evidence lacks a same-run provider record"
            )

    job_bindings = {record.job_digest for record in records if record.job_digest is not None}
    source_commit = next(iter(source_commits))
    deployment_digest = next(iter(deployed))
    runs = tuple(sorted({record.run_digest for record in records}))
    return source_commit, deployment_digest, len(job_bindings), runs


def _declared_artifacts(records: tuple[Any, ...]) -> dict[str, Any]:
    declared: dict[str, Any] = {}
    for record in records:
        for artifact in record.artifacts:
            if artifact.artifact_digest in declared:
                raise EvidenceSetVerificationError("Acceptance artifact reuse is forbidden")
            declared[artifact.artifact_digest] = artifact
    if not declared:
        raise EvidenceSetVerificationError("The evidence set has no artifacts")
    return declared


def _open_root(allowed_root: str | os.PathLike[str]) -> int:
    current_fd: int | None = None
    try:
        root = Path(allowed_root)
        components = root.parts
        if (
            not root.is_absolute()
            or len(components) < 2
            or components[0] != os.sep
            or any(component in {"", ".", ".."} for component in components[1:])
        ):
            raise OSError
        flags = os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY | os.O_NOFOLLOW
        current_fd = os.open(os.sep, flags)
        for component in components[1:]:
            next_fd = os.open(component, flags, dir_fd=current_fd)
            os.close(current_fd)
            current_fd = next_fd
        if not stat.S_ISDIR(os.fstat(current_fd).st_mode):
            raise OSError
        root_fd = current_fd
        current_fd = None
        return root_fd
    except (OSError, TypeError, ValueError):
        if current_fd is not None:
            os.close(current_fd)
        raise EvidenceSetVerificationError(
            "The allowed artifact root is not a stable directory"
        ) from None


def _open_relative_file(root_fd: int, relative_path: str) -> int:
    components = relative_path.split("/")
    current_fd = os.dup(root_fd)
    try:
        directory_flags = os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY | os.O_NOFOLLOW
        for component in components[:-1]:
            next_fd = os.open(component, directory_flags, dir_fd=current_fd)
            os.close(current_fd)
            current_fd = next_fd
        file_flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK
        file_fd = os.open(components[-1], file_flags, dir_fd=current_fd)
    except OSError:
        os.close(current_fd)
        raise EvidenceSetVerificationError(
            "An artifact file is not a confined regular file"
        ) from None
    os.close(current_fd)
    opened = os.fstat(file_fd)
    if not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1:
        os.close(file_fd)
        raise EvidenceSetVerificationError("An artifact file is not a confined regular file")
    return file_fd


def _hash_file(file_fd: int) -> str:
    os.lseek(file_fd, 0, os.SEEK_SET)
    digest = sha256()
    while chunk := os.read(file_fd, _READ_CHUNK_SIZE):
        digest.update(chunk)
    return digest.hexdigest()


def _reject_json_constant(_value: str) -> None:
    raise ValueError("non-finite JSON constant")


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, nested in pairs:
        if key in value:
            raise ValueError("duplicate JSON object member")
        value[key] = nested
    return value


def _verify_json(file_fd: int) -> None:
    with os.fdopen(os.dup(file_fd), "rb") as source:
        source.seek(0)
        _require_bounded_json_depth(source.read())
        source.seek(0)
        json.load(
            source,
            parse_constant=_reject_json_constant,
            object_pairs_hook=_unique_json_object,
        )


def _verify_junit_xml(file_fd: int) -> None:
    with os.fdopen(os.dup(file_fd), "rb") as source:
        source.seek(0)
        decoder = codecs.getincrementaldecoder("utf-8")("strict")
        lowered_tail = ""
        while chunk := source.read(_READ_CHUNK_SIZE):
            lowered = (lowered_tail + decoder.decode(chunk)).casefold()
            if "<!doctype" in lowered or "<!entity" in lowered:
                raise ValueError("XML authority declarations are forbidden")
            lowered_tail = lowered[-16:]
        decoder.decode(b"", final=True)
        source.seek(0)
        root_name: str | None = None
        for event, element in ElementTree.iterparse(source, events=("start", "end")):
            if event == "start" and root_name is None:
                root_name = element.tag.rsplit("}", 1)[-1]
            if event == "end":
                element.clear()
        if root_name not in {"testsuite", "testsuites"}:
            raise ValueError("not a JUnit document")


def _canonical_archive_member(name: str) -> bool:
    if not name or "\x00" in name or "\\" in name or name.startswith("/"):
        return False
    candidate = name[:-1] if name.endswith("/") else name
    components = candidate.split("/")
    return (
        bool(candidate)
        and ":" not in components[0]
        and all(component not in {"", ".", ".."} for component in components)
    )


def _verify_zip(file_fd: int, kind: ArtifactKind) -> None:
    with os.fdopen(os.dup(file_fd), "rb") as source, zipfile.ZipFile(source) as archive:
        members = archive.infolist()
        if not members or len(members) > _MAX_ARCHIVE_MEMBERS:
            raise ValueError("ZIP member count is outside the closed bound")
        expanded_bytes = 0
        trace_members: list[zipfile.ZipInfo] = []
        member_names: set[str] = set()
        for member in members:
            if (
                not _canonical_archive_member(member.filename)
                or member.filename.casefold() in member_names
                or member.flag_bits & 0x1
                or stat.S_ISLNK(member.external_attr >> 16)
            ):
                raise ValueError("ZIP member authority is invalid")
            member_names.add(member.filename.casefold())
            expanded_bytes += member.file_size
            if expanded_bytes > _MAX_ARCHIVE_EXPANDED_BYTES:
                raise ValueError("ZIP expansion is outside the closed bound")
            if member.filename.endswith(".trace") and member.file_size > 0:
                trace_members.append(member)
        if kind is ArtifactKind.BROWSER_TRACE:
            if not trace_members:
                raise ValueError("Browser trace archive is missing trace data")
            trace_recognized = False
            for member in trace_members:
                with archive.open(member) as trace:
                    first_line = trace.readline(1_000_001).strip()
                if len(first_line) <= 1_000_000:
                    _require_bounded_json_depth(first_line)
                    candidate = json.loads(first_line)
                    trace_recognized = (
                        isinstance(candidate, dict) and type(candidate.get("type")) is str
                    )
                if trace_recognized:
                    break
            if not trace_recognized:
                raise ValueError("Browser trace archive is not a recognized sanitized trace")
        if archive.testzip() is not None:
            raise ValueError("ZIP member checksum failed")


def _read_exact(file_fd: int, byte_count: int) -> bytes:
    chunks: list[bytes] = []
    remaining = byte_count
    while remaining:
        chunk = os.read(file_fd, min(remaining, _READ_CHUNK_SIZE))
        if not chunk:
            raise ValueError("truncated binary artifact")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _verify_png(file_fd: int) -> None:
    os.lseek(file_fd, 0, os.SEEK_SET)
    if _read_exact(file_fd, 8) != b"\x89PNG\r\n\x1a\n":
        raise ValueError("invalid PNG signature")
    saw_header = False
    saw_image_data = False
    image_stream: zlib.Decompress | None = None
    decoded_bytes = 0
    while True:
        length = int.from_bytes(_read_exact(file_fd, 4), "big")
        chunk_type = _read_exact(file_fd, 4)
        if length > 100_000_000 or len(chunk_type) != 4:
            raise ValueError("invalid PNG chunk")
        checksum = zlib.crc32(chunk_type)
        header_data = b""
        remaining = length
        if chunk_type == b"IDAT" and image_stream is None:
            image_stream = zlib.decompressobj()
        while remaining:
            data = _read_exact(file_fd, min(remaining, _READ_CHUNK_SIZE))
            if not saw_header:
                header_data += data
            checksum = zlib.crc32(data, checksum)
            if chunk_type == b"IDAT" and image_stream is not None:
                decoded = image_stream.decompress(data, _MAX_PNG_DECODED_BYTES - decoded_bytes + 1)
                decoded_bytes += len(decoded)
                if decoded_bytes > _MAX_PNG_DECODED_BYTES or image_stream.unconsumed_tail:
                    raise ValueError("PNG expansion is outside the closed bound")
            remaining -= len(data)
        if int.from_bytes(_read_exact(file_fd, 4), "big") != checksum & 0xFFFFFFFF:
            raise ValueError("invalid PNG chunk checksum")
        if not saw_header:
            if chunk_type != b"IHDR" or length != 13:
                raise ValueError("invalid PNG header")
            if (
                int.from_bytes(header_data[:4], "big") == 0
                or int.from_bytes(header_data[4:8], "big") == 0
            ):
                raise ValueError("invalid PNG dimensions")
            saw_header = True
        if chunk_type == b"IDAT":
            saw_image_data = True
        if chunk_type == b"IEND":
            if (
                length != 0
                or not saw_image_data
                or image_stream is None
                or not image_stream.eof
                or image_stream.unused_data
                or os.read(file_fd, 1)
            ):
                raise ValueError("invalid PNG terminator")
            return


def _verify_webm(file_fd: int) -> None:
    os.lseek(file_fd, 0, os.SEEK_SET)
    header = os.read(file_fd, 32)
    if not header.startswith(b"\x1aE\xdf\xa3"):
        raise ValueError("invalid WebM signature")


def _verify_format(file_fd: int, artifact: Any, relative_path: str) -> None:
    if PurePosixPath(relative_path).suffix.casefold() != _FORMAT_SUFFIX[artifact.artifact_format]:
        raise EvidenceSetVerificationError("Artifact filename and declared format do not match")
    try:
        if artifact.artifact_format is ArtifactFormat.JSON:
            _verify_json(file_fd)
        elif artifact.artifact_format is ArtifactFormat.JUNIT_XML:
            _verify_junit_xml(file_fd)
        elif artifact.artifact_format is ArtifactFormat.ZIP:
            _verify_zip(file_fd, artifact.kind)
        elif artifact.artifact_format is ArtifactFormat.PNG:
            _verify_png(file_fd)
        else:
            _verify_webm(file_fd)
    except (
        EOFError,
        ElementTree.ParseError,
        NotImplementedError,
        OSError,
        OverflowError,
        RecursionError,
        RuntimeError,
        UnicodeError,
        ValueError,
        zipfile.BadZipFile,
        zlib.error,
    ):
        raise EvidenceSetVerificationError(
            "Artifact bytes do not match the declared format"
        ) from None


def _verify_artifacts(
    declared: dict[str, Any],
    artifact_files: tuple[Phase66ArtifactFile, ...],
    allowed_root: str | os.PathLike[str],
) -> int:
    references: dict[str, Phase66ArtifactFile] = {}
    relative_paths: set[str] = set()
    for reference in artifact_files:
        if reference.artifact_digest in references or reference.relative_path in relative_paths:
            raise EvidenceSetVerificationError("Artifact-file bindings must be unique")
        references[reference.artifact_digest] = reference
        relative_paths.add(reference.relative_path)
    if references.keys() != declared.keys():
        raise EvidenceSetVerificationError("Artifact-file bindings must exactly match evidence")

    root_fd = _open_root(allowed_root)
    total_bytes = 0
    physical_files: set[tuple[int, int]] = set()
    try:
        for artifact_digest in sorted(declared):
            artifact = declared[artifact_digest]
            reference = references[artifact_digest]
            if (
                reference.kind is not artifact.kind
                or reference.artifact_format is not artifact.artifact_format
            ):
                raise EvidenceSetVerificationError("Artifact kind or format binding does not match")
            file_fd = _open_relative_file(root_fd, reference.relative_path)
            try:
                before = os.fstat(file_fd)
                physical_identity = (before.st_dev, before.st_ino)
                if physical_identity in physical_files:
                    raise EvidenceSetVerificationError("Artifact-file inode reuse is forbidden")
                physical_files.add(physical_identity)
                if before.st_size != artifact.byte_count:
                    raise EvidenceSetVerificationError("Artifact byte count does not match")
                format_limit = {
                    ArtifactFormat.JSON: _MAX_STRUCTURED_ARTIFACT_BYTES,
                    ArtifactFormat.JUNIT_XML: _MAX_STRUCTURED_ARTIFACT_BYTES,
                    ArtifactFormat.ZIP: _MAX_ZIP_ARTIFACT_BYTES,
                    ArtifactFormat.PNG: _MAX_PNG_ARTIFACT_BYTES,
                    ArtifactFormat.WEBM: 10_000_000_000,
                }[artifact.artifact_format]
                if before.st_size > format_limit:
                    raise EvidenceSetVerificationError(
                        "Artifact bytes exceed the closed format bound"
                    )
                if total_bytes + before.st_size > _MAX_TOTAL_ARTIFACT_BYTES:
                    raise EvidenceSetVerificationError(
                        "Aggregate artifact bytes exceed the closed verifier bound"
                    )
                if _hash_file(file_fd) != artifact.artifact_digest:
                    raise EvidenceSetVerificationError("Artifact SHA-256 does not match")
                _verify_format(file_fd, artifact, reference.relative_path)
                after = os.fstat(file_fd)
                if (
                    before.st_size,
                    before.st_mtime_ns,
                    before.st_ctime_ns,
                ) != (
                    after.st_size,
                    after.st_mtime_ns,
                    after.st_ctime_ns,
                ):
                    raise EvidenceSetVerificationError("Artifact changed during verification")
                total_bytes += before.st_size
            finally:
                os.close(file_fd)
    finally:
        os.close(root_fd)
    return total_bytes


def verify_phase66_evidence_set(
    records: Sequence[object],
    artifact_files: Sequence[object],
    *,
    allowed_artifact_root: str | os.PathLike[str],
) -> Phase66EvidenceSetVerification:
    """Verify a complete blocking Phase 6.6 evidence set and its confined artifacts.

    The nonblocking five-session target may be omitted.  If represented, it must meet its frozen
    minimum count and prerequisite.  Every blocking gate is always required; callers cannot weaken
    the manifest by selecting a subset.
    """

    validated_records = _validated_records(records)
    counts = _require_manifest_closure(validated_records)
    source_commit, deployment_digest, job_binding_count, runs = _require_cross_record_bindings(
        validated_records
    )
    declared = _declared_artifacts(validated_records)
    validated_files = _validated_artifact_files(artifact_files)
    artifact_bytes = _verify_artifacts(declared, validated_files, allowed_artifact_root)

    manifest = phase66_acceptance_manifest()
    record_payloads = sorted(
        (record.model_dump(mode="json") for record in validated_records),
        key=lambda value: _canonical_json(value),
    )
    gate_counts = sorted((gate_id, count) for gate_id, count in counts.items())
    gate_set_digest = sha256(_canonical_json(gate_counts)).hexdigest()
    evidence_set_digest = sha256(
        _canonical_json(
            {
                "manifest_digest": phase66_manifest_digest(),
                "records": record_payloads,
                "verified_artifacts": [
                    {
                        "artifact_digest": artifact.artifact_digest,
                        "artifact_format": artifact.artifact_format,
                        "byte_count": artifact.byte_count,
                        "kind": artifact.kind,
                    }
                    for artifact in sorted(
                        declared.values(), key=lambda value: value.artifact_digest
                    )
                ],
            }
        )
    ).hexdigest()
    return Phase66EvidenceSetVerification(
        manifest_digest=phase66_manifest_digest(),
        evidence_set_digest=evidence_set_digest,
        gate_set_digest=gate_set_digest,
        source_commit_digest=source_commit,
        run_set_digest=sha256(_canonical_json(runs)).hexdigest(),
        deployment_digest=deployment_digest,
        record_count=len(validated_records),
        gate_count=len(counts),
        blocking_gate_count=len(manifest.phase6_exit_gate_ids),
        artifact_count=len(declared),
        artifact_byte_count=artifact_bytes,
        job_binding_count=job_binding_count,
        run_count=len(runs),
    )


__all__ = [
    "EvidenceSetVerificationError",
    "Phase66ArtifactFile",
    "Phase66EvidenceSetVerification",
    "verify_phase66_evidence_set",
]
