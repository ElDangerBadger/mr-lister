from __future__ import annotations

import ast
import json
import os
from copy import deepcopy
from hashlib import sha256
from pathlib import Path
from typing import Any

import pytest

from mr_lister.acceptance.evidence_set import (
    _declared_artifacts,
    _validated_artifact_files,
    _validated_records,
    _verify_artifacts,
)
from mr_lister.acceptance.phase6 import (
    AcceptanceEvidenceClass,
    ArtifactFormat,
    ArtifactKind,
    phase66_acceptance_manifest,
    phase66_manifest_digest,
    validate_phase66_evidence,
)
from tools import prepare_phase66_outbox_recovery_evidence as assembler

DEPLOYMENT_DIGEST = sha256(b"deployment").hexdigest()
PREREQUISITE_RUN_DIGEST = sha256(b"upload-run").hexdigest()
NAMESPACE_SEED = sha256(b"namespace-seed").hexdigest()
EXECUTION_DIGEST = sha256(b"outbox-execution").hexdigest()
STARTED_AT = "2026-08-29T22:31:00Z"
COMPLETED_AT = "2026-08-29T22:32:03Z"


def _digest(label: str) -> str:
    return sha256(label.encode("ascii")).hexdigest()


def _gate() -> dict[str, object]:
    return {
        "authorization_contract": assembler.GATE_CONTRACT,
        "baseline": {
            "application_record_count": 50,
            "application_record_digest": _digest("application-records"),
            "existing_dispatched_work_count": 0,
            "existing_execution_count": 2,
            "existing_execution_digest": _digest("existing-executions"),
            "provider_record_count": 0,
            "retention_checkpoint_present": True,
            "retention_pinned_version_count": 2,
            "retention_referenced_version_count": 2,
            "retention_source_inventory_digest": _digest("source-inventory"),
            "retention_source_version_count": 2,
            "running_execution_count": 0,
            "synthetic_namespace_absent": True,
            "synthetic_namespace_seed": NAMESPACE_SEED,
        },
        "deployment_digest": DEPLOYMENT_DIGEST,
        "exact_write_budget": {
            "agentcore_invocations": 0,
            "bedrock_invocations": 0,
            "browser_authority_reads": 0,
            "cognito_reads": 0,
            "direct_dispatcher_lambda_invocations": 2,
            "direct_execution_recovery_lambda_invocations": 1,
            "direct_retention_lambda_invocations": 1,
            "dynamodb_item_deletes": 6,
            "dynamodb_item_writes": 17,
            "dynamodb_new_items": 6,
            "dynamodb_transactions": 5,
            "logical_work_requests": 2,
            "provider_calls": 0,
            "provider_records": 0,
            "retention_checkpoint_writes": 1,
            "retention_versions_released": 0,
            "s3_object_deletes": 0,
            "s3_object_puts": 0,
            "s3_version_tag_writes": 2,
            "secretsmanager_reads": 0,
            "stepfunctions_executions_started": 1,
            "stepfunctions_executions_stopped": 0,
            "stepfunctions_retained_executions": 1,
        },
        "gate_id": assembler.GATE_ID,
        "method_authorization": {
            "browser_authority_not_used": True,
            "exact_synthetic_row_cleanup": True,
            "preparation_authority_rejected_before_agentcore": True,
            "one_retained_standard_execution_accepted": True,
            "raw_identity_retained": False,
            "stream_quiescence_before_due_sweep": True,
            "synthetic_cancelled_missing_execution_recovery": True,
            "whole_inventory_referenced_and_pinned": True,
        },
        "prerequisite_evidence_run_digest": PREREQUISITE_RUN_DIGEST,
        "source_authority_commit": assembler.SOURCE_COMMIT,
        "source_authority_commit_digest": assembler.SOURCE_COMMIT_DIGEST,
        "synthetic_namespace_seed": NAMESPACE_SEED,
    }


def _execution_authority() -> dict[str, object]:
    return {
        "authority_contract": assembler.EXECUTION_AUTHORITY_CONTRACT,
        "completed_at": COMPLETED_AT,
        "execution_digest": EXECUTION_DIGEST,
        "started_at": STARTED_AT,
    }


