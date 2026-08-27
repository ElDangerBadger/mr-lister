"""Verify one additive Phase 6 seller-web edge UPDATE change set offline.

The verifier joins canonical predecessor and target Original/Processed template observations,
the sealed local target, and ``describe-change-set --include-property-values`` output.  It proves
that all existing resources are byte-for-byte unchanged and that the update adds exactly the
reviewed Phase 6 web, API, Cognito, and operational resources produced by the SAM transform.

``DescribeChangeSet`` does not return ``RoleARN`` or ``ChangeSetType``.  This module therefore
does not invent either observation: UPDATE is proved by the exact existing stack plus Add-only
resource changes, while the expected execution role is exposed for the separate reviewed
create-request/bootstrap gate.
"""

from __future__ import annotations

import argparse
import json
import re
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import Final

from tools.render_phase6_web_edge_transition import WEB_EDGE_TEMPLATE_SHA256

ROOT: Final = Path(__file__).resolve().parents[1]
FORMAT: Final = "mr-lister-phase6-web-edge-change-set-v1"
ACCOUNT_ID: Final = "384627057108"
REGION: Final = "us-west-2"
STACK_NAME: Final = "mr-lister-phase6-dev"
STACK_ID: Final = (
    "arn:aws:cloudformation:us-west-2:384627057108:stack/mr-lister-phase6-dev/"
    "f3456970-9fdc-11f1-b448-06b81627db1d"
)
EXPECTED_EXECUTION_ROLE_ARN: Final = (
    "arn:aws:iam::384627057108:role/mr-lister-phase6-runtime-cfn-dev"
)
PREDECESSOR_ORIGINAL_SHA256: Final = (
    "f0e1c0cfcf1b80d8c5277aacd68cb9a0246bedc882246c448a8772ebe4d87a78"
)
TARGET_ORIGINAL_SHA256: Final = WEB_EDGE_TEMPLATE_SHA256

_GENERIC_ERROR = "Phase 6 web-edge change-set evidence is invalid"
_MAX_INPUT_BYTES = 16 * 1024 * 1024
_UUID = re.compile(r"^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$")
_PLACEHOLDER = re.compile(
    r"<[A-Z][A-Z0-9_]*>|__[A-Z][A-Z0-9_]*__|"
    r"\b(?:PLACEHOLDER|REPLACE_ME|CHANGEME)\b",
    re.IGNORECASE,
)
_SAM_API_GATEWAY_SOURCE_ARN_PREFIX: Final = (
    "arn:${AWS::Partition}:execute-api:${AWS::Region}:${AWS::AccountId}:${__ApiId__}/${__Stage__}/"
)
_SAM_API_GATEWAY_SUBSTITUTIONS: Final = {
    "__ApiId__": {"Ref": "SellerHttpApi"},
    "__Stage__": "*",
}

_ORIGINAL_PREDECESSOR_COUNT = 40
_ORIGINAL_TARGET_COUNT = 102
_PROCESSED_PREDECESSOR_COUNT = 47
_PROCESSED_TARGET_COUNT = 125

_EXPECTED_PARAMETERS: Final = {
    "AgentCoreRuntimeArn": (
        "arn:aws:bedrock-agentcore:us-west-2:384627057108:runtime/mr_lister_phase6-4HoPmq2hCI"
    ),
    "AgentCoreRuntimeBindingFingerprint": (
        "14b001854285121f34394ce9893c19481f0f844aa6058abc9daca57d86d7c0f6"
    ),
    "AgentCoreRuntimeEndpointArn": (
        "arn:aws:bedrock-agentcore:us-west-2:384627057108:runtime/"
        "mr_lister_phase6-4HoPmq2hCI/runtime-endpoint/phase6_v1_dev"
    ),
    "AgentCoreRuntimeQualifier": "phase6_v1_dev",
    "AgentCoreRuntimeVersion": "1",
    "ApplicationOrigin": "https://massskutiny.com",
    "EnvironmentName": "dev",
    "PrintifySecretArn": (
        "arn:aws:secretsmanager:us-west-2:384627057108:secret:mr-lister/dev/printify/primary-FO1ZNd"
    ),
    "ReleaseFingerprint": ("0c6211a5b0244e9c86d635e6c02e7bc49e5e948d68895b4aaa982c0b0b2e187b"),
}
_APPLICATION_CERTIFICATE_ARN: Final = (
    "arn:aws:acm:us-east-1:384627057108:certificate/28b8cddb-a0d7-4dc8-98de-26fd87cb5b79"
)
_TARGET_PARAMETERS: Final = {
    **_EXPECTED_PARAMETERS,
    "ApplicationCertificateArn": _APPLICATION_CERTIFICATE_ARN,
}
_STACK_TAGS: Final = {
    "DeploymentClass": "FOUNDATION_ONLY",
    "Environment": "dev",
    "Project": "MrLister",
}

