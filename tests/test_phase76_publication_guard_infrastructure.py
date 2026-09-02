from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "infra" / "phase7" / "template.json"


def _template() -> dict[str, Any]:
    return json.loads(TEMPLATE.read_text(encoding="utf-8"))


def _resources() -> dict[str, dict[str, Any]]:
    return _template()["Resources"]


def _guard_statements() -> dict[str, dict[str, Any]]:
    policies = _resources()["PublicationGuardVerificationFunctionRole"]["Properties"]["Policies"]
    assert len(policies) == 1
    return {statement["Sid"]: statement for statement in policies[0]["PolicyDocument"]["Statement"]}


def test_phase76_guard_code_parameters_bind_one_immutable_versioned_release() -> None:
    template = _template()
    parameters = template["Parameters"]

    assert set(parameters) == {
        "ApplicationReleaseFingerprint",
        "EnvironmentName",
        "GuardCodeS3Bucket",
        "GuardCodeS3Key",
        "GuardCodeS3ObjectVersion",
        "GuardReleaseFingerprint",
    }
    assert template["Conditions"]["DeployPublicationStatusQueryScaffold"] == {
        "Fn::Equals": [
            {"Ref": "GuardReleaseFingerprint"},
            "0000000000000000000000000000000000000000000000000000000000000000",
        ]
    }
    fingerprint = "a" * 64
    assert re.fullmatch(
        parameters["GuardCodeS3Key"]["AllowedPattern"],
        f"phase7/releases/{fingerprint}/guard.zip",
    )
    assert re.fullmatch(parameters["GuardReleaseFingerprint"]["AllowedPattern"], fingerprint)
    assert not re.fullmatch(parameters["GuardReleaseFingerprint"]["AllowedPattern"], "0" * 64)
    assert re.fullmatch(parameters["ApplicationReleaseFingerprint"]["AllowedPattern"], fingerprint)
    assert not re.fullmatch(parameters["ApplicationReleaseFingerprint"]["AllowedPattern"], "0" * 64)
    assert re.fullmatch(parameters["GuardCodeS3ObjectVersion"]["AllowedPattern"], "v1.token-2")
    assert not re.fullmatch(parameters["GuardCodeS3ObjectVersion"]["AllowedPattern"], "null")
    assert "Default" not in parameters["GuardCodeS3Bucket"]
    assert "Default" not in parameters["GuardCodeS3Key"]
    assert "Default" not in parameters["GuardCodeS3ObjectVersion"]
    assert "Default" not in parameters["GuardReleaseFingerprint"]
    assert "Default" not in parameters["ApplicationReleaseFingerprint"]


def test_private_guard_function_is_direct_invoke_only_and_exact_disabled_for_publication() -> None:
    template = _template()
    resources = template["Resources"]
    function = resources["PublicationGuardVerificationFunction"]
    properties = function["Properties"]

    assert function["Type"] == "AWS::Serverless::Function"
    assert function["DependsOn"] == "PublicationGuardVerificationLogGroup"
    assert properties["FunctionName"] == {
        "Fn::Sub": "mr-lister-phase7-${EnvironmentName}-guard-verification"
    }
    assert properties["CodeUri"] == {
        "Bucket": {"Ref": "GuardCodeS3Bucket"},
        "Key": {"Ref": "GuardCodeS3Key"},
        "Version": {"Ref": "GuardCodeS3ObjectVersion"},
    }
    assert properties["Handler"] == (
        "mr_lister.cloud.phase7_guard_entrypoint.publication_guard_verification_handler"
    )
    assert properties["Role"] == {"Fn::GetAtt": ["PublicationGuardVerificationFunctionRole", "Arn"]}
    assert properties["ReservedConcurrentExecutions"] == 1
    assert properties["MemorySize"] == 512
    assert properties["Timeout"] == 30
    assert "Events" not in properties
    for forbidden in (
        "DeadLetterQueue",
        "EventInvokeConfig",
        "FunctionUrlConfig",
        "Layers",
        "VpcConfig",
    ):
        assert forbidden not in properties

    variables = properties["Environment"]["Variables"]
    assert variables == {
        "MR_LISTER_PHASE7_SCAFFOLD_ONLY": "false",
        "MR_LISTER_PHASE7_GUARD_ENABLED": "true",
        "MR_LISTER_PHASE7_GUARD_MODE": "approval_version_read_only",
        "MR_LISTER_PHASE7_GUARD_RELEASE_FINGERPRINT": {"Ref": "GuardReleaseFingerprint"},
        "MR_LISTER_RELEASE_FINGERPRINT": {"Ref": "ApplicationReleaseFingerprint"},
        "MR_LISTER_PRODUCT_PROFILE_ID": "gildan_64000_swiftpod",
        "MR_LISTER_PRODUCT_PROFILE_VERSION": "2",
        "MR_LISTER_PRODUCT_PROFILE_FINGERPRINT": (
            "5de1257141cfdacb1731f68bb9113712957483b33d5b0f7115afdba86eb7476c"
        ),
        "MR_LISTER_PRODUCT_PROFILE_PATH": (
            "/var/task/config/product_profiles/gildan_64000_swiftpod.json"
        ),
    }
    globals_variables = template["Globals"]["Function"]["Environment"]["Variables"]
    assert globals_variables["MR_LISTER_PHASE7_QUERY_ENABLED"] == "false"
    assert globals_variables["MR_LISTER_PHASE7_REQUEST_ENABLED"] == "false"
    assert globals_variables["MR_LISTER_PHASE7_PUBLICATION_ENABLED"] == "false"

    forbidden_types = {
        "AWS::ApiGateway::RestApi",
        "AWS::ApiGatewayV2::Api",
        "AWS::Lambda::Permission",
        "AWS::Lambda::Url",
        "AWS::Serverless::Api",
        "AWS::Serverless::HttpApi",
        "AWS::Serverless::StateMachine",
    }
    assert not {resource["Type"] for resource in resources.values()} & forbidden_types


