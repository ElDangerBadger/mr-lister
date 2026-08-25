"""Verify an offline proof for one Phase 6 S3 release object.

The verifier has no AWS client. It consumes either the original canonical, operator-captured
PutObject/readback/revocation lifecycle or a narrower manual-root Lambda readback. A VersionId by
itself is deliberately insufficient.

The evidence scopes remain distinct:

* byte identity binds the locally sealed bytes to one exact S3 VersionId (it does not prove that
  a separately privileged principal can never delete that version); and
* common-v2 additionally proves the upload lifecycle and a retained group Deny governing the
  exact uploading IAM user. The manual-root Lambda format proves only the observed singleton key
  state and exact-version byte binding; it makes no IAM-revocation claim.
"""

from __future__ import annotations

import base64
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import Final, Literal

Phase6ReleaseComponent = Literal["agentcore", "lambda"]

EVIDENCE_FORMAT: Final = "mr-lister-phase6-s3-release-object-evidence-v2"
MANUAL_ROOT_LAMBDA_EVIDENCE_FORMAT: Final = "mr-lister-phase6-s3-manual-root-lambda-evidence-v1"
_ACCOUNT_ID = re.compile(r"^[0-9]{12}$")
_ENVIRONMENT = re.compile(r"^[a-z][a-z0-9-]{1,15}$")
_HEX_32_QUOTED = re.compile(r'^"[A-Fa-f0-9]{32}"$')
_HEX_64 = re.compile(r"^[a-f0-9]{64}$")
_OWNER_ID = re.compile(r"^[A-Fa-f0-9]{64}$")
_TIMESTAMP = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$")
_AWS_TIMESTAMP = re.compile(
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}"
    r"(?:\.[0-9]{1,6})?(?:Z|\+00:00)$"
)
_PLACEHOLDER = re.compile(
    r"<[A-Z][A-Z0-9_]*>|\$\{[^}\r\n]+}|__[A-Z][A-Z0-9_]*__|"
    r"\b(?:PLACEHOLDER|REPLACE_ME|CHANGEME)\b",
    re.IGNORECASE,
)
_MOVING_VERSION_IDS: Final = frozenset(
    {"current", "default", "latest", "moving", "null", "none", "unversioned"}
)
_GENERIC_ERROR: Final = "Phase 6 S3 release-object evidence is invalid"


class Phase6S3ReleaseObjectEvidenceError(RuntimeError):
    """A value-free failure for malformed, moving, incomplete, or drifting evidence."""


