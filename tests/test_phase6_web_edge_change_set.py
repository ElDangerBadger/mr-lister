from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import asdict
from hashlib import sha256
from pathlib import Path
from typing import Any

import pytest

import tools.verify_phase6_web_edge_change_set as verifier
from tools.verify_phase6_web_edge_change_set import (
    EXPECTED_EXECUTION_ROLE_ARN,
    Phase6WebEdgeChangeSetError,
    canonical_phase6_web_edge_change_set,
    verify_phase6_web_edge_change_set,
)

CHANGE_SET_UUID = "01234567-89ab-cdef-0123-456789abcdef"


def _parameters(values: dict[str, str]) -> dict[str, object]:
    return {
        name: {"AllowedValues": [value], "Default": value, "Type": "String"}
        for name, value in values.items()
    }


def _resource(resource_type: str, marker: str) -> dict[str, object]:
    return {"Properties": {"Marker": marker}, "Type": resource_type}


def _original_predecessor() -> dict[str, object]:
    resources = {
        f"ExistingOriginal{index:02d}": _resource("AWS::S3::Bucket", f"original-{index}")
        for index in range(40)
    }
    outputs = {"DeploymentReadiness": {"Value": "CORE_RUNTIME_ACTIVE_DRAFT_ONLY"}}
    outputs.update({f"ExistingOutput{index}": {"Value": str(index)} for index in range(6)})
    return {
        "AWSTemplateFormatVersion": "2010-09-09",
        "Description": "Exact active draft-only core",
        "Globals": {
            "Function": {"Environment": {"Variables": {"MR_LISTER_PHASE6_SCAFFOLD_ONLY": "false"}}}
        },
        "Metadata": {
            "MrListerPhase6CoreRuntimeStaging": {
                "Format": "mr-lister-phase6-core-runtime-transition-v1",
                "Mode": "ACTIVE_DRAFT_ONLY",
            }
        },
        "Outputs": outputs,
        "Parameters": _parameters(dict(verifier._EXPECTED_PARAMETERS)),
        "Resources": resources,
        "Transform": "AWS::Serverless-2016-10-31",
    }


def _processed(original: dict[str, object], *, count: int) -> dict[str, object]:
    resources = {
        f"ExistingProcessed{index:02d}": _resource("AWS::Lambda::Function", f"processed-{index}")
        for index in range(count)
    }
    return {
        key: deepcopy(value)
        for key, value in original.items()
        if key not in {"Globals", "Resources", "Transform"}
    } | {"Resources": resources}


def _target_original(predecessor: dict[str, object]) -> dict[str, object]:
    target = deepcopy(predecessor)
    target["Description"] = "Exact additive seller web edge"
    target["Parameters"] = _parameters(dict(verifier._TARGET_PARAMETERS))
    metadata = target["Metadata"]
    assert isinstance(metadata, dict)
    metadata["MrListerPhase6WebEdgeTransition"] = {
        "Format": "mr-lister-phase6-web-edge-transition-v1",
        "Mode": "WEB_EDGE_ACTIVE_DRAFT_ONLY",
    }
    resources = target["Resources"]
    assert isinstance(resources, dict)
    resources.update(
        {
            logical_id: _resource(resource_type, f"added-{logical_id}")
            for logical_id, resource_type in verifier._ORIGINAL_ADDITION_TYPES.items()
        }
    )
    outputs = target["Outputs"]
    assert isinstance(outputs, dict)
    outputs["DeploymentReadiness"] = {"Value": "SELLER_WEB_ACTIVE_DRAFT_ONLY"}
    outputs.update({name: {"Value": name} for name in verifier._OUTPUT_ADDITIONS})
    return target


def _target_processed(
    target_original: dict[str, object], predecessor_processed: dict[str, object]
) -> dict[str, object]:
    target = {
        key: deepcopy(value)
        for key, value in target_original.items()
        if key not in {"Globals", "Resources", "Transform"}
    }
    resources = deepcopy(predecessor_processed["Resources"])
    assert isinstance(resources, dict)
    resources.update(
        {
            logical_id: _resource(resource_type, f"processed-added-{logical_id}")
            for logical_id, resource_type in verifier._PROCESSED_ADDITION_TYPES.items()
        }
    )
    target["Resources"] = resources
    return target


def _observation(template: dict[str, object]) -> dict[str, object]:
    return {"StagesAvailable": ["Original", "Processed"], "TemplateBody": template}


def _write(path: Path, value: object) -> None:
    path.write_bytes(canonical_phase6_web_edge_change_set(value))


