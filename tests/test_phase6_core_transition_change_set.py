from __future__ import annotations

import ast
import json
from copy import deepcopy
from dataclasses import FrozenInstanceError
from hashlib import sha256
from pathlib import Path
from typing import cast

import pytest

import tools.verify_phase6_core_transition_change_set as verifier
from tools.render_phase6_core_runtime_transition import Phase6CoreRuntimeTransitionTarget
from tools.verify_phase6_core_transition_change_set import (
    FORMAT,
    Phase6CoreTransitionChangeSetError,
    canonical_phase6_core_transition_change_set,
    verify_phase6_core_transition_change_set,
)

STACK_ID = (
    "arn:aws:cloudformation:us-west-2:384627057108:stack/mr-lister-phase6-dev/"
    "f3456970-9fdc-11f1-b448-06b81627db1d"
)
CHANGE_SET_NAME = "mr-lister-phase6-dev-core-transition-a1b2c3d4e5f6"
CHANGE_SET_ID = (
    "arn:aws:cloudformation:us-west-2:384627057108:changeSet/"
    f"{CHANGE_SET_NAME}/12345678-1234-1234-1234-1234567890ab"
)
FUNCTIONS = verifier._FUNCTIONS
MAINTENANCE = verifier._MAINTENANCE_FUNCTIONS
RULES = verifier._PROCESSED_EVENT_RULES
MAPPING = verifier._DISPATCHER_MAPPING


def _canonical(value: object) -> bytes:
    return canonical_phase6_core_transition_change_set(value)


def _parameters() -> dict[str, object]:
    values = {
        "AgentCoreRuntimeArn": (
            "arn:aws:bedrock-agentcore:us-west-2:384627057108:runtime/mr_lister_phase6-4HoPmq2hCI"
        ),
        "AgentCoreRuntimeBindingFingerprint": "1" * 64,
        "AgentCoreRuntimeEndpointArn": (
            "arn:aws:bedrock-agentcore:us-west-2:384627057108:runtime/"
            "mr_lister_phase6-4HoPmq2hCI/runtime-endpoint/phase6_v1_dev"
        ),
        "AgentCoreRuntimeQualifier": "phase6_v1_dev",
        "AgentCoreRuntimeVersion": "1",
        "ApplicationOrigin": "https://massskutiny.com",
        "EnvironmentName": "dev",
        "PrintifySecretArn": (
            "arn:aws:secretsmanager:us-west-2:384627057108:secret:"
            "mr-lister/dev/printify/primary-Ab12Cd"
        ),
        "ReleaseFingerprint": "2" * 64,
    }
    return {
        name: {"AllowedValues": [value], "Default": value, "Type": "String"}
        for name, value in values.items()
    }


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


def _metadata(mode: str, *, staged_sha256: str | None = None) -> dict[str, object]:
    contracts = {
        "staged": (
            "mr-lister-phase6-core-sam-staged-v1",
            "STAGED_FAIL_CLOSED",
            "CORE_RELEASE_BOUND_STAGED",
        ),
        "capacity-released-inert": (
            "mr-lister-phase6-core-runtime-transition-v1",
            "CAPACITY_RELEASED_INERT",
            "CORE_CAPACITY_RELEASED_INERT",
        ),
        "backend-active-draft-only": (
            "mr-lister-phase6-core-runtime-transition-v1",
            "ACTIVE_DRAFT_ONLY",
            "CORE_RUNTIME_ACTIVE_DRAFT_ONLY",
        ),
    }
    template_format, deployment_mode, readiness = contracts[mode]
    value: dict[str, object] = {
        "AgentCore": {"Status": "READY", "Version": "1"},
        "DisabledTriggers": _trigger_inventory(False),
        "Format": template_format,
        "Foundation": {
            "ArtifactBucketName": "mr-lister-phase6-artifacts-dev-384627057108-us-west-2",
            "StackId": STACK_ID,
            "StackName": "mr-lister-phase6-dev",
        },
        "Mode": deployment_mode,
        "Readiness": readiness,
        "ReleaseFingerprint": "2" * 64,
        "Target": {
            "AccountId": "384627057108",
            "Environment": "dev",
            "Region": "us-west-2",
        },
    }
    if mode != "staged":
        value["StagedTemplateSha256"] = staged_sha256
    if mode == "backend-active-draft-only":
        del value["DisabledTriggers"]
        value["ActiveTriggers"] = _trigger_inventory(True)
    return {"MrListerPhase6CoreRuntimeStaging": value}


