"""Verify the create-only Phase 6 durable foundation and AWS CLI captures offline.

The verifier deliberately has no AWS SDK import and never starts a subprocess.  It accepts only
reviewed local JSON plus normalized CLI observations.  A foundation change set is valid only when
the named stack was observed absent first, its pending stack is an empty ``REVIEW_IN_PROGRESS``
placeholder bound to the exact execution role, and its complete surface is the three retained
resources frozen in ``infra/phase6/foundation.json``.
"""

from __future__ import annotations

import argparse
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from hashlib import sha256
from pathlib import Path
from typing import cast

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_TEMPLATE = ROOT / "infra/phase6/foundation.json"

FOUNDATION_TEMPLATE_FINGERPRINT = "689897c254c9db97aa75d508f140980f9b6a5129c0c1fa0121eb8d6ef1e64874"
FOUNDATION_RESOURCE_TYPES = {
    "OperationalStateTable": "AWS::DynamoDB::Table",
    "PrivateArtifactBucket": "AWS::S3::Bucket",
    "PrivateArtifactBucketPolicy": "AWS::S3::BucketPolicy",
}
FOUNDATION_EVIDENCE_FORMAT = "mr-lister-phase6-foundation-deployment-v1"

_ACCOUNT_ID = re.compile(r"^[0-9]{12}$")
_ENVIRONMENT = re.compile(r"^[a-z][a-z0-9-]{1,15}$")
_HEX_12 = re.compile(r"^[a-f0-9]{12}$")
_ARN_ID = re.compile(r"^[0-9a-f-]{32,36}$", re.IGNORECASE)
_GENERIC_ERROR = "Phase 6 foundation deployment evidence is invalid"

_DEPLOYED_EVIDENCE_FILES = {
    "caller_identity": "caller-identity.json",
    "absence": "stack-absence.json",
    "change_set": "change-set.json",
    "change_set_template": "change-set-template.json",
    "pending_stack": "pending-stack.json",
    "pending_stack_resources": "pending-stack-resources.json",
    "stack": "stack.json",
    "stack_resources": "stack-resources.json",
    "table": "table.json",
    "table_ttl": "table-ttl.json",
    "table_backups": "table-backups.json",
    "table_tags": "table-tags.json",
    "bucket_encryption": "bucket-encryption.json",
    "bucket_versioning": "bucket-versioning.json",
    "bucket_public_access": "bucket-public-access-block.json",
    "bucket_ownership": "bucket-ownership-controls.json",
    "bucket_lifecycle": "bucket-lifecycle.json",
    "bucket_tags": "bucket-tags.json",
    "bucket_policy": "bucket-policy.json",
    "bucket_policy_status": "bucket-policy-status.json",
    "bucket_cors_absence": "bucket-cors-absence.json",
}


class Phase6FoundationDeploymentError(RuntimeError):
    """A value-free failure for malformed, drifting, or unsafe deployment evidence."""


@dataclass(frozen=True, slots=True)
class Phase6FoundationBinding:
    """Exact account, Region, environment, stack, role, and deployer deployment binding."""

    account_id: str
    region: str
    environment_name: str
    stack_name: str
    execution_role_arn: str
    deployer_arn: str

    def __post_init__(self) -> None:
        try:
            if (
                not isinstance(self.account_id, str)
                or _ACCOUNT_ID.fullmatch(self.account_id) is None
                or self.account_id == "0" * 12
                or self.region != "us-west-2"
                or not isinstance(self.environment_name, str)
                or _ENVIRONMENT.fullmatch(self.environment_name) is None
                or self.stack_name != f"mr-lister-phase6-{self.environment_name}"
                or self.execution_role_arn != self.expected_execution_role_arn
                or not isinstance(self.deployer_arn, str)
                or not self.deployer_arn.startswith(f"arn:aws:iam::{self.account_id}:")
                or self.deployer_arn.endswith(":root")
                or ":user/" not in self.deployer_arn
            ):
                raise ValueError
        except Exception:
            raise Phase6FoundationDeploymentError(_GENERIC_ERROR) from None

    @property
    def expected_execution_role_arn(self) -> str:
        return (
            f"arn:aws:iam::{self.account_id}:role/"
            f"mr-lister-phase6-foundation-cfn-{self.environment_name}"
        )

    @property
    def table_name(self) -> str:
        return f"mr-lister-phase6-{self.environment_name}"

    @property
    def table_arn(self) -> str:
        return f"arn:aws:dynamodb:{self.region}:{self.account_id}:table/{self.table_name}"

    @property
    def bucket_name(self) -> str:
        return f"mr-lister-phase6-artifacts-{self.environment_name}-{self.account_id}-{self.region}"

    @property
    def bucket_arn(self) -> str:
        return f"arn:aws:s3:::{self.bucket_name}"

    @property
    def change_set_name(self) -> str:
        return f"{self.stack_name}-foundation-create-{FOUNDATION_TEMPLATE_FINGERPRINT[:12]}"


