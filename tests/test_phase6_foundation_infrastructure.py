from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

ROOT = Path(__file__).parents[1]
PHASE6 = ROOT / "infra" / "phase6"
FOUNDATION_TEMPLATE = PHASE6 / "foundation.json"
FULL_TEMPLATE = PHASE6 / "template.json"


def load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def test_foundation_is_explicitly_create_only_and_foundation_only() -> None:
    template = load(FOUNDATION_TEMPLATE)

    assert template["AWSTemplateFormatVersion"] == "2010-09-09"
    assert template["Transform"] == "AWS::Serverless-2016-10-31"
    assert template["Metadata"] == {
        "MrListerDeployment": {
            "DeploymentClass": "FOUNDATION_ONLY",
            "CreateOnly": True,
            "UpgradeTemplate": "infra/phase6/template.json",
        }
    }
    assert template["Outputs"]["DeploymentReadiness"]["Value"] == "FOUNDATION_ONLY"


def test_foundation_has_only_the_durable_create_first_resources() -> None:
    template = load(FOUNDATION_TEMPLATE)

    assert template["Parameters"] == {
        "EnvironmentName": {
            "Type": "String",
            "Default": "dev",
            "AllowedPattern": "^[a-z][a-z0-9-]{1,15}$",
        }
    }
    assert set(template["Resources"]) == {
        "OperationalStateTable",
        "PrivateArtifactBucket",
        "PrivateArtifactBucketPolicy",
    }
    assert {resource["Type"] for resource in template["Resources"].values()} == {
        "AWS::DynamoDB::Table",
        "AWS::S3::Bucket",
        "AWS::S3::BucketPolicy",
    }
    serialized = json.dumps(template, sort_keys=True).lower()
    for forbidden in (
        "aws::iam::",
        "aws::lambda::",
        "aws::serverless::function",
        "aws::serverless::httpapi",
        "aws::stepfunctions::",
        "aws::events::",
        "aws::cognito::",
        "aws::cloudfront::",
        "secretsmanager",
        "agentcore",
        "printify",
    ):
        assert forbidden not in serialized


def test_foundation_resources_are_the_full_template_resources_except_pre_origin_cors() -> None:
    foundation = load(FOUNDATION_TEMPLATE)["Resources"]
    full = load(FULL_TEMPLATE)["Resources"]

    assert foundation["OperationalStateTable"] == full["OperationalStateTable"]
    assert foundation["PrivateArtifactBucketPolicy"] == full["PrivateArtifactBucketPolicy"]

    expected_bucket = deepcopy(full["PrivateArtifactBucket"])
    expected_bucket["Properties"].pop("CorsConfiguration")
    assert foundation["PrivateArtifactBucket"] == expected_bucket
    assert "CorsConfiguration" not in foundation["PrivateArtifactBucket"]["Properties"]


def test_foundation_retains_names_data_protection_and_operational_indexes() -> None:
    resources = load(FOUNDATION_TEMPLATE)["Resources"]
    bucket = resources["PrivateArtifactBucket"]
    table = resources["OperationalStateTable"]

    assert bucket["DeletionPolicy"] == bucket["UpdateReplacePolicy"] == "Retain"
    assert bucket["Properties"]["BucketName"] == {
        "Fn::Sub": (
            "mr-lister-phase6-artifacts-${EnvironmentName}-${AWS::AccountId}-${AWS::Region}"
        )
    }
    assert bucket["Properties"]["VersioningConfiguration"] == {"Status": "Enabled"}
    assert all(bucket["Properties"]["PublicAccessBlockConfiguration"].values())
    assert bucket["Properties"]["BucketEncryption"] == {
        "ServerSideEncryptionConfiguration": [
            {"ServerSideEncryptionByDefault": {"SSEAlgorithm": "AES256"}}
        ]
    }

    assert table["DeletionPolicy"] == table["UpdateReplacePolicy"] == "Retain"
    assert table["Properties"]["TableName"] == {"Fn::Sub": "mr-lister-phase6-${EnvironmentName}"}
    assert table["Properties"]["PointInTimeRecoverySpecification"] == {
        "PointInTimeRecoveryEnabled": True
    }
    assert table["Properties"]["TimeToLiveSpecification"] == {
        "AttributeName": "expires_at",
        "Enabled": True,
    }
    assert table["Properties"]["StreamSpecification"] == {"StreamViewType": "KEYS_ONLY"}
    assert [index["IndexName"] for index in table["Properties"]["GlobalSecondaryIndexes"]] == [
        "DueWorkIndex",
        "OwnerJobsIndex",
        "ExecutionRecoveryIndex",
    ]


def test_foundation_exports_the_upgrade_resource_identities() -> None:
    outputs = load(FOUNDATION_TEMPLATE)["Outputs"]

    assert set(outputs) == {
        "DeploymentReadiness",
        "StateTableName",
        "StateTableArn",
        "ArtifactBucketName",
        "ArtifactBucketArn",
    }
    assert outputs["StateTableName"]["Value"] == {"Ref": "OperationalStateTable"}
    assert outputs["StateTableArn"]["Value"] == {"Fn::GetAtt": ["OperationalStateTable", "Arn"]}
    assert outputs["ArtifactBucketName"]["Value"] == {"Ref": "PrivateArtifactBucket"}
    assert outputs["ArtifactBucketArn"]["Value"] == {"Fn::GetAtt": ["PrivateArtifactBucket", "Arn"]}


def test_bucket_policy_is_deny_only_and_cannot_grant_access() -> None:
    policy = load(FOUNDATION_TEMPLATE)["Resources"]["PrivateArtifactBucketPolicy"]
    statements = policy["Properties"]["PolicyDocument"]["Statement"]

    assert [statement["Sid"] for statement in statements] == [
        "DenyInsecureTransport",
        "DenyStaleBrowserUploadSignatures",
        "DenyUnencryptedBrowserUploads",
    ]
    assert {statement["Effect"] for statement in statements} == {"Deny"}
    assert all(statement["Principal"] == "*" for statement in statements)
