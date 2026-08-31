from __future__ import annotations

from copy import deepcopy

import pytest

import tools.verify_phase6_seller_command_runtime_envelope_change_set as verifier


def _change_set() -> dict[str, object]:
    name = verifier.EXPECTED_CHANGE_SET_NAME
    return {
        "ChangeSetId": (
            f"arn:aws:cloudformation:us-west-2:384627057108:changeSet/{name}/"
            "12345678-1234-1234-1234-123456789abc"
        ),
        "ChangeSetName": name,
        "ChangeSetType": "UPDATE",
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
                                "Name": "MemorySize",
                                "RequiresRecreation": "Never",
                            },
                        }
                    ],
                    "LogicalResourceId": verifier.LOGICAL_ID,
                    "PhysicalResourceId": verifier.PHYSICAL_ID,
                    "Replacement": "False",
                    "ResourceType": verifier.RESOURCE_TYPE,
                    "Scope": ["Properties"],
                },
                "Type": "Resource",
            }
        ],
        "ExecutionStatus": "AVAILABLE",
        "RoleARN": verifier.EXPECTED_EXECUTION_ROLE_ARN,
        "StackId": verifier.STACK_ID,
        "StackName": "mr-lister-phase6-dev",
        "Status": "CREATE_COMPLETE",
    }


def _property_value_change_set() -> dict[str, object]:
    document = _change_set()
    resource = document["Changes"][0]["ResourceChange"]  # type: ignore[index]
    target = resource["Details"][0]["Target"]  # type: ignore[index]
    resource["BeforeContext"] = "before-memory-256"
    resource["AfterContext"] = "after-memory-512"
    target["AttributeChangeType"] = "Modify"
    target["Path"] = "/Properties/MemorySize"
    target["BeforeValue"] = "256"
    target["AfterValue"] = "512"
    return document


def _verify(
    document: dict[str, object],
    *,
    predecessor_template_sha256: str = verifier.PREDECESSOR_TEMPLATE_SHA256,
    target_template_sha256: str = verifier.TARGET_TEMPLATE_SHA256,
) -> verifier.VerifiedPhase6SellerCommandRuntimeEnvelopeChangeSet:
    return verifier.verify_phase6_seller_command_runtime_envelope_change_set(
        change_set=document,
        predecessor_template_sha256=predecessor_template_sha256,
        target_template_sha256=target_template_sha256,
    )


def test_accepts_exact_one_resource_in_place_change() -> None:
    result = _verify(_change_set())
    assert result.format == verifier.FORMAT
    assert result.change_set_name == verifier.EXPECTED_CHANGE_SET_NAME
    assert result.predecessor_template_sha256 == verifier.PREDECESSOR_TEMPLATE_SHA256
    assert result.target_template_sha256 == verifier.TARGET_TEMPLATE_SHA256


def test_accepts_exact_property_value_description() -> None:
    result = _verify(_property_value_change_set())
    assert result.change_set_name == verifier.EXPECTED_CHANGE_SET_NAME


@pytest.mark.parametrize(
    ("path", "value"),
    (
        (("Status",), "FAILED"),
        (("ExecutionStatus",), "OBSOLETE"),
        (("ChangeSetType",), "CREATE"),
        (("StackName",), "another"),
        (("RoleARN",), "arn:aws:iam::384627057108:role/another"),
        (("ChangeSetName",), "mr-lister-phase6-dev-seller-command-memory-000000000000"),
        (("Changes", 0, "ResourceChange", "Action"), "Add"),
        (("Changes", 0, "ResourceChange", "LogicalResourceId"), "ReviewQueryApiFunction"),
        (("Changes", 0, "ResourceChange", "PhysicalResourceId"), "another-function"),
        (("Changes", 0, "ResourceChange", "ResourceType"), "AWS::IAM::Role"),
        (("Changes", 0, "ResourceChange", "Replacement"), "True"),
        (("Changes", 0, "ResourceChange", "Scope"), ["Metadata"]),
        (("Changes", 0, "ResourceChange", "Details", 0, "ChangeSource"), "ParameterReference"),
        (("Changes", 0, "ResourceChange", "Details", 0, "Evaluation"), "Dynamic"),
        (("Changes", 0, "ResourceChange", "Details", 0, "Target", "Name"), "Timeout"),
        (
            ("Changes", 0, "ResourceChange", "Details", 0, "Target", "RequiresRecreation"),
            "Always",
        ),
    ),
)
def test_rejects_authority_drift(path: tuple[object, ...], value: object) -> None:
    document = deepcopy(_change_set())
    nested: object = document
    for component in path[:-1]:
        nested = nested[component]  # type: ignore[index]
    nested[path[-1]] = value  # type: ignore[index]
    with pytest.raises(verifier.Phase6SellerCommandRuntimeEnvelopeChangeSetError):
        _verify(document)


def test_rejects_any_additional_resource_change() -> None:
    document = _change_set()
    document["Changes"].append(deepcopy(document["Changes"][0]))  # type: ignore[union-attr]
    with pytest.raises(verifier.Phase6SellerCommandRuntimeEnvelopeChangeSetError):
        _verify(document)


def test_rejects_paginated_change_set_evidence() -> None:
    document = _change_set()
    document["NextToken"] = "another-page-can-contain-more-changes"
    with pytest.raises(verifier.Phase6SellerCommandRuntimeEnvelopeChangeSetError):
        _verify(document)


@pytest.mark.parametrize(
    ("predecessor", "target"),
    (
        ("0" * 64, verifier.TARGET_TEMPLATE_SHA256),
        (verifier.PREDECESSOR_TEMPLATE_SHA256, "1" * 64),
    ),
)
def test_requires_exact_sealed_template_sha_authorities(predecessor: str, target: str) -> None:
    with pytest.raises(verifier.Phase6SellerCommandRuntimeEnvelopeChangeSetError):
        _verify(
            _change_set(),
            predecessor_template_sha256=predecessor,
            target_template_sha256=target,
        )


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("AttributeChangeType", "Add"),
        ("Path", "/Properties/Timeout"),
        ("BeforeValue", "512"),
        ("AfterValue", "256"),
    ),
)
def test_property_value_description_is_exact(field: str, value: str) -> None:
    document = _property_value_change_set()
    target = document["Changes"][0]["ResourceChange"]["Details"][0]["Target"]  # type: ignore[index]
    target[field] = value
    with pytest.raises(verifier.Phase6SellerCommandRuntimeEnvelopeChangeSetError):
        _verify(document)


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("BeforeContext", ""),
        ("AfterContext", ""),
        ("AfterContext", "before-memory-256"),
    ),
)
def test_property_value_contexts_must_be_present_and_distinct(field: str, value: str) -> None:
    document = _property_value_change_set()
    resource = document["Changes"][0]["ResourceChange"]  # type: ignore[index]
    resource[field] = value
    with pytest.raises(verifier.Phase6SellerCommandRuntimeEnvelopeChangeSetError):
        _verify(document)


@pytest.mark.parametrize(
    ("container", "field", "value"),
    (
        ("resource", "BeforeContext", "before"),
        ("target", "Path", "/Properties/MemorySize"),
        ("target", "AttributeChangeType", "Modify"),
        ("target", "BeforeValue", "256"),
        ("target", "AfterValue", "512"),
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
    with pytest.raises(verifier.Phase6SellerCommandRuntimeEnvelopeChangeSetError):
        _verify(document)
