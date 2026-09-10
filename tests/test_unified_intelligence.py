"""Credential-free real Strands loops over a scripted Bedrock transport."""

from __future__ import annotations

import json
from copy import deepcopy
from hashlib import sha256
from io import BytesIO
from types import SimpleNamespace
from typing import Any

import pytest
from botocore.exceptions import ClientError
from PIL import Image
from strands import Agent, tool
from strands.agent.conversation_manager import NullConversationManager
from strands.models import BedrockModel

from mr_lister.intelligence.listing_draft import finalize_listing_draft
from mr_lister.intelligence.prompts import ETSY_SEO_RELEASE_PROMPT_BUNDLE
from mr_lister.intelligence.settings import BedrockSettings
from mr_lister.intelligence.unified import (
    UNIFIED_SYSTEM_PROMPT,
    NativeJsonBedrockModel,
    UnifiedArtworkListing,
    build_unified_model,
    prepare_unified_review,
    unified_review_prompt,
)
from mr_lister.workflow.errors import (
    IntelligenceConfigurationError,
    IntelligenceUnavailableError,
    InvalidGeneratedOutputError,
)
from mr_lister.workflow.models import ArtworkInput


def payload() -> dict[str, Any]:
    return {
        "analysis": {
            "subject": "Geometric badger with a compass",
            "visual_elements": ["angular badger", "compass", "pine trees"],
            "confidence": 0.95,
        },
        "listing": {
            "title": "Geometric Badger Graphic T-Shirt",
            "description": "A geometric badger holds a compass beside pine trees.",
            "tag_candidates": [
                "badger portrait",
                "badger explorer",
                "amber compass",
                "pine silhouette",
                "crescent moon",
                "retro vector",
                "outdoor adventure",
                "nature lover",
                "animal character",
                "forest traveler",
                "night sky",
                "wearable artwork",
                "hiking gift",
                "geometric wildlife",
                "black gold",
                "compass rose",
                "camping wardrobe",
                "bold shapes",
                "wilderness fan",
                "trail keepsake",
            ],
            "audience": ["badger fans"],
            "title_rationale": "Names the subject and product.",
            "tag_rationale": "Ranks relevant alternatives for deterministic selection.",
        },
    }


def response(
    document: dict[str, Any] | str,
    *,
    output_tokens: int = 600,
    stop_reason: str = "end_turn",
) -> dict[str, Any]:
    return {
        "output": {
            "message": {
                "role": "assistant",
                "content": [
                    {"text": document if isinstance(document, str) else json.dumps(document)}
                ],
            }
        },
        "stopReason": stop_reason,
        "usage": {
            "inputTokens": 1_000,
            "outputTokens": output_tokens,
            "totalTokens": 1_000 + output_tokens,
        },
        "metrics": {"latencyMs": 1},
    }


class ScriptedClient:
    def __init__(self, responses: list[dict[str, Any] | Exception]) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, Any]] = []
        self.meta = SimpleNamespace(region_name="us-west-2")

    def converse(self, **request: Any) -> dict[str, Any]:
        self.calls.append(deepcopy(request))
        assert self.responses, "No additional inference was authorized by this test"
        result = self.responses.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


class ScriptedSession:
    region_name = "us-west-2"

    def __init__(self, client: ScriptedClient) -> None:
        self.transport = client
        self.options: list[dict[str, Any]] = []

    def client(self, **kwargs: Any) -> ScriptedClient:
        assert kwargs["service_name"] == "bedrock-runtime"
        self.options.append(kwargs)
        return self.transport


def agent_for(
    responses: list[dict[str, Any] | Exception],
    *,
    tools: list[Any] | None = None,
) -> tuple[Agent, ScriptedClient, ScriptedSession]:
    client = ScriptedClient(responses)
    session = ScriptedSession(client)
    model = build_unified_model(
        session, BedrockSettings(model_id="google.gemma-3-27b-it", max_repair_attempts=2)
    )
    return (
        Agent(
            model=model,
            system_prompt=UNIFIED_SYSTEM_PROMPT,
            tools=tools,
            retry_strategy=None,
            conversation_manager=NullConversationManager(),
            callback_handler=None,
        ),
        client,
        session,
    )


def artwork() -> tuple[ArtworkInput, bytes]:
    output = BytesIO()
    Image.new("RGBA", (60, 90), (50, 100, 150, 120)).save(output, format="PNG")
    content = output.getvalue()
    return (
        ArtworkInput(
            filename="source.png",
            content_type="image/png",
            content_sha256=sha256(content).hexdigest(),
            size_bytes=len(content),
        ),
        content,
    )


