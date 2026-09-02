"""Verify the P7.15C provider-free operations update and rollback captures offline.

The verifier imports no AWS SDK and starts no subprocess.  It consumes unedited read-only AWS
JSON captures plus the locally sealed artifact and emits only deterministic fingerprints and
counts.  Dynamic resource identifiers and seller configuration are never copied into its result.
"""

from __future__ import annotations

import argparse
import base64
import json
import re
from collections.abc import Mapping, Sequence
from copy import deepcopy
from hashlib import sha256
from pathlib import Path
from typing import Any, Final, cast

from tools.build_phase715c_operations_release import verify_operations_deployment_artifact
from tools.render_phase715c_operations_update import (
    BASE_TEMPLATE,
    BASE_TEMPLATE_SHA256,
    OPERATIONS_ARCHIVE_KEY,
    RECOVERY_HANDLER,
    RETENTION_HANDLER,
    render_operations_update_template,
    verify_operations_update_template,
)

ACCOUNT_ID: Final = "384627057108"
REGION: Final = "us-west-2"
ENVIRONMENT_NAME: Final = "dev"
STACK_NAME: Final = "mr-lister-phase7-dev"
STATE_TABLE_NAME: Final = "mr-lister-phase6-dev"
PHASE6_STACK_NAME: Final = STATE_TABLE_NAME
PUBLICATION_WORKFLOW_ARN: Final = (
    "arn:aws:states:us-west-2:384627057108:stateMachine:mr-lister-phase7-dev-publication"
)
PREDECESSOR_RELEASE_FINGERPRINT: Final = (
    "9c4deca1813e5d1e8cc3f6747681b2194265f9c0b51b64fd9cf6b8afeb823c46"
)
PREDECESSOR_ARCHIVE_SHA256: Final = (
    "43721a48802bd3bbc946671aff938b6df030b495975c8bc59839db18986da88f"
)
PREDECESSOR_ARCHIVE_SIZE_BYTES: Final = 62_982_212
PREDECESSOR_CODE_S3_KEY: Final = (
    f"phase7/candidates/{PREDECESSOR_RELEASE_FINGERPRINT}/production-disabled.zip"
)
PREDECESSOR_CODE_S3_OBJECT_VERSION: Final = "6ix.miylQqgEZyV392IenODAlQvbAp4F"
PREDECESSOR_PACKAGED_TEMPLATE_SHA256: Final = (
    "2a6f45a790e554e3680e23c4d35abf4d8a2a99611a20e301c66d2a61a284b9db"
)
PREDECESSOR_PACKAGED_TEMPLATE_KEY: Final = (
    f"phase7/sam/templates/{PREDECESSOR_PACKAGED_TEMPLATE_SHA256}.yaml"
)
PREDECESSOR_PACKAGED_TEMPLATE_OBJECT_VERSION: Final = "fvTXvRtq9r.JtdyorhIzV.PZGLei9w4D"

_RECOVERY_LOGICAL_ID: Final = "PublicationRecoveryFunction"
_RETENTION_LOGICAL_ID: Final = "PublicationRetentionFunction"
_RECOVERY_FUNCTION_NAME: Final = "mr-lister-phase7-dev-publication-recovery"
_RETENTION_FUNCTION_NAME: Final = "mr-lister-phase7-dev-publication-retention"
_FUNCTION_NAMES: Final = (
    "mr-lister-phase7-dev-publication-query",
    "mr-lister-phase7-dev-publication-request",
    "mr-lister-phase7-dev-publication-dispatcher",
    "mr-lister-phase7-dev-publication-worker",
    _RECOVERY_FUNCTION_NAME,
    _RETENTION_FUNCTION_NAME,
)
_OPERATIONS_PARAMETER_NAMES: Final = frozenset(
    {
        "ApplicationReleaseFingerprint",
        "OperationsCodeS3Bucket",
        "OperationsCodeS3ObjectVersion",
        "OperationsReleaseFingerprint",
    }
)
_OPERATIONS_OUTPUT_NAMES: Final = frozenset(
    {"OperationsReleaseFingerprint", "OperationsRuntimeReadiness"}
)
_MAPPING_LOGICAL_IDS: Final = frozenset(
    {
        "PublicationDispatcherStreamMapping",
        "PublicationRecoveryFunctionRecoveryQueue",
        "PublicationRetentionStreamMapping",
    }
)
_RULE_LOGICAL_IDS: Final = frozenset(
    {
        "PublicationDueWorkSweepRule",
        "PublicationRecoverySweepRule",
        "PublicationWorkflowFailureRule",
    }
)
_PERMISSION_LOGICAL_IDS: Final = frozenset(
    {"PublicationDueWorkSweepPermission", "PublicationRecoverySweepPermission"}
)
_FORBIDDEN_TEMPLATE_TEXT: Final = (
    "FunctionUrlConfig",
    "GENERAL_AVAILABILITY_ENABLED",
    "MR_LISTER_ETSY_API_KEY",
    "MR_LISTER_ETSY_API_SECRET",
    "MR_LISTER_ETSY_TOKEN",
    "MR_LISTER_PRINTIFY_API_KEY",
    "MR_LISTER_PRINTIFY_SECRET_ARN",
    "secretsmanager:",
)
_FINGERPRINT = re.compile(r"^[a-f0-9]{64}$")
_BUCKET = re.compile(r"^[a-z0-9](?:[a-z0-9.-]{1,61}[a-z0-9])$")
_VERSION = re.compile(r"^(?!null$)[A-Za-z0-9._+=/-]{1,1024}$")
_GENERIC_ERROR: Final = "Phase 7.15C operations deployment evidence is invalid"


class Phase715cOperationsDeploymentError(RuntimeError):
    """Value-free refusal for incomplete, unsafe, or drifting deployment evidence."""


