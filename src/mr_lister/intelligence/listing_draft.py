"""Provider-facing listing draft and deterministic Etsy tag selection."""

from __future__ import annotations

from typing import Annotated

from pydantic import Field, StringConstraints, model_validator

from mr_lister.contracts import ListingIntelligence
from mr_lister.contracts.models import (
    CONTRACT_VERSION,
    ContractModel,
    ContractVersion,
    EtsyTitle,
    NonEmptyText,
    ShortText,
)
from mr_lister.workflow.tag_policy import is_complete_tag_phrase, tags_are_redundant

FINAL_TAG_COUNT = 13
MIN_CANDIDATE_TAGS = 18
MAX_CANDIDATE_TAGS = 30
CandidateTagPhrase = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=60),
]


class ListingCandidateDraft(ContractModel):
    """Internal provider contract; never crosses the application boundary."""

    contract_version: ContractVersion = CONTRACT_VERSION
    title: EtsyTitle
    description: NonEmptyText
    tag_candidates: tuple[CandidateTagPhrase, ...] = Field(
        min_length=MIN_CANDIDATE_TAGS,
        max_length=MAX_CANDIDATE_TAGS,
    )
    audience: tuple[ShortText, ...] = ()
    title_rationale: NonEmptyText
    tag_rationale: NonEmptyText

    @model_validator(mode="after")
    def candidates_must_be_unique(self) -> ListingCandidateDraft:
        normalized = {" ".join(tag.casefold().split()) for tag in self.tag_candidates}
        if len(normalized) != len(self.tag_candidates):
            raise ValueError(
                "Candidate tags must be unique after case and whitespace normalization"
            )
        return self


def select_etsy_tags(
    candidates: tuple[str, ...],
    *,
    count: int = FINAL_TAG_COUNT,
) -> tuple[str, ...]:
    """Select ranked complete phrases that contribute distinct lexical search coverage.

    Never shorten, split, or invent phrases. Include-first search preserves the model's
    relevance ranking, backtracking only when necessary to obtain a complete set. Shared
    words are allowed; equivalent inflections and low-information paraphrases are not.
    """

    if not 1 <= count <= FINAL_TAG_COUNT:
        raise ValueError("Tag count must be between 1 and 13")
    if len(candidates) > MAX_CANDIDATE_TAGS:
        raise ValueError("Candidate pool exceeds the bounded 30-phrase contract")
    eligible = tuple(tag for tag in candidates if is_complete_tag_phrase(tag))
    conflicts = tuple(
        sum(
            1 << other
            for other in range(index + 1, len(eligible))
            if tags_are_redundant(tag, eligible[other])
        )
        for index, tag in enumerate(eligible)
    )
    failed: set[tuple[int, int]] = set()

    def search(available: int, needed: int) -> tuple[int, ...] | None:
        if needed == 0:
            return ()
        if available.bit_count() < needed or (available, needed) in failed:
            return None
        state = (available, needed)
        while available.bit_count() >= needed:
            first = available & -available
            index = first.bit_length() - 1
            available ^= first
            tail = search(available & ~conflicts[index], needed - 1)
            if tail is not None:
                return (index, *tail)
        failed.add(state)
        return None

    indexes = search((1 << len(eligible)) - 1, count)
    if indexes is None:
        raise ValueError(
            f"Candidate pool cannot produce {count} complete, nonredundant tags; "
            "additional relevant natural phrases are required"
        )
    return tuple(eligible[index] for index in indexes)


def finalize_listing_draft(draft: ListingCandidateDraft) -> ListingIntelligence:
    """Convert the internal provider draft to the stable application contract."""

    return ListingIntelligence(
        contract_version=draft.contract_version,
        title=draft.title,
        description=draft.description,
        tags=select_etsy_tags(draft.tag_candidates),
        audience=draft.audience,
        title_rationale=draft.title_rationale,
        tag_rationale=draft.tag_rationale,
    )
