from __future__ import annotations

import base64
import copy
from pathlib import Path

import pytest

from tools.verify_phase6_s3_release_object import (
    EVIDENCE_FORMAT,
    Phase6S3ReleaseObjectEvidenceError,
    Phase6S3ReleaseObjectExpectation,
    canonical_phase6_s3_evidence,
    validate_phase6_s3_version_id,
    verify_phase6_s3_release_object_evidence,
)

ACCOUNT = "123456789012"
RELEASE = "a" * 64
ARCHIVE_SHA = "b" * 64
VERSION_ID = "3HL4kqtJlcpXroDTDmJ+rmSpXd3dIbrHY+MTRCxf3vj="
SIZE = 96_306_014
ETAG = '"0123456789abcdef0123456789abcdef"'
OWNER_ID = "1" * 64
CALLER = {
    "Account": ACCOUNT,
    "Arn": f"arn:aws:iam::{ACCOUNT}:user/mr-lister-dev",
    "UserId": "AIDATESTMRLISTERDEV",
}


def _expectation(**overrides: object) -> Phase6S3ReleaseObjectExpectation:
    values: dict[str, object] = {
        "account_id": ACCOUNT,
        "region": "us-west-2",
        "environment": "dev",
        "component": "agentcore",
        "release_fingerprint": RELEASE,
        "archive_sha256": ARCHIVE_SHA,
        "size_bytes": SIZE,
    }
    values.update(overrides)
    return Phase6S3ReleaseObjectExpectation(**values)  # type: ignore[arg-type]