@dataclass(frozen=True, slots=True)
class Phase6S3ReleaseObjectExpectation:
    """Locally verified identity and bytes expected at one content-addressed S3 key."""

    account_id: str
    region: str
    environment: str
    component: Phase6ReleaseComponent
    release_fingerprint: str
    archive_sha256: str
    size_bytes: int

    def __post_init__(self) -> None:
        try:
            strings = (
                self.account_id,
                self.region,
                self.environment,
                self.component,
                self.release_fingerprint,
                self.archive_sha256,
            )
            if (
                not all(isinstance(value, str) for value in strings)
                or _ACCOUNT_ID.fullmatch(self.account_id) is None
                or self.account_id == "0" * 12
                or self.region != "us-west-2"
                or _ENVIRONMENT.fullmatch(self.environment) is None
                or self.environment != "dev"
                or self.component not in ("agentcore", "lambda")
                or _HEX_64.fullmatch(self.release_fingerprint) is None
                or self.release_fingerprint == "0" * 64
                or _HEX_64.fullmatch(self.archive_sha256) is None
                or self.archive_sha256 == "0" * 64
                or not isinstance(self.size_bytes, int)
                or isinstance(self.size_bytes, bool)
                or self.size_bytes <= 0
                or _PLACEHOLDER.search("\n".join(strings))
            ):
                raise ValueError
        except Exception:
            raise Phase6S3ReleaseObjectEvidenceError(_GENERIC_ERROR) from None

    @property
    def bucket(self) -> str:
        return f"mr-lister-phase6-artifacts-{self.environment}-{self.account_id}-{self.region}"

    @property
    def archive_filename(self) -> str:
        return f"phase6-{self.component}.zip"

    @property
    def key(self) -> str:
        return (
            f"private/deployments/{self.component}/releases/{self.release_fingerprint}/"
            f"phase6-{self.component}-{self.archive_sha256}.zip"
        )

    @property
    def checksum_sha256_base64(self) -> str:
        return base64.b64encode(bytes.fromhex(self.archive_sha256)).decode("ascii")

    @property
    def metadata(self) -> dict[str, str]:
        return {
            "mr-lister-archive-sha256": self.archive_sha256,
            "mr-lister-component": self.component,
            "mr-lister-release-fingerprint": self.release_fingerprint,
            "mr-lister-size-bytes": str(self.size_bytes),
        }

    @property
    def upload_policy_name(self) -> str:
        return f"mr-lister-phase6-{self.component}-direct-uploader-dev"

    @property
    def upload_policy_arn(self) -> str:
        return f"arn:aws:iam::{self.account_id}:policy/{self.upload_policy_name}"

    @property
    def readback_policy_name(self) -> str:
        return f"mr-lister-phase6-{self.component}-direct-evidence-reader-dev"

    @property
    def readback_policy_arn(self) -> str:
        return f"arn:aws:iam::{self.account_id}:policy/{self.readback_policy_name}"

    @property
    def freeze_policy_name(self) -> str:
        return f"mr-lister-phase6-{self.component}-release-freeze-dev"

    @property
    def freeze_policy_arn(self) -> str:
        return f"arn:aws:iam::{self.account_id}:policy/{self.freeze_policy_name}"

    @property
    def object_arn(self) -> str:
        return f"arn:aws:s3:::{self.bucket}/{self.key}"


@dataclass(frozen=True, slots=True)
class VerifiedPhase6S3ReleaseObject:
    """Exact immutable remote-object binding after all closed evidence checks pass."""

    account_id: str
    region: str
    environment: str
    component: Phase6ReleaseComponent
    release_fingerprint: str
    archive_sha256: str
    size_bytes: int
    checksum_sha256_base64: str
    bucket: str
    key: str
    version_id: str
    evidence_sha256: str

    def __post_init__(self) -> None:
        try:
            expectation = Phase6S3ReleaseObjectExpectation(
                account_id=self.account_id,
                region=self.region,
                environment=self.environment,
                component=self.component,
                release_fingerprint=self.release_fingerprint,
                archive_sha256=self.archive_sha256,
                size_bytes=self.size_bytes,
            )
            validate_phase6_s3_version_id(self.version_id)
            if (
                self.checksum_sha256_base64 != expectation.checksum_sha256_base64
                or self.bucket != expectation.bucket
                or self.key != expectation.key
                or _HEX_64.fullmatch(self.evidence_sha256) is None
            ):
                raise ValueError
        except Phase6S3ReleaseObjectEvidenceError:
            raise
        except Exception:
            raise Phase6S3ReleaseObjectEvidenceError(_GENERIC_ERROR) from None


def verify_phase6_s3_release_object_evidence(
    expectation: Phase6S3ReleaseObjectExpectation,
    *,
    evidence_path: Path,
) -> VerifiedPhase6S3ReleaseObject:
    """Return a version-bound object only after the complete v2 lifecycle proof validates."""

    return _verify_phase6_s3_release_object_evidence(
        expectation,
        evidence_path=evidence_path,
        allow_manual_root_lambda=False,
    )


def verify_phase6_lambda_release_object_evidence(
    expectation: Phase6S3ReleaseObjectExpectation,
    *,
    evidence_path: Path,
) -> VerifiedPhase6S3ReleaseObject:
    """Verify a Lambda object through either v2 or the narrow manual-root readback format."""

    try:
        if (
            not isinstance(expectation, Phase6S3ReleaseObjectExpectation)
            or expectation.component != "lambda"
        ):
            raise ValueError
        return _verify_phase6_s3_release_object_evidence(
            expectation,
            evidence_path=evidence_path,
            allow_manual_root_lambda=True,
        )
    except Phase6S3ReleaseObjectEvidenceError:
        raise
    except Exception:
        raise Phase6S3ReleaseObjectEvidenceError(_GENERIC_ERROR) from None


