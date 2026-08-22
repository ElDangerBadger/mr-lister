from __future__ import annotations

from base64 import b64encode
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from typing import Any

import pytest

from mr_lister.cloud.artifacts import ExactKeyS3UploadArtifacts
from mr_lister.control.upload_models import UPLOAD_AUTHORIZATION_TTL, UploadIntent

NOW = datetime(2026, 8, 22, 12, 0, tzinfo=UTC)
OWNER_ID = "a" * 64
JOB_ID = "job_phase64_s3"
OBJECT_KEY = f"private/owners/{OWNER_ID}/jobs/{JOB_ID}/source/source.png"
BUCKET = "mr-lister-phase6-artifacts-dev"
BUCKET_OWNER = "123456789012"
ORIGIN = f"https://{BUCKET}.s3.us-west-2.amazonaws.com"
CONTENT = b"exact source artwork bytes"
CONTENT_SHA256 = sha256(CONTENT).hexdigest()
CHECKSUM_BASE64 = b64encode(bytes.fromhex(CONTENT_SHA256)).decode("ascii")


class _Body:
    def __init__(self, content: bytes) -> None:
        self._content = content
        self._offset = 0
        self.read_sizes: list[int] = []
        self.closed = False

    def read(self, size: int = -1) -> bytes:
        self.read_sizes.append(size)
        if size < 0:
            raise AssertionError("The artifact adapter must use bounded reads")
        start = self._offset
        self._offset = min(len(self._content), start + size)
        return self._content[start : self._offset]

    def close(self) -> None:
        self.closed = True


class _S3Spy:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.presigned_response: Mapping[str, Any] = {}
        self.get_response: Mapping[str, Any] = {}
        self.list_calls = 0

    def generate_presigned_post(self, **kwargs: Any) -> Mapping[str, Any]:
        self.calls.append(("generate_presigned_post", kwargs))
        return self.presigned_response

    def get_object(self, **kwargs: Any) -> Mapping[str, Any]:
        self.calls.append(("get_object", kwargs))
        return self.get_response

    def put_object_tagging(self, **kwargs: Any) -> Mapping[str, Any]:
        self.calls.append(("put_object_tagging", kwargs))
        return {}

    def list_objects_v2(self, **kwargs: Any) -> Mapping[str, Any]:
        self.list_calls += 1
        raise AssertionError(f"Bucket listing is forbidden: {kwargs!r}")


def _intent() -> UploadIntent:
    return UploadIntent(
        owner_id=OWNER_ID,
        upload_id="upload_phase64_s3",
        job_id=JOB_ID,
        filename="seller-art.png",
        content_type="image/png",
        content_sha256=CONTENT_SHA256,
        size_bytes=len(CONTENT),
        bucket=BUCKET,
        object_key=OBJECT_KEY,
        product_profile_id="gildan_64000_swiftpod",
        product_profile_version=2,
        product_profile_fingerprint="b" * 64,
        authorization_generation=1,
        authorization_issued_at=NOW,
        authorization_expires_at=NOW + UPLOAD_AUTHORIZATION_TTL,
        intent_expires_at=NOW + timedelta(days=1),
        created_at=NOW,
        updated_at=NOW,
    )


def _artifacts(client: _S3Spy) -> ExactKeyS3UploadArtifacts:
    return ExactKeyS3UploadArtifacts(
        client=client,
        bucket=BUCKET,
        bucket_owner_account_id=BUCKET_OWNER,
        artifact_origin=ORIGIN,
    )


def _required_form_fields() -> dict[str, str]:
    return {
        "key": OBJECT_KEY,
        "Content-Type": "image/png",
        "x-amz-checksum-algorithm": "SHA256",
        "x-amz-checksum-sha256": CHECKSUM_BASE64,
        "x-amz-server-side-encryption": "AES256",
        "x-amz-tagging": "mr-lister-state=staged",
    }


def test_presigned_post_binds_exact_object_integrity_encryption_size_and_ttl() -> None:
    client = _S3Spy()
    returned_fields = {
        **_required_form_fields(),
        "x-amz-algorithm": "AWS4-HMAC-SHA256",
        "x-amz-credential": "redacted-credential-scope",
        "x-amz-date": "20260822T190000Z",
        "policy": "redacted-policy",
        "x-amz-signature": "1" * 64,
    }
    client.presigned_response = {"url": f"{ORIGIN}/", "fields": returned_fields}

    authorization = _artifacts(client).issue_authorization(_intent(), now=NOW)

    required_fields = _required_form_fields()
    assert client.calls == [
        (
            "generate_presigned_post",
            {
                "Bucket": BUCKET,
                "Key": OBJECT_KEY,
                "Fields": required_fields,
                "Conditions": [
                    {"key": OBJECT_KEY},
                    {"Content-Type": "image/png"},
                    {"x-amz-checksum-algorithm": "SHA256"},
                    {"x-amz-checksum-sha256": CHECKSUM_BASE64},
                    {"x-amz-server-side-encryption": "AES256"},
                    {"x-amz-tagging": "mr-lister-state=staged"},
                    ["content-length-range", len(CONTENT), len(CONTENT)],
                ],
                "ExpiresIn": int(UPLOAD_AUTHORIZATION_TTL.total_seconds()),
            },
        )
    ]
    assert authorization.url == f"{ORIGIN}/"
    assert authorization.form_fields == returned_fields
    assert client.list_calls == 0


