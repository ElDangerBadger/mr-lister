"""Credential-free regression checks for the existing evaluator's execution seam."""

from __future__ import annotations

from typing import Any

import pytest
import test_live_bedrock as evaluation
from test_unified_intelligence import ScriptedClient, ScriptedSession, artwork, payload, response

from mr_lister.intelligence.listing_draft import finalize_listing_draft
from mr_lister.intelligence.prompts import (
    BASELINE_PROMPT_BUNDLE,
    ETSY_SEO_RELEASE_PROMPT_BUNDLE,
)
from mr_lister.intelligence.settings import BedrockSettings
from mr_lister.intelligence.unified import UnifiedArtworkListing
from tools.phase2_evaluation import score_case


def settings() -> BedrockSettings:
    return BedrockSettings(model_id="google.gemma-3-27b-it", max_repair_attempts=2)


def one_call(responses: list[dict[str, Any]]):
    client = ScriptedClient(responses)
    intelligence = evaluation._one_call_intelligence(
        ScriptedSession(client), settings(), ETSY_SEO_RELEASE_PROMPT_BUNDLE
    )
    return intelligence, client


def test_one_call_evaluator_preserves_result_and_reports_real_metrics(monkeypatch) -> None:
    intelligence, client = one_call([response(payload())])
    ticks = iter([1.0, 1.025, 2.0, 2.005])
    monkeypatch.setattr(evaluation, "perf_counter", lambda: next(ticks))
    source, content = artwork()

    analysis = intelligence.inspect_artwork(source, content)
    listing = intelligence.draft_listing(source, content, analysis)
    telemetry = intelligence.telemetry([])

    assert len(client.calls) == telemetry["model_calls"] == telemetry["strands_cycles"] == 1
    assert telemetry["repair_attempts"] == 0
    assert telemetry["intelligence_wall_clock_ms"] == 30
    assert listing == finalize_listing_draft(
        UnifiedArtworkListing.model_validate(payload()).listing
    )
    assert telemetry["accumulated_usage"] == {
        "inputTokens": 1000,
        "outputTokens": 600,
        "totalTokens": 1600,
    }
    assert telemetry["provider_responses"] == [
        {"latency_ms": 1, "usage": telemetry["accumulated_usage"]}
    ]
    assert len(telemetry["transport_prompt_fingerprint"]) == 64
    assert telemetry["transport_prompt_fingerprint"] != ETSY_SEO_RELEASE_PROMPT_BUNDLE.fingerprint


def test_repair_score_uses_real_provider_latency_and_accumulated_usage() -> None:
    invalid = payload()
    del invalid["analysis"]["subject"]
    first = response(invalid)
    first["metrics"]["latencyMs"] = 17
    second = response(payload(), output_tokens=700)
    second["metrics"]["latencyMs"] = 23
    intelligence, client = one_call([first, second])
    source, content = artwork()
    analysis = intelligence.inspect_artwork(source, content)
    listing = intelligence.draft_listing(source, content, analysis)
    telemetry = intelligence.telemetry([])
    score = score_case(
        evaluation.CASES[0],
        analysis=analysis,
        listing=listing,
        diagnostics=telemetry["provider_responses"],
    )
    evaluation._apply_one_call_telemetry(score, telemetry)

    assert len(client.calls) == telemetry["model_calls"] == telemetry["strands_cycles"] == 2
    assert score["repair_attempts"] == 1
    assert score["latency_ms"] == 40
    assert score["input_tokens"] == 2000
    assert score["output_tokens"] == 1300
    assert score["total_tokens"] == 3300


def test_missing_provider_timing_is_not_reported_as_zero() -> None:
    native_response = response(payload())
    del native_response["metrics"]
    intelligence, _ = one_call([native_response])
    source, content = artwork()
    analysis = intelligence.inspect_artwork(source, content)
    listing = intelligence.draft_listing(source, content, analysis)
    telemetry = intelligence.telemetry([])
    score = score_case(
        evaluation.CASES[0],
        analysis=analysis,
        listing=listing,
        diagnostics=telemetry["provider_responses"],
    )
    evaluation._apply_one_call_telemetry(score, telemetry)

    assert score["latency_ms"] is None
    assert score["total_tokens"] == 1600
    assert telemetry["intelligence_wall_clock_ms"] > 0


@pytest.mark.parametrize("changed", ["artwork", "content", "analysis"])
def test_one_call_evaluator_rejects_cross_source_result_reuse(changed: str) -> None:
    intelligence, client = one_call([response(payload())])
    source, content = artwork()
    analysis = intelligence.inspect_artwork(source, content)
    if changed == "artwork":
        source = source.model_copy(update={"filename": "different.png"})
    elif changed == "content":
        content += b"changed"
    else:
        analysis = analysis.model_copy(update={"subject": "A different subject"})

    with pytest.raises(ValueError, match="exact inspection"):
        intelligence.draft_listing(source, content, analysis)
    assert len(client.calls) == 1


def test_one_call_evaluator_requires_inspection_and_rejects_repeat() -> None:
    intelligence, client = one_call([response(payload())])
    source, content = artwork()
    analysis = UnifiedArtworkListing.model_validate(payload()).analysis
    with pytest.raises(ValueError, match="exact inspection"):
        intelligence.draft_listing(source, content, analysis)
    assert not client.calls
    with pytest.raises(ValueError, match="source changed"):
        intelligence.inspect_artwork(source, content + b"changed")
    assert not client.calls
    intelligence.inspect_artwork(source, content)
    with pytest.raises(ValueError, match="already inspected"):
        intelligence.inspect_artwork(source, content)
    assert len(client.calls) == 1


def test_legacy_evaluator_preserves_two_methods_without_invented_strands_metrics(
    monkeypatch,
) -> None:
    accepted = UnifiedArtworkListing.model_validate(payload())
    listing = finalize_listing_draft(accepted.listing)
    calls = []

    class Delegate:
        def inspect_artwork(self, *args):
            calls.append(("inspect", args))
            return accepted.analysis

        def draft_listing(self, *args):
            calls.append(("draft", args))
            return listing

    intelligence = evaluation._EvaluationIntelligence(delegate=Delegate())
    ticks = iter([1.0, 1.010, 2.0, 2.020])
    monkeypatch.setattr(evaluation, "perf_counter", lambda: next(ticks))
    source, content = artwork()
    analysis = intelligence.inspect_artwork(source, content)
    assert intelligence.draft_listing(source, content, analysis) == listing
    assert calls == [("inspect", (source, content)), ("draft", (source, content, analysis))]
    telemetry = intelligence.telemetry([{"status": "accepted"}, {"status": "accepted"}])
    assert telemetry["model_calls"] == 2
    assert telemetry["strands_cycles"] is None
    assert telemetry["intelligence_wall_clock_ms"] == 30


def test_one_call_evaluator_rejects_unreleased_prompt_without_client_construction() -> None:
    with pytest.raises(ValueError, match="exact released SEO reference"):
        evaluation._one_call_intelligence(object(), settings(), BASELINE_PROMPT_BUNDLE)


def test_one_call_evaluator_rejects_unpinned_model_without_client_construction() -> None:
    other = settings().model_copy(update={"model_id": "another-model"})
    with pytest.raises(ValueError, match="released native Gemma"):
        evaluation._one_call_intelligence(object(), other, ETSY_SEO_RELEASE_PROMPT_BUNDLE)