def _canary(gate_digest: str) -> dict[str, object]:
    return {
        "artifact_contract": assembler.RAW_CANARY_CONTRACT,
        "assertions": {assertion_id: True for assertion_id in assembler._EXPECTED_ASSERTIONS},
        "counts": {
            "direct_dispatcher_invocations": 2,
            "direct_execution_recovery_invocations": 1,
            "direct_retention_invocations": 1,
            "provider_calls": 0,
            "retained_standard_executions": 1,
            "synthetic_rows_remaining": 0,
        },
        "deployment_digest": DEPLOYMENT_DIGEST,
        "execution_authority": _execution_authority(),
        "gate_digest": gate_digest,
        "prerequisite_evidence_run_digest": PREREQUISITE_RUN_DIGEST,
        "redaction_verified": True,
        "source_authority_commit_digest": assembler.SOURCE_COMMIT_DIGEST,
        "status": "passed",
    }


def _log(gate_digest: str) -> dict[str, object]:
    return {
        "artifact_contract": assembler.RAW_LOG_CONTRACT,
        "deltas": {
            "agentcore_invocations": 0,
            "bedrock_invocations": 0,
            "dynamodb_application_records": 0,
            "provider_calls": 0,
            "provider_records": 0,
            "source_versions": 0,
            "workflow_executions": 1,
        },
        "deployment_digest": DEPLOYMENT_DIGEST,
        "execution_authority": _execution_authority(),
        "gate_digest": gate_digest,
        "prerequisite_evidence_run_digest": PREREQUISITE_RUN_DIGEST,
        "raw_authority_retained": False,
        "source_authority_commit_digest": assembler.SOURCE_COMMIT_DIGEST,
        "status": "passed",
    }


def _artifact(kind: ArtifactKind, label: str) -> dict[str, object]:
    return {
        "artifact_digest": _digest(label),
        "artifact_format": ArtifactFormat.JSON.value,
        "byte_count": 1,
        "kind": kind.value,
        "redaction_verified": True,
    }


def _prerequisite_records(
    *,
    deployment_digest: str = DEPLOYMENT_DIGEST,
    run_digest: str = PREREQUISITE_RUN_DIGEST,
    recorded_at: str = "2026-08-29T22:24:03Z",
) -> list[dict[str, object]]:
    gate = next(
        gate
        for gate in phase66_acceptance_manifest().gates
        if gate.gate_id == assembler.EXECUTION_PREREQUISITE_GATE_ID
    )
    return [
        {
            "actor_digests": [_digest("upload-actor")],
            "artifacts": [
                _artifact(ArtifactKind.CANARY_SUMMARY, "upload-canary"),
                _artifact(ArtifactKind.LOG_AUDIT, "upload-log"),
            ],
            "assertions": [
                {
                    "assertion_id": assertion_id,
                    "observation_digest": _digest(f"upload-{assertion_id}"),
                    "observed_count": 0 if assertion_id == "provider_call_count_is_zero" else 1,
                    "passed": True,
                }
                for assertion_id in gate.required_assertions
            ],
            "correlation_digest": None,
            "deployment_digest": deployment_digest,
            "evidence_class": AcceptanceEvidenceClass.DEPLOYED_NON_DESTRUCTIVE.value,
            "gate_id": assembler.EXECUTION_PREREQUISITE_GATE_ID,
            "job_digest": _digest("upload-job"),
            "manifest_digest": phase66_manifest_digest(),
            "moderated_session": None,
            "outcome": "passed",
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
            "source_commit_digest": assembler.SOURCE_COMMIT_DIGEST,
            "work_digest": None,
        }
    ]


@pytest.fixture
def private_workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    repository = tmp_path / "repository"
    workspace = repository / ".mr_lister_private" / "phase66-acceptance"
    repository.mkdir()
    workspace.mkdir(mode=0o700, parents=True)
    (repository / ".mr_lister_private").chmod(0o700)
    monkeypatch.setattr(assembler, "REPOSITORY_ROOT", repository)
    monkeypatch.setattr(assembler, "PRIVATE_WORKSPACE_ROOT", workspace)
    return workspace


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode() + b"\n"


