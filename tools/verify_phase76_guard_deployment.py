"""Verify a sealed Phase 7.6 guard and exact read-only AWS CLI capture set offline.

This tool never imports an AWS SDK, starts a subprocess, or calls a provider.  Its optional live
gate consumes the unedited JSON and binary payloads produced by reviewed ``aws`` read commands.
"""

from __future__ import annotations

import argparse
import base64
import json
import re
from collections.abc import Mapping
from hashlib import sha256
from pathlib import Path
from typing import cast

from mr_lister.release.phase7 import GUARD_PROFILE_FINGERPRINT
from tools.build_phase76_guard_bundle import verify_guard_deployment_artifact

_BUCKET = re.compile(r"^[a-z0-9](?:[a-z0-9.-]{1,61}[a-z0-9])$")
_ENVIRONMENT = re.compile(r"^[a-z][a-z0-9-]{1,15}$")
_ACCOUNT = re.compile(r"^[0-9]{12}$")
_REGION = re.compile(r"^[a-z]{2}(?:-[a-z0-9]+){1,2}-[1-9][0-9]?$|^us-gov-[a-z]+-[1-9]$")
_STACK = re.compile(r"^[A-Za-z][A-Za-z0-9-]{0,127}$")
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
_FINGERPRINT = re.compile(r"^[a-f0-9]{64}$")
_PHASE7_CONTRACT_FINGERPRINT = "548b710230618e73c20a509f2121799c415b50070e1e2ae7e1b82fe3c37e2981"


def verify_stack_observation(
    descriptor: Mapping[str, object],
    observation: Mapping[str, object],
    *,
    stack_name: str,
    environment_name: str,
    bucket: str,
    key: str,
    version_id: str,
    region: str,
    account_id: str,
) -> None:
    """Verify one exact successful CloudFormation ``describe-stacks`` observation."""

    try:
        _verify_identity_inputs(
            stack_name=stack_name,
            environment_name=environment_name,
            bucket=bucket,
            key=key,
            version_id=version_id,
            region=region,
            account_id=account_id,
        )
        stacks = observation.get("Stacks")
        if set(observation) != {"Stacks"} or not isinstance(stacks, list) or len(stacks) != 1:
            raise ValueError
        stack = stacks[0]
        if not isinstance(stack, Mapping):
            raise ValueError
        release = descriptor["release_fingerprint"]
        application_release = descriptor["application_release_fingerprint"]
        function_name = _function_name(environment_name)
        expected_parameters = {
            "ApplicationReleaseFingerprint": application_release,
            "EnvironmentName": environment_name,
            "GuardCodeS3Bucket": bucket,
            "GuardCodeS3Key": key,
            "GuardCodeS3ObjectVersion": version_id,
            "GuardReleaseFingerprint": release,
        }
        parameters = _key_value_records(stack.get("Parameters"), "ParameterKey", "ParameterValue")
        outputs = _key_value_records(stack.get("Outputs"), "OutputKey", "OutputValue")
        expected_outputs = {
            "DeploymentReadiness": "READ_ONLY_GUARD",
            "PublicationGuardExternalCallsEnabled": "false",
            "PublicationGuardVerificationEnabled": "true",
            "PublicationGuardVerificationFunctionArn": (
                f"arn:{_partition(region)}:lambda:{region}:{account_id}:function:{function_name}"
            ),
            "PublicationEnabled": "false",
            "PublicationRequestEnabled": "false",
            "PublicationStatusAlarmTopicArn": (
                f"arn:{_partition(region)}:sns:{region}:{account_id}:"
                f"mr-lister-phase7-{environment_name}-publication-status-alarms"
            ),
            "PublicationStatusQueryEnabled": "false",
            "PublicationStatusQueryRegistered": "false",
        }
        stack_id = stack.get("StackId")
        if (
            not isinstance(release, str)
            or not isinstance(application_release, str)
            or _FINGERPRINT.fullmatch(application_release) is None
            or application_release == "0" * 64
            or key != f"phase7/releases/{release}/guard.zip"
            or stack_name != _guard_stack_name(environment_name)
            or stack.get("StackName") != stack_name
            or stack.get("StackStatus") not in {"CREATE_COMPLETE", "UPDATE_COMPLETE"}
            or not isinstance(stack_id, str)
            or not stack_id.startswith(
                f"arn:{_partition(region)}:cloudformation:{region}:{account_id}:stack/{stack_name}/"
            )
            or parameters != expected_parameters
            or outputs != expected_outputs
        ):
            raise ValueError
    except Exception:
        raise ValueError("Phase 7 guard CloudFormation observation is invalid") from None


