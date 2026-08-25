from __future__ import annotations

import json
from pathlib import Path

BOOTSTRAP = Path("infra/phase6/bootstrap.json")
EXPECTED_STACK_ARN = {
    "Fn::Sub": (
        "arn:${AWS::Partition}:cloudformation:us-west-2:${AWS::AccountId}:"
        "stack/mr-lister-phase6-dev/*"
    )
}
EXPECTED_TABLE_ARN = {
    "Fn::Sub": (
        "arn:${AWS::Partition}:dynamodb:us-west-2:${AWS::AccountId}:table/mr-lister-phase6-dev"
    )
}
EXPECTED_TABLE_STREAM_ARN = {
    "Fn::Sub": (
        "arn:${AWS::Partition}:dynamodb:us-west-2:${AWS::AccountId}:"
        "table/mr-lister-phase6-dev/stream/*"
    )
}
EXPECTED_BUCKET_ARN = {
    "Fn::Sub": (
        "arn:${AWS::Partition}:s3:::mr-lister-phase6-artifacts-dev-${AWS::AccountId}-us-west-2"
    )
}
EXPIRY = {"DateLessThan": {"aws:CurrentTime": {"Ref": "NotAfter"}}}


def load_bootstrap() -> dict:
    return json.loads(BOOTSTRAP.read_text(encoding="utf-8"))


def statements_by_sid(policy_document: dict) -> dict[str, dict]:
    statements = policy_document["Statement"]
    assert len({statement["Sid"] for statement in statements}) == len(statements)
    return {statement["Sid"]: statement for statement in statements}


def test_bootstrap_is_fixed_to_the_dev_foundation_and_requires_an_expiry() -> None:
    template = load_bootstrap()

    assert template["Metadata"] == {
        "MrListerDeployment": {
            "DeploymentClass": "FOUNDATION_BOOTSTRAP_ONLY",
            "RootApplied": True,
            "TargetStack": "mr-lister-phase6-dev",
        }
    }
    assert set(template["Parameters"]) == {"NotAfter"}
    assert "Default" not in template["Parameters"]["NotAfter"]
    assert template["Parameters"]["NotAfter"]["AllowedPattern"].endswith("Z$")
    assert {name: resource["Type"] for name, resource in template["Resources"].items()} == {
        "CloudFormationExecutionRole": "AWS::IAM::Role",
        "DeveloperDeploymentPolicy": "AWS::IAM::ManagedPolicy",
    }


def test_execution_role_trust_name_and_transform_are_exact() -> None:
    role = load_bootstrap()["Resources"]["CloudFormationExecutionRole"]
    properties = role["Properties"]

    assert role["DeletionPolicy"] == role["UpdateReplacePolicy"] == "Retain"
    assert properties["RoleName"] == "mr-lister-phase6-foundation-cfn-dev"
    assert properties["AssumeRolePolicyDocument"]["Statement"] == [
        {
            "Effect": "Allow",
            "Principal": {"Service": "cloudformation.amazonaws.com"},
            "Action": "sts:AssumeRole",
        }
    ]
    policies = properties["Policies"]
    assert len(policies) == 1
    assert policies[0]["PolicyName"] == "CreateOnlyMrListerPhase6Foundation"
    statements = statements_by_sid(policies[0]["PolicyDocument"])
    transform = statements["ExpandOnlyTheAwsServerlessTransform"]
    assert transform == {
        "Sid": "ExpandOnlyTheAwsServerlessTransform",
        "Effect": "Allow",
        "Action": "cloudformation:CreateChangeSet",
        "Resource": {
            "Fn::Sub": (
                "arn:${AWS::Partition}:cloudformation:us-west-2:aws:transform/Serverless-2016-10-31"
            )
        },
    }


