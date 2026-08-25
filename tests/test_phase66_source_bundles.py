from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from tools.build_phase66_source_bundles import build_source_bundles, verify_source_bundle


def _destination(tmp_path: Path, name: str) -> Path:
    return tmp_path / name / "phase6-release"


def test_source_bundles_are_reproducible_and_manifest_bound(tmp_path: Path) -> None:
    first_lambda, first_agentcore = build_source_bundles(_destination(tmp_path, "first"))
    second_lambda, second_agentcore = build_source_bundles(_destination(tmp_path, "second"))

    assert (first_lambda / "source-manifest.json").read_bytes() == (
        second_lambda / "source-manifest.json"
    ).read_bytes()
    assert (first_agentcore / "source-manifest.json").read_bytes() == (
        second_agentcore / "source-manifest.json"
    ).read_bytes()
    verify_source_bundle(first_lambda)
    verify_source_bundle(first_agentcore)


def test_manifest_has_only_relative_sha256_size_records(tmp_path: Path) -> None:
    lambda_root, agentcore_root = build_source_bundles(_destination(tmp_path, "manifest"))

    for root in (lambda_root, agentcore_root):
        payload = json.loads((root / "source-manifest.json").read_text(encoding="utf-8"))
        assert payload["algorithm"] == "sha256"
        assert payload["format"] == "phase6-source-v1"
        assert payload["files"]
        assert "source-manifest.json" not in {item["path"] for item in payload["files"]}
        for item in payload["files"]:
            assert set(item) == {"path", "sha256", "size_bytes"}
            assert not Path(item["path"]).is_absolute()
            assert ".." not in Path(item["path"]).parts
            assert len(item["sha256"]) == 64
            assert item["size_bytes"] == (root / item["path"]).stat().st_size
        assert (root / "dependency-build-request.json").is_file()
        assert (root / "mr_lister/release/phase6.py").is_file()
        assert (root / "mr_lister/__init__.py").read_bytes() == b""
        assert (root / "mr_lister/release/__init__.py").read_bytes() == b""
        assert {path.name for path in (root / "mr_lister/release").glob("*.py")} == {
            "__init__.py",
            "phase6.py",
        }
        assert not (root / "release-manifest.json").exists()


def test_tamper_or_extra_file_fails_verification(tmp_path: Path) -> None:
    lambda_root, _agentcore = build_source_bundles(_destination(tmp_path, "tamper"))
    target = lambda_root / "phase6_lambda.py"
    target.write_text(target.read_text(encoding="utf-8") + "# drift\n", encoding="utf-8")

    with pytest.raises(ValueError, match="manifest does not match"):
        verify_source_bundle(lambda_root)

    other_lambda, _ = build_source_bundles(_destination(tmp_path, "extra"))
    (other_lambda / "unexpected.txt").write_text("unexpected", encoding="utf-8")
    with pytest.raises(ValueError, match="manifest does not match"):
        verify_source_bundle(other_lambda)


def test_existing_or_wrongly_named_destination_is_never_overwritten(tmp_path: Path) -> None:
    existing = _destination(tmp_path, "existing")
    existing.mkdir(parents=True)
    marker = existing / "user-data.txt"
    marker.write_text("preserve", encoding="utf-8")

    with pytest.raises(ValueError, match="new phase6-release"):
        build_source_bundles(existing)
    with pytest.raises(ValueError, match="new phase6-release"):
        build_source_bundles(tmp_path / "wrong-name")

    assert marker.read_text(encoding="utf-8") == "preserve"


