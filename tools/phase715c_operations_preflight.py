"""Read-only Phase 7.15C due-work and recovery activation preflight.

The preflight accepts an injected DynamoDB read client and explicit, sanitized trigger
observations.  It constructs no AWS client, exposes no mutation method, never scans the table,
and never requests row bodies.  A passing result requires the exact frozen table/index shapes,
two independent empty COUNT queries for both publication indexes, and disabled trigger
readbacks whenever a deployed-disabled topology is supplied.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from hashlib import sha256
from typing import Any, Final, Protocol

from mr_lister.publication.orchestration_dynamodb import (
    PUBLICATION_DUE_WORK_INDEX,
    PUBLICATION_DUE_WORK_PARTITION,
    PUBLICATION_RECOVERY_INDEX,
    PUBLICATION_RECOVERY_PARTITION,
)

PREFLIGHT_FORMAT: Final = "mr-lister-phase7.15c-operations-preflight-v1"
PREFLIGHT_QUERY_ROUNDS: Final = 2

_TABLE_NAME = re.compile(r"^mr-lister-phase6-[a-z][a-z0-9-]{1,15}$")
_TABLE_ARN = re.compile(
    r"^arn:(?:aws|aws-us-gov|aws-cn):dynamodb:[a-z0-9-]+:[0-9]{12}:"
    r"table/mr-lister-phase6-[a-z][a-z0-9-]{1,15}$"
)
_ARN = re.compile(r"^arn:(?:aws|aws-us-gov|aws-cn):[a-z0-9-]+:[a-z0-9-]*:[0-9]*:.+$")
_UUID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{7,127}$")
_LOGICAL_ID = re.compile(r"^[A-Z][A-Za-z0-9]{2,127}$")
_RULE_NAME = re.compile(r"^[A-Za-z0-9._-]{1,64}$")

_EXPECTED_ATTRIBUTE_DEFINITIONS: Final = {
    ("PK", "S"),
    ("SK", "S"),
    ("dispatch_pk", "S"),
    ("dispatch_sk", "S"),
    ("owner_jobs_pk", "S"),
    ("owner_jobs_sk", "S"),
    ("recovery_pk", "S"),
    ("recovery_sk", "S"),
}
_EXPECTED_TABLE_KEY_SCHEMA: Final = (
    ("PK", "HASH"),
    ("SK", "RANGE"),
)
_EXPECTED_INDEXES: Final = {
    PUBLICATION_DUE_WORK_INDEX: (
        (("dispatch_pk", "HASH"), ("dispatch_sk", "RANGE")),
        "ALL",
    ),
    "OwnerJobsIndex": (
        (("owner_jobs_pk", "HASH"), ("owner_jobs_sk", "RANGE")),
        "ALL",
    ),
    PUBLICATION_RECOVERY_INDEX: (
        (("recovery_pk", "HASH"), ("recovery_sk", "RANGE")),
        "KEYS_ONLY",
    ),
}


class Phase715cOperationsPreflightError(RuntimeError):
    """A value-free failure for drift, unsafe inventory, or active trigger authority."""


class DynamoDBOperationsReadClient(Protocol):
    """The complete AWS surface permitted to the preflight."""

    def describe_table(self, **request: Any) -> Mapping[str, Any]: ...

    def query(self, **request: Any) -> Mapping[str, Any]: ...


@dataclass(frozen=True, slots=True)
class ExpectedEventSourceMapping:
    logical_id: str
    uuid: str
    function_arn: str
    event_source_arn: str

    def __post_init__(self) -> None:
        if (
            _LOGICAL_ID.fullmatch(self.logical_id) is None
            or _UUID.fullmatch(self.uuid) is None
            or _ARN.fullmatch(self.function_arn) is None
            or _ARN.fullmatch(self.event_source_arn) is None
        ):
            raise Phase715cOperationsPreflightError("Phase 7.15C trigger authority is invalid")


@dataclass(frozen=True, slots=True)
class ExpectedEventBridgeRule:
    logical_id: str
    name: str
    arn: str

    def __post_init__(self) -> None:
        if (
            _LOGICAL_ID.fullmatch(self.logical_id) is None
            or _RULE_NAME.fullmatch(self.name) is None
            or _ARN.fullmatch(self.arn) is None
        ):
            raise Phase715cOperationsPreflightError("Phase 7.15C trigger authority is invalid")


@dataclass(frozen=True, slots=True)
class OperationsPreflightAuthority:
    table_name: str
    table_arn: str
    event_source_mappings: tuple[ExpectedEventSourceMapping, ...] = ()
    eventbridge_rules: tuple[ExpectedEventBridgeRule, ...] = ()

    def __post_init__(self) -> None:
        if (
            _TABLE_NAME.fullmatch(self.table_name) is None
            or _TABLE_ARN.fullmatch(self.table_arn) is None
            or not self.table_arn.endswith(f"table/{self.table_name}")
            or len({item.logical_id for item in self.event_source_mappings})
            != len(self.event_source_mappings)
            or len({item.uuid for item in self.event_source_mappings})
            != len(self.event_source_mappings)
            or len({item.logical_id for item in self.eventbridge_rules})
            != len(self.eventbridge_rules)
            or len({item.arn for item in self.eventbridge_rules}) != len(self.eventbridge_rules)
        ):
            raise Phase715cOperationsPreflightError("Phase 7.15C preflight authority is invalid")


def _canonical(value: object) -> bytes:
    return (
        json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")


def _fingerprint(value: object) -> str:
    return sha256(_canonical(value)).hexdigest()


def _mapping(value: object) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError
    return value


def _key_schema(value: object) -> tuple[tuple[str, str], ...]:
    if not isinstance(value, list):
        raise ValueError
    result: list[tuple[str, str]] = []
    for item in value:
        row = _mapping(item)
        if set(row) != {"AttributeName", "KeyType"}:
            raise ValueError
        name = row.get("AttributeName")
        kind = row.get("KeyType")
        if not isinstance(name, str) or kind not in {"HASH", "RANGE"}:
            raise ValueError
        result.append((name, kind))
    return tuple(result)


def _validate_table(response: object, authority: OperationsPreflightAuthority) -> dict[str, Any]:
    envelope = _mapping(response)
    table = _mapping(envelope.get("Table"))
    if (
        table.get("TableName") != authority.table_name
        or table.get("TableArn") != authority.table_arn
        or table.get("TableStatus") != "ACTIVE"
        or _key_schema(table.get("KeySchema")) != _EXPECTED_TABLE_KEY_SCHEMA
    ):
        raise ValueError

    definitions = table.get("AttributeDefinitions")
    if not isinstance(definitions, list):
        raise ValueError
    actual_definitions: set[tuple[str, str]] = set()
    for item in definitions:
        row = _mapping(item)
        if set(row) != {"AttributeName", "AttributeType"}:
            raise ValueError
        name = row.get("AttributeName")
        kind = row.get("AttributeType")
        if not isinstance(name, str) or not isinstance(kind, str):
            raise ValueError
        actual_definitions.add((name, kind))
    if (
        len(definitions) != len(actual_definitions)
        or actual_definitions != _EXPECTED_ATTRIBUTE_DEFINITIONS
    ):
        raise ValueError

    raw_indexes = table.get("GlobalSecondaryIndexes")
    if not isinstance(raw_indexes, list):
        raise ValueError
    indexes: dict[str, Mapping[str, Any]] = {}
    for raw_index in raw_indexes:
        index = _mapping(raw_index)
        name = index.get("IndexName")
        if not isinstance(name, str) or name in indexes:
            raise ValueError
        indexes[name] = index
    if set(indexes) != set(_EXPECTED_INDEXES):
        raise ValueError
    normalized_indexes: dict[str, object] = {}
    for name, (expected_keys, expected_projection) in _EXPECTED_INDEXES.items():
        index = indexes[name]
        projection = _mapping(index.get("Projection"))
        if (
            index.get("IndexStatus") != "ACTIVE"
            or index.get("Backfilling") not in (None, False)
            or _key_schema(index.get("KeySchema")) != expected_keys
            or projection != {"ProjectionType": expected_projection}
        ):
            raise ValueError
        normalized_indexes[name] = {
            "key_schema": [list(item) for item in expected_keys],
            "projection": expected_projection,
            "status": "ACTIVE",
        }

    if table.get("StreamSpecification") != {
        "StreamEnabled": True,
        "StreamViewType": "KEYS_ONLY",
    }:
        raise ValueError
    metadata = envelope.get("ResponseMetadata")
    if metadata is not None and (
        not isinstance(metadata, Mapping) or metadata.get("HTTPStatusCode") != 200
    ):
        raise ValueError
    return {
        "attribute_definitions": [list(item) for item in sorted(actual_definitions)],
        "indexes": normalized_indexes,
        "key_schema": [list(item) for item in _EXPECTED_TABLE_KEY_SCHEMA],
        "stream_view_type": "KEYS_ONLY",
        "table_arn": authority.table_arn,
        "table_name": authority.table_name,
        "table_status": "ACTIVE",
    }


def _query_request(
    authority: OperationsPreflightAuthority,
    *,
    index_name: str,
    partition_attribute: str,
    partition_value: str,
) -> dict[str, object]:
    return {
        "TableName": authority.table_name,
        "IndexName": index_name,
        "KeyConditionExpression": "#partition = :partition",
        "ExpressionAttributeNames": {"#partition": partition_attribute},
        "ExpressionAttributeValues": {":partition": {"S": partition_value}},
        "ScanIndexForward": True,
        "Select": "COUNT",
        "Limit": 1,
    }


def _empty_query_response(response: object) -> None:
    value = _mapping(response)
    if not set(value).issubset({"Count", "ScannedCount", "LastEvaluatedKey", "ResponseMetadata"}):
        raise ValueError
    if (
        type(value.get("Count")) is not int
        or value.get("Count") != 0
        or type(value.get("ScannedCount")) is not int
        or value.get("ScannedCount") != 0
        or value.get("LastEvaluatedKey") is not None
    ):
        raise ValueError
    metadata = value.get("ResponseMetadata")
    if metadata is not None and (
        not isinstance(metadata, Mapping) or metadata.get("HTTPStatusCode") != 200
    ):
        raise ValueError


def _validate_trigger_observations(
    authority: OperationsPreflightAuthority,
    observations: object | None,
) -> dict[str, object]:
    expected_mappings = {item.logical_id: item for item in authority.event_source_mappings}
    expected_rules = {item.logical_id: item for item in authority.eventbridge_rules}
    if not expected_mappings and not expected_rules:
        if observations not in (None, {}):
            raise ValueError
        return {"mode": "SOURCE_ONLY_NOT_APPLICABLE", "readback_count": 0}
    value = _mapping(observations)
    if set(value) != {"event_source_mappings", "eventbridge_rules"}:
        raise ValueError
    raw_mappings = value.get("event_source_mappings")
    raw_rules = value.get("eventbridge_rules")
    if not isinstance(raw_mappings, list) or not isinstance(raw_rules, list):
        raise ValueError
    mappings: dict[str, Mapping[str, Any]] = {}
    for raw in raw_mappings:
        item = _mapping(raw)
        if set(item) != {
            "enabled",
            "event_source_arn",
            "function_arn",
            "logical_id",
            "state",
            "uuid",
        }:
            raise ValueError
        logical_id = item.get("logical_id")
        if not isinstance(logical_id, str) or logical_id in mappings:
            raise ValueError
        mappings[logical_id] = item
    if set(mappings) != set(expected_mappings):
        raise ValueError
    for logical_id, expected in expected_mappings.items():
        observed = mappings[logical_id]
        if observed != {
            "enabled": False,
            "event_source_arn": expected.event_source_arn,
            "function_arn": expected.function_arn,
            "logical_id": expected.logical_id,
            "state": "Disabled",
            "uuid": expected.uuid,
        }:
            raise ValueError

    rules: dict[str, Mapping[str, Any]] = {}
    for raw in raw_rules:
        item = _mapping(raw)
        if set(item) != {"arn", "logical_id", "name", "state"}:
            raise ValueError
        logical_id = item.get("logical_id")
        if not isinstance(logical_id, str) or logical_id in rules:
            raise ValueError
        rules[logical_id] = item
    if set(rules) != set(expected_rules):
        raise ValueError
    for logical_id, expected in expected_rules.items():
        if rules[logical_id] != {
            "arn": expected.arn,
            "logical_id": expected.logical_id,
            "name": expected.name,
            "state": "DISABLED",
        }:
            raise ValueError
    return {
        "mode": "DEPLOYED_DISABLED_READBACK",
        "readback_count": len(mappings) + len(rules),
    }


def run_phase715c_operations_preflight(
    *,
    client: DynamoDBOperationsReadClient,
    authority: OperationsPreflightAuthority,
    trigger_observations: object | None = None,
) -> dict[str, object]:
    """Return one sanitized digest-bound preflight after exact read-only checks."""

    try:
        if client is None or not isinstance(authority, OperationsPreflightAuthority):
            raise ValueError
        table = _validate_table(
            client.describe_table(TableName=authority.table_name),
            authority,
        )
        due_request = _query_request(
            authority,
            index_name=PUBLICATION_DUE_WORK_INDEX,
            partition_attribute="dispatch_pk",
            partition_value=PUBLICATION_DUE_WORK_PARTITION,
        )
        recovery_request = _query_request(
            authority,
            index_name=PUBLICATION_RECOVERY_INDEX,
            partition_attribute="recovery_pk",
            partition_value=PUBLICATION_RECOVERY_PARTITION,
        )
        for _round in range(PREFLIGHT_QUERY_ROUNDS):
            _empty_query_response(client.query(**due_request))
            _empty_query_response(client.query(**recovery_request))
        triggers = _validate_trigger_observations(authority, trigger_observations)
        evidence: dict[str, object] = {
            "format": PREFLIGHT_FORMAT,
            "queries": {
                "due": {
                    "empty_observations": PREFLIGHT_QUERY_ROUNDS,
                    "index_name": PUBLICATION_DUE_WORK_INDEX,
                    "partition": PUBLICATION_DUE_WORK_PARTITION,
                    "request_sha256": _fingerprint(due_request),
                },
                "recovery": {
                    "empty_observations": PREFLIGHT_QUERY_ROUNDS,
                    "index_name": PUBLICATION_RECOVERY_INDEX,
                    "partition": PUBLICATION_RECOVERY_PARTITION,
                    "request_sha256": _fingerprint(recovery_request),
                },
            },
            "result": "passed",
            "table": table,
            "triggers": triggers,
        }
        evidence["evidence_sha256"] = _fingerprint(evidence)
        return evidence
    except Phase715cOperationsPreflightError:
        raise
    except Exception:
        raise Phase715cOperationsPreflightError(
            "Phase 7.15C operations preflight failed safely"
        ) from None


__all__ = [
    "DynamoDBOperationsReadClient",
    "ExpectedEventBridgeRule",
    "ExpectedEventSourceMapping",
    "OperationsPreflightAuthority",
    "PREFLIGHT_FORMAT",
    "PREFLIGHT_QUERY_ROUNDS",
    "PUBLICATION_RECOVERY_INDEX",
    "PUBLICATION_RECOVERY_PARTITION",
    "Phase715cOperationsPreflightError",
    "run_phase715c_operations_preflight",
]