def verify_processed_template_delta(
    predecessor_observation: Mapping[str, object],
    target_observation: Mapping[str, object],
) -> str:
    """Require the processed update to change only the two operations functions and metadata."""

    try:
        predecessor = _processed_template(predecessor_observation)
        target = _processed_template(target_observation)
        _verify_predecessor_processed_template(predecessor)
        _verify_expected_resource_types(target)
        _verify_processed_safety(target, operations_active=True)

        if set(target) != set(predecessor):
            raise ValueError
        for key in set(predecessor) - {"Parameters", "Outputs", "Resources"}:
            if target[key] != predecessor[key]:
                raise ValueError

        source_target = cast(Mapping[str, object], json.loads(render_operations_update_template()))
        predecessor_parameters = _mapping(predecessor.get("Parameters"))
        target_parameters = _mapping(target.get("Parameters"))
        source_parameters = _mapping(source_target.get("Parameters"))
        if (
            set(target_parameters) - set(predecessor_parameters) != _OPERATIONS_PARAMETER_NAMES
            or {name: target_parameters[name] for name in predecessor_parameters}
            != predecessor_parameters
            or {name: target_parameters[name] for name in _OPERATIONS_PARAMETER_NAMES}
            != {name: source_parameters[name] for name in _OPERATIONS_PARAMETER_NAMES}
        ):
            raise ValueError

        predecessor_outputs = _mapping(predecessor.get("Outputs"))
        target_outputs = _mapping(target.get("Outputs"))
        source_outputs = _mapping(source_target.get("Outputs"))
        if (
            set(target_outputs) - set(predecessor_outputs) != _OPERATIONS_OUTPUT_NAMES
            or {name: target_outputs[name] for name in predecessor_outputs} != predecessor_outputs
            or {name: target_outputs[name] for name in _OPERATIONS_OUTPUT_NAMES}
            != {name: source_outputs[name] for name in _OPERATIONS_OUTPUT_NAMES}
        ):
            raise ValueError

        predecessor_resources = _mapping(predecessor.get("Resources"))
        target_resources = _mapping(target.get("Resources"))
        changed = {
            name
            for name in target_resources
            if target_resources[name] != predecessor_resources.get(name)
        }
        if changed != {_RECOVERY_LOGICAL_ID, _RETENTION_LOGICAL_ID}:
            raise ValueError
        _verify_function_delta(
            _mapping(predecessor_resources[_RECOVERY_LOGICAL_ID]),
            _mapping(target_resources[_RECOVERY_LOGICAL_ID]),
            handler=RECOVERY_HANDLER,
        )
        _verify_function_delta(
            _mapping(predecessor_resources[_RETENTION_LOGICAL_ID]),
            _mapping(target_resources[_RETENTION_LOGICAL_ID]),
            handler=RETENTION_HANDLER,
        )
        return _fingerprint(target)
    except Phase715cOperationsDeploymentError:
        raise
    except Exception:
        raise Phase715cOperationsDeploymentError(_GENERIC_ERROR) from None


def verify_change_set_observation(
    observation: Mapping[str, object],
    descriptor: Mapping[str, object],
    *,
    bucket: str,
    object_version: str,
    change_set_name: str,
) -> None:
    """Require one available UPDATE change set containing exactly two in-place modifications."""

    try:
        _verify_artifact_identity(descriptor, bucket=bucket, object_version=object_version)
        if (
            not isinstance(change_set_name, str)
            or re.fullmatch(r"[A-Za-z][-A-Za-z0-9]{0,127}", change_set_name) is None
        ):
            raise ValueError
        parameters = _key_value_records(
            observation.get("Parameters"), "ParameterKey", "ParameterValue"
        )
        expected_operations_parameters = {
            "ApplicationReleaseFingerprint": descriptor["application_release_fingerprint"],
            "OperationsCodeS3Bucket": bucket,
            "OperationsCodeS3ObjectVersion": object_version,
            "OperationsReleaseFingerprint": descriptor["release_fingerprint"],
        }
        if any(
            parameters.get(name) != value for name, value in expected_operations_parameters.items()
        ):
            raise ValueError
        changes = observation.get("Changes")
        if not isinstance(changes, list) or len(changes) != 2:
            raise ValueError
        found: set[str] = set()
        for change in changes:
            if not isinstance(change, Mapping) or change.get("Type") != "Resource":
                raise ValueError
            resource = _mapping(change.get("ResourceChange"))
            logical_id = resource.get("LogicalResourceId")
            physical_id = resource.get("PhysicalResourceId")
            details = resource.get("Details")
            expected_physical = {
                _RECOVERY_LOGICAL_ID: _RECOVERY_FUNCTION_NAME,
                _RETENTION_LOGICAL_ID: _RETENTION_FUNCTION_NAME,
            }.get(cast(str, logical_id))
            if (
                not isinstance(logical_id, str)
                or logical_id in found
                or expected_physical is None
                or physical_id != expected_physical
                or resource.get("Action") != "Modify"
                or resource.get("ResourceType") != "AWS::Lambda::Function"
                or resource.get("Replacement") not in {False, "False"}
                or resource.get("Scope") != ["Properties"]
                or not isinstance(details, list)
                or not details
            ):
                raise ValueError
            for detail in details:
                target = _mapping(_mapping(detail).get("Target"))
                if (
                    target.get("Attribute") != "Properties"
                    or target.get("Name")
                    not in {"Code", "Environment", "Handler", "ReservedConcurrentExecutions"}
                    or target.get("RequiresRecreation") not in {"Never", "Conditionally"}
                ):
                    raise ValueError
            found.add(logical_id)
        stack_id = observation.get("StackId")
        change_set_id = observation.get("ChangeSetId")
        if (
            found != {_RECOVERY_LOGICAL_ID, _RETENTION_LOGICAL_ID}
            or observation.get("StackName") != STACK_NAME
            or observation.get("ChangeSetName") != change_set_name
            or observation.get("ChangeSetType") != "UPDATE"
            or observation.get("Status") != "CREATE_COMPLETE"
            or observation.get("ExecutionStatus") != "AVAILABLE"
            or observation.get("IncludeNestedStacks") not in {None, False}
            or not isinstance(stack_id, str)
            or not stack_id.startswith(
                f"arn:aws:cloudformation:{REGION}:{ACCOUNT_ID}:stack/{STACK_NAME}/"
            )
            or not isinstance(change_set_id, str)
            or f"changeSet/{change_set_name}/" not in change_set_id
        ):
            raise ValueError
    except Exception:
        raise Phase715cOperationsDeploymentError(_GENERIC_ERROR) from None


def verify_s3_head_observation(
    descriptor: Mapping[str, object],
    observation: Mapping[str, object],
    *,
    archive_path: Path,
    bucket: str,
    object_version: str,
) -> None:
    """Bind one immutable S3 version/checksum readback to the sealed local archive."""

    try:
        _verify_artifact_identity(descriptor, bucket=bucket, object_version=object_version)
        archive = _mapping(descriptor.get("archive"))
        binding = _mapping(descriptor.get("s3_binding"))
        raw = _exact_file(archive_path)
        checksum = base64.b64encode(sha256(raw).digest()).decode("ascii")
        expected_metadata = {
            cast(str, binding["archive_sha256_metadata_key"]): archive["sha256"],
            cast(str, binding["release_fingerprint_metadata_key"]): descriptor[
                "release_fingerprint"
            ],
        }
        if (
            observation.get("VersionId") != object_version
            or observation.get("ContentLength") != len(raw)
            or observation.get("ChecksumSHA256") != checksum
            or observation.get("ContentType") != "application/zip"
            or observation.get("ServerSideEncryption") != binding["server_side_encryption"]
            or observation.get("Metadata") != expected_metadata
            or observation.get("DeleteMarker") not in {None, False}
        ):
            raise ValueError
    except Exception:
        raise Phase715cOperationsDeploymentError(_GENERIC_ERROR) from None


