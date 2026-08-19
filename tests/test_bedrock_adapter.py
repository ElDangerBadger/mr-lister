from __future__ import annotations

import json
from collections import deque
from io import BytesIO

import boto3
import pytest
from botocore.exceptions import ClientError, EndpointConnectionError, NoCredentialsError
from botocore.stub import Stubber
from PIL import Image

from mr_lister.contracts import ArtworkAnalysis
from mr_lister.intelligence.bedrock import BedrockListingIntelligenceAdapter
from mr_lister.intelligence.diagnostics import InMemoryDiagnosticSink
from mr_lister.intelligence.settings import BedrockSettings
from mr_lister.workflow.errors import (
    IntelligenceConfigurationError,
    IntelligenceUnavailableError,
    InvalidGeneratedOutputError,
)
from mr_lister.workflow.models import ArtworkInput
from mr_lister.workflow.validation import validate_artwork


class ScriptedConverseClient:
    def __init__(self, *results):
        self.results = deque(results)
        self.calls = []

    def converse(self, **kwargs):
        self.calls.append(kwargs)
        result = self.results.popleft()
        if isinstance(result, Exception):
            raise result
        return result


def response(payload: object, *, stop_reason: str = "end_turn") -> dict:
    raw = payload if isinstance(payload, str) else json.dumps(payload)
    return {
        "ResponseMetadata": {"RequestId": "request-123"},
        "output": {"message": {"role": "assistant", "content": [{"text": raw}]}},
        "stopReason": stop_reason,
        "usage": {"inputTokens": 100, "outputTokens": 50, "totalTokens": 150},
        "metrics": {"latencyMs": 321},
    }


def artwork_analysis() -> dict:
    return {
        "subject": "geometric badger",
        "visual_elements": ["angular badger face", "amber compass"],
        "styles": ["bold vector"],
        "themes": ["woodland"],
        "visible_text": [],
        "audience_hypotheses": ["badger fans"],
        "color_notes": ["black and amber"],
        "safety_flags": [],
        "confidence": 0.96,
    }


def listing(*, tag_count: int = 13) -> dict:
    tags = [
        "badger portrait",
        "woodland explorer",
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
    ]
    return {
        "title": "Geometric Badger Graphic Tee",
        "description": "A bold geometric badger design for woodland art fans.",
        "tags": tags[:tag_count],
        "audience": ["badger fans"],
        "title_rationale": "Names the actual subject and product.",
        "tag_rationale": "Covers subject, style, product, and buyer intent.",
    }


def transparent_png() -> bytes:
    image = Image.new("RGBA", (16, 16), (255, 255, 255, 0))
    image.putpixel((8, 8), (0, 0, 0, 255))
    output = BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def artwork_input(content: bytes) -> ArtworkInput:
    return validate_artwork(
        filename="geometric_badger.png",
        content_type="image/png",
        content=content,
    )


def build_adapter(client, diagnostics=None, **settings):
    return BedrockListingIntelligenceAdapter(
        client=client,
        settings=BedrockSettings(**settings),
        diagnostics=diagnostics,
    )


def test_inspection_uses_converse_image_and_provider_compatible_schema() -> None:
    content = transparent_png()
    client = ScriptedConverseClient(response(artwork_analysis()))
    adapter = build_adapter(client)

    result = adapter.inspect_artwork(artwork_input(content), content)

    assert result.subject == "geometric badger"
    request = client.calls[0]
    assert request["modelId"] == "us.anthropic.claude-sonnet-4-6"
    image_bytes = request["messages"][0]["content"][0]["image"]["source"]["bytes"]
    assert image_bytes != content
    assert b"geometric badger" not in image_bytes
    schema = json.loads(request["outputConfig"]["textFormat"]["structure"]["jsonSchema"]["schema"])
    assert "minimum" not in schema["properties"]["confidence"]
    assert schema["properties"]["visual_elements"]["type"] == "array"
    assert schema["additionalProperties"] is False


def test_nova_uses_prompted_json_without_native_structured_output() -> None:
    content = transparent_png()
    client = ScriptedConverseClient(response(artwork_analysis()))
    adapter = build_adapter(
        client,
        model_id="us.amazon.nova-2-lite-v1:0",
        output_mode="prompted_json",
    )

    result = adapter.inspect_artwork(artwork_input(content), content)

    assert result.subject == "geometric badger"
    request = client.calls[0]
    assert request["modelId"] == "us.amazon.nova-2-lite-v1:0"
    assert "outputConfig" not in request
    prompt = request["messages"][0]["content"][-1]["text"]
    assert "Return only one JSON object matching this JSON Schema exactly" in prompt
    assert '"additionalProperties":false' in prompt
    assert '"confidence"' in prompt