_ORIGINAL_ADDITION_TYPES: Final = {
    "ExecutionRecoveryAlarmSignalsAlarm": "AWS::CloudWatch::Alarm",
    "ExecutionRecoveryAuthorityConflictsAlarm": "AWS::CloudWatch::Alarm",
    "ExecutionRecoveryBatchSaturationAlarm": "AWS::CloudWatch::Alarm",
    "ExecutionRecoveryDependencyUnavailableAlarm": "AWS::CloudWatch::Alarm",
    "ExecutionRecoveryRunningPastBoundAlarm": "AWS::CloudWatch::Alarm",
    "ExecutionRecoverySettlementExhaustedAlarm": "AWS::CloudWatch::Alarm",
    "OperationalAlarmTopic": "AWS::SNS::Topic",
    "OperationalAlarmTopicKey": "AWS::KMS::Key",
    "OperationalAlarmTopicPolicy": "AWS::SNS::TopicPolicy",
    "OperationalStateTableThrottlesAlarm": "AWS::CloudWatch::Alarm",
    "Phase6LambdaDurationAlarm": "AWS::CloudWatch::Alarm",
    "Phase6LambdaErrorsAlarm": "AWS::CloudWatch::Alarm",
    "Phase6LambdaThrottlesAlarm": "AWS::CloudWatch::Alarm",
    "PrepareWorkflowAbortedAlarm": "AWS::CloudWatch::Alarm",
    "PrepareWorkflowFailedAlarm": "AWS::CloudWatch::Alarm",
    "PrepareWorkflowTimedOutAlarm": "AWS::CloudWatch::Alarm",
    "ReconcileProductWorkflowAbortedAlarm": "AWS::CloudWatch::Alarm",
    "ReconcileProductWorkflowFailedAlarm": "AWS::CloudWatch::Alarm",
    "ReconcileProductWorkflowTimedOutAlarm": "AWS::CloudWatch::Alarm",
    "RefreshEconomicsWorkflowAbortedAlarm": "AWS::CloudWatch::Alarm",
    "RefreshEconomicsWorkflowFailedAlarm": "AWS::CloudWatch::Alarm",
    "RefreshEconomicsWorkflowTimedOutAlarm": "AWS::CloudWatch::Alarm",
    "ReviewQueryApiFunction": "AWS::Serverless::Function",
    "ReviewQueryApiFunctionRole": "AWS::IAM::Role",
    "ReviewQueryApiLogGroup": "AWS::Logs::LogGroup",
    "SellerApiAccessLogGroup": "AWS::Logs::LogGroup",
    "SellerApiMethodGuardFunction": "AWS::CloudFront::Function",
    "SellerApiNoStoreCachePolicy": "AWS::CloudFront::CachePolicy",
    "SellerApiResourceServer": "AWS::Cognito::UserPoolResourceServer",
    "SellerApiServerErrorsAlarm": "AWS::CloudWatch::Alarm",
    "SellerCommandApiFunction": "AWS::Serverless::Function",
    "SellerCommandApiFunctionRole": "AWS::IAM::Role",
    "SellerCommandApiLogGroup": "AWS::Logs::LogGroup",
    "SellerHttpApi": "AWS::Serverless::HttpApi",
    "SellerSpaRouteFunction": "AWS::CloudFront::Function",
    "SellerUserPool": "AWS::Cognito::UserPool",
    "SellerUserPoolClient": "AWS::Cognito::UserPoolClient",
    "SellerUserPoolDomain": "AWS::Cognito::UserPoolDomain",
    "SellerUserPoolGroup": "AWS::Cognito::UserPoolGroup",
    "SellerWebAssetBucket": "AWS::S3::Bucket",
    "SellerWebAssetBucketPolicy": "AWS::S3::BucketPolicy",
    "SellerWebDistribution": "AWS::CloudFront::Distribution",
    "SellerWebImmutableAssetCachePolicy": "AWS::CloudFront::CachePolicy",
    "SellerWebImmutableResponseHeadersPolicy": "AWS::CloudFront::ResponseHeadersPolicy",
    "SellerWebNoStoreCachePolicy": "AWS::CloudFront::CachePolicy",
    "SellerWebNoStoreResponseHeadersPolicy": "AWS::CloudFront::ResponseHeadersPolicy",
    "SellerWebOriginAccessControl": "AWS::CloudFront::OriginAccessControl",
    "SourceVersionRetentionErrorsAlarm": "AWS::CloudWatch::Alarm",
    "SourceVersionRetentionLivenessAlarm": "AWS::CloudWatch::Alarm",
    "StuckExecutionRecoveryDeadLettersAlarm": "AWS::CloudWatch::Alarm",
    "StuckExecutionRecoveryDurationAlarm": "AWS::CloudWatch::Alarm",
    "StuckExecutionRecoveryErrorsAlarm": "AWS::CloudWatch::Alarm",
    "StuckExecutionRecoveryScheduleFailuresAlarm": "AWS::CloudWatch::Alarm",
    "StuckExecutionRecoveryThrottlesAlarm": "AWS::CloudWatch::Alarm",
    "SynchronizeProductWorkflowAbortedAlarm": "AWS::CloudWatch::Alarm",
    "SynchronizeProductWorkflowFailedAlarm": "AWS::CloudWatch::Alarm",
    "SynchronizeProductWorkflowTimedOutAlarm": "AWS::CloudWatch::Alarm",
    "TerminalOperationalCleanupErrorsAlarm": "AWS::CloudWatch::Alarm",
    "TerminalOperationalCleanupLivenessAlarm": "AWS::CloudWatch::Alarm",
    "UploadApiFunction": "AWS::Serverless::Function",
    "UploadApiFunctionRole": "AWS::IAM::Role",
    "UploadApiLogGroup": "AWS::Logs::LogGroup",
}


