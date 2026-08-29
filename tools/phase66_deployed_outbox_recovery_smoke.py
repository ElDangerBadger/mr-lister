#!/usr/bin/env python3
"""Fail-closed runner for the deployed Phase 6.6 outbox/recovery smoke.

The default command is a repository-local gate preflight.  Live execution is possible only
with an exact mode-0600 private gate, its caller-supplied SHA-256, an explicit one-run
environment switch, and a repository-private output directory.

The live canary uses two synthetic namespaces derived from the gate digest.  One namespace
contains only an orphan PREPARE work row: the due-work sweep can start the deterministic
workflow, but the deployed preparation handler must fail on its first strong job read before
AgentCore is reachable.  The second namespace contains a stale, cancelled Job/Work pair whose
deterministic execution never existed; the recovery sweep must settle it from durable authority.
Every DynamoDB row created by this process is removed by exact-key, exact-payload cleanup.  The
single Standard Step Functions execution is intentionally retained as immutable audit evidence.

Before any canary write, the runner proves the exact deployed function code/configuration and
the exact PREPARE state-machine definition, requires zero pre-existing dispatched work, and
requires the complete bounded source-version inventory to be referenced and pinned.  The
retention invocation can therefore only reassert existing pinned tags and advance its checkpoint;
it cannot release an object version.  No provider, Bedrock, AgentCore, Secrets Manager, Cognito,
browser, session, cookie, or token client exists in this runner.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import stat
import time
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Final, Protocol

from mr_lister.control.dispatch import (
    deterministic_execution_name,
    execution_arn_for,
    work_input_fingerprint,
)
from mr_lister.control.dynamodb import _job_item, _work_item
from mr_lister.control.models import (
    ControlJobRecord,
    ControlJobState,
    WorkRequest,
    WorkRequestStatus,
    WorkType,
)

REPOSITORY_ROOT: Final = Path(__file__).resolve().parents[1]
PRIVATE_ROOT: Final = REPOSITORY_ROOT / ".mr_lister_private" / "phase66-acceptance"
PREPARE_DEFINITION_PATH: Final = (
    REPOSITORY_ROOT / "infra" / "phase6" / "statemachine" / "prepare.asl.json"
)

GATE_ID: Final = "deployed.outbox_recovery_smoke"
GATE_CONTRACT: Final = "phase6.6-deployed-outbox-recovery-run-gate-v1"
SOURCE_AUTHORITY_COMMIT: Final = "e130292db7124425840c2768a94475417f94f2e5"
SOURCE_AUTHORITY_COMMIT_DIGEST: Final = (
    "40e7186ae67d9f6cd7ae630381ff8ed59c09afde0e2022d4b0a3ecbced2277cd"
)
PREPARE_DEFINITION_SHA256: Final = (
    "c8ad39e393fa82e00d08d68aab684315167d5bed08e7bceb248bbef9f3826031"
)
SHARED_LAMBDA_CODE_SHA256: Final = "uvFStzLOhXS2ppJbrnq0/4ScG4PUE3B2xSxmglU/nUg="
SHARED_RELEASE_FINGERPRINT: Final = (
    "0c6211a5b0244e9c86d635e6c02e7bc49e5e948d68895b4aaa982c0b0b2e187b"
)

REGION: Final = "us-west-2"
ACCOUNT_ID: Final = "384627057108"
PROFILE: Final = "mr-lister-bootstrap"
STACK_NAME: Final = "mr-lister-phase6-dev"
LIVE_ENVIRONMENT_SWITCH: Final = "MR_LISTER_RUN_DEPLOYED_OUTBOX_RECOVERY_SMOKE"
LIVE_ENVIRONMENT_VALUE: Final = "I_ACCEPT_THE_EXACT_PRIVATE_GATE"
MAX_PRIVATE_BYTES: Final = 1024 * 1024
MAX_TABLE_ITEMS: Final = 10_000
MAX_SOURCE_VERSIONS: Final = 25
MAX_EXECUTION_WAIT_SECONDS: Final = 90
OUTBOX_STREAM_QUIESCENCE_SECONDS: Final = 20

_CHECKPOINT_ENTITY: Final = "SOURCE_VERSION_RETENTION_CHECKPOINT"
_EXPECTED_METHOD_AUTHORIZATION: Final = {
    "browser_authority_not_used": True,
    "exact_synthetic_row_cleanup": True,
    "missing_job_prepare_dispatch": True,
    "one_retained_standard_execution_accepted": True,
    "raw_identity_retained": False,
    "stream_quiescence_before_due_sweep": True,
    "synthetic_cancelled_missing_execution_recovery": True,
    "whole_inventory_referenced_and_pinned": True,
}
_FIXED_WRITE_BUDGET: Final = {
    "agentcore_invocations": 0,
    "bedrock_invocations": 0,
    "browser_authority_reads": 0,
    "cognito_reads": 0,
    "direct_dispatcher_lambda_invocations": 2,
    "direct_execution_recovery_lambda_invocations": 1,
    "direct_retention_lambda_invocations": 1,
    "dynamodb_item_deletes": 5,
    "dynamodb_item_writes": 15,
    "dynamodb_new_items": 5,
    "dynamodb_transactions": 3,
    "logical_work_requests": 2,
    "provider_calls": 0,
    "provider_records": 0,
    "retention_checkpoint_writes": 1,
    "retention_versions_released": 0,
    "s3_object_deletes": 0,
    "s3_object_puts": 0,
    "secretsmanager_reads": 0,
    "stepfunctions_executions_started": 1,
    "stepfunctions_executions_stopped": 0,
    "stepfunctions_retained_executions": 1,
}
_EXPECTED_FUNCTIONS: Final = {
    "DispatcherFunction": (
        "mr-lister-phase6-dev-dispatcher",
        "phase6_lambda.dispatcher_handler",
        120,
    ),
    "PreparationDispatchFunction": (
        "mr-lister-phase6-dev-preparation-dispatch",
        "phase6_lambda.preparation_dispatch_handler",
        600,
    ),
    "SettlementFunction": (
        "mr-lister-phase6-dev-settlement",
        "phase6_lambda.settlement_handler",
        120,
    ),
    "SourceVersionRetentionFunction": (
        "mr-lister-phase6-dev-source-retention",
        "phase6_lambda.source_version_retention_handler",
        300,
    ),
    "StuckExecutionRecoveryFunction": (
        "mr-lister-phase6-dev-execution-recovery",
        "phase6_lambda.stuck_execution_recovery_handler",
        120,
    ),
}
_SAFE_DISPATCH_STREAM_FILTER: Final = {
    "eventName": ["INSERT", "MODIFY"],
    "dynamodb": {"Keys": {"SK": {"S": [{"prefix": "WORK#"}]}}},
}


class SmokeError(RuntimeError):
    """One closed gate, deployment, observation, or cleanup assertion failed."""


def _canonical_json(value: object, *, pretty: bool = False) -> bytes:
    try:
        return (
            json.dumps(
                value,
                sort_keys=True,
                indent=2 if pretty else None,
                separators=None if pretty else (",", ":"),
                ensure_ascii=True,
                allow_nan=False,
            )
            + ("\n" if pretty else "")
        ).encode()
    except (OverflowError, TypeError, ValueError):
        raise SmokeError("strict JSON serialization failed") from None


def _digest_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _digest_text(value: str) -> str:
    return _digest_bytes(value.encode())


def _digest_json(value: object) -> str:
    return _digest_bytes(_canonical_json(value))


def _mapping(value: object, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or any(not isinstance(key, str) for key in value):
        raise SmokeError(f"{label} is not an exact JSON object")
    return value


def _reject_constant(_value: str) -> None:
    raise ValueError


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError
        result[key] = value
    return result


def _strict_json(payload: bytes, label: str) -> object:
    try:
        return json.loads(
            payload,
            parse_constant=_reject_constant,
            object_pairs_hook=_unique_object,
        )
    except (RecursionError, UnicodeDecodeError, ValueError, json.JSONDecodeError):
        raise SmokeError(f"{label} is not strict JSON") from None


def _exact_private_path(path: Path) -> Path:
    candidate = Path(os.path.abspath(path))
    try:
        candidate.relative_to(PRIVATE_ROOT)
    except ValueError:
        raise SmokeError(
            "private inputs and outputs must remain in the repository workspace"
        ) from None
    return candidate


def _validate_private_directory(path: Path, *, create: bool) -> Path:
    directory = _exact_private_path(path)
    current = REPOSITORY_ROOT
    for component in directory.relative_to(REPOSITORY_ROOT).parts:
        current /= component
        try:
            metadata = current.lstat()
        except FileNotFoundError:
            if not create:
                raise SmokeError("private directory is unavailable") from None
            try:
                current.mkdir(mode=0o700)
                metadata = current.lstat()
            except OSError:
                raise SmokeError("private directory could not be created") from None
        if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
            raise SmokeError("private path contains a non-directory component")
        if metadata.st_mode & 0o077:
            if not create:
                raise SmokeError("private directory permissions are not confined")
            try:
                current.chmod(0o700)
            except OSError:
                raise SmokeError("private directory permissions could not be confined") from None
    return directory


def _read_private_file(path: Path) -> bytes:
    candidate = _exact_private_path(path)
    _validate_private_directory(candidate.parent, create=False)
    descriptor: int | None = None
    try:
        descriptor = os.open(candidate, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or before.st_mode & 0o077
            or not 1 <= before.st_size <= MAX_PRIVATE_BYTES
        ):
            raise OSError
        chunks: list[bytes] = []
        remaining = before.st_size
        while remaining:
            chunk = os.read(descriptor, min(remaining, 1024 * 1024))
            if not chunk:
                raise OSError
            chunks.append(chunk)
            remaining -= len(chunk)
        payload = b"".join(chunks)
        after = os.fstat(descriptor)
    except OSError:
        raise SmokeError("gate must be one stable mode-0600 private regular file") from None
    finally:
        if descriptor is not None:
            os.close(descriptor)
    if len(payload) != before.st_size or (
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
        raise SmokeError("gate changed while it was read")
    return payload


def _atomic_private_json(path: Path, value: object) -> tuple[int, str]:
    candidate = _exact_private_path(path)
    _validate_private_directory(candidate.parent, create=True)
    payload = _canonical_json(value, pretty=True)
    temporary = candidate.with_name(f".{candidate.name}.{secrets.token_hex(12)}.tmp")
    descriptor: int | None = None
    try:
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
            0o600,
        )
        with os.fdopen(descriptor, "wb", closefd=True) as output:
            descriptor = None
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
        temporary.replace(candidate)
        candidate.chmod(0o600)
    except OSError:
        raise SmokeError("sanitized result could not be written") from None
    finally:
        if descriptor is not None:
            os.close(descriptor)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
    return len(payload), _digest_bytes(payload)


def _is_digest(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


@dataclass(frozen=True, slots=True)
class RunGate:
    digest: str
    document: Mapping[str, Any] = field(repr=False)

    @property
    def baseline(self) -> Mapping[str, Any]:
        return _mapping(self.document.get("baseline"), "gate baseline")

    @property
    def deployment_digest(self) -> str:
        value = self.document.get("deployment_digest")
        assert isinstance(value, str)
        return value

    @property
    def prerequisite_digest(self) -> str:
        value = self.document.get("prerequisite_evidence_run_digest")
        assert isinstance(value, str)
        return value


def load_run_gate(path: Path, expected_digest: str) -> RunGate:
    if not _is_digest(expected_digest):
        raise SmokeError("gate SHA-256 is invalid")
    payload = _read_private_file(path)
    if not secrets.compare_digest(_digest_bytes(payload), expected_digest):
        raise SmokeError("gate SHA-256 does not match the exact private file")
    document = _mapping(_strict_json(payload, "gate"), "gate")
    if (
        document.get("gate_id") != GATE_ID
        or document.get("authorization_contract") != GATE_CONTRACT
    ):
        raise SmokeError("gate does not authorize this exact smoke")
    if document.get("source_authority_commit") != SOURCE_AUTHORITY_COMMIT:
        raise SmokeError("gate source authority commit is not the deployed code authority")
    if document.get("source_authority_commit_digest") != SOURCE_AUTHORITY_COMMIT_DIGEST:
        raise SmokeError("gate source authority digest is not the deployed code authority")
    for name in ("deployment_digest", "prerequisite_evidence_run_digest"):
        if not _is_digest(document.get(name)):
            raise SmokeError("gate deployment/prerequisite binding is invalid")
    if _mapping(document.get("method_authorization"), "method authorization") != (
        _EXPECTED_METHOD_AUTHORIZATION
    ):
        raise SmokeError("gate does not authorize the exact synthetic method")
    baseline = _mapping(document.get("baseline"), "gate baseline")
    required_baseline = {
        "application_record_count",
        "application_record_digest",
        "existing_dispatched_work_count",
        "existing_execution_count",
        "existing_execution_digest",
        "provider_record_count",
        "retention_checkpoint_present",
        "retention_pinned_version_count",
        "retention_referenced_version_count",
        "retention_source_inventory_digest",
        "retention_source_version_count",
        "running_execution_count",
        "synthetic_namespace_absent",
    }
    if set(baseline) != required_baseline:
        raise SmokeError("gate baseline is not the exact closed object")
    count_keys = {
        "application_record_count",
        "existing_dispatched_work_count",
        "existing_execution_count",
        "provider_record_count",
        "retention_pinned_version_count",
        "retention_referenced_version_count",
        "retention_source_version_count",
        "running_execution_count",
    }
    for key in count_keys:
        if type(baseline.get(key)) is not int or baseline[key] < 0:
            raise SmokeError("gate baseline count is invalid")
    for key in (
        "application_record_digest",
        "existing_execution_digest",
        "retention_source_inventory_digest",
    ):
        if not _is_digest(baseline.get(key)):
            raise SmokeError("gate baseline digest is invalid")
    if (
        baseline.get("provider_record_count") != 0
        or baseline.get("existing_dispatched_work_count") != 0
        or baseline.get("running_execution_count") != 0
        or baseline.get("retention_checkpoint_present") is not True
        or baseline.get("synthetic_namespace_absent") is not True
        or not 1 <= baseline["retention_source_version_count"] <= MAX_SOURCE_VERSIONS
        or baseline["retention_referenced_version_count"]
        != baseline["retention_source_version_count"]
        or baseline["retention_pinned_version_count"] != baseline["retention_source_version_count"]
    ):
        raise SmokeError("gate baseline does not authorize a provider-zero bounded run")
    budget = _mapping(document.get("exact_write_budget"), "write budget")
    expected_budget = {
        **_FIXED_WRITE_BUDGET,
        "s3_version_tag_writes": baseline["retention_source_version_count"],
    }
    if budget != expected_budget:
        raise SmokeError("gate write budget is not exact")
    return RunGate(digest=expected_digest, document=document)


@dataclass(frozen=True, slots=True)
class CanaryAuthority:
    outbox_owner_id: str = field(repr=False)
    outbox_job_id: str = field(repr=False)
    outbox_work_id: str = field(repr=False)
    outbox_receipt_id: str = field(repr=False)
    recovery_owner_id: str = field(repr=False)
    recovery_job_id: str = field(repr=False)
    recovery_work_id: str = field(repr=False)
    recovery_receipt_id: str = field(repr=False)

    @property
    def sensitive_values(self) -> tuple[str, ...]:
        return (
            self.outbox_owner_id,
            self.outbox_job_id,
            self.outbox_work_id,
            self.outbox_receipt_id,
            self.recovery_owner_id,
            self.recovery_job_id,
            self.recovery_work_id,
            self.recovery_receipt_id,
        )


def derive_canary(gate_digest: str) -> CanaryAuthority:
    if not _is_digest(gate_digest):
        raise SmokeError("canary derivation requires the exact gate digest")

    def identifier(label: str) -> str:
        return f"p66_{label}_{_digest_text(label + chr(0) + gate_digest)[:24]}"

    def owner(label: str) -> str:
        return _digest_text(f"phase66-owner\0{label}\0{gate_digest}")

    return CanaryAuthority(
        outbox_owner_id=owner("outbox"),
        outbox_job_id=identifier("outbox_job"),
        outbox_work_id=identifier("outbox_work"),
        outbox_receipt_id=identifier("outbox_receipt"),
        recovery_owner_id=owner("recovery"),
        recovery_job_id=identifier("recovery_job"),
        recovery_work_id=identifier("recovery_work"),
        recovery_receipt_id=identifier("recovery_receipt"),
    )


@dataclass(frozen=True, slots=True)
class DeploymentAuthority:
    table_name: str = field(repr=False)
    artifact_bucket: str = field(repr=False)
    functions: Mapping[str, str] = field(repr=False)
    state_machine_arns: Mapping[WorkType, str] = field(repr=False)


@dataclass(frozen=True, slots=True)
class LiveSnapshot:
    application_record_count: int
    application_record_digest: str
    provider_record_count: int
    dispatched_work_count: int
    running_execution_count: int
    execution_digests: tuple[str, ...]
    source_version_count: int
    source_inventory_digest: str
    referenced_version_count: int
    pinned_version_count: int
    retention_checkpoint_present: bool
    authority: DeploymentAuthority = field(repr=False)

    def gate_baseline(self, *, synthetic_namespace_absent: bool) -> dict[str, object]:
        return {
            "application_record_count": self.application_record_count,
            "application_record_digest": self.application_record_digest,
            "existing_dispatched_work_count": self.dispatched_work_count,
            "existing_execution_count": len(self.execution_digests),
            "existing_execution_digest": _digest_json(list(self.execution_digests)),
            "provider_record_count": self.provider_record_count,
            "retention_checkpoint_present": self.retention_checkpoint_present,
            "retention_pinned_version_count": self.pinned_version_count,
            "retention_referenced_version_count": self.referenced_version_count,
            "retention_source_inventory_digest": self.source_inventory_digest,
            "retention_source_version_count": self.source_version_count,
            "running_execution_count": self.running_execution_count,
            "synthetic_namespace_absent": synthetic_namespace_absent,
        }


@dataclass(frozen=True, slots=True)
class DispatchObservation:
    execution_arn: str = field(repr=False)
    execution_digest: str
    status: str
    exact_name_count: int
    exact_input: bool


class LiveBackend(Protocol):
    def prepare(self, gate: RunGate, canary: CanaryAuthority) -> LiveSnapshot: ...

    def invoke_retention(self, authority: DeploymentAuthority) -> Mapping[str, Any]: ...

    def verify_retention(self, before: LiveSnapshot) -> None: ...

    def put_outbox_work(self, authority: DeploymentAuthority, canary: CanaryAuthority) -> None: ...

    def invoke_dispatcher(self, authority: DeploymentAuthority) -> Mapping[str, Any]: ...

    def observe_outbox_execution(
        self, authority: DeploymentAuthority, canary: CanaryAuthority
    ) -> DispatchObservation: ...

    def put_recovery_pair(
        self, authority: DeploymentAuthority, canary: CanaryAuthority
    ) -> None: ...

    def invoke_recovery(self, authority: DeploymentAuthority) -> Mapping[str, Any]: ...

    def cleanup_synthetic(self, canary: CanaryAuthority) -> None: ...

    def snapshot(self, authority: DeploymentAuthority) -> LiveSnapshot: ...


def _exact_counter_response(
    response: Mapping[str, Any], expected: Mapping[str, object], label: str
) -> None:
    if dict(response) != dict(expected):
        raise SmokeError(f"{label} returned an unexpected sanitized counter envelope")


def _assert_private_payload(payload: bytes, canary: CanaryAuthority) -> None:
    lowered = payload.lower()
    forbidden_literals = (
        b"authorization",
        b"cookie",
        b"presigned",
        b"secret",
        b"subject",
        b"token",
    )
    if any(value.encode() in payload for value in canary.sensitive_values) or any(
        literal in lowered for literal in forbidden_literals
    ):
        raise SmokeError("sanitized evidence retained private authority")


def _verify_final(
    before: LiveSnapshot,
    after: LiveSnapshot,
    observation: DispatchObservation,
) -> None:
    if (
        after.application_record_count != before.application_record_count
        or after.application_record_digest != before.application_record_digest
        or after.provider_record_count != 0
        or after.dispatched_work_count != 0
        or after.running_execution_count != 0
        or after.source_version_count != before.source_version_count
        or after.source_inventory_digest != before.source_inventory_digest
        or after.referenced_version_count != before.referenced_version_count
        or after.pinned_version_count != before.pinned_version_count
        or not after.retention_checkpoint_present
    ):
        raise SmokeError("final deployed state did not return to the exact bounded baseline")
    added = Counter(after.execution_digests) - Counter(before.execution_digests)
    removed = Counter(before.execution_digests) - Counter(after.execution_digests)
    if removed or added != Counter({observation.execution_digest: 1}):
        raise SmokeError("workflow execution delta is not the one retained canary")


def run_live(gate: RunGate, backend: LiveBackend, output_root: Path) -> Mapping[str, object]:
    canary = derive_canary(gate.digest)
    before = backend.prepare(gate, canary)
    if before.gate_baseline(synthetic_namespace_absent=True) != gate.baseline:
        raise SmokeError("live baseline drifted from the exact private gate")

    retention = backend.invoke_retention(before.authority)
    expected_retention = {
        "contract_version": "1.0.0",
        "pages_scanned": 1,
        "versions_scanned": before.source_version_count,
        "delete_markers_skipped": 0,
        "versions_reasserted_pinned": before.source_version_count,
        "versions_released_to_staged": 0,
        "staged_versions_unchanged": 0,
        "scan_complete": True,
    }
    _exact_counter_response(retention, expected_retention, "retention sweep")
    backend.verify_retention(before)

    observation: DispatchObservation | None = None
    write_attempted = False
    try:
        write_attempted = True
        backend.put_outbox_work(before.authority, canary)
        first_dispatch = backend.invoke_dispatcher(before.authority)
        _exact_counter_response(
            first_dispatch,
            {"attempted": 1, "dispatched": 1},
            "due-work sweep",
        )
        second_dispatch = backend.invoke_dispatcher(before.authority)
        _exact_counter_response(
            second_dispatch,
            {"attempted": 0, "dispatched": 0},
            "idempotent due-work sweep replay",
        )
        observation = backend.observe_outbox_execution(before.authority, canary)
        if (
            observation.status != "FAILED"
            or observation.exact_name_count != 1
            or not observation.exact_input
        ):
            raise SmokeError("deterministic outbox execution was not exact and single")

        backend.put_recovery_pair(before.authority, canary)
        recovery = backend.invoke_recovery(before.authority)
        expected_recovery = {
            "contract_version": "1.0.0",
            "candidates_scanned": 1,
            "already_settled": 0,
            "not_due": 0,
            "running_past_bound": 0,
            "recovered_completion": 0,
            "failure_settled": 0,
            "reconciliation_routed": 0,
            "cancellation_settled": 1,
            "authority_conflicts": 0,
            "dependency_unavailable": 0,
            "settlement_exhausted": 0,
            "terminal_executions_observed": 0,
            "executions_missing": 1,
            "batch_limit": 25,
            "batch_limit_reached": False,
            "alarm_signal_count": 0,
            "requires_operator_attention": False,
        }
        _exact_counter_response(recovery, expected_recovery, "execution recovery sweep")
    finally:
        if write_attempted:
            backend.cleanup_synthetic(canary)

    if observation is None:
        raise SmokeError("outbox execution observation is unavailable")
    after = backend.snapshot(before.authority)
    _verify_final(before, after, observation)

    canary_summary = {
        "artifact_contract": "phase6.6-deployed-outbox-recovery-canary-summary-v1",
        "gate_digest": gate.digest,
        "deployment_digest": gate.deployment_digest,
        "prerequisite_evidence_run_digest": gate.prerequisite_digest,
        "assertions": {
            "committed_work_is_recovered_by_sweep": True,
            "deterministic_execution_starts_once": True,
            "logical_work_is_not_duplicated": True,
            "stuck_execution_recovery_passes": True,
            "reference_aware_retention_sweep_passes": True,
            "privacy_scan_passes": True,
            "provider_call_count_is_zero": True,
        },
        "counts": {
            "direct_dispatcher_invocations": 2,
            "direct_execution_recovery_invocations": 1,
            "direct_retention_invocations": 1,
            "provider_calls": 0,
            "retained_standard_executions": 1,
            "synthetic_rows_remaining": 0,
        },
        "redaction_verified": True,
        "source_authority_commit_digest": SOURCE_AUTHORITY_COMMIT_DIGEST,
        "status": "passed",
    }
    log_audit = {
        "artifact_contract": "phase6.6-deployed-outbox-recovery-log-audit-v1",
        "gate_digest": gate.digest,
        "deployment_digest": gate.deployment_digest,
        "prerequisite_evidence_run_digest": gate.prerequisite_digest,
        "deltas": {
            "agentcore_invocations": 0,
            "bedrock_invocations": 0,
            "dynamodb_application_records": 0,
            "provider_calls": 0,
            "provider_records": 0,
            "source_versions": 0,
            "workflow_executions": 1,
        },
        "raw_authority_retained": False,
        "status": "passed",
    }
    for value in (canary_summary, log_audit):
        _assert_private_payload(_canonical_json(value), canary)
    output = _validate_private_directory(output_root, create=True)
    summary_size, summary_digest = _atomic_private_json(
        output / "canary-summary.json", canary_summary
    )
    audit_size, audit_digest = _atomic_private_json(output / "log-audit.json", log_audit)
    return {
        "gate_id": GATE_ID,
        "mode": "live",
        "status": "passed",
        "artifacts": [
            {"kind": "canary_summary", "byte_count": summary_size, "sha256": summary_digest},
            {"kind": "log_audit", "byte_count": audit_size, "sha256": audit_digest},
        ],
        "redaction_verified": True,
    }


class AwsBackend:
    """Boto3 implementation constructed only after every local gate check succeeds."""

    def __init__(self, *, profile: str = PROFILE, region: str = REGION, stack: str = STACK_NAME):
        try:
            import boto3
            from botocore.config import Config
        except ImportError:
            raise SmokeError("boto3 is unavailable for the explicitly enabled live run") from None
        session = boto3.Session(profile_name=profile, region_name=region)
        no_retry = Config(retries={"total_max_attempts": 1, "mode": "standard"})
        self._cloudformation = session.client("cloudformation", config=no_retry)
        self._dynamodb = session.client("dynamodb", config=no_retry)
        self._lambda = session.client("lambda", config=no_retry)
        self._s3 = session.client("s3", config=no_retry)
        self._sfn = session.client("stepfunctions", config=no_retry)
        self._sts = session.client("sts", config=no_retry)
        self._stack = stack
        self._authority: DeploymentAuthority | None = None
        self._canary: CanaryAuthority | None = None

    def _stack_envelope(self) -> tuple[Mapping[str, str], Mapping[str, str]]:
        response = self._cloudformation.describe_stacks(StackName=self._stack)
        stacks = response.get("Stacks", [])
        if len(stacks) != 1 or stacks[0].get("StackStatus") != "UPDATE_COMPLETE":
            raise SmokeError("Phase 6 stack is not one exact UPDATE_COMPLETE deployment")
        outputs = {
            item["OutputKey"]: item["OutputValue"]
            for item in stacks[0].get("Outputs", [])
            if isinstance(item, Mapping)
            and isinstance(item.get("OutputKey"), str)
            and isinstance(item.get("OutputValue"), str)
        }
        parameters = {
            item["ParameterKey"]: item["ParameterValue"]
            for item in stacks[0].get("Parameters", [])
            if isinstance(item, Mapping)
            and isinstance(item.get("ParameterKey"), str)
            and isinstance(item.get("ParameterValue"), str)
        }
        if len(outputs) != len(stacks[0].get("Outputs", [])) or len(parameters) != len(
            stacks[0].get("Parameters", [])
        ):
            raise SmokeError("Phase 6 stack authority envelope is malformed")
        return outputs, parameters

    def _physical(self, logical_id: str) -> str:
        response = self._cloudformation.describe_stack_resource(
            StackName=self._stack, LogicalResourceId=logical_id
        )
        detail = _mapping(response.get("StackResourceDetail"), "stack resource")
        physical = detail.get("PhysicalResourceId")
        if detail.get("ResourceStatus") not in {
            "CREATE_COMPLETE",
            "UPDATE_COMPLETE",
        } or not isinstance(physical, str):
            raise SmokeError("deployed stack resource is not ready")
        return physical

    def _function_configuration(
        self,
        logical_id: str,
        expected_name: str,
        expected_handler: str,
        expected_timeout: int,
        expected_environment: Mapping[str, str],
    ) -> str:
        name = self._physical(logical_id)
        if name != expected_name:
            raise SmokeError("deployed Lambda physical identity drifted")
        configuration = self._lambda.get_function_configuration(FunctionName=name)
        variables = _mapping(
            _mapping(configuration.get("Environment"), "Lambda environment").get("Variables"),
            "Lambda variables",
        )
        arn = configuration.get("FunctionArn")
        if (
            configuration.get("State") != "Active"
            or configuration.get("LastUpdateStatus") != "Successful"
            or configuration.get("Handler") != expected_handler
            or configuration.get("CodeSha256") != SHARED_LAMBDA_CODE_SHA256
            or configuration.get("Runtime") != "python3.12"
            or configuration.get("Architectures") != ["arm64"]
            or configuration.get("MemorySize") != 256
            or configuration.get("Timeout") != expected_timeout
            or variables != expected_environment
            or not isinstance(arn, str)
        ):
            raise SmokeError("deployed Lambda code/environment envelope drifted")
        return arn

    @staticmethod
    def _base_environment(outputs: Mapping[str, str]) -> dict[str, str]:
        return {
            "MR_LISTER_PHASE6_SCAFFOLD_ONLY": "false",
            "MR_LISTER_ENVIRONMENT": "dev",
            "MR_LISTER_AWS_ACCOUNT_ID": ACCOUNT_ID,
            "MR_LISTER_RELEASE_FINGERPRINT": SHARED_RELEASE_FINGERPRINT,
            "MR_LISTER_STATE_TABLE": outputs["StateTableName"],
            "MR_LISTER_ARTIFACT_BUCKET": outputs["ArtifactBucketName"],
        }

    def _verify_prepare_machine(
        self,
        machine_arn: str,
        preparation_function_arn: str,
        settlement_function_arn: str,
    ) -> None:
        try:
            source = PREPARE_DEFINITION_PATH.read_bytes()
        except OSError:
            raise SmokeError("frozen local PREPARE definition is unavailable") from None
        if _digest_bytes(source) != PREPARE_DEFINITION_SHA256:
            raise SmokeError("frozen local PREPARE definition drifted")
        expected = _strict_json(source, "PREPARE definition")

        def substitute(value: object) -> object:
            if isinstance(value, str):
                return value.replace(
                    "${PreparationDispatchFunctionArn}", preparation_function_arn
                ).replace("${SettlementFunctionArn}", settlement_function_arn)
            if isinstance(value, list):
                return [substitute(item) for item in value]
            if isinstance(value, Mapping):
                return {key: substitute(item) for key, item in value.items()}
            return value

        response = self._sfn.describe_state_machine(stateMachineArn=machine_arn)
        definition = response.get("definition")
        if (
            response.get("status") != "ACTIVE"
            or response.get("type") != "STANDARD"
            or not isinstance(definition, str)
            or _strict_json(definition.encode(), "deployed PREPARE definition")
            != substitute(expected)
            or _mapping(response.get("loggingConfiguration"), "workflow logging").get(
                "includeExecutionData"
            )
            is not False
        ):
            raise SmokeError("deployed PREPARE workflow authority drifted")

    def _verify_dispatch_stream_filter(self, dispatcher_name: str) -> None:
        response = self._lambda.list_event_source_mappings(FunctionName=dispatcher_name)
        mappings = response.get("EventSourceMappings", [])
        if not isinstance(mappings, list) or len(mappings) != 1:
            raise SmokeError("dispatcher stream mapping is not one exact bounded source")
        mapping = _mapping(mappings[0], "dispatcher stream mapping")
        criteria = _mapping(mapping.get("FilterCriteria"), "dispatcher stream filter")
        filters = criteria.get("Filters")
        if not isinstance(filters, list) or len(filters) != 1:
            raise SmokeError("dispatcher stream filter is not exact")
        pattern = _mapping(filters[0], "dispatcher stream filter").get("Pattern")
        if (
            mapping.get("State") != "Enabled"
            or mapping.get("BatchSize") != 25
            or mapping.get("MaximumRetryAttempts") != 3
            or not isinstance(pattern, str)
            or _strict_json(pattern.encode(), "dispatcher stream pattern")
            != _SAFE_DISPATCH_STREAM_FILTER
        ):
            # Exact synthetic Work cleanup emits REMOVE stream records.  Without this event-name
            # allowlist, the deployed handler rejects those records and creates retry/alarm noise.
            # Refuse every write until the mapping proves cleanup is operationally inert.
            raise SmokeError("dispatcher stream filter does not make exact cleanup inert")

    def _scan_raw(self, table_name: str) -> tuple[Mapping[str, Any], ...]:
        items: list[Mapping[str, Any]] = []
        request: dict[str, Any] = {"TableName": table_name, "ConsistentRead": True}
        while True:
            response = self._dynamodb.scan(**request)
            batch = response.get("Items", [])
            if not isinstance(batch, list):
                raise SmokeError("DynamoDB scan envelope is invalid")
            items.extend(_mapping(item, "DynamoDB item") for item in batch)
            if len(items) > MAX_TABLE_ITEMS:
                raise SmokeError("DynamoDB inventory exceeds the bounded smoke limit")
            key = response.get("LastEvaluatedKey")
            if not key:
                break
            request["ExclusiveStartKey"] = key
        return tuple(items)

    @staticmethod
    def _item_payload(item: Mapping[str, Any]) -> tuple[str, Mapping[str, Any]]:
        entity = _mapping(item.get("entity_type"), "entity type").get("S")
        payload = _mapping(item.get("payload"), "payload attribute").get("S")
        if not isinstance(entity, str) or not isinstance(payload, str):
            raise SmokeError("DynamoDB item lacks the closed payload envelope")
        return entity, _mapping(_strict_json(payload.encode(), "record"), "record")

    def _execution_inventory(
        self, state_machine_arns: Mapping[WorkType, str]
    ) -> tuple[tuple[str, ...], int]:
        execution_digests: list[str] = []
        running = 0
        for arn in state_machine_arns.values():
            token: str | None = None
            while True:
                request: dict[str, Any] = {"stateMachineArn": arn, "maxResults": 100}
                if token is not None:
                    request["nextToken"] = token
                response = self._sfn.list_executions(**request)
                for execution in response.get("executions", []):
                    execution_arn = execution.get("executionArn")
                    if not isinstance(execution_arn, str):
                        raise SmokeError("workflow execution inventory is invalid")
                    execution_digests.append(_digest_text(execution_arn))
                    running += execution.get("status") == "RUNNING"
                token = response.get("nextToken")
                if not isinstance(token, str):
                    break
        return tuple(sorted(execution_digests)), running

    def _source_inventory(
        self,
        bucket: str,
        source_authorities: set[tuple[str, str]],
    ) -> tuple[int, str, int, int]:
        response = self._s3.list_object_versions(
            Bucket=bucket,
            Prefix="private/owners/",
            MaxKeys=MAX_SOURCE_VERSIONS + 1,
            ExpectedBucketOwner=ACCOUNT_ID,
        )
        versions = response.get("Versions", [])
        markers = response.get("DeleteMarkers", [])
        if response.get("IsTruncated") or markers or not isinstance(versions, list):
            raise SmokeError("source-version inventory is not one bounded marker-free page")
        sanitized: list[dict[str, object]] = []
        referenced = 0
        pinned = 0
        seen: set[tuple[str, str]] = set()
        for version in versions:
            key = version.get("Key")
            version_id = version.get("VersionId")
            if not isinstance(key, str) or not isinstance(version_id, str):
                raise SmokeError("source-version inventory entry is invalid")
            coordinate = (key, version_id)
            if coordinate in seen:
                raise SmokeError("source-version inventory contains a duplicate")
            seen.add(coordinate)
            if coordinate in source_authorities:
                referenced += 1
            tags = self._s3.get_object_tagging(
                Bucket=bucket,
                Key=key,
                VersionId=version_id,
                ExpectedBucketOwner=ACCOUNT_ID,
            )
            if tags.get("TagSet") == [{"Key": "mr-lister-state", "Value": "pinned"}]:
                pinned += 1
            sanitized.append(
                {
                    "coordinate_digest": _digest_text(bucket + "\0" + key + "\0" + version_id),
                    "is_latest": bool(version.get("IsLatest")),
                    "last_modified": version["LastModified"].astimezone(UTC).isoformat(),
                    "size": int(version["Size"]),
                }
            )
        sanitized.sort(key=lambda item: str(item["coordinate_digest"]))
        return len(versions), _digest_json(sanitized), referenced, pinned

    def _snapshot(self, authority: DeploymentAuthority) -> LiveSnapshot:
        raw_items = self._scan_raw(authority.table_name)
        application_records: list[tuple[str, str]] = []
        provider_records = 0
        dispatched = 0
        checkpoint = False
        source_authorities: set[tuple[str, str]] = set()
        for item in raw_items:
            entity, payload = self._item_payload(item)
            if entity == _CHECKPOINT_ENTITY:
                checkpoint = True
                continue
            application_records.append((entity, _digest_json(payload)))
            provider_records += entity.startswith("PROVIDER_")
            dispatched += entity == "WORK_REQUEST" and payload.get("status") == "dispatched"
            if entity == "SOURCE_ARTIFACT":
                key = payload.get("object_key")
                version = payload.get("version_id")
                bucket = payload.get("bucket")
                if (
                    not isinstance(key, str)
                    or not isinstance(version, str)
                    or bucket != authority.artifact_bucket
                ):
                    raise SmokeError("source authority envelope is invalid")
                source_authorities.add((key, version))
        application_records.sort()
        executions, running = self._execution_inventory(authority.state_machine_arns)
        source_count, source_digest, referenced, pinned = self._source_inventory(
            authority.artifact_bucket, source_authorities
        )
        return LiveSnapshot(
            application_record_count=len(application_records),
            application_record_digest=_digest_json(application_records),
            provider_record_count=provider_records,
            dispatched_work_count=dispatched,
            running_execution_count=running,
            execution_digests=executions,
            source_version_count=source_count,
            source_inventory_digest=source_digest,
            referenced_version_count=referenced,
            pinned_version_count=pinned,
            retention_checkpoint_present=checkpoint,
            authority=authority,
        )

    @staticmethod
    def _raw_text(item: Mapping[str, Any], name: str) -> str | None:
        value = item.get(name)
        if not isinstance(value, Mapping):
            return None
        text = value.get("S")
        return text if isinstance(text, str) else None

    def _synthetic_absent(self, table_name: str, canary: CanaryAuthority) -> bool:
        values = set(canary.sensitive_values)
        for item in self._scan_raw(table_name):
            raw = _canonical_json(item).decode()
            if any(value in raw for value in values):
                return False
        return True

    def prepare(self, gate: RunGate, canary: CanaryAuthority) -> LiveSnapshot:
        if self._sts.get_caller_identity().get("Account") != ACCOUNT_ID:
            raise SmokeError("AWS session is not in the exact deployment account")
        outputs, parameters = self._stack_envelope()
        required_outputs = {
            "ArtifactBucketName",
            "DeploymentReadiness",
            "PrepareStateMachineArn",
            "ReconcileProductStateMachineArn",
            "RefreshEconomicsStateMachineArn",
            "StateTableName",
            "SynchronizeProductStateMachineArn",
        }
        required_parameters = {
            "AgentCoreRuntimeArn",
            "AgentCoreRuntimeBindingFingerprint",
            "AgentCoreRuntimeEndpointArn",
            "AgentCoreRuntimeQualifier",
            "AgentCoreRuntimeVersion",
        }
        if (
            not required_outputs <= set(outputs)
            or not required_parameters <= set(parameters)
            or outputs["DeploymentReadiness"] != "WEB_EDGE_ACTIVE_DRAFT_ONLY"
        ):
            raise SmokeError("Phase 6 stack outputs drifted from active draft-only authority")
        state_machine_arns = {
            WorkType.PREPARE: outputs["PrepareStateMachineArn"],
            WorkType.SYNCHRONIZE_PRODUCT: outputs["SynchronizeProductStateMachineArn"],
            WorkType.RECONCILE_PRODUCT: outputs["ReconcileProductStateMachineArn"],
            WorkType.REFRESH_ECONOMICS: outputs["RefreshEconomicsStateMachineArn"],
        }
        base = self._base_environment(outputs)
        function_names: dict[str, str] = {}
        function_arns: dict[str, str] = {}
        for logical_id, (name, handler, timeout) in _EXPECTED_FUNCTIONS.items():
            expected_environment = dict(base)
            if logical_id == "DispatcherFunction":
                expected_environment.update(
                    {
                        "MR_LISTER_PREPARE_MACHINE_ARN": state_machine_arns[WorkType.PREPARE],
                        "MR_LISTER_SYNCHRONIZE_PRODUCT_MACHINE_ARN": state_machine_arns[
                            WorkType.SYNCHRONIZE_PRODUCT
                        ],
                        "MR_LISTER_RECONCILE_PRODUCT_MACHINE_ARN": state_machine_arns[
                            WorkType.RECONCILE_PRODUCT
                        ],
                        "MR_LISTER_REFRESH_ECONOMICS_MACHINE_ARN": state_machine_arns[
                            WorkType.REFRESH_ECONOMICS
                        ],
                    }
                )
            elif logical_id == "StuckExecutionRecoveryFunction":
                expected_environment.update(
                    {
                        "MR_LISTER_EXECUTION_RECOVERY_STALE_SECONDS": "1200",
                        "MR_LISTER_EXECUTION_RECOVERY_BATCH_LIMIT": "25",
                        "MR_LISTER_EXECUTION_RECOVERY_MAX_CAS_RECHECKS": "2",
                        "MR_LISTER_PREPARE_MACHINE_ARN": state_machine_arns[WorkType.PREPARE],
                        "MR_LISTER_SYNCHRONIZE_PRODUCT_MACHINE_ARN": state_machine_arns[
                            WorkType.SYNCHRONIZE_PRODUCT
                        ],
                        "MR_LISTER_RECONCILE_PRODUCT_MACHINE_ARN": state_machine_arns[
                            WorkType.RECONCILE_PRODUCT
                        ],
                        "MR_LISTER_REFRESH_ECONOMICS_MACHINE_ARN": state_machine_arns[
                            WorkType.REFRESH_ECONOMICS
                        ],
                    }
                )
            elif logical_id == "PreparationDispatchFunction":
                expected_environment.update(
                    {
                        "MR_LISTER_AGENTCORE_RUNTIME_ARN": parameters["AgentCoreRuntimeArn"],
                        "MR_LISTER_AGENTCORE_RUNTIME_BINDING_FINGERPRINT": parameters[
                            "AgentCoreRuntimeBindingFingerprint"
                        ],
                        "MR_LISTER_AGENTCORE_RUNTIME_ENDPOINT_ARN": parameters[
                            "AgentCoreRuntimeEndpointArn"
                        ],
                        "MR_LISTER_AGENTCORE_RUNTIME_QUALIFIER": parameters[
                            "AgentCoreRuntimeQualifier"
                        ],
                        "MR_LISTER_AGENTCORE_RUNTIME_VERSION": parameters[
                            "AgentCoreRuntimeVersion"
                        ],
                    }
                )
            function_names[logical_id] = name
            function_arns[logical_id] = self._function_configuration(
                logical_id,
                name,
                handler,
                timeout,
                expected_environment,
            )
        self._verify_dispatch_stream_filter(function_names["DispatcherFunction"])
        self._verify_prepare_machine(
            state_machine_arns[WorkType.PREPARE],
            function_arns["PreparationDispatchFunction"],
            function_arns["SettlementFunction"],
        )
        authority = DeploymentAuthority(
            table_name=outputs["StateTableName"],
            artifact_bucket=outputs["ArtifactBucketName"],
            functions=function_names,
            state_machine_arns=state_machine_arns,
        )
        self._authority = authority
        self._canary = canary
        snapshot = self._snapshot(authority)
        absent = self._synthetic_absent(authority.table_name, canary)
        if snapshot.gate_baseline(synthetic_namespace_absent=absent) != gate.baseline:
            raise SmokeError("live baseline drifted from the exact private gate")
        return snapshot

    def _invoke(self, function_name: str, event: Mapping[str, Any]) -> Mapping[str, Any]:
        response = self._lambda.invoke(
            FunctionName=function_name,
            InvocationType="RequestResponse",
            Payload=_canonical_json(event),
        )
        stream = response.get("Payload")
        try:
            payload = stream.read(MAX_PRIVATE_BYTES + 1)
        finally:
            close = getattr(stream, "close", None)
            if callable(close):
                close()
        if (
            response.get("StatusCode") != 200
            or response.get("FunctionError") is not None
            or not isinstance(payload, bytes)
            or len(payload) > MAX_PRIVATE_BYTES
        ):
            raise SmokeError("direct deployed Lambda invocation failed closed")
        return _mapping(_strict_json(payload, "Lambda response"), "Lambda response")

    def invoke_retention(self, authority: DeploymentAuthority) -> Mapping[str, Any]:
        return self._invoke(
            authority.functions["SourceVersionRetentionFunction"],
            {"contract_version": "1.0.0", "source": "source-version-retention-sweeper"},
        )

    def verify_retention(self, before: LiveSnapshot) -> None:
        after = self._snapshot(before.authority)
        if (
            after.source_version_count != before.source_version_count
            or after.source_inventory_digest != before.source_inventory_digest
            or after.referenced_version_count != before.referenced_version_count
            or after.pinned_version_count != before.pinned_version_count
            or not after.retention_checkpoint_present
        ):
            raise SmokeError("retention sweep changed source-version authority")

    @staticmethod
    def _outbox_work(canary: CanaryAuthority, now: datetime) -> WorkRequest:
        return WorkRequest(
            work_request_id=canary.outbox_work_id,
            owner_id=canary.outbox_owner_id,
            job_id=canary.outbox_job_id,
            receipt_id=canary.outbox_receipt_id,
            work_type=WorkType.PREPARE,
            input_fingerprint=work_input_fingerprint(
                work_type=WorkType.PREPARE,
                job_id=canary.outbox_job_id,
                work_request_id=canary.outbox_work_id,
            ),
            execution_name=deterministic_execution_name(canary.outbox_work_id),
            status=WorkRequestStatus.PENDING,
            # Keep the insert non-due long enough for its DynamoDB stream record to be consumed.
            # The work becomes due without another table mutation, so the following direct
            # invocation proves the backup due-work sweep rather than the stream path.
            next_dispatch_at=now + timedelta(seconds=OUTBOX_STREAM_QUIESCENCE_SECONDS),
            created_at=now,
            updated_at=now,
        )

    def put_outbox_work(self, authority: DeploymentAuthority, canary: CanaryAuthority) -> None:
        work = self._outbox_work(canary, datetime.now(UTC))
        self._dynamodb.put_item(
            TableName=authority.table_name,
            Item=_work_item(work),
            ConditionExpression="attribute_not_exists(PK) AND attribute_not_exists(SK)",
        )
        due = work.next_dispatch_at + timedelta(milliseconds=250)
        while (remaining := (due - datetime.now(UTC)).total_seconds()) > 0:
            time.sleep(min(remaining, 1.0))

    def invoke_dispatcher(self, authority: DeploymentAuthority) -> Mapping[str, Any]:
        return self._invoke(
            authority.functions["DispatcherFunction"], {"source": "due-work-sweeper"}
        )

    def _get_item(self, table: str, pk: str, sk: str) -> Mapping[str, Any] | None:
        response = self._dynamodb.get_item(
            TableName=table,
            Key={"PK": {"S": pk}, "SK": {"S": sk}},
            ConsistentRead=True,
        )
        item = response.get("Item")
        return _mapping(item, "DynamoDB item") if item is not None else None

    def observe_outbox_execution(
        self, authority: DeploymentAuthority, canary: CanaryAuthority
    ) -> DispatchObservation:
        item = self._get_item(
            authority.table_name,
            f"JOB#{canary.outbox_job_id}",
            f"WORK#{canary.outbox_work_id}",
        )
        if item is None:
            raise SmokeError("outbox work row is unavailable")
        entity, payload = self._item_payload(item)
        expected_name = deterministic_execution_name(canary.outbox_work_id)
        expected_arn = execution_arn_for(
            authority.state_machine_arns[WorkType.PREPARE], expected_name
        )
        if (
            entity != "WORK_REQUEST"
            or payload.get("status") != "dispatched"
            or payload.get("job_id") != canary.outbox_job_id
            or payload.get("work_request_id") != canary.outbox_work_id
            or payload.get("execution_name") != expected_name
            or payload.get("execution_arn") != expected_arn
            or self._get_item(authority.table_name, f"JOB#{canary.outbox_job_id}", "META")
            is not None
        ):
            raise SmokeError("outbox work did not bind one orphan deterministic execution")
        deadline = time.monotonic() + MAX_EXECUTION_WAIT_SECONDS
        description: Mapping[str, Any]
        while True:
            description = self._sfn.describe_execution(executionArn=expected_arn)
            if description.get("status") != "RUNNING":
                break
            if time.monotonic() >= deadline:
                raise SmokeError("outbox canary execution did not settle within its bound")
            time.sleep(1)
        expected_input = _canonical_json(
            {"job_id": canary.outbox_job_id, "work_request_id": canary.outbox_work_id}
        ).decode()
        count = 0
        token: str | None = None
        while True:
            request: dict[str, Any] = {
                "stateMachineArn": authority.state_machine_arns[WorkType.PREPARE],
                "maxResults": 100,
            }
            if token is not None:
                request["nextToken"] = token
            response = self._sfn.list_executions(**request)
            count += sum(
                item.get("name") == expected_name for item in response.get("executions", [])
            )
            token = response.get("nextToken")
            if not isinstance(token, str):
                break
        return DispatchObservation(
            execution_arn=expected_arn,
            execution_digest=_digest_text(expected_arn),
            status=str(description.get("status")),
            exact_name_count=count,
            exact_input=(
                description.get("name") == expected_name
                and description.get("stateMachineArn")
                == authority.state_machine_arns[WorkType.PREPARE]
                and description.get("input") == expected_input
            ),
        )

    @staticmethod
    def _recovery_models(
        authority: DeploymentAuthority,
        canary: CanaryAuthority,
        now: datetime,
    ) -> tuple[ControlJobRecord, WorkRequest]:
        stale = now - timedelta(minutes=30)
        execution_name = deterministic_execution_name(canary.recovery_work_id)
        work = WorkRequest(
            work_request_id=canary.recovery_work_id,
            owner_id=canary.recovery_owner_id,
            job_id=canary.recovery_job_id,
            receipt_id=canary.recovery_receipt_id,
            work_type=WorkType.PREPARE,
            input_fingerprint=work_input_fingerprint(
                work_type=WorkType.PREPARE,
                job_id=canary.recovery_job_id,
                work_request_id=canary.recovery_work_id,
            ),
            execution_name=execution_name,
            status=WorkRequestStatus.DISPATCHED,
            attempt_count=1,
            next_dispatch_at=stale,
            execution_arn=execution_arn_for(
                authority.state_machine_arns[WorkType.PREPARE], execution_name
            ),
            created_at=stale - timedelta(minutes=1),
            updated_at=stale,
        )
        job = ControlJobRecord(
            owner_id=canary.recovery_owner_id,
            job_id=canary.recovery_job_id,
            record_version=1,
            event_sequence=1,
            state=ControlJobState.CANCEL_REQUESTED,
            active_work_request_id=canary.recovery_work_id,
            cancellation_requested_at=stale + timedelta(minutes=1),
            created_at=stale - timedelta(minutes=2),
            updated_at=stale,
        )
        return job, work

    def put_recovery_pair(self, authority: DeploymentAuthority, canary: CanaryAuthority) -> None:
        job, work = self._recovery_models(authority, canary, datetime.now(UTC))
        self._dynamodb.transact_write_items(
            TransactItems=[
                {
                    "Put": {
                        "TableName": authority.table_name,
                        "Item": _job_item(job),
                        "ConditionExpression": "attribute_not_exists(PK)",
                    }
                },
                {
                    "Put": {
                        "TableName": authority.table_name,
                        "Item": _work_item(work),
                        "ConditionExpression": "attribute_not_exists(PK)",
                    }
                },
            ],
            ClientRequestToken=_digest_text("recovery-pair\0" + canary.recovery_job_id)[:32],
        )

    def invoke_recovery(self, authority: DeploymentAuthority) -> Mapping[str, Any]:
        return self._invoke(
            authority.functions["StuckExecutionRecoveryFunction"],
            {"source": "stuck-execution-sweeper"},
        )

    def _query_partition(self, table: str, pk: str) -> tuple[Mapping[str, Any], ...]:
        response = self._dynamodb.query(
            TableName=table,
            KeyConditionExpression="PK = :pk",
            ExpressionAttributeValues={":pk": {"S": pk}},
            ConsistentRead=True,
        )
        if response.get("LastEvaluatedKey"):
            raise SmokeError("synthetic cleanup partition exceeded its exact bound")
        return tuple(_mapping(item, "synthetic item") for item in response.get("Items", []))

    def _cleanup_items(
        self,
        authority: DeploymentAuthority,
        items: Sequence[Mapping[str, Any]],
        canary: CanaryAuthority,
    ) -> None:
        if not items:
            return
        sensitive = set(canary.sensitive_values)
        deletes: list[dict[str, Any]] = []
        for item in items:
            pk = self._raw_text(item, "PK")
            sk = self._raw_text(item, "SK")
            payload = self._raw_text(item, "payload")
            entity = self._raw_text(item, "entity_type")
            if (
                not isinstance(pk, str)
                or not isinstance(sk, str)
                or not isinstance(payload, str)
                or not isinstance(entity, str)
                or not any(value in payload or value in pk or value in sk for value in sensitive)
            ):
                raise SmokeError("synthetic cleanup authority is not exact")
            deletes.append(
                {
                    "Delete": {
                        "TableName": authority.table_name,
                        "Key": {"PK": {"S": pk}, "SK": {"S": sk}},
                        "ConditionExpression": "payload = :payload AND entity_type = :entity",
                        "ExpressionAttributeValues": {
                            ":payload": {"S": payload},
                            ":entity": {"S": entity},
                        },
                    }
                }
            )
        if len(deletes) == 1:
            item = deletes[0]["Delete"]
            self._dynamodb.delete_item(
                TableName=item["TableName"],
                Key=item["Key"],
                ConditionExpression=item["ConditionExpression"],
                ExpressionAttributeValues=item["ExpressionAttributeValues"],
            )
        else:
            self._dynamodb.transact_write_items(
                TransactItems=deletes,
                ClientRequestToken=_digest_json(
                    sorted(
                        (self._raw_text(item, "PK"), self._raw_text(item, "SK")) for item in items
                    )
                )[:32],
            )

    def cleanup_synthetic(self, canary: CanaryAuthority) -> None:
        authority = self._authority
        if authority is None:
            raise SmokeError("synthetic cleanup has no deployment authority")
        outbox = self._query_partition(authority.table_name, f"JOB#{canary.outbox_job_id}")
        recovery_job = self._query_partition(authority.table_name, f"JOB#{canary.recovery_job_id}")
        recovery_owner = self._query_partition(
            authority.table_name, f"OWNER#{canary.recovery_owner_id}"
        )
        combined = (*outbox, *recovery_job, *recovery_owner)
        if len(combined) > 5:
            raise SmokeError("synthetic cleanup inventory exceeded the exact five-row budget")
        expected_outbox = {
            (
                f"JOB#{canary.outbox_job_id}",
                f"WORK#{canary.outbox_work_id}",
                "WORK_REQUEST",
            )
        }
        observed_outbox = {
            (
                self._raw_text(item, "PK"),
                self._raw_text(item, "SK"),
                self._raw_text(item, "entity_type"),
            )
            for item in outbox
        }
        allowed_recovery_job = {
            (f"JOB#{canary.recovery_job_id}", "META", "CONTROL_JOB"),
            (
                f"JOB#{canary.recovery_job_id}",
                f"WORK#{canary.recovery_work_id}",
                "WORK_REQUEST",
            ),
            (
                f"JOB#{canary.recovery_job_id}",
                "EVENT#00000000000000000002",
                "DOMAIN_EVENT",
            ),
        }
        observed_recovery_job = {
            (
                self._raw_text(item, "PK"),
                self._raw_text(item, "SK"),
                self._raw_text(item, "entity_type"),
            )
            for item in recovery_job
        }
        if (
            not observed_outbox <= expected_outbox
            or not observed_recovery_job <= allowed_recovery_job
            or len(recovery_owner) > 1
            or any(
                self._raw_text(item, "PK") != f"OWNER#{canary.recovery_owner_id}"
                or self._raw_text(item, "entity_type") != "COMMAND_RECEIPT"
                or canary.recovery_job_id not in (self._raw_text(item, "payload") or "")
                for item in recovery_owner
            )
        ):
            raise SmokeError("synthetic cleanup inventory is outside the exact canary graph")
        self._cleanup_items(authority, outbox, canary)
        self._cleanup_items(authority, (*recovery_job, *recovery_owner), canary)
        if any(
            self._query_partition(authority.table_name, partition)
            for partition in (
                f"JOB#{canary.outbox_job_id}",
                f"JOB#{canary.recovery_job_id}",
                f"OWNER#{canary.recovery_owner_id}",
            )
        ):
            raise SmokeError("synthetic DynamoDB cleanup did not converge")

    def snapshot(self, authority: DeploymentAuthority) -> LiveSnapshot:
        return self._snapshot(authority)


def _preflight_result(gate: RunGate) -> Mapping[str, object]:
    derive_canary(gate.digest)
    return {
        "gate_id": GATE_ID,
        "gate_digest": gate.digest,
        "deployment_digest": gate.deployment_digest,
        "mode": "local_preflight",
        "network_calls": 0,
        "prerequisite_evidence_run_digest": gate.prerequisite_digest,
        "mutations": 0,
        "status": "ready",
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gate", type=Path, required=True)
    parser.add_argument("--gate-sha256", required=True)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--output-root", type=Path)
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    backend_factory: Callable[[], LiveBackend] = AwsBackend,
) -> int:
    arguments = _parser().parse_args(argv)
    gate = load_run_gate(arguments.gate, arguments.gate_sha256)
    if not arguments.live:
        print(_canonical_json(_preflight_result(gate)).decode())
        return 0
    if arguments.output_root is None:
        raise SmokeError("live mode requires an explicit repository-private output root")
    if os.environ.get(LIVE_ENVIRONMENT_SWITCH) != LIVE_ENVIRONMENT_VALUE:
        raise SmokeError("live mode requires the exact one-run environment switch")
    output_root = _exact_private_path(arguments.output_root)
    backend = backend_factory()
    print(_canonical_json(run_live(gate, backend, output_root)).decode())
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SmokeError as error:
        raise SystemExit(f"phase66 deployed outbox/recovery smoke stopped: {error}") from None
    except Exception:
        raise SystemExit(
            "phase66 deployed outbox/recovery smoke stopped: an external operation failed closed"
        ) from None
