from __future__ import annotations

import json
from pathlib import Path

TEMPLATE_PATH = Path("infra/phase4/durable-workflow.json")
BOOTSTRAP_PATH = Path("infra/phase4/bootstrap.json")


def load_template() -> dict:
    return json.loads(TEMPLATE_PATH.read_text(encoding="utf-8"))


def load_bootstrap() -> dict:
    return json.loads(BOOTSTRAP_PATH.read_text(encoding="utf-8"))


def test_private_artifact_bucket_is_retained_encrypted_versioned_and_blocked() -> None:
    bucket = load_template()["Resources"]["PrivateArtifactBucket"]
    properties = bucket["Properties"]

    assert bucket["DeletionPolicy"] == "Retain"
    assert bucket["UpdateReplacePolicy"] == "Retain"
    assert properties["BucketEncryption"] == {
        "ServerSideEncryptionConfiguration": [
            {"ServerSideEncryptionByDefault": {"SSEAlgorithm": "AES256"}}
        ]
    }
    assert properties["VersioningConfiguration"] == {"Status": "Enabled"}
    assert all(properties["PublicAccessBlockConfiguration"].values())
    assert properties["OwnershipControls"] == {
        "Rules": [{"ObjectOwnership": "BucketOwnerEnforced"}]
    }


def test_artifact_lifecycle_preserves_current_objects() -> None:
    rules = load_template()["Resources"]["PrivateArtifactBucket"]["Properties"][
        "LifecycleConfiguration"
    ]["Rules"]

    assert rules == [
        {
            "Id": "AbortIncompleteMultipartUploads",
            "Status": "Enabled",
            "AbortIncompleteMultipartUpload": {"DaysAfterInitiation": 7},
        },
        {
            "Id": "ExpireNoncurrentArtifactVersions",
            "Status": "Enabled",
            "NoncurrentVersionExpiration": {"NoncurrentDays": 30},
        },
    ]
    assert all("Expiration" not in rule for rule in rules)


def test_bucket_policy_denies_non_tls_access_to_bucket_and_objects() -> None:
    template = load_template()
    statement = template["Resources"]["PrivateArtifactBucketPolicy"]["Properties"][
        "PolicyDocument"
    ]["Statement"]

    assert statement == [
        {
            "Sid": "DenyInsecureTransport",
            "Effect": "Deny",
            "Principal": "*",
            "Action": "s3:*",
            "Resource": [
                {"Fn::GetAtt": ["PrivateArtifactBucket", "Arn"]},
                {"Fn::Sub": "${PrivateArtifactBucket.Arn}/*"},
            ],
            "Condition": {"Bool": {"aws:SecureTransport": "false"}},
        }
    ]


def test_operational_table_is_on_demand_encrypted_retained_and_ttl_ready() -> None:
    table = load_template()["Resources"]["OperationalStateTable"]
    properties = table["Properties"]

    assert table["DeletionPolicy"] == "Retain"
    assert table["UpdateReplacePolicy"] == "Retain"
    assert properties["BillingMode"] == "PAY_PER_REQUEST"
    assert properties["KeySchema"] == [
        {"AttributeName": "PK", "KeyType": "HASH"},
        {"AttributeName": "SK", "KeyType": "RANGE"},
    ]
    assert properties["AttributeDefinitions"] == [
        {"AttributeName": "PK", "AttributeType": "S"},
        {"AttributeName": "SK", "AttributeType": "S"},
    ]
    assert properties["SSESpecification"] == {"SSEEnabled": True}
    assert properties["TimeToLiveSpecification"] == {
        "AttributeName": "expires_at",
        "Enabled": True,
    }


def test_application_resources_have_scopeable_physical_names() -> None:
    resources = load_template()["Resources"]

    assert "mr-lister-phase4-artifacts-" in repr(
        resources["PrivateArtifactBucket"]["Properties"]["BucketName"]
    )
    assert resources["OperationalStateTable"]["Properties"]["TableName"] == {
        "Fn::Sub": "mr-lister-phase4-${EnvironmentName}"
    }
    function_names = {
        resource["Properties"]["FunctionName"]["Fn::Sub"]
        for resource in resources.values()
        if resource["Type"] == "AWS::Serverless::Function"
    }
    assert function_names == {
        "mr-lister-phase4-${EnvironmentName}-prepare",
        "mr-lister-phase4-${EnvironmentName}-register-wait",
        "mr-lister-phase4-${EnvironmentName}-approval",
        "mr-lister-phase4-${EnvironmentName}-fake-publish",
        "mr-lister-phase4-${EnvironmentName}-fake-verify",
    }
    assert resources["DurableWorkflow"]["Properties"]["Name"] == {
        "Fn::Sub": "mr-lister-phase4-${EnvironmentName}"
    }