def test_one_real_strands_cycle_one_native_multimodal_call() -> None:
    agent, client, session = agent_for([response(payload())])
    source, content = artwork()
    analysis, listing = prepare_unified_review(agent, source, content)

    assert analysis.subject == payload()["analysis"]["subject"]
    assert listing.title == payload()["listing"]["title"]
    assert len(listing.tags) == 13
    assert (
        listing.tags
        == finalize_listing_draft(UnifiedArtworkListing.model_validate(payload()).listing).tags
    )
    assert len(client.calls) == 1
    request = client.calls[0]
    assert "toolConfig" not in request
    assert request["modelId"] == "google.gemma-3-27b-it"
    assert request["inferenceConfig"] == {"maxTokens": 2048, "temperature": 0.0}
    schema = json.loads(request["outputConfig"]["textFormat"]["structure"]["jsonSchema"]["schema"])
    assert set(schema["properties"]) == {"analysis", "listing"}
    assert "image" in request["messages"][0]["content"][0]
    assert "checkerboard is not part" in request["messages"][0]["content"][1]["text"]
    assert session.options[0]["config"].retries["max_attempts"] == 0
    summary = agent.event_loop_metrics.get_summary()
    assert summary["total_cycles"] == 1
    assert summary["accumulated_usage"] == {
        "inputTokens": 1000,
        "outputTokens": 600,
        "totalTokens": 1600,
    }


def test_schema_failure_has_one_shared_repair_and_real_accumulated_metrics() -> None:
    invalid = payload()
    del invalid["listing"]["title"]
    agent, client, _ = agent_for([response(invalid), response(payload())])

    _, listing = prepare_unified_review(agent, *artwork())

    assert len(listing.tags) == 13
    assert len(client.calls) == 2
    assert client.calls[1]["inferenceConfig"]["maxTokens"] == 1900
    assert "listing.title" in client.calls[1]["messages"][-1]["content"][0]["text"]
    assert agent.event_loop_metrics.get_summary()["total_cycles"] == 2
    assert agent.event_loop_metrics.get_summary()["accumulated_usage"]["outputTokens"] == 1200


def test_new_sdk_positional_format_shape_preserves_native_json_and_call_budget() -> None:
    agent, client, _ = agent_for([])
    model = agent.model
    assert isinstance(model, NativeJsonBedrockModel)

    for count, remaining_tokens in enumerate((1900, 600), start=1):
        model.authorize_inference(remaining_output_tokens=remaining_tokens)
        request = model.format_request(
            [{"role": "user", "content": [{"text": "Interpret the artwork."}]}],
            None,
            None,
            None,
            0,
        )
        assert request["inferenceConfig"]["maxTokens"] == remaining_tokens
        assert request["outputConfig"]["textFormat"]["type"] == "json_schema"
        schema = json.loads(
            request["outputConfig"]["textFormat"]["structure"]["jsonSchema"]["schema"]
        )
        assert set(schema["properties"]) == {"analysis", "listing"}
        assert "toolConfig" not in request
        assert model.request_count == count
        with pytest.raises(InvalidGeneratedOutputError, match="not authorized"):
            model.format_request([], None, None, None, 0)

    with pytest.raises(InvalidGeneratedOutputError, match="bounded budget"):
        model.authorize_inference(remaining_output_tokens=1)
    assert client.calls == []


def test_dynamic_trailing_blocks_are_rejected_before_sdk_formatting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent, client, _ = agent_for([])
    model = agent.model
    assert isinstance(model, NativeJsonBedrockModel)

    def unexpected_sdk_format(*args: Any, **kwargs: Any) -> None:
        pytest.fail("Unsupported dynamic trailing blocks reached SDK formatting")

    monkeypatch.setattr(BedrockModel, "format_request", unexpected_sdk_format)
    model.authorize_inference(remaining_output_tokens=1900)
    with pytest.raises(IntelligenceConfigurationError, match="dynamic trailing blocks"):
        model.format_request([], None, None, None, 1)
    assert model.request_count == 0
    assert client.calls == []
    with pytest.raises(InvalidGeneratedOutputError, match="not authorized"):
        model.format_request([], None, None, None, 0)


def test_tag_only_repair_cannot_replace_accepted_copy_or_analysis() -> None:
    invalid = payload()
    invalid["listing"]["tag_candidates"] = [
        f"exceptionally detailed badger search {number}" for number in range(18)
    ]
    repaired = payload()
    repaired["analysis"]["subject"] = "A different animal"
    repaired["listing"]["title"] = "Changed title"
    repaired["listing"]["description"] = "Changed description"
    repaired["listing"]["audience"] = ["Changed audience"]
    agent, client, _ = agent_for([response(invalid), response(repaired)])

    analysis, listing = prepare_unified_review(agent, *artwork())

    assert analysis == UnifiedArtworkListing.model_validate(invalid).analysis
    expected = UnifiedArtworkListing.model_validate(invalid).listing.model_dump(
        exclude={"tag_candidates"}
    )
    assert listing.model_dump(exclude={"tags"}) == expected
    assert len(listing.tags) == 13
    assert len(client.calls) == 2
    assert "change only tag_candidates" in client.calls[1]["messages"][-1]["content"][0]["text"]


