"""Offline verifier for one Phase 6 core-runtime transition change set.

The verifier joins six canonical JSON files: the predecessor Original and Processed
``get-template`` observations, the locally rendered target template, the raw
``describe-change-set --include-property-values`` observation, and the target Original and
Processed ``get-template`` observations.  It accepts only the two renderer-owned transitions:

* ``staged`` -> ``capacity-released-inert``;
* ``capacity-released-inert`` -> ``backend-active-draft-only``.

All input files use sorted, two-space-indented JSON with one trailing newline.  Every AWS response
wrapper is closed, every template has the exact reviewed resource inventory, and the only accepted
processed-template resource changes are the three capacity removals or the twelve activation
changes described by :mod:`tools.render_phase6_core_runtime_transition`.

``DescribeChangeSet`` does not return the create request's ``ChangeSetType``.  UPDATE is therefore
proved structurally: the capture names the exact existing stack, contains only ``Modify`` resource
changes with ``Replacement`` equal to ``False``, and contains no import, nested-stack, add, or
remove path.  This module imports no AWS SDK and starts no subprocess.
"""

from __future__ import annotations

import argparse
import json
import re
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from enum import StrEnum
from hashlib import sha256
from pathlib import Path
from typing import Final, cast

FORMAT: Final = "mr-lister-phase6-core-transition-change-set-v1"
ACCOUNT_ID: Final = "384627057108"
REGION: Final = "us-west-2"
STACK_NAME: Final = "mr-lister-phase6-dev"
STACK_ID: Final = (
    "arn:aws:cloudformation:us-west-2:384627057108:stack/mr-lister-phase6-dev/"
    "f3456970-9fdc-11f1-b448-06b81627db1d"
)

_GENERIC_ERROR = "Phase 6 core-runtime transition change-set evidence is invalid"
_MAX_INPUT_BYTES = 8 * 1024 * 1024
_HEX_64 = re.compile(r"^[a-f0-9]{64}$")
_CHANGE_SET_NAME = re.compile(r"^[A-Za-z][-A-Za-z0-9]{0,127}$")
_UUID = re.compile(r"^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$")
_PLACEHOLDER = re.compile(
    r"<[A-Z][A-Z0-9_]*>|__[A-Z][A-Z0-9_]*__|"
    r"\b(?:PLACEHOLDER|REPLACE_ME|CHANGEME)\b",
    re.IGNORECASE,
)

_METADATA_KEY = "MrListerPhase6CoreRuntimeStaging"
_STAGED_FORMAT = "mr-lister-phase6-core-sam-staged-v1"
_TRANSITION_FORMAT = "mr-lister-phase6-core-runtime-transition-v1"
_STAGED_DESCRIPTION = (
    "The exact sealed backend release is staged fail-closed; runtime and web traffic "
    "activation remain separate reviewed gates."
)
_CAPACITY_DESCRIPTION = (
    "The exact sealed backend release has unreserved capacity but remains scaffolded with every "
    "reviewed trigger disabled."
)
_ACTIVE_DESCRIPTION = (
    "The exact sealed backend release is active for draft-only execution; publication, order, "
    "fulfillment, and the seller web surface remain absent."
)

_FUNCTIONS: Final = (
    "DispatcherFunction",
    "PreparationDispatchFunction",
    "ProviderDraftFunction",
    "SettlementFunction",
    "SourceVersionRetentionFunction",
    "StuckExecutionRecoveryFunction",
    "TerminalOperationalCleanupFunction",
)
_MAINTENANCE_FUNCTIONS: Final = (
    "SourceVersionRetentionFunction",
    "StuckExecutionRecoveryFunction",
    "TerminalOperationalCleanupFunction",
)
_STATE_MACHINES: Final = (
    "PrepareStateMachine",
    "ReconcileProductStateMachine",
    "RefreshEconomicsStateMachine",
    "SynchronizeProductStateMachine",
)
_PROCESSED_EVENT_RULES: Final = (
    "DispatcherFunctionDueWorkSweep",
    "SourceVersionRetentionFunctionSourceVersionRetentionSweep",
    "StuckExecutionRecoveryScheduleRule",
    "TerminalOperationalCleanupFunctionTerminalOperationalCleanupSweep",
)
_DISPATCHER_MAPPING: Final = "DispatcherFunctionOperationalStateChanges"

_ORIGINAL_TRIGGER_PATHS: Final = (
    ("DispatcherFunction", "DueWorkSweep", "Schedule"),
    ("DispatcherFunction", "OperationalStateChanges", "DynamoDB"),
    ("SourceVersionRetentionFunction", "SourceVersionRetentionSweep", "Schedule"),
    (
        "TerminalOperationalCleanupFunction",
        "TerminalOperationalCleanupSweep",
        "Schedule",
    ),
)

_PARAMETERS: Final = (
    "AgentCoreRuntimeArn",
    "AgentCoreRuntimeBindingFingerprint",
    "AgentCoreRuntimeEndpointArn",
    "AgentCoreRuntimeQualifier",
    "AgentCoreRuntimeVersion",
    "ApplicationOrigin",
    "EnvironmentName",
    "PrintifySecretArn",
    "ReleaseFingerprint",
)
_STACK_TAGS: Final = {
    "DeploymentClass": "FOUNDATION_ONLY",
    "Environment": "dev",
    "Project": "MrLister",
}

