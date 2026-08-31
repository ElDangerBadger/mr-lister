"""Strict Phase 6 source-artwork and immutable authority helpers.

The direct-upload boundary and every later source reader must make the same
decision about the accepted PNG.  This module deliberately has no S3, HTTP,
or persistence dependency so those adapters cannot weaken the contract.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
from io import BytesIO
from pathlib import PurePath

from PIL import Image, UnidentifiedImageError

from mr_lister.contracts import ProductProfile
from mr_lister.control.fingerprints import canonical_fingerprint
from mr_lister.control.models import (
    CONTROL_CONTRACT_VERSION,
    PHASE6_MAX_SOURCE_ARTWORK_BYTES,
    SourceArtifactRecord,
)
from mr_lister.workflow.errors import InvalidArtworkError
from mr_lister.workflow.models import ArtworkInput
from mr_lister.workflow.validation import MAX_ARTWORK_PIXELS, validate_artwork

PHASE6_MIN_SOURCE_ARTWORK_BYTES = 1
PHASE6_MAX_SOURCE_DIMENSION = 20_000
_PLACEMENT_SCALE_PRECISION = 1_000_000


class Phase6SourceArtworkError(ValueError):
    """The submitted bytes are outside the closed Phase 6 PNG envelope."""


class SourceArtifactAuthorityError(ValueError):
    """A persisted source record's fingerprint does not cover its authority."""


class SourceArtworkPlacementError(ValueError):
    """The verified source cannot use the immutable width-driven placement rule."""


@dataclass(frozen=True)
class VerifiedSourceArtwork:
    """Safe metadata derived from a fully decoded accepted source."""

    artwork: ArtworkInput
    width: int
    height: int
    alpha_minimum: int
    alpha_maximum: int


def source_artwork_placement_scale(
    *,
    placement_scale: float,
    canvas_width: int,
    canvas_height: int,
    artwork_width: int,
    artwork_height: int,
) -> float:
    """Return the calibrated width scale, reduced only when height requires it."""

    if (
        isinstance(artwork_width, bool)
        or not isinstance(artwork_width, int)
        or artwork_width <= 0
        or isinstance(artwork_height, bool)
        or not isinstance(artwork_height, int)
        or artwork_height <= 0
        or isinstance(canvas_width, bool)
        or not isinstance(canvas_width, int)
        or canvas_width <= 0
        or isinstance(canvas_height, bool)
        or not isinstance(canvas_height, int)
        or canvas_height <= 0
        or isinstance(placement_scale, bool)
        or not isinstance(placement_scale, (int, float))
        or not 0 < placement_scale <= 1
    ):
        raise SourceArtworkPlacementError("Artwork placement dimensions are invalid")

    calibrated_scale = float(placement_scale)
    height_limit_numerator = canvas_height * artwork_width
    height_limit_denominator = canvas_width * artwork_height
    height_limited_scale = height_limit_numerator / height_limit_denominator
    if calibrated_scale <= height_limited_scale:
        return calibrated_scale

    # Provider placement values are kept to six decimal places. Quantizing downward guarantees
    # that floating-point or decimal rounding can never make a height-limited design overflow.
    effective_scale_units = (
        height_limit_numerator * _PLACEMENT_SCALE_PRECISION
    ) // height_limit_denominator
    if effective_scale_units <= 0:
        # The verified Phase 6 source envelope cannot reach this branch with the checked product
        # profiles, but retaining the exact positive ratio keeps the geometry helper total for
        # any future valid canvas rather than manufacturing an invalid zero scale.
        return height_limited_scale
    return effective_scale_units / _PLACEMENT_SCALE_PRECISION


