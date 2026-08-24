from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from tools.verify_phase6_foundation_deployment import (
    DEFAULT_TEMPLATE,
    FOUNDATION_EVIDENCE_FORMAT,
    FOUNDATION_TEMPLATE_FINGERPRINT,
    Phase6FoundationBinding,
    Phase6FoundationDeploymentError,
    main,
    verify_create_change_set_observations,
    verify_deployed_foundation,
    verify_foundation_template,
    verify_stack_absence_observation,
)

ACCOUNT = "123456789012"
REGION = "us-west-2"
ENVIRONMENT = "dev"
STACK = "mr-lister-phase6-dev"
ROLE = f"arn:aws:iam::{ACCOUNT}:role/mr-lister-phase6-foundation-cfn-dev"
DEPLOYER = f"arn:aws:iam::{ACCOUNT}:user/mr-lister-dev"
STACK_ID = (
    f"arn:aws:cloudformation:{REGION}:{ACCOUNT}:stack/{STACK}/11111111-2222-3333-4444-555555555555"
)
CHANGE_SET = f"{STACK}-foundation-create-{FOUNDATION_TEMPLATE_FINGERPRINT[:12]}"
CHANGE_SET_ID = (
    f"arn:aws:cloudformation:{REGION}:{ACCOUNT}:changeSet/{CHANGE_SET}/"
    "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
)


def _binding(**overrides: str) -> Phase6FoundationBinding:
    values = {
        "account_id": ACCOUNT,
        "region": REGION,
        "environment_name": ENVIRONMENT,
        "stack_name": STACK,
        "execution_role_arn": ROLE,
        "deployer_arn": DEPLOYER,
    }
    values.update(overrides)
    return Phase6FoundationBinding(**values)


def _tags(classification: str | None = None) -> list[dict[str, str]]:
    values = {
        "DeploymentClass": "FOUNDATION_ONLY",
        "Environment": ENVIRONMENT,
        "Project": "MrLister",
    }
    if classification is not None:
        values["DataClassification"] = classification
    return [{"Key": key, "Value": value} for key, value in values.items()]


def _resource_tags(classification: str, logical_id: str) -> list[dict[str, str]]:
    tags = _tags(classification)
    tags.extend(
        [
            {"Key": "aws:cloudformation:logical-id", "Value": logical_id},
            {"Key": "aws:cloudformation:stack-id", "Value": STACK_ID},
            {"Key": "aws:cloudformation:stack-name", "Value": STACK},
        ]
    )
    return tags


def _absence() -> dict[str, object]:
    return {
        "error_code": "ValidationError",
        "format": "mr-lister-cloudformation-stack-absence-v1",
        "http_status_code": 400,
        "operation": "DescribeStacks",
        "stack_name": STACK,
    }


def _change_set() -> dict[str, object]:
    resources = {
        "OperationalStateTable": "AWS::DynamoDB::Table",
        "PrivateArtifactBucket": "AWS::S3::Bucket",
        "PrivateArtifactBucketPolicy": "AWS::S3::BucketPolicy",
    }
    return {
        "Capabilities": [],
        "ChangeSetId": CHANGE_SET_ID,
        "ChangeSetName": CHANGE_SET,
        "ChangeSetType": "CREATE",
        "Changes": [
            {
                "ResourceChange": {
                    "Action": "Add",
                    "Details": [],
                    "LogicalResourceId": logical_id,
                    "ResourceType": resource_type,
                    "Scope": [],
                },
                "Type": "Resource",
            }
            for logical_id, resource_type in resources.items()
        ],
        "Description": (
            "Mr Lister Phase 6 create-only foundation " + FOUNDATION_TEMPLATE_FINGERPRINT
        ),
        "ExecutionStatus": "AVAILABLE",
        "IncludeNestedStacks": False,
        "NotificationARNs": [],
        "OnStackFailure": "DO_NOTHING",
        "Parameters": [{"ParameterKey": "EnvironmentName", "ParameterValue": ENVIRONMENT}],
        "RoleARN": ROLE,
        "RollbackConfiguration": {"RollbackTriggers": []},
        "StackId": STACK_ID,
        "StackName": STACK,
        "Status": "CREATE_COMPLETE",
        "Tags": _tags(),
    }


