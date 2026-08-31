from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from io import BytesIO

import pytest
from PIL import Image

from mr_lister.agent.phase6_producer import (
    PinnedSourcePreparedReviewProducer,
    PreparedReviewProducerError,
)
from mr_lister.contracts import (
    ArtworkAnalysis,
    ListingIntelligence,
    Placement,
    ProductProfile,
)
from mr_lister.control.fingerprints import canonical_fingerprint
from mr_lister.control.models import SourceArtifactRecord
from mr_lister.control.source_artwork import source_artifact_fingerprint


def _png(*, size: tuple[int, int] = (2, 2), alpha: tuple[int, ...]) -> bytes:
    assert len(alpha) == size[0] * size[1]
    image = Image.new("RGBA", size, (24, 72, 108, 255))
    image.putdata([(24, 72, 108, value) for value in alpha])
    output = BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


VALID_PNG = _png(alpha=(0, 96, 192, 255))
OWNER_ID = "a" * 64
JOB_ID = "job-phase6-producer"
WORK_ID = "work-phase6-producer"
VERSION_ID = "exact-s3-version"


@dataclass(frozen=True)
class ExactProfile:
    profile: ProductProfile
    fingerprint: str


class RecordingStore:
    def __init__(self, source: SourceArtifactRecord) -> None:
        self.source = source
        self.requests: list[str] = []

    def get_source_artifact(self, job_id: str) -> SourceArtifactRecord:
        self.requests.append(job_id)
        return self.source


class RecordingBody:
    def __init__(self, content: bytes) -> None:
        self._body = BytesIO(content)
        self.read_sizes: list[int] = []
        self.close_calls = 0

    def read(self, size: int = -1) -> bytes:
        self.read_sizes.append(size)
        return self._body.read(size)

    def close(self) -> None:
        self.close_calls += 1
        self._body.close()


class RecordingS3:
    def __init__(
        self,
        content: bytes,
        *,
        version_id: object = VERSION_ID,
        content_length: object | None = None,
        content_type: object | None = "image/png",
    ) -> None:
        self.body = RecordingBody(content)
        self.version_id = version_id
        self.content_length = len(content) if content_length is None else content_length
        self.content_type = content_type
        self.requests: list[dict[str, object]] = []

    def get_object(self, **request: object) -> dict[str, object]:
        self.requests.append(request)
        response: dict[str, object] = {
            "Body": self.body,
            "VersionId": self.version_id,
            "ContentLength": self.content_length,
        }
        if self.content_type is not None:
            response["ContentType"] = self.content_type
        return response


class RecordingProfiles:
    def __init__(self, exact: ExactProfile) -> None:
        self.exact = exact
        self.requests: list[tuple[str, int]] = []

    def get_exact(self, *, profile_id: str, profile_version: int) -> ExactProfile:
        self.requests.append((profile_id, profile_version))
        return self.exact


class RecordingIntelligence:
    def __init__(self) -> None:
        self.analysis = ArtworkAnalysis(
            subject="A geometric badger",
            styles=("geometric",),
            confidence=0.94,
        )
        self.listing = ListingIntelligence(
            title="Geometric Badger Graphic Tee",
            description="A prepared listing that still requires seller approval.",
            tags=tuple(f"unique tag {index}" for index in range(1, 14)),
            audience=("wildlife art fans",),
            title_rationale="Names the subject and product.",
            tag_rationale="Covers distinct search intents.",
        )
        self.inspect_calls: list[tuple[object, bytes]] = []
        self.draft_calls: list[tuple[object, bytes, ArtworkAnalysis]] = []

    def inspect_artwork(self, artwork: object, content: bytes) -> ArtworkAnalysis:
        self.inspect_calls.append((artwork, content))
        return self.analysis

    def draft_listing(
        self,
        artwork: object,
        content: bytes,
        analysis: ArtworkAnalysis,
    ) -> ListingIntelligence:
        self.draft_calls.append((artwork, content, analysis))
        return self.listing


def _profile(*, version: int = 4) -> ProductProfile:
    return ProductProfile(
        profile_id="gildan-5000",
        profile_version=version,
        blueprint_id=12,
        print_provider_id=39,
        variant_ids=(101, 102),
        retail_price_cents=2999,
        placement=Placement(x=0.5, y=0.25, scale=0.65),
    )


def _source(
    content: bytes,
    profile: ProductProfile,
    *,
    content_sha256: str | None = None,
    size_bytes: int | None = None,
    profile_fingerprint: str | None = None,
    width: int | None = None,
    height: int | None = None,
) -> SourceArtifactRecord:
    material = {
        "job_id": JOB_ID,
        "owner_id": OWNER_ID,
        "bucket": "mr-lister-private-sources",
        "object_key": f"private/owners/{OWNER_ID}/jobs/{JOB_ID}/source/source.png",
        "version_id": VERSION_ID,
        "content_sha256": content_sha256 or sha256(content).hexdigest(),
        "size_bytes": len(content) if size_bytes is None else size_bytes,
        "media_type": "image/png",
        "product_profile_id": profile.profile_id,
        "product_profile_version": profile.profile_version,
        "product_profile_fingerprint": (profile_fingerprint or canonical_fingerprint(profile)),
        "created_at": datetime(2026, 8, 21, 9, 0, tzinfo=UTC),
    }
    if width is not None or height is not None:
        material.update({"width": width, "height": height})
    return SourceArtifactRecord(
        **material,
        fingerprint=source_artifact_fingerprint(**material),
    )


