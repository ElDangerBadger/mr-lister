from __future__ import annotations

import copy
import json
import re
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path
from typing import Any

import pytest

import tools.verify_phase6_runtime_update as update_verifier
from tools.verify_phase6_runtime_update import (
    FOUNDATION_BINDING_FORMAT,
    FOUNDATION_TEMPLATE_FINGERPRINT,
    UPDATE_EVIDENCE_FORMAT,
    UPDATE_MANIFEST_FORMAT,
    Phase6RuntimeUpdateError,
    main,
    semantic_fingerprint,
    verify_reviewed_update,
)
from tools.verify_phase6_s3_release_object import VerifiedPhase6S3ReleaseObject

ROOT = Path(__file__).resolve().parents[1]
FOUNDATION_TEMPLATE = ROOT / "infra/phase6/foundation.json"
RUNTIME_UPDATE_BOOTSTRAP = ROOT / "infra/phase6/runtime-update-bootstrap.json"
ACCOUNT = "123456789012"
REGION = "us-west-2"
ENVIRONMENT = "dev"
STACK_NAME = "mr-lister-phase6-dev"
STACK_ID = (
    f"arn:aws:cloudformation:{REGION}:{ACCOUNT}:stack/{STACK_NAME}/"
    "11111111-2222-4333-8444-555555555555"
)
FOUNDATION_ROLE = f"arn:aws:iam::{ACCOUNT}:role/mr-lister-phase6-foundation-cfn-dev"
EXECUTION_ROLE_NAME = "mr-lister-phase6-runtime-cfn-dev"
EXECUTION_ROLE = f"arn:aws:iam::{ACCOUNT}:role/{EXECUTION_ROLE_NAME}"
EXECUTION_POLICY_NAME = "mr-lister-phase6-runtime-execution-dev"
DEPLOYER_ROLE_NAME = "mr-lister-phase6-runtime-update-deployer-dev"
DEPLOYER_ROLE = f"arn:aws:iam::{ACCOUNT}:role/{DEPLOYER_ROLE_NAME}"
DEPLOYER_POLICY_NAME = DEPLOYER_ROLE_NAME
EXECUTION_ROLE_ID = "AROAEXECUTIONROLE123456"
DEPLOYER_ROLE_ID = "AROADEPLOYERROLE1234567"
ACCESS_KEY_ID = "ASIAEXAMPLEACCESSKEY01"
RELEASE_FINGERPRINT = "1" * 64
LAMBDA_ARCHIVE_BYTES = b"sealed Phase 6 Lambda test archive\n"
LAMBDA_ARCHIVE_SHA256 = sha256(LAMBDA_ARCHIVE_BYTES).hexdigest()
LAMBDA_VERSION_ID = "lambda-version-123"
LAMBDA_EVIDENCE_DOCUMENT = {
    "archive_sha256": LAMBDA_ARCHIVE_SHA256,
    "component": "lambda",
    "release_fingerprint": RELEASE_FINGERPRINT,
    "version_id": LAMBDA_VERSION_ID,
}
LAMBDA_EVIDENCE_BYTES = (
    json.dumps(
        LAMBDA_EVIDENCE_DOCUMENT,
        indent=2,
        separators=(",", ": "),
        sort_keys=True,
    )
    + "\n"
).encode()
LAMBDA_EVIDENCE_SHA256 = sha256(LAMBDA_EVIDENCE_BYTES).hexdigest()
TABLE_ARN = f"arn:aws:dynamodb:{REGION}:{ACCOUNT}:table/{STACK_NAME}"
STREAM_ARN = f"{TABLE_ARN}/stream/2026-08-24T18:00:00.000"
BUCKET = f"mr-lister-phase6-artifacts-{ENVIRONMENT}-{ACCOUNT}-{REGION}"
BUCKET_ARN = f"arn:aws:s3:::{BUCKET}"
FOUNDATION_CHANGE_SET_NAME = (
    f"{STACK_NAME}-foundation-create-{FOUNDATION_TEMPLATE_FINGERPRINT[:12]}"
)
FOUNDATION_CHANGE_SET_ID = (
    f"arn:aws:cloudformation:{REGION}:{ACCOUNT}:changeSet/{FOUNDATION_CHANGE_SET_NAME}/"
    "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
)
FOUNDATION_CREATION_TIME = "2026-08-24T16:00:00+00:00"
FOUNDATION_UPDATED_TIME = "2026-08-24T16:13:15+00:00"
OPERATION_ID = "97d0ab01-b904-49d3-9dcc-9cc249a90008"
EVENT_ID = "22222222-3333-4444-8555-666666666666"
REQUEST_ID = "33333333-4444-4555-8666-777777777777"