def _template_observation() -> dict[str, object]:
    return {
        "Stages": ["Original", "Processed"],
        "TemplateBody": json.loads(DEFAULT_TEMPLATE.read_text(encoding="utf-8")),
    }


def _stack() -> dict[str, object]:
    return {
        "Stacks": [
            {
                "CreationTime": "2026-08-24T18:00:00.000000+00:00",
                "EnableTerminationProtection": True,
                "Outputs": [
                    {
                        "Description": "Create-only durable foundation",
                        "OutputKey": "DeploymentReadiness",
                        "OutputValue": "FOUNDATION_ONLY",
                    },
                    {"OutputKey": "StateTableName", "OutputValue": STACK},
                    {
                        "OutputKey": "StateTableArn",
                        "OutputValue": f"arn:aws:dynamodb:{REGION}:{ACCOUNT}:table/{STACK}",
                    },
                    {
                        "OutputKey": "ArtifactBucketName",
                        "OutputValue": (
                            f"mr-lister-phase6-artifacts-{ENVIRONMENT}-{ACCOUNT}-{REGION}"
                        ),
                    },
                    {
                        "OutputKey": "ArtifactBucketArn",
                        "OutputValue": (
                            f"arn:aws:s3:::mr-lister-phase6-artifacts-"
                            f"{ENVIRONMENT}-{ACCOUNT}-{REGION}"
                        ),
                    },
                ],
                "Parameters": [{"ParameterKey": "EnvironmentName", "ParameterValue": ENVIRONMENT}],
                "RoleARN": ROLE,
                "StackId": STACK_ID,
                "StackName": STACK,
                "StackStatus": "CREATE_COMPLETE",
                "Tags": _tags(),
            }
        ]
    }


def _stack_resources() -> dict[str, object]:
    bucket = f"mr-lister-phase6-artifacts-{ENVIRONMENT}-{ACCOUNT}-{REGION}"
    return {
        "StackResourceSummaries": [
            {
                "LogicalResourceId": "OperationalStateTable",
                "PhysicalResourceId": STACK,
                "ResourceStatus": "CREATE_COMPLETE",
                "ResourceType": "AWS::DynamoDB::Table",
            },
            {
                "LogicalResourceId": "PrivateArtifactBucket",
                "PhysicalResourceId": bucket,
                "ResourceStatus": "CREATE_COMPLETE",
                "ResourceType": "AWS::S3::Bucket",
            },
            {
                "LogicalResourceId": "PrivateArtifactBucketPolicy",
                "PhysicalResourceId": bucket,
                "ResourceStatus": "CREATE_COMPLETE",
                "ResourceType": "AWS::S3::BucketPolicy",
            },
        ]
    }


def _attributes() -> list[dict[str, str]]:
    return [
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
    ]


def _index(name: str, partition_key: str, sort_key: str, projection: str) -> dict[str, object]:
    return {
        "IndexArn": f"arn:aws:dynamodb:{REGION}:{ACCOUNT}:table/{STACK}/index/{name}",
        "IndexName": name,
        "IndexSizeBytes": 0,
        "IndexStatus": "ACTIVE",
        "ItemCount": 0,
        "KeySchema": [
            {"AttributeName": partition_key, "KeyType": "HASH"},
            {"AttributeName": sort_key, "KeyType": "RANGE"},
        ],
        "Projection": {"ProjectionType": projection},
    }


def _table() -> dict[str, object]:
    table_arn = f"arn:aws:dynamodb:{REGION}:{ACCOUNT}:table/{STACK}"
    return {
        "Table": {
            "AttributeDefinitions": _attributes(),
            "BillingModeSummary": {"BillingMode": "PAY_PER_REQUEST"},
            "GlobalSecondaryIndexes": [
                _index("DueWorkIndex", "dispatch_pk", "dispatch_sk", "ALL"),
                _index("OwnerJobsIndex", "owner_jobs_pk", "owner_jobs_sk", "ALL"),
                _index(
                    "ExecutionRecoveryIndex",
                    "recovery_pk",
                    "recovery_sk",
                    "KEYS_ONLY",
                ),
            ],
            "KeySchema": [
                {"AttributeName": "PK", "KeyType": "HASH"},
                {"AttributeName": "SK", "KeyType": "RANGE"},
            ],
            "LatestStreamArn": f"{table_arn}/stream/2026-08-24T18:00:00.000",
            "SSEDescription": {
                "KMSMasterKeyArn": f"arn:aws:kms:{REGION}:{ACCOUNT}:key/example",
                "SSEType": "KMS",
                "Status": "ENABLED",
            },
            "StreamSpecification": {"StreamEnabled": True, "StreamViewType": "KEYS_ONLY"},
            "TableArn": table_arn,
            "TableName": STACK,
            "TableStatus": "ACTIVE",
        }
    }


