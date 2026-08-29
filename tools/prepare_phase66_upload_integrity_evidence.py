"""Assemble the passed Phase 6.6 upload-integrity smoke into closed evidence.

This is an offline, repository-confined normalization step.  It accepts the exact private run
gate, raw canary summary, raw log audit, and prerequisite edge record; validates every binding;
and writes a fresh four-file evidence fragment.  It has no AWS, HTTP, browser, storage, job, or
provider client.

The original smoke artifacts are immutable authorities.  They use ``status`` because they are
runner outputs, while the frozen evidence verifier requires sanitized JSON artifacts to use
``result``.  This tool therefore emits new normalized artifacts and never rewrites its inputs.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import stat
from dataclasses import dataclass
from datetime import UTC, datetime
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
GATE_ID: Final = "deployed.upload_integrity_smoke"
PREREQUISITE_GATE_ID: Final = "deployed.edge_auth_owner_smoke"
GATE_CONTRACT: Final = "phase6.6-deployed-upload-integrity-run-gate-v1"
RAW_CANARY_CONTRACT: Final = "phase6.6-deployed-upload-integrity-canary-summary-v1"
RAW_LOG_CONTRACT: Final = "phase6.6-deployed-upload-integrity-log-audit-v1"

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
    "expired_upload_grant_is_rejected",
    "modified_upload_grant_is_rejected",
    "wrong_artwork_bytes_are_rejected",
    "preview_binds_exact_version",
    "post_finalize_overwrite_cannot_change_preview",
    "provider_call_count_is_zero",
)
_MAX_INPUT_BYTES: Final = 4 * 1024 * 1024
_UTC_TIMESTAMP: Final = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$")

type Digest = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]


class Phase66UploadIntegrityEvidenceError(RuntimeError):
    """One input, confinement, evidence, or immutable-output assertion failed."""


class _ClosedModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class _MethodAuthorization(_ClosedModel):
    browser_authority_not_used: Literal[True]
    direct_review_lambda_invocations: Literal[2]
    direct_upload_lambda_invocations: Literal[1]
    ephemeral_cognito_group_read: Literal[True]
    ephemeral_cognito_list_users: Literal[True]
    raw_identity_retained: Literal[False]


class _WriteBudget(_ClosedModel):
    agentcore_invocations: Literal[0]
    bedrock_invocations: Literal[0]
    cancel_upload_requests: Literal[0]
    complete_upload_requests: Literal[0]
    create_upload_requests: Literal[1]
    dynamodb_item_writes: Literal[2]
    dynamodb_new_items: Literal[2]
    dynamodb_transactions: Literal[1]
    new_jobs: Literal[0]
    new_work_requests: Literal[0]
    provider_calls: Literal[0]
    provider_records: Literal[0]
    reauthorize_upload_requests: Literal[0]
    s3_negative_post_attempts: Literal[3]
    s3_negative_post_persisted_versions: Literal[0]
    s3_temporary_exact_version_deletes: Literal[1]
    s3_temporary_overwrite_puts: Literal[1]
    s3_total_new_version_ceiling: Literal[1]
    s3_version_net_delta_after_cleanup: Literal[0]
    stepfunctions_executions: Literal[0]


class _PrimaryCanary(_ClosedModel):
    byte_count: Literal[5_242_880]
    sha256: Literal["d32bfa718ba9073db3da4e9aefb995212e46215d880e17b1dedc241f496691cc"]


class _WrongCanary(_ClosedModel):
    byte_count: Literal[5_242_880]
    mutation: Literal["xor_0x01_at_zero_based_file_offset_1048576"]
    sha256: Literal["8bc2aa2e193cab8956f8626e04e76c80cb08744fd3deb87b26a376212f6b19a2"]


class _OverwriteCanary(_ClosedModel):
    byte_count: Literal[10_702]
    sha256: Literal["12d15003d1bb881397a278592be424b4160356db2baa67f5b435df9e89a64a8e"]


class _Canaries(_ClosedModel):
    overwrite: _OverwriteCanary
    primary: _PrimaryCanary
    wrong_bytes: _WrongCanary


class _GateBaseline(_ClosedModel):
    actor_digest: Digest
    bucket_versioning_enabled: Literal[True]
    existing_job_count: StrictInt = Field(ge=1, le=1_000_000)
    existing_job_set_digest: Digest
    existing_job_states: list[Literal["failed_retryable"]] = Field(
        min_length=1, max_length=1_000_000
    )
    provider_record_count: Literal[0]
    running_execution_count: Literal[0]
    selected_inventory_count: Literal[1]
    selected_inventory_digest: Digest
    selected_job_digest: Digest
    selected_job_record_digest: Digest
    selected_object_coordinate_digest: Digest
    selected_pinned_is_latest: Literal[True]
    selected_pinned_version_digest: Digest
    selected_source_authority_digest: Digest
    selected_source_record_digest: Digest
    selected_version_head_matches_exact_canary: Literal[True]
    selected_version_tag_is_pinned: Literal[True]
    table_record_count: StrictInt = Field(ge=1, le=10_000_000)

    @model_validator(mode="after")
    def state_count_matches_inventory(self) -> _GateBaseline:
        if len(self.existing_job_states) != self.existing_job_count:
            raise ValueError("Gate job-state inventory is inconsistent")
        return self


class _RunGate(_ClosedModel):
    alternate_method_authorization: _MethodAuthorization
    authorization_contract: Literal[GATE_CONTRACT]
    baseline: _GateBaseline
    canaries: _Canaries
    deployment_digest: Digest
    exact_write_budget: _WriteBudget
    gate_id: Literal[GATE_ID]
    prerequisite_evidence_run_digest: Digest
    source_authority_commit: Literal[SOURCE_COMMIT]
    source_authority_commit_digest: Literal[SOURCE_COMMIT_DIGEST]


class _Assertions(_ClosedModel):
    expired_upload_grant_is_rejected: Literal[True]
    modified_upload_grant_is_rejected: Literal[True]
    post_finalize_overwrite_cannot_change_preview: Literal[True]
    preview_binds_exact_version: Literal[True]
    provider_call_count_is_zero: Literal[True]
    wrong_artwork_bytes_are_rejected: Literal[True]


class _Counts(_ClosedModel):
    create_upload_requests: Literal[1]
    direct_review_lambda_invocations: Literal[2]
    dynamodb_new_items: Literal[2]
    negative_s3_posts: Literal[3]
    persisted_reserved_versions: Literal[0]
    temporary_exact_version_deletes: Literal[1]
    temporary_overwrite_puts: Literal[1]


class _RawCanarySummary(_ClosedModel):
    artifact_contract: Literal[RAW_CANARY_CONTRACT]
    assertions: _Assertions
    counts: _Counts
    deployment_digest: Digest
    gate_digest: Digest
    prerequisite_evidence_run_digest: Digest
    redaction_verified: Literal[True]
    source_authority_commit_digest: Literal[SOURCE_COMMIT_DIGEST]
    status: Literal["passed"]


class _ZeroDeltas(_ClosedModel):
    agentcore_invocations: Literal[0]
    bedrock_invocations: Literal[0]
    jobs: Literal[0]
    provider_calls: Literal[0]
    provider_records: Literal[0]
    source_artifacts: Literal[0]
    work_requests: Literal[0]
    workflow_executions: Literal[0]


class _RawLogAudit(_ClosedModel):
    artifact_contract: Literal[RAW_LOG_CONTRACT]
    deltas: _ZeroDeltas
    deployment_digest: Digest
    gate_digest: Digest
    prerequisite_evidence_run_digest: Digest
    raw_authority_retained: Literal[False]
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
        raise Phase66UploadIntegrityEvidenceError("An evidence input is not strict JSON") from None


def _confined(path: Path) -> Path:
    candidate = Path(os.path.abspath(path))
    try:
        relative = candidate.relative_to(PRIVATE_WORKSPACE_ROOT)
    except ValueError:
        raise Phase66UploadIntegrityEvidenceError(
            "Evidence paths must stay in the repository-private workspace"
        ) from None
    if not relative.parts:
        raise Phase66UploadIntegrityEvidenceError("An evidence path must name a private child")
    return candidate


def _validate_private_parent(path: Path) -> None:
    candidate = _confined(path)
    current = REPOSITORY_ROOT
    for component in candidate.parent.relative_to(REPOSITORY_ROOT).parts:
        current /= component
        try:
            metadata = current.lstat()
        except OSError:
            raise Phase66UploadIntegrityEvidenceError(
                "A private evidence parent is unavailable"
            ) from None
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or stat.S_ISLNK(metadata.st_mode)
            or metadata.st_mode & 0o077
        ):
            raise Phase66UploadIntegrityEvidenceError("A private evidence parent is not confined")


def _read_exact_input(
    path: Path,
    *,
    expected_digest: str,
    expected_size: int | None,
) -> _InputFile:
    if not re.fullmatch(r"[0-9a-f]{64}", expected_digest):
        raise Phase66UploadIntegrityEvidenceError("An expected input SHA-256 is invalid")
    if expected_size is not None and not 1 <= expected_size <= _MAX_INPUT_BYTES:
        raise Phase66UploadIntegrityEvidenceError("An expected input size is invalid")
    candidate = _confined(path)
    _validate_private_parent(candidate)
    descriptor: int | None = None
    try:
        descriptor = os.open(candidate, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
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
        raise Phase66UploadIntegrityEvidenceError(
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
        raise Phase66UploadIntegrityEvidenceError("An evidence input changed during its read")
    payload = b"".join(chunks)
    actual_digest = sha256(payload).hexdigest()
    if not secrets.compare_digest(actual_digest, expected_digest):
        raise Phase66UploadIntegrityEvidenceError("An evidence input SHA-256 does not match")
    return _InputFile(
        payload=payload,
        digest=actual_digest,
        byte_count=len(payload),
        identity=(before.st_dev, before.st_ino),
    )


def _create_fresh_output_root(run_root: Path) -> Path:
    candidate = _confined(run_root)
    _validate_private_parent(candidate)
    if candidate.exists() or candidate.is_symlink():
        raise Phase66UploadIntegrityEvidenceError("Evidence output root must be fresh")
    try:
        candidate.mkdir(mode=0o700)
    except OSError:
        raise Phase66UploadIntegrityEvidenceError(
            "Evidence output root could not be created"
        ) from None
    metadata = candidate.lstat()
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or metadata.st_mode & 0o077
    ):
        raise Phase66UploadIntegrityEvidenceError("Evidence output root is not confined")
    return candidate


def _write_once(path: Path, contents: bytes) -> None:
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(12)}.tmp")
    descriptor: int | None = None
    try:
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
            0o600,
        )
        with os.fdopen(descriptor, "wb", closefd=True) as stream:
            descriptor = None
            stream.write(contents)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, path, follow_symlinks=False)
        temporary.unlink()
        path.chmod(0o600)
    except OSError:
        raise Phase66UploadIntegrityEvidenceError(
            "An immutable evidence output could not be written"
        ) from None
    finally:
        if descriptor is not None:
            os.close(descriptor)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _gate_authority() -> Any:
    matches = tuple(gate for gate in phase66_acceptance_manifest().gates if gate.gate_id == GATE_ID)
    if len(matches) != 1 or matches[0].required_assertions != _EXPECTED_ASSERTIONS:
        raise Phase66UploadIntegrityEvidenceError("The frozen upload-integrity gate has drifted")
    if matches[0].required_artifact_kinds != (
        ArtifactKind.CANARY_SUMMARY,
        ArtifactKind.LOG_AUDIT,
    ):
        raise Phase66UploadIntegrityEvidenceError(
            "The frozen upload-integrity artifact contract has drifted"
        )
    if matches[0].prerequisites != (PREREQUISITE_GATE_ID,):
        raise Phase66UploadIntegrityEvidenceError(
            "The frozen upload-integrity prerequisite has drifted"
        )
    return matches[0]


def _validate_source_authority(source_commit: str, source_commit_digest: str) -> None:
    if (
        source_commit != SOURCE_COMMIT
        or source_commit_digest != SOURCE_COMMIT_DIGEST
        or sha256(source_commit.encode("ascii")).hexdigest() != source_commit_digest
    ):
        raise Phase66UploadIntegrityEvidenceError("The exact Phase 6 source authority is required")


def _derived_digest(kind: str, run_digest: str, source_digest: str) -> str:
    return sha256(
        b"\0".join(
            (
                b"phase6.6-upload-integrity-scoped-digest-v1",
                kind.encode("ascii"),
                run_digest.encode("ascii"),
                source_digest.encode("ascii"),
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
    recorded_at: str,
) -> tuple[_RunGate, _RawCanarySummary, _RawLogAudit, DeployedNonDestructiveEvidenceRecord]:
    try:
        gate = _RunGate.model_validate(gate_value)
        canary = _RawCanarySummary.model_validate(canary_value)
        log = _RawLogAudit.model_validate(log_value)
    except ValueError:
        raise Phase66UploadIntegrityEvidenceError(
            "Upload-integrity authorities do not match their closed contracts"
        ) from None
    if not isinstance(prerequisite_value, list) or len(prerequisite_value) != 1:
        raise Phase66UploadIntegrityEvidenceError(
            "Prerequisite evidence must contain exactly the edge record"
        )
    try:
        prerequisite = validate_phase66_evidence(prerequisite_value[0])
    except (TypeError, ValueError):
        raise Phase66UploadIntegrityEvidenceError("Prerequisite edge evidence is invalid") from None
    if not isinstance(prerequisite, DeployedNonDestructiveEvidenceRecord):
        raise Phase66UploadIntegrityEvidenceError(
            "Prerequisite evidence is not deployed non-destructive evidence"
        )
    bindings = (
        gate_digest,
        gate.deployment_digest,
        gate.prerequisite_evidence_run_digest,
    )
    if (
        canary.gate_digest != bindings[0]
        or log.gate_digest != bindings[0]
        or canary.deployment_digest != bindings[1]
        or log.deployment_digest != bindings[1]
        or canary.prerequisite_evidence_run_digest != bindings[2]
        or log.prerequisite_evidence_run_digest != bindings[2]
    ):
        raise Phase66UploadIntegrityEvidenceError(
            "Upload-integrity gate, deployment, or prerequisite bindings disagree"
        )
    if (
        prerequisite.gate_id != PREREQUISITE_GATE_ID
        or prerequisite.outcome is not AcceptanceOutcome.PASSED
        or prerequisite.run_digest != gate.prerequisite_evidence_run_digest
        or prerequisite.deployment_digest != gate.deployment_digest
        or prerequisite.source_commit_digest != SOURCE_COMMIT_DIGEST
    ):
        raise Phase66UploadIntegrityEvidenceError(
            "The exact passed edge prerequisite is not closed"
        )
    timestamp = _parse_timestamp(recorded_at)
    if timestamp < prerequisite.recorded_at:
        raise Phase66UploadIntegrityEvidenceError(
            "Upload-integrity evidence predates its edge prerequisite"
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
    recorded_at: str,
) -> tuple[dict[str, bytes], dict[str, object]]:
    frozen_gate = _gate_authority()
    manifest_digest = phase66_manifest_digest()
    run_digest = _digest(
        {
            "contract": "phase6.6-upload-integrity-evidence-run-v1",
            "deployment_digest": gate.deployment_digest,
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
    actor_digest = _derived_digest("actor", run_digest, gate.baseline.actor_digest)
    job_digest = _derived_digest("job", run_digest, gate.baseline.selected_job_digest)
    common = {
        "deployment_digest": gate.deployment_digest,
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
        "artifact_contract": "phase6.6-sanitized-upload-integrity-canary-summary-v1",
        "assertions": canary.assertions.model_dump(mode="json"),
        "counts": canary.counts.model_dump(mode="json"),
        "raw_canary_summary_byte_count": canary_input.byte_count,
        "raw_canary_summary_digest": canary_input.digest,
        "redaction_verified": True,
    }
    log_document = {
        **common,
        "artifact_contract": "phase6.6-sanitized-upload-integrity-log-audit-v1",
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

    observed_counts = {
        "expired_upload_grant_is_rejected": 1,
        "modified_upload_grant_is_rejected": 1,
        "wrong_artwork_bytes_are_rejected": 1,
        "preview_binds_exact_version": 1,
        "post_finalize_overwrite_cannot_change_preview": 1,
        "provider_call_count_is_zero": 0,
    }
    assertion_values = canary.assertions.model_dump(mode="json")
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
        raise Phase66UploadIntegrityEvidenceError("The closed evidence output set drifted")
    return outputs, {
        "artifact_count": len(artifact_documents),
        "deployment_digest": gate.deployment_digest,
        "record_digest": _digest(record_value),
        "result": "passed",
        "run_digest": run_digest,
    }


def prepare_phase66_upload_integrity_evidence(
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
    recorded_at: str,
) -> dict[str, object]:
    """Validate exact authorities and create one fresh normalized evidence fragment."""

    _validate_source_authority(source_commit, source_commit_digest)
    try:
        _parse_timestamp(recorded_at)
    except ValueError:
        raise Phase66UploadIntegrityEvidenceError(
            "Recorded-at must be canonical second-resolution UTC text"
        ) from None
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
        raise Phase66UploadIntegrityEvidenceError(
            "Evidence inputs must be distinct and outside the fresh output root"
        )
    inputs = tuple(
        _read_exact_input(path, expected_digest=digest, expected_size=size)
        for path, digest, size in input_specs
    )
    if len({source.identity for source in inputs}) != len(inputs):
        raise Phase66UploadIntegrityEvidenceError("Evidence input inode reuse is forbidden")
    gate_input, canary_input, log_input, prerequisite_input = inputs
    gate, canary, log, prerequisite = _validated_inputs(
        _strict_json(gate_input.payload),
        _strict_json(canary_input.payload),
        _strict_json(log_input.payload),
        _strict_json(prerequisite_input.payload),
        gate_digest=gate_input.digest,
        recorded_at=recorded_at,
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
        recorded_at=recorded_at,
    )
    output_root = _create_fresh_output_root(output_root)
    for filename in _OUTPUT_FILENAMES:
        _write_once(output_root / filename, outputs[filename])

    records_value = _strict_json(outputs[RECORDS_FILENAME])
    files_value = _strict_json(outputs[ARTIFACT_FILES_FILENAME])
    if not isinstance(records_value, list) or not isinstance(files_value, list):
        raise Phase66UploadIntegrityEvidenceError("Evidence indexes are not exact arrays")
    try:
        records = _validated_records(records_value)
        declared = _declared_artifacts(records)
        files = _validated_artifact_files(files_value)
        artifact_bytes = _verify_artifacts(declared, files, output_root)
    except ValueError:
        raise Phase66UploadIntegrityEvidenceError(
            "Normalized upload-integrity evidence failed byte verification"
        ) from None
    if (
        len(records) != 1
        or records[0].gate_id != GATE_ID
        or len(declared) != 2
        or artifact_bytes != sum(item.byte_count for item in records[0].artifacts)
    ):
        raise Phase66UploadIntegrityEvidenceError(
            "Normalized upload-integrity evidence is not the exact fragment"
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
    parser.add_argument("--recorded-at", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    arguments = parser.parse_args(argv)
    try:
        summary = prepare_phase66_upload_integrity_evidence(
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
            recorded_at=arguments.recorded_at,
        )
    except Phase66UploadIntegrityEvidenceError as error:
        parser.error(str(error))
    print(_render(summary).decode("utf-8"), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
