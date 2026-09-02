"""Adversarial tests for the fixed Phase 7.15C AWS preflight adapter."""

from __future__ import annotations

import inspect
import json
from typing import Any

import pytest

import tools.phase715c_operations_preflight_aws as adapter
from tools.phase715c_operations_preflight_aws import (
    Phase715cOperationsPreflightAwsError,
    run_phase715c_operations_preflight_aws,
)

PRIVATE_MARKER = "private-live-value-never-print"
STREAM_ARN = f"{adapter.TABLE_ARN}/stream/2026-09-01T18:01:13.000"


def _table_response() -> dict[str, Any]:
    return {
        "Table": {
            "TableName": adapter.TABLE_NAME,
            "TableArn": adapter.TABLE_ARN,
            "TableStatus": "ACTIVE",
            "LatestStreamArn": STREAM_ARN,
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
                    "IndexName": "ExecutionRecoveryIndex",
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
            "PrivateMarker": PRIVATE_MARKER,
        },
        "ResponseMetadata": {"HTTPStatusCode": 200},
    }


class RecordingCloudFormation:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def describe_stacks(self, **request: Any) -> dict[str, Any]:
        self.calls.append(request)
        name = request["StackName"]
        if name == adapter.PHASE6_STACK_NAME:
            stack = {
                "StackId": (
                    f"arn:aws:cloudformation:{adapter.REGION}:{adapter.ACCOUNT_ID}:"
                    f"stack/{name}/00000000-0000-4000-8000-000000000006"
                ),
                "StackName": name,
                "StackStatus": "UPDATE_COMPLETE",
                "Outputs": [
                    {"OutputKey": "StateTableName", "OutputValue": adapter.TABLE_NAME},
                    {"OutputKey": "PrivateOutput", "OutputValue": PRIVATE_MARKER},
                ],
            }
        else:
            stack = {
                "StackId": (
                    f"arn:aws:cloudformation:{adapter.REGION}:{adapter.ACCOUNT_ID}:"
                    f"stack/{name}/00000000-0000-4000-8000-000000000007"
                ),
                "StackName": name,
                "StackStatus": "CREATE_COMPLETE",
                "Parameters": [
                    {"ParameterKey": "ActivationMode", "ParameterValue": "PRODUCTION_DISABLED"},
                    {"ParameterKey": "EnvironmentName", "ParameterValue": "dev"},
                    {"ParameterKey": "StateTableArn", "ParameterValue": adapter.TABLE_ARN},
                    {"ParameterKey": "StateTableStreamArn", "ParameterValue": STREAM_ARN},
                    {"ParameterKey": "PrivateParameter", "ParameterValue": PRIVATE_MARKER},
                ],
                "Outputs": [
                    {"OutputKey": "DeploymentReadiness", "OutputValue": "PRODUCTION_DISABLED"},
                    {"OutputKey": "ResourceInstantiationPossible", "OutputValue": "true"},
                    {"OutputKey": "PublicationQueryRegistered", "OutputValue": "false"},
                    {"OutputKey": "PublicationRequestRegistered", "OutputValue": "false"},
                    {"OutputKey": "PublicationWorkerTriggered", "OutputValue": "false"},
                    {"OutputKey": "SellerPublicationEnabled", "OutputValue": "false"},
                    {"OutputKey": "ProviderMutationEnabled", "OutputValue": "false"},
                ],
            }
        return {"Stacks": [stack], "ResponseMetadata": {"HTTPStatusCode": 200}}


class RecordingDynamoDB:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def describe_table(self, **request: Any) -> dict[str, Any]:
        self.calls.append(("describe_table", request))
        return _table_response()

    def query(self, **request: Any) -> dict[str, Any]:
        self.calls.append(("query", request))
        return {
            "Count": 0,
            "ScannedCount": 0,
            "ResponseMetadata": {"HTTPStatusCode": 200},
        }


class RecordingLambda:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.rows: dict[str, dict[str, Any]] = {}
        for index, spec in enumerate(adapter._MAPPING_SPECS, start=1):
            source = (
                STREAM_ARN if spec.event_source == "phase6_stream" else adapter.RECOVERY_QUEUE_ARN
            )
            uuid = f"00000000-0000-4000-8000-{index:012d}"
            self.rows[spec.function_name] = {
                "UUID": uuid,
                "FunctionArn": spec.function_arn,
                "EventSourceArn": source,
                "State": "Disabled",
                "PrivateMarker": PRIVATE_MARKER,
            }

    def list_event_source_mappings(self, **request: Any) -> dict[str, Any]:
        self.calls.append(("list_event_source_mappings", request))
        return {
            "EventSourceMappings": [self.rows[request["FunctionName"]]],
            "ResponseMetadata": {"HTTPStatusCode": 200},
        }

    def get_event_source_mapping(self, **request: Any) -> dict[str, Any]:
        self.calls.append(("get_event_source_mapping", request))
        return next(row for row in self.rows.values() if row["UUID"] == request["UUID"])