def _deployed_policy() -> dict[str, object]:
    bucket_arn = f"arn:aws:s3:::mr-lister-phase6-artifacts-{ENVIRONMENT}-{ACCOUNT}-{REGION}"
    source = f"{bucket_arn}/private/owners/*/jobs/*/source/source.png"
    return {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "DenyInsecureTransport",
                "Effect": "Deny",
                "Principal": "*",
                "Action": "s3:*",
                "Resource": [bucket_arn, f"{bucket_arn}/*"],
                "Condition": {"Bool": {"aws:SecureTransport": "false"}},
            },
            {
                "Sid": "DenyStaleBrowserUploadSignatures",
                "Effect": "Deny",
                "Principal": "*",
                "Action": "s3:PutObject",
                "Resource": source,
                "Condition": {"NumericGreaterThan": {"s3:signatureAge": "300000"}},
            },
            {
                "Sid": "DenyUnencryptedBrowserUploads",
                "Effect": "Deny",
                "Principal": "*",
                "Action": "s3:PutObject",
                "Resource": source,
                "Condition": {"StringNotEquals": {"s3:x-amz-server-side-encryption": "AES256"}},
            },
        ],
    }


def _captures() -> dict[str, dict[str, object]]:
    bucket = f"mr-lister-phase6-artifacts-{ENVIRONMENT}-{ACCOUNT}-{REGION}"
    return {
        "caller-identity.json": {"Account": ACCOUNT, "Arn": DEPLOYER, "UserId": "DEVUSERID"},
        "stack-absence.json": _absence(),
        "change-set.json": _change_set(),
        "change-set-template.json": _template_observation(),
        "stack.json": _stack(),
        "stack-resources.json": _stack_resources(),
        "table.json": _table(),
        "table-ttl.json": {
            "TimeToLiveDescription": {
                "AttributeName": "expires_at",
                "TimeToLiveStatus": "ENABLED",
            }
        },
        "table-backups.json": {
            "ContinuousBackupsDescription": {
                "ContinuousBackupsStatus": "ENABLED",
                "PointInTimeRecoveryDescription": {
                    "EarliestRestorableDateTime": "2026-08-24T18:00:00+00:00",
                    "LatestRestorableDateTime": "2026-08-24T19:00:00+00:00",
                    "PointInTimeRecoveryStatus": "ENABLED",
                },
            }
        },
        "table-tags.json": {"Tags": _resource_tags("OperationalState", "OperationalStateTable")},
        "bucket-encryption.json": {
            "ServerSideEncryptionConfiguration": [
                {
                    "ApplyServerSideEncryptionByDefault": {"SSEAlgorithm": "AES256"},
                }
            ]
        },
        "bucket-versioning.json": {"Status": "Enabled"},
        "bucket-public-access-block.json": {
            "PublicAccessBlockConfiguration": {
                "BlockPublicAcls": True,
                "BlockPublicPolicy": True,
                "IgnorePublicAcls": True,
                "RestrictPublicBuckets": True,
            }
        },
        "bucket-ownership-controls.json": {
            "OwnershipControls": {"Rules": [{"ObjectOwnership": "BucketOwnerEnforced"}]}
        },
        "bucket-lifecycle.json": {
            "Rules": [
                {
                    "AbortIncompleteMultipartUpload": {"DaysAfterInitiation": 7},
                    "Filter": {"Prefix": ""},
                    "ID": "AbortIncompleteMultipartUploads",
                    "Status": "Enabled",
                },
                {
                    "Expiration": {"Days": 1},
                    "Filter": {"Tag": {"Key": "mr-lister-state", "Value": "staged"}},
                    "ID": "ExpireUnreferencedStagedArtwork",
                    "NoncurrentVersionExpiration": {"NoncurrentDays": 1},
                    "Status": "Enabled",
                },
                {
                    "Expiration": {"ExpiredObjectDeleteMarker": True},
                    "Filter": {"Prefix": "private/owners/"},
                    "ID": "RemoveExpiredPrivateSourceDeleteMarkers",
                    "Status": "Enabled",
                },
            ]
        },
        "bucket-tags.json": {"TagSet": _resource_tags("PrivateArtwork", "PrivateArtifactBucket")},
        "bucket-policy.json": {
            "Policy": json.dumps(_deployed_policy(), separators=(",", ":"), sort_keys=True)
        },
        "bucket-policy-status.json": {"PolicyStatus": {"IsPublic": False}},
        "bucket-cors-absence.json": {
            "bucket_name": bucket,
            "error_code": "NoSuchCORSConfiguration",
            "format": "mr-lister-s3-cors-absence-v1",
            "http_status_code": 404,
            "operation": "GetBucketCors",
        },
    }