def _utc_z(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _times() -> tuple[datetime, datetime, datetime]:
    event = datetime.now(UTC).replace(microsecond=0) - timedelta(minutes=1)
    created = event + timedelta(seconds=5)
    expires = event + timedelta(minutes=10)
    return event, created, expires


@pytest.fixture(autouse=True)
def _closed_local_lambda_artifact_verifiers(monkeypatch: pytest.MonkeyPatch) -> None:
    def verify_artifacts(
        deployment_root: Path,
        *,
        artifact_root: Path,
        verify_current_source: bool = True,
    ) -> dict[str, Any]:
        del verify_current_source
        assert deployment_root.name == "phase6-deployment"
        raw = (artifact_root / "phase6-lambda.zip").read_bytes()
        return {
            "release_fingerprint": RELEASE_FINGERPRINT,
            "components": {
                "lambda": {
                    "archive": {
                        "path": "phase6-lambda.zip",
                        "sha256": sha256(raw).hexdigest(),
                        "size_bytes": len(raw),
                    }
                }
            },
        }

    def verify_object(expectation: Any, *, evidence_path: Path) -> VerifiedPhase6S3ReleaseObject:
        raw = evidence_path.read_bytes()
        document = json.loads(raw)
        if (
            document
            != {
                "archive_sha256": expectation.archive_sha256,
                "component": "lambda",
                "release_fingerprint": expectation.release_fingerprint,
                "version_id": LAMBDA_VERSION_ID,
            }
            or raw
            != (
                json.dumps(document, indent=2, separators=(",", ": "), sort_keys=True) + "\n"
            ).encode()
        ):
            raise ValueError
        return VerifiedPhase6S3ReleaseObject(
            account_id=expectation.account_id,
            region=expectation.region,
            environment=expectation.environment,
            component="lambda",
            release_fingerprint=expectation.release_fingerprint,
            archive_sha256=expectation.archive_sha256,
            size_bytes=expectation.size_bytes,
            checksum_sha256_base64=expectation.checksum_sha256_base64,
            bucket=expectation.bucket,
            key=expectation.key,
            version_id=document["version_id"],
            evidence_sha256=sha256(raw).hexdigest(),
        )

    monkeypatch.setattr(update_verifier, "verify_phase6_deployment_artifacts", verify_artifacts)
    monkeypatch.setattr(
        update_verifier,
        "verify_phase6_s3_release_object_evidence",
        verify_object,
    )


def _target_template() -> dict[str, Any]:
    template = json.loads(FOUNDATION_TEMPLATE.read_text(encoding="utf-8"))
    template["Parameters"]["EnvironmentName"]["AllowedValues"] = [ENVIRONMENT]
    template["Parameters"]["ReleaseFingerprint"] = {
        "AllowedPattern": "^(?!0{64}$)[a-f0-9]{64}$",
        "AllowedValues": [RELEASE_FINGERPRINT],
        "Default": RELEASE_FINGERPRINT,
        "Type": "String",
    }
    template["Description"] = "Reviewed Phase 6 runtime transition test template"
    template["Transform"] = "AWS::Serverless-2016-10-31"
    template["Resources"]["PrivateArtifactBucket"]["Properties"]["CorsConfiguration"] = {
        "CorsRules": [
            {
                "AllowedHeaders": ["content-type"],
                "AllowedMethods": ["POST"],
                "AllowedOrigins": ["https://app.example.test"],
                "MaxAge": 300,
            }
        ]
    }
    template["Resources"]["RuntimeWorker"] = {
        "Type": "AWS::Serverless::Function",
        "Properties": {
            "Architectures": ["arm64"],
            "CodeUri": {
                "Bucket": BUCKET,
                "Key": (
                    f"private/deployments/lambda/releases/{RELEASE_FINGERPRINT}/"
                    f"phase6-lambda-{LAMBDA_ARCHIVE_SHA256}.zip"
                ),
                "Version": LAMBDA_VERSION_ID,
            },
            "Runtime": "python3.12",
        },
    }
    return template


def _processed_template(target: dict[str, Any]) -> dict[str, Any]:
    processed = copy.deepcopy(target)
    processed.pop("Transform")
    processed["Metadata"] = {"CloudFormationProcessed": True}
    processed["Resources"]["RuntimeWorker"]["Type"] = "AWS::Lambda::Function"
    return processed


def _foundation_binding() -> dict[str, Any]:
    return {
        "account_id": ACCOUNT,
        "artifact_bucket_arn": BUCKET_ARN,
        "artifact_bucket_name": BUCKET,
        "environment_name": ENVIRONMENT,
        "format": FOUNDATION_BINDING_FORMAT,
        "foundation_template_fingerprint": FOUNDATION_TEMPLATE_FINGERPRINT,
        "operational_state_stream_arn": STREAM_ARN,
        "operational_state_table_arn": TABLE_ARN,
        "operational_state_table_name": STACK_NAME,
        "region": REGION,
        "stack_id": STACK_ID,
        "stack_name": STACK_NAME,
    }


def _stack_tags() -> list[dict[str, str]]:
    return [
        {"Key": "DeploymentClass", "Value": "FOUNDATION_ONLY"},
        {"Key": "Environment", "Value": ENVIRONMENT},
        {"Key": "Project", "Value": "MrLister"},
    ]


def _pre_stack() -> dict[str, Any]:
    return {
        "Stacks": [
            {
                "ChangeSetId": FOUNDATION_CHANGE_SET_ID,
                "CreationTime": FOUNDATION_CREATION_TIME,
                "DeploymentConfig": {"DisableRollback": True, "Mode": "STANDARD"},
                "Description": "Mr Lister Phase 6 create-only durable foundation",
                "DisableRollback": True,
                "DriftInformation": {"StackDriftStatus": "NOT_CHECKED"},
                "EnableTerminationProtection": True,
                "LastOperations": [{"OperationId": OPERATION_ID, "OperationType": "CREATE_STACK"}],
                "LastUpdatedTime": FOUNDATION_UPDATED_TIME,
                "NotificationARNs": [],
                "Outputs": [
                    {"OutputKey": "ArtifactBucketArn", "OutputValue": BUCKET_ARN},
                    {"OutputKey": "ArtifactBucketName", "OutputValue": BUCKET},
                    {
                        "Description": "Create-only durable foundation",
                        "OutputKey": "DeploymentReadiness",
                        "OutputValue": "FOUNDATION_ONLY",
                    },
                    {"OutputKey": "StateTableArn", "OutputValue": TABLE_ARN},
                    {"OutputKey": "StateTableName", "OutputValue": STACK_NAME},
                ],
                "Parameters": [{"ParameterKey": "EnvironmentName", "ParameterValue": ENVIRONMENT}],
                "RoleARN": FOUNDATION_ROLE,
                "RollbackConfiguration": {},
                "StackId": STACK_ID,
                "StackName": STACK_NAME,
                "StackStatus": "CREATE_COMPLETE",
                "Tags": _stack_tags(),
            }
        ]
    }


def _resource(logical_id: str, resource_type: str, physical_id: str) -> dict[str, Any]:
    return {
        "DriftInformation": {"StackResourceDriftStatus": "NOT_CHECKED"},
        "LastUpdatedTimestamp": FOUNDATION_UPDATED_TIME,
        "LogicalResourceId": logical_id,
        "PhysicalResourceId": physical_id,
        "ResourceStatus": "CREATE_COMPLETE",
        "ResourceType": resource_type,
    }


def _pre_stack_resources() -> dict[str, Any]:
    return {
        "StackResourceSummaries": [
            _resource("OperationalStateTable", "AWS::DynamoDB::Table", STACK_NAME),
            _resource("PrivateArtifactBucket", "AWS::S3::Bucket", BUCKET),
            _resource("PrivateArtifactBucketPolicy", "AWS::S3::BucketPolicy", BUCKET),
        ]
    }


def _bucket_before_context() -> dict[str, Any]:
    return {
        "DeletionPolicy": "Retain",
        "Properties": {"BucketName": BUCKET},
        "UpdateReplacePolicy": "Retain",
    }


def _bucket_after_context() -> dict[str, Any]:
    context = _bucket_before_context()
    context["Properties"]["CorsConfiguration"] = {
        "CorsRules": [{"AllowedMethods": ["POST"], "AllowedOrigins": ["https://app.example.test"]}]
    }
    return context


def _bucket_detail() -> dict[str, Any]:
    return {
        "ChangeSource": "DirectModification",
        "Evaluation": "Static",
        "Target": {
            "AfterValue": json.dumps(
                {
                    "CorsRules": [
                        {
                            "AllowedMethods": ["POST"],
                            "AllowedOrigins": ["https://app.example.test"],
                        }
                    ]
                },
                separators=(",", ":"),
                sort_keys=True,
            ),
            "Attribute": "Properties",
            "AttributeChangeType": "Add",
            "BeforeValue": "null",
            "Name": "CorsConfiguration",
            "Path": "/Properties/CorsConfiguration",
            "RequiresRecreation": "Never",
        },
    }


def _normalized_changes() -> list[dict[str, Any]]:
    return [
        {
            "action": "Modify",
            "after_context": _bucket_after_context(),
            "before_context": _bucket_before_context(),
            "details": [_bucket_detail()],
            "logical_resource_id": "PrivateArtifactBucket",
            "physical_resource_id": BUCKET,
            "replacement": "False",
            "resource_type": "AWS::S3::Bucket",
            "scope": ["Properties"],
        },
        {
            "action": "Add",
            "after_context": {"Properties": {"Architectures": ["arm64"], "Runtime": "python3.12"}},
            "before_context": None,
            "details": [],
            "logical_resource_id": "RuntimeWorker",
            "physical_resource_id": None,
            "replacement": None,
            "resource_type": "AWS::Lambda::Function",
            "scope": [],
        },
    ]


def _execution_policy() -> dict[str, Any]:
    template = json.loads(RUNTIME_UPDATE_BOOTSTRAP.read_text(encoding="utf-8"))
    policy = template["Resources"]["CoreRuntimeExecutionRole"]["Properties"]["Policies"][0][
        "PolicyDocument"
    ]
    substitutions = {
        "AWS::AccountId": ACCOUNT,
        "AWS::Partition": "aws",
        "LambdaArchiveSha256": LAMBDA_ARCHIVE_SHA256,
        "LambdaVersionId": LAMBDA_VERSION_ID,
        "ReleaseFingerprint": RELEASE_FINGERPRINT,
    }

    def resolve(value: Any) -> Any:
        if isinstance(value, list):
            return [resolve(item) for item in value]
        if not isinstance(value, dict):
            return value
        if set(value) == {"Ref"}:
            return substitutions[value["Ref"]]
        if set(value) == {"Fn::Sub"}:
            return re.sub(
                r"\$\{([^}]+)\}",
                lambda match: substitutions[match.group(1)],
                value["Fn::Sub"],
            )
        return {key: resolve(item) for key, item in value.items()}

    resolved = resolve(policy)
    assert isinstance(resolved, dict)
    return resolved


def _template_url(target_fingerprint: str) -> str:
    del target_fingerprint
    return (
        f"https://{BUCKET}.s3.{REGION}.amazonaws.com/private/deployments/cloudformation/"
        f"core/releases/{RELEASE_FINGERPRINT}/core-template.json"
        "?versionId=version-123"
    )


def _manifest(
    binding: dict[str, Any],
    target: dict[str, Any],
    processed: dict[str, Any],
    expires_at: datetime,
) -> dict[str, Any]:
    fingerprint = semantic_fingerprint(target)
    return {
        "account_id": ACCOUNT,
        "capabilities": ["CAPABILITY_NAMED_IAM"],
        "change_set_description": f"Mr Lister Phase 6 reviewed UPDATE {fingerprint}",
        "change_set_name": f"{STACK_NAME}-runtime-update-{fingerprint[:12]}",
        "changes": _normalized_changes(),
        "client_token": f"phase6-{fingerprint[:32]}",
        "deployment_config": {"DisableRollback": False, "Mode": "STANDARD"},
        "deployer_policy_name": DEPLOYER_POLICY_NAME,
        "deployer_role_arn": DEPLOYER_ROLE,
        "deployer_session_name": f"phase6-update-{fingerprint[:12]}",
        "environment_name": ENVIRONMENT,
        "execution_role_arn": EXECUTION_ROLE,
        "execution_role_policy_fingerprint": semantic_fingerprint(_execution_policy()),
        "execution_role_policy_name": EXECUTION_POLICY_NAME,
        "format": UPDATE_MANIFEST_FORMAT,
        "foundation_binding_fingerprint": semantic_fingerprint(binding),
        "lambda_release_object_evidence_fingerprint": LAMBDA_EVIDENCE_SHA256,
        "notification_arns": [],
        "parameters": {
            "EnvironmentName": ENVIRONMENT,
            "ReleaseFingerprint": RELEASE_FINGERPRINT,
        },
        "policy_expires_at": _utc_z(expires_at),
        "processed_template_fingerprint": semantic_fingerprint(processed),
        "region": REGION,
        "rollback_configuration": {},
        "stack_id": STACK_ID,
        "stack_name": STACK_NAME,
        "tags": {
            "DeploymentClass": "RUNTIME_UPDATE",
            "Environment": ENVIRONMENT,
            "Project": "MrLister",
        },
        "target_template_fingerprint": fingerprint,
        "template_url": _template_url(fingerprint),
    }


def _change_set(manifest: dict[str, Any], created_at: datetime) -> dict[str, Any]:
    change_set_name = manifest["change_set_name"]
    return {
        "Capabilities": ["CAPABILITY_NAMED_IAM"],
        "ChangeSetId": (
            f"arn:aws:cloudformation:{REGION}:{ACCOUNT}:changeSet/{change_set_name}/"
            "bbbbbbbb-cccc-4ddd-8eee-ffffffffffff"
        ),
        "ChangeSetName": change_set_name,
        "Changes": [
            {
                "ResourceChange": {
                    "Action": "Modify",
                    "AfterContext": json.dumps(_bucket_after_context()),
                    "BeforeContext": json.dumps(_bucket_before_context()),
                    "Details": [_bucket_detail()],
                    "LogicalResourceId": "PrivateArtifactBucket",
                    "PhysicalResourceId": BUCKET,
                    "Replacement": "False",
                    "ResourceType": "AWS::S3::Bucket",
                    "Scope": ["Properties"],
                },
                "Type": "Resource",
            },
            {
                "ResourceChange": {
                    "Action": "Add",
                    "AfterContext": json.dumps(
                        {
                            "Properties": {
                                "Architectures": ["arm64"],
                                "Runtime": "python3.12",
                            }
                        }
                    ),
                    "Details": [],
                    "LogicalResourceId": "RuntimeWorker",
                    "ResourceType": "AWS::Lambda::Function",
                    "Scope": [],
                },
                "Type": "Resource",
            },
        ],
        "CreationTime": created_at.isoformat(),
        "DeploymentConfig": {"DisableRollback": False, "Mode": "STANDARD"},
        "DeploymentMode": None,
        "Description": manifest["change_set_description"],
        "ExecutionStatus": "AVAILABLE",
        "ImportExistingResources": None,
        "IncludeNestedStacks": False,
        "NotificationARNs": [],
        "OnStackFailure": None,
        "Parameters": [
            {"ParameterKey": "EnvironmentName", "ParameterValue": ENVIRONMENT},
            {
                "ParameterKey": "ReleaseFingerprint",
                "ParameterValue": RELEASE_FINGERPRINT,
            },
        ],
        "ParentChangeSetId": None,
        "RollbackConfiguration": {},
        "RootChangeSetId": None,
        "StackDriftStatus": None,
        "StackId": STACK_ID,
        "StackName": STACK_NAME,
        "Status": "CREATE_COMPLETE",
        "StatusReason": None,
        "Tags": [
            {"Key": "DeploymentClass", "Value": "RUNTIME_UPDATE"},
            {"Key": "Environment", "Value": ENVIRONMENT},
            {"Key": "Project", "Value": "MrLister"},
        ],
    }


def _trust(principal: dict[str, str]) -> dict[str, Any]:
    return {
        "Statement": [{"Action": "sts:AssumeRole", "Effect": "Allow", "Principal": principal}],
        "Version": "2012-10-17",
    }


def _role(
    *, name: str, arn: str, role_id: str, trust: dict[str, Any], tags: dict[str, str]
) -> dict[str, Any]:
    return {
        "Role": {
            "Arn": arn,
            "AssumeRolePolicyDocument": trust,
            "CreateDate": "2026-08-24T16:30:00+00:00",
            "MaxSessionDuration": 3600,
            "Path": "/",
            "RoleId": role_id,
            "RoleName": name,
            "Tags": [{"Key": key, "Value": value} for key, value in sorted(tags.items())],
        }
    }


def _deployer_policy(manifest: dict[str, Any]) -> dict[str, Any]:
    template_key = (
        f"private/deployments/cloudformation/core/releases/{RELEASE_FINGERPRINT}/core-template.json"
    )
    change_set_arn = (
        f"arn:aws:cloudformation:{REGION}:{ACCOUNT}:changeSet/{manifest['change_set_name']}/*"
    )
    return {
        "Statement": [
            {
                "Action": "cloudformation:CreateChangeSet",
                "Condition": {
                    "DateLessThan": {"aws:CurrentTime": manifest["policy_expires_at"]},
                    "ForAllValues:StringEquals": {
                        "aws:TagKeys": ["DeploymentClass", "Environment", "Project"]
                    },
                    "StringEquals": {
                        "aws:RequestTag/DeploymentClass": "RUNTIME_UPDATE",
                        "aws:RequestTag/Environment": ENVIRONMENT,
                        "aws:RequestTag/Project": "MrLister",
                        "cloudformation:ChangeSetName": manifest["change_set_name"],
                        "cloudformation:RoleArn": EXECUTION_ROLE,
                        "cloudformation:TemplateUrl": manifest["template_url"],
                    },
                },
                "Effect": "Allow",
                "Resource": [
                    STACK_ID,
                    f"arn:aws:cloudformation:{REGION}:aws:transform/Serverless-2016-10-31",
                ],
                "Sid": "CreateExactReviewedRuntimeUpdate",
            },
            {
                "Action": "iam:PassRole",
                "Condition": {
                    "DateLessThan": {"aws:CurrentTime": manifest["policy_expires_at"]},
                    "StringEquals": {"iam:PassedToService": "cloudformation.amazonaws.com"},
                },
                "Effect": "Allow",
                "Resource": EXECUTION_ROLE,
                "Sid": "PassExactRuntimeExecutionRole",
            },
            {
                "Action": "s3:GetObjectVersion",
                "Condition": {
                    "DateLessThan": {"aws:CurrentTime": manifest["policy_expires_at"]},
                    "StringEquals": {"s3:VersionId": "version-123"},
                },
                "Effect": "Allow",
                "Resource": f"arn:aws:s3:::{BUCKET}/{template_key}",
                "Sid": "ReadOnlyExactReviewedTemplateVersion",
            },
            {
                "Action": ["cloudformation:DescribeChangeSet", "cloudformation:GetTemplate"],
                "Condition": {"DateLessThan": {"aws:CurrentTime": manifest["policy_expires_at"]}},
                "Effect": "Allow",
                "Resource": [STACK_ID, change_set_arn],
                "Sid": "ReadOnlyExactReviewedChangeSet",
            },
            {
                "Action": ["cloudformation:DescribeStacks", "cloudformation:ListStackResources"],
                "Condition": {"DateLessThan": {"aws:CurrentTime": manifest["policy_expires_at"]}},
                "Effect": "Allow",
                "Resource": STACK_ID,
                "Sid": "ReadOnlyExactFoundationStack",
            },
            {
                "Action": [
                    "iam:GetRole",
                    "iam:GetRolePolicy",
                    "iam:ListAttachedRolePolicies",
                    "iam:ListRolePolicies",
                ],
                "Condition": {"DateLessThan": {"aws:CurrentTime": manifest["policy_expires_at"]}},
                "Effect": "Allow",
                "Resource": [DEPLOYER_ROLE, EXECUTION_ROLE],
                "Sid": "ReadBackOnlyExactDeploymentRoles",
            },
            {
                "Action": "cloudtrail:LookupEvents",
                "Condition": {
                    "DateLessThan": {"aws:CurrentTime": manifest["policy_expires_at"]},
                    "StringEquals": {"aws:RequestedRegion": REGION},
                },
                "Effect": "Allow",
                "Resource": "*",
                "Sid": "ReadOnlyRegionalCreateEventHistory",
            },
        ],
        "Version": "2012-10-17",
    }


def _caller(manifest: dict[str, Any]) -> dict[str, str]:
    session = manifest["deployer_session_name"]
    return {
        "Account": ACCOUNT,
        "Arn": f"arn:aws:sts::{ACCOUNT}:assumed-role/{DEPLOYER_ROLE_NAME}/{session}",
        "UserId": f"{DEPLOYER_ROLE_ID}:{session}",
    }


def _cloudtrail(
    manifest: dict[str, Any], change_set: dict[str, Any], event_time: datetime
) -> dict[str, Any]:
    caller = _caller(manifest)
    session = manifest["deployer_session_name"]
    event = {
        "awsRegion": REGION,
        "eventCategory": "Management",
        "eventID": EVENT_ID,
        "eventName": "CreateChangeSet",
        "eventSource": "cloudformation.amazonaws.com",
        "eventTime": _utc_z(event_time),
        "eventType": "AwsApiCall",
        "eventVersion": "1.09",
        "managementEvent": True,
        "readOnly": False,
        "recipientAccountId": ACCOUNT,
        "requestID": REQUEST_ID,
        "requestParameters": {
            "capabilities": ["CAPABILITY_NAMED_IAM"],
            "changeSetName": manifest["change_set_name"],
            "changeSetType": "UPDATE",
            "clientToken": manifest["client_token"],
            "deploymentConfig": {"disableRollback": False, "mode": "STANDARD"},
            "description": manifest["change_set_description"],
            "importExistingResources": False,
            "includeNestedStacks": False,
            "notificationARNs": [],
            "parameters": [
                {"parameterKey": "EnvironmentName"},
                {"parameterKey": "ReleaseFingerprint"},
            ],
            "roleARN": EXECUTION_ROLE,
            "rollbackConfiguration": {},
            "stackName": STACK_ID,
            "tags": [
                {"key": "DeploymentClass", "value": "RUNTIME_UPDATE"},
                {"key": "Environment", "value": ENVIRONMENT},
                {"key": "Project", "value": "MrLister"},
            ],
            "templateURL": manifest["template_url"],
        },
        "responseElements": {"id": change_set["ChangeSetId"], "stackId": STACK_ID},
        "sessionCredentialFromConsole": "false",
        "sourceIPAddress": "203.0.113.5",
        "tlsDetails": {
            "cipherSuite": "TLS_AES_128_GCM_SHA256",
            "clientProvidedHostHeader": f"cloudformation.{REGION}.amazonaws.com",
            "tlsVersion": "TLSv1.3",
        },
        "userAgent": "aws-cli/2.36.25",
        "userIdentity": {
            "accessKeyId": ACCESS_KEY_ID,
            "accountId": ACCOUNT,
            "arn": caller["Arn"],
            "principalId": caller["UserId"],
            "sessionContext": {
                "attributes": {
                    "creationDate": _utc_z(event_time - timedelta(minutes=1)),
                    "mfaAuthenticated": "false",
                },
                "sessionIssuer": {
                    "accountId": ACCOUNT,
                    "arn": DEPLOYER_ROLE,
                    "principalId": DEPLOYER_ROLE_ID,
                    "type": "Role",
                    "userName": DEPLOYER_ROLE_NAME,
                },
            },
            "type": "AssumedRole",
        },
    }
    return {
        "Events": [
            {
                "AccessKeyId": ACCESS_KEY_ID,
                "CloudTrailEvent": json.dumps(event, separators=(",", ":"), sort_keys=True),
                "EventId": EVENT_ID,
                "EventName": "CreateChangeSet",
                "EventSource": "cloudformation.amazonaws.com",
                "EventTime": event_time.isoformat(),
                "ReadOnly": "false",
                "Resources": [],
                "Username": session,
            }
        ]
    }


def _documents() -> dict[str, dict[str, Any]]:
    event_time, change_set_time, expires = _times()
    target = _target_template()
    processed = _processed_template(target)
    binding = _foundation_binding()
    manifest = _manifest(binding, target, processed, expires)
    change_set = _change_set(manifest, change_set_time)
    execution_role = _role(
        name=EXECUTION_ROLE_NAME,
        arn=EXECUTION_ROLE,
        role_id=EXECUTION_ROLE_ID,
        trust=_trust({"Service": "cloudformation.amazonaws.com"}),
        tags={
            "DeploymentClass": "RUNTIME_CFN_EXECUTION",
            "Environment": ENVIRONMENT,
            "Project": "MrLister",
        },
    )
    deployer_role = _role(
        name=DEPLOYER_ROLE_NAME,
        arn=DEPLOYER_ROLE,
        role_id=DEPLOYER_ROLE_ID,
        trust=_trust({"AWS": f"arn:aws:iam::{ACCOUNT}:user/mr-lister-dev"}),
        tags={
            "DeploymentClass": "RUNTIME_UPDATE_DEPLOYER",
            "Environment": ENVIRONMENT,
            "ExpiresAt": manifest["policy_expires_at"],
            "Project": "MrLister",
        },
    )
    return {
        "caller-identity.json": _caller(manifest),
        "change-set.json": change_set,
        "cloudtrail.json": _cloudtrail(manifest, change_set, event_time),
        "deployer-attached-policies.json": {"AttachedPolicies": []},
        "deployer-inline-policies.json": {"PolicyNames": [DEPLOYER_POLICY_NAME]},
        "deployer-policy.json": {
            "PolicyDocument": _deployer_policy(manifest),
            "PolicyName": DEPLOYER_POLICY_NAME,
            "RoleName": DEPLOYER_ROLE_NAME,
        },
        "deployer-role.json": deployer_role,
        "execution-attached-policies.json": {"AttachedPolicies": []},
        "execution-inline-policies.json": {"PolicyNames": [EXECUTION_POLICY_NAME]},
        "execution-policy.json": {
            "PolicyDocument": _execution_policy(),
            "PolicyName": EXECUTION_POLICY_NAME,
            "RoleName": EXECUTION_ROLE_NAME,
        },
        "execution-role.json": execution_role,
        "expected-manifest.json": manifest,
        "foundation-binding.json": binding,
        "original-template.json": {
            "StagesAvailable": ["Original", "Processed"],
            "TemplateBody": target,
        },
        "pre-stack-resources.json": _pre_stack_resources(),
        "pre-stack.json": _pre_stack(),
        "processed-template.json": {
            "StagesAvailable": ["Original", "Processed"],
            "TemplateBody": processed,
        },
        "target-template.json": target,
    }


def _write_documents(root: Path, documents: dict[str, dict[str, Any]]) -> None:
    for name, document in documents.items():
        (root / name).write_text(json.dumps(document), encoding="utf-8")
    deployment_root = root / "phase6-deployment"
    artifact_root = root / "phase6-artifacts"
    deployment_root.mkdir(exist_ok=True)
    artifact_root.mkdir(exist_ok=True)
    (artifact_root / "phase6-lambda.zip").write_bytes(LAMBDA_ARCHIVE_BYTES)
    (artifact_root / "deployment-descriptor.json").write_text("{}", encoding="utf-8")
    (root / "lambda-object-evidence.json").write_bytes(LAMBDA_EVIDENCE_BYTES)


def _verify(root: Path) -> dict[str, object]:
    return verify_reviewed_update(
        deployment_root=root / "phase6-deployment",
        artifact_root=root / "phase6-artifacts",
        lambda_object_evidence_path=root / "lambda-object-evidence.json",
        foundation_binding_path=root / "foundation-binding.json",
        expected_manifest_path=root / "expected-manifest.json",
        pre_stack_observation_path=root / "pre-stack.json",
        pre_stack_resources_observation_path=root / "pre-stack-resources.json",
        change_set_observation_path=root / "change-set.json",
        original_template_observation_path=root / "original-template.json",
        processed_template_observation_path=root / "processed-template.json",
        target_template_path=root / "target-template.json",
        caller_identity_observation_path=root / "caller-identity.json",
        cloudtrail_observation_path=root / "cloudtrail.json",
        execution_role_observation_path=root / "execution-role.json",
        execution_role_inline_policies_observation_path=root / "execution-inline-policies.json",
        execution_role_attached_policies_observation_path=root / "execution-attached-policies.json",
        execution_role_policy_observation_path=root / "execution-policy.json",
        deployer_role_observation_path=root / "deployer-role.json",
        deployer_role_inline_policies_observation_path=root / "deployer-inline-policies.json",
        deployer_role_attached_policies_observation_path=root / "deployer-attached-policies.json",
        deployer_role_policy_observation_path=root / "deployer-policy.json",
    )


def _cli_args(root: Path) -> list[str]:
    return [
        "--deployment-root",
        str(root / "phase6-deployment"),
        "--artifact-root",
        str(root / "phase6-artifacts"),
        "--lambda-object-evidence",
        str(root / "lambda-object-evidence.json"),
        "--foundation-binding",
        str(root / "foundation-binding.json"),
        "--expected-manifest",
        str(root / "expected-manifest.json"),
        "--pre-stack-observation",
        str(root / "pre-stack.json"),
        "--pre-stack-resources-observation",
        str(root / "pre-stack-resources.json"),
        "--change-set-observation",
        str(root / "change-set.json"),
        "--original-template-observation",
        str(root / "original-template.json"),
        "--processed-template-observation",
        str(root / "processed-template.json"),
        "--target-template",
        str(root / "target-template.json"),
        "--caller-identity-observation",
        str(root / "caller-identity.json"),
        "--cloudtrail-observation",
        str(root / "cloudtrail.json"),
        "--execution-role-observation",
        str(root / "execution-role.json"),
        "--execution-role-inline-policies-observation",
        str(root / "execution-inline-policies.json"),
        "--execution-role-attached-policies-observation",
        str(root / "execution-attached-policies.json"),
        "--execution-role-policy-observation",
        str(root / "execution-policy.json"),
        "--deployer-role-observation",
        str(root / "deployer-role.json"),
        "--deployer-role-inline-policies-observation",
        str(root / "deployer-inline-policies.json"),
        "--deployer-role-attached-policies-observation",
        str(root / "deployer-attached-policies.json"),
        "--deployer-role-policy-observation",
        str(root / "deployer-policy.json"),
    ]


def _mutate_cloudtrail(documents: dict[str, dict[str, Any]], mutation: Any) -> None:
    wrapper = documents["cloudtrail.json"]["Events"][0]
    event = json.loads(wrapper["CloudTrailEvent"])
    mutation(event)
    wrapper["CloudTrailEvent"] = json.dumps(event, separators=(",", ":"), sort_keys=True)


def test_exact_reviewed_update_returns_short_lived_capture_descriptor(tmp_path: Path) -> None:
    documents = _documents()
    _write_documents(tmp_path, documents)

    result = _verify(tmp_path)

    assert result["format"] == UPDATE_EVIDENCE_FORMAT
    assert result["previous_execution_role_arn"] == FOUNDATION_ROLE
    assert result["execution_role_arn"] == EXECUTION_ROLE
    assert result["deployer_role_arn"] == DEPLOYER_ROLE
    assert result["lambda_archive_sha256"] == LAMBDA_ARCHIVE_SHA256
    assert result["lambda_release_object_evidence_fingerprint"] == LAMBDA_EVIDENCE_SHA256
    assert result["lambda_release_object_version_id"] == LAMBDA_VERSION_ID
    assert result["change_set_type"] == "UPDATE"
    assert result["availability_claim"] == "CAPTURE_ONLY_RECAPTURE_REQUIRED"
    assert result["verification_scope"] == "OFFLINE_CAPTURE_ONLY"
    assert result["processed_template_fingerprint"] == semantic_fingerprint(
        documents["processed-template.json"]["TemplateBody"]
    )
    assert result["recapture_contract"] == {
        "execute_before": documents["expected-manifest.json"]["policy_expires_at"],
        "maximum_review_window_seconds": 900,
        "require_identical_canonical_descriptor": True,
        "required_immediately_before_execute": True,
    }
    assert len(result["evidence_bundle_fingerprint"]) == 64


def test_broadened_execution_policy_fails_even_with_recomputed_manifest_fingerprint(
    tmp_path: Path,
) -> None:
    documents = _documents()
    policy = documents["execution-policy.json"]["PolicyDocument"]
    policy["Statement"].append(
        {
            "Action": "*",
            "Effect": "Allow",
            "Resource": "*",
            "Sid": "UnreviewedAdministratorEscape",
        }
    )
    documents["expected-manifest.json"]["execution_role_policy_fingerprint"] = semantic_fingerprint(
        policy
    )
    _write_documents(tmp_path, documents)

    with pytest.raises(Phase6RuntimeUpdateError, match="evidence is invalid"):
        _verify(tmp_path)


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("archive_sha256", "f" * 64),
        ("version_id", "substituted-version-456"),
    ],
)
def test_substituted_lambda_release_object_evidence_fails_closed(
    tmp_path: Path,
    field: str,
    replacement: str,
) -> None:
    documents = _documents()
    _write_documents(tmp_path, documents)
    evidence = dict(LAMBDA_EVIDENCE_DOCUMENT)
    evidence[field] = replacement
    raw = (json.dumps(evidence, indent=2, separators=(",", ": "), sort_keys=True) + "\n").encode()
    (tmp_path / "lambda-object-evidence.json").write_bytes(raw)
    documents["expected-manifest.json"]["lambda_release_object_evidence_fingerprint"] = sha256(
        raw
    ).hexdigest()
    (tmp_path / "expected-manifest.json").write_text(
        json.dumps(documents["expected-manifest.json"]), encoding="utf-8"
    )

    with pytest.raises(Phase6RuntimeUpdateError, match="evidence is invalid"):
        _verify(tmp_path)