def _verify_phase6_s3_release_object_evidence(
    expectation: Phase6S3ReleaseObjectExpectation,
    *,
    evidence_path: Path,
    allow_manual_root_lambda: bool,
) -> VerifiedPhase6S3ReleaseObject:
    """Return a version-bound object only after one explicitly allowed format validates."""

    try:
        if not isinstance(expectation, Phase6S3ReleaseObjectExpectation):
            raise ValueError
        if not isinstance(evidence_path, Path) or any(
            candidate.is_symlink() for candidate in (evidence_path, *evidence_path.parents)
        ):
            raise ValueError
        path = evidence_path.resolve(strict=True)
        if not path.is_file():
            raise ValueError
        raw = path.read_bytes()
        document = json.loads(raw, object_pairs_hook=_unique_object)
        if not isinstance(document, dict) or canonical_phase6_s3_evidence(document) != raw:
            raise ValueError
        evidence_format = document.get("format")
        if evidence_format == EVIDENCE_FORMAT:
            _validate_closed_evidence(expectation, document)
        elif allow_manual_root_lambda and evidence_format == MANUAL_ROOT_LAMBDA_EVIDENCE_FORMAT:
            _validate_manual_root_lambda_evidence(expectation, document)
        else:
            raise ValueError
        return VerifiedPhase6S3ReleaseObject(
            account_id=expectation.account_id,
            region=expectation.region,
            environment=expectation.environment,
            component=expectation.component,
            release_fingerprint=expectation.release_fingerprint,
            archive_sha256=expectation.archive_sha256,
            size_bytes=expectation.size_bytes,
            checksum_sha256_base64=expectation.checksum_sha256_base64,
            bucket=expectation.bucket,
            key=expectation.key,
            version_id=document["versionId"],
            evidence_sha256=sha256(raw).hexdigest(),
        )
    except Phase6S3ReleaseObjectEvidenceError:
        raise
    except Exception:
        raise Phase6S3ReleaseObjectEvidenceError(_GENERIC_ERROR) from None


def canonical_phase6_s3_evidence(value: object) -> bytes:
    """Return the one accepted byte representation for normalized evidence."""

    try:
        return (
            json.dumps(
                value,
                allow_nan=False,
                indent=2,
                separators=(",", ": "),
                sort_keys=True,
            )
            + "\n"
        ).encode("utf-8")
    except Exception:
        raise Phase6S3ReleaseObjectEvidenceError(_GENERIC_ERROR) from None


def validate_phase6_s3_version_id(value: str) -> None:
    """Reject null, moving, placeholder, whitespace, and non-literal S3 version identities."""

    try:
        if (
            not isinstance(value, str)
            or not 3 <= len(value) <= 1024
            or value != value.strip()
            or value.casefold() in _MOVING_VERSION_IDS
            or _PLACEHOLDER.search(value)
            or any(ord(character) < 0x21 or ord(character) > 0x7E for character in value)
            or any(character in value for character in ('"', "\\"))
        ):
            raise ValueError
    except Exception:
        raise Phase6S3ReleaseObjectEvidenceError(_GENERIC_ERROR) from None