def _write_private(path: Path, value: object) -> tuple[str, int]:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    current = path.parent
    while current.name != ".mr_lister_private":
        current.chmod(0o700)
        current = current.parent
    contents = _canonical(value)
    path.write_bytes(contents)
    path.chmod(0o600)
    return sha256(contents).hexdigest(), len(contents)


def _inputs(
    workspace: Path,
    *,
    gate: dict[str, object] | None = None,
    canary_mutator: Any = None,
    log_mutator: Any = None,
    prerequisite: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    root = workspace / f"inputs-{len(tuple(workspace.iterdir()))}"
    gate_value = gate or _gate()
    gate_path = root / "gate.json"
    gate_digest, _ = _write_private(gate_path, gate_value)
    canary_value = _canary(gate_digest)
    log_value = _log(gate_digest)
    if canary_mutator is not None:
        canary_mutator(canary_value)
    if log_mutator is not None:
        log_mutator(log_value)
    canary_path = root / "canary-summary.json"
    canary_digest, canary_size = _write_private(canary_path, canary_value)
    log_path = root / "log-audit.json"
    log_digest, log_size = _write_private(log_path, log_value)
    prerequisite_path = root / "records.json"
    prerequisite_digest, prerequisite_size = _write_private(
        prerequisite_path,
        prerequisite or _prerequisite_records(),
    )
    return {
        "gate_path": gate_path,
        "gate_sha256": gate_digest,
        "canary_summary_path": canary_path,
        "canary_summary_sha256": canary_digest,
        "canary_summary_size": canary_size,
        "log_audit_path": log_path,
        "log_audit_sha256": log_digest,
        "log_audit_size": log_size,
        "prerequisite_records_path": prerequisite_path,
        "prerequisite_records_sha256": prerequisite_digest,
        "prerequisite_records_size": prerequisite_size,
    }


def _assemble(
    workspace: Path,
    *,
    run_name: str = "normalized-fragment",
    inputs: dict[str, object] | None = None,
    **overrides: object,
) -> tuple[Path, dict[str, object]]:
    arguments: dict[str, object] = {
        "run_root": workspace / run_name,
        "source_commit": assembler.SOURCE_COMMIT,
        "source_commit_digest": assembler.SOURCE_COMMIT_DIGEST,
        **(inputs or _inputs(workspace)),
        **overrides,
    }
    result = assembler.prepare_phase66_outbox_recovery_evidence(**arguments)
    return arguments["run_root"], result  # type: ignore[return-value]


def _load(path: Path) -> object:
    return json.loads(path.read_bytes())


def test_assembler_emits_exact_verified_private_fragment(private_workspace: Path) -> None:
    inputs = _inputs(private_workspace)
    source_bytes = {
        path.name: path.read_bytes()
        for path in (
            inputs["gate_path"],
            inputs["canary_summary_path"],
            inputs["log_audit_path"],
            inputs["prerequisite_records_path"],
        )
        if isinstance(path, Path)
    }
    run_root, result = _assemble(private_workspace, inputs=inputs)

    assert {path.name for path in run_root.iterdir()} == set(assembler._OUTPUT_FILENAMES)
    assert os.stat(run_root, follow_symlinks=False).st_mode & 0o777 == 0o700
    assert all(
        os.stat(run_root / filename, follow_symlinks=False).st_mode & 0o777 == 0o600
        for filename in assembler._OUTPUT_FILENAMES
    )
    records_value = _load(run_root / assembler.RECORDS_FILENAME)
    files_value = _load(run_root / assembler.ARTIFACT_FILES_FILENAME)
    assert isinstance(records_value, list) and isinstance(files_value, list)
    records = _validated_records(records_value)
    record = validate_phase66_evidence(records_value[0])
    assert record.gate_id == assembler.GATE_ID
    assert record.recorded_at.isoformat() == "2026-08-29T22:32:03+00:00"
    assert record.deployment_digest == DEPLOYMENT_DIGEST
    assert record.source_commit_digest == assembler.SOURCE_COMMIT_DIGEST
    assert tuple(item.assertion_id for item in record.assertions) == assembler._EXPECTED_ASSERTIONS
    assert tuple(item.observed_count for item in record.assertions) == (1, 1, 1, 1, 1, 1, 0)
    assert all(item.passed for item in record.assertions)
    declared = _declared_artifacts(records)
    files = _validated_artifact_files(files_value)
    assert _verify_artifacts(declared, files, run_root) == sum(
        artifact.byte_count for artifact in record.artifacts
    )
    assert result["execution_digest"] == EXECUTION_DIGEST
    assert result["run_digest"] == record.run_digest
    output = b"".join(path.read_bytes() for path in sorted(run_root.iterdir()))
    assert NAMESPACE_SEED.encode() not in output
    assert source_bytes == {
        path.name: path.read_bytes()
        for path in (
            inputs["gate_path"],
            inputs["canary_summary_path"],
            inputs["log_audit_path"],
            inputs["prerequisite_records_path"],
        )
        if isinstance(path, Path)
    }


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("gate_sha256", "0" * 64),
        ("canary_summary_sha256", "1" * 64),
        ("log_audit_sha256", "2" * 64),
        ("prerequisite_records_sha256", "3" * 64),
        ("canary_summary_size", 1),
        ("log_audit_size", 1),
        ("prerequisite_records_size", 1),
    ],
)
def test_every_exact_file_binding_fails_closed(
    private_workspace: Path,
    field: str,
    replacement: object,
) -> None:
    inputs = _inputs(private_workspace)
    inputs[field] = replacement
    with pytest.raises(assembler.Phase66OutboxRecoveryEvidenceError):
        _assemble(private_workspace, inputs=inputs)
    assert not (private_workspace / "normalized-fragment").exists()


