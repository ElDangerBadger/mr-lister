"""Verify one reviewed Phase 6 CloudFormation UPDATE from closed AWS evidence.

The gate is intentionally offline: it imports no AWS SDK, starts no subprocess, and never
executes a change set. It joins the locally sealed Lambda archive and accepted common-v2 S3
release-object evidence to a short-lived review manifest, the existing foundation, Original and
Processed templates, property-value change details, an isolated deployer role and its exact IAM
policy, the complete verifier-derived runtime execution policy, and the single successful
CloudTrail ``CreateChangeSet`` management event.

Success proves only what the supplied captures showed. The emitted descriptor requires the
entire evidence set to be recaptured and verified immediately before a separate execution
decision; it is not a live-availability assertion.
"""

from __future__ import annotations

import argparse
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path
from typing import cast
from urllib.parse import parse_qs, unquote, urlsplit

from tools.build_phase66_source_bundles import (
    DEPLOYMENT_DESCRIPTOR_FILENAME,
    LAMBDA_ARCHIVE_FILENAME,
    verify_phase6_deployment_artifacts,
)
from tools.verify_phase6_s3_release_object import (
    Phase6S3ReleaseObjectExpectation,
    VerifiedPhase6S3ReleaseObject,
    verify_phase6_s3_release_object_evidence,
)

FOUNDATION_BINDING_FORMAT = "mr-lister-phase6-foundation-deployment-v1"
FOUNDATION_TEMPLATE_FINGERPRINT = "689897c254c9db97aa75d508f140980f9b6a5129c0c1fa0121eb8d6ef1e64874"
UPDATE_MANIFEST_FORMAT = "mr-lister-phase6-reviewed-update-manifest-v2"
UPDATE_EVIDENCE_FORMAT = "mr-lister-phase6-reviewed-update-v2"
RUNTIME_UPDATE_BOOTSTRAP = (
    Path(__file__).resolve().parents[1] / "infra/phase6/runtime-update-bootstrap.json"
)

MAX_REVIEW_WINDOW = timedelta(minutes=15)
MAX_EVENT_TO_CHANGE_SET_DELAY = timedelta(minutes=5)
MAX_FUTURE_EVENT_SKEW = timedelta(minutes=2)

FOUNDATION_RESOURCE_TYPES = {
    "OperationalStateTable": "AWS::DynamoDB::Table",
    "PrivateArtifactBucket": "AWS::S3::Bucket",
    "PrivateArtifactBucketPolicy": "AWS::S3::BucketPolicy",
}

_ACCOUNT_ID = re.compile(r"^[0-9]{12}$")
_ENVIRONMENT = re.compile(r"^[a-z][a-z0-9-]{1,15}$")
_FINGERPRINT = re.compile(r"^[a-f0-9]{64}$")
_CHANGE_SET_NAME = re.compile(r"^[A-Za-z][-A-Za-z0-9]{0,127}$")
_CLIENT_TOKEN = re.compile(r"^[A-Za-z0-9][-A-Za-z0-9]{0,127}$")
_ROLE_ID = re.compile(r"^ARO[A-Z0-9]{12,64}$")
_ACCESS_KEY_ID = re.compile(r"^ASIA[A-Z0-9]{12,32}$")
_PLACEHOLDER = re.compile(
    r"<[A-Z][A-Z0-9_]*>|\$\{[^}\r\n]+}|__[A-Z][A-Z0-9_]*__|"
    r"\b(?:PLACEHOLDER|REPLACE_ME|CHANGEME)\b",
    re.IGNORECASE,
)
_MOVING_VERSION_IDS = frozenset(
    {"current", "default", "latest", "moving", "null", "none", "unversioned", "pending"}
)
_UUID = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)
_GENERIC_ERROR = "Phase 6 runtime UPDATE evidence is invalid"

_FOUNDATION_BINDING_KEYS = {
    "account_id",
    "artifact_bucket_arn",
    "artifact_bucket_name",
    "environment_name",
    "format",
    "foundation_template_fingerprint",
    "operational_state_stream_arn",
    "operational_state_table_arn",
    "operational_state_table_name",
    "region",
    "stack_id",
    "stack_name",
}
_MANIFEST_KEYS = {
    "account_id",
    "capabilities",
    "change_set_description",
    "change_set_name",
    "changes",
    "client_token",
    "deployment_config",
    "deployer_policy_name",
    "deployer_role_arn",
    "deployer_session_name",
    "environment_name",
    "execution_role_arn",
    "execution_role_policy_fingerprint",
    "execution_role_policy_name",
    "format",
    "foundation_binding_fingerprint",
    "lambda_release_object_evidence_fingerprint",
    "notification_arns",
    "parameters",
    "policy_expires_at",
    "processed_template_fingerprint",
    "region",
    "rollback_configuration",
    "stack_id",
    "stack_name",
    "tags",
    "target_template_fingerprint",
    "template_url",
}
_PRE_STACK_KEYS = {
    "ChangeSetId",
    "CreationTime",
    "DeploymentConfig",
    "Description",
    "DisableRollback",
    "DriftInformation",
    "EnableTerminationProtection",
    "LastOperations",
    "LastUpdatedTime",
    "NotificationARNs",
    "Outputs",
    "Parameters",
    "RoleARN",
    "RollbackConfiguration",
    "StackId",
    "StackName",
    "StackStatus",
    "Tags",
}
_CHANGE_SET_REQUIRED_KEYS = {
    "Capabilities",
    "ChangeSetId",
    "ChangeSetName",
    "Changes",
    "CreationTime",
    "DeploymentConfig",
    "Description",
    "ExecutionStatus",
    "IncludeNestedStacks",
    "NotificationARNs",
    "Parameters",
    "RollbackConfiguration",
    "StackId",
    "StackName",
    "Status",
    "Tags",
}
_CHANGE_SET_OPTIONAL_KEYS = {
    "DeploymentMode",
    "ImportExistingResources",
    "OnStackFailure",
    "ParentChangeSetId",
    "RootChangeSetId",
    "StackDriftStatus",
    "StatusReason",
}
_CHANGE_SET_KEYS = _CHANGE_SET_REQUIRED_KEYS | _CHANGE_SET_OPTIONAL_KEYS
_RESOURCE_SUMMARY_KEYS = {
    "DriftInformation",
    "LastUpdatedTimestamp",
    "LogicalResourceId",
    "PhysicalResourceId",
    "ResourceStatus",
    "ResourceType",
}
_MANIFEST_CHANGE_KEYS = {
    "action",
    "after_context",
    "before_context",
    "details",
    "logical_resource_id",
    "physical_resource_id",
    "replacement",
    "resource_type",
    "scope",
}
_RESOURCE_CHANGE_BASE_KEYS = {
    "Action",
    "Details",
    "LogicalResourceId",
    "ResourceType",
    "Scope",
}
_DETAIL_KEYS = {"CausingEntity", "ChangeSource", "Evaluation", "Target"}
_DETAIL_REQUIRED_KEYS = {"ChangeSource", "Evaluation", "Target"}
_TARGET_KEYS = {
    "AfterValue",
    "AfterValueFrom",
    "Attribute",
    "AttributeChangeType",
    "BeforeValue",
    "BeforeValueFrom",
    "Drift",
    "Name",
    "Path",
    "RequiresRecreation",
}
_SCOPES = {
    "Properties",
    "Metadata",
    "CreationPolicy",
    "UpdatePolicy",
    "DeletionPolicy",
    "UpdateReplacePolicy",
    "Tags",
}
_CHANGE_SOURCES = {
    "ResourceReference",
    "ParameterReference",
    "ResourceAttribute",
    "DirectModification",
    "Automatic",
    "NoModification",
}
_ROLE_KEYS_REQUIRED = {
    "Arn",
    "AssumeRolePolicyDocument",
    "CreateDate",
    "MaxSessionDuration",
    "Path",
    "RoleId",
    "RoleName",
    "Tags",
}
_ROLE_KEYS_ALLOWED = _ROLE_KEYS_REQUIRED | {"Description", "RoleLastUsed"}
_CLOUDTRAIL_EVENT_KEYS_REQUIRED = {
    "awsRegion",
    "eventCategory",
    "eventID",
    "eventName",
    "eventSource",
    "eventTime",
    "eventType",
    "eventVersion",
    "managementEvent",
    "readOnly",
    "recipientAccountId",
    "requestID",
    "requestParameters",
    "responseElements",
    "sourceIPAddress",
    "userAgent",
    "userIdentity",
}
_CLOUDTRAIL_EVENT_KEYS_ALLOWED = _CLOUDTRAIL_EVENT_KEYS_REQUIRED | {
    "additionalEventData",
    "sessionCredentialFromConsole",
    "tlsDetails",
}
_CLOUDTRAIL_REQUEST_REQUIRED = {
    "capabilities",
    "changeSetName",
    "changeSetType",
    "clientToken",
    "description",
    "includeNestedStacks",
    "parameters",
    "roleARN",
    "stackName",
    "tags",
    "templateURL",
}
_CLOUDTRAIL_REQUEST_ALLOWED = _CLOUDTRAIL_REQUEST_REQUIRED | {
    "deploymentConfig",
    "importExistingResources",
    "notificationARNs",
    "rollbackConfiguration",
}


class Phase6RuntimeUpdateError(RuntimeError):
    """A value-free failure for incomplete, drifting, or unsafe UPDATE evidence."""


@dataclass(frozen=True, slots=True)
class Phase6FoundationBinding:
    """The exact durable identities emitted by the accepted foundation verifier."""

    account_id: str
    region: str
    environment_name: str
    stack_name: str
    stack_id: str
    table_name: str
    table_arn: str
    stream_arn: str
    bucket_name: str
    bucket_arn: str
    fingerprint: str


