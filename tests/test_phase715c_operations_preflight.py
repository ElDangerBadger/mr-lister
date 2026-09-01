"""Adversarial tests for the read-only Phase 7.15C operations preflight."""

from __future__ import annotations

import ast
import copy
import inspect
from typing import Any

import pytest

import tools.phase715c_operations_preflight as preflight
from mr_lister.publication.orchestration_dynamodb import (
    PUBLICATION_RECOVERY_INDEX,
    PUBLICATION_RECOVERY_PARTITION,
)
from tools.phase715c_operations_preflight import (
    ExpectedEventBridgeRule,
    ExpectedEventSourceMapping,
    OperationsPreflightAuthority,
    Phase715cOperationsPreflightError,
    run_phase715c_operations_preflight,
)

TABLE_NAME = "mr-lister-phase6-dev"
TABLE_ARN = f"arn:aws:dynamodb:us-west-2:123456789012:table/{TABLE_NAME}"
FUNCTION_ARN = "arn:aws:lambda:us-west-2:123456789012:function:phase7-dispatcher"
STREAM_ARN = f"{TABLE_ARN}/stream/2026-09-01T00:00:00.000"
RULE_ARN = "arn:aws:events:us-west-2:123456789012:rule/phase7-due-sweep"


def _authority(*, deployed_triggers: bool = False) -> OperationsPreflightAuthority:
    if not deployed_triggers:
        return OperationsPreflightAuthority(table_name=TABLE_NAME, table_arn=TABLE_ARN)
    return OperationsPreflightAuthority(
        table_name=TABLE_NAME,
        table_arn=TABLE_ARN,
        event_source_mappings=(
            ExpectedEventSourceMapping(
                logical_id="PublicationDispatcherStreamMapping",
                uuid="00000000-0000-4000-8000-000000000001",
                function_arn=FUNCTION_ARN,
                event_source_arn=STREAM_ARN,
            ),
        ),
        eventbridge_rules=(
            ExpectedEventBridgeRule(
                logical_id="PublicationDueSweepRule",
                name="phase7-due-sweep",
                arn=RULE_ARN,
            ),
        ),
    )


def _table_response() -> dict[str, Any]:
    return {
        "Table": {
            "TableName": TABLE_NAME,
            "TableArn": TABLE_ARN,
            "TableStatus": "ACTIVE",
            "AttributeDefinitions": [
                {"AttributeName": name, "AttributeType": "S"}
                for name in (
                    "PK",
                    "SK",
                    "dispatch_pk",
                    "dispatch_sk",
                    "owner_jobs_pk",
                    "owner_jobs_sk",
                    "recovery_pk",
                    "recovery_sk",
                )
            ],
            "KeySchema": [
                {"AttributeName": "PK", "KeyType": "HASH"},
                {"AttributeName": "SK", "KeyType": "RANGE"},
            ],
            "GlobalSecondaryIndexes": [
                {
                    "IndexName": "DueWorkIndex",
                    "IndexStatus": "ACTIVE",
                    "KeySchema": [
                        {"AttributeName": "dispatch_pk", "KeyType": "HASH"},
                        {"AttributeName": "dispatch_sk", "KeyType": "RANGE"},
                    ],
                    "Projection": {"ProjectionType": "ALL"},
                },
                {
                    "IndexName": "OwnerJobsIndex",
                    "IndexStatus": "ACTIVE",
                    "KeySchema": [
                        {"AttributeName": "owner_jobs_pk", "KeyType": "HASH"},
                        {"AttributeName": "owner_jobs_sk", "KeyType": "RANGE"},
                    ],
                    "Projection": {"ProjectionType": "ALL"},
                },
                {
                    "IndexName": PUBLICATION_RECOVERY_INDEX,
                    "IndexStatus": "ACTIVE",
                    "KeySchema": [
                        {"AttributeName": "recovery_pk", "KeyType": "HASH"},
                        {"AttributeName": "recovery_sk", "KeyType": "RANGE"},
                    ],
                    "Projection": {"ProjectionType": "KEYS_ONLY"},
                },
            ],
            "StreamSpecification": {
                "StreamEnabled": True,
                "StreamViewType": "KEYS_ONLY",
            },
        },
        "ResponseMetadata": {"HTTPStatusCode": 200},
    }


