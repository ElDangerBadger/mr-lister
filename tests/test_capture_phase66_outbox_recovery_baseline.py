from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from mr_lister.control.models import WorkType
from tools import capture_phase66_outbox_recovery_baseline as capture
from tools import phase66_deployed_outbox_recovery_smoke as smoke


def _authority() -> smoke.DeploymentAuthority:
    return smoke.DeploymentAuthority(
        table_name="private-table",
        artifact_bucket="private-bucket",
        functions={},
        state_machine_arns={WorkType.PREPARE: "private-machine"},
    )


def _snapshot(**changes: Any) -> smoke.LiveSnapshot:
    value = smoke.LiveSnapshot(
        application_record_count=44,
        application_record_digest="1" * 64,
        provider_record_count=0,
        dispatched_work_count=0,
        running_execution_count=0,
        execution_digests=("2" * 64,),
        source_version_count=2,
        source_inventory_digest="3" * 64,
        referenced_version_count=2,
        pinned_version_count=2,
        retention_checkpoint_present=True,
        authority=_authority(),
    )
    return replace(value, **changes)


def _seed_document() -> dict[str, object]:
    return {
        "authorization_contract": smoke.GATE_CONTRACT,
        "deployment_digest": "d" * 64,
        "gate_id": smoke.GATE_ID,
        "gate_seed_contract": capture.GATE_SEED_CONTRACT,
        "method_authorization": dict(smoke._EXPECTED_METHOD_AUTHORIZATION),
        "namespace_nonce": "8" * 64,
        "prerequisite_evidence_run_digest": "e" * 64,
        "source_authority_commit": smoke.SOURCE_AUTHORITY_COMMIT,
        "source_authority_commit_digest": smoke.SOURCE_AUTHORITY_COMMIT_DIGEST,
    }


