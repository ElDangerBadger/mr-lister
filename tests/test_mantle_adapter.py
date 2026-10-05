"""Offline Gemma 4 boundary checks; no model calls or store side effects."""

from __future__ import annotations

import base64
import copy
import json
import random
from collections import deque
from dataclasses import replace
from hashlib import sha256
from io import BytesIO
from zlib import crc32

import pytest
from PIL import Image

from mr_lister.contracts import ArtworkAnalysis
from mr_lister.intelligence.diagnostics import InMemoryDiagnosticSink
from mr_lister.intelligence.mantle import MantleListingIntelligenceAdapter
from mr_lister.intelligence.prompts import ETSY_SEO_RELEASE_PROMPT_BUNDLE
from mr_lister.intelligence.settings import BedrockSettings
from mr_lister.workflow.errors import (
    IntelligenceConfigurationError,
    InvalidGeneratedOutputError,
)
from mr_lister.workflow.validation import validate_artwork


class ScriptedMantleClient:
    def __init__(self, *results):
        self.results = deque(results)
        self.calls = []

    def complete(self, request):
        self.calls.append(copy.deepcopy(request))
        result = self.results.popleft()
        if isinstance(result, Exception):
            raise result
        return result


def analysis_payload():
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


def listing_payload():
    return {
        "title": "Geometric Badger Graphic Tee",
        "description": "A bold geometric badger design for woodland art fans.",
        "tag_candidates": [
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
            "geometric wildlife",
            "black amber",
            "compass rose",
            "camping wardrobe",
            "bold shapes",
            "wilderness fan",
            "trail keepsake",
        ],
        "audience": ["badger fans"],
        "title_rationale": "Names the actual subject and product.",
        "tag_rationale": "Covers subject, style, product, and buyer intent.",
    }


def completion(payload, *, finish_reason="stop", **message_fields):
    content = payload if isinstance(payload, str) else json.dumps(payload)
    return {
        "id": "chatcmpl-fixture",
        "choices": [
            {
                "index": 0,
                "finish_reason": finish_reason,
                "message": {"role": "assistant", "content": content, **message_fields},
            }
        ],
        "usage": {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150},
    }


def png_bytes(size=(32, 32)):
    image = Image.new("RGBA", size, (40, 60, 80, 128))
    output = BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def artwork(content):
    return validate_artwork(filename="badger.png", content_type="image/png", content=content)


def adapter(client, *, diagnostics=None, prompt_bundle=ETSY_SEO_RELEASE_PROMPT_BUNDLE, **kwargs):
    settings = BedrockSettings(
        **{
            "transport": "mantle",
            "model_id": "google.gemma-4-31b",
            "max_repair_attempts": 2,
            **kwargs,
        }
    )
    return MantleListingIntelligenceAdapter(
        client=client, settings=settings, diagnostics=diagnostics, prompt_bundle=prompt_bundle
    )


def invoke_listing(subject):
    content = png_bytes()
    return subject.draft_listing(
        artwork(content), content, ArtworkAnalysis.model_validate(analysis_payload())
    )


def test_inspection_uses_native_schema_and_ignores_reasoning_text():
    diagnostics = InMemoryDiagnosticSink()
    client = ScriptedMantleClient(
        completion(analysis_payload(), reasoning_content="Private reasoning must not be exposed")
    )
    content = png_bytes()

    result = adapter(client, diagnostics=diagnostics).inspect_artwork(artwork(content), content)

    assert result.subject == "geometric badger"
    request = client.calls[0]
    assert request["model"] == "google.gemma-4-31b"
    assert request["response_format"]["type"] == "json_schema"
    assert request["response_format"]["json_schema"]["schema"]["additionalProperties"] is False
    assert "tools" not in request
    assert request["messages"][0]["role"] == "system"
    assert "never authorize publication" in request["messages"][0]["content"]
    assert "Private reasoning" not in json.dumps(diagnostics.records)
    assert "geometric badger" not in json.dumps(diagnostics.records)