def _original_resources(mode: str) -> dict[str, object]:
    enabled = mode == "backend-active-draft-only"
    resources: dict[str, object] = {
        logical_id: {"Properties": {"Fixture": logical_id}, "Type": resource_type}
        for logical_id, resource_type in verifier._ORIGINAL_RESOURCE_TYPES.items()
    }
    for logical_id in FUNCTIONS:
        properties = cast(
            dict[str, object], cast(dict[str, object], resources[logical_id])["Properties"]
        )
        properties.update(
            {
                "Architectures": ["arm64"],
                "CodeUri": {
                    "Bucket": "mr-lister-phase6-artifacts-dev-384627057108-us-west-2",
                    "Key": "private/deployments/lambda/releases/sealed/phase6-lambda.zip",
                    "Version": "ExactVersion1",
                },
                "Runtime": "python3.12",
            }
        )
        if mode == "staged" and logical_id in MAINTENANCE:
            properties["ReservedConcurrentExecutions"] = 0
    dispatcher = cast(
        dict[str, object],
        cast(dict[str, object], resources["DispatcherFunction"])["Properties"],
    )
    dispatcher["Events"] = {
        "DueWorkSweep": {
            "Properties": {"Enabled": enabled, "Schedule": "rate(1 minute)"},
            "Type": "Schedule",
        },
        "OperationalStateChanges": {
            "Properties": {"Enabled": enabled, "StartingPosition": "LATEST"},
            "Type": "DynamoDB",
        },
    }
    source = cast(
        dict[str, object],
        cast(dict[str, object], resources["SourceVersionRetentionFunction"])["Properties"],
    )
    source["Events"] = {
        "SourceVersionRetentionSweep": {
            "Properties": {"Enabled": enabled, "Schedule": "rate(15 minutes)"},
            "Type": "Schedule",
        }
    }
    cleanup = cast(
        dict[str, object],
        cast(dict[str, object], resources["TerminalOperationalCleanupFunction"])["Properties"],
    )
    cleanup["Events"] = {
        "TerminalOperationalCleanupSweep": {
            "Properties": {"Enabled": enabled, "Schedule": "rate(1 day)"},
            "Type": "Schedule",
        }
    }
    recovery = cast(
        dict[str, object],
        cast(dict[str, object], resources["StuckExecutionRecoveryScheduleRule"])["Properties"],
    )
    recovery["State"] = "ENABLED" if enabled else "DISABLED"
    return resources


def _original(mode: str, *, staged_sha256: str | None = None) -> dict[str, object]:
    contracts = {
        "staged": (
            "CORE_RELEASE_BOUND_STAGED",
            verifier._STAGED_DESCRIPTION,
        ),
        "capacity-released-inert": (
            "CORE_CAPACITY_RELEASED_INERT",
            verifier._CAPACITY_DESCRIPTION,
        ),
        "backend-active-draft-only": (
            "CORE_RUNTIME_ACTIVE_DRAFT_ONLY",
            verifier._ACTIVE_DESCRIPTION,
        ),
    }
    readiness, description = contracts[mode]
    return {
        "AWSTemplateFormatVersion": "2010-09-09",
        "Description": "Mr Lister Phase 6 exact core fixture",
        "Globals": {
            "Function": {
                "Environment": {
                    "Variables": {
                        "MR_LISTER_PHASE6_SCAFFOLD_ONLY": (
                            "false" if mode == "backend-active-draft-only" else "true"
                        )
                    }
                }
            }
        },
        "Metadata": _metadata(mode, staged_sha256=staged_sha256),
        "Outputs": {
            "DeploymentReadiness": {"Description": description, "Value": readiness},
            "StateTableName": {"Value": "mr-lister-phase6-dev"},
        },
        "Parameters": _parameters(),
        "Resources": _original_resources(mode),
        "Transform": "AWS::Serverless-2016-10-31",
    }


