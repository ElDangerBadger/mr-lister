from __future__ import annotations

import json
import os
from hashlib import sha256
from pathlib import Path

import pytest

from mr_lister.acceptance.evidence_set import Phase66EvidenceSetVerification
from mr_lister.acceptance.phase6 import phase66_manifest_digest
from mr_lister.control.models import PHASE6_MAX_SOURCE_ARTWORK_BYTES
from mr_lister.control.source_artwork import verify_phase6_source_artwork
from tools import phase66_live_acceptance as live_acceptance


def _digest(label: str) -> str:
    return sha256(label.encode()).hexdigest()


@pytest.fixture
def private_workspace(tmp_path: Path, monkeypatch) -> Path:
    repository = tmp_path / "repository"
    workspace = repository / ".mr_lister_private" / "phase66-acceptance"
    repository.mkdir()
    monkeypatch.setattr(live_acceptance, "REPOSITORY_ROOT", repository)
    monkeypatch.setattr(live_acceptance, "PRIVATE_WORKSPACE_ROOT", workspace)
    return workspace


def test_exact_canary_png_is_five_mib_and_passes_phase6_authority() -> None:
    contents = live_acceptance.exact_phase66_canary_png()

    assert len(contents) == PHASE6_MAX_SOURCE_ARTWORK_BYTES
    verified = verify_phase6_source_artwork(
        filename=live_acceptance.CANARY_FILENAME,
        content_type="image/png",
        content=contents,
        expected_sha256=sha256(contents).hexdigest(),
        expected_size_bytes=len(contents),
    )
    assert (verified.width, verified.height) == (512, 512)
    assert (verified.alpha_minimum, verified.alpha_maximum) == (0, 255)


def test_canary_output_is_private_and_reports_no_local_path(
    private_workspace: Path,
    capsys,
) -> None:
    run_root = private_workspace / "run-a"
    output = run_root / live_acceptance.CANARY_FILENAME

    assert live_acceptance.main(["make-canary-png", str(run_root)]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["result"] == "passed"
    assert payload["byte_count"] == PHASE6_MAX_SOURCE_ARTWORK_BYTES
    assert str(private_workspace) not in json.dumps(payload)
    assert output.stat().st_size == PHASE6_MAX_SOURCE_ARTWORK_BYTES
    assert stat_mode(output) == 0o600
    assert stat_mode(output.parent) == 0o700


def test_canary_rejects_output_outside_private_workspace(
    private_workspace: Path,
) -> None:
    with pytest.raises(live_acceptance.Phase66LiveAcceptanceError, match="must stay inside"):
        live_acceptance.write_exact_canary_png(private_workspace.parent / "escaped")


def test_canary_cannot_overwrite_a_bundle_control_file(private_workspace: Path) -> None:
    run_root = private_workspace / "run-control"
    live_acceptance._write_atomic(run_root / live_acceptance.RECORDS_FILENAME, b"[]\n")

    live_acceptance.write_exact_canary_png(run_root)

    assert (run_root / live_acceptance.RECORDS_FILENAME).read_bytes() == b"[]\n"
    assert (run_root / live_acceptance.CANARY_FILENAME).stat().st_size == (
        PHASE6_MAX_SOURCE_ARTWORK_BYTES
    )


def test_json_loader_rejects_duplicate_members(private_workspace: Path) -> None:
    bundle = private_workspace / "run-duplicate"
    live_acceptance._ensure_private_directory(bundle)
    path = bundle / live_acceptance.RECORDS_FILENAME
    live_acceptance._write_atomic(path, b'{"records":[],"records":[1]}')

    with pytest.raises(live_acceptance.Phase66LiveAcceptanceError, match="strict JSON"):
        live_acceptance._load_json(path)


def test_verify_bundle_loads_closed_indexes_and_returns_closed_result(
    private_workspace: Path,
    monkeypatch,
) -> None:
    bundle = private_workspace / "run-verify"
    live_acceptance._write_atomic(bundle / live_acceptance.RECORDS_FILENAME, b"[]\n")
    live_acceptance._write_atomic(bundle / live_acceptance.ARTIFACT_INDEX_FILENAME, b"[]\n")
    observed: dict[str, object] = {}

    def fake_verify(records, artifact_files, *, allowed_artifact_root):
        observed["records"] = records
        observed["artifact_files"] = artifact_files
        observed["root"] = allowed_artifact_root
        return Phase66EvidenceSetVerification(
            manifest_digest=phase66_manifest_digest(),
            evidence_set_digest=_digest("evidence-set"),
            gate_set_digest=_digest("gate-set"),
            source_commit_digest=_digest("source-commit"),
            run_set_digest=_digest("run-set"),
            deployment_digest=_digest("deployment"),
            record_count=11,
            gate_count=11,
            blocking_gate_count=11,
            artifact_count=20,
            artifact_byte_count=1234,
            job_binding_count=3,
            run_count=10,
        )

    monkeypatch.setattr(live_acceptance, "verify_phase66_evidence_set", fake_verify)

    result = live_acceptance.verify_bundle(bundle)

    assert result["result"] == "passed"
    assert result["record_count"] == 11
    assert observed == {"records": [], "artifact_files": [], "root": bundle}


def test_verify_bundle_rejects_symlinked_control_file(
    private_workspace: Path,
) -> None:
    bundle = private_workspace / "run-symlink"
    live_acceptance._ensure_private_directory(bundle)
    outside = private_workspace / "outside.json"
    live_acceptance._write_atomic(outside, b"[]\n")
    (bundle / live_acceptance.RECORDS_FILENAME).symlink_to(outside)
    live_acceptance._write_atomic(bundle / live_acceptance.ARTIFACT_INDEX_FILENAME, b"[]\n")

    with pytest.raises(live_acceptance.Phase66LiveAcceptanceError, match="regular file"):
        live_acceptance.verify_bundle(bundle)


def test_verify_missing_bundle_is_read_only(private_workspace: Path) -> None:
    bundle = private_workspace / "missing-run"

    with pytest.raises(live_acceptance.Phase66LiveAcceptanceError, match="unavailable"):
        live_acceptance.verify_bundle(bundle)

    assert not bundle.exists()


def test_real_verifier_rejects_an_incomplete_bundle(private_workspace: Path) -> None:
    bundle = private_workspace / "run-incomplete"
    live_acceptance._write_atomic(bundle / live_acceptance.RECORDS_FILENAME, b"[]\n")
    live_acceptance._write_atomic(bundle / live_acceptance.ARTIFACT_INDEX_FILENAME, b"[]\n")

    with pytest.raises(live_acceptance.Phase66LiveAcceptanceError, match="incomplete or invalid"):
        live_acceptance.verify_bundle(bundle)


def stat_mode(path: Path) -> int:
    return os.stat(path, follow_symlinks=False).st_mode & 0o777