def verify_stack_transition(
    predecessor_observation: Mapping[str, object],
    target_observation: Mapping[str, object],
    descriptor: Mapping[str, object],
    *,
    bucket: str,
    object_version: str,
) -> Mapping[str, str]:
    """Require the same stack identity with only the four operations parameters/two outputs."""

    try:
        _verify_artifact_identity(descriptor, bucket=bucket, object_version=object_version)
        predecessor = _one_stack(predecessor_observation)
        target = _one_stack(target_observation)
        if (
            predecessor.get("StackName") != STACK_NAME
            or target.get("StackName") != STACK_NAME
            or predecessor.get("StackId") != target.get("StackId")
            or predecessor.get("StackStatus") not in {"CREATE_COMPLETE", "UPDATE_COMPLETE"}
            or target.get("StackStatus") != "UPDATE_COMPLETE"
        ):
            raise ValueError
        predecessor_parameters = _key_value_records(
            predecessor.get("Parameters"), "ParameterKey", "ParameterValue"
        )
        target_parameters = _key_value_records(
            target.get("Parameters"), "ParameterKey", "ParameterValue"
        )
        _verify_predecessor_parameters(predecessor_parameters)
        expected_parameters = {
            **predecessor_parameters,
            "ApplicationReleaseFingerprint": cast(
                str, descriptor["application_release_fingerprint"]
            ),
            "OperationsCodeS3Bucket": bucket,
            "OperationsCodeS3ObjectVersion": object_version,
            "OperationsReleaseFingerprint": cast(str, descriptor["release_fingerprint"]),
        }
        if target_parameters != expected_parameters:
            raise ValueError

        predecessor_outputs = _key_value_records(
            predecessor.get("Outputs"), "OutputKey", "OutputValue"
        )
        target_outputs = _key_value_records(target.get("Outputs"), "OutputKey", "OutputValue")
        _verify_closed_outputs(predecessor_outputs)
        if target_outputs != {
            **predecessor_outputs,
            "OperationsReleaseFingerprint": descriptor["release_fingerprint"],
            "OperationsRuntimeReadiness": "PROVIDER_FREE_OPERATIONS_DIRECT_INVOKE_ONLY",
        }:
            raise ValueError
        for field in ("EnableTerminationProtection", "NotificationARNs", "RoleARN", "Tags"):
            if predecessor.get(field) != target.get(field):
                raise ValueError
        return target_parameters
    except Exception:
        raise Phase715cOperationsDeploymentError(_GENERIC_ERROR) from None


def verify_phase6_application_release_observation(
    descriptor: Mapping[str, object],
    observation: Mapping[str, object],
) -> str:
    """Bind the operations runtime to the exact deployed Phase 6 release parameter."""

    try:
        application_release = descriptor.get("application_release_fingerprint")
        stack = _one_stack(observation)
        parameters = _key_value_records(
            stack.get("Parameters"),
            "ParameterKey",
            "ParameterValue",
        )
        stack_id = stack.get("StackId")
        if (
            not isinstance(application_release, str)
            or _FINGERPRINT.fullmatch(application_release) is None
            or application_release == "0" * 64
            or stack.get("StackName") != PHASE6_STACK_NAME
            or stack.get("StackStatus") not in {"CREATE_COMPLETE", "UPDATE_COMPLETE"}
            or not isinstance(stack_id, str)
            or not stack_id.startswith(
                f"arn:aws:cloudformation:{REGION}:{ACCOUNT_ID}:stack/{PHASE6_STACK_NAME}/"
            )
            or parameters.get("EnvironmentName") != ENVIRONMENT_NAME
            or parameters.get("ReleaseFingerprint") != application_release
        ):
            raise ValueError
        return _fingerprint(
            {
                "application_release_fingerprint": application_release,
                "environment_name": ENVIRONMENT_NAME,
                "stack_id": stack_id,
                "stack_name": PHASE6_STACK_NAME,
                "stack_status": stack["StackStatus"],
            }
        )
    except Exception:
        raise Phase715cOperationsDeploymentError(_GENERIC_ERROR) from None


def verify_operations_lambda_readback(
    descriptor: Mapping[str, object],
    configurations: Mapping[str, object],
    concurrencies: Mapping[str, object],
    *,
    stack_parameters: Mapping[str, str],
) -> str:
    """Verify the two deployed functions use only the sealed operations archive and environment."""

    try:
        if set(configurations) != {_RECOVERY_LOGICAL_ID, _RETENTION_LOGICAL_ID} or set(
            concurrencies
        ) != {_RECOVERY_LOGICAL_ID, _RETENTION_LOGICAL_ID}:
            raise ValueError
        expected_environment = _resolved_environment(
            stack_parameters,
            application_release=cast(str, descriptor["application_release_fingerprint"]),
            operations_release=cast(str, descriptor["release_fingerprint"]),
            operations=True,
        )
        archive = _mapping(descriptor.get("archive"))
        normalized = {
            _RECOVERY_LOGICAL_ID: _verify_lambda_configuration(
                _mapping(configurations[_RECOVERY_LOGICAL_ID]),
                _mapping(concurrencies[_RECOVERY_LOGICAL_ID]),
                function_name=_RECOVERY_FUNCTION_NAME,
                handler=RECOVERY_HANDLER,
                role_name="mr-lister-phase7-dev-publication-recovery-role",
                environment=expected_environment,
                code_sha256=cast(str, archive["sha256"]),
                code_size=cast(int, archive["size_bytes"]),
                concurrency=1,
            ),
            _RETENTION_LOGICAL_ID: _verify_lambda_configuration(
                _mapping(configurations[_RETENTION_LOGICAL_ID]),
                _mapping(concurrencies[_RETENTION_LOGICAL_ID]),
                function_name=_RETENTION_FUNCTION_NAME,
                handler=RETENTION_HANDLER,
                role_name="mr-lister-phase7-dev-publication-retention-role",
                environment=expected_environment,
                code_sha256=cast(str, archive["sha256"]),
                code_size=cast(int, archive["size_bytes"]),
                concurrency=1,
            ),
        }
        return _fingerprint(normalized)
    except Exception:
        raise Phase715cOperationsDeploymentError(_GENERIC_ERROR) from None