def _validate_closed_evidence(
    expectation: Phase6S3ReleaseObjectExpectation,
    document: Mapping[str, object],
) -> None:
    if set(document) != {
        "accountId",
        "bucket",
        "bucketStateBeforePut",
        "component",
        "format",
        "key",
        "postRevocationReadback",
        "preRevocationReadback",
        "putObject",
        "region",
        "releaseFingerprint",
        "uploadAuthorityRevocation",
        "versionId",
    }:
        raise ValueError
    version_id = document.get("versionId")
    validate_phase6_s3_version_id(version_id)  # type: ignore[arg-type]
    if (
        document.get("format") != EVIDENCE_FORMAT
        or document.get("accountId") != expectation.account_id
        or document.get("region") != expectation.region
        or document.get("component") != expectation.component
        or document.get("releaseFingerprint") != expectation.release_fingerprint
        or document.get("bucket") != expectation.bucket
        or document.get("key") != expectation.key
    ):
        raise ValueError

    bucket_state = _mapping(document, "bucketStateBeforePut")
    _validate_bucket_state(expectation, bucket_state)
    put = _mapping(document, "putObject")
    caller_identity, etag = _validate_put_object(expectation, version_id, put)
    before = _mapping(document, "preRevocationReadback")
    _validate_readback(expectation, version_id, etag, before)
    revocation = _mapping(document, "uploadAuthorityRevocation")
    _validate_revocation(expectation, caller_identity, revocation)
    after = _mapping(document, "postRevocationReadback")
    _validate_readback(expectation, version_id, etag, after)

    if before.get("headObject") != after.get("headObject"):
        raise ValueError
    if before.get("listObjectVersions") != after.get("listObjectVersions"):
        raise ValueError

    ordered_times = [
        _timestamp(bucket_state, "capturedAt"),
        _timestamp(put, "capturedAt"),
        _timestamp(before, "capturedAt"),
        _timestamp(_mapping(revocation, "attachmentBefore"), "capturedAt"),
        _timestamp(_mapping(revocation, "attachmentAfter"), "capturedAt"),
        _timestamp(_mapping(revocation, "freezePolicyReadback"), "capturedAt"),
        _timestamp(_mapping(revocation, "groupMembershipReadback"), "capturedAt"),
        _timestamp(_mapping(revocation, "denyProbe"), "capturedAt"),
        _timestamp(after, "capturedAt"),
    ]
    if any(left >= right for left, right in zip(ordered_times, ordered_times[1:], strict=False)):
        raise ValueError


def _validate_manual_root_lambda_evidence(
    expectation: Phase6S3ReleaseObjectExpectation,
    document: Mapping[str, object],
) -> None:
    if expectation.component != "lambda" or set(document) != {
        "accountId",
        "bucket",
        "bucketState",
        "component",
        "format",
        "key",
        "readback",
        "region",
        "releaseFingerprint",
        "versionId",
    }:
        raise ValueError
    version_id = document.get("versionId")
    validate_phase6_s3_version_id(version_id)  # type: ignore[arg-type]
    if (
        document.get("format") != MANUAL_ROOT_LAMBDA_EVIDENCE_FORMAT
        or document.get("accountId") != expectation.account_id
        or document.get("region") != expectation.region
        or document.get("component") != "lambda"
        or document.get("releaseFingerprint") != expectation.release_fingerprint
        or document.get("bucket") != expectation.bucket
        or document.get("key") != expectation.key
    ):
        raise ValueError

    bucket_state = _mapping(document, "bucketState")
    _validate_bucket_state(expectation, bucket_state)
    readback = _mapping(document, "readback")
    head = _mapping(readback, "headObject")
    etag = _mapping(head, "response").get("ETag")
    if not isinstance(etag, str) or _HEX_32_QUOTED.fullmatch(etag) is None:
        raise ValueError
    _validate_readback(expectation, version_id, etag, readback)
    if _timestamp(bucket_state, "capturedAt") >= _timestamp(readback, "capturedAt"):
        raise ValueError


def _validate_bucket_state(
    expectation: Phase6S3ReleaseObjectExpectation,
    value: Mapping[str, object],
) -> None:
    if set(value) != {"capturedAt", "ownershipControls", "versioning"}:
        raise ValueError
    _timestamp(value, "capturedAt")
    versioning = _mapping(value, "versioning")
    if set(versioning) != {"request", "response"}:
        raise ValueError
    _validate_bucket_request(expectation, _mapping(versioning, "request"))
    if _mapping(versioning, "response") != {"Status": "Enabled"}:
        raise ValueError
    ownership = _mapping(value, "ownershipControls")
    if set(ownership) != {"request", "response"}:
        raise ValueError
    _validate_bucket_request(expectation, _mapping(ownership, "request"))
    if _mapping(ownership, "response") != {
        "OwnershipControls": {"Rules": [{"ObjectOwnership": "BucketOwnerEnforced"}]}
    }:
        raise ValueError


