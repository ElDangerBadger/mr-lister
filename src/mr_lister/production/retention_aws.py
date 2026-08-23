"""Least-capability AWS adapters for reference-aware source-version retention.

The public surface is intentionally limited to version inventory, exact-version tags,
strong job/source authority reads, and one CAS-protected checkpoint.  It contains no object-byte
read, object deletion, secret, publication, order, fulfillment, or provider capability.
"""

from __future__ import annotations

import base64
import json
import re
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from typing import Any, Protocol

from mr_lister.control.models import ControlJobRecord, SourceArtifactRecord
from mr_lister.control.source_artwork import validate_source_artifact_authority
from mr_lister.production.retention import (
    PHASE6_SOURCE_PREFIX,
    ListedSourceVersion,
    RetentionBoundaryInvalidError,
    RetentionCheckpoint,
    RetentionDependencyUnavailableError,
    SourceAuthoritySnapshot,
    SourceLifecycleState,
    SourceVersionPage,
    SourceVersionTag,
    SourceVersionTags,
)

RETENTION_CHECKPOINT_PARTITION_KEY = "SYSTEM#SOURCE_VERSION_RETENTION"
RETENTION_CHECKPOINT_SORT_KEY = "CHECKPOINT"

_DEPENDENCY_UNAVAILABLE = "Retention dependency is unavailable"
_BOUNDARY_INVALID = "Retention AWS response is invalid"
_CURSOR_PREFIX = "s3-version-page-v1."
_CHECKPOINT_ENTITY = "SOURCE_VERSION_RETENTION_CHECKPOINT"
_MAX_CHECKPOINT_PAYLOAD_BYTES = 350 * 1024

_BUCKET = re.compile(r"^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$")
_ACCOUNT_ID = re.compile(r"^[0-9]{12}$")
_TABLE_NAME = re.compile(r"^[A-Za-z0-9_.-]{3,255}$")
_JOB_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
_SOURCE_KEY = re.compile(
    r"^private/owners/[a-f0-9]{64}/jobs/"
    r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}/source/source\.png$"
)
_VERSION_ID = re.compile(r"^[\x21-\x7e]{1,1024}$")
_CURSOR = re.compile(r"^[A-Za-z0-9._-]{1,2048}$")


class S3VersionInventoryClient(Protocol):
    def list_object_versions(self, **kwargs: Any) -> Mapping[str, Any]: ...


class S3VersionTaggingClient(Protocol):
    def get_object_tagging(self, **kwargs: Any) -> Mapping[str, Any]: ...

    def put_object_tagging(self, **kwargs: Any) -> Mapping[str, Any]: ...


class DynamoRetentionClient(Protocol):
    def transact_get_items(self, **kwargs: Any) -> Mapping[str, Any]: ...

    def get_item(self, **kwargs: Any) -> Mapping[str, Any]: ...

    def put_item(self, **kwargs: Any) -> Mapping[str, Any]: ...


class S3SourceVersionInventory:
    """List only bounded versions below the fixed private source prefix."""

    __slots__ = ("_bucket", "_bucket_owner", "_client")

    def __init__(
        self,
        *,
        client: S3VersionInventoryClient,
        artifact_bucket: str,
        bucket_owner_account_id: str,
    ) -> None:
        _validate_s3_configuration(artifact_bucket, bucket_owner_account_id)
        self._client = client
        self._bucket = artifact_bucket
        self._bucket_owner = bucket_owner_account_id

    def list_source_versions(
        self,
        *,
        source_prefix: str,
        cursor: str | None,
        limit: int,
    ) -> SourceVersionPage:
        if source_prefix != PHASE6_SOURCE_PREFIX:
            raise RetentionBoundaryInvalidError(_BOUNDARY_INVALID)
        if type(limit) is not int or not 1 <= limit <= 1_000:
            raise RetentionBoundaryInvalidError(_BOUNDARY_INVALID)
        markers = _decode_cursor(cursor)
        request: dict[str, Any] = {
            "Bucket": self._bucket,
            "Prefix": PHASE6_SOURCE_PREFIX,
            "MaxKeys": limit,
            "ExpectedBucketOwner": self._bucket_owner,
        }
        if markers is not None:
            request["KeyMarker"], request["VersionIdMarker"] = markers
        response = _dependency_call(lambda: self._client.list_object_versions(**request))
        return _parse_inventory_page(
            response,
            bucket=self._bucket,
            limit=limit,
            expected_markers=markers,
        )