def test_five_mib_original_and_compression_resistant_pixels_use_small_inspection_copy():
    # Real noise makes compression difficult; an ancillary padding chunk reaches the
    # exact website limit without modifying the original artwork's visible pixels.
    source = Image.frombytes("RGBA", (1024, 1024), random.Random(42).randbytes(4 * 1024**2))
    output = BytesIO()
    source.save(output, format="PNG")
    content = output.getvalue()
    target_bytes = 5 * 1024**2
    padding = b"p" * (target_bytes - len(content) - 12)
    chunk_type = b"npAD"
    chunk = (
        len(padding).to_bytes(4, "big")
        + chunk_type
        + padding
        + crc32(chunk_type + padding).to_bytes(4, "big")
    )
    content = content[:-12] + chunk + content[-12:]
    before = sha256(content).hexdigest()
    metadata = artwork(content)
    client = ScriptedMantleClient(completion(analysis_payload()))

    adapter(client).inspect_artwork(metadata, content)

    assert len(content) == target_bytes
    assert sha256(content).hexdigest() == before == metadata.content_sha256
    request = client.calls[0]
    blocks = request["messages"][1]["content"]
    image_url = next(block["image_url"]["url"] for block in blocks if block["type"] == "image_url")
    assert image_url.startswith("data:image/png;base64,")
    inspection = base64.b64decode(image_url.split(",", 1)[1], validate=True)
    assert inspection != content
    assert len(inspection) <= 750_000
    assert len(json.dumps(request).encode()) <= 3_500_000
    with Image.open(BytesIO(inspection)) as rendition:
        assert max(rendition.size) <= 1600
        assert rendition.mode == "RGB"


def test_large_dimension_transparent_artwork_is_scaled_without_changing_print_file():
    content = png_bytes((6000, 3000))
    before = sha256(content).hexdigest()
    client = ScriptedMantleClient(completion(analysis_payload()))

    adapter(client).inspect_artwork(artwork(content), content)

    blocks = client.calls[0]["messages"][1]["content"]
    image_url = next(block["image_url"]["url"] for block in blocks if block["type"] == "image_url")
    with Image.open(BytesIO(base64.b64decode(image_url.split(",", 1)[1]))) as rendition:
        assert rendition.size == (1600, 800)
        assert rendition.mode == "RGB"
    assert sha256(content).hexdigest() == before
    assert "checkerboard" in json.dumps(blocks)


def test_whole_request_limit_includes_system_prompt_before_transport():
    client = ScriptedMantleClient(completion(analysis_payload()))
    prompts = replace(ETSY_SEO_RELEASE_PROMPT_BUNDLE, system="x" * 3_500_000)
    content = png_bytes()

    with pytest.raises(IntelligenceConfigurationError):
        adapter(client, prompt_bundle=prompts).inspect_artwork(artwork(content), content)

    assert client.calls == []


def test_repair_history_is_rechecked_before_an_oversized_second_request():
    client = ScriptedMantleClient(
        completion("private response " + "x" * 3_500_000), completion(analysis_payload())
    )
    content = png_bytes()

    with pytest.raises(IntelligenceConfigurationError):
        adapter(client).inspect_artwork(artwork(content), content)

    assert len(client.calls) == 1


@pytest.mark.parametrize("finish_reason", ["length", "tool_calls", "content_filter", None])
def test_incomplete_or_unsafe_completion_cannot_become_a_listing(finish_reason):
    response = completion(listing_payload(), finish_reason=finish_reason)
    client = ScriptedMantleClient(response, response, completion(listing_payload()))

    with pytest.raises(InvalidGeneratedOutputError):
        invoke_listing(adapter(client))

    assert len(client.calls) == 1