def verify_foundation_template(template_path: Path = DEFAULT_TEMPLATE) -> str:
    """Verify the frozen three-resource template and return its semantic fingerprint."""

    try:
        document = _load_json_file(template_path)
        if not isinstance(document, Mapping):
            raise ValueError
        fingerprint = _fingerprint(document)
        if fingerprint != FOUNDATION_TEMPLATE_FINGERPRINT:
            raise ValueError
        if set(document) != {
            "AWSTemplateFormatVersion",
            "Transform",
            "Description",
            "Metadata",
            "Parameters",
            "Resources",
            "Outputs",
        }:
            raise ValueError
        if (
            document.get("AWSTemplateFormatVersion") != "2010-09-09"
            or document.get("Transform") != "AWS::Serverless-2016-10-31"
            or document.get("Description") != "Mr Lister Phase 6 create-only durable foundation"
            or document.get("Metadata")
            != {
                "MrListerDeployment": {
                    "DeploymentClass": "FOUNDATION_ONLY",
                    "CreateOnly": True,
                    "UpgradeTemplate": "infra/phase6/template.json",
                }
            }
            or document.get("Parameters")
            != {
                "EnvironmentName": {
                    "Type": "String",
                    "Default": "dev",
                    "AllowedPattern": "^[a-z][a-z0-9-]{1,15}$",
                }
            }
        ):
            raise ValueError
        resources = document.get("Resources")
        if (
            not isinstance(resources, Mapping)
            or {
                key: cast(Mapping[str, object], value).get("Type")
                for key, value in resources.items()
                if isinstance(value, Mapping)
            }
            != FOUNDATION_RESOURCE_TYPES
        ):
            raise ValueError
        if any(not isinstance(value, Mapping) for value in resources.values()):
            raise ValueError
        _verify_template_table(cast(Mapping[str, object], resources["OperationalStateTable"]))
        _verify_template_bucket(cast(Mapping[str, object], resources["PrivateArtifactBucket"]))
        _verify_template_bucket_policy(
            cast(Mapping[str, object], resources["PrivateArtifactBucketPolicy"])
        )
        _verify_template_outputs(cast(Mapping[str, object], document.get("Outputs")))
        return fingerprint
    except Phase6FoundationDeploymentError:
        raise
    except Exception:
        raise Phase6FoundationDeploymentError(_GENERIC_ERROR) from None


def verify_caller_identity(
    observation: Mapping[str, object], binding: Phase6FoundationBinding
) -> None:
    """Verify a non-root, exact deployer ``sts get-caller-identity`` capture."""

    try:
        if (
            set(observation) != {"Account", "Arn", "UserId"}
            or observation.get("Account") != binding.account_id
            or observation.get("Arn") != binding.deployer_arn
            or not isinstance(observation.get("UserId"), str)
            or not cast(str, observation["UserId"]).strip()
        ):
            raise ValueError
    except Exception:
        raise Phase6FoundationDeploymentError(_GENERIC_ERROR) from None


def verify_stack_absence_observation(
    observation: Mapping[str, object], binding: Phase6FoundationBinding
) -> None:
    """Require the normalized failure from describing the not-yet-created exact stack."""

    expected = {
        "error_code": "ValidationError",
        "format": "mr-lister-cloudformation-stack-absence-v1",
        "http_status_code": 400,
        "operation": "DescribeStacks",
        "stack_name": binding.stack_name,
    }
    if observation != expected:
        raise Phase6FoundationDeploymentError(_GENERIC_ERROR)


def verify_create_change_set_observations(
    change_set_observation: Mapping[str, object],
    template_observation: Mapping[str, object],
    absence_observation: Mapping[str, object],
    binding: Phase6FoundationBinding,
    *,
    pending_stack_observation: Mapping[str, object],
    pending_stack_resources_observation: Mapping[str, object],
    template_path: Path = DEFAULT_TEMPLATE,
) -> None:
    """Verify an exact, executable create change set and its authoritative pending stack."""

    try:
        verify_foundation_template(template_path)
        verify_stack_absence_observation(absence_observation, binding)
        _verify_change_set_template(template_observation)
        _verify_change_set(change_set_observation, binding)
        _verify_pending_stack_observations(
            pending_stack_observation,
            pending_stack_resources_observation,
            change_set_observation,
            binding,
        )
    except Phase6FoundationDeploymentError:
        raise
    except Exception:
        raise Phase6FoundationDeploymentError(_GENERIC_ERROR) from None


def verify_deployed_foundation(
    evidence_directory: Path,
    binding: Phase6FoundationBinding,
    *,
    template_path: Path = DEFAULT_TEMPLATE,
) -> dict[str, object]:
    """Verify the complete post-create capture set and return its downstream binding manifest."""

    try:
        verify_foundation_template(template_path)
        captures = _load_evidence_directory(evidence_directory)
        verify_caller_identity(captures["caller_identity"], binding)
        verify_create_change_set_observations(
            captures["change_set"],
            captures["change_set_template"],
            captures["absence"],
            binding,
            pending_stack_observation=captures["pending_stack"],
            pending_stack_resources_observation=captures["pending_stack_resources"],
            template_path=template_path,
        )
        stack_id = _verify_stack_observation(
            captures["stack"],
            binding,
            expected_change_set_id=cast(str, captures["change_set"]["ChangeSetId"]),
            expected_stack_id=cast(str, captures["change_set"]["StackId"]),
            expected_creation_time=cast(str, captures["change_set"]["CreationTime"]),
        )
        _verify_stack_resources(captures["stack_resources"], binding)
        stream_arn = _verify_table_observation(captures["table"], binding)
        _verify_table_ttl(captures["table_ttl"], binding)
        _verify_table_backups(captures["table_backups"])
        _verify_resource_tags(
            captures["table_tags"],
            expected=_resource_tags(binding.environment_name, "OperationalState"),
            binding=binding,
            logical_id="OperationalStateTable",
            stack_id=stack_id,
        )
        _verify_bucket_observations(captures, binding, stack_id=stack_id)
        return {
            "account_id": binding.account_id,
            "artifact_bucket_arn": binding.bucket_arn,
            "artifact_bucket_name": binding.bucket_name,
            "environment_name": binding.environment_name,
            "format": FOUNDATION_EVIDENCE_FORMAT,
            "foundation_template_fingerprint": FOUNDATION_TEMPLATE_FINGERPRINT,
            "operational_state_stream_arn": stream_arn,
            "operational_state_table_arn": binding.table_arn,
            "operational_state_table_name": binding.table_name,
            "region": binding.region,
            "stack_id": stack_id,
            "stack_name": binding.stack_name,
        }
    except Phase6FoundationDeploymentError:
        raise
    except Exception:
        raise Phase6FoundationDeploymentError(_GENERIC_ERROR) from None


