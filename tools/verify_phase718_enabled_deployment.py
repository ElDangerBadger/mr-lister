"""Offline verification of Phase 7.18 change-set, enabled readback, and rollback captures.

This module performs no AWS calls.  Operators capture AWS CLI JSON separately and pass those
observations here.  The verifier binds the live stack to the exact rendered template and enabled
artifact while proving that the referenced Phase 6 stack authority did not change.
"""

from __future__ import annotations

import argparse
import base64
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Any, Final, cast

from mr_lister.release.phase718 import PHASE718_ENTRYPOINTS
from tools.build_phase718_enabled_release import verify_enabled_deployment_artifact
from tools.render_phase718_enabled_template import (
    BASE_TEMPLATE,
    CONTRACT_FINGERPRINT,
    PROFILE_FINGERPRINT,
    WORKFLOW_DEFINITION,
    render_phase718_enabled_template,
)
from tools.verify_phase715c_operations_deployment import (
    PREDECESSOR_PACKAGED_TEMPLATE_KEY,
    PREDECESSOR_PACKAGED_TEMPLATE_OBJECT_VERSION,
    PREDECESSOR_PACKAGED_TEMPLATE_SHA256,
)

ACCOUNT_ID: Final = "384627057108"
REGION: Final = "us-west-2"
STACK_NAME: Final = "mr-lister-phase7-dev"
PHASE6_STACK_NAME: Final = "mr-lister-phase6-dev"
_GENERIC_ERROR: Final = "Phase 7.18 enabled deployment observation is invalid"
_FINGERPRINT: Final = re.compile(r"^(?!0{64}$)[a-f0-9]{64}$")
_COMPONENTS: Final = ("Query", "Request", "Dispatcher", "Worker", "Recovery", "Retention")
_TIMEOUTS: Final = {
    "Query": 10,
    "Request": 15,
    "Dispatcher": 30,
    "Worker": 60,
    "Recovery": 60,
    "Retention": 60,
}
_POLICY_NAMES: Final = {
    "Query": "ReadOnlyPublicationProjection",
    "Request": "AtomicPublicationRequest",
    "Dispatcher": "DispatchExactPublicationWork",
    "Worker": "OneStepPublicationWorker",
    "Recovery": "SameExecutionPublicationRecovery",
    "Retention": "TerminalPublicationRetention",
}
_RULES: Final = (
    "PublicationDueWorkSweepRule",
    "PublicationRecoverySweepRule",
    "PublicationWorkflowFailureRule",
)
_API_RESOURCES: Final = {
    "PublicationQueryIntegration",
    "PublicationQueryInvokePermission",
    "PublicationQueryRoute",
    "PublicationRequestIntegration",
    "PublicationRequestInvokePermission",
    "PublicationRequestRoute",
}
_REQUIRED_ENABLED_CHANGES: Final = {
    *(f"Publication{name}Function" for name in _COMPONENTS),
    "PublicationWorkerRole",
    "PublicationDispatcherStreamMapping",
    "PublicationRetentionStreamMapping",
    "PublicationRecoveryFunctionRecoveryQueue",
    *_RULES,
    *_API_RESOURCES,
}


class Phase718EnabledDeploymentError(RuntimeError):
    """Value-free refusal for incomplete, drifting, or unsafe AWS evidence."""


@dataclass(frozen=True, slots=True)
class Phase718EnabledDeploymentBinding:
    stack_name: str
    release_fingerprint: str
    application_release_fingerprint: str
    archive_fingerprint: str
    phase6_stack_name: str
    routes: tuple[str, str]


def verify_change_set_observation(
    change_set: Mapping[str, object],
    *,
    descriptor: Mapping[str, object],
    expected_parameters: Mapping[str, str],
    phase6_before: Mapping[str, object],
    s3_head: Mapping[str, object],
    archive_path: Path,
    original_template: Mapping[str, object],
    processed_template: Mapping[str, object],
    change_set_name: str,
) -> tuple[str, ...]:
    """Require the exact source template and a non-destructive, target-bounded delta."""

    try:
        parameters = _enabled_parameters(expected_parameters, descriptor=descriptor)
        _verify_phase6_before_capture(phase6_before, parameters=parameters)
        verify_s3_head_observation(
            descriptor,
            s3_head,
            archive_path=archive_path,
            bucket=parameters["EnabledCodeS3Bucket"],
            object_version=parameters["EnabledCodeS3ObjectVersion"],
        )
        if (
            not isinstance(change_set, Mapping)
            or original_template != json.loads(render_phase718_enabled_template())
            or change_set.get("StackName") != STACK_NAME
            or change_set.get("ChangeSetName") != change_set_name
            or change_set.get("Status") != "CREATE_COMPLETE"
            or change_set.get("ExecutionStatus") != "AVAILABLE"
            or _records(change_set.get("Parameters"), "ParameterKey", "ParameterValue")
            != parameters
        ):
            raise ValueError
        target_resources = _mapping(processed_template.get("Resources"))
        source_resources = _mapping(original_template.get("Resources"))
        if not set(source_resources).issubset(target_resources):
            raise ValueError
        changes = change_set.get("Changes")
        if not isinstance(changes, list) or not changes:
            raise ValueError
        observed: set[str] = set()
        for raw_change in changes:
            change = _mapping(raw_change)
            resource = _mapping(change.get("ResourceChange"))
            logical_id = resource.get("LogicalResourceId")
            action = resource.get("Action")
            replacement = resource.get("Replacement", "False")
            if (
                not isinstance(logical_id, str)
                or logical_id not in target_resources
                or logical_id in observed
                or action not in {"Add", "Modify"}
                or replacement != "False"
                or (logical_id in _API_RESOURCES and action != "Add")
                or resource.get("ResourceType")
                != _mapping(target_resources[logical_id]).get("Type")
            ):
                raise ValueError
            observed.add(logical_id)
        if not _REQUIRED_ENABLED_CHANGES.issubset(observed):
            raise ValueError
        return tuple(sorted(observed))
    except Exception:
        raise Phase718EnabledDeploymentError(_GENERIC_ERROR) from None


