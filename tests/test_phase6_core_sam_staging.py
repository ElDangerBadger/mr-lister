from __future__ import annotations

import json
import shutil
import subprocess
from base64 import b64encode
from dataclasses import replace
from hashlib import sha256
from pathlib import Path
from unittest.mock import patch

import pytest

import tools.render_phase6_agentcore_direct_codezip as direct
from mr_lister.agent.runtime_binding import agentcore_runtime_binding_fingerprint
from mr_lister.release.phase6 import LINUX_ARM64_TARGET, render_manifest
from tools.build_phase66_source_bundles import (
    AGENTCORE_ARCHIVE_FILENAME,
    DEPLOYMENT_DESCRIPTOR_FILENAME,
    LAMBDA_ARCHIVE_FILENAME,
)
from tools.render_phase6_core_sam_staging import (
    CORE_STAGED_TEMPLATE_OUTPUT,
    SOURCE_TEMPLATE,
    Phase6CoreSamStagingBinding,
    Phase6CoreSamStagingError,
    reject_phase6_core_sam_activation,
    render_phase6_core_sam_staged_template,
    verify_core_runtime_dependency_closure,
    verify_phase6_core_sam_staged_inertness,
    verify_rendered_phase6_core_sam_staged_template,
    write_phase6_core_sam_staged_template,
)
from tools.verify_phase6_s3_release_object import (
    Phase6S3ReleaseObjectExpectation,
    VerifiedPhase6S3ReleaseObject,
)

ROOT = Path(__file__).parents[1]
ACCOUNT = "123456789012"
REGION = "us-west-2"
ENVIRONMENT = "dev"
STACK_ID = (
    f"arn:aws:cloudformation:{REGION}:{ACCOUNT}:stack/mr-lister-phase6-dev/"
    "11111111-2222-3333-4444-555555555555"
)
RELEASE = "a" * 64
RUNTIME_VERSION = "1"
RUNTIME_QUALIFIER = "phase6_v1_dev"
RUNTIME_ARN = f"arn:aws:bedrock-agentcore:{REGION}:{ACCOUNT}:runtime/mr_lister_phase6-Ab12Cd34Ef"
RUNTIME_ID = RUNTIME_ARN.rsplit("/", 1)[1]
ENDPOINT_ARN = f"{RUNTIME_ARN}/runtime-endpoint/{RUNTIME_QUALIFIER}"
LAMBDA_BYTES = b"synthetic exact sealed core lambda archive"
AGENTCORE_BYTES = b"synthetic exact sealed agentcore archive"
LAMBDA_SHA = sha256(LAMBDA_BYTES).hexdigest()
AGENTCORE_SHA = sha256(AGENTCORE_BYTES).hexdigest()
BUCKET = f"mr-lister-phase6-artifacts-{ENVIRONMENT}-{ACCOUNT}-{REGION}"
LAMBDA_EXPECTATION = Phase6S3ReleaseObjectExpectation(
    account_id=ACCOUNT,
    region=REGION,
    environment=ENVIRONMENT,
    component="lambda",
    release_fingerprint=RELEASE,
    archive_sha256=LAMBDA_SHA,
    size_bytes=len(LAMBDA_BYTES),
)
KEY = LAMBDA_EXPECTATION.key
VERSION_ID = "3LgT7f_ExactVersion+AbCd/012="
AGENTCORE_VERSION_ID = "3AgentCore_ExactVersion+AbCd/012="
AGENTCORE_EXPECTATION = Phase6S3ReleaseObjectExpectation(
    account_id=ACCOUNT,
    region=REGION,
    environment=ENVIRONMENT,
    component="agentcore",
    release_fingerprint=RELEASE,
    archive_sha256=AGENTCORE_SHA,
    size_bytes=len(AGENTCORE_BYTES),
)
TEST_RUNTIME_ENVIRONMENT = {"MR_LISTER_RELEASE_FINGERPRINT": RELEASE}

FUNCTION_HANDLERS = {
    "DispatcherFunction": "phase6_lambda.dispatcher_handler",
    "PreparationDispatchFunction": "phase6_lambda.preparation_dispatch_handler",
    "ProviderDraftFunction": "phase6_lambda.provider_draft_handler",
    "SettlementFunction": "phase6_lambda.settlement_handler",
    "SourceVersionRetentionFunction": "phase6_lambda.source_version_retention_handler",
    "StuckExecutionRecoveryFunction": "phase6_lambda.stuck_execution_recovery_handler",
    "TerminalOperationalCleanupFunction": "phase6_lambda.terminal_operational_cleanup_handler",
}
MAINTENANCE_FUNCTIONS = frozenset(
    {
        "SourceVersionRetentionFunction",
        "StuckExecutionRecoveryFunction",
        "TerminalOperationalCleanupFunction",
    }
)