@dataclass(frozen=True, slots=True)
class Phase6UpdateAuthority:
    """The exact human-reviewed authority for one short-lived change-set request."""

    execution_role_arn: str
    execution_role_policy_name: str
    execution_role_policy_fingerprint: str
    deployer_role_arn: str
    deployer_policy_name: str
    deployer_session_name: str
    change_set_name: str
    change_set_description: str
    client_token: str
    target_template_fingerprint: str
    processed_template_fingerprint: str
    lambda_release_object_evidence_fingerprint: str
    template_url: str
    policy_expires_at: datetime
    policy_expires_at_text: str
    parameters: dict[str, str]
    tags: dict[str, str]
    capabilities: tuple[str, ...]
    notification_arns: tuple[str, ...]
    rollback_configuration: Mapping[str, object]
    deployment_config: Mapping[str, object]
    changes: dict[str, Mapping[str, object]]
    fingerprint: str


@dataclass(frozen=True, slots=True)
class RoleAuthority:
    """Security-relevant fields from one IAM ``GetRole`` capture."""

    arn: str
    role_id: str
    normalized: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class CloudTrailAuthority:
    """The joined successful CreateChangeSet management event."""

    event_id: str
    event_time: datetime
    normalized: Mapping[str, object]


def semantic_fingerprint(value: object) -> str:
    """Return the repository's canonical semantic JSON SHA-256 fingerprint."""

    return sha256(
        (json.dumps(value, allow_nan=False, separators=(",", ":"), sort_keys=True) + "\n").encode(
            "utf-8"
        )
    ).hexdigest()


def verify_reviewed_update(
    *,
    deployment_root: Path,
    artifact_root: Path,
    lambda_object_evidence_path: Path,
    foundation_binding_path: Path,
    expected_manifest_path: Path,
    pre_stack_observation_path: Path,
    pre_stack_resources_observation_path: Path,
    change_set_observation_path: Path,
    original_template_observation_path: Path,
    processed_template_observation_path: Path,
    target_template_path: Path,
    caller_identity_observation_path: Path,
    cloudtrail_observation_path: Path,
    execution_role_observation_path: Path,
    execution_role_inline_policies_observation_path: Path,
    execution_role_attached_policies_observation_path: Path,
    execution_role_policy_observation_path: Path,
    deployer_role_observation_path: Path,
    deployer_role_inline_policies_observation_path: Path,
    deployer_role_attached_policies_observation_path: Path,
    deployer_role_policy_observation_path: Path,
) -> dict[str, object]:
    """Verify one complete, unpaginated, short-lived UPDATE evidence set."""

    try:
        documents = {
            "foundation_binding": _load_mapping(foundation_binding_path),
            "expected_manifest": _load_mapping(expected_manifest_path),
            "pre_stack": _load_mapping(pre_stack_observation_path),
            "pre_stack_resources": _load_mapping(pre_stack_resources_observation_path),
            "change_set": _load_mapping(change_set_observation_path),
            "original_template": _load_mapping(original_template_observation_path),
            "processed_template": _load_mapping(processed_template_observation_path),
            "target_template": _load_mapping(target_template_path),
            "caller_identity": _load_mapping(caller_identity_observation_path),
            "cloudtrail": _load_mapping(cloudtrail_observation_path),
            "execution_role": _load_mapping(execution_role_observation_path),
            "execution_role_inline_policies": _load_mapping(
                execution_role_inline_policies_observation_path
            ),
            "execution_role_attached_policies": _load_mapping(
                execution_role_attached_policies_observation_path
            ),
            "execution_role_policy": _load_mapping(execution_role_policy_observation_path),
            "deployer_role": _load_mapping(deployer_role_observation_path),
            "deployer_role_inline_policies": _load_mapping(
                deployer_role_inline_policies_observation_path
            ),
            "deployer_role_attached_policies": _load_mapping(
                deployer_role_attached_policies_observation_path
            ),
            "deployer_role_policy": _load_mapping(deployer_role_policy_observation_path),
        }

        binding = _verify_foundation_binding(documents["foundation_binding"])
        target_template = documents["target_template"]
        target_fingerprint = semantic_fingerprint(target_template)
        _verify_target_foundation_identities(target_template)
        lambda_object = _verify_lambda_release_object(
            deployment_root=deployment_root,
            artifact_root=artifact_root,
            evidence_path=lambda_object_evidence_path,
            binding=binding,
            target_template=target_template,
        )
        authority = _verify_expected_manifest(
            documents["expected_manifest"],
            binding,
            target_template=target_template,
            target_fingerprint=target_fingerprint,
            lambda_object=lambda_object,
        )

        previous_role_arn = _verify_pre_update_stack(documents["pre_stack"], binding)
        _verify_pre_update_resources(documents["pre_stack_resources"], binding)
        _verify_template_observation(
            documents["original_template"],
            expected_fingerprint=authority.target_template_fingerprint,
        )
        _verify_template_observation(
            documents["processed_template"],
            expected_fingerprint=authority.processed_template_fingerprint,
        )
        processed_body = cast(Mapping[str, object], documents["processed_template"]["TemplateBody"])
        _verify_target_foundation_identities(processed_body)

        execution_role = _verify_role(
            documents["execution_role"],
            expected_arn=authority.execution_role_arn,
            expected_name=f"mr-lister-phase6-runtime-cfn-{binding.environment_name}",
            expected_tags={
                "DeploymentClass": "RUNTIME_CFN_EXECUTION",
                "Environment": binding.environment_name,
                "Project": "MrLister",
            },
            expected_trust=_service_trust("cloudformation.amazonaws.com"),
            expires_at=None,
        )
        execution_policy = _verify_exact_role_policy_set(
            role_name=f"mr-lister-phase6-runtime-cfn-{binding.environment_name}",
            policy_name=authority.execution_role_policy_name,
            inline_list=documents["execution_role_inline_policies"],
            attached_list=documents["execution_role_attached_policies"],
            policy_observation=documents["execution_role_policy"],
        )
        expected_execution_policy = _expected_execution_policy(
            binding,
            lambda_object,
            target_template=target_template,
        )
        if (
            execution_policy != expected_execution_policy
            or semantic_fingerprint(expected_execution_policy)
            != authority.execution_role_policy_fingerprint
        ):
            raise ValueError

        deployer_role_name = f"mr-lister-phase6-runtime-update-deployer-{binding.environment_name}"
        deployer_role = _verify_role(
            documents["deployer_role"],
            expected_arn=authority.deployer_role_arn,
            expected_name=deployer_role_name,
            expected_tags={
                "DeploymentClass": "RUNTIME_UPDATE_DEPLOYER",
                "Environment": binding.environment_name,
                "ExpiresAt": authority.policy_expires_at_text,
                "Project": "MrLister",
            },
            expected_trust=_user_trust(f"arn:aws:iam::{binding.account_id}:user/mr-lister-dev"),
            expires_at=authority.policy_expires_at,
        )
        deployer_policy = _verify_exact_role_policy_set(
            role_name=deployer_role_name,
            policy_name=authority.deployer_policy_name,
            inline_list=documents["deployer_role_inline_policies"],
            attached_list=documents["deployer_role_attached_policies"],
            policy_observation=documents["deployer_role_policy"],
        )
        if deployer_policy != _expected_deployer_policy(binding, authority):
            raise ValueError

        _verify_caller_identity(documents["caller_identity"], binding, authority, deployer_role)
        change_set_id, change_set_time, normalized_change_set = _verify_update_change_set(
            documents["change_set"], binding, authority
        )
        cloudtrail = _verify_cloudtrail_event(
            documents["cloudtrail"],
            documents["caller_identity"],
            binding,
            authority,
            deployer_role,
            change_set_id=change_set_id,
            change_set_time=change_set_time,
        )
        _verify_review_window(authority, cloudtrail.event_time, change_set_time)

        normalized_evidence = {
            "caller_identity": documents["caller_identity"],
            "change_set": normalized_change_set,
            "cloudtrail_event": cloudtrail.normalized,
            "deployer_policy": deployer_policy,
            "deployer_policy_inventory": {
                "attached": documents["deployer_role_attached_policies"],
                "inline": documents["deployer_role_inline_policies"],
            },
            "deployer_role": deployer_role.normalized,
            "execution_policy": execution_policy,
            "execution_policy_inventory": {
                "attached": documents["execution_role_attached_policies"],
                "inline": documents["execution_role_inline_policies"],
            },
            "execution_role": execution_role.normalized,
            "expected_manifest": documents["expected_manifest"],
            "foundation_binding": documents["foundation_binding"],
            "lambda_release_object_evidence": {
                "archive_sha256": lambda_object.archive_sha256,
                "bucket": lambda_object.bucket,
                "checksum_sha256_base64": lambda_object.checksum_sha256_base64,
                "component": lambda_object.component,
                "evidence_sha256": lambda_object.evidence_sha256,
                "key": lambda_object.key,
                "release_fingerprint": lambda_object.release_fingerprint,
                "size_bytes": lambda_object.size_bytes,
                "version_id": lambda_object.version_id,
            },
            "original_template": documents["original_template"],
            "pre_stack": documents["pre_stack"],
            "pre_stack_resources": documents["pre_stack_resources"],
            "processed_template": documents["processed_template"],
            "target_template": target_template,
        }
        component_fingerprints = {
            key: semantic_fingerprint(value) for key, value in sorted(normalized_evidence.items())
        }
        bundle_fingerprint = semantic_fingerprint(component_fingerprints)
        return {
            "account_id": binding.account_id,
            "availability_claim": "CAPTURE_ONLY_RECAPTURE_REQUIRED",
            "change_set_id": change_set_id,
            "change_set_name": authority.change_set_name,
            "change_set_type": "UPDATE",
            "cloudtrail_create_event_id": cloudtrail.event_id,
            "cloudtrail_create_event_time": _canonical_utc(cloudtrail.event_time),
            "deployer_role_arn": authority.deployer_role_arn,
            "deployer_session_name": authority.deployer_session_name,
            "evidence_bundle_fingerprint": bundle_fingerprint,
            "evidence_component_fingerprints": component_fingerprints,
            "execution_role_arn": authority.execution_role_arn,
            "lambda_archive_sha256": lambda_object.archive_sha256,
            "lambda_archive_size_bytes": lambda_object.size_bytes,
            "lambda_release_object_bucket": lambda_object.bucket,
            "lambda_release_object_evidence_fingerprint": lambda_object.evidence_sha256,
            "lambda_release_object_key": lambda_object.key,
            "lambda_release_object_version_id": lambda_object.version_id,
            "expected_manifest_fingerprint": authority.fingerprint,
            "format": UPDATE_EVIDENCE_FORMAT,
            "observed_change_set_state": "CREATE_COMPLETE/AVAILABLE_AT_CAPTURE_ONLY",
            "preserved_physical_identities": {
                "OperationalStateTable": binding.table_name,
                "PrivateArtifactBucket": binding.bucket_name,
                "PrivateArtifactBucketPolicy": binding.bucket_name,
            },
            "previous_execution_role_arn": previous_role_arn,
            "processed_template_fingerprint": authority.processed_template_fingerprint,
            "recapture_contract": {
                "execute_before": authority.policy_expires_at_text,
                "maximum_review_window_seconds": int(MAX_REVIEW_WINDOW.total_seconds()),
                "require_identical_canonical_descriptor": True,
                "required_immediately_before_execute": True,
            },
            "region": binding.region,
            "reviewed_change_count": len(authority.changes),
            "reviewed_template_fingerprint": authority.target_template_fingerprint,
            "stack_id": binding.stack_id,
            "stack_name": binding.stack_name,
            "verification_scope": "OFFLINE_CAPTURE_ONLY",
        }
    except Phase6RuntimeUpdateError:
        raise
    except Exception:
        raise Phase6RuntimeUpdateError(_GENERIC_ERROR) from None