@pytest.mark.parametrize(
    ("target", "field", "replacement"),
    [
        ("canary", "deployment_digest", _digest("wrong-deployment")),
        ("canary", "prerequisite_evidence_run_digest", _digest("wrong-prerequisite")),
        ("canary", "source_authority_commit_digest", _digest("wrong-source")),
        ("log", "source_authority_commit_digest", _digest("wrong-log-source")),
        ("canary", "redaction_verified", False),
        ("log", "raw_authority_retained", True),
    ],
)
def test_raw_contract_or_cross_binding_drift_fails_closed(
    private_workspace: Path,
    target: str,
    field: str,
    replacement: object,
) -> None:
    def mutate(value: dict[str, object]) -> None:
        value[field] = replacement

    with pytest.raises(assembler.Phase66OutboxRecoveryEvidenceError):
        _assemble(
            private_workspace,
            inputs=_inputs(
                private_workspace,
                canary_mutator=mutate if target == "canary" else None,
                log_mutator=mutate if target == "log" else None,
            ),
        )


def test_failed_assertion_or_unexpected_delta_fails_closed(private_workspace: Path) -> None:
    def fail_assertion(value: dict[str, object]) -> None:
        assertions = value["assertions"]
        assert isinstance(assertions, dict)
        assertions["stuck_execution_recovery_passes"] = False

    with pytest.raises(assembler.Phase66OutboxRecoveryEvidenceError):
        _assemble(
            private_workspace,
            inputs=_inputs(private_workspace, canary_mutator=fail_assertion),
        )

    def add_delta(value: dict[str, object]) -> None:
        deltas = value["deltas"]
        assert isinstance(deltas, dict)
        deltas["provider_calls"] = 1

    with pytest.raises(assembler.Phase66OutboxRecoveryEvidenceError):
        _assemble(
            private_workspace,
            run_name="second-output",
            inputs=_inputs(private_workspace, log_mutator=add_delta),
        )