def _empty_query() -> dict[str, Any]:
    return {
        "Count": 0,
        "ScannedCount": 0,
        "ResponseMetadata": {"HTTPStatusCode": 200},
    }


class RecordingReadClient:
    def __init__(
        self,
        *,
        table_response: object | None = None,
        query_responses: list[object] | None = None,
    ) -> None:
        self.table_response = table_response if table_response is not None else _table_response()
        self.query_responses = query_responses or [_empty_query() for _ in range(4)]
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def describe_table(self, **request: Any) -> Any:
        self.calls.append(("describe_table", request))
        return self.table_response

    def query(self, **request: Any) -> Any:
        self.calls.append(("query", request))
        return self.query_responses.pop(0)


def _trigger_observations() -> dict[str, Any]:
    return {
        "event_source_mappings": [
            {
                "enabled": False,
                "event_source_arn": STREAM_ARN,
                "function_arn": FUNCTION_ARN,
                "logical_id": "PublicationDispatcherStreamMapping",
                "state": "Disabled",
                "uuid": "00000000-0000-4000-8000-000000000001",
            }
        ],
        "eventbridge_rules": [
            {
                "arn": RULE_ARN,
                "logical_id": "PublicationDueSweepRule",
                "name": "phase7-due-sweep",
                "state": "DISABLED",
            }
        ],
    }


def test_preflight_uses_one_describe_and_exact_empty_count_queries_twice() -> None:
    client = RecordingReadClient()

    evidence = run_phase715c_operations_preflight(client=client, authority=_authority())

    due = {
        "TableName": TABLE_NAME,
        "IndexName": "DueWorkIndex",
        "KeyConditionExpression": "#partition = :partition",
        "ExpressionAttributeNames": {"#partition": "dispatch_pk"},
        "ExpressionAttributeValues": {":partition": {"S": "PUBLICATION_WORK_DUE#0"}},
        "ScanIndexForward": True,
        "Select": "COUNT",
        "Limit": 1,
    }
    recovery = {
        "TableName": TABLE_NAME,
        "IndexName": PUBLICATION_RECOVERY_INDEX,
        "KeyConditionExpression": "#partition = :partition",
        "ExpressionAttributeNames": {"#partition": "recovery_pk"},
        "ExpressionAttributeValues": {":partition": {"S": PUBLICATION_RECOVERY_PARTITION}},
        "ScanIndexForward": True,
        "Select": "COUNT",
        "Limit": 1,
    }
    assert client.calls == [
        ("describe_table", {"TableName": TABLE_NAME}),
        ("query", due),
        ("query", recovery),
        ("query", due),
        ("query", recovery),
    ]
    assert evidence["result"] == "passed"
    assert evidence["triggers"] == {
        "mode": "SOURCE_ONLY_NOT_APPLICABLE",
        "readback_count": 0,
    }
    assert len(str(evidence["evidence_sha256"])) == 64
    assert "Items" not in repr(evidence)


