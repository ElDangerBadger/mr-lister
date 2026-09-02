"""Verify an exact mode-bound Phase 7 canary deployment from captured AWS JSON.

This tool is provider-free: it reads a sealed local artifact and unedited read-only capture files.
It never imports an AWS SDK, starts a subprocess, calls AWS, or mutates deployment state.
"""

from __future__ import annotations

import argparse
import base64
import json
import re
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from hashlib import sha256
from pathlib import Path
from typing import cast

from mr_lister.release.phase7_canary import CANARY_ENTRYPOINT, CANARY_PROFILE_FINGERPRINT
from tools.build_phase711_canary_release import verify_canary_deployment_artifact

_ACCOUNT = re.compile(r"^[0-9]{12}$")
_BUCKET = re.compile(r"^[a-z0-9](?:[a-z0-9.-]{1,61}[a-z0-9])$")
_ENVIRONMENT = re.compile(r"^[a-z][a-z0-9-]{1,15}$")
_FINGERPRINT = re.compile(r"^[a-f0-9]{64}$")
_REGION = re.compile(r"^[a-z]{2}(?:-[a-z0-9]+){1,2}-[1-9][0-9]?$|^us-gov-[a-z]+-[1-9]$")
_SECRET_NAME = re.compile(r"^mr-lister/[A-Za-z0-9/_-]+-[A-Za-z0-9]{6}$")
_VERSION = re.compile(r"^[A-Za-z0-9._~+/=-]{3,1024}$")
_CANARY_MODES = frozenset({"publish_once", "read_only_preflight"})
_GENERIC_ERROR = "Phase 7 canary deployment observation is invalid"


class Phase716CanaryDeploymentError(RuntimeError):
    """Value-free refusal for incomplete, expanded, or drifting deployment evidence."""


@dataclass(frozen=True, slots=True)
class Phase716CanaryDeploymentBinding:
    """Sanitized identity of one fully verified exact-mode canary deployment."""

    application_release_fingerprint: str
    archive_fingerprint: str
    binding_fingerprint: str
    binding_mode: str
    function_name: str
    release_fingerprint: str
    stack_name: str


def verify_canary_deployment_observations(
    *,
    deployment_root: Path,
    archive_path: Path,
    descriptor_path: Path,
    head_observation: Mapping[str, object],
    stack_observation: Mapping[str, object],
    stack_resources_observation: Mapping[str, object],
    lambda_configuration_observation: Mapping[str, object],
    lambda_concurrency_observation: Mapping[str, object],
    role_observation: Mapping[str, object],
    inline_policy_observation: Mapping[str, object],
    inline_policy_list_observation: Mapping[str, object],
    attached_policy_list_observation: Mapping[str, object],
    event_source_observation: Mapping[str, object],
    versions_observation: Mapping[str, object],
    aliases_observation: Mapping[str, object],
    url_configs_observation: Mapping[str, object],
    absence_observation: Mapping[str, object],
    expected_mode: str,
    stack_name: str,
    environment_name: str,
    bucket: str,
    key: str,
    version_id: str,
    printify_secret_arn: str,
    region: str,
    account_id: str,
) -> Phase716CanaryDeploymentBinding:
    """Verify the artifact and complete immutable three-resource exact-mode readback."""

    try:
        descriptor = verify_canary_deployment_artifact(
            deployment_root,
            archive_path=archive_path,
            descriptor_path=descriptor_path,
        )
        release, application, binding, mode, archive_fingerprint = _descriptor_authority(
            descriptor,
            expected_mode=expected_mode,
        )
        _verify_identity_inputs(
            stack_name=stack_name,
            environment_name=environment_name,
            bucket=bucket,
            key=key,
            version_id=version_id,
            printify_secret_arn=printify_secret_arn,
            region=region,
            account_id=account_id,
            release_fingerprint=release,
        )
        verify_s3_head_observation(
            descriptor,
            head_observation,
            archive_path=archive_path,
            bucket=bucket,
            key=key,
            version_id=version_id,
            expected_mode=expected_mode,
        )
        verify_stack_observation(
            descriptor,
            stack_observation,
            stack_name=stack_name,
            environment_name=environment_name,
            bucket=bucket,
            key=key,
            version_id=version_id,
            printify_secret_arn=printify_secret_arn,
            expected_mode=expected_mode,
            region=region,
            account_id=account_id,
        )
        verify_stack_resources_observation(
            stack_resources_observation,
            environment_name=environment_name,
            region=region,
            account_id=account_id,
        )
        verify_lambda_configuration_observation(
            descriptor,
            lambda_configuration_observation,
            lambda_concurrency_observation,
            archive_path=archive_path,
            environment_name=environment_name,
            printify_secret_arn=printify_secret_arn,
            expected_mode=expected_mode,
            region=region,
            account_id=account_id,
        )
        verify_iam_role_observations(
            role_observation,
            inline_policy_observation,
            inline_policy_list_observation,
            attached_policy_list_observation,
            environment_name=environment_name,
            printify_secret_arn=printify_secret_arn,
            region=region,
            account_id=account_id,
        )
        verify_lambda_surface_absence_observations(
            event_source_observation,
            versions_observation,
            aliases_observation,
            url_configs_observation,
            absence_observation,
            environment_name=environment_name,
        )
        return Phase716CanaryDeploymentBinding(
            application_release_fingerprint=application,
            archive_fingerprint=archive_fingerprint,
            binding_fingerprint=binding,
            binding_mode=mode,
            function_name=_function_name(environment_name),
            release_fingerprint=release,
            stack_name=stack_name,
        )
    except Exception:
        raise Phase716CanaryDeploymentError(_GENERIC_ERROR) from None