def verify_enabled_deployment_readback(
    observation: Mapping[str, object],
    *,
    deployment_root: Path,
    archive_path: Path,
    descriptor_path: Path,
    expected_parameters: Mapping[str, str],
) -> Phase718EnabledDeploymentBinding:
    """Authenticate the enabled stack, six functions, triggers, routes, and Phase 6 non-delta."""

    try:
        descriptor = verify_enabled_deployment_artifact(
            deployment_root,
            archive_path=archive_path,
            descriptor_path=descriptor_path,
        )
        parameters = _enabled_parameters(expected_parameters, descriptor=descriptor)
        verify_s3_head_observation(
            descriptor,
            _mapping(observation.get("s3_head")),
            archive_path=archive_path,
            bucket=parameters["EnabledCodeS3Bucket"],
            object_version=parameters["EnabledCodeS3ObjectVersion"],
        )
        environment_name = parameters["EnvironmentName"]
        stack = _one_stack(_mapping(observation.get("stack")), expected_name=STACK_NAME)
        if (
            stack.get("StackStatus") != "UPDATE_COMPLETE"
            or _records(stack.get("Parameters"), "ParameterKey", "ParameterValue") != parameters
            or _records(stack.get("Outputs"), "OutputKey", "OutputValue")
            != {
                "DeploymentReadiness": "GENERAL_AVAILABILITY",
                "EnabledReleaseFingerprint": parameters["EnabledReleaseFingerprint"],
                "ProviderMutationEnabled": "true",
                "PublicationQueryRegistered": "true",
                "PublicationRequestRegistered": "true",
                "PublicationWorkerTriggered": "true",
                "SellerPublicationEnabled": "true",
            }
        ):
            raise ValueError
        phase6_outputs, phase6_parameters = _verify_phase6_unchanged(
            _mapping(observation.get("phase6_before")),
            _mapping(observation.get("phase6_after")),
        )
        _verify_phase6_bindings(
            parameters,
            outputs=phase6_outputs,
            phase6_parameters=phase6_parameters,
        )
        archive_digest = _fingerprint(_mapping(descriptor["archive"])["sha256"])
        _verify_enabled_lambdas(
            _mapping(observation.get("lambda_configurations")),
            _mapping(observation.get("lambda_concurrency")),
            parameters=parameters,
            archive_fingerprint=archive_digest,
        )
        _verify_execution_role_policies(
            _mapping(observation.get("execution_role_policies")),
            parameters=parameters,
        )
        _verify_enabled_mappings(
            _mapping(observation.get("event_source_mappings")),
            parameters=parameters,
        )
        _verify_rules(
            _mapping(observation.get("event_rules")),
            environment_name=environment_name,
            enabled=True,
        )
        _verify_state_machine(
            _mapping(observation.get("state_machine")),
            environment_name=environment_name,
        )
        routes = _verify_api(
            _mapping(observation.get("api")),
            _mapping(observation.get("lambda_policies")),
            parameters=parameters,
        )
        return Phase718EnabledDeploymentBinding(
            stack_name=STACK_NAME,
            release_fingerprint=parameters["EnabledReleaseFingerprint"],
            application_release_fingerprint=parameters["ApplicationReleaseFingerprint"],
            archive_fingerprint=archive_digest,
            phase6_stack_name=PHASE6_STACK_NAME,
            routes=routes,
        )
    except Exception as error:
        if isinstance(error, Phase718EnabledDeploymentError):
            raise
        raise Phase718EnabledDeploymentError(_GENERIC_ERROR) from None


def verify_s3_head_observation(
    descriptor: Mapping[str, object],
    observation: Mapping[str, object],
    *,
    archive_path: Path,
    bucket: str,
    object_version: str,
) -> None:
    """Bind one immutable, checksum-enabled S3 version to the sealed enabled archive."""

    try:
        archive = _mapping(descriptor.get("archive"))
        binding = _mapping(descriptor.get("s3_binding"))
        raw = archive_path.read_bytes()
        release = _fingerprint(descriptor.get("release_fingerprint"))
        if (
            bucket != "mr-lister-phase6-artifacts-dev-384627057108-us-west-2"
            or object_version in {"", "null"}
            or binding.get("key_template") != "phase7/releases/{release_fingerprint}/enabled.zip"
            or observation.get("VersionId") != object_version
            or observation.get("ContentLength") != len(raw)
            or observation.get("ChecksumSHA256")
            != base64.b64encode(sha256(raw).digest()).decode("ascii")
            or observation.get("ContentType") != "application/zip"
            or observation.get("ServerSideEncryption") != "AES256"
            or observation.get("DeleteMarker") not in {None, False}
            or observation.get("Metadata")
            != {
                "mr-lister-archive-sha256": archive.get("sha256"),
                "mr-lister-release-fingerprint": release,
            }
        ):
            raise ValueError
    except Exception:
        raise Phase718EnabledDeploymentError(_GENERIC_ERROR) from None


def verify_predecessor_rollback_readback(
    observation: Mapping[str, object],
    *,
    expected_parameters: Mapping[str, str],
) -> None:
    """Prove rollback restored the exact production-disabled predecessor and removed routes."""

    try:
        authority = _mapping(observation.get("predecessor_template_authority"))
        if authority != {
            "packaged_template_s3_key": PREDECESSOR_PACKAGED_TEMPLATE_KEY,
            "packaged_template_s3_object_version": (PREDECESSOR_PACKAGED_TEMPLATE_OBJECT_VERSION),
            "packaged_template_sha256": PREDECESSOR_PACKAGED_TEMPLATE_SHA256,
        }:
            raise ValueError
        predecessor_template = _processed_template(
            _mapping(observation.get("predecessor_processed_template"))
        )
        rollback_template = _processed_template(
            _mapping(observation.get("rollback_processed_template"))
        )
        _verify_predecessor_processed_template(predecessor_template)
        if rollback_template != predecessor_template:
            raise ValueError
        predecessor_stack = _one_stack(
            _mapping(observation.get("predecessor_stack")),
            expected_name=STACK_NAME,
        )
        stack = _one_stack(_mapping(observation.get("stack")), expected_name=STACK_NAME)
        if (
            stack.get("StackStatus") != "UPDATE_COMPLETE"
            or _rollback_stack_authority(predecessor_stack) != _rollback_stack_authority(stack)
            or _records(stack.get("Parameters"), "ParameterKey", "ParameterValue")
            != dict(expected_parameters)
            or _records(stack.get("Outputs"), "OutputKey", "OutputValue")
            != {
                "DeploymentReadiness": "PRODUCTION_DISABLED",
                "ProviderMutationEnabled": "false",
                "PublicationQueryRegistered": "false",
                "PublicationRequestRegistered": "false",
                "PublicationWorkerTriggered": "false",
                "ResourceInstantiationPossible": "true",
                "SellerPublicationEnabled": "false",
            }
        ):
            raise ValueError
        _verify_phase6_unchanged(
            _mapping(observation.get("phase6_before")),
            _mapping(observation.get("phase6_after")),
        )
        environment_name = cast(str, expected_parameters["EnvironmentName"])
        configurations = _mapping(observation.get("lambda_configurations"))
        concurrency = _mapping(observation.get("lambda_concurrency"))
        predecessor_configurations = _mapping(observation.get("predecessor_lambda_configurations"))
        predecessor_concurrency = _mapping(observation.get("predecessor_lambda_concurrency"))
        if set(predecessor_configurations) != set(configurations) or set(
            predecessor_concurrency
        ) != set(concurrency):
            raise ValueError
        for component in _COMPONENTS:
            configuration = _mapping(configurations.get(component))
            if (
                configuration.get("FunctionName") != _function_name(environment_name, component)
                or configuration.get("Handler")
                != (
                    "mr_lister.cloud.phase7_production_entrypoints."
                    f"publication_{component.casefold()}_handler"
                )
                or configuration.get("State") != "Active"
                or configuration.get("LastUpdateStatus") != "Successful"
                or _mapping(concurrency.get(component)).get("ReservedConcurrentExecutions") != 0
                or _stable_lambda(configuration)
                != _stable_lambda(_mapping(predecessor_configurations[component]))
                or _mapping(predecessor_concurrency[component]).get("ReservedConcurrentExecutions")
                != 0
            ):
                raise ValueError
        _verify_enabled_mappings(
            _mapping(observation.get("event_source_mappings")),
            parameters=expected_parameters,
            enabled=False,
        )
        _verify_rules(
            _mapping(observation.get("event_rules")),
            environment_name=environment_name,
            enabled=False,
        )
        route_items = _sequence(_mapping(observation.get("api")).get("routes"), "Items")
        forbidden = {"GET /v1/jobs/{job_id}/publication", "POST /v1/jobs/{job_id}/publish"}
        if any(_mapping(item).get("RouteKey") in forbidden for item in route_items):
            raise ValueError
    except Exception:
        raise Phase718EnabledDeploymentError(_GENERIC_ERROR) from None


