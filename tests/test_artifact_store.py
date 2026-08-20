from __future__ import annotations

from hashlib import sha256
from io import BytesIO

import pytest

from mr_lister.workflow.artifacts import (
    InMemoryArtifactStore,
    S3ArtifactStore,
    artwork_object_key,
)
from mr_lister.workflow.errors import ArtifactIntegrityError

PNG_CONTENT = b"synthetic-private-png"
PNG_SHA256 = sha256(PNG_CONTENT).hexdigest()


class RecordingS3Client:
    def __init__(self, content: bytes = PNG_CONTENT) -> None:
        self.content = content
        self.put_request: dict[str, object] | None = None
        self.get_request: dict[str, object] | None = None

    def put_object(self, **request: object) -> None:
        self.put_request = request

    def get_object(self, **request: object) -> dict[str, BytesIO]:
        self.get_request = request
        return {"Body": BytesIO(self.content)}


def test_content_addressed_key_contains_no_user_filename() -> None:
    key = artwork_object_key(PNG_SHA256)

    assert key == f"private/artwork/sha256/{PNG_SHA256[:2]}/{PNG_SHA256}.png"


def test_in_memory_artifacts_are_idempotent_and_checksum_verified() -> None:
    store = InMemoryArtifactStore()

    first = store.put_artwork(content_sha256=PNG_SHA256, content=PNG_CONTENT)
    repeated = store.put_artwork(content_sha256=PNG_SHA256, content=PNG_CONTENT)

    assert repeated == first
    assert store.get_artwork(object_key=first, expected_sha256=PNG_SHA256) == PNG_CONTENT
    with pytest.raises(ArtifactIntegrityError, match="expected checksum"):
        store.put_artwork(content_sha256="0" * 64, content=PNG_CONTENT)


def test_s3_put_requests_private_encrypted_checksum_bound_object() -> None:
    client = RecordingS3Client()
    store = S3ArtifactStore(client=client, bucket="private-bucket")

    key = store.put_artwork(content_sha256=PNG_SHA256, content=PNG_CONTENT)

    assert client.put_request == {
        "Bucket": "private-bucket",
        "Key": key,
        "Body": PNG_CONTENT,
        "ContentType": "image/png",
        "ChecksumSHA256": "L+z2HfAmmjAEinsr/0TU7KLWgfemACNcu2Z8mUZe3tA=",
        "Metadata": {"content-sha256": PNG_SHA256},
        "ServerSideEncryption": "AES256",
    }
    assert "ACL" not in client.put_request


def test_s3_kms_encryption_and_download_integrity() -> None:
    client = RecordingS3Client()
    store = S3ArtifactStore(
        client=client,
        bucket="private-bucket",
        kms_key_id="arn:aws:kms:us-west-2:123456789012:key/example",
    )
    key = store.put_artwork(content_sha256=PNG_SHA256, content=PNG_CONTENT)

    downloaded = store.get_artwork(object_key=key, expected_sha256=PNG_SHA256)

    assert downloaded == PNG_CONTENT
    assert client.put_request is not None
    assert client.put_request["ServerSideEncryption"] == "aws:kms"
    assert client.put_request["SSEKMSKeyId"] == ("arn:aws:kms:us-west-2:123456789012:key/example")
    assert client.get_request == {"Bucket": "private-bucket", "Key": key}


def test_s3_download_rejects_corrupt_content() -> None:
    client = RecordingS3Client(content=b"corrupt")
    store = S3ArtifactStore(client=client, bucket="private-bucket")

    with pytest.raises(ArtifactIntegrityError, match="expected checksum"):
        store.get_artwork(
            object_key=artwork_object_key(PNG_SHA256),
            expected_sha256=PNG_SHA256,
        )