_ORIGINAL_RESOURCE_TYPES: Final = {
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

_PROCESSED_RESOURCE_TYPES = dict(_ORIGINAL_RESOURCE_TYPES)
for _logical_id in _FUNCTIONS:
    _PROCESSED_RESOURCE_TYPES[_logical_id] = "AWS::Lambda::Function"
for _logical_id in _STATE_MACHINES:
    _PROCESSED_RESOURCE_TYPES[_logical_id] = "AWS::StepFunctions::StateMachine"
_PROCESSED_RESOURCE_TYPES.update(
    {
        "DispatcherFunctionDueWorkSweep": "AWS::Events::Rule",
        "DispatcherFunctionDueWorkSweepPermission": "AWS::Lambda::Permission",
        _DISPATCHER_MAPPING: "AWS::Lambda::EventSourceMapping",
        "SourceVersionRetentionFunctionSourceVersionRetentionSweep": "AWS::Events::Rule",
        "SourceVersionRetentionFunctionSourceVersionRetentionSweepPermission": (
            "AWS::Lambda::Permission"
        ),
        "TerminalOperationalCleanupFunctionTerminalOperationalCleanupSweep": ("AWS::Events::Rule"),
        "TerminalOperationalCleanupFunctionTerminalOperationalCleanupSweepPermission": (
            "AWS::Lambda::Permission"
        ),
    }
)

_ORIGINAL_TEMPLATE_KEYS: Final = {
    "AWSTemplateFormatVersion",
    "Description",
    "Globals",
    "Metadata",
    "Outputs",
    "Parameters",
    "Resources",
    "Transform",
}
_PROCESSED_TEMPLATE_KEYS: Final = {
    "AWSTemplateFormatVersion",
    "Description",
    "Metadata",
    "Outputs",
    "Parameters",
    "Resources",
}
_GET_TEMPLATE_KEYS: Final = {"StagesAvailable", "TemplateBody"}
_CHANGE_SET_REQUIRED_KEYS: Final = {
    "Capabilities",
    "ChangeSetId",
    "ChangeSetName",
    "Changes",
    "CreationTime",
    "DeploymentConfig",
    "Description",
    "ExecutionStatus",
    "IncludeNestedStacks",
    "NotificationARNs",
    "Parameters",
    "RollbackConfiguration",
    "StackId",
    "StackName",
    "Status",
    "Tags",
}
_CHANGE_SET_OPTIONAL_KEYS: Final = {
    "DeploymentMode",
    "ImportExistingResources",
    "OnStackFailure",
    "ParentChangeSetId",
    "RootChangeSetId",
    "StackDriftStatus",
    "StatusReason",
}
_CHANGE_SET_KEYS: Final = _CHANGE_SET_REQUIRED_KEYS | _CHANGE_SET_OPTIONAL_KEYS
_RESOURCE_CHANGE_KEYS: Final = {
    "Action",
    "AfterContext",
    "BeforeContext",
    "Details",
    "LogicalResourceId",
    "PhysicalResourceId",
    "Replacement",
    "ResourceType",
    "Scope",
}
_DETAIL_KEYS: Final = {"ChangeSource", "Evaluation", "Target"}
_DETAIL_TARGET_REQUIRED_KEYS: Final = {
    "Attribute",
    "AttributeChangeType",
    "Name",
    "Path",
    "RequiresRecreation",
}
_DETAIL_TARGET_ALLOWED_KEYS: Final = _DETAIL_TARGET_REQUIRED_KEYS | {
    "AfterValue",
    "BeforeValue",
}


class Phase6CoreTransitionChangeSetError(RuntimeError):
    """Value-free failure for a broadened, malformed, or mismatched transition."""


class Phase6CoreRuntimeTransitionTarget(StrEnum):
    """The only transition targets accepted by this standalone offline verifier."""

    CAPACITY_RELEASED_INERT = "capacity-released-inert"
    BACKEND_ACTIVE_DRAFT_ONLY = "backend-active-draft-only"


@dataclass(frozen=True, slots=True)
class VerifiedPhase6CoreTransitionChangeSet:
    """Frozen identity emitted only after the complete offline join succeeds."""

    format: str
    transition_target: str
    predecessor_mode: str
    target_mode: str
    stack_id: str
    change_set_id: str
    change_set_name: str
    change_set_type: str
    changed_resources: tuple[str, ...]
    original_resource_count: int
    processed_resource_count: int
    predecessor_original_sha256: str
    predecessor_processed_sha256: str
    target_local_sha256: str
    change_set_sha256: str
    target_original_sha256: str
    target_processed_sha256: str
    canonical_sha256: str


@dataclass(frozen=True, slots=True)
class _ModeContract:
    mode: str
    readiness: str
    description: str
    template_format: str
    scaffold_only: bool
    triggers_enabled: bool
    maintenance_concurrency: int | None


_MODE_CONTRACTS: Final = {
    "staged": _ModeContract(
        mode="STAGED_FAIL_CLOSED",
        readiness="CORE_RELEASE_BOUND_STAGED",
        description=_STAGED_DESCRIPTION,
        template_format=_STAGED_FORMAT,
        scaffold_only=True,
        triggers_enabled=False,
        maintenance_concurrency=0,
    ),
    "capacity-released-inert": _ModeContract(
        mode="CAPACITY_RELEASED_INERT",
        readiness="CORE_CAPACITY_RELEASED_INERT",
        description=_CAPACITY_DESCRIPTION,
        template_format=_TRANSITION_FORMAT,
        scaffold_only=True,
        triggers_enabled=False,
        maintenance_concurrency=None,
    ),
    "backend-active-draft-only": _ModeContract(
        mode="ACTIVE_DRAFT_ONLY",
        readiness="CORE_RUNTIME_ACTIVE_DRAFT_ONLY",
        description=_ACTIVE_DESCRIPTION,
        template_format=_TRANSITION_FORMAT,
        scaffold_only=False,
        triggers_enabled=True,
        maintenance_concurrency=None,
    ),
}


@dataclass(frozen=True, slots=True)
class _Diff:
    before_present: bool
    before: object
    after_present: bool
    after: object


def canonical_phase6_core_transition_change_set(value: object) -> bytes:
    """Return the only accepted JSON byte representation for inputs and verified records."""

    try:
        return (
            json.dumps(value, allow_nan=False, indent=2, separators=(",", ": "), sort_keys=True)
            + "\n"
        ).encode("utf-8")
    except Exception:
        raise Phase6CoreTransitionChangeSetError(_GENERIC_ERROR) from None


def verify_phase6_core_transition_change_set(
    *,
    target: Phase6CoreRuntimeTransitionTarget | str,
    predecessor_original_template_observation_path: Path,
    predecessor_processed_template_observation_path: Path,
    target_template_path: Path,
    change_set_observation_path: Path,
    target_original_template_observation_path: Path,
    target_processed_template_observation_path: Path,
) -> VerifiedPhase6CoreTransitionChangeSet:
    """Verify one exact transition using canonical local captures only."""

    try:
        selected = _target(target)
        predecessor_mode, target_mode = _transition_modes(selected)

        predecessor_original_raw, predecessor_original_observation = _load_canonical_mapping(
            predecessor_original_template_observation_path
        )
        predecessor_processed_raw, predecessor_processed_observation = _load_canonical_mapping(
            predecessor_processed_template_observation_path
        )
        target_local_raw, target_local = _load_canonical_mapping(target_template_path)
        change_set_raw, change_set = _load_canonical_mapping(change_set_observation_path)
        target_original_raw, target_original_observation = _load_canonical_mapping(
            target_original_template_observation_path
        )
        target_processed_raw, target_processed_observation = _load_canonical_mapping(
            target_processed_template_observation_path
        )

        predecessor_original = _get_template_body(predecessor_original_observation)
        predecessor_processed = _get_template_body(predecessor_processed_observation)
        target_original = _get_template_body(target_original_observation)
        target_processed = _get_template_body(target_processed_observation)
        if target_original != target_local:
            raise ValueError

        _verify_original_template(predecessor_original, predecessor_mode)
        _verify_processed_template(predecessor_processed, predecessor_mode)
        _verify_original_template(target_local, target_mode)
        _verify_original_template(target_original, target_mode)
        _verify_processed_template(target_processed, target_mode)
        _verify_original_processed_join(predecessor_original, predecessor_processed)
        _verify_original_processed_join(target_original, target_processed)

        _verify_original_transition(
            predecessor_original,
            target_original,
            selected=selected,
        )
        changed_resources = _verify_processed_transition(
            predecessor_processed,
            target_processed,
            selected=selected,
            predecessor_original=predecessor_original,
        )
        change_set_id, change_set_name = _verify_change_set(
            change_set,
            target_original=target_original,
            predecessor_processed=predecessor_processed,
            target_processed=target_processed,
            changed_resources=changed_resources,
            selected=selected,
        )

        payload = {
            "change_set_id": change_set_id,
            "change_set_name": change_set_name,
            "change_set_sha256": sha256(change_set_raw).hexdigest(),
            "change_set_type": "UPDATE",
            "changed_resources": list(changed_resources),
            "format": FORMAT,
            "original_resource_count": 40,
            "predecessor_mode": predecessor_mode,
            "predecessor_original_sha256": sha256(predecessor_original_raw).hexdigest(),
            "predecessor_processed_sha256": sha256(predecessor_processed_raw).hexdigest(),
            "processed_resource_count": 47,
            "stack_id": STACK_ID,
            "target_local_sha256": sha256(target_local_raw).hexdigest(),
            "target_mode": target_mode,
            "target_original_sha256": sha256(target_original_raw).hexdigest(),
            "target_processed_sha256": sha256(target_processed_raw).hexdigest(),
            "transition_target": selected.value,
        }
        fingerprint = sha256(canonical_phase6_core_transition_change_set(payload)).hexdigest()
        return VerifiedPhase6CoreTransitionChangeSet(
            format=FORMAT,
            transition_target=selected.value,
            predecessor_mode=predecessor_mode,
            target_mode=target_mode,
            stack_id=STACK_ID,
            change_set_id=change_set_id,
            change_set_name=change_set_name,
            change_set_type="UPDATE",
            changed_resources=changed_resources,
            original_resource_count=40,
            processed_resource_count=47,
            predecessor_original_sha256=payload["predecessor_original_sha256"],
            predecessor_processed_sha256=payload["predecessor_processed_sha256"],
            target_local_sha256=payload["target_local_sha256"],
            change_set_sha256=payload["change_set_sha256"],
            target_original_sha256=payload["target_original_sha256"],
            target_processed_sha256=payload["target_processed_sha256"],
            canonical_sha256=fingerprint,
        )
    except Phase6CoreTransitionChangeSetError:
        raise
    except Exception:
        raise Phase6CoreTransitionChangeSetError(_GENERIC_ERROR) from None


def _transition_modes(
    target: Phase6CoreRuntimeTransitionTarget,
) -> tuple[str, str]:
    if target is Phase6CoreRuntimeTransitionTarget.CAPACITY_RELEASED_INERT:
        return "staged", "capacity-released-inert"
    return "capacity-released-inert", "backend-active-draft-only"


def _target(
    value: Phase6CoreRuntimeTransitionTarget | str,
) -> Phase6CoreRuntimeTransitionTarget:
    if isinstance(value, Phase6CoreRuntimeTransitionTarget):
        return value
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError
    return Phase6CoreRuntimeTransitionTarget(value)


def _get_template_body(observation: Mapping[str, object]) -> Mapping[str, object]:
    if set(observation) != _GET_TEMPLATE_KEYS:
        raise ValueError
    if observation.get("StagesAvailable") != ["Original", "Processed"]:
        raise ValueError
    body = observation.get("TemplateBody")
    if not isinstance(body, Mapping):
        raise ValueError
    return body


def _verify_original_template(document: Mapping[str, object], mode: str) -> None:
    if (
        set(document) != _ORIGINAL_TEMPLATE_KEYS
        or document.get("AWSTemplateFormatVersion") != "2010-09-09"
        or document.get("Transform") != "AWS::Serverless-2016-10-31"
    ):
        raise ValueError
    _verify_resource_types(document, _ORIGINAL_RESOURCE_TYPES)
    _verify_parameter_defaults(document)
    _verify_mode_metadata_and_output(document, mode)
    contract = _MODE_CONTRACTS[mode]

    globals_value = _mapping(document, "Globals")
    function = _mapping(globals_value, "Function")
    environment = _mapping(function, "Environment")
    variables = _mapping(environment, "Variables")
    expected_marker = "true" if contract.scaffold_only else "false"
    if variables.get("MR_LISTER_PHASE6_SCAFFOLD_ONLY") != expected_marker:
        raise ValueError

    resources = _mapping(document, "Resources")
    for logical_id in _MAINTENANCE_FUNCTIONS:
        properties = _resource_properties(resources, logical_id)
        if contract.maintenance_concurrency is None:
            if "ReservedConcurrentExecutions" in properties:
                raise ValueError
        elif not _exact_int_equal(
            properties.get("ReservedConcurrentExecutions"),
            contract.maintenance_concurrency,
        ):
            raise ValueError
    for logical_id, event_name, event_type in _ORIGINAL_TRIGGER_PATHS:
        properties = _resource_properties(resources, logical_id)
        events = _mapping(properties, "Events")
        event = _mapping(events, event_name)
        event_properties = _mapping(event, "Properties")
        if event.get("Type") != event_type or event_properties.get("Enabled") is not (
            contract.triggers_enabled
        ):
            raise ValueError
    recovery = _resource_properties(resources, "StuckExecutionRecoveryScheduleRule")
    if recovery.get("State") != ("ENABLED" if contract.triggers_enabled else "DISABLED"):
        raise ValueError


def _verify_processed_template(document: Mapping[str, object], mode: str) -> None:
    if (
        set(document) != _PROCESSED_TEMPLATE_KEYS
        or document.get("AWSTemplateFormatVersion") != "2010-09-09"
    ):
        raise ValueError
    _verify_resource_types(document, _PROCESSED_RESOURCE_TYPES)
    _verify_parameter_defaults(document)
    _verify_mode_metadata_and_output(document, mode)
    contract = _MODE_CONTRACTS[mode]
    resources = _mapping(document, "Resources")
    expected_marker = "true" if contract.scaffold_only else "false"
    for logical_id in _FUNCTIONS:
        variables = _mapping(
            _mapping(_resource_properties(resources, logical_id), "Environment"), "Variables"
        )
        if variables.get("MR_LISTER_PHASE6_SCAFFOLD_ONLY") != expected_marker:
            raise ValueError
    for logical_id in _MAINTENANCE_FUNCTIONS:
        properties = _resource_properties(resources, logical_id)
        if contract.maintenance_concurrency is None:
            if "ReservedConcurrentExecutions" in properties:
                raise ValueError
        elif not _exact_int_equal(
            properties.get("ReservedConcurrentExecutions"),
            contract.maintenance_concurrency,
        ):
            raise ValueError
    expected_state = "ENABLED" if contract.triggers_enabled else "DISABLED"
    for logical_id in _PROCESSED_EVENT_RULES:
        if _resource_properties(resources, logical_id).get("State") != expected_state:
            raise ValueError
    if _resource_properties(resources, _DISPATCHER_MAPPING).get("Enabled") is not (
        contract.triggers_enabled
    ):
        raise ValueError


def _verify_resource_types(document: Mapping[str, object], expected: Mapping[str, str]) -> None:
    resources = _mapping(document, "Resources")
    actual: dict[str, str] = {}
    for logical_id, resource in resources.items():
        if not isinstance(logical_id, str) or not isinstance(resource, Mapping):
            raise ValueError
        resource_type = resource.get("Type")
        if not isinstance(resource_type, str):
            raise ValueError
        actual[logical_id] = resource_type
    if actual != expected:
        raise ValueError


def _verify_parameter_defaults(document: Mapping[str, object]) -> dict[str, str]:
    parameters = _mapping(document, "Parameters")
    if set(parameters) != set(_PARAMETERS):
        raise ValueError
    result: dict[str, str] = {}
    for name in _PARAMETERS:
        definition = _mapping(parameters, name)
        default = definition.get("Default")
        if (
            not isinstance(default, str)
            or not default
            or default != default.strip()
            or definition.get("AllowedValues") != [default]
        ):
            raise ValueError
        result[name] = default
    return result


def _verify_mode_metadata_and_output(document: Mapping[str, object], mode: str) -> None:
    contract = _MODE_CONTRACTS[mode]
    metadata_container = _mapping(document, "Metadata")
    if set(metadata_container) != {_METADATA_KEY}:
        raise ValueError
    metadata = _mapping(metadata_container, _METADATA_KEY)
    if (
        metadata.get("Format") != contract.template_format
        or metadata.get("Mode") != contract.mode
        or metadata.get("Readiness") != contract.readiness
    ):
        raise ValueError
    target = _mapping(metadata, "Target")
    if target != {"AccountId": ACCOUNT_ID, "Environment": "dev", "Region": REGION}:
        raise ValueError
    foundation = _mapping(metadata, "Foundation")
    if foundation.get("StackId") != STACK_ID or foundation.get("StackName") != STACK_NAME:
        raise ValueError
    disabled = _trigger_inventory(False)
    active = _trigger_inventory(True)
    if mode == "staged":
        if (
            metadata.get("DisabledTriggers") != disabled
            or "ActiveTriggers" in metadata
            or "StagedTemplateSha256" in metadata
        ):
            raise ValueError
    elif mode == "capacity-released-inert":
        if (
            metadata.get("DisabledTriggers") != disabled
            or "ActiveTriggers" in metadata
            or _HEX_64.fullmatch(cast(str, metadata.get("StagedTemplateSha256", ""))) is None
        ):
            raise ValueError
    elif (
        metadata.get("ActiveTriggers") != active
        or "DisabledTriggers" in metadata
        or _HEX_64.fullmatch(cast(str, metadata.get("StagedTemplateSha256", ""))) is None
    ):
        raise ValueError
    outputs = _mapping(document, "Outputs")
    readiness = _mapping(outputs, "DeploymentReadiness")
    if set(readiness) != {"Description", "Value"} or readiness != {
        "Description": contract.description,
        "Value": contract.readiness,
    }:
        raise ValueError


def _verify_original_processed_join(
    original: Mapping[str, object], processed: Mapping[str, object]
) -> None:
    for key in (
        "AWSTemplateFormatVersion",
        "Description",
        "Metadata",
        "Outputs",
        "Parameters",
    ):
        if original.get(key) != processed.get(key):
            raise ValueError


def _verify_original_transition(
    predecessor: Mapping[str, object],
    target: Mapping[str, object],
    *,
    selected: Phase6CoreRuntimeTransitionTarget,
) -> None:
    changes = _changes(predecessor, target)
    expected = _expected_original_changes(
        selected,
        staged_sha256=_canonical_sha256(predecessor),
    )
    if changes != expected:
        raise ValueError


def _verify_processed_transition(
    predecessor: Mapping[str, object],
    target: Mapping[str, object],
    *,
    selected: Phase6CoreRuntimeTransitionTarget,
    predecessor_original: Mapping[str, object],
) -> tuple[str, ...]:
    changes = _changes(predecessor, target)
    expected = _expected_processed_changes(
        selected,
        staged_sha256=_canonical_sha256(predecessor_original),
    )
    if changes != expected:
        raise ValueError
    logical_ids = {path[1] for path in changes if len(path) >= 2 and path[0] == "Resources"}
    return tuple(sorted(logical_ids))


def _expected_original_changes(
    selected: Phase6CoreRuntimeTransitionTarget,
    *,
    staged_sha256: str,
) -> dict[tuple[str, ...], _Diff]:
    outside = _expected_outside_resource_changes(selected, staged_sha256=staged_sha256)
    if selected is Phase6CoreRuntimeTransitionTarget.CAPACITY_RELEASED_INERT:
        outside.update(
            {
                ("Resources", logical_id, "Properties", "ReservedConcurrentExecutions"): _Diff(
                    True, 0, False, None
                )
                for logical_id in _MAINTENANCE_FUNCTIONS
            }
        )
        return outside
    outside[
        (
            "Globals",
            "Function",
            "Environment",
            "Variables",
            "MR_LISTER_PHASE6_SCAFFOLD_ONLY",
        )
    ] = _Diff(True, "true", True, "false")
    for logical_id, event_name, _event_type in _ORIGINAL_TRIGGER_PATHS:
        outside[
            (
                "Resources",
                logical_id,
                "Properties",
                "Events",
                event_name,
                "Properties",
                "Enabled",
            )
        ] = _Diff(True, False, True, True)
    outside[("Resources", "StuckExecutionRecoveryScheduleRule", "Properties", "State")] = _Diff(
        True, "DISABLED", True, "ENABLED"
    )
    return outside


def _expected_processed_changes(
    selected: Phase6CoreRuntimeTransitionTarget,
    *,
    staged_sha256: str,
) -> dict[tuple[str, ...], _Diff]:
    outside = _expected_outside_resource_changes(selected, staged_sha256=staged_sha256)
    if selected is Phase6CoreRuntimeTransitionTarget.CAPACITY_RELEASED_INERT:
        outside.update(
            {
                ("Resources", logical_id, "Properties", "ReservedConcurrentExecutions"): _Diff(
                    True, 0, False, None
                )
                for logical_id in _MAINTENANCE_FUNCTIONS
            }
        )
        return outside
    for logical_id in _FUNCTIONS:
        outside[
            (
                "Resources",
                logical_id,
                "Properties",
                "Environment",
                "Variables",
                "MR_LISTER_PHASE6_SCAFFOLD_ONLY",
            )
        ] = _Diff(True, "true", True, "false")
    for logical_id in _PROCESSED_EVENT_RULES:
        outside[("Resources", logical_id, "Properties", "State")] = _Diff(
            True, "DISABLED", True, "ENABLED"
        )
    outside[("Resources", _DISPATCHER_MAPPING, "Properties", "Enabled")] = _Diff(
        True, False, True, True
    )
    return outside


def _expected_outside_resource_changes(
    selected: Phase6CoreRuntimeTransitionTarget,
    *,
    staged_sha256: str,
) -> dict[tuple[str, ...], _Diff]:
    path = ("Metadata", _METADATA_KEY)
    if selected is Phase6CoreRuntimeTransitionTarget.CAPACITY_RELEASED_INERT:
        return {
            (*path, "Format"): _Diff(True, _STAGED_FORMAT, True, _TRANSITION_FORMAT),
            (*path, "Mode"): _Diff(True, "STAGED_FAIL_CLOSED", True, "CAPACITY_RELEASED_INERT"),
            (*path, "Readiness"): _Diff(
                True, "CORE_RELEASE_BOUND_STAGED", True, "CORE_CAPACITY_RELEASED_INERT"
            ),
            (*path, "StagedTemplateSha256"): _Diff(False, None, True, staged_sha256),
            ("Outputs", "DeploymentReadiness", "Description"): _Diff(
                True, _STAGED_DESCRIPTION, True, _CAPACITY_DESCRIPTION
            ),
            ("Outputs", "DeploymentReadiness", "Value"): _Diff(
                True, "CORE_RELEASE_BOUND_STAGED", True, "CORE_CAPACITY_RELEASED_INERT"
            ),
        }
    return {
        (*path, "ActiveTriggers"): _Diff(False, None, True, _trigger_inventory(True)),
        (*path, "DisabledTriggers"): _Diff(True, _trigger_inventory(False), False, None),
        (*path, "Mode"): _Diff(True, "CAPACITY_RELEASED_INERT", True, "ACTIVE_DRAFT_ONLY"),
        (*path, "Readiness"): _Diff(
            True, "CORE_CAPACITY_RELEASED_INERT", True, "CORE_RUNTIME_ACTIVE_DRAFT_ONLY"
        ),
        ("Outputs", "DeploymentReadiness", "Description"): _Diff(
            True, _CAPACITY_DESCRIPTION, True, _ACTIVE_DESCRIPTION
        ),
        ("Outputs", "DeploymentReadiness", "Value"): _Diff(
            True, "CORE_CAPACITY_RELEASED_INERT", True, "CORE_RUNTIME_ACTIVE_DRAFT_ONLY"
        ),
    }


def _verify_change_set(
    observation: Mapping[str, object],
    *,
    target_original: Mapping[str, object],
    predecessor_processed: Mapping[str, object],
    target_processed: Mapping[str, object],
    changed_resources: tuple[str, ...],
    selected: Phase6CoreRuntimeTransitionTarget,
) -> tuple[str, str]:
    if not _CHANGE_SET_REQUIRED_KEYS <= set(observation) <= _CHANGE_SET_KEYS:
        raise ValueError
    change_set_name = observation.get("ChangeSetName")
    change_set_id = observation.get("ChangeSetId")
    description = observation.get("Description")
    if (
        observation.get("StackId") != STACK_ID
        or observation.get("StackName") != STACK_NAME
        or observation.get("Status") != "CREATE_COMPLETE"
        or observation.get("StatusReason") is not None
        or observation.get("ExecutionStatus") != "AVAILABLE"
        or observation.get("IncludeNestedStacks") is not False
        or observation.get("ImportExistingResources") not in (None, False)
        or observation.get("ParentChangeSetId") is not None
        or observation.get("RootChangeSetId") is not None
        or observation.get("DeploymentMode") is not None
        or observation.get("StackDriftStatus") is not None
        or observation.get("OnStackFailure") is not None
        or observation.get("DeploymentConfig") != {"DisableRollback": False, "Mode": "STANDARD"}
        or observation.get("RollbackConfiguration") != {}
        or observation.get("NotificationARNs") != []
        or observation.get("Capabilities") != ["CAPABILITY_NAMED_IAM"]
        or not isinstance(description, str)
        or not description.strip()
        or description != description.strip()
        or not isinstance(change_set_name, str)
        or _CHANGE_SET_NAME.fullmatch(change_set_name) is None
        or not isinstance(change_set_id, str)
        or not _exact_change_set_id(change_set_id, change_set_name)
    ):
        raise ValueError
    _utc_datetime(observation.get("CreationTime"))
    if _records(observation.get("Tags"), "Key", "Value") != _STACK_TAGS:
        raise ValueError
    expected_parameters = _verify_parameter_defaults(target_original)
    if _records(observation.get("Parameters"), "ParameterKey", "ParameterValue") != (
        expected_parameters
    ):
        raise ValueError
    _verify_resource_changes(
        observation.get("Changes"),
        predecessor_processed=predecessor_processed,
        target_processed=target_processed,
        changed_resources=changed_resources,
        selected=selected,
    )
    return change_set_id, change_set_name


def _verify_resource_changes(
    value: object,
    *,
    predecessor_processed: Mapping[str, object],
    target_processed: Mapping[str, object],
    changed_resources: tuple[str, ...],
    selected: Phase6CoreRuntimeTransitionTarget,
) -> None:
    if not isinstance(value, list) or len(value) != len(changed_resources):
        raise ValueError
    expected_paths = _resource_change_paths(selected)
    before_resources = _mapping(predecessor_processed, "Resources")
    after_resources = _mapping(target_processed, "Resources")
    actual: set[str] = set()
    for item in value:
        if not isinstance(item, Mapping) or set(item) != {"ResourceChange", "Type"}:
            raise ValueError
        if item.get("Type") != "Resource":
            raise ValueError
        resource = item.get("ResourceChange")
        if not isinstance(resource, Mapping) or set(resource) != _RESOURCE_CHANGE_KEYS:
            raise ValueError
        logical_id = resource.get("LogicalResourceId")
        physical_id = resource.get("PhysicalResourceId")
        if (
            not isinstance(logical_id, str)
            or logical_id in actual
            or logical_id not in expected_paths
            or resource.get("Action") != "Modify"
            or resource.get("Replacement") != "False"
            or resource.get("ResourceType") != _PROCESSED_RESOURCE_TYPES[logical_id]
            or resource.get("Scope") != ["Properties"]
            or not isinstance(physical_id, str)
            or not physical_id.strip()
            or physical_id != physical_id.strip()
        ):
            raise ValueError
        actual.add(logical_id)
        expected_path, before_value, after_present, after_value = expected_paths[logical_id]
        _verify_change_details(
            resource.get("Details"),
            property_path=expected_path,
            before_value=before_value,
            after_present=after_present,
            after_value=after_value,
        )
        _verify_change_contexts(
            resource.get("BeforeContext"),
            resource.get("AfterContext"),
            expected_path=expected_path,
            before_value=before_value,
            after_present=after_present,
            after_value=after_value,
        )
        if _mapping(before_resources, logical_id).get("Type") != resource.get("ResourceType"):
            raise ValueError
        if _mapping(after_resources, logical_id).get("Type") != resource.get("ResourceType"):
            raise ValueError
    if actual != set(changed_resources) or actual != set(expected_paths):
        raise ValueError


def _resource_change_paths(
    selected: Phase6CoreRuntimeTransitionTarget | str,
) -> dict[str, tuple[tuple[str, ...], object, bool, object]]:
    selected = _target(selected)
    if selected is Phase6CoreRuntimeTransitionTarget.CAPACITY_RELEASED_INERT:
        return {
            logical_id: (("Properties", "ReservedConcurrentExecutions"), 0, False, None)
            for logical_id in _MAINTENANCE_FUNCTIONS
        }
    result = {
        logical_id: (
            ("Properties", "Environment", "Variables", "MR_LISTER_PHASE6_SCAFFOLD_ONLY"),
            "true",
            True,
            "false",
        )
        for logical_id in _FUNCTIONS
    }
    result.update(
        {
            logical_id: (("Properties", "State"), "DISABLED", True, "ENABLED")
            for logical_id in _PROCESSED_EVENT_RULES
        }
    )
    result[_DISPATCHER_MAPPING] = (("Properties", "Enabled"), False, True, True)
    return result


def _verify_change_details(
    value: object,
    *,
    property_path: tuple[str, ...],
    before_value: object,
    after_present: bool,
    after_value: object,
) -> None:
    if not isinstance(value, list) or len(value) != 1:
        raise ValueError
    detail = value[0]
    if not isinstance(detail, Mapping) or set(detail) != _DETAIL_KEYS:
        raise ValueError
    if detail.get("ChangeSource") != "DirectModification" or detail.get("Evaluation") != "Static":
        raise ValueError
    target = detail.get("Target")
    if (
        not isinstance(target, Mapping)
        or not _DETAIL_TARGET_REQUIRED_KEYS <= set(target) <= _DETAIL_TARGET_ALLOWED_KEYS
        or target.get("Attribute") != "Properties"
        or target.get("RequiresRecreation") != "Never"
        or target.get("Name") != property_path[1]
        or target.get("Path") != "/" + "/".join(property_path)
        or target.get("AttributeChangeType") != ("Modify" if after_present else "Remove")
    ):
        raise ValueError
    if "BeforeValue" in target and target.get("BeforeValue") != _detail_value(before_value):
        raise ValueError
    if "AfterValue" in target:
        if not after_present or target.get("AfterValue") != _detail_value(after_value):
            raise ValueError


def _verify_change_contexts(
    before_raw: object,
    after_raw: object,
    *,
    expected_path: tuple[str, ...],
    before_value: object,
    after_present: bool,
    after_value: object,
) -> None:
    before = _decode_context(before_raw)
    after = _decode_context(after_raw)
    changes = _changes(before, after)
    expected = {
        expected_path: _Diff(True, before_value, after_present, after_value),
    }
    if changes != expected:
        raise ValueError


def _detail_value(value: object) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, allow_nan=False, separators=(",", ":"), sort_keys=True)