def _validate_put_object(
    expectation: Phase6S3ReleaseObjectExpectation,
    version_id: object,
    value: Mapping[str, object],
) -> tuple[dict[str, object], str]:
    if set(value) != {"callerIdentity", "capturedAt", "request", "response"}:
        raise ValueError
    _timestamp(value, "capturedAt")
    caller_identity = _validate_caller_identity(expectation, _mapping(value, "callerIdentity"))
    _validate_put_request(expectation, _mapping(value, "request"))
    response = _mapping(value, "response")
    if set(response) != {
        "ChecksumSHA256",
        "ChecksumType",
        "ETag",
        "ServerSideEncryption",
        "VersionId",
    }:
        raise ValueError
    etag = response.get("ETag")
    if (
        response.get("ChecksumSHA256") != expectation.checksum_sha256_base64
        or response.get("ChecksumType") != "FULL_OBJECT"
        or response.get("ServerSideEncryption") != "AES256"
        or response.get("VersionId") != version_id
        or not isinstance(etag, str)
        or _HEX_32_QUOTED.fullmatch(etag) is None
    ):
        raise ValueError
    return caller_identity, etag


def _validate_readback(
    expectation: Phase6S3ReleaseObjectExpectation,
    version_id: object,
    etag: str,
    value: Mapping[str, object],
) -> None:
    if set(value) != {"capturedAt", "headObject", "listObjectVersions"}:
        raise ValueError
    _timestamp(value, "capturedAt")
    head = _mapping(value, "headObject")
    if set(head) != {"request", "response"}:
        raise ValueError
    head_request = _mapping(head, "request")
    if head_request != {
        "bucket": expectation.bucket,
        "checksumMode": "ENABLED",
        "expectedBucketOwner": expectation.account_id,
        "key": expectation.key,
        "versionId": version_id,
    }:
        raise ValueError
    head_response = _mapping(head, "response")
    if set(head_response) != {
        "ChecksumSHA256",
        "ChecksumType",
        "ContentLength",
        "ETag",
        "LastModified",
        "Metadata",
        "ServerSideEncryption",
        "VersionId",
    }:
        raise ValueError
    content_length = head_response.get("ContentLength")
    last_modified = head_response.get("LastModified")
    if (
        not isinstance(content_length, int)
        or isinstance(content_length, bool)
        or content_length != expectation.size_bytes
        or head_response.get("ChecksumSHA256") != expectation.checksum_sha256_base64
        or head_response.get("ChecksumType") != "FULL_OBJECT"
        or head_response.get("ETag") != etag
        or head_response.get("Metadata") != expectation.metadata
        or head_response.get("ServerSideEncryption") != "AES256"
        or head_response.get("VersionId") != version_id
        or not isinstance(last_modified, str)
        or not _valid_aws_timestamp(last_modified)
    ):
        raise ValueError

    listing = _mapping(value, "listObjectVersions")
    if set(listing) != {"request", "response"}:
        raise ValueError
    if _mapping(listing, "request") != {
        "bucket": expectation.bucket,
        "expectedBucketOwner": expectation.account_id,
        "prefix": expectation.key,
    }:
        raise ValueError
    response = _mapping(listing, "response")
    if set(response) != {
        "DeleteMarkers",
        "IsTruncated",
        "Name",
        "Prefix",
        "Versions",
    }:
        raise ValueError
    listed = response.get("Versions")
    if (
        response.get("Name") != expectation.bucket
        or response.get("Prefix") != expectation.key
        or response.get("IsTruncated") is not False
        or response.get("DeleteMarkers") != []
        or not isinstance(listed, list)
        or len(listed) != 1
    ):
        raise ValueError
    listed_version = listed[0]
    if not isinstance(listed_version, Mapping) or set(listed_version) != {
        "ChecksumAlgorithm",
        "ChecksumType",
        "ETag",
        "IsLatest",
        "Key",
        "LastModified",
        "Owner",
        "Size",
        "StorageClass",
        "VersionId",
    }:
        raise ValueError
    owner = listed_version.get("Owner")
    listed_size = listed_version.get("Size")
    listed_modified = listed_version.get("LastModified")
    if (
        listed_version.get("ChecksumAlgorithm") != ["SHA256"]
        or listed_version.get("ChecksumType") != "FULL_OBJECT"
        or listed_version.get("ETag") != etag
        or listed_version.get("Key") != expectation.key
        or listed_version.get("VersionId") != version_id
        or listed_version.get("IsLatest") is not True
        or listed_version.get("StorageClass") != "STANDARD"
        or not isinstance(listed_size, int)
        or isinstance(listed_size, bool)
        or listed_size != expectation.size_bytes
        or not isinstance(listed_modified, str)
        or not _valid_aws_timestamp(listed_modified)
        or not isinstance(owner, Mapping)
        or set(owner) != {"ID"}
        or not isinstance(owner.get("ID"), str)
        or _OWNER_ID.fullmatch(owner["ID"]) is None  # type: ignore[arg-type]
    ):
        raise ValueError