def test_schema_then_tag_failure_cannot_open_a_third_call() -> None:
    invalid_tags = payload()
    invalid_tags["listing"]["tag_candidates"] = [
        f"exceptionally detailed badger search {number}" for number in range(18)
    ]
    agent, client, _ = agent_for([response("{"), response(invalid_tags)])

    with pytest.raises(InvalidGeneratedOutputError, match="one bounded repair"):
        prepare_unified_review(agent, *artwork())
    assert len(client.calls) == 2


def test_truncated_output_uses_only_the_genuine_remaining_token_budget() -> None:
    agent, client, _ = agent_for(
        [
            response("{", output_tokens=2048, stop_reason="max_tokens"),
            response(payload(), output_tokens=400),
        ]
    )

    prepare_unified_review(agent, *artwork())

    assert [request["inferenceConfig"]["maxTokens"] for request in client.calls] == [2048, 452]
    assert agent.event_loop_metrics.get_summary()["accumulated_usage"]["outputTokens"] == 2448


def test_exhausted_budget_does_not_invoke_another_model() -> None:
    agent, client, _ = agent_for([response("{", output_tokens=2500)])
    with pytest.raises(InvalidGeneratedOutputError, match="budget"):
        prepare_unified_review(agent, *artwork())
    assert len(client.calls) == 1
    assert agent.event_loop_metrics.get_summary()["accumulated_usage"]["outputTokens"] == 2500


@pytest.mark.parametrize(
    "code,message",
    [
        ("ThrottlingException", "Throttled"),
        ("ValidationException", "Input is too long for requested model"),
    ],
)
def test_provider_failures_do_not_trigger_hidden_sdk_or_context_retries(
    code: str, message: str
) -> None:
    error = ClientError({"Error": {"Code": code, "Message": message}}, "Converse")
    agent, client, _ = agent_for([error])

    with pytest.raises(IntelligenceUnavailableError, match="unavailable"):
        prepare_unified_review(agent, *artwork())
    assert len(client.calls) == 1


def test_model_tools_are_absent_and_unexpected_tool_output_is_never_executed() -> None:
    calls: list[str] = []

    @tool
    def record_prepared_review() -> str:
        """Record the trusted preparation checkpoint."""
        calls.append("record")
        return "recorded"

    malicious = response(payload(), stop_reason="tool_use")
    malicious["output"]["message"]["content"] = [
        {"toolUse": {"name": "record_prepared_review", "toolUseId": "not-authorized", "input": {}}}
    ]
    agent, client, _ = agent_for([malicious], tools=[record_prepared_review])

    with pytest.raises(InvalidGeneratedOutputError, match="unauthorized tool"):
        prepare_unified_review(agent, *artwork())
    assert len(client.calls) == 1
    assert "toolConfig" not in client.calls[0]
    assert calls == []


def test_a_new_request_cannot_reuse_the_previous_request_model() -> None:
    agent, client, session = agent_for([response(payload())])
    prepare_unified_review(agent, *artwork())
    with pytest.raises(IntelligenceConfigurationError, match="fresh request model"):
        prepare_unified_review(agent, *artwork())
    second = build_unified_model(session, BedrockSettings(model_id="google.gemma-3-27b-it"))
    assert isinstance(second, NativeJsonBedrockModel)
    assert second is not agent.model
    assert second.request_count == 0
    assert len(client.calls) == 1


def test_released_voice_and_rollback_prompt_are_not_changed() -> None:
    fingerprint = ETSY_SEO_RELEASE_PROMPT_BUNDLE.fingerprint
    prompt = unified_review_prompt()

    assert "Do not invent narrative, character intentions, personality, adventures" in prompt
    assert "Do not substitute a related" in prompt
    assert "without redundant search intent" in prompt
    assert "not assume access to the original image" not in prompt
    assert "{analysis_json}" not in prompt
    assert ETSY_SEO_RELEASE_PROMPT_BUNDLE.fingerprint == fingerprint


@pytest.mark.parametrize(
    "usage", [None, {}, {"inputTokens": 10, "outputTokens": 10, "totalTokens": 10}]
)
def test_provider_must_supply_truthful_usage_before_it_can_be_accepted(usage: Any) -> None:
    invalid = response(payload())
    if usage is None:
        del invalid["usage"]
    else:
        invalid["usage"] = usage
    agent, client, _ = agent_for([invalid])
    with pytest.raises(InvalidGeneratedOutputError, match="invalid token usage"):
        prepare_unified_review(agent, *artwork())
    assert len(client.calls) == 1


def test_transport_value_error_is_not_mistaken_for_structured_validation_failure() -> None:
    agent, client, _ = agent_for([ValueError("Transport configuration failed")])
    with pytest.raises(ValueError, match="Transport configuration failed"):
        prepare_unified_review(agent, *artwork())
    assert len(client.calls) == 1
