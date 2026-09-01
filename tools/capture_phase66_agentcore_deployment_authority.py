#!/usr/bin/env python3
"""Capture the exact deployed Phase 6.6 artwork-closure AgentCore authority.

The closure is a bounded release mosaic: the stack-wide release stays on its sealed predecessor,
while the preparation dispatcher explicitly overrides that value with the artwork-closure release
bound to AgentCore v3. Callers cannot substitute an account, Region, stack, runtime, endpoint,
release, or artifact expectation. The capture cross-checks the original stack template, live
dispatcher, AgentCore runtime and custom endpoint, and exact versioned S3 CodeZip object. AWS
clients are injected for hermetic tests. The CLI writes only one create-only, owner-only JSON
document beneath the repository-private Phase 6.6 acceptance root.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import secrets
import stat
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import Any, Final, Protocol

from mr_lister.agent.runtime_binding import (
    AgentCoreRuntimeBinding,
    agentcore_runtime_binding_fingerprint,
    load_agentcore_runtime_binding,
)
from tools.verify_phase6_agentcore_endpoint_observation import (
    verify_phase6_agentcore_endpoint_observation,
)
from tools.verify_phase6_s3_release_object import validate_phase6_s3_version_id

REPOSITORY_ROOT: Final = Path(__file__).resolve().parents[1]
PRIVATE_OUTPUT_ROOT: Final = REPOSITORY_ROOT / ".mr_lister_private" / "phase66-acceptance"

FORMAT: Final = "phase6.6-agentcore-deployment-authority-v1"
EXPECTED_PROFILE: Final = "mr-lister-dev"
EXPECTED_ACCOUNT_ID: Final = "384627057108"
EXPECTED_CALLER_ARN: Final = f"arn:aws:iam::{EXPECTED_ACCOUNT_ID}:user/mr-lister-dev"
EXPECTED_REGION: Final = "us-west-2"
EXPECTED_ENVIRONMENT: Final = "dev"
EXPECTED_STACK_NAME: Final = "mr-lister-phase6-dev"
EXPECTED_READINESS: Final = "WEB_EDGE_ACTIVE_DRAFT_ONLY"
EXPECTED_RUNTIME_NAME: Final = "mr_lister_phase6"
EXPECTED_RUNTIME_LANGUAGE: Final = "PYTHON_3_12"
EXPECTED_RUNTIME_ENTRY_POINT: Final = ["main.py"]
EXPECTED_PREPARATION_LOGICAL_ID: Final = "PreparationDispatchFunction"
EXPECTED_SHARED_RELEASE_FINGERPRINT: Final = (
    "0c6211a5b0244e9c86d635e6c02e7bc49e5e948d68895b4aaa982c0b0b2e187b"
)
EXPECTED_CLOSURE_RELEASE_FINGERPRINT: Final = (
    "f34ab73042014fccce2cb3733624f005a4ccc10bb065b39c3e20befd3c33923f"
)
EXPECTED_RUNTIME_ID: Final = "mr_lister_phase6-4HoPmq2hCI"
EXPECTED_RUNTIME_ARN: Final = (
    f"arn:aws:bedrock-agentcore:us-west-2:384627057108:runtime/{EXPECTED_RUNTIME_ID}"
)
EXPECTED_RUNTIME_VERSION: Final = "3"
EXPECTED_RUNTIME_QUALIFIER: Final = "phase6_v3_dev"
EXPECTED_ENDPOINT_ARN: Final = (
    f"{EXPECTED_RUNTIME_ARN}/runtime-endpoint/{EXPECTED_RUNTIME_QUALIFIER}"
)
EXPECTED_RUNTIME_BINDING_FINGERPRINT: Final = (
    "d8194386435d2f941d0942b102595830c1efc48e9bc4890457b46e17e0df3196"
)
EXPECTED_AGENTCORE_ARCHIVE_SHA256: Final = (
    "443f62fe01a2ebd54c8ff4b551eab94c829a878b42333973a6731e1cdd105f8b"
)
EXPECTED_AGENTCORE_ARCHIVE_SIZE: Final = 96_310_832
EXPECTED_RUNTIME_ROLE_ARN: Final = (
    f"arn:aws:iam::{EXPECTED_ACCOUNT_ID}:role/mr-lister-phase6-agentcore-runtime-dev"
)
EXPECTED_RUNTIME_DESCRIPTION: Final = "Release-bound Phase 6 Strands preparation runtime"
EXPECTED_RELEASE_TOPOLOGY: Final = "BOUNDED_ARTWORK_CLOSURE_MOSAIC"

_GENERIC_ERROR: Final = "Phase 6.6 AgentCore deployment-authority capture is invalid"
_DIGEST = re.compile(r"^[a-f0-9]{64}$")
_ARCHIVE_KEY = re.compile(
    r"^private/deployments/agentcore/releases/(?P<release>[a-f0-9]{64})/"
    r"phase6-agentcore-(?P<archive>[a-f0-9]{64})\.zip$"
)
_ENDPOINT_ID = re.compile(r"^[A-Za-z0-9][-A-Za-z0-9_]{1,63}$")
_BINDING_PARAMETERS: Final = {
    "AgentCoreRuntimeArn": "MR_LISTER_AGENTCORE_RUNTIME_ARN",
    "AgentCoreRuntimeBindingFingerprint": ("MR_LISTER_AGENTCORE_RUNTIME_BINDING_FINGERPRINT"),
    "AgentCoreRuntimeEndpointArn": "MR_LISTER_AGENTCORE_RUNTIME_ENDPOINT_ARN",
    "AgentCoreRuntimeQualifier": "MR_LISTER_AGENTCORE_RUNTIME_QUALIFIER",
    "AgentCoreRuntimeVersion": "MR_LISTER_AGENTCORE_RUNTIME_VERSION",
}


class Phase66AgentCoreDeploymentAuthorityError(RuntimeError):
    """A value-free AWS authority, validation, confinement, or output failure."""


class AwsClientProvider(Protocol):
    """Minimal injected AWS client factory."""

    def client(self, service_name: str) -> Any: ...


def _canonical(value: object, *, pretty: bool = False) -> bytes:
    return (
        json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=True,
            indent=2 if pretty else None,
            separators=(",", ": ") if pretty else (",", ":"),
            sort_keys=True,
        )
        + ("\n" if pretty else "")
    ).encode("utf-8")


def _digest(value: object) -> str:
    return sha256(_canonical(value)).hexdigest()


def _mapping(value: object) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError
    return value


def _sequence(value: object) -> Sequence[Any]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise ValueError
    return value


def _string(value: object) -> str:
    if not isinstance(value, str) or not value or value != value.strip() or "\x00" in value:
        raise ValueError
    return value


def _timestamp(value: datetime) -> str:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError
    return value.astimezone(UTC).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


def _aware_timestamp(value: object) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError
    return value.astimezone(UTC)


def _client(provider: AwsClientProvider, service_name: str) -> Any:
    client = provider.client(service_name)
    if client is None:
        raise ValueError
    return client


def _expected_runtime_environment() -> dict[str, str]:
    return {
        "AWS_REGION": EXPECTED_REGION,
        "MR_LISTER_ARTIFACT_BUCKET": (
            f"mr-lister-phase6-artifacts-{EXPECTED_ENVIRONMENT}-"
            f"{EXPECTED_ACCOUNT_ID}-{EXPECTED_REGION}"
        ),
        "MR_LISTER_AWS_ACCOUNT_ID": EXPECTED_ACCOUNT_ID,
        "MR_LISTER_ENVIRONMENT": EXPECTED_ENVIRONMENT,
        "MR_LISTER_GEMMA_CONFIG_FINGERPRINT": (
            "f036b77edad91d9923f844d0f4db9725b89574d698cc5ce6fcdee23101f9e929"
        ),
        "MR_LISTER_GEMMA_CONFIG_PATH": "/var/task/config/bedrock/google_gemma_3_27b_it.json",
        "MR_LISTER_PRODUCT_PROFILE_FINGERPRINT": (
            "5de1257141cfdacb1731f68bb9113712957483b33d5b0f7115afdba86eb7476c"
        ),
        "MR_LISTER_PRODUCT_PROFILE_ID": "gildan_64000_swiftpod",
        "MR_LISTER_PRODUCT_PROFILE_PATH": (
            "/var/task/config/product_profiles/gildan_64000_swiftpod.json"
        ),
        "MR_LISTER_PRODUCT_PROFILE_VERSION": "2",
        "MR_LISTER_RELEASE_FINGERPRINT": EXPECTED_CLOSURE_RELEASE_FINGERPRINT,
        "MR_LISTER_STATE_TABLE": "mr-lister-phase6-dev",
        "MR_LISTER_STRANDS_CONTROLLER_MODEL_ID": "us.amazon.nova-2-lite-v1:0",
    }


def _expected_runtime_configuration() -> dict[str, object]:
    return {
        "description": EXPECTED_RUNTIME_DESCRIPTION,
        "environment_variables": _expected_runtime_environment(),
        "lifecycle_configuration": {
            "idleRuntimeSessionTimeout": 900,
            "maxLifetime": 3600,
        },
        "metadata_configuration": {"requireMMDSV2": True},
        "network_configuration": {"networkMode": "PUBLIC"},
        "protocol_configuration": {"serverProtocol": "HTTP"},
        "role_arn": EXPECTED_RUNTIME_ROLE_ARN,
    }


def _stack_parameters(stack: Mapping[str, Any]) -> dict[str, str]:
    parameters: dict[str, str] = {}
    for raw_parameter in _sequence(stack.get("Parameters", [])):
        parameter = _mapping(raw_parameter)
        name = _string(parameter.get("ParameterKey"))
        value = _string(parameter.get("ParameterValue"))
        if name in parameters:
            raise ValueError
        parameters[name] = value
    required = {"EnvironmentName", "ReleaseFingerprint", *_BINDING_PARAMETERS}
    if not required.issubset(parameters):
        raise ValueError
    return parameters


def _stack_outputs(stack: Mapping[str, Any]) -> dict[str, str]:
    outputs: dict[str, str] = {}
    for raw_output in _sequence(stack.get("Outputs", [])):
        output = _mapping(raw_output)
        name = _string(output.get("OutputKey"))
        value = _string(output.get("OutputValue"))
        if name in outputs:
            raise ValueError
        outputs[name] = value
    return outputs


def _require_explicit_dispatch_override(cloudformation: Any) -> None:
    response = _mapping(
        cloudformation.get_template(
            StackName=EXPECTED_STACK_NAME,
            TemplateStage="Original",
        )
    )
    body: object = response.get("TemplateBody")
    if isinstance(body, str):
        body = json.loads(body)
    template = _mapping(body)
    parameters = _mapping(template.get("Parameters"))
    global_release = _mapping(parameters.get("ReleaseFingerprint"))
    globals_value = _mapping(template.get("Globals"))
    global_function = _mapping(globals_value.get("Function"))
    global_environment = _mapping(global_function.get("Environment"))
    global_variables = _mapping(global_environment.get("Variables"))
    resources = _mapping(template.get("Resources"))
    dispatch = _mapping(resources.get(EXPECTED_PREPARATION_LOGICAL_ID))
    dispatch_properties = _mapping(dispatch.get("Properties"))
    dispatch_environment = _mapping(dispatch_properties.get("Environment"))
    dispatch_variables = _mapping(dispatch_environment.get("Variables"))
    if (
        global_release.get("Default") != EXPECTED_SHARED_RELEASE_FINGERPRINT
        or global_release.get("AllowedValues") != [EXPECTED_SHARED_RELEASE_FINGERPRINT]
        or global_variables.get("MR_LISTER_RELEASE_FINGERPRINT") != {"Ref": "ReleaseFingerprint"}
        or dispatch.get("Type") != "AWS::Serverless::Function"
        or dispatch_variables.get("MR_LISTER_RELEASE_FINGERPRINT")
        != EXPECTED_CLOSURE_RELEASE_FINGERPRINT
    ):
        raise ValueError


def _deployed_binding(
    provider: AwsClientProvider,
) -> tuple[AgentCoreRuntimeBinding, str]:
    cloudformation = _client(provider, "cloudformation")
    response = _mapping(cloudformation.describe_stacks(StackName=EXPECTED_STACK_NAME))
    stacks = _sequence(response.get("Stacks"))
    if len(stacks) != 1:
        raise ValueError
    stack = _mapping(stacks[0])
    parameters = _stack_parameters(stack)
    outputs = _stack_outputs(stack)
    if (
        stack.get("StackName") != EXPECTED_STACK_NAME
        or stack.get("StackStatus") != "UPDATE_COMPLETE"
        or stack.get("EnableTerminationProtection") is not True
        or parameters.get("EnvironmentName") != EXPECTED_ENVIRONMENT
        or parameters.get("ReleaseFingerprint") != EXPECTED_SHARED_RELEASE_FINGERPRINT
        or parameters.get("AgentCoreRuntimeArn") != EXPECTED_RUNTIME_ARN
        or parameters.get("AgentCoreRuntimeEndpointArn") != EXPECTED_ENDPOINT_ARN
        or parameters.get("AgentCoreRuntimeQualifier") != EXPECTED_RUNTIME_QUALIFIER
        or parameters.get("AgentCoreRuntimeVersion") != EXPECTED_RUNTIME_VERSION
        or parameters.get("AgentCoreRuntimeBindingFingerprint")
        != EXPECTED_RUNTIME_BINDING_FINGERPRINT
        or outputs.get("DeploymentReadiness") != EXPECTED_READINESS
    ):
        raise ValueError
    _require_explicit_dispatch_override(cloudformation)
    environment = {
        environment_name: parameters[parameter_name]
        for parameter_name, environment_name in _BINDING_PARAMETERS.items()
    }
    binding = load_agentcore_runtime_binding(
        environment,
        region=EXPECTED_REGION,
        account_id=EXPECTED_ACCOUNT_ID,
        environment_name=EXPECTED_ENVIRONMENT,
        release_fingerprint=EXPECTED_CLOSURE_RELEASE_FINGERPRINT,
    )

    detail_response = _mapping(
        cloudformation.describe_stack_resource(
            StackName=EXPECTED_STACK_NAME,
            LogicalResourceId=EXPECTED_PREPARATION_LOGICAL_ID,
        )
    )
    detail = _mapping(detail_response.get("StackResourceDetail"))
    function_name = _string(detail.get("PhysicalResourceId"))
    resource_status = _string(detail.get("ResourceStatus"))
    if (
        detail.get("LogicalResourceId") != EXPECTED_PREPARATION_LOGICAL_ID
        or detail.get("ResourceType") != "AWS::Lambda::Function"
        or not resource_status.endswith("_COMPLETE")
        or resource_status.startswith("DELETE_")
    ):
        raise ValueError
    configuration = _mapping(
        _client(provider, "lambda").get_function_configuration(FunctionName=function_name)
    )
    variables = _mapping(_mapping(configuration.get("Environment")).get("Variables"))
    expected_variables = {
        "MR_LISTER_RELEASE_FINGERPRINT": EXPECTED_CLOSURE_RELEASE_FINGERPRINT,
        **environment,
    }
    if (
        configuration.get("State") != "Active"
        or configuration.get("LastUpdateStatus") != "Successful"
        or any(variables.get(name) != value for name, value in expected_variables.items())
    ):
        raise ValueError
    return binding, _digest(expected_variables)


def _runtime_capture(
    provider: AwsClientProvider,
    binding: AgentCoreRuntimeBinding,
) -> tuple[dict[str, object], str, str, str]:
    runtime_id = binding.runtime_arn.rsplit("/", 1)[-1]
    client = _client(provider, "bedrock-agentcore-control")
    runtime = _mapping(
        client.get_agent_runtime(
            agentRuntimeId=runtime_id,
            agentRuntimeVersion=binding.runtime_version,
        )
    )
    required_runtime_fields = {
        "agentRuntimeArn",
        "agentRuntimeArtifact",
        "agentRuntimeId",
        "agentRuntimeName",
        "agentRuntimeVersion",
        "createdAt",
        "description",
        "environmentVariables",
        "lastUpdatedAt",
        "lifecycleConfiguration",
        "metadataConfiguration",
        "networkConfiguration",
        "protocolConfiguration",
        "roleArn",
        "status",
        "workloadIdentityDetails",
    }
    optional_runtime_fields = {
        "authorizerConfiguration",
        "capacityProviderConfiguration",
        "failureReason",
        "filesystemConfigurations",
        "requestHeaderConfiguration",
        "ResponseMetadata",
    }
    if (
        not required_runtime_fields.issubset(runtime)
        or set(runtime) - required_runtime_fields - optional_runtime_fields
    ):
        raise ValueError
    created_at = _aware_timestamp(runtime.get("createdAt"))
    last_updated_at = _aware_timestamp(runtime.get("lastUpdatedAt"))
    workload_identity = _mapping(runtime.get("workloadIdentityDetails"))
    workload_identity_arn = _string(workload_identity.get("workloadIdentityArn"))
    if (
        created_at > last_updated_at
        or set(workload_identity) != {"workloadIdentityArn"}
        or not workload_identity_arn.startswith(
            f"arn:aws:bedrock-agentcore:{EXPECTED_REGION}:{EXPECTED_ACCOUNT_ID}:"
        )
    ):
        raise ValueError
    environment = _mapping(runtime.get("environmentVariables"))
    artifact = _mapping(runtime.get("agentRuntimeArtifact"))
    if set(artifact) != {"codeConfiguration"}:
        raise ValueError
    code_configuration = _mapping(artifact.get("codeConfiguration"))
    code = _mapping(code_configuration.get("code"))
    if set(code) != {"s3"}:
        raise ValueError
    s3_location = _mapping(code.get("s3"))
    if set(s3_location) != {"bucket", "prefix", "versionId"}:
        raise ValueError
    bucket = _string(s3_location.get("bucket"))
    key = _string(s3_location.get("prefix"))
    version_id = _string(s3_location.get("versionId"))
    validate_phase6_s3_version_id(version_id)
    expected_bucket = (
        f"mr-lister-phase6-artifacts-{EXPECTED_ENVIRONMENT}-{EXPECTED_ACCOUNT_ID}-{EXPECTED_REGION}"
    )
    expected_configuration = _expected_runtime_configuration()
    key_match = _ARCHIVE_KEY.fullmatch(key)
    if (
        binding.runtime_arn != EXPECTED_RUNTIME_ARN
        or binding.endpoint_arn != EXPECTED_ENDPOINT_ARN
        or binding.runtime_version != EXPECTED_RUNTIME_VERSION
        or binding.qualifier != EXPECTED_RUNTIME_QUALIFIER
        or binding.release_fingerprint != EXPECTED_CLOSURE_RELEASE_FINGERPRINT
        or binding.binding_fingerprint != EXPECTED_RUNTIME_BINDING_FINGERPRINT
        or runtime.get("agentRuntimeArn") != EXPECTED_RUNTIME_ARN
        or runtime.get("agentRuntimeId") != EXPECTED_RUNTIME_ID
        or runtime.get("agentRuntimeName") != EXPECTED_RUNTIME_NAME
        or runtime.get("agentRuntimeVersion") != EXPECTED_RUNTIME_VERSION
        or runtime.get("status") != "READY"
        or runtime.get("failureReason") not in {None, ""}
        or runtime.get("description") != expected_configuration["description"]
        or environment != expected_configuration["environment_variables"]
        or runtime.get("roleArn") != expected_configuration["role_arn"]
        or runtime.get("lifecycleConfiguration")
        != expected_configuration["lifecycle_configuration"]
        or runtime.get("networkConfiguration") != expected_configuration["network_configuration"]
        or runtime.get("protocolConfiguration") != expected_configuration["protocol_configuration"]
        or runtime.get("metadataConfiguration") != expected_configuration["metadata_configuration"]
        or runtime.get("authorizerConfiguration") is not None
        or runtime.get("capacityProviderConfiguration") is not None
        or runtime.get("filesystemConfigurations") not in (None, [])
        or runtime.get("requestHeaderConfiguration") not in (None, {"requestHeaderAllowlist": []})
        or code_configuration.get("runtime") != EXPECTED_RUNTIME_LANGUAGE
        or code_configuration.get("entryPoint") != EXPECTED_RUNTIME_ENTRY_POINT
        or bucket != expected_bucket
        or key_match is None
        or key_match.group("release") != EXPECTED_CLOSURE_RELEASE_FINGERPRINT
        or key_match.group("archive") != EXPECTED_AGENTCORE_ARCHIVE_SHA256
    ):
        raise ValueError
    return (
        {
            "arn": EXPECTED_RUNTIME_ARN,
            "configuration_digest": _digest(expected_configuration),
            "id": EXPECTED_RUNTIME_ID,
            "name": EXPECTED_RUNTIME_NAME,
            "role_arn": EXPECTED_RUNTIME_ROLE_ARN,
            "status": "READY",
            "version": EXPECTED_RUNTIME_VERSION,
        },
        bucket,
        key,
        version_id,
    )


def _endpoint_capture(
    provider: AwsClientProvider,
    binding: AgentCoreRuntimeBinding,
) -> dict[str, object]:
    runtime_id = binding.runtime_arn.rsplit("/", 1)[-1]
    response = _mapping(
        _client(provider, "bedrock-agentcore-control").get_agent_runtime_endpoint(
            agentRuntimeId=runtime_id,
            endpointName=binding.qualifier,
        )
    )
    required_endpoint_fields = {
        "agentRuntimeArn",
        "agentRuntimeEndpointArn",
        "createdAt",
        "id",
        "lastUpdatedAt",
        "liveVersion",
        "name",
        "status",
        "targetVersion",
    }
    if (
        not required_endpoint_fields.issubset(response)
        or set(response)
        - required_endpoint_fields
        - {"ResponseMetadata", "description", "failureReason"}
        or _aware_timestamp(response.get("createdAt"))
        > _aware_timestamp(response.get("lastUpdatedAt"))
        or _ENDPOINT_ID.fullmatch(_string(response.get("id"))) is None
    ):
        raise ValueError
    observation = {
        name: response[name]
        for name in (
            "agentRuntimeArn",
            "agentRuntimeEndpointArn",
            "liveVersion",
            "name",
            "status",
        )
        if name in response
    }
    for optional in ("failureReason", "targetVersion"):
        if optional in response:
            observation[optional] = response[optional]
    verify_phase6_agentcore_endpoint_observation(binding, observation)
    return {
        "arn": binding.endpoint_arn,
        "live_version": binding.runtime_version,
        "name": binding.qualifier,
        "status": "READY",
        "target_version": binding.runtime_version,
    }


def _artifact_capture(
    provider: AwsClientProvider,
    binding: AgentCoreRuntimeBinding,
    *,
    bucket: str,
    key: str,
    version_id: str,
) -> dict[str, object]:
    key_match = _ARCHIVE_KEY.fullmatch(key)
    if (
        binding.release_fingerprint != EXPECTED_CLOSURE_RELEASE_FINGERPRINT
        or key_match is None
        or key_match.group("release") != EXPECTED_CLOSURE_RELEASE_FINGERPRINT
        or key_match.group("archive") != EXPECTED_AGENTCORE_ARCHIVE_SHA256
    ):
        raise ValueError
    archive_sha256 = EXPECTED_AGENTCORE_ARCHIVE_SHA256
    expected_checksum = base64.b64encode(bytes.fromhex(archive_sha256)).decode("ascii")
    response = _mapping(
        _client(provider, "s3").head_object(
            Bucket=bucket,
            Key=key,
            VersionId=version_id,
            ChecksumMode="ENABLED",
            ExpectedBucketOwner=EXPECTED_ACCOUNT_ID,
        )
    )
    size_bytes = response.get("ContentLength")
    expected_metadata = {
        "mr-lister-archive-sha256": archive_sha256,
        "mr-lister-component": "agentcore",
        "mr-lister-release-fingerprint": binding.release_fingerprint,
        "mr-lister-size-bytes": str(size_bytes),
    }
    if (
        type(size_bytes) is not int
        or size_bytes != EXPECTED_AGENTCORE_ARCHIVE_SIZE
        or response.get("VersionId") != version_id
        or response.get("ChecksumSHA256") != expected_checksum
        or response.get("ChecksumType") not in {None, "FULL_OBJECT"}
        or response.get("ServerSideEncryption") != "AES256"
        or response.get("DeleteMarker") is True
        or _mapping(response.get("Metadata")) != expected_metadata
    ):
        raise ValueError
    return {
        "archive_sha256": archive_sha256,
        "bucket": bucket,
        "checksum_sha256_base64": expected_checksum,
        "key": key,
        "size_bytes": size_bytes,
        "version_id": version_id,
    }


def capture_phase66_agentcore_deployment_authority(
    *,
    aws_clients: AwsClientProvider,
    captured_at: datetime | None = None,
) -> dict[str, object]:
    """Capture one fixed, exact AgentCore deployment authority through injected AWS clients."""

    try:
        caller = _mapping(_client(aws_clients, "sts").get_caller_identity())
        if caller.get("Account") != EXPECTED_ACCOUNT_ID or caller.get("Arn") != EXPECTED_CALLER_ARN:
            raise ValueError
        binding, dispatcher_binding_digest = _deployed_binding(aws_clients)
        runtime, bucket, key, version_id = _runtime_capture(aws_clients, binding)
        endpoint = _endpoint_capture(aws_clients, binding)
        artifact = _artifact_capture(
            aws_clients,
            binding,
            bucket=bucket,
            key=key,
            version_id=version_id,
        )
        authority = {
            "account_id": EXPECTED_ACCOUNT_ID,
            "artifact": artifact,
            "deployment_readiness": EXPECTED_READINESS,
            "endpoint": endpoint,
            "environment": EXPECTED_ENVIRONMENT,
            "preparation_dispatch_binding_digest": dispatcher_binding_digest,
            "region": EXPECTED_REGION,
            "release_topology": {
                "agentcore_release_fingerprint": EXPECTED_CLOSURE_RELEASE_FINGERPRINT,
                "artwork_closure_release_fingerprint": EXPECTED_CLOSURE_RELEASE_FINGERPRINT,
                "mode": EXPECTED_RELEASE_TOPOLOGY,
                "preparation_dispatch_override": "EXPLICIT_RESOURCE_LEVEL",
                "preparation_dispatch_release_fingerprint": (EXPECTED_CLOSURE_RELEASE_FINGERPRINT),
                "shared_global_release_fingerprint": EXPECTED_SHARED_RELEASE_FINGERPRINT,
            },
            "runtime": runtime,
            "runtime_binding_fingerprint": binding.binding_fingerprint,
            "stack_name": EXPECTED_STACK_NAME,
            "stack_status": "UPDATE_COMPLETE",
        }
        document = {
            "authority": authority,
            "authority_digest": _digest(authority),
            "captured_at": _timestamp(datetime.now(UTC) if captured_at is None else captured_at),
            "format": FORMAT,
        }
        _validate_document(document)
        return document
    except Phase66AgentCoreDeploymentAuthorityError:
        raise
    except Exception:
        raise Phase66AgentCoreDeploymentAuthorityError(_GENERIC_ERROR) from None


def _validate_document(document: Mapping[str, object]) -> None:
    if set(document) != {"authority", "authority_digest", "captured_at", "format"}:
        raise ValueError
    authority = _mapping(document.get("authority"))
    if (
        document.get("format") != FORMAT
        or document.get("authority_digest") != _digest(authority)
        or _DIGEST.fullmatch(_string(document.get("authority_digest"))) is None
        or re.fullmatch(
            r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z",
            _string(document.get("captured_at")),
        )
        is None
        or set(authority)
        != {
            "account_id",
            "artifact",
            "deployment_readiness",
            "endpoint",
            "environment",
            "preparation_dispatch_binding_digest",
            "region",
            "release_topology",
            "runtime",
            "runtime_binding_fingerprint",
            "stack_name",
            "stack_status",
        }
        or authority.get("account_id") != EXPECTED_ACCOUNT_ID
        or authority.get("deployment_readiness") != EXPECTED_READINESS
        or authority.get("environment") != EXPECTED_ENVIRONMENT
        or authority.get("region") != EXPECTED_REGION
        or authority.get("stack_name") != EXPECTED_STACK_NAME
        or authority.get("stack_status") != "UPDATE_COMPLETE"
    ):
        raise ValueError
    release_topology = _mapping(authority.get("release_topology"))
    if release_topology != {
        "agentcore_release_fingerprint": EXPECTED_CLOSURE_RELEASE_FINGERPRINT,
        "artwork_closure_release_fingerprint": EXPECTED_CLOSURE_RELEASE_FINGERPRINT,
        "mode": EXPECTED_RELEASE_TOPOLOGY,
        "preparation_dispatch_override": "EXPLICIT_RESOURCE_LEVEL",
        "preparation_dispatch_release_fingerprint": EXPECTED_CLOSURE_RELEASE_FINGERPRINT,
        "shared_global_release_fingerprint": EXPECTED_SHARED_RELEASE_FINGERPRINT,
    }:
        raise ValueError
    release = EXPECTED_CLOSURE_RELEASE_FINGERPRINT
    dispatcher_digest = _string(authority.get("preparation_dispatch_binding_digest"))
    binding_fingerprint = _string(authority.get("runtime_binding_fingerprint"))
    if any(
        _DIGEST.fullmatch(value) is None or value == "0" * 64
        for value in (release, binding_fingerprint)
    ):
        raise ValueError
    if _DIGEST.fullmatch(dispatcher_digest) is None:
        raise ValueError
    runtime = _mapping(authority.get("runtime"))
    endpoint = _mapping(authority.get("endpoint"))
    artifact = _mapping(authority.get("artifact"))
    if (
        set(runtime)
        != {"arn", "configuration_digest", "id", "name", "role_arn", "status", "version"}
        or set(endpoint) != {"arn", "live_version", "name", "status", "target_version"}
        or set(artifact)
        != {
            "archive_sha256",
            "bucket",
            "checksum_sha256_base64",
            "key",
            "size_bytes",
            "version_id",
        }
    ):
        raise ValueError
    runtime_arn = _string(runtime.get("arn"))
    runtime_id = _string(runtime.get("id"))
    runtime_version = _string(runtime.get("version"))
    endpoint_arn = _string(endpoint.get("arn"))
    qualifier = _string(endpoint.get("name"))
    reconstructed_binding = load_agentcore_runtime_binding(
        {
            "MR_LISTER_AGENTCORE_RUNTIME_ARN": runtime_arn,
            "MR_LISTER_AGENTCORE_RUNTIME_BINDING_FINGERPRINT": binding_fingerprint,
            "MR_LISTER_AGENTCORE_RUNTIME_ENDPOINT_ARN": endpoint_arn,
            "MR_LISTER_AGENTCORE_RUNTIME_QUALIFIER": qualifier,
            "MR_LISTER_AGENTCORE_RUNTIME_VERSION": runtime_version,
        },
        region=EXPECTED_REGION,
        account_id=EXPECTED_ACCOUNT_ID,
        environment_name=EXPECTED_ENVIRONMENT,
        release_fingerprint=release,
    )
    expected_dispatcher_digest = _digest(
        {
            "MR_LISTER_AGENTCORE_RUNTIME_ARN": reconstructed_binding.runtime_arn,
            "MR_LISTER_AGENTCORE_RUNTIME_BINDING_FINGERPRINT": (
                reconstructed_binding.binding_fingerprint
            ),
            "MR_LISTER_AGENTCORE_RUNTIME_ENDPOINT_ARN": reconstructed_binding.endpoint_arn,
            "MR_LISTER_AGENTCORE_RUNTIME_QUALIFIER": reconstructed_binding.qualifier,
            "MR_LISTER_AGENTCORE_RUNTIME_VERSION": reconstructed_binding.runtime_version,
            "MR_LISTER_RELEASE_FINGERPRINT": reconstructed_binding.release_fingerprint,
        }
    )
    if (
        runtime_arn != EXPECTED_RUNTIME_ARN
        or runtime_id != EXPECTED_RUNTIME_ID
        or runtime_version != EXPECTED_RUNTIME_VERSION
        or runtime.get("name") != EXPECTED_RUNTIME_NAME
        or runtime.get("role_arn") != EXPECTED_RUNTIME_ROLE_ARN
        or runtime.get("configuration_digest") != _digest(_expected_runtime_configuration())
        or runtime.get("status") != "READY"
        or not runtime_arn.endswith(f"/{runtime_id}")
        or endpoint.get("status") != "READY"
        or endpoint.get("live_version") != runtime_version
        or endpoint.get("target_version") != runtime_version
        or endpoint_arn != EXPECTED_ENDPOINT_ARN
        or qualifier != EXPECTED_RUNTIME_QUALIFIER
        or endpoint_arn != f"{runtime_arn}/runtime-endpoint/{qualifier}"
        or qualifier != f"phase6_v{runtime_version}_{EXPECTED_ENVIRONMENT}"
        or dispatcher_digest != expected_dispatcher_digest
        or binding_fingerprint != EXPECTED_RUNTIME_BINDING_FINGERPRINT
        or agentcore_runtime_binding_fingerprint(
            runtime_arn=runtime_arn,
            endpoint_arn=endpoint_arn,
            qualifier=qualifier,
            runtime_version=runtime_version,
            release_fingerprint=release,
        )
        != binding_fingerprint
    ):
        raise ValueError
    archive_sha256 = _string(artifact.get("archive_sha256"))
    bucket = _string(artifact.get("bucket"))
    key = _string(artifact.get("key"))
    version_id = _string(artifact.get("version_id"))
    size_bytes = artifact.get("size_bytes")
    validate_phase6_s3_version_id(version_id)
    expected_bucket = (
        f"mr-lister-phase6-artifacts-{EXPECTED_ENVIRONMENT}-{EXPECTED_ACCOUNT_ID}-{EXPECTED_REGION}"
    )
    expected_key = (
        f"private/deployments/agentcore/releases/{release}/phase6-agentcore-{archive_sha256}.zip"
    )
    expected_checksum = base64.b64encode(bytes.fromhex(archive_sha256)).decode("ascii")
    if (
        archive_sha256 != EXPECTED_AGENTCORE_ARCHIVE_SHA256
        or bucket != expected_bucket
        or key != expected_key
        or artifact.get("checksum_sha256_base64") != expected_checksum
        or type(size_bytes) is not int
        or size_bytes != EXPECTED_AGENTCORE_ARCHIVE_SIZE
    ):
        raise ValueError


def _private_path(path: Path) -> Path:
    candidate = Path(os.path.abspath(path))
    try:
        relative = candidate.relative_to(PRIVATE_OUTPUT_ROOT)
    except ValueError:
        raise ValueError from None
    if not relative.parts:
        raise ValueError
    return candidate


def _open_repository_root() -> int:
    flags = os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY | os.O_NOFOLLOW
    root = Path(os.path.abspath(REPOSITORY_ROOT))
    descriptor: int | None = None
    try:
        descriptor = os.open(os.sep, flags)
        for component in root.parts[1:]:
            next_descriptor = os.open(component, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = next_descriptor
        if not stat.S_ISDIR(os.fstat(descriptor).st_mode):
            raise OSError
        result = descriptor
        descriptor = None
        return result
    finally:
        if descriptor is not None:
            os.close(descriptor)


@contextmanager
def _private_directory_descriptor(path: Path, *, create: bool) -> Iterator[int]:
    directory = Path(os.path.abspath(path))
    directory.relative_to(PRIVATE_OUTPUT_ROOT)
    flags = os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY | os.O_NOFOLLOW
    descriptor: int | None = _open_repository_root()
    try:
        for component in directory.relative_to(REPOSITORY_ROOT).parts:
            next_descriptor: int | None = None
            try:
                try:
                    next_descriptor = os.open(component, flags, dir_fd=descriptor)
                except FileNotFoundError:
                    if not create:
                        raise
                    os.mkdir(component, mode=0o700, dir_fd=descriptor)
                    next_descriptor = os.open(component, flags, dir_fd=descriptor)
                metadata = os.fstat(next_descriptor)
                if not stat.S_ISDIR(metadata.st_mode) or metadata.st_mode & 0o077:
                    raise OSError
            except OSError:
                if next_descriptor is not None:
                    os.close(next_descriptor)
                raise
            os.close(descriptor)
            descriptor = next_descriptor
        yield descriptor
    finally:
        if descriptor is not None:
            os.close(descriptor)


def write_phase66_agentcore_deployment_authority(
    path: Path,
    document: Mapping[str, object],
) -> Path:
    """Create one canonical owner-only authority file in the private acceptance root."""

    try:
        _validate_document(document)
        candidate = _private_path(path)
        if candidate.name in {"", ".", ".."} or "/" in candidate.name or "\x00" in candidate.name:
            raise ValueError
        payload = _canonical(document, pretty=True)
        temporary = f".{candidate.name}.{secrets.token_hex(12)}.tmp"
        with _private_directory_descriptor(candidate.parent, create=True) as directory_descriptor:
            directory_identity = os.fstat(directory_descriptor)
            descriptor: int | None = None
            linked = False
            try:
                descriptor = os.open(
                    temporary,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
                    0o600,
                    dir_fd=directory_descriptor,
                )
                with os.fdopen(descriptor, "wb", closefd=True) as output:
                    descriptor = None
                    output.write(payload)
                    output.flush()
                    os.fsync(output.fileno())
                os.link(
                    temporary,
                    candidate.name,
                    src_dir_fd=directory_descriptor,
                    dst_dir_fd=directory_descriptor,
                    follow_symlinks=False,
                )
                linked = True
                os.unlink(temporary, dir_fd=directory_descriptor)
                os.fsync(directory_descriptor)
                with _private_directory_descriptor(
                    candidate.parent, create=False
                ) as verification_descriptor:
                    verified_identity = os.fstat(verification_descriptor)
                    if (directory_identity.st_dev, directory_identity.st_ino) != (
                        verified_identity.st_dev,
                        verified_identity.st_ino,
                    ):
                        raise OSError
            except Exception:
                if linked:
                    try:
                        os.unlink(candidate.name, dir_fd=directory_descriptor)
                        os.fsync(directory_descriptor)
                    except OSError:
                        pass
                raise
            finally:
                if descriptor is not None:
                    os.close(descriptor)
                try:
                    os.unlink(temporary, dir_fd=directory_descriptor)
                except FileNotFoundError:
                    pass
        return candidate
    except Phase66AgentCoreDeploymentAuthorityError:
        raise
    except Exception:
        raise Phase66AgentCoreDeploymentAuthorityError(_GENERIC_ERROR) from None


class _Boto3Provider:
    def __init__(self, profile: str) -> None:
        import boto3

        self._session = boto3.Session(profile_name=profile, region_name=EXPECTED_REGION)

    def client(self, service_name: str) -> Any:
        return self._session.client(service_name)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", required=True, choices=(EXPECTED_PROFILE,))
    parser.add_argument("--output", required=True, type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        document = capture_phase66_agentcore_deployment_authority(
            aws_clients=_Boto3Provider(arguments.profile)
        )
        output = write_phase66_agentcore_deployment_authority(arguments.output, document)
    except Exception:
        print(_GENERIC_ERROR)
        return 2
    print(
        json.dumps(
            {
                "authority_digest": document["authority_digest"],
                "result": "passed",
                "target_sha256": sha256(output.read_bytes()).hexdigest(),
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
