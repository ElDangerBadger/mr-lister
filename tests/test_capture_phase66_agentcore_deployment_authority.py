from __future__ import annotations

import base64
import inspect
import json
import stat
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

import tools.capture_phase66_agentcore_deployment_authority as capture
from mr_lister.agent.runtime_binding import agentcore_runtime_binding_fingerprint

SHARED_RELEASE = capture.EXPECTED_SHARED_RELEASE_FINGERPRINT
RELEASE = capture.EXPECTED_CLOSURE_RELEASE_FINGERPRINT
ARCHIVE_SHA256 = capture.EXPECTED_AGENTCORE_ARCHIVE_SHA256
ARCHIVE_SIZE = capture.EXPECTED_AGENTCORE_ARCHIVE_SIZE
VERSION_ID = "B64_bDuTGgc2a4K1PrLNWdSBqOeJpOo6"
RUNTIME_VERSION = capture.EXPECTED_RUNTIME_VERSION
RUNTIME_ID = capture.EXPECTED_RUNTIME_ID
RUNTIME_ARN = capture.EXPECTED_RUNTIME_ARN
QUALIFIER = capture.EXPECTED_RUNTIME_QUALIFIER
ENDPOINT_ARN = capture.EXPECTED_ENDPOINT_ARN
BUCKET = f"mr-lister-phase6-artifacts-dev-{capture.EXPECTED_ACCOUNT_ID}-{capture.EXPECTED_REGION}"
KEY = f"private/deployments/agentcore/releases/{RELEASE}/phase6-agentcore-{ARCHIVE_SHA256}.zip"
CHECKSUM = base64.b64encode(bytes.fromhex(ARCHIVE_SHA256)).decode("ascii")
BINDING_FINGERPRINT = agentcore_runtime_binding_fingerprint(
    runtime_arn=RUNTIME_ARN,
    endpoint_arn=ENDPOINT_ARN,
    qualifier=QUALIFIER,
    runtime_version=RUNTIME_VERSION,
    release_fingerprint=RELEASE,
)
assert BINDING_FINGERPRINT == capture.EXPECTED_RUNTIME_BINDING_FINGERPRINT
NOW = datetime(2026, 9, 1, 4, 30, tzinfo=UTC)


def _parameters() -> list[dict[str, str]]:
    return [
        {"ParameterKey": "EnvironmentName", "ParameterValue": "dev"},
        {"ParameterKey": "ReleaseFingerprint", "ParameterValue": SHARED_RELEASE},
        {"ParameterKey": "AgentCoreRuntimeArn", "ParameterValue": RUNTIME_ARN},
        {
            "ParameterKey": "AgentCoreRuntimeBindingFingerprint",
            "ParameterValue": BINDING_FINGERPRINT,
        },
        {"ParameterKey": "AgentCoreRuntimeEndpointArn", "ParameterValue": ENDPOINT_ARN},
        {"ParameterKey": "AgentCoreRuntimeQualifier", "ParameterValue": QUALIFIER},
        {"ParameterKey": "AgentCoreRuntimeVersion", "ParameterValue": RUNTIME_VERSION},
        {"ParameterKey": "UnrelatedSecretArn", "ParameterValue": "not-retained"},
    ]


def _binding_variables() -> dict[str, str]:
    return {
        "MR_LISTER_AGENTCORE_RUNTIME_ARN": RUNTIME_ARN,
        "MR_LISTER_AGENTCORE_RUNTIME_BINDING_FINGERPRINT": BINDING_FINGERPRINT,
        "MR_LISTER_AGENTCORE_RUNTIME_ENDPOINT_ARN": ENDPOINT_ARN,
        "MR_LISTER_AGENTCORE_RUNTIME_QUALIFIER": QUALIFIER,
        "MR_LISTER_AGENTCORE_RUNTIME_VERSION": RUNTIME_VERSION,
        "MR_LISTER_RELEASE_FINGERPRINT": RELEASE,
    }