def _decode_context(value: object) -> Mapping[str, object]:
    if not isinstance(value, str) or not value:
        raise ValueError
    decoded = json.loads(value, object_pairs_hook=_unique_json_object, parse_constant=_bad_constant)
    if not isinstance(decoded, Mapping):
        raise ValueError
    _reject_placeholders(decoded)
    return decoded


def _records(value: object, key_name: str, value_name: str) -> dict[str, str]:
    if not isinstance(value, list):
        raise ValueError
    result: dict[str, str] = {}
    for record in value:
        if not isinstance(record, Mapping) or set(record) != {key_name, value_name}:
            raise ValueError
        key = record.get(key_name)
        record_value = record.get(value_name)
        if (
            not isinstance(key, str)
            or not key
            or key in result
            or not isinstance(record_value, str)
            or not record_value
        ):
            raise ValueError
        result[key] = record_value
    return result


def _changes(
    before: object,
    after: object,
    *,
    path: tuple[str, ...] = (),
) -> dict[tuple[str, ...], _Diff]:
    result: dict[tuple[str, ...], _Diff] = {}
    _collect_changes(before, after, path=path, result=result)
    return result


def _collect_changes(
    before: object,
    after: object,
    *,
    path: tuple[str, ...],
    result: dict[tuple[str, ...], _Diff],
) -> None:
    if isinstance(before, Mapping) and isinstance(after, Mapping):
        if any(not isinstance(key, str) for key in before) or any(
            not isinstance(key, str) for key in after
        ):
            raise ValueError
        for key in sorted(set(before) | set(after)):
            child = (*path, key)
            if key not in before:
                result[child] = _Diff(False, None, True, deepcopy(after[key]))
            elif key not in after:
                result[child] = _Diff(True, deepcopy(before[key]), False, None)
            else:
                _collect_changes(before[key], after[key], path=child, result=result)
        return
    if before != after or type(before) is not type(after):
        result[path] = _Diff(True, deepcopy(before), True, deepcopy(after))