class S3SourceVersionTagStore:
    """Read or replace only the lifecycle tag on one exact S3 VersionId."""

    __slots__ = ("_bucket", "_bucket_owner", "_client")

    def __init__(
        self,
        *,
        client: S3VersionTaggingClient,
        artifact_bucket: str,
        bucket_owner_account_id: str,
    ) -> None:
        _validate_s3_configuration(artifact_bucket, bucket_owner_account_id)
        self._client = client
        self._bucket = artifact_bucket
        self._bucket_owner = bucket_owner_account_id

    def get_version_tags(
        self,
        *,
        object_key: str,
        version_id: str,
    ) -> SourceVersionTags:
        _validate_source_identity(object_key, version_id)
        response = _dependency_call(
            lambda: self._client.get_object_tagging(
                Bucket=self._bucket,
                Key=object_key,
                VersionId=version_id,
                ExpectedBucketOwner=self._bucket_owner,
            )
        )
        return _parse_version_tags(response)

    def set_version_state(
        self,
        *,
        object_key: str,
        version_id: str,
        state: SourceLifecycleState,
    ) -> None:
        _validate_source_identity(object_key, version_id)
        if state not in {"staged", "pinned"}:
            raise RetentionBoundaryInvalidError(_BOUNDARY_INVALID)
        _validate_write_response(
            _dependency_call(
                lambda: self._client.put_object_tagging(
                    Bucket=self._bucket,
                    Key=object_key,
                    VersionId=version_id,
                    Tagging={"TagSet": [{"Key": "mr-lister-state", "Value": state}]},
                    ExpectedBucketOwner=self._bucket_owner,
                )
            )
        )


class DynamoDBStrongSourceAuthorityReader:
    """Read the job and canonical source row in one serializable Dynamo transaction."""

    __slots__ = ("_client", "_table_name")

    def __init__(self, *, client: DynamoRetentionClient, table_name: str) -> None:
        _validate_table_name(table_name)
        self._client = client
        self._table_name = table_name

    def read_source_authority_strong(self, *, job_id: str) -> SourceAuthoritySnapshot:
        if not isinstance(job_id, str) or _JOB_ID.fullmatch(job_id) is None:
            raise RetentionBoundaryInvalidError(_BOUNDARY_INVALID)
        partition_key = f"JOB#{job_id}"
        response = _dependency_call(
            lambda: self._client.transact_get_items(
                TransactItems=[
                    {
                        "Get": {
                            "TableName": self._table_name,
                            "Key": {"PK": _s(partition_key), "SK": _s("META")},
                        }
                    },
                    {
                        "Get": {
                            "TableName": self._table_name,
                            "Key": {"PK": _s(partition_key), "SK": _s("SOURCE")},
                        }
                    },
                ]
            )
        )
        return _parse_authority_response(response, job_id=job_id)


class DynamoDBRetentionCheckpointStore:
    """Persist the bounded whole-prefix continuation behind exact revision/payload CAS."""

    __slots__ = ("_client", "_table_name")

    def __init__(self, *, client: DynamoRetentionClient, table_name: str) -> None:
        _validate_table_name(table_name)
        self._client = client
        self._table_name = table_name

    def load_checkpoint(self) -> RetentionCheckpoint:
        response = _dependency_call(
            lambda: self._client.get_item(
                TableName=self._table_name,
                Key=_checkpoint_key(),
                ConsistentRead=True,
            )
        )
        if not isinstance(response, Mapping):
            raise RetentionBoundaryInvalidError(_BOUNDARY_INVALID)
        if "Item" not in response:
            return RetentionCheckpoint()
        item = response.get("Item")
        if not isinstance(item, Mapping):
            raise RetentionBoundaryInvalidError(_BOUNDARY_INVALID)
        return _parse_checkpoint_item(item)

    def save_checkpoint(
        self,
        *,
        expected: RetentionCheckpoint,
        updated: RetentionCheckpoint,
    ) -> None:
        current = _strict_checkpoint(expected)
        replacement = _strict_checkpoint(updated)
        if replacement.revision != current.revision + 1:
            raise RetentionBoundaryInvalidError(_BOUNDARY_INVALID)
        if current.revision == 0 and current != RetentionCheckpoint():
            raise RetentionBoundaryInvalidError(_BOUNDARY_INVALID)
        item = _checkpoint_item(replacement)
        if current.revision == 0:
            condition = "attribute_not_exists(PK)"
            values: dict[str, dict[str, str]] | None = None
        else:
            condition = (
                "entity_type = :entity_type AND revision = :expected_revision "
                "AND payload = :expected_payload"
            )
            values = {
                ":entity_type": _s(_CHECKPOINT_ENTITY),
                ":expected_revision": _n(current.revision),
                ":expected_payload": _s(_checkpoint_payload(current)),
            }
        request: dict[str, Any] = {
            "TableName": self._table_name,
            "Item": item,
            "ConditionExpression": condition,
        }
        if values is not None:
            request["ExpressionAttributeValues"] = values
        _validate_write_response(_dependency_call(lambda: self._client.put_item(**request)))