def _verify_template_table(resource: Mapping[str, object]) -> None:
    properties = cast(Mapping[str, object], resource.get("Properties"))
    if (
        resource.get("Type") != "AWS::DynamoDB::Table"
        or resource.get("DeletionPolicy") != "Retain"
        or resource.get("UpdateReplacePolicy") != "Retain"
        or properties.get("TableName") != {"Fn::Sub": "mr-lister-phase6-${EnvironmentName}"}
        or properties.get("BillingMode") != "PAY_PER_REQUEST"
        or properties.get("KeySchema")
        != [
            {"AttributeName": "PK", "KeyType": "HASH"},
            {"AttributeName": "SK", "KeyType": "RANGE"},
        ]
        or properties.get("PointInTimeRecoverySpecification")
        != {"PointInTimeRecoveryEnabled": True}
        or properties.get("SSESpecification") != {"SSEEnabled": True}
        or properties.get("StreamSpecification") != {"StreamViewType": "KEYS_ONLY"}
        or properties.get("TimeToLiveSpecification")
        != {"AttributeName": "expires_at", "Enabled": True}
        or _template_tags(properties.get("Tags"))
        != {
            "DataClassification": "OperationalState",
            "Environment": {"Ref": "EnvironmentName"},
            "Project": "MrLister",
        }
    ):
        raise ValueError
    attributes = properties.get("AttributeDefinitions")
    if attributes != [
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
    ]:
        raise ValueError
    expected_indexes = {
        "DueWorkIndex": ("dispatch_pk", "dispatch_sk", "ALL"),
        "OwnerJobsIndex": ("owner_jobs_pk", "owner_jobs_sk", "ALL"),
        "ExecutionRecoveryIndex": ("recovery_pk", "recovery_sk", "KEYS_ONLY"),
    }
    if _index_shapes(properties.get("GlobalSecondaryIndexes")) != expected_indexes:
        raise ValueError


def _verify_template_bucket(resource: Mapping[str, object]) -> None:
    properties = cast(Mapping[str, object], resource.get("Properties"))
    if (
        resource.get("Type") != "AWS::S3::Bucket"
        or resource.get("DeletionPolicy") != "Retain"
        or resource.get("UpdateReplacePolicy") != "Retain"
        or properties.get("BucketName")
        != {
            "Fn::Sub": (
                "mr-lister-phase6-artifacts-${EnvironmentName}-${AWS::AccountId}-${AWS::Region}"
            )
        }
        or properties.get("BucketEncryption")
        != {
            "ServerSideEncryptionConfiguration": [
                {"ServerSideEncryptionByDefault": {"SSEAlgorithm": "AES256"}}
            ]
        }
        or properties.get("OwnershipControls")
        != {"Rules": [{"ObjectOwnership": "BucketOwnerEnforced"}]}
        or properties.get("PublicAccessBlockConfiguration")
        != {
            "BlockPublicAcls": True,
            "BlockPublicPolicy": True,
            "IgnorePublicAcls": True,
            "RestrictPublicBuckets": True,
        }
        or properties.get("VersioningConfiguration") != {"Status": "Enabled"}
        or "CorsConfiguration" in properties
        or _template_tags(properties.get("Tags"))
        != {
            "DataClassification": "PrivateArtwork",
            "Environment": {"Ref": "EnvironmentName"},
            "Project": "MrLister",
        }
        or properties.get("LifecycleConfiguration") != _template_bucket_lifecycle()
    ):
        raise ValueError


def _verify_template_bucket_policy(resource: Mapping[str, object]) -> None:
    if (
        set(resource) != {"Type", "Properties"}
        or resource.get("Type") != "AWS::S3::BucketPolicy"
        or resource.get("Properties")
        != {
            "Bucket": {"Ref": "PrivateArtifactBucket"},
            "PolicyDocument": _template_bucket_policy(),
        }
    ):
        raise ValueError


def _verify_template_outputs(outputs: Mapping[str, object]) -> None:
    if set(outputs) != {
        "ArtifactBucketArn",
        "ArtifactBucketName",
        "DeploymentReadiness",
        "StateTableArn",
        "StateTableName",
    }:
        raise ValueError
    if (
        outputs.get("ArtifactBucketArn")
        != {"Value": {"Fn::GetAtt": ["PrivateArtifactBucket", "Arn"]}}
        or outputs.get("ArtifactBucketName") != {"Value": {"Ref": "PrivateArtifactBucket"}}
        or outputs.get("StateTableArn")
        != {"Value": {"Fn::GetAtt": ["OperationalStateTable", "Arn"]}}
        or outputs.get("StateTableName") != {"Value": {"Ref": "OperationalStateTable"}}
        or not isinstance(outputs.get("DeploymentReadiness"), Mapping)
        or cast(Mapping[str, object], outputs["DeploymentReadiness"]).get("Value")
        != "FOUNDATION_ONLY"
    ):
        raise ValueError


def _verify_change_set_template(observation: Mapping[str, object]) -> None:
    if set(observation) != {"StagesAvailable", "TemplateBody"}:
        raise ValueError
    stages = observation.get("StagesAvailable")
    body = observation.get("TemplateBody")
    if (
        not isinstance(stages, list)
        or stages != ["Original", "Processed"]
        or not isinstance(body, Mapping)
        or _fingerprint(body) != FOUNDATION_TEMPLATE_FINGERPRINT
    ):
        raise ValueError