def source_artwork_placement_y(
    *,
    calibrated_square_y: float,
    placement_scale: float,
    canvas_width: int,
    canvas_height: int,
    artwork_width: int,
    artwork_height: int,
) -> float:
    """Return the vertical center for a top-aligned, aspect-preserving source."""

    if (
        isinstance(calibrated_square_y, bool)
        or not isinstance(calibrated_square_y, (int, float))
        or not 0 <= calibrated_square_y <= 1
    ):
        raise SourceArtworkPlacementError("Artwork placement dimensions are invalid")

    effective_scale = source_artwork_placement_scale(
        placement_scale=placement_scale,
        canvas_width=canvas_width,
        canvas_height=canvas_height,
        artwork_width=artwork_width,
        artwork_height=artwork_height,
    )
    if artwork_width == artwork_height and effective_scale == float(placement_scale):
        return float(calibrated_square_y)
    rendered_height = (
        effective_scale * canvas_width * artwork_height / (canvas_height * artwork_width)
    )
    if rendered_height <= 0:
        raise SourceArtworkPlacementError("Artwork placement dimensions are invalid")
    return round(min(rendered_height, 1.0) / 2, 6)


def validate_source_artwork_fit(
    *,
    profile: ProductProfile,
    artwork_width: int | None,
    artwork_height: int | None,
) -> None:
    """Validate that every profile canvas has a deterministic proportional placement."""

    if (artwork_width is None) != (artwork_height is None):
        raise SourceArtworkPlacementError("Artwork placement dimensions must be present together")
    if artwork_width is None or artwork_height is None:
        return
    for group in profile.placement_groups:
        source_artwork_placement_y(
            calibrated_square_y=group.placement.y,
            placement_scale=group.placement.scale,
            canvas_width=group.canvas_width,
            canvas_height=group.canvas_height,
            artwork_width=artwork_width,
            artwork_height=artwork_height,
        )


def verify_phase6_source_artwork(
    *,
    filename: str,
    content_type: str,
    content: bytes,
    expected_sha256: str | None = None,
    expected_size_bytes: int | None = None,
) -> VerifiedSourceArtwork:
    """Fully verify one checksum-bound PNG accepted by Phase 6.

    ``expected_*`` values are authoritative declarations from an upload
    intent.  They are optional only so trusted readers can reuse the decoder;
    intake completion must supply both.
    """

    if not isinstance(content, bytes):
        raise Phase6SourceArtworkError("Source artwork bytes are invalid")
    size_bytes = len(content)
    if not PHASE6_MIN_SOURCE_ARTWORK_BYTES <= size_bytes <= PHASE6_MAX_SOURCE_ARTWORK_BYTES:
        raise Phase6SourceArtworkError("Source artwork size is outside the Phase 6 limit")
    if expected_size_bytes is not None and (
        isinstance(expected_size_bytes, bool)
        or not isinstance(expected_size_bytes, int)
        or expected_size_bytes != size_bytes
    ):
        raise Phase6SourceArtworkError("Source artwork size does not match its upload intent")

    content_sha256 = sha256(content).hexdigest()
    if expected_sha256 is not None and (
        not isinstance(expected_sha256, str)
        or len(expected_sha256) != 64
        or any(character not in "0123456789abcdef" for character in expected_sha256)
        or expected_sha256 != content_sha256
    ):
        raise Phase6SourceArtworkError("Source artwork checksum does not match its upload intent")

    if (
        not isinstance(filename, str)
        or not filename
        or filename != filename.strip()
        or any(ord(character) < 32 or ord(character) == 127 for character in filename)
        or "/" in filename
        or "\\" in filename
        or PurePath(filename).name != filename
        or PurePath(filename).suffix.casefold() != ".png"
    ):
        raise Phase6SourceArtworkError("Source artwork requires a PNG basename")
    if content_type != "image/png":
        raise Phase6SourceArtworkError("Source artwork content type must be image/png")

    try:
        artwork = validate_artwork(
            filename=filename,
            content_type=content_type,
            content=content,
        )
    except InvalidArtworkError:
        raise Phase6SourceArtworkError("Source artwork PNG is invalid") from None

    try:
        with Image.open(BytesIO(content)) as image:
            image.load()
            if image.format != "PNG":
                raise Phase6SourceArtworkError("Source artwork PNG is invalid")
            width, height = image.size
            if (
                width < 1
                or height < 1
                or width > PHASE6_MAX_SOURCE_DIMENSION
                or height > PHASE6_MAX_SOURCE_DIMENSION
                or width * height > MAX_ARTWORK_PIXELS
            ):
                raise Phase6SourceArtworkError(
                    "Source artwork must be a PNG within the Phase 6 dimensions"
                )
            alpha_minimum, alpha_maximum = image.convert("RGBA").getchannel("A").getextrema()
    except Phase6SourceArtworkError:
        raise
    except (OSError, SyntaxError, UnidentifiedImageError, ValueError):
        raise Phase6SourceArtworkError("Source artwork PNG is invalid") from None

    if alpha_maximum == 0:
        raise Phase6SourceArtworkError("Source artwork must contain visible pixels")
    if (
        artwork.content_sha256 != content_sha256
        or artwork.size_bytes != size_bytes
        or artwork.content_type != "image/png"
    ):
        raise Phase6SourceArtworkError("Source artwork integrity verification failed")
    return VerifiedSourceArtwork(
        artwork=artwork,
        width=width,
        height=height,
        alpha_minimum=alpha_minimum,
        alpha_maximum=alpha_maximum,
    )