def _parse_inventory_page(
    response: object,
    *,
    bucket: str,
    limit: int,
    expected_markers: tuple[str, str] | None,
) -> SourceVersionPage:
    if not isinstance(response, Mapping):
        raise RetentionBoundaryInvalidError(_BOUNDARY_INVALID)
    if (
        response.get("Name") != bucket
        or response.get("Prefix") != PHASE6_SOURCE_PREFIX
        or type(response.get("MaxKeys")) is not int
        or response.get("MaxKeys") != limit
    ):
        raise RetentionBoundaryInvalidError(_BOUNDARY_INVALID)
    echoed_key = response.get("KeyMarker")
    echoed_version = response.get("VersionIdMarker")
    if expected_markers is None:
        if echoed_key not in {None, ""} or echoed_version not in {None, ""}:
            raise RetentionBoundaryInvalidError(_BOUNDARY_INVALID)
    elif (echoed_key, echoed_version) != expected_markers:
        raise RetentionBoundaryInvalidError(_BOUNDARY_INVALID)
    observed_at = _response_observed_at(response)
    raw_versions = response.get("Versions", [])
    raw_delete_markers = response.get("DeleteMarkers", [])
    if not isinstance(raw_versions, list) or not isinstance(raw_delete_markers, list):
        raise RetentionBoundaryInvalidError(_BOUNDARY_INVALID)
    if len(raw_versions) + len(raw_delete_markers) > limit:
        raise RetentionBoundaryInvalidError(_BOUNDARY_INVALID)

    versions: list[ListedSourceVersion] = []
    for raw in raw_versions:
        if not isinstance(raw, Mapping):
            raise RetentionBoundaryInvalidError(_BOUNDARY_INVALID)
        object_key = raw.get("Key")
        version_id = raw.get("VersionId")
        last_modified = raw.get("LastModified")
        _validate_source_identity(object_key, version_id)
        if not isinstance(last_modified, datetime) or last_modified.utcoffset() != timedelta(0):
            raise RetentionBoundaryInvalidError(_BOUNDARY_INVALID)
        try:
            versions.append(
                ListedSourceVersion(
                    object_key=object_key,
                    version_id=version_id,
                    last_modified=last_modified.astimezone(UTC),
                )
            )
        except Exception:
            pass
        else:
            continue
        raise RetentionBoundaryInvalidError(_BOUNDARY_INVALID) from None

    for raw in raw_delete_markers:
        if not isinstance(raw, Mapping):
            raise RetentionBoundaryInvalidError(_BOUNDARY_INVALID)
        _validate_source_identity(raw.get("Key"), raw.get("VersionId"))

    truncated = response.get("IsTruncated")
    if type(truncated) is not bool:
        raise RetentionBoundaryInvalidError(_BOUNDARY_INVALID)
    next_key = response.get("NextKeyMarker")
    next_version = response.get("NextVersionIdMarker")
    if truncated:
        _validate_source_identity(next_key, next_version)
        next_cursor = _encode_cursor(next_key, next_version)
    else:
        if next_key is not None or next_version is not None:
            raise RetentionBoundaryInvalidError(_BOUNDARY_INVALID)
        next_cursor = None
    try:
        return SourceVersionPage(
            observed_at=observed_at,
            versions=tuple(versions),
            delete_marker_count=len(raw_delete_markers),
            next_cursor=next_cursor,
        )
    except Exception:
        pass
    raise RetentionBoundaryInvalidError(_BOUNDARY_INVALID) from None


