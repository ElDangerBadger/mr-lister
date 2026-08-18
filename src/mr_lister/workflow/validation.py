"""Deterministic validation at the artwork and listing boundaries."""

from hashlib import sha256
from pathlib import PurePath
from zlib import crc32

from mr_lister.contracts import ListingIntelligence, ValidationIssue, ValidationResult
from mr_lister.workflow.errors import InvalidArtworkError
from mr_lister.workflow.models import ArtworkInput

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
MAX_ARTWORK_BYTES = 25 * 1024 * 1024


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
    if len(content) > MAX_ARTWORK_BYTES:
        raise InvalidArtworkError("Artwork exceeds the 25 MiB Phase 1 limit")

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
    return ValidationResult(passed=True, issues=tuple(issues))
