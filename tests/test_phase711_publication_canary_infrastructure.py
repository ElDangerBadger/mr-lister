"""Source-only infrastructure checks for the isolated Phase 7 canary runtime."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE_PATH = ROOT / "infra/phase7/canary-template.json"
GUARD_TEMPLATE_PATH = ROOT / "infra/phase7/template.json"

PROFILE_FINGERPRINT = "5de1257141cfdacb1731f68bb9113712957483b33d5b0f7115afdba86eb7476c"


def _template() -> dict[str, Any]:
    return json.loads(TEMPLATE_PATH.read_text(encoding="utf-8"))


def _resources() -> dict[str, dict[str, Any]]:
    return _template()["Resources"]


def _role_statements() -> dict[str, dict[str, Any]]:
    policies = _resources()["PublicationCanaryFunctionRole"]["Properties"]["Policies"]
    assert len(policies) == 1
    assert policies[0]["PolicyName"] == "ExactBoundPublicationCanary"
    return {statement["Sid"]: statement for statement in policies[0]["PolicyDocument"]["Statement"]}


def test_canary_parameters_bind_versioned_code_and_three_distinct_authorities() -> None:
    parameters = _template()["Parameters"]
    assert set(parameters) == {
        "ApplicationReleaseFingerprint",
        "CanaryBindingFingerprint",
        "CanaryCodeS3Bucket",
        "CanaryCodeS3Key",
        "CanaryCodeS3ObjectVersion",
        "CanaryMode",
        "CanaryReleaseFingerprint",
        "EnvironmentName",
        "PrintifySecretArn",
    }

    fingerprint = "a" * 64
    assert re.fullmatch(
        parameters["CanaryCodeS3Key"]["AllowedPattern"],
        f"phase7/releases/{fingerprint}/canary.zip",
    )
    for name in (
        "ApplicationReleaseFingerprint",
        "CanaryBindingFingerprint",
        "CanaryReleaseFingerprint",
    ):
        assert re.fullmatch(parameters[name]["AllowedPattern"], fingerprint)
        assert not re.fullmatch(parameters[name]["AllowedPattern"], "0" * 64)
        assert "Default" not in parameters[name]
    assert parameters["CanaryMode"]["AllowedValues"] == [
        "read_only_preflight",
        "publish_once",
    ]
    assert "Default" not in parameters["CanaryMode"]
    assert re.fullmatch(parameters["CanaryCodeS3ObjectVersion"]["AllowedPattern"], "v1.token-2")
    assert re.fullmatch(
        parameters["CanaryCodeS3ObjectVersion"]["AllowedPattern"],
        "3/L4k+base64ish==",
    )
    assert not re.fullmatch(parameters["CanaryCodeS3ObjectVersion"]["AllowedPattern"], "null")
    for name in (
        "CanaryCodeS3Bucket",
        "CanaryCodeS3Key",
        "CanaryCodeS3ObjectVersion",
        "PrintifySecretArn",
    ):
        assert "Default" not in parameters[name]


def test_canary_stack_has_exactly_one_log_group_role_and_direct_invoke_function() -> None:
    resources = _resources()
    assert {logical_id: resource["Type"] for logical_id, resource in resources.items()} == {
        "PublicationCanaryLogGroup": "AWS::Logs::LogGroup",
        "PublicationCanaryFunctionRole": "AWS::IAM::Role",
        "PublicationCanaryFunction": "AWS::Serverless::Function",
    }

    log_group = resources["PublicationCanaryLogGroup"]
    assert log_group["DeletionPolicy"] == "Retain"
    assert log_group["UpdateReplacePolicy"] == "Retain"
    assert log_group["Properties"]["RetentionInDays"] == 14
    assert log_group["Properties"]["LogGroupName"] == {
        "Fn::Sub": "/aws/lambda/mr-lister-phase7-${EnvironmentName}-publication-canary"
    }

    function = resources["PublicationCanaryFunction"]
    properties = function["Properties"]
    assert function["DependsOn"] == "PublicationCanaryLogGroup"
    assert properties["FunctionName"] == {
        "Fn::Sub": "mr-lister-phase7-${EnvironmentName}-publication-canary"
    }
    assert properties["CodeUri"] == {
        "Bucket": {"Ref": "CanaryCodeS3Bucket"},
        "Key": {"Ref": "CanaryCodeS3Key"},
        "Version": {"Ref": "CanaryCodeS3ObjectVersion"},
    }
    assert properties["Handler"] == (
        "mr_lister.cloud.phase7_canary_entrypoint.publication_canary_handler"
    )
    assert properties["Runtime"] == "python3.12"
    assert properties["Architectures"] == ["arm64"]
    assert properties["Role"] == {"Fn::GetAtt": ["PublicationCanaryFunctionRole", "Arn"]}
    assert properties["ReservedConcurrentExecutions"] == 1
    assert properties["MemorySize"] == 512
    assert properties["Timeout"] == 60
    assert properties["LoggingConfig"] == {
        "LogFormat": "JSON",
        "ApplicationLogLevel": "ERROR",
        "SystemLogLevel": "WARN",
    }
    for forbidden in (
        "Aliases",
        "DeadLetterQueue",
        "EventInvokeConfig",
        "Events",
        "FileSystemConfigs",
        "FunctionUrlConfig",
        "Layers",
        "ProvisionedConcurrencyConfig",
        "VpcConfig",
    ):
        assert forbidden not in properties


def test_canary_environment_keeps_application_disabled_and_authorities_separate() -> None:
    variables = _resources()["PublicationCanaryFunction"]["Properties"]["Environment"]["Variables"]
    assert variables == {
        "MR_LISTER_AWS_ACCOUNT_ID": {"Ref": "AWS::AccountId"},
        "MR_LISTER_ENVIRONMENT": {"Ref": "EnvironmentName"},
        "MR_LISTER_STATE_TABLE": {"Fn::Sub": "mr-lister-phase6-${EnvironmentName}"},
        "MR_LISTER_PHASE7_CANARY_RELEASE_FINGERPRINT": {"Ref": "CanaryReleaseFingerprint"},
        "MR_LISTER_PHASE7_CANARY_BINDING_FINGERPRINT": {"Ref": "CanaryBindingFingerprint"},
        "MR_LISTER_PHASE7_CANARY_MODE": {"Ref": "CanaryMode"},
        "MR_LISTER_RELEASE_FINGERPRINT": {"Ref": "ApplicationReleaseFingerprint"},
        "MR_LISTER_PRINTIFY_SECRET_ARN": {"Ref": "PrintifySecretArn"},
        "MR_LISTER_PHASE7_SCAFFOLD_ONLY": "false",
        "MR_LISTER_PHASE7_CANARY_ENABLED": "true",
        "MR_LISTER_PHASE7_QUERY_ENABLED": "false",
        "MR_LISTER_PHASE7_REQUEST_ENABLED": "false",
        "MR_LISTER_PHASE7_PUBLICATION_ENABLED": "false",
        "MR_LISTER_PRODUCT_PROFILE_ID": "gildan_64000_swiftpod",
        "MR_LISTER_PRODUCT_PROFILE_VERSION": "2",
        "MR_LISTER_PRODUCT_PROFILE_FINGERPRINT": PROFILE_FINGERPRINT,
        "MR_LISTER_PRODUCT_PROFILE_PATH": (
            "/var/task/config/product_profiles/gildan_64000_swiftpod.json"
        ),
    }
    assert (
        len(
            {
                variables["MR_LISTER_PHASE7_CANARY_RELEASE_FINGERPRINT"]["Ref"],
                variables["MR_LISTER_PHASE7_CANARY_BINDING_FINGERPRINT"]["Ref"],
                variables["MR_LISTER_RELEASE_FINGERPRINT"]["Ref"],
            }
        )
        == 3
    )


def test_canary_role_has_only_exact_logs_state_and_secret_authority() -> None:
    role = _resources()["PublicationCanaryFunctionRole"]["Properties"]
    assert "ManagedPolicyArns" not in role
    assert "PermissionsBoundary" not in role
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
    statements = _role_statements()
    assert set(statements) == {
        "CommitExactPublicationAuthorityConditionChecks",
        "CommitExactPublicationAuthorityPuts",
        "WritePublicationCanaryLogs",
        "ReadExactPublicationAuthority",
        "ReadExactPublicationCredential",
    }
    assert statements["WritePublicationCanaryLogs"] == {
        "Sid": "WritePublicationCanaryLogs",
        "Effect": "Allow",
        "Action": ["logs:CreateLogStream", "logs:PutLogEvents"],
        "Resource": {"Fn::GetAtt": ["PublicationCanaryLogGroup", "Arn"]},
    }
    assert statements["ReadExactPublicationAuthority"] == {
        "Sid": "ReadExactPublicationAuthority",
        "Effect": "Allow",
        "Action": [
            "dynamodb:GetItem",
            "dynamodb:Query",
        ],
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
    assert statements["CommitExactPublicationAuthorityConditionChecks"] == {
        "Sid": "CommitExactPublicationAuthorityConditionChecks",
        "Effect": "Allow",
        "Action": "dynamodb:ConditionCheckItem",
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
    assert statements["CommitExactPublicationAuthorityPuts"] == {
        "Sid": "CommitExactPublicationAuthorityPuts",
        "Effect": "Allow",
        "Action": "dynamodb:PutItem",
        "Resource": {
            "Fn::Sub": (
                "arn:${AWS::Partition}:dynamodb:${AWS::Region}:${AWS::AccountId}:table/"
                "mr-lister-phase6-${EnvironmentName}"
            )
        },
        "Condition": {
            "ForAllValues:StringLike": {"dynamodb:LeadingKeys": ["JOB#*", "PUBLICATION#*"]},
            "ForAnyValue:StringEquals": {"dynamodb:EnclosingOperation": ["TransactWriteItems"]},
        },
    }
    assert statements["ReadExactPublicationCredential"] == {
        "Sid": "ReadExactPublicationCredential",
        "Effect": "Allow",
        "Action": "secretsmanager:GetSecretValue",
        "Resource": {"Ref": "PrintifySecretArn"},
    }
    actions = {
        action
        for statement in statements.values()
        for action in (
            [statement["Action"]] if isinstance(statement["Action"], str) else statement["Action"]
        )
    }
    assert actions == {
        "dynamodb:ConditionCheckItem",
        "dynamodb:GetItem",
        "dynamodb:PutItem",
        "dynamodb:Query",
        "logs:CreateLogStream",
        "logs:PutLogEvents",
        "secretsmanager:GetSecretValue",
    }


def test_canary_template_contains_no_trigger_or_broader_runtime_surface() -> None:
    template = _template()
    serialized = json.dumps(template, sort_keys=True).casefold()
    forbidden_resource_types = {
        "AWS::ApiGateway::RestApi",
        "AWS::ApiGatewayV2::Api",
        "AWS::Events::Rule",
        "AWS::Lambda::Alias",
        "AWS::Lambda::EventSourceMapping",
        "AWS::Lambda::Permission",
        "AWS::Lambda::Url",
        "AWS::Lambda::Version",
        "AWS::SQS::Queue",
        "AWS::Serverless::Api",
        "AWS::Serverless::HttpApi",
        "AWS::Serverless::StateMachine",
        "AWS::StepFunctions::StateMachine",
    }
    assert (
        not {resource["Type"] for resource in template["Resources"].values()}
        & forbidden_resource_types
    )
    for forbidden in (
        "apigateway:",
        "bedrock:",
        "dynamodb:batch",
        "dynamodb:delete",
        "dynamodb:execute",
        "dynamodb:partiql",
        "dynamodb:scan",
        "dynamodb:updateitem",
        "execute-api:",
        "kms:",
        "lambda:invoke",
        "s3:",
        "sns:",
        "states:",
        '"resource": "*"',
    ):
        assert forbidden not in serialized

    # This slice is additive and must not rewrite the already deployed read-only guard template.
    guard = json.loads(GUARD_TEMPLATE_PATH.read_text(encoding="utf-8"))
    assert "PublicationCanaryFunction" not in guard["Resources"]
    assert "CanaryReleaseFingerprint" not in guard["Parameters"]
