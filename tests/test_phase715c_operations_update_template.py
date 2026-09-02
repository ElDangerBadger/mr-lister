from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

import pytest

from tools.render_phase715c_operations_update import (
    BASE_TEMPLATE,
    BASE_TEMPLATE_SHA256,
    OPERATIONS_ARCHIVE_KEY,
    Phase715cOperationsUpdateError,
    render_operations_update_template,
    verify_operations_update_template,
    write_operations_update_template,
)


def _template() -> dict[str, object]:
    return json.loads(render_operations_update_template())


def test_update_changes_only_two_functions_and_keeps_every_trigger_disabled() -> None:
    base = json.loads(BASE_TEMPLATE.read_bytes())
    updated = _template()
    assert BASE_TEMPLATE_SHA256 == (
        "2a98ab2a7cf3fb04590f9f8cd3a30cc6c2e373421e70c70220be419b80ca7df2"
    )

    base_resources = base["Resources"]
    resources = updated["Resources"]
    assert updated["Description"] == base["Description"]
    assert set(updated["Parameters"]) - set(base["Parameters"]) == {
        "ApplicationReleaseFingerprint",
        "OperationsCodeS3Bucket",
        "OperationsCodeS3ObjectVersion",
        "OperationsReleaseFingerprint",
    }
    assert set(updated["Outputs"]) - set(base["Outputs"]) == {
        "OperationsReleaseFingerprint",
        "OperationsRuntimeReadiness",
    }
    changed = {name for name in resources if resources[name] != base_resources.get(name)}
    assert changed == {"PublicationRecoveryFunction", "PublicationRetentionFunction"}
    assert all(
        resource["Properties"]["Enabled"] is False
        for resource in resources.values()
        if resource["Type"] == "AWS::Lambda::EventSourceMapping"
    )
    assert all(
        resource["Properties"]["State"] == "DISABLED"
        for resource in resources.values()
        if resource["Type"] == "AWS::Events::Rule"
    )

    for name in ("Recovery", "Retention"):
        properties = resources[f"Publication{name}Function"]["Properties"]
        assert properties["ReservedConcurrentExecutions"] == 1
        assert properties["CodeUri"] == {
            "Bucket": {"Ref": "OperationsCodeS3Bucket"},
            "Key": {"Fn::Sub": OPERATIONS_ARCHIVE_KEY},
            "Version": {"Ref": "OperationsCodeS3ObjectVersion"},
        }
        variables = properties["Environment"]["Variables"]
        assert variables["MR_LISTER_PHASE7_DISPATCHER_ENABLED"] == "false"
        assert variables["MR_LISTER_PHASE7_WORKER_ENABLED"] == "false"
        assert variables["MR_LISTER_RELEASE_FINGERPRINT"] == {
            "Ref": "ApplicationReleaseFingerprint"
        }

    for name in ("Query", "Request", "Dispatcher", "Worker"):
        assert (
            resources[f"Publication{name}Function"] == base_resources[f"Publication{name}Function"]
        )


def test_update_adds_no_provider_secret_route_or_iam_delta() -> None:
    base = json.loads(BASE_TEMPLATE.read_bytes())
    updated = _template()
    assert {
        name: resource
        for name, resource in updated["Resources"].items()
        if resource["Type"] == "AWS::IAM::Role"
    } == {
        name: resource
        for name, resource in base["Resources"].items()
        if resource["Type"] == "AWS::IAM::Role"
    }
    serialized = json.dumps(updated, sort_keys=True)
    assert "MR_LISTER_PRINTIFY_SECRET_ARN" not in serialized
    assert "GENERAL_AVAILABILITY_ENABLED" not in serialized
    assert "FunctionUrlConfig" not in serialized
    assert updated["Outputs"]["SellerPublicationEnabled"] == {"Value": "false"}
    assert updated["Outputs"]["ProviderMutationEnabled"] == {"Value": "false"}


def test_base_drift_and_noncanonical_generated_template_are_refused(tmp_path: Path) -> None:
    drift = json.loads(BASE_TEMPLATE.read_bytes())
    drift["Description"] = "drifted"
    drift_path = tmp_path / "base.json"
    drift_path.write_text(json.dumps(drift), encoding="utf-8")
    with pytest.raises(Phase715cOperationsUpdateError):
        render_operations_update_template(base_template_path=drift_path)

    output = tmp_path / "operations.json"
    created, fingerprint = write_operations_update_template(output)
    assert created == output.resolve()
    assert verify_operations_update_template(output) == fingerprint
    changed = deepcopy(json.loads(output.read_bytes()))
    changed["Outputs"]["SellerPublicationEnabled"]["Value"] = "true"
    output.write_text(json.dumps(changed), encoding="utf-8")
    with pytest.raises(Phase715cOperationsUpdateError):
        verify_operations_update_template(output)


def test_renderer_never_overwrites_an_existing_output(tmp_path: Path) -> None:
    destination = tmp_path / "operations.json"
    destination.write_text("keep", encoding="utf-8")
    with pytest.raises(Phase715cOperationsUpdateError):
        write_operations_update_template(destination)
    assert destination.read_text(encoding="utf-8") == "keep"
