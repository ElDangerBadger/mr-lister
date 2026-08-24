from __future__ import annotations

import importlib.util
import json
import re
import sys
from collections.abc import Iterator, Mapping
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
PHASE7 = ROOT / "infra" / "phase7"
TEMPLATE = PHASE7 / "template.json"
SHIM = PHASE7 / "lambda" / "phase7_lambda.py"
README = PHASE7 / "README.md"

EXPECTED_DISABLED_ENVIRONMENT = {
    "MR_LISTER_PHASE7_SCAFFOLD_ONLY": "true",
    "MR_LISTER_PHASE7_PUBLICATION_ENABLED": "false",
    "MR_LISTER_PHASE7_REQUEST_ENABLED": "false",
    "MR_LISTER_PHASE7_QUERY_ENABLED": "false",
}


def _template() -> dict[str, Any]:
    return json.loads(TEMPLATE.read_text(encoding="utf-8"))


def _resources() -> dict[str, dict[str, Any]]:
    return _template()["Resources"]


def _walk(value: object) -> Iterator[object]:
    yield value
    if isinstance(value, dict):
        for key, nested in value.items():
            yield key
            yield from _walk(nested)
    elif isinstance(value, list):
        for nested in value:
            yield from _walk(nested)


def _load_shim(name: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, SHIM)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _inline_statements() -> dict[str, dict[str, Any]]:
    policies = _resources()["PublicationStatusQueryFunctionRole"]["Properties"]["Policies"]
    assert len(policies) == 1
    return {statement["Sid"]: statement for statement in policies[0]["PolicyDocument"]["Statement"]}


def test_phase74_is_a_separate_hard_disabled_sam_application() -> None:
    template = _template()

    assert template["Transform"] == "AWS::Serverless-2016-10-31"
    assert set(template["Parameters"]) == {"EnvironmentName"}
    environment_pattern = template["Parameters"]["EnvironmentName"]["AllowedPattern"]
    assert re.fullmatch(environment_pattern, "dev")
    assert re.fullmatch(environment_pattern, "prod-west")
    assert not re.fullmatch(environment_pattern, "Prod")
    assert not re.fullmatch(environment_pattern, "x")

    variables = template["Globals"]["Function"]["Environment"]["Variables"]
    assert variables == EXPECTED_DISABLED_ENVIRONMENT | {
        "MR_LISTER_ENVIRONMENT": {"Ref": "EnvironmentName"},
        "MR_LISTER_AWS_ACCOUNT_ID": {"Ref": "AWS::AccountId"},
        "MR_LISTER_STATE_TABLE": {"Fn::Sub": "mr-lister-phase6-${EnvironmentName}"},
    }
    for marker in EXPECTED_DISABLED_ENVIRONMENT:
        assert not isinstance(variables[marker], dict)
        assert marker not in template["Parameters"]
    assert set(variables).isdisjoint(
        {
            "MR_LISTER_RELEASE_FINGERPRINT",
            "MR_LISTER_COGNITO_ISSUER",
            "MR_LISTER_COGNITO_CLIENT_ID",
            "MR_LISTER_COGNITO_SCOPE",
            "MR_LISTER_COGNITO_GROUP",
            "MR_LISTER_PRODUCT_PROFILE_ID",
            "MR_LISTER_PRODUCT_PROFILE_VERSION",
            "MR_LISTER_PRODUCT_PROFILE_FINGERPRINT",
            "MR_LISTER_PRODUCT_PROFILE_PATH",
        }
    )

    outputs = template["Outputs"]
    assert outputs["DeploymentReadiness"]["Value"] == "SCAFFOLD_ONLY"
    assert outputs["PublicationStatusQueryRegistered"]["Value"] == "false"
    assert outputs["PublicationStatusQueryEnabled"]["Value"] == "false"
    assert outputs["PublicationRequestEnabled"]["Value"] == "false"
    assert outputs["PublicationEnabled"]["Value"] == "false"