def verify_s3_head_observation(
    descriptor: Mapping[str, object],
    observation: Mapping[str, object],
    *,
    archive_path: Path,
    bucket: str,
    key: str,
    version_id: str,
    expected_mode: str,
) -> None:
    """Verify one checksum-enabled immutable S3 object-version observation."""

    try:
        release, _application, _binding, _mode, archive_fingerprint = _descriptor_authority(
            descriptor,
            expected_mode=expected_mode,
        )
        _verify_object_inputs(
            bucket=bucket,
            key=key,
            version_id=version_id,
            release_fingerprint=release,
        )
        archive = descriptor.get("archive")
        s3_binding = descriptor.get("s3_binding")
        if not isinstance(archive, Mapping) or not isinstance(s3_binding, Mapping):
            raise ValueError
        raw_archive = archive_path.read_bytes()
        expected_metadata = {
            cast(str, s3_binding["archive_sha256_metadata_key"]): archive_fingerprint,
            cast(str, s3_binding["release_fingerprint_metadata_key"]): release,
        }
        if (
            key != cast(str, s3_binding["key_template"]).format(release_fingerprint=release)
            or observation.get("VersionId") != version_id
            or observation.get("ContentLength") != len(raw_archive)
            or observation.get("ChecksumSHA256")
            != base64.b64encode(sha256(raw_archive).digest()).decode("ascii")
            or observation.get("ContentType") != "application/zip"
            or observation.get("ServerSideEncryption") != s3_binding["server_side_encryption"]
            or observation.get("DeleteMarker") not in {None, False}
            or observation.get("Metadata") != expected_metadata
        ):
            raise ValueError
    except Exception:
        raise Phase716CanaryDeploymentError(_GENERIC_ERROR) from None


