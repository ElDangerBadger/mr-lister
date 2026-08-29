from __future__ import annotations

from copy import deepcopy

import pytest

import tools.verify_phase6_dispatch_filter_change_set as verifier


def _change_set() -> dict[str, object]:
    name = "mr-lister-phase6-dev-dispatch-filter-abcdef123456"
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
                    "AfterContext": "after",
                    "BeforeContext": "before",
                    "Details": [
                        {
                            "ChangeSource": "DirectModification",
                            "Evaluation": "Static",
                            "Target": {
                                "Attribute": "Properties",
                                "AttributeChangeType": "Modify",
                                "Name": "FilterCriteria",
                                "Path": "/Properties/FilterCriteria",
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


def test_accepts_exact_one_resource_in_place_change() -> None:
    result = verifier.verify_phase6_dispatch_filter_change_set(change_set=_change_set())
    assert result.format == verifier.FORMAT
    assert result.predecessor_template_sha256 == verifier.PREDECESSOR_TEMPLATE_SHA256
    assert result.target_template_sha256 == verifier.DISPATCH_FILTER_CORRECTION_TEMPLATE_SHA256


@pytest.mark.parametrize(
    ("path", "value"),
    (
        (("Status",), "FAILED"),
        (("ExecutionStatus",), "OBSOLETE"),
        (("StackName",), "another"),
        (("Changes", 0, "ResourceChange", "Action"), "Add"),
        (("Changes", 0, "ResourceChange", "LogicalResourceId"), "DispatcherFunction"),
        (("Changes", 0, "ResourceChange", "Replacement"), "True"),
        (("Changes", 0, "ResourceChange", "Scope"), ["Metadata"]),
        (("Changes", 0, "ResourceChange", "Details", 0, "ChangeSource"), "ParameterReference"),
        (("Changes", 0, "ResourceChange", "Details", 0, "Target", "Name"), "Enabled"),
        (
            ("Changes", 0, "ResourceChange", "Details", 0, "Target", "Path"),
            "/Properties/Enabled",
        ),
    ),
)
def test_rejects_authority_drift(path: tuple[object, ...], value: object) -> None:
    document = deepcopy(_change_set())
    nested: object = document
    for component in path[:-1]:
        nested = nested[component]  # type: ignore[index]
    nested[path[-1]] = value  # type: ignore[index]
    with pytest.raises(verifier.Phase6DispatchFilterChangeSetError):
        verifier.verify_phase6_dispatch_filter_change_set(change_set=document)


def test_rejects_any_additional_resource_change() -> None:
    document = _change_set()
    document["Changes"].append(deepcopy(document["Changes"][0]))  # type: ignore[union-attr]
    with pytest.raises(verifier.Phase6DispatchFilterChangeSetError):
        verifier.verify_phase6_dispatch_filter_change_set(change_set=document)


def test_validates_after_value_when_cloudformation_returns_it() -> None:
    document = _change_set()
    target = document["Changes"][0]["ResourceChange"]["Details"][0]["Target"]  # type: ignore[index]
    target["AfterValue"] = "{}"
    with pytest.raises(verifier.Phase6DispatchFilterChangeSetError):
        verifier.verify_phase6_dispatch_filter_change_set(change_set=document)