def _enabled_parameters(
    observed: Mapping[str, str],
    *,
    descriptor: Mapping[str, object],
) -> dict[str, str]:
    required = {
        "ActivationMode",
        "ApplicationReleaseFingerprint",
        "CanaryEvidenceFingerprint",
        "EnabledCodeS3Bucket",
        "EnabledCodeS3ObjectVersion",
        "EnabledReleaseFingerprint",
        "EnablementEvidenceFingerprint",
        "EnvironmentName",
        "PrintifySecretArn",
        "SellerHttpApiAuthorizerId",
        "SellerHttpApiId",
        "SellerUserPoolClientId",
        "SellerUserPoolId",
        "StateTableArn",
        "StateTableStreamArn",
    }
    if set(observed) != required:
        raise ValueError
    result = dict(observed)
    if (
        result["ActivationMode"] != "GENERAL_AVAILABILITY"
        or result["EnvironmentName"] != "dev"
        or result["ApplicationReleaseFingerprint"] != descriptor["application_release_fingerprint"]
        or result["CanaryEvidenceFingerprint"] != descriptor["canary_evidence_fingerprint"]
        or result["EnabledReleaseFingerprint"] != descriptor["release_fingerprint"]
        or result["EnablementEvidenceFingerprint"] != descriptor["enablement_evidence_fingerprint"]
        or descriptor.get("state_table") != "mr-lister-phase6-dev"
        or not _FINGERPRINT.fullmatch(result["EnabledReleaseFingerprint"])
        or result["StateTableArn"]
        != f"arn:aws:dynamodb:{REGION}:{ACCOUNT_ID}:table/mr-lister-phase6-dev"
        or not result["StateTableStreamArn"].startswith(result["StateTableArn"] + "/stream/")
        or not result["PrintifySecretArn"].startswith(
            f"arn:aws:secretsmanager:{REGION}:{ACCOUNT_ID}:secret:"
        )
    ):
        raise ValueError
    return result


def _verify_enabled_lambdas(
    configurations: Mapping[str, object],
    concurrency: Mapping[str, object],
    *,
    parameters: Mapping[str, str],
    archive_fingerprint: str,
) -> None:
    if set(configurations) != set(_COMPONENTS) or set(concurrency) != set(_COMPONENTS):
        raise ValueError
    archive_code_sha = base64.b64encode(bytes.fromhex(archive_fingerprint)).decode("ascii")
    environment_name = parameters["EnvironmentName"]
    workflow_arn = (
        f"arn:aws:states:{REGION}:{ACCOUNT_ID}:stateMachine:"
        f"mr-lister-phase7-{environment_name}-publication"
    )
    recovery_queue_url = (
        f"https://sqs.{REGION}.amazonaws.com/{ACCOUNT_ID}/"
        f"mr-lister-phase7-{environment_name}-publication-recovery"
    )
    common = {
        "MR_LISTER_COGNITO_CLIENT_ID": parameters["SellerUserPoolClientId"],
        "MR_LISTER_COGNITO_GROUP": "seller",
        "MR_LISTER_COGNITO_ISSUER": (
            f"https://cognito-idp.{REGION}.amazonaws.com/{parameters['SellerUserPoolId']}"
        ),
        "MR_LISTER_COGNITO_SCOPE": "mr-lister-api/seller",
        "MR_LISTER_PHASE7_ACTIVATION_MODE": "GENERAL_AVAILABILITY",
        "MR_LISTER_PHASE7_CANARY_EVIDENCE_FINGERPRINT": parameters["CanaryEvidenceFingerprint"],
        "MR_LISTER_PHASE7_CONTRACT_FINGERPRINT": CONTRACT_FINGERPRINT,
        "MR_LISTER_PHASE7_CONTRACT_VERSION": "7.1.0",
        "MR_LISTER_PHASE7_DISPATCHER_ENABLED": "true",
        "MR_LISTER_PHASE7_ENABLED_RELEASE_FINGERPRINT": parameters["EnabledReleaseFingerprint"],
        "MR_LISTER_PHASE7_ENABLEMENT_EVIDENCE_FINGERPRINT": parameters[
            "EnablementEvidenceFingerprint"
        ],
        "MR_LISTER_PHASE7_PUBLICATION_ENABLED": "true",
        "MR_LISTER_PHASE7_QUERY_ENABLED": "true",
        "MR_LISTER_PHASE7_RECOVERY_ENABLED": "true",
        "MR_LISTER_PHASE7_REQUEST_ENABLED": "true",
        "MR_LISTER_PHASE7_RETENTION_ENABLED": "true",
        "MR_LISTER_PHASE7_SCAFFOLD_ONLY": "false",
        "MR_LISTER_PHASE7_WORKER_ENABLED": "true",
        "MR_LISTER_PRODUCT_PROFILE_FINGERPRINT": PROFILE_FINGERPRINT,
        "MR_LISTER_PRODUCT_PROFILE_ID": "gildan_64000_swiftpod",
        "MR_LISTER_PRODUCT_PROFILE_PATH": (
            "/var/task/config/product_profiles/gildan_64000_swiftpod.json"
        ),
        "MR_LISTER_PRODUCT_PROFILE_VERSION": "2",
        "MR_LISTER_RELEASE_FINGERPRINT": parameters["ApplicationReleaseFingerprint"],
        "MR_LISTER_STATE_TABLE": "mr-lister-phase6-dev",
    }
    entrypoints = dict(zip(_COMPONENTS, PHASE718_ENTRYPOINTS, strict=True))
    for component in _COMPONENTS:
        configuration = _mapping(configurations[component])
        variables = dict(common)
        if component == "Dispatcher":
            variables.update(
                {
                    "MR_LISTER_PUBLICATION_RECOVERY_QUEUE_URL": recovery_queue_url,
                    "MR_LISTER_PUBLICATION_WORKFLOW_ARN": workflow_arn,
                }
            )
        elif component == "Recovery":
            variables["MR_LISTER_PUBLICATION_WORKFLOW_ARN"] = workflow_arn
        elif component == "Worker":
            variables["MR_LISTER_PRINTIFY_SECRET_ARN"] = parameters["PrintifySecretArn"]
        if (
            configuration.get("FunctionName") != _function_name(environment_name, component)
            or configuration.get("Handler") != entrypoints[component]
            or configuration.get("Runtime") != "python3.12"
            or configuration.get("Architectures") != ["arm64"]
            or configuration.get("MemorySize") != 512
            or configuration.get("Timeout") != _TIMEOUTS[component]
            or configuration.get("Role")
            != (
                f"arn:aws:iam::{ACCOUNT_ID}:role/"
                f"mr-lister-phase7-{environment_name}-publication-{component.casefold()}-role"
            )
            or configuration.get("CodeSha256") != archive_code_sha
            or configuration.get("State") != "Active"
            or configuration.get("LastUpdateStatus") != "Successful"
            or _mapping(configuration.get("Environment")).get("Variables") != variables
            or _mapping(concurrency[component]).get("ReservedConcurrentExecutions") != 1
        ):
            raise ValueError