def test_exactly_one_bounded_query_lambda_exists_without_any_invocation_surface() -> None:
    template = _template()
    resources = template["Resources"]
    functions = {
        name: resource
        for name, resource in resources.items()
        if resource["Type"] == "AWS::Serverless::Function"
    }

    assert set(functions) == {"PublicationStatusQueryFunction"}
    function = functions["PublicationStatusQueryFunction"]
    properties = function["Properties"]
    assert function["DependsOn"] == "PublicationStatusQueryLogGroup"
    assert properties["FunctionName"] == {
        "Fn::Sub": "mr-lister-phase7-${EnvironmentName}-publication-status-query"
    }
    assert properties["CodeUri"] == "lambda/"
    assert properties["Handler"] == "phase7_lambda.publication_query_api_handler"
    assert properties["Role"] == {"Fn::GetAtt": ["PublicationStatusQueryFunctionRole", "Arn"]}
    assert properties["ReservedConcurrentExecutions"] == 1
    assert properties["Timeout"] == 10
    assert "Events" not in properties
    for forbidden_property in (
        "DeadLetterQueue",
        "EventInvokeConfig",
        "FunctionUrlConfig",
        "Layers",
        "VpcConfig",
    ):
        assert forbidden_property not in properties

    assert template["Globals"]["Function"] | {} == {
        "Runtime": "python3.12",
        "Architectures": ["arm64"],
        "MemorySize": 256,
        "Environment": template["Globals"]["Function"]["Environment"],
    }
    forbidden_resource_types = {
        "AWS::ApiGateway::RestApi",
        "AWS::ApiGatewayV2::Api",
        "AWS::Lambda::Permission",
        "AWS::Lambda::Url",
        "AWS::Serverless::Api",
        "AWS::Serverless::HttpApi",
        "AWS::Serverless::StateMachine",
    }
    assert not {resource["Type"] for resource in resources.values()} & forbidden_resource_types

    serialized = json.dumps(template, sort_keys=True).casefold()
    for forbidden in (
        '"events"',
        "functionurl",
        "httpapi",
        "/v1/",
        "printify",
        "provider",
        "coordinator",
        "dispatcher",
        "state machine",
        "publish.json",
    ):
        assert forbidden not in serialized


def test_query_role_has_only_exact_log_and_dynamodb_read_permissions() -> None:
    resources = _resources()
    roles = {
        name: resource
        for name, resource in resources.items()
        if resource["Type"] == "AWS::IAM::Role"
    }
    assert set(roles) == {"PublicationStatusQueryFunctionRole"}
    role = roles["PublicationStatusQueryFunctionRole"]["Properties"]
    assert "ManagedPolicyArns" not in role
    assert role["AssumeRolePolicyDocument"] == {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Principal": {"Service": "lambda.amazonaws.com"},
                "Action": "sts:AssumeRole",
            }
        ],
    }

    statements = _inline_statements()
    assert set(statements) == {
        "WritePublicationStatusQueryLogs",
        "ReadExactPhase6OperationalState",
    }
    assert statements["WritePublicationStatusQueryLogs"] == {
        "Sid": "WritePublicationStatusQueryLogs",
        "Effect": "Allow",
        "Action": ["logs:CreateLogStream", "logs:PutLogEvents"],
        "Resource": {"Fn::Sub": "${PublicationStatusQueryLogGroup.Arn}:*"},
    }
    assert statements["ReadExactPhase6OperationalState"] == {
        "Sid": "ReadExactPhase6OperationalState",
        "Effect": "Allow",
        "Action": ["dynamodb:GetItem", "dynamodb:Query"],
        "Resource": {
            "Fn::Sub": (
                "arn:${AWS::Partition}:dynamodb:${AWS::Region}:${AWS::AccountId}:table/"
                "mr-lister-phase6-${EnvironmentName}"
            )
        },
        "Condition": {
            "ForAllValues:StringLike": {"dynamodb:LeadingKeys": ["JOB#*", "PUBLICATION#*"]}
        },
    }

    serialized = json.dumps(role, sort_keys=True).casefold()
    for forbidden in (
        "dynamodb:batch",
        "dynamodb:delete",
        "dynamodb:execute",
        "dynamodb:partiql",
        "dynamodb:put",
        "dynamodb:scan",
        "dynamodb:transact",
        "dynamodb:update",
        "secretsmanager:",
        "s3:",
        "states:",
        "lambda:invoke",
        "execute-api:",
        "bedrock",
        "ec2:",
        "kms:",
        '"resource": "*"',
    ):
        assert forbidden not in serialized


