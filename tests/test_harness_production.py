"""Production harness pairing, source integrity and human-approval boundaries (offline)."""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from test_harness_candidate import ScriptedClient, artwork, draft, evidence, png_bytes, response

from mr_lister.intelligence import harness_candidate, harness_production
from mr_lister.intelligence.harness_candidate import (
    EvidenceBrief,
    HarnessCandidateAdapter,
    HarnessResult,
    VerifiedProductContext,
    candidate_prompt_bundles,
)
from mr_lister.intelligence.harness_production import (
    PRODUCTION_HARNESS_PROMPT_FINGERPRINT,
    UNCALIBRATED_CONFIDENCE_NOTE,
    HarnessProductionAdapter,
    build_harness_production_adapter,
)
from mr_lister.intelligence.settings import BedrockSettings
from mr_lister.workflow.errors import IntelligenceConfigurationError, InvalidGeneratedOutputError


def settings():
    return BedrockSettings(transport="mantle", model_id="google.gemma-4-31b", max_repair_attempts=2)


def accepted(source, listing, *, subject="badger explorer", revision="v6"):
    bundle = candidate_prompt_bundles(revision=revision)["full"]
    return HarnessResult(
        state="accepted_for_evaluation",
        artwork_sha256=source.content_sha256,
        brief=EvidenceBrief.model_validate(evidence(supported_subject=subject)),
        listing=listing,
        subject_verification="agrees",
        prompt_version=bundle.version,
        prompt_fingerprint=bundle.fingerprint,
    )


def test_factory_pins_v6_product_context_settings_and_prompt_before_clients(monkeypatch):
    factory = Mock(return_value=object())
    monkeypatch.setattr(harness_production, "build_harness_candidate_adapter", factory)
    session = object()
    adapter = build_harness_production_adapter(settings(), session=session)
    assert callable(adapter.prepare_listing)
    assert factory.call_args.kwargs == {
        "session": session,
        "diagnostics": None,
        "product_context": VerifiedProductContext(product_type="T-shirt"),
        "revision": "v6",
    }
    assert (
        PRODUCTION_HARNESS_PROMPT_FINGERPRINT
        == candidate_prompt_bundles(revision="v6")["full"].fingerprint
    )
    assert not hasattr(adapter, "approve")
    assert not hasattr(adapter, "publish")


@pytest.mark.parametrize(
    "updates",
    [
        {"model_id": "google.gemma-4-26b-a4b"},
        {"region": "us-east-1"},
        {"temperature": 0.5},
        {"max_tokens": 4096},
        {"max_repair_attempts": 1},
    ],
)
def test_runtime_settings_drift_fails_before_factory(monkeypatch, updates):
    factory = Mock()
    monkeypatch.setattr(harness_production, "build_harness_candidate_adapter", factory)
    with pytest.raises(IntelligenceConfigurationError):
        build_harness_production_adapter(settings().model_copy(update=updates))
    factory.assert_not_called()


def test_prompt_drift_fails_before_factory(monkeypatch):
    factory = Mock()
    monkeypatch.setattr(harness_production, "build_harness_candidate_adapter", factory)
    monkeypatch.setattr(harness_production, "PRODUCTION_HARNESS_PROMPT_FINGERPRINT", "0" * 64)
    with pytest.raises(IntelligenceConfigurationError):
        build_harness_production_adapter(settings())
    factory.assert_not_called()


def test_restored_two_call_harness_retains_standard_image_budget(monkeypatch):
    content = png_bytes()
    source = artwork(content)
    client = ScriptedClient(response(evidence()), response(draft()))
    spy = Mock(wraps=harness_candidate.prepare_bedrock_image)
    monkeypatch.setattr(harness_candidate, "prepare_bedrock_image", spy)
    adapter = HarnessProductionAdapter(
        HarnessCandidateAdapter(
            client=client,
            settings=settings(),
            product_context=VerifiedProductContext(),
            revision="v6",
        )
    )
    analysis, listing = adapter.prepare_listing(source, content)
    assert len(client.calls) == 2
    assert spy.call_args.kwargs == {"max_side": 1600, "max_bytes": 750_000}
    assert analysis.subject == "badger explorer"
    assert UNCALIBRATED_CONFIDENCE_NOTE in analysis.safety_flags
    assert listing.title == "Badger Explorer Graphic T-Shirt"
    assert content == png_bytes()


