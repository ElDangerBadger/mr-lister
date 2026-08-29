from __future__ import annotations

import json
from copy import deepcopy

import pytest

import tools.verify_phase6_dispatch_filter_change_set as verifier


def _change_set() -> dict[str, object]:
    name = verifier.EXPECTED_CHANGE_SET_NAME
    return {
        "ChangeSetId": (
            f"arn:aws:cloudformation:us-west-2:384627057108:changeSet/{name}/"
            "12345678-1234-1234-1234-123456789abc"
        ),
        "ChangeSetName": name,
        "Changes": [
            {
                "ResourceChange": {
                    "Action": "Modify",
                    "Details": [
                        {
                            "ChangeSource": "DirectModification",
                            "Evaluation": "Static",
                            "Target": {
                                "Attribute": "Properties",
                                "Name": "FilterCriteria",
                                "RequiresRecreation": "Never",
                            },
                        }
                    ],
                    "LogicalResourceId": verifier.LOGICAL_ID,
                    "PhysicalResourceId": "exact-existing-mapping",
                    "Replacement": "False",
                    "ResourceType": verifier.RESOURCE_TYPE,
                    "Scope": ["Properties"],
                },
                "Type": "Resource",
            }
        ],
        "ExecutionStatus": "AVAILABLE",
        "StackId": verifier.STACK_ID,
        "StackName": "mr-lister-phase6-dev",
        "Status": "CREATE_COMPLETE",
    }


def _property_value_change_set() -> dict[str, object]:
    document = _change_set()
    resource = document["Changes"][0]["ResourceChange"]  # type: ignore[index]
    target = resource["Details"][0]["Target"]  # type: ignore[index]
    resource["AfterContext"] = "after"
    resource["BeforeContext"] = "before"
    target["AttributeChangeType"] = "Modify"
    target["Path"] = "/Properties/FilterCriteria"
    return document


def _verify(
    document: dict[str, object],
    *,
    predecessor_template_sha256: str = verifier.PREDECESSOR_TEMPLATE_SHA256,
    target_template_sha256: str = verifier.DISPATCH_FILTER_CORRECTION_TEMPLATE_SHA256,
) -> verifier.VerifiedPhase6DispatchFilterChangeSet:
    return verifier.verify_phase6_dispatch_filter_change_set(
        change_set=document,
        predecessor_template_sha256=predecessor_template_sha256,
        target_template_sha256=target_template_sha256,
    )


def test_accepts_exact_one_resource_in_place_change() -> None:
    result = _verify(_change_set())
    assert result.format == verifier.FORMAT
    assert result.predecessor_template_sha256 == verifier.PREDECESSOR_TEMPLATE_SHA256
    assert result.target_template_sha256 == verifier.DISPATCH_FILTER_CORRECTION_TEMPLATE_SHA256


@pytest.mark.parametrize(
    ("path", "value"),
    (
        (("Status",), "FAILED"),
        (("ExecutionStatus",), "OBSOLETE"),
        (("StackName",), "another"),
        (("ChangeSetName",), "mr-lister-phase6-dev-dispatch-filter-000000000000"),
        (("Changes", 0, "ResourceChange", "Action"), "Add"),
        (("Changes", 0, "ResourceChange", "LogicalResourceId"), "DispatcherFunction"),
        (("Changes", 0, "ResourceChange", "Replacement"), "True"),
        (("Changes", 0, "ResourceChange", "Scope"), ["Metadata"]),
        (("Changes", 0, "ResourceChange", "Details", 0, "ChangeSource"), "ParameterReference"),
        (("Changes", 0, "ResourceChange", "Details", 0, "Target", "Name"), "Enabled"),
    ),
)
def test_rejects_authority_drift(path: tuple[object, ...], value: object) -> None:
    document = deepcopy(_change_set())
    nested: object = document
    for component in path[:-1]:
        nested = nested[component]  # type: ignore[index]
    nested[path[-1]] = value  # type: ignore[index]
    with pytest.raises(verifier.Phase6DispatchFilterChangeSetError):
        _verify(document)


def test_rejects_any_additional_resource_change() -> None:
    document = _change_set()
    document["Changes"].append(deepcopy(document["Changes"][0]))  # type: ignore[union-attr]
    with pytest.raises(verifier.Phase6DispatchFilterChangeSetError):
        _verify(document)


def test_rejects_paginated_change_set_evidence() -> None:
    document = _change_set()
    document["NextToken"] = "another-page-can-contain-more-changes"
    with pytest.raises(verifier.Phase6DispatchFilterChangeSetError):
        _verify(document)


def test_accepts_and_validates_property_value_shape_when_available() -> None:
    document = _property_value_change_set()
    target = document["Changes"][0]["ResourceChange"]["Details"][0]["Target"]  # type: ignore[index]
    expected_after = {
        "Filters": [{"Pattern": json.dumps(verifier.SAFE_FILTER, separators=(",", ":"))}]
    }
    target["AfterValue"] = verifier._detail_value(expected_after)
    assert _verify(document).change_set_name == verifier.EXPECTED_CHANGE_SET_NAME

    target["AfterValue"] = "{}"
    with pytest.raises(verifier.Phase6DispatchFilterChangeSetError):
        _verify(document)


@pytest.mark.parametrize(
    ("predecessor", "target"),
    (
        ("0" * 64, verifier.DISPATCH_FILTER_CORRECTION_TEMPLATE_SHA256),
        (verifier.PREDECESSOR_TEMPLATE_SHA256, "1" * 64),
    ),
)
def test_requires_exact_sealed_template_sha_authorities(predecessor: str, target: str) -> None:
    with pytest.raises(verifier.Phase6DispatchFilterChangeSetError):
        _verify(
            _change_set(),
            predecessor_template_sha256=predecessor,
            target_template_sha256=target,
        )


@pytest.mark.parametrize(
    ("container", "field", "value"),
    (
        ("resource", "BeforeContext", "before"),
        ("target", "Path", "/Properties/FilterCriteria"),
        ("target", "AttributeChangeType", "Modify"),
        ("target", "AfterValue", "exact-looking"),
    ),
)
def test_standard_shape_rejects_partial_property_value_fields(
    container: str,
    field: str,
    value: str,
) -> None:
    document = _change_set()
    resource = document["Changes"][0]["ResourceChange"]  # type: ignore[index]
    target = resource["Details"][0]["Target"]  # type: ignore[index]
    selected = resource if container == "resource" else target
    selected[field] = value
    with pytest.raises(verifier.Phase6DispatchFilterChangeSetError):
        _verify(document)


def test_requires_nonempty_physical_resource_identity() -> None:
    document = _change_set()
    resource = document["Changes"][0]["ResourceChange"]  # type: ignore[index]
    resource["PhysicalResourceId"] = ""
    with pytest.raises(verifier.Phase6DispatchFilterChangeSetError):
        _verify(document)