@pytest.mark.parametrize(
    "fields",
    [
        {"tool_calls": [{"id": "call-1", "type": "function", "function": {"name": "publish"}}]},
        {"refusal": "I cannot provide that"},
        {"role": "tool"},
    ],
)
def test_refusals_tools_and_wrong_role_fail_even_with_valid_json(fields):
    response = completion(listing_payload(), **fields)
    client = ScriptedMantleClient(response, response)

    with pytest.raises(InvalidGeneratedOutputError):
        invoke_listing(adapter(client))

    assert len(client.calls) == 1


def test_valid_json_cannot_grant_publish_authority_and_repair_is_bounded():
    response = completion({**listing_payload(), "publish_enabled": True})
    client = ScriptedMantleClient(response, response, completion(listing_payload()))

    with pytest.raises(InvalidGeneratedOutputError):
        invoke_listing(adapter(client))

    assert len(client.calls) == 2
    assert len(client.results) == 1


def test_invalid_analysis_is_repaired_using_application_contract():
    invalid = {**analysis_payload(), "confidence": 8.0}
    client = ScriptedMantleClient(completion(invalid), completion(analysis_payload()))
    content = png_bytes()

    result = adapter(client).inspect_artwork(artwork(content), content)

    assert result.confidence == 0.96
    assert len(client.calls) == 2
    assert [message["role"] for message in client.calls[1]["messages"]][-2:] == [
        "assistant",
        "user",
    ]
    assert "confidence" in json.dumps(client.calls[1]["messages"][-1])


def test_tag_repair_preserves_accepted_copy_and_returns_thirteen_whole_tags():
    original = listing_payload()
    original["tag_candidates"] = [f"geometric woodland badger {index}" for index in range(18)]
    repaired = listing_payload()
    repaired.update(
        title="Unexpected rewritten title", description="Unexpected rewritten description"
    )
    client = ScriptedMantleClient(completion(original), completion(repaired))

    result = invoke_listing(adapter(client))

    assert result.title == original["title"]
    assert result.description == original["description"]
    assert len(result.tags) == 13
    assert set(result.tags) <= set(repaired["tag_candidates"])
    assert all(len(tag) <= 20 for tag in result.tags)
    assert len(client.calls) == 2
    assert all(
        block.get("type") != "image_url"
        for request in client.calls
        for message in request["messages"]
        if isinstance(message["content"], list)
        for block in message["content"]
    )


class ScriptedHTTPSender:
    def __init__(self, *results):
        self.results = deque(results)
        self.calls = []

    def send(self, request):
        self.calls.append(request)
        result = self.results.popleft()
        if isinstance(result, Exception):
            raise result
        return result


class ResponseStream:
    def __init__(self, body):
        self.body = body
        self.closed = False
        self.read_sizes = []

    def read(self, size):
        self.read_sizes.append(size)
        return self.body[:size]

    def close(self):
        self.closed = True


class RotatingCredentials:
    def __init__(self):
        self.freezes = 0

    def get_frozen_credentials(self):
        from botocore.credentials import ReadOnlyCredentials

        self.freezes += 1
        return ReadOnlyCredentials(
            f"AKID{self.freezes}", "private-aws-secret", f"private-session-token-{self.freezes}"
        )


class LocalSession:
    def __init__(self, credentials=None):
        self.credentials = credentials or RotatingCredentials()

    def get_credentials(self):
        return self.credentials


def http_response(status=200, *, body=None, headers=None):
    from botocore.awsrequest import AWSResponse

    if body is None:
        body = json.dumps(completion(analysis_payload())).encode()
    return AWSResponse(
        "https://bedrock-mantle.us-west-2.api.aws/openai/v1/chat/completions",
        status,
        headers or {"content-type": "application/json", "x-amzn-requestid": "request-123"},
        ResponseStream(body),
    )


def transport(sender, *, session=None, region="us-west-2"):
    from mr_lister.intelligence.mantle import SigV4MantleClient

    return SigV4MantleClient(session=session or LocalSession(), region=region, sender=sender)


