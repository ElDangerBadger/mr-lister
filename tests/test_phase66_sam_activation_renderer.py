from __future__ import annotations

import json
import shutil
import subprocess
from base64 import b64encode
from dataclasses import asdict, replace
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

import tools.render_phase6_agentcore_direct_codezip as direct
import tools.render_phase6_sam_activation as sam_renderer
from mr_lister.agent.runtime_binding import agentcore_runtime_binding_fingerprint
from mr_lister.release.phase6 import LINUX_ARM64_TARGET, render_manifest
from tools.build_phase66_source_bundles import (
    AGENTCORE_ARCHIVE_FILENAME,
    DEPLOYMENT_DESCRIPTOR_FILENAME,
    LAMBDA_ARCHIVE_FILENAME,
)
from tools.render_phase6_sam_activation import (
    SOURCE_TEMPLATE,
    STAGED_TEMPLATE_OUTPUT,
    Phase6SamStagingBinding,
    Phase6SamStagingError,
    reject_phase6_sam_activation,
    render_phase6_sam_staged_template,
    verify_phase6_sam_staged_inertness,
    verify_rendered_phase6_sam_staged_template,
    write_phase6_sam_staged_template,
)
from tools.verify_phase6_s3_release_object import (
    Phase6S3ReleaseObjectExpectation,
    VerifiedPhase6S3ReleaseObject,
)

ROOT = Path(__file__).parents[1]
ACCOUNT = "123456789012"
REGION = "us-west-2"
ENVIRONMENT = "dev"
RELEASE = "a" * 64
VERSION = "1"
QUALIFIER = "phase6_v1_dev"
RUNTIME_ARN = f"arn:aws:bedrock-agentcore:{REGION}:{ACCOUNT}:runtime/mr_lister_phase6-Ab12Cd34Ef"
RUNTIME_ID = RUNTIME_ARN.rsplit("/", 1)[1]
ENDPOINT_ARN = f"{RUNTIME_ARN}/runtime-endpoint/{QUALIFIER}"
RUNTIME_BINDING = agentcore_runtime_binding_fingerprint(
    runtime_arn=RUNTIME_ARN,
    endpoint_arn=ENDPOINT_ARN,
    qualifier=QUALIFIER,
    runtime_version=VERSION,
    release_fingerprint=RELEASE,
)
LAMBDA_BYTES = b"synthetic exact sealed lambda archive"
AGENTCORE_BYTES = b"synthetic exact sealed agentcore archive"
LAMBDA_SHA256 = sha256(LAMBDA_BYTES).hexdigest()
AGENTCORE_SHA256 = sha256(AGENTCORE_BYTES).hexdigest()
LAMBDA_BUCKET = f"mr-lister-phase6-artifacts-{ENVIRONMENT}-{ACCOUNT}-{REGION}"
LAMBDA_EXPECTATION = Phase6S3ReleaseObjectExpectation(
    account_id=ACCOUNT,
    region=REGION,
    environment=ENVIRONMENT,
    component="lambda",
    release_fingerprint=RELEASE,
    archive_sha256=LAMBDA_SHA256,
    size_bytes=len(LAMBDA_BYTES),
)
LAMBDA_KEY = LAMBDA_EXPECTATION.key
LAMBDA_VERSION = "3LgT7f_ExactVersion+AbCd/012="
OBJECT_EVIDENCE_SHA256 = "e" * 64
AGENTCORE_VERSION = "3AgentCore_ExactVersion+AbCd/012="
AGENTCORE_OBJECT_EVIDENCE_SHA256 = "f" * 64
AGENTCORE_EXPECTATION = Phase6S3ReleaseObjectExpectation(
    account_id=ACCOUNT,
    region=REGION,
    environment=ENVIRONMENT,
    component="agentcore",
    release_fingerprint=RELEASE,
    archive_sha256=AGENTCORE_SHA256,
    size_bytes=len(AGENTCORE_BYTES),
)
TEST_RUNTIME_ENVIRONMENT = {"MR_LISTER_RELEASE_FINGERPRINT": RELEASE}

FUNCTION_HANDLERS = {
    "DispatcherFunction": "phase6_lambda.dispatcher_handler",
    "PreparationDispatchFunction": "phase6_lambda.preparation_dispatch_handler",
    "ProviderDraftFunction": "phase6_lambda.provider_draft_handler",
    "ReviewQueryApiFunction": "phase6_lambda.review_query_api_handler",
    "SellerCommandApiFunction": "phase6_lambda.seller_command_api_handler",
    "SettlementFunction": "phase6_lambda.settlement_handler",
    "SourceVersionRetentionFunction": "phase6_lambda.source_version_retention_handler",
    "StuckExecutionRecoveryFunction": "phase6_lambda.stuck_execution_recovery_handler",
    "TerminalOperationalCleanupFunction": "phase6_lambda.terminal_operational_cleanup_handler",
    "UploadApiFunction": "phase6_lambda.upload_api_handler",
}

SOURCE_RESERVED_CONCURRENCY = {
    "SourceVersionRetentionFunction": 1,
    "StuckExecutionRecoveryFunction": 1,
    "TerminalOperationalCleanupFunction": 1,
}
STAGED_RESERVED_CONCURRENCY = {logical_id: 0 for logical_id in SOURCE_RESERVED_CONCURRENCY}