def _validate_revocation(
    expectation: Phase6S3ReleaseObjectExpectation,
    upload_caller_identity: Mapping[str, object],
    value: Mapping[str, object],
) -> None:
    if set(value) != {
        "attachmentAfter",
        "attachmentBefore",
        "denyProbe",
        "freezePolicyReadback",
        "groupMembershipReadback",
        "groupName",
    }:
        raise ValueError
    if value.get("groupName") != "mr-lister-developers":
        raise ValueError
    before = _mapping(value, "attachmentBefore")
    after = _mapping(value, "attachmentAfter")
    before_policies = _validate_attachment_readback(expectation, before)
    after_policies = _validate_attachment_readback(expectation, after)
    upload = (expectation.upload_policy_arn, expectation.upload_policy_name)
    readback = (expectation.readback_policy_arn, expectation.readback_policy_name)
    freeze = (expectation.freeze_policy_arn, expectation.freeze_policy_name)
    if (
        upload not in before_policies
        or upload in after_policies
        or readback not in before_policies
        or readback not in after_policies
        or freeze in before_policies
        or freeze not in after_policies
        or before_policies - {upload} | {freeze} != after_policies
    ):
        raise ValueError
    _validate_freeze_policy_readback(
        expectation,
        _mapping(value, "freezePolicyReadback"),
    )
    _validate_group_membership_readback(
        expectation,
        upload_caller_identity,
        _mapping(value, "groupMembershipReadback"),
    )
    deny = _mapping(value, "denyProbe")
    if set(deny) != {"callerIdentity", "capturedAt", "request", "response"}:
        raise ValueError
    _timestamp(deny, "capturedAt")
    if _validate_caller_identity(expectation, _mapping(deny, "callerIdentity")) != dict(
        upload_caller_identity
    ):
        raise ValueError
    _validate_put_request(expectation, _mapping(deny, "request"))
    if _mapping(deny, "response") != {
        "ErrorCode": "AccessDenied",
        "HTTPStatusCode": 403,
    }:
        raise ValueError