def verify_safety_readback(
    observation: Mapping[str, object],
    *,
    stack_parameters: Mapping[str, str],
) -> str:
    """Verify the sanitized trigger, target, route, and provider stop-line readback."""

    try:
        stream_arn = stack_parameters.get("StateTableStreamArn")
        if not isinstance(stream_arn, str):
            raise ValueError
        lambda_prefix = f"arn:aws:lambda:{REGION}:{ACCOUNT_ID}:function:"
        sqs_prefix = f"arn:aws:sqs:{REGION}:{ACCOUNT_ID}:"
        expected = {
            "event_source_mappings": [
                {
                    "batch_size": 25,
                    "enabled": False,
                    "event_source_arn": stream_arn,
                    "function_name": "mr-lister-phase7-dev-publication-dispatcher",
                    "logical_id": "PublicationDispatcherStreamMapping",
                    "state": "Disabled",
                },
                {
                    "batch_size": 1,
                    "enabled": False,
                    "event_source_arn": (f"{sqs_prefix}mr-lister-phase7-dev-publication-recovery"),
                    "function_name": _RECOVERY_FUNCTION_NAME,
                    "logical_id": "PublicationRecoveryFunctionRecoveryQueue",
                    "state": "Disabled",
                },
                {
                    "batch_size": 1,
                    "enabled": False,
                    "event_source_arn": stream_arn,
                    "function_name": _RETENTION_FUNCTION_NAME,
                    "logical_id": "PublicationRetentionStreamMapping",
                    "state": "Disabled",
                },
            ],
            "eventbridge_rules": [
                {
                    "logical_id": "PublicationDueWorkSweepRule",
                    "name": "mr-lister-phase7-dev-publication-due-sweep",
                    "state": "DISABLED",
                    "target_arn": f"{lambda_prefix}mr-lister-phase7-dev-publication-dispatcher",
                    "target_id": "PublicationDispatcher",
                },
                {
                    "logical_id": "PublicationRecoverySweepRule",
                    "name": "mr-lister-phase7-dev-publication-recovery-sweep",
                    "state": "DISABLED",
                    "target_arn": f"{lambda_prefix}{_RECOVERY_FUNCTION_NAME}",
                    "target_id": "PublicationRecovery",
                },
                {
                    "logical_id": "PublicationWorkflowFailureRule",
                    "name": "mr-lister-phase7-dev-publication-workflow-failure",
                    "state": "DISABLED",
                    "target_arn": f"{sqs_prefix}mr-lister-phase7-dev-publication-recovery",
                    "target_id": "PublicationRecoveryQueue",
                },
            ],
            "function_urls_absent": list(_FUNCTION_NAMES),
            "provider_credential_environment_name_count": 0,
            "provider_mutation_enabled": False,
            "registered_route_count": 0,
            "seller_publication_enabled": False,
        }
        if observation != expected:
            raise ValueError
        return _fingerprint(expected)
    except Exception:
        raise Phase715cOperationsDeploymentError(_GENERIC_ERROR) from None


def verify_predecessor_rollback_readback(
    predecessor_processed_observation: Mapping[str, object],
    rollback_processed_observation: Mapping[str, object],
    predecessor_stack_observation: Mapping[str, object],
    rollback_stack_observation: Mapping[str, object],
    predecessor_configurations: Mapping[str, object],
    rollback_configurations: Mapping[str, object],
    predecessor_concurrencies: Mapping[str, object],
    rollback_concurrencies: Mapping[str, object],
    safety_observation: Mapping[str, object],
) -> Mapping[str, object]:
    """Prove rollback restored the exact captured production-disabled predecessor tuple."""

    try:
        predecessor_template = _processed_template(predecessor_processed_observation)
        rollback_template = _processed_template(rollback_processed_observation)
        _verify_predecessor_processed_template(predecessor_template)
        if _canonical(rollback_template) != _canonical(predecessor_template):
            raise ValueError

        predecessor_stack = _one_stack(predecessor_stack_observation)
        rollback_stack = _one_stack(rollback_stack_observation)
        predecessor_authority = _stack_rollback_authority(predecessor_stack)
        rollback_authority = _stack_rollback_authority(rollback_stack)
        if (
            predecessor_authority != rollback_authority
            or rollback_stack.get("StackStatus") != "UPDATE_COMPLETE"
        ):
            raise ValueError
        stack_parameters = cast(Mapping[str, str], predecessor_authority["parameters"])
        _verify_predecessor_parameters(stack_parameters)
        _verify_closed_outputs(cast(Mapping[str, str], predecessor_authority["outputs"]))

        predecessor_lambda = _verify_predecessor_lambdas(
            predecessor_configurations,
            predecessor_concurrencies,
            stack_parameters=stack_parameters,
        )
        rollback_lambda = _verify_predecessor_lambdas(
            rollback_configurations,
            rollback_concurrencies,
            stack_parameters=stack_parameters,
        )
        if predecessor_lambda != rollback_lambda:
            raise ValueError
        safety_fingerprint = verify_safety_readback(
            safety_observation,
            stack_parameters=stack_parameters,
        )
        tuple_document: dict[str, object] = {
            "format": "mr-lister-phase7.15c-operations-rollback-readback-v1",
            "predecessor": {
                "archive_sha256": PREDECESSOR_ARCHIVE_SHA256,
                "archive_size_bytes": PREDECESSOR_ARCHIVE_SIZE_BYTES,
                "candidate_code_s3_key": PREDECESSOR_CODE_S3_KEY,
                "candidate_code_s3_object_version": PREDECESSOR_CODE_S3_OBJECT_VERSION,
                "packaged_template_s3_key": PREDECESSOR_PACKAGED_TEMPLATE_KEY,
                "packaged_template_s3_object_version": (
                    PREDECESSOR_PACKAGED_TEMPLATE_OBJECT_VERSION
                ),
                "packaged_template_sha256": PREDECESSOR_PACKAGED_TEMPLATE_SHA256,
                "processed_template_sha256": _fingerprint(predecessor_template),
                "release_fingerprint": PREDECESSOR_RELEASE_FINGERPRINT,
                "source_template_sha256": BASE_TEMPLATE_SHA256,
                "stack_authority_sha256": _fingerprint(predecessor_authority),
                "two_function_configuration_sha256": predecessor_lambda,
            },
            "readback": {
                "processed_template_sha256": _fingerprint(rollback_template),
                "safety_sha256": safety_fingerprint,
                "stack_authority_sha256": _fingerprint(rollback_authority),
                "two_function_configuration_sha256": rollback_lambda,
            },
            "result": "passed",
        }
        tuple_document["evidence_sha256"] = _fingerprint(tuple_document)
        return tuple_document
    except Phase715cOperationsDeploymentError:
        raise
    except Exception:
        raise Phase715cOperationsDeploymentError(_GENERIC_ERROR) from None