def _fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    predecessor_original = _original_predecessor()
    predecessor_processed = _processed(predecessor_original, count=47)
    target_original = _target_original(predecessor_original)
    target_processed = _target_processed(target_original, predecessor_processed)
    target_raw = canonical_phase6_web_edge_change_set(target_original)
    target_sha = sha256(target_raw).hexdigest()
    change_set_name = f"mr-lister-phase6-dev-web-edge-{target_sha[:12]}"
    change_set_id = (
        f"arn:aws:cloudformation:{verifier.REGION}:{verifier.ACCOUNT_ID}:"
        f"changeSet/{change_set_name}/{CHANGE_SET_UUID}"
    )
    change_set = {
        "Capabilities": ["CAPABILITY_NAMED_IAM"],
        "ChangeSetId": change_set_id,
        "ChangeSetName": change_set_name,
        "Changes": [
            {
                "ResourceChange": {
                    "Action": "Add",
                    "Details": [],
                    "LogicalResourceId": logical_id,
                    "ResourceType": resource_type,
                    "Scope": [],
                },
                "Type": "Resource",
            }
            for logical_id, resource_type in verifier._PROCESSED_ADDITION_TYPES.items()
        ],
        "CreationTime": "2026-08-27T04:00:00Z",
        "DeploymentConfig": {"DisableRollback": False, "Mode": "STANDARD"},
        "DeploymentMode": None,
        "Description": "Add the exact Phase 6 seller web edge",
        "ExecutionStatus": "AVAILABLE",
        "ImportExistingResources": False,
        "IncludeNestedStacks": False,
        "NotificationARNs": [],
        "OnStackFailure": None,
        "Parameters": [
            {"ParameterKey": key, "ParameterValue": value}
            for key, value in verifier._TARGET_PARAMETERS.items()
        ],
        "ParentChangeSetId": None,
        "RollbackConfiguration": {},
        "RootChangeSetId": None,
        "StackDriftStatus": None,
        "StackId": verifier.STACK_ID,
        "StackName": verifier.STACK_NAME,
        "Status": "CREATE_COMPLETE",
        "StatusReason": None,
        "Tags": [{"Key": key, "Value": value} for key, value in verifier._STACK_TAGS.items()],
    }
    values = {
        "predecessor-original.json": _observation(predecessor_original),
        "predecessor-processed.json": _observation(predecessor_processed),
        "target.json": target_original,
        "change-set.json": change_set,
        "target-original.json": _observation(target_original),
        "target-processed.json": _observation(target_processed),
    }
    paths: dict[str, Path] = {}
    for name, value in values.items():
        path = tmp_path / name
        _write(path, value)
        paths[name] = path
    monkeypatch.setattr(
        verifier,
        "PREDECESSOR_ORIGINAL_SHA256",
        sha256(canonical_phase6_web_edge_change_set(predecessor_original)).hexdigest(),
    )
    monkeypatch.setattr(verifier, "TARGET_ORIGINAL_SHA256", target_sha)
    return {"paths": paths, "templates": values}


def _kwargs(fixture: dict[str, Any]) -> dict[str, Path]:
    paths: dict[str, Path] = fixture["paths"]
    return {
        "predecessor_original_template_observation_path": paths["predecessor-original.json"],
        "predecessor_processed_template_observation_path": paths["predecessor-processed.json"],
        "target_template_path": paths["target.json"],
        "change_set_observation_path": paths["change-set.json"],
        "target_original_template_observation_path": paths["target-original.json"],
        "target_processed_template_observation_path": paths["target-processed.json"],
        "repository_root": paths["target.json"].parent,
    }


def _rewrite(path: Path, mutation: Any) -> None:
    value = json.loads(path.read_text(encoding="utf-8"))
    mutation(value)
    _write(path, value)


def test_accepts_only_the_exact_additive_web_edge_join(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = _fixture(tmp_path, monkeypatch)
    first = verify_phase6_web_edge_change_set(**_kwargs(fixture))
    second = verify_phase6_web_edge_change_set(**_kwargs(fixture))

    assert first == second
    assert first.change_set_type == "UPDATE"
    assert first.expected_execution_role_arn == EXPECTED_EXECUTION_ROLE_ARN
    assert len(first.original_added_resources) == 62
    assert len(first.processed_added_resources) == 78
    assert first.change_set_name.endswith(first.target_local_sha256[:12])
    assert len(first.canonical_sha256) == 64
    assert canonical_phase6_web_edge_change_set(asdict(first))


@pytest.mark.parametrize("stage", ["original", "processed"])
def test_rejects_any_existing_resource_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stage: str,
) -> None:
    fixture = _fixture(tmp_path, monkeypatch)
    paths: dict[str, Path] = fixture["paths"]
    if stage == "original":
        for name in ("target.json", "target-original.json"):
            path = paths[name]

            def mutate(value: dict[str, Any], *, wrapped: bool = name != "target.json") -> None:
                document = value["TemplateBody"] if wrapped else value
                document["Resources"]["ExistingOriginal00"]["Properties"]["Marker"] = "drift"

            _rewrite(path, mutate)
    else:
        _rewrite(
            paths["target-processed.json"],
            lambda value: value["TemplateBody"]["Resources"]["ExistingProcessed00"][
                "Properties"
            ].__setitem__("Marker", "drift"),
        )

    with pytest.raises(Phase6WebEdgeChangeSetError):
        verify_phase6_web_edge_change_set(**_kwargs(fixture))