def _processed(original: dict[str, object], mode: str) -> dict[str, object]:
    resources = deepcopy(cast(dict[str, object], original["Resources"]))
    marker = "false" if mode == "backend-active-draft-only" else "true"
    state = "ENABLED" if mode == "backend-active-draft-only" else "DISABLED"
    enabled = mode == "backend-active-draft-only"
    for logical_id in FUNCTIONS:
        resource = cast(dict[str, object], resources[logical_id])
        resource["Type"] = "AWS::Lambda::Function"
        properties = cast(dict[str, object], resource["Properties"])
        properties.pop("Events", None)
        properties["Environment"] = {"Variables": {"MR_LISTER_PHASE6_SCAFFOLD_ONLY": marker}}
    for logical_id in verifier._STATE_MACHINES:
        cast(dict[str, object], resources[logical_id])["Type"] = "AWS::StepFunctions::StateMachine"
    resources.update(
        {
            "DispatcherFunctionDueWorkSweep": {
                "Properties": {"State": state},
                "Type": "AWS::Events::Rule",
            },
            "DispatcherFunctionDueWorkSweepPermission": {
                "Properties": {"Action": "lambda:InvokeFunction"},
                "Type": "AWS::Lambda::Permission",
            },
            MAPPING: {
                "Properties": {"Enabled": enabled},
                "Type": "AWS::Lambda::EventSourceMapping",
            },
            "SourceVersionRetentionFunctionSourceVersionRetentionSweep": {
                "Properties": {"State": state},
                "Type": "AWS::Events::Rule",
            },
            "SourceVersionRetentionFunctionSourceVersionRetentionSweepPermission": {
                "Properties": {"Action": "lambda:InvokeFunction"},
                "Type": "AWS::Lambda::Permission",
            },
            "TerminalOperationalCleanupFunctionTerminalOperationalCleanupSweep": {
                "Properties": {"State": state},
                "Type": "AWS::Events::Rule",
            },
            "TerminalOperationalCleanupFunctionTerminalOperationalCleanupSweepPermission": {
                "Properties": {"Action": "lambda:InvokeFunction"},
                "Type": "AWS::Lambda::Permission",
            },
        }
    )
    return {
        key: deepcopy(original[key])
        for key in (
            "AWSTemplateFormatVersion",
            "Description",
            "Metadata",
            "Outputs",
            "Parameters",
        )
    } | {"Resources": resources}


def _observation(body: dict[str, object]) -> dict[str, object]:
    return {"StagesAvailable": ["Original", "Processed"], "TemplateBody": body}


def _path_value(document: dict[str, object], path: tuple[str, ...]) -> object:
    current: object = document
    for component in path:
        current = cast(dict[str, object], current)[component]
    return current


def _change_paths(target: str) -> dict[str, tuple[tuple[str, ...], object, bool, object]]:
    selected = Phase6CoreRuntimeTransitionTarget(target)
    return verifier._resource_change_paths(selected)


def _change_set(
    target: str,
    predecessor_processed: dict[str, object],
    target_processed: dict[str, object],
    target_original: dict[str, object],
) -> dict[str, object]:
    changes = []
    before_resources = cast(dict[str, object], predecessor_processed["Resources"])
    after_resources = cast(dict[str, object], target_processed["Resources"])
    for logical_id, (path, before_value, after_present, after_value) in sorted(
        _change_paths(target).items()
    ):
        before_context = deepcopy(cast(dict[str, object], before_resources[logical_id]))
        after_context = deepcopy(cast(dict[str, object], after_resources[logical_id]))
        before_leaf = before_context
        for component in path[:-1]:
            before_leaf = cast(dict[str, object], before_leaf[component])
        before_leaf[path[-1]] = verifier._detail_value(before_value)
        if after_present:
            after_leaf = after_context
            for component in path[:-1]:
                after_leaf = cast(dict[str, object], after_leaf[component])
            after_leaf[path[-1]] = verifier._detail_value(after_value)
        target_detail: dict[str, object] = {
            "Attribute": "Properties",
            "AttributeChangeType": "Modify" if after_present else "Remove",
            "BeforeValue": verifier._detail_value(before_value),
            "Name": path[1],
            "Path": "/" + "/".join(path),
            "RequiresRecreation": "Never",
        }
        if after_present:
            target_detail["AfterValue"] = verifier._detail_value(after_value)
        changes.append(
            {
                "ResourceChange": {
                    "Action": "Modify",
                    "AfterContext": json.dumps(
                        after_context, allow_nan=False, separators=(",", ":"), sort_keys=True
                    ),
                    "BeforeContext": json.dumps(
                        before_context, allow_nan=False, separators=(",", ":"), sort_keys=True
                    ),
                    "Details": [
                        {
                            "ChangeSource": "DirectModification",
                            "Evaluation": "Static",
                            "Target": target_detail,
                        }
                    ],
                    "LogicalResourceId": logical_id,
                    "PhysicalResourceId": f"mr-lister-phase6-dev-{logical_id.lower()}",
                    "Replacement": "False",
                    "ResourceType": cast(dict[str, object], after_resources[logical_id])["Type"],
                    "Scope": ["Properties"],
                },
                "Type": "Resource",
            }
        )
    parameters = cast(dict[str, object], target_original["Parameters"])
    return {
        "Capabilities": ["CAPABILITY_NAMED_IAM"],
        "ChangeSetId": CHANGE_SET_ID,
        "ChangeSetName": CHANGE_SET_NAME,
        "Changes": changes,
        "CreationTime": "2026-08-25T21:00:00+00:00",
        "DeploymentConfig": {"DisableRollback": False, "Mode": "STANDARD"},
        "DeploymentMode": None,
        "Description": f"Reviewed Phase 6 {target} transition",
        "ExecutionStatus": "AVAILABLE",
        "ImportExistingResources": None,
        "IncludeNestedStacks": False,
        "NotificationARNs": [],
        "OnStackFailure": None,
        "Parameters": [
            {
                "ParameterKey": name,
                "ParameterValue": cast(str, cast(dict[str, object], definition)["Default"]),
            }
            for name, definition in sorted(parameters.items())
        ],
        "ParentChangeSetId": None,
        "RollbackConfiguration": {},
        "RootChangeSetId": None,
        "StackDriftStatus": None,
        "StackId": STACK_ID,
        "StackName": "mr-lister-phase6-dev",
        "Status": "CREATE_COMPLETE",
        "StatusReason": None,
        "Tags": [
            {"Key": "DeploymentClass", "Value": "FOUNDATION_ONLY"},
            {"Key": "Environment", "Value": "dev"},
            {"Key": "Project", "Value": "MrLister"},
        ],
    }