def _verify_change_set(observation: Mapping[str, object], binding: Phase6FoundationBinding) -> None:
    expected_tags = _stack_tags(binding.environment_name)
    if (
        observation.get("StackName") != binding.stack_name
        or observation.get("ChangeSetName") != binding.change_set_name
        or "ChangeSetType" in observation
        or "RoleARN" in observation
        or observation.get("Status") != "CREATE_COMPLETE"
        or observation.get("ExecutionStatus") != "AVAILABLE"
        or observation.get("Description")
        != f"Mr Lister Phase 6 create-only foundation {FOUNDATION_TEMPLATE_FINGERPRINT}"
        or observation.get("IncludeNestedStacks") not in (None, False)
        or observation.get("ImportExistingResources") not in (None, False)
        or observation.get("OnStackFailure") != "DO_NOTHING"
        or observation.get("NotificationARNs", []) != []
        or observation.get("Capabilities", []) != []
        or observation.get("RollbackConfiguration", {})
        not in (
            {},
            {"RollbackTriggers": []},
            {"MonitoringTimeInMinutes": 0, "RollbackTriggers": []},
        )
        or _key_value_records(observation.get("Parameters"), "ParameterKey", "ParameterValue")
        != {"EnvironmentName": binding.environment_name}
        or _key_value_records(observation.get("Tags"), "Key", "Value") != expected_tags
    ):
        raise ValueError
    change_set_id = observation.get("ChangeSetId")
    stack_id = observation.get("StackId")
    creation_time = observation.get("CreationTime")
    if (
        not isinstance(change_set_id, str)
        or not change_set_id.startswith(
            f"arn:aws:cloudformation:{binding.region}:{binding.account_id}:"
            f"changeSet/{binding.change_set_name}/"
        )
        or not isinstance(stack_id, str)
        or not stack_id.startswith(
            f"arn:aws:cloudformation:{binding.region}:{binding.account_id}:"
            f"stack/{binding.stack_name}/"
        )
        or not isinstance(creation_time, str)
        or not creation_time.strip()
    ):
        raise ValueError
    changes = observation.get("Changes")
    if not isinstance(changes, list) or len(changes) != 3:
        raise ValueError
    actual: dict[str, str] = {}
    for change in changes:
        if not isinstance(change, Mapping) or change.get("Type") != "Resource":
            raise ValueError
        resource = change.get("ResourceChange")
        if not isinstance(resource, Mapping):
            raise ValueError
        logical_id = resource.get("LogicalResourceId")
        resource_type = resource.get("ResourceType")
        if (
            not isinstance(logical_id, str)
            or logical_id in actual
            or resource.get("Action") != "Add"
            or not isinstance(resource_type, str)
            or resource.get("Replacement") not in (None, "False", False)
            or resource.get("Scope", []) != []
            or resource.get("Details", []) != []
            or resource.get("ModuleInfo") not in (None, {})
        ):
            raise ValueError
        actual[logical_id] = resource_type
    if actual != FOUNDATION_RESOURCE_TYPES:
        raise ValueError


def _verify_pending_stack_observations(
    stack_observation: Mapping[str, object],
    resources_observation: Mapping[str, object],
    change_set_observation: Mapping[str, object],
    binding: Phase6FoundationBinding,
) -> None:
    stacks = stack_observation.get("Stacks")
    if (
        set(stack_observation) != {"Stacks"}
        or not isinstance(stacks, list)
        or len(stacks) != 1
        or not isinstance(stacks[0], Mapping)
    ):
        raise ValueError
    stack = cast(Mapping[str, object], stacks[0])
    if (
        stack.get("StackName") != binding.stack_name
        or stack.get("StackId") != change_set_observation.get("StackId")
        or stack.get("CreationTime") != change_set_observation.get("CreationTime")
        or stack.get("StackStatus") != "REVIEW_IN_PROGRESS"
        or stack.get("StackStatusReason") != "User Initiated"
        or stack.get("RoleARN") != binding.execution_role_arn
        or stack.get("DisableRollback") is not False
        or stack.get("EnableTerminationProtection") is not False
        or stack.get("NotificationARNs") != []
        or stack.get("RollbackConfiguration") != {}
        or stack.get("Tags") != []
        or "LastUpdatedTime" in stack
        or "Outputs" in stack
    ):
        raise ValueError
    if resources_observation != {"StackResourceSummaries": []}:
        raise ValueError


def _verify_stack_observation(
    observation: Mapping[str, object],
    binding: Phase6FoundationBinding,
    *,
    expected_change_set_id: str,
    expected_stack_id: str,
    expected_creation_time: str,
) -> str:
    stacks = observation.get("Stacks")
    if (
        set(observation) != {"Stacks"}
        or not isinstance(stacks, list)
        or len(stacks) != 1
        or not isinstance(stacks[0], Mapping)
    ):
        raise ValueError
    stack = cast(Mapping[str, object], stacks[0])
    stack_id = stack.get("StackId")
    last_updated_time = stack.get("LastUpdatedTime")
    last_operations = stack.get("LastOperations")
    creation_timestamp = _aws_utc_datetime(expected_creation_time)
    last_updated_timestamp = _aws_utc_datetime(last_updated_time)
    if (
        stack.get("StackName") != binding.stack_name
        or stack.get("ChangeSetId") != expected_change_set_id
        or stack.get("Description") != "Mr Lister Phase 6 create-only durable foundation"
        or stack.get("StackStatus") != "CREATE_COMPLETE"
        or stack.get("RoleARN") != binding.execution_role_arn
        or stack.get("EnableTerminationProtection") is not True
        or stack.get("DisableRollback") is not True
        or stack.get("DeploymentConfig") != {"Mode": "STANDARD", "DisableRollback": True}
        or stack.get("NotificationARNs") != []
        or stack.get("RollbackConfiguration") != {}
        or stack_id != expected_stack_id
        or stack.get("CreationTime") != expected_creation_time
        or last_updated_timestamp <= creation_timestamp
        or not isinstance(last_operations, list)
        or len(last_operations) != 1
        or not isinstance(last_operations[0], Mapping)
        or set(last_operations[0]) != {"OperationId", "OperationType"}
        or last_operations[0].get("OperationType") != "CREATE_STACK"
        or not isinstance(last_operations[0].get("OperationId"), str)
        or _ARN_ID.fullmatch(cast(str, last_operations[0]["OperationId"])) is None
        or _key_value_records(stack.get("Parameters"), "ParameterKey", "ParameterValue")
        != {"EnvironmentName": binding.environment_name}
        or _key_value_records(stack.get("Tags"), "Key", "Value")
        != _stack_tags(binding.environment_name)
        or _key_value_records(
            stack.get("Outputs"),
            "OutputKey",
            "OutputValue",
            allowed_extra={"Description", "ExportName"},
        )
        != {
            "ArtifactBucketArn": binding.bucket_arn,
            "ArtifactBucketName": binding.bucket_name,
            "DeploymentReadiness": "FOUNDATION_ONLY",
            "StateTableArn": binding.table_arn,
            "StateTableName": binding.table_name,
        }
    ):
        raise ValueError
    return cast(str, stack_id)