class _Sts:
    def __init__(self) -> None:
        self.account = capture.EXPECTED_ACCOUNT_ID
        self.arn = capture.EXPECTED_CALLER_ARN

    def get_caller_identity(self) -> dict[str, str]:
        return {"Account": self.account, "Arn": self.arn, "UserId": "not-retained"}


class _CloudFormation:
    def __init__(self) -> None:
        self.stack = {
            "EnableTerminationProtection": True,
            "Outputs": [
                {
                    "OutputKey": "DeploymentReadiness",
                    "OutputValue": capture.EXPECTED_READINESS,
                },
                {"OutputKey": "PrivateOutput", "OutputValue": "not-retained"},
            ],
            "Parameters": _parameters(),
            "StackName": capture.EXPECTED_STACK_NAME,
            "StackStatus": "UPDATE_COMPLETE",
        }
        self.template = {
            "Globals": {
                "Function": {
                    "Environment": {
                        "Variables": {
                            "MR_LISTER_RELEASE_FINGERPRINT": {"Ref": "ReleaseFingerprint"}
                        }
                    }
                }
            },
            "Parameters": {
                "ReleaseFingerprint": {
                    "AllowedValues": [SHARED_RELEASE],
                    "Default": SHARED_RELEASE,
                }
            },
            "Resources": {
                capture.EXPECTED_PREPARATION_LOGICAL_ID: {
                    "Properties": {
                        "Environment": {"Variables": {"MR_LISTER_RELEASE_FINGERPRINT": RELEASE}}
                    },
                    "Type": "AWS::Serverless::Function",
                }
            },
        }
        self.describe_calls: list[dict[str, object]] = []
        self.resource_calls: list[dict[str, object]] = []
        self.template_calls: list[dict[str, object]] = []

    def describe_stacks(self, **kwargs: object) -> dict[str, object]:
        self.describe_calls.append(kwargs)
        return {"Stacks": [self.stack]}

    def describe_stack_resource(self, **kwargs: object) -> dict[str, object]:
        self.resource_calls.append(kwargs)
        return {
            "StackResourceDetail": {
                "LogicalResourceId": capture.EXPECTED_PREPARATION_LOGICAL_ID,
                "PhysicalResourceId": "private-preparation-function-name",
                "ResourceStatus": "UPDATE_COMPLETE",
                "ResourceType": "AWS::Lambda::Function",
            }
        }

    def get_template(self, **kwargs: object) -> dict[str, object]:
        self.template_calls.append(kwargs)
        return {"TemplateBody": self.template}


class _Lambda:
    def __init__(self) -> None:
        self.variables = _binding_variables() | {"PRIVATE_SECRET_ARN": "not-retained"}
        self.calls: list[dict[str, object]] = []

    def get_function_configuration(self, **kwargs: object) -> dict[str, object]:
        self.calls.append(kwargs)
        return {
            "Environment": {"Variables": self.variables},
            "LastUpdateStatus": "Successful",
            "State": "Active",
        }