def test_lambda_functions_use_explicit_least_privilege_roles() -> None:
    resources = load_template()["Resources"]
    function_roles = {
        logical_id: resource["Properties"]["Role"]["Fn::GetAtt"][0]
        for logical_id, resource in resources.items()
        if resource["Type"] == "AWS::Serverless::Function"
    }

    assert function_roles == {
        "PrepareFunction": "PrepareFunctionRole",
        "RegisterApprovalWaitFunction": "RegisterApprovalWaitFunctionRole",
        "ApprovalFunction": "ApprovalFunctionRole",
        "FakePublishFunction": "FakePublishFunctionRole",
        "FakeVerifyFunction": "FakeVerifyFunctionRole",
    }
    for role_name in function_roles.values():
        role = resources[role_name]
        assert role["Type"] == "AWS::IAM::Role"
        assert "ManagedPolicyArns" not in role["Properties"]

    serialized = json.dumps(
        {role_name: resources[role_name] for role_name in function_roles.values()}
    )
    assert "AWSLambdaBasicExecutionRole" not in serialized
    assert "logs:CreateLogGroup" not in serialized
    for role_name in ("ApprovalFunctionRole", "FakeVerifyFunctionRole"):
        policies = resources[role_name]["Properties"]["Policies"]
        assert "dynamodb:PutItem" in json.dumps(policies)
        assert "dynamodb:TransactWriteItems" in json.dumps(policies)


def test_bootstrap_keeps_developer_and_execution_roles_separate_and_scoped() -> None:
    resources = load_bootstrap()["Resources"]
    execution_role = resources["CloudFormationExecutionRole"]["Properties"]
    developer_policy = resources["DeveloperDeploymentPolicy"]["Properties"]

    assert execution_role["AssumeRolePolicyDocument"]["Statement"] == [
        {
            "Effect": "Allow",
            "Principal": {"Service": "cloudformation.amazonaws.com"},
            "Action": "sts:AssumeRole",
        }
    ]
    developer_serialized = json.dumps(developer_policy["PolicyDocument"])
    assert "cloudformation:ExecuteChangeSet" in developer_serialized
    assert "iam:PassRole" in developer_serialized
    assert "iam:CreateRole" not in developer_serialized
    assert "s3:*" not in developer_serialized
    assert 'Resource": "*' not in developer_serialized

    execution_serialized = json.dumps(execution_role["Policies"])
    assert "mr-lister-phase4-${EnvironmentName}" in execution_serialized
    assert "aws:transform/Serverless-2016-10-31" in execution_serialized
    assert "cloudformation:CreateChangeSet" in execution_serialized
    assert "iam:PassRole" in execution_serialized
    assert "bedrock" not in execution_serialized
    assert "secretsmanager" not in execution_serialized


def test_bootstrap_canary_permissions_are_runtime_scoped() -> None:
    statements = load_bootstrap()["Resources"]["DeveloperDeploymentPolicy"]["Properties"][
        "PolicyDocument"
    ]["Statement"]
    canary = {
        statement["Sid"]: statement
        for statement in statements
        if "Canary" in statement["Sid"] or statement["Sid"].startswith("InvokeOnly")
    }

    assert set(canary) == {
        "RunCanaryAgainstPrivateArtworkOnly",
        "RunCanaryAgainstPhase4StateOnly",
        "StartOnlyThePhase4CanaryWorkflow",
        "ReadOnlyPhase4CanaryExecutions",
        "ListOnlyPhase4CanaryExecutions",
        "InvokeOnlyThePhase4ApprovalFunction",
    }
    serialized = json.dumps(canary)
    assert "private/artwork/*" in serialized
    assert "table/mr-lister-phase4-${EnvironmentName}" in serialized
    assert "stateMachine:mr-lister-phase4-${EnvironmentName}" in serialized
    assert "execution:mr-lister-phase4-${EnvironmentName}:*" in serialized
    assert "function:mr-lister-phase4-${EnvironmentName}-approval" in serialized
    assert 'Resource": "*"' not in serialized
    assert "Delete" not in serialized