def verify_stack_resources_observation(
    observation: Mapping[str, object],
    *,
    environment_name: str,
    region: str,
    account_id: str,
) -> None:
    """Verify the stack instantiated only the nine private guard-support resources."""

    try:
        if (
            _ENVIRONMENT.fullmatch(environment_name) is None
            or _REGION.fullmatch(region) is None
            or _ACCOUNT.fullmatch(account_id) is None
        ):
            raise ValueError
        summaries = observation.get("StackResourceSummaries")
        if (
            not isinstance(summaries, list)
            or observation.get("NextToken") is not None
            or set(observation) - {"NextToken", "StackResourceSummaries"}
        ):
            raise ValueError
        expected_types = {
            "PublicationGuardVerificationDurationAlarm": "AWS::CloudWatch::Alarm",
            "PublicationGuardVerificationErrorsAlarm": "AWS::CloudWatch::Alarm",
            "PublicationGuardVerificationFunction": "AWS::Lambda::Function",
            "PublicationGuardVerificationFunctionRole": "AWS::IAM::Role",
            "PublicationGuardVerificationLogGroup": "AWS::Logs::LogGroup",
            "PublicationGuardVerificationThrottlesAlarm": "AWS::CloudWatch::Alarm",
            "PublicationStatusAlarmTopic": "AWS::SNS::Topic",
            "PublicationStatusAlarmTopicKey": "AWS::KMS::Key",
            "PublicationStatusAlarmTopicPolicy": "AWS::SNS::TopicPolicy",
        }
        resources: dict[str, Mapping[str, object]] = {}
        for summary in summaries:
            if not isinstance(summary, Mapping):
                raise ValueError
            logical_id = summary.get("LogicalResourceId")
            physical_id = summary.get("PhysicalResourceId")
            if (
                not isinstance(logical_id, str)
                or logical_id in resources
                or summary.get("ResourceType") != expected_types.get(logical_id)
                or summary.get("ResourceStatus") not in {"CREATE_COMPLETE", "UPDATE_COMPLETE"}
                or not isinstance(physical_id, str)
                or not physical_id
                or physical_id != physical_id.strip()
            ):
                raise ValueError
            resources[logical_id] = cast(Mapping[str, object], summary)
        if set(resources) != set(expected_types):
            raise ValueError

        function_name = _function_name(environment_name)
        expected_physical_ids = {
            "PublicationGuardVerificationDurationAlarm": (
                f"mr-lister-phase7-{environment_name}-publication-status-guard-duration"
            ),
            "PublicationGuardVerificationErrorsAlarm": (
                f"mr-lister-phase7-{environment_name}-publication-status-guard-errors"
            ),
            "PublicationGuardVerificationFunction": function_name,
            "PublicationGuardVerificationFunctionRole": f"{function_name}-role",
            "PublicationGuardVerificationLogGroup": f"/aws/lambda/{function_name}",
            "PublicationGuardVerificationThrottlesAlarm": (
                f"mr-lister-phase7-{environment_name}-publication-status-guard-throttles"
            ),
            "PublicationStatusAlarmTopic": (
                f"arn:{_partition(region)}:sns:{region}:{account_id}:"
                f"mr-lister-phase7-{environment_name}-publication-status-alarms"
            ),
        }
        if any(
            resources[logical_id].get("PhysicalResourceId") != physical_id
            for logical_id, physical_id in expected_physical_ids.items()
        ):
            raise ValueError
    except Exception:
        raise ValueError("Phase 7 guard stack resource observation is invalid") from None


def verify_phase6_application_release_observation(
    descriptor: Mapping[str, object],
    observation: Mapping[str, object],
    *,
    environment_name: str,
    region: str,
    account_id: str,
) -> None:
    """Bind the guard's application fingerprint to the exact deployed Phase 6 stack."""

    try:
        if (
            _ENVIRONMENT.fullmatch(environment_name) is None
            or _REGION.fullmatch(region) is None
            or _ACCOUNT.fullmatch(account_id) is None
        ):
            raise ValueError
        stacks = observation.get("Stacks")
        if set(observation) != {"Stacks"} or not isinstance(stacks, list) or len(stacks) != 1:
            raise ValueError
        stack = stacks[0]
        if not isinstance(stack, Mapping):
            raise ValueError
        stack_name = f"mr-lister-phase6-{environment_name}"
        stack_id = stack.get("StackId")
        parameters = _key_value_records(
            stack.get("Parameters"),
            "ParameterKey",
            "ParameterValue",
        )
        application_release = descriptor.get("application_release_fingerprint")
        if (
            not isinstance(application_release, str)
            or _FINGERPRINT.fullmatch(application_release) is None
            or application_release == "0" * 64
            or parameters.get("EnvironmentName") != environment_name
            or parameters.get("ReleaseFingerprint") != application_release
            or stack.get("StackName") != stack_name
            or stack.get("StackStatus") not in {"CREATE_COMPLETE", "UPDATE_COMPLETE"}
            or not isinstance(stack_id, str)
            or not stack_id.startswith(
                f"arn:{_partition(region)}:cloudformation:{region}:{account_id}:stack/{stack_name}/"
            )
        ):
            raise ValueError
    except Exception:
        raise ValueError("Phase 6 application release observation is invalid") from None