def _processed_addition_types() -> dict[str, str]:
    result = dict(_ORIGINAL_ADDITION_TYPES)
    for logical_id in (
        "ReviewQueryApiFunction",
        "SellerCommandApiFunction",
        "UploadApiFunction",
    ):
        result[logical_id] = "AWS::Lambda::Function"
    result["SellerHttpApi"] = "AWS::ApiGatewayV2::Api"
    result.update(
        {
            "ReviewQueryApiFunctionGetArtworkPreviewPermission": "AWS::Lambda::Permission",
            "ReviewQueryApiFunctionGetJobPermission": "AWS::Lambda::Permission",
            "ReviewQueryApiFunctionGetReviewPermission": "AWS::Lambda::Permission",
            "ReviewQueryApiFunctionHealthPermission": "AWS::Lambda::Permission",
            "ReviewQueryApiFunctionListJobsPermission": "AWS::Lambda::Permission",
            "SellerCommandApiFunctionApproveReviewPermission": "AWS::Lambda::Permission",
            "SellerCommandApiFunctionCancelJobPermission": "AWS::Lambda::Permission",
            "SellerCommandApiFunctionRefreshEconomicsPermission": "AWS::Lambda::Permission",
            "SellerCommandApiFunctionRetryJobPermission": "AWS::Lambda::Permission",
            "SellerCommandApiFunctionReviseListingPermission": "AWS::Lambda::Permission",
            "SellerHttpApiApiGatewayDefaultStage": "AWS::ApiGatewayV2::Stage",
            "UploadApiFunctionAuthorizeUploadPermission": "AWS::Lambda::Permission",
            "UploadApiFunctionCancelUploadPermission": "AWS::Lambda::Permission",
            "UploadApiFunctionCompleteUploadPermission": "AWS::Lambda::Permission",
            "UploadApiFunctionCreateUploadPermission": "AWS::Lambda::Permission",
            "UploadApiFunctionGetUploadPermission": "AWS::Lambda::Permission",
        }
    )
    return result


_PROCESSED_ADDITION_TYPES: Final = _processed_addition_types()
_OUTPUT_ADDITIONS: Final = frozenset(
    {
        "ArtifactBucketBrowserOrigin",
        "OperationalAlarmTopicArn",
        "SellerApiOrigin",
        "SellerApplicationOrigin",
        "SellerRuntimeConfig",
        "SellerRuntimeConfigObjectKey",
        "SellerSignInOrigin",
        "SellerUserPoolClientId",
        "SellerUserPoolId",
        "SellerWebAssetBucketName",
        "SellerWebDistributionDomainName",
        "SellerWebDistributionId",
    }
)

