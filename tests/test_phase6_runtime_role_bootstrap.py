from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
BOOTSTRAP_PATH = ROOT / "infra/phase6/runtime-role-bootstrap.json"
LEGACY_BOOTSTRAP_PATH = ROOT / "infra/phase6/runtime-update-bootstrap.json"


def _document(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def _statements(role: dict[str, Any]) -> dict[str, dict[str, Any]]:
    policy = role["Properties"]["Policies"][0]["PolicyDocument"]
    return {statement["Sid"]: statement for statement in policy["Statement"]}


def test_root_only_bootstrap_has_one_static_role_and_three_exact_inputs() -> None:
    template = _document(BOOTSTRAP_PATH)

    assert set(template) == {
        "AWSTemplateFormatVersion",
        "Description",
        "Metadata",
        "Outputs",
        "Parameters",
        "Resources",
        "Rules",
    }
    assert template["Metadata"]["MrListerDeployment"] == {
        "DeploymentClass": "CORE_RUNTIME_CFN_ROLE_BOOTSTRAP_ONLY",
        "Environment": "dev",
        "Region": "us-west-2",
        "RootApplied": True,
    }
    assert set(template["Parameters"]) == {
        "LambdaArchiveSha256",
        "LambdaVersionId",
        "ReleaseFingerprint",
    }
    assert all("Default" not in definition for definition in template["Parameters"].values())
    version_pattern = template["Parameters"]["LambdaVersionId"]["AllowedPattern"]
    assert re.fullmatch(version_pattern, "BrN1FSvu_H9ZpkLKXFzvmbVPyfdgBUon")
    for moving in (
        "PENDING",
        "current",
        "DEFAULT",
        "latest",
        "moving",
        "null",
        "none",
        "unversioned",
    ):
        assert re.fullmatch(version_pattern, moving) is None
    assert template["Rules"] == {
        "OnlyUsWest2": {
            "Assertions": [
                {
                    "Assert": {"Fn::Equals": [{"Ref": "AWS::Region"}, "us-west-2"]},
                    "AssertDescription": ("This dev-only bootstrap must be created in us-west-2"),
                }
            ]
        }
    }
    assert set(template["Resources"]) == {"CoreRuntimeExecutionRole"}
    assert template["Outputs"] == {
        "CoreRuntimeExecutionRoleArn": {
            "Value": {"Fn::GetAtt": ["CoreRuntimeExecutionRole", "Arn"]}
        }
    }


def test_root_only_bootstrap_copies_the_reviewed_execution_role_exactly() -> None:
    role = _document(BOOTSTRAP_PATH)["Resources"]["CoreRuntimeExecutionRole"]
    reviewed = _document(LEGACY_BOOTSTRAP_PATH)["Resources"]["CoreRuntimeExecutionRole"]

    assert role == reviewed
    assert role["DeletionPolicy"] == "Retain"
    assert role["UpdateReplacePolicy"] == "Retain"
    assert role["Properties"]["RoleName"] == "mr-lister-phase6-runtime-cfn-dev"
    assert role["Properties"]["MaxSessionDuration"] == 3600
    assert role["Properties"]["AssumeRolePolicyDocument"] == {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Principal": {"Service": "cloudformation.amazonaws.com"},
                "Action": "sts:AssumeRole",
            }
        ],
    }


def test_root_only_bootstrap_has_no_developer_or_temporary_authority() -> None:
    template = _document(BOOTSTRAP_PATH)
    serialized = json.dumps(template, sort_keys=True)

    assert all(
        resource["Type"] != "AWS::IAM::ManagedPolicy" for resource in template["Resources"].values()
    )
    assert all(
        "Groups" not in resource["Properties"] for resource in template["Resources"].values()
    )
    assert "mr-lister-developers" not in serialized
    assert "mr-lister-dev" not in serialized
    assert "NotAfter" not in serialized
    assert "DateLessThan" not in serialized
    assert "RuntimeUpdateDeployerRole" not in serialized


def test_execution_role_keeps_exact_resource_and_wildcard_boundaries() -> None:
    role = _document(BOOTSTRAP_PATH)["Resources"]["CoreRuntimeExecutionRole"]
    statements = _statements(role)

    assert len(statements) == 14
    wildcard = {
        sid: statement for sid, statement in statements.items() if statement["Resource"] == "*"
    }
    assert set(wildcard) == {
        "ConfigureOnlyRegionalStepFunctionsLogDelivery",
        "CreateOnlyDispatcherEventSourceMapping",
    }
    assert all(
        statement["Condition"]["StringEquals"]["aws:RequestedRegion"] == "us-west-2"
        for statement in wildcard.values()
    )

    artifact = statements["ReadOnlyExactLambdaDeploymentArchiveVersion"]
    assert artifact == {
        "Sid": "ReadOnlyExactLambdaDeploymentArchiveVersion",
        "Effect": "Allow",
        "Action": "s3:GetObjectVersion",
        "Resource": {
            "Fn::Sub": (
                "arn:${AWS::Partition}:s3:::mr-lister-phase6-artifacts-dev-"
                "${AWS::AccountId}-us-west-2/private/deployments/lambda/releases/"
                "${ReleaseFingerprint}/phase6-lambda-${LambdaArchiveSha256}.zip"
            )
        },
        "Condition": {"StringEquals": {"s3:VersionId": {"Ref": "LambdaVersionId"}}},
    }

    pass_role = statements["PassOnlyCoreRuntimeRoles"]
    assert pass_role["Resource"] == {
        "Fn::Sub": ("arn:${AWS::Partition}:iam::${AWS::AccountId}:role/mr-lister-phase6-dev-*")
    }
    assert pass_role["Condition"] == {
        "StringEquals": {"iam:PassedToService": ["lambda.amazonaws.com", "states.amazonaws.com"]}
    }


def test_execution_role_has_no_application_runtime_or_web_surface_authority() -> None:
    role = _document(BOOTSTRAP_PATH)["Resources"]["CoreRuntimeExecutionRole"]
    statements = _statements(role)
    actions = {
        action
        for statement in statements.values()
        for action in (
            statement["Action"] if isinstance(statement["Action"], list) else [statement["Action"]]
        )
    }
    serialized = json.dumps(role, sort_keys=True).lower()

    assert {
        "dynamodb:ConditionCheckItem",
        "dynamodb:GetItem",
        "dynamodb:PutItem",
        "lambda:InvokeFunction",
        "secretsmanager:GetSecretValue",
        "states:StartExecution",
    }.isdisjoint(actions)
    for service in (
        "apigateway",
        "bedrock",
        "bedrock-agentcore",
        "cloudfront",
        "cognito",
        "kms",
        "secretsmanager",
        "sns",
    ):
        assert service not in serialized