def verify_legacy_query_absence_observations(
    absence_observation: Mapping[str, object],
    alarm_observation: Mapping[str, object],
    log_group_observation: Mapping[str, object],
    *,
    environment_name: str,
) -> None:
    """Verify the conditioned-out Phase 7.4 query has no remaining physical surface."""

    try:
        if _ENVIRONMENT.fullmatch(environment_name) is None:
            raise ValueError
        legacy_function = f"mr-lister-phase7-{environment_name}-publication-status-query"
        legacy_role = f"{legacy_function}-role"
        if absence_observation != {
            "function_name": legacy_function,
            "get_function": {
                "error_code": "ResourceNotFoundException",
                "http_status_code": 404,
            },
            "get_role": {
                "error_code": "NoSuchEntity",
                "http_status_code": 404,
            },
            "role_name": legacy_role,
        }:
            raise ValueError
        if (
            alarm_observation.get("CompositeAlarms") != []
            or alarm_observation.get("MetricAlarms") != []
            or alarm_observation.get("LogAlarms") not in (None, [])
            or alarm_observation.get("NextToken") is not None
            or set(alarm_observation)
            - {"CompositeAlarms", "LogAlarms", "MetricAlarms", "NextToken"}
            or log_group_observation.get("logGroups") != []
            or log_group_observation.get("nextToken") is not None
            or set(log_group_observation) - {"logGroups", "nextToken"}
        ):
            raise ValueError
    except Exception:
        raise ValueError("Phase 7 legacy query absence observation is invalid") from None


