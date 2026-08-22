"""Pinned-source intelligence producer for the Phase 6 preparation agent.

This adapter can read one immutable source object and call the intelligence
boundary.  It has no lifecycle, credential, marketplace, or publication
surface; the application-owned worker commands persist every resulting
checkpoint.
"""

from __future__ import annotations

from collections.abc import Mapping
from hashlib import sha256
from io import BytesIO
from typing import Protocol, cast

from PIL import Image, UnidentifiedImageError

from mr_lister.agent.phase6 import PreparedReviewObservation
from mr_lister.contracts import ArtworkAnalysis, ListingIntelligence, ProductProfile
from mr_lister.control.fingerprints import canonical_fingerprint
from mr_lister.control.models import SourceArtifactRecord
from mr_lister.workflow.errors import InvalidArtworkError
from mr_lister.workflow.models import ArtworkInput
from mr_lister.workflow.ports import IntelligencePort
from mr_lister.workflow.validation import validate_artwork


class PreparedReviewProducerError(Exception):
    """Safe failure raised when pinned preparation authority cannot be proven."""

    code = "PREPARED_REVIEW_PRODUCTION_FAILED"


class SourceArtifactAuthority(Protocol):
    """Read-only access to the immutable source record created at intake."""

    def get_source_artifact(self, job_id: str) -> SourceArtifactRecord: ...


class VersionedObjectClient(Protocol):
    """The sole S3 operation available to this producer."""

    def get_object(
        self,
        *,
        Bucket: str,
        Key: str,
        VersionId: str,
    ) -> Mapping[str, object]: ...


class VersionedProductProfile(Protocol):
    """An exact profile snapshot returned by application-owned configuration."""

    profile: ProductProfile
    fingerprint: str


class ProductProfileAuthority(Protocol):
    """Resolve a historical profile version, never an implicit latest profile."""

    def get_exact(
        self,
        *,
        profile_id: str,
        profile_version: int,
    ) -> VersionedProductProfile: ...


class _ReadableBody(Protocol):
    def read(self, size: int = -1) -> bytes: ...