def verify_stack_observation(
    descriptor: Mapping[str, object],
    observation: Mapping[str, object],
    *,
    stack_name: str,
    environment_name: str,
    bucket: str,
    key: str,
    version_id: str,
    printify_secret_arn: str,
    expected_mode: str,
    region: str,
    account_id: str,
) -> None:
    """Verify one exact successful CloudFormation canary stack observation."""

    try:
        release, application, binding, mode, _archive = _descriptor_authority(
            descriptor,
            expected_mode=expected_mode,
        )
        _verify_identity_inputs(
            stack_name=stack_name,
            environment_name=environment_name,
            bucket=bucket,
            key=key,
            version_id=version_id,
            printify_secret_arn=printify_secret_arn,
            region=region,
            account_id=account_id,
            release_fingerprint=release,
        )
        stacks = observation.get("Stacks")
        if set(observation) != {"Stacks"} or not isinstance(stacks, list) or len(stacks) != 1:
            raise ValueError
        stack = stacks[0]
        if not isinstance(stack, Mapping):
            raise ValueError
        function_name = _function_name(environment_name)
        expected_parameters = {
            "ApplicationReleaseFingerprint": application,
            "CanaryBindingFingerprint": binding,
            "CanaryCodeS3Bucket": bucket,
            "CanaryCodeS3Key": key,
            "CanaryCodeS3ObjectVersion": version_id,
            "CanaryMode": mode,
            "CanaryReleaseFingerprint": release,
            "EnvironmentName": environment_name,
            "PrintifySecretArn": printify_secret_arn,
        }
        expected_outputs = {
            "ApplicationReleaseFingerprint": application,
            "PublicationCanaryBindingFingerprint": binding,
            "PublicationCanaryFunctionArn": (
                f"arn:{_partition(region)}:lambda:{region}:{account_id}:function:{function_name}"
            ),
            "PublicationCanaryMode": mode,
            "PublicationCanaryReleaseFingerprint": release,
            "SellerPublicationEnabled": "false",
        }
        stack_id = stack.get("StackId")
        if (
            stack_name != _stack_name(environment_name)
            or stack.get("StackName") != stack_name
            or stack.get("StackStatus") not in {"CREATE_COMPLETE", "UPDATE_COMPLETE"}
            or not isinstance(stack_id, str)
            or not stack_id.startswith(
                f"arn:{_partition(region)}:cloudformation:{region}:{account_id}:stack/{stack_name}/"
            )
            or _key_value_records(stack.get("Parameters"), "ParameterKey", "ParameterValue")
            != expected_parameters
            or _key_value_records(stack.get("Outputs"), "OutputKey", "OutputValue")
            != expected_outputs
        ):
            raise ValueError
    except Exception:
        raise Phase716CanaryDeploymentError(_GENERIC_ERROR) from None


def verify_stack_resources_observation(
    observation: Mapping[str, object],
    *,
    environment_name: str,
    region: str,
    account_id: str,
) -> None:
    """Verify exactly one log group, one execution role, and one Lambda function."""

    try:
        _verify_location(environment_name, region, account_id)
        summaries = observation.get("StackResourceSummaries")
        if (
            not isinstance(summaries, list)
            or len(summaries) != 3
            or observation.get("NextToken") is not None
            or set(observation) - {"NextToken", "StackResourceSummaries"}
        ):
            raise ValueError
        function_name = _function_name(environment_name)
        expected = {
            "PublicationCanaryFunction": ("AWS::Lambda::Function", function_name),
            "PublicationCanaryFunctionRole": ("AWS::IAM::Role", f"{function_name}-role"),
            "PublicationCanaryLogGroup": (
                "AWS::Logs::LogGroup",
                f"/aws/lambda/{function_name}",
            ),
        }
        observed: dict[str, tuple[object, object]] = {}
        for summary in summaries:
            if not isinstance(summary, Mapping):
                raise ValueError
            logical_id = summary.get("LogicalResourceId")
            if (
                not isinstance(logical_id, str)
                or logical_id in observed
                or summary.get("ResourceStatus") not in {"CREATE_COMPLETE", "UPDATE_COMPLETE"}
            ):
                raise ValueError
            observed[logical_id] = (
                summary.get("ResourceType"),
                summary.get("PhysicalResourceId"),
            )
        if observed != expected:
            raise ValueError
    except Exception:
        raise Phase716CanaryDeploymentError(_GENERIC_ERROR) from None