def _validate_freeze_policy_readback(
    expectation: Phase6S3ReleaseObjectExpectation,
    value: Mapping[str, object],
) -> None:
    if set(value) != {"capturedAt", "getPolicy", "getPolicyVersion"}:
        raise ValueError
    _timestamp(value, "capturedAt")
    get_policy = _mapping(value, "getPolicy")
    if set(get_policy) != {"request", "response"}:
        raise ValueError
    if _mapping(get_policy, "request") != {"policyArn": expectation.freeze_policy_arn}:
        raise ValueError
    policy_response = _mapping(get_policy, "response")
    if set(policy_response) != {
        "Arn",
        "AttachmentCount",
        "DefaultVersionId",
        "PolicyName",
    }:
        raise ValueError
    version_id = policy_response.get("DefaultVersionId")
    if (
        policy_response.get("Arn") != expectation.freeze_policy_arn
        or policy_response.get("PolicyName") != expectation.freeze_policy_name
        or policy_response.get("AttachmentCount") != 1
        or not isinstance(version_id, str)
        or re.fullmatch(r"v[1-9][0-9]*", version_id) is None
    ):
        raise ValueError
    get_version = _mapping(value, "getPolicyVersion")
    if set(get_version) != {"request", "response"}:
        raise ValueError
    if _mapping(get_version, "request") != {
        "policyArn": expectation.freeze_policy_arn,
        "versionId": version_id,
    }:
        raise ValueError
    if _mapping(get_version, "response") != {
        "Document": {
            "Statement": [
                {
                    "Action": [
                        "s3:DeleteObject",
                        "s3:DeleteObjectVersion",
                        "s3:PutObject",
                    ],
                    "Effect": "Deny",
                    "Resource": expectation.object_arn,
                    "Sid": "FreezeExactPhase6ReleaseObject",
                }
            ],
            "Version": "2012-10-17",
        },
        "IsDefaultVersion": True,
        "VersionId": version_id,
    }:
        raise ValueError


def _validate_group_membership_readback(
    expectation: Phase6S3ReleaseObjectExpectation,
    upload_caller_identity: Mapping[str, object],
    value: Mapping[str, object],
) -> None:
    if set(value) != {"capturedAt", "request", "response"}:
        raise ValueError
    _timestamp(value, "capturedAt")
    if _mapping(value, "request") != {"groupName": "mr-lister-developers"}:
        raise ValueError
    response = _mapping(value, "response")
    if set(response) != {"Group", "IsTruncated", "Users"}:
        raise ValueError
    if (
        _mapping(response, "Group")
        != {
            "Arn": f"arn:aws:iam::{expectation.account_id}:group/mr-lister-developers",
            "GroupName": "mr-lister-developers",
        }
        or response.get("IsTruncated") is not False
    ):
        raise ValueError
    users = response.get("Users")
    if not isinstance(users, list) or not users:
        raise ValueError
    normalized: set[tuple[str, str, str]] = set()
    for user in users:
        if not isinstance(user, Mapping) or set(user) != {"Arn", "UserId", "UserName"}:
            raise ValueError
        arn = user.get("Arn")
        user_id = user.get("UserId")
        user_name = user.get("UserName")
        if (
            not isinstance(arn, str)
            or not isinstance(user_id, str)
            or not isinstance(user_name, str)
            or re.fullmatch(
                rf"arn:aws:iam::{expectation.account_id}:user/(?:[A-Za-z0-9+=,.@_-]+/)*"
                r"[A-Za-z0-9+=,.@_-]+",
                arn,
            )
            is None
            or not 3 <= len(user_id) <= 256
            or user_id != user_id.strip()
            or any(ord(character) < 0x21 or ord(character) > 0x7E for character in user_id)
            or not 1 <= len(user_name) <= 64
            or arn.rsplit("/", 1)[-1] != user_name
            or _PLACEHOLDER.search(f"{arn}\n{user_id}\n{user_name}")
        ):
            raise ValueError
        normalized.add((arn, user_id, user_name))
    if (
        len(normalized) != len(users)
        or (
            upload_caller_identity["Arn"],
            upload_caller_identity["UserId"],
            "mr-lister-dev",
        )
        not in normalized
    ):
        raise ValueError


