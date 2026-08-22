from __future__ import annotations

from fastapi.testclient import TestClient

from mr_lister.agent.contracts import (
    AGENT_FRAMEWORK,
    PREPARATION_AGENT_ID,
    PreparationDecision,
    PreparationRequest,
)
from mr_lister.agent.http import create_agentcore_app
from mr_lister.agent.runtime import AgentExecutionError

SESSION_HEADER = "X-Amzn-Bedrock-AgentCore-Runtime-Session-Id"


class RecordingRunner:
    def __init__(self) -> None:
        self.requests: list[PreparationRequest] = []

    def __call__(self, request: PreparationRequest) -> PreparationDecision:
        self.requests.append(request)
        return PreparationDecision(
            summary="The staged listing passed deterministic validation.",
            recommendation="A human should review the listing before approval.",
            next_action="human_review",
        )


def test_agentcore_http_contract_exposes_ping_and_invocations() -> None:
    runner = RecordingRunner()
    client = TestClient(create_agentcore_app(runner))

    ping = client.get("/ping")
    response = client.post(
        "/invocations",
        headers={SESSION_HEADER: "session_agentcore_1"},
        json={
            "job_id": "job_phase1_fixture",
            "mode": "review",
            "instruction": "Explain the staged listing.",
        },
    )

    assert ping.status_code == 200
    assert ping.json() == {"status": "Healthy"}
    assert response.status_code == 200
    assert response.json()["status"] == "success"
    assert response.json()["framework"] == AGENT_FRAMEWORK
    assert response.json()["agent_id"] == PREPARATION_AGENT_ID
    assert response.json()["decision"]["requires_human_approval"] is True
    assert response.json()["decision"]["publication_authorized"] is False
    assert runner.requests == [
        PreparationRequest(
            session_id="session_agentcore_1",
            job_id="job_phase1_fixture",
            mode="review",
            instruction="Explain the staged listing.",
        )
    ]


def test_agentcore_validation_errors_do_not_echo_untrusted_instruction() -> None:
    client = TestClient(create_agentcore_app(RecordingRunner()))
    secret_marker = "seller-private-marker"

    response = client.post(
        "/invocations",
        headers={SESSION_HEADER: "invalid session with spaces"},
        json={
            "job_id": "job_phase1_fixture",
            "instruction": secret_marker,
            "unexpected": "field",
        },
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "INVALID_AGENT_REQUEST"
    assert secret_marker not in response.text


def test_agentcore_execution_errors_are_sanitized() -> None:
    def failing_runner(_request: PreparationRequest) -> PreparationDecision:
        raise AgentExecutionError("provider detail must not escape")

    client = TestClient(create_agentcore_app(failing_runner))
    response = client.post(
        "/invocations",
        headers={SESSION_HEADER: "session_agentcore_2"},
        json={
            "job_id": "job_phase1_fixture",
            "mode": "review",
            "instruction": "Review the listing.",
        },
    )

    assert response.status_code == 502
    assert response.json()["error"]["code"] == "AGENT_EXECUTION_FAILED"
    assert "provider detail" not in response.text