def _write_captures(root: Path, captures: dict[str, dict[str, object]] | None = None) -> None:
    root.mkdir()
    for filename, document in (captures or _captures()).items():
        (root / filename).write_text(json.dumps(document), encoding="utf-8")


def test_frozen_template_and_binding_are_exact() -> None:
    assert verify_foundation_template() == FOUNDATION_TEMPLATE_FINGERPRINT
    binding = _binding()
    assert binding.change_set_name == CHANGE_SET
    assert binding.table_name == STACK
    assert binding.bucket_name == (f"mr-lister-phase6-artifacts-{ENVIRONMENT}-{ACCOUNT}-{REGION}")

    for overrides in (
        {"region": "us-east-1"},
        {"stack_name": "another-stack"},
        {"execution_role_arn": f"arn:aws:iam::{ACCOUNT}:role/Admin"},
        {"deployer_arn": f"arn:aws:iam::{ACCOUNT}:root"},
    ):
        with pytest.raises(Phase6FoundationDeploymentError, match="evidence is invalid"):
            _binding(**overrides)


def test_template_rejects_any_semantic_drift(tmp_path: Path) -> None:
    template = json.loads(DEFAULT_TEMPLATE.read_text(encoding="utf-8"))
    template["Resources"]["PrivateArtifactBucket"]["Properties"]["VersioningConfiguration"] = {
        "Status": "Suspended"
    }
    path = tmp_path / "foundation.json"
    path.write_text(json.dumps(template), encoding="utf-8")
    with pytest.raises(Phase6FoundationDeploymentError, match="evidence is invalid"):
        verify_foundation_template(path)


def test_absence_gate_refuses_an_existing_stack_or_a_different_error() -> None:
    verify_stack_absence_observation(_absence(), _binding())
    for observation in (
        _stack(),
        _absence() | {"error_code": "AccessDenied"},
        _absence() | {"stack_name": "another-stack"},
    ):
        with pytest.raises(Phase6FoundationDeploymentError, match="evidence is invalid"):
            verify_stack_absence_observation(observation, _binding())


def test_create_change_set_is_exactly_three_adds_and_original_template() -> None:
    verify_create_change_set_observations(
        _change_set(), _template_observation(), _absence(), _binding()
    )

    mutations = []
    update = _change_set()
    update["ChangeSetType"] = "UPDATE"
    mutations.append(update)
    replacement = _change_set()
    replacement["Changes"][0]["ResourceChange"]["Replacement"] = "True"  # type: ignore[index]
    mutations.append(replacement)
    extra = _change_set()
    extra["Changes"].append(  # type: ignore[union-attr]
        {
            "Type": "Resource",
            "ResourceChange": {
                "Action": "Add",
                "Details": [],
                "LogicalResourceId": "UnexpectedRole",
                "ResourceType": "AWS::IAM::Role",
                "Scope": [],
            },
        }
    )
    mutations.append(extra)
    wrong_role = _change_set()
    wrong_role["RoleARN"] = f"arn:aws:iam::{ACCOUNT}:role/Admin"
    mutations.append(wrong_role)

    for observation in mutations:
        with pytest.raises(Phase6FoundationDeploymentError, match="evidence is invalid"):
            verify_create_change_set_observations(
                observation, _template_observation(), _absence(), _binding()
            )