def _verify_enabled_mappings(
    observations: Mapping[str, object],
    *,
    parameters: Mapping[str, str],
    enabled: bool = True,
) -> None:
    if set(observations) != {"Dispatcher", "Recovery", "Retention"}:
        raise ValueError
    environment_name = parameters["EnvironmentName"]
    expected_state = "Enabled" if enabled else "Disabled"
    dlq_arn = (
        f"arn:aws:sqs:{REGION}:{ACCOUNT_ID}:"
        f"mr-lister-phase7-{environment_name}-publication-operations-dlq"
    )
    recovery_queue_arn = (
        f"arn:aws:sqs:{REGION}:{ACCOUNT_ID}:"
        f"mr-lister-phase7-{environment_name}-publication-recovery"
    )
    expected = {
        "Dispatcher": {
            "BatchSize": 25,
            "BisectBatchOnFunctionError": True,
            "DestinationConfig": {"OnFailure": {"Destination": dlq_arn}},
            "EventSourceArn": parameters["StateTableStreamArn"],
            "FilterCriteria": {
                "Filters": [
                    {
                        "Pattern": (
                            '{"eventName":["INSERT","MODIFY"],"dynamodb":{"Keys":'
                            '{"SK":{"S":[{"prefix":"PUBLICATION_WORK#"}]}}}}'
                        )
                    }
                ]
            },
            "MaximumBatchingWindowInSeconds": 1,
            "MaximumRecordAgeInSeconds": 300,
            "MaximumRetryAttempts": 2,
            "StartingPosition": "LATEST",
        },
        "Recovery": {
            "BatchSize": 1,
            "EventSourceArn": recovery_queue_arn,
            "FunctionResponseTypes": ["ReportBatchItemFailures"],
        },
        "Retention": {
            "BatchSize": 1,
            "BisectBatchOnFunctionError": True,
            "DestinationConfig": {"OnFailure": {"Destination": dlq_arn}},
            "EventSourceArn": parameters["StateTableStreamArn"],
            "FilterCriteria": {
                "Filters": [
                    {
                        "Pattern": (
                            '{"eventName":["INSERT"],"dynamodb":{"Keys":{"PK":{"S":'
                            '[{"prefix":"PUBLICATION#"}]},"SK":{"S":["TERMINAL_JOB_LINK"]}},'
                            '"StreamViewType":["KEYS_ONLY"]}}'
                        )
                    }
                ]
            },
            "MaximumBatchingWindowInSeconds": 0,
            "MaximumRecordAgeInSeconds": 300,
            "MaximumRetryAttempts": 2,
            "StartingPosition": "LATEST",
        },
    }
    for component in ("Dispatcher", "Recovery", "Retention"):
        items = _sequence(_mapping(observations[component]), "EventSourceMappings")
        matches = [
            _mapping(item)
            for item in items
            if _mapping(item)
            .get("FunctionArn", "")
            .endswith(":function:" + _function_name(environment_name, component))
        ]
        if (
            len(matches) != 1
            or matches[0].get("State") != expected_state
            or any(matches[0].get(key) != value for key, value in expected[component].items())
        ):
            raise ValueError


def _verify_execution_role_policies(
    observations: Mapping[str, object],
    *,
    parameters: Mapping[str, str],
) -> None:
    if set(observations) != set(_COMPONENTS):
        raise ValueError
    for component in _COMPONENTS:
        observation = _mapping(observations[component])
        if set(observation) != {"attached_policies", "inline_policy", "inline_policy_names"}:
            raise ValueError
        role_name = f"mr-lister-phase7-{parameters['EnvironmentName']}-publication-"
        role_name += f"{component.casefold()}-role"
        policy_name = _POLICY_NAMES[component]
        inline_names = _mapping(observation["inline_policy_names"])
        attached = _mapping(observation["attached_policies"])
        inline = _mapping(observation["inline_policy"])
        if (
            inline_names.get("PolicyNames") != [policy_name]
            or attached.get("AttachedPolicies") != []
            or inline.get("RoleName") != role_name
            or inline.get("PolicyName") != policy_name
            or inline.get("PolicyDocument")
            != _expected_execution_policy(component, parameters=parameters)
        ):
            raise ValueError


def _expected_execution_policy(
    component: str,
    *,
    parameters: Mapping[str, str],
) -> Mapping[str, object]:
    template = json.loads(render_phase718_enabled_template())
    policy = template["Resources"][f"Publication{component}Role"]["Properties"]["Policies"][0][
        "PolicyDocument"
    ]
    resolved = _resolve_policy_value(policy, parameters=parameters)
    if not isinstance(resolved, Mapping):
        raise ValueError
    return cast(Mapping[str, object], resolved)