def _verify_stack_resources(
    observation: Mapping[str, object], binding: Phase6FoundationBinding
) -> None:
    resources = observation.get("StackResourceSummaries")
    if (
        not isinstance(resources, list)
        or observation.get("NextToken") is not None
        or set(observation) - {"NextToken", "StackResourceSummaries"}
        or len(resources) != 3
    ):
        raise ValueError
    actual: dict[str, tuple[str, str]] = {}
    for resource in resources:
        if not isinstance(resource, Mapping):
            raise ValueError
        logical_id = resource.get("LogicalResourceId")
        resource_type = resource.get("ResourceType")
        physical_id = resource.get("PhysicalResourceId")
        if (
            not isinstance(logical_id, str)
            or logical_id in actual
            or resource.get("ResourceStatus") != "CREATE_COMPLETE"
            or not isinstance(resource_type, str)
            or not isinstance(physical_id, str)
        ):
            raise ValueError
        actual[logical_id] = (resource_type, physical_id)
    expected = {
        "OperationalStateTable": ("AWS::DynamoDB::Table", binding.table_name),
        "PrivateArtifactBucket": ("AWS::S3::Bucket", binding.bucket_name),
        "PrivateArtifactBucketPolicy": ("AWS::S3::BucketPolicy", binding.bucket_name),
    }
    if actual != expected:
        raise ValueError


def _verify_table_observation(
    observation: Mapping[str, object], binding: Phase6FoundationBinding
) -> str:
    table = observation.get("Table")
    if set(observation) != {"Table"} or not isinstance(table, Mapping):
        raise ValueError
    if (
        table.get("TableName") != binding.table_name
        or table.get("TableArn") != binding.table_arn
        or table.get("TableStatus") != "ACTIVE"
        or table.get("BillingModeSummary", {}).get("BillingMode") != "PAY_PER_REQUEST"
        or _key_schema(table.get("KeySchema")) != {"HASH": "PK", "RANGE": "SK"}
        or _attribute_definitions(table.get("AttributeDefinitions"))
        != {
            "PK": "S",
            "SK": "S",
            "dispatch_pk": "S",
            "dispatch_sk": "S",
            "owner_jobs_pk": "S",
            "owner_jobs_sk": "S",
            "recovery_pk": "S",
            "recovery_sk": "S",
        }
        or table.get("StreamSpecification")
        != {"StreamEnabled": True, "StreamViewType": "KEYS_ONLY"}
        or _deployed_index_shapes(table.get("GlobalSecondaryIndexes"))
        != {
            "DueWorkIndex": ("dispatch_pk", "dispatch_sk", "ALL", "ACTIVE"),
            "OwnerJobsIndex": ("owner_jobs_pk", "owner_jobs_sk", "ALL", "ACTIVE"),
            "ExecutionRecoveryIndex": ("recovery_pk", "recovery_sk", "KEYS_ONLY", "ACTIVE"),
        }
    ):
        raise ValueError
    sse = table.get("SSEDescription")
    stream_arn = table.get("LatestStreamArn")
    if (
        not isinstance(sse, Mapping)
        or sse.get("Status") != "ENABLED"
        or sse.get("SSEType") != "KMS"
        or not isinstance(stream_arn, str)
        or not stream_arn.startswith(f"{binding.table_arn}/stream/")
    ):
        raise ValueError
    return stream_arn


def _verify_table_ttl(observation: Mapping[str, object], binding: Phase6FoundationBinding) -> None:
    if observation != {
        "TimeToLiveDescription": {
            "AttributeName": "expires_at",
            "TimeToLiveStatus": "ENABLED",
        }
    }:
        raise ValueError


def _verify_table_backups(observation: Mapping[str, object]) -> None:
    description = observation.get("ContinuousBackupsDescription")
    if set(observation) != {"ContinuousBackupsDescription"} or not isinstance(description, Mapping):
        raise ValueError
    pitr = description.get("PointInTimeRecoveryDescription")
    if (
        description.get("ContinuousBackupsStatus") != "ENABLED"
        or not isinstance(pitr, Mapping)
        or pitr.get("PointInTimeRecoveryStatus") != "ENABLED"
    ):
        raise ValueError


def _verify_bucket_observations(
    captures: Mapping[str, Mapping[str, object]],
    binding: Phase6FoundationBinding,
    *,
    stack_id: str,
) -> None:
    encryption = captures["bucket_encryption"].get("ServerSideEncryptionConfiguration")
    if (
        set(captures["bucket_encryption"]) != {"ServerSideEncryptionConfiguration"}
        or not isinstance(encryption, Mapping)
        or set(encryption) != {"Rules"}
    ):
        raise ValueError
    encryption_rules = encryption.get("Rules")
    if not isinstance(encryption_rules, list) or len(encryption_rules) != 1:
        raise ValueError
    [encryption_rule] = encryption_rules
    if not isinstance(encryption_rule, Mapping):
        raise ValueError
    encryption_default = encryption_rule.get("ApplyServerSideEncryptionByDefault")
    blocked_types = encryption_rule.get("BlockedEncryptionTypes")
    if (
        not isinstance(encryption_default, Mapping)
        or encryption_default != {"SSEAlgorithm": "AES256"}
        or encryption_rule.get("BucketKeyEnabled") not in (None, False)
        or blocked_types not in (None, {"EncryptionType": ["SSE-C"]})
        or set(encryption_rule)
        - {
            "ApplyServerSideEncryptionByDefault",
            "BlockedEncryptionTypes",
            "BucketKeyEnabled",
        }
    ):
        raise ValueError
    if captures["bucket_versioning"] != {"Status": "Enabled"}:
        raise ValueError
    if captures["bucket_public_access"] != {
        "PublicAccessBlockConfiguration": {
            "BlockPublicAcls": True,
            "BlockPublicPolicy": True,
            "IgnorePublicAcls": True,
            "RestrictPublicBuckets": True,
        }
    }:
        raise ValueError
    if captures["bucket_ownership"] != {
        "OwnershipControls": {"Rules": [{"ObjectOwnership": "BucketOwnerEnforced"}]}
    }:
        raise ValueError
    _verify_bucket_lifecycle(captures["bucket_lifecycle"])
    _verify_resource_tags(
        captures["bucket_tags"],
        expected=_resource_tags(binding.environment_name, "PrivateArtwork"),
        binding=binding,
        logical_id="PrivateArtifactBucket",
        stack_id=stack_id,
    )
    policy_value = captures["bucket_policy"].get("Policy")
    if set(captures["bucket_policy"]) != {"Policy"} or not isinstance(policy_value, str):
        raise ValueError
    policy = json.loads(policy_value)
    if policy != _deployed_bucket_policy(binding.bucket_arn):
        raise ValueError
    if captures["bucket_policy_status"] != {"PolicyStatus": {"IsPublic": False}}:
        raise ValueError
    if captures["bucket_cors_absence"] != {
        "bucket_name": binding.bucket_name,
        "error_code": "NoSuchCORSConfiguration",
        "format": "mr-lister-s3-cors-absence-v1",
        "http_status_code": 404,
        "operation": "GetBucketCors",
    }:
        raise ValueError


