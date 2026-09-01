#!/usr/bin/env python3
"""Prepare one closed Phase 6.6 first-time-seller evidence fragment.

This offline normalizer consumes an already verified primary provider-gate fragment plus two
closed, digest-bound human observation inputs.  It performs no browser, AWS, Cognito, Printify,
or other network operation.  The emitted evidence inherits the provider gate's exact source,
deployment, run, actor, job, work, and Strands-correlation authority; moderated evidence never
grants provider-write authority.

Inputs and outputs must remain below one caller-selected, repository-private closure root.  Raw
identity, credentials, provider payloads, URLs, and free-form observations have no input schema.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import stat
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path
from typing import Annotated, Any, Final, Literal, get_args, get_origin

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictInt,
    StringConstraints,
    ValidationError,
    field_validator,
    model_validator,
)

from mr_lister.acceptance.evidence_set import (
    Phase66ArtifactFile,
    _declared_artifacts,
    _validated_artifact_files,
    _validated_records,
    _verify_artifacts,
)
from mr_lister.acceptance.phase6 import (
    PHASE66_FIRST_TIME_SELLER_TASK_SHA256,
    AcceptanceEvidenceClass,
    AcceptanceOutcome,
    ArtifactFormat,
    ArtifactKind,
    ProviderDestructiveEvidenceRecord,
    phase66_acceptance_manifest,
    phase66_manifest_digest,
    validate_phase66_evidence,
)

REPOSITORY_ROOT: Final = Path(__file__).resolve().parents[1]
PRIVATE_ROOT: Final = REPOSITORY_ROOT / ".mr_lister_private" / "phase66-acceptance"
TASK_CONTRACT_RELATIVE_PATH: Final = Path(
    "contracts/acceptance/phase6.6.first-time-seller-task.json"
)
TASK_CONTRACT_SHA256: Final = PHASE66_FIRST_TIME_SELLER_TASK_SHA256
MANIFEST_DIGEST: Final = "84851fe2ed78072d077cc5e642d0e222619b9a7226367219b536b7e2aaac7d73"

TASK_CONTRACT: Final = "phase6.6-moderated-first-time-seller-task-v1"
CONSENT_CONTRACT: Final = "phase6.6-moderated-participant-consent-v1"
OBSERVATION_CONTRACT: Final = "phase6.6-moderated-first-time-seller-observation-v1"
ARTIFACT_CONTRACT: Final = "phase6.6-sanitized-moderated-session-record-v1"
GATE_ID: Final = "moderated.first_time_seller_exit"
PROVIDER_GATE_ID: Final = "provider.primary_same_job_canary"
OUTPUT_DIRECTORY_NAME: Final = "phase66-moderated-first-time-seller"
SESSION_RECORD_FILENAME: Final = "moderated_session_record.json"
RECORDS_FILENAME: Final = "records.json"
ARTIFACT_FILES_FILENAME: Final = "artifact-files.json"
OUTPUT_FILENAMES: Final = (
    SESSION_RECORD_FILENAME,
    RECORDS_FILENAME,
    ARTIFACT_FILES_FILENAME,
)

EXPECTED_AUTHORITY_BINDINGS: Final = (
    "source_commit_digest",
    "deployment_digest",
    "run_digest",
    "job_digest",
    "actor_digest",
    "work_digest",
    "correlation_digest",
)
EXPECTED_FORBIDDEN_ASSISTANCE: Final = (
    "external_documentation",
    "moderator_help",
    "operator_intervention",
)
EXPECTED_ACCESSIBILITY_CHECKS: Final = (
    "screen_reader",
    "keyboard_only",
    "visible_focus",
    "contrast",
    "reduced_motion",
    "zoom_200_percent",
)
EXPECTED_MANUAL_JOURNEYS: Final = (
    "upload",
    "review",
    "edit",
    "refresh",
    "cancel",
    "retry",
    "logout",
)
EXPECTED_PARTICIPANT_REQUIREMENTS: Final = (
    "explicit_consent",
    "first_time_seller",
    "invited_seller",
    "mfa_complete",
    "authenticated_session",
)
EXPECTED_TASK_STEPS: Final = (
    "upload_supported_artwork",
    "recover_same_job_after_browser_restart",
    "find_same_job_strands_evidence",
    "review_unpublished_printify_draft",
    "complete_human_approval",
    "stop_before_publication",
)
EXPECTED_ASSERTIONS: Final = (
    "invite_and_mfa_complete",
    "supported_upload_completes",
    "browser_restart_recovers_job",
    "unpublished_boundary_is_understood",
    "strands_evidence_is_found",
    "human_decision_completes_without_intervention",
)

MAX_INPUT_BYTES: Final = 2 * 1024 * 1024
MAX_SESSION_SECONDS: Final = 86_400
UTC_TIMESTAMP: Final = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$")

type Digest = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
type CanonicalTimestamp = Annotated[
    str,
    StringConstraints(pattern=r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$"),
]


class Phase66ModeratedEvidenceError(RuntimeError):
    """One authority, privacy, schema, confinement, or immutable-output check failed."""


class _ClosedModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    @model_validator(mode="before")
    @classmethod
    def literal_boolean_and_integer_types_are_exact(cls, value: object) -> object:
        if isinstance(value, Mapping):
            for name, field in cls.model_fields.items():
                if get_origin(field.annotation) is not Literal or name not in value:
                    continue
                expected = get_args(field.annotation)
                if (
                    len(expected) == 1
                    and type(expected[0]) in {bool, int}
                    and type(value[name]) is not type(expected[0])
                ):
                    raise ValueError("Literal boolean and integer fields must use exact JSON types")
        return value


class _PrivacyBoundary(_ClosedModel):
    free_text_forbidden: Literal[True]
    raw_authority_forbidden: Literal[True]
    raw_identity_forbidden: Literal[True]
    secrets_forbidden: Literal[True]


class _PublicationBoundary(_ClosedModel):
    approval_is_terminal: Literal[True]
    etsy_publication_is_phase7: Literal[True]
    publication_must_remain_disabled: Literal[True]
    provider_draft_must_remain_unpublished: Literal[True]


class _FrozenTaskContract(_ClosedModel):
    acceptance_manifest_digest: Literal[MANIFEST_DIGEST]
    authority_bindings: tuple[str, ...]
    contract: Literal[TASK_CONTRACT]
    evidence_class: Literal["moderated_user"]
    forbidden_assistance: tuple[str, ...]
    frozen_at: Literal["2026-08-31T20:00:00Z"]
    gate_id: Literal[GATE_ID]
    manual_accessibility_checks: tuple[str, ...]
    manual_journeys: tuple[str, ...]
    participant_requirements: tuple[str, ...]
    privacy_boundary: _PrivacyBoundary
    provider_prerequisite_gate_id: Literal[PROVIDER_GATE_ID]
    provider_write_authority: Literal["separate_provider_evidence_only"]
    publication_boundary: _PublicationBoundary
    status: Literal["frozen"]
    task_steps: tuple[str, ...]

    @field_validator(
        "authority_bindings",
        "forbidden_assistance",
        "manual_accessibility_checks",
        "manual_journeys",
        "participant_requirements",
        "task_steps",
        mode="before",
    )
    @classmethod
    def json_arrays_are_frozen_as_tuples(cls, value: object) -> object:
        if type(value) is list:
            return tuple(value)
        return value

    @model_validator(mode="after")
    def frozen_order_and_contents_are_exact(self) -> _FrozenTaskContract:
        if (
            self.authority_bindings != EXPECTED_AUTHORITY_BINDINGS
            or self.forbidden_assistance != EXPECTED_FORBIDDEN_ASSISTANCE
            or self.manual_accessibility_checks != EXPECTED_ACCESSIBILITY_CHECKS
            or self.manual_journeys != EXPECTED_MANUAL_JOURNEYS
            or self.participant_requirements != EXPECTED_PARTICIPANT_REQUIREMENTS
            or self.task_steps != EXPECTED_TASK_STEPS
        ):
            raise ValueError("The moderated task contract has drifted")
        return self


class _ConsentRecord(_ClosedModel):
    consent_contract: Literal[CONSENT_CONTRACT]
    recorded_at: CanonicalTimestamp
    participant_digest: Digest
    task_contract_digest: Literal[TASK_CONTRACT_SHA256]
    explicit_consent: Literal[True]
    first_time_seller: Literal[True]
    observation_recording_accepted: Literal[True]
    raw_identity_retained: Literal[False]
    free_text_value_count: Literal[0]

    @field_validator("recorded_at")
    @classmethod
    def timestamp_is_calendar_valid(cls, value: str) -> str:
        _parse_timestamp(value)
        return value


class _SessionAuthority(_ClosedModel):
    source_commit_digest: Digest
    deployment_digest: Digest
    run_digest: Digest
    job_digest: Digest
    actor_digest: Digest
    work_digest: Digest
    correlation_digest: Digest
    provider_primary_record_digest: Digest
    task_contract_digest: Literal[TASK_CONTRACT_SHA256]


class _AuthenticatedAccess(_ClosedModel):
    invited_seller: Literal[True]
    mfa_complete: Literal[True]
    authenticated_session_observed: Literal[True]
    session_renewal_succeeded: Literal[True]
    credential_material_retained: Literal[False]


class _AssistanceObservation(_ClosedModel):
    external_documentation_used: Literal[False]
    moderator_help_used: Literal[False]
    operator_intervention_count: Literal[0]


class _FlowObservation(_ClosedModel):
    supported_upload_completed: Literal[True]
    artwork_normalization_completed: Literal[True]
    browser_restarted: Literal[True]
    same_job_recovered_after_restart: Literal[True]
    same_job_strands_evidence_found: Literal[True]
    unpublished_printify_draft_reviewed: Literal[True]
    unpublished_boundary_understood: Literal[True]
    human_approval_completed: Literal[True]
    final_job_state: Literal["APPROVED"]


class _AccessibilityObservation(_ClosedModel):
    screen_reader_passed: Literal[True]
    keyboard_only_passed: Literal[True]
    visible_focus_passed: Literal[True]
    contrast_passed: Literal[True]
    reduced_motion_passed: Literal[True]
    zoom_200_percent_passed: Literal[True]


class _ManualJourneys(_ClosedModel):
    upload_passed: Literal[True]
    review_passed: Literal[True]
    edit_passed: Literal[True]
    refresh_passed: Literal[True]
    cancel_passed: Literal[True]
    retry_passed: Literal[True]
    logout_passed: Literal[True]


class _PublicationObservation(_ClosedModel):
    publication_disabled: Literal[True]
    publication_action_absent: Literal[True]
    provider_draft_state: Literal["unpublished_unlocked"]
    publication_attempt_count: Literal[0]
    order_attempt_count: Literal[0]
    fulfillment_attempt_count: Literal[0]
    provider_write_authority_is_separate: Literal[True]


class _PrivacyObservation(_ClosedModel):
    forbidden_field_match_count: Literal[0]
    sensitive_value_match_count: Literal[0]
    free_text_value_count: Literal[0]
    raw_authority_retained: Literal[False]
    raw_identity_retained: Literal[False]


class _SessionObservation(_ClosedModel):
    observation_contract: Literal[OBSERVATION_CONTRACT]
    started_at: CanonicalTimestamp
    completed_at: CanonicalTimestamp
    participant_digest: Digest
    consent_record_digest: Digest
    authority: _SessionAuthority
    authenticated_access: _AuthenticatedAccess
    assistance: _AssistanceObservation
    flow: _FlowObservation
    accessibility: _AccessibilityObservation
    manual_journeys: _ManualJourneys
    publication: _PublicationObservation
    privacy: _PrivacyObservation

    @model_validator(mode="after")
    def session_time_is_ordered_and_bounded(self) -> _SessionObservation:
        started = _parse_timestamp(self.started_at)
        completed = _parse_timestamp(self.completed_at)
        if completed < started or completed - started > timedelta(seconds=MAX_SESSION_SECONDS):
            raise ValueError("The moderated session time is outside the closed bound")
        return self


class _SanitizedSessionArtifact(_ClosedModel):
    artifact_contract: Literal[ARTIFACT_CONTRACT]
    gate_id: Literal[GATE_ID]
    result: Literal["passed"]
    recorded_at: CanonicalTimestamp
    source_commit_digest: Digest
    deployment_digest: Digest
    run_digest: Digest
    job_digest: Digest
    actor_digest: Digest
    work_digest: Digest
    correlation_digest: Digest
    provider_primary_record_digest: Digest
    task_contract_digest: Literal[TASK_CONTRACT_SHA256]
    participant_digest: Digest
    consent_record_digest: Digest
    session_record_digest: Digest
    first_time_seller: Literal[True]
    explicit_consent: Literal[True]
    completed_supported_flow: Literal[True]
    duration_seconds: StrictInt = Field(ge=1, le=MAX_SESSION_SECONDS)
    authenticated_access: _AuthenticatedAccess
    assistance: _AssistanceObservation
    flow: _FlowObservation
    accessibility: _AccessibilityObservation
    manual_journeys: _ManualJourneys
    publication: _PublicationObservation
    privacy: _PrivacyObservation


@dataclass(frozen=True, slots=True)
class _ExactFile:
    payload: bytes
    digest: str
    byte_count: int
    identity: tuple[int, int]


def _canonical(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (OverflowError, RecursionError, TypeError, ValueError):
        raise Phase66ModeratedEvidenceError(
            "A moderated evidence value is not strict JSON"
        ) from None


def _render(value: object) -> bytes:
    return _canonical(value) + b"\n"


def _digest(value: object) -> str:
    return sha256(_canonical(value)).hexdigest()


def _parse_timestamp(value: str) -> datetime:
    if UTC_TIMESTAMP.fullmatch(value) is None:
        raise ValueError("Timestamp is not canonical UTC text")
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)


def _reject_json_constant(_value: str) -> None:
    raise ValueError("Non-finite JSON constant")


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, nested in pairs:
        if key in value:
            raise ValueError("Duplicate JSON member")
        value[key] = nested
    return value


def _strict_json(payload: bytes) -> object:
    try:
        return json.loads(
            payload,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_json_constant,
        )
    except (RecursionError, UnicodeDecodeError, ValueError, json.JSONDecodeError):
        raise Phase66ModeratedEvidenceError(
            "A moderated evidence input is not strict JSON"
        ) from None


def _effective_uid() -> int:
    return os.geteuid()


def _require_non_root() -> None:
    if _effective_uid() == 0:
        raise Phase66ModeratedEvidenceError(
            "Moderated human/provider evidence must never be prepared as root"
        )


def _absolute(path: Path) -> Path:
    return Path(os.path.abspath(path))


def _private_path(path: Path) -> Path:
    candidate = _absolute(path)
    private_root = _absolute(PRIVATE_ROOT)
    try:
        relative = candidate.relative_to(private_root)
    except ValueError:
        raise Phase66ModeratedEvidenceError(
            "Moderated evidence paths must stay in the repository-private workspace"
        ) from None
    if not relative.parts:
        raise Phase66ModeratedEvidenceError("A closure root must name a private child")
    return candidate


def _closure_child(path: Path, closure_root: Path) -> Path:
    candidate = _private_path(path)
    closure = _private_path(closure_root)
    try:
        relative = candidate.relative_to(closure)
    except ValueError:
        raise Phase66ModeratedEvidenceError(
            "Moderated evidence inputs must stay in the selected closure root"
        ) from None
    if not relative.parts:
        raise Phase66ModeratedEvidenceError("A moderated evidence input must name a file")
    return candidate


def _open_repository_root() -> int:
    flags = os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY | os.O_NOFOLLOW
    root = _absolute(REPOSITORY_ROOT)
    descriptor: int | None = None
    try:
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
        raise Phase66ModeratedEvidenceError(
            "The repository root is not one stable directory chain"
        ) from None
    finally:
        if descriptor is not None:
            os.close(descriptor)


@contextmanager
def _private_directory_descriptor(path: Path) -> Iterator[int]:
    directory = _private_path(path)
    descriptor: int | None = None
    flags = os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY | os.O_NOFOLLOW
    try:
        descriptor = _open_repository_root()
        for component in directory.relative_to(_absolute(REPOSITORY_ROOT)).parts:
            next_descriptor: int | None = None
            try:
                next_descriptor = os.open(component, flags, dir_fd=descriptor)
                metadata = os.fstat(next_descriptor)
                if not stat.S_ISDIR(metadata.st_mode) or metadata.st_mode & 0o077:
                    raise OSError
            except OSError:
                if next_descriptor is not None:
                    os.close(next_descriptor)
                raise
            os.close(descriptor)
            descriptor = next_descriptor
        yield descriptor
    except (OSError, ValueError):
        raise Phase66ModeratedEvidenceError(
            "A private closure directory chain is not confined and owner-only"
        ) from None
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _read_regular_file_at(
    parent_descriptor: int,
    name: str,
    *,
    max_bytes: int,
    owner_only: bool,
) -> _ExactFile:
    descriptor: int | None = None
    try:
        descriptor = os.open(
            name,
            os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW,
            dir_fd=parent_descriptor,
        )
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or (owner_only and before.st_mode & 0o077)
            or not 1 <= before.st_size <= max_bytes
        ):
            raise OSError
        chunks: list[bytes] = []
        remaining = before.st_size
        while remaining:
            chunk = os.read(descriptor, min(1024 * 1024, remaining))
            if not chunk:
                raise OSError
            chunks.append(chunk)
            remaining -= len(chunk)
        after = os.fstat(descriptor)
    except OSError:
        raise Phase66ModeratedEvidenceError(
            "A moderated evidence input is not one stable regular file"
        ) from None
    finally:
        if descriptor is not None:
            os.close(descriptor)
    if (
        before.st_dev,
        before.st_ino,
        before.st_size,
        before.st_mtime_ns,
        before.st_ctime_ns,
    ) != (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
        after.st_ctime_ns,
    ):
        raise Phase66ModeratedEvidenceError("A moderated evidence input changed during its read")
    payload = b"".join(chunks)
    return _ExactFile(
        payload=payload,
        digest=sha256(payload).hexdigest(),
        byte_count=len(payload),
        identity=(before.st_dev, before.st_ino),
    )


def _read_exact_private(
    path: Path,
    *,
    closure_root: Path,
    expected_digest: str,
    expected_size: int,
) -> _ExactFile:
    if (
        type(expected_digest) is not str
        or re.fullmatch(r"[0-9a-f]{64}", expected_digest) is None
        or (type(expected_size) is not int or not 1 <= expected_size <= MAX_INPUT_BYTES)
    ):
        raise Phase66ModeratedEvidenceError("An exact moderated input binding is invalid")
    candidate = _closure_child(path, closure_root)
    with _private_directory_descriptor(candidate.parent) as parent_descriptor:
        value = _read_regular_file_at(
            parent_descriptor,
            candidate.name,
            max_bytes=MAX_INPUT_BYTES,
            owner_only=True,
        )
    if value.byte_count != expected_size or not secrets.compare_digest(
        value.digest, expected_digest
    ):
        raise Phase66ModeratedEvidenceError(
            "A moderated evidence input changed or does not match its exact binding"
        )
    return value


def _read_task_contract(expected_digest: str) -> tuple[_FrozenTaskContract, _ExactFile]:
    if type(expected_digest) is not str or not secrets.compare_digest(
        expected_digest, TASK_CONTRACT_SHA256
    ):
        raise Phase66ModeratedEvidenceError(
            "The caller did not bind the frozen moderated task contract"
        )
    contract_path = _absolute(REPOSITORY_ROOT / TASK_CONTRACT_RELATIVE_PATH)
    parent_descriptor: int | None = None
    try:
        parent_descriptor = _open_repository_root()
        flags = os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY | os.O_NOFOLLOW
        for component in TASK_CONTRACT_RELATIVE_PATH.parts[:-1]:
            next_descriptor = os.open(component, flags, dir_fd=parent_descriptor)
            os.close(parent_descriptor)
            parent_descriptor = next_descriptor
        value = _read_regular_file_at(
            parent_descriptor,
            contract_path.name,
            max_bytes=MAX_INPUT_BYTES,
            owner_only=False,
        )
    except OSError:
        raise Phase66ModeratedEvidenceError(
            "The frozen moderated task contract is not one stable tracked file"
        ) from None
    finally:
        if parent_descriptor is not None:
            os.close(parent_descriptor)
    if not secrets.compare_digest(value.digest, TASK_CONTRACT_SHA256):
        raise Phase66ModeratedEvidenceError("The frozen moderated task contract has drifted")
    try:
        contract = _FrozenTaskContract.model_validate_json(_canonical(_strict_json(value.payload)))
    except (ValidationError, ValueError):
        raise Phase66ModeratedEvidenceError(
            "The frozen moderated task contract schema has drifted"
        ) from None
    if phase66_manifest_digest() != MANIFEST_DIGEST:
        raise Phase66ModeratedEvidenceError("The frozen Phase 6.6 acceptance manifest has drifted")
    return contract, value


@contextmanager
def _fresh_output_directory(closure_root: Path) -> Iterator[tuple[Path, int]]:
    closure = _private_path(closure_root)
    output = closure / OUTPUT_DIRECTORY_NAME
    with _private_directory_descriptor(closure) as closure_descriptor:
        try:
            os.mkdir(OUTPUT_DIRECTORY_NAME, mode=0o700, dir_fd=closure_descriptor)
            descriptor = os.open(
                OUTPUT_DIRECTORY_NAME,
                os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY | os.O_NOFOLLOW,
                dir_fd=closure_descriptor,
            )
        except OSError:
            raise Phase66ModeratedEvidenceError(
                "The moderated evidence output must be one fresh closure child"
            ) from None
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISDIR(metadata.st_mode) or metadata.st_mode & 0o077:
            raise Phase66ModeratedEvidenceError("The moderated evidence output is not owner-only")
        yield output, descriptor
    finally:
        os.close(descriptor)


def _write_once(directory_descriptor: int, name: str, payload: bytes) -> None:
    if not name or name in {".", ".."} or "/" in name or "\x00" in name:
        raise Phase66ModeratedEvidenceError("A moderated evidence filename is invalid")
    temporary = f".{name}.{secrets.token_hex(12)}.tmp"
    descriptor: int | None = None
    try:
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
            0o600,
            dir_fd=directory_descriptor,
        )
        with os.fdopen(descriptor, "wb", closefd=True) as stream:
            descriptor = None
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(
            temporary,
            name,
            src_dir_fd=directory_descriptor,
            dst_dir_fd=directory_descriptor,
            follow_symlinks=False,
        )
        os.unlink(temporary, dir_fd=directory_descriptor)
        os.fsync(directory_descriptor)
    except OSError:
        raise Phase66ModeratedEvidenceError(
            "An immutable moderated evidence output could not be written"
        ) from None
    finally:
        if descriptor is not None:
            os.close(descriptor)
        try:
            os.unlink(temporary, dir_fd=directory_descriptor)
        except FileNotFoundError:
            pass


def _verify_written_output(
    *,
    output_root: Path,
    output_descriptor: int,
    expected: Mapping[str, bytes],
) -> None:
    before = os.fstat(output_descriptor)
    if (
        not stat.S_ISDIR(before.st_mode)
        or before.st_mode & 0o077
        or set(os.listdir(output_descriptor)) != set(OUTPUT_FILENAMES)
        or set(expected) != set(OUTPUT_FILENAMES)
    ):
        raise Phase66ModeratedEvidenceError(
            "The written moderated evidence directory is not the exact closed output"
        )
    for filename in OUTPUT_FILENAMES:
        observed = _read_regular_file_at(
            output_descriptor,
            filename,
            max_bytes=MAX_INPUT_BYTES,
            owner_only=True,
        )
        payload = expected[filename]
        if (
            observed.byte_count != len(payload)
            or not secrets.compare_digest(observed.digest, sha256(payload).hexdigest())
            or not secrets.compare_digest(observed.payload, payload)
        ):
            raise Phase66ModeratedEvidenceError(
                "A written moderated evidence control or artifact file drifted"
            )
    after = os.fstat(output_descriptor)
    if (
        before.st_dev,
        before.st_ino,
        before.st_mode,
        before.st_nlink,
        before.st_mtime_ns,
        before.st_ctime_ns,
    ) != (
        after.st_dev,
        after.st_ino,
        after.st_mode,
        after.st_nlink,
        after.st_mtime_ns,
        after.st_ctime_ns,
    ):
        raise Phase66ModeratedEvidenceError(
            "The written moderated evidence directory changed during verification"
        )
    with _private_directory_descriptor(output_root) as reopened_descriptor:
        reopened = os.fstat(reopened_descriptor)
        if (reopened.st_dev, reopened.st_ino) != (after.st_dev, after.st_ino):
            raise Phase66ModeratedEvidenceError(
                "The moderated evidence output path changed during verification"
            )


def _gate_authority() -> None:
    matches = tuple(gate for gate in phase66_acceptance_manifest().gates if gate.gate_id == GATE_ID)
    if (
        len(matches) != 1
        or matches[0].evidence_class is not AcceptanceEvidenceClass.MODERATED_USER
        or matches[0].required_assertions != EXPECTED_ASSERTIONS
        or matches[0].required_artifact_kinds != (ArtifactKind.MODERATED_SESSION_RECORD,)
        or matches[0].prerequisites != ("deployed.edge_auth_owner_smoke", PROVIDER_GATE_ID)
        or matches[0].provider_mutation_policy.value != "separate_provider_evidence"
    ):
        raise Phase66ModeratedEvidenceError(
            "The frozen first-time-seller acceptance gate has drifted"
        )


def _validated_provider_fragment(
    *,
    records_input: _ExactFile,
    artifact_files_input: _ExactFile,
    fragment_root: Path,
) -> ProviderDestructiveEvidenceRecord:
    records_value = _strict_json(records_input.payload)
    files_value = _strict_json(artifact_files_input.payload)
    if not isinstance(records_value, list) or not isinstance(files_value, list):
        raise Phase66ModeratedEvidenceError(
            "The provider prerequisite indexes are not exact JSON arrays"
        )
    try:
        records = _validated_records(records_value)
        files = _validated_artifact_files(files_value)
        declared = _declared_artifacts(records)
        artifact_bytes = _verify_artifacts(declared, files, fragment_root)
    except ValueError:
        raise Phase66ModeratedEvidenceError(
            "The provider prerequisite fragment failed authoritative verification"
        ) from None
    if (
        len(records) != 1
        or not isinstance(records[0], ProviderDestructiveEvidenceRecord)
        or records[0].gate_id != PROVIDER_GATE_ID
        or records[0].outcome is not AcceptanceOutcome.PASSED
        or records[0].work_digest is None
        or records[0].correlation_digest is None
        or len(declared) != 3
        or artifact_bytes != sum(artifact.byte_count for artifact in records[0].artifacts)
    ):
        raise Phase66ModeratedEvidenceError(
            "The prerequisite is not one exact passed primary provider gate"
        )
    return records[0]


def _validate_human_inputs(
    *,
    consent_input: _ExactFile,
    observation_input: _ExactFile,
    provider: ProviderDestructiveEvidenceRecord,
    provider_record_digest: str,
    now: datetime,
) -> tuple[_ConsentRecord, _SessionObservation, int]:
    try:
        consent = _ConsentRecord.model_validate(_strict_json(consent_input.payload))
        observation = _SessionObservation.model_validate(_strict_json(observation_input.payload))
    except (ValidationError, ValueError):
        raise Phase66ModeratedEvidenceError(
            "A human observation does not match the closed sanitized schema"
        ) from None
    authority = observation.authority
    if (
        consent.participant_digest != observation.participant_digest
        or observation.consent_record_digest != consent_input.digest
        or authority.provider_primary_record_digest != provider_record_digest
        or authority.source_commit_digest != provider.source_commit_digest
        or authority.deployment_digest != provider.deployment_digest
        or authority.run_digest != provider.run_digest
        or authority.job_digest != provider.job_digest
        or authority.actor_digest != provider.actor_digests[0]
        or authority.work_digest != provider.work_digest
        or authority.correlation_digest != provider.correlation_digest
    ):
        raise Phase66ModeratedEvidenceError(
            "The moderated session does not bind the exact primary provider run/job/deployment"
        )
    if (
        len(
            {
                consent.participant_digest,
                consent_input.digest,
                observation_input.digest,
                provider_record_digest,
                provider.run_digest,
                provider.job_digest,
                provider.actor_digests[0],
                provider.work_digest,
                provider.correlation_digest,
            }
        )
        != 9
    ):
        raise Phase66ModeratedEvidenceError(
            "Moderated participant, session, and provider authorities must not be reused"
        )

    consent_at = _parse_timestamp(consent.recorded_at)
    started_at = _parse_timestamp(observation.started_at)
    completed_at = _parse_timestamp(observation.completed_at)
    provider_at = provider.recorded_at
    if not isinstance(now, datetime) or now.utcoffset() is None:
        raise Phase66ModeratedEvidenceError("The moderated evidence clock must be timezone-aware")
    if (
        consent_at > started_at
        or not started_at <= provider_at <= completed_at
        or completed_at > now.astimezone(UTC)
    ):
        raise Phase66ModeratedEvidenceError(
            "Consent, provider, and moderated-session timestamps are not ordered"
        )
    duration_seconds = int((completed_at - started_at).total_seconds())
    if not 1 <= duration_seconds <= MAX_SESSION_SECONDS:
        raise Phase66ModeratedEvidenceError(
            "The moderated-session duration is outside the closed bound"
        )
    return consent, observation, duration_seconds


def _build_outputs(
    *,
    consent: _ConsentRecord,
    consent_input: _ExactFile,
    observation: _SessionObservation,
    observation_input: _ExactFile,
    provider: ProviderDestructiveEvidenceRecord,
    provider_record_digest: str,
    duration_seconds: int,
) -> tuple[dict[str, bytes], dict[str, object]]:
    authority = observation.authority
    try:
        artifact = _SanitizedSessionArtifact.model_validate(
            {
                "artifact_contract": ARTIFACT_CONTRACT,
                "gate_id": GATE_ID,
                "result": "passed",
                "recorded_at": observation.completed_at,
                "source_commit_digest": authority.source_commit_digest,
                "deployment_digest": authority.deployment_digest,
                "run_digest": authority.run_digest,
                "job_digest": authority.job_digest,
                "actor_digest": authority.actor_digest,
                "work_digest": authority.work_digest,
                "correlation_digest": authority.correlation_digest,
                "provider_primary_record_digest": provider_record_digest,
                "task_contract_digest": TASK_CONTRACT_SHA256,
                "participant_digest": consent.participant_digest,
                "consent_record_digest": consent_input.digest,
                "session_record_digest": observation_input.digest,
                "first_time_seller": True,
                "explicit_consent": True,
                "completed_supported_flow": True,
                "duration_seconds": duration_seconds,
                "authenticated_access": observation.authenticated_access.model_dump(mode="json"),
                "assistance": observation.assistance.model_dump(mode="json"),
                "flow": observation.flow.model_dump(mode="json"),
                "accessibility": observation.accessibility.model_dump(mode="json"),
                "manual_journeys": observation.manual_journeys.model_dump(mode="json"),
                "publication": observation.publication.model_dump(mode="json"),
                "privacy": observation.privacy.model_dump(mode="json"),
            }
        )
    except ValidationError:
        raise Phase66ModeratedEvidenceError(
            "The sanitized moderated-session artifact failed its closed schema"
        ) from None
    artifact_bytes = _render(artifact.model_dump(mode="json"))
    artifact_digest = sha256(artifact_bytes).hexdigest()
    artifact_evidence = {
        "artifact_digest": artifact_digest,
        "artifact_format": ArtifactFormat.JSON.value,
        "byte_count": len(artifact_bytes),
        "kind": ArtifactKind.MODERATED_SESSION_RECORD.value,
        "redaction_verified": True,
    }
    try:
        artifact_file = Phase66ArtifactFile.model_validate(
            {
                "artifact_digest": artifact_digest,
                "artifact_format": ArtifactFormat.JSON,
                "kind": ArtifactKind.MODERATED_SESSION_RECORD,
                "relative_path": SESSION_RECORD_FILENAME,
            }
        )
    except ValidationError:
        raise Phase66ModeratedEvidenceError(
            "The moderated artifact index failed its closed schema"
        ) from None

    assertion_observations = {
        "invite_and_mfa_complete": {
            "authenticated_access": observation.authenticated_access.model_dump(mode="json")
        },
        "supported_upload_completes": {
            "artwork_normalization_completed": True,
            "supported_upload_completed": True,
        },
        "browser_restart_recovers_job": {
            "browser_restarted": True,
            "same_job_recovered_after_restart": True,
        },
        "unpublished_boundary_is_understood": {
            "provider_draft_state": "unpublished_unlocked",
            "publication_disabled": True,
            "unpublished_boundary_understood": True,
        },
        "strands_evidence_is_found": {
            "correlation_digest": authority.correlation_digest,
            "same_job_strands_evidence_found": True,
            "work_digest": authority.work_digest,
        },
        "human_decision_completes_without_intervention": {
            "completed_supported_flow": True,
            "external_documentation_used": False,
            "moderator_help_used": False,
            "operator_intervention_count": 0,
        },
    }
    assertions = [
        {
            "assertion_id": assertion_id,
            "observation_digest": _digest(
                {
                    "artifact_digest": artifact_digest,
                    "assertion_id": assertion_id,
                    "observation": assertion_observations[assertion_id],
                    "provider_primary_record_digest": provider_record_digest,
                    "run_digest": provider.run_digest,
                }
            ),
            "observed_count": 1,
            "passed": True,
        }
        for assertion_id in EXPECTED_ASSERTIONS
    ]
    try:
        record = validate_phase66_evidence(
            {
                "actor_digests": [provider.actor_digests[0]],
                "artifacts": [artifact_evidence],
                "assertions": assertions,
                "correlation_digest": provider.correlation_digest,
                "deployment_digest": provider.deployment_digest,
                "evidence_class": AcceptanceEvidenceClass.MODERATED_USER.value,
                "gate_id": GATE_ID,
                "job_digest": provider.job_digest,
                "manifest_digest": phase66_manifest_digest(),
                "moderated_session": {
                    "participant_digest": consent.participant_digest,
                    "consent_record_digest": consent_input.digest,
                    "task_script_digest": TASK_CONTRACT_SHA256,
                    "session_record_digest": observation_input.digest,
                    "first_time_seller": True,
                    "external_documentation_used": False,
                    "operator_intervention_count": 0,
                    "completed_supported_flow": True,
                    "duration_seconds": duration_seconds,
                },
                "outcome": AcceptanceOutcome.PASSED.value,
                "privacy": {
                    "forbidden_field_match_count": 0,
                    "free_text_value_count": 0,
                    "sanitizer_contract": "phase6.6-sanitized-evidence-v1",
                    "sensitive_value_match_count": 0,
                },
                "provider_call_summary": None,
                "provider_gate_attestation": None,
                "recorded_at": observation.completed_at,
                "run_digest": provider.run_digest,
                "schema_version": "6.6.0",
                "source_commit_digest": provider.source_commit_digest,
                "work_digest": provider.work_digest,
            }
        )
    except ValueError:
        raise Phase66ModeratedEvidenceError(
            "The moderated evidence record failed the authoritative schema"
        ) from None
    record_value = record.model_dump(mode="json")
    outputs = {
        SESSION_RECORD_FILENAME: artifact_bytes,
        RECORDS_FILENAME: _render([record_value]),
        ARTIFACT_FILES_FILENAME: _render([artifact_file.model_dump(mode="json")]),
    }
    if tuple(outputs) != OUTPUT_FILENAMES:
        raise Phase66ModeratedEvidenceError("The closed moderated evidence output set drifted")

    # Validate every transport/schema boundary before creating the output directory.
    records_value = _strict_json(outputs[RECORDS_FILENAME])
    artifact_files_value = _strict_json(outputs[ARTIFACT_FILES_FILENAME])
    if not isinstance(records_value, list) or not isinstance(artifact_files_value, list):
        raise Phase66ModeratedEvidenceError("The moderated evidence indexes are not arrays")
    try:
        validated_records = _validated_records(records_value)
        validated_files = _validated_artifact_files(artifact_files_value)
        declared = _declared_artifacts(validated_records)
    except ValueError:
        raise Phase66ModeratedEvidenceError(
            "The moderated evidence output failed schema validation"
        ) from None
    if (
        len(validated_records) != 1
        or validated_records[0].gate_id != GATE_ID
        or len(validated_files) != 1
        or len(declared) != 1
    ):
        raise Phase66ModeratedEvidenceError(
            "The moderated evidence output is not the exact gate fragment"
        )
    return outputs, {
        "artifact_count": 1,
        "deployment_digest": provider.deployment_digest,
        "job_digest": provider.job_digest,
        "record_digest": _digest(record_value),
        "result": "passed",
        "run_digest": provider.run_digest,
        "session_record_digest": observation_input.digest,
        "task_contract_digest": TASK_CONTRACT_SHA256,
    }


def prepare_phase66_moderated_evidence(
    *,
    closure_root: Path,
    task_contract_sha256: str,
    provider_records_path: Path,
    provider_records_sha256: str,
    provider_records_size: int,
    provider_artifact_files_path: Path,
    provider_artifact_files_sha256: str,
    provider_artifact_files_size: int,
    consent_path: Path,
    consent_sha256: str,
    consent_size: int,
    session_observation_path: Path,
    session_observation_sha256: str,
    session_observation_size: int,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> dict[str, object]:
    """Create one fresh, fully sanitized gate-11 fragment below ``closure_root``."""

    _require_non_root()
    _gate_authority()
    _read_task_contract(task_contract_sha256)
    closure = _private_path(closure_root)
    with _private_directory_descriptor(closure):
        pass
    output_root = closure / OUTPUT_DIRECTORY_NAME

    paths = tuple(
        _closure_child(path, closure)
        for path in (
            provider_records_path,
            provider_artifact_files_path,
            consent_path,
            session_observation_path,
        )
    )
    if (
        len(set(paths)) != len(paths)
        or paths[0].parent != paths[1].parent
        or any(path == output_root or output_root in path.parents for path in paths)
    ):
        raise Phase66ModeratedEvidenceError(
            "Moderated inputs must be distinct and outside the fresh output"
        )
    specs = (
        (paths[0], provider_records_sha256, provider_records_size),
        (paths[1], provider_artifact_files_sha256, provider_artifact_files_size),
        (paths[2], consent_sha256, consent_size),
        (paths[3], session_observation_sha256, session_observation_size),
    )
    inputs = tuple(
        _read_exact_private(
            path,
            closure_root=closure,
            expected_digest=digest,
            expected_size=size,
        )
        for path, digest, size in specs
    )
    if len({value.identity for value in inputs}) != len(inputs):
        raise Phase66ModeratedEvidenceError("Moderated evidence input inode reuse is forbidden")
    provider_records, provider_files, consent_input, observation_input = inputs
    provider = _validated_provider_fragment(
        records_input=provider_records,
        artifact_files_input=provider_files,
        fragment_root=paths[0].parent,
    )
    provider_record_digest = _digest(provider.model_dump(mode="json"))
    consent, observation, duration_seconds = _validate_human_inputs(
        consent_input=consent_input,
        observation_input=observation_input,
        provider=provider,
        provider_record_digest=provider_record_digest,
        now=clock(),
    )
    outputs, summary = _build_outputs(
        consent=consent,
        consent_input=consent_input,
        observation=observation,
        observation_input=observation_input,
        provider=provider,
        provider_record_digest=provider_record_digest,
        duration_seconds=duration_seconds,
    )
    with _fresh_output_directory(closure) as (created_root, output_descriptor):
        for filename in OUTPUT_FILENAMES:
            _write_once(output_descriptor, filename, outputs[filename])
        try:
            records = _validated_records(_strict_json(outputs[RECORDS_FILENAME]))
            files = _validated_artifact_files(_strict_json(outputs[ARTIFACT_FILES_FILENAME]))
            declared = _declared_artifacts(records)
            artifact_bytes = _verify_artifacts(declared, files, created_root)
        except ValueError:
            raise Phase66ModeratedEvidenceError(
                "The written moderated evidence failed byte verification"
            ) from None
        if artifact_bytes != records[0].artifacts[0].byte_count:
            raise Phase66ModeratedEvidenceError("The written moderated artifact byte count drifted")
        _verify_written_output(
            output_root=created_root,
            output_descriptor=output_descriptor,
            expected=outputs,
        )
    return summary


def _positive_size(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("value must be an integer") from None
    if not 1 <= parsed <= MAX_INPUT_BYTES:
        raise argparse.ArgumentTypeError("value is outside the accepted byte bound")
    return parsed


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--closure-root", required=True, type=Path)
    parser.add_argument("--task-contract-sha256", required=True)
    parser.add_argument("--provider-records", required=True, type=Path)
    parser.add_argument("--provider-records-sha256", required=True)
    parser.add_argument("--provider-records-size", required=True, type=_positive_size)
    parser.add_argument("--provider-artifact-files", required=True, type=Path)
    parser.add_argument("--provider-artifact-files-sha256", required=True)
    parser.add_argument("--provider-artifact-files-size", required=True, type=_positive_size)
    parser.add_argument("--consent", required=True, type=Path)
    parser.add_argument("--consent-sha256", required=True)
    parser.add_argument("--consent-size", required=True, type=_positive_size)
    parser.add_argument("--session-observation", required=True, type=Path)
    parser.add_argument("--session-observation-sha256", required=True)
    parser.add_argument("--session-observation-size", required=True, type=_positive_size)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    arguments = parser.parse_args(argv)
    try:
        summary = prepare_phase66_moderated_evidence(
            closure_root=arguments.closure_root,
            task_contract_sha256=arguments.task_contract_sha256,
            provider_records_path=arguments.provider_records,
            provider_records_sha256=arguments.provider_records_sha256,
            provider_records_size=arguments.provider_records_size,
            provider_artifact_files_path=arguments.provider_artifact_files,
            provider_artifact_files_sha256=arguments.provider_artifact_files_sha256,
            provider_artifact_files_size=arguments.provider_artifact_files_size,
            consent_path=arguments.consent,
            consent_sha256=arguments.consent_sha256,
            consent_size=arguments.consent_size,
            session_observation_path=arguments.session_observation,
            session_observation_sha256=arguments.session_observation_sha256,
            session_observation_size=arguments.session_observation_size,
        )
    except Phase66ModeratedEvidenceError as error:
        parser.error(str(error))
    print(_render(summary).decode("utf-8"), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