def _response_observed_at(response: Mapping[str, Any]) -> datetime:
    metadata = response.get("ResponseMetadata")
    headers = metadata.get("HTTPHeaders") if isinstance(metadata, Mapping) else None
    if not isinstance(headers, Mapping):
        raise RetentionBoundaryInvalidError(_BOUNDARY_INVALID)
    dates = [value for key, value in headers.items() if str(key).lower() == "date"]
    if len(dates) != 1 or not isinstance(dates[0], str):
        raise RetentionBoundaryInvalidError(_BOUNDARY_INVALID)
    try:
        observed_at = parsedate_to_datetime(dates[0])
    except Exception:
        pass
    else:
        if observed_at.utcoffset() == timedelta(0):
            return observed_at.astimezone(UTC)
    raise RetentionBoundaryInvalidError(_BOUNDARY_INVALID) from None


def _parse_version_tags(response: object) -> SourceVersionTags:
    if not isinstance(response, Mapping):
        raise RetentionBoundaryInvalidError(_BOUNDARY_INVALID)
    raw_tags = response.get("TagSet")
    if not isinstance(raw_tags, list) or len(raw_tags) > 10:
        raise RetentionBoundaryInvalidError(_BOUNDARY_INVALID)
    tags: list[SourceVersionTag] = []
    for raw in raw_tags:
        if not isinstance(raw, Mapping) or set(raw) != {"Key", "Value"}:
            raise RetentionBoundaryInvalidError(_BOUNDARY_INVALID)
        try:
            tags.append(SourceVersionTag(key=raw.get("Key"), value=raw.get("Value")))
        except Exception:
            pass
        else:
            continue
        raise RetentionBoundaryInvalidError(_BOUNDARY_INVALID) from None
    try:
        return SourceVersionTags(tags=tuple(tags))
    except Exception:
        pass
    raise RetentionBoundaryInvalidError(_BOUNDARY_INVALID) from None


def _parse_authority_response(response: object, *, job_id: str) -> SourceAuthoritySnapshot:
    if not isinstance(response, Mapping):
        raise RetentionBoundaryInvalidError(_BOUNDARY_INVALID)
    responses = response.get("Responses")
    if not isinstance(responses, list) or len(responses) != 2:
        raise RetentionBoundaryInvalidError(_BOUNDARY_INVALID)
    envelopes: list[Mapping[str, Any] | None] = []
    for raw in responses:
        if not isinstance(raw, Mapping):
            raise RetentionBoundaryInvalidError(_BOUNDARY_INVALID)
        if "Item" not in raw:
            envelopes.append(None)
            continue
        item = raw.get("Item")
        if not isinstance(item, Mapping):
            raise RetentionBoundaryInvalidError(_BOUNDARY_INVALID)
        envelopes.append(item)
    job_item, source_item = envelopes
    if job_item is None and source_item is None:
        return SourceAuthoritySnapshot()
    if job_item is None or source_item is None:
        raise RetentionBoundaryInvalidError(_BOUNDARY_INVALID)
    job = _parse_job_item(job_item, job_id=job_id)
    source = _parse_source_item(source_item, job_id=job_id)
    if job.owner_id != source.owner_id or job.source_artifact_fingerprint != source.fingerprint:
        raise RetentionBoundaryInvalidError(_BOUNDARY_INVALID)
    try:
        return SourceAuthoritySnapshot(job=job, source=source)
    except Exception:
        pass
    raise RetentionBoundaryInvalidError(_BOUNDARY_INVALID) from None


def _parse_job_item(item: Mapping[str, Any], *, job_id: str) -> ControlJobRecord:
    if (
        _av_string(item, "PK") != f"JOB#{job_id}"
        or _av_string(item, "SK") != "META"
        or _av_string(item, "entity_type") != "CONTROL_JOB"
    ):
        raise RetentionBoundaryInvalidError(_BOUNDARY_INVALID)
    payload = _av_string(item, "payload")
    try:
        job = ControlJobRecord.model_validate_json(payload, strict=True)
    except Exception:
        pass
    else:
        if (
            job.job_id == job_id
            and _av_string(item, "owner_id") == job.owner_id
            and _av_number(item, "record_version") == job.record_version
            and _av_string(item, "contract_version") == job.contract_version
        ):
            return job
    raise RetentionBoundaryInvalidError(_BOUNDARY_INVALID) from None