def _verify_bucket_lifecycle(observation: Mapping[str, object]) -> None:
    if set(observation) - {"Rules", "TransitionDefaultMinimumObjectSize"}:
        raise ValueError
    minimum_size = observation.get("TransitionDefaultMinimumObjectSize")
    if minimum_size not in (None, "all_storage_classes_128K"):
        raise ValueError
    rules = observation.get("Rules")
    if not isinstance(rules, list) or len(rules) != 3:
        raise ValueError
    by_id = {
        cast(str, rule.get("ID")): rule
        for rule in rules
        if isinstance(rule, Mapping) and isinstance(rule.get("ID"), str)
    }
    if set(by_id) != {
        "AbortIncompleteMultipartUploads",
        "ExpireUnreferencedStagedArtwork",
        "RemoveExpiredPrivateSourceDeleteMarkers",
    }:
        raise ValueError
    abort = by_id["AbortIncompleteMultipartUploads"]
    staged = by_id["ExpireUnreferencedStagedArtwork"]
    markers = by_id["RemoveExpiredPrivateSourceDeleteMarkers"]
    if (
        set(abort)
        not in (
            {"AbortIncompleteMultipartUpload", "Filter", "ID", "Status"},
            {"AbortIncompleteMultipartUpload", "ID", "Prefix", "Status"},
        )
        or abort.get("Status") != "Enabled"
        or abort.get("AbortIncompleteMultipartUpload") != {"DaysAfterInitiation": 7}
        or not _empty_prefix_filter(abort.get("Filter"), abort.get("Prefix"))
        or set(staged)
        != {
            "Expiration",
            "Filter",
            "ID",
            "NoncurrentVersionExpiration",
            "Status",
        }
        or staged.get("Status") != "Enabled"
        or staged.get("Filter")
        not in (
            {"Tag": {"Key": "mr-lister-state", "Value": "staged"}},
            {"And": {"Tags": [{"Key": "mr-lister-state", "Value": "staged"}]}},
            {
                "And": {
                    "Prefix": "",
                    "Tags": [{"Key": "mr-lister-state", "Value": "staged"}],
                }
            },
        )
        or staged.get("Expiration") != {"Days": 1}
        or staged.get("NoncurrentVersionExpiration") != {"NoncurrentDays": 1}
        or set(markers)
        not in (
            {"Expiration", "Filter", "ID", "Status"},
            {"Expiration", "ID", "Prefix", "Status"},
        )
        or markers.get("Status") != "Enabled"
        or not _prefix_filter(
            markers.get("Filter"), markers.get("Prefix"), expected="private/owners/"
        )
        or markers.get("Expiration") != {"ExpiredObjectDeleteMarker": True}
    ):
        raise ValueError


def _verify_resource_tags(
    observation: Mapping[str, object],
    *,
    expected: dict[str, str],
    binding: Phase6FoundationBinding,
    logical_id: str,
    stack_id: str,
) -> None:
    tags = observation.get("Tags", observation.get("TagSet"))
    actual = _key_value_records(tags, "Key", "Value")
    system_tags = {
        "aws:cloudformation:logical-id": logical_id,
        "aws:cloudformation:stack-id": stack_id,
        "aws:cloudformation:stack-name": binding.stack_name,
    }
    if set(observation) not in ({"Tags"}, {"TagSet"}) or actual not in (
        expected,
        expected | system_tags,
    ):
        raise ValueError


def _load_evidence_directory(path: Path) -> dict[str, Mapping[str, object]]:
    if path.is_symlink():
        raise ValueError
    root = path.resolve(strict=True)
    if not root.is_dir():
        raise ValueError
    captures: dict[str, Mapping[str, object]] = {}
    for key, filename in _DEPLOYED_EVIDENCE_FILES.items():
        value = _load_json_file(root / filename)
        if not isinstance(value, Mapping):
            raise ValueError
        captures[key] = value
    return captures


def _load_json_file(path: Path) -> object:
    if path.is_symlink() or not path.is_file():
        raise ValueError
    return json.loads(path.read_text(encoding="utf-8"))


def _aws_utc_datetime(value: object) -> datetime:
    if not isinstance(value, str) or not value.endswith("+00:00"):
        raise ValueError
    parsed = datetime.fromisoformat(value)
    if parsed.utcoffset() != timedelta(0):
        raise ValueError
    return parsed


def _fingerprint(value: object) -> str:
    return sha256(
        (
            json.dumps(
                value,
                allow_nan=False,
                separators=(",", ":"),
                sort_keys=True,
            )
            + "\n"
        ).encode("utf-8")
    ).hexdigest()