def _resolve_policy_value(value: object, *, parameters: Mapping[str, str]) -> object:
    if isinstance(value, list):
        return [_resolve_policy_value(item, parameters=parameters) for item in value]
    if not isinstance(value, Mapping):
        return value
    if set(value) == {"Ref"}:
        reference = value["Ref"]
        if not isinstance(reference, str):
            raise ValueError
        return _policy_reference(reference, parameters=parameters)
    if set(value) == {"Fn::GetAtt"}:
        get_att = value["Fn::GetAtt"]
        if (
            not isinstance(get_att, list)
            or len(get_att) != 2
            or not all(isinstance(item, str) for item in get_att)
            or get_att[1] != "Arn"
        ):
            raise ValueError
        return _policy_resource_arn(cast(str, get_att[0]), parameters=parameters)
    if set(value) == {"Fn::Sub"}:
        substitute = value["Fn::Sub"]
        if not isinstance(substitute, str):
            raise ValueError
        return re.sub(
            r"\$\{([^}]+)\}",
            lambda match: _policy_substitution(match.group(1), parameters=parameters),
            substitute,
        )
    return {
        str(key): _resolve_policy_value(item, parameters=parameters) for key, item in value.items()
    }


def _policy_reference(reference: str, *, parameters: Mapping[str, str]) -> str:
    if reference in parameters:
        return parameters[reference]
    if reference == "AWS::Region":
        return REGION
    if reference == "PublicationWorkflowStateMachine":
        return (
            f"arn:aws:states:{REGION}:{ACCOUNT_ID}:stateMachine:"
            f"mr-lister-phase7-{parameters['EnvironmentName']}-publication"
        )
    raise ValueError


def _policy_resource_arn(logical_id: str, *, parameters: Mapping[str, str]) -> str:
    environment = parameters["EnvironmentName"]
    if logical_id.startswith("Publication") and logical_id.endswith("LogGroup"):
        component = logical_id.removeprefix("Publication").removesuffix("LogGroup")
        return (
            f"arn:aws:logs:{REGION}:{ACCOUNT_ID}:log-group:/aws/lambda/"
            f"mr-lister-phase7-{environment}-publication-{component.casefold()}:*"
        )
    queues = {
        "PublicationWorkflowRecoveryQueue": "publication-recovery",
        "PublicationOperationsDeadLetterQueue": "publication-operations-dlq",
    }
    suffix = queues.get(logical_id)
    if suffix is not None:
        return f"arn:aws:sqs:{REGION}:{ACCOUNT_ID}:mr-lister-phase7-{environment}-{suffix}"
    raise ValueError


def _policy_substitution(value: str, *, parameters: Mapping[str, str]) -> str:
    if value.endswith(".Arn"):
        return _policy_resource_arn(value.removesuffix(".Arn"), parameters=parameters)
    pseudo = {
        "AWS::AccountId": ACCOUNT_ID,
        "AWS::Partition": "aws",
        "AWS::Region": REGION,
    }
    if value in pseudo:
        return pseudo[value]
    return _policy_reference(value, parameters=parameters)


def _verify_rules(
    observations: Mapping[str, object],
    *,
    environment_name: str,
    enabled: bool,
) -> None:
    if set(observations) != set(_RULES):
        raise ValueError
    expected_state = "ENABLED" if enabled else "DISABLED"
    expected_names = {
        "PublicationDueWorkSweepRule": (
            f"mr-lister-phase7-{environment_name}-publication-due-sweep"
        ),
        "PublicationRecoverySweepRule": (
            f"mr-lister-phase7-{environment_name}-publication-recovery-sweep"
        ),
        "PublicationWorkflowFailureRule": (
            f"mr-lister-phase7-{environment_name}-publication-workflow-failure"
        ),
    }
    for logical_id, expected_name in expected_names.items():
        observation = _mapping(observations[logical_id])
        if set(observation) != {"rule", "targets"}:
            raise ValueError
        rule = _mapping(observation["rule"])
        targets = _sequence(_mapping(observation["targets"]), "Targets")
        if (
            rule.get("Name") != expected_name
            or rule.get("State") != expected_state
            or len(targets) != 1
        ):
            raise ValueError
    _verify_rule_semantics(observations, environment_name=environment_name)


def _verify_rule_semantics(
    observations: Mapping[str, object],
    *,
    environment_name: str,
) -> None:
    lambda_prefix = f"arn:aws:lambda:{REGION}:{ACCOUNT_ID}:function:mr-lister-phase7-"
    lambda_prefix += f"{environment_name}-publication-"
    sqs_prefix = f"arn:aws:sqs:{REGION}:{ACCOUNT_ID}:mr-lister-phase7-{environment_name}-"
    workflow_arn = (
        f"arn:aws:states:{REGION}:{ACCOUNT_ID}:stateMachine:"
        f"mr-lister-phase7-{environment_name}-publication"
    )
    dlq = f"{sqs_prefix}publication-operations-dlq"
    common_retry = {"MaximumEventAgeInSeconds": 3600, "MaximumRetryAttempts": 2}
    expected = {
        "PublicationDueWorkSweepRule": {
            "rule": {"ScheduleExpression": "rate(1 minute)"},
            "target": {
                "Arn": f"{lambda_prefix}dispatcher",
                "DeadLetterConfig": {"Arn": dlq},
                "Id": "PublicationDispatcher",
                "Input": '{"kind":"publication_due_sweep"}',
                "RetryPolicy": common_retry,
            },
        },
        "PublicationRecoverySweepRule": {
            "rule": {"ScheduleExpression": "rate(1 minute)"},
            "target": {
                "Arn": f"{lambda_prefix}recovery",
                "DeadLetterConfig": {"Arn": dlq},
                "Id": "PublicationRecovery",
                "Input": '{"kind":"publication_recovery_sweep"}',
                "RetryPolicy": common_retry,
            },
        },
        "PublicationWorkflowFailureRule": {
            "rule": {
                "EventPattern": {
                    "detail": {
                        "stateMachineArn": [workflow_arn],
                        "status": ["FAILED", "TIMED_OUT", "ABORTED"],
                    },
                    "detail-type": ["Step Functions Execution Status Change"],
                    "source": ["aws.states"],
                }
            },
            "target": {
                "Arn": f"{sqs_prefix}publication-recovery",
                "DeadLetterConfig": {"Arn": dlq},
                "Id": "PublicationRecoveryQueue",
                "InputTransformer": {
                    "InputPathsMap": {
                        "execution_arn": "$.detail.executionArn",
                        "machine_arn": "$.detail.stateMachineArn",
                        "status": "$.detail.status",
                    },
                    "InputTemplate": (
                        '{"execution_arn":<execution_arn>,"machine_arn":<machine_arn>,'
                        '"status":<status>}'
                    ),
                },
                "RetryPolicy": common_retry,
            },
        },
    }
    for logical_id, values in expected.items():
        observation = _mapping(observations[logical_id])
        rule = _mapping(observation["rule"])
        target = _mapping(_sequence(_mapping(observation["targets"]), "Targets")[0])
        expected_rule = _mapping(values["rule"])
        for key, expected_value in expected_rule.items():
            observed_value = rule.get(key)
            if key == "EventPattern" and isinstance(observed_value, str):
                observed_value = json.loads(observed_value)
            if observed_value != expected_value:
                raise ValueError
        if target != values["target"]:
            raise ValueError


