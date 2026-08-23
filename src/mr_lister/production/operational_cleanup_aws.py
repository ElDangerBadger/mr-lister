"""Least-capability DynamoDB adapters for terminal operational-record cleanup.

The adapters can scan projected control-job authority, query exact job/owner partitions, assign
the existing table TTL attribute, and persist one CAS checkpoint.  They expose no delete, object,
secret, provider, Bedrock, AgentCore, orchestration, or raw-record payload write capability.
"""

from __future__ import annotations

import base64
import json
import re
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from hashlib import sha256
from typing import Any, Literal, Protocol

from mr_lister.control.models import ControlJobRecord
from mr_lister.production.operational_cleanup import (
    DEFAULT_TERMINAL_OPERATIONAL_RETENTION,
    OperationalCleanupAuthorityChangedError,
    OperationalCleanupBoundaryInvalidError,
    OperationalCleanupCheckpoint,
    OperationalCleanupDependencyUnavailableError,
    OperationalExpiryPage,
    OperationalJobSearchPage,
    TerminalJobAuthority,
)

OPERATIONAL_CLEANUP_CHECKPOINT_PARTITION_KEY = "SYSTEM#TERMINAL_OPERATIONAL_CLEANUP"
OPERATIONAL_CLEANUP_CHECKPOINT_SORT_KEY = "CHECKPOINT"

_CHECKPOINT_ENTITY = "TERMINAL_OPERATIONAL_CLEANUP_CHECKPOINT"
_DEPENDENCY_UNAVAILABLE = "Operational cleanup dependency is unavailable"
_BOUNDARY_INVALID = "Operational cleanup AWS response is invalid"
_AUTHORITY_CHANGED = "Operational cleanup authority changed"
_SCAN_CURSOR_PREFIX = "ddb-terminal-scan-v1."
_ASSIGNMENT_CURSOR_PREFIX = "ddb-terminal-assignment-v1."
_OWNER_START_CURSOR = f"{_ASSIGNMENT_CURSOR_PREFIX}owner"
_MAX_CHECKPOINT_PAYLOAD_BYTES = 64 * 1024

_TABLE_NAME = re.compile(r"^[A-Za-z0-9_.-]{3,255}$")
_SAFE_KEY = re.compile(r"^[\x20-\x7e]{1,2048}$")
_SAFE_CURSOR = re.compile(r"^[A-Za-z0-9._-]{1,4096}$")
_JOB_PK = re.compile(r"^JOB#(?P<job_id>[A-Za-z0-9][A-Za-z0-9_-]{0,127})$")
_OWNER_PK = re.compile(r"^OWNER#[a-f0-9]{64}$")

_JOB_ENTITY_TYPES = frozenset(
    {
        "CONTROL_JOB",
        "SOURCE_ARTIFACT",
        "DOMAIN_EVENT",
        "WORK_REQUEST",
        "REVIEW",
        "ARTWORK_ANALYSIS",
        "AGENT_PREPARATION_EVIDENCE",
        "REVIEW_DECISION",
        "CANCELLATION_DECISION",
        "PRODUCT_SYNC",
        "PROVIDER_UPLOAD_ATTEMPT",
        "UPLOADED_ARTWORK",
        "PROVIDER_WRITE_ATTEMPT",
        "PROVIDER_CALL_PERMIT",
        "RECONCILIATION_OBSERVATION",
        "UPLOAD_RECONCILIATION_OBSERVATION",
        "PRICING_SNAPSHOT",
        "PRICING_EVIDENCE",
        "FAILURE",
    }
)
_OWNER_RECEIPT_ENTITY_TYPES = frozenset({"COMMAND_RECEIPT", "UPLOAD_RECEIPT"})


class DynamoOperationalCleanupClient(Protocol):
    def scan(self, **kwargs: Any) -> Mapping[str, Any]: ...

    def query(self, **kwargs: Any) -> Mapping[str, Any]: ...

    def get_item(self, **kwargs: Any) -> Mapping[str, Any]: ...

    def put_item(self, **kwargs: Any) -> Mapping[str, Any]: ...

    def transact_write_items(self, **kwargs: Any) -> Mapping[str, Any]: ...