def _trigger_inventory(enabled: bool) -> dict[str, dict[str, object]]:
    return {
        "DispatcherFunction.Events.DueWorkSweep": {
            "Enabled": enabled,
            "Type": "Schedule",
        },
        "DispatcherFunction.Events.OperationalStateChanges": {
            "Enabled": enabled,
            "Type": "DynamoDB",
        },
        "SourceVersionRetentionFunction.Events.SourceVersionRetentionSweep": {
            "Enabled": enabled,
            "Type": "Schedule",
        },
        "StuckExecutionRecoveryScheduleRule": {
            "State": "ENABLED" if enabled else "DISABLED",
            "Type": "AWS::Events::Rule",
        },
        "TerminalOperationalCleanupFunction.Events.TerminalOperationalCleanupSweep": {
            "Enabled": enabled,
            "Type": "Schedule",
        },
    }


def _mapping(value: Mapping[str, object], key: str) -> Mapping[str, object]:
    nested = value.get(key)
    if not isinstance(nested, Mapping):
        raise ValueError
    return nested


def _resource_properties(resources: Mapping[str, object], logical_id: str) -> Mapping[str, object]:
    return _mapping(_mapping(resources, logical_id), "Properties")


def _exact_int_equal(value: object, expected: int) -> bool:
    return type(value) is int and value == expected