def _producer(
    *,
    source: SourceArtifactRecord,
    s3: RecordingS3,
    exact: ExactProfile,
    intelligence: RecordingIntelligence | None = None,
) -> tuple[
    PinnedSourcePreparedReviewProducer,
    RecordingStore,
    RecordingProfiles,
    RecordingIntelligence,
]:
    store = RecordingStore(source)
    profiles = RecordingProfiles(exact)
    intelligence = intelligence or RecordingIntelligence()
    return (
        PinnedSourcePreparedReviewProducer(
            store=store,
            s3=s3,
            profiles=profiles,
            intelligence=intelligence,
        ),
        store,
        profiles,
        intelligence,
    )


def test_exact_versioned_source_and_profile_produce_one_inspection_and_draft() -> None:
    profile = _profile()
    source = _source(VALID_PNG, profile)
    s3 = RecordingS3(VALID_PNG)
    producer, store, profiles, intelligence = _producer(
        source=source,
        s3=s3,
        exact=ExactProfile(profile=profile, fingerprint=canonical_fingerprint(profile)),
    )

    observation = producer.prepare_review(JOB_ID, WORK_ID)

    assert store.requests == [JOB_ID]
    assert s3.requests == [
        {
            "Bucket": source.bucket,
            "Key": source.object_key,
            "VersionId": VERSION_ID,
        }
    ]
    assert s3.body.read_sizes[0] == len(VALID_PNG) + 1
    assert s3.body.close_calls == 1
    assert profiles.requests == [(profile.profile_id, profile.profile_version)]
    assert len(intelligence.inspect_calls) == 1
    assert len(intelligence.draft_calls) == 1
    artwork, inspected_content = intelligence.inspect_calls[0]
    drafted_artwork, drafted_content, drafted_analysis = intelligence.draft_calls[0]
    assert inspected_content == VALID_PNG
    assert drafted_artwork is artwork
    assert drafted_content == VALID_PNG
    assert drafted_analysis is intelligence.analysis
    assert observation.source_artifact_fingerprint == source.fingerprint
    assert observation.product_profile_fingerprint == canonical_fingerprint(profile)
    assert observation.artwork_analysis == intelligence.analysis
    assert observation.listing == intelligence.listing


def test_tampered_source_authority_is_rejected_before_s3_read() -> None:
    profile = _profile()
    source = _source(VALID_PNG, profile).model_copy(update={"fingerprint": "0" * 64})
    s3 = RecordingS3(VALID_PNG)
    producer, _, _, intelligence = _producer(
        source=source,
        s3=s3,
        exact=ExactProfile(profile=profile, fingerprint=canonical_fingerprint(profile)),
    )

    with pytest.raises(PreparedReviewProducerError, match="authority is unavailable"):
        producer.prepare_review(JOB_ID, WORK_ID)

    assert s3.requests == []
    assert intelligence.inspect_calls == []
    assert intelligence.draft_calls == []


@pytest.mark.parametrize("returned_version", [None, "different-version"])
def test_missing_or_different_returned_version_is_rejected(returned_version: object) -> None:
    profile = _profile()
    source = _source(VALID_PNG, profile)
    s3 = RecordingS3(VALID_PNG, version_id=returned_version)
    producer, _, _, intelligence = _producer(
        source=source,
        s3=s3,
        exact=ExactProfile(profile=profile, fingerprint=canonical_fingerprint(profile)),
    )

    with pytest.raises(PreparedReviewProducerError, match="version is invalid"):
        producer.prepare_review(JOB_ID, WORK_ID)

    assert intelligence.inspect_calls == []
    assert intelligence.draft_calls == []


def test_checksum_mismatch_is_rejected_before_intelligence() -> None:
    profile = _profile()
    source = _source(VALID_PNG, profile, content_sha256="0" * 64)
    s3 = RecordingS3(VALID_PNG)
    producer, _, _, intelligence = _producer(
        source=source,
        s3=s3,
        exact=ExactProfile(profile=profile, fingerprint=canonical_fingerprint(profile)),
    )

    with pytest.raises(PreparedReviewProducerError, match="integrity check failed"):
        producer.prepare_review(JOB_ID, WORK_ID)

    assert intelligence.inspect_calls == []
    assert intelligence.draft_calls == []


@pytest.mark.parametrize(
    ("source_size", "response_size"),
    [
        (len(VALID_PNG) - 1, len(VALID_PNG) - 1),
        (len(VALID_PNG), len(VALID_PNG) + 1),
    ],
)
def test_body_or_header_size_mismatch_is_rejected(
    source_size: int,
    response_size: int,
) -> None:
    profile = _profile()
    source = _source(VALID_PNG, profile, size_bytes=source_size)
    s3 = RecordingS3(VALID_PNG, content_length=response_size)
    producer, _, _, intelligence = _producer(
        source=source,
        s3=s3,
        exact=ExactProfile(profile=profile, fingerprint=canonical_fingerprint(profile)),
    )

    with pytest.raises(PreparedReviewProducerError, match="size is invalid"):
        producer.prepare_review(JOB_ID, WORK_ID)

    assert intelligence.inspect_calls == []
    assert intelligence.draft_calls == []