def test_prerequisite_must_be_exact_and_earlier(private_workspace: Path) -> None:
    with pytest.raises(assembler.Phase66OutboxRecoveryEvidenceError, match="exact passed upload"):
        _assemble(
            private_workspace,
            inputs=_inputs(
                private_workspace,
                prerequisite=_prerequisite_records(run_digest=_digest("other-upload-run")),
            ),
        )
    with pytest.raises(assembler.Phase66OutboxRecoveryEvidenceError, match="predates"):
        _assemble(
            private_workspace,
            run_name="later-output",
            inputs=_inputs(
                private_workspace,
                prerequisite=_prerequisite_records(recorded_at="2026-08-29T22:31:01Z"),
            ),
        )


def test_shared_execution_authority_is_replay_stable_and_not_operator_mintable(
    private_workspace: Path,
) -> None:
    inputs = _inputs(private_workspace)
    first_root, first = _assemble(private_workspace, run_name="first", inputs=inputs)
    second_root, second = _assemble(private_workspace, run_name="second", inputs=inputs)
    first_record = validate_phase66_evidence(_load(first_root / assembler.RECORDS_FILENAME)[0])
    second_record = validate_phase66_evidence(_load(second_root / assembler.RECORDS_FILENAME)[0])
    assert first["run_digest"] == second["run_digest"]
    assert first["record_digest"] == second["record_digest"]
    assert first_record.recorded_at == second_record.recorded_at
    assert "--recorded-at" not in assembler._parser().format_help()


def test_mismatched_future_or_legacy_execution_authority_fails_closed(
    private_workspace: Path,
) -> None:
    def mismatch(value: dict[str, object]) -> None:
        execution = value["execution_authority"]
        assert isinstance(execution, dict)
        execution["execution_digest"] = _digest("other-execution")

    with pytest.raises(assembler.Phase66OutboxRecoveryEvidenceError, match="one execution"):
        _assemble(private_workspace, inputs=_inputs(private_workspace, log_mutator=mismatch))

    def future(value: dict[str, object]) -> None:
        execution = value["execution_authority"]
        assert isinstance(execution, dict)
        execution["started_at"] = "2099-08-29T22:31:00Z"
        execution["completed_at"] = "2099-08-29T22:32:00Z"

    with pytest.raises(assembler.Phase66OutboxRecoveryEvidenceError, match="future"):
        _assemble(
            private_workspace,
            run_name="future-output",
            inputs=_inputs(private_workspace, canary_mutator=future, log_mutator=future),
        )

    def legacy_canary(value: dict[str, object]) -> None:
        value["artifact_contract"] = "phase6.6-deployed-outbox-recovery-canary-summary-v1"
        value.pop("execution_authority")

    def legacy_log(value: dict[str, object]) -> None:
        value["artifact_contract"] = "phase6.6-deployed-outbox-recovery-log-audit-v1"
        value.pop("execution_authority")

    with pytest.raises(assembler.Phase66OutboxRecoveryEvidenceError, match="closed contracts"):
        _assemble(
            private_workspace,
            run_name="legacy-output",
            inputs=_inputs(
                private_workspace,
                canary_mutator=legacy_canary,
                log_mutator=legacy_log,
            ),
        )


def test_output_is_fresh_confined_and_inputs_do_not_follow_symlinks(
    private_workspace: Path,
) -> None:
    inputs = _inputs(private_workspace)
    existing = private_workspace / "existing-output"
    existing.mkdir(mode=0o700)
    marker = existing / "marker.json"
    marker.write_bytes(b"{}\n")
    marker.chmod(0o600)
    with pytest.raises(assembler.Phase66OutboxRecoveryEvidenceError, match="fresh"):
        _assemble(private_workspace, run_name="existing-output", inputs=inputs)
    assert marker.read_bytes() == b"{}\n"

    target = inputs["canary_summary_path"]
    assert isinstance(target, Path)
    linked = target.with_name("linked-canary.json")
    linked.symlink_to(target)
    inputs["canary_summary_path"] = linked
    with pytest.raises(assembler.Phase66OutboxRecoveryEvidenceError, match="regular file"):
        _assemble(private_workspace, run_name="symlink-output", inputs=inputs)

    with pytest.raises(assembler.Phase66OutboxRecoveryEvidenceError, match="must stay"):
        _assemble(private_workspace, run_root=private_workspace.parent / "escaped")


