"""Render the exact additive Phase 6 seller web-edge transition.

The source SAM template is intentionally not a deployment target.  This renderer recomputes the
exact corrected active draft-only backend, recomputes the sealed full Phase 6 staging template,
and grafts only the resources and outputs absent from the active backend.  Every existing resource
is preserved byte-for-byte.  The only newly serving resources are the HTTP API and CloudFront
distribution that are deliberately enabled here after the ACM certificate is pinned.

This module is local-only.  It does not contact AWS, upload assets, create a change set, or alter
DNS.  A separate change-set verifier must prove that the processed CloudFormation update contains
only the expected additions before execution is considered.
"""

from __future__ import annotations

import argparse
import json
import re
from collections.abc import Mapping
from copy import deepcopy
from hashlib import sha256
from pathlib import Path

import tools.render_phase6_core_runtime_transition as core_transition
import tools.render_phase6_sam_activation as full_staging
from tools.render_phase6_core_runtime_transition import (
    Phase6CoreRuntimeTransitionTarget,
)
from tools.render_phase6_core_sam_staging import (
    DEFAULT_ARTIFACT_ROOT,
    DEFAULT_DEPLOYMENT_ROOT,
    ROOT,
    Phase6CoreSamStagingBinding,
)
from tools.render_phase6_sam_activation import Phase6SamStagingBinding

WEB_EDGE_TEMPLATE_OUTPUT = Path(
    ".mr_lister_private/phase6-web-edge-transition/template.web-edge-active-draft-only.local.json"
)

ACTIVE_CORE_TEMPLATE_SHA256 = "f0e1c0cfcf1b80d8c5277aacd68cb9a0246bedc882246c448a8772ebe4d87a78"
SOURCE_TEMPLATE_SHA256 = "6b8221fd526cd06cf76cf0029d9c2cd6baf81662aeaa280aa573391e0dfdec3b"
WEB_EDGE_TEMPLATE_SHA256 = "0ab2c8f016afb513d7de5dd65aefd975eeaf827800aa19ceb31d0f64c02748c8"
WEB_EDGE_TEMPLATE_DESCRIPTION = (
    "Mr Lister Phase 6 active draft-only seller control and private web edge"
)
WEB_EDGE_READINESS = "WEB_EDGE_ACTIVE_DRAFT_ONLY"
WEB_EDGE_DESCRIPTION = (
    "The corrected draft-only backend is preserved while the seller web, identity, API, and "
    "operational edge are active; publication, order, and fulfillment remain unavailable."
)

_GENERIC_ERROR = "Phase 6 web-edge transition configuration is invalid"
_METADATA_KEY = "MrListerPhase6WebEdgeTransition"
_CORE_METADATA_KEY = "MrListerPhase6CoreRuntimeStaging"
_FULL_METADATA_KEY = "MrListerPhase6StagedDeployment"
_FORMAT = "mr-lister-phase6-web-edge-transition-v1"
_CERTIFICATE_ID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_FINGERPRINT = re.compile(r"^[a-f0-9]{64}$")
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
_SUB_TOKEN = re.compile(r"\$\{([^}]+)\}")

_ACTIVE_RESOURCE_COUNT = 40
_TARGET_RESOURCE_COUNT = 102
_ADDED_RESOURCE_COUNT = 62
_ACTIVE_OUTPUT_COUNT = 7
_TARGET_OUTPUT_COUNT = 19
_ADDED_OUTPUT_COUNT = 12
_OPERATIONAL_ALARM_KEY_CLASSIFICATION = "OperationalAlarmTransport"
_CERTIFICATE_PARAMETER = "ApplicationCertificateArn"
_API_FUNCTIONS = frozenset(
    {"ReviewQueryApiFunction", "SellerCommandApiFunction", "UploadApiFunction"}
)


class Phase6WebEdgeTransitionError(RuntimeError):
    """A value-free failure for a drifting or broadened web-edge transition."""


