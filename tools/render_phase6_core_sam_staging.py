"""Render the domain-independent Phase 6 core-runtime SAM staging template.

The renderer is deliberately local-only and fail-closed.  It selects the durable foundation,
seven non-web Lambda functions, four Step Functions workflows, and their exact supporting
resources from the checked full Phase 6 SAM authority.  It never builds, packages, uploads,
contacts AWS, or enables runtime execution.

The resulting template keeps ``MR_LISTER_PHASE6_SCAFFOLD_ONLY=true`` and disables the exact closed
set of four SAM schedule/stream events plus the standalone recovery rule.  It may be used only to
review and stage the backend slice; runtime-trigger activation and the seller web surface remain
separate later gates.
"""

from __future__ import annotations

import argparse
import base64
import json
import re
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path, PurePosixPath
from typing import NoReturn, cast

from mr_lister.agent.runtime_binding import (
    AgentCoreRuntimeBinding,
    load_agentcore_runtime_binding,
)
from mr_lister.release.phase6 import LINUX_ARM64_TARGET, render_manifest
from tools.build_phase66_source_bundles import (
    AGENTCORE_ARCHIVE_FILENAME,
    DEPLOYMENT_DESCRIPTOR_FILENAME,
    LAMBDA_ARCHIVE_FILENAME,
    verify_phase6_deployment_artifacts,
)
from tools.render_phase6_agentcore_direct_codezip import (
    Phase6AgentCoreDirectCodeZipBinding,
    VerifiedAgentCoreArchive,
    VerifiedAgentCoreRuntimeV1,
    verify_phase6_agentcore_runtime_v1_evidence,
)
from tools.verify_phase6_agentcore_endpoint_observation import (
    verify_phase6_agentcore_endpoint_observation,
)
from tools.verify_phase6_s3_release_object import (
    Phase6S3ReleaseObjectExpectation,
    VerifiedPhase6S3ReleaseObject,
    validate_phase6_s3_version_id,
    verify_phase6_s3_release_object_evidence,
)

ROOT = Path(__file__).resolve().parents[1]
SOURCE_TEMPLATE = Path("infra/phase6/template.json")
CORE_STAGED_TEMPLATE_OUTPUT = Path(
    ".mr_lister_private/phase6-core-sam/template.core-release-bound-staged.local.json"
)
DEFAULT_DEPLOYMENT_ROOT = ROOT / ".mr_lister_private/phase6-deployment"
DEFAULT_ARTIFACT_ROOT = ROOT / ".mr_lister_private/phase6-artifacts"

_SOURCE_TEMPLATE_SHA256 = "9a110b3e813ed23102033ace67341d9cb4015274d7acc9f0fff6c08439c57ed7"
_FOUNDATION_TEMPLATE_FINGERPRINT = (
    "689897c254c9db97aa75d508f140980f9b6a5129c0c1fa0121eb8d6ef1e64874"
)
_FOUNDATION_BINDING_FORMAT = "mr-lister-phase6-foundation-deployment-v1"
_REGION = "us-west-2"
_ENVIRONMENT = "dev"
_GENERIC_ERROR = "Phase 6 core-runtime SAM staging configuration is invalid"
_ACTIVATION_ERROR = "Phase 6 core-runtime staging cannot activate runtime execution or web traffic"

_ACCOUNT_ID = re.compile(r"^[0-9]{12}$")
_FINGERPRINT = re.compile(r"^[a-f0-9]{64}$")
_S3_BUCKET = re.compile(r"^[a-z0-9][a-z0-9-]{1,61}[a-z0-9]$")
_ORIGIN = re.compile(r"^https://[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?$")
_STACK_UUID = re.compile(r"^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$")
_PLACEHOLDER = re.compile(
    r"<[A-Z][A-Z0-9_]*>|__[A-Z][A-Z0-9_]*__|"
    r"\b(?:PLACEHOLDER|REPLACE_ME|CHANGEME)\b",
    re.IGNORECASE,
)
_MOVING_VALUES = frozenset({"current", "default", "latest", "mutable", "null", "unversioned"})
_SUB_TOKEN = re.compile(r"\$\{([^}]+)\}")

_PARAMETER_FIELDS = {
    "AgentCoreRuntimeArn": "agentcore_runtime_arn",
    "AgentCoreRuntimeBindingFingerprint": "agentcore_runtime_binding_fingerprint",
    "AgentCoreRuntimeEndpointArn": "agentcore_runtime_endpoint_arn",
    "AgentCoreRuntimeQualifier": "agentcore_runtime_qualifier",
    "AgentCoreRuntimeVersion": "agentcore_runtime_version",
    "ApplicationOrigin": "application_origin",
    "EnvironmentName": "environment",
    "PrintifySecretArn": "printify_secret_arn",
    "ReleaseFingerprint": "release_fingerprint",
}

_FUNCTION_HANDLERS = {
    "DispatcherFunction": "phase6_lambda.dispatcher_handler",
    "PreparationDispatchFunction": "phase6_lambda.preparation_dispatch_handler",
    "ProviderDraftFunction": "phase6_lambda.provider_draft_handler",
    "SettlementFunction": "phase6_lambda.settlement_handler",
    "SourceVersionRetentionFunction": "phase6_lambda.source_version_retention_handler",
    "StuckExecutionRecoveryFunction": "phase6_lambda.stuck_execution_recovery_handler",
    "TerminalOperationalCleanupFunction": "phase6_lambda.terminal_operational_cleanup_handler",
}