class _AgentCore:
    def __init__(self) -> None:
        self.runtime = {
            "agentRuntimeArn": RUNTIME_ARN,
            "agentRuntimeArtifact": {
                "codeConfiguration": {
                    "code": {"s3": {"bucket": BUCKET, "prefix": KEY, "versionId": VERSION_ID}},
                    "entryPoint": ["main.py"],
                    "runtime": "PYTHON_3_12",
                }
            },
            "agentRuntimeId": RUNTIME_ID,
            "agentRuntimeName": "mr_lister_phase6",
            "agentRuntimeVersion": RUNTIME_VERSION,
            "authorizerConfiguration": None,
            "capacityProviderConfiguration": None,
            "createdAt": NOW - timedelta(minutes=2),
            "description": capture.EXPECTED_RUNTIME_DESCRIPTION,
            "environmentVariables": capture._expected_runtime_environment(),
            "failureReason": None,
            "filesystemConfigurations": [],
            "lifecycleConfiguration": {
                "idleRuntimeSessionTimeout": 900,
                "maxLifetime": 3600,
            },
            "lastUpdatedAt": NOW - timedelta(minutes=1),
            "metadataConfiguration": {"requireMMDSV2": True},
            "networkConfiguration": {"networkMode": "PUBLIC"},
            "protocolConfiguration": {"serverProtocol": "HTTP"},
            "requestHeaderConfiguration": {"requestHeaderAllowlist": []},
            "roleArn": capture.EXPECTED_RUNTIME_ROLE_ARN,
            "status": "READY",
            "workloadIdentityDetails": {
                "workloadIdentityArn": (
                    f"arn:aws:bedrock-agentcore:{capture.EXPECTED_REGION}:"
                    f"{capture.EXPECTED_ACCOUNT_ID}:workload-identity/not-retained"
                )
            },
        }
        self.endpoint = {
            "agentRuntimeArn": RUNTIME_ARN,
            "agentRuntimeEndpointArn": ENDPOINT_ARN,
            "createdAt": NOW - timedelta(minutes=1),
            "failureReason": None,
            "id": "private-endpoint-id",
            "lastUpdatedAt": NOW,
            "liveVersion": RUNTIME_VERSION,
            "name": QUALIFIER,
            "status": "READY",
            "targetVersion": RUNTIME_VERSION,
        }
        self.runtime_calls: list[dict[str, object]] = []
        self.endpoint_calls: list[dict[str, object]] = []

    def get_agent_runtime(self, **kwargs: object) -> dict[str, object]:
        self.runtime_calls.append(kwargs)
        return self.runtime

    def get_agent_runtime_endpoint(self, **kwargs: object) -> dict[str, object]:
        self.endpoint_calls.append(kwargs)
        return self.endpoint


class _S3:
    def __init__(self) -> None:
        self.response = {
            "ChecksumSHA256": CHECKSUM,
            "ChecksumType": "FULL_OBJECT",
            "ContentLength": ARCHIVE_SIZE,
            "Metadata": {
                "mr-lister-archive-sha256": ARCHIVE_SHA256,
                "mr-lister-component": "agentcore",
                "mr-lister-release-fingerprint": RELEASE,
                "mr-lister-size-bytes": str(ARCHIVE_SIZE),
            },
            "ServerSideEncryption": "AES256",
            "VersionId": VERSION_ID,
        }
        self.calls: list[dict[str, object]] = []

    def head_object(self, **kwargs: object) -> dict[str, object]:
        self.calls.append(kwargs)
        return self.response


class _Provider:
    def __init__(self) -> None:
        self.sts = _Sts()
        self.cloudformation = _CloudFormation()
        self.lambda_client = _Lambda()
        self.agentcore = _AgentCore()
        self.s3 = _S3()
        self.clients = {
            "bedrock-agentcore-control": self.agentcore,
            "cloudformation": self.cloudformation,
            "lambda": self.lambda_client,
            "s3": self.s3,
            "sts": self.sts,
        }

    def client(self, service_name: str) -> Any:
        return self.clients[service_name]


def _capture(provider: _Provider | None = None, *, at: datetime = NOW) -> dict[str, object]:
    return capture.capture_phase66_agentcore_deployment_authority(
        aws_clients=_Provider() if provider is None else provider,
        captured_at=at,
    )