class DynamoDBOperationalJobInventory:
    """Strong scan returning only the first projected control job in a bounded page."""

    __slots__ = ("_client", "_table_name")

    def __init__(self, *, client: DynamoOperationalCleanupClient, table_name: str) -> None:
        _validate_table_name(table_name)
        self._client = client
        self._table_name = table_name

    def search_next_job(
        self,
        *,
        cursor: str | None,
        limit: int,
    ) -> OperationalJobSearchPage:
        if type(limit) is not int or not 1 <= limit <= 1_000:
            raise OperationalCleanupBoundaryInvalidError(_BOUNDARY_INVALID)
        exclusive_start_key = _decode_scan_cursor(cursor)
        request: dict[str, Any] = {
            "TableName": self._table_name,
            "ConsistentRead": True,
            "Limit": limit,
            "FilterExpression": "#entity_type = :control_job",
            "ProjectionExpression": (
                "PK, SK, #entity_type, contract_version, owner_id, #state, "
                "record_version, event_sequence, payload"
            ),
            "ExpressionAttributeNames": {
                "#entity_type": "entity_type",
                "#state": "state",
            },
            "ExpressionAttributeValues": {":control_job": _s("CONTROL_JOB")},
        }
        if exclusive_start_key is not None:
            request["ExclusiveStartKey"] = exclusive_start_key
        response = _dependency_call(lambda: self._client.scan(**request))
        return _parse_scan_page(
            response,
            limit=limit,
            prior_key=exclusive_start_key,
        )