RESOURCE_TYPES = {
    "DispatcherFunction": "AWS::Serverless::Function",
    "DispatcherFunctionRole": "AWS::IAM::Role",
    "DispatcherLogGroup": "AWS::Logs::LogGroup",
    "OperationalStateTable": "AWS::DynamoDB::Table",
    "PreparationDispatchFunction": "AWS::Serverless::Function",
    "PreparationDispatchFunctionRole": "AWS::IAM::Role",
    "PreparationDispatchLogGroup": "AWS::Logs::LogGroup",
    "PrepareStateMachine": "AWS::Serverless::StateMachine",
    "PrepareStateMachineRole": "AWS::IAM::Role",
    "PrepareWorkflowLogGroup": "AWS::Logs::LogGroup",
    "PrivateArtifactBucket": "AWS::S3::Bucket",
    "PrivateArtifactBucketPolicy": "AWS::S3::BucketPolicy",
    "ProviderDraftFunction": "AWS::Serverless::Function",
    "ProviderDraftFunctionRole": "AWS::IAM::Role",
    "ProviderDraftLogGroup": "AWS::Logs::LogGroup",
    "ReconcileProductStateMachine": "AWS::Serverless::StateMachine",
    "ReconcileProductStateMachineRole": "AWS::IAM::Role",
    "ReconcileProductWorkflowLogGroup": "AWS::Logs::LogGroup",
    "RefreshEconomicsStateMachine": "AWS::Serverless::StateMachine",
    "RefreshEconomicsStateMachineRole": "AWS::IAM::Role",
    "RefreshEconomicsWorkflowLogGroup": "AWS::Logs::LogGroup",
    "SettlementFunction": "AWS::Serverless::Function",
    "SettlementFunctionRole": "AWS::IAM::Role",
    "SettlementLogGroup": "AWS::Logs::LogGroup",
    "SourceVersionRetentionFunction": "AWS::Serverless::Function",
    "SourceVersionRetentionFunctionRole": "AWS::IAM::Role",
    "SourceVersionRetentionLogGroup": "AWS::Logs::LogGroup",
    "StuckExecutionRecoveryDeadLetterQueue": "AWS::SQS::Queue",
    "StuckExecutionRecoveryDeadLetterQueuePolicy": "AWS::SQS::QueuePolicy",
    "StuckExecutionRecoveryFunction": "AWS::Serverless::Function",
    "StuckExecutionRecoveryFunctionRole": "AWS::IAM::Role",
    "StuckExecutionRecoveryLogGroup": "AWS::Logs::LogGroup",
    "StuckExecutionRecoverySchedulePermission": "AWS::Lambda::Permission",
    "StuckExecutionRecoveryScheduleRule": "AWS::Events::Rule",
    "SynchronizeProductStateMachine": "AWS::Serverless::StateMachine",
    "SynchronizeProductStateMachineRole": "AWS::IAM::Role",
    "SynchronizeProductWorkflowLogGroup": "AWS::Logs::LogGroup",
    "TerminalOperationalCleanupFunction": "AWS::Serverless::Function",
    "TerminalOperationalCleanupFunctionRole": "AWS::IAM::Role",
    "TerminalOperationalCleanupLogGroup": "AWS::Logs::LogGroup",
}

STATE_MACHINES = {
    "PrepareStateMachine": Path("infra/phase6/statemachine/prepare.asl.json"),
    "ReconcileProductStateMachine": Path("infra/phase6/statemachine/reconcile-product.asl.json"),
    "RefreshEconomicsStateMachine": Path("infra/phase6/statemachine/refresh-economics.asl.json"),
    "SynchronizeProductStateMachine": Path(
        "infra/phase6/statemachine/synchronize-product.asl.json"
    ),
}

DISABLED_TRIGGER_METADATA = {
    "DispatcherFunction.Events.DueWorkSweep": {"Enabled": False, "Type": "Schedule"},
    "DispatcherFunction.Events.OperationalStateChanges": {
        "Enabled": False,
        "Type": "DynamoDB",
    },
    "SourceVersionRetentionFunction.Events.SourceVersionRetentionSweep": {
        "Enabled": False,
        "Type": "Schedule",
    },
    "StuckExecutionRecoveryScheduleRule": {
        "State": "DISABLED",
        "Type": "AWS::Events::Rule",
    },
    "TerminalOperationalCleanupFunction.Events.TerminalOperationalCleanupSweep": {
        "Enabled": False,
        "Type": "Schedule",
    },
}

TRIGGER_STATE_LOCATIONS = (
    ("DispatcherFunction", "DueWorkSweep", "Enabled", True),
    ("DispatcherFunction", "OperationalStateChanges", "Enabled", True),
    ("SourceVersionRetentionFunction", "SourceVersionRetentionSweep", "Enabled", True),
    (
        "TerminalOperationalCleanupFunction",
        "TerminalOperationalCleanupSweep",
        "Enabled",
        True,
    ),
    ("StuckExecutionRecoveryScheduleRule", None, "State", "ENABLED"),
)


def _binding(**overrides: object) -> Phase6CoreSamStagingBinding:
    release = str(overrides.get("release_fingerprint", RELEASE))
    runtime_arn = str(overrides.get("agentcore_runtime_arn", RUNTIME_ARN))
    endpoint_arn = str(overrides.get("agentcore_runtime_endpoint_arn", ENDPOINT_ARN))
    runtime_version = str(overrides.get("agentcore_runtime_version", RUNTIME_VERSION))
    qualifier = str(overrides.get("agentcore_runtime_qualifier", RUNTIME_QUALIFIER))
    values: dict[str, object] = {
        "account_id": ACCOUNT,
        "region": REGION,
        "environment": ENVIRONMENT,
        "foundation_stack_id": STACK_ID,
        "release_fingerprint": release,
        "agentcore_runtime_arn": runtime_arn,
        "agentcore_runtime_endpoint_arn": endpoint_arn,
        "agentcore_runtime_version": runtime_version,
        "agentcore_runtime_qualifier": qualifier,
        "agentcore_runtime_binding_fingerprint": agentcore_runtime_binding_fingerprint(
            runtime_arn=runtime_arn,
            endpoint_arn=endpoint_arn,
            qualifier=qualifier,
            runtime_version=runtime_version,
            release_fingerprint=release,
        ),
        "printify_secret_arn": (
            f"arn:aws:secretsmanager:{REGION}:{ACCOUNT}:secret:mr-lister/printify-demo-Ab12Cd"
        ),
        "application_origin": "https://future.example.com",
        "lambda_artifact_bucket": BUCKET,
        "lambda_artifact_key": KEY,
        "lambda_artifact_version": VERSION_ID,
    }
    values.update(overrides)
    return Phase6CoreSamStagingBinding(**values)  # type: ignore[arg-type]


def _descriptor(*, release: str = RELEASE) -> dict[str, object]:
    return {
        "algorithm": "sha256",
        "components": {
            "agentcore": {
                "archive": {
                    "path": AGENTCORE_ARCHIVE_FILENAME,
                    "sha256": AGENTCORE_SHA,
                    "size_bytes": len(AGENTCORE_BYTES),
                },
                "architecture": "arm64",
                "component": "agentcore",
                "deployment_manifest_sha256": "b" * 64,
                "package_format": "zip",
                "runtime": "python3.12",
            },
            "lambda": {
                "archive": {
                    "path": LAMBDA_ARCHIVE_FILENAME,
                    "sha256": LAMBDA_SHA,
                    "size_bytes": len(LAMBDA_BYTES),
                },
                "architecture": "arm64",
                "component": "lambda",
                "deployment_manifest_sha256": "c" * 64,
                "package_format": "zip",
                "runtime": "python3.12",
            },
        },
        "format": "phase6-deployment-artifacts-v1",
        "release_fingerprint": release,
        "target": dict(LINUX_ARM64_TARGET),
    }