def verify_operations_deployment_evidence(
    *,
    deployment_root: Path,
    archive_path: Path,
    descriptor_path: Path,
    update_template_path: Path,
    predecessor_processed_observation: Mapping[str, object],
    target_processed_observation: Mapping[str, object],
    change_set_observation: Mapping[str, object],
    head_object_observation: Mapping[str, object],
    predecessor_stack_observation: Mapping[str, object],
    target_stack_observation: Mapping[str, object],
    phase6_stack_observation: Mapping[str, object],
    configurations: Mapping[str, object],
    concurrencies: Mapping[str, object],
    safety_observation: Mapping[str, object],
    bucket: str,
    object_version: str,
    change_set_name: str,
) -> Mapping[str, object]:
    """Verify all local artifact and deployed provider-free update evidence as one proof."""

    try:
        descriptor = verify_operations_deployment_artifact(
            deployment_root,
            archive_path=archive_path,
            descriptor_path=descriptor_path,
        )
        source_template_fingerprint = verify_operations_update_template(update_template_path)
        processed_template_fingerprint = verify_processed_template_delta(
            predecessor_processed_observation,
            target_processed_observation,
        )
        verify_change_set_observation(
            change_set_observation,
            descriptor,
            bucket=bucket,
            object_version=object_version,
            change_set_name=change_set_name,
        )
        verify_s3_head_observation(
            descriptor,
            head_object_observation,
            archive_path=archive_path,
            bucket=bucket,
            object_version=object_version,
        )
        phase6_binding_fingerprint = verify_phase6_application_release_observation(
            descriptor,
            phase6_stack_observation,
        )
        stack_parameters = verify_stack_transition(
            predecessor_stack_observation,
            target_stack_observation,
            descriptor,
            bucket=bucket,
            object_version=object_version,
        )
        lambda_fingerprint = verify_operations_lambda_readback(
            descriptor,
            configurations,
            concurrencies,
            stack_parameters=stack_parameters,
        )
        safety_fingerprint = verify_safety_readback(
            safety_observation,
            stack_parameters=stack_parameters,
        )
        result: dict[str, object] = {
            "format": "mr-lister-phase7.15c-operations-deployment-readback-v1",
            "lambda_readback_count": 2,
            "lambda_readback_sha256": lambda_fingerprint,
            "operations_release_fingerprint": descriptor["release_fingerprint"],
            "phase6_application_binding_sha256": phase6_binding_fingerprint,
            "processed_template_sha256": processed_template_fingerprint,
            "result": "passed",
            "safety_readback_count": 12,
            "safety_readback_sha256": safety_fingerprint,
            "source_template_sha256": source_template_fingerprint,
        }
        result["evidence_sha256"] = _fingerprint(result)
        return result
    except Phase715cOperationsDeploymentError:
        raise
    except Exception:
        raise Phase715cOperationsDeploymentError(_GENERIC_ERROR) from None


def _verify_predecessor_processed_template(template: Mapping[str, object]) -> None:
    _verify_expected_resource_types(template)
    _verify_processed_safety(template, operations_active=False)
    base = _mapping(json.loads(_exact_file(BASE_TEMPLATE)))
    if sha256(_exact_file(BASE_TEMPLATE)).hexdigest() != BASE_TEMPLATE_SHA256:
        raise ValueError
    if template.get("Description") != base.get("Description"):
        raise ValueError
    if _mapping(template.get("Parameters")) != _mapping(base.get("Parameters")):
        raise ValueError
    if _mapping(template.get("Outputs")) != _mapping(base.get("Outputs")):
        raise ValueError
    resources = _mapping(template.get("Resources"))
    for logical_id, handler in (
        (
            _RECOVERY_LOGICAL_ID,
            "mr_lister.cloud.phase7_production_entrypoints.publication_recovery_handler",
        ),
        (
            _RETENTION_LOGICAL_ID,
            "mr_lister.cloud.phase7_production_entrypoints.publication_retention_handler",
        ),
    ):
        properties = _mapping(_mapping(resources[logical_id]).get("Properties"))
        variables = _mapping(_mapping(properties.get("Environment")).get("Variables"))
        if (
            properties.get("Code")
            != {
                "S3Bucket": {"Ref": "CandidateCodeS3Bucket"},
                "S3Key": {
                    "Fn::Sub": (
                        "phase7/candidates/${CandidateReleaseFingerprint}/production-disabled.zip"
                    )
                },
                "S3ObjectVersion": {"Ref": "CandidateCodeS3ObjectVersion"},
            }
            or properties.get("Handler") != handler
            or properties.get("ReservedConcurrentExecutions") != 0
            or variables.get("MR_LISTER_RELEASE_FINGERPRINT")
            != {"Ref": "CandidateReleaseFingerprint"}
            or any(name in variables for name in _operations_environment_names())
        ):
            raise ValueError


def _verify_function_delta(
    predecessor: Mapping[str, object],
    target: Mapping[str, object],
    *,
    handler: str,
) -> None:
    expected = deepcopy(cast(dict[str, Any], predecessor))
    properties = cast(dict[str, Any], _mapping(expected.get("Properties")))
    properties["Code"] = {
        "S3Bucket": {"Ref": "OperationsCodeS3Bucket"},
        "S3Key": {"Fn::Sub": OPERATIONS_ARCHIVE_KEY},
        "S3ObjectVersion": {"Ref": "OperationsCodeS3ObjectVersion"},
    }
    properties["Handler"] = handler
    properties["ReservedConcurrentExecutions"] = 1
    environment = cast(dict[str, Any], _mapping(properties.get("Environment")))
    variables = cast(dict[str, Any], _mapping(environment.get("Variables")))
    variables.update(
        {
            "MR_LISTER_PHASE715C_OPERATIONS_RELEASE_FINGERPRINT": {
                "Ref": "OperationsReleaseFingerprint"
            },
            "MR_LISTER_RELEASE_FINGERPRINT": {"Ref": "ApplicationReleaseFingerprint"},
            "MR_LISTER_PHASE715C_OPERATIONS_MODE": "PROVIDER_FREE_OPERATIONS",
            "MR_LISTER_PUBLICATION_WORKFLOW_ARN": {"Ref": "PublicationWorkflowStateMachine"},
            "MR_LISTER_PHASE7_DISPATCHER_ENABLED": "false",
            "MR_LISTER_PHASE7_WORKER_ENABLED": "false",
        }
    )
    if target != expected:
        raise ValueError


def _verify_expected_resource_types(template: Mapping[str, object]) -> None:
    if "Transform" in template or "Globals" in template:
        raise ValueError
    resources = _mapping(template.get("Resources"))
    expected = _expected_processed_resource_types()
    observed = {name: _mapping(resource).get("Type") for name, resource in resources.items()}
    if len(resources) != 49 or observed != expected:
        raise ValueError


def _expected_processed_resource_types() -> dict[str, str]:
    base = _mapping(json.loads(_exact_file(BASE_TEMPLATE)))
    resources = _mapping(base.get("Resources"))
    conversions = {
        "AWS::Serverless::Function": "AWS::Lambda::Function",
        "AWS::Serverless::StateMachine": "AWS::StepFunctions::StateMachine",
    }
    expected = {
        name: conversions.get(
            cast(str, _mapping(resource).get("Type")), cast(str, _mapping(resource).get("Type"))
        )
        for name, resource in resources.items()
    }
    expected["PublicationRecoveryFunctionRecoveryQueue"] = "AWS::Lambda::EventSourceMapping"
    return expected


