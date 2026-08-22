from __future__ import annotations

from fastapi.testclient import TestClient

from mr_lister.agent.agentcore_sdk import create_agentcore_sdk_app
from mr_lister.agent.contracts import (
    AGENT_FRAMEWORK,
    PREPARATION_AGENT_ID,
    PreparationDecision,
)


class RecordingRunner:
    def __init__(self) -> None:
        self.requests = []

    def __call__(self, request):
        self.requests.append(request)
        return PreparationDecision(
            summary="The staged listing passed validation.",
            recommendation="Send it to human review.",
            next_action="human_review",
        )


class FailingRunner:
    def __call__(self, request):
        del request
        raise RuntimeError("DO_NOT_ECHO_PROVIDER_SECRET")


def test_official_agentcore_sdk_boundary_preserves_authority_contract() -> None:
    runner = RecordingRunner()
    client = TestClient(create_agentcore_sdk_app(runner))

    response = client.post(
        "/invocations",
        headers={"X-Amzn-Bedrock-AgentCore-Runtime-Session-Id": "session_sdk_1"},
        json={
            "job_id": "job_sdk_1",
            "mode": "review",
            "instruction": "Review this staged listing.",
        },
    )

    assert response.status_code == 200
    assert response.json()["framework"] == AGENT_FRAMEWORK
    assert response.json()["agent_id"] == PREPARATION_AGENT_ID
    assert response.json()["decision"]["requires_human_approval"] is True
    assert response.json()["decision"]["publication_authorized"] is False
    assert runner.requests[0].session_id == "session_sdk_1"


def test_official_agentcore_sdk_boundary_sanitizes_bad_request() -> None:
    client = TestClient(create_agentcore_sdk_app(RecordingRunner()))
    marker = "DO_NOT_ECHO_THIS_PRIVATE_INPUT"

    response = client.post(
        "/invocations",
        headers={"X-Amzn-Bedrock-AgentCore-Runtime-Session-Id": "session_sdk_2"},
        json={"job_id": "bad job", "instruction": marker},
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "INVALID_AGENT_REQUEST"
    assert marker not in response.text


def test_official_agentcore_sdk_boundary_sanitizes_unexpected_failures() -> None:
    client = TestClient(create_agentcore_sdk_app(FailingRunner()))

    response = client.post(
        "/invocations",
        headers={"X-Amzn-Bedrock-AgentCore-Runtime-Session-Id": "session_sdk_3"},
        json={
            "job_id": "job_sdk_3",
            "mode": "review",
            "instruction": "Review this staged listing.",
        },
    )

    assert response.status_code == 502
    assert response.json()["error"]["code"] == "AGENT_EXECUTION_FAILED"
    assert "DO_NOT_ECHO_PROVIDER_SECRET" not in response.text
