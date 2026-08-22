"""Credential-free proof that Mr Lister executes the real Strands agent loop."""

from __future__ import annotations

import json
from base64 import b64decode
from collections.abc import AsyncGenerator, AsyncIterable
from typing import Any

from pydantic import BaseModel
from strands.models.model import Model
from strands.types.content import Messages, SystemContentBlock
from strands.types.streaming import StreamEvent
from strands.types.tools import ToolChoice, ToolResult, ToolSpec

from mr_lister.agent.contracts import PreparationDecision, PreparationRequest
from mr_lister.agent.observability import InMemoryAgentAuditSink
from mr_lister.agent.runtime import StrandsPreparationRunner

SYNTHETIC_PNG = b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAIAAAACCAYAAABytg0kAAAAFUlEQVR4nGNU07X8z8DAwMAEIkAYABbVAY+Z/lCyAAAAAElFTkSuQmCC"
)


class DeterministicToolCallingModel(Model):
    """Local model double that still drives the installed Strands event loop."""

    def __init__(self) -> None:
        self._config: dict[str, Any] = {
            "model_id": "deterministic-strands-loop-test",
            "context_window_limit": 10_000,
        }
        self.stream_calls = 0
        self.inspect_result: ToolResult | None = None

    def update_config(self, **model_config: Any) -> None:
        self._config.update(model_config)

    def get_config(self) -> dict[str, Any]:
        return dict(self._config)

    async def structured_output(
        self,
        output_model: type[BaseModel],
        prompt: Messages,
        system_prompt: str | None = None,
        **kwargs: Any,
    ) -> AsyncGenerator[dict[str, Any], None]:
        del prompt, system_prompt, kwargs
        yield {"output": output_model.model_validate(_decision_payload())}

    async def stream(
        self,
        messages: Messages,
        tool_specs: list[ToolSpec] | None = None,
        system_prompt: str | None = None,
        *,
        tool_choice: ToolChoice | None = None,
        system_prompt_content: list[SystemContentBlock] | None = None,
        invocation_state: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> AsyncIterable[StreamEvent]:
        del system_prompt, tool_choice, system_prompt_content, invocation_state, kwargs
        self.stream_calls += 1
        tool_names = {spec["name"] for spec in tool_specs or []}

        if self.stream_calls == 1:
            assert "inspect_staged_review" in tool_names
            async for event in _tool_call_events(
                name="inspect_staged_review",
                tool_use_id="inspect-call-1",
                tool_input={},
            ):
                yield event
            return

        inspect_results = [
            block["toolResult"]
            for message in messages
            for block in message["content"]
            if "toolResult" in block and block["toolResult"]["toolUseId"] == "inspect-call-1"
        ]
        assert len(inspect_results) == 1
        self.inspect_result = inspect_results[0]
        assert PreparationDecision.__name__ in tool_names
        async for event in _tool_call_events(
            name=PreparationDecision.__name__,
            tool_use_id="decision-call-1",
            tool_input=_decision_payload(),
        ):
            yield event


async def _tool_call_events(
    *,
    name: str,
    tool_use_id: str,
    tool_input: dict[str, Any],
) -> AsyncGenerator[StreamEvent, None]:
    yield {"messageStart": {"role": "assistant"}}
    yield {"contentBlockStart": {"start": {"toolUse": {"name": name, "toolUseId": tool_use_id}}}}
    yield {"contentBlockDelta": {"delta": {"toolUse": {"input": json.dumps(tool_input)}}}}
    yield {"contentBlockStop": {}}
    yield {"messageStop": {"stopReason": "tool_use"}}
    yield {
        "metadata": {
            "usage": {"inputTokens": 10, "outputTokens": 5, "totalTokens": 15},
            "metrics": {"latencyMs": 1},
        }
    }


def _decision_payload() -> dict[str, Any]:
    return {
        "summary": "The Strands agent inspected the staged review through its real tool loop.",
        "recommendation": "Send the validated listing to a human for review.",
        "next_action": "human_review",
        "requires_human_approval": True,
        "publication_authorized": False,
    }


def test_installed_strands_agent_loop_calls_real_tool_and_returns_contract(
    workflow,
    production,
) -> None:
    job = workflow.submit(
        filename="geometric_badger.png",
        content_type="image/png",
        content=SYNTHETIC_PNG,
        idempotency_key="strands-real-loop-001",
        profile_id="synthetic_gildan_5000",
    )
    model = DeterministicToolCallingModel()
    audit = InMemoryAgentAuditSink()
    runner = StrandsPreparationRunner(workflow=workflow, model=model, audit_sink=audit)

    decision = runner(
        PreparationRequest(
            session_id="session_strands_real_loop",
            job_id=job.job_id,
            mode="review",
            instruction="Inspect the staged listing and return a recommendation.",
        )
    )

    assert decision == PreparationDecision.model_validate(_decision_payload())
    assert model.stream_calls == 2
    assert model.inspect_result is not None
    assert model.inspect_result["status"] == "success"
    inspect_payload = json.loads(model.inspect_result["content"][0]["text"])
    assert inspect_payload["artwork_analysis"]["subject"] == "geometric badger"
    assert audit.records[0].tool_calls == ("inspect_staged_review",)
    assert production.publish_calls == 0