def _verify_foundation_binding(document: Mapping[str, object]) -> Phase6FoundationBinding:
    if set(document) != _FOUNDATION_BINDING_KEYS:
        raise ValueError
    account_id = document.get("account_id")
    region = document.get("region")
    environment = document.get("environment_name")
    stack_name = document.get("stack_name")
    stack_id = document.get("stack_id")
    if (
        not isinstance(account_id, str)
        or _ACCOUNT_ID.fullmatch(account_id) is None
        or account_id == "0" * 12
        or region != "us-west-2"
        or environment != "dev"
        or not isinstance(environment, str)
        or _ENVIRONMENT.fullmatch(environment) is None
        or stack_name != f"mr-lister-phase6-{environment}"
        or not isinstance(stack_id, str)
        or not _exact_stack_id(stack_id, region, account_id, stack_name)
        or document.get("format") != FOUNDATION_BINDING_FORMAT
        or document.get("foundation_template_fingerprint") != FOUNDATION_TEMPLATE_FINGERPRINT
    ):
        raise ValueError
    table_name = f"mr-lister-phase6-{environment}"
    table_arn = f"arn:aws:dynamodb:{region}:{account_id}:table/{table_name}"
    bucket_name = f"mr-lister-phase6-artifacts-{environment}-{account_id}-{region}"
    bucket_arn = f"arn:aws:s3:::{bucket_name}"
    stream_arn = document.get("operational_state_stream_arn")
    if (
        document.get("operational_state_table_name") != table_name
        or document.get("operational_state_table_arn") != table_arn
        or document.get("artifact_bucket_name") != bucket_name
        or document.get("artifact_bucket_arn") != bucket_arn
        or not isinstance(stream_arn, str)
        or not stream_arn.startswith(f"{table_arn}/stream/")
        or not stream_arn.removeprefix(f"{table_arn}/stream/").strip()
    ):
        raise ValueError
    return Phase6FoundationBinding(
        account_id=account_id,
        region=cast(str, region),
        environment_name=environment,
        stack_name=cast(str, stack_name),
        stack_id=stack_id,
        table_name=table_name,
        table_arn=table_arn,
        stream_arn=stream_arn,
        bucket_name=bucket_name,
        bucket_arn=bucket_arn,
        fingerprint=semantic_fingerprint(document),
    )


def _verify_lambda_release_object(
    *,
    deployment_root: Path,
    artifact_root: Path,
    evidence_path: Path,
    binding: Phase6FoundationBinding,
    target_template: Mapping[str, object],
) -> VerifiedPhase6S3ReleaseObject:
    descriptor = verify_phase6_deployment_artifacts(
        deployment_root,
        artifact_root=artifact_root,
    )
    if not isinstance(descriptor, Mapping):
        raise ValueError
    release_fingerprint = descriptor.get("release_fingerprint")
    components = descriptor.get("components")
    if (
        not isinstance(release_fingerprint, str)
        or _FINGERPRINT.fullmatch(release_fingerprint) is None
        or not isinstance(components, Mapping)
    ):
        raise ValueError
    lambda_record = components.get("lambda")
    if not isinstance(lambda_record, Mapping):
        raise ValueError
    archive_record = lambda_record.get("archive")
    if not isinstance(archive_record, Mapping):
        raise ValueError
    archive_path = artifact_root / LAMBDA_ARCHIVE_FILENAME
    descriptor_path = artifact_root / DEPLOYMENT_DESCRIPTOR_FILENAME
    if (
        archive_path.is_symlink()
        or not archive_path.is_file()
        or descriptor_path.is_symlink()
        or not descriptor_path.is_file()
    ):
        raise ValueError
    archive_bytes = archive_path.read_bytes()
    archive_sha256 = sha256(archive_bytes).hexdigest()
    archive_size = len(archive_bytes)
    if (
        archive_record.get("path") != LAMBDA_ARCHIVE_FILENAME
        or archive_record.get("sha256") != archive_sha256
        or archive_record.get("size_bytes") != archive_size
        or archive_size <= 0
    ):
        raise ValueError
    parameters = target_template.get("Parameters")
    release_parameter = (
        parameters.get("ReleaseFingerprint") if isinstance(parameters, Mapping) else None
    )
    if (
        not isinstance(release_parameter, Mapping)
        or release_parameter.get("Default") != release_fingerprint
        or release_parameter.get("AllowedValues") != [release_fingerprint]
    ):
        raise ValueError
    expectation = Phase6S3ReleaseObjectExpectation(
        account_id=binding.account_id,
        region=binding.region,
        environment=binding.environment_name,
        component="lambda",
        release_fingerprint=release_fingerprint,
        archive_sha256=archive_sha256,
        size_bytes=archive_size,
    )
    verified = verify_phase6_s3_release_object_evidence(
        expectation,
        evidence_path=evidence_path,
    )
    if (
        verified.account_id != binding.account_id
        or verified.region != binding.region
        or verified.environment != binding.environment_name
        or verified.component != "lambda"
        or verified.release_fingerprint != release_fingerprint
        or verified.archive_sha256 != archive_sha256
        or verified.size_bytes != archive_size
        or verified.bucket != binding.bucket_name
        or verified.bucket != expectation.bucket
        or verified.key != expectation.key
        or verified.checksum_sha256_base64 != expectation.checksum_sha256_base64
        or _FINGERPRINT.fullmatch(verified.evidence_sha256) is None
        or verified.evidence_sha256 == "0" * 64
    ):
        raise ValueError
    return verified


def _expected_execution_policy(
    binding: Phase6FoundationBinding,
    lambda_object: VerifiedPhase6S3ReleaseObject,
    *,
    target_template: Mapping[str, object],
) -> Mapping[str, object]:
    bootstrap = _load_mapping(RUNTIME_UPDATE_BOOTSTRAP)
    resources = bootstrap.get("Resources")
    role = resources.get("CoreRuntimeExecutionRole") if isinstance(resources, Mapping) else None
    properties = role.get("Properties") if isinstance(role, Mapping) else None
    policies = properties.get("Policies") if isinstance(properties, Mapping) else None
    if not isinstance(policies, list) or len(policies) != 1 or not isinstance(policies[0], Mapping):
        raise ValueError
    policy = policies[0].get("PolicyDocument")
    if not isinstance(policy, Mapping):
        raise ValueError
    substitutions = {
        "AWS::AccountId": binding.account_id,
        "AWS::Partition": "aws",
        "LambdaArchiveSha256": lambda_object.archive_sha256,
        "LambdaVersionId": lambda_object.version_id,
        "ReleaseFingerprint": lambda_object.release_fingerprint,
    }

    def resolve(value: object) -> object:
        if isinstance(value, list):
            return [resolve(item) for item in value]
        if not isinstance(value, Mapping):
            return value
        if set(value) == {"Ref"}:
            ref = value.get("Ref")
            if not isinstance(ref, str) or ref not in substitutions:
                raise ValueError
            return substitutions[ref]
        if set(value) == {"Fn::Sub"}:
            expression = value.get("Fn::Sub")
            if not isinstance(expression, str):
                raise ValueError

            def replace(match: re.Match[str]) -> str:
                name = match.group(1)
                if name not in substitutions:
                    raise ValueError
                return substitutions[name]

            return re.sub(r"\$\{([^}]+)\}", replace, expression)
        return {str(key): resolve(item) for key, item in value.items()}

    resolved = resolve(policy)
    if not isinstance(resolved, Mapping):
        raise ValueError
    _verify_execution_policy_artifact_binding(
        resolved,
        target_template=target_template,
        binding=binding,
        lambda_object=lambda_object,
    )
    return resolved