def _canonical_sha256(value: object) -> str:
    return sha256(canonical_phase6_core_transition_change_set(value)).hexdigest()


def _exact_change_set_id(value: str, name: str) -> bool:
    prefix = f"arn:aws:cloudformation:{REGION}:{ACCOUNT_ID}:changeSet/{name}/"
    suffix = value.removeprefix(prefix)
    return value.startswith(prefix) and _UUID.fullmatch(suffix) is not None


def _utc_datetime(value: object) -> datetime:
    if not isinstance(value, str):
        raise ValueError
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError
    if parsed.utcoffset().total_seconds() != 0:
        raise ValueError
    return parsed.astimezone(UTC)


def _load_canonical_mapping(path: Path) -> tuple[bytes, Mapping[str, object]]:
    if not isinstance(path, Path) or any(
        candidate.is_symlink() for candidate in (path, *path.parents)
    ):
        raise ValueError
    resolved = path.resolve(strict=True)
    if not resolved.is_file():
        raise ValueError
    raw = resolved.read_bytes()
    if not raw or len(raw) > _MAX_INPUT_BYTES or b"\x00" in raw:
        raise ValueError
    value = json.loads(raw, object_pairs_hook=_unique_json_object, parse_constant=_bad_constant)
    if not isinstance(value, Mapping) or canonical_phase6_core_transition_change_set(value) != raw:
        raise ValueError
    _reject_placeholders(value)
    return raw, value