_ORIGINAL_KEYS = {
    "AWSTemplateFormatVersion",
    "Description",
    "Globals",
    "Metadata",
    "Outputs",
    "Parameters",
    "Resources",
    "Transform",
}
_PROCESSED_KEYS = {
    "AWSTemplateFormatVersion",
    "Description",
    "Metadata",
    "Outputs",
    "Parameters",
    "Resources",
}
_GET_TEMPLATE_KEYS = {"StagesAvailable", "TemplateBody"}
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
_ADD_REQUIRED_KEYS = {"Action", "Details", "LogicalResourceId", "ResourceType", "Scope"}
_ADD_ALLOWED_KEYS = _ADD_REQUIRED_KEYS | {"AfterContext"}


class Phase6WebEdgeChangeSetError(RuntimeError):
    """A value-free failure for broadened or mismatched web-edge evidence."""


@dataclass(frozen=True, slots=True)
class VerifiedPhase6WebEdgeChangeSet:
    """Identity emitted only after the complete additive offline join succeeds."""

    format: str
    stack_id: str
    expected_execution_role_arn: str
    change_set_id: str
    change_set_name: str
    change_set_type: str
    original_added_resources: tuple[str, ...]
    processed_added_resources: tuple[str, ...]
    predecessor_original_sha256: str
    predecessor_processed_sha256: str
    target_local_sha256: str
    target_original_sha256: str
    target_processed_sha256: str
    change_set_sha256: str
    canonical_sha256: str


def canonical_phase6_web_edge_change_set(value: object) -> bytes:
    """Return the only accepted JSON representation for evidence and success records."""

    try:
        return (
            json.dumps(value, allow_nan=False, indent=2, separators=(",", ": "), sort_keys=True)
            + "\n"
        ).encode("utf-8")
    except Exception:
        raise Phase6WebEdgeChangeSetError(_GENERIC_ERROR) from None


def verify_phase6_web_edge_change_set(
    *,
    predecessor_original_template_observation_path: Path,
    predecessor_processed_template_observation_path: Path,
    target_template_path: Path,
    change_set_observation_path: Path,
    target_original_template_observation_path: Path,
    target_processed_template_observation_path: Path,
    repository_root: Path = ROOT,
) -> VerifiedPhase6WebEdgeChangeSet:
    """Verify the exact additive web-edge change set from canonical local captures."""

    try:
        repository = _repository(repository_root)
        predecessor_original_raw, predecessor_original_observation = _load_mapping(
            predecessor_original_template_observation_path, repository
        )
        predecessor_processed_raw, predecessor_processed_observation = _load_mapping(
            predecessor_processed_template_observation_path, repository
        )
        target_local_raw, target_local = _load_mapping(target_template_path, repository)
        change_set_raw, change_set = _load_mapping(change_set_observation_path, repository)
        target_original_raw, target_original_observation = _load_mapping(
            target_original_template_observation_path, repository
        )
        target_processed_raw, target_processed_observation = _load_mapping(
            target_processed_template_observation_path,
            repository,
            allow_sam_api_gateway_source_arns=True,
        )

        predecessor_original = _template_body(predecessor_original_observation)
        predecessor_processed = _template_body(predecessor_processed_observation)
        target_original = _template_body(target_original_observation)
        target_processed = _template_body(target_processed_observation)
        if target_original != target_local:
            raise ValueError
        if _canonical_sha256(predecessor_original) != PREDECESSOR_ORIGINAL_SHA256:
            raise ValueError

        _verify_template_shapes(
            predecessor_original,
            predecessor_processed,
            target_original,
            target_processed,
        )
        _verify_original_addition(predecessor_original, target_original)
        _verify_processed_addition(predecessor_processed, target_processed)

        target_sha256 = sha256(target_local_raw).hexdigest()
        if target_sha256 != TARGET_ORIGINAL_SHA256:
            raise ValueError
        change_set_id, change_set_name = _verify_change_set(
            change_set,
            target_original=target_original,
            target_processed=target_processed,
            target_sha256=target_sha256,
        )
        payload = {
            "change_set_id": change_set_id,
            "change_set_name": change_set_name,
            "change_set_sha256": sha256(change_set_raw).hexdigest(),
            "change_set_type": "UPDATE",
            "expected_execution_role_arn": EXPECTED_EXECUTION_ROLE_ARN,
            "format": FORMAT,
            "original_added_resources": sorted(_ORIGINAL_ADDITION_TYPES),
            "predecessor_original_sha256": sha256(predecessor_original_raw).hexdigest(),
            "predecessor_processed_sha256": sha256(predecessor_processed_raw).hexdigest(),
            "processed_added_resources": sorted(_PROCESSED_ADDITION_TYPES),
            "stack_id": STACK_ID,
            "target_local_sha256": target_sha256,
            "target_original_sha256": sha256(target_original_raw).hexdigest(),
            "target_processed_sha256": sha256(target_processed_raw).hexdigest(),
        }
        fingerprint = sha256(canonical_phase6_web_edge_change_set(payload)).hexdigest()
        return VerifiedPhase6WebEdgeChangeSet(
            format=FORMAT,
            stack_id=STACK_ID,
            expected_execution_role_arn=EXPECTED_EXECUTION_ROLE_ARN,
            change_set_id=change_set_id,
            change_set_name=change_set_name,
            change_set_type="UPDATE",
            original_added_resources=tuple(sorted(_ORIGINAL_ADDITION_TYPES)),
            processed_added_resources=tuple(sorted(_PROCESSED_ADDITION_TYPES)),
            predecessor_original_sha256=payload["predecessor_original_sha256"],
            predecessor_processed_sha256=payload["predecessor_processed_sha256"],
            target_local_sha256=payload["target_local_sha256"],
            target_original_sha256=payload["target_original_sha256"],
            target_processed_sha256=payload["target_processed_sha256"],
            change_set_sha256=payload["change_set_sha256"],
            canonical_sha256=fingerprint,
        )
    except Phase6WebEdgeChangeSetError:
        raise
    except Exception:
        raise Phase6WebEdgeChangeSetError(_GENERIC_ERROR) from None