def verify_lambda_configuration_observation(
    descriptor: Mapping[str, object],
    configuration_observation: Mapping[str, object],
    concurrency_observation: Mapping[str, object],
    *,
    archive_path: Path,
    environment_name: str,
    printify_secret_arn: str,
    expected_mode: str,
    region: str,
    account_id: str,
) -> None:
    """Verify exact code, runtime, environment, role, and concurrency-one authority."""

    try:
        _verify_location(environment_name, region, account_id)
        _verify_secret_arn(printify_secret_arn, region=region, account_id=account_id)
        release, application, binding, mode, _archive = _descriptor_authority(
            descriptor,
            expected_mode=expected_mode,
        )
        configuration_value = configuration_observation.get(
            "Configuration", configuration_observation
        )
        if not isinstance(configuration_value, Mapping):
            raise ValueError
        configuration = cast(Mapping[str, object], configuration_value)
        function_name = _function_name(environment_name)
        raw_archive = archive_path.read_bytes()
        expected_environment = {
            "MR_LISTER_AWS_ACCOUNT_ID": account_id,
            "MR_LISTER_ENVIRONMENT": environment_name,
            "MR_LISTER_PHASE7_CANARY_BINDING_FINGERPRINT": binding,
            "MR_LISTER_PHASE7_CANARY_ENABLED": "true",
            "MR_LISTER_PHASE7_CANARY_MODE": mode,
            "MR_LISTER_PHASE7_CANARY_RELEASE_FINGERPRINT": release,
            "MR_LISTER_PHASE7_PUBLICATION_ENABLED": "false",
            "MR_LISTER_PHASE7_QUERY_ENABLED": "false",
            "MR_LISTER_PHASE7_REQUEST_ENABLED": "false",
            "MR_LISTER_PHASE7_SCAFFOLD_ONLY": "false",
            "MR_LISTER_PRINTIFY_SECRET_ARN": printify_secret_arn,
            "MR_LISTER_PRODUCT_PROFILE_FINGERPRINT": CANARY_PROFILE_FINGERPRINT,
            "MR_LISTER_PRODUCT_PROFILE_ID": "gildan_64000_swiftpod",
            "MR_LISTER_PRODUCT_PROFILE_PATH": (
                "/var/task/config/product_profiles/gildan_64000_swiftpod.json"
            ),
            "MR_LISTER_PRODUCT_PROFILE_VERSION": "2",
            "MR_LISTER_RELEASE_FINGERPRINT": application,
            "MR_LISTER_STATE_TABLE": f"mr-lister-phase6-{environment_name}",
        }
        environment = configuration.get("Environment")
        vpc = configuration.get("VpcConfig", {})
        if (
            configuration.get("FunctionName") != function_name
            or configuration.get("FunctionArn")
            != f"arn:{_partition(region)}:lambda:{region}:{account_id}:function:{function_name}"
            or configuration.get("Runtime") != "python3.12"
            or configuration.get("Role")
            != f"arn:{_partition(region)}:iam::{account_id}:role/{function_name}-role"
            or configuration.get("Handler") != CANARY_ENTRYPOINT
            or configuration.get("CodeSize") != len(raw_archive)
            or configuration.get("CodeSha256")
            != base64.b64encode(sha256(raw_archive).digest()).decode("ascii")
            or configuration.get("Timeout") != 60
            or configuration.get("MemorySize") != 512
            or configuration.get("Version") != "$LATEST"
            or configuration.get("State") != "Active"
            or configuration.get("LastUpdateStatus") != "Successful"
            or configuration.get("PackageType") != "Zip"
            or configuration.get("Architectures") != ["arm64"]
            or not isinstance(environment, Mapping)
            or set(environment) != {"Variables"}
            or environment.get("Variables") != expected_environment
            or configuration.get("LoggingConfig")
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
            or configuration.get("KMSKeyArn") not in (None, "")
            or concurrency_observation != {"ReservedConcurrentExecutions": 1}
        ):
            raise ValueError
    except Exception:
        raise Phase716CanaryDeploymentError(_GENERIC_ERROR) from None