def _evidence(expectation: Phase6S3ReleaseObjectExpectation | None = None) -> dict:
    expected = expectation or _expectation()
    put_request = {
        "bucket": expected.bucket,
        "checksumAlgorithm": "SHA256",
        "checksumSHA256": expected.checksum_sha256_base64,
        "expectedBucketOwner": expected.account_id,
        "ifNoneMatch": "*",
        "key": expected.key,
        "metadata": expected.metadata,
        "operation": "PutObject",
        "serverSideEncryption": "AES256",
    }
    head = {
        "request": {
            "bucket": expected.bucket,
            "checksumMode": "ENABLED",
            "expectedBucketOwner": expected.account_id,
            "key": expected.key,
            "versionId": VERSION_ID,
        },
        "response": {
            "ChecksumSHA256": expected.checksum_sha256_base64,
            "ChecksumType": "FULL_OBJECT",
            "ContentLength": expected.size_bytes,
            "ETag": ETAG,
            "LastModified": "2026-08-24T00:00:02+00:00",
            "Metadata": expected.metadata,
            "ServerSideEncryption": "AES256",
            "VersionId": VERSION_ID,
        },
    }
    listing = {
        "request": {
            "bucket": expected.bucket,
            "expectedBucketOwner": expected.account_id,
            "prefix": expected.key,
        },
        "response": {
            "DeleteMarkers": [],
            "IsTruncated": False,
            "Name": expected.bucket,
            "Prefix": expected.key,
            "Versions": [
                {
                    "ChecksumAlgorithm": ["SHA256"],
                    "ChecksumType": "FULL_OBJECT",
                    "ETag": ETAG,
                    "IsLatest": True,
                    "Key": expected.key,
                    "LastModified": "2026-08-24T00:00:02+00:00",
                    "Owner": {"ID": OWNER_ID},
                    "Size": expected.size_bytes,
                    "StorageClass": "STANDARD",
                    "VersionId": VERSION_ID,
                }
            ],
        },
    }
    unrelated = {
        "PolicyArn": "arn:aws:iam::aws:policy/ReadOnlyAccess",
        "PolicyName": "ReadOnlyAccess",
    }
    upload = {
        "PolicyArn": expected.upload_policy_arn,
        "PolicyName": expected.upload_policy_name,
    }
    readback = {
        "PolicyArn": expected.readback_policy_arn,
        "PolicyName": expected.readback_policy_name,
    }
    freeze = {
        "PolicyArn": expected.freeze_policy_arn,
        "PolicyName": expected.freeze_policy_name,
    }
    return {
        "accountId": expected.account_id,
        "bucket": expected.bucket,
        "bucketStateBeforePut": {
            "capturedAt": "2026-08-24T00:00:00Z",
            "ownershipControls": {
                "request": {
                    "bucket": expected.bucket,
                    "expectedBucketOwner": expected.account_id,
                },
                "response": {
                    "OwnershipControls": {"Rules": [{"ObjectOwnership": "BucketOwnerEnforced"}]}
                },
            },
            "versioning": {
                "request": {
                    "bucket": expected.bucket,
                    "expectedBucketOwner": expected.account_id,
                },
                "response": {"Status": "Enabled"},
            },
        },
        "component": expected.component,
        "format": EVIDENCE_FORMAT,
        "key": expected.key,
        "postRevocationReadback": {
            "capturedAt": "2026-08-24T00:00:09Z",
            "headObject": copy.deepcopy(head),
            "listObjectVersions": copy.deepcopy(listing),
        },
        "preRevocationReadback": {
            "capturedAt": "2026-08-24T00:00:03Z",
            "headObject": head,
            "listObjectVersions": listing,
        },
        "putObject": {
            "callerIdentity": CALLER,
            "capturedAt": "2026-08-24T00:00:01Z",
            "request": put_request,
            "response": {
                "ChecksumSHA256": expected.checksum_sha256_base64,
                "ChecksumType": "FULL_OBJECT",
                "ETag": ETAG,
                "ServerSideEncryption": "AES256",
                "VersionId": VERSION_ID,
            },
        },
        "region": expected.region,
        "releaseFingerprint": expected.release_fingerprint,
        "uploadAuthorityRevocation": {
            "attachmentAfter": {
                "capturedAt": "2026-08-24T00:00:05Z",
                "request": {"groupName": "mr-lister-developers"},
                "response": {
                    "AttachedPolicies": [unrelated, readback, freeze],
                    "IsTruncated": False,
                },
            },
            "attachmentBefore": {
                "capturedAt": "2026-08-24T00:00:04Z",
                "request": {"groupName": "mr-lister-developers"},
                "response": {
                    "AttachedPolicies": [unrelated, upload, readback],
                    "IsTruncated": False,
                },
            },
            "denyProbe": {
                "callerIdentity": CALLER,
                "capturedAt": "2026-08-24T00:00:08Z",
                "request": copy.deepcopy(put_request),
                "response": {"ErrorCode": "AccessDenied", "HTTPStatusCode": 403},
            },
            "freezePolicyReadback": {
                "capturedAt": "2026-08-24T00:00:06Z",
                "getPolicy": {
                    "request": {"policyArn": expected.freeze_policy_arn},
                    "response": {
                        "Arn": expected.freeze_policy_arn,
                        "AttachmentCount": 1,
                        "DefaultVersionId": "v1",
                        "PolicyName": expected.freeze_policy_name,
                    },
                },
                "getPolicyVersion": {
                    "request": {
                        "policyArn": expected.freeze_policy_arn,
                        "versionId": "v1",
                    },
                    "response": {
                        "Document": {
                            "Statement": [
                                {
                                    "Action": [
                                        "s3:DeleteObject",
                                        "s3:DeleteObjectVersion",
                                        "s3:PutObject",
                                    ],
                                    "Effect": "Deny",
                                    "Resource": expected.object_arn,
                                    "Sid": "FreezeExactPhase6ReleaseObject",
                                }
                            ],
                            "Version": "2012-10-17",
                        },
                        "IsDefaultVersion": True,
                        "VersionId": "v1",
                    },
                },
            },
            "groupMembershipReadback": {
                "capturedAt": "2026-08-24T00:00:07Z",
                "request": {"groupName": "mr-lister-developers"},
                "response": {
                    "Group": {
                        "Arn": f"arn:aws:iam::{ACCOUNT}:group/mr-lister-developers",
                        "GroupName": "mr-lister-developers",
                    },
                    "IsTruncated": False,
                    "Users": [
                        {
                            "Arn": CALLER["Arn"],
                            "UserId": CALLER["UserId"],
                            "UserName": "mr-lister-dev",
                        }
                    ],
                },
            },
            "groupName": "mr-lister-developers",
        },
        "versionId": VERSION_ID,
    }