def test_capture_binds_exact_bounded_mosaic_runtime_endpoint_and_s3_object() -> None:
    provider = _Provider()

    document = _capture(provider)

    authority = document["authority"]
    assert document == {
        "authority": authority,
        "authority_digest": capture._digest(authority),
        "captured_at": "2026-09-01T04:30:00Z",
        "format": capture.FORMAT,
    }
    assert authority["release_topology"] == {
        "agentcore_release_fingerprint": RELEASE,
        "artwork_closure_release_fingerprint": RELEASE,
        "mode": "BOUNDED_ARTWORK_CLOSURE_MOSAIC",
        "preparation_dispatch_override": "EXPLICIT_RESOURCE_LEVEL",
        "preparation_dispatch_release_fingerprint": RELEASE,
        "shared_global_release_fingerprint": SHARED_RELEASE,
    }
    assert authority["runtime_binding_fingerprint"] == BINDING_FINGERPRINT
    assert authority["runtime"] == {
        "arn": RUNTIME_ARN,
        "configuration_digest": capture._digest(capture._expected_runtime_configuration()),
        "id": RUNTIME_ID,
        "name": "mr_lister_phase6",
        "role_arn": capture.EXPECTED_RUNTIME_ROLE_ARN,
        "status": "READY",
        "version": RUNTIME_VERSION,
    }
    assert authority["endpoint"] == {
        "arn": ENDPOINT_ARN,
        "live_version": RUNTIME_VERSION,
        "name": QUALIFIER,
        "status": "READY",
        "target_version": RUNTIME_VERSION,
    }
    assert authority["artifact"] == {
        "archive_sha256": ARCHIVE_SHA256,
        "bucket": BUCKET,
        "checksum_sha256_base64": CHECKSUM,
        "key": KEY,
        "size_bytes": ARCHIVE_SIZE,
        "version_id": VERSION_ID,
    }
    assert provider.cloudformation.describe_calls == [{"StackName": capture.EXPECTED_STACK_NAME}]
    assert provider.cloudformation.resource_calls == [
        {
            "LogicalResourceId": capture.EXPECTED_PREPARATION_LOGICAL_ID,
            "StackName": capture.EXPECTED_STACK_NAME,
        }
    ]
    assert provider.cloudformation.template_calls == [
        {"StackName": capture.EXPECTED_STACK_NAME, "TemplateStage": "Original"}
    ]
    assert provider.lambda_client.calls == [{"FunctionName": "private-preparation-function-name"}]
    assert provider.agentcore.runtime_calls == [
        {"agentRuntimeId": RUNTIME_ID, "agentRuntimeVersion": RUNTIME_VERSION}
    ]
    assert provider.agentcore.endpoint_calls == [
        {"agentRuntimeId": RUNTIME_ID, "endpointName": QUALIFIER}
    ]
    assert provider.s3.calls == [
        {
            "Bucket": BUCKET,
            "ChecksumMode": "ENABLED",
            "ExpectedBucketOwner": capture.EXPECTED_ACCOUNT_ID,
            "Key": KEY,
            "VersionId": VERSION_ID,
        }
    ]
    rendered = json.dumps(document, sort_keys=True)
    assert "not-retained" not in rendered
    assert "workloadIdentityArn" not in rendered
    assert "PRIVATE_SECRET_ARN" not in rendered


def test_authority_digest_is_stable_across_capture_times() -> None:
    first = _capture(at=NOW)
    second = _capture(at=NOW + timedelta(minutes=5))

    assert first["captured_at"] != second["captured_at"]
    assert first["authority"] == second["authority"]
    assert first["authority_digest"] == second["authority_digest"]


