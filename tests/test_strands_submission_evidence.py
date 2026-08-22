"""Regression gates that keep required Strands usage public and submission-visible."""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _read(relative_path: str) -> str:
    return (ROOT / relative_path).read_text(encoding="utf-8")


def test_readme_exposes_real_strands_loop_and_evidence() -> None:
    readme = _read("README.md")

    assert "Mr Lister is built with the Strands Agents SDK" in readme
    assert "Core Strands agentic loop" in readme
    assert "src/mr_lister/agent/runtime.py" in readme
    assert "src/mr_lister/agent/tools.py" in readme
    assert "src/mr_lister/agent/agentcore_sdk.py" in readme
    assert "tests/test_strands_real_loop.py" in readme
    assert "docs/strands-submission-evidence.md" in readme
    assert "still an open acceptance gate" in readme


def test_submission_gate_requires_same_job_fail_closed_strands_path() -> None:
    checklist = _read("docs/phase-checklist.md")
    contract = _read("docs/phase6-seller-control-contract.md")

    assert "Required Strands production and submission path (blocking Phase 6 exit)" in checklist
    assert (
        "[ ] Durable `PREPARE` work invokes the exact AgentCore Strands runtime fail-closed"
        in checklist
    )
    assert "[ ] End-to-end acceptance proves upload -> Strands model/tool loop" in checklist
    assert "`PREPARE` fails closed if the exact AgentCore" in contract
    assert "It cannot bypass the runtime or fall back to a direct model" in contract


def test_public_canary_evidence_is_explicit_and_sanitized() -> None:
    evidence_path = ROOT / "docs/evidence/strands-agentcore-canary.json"
    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    serialized = json.dumps(evidence, sort_keys=True)

    assert evidence["framework"] == "strands-agents"
    assert evidence["agent_id"] == "mr-lister-preparation"
    assert evidence["tool_calls"] == [
        "inspect_staged_review",
        "validate_staged_listing",
    ]
    assert evidence["next_action"] == "human_review"
    assert evidence["requires_human_approval"] is True
    assert evidence["publication_authorized"] is False
    assert "redeployed canary pending" in evidence["metadata_provenance"]["framework"]
    assert "redeployed canary pending" in evidence["metadata_provenance"]["agent_id"]
    assert evidence["sanitization"] == {
        "account_id_included": False,
        "artwork_included": False,
        "job_id_included": False,
        "prompt_included": False,
        "provider_payload_included": False,
        "session_id_included": False,
    }
    string_values = _string_values(evidence)
    assert re.search(r"\b\d{12}\b", serialized) is None
    assert all(not value.startswith("job_") for value in string_values)
    assert all(not value.startswith("session_") for value in string_values)


def test_source_contains_real_agent_tools_and_agentcore_runner() -> None:
    runtime_tree = ast.parse(_read("src/mr_lister/agent/runtime.py"))
    tools_tree = ast.parse(_read("src/mr_lister/agent/tools.py"))
    agentcore_source = _read("src/mr_lister/agent/agentcore_sdk.py")

    runtime_imports = {
        alias.name
        for node in ast.walk(runtime_tree)
        if isinstance(node, ast.ImportFrom) and node.module == "strands"
        for alias in node.names
    }
    runtime_calls = {
        node.func.id
        for node in ast.walk(runtime_tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    tool_decorators = [
        decorator
        for node in ast.walk(tools_tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        for decorator in node.decorator_list
        if isinstance(decorator, ast.Name) and decorator.id == "tool"
    ]

    assert "Agent" in runtime_imports
    assert "Agent" in runtime_calls
    assert len(tool_decorators) >= 4
    assert "StrandsPreparationRunner" in agentcore_source
    assert "BedrockModel" in agentcore_source


def _string_values(value: object) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [item for child in value.values() for item in _string_values(child)]
    if isinstance(value, list):
        return [item for child in value for item in _string_values(child)]
    return []