def simple_request():
    return {
        "model": "google.gemma-4-31b",
        "messages": [{"role": "user", "content": "Return one JSON object"}],
        "max_tokens": 2048,
    }


def header(request, name):
    value = request.headers[name]
    return value.decode() if isinstance(value, bytes) else value


def test_transport_refreshes_temporary_credentials_and_signs_only_the_fixed_aws_target():
    credentials = RotatingCredentials()
    sender = ScriptedHTTPSender(http_response(), http_response())
    client = transport(sender, session=LocalSession(credentials))

    client.complete(simple_request())
    client.complete(simple_request())

    assert credentials.freezes == 2
    for index, request in enumerate(sender.calls, 1):
        assert request.url == (
            "https://bedrock-mantle.us-west-2.api.aws/openai/v1/chat/completions"
        )
        assert request.method == "POST"
        assert f"Credential=AKID{index}/" in header(request, "Authorization")
        assert "/us-west-2/bedrock-mantle/aws4_request" in header(request, "Authorization")
        assert header(request, "X-Amz-Security-Token") == f"private-session-token-{index}"
        assert "private-aws-secret" not in repr(request.headers)
        assert "private" not in request.body.decode()
        assert len(request.body) <= 3_500_000


@pytest.mark.parametrize(
    "region",
    [
        "us-west-2.evil.invalid",
        "us-west-2/evil",
        "https://evil.invalid",
        "cn-north-1",
    ],
)
def test_transport_rejects_unapproved_signing_targets(region):
    sender = ScriptedHTTPSender(http_response())

    with pytest.raises((IntelligenceConfigurationError, ValueError)):
        transport(sender, region=region).complete(simple_request())

    assert sender.calls == []


def test_actual_transport_rechecks_body_budget_including_utf8_bytes():
    sender = ScriptedHTTPSender(http_response())
    request = simple_request()
    # Fewer than 3.5 million characters, but too many bytes in either UTF-8 or
    # escaped JSON: a character-count guard would miss this oversized payload.
    request["messages"][0]["content"] = "波" * 1_200_000

    with pytest.raises(IntelligenceConfigurationError):
        transport(sender).complete(request)

    assert sender.calls == []


@pytest.mark.parametrize("status", [301, 302, 400, 401, 403, 404, 422])
def test_configuration_failures_do_not_retry_or_expose_provider_body(status):
    response = http_response(status, body=b"Authorization: private-provider-body")
    sender = ScriptedHTTPSender(response, http_response())
    diagnostics = InMemoryDiagnosticSink()
    subject = adapter(transport(sender), diagnostics=diagnostics)

    with pytest.raises(IntelligenceConfigurationError) as captured:
        invoke_listing(subject)

    assert len(sender.calls) == 1
    assert "private-provider-body" not in str(captured.value)
    assert "private-provider-body" not in json.dumps(diagnostics.records)
    assert "private-session-token" not in json.dumps(diagnostics.records)
    assert captured.value.__cause__ is None


@pytest.mark.parametrize("status", [408, 429, 500, 502, 503, 504])
def test_temporary_http_failures_are_bounded_and_return_safe_retryable_errors(status):
    from mr_lister.workflow.errors import IntelligenceUnavailableError

    sender = ScriptedHTTPSender(http_response(status, body=b"private detail"), http_response())

    with pytest.raises(IntelligenceUnavailableError) as captured:
        transport(sender).complete(simple_request())

    assert len(sender.calls) == 1
    assert "private detail" not in str(captured.value)
    assert captured.value.__cause__ is None