def _foundation_binding(**overrides: object) -> dict[str, object]:
    document: dict[str, object] = {
        "account_id": ACCOUNT,
        "artifact_bucket_arn": f"arn:aws:s3:::{BUCKET}",
        "artifact_bucket_name": BUCKET,
        "environment_name": ENVIRONMENT,
        "format": "mr-lister-phase6-foundation-deployment-v1",
        "foundation_template_fingerprint": (
            "689897c254c9db97aa75d508f140980f9b6a5129c0c1fa0121eb8d6ef1e64874"
        ),
        "operational_state_stream_arn": (
            f"arn:aws:dynamodb:{REGION}:{ACCOUNT}:table/mr-lister-phase6-dev/"
            "stream/2026-08-24T17:11:15.037"
        ),
        "operational_state_table_arn": (
            f"arn:aws:dynamodb:{REGION}:{ACCOUNT}:table/mr-lister-phase6-dev"
        ),
        "operational_state_table_name": "mr-lister-phase6-dev",
        "region": REGION,
        "stack_id": STACK_ID,
        "stack_name": "mr-lister-phase6-dev",
    }
    document.update(overrides)
    return document


def _endpoint_observation(**overrides: object) -> dict[str, object]:
    document: dict[str, object] = {
        "agentRuntimeArn": RUNTIME_ARN,
        "agentRuntimeEndpointArn": ENDPOINT_ARN,
        "liveVersion": RUNTIME_VERSION,
        "name": RUNTIME_QUALIFIER,
        "status": "READY",
    }
    document.update(overrides)
    return document


def _verified_lambda_object(**overrides: object) -> VerifiedPhase6S3ReleaseObject:
    values: dict[str, object] = {
        "account_id": ACCOUNT,
        "region": REGION,
        "environment": ENVIRONMENT,
        "component": "lambda",
        "release_fingerprint": RELEASE,
        "archive_sha256": LAMBDA_SHA,
        "size_bytes": len(LAMBDA_BYTES),
        "checksum_sha256_base64": LAMBDA_EXPECTATION.checksum_sha256_base64,
        "bucket": BUCKET,
        "key": KEY,
        "version_id": VERSION_ID,
        "evidence_sha256": "e" * 64,
    }
    values.update(overrides)
    return VerifiedPhase6S3ReleaseObject(**values)  # type: ignore[arg-type]


def _verified_agentcore_object(**overrides: object) -> VerifiedPhase6S3ReleaseObject:
    values: dict[str, object] = {
        "account_id": ACCOUNT,
        "region": REGION,
        "environment": ENVIRONMENT,
        "component": "agentcore",
        "release_fingerprint": RELEASE,
        "archive_sha256": AGENTCORE_SHA,
        "size_bytes": len(AGENTCORE_BYTES),
        "checksum_sha256_base64": AGENTCORE_EXPECTATION.checksum_sha256_base64,
        "bucket": BUCKET,
        "key": AGENTCORE_EXPECTATION.key,
        "version_id": AGENTCORE_VERSION_ID,
        "evidence_sha256": "f" * 64,
    }
    values.update(overrides)
    return VerifiedPhase6S3ReleaseObject(**values)  # type: ignore[arg-type]


def _direct_binding() -> direct.Phase6AgentCoreDirectCodeZipBinding:
    return direct.Phase6AgentCoreDirectCodeZipBinding(
        account_id=ACCOUNT,
        release_fingerprint=RELEASE,
        agentcore_archive_sha256=AGENTCORE_SHA,
    )


def _verified_agentcore_archive(descriptor: dict[str, object]) -> direct.VerifiedAgentCoreArchive:
    return direct.VerifiedAgentCoreArchive(
        sha256=AGENTCORE_SHA,
        size_bytes=len(AGENTCORE_BYTES),
        checksum_sha256_base64=b64encode(sha256(AGENTCORE_BYTES).digest()).decode("ascii"),
        descriptor_sha256=sha256(render_manifest(descriptor)).hexdigest(),
    )


def _runtime_evidence(runtime_documents: dict[Path, bytes]) -> dict[str, object]:
    runtime_input = json.loads(runtime_documents[direct.RUNTIME_CREATE_OUTPUT])
    workload = {
        "workloadIdentityArn": (
            f"arn:aws:bedrock-agentcore:{REGION}:{ACCOUNT}:"
            "workload-identity-directory/default/workload-identity/mr_lister_phase6"
        )
    }
    created_at = "2026-08-24T18:00:00+00:00"
    return {
        "accountId": ACCOUNT,
        "createAgentRuntime": {
            "inputSHA256": sha256(runtime_documents[direct.RUNTIME_CREATE_OUTPUT]).hexdigest(),
            "response": {
                "agentRuntimeArn": RUNTIME_ARN,
                "agentRuntimeId": RUNTIME_ID,
                "agentRuntimeVersion": "1",
                "createdAt": created_at,
                "status": "CREATING",
                "workloadIdentityDetails": workload,
            },
        },
        "format": direct.RUNTIME_V1_EVIDENCE_FORMAT,
        "getAgentRuntime": {
            "request": {"agentRuntimeId": RUNTIME_ID, "agentRuntimeVersion": "1"},
            "response": {
                "agentRuntimeArn": RUNTIME_ARN,
                "agentRuntimeArtifact": runtime_input["agentRuntimeArtifact"],
                "agentRuntimeId": RUNTIME_ID,
                "agentRuntimeName": runtime_input["agentRuntimeName"],
                "agentRuntimeVersion": "1",
                "createdAt": created_at,
                "description": runtime_input["description"],
                "environmentVariables": runtime_input["environmentVariables"],
                "lastUpdatedAt": "2026-08-24T18:03:00+00:00",
                "lifecycleConfiguration": runtime_input["lifecycleConfiguration"],
                "metadataConfiguration": {"requireMMDSV2": True},
                "networkConfiguration": runtime_input["networkConfiguration"],
                "protocolConfiguration": runtime_input["protocolConfiguration"],
                "roleArn": runtime_input["roleArn"],
                "status": "READY",
                "workloadIdentityDetails": workload,
            },
        },
        "listTagsForResource": {
            "request": {"resourceArn": RUNTIME_ARN},
            "response": {"tags": _direct_binding().tags},
        },
        "region": REGION,
        "remoteObjectEvidenceSHA256": _verified_agentcore_object().evidence_sha256,
        "runtimeRenderManifestSHA256": sha256(
            runtime_documents[direct.RUNTIME_MANIFEST_OUTPUT]
        ).hexdigest(),
    }