def _parse_source_item(item: Mapping[str, Any], *, job_id: str) -> SourceArtifactRecord:
    if (
        _av_string(item, "PK") != f"JOB#{job_id}"
        or _av_string(item, "SK") != "SOURCE"
        or _av_string(item, "entity_type") != "SOURCE_ARTIFACT"
    ):
        raise RetentionBoundaryInvalidError(_BOUNDARY_INVALID)
    payload = _av_string(item, "payload")
    try:
        source = SourceArtifactRecord.model_validate_json(payload, strict=True)
        source = validate_source_artifact_authority(source)
    except Exception:
        pass
    else:
        if (
            source.job_id == job_id
            and _av_string(item, "contract_version") == source.contract_version
        ):
            return source
    raise RetentionBoundaryInvalidError(_BOUNDARY_INVALID) from None


def _parse_checkpoint_item(item: Mapping[str, Any]) -> RetentionCheckpoint:
    if (
        _av_string(item, "PK") != RETENTION_CHECKPOINT_PARTITION_KEY
        or _av_string(item, "SK") != RETENTION_CHECKPOINT_SORT_KEY
        or _av_string(item, "entity_type") != _CHECKPOINT_ENTITY
    ):
        raise RetentionBoundaryInvalidError(_BOUNDARY_INVALID)
    payload = _av_string(item, "payload")
    if len(payload.encode("utf-8")) > _MAX_CHECKPOINT_PAYLOAD_BYTES:
        raise RetentionBoundaryInvalidError(_BOUNDARY_INVALID)
    try:
        checkpoint = RetentionCheckpoint.model_validate_json(payload, strict=True)
    except Exception:
        pass
    else:
        if (
            checkpoint.revision > 0
            and _av_number(item, "revision") == checkpoint.revision
            and _av_string(item, "contract_version") == checkpoint.contract_version
        ):
            return checkpoint
    raise RetentionBoundaryInvalidError(_BOUNDARY_INVALID) from None


def _strict_checkpoint(value: object) -> RetentionCheckpoint:
    if not isinstance(value, RetentionCheckpoint):
        raise RetentionBoundaryInvalidError(_BOUNDARY_INVALID)
    try:
        checkpoint = RetentionCheckpoint.model_validate(
            value.model_dump(mode="python"), strict=True
        )
    except Exception:
        pass
    else:
        if len(_checkpoint_payload(checkpoint).encode("utf-8")) <= _MAX_CHECKPOINT_PAYLOAD_BYTES:
            return checkpoint
    raise RetentionBoundaryInvalidError(_BOUNDARY_INVALID) from None


def _checkpoint_item(checkpoint: RetentionCheckpoint) -> dict[str, dict[str, str]]:
    return {
        "PK": _s(RETENTION_CHECKPOINT_PARTITION_KEY),
        "SK": _s(RETENTION_CHECKPOINT_SORT_KEY),
        "entity_type": _s(_CHECKPOINT_ENTITY),
        "contract_version": _s(checkpoint.contract_version),
        "revision": _n(checkpoint.revision),
        "payload": _s(_checkpoint_payload(checkpoint)),
    }


def _checkpoint_key() -> dict[str, dict[str, str]]:
    return {
        "PK": _s(RETENTION_CHECKPOINT_PARTITION_KEY),
        "SK": _s(RETENTION_CHECKPOINT_SORT_KEY),
    }


def _checkpoint_payload(checkpoint: RetentionCheckpoint) -> str:
    return checkpoint.model_dump_json()