def test_transport_timeouts_are_sanitized_without_repeating_paid_inference():
    from botocore.exceptions import ReadTimeoutError

    from mr_lister.workflow.errors import IntelligenceUnavailableError

    sender = ScriptedHTTPSender(
        ReadTimeoutError(endpoint_url="https://private.invalid/?secret=private-token"),
        http_response(),
    )

    with pytest.raises(IntelligenceUnavailableError) as captured:
        transport(sender).complete(simple_request())

    assert len(sender.calls) == 1
    assert "private" not in str(captured.value)
    assert captured.value.__cause__ is None


def test_default_transport_has_bounded_connection_and_response_timeouts(monkeypatch):
    import mr_lister.intelligence.mantle as mantle

    configured = {}

    def sender_factory(**kwargs):
        configured.update(kwargs)
        return ScriptedHTTPSender(http_response())

    monkeypatch.setattr(mantle, "URLLib3Session", sender_factory)

    mantle.SigV4MantleClient(session=LocalSession(), region="us-west-2")

    assert configured["timeout"] == (10, 300)


def test_overlarge_provider_response_is_rejected_without_retaining_raw_body():
    response = http_response(body=b"private-response" + b"x" * 1_000_000)
    sender = ScriptedHTTPSender(response)

    with pytest.raises(InvalidGeneratedOutputError) as captured:
        transport(sender).complete(simple_request())

    assert "private-response" not in str(captured.value)
    assert response.raw.closed


def test_long_lived_credentials_are_not_used_for_candidate_transport():
    from botocore.credentials import Credentials

    sender = ScriptedHTTPSender(http_response())
    session = LocalSession(Credentials("AKID", "private-secret"))

    with pytest.raises(IntelligenceConfigurationError):
        transport(sender, session=session).complete(simple_request())

    assert sender.calls == []


@pytest.mark.parametrize(
    "body",
    [
        b'{"choices":[],"choices":[]}',
        b'{"choices":[],"cost":NaN}',
        b"[]",
        b"not json",
    ],
)
def test_malformed_http_envelope_fails_closed_and_closes_stream(body):
    response = http_response(body=body)
    sender = ScriptedHTTPSender(response)

    with pytest.raises(InvalidGeneratedOutputError):
        transport(sender).complete(simple_request())

    assert response.raw.closed
    assert response.raw.read_sizes == [1_000_001]


def test_http_error_body_is_not_read_or_logged():
    response = http_response(403, body=b"private-token private-artwork")
    sender = ScriptedHTTPSender(response)

    with pytest.raises(IntelligenceConfigurationError):
        transport(sender).complete(simple_request())

    assert response.raw.closed
    assert response.raw.read_sizes == []


def test_aws_signing_does_not_log_credential_material_even_at_debug_level(caplog):
    import logging

    sender = ScriptedHTTPSender(http_response())
    with caplog.at_level(logging.DEBUG):
        transport(sender).complete(simple_request())

    assert "private-session-token" not in caplog.text
    assert "private-aws-secret" not in caplog.text
    assert "Credential=AKID" not in caplog.text


def test_missing_credentials_fail_before_sending_any_request():
    class MissingCredentialsSession:
        def get_credentials(self):
            return None

    sender = ScriptedHTTPSender(http_response())

    with pytest.raises(IntelligenceConfigurationError):
        transport(sender, session=MissingCredentialsSession()).complete(simple_request())

    assert sender.calls == []


@pytest.mark.parametrize("model_id", ["google.gemma-4-31b", "google.gemma-4-26b-a4b"])
def test_both_candidate_models_use_the_same_contract_boundary(model_id):
    client = ScriptedMantleClient(completion(listing_payload()))

    result = invoke_listing(adapter(client, model_id=model_id))

    assert len(result.tags) == 13
    assert client.calls[0]["model"] == model_id
    assert client.calls[0]["response_format"]["json_schema"]["strict"] is True


def test_artwork_repairs_stop_at_the_configured_limit():
    client = ScriptedMantleClient(
        completion("not json"),
        completion("not json"),
        completion("not json"),
        completion(analysis_payload()),
    )
    content = png_bytes()

    with pytest.raises(InvalidGeneratedOutputError):
        adapter(client).inspect_artwork(artwork(content), content)

    assert len(client.calls) == 3
    assert len(client.results) == 1