STATE_MACHINE_IDENTITIES = {
    "PrepareStateMachine": (
        Path("infra/phase6/statemachine/prepare.asl.json"),
        "c8ad39e393fa82e00d08d68aab684315167d5bed08e7bceb248bbef9f3826031",
    ),
    "ReconcileProductStateMachine": (
        Path("infra/phase6/statemachine/reconcile-product.asl.json"),
        "da9de08270b43e5a4a05814ab084463c939ccbd512649020388e41318f0bc097",
    ),
    "RefreshEconomicsStateMachine": (
        Path("infra/phase6/statemachine/refresh-economics.asl.json"),
        "c105021f581ad84a55526bf6713b63dbbfb55eda6a80fc04b085f3afdefe534d",
    ),
    "SynchronizeProductStateMachine": (
        Path("infra/phase6/statemachine/synchronize-product.asl.json"),
        "7d439e439e325a118fdf5e899bc70bf67a729efa321a90060d231007f5e1b86d",
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
DISABLED_EXTERNAL_SERVING_METADATA = {
    "SellerHttpApi": {
        "DisableExecuteApiEndpoint": True,
        "Type": "AWS::Serverless::HttpApi",
    },
    "SellerWebDistribution": {
        "Enabled": False,
        "Type": "AWS::CloudFront::Distribution",
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


def _binding(**overrides: object) -> Phase6SamStagingBinding:
    values: dict[str, object] = {
        "account_id": ACCOUNT,
        "region": REGION,
        "environment": ENVIRONMENT,
        "release_fingerprint": RELEASE,
        "agentcore_runtime_arn": RUNTIME_ARN,
        "agentcore_runtime_endpoint_arn": ENDPOINT_ARN,
        "agentcore_runtime_version": VERSION,
        "agentcore_runtime_qualifier": QUALIFIER,
        "agentcore_runtime_binding_fingerprint": RUNTIME_BINDING,
        "printify_secret_arn": (
            f"arn:aws:secretsmanager:{REGION}:{ACCOUNT}:secret:mr-lister/printify-demo-Ab12Cd"
        ),
        "application_origin": "https://demo.example.com",
        "application_certificate_arn": (
            f"arn:aws:acm:us-east-1:{ACCOUNT}:certificate/12345678-1234-4abc-8def-1234567890ab"
        ),
        "lambda_artifact_bucket": LAMBDA_BUCKET,
        "lambda_artifact_key": LAMBDA_KEY,
        "lambda_artifact_version": LAMBDA_VERSION,
    }
    values.update(overrides)
    return Phase6SamStagingBinding(**values)  # type: ignore[arg-type]


def _descriptor() -> dict[str, object]:
    return {
        "algorithm": "sha256",
        "components": {
            "agentcore": {
                "archive": {
                    "path": AGENTCORE_ARCHIVE_FILENAME,
                    "sha256": AGENTCORE_SHA256,
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
                    "sha256": LAMBDA_SHA256,
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
        "release_fingerprint": RELEASE,
        "target": dict(LINUX_ARM64_TARGET),
    }


def _repository(tmp_path: Path) -> tuple[Path, Path, Path, dict[str, object]]:
    repository = tmp_path / "repository"
    source = repository / SOURCE_TEMPLATE
    source.parent.mkdir(parents=True)
    shutil.copyfile(ROOT / SOURCE_TEMPLATE, source)
    for path, _fingerprint in STATE_MACHINE_IDENTITIES.values():
        destination = repository / path
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / path, destination)
    deployment = repository / ".mr_lister_private/phase6-deployment"
    (deployment / "lambda").mkdir(parents=True)
    (deployment / "agentcore").mkdir()
    artifacts = repository / ".mr_lister_private/phase6-artifacts"
    artifacts.mkdir()
    (artifacts / LAMBDA_ARCHIVE_FILENAME).write_bytes(LAMBDA_BYTES)
    (artifacts / AGENTCORE_ARCHIVE_FILENAME).write_bytes(AGENTCORE_BYTES)
    descriptor = _descriptor()
    (artifacts / DEPLOYMENT_DESCRIPTOR_FILENAME).write_bytes(render_manifest(descriptor))
    _lambda_object_evidence(repository).parent.mkdir(parents=True)
    _lambda_object_evidence(repository).write_bytes(b"verified by the shared evidence gate")
    _agentcore_object_evidence(repository).write_bytes(
        b"AgentCore object verified by the shared evidence gate"
    )
    _endpoint_observation(repository).write_bytes(
        render_manifest(
            {
                "agentRuntimeArn": RUNTIME_ARN,
                "agentRuntimeEndpointArn": ENDPOINT_ARN,
                "liveVersion": VERSION,
                "name": QUALIFIER,
                "status": "READY",
            }
        )
    )
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
    _runtime_v1_evidence(repository).write_bytes(
        render_manifest(_runtime_evidence_document(runtime_documents))
    )
    return repository, deployment, artifacts, descriptor


def _lambda_object_evidence(repository: Path) -> Path:
    return repository / ".mr_lister_private/evidence/lambda-object-binding.json"


def _agentcore_object_evidence(repository: Path) -> Path:
    return repository / ".mr_lister_private/evidence/agentcore-object-binding.json"


def _runtime_v1_evidence(repository: Path) -> Path:
    return repository / ".mr_lister_private/evidence/agentcore-runtime-v1.json"


def _endpoint_observation(repository: Path) -> Path:
    return repository / ".mr_lister_private/evidence/agentcore-endpoint-ready.json"


def _agentcore_evidence_arguments(repository: Path) -> dict[str, Path]:
    return {
        "agentcore_endpoint_observation_path": _endpoint_observation(repository),
        "agentcore_object_evidence_path": _agentcore_object_evidence(repository),
        "agentcore_runtime_v1_evidence_path": _runtime_v1_evidence(repository),
    }


def _verified_lambda_object(**overrides: object) -> VerifiedPhase6S3ReleaseObject:
    values: dict[str, object] = {
        "account_id": ACCOUNT,
        "region": REGION,
        "environment": ENVIRONMENT,
        "component": "lambda",
        "release_fingerprint": RELEASE,
        "archive_sha256": LAMBDA_SHA256,
        "size_bytes": len(LAMBDA_BYTES),
        "checksum_sha256_base64": LAMBDA_EXPECTATION.checksum_sha256_base64,
        "bucket": LAMBDA_BUCKET,
        "key": LAMBDA_KEY,
        "version_id": LAMBDA_VERSION,
        "evidence_sha256": OBJECT_EVIDENCE_SHA256,
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
        "archive_sha256": AGENTCORE_SHA256,
        "size_bytes": len(AGENTCORE_BYTES),
        "checksum_sha256_base64": AGENTCORE_EXPECTATION.checksum_sha256_base64,
        "bucket": LAMBDA_BUCKET,
        "key": AGENTCORE_EXPECTATION.key,
        "version_id": AGENTCORE_VERSION,
        "evidence_sha256": AGENTCORE_OBJECT_EVIDENCE_SHA256,
    }
    values.update(overrides)
    return VerifiedPhase6S3ReleaseObject(**values)  # type: ignore[arg-type]


def _direct_binding() -> direct.Phase6AgentCoreDirectCodeZipBinding:
    return direct.Phase6AgentCoreDirectCodeZipBinding(
        account_id=ACCOUNT,
        release_fingerprint=RELEASE,
        agentcore_archive_sha256=AGENTCORE_SHA256,
    )


def _verified_agentcore_archive(descriptor: dict[str, object]) -> direct.VerifiedAgentCoreArchive:
    return direct.VerifiedAgentCoreArchive(
        sha256=AGENTCORE_SHA256,
        size_bytes=len(AGENTCORE_BYTES),
        checksum_sha256_base64=b64encode(sha256(AGENTCORE_BYTES).digest()).decode("ascii"),
        descriptor_sha256=sha256(render_manifest(descriptor)).hexdigest(),
    )


def _runtime_evidence_document(runtime_documents: dict[Path, bytes]) -> dict[str, object]:
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
        "remoteObjectEvidenceSHA256": AGENTCORE_OBJECT_EVIDENCE_SHA256,
        "runtimeRenderManifestSHA256": sha256(
            runtime_documents[direct.RUNTIME_MANIFEST_OUTPUT]
        ).hexdigest(),
    }


def _render(
    repository: Path,
    deployment: Path,
    artifacts: Path,
    descriptor: dict[str, object],
) -> bytes:
    def verify_object(
        expectation: Phase6S3ReleaseObjectExpectation,
        *,
        evidence_path: Path,
    ) -> VerifiedPhase6S3ReleaseObject:
        if expectation.component == "lambda":
            assert evidence_path == _lambda_object_evidence(repository)
            return _verified_lambda_object()
        assert expectation == AGENTCORE_EXPECTATION
        assert evidence_path == _agentcore_object_evidence(repository)
        return _verified_agentcore_object()

    with (
        patch(
            "tools.render_phase6_sam_activation.verify_phase6_deployment_artifacts",
            return_value=descriptor,
        ) as verifier,
        patch(
            "tools.render_phase6_sam_activation.verify_phase6_s3_release_object_evidence",
            side_effect=verify_object,
        ) as object_verifier,
        patch(
            "tools.render_phase6_agentcore_direct_codezip._existing_phase6_environment",
            return_value=TEST_RUNTIME_ENVIRONMENT,
        ),
    ):
        rendered = render_phase6_sam_staged_template(
            _binding(),
            agentcore_endpoint_observation_path=_endpoint_observation(repository),
            agentcore_object_evidence_path=_agentcore_object_evidence(repository),
            agentcore_runtime_v1_evidence_path=_runtime_v1_evidence(repository),
            lambda_object_evidence_path=_lambda_object_evidence(repository),
            repository_root=repository,
            deployment_root=deployment,
            artifact_root=artifacts,
        )
    verifier.assert_called_once_with(
        deployment.resolve(),
        artifact_root=artifacts.resolve(),
        verify_current_source=True,
    )
    assert object_verifier.call_count == 2
    assert [call.args[0] for call in object_verifier.call_args_list] == [
        LAMBDA_EXPECTATION,
        AGENTCORE_EXPECTATION,
    ]
    return rendered


def _functions(document: dict[str, object]) -> dict[str, dict[str, object]]:
    resources = document["Resources"]
    assert isinstance(resources, dict)
    return {
        name: resource
        for name, resource in resources.items()
        if isinstance(resource, dict) and resource.get("Type") == "AWS::Serverless::Function"
    }


def _reserved_concurrency_inventory(document: dict[str, object]) -> dict[str, object]:
    inventory: dict[str, object] = {}
    for logical_id, function in _functions(document).items():
        properties = function["Properties"]
        if "ReservedConcurrentExecutions" in properties:
            inventory[logical_id] = properties["ReservedConcurrentExecutions"]
    return inventory


def _trigger_state_properties(
    document: dict[str, object], logical_id: str, event_name: str | None
) -> dict[str, object]:
    resource = document["Resources"][logical_id]  # type: ignore[index]
    properties = resource["Properties"]
    if event_name is None:
        return properties
    return properties["Events"][event_name]["Properties"]


def _http_api_routes(document: dict[str, object]) -> dict[str, object]:
    routes: dict[str, object] = {}
    for logical_id, function in _functions(document).items():
        properties = function["Properties"]
        for event_name, event in properties.get("Events", {}).items():
            if event["Type"] == "HttpApi":
                routes[f"{logical_id}.Events.{event_name}"] = event
    return routes


def test_render_stages_all_ten_functions_without_activating_runtime(tmp_path: Path) -> None:
    repository, deployment, artifacts, descriptor = _repository(tmp_path)
    checked_before = (repository / SOURCE_TEMPLATE).read_bytes()

    raw = _render(repository, deployment, artifacts, descriptor)
    document = json.loads(raw)
    functions = _functions(document)
    expected_code_uri = {
        "Bucket": LAMBDA_BUCKET,
        "Key": LAMBDA_KEY,
        "Version": LAMBDA_VERSION,
    }

    assert set(functions) == set(FUNCTION_HANDLERS)
    assert len(functions) == 10
    for logical_id, handler in FUNCTION_HANDLERS.items():
        properties = functions[logical_id]["Properties"]
        assert properties["Handler"] == handler
        assert properties["CodeUri"] == expected_code_uri
        assert properties["CodeUri"] != "lambda/"

    resources = document["Resources"]
    state_machines = {
        logical_id: resource
        for logical_id, resource in resources.items()
        if resource["Type"] == "AWS::Serverless::StateMachine"
    }
    checked_template = json.loads((repository / SOURCE_TEMPLATE).read_bytes())
    for logical_id, (path, _fingerprint) in STATE_MACHINE_IDENTITIES.items():
        properties = state_machines[logical_id]["Properties"]
        assert "DefinitionUri" not in properties
        assert properties["Definition"] == json.loads((repository / path).read_bytes())
        assert (
            properties["DefinitionSubstitutions"]
            == checked_template["Resources"][logical_id]["Properties"]["DefinitionSubstitutions"]
        )

    serialized = raw.decode("utf-8")
    assert '"CodeUri": "lambda/"' not in serialized
    assert '"DefinitionUri"' not in serialized

    variables = document["Globals"]["Function"]["Environment"]["Variables"]
    assert variables["MR_LISTER_PHASE6_SCAFFOLD_ONLY"] == "true"
    assert variables["MR_LISTER_RELEASE_FINGERPRINT"] == {"Ref": "ReleaseFingerprint"}
    assert document["Outputs"]["DeploymentReadiness"] == {
        "Description": (
            "The exact sealed Phase 6 artifacts and READY AgentCore v1 are staged; runtime and "
            "web activation remain fail-closed."
        ),
        "Value": "RELEASE_BOUND_STAGED",
    }
    assert (repository / SOURCE_TEMPLATE).read_bytes() == checked_before


def test_checked_source_serves_and_triggers_but_full_render_is_exactly_inert(
    tmp_path: Path,
) -> None:
    repository, deployment, artifacts, descriptor = _repository(tmp_path)
    source = json.loads((repository / SOURCE_TEMPLATE).read_bytes())
    rendered = json.loads(_render(repository, deployment, artifacts, descriptor))

    assert _reserved_concurrency_inventory(source) == SOURCE_RESERVED_CONCURRENCY
    assert _reserved_concurrency_inventory(rendered) == STAGED_RESERVED_CONCURRENCY

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

    source_resources = source["Resources"]
    rendered_resources = rendered["Resources"]
    assert len(_http_api_routes(source)) == 15
    assert _http_api_routes(rendered) == _http_api_routes(source)
    assert "DisableExecuteApiEndpoint" not in source_resources["SellerHttpApi"]["Properties"]
    assert (
        source_resources["SellerWebDistribution"]["Properties"]["DistributionConfig"]["Enabled"]
        is True
    )
    assert rendered_resources["SellerHttpApi"]["Properties"]["DisableExecuteApiEndpoint"] is True
    assert (
        rendered_resources["SellerWebDistribution"]["Properties"]["DistributionConfig"]["Enabled"]
        is False
    )

    metadata = rendered["Metadata"]["MrListerPhase6StagedDeployment"]
    assert metadata["DisabledTriggers"] == DISABLED_TRIGGER_METADATA
    assert metadata["DisabledExternalServing"] == DISABLED_EXTERNAL_SERVING_METADATA
    verify_phase6_sam_staged_inertness(rendered)


@pytest.mark.parametrize("mutation", ("one", "missing", "extra"))
def test_staged_reserved_concurrency_drift_fails_closed(
    tmp_path: Path,
    mutation: str,
) -> None:
    repository, deployment, artifacts, descriptor = _repository(tmp_path)
    rendered = json.loads(_render(repository, deployment, artifacts, descriptor))
    if mutation == "one":
        rendered["Resources"]["SourceVersionRetentionFunction"]["Properties"][  # type: ignore[index]
            "ReservedConcurrentExecutions"
        ] = 1
    elif mutation == "missing":
        rendered["Resources"]["StuckExecutionRecoveryFunction"]["Properties"].pop(  # type: ignore[index]
            "ReservedConcurrentExecutions"
        )
    else:
        rendered["Resources"]["DispatcherFunction"]["Properties"][  # type: ignore[index]
            "ReservedConcurrentExecutions"
        ] = 0

    with pytest.raises(Phase6SamStagingError):
        verify_phase6_sam_staged_inertness(rendered)


@pytest.mark.parametrize(
    ("logical_id", "event_name", "field", "active_value"), TRIGGER_STATE_LOCATIONS
)
@pytest.mark.parametrize("mutation", ("active", "missing"))
def test_every_full_staging_trigger_flip_or_missing_state_fails_closed(
    tmp_path: Path,
    logical_id: str,
    event_name: str | None,
    field: str,
    active_value: object,
    mutation: str,
) -> None:
    repository, deployment, artifacts, descriptor = _repository(tmp_path)
    rendered = json.loads(_render(repository, deployment, artifacts, descriptor))
    properties = _trigger_state_properties(rendered, logical_id, event_name)
    if mutation == "active":
        properties[field] = active_value
    else:
        properties.pop(field)

    with pytest.raises(Phase6SamStagingError):
        verify_phase6_sam_staged_inertness(rendered)


@pytest.mark.parametrize("mutation", ("missing", "extra"))
def test_missing_or_extra_full_staging_trigger_fails_closed(
    tmp_path: Path,
    mutation: str,
) -> None:
    repository, deployment, artifacts, descriptor = _repository(tmp_path)
    rendered = json.loads(_render(repository, deployment, artifacts, descriptor))
    events = rendered["Resources"]["DispatcherFunction"]["Properties"]["Events"]
    if mutation == "missing":
        events.pop("DueWorkSweep")
    else:
        events["UnreviewedSweep"] = {
            "Properties": {"Enabled": False, "Schedule": "rate(1 day)"},
            "Type": "Schedule",
        }

    with pytest.raises(Phase6SamStagingError):
        verify_phase6_sam_staged_inertness(rendered)


@pytest.mark.parametrize(
    ("logical_id", "path", "active_value"),
    (
        ("SellerHttpApi", ("DisableExecuteApiEndpoint",), False),
        ("SellerWebDistribution", ("DistributionConfig", "Enabled"), True),
    ),
)
@pytest.mark.parametrize("mutation", ("active", "missing"))
def test_each_external_serving_gate_flip_or_missing_field_fails_closed(
    tmp_path: Path,
    logical_id: str,
    path: tuple[str, ...],
    active_value: bool,
    mutation: str,
) -> None:
    repository, deployment, artifacts, descriptor = _repository(tmp_path)
    rendered = json.loads(_render(repository, deployment, artifacts, descriptor))
    properties = rendered["Resources"][logical_id]["Properties"]
    for component in path[:-1]:
        properties = properties[component]
    if mutation == "active":
        properties[path[-1]] = active_value
    else:
        properties.pop(path[-1])

    with pytest.raises(Phase6SamStagingError):
        verify_phase6_sam_staged_inertness(rendered)


def test_unreviewed_external_serving_resource_fails_closed(tmp_path: Path) -> None:
    repository, deployment, artifacts, descriptor = _repository(tmp_path)
    rendered = json.loads(_render(repository, deployment, artifacts, descriptor))
    rendered["Resources"]["UnreviewedHttpApi"] = {
        "Properties": {"DisableExecuteApiEndpoint": True},
        "Type": "AWS::Serverless::HttpApi",
    }

    with pytest.raises(Phase6SamStagingError):
        verify_phase6_sam_staged_inertness(rendered)


def test_render_binds_every_explicit_parameter_and_canonical_artifact_identity(
    tmp_path: Path,
) -> None:
    repository, deployment, artifacts, descriptor = _repository(tmp_path)
    first = _render(repository, deployment, artifacts, descriptor)
    second = _render(repository, deployment, artifacts, descriptor)
    assert first == second
    assert first.endswith(b"\n")
    assert render_manifest(json.loads(first)) == first

    document = json.loads(first)
    parameters = document["Parameters"]
    expected_parameters = {
        "AgentCoreRuntimeArn": RUNTIME_ARN,
        "AgentCoreRuntimeBindingFingerprint": RUNTIME_BINDING,
        "AgentCoreRuntimeEndpointArn": ENDPOINT_ARN,
        "AgentCoreRuntimeQualifier": QUALIFIER,
        "AgentCoreRuntimeVersion": VERSION,
        "ApplicationCertificateArn": (
            f"arn:aws:acm:us-east-1:{ACCOUNT}:certificate/12345678-1234-4abc-8def-1234567890ab"
        ),
        "ApplicationOrigin": "https://demo.example.com",
        "EnvironmentName": ENVIRONMENT,
        "PrintifySecretArn": (
            f"arn:aws:secretsmanager:{REGION}:{ACCOUNT}:secret:mr-lister/printify-demo-Ab12Cd"
        ),
        "ReleaseFingerprint": RELEASE,
    }
    for name, expected in expected_parameters.items():
        assert parameters[name]["Default"] == expected
        assert parameters[name]["AllowedValues"] == [expected]

    descriptor_raw = (artifacts / DEPLOYMENT_DESCRIPTOR_FILENAME).read_bytes()
    metadata = document["Metadata"]["MrListerPhase6StagedDeployment"]
    assert metadata == {
        "ArtifactDescriptorSha256": sha256(descriptor_raw).hexdigest(),
        "AgentCore": {
            "BindingFingerprint": RUNTIME_BINDING,
            "EndpointArn": ENDPOINT_ARN,
            "EndpointObservationSha256": sha256(
                _endpoint_observation(repository).read_bytes()
            ).hexdigest(),
            "Qualifier": QUALIFIER,
            "RuntimeArn": RUNTIME_ARN,
            "RuntimeCreateInputSha256": sha256(
                (repository / direct.RUNTIME_CREATE_OUTPUT).read_bytes()
            ).hexdigest(),
            "RuntimeEvidenceSha256": sha256(
                _runtime_v1_evidence(repository).read_bytes()
            ).hexdigest(),
            "RuntimeRenderManifestSha256": sha256(
                (repository / direct.RUNTIME_MANIFEST_OUTPUT).read_bytes()
            ).hexdigest(),
            "Status": "READY",
            "Version": VERSION,
        },
        "AgentCoreArtifact": {
            "Bucket": LAMBDA_BUCKET,
            "ChecksumSHA256Base64": AGENTCORE_EXPECTATION.checksum_sha256_base64,
            "Key": AGENTCORE_EXPECTATION.key,
            "ObjectEvidenceSha256": AGENTCORE_OBJECT_EVIDENCE_SHA256,
            "Sha256": AGENTCORE_SHA256,
            "SizeBytes": len(AGENTCORE_BYTES),
            "Version": AGENTCORE_VERSION,
        },
        "DisabledExternalServing": DISABLED_EXTERNAL_SERVING_METADATA,
        "DisabledTriggers": DISABLED_TRIGGER_METADATA,
        "Format": "mr-lister-phase6-sam-staged-v1",
        "LambdaArtifact": {
            "Bucket": LAMBDA_BUCKET,
            "ChecksumSHA256Base64": LAMBDA_EXPECTATION.checksum_sha256_base64,
            "Key": LAMBDA_KEY,
            "ObjectEvidenceSha256": OBJECT_EVIDENCE_SHA256,
            "Sha256": LAMBDA_SHA256,
            "SizeBytes": len(LAMBDA_BYTES),
            "Version": LAMBDA_VERSION,
        },
        "Mode": "STAGED_FAIL_CLOSED",
        "ReleaseFingerprint": RELEASE,
        "SourceTemplateSha256": sha256((ROOT / SOURCE_TEMPLATE).read_bytes()).hexdigest(),
        "StateMachineDefinitions": {
            logical_id: {"Path": path.as_posix(), "Sha256": fingerprint}
            for logical_id, (path, fingerprint) in STATE_MACHINE_IDENTITIES.items()
        },
        "Target": {
            "AccountId": ACCOUNT,
            "Environment": ENVIRONMENT,
            "Region": REGION,
        },
    }


@pytest.mark.parametrize(
    "overrides",
    [
        {"account_id": "000000000000"},
        {"account_id": "123"},
        {"region": "us-east-1"},
        {"environment": "DEFAULT"},
        {"release_fingerprint": "0" * 64},
        {"release_fingerprint": "A" * 64},
        {"agentcore_runtime_qualifier": "DEFAULT"},
        {"agentcore_runtime_binding_fingerprint": "d" * 64},
        {"lambda_artifact_bucket": "Moving.Bucket"},
        {"lambda_artifact_bucket": "valid-but-wrong-foundation-bucket"},
        {"lambda_artifact_key": "phase6/latest/phase6-lambda.zip"},
        {"lambda_artifact_key": ""},
        {"lambda_artifact_version": "latest"},
        {"lambda_artifact_version": "null"},
        {"application_origin": "https://*.example.com"},
        {"application_origin": " https://demo.example.com"},
        {
            "printify_secret_arn": (
                "arn:aws:secretsmanager:us-west-2:999999999999:"
                "secret:mr-lister/printify-demo-Ab12Cd"
            )
        },
        {
            "application_certificate_arn": (
                f"arn:aws:acm:us-west-2:{ACCOUNT}:certificate/12345678-1234-4abc-8def-1234567890ab"
            )
        },
    ],
)
def test_binding_rejects_blank_moving_cross_authority_or_placeholder_inputs(
    overrides: dict[str, object],
) -> None:
    with pytest.raises(Phase6SamStagingError) as captured:
        _binding(**overrides)

    assert str(captured.value) == "Phase 6 SAM staged deployment configuration is invalid"
    assert all(not value or str(value) not in str(captured.value) for value in overrides.values())


def test_content_addressed_lambda_key_must_match_verified_archive(tmp_path: Path) -> None:
    repository, deployment, artifacts, descriptor = _repository(tmp_path)
    binding = _binding(
        lambda_artifact_key=(
            f"phase6/releases/{RELEASE}/lambda/{'d' * 64}/{LAMBDA_ARCHIVE_FILENAME}"
        )
    )
    with (
        patch(
            "tools.render_phase6_sam_activation.verify_phase6_deployment_artifacts",
            return_value=descriptor,
        ),
        patch(
            "tools.render_phase6_sam_activation.verify_phase6_s3_release_object_evidence",
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
        pytest.raises(Phase6SamStagingError),
    ):
        render_phase6_sam_staged_template(
            binding,
            **_agentcore_evidence_arguments(repository),
            lambda_object_evidence_path=_lambda_object_evidence(repository),
            repository_root=repository,
            deployment_root=deployment,
            artifact_root=artifacts,
        )


def test_checked_scaffold_or_any_verified_artifact_drift_fails_closed(tmp_path: Path) -> None:
    repository, deployment, artifacts, descriptor = _repository(tmp_path)
    source = repository / SOURCE_TEMPLATE
    source.write_text(source.read_text(encoding="utf-8").replace("SCAFFOLD_ONLY", "DRIFTED", 1))
    with (
        patch(
            "tools.render_phase6_sam_activation.verify_phase6_deployment_artifacts",
            return_value=descriptor,
        ),
        pytest.raises(Phase6SamStagingError),
    ):
        render_phase6_sam_staged_template(
            _binding(),
            **_agentcore_evidence_arguments(repository),
            lambda_object_evidence_path=_lambda_object_evidence(repository),
            repository_root=repository,
            deployment_root=deployment,
            artifact_root=artifacts,
        )

    shutil.copyfile(ROOT / SOURCE_TEMPLATE, source)
    (artifacts / LAMBDA_ARCHIVE_FILENAME).write_bytes(LAMBDA_BYTES + b"drift")
    with (
        patch(
            "tools.render_phase6_sam_activation.verify_phase6_deployment_artifacts",
            return_value=descriptor,
        ),
        pytest.raises(Phase6SamStagingError),
    ):
        render_phase6_sam_staged_template(
            _binding(),
            **_agentcore_evidence_arguments(repository),
            lambda_object_evidence_path=_lambda_object_evidence(repository),
            repository_root=repository,
            deployment_root=deployment,
            artifact_root=artifacts,
        )


@pytest.mark.parametrize(
    "logical_id",
    sorted(STATE_MACHINE_IDENTITIES),
)
def test_any_checked_asl_byte_drift_fails_before_artifact_verification(
    tmp_path: Path,
    logical_id: str,
) -> None:
    repository, deployment, artifacts, descriptor = _repository(tmp_path)
    path, _fingerprint = STATE_MACHINE_IDENTITIES[logical_id]
    target = repository / path
    target.write_bytes(target.read_bytes() + b"\n")

    with (
        patch(
            "tools.render_phase6_sam_activation.verify_phase6_deployment_artifacts",
            return_value=descriptor,
        ) as verifier,
        pytest.raises(Phase6SamStagingError),
    ):
        render_phase6_sam_staged_template(
            _binding(),
            **_agentcore_evidence_arguments(repository),
            lambda_object_evidence_path=_lambda_object_evidence(repository),
            repository_root=repository,
            deployment_root=deployment,
            artifact_root=artifacts,
        )
    verifier.assert_not_called()


def test_asl_symlink_is_rejected_even_when_it_targets_exact_checked_bytes(tmp_path: Path) -> None:
    repository, deployment, artifacts, descriptor = _repository(tmp_path)
    path, _fingerprint = STATE_MACHINE_IDENTITIES["PrepareStateMachine"]
    target = repository / path
    target.unlink()
    target.symlink_to(ROOT / path)

    with (
        patch(
            "tools.render_phase6_sam_activation.verify_phase6_deployment_artifacts",
            return_value=descriptor,
        ) as verifier,
        pytest.raises(Phase6SamStagingError),
    ):
        render_phase6_sam_staged_template(
            _binding(),
            **_agentcore_evidence_arguments(repository),
            lambda_object_evidence_path=_lambda_object_evidence(repository),
            repository_root=repository,
            deployment_root=deployment,
            artifact_root=artifacts,
        )
    verifier.assert_not_called()


def test_hash_matching_but_schema_invalid_asl_is_still_rejected(tmp_path: Path) -> None:
    repository, deployment, artifacts, descriptor = _repository(tmp_path)
    logical_id = "PrepareStateMachine"
    path, _fingerprint = STATE_MACHINE_IDENTITIES[logical_id]
    target = repository / path
    definition = json.loads(target.read_bytes())
    definition["States"] = []
    raw = render_manifest(definition)
    target.write_bytes(raw)
    authority = sam_renderer._STATE_MACHINE_AUTHORITIES[logical_id]
    changed_authority = replace(authority, sha256=sha256(raw).hexdigest())

    with (
        patch.dict(
            sam_renderer._STATE_MACHINE_AUTHORITIES,
            {logical_id: changed_authority},
        ),
        patch(
            "tools.render_phase6_sam_activation.verify_phase6_deployment_artifacts",
            return_value=descriptor,
        ) as verifier,
        pytest.raises(Phase6SamStagingError),
    ):
        render_phase6_sam_staged_template(
            _binding(),
            **_agentcore_evidence_arguments(repository),
            lambda_object_evidence_path=_lambda_object_evidence(repository),
            repository_root=repository,
            deployment_root=deployment,
            artifact_root=artifacts,
        )
    verifier.assert_not_called()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("account_id", "999999999999"),
        ("region", "us-east-1"),
        ("environment", "prod"),
        ("component", "agentcore"),
        ("release_fingerprint", "d" * 64),
        ("archive_sha256", "d" * 64),
        ("size_bytes", len(LAMBDA_BYTES) + 1),
        ("checksum_sha256_base64", "remote-bytes-do-not-match"),
        ("bucket", "different-bucket"),
        ("key", "private/deployments/lambda/releases/moving.zip"),
        ("version_id", "different-version-id"),
        ("evidence_sha256", "0" * 64),
    ],
)
def test_every_returned_remote_identity_byte_and_evidence_field_is_cross_checked(
    tmp_path: Path,
    field: str,
    value: object,
) -> None:
    repository, deployment, artifacts, descriptor = _repository(tmp_path)
    returned = asdict(_verified_lambda_object())
    returned[field] = value

    with (
        patch(
            "tools.render_phase6_sam_activation.verify_phase6_deployment_artifacts",
            return_value=descriptor,
        ),
        patch(
            "tools.render_phase6_sam_activation.verify_phase6_s3_release_object_evidence",
            return_value=SimpleNamespace(**returned),
        ),
        pytest.raises(Phase6SamStagingError),
    ):
        render_phase6_sam_staged_template(
            _binding(),
            **_agentcore_evidence_arguments(repository),
            lambda_object_evidence_path=_lambda_object_evidence(repository),
            repository_root=repository,
            deployment_root=deployment,
            artifact_root=artifacts,
        )


def test_invalid_remote_object_evidence_cannot_be_replaced_by_a_bare_version_id(
    tmp_path: Path,
) -> None:
    repository, deployment, artifacts, descriptor = _repository(tmp_path)
    with (
        patch(
            "tools.render_phase6_sam_activation.verify_phase6_deployment_artifacts",
            return_value=descriptor,
        ),
        pytest.raises(Phase6SamStagingError) as captured,
    ):
        render_phase6_sam_staged_template(
            _binding(),
            **_agentcore_evidence_arguments(repository),
            lambda_object_evidence_path=_lambda_object_evidence(repository),
            repository_root=repository,
            deployment_root=deployment,
            artifact_root=artifacts,
        )

    assert str(captured.value) == "Phase 6 SAM staged deployment configuration is invalid"


@pytest.mark.parametrize(
    "mutation",
    (
        "runtime_s3_version",
        "runtime_role",
        "runtime_tags",
        "endpoint_status",
        "endpoint_target_version",
    ),
)
def test_full_staging_rejects_substituted_runtime_tag_or_ready_endpoint_evidence(
    tmp_path: Path,
    mutation: str,
) -> None:
    repository, deployment, artifacts, descriptor = _repository(tmp_path)
    if mutation.startswith("endpoint_"):
        path = _endpoint_observation(repository)
        document = json.loads(path.read_bytes())
        if mutation == "endpoint_status":
            document["status"] = "UPDATING"
        else:
            document["targetVersion"] = "2"
    else:
        path = _runtime_v1_evidence(repository)
        document = json.loads(path.read_bytes())
        get_response = document["getAgentRuntime"]["response"]
        if mutation == "runtime_s3_version":
            get_response["agentRuntimeArtifact"]["codeConfiguration"]["code"]["s3"]["versionId"] = (
                "substituted-version"
            )
        elif mutation == "runtime_role":
            get_response["roleArn"] = f"arn:aws:iam::{ACCOUNT}:role/substituted"
        else:
            document["listTagsForResource"]["response"]["tags"] = {
                **_direct_binding().tags,
                "ReleaseFingerprint": "f" * 64,
            }
    path.write_bytes(render_manifest(document))

    with pytest.raises(Phase6SamStagingError):
        _render(repository, deployment, artifacts, descriptor)


def test_full_staging_requires_mmdsv2_runtime_evidence(tmp_path: Path) -> None:
    repository, deployment, artifacts, descriptor = _repository(tmp_path)
    path = _runtime_v1_evidence(repository)
    document = json.loads(path.read_bytes())
    document["getAgentRuntime"]["response"].pop("metadataConfiguration")
    path.write_bytes(render_manifest(document))

    with pytest.raises(Phase6SamStagingError):
        _render(repository, deployment, artifacts, descriptor)


def test_full_staging_rejects_agentcore_object_substitution_before_runtime_binding(
    tmp_path: Path,
) -> None:
    repository, deployment, artifacts, descriptor = _repository(tmp_path)
    with (
        patch(
            "tools.render_phase6_sam_activation.verify_phase6_deployment_artifacts",
            return_value=descriptor,
        ),
        patch(
            "tools.render_phase6_sam_activation.verify_phase6_s3_release_object_evidence",
            side_effect=lambda expectation, **_kwargs: (
                _verified_lambda_object()
                if expectation.component == "lambda"
                else _verified_agentcore_object(version_id="substituted-version")
            ),
        ),
        patch(
            "tools.render_phase6_agentcore_direct_codezip._existing_phase6_environment",
            return_value=TEST_RUNTIME_ENVIRONMENT,
        ),
        pytest.raises(Phase6SamStagingError),
    ):
        render_phase6_sam_staged_template(
            _binding(),
            **_agentcore_evidence_arguments(repository),
            lambda_object_evidence_path=_lambda_object_evidence(repository),
            repository_root=repository,
            deployment_root=deployment,
            artifact_root=artifacts,
        )


def test_evidence_fingerprint_drift_invalidates_previously_rendered_output(
    tmp_path: Path,
) -> None:
    repository, deployment, artifacts, descriptor = _repository(tmp_path)
    options = {
        **_agentcore_evidence_arguments(repository),
        "lambda_object_evidence_path": _lambda_object_evidence(repository),
        "repository_root": repository,
        "deployment_root": deployment,
        "artifact_root": artifacts,
    }
    with (
        patch(
            "tools.render_phase6_sam_activation.verify_phase6_deployment_artifacts",
            return_value=descriptor,
        ),
        patch(
            "tools.render_phase6_sam_activation.verify_phase6_s3_release_object_evidence",
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
        write_phase6_sam_staged_template(_binding(), **options)

    with (
        patch(
            "tools.render_phase6_sam_activation.verify_phase6_deployment_artifacts",
            return_value=descriptor,
        ),
        patch(
            "tools.render_phase6_sam_activation.verify_phase6_s3_release_object_evidence",
            side_effect=lambda expectation, **_kwargs: (
                _verified_lambda_object(evidence_sha256="d" * 64)
                if expectation.component == "lambda"
                else _verified_agentcore_object()
            ),
        ),
        patch(
            "tools.render_phase6_agentcore_direct_codezip._existing_phase6_environment",
            return_value=TEST_RUNTIME_ENVIRONMENT,
        ),
        pytest.raises(Phase6SamStagingError),
    ):
        verify_rendered_phase6_sam_staged_template(_binding(), **options)


def test_cli_requires_agentcore_evidence_even_with_literal_runtime_fields(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    arguments = [
        "render_phase6_sam_activation.py",
        "--account-id",
        ACCOUNT,
        "--region",
        REGION,
        "--environment",
        ENVIRONMENT,
        "--release-fingerprint",
        RELEASE,
        "--agentcore-runtime-arn",
        RUNTIME_ARN,
        "--agentcore-runtime-endpoint-arn",
        ENDPOINT_ARN,
        "--agentcore-runtime-version",
        VERSION,
        "--agentcore-runtime-qualifier",
        QUALIFIER,
        "--agentcore-runtime-binding-fingerprint",
        RUNTIME_BINDING,
        "--printify-secret-arn",
        f"arn:aws:secretsmanager:{REGION}:{ACCOUNT}:secret:mr-lister/printify-demo-Ab12Cd",
        "--application-origin",
        "https://demo.example.com",
        "--application-certificate-arn",
        f"arn:aws:acm:us-east-1:{ACCOUNT}:certificate/12345678-1234-4abc-8def-1234567890ab",
        "--lambda-artifact-bucket",
        LAMBDA_BUCKET,
        "--lambda-artifact-key",
        LAMBDA_KEY,
        "--lambda-artifact-version",
        LAMBDA_VERSION,
        "--write-staged",
    ]
    monkeypatch.setattr("sys.argv", arguments)

    with pytest.raises(SystemExit) as captured:
        sam_renderer.main()

    assert captured.value.code == 2
    assert "--agentcore-endpoint-observation" in capsys.readouterr().err


def test_write_refuses_preexisting_output_and_verify_rejects_byte_drift(tmp_path: Path) -> None:
    repository, deployment, artifacts, descriptor = _repository(tmp_path)
    with (
        patch(
            "tools.render_phase6_sam_activation.verify_phase6_deployment_artifacts",
            return_value=descriptor,
        ),
        patch(
            "tools.render_phase6_sam_activation.verify_phase6_s3_release_object_evidence",
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
        output = write_phase6_sam_staged_template(
            _binding(),
            **_agentcore_evidence_arguments(repository),
            lambda_object_evidence_path=_lambda_object_evidence(repository),
            repository_root=repository,
            deployment_root=deployment,
            artifact_root=artifacts,
        )
        assert output == repository / STAGED_TEMPLATE_OUTPUT
        assert output.is_file()
        assert output.stat().st_mode & 0o777 == 0o600
        verify_rendered_phase6_sam_staged_template(
            _binding(),
            **_agentcore_evidence_arguments(repository),
            lambda_object_evidence_path=_lambda_object_evidence(repository),
            repository_root=repository,
            deployment_root=deployment,
            artifact_root=artifacts,
        )

        with pytest.raises(Phase6SamStagingError):
            write_phase6_sam_staged_template(
                _binding(),
                **_agentcore_evidence_arguments(repository),
                lambda_object_evidence_path=_lambda_object_evidence(repository),
                repository_root=repository,
                deployment_root=deployment,
                artifact_root=artifacts,
            )

        output.write_bytes(output.read_bytes() + b"drift")
        with pytest.raises(Phase6SamStagingError):
            verify_rendered_phase6_sam_staged_template(
                _binding(),
                **_agentcore_evidence_arguments(repository),
                lambda_object_evidence_path=_lambda_object_evidence(repository),
                repository_root=repository,
                deployment_root=deployment,
                artifact_root=artifacts,
            )


def test_unverified_release_never_writes_and_errors_are_value_free(tmp_path: Path) -> None:
    repository, deployment, artifacts, _descriptor_value = _repository(tmp_path)
    private_failure = "private archive verification detail"
    with (
        patch(
            "tools.render_phase6_sam_activation.verify_phase6_deployment_artifacts",
            side_effect=ValueError(private_failure),
        ),
        pytest.raises(Phase6SamStagingError) as captured,
    ):
        write_phase6_sam_staged_template(
            _binding(),
            **_agentcore_evidence_arguments(repository),
            lambda_object_evidence_path=_lambda_object_evidence(repository),
            repository_root=repository,
            deployment_root=deployment,
            artifact_root=artifacts,
        )

    assert str(captured.value) == "Phase 6 SAM staged deployment configuration is invalid"
    assert private_failure not in str(captured.value)
    assert not (repository / STAGED_TEMPLATE_OUTPUT).exists()


def test_rendered_full_template_passes_sam_lint_when_cli_is_available(tmp_path: Path) -> None:
    sam = shutil.which("sam")
    if sam is None:
        pytest.skip("AWS SAM CLI is not available")
    repository, deployment, artifacts, descriptor = _repository(tmp_path)
    template = tmp_path / "full-release-bound-staged.json"
    template.write_bytes(_render(repository, deployment, artifacts, descriptor))

    completed = subprocess.run(
        [sam, "validate", "--lint", "--region", REGION, "--template-file", str(template)],
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr


def test_activation_is_explicitly_unavailable_without_separate_live_evidence() -> None:
    with pytest.raises(Phase6SamStagingError) as captured:
        reject_phase6_sam_activation()

    assert str(captured.value) == (
        "Phase 6 SAM activation requires a separate verified staged-deployment evidence gate"
    )
    assert STAGED_TEMPLATE_OUTPUT.parts[0] == ".mr_lister_private"