@pytest.mark.parametrize(
    "failure",
    (
        "caller",
        "stack",
        "stack_release",
        "missing_override",
        "dispatcher",
        "dispatcher_release",
        "runtime_status",
        "runtime_release",
        "runtime_role",
        "runtime_configuration",
        "runtime_artifact",
        "runtime_archive",
        "endpoint_status",
        "endpoint_version",
        "object_version",
        "object_checksum",
        "object_size",
        "object_metadata",
    ),
)
def test_capture_fails_closed_on_every_drifted_authority(failure: str) -> None:
    provider = _Provider()
    if failure == "caller":
        provider.sts.arn = f"arn:aws:iam::{capture.EXPECTED_ACCOUNT_ID}:root"
    elif failure == "stack":
        provider.cloudformation.stack["StackStatus"] = "UPDATE_ROLLBACK_COMPLETE"
    elif failure == "stack_release":
        provider.cloudformation.stack["Parameters"][1]["ParameterValue"] = "c" * 64
    elif failure == "missing_override":
        del provider.cloudformation.template["Resources"][capture.EXPECTED_PREPARATION_LOGICAL_ID][
            "Properties"
        ]["Environment"]["Variables"]["MR_LISTER_RELEASE_FINGERPRINT"]
    elif failure == "dispatcher":
        provider.lambda_client.variables["MR_LISTER_AGENTCORE_RUNTIME_VERSION"] = "2"
    elif failure == "dispatcher_release":
        provider.lambda_client.variables["MR_LISTER_RELEASE_FINGERPRINT"] = SHARED_RELEASE
    elif failure == "runtime_status":
        provider.agentcore.runtime["status"] = "UPDATING"
    elif failure == "runtime_release":
        provider.agentcore.runtime["environmentVariables"]["MR_LISTER_RELEASE_FINGERPRINT"] = (
            "c" * 64
        )
    elif failure == "runtime_role":
        provider.agentcore.runtime["roleArn"] = (
            f"arn:aws:iam::{capture.EXPECTED_ACCOUNT_ID}:role/unreviewed"
        )
    elif failure == "runtime_configuration":
        provider.agentcore.runtime["networkConfiguration"] = {"networkMode": "VPC"}
    elif failure == "runtime_artifact":
        provider.agentcore.runtime["agentRuntimeArtifact"]["codeConfiguration"]["code"]["s3"][
            "versionId"
        ] = "null"
    elif failure == "runtime_archive":
        provider.agentcore.runtime["agentRuntimeArtifact"]["codeConfiguration"]["code"]["s3"][
            "prefix"
        ] = KEY.replace(ARCHIVE_SHA256, "c" * 64)
    elif failure == "endpoint_status":
        provider.agentcore.endpoint["status"] = "UPDATING"
        provider.agentcore.endpoint["failureReason"] = "private endpoint failure"
    elif failure == "endpoint_version":
        provider.agentcore.endpoint["liveVersion"] = "2"
    elif failure == "object_version":
        provider.s3.response["VersionId"] = "another-exact-version"
    elif failure == "object_checksum":
        provider.s3.response["ChecksumSHA256"] = base64.b64encode(b"c" * 32).decode()
    elif failure == "object_size":
        provider.s3.response["ContentLength"] = ARCHIVE_SIZE + 1
    else:
        provider.s3.response["Metadata"]["mr-lister-release-fingerprint"] = "c" * 64

    with pytest.raises(capture.Phase66AgentCoreDeploymentAuthorityError) as captured:
        _capture(provider)
    assert str(captured.value) == capture._GENERIC_ERROR
    assert "private endpoint failure" not in str(captured.value)


@pytest.mark.parametrize("old_version", ("1", "2"))
def test_capture_rejects_coherently_bound_preclosure_runtime_versions(old_version: str) -> None:
    provider = _Provider()
    qualifier = f"phase6_v{old_version}_dev"
    endpoint_arn = f"{RUNTIME_ARN}/runtime-endpoint/{qualifier}"
    binding = agentcore_runtime_binding_fingerprint(
        runtime_arn=RUNTIME_ARN,
        endpoint_arn=endpoint_arn,
        qualifier=qualifier,
        runtime_version=old_version,
        release_fingerprint=RELEASE,
    )
    replacements = {
        "AgentCoreRuntimeBindingFingerprint": binding,
        "AgentCoreRuntimeEndpointArn": endpoint_arn,
        "AgentCoreRuntimeQualifier": qualifier,
        "AgentCoreRuntimeVersion": old_version,
    }
    for parameter in provider.cloudformation.stack["Parameters"]:
        name = parameter["ParameterKey"]
        if name in replacements:
            parameter["ParameterValue"] = replacements[name]
    provider.lambda_client.variables.update(
        {
            "MR_LISTER_AGENTCORE_RUNTIME_BINDING_FINGERPRINT": binding,
            "MR_LISTER_AGENTCORE_RUNTIME_ENDPOINT_ARN": endpoint_arn,
            "MR_LISTER_AGENTCORE_RUNTIME_QUALIFIER": qualifier,
            "MR_LISTER_AGENTCORE_RUNTIME_VERSION": old_version,
        }
    )
    provider.agentcore.runtime["agentRuntimeVersion"] = old_version
    provider.agentcore.endpoint.update(
        {
            "agentRuntimeEndpointArn": endpoint_arn,
            "liveVersion": old_version,
            "name": qualifier,
            "targetVersion": old_version,
        }
    )

    with pytest.raises(capture.Phase66AgentCoreDeploymentAuthorityError):
        _capture(provider)