def _write(path: Path, value: object) -> Path:
    path.write_bytes(_canonical(value))
    return path


def _fixture(tmp_path: Path, target: str) -> dict[str, object]:
    staged = _original("staged")
    staged_sha = sha256(_canonical(staged)).hexdigest()
    capacity = _original("capacity-released-inert", staged_sha256=staged_sha)
    active = _original("backend-active-draft-only", staged_sha256=staged_sha)
    predecessor = staged if target == "capacity-released-inert" else capacity
    target_original = capacity if target == "capacity-released-inert" else active
    predecessor_processed = _processed(
        predecessor,
        "staged" if target == "capacity-released-inert" else "capacity-released-inert",
    )
    target_processed = _processed(target_original, target)
    change_set = _change_set(target, predecessor_processed, target_processed, target_original)
    paths = {
        "predecessor_original_template_observation_path": _write(
            tmp_path / "predecessor-original.json", _observation(predecessor)
        ),
        "predecessor_processed_template_observation_path": _write(
            tmp_path / "predecessor-processed.json", _observation(predecessor_processed)
        ),
        "target_template_path": _write(tmp_path / "target.local.json", target_original),
        "change_set_observation_path": _write(tmp_path / "change-set.json", change_set),
        "target_original_template_observation_path": _write(
            tmp_path / "target-original.json", _observation(target_original)
        ),
        "target_processed_template_observation_path": _write(
            tmp_path / "target-processed.json", _observation(target_processed)
        ),
    }
    return {
        "kwargs": {"target": target, **paths},
        "paths": paths,
        "predecessor": predecessor,
        "predecessor_processed": predecessor_processed,
        "target_original": target_original,
        "target_processed": target_processed,
        "change_set": change_set,
    }


def _rewrite(path: Path, mutate: object) -> dict[str, object]:
    document = json.loads(path.read_text())
    cast(object, mutate)(document)  # type: ignore[operator]
    _write(path, document)
    return document


def _verify(fixture: dict[str, object]):
    return verify_phase6_core_transition_change_set(**cast(dict[str, object], fixture["kwargs"]))


@pytest.mark.parametrize(
    "target,predecessor_mode,target_mode,changed_count",
    [
        ("capacity-released-inert", "staged", "capacity-released-inert", 3),
        (
            "backend-active-draft-only",
            "capacity-released-inert",
            "backend-active-draft-only",
            12,
        ),
    ],
)
def test_accepts_exact_transition(
    tmp_path: Path,
    target: str,
    predecessor_mode: str,
    target_mode: str,
    changed_count: int,
) -> None:
    fixture = _fixture(tmp_path, target)
    verified = _verify(fixture)
    assert verified.format == FORMAT
    assert verified.transition_target == target
    assert verified.predecessor_mode == predecessor_mode
    assert verified.target_mode == target_mode
    assert verified.stack_id == STACK_ID
    assert verified.change_set_type == "UPDATE"
    assert len(verified.changed_resources) == changed_count
    assert verified.original_resource_count == 40
    assert verified.processed_resource_count == 47
    assert len(verified.canonical_sha256) == 64