def _key_value_records(
    value: object,
    key_name: str,
    value_name: str,
    *,
    allowed_extra: set[str] | None = None,
) -> dict[str, str]:
    if not isinstance(value, list):
        raise ValueError
    result: dict[str, str] = {}
    for item in value:
        if (
            not isinstance(item, Mapping)
            or not {key_name, value_name} <= set(item)
            or set(item) - {key_name, value_name} - (allowed_extra or set())
        ):
            raise ValueError
        key = item.get(key_name)
        item_value = item.get(value_name)
        if not isinstance(key, str) or not isinstance(item_value, str) or key in result:
            raise ValueError
        result[key] = item_value
    return result


def _template_tags(value: object) -> dict[str, object]:
    if not isinstance(value, list):
        raise ValueError
    result: dict[str, object] = {}
    for item in value:
        if not isinstance(item, Mapping) or set(item) != {"Key", "Value"}:
            raise ValueError
        key = item.get("Key")
        if not isinstance(key, str) or key in result:
            raise ValueError
        result[key] = item.get("Value")
    return result


def _index_shapes(value: object) -> dict[str, tuple[str, str, str]]:
    if not isinstance(value, list):
        raise ValueError
    result: dict[str, tuple[str, str, str]] = {}
    for item in value:
        if not isinstance(item, Mapping):
            raise ValueError
        name = item.get("IndexName")
        schema = item.get("KeySchema")
        projection = item.get("Projection")
        if (
            not isinstance(name, str)
            or name in result
            or not isinstance(schema, list)
            or len(schema) != 2
            or not isinstance(projection, Mapping)
        ):
            raise ValueError
        by_type = {
            cast(str, part.get("KeyType")): part.get("AttributeName")
            for part in schema
            if isinstance(part, Mapping)
        }
        if set(by_type) != {"HASH", "RANGE"}:
            raise ValueError
        result[name] = (
            cast(str, by_type["HASH"]),
            cast(str, by_type["RANGE"]),
            cast(str, projection.get("ProjectionType")),
        )
    return result


def _deployed_index_shapes(value: object) -> dict[str, tuple[str, str, str, str]]:
    if not isinstance(value, list):
        raise ValueError
    result: dict[str, tuple[str, str, str, str]] = {}
    for item in value:
        if not isinstance(item, Mapping):
            raise ValueError
        name = item.get("IndexName")
        schema = item.get("KeySchema")
        projection = item.get("Projection")
        if (
            not isinstance(name, str)
            or name in result
            or not isinstance(schema, list)
            or len(schema) != 2
            or not isinstance(projection, Mapping)
        ):
            raise ValueError
        by_type = {
            cast(str, part.get("KeyType")): part.get("AttributeName")
            for part in schema
            if isinstance(part, Mapping)
        }
        result[name] = (
            cast(str, by_type.get("HASH")),
            cast(str, by_type.get("RANGE")),
            cast(str, projection.get("ProjectionType")),
            cast(str, item.get("IndexStatus")),
        )
    return result


def _attribute_definitions(value: object) -> dict[str, str]:
    if not isinstance(value, list):
        raise ValueError
    result: dict[str, str] = {}
    for item in value:
        if not isinstance(item, Mapping) or set(item) != {"AttributeName", "AttributeType"}:
            raise ValueError
        name = item.get("AttributeName")
        attribute_type = item.get("AttributeType")
        if not isinstance(name, str) or not isinstance(attribute_type, str) or name in result:
            raise ValueError
        result[name] = attribute_type
    return result


def _key_schema(value: object) -> dict[str, str]:
    if not isinstance(value, list):
        raise ValueError
    result: dict[str, str] = {}
    for item in value:
        if not isinstance(item, Mapping) or set(item) != {"AttributeName", "KeyType"}:
            raise ValueError
        name = item.get("AttributeName")
        key_type = item.get("KeyType")
        if not isinstance(name, str) or not isinstance(key_type, str) or key_type in result:
            raise ValueError
        result[key_type] = name
    return result


def _template_bucket_lifecycle() -> dict[str, object]:
    return {
        "Rules": [
            {
                "AbortIncompleteMultipartUpload": {"DaysAfterInitiation": 7},
                "Id": "AbortIncompleteMultipartUploads",
                "Status": "Enabled",
            },
            {
                "ExpirationInDays": 1,
                "Id": "ExpireUnreferencedStagedArtwork",
                "NoncurrentVersionExpiration": {"NoncurrentDays": 1},
                "Status": "Enabled",
                "TagFilters": [{"Key": "mr-lister-state", "Value": "staged"}],
            },
            {
                "ExpiredObjectDeleteMarker": True,
                "Id": "RemoveExpiredPrivateSourceDeleteMarkers",
                "Prefix": "private/owners/",
                "Status": "Enabled",
            },
        ]
    }


def _template_bucket_policy() -> dict[str, object]:
    return {
        "Statement": [
            {
                "Action": "s3:*",
                "Condition": {"Bool": {"aws:SecureTransport": "false"}},
                "Effect": "Deny",
                "Principal": "*",
                "Resource": [
                    {"Fn::GetAtt": ["PrivateArtifactBucket", "Arn"]},
                    {"Fn::Sub": "${PrivateArtifactBucket.Arn}/*"},
                ],
                "Sid": "DenyInsecureTransport",
            },
            {
                "Action": "s3:PutObject",
                "Condition": {"NumericGreaterThan": {"s3:signatureAge": "300000"}},
                "Effect": "Deny",
                "Principal": "*",
                "Resource": {
                    "Fn::Sub": (
                        "${PrivateArtifactBucket.Arn}/private/owners/*/jobs/*/source/source.png"
                    )
                },
                "Sid": "DenyStaleBrowserUploadSignatures",
            },
            {
                "Action": "s3:PutObject",
                "Condition": {"StringNotEquals": {"s3:x-amz-server-side-encryption": "AES256"}},
                "Effect": "Deny",
                "Principal": "*",
                "Resource": {
                    "Fn::Sub": (
                        "${PrivateArtifactBucket.Arn}/private/owners/*/jobs/*/source/source.png"
                    )
                },
                "Sid": "DenyUnencryptedBrowserUploads",
            },
        ],
        "Version": "2012-10-17",
    }