def _write_evidence(tmp_path: Path, document: object, *, canonical: bool = True) -> Path:
    path = tmp_path / "object-binding-evidence.json"
    if canonical:
        path.write_bytes(canonical_phase6_s3_evidence(document))
    else:
        path.write_text("{}", encoding="utf-8")
    return path


def test_expectation_derives_content_addressed_identity_checksum_and_metadata() -> None:
    expected = _expectation()
    assert expected.bucket == f"mr-lister-phase6-artifacts-dev-{ACCOUNT}-us-west-2"
    assert expected.key == (
        f"private/deployments/agentcore/releases/{RELEASE}/phase6-agentcore-{ARCHIVE_SHA}.zip"
    )
    assert expected.archive_filename == "phase6-agentcore.zip"
    assert expected.checksum_sha256_base64 == base64.b64encode(bytes.fromhex(ARCHIVE_SHA)).decode(
        "ascii"
    )
    assert expected.metadata == {
        "mr-lister-archive-sha256": ARCHIVE_SHA,
        "mr-lister-component": "agentcore",
        "mr-lister-release-fingerprint": RELEASE,
        "mr-lister-size-bytes": str(SIZE),
    }
    assert expected.upload_policy_arn == (
        f"arn:aws:iam::{ACCOUNT}:policy/mr-lister-phase6-agentcore-direct-uploader-dev"
    )
    assert expected.readback_policy_arn.endswith(
        ":policy/mr-lister-phase6-agentcore-direct-evidence-reader-dev"
    )
    assert expected.freeze_policy_arn.endswith(
        ":policy/mr-lister-phase6-agentcore-release-freeze-dev"
    )
    assert expected.object_arn == f"arn:aws:s3:::{expected.bucket}/{expected.key}"


def test_lambda_expectation_uses_separate_component_key() -> None:
    expected = _expectation(component="lambda", size_bytes=62_692_151)
    assert expected.archive_filename == "phase6-lambda.zip"
    assert expected.key == (
        f"private/deployments/lambda/releases/{RELEASE}/phase6-lambda-{ARCHIVE_SHA}.zip"
    )
    assert expected.metadata["mr-lister-component"] == "lambda"


def test_closed_evidence_returns_exact_immutable_binding(tmp_path: Path) -> None:
    expected = _expectation()
    path = _write_evidence(tmp_path, _evidence(expected))
    binding = verify_phase6_s3_release_object_evidence(expected, evidence_path=path)
    assert binding.account_id == ACCOUNT
    assert binding.region == "us-west-2"
    assert binding.environment == "dev"
    assert binding.component == "agentcore"
    assert binding.release_fingerprint == RELEASE
    assert binding.archive_sha256 == ARCHIVE_SHA
    assert binding.size_bytes == SIZE
    assert binding.checksum_sha256_base64 == expected.checksum_sha256_base64
    assert binding.bucket == expected.bucket
    assert binding.key == expected.key
    assert binding.version_id == VERSION_ID
    assert len(binding.evidence_sha256) == 64


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("account_id", "000000000000"),
        ("account_id", "123"),
        ("region", "us-east-1"),
        ("environment", "prod"),
        ("component", "other"),
        ("release_fingerprint", "0" * 64),
        ("archive_sha256", "B" * 64),
        ("size_bytes", 0),
        ("size_bytes", True),
    ),
)
def test_invalid_expectations_are_rejected(field: str, value: object) -> None:
    with pytest.raises(Phase6S3ReleaseObjectEvidenceError):
        _expectation(**{field: value})


@pytest.mark.parametrize(
    "version_id",
    (
        "",
        "ab",
        "null",
        "latest",
        "DEFAULT",
        "<VERSION_ID>",
        " abc ",
        "abc\ndef",
        'abc"def',
        "a" * 1025,
    ),
)
def test_version_id_must_be_nonmoving_nonnull_literal(version_id: str) -> None:
    with pytest.raises(Phase6S3ReleaseObjectEvidenceError):
        validate_phase6_s3_version_id(version_id)


