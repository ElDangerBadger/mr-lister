"""Provider-facing listing draft and deterministic Etsy tag selection."""

from __future__ import annotations

from re import findall
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
from mr_lister.workflow.validation import normalized_tag_keywords

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
    """Return the first ranked combination whose searchable keywords do not overlap.

    Candidate order is the provider's relevance ranking. The depth-first include-first
    search therefore preserves stronger candidates whenever a complete valid set remains
    possible, while still recovering from an early candidate that blocks several later ones.
    """

    keyword_sets = tuple(normalized_tag_keywords(candidate) for candidate in candidates)
    memo: set[tuple[int, int, frozenset[str]]] = set()

    def search(
        index: int,
        selected: tuple[int, ...],
        used_keywords: frozenset[str],
    ) -> tuple[int, ...] | None:
        if len(selected) == count:
            return selected
        if len(selected) + len(candidates) - index < count:
            return None

        state = (index, len(selected), used_keywords)
        if state in memo:
            return None
        memo.add(state)

        for candidate_index in range(index, len(candidates)):
            if len(selected) + len(candidates) - candidate_index < count:
                break
            if len(candidates[candidate_index]) > 20:
                continue
            keywords = keyword_sets[candidate_index]
            if keywords and keywords.isdisjoint(used_keywords):
                result = search(
                    candidate_index + 1,
                    (*selected, candidate_index),
                    used_keywords | keywords,
                )
                if result is not None:
                    return result
        return None

    selected_indexes = search(0, (), frozenset())
    if selected_indexes is not None:
        return tuple(candidates[index] for index in selected_indexes)

    best_indexes: tuple[int, ...] = ()

    def retain_best_complete_phrases(
        index: int,
        selected: tuple[int, ...],
        used_keywords: frozenset[str],
    ) -> None:
        nonlocal best_indexes
        if len(selected) > len(best_indexes):
            best_indexes = selected
        if len(selected) + len(candidates) - index <= len(best_indexes):
            return
        for candidate_index in range(index, len(candidates)):
            candidate = candidates[candidate_index]
            keywords = keyword_sets[candidate_index]
            if len(candidate) <= 20 and keywords and keywords.isdisjoint(used_keywords):
                retain_best_complete_phrases(
                    candidate_index + 1,
                    (*selected, candidate_index),
                    used_keywords | keywords,
                )

    retain_best_complete_phrases(0, (), frozenset())

    # A relevant candidate pool often expresses the same subject in several phrases. Etsy can
    # combine words across tags, so retain each candidate's still-unused searchable words instead
    # of asking the model to invent filler synonyms. Complete ranked phrases remain preferred.
    selected = [(index, candidates[index]) for index in best_indexes]
    selected_normalized = {" ".join(tag.casefold().split()) for _, tag in selected}
    used_keywords = frozenset(keyword for index in best_indexes for keyword in keyword_sets[index])
    for candidate_index, candidate in enumerate(candidates):
        if candidate_index in best_indexes:
            continue
        available_tokens: list[str] = []
        available_keywords: set[str] = set()
        for token in findall(r"[A-Za-z0-9]+", candidate):
            token_keywords = normalized_tag_keywords(token)
            if not token_keywords or token_keywords & (used_keywords | available_keywords):
                continue
            proposed = " ".join((*available_tokens, token.casefold()))
            if len(proposed) > 20:
                continue
            available_tokens.append(token.casefold())
            available_keywords.update(token_keywords)
        projected = " ".join(available_tokens)
        normalized = " ".join(projected.casefold().split())
        if not available_keywords or normalized in selected_normalized:
            continue
        selected.append((candidate_index, projected))
        selected_normalized.add(normalized)
        used_keywords |= frozenset(available_keywords)
        if len(selected) == count:
            return tuple(tag for _, tag in sorted(selected))

    raise ValueError(f"Candidate pool cannot produce {count} tags without meaningful keyword reuse")


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