@pytest.mark.parametrize("stage", ["original", "processed"])
def test_rejects_wrong_addition_inventory_or_type(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stage: str,
) -> None:
    fixture = _fixture(tmp_path, monkeypatch)
    paths: dict[str, Path] = fixture["paths"]
    logical_id = (
        next(iter(verifier._ORIGINAL_ADDITION_TYPES))
        if stage == "original"
        else next(iter(verifier._PROCESSED_ADDITION_TYPES))
    )
    names = (
        ("target.json", "target-original.json")
        if stage == "original"
        else ("target-processed.json",)
    )
    for name in names:
        path = paths[name]

        def mutate(value: dict[str, Any], *, wrapped: bool = name != "target.json") -> None:
            document = value["TemplateBody"] if wrapped else value
            document["Resources"][logical_id]["Type"] = "AWS::SNS::Topic"

        _rewrite(path, mutate)

    with pytest.raises(Phase6WebEdgeChangeSetError):
        verify_phase6_web_edge_change_set(**_kwargs(fixture))


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("Action", "Modify"),
        ("Details", [{"Evaluation": "Dynamic"}]),
        ("Scope", ["Properties"]),
        ("ResourceType", "AWS::SNS::Topic"),
        ("Replacement", "False"),
        ("PhysicalResourceId", "already-exists"),
    ],
)
def test_rejects_non_add_or_replacement_change_records(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    value: object,
) -> None:
    fixture = _fixture(tmp_path, monkeypatch)
    path: Path = fixture["paths"]["change-set.json"]
    _rewrite(
        path,
        lambda document: document["Changes"][0]["ResourceChange"].__setitem__(field, value),
    )

    with pytest.raises(Phase6WebEdgeChangeSetError):
        verify_phase6_web_edge_change_set(**_kwargs(fixture))


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("StackId", "arn:aws:cloudformation:us-west-2:384627057108:stack/other/id"),
        ("Capabilities", []),
        ("ImportExistingResources", True),
        ("IncludeNestedStacks", True),
        ("RoleARN", EXPECTED_EXECUTION_ROLE_ARN),
        ("ChangeSetType", "UPDATE"),
    ],
)
def test_rejects_change_set_scope_broadening_or_synthetic_fields(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    value: object,
) -> None:
    fixture = _fixture(tmp_path, monkeypatch)
    path: Path = fixture["paths"]["change-set.json"]
    _rewrite(path, lambda document: document.__setitem__(field, value))

    with pytest.raises(Phase6WebEdgeChangeSetError):
        verify_phase6_web_edge_change_set(**_kwargs(fixture))


def test_rejects_target_observation_that_is_not_the_sealed_local_template(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = _fixture(tmp_path, monkeypatch)
    path: Path = fixture["paths"]["target-original.json"]
    _rewrite(path, lambda value: value["TemplateBody"].__setitem__("Description", "drift"))

    with pytest.raises(Phase6WebEdgeChangeSetError):
        verify_phase6_web_edge_change_set(**_kwargs(fixture))


def test_rejects_parameter_drift_even_when_every_capture_agrees(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = _fixture(tmp_path, monkeypatch)
    paths: dict[str, Path] = fixture["paths"]
    for name in ("target.json", "target-original.json", "target-processed.json"):
        path = paths[name]

        def mutate(value: dict[str, Any], *, wrapped: bool = name != "target.json") -> None:
            document = value["TemplateBody"] if wrapped else value
            definition = document["Parameters"]["ApplicationOrigin"]
            definition["Default"] = "https://wrong.example"
            definition["AllowedValues"] = ["https://wrong.example"]

        _rewrite(path, mutate)
    _rewrite(
        paths["change-set.json"],
        lambda value: next(
            item for item in value["Parameters"] if item["ParameterKey"] == "ApplicationOrigin"
        ).__setitem__("ParameterValue", "https://wrong.example"),
    )

    with pytest.raises(Phase6WebEdgeChangeSetError):
        verify_phase6_web_edge_change_set(**_kwargs(fixture))


def test_rejects_wrong_predecessor_template_binding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = _fixture(tmp_path, monkeypatch)
    monkeypatch.setattr(verifier, "PREDECESSOR_ORIGINAL_SHA256", "0" * 64)

    with pytest.raises(Phase6WebEdgeChangeSetError):
        verify_phase6_web_edge_change_set(**_kwargs(fixture))


def test_rejects_noncanonical_evidence(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fixture = _fixture(tmp_path, monkeypatch)
    path: Path = fixture["paths"]["change-set.json"]
    path.write_bytes(path.read_bytes() + b" ")

    with pytest.raises(Phase6WebEdgeChangeSetError):
        verify_phase6_web_edge_change_set(**_kwargs(fixture))


def test_rejects_evidence_reached_through_symlinked_parent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = _fixture(tmp_path, monkeypatch)
    paths: dict[str, Path] = fixture["paths"]
    real = tmp_path / "real"
    real.mkdir()
    moved = real / "change-set.json"
    paths["change-set.json"].replace(moved)
    linked = tmp_path / "linked"
    linked.symlink_to(real, target_is_directory=True)
    paths["change-set.json"] = linked / "change-set.json"

    with pytest.raises(Phase6WebEdgeChangeSetError):
        verify_phase6_web_edge_change_set(**_kwargs(fixture))