@pytest.mark.parametrize(
    "response",
    [
        {"choices": []},
        {"choices": [completion({})["choices"][0]] * 2},
        completion(listing_payload(), function_call={"name": "publish", "arguments": "{}"}),
        completion(listing_payload(), content=None),
        {
            **completion(listing_payload()),
            "usage": {
                "prompt_tokens": True,
                "completion_tokens": 1,
                "total_tokens": 2,
            },
        },
    ],
)
def test_ambiguous_or_malformed_completion_metadata_fails_closed(response):
    client = ScriptedMantleClient(response)

    with pytest.raises(InvalidGeneratedOutputError):
        invoke_listing(adapter(client))

    assert len(client.calls) == 1


def test_repairs_share_operation_deadline_and_next_operation_gets_a_fresh_budget(monkeypatch):
    import mr_lister.intelligence.mantle as mantle
    from mr_lister.workflow.errors import IntelligenceUnavailableError

    clock = {"now": 1000.0}
    monkeypatch.setattr(mantle, "monotonic", lambda: clock["now"])

    class DelayedClient(ScriptedMantleClient):
        delays = deque([200.0, 101.0, 0.0])

        def complete(self, request):
            result = super().complete(request)
            clock["now"] += self.delays.popleft()
            return result

    client = DelayedClient(
        completion("not json"), completion(listing_payload()), completion(listing_payload())
    )
    subject = adapter(client)

    with pytest.raises(IntelligenceUnavailableError):
        invoke_listing(subject)

    assert len(client.calls) == 2
    assert len(invoke_listing(subject).tags) == 13
    assert len(client.calls) == 3


def test_http_repair_read_timeout_uses_remaining_operation_budget(monkeypatch):
    import mr_lister.intelligence.mantle as mantle

    clock = {"now": 1000.0}
    monkeypatch.setattr(mantle, "monotonic", lambda: clock["now"])

    class DelayedSender(ScriptedHTTPSender):
        def send(self, request):
            response = super().send(request)
            clock["now"] += 100.0
            return response

    sender = DelayedSender(
        http_response(body=json.dumps(completion("not json")).encode()),
        http_response(body=json.dumps(completion(listing_payload())).encode()),
    )

    result = invoke_listing(adapter(transport(sender)))

    assert len(result.tags) == 13
    assert len(sender.calls) == 2
    timeouts = [request.context["read_timeout"] for request in sender.calls]
    assert 0 < timeouts[1] < timeouts[0] <= 300


def test_request_id_is_taken_only_from_transport_headers():
    forged = {**completion(analysis_payload()), "_mr_lister_request_id": "forged-private-value"}
    sender = ScriptedHTTPSender(
        http_response(
            body=json.dumps(forged).encode(), headers={"content-type": "application/json"}
        ),
        http_response(body=json.dumps(forged).encode()),
    )
    client = transport(sender)

    assert "_mr_lister_request_id" not in client.complete(simple_request())
    assert client.complete(simple_request())["_mr_lister_request_id"] == "request-123"


