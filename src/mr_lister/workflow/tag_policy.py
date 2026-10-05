"""Deterministic phrase redundancy, not a prohibition on shared search words.

The model owns relevance and natural language. This small, versioned lexical policy
recognizes duplicate inflections and low-information paraphrases; it is not a search
ranking model or a proof of semantic grounding. It never generates or rewrites a tag.
"""

from itertools import combinations
from re import findall, fullmatch

TAG_POLICY_VERSION = "2026-09-08.phrase-coverage-1"
ETSY_TAG_LENGTH_LIMIT = 20
_STOP_WORDS = frozenset({"a", "an", "and", "for", "from", "in", "of", "on", "the", "to", "with"})
_ALIASES = {
    "art": "artwork",
    "design": "artwork",
    "graphic": "artwork",
    "print": "artwork",
    "tee": "shirt",
    "tshirt": "shirt",
    "enthusiast": "fan",
    "lover": "fan",
    "present": "gift",
}
_LOW_INFORMATION = frozenset({"artwork", "shirt", "gift", "fan", "wearable"})
_SINGULAR_EXCEPTIONS = {
    "octopuses": "octopus",
    "cactuses": "cactus",
    "statuses": "status",
    "movies": "movie",
    "cookies": "cookie",
    "zombies": "zombie",
    "leaves": "leaf",
    "wolves": "wolf",
    "knives": "knife",
    "lives": "life",
}
_UNCHANGED_SINGULARS = frozenset({"canvas", "cosmos"})

# A soft selection preference only. These words can be useful searches, but changing
# generic apparel/style wording should not outrank a new subject, motif or detail.
# Do not use this signature to reject tags or validate manually edited listings.
_COVERAGE_MODIFIERS = _LOW_INFORMATION | frozenset(
    {
        "apparel",
        "aesthetic",
        "classic",
        "clothing",
        "era",
        "fashion",
        "groovy",
        "inspired",
        "look",
        "retro",
        "style",
        "theme",
        "top",
        "vibe",
        "vintage",
        "wear",
    }
)


def _keyword_root(token: str) -> str:
    if token in _SINGULAR_EXCEPTIONS:
        return _SINGULAR_EXCEPTIONS[token]
    if token in _UNCHANGED_SINGULARS:
        return token
    if len(token) > 4 and token.endswith("ies"):
        return token[:-3] + "y"
    if len(token) > 4 and token.endswith(("ches", "shes", "xes", "zzes", "sses")):
        return token[:-2]
    if len(token) > 3 and token.endswith("s") and not token.endswith(("ss", "us", "is")):
        return token[:-1]
    return token


def tag_intent_keywords(tag: str) -> frozenset[str]:
    """Conservative lexical signature used for redundancy, never displayed as a tag."""

    roots = (
        _keyword_root(token)
        for token in findall(r"[a-z0-9]+", tag.casefold())
        if len(token) > 1 and token not in _STOP_WORDS
    )
    return frozenset(_ALIASES.get(root, root) for root in roots)


def tag_coverage_keywords(tag: str) -> frozenset[str]:
    """Specific lexical coverage, without treating style synonyms as hard duplicates.

    Decade modifiers such as 70s/1970s are soft context. Other numbers, names and
    distinguishing style terms remain eligible. This does not infer semantic meaning.
    """

    tokens = (
        token
        for token in findall(r"[a-z0-9]+", tag.casefold())
        if not fullmatch(r"(?:[0-9]{2}|[0-9]{4})s", token)
    )
    return tag_intent_keywords(" ".join(tokens)) - _COVERAGE_MODIFIERS


def is_complete_tag_phrase(tag: str) -> bool:
    """Reject obvious fragments, overlength phrases, and generic-only slot filling.

    Passing this structural check does not establish whether a phrase describes the
    artwork. That remains the intelligence boundary's and seller review's responsibility.
    """

    words = findall(r"[a-z0-9]+", tag.casefold())
    meaningful = [word for word in words if len(word) > 1 and word not in _STOP_WORDS]
    return (
        0 < len(tag) <= ETSY_TAG_LENGTH_LIMIT
        and len(meaningful) >= 2
        and words[-1] not in _STOP_WORDS
        and len(tag_intent_keywords(tag)) >= 2
        and bool(tag_intent_keywords(tag) - _LOW_INFORMATION)
    )


def tags_are_redundant(first: str, second: str) -> bool:
    """Allow shared words unless the remaining difference adds no specific concept.

    For example octopus art/print differ only in a generic artwork head, whereas
    diamond ring/engagement ring introduce distinct descriptive concepts.
    """

    left, right = tag_intent_keywords(first), tag_intent_keywords(second)
    if left == right:
        return True
    return bool((left & right) - _LOW_INFORMATION) and (left ^ right) <= _LOW_INFORMATION


def redundant_tag_pairs(tags: tuple[str, ...]) -> tuple[tuple[int, int], ...]:
    """Return deterministic one-based positions of redundant complete phrases."""

    return tuple(
        (first + 1, second + 1)
        for first, second in combinations(range(len(tags)), 2)
        if tags_are_redundant(tags[first], tags[second])
    )