def test_accepts_enum_and_returns_deterministic_frozen_record(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path, "capacity-released-inert")
    kwargs = cast(dict[str, object], fixture["kwargs"])
    kwargs["target"] = Phase6CoreRuntimeTransitionTarget.CAPACITY_RELEASED_INERT
    first = verify_phase6_core_transition_change_set(**kwargs)
    second = verify_phase6_core_transition_change_set(**kwargs)
    assert first == second
    with pytest.raises(FrozenInstanceError):
        first.stack_id = "changed"  # type: ignore[misc]


@pytest.mark.parametrize("target", ["", "unknown", " capacity-released-inert", True, None])
def test_rejects_invalid_target(tmp_path: Path, target: object) -> None:
    fixture = _fixture(tmp_path, "capacity-released-inert")
    cast(dict[str, object], fixture["kwargs"])["target"] = target
    with pytest.raises(Phase6CoreTransitionChangeSetError, match="invalid"):
        _verify(fixture)


@pytest.mark.parametrize(
    "path_key",
    [
        "predecessor_original_template_observation_path",
        "predecessor_processed_template_observation_path",
        "target_template_path",
        "change_set_observation_path",
        "target_original_template_observation_path",
        "target_processed_template_observation_path",
    ],
)
def test_rejects_noncanonical_input_file(tmp_path: Path, path_key: str) -> None:
    fixture = _fixture(tmp_path, "capacity-released-inert")
    path = cast(dict[str, Path], fixture["paths"])[path_key]
    value = json.loads(path.read_text())
    path.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(Phase6CoreTransitionChangeSetError):
        _verify(fixture)


def test_rejects_duplicate_json_keys(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path, "capacity-released-inert")
    path = cast(dict[str, Path], fixture["paths"])["change_set_observation_path"]
    path.write_text('{"x": 1, "x": 2}\n', encoding="utf-8")
    with pytest.raises(Phase6CoreTransitionChangeSetError):
        _verify(fixture)


def test_rejects_symlinked_input(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path, "capacity-released-inert")
    original = cast(dict[str, Path], fixture["paths"])["target_template_path"]
    link = tmp_path / "target-link.json"
    link.symlink_to(original)
    cast(dict[str, object], fixture["kwargs"])["target_template_path"] = link
    with pytest.raises(Phase6CoreTransitionChangeSetError):
        _verify(fixture)


def test_rejects_placeholder(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path, "capacity-released-inert")
    path = cast(dict[str, Path], fixture["paths"])["change_set_observation_path"]
    _rewrite(path, lambda value: value.update({"Description": "REPLACE_ME"}))
    with pytest.raises(Phase6CoreTransitionChangeSetError):
        _verify(fixture)


@pytest.mark.parametrize("mutation", ["missing", "extra", "stage-order", "body-type"])
def test_rejects_get_template_wrapper_drift(tmp_path: Path, mutation: str) -> None:
    fixture = _fixture(tmp_path, "capacity-released-inert")
    path = cast(dict[str, Path], fixture["paths"])["target_original_template_observation_path"]

    def mutate(value: dict[str, object]) -> None:
        if mutation == "missing":
            del value["StagesAvailable"]
        elif mutation == "extra":
            value["ResponseMetadata"] = {}
        elif mutation == "stage-order":
            value["StagesAvailable"] = ["Processed", "Original"]
        else:
            value["TemplateBody"] = []

    _rewrite(path, mutate)
    with pytest.raises(Phase6CoreTransitionChangeSetError):
        _verify(fixture)


def test_rejects_target_original_not_equal_to_local_target(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path, "capacity-released-inert")
    path = cast(dict[str, Path], fixture["paths"])["target_original_template_observation_path"]
    _rewrite(path, lambda value: value["TemplateBody"].update({"Description": "drift"}))
    with pytest.raises(Phase6CoreTransitionChangeSetError):
        _verify(fixture)


@pytest.mark.parametrize("stage", ["original", "processed"])
@pytest.mark.parametrize("mutation", ["missing", "extra", "type"])
def test_rejects_resource_inventory_drift(tmp_path: Path, stage: str, mutation: str) -> None:
    fixture = _fixture(tmp_path, "capacity-released-inert")
    key = (
        "target_original_template_observation_path"
        if stage == "original"
        else "target_processed_template_observation_path"
    )
    path = cast(dict[str, Path], fixture["paths"])[key]

    def mutate(value: dict[str, object]) -> None:
        resources = value["TemplateBody"]["Resources"]
        if mutation == "missing":
            del resources["DispatcherLogGroup"]
        elif mutation == "extra":
            resources["UnexpectedResource"] = {"Properties": {}, "Type": "AWS::SNS::Topic"}
        else:
            resources["DispatcherLogGroup"]["Type"] = "AWS::SNS::Topic"

    _rewrite(path, mutate)
    with pytest.raises(Phase6CoreTransitionChangeSetError):
        _verify(fixture)