def _verify_state_machine(observation: Mapping[str, object], *, environment_name: str) -> None:
    definition = observation.get("definition")
    if isinstance(definition, str):
        definition = json.loads(definition)
    worker_arn = (
        f"arn:aws:lambda:{REGION}:{ACCOUNT_ID}:function:"
        f"{_function_name(environment_name, 'Worker')}"
    )
    raw_expected = WORKFLOW_DEFINITION.read_text(encoding="utf-8")
    if raw_expected.count("${PublicationWorkerFunctionArn}") != 1:
        raise ValueError
    expected_definition = json.loads(
        raw_expected.replace("${PublicationWorkerFunctionArn}", worker_arn)
    )
    if (
        observation.get("name") != f"mr-lister-phase7-{environment_name}-publication"
        or observation.get("status") != "ACTIVE"
        or observation.get("type") != "STANDARD"
        or definition != expected_definition
    ):
        raise ValueError


def _verify_api(
    observation: Mapping[str, object],
    policies: Mapping[str, object],
    *,
    parameters: Mapping[str, str],
) -> tuple[str, str]:
    if set(policies) != {"Query", "Request"}:
        raise ValueError
    _verify_api_authority(observation, parameters=parameters)
    routes = _sequence(_mapping(observation.get("routes")), "Items")
    integrations = _sequence(_mapping(observation.get("integrations")), "Items")
    integration_by_id = {
        cast(str, _mapping(item).get("IntegrationId")): _mapping(item) for item in integrations
    }
    cases = (
        ("Query", "GET /v1/jobs/{job_id}/publication", "GET", "/v1/jobs/*/publication"),
        ("Request", "POST /v1/jobs/{job_id}/publish", "POST", "/v1/jobs/*/publish"),
    )
    found: list[str] = []
    for component, route_key, source_method, source_path in cases:
        matches = [_mapping(item) for item in routes if _mapping(item).get("RouteKey") == route_key]
        if len(matches) != 1:
            raise ValueError
        route = matches[0]
        target = route.get("Target")
        if (
            not isinstance(target, str)
            or not target.startswith("integrations/")
            or sum(_mapping(item).get("Target") == target for item in routes) != 1
        ):
            raise ValueError
        integration = integration_by_id.get(target.removeprefix("integrations/"))
        function_arn = (
            f"arn:aws:lambda:{REGION}:{ACCOUNT_ID}:function:"
            f"{_function_name(parameters['EnvironmentName'], component)}"
        )
        expected_uri = (
            f"arn:aws:apigateway:{REGION}:lambda:path/2015-03-31/functions/"
            f"{function_arn}/invocations"
        )
        if (
            route.get("AuthorizationType") != "JWT"
            or route.get("AuthorizationScopes") != ["mr-lister-api/seller"]
            or route.get("AuthorizerId") != parameters["SellerHttpApiAuthorizerId"]
            or integration is None
            or integration.get("IntegrationType") != "AWS_PROXY"
            or integration.get("IntegrationMethod") != "POST"
            or integration.get("IntegrationUri") != expected_uri
            or integration.get("PayloadFormatVersion") != "2.0"
        ):
            raise ValueError
        source_arn = (
            f"arn:aws:execute-api:{REGION}:{ACCOUNT_ID}:{parameters['SellerHttpApiId']}"
            f"/*/{source_method}{source_path}"
        )
        _verify_lambda_policy(
            _mapping(policies[component]),
            function_arn=function_arn,
            source_arn=source_arn,
        )
        found.append(route_key)
    return cast(tuple[str, str], tuple(found))


def _verify_api_authority(
    observation: Mapping[str, object],
    *,
    parameters: Mapping[str, str],
) -> None:
    api = _mapping(observation.get("configuration"))
    authorizer = _mapping(observation.get("authorizer"))
    expected_issuer = f"https://cognito-idp.{REGION}.amazonaws.com/{parameters['SellerUserPoolId']}"
    if (
        api.get("ApiId") != parameters["SellerHttpApiId"]
        or api.get("ProtocolType") != "HTTP"
        or authorizer.get("AuthorizerId") != parameters["SellerHttpApiAuthorizerId"]
        or authorizer.get("AuthorizerType") != "JWT"
        or authorizer.get("IdentitySource") != ["$request.header.Authorization"]
        or _mapping(authorizer.get("JwtConfiguration"))
        != {
            "Audience": [parameters["SellerUserPoolClientId"]],
            "Issuer": expected_issuer,
        }
    ):
        raise ValueError


def _verify_lambda_policy(
    observation: Mapping[str, object],
    *,
    function_arn: str,
    source_arn: str,
) -> None:
    policy = observation.get("Policy")
    if not isinstance(policy, str):
        raise ValueError
    statements = _mapping(json.loads(policy)).get("Statement")
    if not isinstance(statements, list):
        raise ValueError
    matches = []
    for raw_statement in statements:
        statement = _mapping(raw_statement)
        condition = _mapping(statement.get("Condition"))
        arn_like = _mapping(condition.get("ArnLike"))
        string_equals = _mapping(condition.get("StringEquals"))
        if (
            statement.get("Effect") == "Allow"
            and statement.get("Action") == "lambda:InvokeFunction"
            and statement.get("Resource") == function_arn
            and _mapping(statement.get("Principal")).get("Service") == "apigateway.amazonaws.com"
            and arn_like.get("AWS:SourceArn") == source_arn
            and string_equals.get("AWS:SourceAccount") == ACCOUNT_ID
        ):
            matches.append(statement)
    if len(statements) != 1 or len(matches) != 1:
        raise ValueError


def _verify_phase6_unchanged(
    before: Mapping[str, object],
    after: Mapping[str, object],
) -> tuple[dict[str, str], dict[str, str]]:
    prior = _one_stack(before, expected_name=PHASE6_STACK_NAME)
    current = _one_stack(after, expected_name=PHASE6_STACK_NAME)
    if (
        prior.get("StackStatus") != "UPDATE_COMPLETE"
        or current.get("StackStatus") != "UPDATE_COMPLETE"
        or _stable_stack(prior) != _stable_stack(current)
    ):
        raise ValueError
    return (
        _records(prior.get("Outputs"), "OutputKey", "OutputValue"),
        _records(prior.get("Parameters"), "ParameterKey", "ParameterValue"),
    )