_STATE_MACHINE_AUTHORITIES = {
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

_RESOURCE_TYPES = {
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

_OUTPUTS = frozenset(
    {
        "ArtifactBucketName",
        "DeploymentReadiness",
        "PrepareStateMachineArn",
        "ReconcileProductStateMachineArn",
        "RefreshEconomicsStateMachineArn",
        "StateTableName",
        "SynchronizeProductStateMachineArn",
    }
)
_FOUNDATION_RESOURCES = frozenset(
    {"OperationalStateTable", "PrivateArtifactBucket", "PrivateArtifactBucketPolicy"}
)
_DISABLED_SAM_TRIGGER_SPECS = (
    ("DispatcherFunction", "DueWorkSweep", "Schedule"),
    ("DispatcherFunction", "OperationalStateChanges", "DynamoDB"),
    ("SourceVersionRetentionFunction", "SourceVersionRetentionSweep", "Schedule"),
    (
        "TerminalOperationalCleanupFunction",
        "TerminalOperationalCleanupSweep",
        "Schedule",
    ),
)
_DISABLED_EVENT_RULE_SPECS = ("StuckExecutionRecoveryScheduleRule",)
_PSEUDO_PARAMETERS = frozenset(
    {
        "AWS::AccountId",
        "AWS::NoValue",
        "AWS::NotificationARNs",
        "AWS::Partition",
        "AWS::Region",
        "AWS::StackId",
        "AWS::StackName",
        "AWS::URLSuffix",
    }
)


class Phase6CoreSamStagingError(RuntimeError):
    """A value-free failure for drifting or mutable core staging input."""


@dataclass(frozen=True, slots=True)
class Phase6CoreSamStagingBinding:
    """Exact local identities used to bind one core-runtime staging template."""

    account_id: str
    region: str
    environment: str
    foundation_stack_id: str
    release_fingerprint: str
    agentcore_runtime_arn: str
    agentcore_runtime_endpoint_arn: str
    agentcore_runtime_version: str
    agentcore_runtime_qualifier: str
    agentcore_runtime_binding_fingerprint: str
    printify_secret_arn: str
    application_origin: str
    lambda_artifact_bucket: str
    lambda_artifact_key: str
    lambda_artifact_version: str

    def __post_init__(self) -> None:
        try:
            values = tuple(getattr(self, field) for field in self.__dataclass_fields__)
            if any(not _is_exact_input(value) for value in values):
                raise ValueError
            expected_bucket = (
                f"mr-lister-phase6-artifacts-{_ENVIRONMENT}-{self.account_id}-{_REGION}"
            )
            expected_stack_prefix = (
                f"arn:aws:cloudformation:{_REGION}:{self.account_id}:"
                f"stack/mr-lister-phase6-{_ENVIRONMENT}/"
            )
            stack_uuid = self.foundation_stack_id.removeprefix(expected_stack_prefix)
            if (
                _ACCOUNT_ID.fullmatch(self.account_id) is None
                or self.account_id == "0" * 12
                or self.region != _REGION
                or self.environment != _ENVIRONMENT
                or not self.foundation_stack_id.startswith(expected_stack_prefix)
                or _STACK_UUID.fullmatch(stack_uuid) is None
                or _FINGERPRINT.fullmatch(self.release_fingerprint) is None
                or self.release_fingerprint == "0" * 64
                or not _valid_s3_bucket(self.lambda_artifact_bucket)
                or self.lambda_artifact_bucket != expected_bucket
                or not _valid_s3_key(self.lambda_artifact_key)
                or self.lambda_artifact_version.casefold() in _MOVING_VALUES
                or _ORIGIN.fullmatch(self.application_origin) is None
                or ".." in self.application_origin.removeprefix("https://")
            ):
                raise ValueError
            validate_phase6_s3_version_id(self.lambda_artifact_version)

            runtime = _runtime_binding(self)
            if not self.agentcore_runtime_endpoint_arn.endswith(
                f"/runtime-endpoint/{runtime.qualifier}"
            ):
                raise ValueError

            secret_prefix = (
                f"arn:aws:secretsmanager:{self.region}:{self.account_id}:secret:mr-lister/"
            )
            suffix = self.printify_secret_arn.removeprefix(secret_prefix)
            if (
                not self.printify_secret_arn.startswith(secret_prefix)
                or re.fullmatch(r"[A-Za-z0-9/_-]+-[A-Za-z0-9]{6}", suffix) is None
            ):
                raise ValueError
        except Exception:
            raise Phase6CoreSamStagingError(_GENERIC_ERROR) from None


@dataclass(frozen=True, slots=True)
class _ArtifactSet:
    descriptor_sha256: str
    agentcore_archive: VerifiedAgentCoreArchive
    agentcore_object: VerifiedPhase6S3ReleaseObject
    lambda_archive_sha256: str
    lambda_archive_size_bytes: int
    lambda_object: VerifiedPhase6S3ReleaseObject


@dataclass(frozen=True, slots=True)
class _FoundationBinding:
    sha256: str
    document: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class _EndpointEvidence:
    sha256: str
    document: Mapping[str, object]


def render_phase6_core_sam_staged_template(
    binding: Phase6CoreSamStagingBinding,
    *,
    foundation_binding_path: Path,
    agentcore_endpoint_observation_path: Path,
    agentcore_object_evidence_path: Path,
    agentcore_runtime_v1_evidence_path: Path,
    lambda_object_evidence_path: Path,
    repository_root: Path = ROOT,
    deployment_root: Path = DEFAULT_DEPLOYMENT_ROOT,
    artifact_root: Path = DEFAULT_ARTIFACT_ROOT,
) -> bytes:
    """Return deterministic canonical bytes for the fail-closed backend staging slice."""

    try:
        if not isinstance(binding, Phase6CoreSamStagingBinding):
            raise ValueError
        repository = repository_root.resolve(strict=True)
        source = _load_source_template(repository)
        definitions = _load_state_machine_definitions(repository, source)
        foundation = _load_foundation_binding(foundation_binding_path, binding)
        artifacts = _verify_artifacts(
            binding,
            deployment_root=deployment_root,
            artifact_root=artifact_root,
            agentcore_object_evidence_path=agentcore_object_evidence_path,
            lambda_object_evidence_path=lambda_object_evidence_path,
        )
        runtime = _load_ready_runtime_v1_evidence(
            agentcore_runtime_v1_evidence_path,
            binding,
            artifacts,
            repository,
        )
        endpoint = _load_ready_endpoint_observation(
            agentcore_endpoint_observation_path,
            binding,
            runtime,
        )
        document = _render_document(
            binding,
            source,
            definitions,
            foundation,
            runtime,
            endpoint,
            artifacts,
        )
        _validate_rendered_document(
            binding,
            document,
            source,
            definitions,
            foundation,
            runtime,
            endpoint,
            artifacts,
        )
        return _canonical_json(document)
    except Phase6CoreSamStagingError:
        raise
    except Exception:
        raise Phase6CoreSamStagingError(_GENERIC_ERROR) from None


def write_phase6_core_sam_staged_template(
    binding: Phase6CoreSamStagingBinding,
    *,
    foundation_binding_path: Path,
    agentcore_endpoint_observation_path: Path,
    agentcore_object_evidence_path: Path,
    agentcore_runtime_v1_evidence_path: Path,
    lambda_object_evidence_path: Path,
    repository_root: Path = ROOT,
    deployment_root: Path = DEFAULT_DEPLOYMENT_ROOT,
    artifact_root: Path = DEFAULT_ARTIFACT_ROOT,
) -> Path:
    """Create the one fixed ignored output, refusing every preexisting destination."""

    try:
        repository = repository_root.resolve(strict=True)
        destination = _output_destination(repository)
        if destination.exists() or destination.is_symlink():
            raise ValueError
        raw = render_phase6_core_sam_staged_template(
            binding,
            foundation_binding_path=foundation_binding_path,
            agentcore_endpoint_observation_path=agentcore_endpoint_observation_path,
            agentcore_object_evidence_path=agentcore_object_evidence_path,
            agentcore_runtime_v1_evidence_path=agentcore_runtime_v1_evidence_path,
            lambda_object_evidence_path=lambda_object_evidence_path,
            repository_root=repository,
            deployment_root=deployment_root,
            artifact_root=artifact_root,
        )
        _prepare_private_parent(repository, destination.parent)
        with destination.open("xb") as stream:
            stream.write(raw)
        destination.chmod(0o600)
        return destination
    except Phase6CoreSamStagingError:
        raise
    except Exception:
        raise Phase6CoreSamStagingError(_GENERIC_ERROR) from None


def verify_rendered_phase6_core_sam_staged_template(
    binding: Phase6CoreSamStagingBinding,
    *,
    foundation_binding_path: Path,
    agentcore_endpoint_observation_path: Path,
    agentcore_object_evidence_path: Path,
    agentcore_runtime_v1_evidence_path: Path,
    lambda_object_evidence_path: Path,
    repository_root: Path = ROOT,
    deployment_root: Path = DEFAULT_DEPLOYMENT_ROOT,
    artifact_root: Path = DEFAULT_ARTIFACT_ROOT,
) -> None:
    """Reject a missing or byte-drifted fixed output and any authority drift."""

    try:
        repository = repository_root.resolve(strict=True)
        destination = _output_destination(repository)
        expected = render_phase6_core_sam_staged_template(
            binding,
            foundation_binding_path=foundation_binding_path,
            agentcore_endpoint_observation_path=agentcore_endpoint_observation_path,
            agentcore_object_evidence_path=agentcore_object_evidence_path,
            agentcore_runtime_v1_evidence_path=agentcore_runtime_v1_evidence_path,
            lambda_object_evidence_path=lambda_object_evidence_path,
            repository_root=repository,
            deployment_root=deployment_root,
            artifact_root=artifact_root,
        )
        if (
            destination.is_symlink()
            or not destination.is_file()
            or destination.read_bytes() != expected
        ):
            raise ValueError
    except Phase6CoreSamStagingError:
        raise
    except Exception:
        raise Phase6CoreSamStagingError(_GENERIC_ERROR) from None


def verify_core_runtime_dependency_closure(document: Mapping[str, object]) -> None:
    """Require every intrinsic resource reference to remain inside the hardcoded slice."""

    try:
        resources = document.get("Resources")
        parameters = document.get("Parameters")
        if not isinstance(resources, Mapping) or not isinstance(parameters, Mapping):
            raise ValueError
        if {
            name: cast(Mapping[str, object], resource).get("Type")
            for name, resource in resources.items()
            if isinstance(name, str) and isinstance(resource, Mapping)
        } != _RESOURCE_TYPES:
            raise ValueError
        if set(parameters) != set(_PARAMETER_FIELDS):
            raise ValueError
        references = _intrinsic_references(document)
        allowed = set(resources) | set(parameters) | set(_PSEUDO_PARAMETERS)
        if references - allowed:
            raise ValueError
        if references & set(parameters) != set(_PARAMETER_FIELDS):
            raise ValueError
    except Exception:
        raise Phase6CoreSamStagingError(_GENERIC_ERROR) from None


def verify_phase6_core_sam_staged_inertness(document: Mapping[str, object]) -> None:
    """Require the exact closed set of staged backend triggers to be disabled."""

    try:
        _require_exact_disabled_triggers(document)
    except Exception:
        raise Phase6CoreSamStagingError(_GENERIC_ERROR) from None


def reject_phase6_core_sam_activation() -> NoReturn:
    """Fail closed: this segment is neither runtime nor web-traffic activation."""

    raise Phase6CoreSamStagingError(_ACTIVATION_ERROR)


def _runtime_binding(binding: Phase6CoreSamStagingBinding) -> AgentCoreRuntimeBinding:
    return load_agentcore_runtime_binding(
        {
            "MR_LISTER_AGENTCORE_RUNTIME_ARN": binding.agentcore_runtime_arn,
            "MR_LISTER_AGENTCORE_RUNTIME_BINDING_FINGERPRINT": (
                binding.agentcore_runtime_binding_fingerprint
            ),
            "MR_LISTER_AGENTCORE_RUNTIME_ENDPOINT_ARN": binding.agentcore_runtime_endpoint_arn,
            "MR_LISTER_AGENTCORE_RUNTIME_QUALIFIER": binding.agentcore_runtime_qualifier,
            "MR_LISTER_AGENTCORE_RUNTIME_VERSION": binding.agentcore_runtime_version,
        },
        region=binding.region,
        account_id=binding.account_id,
        environment_name=binding.environment,
        release_fingerprint=binding.release_fingerprint,
    )


def _load_source_template(repository: Path) -> dict[str, object]:
    path = repository / SOURCE_TEMPLATE
    if _path_has_symlink_component(repository, SOURCE_TEMPLATE) or not path.is_file():
        raise ValueError
    raw = path.read_bytes()
    if sha256(raw).hexdigest() != _SOURCE_TEMPLATE_SHA256:
        raise ValueError
    document = json.loads(raw, object_pairs_hook=_unique_json_object)
    if not isinstance(document, dict):
        raise ValueError
    if set(document) != {
        "AWSTemplateFormatVersion",
        "Description",
        "Globals",
        "Outputs",
        "Parameters",
        "Resources",
        "Transform",
    }:
        raise ValueError
    resources = document.get("Resources")
    parameters = document.get("Parameters")
    outputs = document.get("Outputs")
    if (
        document.get("AWSTemplateFormatVersion") != "2010-09-09"
        or document.get("Transform") != "AWS::Serverless-2016-10-31"
        or not isinstance(resources, Mapping)
        or not isinstance(parameters, Mapping)
        or not isinstance(outputs, Mapping)
        or set(parameters) != set(_PARAMETER_FIELDS) | {"ApplicationCertificateArn"}
        or not _OUTPUTS <= set(outputs)
        or _global_variables(document).get("MR_LISTER_PHASE6_SCAFFOLD_ONLY") != "true"
    ):
        raise ValueError
    actual_types: dict[str, object] = {}
    for name in _RESOURCE_TYPES:
        resource = resources.get(name)
        if not isinstance(resource, Mapping):
            raise ValueError
        actual_types[name] = resource.get("Type")
    if actual_types != _RESOURCE_TYPES:
        raise ValueError
    for logical_id, handler in _FUNCTION_HANDLERS.items():
        properties = cast(Mapping[str, object], resources[logical_id].get("Properties"))
        if (
            properties.get("Handler") != handler
            or properties.get("CodeUri") != "lambda/"
            or properties.get("Role") != {"Fn::GetAtt": [f"{logical_id}Role", "Arn"]}
        ):
            raise ValueError
    for logical_id, (definition_path, _definition_sha) in _STATE_MACHINE_AUTHORITIES.items():
        properties = cast(Mapping[str, object], resources[logical_id].get("Properties"))
        expected_uri = definition_path.relative_to(SOURCE_TEMPLATE.parent).as_posix()
        if properties.get("DefinitionUri") != expected_uri or "Definition" in properties:
            raise ValueError
    return document


def _load_state_machine_definitions(
    repository: Path,
    source: Mapping[str, object],
) -> dict[str, Mapping[str, object]]:
    resources = cast(Mapping[str, object], source["Resources"])
    definitions: dict[str, Mapping[str, object]] = {}
    for logical_id, (relative_path, expected_sha) in _STATE_MACHINE_AUTHORITIES.items():
        path = repository / relative_path
        if _path_has_symlink_component(repository, relative_path) or not path.is_file():
            raise ValueError
        raw = path.read_bytes()
        if sha256(raw).hexdigest() != expected_sha:
            raise ValueError
        definition = json.loads(raw, object_pairs_hook=_unique_json_object)
        if not isinstance(definition, Mapping):
            raise ValueError
        resource = cast(Mapping[str, object], resources[logical_id])
        properties = cast(Mapping[str, object], resource["Properties"])
        substitutions = properties.get("DefinitionSubstitutions")
        if not isinstance(substitutions, Mapping):
            raise ValueError
        tokens = _definition_tokens(definition)
        if tokens != set(substitutions):
            raise ValueError
        definitions[logical_id] = definition
    return definitions


def _load_foundation_binding(
    path: Path,
    binding: Phase6CoreSamStagingBinding,
) -> _FoundationBinding:
    raw, document = _load_canonical_mapping(path)
    expected_bucket = binding.lambda_artifact_bucket
    expected_table = f"mr-lister-phase6-{binding.environment}"
    expected = {
        "account_id": binding.account_id,
        "artifact_bucket_arn": f"arn:aws:s3:::{expected_bucket}",
        "artifact_bucket_name": expected_bucket,
        "environment_name": binding.environment,
        "format": _FOUNDATION_BINDING_FORMAT,
        "foundation_template_fingerprint": _FOUNDATION_TEMPLATE_FINGERPRINT,
        "operational_state_table_arn": (
            f"arn:aws:dynamodb:{binding.region}:{binding.account_id}:table/{expected_table}"
        ),
        "operational_state_table_name": expected_table,
        "region": binding.region,
        "stack_id": binding.foundation_stack_id,
        "stack_name": expected_table,
    }
    if set(document) != set(expected) | {"operational_state_stream_arn"}:
        raise ValueError
    if any(document.get(name) != value for name, value in expected.items()):
        raise ValueError
    stream = document.get("operational_state_stream_arn")
    stream_prefix = f"{expected['operational_state_table_arn']}/stream/"
    if (
        not isinstance(stream, str)
        or not stream.startswith(stream_prefix)
        or not stream.removeprefix(stream_prefix)
        or len(stream) > 512
    ):
        raise ValueError
    return _FoundationBinding(sha256=sha256(raw).hexdigest(), document=document)


def _load_ready_runtime_v1_evidence(
    path: Path,
    binding: Phase6CoreSamStagingBinding,
    artifacts: _ArtifactSet,
    repository: Path,
) -> VerifiedAgentCoreRuntimeV1:
    direct_binding = Phase6AgentCoreDirectCodeZipBinding(
        account_id=binding.account_id,
        release_fingerprint=binding.release_fingerprint,
        agentcore_archive_sha256=artifacts.agentcore_archive.sha256,
    )
    runtime = verify_phase6_agentcore_runtime_v1_evidence(
        direct_binding,
        artifacts.agentcore_archive,
        artifacts.agentcore_object,
        runtime_v1_evidence_path=path,
        repository_root=repository,
    )
    if (
        runtime.runtime_arn != binding.agentcore_runtime_arn
        or binding.agentcore_runtime_version != "1"
    ):
        raise ValueError
    return runtime


def _load_ready_endpoint_observation(
    path: Path,
    binding: Phase6CoreSamStagingBinding,
    runtime: VerifiedAgentCoreRuntimeV1,
) -> _EndpointEvidence:
    raw, document = _load_canonical_mapping(path)
    if (
        binding.agentcore_runtime_arn != runtime.runtime_arn
        or binding.agentcore_runtime_version != "1"
        or binding.agentcore_runtime_qualifier != "phase6_v1_dev"
    ):
        raise ValueError
    verify_phase6_agentcore_endpoint_observation(_runtime_binding(binding), document)
    return _EndpointEvidence(sha256=sha256(raw).hexdigest(), document=document)


def _verify_artifacts(
    binding: Phase6CoreSamStagingBinding,
    *,
    deployment_root: Path,
    artifact_root: Path,
    agentcore_object_evidence_path: Path,
    lambda_object_evidence_path: Path,
) -> _ArtifactSet:
    if any(
        candidate.is_symlink()
        for root in (deployment_root, artifact_root)
        for candidate in (root, *root.parents)
    ):
        raise ValueError
    deployment = deployment_root.resolve(strict=True)
    artifacts = artifact_root.resolve(strict=True)
    if (
        deployment.name != "phase6-deployment"
        or artifacts.name != "phase6-artifacts"
        or not deployment.is_dir()
        or not artifacts.is_dir()
    ):
        raise ValueError
    descriptor = verify_phase6_deployment_artifacts(
        deployment,
        artifact_root=artifacts,
        verify_current_source=True,
    )
    descriptor_path = artifacts / DEPLOYMENT_DESCRIPTOR_FILENAME
    lambda_archive_path = artifacts / LAMBDA_ARCHIVE_FILENAME
    agentcore_archive_path = artifacts / AGENTCORE_ARCHIVE_FILENAME
    if (
        descriptor_path.is_symlink()
        or lambda_archive_path.is_symlink()
        or agentcore_archive_path.is_symlink()
        or not descriptor_path.is_file()
        or not lambda_archive_path.is_file()
        or not agentcore_archive_path.is_file()
    ):
        raise ValueError
    raw_descriptor = descriptor_path.read_bytes()
    parsed = json.loads(raw_descriptor, object_pairs_hook=_unique_json_object)
    if (
        not isinstance(descriptor, Mapping)
        or not isinstance(parsed, Mapping)
        or dict(descriptor) != dict(parsed)
        or render_manifest(parsed) != raw_descriptor
        or set(parsed) != {"algorithm", "components", "format", "release_fingerprint", "target"}
        or parsed.get("algorithm") != "sha256"
        or parsed.get("format") != "phase6-deployment-artifacts-v1"
        or parsed.get("release_fingerprint") != binding.release_fingerprint
        or parsed.get("target") != LINUX_ARM64_TARGET
    ):
        raise ValueError
    components = parsed.get("components")
    if not isinstance(components, Mapping) or set(components) != {"agentcore", "lambda"}:
        raise ValueError
    component_fields = {
        "archive",
        "architecture",
        "component",
        "deployment_manifest_sha256",
        "package_format",
        "runtime",
    }
    lambda_record = components.get("lambda")
    agentcore_record = components.get("agentcore")
    if (
        not isinstance(lambda_record, Mapping)
        or set(lambda_record) != component_fields
        or not isinstance(agentcore_record, Mapping)
        or set(agentcore_record) != component_fields
    ):
        raise ValueError
    archive_record = lambda_record.get("archive")
    if (
        not isinstance(archive_record, Mapping)
        or set(archive_record) != {"path", "sha256", "size_bytes"}
        or archive_record.get("path") != LAMBDA_ARCHIVE_FILENAME
        or lambda_record.get("architecture") != "arm64"
        or lambda_record.get("component") != "lambda"
        or lambda_record.get("package_format") != "zip"
        or lambda_record.get("runtime") != "python3.12"
        or _FINGERPRINT.fullmatch(str(lambda_record.get("deployment_manifest_sha256"))) is None
    ):
        raise ValueError
    agentcore_archive_record = agentcore_record.get("archive")
    if (
        not isinstance(agentcore_archive_record, Mapping)
        or set(agentcore_archive_record) != {"path", "sha256", "size_bytes"}
        or agentcore_archive_record.get("path") != AGENTCORE_ARCHIVE_FILENAME
        or agentcore_record.get("architecture") != "arm64"
        or agentcore_record.get("component") != "agentcore"
        or agentcore_record.get("package_format") != "zip"
        or agentcore_record.get("runtime") != "python3.12"
        or _FINGERPRINT.fullmatch(str(agentcore_record.get("deployment_manifest_sha256"))) is None
    ):
        raise ValueError

    raw_archive = lambda_archive_path.read_bytes()
    archive_sha = sha256(raw_archive).hexdigest()
    archive_size = len(raw_archive)
    if (
        archive_record.get("sha256") != archive_sha
        or archive_record.get("size_bytes") != archive_size
        or archive_size <= 0
    ):
        raise ValueError
    raw_agentcore_archive = agentcore_archive_path.read_bytes()
    agentcore_archive_sha = sha256(raw_agentcore_archive).hexdigest()
    agentcore_archive_size = len(raw_agentcore_archive)
    if (
        agentcore_archive_record.get("sha256") != agentcore_archive_sha
        or agentcore_archive_record.get("size_bytes") != agentcore_archive_size
        or agentcore_archive_size <= 0
    ):
        raise ValueError
    descriptor_sha = sha256(raw_descriptor).hexdigest()
    verified_agentcore_archive = VerifiedAgentCoreArchive(
        sha256=agentcore_archive_sha,
        size_bytes=agentcore_archive_size,
        checksum_sha256_base64=base64.b64encode(sha256(raw_agentcore_archive).digest()).decode(
            "ascii"
        ),
        descriptor_sha256=descriptor_sha,
    )

    lambda_expectation = Phase6S3ReleaseObjectExpectation(
        account_id=binding.account_id,
        region=binding.region,
        environment=binding.environment,
        component="lambda",
        release_fingerprint=binding.release_fingerprint,
        archive_sha256=archive_sha,
        size_bytes=archive_size,
    )
    lambda_object = verify_phase6_s3_release_object_evidence(
        lambda_expectation,
        evidence_path=lambda_object_evidence_path,
    )
    if (
        binding.lambda_artifact_bucket != lambda_object.bucket
        or binding.lambda_artifact_key != lambda_object.key
        or binding.lambda_artifact_version != lambda_object.version_id
        or archive_sha != lambda_object.archive_sha256
        or archive_size != lambda_object.size_bytes
    ):
        raise ValueError
    agentcore_expectation = Phase6S3ReleaseObjectExpectation(
        account_id=binding.account_id,
        region=binding.region,
        environment=binding.environment,
        component="agentcore",
        release_fingerprint=binding.release_fingerprint,
        archive_sha256=agentcore_archive_sha,
        size_bytes=agentcore_archive_size,
    )
    agentcore_object = verify_phase6_s3_release_object_evidence(
        agentcore_expectation,
        evidence_path=agentcore_object_evidence_path,
    )
    if (
        agentcore_object.bucket != binding.lambda_artifact_bucket
        or agentcore_object.key != agentcore_expectation.key
        or agentcore_object.archive_sha256 != agentcore_archive_sha
        or agentcore_object.size_bytes != agentcore_archive_size
    ):
        raise ValueError
    return _ArtifactSet(
        descriptor_sha256=descriptor_sha,
        agentcore_archive=verified_agentcore_archive,
        agentcore_object=agentcore_object,
        lambda_archive_sha256=archive_sha,
        lambda_archive_size_bytes=archive_size,
        lambda_object=lambda_object,
    )


def _render_document(
    binding: Phase6CoreSamStagingBinding,
    source: Mapping[str, object],
    definitions: Mapping[str, Mapping[str, object]],
    foundation: _FoundationBinding,
    runtime: VerifiedAgentCoreRuntimeV1,
    endpoint: _EndpointEvidence,
    artifacts: _ArtifactSet,
) -> dict[str, object]:
    source_resources = cast(Mapping[str, object], source["Resources"])
    source_parameters = cast(Mapping[str, object], source["Parameters"])
    source_outputs = cast(Mapping[str, object], source["Outputs"])
    resources = {name: deepcopy(source_resources[name]) for name in sorted(_RESOURCE_TYPES)}
    parameters: dict[str, object] = {}
    for name, field in sorted(_PARAMETER_FIELDS.items()):
        definition = deepcopy(source_parameters[name])
        if not isinstance(definition, dict):
            raise ValueError
        value = getattr(binding, field)
        definition["AllowedValues"] = [value]
        definition["Default"] = value
        parameters[name] = definition

    code_uri = {
        "Bucket": binding.lambda_artifact_bucket,
        "Key": binding.lambda_artifact_key,
        "Version": binding.lambda_artifact_version,
    }
    for logical_id in _FUNCTION_HANDLERS:
        resource = cast(dict[str, object], resources[logical_id])
        properties = cast(dict[str, object], resource["Properties"])
        properties["CodeUri"] = deepcopy(code_uri)
    for logical_id, definition in definitions.items():
        resource = cast(dict[str, object], resources[logical_id])
        properties = cast(dict[str, object], resource["Properties"])
        if properties.pop("DefinitionUri", None) is None:
            raise ValueError
        properties["Definition"] = deepcopy(dict(definition))
    _disable_staging_triggers(resources)

    outputs = {name: deepcopy(source_outputs[name]) for name in sorted(_OUTPUTS)}
    outputs["DeploymentReadiness"] = {
        "Description": (
            "The exact sealed backend release is staged fail-closed; runtime and web traffic "
            "activation remain separate reviewed gates."
        ),
        "Value": "CORE_RELEASE_BOUND_STAGED",
    }
    metadata = _expected_metadata(
        binding,
        definitions,
        foundation,
        runtime,
        endpoint,
        artifacts,
        code_uri,
    )
    return {
        "AWSTemplateFormatVersion": source["AWSTemplateFormatVersion"],
        "Description": (
            "Mr Lister Phase 6 domain-independent core-runtime release-bound staging slice"
        ),
        "Globals": deepcopy(source["Globals"]),
        "Metadata": metadata,
        "Outputs": outputs,
        "Parameters": parameters,
        "Resources": resources,
        "Transform": source["Transform"],
    }


def _validate_rendered_document(
    binding: Phase6CoreSamStagingBinding,
    document: Mapping[str, object],
    source: Mapping[str, object],
    definitions: Mapping[str, Mapping[str, object]],
    foundation: _FoundationBinding,
    runtime: VerifiedAgentCoreRuntimeV1,
    endpoint: _EndpointEvidence,
    artifacts: _ArtifactSet,
) -> None:
    if set(document) != {
        "AWSTemplateFormatVersion",
        "Description",
        "Globals",
        "Metadata",
        "Outputs",
        "Parameters",
        "Resources",
        "Transform",
    }:
        raise ValueError
    resources = cast(Mapping[str, object], document.get("Resources"))
    source_resources = cast(Mapping[str, object], source["Resources"])
    if {
        name: cast(Mapping[str, object], value).get("Type")
        for name, value in resources.items()
        if isinstance(name, str) and isinstance(value, Mapping)
    } != _RESOURCE_TYPES:
        raise ValueError
    for logical_id in _FOUNDATION_RESOURCES:
        if resources.get(logical_id) != source_resources.get(logical_id):
            raise ValueError
    code_uri = {
        "Bucket": binding.lambda_artifact_bucket,
        "Key": binding.lambda_artifact_key,
        "Version": binding.lambda_artifact_version,
    }
    for logical_id, handler in _FUNCTION_HANDLERS.items():
        resource = cast(Mapping[str, object], resources[logical_id])
        properties = cast(Mapping[str, object], resource["Properties"])
        if properties.get("Handler") != handler or properties.get("CodeUri") != code_uri:
            raise ValueError
    for logical_id, definition in definitions.items():
        resource = cast(Mapping[str, object], resources[logical_id])
        properties = cast(Mapping[str, object], resource["Properties"])
        if "DefinitionUri" in properties or properties.get("Definition") != definition:
            raise ValueError
    if _global_variables(document).get("MR_LISTER_PHASE6_SCAFFOLD_ONLY") != "true":
        raise ValueError
    _require_exact_disabled_triggers(document)
    parameters = document.get("Parameters")
    if not isinstance(parameters, Mapping) or set(parameters) != set(_PARAMETER_FIELDS):
        raise ValueError
    for name, field in _PARAMETER_FIELDS.items():
        definition = parameters.get(name)
        value = getattr(binding, field)
        if (
            not isinstance(definition, Mapping)
            or definition.get("Default") != value
            or definition.get("AllowedValues") != [value]
        ):
            raise ValueError
    outputs = document.get("Outputs")
    if not isinstance(outputs, Mapping) or set(outputs) != _OUTPUTS:
        raise ValueError
    if outputs.get("DeploymentReadiness") != {
        "Description": (
            "The exact sealed backend release is staged fail-closed; runtime and web traffic "
            "activation remain separate reviewed gates."
        ),
        "Value": "CORE_RELEASE_BOUND_STAGED",
    }:
        raise ValueError
    expected_metadata = _expected_metadata(
        binding,
        definitions,
        foundation,
        runtime,
        endpoint,
        artifacts,
        code_uri,
    )
    if document.get("Metadata") != expected_metadata:
        raise ValueError
    _reject_local_or_web_references(document)
    verify_core_runtime_dependency_closure(document)


def _expected_metadata(
    binding: Phase6CoreSamStagingBinding,
    definitions: Mapping[str, Mapping[str, object]],
    foundation: _FoundationBinding,
    runtime: VerifiedAgentCoreRuntimeV1,
    endpoint: _EndpointEvidence,
    artifacts: _ArtifactSet,
    code_uri: Mapping[str, str],
) -> dict[str, object]:
    del definitions
    foundation_document = foundation.document
    return {
        "MrListerPhase6CoreRuntimeStaging": {
            "AgentCore": {
                "BindingFingerprint": binding.agentcore_runtime_binding_fingerprint,
                "EndpointArn": binding.agentcore_runtime_endpoint_arn,
                "EndpointObservationSha256": endpoint.sha256,
                "Qualifier": binding.agentcore_runtime_qualifier,
                "RuntimeArn": binding.agentcore_runtime_arn,
                "RuntimeCreateInputSha256": runtime.runtime_create_input_sha256,
                "RuntimeEvidenceSha256": runtime.evidence_sha256,
                "RuntimeRenderManifestSha256": runtime.runtime_render_manifest_sha256,
                "Status": "READY",
                "Version": binding.agentcore_runtime_version,
            },
            "AgentCoreArtifact": {
                "Bucket": artifacts.agentcore_object.bucket,
                "ChecksumSHA256Base64": artifacts.agentcore_object.checksum_sha256_base64,
                "Key": artifacts.agentcore_object.key,
                "ObjectEvidenceSha256": artifacts.agentcore_object.evidence_sha256,
                "Sha256": artifacts.agentcore_archive.sha256,
                "SizeBytes": artifacts.agentcore_archive.size_bytes,
                "Version": artifacts.agentcore_object.version_id,
            },
            "ArtifactDescriptor": {
                "Path": f".mr_lister_private/phase6-artifacts/{DEPLOYMENT_DESCRIPTOR_FILENAME}",
                "Sha256": artifacts.descriptor_sha256,
            },
            "DisabledTriggers": _expected_disabled_trigger_metadata(),
            "Format": "mr-lister-phase6-core-sam-staged-v1",
            "Foundation": {
                "ArtifactBucketArn": foundation_document["artifact_bucket_arn"],
                "ArtifactBucketName": foundation_document["artifact_bucket_name"],
                "BindingSha256": foundation.sha256,
                "OperationalStateTableArn": foundation_document["operational_state_table_arn"],
                "OperationalStateTableName": foundation_document["operational_state_table_name"],
                "StackId": foundation_document["stack_id"],
                "StackName": foundation_document["stack_name"],
            },
            "LambdaArtifact": {
                **dict(code_uri),
                "ChecksumSHA256Base64": artifacts.lambda_object.checksum_sha256_base64,
                "ObjectEvidenceSha256": artifacts.lambda_object.evidence_sha256,
                "Sha256": artifacts.lambda_archive_sha256,
                "SizeBytes": artifacts.lambda_archive_size_bytes,
            },
            "Mode": "STAGED_FAIL_CLOSED",
            "Readiness": "CORE_RELEASE_BOUND_STAGED",
            "ReleaseFingerprint": binding.release_fingerprint,
            "SourceTemplate": {
                "Path": SOURCE_TEMPLATE.as_posix(),
                "Sha256": _SOURCE_TEMPLATE_SHA256,
            },
            "StateMachineDefinitions": {
                name: {"Path": path.as_posix(), "Sha256": fingerprint}
                for name, (path, fingerprint) in sorted(_STATE_MACHINE_AUTHORITIES.items())
            },
            "Target": {
                "AccountId": binding.account_id,
                "Environment": binding.environment,
                "Region": binding.region,
            },
        }
    }


def _disable_staging_triggers(resources: Mapping[str, object]) -> None:
    source_document = {"Resources": resources}
    if _automatic_trigger_inventory(source_document) != _expected_active_trigger_inventory():
        raise ValueError
    for logical_id, event_name, event_type in _DISABLED_SAM_TRIGGER_SPECS:
        resource = resources.get(logical_id)
        if not isinstance(resource, Mapping):
            raise ValueError
        properties = resource.get("Properties")
        if not isinstance(properties, dict):
            raise ValueError
        events = properties.get("Events")
        if not isinstance(events, Mapping):
            raise ValueError
        event = events.get(event_name)
        if not isinstance(event, Mapping) or event.get("Type") != event_type:
            raise ValueError
        event_properties = event.get("Properties")
        if not isinstance(event_properties, dict):
            raise ValueError
        event_properties["Enabled"] = False
    for logical_id in _DISABLED_EVENT_RULE_SPECS:
        resource = resources.get(logical_id)
        if not isinstance(resource, Mapping):
            raise ValueError
        properties = resource.get("Properties")
        if not isinstance(properties, dict):
            raise ValueError
        properties["State"] = "DISABLED"
    _require_exact_disabled_triggers(source_document)


def _require_exact_disabled_triggers(document: Mapping[str, object]) -> None:
    if _automatic_trigger_inventory(document) != _expected_disabled_trigger_metadata():
        raise ValueError


def _automatic_trigger_inventory(document: Mapping[str, object]) -> dict[str, dict[str, object]]:
    resources = document.get("Resources")
    if not isinstance(resources, Mapping):
        raise ValueError
    inventory: dict[str, dict[str, object]] = {}
    for logical_id, resource in resources.items():
        if not isinstance(logical_id, str) or not isinstance(resource, Mapping):
            raise ValueError
        resource_type = resource.get("Type")
        properties = resource.get("Properties")
        if not isinstance(properties, Mapping):
            raise ValueError
        if resource_type == "AWS::Serverless::Function":
            events = properties.get("Events", {})
            if not isinstance(events, Mapping):
                raise ValueError
            for event_name, event in events.items():
                if not isinstance(event_name, str) or not isinstance(event, Mapping):
                    raise ValueError
                event_type = event.get("Type")
                event_properties = event.get("Properties")
                if not isinstance(event_type, str) or not isinstance(event_properties, Mapping):
                    raise ValueError
                if event_type == "HttpApi":
                    continue
                inventory[f"{logical_id}.Events.{event_name}"] = {
                    "Enabled": event_properties.get("Enabled", "DEFAULT_ENABLED"),
                    "Type": event_type,
                }
        elif resource_type == "AWS::Events::Rule":
            inventory[logical_id] = {
                "State": properties.get("State", "DEFAULT_ENABLED"),
                "Type": resource_type,
            }
    return inventory


def _expected_active_trigger_inventory() -> dict[str, dict[str, object]]:
    inventory = _expected_disabled_trigger_metadata()
    for logical_id, event_name, _event_type in _DISABLED_SAM_TRIGGER_SPECS:
        key = f"{logical_id}.Events.{event_name}"
        inventory[key]["Enabled"] = (
            "DEFAULT_ENABLED" if event_name == "OperationalStateChanges" else True
        )
    for logical_id in _DISABLED_EVENT_RULE_SPECS:
        inventory[logical_id]["State"] = "ENABLED"
    return inventory


def _expected_disabled_trigger_metadata() -> dict[str, dict[str, object]]:
    return {
        **{
            f"{logical_id}.Events.{event_name}": {
                "Enabled": False,
                "Type": event_type,
            }
            for logical_id, event_name, event_type in _DISABLED_SAM_TRIGGER_SPECS
        },
        **{
            logical_id: {"State": "DISABLED", "Type": "AWS::Events::Rule"}
            for logical_id in _DISABLED_EVENT_RULE_SPECS
        },
    }


def _intrinsic_references(value: object) -> set[str]:
    references: set[str] = set()
    if isinstance(value, list):
        for item in value:
            references.update(_intrinsic_references(item))
        return references
    if not isinstance(value, Mapping):
        return references
    for key, item in value.items():
        if key == "Ref" and isinstance(item, str):
            references.add(item)
        elif key == "Fn::GetAtt":
            if isinstance(item, str):
                references.add(item.split(".", 1)[0])
            elif isinstance(item, list) and item and isinstance(item[0], str):
                references.add(item[0])
        elif key == "DependsOn":
            if isinstance(item, str):
                references.add(item)
            elif isinstance(item, list):
                if any(not isinstance(name, str) for name in item):
                    raise ValueError
                references.update(cast(list[str], item))
        elif key == "Fn::Sub":
            template: str | None = None
            mapped: set[str] = set()
            if isinstance(item, str):
                template = item
            elif (
                isinstance(item, list)
                and len(item) == 2
                and isinstance(item[0], str)
                and isinstance(item[1], Mapping)
                and all(isinstance(name, str) for name in item[1])
            ):
                template = item[0]
                mapped = set(item[1])
            if template is None:
                raise ValueError
            for match in _SUB_TOKEN.finditer(template):
                token = match.group(1)
                if token.startswith("!") or token in mapped:
                    continue
                references.add(token.split(".", 1)[0])
        references.update(_intrinsic_references(item))
    return references


def _definition_tokens(value: object) -> set[str]:
    found: set[str] = set()
    if isinstance(value, str):
        match = re.fullmatch(r"\$\{([A-Za-z][A-Za-z0-9]*)\}", value)
        if match is not None:
            found.add(match.group(1))
    elif isinstance(value, list):
        for item in value:
            found.update(_definition_tokens(item))
    elif isinstance(value, Mapping):
        for item in value.values():
            found.update(_definition_tokens(item))
    return found


def _reject_local_or_web_references(value: object) -> None:
    forbidden_names = {
        "ApplicationCertificateArn",
        "SellerHttpApi",
        "SellerUserPool",
        "SellerWebAssetBucket",
        "SellerWebDistribution",
    }
    if isinstance(value, list):
        for item in value:
            _reject_local_or_web_references(item)
        return
    if not isinstance(value, Mapping):
        return
    for key, item in value.items():
        if key == "DefinitionUri":
            raise ValueError
        if key == "CodeUri" and not isinstance(item, Mapping):
            raise ValueError
        if isinstance(key, str) and key in forbidden_names:
            raise ValueError
        _reject_local_or_web_references(item)


def _global_variables(document: Mapping[str, object]) -> Mapping[str, object]:
    globals_value = document.get("Globals")
    if not isinstance(globals_value, Mapping):
        raise ValueError
    function = globals_value.get("Function")
    if not isinstance(function, Mapping):
        raise ValueError
    environment = function.get("Environment")
    if not isinstance(environment, Mapping):
        raise ValueError
    variables = environment.get("Variables")
    if not isinstance(variables, Mapping):
        raise ValueError
    return variables


def _load_canonical_mapping(path: Path) -> tuple[bytes, Mapping[str, object]]:
    if any(candidate.is_symlink() for candidate in (path, *path.parents)) or not path.is_file():
        raise ValueError
    raw = path.read_bytes()
    if not 1 <= len(raw) <= 1024 * 1024:
        raise ValueError
    document = json.loads(raw, object_pairs_hook=_unique_json_object)
    if not isinstance(document, Mapping) or render_manifest(document) != raw:
        raise ValueError
    return raw, document


def _unique_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    document: dict[str, object] = {}
    for key, value in pairs:
        if key in document:
            raise ValueError
        document[key] = value
    return document


def _path_has_symlink_component(root: Path, relative: Path) -> bool:
    current = root
    for component in relative.parts:
        current = current / component
        if current.is_symlink():
            return True
    return False


def _canonical_json(value: object) -> bytes:
    return (
        json.dumps(value, allow_nan=False, indent=2, separators=(",", ": "), sort_keys=True) + "\n"
    ).encode("utf-8")


def _is_exact_input(value: object) -> bool:
    return (
        isinstance(value, str)
        and bool(value)
        and value == value.strip()
        and len(value) <= 4096
        and "\x00" not in value
        and _PLACEHOLDER.search(value) is None
    )


def _valid_s3_bucket(value: str) -> bool:
    return (
        _S3_BUCKET.fullmatch(value) is not None
        and ".." not in value
        and not value.startswith("xn--")
        and not value.endswith("-s3alias")
        and re.fullmatch(r"[0-9]+(?:\.[0-9]+){3}", value) is None
    )


def _valid_s3_key(value: str) -> bool:
    if (
        len(value) > 1024
        or not value.isascii()
        or value.startswith("/")
        or value.endswith("/")
        or "//" in value
        or "\\" in value
        or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]*", value) is None
    ):
        return False
    parts = value.split("/")
    return (
        all(part not in {"", ".", ".."} for part in parts)
        and not any(part.casefold() in _MOVING_VALUES for part in parts)
        and PurePosixPath(value).as_posix() == value
    )


def _output_destination(repository: Path) -> Path:
    destination = repository / CORE_STAGED_TEMPLATE_OUTPUT
    if destination.relative_to(repository).parts[0] != ".mr_lister_private":
        raise ValueError
    return destination


def _prepare_private_parent(repository: Path, parent: Path) -> None:
    current = repository
    for component in parent.relative_to(repository).parts:
        current = current / component
        if current.is_symlink():
            raise ValueError
        if current.exists() and not current.is_dir():
            raise ValueError
    parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if parent.is_symlink():
        raise ValueError


def _binding_from_arguments(arguments: argparse.Namespace) -> Phase6CoreSamStagingBinding:
    return Phase6CoreSamStagingBinding(
        account_id=arguments.account_id,
        region=arguments.region,
        environment=arguments.environment,
        foundation_stack_id=arguments.foundation_stack_id,
        release_fingerprint=arguments.release_fingerprint,
        agentcore_runtime_arn=arguments.agentcore_runtime_arn,
        agentcore_runtime_endpoint_arn=arguments.agentcore_runtime_endpoint_arn,
        agentcore_runtime_version=arguments.agentcore_runtime_version,
        agentcore_runtime_qualifier=arguments.agentcore_runtime_qualifier,
        agentcore_runtime_binding_fingerprint=arguments.agentcore_runtime_binding_fingerprint,
        printify_secret_arn=arguments.printify_secret_arn,
        application_origin=arguments.application_origin,
        lambda_artifact_bucket=arguments.lambda_artifact_bucket,
        lambda_artifact_key=arguments.lambda_artifact_key,
        lambda_artifact_version=arguments.lambda_artifact_version,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--account-id", required=True)
    parser.add_argument("--region", required=True)
    parser.add_argument("--environment", required=True)
    parser.add_argument("--foundation-stack-id", required=True)
    parser.add_argument("--foundation-binding", required=True, type=Path)
    parser.add_argument("--release-fingerprint", required=True)
    parser.add_argument("--agentcore-runtime-arn", required=True)
    parser.add_argument("--agentcore-runtime-endpoint-arn", required=True)
    parser.add_argument("--agentcore-runtime-version", required=True)
    parser.add_argument("--agentcore-runtime-qualifier", required=True)
    parser.add_argument("--agentcore-runtime-binding-fingerprint", required=True)
    parser.add_argument("--agentcore-endpoint-observation", required=True, type=Path)
    parser.add_argument("--agentcore-object-evidence", required=True, type=Path)
    parser.add_argument("--agentcore-runtime-v1-evidence", required=True, type=Path)
    parser.add_argument("--printify-secret-arn", required=True)
    parser.add_argument("--application-origin", required=True)
    parser.add_argument("--lambda-artifact-bucket", required=True)
    parser.add_argument("--lambda-artifact-key", required=True)
    parser.add_argument("--lambda-artifact-version", required=True)
    parser.add_argument("--lambda-object-evidence", required=True, type=Path)
    parser.add_argument("--deployment-root", type=Path, default=DEFAULT_DEPLOYMENT_ROOT)
    parser.add_argument("--artifact-root", type=Path, default=DEFAULT_ARTIFACT_ROOT)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--write-staged", action="store_true")
    action.add_argument("--verify-staged", action="store_true")
    action.add_argument("--activate", action="store_true")
    arguments = parser.parse_args()
    try:
        if arguments.activate:
            reject_phase6_core_sam_activation()
        binding = _binding_from_arguments(arguments)
        options = {
            "foundation_binding_path": arguments.foundation_binding,
            "agentcore_endpoint_observation_path": arguments.agentcore_endpoint_observation,
            "agentcore_object_evidence_path": arguments.agentcore_object_evidence,
            "agentcore_runtime_v1_evidence_path": arguments.agentcore_runtime_v1_evidence,
            "lambda_object_evidence_path": arguments.lambda_object_evidence,
            "deployment_root": arguments.deployment_root,
            "artifact_root": arguments.artifact_root,
        }
        if arguments.write_staged:
            print(write_phase6_core_sam_staged_template(binding, **options))
        else:
            verify_rendered_phase6_core_sam_staged_template(binding, **options)
            print(CORE_STAGED_TEMPLATE_OUTPUT)
    except Phase6CoreSamStagingError as error:
        parser.exit(2, f"{error}\n")


__all__ = [
    "CORE_STAGED_TEMPLATE_OUTPUT",
    "DEFAULT_ARTIFACT_ROOT",
    "DEFAULT_DEPLOYMENT_ROOT",
    "SOURCE_TEMPLATE",
    "Phase6CoreSamStagingBinding",
    "Phase6CoreSamStagingError",
    "reject_phase6_core_sam_activation",
    "render_phase6_core_sam_staged_template",
    "verify_core_runtime_dependency_closure",
    "verify_phase6_core_sam_staged_inertness",
    "verify_rendered_phase6_core_sam_staged_template",
    "write_phase6_core_sam_staged_template",
]


if __name__ == "__main__":
    main()
