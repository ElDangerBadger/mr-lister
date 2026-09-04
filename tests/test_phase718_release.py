"""Adversarial release-first checks for the P7.18 enabled runtime."""

from __future__ import annotations

import shutil
from hashlib import sha256
from pathlib import Path

import pytest

from mr_lister.release.phase6 import LINUX_ARM64_TARGET, render_manifest
from mr_lister.release.phase718 import (
    DEPENDENCY_MANIFEST_FILENAME,
    PHASE718_BINDING_FILENAME,
    PHASE718_CONTRACT_FINGERPRINT,
    PHASE718_CONTRACT_PATH,
    PHASE718_DEPLOYMENT_MANIFEST_FILENAME,
    PHASE718_ENTRYPOINTS,
    PHASE718_PROFILE_FINGERPRINT,
    PHASE718_PROFILE_PATH,
    PHASE718_RELEASE_MANIFEST_FILENAME,
    PHASE718_SOURCE_MANIFEST_FILENAME,
    Phase718ReleaseAuthorityError,
    _inventory,
    verify_phase718_runtime_release,
)

ROOT = Path(__file__).resolve().parents[1]


def _write_json(path: Path, payload: dict[str, object]) -> bytes:
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = render_manifest(payload)
    path.write_bytes(raw)
    return raw


def _bundle(tmp_path: Path) -> tuple[Path, dict[str, object]]:
    root = tmp_path.resolve()
    contract = root / PHASE718_CONTRACT_PATH
    profile = root / PHASE718_PROFILE_PATH
    contract.parent.mkdir(parents=True)
    profile.parent.mkdir(parents=True)
    shutil.copyfile(ROOT / PHASE718_CONTRACT_PATH, contract)
    shutil.copyfile(ROOT / PHASE718_PROFILE_PATH, profile)
    (root / "mr_lister/cloud").mkdir(parents=True)
    (root / "mr_lister/cloud/phase718_entrypoints.py").write_text("# sealed runtime\n")

    application = "a" * 64
    canary = "b" * 64
    enablement = "c" * 64
    state_table = "mr-lister-phase6-dev"
    binding_raw = _write_json(
        root / PHASE718_BINDING_FILENAME,
        {
            "application_release_fingerprint": application,
            "canary_evidence_fingerprint": canary,
            "contract_fingerprint": PHASE718_CONTRACT_FINGERPRINT,
            "contract_version": "7.1.0",
            "enablement_evidence_fingerprint": enablement,
            "entrypoints": list(PHASE718_ENTRYPOINTS),
            "format": "phase718-enabled-binding-v1",
            "profile_fingerprint": PHASE718_PROFILE_FINGERPRINT,
            "state_table": state_table,
        },
    )
    source_raw = _write_json(
        root / PHASE718_SOURCE_MANIFEST_FILENAME,
        {"format": "phase718-enabled-source-v1"},
    )
    dependency_raw = _write_json(
        root / DEPENDENCY_MANIFEST_FILENAME,
        {"format": "phase6-linux-arm64-dependencies-v2"},
    )
    deployment_raw = _write_json(
        root / PHASE718_DEPLOYMENT_MANIFEST_FILENAME,
        {
            "algorithm": "sha256",
            "component": "phase718-enabled-lambda",
            "entrypoints": list(PHASE718_ENTRYPOINTS),
            "files": _inventory(
                root,
                excluded=frozenset(
                    {
                        PHASE718_DEPLOYMENT_MANIFEST_FILENAME,
                        PHASE718_RELEASE_MANIFEST_FILENAME,
                    }
                ),
            ),
            "format": "phase718-enabled-deployment-v1",
            "target": LINUX_ARM64_TARGET,
        },
    )
    release_raw = _write_json(
        root / PHASE718_RELEASE_MANIFEST_FILENAME,
        {
            "algorithm": "sha256",
            "application_release_fingerprint": application,
            "binding_sha256": sha256(binding_raw).hexdigest(),
            "canary_evidence_fingerprint": canary,
            "component": "phase718-enabled-lambda",
            "contract_fingerprint": PHASE718_CONTRACT_FINGERPRINT,
            "dependency_manifest_sha256": sha256(dependency_raw).hexdigest(),
            "deployment_manifest_sha256": sha256(deployment_raw).hexdigest(),
            "enablement_evidence_fingerprint": enablement,
            "entrypoints": list(PHASE718_ENTRYPOINTS),
            "format": "phase718-enabled-release-v1",
            "profile_fingerprint": PHASE718_PROFILE_FINGERPRINT,
            "source_manifest_sha256": sha256(source_raw).hexdigest(),
            "state_table": state_table,
            "target": LINUX_ARM64_TARGET,
        },
    )
    environment: dict[str, object] = {
        "MR_LISTER_PHASE7_ENABLED_RELEASE_FINGERPRINT": sha256(release_raw).hexdigest(),
        "MR_LISTER_RELEASE_FINGERPRINT": application,
        "MR_LISTER_PHASE7_CONTRACT_FINGERPRINT": PHASE718_CONTRACT_FINGERPRINT,
        "MR_LISTER_PHASE7_CANARY_EVIDENCE_FINGERPRINT": canary,
        "MR_LISTER_PHASE7_ENABLEMENT_EVIDENCE_FINGERPRINT": enablement,
        "MR_LISTER_PRODUCT_PROFILE_FINGERPRINT": PHASE718_PROFILE_FINGERPRINT,
        "MR_LISTER_PRODUCT_PROFILE_PATH": profile.as_posix(),
        "MR_LISTER_STATE_TABLE": state_table,
    }
    return root, environment