def _verify_processed_safety(
    template: Mapping[str, object],
    *,
    operations_active: bool,
) -> None:
    resources = _mapping(template.get("Resources"))
    mappings = {
        name
        for name, resource in resources.items()
        if _mapping(resource).get("Type") == "AWS::Lambda::EventSourceMapping"
    }
    rules = {
        name
        for name, resource in resources.items()
        if _mapping(resource).get("Type") == "AWS::Events::Rule"
    }
    permissions = {
        name
        for name, resource in resources.items()
        if _mapping(resource).get("Type") == "AWS::Lambda::Permission"
    }
    if (
        mappings != _MAPPING_LOGICAL_IDS
        or rules != _RULE_LOGICAL_IDS
        or permissions != _PERMISSION_LOGICAL_IDS
    ):
        raise ValueError
    if any(
        _mapping(_mapping(resources[name]).get("Properties")).get("Enabled") is not False
        for name in mappings
    ):
        raise ValueError
    if any(
        _mapping(_mapping(resources[name]).get("Properties")).get("State") != "DISABLED"
        for name in rules
    ):
        raise ValueError
    disallowed_types = {
        "AWS::ApiGateway::RestApi",
        "AWS::ApiGatewayV2::Api",
        "AWS::Lambda::Url",
    }
    if any(_mapping(resource).get("Type") in disallowed_types for resource in resources.values()):
        raise ValueError
    function_concurrency = {
        "PublicationQueryFunction": 0,
        "PublicationRequestFunction": 0,
        "PublicationDispatcherFunction": 0,
        "PublicationWorkerFunction": 0,
        _RECOVERY_LOGICAL_ID: 1 if operations_active else 0,
        _RETENTION_LOGICAL_ID: 1 if operations_active else 0,
    }
    for name, concurrency in function_concurrency.items():
        properties = _mapping(_mapping(resources[name]).get("Properties"))
        variables = _mapping(_mapping(properties.get("Environment")).get("Variables"))
        if (
            properties.get("ReservedConcurrentExecutions") != concurrency
            or variables.get("MR_LISTER_PHASE7_QUERY_ENABLED") != "false"
            or variables.get("MR_LISTER_PHASE7_REQUEST_ENABLED") != "false"
            or variables.get("MR_LISTER_PHASE7_PUBLICATION_ENABLED") != "false"
        ):
            raise ValueError
    outputs = _mapping(template.get("Outputs"))
    if (
        _mapping(outputs.get("SellerPublicationEnabled")).get("Value") != "false"
        or _mapping(outputs.get("ProviderMutationEnabled")).get("Value") != "false"
        or _mapping(outputs.get("PublicationQueryRegistered")).get("Value") != "false"
        or _mapping(outputs.get("PublicationRequestRegistered")).get("Value") != "false"
        or _mapping(outputs.get("PublicationWorkerTriggered")).get("Value") != "false"
    ):
        raise ValueError
    serialized = _canonical(template).decode("utf-8")
    if any(forbidden in serialized for forbidden in _FORBIDDEN_TEMPLATE_TEXT):
        raise ValueError


def _verify_predecessor_parameters(parameters: Mapping[str, str]) -> None:
    if (
        set(parameters)
        != {
            "ActivationMode",
            "CandidateCodeS3Bucket",
            "CandidateCodeS3ObjectVersion",
            "CandidateReleaseFingerprint",
            "EnvironmentName",
            "SellerUserPoolClientId",
            "SellerUserPoolId",
            "StateTableArn",
            "StateTableStreamArn",
        }
        or parameters.get("ActivationMode") != "PRODUCTION_DISABLED"
        or parameters.get("EnvironmentName") != ENVIRONMENT_NAME
        or parameters.get("CandidateReleaseFingerprint") != PREDECESSOR_RELEASE_FINGERPRINT
        or parameters.get("CandidateCodeS3ObjectVersion") != PREDECESSOR_CODE_S3_OBJECT_VERSION
        or parameters.get("StateTableArn")
        != f"arn:aws:dynamodb:{REGION}:{ACCOUNT_ID}:table/{STATE_TABLE_NAME}"
        or not cast(str, parameters.get("StateTableStreamArn", "")).startswith(
            f"arn:aws:dynamodb:{REGION}:{ACCOUNT_ID}:table/{STATE_TABLE_NAME}/stream/"
        )
    ):
        raise ValueError


def _verify_closed_outputs(outputs: Mapping[str, str]) -> None:
    if outputs != {
        "DeploymentReadiness": "PRODUCTION_DISABLED",
        "ProviderMutationEnabled": "false",
        "PublicationQueryRegistered": "false",
        "PublicationRequestRegistered": "false",
        "PublicationWorkerTriggered": "false",
        "ResourceInstantiationPossible": "true",
        "SellerPublicationEnabled": "false",
    }:
        raise ValueError


def _resolved_environment(
    stack_parameters: Mapping[str, str],
    *,
    application_release: str,
    operations_release: str | None,
    operations: bool,
) -> dict[str, str]:
    _verify_predecessor_parameters(
        {
            name: value
            for name, value in stack_parameters.items()
            if name not in _OPERATIONS_PARAMETER_NAMES
        }
    )
    user_pool = stack_parameters["SellerUserPoolId"]
    result = {
        "MR_LISTER_COGNITO_CLIENT_ID": stack_parameters["SellerUserPoolClientId"],
        "MR_LISTER_COGNITO_GROUP": "seller",
        "MR_LISTER_COGNITO_ISSUER": (f"https://cognito-idp.{REGION}.amazonaws.com/{user_pool}"),
        "MR_LISTER_COGNITO_SCOPE": "mr-lister-api/seller",
        "MR_LISTER_PHASE7_ACTIVATION_MODE": "SOURCE_ONLY_DISABLED",
        "MR_LISTER_PHASE7_CONTRACT_FINGERPRINT": (
            "548b710230618e73c20a509f2121799c415b50070e1e2ae7e1b82fe3c37e2981"
        ),
        "MR_LISTER_PHASE7_CONTRACT_VERSION": "7.0.1",
        "MR_LISTER_PHASE7_PRODUCTION_CANDIDATE_ENABLED": "false",
        "MR_LISTER_PHASE7_PRODUCTION_RELEASE_FINGERPRINT": PREDECESSOR_RELEASE_FINGERPRINT,
        "MR_LISTER_PHASE7_PUBLICATION_ENABLED": "false",
        "MR_LISTER_PHASE7_QUERY_ENABLED": "false",
        "MR_LISTER_PHASE7_REQUEST_ENABLED": "false",
        "MR_LISTER_PHASE7_SCAFFOLD_ONLY": "true",
        "MR_LISTER_PRODUCT_PROFILE_FINGERPRINT": (
            "5de1257141cfdacb1731f68bb9113712957483b33d5b0f7115afdba86eb7476c"
        ),
        "MR_LISTER_PRODUCT_PROFILE_ID": "gildan_64000_swiftpod",
        "MR_LISTER_PRODUCT_PROFILE_PATH": (
            "/var/task/config/product_profiles/gildan_64000_swiftpod.json"
        ),
        "MR_LISTER_PRODUCT_PROFILE_VERSION": "2",
        "MR_LISTER_RELEASE_FINGERPRINT": application_release,
        "MR_LISTER_STATE_TABLE": STATE_TABLE_NAME,
    }
    if operations:
        if operations_release is None:
            raise ValueError
        result.update(
            {
                "MR_LISTER_PHASE715C_OPERATIONS_MODE": "PROVIDER_FREE_OPERATIONS",
                "MR_LISTER_PHASE715C_OPERATIONS_RELEASE_FINGERPRINT": operations_release,
                "MR_LISTER_PHASE7_DISPATCHER_ENABLED": "false",
                "MR_LISTER_PHASE7_WORKER_ENABLED": "false",
                "MR_LISTER_PUBLICATION_WORKFLOW_ARN": PUBLICATION_WORKFLOW_ARN,
            }
        )
    return result