def render_phase6_web_edge_transition(
    binding: Phase6CoreSamStagingBinding,
    *,
    application_certificate_arn: str,
    foundation_binding_path: Path,
    agentcore_endpoint_observation_path: Path,
    agentcore_object_evidence_path: Path,
    agentcore_runtime_v1_evidence_path: Path,
    lambda_object_evidence_path: Path,
    repository_root: Path = ROOT,
    deployment_root: Path = DEFAULT_DEPLOYMENT_ROOT,
    artifact_root: Path = DEFAULT_ARTIFACT_ROOT,
) -> bytes:
    """Return canonical bytes for the exact active-core-preserving web target."""

    try:
        repository = _repository(repository_root)
        foundation_binding = _repository_file(repository, foundation_binding_path)
        agentcore_endpoint_observation = _repository_file(
            repository, agentcore_endpoint_observation_path
        )
        agentcore_object_evidence = _repository_file(repository, agentcore_object_evidence_path)
        agentcore_runtime_v1_evidence = _repository_file(
            repository, agentcore_runtime_v1_evidence_path
        )
        lambda_object_evidence = _repository_file(repository, lambda_object_evidence_path)
        deployment = _repository_directory(repository, deployment_root)
        artifacts = _repository_directory(repository, artifact_root)
        _validate_certificate(
            application_certificate_arn,
            account_id=binding.account_id,
        )
        active_raw = core_transition.render_phase6_core_runtime_transition(
            binding,
            target=Phase6CoreRuntimeTransitionTarget.BACKEND_ACTIVE_DRAFT_ONLY,
            foundation_binding_path=foundation_binding,
            agentcore_endpoint_observation_path=agentcore_endpoint_observation,
            agentcore_object_evidence_path=agentcore_object_evidence,
            agentcore_runtime_v1_evidence_path=agentcore_runtime_v1_evidence,
            lambda_object_evidence_path=lambda_object_evidence,
            repository_root=repository,
            deployment_root=deployment,
            artifact_root=artifacts,
        )
        active = _canonical_document(active_raw)
        if sha256(active_raw).hexdigest() != ACTIVE_CORE_TEMPLATE_SHA256:
            raise ValueError

        staged_binding = _full_staging_binding(
            binding,
            application_certificate_arn=application_certificate_arn,
        )
        full_raw = full_staging.render_phase6_sam_staged_template(
            staged_binding,
            agentcore_endpoint_observation_path=agentcore_endpoint_observation,
            agentcore_object_evidence_path=agentcore_object_evidence,
            agentcore_runtime_v1_evidence_path=agentcore_runtime_v1_evidence,
            lambda_object_evidence_path=lambda_object_evidence,
            repository_root=repository,
            deployment_root=deployment,
            artifact_root=artifacts,
        )
        full = _canonical_document(full_raw)
        rendered = _render_additive_target(
            active,
            full,
            active_sha256=sha256(active_raw).hexdigest(),
            full_staged_sha256=sha256(full_raw).hexdigest(),
            application_certificate_arn=application_certificate_arn,
            application_origin=binding.application_origin,
        )
        _validate_target(
            active,
            full,
            rendered,
            active_sha256=sha256(active_raw).hexdigest(),
            full_staged_sha256=sha256(full_raw).hexdigest(),
            application_certificate_arn=application_certificate_arn,
            application_origin=binding.application_origin,
        )
        raw = _canonical_json(rendered)
        if sha256(raw).hexdigest() != WEB_EDGE_TEMPLATE_SHA256:
            raise ValueError
        return raw
    except Phase6WebEdgeTransitionError:
        raise
    except Exception:
        raise Phase6WebEdgeTransitionError(_GENERIC_ERROR) from None