def _verify_expected_manifest(
    document: Mapping[str, object],
    binding: Phase6FoundationBinding,
    *,
    target_template: Mapping[str, object],
    target_fingerprint: str,
    lambda_object: VerifiedPhase6S3ReleaseObject,
) -> Phase6UpdateAuthority:
    if set(document) != _MANIFEST_KEYS:
        raise ValueError
    role = document.get("execution_role_arn")
    role_policy_name = document.get("execution_role_policy_name")
    role_policy_fingerprint = document.get("execution_role_policy_fingerprint")
    deployer_role = document.get("deployer_role_arn")
    deployer_policy_name = document.get("deployer_policy_name")
    deployer_session = document.get("deployer_session_name")
    name = document.get("change_set_name")
    description = document.get("change_set_description")
    client_token = document.get("client_token")
    fingerprint = document.get("target_template_fingerprint")
    processed_fingerprint = document.get("processed_template_fingerprint")
    lambda_evidence_fingerprint = document.get("lambda_release_object_evidence_fingerprint")
    template_url = document.get("template_url")
    expiry_text = document.get("policy_expires_at")
    expected_role = (
        f"arn:aws:iam::{binding.account_id}:role/"
        f"mr-lister-phase6-runtime-cfn-{binding.environment_name}"
    )
    expected_deployer_role = (
        f"arn:aws:iam::{binding.account_id}:role/"
        f"mr-lister-phase6-runtime-update-deployer-{binding.environment_name}"
    )
    expected_name = f"{binding.stack_name}-runtime-update-{target_fingerprint[:12]}"
    expected_description = f"Mr Lister Phase 6 reviewed UPDATE {target_fingerprint}"
    expected_client_token = f"phase6-{target_fingerprint[:32]}"
    expected_session = f"phase6-update-{target_fingerprint[:12]}"
    if (
        document.get("format") != UPDATE_MANIFEST_FORMAT
        or document.get("account_id") != binding.account_id
        or document.get("region") != binding.region
        or document.get("environment_name") != binding.environment_name
        or document.get("stack_name") != binding.stack_name
        or document.get("stack_id") != binding.stack_id
        or document.get("foundation_binding_fingerprint") != binding.fingerprint
        or role != expected_role
        or role_policy_name != f"mr-lister-phase6-runtime-execution-{binding.environment_name}"
        or not isinstance(role_policy_fingerprint, str)
        or _FINGERPRINT.fullmatch(role_policy_fingerprint) is None
        or deployer_role != expected_deployer_role
        or deployer_policy_name
        != f"mr-lister-phase6-runtime-update-deployer-{binding.environment_name}"
        or deployer_session != expected_session
        or name != expected_name
        or not isinstance(name, str)
        or _CHANGE_SET_NAME.fullmatch(name) is None
        or description != expected_description
        or client_token != expected_client_token
        or not isinstance(client_token, str)
        or _CLIENT_TOKEN.fullmatch(client_token) is None
        or not isinstance(fingerprint, str)
        or _FINGERPRINT.fullmatch(fingerprint) is None
        or fingerprint != target_fingerprint
        or not isinstance(processed_fingerprint, str)
        or _FINGERPRINT.fullmatch(processed_fingerprint) is None
        or lambda_evidence_fingerprint != lambda_object.evidence_sha256
        or not isinstance(template_url, str)
        or not isinstance(expiry_text, str)
    ):
        raise ValueError
    expiry = _aws_utc_datetime(expiry_text)
    parameters = _verify_locked_target_parameters(
        document.get("parameters"),
        target_template=target_template,
    )
    tags = _string_mapping(document.get("tags"))
    if parameters.get("EnvironmentName") != binding.environment_name or tags != {
        "DeploymentClass": "RUNTIME_UPDATE",
        "Environment": binding.environment_name,
        "Project": "MrLister",
    }:
        raise ValueError
    release_fingerprint = parameters.get("ReleaseFingerprint")
    if (
        not isinstance(release_fingerprint, str)
        or _FINGERPRINT.fullmatch(release_fingerprint) is None
        or not _valid_template_url(
            template_url,
            binding,
            release_fingerprint=release_fingerprint,
        )
    ):
        raise ValueError
    capabilities = _string_tuple(document.get("capabilities"))
    if capabilities != ("CAPABILITY_NAMED_IAM",):
        raise ValueError
    notification_arns = _string_tuple(document.get("notification_arns"))
    if notification_arns:
        raise ValueError
    rollback = document.get("rollback_configuration")
    deployment = document.get("deployment_config")
    if not isinstance(rollback, Mapping) or not isinstance(deployment, Mapping):
        raise ValueError
    if rollback != {} or deployment != {"DisableRollback": False, "Mode": "STANDARD"}:
        raise ValueError
    changes = _manifest_changes(document.get("changes"), binding)
    if not changes:
        raise ValueError
    return Phase6UpdateAuthority(
        execution_role_arn=cast(str, role),
        execution_role_policy_name=cast(str, role_policy_name),
        execution_role_policy_fingerprint=role_policy_fingerprint,
        deployer_role_arn=cast(str, deployer_role),
        deployer_policy_name=cast(str, deployer_policy_name),
        deployer_session_name=cast(str, deployer_session),
        change_set_name=name,
        change_set_description=cast(str, description),
        client_token=client_token,
        target_template_fingerprint=fingerprint,
        processed_template_fingerprint=processed_fingerprint,
        lambda_release_object_evidence_fingerprint=cast(str, lambda_evidence_fingerprint),
        template_url=template_url,
        policy_expires_at=expiry,
        policy_expires_at_text=expiry_text,
        parameters=parameters,
        tags=tags,
        capabilities=capabilities,
        notification_arns=notification_arns,
        rollback_configuration=rollback,
        deployment_config=deployment,
        changes=changes,
        fingerprint=semantic_fingerprint(document),
    )


def _verify_pre_update_stack(
    observation: Mapping[str, object], binding: Phase6FoundationBinding
) -> str:
    if set(observation) != {"Stacks"}:
        raise ValueError
    stacks = observation.get("Stacks")
    if not isinstance(stacks, list) or len(stacks) != 1 or not isinstance(stacks[0], Mapping):
        raise ValueError
    stack = cast(Mapping[str, object], stacks[0])
    if set(stack) != _PRE_STACK_KEYS:
        raise ValueError
    creation = _aws_utc_datetime(stack.get("CreationTime"))
    updated = _aws_utc_datetime(stack.get("LastUpdatedTime"))
    last_operations = stack.get("LastOperations")
    change_set_id = stack.get("ChangeSetId")
    foundation_role = (
        f"arn:aws:iam::{binding.account_id}:role/"
        f"mr-lister-phase6-foundation-cfn-{binding.environment_name}"
    )
    expected_foundation_name = (
        f"{binding.stack_name}-foundation-create-{FOUNDATION_TEMPLATE_FINGERPRINT[:12]}"
    )
    if (
        stack.get("StackId") != binding.stack_id
        or stack.get("StackName") != binding.stack_name
        or stack.get("RoleARN") != foundation_role
        or stack.get("StackStatus") != "CREATE_COMPLETE"
        or stack.get("Description") != "Mr Lister Phase 6 create-only durable foundation"
        or stack.get("EnableTerminationProtection") is not True
        or stack.get("DisableRollback") is not True
        or stack.get("DeploymentConfig") != {"DisableRollback": True, "Mode": "STANDARD"}
        or stack.get("NotificationARNs") != []
        or stack.get("RollbackConfiguration") != {}
        or stack.get("DriftInformation")
        not in ({"StackDriftStatus": "NOT_CHECKED"}, {"StackDriftStatus": "IN_SYNC"})
        or updated <= creation
        or not isinstance(change_set_id, str)
        or not _exact_change_set_id(
            change_set_id, binding.region, binding.account_id, expected_foundation_name
        )
        or _key_value_records(stack.get("Parameters"), "ParameterKey", "ParameterValue")
        != {"EnvironmentName": binding.environment_name}
        or _key_value_records(stack.get("Tags"), "Key", "Value")
        != {
            "DeploymentClass": "FOUNDATION_ONLY",
            "Environment": binding.environment_name,
            "Project": "MrLister",
        }
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
        or not isinstance(last_operations, list)
        or len(last_operations) != 1
        or not isinstance(last_operations[0], Mapping)
        or set(last_operations[0]) != {"OperationId", "OperationType"}
        or last_operations[0].get("OperationType") != "CREATE_STACK"
        or not isinstance(last_operations[0].get("OperationId"), str)
        or _UUID.fullmatch(cast(str, last_operations[0]["OperationId"])) is None
    ):
        raise ValueError
    return foundation_role


def _verify_pre_update_resources(
    observation: Mapping[str, object], binding: Phase6FoundationBinding
) -> None:
    if set(observation) != {"StackResourceSummaries"}:
        raise ValueError
    resources = observation.get("StackResourceSummaries")
    if not isinstance(resources, list) or len(resources) != 3:
        raise ValueError
    expected = {
        "OperationalStateTable": ("AWS::DynamoDB::Table", binding.table_name),
        "PrivateArtifactBucket": ("AWS::S3::Bucket", binding.bucket_name),
        "PrivateArtifactBucketPolicy": ("AWS::S3::BucketPolicy", binding.bucket_name),
    }
    actual: dict[str, tuple[str, str]] = {}
    for resource in resources:
        if not isinstance(resource, Mapping) or set(resource) != _RESOURCE_SUMMARY_KEYS:
            raise ValueError
        logical_id = resource.get("LogicalResourceId")
        resource_type = resource.get("ResourceType")
        physical_id = resource.get("PhysicalResourceId")
        if (
            not isinstance(logical_id, str)
            or logical_id in actual
            or not isinstance(resource_type, str)
            or not isinstance(physical_id, str)
            or resource.get("ResourceStatus") != "CREATE_COMPLETE"
            or resource.get("DriftInformation")
            not in (
                {"StackResourceDriftStatus": "NOT_CHECKED"},
                {"StackResourceDriftStatus": "IN_SYNC"},
            )
        ):
            raise ValueError
        _aws_utc_datetime(resource.get("LastUpdatedTimestamp"))
        actual[logical_id] = (resource_type, physical_id)
    if actual != expected:
        raise ValueError


