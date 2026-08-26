"""Render one exact Phase 6 core-runtime transition from sealed inert staging.

This module has no independent release or artifact loader.  It always obtains its source bytes
from :func:`tools.render_phase6_core_sam_staging.render_phase6_core_sam_staged_template`, then
permits only one of two closed transformations:

* release the three zero concurrency guards while keeping scaffold mode and all triggers inert;
* activate the draft-only backend from that capacity-released form by changing only the scaffold
  marker and the five reviewed trigger states.

Neither target adds a web resource or grants publication, order, or fulfillment authority.  The
renderer is local-only: it never contacts AWS, uploads an artifact, or executes a change set.
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
from enum import StrEnum
from hashlib import sha256
from pathlib import Path
from typing import cast

import tools.render_phase6_core_sam_staging as core_staging
from tools.render_phase6_core_sam_staging import (
    DEFAULT_ARTIFACT_ROOT,
    DEFAULT_DEPLOYMENT_ROOT,
    ROOT,
    Phase6CoreSamStagingBinding,
)

CAPACITY_RELEASED_TEMPLATE_OUTPUT = Path(
    ".mr_lister_private/phase6-core-runtime-transition/"
    "template.core-capacity-released-inert.local.json"
)
ACTIVE_DRAFT_ONLY_TEMPLATE_OUTPUT = Path(
    ".mr_lister_private/phase6-core-runtime-transition/"
    "template.core-runtime-active-draft-only.local.json"
)

_GENERIC_ERROR = "Phase 6 core-runtime transition configuration is invalid"
_STAGED_READINESS = "CORE_RELEASE_BOUND_STAGED"
_STAGED_DESCRIPTION = (
    "The exact sealed backend release is staged fail-closed; runtime and web traffic "
    "activation remain separate reviewed gates."
)
_TRANSITION_FORMAT = "mr-lister-phase6-core-runtime-transition-v1"
_METADATA_KEY = "MrListerPhase6CoreRuntimeStaging"

_MAINTENANCE_FUNCTIONS = frozenset(
    {
        "SourceVersionRetentionFunction",
        "StuckExecutionRecoveryFunction",
        "TerminalOperationalCleanupFunction",
    }
)
_FUNCTIONS = frozenset(
    {
        "DispatcherFunction",
        "PreparationDispatchFunction",
        "ProviderDraftFunction",
        "SettlementFunction",
        "SourceVersionRetentionFunction",
        "StuckExecutionRecoveryFunction",
        "TerminalOperationalCleanupFunction",
    }
)
_SAM_TRIGGER_SPECS = (
    ("DispatcherFunction", "DueWorkSweep", "Schedule"),
    ("DispatcherFunction", "OperationalStateChanges", "DynamoDB"),
    ("SourceVersionRetentionFunction", "SourceVersionRetentionSweep", "Schedule"),
    (
        "TerminalOperationalCleanupFunction",
        "TerminalOperationalCleanupSweep",
        "Schedule",
    ),
)
_EVENT_RULE_SPECS = ("StuckExecutionRecoveryScheduleRule",)
_WEB_RESOURCE_PREFIXES = (
    "AWS::ApiGateway::",
    "AWS::ApiGatewayV2::",
    "AWS::CertificateManager::",
    "AWS::CloudFront::",
    "AWS::Cognito::",
    "AWS::Route53::",
    "AWS::Serverless::Api",
    "AWS::Serverless::HttpApi",
)


class Phase6CoreRuntimeTransitionError(RuntimeError):
    """A value-free failure for a drifting or broadened transition."""


class Phase6CoreRuntimeTransitionTarget(StrEnum):
    """The only two core-runtime transition states this renderer may produce."""

    CAPACITY_RELEASED_INERT = "capacity-released-inert"
    BACKEND_ACTIVE_DRAFT_ONLY = "backend-active-draft-only"

    @property
    def readiness(self) -> str:
        if self is self.CAPACITY_RELEASED_INERT:
            return "CORE_CAPACITY_RELEASED_INERT"
        return "CORE_RUNTIME_ACTIVE_DRAFT_ONLY"

    @property
    def mode(self) -> str:
        if self is self.CAPACITY_RELEASED_INERT:
            return "CAPACITY_RELEASED_INERT"
        return "ACTIVE_DRAFT_ONLY"

    @property
    def description(self) -> str:
        if self is self.CAPACITY_RELEASED_INERT:
            return (
                "The exact sealed backend release has unreserved capacity but remains "
                "scaffolded with every reviewed trigger disabled."
            )
        return (
            "The exact sealed backend release is active for draft-only execution; publication, "
            "order, fulfillment, and the seller web surface remain absent."
        )


@dataclass(frozen=True, slots=True)
class _Change:
    before_present: bool
    before: object
    after_present: bool
    after: object


def render_phase6_core_runtime_transition(
    binding: Phase6CoreSamStagingBinding,
    *,
    target: Phase6CoreRuntimeTransitionTarget | str,
    foundation_binding_path: Path,
    agentcore_endpoint_observation_path: Path,
    agentcore_object_evidence_path: Path,
    agentcore_runtime_v1_evidence_path: Path,
    lambda_object_evidence_path: Path,
    repository_root: Path = ROOT,
    deployment_root: Path = DEFAULT_DEPLOYMENT_ROOT,
    artifact_root: Path = DEFAULT_ARTIFACT_ROOT,
) -> bytes:
    """Return canonical bytes for one exact transition from sealed core staging."""

    try:
        selected = _target(target)
        repository = repository_root.resolve(strict=True)
        staged_raw = core_staging.render_phase6_core_sam_staged_template(
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
        staged = _canonical_document(staged_raw)
        _validate_staged_authority(staged)

        capacity_released = _render_capacity_released_inert(
            staged,
            staged_sha256=sha256(staged_raw).hexdigest(),
        )
        transitioned = (
            capacity_released
            if selected is Phase6CoreRuntimeTransitionTarget.CAPACITY_RELEASED_INERT
            else _render_backend_active_draft_only(capacity_released)
        )
        _validate_transition(
            staged,
            transitioned,
            target=selected,
            staged_sha256=sha256(staged_raw).hexdigest(),
        )
        return _canonical_json(transitioned)
    except Phase6CoreRuntimeTransitionError:
        raise
    except Exception:
        raise Phase6CoreRuntimeTransitionError(_GENERIC_ERROR) from None


def write_phase6_core_runtime_transition(
    binding: Phase6CoreSamStagingBinding,
    *,
    target: Phase6CoreRuntimeTransitionTarget | str,
    foundation_binding_path: Path,
    agentcore_endpoint_observation_path: Path,
    agentcore_object_evidence_path: Path,
    agentcore_runtime_v1_evidence_path: Path,
    lambda_object_evidence_path: Path,
    repository_root: Path = ROOT,
    deployment_root: Path = DEFAULT_DEPLOYMENT_ROOT,
    artifact_root: Path = DEFAULT_ARTIFACT_ROOT,
) -> Path:
    """Create one fixed private output and refuse every existing destination."""

    try:
        selected = _target(target)
        repository = repository_root.resolve(strict=True)
        destination = _output_destination(repository, selected)
        if destination.exists() or destination.is_symlink():
            raise ValueError
        raw = render_phase6_core_runtime_transition(
            binding,
            target=selected,
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
    except Phase6CoreRuntimeTransitionError:
        raise
    except Exception:
        raise Phase6CoreRuntimeTransitionError(_GENERIC_ERROR) from None


def verify_rendered_phase6_core_runtime_transition(
    binding: Phase6CoreSamStagingBinding,
    *,
    target: Phase6CoreRuntimeTransitionTarget | str,
    foundation_binding_path: Path,
    agentcore_endpoint_observation_path: Path,
    agentcore_object_evidence_path: Path,
    agentcore_runtime_v1_evidence_path: Path,
    lambda_object_evidence_path: Path,
    repository_root: Path = ROOT,
    deployment_root: Path = DEFAULT_DEPLOYMENT_ROOT,
    artifact_root: Path = DEFAULT_ARTIFACT_ROOT,
) -> None:
    """Recompute every sealed input and require byte-identical fixed output."""

    try:
        selected = _target(target)
        repository = repository_root.resolve(strict=True)
        destination = _output_destination(repository, selected)
        expected = render_phase6_core_runtime_transition(
            binding,
            target=selected,
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
    except Phase6CoreRuntimeTransitionError:
        raise
    except Exception:
        raise Phase6CoreRuntimeTransitionError(_GENERIC_ERROR) from None


def _validate_staged_authority(document: Mapping[str, object]) -> None:
    core_staging.verify_core_runtime_dependency_closure(document)
    core_staging.verify_phase6_core_sam_staged_inertness(document)
    if _function_ids(document) != _FUNCTIONS:
        raise ValueError
    if _reserved_concurrency_inventory(document) != {
        logical_id: 0 for logical_id in _MAINTENANCE_FUNCTIONS
    }:
        raise ValueError
    if _trigger_inventory(document) != _disabled_trigger_inventory():
        raise ValueError
    if _scaffold_marker(document) != "true":
        raise ValueError
    if _deployment_readiness(document) != {
        "Description": _STAGED_DESCRIPTION,
        "Value": _STAGED_READINESS,
    }:
        raise ValueError
    metadata = _transition_metadata(document)
    if (
        metadata.get("Format") != "mr-lister-phase6-core-sam-staged-v1"
        or metadata.get("Mode") != "STAGED_FAIL_CLOSED"
        or metadata.get("Readiness") != _STAGED_READINESS
        or metadata.get("DisabledTriggers") != _disabled_trigger_inventory()
        or "ActiveTriggers" in metadata
        or "StagedTemplateSha256" in metadata
    ):
        raise ValueError
    _require_no_web_resources(document)


def _render_capacity_released_inert(
    staged: Mapping[str, object],
    *,
    staged_sha256: str,
) -> dict[str, object]:
    rendered = deepcopy(dict(staged))
    resources = _resources(rendered)
    for logical_id in _MAINTENANCE_FUNCTIONS:
        properties = _resource_properties(resources, logical_id)
        if properties.pop("ReservedConcurrentExecutions", None) != 0:
            raise ValueError
    _set_readiness(
        rendered,
        Phase6CoreRuntimeTransitionTarget.CAPACITY_RELEASED_INERT,
    )
    metadata = _transition_metadata(rendered)
    metadata["Format"] = _TRANSITION_FORMAT
    metadata["Mode"] = Phase6CoreRuntimeTransitionTarget.CAPACITY_RELEASED_INERT.mode
    metadata["Readiness"] = Phase6CoreRuntimeTransitionTarget.CAPACITY_RELEASED_INERT.readiness
    metadata["StagedTemplateSha256"] = staged_sha256
    return rendered


def _render_backend_active_draft_only(
    capacity_released: Mapping[str, object],
) -> dict[str, object]:
    rendered = deepcopy(dict(capacity_released))
    if _reserved_concurrency_inventory(rendered):
        raise ValueError
    variables = _global_variables(rendered)
    if variables.get("MR_LISTER_PHASE6_SCAFFOLD_ONLY") != "true":
        raise ValueError
    variables["MR_LISTER_PHASE6_SCAFFOLD_ONLY"] = "false"
    _set_trigger_states(rendered, enabled=True)
    _set_readiness(
        rendered,
        Phase6CoreRuntimeTransitionTarget.BACKEND_ACTIVE_DRAFT_ONLY,
    )
    metadata = _transition_metadata(rendered)
    if metadata.pop("DisabledTriggers", None) != _disabled_trigger_inventory():
        raise ValueError
    metadata["ActiveTriggers"] = _active_trigger_inventory()
    metadata["Mode"] = Phase6CoreRuntimeTransitionTarget.BACKEND_ACTIVE_DRAFT_ONLY.mode
    metadata["Readiness"] = Phase6CoreRuntimeTransitionTarget.BACKEND_ACTIVE_DRAFT_ONLY.readiness
    return rendered


def _validate_transition(
    staged: Mapping[str, object],
    transitioned: Mapping[str, object],
    *,
    target: Phase6CoreRuntimeTransitionTarget,
    staged_sha256: str,
) -> None:
    core_staging.verify_core_runtime_dependency_closure(transitioned)
    _require_no_web_resources(transitioned)
    if set(_resources(transitioned)) != set(_resources(staged)):
        raise ValueError
    if _function_ids(transitioned) != _FUNCTIONS or _reserved_concurrency_inventory(transitioned):
        raise ValueError
    expected_triggers = (
        _disabled_trigger_inventory()
        if target is Phase6CoreRuntimeTransitionTarget.CAPACITY_RELEASED_INERT
        else _active_trigger_inventory()
    )
    if _trigger_inventory(transitioned) != expected_triggers:
        raise ValueError
    expected_marker = (
        "true" if target is Phase6CoreRuntimeTransitionTarget.CAPACITY_RELEASED_INERT else "false"
    )
    if _scaffold_marker(transitioned) != expected_marker:
        raise ValueError
    if _deployment_readiness(transitioned) != {
        "Description": target.description,
        "Value": target.readiness,
    }:
        raise ValueError
    metadata = _transition_metadata(transitioned)
    if (
        metadata.get("Format") != _TRANSITION_FORMAT
        or metadata.get("Mode") != target.mode
        or metadata.get("Readiness") != target.readiness
        or metadata.get("StagedTemplateSha256") != staged_sha256
    ):
        raise ValueError
    if target is Phase6CoreRuntimeTransitionTarget.CAPACITY_RELEASED_INERT:
        if (
            metadata.get("DisabledTriggers") != _disabled_trigger_inventory()
            or "ActiveTriggers" in metadata
        ):
            raise ValueError
    elif (
        metadata.get("ActiveTriggers") != _active_trigger_inventory()
        or "DisabledTriggers" in metadata
    ):
        raise ValueError

    actual_changes: dict[tuple[str, ...], _Change] = {}
    _collect_changes(staged, transitioned, path=(), changes=actual_changes)
    if actual_changes != _expected_changes(target, staged_sha256=staged_sha256):
        raise ValueError


def _expected_changes(
    target: Phase6CoreRuntimeTransitionTarget,
    *,
    staged_sha256: str,
) -> dict[tuple[str, ...], _Change]:
    changes = {
        ("Resources", logical_id, "Properties", "ReservedConcurrentExecutions"): _Change(
            True,
            0,
            False,
            None,
        )
        for logical_id in _MAINTENANCE_FUNCTIONS
    }
    changes.update(
        {
            ("Outputs", "DeploymentReadiness", "Description"): _Change(
                True,
                _STAGED_DESCRIPTION,
                True,
                target.description,
            ),
            ("Outputs", "DeploymentReadiness", "Value"): _Change(
                True,
                _STAGED_READINESS,
                True,
                target.readiness,
            ),
            ("Metadata", _METADATA_KEY, "Format"): _Change(
                True,
                "mr-lister-phase6-core-sam-staged-v1",
                True,
                _TRANSITION_FORMAT,
            ),
            ("Metadata", _METADATA_KEY, "Mode"): _Change(
                True,
                "STAGED_FAIL_CLOSED",
                True,
                target.mode,
            ),
            ("Metadata", _METADATA_KEY, "Readiness"): _Change(
                True,
                _STAGED_READINESS,
                True,
                target.readiness,
            ),
            ("Metadata", _METADATA_KEY, "StagedTemplateSha256"): _Change(
                False,
                None,
                True,
                staged_sha256,
            ),
        }
    )
    if target is Phase6CoreRuntimeTransitionTarget.BACKEND_ACTIVE_DRAFT_ONLY:
        changes[
            (
                "Globals",
                "Function",
                "Environment",
                "Variables",
                "MR_LISTER_PHASE6_SCAFFOLD_ONLY",
            )
        ] = _Change(True, "true", True, "false")
        for logical_id, event_name, _event_type in _SAM_TRIGGER_SPECS:
            changes[
                (
                    "Resources",
                    logical_id,
                    "Properties",
                    "Events",
                    event_name,
                    "Properties",
                    "Enabled",
                )
            ] = _Change(True, False, True, True)
        for logical_id in _EVENT_RULE_SPECS:
            changes[("Resources", logical_id, "Properties", "State")] = _Change(
                True,
                "DISABLED",
                True,
                "ENABLED",
            )
        changes[("Metadata", _METADATA_KEY, "DisabledTriggers")] = _Change(
            True,
            _disabled_trigger_inventory(),
            False,
            None,
        )
        changes[("Metadata", _METADATA_KEY, "ActiveTriggers")] = _Change(
            False,
            None,
            True,
            _active_trigger_inventory(),
        )
    return changes


def _collect_changes(
    before: object,
    after: object,
    *,
    path: tuple[str, ...],
    changes: dict[tuple[str, ...], _Change],
) -> None:
    if isinstance(before, Mapping) and isinstance(after, Mapping):
        if any(not isinstance(key, str) for key in before) or any(
            not isinstance(key, str) for key in after
        ):
            raise ValueError
        before_keys = set(before)
        after_keys = set(after)
        for key in sorted(before_keys | after_keys):
            child = (*path, key)
            if key not in before:
                changes[child] = _Change(False, None, True, deepcopy(after[key]))
            elif key not in after:
                changes[child] = _Change(True, deepcopy(before[key]), False, None)
            else:
                _collect_changes(before[key], after[key], path=child, changes=changes)
        return
    if before != after:
        changes[path] = _Change(True, deepcopy(before), True, deepcopy(after))


def _set_trigger_states(document: Mapping[str, object], *, enabled: bool) -> None:
    resources = _resources(document)
    expected = _disabled_trigger_inventory() if enabled else _active_trigger_inventory()
    if _trigger_inventory(document) != expected:
        raise ValueError
    for logical_id, event_name, event_type in _SAM_TRIGGER_SPECS:
        properties = _resource_properties(resources, logical_id)
        events = properties.get("Events")
        if not isinstance(events, Mapping):
            raise ValueError
        event = events.get(event_name)
        if not isinstance(event, Mapping) or event.get("Type") != event_type:
            raise ValueError
        event_properties = event.get("Properties")
        if not isinstance(event_properties, dict):
            raise ValueError
        event_properties["Enabled"] = enabled
    for logical_id in _EVENT_RULE_SPECS:
        _resource_properties(resources, logical_id)["State"] = "ENABLED" if enabled else "DISABLED"


def _set_readiness(
    document: Mapping[str, object],
    target: Phase6CoreRuntimeTransitionTarget,
) -> None:
    outputs = document.get("Outputs")
    if not isinstance(outputs, Mapping):
        raise ValueError
    readiness = outputs.get("DeploymentReadiness")
    if not isinstance(readiness, dict):
        raise ValueError
    readiness["Description"] = target.description
    readiness["Value"] = target.readiness


def _resources(document: Mapping[str, object]) -> Mapping[str, object]:
    resources = document.get("Resources")
    if not isinstance(resources, Mapping):
        raise ValueError
    return resources


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


def _function_ids(document: Mapping[str, object]) -> frozenset[str]:
    found: set[str] = set()
    for logical_id, resource in _resources(document).items():
        if not isinstance(logical_id, str) or not isinstance(resource, Mapping):
            raise ValueError
        if resource.get("Type") == "AWS::Serverless::Function":
            found.add(logical_id)
    return frozenset(found)


def _reserved_concurrency_inventory(document: Mapping[str, object]) -> dict[str, object]:
    inventory: dict[str, object] = {}
    resources = _resources(document)
    for logical_id in _function_ids(document):
        properties = _resource_properties(resources, logical_id)
        if "ReservedConcurrentExecutions" in properties:
            inventory[logical_id] = properties["ReservedConcurrentExecutions"]
    return inventory


def _trigger_inventory(document: Mapping[str, object]) -> dict[str, dict[str, object]]:
    inventory: dict[str, dict[str, object]] = {}
    for logical_id, resource in _resources(document).items():
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


def _disabled_trigger_inventory() -> dict[str, dict[str, object]]:
    return {
        **{
            f"{logical_id}.Events.{event_name}": {
                "Enabled": False,
                "Type": event_type,
            }
            for logical_id, event_name, event_type in _SAM_TRIGGER_SPECS
        },
        **{
            logical_id: {"State": "DISABLED", "Type": "AWS::Events::Rule"}
            for logical_id in _EVENT_RULE_SPECS
        },
    }


def _active_trigger_inventory() -> dict[str, dict[str, object]]:
    inventory = _disabled_trigger_inventory()
    for logical_id, event_name, _event_type in _SAM_TRIGGER_SPECS:
        inventory[f"{logical_id}.Events.{event_name}"]["Enabled"] = True
    for logical_id in _EVENT_RULE_SPECS:
        inventory[logical_id]["State"] = "ENABLED"
    return inventory


def _global_variables(document: Mapping[str, object]) -> dict[str, object]:
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
    if not isinstance(variables, dict):
        raise ValueError
    return variables


def _scaffold_marker(document: Mapping[str, object]) -> object:
    return _global_variables(document).get("MR_LISTER_PHASE6_SCAFFOLD_ONLY")


def _deployment_readiness(document: Mapping[str, object]) -> object:
    outputs = document.get("Outputs")
    if not isinstance(outputs, Mapping):
        raise ValueError
    return outputs.get("DeploymentReadiness")


def _transition_metadata(document: Mapping[str, object]) -> dict[str, object]:
    metadata = document.get("Metadata")
    if not isinstance(metadata, Mapping) or set(metadata) != {_METADATA_KEY}:
        raise ValueError
    transition = metadata.get(_METADATA_KEY)
    if not isinstance(transition, dict):
        raise ValueError
    return transition


def _require_no_web_resources(document: Mapping[str, object]) -> None:
    for resource in _resources(document).values():
        if not isinstance(resource, Mapping):
            raise ValueError
        resource_type = resource.get("Type")
        if not isinstance(resource_type, str) or resource_type.startswith(_WEB_RESOURCE_PREFIXES):
            raise ValueError


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


def _target(value: Phase6CoreRuntimeTransitionTarget | str) -> Phase6CoreRuntimeTransitionTarget:
    if isinstance(value, Phase6CoreRuntimeTransitionTarget):
        return value
    if not isinstance(value, str) or not value or value != value.strip():
        raise Phase6CoreRuntimeTransitionError(_GENERIC_ERROR)
    try:
        return Phase6CoreRuntimeTransitionTarget(value)
    except ValueError:
        raise Phase6CoreRuntimeTransitionError(_GENERIC_ERROR) from None


def _output_destination(
    repository: Path,
    target: Phase6CoreRuntimeTransitionTarget,
) -> Path:
    relative = (
        CAPACITY_RELEASED_TEMPLATE_OUTPUT
        if target is Phase6CoreRuntimeTransitionTarget.CAPACITY_RELEASED_INERT
        else ACTIVE_DRAFT_ONLY_TEMPLATE_OUTPUT
    )
    destination = repository / relative
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
    parser.add_argument(
        "--target",
        required=True,
        choices=[target.value for target in Phase6CoreRuntimeTransitionTarget],
    )
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
    action.add_argument("--write", action="store_true")
    action.add_argument("--verify", action="store_true")
    arguments = parser.parse_args()
    try:
        binding = _binding_from_arguments(arguments)
        options = {
            "target": cast(str, arguments.target),
            "foundation_binding_path": arguments.foundation_binding,
            "agentcore_endpoint_observation_path": arguments.agentcore_endpoint_observation,
            "agentcore_object_evidence_path": arguments.agentcore_object_evidence,
            "agentcore_runtime_v1_evidence_path": arguments.agentcore_runtime_v1_evidence,
            "lambda_object_evidence_path": arguments.lambda_object_evidence,
            "deployment_root": arguments.deployment_root,
            "artifact_root": arguments.artifact_root,
        }
        if arguments.write:
            print(write_phase6_core_runtime_transition(binding, **options))
        else:
            verify_rendered_phase6_core_runtime_transition(binding, **options)
            print(_output_destination(ROOT, _target(arguments.target)))
    except Phase6CoreRuntimeTransitionError as error:
        parser.exit(2, f"{error}\n")


__all__ = [
    "ACTIVE_DRAFT_ONLY_TEMPLATE_OUTPUT",
    "CAPACITY_RELEASED_TEMPLATE_OUTPUT",
    "Phase6CoreRuntimeTransitionError",
    "Phase6CoreRuntimeTransitionTarget",
    "render_phase6_core_runtime_transition",
    "verify_rendered_phase6_core_runtime_transition",
    "write_phase6_core_runtime_transition",
]


if __name__ == "__main__":
    main()
