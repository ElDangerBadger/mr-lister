from __future__ import annotations

import json
from pathlib import Path

import pytest

from tools.build_agentcore_bundle import build_bundle

ROOT = Path(__file__).resolve().parents[1]


def test_agentcore_bundle_contains_only_explicit_runtime_inputs(tmp_path) -> None:
    destination = build_bundle(tmp_path / "agentcore-bundle")

    assert (destination / "main.py").is_file()
    assert (destination / "mr_lister/agent/agentcore_sdk.py").is_file()
    assert (destination / "config/product_profiles/synthetic_gildan_5000.json").is_file()
    assert not (destination / ".git").exists()
    assert not (destination / ".mr_lister_private").exists()
    assert not (destination / "tests").exists()
    assert not (destination / "docs_legacy").exists()
    assert not (destination / "bedrock-policy.json").exists()


def test_agentcore_bundle_rejects_broad_or_malformed_destinations(tmp_path) -> None:
    with pytest.raises(ValueError, match="destination"):
        build_bundle(tmp_path)


def test_agentcore_runtime_uses_external_narrow_role_and_disables_otel() -> None:
    config = json.loads(
        (ROOT / "infra/agentcore/mrlisterphase3/agentcore/agentcore.json.tmpl").read_text(
            encoding="utf-8"
        )
    )
    runtime = config["runtimes"][0]

    assert runtime["executionRoleArn"].endswith(":role/mr-lister-agentcore-runtime-canary")
    assert runtime["instrumentation"] == {"enableOtel": False}
    assert "additionalPolicies" not in runtime