def test_lambda_bundle_excludes_agentcore_and_legacy_broad_surfaces(tmp_path: Path) -> None:
    lambda_root, _agentcore = build_source_bundles(_destination(tmp_path, "lambda-surface"))

    assert (lambda_root / "phase6_lambda.py").is_file()
    assert (lambda_root / "mr_lister/cloud/phase6_entrypoints.py").is_file()
    assert (lambda_root / "mr_lister/cloud/phase6_retention_entrypoint.py").is_file()
    assert (lambda_root / "mr_lister/cloud/phase6_operational_cleanup_entrypoint.py").is_file()
    assert (lambda_root / "mr_lister/cloud/phase6_execution_recovery_composition.py").is_file()
    assert (lambda_root / "mr_lister/cloud/phase6_execution_recovery_entrypoint.py").is_file()
    assert (lambda_root / "mr_lister/agent/runtime_binding.py").is_file()
    assert (lambda_root / "mr_lister/production/operational_cleanup.py").is_file()
    assert (lambda_root / "mr_lister/production/operational_cleanup_aws.py").is_file()
    assert (lambda_root / "mr_lister/production/retention.py").is_file()
    assert (lambda_root / "mr_lister/production/retention_aws.py").is_file()
    assert not (lambda_root / "mr_lister/api").exists()
    assert not (lambda_root / "mr_lister/production/adapter.py").exists()
    assert not (lambda_root / "mr_lister/workflow/service.py").exists()
    requirements = (lambda_root / "requirements.txt").read_text(encoding="utf-8")
    assert "strands" not in requirements
    assert "agentcore" not in requirements
    assert "fastapi" not in requirements


def test_agentcore_bundle_is_phase6_gemma_strands_not_phase3_synthetic(tmp_path: Path) -> None:
    _lambda_root, agentcore_root = build_source_bundles(_destination(tmp_path, "agentcore-surface"))

    main = (agentcore_root / "main.py").read_text(encoding="utf-8")
    assert "build_phase6_agentcore_runtime" in main
    assert "verify_phase6_packaged_release" in main
    assert "build_synthetic_canary_runtime" not in main
    assert (agentcore_root / "config/bedrock/google_gemma_3_27b_it.json").is_file()
    assert not (agentcore_root / "mr_lister/cloud").exists()
    assert not (agentcore_root / "mr_lister/production").exists()
    assert not (agentcore_root / "mr_lister/workflow/service.py").exists()
    requirements = (agentcore_root / "requirements.txt").read_text(encoding="utf-8")
    assert "strands-agents" in requirements
    assert "bedrock-agentcore" in requirements


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="symlink contract requires POSIX support")
def test_verifier_rejects_a_symlink_even_when_manifest_is_untouched(tmp_path: Path) -> None:
    lambda_root, _agentcore = build_source_bundles(_destination(tmp_path, "symlink"))
    os.symlink(lambda_root / "requirements.txt", lambda_root / "linked.txt")

    with pytest.raises(ValueError):
        verify_source_bundle(lambda_root)


def test_bundled_module_imports_do_not_eager_load_legacy_publish_surfaces(tmp_path: Path) -> None:
    lambda_root, agentcore_root = build_source_bundles(_destination(tmp_path, "imports"))
    interpreter = Path(sys.executable)

    lambda_result = subprocess.run(
        [
            interpreter,
            "-c",
            (
                "import sys; import mr_lister.cloud.phase6_entrypoints; "
                "import mr_lister.cloud.phase6_execution_recovery_entrypoint; "
                "import mr_lister.cloud.phase6_operational_cleanup_entrypoint; "
                "import mr_lister.cloud.phase6_retention_entrypoint; "
                "assert 'mr_lister.production.adapter' not in sys.modules; "
                "assert 'mr_lister.workflow.service' not in sys.modules"
            ),
        ],
        cwd=tmp_path,
        env={**os.environ, "PYTHONPATH": str(lambda_root)},
        capture_output=True,
        check=False,
        text=True,
    )
    assert lambda_result.returncode == 0, lambda_result.stderr

    agentcore_result = subprocess.run(
        [
            interpreter,
            "-c",
            (
                "import sys; import mr_lister.agent.phase6_composition; "
                "assert 'mr_lister.production.adapter' not in sys.modules; "
                "assert 'mr_lister.workflow.service' not in sys.modules"
            ),
        ],
        cwd=tmp_path,
        env={**os.environ, "PYTHONPATH": str(agentcore_root)},
        capture_output=True,
        check=False,
        text=True,
    )
    assert agentcore_result.returncode == 0, agentcore_result.stderr