def test_checked_target_cannot_be_replaced_by_capture_arguments() -> None:
    signature = inspect.signature(capture.capture_phase66_agentcore_deployment_authority)
    assert set(signature.parameters) == {"aws_clients", "captured_at"}
    parser = capture._parser()
    options = {option for action in parser._actions for option in action.option_strings}
    assert options == {"-h", "--help", "--output", "--profile"}
    profile_action = next(
        action for action in parser._actions if "--profile" in action.option_strings
    )
    assert profile_action.choices == (capture.EXPECTED_PROFILE,)


def _private_workspace(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    repository = tmp_path / "repository"
    repository.mkdir()
    private = repository / ".mr_lister_private" / "phase66-acceptance"
    monkeypatch.setattr(capture, "REPOSITORY_ROOT", repository)
    monkeypatch.setattr(capture, "PRIVATE_OUTPUT_ROOT", private)
    return private


def test_private_write_is_canonical_create_only_and_owner_confined(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    private = _private_workspace(monkeypatch, tmp_path)
    output = private / "run" / "agentcore-deployment-authority.json"
    document = _capture()

    assert capture.write_phase66_agentcore_deployment_authority(output, document) == output
    assert output.read_bytes() == capture._canonical(document, pretty=True)
    assert stat.S_IMODE(output.stat().st_mode) == 0o600
    assert stat.S_IMODE(output.parent.stat().st_mode) == 0o700
    with pytest.raises(capture.Phase66AgentCoreDeploymentAuthorityError):
        capture.write_phase66_agentcore_deployment_authority(output, document)
    with pytest.raises(capture.Phase66AgentCoreDeploymentAuthorityError):
        capture.write_phase66_agentcore_deployment_authority(
            tmp_path / "outside.json",
            document,
        )


def test_private_write_rejects_symlink_parent_and_semantic_drift(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    private = _private_workspace(monkeypatch, tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (private.parent).symlink_to(outside, target_is_directory=True)
    output = private / "agentcore-deployment-authority.json"

    with pytest.raises(capture.Phase66AgentCoreDeploymentAuthorityError):
        capture.write_phase66_agentcore_deployment_authority(output, _capture())
    assert not (outside / "phase66-acceptance/agentcore-deployment-authority.json").exists()

    private.parent.unlink()
    drifted = deepcopy(_capture())
    drifted["authority"]["endpoint"]["status"] = "UPDATING"
    drifted["authority_digest"] = capture._digest(drifted["authority"])
    with pytest.raises(capture.Phase66AgentCoreDeploymentAuthorityError):
        capture.write_phase66_agentcore_deployment_authority(output, drifted)

    drifted = deepcopy(_capture())
    drifted["authority"]["release_topology"]["shared_global_release_fingerprint"] = RELEASE
    drifted["authority_digest"] = capture._digest(drifted["authority"])
    with pytest.raises(capture.Phase66AgentCoreDeploymentAuthorityError):
        capture.write_phase66_agentcore_deployment_authority(output, drifted)