def verify_iam_role_observations(
    role_observation: Mapping[str, object],
    inline_policy_observation: Mapping[str, object],
    inline_policy_list_observation: Mapping[str, object],
    attached_policy_list_observation: Mapping[str, object],
    *,
    environment_name: str,
    printify_secret_arn: str,
    region: str,
    account_id: str,
) -> None:
    """Verify the canary role has only its exact log/state/credential authority."""

    try:
        _verify_location(environment_name, region, account_id)
        _verify_secret_arn(printify_secret_arn, region=region, account_id=account_id)
        function_name = _function_name(environment_name)
        role_name = f"{function_name}-role"
        role = role_observation.get("Role")
        if set(role_observation) != {"Role"} or not isinstance(role, Mapping):
            raise ValueError
        role_id = role.get("RoleId")
        create_date = role.get("CreateDate")
        if (
            set(role)
            - {
                "Arn",
                "AssumeRolePolicyDocument",
                "CreateDate",
                "MaxSessionDuration",
                "Path",
                "RoleId",
                "RoleLastUsed",
                "RoleName",
                "Tags",
            }
            or role.get("Path") != "/"
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
                "Phase": "7-isolated-canary",
                "Project": "MrLister",
            }
        ):
            raise ValueError
        table_arn = (
            f"arn:{_partition(region)}:dynamodb:{region}:{account_id}:"
            f"table/mr-lister-phase6-{environment_name}"
        )
        leading_keys = {
            "ForAllValues:StringLike": {"dynamodb:LeadingKeys": ["JOB#*", "PUBLICATION#*"]}
        }
        expected_policy = {
            "Statement": [
                {
                    "Action": ["logs:CreateLogStream", "logs:PutLogEvents"],
                    "Effect": "Allow",
                    "Resource": (
                        f"arn:{_partition(region)}:logs:{region}:{account_id}:"
                        f"log-group:/aws/lambda/{function_name}:*"
                    ),
                    "Sid": "WritePublicationCanaryLogs",
                },
                {
                    "Action": ["dynamodb:GetItem", "dynamodb:Query"],
                    "Condition": leading_keys,
                    "Effect": "Allow",
                    "Resource": table_arn,
                    "Sid": "ReadExactPublicationAuthority",
                },
                {
                    "Action": "dynamodb:ConditionCheckItem",
                    "Condition": leading_keys,
                    "Effect": "Allow",
                    "Resource": table_arn,
                    "Sid": "CommitExactPublicationAuthorityConditionChecks",
                },
                {
                    "Action": "dynamodb:PutItem",
                    "Condition": {
                        **leading_keys,
                        "ForAnyValue:StringEquals": {
                            "dynamodb:EnclosingOperation": ["TransactWriteItems"]
                        },
                    },
                    "Effect": "Allow",
                    "Resource": table_arn,
                    "Sid": "CommitExactPublicationAuthorityPuts",
                },
                {
                    "Action": "secretsmanager:GetSecretValue",
                    "Effect": "Allow",
                    "Resource": printify_secret_arn,
                    "Sid": "ReadExactPublicationCredential",
                },
            ],
            "Version": "2012-10-17",
        }
        policy_name = "ExactBoundPublicationCanary"
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
        raise Phase716CanaryDeploymentError(_GENERIC_ERROR) from None


def verify_lambda_surface_absence_observations(
    event_source_observation: Mapping[str, object],
    versions_observation: Mapping[str, object],
    aliases_observation: Mapping[str, object],
    url_configs_observation: Mapping[str, object],
    absence_observation: Mapping[str, object],
    *,
    environment_name: str,
) -> None:
    """Verify no trigger, published version, alias, URL, or resource policy exists."""

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
        raise Phase716CanaryDeploymentError(_GENERIC_ERROR) from None


