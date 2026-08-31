from __future__ import annotations

from datetime import UTC, datetime
from hashlib import sha256
from io import BytesIO

import pytest
from PIL import Image
from pydantic import ValidationError

from mr_lister.control.fingerprints import canonical_fingerprint
from mr_lister.control.models import PHASE6_MAX_SOURCE_ARTWORK_BYTES, SourceArtifactRecord
from mr_lister.control.source_artwork import (
    Phase6SourceArtworkError,
    SourceArtifactAuthorityError,
    source_artifact_authority_fingerprint,
    source_artifact_fingerprint,
    source_artwork_placement_scale,
    source_artwork_placement_y,
    validate_source_artifact_authority,
    verify_phase6_source_artwork,
)

NOW = datetime(2026, 8, 22, 9, 0, tzinfo=UTC)
OWNER = "a" * 64
JOB_ID = "job_phase64_source"
PROFILE_FINGERPRINT = "b" * 64


def _png(
    *,
    size: tuple[int, int] = (2, 2),
    alpha: tuple[int, ...] = (0, 64, 192, 255),
) -> bytes:
    assert len(alpha) == size[0] * size[1]
    image = Image.new("RGBA", size, (24, 72, 108, 255))
    image.putdata([(24, 72, 108, value) for value in alpha])
    output = BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def _source(**updates: object) -> SourceArtifactRecord:
    content = _png()
    material: dict[str, object] = {
        "job_id": JOB_ID,
        "owner_id": OWNER,
        "bucket": "mr-lister-phase6-artifacts-dev",
        "object_key": f"private/owners/{OWNER}/jobs/{JOB_ID}/source/source.png",
        "version_id": "s3-version-1",
        "content_sha256": sha256(content).hexdigest(),
        "size_bytes": len(content),
        "media_type": "image/png",
        "product_profile_id": "gildan_64000_swiftpod",
        "product_profile_version": 2,
        "product_profile_fingerprint": PROFILE_FINGERPRINT,
        "created_at": NOW,
    }
    material.update(updates)
    fingerprint = source_artifact_fingerprint(**material)  # type: ignore[arg-type]
    return SourceArtifactRecord(fingerprint=fingerprint, **material)  # type: ignore[arg-type]


def test_phase6_verifier_accepts_one_fully_decoded_checksum_bound_png() -> None:
    content = _png()

    verified = verify_phase6_source_artwork(
        filename="seller-art.png",
        content_type="image/png",
        content=content,
        expected_sha256=sha256(content).hexdigest(),
        expected_size_bytes=len(content),
    )

    assert verified.artwork.content_sha256 == sha256(content).hexdigest()
    assert verified.artwork.size_bytes == len(content)
    assert (verified.width, verified.height) == (2, 2)
    assert (verified.alpha_minimum, verified.alpha_maximum) == (0, 255)


def test_phase6_verifier_preserves_rectangular_source_dimensions() -> None:
    content = _png(size=(3, 2), alpha=(0, 64, 128, 192, 224, 255))

    verified = verify_phase6_source_artwork(
        filename="seller-art.png",
        content_type="image/png",
        content=content,
        expected_sha256=sha256(content).hexdigest(),
        expected_size_bytes=len(content),
    )

    assert (verified.width, verified.height) == (3, 2)
    assert (verified.alpha_minimum, verified.alpha_maximum) == (0, 255)


@pytest.mark.parametrize(
    ("filename", "content_type"),
    (
        ("art.jpg", "image/png"),
        ("../art.png", "image/png"),
        (r"folder\art.png", "image/png"),
        (" art.png", "image/png"),
        ("art\n.png", "image/png"),
        ("art.png", "image/PNG"),
        ("art.png", "application/octet-stream"),
    ),
)
def test_phase6_verifier_rejects_noncanonical_filename_or_type(
    filename: str,
    content_type: str,
) -> None:
    with pytest.raises(Phase6SourceArtworkError):
        verify_phase6_source_artwork(
            filename=filename,
            content_type=content_type,
            content=_png(),
        )


@pytest.mark.parametrize(
    "content",
    (
        b"",
        b"not-a-png",
        _png()[:-8],
        _png() + b"x" * PHASE6_MAX_SOURCE_ARTWORK_BYTES,
    ),
)
def test_phase6_verifier_rejects_empty_corrupt_truncated_or_oversized_bytes(
    content: bytes,
) -> None:
    with pytest.raises(Phase6SourceArtworkError):
        verify_phase6_source_artwork(
            filename="art.png",
            content_type="image/png",
            content=content,
        )


def test_phase6_verifier_accepts_fully_opaque_artwork() -> None:
    content = _png(alpha=(255, 255, 255, 255))

    verified = verify_phase6_source_artwork(
        filename="art.png",
        content_type="image/png",
        content=content,
    )

    assert (verified.alpha_minimum, verified.alpha_maximum) == (255, 255)