def test_v6_factory_rejects_an_otherwise_accepted_previous_v4_result(monkeypatch, listing):
    content = png_bytes()
    source = artwork(content)
    stale_result = accepted(source, listing, revision="v4")
    prepare = Mock(return_value=stale_result)
    factory = Mock(return_value=SimpleNamespace(prepare=prepare))
    monkeypatch.setattr(harness_production, "build_harness_candidate_adapter", factory)

    adapter = build_harness_production_adapter(settings(), session=object())

    assert factory.call_args.kwargs["revision"] == "v6"
    assert stale_result.state == "accepted_for_evaluation"
    assert stale_result.subject_verification == "agrees"
    assert stale_result.listing is not None
    assert not stale_result.issues
    with pytest.raises(InvalidGeneratedOutputError, match="requires further review"):
        adapter.prepare_listing(source, content)
    prepare.assert_called_once_with(source, content)


@pytest.mark.parametrize(
    "updates",
    [
        {"state": "review_required"},
        {"listing": None},
        {"issues": ("Check subject",)},
        {"subject_verification": "disagrees"},
        {"subject_verification": "unresolved"},
        {"artwork_sha256": "a" * 64},
        {"prompt_fingerprint": "a" * 64},
        {"prompt_version": "old"},
    ],
)
def test_rejected_or_unbound_result_cannot_become_production_draft(listing, updates):
    content = png_bytes()
    source = artwork(content)
    result = accepted(source, listing).model_copy(update=updates)
    adapter = HarnessProductionAdapter(SimpleNamespace(prepare=lambda *_: result))
    with pytest.raises(InvalidGeneratedOutputError):
        adapter.prepare_listing(source, content)


@pytest.mark.parametrize(
    "updates",
    [
        {"subject_status": "uncertain"},
        {"supported_subject": None},
        {"unresolved_alternatives": ("A different subject",)},
    ],
)
def test_uncertain_evidence_fails_closed_even_if_writer_claims_agreement(listing, updates):
    content = png_bytes()
    source = artwork(content)
    result = accepted(source, listing)
    result = result.model_copy(update={"brief": result.brief.model_copy(update=updates)})
    with pytest.raises(InvalidGeneratedOutputError):
        HarnessProductionAdapter(SimpleNamespace(prepare=lambda *_: result)).prepare_listing(
            source, content
        )


def test_tampered_source_never_reaches_harness(listing):
    content = png_bytes()
    source = artwork(content)
    prepare = Mock(return_value=accepted(source, listing))
    with pytest.raises(InvalidGeneratedOutputError):
        HarnessProductionAdapter(SimpleNamespace(prepare=prepare)).prepare_listing(
            source, content + b"x"
        )
    prepare.assert_not_called()


def test_shared_adapter_has_no_cross_request_cache_or_second_call(listing):
    contents = [png_bytes((10, 20, 30, 255)), png_bytes((90, 80, 70, 255))]
    sources = [artwork(content) for content in contents]
    barrier = Barrier(2)
    calls = []

    def prepare(source, content):
        calls.append((source, content))
        barrier.wait(timeout=5)
        index = next(
            i for i, item in enumerate(sources) if item.content_sha256 == source.content_sha256
        )
        return accepted(
            source,
            listing.model_copy(update={"title": f"Listing {index}"}),
            subject=f"Subject {index}",
        )

    adapter = HarnessProductionAdapter(SimpleNamespace(prepare=prepare))
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(adapter.prepare_listing, source, content)
            for source, content in zip(sources, contents, strict=True)
        ]
        results = [future.result(timeout=10) for future in futures]
    assert len(calls) == 2
    assert [(analysis.subject, result.title) for analysis, result in results] == [
        ("Subject 0", "Listing 0"),
        ("Subject 1", "Listing 1"),
    ]