def test_noncanonical_or_symlinked_evidence_is_rejected(tmp_path: Path) -> None:
    expected = _expectation()
    noncanonical = _write_evidence(tmp_path, _evidence(), canonical=False)
    with pytest.raises(Phase6S3ReleaseObjectEvidenceError):
        verify_phase6_s3_release_object_evidence(expected, evidence_path=noncanonical)
    target = tmp_path / "target.json"
    target.write_bytes(canonical_phase6_s3_evidence(_evidence()))
    symlink = tmp_path / "symlink.json"
    symlink.symlink_to(target)
    with pytest.raises(Phase6S3ReleaseObjectEvidenceError):
        verify_phase6_s3_release_object_evidence(expected, evidence_path=symlink)

    real_root = tmp_path / "real-root"
    nested = real_root / "nested"
    nested.mkdir(parents=True)
    grandparent_target = nested / "evidence.json"
    grandparent_target.write_bytes(canonical_phase6_s3_evidence(_evidence()))
    aliased_root = tmp_path / "aliased-root"
    aliased_root.symlink_to(real_root, target_is_directory=True)
    through_symlinked_grandparent = aliased_root / "nested/evidence.json"
    assert not through_symlinked_grandparent.is_symlink()
    assert not through_symlinked_grandparent.parent.is_symlink()
    with pytest.raises(Phase6S3ReleaseObjectEvidenceError):
        verify_phase6_s3_release_object_evidence(
            expected,
            evidence_path=through_symlinked_grandparent,
        )


@pytest.mark.parametrize(
    ("path", "value"),
    (
        (("accountId",), "999999999999"),
        (("region",), "us-east-1"),
        (("component",), "lambda"),
        (("releaseFingerprint",), "c" * 64),
        (("bucket",), "other-bucket"),
        (("key",), "moving.zip"),
        (("versionId",), "different-version"),
        (("bucketStateBeforePut", "versioning", "response", "Status"), "Suspended"),
        (
            (
                "bucketStateBeforePut",
                "ownershipControls",
                "response",
                "OwnershipControls",
                "Rules",
                0,
                "ObjectOwnership",
            ),
            "ObjectWriter",
        ),
        (("putObject", "request", "ifNoneMatch"), "moving"),
        (
            ("putObject", "callerIdentity", "Arn"),
            f"arn:aws:sts::{ACCOUNT}:assumed-role/mr-lister-developer/test-session",
        ),
        (("putObject", "request", "checksumSHA256"), "wrong"),
        (("putObject", "request", "serverSideEncryption"), "aws:kms"),
        (("putObject", "response", "ChecksumSHA256"), "wrong"),
        (("putObject", "response", "ChecksumType"), "COMPOSITE"),
        (("putObject", "response", "VersionId"), "different-version"),
        (
            ("preRevocationReadback", "headObject", "response", "ContentLength"),
            SIZE + 1,
        ),
        (
            ("preRevocationReadback", "headObject", "response", "ChecksumSHA256"),
            "wrong",
        ),
        (
            ("preRevocationReadback", "headObject", "response", "Metadata"),
            {},
        ),
        (
            (
                "preRevocationReadback",
                "listObjectVersions",
                "response",
                "IsTruncated",
            ),
            True,
        ),
        (
            (
                "preRevocationReadback",
                "listObjectVersions",
                "response",
                "DeleteMarkers",
            ),
            [{"VersionId": "deleted"}],
        ),
        (
            (
                "preRevocationReadback",
                "listObjectVersions",
                "response",
                "Versions",
                0,
                "IsLatest",
            ),
            False,
        ),
        (
            (
                "preRevocationReadback",
                "listObjectVersions",
                "response",
                "Versions",
                0,
                "Owner",
                "ID",
            ),
            "wrong-owner",
        ),
        (
            (
                "uploadAuthorityRevocation",
                "attachmentAfter",
                "response",
                "IsTruncated",
            ),
            True,
        ),
        (
            (
                "uploadAuthorityRevocation",
                "freezePolicyReadback",
                "getPolicyVersion",
                "response",
                "Document",
                "Statement",
                0,
                "Action",
            ),
            ["s3:PutObject"],
        ),
        (
            ("uploadAuthorityRevocation", "denyProbe", "response", "ErrorCode"),
            "PreconditionFailed",
        ),
        (
            ("uploadAuthorityRevocation", "denyProbe", "response", "HTTPStatusCode"),
            412,
        ),
        (
            (
                "uploadAuthorityRevocation",
                "groupMembershipReadback",
                "response",
                "Users",
                0,
                "UserId",
            ),
            "OTHERUSERID",
        ),
        (
            ("postRevocationReadback", "headObject", "response", "ChecksumSHA256"),
            "wrong",
        ),
    ),
)
def test_every_remote_identity_checksum_metadata_and_singleton_field_is_exact(
    tmp_path: Path,
    path: tuple[object, ...],
    value: object,
) -> None:
    document = copy.deepcopy(_evidence())
    target: object = document
    for part in path[:-1]:
        target = target[part]  # type: ignore[index]
    target[path[-1]] = value  # type: ignore[index]
    evidence_path = _write_evidence(tmp_path, document)
    with pytest.raises(Phase6S3ReleaseObjectEvidenceError):
        verify_phase6_s3_release_object_evidence(
            _expectation(),
            evidence_path=evidence_path,
        )