def test_query_observability_is_bounded_retained_and_actionable() -> None:
    resources = _resources()
    log_group = resources["PublicationStatusQueryLogGroup"]
    assert log_group["DeletionPolicy"] == "Retain"
    assert log_group["UpdateReplacePolicy"] == "Retain"
    assert log_group["Properties"]["LogGroupName"] == {
        "Fn::Sub": "/aws/lambda/mr-lister-phase7-${EnvironmentName}-publication-status-query"
    }
    assert log_group["Properties"]["RetentionInDays"] == 14

    alarm_ids = {
        name for name, resource in resources.items() if resource["Type"] == "AWS::CloudWatch::Alarm"
    }
    assert alarm_ids == {
        "PublicationStatusQueryErrorsAlarm",
        "PublicationStatusQueryThrottlesAlarm",
        "PublicationStatusQueryDurationAlarm",
    }
    expected_metrics = {
        "PublicationStatusQueryErrorsAlarm": ("Errors", "Sum", 0),
        "PublicationStatusQueryThrottlesAlarm": ("Throttles", "Sum", 0),
        "PublicationStatusQueryDurationAlarm": ("Duration", "Maximum", 8000),
    }
    for alarm_id, (metric, statistic, threshold) in expected_metrics.items():
        alarm = resources[alarm_id]["Properties"]
        assert alarm["ActionsEnabled"] is True
        assert alarm["AlarmActions"] == [{"Ref": "PublicationStatusAlarmTopic"}]
        assert alarm["Namespace"] == "AWS/Lambda"
        assert alarm["MetricName"] == metric
        assert alarm["Statistic"] == statistic
        assert alarm["Threshold"] == threshold
        assert alarm["Period"] == 300
        assert alarm["EvaluationPeriods"] == 1
        assert alarm["DatapointsToAlarm"] == 1
        assert alarm["TreatMissingData"] == "notBreaching"
        assert alarm["Dimensions"] == [
            {"Name": "FunctionName", "Value": {"Ref": "PublicationStatusQueryFunction"}}
        ]

    topic = resources["PublicationStatusAlarmTopic"]
    assert topic["DeletionPolicy"] == "Retain"
    assert topic["UpdateReplacePolicy"] == "Retain"
    assert topic["Properties"]["KmsMasterKeyId"] == {
        "Fn::GetAtt": ["PublicationStatusAlarmTopicKey", "Arn"]
    }
    assert "Subscription" not in topic["Properties"]
    policy = resources["PublicationStatusAlarmTopicPolicy"]["Properties"]["PolicyDocument"]
    assert len(policy["Statement"]) == 1
    statement = policy["Statement"][0]
    assert statement["Principal"] == {"Service": "cloudwatch.amazonaws.com"}
    assert statement["Action"] == "sns:Publish"
    assert statement["Resource"] == {"Ref": "PublicationStatusAlarmTopic"}
    assert "mr-lister-phase7-${EnvironmentName}-publication-status-*" in json.dumps(
        statement["Condition"]
    )