def test_parent_symlink_swap_cannot_redirect_input_or_output(
    private_workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inputs = _inputs(private_workspace)
    target = inputs["canary_summary_path"]
    expected_digest = inputs["canary_summary_sha256"]
    expected_size = inputs["canary_summary_size"]
    assert isinstance(target, Path) and isinstance(expected_digest, str)
    assert isinstance(expected_size, int)
    parent = target.parent
    relocated = parent.with_name(f"{parent.name}-relocated")
    outside = private_workspace / "swap-target"
    outside.mkdir(mode=0o700)
    decoy = outside / target.name
    decoy.write_bytes(b"{}\n")
    decoy.chmod(0o600)
    real_open = os.open
    swapped = False

    def swapping_open(
        path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        nonlocal swapped
        if path == target.name and dir_fd is not None and not swapped:
            swapped = True
            parent.rename(relocated)
            parent.symlink_to(outside, target_is_directory=True)
            try:
                return real_open(path, flags, mode, dir_fd=dir_fd)
            finally:
                parent.unlink()
                relocated.rename(parent)
        return real_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(assembler.os, "open", swapping_open)
    observed = assembler._read_exact_input(
        target,
        expected_digest=expected_digest,
        expected_size=expected_size,
    )
    assert swapped is True and observed.digest == expected_digest
    assert decoy.read_bytes() == b"{}\n"

    monkeypatch.setattr(assembler.os, "open", real_open)
    output = private_workspace / "swap-output"
    output_relocated = private_workspace / "swap-output-relocated"
    output_target = private_workspace / "swap-output-target"
    output_target.mkdir(mode=0o700)
    real_link = os.link
    output_swapped = False

    def swapping_link(
        src: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        dst: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        *,
        src_dir_fd: int | None = None,
        dst_dir_fd: int | None = None,
        follow_symlinks: bool = True,
    ) -> None:
        nonlocal output_swapped
        if dst == "proof.json" and not output_swapped:
            output_swapped = True
            output.rename(output_relocated)
            output.symlink_to(output_target, target_is_directory=True)
            try:
                real_link(
                    src,
                    dst,
                    src_dir_fd=src_dir_fd,
                    dst_dir_fd=dst_dir_fd,
                    follow_symlinks=follow_symlinks,
                )
            finally:
                output.unlink()
                output_relocated.rename(output)
            return
        real_link(
            src,
            dst,
            src_dir_fd=src_dir_fd,
            dst_dir_fd=dst_dir_fd,
            follow_symlinks=follow_symlinks,
        )

    monkeypatch.setattr(assembler.os, "link", swapping_link)
    with assembler._create_fresh_output_root(output) as (_path, descriptor):
        assembler._write_once(descriptor, "proof.json", b"{}\n")
    assert output_swapped is True
    assert (output / "proof.json").read_bytes() == b"{}\n"
    assert not (output_target / "proof.json").exists()


def test_source_gate_shape_and_import_surface_are_closed(private_workspace: Path) -> None:
    with pytest.raises(assembler.Phase66OutboxRecoveryEvidenceError, match="source authority"):
        _assemble(
            private_workspace,
            source_commit="0" * 40,
            source_commit_digest=_digest("wrong-source"),
        )

    gate = deepcopy(_gate())
    baseline = gate["baseline"]
    assert isinstance(baseline, dict)
    baseline["local_path"] = "/private/path"
    with pytest.raises(assembler.Phase66OutboxRecoveryEvidenceError, match="closed contracts"):
        _assemble(
            private_workspace,
            run_name="closed-gate-output",
            inputs=_inputs(private_workspace, gate=gate),
        )

    tree = ast.parse(Path(assembler.__file__).read_text(encoding="utf-8"))
    imported_roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_roots.update(alias.name.partition(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported_roots.add(node.module.partition(".")[0])
    assert imported_roots.isdisjoint(
        {"boto3", "botocore", "urllib", "httpx", "requests", "playwright", "selenium"}
    )