class DynamoDBTerminalOperationalExpiryStore:
    """Assign one expiry to exact job rows and matching owner receipt rows."""

    __slots__ = ("_client", "_table_name")

    def __init__(self, *, client: DynamoOperationalCleanupClient, table_name: str) -> None:
        _validate_table_name(table_name)
        self._client = client
        self._table_name = table_name

    def assign_terminal_expiry(
        self,
        *,
        authority: TerminalJobAuthority,
        expires_at_epoch_seconds: int,
        cursor: str | None,
        limit: int,
    ) -> OperationalExpiryPage:
        exact = _strict_authority(authority)
        expected_expiry = int(
            (exact.terminal_updated_at + DEFAULT_TERMINAL_OPERATIONAL_RETENTION).timestamp()
        )
        if (
            type(expires_at_epoch_seconds) is not int
            or not 1 <= expires_at_epoch_seconds <= 253_402_300_799
            or expires_at_epoch_seconds != expected_expiry
            or type(limit) is not int
            or not 1 <= limit <= 24
        ):
            raise OperationalCleanupBoundaryInvalidError(_BOUNDARY_INVALID)
        phase, exclusive_start_key = _decode_assignment_cursor(cursor)
        if phase == "job":
            partition_key = f"JOB#{exact.job_id}"
        else:
            partition_key = f"OWNER#{exact.owner_id}"
        request: dict[str, Any] = {
            "TableName": self._table_name,
            "KeyConditionExpression": "PK = :partition_key",
            "ExpressionAttributeValues": {":partition_key": _s(partition_key)},
            "ProjectionExpression": (
                "PK, SK, #entity_type, expires_at, job_id, owner_id, #state, "
                "record_version, event_sequence"
            ),
            "ExpressionAttributeNames": {
                "#entity_type": "entity_type",
                "#state": "state",
            },
            "ConsistentRead": True,
            "ScanIndexForward": True,
            "Limit": limit,
        }
        if exclusive_start_key is not None:
            request["ExclusiveStartKey"] = exclusive_start_key
        response = _dependency_call(lambda: self._client.query(**request))
        rows, last_key = _parse_query_page(
            response,
            partition_key=partition_key,
            limit=limit,
        )
        selected = _select_expiry_rows(
            rows,
            phase=phase,
            authority=exact,
            expiry=expires_at_epoch_seconds,
        )
        if phase == "job" and not rows:
            self._assert_authority_current(exact)
            raise OperationalCleanupAuthorityChangedError(_AUTHORITY_CHANGED)
        if selected:
            self._transact_expiry(
                authority=exact,
                expiry=expires_at_epoch_seconds,
                rows=selected,
                phase=phase,
                cursor=cursor,
            )

        if last_key is not None:
            next_cursor = _encode_assignment_cursor(phase, last_key)
        elif phase == "job":
            next_cursor = _OWNER_START_CURSOR
        else:
            next_cursor = None
        return OperationalExpiryPage(
            records_examined=len(rows),
            records_assigned=len(selected),
            next_cursor=next_cursor,
        )

    def _transact_expiry(
        self,
        *,
        authority: TerminalJobAuthority,
        expiry: int,
        rows: tuple[tuple[str, str, str], ...],
        phase: Literal["job", "owner"],
        cursor: str | None,
    ) -> None:
        operations: list[dict[str, Any]] = []
        meta_in_page = False
        for partition_key, sort_key, entity_type in rows:
            values: dict[str, Any] = {
                ":entity_type": _s(entity_type),
                ":expiry": _n(expiry),
            }
            names = {"#entity_type": "entity_type"}
            condition = (
                "#entity_type = :entity_type AND "
                "(attribute_not_exists(expires_at) OR expires_at = :expiry)"
            )
            if phase == "owner":
                condition += " AND job_id = :job_id"
                values[":job_id"] = _s(authority.job_id)
            elif sort_key == "META":
                meta_in_page = True
                condition += (
                    " AND owner_id = :owner_id AND #state = :state"
                    " AND record_version = :record_version"
                    " AND event_sequence = :event_sequence"
                )
                names["#state"] = "state"
                values.update(_authority_values(authority))
            operations.append(
                {
                    "Update": {
                        "TableName": self._table_name,
                        "Key": {"PK": _s(partition_key), "SK": _s(sort_key)},
                        "UpdateExpression": "SET expires_at = :expiry",
                        "ConditionExpression": condition,
                        "ExpressionAttributeNames": names,
                        "ExpressionAttributeValues": values,
                    }
                }
            )
        if not meta_in_page:
            operations.insert(0, self._authority_condition(authority))
        if len(operations) > 25:
            raise OperationalCleanupBoundaryInvalidError(_BOUNDARY_INVALID)
        token_material = json.dumps(
            {
                "job": authority.job_id,
                "version": authority.record_version,
                "sequence": authority.event_sequence,
                "expiry": expiry,
                "phase": phase,
                "cursor": cursor,
                "keys": [(row[0], row[1]) for row in rows],
            },
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("ascii")
        try:
            response = self._client.transact_write_items(
                TransactItems=operations,
                ClientRequestToken=sha256(token_material).hexdigest()[:32],
            )
        except Exception as error:
            if _error_code(error) == "TransactionCanceledException":
                self._assert_authority_current(authority)
            raise OperationalCleanupDependencyUnavailableError(_DEPENDENCY_UNAVAILABLE) from None
        _validate_write_response(response)

    def _authority_condition(self, authority: TerminalJobAuthority) -> dict[str, Any]:
        return {
            "ConditionCheck": {
                "TableName": self._table_name,
                "Key": {"PK": _s(f"JOB#{authority.job_id}"), "SK": _s("META")},
                "ConditionExpression": (
                    "#entity_type = :control_job AND owner_id = :owner_id"
                    " AND #state = :state AND record_version = :record_version"
                    " AND event_sequence = :event_sequence"
                ),
                "ExpressionAttributeNames": {
                    "#entity_type": "entity_type",
                    "#state": "state",
                },
                "ExpressionAttributeValues": {
                    ":control_job": _s("CONTROL_JOB"),
                    **_authority_values(authority),
                },
            }
        }

    def _assert_authority_current(self, authority: TerminalJobAuthority) -> None:
        try:
            response = self._client.get_item(
                TableName=self._table_name,
                Key={"PK": _s(f"JOB#{authority.job_id}"), "SK": _s("META")},
                ConsistentRead=True,
                ProjectionExpression=(
                    "PK, SK, #entity_type, owner_id, #state, record_version, event_sequence"
                ),
                ExpressionAttributeNames={
                    "#entity_type": "entity_type",
                    "#state": "state",
                },
            )
        except Exception:
            raise OperationalCleanupDependencyUnavailableError(_DEPENDENCY_UNAVAILABLE) from None
        if not isinstance(response, Mapping):
            raise OperationalCleanupBoundaryInvalidError(_BOUNDARY_INVALID)
        item = response.get("Item")
        if not isinstance(item, Mapping) or not _item_matches_authority(item, authority):
            raise OperationalCleanupAuthorityChangedError(_AUTHORITY_CHANGED)


class DynamoDBOperationalCleanupCheckpointStore:
    """Persist the bounded cleanup continuation behind exact revision/payload CAS."""

    __slots__ = ("_client", "_table_name")

    def __init__(self, *, client: DynamoOperationalCleanupClient, table_name: str) -> None:
        _validate_table_name(table_name)
        self._client = client
        self._table_name = table_name

    def load_checkpoint(self) -> OperationalCleanupCheckpoint:
        response = _dependency_call(
            lambda: self._client.get_item(
                TableName=self._table_name,
                Key=_checkpoint_key(),
                ConsistentRead=True,
            )
        )
        if not isinstance(response, Mapping):
            raise OperationalCleanupBoundaryInvalidError(_BOUNDARY_INVALID)
        if "Item" not in response:
            return OperationalCleanupCheckpoint()
        item = response.get("Item")
        if not isinstance(item, Mapping):
            raise OperationalCleanupBoundaryInvalidError(_BOUNDARY_INVALID)
        return _parse_checkpoint_item(item)

    def save_checkpoint(
        self,
        *,
        expected: OperationalCleanupCheckpoint,
        updated: OperationalCleanupCheckpoint,
    ) -> None:
        current = _strict_checkpoint(expected)
        replacement = _strict_checkpoint(updated)
        if replacement.revision != current.revision + 1:
            raise OperationalCleanupBoundaryInvalidError(_BOUNDARY_INVALID)
        if current.revision == 0 and current != OperationalCleanupCheckpoint():
            raise OperationalCleanupBoundaryInvalidError(_BOUNDARY_INVALID)
        if current.revision == 0:
            condition = "attribute_not_exists(PK)"
            values: dict[str, Any] | None = None
        else:
            condition = (
                "#entity_type = :entity_type AND revision = :expected_revision"
                " AND payload = :expected_payload"
            )
            values = {
                ":entity_type": _s(_CHECKPOINT_ENTITY),
                ":expected_revision": _n(current.revision),
                ":expected_payload": _s(current.model_dump_json()),
            }
        request: dict[str, Any] = {
            "TableName": self._table_name,
            "Item": _checkpoint_item(replacement),
            "ConditionExpression": condition,
        }
        if values is not None:
            request["ExpressionAttributeNames"] = {"#entity_type": "entity_type"}
            request["ExpressionAttributeValues"] = values
        _validate_write_response(_dependency_call(lambda: self._client.put_item(**request)))


def _parse_scan_page(
    response: object,
    *,
    limit: int,
    prior_key: Mapping[str, Any] | None,
) -> OperationalJobSearchPage:
    if not isinstance(response, Mapping):
        raise OperationalCleanupBoundaryInvalidError(_BOUNDARY_INVALID)
    items = response.get("Items")
    count = response.get("Count")
    scanned_count = response.get("ScannedCount")
    if (
        not isinstance(items, list)
        or type(count) is not int
        or type(scanned_count) is not int
        or count != len(items)
        or not 0 <= count <= scanned_count <= limit
    ):
        raise OperationalCleanupBoundaryInvalidError(_BOUNDARY_INVALID)
    observed_at = _response_observed_at(response)
    parsed: list[tuple[ControlJobRecord, dict[str, Any]]] = []
    for raw in items:
        if not isinstance(raw, Mapping):
            raise OperationalCleanupBoundaryInvalidError(_BOUNDARY_INVALID)
        job = _parse_control_job_item(raw)
        parsed.append((job, _key_from_item(raw)))
    last_key = _parse_last_evaluated_key(response.get("LastEvaluatedKey"))
    if prior_key is not None and last_key == prior_key:
        raise OperationalCleanupBoundaryInvalidError(_BOUNDARY_INVALID)
    job = parsed[0][0] if parsed else None
    if parsed and (len(parsed) > 1 or last_key is not None):
        next_cursor = _encode_scan_cursor(parsed[0][1])
    elif not parsed and last_key is not None:
        next_cursor = _encode_scan_cursor(last_key)
    else:
        next_cursor = None
    return OperationalJobSearchPage(
        observed_at=observed_at,
        job=job,
        records_scanned=scanned_count,
        next_cursor=next_cursor,
    )


def _parse_query_page(
    response: object,
    *,
    partition_key: str,
    limit: int,
) -> tuple[tuple[Mapping[str, Any], ...], dict[str, Any] | None]:
    if not isinstance(response, Mapping):
        raise OperationalCleanupBoundaryInvalidError(_BOUNDARY_INVALID)
    items = response.get("Items")
    count = response.get("Count")
    scanned = response.get("ScannedCount", count)
    if (
        not isinstance(items, list)
        or type(count) is not int
        or type(scanned) is not int
        or count != len(items)
        or scanned != count
        or not 0 <= count <= limit
    ):
        raise OperationalCleanupBoundaryInvalidError(_BOUNDARY_INVALID)
    rows: list[Mapping[str, Any]] = []
    for raw in items:
        if not isinstance(raw, Mapping) or _av_string(raw, "PK") != partition_key:
            raise OperationalCleanupBoundaryInvalidError(_BOUNDARY_INVALID)
        _key_from_item(raw)
        rows.append(raw)
    return tuple(rows), _parse_last_evaluated_key(response.get("LastEvaluatedKey"))


def _select_expiry_rows(
    rows: tuple[Mapping[str, Any], ...],
    *,
    phase: Literal["job", "owner"],
    authority: TerminalJobAuthority,
    expiry: int,
) -> tuple[tuple[str, str, str], ...]:
    selected: list[tuple[str, str, str]] = []
    for row in rows:
        partition_key = _av_string(row, "PK")
        sort_key = _av_string(row, "SK")
        entity_type = _av_string(row, "entity_type")
        existing_expiry = _optional_av_number(row, "expires_at")
        if phase == "job":
            if partition_key != f"JOB#{authority.job_id}" or entity_type not in _JOB_ENTITY_TYPES:
                raise OperationalCleanupBoundaryInvalidError(_BOUNDARY_INVALID)
            if existing_expiry is not None and existing_expiry != expiry:
                raise OperationalCleanupBoundaryInvalidError(_BOUNDARY_INVALID)
            if sort_key == "META" and not _item_matches_authority(row, authority):
                raise OperationalCleanupAuthorityChangedError(_AUTHORITY_CHANGED)
            selected.append((partition_key, sort_key, entity_type))
            continue
        if partition_key != f"OWNER#{authority.owner_id}":
            raise OperationalCleanupBoundaryInvalidError(_BOUNDARY_INVALID)
        raw_job_id = _optional_av_string(row, "job_id")
        if raw_job_id != authority.job_id:
            continue
        if entity_type not in _OWNER_RECEIPT_ENTITY_TYPES:
            raise OperationalCleanupBoundaryInvalidError(_BOUNDARY_INVALID)
        if existing_expiry is not None and existing_expiry != expiry:
            raise OperationalCleanupBoundaryInvalidError(_BOUNDARY_INVALID)
        selected.append((partition_key, sort_key, entity_type))
    return tuple(selected)


def _parse_control_job_item(item: Mapping[str, Any]) -> ControlJobRecord:
    partition_key = _av_string(item, "PK")
    match = _JOB_PK.fullmatch(partition_key)
    if (
        match is None
        or _av_string(item, "SK") != "META"
        or _av_string(item, "entity_type") != "CONTROL_JOB"
    ):
        raise OperationalCleanupBoundaryInvalidError(_BOUNDARY_INVALID)
    try:
        job = ControlJobRecord.model_validate_json(_av_string(item, "payload"), strict=True)
    except Exception:
        raise OperationalCleanupBoundaryInvalidError(_BOUNDARY_INVALID) from None
    if (
        job.job_id != match.group("job_id")
        or _av_string(item, "contract_version") != job.contract_version
        or _av_string(item, "owner_id") != job.owner_id
        or _av_string(item, "state") != job.state.value
        or _av_number(item, "record_version") != job.record_version
        or _av_number(item, "event_sequence") != job.event_sequence
        or job.updated_at.utcoffset() != timedelta(0)
    ):
        raise OperationalCleanupBoundaryInvalidError(_BOUNDARY_INVALID)
    return job


def _item_matches_authority(
    item: Mapping[str, Any],
    authority: TerminalJobAuthority,
) -> bool:
    try:
        return (
            _av_string(item, "PK") == f"JOB#{authority.job_id}"
            and _av_string(item, "SK") == "META"
            and _av_string(item, "entity_type") == "CONTROL_JOB"
            and _av_string(item, "owner_id") == authority.owner_id
            and _av_string(item, "state") == authority.state.value
            and _av_number(item, "record_version") == authority.record_version
            and _av_number(item, "event_sequence") == authority.event_sequence
        )
    except OperationalCleanupBoundaryInvalidError:
        return False


def _strict_authority(value: object) -> TerminalJobAuthority:
    if not isinstance(value, TerminalJobAuthority):
        raise OperationalCleanupBoundaryInvalidError(_BOUNDARY_INVALID)
    try:
        authority = TerminalJobAuthority.model_validate(
            value.model_dump(mode="python"), strict=True
        )
    except Exception:
        raise OperationalCleanupBoundaryInvalidError(_BOUNDARY_INVALID) from None
    if authority.terminal_updated_at.utcoffset() != timedelta(0):
        raise OperationalCleanupBoundaryInvalidError(_BOUNDARY_INVALID)
    return authority


def _strict_checkpoint(value: object) -> OperationalCleanupCheckpoint:
    if not isinstance(value, OperationalCleanupCheckpoint):
        raise OperationalCleanupBoundaryInvalidError(_BOUNDARY_INVALID)
    try:
        checkpoint = OperationalCleanupCheckpoint.model_validate(
            value.model_dump(mode="python"), strict=True
        )
    except Exception:
        raise OperationalCleanupBoundaryInvalidError(_BOUNDARY_INVALID) from None
    if len(checkpoint.model_dump_json().encode("utf-8")) > _MAX_CHECKPOINT_PAYLOAD_BYTES:
        raise OperationalCleanupBoundaryInvalidError(_BOUNDARY_INVALID)
    return checkpoint


def _parse_checkpoint_item(item: Mapping[str, Any]) -> OperationalCleanupCheckpoint:
    if (
        _av_string(item, "PK") != OPERATIONAL_CLEANUP_CHECKPOINT_PARTITION_KEY
        or _av_string(item, "SK") != OPERATIONAL_CLEANUP_CHECKPOINT_SORT_KEY
        or _av_string(item, "entity_type") != _CHECKPOINT_ENTITY
    ):
        raise OperationalCleanupBoundaryInvalidError(_BOUNDARY_INVALID)
    payload = _av_string(item, "payload")
    if len(payload.encode("utf-8")) > _MAX_CHECKPOINT_PAYLOAD_BYTES:
        raise OperationalCleanupBoundaryInvalidError(_BOUNDARY_INVALID)
    try:
        checkpoint = OperationalCleanupCheckpoint.model_validate_json(payload, strict=True)
    except Exception:
        raise OperationalCleanupBoundaryInvalidError(_BOUNDARY_INVALID) from None
    if (
        checkpoint.revision <= 0
        or _av_number(item, "revision") != checkpoint.revision
        or _av_string(item, "contract_version") != checkpoint.contract_version
    ):
        raise OperationalCleanupBoundaryInvalidError(_BOUNDARY_INVALID)
    return checkpoint


def _checkpoint_item(
    checkpoint: OperationalCleanupCheckpoint,
) -> dict[str, dict[str, str]]:
    return {
        "PK": _s(OPERATIONAL_CLEANUP_CHECKPOINT_PARTITION_KEY),
        "SK": _s(OPERATIONAL_CLEANUP_CHECKPOINT_SORT_KEY),
        "entity_type": _s(_CHECKPOINT_ENTITY),
        "contract_version": _s(checkpoint.contract_version),
        "revision": _n(checkpoint.revision),
        "payload": _s(checkpoint.model_dump_json()),
    }


def _checkpoint_key() -> dict[str, dict[str, str]]:
    return {
        "PK": _s(OPERATIONAL_CLEANUP_CHECKPOINT_PARTITION_KEY),
        "SK": _s(OPERATIONAL_CLEANUP_CHECKPOINT_SORT_KEY),
    }


def _encode_scan_cursor(key: Mapping[str, Any]) -> str:
    return _encode_cursor(_SCAN_CURSOR_PREFIX, {"key": _plain_key(key)})


def _decode_scan_cursor(cursor: str | None) -> dict[str, Any] | None:
    if cursor is None:
        return None
    payload = _decode_cursor(_SCAN_CURSOR_PREFIX, cursor)
    if set(payload) != {"key"} or not isinstance(payload["key"], dict):
        raise OperationalCleanupBoundaryInvalidError(_BOUNDARY_INVALID)
    key = _typed_key(payload["key"])
    if _encode_scan_cursor(key) != cursor:
        raise OperationalCleanupBoundaryInvalidError(_BOUNDARY_INVALID)
    return key


def _encode_assignment_cursor(
    phase: Literal["job", "owner"],
    key: Mapping[str, Any],
) -> str:
    return _encode_cursor(
        _ASSIGNMENT_CURSOR_PREFIX,
        {"phase": phase, "key": _plain_key(key)},
    )


def _decode_assignment_cursor(
    cursor: str | None,
) -> tuple[Literal["job", "owner"], dict[str, Any] | None]:
    if cursor is None:
        return "job", None
    if cursor == _OWNER_START_CURSOR:
        return "owner", None
    payload = _decode_cursor(_ASSIGNMENT_CURSOR_PREFIX, cursor)
    if set(payload) != {"phase", "key"} or payload["phase"] not in {"job", "owner"}:
        raise OperationalCleanupBoundaryInvalidError(_BOUNDARY_INVALID)
    if not isinstance(payload["key"], dict):
        raise OperationalCleanupBoundaryInvalidError(_BOUNDARY_INVALID)
    phase: Literal["job", "owner"] = payload["phase"]
    key = _typed_key(payload["key"])
    if _encode_assignment_cursor(phase, key) != cursor:
        raise OperationalCleanupBoundaryInvalidError(_BOUNDARY_INVALID)
    return phase, key


def _encode_cursor(prefix: str, payload: Mapping[str, Any]) -> str:
    raw = json.dumps(
        payload,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")
    encoded = base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")
    cursor = f"{prefix}{encoded}"
    if len(cursor) > 4096 or _SAFE_CURSOR.fullmatch(cursor) is None:
        raise OperationalCleanupBoundaryInvalidError(_BOUNDARY_INVALID)
    return cursor


def _decode_cursor(prefix: str, cursor: str) -> dict[str, Any]:
    if (
        not isinstance(cursor, str)
        or _SAFE_CURSOR.fullmatch(cursor) is None
        or not cursor.startswith(prefix)
    ):
        raise OperationalCleanupBoundaryInvalidError(_BOUNDARY_INVALID)
    encoded = cursor[len(prefix) :]
    if not encoded:
        raise OperationalCleanupBoundaryInvalidError(_BOUNDARY_INVALID)
    try:
        raw = base64.b64decode(
            encoded + "=" * (-len(encoded) % 4),
            altchars=b"-_",
            validate=True,
        )
        payload = json.loads(
            raw.decode("ascii"),
            object_pairs_hook=_unique_json_object,
            parse_constant=_reject_json_constant,
        )
    except Exception:
        raise OperationalCleanupBoundaryInvalidError(_BOUNDARY_INVALID) from None
    if not isinstance(payload, dict):
        raise OperationalCleanupBoundaryInvalidError(_BOUNDARY_INVALID)
    return payload


def _plain_key(key: Mapping[str, Any]) -> dict[str, str]:
    partition_key = _av_string(key, "PK")
    sort_key = _av_string(key, "SK")
    if _SAFE_KEY.fullmatch(partition_key) is None or _SAFE_KEY.fullmatch(sort_key) is None:
        raise OperationalCleanupBoundaryInvalidError(_BOUNDARY_INVALID)
    return {"PK": partition_key, "SK": sort_key}


def _typed_key(key: Mapping[str, Any]) -> dict[str, dict[str, str]]:
    if set(key) != {"PK", "SK"}:
        raise OperationalCleanupBoundaryInvalidError(_BOUNDARY_INVALID)
    partition_key = key.get("PK")
    sort_key = key.get("SK")
    if (
        not isinstance(partition_key, str)
        or _SAFE_KEY.fullmatch(partition_key) is None
        or not isinstance(sort_key, str)
        or _SAFE_KEY.fullmatch(sort_key) is None
    ):
        raise OperationalCleanupBoundaryInvalidError(_BOUNDARY_INVALID)
    return {"PK": _s(partition_key), "SK": _s(sort_key)}


def _parse_last_evaluated_key(value: object) -> dict[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, Mapping) or set(value) != {"PK", "SK"}:
        raise OperationalCleanupBoundaryInvalidError(_BOUNDARY_INVALID)
    _plain_key(value)
    return {"PK": value["PK"], "SK": value["SK"]}


def _key_from_item(item: Mapping[str, Any]) -> dict[str, Any]:
    key = {"PK": item.get("PK"), "SK": item.get("SK")}
    _plain_key(key)
    return key


def _response_observed_at(response: Mapping[str, Any]) -> datetime:
    metadata = response.get("ResponseMetadata")
    headers = metadata.get("HTTPHeaders") if isinstance(metadata, Mapping) else None
    if not isinstance(headers, Mapping):
        raise OperationalCleanupBoundaryInvalidError(_BOUNDARY_INVALID)
    dates = [value for key, value in headers.items() if str(key).casefold() == "date"]
    if len(dates) != 1 or not isinstance(dates[0], str):
        raise OperationalCleanupBoundaryInvalidError(_BOUNDARY_INVALID)
    try:
        observed_at = parsedate_to_datetime(dates[0])
    except Exception:
        raise OperationalCleanupBoundaryInvalidError(_BOUNDARY_INVALID) from None
    if observed_at.utcoffset() != timedelta(0):
        raise OperationalCleanupBoundaryInvalidError(_BOUNDARY_INVALID)
    return observed_at.astimezone(UTC)


def _authority_values(authority: TerminalJobAuthority) -> dict[str, Any]:
    return {
        ":owner_id": _s(authority.owner_id),
        ":state": _s(authority.state.value),
        ":record_version": _n(authority.record_version),
        ":event_sequence": _n(authority.event_sequence),
    }


def _av_string(item: Mapping[str, Any], name: str) -> str:
    value = item.get(name)
    if not isinstance(value, Mapping) or set(value) != {"S"}:
        raise OperationalCleanupBoundaryInvalidError(_BOUNDARY_INVALID)
    raw = value.get("S")
    if not isinstance(raw, str):
        raise OperationalCleanupBoundaryInvalidError(_BOUNDARY_INVALID)
    return raw


def _optional_av_string(item: Mapping[str, Any], name: str) -> str | None:
    if name not in item:
        return None
    return _av_string(item, name)


def _av_number(item: Mapping[str, Any], name: str) -> int:
    value = item.get(name)
    if not isinstance(value, Mapping) or set(value) != {"N"}:
        raise OperationalCleanupBoundaryInvalidError(_BOUNDARY_INVALID)
    raw = value.get("N")
    if not isinstance(raw, str) or not raw.isascii() or not raw.isdigit():
        raise OperationalCleanupBoundaryInvalidError(_BOUNDARY_INVALID)
    parsed = int(raw)
    if str(parsed) != raw:
        raise OperationalCleanupBoundaryInvalidError(_BOUNDARY_INVALID)
    return parsed


def _optional_av_number(item: Mapping[str, Any], name: str) -> int | None:
    if name not in item:
        return None
    return _av_number(item, name)


def _s(value: str) -> dict[str, str]:
    return {"S": value}


def _n(value: int) -> dict[str, str]:
    return {"N": str(value)}


def _validate_table_name(table_name: str) -> None:
    if not isinstance(table_name, str) or _TABLE_NAME.fullmatch(table_name) is None:
        raise ValueError("Operational cleanup table configuration is invalid")


def _dependency_call[T](operation: Callable[[], T]) -> T:
    try:
        return operation()
    except Exception:
        raise OperationalCleanupDependencyUnavailableError(_DEPENDENCY_UNAVAILABLE) from None


def _validate_write_response(response: object) -> None:
    if not isinstance(response, Mapping):
        raise OperationalCleanupBoundaryInvalidError(_BOUNDARY_INVALID)
    metadata = response.get("ResponseMetadata")
    if not isinstance(metadata, Mapping) or metadata.get("HTTPStatusCode") != 200:
        raise OperationalCleanupBoundaryInvalidError(_BOUNDARY_INVALID)


def _error_code(error: Exception) -> str | None:
    response = getattr(error, "response", None)
    if not isinstance(response, Mapping):
        return None
    envelope = response.get("Error")
    if not isinstance(envelope, Mapping):
        return None
    code = envelope.get("Code")
    return code if isinstance(code, str) else None


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> None:
    raise ValueError(value)


__all__ = [
    "DynamoDBOperationalCleanupCheckpointStore",
    "DynamoDBOperationalJobInventory",
    "DynamoDBTerminalOperationalExpiryStore",
    "DynamoOperationalCleanupClient",
    "OPERATIONAL_CLEANUP_CHECKPOINT_PARTITION_KEY",
    "OPERATIONAL_CLEANUP_CHECKPOINT_SORT_KEY",
]
