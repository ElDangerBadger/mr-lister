"""Assemble the passed Phase 6.6 outbox/recovery smoke into closed evidence.

This offline, repository-confined normalizer accepts one exact v2 run gate, the two exact
v2 runner artifacts, and the exact passed upload-integrity execution prerequisite.  It
validates every immutable binding and writes a fresh four-file evidence fragment.  It has no
AWS, HTTP, browser, storage, job, workflow, or provider client and never rewrites its inputs.

Legacy v1 runner artifacts deliberately lack the shared execution authority required to prove
same-run origin and an operator-independent ``recorded_at``.  They are therefore rejected.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path
from typing import Annotated, Any, Final, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictInt,
    StringConstraints,
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
    AcceptanceEvidenceClass,
    AcceptanceOutcome,
    ArtifactFormat,
    ArtifactKind,
    DeployedNonDestructiveEvidenceRecord,
    phase66_acceptance_manifest,
    phase66_manifest_digest,
    validate_phase66_evidence,
)

REPOSITORY_ROOT: Final = Path(__file__).resolve().parents[1]
PRIVATE_WORKSPACE_ROOT: Final = REPOSITORY_ROOT / ".mr_lister_private" / "phase66-acceptance"

SOURCE_COMMIT: Final = "e130292db7124425840c2768a94475417f94f2e5"
SOURCE_COMMIT_DIGEST: Final = "40e7186ae67d9f6cd7ae630381ff8ed59c09afde0e2022d4b0a3ecbced2277cd"
GATE_ID: Final = "deployed.outbox_recovery_smoke"
MANIFEST_PREREQUISITE_GATE_ID: Final = "deployed.edge_auth_owner_smoke"
EXECUTION_PREREQUISITE_GATE_ID: Final = "deployed.upload_integrity_smoke"
GATE_CONTRACT: Final = "phase6.6-deployed-outbox-recovery-run-gate-v2"
EXECUTION_AUTHORITY_CONTRACT: Final = "phase6.6-deployed-outbox-recovery-execution-authority-v1"
RAW_CANARY_CONTRACT: Final = "phase6.6-deployed-outbox-recovery-canary-summary-v2"
RAW_LOG_CONTRACT: Final = "phase6.6-deployed-outbox-recovery-log-audit-v2"

CANARY_SUMMARY_FILENAME: Final = "canary_summary.json"
LOG_AUDIT_FILENAME: Final = "log_audit.json"
RECORDS_FILENAME: Final = "records.json"
ARTIFACT_FILES_FILENAME: Final = "artifact-files.json"
_OUTPUT_FILENAMES: Final = (
    CANARY_SUMMARY_FILENAME,
    LOG_AUDIT_FILENAME,
    RECORDS_FILENAME,
    ARTIFACT_FILES_FILENAME,
)

_EXPECTED_ASSERTIONS: Final = (
    "committed_work_is_recovered_by_sweep",
    "deterministic_execution_starts_once",
    "logical_work_is_not_duplicated",
    "stuck_execution_recovery_passes",
    "reference_aware_retention_sweep_passes",
    "privacy_scan_passes",
    "provider_call_count_is_zero",
)
_MAX_INPUT_BYTES: Final = 4 * 1024 * 1024
_MAX_EXECUTION_SECONDS: Final = 60 * 60
_MAX_SOURCE_VERSIONS: Final = 25
_UTC_TIMESTAMP: Final = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$")

type Digest = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
type CanonicalTimestamp = Annotated[
    str,
    StringConstraints(pattern=r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$"),
]


class Phase66OutboxRecoveryEvidenceError(RuntimeError):
    """One input, confinement, evidence, or immutable-output assertion failed."""


class _ClosedModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class _MethodAuthorization(_ClosedModel):
    browser_authority_not_used: Literal[True]
    exact_synthetic_row_cleanup: Literal[True]
    missing_job_prepare_dispatch: Literal[True]
    one_retained_standard_execution_accepted: Literal[True]
    raw_identity_retained: Literal[False]
    stream_quiescence_before_due_sweep: Literal[True]
    synthetic_cancelled_missing_execution_recovery: Literal[True]
    whole_inventory_referenced_and_pinned: Literal[True]


class _WriteBudget(_ClosedModel):
    agentcore_invocations: Literal[0]
    bedrock_invocations: Literal[0]
    browser_authority_reads: Literal[0]
    cognito_reads: Literal[0]
    direct_dispatcher_lambda_invocations: Literal[2]
    direct_execution_recovery_lambda_invocations: Literal[1]
    direct_retention_lambda_invocations: Literal[1]
    dynamodb_item_deletes: Literal[5]
    dynamodb_item_writes: Literal[15]
    dynamodb_new_items: Literal[5]
    dynamodb_transactions: Literal[3]
    logical_work_requests: Literal[2]
    provider_calls: Literal[0]
    provider_records: Literal[0]
    retention_checkpoint_writes: Literal[1]
    retention_versions_released: Literal[0]
    s3_object_deletes: Literal[0]
    s3_object_puts: Literal[0]
    s3_version_tag_writes: StrictInt = Field(ge=1, le=_MAX_SOURCE_VERSIONS)
    secretsmanager_reads: Literal[0]
    stepfunctions_executions_started: Literal[1]
    stepfunctions_executions_stopped: Literal[0]
    stepfunctions_retained_executions: Literal[1]


class _GateBaseline(_ClosedModel):
    application_record_count: StrictInt = Field(ge=1, le=10_000_000)
    application_record_digest: Digest
    existing_dispatched_work_count: Literal[0]
    existing_execution_count: StrictInt = Field(ge=0, le=10_000)
    existing_execution_digest: Digest
    provider_record_count: Literal[0]
    retention_checkpoint_present: Literal[True]
    retention_pinned_version_count: StrictInt = Field(ge=1, le=_MAX_SOURCE_VERSIONS)
    retention_referenced_version_count: StrictInt = Field(ge=1, le=_MAX_SOURCE_VERSIONS)
    retention_source_inventory_digest: Digest
    retention_source_version_count: StrictInt = Field(ge=1, le=_MAX_SOURCE_VERSIONS)
    running_execution_count: Literal[0]
    synthetic_namespace_absent: Literal[True]
    synthetic_namespace_seed: Digest

    @model_validator(mode="after")
    def inventory_is_wholly_referenced_and_pinned(self) -> _GateBaseline:
        if not (
            self.retention_pinned_version_count
            == self.retention_referenced_version_count
            == self.retention_source_version_count
        ):
            raise ValueError("Gate retention inventory is inconsistent")
        return self


class _RunGate(_ClosedModel):
    authorization_contract: Literal[GATE_CONTRACT]
    baseline: _GateBaseline
    deployment_digest: Digest
    exact_write_budget: _WriteBudget
    gate_id: Literal[GATE_ID]
    method_authorization: _MethodAuthorization
    prerequisite_evidence_run_digest: Digest
    source_authority_commit: Literal[SOURCE_COMMIT]
    source_authority_commit_digest: Literal[SOURCE_COMMIT_DIGEST]
    synthetic_namespace_seed: Digest

    @model_validator(mode="after")
    def dynamic_bindings_are_exact(self) -> _RunGate:
        if (
            self.synthetic_namespace_seed != self.baseline.synthetic_namespace_seed
            or self.exact_write_budget.s3_version_tag_writes
            != self.baseline.retention_source_version_count
        ):
            raise ValueError("Gate namespace or dynamic write budget is inconsistent")
        return self


class _Assertions(_ClosedModel):
    committed_work_is_recovered_by_sweep: Literal[True]
    deterministic_execution_starts_once: Literal[True]
    logical_work_is_not_duplicated: Literal[True]
    privacy_scan_passes: Literal[True]
    provider_call_count_is_zero: Literal[True]
    reference_aware_retention_sweep_passes: Literal[True]
    stuck_execution_recovery_passes: Literal[True]


class _Counts(_ClosedModel):
    direct_dispatcher_invocations: Literal[2]
    direct_execution_recovery_invocations: Literal[1]
    direct_retention_invocations: Literal[1]
    provider_calls: Literal[0]
    retained_standard_executions: Literal[1]
    synthetic_rows_remaining: Literal[0]


class _ExecutionAuthority(_ClosedModel):
    authority_contract: Literal[EXECUTION_AUTHORITY_CONTRACT]
    completed_at: CanonicalTimestamp
    execution_digest: Digest
    started_at: CanonicalTimestamp

    @model_validator(mode="after")
    def timestamps_are_ordered_and_bounded(self) -> _ExecutionAuthority:
        started_at = _parse_timestamp(self.started_at)
        completed_at = _parse_timestamp(self.completed_at)
        if completed_at < started_at or completed_at - started_at > timedelta(
            seconds=_MAX_EXECUTION_SECONDS
        ):
            raise ValueError("Execution timestamps are not ordered within the closed bound")
        return self


class _RawCanarySummary(_ClosedModel):
    artifact_contract: Literal[RAW_CANARY_CONTRACT]
    assertions: _Assertions
    counts: _Counts
    deployment_digest: Digest
    execution_authority: _ExecutionAuthority
    gate_digest: Digest
    prerequisite_evidence_run_digest: Digest
    redaction_verified: Literal[True]
    source_authority_commit_digest: Literal[SOURCE_COMMIT_DIGEST]
    status: Literal["passed"]


class _Deltas(_ClosedModel):
    agentcore_invocations: Literal[0]
    bedrock_invocations: Literal[0]
    dynamodb_application_records: Literal[0]
    provider_calls: Literal[0]
    provider_records: Literal[0]
    source_versions: Literal[0]
    workflow_executions: Literal[1]


class _RawLogAudit(_ClosedModel):
    artifact_contract: Literal[RAW_LOG_CONTRACT]
    deltas: _Deltas
    deployment_digest: Digest
    execution_authority: _ExecutionAuthority
    gate_digest: Digest
    prerequisite_evidence_run_digest: Digest
    raw_authority_retained: Literal[False]
    source_authority_commit_digest: Literal[SOURCE_COMMIT_DIGEST]
    status: Literal["passed"]


@dataclass(frozen=True, slots=True)
class _InputFile:
    payload: bytes
    digest: str
    byte_count: int
    identity: tuple[int, int]


def _canonical(value: object) -> bytes:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _render(value: object) -> bytes:
    return _canonical(value) + b"\n"


def _digest(value: object) -> str:
    return sha256(_canonical(value)).hexdigest()


def _parse_timestamp(value: str) -> datetime:
    if _UTC_TIMESTAMP.fullmatch(value) is None:
        raise ValueError("Timestamp is not canonical second-resolution UTC text")
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
        raise Phase66OutboxRecoveryEvidenceError("An evidence input is not strict JSON") from None


def _confined(path: Path) -> Path:
    candidate = Path(os.path.abspath(path))
    try:
        relative = candidate.relative_to(PRIVATE_WORKSPACE_ROOT)
    except ValueError:
        raise Phase66OutboxRecoveryEvidenceError(
            "Evidence paths must stay in the repository-private workspace"
        ) from None
    if not relative.parts:
        raise Phase66OutboxRecoveryEvidenceError("An evidence path must name a private child")
    return candidate


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
        raise Phase66OutboxRecoveryEvidenceError(
            "The repository root is not one stable directory chain"
        ) from None
    finally:
        if descriptor is not None:
            os.close(descriptor)


@contextmanager
def _private_directory_descriptor(path: Path) -> Iterator[int]:
    directory = Path(os.path.abspath(path))
    try:
        directory.relative_to(PRIVATE_WORKSPACE_ROOT)
    except ValueError:
        raise Phase66OutboxRecoveryEvidenceError(
            "Evidence paths must stay in the repository-private workspace"
        ) from None
    flags = os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY | os.O_NOFOLLOW
    descriptor: int | None = None
    try:
        descriptor = _open_repository_root()
        for component in directory.relative_to(REPOSITORY_ROOT).parts:
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
    except OSError:
        raise Phase66OutboxRecoveryEvidenceError(
            "A private evidence directory chain is not confined"
        ) from None
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _read_exact_input(
    path: Path,
    *,
    expected_digest: str,
    expected_size: int | None,
) -> _InputFile:
    if not re.fullmatch(r"[0-9a-f]{64}", expected_digest):
        raise Phase66OutboxRecoveryEvidenceError("An expected input SHA-256 is invalid")
    if expected_size is not None and not 1 <= expected_size <= _MAX_INPUT_BYTES:
        raise Phase66OutboxRecoveryEvidenceError("An expected input size is invalid")
    candidate = _confined(path)
    descriptor: int | None = None
    with _private_directory_descriptor(candidate.parent) as parent_descriptor:
        try:
            descriptor = os.open(
                candidate.name,
                os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW,
                dir_fd=parent_descriptor,
            )
            before = os.fstat(descriptor)
            if (
                not stat.S_ISREG(before.st_mode)
                or before.st_nlink != 1
                or before.st_mode & 0o077
                or not 1 <= before.st_size <= _MAX_INPUT_BYTES
                or (expected_size is not None and before.st_size != expected_size)
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
            raise Phase66OutboxRecoveryEvidenceError(
                "An evidence input is not one exact owner-only regular file"
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
        raise Phase66OutboxRecoveryEvidenceError("An evidence input changed during its read")
    payload = b"".join(chunks)
    actual_digest = sha256(payload).hexdigest()
    if not secrets.compare_digest(actual_digest, expected_digest):
        raise Phase66OutboxRecoveryEvidenceError("An evidence input SHA-256 does not match")
    return _InputFile(
        payload=payload,
        digest=actual_digest,
        byte_count=len(payload),
        identity=(before.st_dev, before.st_ino),
    )


@contextmanager
def _create_fresh_output_root(run_root: Path) -> Iterator[tuple[Path, int]]:
    candidate = _confined(run_root)
    with _private_directory_descriptor(candidate.parent) as parent_descriptor:
        try:
            os.mkdir(candidate.name, mode=0o700, dir_fd=parent_descriptor)
            flags = os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY | os.O_NOFOLLOW
            descriptor = os.open(candidate.name, flags, dir_fd=parent_descriptor)
        except OSError:
            raise Phase66OutboxRecoveryEvidenceError(
                "Evidence output root must be one fresh confined directory"
            ) from None
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISDIR(metadata.st_mode) or metadata.st_mode & 0o077:
            raise Phase66OutboxRecoveryEvidenceError("Evidence output root is not confined")
        yield candidate, descriptor
    finally:
        os.close(descriptor)


def _write_once(directory_descriptor: int, name: str, contents: bytes) -> None:
    if not name or name in {".", ".."} or "/" in name or "\x00" in name:
        raise Phase66OutboxRecoveryEvidenceError("An evidence output filename is invalid")
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
            stream.write(contents)
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
        raise Phase66OutboxRecoveryEvidenceError(
            "An immutable evidence output could not be written"
        ) from None
    finally:
        if descriptor is not None:
            os.close(descriptor)
        try:
            os.unlink(temporary, dir_fd=directory_descriptor)
        except FileNotFoundError:
            pass


def _gate_authority() -> Any:
    matches = tuple(gate for gate in phase66_acceptance_manifest().gates if gate.gate_id == GATE_ID)
    if len(matches) != 1 or matches[0].required_assertions != _EXPECTED_ASSERTIONS:
        raise Phase66OutboxRecoveryEvidenceError("The frozen outbox/recovery gate has drifted")
    if matches[0].required_artifact_kinds != (
        ArtifactKind.CANARY_SUMMARY,
        ArtifactKind.LOG_AUDIT,
    ):
        raise Phase66OutboxRecoveryEvidenceError(
            "The frozen outbox/recovery artifact contract has drifted"
        )
    if matches[0].prerequisites != (MANIFEST_PREREQUISITE_GATE_ID,):
        raise Phase66OutboxRecoveryEvidenceError(
            "The frozen outbox/recovery acceptance prerequisite has drifted"
        )
    return matches[0]


def _validate_source_authority(source_commit: str, source_commit_digest: str) -> None:
    if (
        source_commit != SOURCE_COMMIT
        or source_commit_digest != SOURCE_COMMIT_DIGEST
        or sha256(source_commit.encode("ascii")).hexdigest() != source_commit_digest
    ):
        raise Phase66OutboxRecoveryEvidenceError("The exact Phase 6 source authority is required")


def _derived_digest(kind: str, run_digest: str, namespace_seed: str) -> str:
    return sha256(
        b"\0".join(
            (
                b"phase6.6-outbox-recovery-scoped-digest-v1",
                kind.encode("ascii"),
                run_digest.encode("ascii"),
                namespace_seed.encode("ascii"),
            )
        )
    ).hexdigest()


def _validated_inputs(
    gate_value: object,
    canary_value: object,
    log_value: object,
    prerequisite_value: object,
    *,
    gate_digest: str,
    now: datetime,
) -> tuple[_RunGate, _RawCanarySummary, _RawLogAudit, DeployedNonDestructiveEvidenceRecord]:
    try:
        gate = _RunGate.model_validate(gate_value)
        canary = _RawCanarySummary.model_validate(canary_value)
        log = _RawLogAudit.model_validate(log_value)
    except ValueError:
        raise Phase66OutboxRecoveryEvidenceError(
            "Outbox/recovery authorities do not match their closed contracts"
        ) from None
    if not isinstance(prerequisite_value, list) or len(prerequisite_value) != 1:
        raise Phase66OutboxRecoveryEvidenceError(
            "Execution-prerequisite evidence must contain exactly one upload record"
        )
    try:
        prerequisite = validate_phase66_evidence(prerequisite_value[0])
    except (TypeError, ValueError):
        raise Phase66OutboxRecoveryEvidenceError(
            "Execution-prerequisite upload evidence is invalid"
        ) from None
    if not isinstance(prerequisite, DeployedNonDestructiveEvidenceRecord):
        raise Phase66OutboxRecoveryEvidenceError(
            "Execution-prerequisite evidence is not deployed non-destructive evidence"
        )
    if (
        canary.gate_digest != gate_digest
        or log.gate_digest != gate_digest
        or canary.deployment_digest != gate.deployment_digest
        or log.deployment_digest != gate.deployment_digest
        or canary.prerequisite_evidence_run_digest != gate.prerequisite_evidence_run_digest
        or log.prerequisite_evidence_run_digest != gate.prerequisite_evidence_run_digest
        or canary.source_authority_commit_digest != gate.source_authority_commit_digest
        or log.source_authority_commit_digest != gate.source_authority_commit_digest
    ):
        raise Phase66OutboxRecoveryEvidenceError(
            "Outbox/recovery gate, deployment, source, or prerequisite bindings disagree"
        )
    if canary.execution_authority != log.execution_authority:
        raise Phase66OutboxRecoveryEvidenceError(
            "Outbox/recovery artifacts do not share one execution authority"
        )
    if (
        prerequisite.gate_id != EXECUTION_PREREQUISITE_GATE_ID
        or prerequisite.outcome is not AcceptanceOutcome.PASSED
        or prerequisite.run_digest != gate.prerequisite_evidence_run_digest
        or prerequisite.deployment_digest != gate.deployment_digest
        or prerequisite.source_commit_digest != SOURCE_COMMIT_DIGEST
    ):
        raise Phase66OutboxRecoveryEvidenceError(
            "The exact passed upload execution prerequisite is not closed"
        )
    if not isinstance(now, datetime) or now.utcoffset() is None:
        raise Phase66OutboxRecoveryEvidenceError("Evidence assembly clock must be timezone-aware")
    execution_started_at = _parse_timestamp(canary.execution_authority.started_at)
    execution_completed_at = _parse_timestamp(canary.execution_authority.completed_at)
    if execution_completed_at > now.astimezone(UTC):
        raise Phase66OutboxRecoveryEvidenceError(
            "Outbox/recovery execution completion is in the future"
        )
    if execution_started_at < prerequisite.recorded_at:
        raise Phase66OutboxRecoveryEvidenceError(
            "Outbox/recovery execution predates its upload execution prerequisite"
        )
    return gate, canary, log, prerequisite


def _build_outputs(
    *,
    gate: _RunGate,
    gate_input: _InputFile,
    canary: _RawCanarySummary,
    canary_input: _InputFile,
    log: _RawLogAudit,
    log_input: _InputFile,
    prerequisite: DeployedNonDestructiveEvidenceRecord,
    prerequisite_input: _InputFile,
) -> tuple[dict[str, bytes], dict[str, object]]:
    frozen_gate = _gate_authority()
    manifest_digest = phase66_manifest_digest()
    execution = canary.execution_authority
    recorded_at = execution.completed_at
    run_digest = _digest(
        {
            "contract": "phase6.6-outbox-recovery-evidence-run-v2",
            "deployment_digest": gate.deployment_digest,
            "execution_authority": execution.model_dump(mode="json"),
            "gate_byte_count": gate_input.byte_count,
            "gate_digest": gate_input.digest,
            "prerequisite_evidence_run_digest": prerequisite.run_digest,
            "prerequisite_records_byte_count": prerequisite_input.byte_count,
            "prerequisite_records_digest": prerequisite_input.digest,
            "raw_canary_summary_byte_count": canary_input.byte_count,
            "raw_canary_summary_digest": canary_input.digest,
            "raw_log_audit_byte_count": log_input.byte_count,
            "raw_log_audit_digest": log_input.digest,
            "recorded_at": recorded_at,
            "source_commit_digest": SOURCE_COMMIT_DIGEST,
        }
    )
    actor_digest = _derived_digest("actor", run_digest, gate.synthetic_namespace_seed)
    job_digest = _derived_digest("job", run_digest, gate.synthetic_namespace_seed)
    common = {
        "deployment_digest": gate.deployment_digest,
        "execution_digest": execution.execution_digest,
        "execution_started_at": execution.started_at,
        "gate": GATE_ID,
        "manifest_digest": manifest_digest,
        "prerequisite_evidence_run_digest": prerequisite.run_digest,
        "recorded_at": recorded_at,
        "result": "passed",
        "run_digest": run_digest,
        "source_commit_digest": SOURCE_COMMIT_DIGEST,
        "source_gate_digest": gate_input.digest,
    }
    canary_document = {
        **common,
        "artifact_contract": "phase6.6-sanitized-outbox-recovery-canary-summary-v2",
        "assertions": canary.assertions.model_dump(mode="json"),
        "counts": canary.counts.model_dump(mode="json"),
        "raw_canary_summary_byte_count": canary_input.byte_count,
        "raw_canary_summary_digest": canary_input.digest,
        "redaction_verified": True,
    }
    log_document = {
        **common,
        "artifact_contract": "phase6.6-sanitized-outbox-recovery-log-audit-v2",
        "deltas": log.deltas.model_dump(mode="json"),
        "forbidden_field_match_count": 0,
        "free_text_value_count": 0,
        "raw_authority_retained": False,
        "raw_log_audit_byte_count": log_input.byte_count,
        "raw_log_audit_digest": log_input.digest,
        "sensitive_value_match_count": 0,
    }
    artifact_documents = (
        (ArtifactKind.CANARY_SUMMARY, CANARY_SUMMARY_FILENAME, canary_document),
        (ArtifactKind.LOG_AUDIT, LOG_AUDIT_FILENAME, log_document),
    )
    outputs: dict[str, bytes] = {}
    artifact_evidence: list[dict[str, object]] = []
    artifact_files: list[dict[str, object]] = []
    for kind, filename, document in artifact_documents:
        contents = _render(document)
        artifact_digest = sha256(contents).hexdigest()
        outputs[filename] = contents
        artifact_evidence.append(
            {
                "artifact_digest": artifact_digest,
                "artifact_format": ArtifactFormat.JSON.value,
                "byte_count": len(contents),
                "kind": kind.value,
                "redaction_verified": True,
            }
        )
        artifact_files.append(
            Phase66ArtifactFile.model_validate(
                {
                    "artifact_digest": artifact_digest,
                    "artifact_format": ArtifactFormat.JSON,
                    "kind": kind,
                    "relative_path": filename,
                }
            ).model_dump(mode="json")
        )

    assertion_values = canary.assertions.model_dump(mode="json")
    observed_counts = {assertion_id: 1 for assertion_id in _EXPECTED_ASSERTIONS}
    observed_counts["provider_call_count_is_zero"] = 0
    assertions = [
        {
            "assertion_id": assertion_id,
            "observation_digest": _digest(
                {
                    "assertion_id": assertion_id,
                    "canary_summary_digest": canary_input.digest,
                    "log_audit_digest": log_input.digest,
                    "observed_count": observed_counts[assertion_id],
                    "passed": assertion_values[assertion_id],
                    "run_digest": run_digest,
                }
            ),
            "observed_count": observed_counts[assertion_id],
            "passed": True,
        }
        for assertion_id in frozen_gate.required_assertions
    ]
    record = validate_phase66_evidence(
        {
            "actor_digests": [actor_digest],
            "artifacts": artifact_evidence,
            "assertions": assertions,
            "correlation_digest": None,
            "deployment_digest": gate.deployment_digest,
            "evidence_class": AcceptanceEvidenceClass.DEPLOYED_NON_DESTRUCTIVE.value,
            "gate_id": GATE_ID,
            "job_digest": job_digest,
            "manifest_digest": manifest_digest,
            "moderated_session": None,
            "outcome": AcceptanceOutcome.PASSED.value,
            "privacy": {
                "forbidden_field_match_count": 0,
                "free_text_value_count": 0,
                "sanitizer_contract": "phase6.6-sanitized-evidence-v1",
                "sensitive_value_match_count": 0,
            },
            "provider_call_summary": None,
            "provider_gate_attestation": None,
            "recorded_at": recorded_at,
            "run_digest": run_digest,
            "schema_version": "6.6.0",
            "source_commit_digest": SOURCE_COMMIT_DIGEST,
            "work_digest": None,
        }
    )
    record_value = record.model_dump(mode="json")
    outputs[RECORDS_FILENAME] = _render([record_value])
    outputs[ARTIFACT_FILES_FILENAME] = _render(artifact_files)
    if set(outputs) != set(_OUTPUT_FILENAMES):
        raise Phase66OutboxRecoveryEvidenceError("The closed evidence output set drifted")
    return outputs, {
        "artifact_count": len(artifact_documents),
        "deployment_digest": gate.deployment_digest,
        "execution_digest": execution.execution_digest,
        "record_digest": _digest(record_value),
        "result": "passed",
        "run_digest": run_digest,
    }


def prepare_phase66_outbox_recovery_evidence(
    *,
    run_root: Path,
    source_commit: str,
    source_commit_digest: str,
    gate_path: Path,
    gate_sha256: str,
    canary_summary_path: Path,
    canary_summary_sha256: str,
    canary_summary_size: int,
    log_audit_path: Path,
    log_audit_sha256: str,
    log_audit_size: int,
    prerequisite_records_path: Path,
    prerequisite_records_sha256: str,
    prerequisite_records_size: int,
) -> dict[str, object]:
    """Validate exact authorities and create one fresh normalized evidence fragment."""

    _validate_source_authority(source_commit, source_commit_digest)
    output_root = _confined(run_root)
    input_specs = (
        (gate_path, gate_sha256, None),
        (canary_summary_path, canary_summary_sha256, canary_summary_size),
        (log_audit_path, log_audit_sha256, log_audit_size),
        (
            prerequisite_records_path,
            prerequisite_records_sha256,
            prerequisite_records_size,
        ),
    )
    input_paths = tuple(_confined(path) for path, _, _ in input_specs)
    if len(set(input_paths)) != len(input_paths) or any(
        path == output_root or output_root in path.parents for path in input_paths
    ):
        raise Phase66OutboxRecoveryEvidenceError(
            "Evidence inputs must be distinct and outside the fresh output root"
        )
    inputs = tuple(
        _read_exact_input(path, expected_digest=digest, expected_size=size)
        for path, digest, size in input_specs
    )
    if len({source.identity for source in inputs}) != len(inputs):
        raise Phase66OutboxRecoveryEvidenceError("Evidence input inode reuse is forbidden")
    gate_input, canary_input, log_input, prerequisite_input = inputs
    gate, canary, log, prerequisite = _validated_inputs(
        _strict_json(gate_input.payload),
        _strict_json(canary_input.payload),
        _strict_json(log_input.payload),
        _strict_json(prerequisite_input.payload),
        gate_digest=gate_input.digest,
        now=datetime.now(UTC),
    )
    outputs, summary = _build_outputs(
        gate=gate,
        gate_input=gate_input,
        canary=canary,
        canary_input=canary_input,
        log=log,
        log_input=log_input,
        prerequisite=prerequisite,
        prerequisite_input=prerequisite_input,
    )
    with _create_fresh_output_root(output_root) as (output_root, output_descriptor):
        for filename in _OUTPUT_FILENAMES:
            _write_once(output_descriptor, filename, outputs[filename])

        records_value = _strict_json(outputs[RECORDS_FILENAME])
        files_value = _strict_json(outputs[ARTIFACT_FILES_FILENAME])
        if not isinstance(records_value, list) or not isinstance(files_value, list):
            raise Phase66OutboxRecoveryEvidenceError("Evidence indexes are not exact arrays")
        try:
            records = _validated_records(records_value)
            declared = _declared_artifacts(records)
            files = _validated_artifact_files(files_value)
            artifact_bytes = _verify_artifacts(declared, files, output_root)
        except ValueError:
            raise Phase66OutboxRecoveryEvidenceError(
                "Normalized outbox/recovery evidence failed byte verification"
            ) from None
        if (
            len(records) != 1
            or records[0].gate_id != GATE_ID
            or len(declared) != 2
            or artifact_bytes != sum(item.byte_count for item in records[0].artifacts)
        ):
            raise Phase66OutboxRecoveryEvidenceError(
                "Normalized outbox/recovery evidence is not the exact fragment"
            )
    return summary


def _positive_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("value must be an integer") from None
    if not 1 <= parsed <= _MAX_INPUT_BYTES:
        raise argparse.ArgumentTypeError("value is outside the accepted byte bound")
    return parsed


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-root", required=True, type=Path)
    parser.add_argument("--source-commit", required=True)
    parser.add_argument("--source-commit-digest", required=True)
    parser.add_argument("--gate", required=True, type=Path)
    parser.add_argument("--gate-sha256", required=True)
    parser.add_argument("--canary-summary", required=True, type=Path)
    parser.add_argument("--canary-summary-sha256", required=True)
    parser.add_argument("--canary-summary-size", required=True, type=_positive_int)
    parser.add_argument("--log-audit", required=True, type=Path)
    parser.add_argument("--log-audit-sha256", required=True)
    parser.add_argument("--log-audit-size", required=True, type=_positive_int)
    parser.add_argument("--prerequisite-records", required=True, type=Path)
    parser.add_argument("--prerequisite-records-sha256", required=True)
    parser.add_argument("--prerequisite-records-size", required=True, type=_positive_int)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    arguments = parser.parse_args(argv)
    try:
        summary = prepare_phase66_outbox_recovery_evidence(
            run_root=arguments.run_root,
            source_commit=arguments.source_commit,
            source_commit_digest=arguments.source_commit_digest,
            gate_path=arguments.gate,
            gate_sha256=arguments.gate_sha256,
            canary_summary_path=arguments.canary_summary,
            canary_summary_sha256=arguments.canary_summary_sha256,
            canary_summary_size=arguments.canary_summary_size,
            log_audit_path=arguments.log_audit,
            log_audit_sha256=arguments.log_audit_sha256,
            log_audit_size=arguments.log_audit_size,
            prerequisite_records_path=arguments.prerequisite_records,
            prerequisite_records_sha256=arguments.prerequisite_records_sha256,
            prerequisite_records_size=arguments.prerequisite_records_size,
        )
    except Phase66OutboxRecoveryEvidenceError as error:
        parser.error(str(error))
    print(_render(summary).decode("utf-8"), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