def _verify_template_observation(
    observation: Mapping[str, object], *, expected_fingerprint: str
) -> None:
    if set(observation) != {"StagesAvailable", "TemplateBody"}:
        raise ValueError
    stages = observation.get("StagesAvailable")
    body = observation.get("TemplateBody")
    if (
        not isinstance(stages, list)
        or any(not isinstance(stage, str) for stage in stages)
        or sorted(stages) != ["Original", "Processed"]
        or len(set(cast(list[str], stages))) != 2
        or not isinstance(body, Mapping)
        or semantic_fingerprint(body) != expected_fingerprint
    ):
        raise ValueError


def _verify_update_change_set(
    observation: Mapping[str, object],
    binding: Phase6FoundationBinding,
    authority: Phase6UpdateAuthority,
) -> tuple[str, datetime, Mapping[str, object]]:
    # DescribeChangeSet returns neither ChangeSetType nor RoleARN. Those fields are proven by the
    # successful CloudTrail CreateChangeSet event instead of being inferred or synthesized.
    if not _CHANGE_SET_REQUIRED_KEYS <= set(observation) <= _CHANGE_SET_KEYS:
        raise ValueError
    normalized_observation = {key: observation.get(key) for key in sorted(_CHANGE_SET_KEYS)}
    change_set_id = observation.get("ChangeSetId")
    if (
        observation.get("StackId") != binding.stack_id
        or observation.get("StackName") != binding.stack_name
        or observation.get("ChangeSetName") != authority.change_set_name
        or observation.get("Description") != authority.change_set_description
        or observation.get("Status") != "CREATE_COMPLETE"
        or observation.get("StatusReason") is not None
        or observation.get("ExecutionStatus") != "AVAILABLE"
        or observation.get("IncludeNestedStacks") is not False
        or observation.get("ParentChangeSetId") is not None
        or observation.get("RootChangeSetId") is not None
        or observation.get("ImportExistingResources") is not None
        or observation.get("OnStackFailure") is not None
        or observation.get("DeploymentMode") is not None
        or observation.get("StackDriftStatus") is not None
        or observation.get("DeploymentConfig") != authority.deployment_config
        or observation.get("RollbackConfiguration") != authority.rollback_configuration
        or _string_tuple(observation.get("Capabilities")) != authority.capabilities
        or _string_tuple(observation.get("NotificationARNs")) != authority.notification_arns
        or _key_value_records(observation.get("Parameters"), "ParameterKey", "ParameterValue")
        != authority.parameters
        or _key_value_records(observation.get("Tags"), "Key", "Value") != authority.tags
        or not isinstance(change_set_id, str)
        or not _exact_change_set_id(
            change_set_id, binding.region, binding.account_id, authority.change_set_name
        )
    ):
        raise ValueError
    creation_time = _aws_utc_datetime(observation.get("CreationTime"))
    actual_changes = _observed_changes(observation.get("Changes"), binding)
    if actual_changes != authority.changes:
        raise ValueError
    return change_set_id, creation_time, normalized_observation


def _verify_role(
    observation: Mapping[str, object],
    *,
    expected_arn: str,
    expected_name: str,
    expected_tags: Mapping[str, str],
    expected_trust: Mapping[str, object],
    expires_at: datetime | None,
) -> RoleAuthority:
    if set(observation) != {"Role"}:
        raise ValueError
    role = observation.get("Role")
    if (
        not isinstance(role, Mapping)
        or not _ROLE_KEYS_REQUIRED <= set(role) <= _ROLE_KEYS_ALLOWED
        or "PermissionsBoundary" in role
        or role.get("Path") != "/"
        or role.get("RoleName") != expected_name
        or role.get("Arn") != expected_arn
        or role.get("MaxSessionDuration") != 3600
        or role.get("AssumeRolePolicyDocument") != expected_trust
        or _key_value_records(role.get("Tags"), "Key", "Value") != expected_tags
    ):
        raise ValueError
    role_id = role.get("RoleId")
    if not isinstance(role_id, str) or _ROLE_ID.fullmatch(role_id) is None:
        raise ValueError
    create_date = _aws_utc_datetime(role.get("CreateDate"))
    if expires_at is not None and create_date >= expires_at:
        raise ValueError
    normalized = {key: value for key, value in role.items() if key != "RoleLastUsed"}
    return RoleAuthority(arn=expected_arn, role_id=role_id, normalized=normalized)


def _verify_exact_role_policy_set(
    *,
    role_name: str,
    policy_name: str,
    inline_list: Mapping[str, object],
    attached_list: Mapping[str, object],
    policy_observation: Mapping[str, object],
) -> Mapping[str, object]:
    if inline_list != {"PolicyNames": [policy_name]}:
        raise ValueError
    if attached_list != {"AttachedPolicies": []}:
        raise ValueError
    if set(policy_observation) != {"PolicyDocument", "PolicyName", "RoleName"}:
        raise ValueError
    policy = policy_observation.get("PolicyDocument")
    if (
        policy_observation.get("RoleName") != role_name
        or policy_observation.get("PolicyName") != policy_name
        or not isinstance(policy, Mapping)
        or policy.get("Version") != "2012-10-17"
        or not isinstance(policy.get("Statement"), list)
        or not policy.get("Statement")
    ):
        raise ValueError
    return policy


def _verify_execution_policy_artifact_binding(
    policy: Mapping[str, object],
    *,
    target_template: Mapping[str, object],
    binding: Phase6FoundationBinding,
    lambda_object: VerifiedPhase6S3ReleaseObject,
) -> None:
    resources = target_template.get("Resources")
    parameters = target_template.get("Parameters")
    if not isinstance(resources, Mapping) or not isinstance(parameters, Mapping):
        raise ValueError
    release_definition = parameters.get("ReleaseFingerprint")
    if not isinstance(release_definition, Mapping):
        raise ValueError
    release_fingerprint = release_definition.get("Default")
    if (
        not isinstance(release_fingerprint, str)
        or _FINGERPRINT.fullmatch(release_fingerprint) is None
    ):
        raise ValueError
    code_locations: list[Mapping[str, object]] = []
    for resource in resources.values():
        if not isinstance(resource, Mapping) or resource.get("Type") != "AWS::Serverless::Function":
            continue
        properties = resource.get("Properties")
        if not isinstance(properties, Mapping):
            raise ValueError
        code_uri = properties.get("CodeUri")
        if not isinstance(code_uri, Mapping) or set(code_uri) != {"Bucket", "Key", "Version"}:
            raise ValueError
        code_locations.append(code_uri)
    if not code_locations or any(location != code_locations[0] for location in code_locations[1:]):
        raise ValueError
    code = code_locations[0]
    key = code.get("Key")
    version_id = code.get("Version")
    if (
        release_fingerprint != lambda_object.release_fingerprint
        or code.get("Bucket") != binding.bucket_name
        or code.get("Bucket") != lambda_object.bucket
        or key != lambda_object.key
        or version_id != lambda_object.version_id
    ):
        raise ValueError
    statements = policy.get("Statement")
    if not isinstance(statements, list):
        raise ValueError
    matches = [
        statement
        for statement in statements
        if isinstance(statement, Mapping)
        and statement.get("Sid") == "ReadOnlyExactLambdaDeploymentArchiveVersion"
    ]
    expected = {
        "Action": "s3:GetObjectVersion",
        "Condition": {"StringEquals": {"s3:VersionId": lambda_object.version_id}},
        "Effect": "Allow",
        "Resource": f"arn:aws:s3:::{lambda_object.bucket}/{lambda_object.key}",
        "Sid": "ReadOnlyExactLambdaDeploymentArchiveVersion",
    }
    if matches != [expected]:
        raise ValueError


def _verify_caller_identity(
    observation: Mapping[str, object],
    binding: Phase6FoundationBinding,
    authority: Phase6UpdateAuthority,
    deployer_role: RoleAuthority,
) -> None:
    if set(observation) != {"Account", "Arn", "UserId"}:
        raise ValueError
    expected_arn = (
        f"arn:aws:sts::{binding.account_id}:assumed-role/"
        f"mr-lister-phase6-runtime-update-deployer-{binding.environment_name}/"
        f"{authority.deployer_session_name}"
    )
    if (
        observation.get("Account") != binding.account_id
        or observation.get("Arn") != expected_arn
        or observation.get("UserId") != f"{deployer_role.role_id}:{authority.deployer_session_name}"
    ):
        raise ValueError


