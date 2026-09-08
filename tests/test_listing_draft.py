from __future__ import annotations

import pytest
from pydantic import ValidationError

from mr_lister.intelligence.listing_draft import (
    ListingCandidateDraft,
    finalize_listing_draft,
    select_etsy_tags,
)
from mr_lister.workflow.tag_policy import (
    is_complete_tag_phrase,
    redundant_tag_pairs,
    tags_are_redundant,
)


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


def test_selector_preserves_rank_and_distinct_intents_with_shared_words() -> None:
    selected = select_etsy_tags(candidate_draft().tag_candidates)

    assert len(selected) == 13
    assert selected[0] == "badger portrait"
    assert "badger explorer" in selected
    assert redundant_tag_pairs(selected) == ()


def test_selector_prefers_next_complete_phrase_over_redundant_alternative() -> None:
    selected = select_etsy_tags(("octopus art", "octopus print", "animal wall decor"), count=2)
    assert selected == ("octopus art", "animal wall decor")


def test_selector_retains_natural_phrases_instead_of_projecting_fragments() -> None:
    selected = select_etsy_tags(
        ("owl shirt", "owl graphic tee", "minimalist owl", "night owl shirt", "forest bird"),
        count=4,
    )
    assert selected == ("owl shirt", "minimalist owl", "night owl shirt", "forest bird")
    assert redundant_tag_pairs(selected) == ()


def test_selector_never_fills_short_pool_with_projected_words() -> None:
    with pytest.raises(ValueError, match="cannot produce 4 complete"):
        select_etsy_tags(("owl shirt", "owl graphic", "minimalist bird", "night sky"), count=4)


def test_selector_skips_overlength_phrase_without_shortening_it() -> None:
    selected = select_etsy_tags(
        ("visible prompt injection robot", "retro machine", "security test"),
        count=2,
    )
    assert selected == ("retro machine", "security test")
    assert all(len(tag) <= 20 for tag in selected)


def test_selector_rejects_pool_without_feasible_combination() -> None:
    with pytest.raises(ValueError, match="cannot produce 3 complete"):
        select_etsy_tags(("badger for", "badger with", "badger and"), count=3)


def test_selector_does_not_treat_stopword_only_phrase_as_a_tag() -> None:
    with pytest.raises(ValueError, match="cannot produce 2 complete"):
        select_etsy_tags(("badger", "for a"), count=2)


def test_provider_contract_requires_unique_candidate_phrases() -> None:
    payload = candidate_draft().model_dump()
    payload["tag_candidates"] = (*payload["tag_candidates"][:-1], " BADGER   PORTRAIT ")

    with pytest.raises(ValidationError, match="Candidate tags must be unique"):
        ListingCandidateDraft.model_validate(payload)


def test_provider_contract_allows_selector_to_skip_long_candidate() -> None:
    payload = candidate_draft().model_dump()
    payload["tag_candidates"] = (
        "anthropomorphic woodland badger explorer",
        *payload["tag_candidates"][1:],
    )

    draft = ListingCandidateDraft.model_validate(payload)
    listing = finalize_listing_draft(draft)

    assert all(len(tag) <= 20 for tag in listing.tags)
    assert all(tag in draft.tag_candidates for tag in listing.tags)


def test_finalizer_returns_stable_public_listing_contract() -> None:
    listing = finalize_listing_draft(candidate_draft())

    assert listing.title == candidate_draft().title
    assert len(listing.tags) == 13
    assert redundant_tag_pairs(listing.tags) == ()
    assert "tag_candidates" not in listing.model_dump()


def test_finalizer_does_not_change_any_v1_prose_field() -> None:
    draft = candidate_draft()
    before = draft.model_dump(exclude={"tag_candidates"})
    assert finalize_listing_draft(draft).model_dump(exclude={"tags"}) == before


@pytest.mark.parametrize(
    ("first", "second"),
    [
        ("diamond ring", "diamond rings"),
        ("turtle art", "turtles art"),
        ("fox portrait", "foxes portrait"),
        ("octopus art", "octopuses art"),
        ("fairy garden", "fairies garden"),
        ("movie lover", "movies lover"),
        ("leaf print", "leaves print"),
        ("wolf shirt", "wolves shirt"),
        ("artist gift", "artists gifts"),
        (" BADGER  PORTRAIT ", "badger portrait"),
        ("owl t-shirt", "owl tee"),
        ("octopus art", "octopus print"),
    ],
)
def test_policy_rejects_exact_plural_and_generic_paraphrase_duplicates(first, second) -> None:
    assert tags_are_redundant(first, second)


@pytest.mark.parametrize(
    ("first", "second"),
    [
        ("diamond ring", "engagement ring"),
        ("moon moth", "lunar phases"),
        ("badger portrait", "badger explorer"),
        ("night owl shirt", "owl shirt"),
    ],
)
def test_policy_allows_distinct_phrases_despite_shared_keywords(first, second) -> None:
    assert not tags_are_redundant(first, second)
    assert select_etsy_tags((first, second), count=2) == (first, second)


@pytest.mark.parametrize(
    "phrase", ["lover", "life", "tee", "art", "lover gift", "for a", "owl for"]
)
def test_policy_rejects_obvious_orphan_or_generic_only_phrases(phrase) -> None:
    assert not is_complete_tag_phrase(phrase)


def test_selection_is_stable_and_only_emits_complete_input_phrases() -> None:
    pool = candidate_draft().tag_candidates
    first = select_etsy_tags(pool)
    assert len(first) == 13
    assert all(is_complete_tag_phrase(tag) and tag in pool for tag in first)
    assert len(set(first)) == 13
    assert select_etsy_tags(pool) == first


def test_generic_or_duplicate_candidates_never_manufacture_a_complete_set() -> None:
    pool = ("octopus art", "octopus print", "octopuses artwork", "lover gift", "wearable artwork")
    with pytest.raises(ValueError, match="additional relevant natural phrases"):
        select_etsy_tags(pool, count=2)