def _verify_phase6_bindings(
    parameters: Mapping[str, str],
    *,
    outputs: Mapping[str, str],
    phase6_parameters: Mapping[str, str],
) -> None:
    expected_api_origin = (
        f"https://{parameters['SellerHttpApiId']}.execute-api.{REGION}.amazonaws.com"
    )
    if (
        outputs.get("SellerApiOrigin") != expected_api_origin
        or outputs.get("SellerUserPoolId") != parameters["SellerUserPoolId"]
        or outputs.get("SellerUserPoolClientId") != parameters["SellerUserPoolClientId"]
        or outputs.get("StateTableName") != "mr-lister-phase6-dev"
        or (
            "StateTableArn" in outputs
            and outputs.get("StateTableArn") != parameters["StateTableArn"]
        )
        or outputs.get("ArtifactBucketName") != parameters["EnabledCodeS3Bucket"]
        or phase6_parameters.get("ReleaseFingerprint")
        != parameters["ApplicationReleaseFingerprint"]
        or phase6_parameters.get("PrintifySecretArn") != parameters["PrintifySecretArn"]
    ):
        raise ValueError


def _verify_phase6_before_capture(
    capture: Mapping[str, object],
    *,
    parameters: Mapping[str, str],
) -> None:
    if set(capture) != {"api", "stack", "table"}:
        raise ValueError
    stack = _one_stack(_mapping(capture["stack"]), expected_name=PHASE6_STACK_NAME)
    if stack.get("StackStatus") != "UPDATE_COMPLETE":
        raise ValueError
    _stable_stack(stack)
    _verify_phase6_bindings(
        parameters,
        outputs=_records(stack.get("Outputs"), "OutputKey", "OutputValue"),
        phase6_parameters=_records(
            stack.get("Parameters"),
            "ParameterKey",
            "ParameterValue",
        ),
    )
    _verify_api_authority(_mapping(capture["api"]), parameters=parameters)
    table = _mapping(_mapping(capture["table"]).get("Table"))
    if (
        table.get("TableName") != "mr-lister-phase6-dev"
        or table.get("TableArn") != parameters["StateTableArn"]
        or table.get("LatestStreamArn") != parameters["StateTableStreamArn"]
        or table.get("TableStatus") != "ACTIVE"
        or _mapping(table.get("StreamSpecification"))
        != {"StreamEnabled": True, "StreamViewType": "KEYS_ONLY"}
    ):
        raise ValueError


def _processed_template(observation: Mapping[str, object]) -> Mapping[str, object]:
    if set(observation) - {"StagesAvailable", "TemplateBody"}:
        raise ValueError
    stages = observation.get("StagesAvailable")
    body = observation.get("TemplateBody")
    if not isinstance(body, Mapping) or (
        stages is not None and (not isinstance(stages, list) or "Processed" not in stages)
    ):
        raise ValueError
    return cast(Mapping[str, object], body)


def _verify_predecessor_processed_template(template: Mapping[str, object]) -> None:
    base = _mapping(json.loads(BASE_TEMPLATE.read_bytes()))
    if (
        "Transform" in template
        or "Globals" in template
        or template.get("Description") != base.get("Description")
        or _mapping(template.get("Parameters")) != _mapping(base.get("Parameters"))
        or _mapping(template.get("Outputs")) != _mapping(base.get("Outputs"))
        or _mapping(template.get("Conditions")) != _mapping(base.get("Conditions"))
    ):
        raise ValueError
    base_resources = _mapping(base.get("Resources"))
    resources = _mapping(template.get("Resources"))
    expected_names = {*base_resources, "PublicationRecoveryFunctionRecoveryQueue"}
    if set(resources) != expected_names:
        raise ValueError
    for logical_id, base_resource in base_resources.items():
        source_type = _mapping(base_resource).get("Type")
        expected_type = {
            "AWS::Serverless::Function": "AWS::Lambda::Function",
            "AWS::Serverless::StateMachine": "AWS::StepFunctions::StateMachine",
        }.get(cast(str, source_type), source_type)
        if _mapping(resources[logical_id]).get("Type") != expected_type:
            raise ValueError
    for component in _COMPONENTS:
        properties = _mapping(
            _mapping(resources[f"Publication{component}Function"]).get("Properties")
        )
        code = _mapping(properties.get("Code"))
        variables = _mapping(_mapping(properties.get("Environment")).get("Variables"))
        if (
            properties.get("Handler")
            != (
                "mr_lister.cloud.phase7_production_entrypoints."
                f"publication_{component.casefold()}_handler"
            )
            or properties.get("ReservedConcurrentExecutions") != 0
            or code
            != {
                "S3Bucket": {"Ref": "CandidateCodeS3Bucket"},
                "S3Key": {
                    "Fn::Sub": (
                        "phase7/candidates/${CandidateReleaseFingerprint}/production-disabled.zip"
                    )
                },
                "S3ObjectVersion": {"Ref": "CandidateCodeS3ObjectVersion"},
            }
            or variables.get("MR_LISTER_PHASE7_PUBLICATION_ENABLED") != "false"
            or "MR_LISTER_PRINTIFY_SECRET_ARN" in variables
        ):
            raise ValueError
    for logical_id in (
        "PublicationDispatcherStreamMapping",
        "PublicationRetentionStreamMapping",
        "PublicationRecoveryFunctionRecoveryQueue",
    ):
        if _mapping(_mapping(resources[logical_id]).get("Properties")).get("Enabled") is not False:
            raise ValueError
    for logical_id in _RULES:
        if _mapping(_mapping(resources[logical_id]).get("Properties")).get("State") != "DISABLED":
            raise ValueError
    workflow = _mapping(_mapping(resources["PublicationWorkflowStateMachine"]).get("Properties"))
    definition_location = _mapping(workflow.get("DefinitionS3Location"))
    if set(definition_location) != {"Bucket", "Key", "Version"} or not all(
        isinstance(value, str) and value for value in definition_location.values()
    ):
        raise ValueError
    serialized = json.dumps(template, sort_keys=True)
    if (
        "AWS::ApiGatewayV2::Route" in serialized
        or "AWS::ApiGatewayV2::Integration" in serialized
        or "secretsmanager:GetSecretValue" in serialized
    ):
        raise ValueError