def verify_lambda_configuration_observation(
    descriptor: Mapping[str, object],
    configuration_observation: Mapping[str, object],
    concurrency_observation: Mapping[str, object],
    *,
    archive_path: Path,
    environment_name: str,
    region: str,
    account_id: str,
) -> None:
    """Verify deployed Lambda identity, code bytes, role, concurrency, and closed environment."""

    try:
        _verify_identity_inputs(
            stack_name="Phase7Guard",
            environment_name=environment_name,
            bucket="aaa",
            key=f"phase7/releases/{descriptor['release_fingerprint']}/guard.zip",
            version_id="capture",
            region=region,
            account_id=account_id,
        )
        configuration_value = configuration_observation.get(
            "Configuration", configuration_observation
        )
        if not isinstance(configuration_value, Mapping):
            raise ValueError
        configuration = cast(Mapping[str, object], configuration_value)
        function_name = _function_name(environment_name)
        partition = _partition(region)
        raw_archive = archive_path.read_bytes()
        release = descriptor["release_fingerprint"]
        application_release = descriptor["application_release_fingerprint"]
        profile = descriptor["profile_fingerprint"]
        expected_environment = {
            "MR_LISTER_AWS_ACCOUNT_ID": account_id,
            "MR_LISTER_ENVIRONMENT": environment_name,
            "MR_LISTER_PHASE7_GUARD_ENABLED": "true",
            "MR_LISTER_PHASE7_GUARD_MODE": "approval_version_read_only",
            "MR_LISTER_PHASE7_GUARD_RELEASE_FINGERPRINT": release,
            "MR_LISTER_PHASE7_PUBLICATION_ENABLED": "false",
            "MR_LISTER_PHASE7_QUERY_ENABLED": "false",
            "MR_LISTER_PHASE7_REQUEST_ENABLED": "false",
            "MR_LISTER_PHASE7_SCAFFOLD_ONLY": "false",
            "MR_LISTER_PRODUCT_PROFILE_FINGERPRINT": profile,
            "MR_LISTER_PRODUCT_PROFILE_ID": "gildan_64000_swiftpod",
            "MR_LISTER_PRODUCT_PROFILE_PATH": (
                "/var/task/config/product_profiles/gildan_64000_swiftpod.json"
            ),
            "MR_LISTER_PRODUCT_PROFILE_VERSION": "2",
            "MR_LISTER_RELEASE_FINGERPRINT": application_release,
            "MR_LISTER_STATE_TABLE": f"mr-lister-phase6-{environment_name}",
        }
        environment = configuration.get("Environment")
        logging = configuration.get("LoggingConfig")
        vpc = configuration.get("VpcConfig", {})
        if (
            configuration.get("FunctionName") != function_name
            or configuration.get("FunctionArn")
            != f"arn:{partition}:lambda:{region}:{account_id}:function:{function_name}"
            or configuration.get("Runtime") != "python3.12"
            or configuration.get("Role")
            != f"arn:{partition}:iam::{account_id}:role/{function_name}-role"
            or configuration.get("Handler")
            != "mr_lister.cloud.phase7_guard_entrypoint.publication_guard_verification_handler"
            or configuration.get("CodeSize") != len(raw_archive)
            or configuration.get("CodeSha256")
            != base64.b64encode(sha256(raw_archive).digest()).decode("ascii")
            or configuration.get("Timeout") != 30
            or configuration.get("MemorySize") != 512
            or configuration.get("Version") != "$LATEST"
            or configuration.get("State") != "Active"
            or configuration.get("LastUpdateStatus") != "Successful"
            or configuration.get("PackageType") != "Zip"
            or configuration.get("Architectures") != ["arm64"]
            or profile != GUARD_PROFILE_FINGERPRINT
            or not isinstance(application_release, str)
            or _FINGERPRINT.fullmatch(application_release) is None
            or application_release == "0" * 64
            or not isinstance(environment, Mapping)
            or set(environment) != {"Variables"}
            or environment.get("Variables") != expected_environment
            or logging
            != {
                "ApplicationLogLevel": "ERROR",
                "LogFormat": "JSON",
                "LogGroup": f"/aws/lambda/{function_name}",
                "SystemLogLevel": "WARN",
            }
            or not isinstance(vpc, Mapping)
            or bool(vpc.get("VpcId"))
            or bool(vpc.get("SubnetIds"))
            or bool(vpc.get("SecurityGroupIds"))
            or configuration.get("Layers") not in (None, [])
            or configuration.get("FileSystemConfigs") not in (None, [])
            or configuration.get("DeadLetterConfig") not in (None, {})
            or concurrency_observation != {"ReservedConcurrentExecutions": 1}
        ):
            raise ValueError
    except Exception:
        raise ValueError("Phase 7 guard Lambda configuration observation is invalid") from None