def _repository(
    tmp_path: Path,
) -> tuple[Path, Path, Path, Path, Path, Path, dict[str, object]]:
    repository = tmp_path / "repository"
    source = repository / SOURCE_TEMPLATE
    source.parent.mkdir(parents=True)
    shutil.copyfile(ROOT / SOURCE_TEMPLATE, source)
    for relative in STATE_MACHINES.values():
        destination = repository / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / relative, destination)

    deployment = repository / ".mr_lister_private/phase6-deployment"
    (deployment / "lambda").mkdir(parents=True)
    (deployment / "agentcore").mkdir()
    artifacts = repository / ".mr_lister_private/phase6-artifacts"
    artifacts.mkdir()
    (artifacts / LAMBDA_ARCHIVE_FILENAME).write_bytes(LAMBDA_BYTES)
    (artifacts / AGENTCORE_ARCHIVE_FILENAME).write_bytes(AGENTCORE_BYTES)
    descriptor = _descriptor()
    (artifacts / DEPLOYMENT_DESCRIPTOR_FILENAME).write_bytes(render_manifest(descriptor))

    evidence = repository / ".mr_lister_private/evidence"
    evidence.mkdir()
    foundation = evidence / "foundation-binding.json"
    endpoint = evidence / "agentcore-endpoint-ready.json"
    agentcore_object = evidence / "agentcore-object-binding.json"
    runtime_v1 = evidence / "agentcore-runtime-v1.json"
    lambda_object = evidence / "lambda-object-binding.json"
    foundation.write_bytes(render_manifest(_foundation_binding()))
    endpoint.write_bytes(render_manifest(_endpoint_observation()))
    agentcore_object.write_bytes(
        b"closed AgentCore evidence is verified by the shared verifier in production"
    )
    lambda_object.write_bytes(b"closed evidence is verified by the shared verifier in production")
    with patch(
        "tools.render_phase6_agentcore_direct_codezip._existing_phase6_environment",
        return_value=TEST_RUNTIME_ENVIRONMENT,
    ):
        runtime_documents = direct.render_phase6_agentcore_runtime_documents(
            _direct_binding(),
            _verified_agentcore_archive(descriptor),
            _verified_agentcore_object(),
            repository_root=repository,
        )
    for relative, raw in runtime_documents.items():
        destination = repository / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(raw)
    runtime_v1.write_bytes(render_manifest(_runtime_evidence(runtime_documents)))
    return repository, deployment, artifacts, foundation, endpoint, lambda_object, descriptor


def _render(
    repository: Path,
    deployment: Path,
    artifacts: Path,
    foundation: Path,
    endpoint: Path,
    lambda_object: Path,
    descriptor: dict[str, object],
    *,
    binding: Phase6CoreSamStagingBinding | None = None,
    object_binding: VerifiedPhase6S3ReleaseObject | None = None,
) -> bytes:
    agentcore_object = repository / ".mr_lister_private/evidence/agentcore-object-binding.json"
    runtime_v1 = repository / ".mr_lister_private/evidence/agentcore-runtime-v1.json"

    def verify_object(
        expectation: Phase6S3ReleaseObjectExpectation,
        *,
        evidence_path: Path,
    ) -> VerifiedPhase6S3ReleaseObject:
        if expectation.component == "lambda":
            assert evidence_path == lambda_object
            return object_binding or _verified_lambda_object()
        assert expectation == AGENTCORE_EXPECTATION
        assert evidence_path == agentcore_object
        return _verified_agentcore_object()

    with (
        patch(
            "tools.render_phase6_core_sam_staging.verify_phase6_deployment_artifacts",
            return_value=descriptor,
        ) as artifact_verifier,
        patch(
            "tools.render_phase6_core_sam_staging.verify_phase6_s3_release_object_evidence",
            side_effect=verify_object,
        ) as object_verifier,
        patch(
            "tools.render_phase6_agentcore_direct_codezip._existing_phase6_environment",
            return_value=TEST_RUNTIME_ENVIRONMENT,
        ),
    ):
        raw = render_phase6_core_sam_staged_template(
            binding or _binding(),
            foundation_binding_path=foundation,
            agentcore_endpoint_observation_path=endpoint,
            agentcore_object_evidence_path=agentcore_object,
            agentcore_runtime_v1_evidence_path=runtime_v1,
            lambda_object_evidence_path=lambda_object,
            repository_root=repository,
            deployment_root=deployment,
            artifact_root=artifacts,
        )
    artifact_verifier.assert_called_once_with(
        deployment.resolve(),
        artifact_root=artifacts.resolve(),
        verify_current_source=True,
    )
    assert object_verifier.call_count == 2
    expectations = [call.args[0] for call in object_verifier.call_args_list]
    assert expectations == [LAMBDA_EXPECTATION, AGENTCORE_EXPECTATION]
    return raw


def _trigger_state_properties(
    document: dict[str, object], logical_id: str, event_name: str | None
) -> dict[str, object]:
    resource = document["Resources"][logical_id]  # type: ignore[index]
    properties = resource["Properties"]
    if event_name is None:
        return properties
    return properties["Events"][event_name]["Properties"]


def _maintenance_concurrency_inventory(document: dict[str, object]) -> dict[str, int]:
    return {
        logical_id: resource["Properties"]["ReservedConcurrentExecutions"]
        for logical_id, resource in document["Resources"].items()  # type: ignore[union-attr]
        if resource["Type"] == "AWS::Serverless::Function"
        and "ReservedConcurrentExecutions" in resource["Properties"]
    }