def _verify_cloudtrail_event(
    observation: Mapping[str, object],
    caller_identity: Mapping[str, object],
    binding: Phase6FoundationBinding,
    authority: Phase6UpdateAuthority,
    deployer_role: RoleAuthority,
    *,
    change_set_id: str,
    change_set_time: datetime,
) -> CloudTrailAuthority:
    if set(observation) != {"Events"}:
        raise ValueError
    events = observation.get("Events")
    if not isinstance(events, list) or len(events) != 1 or not isinstance(events[0], Mapping):
        raise ValueError
    wrapper = cast(Mapping[str, object], events[0])
    if set(wrapper) != {
        "AccessKeyId",
        "CloudTrailEvent",
        "EventId",
        "EventName",
        "EventSource",
        "EventTime",
        "ReadOnly",
        "Resources",
        "Username",
    }:
        raise ValueError
    raw_event = wrapper.get("CloudTrailEvent")
    if not isinstance(raw_event, str):
        raise ValueError
    event = json.loads(raw_event)
    if (
        not isinstance(event, Mapping)
        or not _CLOUDTRAIL_EVENT_KEYS_REQUIRED <= set(event) <= _CLOUDTRAIL_EVENT_KEYS_ALLOWED
        or any(key in event for key in ("errorCode", "errorMessage"))
    ):
        raise ValueError
    event_id = event.get("eventID")
    request_id = event.get("requestID")
    event_time = _aws_utc_datetime(event.get("eventTime"))
    wrapper_time = _aws_utc_datetime(wrapper.get("EventTime"))
    if (
        not isinstance(event_id, str)
        or _UUID.fullmatch(event_id) is None
        or not isinstance(request_id, str)
        or _UUID.fullmatch(request_id) is None
        or wrapper.get("EventId") != event_id
        or wrapper.get("EventName") != "CreateChangeSet"
        or wrapper.get("EventSource") != "cloudformation.amazonaws.com"
        or wrapper.get("ReadOnly") != "false"
        or wrapper.get("Username") != authority.deployer_session_name
        or wrapper_time != event_time
        or event.get("eventSource") != "cloudformation.amazonaws.com"
        or event.get("eventName") != "CreateChangeSet"
        or event.get("awsRegion") != binding.region
        or event.get("eventType") != "AwsApiCall"
        or event.get("eventCategory") != "Management"
        or event.get("managementEvent") is not True
        or event.get("readOnly") is not False
        or event.get("recipientAccountId") != binding.account_id
        or not isinstance(event.get("eventVersion"), str)
        or not isinstance(event.get("sourceIPAddress"), str)
        or not isinstance(event.get("userAgent"), str)
        or event_time > datetime.now(UTC) + MAX_FUTURE_EVENT_SKEW
        or abs(change_set_time - event_time) > MAX_EVENT_TO_CHANGE_SET_DELAY
    ):
        raise ValueError
    if "sessionCredentialFromConsole" in event and event["sessionCredentialFromConsole"] not in {
        "true",
        "false",
    }:
        raise ValueError
    _verify_tls_details(event.get("tlsDetails"))
    _verify_cloudtrail_resources(wrapper.get("Resources"), binding, change_set_id)
    access_key = _verify_cloudtrail_identity(
        event.get("userIdentity"), caller_identity, binding, authority, deployer_role
    )
    if wrapper.get("AccessKeyId") != access_key:
        raise ValueError
    _verify_cloudtrail_request(event.get("requestParameters"), binding, authority)
    if event.get("responseElements") != {"id": change_set_id, "stackId": binding.stack_id}:
        raise ValueError
    normalized = {
        "event": event,
        "wrapper": {key: value for key, value in wrapper.items() if key != "CloudTrailEvent"},
    }
    return CloudTrailAuthority(event_id=event_id, event_time=event_time, normalized=normalized)


def _verify_cloudtrail_identity(
    value: object,
    caller_identity: Mapping[str, object],
    binding: Phase6FoundationBinding,
    authority: Phase6UpdateAuthority,
    deployer_role: RoleAuthority,
) -> str:
    if not isinstance(value, Mapping) or set(value) != {
        "accessKeyId",
        "accountId",
        "arn",
        "principalId",
        "sessionContext",
        "type",
    }:
        raise ValueError
    access_key = value.get("accessKeyId")
    if (
        value.get("type") != "AssumedRole"
        or value.get("accountId") != binding.account_id
        or value.get("arn") != caller_identity.get("Arn")
        or value.get("principalId") != caller_identity.get("UserId")
        or not isinstance(access_key, str)
        or _ACCESS_KEY_ID.fullmatch(access_key) is None
    ):
        raise ValueError
    context = value.get("sessionContext")
    if not isinstance(context, Mapping) or set(context) != {"attributes", "sessionIssuer"}:
        raise ValueError
    issuer = context.get("sessionIssuer")
    attributes = context.get("attributes")
    if (
        not isinstance(issuer, Mapping)
        or set(issuer) != {"accountId", "arn", "principalId", "type", "userName"}
        or issuer.get("type") != "Role"
        or issuer.get("accountId") != binding.account_id
        or issuer.get("arn") != authority.deployer_role_arn
        or issuer.get("principalId") != deployer_role.role_id
        or issuer.get("userName")
        != f"mr-lister-phase6-runtime-update-deployer-{binding.environment_name}"
        or not isinstance(attributes, Mapping)
        or set(attributes) != {"creationDate", "mfaAuthenticated"}
        or attributes.get("mfaAuthenticated") not in {"true", "false"}
    ):
        raise ValueError
    _aws_utc_datetime(attributes.get("creationDate"))
    return access_key


def _verify_cloudtrail_request(
    value: object, binding: Phase6FoundationBinding, authority: Phase6UpdateAuthority
) -> None:
    if (
        not isinstance(value, Mapping)
        or not _CLOUDTRAIL_REQUEST_REQUIRED <= set(value) <= _CLOUDTRAIL_REQUEST_ALLOWED
        or value.get("stackName") != binding.stack_id
        or value.get("templateURL") != authority.template_url
        or value.get("roleARN") != authority.execution_role_arn
        or value.get("changeSetName") != authority.change_set_name
        or value.get("description") != authority.change_set_description
        or value.get("clientToken") != authority.client_token
        or value.get("changeSetType") != "UPDATE"
        or value.get("includeNestedStacks") is not False
        or _string_tuple(value.get("capabilities")) != authority.capabilities
        or _cloudtrail_parameters(value.get("parameters"), authority.parameters)
        != authority.parameters
        or _key_value_records(value.get("tags"), "key", "value") != authority.tags
    ):
        raise ValueError
    optional_expected = {
        "deploymentConfig": {"disableRollback": False, "mode": "STANDARD"},
        "importExistingResources": False,
        "notificationARNs": [],
        "rollbackConfiguration": {},
    }
    for key, expected in optional_expected.items():
        if key in value and value.get(key) != expected:
            raise ValueError


def _cloudtrail_parameters(value: object, expected: Mapping[str, str]) -> dict[str, str]:
    """Join CloudTrail's redacted parameter-key records to DescribeChangeSet values."""

    if not isinstance(value, list):
        raise ValueError
    result: dict[str, str] = {}
    for item in value:
        if not isinstance(item, Mapping) or not {"parameterKey"} <= set(item) <= {
            "parameterKey",
            "parameterValue",
        }:
            raise ValueError
        key = item.get("parameterKey")
        if not isinstance(key, str) or key not in expected or key in result:
            raise ValueError
        if "parameterValue" in item and item.get("parameterValue") != expected[key]:
            raise ValueError
        result[key] = expected[key]
    return result


def _verify_cloudtrail_resources(
    value: object, binding: Phase6FoundationBinding, change_set_id: str
) -> None:
    if not isinstance(value, list):
        raise ValueError
    allowed = {
        ("AWS::CloudFormation::Stack", binding.stack_id),
        ("AWS::CloudFormation::ChangeSet", change_set_id),
    }
    observed: set[tuple[str, str]] = set()
    for item in value:
        if not isinstance(item, Mapping) or set(item) != {"ResourceName", "ResourceType"}:
            raise ValueError
        resource_type = item.get("ResourceType")
        resource_name = item.get("ResourceName")
        if not isinstance(resource_type, str) or not isinstance(resource_name, str):
            raise ValueError
        pair = (resource_type, resource_name)
        if pair not in allowed or pair in observed:
            raise ValueError
        observed.add(pair)


def _verify_tls_details(value: object) -> None:
    if value is None:
        return
    if not isinstance(value, Mapping) or set(value) != {
        "cipherSuite",
        "clientProvidedHostHeader",
        "tlsVersion",
    }:
        raise ValueError
    if any(not isinstance(item, str) or not item for item in value.values()):
        raise ValueError


def _verify_review_window(
    authority: Phase6UpdateAuthority, event_time: datetime, change_set_time: datetime
) -> None:
    latest_authority_time = max(event_time, change_set_time)
    if (
        authority.policy_expires_at <= latest_authority_time
        or authority.policy_expires_at > event_time + MAX_REVIEW_WINDOW
        or authority.policy_expires_at <= datetime.now(UTC)
    ):
        raise ValueError