def test_nova_accepts_one_json_fence_then_applies_the_strict_contract() -> None:
    content = transparent_png()
    fenced = f"```json\n{json.dumps(artwork_analysis())}\n```"
    client = ScriptedConverseClient(response(fenced))
    adapter = build_adapter(client, output_mode="prompted_json")

    result = adapter.inspect_artwork(artwork_input(content), content)

    assert result.subject == "geometric badger"
    assert len(client.calls) == 1


@pytest.mark.parametrize(
    "raw_output",
    [
        "Here is the result:\n```json\n{}\n```",
        "```javascript\n{}\n```",
        "```json\n{}",
        "```json\n```json\n{}\n```\n```",
    ],
)
def test_nova_rejects_commentary_or_malformed_json_fences(raw_output: str) -> None:
    content = transparent_png()
    client = ScriptedConverseClient(response(raw_output))
    adapter = build_adapter(client, output_mode="prompted_json", max_repair_attempts=0)

    with pytest.raises(InvalidGeneratedOutputError, match="bounded repair"):
        adapter.inspect_artwork(artwork_input(content), content)


def test_native_structured_output_does_not_normalize_markdown_fences() -> None:
    content = transparent_png()
    fenced = f"```json\n{json.dumps(artwork_analysis())}\n```"
    client = ScriptedConverseClient(response(fenced))
    adapter = build_adapter(client, output_mode="native_json_schema", max_repair_attempts=0)

    with pytest.raises(InvalidGeneratedOutputError, match="bounded repair"):
        adapter.inspect_artwork(artwork_input(content), content)


def test_converse_request_matches_installed_botocore_service_model() -> None:
    content = transparent_png()
    client = boto3.client(
        "bedrock-runtime",
        region_name="us-west-2",
        aws_access_key_id="offline-test",
        aws_secret_access_key="offline-test",
        aws_session_token="offline-test",
    )
    with Stubber(client) as stubber:
        stubber.add_response("converse", response(artwork_analysis()))
        result = build_adapter(client).inspect_artwork(artwork_input(content), content)

    assert result.subject == "geometric badger"


def test_invalid_listing_is_repaired_once_and_revalidated() -> None:
    content = transparent_png()
    diagnostics = InMemoryDiagnosticSink()
    client = ScriptedConverseClient(response(listing(tag_count=12)), response(listing()))
    adapter = build_adapter(client, diagnostics=diagnostics, max_repair_attempts=1)

    result = adapter.draft_listing(
        artwork_input(content),
        content,
        ArtworkAnalysis.model_validate(artwork_analysis()),
    )

    assert len(result.tags) == 13
    assert len(client.calls) == 2
    assert all("image" not in block for block in client.calls[0]["messages"][0]["content"])
    repair_text = client.calls[1]["messages"][-1]["content"][0]["text"]
    assert "at least 13 items" in repair_text
    assert [record["status"] for record in diagnostics.records] == [
        "invalid_output",
        "accepted",
    ]
    assert "at least 13 items" in diagnostics.records[0]["metadata"]["validation_problems"]
    assert diagnostics.records[1]["metadata"]["validation_problems"] is None
    assert all("raw_model_output" not in record for record in diagnostics.records)
    assert diagnostics.records[-1]["prompt_version"] == "2026-08-18.5"
    assert len(diagnostics.records[-1]["response_sha256"]) == 64


def test_repeated_tag_keywords_receive_one_soft_quality_repair() -> None:
    content = transparent_png()
    repeated = listing()
    repeated["tags"] = [
        "badger portrait",
        "badger explorer",
        *listing()["tags"][2:],
    ]
    diagnostics = InMemoryDiagnosticSink()
    client = ScriptedConverseClient(response(repeated), response(listing()))
    adapter = build_adapter(client, diagnostics=diagnostics, max_repair_attempts=1)

    result = adapter.draft_listing(
        artwork_input(content),
        content,
        ArtworkAnalysis.model_validate(artwork_analysis()),
    )

    assert result.tags == tuple(listing()["tags"])
    assert len(client.calls) == 2
    repair_text = client.calls[1]["messages"][-1]["content"][0]["text"]
    assert "diversify repeated keywords: badger" in repair_text
    assert [record["status"] for record in diagnostics.records] == [
        "invalid_output",
        "accepted",
    ]


def test_final_listing_with_repetition_is_returned_for_workflow_validation() -> None:
    content = transparent_png()
    repeated = listing()
    repeated["tags"] = [
        "badger portrait",
        "badger explorer",
        *listing()["tags"][2:],
    ]
    diagnostics = InMemoryDiagnosticSink()
    client = ScriptedConverseClient(response(repeated))
    adapter = build_adapter(client, diagnostics=diagnostics, max_repair_attempts=0)

    result = adapter.draft_listing(
        artwork_input(content),
        content,
        ArtworkAnalysis.model_validate(artwork_analysis()),
    )

    assert result.tags[0:2] == ("badger portrait", "badger explorer")
    assert diagnostics.records[0]["status"] == "accepted"
    assert (
        "diversify repeated keywords: badger"
        in diagnostics.records[0]["metadata"]["validation_problems"]
    )