def write_phase6_web_edge_transition(
    binding: Phase6CoreSamStagingBinding,
    *,
    application_certificate_arn: str,
    foundation_binding_path: Path,
    agentcore_endpoint_observation_path: Path,
    agentcore_object_evidence_path: Path,
    agentcore_runtime_v1_evidence_path: Path,
    lambda_object_evidence_path: Path,
    repository_root: Path = ROOT,
    deployment_root: Path = DEFAULT_DEPLOYMENT_ROOT,
    artifact_root: Path = DEFAULT_ARTIFACT_ROOT,
) -> Path:
    """Create the fixed private target and refuse every existing destination."""

    try:
        repository = _repository(repository_root)
        destination = _output_destination(repository)
        if destination.exists() or destination.is_symlink():
            raise ValueError
        raw = render_phase6_web_edge_transition(
            binding,
            application_certificate_arn=application_certificate_arn,
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
    except Phase6WebEdgeTransitionError:
        raise
    except Exception:
        raise Phase6WebEdgeTransitionError(_GENERIC_ERROR) from None


def verify_rendered_phase6_web_edge_transition(
    binding: Phase6CoreSamStagingBinding,
    *,
    application_certificate_arn: str,
    foundation_binding_path: Path,
    agentcore_endpoint_observation_path: Path,
    agentcore_object_evidence_path: Path,
    agentcore_runtime_v1_evidence_path: Path,
    lambda_object_evidence_path: Path,
    repository_root: Path = ROOT,
    deployment_root: Path = DEFAULT_DEPLOYMENT_ROOT,
    artifact_root: Path = DEFAULT_ARTIFACT_ROOT,
) -> None:
    """Recompute every sealed input and require the fixed output byte-for-byte."""

    try:
        repository = _repository(repository_root)
        destination = _output_destination(repository)
        expected = render_phase6_web_edge_transition(
            binding,
            application_certificate_arn=application_certificate_arn,
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
    except Phase6WebEdgeTransitionError:
        raise
    except Exception:
        raise Phase6WebEdgeTransitionError(_GENERIC_ERROR) from None


def _full_staging_binding(
    binding: Phase6CoreSamStagingBinding,
    *,
    application_certificate_arn: str,
) -> Phase6SamStagingBinding:
    return Phase6SamStagingBinding(
        account_id=binding.account_id,
        region=binding.region,
        environment=binding.environment,
        release_fingerprint=binding.release_fingerprint,
        agentcore_runtime_arn=binding.agentcore_runtime_arn,
        agentcore_runtime_endpoint_arn=binding.agentcore_runtime_endpoint_arn,
        agentcore_runtime_version=binding.agentcore_runtime_version,
        agentcore_runtime_qualifier=binding.agentcore_runtime_qualifier,
        agentcore_runtime_binding_fingerprint=(binding.agentcore_runtime_binding_fingerprint),
        printify_secret_arn=binding.printify_secret_arn,
        application_origin=binding.application_origin,
        application_certificate_arn=application_certificate_arn,
        lambda_artifact_bucket=binding.lambda_artifact_bucket,
        lambda_artifact_key=binding.lambda_artifact_key,
        lambda_artifact_version=binding.lambda_artifact_version,
    )


def _render_additive_target(
    active: Mapping[str, object],
    full: Mapping[str, object],
    *,
    active_sha256: str,
    full_staged_sha256: str,
    application_certificate_arn: str,
    application_origin: str,
) -> dict[str, object]:
    active_resources = _mapping(active, "Resources")
    full_resources = _mapping(full, "Resources")
    added_resources = set(full_resources) - set(active_resources)
    if (
        len(active_resources) != _ACTIVE_RESOURCE_COUNT
        or len(full_resources) != _TARGET_RESOURCE_COUNT
        or len(added_resources) != _ADDED_RESOURCE_COUNT
        or not set(active_resources) < set(full_resources)
    ):
        raise ValueError

    rendered = deepcopy(dict(active))
    rendered["Description"] = WEB_EDGE_TEMPLATE_DESCRIPTION
    target_resources = _mutable_mapping(rendered, "Resources")
    for logical_id in sorted(added_resources):
        target_resources[logical_id] = deepcopy(full_resources[logical_id])
    _enable_external_serving(target_resources)
    _classify_operational_alarm_key(target_resources)

    active_parameters = _mapping(active, "Parameters")
    full_parameters = _mapping(full, "Parameters")
    if set(full_parameters) - set(active_parameters) != {_CERTIFICATE_PARAMETER} or any(
        full_parameters[name] != value for name, value in active_parameters.items()
    ):
        raise ValueError
    target_parameters = _mutable_mapping(rendered, "Parameters")
    target_parameters[_CERTIFICATE_PARAMETER] = deepcopy(full_parameters[_CERTIFICATE_PARAMETER])

    active_outputs = _mapping(active, "Outputs")
    full_outputs = _mapping(full, "Outputs")
    added_outputs = set(full_outputs) - set(active_outputs)
    if (
        len(active_outputs) != _ACTIVE_OUTPUT_COUNT
        or len(full_outputs) != _TARGET_OUTPUT_COUNT
        or len(added_outputs) != _ADDED_OUTPUT_COUNT
        or not set(active_outputs) < set(full_outputs)
    ):
        raise ValueError
    target_outputs = _mutable_mapping(rendered, "Outputs")
    for output_name in sorted(added_outputs):
        target_outputs[output_name] = deepcopy(full_outputs[output_name])
    target_outputs["DeploymentReadiness"] = {
        "Description": WEB_EDGE_DESCRIPTION,
        "Value": WEB_EDGE_READINESS,
    }

    metadata = _mutable_mapping(rendered, "Metadata")
    if _METADATA_KEY in metadata or _CORE_METADATA_KEY not in metadata:
        raise ValueError
    metadata[_METADATA_KEY] = _web_metadata(
        active_sha256=active_sha256,
        full_staged_sha256=full_staged_sha256,
        application_certificate_arn=application_certificate_arn,
        application_origin=application_origin,
        added_resources=added_resources,
        added_outputs=added_outputs,
    )
    return rendered


def _enable_external_serving(resources: Mapping[str, object]) -> None:
    api_properties = _resource_properties(resources, "SellerHttpApi")
    distribution_properties = _resource_properties(resources, "SellerWebDistribution")
    distribution_config = distribution_properties.get("DistributionConfig")
    if not isinstance(distribution_config, dict):
        raise ValueError
    if api_properties.pop("DisableExecuteApiEndpoint", None) is not True:
        raise ValueError
    if distribution_config.get("Enabled") is not False:
        raise ValueError
    distribution_config["Enabled"] = True


def _classify_operational_alarm_key(resources: Mapping[str, object]) -> None:
    properties = _resource_properties(resources, "OperationalAlarmTopicKey")
    tags = properties.get("Tags")
    expected = [
        {"Key": "Project", "Value": "MrLister"},
        {"Key": "Environment", "Value": {"Ref": "EnvironmentName"}},
    ]
    if tags != expected:
        raise ValueError
    properties["Tags"] = [
        *expected,
        {
            "Key": "DataClassification",
            "Value": _OPERATIONAL_ALARM_KEY_CLASSIFICATION,
        },
    ]


def _validate_target(
    active: Mapping[str, object],
    full: Mapping[str, object],
    target: Mapping[str, object],
    *,
    active_sha256: str,
    full_staged_sha256: str,
    application_certificate_arn: str,
    application_origin: str,
) -> None:
    if (
        active_sha256 != ACTIVE_CORE_TEMPLATE_SHA256
        or _source_template_sha256(full) != SOURCE_TEMPLATE_SHA256
        or _FINGERPRINT.fullmatch(full_staged_sha256) is None
    ):
        raise ValueError

    structural_keys = {"Description", "Metadata", "Outputs", "Parameters", "Resources"}
    if set(target) != set(active) or any(
        target[key] != value for key, value in active.items() if key not in structural_keys
    ):
        raise ValueError
    if target.get("Description") != WEB_EDGE_TEMPLATE_DESCRIPTION:
        raise ValueError

    active_resources = _mapping(active, "Resources")
    full_resources = _mapping(full, "Resources")
    target_resources = _mapping(target, "Resources")
    added_resources = set(full_resources) - set(active_resources)
    if (
        len(target_resources) != _TARGET_RESOURCE_COUNT
        or set(target_resources) != set(full_resources)
        or any(target_resources[name] != value for name, value in active_resources.items())
    ):
        raise ValueError
    expected_added = {name: deepcopy(full_resources[name]) for name in sorted(added_resources)}
    _enable_external_serving(expected_added)
    _classify_operational_alarm_key(expected_added)
    if any(target_resources[name] != value for name, value in expected_added.items()):
        raise ValueError

    active_parameters = _mapping(active, "Parameters")
    target_parameters = _mapping(target, "Parameters")
    if set(target_parameters) != set(active_parameters) | {_CERTIFICATE_PARAMETER} or any(
        target_parameters[name] != value for name, value in active_parameters.items()
    ):
        raise ValueError
    certificate_parameter = target_parameters.get(_CERTIFICATE_PARAMETER)
    if (
        not isinstance(certificate_parameter, Mapping)
        or certificate_parameter.get("Default") != application_certificate_arn
        or certificate_parameter.get("AllowedValues") != [application_certificate_arn]
    ):
        raise ValueError

    active_outputs = _mapping(active, "Outputs")
    full_outputs = _mapping(full, "Outputs")
    target_outputs = _mapping(target, "Outputs")
    added_outputs = set(full_outputs) - set(active_outputs)
    if (
        set(target_outputs) != set(full_outputs)
        or target_outputs.get("DeploymentReadiness")
        != {"Description": WEB_EDGE_DESCRIPTION, "Value": WEB_EDGE_READINESS}
        or any(
            target_outputs[name] != value
            for name, value in active_outputs.items()
            if name != "DeploymentReadiness"
        )
        or any(target_outputs[name] != full_outputs[name] for name in added_outputs)
    ):
        raise ValueError

    active_metadata = _mapping(active, "Metadata")
    target_metadata = _mapping(target, "Metadata")
    if (
        set(target_metadata) != set(active_metadata) | {_METADATA_KEY}
        or any(target_metadata[name] != value for name, value in active_metadata.items())
        or target_metadata.get(_METADATA_KEY)
        != _web_metadata(
            active_sha256=active_sha256,
            full_staged_sha256=full_staged_sha256,
            application_certificate_arn=application_certificate_arn,
            application_origin=application_origin,
            added_resources=added_resources,
            added_outputs=added_outputs,
        )
    ):
        raise ValueError

    globals_value = _mapping(target, "Globals")
    function_globals = globals_value.get("Function")
    if not isinstance(function_globals, Mapping):
        raise ValueError
    environment = function_globals.get("Environment")
    if not isinstance(environment, Mapping):
        raise ValueError
    variables = environment.get("Variables")
    if (
        not isinstance(variables, Mapping)
        or variables.get("MR_LISTER_PHASE6_SCAFFOLD_ONLY") != "false"
    ):
        raise ValueError

    for logical_id in ("DispatcherFunction", "SettlementFunction"):
        if _resource_properties(target_resources, logical_id).get("Timeout") != 120:
            raise ValueError
    new_functions = {
        name
        for name in added_resources
        if _resource_type(target_resources, name) == "AWS::Serverless::Function"
    }
    if new_functions != _API_FUNCTIONS:
        raise ValueError
    active_code = _resource_properties(target_resources, "DispatcherFunction").get("CodeUri")
    if not isinstance(active_code, Mapping) or set(active_code) != {
        "Bucket",
        "Key",
        "Version",
    }:
        raise ValueError
    for logical_id in _API_FUNCTIONS:
        properties = _resource_properties(target_resources, logical_id)
        if properties.get("CodeUri") != active_code or "ReservedConcurrentExecutions" in properties:
            raise ValueError

    api_properties = _resource_properties(target_resources, "SellerHttpApi")
    distribution = _resource_properties(target_resources, "SellerWebDistribution").get(
        "DistributionConfig"
    )
    if (
        "DisableExecuteApiEndpoint" in api_properties
        or not isinstance(distribution, Mapping)
        or distribution.get("Enabled") is not True
        or any(
            isinstance(resource, Mapping)
            and isinstance(resource.get("Type"), str)
            and resource["Type"].startswith("AWS::Route53::")
            for resource in target_resources.values()
        )
    ):
        raise ValueError

    _reject_local_deployment_references(target)
    _verify_intrinsic_reference_closure(target)


def _web_metadata(
    *,
    active_sha256: str,
    full_staged_sha256: str,
    application_certificate_arn: str,
    application_origin: str,
    added_resources: set[str],
    added_outputs: set[str],
) -> dict[str, object]:
    return {
        "ActiveCoreTemplateSha256": active_sha256,
        "AddedOutputs": sorted(added_outputs),
        "AddedResources": sorted(added_resources),
        "ApplicationCertificateArn": application_certificate_arn,
        "ApplicationOrigin": application_origin,
        "ExternalServing": {
            "SellerHttpApi": {"DisableExecuteApiEndpoint": "DEFAULT_ENABLED"},
            "SellerWebDistribution": {"Enabled": True},
        },
        "Format": _FORMAT,
        "FullStagedTemplateSha256": full_staged_sha256,
        "Mode": WEB_EDGE_READINESS,
        "Readiness": WEB_EDGE_READINESS,
        "SourceTemplateSha256": SOURCE_TEMPLATE_SHA256,
    }


def _source_template_sha256(document: Mapping[str, object]) -> object:
    metadata = _mapping(document, "Metadata")
    staged = metadata.get(_FULL_METADATA_KEY)
    if not isinstance(staged, Mapping):
        raise ValueError
    return staged.get("SourceTemplateSha256")


def _validate_certificate(value: str, *, account_id: str) -> None:
    if not isinstance(value, str) or value != value.strip():
        raise ValueError
    prefix = f"arn:aws:acm:us-east-1:{account_id}:certificate/"
    certificate_id = value.removeprefix(prefix)
    if not value.startswith(prefix) or _CERTIFICATE_ID.fullmatch(certificate_id) is None:
        raise ValueError


def _verify_intrinsic_reference_closure(document: Mapping[str, object]) -> None:
    resources = _mapping(document, "Resources")
    parameters = _mapping(document, "Parameters")
    allowed = set(resources) | set(parameters) | set(_PSEUDO_PARAMETERS)
    references: set[str] = set()

    def visit(value: object) -> None:
        if isinstance(value, list):
            for item in value:
                visit(item)
            return
        if not isinstance(value, Mapping):
            return
        ref = value.get("Ref")
        if isinstance(ref, str):
            references.add(ref)
        get_att = value.get("Fn::GetAtt")
        if isinstance(get_att, list) and get_att and isinstance(get_att[0], str):
            references.add(get_att[0])
        elif isinstance(get_att, str) and get_att:
            references.add(get_att.split(".", 1)[0])
        substitution = value.get("Fn::Sub")
        if isinstance(substitution, str):
            _collect_substitution_references(substitution, frozenset(), references)
        elif (
            isinstance(substitution, list)
            and len(substitution) == 2
            and isinstance(substitution[0], str)
            and isinstance(substitution[1], Mapping)
        ):
            _collect_substitution_references(
                substitution[0],
                frozenset(key for key in substitution[1] if isinstance(key, str)),
                references,
            )
        for item in value.values():
            visit(item)

    visit(document)
    if references - allowed:
        raise ValueError


def _collect_substitution_references(
    template: str,
    substitutions: frozenset[str],
    references: set[str],
) -> None:
    for token in _SUB_TOKEN.findall(template):
        if token.startswith("!") or token in substitutions:
            continue
        references.add(token.split(".", 1)[0])


def _reject_local_deployment_references(value: object) -> None:
    if isinstance(value, list):
        for item in value:
            _reject_local_deployment_references(item)
        return
    if not isinstance(value, Mapping):
        return
    for key, item in value.items():
        if key == "DefinitionUri":
            raise ValueError
        if key == "CodeUri" and not isinstance(item, Mapping):
            raise ValueError
        _reject_local_deployment_references(item)


def _mapping(document: Mapping[str, object], key: str) -> Mapping[str, object]:
    value = document.get(key)
    if not isinstance(value, Mapping) or any(not isinstance(name, str) for name in value):
        raise ValueError
    return value


def _mutable_mapping(document: Mapping[str, object], key: str) -> dict[str, object]:
    value = document.get(key)
    if not isinstance(value, dict):
        raise ValueError
    return value


def _resource_properties(
    resources: Mapping[str, object],
    logical_id: str,
) -> dict[str, object]:
    resource = resources.get(logical_id)
    if not isinstance(resource, Mapping):
        raise ValueError
    properties = resource.get("Properties")
    if not isinstance(properties, dict):
        raise ValueError
    return properties


def _resource_type(resources: Mapping[str, object], logical_id: str) -> object:
    resource = resources.get(logical_id)
    if not isinstance(resource, Mapping):
        raise ValueError
    return resource.get("Type")


def _canonical_document(raw: bytes) -> dict[str, object]:
    if not isinstance(raw, bytes) or not raw:
        raise ValueError
    document = json.loads(raw, object_pairs_hook=_unique_json_object)
    if not isinstance(document, dict) or _canonical_json(document) != raw:
        raise ValueError
    return document


def _unique_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    document: dict[str, object] = {}
    for key, value in pairs:
        if key in document:
            raise ValueError
        document[key] = value
    return document


def _canonical_json(value: object) -> bytes:
    return (
        json.dumps(value, allow_nan=False, indent=2, separators=(",", ": "), sort_keys=True) + "\n"
    ).encode("utf-8")


def _repository(path: Path) -> Path:
    if not isinstance(path, Path) or path.is_symlink():
        raise ValueError
    repository = path.resolve(strict=True)
    if not repository.is_dir():
        raise ValueError
    return repository


def _repository_file(repository: Path, path: Path) -> Path:
    return _repository_path(repository, path, require_directory=False)


def _repository_directory(repository: Path, path: Path) -> Path:
    return _repository_path(repository, path, require_directory=True)


def _repository_path(repository: Path, path: Path, *, require_directory: bool) -> Path:
    if not isinstance(path, Path):
        raise ValueError
    candidate = path if path.is_absolute() else repository / path
    try:
        relative = candidate.relative_to(repository)
    except ValueError:
        raise ValueError from None
    if not relative.parts or any(part in {"..", "."} for part in relative.parts):
        raise ValueError
    current = repository
    for component in relative.parts:
        current = current / component
        if current.is_symlink():
            raise ValueError
    resolved = candidate.resolve(strict=True)
    try:
        resolved.relative_to(repository)
    except ValueError:
        raise ValueError from None
    if require_directory:
        if not resolved.is_dir():
            raise ValueError
    elif not resolved.is_file():
        raise ValueError
    return resolved


def _output_destination(repository: Path) -> Path:
    destination = repository / WEB_EDGE_TEMPLATE_OUTPUT
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
        agentcore_runtime_binding_fingerprint=(arguments.agentcore_runtime_binding_fingerprint),
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
    parser.add_argument("--application-certificate-arn", required=True)
    parser.add_argument("--lambda-artifact-bucket", required=True)
    parser.add_argument("--lambda-artifact-key", required=True)
    parser.add_argument("--lambda-artifact-version", required=True)
    parser.add_argument("--lambda-object-evidence", required=True, type=Path)
    parser.add_argument("--deployment-root", type=Path, default=DEFAULT_DEPLOYMENT_ROOT)
    parser.add_argument("--artifact-root", type=Path, default=DEFAULT_ARTIFACT_ROOT)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--write", action="store_true")
    action.add_argument("--verify", action="store_true")
    arguments = parser.parse_args()
    try:
        binding = _binding_from_arguments(arguments)
        options = {
            "application_certificate_arn": arguments.application_certificate_arn,
            "foundation_binding_path": arguments.foundation_binding,
            "agentcore_endpoint_observation_path": arguments.agentcore_endpoint_observation,
            "agentcore_object_evidence_path": arguments.agentcore_object_evidence,
            "agentcore_runtime_v1_evidence_path": arguments.agentcore_runtime_v1_evidence,
            "lambda_object_evidence_path": arguments.lambda_object_evidence,
            "deployment_root": arguments.deployment_root,
            "artifact_root": arguments.artifact_root,
        }
        if arguments.write:
            print(write_phase6_web_edge_transition(binding, **options))
        else:
            verify_rendered_phase6_web_edge_transition(binding, **options)
            print(_output_destination(ROOT))
    except Phase6WebEdgeTransitionError as error:
        parser.exit(2, f"{error}\n")


__all__ = [
    "ACTIVE_CORE_TEMPLATE_SHA256",
    "SOURCE_TEMPLATE_SHA256",
    "WEB_EDGE_DESCRIPTION",
    "WEB_EDGE_READINESS",
    "WEB_EDGE_TEMPLATE_DESCRIPTION",
    "WEB_EDGE_TEMPLATE_SHA256",
    "WEB_EDGE_TEMPLATE_OUTPUT",
    "Phase6WebEdgeTransitionError",
    "render_phase6_web_edge_transition",
    "verify_rendered_phase6_web_edge_transition",
    "write_phase6_web_edge_transition",
]


if __name__ == "__main__":
    main()