def verify_iam_role_observations(
    role_observation: Mapping[str, object],
    inline_policy_observation: Mapping[str, object],
    inline_policy_list_observation: Mapping[str, object],
    attached_policy_list_observation: Mapping[str, object],
    *,
    environment_name: str,
    region: str,
    account_id: str,
) -> None:
    """Verify the deployed execution role retains only exact log and strong-read authority."""

    try:
        if (
            _ENVIRONMENT.fullmatch(environment_name) is None
            or _REGION.fullmatch(region) is None
            or _ACCOUNT.fullmatch(account_id) is None
        ):
            raise ValueError
        function_name = _function_name(environment_name)
        role_name = f"{function_name}-role"
        policy_name = "ReadOnlyApprovalPublicationGuard"
        role = role_observation.get("Role")
        if set(role_observation) != {"Role"} or not isinstance(role, Mapping):
            raise ValueError
        allowed_role_keys = {
            "Arn",
            "AssumeRolePolicyDocument",
            "CreateDate",
            "Description",
            "MaxSessionDuration",
            "Path",
            "RoleId",
            "RoleLastUsed",
            "RoleName",
            "Tags",
        }
        role_id = role.get("RoleId")
        create_date = role.get("CreateDate")
        if (
            set(role) - allowed_role_keys
            or role.get("Path") != "/"
            or role.get("Description") != ""
            or role.get("RoleName") != role_name
            or not isinstance(role_id, str)
            or re.fullmatch(r"[A-Z0-9]{16,128}", role_id) is None
            or role.get("Arn") != f"arn:{_partition(region)}:iam::{account_id}:role/{role_name}"
            or not isinstance(create_date, str)
            or re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9:.+-]+", create_date) is None
            or role.get("MaxSessionDuration") != 3600
            or role.get("AssumeRolePolicyDocument")
            != {
                "Statement": [
                    {
                        "Action": "sts:AssumeRole",
                        "Effect": "Allow",
                        "Principal": {"Service": "lambda.amazonaws.com"},
                    }
                ],
                "Version": "2012-10-17",
            }
            or _key_value_records(role.get("Tags"), "Key", "Value")
            != {
                "Environment": environment_name,
                "Phase": "7.6-read-only-guard",
                "Project": "MrLister",
            }
        ):
            raise ValueError
        expected_policy = {
            "Statement": [
                {
                    "Action": ["logs:CreateLogStream", "logs:PutLogEvents"],
                    "Effect": "Allow",
                    "Resource": (
                        f"arn:{_partition(region)}:logs:{region}:{account_id}:log-group:"
                        f"/aws/lambda/{function_name}:*:*"
                    ),
                    "Sid": "WritePublicationGuardLogs",
                },
                {
                    "Action": ["dynamodb:GetItem", "dynamodb:Query"],
                    "Condition": {
                        "ForAllValues:StringLike": {
                            "dynamodb:LeadingKeys": ["JOB#*", "PUBLICATION#*"]
                        }
                    },
                    "Effect": "Allow",
                    "Resource": (
                        f"arn:{_partition(region)}:dynamodb:{region}:{account_id}:table/"
                        f"mr-lister-phase6-{environment_name}"
                    ),
                    "Sid": "ReadExactApprovalPublicationAuthority",
                },
            ],
            "Version": "2012-10-17",
        }
        if inline_policy_observation != {
            "PolicyDocument": expected_policy,
            "PolicyName": policy_name,
            "RoleName": role_name,
        }:
            raise ValueError
        if (
            inline_policy_list_observation.get("PolicyNames") != [policy_name]
            or inline_policy_list_observation.get("IsTruncated") not in {None, False}
            or set(inline_policy_list_observation) - {"IsTruncated", "PolicyNames"}
        ):
            raise ValueError
        if (
            attached_policy_list_observation.get("AttachedPolicies") != []
            or attached_policy_list_observation.get("IsTruncated") not in {None, False}
            or set(attached_policy_list_observation) - {"AttachedPolicies", "IsTruncated"}
        ):
            raise ValueError
    except Exception:
        raise ValueError("Phase 7 guard IAM role observation is invalid") from None


def verify_lambda_surface_absence_observations(
    event_source_observation: Mapping[str, object],
    versions_observation: Mapping[str, object],
    aliases_observation: Mapping[str, object],
    url_configs_observation: Mapping[str, object],
    absence_observation: Mapping[str, object],
    *,
    environment_name: str,
) -> None:
    """Verify no asynchronous trigger, function URL, or Lambda resource policy exists."""

    try:
        if _ENVIRONMENT.fullmatch(environment_name) is None:
            raise ValueError
        function_name = _function_name(environment_name)
        versions = versions_observation.get("Versions")
        if (
            event_source_observation.get("EventSourceMappings") != []
            or event_source_observation.get("NextMarker") is not None
            or set(event_source_observation) - {"EventSourceMappings", "NextMarker"}
            or not isinstance(versions, list)
            or len(versions) != 1
            or not isinstance(versions[0], Mapping)
            or versions[0].get("FunctionName") != function_name
            or versions[0].get("Version") != "$LATEST"
            or versions_observation.get("NextMarker") is not None
            or set(versions_observation) - {"NextMarker", "Versions"}
            or aliases_observation.get("Aliases") != []
            or aliases_observation.get("NextMarker") is not None
            or set(aliases_observation) - {"Aliases", "NextMarker"}
            or url_configs_observation.get("FunctionUrlConfigs") != []
            or url_configs_observation.get("NextMarker") is not None
            or set(url_configs_observation) - {"FunctionUrlConfigs", "NextMarker"}
            or absence_observation
            != {
                "function_name": function_name,
                "get_function_event_invoke_config": {
                    "error_code": "ResourceNotFoundException",
                    "http_status_code": 404,
                },
                "get_function_url_config": {
                    "error_code": "ResourceNotFoundException",
                    "http_status_code": 404,
                },
                "get_policy": {
                    "error_code": "ResourceNotFoundException",
                    "http_status_code": 404,
                },
            }
        ):
            raise ValueError
    except Exception:
        raise ValueError("Phase 7 guard Lambda surface observation is invalid") from None