def _unique_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError
        result[key] = value
    return result


def _bad_constant(_value: str) -> object:
    raise ValueError


def _reject_placeholders(value: object) -> None:
    if isinstance(value, str):
        if _PLACEHOLDER.search(value):
            raise ValueError
        return
    if isinstance(value, Mapping):
        for key, nested in value.items():
            if not isinstance(key, str) or _PLACEHOLDER.search(key):
                raise ValueError
            _reject_placeholders(nested)
        return
    if isinstance(value, list):
        for nested in value:
            _reject_placeholders(nested)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--target",
        required=True,
        choices=[member.value for member in Phase6CoreRuntimeTransitionTarget],
    )
    parser.add_argument("--predecessor-original-template-observation", type=Path, required=True)
    parser.add_argument("--predecessor-processed-template-observation", type=Path, required=True)
    parser.add_argument("--target-template", type=Path, required=True)
    parser.add_argument("--change-set-observation", type=Path, required=True)
    parser.add_argument("--target-original-template-observation", type=Path, required=True)
    parser.add_argument("--target-processed-template-observation", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run the closed offline verifier and print only its canonical success record."""

    arguments = _parser().parse_args(argv)
    try:
        verified = verify_phase6_core_transition_change_set(
            target=arguments.target,
            predecessor_original_template_observation_path=(
                arguments.predecessor_original_template_observation
            ),
            predecessor_processed_template_observation_path=(
                arguments.predecessor_processed_template_observation
            ),
            target_template_path=arguments.target_template,
            change_set_observation_path=arguments.change_set_observation,
            target_original_template_observation_path=(
                arguments.target_original_template_observation
            ),
            target_processed_template_observation_path=(
                arguments.target_processed_template_observation
            ),
        )
    except Phase6CoreTransitionChangeSetError:
        _parser().error(_GENERIC_ERROR)
    print(canonical_phase6_core_transition_change_set(asdict(verified)).decode("utf-8"), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