def test_multiple_versions_extra_fields_and_missing_fields_are_rejected(tmp_path: Path) -> None:
    for mutate in ("multiple", "extra", "missing"):
        document = copy.deepcopy(_evidence())
        listing = document["preRevocationReadback"]["listObjectVersions"]["response"]
        if mutate == "multiple":
            listing["Versions"].append(copy.deepcopy(listing["Versions"][0]))
        elif mutate == "extra":
            document["putObject"]["response"]["Size"] = SIZE
        else:
            del listing["DeleteMarkers"]
        path = tmp_path / f"{mutate}.json"
        path.write_bytes(canonical_phase6_s3_evidence(document))
        with pytest.raises(Phase6S3ReleaseObjectEvidenceError):
            verify_phase6_s3_release_object_evidence(
                _expectation(),
                evidence_path=path,
            )


def test_capture_order_and_aws_timestamp_shape_are_fail_closed(tmp_path: Path) -> None:
    for name, mutate in (
        (
            "out-of-order",
            lambda document: document["postRevocationReadback"].__setitem__(
                "capturedAt", "2026-08-24T00:00:02Z"
            ),
        ),
        (
            "arbitrary-last-modified",
            lambda document: document["preRevocationReadback"]["headObject"][
                "response"
            ].__setitem__("LastModified", "not-a-timestamp"),
        ),
    ):
        document = copy.deepcopy(_evidence())
        mutate(document)
        path = tmp_path / f"{name}.json"
        path.write_bytes(canonical_phase6_s3_evidence(document))
        with pytest.raises(Phase6S3ReleaseObjectEvidenceError):
            verify_phase6_s3_release_object_evidence(
                _expectation(),
                evidence_path=path,
            )


def test_upload_and_delete_freeze_is_live_attached_and_unrelated_policy_is_unchanged(
    tmp_path: Path,
) -> None:
    document = _evidence()
    revocation = document["uploadAuthorityRevocation"]
    before = revocation["attachmentBefore"]["response"]["AttachedPolicies"]
    after = revocation["attachmentAfter"]["response"]["AttachedPolicies"]
    assert {policy["PolicyArn"] for policy in before} - {_expectation().upload_policy_arn} | {
        _expectation().freeze_policy_arn
    } == {policy["PolicyArn"] for policy in after}
    statement = revocation["freezePolicyReadback"]["getPolicyVersion"]["response"]["Document"][
        "Statement"
    ][0]
    assert statement["Effect"] == "Deny"
    assert set(statement["Action"]) == {
        "s3:DeleteObject",
        "s3:DeleteObjectVersion",
        "s3:PutObject",
    }
    path = _write_evidence(tmp_path, document)
    verify_phase6_s3_release_object_evidence(_expectation(), evidence_path=path)