@pytest.mark.parametrize(
    "field,value",
    [
        ("Mode", "ACTIVE_DRAFT_ONLY"),
        ("Readiness", "CORE_RUNTIME_ACTIVE_DRAFT_ONLY"),
        ("Format", "wrong-v1"),
    ],
)
def test_rejects_mode_metadata_drift(tmp_path: Path, field: str, value: str) -> None:
    fixture = _fixture(tmp_path, "capacity-released-inert")
    path = cast(dict[str, Path], fixture["paths"])["target_template_path"]

    def mutate(document: dict[str, object]) -> None:
        document["Metadata"]["MrListerPhase6CoreRuntimeStaging"][field] = value

    _rewrite(path, mutate)
    with pytest.raises(Phase6CoreTransitionChangeSetError):
        _verify(fixture)


def test_rejects_boolean_as_zero_concurrency(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path, "capacity-released-inert")
    path = cast(dict[str, Path], fixture["paths"])["predecessor_original_template_observation_path"]

    def mutate(value: dict[str, object]) -> None:
        value["TemplateBody"]["Resources"][MAINTENANCE[0]]["Properties"][
            "ReservedConcurrentExecutions"
        ] = False

    _rewrite(path, mutate)
    with pytest.raises(Phase6CoreTransitionChangeSetError):
        _verify(fixture)


@pytest.mark.parametrize(
    "target,path_key,path",
    [
        (
            "capacity-released-inert",
            "target_processed_template_observation_path",
            ("TemplateBody", "Resources", "DispatcherFunction", "Properties", "Fixture"),
        ),
        (
            "backend-active-draft-only",
            "target_processed_template_observation_path",
            ("TemplateBody", "Resources", "DispatcherLogGroup", "Properties", "Fixture"),
        ),
    ],
)
def test_rejects_unreviewed_processed_property_diff(
    tmp_path: Path, target: str, path_key: str, path: tuple[str, ...]
) -> None:
    fixture = _fixture(tmp_path, target)
    capture = cast(dict[str, Path], fixture["paths"])[path_key]

    def mutate(value: dict[str, object]) -> None:
        current = value
        for component in path[:-1]:
            current = current[component]
        current[path[-1]] = "drift"

    _rewrite(capture, mutate)
    with pytest.raises(Phase6CoreTransitionChangeSetError):
        _verify(fixture)


def test_rejects_missing_one_active_function_marker_change(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path, "backend-active-draft-only")
    path = cast(dict[str, Path], fixture["paths"])["target_processed_template_observation_path"]

    def mutate(value: dict[str, object]) -> None:
        value["TemplateBody"]["Resources"]["SettlementFunction"]["Properties"]["Environment"][
            "Variables"
        ]["MR_LISTER_PHASE6_SCAFFOLD_ONLY"] = "true"

    _rewrite(path, mutate)
    with pytest.raises(Phase6CoreTransitionChangeSetError):
        _verify(fixture)


def test_rejects_processed_metadata_or_output_path_drift(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path, "capacity-released-inert")
    path = cast(dict[str, Path], fixture["paths"])["target_processed_template_observation_path"]

    def mutate(value: dict[str, object]) -> None:
        value["TemplateBody"]["Outputs"]["StateTableName"]["Description"] = "unexpected"

    _rewrite(path, mutate)
    with pytest.raises(Phase6CoreTransitionChangeSetError):
        _verify(fixture)


@pytest.mark.parametrize(
    "field,value",
    [
        ("Status", "FAILED"),
        ("ExecutionStatus", "OBSOLETE"),
        ("StackId", STACK_ID + "-drift"),
        ("StackName", "other-stack"),
        ("IncludeNestedStacks", True),
        ("ImportExistingResources", True),
        ("ParentChangeSetId", "parent"),
        ("RootChangeSetId", "root"),
        ("Capabilities", []),
        ("OnStackFailure", "DO_NOTHING"),
        ("NotificationARNs", ["arn:aws:sns:us-west-2:384627057108:topic"]),
    ],
)
def test_rejects_change_set_envelope_drift(tmp_path: Path, field: str, value: object) -> None:
    fixture = _fixture(tmp_path, "capacity-released-inert")
    path = cast(dict[str, Path], fixture["paths"])["change_set_observation_path"]
    _rewrite(path, lambda document: document.update({field: value}))
    with pytest.raises(Phase6CoreTransitionChangeSetError):
        _verify(fixture)