def _verify_template_shapes(
    predecessor_original: Mapping[str, object],
    predecessor_processed: Mapping[str, object],
    target_original: Mapping[str, object],
    target_processed: Mapping[str, object],
) -> None:
    if (
        set(predecessor_original) != _ORIGINAL_KEYS
        or set(target_original) != _ORIGINAL_KEYS
        or set(predecessor_processed) != _PROCESSED_KEYS
        or set(target_processed) != _PROCESSED_KEYS
        or predecessor_original.get("AWSTemplateFormatVersion") != "2010-09-09"
        or target_original.get("AWSTemplateFormatVersion") != "2010-09-09"
        or predecessor_original.get("Transform") != "AWS::Serverless-2016-10-31"
        or target_original.get("Transform") != "AWS::Serverless-2016-10-31"
    ):
        raise ValueError
    for original, processed in (
        (predecessor_original, predecessor_processed),
        (target_original, target_processed),
    ):
        for key in (
            "AWSTemplateFormatVersion",
            "Description",
            "Metadata",
            "Outputs",
            "Parameters",
        ):
            if original.get(key) != processed.get(key):
                raise ValueError
    if _parameter_values(predecessor_original) != _EXPECTED_PARAMETERS:
        raise ValueError
    if _parameter_values(predecessor_processed) != _EXPECTED_PARAMETERS:
        raise ValueError
    if _parameter_values(target_original) != _TARGET_PARAMETERS:
        raise ValueError
    if _parameter_values(target_processed) != _TARGET_PARAMETERS:
        raise ValueError


def _verify_original_addition(
    predecessor: Mapping[str, object], target: Mapping[str, object]
) -> None:
    before = _mapping(predecessor, "Resources")
    after = _mapping(target, "Resources")
    if len(before) != _ORIGINAL_PREDECESSOR_COUNT or len(after) != _ORIGINAL_TARGET_COUNT:
        raise ValueError
    _require_exact_addition(before, after, _ORIGINAL_ADDITION_TYPES)
    if predecessor.get("Globals") != target.get("Globals"):
        raise ValueError
    predecessor_metadata = _mapping(predecessor, "Metadata")
    target_metadata = _mapping(target, "Metadata")
    core_key = "MrListerPhase6CoreRuntimeStaging"
    if predecessor_metadata.get(core_key) != target_metadata.get(core_key):
        raise ValueError
    before_outputs = _mapping(predecessor, "Outputs")
    after_outputs = _mapping(target, "Outputs")
    if set(after_outputs) - set(before_outputs) != _OUTPUT_ADDITIONS:
        raise ValueError
    if set(before_outputs) - set(after_outputs):
        raise ValueError
    for name in set(before_outputs) - {"DeploymentReadiness"}:
        if before_outputs[name] != after_outputs[name]:
            raise ValueError
    if not isinstance(after_outputs.get("DeploymentReadiness"), Mapping):
        raise ValueError


