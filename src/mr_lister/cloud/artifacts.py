"""Exact-key S3 direct-upload adapter for the Phase 6 intake boundary."""

from __future__ import annotations

from base64 import b64encode
from collections.abc import Mapping
from datetime import datetime
from typing import Any, Protocol, cast
from urllib.parse import urlsplit

from mr_lister.control.upload_models import UploadAuthorization, UploadIntent
from mr_lister.control.upload_service import CurrentUploadObject

_STAGED_TAG = "mr-lister-state=staged"
_STAGED_TAG_SET = {"TagSet": [{"Key": "mr-lister-state", "Value": "staged"}]}
_PINNED_TAG_SET = {"TagSet": [{"Key": "mr-lister-state", "Value": "pinned"}]}


class _ReadableBody(Protocol):
    def read(self, size: int = -1) -> bytes: ...

    def close(self) -> None: ...


class S3UploadClient(Protocol):
    def generate_presigned_post(self, **kwargs: Any) -> Mapping[str, Any]: ...

    def get_object(self, **kwargs: Any) -> Mapping[str, Any]: ...

    def put_object_tagging(self, **kwargs: Any) -> Mapping[str, Any]: ...


class ExactKeyS3UploadArtifacts:
    """Issue and verify only one configured bucket's server-derived source key."""

    def __init__(
        self,
        *,
        client: S3UploadClient,
        bucket: str,
        bucket_owner_account_id: str,
        artifact_origin: str,
    ) -> None:
        parsed = urlsplit(artifact_origin)
        if (
            parsed.scheme != "https"
            or parsed.hostname is None
            or parsed.netloc != parsed.hostname
            or parsed.path
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("Artifact origin must be one exact HTTPS origin")
        if not bucket or not bucket.isascii() or "/" in bucket:
            raise ValueError("Artifact bucket configuration is invalid")
        if (
            len(bucket_owner_account_id) != 12
            or not bucket_owner_account_id.isascii()
            or not bucket_owner_account_id.isdigit()
        ):
            raise ValueError("Artifact bucket owner configuration is invalid")
        self._client = client
        self._bucket = bucket
        self._bucket_owner = bucket_owner_account_id
        self._origin = artifact_origin

    def issue_authorization(
        self,
        intent: UploadIntent,
        *,
        now: datetime,
    ) -> UploadAuthorization:
        self._require_intent_bucket(intent)
        if intent.authorization_issued_at is None or intent.authorization_expires_at is None:
            raise ValueError("Upload authorization time is unavailable")
        if now.utcoffset() is None or not intent.authorization_issued_at <= now:
            raise ValueError("Upload authorization signing time is invalid")
        expires_in = int((intent.authorization_expires_at - now).total_seconds())
        if expires_in < 1:
            raise ValueError("Upload authorization has expired")
        checksum = b64encode(bytes.fromhex(intent.content_sha256)).decode("ascii")
        fields = {
            "key": intent.object_key,
            "Content-Type": intent.content_type,
            "x-amz-checksum-algorithm": "SHA256",
            "x-amz-checksum-sha256": checksum,
            "x-amz-server-side-encryption": "AES256",
            "x-amz-tagging": _STAGED_TAG,
        }
        conditions: list[Any] = [
            {"key": intent.object_key},
            {"Content-Type": intent.content_type},
            {"x-amz-checksum-algorithm": "SHA256"},
            {"x-amz-checksum-sha256": checksum},
            {"x-amz-server-side-encryption": "AES256"},
            {"x-amz-tagging": _STAGED_TAG},
            ["content-length-range", intent.size_bytes, intent.size_bytes],
        ]
        response = self._client.generate_presigned_post(
            Bucket=self._bucket,
            Key=intent.object_key,
            Fields=fields,
            Conditions=conditions,
            ExpiresIn=expires_in,
        )
        if not isinstance(response, Mapping):
            raise ValueError("S3 upload authorization response is invalid")
        url = response.get("url")
        returned_fields = response.get("fields")
        if (
            url != f"{self._origin}/"
            or not isinstance(returned_fields, Mapping)
            or any(
                not isinstance(key, str) or not isinstance(value, str)
                for key, value in returned_fields.items()
            )
        ):
            raise ValueError("S3 upload authorization response is invalid")
        normalized_fields = dict(returned_fields)
        if any(normalized_fields.get(key) != value for key, value in fields.items()):
            raise ValueError("S3 upload authorization changed exact object conditions")
        return UploadAuthorization(
            owner_id=intent.owner_id,
            upload_id=intent.upload_id,
            job_id=intent.job_id,
            authorization_generation=intent.authorization_generation,
            url=cast(str, url),
            form_fields=normalized_fields,
            content_sha256=intent.content_sha256,
            size_bytes=intent.size_bytes,
            issued_at=now,
            expires_at=intent.authorization_expires_at,
        )

    def read_current_object(self, intent: UploadIntent) -> CurrentUploadObject:
        self._require_intent_bucket(intent)
        response = self._client.get_object(
            Bucket=self._bucket,
            Key=intent.object_key,
            ChecksumMode="ENABLED",
            ExpectedBucketOwner=self._bucket_owner,
        )
        if not isinstance(response, Mapping):
            raise ValueError("S3 current-object response is invalid")
        body = response.get("Body")
        read = getattr(body, "read", None)
        if not callable(read):
            raise ValueError("S3 current-object body is invalid")
        try:
            content = _read_bounded(cast(_ReadableBody, body), intent.size_bytes)
        finally:
            close = getattr(body, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:
                    pass
        return CurrentUploadObject(
            version_id=response.get("VersionId"),
            content=content,
            content_length=response.get("ContentLength"),
            content_type=response.get("ContentType"),
            checksum_sha256_base64=response.get("ChecksumSHA256"),
            server_side_encryption=response.get("ServerSideEncryption"),
        )

    def pin_object_version(self, intent: UploadIntent, version_id: str) -> None:
        self._tag_object_version(intent, version_id, _PINNED_TAG_SET)

    def release_unreferenced_version(self, intent: UploadIntent, version_id: str) -> None:
        """Return an exact unreferenced version to cleanup-eligible staged state."""

        self._tag_object_version(intent, version_id, _STAGED_TAG_SET)

    def _tag_object_version(
        self,
        intent: UploadIntent,
        version_id: str,
        tag_set: Mapping[str, Any],
    ) -> None:
        self._require_intent_bucket(intent)
        if not isinstance(version_id, str) or not version_id or not version_id.isascii():
            raise ValueError("Pinned S3 version is invalid")
        self._client.put_object_tagging(
            Bucket=self._bucket,
            Key=intent.object_key,
            VersionId=version_id,
            Tagging=tag_set,
            ExpectedBucketOwner=self._bucket_owner,
        )

    def _require_intent_bucket(self, intent: UploadIntent) -> None:
        if intent.bucket != self._bucket:
            raise ValueError("Upload intent does not bind the configured artifact bucket")


def _read_bounded(body: _ReadableBody, exact_size: int) -> bytes:
    chunks: list[bytes] = []
    remaining = exact_size + 1
    while remaining > 0:
        chunk = body.read(min(64 * 1024, remaining))
        if not isinstance(chunk, bytes):
            raise ValueError("S3 current-object body is invalid")
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    content = b"".join(chunks)
    if len(content) != exact_size:
        raise ValueError("S3 current-object size is invalid")
    return content


__all__ = ["ExactKeyS3UploadArtifacts", "S3UploadClient"]