def _deployed_bucket_policy(bucket_arn: str) -> dict[str, object]:
    source_arn = f"{bucket_arn}/private/owners/*/jobs/*/source/source.png"
    return {
        "Statement": [
            {
                "Action": "s3:*",
                "Condition": {"Bool": {"aws:SecureTransport": "false"}},
                "Effect": "Deny",
                "Principal": "*",
                "Resource": [bucket_arn, f"{bucket_arn}/*"],
                "Sid": "DenyInsecureTransport",
            },
            {
                "Action": "s3:PutObject",
                "Condition": {"NumericGreaterThan": {"s3:signatureAge": "300000"}},
                "Effect": "Deny",
                "Principal": "*",
                "Resource": source_arn,
                "Sid": "DenyStaleBrowserUploadSignatures",
            },
            {
                "Action": "s3:PutObject",
                "Condition": {"StringNotEquals": {"s3:x-amz-server-side-encryption": "AES256"}},
                "Effect": "Deny",
                "Principal": "*",
                "Resource": source_arn,
                "Sid": "DenyUnencryptedBrowserUploads",
            },
        ],
        "Version": "2012-10-17",
    }


def _empty_prefix_filter(filter_value: object, prefix_value: object) -> bool:
    return filter_value in (None, {"Prefix": ""}) and prefix_value in (None, "")


def _prefix_filter(filter_value: object, prefix_value: object, *, expected: str) -> bool:
    return (filter_value == {"Prefix": expected} and prefix_value is None) or (
        filter_value is None and prefix_value == expected
    )


def _stack_tags(environment_name: str) -> dict[str, str]:
    return {
        "DeploymentClass": "FOUNDATION_ONLY",
        "Environment": environment_name,
        "Project": "MrLister",
    }


def _resource_tags(environment_name: str, classification: str) -> dict[str, str]:
    return _stack_tags(environment_name) | {"DataClassification": classification}


def _binding_from_args(args: argparse.Namespace) -> Phase6FoundationBinding:
    return Phase6FoundationBinding(
        account_id=args.account_id,
        region=args.region,
        environment_name=args.environment_name,
        stack_name=args.stack_name,
        execution_role_arn=args.execution_role_arn,
        deployer_arn=args.deployer_arn,
    )


def _json_mapping(path: str) -> Mapping[str, object]:
    value = _load_json_file(Path(path))
    if not isinstance(value, Mapping):
        raise Phase6FoundationDeploymentError(_GENERIC_ERROR)
    return value


def _add_binding_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--account-id", required=True)
    parser.add_argument("--region", default="us-west-2")
    parser.add_argument("--environment-name", default="dev")
    parser.add_argument("--stack-name", default="mr-lister-phase6-dev")
    parser.add_argument("--execution-role-arn", required=True)
    parser.add_argument("--deployer-arn", required=True)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    template = subparsers.add_parser("template", help="verify the local frozen template")
    template.add_argument("--template", type=Path, default=DEFAULT_TEMPLATE)

    absence = subparsers.add_parser("absence", help="verify the pre-create absence capture")
    _add_binding_arguments(absence)
    absence.add_argument("--observation", required=True)

    change_set = subparsers.add_parser("change-set", help="verify the reviewed CREATE change set")
    _add_binding_arguments(change_set)
    change_set.add_argument("--template", type=Path, default=DEFAULT_TEMPLATE)
    change_set.add_argument("--absence-observation", required=True)
    change_set.add_argument("--observation", required=True)
    change_set.add_argument("--pending-stack-observation", required=True)
    change_set.add_argument("--pending-stack-resources-observation", required=True)
    change_set.add_argument("--template-observation", required=True)

    deployed = subparsers.add_parser("deployed", help="verify all post-create evidence")
    _add_binding_arguments(deployed)
    deployed.add_argument("--template", type=Path, default=DEFAULT_TEMPLATE)
    deployed.add_argument("--evidence-directory", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run one offline verification gate and emit only a canonical success descriptor."""

    try:
        args = _parser().parse_args(argv)
        if args.command == "template":
            result: object = {
                "format": FOUNDATION_EVIDENCE_FORMAT,
                "foundation_template_fingerprint": verify_foundation_template(args.template),
            }
        else:
            binding = _binding_from_args(args)
            if args.command == "absence":
                verify_stack_absence_observation(_json_mapping(args.observation), binding)
                result = {
                    "format": FOUNDATION_EVIDENCE_FORMAT,
                    "stack_absent": True,
                    "stack_name": binding.stack_name,
                }
            elif args.command == "change-set":
                verify_create_change_set_observations(
                    _json_mapping(args.observation),
                    _json_mapping(args.template_observation),
                    _json_mapping(args.absence_observation),
                    binding,
                    pending_stack_observation=_json_mapping(args.pending_stack_observation),
                    pending_stack_resources_observation=_json_mapping(
                        args.pending_stack_resources_observation
                    ),
                    template_path=args.template,
                )
                result = {
                    "change_set_name": binding.change_set_name,
                    "change_set_type": "CREATE",
                    "format": FOUNDATION_EVIDENCE_FORMAT,
                    "stack_name": binding.stack_name,
                }
            else:
                result = verify_deployed_foundation(
                    args.evidence_directory,
                    binding,
                    template_path=args.template,
                )
        # Downstream release renderers consume this descriptor as byte-canonical evidence.
        # Match ``mr_lister.release.phase6.render_manifest`` rather than emitting a second,
        # compact JSON dialect that is semantically equal but fails the sealed-byte check.
        print(
            json.dumps(
                result,
                allow_nan=False,
                indent=2,
                separators=(",", ": "),
                sort_keys=True,
            )
        )
        return 0
    except Phase6FoundationDeploymentError as error:
        raise SystemExit(str(error)) from None


if __name__ == "__main__":
    raise SystemExit(main())