class PinnedSourcePreparedReviewProducer:
    """Produce one analysis and listing from an exact, integrity-checked S3 version."""

    def __init__(
        self,
        *,
        store: SourceArtifactAuthority,
        s3: VersionedObjectClient,
        profiles: ProductProfileAuthority,
        intelligence: IntelligencePort,
    ) -> None:
        self._store = store
        self._s3 = s3
        self._profiles = profiles
        self._intelligence = intelligence

    def prepare_review(self, job_id: str, work_request_id: str) -> PreparedReviewObservation:
        """Read and inspect the exact intake source without retries or fallback."""

        if not job_id or not work_request_id:
            raise PreparedReviewProducerError("Preparation identity is invalid")
        source = self._source(job_id)
        content = self._source_content(source)
        artwork = self._validated_artwork(source, content)
        profile_fingerprint = self._profile_fingerprint(source)
        analysis, listing = self._run_intelligence(artwork, content)
        return PreparedReviewObservation(
            source_artifact_fingerprint=source.fingerprint,
            artwork_analysis=analysis,
            listing=listing,
            product_profile_fingerprint=profile_fingerprint,
        )

    def _source(self, job_id: str) -> SourceArtifactRecord:
        try:
            source = SourceArtifactRecord.model_validate(self._store.get_source_artifact(job_id))
        except Exception:
            raise PreparedReviewProducerError("Pinned source authority is unavailable") from None
        if source.job_id != job_id:
            raise PreparedReviewProducerError("Pinned source authority does not match the job")
        return source

    def _source_content(self, source: SourceArtifactRecord) -> bytes:
        try:
            response = self._s3.get_object(
                Bucket=source.bucket,
                Key=source.object_key,
                VersionId=source.version_id,
            )
        except Exception:
            raise PreparedReviewProducerError("Pinned source object is unavailable") from None
        if not isinstance(response, Mapping):
            raise PreparedReviewProducerError("Pinned source object response is invalid")
        if response.get("VersionId") != source.version_id:
            raise PreparedReviewProducerError("Pinned source object version is invalid")

        content_length = response.get("ContentLength")
        if content_length is not None and (
            isinstance(content_length, bool)
            or not isinstance(content_length, int)
            or content_length != source.size_bytes
        ):
            raise PreparedReviewProducerError("Pinned source object size is invalid")
        content_type = response.get("ContentType")
        if content_type is not None and content_type != source.media_type:
            raise PreparedReviewProducerError("Pinned source object media type is invalid")

        body = response.get("Body")
        read = getattr(body, "read", None)
        if not callable(read):
            raise PreparedReviewProducerError("Pinned source object body is invalid")
        try:
            content = _read_exactly_bounded(cast(_ReadableBody, body), source.size_bytes)
        except Exception:
            raise PreparedReviewProducerError("Pinned source object size is invalid") from None
        finally:
            close = getattr(body, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:
                    pass
        return content

    @staticmethod
    def _validated_artwork(source: SourceArtifactRecord, content: bytes) -> ArtworkInput:
        if (
            len(content) != source.size_bytes
            or sha256(content).hexdigest() != source.content_sha256
        ):
            raise PreparedReviewProducerError("Pinned source integrity check failed")
        try:
            artwork = validate_artwork(
                filename="source.png",
                content_type=source.media_type,
                content=content,
            )
        except InvalidArtworkError:
            raise PreparedReviewProducerError("Pinned source PNG is invalid") from None
        try:
            with Image.open(BytesIO(content)) as image:
                alpha_minimum, alpha_maximum = image.convert("RGBA").getchannel("A").getextrema()
                if image.width != image.height:
                    raise PreparedReviewProducerError("Pinned source PNG is invalid")
                if alpha_minimum == 255 or alpha_maximum == 0:
                    raise PreparedReviewProducerError("Pinned source PNG is invalid")
        except PreparedReviewProducerError:
            raise
        except (OSError, SyntaxError, UnidentifiedImageError):
            raise PreparedReviewProducerError("Pinned source PNG is invalid") from None
        if (
            artwork.content_sha256 != source.content_sha256
            or artwork.size_bytes != source.size_bytes
            or artwork.content_type != source.media_type
        ):
            raise PreparedReviewProducerError("Pinned source integrity check failed")
        return artwork

    def _profile_fingerprint(self, source: SourceArtifactRecord) -> str:
        try:
            exact = self._profiles.get_exact(
                profile_id=source.product_profile_id,
                profile_version=source.product_profile_version,
            )
            profile = ProductProfile.model_validate(exact.profile)
            reported_fingerprint = exact.fingerprint
        except Exception:
            raise PreparedReviewProducerError(
                "Pinned product profile authority is unavailable"
            ) from None
        computed_fingerprint = canonical_fingerprint(profile)
        if (
            profile.profile_id != source.product_profile_id
            or profile.profile_version != source.product_profile_version
            or reported_fingerprint != computed_fingerprint
            or computed_fingerprint != source.product_profile_fingerprint
        ):
            raise PreparedReviewProducerError("Pinned product profile authority has drifted")
        return computed_fingerprint

    def _run_intelligence(
        self,
        artwork: ArtworkInput,
        content: bytes,
    ) -> tuple[ArtworkAnalysis, ListingIntelligence]:
        try:
            analysis = ArtworkAnalysis.model_validate(
                self._intelligence.inspect_artwork(artwork, content)
            )
            listing = ListingIntelligence.model_validate(
                self._intelligence.draft_listing(artwork, content, analysis)
            )
        except Exception:
            raise PreparedReviewProducerError("Prepared review intelligence failed") from None
        return analysis, listing


def _read_exactly_bounded(body: _ReadableBody, expected_size: int) -> bytes:
    """Read at most ``expected_size + 1`` bytes and require an exact EOF boundary."""

    content = bytearray()
    upper_bound = expected_size + 1
    while len(content) < upper_bound:
        chunk = body.read(upper_bound - len(content))
        if not isinstance(chunk, bytes):
            raise TypeError("Object body returned non-bytes content")
        if not chunk:
            break
        content.extend(chunk)
    if len(content) != expected_size:
        raise ValueError("Object body size differs from pinned authority")
    return bytes(content)