def _verify_processed_addition(
    predecessor: Mapping[str, object], target: Mapping[str, object]
) -> None:
    before = _mapping(predecessor, "Resources")
    after = _mapping(target, "Resources")
    if len(before) != _PROCESSED_PREDECESSOR_COUNT or len(after) != _PROCESSED_TARGET_COUNT:
        raise ValueError
    _require_exact_addition(before, after, _PROCESSED_ADDITION_TYPES)


def _require_exact_addition(
    before: Mapping[str, object],
    after: Mapping[str, object],
    additions: Mapping[str, str],
) -> None:
    if set(after) != set(before) | set(additions) or set(before) & set(additions):
        raise ValueError
    for logical_id, resource in before.items():
        if after.get(logical_id) != resource:
            raise ValueError
    for logical_id, resource_type in additions.items():
        resource = after.get(logical_id)
        if not isinstance(resource, Mapping) or resource.get("Type") != resource_type:
            raise ValueError


def _verify_change_set(
    observation: Mapping[str, object],
    *,
    target_original: Mapping[str, object],
    target_processed: Mapping[str, object],
    target_sha256: str,
) -> tuple[str, str]:
    allowed = _CHANGE_SET_REQUIRED_KEYS | _CHANGE_SET_OPTIONAL_KEYS
    if not _CHANGE_SET_REQUIRED_KEYS <= set(observation) <= allowed:
        raise ValueError
    expected_name = f"mr-lister-phase6-dev-web-edge-{target_sha256[:12]}"
    change_set_id = observation.get("ChangeSetId")
    if (
        observation.get("StackId") != STACK_ID
        or observation.get("StackName") != STACK_NAME
        or observation.get("ChangeSetName") != expected_name
        or observation.get("Status") != "CREATE_COMPLETE"
        or observation.get("StatusReason") is not None
        or observation.get("ExecutionStatus") != "AVAILABLE"
        or observation.get("IncludeNestedStacks") is not False
        or observation.get("ImportExistingResources") not in (None, False)
        or observation.get("ParentChangeSetId") is not None
        or observation.get("RootChangeSetId") is not None
        or observation.get("DeploymentMode") is not None
        or observation.get("StackDriftStatus") is not None
        or observation.get("OnStackFailure") is not None
        or observation.get("DeploymentConfig") != {"DisableRollback": False, "Mode": "STANDARD"}
        or observation.get("RollbackConfiguration") != {}
        or observation.get("NotificationARNs") != []
        or observation.get("Capabilities") != ["CAPABILITY_NAMED_IAM"]
        or not isinstance(observation.get("Description"), str)
        or not observation["Description"].strip()  # type: ignore[union-attr]
        or observation["Description"] != observation["Description"].strip()  # type: ignore[union-attr]
        or not isinstance(change_set_id, str)
        or not _exact_change_set_id(change_set_id, expected_name)
    ):
        raise ValueError
    _utc_datetime(observation.get("CreationTime"))
    if _records(observation.get("Tags"), "Key", "Value") != _STACK_TAGS:
        raise ValueError
    if _records(observation.get("Parameters"), "ParameterKey", "ParameterValue") != (
        _parameter_values(target_original)
    ):
        raise ValueError
    _verify_add_changes(observation.get("Changes"), target_processed)
    return change_set_id, expected_name