def test_release_verifier_authenticates_every_file_and_exact_handler(tmp_path: Path) -> None:
    root, environment = _bundle(tmp_path)

    verified = verify_phase718_runtime_release(
        environment,
        expected_entrypoint=PHASE718_ENTRYPOINTS[3],
        bundle_root=root,
    )

    assert verified.entrypoint == PHASE718_ENTRYPOINTS[3]
    assert (
        verified.release_fingerprint == environment["MR_LISTER_PHASE7_ENABLED_RELEASE_FINGERPRINT"]
    )
    assert verified.canary_evidence_fingerprint == "b" * 64
    assert verified.enablement_evidence_fingerprint == "c" * 64


def test_release_verifier_rejects_byte_or_environment_drift_without_detail(tmp_path: Path) -> None:
    root, environment = _bundle(tmp_path)
    (root / "mr_lister/cloud/phase718_entrypoints.py").write_text("# drifted runtime\n")

    with pytest.raises(Phase718ReleaseAuthorityError) as captured:
        verify_phase718_runtime_release(
            environment,
            expected_entrypoint=PHASE718_ENTRYPOINTS[0],
            bundle_root=root,
        )

    assert str(captured.value) == "Phase 7.18 enabled release authority is invalid"
    assert captured.value.__cause__ is None
    assert "drifted" not in str(captured.value)

    root, environment = _bundle(tmp_path / "second")
    environment["MR_LISTER_PHASE7_CANARY_EVIDENCE_FINGERPRINT"] = "d" * 64
    with pytest.raises(Phase718ReleaseAuthorityError):
        verify_phase718_runtime_release(
            environment,
            expected_entrypoint=PHASE718_ENTRYPOINTS[0],
            bundle_root=root,
        )


def test_release_verifier_rejects_unknown_entrypoint(tmp_path: Path) -> None:
    root, environment = _bundle(tmp_path)
    with pytest.raises(Phase718ReleaseAuthorityError):
        verify_phase718_runtime_release(
            environment,
            expected_entrypoint="mr_lister.cloud.phase718_entrypoints.not_a_handler",
            bundle_root=root,
        )