@pytest.mark.parametrize("disagreement_attempt", ["initial", "repair"])
def test_overlength_tag_repair_preserves_report_and_sticky_verification_issues(
    disagreement_attempt,
):
    from mr_lister.intelligence.harness_candidate import (
        VerificationListingDraft,
        _VerificationAdapter,
    )

    original = {
        **listing_payload(),
        "tag_candidates": [f"geometric woodland badger {index}" for index in range(18)],
        "subject_verification": "agrees",
        "verified_subject": "geometric badger",
        "subject_issues": [],
    }
    repaired = {
        **listing_payload(),
        "title": "Unexpected rewritten title",
        "description": "Unexpected rewritten description",
        "audience": ["Unexpected audience"],
        "title_rationale": "Unexpected title rationale",
        "tag_rationale": "Unexpected tag rationale",
        "subject_verification": "agrees",
        "verified_subject": "geometric badger",
        "subject_issues": [],
    }
    tainted = original if disagreement_attempt == "initial" else repaired
    tainted.update(
        subject_verification="disagrees",
        verified_subject="a different animal",
        subject_issues=["The visible subject contradicts the supplied interpretation"],
    )
    client = ScriptedMantleClient(completion(original), completion(repaired))
    subject = _VerificationAdapter(
        expected_subject="geometric badger",
        client=client,
        settings=BedrockSettings(
            transport="mantle", model_id="google.gemma-4-31b", max_repair_attempts=2
        ),
        prompt_bundle=ETSY_SEO_RELEASE_PROMPT_BUNDLE,
    )

    result = subject._invoke_contract(
        operation="draft_listing",
        contract=VerificationListingDraft,
        schema_name="mr_lister_verification_listing_candidate_v1",
        prompt="Return the complete requested listing and verification report.",
        image=None,
        artwork_sha256="a" * 64,
    )

    expected = VerificationListingDraft.model_validate(
        {**original, "tag_candidates": repaired["tag_candidates"]}
    )
    assert result == expected
    assert subject.reported_verification == "disagrees"
    assert subject.reported_issues
    assert len(client.calls) == 2
    assert "change only tag_candidates" in json.dumps(client.calls[1]["messages"][-1])
    assert "validation tag" not in json.dumps(client.calls)
    assert "validation tag" not in result.model_dump_json()


@pytest.mark.parametrize("with_verification", [False, True])
def test_listing_wire_schema_adds_only_the_tested_twenty_character_tag_bound(with_verification):
    from mr_lister.intelligence.harness_candidate import VerificationListingDraft
    from mr_lister.intelligence.listing_draft import ListingCandidateDraft
    from mr_lister.intelligence.mantle import mantle_output_schema
    from mr_lister.intelligence.schema import bedrock_output_schema

    contract = VerificationListingDraft if with_verification else ListingCandidateDraft
    payload = listing_payload()
    if with_verification:
        payload.update(
            subject_verification="agrees", verified_subject="geometric badger", subject_issues=[]
        )
    client = ScriptedMantleClient(completion(payload))

    result = adapter(client)._invoke_contract(
        operation="draft_listing",
        contract=contract,
        schema_name="mr_lister_listing_schema_boundary_test",
        prompt="Return the requested listing contract.",
        image=None,
        artwork_sha256="a" * 64,
    )

    assert isinstance(result, contract)
    wire_schema = client.calls[0]["response_format"]["json_schema"]["schema"]
    assert wire_schema == mantle_output_schema(contract)
    without_tested_bound = copy.deepcopy(wire_schema)
    assert without_tested_bound["properties"]["tag_candidates"]["items"].pop("maxLength") == 20
    assert without_tested_bound == bedrock_output_schema(contract)
    assert contract.model_json_schema()["properties"]["tag_candidates"]["items"]["maxLength"] == 20


def test_mantle_non_candidate_schemas_remain_exactly_the_generic_sanitized_schema():
    from mr_lister.contracts import ListingIntelligence
    from mr_lister.intelligence.harness_candidate import EvidenceBrief
    from mr_lister.intelligence.mantle import mantle_output_schema
    from mr_lister.intelligence.schema import bedrock_output_schema

    for contract in (ArtworkAnalysis, EvidenceBrief, ListingIntelligence):
        assert mantle_output_schema(contract) == bedrock_output_schema(contract)

    client = ScriptedMantleClient(completion(analysis_payload()))
    content = png_bytes()
    adapter(client).inspect_artwork(artwork(content), content)
    assert client.calls[0]["response_format"]["json_schema"]["schema"] == bedrock_output_schema(
        ArtworkAnalysis
    )