def _verify_add_changes(value: object, target_processed: Mapping[str, object]) -> None:
    if not isinstance(value, list) or len(value) != len(_PROCESSED_ADDITION_TYPES):
        raise ValueError
    target_resources = _mapping(target_processed, "Resources")
    found: set[str] = set()
    for item in value:
        if not isinstance(item, Mapping) or set(item) != {"ResourceChange", "Type"}:
            raise ValueError
        resource = item.get("ResourceChange")
        if item.get("Type") != "Resource" or not isinstance(resource, Mapping):
            raise ValueError
        if not _ADD_REQUIRED_KEYS <= set(resource) <= _ADD_ALLOWED_KEYS:
            raise ValueError
        logical_id = resource.get("LogicalResourceId")
        if (
            not isinstance(logical_id, str)
            or logical_id in found
            or logical_id not in _PROCESSED_ADDITION_TYPES
            or resource.get("Action") != "Add"
            or resource.get("Details") != []
            or resource.get("Scope") != []
            or resource.get("ResourceType") != _PROCESSED_ADDITION_TYPES[logical_id]
            or _mapping(target_resources, logical_id).get("Type") != resource.get("ResourceType")
        ):
            raise ValueError
        if "AfterContext" in resource:
            _decode_context(resource["AfterContext"])
        found.add(logical_id)
    if found != set(_PROCESSED_ADDITION_TYPES):
        raise ValueError


def _template_body(observation: Mapping[str, object]) -> Mapping[str, object]:
    if (
        set(observation) != _GET_TEMPLATE_KEYS
        or observation.get("StagesAvailable") != ["Original", "Processed"]
        or not isinstance(observation.get("TemplateBody"), Mapping)
    ):
        raise ValueError
    return observation["TemplateBody"]  # type: ignore[return-value]


def _parameter_values(document: Mapping[str, object]) -> dict[str, str]:
    parameters = _mapping(document, "Parameters")
    result: dict[str, str] = {}
    for name, definition in parameters.items():
        if not isinstance(name, str) or not isinstance(definition, Mapping):
            raise ValueError
        default = definition.get("Default")
        if (
            not isinstance(default, str)
            or not default
            or default != default.strip()
            or definition.get("AllowedValues") != [default]
        ):
            raise ValueError
        result[name] = default
    return result


def _records(value: object, key_name: str, value_name: str) -> dict[str, str]:
    if not isinstance(value, list):
        raise ValueError
    result: dict[str, str] = {}
    for record in value:
        if not isinstance(record, Mapping) or set(record) != {key_name, value_name}:
            raise ValueError
        key = record.get(key_name)
        record_value = record.get(value_name)
        if (
            not isinstance(key, str)
            or not key
            or key in result
            or not isinstance(record_value, str)
            or not record_value
        ):
            raise ValueError
        result[key] = record_value
    return result


def _mapping(value: Mapping[str, object], key: str) -> Mapping[str, object]:
    nested = value.get(key)
    if not isinstance(nested, Mapping):
        raise ValueError
    return nested


def _decode_context(value: object) -> Mapping[str, object]:
    if not isinstance(value, str) or not value:
        raise ValueError
    decoded = json.loads(value, object_pairs_hook=_unique_object, parse_constant=_bad_constant)
    if not isinstance(decoded, Mapping):
        raise ValueError
    _reject_placeholders(
        decoded,
        allow_sam_api_gateway_source_arns=False,
        path=(),
    )
    return decoded


def _canonical_sha256(value: object) -> str:
    return sha256(canonical_phase6_web_edge_change_set(value)).hexdigest()


def _exact_change_set_id(value: str, name: str) -> bool:
    prefix = f"arn:aws:cloudformation:{REGION}:{ACCOUNT_ID}:changeSet/{name}/"
    suffix = value.removeprefix(prefix)
    return value.startswith(prefix) and _UUID.fullmatch(suffix) is not None


def _utc_datetime(value: object) -> datetime:
    if not isinstance(value, str):
        raise ValueError
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError
    if parsed.utcoffset().total_seconds() != 0:
        raise ValueError
    return parsed.astimezone(UTC)


def _repository(path: Path) -> Path:
    if not isinstance(path, Path) or path.is_symlink():
        raise ValueError
    resolved = path.resolve(strict=True)
    if not resolved.is_dir():
        raise ValueError
    return resolved