def _encode_cursor(key_marker: str, version_marker: str) -> str:
    payload = json.dumps(
        {"key_marker": key_marker, "version_id_marker": version_marker},
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")
    encoded = base64.urlsafe_b64encode(payload).rstrip(b"=").decode("ascii")
    cursor = f"{_CURSOR_PREFIX}{encoded}"
    if len(cursor) > 2_048 or _CURSOR.fullmatch(cursor) is None:
        raise RetentionBoundaryInvalidError(_BOUNDARY_INVALID)
    return cursor


def _decode_cursor(cursor: str | None) -> tuple[str, str] | None:
    if cursor is None:
        return None
    if (
        not isinstance(cursor, str)
        or _CURSOR.fullmatch(cursor) is None
        or not cursor.startswith(_CURSOR_PREFIX)
    ):
        raise RetentionBoundaryInvalidError(_BOUNDARY_INVALID)
    encoded = cursor[len(_CURSOR_PREFIX) :]
    if not encoded:
        raise RetentionBoundaryInvalidError(_BOUNDARY_INVALID)
    try:
        padded = encoded + "=" * (-len(encoded) % 4)
        raw = base64.b64decode(padded, altchars=b"-_", validate=True)
        payload = json.loads(
            raw.decode("ascii"),
            object_pairs_hook=_unique_json_object,
            parse_constant=_reject_json_constant,
        )
    except Exception:
        pass
    else:
        if isinstance(payload, dict) and set(payload) == {"key_marker", "version_id_marker"}:
            key_marker = payload["key_marker"]
            version_marker = payload["version_id_marker"]
            try:
                _validate_source_identity(key_marker, version_marker)
            except RetentionBoundaryInvalidError:
                pass
            else:
                if _encode_cursor(key_marker, version_marker) == cursor:
                    return key_marker, version_marker
    raise RetentionBoundaryInvalidError(_BOUNDARY_INVALID) from None


def _validate_source_identity(object_key: object, version_id: object) -> None:
    if (
        not isinstance(object_key, str)
        or _SOURCE_KEY.fullmatch(object_key) is None
        or not isinstance(version_id, str)
        or _VERSION_ID.fullmatch(version_id) is None
        or version_id == "null"
    ):
        raise RetentionBoundaryInvalidError(_BOUNDARY_INVALID)


def _validate_s3_configuration(bucket: str, account_id: str) -> None:
    if not isinstance(bucket, str) or _BUCKET.fullmatch(bucket) is None:
        raise ValueError("Artifact bucket configuration is invalid")
    if not isinstance(account_id, str) or _ACCOUNT_ID.fullmatch(account_id) is None:
        raise ValueError("Artifact bucket owner configuration is invalid")


def _validate_table_name(table_name: str) -> None:
    if not isinstance(table_name, str) or _TABLE_NAME.fullmatch(table_name) is None:
        raise ValueError("Retention table configuration is invalid")


def _dependency_call[T](operation: Callable[[], T]) -> T:
    try:
        result = operation()
    except Exception:
        pass
    else:
        return result
    raise RetentionDependencyUnavailableError(_DEPENDENCY_UNAVAILABLE) from None


def _validate_write_response(response: object) -> None:
    if not isinstance(response, Mapping):
        raise RetentionBoundaryInvalidError(_BOUNDARY_INVALID)
    metadata = response.get("ResponseMetadata")
    status = metadata.get("HTTPStatusCode") if isinstance(metadata, Mapping) else None
    if type(status) is not int or status != 200:
        raise RetentionBoundaryInvalidError(_BOUNDARY_INVALID)


def _av_string(item: Mapping[str, Any], name: str) -> str:
    value = item.get(name)
    if not isinstance(value, Mapping) or set(value) != {"S"} or not isinstance(value.get("S"), str):
        raise RetentionBoundaryInvalidError(_BOUNDARY_INVALID)
    return value["S"]


def _av_number(item: Mapping[str, Any], name: str) -> int:
    value = item.get(name)
    raw = value.get("N") if isinstance(value, Mapping) and set(value) == {"N"} else None
    if not isinstance(raw, str) or re.fullmatch(r"0|[1-9][0-9]*", raw) is None:
        raise RetentionBoundaryInvalidError(_BOUNDARY_INVALID)
    return int(raw)


def _s(value: str) -> dict[str, str]:
    return {"S": value}


def _n(value: int) -> dict[str, str]:
    return {"N": str(value)}


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate cursor field")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> None:
    del value
    raise ValueError("nonstandard cursor value")


__all__ = [
    "DynamoDBRetentionCheckpointStore",
    "DynamoDBStrongSourceAuthorityReader",
    "DynamoRetentionClient",
    "RETENTION_CHECKPOINT_PARTITION_KEY",
    "RETENTION_CHECKPOINT_SORT_KEY",
    "S3SourceVersionInventory",
    "S3SourceVersionTagStore",
    "S3VersionInventoryClient",
    "S3VersionTaggingClient",
]