def _private_workspace(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    repository = tmp_path / "repository"
    private = repository / ".mr_lister_private" / "phase66-acceptance"
    private.mkdir(parents=True, mode=0o700)
    repository.chmod(0o700)
    (repository / ".mr_lister_private").chmod(0o700)
    private.chmod(0o700)
    monkeypatch.setattr(capture, "REPOSITORY_ROOT", repository)
    monkeypatch.setattr(capture, "PRIVATE_ROOT", private)
    monkeypatch.setattr(smoke, "REPOSITORY_ROOT", repository)
    monkeypatch.setattr(smoke, "PRIVATE_ROOT", private)
    return private


def _write(path: Path, value: object) -> str:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.parent.chmod(0o700)
    payload = smoke._canonical_json(value, pretty=True)
    path.write_bytes(payload)
    path.chmod(0o600)
    return smoke._digest_bytes(payload)


class FakeBackend:
    def __init__(self, expected_seed: str) -> None:
        self.expected_seed = expected_seed
        self.calls = 0

    def capture_baseline(self, canary: smoke.CanaryAuthority) -> tuple[smoke.LiveSnapshot, bool]:
        self.calls += 1
        assert canary == smoke.derive_canary(self.expected_seed)
        return _snapshot(), True


def test_capture_writes_one_sanitized_owner_only_gate_baseline(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    private = _private_workspace(monkeypatch, tmp_path)
    seed_path = private / "inputs" / "seed.json"
    deployment_path = private / "inputs" / "deployment.json"
    prerequisite_path = private / "inputs" / "prerequisite.json"
    output = private / "run" / "outbox-gate.json"
    seed_digest = _write(seed_path, _seed_document())
    deployment_digest = _write(deployment_path, {})
    prerequisite_digest = _write(prerequisite_path, [])
    monkeypatch.setattr(
        capture,
        "_deployment",
        lambda _value, _seed: SimpleNamespace(captured_at="2026-08-29T22:00:00Z"),
    )
    monkeypatch.setattr(
        capture,
        "_prerequisite",
        lambda _value, _seed: SimpleNamespace(recorded_at=datetime(2026, 8, 29, 22, 5, tzinfo=UTC)),
    )
    backend = FakeBackend(seed_digest)

    result = capture.capture_phase66_outbox_recovery_baseline(
        gate_seed_path=seed_path,
        gate_seed_sha256=seed_digest,
        deployment_authority_path=deployment_path,
        deployment_authority_sha256=deployment_digest,
        prerequisite_records_path=prerequisite_path,
        prerequisite_records_sha256=prerequisite_digest,
        output_path=output,
        backend_factory=lambda: backend,
        clock=lambda: datetime(2026, 8, 29, 22, 10, tzinfo=UTC),
    )

    assert result["result"] == "passed"
    assert result["gate_seed_digest"] == seed_digest
    assert result["gate_sha256"] == smoke._digest_bytes(output.read_bytes())
    assert backend.calls == 1
    assert output.stat().st_mode & 0o777 == 0o600
    document = json.loads(output.read_text())
    assert set(document) == {
        "authorization_contract",
        "baseline",
        "deployment_digest",
        "exact_write_budget",
        "gate_id",
        "method_authorization",
        "prerequisite_evidence_run_digest",
        "source_authority_commit",
        "source_authority_commit_digest",
        "synthetic_namespace_seed",
    }
    assert document["authorization_contract"] == smoke.GATE_CONTRACT
    assert document["synthetic_namespace_seed"] == seed_digest
    assert document["baseline"] == _snapshot().gate_baseline(
        synthetic_namespace_absent=True,
        synthetic_namespace_seed=seed_digest,
    )
    assert document["exact_write_budget"]["s3_version_tag_writes"] == 2
    assert smoke.load_run_gate(output, result["gate_sha256"]).baseline == document["baseline"]
    rendered = output.read_bytes()
    canary = smoke.derive_canary(seed_digest)
    assert all(value.encode() not in rendered for value in canary.sensitive_values)
    with pytest.raises(capture.BaselineCaptureError, match="fresh mode-0600"):
        capture.capture_phase66_outbox_recovery_baseline(
            gate_seed_path=seed_path,
            gate_seed_sha256=seed_digest,
            deployment_authority_path=deployment_path,
            deployment_authority_sha256=deployment_digest,
            prerequisite_records_path=prerequisite_path,
            prerequisite_records_sha256=prerequisite_digest,
            output_path=output,
            backend_factory=lambda: backend,
            clock=lambda: datetime(2026, 8, 29, 22, 10, tzinfo=UTC),
        )


def test_local_authority_failure_happens_before_backend_construction(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    private = _private_workspace(monkeypatch, tmp_path)
    seed_path = private / "seed.json"
    deployment_path = private / "deployment.json"
    prerequisite_path = private / "prerequisite.json"
    seed_digest = _write(seed_path, _seed_document())
    _write(deployment_path, {})
    prerequisite_digest = _write(prerequisite_path, [])
    constructed = False

    def forbidden_factory() -> FakeBackend:
        nonlocal constructed
        constructed = True
        raise AssertionError("backend must not be constructed")

    with pytest.raises(capture.BaselineCaptureError, match="changed or does not match"):
        capture.capture_phase66_outbox_recovery_baseline(
            gate_seed_path=seed_path,
            gate_seed_sha256=seed_digest,
            deployment_authority_path=deployment_path,
            deployment_authority_sha256="0" * 64,
            prerequisite_records_path=prerequisite_path,
            prerequisite_records_sha256=prerequisite_digest,
            output_path=private / "output.json",
            backend_factory=forbidden_factory,
        )
    assert constructed is False


def test_gate_seed_is_exact_and_binds_the_v2_smoke_contract() -> None:
    seed = _seed_document()
    parsed = capture._gate_seed(seed, "a" * 64)
    assert parsed.deployment_digest == "d" * 64
    assert parsed.prerequisite_digest == "e" * 64

    seed["authorization_contract"] = "phase6.6-deployed-outbox-recovery-run-gate-v1"
    with pytest.raises(capture.BaselineCaptureError, match="frozen smoke authority"):
        capture._gate_seed(seed, "a" * 64)

    seed = _seed_document()
    seed["extra"] = True
    with pytest.raises(capture.BaselineCaptureError, match="exact closed"):
        capture._gate_seed(seed, "a" * 64)


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"provider_record_count": 1}, "provider-zero"),
        ({"dispatched_work_count": 1}, "provider-zero"),
        ({"running_execution_count": 1}, "provider-zero"),
        ({"retention_checkpoint_present": False}, "provider-zero"),
        ({"referenced_version_count": 1}, "complete, referenced, pinned"),
        ({"pinned_version_count": 1}, "complete, referenced, pinned"),
    ],
)
def test_unsafe_baseline_is_rejected(changes: dict[str, object], message: str) -> None:
    snapshot = _snapshot(**changes)
    baseline = snapshot.gate_baseline(
        synthetic_namespace_absent=True,
        synthetic_namespace_seed="a" * 64,
    )
    with pytest.raises(capture.BaselineCaptureError, match=message):
        capture._verify_baseline(baseline)