def test_phase76_deployment_conditions_out_the_legacy_query_scaffold() -> None:
    resources = _resources()
    conditioned_resources = {
        "PublicationStatusQueryFunctionRole",
        "PublicationStatusQueryLogGroup",
        "PublicationStatusQueryFunction",
        "PublicationStatusQueryErrorsAlarm",
        "PublicationStatusQueryThrottlesAlarm",
        "PublicationStatusQueryDurationAlarm",
    }

    assert all(
        resources[resource_id]["Condition"] == "DeployPublicationStatusQueryScaffold"
        for resource_id in conditioned_resources
    )
    active_resources = {
        "PublicationGuardVerificationFunctionRole",
        "PublicationGuardVerificationLogGroup",
        "PublicationGuardVerificationFunction",
        "PublicationGuardVerificationErrorsAlarm",
        "PublicationGuardVerificationThrottlesAlarm",
        "PublicationGuardVerificationDurationAlarm",
        "PublicationStatusAlarmTopicKey",
        "PublicationStatusAlarmTopic",
        "PublicationStatusAlarmTopicPolicy",
    }
    assert set(resources) == conditioned_resources | active_resources
    assert all("Condition" not in resources[resource_id] for resource_id in active_resources)
    assert set(_template()["Outputs"]) == {
        "DeploymentReadiness",
        "PublicationEnabled",
        "PublicationGuardExternalCallsEnabled",
        "PublicationGuardVerificationEnabled",
        "PublicationGuardVerificationFunctionArn",
        "PublicationRequestEnabled",
        "PublicationStatusAlarmTopicArn",
        "PublicationStatusQueryEnabled",
        "PublicationStatusQueryRegistered",
    }


def test_guard_role_can_only_log_and_strong_read_exact_job_publication_roots() -> None:
    role = _resources()["PublicationGuardVerificationFunctionRole"]["Properties"]
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
    statements = _guard_statements()
    assert set(statements) == {
        "WritePublicationGuardLogs",
        "ReadExactApprovalPublicationAuthority",
    }
    assert statements["ReadExactApprovalPublicationAuthority"] == {
        "Sid": "ReadExactApprovalPublicationAuthority",
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
        "kms:",
        '"resource": "*"',
    ):
        assert forbidden not in serialized


def test_guard_observability_is_bounded_and_outputs_do_not_enable_any_publish_surface() -> None:
    resources = _resources()
    log_group = resources["PublicationGuardVerificationLogGroup"]
    assert log_group["DeletionPolicy"] == "Retain"
    assert log_group["UpdateReplacePolicy"] == "Retain"
    assert log_group["Properties"]["RetentionInDays"] == 14

    expected = {
        "PublicationGuardVerificationErrorsAlarm": ("Errors", "Sum", 0),
        "PublicationGuardVerificationThrottlesAlarm": ("Throttles", "Sum", 0),
        "PublicationGuardVerificationDurationAlarm": ("Duration", "Maximum", 24000),
    }
    for alarm_id, (metric, statistic, threshold) in expected.items():
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
            {"Name": "FunctionName", "Value": {"Ref": "PublicationGuardVerificationFunction"}}
        ]

    outputs = _template()["Outputs"]
    assert outputs["DeploymentReadiness"]["Value"] == "READ_ONLY_GUARD"
    assert outputs["PublicationGuardVerificationEnabled"]["Value"] == "true"
    assert outputs["PublicationGuardExternalCallsEnabled"]["Value"] == "false"
    assert outputs["PublicationStatusQueryRegistered"]["Value"] == "false"
    assert outputs["PublicationStatusQueryEnabled"]["Value"] == "false"
    assert outputs["PublicationRequestEnabled"]["Value"] == "false"
    assert outputs["PublicationEnabled"]["Value"] == "false"
