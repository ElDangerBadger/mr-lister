"""Owner-scoped artwork-preview links and exact-version S3 redirect authorization."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol
from urllib.parse import parse_qs, quote, urlsplit

from mr_lister.control.models import ControlJobRecord, SourceArtifactRecord
from mr_lister.control.projection import PreviewGrant
from mr_lister.control.source_artwork import validate_source_artifact_authority

MAX_PREVIEW_SECONDS = 300


class PreviewAuthorityStore(Protocol):
    def get_job_for_owner(self, owner_id: str, job_id: str) -> ControlJobRecord: ...

    def get_source_artifact(self, job_id: str) -> SourceArtifactRecord: ...


class S3PreviewPresigner(Protocol):
    def generate_presigned_url(
        self,
        ClientMethod: str,
        Params: dict[str, str],
        ExpiresIn: int,
        HttpMethod: str,
    ) -> str: ...


@dataclass(frozen=True, slots=True)
class PreviewRedirect:
    location: str
    expires_at: datetime


class AuthenticatedPreviewLinkIssuer:
    """Issue only the authenticated application endpoint shown in seller projections."""

    def __init__(
        self,
        *,
        application_origin: str,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        origin = urlsplit(application_origin)
        if (
            origin.scheme != "https"
            or origin.hostname is None
            or origin.netloc != origin.hostname
            or origin.path
            or origin.query
            or origin.fragment
        ):
            raise ValueError("Preview application origin must be one exact HTTPS origin")
        self._origin = application_origin
        self._clock = clock or (lambda: datetime.now(UTC))

    def issue(self, *, source: SourceArtifactRecord) -> PreviewGrant:
        now = self._clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("Preview clock must return an aware timestamp")
        return PreviewGrant(
            url=f"{self._origin}/v1/jobs/{source.job_id}/artwork-preview",
            expires_at=now + timedelta(seconds=MAX_PREVIEW_SECONDS),
            source_artifact_fingerprint=source.fingerprint,
        )


class ExactVersionArtworkPreviewService:
    """Authorize a direct S3 GET for only an owned job's immutable source version."""

    def __init__(
        self,
        *,
        store: PreviewAuthorityStore,
        presigner: S3PreviewPresigner,
        artifact_bucket: str,
        artifact_origin: str,
        clock: Callable[[], datetime] | None = None,
        expires_in: int = MAX_PREVIEW_SECONDS,
    ) -> None:
        if not 1 <= expires_in <= MAX_PREVIEW_SECONDS:
            raise ValueError("Preview expiration must be between one and five minutes")
        parsed_origin = urlsplit(artifact_origin)
        if (
            parsed_origin.scheme != "https"
            or parsed_origin.hostname is None
            or parsed_origin.netloc != parsed_origin.hostname
            or parsed_origin.path
            or parsed_origin.query
            or parsed_origin.fragment
        ):
            raise ValueError("Artifact origin must be one exact HTTPS origin")
        if not artifact_bucket or not artifact_bucket.isascii() or "/" in artifact_bucket:
            raise ValueError("Artifact bucket configuration is invalid")
        self._store = store
        self._presigner = presigner
        self._bucket = artifact_bucket
        self._artifact_origin = artifact_origin
        self._artifact_host = parsed_origin.hostname
        self._clock = clock or (lambda: datetime.now(UTC))
        self._expires_in = expires_in

    def authorize(self, *, owner_id: str, job_id: str) -> PreviewRedirect:
        job = self._store.get_job_for_owner(owner_id, job_id)
        source = self._store.get_source_artifact(job.job_id)
        try:
            validate_source_artifact_authority(source)
        except ValueError:
            raise PreviewAuthorizationUnavailableError from None
        if (
            source.owner_id != job.owner_id
            or source.job_id != job.job_id
            or source.fingerprint != job.source_artifact_fingerprint
            or source.bucket != self._bucket
        ):
            raise PreviewAuthorizationUnavailableError
        params = {
            "Bucket": self._bucket,
            "Key": source.object_key,
            "VersionId": source.version_id,
            "ResponseContentType": "image/png",
            "ResponseCacheControl": "private, no-store, max-age=0",
        }
        try:
            location = self._presigner.generate_presigned_url(
                "get_object",
                Params=params,
                ExpiresIn=self._expires_in,
                HttpMethod="GET",
            )
        except Exception:
            raise PreviewAuthorizationUnavailableError from None
        self._validate_location(location, source)
        now = self._clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise PreviewAuthorizationUnavailableError
        return PreviewRedirect(
            location=location,
            expires_at=now + timedelta(seconds=self._expires_in),
        )

    def _validate_location(self, location: object, source: SourceArtifactRecord) -> None:
        if (
            not isinstance(location, str)
            or not location.isascii()
            or len(location) > 8_192
            or "\\" in location
            or any(ord(character) < 32 or ord(character) == 127 for character in location)
        ):
            raise PreviewAuthorizationUnavailableError
        try:
            parsed = urlsplit(location)
            query = parse_qs(parsed.query, keep_blank_values=True, strict_parsing=True)
        except (TypeError, ValueError):
            raise PreviewAuthorizationUnavailableError from None
        expected_path = "/" + quote(source.object_key, safe="/")
        if (
            parsed.scheme != "https"
            or parsed.hostname != self._artifact_host
            or parsed.netloc != self._artifact_host
            or parsed.username is not None
            or parsed.password is not None
            or parsed.fragment
            or parsed.path != expected_path
            or query.get("versionId") != [source.version_id]
            or query.get("response-content-type") != ["image/png"]
            or query.get("response-cache-control") != ["private, no-store, max-age=0"]
            or query.get("X-Amz-Expires") != [str(self._expires_in)]
        ):
            raise PreviewAuthorizationUnavailableError


class PreviewAuthorizationUnavailableError(Exception):
    """A preview could not be authorized without exposing storage details."""

    code = "PROJECTION_UNAVAILABLE"


def preview_redirect_response(
    redirect: PreviewRedirect,
    *,
    request_id: str,
) -> dict[str, Any]:
    """Return a bodyless, non-cacheable redirect directly to the immutable S3 object."""

    if (
        not isinstance(redirect.location, str)
        or not redirect.location.isascii()
        or len(redirect.location) > 8_192
        or "\\" in redirect.location
        or any(ord(character) < 32 or ord(character) == 127 for character in redirect.location)
    ):
        raise PreviewAuthorizationUnavailableError

    return {
        "statusCode": 302,
        "headers": {
            "Cache-Control": "private, no-store, max-age=0",
            "Location": redirect.location,
            "Pragma": "no-cache",
            "Referrer-Policy": "no-referrer",
            "X-Content-Type-Options": "nosniff",
            "X-Request-Id": request_id,
        },
        "body": "",
        "isBase64Encoded": False,
    }


__all__ = [
    "AuthenticatedPreviewLinkIssuer",
    "ExactVersionArtworkPreviewService",
    "MAX_PREVIEW_SECONDS",
    "PreviewAuthorizationUnavailableError",
    "PreviewAuthorityStore",
    "PreviewRedirect",
    "preview_redirect_response",
]