def test_execution_role_can_only_build_and_rollback_the_exact_foundation_resources() -> None:
    role = load_bootstrap()["Resources"]["CloudFormationExecutionRole"]["Properties"]
    statements = statements_by_sid(role["Policies"][0]["PolicyDocument"])

    assert set(statements) == {
        "ExpandOnlyTheAwsServerlessTransform",
        "CreateConfigureAndRollbackOnlyTheFoundationTable",
        "TagOnlyTheFoundationTableStream",
        "CreateOnlyTheFoundationBucketInUsWest2",
        "ConfigureAndRollbackOnlyTheFoundationBucket",
    }
    table = statements["CreateConfigureAndRollbackOnlyTheFoundationTable"]
    assert table["Resource"] == EXPECTED_TABLE_ARN
    assert set(table["Action"]) == {
        "dynamodb:CreateTable",
        "dynamodb:DeleteTable",
        "dynamodb:DescribeContinuousBackups",
        "dynamodb:DescribeTable",
        "dynamodb:DescribeTimeToLive",
        "dynamodb:ListTagsOfResource",
        "dynamodb:TagResource",
        "dynamodb:UntagResource",
        "dynamodb:UpdateContinuousBackups",
        "dynamodb:UpdateTimeToLive",
    }
    bucket_create = statements["CreateOnlyTheFoundationBucketInUsWest2"]
    assert bucket_create == {
        "Sid": "CreateOnlyTheFoundationBucketInUsWest2",
        "Effect": "Allow",
        "Action": "s3:CreateBucket",
        "Resource": EXPECTED_BUCKET_ARN,
        "Condition": {"StringEquals": {"s3:LocationConstraint": "us-west-2"}},
    }
    bucket = statements["ConfigureAndRollbackOnlyTheFoundationBucket"]
    assert bucket["Resource"] == EXPECTED_BUCKET_ARN
    assert set(bucket["Action"]) == {
        "s3:DeleteBucket",
        "s3:DeleteBucketPolicy",
        "s3:GetBucketAcl",
        "s3:GetBucketCORS",
        "s3:GetBucketLocation",
        "s3:GetBucketOwnershipControls",
        "s3:GetBucketPolicy",
        "s3:GetBucketPolicyStatus",
        "s3:GetBucketPublicAccessBlock",
        "s3:GetBucketTagging",
        "s3:GetBucketVersioning",
        "s3:GetEncryptionConfiguration",
        "s3:GetLifecycleConfiguration",
        "s3:ListBucket",
        "s3:PutBucketOwnershipControls",
        "s3:PutBucketPolicy",
        "s3:PutBucketPublicAccessBlock",
        "s3:PutBucketTagging",
        "s3:PutBucketVersioning",
        "s3:PutEncryptionConfiguration",
        "s3:PutLifecycleConfiguration",
    }

    serialized = json.dumps(role["Policies"], sort_keys=True).lower()
    for forbidden in (
        "apigateway",
        "bedrock",
        "cognito",
        "events:",
        "iam:passrole",
        "lambda:",
        "secretsmanager",
        "states:",
        "s3:getobject",
        "s3:putobject",
        '"resource": "*"',
        '"s3:*"',
    ):
        assert forbidden not in serialized


def test_execution_role_stream_tag_retry_permission_is_exact_and_narrow() -> None:
    role = load_bootstrap()["Resources"]["CloudFormationExecutionRole"]["Properties"]
    statements = statements_by_sid(role["Policies"][0]["PolicyDocument"])

    assert statements["TagOnlyTheFoundationTableStream"] == {
        "Sid": "TagOnlyTheFoundationTableStream",
        "Effect": "Allow",
        "Action": "dynamodb:TagResource",
        "Resource": EXPECTED_TABLE_STREAM_ARN,
    }
    stream_scoped = [
        statement
        for statement in statements.values()
        if statement.get("Resource") == EXPECTED_TABLE_STREAM_ARN
    ]
    assert stream_scoped == [statements["TagOnlyTheFoundationTableStream"]]


def test_developer_policy_is_expiring_and_attached_only_to_the_existing_group() -> None:
    properties = load_bootstrap()["Resources"]["DeveloperDeploymentPolicy"]["Properties"]

    assert properties["ManagedPolicyName"] == "mr-lister-phase6-foundation-deployer-dev"
    assert properties["Groups"] == ["mr-lister-developers"]
    statements = properties["PolicyDocument"]["Statement"]
    assert statements
    for statement in statements:
        assert statement["Effect"] == "Allow"
        assert statement["Condition"]["DateLessThan"] == EXPIRY["DateLessThan"]


def test_developer_can_create_only_the_named_reviewed_change_set_with_exact_inputs() -> None:
    policy = load_bootstrap()["Resources"]["DeveloperDeploymentPolicy"]["Properties"]
    statements = statements_by_sid(policy["PolicyDocument"])
    create = statements["CreateOnlyTheReviewedFoundationChangeSet"]

    assert create["Action"] == "cloudformation:CreateChangeSet"
    assert create["Resource"] == EXPECTED_STACK_ARN
    condition = create["Condition"]
    assert condition["StringEquals"] == {
        "aws:RequestTag/DeploymentClass": "FOUNDATION_ONLY",
        "aws:RequestTag/Environment": "dev",
        "aws:RequestTag/Project": "MrLister",
        "cloudformation:ChangeSetName": ("mr-lister-phase6-dev-foundation-create-689897c254c9"),
        "cloudformation:RoleArn": {"Fn::GetAtt": ["CloudFormationExecutionRole", "Arn"]},
    }
    assert condition["ForAllValues:StringEquals"] == {
        "aws:TagKeys": ["DeploymentClass", "Environment", "Project"],
        "cloudformation:ResourceTypes": [
            "AWS::DynamoDB::Table",
            "AWS::S3::Bucket",
            "AWS::S3::BucketPolicy",
        ],
    }
    assert condition["Null"] == {
        "aws:TagKeys": "false",
        "cloudformation:ResourceTypes": "false",
    }
    transform = statements["UseOnlyTheServerlessTransformForTheReviewedChangeSet"]
    assert transform["Action"] == "cloudformation:CreateChangeSet"
    assert transform["Resource"] == {
        "Fn::Sub": (
            "arn:${AWS::Partition}:cloudformation:us-west-2:aws:transform/Serverless-2016-10-31"
        )
    }
    assert transform["Condition"] == condition