def test_phase6_verifier_rejects_fully_transparent_artwork() -> None:
    with pytest.raises(Phase6SourceArtworkError):
        verify_phase6_source_artwork(
            filename="art.png",
            content_type="image/png",
            content=_png(alpha=(0, 0, 0, 0)),
        )


@pytest.mark.parametrize(
    ("artwork_size", "expected_scale", "expected_y"),
    (
        ((2400, 2400), 0.65, 0.25),
        ((2000, 800), 0.65, 0.100008),
        ((1000, 2100), 0.619, 0.5),
    ),
)
def test_phase6_placement_preserves_legacy_width_or_scales_down_for_height(
    artwork_size: tuple[int, int],
    expected_scale: float,
    expected_y: float,
) -> None:
    width, height = artwork_size
    scale = source_artwork_placement_scale(
        placement_scale=0.65,
        canvas_width=3021,
        canvas_height=3927,
        artwork_width=width,
        artwork_height=height,
    )
    y = source_artwork_placement_y(
        calibrated_square_y=0.25,
        placement_scale=0.65,
        canvas_width=3021,
        canvas_height=3927,
        artwork_width=width,
        artwork_height=height,
    )

    assert scale == expected_scale
    assert y == expected_y
    rendered_height = scale * 3021 * height / (3927 * width)
    assert rendered_height <= 1
    assert y == round(rendered_height / 2, 6) or artwork_size == (2400, 2400)


@pytest.mark.parametrize(
    ("expected_sha256", "expected_size_delta"),
    (("0" * 64, 0), (None, 1)),
)
def test_phase6_verifier_rejects_upload_intent_integrity_mismatch(
    expected_sha256: str | None,
    expected_size_delta: int,
) -> None:
    content = _png()
    with pytest.raises(Phase6SourceArtworkError):
        verify_phase6_source_artwork(
            filename="art.png",
            content_type="image/png",
            content=content,
            expected_sha256=expected_sha256 or sha256(content).hexdigest(),
            expected_size_bytes=len(content) + expected_size_delta,
        )


def test_source_artifact_fingerprint_covers_every_immutable_authority_field() -> None:
    source = _source()

    assert source_artifact_authority_fingerprint(source) == source.fingerprint
    assert validate_source_artifact_authority(source) is source

    for field, changed in (
        ("contract_version", "1.0.0"),
        ("job_id", "job_phase64_other"),
        ("owner_id", "c" * 64),
        ("bucket", "other-private-bucket"),
        ("object_key", "private/owners/forged/jobs/forged/source/source.png"),
        ("version_id", "s3-version-2"),
        ("content_sha256", "c" * 64),
        ("size_bytes", source.size_bytes + 1),
        ("media_type", "application/octet-stream"),
        ("product_profile_id", "other_profile"),
        ("product_profile_version", 3),
        ("product_profile_fingerprint", "d" * 64),
        ("created_at", NOW.replace(minute=1)),
    ):
        tampered = source.model_copy(update={field: changed})
        with pytest.raises(SourceArtifactAuthorityError):
            validate_source_artifact_authority(tampered)


def test_source_artifact_schema_rejects_persisted_geometry() -> None:
    source = _source()
    material = source.model_dump(mode="python", exclude={"fingerprint"})

    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        SourceArtifactRecord(
            fingerprint=source.fingerprint,
            width=3,
            height=2,
            **material,
        )


def test_source_payload_and_fingerprint_remain_agentcore_v1_compatible() -> None:
    source = _source()
    serialized = source.model_dump(mode="json")

    assert "width" not in serialized
    assert "height" not in serialized
    assert source.fingerprint == canonical_fingerprint(
        {
            "contract_version": source.contract_version,
            "job_id": source.job_id,
            "owner_id": source.owner_id,
            "bucket": source.bucket,
            "object_key": source.object_key,
            "version_id": source.version_id,
            "content_sha256": source.content_sha256,
            "size_bytes": source.size_bytes,
            "media_type": source.media_type,
            "product_profile_id": source.product_profile_id,
            "product_profile_version": source.product_profile_version,
            "product_profile_fingerprint": source.product_profile_fingerprint,
            "created_at": source.created_at.isoformat(),
        }
    )


def test_source_artifact_authority_rejects_naive_creation_time() -> None:
    source = _source()
    naive = source.model_copy(update={"created_at": NOW.replace(tzinfo=None)})

    with pytest.raises(SourceArtifactAuthorityError, match="timestamp"):
        validate_source_artifact_authority(naive)


def test_source_artifact_authority_rejects_the_mutable_s3_null_version() -> None:
    source = _source()
    null_version = source.model_copy(update={"version_id": "null"})

    with pytest.raises(SourceArtifactAuthorityError, match="version"):
        validate_source_artifact_authority(null_version)