def _verify_lambda_configuration(
    configuration: Mapping[str, object],
    concurrency_observation: Mapping[str, object],
    *,
    function_name: str,
    handler: str,
    role_name: str,
    environment: Mapping[str, str],
    code_sha256: str,
    code_size: int,
    concurrency: int,
) -> Mapping[str, object]:
    value = configuration.get("Configuration", configuration)
    config = _mapping(value)
    expected_code_sha = base64.b64encode(bytes.fromhex(code_sha256)).decode("ascii")
    variables = _mapping(_mapping(config.get("Environment")).get("Variables"))
    logging = _mapping(config.get("LoggingConfig"))
    vpc = _mapping(config.get("VpcConfig", {}))
    if (
        config.get("FunctionName") != function_name
        or config.get("FunctionArn")
        != f"arn:aws:lambda:{REGION}:{ACCOUNT_ID}:function:{function_name}"
        or config.get("Runtime") != "python3.12"
        or config.get("Role") != f"arn:aws:iam::{ACCOUNT_ID}:role/{role_name}"
        or config.get("Handler") != handler
        or config.get("CodeSize") != code_size
        or config.get("CodeSha256") != expected_code_sha
        or config.get("Timeout") != 60
        or config.get("MemorySize") != 512
        or config.get("Version") != "$LATEST"
        or config.get("State") != "Active"
        or config.get("LastUpdateStatus") != "Successful"
        or config.get("PackageType") != "Zip"
        or config.get("Architectures") != ["arm64"]
        or variables != environment
        or logging
        != {
            "ApplicationLogLevel": "ERROR",
            "LogFormat": "JSON",
            "LogGroup": f"/aws/lambda/{function_name}",
            "SystemLogLevel": "WARN",
        }
        or bool(vpc.get("VpcId"))
        or bool(vpc.get("SubnetIds"))
        or bool(vpc.get("SecurityGroupIds"))
        or config.get("Layers") not in (None, (), [])
        or config.get("FileSystemConfigs") not in (None, (), [])
        or config.get("DeadLetterConfig") not in (None, (), {})
        or concurrency_observation != {"ReservedConcurrentExecutions": concurrency}
    ):
        raise ValueError
    return {
        "architectures": ["arm64"],
        "code_sha256": code_sha256,
        "code_size": code_size,
        "concurrency": concurrency,
        "environment_sha256": _fingerprint(environment),
        "function_name": function_name,
        "handler": handler,
        "role_name": role_name,
        "runtime": "python3.12",
    }


def _verify_predecessor_lambdas(
    configurations: Mapping[str, object],
    concurrencies: Mapping[str, object],
    *,
    stack_parameters: Mapping[str, str],
) -> str:
    if set(configurations) != {_RECOVERY_LOGICAL_ID, _RETENTION_LOGICAL_ID} or set(
        concurrencies
    ) != {_RECOVERY_LOGICAL_ID, _RETENTION_LOGICAL_ID}:
        raise ValueError
    environment = _resolved_environment(
        stack_parameters,
        application_release=PREDECESSOR_RELEASE_FINGERPRINT,
        operations_release=None,
        operations=False,
    )
    normalized = {
        _RECOVERY_LOGICAL_ID: _verify_lambda_configuration(
            _mapping(configurations[_RECOVERY_LOGICAL_ID]),
            _mapping(concurrencies[_RECOVERY_LOGICAL_ID]),
            function_name=_RECOVERY_FUNCTION_NAME,
            handler="mr_lister.cloud.phase7_production_entrypoints.publication_recovery_handler",
            role_name="mr-lister-phase7-dev-publication-recovery-role",
            environment=environment,
            code_sha256=PREDECESSOR_ARCHIVE_SHA256,
            code_size=PREDECESSOR_ARCHIVE_SIZE_BYTES,
            concurrency=0,
        ),
        _RETENTION_LOGICAL_ID: _verify_lambda_configuration(
            _mapping(configurations[_RETENTION_LOGICAL_ID]),
            _mapping(concurrencies[_RETENTION_LOGICAL_ID]),
            function_name=_RETENTION_FUNCTION_NAME,
            handler="mr_lister.cloud.phase7_production_entrypoints.publication_retention_handler",
            role_name="mr-lister-phase7-dev-publication-retention-role",
            environment=environment,
            code_sha256=PREDECESSOR_ARCHIVE_SHA256,
            code_size=PREDECESSOR_ARCHIVE_SIZE_BYTES,
            concurrency=0,
        ),
    }
    return _fingerprint(normalized)


def _stack_rollback_authority(stack: Mapping[str, object]) -> Mapping[str, object]:
    if stack.get("StackName") != STACK_NAME or not isinstance(stack.get("StackId"), str):
        raise ValueError
    return {
        "enable_termination_protection": stack.get("EnableTerminationProtection"),
        "notification_arns": stack.get("NotificationARNs"),
        "outputs": _key_value_records(stack.get("Outputs"), "OutputKey", "OutputValue"),
        "parameters": _key_value_records(stack.get("Parameters"), "ParameterKey", "ParameterValue"),
        "role_arn": stack.get("RoleARN"),
        "stack_id": stack["StackId"],
        "stack_name": stack["StackName"],
        "tags": _key_value_records(stack.get("Tags", []), "Key", "Value"),
    }


def _processed_template(observation: Mapping[str, object]) -> Mapping[str, object]:
    if set(observation) - {"StagesAvailable", "TemplateBody"}:
        raise ValueError
    body = observation.get("TemplateBody")
    stages = observation.get("StagesAvailable")
    if not isinstance(body, Mapping) or (
        stages is not None and (not isinstance(stages, list) or "Processed" not in stages)
    ):
        raise ValueError
    return cast(Mapping[str, object], body)


def _one_stack(observation: Mapping[str, object]) -> Mapping[str, object]:
    stacks = observation.get("Stacks")
    if set(observation) != {"Stacks"} or not isinstance(stacks, list) or len(stacks) != 1:
        raise ValueError
    return _mapping(stacks[0])


def _verify_artifact_identity(
    descriptor: Mapping[str, object],
    *,
    bucket: str,
    object_version: str,
) -> None:
    release = descriptor.get("release_fingerprint")
    application = descriptor.get("application_release_fingerprint")
    binding = _mapping(descriptor.get("s3_binding"))
    if (
        _BUCKET.fullmatch(bucket) is None
        or _VERSION.fullmatch(object_version) is None
        or not isinstance(release, str)
        or _FINGERPRINT.fullmatch(release) is None
        or release == "0" * 64
        or not isinstance(application, str)
        or _FINGERPRINT.fullmatch(application) is None
        or application == "0" * 64
        or binding.get("key_template")
        != "phase7/operations/{release_fingerprint}/phase715c-operations.zip"
        or binding.get("bucket_parameter") != "OperationsCodeS3Bucket"
        or binding.get("object_version_parameter") != "OperationsCodeS3ObjectVersion"
        or binding.get("release_fingerprint_parameter") != "OperationsReleaseFingerprint"
        or binding.get("application_release_fingerprint_parameter")
        != "ApplicationReleaseFingerprint"
    ):
        raise ValueError