def verify_lambda_invocation_observation(
    descriptor: Mapping[str, object],
    request: Mapping[str, object],
    invocation: Mapping[str, object],
    payload: bytes,
    *,
    expected_outcome: str,
) -> None:
    """Verify an exact request/response pair from one direct synchronous Lambda invocation."""

    try:
        if expected_outcome == "sealed_configuration":
            if request != {"operation": "status"}:
                raise ValueError
            expected_operation = "status"
            expected_current: bool | None = None
        elif expected_outcome == "authority_rejected":
            if set(request) != {"aggregate_id", "operation", "owner_id"}:
                raise ValueError
            if (
                request.get("operation") != "verify_authority"
                or not isinstance(request.get("owner_id"), str)
                or _FINGERPRINT.fullmatch(cast(str, request["owner_id"])) is None
                or request.get("owner_id") == "0" * 64
                or not isinstance(request.get("aggregate_id"), str)
                or _SAFE_ID.fullmatch(cast(str, request["aggregate_id"])) is None
            ):
                raise ValueError
            expected_operation = "verify_authority"
            expected_current = False
        else:
            raise ValueError
        if invocation != {"ExecutedVersion": "$LATEST", "StatusCode": 200}:
            raise ValueError
        attestation = json.loads(payload)
        if not isinstance(attestation, Mapping):
            raise ValueError
        exact_keys = {
            "approval_authority_current",
            "approval_guard_enabled",
            "contract_fingerprint",
            "contract_version",
            "fingerprint",
            "guard_release_fingerprint",
            "operation",
            "outcome",
            "profile_fingerprint",
            "provider_calls_authorized",
            "publication_enabled",
            "query_enabled",
            "request_enabled",
        }
        fingerprint = attestation.get("fingerprint")
        unsigned = {key: value for key, value in attestation.items() if key != "fingerprint"}
        calculated = sha256(
            json.dumps(
                unsigned,
                allow_nan=False,
                ensure_ascii=True,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest()
        if (
            set(attestation) != exact_keys
            or attestation.get("contract_version") != "7.0.1"
            or attestation.get("contract_fingerprint") != _PHASE7_CONTRACT_FINGERPRINT
            or attestation.get("guard_release_fingerprint") != descriptor["release_fingerprint"]
            or attestation.get("profile_fingerprint") != descriptor["profile_fingerprint"]
            or attestation.get("operation") != expected_operation
            or attestation.get("outcome") != expected_outcome
            or attestation.get("approval_authority_current") is not expected_current
            or attestation.get("approval_guard_enabled") is not True
            or attestation.get("query_enabled") is not False
            or attestation.get("request_enabled") is not False
            or attestation.get("publication_enabled") is not False
            or type(attestation.get("provider_calls_authorized")) is not int
            or attestation.get("provider_calls_authorized") != 0
            or not isinstance(fingerprint, str)
            or fingerprint != calculated
        ):
            raise ValueError
    except Exception:
        raise ValueError("Phase 7 guard Lambda invocation observation is invalid") from None


def _verify_identity_inputs(
    *,
    stack_name: str,
    environment_name: str,
    bucket: str,
    key: str,
    version_id: str,
    region: str,
    account_id: str,
) -> None:
    if (
        _STACK.fullmatch(stack_name) is None
        or _ENVIRONMENT.fullmatch(environment_name) is None
        or _BUCKET.fullmatch(bucket) is None
        or _REGION.fullmatch(region) is None
        or _ACCOUNT.fullmatch(account_id) is None
        or not version_id
        or version_id != version_id.strip()
        or version_id.casefold() == "null"
        or len(version_id) > 1_024
    ):
        raise ValueError
    match = re.fullmatch(r"phase7/releases/(?P<release>[a-f0-9]{64})/guard\.zip", key)
    if match is None or match.group("release") == "0" * 64:
        raise ValueError


def _key_value_records(value: object, key_name: str, value_name: str) -> dict[str, str]:
    if not isinstance(value, list):
        raise ValueError
    result: dict[str, str] = {}
    for record in value:
        if not isinstance(record, Mapping):
            raise ValueError
        key = record.get(key_name)
        item = record.get(value_name)
        if not isinstance(key, str) or not isinstance(item, str) or key in result:
            raise ValueError
        result[key] = item
    return result


def _function_name(environment_name: str) -> str:
    return f"mr-lister-phase7-{environment_name}-guard-verification"


def _guard_stack_name(environment_name: str) -> str:
    return f"mr-lister-phase7-guard-{environment_name}"


def _partition(region: str) -> str:
    if region.startswith("us-gov-"):
        return "aws-us-gov"
    if region.startswith("cn-"):
        return "aws-cn"
    return "aws"


def _read_json_mapping(path: Path) -> Mapping[str, object]:
    if path.is_symlink() or not path.is_file() or not 1 <= path.stat().st_size <= 4 * 1024 * 1024:
        raise ValueError("Phase 7 guard deployment capture is invalid")
    value = json.loads(path.read_bytes())
    if not isinstance(value, Mapping):
        raise ValueError("Phase 7 guard deployment capture is invalid")
    return cast(Mapping[str, object], value)


def verify_s3_head_observation(
    descriptor: Mapping[str, object],
    observation: Mapping[str, object],
    *,
    archive_path: Path,
    bucket: str,
    key: str,
    version_id: str,
) -> None:
    """Verify one read-only ``head-object`` result against the sealed artifact identity."""

    try:
        if (
            _BUCKET.fullmatch(bucket) is None
            or not version_id
            or version_id != version_id.strip()
            or version_id.casefold() == "null"
            or len(version_id) > 1_024
        ):
            raise ValueError
        release = descriptor["release_fingerprint"]
        archive = descriptor["archive"]
        binding = descriptor["s3_binding"]
        if (
            not isinstance(release, str)
            or not isinstance(archive, Mapping)
            or not isinstance(binding, Mapping)
            or key != cast(str, binding["key_template"]).format(release_fingerprint=release)
        ):
            raise ValueError
        raw = archive_path.read_bytes()
        checksum = base64.b64encode(sha256(raw).digest()).decode("ascii")
        metadata = observation.get("Metadata")
        expected_metadata = {
            cast(str, binding["archive_sha256_metadata_key"]): archive["sha256"],
            cast(str, binding["release_fingerprint_metadata_key"]): release,
        }
        if (
            observation.get("VersionId") != version_id
            or observation.get("ContentLength") != len(raw)
            or observation.get("ChecksumSHA256") != checksum
            or observation.get("ContentType") != "application/zip"
            or observation.get("ServerSideEncryption") != binding["server_side_encryption"]
            or observation.get("DeleteMarker") not in {None, False}
            or metadata != expected_metadata
        ):
            raise ValueError
    except Exception:
        raise ValueError("Phase 7 guard S3 object observation is invalid") from None


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--deployment", type=Path, required=True)
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--descriptor", type=Path, required=True)
    parser.add_argument("--head-object-json", type=Path)
    parser.add_argument("--bucket")
    parser.add_argument("--key")
    parser.add_argument("--version-id")
    parser.add_argument("--stack-json", type=Path)
    parser.add_argument("--phase6-stack-json", type=Path)
    parser.add_argument("--stack-resources-json", type=Path)
    parser.add_argument("--lambda-configuration-json", type=Path)
    parser.add_argument("--lambda-concurrency-json", type=Path)
    parser.add_argument("--iam-role-json", type=Path)
    parser.add_argument("--iam-inline-policy-json", type=Path)
    parser.add_argument("--iam-inline-policy-list-json", type=Path)
    parser.add_argument("--iam-attached-policy-list-json", type=Path)
    parser.add_argument("--event-source-mappings-json", type=Path)
    parser.add_argument("--lambda-versions-json", type=Path)
    parser.add_argument("--lambda-aliases-json", type=Path)
    parser.add_argument("--lambda-url-configs-json", type=Path)
    parser.add_argument("--lambda-absence-json", type=Path)
    parser.add_argument("--legacy-query-absence-json", type=Path)
    parser.add_argument("--legacy-query-alarms-json", type=Path)
    parser.add_argument("--legacy-query-log-groups-json", type=Path)
    parser.add_argument("--status-request-json", type=Path)
    parser.add_argument("--status-invocation-json", type=Path)
    parser.add_argument("--status-payload", type=Path)
    parser.add_argument("--rejected-request-json", type=Path)
    parser.add_argument("--rejected-invocation-json", type=Path)
    parser.add_argument("--rejected-payload", type=Path)
    parser.add_argument("--stack-name")
    parser.add_argument("--environment-name")
    parser.add_argument("--region")
    parser.add_argument("--account-id")
    arguments = parser.parse_args()
    descriptor = verify_guard_deployment_artifact(
        arguments.deployment,
        archive_path=arguments.archive,
        descriptor_path=arguments.descriptor,
    )
    print(descriptor["release_fingerprint"])
    print(descriptor["archive"]["sha256"])
    head_values = (
        arguments.head_object_json,
        arguments.bucket,
        arguments.key,
        arguments.version_id,
    )
    if any(value is not None for value in head_values):
        if any(value is None for value in head_values):
            parser.error(
                "S3 verification requires --head-object-json, --bucket, --key, and --version-id"
            )
        verify_s3_head_observation(
            descriptor,
            _read_json_mapping(arguments.head_object_json),
            archive_path=arguments.archive,
            bucket=arguments.bucket,
            key=arguments.key,
            version_id=arguments.version_id,
        )
        print(arguments.version_id)

    deployment_values = (
        arguments.stack_json,
        arguments.phase6_stack_json,
        arguments.stack_resources_json,
        arguments.lambda_configuration_json,
        arguments.lambda_concurrency_json,
        arguments.iam_role_json,
        arguments.iam_inline_policy_json,
        arguments.iam_inline_policy_list_json,
        arguments.iam_attached_policy_list_json,
        arguments.event_source_mappings_json,
        arguments.lambda_versions_json,
        arguments.lambda_aliases_json,
        arguments.lambda_url_configs_json,
        arguments.lambda_absence_json,
        arguments.legacy_query_absence_json,
        arguments.legacy_query_alarms_json,
        arguments.legacy_query_log_groups_json,
        arguments.status_request_json,
        arguments.status_invocation_json,
        arguments.status_payload,
        arguments.rejected_request_json,
        arguments.rejected_invocation_json,
        arguments.rejected_payload,
        arguments.stack_name,
        arguments.environment_name,
        arguments.region,
        arguments.account_id,
    )
    if any(value is not None for value in deployment_values):
        if any(value is None for value in deployment_values) or any(
            value is None for value in head_values
        ):
            parser.error(
                "full deployment verification requires every stack, Lambda, invocation, identity, "
                "and S3 capture argument"
            )
        verify_stack_observation(
            descriptor,
            _read_json_mapping(arguments.stack_json),
            stack_name=arguments.stack_name,
            environment_name=arguments.environment_name,
            bucket=arguments.bucket,
            key=arguments.key,
            version_id=arguments.version_id,
            region=arguments.region,
            account_id=arguments.account_id,
        )
        verify_phase6_application_release_observation(
            descriptor,
            _read_json_mapping(arguments.phase6_stack_json),
            environment_name=arguments.environment_name,
            region=arguments.region,
            account_id=arguments.account_id,
        )
        verify_stack_resources_observation(
            _read_json_mapping(arguments.stack_resources_json),
            environment_name=arguments.environment_name,
            region=arguments.region,
            account_id=arguments.account_id,
        )
        verify_lambda_configuration_observation(
            descriptor,
            _read_json_mapping(arguments.lambda_configuration_json),
            _read_json_mapping(arguments.lambda_concurrency_json),
            archive_path=arguments.archive,
            environment_name=arguments.environment_name,
            region=arguments.region,
            account_id=arguments.account_id,
        )
        verify_iam_role_observations(
            _read_json_mapping(arguments.iam_role_json),
            _read_json_mapping(arguments.iam_inline_policy_json),
            _read_json_mapping(arguments.iam_inline_policy_list_json),
            _read_json_mapping(arguments.iam_attached_policy_list_json),
            environment_name=arguments.environment_name,
            region=arguments.region,
            account_id=arguments.account_id,
        )
        verify_lambda_surface_absence_observations(
            _read_json_mapping(arguments.event_source_mappings_json),
            _read_json_mapping(arguments.lambda_versions_json),
            _read_json_mapping(arguments.lambda_aliases_json),
            _read_json_mapping(arguments.lambda_url_configs_json),
            _read_json_mapping(arguments.lambda_absence_json),
            environment_name=arguments.environment_name,
        )
        verify_legacy_query_absence_observations(
            _read_json_mapping(arguments.legacy_query_absence_json),
            _read_json_mapping(arguments.legacy_query_alarms_json),
            _read_json_mapping(arguments.legacy_query_log_groups_json),
            environment_name=arguments.environment_name,
        )
        verify_lambda_invocation_observation(
            descriptor,
            _read_json_mapping(arguments.status_request_json),
            _read_json_mapping(arguments.status_invocation_json),
            arguments.status_payload.read_bytes(),
            expected_outcome="sealed_configuration",
        )
        verify_lambda_invocation_observation(
            descriptor,
            _read_json_mapping(arguments.rejected_request_json),
            _read_json_mapping(arguments.rejected_invocation_json),
            arguments.rejected_payload.read_bytes(),
            expected_outcome="authority_rejected",
        )
        print(_function_name(arguments.environment_name))


if __name__ == "__main__":
    main()
