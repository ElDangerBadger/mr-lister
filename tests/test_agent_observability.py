from __future__ import annotations

import json

from mr_lister.agent.contracts import AGENT_FRAMEWORK, PREPARATION_AGENT_ID
from mr_lister.agent.observability import AgentAuditRecord, LoggingAgentAuditSink


def test_logging_sink_emits_only_the_sanitized_contract(capsys) -> None:
    record = AgentAuditRecord(
        correlation_id="a" * 24,
        mode="review",
        status="failed",
        elapsed_ms=12.5,
        error_code="AGENT_EXECUTION_FAILED",
    )

    LoggingAgentAuditSink().write(record)

    captured = capsys.readouterr()
    rendered = captured.out
    assert captured.err == ""
    assert rendered.count("\n") == 1
    assert "AGENT_EXECUTION_FAILED" in rendered
    assert "session_id" not in rendered
    assert "job_id" not in rendered
    assert "prompt" not in rendered
    assert "provider" not in rendered
    payload = json.loads(rendered.split("agent_audit=", 1)[1].strip())
    assert payload == record.model_dump(mode="json")
    assert payload["framework"] == AGENT_FRAMEWORK
    assert payload["agent_id"] == PREPARATION_AGENT_ID