def test_synthetic_namespace_presence_is_rejected() -> None:
    baseline = _snapshot().gate_baseline(
        synthetic_namespace_absent=False,
        synthetic_namespace_seed="a" * 64,
    )
    with pytest.raises(capture.BaselineCaptureError, match="provider-zero"):
        capture._verify_baseline(baseline)


def test_input_parent_swap_reads_only_the_open_confined_directory(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    private = _private_workspace(monkeypatch, tmp_path)
    input_directory = private / "inputs"
    replacement = private / "replacement"
    original = private / "inputs-original"
    seed_path = input_directory / "seed.json"
    expected_digest = _write(seed_path, _seed_document())
    _write(replacement / "seed.json", {"wrong": True})
    real_open = capture.os.open
    swapped = False

    def swapping_open(
        path: str | bytes | Path,
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        nonlocal swapped
        if path == "seed.json" and dir_fd is not None and not swapped:
            swapped = True
            input_directory.rename(original)
            input_directory.symlink_to(replacement, target_is_directory=True)
        return real_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(capture.os, "open", swapping_open)
    assert capture._read_exact_json(seed_path, expected_digest, "gate seed") == _seed_document()
    assert swapped is True


def test_output_parent_swap_cannot_redirect_or_leave_a_gate(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    private = _private_workspace(monkeypatch, tmp_path)
    output_directory = private / "run"
    moved_directory = private / "run-original"
    replacement = private / "replacement"
    output_directory.mkdir(mode=0o700)
    replacement.mkdir(mode=0o700)
    output = output_directory / "gate.json"
    real_link = capture.os.link
    swapped = False

    def swapping_link(
        source: str,
        destination: str,
        *,
        src_dir_fd: int | None = None,
        dst_dir_fd: int | None = None,
        follow_symlinks: bool = True,
    ) -> None:
        nonlocal swapped
        if not swapped:
            swapped = True
            output_directory.rename(moved_directory)
            output_directory.symlink_to(replacement, target_is_directory=True)
        real_link(
            source,
            destination,
            src_dir_fd=src_dir_fd,
            dst_dir_fd=dst_dir_fd,
            follow_symlinks=follow_symlinks,
        )

    monkeypatch.setattr(capture.os, "link", swapping_link)
    with pytest.raises(capture.BaselineCaptureError, match="fresh mode-0600"):
        capture._write_once(output, {"gate": True})

    assert swapped is True
    assert not (moved_directory / "gate.json").exists()
    assert not (replacement / "gate.json").exists()


def test_existing_output_is_never_overwritten(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    private = _private_workspace(monkeypatch, tmp_path)
    output = private / "run" / "gate.json"
    original_digest = _write(output, {"original": True})

    with pytest.raises(capture.BaselineCaptureError, match="fresh mode-0600"):
        capture._write_once(output, {"replacement": True})

    assert smoke._digest_bytes(output.read_bytes()) == original_digest
    assert json.loads(output.read_text()) == {"original": True}
