"""Deterministic validation at the artwork and listing boundaries."""

from hashlib import sha256
from io import BytesIO
from pathlib import PurePath
from re import findall
from zlib import crc32

from PIL import Image, UnidentifiedImageError

from mr_lister.contracts import ListingIntelligence, ValidationIssue, ValidationResult
from mr_lister.workflow.errors import InvalidArtworkError
from mr_lister.workflow.models import ArtworkInput

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
MAX_ARTWORK_BYTES = 25 * 1024 * 1024
MAX_ARTWORK_PIXELS = 100_000_000
_TAG_STOP_WORDS = frozenset(
    {"a", "an", "and", "for", "from", "in", "of", "on", "the", "to", "with"}
)


def validate_artwork(*, filename: str, content_type: str, content: bytes) -> ArtworkInput:
    if not filename or PurePath(filename).suffix.casefold() != ".png":
        raise InvalidArtworkError("Artwork must have a .png filename")
    if content_type != "image/png":
        raise InvalidArtworkError("Artwork content type must be image/png")
    if not content.startswith(PNG_SIGNATURE):
        raise InvalidArtworkError("Artwork does not contain a valid PNG signature")
    if len(content) < 33:
        raise InvalidArtworkError("Artwork is missing the required PNG header chunk")
    if int.from_bytes(content[8:12], "big") != 13 or content[12:16] != b"IHDR":
        raise InvalidArtworkError("Artwork has an invalid PNG IHDR chunk")
    expected_crc = int.from_bytes(content[29:33], "big")
    if crc32(content[12:29]) != expected_crc:
        raise InvalidArtworkError("Artwork has a corrupt PNG IHDR checksum")
    width = int.from_bytes(content[16:20], "big")
    height = int.from_bytes(content[20:24], "big")
    if width <= 0 or height <= 0 or width > 20_000 or height > 20_000:
        raise InvalidArtworkError("Artwork has invalid PNG dimensions")
    if width * height > MAX_ARTWORK_PIXELS:
        raise InvalidArtworkError("Artwork exceeds the safe decoded-pixel limit")
    if len(content) > MAX_ARTWORK_BYTES:
        raise InvalidArtworkError("Artwork exceeds the 25 MiB Phase 1 limit")
    try:
        with Image.open(BytesIO(content)) as image:
            if image.format != "PNG" or image.size != (width, height):
                raise InvalidArtworkError("Artwork PNG metadata is inconsistent")
            image.verify()
        with Image.open(BytesIO(content)) as image:
            alpha_extrema = image.convert("RGBA").getchannel("A").getextrema()
            if alpha_extrema[1] == 0:
                raise InvalidArtworkError("Artwork is fully transparent and has no visible content")
    except InvalidArtworkError:
        raise
    except (OSError, SyntaxError, UnidentifiedImageError) as error:
        raise InvalidArtworkError("Artwork contains corrupt or incomplete PNG data") from error

    return ArtworkInput(
        filename=PurePath(filename).name,
        content_type="image/png",
        content_sha256=sha256(content).hexdigest(),
        size_bytes=len(content),
    )


def validate_listing(listing: ListingIntelligence) -> ValidationResult:
    issues: list[ValidationIssue] = []
    title_words = listing.title.casefold().split()
    if len(title_words) != len(set(title_words)):
        issues.append(
            ValidationIssue(
                code="TITLE_REPETITION",
                field="title",
                message="Title repeats one or more words.",
                severity="warning",
            )
        )
    repeated_keywords = find_repeated_tag_keywords(listing.tags)
    if repeated_keywords:
        issues.append(
            ValidationIssue(
                code="TAG_KEYWORD_REPETITION",
                field="tags",
                message=(
                    "Tags repeat searchable keywords and are not ready for publication: "
                    + ", ".join(repeated_keywords)
                    + "."
                ),
                severity="error",
            )
        )
    return ValidationResult(
        passed=not any(issue.severity.value == "error" for issue in issues),
        issues=tuple(issues),
    )


def find_repeated_tag_keywords(tags: tuple[str, ...]) -> tuple[str, ...]:
    """Return normalized searchable words used in more than one Etsy tag."""

    return tuple(find_repeated_tag_keyword_locations(tags))


def find_repeated_tag_keyword_locations(
    tags: tuple[str, ...],
) -> dict[str, tuple[int, ...]]:
    """Map repeated normalized words to their one-based tag positions."""

    locations: dict[str, list[int]] = {}
    for position, tag in enumerate(tags, start=1):
        for keyword in normalized_tag_keywords(tag):
            locations.setdefault(keyword, []).append(position)
    return {
        keyword: tuple(positions)
        for keyword, positions in sorted(locations.items())
        if len(positions) > 1
    }


def tag_keyword_reuse_count(tags: tuple[str, ...]) -> int:
    """Count repeated keyword occurrences beyond their first tag-level use."""

    counts: dict[str, int] = {}
    for tag in tags:
        for keyword in normalized_tag_keywords(tag):
            counts[keyword] = counts.get(keyword, 0) + 1
    return sum(count - 1 for count in counts.values() if count > 1)


def normalized_tag_keywords(tag: str) -> frozenset[str]:
    """Return searchable tag keywords using Etsy-boundary normalization rules."""

    return frozenset(
        keyword
        for token in findall(r"[a-z0-9]+", tag.casefold())
        if len(keyword := _keyword_root(token)) > 1 and keyword not in _TAG_STOP_WORDS
    )


def _keyword_root(token: str) -> str:
    if len(token) > 4 and token.endswith("ies"):
        return token[:-3] + "y"
    if len(token) > 4 and token.endswith("es") and not token.endswith("ses"):
        return token[:-2]
    if len(token) > 3 and token.endswith("s") and not token.endswith("ss"):
        return token[:-1]
    return token