def _expected_deployer_policy(
    binding: Phase6FoundationBinding, authority: Phase6UpdateAuthority
) -> Mapping[str, object]:
    template = urlsplit(authority.template_url)
    template_key = unquote(template.path).removeprefix("/")
    template_version = parse_qs(template.query, keep_blank_values=True)["versionId"][0]
    template_object_arn = f"arn:aws:s3:::{binding.bucket_name}/{template_key}"
    change_set_arn = (
        f"arn:aws:cloudformation:{binding.region}:{binding.account_id}:"
        f"changeSet/{authority.change_set_name}/*"
    )
    return {
        "Statement": [
            {
                "Action": "cloudformation:CreateChangeSet",
                "Condition": {
                    "DateLessThan": {"aws:CurrentTime": authority.policy_expires_at_text},
                    "ForAllValues:StringEquals": {
                        "aws:TagKeys": ["DeploymentClass", "Environment", "Project"]
                    },
                    "StringEquals": {
                        "aws:RequestTag/DeploymentClass": "RUNTIME_UPDATE",
                        "aws:RequestTag/Environment": binding.environment_name,
                        "aws:RequestTag/Project": "MrLister",
                        "cloudformation:ChangeSetName": authority.change_set_name,
                        "cloudformation:RoleArn": authority.execution_role_arn,
                        "cloudformation:TemplateUrl": authority.template_url,
                    },
                },
                "Effect": "Allow",
                "Resource": [
                    binding.stack_id,
                    (
                        f"arn:aws:cloudformation:{binding.region}:aws:transform/"
                        "Serverless-2016-10-31"
                    ),
                ],
                "Sid": "CreateExactReviewedRuntimeUpdate",
            },
            {
                "Action": "iam:PassRole",
                "Condition": {
                    "DateLessThan": {"aws:CurrentTime": authority.policy_expires_at_text},
                    "StringEquals": {"iam:PassedToService": "cloudformation.amazonaws.com"},
                },
                "Effect": "Allow",
                "Resource": authority.execution_role_arn,
                "Sid": "PassExactRuntimeExecutionRole",
            },
            {
                "Action": "s3:GetObjectVersion",
                "Condition": {
                    "DateLessThan": {"aws:CurrentTime": authority.policy_expires_at_text},
                    "StringEquals": {"s3:VersionId": template_version},
                },
                "Effect": "Allow",
                "Resource": template_object_arn,
                "Sid": "ReadOnlyExactReviewedTemplateVersion",
            },
            {
                "Action": ["cloudformation:DescribeChangeSet", "cloudformation:GetTemplate"],
                "Condition": {
                    "DateLessThan": {"aws:CurrentTime": authority.policy_expires_at_text}
                },
                "Effect": "Allow",
                "Resource": [binding.stack_id, change_set_arn],
                "Sid": "ReadOnlyExactReviewedChangeSet",
            },
            {
                "Action": ["cloudformation:DescribeStacks", "cloudformation:ListStackResources"],
                "Condition": {
                    "DateLessThan": {"aws:CurrentTime": authority.policy_expires_at_text}
                },
                "Effect": "Allow",
                "Resource": binding.stack_id,
                "Sid": "ReadOnlyExactFoundationStack",
            },
            {
                "Action": [
                    "iam:GetRole",
                    "iam:GetRolePolicy",
                    "iam:ListAttachedRolePolicies",
                    "iam:ListRolePolicies",
                ],
                "Condition": {
                    "DateLessThan": {"aws:CurrentTime": authority.policy_expires_at_text}
                },
                "Effect": "Allow",
                "Resource": [authority.deployer_role_arn, authority.execution_role_arn],
                "Sid": "ReadBackOnlyExactDeploymentRoles",
            },
            {
                "Action": "cloudtrail:LookupEvents",
                "Condition": {
                    "DateLessThan": {"aws:CurrentTime": authority.policy_expires_at_text},
                    "StringEquals": {"aws:RequestedRegion": binding.region},
                },
                "Effect": "Allow",
                "Resource": "*",
                "Sid": "ReadOnlyRegionalCreateEventHistory",
            },
        ],
        "Version": "2012-10-17",
    }


def _service_trust(service: str) -> Mapping[str, object]:
    return {
        "Statement": [
            {
                "Action": "sts:AssumeRole",
                "Effect": "Allow",
                "Principal": {"Service": service},
            }
        ],
        "Version": "2012-10-17",
    }


def _user_trust(user_arn: str) -> Mapping[str, object]:
    return {
        "Statement": [
            {
                "Action": "sts:AssumeRole",
                "Effect": "Allow",
                "Principal": {"AWS": user_arn},
            }
        ],
        "Version": "2012-10-17",
    }


def _manifest_changes(
    value: object, binding: Phase6FoundationBinding
) -> dict[str, Mapping[str, object]]:
    if not isinstance(value, list):
        raise ValueError
    changes: dict[str, Mapping[str, object]] = {}
    for item in value:
        if not isinstance(item, Mapping) or set(item) != _MANIFEST_CHANGE_KEYS:
            raise ValueError
        normalized = _validate_normalized_change(item, binding)
        logical_id = cast(str, normalized["logical_resource_id"])
        if logical_id in changes:
            raise ValueError
        changes[logical_id] = normalized
    return changes


def _observed_changes(
    value: object, binding: Phase6FoundationBinding
) -> dict[str, Mapping[str, object]]:
    if not isinstance(value, list):
        raise ValueError
    changes: dict[str, Mapping[str, object]] = {}
    for item in value:
        if not isinstance(item, Mapping) or set(item) != {"ResourceChange", "Type"}:
            raise ValueError
        if item.get("Type") != "Resource":
            raise ValueError
        resource = item.get("ResourceChange")
        if not isinstance(resource, Mapping):
            raise ValueError
        action = resource.get("Action")
        allowed_keys = set(_RESOURCE_CHANGE_BASE_KEYS) | {"AfterContext", "BeforeContext"}
        required_keys = set(_RESOURCE_CHANGE_BASE_KEYS)
        if action == "Modify":
            required_keys |= {
                "AfterContext",
                "BeforeContext",
                "PhysicalResourceId",
                "Replacement",
            }
            allowed_keys |= {"PhysicalResourceId", "Replacement"}
        if not required_keys <= set(resource) <= allowed_keys:
            raise ValueError
        normalized = {
            "action": action,
            "after_context": _decode_context(resource.get("AfterContext")),
            "before_context": _decode_context(resource.get("BeforeContext")),
            "details": resource.get("Details"),
            "logical_resource_id": resource.get("LogicalResourceId"),
            "physical_resource_id": resource.get("PhysicalResourceId"),
            "replacement": resource.get("Replacement"),
            "resource_type": resource.get("ResourceType"),
            "scope": resource.get("Scope"),
        }
        normalized = _validate_normalized_change(normalized, binding)
        logical_id = cast(str, normalized["logical_resource_id"])
        if logical_id in changes:
            raise ValueError
        changes[logical_id] = normalized
    return changes


def _validate_normalized_change(
    item: Mapping[str, object], binding: Phase6FoundationBinding
) -> Mapping[str, object]:
    action = item.get("action")
    logical_id = item.get("logical_resource_id")
    resource_type = item.get("resource_type")
    physical_id = item.get("physical_resource_id")
    replacement = item.get("replacement")
    before_context = _normalize_context(item.get("before_context"))
    after_context = _normalize_context(item.get("after_context"))
    if (
        action not in {"Add", "Modify"}
        or not isinstance(logical_id, str)
        or not logical_id.strip()
        or not isinstance(resource_type, str)
        or not resource_type.startswith("AWS::")
    ):
        raise ValueError
    details = _normalize_details(item.get("details"))
    scope = _normalize_scope(item.get("scope"))
    detail_scopes = {
        cast(str, cast(Mapping[str, object], detail["Target"])["Attribute"]) for detail in details
    }
    if set(scope) != detail_scopes:
        raise ValueError
    if action == "Add":
        if (
            physical_id is not None
            or replacement is not None
            or scope
            or details
            or before_context is not None
        ):
            raise ValueError
    elif (
        not isinstance(physical_id, str)
        or not physical_id.strip()
        or replacement not in {"False", "Conditional", "True"}
        or not scope
        or not details
        or before_context is None
        or after_context is None
    ):
        raise ValueError
    if action == "Modify" and logical_id not in FOUNDATION_RESOURCE_TYPES:
        raise ValueError
    if logical_id in FOUNDATION_RESOURCE_TYPES:
        expected_physical = (
            binding.table_name if logical_id == "OperationalStateTable" else binding.bucket_name
        )
        if (
            action != "Modify"
            or resource_type != FOUNDATION_RESOURCE_TYPES[logical_id]
            or physical_id != expected_physical
            or replacement != "False"
            or any(
                cast(Mapping[str, object], detail["Target"]).get("RequiresRecreation") != "Never"
                for detail in details
            )
        ):
            raise ValueError
    return {
        "action": action,
        "after_context": after_context,
        "before_context": before_context,
        "details": details,
        "logical_resource_id": logical_id,
        "physical_resource_id": physical_id,
        "replacement": replacement,
        "resource_type": resource_type,
        "scope": scope,
    }


def _normalize_details(value: object) -> list[Mapping[str, object]]:
    if not isinstance(value, list):
        raise ValueError
    result: list[Mapping[str, object]] = []
    for detail in value:
        if (
            not isinstance(detail, Mapping)
            or not _DETAIL_REQUIRED_KEYS <= set(detail) <= _DETAIL_KEYS
            or detail.get("ChangeSource") not in _CHANGE_SOURCES
            or detail.get("Evaluation") != "Static"
        ):
            raise ValueError
        causing = detail.get("CausingEntity")
        if "CausingEntity" in detail and (not isinstance(causing, str) or not causing.strip()):
            raise ValueError
        target = detail.get("Target")
        if (
            not isinstance(target, Mapping)
            or not {"Attribute", "RequiresRecreation"} <= set(target) <= _TARGET_KEYS
        ):
            raise ValueError
        attribute = target.get("Attribute")
        name = target.get("Name")
        path = target.get("Path")
        if (
            attribute not in _SCOPES
            or target.get("RequiresRecreation") not in {"Never", "Conditionally", "Always"}
            or ("Name" in target and (not isinstance(name, str) or not name.strip()))
            or ("Path" in target and (not isinstance(path, str) or not path.startswith("/")))
            or (
                "AttributeChangeType" in target
                and target.get("AttributeChangeType") not in {"Add", "Remove", "Modify"}
            )
            or "BeforeValueFrom" in target
            or "AfterValueFrom" in target
            or "Drift" in target
            or any(
                key in target and not isinstance(target.get(key), str)
                for key in ("BeforeValue", "AfterValue")
            )
        ):
            raise ValueError
        result.append({**detail, "Target": dict(target)})
    return sorted(result, key=_canonical_json)


def _normalize_scope(value: object) -> list[str]:
    result = list(_string_tuple(value))
    if not set(result) <= _SCOPES:
        raise ValueError
    return result