def test_exact_core_resource_allowlist_types_count_and_dependency_closure(
    tmp_path: Path,
) -> None:
    repository, deployment, artifacts, foundation, endpoint, lambda_object, descriptor = (
        _repository(tmp_path)
    )

    document = json.loads(
        _render(
            repository,
            deployment,
            artifacts,
            foundation,
            endpoint,
            lambda_object,
            descriptor,
        )
    )
    resources = document["Resources"]

    assert {name: resource["Type"] for name, resource in resources.items()} == RESOURCE_TYPES
    assert len(resources) == 40
    assert set(document["Parameters"]) == {
        "AgentCoreRuntimeArn",
        "AgentCoreRuntimeBindingFingerprint",
        "AgentCoreRuntimeEndpointArn",
        "AgentCoreRuntimeQualifier",
        "AgentCoreRuntimeVersion",
        "ApplicationOrigin",
        "EnvironmentName",
        "PrintifySecretArn",
        "ReleaseFingerprint",
    }
    assert "ApplicationCertificateArn" not in document["Parameters"]
    assert set(document["Outputs"]) == {
        "ArtifactBucketName",
        "DeploymentReadiness",
        "PrepareStateMachineArn",
        "ReconcileProductStateMachineArn",
        "RefreshEconomicsStateMachineArn",
        "StateTableName",
        "SynchronizeProductStateMachineArn",
    }
    serialized = json.dumps(document)
    for forbidden in ("AWS::CloudFront", "AWS::Cognito", "SellerHttpApi", "SellerWeb"):
        assert forbidden not in serialized
    verify_core_runtime_dependency_closure(document)


def test_all_seven_handlers_and_four_inline_state_machines_are_exact(
    tmp_path: Path,
) -> None:
    repository, deployment, artifacts, foundation, endpoint, lambda_object, descriptor = (
        _repository(tmp_path)
    )

    document = json.loads(
        _render(
            repository,
            deployment,
            artifacts,
            foundation,
            endpoint,
            lambda_object,
            descriptor,
        )
    )
    resources = document["Resources"]
    code_uri = {"Bucket": BUCKET, "Key": KEY, "Version": VERSION_ID}
    assert {
        name: resource["Properties"]["Handler"]
        for name, resource in resources.items()
        if resource["Type"] == "AWS::Serverless::Function"
    } == FUNCTION_HANDLERS
    assert all(resources[name]["Properties"]["CodeUri"] == code_uri for name in FUNCTION_HANDLERS)
    assert resources["DispatcherFunction"]["Properties"]["Timeout"] == 120
    assert resources["SettlementFunction"]["Properties"]["Timeout"] == 120
    for logical_id, relative in STATE_MACHINES.items():
        properties = resources[logical_id]["Properties"]
        assert "DefinitionUri" not in properties
        assert properties["Definition"] == json.loads((ROOT / relative).read_text())
    assert (
        document["Globals"]["Function"]["Environment"]["Variables"][
            "MR_LISTER_PHASE6_SCAFFOLD_ONLY"
        ]
        == "true"
    )
    assert document["Outputs"]["DeploymentReadiness"]["Value"] == ("CORE_RELEASE_BOUND_STAGED")


def test_checked_source_is_active_but_rendered_core_is_exactly_inert(tmp_path: Path) -> None:
    repository, deployment, artifacts, foundation, endpoint, lambda_object, descriptor = (
        _repository(tmp_path)
    )
    source = json.loads((repository / SOURCE_TEMPLATE).read_bytes())
    rendered = json.loads(
        _render(
            repository,
            deployment,
            artifacts,
            foundation,
            endpoint,
            lambda_object,
            descriptor,
        )
    )

    for logical_id, event_name, field, active_value in TRIGGER_STATE_LOCATIONS:
        source_properties = _trigger_state_properties(source, logical_id, event_name)
        if logical_id == "DispatcherFunction" and event_name == "OperationalStateChanges":
            assert field not in source_properties
        else:
            assert source_properties[field] == active_value
        assert _trigger_state_properties(rendered, logical_id, event_name)[field] in {
            False,
            "DISABLED",
        }

    assert _maintenance_concurrency_inventory(source) == {
        logical_id: 1 for logical_id in MAINTENANCE_FUNCTIONS
    }
    assert _maintenance_concurrency_inventory(rendered) == {
        logical_id: 0 for logical_id in MAINTENANCE_FUNCTIONS
    }

    metadata = rendered["Metadata"]["MrListerPhase6CoreRuntimeStaging"]
    assert metadata["DisabledTriggers"] == DISABLED_TRIGGER_METADATA
    verify_phase6_core_sam_staged_inertness(rendered)


@pytest.mark.parametrize(
    ("logical_id", "event_name", "field", "active_value"), TRIGGER_STATE_LOCATIONS
)
@pytest.mark.parametrize("mutation", ("active", "missing"))
def test_every_core_trigger_flip_or_missing_state_fails_closed(
    tmp_path: Path,
    logical_id: str,
    event_name: str | None,
    field: str,
    active_value: object,
    mutation: str,
) -> None:
    repository, deployment, artifacts, foundation, endpoint, lambda_object, descriptor = (
        _repository(tmp_path)
    )
    rendered = json.loads(
        _render(
            repository,
            deployment,
            artifacts,
            foundation,
            endpoint,
            lambda_object,
            descriptor,
        )
    )
    properties = _trigger_state_properties(rendered, logical_id, event_name)
    if mutation == "active":
        properties[field] = active_value
    else:
        properties.pop(field)

    with pytest.raises(Phase6CoreSamStagingError):
        verify_phase6_core_sam_staged_inertness(rendered)


@pytest.mark.parametrize("logical_id", sorted(MAINTENANCE_FUNCTIONS))
@pytest.mark.parametrize("mutation", ("one", "missing"))
def test_each_staged_maintenance_concurrency_drift_fails_closed(
    tmp_path: Path,
    logical_id: str,
    mutation: str,
) -> None:
    repository, deployment, artifacts, foundation, endpoint, lambda_object, descriptor = (
        _repository(tmp_path)
    )
    rendered = json.loads(
        _render(
            repository,
            deployment,
            artifacts,
            foundation,
            endpoint,
            lambda_object,
            descriptor,
        )
    )
    properties = rendered["Resources"][logical_id]["Properties"]
    if mutation == "one":
        properties["ReservedConcurrentExecutions"] = 1
    else:
        properties.pop("ReservedConcurrentExecutions")

    with pytest.raises(Phase6CoreSamStagingError):
        verify_phase6_core_sam_staged_inertness(rendered)