def test_wrong_local_lambda_archive_fails_before_update_review(tmp_path: Path) -> None:
    documents = _documents()
    _write_documents(tmp_path, documents)
    (tmp_path / "phase6-artifacts/phase6-lambda.zip").write_bytes(
        b"wrong substituted lambda archive\n"
    )

    with pytest.raises(Phase6RuntimeUpdateError, match="evidence is invalid"):
        _verify(tmp_path)


def test_cli_emits_only_canonical_success_json(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    documents = _documents()
    _write_documents(tmp_path, documents)

    assert main(_cli_args(tmp_path)) == 0
    output = capsys.readouterr().out
    assert json.loads(output)["change_set_type"] == "UPDATE"
    assert output == json.dumps(json.loads(output), separators=(",", ":"), sort_keys=True) + "\n"


def test_absent_optional_describe_change_set_fields_normalize_to_null(
    tmp_path: Path,
) -> None:
    documents = _documents()
    _write_documents(tmp_path, documents)
    explicit_descriptor = _verify(tmp_path)

    for key in (
        "DeploymentMode",
        "ImportExistingResources",
        "OnStackFailure",
        "ParentChangeSetId",
        "RootChangeSetId",
        "StackDriftStatus",
        "StatusReason",
    ):
        documents["change-set.json"].pop(key)
    _write_documents(tmp_path, documents)

    assert _verify(tmp_path) == explicit_descriptor


@pytest.mark.parametrize(
    ("filename", "mutation"),
    [
        ("foundation-binding.json", lambda value: value.__setitem__("account_id", "999999999999")),
        ("foundation-binding.json", lambda value: value.__setitem__("stack_id", STACK_ID + "x")),
        (
            "expected-manifest.json",
            lambda value: value.__setitem__("execution_role_arn", FOUNDATION_ROLE),
        ),
        (
            "expected-manifest.json",
            lambda value: value.__setitem__("deployer_role_arn", EXECUTION_ROLE),
        ),
        (
            "expected-manifest.json",
            lambda value: value.__setitem__("foundation_binding_fingerprint", "0" * 64),
        ),
        (
            "pre-stack.json",
            lambda value: value["Stacks"][0].__setitem__("RoleARN", EXECUTION_ROLE),
        ),
        ("pre-stack.json", lambda value: value.__setitem__("NextToken", "more")),
        ("pre-stack-resources.json", lambda value: value.__setitem__("NextToken", "more")),
    ],
)
def test_foundation_old_role_and_new_authority_cannot_be_conflated(
    tmp_path: Path, filename: str, mutation: Any
) -> None:
    documents = _documents()
    mutation(documents[filename])
    _write_documents(tmp_path, documents)

    with pytest.raises(Phase6RuntimeUpdateError, match="evidence is invalid"):
        _verify(tmp_path)


@pytest.mark.parametrize(
    ("filename", "mutation"),
    [
        (
            "execution-role.json",
            lambda value: value["Role"].__setitem__("Arn", EXECUTION_ROLE + "-other"),
        ),
        (
            "execution-role.json",
            lambda value: value["Role"]["AssumeRolePolicyDocument"]["Statement"][0][
                "Principal"
            ].__setitem__("Service", "ec2.amazonaws.com"),
        ),
        (
            "execution-inline-policies.json",
            lambda value: value["PolicyNames"].append("unreviewed"),
        ),
        (
            "execution-attached-policies.json",
            lambda value: value["AttachedPolicies"].append(
                {"PolicyArn": "arn:aws:iam::aws:policy/AdministratorAccess", "PolicyName": "Admin"}
            ),
        ),
        (
            "execution-policy.json",
            lambda value: value["PolicyDocument"]["Statement"][0].__setitem__("Action", "*"),
        ),
        (
            "execution-policy.json",
            lambda value: value["PolicyDocument"]["Statement"][1]["Condition"][
                "StringEquals"
            ].__setitem__("s3:VersionId", "other-version"),
        ),
        (
            "deployer-role.json",
            lambda value: value["Role"].__setitem__("PermissionsBoundary", {"x": "y"}),
        ),
        (
            "deployer-role.json",
            lambda value: value["Role"]["AssumeRolePolicyDocument"]["Statement"][0][
                "Principal"
            ].__setitem__("AWS", f"arn:aws:iam::{ACCOUNT}:root"),
        ),
        (
            "deployer-inline-policies.json",
            lambda value: value["PolicyNames"].append("extra"),
        ),
        (
            "deployer-attached-policies.json",
            lambda value: value["AttachedPolicies"].append(
                {"PolicyArn": "arn:aws:iam::aws:policy/ReadOnlyAccess", "PolicyName": "ReadOnly"}
            ),
        ),
        (
            "deployer-policy.json",
            lambda value: value["PolicyDocument"]["Statement"][0]["Condition"][
                "StringEquals"
            ].__setitem__("cloudformation:RoleArn", FOUNDATION_ROLE),
        ),
        (
            "deployer-policy.json",
            lambda value: value["PolicyDocument"]["Statement"][0]["Condition"][
                "StringEquals"
            ].__setitem__("cloudformation:ChangeSetName", "other"),
        ),
        (
            "deployer-policy.json",
            lambda value: value["PolicyDocument"]["Statement"][0]["Condition"][
                "StringEquals"
            ].__setitem__("cloudformation:TemplateUrl", "https://example.test/template.json"),
        ),
        (
            "deployer-policy.json",
            lambda value: value["PolicyDocument"]["Statement"][1]["Condition"][
                "StringEquals"
            ].__setitem__("iam:PassedToService", "ec2.amazonaws.com"),
        ),
        (
            "deployer-policy.json",
            lambda value: value["PolicyDocument"]["Statement"][1].__setitem__("Resource", "*"),
        ),
    ],
)
def test_iam_readback_rejects_every_role_policy_or_prevention_substitution(
    tmp_path: Path, filename: str, mutation: Any
) -> None:
    documents = _documents()
    mutation(documents[filename])
    _write_documents(tmp_path, documents)

    with pytest.raises(Phase6RuntimeUpdateError, match="evidence is invalid"):
        _verify(tmp_path)


@pytest.mark.parametrize(
    "mutation",
    [
        lambda event: event["requestParameters"].__setitem__("roleARN", FOUNDATION_ROLE),
        lambda event: event["requestParameters"].__setitem__("changeSetName", "other"),
        lambda event: event["requestParameters"].__setitem__(
            "templateURL", "https://example.test/template.json"
        ),
        lambda event: event["requestParameters"].__setitem__("changeSetType", "IMPORT"),
        lambda event: event["requestParameters"].__setitem__("capabilities", []),
        lambda event: event["requestParameters"].__setitem__("parameters", []),
        lambda event: event["requestParameters"]["parameters"][0].__setitem__(
            "parameterValue", "prod"
        ),
        lambda event: event["requestParameters"]["tags"][0].__setitem__("value", "OTHER"),
        lambda event: event["responseElements"].__setitem__("id", "other"),
        lambda event: event["userIdentity"].__setitem__(
            "arn", f"arn:aws:iam::{ACCOUNT}:user/mr-lister-dev"
        ),
        lambda event: event["userIdentity"]["sessionContext"]["sessionIssuer"].__setitem__(
            "arn", EXECUTION_ROLE
        ),
        lambda event: event.__setitem__("errorCode", "AccessDenied"),
        lambda event: event.__setitem__("managementEvent", False),
        lambda event: event.__setitem__("syntheticRoleProof", EXECUTION_ROLE),
    ],
)
def test_cloudtrail_success_record_rejects_every_request_or_caller_substitution(
    tmp_path: Path, mutation: Any
) -> None:
    documents = _documents()
    _mutate_cloudtrail(documents, mutation)
    _write_documents(tmp_path, documents)

    with pytest.raises(Phase6RuntimeUpdateError, match="evidence is invalid"):
        _verify(tmp_path)


@pytest.mark.parametrize(
    ("filename", "mutation"),
    [
        ("cloudtrail.json", lambda value: value.__setitem__("NextToken", "more")),
        (
            "cloudtrail.json",
            lambda value: value["Events"].append(copy.deepcopy(value["Events"][0])),
        ),
        (
            "caller-identity.json",
            lambda value: value.__setitem__("Arn", f"arn:aws:iam::{ACCOUNT}:user/mr-lister-dev"),
        ),
        (
            "caller-identity.json",
            lambda value: value.__setitem__("UserId", "AIDAOTHER"),
        ),
        ("change-set.json", lambda value: value.__setitem__("RoleARN", EXECUTION_ROLE)),
        ("change-set.json", lambda value: value.__setitem__("ChangeSetType", "UPDATE")),
        ("change-set.json", lambda value: value.__setitem__("NextToken", "more")),
        ("change-set.json", lambda value: value.__setitem__("ExecutionStatus", "OBSOLETE")),
    ],
)
def test_authority_captures_are_closed_singleton_and_unsynthesized(
    tmp_path: Path, filename: str, mutation: Any
) -> None:
    documents = _documents()
    mutation(documents[filename])
    _write_documents(tmp_path, documents)

    with pytest.raises(Phase6RuntimeUpdateError, match="evidence is invalid"):
        _verify(tmp_path)


@pytest.mark.parametrize(
    ("filename", "mutation"),
    [
        (
            "original-template.json",
            lambda value: value["TemplateBody"]["Resources"].pop("OperationalStateTable"),
        ),
        (
            "processed-template.json",
            lambda value: value["TemplateBody"].__setitem__("Metadata", {"changed": True}),
        ),
        (
            "processed-template.json",
            lambda value: value.__setitem__("StagesAvailable", ["Original"]),
        ),
        (
            "target-template.json",
            lambda value: value["Resources"]["PrivateArtifactBucket"]["Properties"].__setitem__(
                "BucketName", "renamed"
            ),
        ),
        (
            "target-template.json",
            lambda value: value["Resources"]["RuntimeWorker"]["Properties"]["CodeUri"].__setitem__(
                "Version", "other-version"
            ),
        ),
        (
            "change-set.json",
            lambda value: value["Changes"][0]["ResourceChange"].pop("AfterContext"),
        ),
        (
            "change-set.json",
            lambda value: value["Changes"][0]["ResourceChange"].__setitem__(
                "AfterContext", json.dumps({"Properties": {"CorsConfiguration": "other"}})
            ),
        ),
        (
            "change-set.json",
            lambda value: value["Changes"][0]["ResourceChange"]["Details"][0]["Target"].__setitem__(
                "AfterValue", "other"
            ),
        ),
        (
            "change-set.json",
            lambda value: value["Changes"][0]["ResourceChange"].__setitem__("Replacement", "True"),
        ),
        (
            "expected-manifest.json",
            lambda value: value["changes"][0].__setitem__("after_context", {"other": True}),
        ),
    ],
)
def test_original_processed_and_property_value_review_is_exact(
    tmp_path: Path, filename: str, mutation: Any
) -> None:
    documents = _documents()
    mutation(documents[filename])
    _write_documents(tmp_path, documents)

    with pytest.raises(Phase6RuntimeUpdateError, match="evidence is invalid"):
        _verify(tmp_path)


def test_expired_review_window_is_rejected_even_when_iam_and_manifest_match(tmp_path: Path) -> None:
    documents = _documents()
    expired = _utc_z(datetime.now(UTC) - timedelta(seconds=1))
    documents["expected-manifest.json"]["policy_expires_at"] = expired
    documents["deployer-role.json"]["Role"]["Tags"] = [
        {"Key": "DeploymentClass", "Value": "RUNTIME_UPDATE_DEPLOYER"},
        {"Key": "Environment", "Value": ENVIRONMENT},
        {"Key": "ExpiresAt", "Value": expired},
        {"Key": "Project", "Value": "MrLister"},
    ]
    for statement in documents["deployer-policy.json"]["PolicyDocument"]["Statement"]:
        statement["Condition"]["DateLessThan"]["aws:CurrentTime"] = expired
    _write_documents(tmp_path, documents)

    with pytest.raises(Phase6RuntimeUpdateError, match="evidence is invalid"):
        _verify(tmp_path)


def test_review_window_longer_than_fifteen_minutes_is_rejected(tmp_path: Path) -> None:
    documents = _documents()
    wrapper = documents["cloudtrail.json"]["Events"][0]
    event = json.loads(wrapper["CloudTrailEvent"])
    event_time = datetime.fromisoformat(event["eventTime"].replace("Z", "+00:00"))
    too_late = _utc_z(event_time + timedelta(minutes=16))
    documents["expected-manifest.json"]["policy_expires_at"] = too_late
    for tag in documents["deployer-role.json"]["Role"]["Tags"]:
        if tag["Key"] == "ExpiresAt":
            tag["Value"] = too_late
    for statement in documents["deployer-policy.json"]["PolicyDocument"]["Statement"]:
        statement["Condition"]["DateLessThan"]["aws:CurrentTime"] = too_late
    _write_documents(tmp_path, documents)

    with pytest.raises(Phase6RuntimeUpdateError, match="evidence is invalid"):
        _verify(tmp_path)


def test_symlinked_evidence_is_rejected(tmp_path: Path) -> None:
    documents = _documents()
    _write_documents(tmp_path, documents)
    target = tmp_path / "foundation-binding-target.json"
    target.write_text((tmp_path / "foundation-binding.json").read_text(encoding="utf-8"))
    (tmp_path / "foundation-binding.json").unlink()
    (tmp_path / "foundation-binding.json").symlink_to(target)

    with pytest.raises(Phase6RuntimeUpdateError, match="evidence is invalid"):
        _verify(tmp_path)