def _decode_context(value: object) -> Mapping[str, object] | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError
    decoded = json.loads(value)
    if not isinstance(decoded, Mapping):
        raise ValueError
    return decoded


def _normalize_context(value: object) -> Mapping[str, object] | None:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise ValueError
    decoded = json.loads(_canonical_json(value))
    if not isinstance(decoded, Mapping):
        raise ValueError
    return decoded


def _verify_target_foundation_identities(template: Mapping[str, object]) -> None:
    resources = template.get("Resources")
    if not isinstance(resources, Mapping):
        raise ValueError
    table = resources.get("OperationalStateTable")
    bucket = resources.get("PrivateArtifactBucket")
    policy = resources.get("PrivateArtifactBucketPolicy")
    if not all(isinstance(value, Mapping) for value in (table, bucket, policy)):
        raise ValueError
    table = cast(Mapping[str, object], table)
    bucket = cast(Mapping[str, object], bucket)
    policy = cast(Mapping[str, object], policy)
    table_properties = table.get("Properties")
    bucket_properties = bucket.get("Properties")
    policy_properties = policy.get("Properties")
    if (
        table.get("Type") != FOUNDATION_RESOURCE_TYPES["OperationalStateTable"]
        or table.get("DeletionPolicy") != "Retain"
        or table.get("UpdateReplacePolicy") != "Retain"
        or not isinstance(table_properties, Mapping)
        or table_properties.get("TableName") != {"Fn::Sub": "mr-lister-phase6-${EnvironmentName}"}
        or bucket.get("Type") != FOUNDATION_RESOURCE_TYPES["PrivateArtifactBucket"]
        or bucket.get("DeletionPolicy") != "Retain"
        or bucket.get("UpdateReplacePolicy") != "Retain"
        or not isinstance(bucket_properties, Mapping)
        or bucket_properties.get("BucketName")
        != {
            "Fn::Sub": (
                "mr-lister-phase6-artifacts-${EnvironmentName}-${AWS::AccountId}-${AWS::Region}"
            )
        }
        or policy.get("Type") != FOUNDATION_RESOURCE_TYPES["PrivateArtifactBucketPolicy"]
        or not isinstance(policy_properties, Mapping)
        or policy_properties.get("Bucket") != {"Ref": "PrivateArtifactBucket"}
    ):
        raise ValueError


def _verify_locked_target_parameters(
    value: object, *, target_template: Mapping[str, object]
) -> dict[str, str]:
    supplied = _string_mapping(value)
    definitions = target_template.get("Parameters")
    if not isinstance(definitions, Mapping) or not definitions or set(supplied) != set(definitions):
        raise ValueError
    for name, expected_value in supplied.items():
        definition = definitions.get(name)
        if (
            not isinstance(definition, Mapping)
            or definition.get("Type") != "String"
            or definition.get("Default") != expected_value
            or definition.get("AllowedValues") != [expected_value]
            or definition.get("NoEcho") is True
        ):
            raise ValueError
        allowed_pattern = definition.get("AllowedPattern")
        if allowed_pattern is not None:
            if (
                not isinstance(allowed_pattern, str)
                or re.fullmatch(allowed_pattern, expected_value) is None
            ):
                raise ValueError
        minimum = definition.get("MinLength")
        maximum = definition.get("MaxLength")
        if (
            minimum is not None and (not isinstance(minimum, int) or len(expected_value) < minimum)
        ) or (
            maximum is not None and (not isinstance(maximum, int) or len(expected_value) > maximum)
        ):
            raise ValueError
    return supplied


def _valid_template_url(
    value: str,
    binding: Phase6FoundationBinding,
    *,
    release_fingerprint: str,
) -> bool:
    parsed = urlsplit(value)
    query = parse_qs(parsed.query, keep_blank_values=True)
    path = unquote(parsed.path)
    version_ids = query.get("versionId", [])
    expected_path = (
        "/private/deployments/cloudformation/core/releases/"
        f"{release_fingerprint}/core-template.json"
    )
    version_id = version_ids[0] if len(version_ids) == 1 else ""
    return bool(
        parsed.scheme == "https"
        and parsed.hostname == f"{binding.bucket_name}.s3.{binding.region}.amazonaws.com"
        and parsed.port is None
        and parsed.username is None
        and parsed.password is None
        and parsed.fragment == ""
        and path == expected_path
        and set(query) == {"versionId"}
        and _valid_s3_version_id(version_id)
    )


def _valid_s3_version_id(value: str) -> bool:
    return bool(
        isinstance(value, str)
        and 3 <= len(value) <= 1024
        and value == value.strip()
        and value.casefold() not in _MOVING_VERSION_IDS
        and _PLACEHOLDER.search(value) is None
        and all(0x21 <= ord(character) <= 0x7E for character in value)
        and not any(character in value for character in ('"', "\\"))
    )


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


def _string_mapping(value: object) -> dict[str, str]:
    if not isinstance(value, Mapping):
        raise ValueError
    result: dict[str, str] = {}
    for key, item in value.items():
        if not isinstance(key, str) or not key or not isinstance(item, str) or not item:
            raise ValueError
        result[key] = item
    return result


def _string_tuple(value: object) -> tuple[str, ...]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ValueError
    result = tuple(sorted(cast(list[str], value)))
    if len(set(result)) != len(result):
        raise ValueError
    return result


def _exact_stack_id(value: str, region: str, account_id: str, stack_name: str) -> bool:
    prefix = f"arn:aws:cloudformation:{region}:{account_id}:stack/{stack_name}/"
    suffix = value.removeprefix(prefix)
    return value.startswith(prefix) and _UUID.fullmatch(suffix) is not None


def _exact_change_set_id(value: str, region: str, account_id: str, name: str) -> bool:
    prefix = f"arn:aws:cloudformation:{region}:{account_id}:changeSet/{name}/"
    suffix = value.removeprefix(prefix)
    return value.startswith(prefix) and _UUID.fullmatch(suffix) is not None


def _aws_utc_datetime(value: object) -> datetime:
    if not isinstance(value, str):
        raise ValueError
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise ValueError
    return parsed.astimezone(UTC)


def _canonical_utc(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _canonical_json(value: object) -> str:
    return json.dumps(value, allow_nan=False, separators=(",", ":"), sort_keys=True)


def _load_mapping(path: Path) -> Mapping[str, object]:
    if path.is_symlink() or not path.is_file():
        raise ValueError
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, Mapping):
        raise ValueError
    return value


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--deployment-root", type=Path, required=True)
    parser.add_argument("--artifact-root", type=Path, required=True)
    parser.add_argument("--lambda-object-evidence", type=Path, required=True)
    parser.add_argument("--foundation-binding", type=Path, required=True)
    parser.add_argument("--expected-manifest", type=Path, required=True)
    parser.add_argument("--pre-stack-observation", type=Path, required=True)
    parser.add_argument("--pre-stack-resources-observation", type=Path, required=True)
    parser.add_argument("--change-set-observation", type=Path, required=True)
    parser.add_argument("--original-template-observation", type=Path, required=True)
    parser.add_argument("--processed-template-observation", type=Path, required=True)
    parser.add_argument("--target-template", type=Path, required=True)
    parser.add_argument("--caller-identity-observation", type=Path, required=True)
    parser.add_argument("--cloudtrail-observation", type=Path, required=True)
    parser.add_argument("--execution-role-observation", type=Path, required=True)
    parser.add_argument("--execution-role-inline-policies-observation", type=Path, required=True)
    parser.add_argument("--execution-role-attached-policies-observation", type=Path, required=True)
    parser.add_argument("--execution-role-policy-observation", type=Path, required=True)
    parser.add_argument("--deployer-role-observation", type=Path, required=True)
    parser.add_argument("--deployer-role-inline-policies-observation", type=Path, required=True)
    parser.add_argument("--deployer-role-attached-policies-observation", type=Path, required=True)
    parser.add_argument("--deployer-role-policy-observation", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the offline gate and emit only a canonical success descriptor."""

    try:
        args = _parser().parse_args(argv)
        result = verify_reviewed_update(
            deployment_root=args.deployment_root,
            artifact_root=args.artifact_root,
            lambda_object_evidence_path=args.lambda_object_evidence,
            foundation_binding_path=args.foundation_binding,
            expected_manifest_path=args.expected_manifest,
            pre_stack_observation_path=args.pre_stack_observation,
            pre_stack_resources_observation_path=args.pre_stack_resources_observation,
            change_set_observation_path=args.change_set_observation,
            original_template_observation_path=args.original_template_observation,
            processed_template_observation_path=args.processed_template_observation,
            target_template_path=args.target_template,
            caller_identity_observation_path=args.caller_identity_observation,
            cloudtrail_observation_path=args.cloudtrail_observation,
            execution_role_observation_path=args.execution_role_observation,
            execution_role_inline_policies_observation_path=(
                args.execution_role_inline_policies_observation
            ),
            execution_role_attached_policies_observation_path=(
                args.execution_role_attached_policies_observation
            ),
            execution_role_policy_observation_path=args.execution_role_policy_observation,
            deployer_role_observation_path=args.deployer_role_observation,
            deployer_role_inline_policies_observation_path=(
                args.deployer_role_inline_policies_observation
            ),
            deployer_role_attached_policies_observation_path=(
                args.deployer_role_attached_policies_observation
            ),
            deployer_role_policy_observation_path=args.deployer_role_policy_observation,
        )
        print(json.dumps(result, allow_nan=False, separators=(",", ":"), sort_keys=True))
        return 0
    except Phase6RuntimeUpdateError as error:
        raise SystemExit(str(error)) from None


if __name__ == "__main__":
    raise SystemExit(main())