class _ExplosiveEvent(Mapping[str, Any]):
    def __getitem__(self, key: str) -> Any:
        raise AssertionError(f"scaffold inspected event key {key!r}")

    def __iter__(self) -> Iterator[str]:
        raise AssertionError("scaffold iterated the event")

    def __len__(self) -> int:
        raise AssertionError("scaffold measured the event")


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("MR_LISTER_PHASE7_SCAFFOLD_ONLY", None),
        ("MR_LISTER_PHASE7_SCAFFOLD_ONLY", "false"),
        ("MR_LISTER_PHASE7_SCAFFOLD_ONLY", "TRUE"),
        ("MR_LISTER_PHASE7_PUBLICATION_ENABLED", "true"),
        ("MR_LISTER_PHASE7_REQUEST_ENABLED", "true"),
        ("MR_LISTER_PHASE7_QUERY_ENABLED", "true"),
        ("MR_LISTER_PHASE7_QUERY_ENABLED", "False"),
    ],
)
def test_scaffold_rejects_every_drifted_marker_before_event_or_entrypoint_access(
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    value: str | None,
) -> None:
    for marker, expected in EXPECTED_DISABLED_ENVIRONMENT.items():
        monkeypatch.setenv(marker, expected)
    if value is None:
        monkeypatch.delenv(name, raising=False)
    else:
        monkeypatch.setenv(name, value)

    calls: list[object] = []
    entrypoint = ModuleType("mr_lister.cloud.phase7_entrypoints")
    entrypoint.publication_query_api_handler = lambda event, context=None: calls.append(event)
    monkeypatch.setitem(sys.modules, entrypoint.__name__, entrypoint)
    module = _load_shim(f"phase7_lambda_drift_{name}_{value!r}")

    with pytest.raises(module.Phase7ReadOnlyScaffoldNotReady) as captured:
        module.publication_query_api_handler(_ExplosiveEvent(), None)

    assert calls == []
    assert captured.value.__cause__ is None
    assert "event" not in str(captured.value).casefold()


def test_exact_disabled_tuple_delegates_only_to_the_coordinated_entrypoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for marker, expected in EXPECTED_DISABLED_ENVIRONMENT.items():
        monkeypatch.setenv(marker, expected)
    calls: list[tuple[Mapping[str, Any], object | None]] = []
    sentinel = {"disabled": True}

    def disabled_handler(
        event: Mapping[str, Any],
        context: object | None = None,
    ) -> dict[str, Any]:
        calls.append((event, context))
        return sentinel

    entrypoint = ModuleType("mr_lister.cloud.phase7_entrypoints")
    entrypoint.publication_query_api_handler = disabled_handler
    monkeypatch.setitem(sys.modules, entrypoint.__name__, entrypoint)
    module = _load_shim("phase7_lambda_exact_disabled")
    event = {"opaque": "not-inspected-by-shim"}
    context = object()

    assert module.PRODUCTION_ENTRYPOINT == (
        "mr_lister.cloud.phase7_entrypoints.publication_query_api_handler"
    )
    assert module.REQUIRED_DISABLED_ENVIRONMENT == EXPECTED_DISABLED_ENVIRONMENT
    assert module.publication_query_api_handler(event, context) is sentinel
    assert calls == [(event, context)]


def test_readme_freezes_the_unregistered_and_non_mutating_scope() -> None:
    readme = README.read_text(encoding="utf-8")

    assert "registers no API Gateway" in readme
    assert "dynamodb:GetItem" in readme
    assert "dynamodb:Query" in readme
    assert "MR_LISTER_PHASE7_QUERY_ENABLED=false" in readme
    assert "mr_lister.cloud.phase7_entrypoints.publication_query_api_handler" in readme
    assert "omits the release fingerprint" in readme
    assert "Cognito issuer/client/scope/group" in readme
    assert "packaged-source parity" in readme
    assert "does not claim that it can compose" in readme
    assert "There is intentionally no" in readme
    assert "SCAFFOLD_ONLY=false" in readme