@pytest.mark.parametrize("mutation", ["missing", "extra", "pagination"])
def test_rejects_change_set_key_drift(tmp_path: Path, mutation: str) -> None:
    fixture = _fixture(tmp_path, "capacity-released-inert")
    path = cast(dict[str, Path], fixture["paths"])["change_set_observation_path"]

    def mutate(document: dict[str, object]) -> None:
        if mutation == "missing":
            del document["RollbackConfiguration"]
        elif mutation == "extra":
            document["SyntheticChangeSetType"] = "UPDATE"
        else:
            document["NextToken"] = "not-allowed"

    _rewrite(path, mutate)
    with pytest.raises(Phase6CoreTransitionChangeSetError):
        _verify(fixture)


def test_accepts_omitted_safe_optional_change_set_fields(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path, "capacity-released-inert")
    path = cast(dict[str, Path], fixture["paths"])["change_set_observation_path"]

    def mutate(document: dict[str, object]) -> None:
        for key in verifier._CHANGE_SET_OPTIONAL_KEYS:
            document.pop(key, None)

    _rewrite(path, mutate)
    assert _verify(fixture).change_set_type == "UPDATE"


def test_accepts_explicit_false_import_existing_resources(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path, "capacity-released-inert")
    path = cast(dict[str, Path], fixture["paths"])["change_set_observation_path"]
    _rewrite(path, lambda document: document.update({"ImportExistingResources": False}))
    assert _verify(fixture).change_set_type == "UPDATE"


@pytest.mark.parametrize("mutation", ["missing", "extra", "wrong", "record-extra"])
def test_rejects_locked_parameter_drift(tmp_path: Path, mutation: str) -> None:
    fixture = _fixture(tmp_path, "capacity-released-inert")
    path = cast(dict[str, Path], fixture["paths"])["change_set_observation_path"]

    def mutate(document: dict[str, object]) -> None:
        parameters = document["Parameters"]
        if mutation == "missing":
            parameters.pop()
        elif mutation == "extra":
            parameters.append({"ParameterKey": "Extra", "ParameterValue": "value"})
        elif mutation == "wrong":
            parameters[0]["ParameterValue"] = "wrong"
        else:
            parameters[0]["UsePreviousValue"] = False

    _rewrite(path, mutate)
    with pytest.raises(Phase6CoreTransitionChangeSetError):
        _verify(fixture)


@pytest.mark.parametrize("mutation", ["missing", "extra", "wrong", "record-extra"])
def test_rejects_locked_tag_drift(tmp_path: Path, mutation: str) -> None:
    fixture = _fixture(tmp_path, "capacity-released-inert")
    path = cast(dict[str, Path], fixture["paths"])["change_set_observation_path"]

    def mutate(document: dict[str, object]) -> None:
        tags = document["Tags"]
        if mutation == "missing":
            tags.pop()
        elif mutation == "extra":
            tags.append({"Key": "Extra", "Value": "value"})
        elif mutation == "wrong":
            tags[0]["Value"] = "wrong"
        else:
            tags[0]["PropagateAtLaunch"] = False

    _rewrite(path, mutate)
    with pytest.raises(Phase6CoreTransitionChangeSetError):
        _verify(fixture)


@pytest.mark.parametrize(
    "field,value",
    [
        ("Action", "Add"),
        ("Replacement", "True"),
        ("ResourceType", "AWS::SNS::Topic"),
        ("Scope", ["Metadata"]),
        ("PhysicalResourceId", ""),
    ],
)
def test_rejects_resource_change_broadening(tmp_path: Path, field: str, value: object) -> None:
    fixture = _fixture(tmp_path, "capacity-released-inert")
    path = cast(dict[str, Path], fixture["paths"])["change_set_observation_path"]

    def mutate(document: dict[str, object]) -> None:
        document["Changes"][0]["ResourceChange"][field] = value

    _rewrite(path, mutate)
    with pytest.raises(Phase6CoreTransitionChangeSetError):
        _verify(fixture)


@pytest.mark.parametrize("mutation", ["missing", "extra", "duplicate"])
def test_rejects_changed_resource_set_drift(tmp_path: Path, mutation: str) -> None:
    fixture = _fixture(tmp_path, "capacity-released-inert")
    path = cast(dict[str, Path], fixture["paths"])["change_set_observation_path"]

    def mutate(document: dict[str, object]) -> None:
        if mutation == "missing":
            document["Changes"].pop()
        elif mutation == "extra":
            extra = deepcopy(document["Changes"][0])
            extra["ResourceChange"]["LogicalResourceId"] = "DispatcherFunction"
            document["Changes"].append(extra)
        else:
            document["Changes"][1]["ResourceChange"]["LogicalResourceId"] = document["Changes"][0][
                "ResourceChange"
            ]["LogicalResourceId"]

    _rewrite(path, mutate)
    with pytest.raises(Phase6CoreTransitionChangeSetError):
        _verify(fixture)