def _validate_attachment_readback(
    expectation: Phase6S3ReleaseObjectExpectation,
    value: Mapping[str, object],
) -> set[tuple[str, str]]:
    if set(value) != {"capturedAt", "request", "response"}:
        raise ValueError
    _timestamp(value, "capturedAt")
    if _mapping(value, "request") != {"groupName": "mr-lister-developers"}:
        raise ValueError
    response = _mapping(value, "response")
    if set(response) != {"AttachedPolicies", "IsTruncated"}:
        raise ValueError
    policies = response.get("AttachedPolicies")
    if response.get("IsTruncated") is not False or not isinstance(policies, list):
        raise ValueError
    normalized: set[tuple[str, str]] = set()
    for policy in policies:
        if not isinstance(policy, Mapping) or set(policy) != {"PolicyArn", "PolicyName"}:
            raise ValueError
        arn = policy.get("PolicyArn")
        name = policy.get("PolicyName")
        arn_match = (
            re.fullmatch(
                rf"arn:aws:iam::(?:{expectation.account_id}|aws):policy/(?P<path>[^\r\n]+)",
                arn,
            )
            if isinstance(arn, str)
            else None
        )
        if (
            arn_match is None
            or not isinstance(name, str)
            or arn_match.group("path").rsplit("/", 1)[-1] != name
            or _PLACEHOLDER.search(f"{arn}\n{name}")
        ):
            raise ValueError
        normalized.add((arn, name))
    if len(normalized) != len(policies):
        raise ValueError
    return normalized


def _validate_put_request(
    expectation: Phase6S3ReleaseObjectExpectation,
    value: Mapping[str, object],
) -> None:
    if value != {
        "bucket": expectation.bucket,
        "checksumAlgorithm": "SHA256",
        "checksumSHA256": expectation.checksum_sha256_base64,
        "expectedBucketOwner": expectation.account_id,
        "ifNoneMatch": "*",
        "key": expectation.key,
        "metadata": expectation.metadata,
        "operation": "PutObject",
        "serverSideEncryption": "AES256",
    }:
        raise ValueError


def _validate_bucket_request(
    expectation: Phase6S3ReleaseObjectExpectation,
    value: Mapping[str, object],
) -> None:
    if value != {
        "bucket": expectation.bucket,
        "expectedBucketOwner": expectation.account_id,
    }:
        raise ValueError


def _validate_caller_identity(
    expectation: Phase6S3ReleaseObjectExpectation,
    value: Mapping[str, object],
) -> dict[str, object]:
    if set(value) != {"Account", "Arn", "UserId"}:
        raise ValueError
    arn = value.get("Arn")
    user_id = value.get("UserId")
    if (
        value.get("Account") != expectation.account_id
        or arn != f"arn:aws:iam::{expectation.account_id}:user/mr-lister-dev"
        or not isinstance(user_id, str)
        or not 3 <= len(user_id) <= 256
        or user_id != user_id.strip()
        or any(ord(character) < 0x21 or ord(character) > 0x7E for character in user_id)
        or _PLACEHOLDER.search(f"{arn}\n{user_id}")
    ):
        raise ValueError
    return dict(value)


def _timestamp(value: Mapping[str, object], field: str) -> datetime:
    raw = value.get(field)
    if not isinstance(raw, str) or _TIMESTAMP.fullmatch(raw) is None:
        raise ValueError
    parsed = datetime.strptime(raw, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    if parsed.year < 2020:
        raise ValueError
    return parsed


def _valid_aws_timestamp(value: str) -> bool:
    if _AWS_TIMESTAMP.fullmatch(value) is None:
        return False
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return False
    return parsed.tzinfo is not None and parsed.utcoffset() == UTC.utcoffset(parsed)


def _mapping(value: Mapping[str, object], field: str) -> Mapping[str, object]:
    nested = value.get(field)
    if not isinstance(nested, Mapping):
        raise ValueError
    return nested


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError
        result[key] = value
    return result


__all__ = [
    "EVIDENCE_FORMAT",
    "MANUAL_ROOT_LAMBDA_EVIDENCE_FORMAT",
    "Phase6ReleaseComponent",
    "Phase6S3ReleaseObjectEvidenceError",
    "Phase6S3ReleaseObjectExpectation",
    "VerifiedPhase6S3ReleaseObject",
    "canonical_phase6_s3_evidence",
    "validate_phase6_s3_version_id",
    "verify_phase6_lambda_release_object_evidence",
    "verify_phase6_s3_release_object_evidence",
]