@pytest.mark.parametrize("corrupt", [b"not-a-png", b"\x89PNG\r\n\x1a\n" + b"x" * 40])
def test_corrupt_png_is_rejected_after_size_and_checksum_pass(corrupt: bytes) -> None:
    profile = _profile()
    source = _source(corrupt, profile)
    s3 = RecordingS3(corrupt)
    producer, _, _, intelligence = _producer(
        source=source,
        s3=s3,
        exact=ExactProfile(profile=profile, fingerprint=canonical_fingerprint(profile)),
    )

    with pytest.raises(PreparedReviewProducerError, match="PNG is invalid"):
        producer.prepare_review(JOB_ID, WORK_ID)

    assert intelligence.inspect_calls == []
    assert intelligence.draft_calls == []


def test_fully_transparent_png_is_rejected() -> None:
    output = BytesIO()
    Image.new("RGBA", (2, 2), (0, 0, 0, 0)).save(output, format="PNG")
    transparent = output.getvalue()
    profile = _profile()
    source = _source(transparent, profile)
    s3 = RecordingS3(transparent)
    producer, _, _, intelligence = _producer(
        source=source,
        s3=s3,
        exact=ExactProfile(profile=profile, fingerprint=canonical_fingerprint(profile)),
    )

    with pytest.raises(PreparedReviewProducerError, match="PNG is invalid"):
        producer.prepare_review(JOB_ID, WORK_ID)

    assert intelligence.inspect_calls == []
    assert intelligence.draft_calls == []


def test_phase6_opaque_envelope_is_revalidated_before_intelligence() -> None:
    invalid_png = _png(alpha=(255, 255, 255, 255))
    profile = _profile()
    source = _source(invalid_png, profile)
    s3 = RecordingS3(invalid_png)
    producer, _, _, intelligence = _producer(
        source=source,
        s3=s3,
        exact=ExactProfile(profile=profile, fingerprint=canonical_fingerprint(profile)),
    )

    with pytest.raises(PreparedReviewProducerError, match="PNG is invalid"):
        producer.prepare_review(JOB_ID, WORK_ID)

    assert intelligence.inspect_calls == []
    assert intelligence.draft_calls == []


def test_rectangular_source_geometry_is_revalidated_and_accepted() -> None:
    rectangular = _png(size=(2, 3), alpha=(0, 64, 128, 192, 224, 255))
    profile = _profile()
    source = _source(rectangular, profile, width=2, height=3)
    producer, _, _, intelligence = _producer(
        source=source,
        s3=RecordingS3(rectangular),
        exact=ExactProfile(profile=profile, fingerprint=canonical_fingerprint(profile)),
    )

    observation = producer.prepare_review(JOB_ID, WORK_ID)

    assert observation.source_artifact_fingerprint == source.fingerprint
    assert len(intelligence.inspect_calls) == 1
    assert len(intelligence.draft_calls) == 1


def test_persisted_source_geometry_must_match_the_exact_versioned_bytes() -> None:
    rectangular = _png(size=(2, 3), alpha=(0, 64, 128, 192, 224, 255))
    profile = _profile()
    source = _source(rectangular, profile, width=3, height=2)
    producer, _, _, intelligence = _producer(
        source=source,
        s3=RecordingS3(rectangular),
        exact=ExactProfile(profile=profile, fingerprint=canonical_fingerprint(profile)),
    )

    with pytest.raises(PreparedReviewProducerError, match="integrity check failed"):
        producer.prepare_review(JOB_ID, WORK_ID)

    assert intelligence.inspect_calls == []
    assert intelligence.draft_calls == []


@pytest.mark.parametrize("drift", ["version", "reported-fingerprint", "source-fingerprint"])
def test_profile_version_or_fingerprint_drift_is_rejected(drift: str) -> None:
    source_profile = _profile()
    exact_profile = (
        _profile(version=source_profile.profile_version + 1)
        if drift == "version"
        else source_profile
    )
    reported = "0" * 64 if drift == "reported-fingerprint" else canonical_fingerprint(exact_profile)
    source = _source(
        VALID_PNG,
        source_profile,
        profile_fingerprint=(
            "1" * 64 if drift == "source-fingerprint" else canonical_fingerprint(source_profile)
        ),
    )
    s3 = RecordingS3(VALID_PNG)
    producer, _, profiles, intelligence = _producer(
        source=source,
        s3=s3,
        exact=ExactProfile(profile=exact_profile, fingerprint=reported),
    )

    with pytest.raises(PreparedReviewProducerError, match="has drifted"):
        producer.prepare_review(JOB_ID, WORK_ID)

    assert profiles.requests == [(source_profile.profile_id, source_profile.profile_version)]
    assert intelligence.inspect_calls == []
    assert intelligence.draft_calls == []