def test_deployed_gate_returns_binding_for_later_agentcore_and_sam_verifiers(
    tmp_path: Path,
) -> None:
    evidence = tmp_path / "evidence"
    _write_captures(evidence)
    result = verify_deployed_foundation(evidence, _binding())
    assert result == {
        "account_id": ACCOUNT,
        "artifact_bucket_arn": (
            f"arn:aws:s3:::mr-lister-phase6-artifacts-{ENVIRONMENT}-{ACCOUNT}-{REGION}"
        ),
        "artifact_bucket_name": (f"mr-lister-phase6-artifacts-{ENVIRONMENT}-{ACCOUNT}-{REGION}"),
        "environment_name": ENVIRONMENT,
        "format": FOUNDATION_EVIDENCE_FORMAT,
        "foundation_template_fingerprint": FOUNDATION_TEMPLATE_FINGERPRINT,
        "operational_state_stream_arn": (
            f"arn:aws:dynamodb:{REGION}:{ACCOUNT}:table/{STACK}/stream/2026-08-24T18:00:00.000"
        ),
        "operational_state_table_arn": (f"arn:aws:dynamodb:{REGION}:{ACCOUNT}:table/{STACK}"),
        "operational_state_table_name": STACK,
        "region": REGION,
        "stack_id": STACK_ID,
        "stack_name": STACK,
    }


@pytest.mark.parametrize(
    ("filename", "mutate"),
    [
        (
            "stack.json",
            lambda value: value["Stacks"][0].__setitem__("StackStatus", "UPDATE_COMPLETE"),
        ),
        (
            "stack.json",
            lambda value: value["Stacks"][0].__setitem__(
                "LastUpdatedTime", "2026-08-24T19:00:00+00:00"
            ),
        ),
        (
            "stack-resources.json",
            lambda value: value["StackResourceSummaries"].append(
                {
                    "LogicalResourceId": "UnexpectedFunction",
                    "PhysicalResourceId": "unexpected",
                    "ResourceStatus": "CREATE_COMPLETE",
                    "ResourceType": "AWS::Lambda::Function",
                }
            ),
        ),
        (
            "table.json",
            lambda value: value["Table"]["SSEDescription"].__setitem__("Status", "DISABLED"),
        ),
        (
            "table-ttl.json",
            lambda value: value["TimeToLiveDescription"].__setitem__(
                "TimeToLiveStatus", "DISABLED"
            ),
        ),
        (
            "table-backups.json",
            lambda value: value["ContinuousBackupsDescription"][
                "PointInTimeRecoveryDescription"
            ].__setitem__("PointInTimeRecoveryStatus", "DISABLED"),
        ),
        (
            "bucket-versioning.json",
            lambda value: value.__setitem__("Status", "Suspended"),
        ),
        (
            "bucket-public-access-block.json",
            lambda value: value["PublicAccessBlockConfiguration"].__setitem__(
                "BlockPublicPolicy", False
            ),
        ),
        (
            "bucket-policy-status.json",
            lambda value: value["PolicyStatus"].__setitem__("IsPublic", True),
        ),
        (
            "bucket-cors-absence.json",
            lambda value: value.__setitem__("error_code", "AccessDenied"),
        ),
    ],
)
def test_deployed_gate_fails_closed_on_update_or_security_drift(
    tmp_path: Path, filename: str, mutate: object
) -> None:
    captures = _captures()
    altered = copy.deepcopy(captures[filename])
    mutate(altered)  # type: ignore[operator]
    captures[filename] = altered
    evidence = tmp_path / "evidence"
    _write_captures(evidence, captures)
    with pytest.raises(Phase6FoundationDeploymentError, match="evidence is invalid"):
        verify_deployed_foundation(evidence, _binding())


def test_cli_template_gate_emits_canonical_descriptor(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["template", "--template", str(DEFAULT_TEMPLATE)]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output == {
        "format": FOUNDATION_EVIDENCE_FORMAT,
        "foundation_template_fingerprint": FOUNDATION_TEMPLATE_FINGERPRINT,
    }


def test_verifier_has_no_aws_sdk_or_subprocess_surface() -> None:
    source = (Path(__file__).parents[1] / "tools/verify_phase6_foundation_deployment.py").read_text(
        encoding="utf-8"
    )
    for forbidden in ("import boto", "import subprocess", "os.system", "Popen(", "aws "):
        assert forbidden not in source