def test_presigned_post_replay_uses_only_the_remaining_durable_ttl() -> None:
    client = _S3Spy()
    returned_fields = {
        **_required_form_fields(),
        "x-amz-algorithm": "AWS4-HMAC-SHA256",
        "x-amz-credential": "redacted-credential-scope",
        "x-amz-date": "20260822T190000Z",
        "policy": "redacted-policy",
        "x-amz-signature": "1" * 64,
    }
    client.presigned_response = {"url": f"{ORIGIN}/", "fields": returned_fields}
    replay_time = NOW + timedelta(minutes=4)

    authorization = _artifacts(client).issue_authorization(
        _intent(),
        now=replay_time,
    )

    assert client.calls[0][1]["ExpiresIn"] == 60
    assert authorization.issued_at == replay_time
    assert authorization.expires_at == NOW + UPLOAD_AUTHORIZATION_TTL


@pytest.mark.parametrize(
    ("url", "field_update", "error"),
    (
        (
            "https://different-bucket.s3.us-west-2.amazonaws.com/",
            {},
            "response is invalid",
        ),
        (
            f"{ORIGIN}/",
            {"x-amz-server-side-encryption": "aws:kms"},
            "changed exact object conditions",
        ),
        (
            f"{ORIGIN}/",
            {"key": f"private/owners/{OWNER_ID}/jobs/other/source/source.png"},
            "changed exact object conditions",
        ),
    ),
)
def test_presigned_post_rejects_any_changed_returned_origin_or_required_field(
    url: str,
    field_update: dict[str, str],
    error: str,
) -> None:
    client = _S3Spy()
    client.presigned_response = {
        "url": url,
        "fields": {
            **_required_form_fields(),
            **field_update,
            "policy": "redacted-policy",
            "x-amz-signature": "1" * 64,
        },
    }

    with pytest.raises(ValueError, match=error):
        _artifacts(client).issue_authorization(_intent(), now=NOW)

    assert client.list_calls == 0


def test_read_current_object_is_exact_key_owner_bound_checksum_enabled_and_bounded() -> None:
    client = _S3Spy()
    body = _Body(CONTENT)
    client.get_response = {
        "Body": body,
        "VersionId": "version-exact-1",
        "ContentLength": len(CONTENT),
        "ContentType": "image/png",
        "ChecksumSHA256": CHECKSUM_BASE64,
        "ServerSideEncryption": "AES256",
    }

    current = _artifacts(client).read_current_object(_intent())

    assert client.calls == [
        (
            "get_object",
            {
                "Bucket": BUCKET,
                "Key": OBJECT_KEY,
                "ChecksumMode": "ENABLED",
                "ExpectedBucketOwner": BUCKET_OWNER,
            },
        )
    ]
    assert current.content == CONTENT
    assert current.version_id == "version-exact-1"
    assert current.checksum_sha256_base64 == CHECKSUM_BASE64
    assert body.read_sizes == [len(CONTENT) + 1, 1]
    assert body.closed is True
    assert client.list_calls == 0


def test_read_current_object_rejects_one_byte_beyond_declared_size() -> None:
    client = _S3Spy()
    body = _Body(CONTENT + b"!")
    client.get_response = {"Body": body}

    with pytest.raises(ValueError, match="size is invalid"):
        _artifacts(client).read_current_object(_intent())

    assert body.read_sizes == [len(CONTENT) + 1]
    assert body.closed is True
    assert client.list_calls == 0


def test_pin_tags_only_the_exact_version_and_owner_bound_key() -> None:
    client = _S3Spy()

    _artifacts(client).pin_object_version(_intent(), "version-exact-1")

    assert client.calls == [
        (
            "put_object_tagging",
            {
                "Bucket": BUCKET,
                "Key": OBJECT_KEY,
                "VersionId": "version-exact-1",
                "Tagging": {
                    "TagSet": [{"Key": "mr-lister-state", "Value": "pinned"}],
                },
                "ExpectedBucketOwner": BUCKET_OWNER,
            },
        )
    ]
    assert client.list_calls == 0


def test_release_unreferenced_retags_only_the_exact_version_as_staged() -> None:
    client = _S3Spy()

    _artifacts(client).release_unreferenced_version(_intent(), "version-exact-1")

    assert client.calls == [
        (
            "put_object_tagging",
            {
                "Bucket": BUCKET,
                "Key": OBJECT_KEY,
                "VersionId": "version-exact-1",
                "Tagging": {
                    "TagSet": [{"Key": "mr-lister-state", "Value": "staged"}],
                },
                "ExpectedBucketOwner": BUCKET_OWNER,
            },
        )
    ]
    assert client.list_calls == 0