def test_extra_staged_function_concurrency_fails_closed(tmp_path: Path) -> None:
    repository, deployment, artifacts, foundation, endpoint, lambda_object, descriptor = (
        _repository(tmp_path)
    )
    rendered = json.loads(
        _render(
            repository,
            deployment,
            artifacts,
            foundation,
            endpoint,
            lambda_object,
            descriptor,
        )
    )
    rendered["Resources"]["DispatcherFunction"]["Properties"]["ReservedConcurrentExecutions"] = 0

    with pytest.raises(Phase6CoreSamStagingError):
        verify_phase6_core_sam_staged_inertness(rendered)


@pytest.mark.parametrize("mutation", ("missing", "extra"))
def test_missing_or_extra_core_trigger_fails_closed(tmp_path: Path, mutation: str) -> None:
    repository, deployment, artifacts, foundation, endpoint, lambda_object, descriptor = (
        _repository(tmp_path)
    )
    rendered = json.loads(
        _render(
            repository,
            deployment,
            artifacts,
            foundation,
            endpoint,
            lambda_object,
            descriptor,
        )
    )
    events = rendered["Resources"]["DispatcherFunction"]["Properties"]["Events"]
    if mutation == "missing":
        events.pop("DueWorkSweep")
    else:
        events["UnreviewedSweep"] = {
            "Properties": {"Enabled": False, "Schedule": "rate(1 day)"},
            "Type": "Schedule",
        }

    with pytest.raises(Phase6CoreSamStagingError):
        verify_phase6_core_sam_staged_inertness(rendered)


def test_foundation_resources_remain_semantically_identical_to_checked_source(
    tmp_path: Path,
) -> None:
    repository, deployment, artifacts, foundation, endpoint, lambda_object, descriptor = (
        _repository(tmp_path)
    )
    source = json.loads((repository / SOURCE_TEMPLATE).read_text())

    document = json.loads(
        _render(
            repository,
            deployment,
            artifacts,
            foundation,
            endpoint,
            lambda_object,
            descriptor,
        )
    )

    for logical_id in (
        "OperationalStateTable",
        "PrivateArtifactBucket",
        "PrivateArtifactBucketPolicy",
    ):
        assert document["Resources"][logical_id] == source["Resources"][logical_id]
    assert document["Resources"]["OperationalStateTable"]["DeletionPolicy"] == "Retain"
    assert document["Resources"]["PrivateArtifactBucket"]["UpdateReplacePolicy"] == "Retain"


def test_exact_binding_metadata_canonical_determinism_and_no_source_mutation(
    tmp_path: Path,
) -> None:
    repository, deployment, artifacts, foundation, endpoint, lambda_object, descriptor = (
        _repository(tmp_path)
    )
    source_before = (repository / SOURCE_TEMPLATE).read_bytes()
    asl_before = {
        relative: (repository / relative).read_bytes() for relative in STATE_MACHINES.values()
    }

    first = _render(
        repository,
        deployment,
        artifacts,
        foundation,
        endpoint,
        lambda_object,
        descriptor,
    )
    second = _render(
        repository,
        deployment,
        artifacts,
        foundation,
        endpoint,
        lambda_object,
        descriptor,
    )
    document = json.loads(first)
    metadata = document["Metadata"]["MrListerPhase6CoreRuntimeStaging"]

    assert first == second
    assert (
        first
        == (
            json.dumps(document, allow_nan=False, indent=2, separators=(",", ": "), sort_keys=True)
            + "\n"
        ).encode()
    )
    assert metadata["ReleaseFingerprint"] == RELEASE
    assert metadata["DisabledTriggers"] == DISABLED_TRIGGER_METADATA
    assert metadata["LambdaArtifact"] == {
        "Bucket": BUCKET,
        "ChecksumSHA256Base64": b64encode(bytes.fromhex(LAMBDA_SHA)).decode("ascii"),
        "Key": KEY,
        "ObjectEvidenceSha256": "e" * 64,
        "Sha256": LAMBDA_SHA,
        "SizeBytes": len(LAMBDA_BYTES),
        "Version": VERSION_ID,
    }
    assert metadata["Target"] == {
        "AccountId": ACCOUNT,
        "Environment": ENVIRONMENT,
        "Region": REGION,
    }
    assert metadata["Foundation"]["StackId"] == STACK_ID
    assert metadata["AgentCore"]["Status"] == "READY"
    assert (
        metadata["AgentCore"]["RuntimeEvidenceSha256"]
        == sha256(
            (repository / ".mr_lister_private/evidence/agentcore-runtime-v1.json").read_bytes()
        ).hexdigest()
    )
    assert (
        metadata["AgentCore"]["RuntimeCreateInputSha256"]
        == sha256((repository / direct.RUNTIME_CREATE_OUTPUT).read_bytes()).hexdigest()
    )
    assert (
        metadata["AgentCore"]["RuntimeRenderManifestSha256"]
        == sha256((repository / direct.RUNTIME_MANIFEST_OUTPUT).read_bytes()).hexdigest()
    )
    assert metadata["AgentCoreArtifact"] == {
        "Bucket": BUCKET,
        "ChecksumSHA256Base64": AGENTCORE_EXPECTATION.checksum_sha256_base64,
        "Key": AGENTCORE_EXPECTATION.key,
        "ObjectEvidenceSha256": "f" * 64,
        "Sha256": AGENTCORE_SHA,
        "SizeBytes": len(AGENTCORE_BYTES),
        "Version": AGENTCORE_VERSION_ID,
    }
    assert {name: value["Path"] for name, value in metadata["StateMachineDefinitions"].items()} == {
        name: path.as_posix() for name, path in STATE_MACHINES.items()
    }
    assert (
        metadata["ArtifactDescriptor"]["Sha256"] == sha256(render_manifest(descriptor)).hexdigest()
    )
    assert (repository / SOURCE_TEMPLATE).read_bytes() == source_before
    assert all((repository / path).read_bytes() == raw for path, raw in asl_before.items())