class RecordingEvents:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def describe_rule(self, **request: Any) -> dict[str, Any]:
        self.calls.append(request)
        spec = next(item for item in adapter._RULE_SPECS if item.name == request["Name"])
        return {
            "Name": spec.name,
            "Arn": spec.arn,
            "State": "DISABLED",
            "EventBusName": "default",
            "PrivateMarker": PRIVATE_MARKER,
        }


class RecordingProvider:
    def __init__(self) -> None:
        self.cloudformation = RecordingCloudFormation()
        self.dynamodb = RecordingDynamoDB()
        self.lambda_client = RecordingLambda()
        self.events = RecordingEvents()
        self.calls: list[str] = []

    def client(self, service_name: str) -> object:
        self.calls.append(service_name)
        return {
            "cloudformation": self.cloudformation,
            "dynamodb": self.dynamodb,
            "events": self.events,
            "lambda": self.lambda_client,
        }[service_name]


def test_adapter_uses_only_exact_read_calls_and_emits_identifier_free_evidence() -> None:
    provider = RecordingProvider()

    result = run_phase715c_operations_preflight_aws(provider=provider)

    assert {spec.logical_id for spec in adapter._MAPPING_SPECS} == {
        "PublicationDispatcherStreamMapping",
        "PublicationRecoveryFunctionRecoveryQueue",
        "PublicationRetentionStreamMapping",
    }
    assert provider.calls == ["cloudformation", "lambda", "events", "dynamodb"]
    assert provider.cloudformation.calls == [
        {"StackName": adapter.PHASE6_STACK_NAME},
        {"StackName": adapter.PHASE7_STACK_NAME},
    ]
    assert [name for name, _request in provider.dynamodb.calls] == [
        "describe_table",
        "query",
        "query",
        "query",
        "query",
    ]
    assert provider.events.calls == [
        {"Name": spec.name, "EventBusName": "default"} for spec in adapter._RULE_SPECS
    ]
    assert [name for name, _request in provider.lambda_client.calls] == [
        call
        for _spec in adapter._MAPPING_SPECS
        for call in ("list_event_source_mappings", "get_event_source_mapping")
    ]
    assert result == {
        "authority_sha256": result["authority_sha256"],
        "core_evidence_sha256": result["core_evidence_sha256"],
        "empty_query_readback_count": 4,
        "event_source_mapping_readback_count": 3,
        "eventbridge_rule_readback_count": 3,
        "format": adapter.ADAPTER_FORMAT,
        "result": "passed",
        "trigger_readback_count": 6,
    }
    public = json.dumps(result, sort_keys=True)
    raw_identifiers = {
        PRIVATE_MARKER,
        STREAM_ARN,
        adapter.ACCOUNT_ID,
        adapter.PHASE6_STACK_NAME,
        adapter.PHASE7_STACK_NAME,
        adapter.TABLE_ARN,
        *(row["UUID"] for row in provider.lambda_client.rows.values()),
        *(spec.function_arn for spec in adapter._MAPPING_SPECS),
        *(spec.arn for spec in adapter._RULE_SPECS),
    }
    assert all(identifier not in public for identifier in raw_identifiers)


def test_active_or_expanded_mapping_fails_closed_without_query_or_identifier_leak() -> None:
    provider = RecordingProvider()
    first = next(iter(provider.lambda_client.rows.values()))
    first["State"] = "Enabled"

    with pytest.raises(Phase715cOperationsPreflightAwsError, match="failed safely") as captured:
        run_phase715c_operations_preflight_aws(provider=provider)

    assert captured.value.__cause__ is None
    assert PRIVATE_MARKER not in str(captured.value)
    assert first["UUID"] not in str(captured.value)
    assert provider.dynamodb.calls == []


def test_adapter_has_closed_clients_and_no_aws_mutation_or_arbitrary_resource_cli() -> None:
    source = inspect.getsource(adapter)
    normalized = source.replace("_", "").lower()

    assert adapter._Boto3Provider._SERVICES == {
        "cloudformation",
        "dynamodb",
        "events",
        "lambda",
    }
    for forbidden in (
        "deleteitem(",
        "enable_rule(".replace("_", ""),
        "invoke(",
        "putitem(",
        "scan(",
        "sendmessage(",
        "startmessage move".replace(" ", ""),
        "updateitem(",
    ):
        assert forbidden not in normalized

    constructed = False

    def provider_factory() -> RecordingProvider:
        nonlocal constructed
        constructed = True
        return RecordingProvider()

    with pytest.raises(SystemExit):
        adapter.main(["--stack", "other-stack"], provider_factory=provider_factory)
    assert constructed is False


def test_cli_prints_only_the_sanitized_adapter_document(capsys: pytest.CaptureFixture[str]) -> None:
    provider = RecordingProvider()

    assert adapter.main([], provider_factory=lambda: provider) == 0

    output = capsys.readouterr().out
    document = json.loads(output)
    assert document["result"] == "passed"
    assert PRIVATE_MARKER not in output
    assert STREAM_ARN not in output
    assert adapter.ACCOUNT_ID not in output
