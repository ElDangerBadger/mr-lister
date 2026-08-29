#!/usr/bin/env python3
"""Fail-closed runner for the deployed Phase 6.6 upload-integrity smoke.

The default invocation is a local-only gate/canary preflight.  Live execution is possible only
with an exact private run gate, its caller-supplied SHA-256, an explicit environment switch, and
an explicitly supplied repository-private output directory.  The live path invokes the deployed
upload and review Lambdas directly with a synthetic API Gateway authorizer context; it never reads
browser cookies, tokens, storage, or profiles.

No raw subject, owner, job identifier, S3 coordinate, version identifier, presigned URL/form,
credential, or local path is serialized or printed.  A live discrepancy raises only a closed
error message.  The only cleanup mutation deletes the VersionId returned by this process's exact
temporary overwrite, after that version has been independently proven.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import secrets
import stat
import time
import urllib.error
import urllib.request
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Final, Protocol
from urllib.parse import parse_qs, urlsplit

from tools.phase66_live_acceptance import exact_phase66_canary_png

REPOSITORY_ROOT: Final = Path(__file__).resolve().parents[1]
PRIVATE_ROOT: Final = REPOSITORY_ROOT / ".mr_lister_private" / "phase66-acceptance"
OVERWRITE_CANARY: Final = (
    REPOSITORY_ROOT / "tests" / "evaluation" / "assets" / "holdout_transparent_jellyfish.png"
)

GATE_ID: Final = "deployed.upload_integrity_smoke"
GATE_CONTRACT: Final = "phase6.6-deployed-upload-integrity-run-gate-v1"
SOURCE_AUTHORITY_COMMIT: Final = "e130292db7124425840c2768a94475417f94f2e5"
SOURCE_AUTHORITY_COMMIT_DIGEST: Final = (
    "40e7186ae67d9f6cd7ae630381ff8ed59c09afde0e2022d4b0a3ecbced2277cd"
)
PRIMARY_SIZE: Final = 5 * 1024 * 1024
PRIMARY_SHA256: Final = "d32bfa718ba9073db3da4e9aefb995212e46215d880e17b1dedc241f496691cc"
WRONG_SHA256: Final = "8bc2aa2e193cab8956f8626e04e76c80cb08744fd3deb87b26a376212f6b19a2"
OVERWRITE_SIZE: Final = 10_702
OVERWRITE_SHA256: Final = "12d15003d1bb881397a278592be424b4160356db2baa67f5b435df9e89a64a8e"
WRONG_OFFSET: Final = 1_048_576
REGION: Final = "us-west-2"
STACK_NAME: Final = "mr-lister-phase6-dev"
PROFILE: Final = "mr-lister-bootstrap"
ACCOUNT_ID: Final = "384627057108"
LIVE_ENVIRONMENT_SWITCH: Final = "MR_LISTER_RUN_DEPLOYED_UPLOAD_INTEGRITY_SMOKE"
LIVE_ENVIRONMENT_VALUE: Final = "I_ACCEPT_THE_EXACT_PRIVATE_GATE"
MAX_GATE_BYTES: Final = 1024 * 1024
MAX_HTTP_RESPONSE_BYTES: Final = 64 * 1024
EXPIRY_SKEW_SECONDS: Final = 5

_EXPECTED_BUDGET: Final = {
    "agentcore_invocations": 0,
    "bedrock_invocations": 0,
    "cancel_upload_requests": 0,
    "complete_upload_requests": 0,
    "create_upload_requests": 1,
    "dynamodb_item_writes": 2,
    "dynamodb_new_items": 2,
    "dynamodb_transactions": 1,
    "new_jobs": 0,
    "new_work_requests": 0,
    "provider_calls": 0,
    "provider_records": 0,
    "reauthorize_upload_requests": 0,
    "s3_negative_post_attempts": 3,
    "s3_negative_post_persisted_versions": 0,
    "s3_temporary_exact_version_deletes": 1,
    "s3_temporary_overwrite_puts": 1,
    "s3_total_new_version_ceiling": 1,
    "s3_version_net_delta_after_cleanup": 0,
    "stepfunctions_executions": 0,
}
_EXPECTED_METHOD_AUTHORIZATION: Final = {
    "browser_authority_not_used": True,
    "direct_upload_lambda_invocations": 1,
    "direct_review_lambda_invocations": 2,
    "ephemeral_cognito_list_users": True,
    "ephemeral_cognito_group_read": True,
    "raw_identity_retained": False,
}
_PROTECTED_ENTITY_TYPES: Final = frozenset(
    {
        "COMMAND_RECEIPT",
        "CONTROL_JOB",
        "DOMAIN_EVENT",
        "EXTERNAL_WRITE_CLAIM",
        "REVIEW",
        "SOURCE_ARTIFACT",
        "WORK_REQUEST",
    }
)
_EXPECTED_LAMBDA_HANDLERS: Final = {
    "UploadApiFunction": "phase6_lambda.upload_api_handler",
    "ReviewQueryApiFunction": "phase6_lambda.review_query_api_handler",
}
_EXPECTED_LAMBDA_CODE_SHA256: Final = {
    "UploadApiFunction": "uvFStzLOhXS2ppJbrnq0/4ScG4PUE3B2xSxmglU/nUg=",
    "ReviewQueryApiFunction": "EilYwd9+2RbeEiypXFz5uKNMOFpFtwbzltKQfCnLj5w=",
}
_EXPECTED_LAMBDA_RELEASE_FINGERPRINT: Final = {
    "UploadApiFunction": "0c6211a5b0244e9c86d635e6c02e7bc49e5e948d68895b4aaa982c0b0b2e187b",
    "ReviewQueryApiFunction": "6e32d16ce16371a65815e2931e0a897a34bbbce5526300438d4fc29061813571",
}


class SmokeError(RuntimeError):
    """One closed precondition, observation, or cleanup assertion failed."""


def _canonical_json(value: object, *, pretty: bool = False) -> bytes:
    try:
        return (
            json.dumps(
                value,
                sort_keys=True,
                indent=2 if pretty else None,
                separators=None if pretty else (",", ":"),
                ensure_ascii=True,
                allow_nan=False,
            )
            + ("\n" if pretty else "")
        ).encode("utf-8")
    except (OverflowError, TypeError, ValueError):
        raise SmokeError("strict JSON serialization failed") from None


def _digest_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _digest_text(value: str) -> str:
    return _digest_bytes(value.encode("utf-8"))


def _digest_json(value: object) -> str:
    return _digest_bytes(_canonical_json(value))


def _expected_lambda_environment(
    outputs: Mapping[str, str], release_fingerprint: str
) -> dict[str, str]:
    return {
        "MR_LISTER_AWS_ACCOUNT_ID": ACCOUNT_ID,
        "MR_LISTER_ENVIRONMENT": "dev",
        "MR_LISTER_STATE_TABLE": outputs["StateTableName"],
        "MR_LISTER_ARTIFACT_BUCKET": outputs["ArtifactBucketName"],
        "MR_LISTER_ARTIFACT_BUCKET_OWNER_ACCOUNT_ID": ACCOUNT_ID,
        "MR_LISTER_ARTIFACT_ORIGIN": outputs["ArtifactBucketBrowserOrigin"],
        "MR_LISTER_COGNITO_ISSUER": (
            f"https://cognito-idp.{REGION}.amazonaws.com/{outputs['SellerUserPoolId']}"
        ),
        "MR_LISTER_COGNITO_CLIENT_ID": outputs["SellerUserPoolClientId"],
        "MR_LISTER_COGNITO_SCOPE": "mr-lister-api/seller",
        "MR_LISTER_COGNITO_GROUP": "seller",
        "MR_LISTER_APPLICATION_ORIGIN": outputs["SellerApplicationOrigin"],
        "MR_LISTER_PHASE6_SCAFFOLD_ONLY": "false",
        "MR_LISTER_PRODUCT_PROFILE_ID": "gildan_64000_swiftpod",
        "MR_LISTER_PRODUCT_PROFILE_VERSION": "2",
        "MR_LISTER_PRODUCT_PROFILE_FINGERPRINT": (
            "5de1257141cfdacb1731f68bb9113712957483b33d5b0f7115afdba86eb7476c"
        ),
        "MR_LISTER_PRODUCT_PROFILE_PATH": (
            "/var/task/config/product_profiles/gildan_64000_swiftpod.json"
        ),
        "MR_LISTER_RELEASE_FINGERPRINT": release_fingerprint,
    }


def _mapping(value: object, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or any(not isinstance(key, str) for key in value):
        raise SmokeError(f"{label} is not an exact JSON object")
    return value


def _exact_private_path(path: Path) -> Path:
    candidate = Path(os.path.abspath(path))
    try:
        candidate.relative_to(PRIVATE_ROOT)
    except ValueError:
        raise SmokeError(
            "private inputs and outputs must remain in the repository workspace"
        ) from None
    return candidate


def _validate_private_directory(path: Path, *, create: bool) -> Path:
    directory = _exact_private_path(path)
    current = REPOSITORY_ROOT
    for component in directory.relative_to(REPOSITORY_ROOT).parts:
        current /= component
        try:
            metadata = current.lstat()
        except FileNotFoundError:
            if not create:
                raise SmokeError("private directory is unavailable") from None
            try:
                current.mkdir(mode=0o700)
                metadata = current.lstat()
            except OSError:
                raise SmokeError("private directory could not be created") from None
        if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
            raise SmokeError("private path contains a non-directory component")
        if metadata.st_mode & 0o077:
            if not create:
                raise SmokeError("private directory permissions are not confined")
            try:
                current.chmod(0o700)
            except OSError:
                raise SmokeError("private directory permissions could not be confined") from None
    return directory


def _read_private_file(path: Path, *, max_bytes: int = MAX_GATE_BYTES) -> bytes:
    candidate = _exact_private_path(path)
    _validate_private_directory(candidate.parent, create=False)
    descriptor: int | None = None
    try:
        descriptor = os.open(candidate, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or before.st_mode & 0o077
            or not 1 <= before.st_size <= max_bytes
        ):
            raise OSError
        chunks: list[bytes] = []
        remaining = before.st_size
        while remaining:
            chunk = os.read(descriptor, min(1024 * 1024, remaining))
            if not chunk:
                raise OSError
            chunks.append(chunk)
            remaining -= len(chunk)
        after = os.fstat(descriptor)
    except OSError:
        raise SmokeError("gate must be one stable mode-0600 private regular file") from None
    finally:
        if descriptor is not None:
            os.close(descriptor)
    if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
        after.st_ctime_ns,
    ):
        raise SmokeError("gate changed while it was read")
    return b"".join(chunks)


def _reject_constant(_value: str) -> None:
    raise ValueError


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError
        result[key] = value
    return result


def _strict_json(payload: bytes, label: str) -> object:
    try:
        return json.loads(
            payload,
            parse_constant=_reject_constant,
            object_pairs_hook=_unique_object,
        )
    except (RecursionError, UnicodeDecodeError, ValueError, json.JSONDecodeError):
        raise SmokeError(f"{label} is not strict JSON") from None


def _atomic_private_json(path: Path, value: object) -> tuple[int, str]:
    candidate = _exact_private_path(path)
    _validate_private_directory(candidate.parent, create=True)
    payload = _canonical_json(value, pretty=True)
    temporary = candidate.with_name(f".{candidate.name}.{secrets.token_hex(12)}.tmp")
    descriptor: int | None = None
    try:
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
            0o600,
        )
        with os.fdopen(descriptor, "wb", closefd=True) as output:
            descriptor = None
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
        temporary.replace(candidate)
        candidate.chmod(0o600)
    except OSError:
        raise SmokeError("sanitized result could not be written") from None
    finally:
        if descriptor is not None:
            os.close(descriptor)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
    return len(payload), _digest_bytes(payload)


@dataclass(frozen=True, slots=True)
class RunGate:
    digest: str
    document: Mapping[str, Any] = field(repr=False)

    @property
    def baseline(self) -> Mapping[str, Any]:
        return _mapping(self.document.get("baseline"), "gate baseline")

    @property
    def deployment_digest(self) -> str:
        value = self.document.get("deployment_digest")
        assert isinstance(value, str)
        return value

    @property
    def prerequisite_digest(self) -> str:
        value = self.document.get("prerequisite_evidence_run_digest")
        assert isinstance(value, str)
        return value


def load_run_gate(path: Path, expected_digest: str) -> RunGate:
    if len(expected_digest) != 64 or any(
        character not in "0123456789abcdef" for character in expected_digest
    ):
        raise SmokeError("gate SHA-256 is invalid")
    payload = _read_private_file(path)
    if not secrets.compare_digest(_digest_bytes(payload), expected_digest):
        raise SmokeError("gate SHA-256 does not match the exact private file")
    document = _mapping(_strict_json(payload, "gate"), "gate")
    if (
        document.get("gate_id") != GATE_ID
        or document.get("authorization_contract") != GATE_CONTRACT
    ):
        raise SmokeError("gate does not authorize this exact smoke")
    if document.get("source_authority_commit") != SOURCE_AUTHORITY_COMMIT:
        raise SmokeError("gate source authority commit is not the deployed code authority")
    if document.get("source_authority_commit_digest") != SOURCE_AUTHORITY_COMMIT_DIGEST:
        raise SmokeError("gate source authority digest is not the deployed code authority")
    for name in ("deployment_digest", "prerequisite_evidence_run_digest"):
        value = document.get(name)
        if (
            not isinstance(value, str)
            or len(value) != 64
            or any(character not in "0123456789abcdef" for character in value)
        ):
            raise SmokeError("gate deployment/prerequisite binding is invalid")
    if _mapping(document.get("alternate_method_authorization"), "method authorization") != (
        _EXPECTED_METHOD_AUTHORIZATION
    ):
        raise SmokeError("gate does not authorize the exact direct-invocation method")
    if _mapping(document.get("exact_write_budget"), "write budget") != _EXPECTED_BUDGET:
        raise SmokeError("gate write budget is not exact")
    canaries = _mapping(document.get("canaries"), "canaries")
    expected_canaries = {
        "primary": {"byte_count": PRIMARY_SIZE, "sha256": PRIMARY_SHA256},
        "wrong_bytes": {
            "byte_count": PRIMARY_SIZE,
            "mutation": "xor_0x01_at_zero_based_file_offset_1048576",
            "sha256": WRONG_SHA256,
        },
        "overwrite": {"byte_count": OVERWRITE_SIZE, "sha256": OVERWRITE_SHA256},
    }
    if canaries != expected_canaries:
        raise SmokeError("gate canary authority is not exact")
    baseline = _mapping(document.get("baseline"), "baseline")
    required_baseline = {
        "actor_digest",
        "bucket_versioning_enabled",
        "existing_job_count",
        "existing_job_set_digest",
        "existing_job_states",
        "provider_record_count",
        "running_execution_count",
        "selected_inventory_count",
        "selected_inventory_digest",
        "selected_job_digest",
        "selected_job_record_digest",
        "selected_object_coordinate_digest",
        "selected_pinned_is_latest",
        "selected_pinned_version_digest",
        "selected_source_authority_digest",
        "selected_source_record_digest",
        "selected_version_head_matches_exact_canary",
        "selected_version_tag_is_pinned",
        "table_record_count",
    }
    if not required_baseline <= set(baseline):
        raise SmokeError("gate baseline is incomplete")
    for key in required_baseline - {
        "bucket_versioning_enabled",
        "existing_job_count",
        "existing_job_states",
        "provider_record_count",
        "running_execution_count",
        "selected_inventory_count",
        "selected_pinned_is_latest",
        "selected_version_head_matches_exact_canary",
        "selected_version_tag_is_pinned",
        "table_record_count",
    }:
        value = baseline.get(key)
        if (
            not isinstance(value, str)
            or len(value) != 64
            or any(character not in "0123456789abcdef" for character in value)
        ):
            raise SmokeError("gate baseline digest is invalid")
    for key in (
        "existing_job_count",
        "provider_record_count",
        "running_execution_count",
        "selected_inventory_count",
        "table_record_count",
    ):
        if type(baseline.get(key)) is not int or baseline[key] < 0:
            raise SmokeError("gate baseline count is invalid")
    for key in (
        "bucket_versioning_enabled",
        "selected_pinned_is_latest",
        "selected_version_head_matches_exact_canary",
        "selected_version_tag_is_pinned",
    ):
        if type(baseline.get(key)) is not bool:
            raise SmokeError("gate baseline proof flag is invalid")
    states = baseline.get("existing_job_states")
    if (
        not isinstance(states, list)
        or len(states) != baseline["existing_job_count"]
        or any(not isinstance(state, str) or not state for state in states)
        or states != sorted(states)
    ):
        raise SmokeError("gate job-state baseline is invalid")
    return RunGate(digest=expected_digest, document=document)


def exact_canaries() -> tuple[bytes, bytes, bytes]:
    primary = exact_phase66_canary_png()
    wrong = bytearray(primary)
    wrong[WRONG_OFFSET] ^= 0x01
    overwrite = OVERWRITE_CANARY.read_bytes()
    if (
        len(primary) != PRIMARY_SIZE
        or _digest_bytes(primary) != PRIMARY_SHA256
        or len(wrong) != PRIMARY_SIZE
        or _digest_bytes(wrong) != WRONG_SHA256
        or len(overwrite) != OVERWRITE_SIZE
        or _digest_bytes(overwrite) != OVERWRITE_SHA256
    ):
        raise SmokeError("local canary bytes do not match frozen authority")
    return primary, bytes(wrong), overwrite


@dataclass(frozen=True, slots=True)
class Authority:
    owner_id: str = field(repr=False)
    subject: str = field(repr=False)
    job_id: str = field(repr=False)
    bucket: str = field(repr=False)
    source_key: str = field(repr=False)
    source_version: str = field(repr=False)
    issuer: str = field(repr=False)
    client_id: str = field(repr=False)
    upload_function: str = field(repr=False)
    review_function: str = field(repr=False)
    table_name: str = field(repr=False)
    state_machine_arns: tuple[str, ...] = field(repr=False)


@dataclass(frozen=True, slots=True)
class InventoryVersion:
    version_id: str = field(repr=False)
    is_latest: bool
    last_modified: str
    size_bytes: int
    etag: str = field(repr=False)

    def sanitized(self) -> dict[str, object]:
        return {
            "kind": "version",
            "version_digest": _digest_text(self.version_id),
            "is_latest": self.is_latest,
            "last_modified": self.last_modified,
            "size_bytes": self.size_bytes,
            "etag_digest": _digest_text(self.etag),
        }


@dataclass(frozen=True, slots=True)
class Snapshot:
    authority: Authority = field(repr=False)
    items: tuple[tuple[str, Mapping[str, Any]], ...] = field(repr=False)
    selected_job: Mapping[str, Any] = field(repr=False)
    selected_source: Mapping[str, Any] = field(repr=False)
    inventory: tuple[InventoryVersion, ...] = field(repr=False)
    execution_digests: tuple[str, ...]

    @property
    def entity_counts(self) -> Counter[str]:
        return Counter(entity_type for entity_type, _payload in self.items)


@dataclass(frozen=True, slots=True)
class UploadGrant:
    upload_id: str = field(repr=False)
    job_id: str = field(repr=False)
    url: str = field(repr=False)
    fields: Mapping[str, str] = field(repr=False)
    key: str = field(repr=False)
    issued_at: datetime
    expires_at: datetime


class LiveBackend(Protocol):
    def prepare(self, gate: RunGate, primary: bytes) -> Snapshot: ...

    def invoke_upload(
        self, authority: Authority, event: Mapping[str, Any]
    ) -> Mapping[str, Any]: ...

    def invoke_review(
        self, authority: Authority, event: Mapping[str, Any]
    ) -> Mapping[str, Any]: ...

    def post_form(
        self, url: str, fields: Mapping[str, str], content: bytes, *, content_type: str
    ) -> int: ...

    def count_exact_versions(self, authority: Authority, key: str) -> int: ...

    def get_preview(self, url: str) -> bytes: ...

    def put_temporary(self, authority: Authority, content: bytes) -> str: ...

    def prove_temporary(self, authority: Authority, version_id: str, content: bytes) -> None: ...

    def delete_temporary(self, authority: Authority, version_id: str) -> None: ...

    def inventory(self, authority: Authority) -> tuple[InventoryVersion, ...]: ...

    def snapshot(self, authority: Authority) -> Snapshot: ...

    def wait_until(self, timestamp: datetime) -> None: ...


def _claims(authority: Authority) -> dict[str, object]:
    return {
        "iss": authority.issuer,
        "sub": authority.subject,
        "token_use": "access",
        "client_id": authority.client_id,
        "scope": "mr-lister-api/seller",
        "cognito:groups": '["seller"]',
    }


def _event(
    authority: Authority,
    route_key: str,
    raw_path: str,
    *,
    body: Mapping[str, object] | None = None,
    path_parameters: Mapping[str, str] | None = None,
    idempotency_key: str | None = None,
) -> dict[str, object]:
    headers: dict[str, str] = {}
    if body is not None:
        headers["Content-Type"] = "application/json"
    if idempotency_key is not None:
        headers["Idempotency-Key"] = idempotency_key
    return {
        "version": "2.0",
        "routeKey": route_key,
        "rawPath": raw_path,
        "rawQueryString": "",
        "queryStringParameters": None,
        "pathParameters": dict(path_parameters) if path_parameters else None,
        "headers": headers,
        "requestContext": {
            "requestId": "phase66-direct-smoke",
            "authorizer": {"jwt": {"claims": _claims(authority)}},
        },
        "body": _canonical_json(body).decode("utf-8") if body is not None else None,
        "isBase64Encoded": False,
    }


def _response_body(response: Mapping[str, Any], expected_status: int) -> Mapping[str, Any]:
    if response.get("statusCode") != expected_status:
        raise SmokeError("deployed Lambda returned an unexpected closed response")
    body = response.get("body")
    if not isinstance(body, str) or len(body) > MAX_GATE_BYTES:
        raise SmokeError("deployed Lambda response envelope is invalid")
    return _mapping(_strict_json(body.encode("utf-8"), "Lambda response"), "Lambda response")


def _parse_timestamp(value: object) -> datetime:
    if not isinstance(value, str):
        raise SmokeError("upload authorization timestamp is invalid")
    try:
        timestamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise SmokeError("upload authorization timestamp is invalid") from None
    if timestamp.utcoffset() is None:
        raise SmokeError("upload authorization timestamp is invalid")
    return timestamp.astimezone(UTC)


def _parse_upload_grant(response: Mapping[str, Any], authority: Authority) -> UploadGrant:
    body = _response_body(response, 201)
    upload = _mapping(body.get("upload"), "upload projection")
    authorization = _mapping(body.get("authorization"), "upload authorization")
    upload_id = upload.get("upload_id")
    job_id = upload.get("job_id")
    url = authorization.get("url")
    fields = _mapping(authorization.get("form_fields"), "upload form")
    if (
        not isinstance(upload_id, str)
        or not isinstance(job_id, str)
        or authorization.get("upload_id") != upload_id
        or authorization.get("job_id") != job_id
        or upload.get("status") != "open"
        or authorization.get("method") != "POST"
        or authorization.get("authorization_generation") != 1
        or authorization.get("content_sha256") != PRIMARY_SHA256
        or authorization.get("size_bytes") != PRIMARY_SIZE
        or not isinstance(url, str)
        or any(
            not isinstance(key, str) or not isinstance(value, str) for key, value in fields.items()
        )
    ):
        raise SmokeError("create upload response is not bound to the exact canary")
    normalized_fields = dict(fields)
    key = normalized_fields.get("key")
    required = {
        "Content-Type": "image/png",
        "x-amz-checksum-algorithm": "SHA256",
        "x-amz-checksum-sha256": base64.b64encode(bytes.fromhex(PRIMARY_SHA256)).decode(),
        "x-amz-server-side-encryption": "AES256",
        "x-amz-tagging": "mr-lister-state=staged",
    }
    expected_origin = f"https://{authority.bucket}.s3.{REGION}.amazonaws.com/"
    allowed_field_names = {
        *required,
        "key",
        "policy",
        "x-amz-algorithm",
        "x-amz-credential",
        "x-amz-date",
        "x-amz-security-token",
        "x-amz-signature",
    }
    if (
        url != expected_origin
        or not isinstance(key, str)
        or not key.startswith(f"private/owners/{authority.owner_id}/jobs/")
        or not key.endswith("/source/source.png")
        or not set(normalized_fields) <= allowed_field_names
        or any(
            not value.isascii()
            or "\r" in value
            or "\n" in value
            or any(ord(character) < 32 or ord(character) == 127 for character in value)
            for value in normalized_fields.values()
        )
        or any(normalized_fields.get(name) != value for name, value in required.items())
    ):
        raise SmokeError("presigned form changed exact key or integrity conditions")
    issued_at = _parse_timestamp(authorization.get("issued_at"))
    expires_at = _parse_timestamp(authorization.get("expires_at"))
    duration = (expires_at - issued_at).total_seconds()
    if not 1 <= duration <= 300:
        raise SmokeError("upload grant expiration exceeds the frozen five-minute ceiling")
    return UploadGrant(
        upload_id=upload_id,
        job_id=job_id,
        url=url,
        fields=normalized_fields,
        key=key,
        issued_at=issued_at,
        expires_at=expires_at,
    )


def _preview_location(response: Mapping[str, Any], authority: Authority) -> str:
    if response.get("statusCode") != 302 or response.get("body") != "":
        raise SmokeError("preview did not return the exact bodyless redirect")
    headers = _mapping(response.get("headers"), "preview headers")
    location = headers.get("Location")
    if not isinstance(location, str):
        raise SmokeError("preview redirect is unavailable")
    try:
        parsed = urlsplit(location)
        query = parse_qs(parsed.query, strict_parsing=True, keep_blank_values=True)
    except ValueError:
        raise SmokeError("preview redirect is invalid") from None
    if query.get("versionId") != [authority.source_version]:
        raise SmokeError("preview redirect is not pinned to the original exact version")
    return location


def _inventory_digest(inventory: Sequence[InventoryVersion]) -> str:
    sanitized = sorted(
        (version.sanitized() for version in inventory),
        key=lambda value: (str(value["last_modified"]), str(value["version_digest"])),
    )
    return _digest_json(sanitized)


def _verify_gate_baseline(snapshot: Snapshot, gate: RunGate, primary: bytes) -> None:
    baseline = gate.baseline
    jobs = {
        payload["job_id"]: payload
        for entity_type, payload in snapshot.items
        if entity_type == "CONTROL_JOB" and isinstance(payload.get("job_id"), str)
    }
    sources = {
        payload["job_id"]: payload
        for entity_type, payload in snapshot.items
        if entity_type == "SOURCE_ARTIFACT" and isinstance(payload.get("job_id"), str)
    }
    job_digests = sorted(_digest_text(job_id) for job_id in jobs)
    source = snapshot.selected_source
    authority = snapshot.authority
    source_authority = {
        key: source[key]
        for key in ("bucket", "object_key", "version_id", "content_sha256", "fingerprint")
    }
    expected = {
        "actor_digest": _digest_text(authority.owner_id),
        "existing_job_count": len(jobs),
        "existing_job_set_digest": _digest_json(job_digests),
        "existing_job_states": sorted(job.get("state") for job in jobs.values()),
        "provider_record_count": sum(
            count
            for entity_type, count in snapshot.entity_counts.items()
            if entity_type.startswith("PROVIDER_")
        ),
        "running_execution_count": 0,
        "selected_inventory_count": len(snapshot.inventory),
        "selected_inventory_digest": _inventory_digest(snapshot.inventory),
        "selected_job_digest": _digest_text(authority.job_id),
        "selected_job_record_digest": _digest_json(snapshot.selected_job),
        "selected_object_coordinate_digest": _digest_text(
            authority.bucket + "\0" + authority.source_key
        ),
        "selected_pinned_is_latest": any(
            version.version_id == authority.source_version and version.is_latest
            for version in snapshot.inventory
        ),
        "selected_pinned_version_digest": _digest_text(authority.source_version),
        "selected_source_authority_digest": _digest_json(source_authority),
        "selected_source_record_digest": _digest_json(snapshot.selected_source),
        "table_record_count": len(snapshot.items),
    }
    for key, value in expected.items():
        if baseline.get(key) != value:
            raise SmokeError("live baseline drifted from the exact private gate")
    if (
        set(jobs) != set(sources)
        or snapshot.selected_job.get("owner_id") != authority.owner_id
        or {job.get("owner_id") for job in jobs.values()} != {authority.owner_id}
    ):
        raise SmokeError("selected owner/job/source baseline is inconsistent")
    if baseline.get("bucket_versioning_enabled") is not True:
        raise SmokeError("gate does not prove bucket versioning")
    if baseline.get("selected_version_head_matches_exact_canary") is not True:
        raise SmokeError("gate does not prove pinned canary authority")
    if baseline.get("selected_version_tag_is_pinned") is not True:
        raise SmokeError("gate does not prove pinned source tag")
    if snapshot.selected_source.get("content_sha256") != _digest_bytes(primary):
        raise SmokeError("selected source does not bind the exact primary canary")


def _protected_digest(snapshot: Snapshot) -> str:
    records = sorted(
        (entity_type, _digest_json(payload))
        for entity_type, payload in snapshot.items
        if entity_type in _PROTECTED_ENTITY_TYPES or entity_type.startswith("PROVIDER_")
    )
    return _digest_json(records)


def _verify_final_delta(before: Snapshot, after: Snapshot, grant: UploadGrant) -> None:
    delta = after.entity_counts - before.entity_counts
    removed = before.entity_counts - after.entity_counts
    if removed or delta != Counter({"UPLOAD_INTENT": 1, "UPLOAD_RECEIPT": 1}):
        raise SmokeError("DynamoDB delta is not exactly one intent and one create receipt")
    before_records = Counter(
        (entity_type, _digest_json(payload)) for entity_type, payload in before.items
    )
    after_records = Counter(
        (entity_type, _digest_json(payload)) for entity_type, payload in after.items
    )
    record_additions = after_records - before_records
    record_removals = before_records - after_records
    if record_removals or Counter(entity for entity, _digest in record_additions.elements()) != (
        Counter({"UPLOAD_INTENT": 1, "UPLOAD_RECEIPT": 1})
    ):
        raise SmokeError("an existing DynamoDB record changed during the smoke")
    if len(after.items) != len(before.items) + 2 or _protected_digest(before) != _protected_digest(
        after
    ):
        raise SmokeError("job/source/work/provider state changed during the smoke")
    if before.execution_digests != after.execution_digests:
        raise SmokeError("a workflow execution delta was observed")
    if _inventory_digest(before.inventory) != _inventory_digest(after.inventory):
        raise SmokeError("source inventory changed during the final state audit")
    new_intents = [
        payload
        for entity_type, payload in after.items
        if entity_type == "UPLOAD_INTENT"
        and payload.get("upload_id") == grant.upload_id
        and not any(
            old_type == entity_type and old_payload.get("upload_id") == grant.upload_id
            for old_type, old_payload in before.items
        )
    ]
    new_receipts = [
        payload
        for entity_type, payload in after.items
        if entity_type == "UPLOAD_RECEIPT"
        and payload.get("upload_id") == grant.upload_id
        and payload.get("command_type") == "create_upload"
    ]
    if len(new_intents) != 1 or len(new_receipts) != 1:
        raise SmokeError("new upload records do not match the exact create command")
    durable_upload_records = _canonical_json([new_intents[0], new_receipts[0]]).lower()
    if any(
        forbidden in durable_upload_records
        for forbidden in (
            b'"form_fields"',
            b'"policy"',
            b'"presigned',
            b'"signature"',
            b'"credential"',
            b'"url"',
        )
    ):
        raise SmokeError("ephemeral presigned authority entered a durable upload record")
    intent = new_intents[0]
    if (
        intent.get("owner_id") != before.authority.owner_id
        or intent.get("job_id") != grant.job_id
        or intent.get("bucket") != before.authority.bucket
        or intent.get("object_key") != grant.key
        or intent.get("content_sha256") != PRIMARY_SHA256
        or intent.get("content_type") != "image/png"
        or intent.get("size_bytes") != PRIMARY_SIZE
        or intent.get("status") != "open"
        or new_receipts[0].get("owner_id") != before.authority.owner_id
        or new_receipts[0].get("job_id") != grant.job_id
    ):
        raise SmokeError("new upload intent does not bind the exact reserved canary")


def run_live(gate: RunGate, backend: LiveBackend, output_root: Path) -> Mapping[str, object]:
    primary, wrong, overwrite = exact_canaries()
    before = backend.prepare(gate, primary)
    _verify_gate_baseline(before, gate, primary)
    authority = before.authority

    preview_event = _event(
        authority,
        "GET /v1/jobs/{job_id}/artwork-preview",
        f"/v1/jobs/{authority.job_id}/artwork-preview",
        path_parameters={"job_id": authority.job_id},
    )
    before_location = _preview_location(backend.invoke_review(authority, preview_event), authority)
    if _digest_bytes(backend.get_preview(before_location)) != PRIMARY_SHA256:
        raise SmokeError("pre-overwrite preview bytes differ from the pinned canary")

    upload_event = _event(
        authority,
        "POST /v1/uploads",
        "/v1/uploads",
        body={
            "filename": "phase66-upload-integrity-canary.png",
            "content_type": "image/png",
            "content_sha256": PRIMARY_SHA256,
            "size_bytes": PRIMARY_SIZE,
        },
        idempotency_key=f"phase66-direct-{gate.digest[:32]}",
    )
    grant = _parse_upload_grant(backend.invoke_upload(authority, upload_event), authority)
    probes: tuple[tuple[Mapping[str, str], bytes], ...] = (
        ({**grant.fields, "Content-Type": "image/gif"}, primary),
        (grant.fields, wrong),
    )
    for fields, content in probes:
        status = backend.post_form(grant.url, fields, content, content_type="image/png")
        if not 400 <= status < 500:
            raise SmokeError("a negative upload probe did not return a definitive rejection")
        if backend.count_exact_versions(authority, grant.key) != 0:
            raise SmokeError("a negative upload probe persisted a reserved-key version")
    backend.wait_until(grant.expires_at + timedelta(seconds=EXPIRY_SKEW_SECONDS))
    status = backend.post_form(grant.url, grant.fields, primary, content_type="image/png")
    if not 400 <= status < 500:
        raise SmokeError("the expired upload grant did not return a definitive rejection")
    if backend.count_exact_versions(authority, grant.key) != 0:
        raise SmokeError("the expired upload replay persisted a reserved-key version")

    temporary_version: str | None = None
    cleanup_authorized = False
    try:
        temporary_version = backend.put_temporary(authority, overwrite)
        if temporary_version == authority.source_version:
            raise SmokeError("temporary overwrite reused the pinned VersionId")
        # The exact VersionId returned by our one bounded PutObject is the cleanup authority.  Set
        # it before later readback assertions so any failed proof cannot orphan that new version.
        cleanup_authorized = True
        backend.prove_temporary(authority, temporary_version, overwrite)
        during = backend.inventory(authority)
        before_by_id = {version.version_id: version for version in before.inventory}
        during_by_id = {version.version_id: version for version in during}
        temporary_observation = during_by_id.get(temporary_version)
        if (
            len(during) != len(before.inventory) + 1
            or len(during_by_id) != len(during)
            or set(during_by_id) != {*before_by_id, temporary_version}
            or temporary_observation is None
            or not temporary_observation.is_latest
            or temporary_observation.size_bytes != OVERWRITE_SIZE
            or any(during_by_id[version_id].is_latest for version_id in before_by_id)
            or any(
                during_by_id[version_id].last_modified != baseline.last_modified
                or during_by_id[version_id].size_bytes != baseline.size_bytes
                or during_by_id[version_id].etag != baseline.etag
                for version_id, baseline in before_by_id.items()
            )
        ):
            raise SmokeError("temporary overwrite did not create exactly one new version")
        after_location = _preview_location(
            backend.invoke_review(authority, preview_event), authority
        )
        if _digest_bytes(backend.get_preview(after_location)) != PRIMARY_SHA256:
            raise SmokeError("post-overwrite preview bytes escaped the pinned version")
    finally:
        if temporary_version is not None and cleanup_authorized:
            backend.delete_temporary(authority, temporary_version)

    restored = backend.inventory(authority)
    if _inventory_digest(restored) != _inventory_digest(before.inventory):
        raise SmokeError("source inventory was not restored after exact-version cleanup")
    after = backend.snapshot(authority)
    _verify_final_delta(before, after, grant)
    if backend.count_exact_versions(authority, grant.key) != 0:
        raise SmokeError("reserved upload key is not empty at final audit")

    canary_summary = {
        "artifact_contract": "phase6.6-deployed-upload-integrity-canary-summary-v1",
        "gate_digest": gate.digest,
        "deployment_digest": gate.deployment_digest,
        "prerequisite_evidence_run_digest": gate.prerequisite_digest,
        "assertions": {
            "expired_upload_grant_is_rejected": True,
            "modified_upload_grant_is_rejected": True,
            "post_finalize_overwrite_cannot_change_preview": True,
            "preview_binds_exact_version": True,
            "provider_call_count_is_zero": True,
            "wrong_artwork_bytes_are_rejected": True,
        },
        "counts": {
            "create_upload_requests": 1,
            "direct_review_lambda_invocations": 2,
            "dynamodb_new_items": 2,
            "negative_s3_posts": 3,
            "persisted_reserved_versions": 0,
            "temporary_exact_version_deletes": 1,
            "temporary_overwrite_puts": 1,
        },
        "redaction_verified": True,
        "source_authority_commit_digest": SOURCE_AUTHORITY_COMMIT_DIGEST,
        "status": "passed",
    }
    log_audit = {
        "artifact_contract": "phase6.6-deployed-upload-integrity-log-audit-v1",
        "gate_digest": gate.digest,
        "deployment_digest": gate.deployment_digest,
        "prerequisite_evidence_run_digest": gate.prerequisite_digest,
        "deltas": {
            "agentcore_invocations": 0,
            "bedrock_invocations": 0,
            "jobs": 0,
            "provider_calls": 0,
            "provider_records": 0,
            "source_artifacts": 0,
            "workflow_executions": 0,
            "work_requests": 0,
        },
        "raw_authority_retained": False,
        "status": "passed",
    }
    output = _validate_private_directory(output_root, create=True)
    summary_size, summary_digest = _atomic_private_json(
        output / "canary-summary.json", canary_summary
    )
    audit_size, audit_digest = _atomic_private_json(output / "log-audit.json", log_audit)
    return {
        "gate_id": GATE_ID,
        "mode": "live",
        "status": "passed",
        "artifacts": [
            {"kind": "canary_summary", "byte_count": summary_size, "sha256": summary_digest},
            {"kind": "log_audit", "byte_count": audit_size, "sha256": audit_digest},
        ],
        "redaction_verified": True,
    }


class AwsBackend:
    """Boto3/HTTPS implementation; constructed only after all local live gates pass."""

    def __init__(self, *, profile: str = PROFILE, region: str = REGION, stack: str = STACK_NAME):
        try:
            import boto3
            from botocore.config import Config
        except ImportError:
            raise SmokeError("boto3 is unavailable for the explicitly enabled live run") from None
        session = boto3.Session(profile_name=profile, region_name=region)
        no_retry = Config(retries={"total_max_attempts": 1, "mode": "standard"})
        self._cloudformation = session.client("cloudformation", config=no_retry)
        self._cognito = session.client("cognito-idp", config=no_retry)
        self._dynamodb = session.client("dynamodb", config=no_retry)
        self._lambda = session.client("lambda", config=no_retry)
        self._s3 = session.client("s3", config=no_retry)
        self._sfn = session.client("stepfunctions", config=no_retry)
        self._sts = session.client("sts", config=no_retry)
        self._region = region
        self._stack = stack

    def _stack_outputs(self) -> Mapping[str, str]:
        response = self._cloudformation.describe_stacks(StackName=self._stack)
        stacks = response.get("Stacks", [])
        if len(stacks) != 1 or stacks[0].get("StackStatus") != "UPDATE_COMPLETE":
            raise SmokeError("Phase 6 stack is not one exact UPDATE_COMPLETE deployment")
        outputs = stacks[0].get("Outputs", [])
        result = {
            item["OutputKey"]: item["OutputValue"]
            for item in outputs
            if isinstance(item, Mapping)
            and isinstance(item.get("OutputKey"), str)
            and isinstance(item.get("OutputValue"), str)
        }
        if len(result) != len(outputs):
            raise SmokeError("Phase 6 stack outputs are duplicated or malformed")
        return result

    def _physical(self, logical_id: str) -> str:
        response = self._cloudformation.describe_stack_resource(
            StackName=self._stack, LogicalResourceId=logical_id
        )
        detail = _mapping(response.get("StackResourceDetail"), "stack resource")
        value = detail.get("PhysicalResourceId")
        if detail.get("ResourceStatus") not in {
            "CREATE_COMPLETE",
            "UPDATE_COMPLETE",
        } or not isinstance(value, str):
            raise SmokeError("deployed stack resource is not ready")
        return value

    def _configuration(
        self,
        function_name: str,
        handler: str,
        code_sha256: str,
        release_fingerprint: str,
        outputs: Mapping[str, str],
    ) -> None:
        configuration = self._lambda.get_function_configuration(FunctionName=function_name)
        variables = _mapping(
            _mapping(configuration.get("Environment"), "Lambda environment").get("Variables"),
            "Lambda variables",
        )
        expected = _expected_lambda_environment(outputs, release_fingerprint)
        if (
            configuration.get("State") != "Active"
            or configuration.get("LastUpdateStatus") != "Successful"
            or configuration.get("Handler") != handler
            or configuration.get("CodeSha256") != code_sha256
            or configuration.get("Runtime") != "python3.12"
            or configuration.get("Timeout") != 30
            or configuration.get("MemorySize") != 512
            or configuration.get("Architectures") != ["arm64"]
            or configuration.get("PackageType") != "Zip"
            or variables != expected
        ):
            raise SmokeError("deployed Lambda code/environment envelope drifted")

    def _scan(self, table_name: str) -> tuple[tuple[str, Mapping[str, Any]], ...]:
        items: list[tuple[str, Mapping[str, Any]]] = []
        request: dict[str, Any] = {
            "TableName": table_name,
            "ProjectionExpression": "#e,payload",
            "ExpressionAttributeNames": {"#e": "entity_type"},
            "ConsistentRead": True,
        }
        while True:
            response = self._dynamodb.scan(**request)
            for item in response.get("Items", []):
                entity = item.get("entity_type", {}).get("S")
                payload = item.get("payload", {}).get("S")
                if not isinstance(entity, str) or not isinstance(payload, str):
                    raise SmokeError("DynamoDB item lacks the closed payload envelope")
                items.append((entity, _mapping(_strict_json(payload.encode(), "record"), "record")))
            key = response.get("LastEvaluatedKey")
            if not key:
                break
            request["ExclusiveStartKey"] = key
        return tuple(items)

    def _inventories(self, bucket: str, key: str) -> tuple[InventoryVersion, ...]:
        response = self._s3.list_object_versions(Bucket=bucket, Prefix=key)
        if response.get("IsTruncated"):
            raise SmokeError("bounded exact-key inventory was unexpectedly truncated")
        if any(marker.get("Key") == key for marker in response.get("DeleteMarkers", [])):
            raise SmokeError("exact source key has a delete marker")
        versions = tuple(
            InventoryVersion(
                version_id=record["VersionId"],
                is_latest=bool(record["IsLatest"]),
                last_modified=record["LastModified"].astimezone(UTC).isoformat(),
                size_bytes=int(record["Size"]),
                etag=record["ETag"],
            )
            for record in response.get("Versions", [])
            if record.get("Key") == key
        )
        return versions

    def _execution_digests(self, arns: Sequence[str]) -> tuple[str, ...]:
        values: list[str] = []
        for arn in arns:
            token: str | None = None
            while True:
                request: dict[str, Any] = {"stateMachineArn": arn, "maxResults": 100}
                if token is not None:
                    request["nextToken"] = token
                response = self._sfn.list_executions(**request)
                for execution in response.get("executions", []):
                    execution_arn = execution.get("executionArn")
                    if not isinstance(execution_arn, str):
                        raise SmokeError("workflow execution inventory is invalid")
                    values.append(_digest_text(execution_arn))
                token = response.get("nextToken")
                if not isinstance(token, str):
                    break
        return tuple(sorted(values))

    def _subject(self, user_pool_id: str, issuer: str, owner_id: str) -> str:
        matches: list[tuple[str, str]] = []
        token: str | None = None
        while True:
            request: dict[str, Any] = {"UserPoolId": user_pool_id, "Limit": 60}
            if token is not None:
                request["PaginationToken"] = token
            response = self._cognito.list_users(**request)
            for user in response.get("Users", []):
                username = user.get("Username")
                attributes = {
                    item.get("Name"): item.get("Value") for item in user.get("Attributes", [])
                }
                subject = attributes.get("sub")
                if isinstance(username, str) and isinstance(subject, str):
                    derived = hashlib.sha256(issuer.encode() + b"\0" + subject.encode()).hexdigest()
                    if secrets.compare_digest(derived, owner_id):
                        matches.append((username, subject))
            token = response.get("PaginationToken")
            if not isinstance(token, str):
                break
        if len(matches) != 1:
            raise SmokeError("Cognito subject did not resolve uniquely to the selected owner")
        username, subject = matches[0]
        groups = self._cognito.admin_list_groups_for_user(
            Username=username, UserPoolId=user_pool_id, Limit=60
        ).get("Groups", [])
        if {group.get("GroupName") for group in groups} != {"seller"}:
            raise SmokeError("selected Cognito subject does not have exact seller membership")
        return subject

    def _snapshot_from(
        self,
        authority: Authority,
        selected_job: Mapping[str, Any],
        selected_source: Mapping[str, Any],
    ) -> Snapshot:
        items = self._scan(authority.table_name)
        jobs = [
            payload
            for entity, payload in items
            if entity == "CONTROL_JOB" and payload.get("job_id") == authority.job_id
        ]
        sources = [
            payload
            for entity, payload in items
            if entity == "SOURCE_ARTIFACT" and payload.get("job_id") == authority.job_id
        ]
        if len(jobs) != 1 or len(sources) != 1:
            raise SmokeError("selected job/source is no longer unique")
        return Snapshot(
            authority=authority,
            items=items,
            selected_job=jobs[0],
            selected_source=sources[0],
            inventory=self._inventories(authority.bucket, authority.source_key),
            execution_digests=self._execution_digests(authority.state_machine_arns),
        )

    def prepare(self, gate: RunGate, primary: bytes) -> Snapshot:
        identity = self._sts.get_caller_identity()
        if identity.get("Account") != ACCOUNT_ID:
            raise SmokeError("AWS session is not in the exact deployment account")
        outputs = self._stack_outputs()
        required_outputs = {
            "ArtifactBucketBrowserOrigin",
            "ArtifactBucketName",
            "DeploymentReadiness",
            "PrepareStateMachineArn",
            "ReconcileProductStateMachineArn",
            "RefreshEconomicsStateMachineArn",
            "SellerApplicationOrigin",
            "SellerUserPoolClientId",
            "SellerUserPoolId",
            "StateTableName",
            "SynchronizeProductStateMachineArn",
        }
        if not required_outputs <= set(outputs) or outputs["DeploymentReadiness"] != (
            "WEB_EDGE_ACTIVE_DRAFT_ONLY"
        ):
            raise SmokeError("Phase 6 stack outputs drifted from the active draft-only envelope")
        functions = {
            logical_id: self._physical(logical_id) for logical_id in _EXPECTED_LAMBDA_HANDLERS
        }
        if functions != {
            "UploadApiFunction": "mr-lister-phase6-dev-upload-api",
            "ReviewQueryApiFunction": "mr-lister-phase6-dev-review-query-api",
        }:
            raise SmokeError("deployed Lambda physical identities drifted")
        for logical_id, handler in _EXPECTED_LAMBDA_HANDLERS.items():
            self._configuration(
                functions[logical_id],
                handler,
                _EXPECTED_LAMBDA_CODE_SHA256[logical_id],
                _EXPECTED_LAMBDA_RELEASE_FINGERPRINT[logical_id],
                outputs,
            )
        items = self._scan(outputs["StateTableName"])
        candidates = [
            payload
            for entity, payload in items
            if entity == "CONTROL_JOB"
            and isinstance(payload.get("job_id"), str)
            and _digest_text(payload["job_id"]) == gate.baseline["selected_job_digest"]
        ]
        if len(candidates) != 1:
            raise SmokeError("gate-selected job is unavailable")
        selected_job = candidates[0]
        job_id = selected_job["job_id"]
        owner_id = selected_job.get("owner_id")
        sources = [
            payload
            for entity, payload in items
            if entity == "SOURCE_ARTIFACT" and payload.get("job_id") == job_id
        ]
        if len(sources) != 1 or not isinstance(owner_id, str):
            raise SmokeError("gate-selected source authority is unavailable")
        source = sources[0]
        required_source = ("bucket", "object_key", "version_id")
        if any(not isinstance(source.get(key), str) for key in required_source):
            raise SmokeError("source authority envelope is invalid")
        issuer = f"https://cognito-idp.{REGION}.amazonaws.com/{outputs['SellerUserPoolId']}"
        subject = self._subject(outputs["SellerUserPoolId"], issuer, owner_id)
        authority = Authority(
            owner_id=owner_id,
            subject=subject,
            job_id=job_id,
            bucket=source["bucket"],
            source_key=source["object_key"],
            source_version=source["version_id"],
            issuer=issuer,
            client_id=outputs["SellerUserPoolClientId"],
            upload_function=functions["UploadApiFunction"],
            review_function=functions["ReviewQueryApiFunction"],
            table_name=outputs["StateTableName"],
            state_machine_arns=tuple(
                outputs[key]
                for key in (
                    "PrepareStateMachineArn",
                    "SynchronizeProductStateMachineArn",
                    "ReconcileProductStateMachineArn",
                    "RefreshEconomicsStateMachineArn",
                )
            ),
        )
        snapshot = Snapshot(
            authority=authority,
            items=items,
            selected_job=selected_job,
            selected_source=source,
            inventory=self._inventories(authority.bucket, authority.source_key),
            execution_digests=self._execution_digests(authority.state_machine_arns),
        )
        head = self._s3.head_object(
            Bucket=authority.bucket,
            Key=authority.source_key,
            VersionId=authority.source_version,
            ChecksumMode="ENABLED",
            ExpectedBucketOwner=ACCOUNT_ID,
        )
        tags = self._s3.get_object_tagging(
            Bucket=authority.bucket,
            Key=authority.source_key,
            VersionId=authority.source_version,
            ExpectedBucketOwner=ACCOUNT_ID,
        )
        versioning = self._s3.get_bucket_versioning(Bucket=authority.bucket)
        checksum = base64.b64encode(hashlib.sha256(primary).digest()).decode()
        if (
            versioning.get("Status") != "Enabled"
            or head.get("VersionId") != authority.source_version
            or head.get("ContentLength") != len(primary)
            or head.get("ContentType") != "image/png"
            or head.get("ChecksumSHA256") != checksum
            or head.get("ServerSideEncryption") != "AES256"
            or tags.get("TagSet") != [{"Key": "mr-lister-state", "Value": "pinned"}]
        ):
            raise SmokeError("pinned source object does not match exact live authority")
        running = 0
        for arn in authority.state_machine_arns:
            running += len(
                self._sfn.list_executions(
                    stateMachineArn=arn, statusFilter="RUNNING", maxResults=100
                ).get("executions", [])
            )
        if running:
            raise SmokeError("a Phase 6 workflow execution is already running")
        return snapshot

    def _invoke(self, function_name: str, event: Mapping[str, Any]) -> Mapping[str, Any]:
        response = self._lambda.invoke(
            FunctionName=function_name,
            InvocationType="RequestResponse",
            Payload=_canonical_json(event),
        )
        stream = response.get("Payload")
        try:
            payload = stream.read(MAX_GATE_BYTES + 1)
        finally:
            close = getattr(stream, "close", None)
            if callable(close):
                close()
        if (
            response.get("StatusCode") != 200
            or response.get("FunctionError") is not None
            or not isinstance(payload, bytes)
            or len(payload) > MAX_GATE_BYTES
        ):
            raise SmokeError("direct deployed Lambda invocation failed closed")
        return _mapping(_strict_json(payload, "Lambda payload"), "Lambda payload")

    def invoke_upload(self, authority: Authority, event: Mapping[str, Any]) -> Mapping[str, Any]:
        return self._invoke(authority.upload_function, event)

    def invoke_review(self, authority: Authority, event: Mapping[str, Any]) -> Mapping[str, Any]:
        return self._invoke(authority.review_function, event)

    def post_form(
        self, url: str, fields: Mapping[str, str], content: bytes, *, content_type: str
    ) -> int:
        boundary = f"phase66-{secrets.token_hex(24)}"
        chunks: list[bytes] = []
        for name, value in fields.items():
            chunks.extend(
                (
                    f"--{boundary}\r\n".encode(),
                    f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode(),
                    value.encode(),
                    b"\r\n",
                )
            )
        chunks.extend(
            (
                f"--{boundary}\r\n".encode(),
                b'Content-Disposition: form-data; name="file"; filename="canary.png"\r\n',
                f"Content-Type: {content_type}\r\n\r\n".encode(),
                content,
                b"\r\n",
                f"--{boundary}--\r\n".encode(),
            )
        )
        request = urllib.request.Request(
            url,
            data=b"".join(chunks),
            method="POST",
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        )
        try:
            with urllib.request.urlopen(request, timeout=90) as response:
                response.read(MAX_HTTP_RESPONSE_BYTES + 1)
                return int(response.status)
        except urllib.error.HTTPError as error:
            try:
                error.read(MAX_HTTP_RESPONSE_BYTES + 1)
            finally:
                error.close()
            return int(error.code)
        except (OSError, urllib.error.URLError, ValueError):
            raise SmokeError(
                "negative HTTPS upload probe did not return a closed response"
            ) from None

    def count_exact_versions(self, authority: Authority, key: str) -> int:
        response = self._s3.list_object_versions(Bucket=authority.bucket, Prefix=key)
        if response.get("IsTruncated"):
            raise SmokeError("reserved-key inventory was truncated")
        return sum(
            record.get("Key") == key
            for record in response.get("Versions", []) + response.get("DeleteMarkers", [])
        )

    def get_preview(self, url: str) -> bytes:
        request = urllib.request.Request(url, method="GET")
        try:
            with urllib.request.urlopen(request, timeout=90) as response:
                content = response.read(PRIMARY_SIZE + 1)
                if response.status != 200 or len(content) != PRIMARY_SIZE:
                    raise SmokeError("preview response does not contain the exact canary size")
                return content
        except SmokeError:
            raise
        except (OSError, urllib.error.URLError, ValueError):
            raise SmokeError("preview HTTPS read failed closed") from None

    def put_temporary(self, authority: Authority, content: bytes) -> str:
        response = self._s3.put_object(
            Bucket=authority.bucket,
            Key=authority.source_key,
            Body=content,
            ContentType="image/png",
            ChecksumAlgorithm="SHA256",
            ChecksumSHA256=base64.b64encode(hashlib.sha256(content).digest()).decode(),
            ServerSideEncryption="AES256",
            Tagging="mr-lister-state=staged",
            ExpectedBucketOwner=ACCOUNT_ID,
        )
        version_id = response.get("VersionId")
        if not isinstance(version_id, str) or not version_id:
            raise SmokeError("temporary overwrite did not return one exact VersionId")
        return version_id

    def prove_temporary(self, authority: Authority, version_id: str, content: bytes) -> None:
        head = self._s3.head_object(
            Bucket=authority.bucket,
            Key=authority.source_key,
            VersionId=version_id,
            ChecksumMode="ENABLED",
            ExpectedBucketOwner=ACCOUNT_ID,
        )
        tags = self._s3.get_object_tagging(
            Bucket=authority.bucket,
            Key=authority.source_key,
            VersionId=version_id,
            ExpectedBucketOwner=ACCOUNT_ID,
        )
        if (
            version_id == authority.source_version
            or head.get("VersionId") != version_id
            or head.get("ContentLength") != len(content)
            or head.get("ContentType") != "image/png"
            or head.get("ChecksumSHA256")
            != base64.b64encode(hashlib.sha256(content).digest()).decode()
            or head.get("ServerSideEncryption") != "AES256"
            or tags.get("TagSet") != [{"Key": "mr-lister-state", "Value": "staged"}]
        ):
            raise SmokeError("temporary overwrite identity could not be proven exactly")

    def delete_temporary(self, authority: Authority, version_id: str) -> None:
        response = self._s3.delete_object(
            Bucket=authority.bucket,
            Key=authority.source_key,
            VersionId=version_id,
            ExpectedBucketOwner=ACCOUNT_ID,
        )
        if response.get("VersionId") != version_id or response.get("DeleteMarker") is True:
            raise SmokeError("exact temporary VersionId cleanup was not confirmed")

    def inventory(self, authority: Authority) -> tuple[InventoryVersion, ...]:
        return self._inventories(authority.bucket, authority.source_key)

    def snapshot(self, authority: Authority) -> Snapshot:
        return self._snapshot_from(authority, {}, {})

    def wait_until(self, timestamp: datetime) -> None:
        if timestamp.utcoffset() is None:
            raise SmokeError("expiry timestamp is invalid")
        remaining = (timestamp - datetime.now(UTC)).total_seconds()
        if remaining > 310:
            raise SmokeError("expiry wait exceeds the exact five-minute grant ceiling")
        while remaining > 0:
            time.sleep(min(remaining, 1.0))
            remaining = (timestamp - datetime.now(UTC)).total_seconds()


def _preflight_result(gate: RunGate) -> Mapping[str, object]:
    exact_canaries()
    return {
        "gate_id": GATE_ID,
        "gate_digest": gate.digest,
        "deployment_digest": gate.deployment_digest,
        "mode": "local_preflight",
        "network_calls": 0,
        "prerequisite_evidence_run_digest": gate.prerequisite_digest,
        "mutations": 0,
        "status": "ready",
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gate", type=Path, required=True)
    parser.add_argument("--gate-sha256", required=True)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--output-root", type=Path)
    return parser


def main(
    argv: Sequence[str] | None = None, *, backend_factory: Callable[[], LiveBackend] = AwsBackend
) -> int:
    arguments = _parser().parse_args(argv)
    gate = load_run_gate(arguments.gate, arguments.gate_sha256)
    if not arguments.live:
        print(_canonical_json(_preflight_result(gate)).decode())
        return 0
    if arguments.output_root is None:
        raise SmokeError("live mode requires an explicit repository-private output root")
    if os.environ.get(LIVE_ENVIRONMENT_SWITCH) != LIVE_ENVIRONMENT_VALUE:
        raise SmokeError("live mode requires the exact one-run environment switch")
    output_root = _exact_private_path(arguments.output_root)
    backend = backend_factory()
    print(_canonical_json(run_live(gate, backend, output_root)).decode())
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SmokeError as error:
        raise SystemExit(f"phase66 deployed upload-integrity smoke stopped: {error}") from None
    except Exception:
        # AWS/HTTP exceptions may carry request parameters.  Never render them at this boundary.
        raise SystemExit(
            "phase66 deployed upload-integrity smoke stopped: an external operation failed closed"
        ) from None