def _descriptor_authority(
    descriptor: Mapping[str, object],
    *,
    expected_mode: str,
) -> tuple[str, str, str, str, str]:
    release = _required_fingerprint(descriptor, "release_fingerprint")
    application = _required_fingerprint(descriptor, "application_release_fingerprint")
    binding = _required_fingerprint(descriptor, "binding_fingerprint")
    archive = descriptor.get("archive")
    if not isinstance(archive, Mapping):
        raise ValueError
    archive_fingerprint = _required_fingerprint(archive, "sha256")
    mode = descriptor.get("binding_mode")
    if (
        expected_mode not in _CANARY_MODES
        or mode != expected_mode
        or descriptor.get("profile_fingerprint") != CANARY_PROFILE_FINGERPRINT
    ):
        raise ValueError
    return release, application, binding, cast(str, mode), archive_fingerprint


def _required_fingerprint(value: Mapping[str, object], name: str) -> str:
    fingerprint = value.get(name)
    if (
        not isinstance(fingerprint, str)
        or _FINGERPRINT.fullmatch(fingerprint) is None
        or fingerprint == "0" * 64
    ):
        raise ValueError
    return fingerprint


def _verify_identity_inputs(
    *,
    stack_name: str,
    environment_name: str,
    bucket: str,
    key: str,
    version_id: str,
    printify_secret_arn: str,
    region: str,
    account_id: str,
    release_fingerprint: str,
) -> None:
    _verify_location(environment_name, region, account_id)
    _verify_object_inputs(
        bucket=bucket,
        key=key,
        version_id=version_id,
        release_fingerprint=release_fingerprint,
    )
    _verify_secret_arn(printify_secret_arn, region=region, account_id=account_id)
    if stack_name != _stack_name(environment_name):
        raise ValueError


def _verify_location(environment_name: str, region: str, account_id: str) -> None:
    if (
        _ENVIRONMENT.fullmatch(environment_name) is None
        or _REGION.fullmatch(region) is None
        or _ACCOUNT.fullmatch(account_id) is None
    ):
        raise ValueError


def _verify_object_inputs(
    *,
    bucket: str,
    key: str,
    version_id: str,
    release_fingerprint: str,
) -> None:
    if (
        _BUCKET.fullmatch(bucket) is None
        or key != f"phase7/releases/{release_fingerprint}/canary.zip"
        or _VERSION.fullmatch(version_id) is None
        or version_id.casefold()
        in {"pending", "current", "default", "latest", "moving", "null", "none", "unversioned"}
        or len(version_id) > 1_024
    ):
        raise ValueError


def _verify_secret_arn(secret_arn: str, *, region: str, account_id: str) -> None:
    prefix = f"arn:{_partition(region)}:secretsmanager:{region}:{account_id}:secret:"
    if (
        not secret_arn.startswith(prefix)
        or _SECRET_NAME.fullmatch(secret_arn[len(prefix) :]) is None
    ):
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


def _partition(region: str) -> str:
    if region.startswith("us-gov-"):
        return "aws-us-gov"
    if region.startswith("cn-"):
        return "aws-cn"
    return "aws"


def _stack_name(environment_name: str) -> str:
    return f"mr-lister-phase7-canary-{environment_name}"


def _function_name(environment_name: str) -> str:
    return f"mr-lister-phase7-{environment_name}-publication-canary"