def _operations_environment_names() -> frozenset[str]:
    return frozenset(
        {
            "MR_LISTER_PHASE715C_OPERATIONS_MODE",
            "MR_LISTER_PHASE715C_OPERATIONS_RELEASE_FINGERPRINT",
            "MR_LISTER_PHASE7_DISPATCHER_ENABLED",
            "MR_LISTER_PHASE7_WORKER_ENABLED",
            "MR_LISTER_PUBLICATION_WORKFLOW_ARN",
        }
    )


def _key_value_records(value: object, key: str, field: str) -> dict[str, str]:
    if not isinstance(value, list):
        raise ValueError
    result: dict[str, str] = {}
    for raw in value:
        item = _mapping(raw)
        name = item.get(key)
        content = item.get(field)
        if not isinstance(name, str) or not isinstance(content, str) or name in result:
            raise ValueError
        result[name] = content
    return result


def _mapping(value: object) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError
    return cast(Mapping[str, Any], value)


def _exact_file(path: Path) -> bytes:
    resolved = path.resolve(strict=True)
    if (
        path.is_symlink()
        or not resolved.is_file()
        or not 1 <= resolved.stat().st_size <= 128 * 1024 * 1024
    ):
        raise ValueError
    return resolved.read_bytes()


def _canonical(value: object) -> bytes:
    return (
        json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")


def _fingerprint(value: object) -> str:
    return sha256(_canonical(value)).hexdigest()


def _read_json_mapping(path: Path) -> Mapping[str, object]:
    try:
        value = json.loads(_exact_file(path))
        return cast(Mapping[str, object], _mapping(value))
    except Exception:
        raise Phase715cOperationsDeploymentError(_GENERIC_ERROR) from None


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="mode", required=True)

    deployment = commands.add_parser(
        "deployment",
        help="verify the provider-free operations update and deployed readback",
    )
    deployment.add_argument("--deployment", type=Path, required=True)
    deployment.add_argument("--archive", type=Path, required=True)
    deployment.add_argument("--descriptor", type=Path, required=True)
    deployment.add_argument("--update-template", type=Path, required=True)
    deployment.add_argument("--predecessor-processed-json", type=Path, required=True)
    deployment.add_argument("--target-processed-json", type=Path, required=True)
    deployment.add_argument("--change-set-json", type=Path, required=True)
    deployment.add_argument("--head-object-json", type=Path, required=True)
    deployment.add_argument("--predecessor-stack-json", type=Path, required=True)
    deployment.add_argument("--target-stack-json", type=Path, required=True)
    deployment.add_argument("--phase6-stack-json", type=Path, required=True)
    deployment.add_argument("--lambda-configurations-json", type=Path, required=True)
    deployment.add_argument("--lambda-concurrencies-json", type=Path, required=True)
    deployment.add_argument("--safety-json", type=Path, required=True)
    deployment.add_argument("--bucket", required=True)
    deployment.add_argument("--object-version", required=True)
    deployment.add_argument("--change-set-name", required=True)

    rollback = commands.add_parser(
        "rollback",
        help="verify exact restoration of the immutable production-disabled predecessor",
    )
    rollback.add_argument("--predecessor-processed-json", type=Path, required=True)
    rollback.add_argument("--rollback-processed-json", type=Path, required=True)
    rollback.add_argument("--predecessor-stack-json", type=Path, required=True)
    rollback.add_argument("--rollback-stack-json", type=Path, required=True)
    rollback.add_argument("--predecessor-lambda-configurations-json", type=Path, required=True)
    rollback.add_argument("--rollback-lambda-configurations-json", type=Path, required=True)
    rollback.add_argument("--predecessor-lambda-concurrencies-json", type=Path, required=True)
    rollback.add_argument("--rollback-lambda-concurrencies-json", type=Path, required=True)
    rollback.add_argument("--safety-json", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Verify one complete capture set and emit only the sanitized proof document."""

    arguments = _parser().parse_args(argv)
    if arguments.mode == "deployment":
        result = verify_operations_deployment_evidence(
            deployment_root=arguments.deployment,
            archive_path=arguments.archive,
            descriptor_path=arguments.descriptor,
            update_template_path=arguments.update_template,
            predecessor_processed_observation=_read_json_mapping(
                arguments.predecessor_processed_json
            ),
            target_processed_observation=_read_json_mapping(arguments.target_processed_json),
            change_set_observation=_read_json_mapping(arguments.change_set_json),
            head_object_observation=_read_json_mapping(arguments.head_object_json),
            predecessor_stack_observation=_read_json_mapping(arguments.predecessor_stack_json),
            target_stack_observation=_read_json_mapping(arguments.target_stack_json),
            phase6_stack_observation=_read_json_mapping(arguments.phase6_stack_json),
            configurations=_read_json_mapping(arguments.lambda_configurations_json),
            concurrencies=_read_json_mapping(arguments.lambda_concurrencies_json),
            safety_observation=_read_json_mapping(arguments.safety_json),
            bucket=arguments.bucket,
            object_version=arguments.object_version,
            change_set_name=arguments.change_set_name,
        )
    else:
        result = verify_predecessor_rollback_readback(
            _read_json_mapping(arguments.predecessor_processed_json),
            _read_json_mapping(arguments.rollback_processed_json),
            _read_json_mapping(arguments.predecessor_stack_json),
            _read_json_mapping(arguments.rollback_stack_json),
            _read_json_mapping(arguments.predecessor_lambda_configurations_json),
            _read_json_mapping(arguments.rollback_lambda_configurations_json),
            _read_json_mapping(arguments.predecessor_lambda_concurrencies_json),
            _read_json_mapping(arguments.rollback_lambda_concurrencies_json),
            _read_json_mapping(arguments.safety_json),
        )
    print(_canonical(result).decode("utf-8"), end="")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())


__all__ = [
    "ACCOUNT_ID",
    "ENVIRONMENT_NAME",
    "PHASE6_STACK_NAME",
    "PREDECESSOR_ARCHIVE_SHA256",
    "PREDECESSOR_ARCHIVE_SIZE_BYTES",
    "PREDECESSOR_CODE_S3_KEY",
    "PREDECESSOR_CODE_S3_OBJECT_VERSION",
    "PREDECESSOR_PACKAGED_TEMPLATE_KEY",
    "PREDECESSOR_PACKAGED_TEMPLATE_OBJECT_VERSION",
    "PREDECESSOR_PACKAGED_TEMPLATE_SHA256",
    "PREDECESSOR_RELEASE_FINGERPRINT",
    "REGION",
    "STACK_NAME",
    "Phase715cOperationsDeploymentError",
    "verify_change_set_observation",
    "verify_operations_deployment_evidence",
    "verify_operations_lambda_readback",
    "verify_phase6_application_release_observation",
    "verify_predecessor_rollback_readback",
    "verify_processed_template_delta",
    "verify_s3_head_observation",
    "verify_safety_readback",
    "verify_stack_transition",
]