def _rollback_stack_authority(stack: Mapping[str, object]) -> dict[str, object]:
    return {
        "EnableTerminationProtection": stack.get("EnableTerminationProtection"),
        "NotificationARNs": stack.get("NotificationARNs", []),
        "Outputs": _records(stack.get("Outputs"), "OutputKey", "OutputValue"),
        "Parameters": _records(stack.get("Parameters"), "ParameterKey", "ParameterValue"),
        "RoleARN": stack.get("RoleARN"),
        "StackId": stack.get("StackId"),
        "StackName": stack.get("StackName"),
        "Tags": _records(stack.get("Tags"), "Key", "Value"),
    }


def _stable_lambda(configuration: Mapping[str, object]) -> dict[str, object]:
    ignored = {
        "LastModified",
        "LastUpdateStatus",
        "LastUpdateStatusReason",
        "LastUpdateStatusReasonCode",
        "RevisionId",
        "State",
        "StateReason",
        "StateReasonCode",
    }
    required = {"CodeSha256", "Environment", "FunctionName", "Handler", "Role", "Runtime"}
    if not required.issubset(configuration):
        raise ValueError
    return {key: value for key, value in configuration.items() if key not in ignored}


def _stable_stack(stack: Mapping[str, object]) -> dict[str, object]:
    creation_time = stack.get("CreationTime")
    last_updated_time = stack.get("LastUpdatedTime")
    if not isinstance(creation_time, str) or not isinstance(last_updated_time, str):
        raise ValueError
    return {
        "CreationTime": creation_time,
        "EnableTerminationProtection": stack.get("EnableTerminationProtection"),
        "LastUpdatedTime": last_updated_time,
        "Outputs": _records(stack.get("Outputs"), "OutputKey", "OutputValue"),
        "Parameters": _records(stack.get("Parameters"), "ParameterKey", "ParameterValue"),
        "StackId": stack.get("StackId"),
        "StackName": stack.get("StackName"),
        "Tags": _records(stack.get("Tags"), "Key", "Value"),
    }


def _one_stack(value: Mapping[str, object], *, expected_name: str) -> Mapping[str, object]:
    stacks = value.get("Stacks")
    if not isinstance(stacks, list) or len(stacks) != 1:
        raise ValueError
    stack = _mapping(stacks[0])
    if stack.get("StackName") != expected_name:
        raise ValueError
    return stack


def _function_name(environment_name: str, component: str) -> str:
    return f"mr-lister-phase7-{environment_name}-publication-{component.casefold()}"


def _records(value: object, key_name: str, value_name: str) -> dict[str, str]:
    if not isinstance(value, list):
        raise ValueError
    result: dict[str, str] = {}
    for raw_record in value:
        record = _mapping(raw_record)
        key = record.get(key_name)
        item = record.get(value_name)
        if not isinstance(key, str) or not isinstance(item, str) or key in result:
            raise ValueError
        result[key] = item
    return result


def _sequence(value: object, key: str) -> list[object]:
    items = _mapping(value).get(key)
    if not isinstance(items, list):
        raise ValueError
    return items


def _fingerprint(value: object) -> str:
    if not isinstance(value, str) or _FINGERPRINT.fullmatch(value) is None:
        raise ValueError
    return value


def _mapping(value: object) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError
    return cast(Mapping[str, Any], value)


def _read_json(path: Path) -> Mapping[str, object]:
    if path.is_symlink() or not path.is_file() or not 1 <= path.stat().st_size <= 16 << 20:
        raise ValueError
    value = json.loads(path.read_bytes())
    if not isinstance(value, Mapping):
        raise ValueError
    return cast(Mapping[str, object], value)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("change-set", "enabled", "rollback"), required=True)
    parser.add_argument("--observation", type=Path, required=True)
    parser.add_argument("--parameters", type=Path)
    parser.add_argument("--original-template", type=Path)
    parser.add_argument("--processed-template", type=Path)
    parser.add_argument("--change-set-name")
    parser.add_argument("--deployment-root", type=Path)
    parser.add_argument("--archive", type=Path)
    parser.add_argument("--descriptor", type=Path)
    parser.add_argument("--phase6-before", type=Path)
    parser.add_argument("--s3-head", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        observation = _read_json(arguments.observation)
        if arguments.mode == "change-set":
            if (
                arguments.parameters is None
                or arguments.deployment_root is None
                or arguments.archive is None
                or arguments.descriptor is None
                or arguments.phase6_before is None
                or arguments.s3_head is None
                or arguments.original_template is None
                or arguments.processed_template is None
                or arguments.change_set_name is None
            ):
                raise ValueError
            descriptor = verify_enabled_deployment_artifact(
                arguments.deployment_root,
                archive_path=arguments.archive,
                descriptor_path=arguments.descriptor,
            )
            parameters = {
                str(key): str(value) for key, value in _read_json(arguments.parameters).items()
            }
            changes = verify_change_set_observation(
                observation,
                descriptor=descriptor,
                expected_parameters=parameters,
                phase6_before=_read_json(arguments.phase6_before),
                s3_head=_read_json(arguments.s3_head),
                archive_path=arguments.archive,
                original_template=_read_json(arguments.original_template),
                processed_template=_read_json(arguments.processed_template),
                change_set_name=arguments.change_set_name,
            )
            output = {
                "change_count": len(changes),
                "stack_name": STACK_NAME,
                "status": "passed",
            }
        elif arguments.mode == "enabled":
            if (
                arguments.parameters is None
                or arguments.deployment_root is None
                or arguments.archive is None
                or arguments.descriptor is None
            ):
                raise ValueError
            parameters = {
                str(key): str(value) for key, value in _read_json(arguments.parameters).items()
            }
            result = verify_enabled_deployment_readback(
                observation,
                deployment_root=arguments.deployment_root,
                archive_path=arguments.archive,
                descriptor_path=arguments.descriptor,
                expected_parameters=parameters,
            )
            output = {
                "release_fingerprint": result.release_fingerprint,
                "stack_name": result.stack_name,
                "status": "passed",
            }
        else:
            if arguments.parameters is None:
                raise ValueError
            parameters = {
                str(key): str(value) for key, value in _read_json(arguments.parameters).items()
            }
            verify_predecessor_rollback_readback(
                observation,
                expected_parameters=parameters,
            )
            output = {"stack_name": STACK_NAME, "status": "rollback-restored"}
        print(json.dumps(output, sort_keys=True))
        return 0
    except Exception:
        print(json.dumps({"status": "refused"}, sort_keys=True))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "ACCOUNT_ID",
    "PHASE6_STACK_NAME",
    "REGION",
    "STACK_NAME",
    "Phase718EnabledDeploymentBinding",
    "Phase718EnabledDeploymentError",
    "verify_change_set_observation",
    "verify_enabled_deployment_readback",
    "verify_predecessor_rollback_readback",
    "verify_s3_head_observation",
]
