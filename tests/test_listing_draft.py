from __future__ import annotations

import pytest
from pydantic import ValidationError

from mr_lister.intelligence.listing_draft import (
    ListingCandidateDraft,
    finalize_listing_draft,
    select_etsy_tags,
)
from mr_lister.workflow.validation import find_repeated_tag_keywords


def candidate_draft() -> ListingCandidateDraft:
    return ListingCandidateDraft(
        title="Geometric Badger Graphic Tee",
        description="A bold geometric badger design for woodland fans.",
        tag_candidates=(
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
        ),
        audience=("badger fans",),
        title_rationale="Names the subject and product.",
        tag_rationale="Ranks relevant alternatives for deterministic selection.",
    )


def test_selector_preserves_rank_while_skipping_keyword_collisions() -> None:
    selected = select_etsy_tags(candidate_draft().tag_candidates)

    assert len(selected) == 13
    assert selected[0] == "badger portrait"
    assert "badger explorer" not in selected
    assert find_repeated_tag_keywords(selected) == ()


def test_selector_backtracks_when_top_candidate_blocks_complete_set() -> None:
    selected = select_etsy_tags(
        ("red blue", "red", "blue", "green"),
        count=3,
    )

    assert selected == ("red", "blue", "green")


def test_selector_projects_only_unused_relevant_candidate_words_as_fallback() -> None:
    selected = select_etsy_tags(
        ("owl shirt", "owl graphic tee", "minimalist owl", "night owl shirt"),
        count=4,
    )

    assert selected == ("owl shirt", "graphic tee", "minimalist", "night")
    assert find_repeated_tag_keywords(selected) == ()


def test_selector_maximizes_complete_phrases_before_projecting() -> None:
    selected = select_etsy_tags(
        ("owl shirt", "owl graphic", "minimalist bird", "night sky"),
        count=4,
    )

    assert selected == ("owl shirt", "graphic", "minimalist bird", "night sky")


def test_selector_projects_overlength_internal_candidate_to_etsy_limit() -> None:
    selected = select_etsy_tags(
        ("visible prompt injection robot", "retro machine", "security test"),
        count=3,
    )

    assert selected == ("visible prompt robot", "retro machine", "security test")
    assert all(len(tag) <= 20 for tag in selected)


def test_selector_rejects_pool_without_feasible_combination() -> None:
    with pytest.raises(ValueError, match="cannot produce 3 tags"):
        select_etsy_tags(("badger for", "badger with", "badger and"), count=3)


def test_selector_does_not_treat_stopword_only_phrase_as_a_tag() -> None:
    with pytest.raises(ValueError, match="cannot produce 2 tags"):
        select_etsy_tags(("badger", "for a"), count=2)


def test_provider_contract_requires_unique_candidate_phrases() -> None:
    payload = candidate_draft().model_dump()
    payload["tag_candidates"] = (*payload["tag_candidates"][:-1], " BADGER   PORTRAIT ")

    with pytest.raises(ValidationError, match="Candidate tags must be unique"):
        ListingCandidateDraft.model_validate(payload)


def test_provider_contract_allows_selector_to_shorten_long_candidate() -> None:
    payload = candidate_draft().model_dump()
    payload["tag_candidates"] = (
        "anthropomorphic woodland badger explorer",
        *payload["tag_candidates"][1:],
    )

    draft = ListingCandidateDraft.model_validate(payload)
    listing = finalize_listing_draft(draft)

    assert all(len(tag) <= 20 for tag in listing.tags)


def test_finalizer_returns_stable_public_listing_contract() -> None:
    listing = finalize_listing_draft(candidate_draft())

    assert listing.title == candidate_draft().title
    assert len(listing.tags) == 13
    assert find_repeated_tag_keywords(listing.tags) == ()
    assert "tag_candidates" not in listing.model_dump()
