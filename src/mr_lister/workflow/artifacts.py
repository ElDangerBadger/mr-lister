"""Private artwork storage boundary with local and Amazon S3 adapters."""

from __future__ import annotations

from base64 import b64encode
from hashlib import sha256
from typing import Any, Protocol

from mr_lister.workflow.errors import ArtifactIntegrityError


class ArtifactStore(Protocol):
    def put_artwork(self, *, content_sha256: str, content: bytes) -> str: ...

    def get_artwork(self, *, object_key: str, expected_sha256: str) -> bytes: ...


def artwork_object_key(content_sha256: str, *, prefix: str = "private/artwork") -> str:
    """Return a stable, non-user-controlled key for one validated PNG."""

    return f"{prefix.rstrip('/')}/sha256/{content_sha256[:2]}/{content_sha256}.png"


def _verify_content(content: bytes, expected_sha256: str) -> None:
    if sha256(content).hexdigest() != expected_sha256:
        raise ArtifactIntegrityError("Artwork content does not match its expected checksum")


class InMemoryArtifactStore:
    """Offline artifact adapter with the same checksum contract as S3."""

    def __init__(self) -> None:
        self._objects: dict[str, bytes] = {}

    def put_artwork(self, *, content_sha256: str, content: bytes) -> str:
        _verify_content(content, content_sha256)
        key = artwork_object_key(content_sha256)
        existing = self._objects.get(key)
        if existing is not None and existing != content:
            raise ArtifactIntegrityError("Artwork key is already bound to different content")
        self._objects[key] = content
        return key

    def get_artwork(self, *, object_key: str, expected_sha256: str) -> bytes:
        try:
            content = self._objects[object_key]
        except KeyError as error:
            raise ArtifactIntegrityError("Artwork object is unavailable") from error
        _verify_content(content, expected_sha256)
        return content


class S3ArtifactStore:
    """Store private artwork in a preconfigured S3 bucket.

    Bucket policy, public-access blocks, lifecycle rules, and the optional KMS key are provisioned
    by infrastructure code. The adapter never sets an ACL and always requests encryption.
    """

    def __init__(
        self,
        *,
        client: Any,
        bucket: str,
        prefix: str = "private/artwork",
        kms_key_id: str | None = None,
    ) -> None:
        self._client = client
        self._bucket = bucket
        self._prefix = prefix
        self._kms_key_id = kms_key_id

    def put_artwork(self, *, content_sha256: str, content: bytes) -> str:
        _verify_content(content, content_sha256)
        key = artwork_object_key(content_sha256, prefix=self._prefix)
        checksum = b64encode(bytes.fromhex(content_sha256)).decode("ascii")
        encryption = (
            {"ServerSideEncryption": "aws:kms", "SSEKMSKeyId": self._kms_key_id}
            if self._kms_key_id
            else {"ServerSideEncryption": "AES256"}
        )
        self._client.put_object(
            Bucket=self._bucket,
            Key=key,
            Body=content,
            ContentType="image/png",
            ChecksumSHA256=checksum,
            Metadata={"content-sha256": content_sha256},
            **encryption,
        )
        return key

    def get_artwork(self, *, object_key: str, expected_sha256: str) -> bytes:
        response = self._client.get_object(Bucket=self._bucket, Key=object_key)
        content = response["Body"].read()
        _verify_content(content, expected_sha256)
        return content