def test_output_outside_contract_is_rejected_after_bounded_repair() -> None:
    content = transparent_png()
    client = ScriptedConverseClient(response("not json"), response(listing(tag_count=12)))
    adapter = build_adapter(client, max_repair_attempts=1)

    with pytest.raises(InvalidGeneratedOutputError, match="bounded repair"):
        adapter.draft_listing(
            artwork_input(content),
            content,
            ArtworkAnalysis.model_validate(artwork_analysis()),
        )

    assert len(client.calls) == 2


@pytest.mark.parametrize(
    "invalid_response",
    [
        {
            "ResponseMetadata": {"RequestId": "request-missing-message"},
            "stopReason": "end_turn",
            "output": {},
        },
        {
            "ResponseMetadata": {"RequestId": "request-missing-text"},
            "stopReason": "end_turn",
            "output": {"message": {"role": "assistant", "content": [{}]}},
        },
    ],
    ids=["missing-message", "missing-text"],
)
def test_missing_response_text_fails_after_bounded_repair(
    invalid_response: dict,
) -> None:
    content = transparent_png()
    client = ScriptedConverseClient(invalid_response, invalid_response)
    adapter = build_adapter(client, max_repair_attempts=1)

    with pytest.raises(InvalidGeneratedOutputError, match="bounded repair"):
        adapter.draft_listing(
            artwork_input(content),
            content,
            ArtworkAnalysis.model_validate(artwork_analysis()),
        )

    assert len(client.calls) == 2


def test_non_end_turn_response_fails_after_bounded_repair() -> None:
    content = transparent_png()
    client = ScriptedConverseClient(
        response(listing(), stop_reason="max_tokens"),
        response(listing(), stop_reason="max_tokens"),
    )
    adapter = build_adapter(client, max_repair_attempts=1)

    with pytest.raises(InvalidGeneratedOutputError, match="bounded repair"):
        adapter.draft_listing(
            artwork_input(content),
            content,
            ArtworkAnalysis.model_validate(artwork_analysis()),
        )

    assert len(client.calls) == 2


def test_model_cannot_add_publish_authority_to_listing_contract() -> None:
    content = transparent_png()
    unauthorized_listing = {**listing(), "publish_enabled": True}
    client = ScriptedConverseClient(
        response(unauthorized_listing),
        response(unauthorized_listing),
    )
    adapter = build_adapter(client, max_repair_attempts=1)

    with pytest.raises(InvalidGeneratedOutputError, match="bounded repair"):
        adapter.draft_listing(
            artwork_input(content),
            content,
            ArtworkAnalysis.model_validate(artwork_analysis()),
        )

    assert len(client.calls) == 2


@pytest.mark.parametrize(
    ("code", "expected_error"),
    [
        ("AccessDeniedException", IntelligenceConfigurationError),
        ("ExpiredTokenException", IntelligenceConfigurationError),
        ("ThrottlingException", IntelligenceUnavailableError),
        ("UnexpectedProviderError", IntelligenceConfigurationError),
    ],
)
def test_provider_errors_are_sanitized_and_classified(code, expected_error) -> None:
    content = transparent_png()
    provider_error = ClientError(
        {
            "Error": {"Code": code, "Message": "provider detail must not escape"},
            "ResponseMetadata": {"RequestId": "request-error"},
        },
        "Converse",
    )
    diagnostics = InMemoryDiagnosticSink()
    adapter = build_adapter(
        ScriptedConverseClient(provider_error),
        diagnostics=diagnostics,
    )

    with pytest.raises(expected_error) as captured:
        adapter.inspect_artwork(artwork_input(content), content)

    assert "provider detail" not in str(captured.value)
    assert diagnostics.records[0]["error_type"] == "ClientError"
    assert diagnostics.records[0]["error_code"] == code
    assert diagnostics.records[0]["request_id"] == "request-error"


@pytest.mark.parametrize(
    ("sdk_error", "expected_error"),
    [
        (NoCredentialsError(), IntelligenceConfigurationError),
        (
            EndpointConnectionError(endpoint_url="https://private.invalid"),
            IntelligenceUnavailableError,
        ),
    ],
    ids=["missing-credentials", "transport-unavailable"],
)
def test_sdk_errors_are_sanitized_and_classified(sdk_error, expected_error) -> None:
    content = transparent_png()
    diagnostics = InMemoryDiagnosticSink()
    adapter = build_adapter(
        ScriptedConverseClient(sdk_error),
        diagnostics=diagnostics,
    )

    with pytest.raises(expected_error) as captured:
        adapter.inspect_artwork(artwork_input(content), content)

    assert "private.invalid" not in str(captured.value)
    assert diagnostics.records[0]["error_type"] == type(sdk_error).__name__
    assert diagnostics.records[0]["error_message"] == "[REDACTED]"