def test_core_staging_rejects_substituted_runtime_evidence_or_create_authority(
    tmp_path: Path,
) -> None:
    repository, deployment, artifacts, foundation, endpoint, lambda_object, descriptor = (
        _repository(tmp_path)
    )
    runtime_evidence = repository / ".mr_lister_private/evidence/agentcore-runtime-v1.json"
    original_evidence = runtime_evidence.read_bytes()
    altered = json.loads(original_evidence)
    altered["getAgentRuntime"]["response"]["agentRuntimeArtifact"]["codeConfiguration"]["code"][
        "s3"
    ]["versionId"] = "substituted-version"
    runtime_evidence.write_bytes(render_manifest(altered))
    with pytest.raises(Phase6CoreSamStagingError):
        _render(
            repository,
            deployment,
            artifacts,
            foundation,
            endpoint,
            lambda_object,
            descriptor,
        )

    runtime_evidence.write_bytes(original_evidence)
    runtime_input = repository / direct.RUNTIME_CREATE_OUTPUT
    original_runtime_input = runtime_input.read_bytes()
    altered_input = json.loads(original_runtime_input)
    altered_input["roleArn"] = f"arn:aws:iam::{ACCOUNT}:role/substituted"
    runtime_input.write_bytes(render_manifest(altered_input))
    with pytest.raises(Phase6CoreSamStagingError):
        _render(
            repository,
            deployment,
            artifacts,
            foundation,
            endpoint,
            lambda_object,
            descriptor,
        )


def test_core_staging_requires_mmdsv2_runtime_evidence(tmp_path: Path) -> None:
    repository, deployment, artifacts, foundation, endpoint, lambda_object, descriptor = (
        _repository(tmp_path)
    )
    runtime_evidence = repository / ".mr_lister_private/evidence/agentcore-runtime-v1.json"
    evidence = json.loads(runtime_evidence.read_bytes())
    evidence["getAgentRuntime"]["response"]["metadataConfiguration"] = {"requireMMDSV2": False}
    runtime_evidence.write_bytes(render_manifest(evidence))

    with pytest.raises(Phase6CoreSamStagingError):
        _render(
            repository,
            deployment,
            artifacts,
            foundation,
            endpoint,
            lambda_object,
            descriptor,
        )


@pytest.mark.parametrize(
    "overrides",
    (
        {"lambda_artifact_bucket": "different-bucket"},
        {"lambda_artifact_key": "phase6/releases/wrong/lambda/archive.zip"},
        {"lambda_artifact_version": "latest"},
    ),
)
def test_wrong_bucket_key_or_moving_version_is_rejected(
    tmp_path: Path,
    overrides: dict[str, str],
) -> None:
    repository, deployment, artifacts, foundation, endpoint, lambda_object, descriptor = (
        _repository(tmp_path)
    )

    with pytest.raises(Phase6CoreSamStagingError):
        binding = _binding(**overrides)
        _render(
            repository,
            deployment,
            artifacts,
            foundation,
            endpoint,
            lambda_object,
            descriptor,
            binding=binding,
        )


def test_bare_version_id_and_mismatched_shared_object_proof_are_rejected(
    tmp_path: Path,
) -> None:
    repository, deployment, artifacts, foundation, endpoint, lambda_object, descriptor = (
        _repository(tmp_path)
    )
    with (
        patch(
            "tools.render_phase6_core_sam_staging.verify_phase6_deployment_artifacts",
            return_value=descriptor,
        ),
        pytest.raises(Phase6CoreSamStagingError),
    ):
        render_phase6_core_sam_staged_template(
            _binding(),
            foundation_binding_path=foundation,
            agentcore_endpoint_observation_path=endpoint,
            agentcore_object_evidence_path=(
                repository / ".mr_lister_private/evidence/agentcore-object-binding.json"
            ),
            agentcore_runtime_v1_evidence_path=(
                repository / ".mr_lister_private/evidence/agentcore-runtime-v1.json"
            ),
            lambda_object_evidence_path=lambda_object,
            repository_root=repository,
            deployment_root=deployment,
            artifact_root=artifacts,
        )

    with pytest.raises(Phase6CoreSamStagingError):
        _render(
            repository,
            deployment,
            artifacts,
            foundation,
            endpoint,
            lambda_object,
            descriptor,
            object_binding=_verified_lambda_object(version_id="different-version-id"),
        )


def test_runtime_release_foundation_and_ready_evidence_cross_binding_is_fail_closed(
    tmp_path: Path,
) -> None:
    repository, deployment, artifacts, foundation, endpoint, lambda_object, descriptor = (
        _repository(tmp_path)
    )

    endpoint.write_bytes(render_manifest(_endpoint_observation(status="UPDATING")))
    with pytest.raises(Phase6CoreSamStagingError):
        _render(
            repository,
            deployment,
            artifacts,
            foundation,
            endpoint,
            lambda_object,
            descriptor,
        )

    endpoint.write_bytes(render_manifest(_endpoint_observation()))
    foundation.write_bytes(
        render_manifest(_foundation_binding(artifact_bucket_name="different-bucket"))
    )
    with pytest.raises(Phase6CoreSamStagingError):
        _render(
            repository,
            deployment,
            artifacts,
            foundation,
            endpoint,
            lambda_object,
            descriptor,
        )

    foundation.write_bytes(render_manifest(_foundation_binding()))
    wrong_release = "d" * 64
    wrong_expectation = Phase6S3ReleaseObjectExpectation(
        account_id=ACCOUNT,
        region=REGION,
        environment=ENVIRONMENT,
        component="lambda",
        release_fingerprint=wrong_release,
        archive_sha256=LAMBDA_SHA,
        size_bytes=len(LAMBDA_BYTES),
    )
    wrong_binding = _binding(
        release_fingerprint=wrong_release,
        lambda_artifact_key=wrong_expectation.key,
    )
    with pytest.raises(Phase6CoreSamStagingError):
        _render(
            repository,
            deployment,
            artifacts,
            foundation,
            endpoint,
            lambda_object,
            descriptor,
            binding=wrong_binding,
        )

    with pytest.raises(Phase6CoreSamStagingError):
        _binding(agentcore_runtime_arn=RUNTIME_ARN.replace(ACCOUNT, "999999999999"))