def _read_json_mapping(path: Path) -> Mapping[str, object]:
    try:
        if (
            path.is_symlink()
            or not path.is_file()
            or not 1 <= path.stat().st_size <= 4 * 1024 * 1024
        ):
            raise ValueError
        value = json.loads(path.read_bytes())
        if not isinstance(value, Mapping):
            raise ValueError
        return cast(Mapping[str, object], value)
    except Exception:
        raise Phase716CanaryDeploymentError(_GENERIC_ERROR) from None


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--deployment", type=Path, required=True)
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--descriptor", type=Path, required=True)
    parser.add_argument("--head-object-json", type=Path, required=True)
    parser.add_argument("--stack-json", type=Path, required=True)
    parser.add_argument("--stack-resources-json", type=Path, required=True)
    parser.add_argument("--lambda-configuration-json", type=Path, required=True)
    parser.add_argument("--lambda-concurrency-json", type=Path, required=True)
    parser.add_argument("--iam-role-json", type=Path, required=True)
    parser.add_argument("--iam-inline-policy-json", type=Path, required=True)
    parser.add_argument("--iam-inline-policy-list-json", type=Path, required=True)
    parser.add_argument("--iam-attached-policy-list-json", type=Path, required=True)
    parser.add_argument("--event-source-mappings-json", type=Path, required=True)
    parser.add_argument("--lambda-versions-json", type=Path, required=True)
    parser.add_argument("--lambda-aliases-json", type=Path, required=True)
    parser.add_argument("--lambda-url-configs-json", type=Path, required=True)
    parser.add_argument("--lambda-absence-json", type=Path, required=True)
    parser.add_argument("--expected-mode", choices=sorted(_CANARY_MODES), required=True)
    parser.add_argument("--stack-name", required=True)
    parser.add_argument("--environment-name", required=True)
    parser.add_argument("--bucket", required=True)
    parser.add_argument("--key", required=True)
    parser.add_argument("--version-id", required=True)
    parser.add_argument("--printify-secret-arn", required=True)
    parser.add_argument("--region", required=True)
    parser.add_argument("--account-id", required=True)
    arguments = parser.parse_args()

    result = verify_canary_deployment_observations(
        deployment_root=arguments.deployment,
        archive_path=arguments.archive,
        descriptor_path=arguments.descriptor,
        head_observation=_read_json_mapping(arguments.head_object_json),
        stack_observation=_read_json_mapping(arguments.stack_json),
        stack_resources_observation=_read_json_mapping(arguments.stack_resources_json),
        lambda_configuration_observation=_read_json_mapping(arguments.lambda_configuration_json),
        lambda_concurrency_observation=_read_json_mapping(arguments.lambda_concurrency_json),
        role_observation=_read_json_mapping(arguments.iam_role_json),
        inline_policy_observation=_read_json_mapping(arguments.iam_inline_policy_json),
        inline_policy_list_observation=_read_json_mapping(arguments.iam_inline_policy_list_json),
        attached_policy_list_observation=_read_json_mapping(
            arguments.iam_attached_policy_list_json
        ),
        event_source_observation=_read_json_mapping(arguments.event_source_mappings_json),
        versions_observation=_read_json_mapping(arguments.lambda_versions_json),
        aliases_observation=_read_json_mapping(arguments.lambda_aliases_json),
        url_configs_observation=_read_json_mapping(arguments.lambda_url_configs_json),
        absence_observation=_read_json_mapping(arguments.lambda_absence_json),
        expected_mode=arguments.expected_mode,
        stack_name=arguments.stack_name,
        environment_name=arguments.environment_name,
        bucket=arguments.bucket,
        key=arguments.key,
        version_id=arguments.version_id,
        printify_secret_arn=arguments.printify_secret_arn,
        region=arguments.region,
        account_id=arguments.account_id,
    )
    print(json.dumps(asdict(result), sort_keys=True, separators=(",", ":")))


if __name__ == "__main__":
    main()


__all__ = [
    "Phase716CanaryDeploymentBinding",
    "Phase716CanaryDeploymentError",
    "verify_canary_deployment_observations",
    "verify_iam_role_observations",
    "verify_lambda_configuration_observation",
    "verify_lambda_surface_absence_observations",
    "verify_s3_head_observation",
    "verify_stack_observation",
    "verify_stack_resources_observation",
]
