"""Hermetic source-builder gates for the P7.18 enabled artifact."""

from __future__ import annotations

from hashlib import sha256
from pathlib import Path

import pytest

from mr_lister.release.phase718 import (
    PHASE718_CONTRACT_FINGERPRINT,
    PHASE718_CONTRACT_PATH,
    PHASE718_ENTRYPOINTS,
    PHASE718_SOURCE_MANIFEST_FILENAME,
)
from tools.build_phase718_enabled_release import (
    ENABLED_SOURCE_DIRECTORY_NAME,
    Phase718EnabledReleaseError,
    build_enabled_source_bundle,
    resolve_enabled_import_closure,
    verify_enabled_source_bundle,
)


def _build(destination: Path) -> Path:
    return build_enabled_source_bundle(
        destination / ENABLED_SOURCE_DIRECTORY_NAME,
        application_release_fingerprint="a" * 64,
        canary_evidence_fingerprint="b" * 64,
        enablement_evidence_fingerprint="c" * 64,
        state_table="mr-lister-phase6-dev",
    )


def test_enabled_source_builder_is_hermetic_and_deterministic(tmp_path: Path) -> None:
    first = _build(tmp_path / "first")
    second = _build(tmp_path / "second")

    first_manifest = (first / PHASE718_SOURCE_MANIFEST_FILENAME).read_bytes()
    second_manifest = (second / PHASE718_SOURCE_MANIFEST_FILENAME).read_bytes()
    assert first_manifest == second_manifest
    assert verify_enabled_source_bundle(first)["entrypoints"] == list(PHASE718_ENTRYPOINTS)
    assert sha256((first / PHASE718_CONTRACT_PATH).read_bytes()).hexdigest() == (
        PHASE718_CONTRACT_FINGERPRINT
    )
    assert not any(".mr_lister_private" in path.parts for path in first.rglob("*"))


def test_enabled_import_closure_starts_at_release_first_entrypoints_only() -> None:
    closure = resolve_enabled_import_closure()

    assert "mr_lister.cloud.phase718_entrypoints" in closure
    assert "mr_lister.release.phase718" in closure
    assert "mr_lister.cloud.phase6_entrypoints" not in closure
    assert "mr_lister.cloud.phase7_production_entrypoints" not in closure
    assert all(path.is_file() and not path.is_symlink() for path in closure.values())


def test_enabled_source_verifier_rejects_one_changed_runtime_byte(tmp_path: Path) -> None:
    source = _build(tmp_path)
    entrypoint = source / "mr_lister/cloud/phase718_entrypoints.py"
    entrypoint.write_bytes(entrypoint.read_bytes() + b"\n# drift\n")

    with pytest.raises(Phase718EnabledReleaseError) as captured:
        verify_enabled_source_bundle(source)

    assert str(captured.value) == "Phase 7.18 enabled release build is invalid"
    assert captured.value.__cause__ is None