@pytest.mark.parametrize(
    ("path", "replacement"),
    [
        (("Table", "TableStatus"), "UPDATING"),
        (("Table", "StreamSpecification", "StreamViewType"), "NEW_IMAGE"),
        (("Table", "GlobalSecondaryIndexes", 0, "IndexStatus"), "CREATING"),
        (("Table", "GlobalSecondaryIndexes", 0, "Projection", "ProjectionType"), "KEYS_ONLY"),
        (
            ("Table", "GlobalSecondaryIndexes", 2, "KeySchema", 1, "AttributeName"),
            "wrong_sk",
        ),
    ],
)
def test_preflight_rejects_any_required_table_or_index_drift_without_values(
    path: tuple[object, ...],
    replacement: object,
) -> None:
    response = copy.deepcopy(_table_response())
    target: Any = response
    for component in path[:-1]:
        target = target[component]
    target[path[-1]] = replacement
    client = RecordingReadClient(table_response=response)

    with pytest.raises(Phase715cOperationsPreflightError, match="failed safely") as captured:
        run_phase715c_operations_preflight(client=client, authority=_authority())

    assert captured.value.__cause__ is None
    assert str(replacement) not in str(captured.value)
    assert [name for name, _request in client.calls] == ["describe_table"]


def test_preflight_rejects_duplicate_attribute_definitions() -> None:
    response = copy.deepcopy(_table_response())
    response["Table"]["AttributeDefinitions"].append({"AttributeName": "PK", "AttributeType": "S"})

    with pytest.raises(Phase715cOperationsPreflightError, match="failed safely"):
        run_phase715c_operations_preflight(
            client=RecordingReadClient(table_response=response),
            authority=_authority(),
        )


@pytest.mark.parametrize(
    "unsafe_response",
    [
        {"Count": 1, "ScannedCount": 1},
        {"Count": 0, "ScannedCount": 0, "LastEvaluatedKey": {}},
        {"Count": 0, "ScannedCount": 0, "Items": []},
        {"Count": False, "ScannedCount": 0},
        {"Count": 0, "ScannedCount": 0, "ResponseMetadata": {"HTTPStatusCode": 500}},
    ],
)
def test_preflight_rejects_rows_cursors_ambiguous_types_and_response_expansion(
    unsafe_response: object,
) -> None:
    client = RecordingReadClient(
        query_responses=[_empty_query(), _empty_query(), unsafe_response, _empty_query()]
    )

    with pytest.raises(Phase715cOperationsPreflightError, match="failed safely"):
        run_phase715c_operations_preflight(client=client, authority=_authority())

    assert [name for name, _request in client.calls].count("query") == 3


def test_preflight_accepts_only_exact_disabled_trigger_readbacks() -> None:
    client = RecordingReadClient()
    evidence = run_phase715c_operations_preflight(
        client=client,
        authority=_authority(deployed_triggers=True),
        trigger_observations=_trigger_observations(),
    )
    assert evidence["triggers"] == {
        "mode": "DEPLOYED_DISABLED_READBACK",
        "readback_count": 2,
    }

    active = _trigger_observations()
    active["event_source_mappings"][0]["enabled"] = True
    active["event_source_mappings"][0]["state"] = "Enabled"
    with pytest.raises(Phase715cOperationsPreflightError, match="failed safely"):
        run_phase715c_operations_preflight(
            client=RecordingReadClient(),
            authority=_authority(deployed_triggers=True),
            trigger_observations=active,
        )


def test_preflight_rejects_unbound_trigger_observations_in_source_only_mode() -> None:
    with pytest.raises(Phase715cOperationsPreflightError, match="failed safely"):
        run_phase715c_operations_preflight(
            client=RecordingReadClient(),
            authority=_authority(),
            trigger_observations=_trigger_observations(),
        )


def test_preflight_source_has_only_injected_describe_and_query_aws_calls() -> None:
    source = inspect.getsource(preflight)
    tree = ast.parse(source)
    client_calls = {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "client"
    }

    assert client_calls == {"describe_table", "query"}
    assert "boto3" not in source
    assert "start_message_move_task" not in source.lower()
    assert "PUBLICATION_WORK_RECOVERY#0" not in source
    assert preflight.PUBLICATION_RECOVERY_INDEX == PUBLICATION_RECOVERY_INDEX
    assert preflight.PUBLICATION_RECOVERY_PARTITION == PUBLICATION_RECOVERY_PARTITION