def _load_mapping(
    path: Path,
    repository: Path,
    *,
    allow_sam_api_gateway_source_arns: bool = False,
) -> tuple[bytes, Mapping[str, object]]:
    if not isinstance(path, Path):
        raise ValueError
    candidate = path if path.is_absolute() else repository / path
    if not candidate.is_relative_to(repository):
        raise ValueError
    current = repository
    for component in candidate.relative_to(repository).parts:
        if component in {"", ".", ".."}:
            raise ValueError
        current = current / component
        if current.is_symlink():
            raise ValueError
    resolved = candidate.resolve(strict=True)
    if not resolved.is_relative_to(repository):
        raise ValueError
    if not resolved.is_file():
        raise ValueError
    raw = resolved.read_bytes()
    if not raw or len(raw) > _MAX_INPUT_BYTES or b"\x00" in raw:
        raise ValueError
    value = json.loads(raw, object_pairs_hook=_unique_object, parse_constant=_bad_constant)
    if not isinstance(value, Mapping) or canonical_phase6_web_edge_change_set(value) != raw:
        raise ValueError
    _reject_placeholders(
        value,
        allow_sam_api_gateway_source_arns=allow_sam_api_gateway_source_arns,
        path=(),
    )
    return raw, value


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError
        result[key] = value
    return result


def _bad_constant(_value: str) -> object:
    raise ValueError


def _reject_placeholders(
    value: object,
    *,
    allow_sam_api_gateway_source_arns: bool,
    path: tuple[str, ...],
) -> None:
    if isinstance(value, str):
        if _PLACEHOLDER.search(value):
            raise ValueError
    elif isinstance(value, Mapping):
        if allow_sam_api_gateway_source_arns and _is_sam_api_gateway_source_arn(value, path):
            return
        for key, nested in value.items():
            if not isinstance(key, str) or _PLACEHOLDER.search(key):
                raise ValueError
            _reject_placeholders(
                nested,
                allow_sam_api_gateway_source_arns=allow_sam_api_gateway_source_arns,
                path=(*path, key),
            )
    elif isinstance(value, list):
        for index, nested in enumerate(value):
            _reject_placeholders(
                nested,
                allow_sam_api_gateway_source_arns=allow_sam_api_gateway_source_arns,
                path=(*path, f"[{index}]"),
            )


def _is_sam_api_gateway_source_arn(value: Mapping[str, object], path: tuple[str, ...]) -> bool:
    """Recognize only SAM's fully bound API permission ``Fn::Sub`` form."""

    if len(path) != 5 or path[:2] != ("TemplateBody", "Resources"):
        return False
    logical_id = path[2]
    if (
        path[3:] != ("Properties", "SourceArn")
        or _PROCESSED_ADDITION_TYPES.get(logical_id) != "AWS::Lambda::Permission"
        or set(value) != {"Fn::Sub"}
    ):
        return False
    substitution = value["Fn::Sub"]
    if not isinstance(substitution, list) or len(substitution) != 2:
        return False
    template, substitutions = substitution
    if not isinstance(template, str) or not isinstance(substitutions, Mapping):
        return False
    if dict(substitutions) != _SAM_API_GATEWAY_SUBSTITUTIONS:
        return False
    if not template.startswith(_SAM_API_GATEWAY_SOURCE_ARN_PREFIX):
        return False
    if len(template) == len(_SAM_API_GATEWAY_SOURCE_ARN_PREFIX):
        return False
    if template.count("${__ApiId__}") != 1 or template.count("${__Stage__}") != 1:
        return False
    remainder = template.replace("${__ApiId__}", "").replace("${__Stage__}", "")
    return _PLACEHOLDER.search(remainder) is None


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--predecessor-original-template-observation", type=Path, required=True)
    parser.add_argument("--predecessor-processed-template-observation", type=Path, required=True)
    parser.add_argument("--target-template", type=Path, required=True)
    parser.add_argument("--change-set-observation", type=Path, required=True)
    parser.add_argument("--target-original-template-observation", type=Path, required=True)
    parser.add_argument("--target-processed-template-observation", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run the offline verifier and print only its canonical success record."""

    arguments = _parser().parse_args(argv)
    try:
        verified = verify_phase6_web_edge_change_set(
            predecessor_original_template_observation_path=(
                arguments.predecessor_original_template_observation
            ),
            predecessor_processed_template_observation_path=(
                arguments.predecessor_processed_template_observation
            ),
            target_template_path=arguments.target_template,
            change_set_observation_path=arguments.change_set_observation,
            target_original_template_observation_path=(
                arguments.target_original_template_observation
            ),
            target_processed_template_observation_path=(
                arguments.target_processed_template_observation
            ),
        )
    except Phase6WebEdgeChangeSetError:
        _parser().error(_GENERIC_ERROR)
    print(canonical_phase6_web_edge_change_set(asdict(verified)).decode("utf-8"), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