def test_checked_source_and_asl_drift_or_symlink_is_rejected(tmp_path: Path) -> None:
    repository, deployment, artifacts, foundation, endpoint, lambda_object, descriptor = (
        _repository(tmp_path)
    )
    source = repository / SOURCE_TEMPLATE
    source.write_bytes(source.read_bytes() + b"\n")
    with pytest.raises(Phase6CoreSamStagingError):
        _render(
            repository,
            deployment,
            artifacts,
            foundation,
            endpoint,
            lambda_object,
            descriptor,
        )

    shutil.copyfile(ROOT / SOURCE_TEMPLATE, source)
    asl = repository / STATE_MACHINES["PrepareStateMachine"]
    asl.write_bytes(asl.read_bytes() + b"\n")
    with pytest.raises(Phase6CoreSamStagingError):
        _render(
            repository,
            deployment,
            artifacts,
            foundation,
            endpoint,
            lambda_object,
            descriptor,
        )

    shutil.copyfile(ROOT / STATE_MACHINES["PrepareStateMachine"], asl)
    source.unlink()
    source.symlink_to(ROOT / SOURCE_TEMPLATE)
    with pytest.raises(Phase6CoreSamStagingError):
        _render(
            repository,
            deployment,
            artifacts,
            foundation,
            endpoint,
            lambda_object,
            descriptor,
        )

    source.unlink()
    shutil.copyfile(ROOT / SOURCE_TEMPLATE, source)
    asl.unlink()
    asl.symlink_to(ROOT / STATE_MACHINES["PrepareStateMachine"])
    with pytest.raises(Phase6CoreSamStagingError):
        _render(
            repository,
            deployment,
            artifacts,
            foundation,
            endpoint,
            lambda_object,
            descriptor,
        )


def test_core_loader_rejects_canonical_evidence_through_symlinked_grandparent(
    tmp_path: Path,
) -> None:
    repository, deployment, artifacts, foundation, endpoint, lambda_object, descriptor = (
        _repository(tmp_path)
    )
    real_root = tmp_path / "real-authority-root"
    nested = real_root / "nested"
    nested.mkdir(parents=True)
    target = nested / foundation.name
    target.write_bytes(foundation.read_bytes())
    alias = tmp_path / "authority-alias"
    alias.symlink_to(real_root, target_is_directory=True)
    aliased_foundation = alias / "nested" / foundation.name
    assert not aliased_foundation.is_symlink()
    assert not aliased_foundation.parent.is_symlink()

    with pytest.raises(Phase6CoreSamStagingError):
        _render(
            repository,
            deployment,
            artifacts,
            aliased_foundation,
            endpoint,
            lambda_object,
            descriptor,
        )


def test_dependency_closure_rejects_any_omitted_or_unknown_reference(tmp_path: Path) -> None:
    repository, deployment, artifacts, foundation, endpoint, lambda_object, descriptor = (
        _repository(tmp_path)
    )
    document = json.loads(
        _render(
            repository,
            deployment,
            artifacts,
            foundation,
            endpoint,
            lambda_object,
            descriptor,
        )
    )
    document["Resources"]["DispatcherFunction"]["DependsOn"] = "SellerHttpApi"

    with pytest.raises(Phase6CoreSamStagingError):
        verify_core_runtime_dependency_closure(document)


def test_fixed_output_is_exclusive_and_byte_drift_verification_fails(tmp_path: Path) -> None:
    repository, deployment, artifacts, foundation, endpoint, lambda_object, descriptor = (
        _repository(tmp_path)
    )
    options = {
        "foundation_binding_path": foundation,
        "agentcore_endpoint_observation_path": endpoint,
        "agentcore_object_evidence_path": (
            repository / ".mr_lister_private/evidence/agentcore-object-binding.json"
        ),
        "agentcore_runtime_v1_evidence_path": (
            repository / ".mr_lister_private/evidence/agentcore-runtime-v1.json"
        ),
        "lambda_object_evidence_path": lambda_object,
        "repository_root": repository,
        "deployment_root": deployment,
        "artifact_root": artifacts,
    }
    with (
        patch(
            "tools.render_phase6_core_sam_staging.verify_phase6_deployment_artifacts",
            return_value=descriptor,
        ),
        patch(
            "tools.render_phase6_core_sam_staging.verify_phase6_s3_release_object_evidence",
            side_effect=lambda expectation, **_kwargs: (
                _verified_lambda_object()
                if expectation.component == "lambda"
                else _verified_agentcore_object()
            ),
        ),
        patch(
            "tools.render_phase6_agentcore_direct_codezip._existing_phase6_environment",
            return_value=TEST_RUNTIME_ENVIRONMENT,
        ),
    ):
        destination = write_phase6_core_sam_staged_template(_binding(), **options)
        assert destination == repository / CORE_STAGED_TEMPLATE_OUTPUT
        verify_rendered_phase6_core_sam_staged_template(_binding(), **options)
        with pytest.raises(Phase6CoreSamStagingError):
            write_phase6_core_sam_staged_template(_binding(), **options)
        destination.write_bytes(destination.read_bytes() + b" ")
        with pytest.raises(Phase6CoreSamStagingError):
            verify_rendered_phase6_core_sam_staged_template(_binding(), **options)


def test_activation_is_explicitly_rejected() -> None:
    with pytest.raises(Phase6CoreSamStagingError, match="cannot activate"):
        reject_phase6_core_sam_activation()


def test_sam_schema_validation_when_cli_is_available(tmp_path: Path) -> None:
    sam = shutil.which("sam")
    if sam is None:
        pytest.skip("AWS SAM CLI is not available")
    repository, deployment, artifacts, foundation, endpoint, lambda_object, descriptor = (
        _repository(tmp_path)
    )
    rendered = _render(
        repository,
        deployment,
        artifacts,
        foundation,
        endpoint,
        lambda_object,
        descriptor,
    )
    template = tmp_path / "core-staged.json"
    template.write_bytes(rendered)

    completed = subprocess.run(
        [sam, "validate", "--lint", "--region", REGION, "--template-file", str(template)],
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr


def test_parameter_object_is_immutable() -> None:
    with pytest.raises(Phase6CoreSamStagingError):
        replace(_binding(), environment="prod")
    with pytest.raises(Phase6CoreSamStagingError):
        replace(_binding(), foundation_stack_id=STACK_ID.replace("dev", "prod"))