def test_developer_mutations_are_only_change_set_execution_protection_and_passrole() -> None:
    policy = load_bootstrap()["Resources"]["DeveloperDeploymentPolicy"]["Properties"]
    statements = statements_by_sid(policy["PolicyDocument"])

    change_set = statements["ManageOnlyTheReviewedFoundationChangeSet"]
    assert set(change_set["Action"]) == {
        "cloudformation:DeleteChangeSet",
        "cloudformation:ExecuteChangeSet",
    }
    assert change_set["Resource"] == EXPECTED_STACK_ARN
    assert change_set["Condition"]["StringEquals"] == {
        "cloudformation:ChangeSetName": ("mr-lister-phase6-dev-foundation-create-689897c254c9")
    }

    protection = statements["EnableOnlyFoundationTerminationProtection"]
    assert protection["Action"] == "cloudformation:UpdateTerminationProtection"
    assert protection["Resource"] == EXPECTED_STACK_ARN

    pass_role = statements["PassOnlyTheFoundationCloudFormationRole"]
    assert pass_role["Action"] == "iam:PassRole"
    assert pass_role["Resource"] == {"Fn::GetAtt": ["CloudFormationExecutionRole", "Arn"]}
    assert pass_role["Condition"]["StringEquals"] == {
        "iam:PassedToService": "cloudformation.amazonaws.com"
    }

    serialized = json.dumps(policy["PolicyDocument"], sort_keys=True)
    for forbidden in (
        "cloudformation:CreateStack",
        "cloudformation:DeleteStack",
        "cloudformation:UpdateStack",
        "dynamodb:PutItem",
        "dynamodb:UpdateItem",
        "s3:GetObject",
        "s3:PutObject",
        "secretsmanager:",
        "lambda:",
        '"Resource": "*"',
    ):
        assert forbidden not in serialized


def test_developer_readback_matches_the_offline_foundation_evidence_captures() -> None:
    policy = load_bootstrap()["Resources"]["DeveloperDeploymentPolicy"]["Properties"]
    statements = statements_by_sid(policy["PolicyDocument"])

    change_set = statements["InspectOnlyTheFoundationChangeSet"]
    assert change_set["Action"] == "cloudformation:DescribeChangeSet"
    assert change_set["Resource"] == EXPECTED_STACK_ARN
    assert change_set["Condition"] == EXPIRY

    stack = statements["InspectOnlyTheFoundationStack"]
    assert set(stack["Action"]) == {
        "cloudformation:DescribeStackEvents",
        "cloudformation:DescribeStacks",
        "cloudformation:GetTemplate",
        "cloudformation:ListStackResources",
    }
    assert stack["Resource"] == EXPECTED_STACK_ARN

    table = statements["ReadOnlyTheFoundationTableConfiguration"]
    assert set(table["Action"]) == {
        "dynamodb:DescribeContinuousBackups",
        "dynamodb:DescribeTable",
        "dynamodb:DescribeTimeToLive",
        "dynamodb:ListTagsOfResource",
    }
    assert table["Resource"] == EXPECTED_TABLE_ARN

    bucket = statements["ReadOnlyTheFoundationBucketConfiguration"]
    assert set(bucket["Action"]) == {
        "s3:GetBucketCORS",
        "s3:GetBucketOwnershipControls",
        "s3:GetBucketPolicy",
        "s3:GetBucketPolicyStatus",
        "s3:GetBucketPublicAccessBlock",
        "s3:GetBucketTagging",
        "s3:GetBucketVersioning",
        "s3:GetEncryptionConfiguration",
        "s3:GetLifecycleConfiguration",
    }
    assert bucket["Resource"] == EXPECTED_BUCKET_ARN


def test_bootstrap_outputs_exact_role_policy_and_expiry_bindings() -> None:
    assert load_bootstrap()["Outputs"] == {
        "CloudFormationExecutionRoleArn": {
            "Value": {"Fn::GetAtt": ["CloudFormationExecutionRole", "Arn"]}
        },
        "DeveloperDeploymentPolicyArn": {"Value": {"Ref": "DeveloperDeploymentPolicy"}},
        "DeveloperDeploymentPolicyNotAfter": {"Value": {"Ref": "NotAfter"}},
    }