def source_artifact_fingerprint(
    *,
    job_id: str,
    owner_id: str,
    bucket: str,
    object_key: str,
    version_id: str,
    content_sha256: str,
    size_bytes: int,
    media_type: str,
    product_profile_id: str,
    product_profile_version: int,
    product_profile_fingerprint: str,
    created_at: datetime,
    contract_version: str = CONTROL_CONTRACT_VERSION,
) -> str:
    """Hash every immutable field that grants authority over a source version."""

    if created_at.utcoffset() is None:
        raise SourceArtifactAuthorityError("Pinned source artifact timestamp is invalid")
    if version_id == "null":
        raise SourceArtifactAuthorityError("Pinned source artifact version is invalid")
    material = {
        "contract_version": contract_version,
        "job_id": job_id,
        "owner_id": owner_id,
        "bucket": bucket,
        "object_key": object_key,
        "version_id": version_id,
        "content_sha256": content_sha256,
        "size_bytes": size_bytes,
        "media_type": media_type,
        "product_profile_id": product_profile_id,
        "product_profile_version": product_profile_version,
        "product_profile_fingerprint": product_profile_fingerprint,
        "created_at": created_at.isoformat(),
    }
    return canonical_fingerprint(material)


def source_artifact_authority_fingerprint(source: SourceArtifactRecord) -> str:
    """Recompute the fingerprint of one parsed source authority record."""

    return source_artifact_fingerprint(
        contract_version=source.contract_version,
        job_id=source.job_id,
        owner_id=source.owner_id,
        bucket=source.bucket,
        object_key=source.object_key,
        version_id=source.version_id,
        content_sha256=source.content_sha256,
        size_bytes=source.size_bytes,
        media_type=source.media_type,
        product_profile_id=source.product_profile_id,
        product_profile_version=source.product_profile_version,
        product_profile_fingerprint=source.product_profile_fingerprint,
        created_at=source.created_at,
    )


def validate_source_artifact_authority(source: SourceArtifactRecord) -> SourceArtifactRecord:
    """Return ``source`` only when its fingerprint covers every immutable field."""

    if source.created_at.utcoffset() is None:
        raise SourceArtifactAuthorityError("Pinned source artifact timestamp is invalid")
    if source.version_id == "null":
        raise SourceArtifactAuthorityError("Pinned source artifact version is invalid")
    if source.fingerprint != source_artifact_authority_fingerprint(source):
        raise SourceArtifactAuthorityError("Pinned source artifact authority is invalid")
    return source


__all__ = [
    "PHASE6_MAX_SOURCE_DIMENSION",
    "PHASE6_MIN_SOURCE_ARTWORK_BYTES",
    "Phase6SourceArtworkError",
    "SourceArtworkPlacementError",
    "SourceArtifactAuthorityError",
    "VerifiedSourceArtwork",
    "source_artifact_authority_fingerprint",
    "source_artifact_fingerprint",
    "source_artwork_placement_scale",
    "source_artwork_placement_y",
    "validate_source_artwork_fit",
    "validate_source_artifact_authority",
    "verify_phase6_source_artwork",
]