@pytest.mark.parametrize(
    "mutation",
    ["dynamic", "path", "name", "recreation", "source", "detail-extra", "target-extra"],
)
def test_rejects_detail_or_property_path_drift(tmp_path: Path, mutation: str) -> None:
    fixture = _fixture(tmp_path, "backend-active-draft-only")
    path = cast(dict[str, Path], fixture["paths"])["change_set_observation_path"]

    def mutate(document: dict[str, object]) -> None:
        detail = document["Changes"][0]["ResourceChange"]["Details"][0]
        target = detail["Target"]
        if mutation == "dynamic":
            detail["Evaluation"] = "Dynamic"
        elif mutation == "path":
            target["Path"] = "/Properties/Environment"
        elif mutation == "name":
            target["Name"] = "Role"
        elif mutation == "recreation":
            target["RequiresRecreation"] = "Always"
        elif mutation == "source":
            detail["ChangeSource"] = "ParameterReference"
        elif mutation == "detail-extra":
            detail["CausingEntity"] = "Parameter"
        else:
            target["BeforeValueFrom"] = "dynamic"

    _rewrite(path, mutate)
    with pytest.raises(Phase6CoreTransitionChangeSetError):
        _verify(fixture)


@pytest.mark.parametrize("mutation", ["malformed", "missing-change", "extra-change", "wrong-value"])
def test_rejects_before_after_context_drift(tmp_path: Path, mutation: str) -> None:
    fixture = _fixture(tmp_path, "capacity-released-inert")
    path = cast(dict[str, Path], fixture["paths"])["change_set_observation_path"]

    def mutate(document: dict[str, object]) -> None:
        resource = document["Changes"][0]["ResourceChange"]
        if mutation == "malformed":
            resource["BeforeContext"] = "not-json"
        elif mutation == "missing-change":
            resource["AfterContext"] = resource["BeforeContext"]
        else:
            after = json.loads(resource["AfterContext"])
            if mutation == "extra-change":
                after["Properties"]["Fixture"] = "drift"
            else:
                after["Properties"]["ReservedConcurrentExecutions"] = 1
            resource["AfterContext"] = json.dumps(after, separators=(",", ":"), sort_keys=True)

    _rewrite(path, mutate)
    with pytest.raises(Phase6CoreTransitionChangeSetError):
        _verify(fixture)


def test_rejects_change_set_id_not_bound_to_name(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path, "capacity-released-inert")
    path = cast(dict[str, Path], fixture["paths"])["change_set_observation_path"]
    _rewrite(path, lambda document: document.update({"ChangeSetName": "different-name"}))
    with pytest.raises(Phase6CoreTransitionChangeSetError):
        _verify(fixture)


@pytest.mark.parametrize("value", ["not-a-time", "2026-08-25T21:00:00-07:00", None])
def test_rejects_non_utc_creation_time(tmp_path: Path, value: object) -> None:
    fixture = _fixture(tmp_path, "capacity-released-inert")
    path = cast(dict[str, Path], fixture["paths"])["change_set_observation_path"]
    _rewrite(path, lambda document: document.update({"CreationTime": value}))
    with pytest.raises(Phase6CoreTransitionChangeSetError):
        _verify(fixture)


def test_cli_emits_canonical_verified_record(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    fixture = _fixture(tmp_path, "capacity-released-inert")
    paths = cast(dict[str, Path], fixture["paths"])
    result = verifier.main(
        [
            "--target",
            "capacity-released-inert",
            "--predecessor-original-template-observation",
            str(paths["predecessor_original_template_observation_path"]),
            "--predecessor-processed-template-observation",
            str(paths["predecessor_processed_template_observation_path"]),
            "--target-template",
            str(paths["target_template_path"]),
            "--change-set-observation",
            str(paths["change_set_observation_path"]),
            "--target-original-template-observation",
            str(paths["target_original_template_observation_path"]),
            "--target-processed-template-observation",
            str(paths["target_processed_template_observation_path"]),
        ]
    )
    output = capsys.readouterr().out.encode()
    assert result == 0
    assert _canonical(json.loads(output)) == output
    assert json.loads(output)["format"] == FORMAT


def test_module_imports_no_aws_sdk_or_subprocess() -> None:
    source = Path(verifier.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    imports = {
        alias.name.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    imports.update(
        cast(str, node.module).split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module is not None
    )
    assert imports.isdisjoint({"boto3", "botocore", "subprocess"})
