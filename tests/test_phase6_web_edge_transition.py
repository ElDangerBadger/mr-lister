from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from copy import deepcopy
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import patch

import pytest

import tools.render_phase6_web_edge_transition as web_transition
from tools.render_phase6_core_sam_staging import Phase6CoreSamStagingBinding
from tools.render_phase6_web_edge_transition import (
    SOURCE_TEMPLATE_SHA256,
    WEB_EDGE_READINESS,
    WEB_EDGE_TEMPLATE_OUTPUT,
    Phase6WebEdgeTransitionError,
    render_phase6_web_edge_transition,
    verify_rendered_phase6_web_edge_transition,
    write_phase6_web_edge_transition,
)

ACCOUNT_ID = "123456789012"
CERTIFICATE_ARN = (
    "arn:aws:acm:us-east-1:123456789012:certificate/12345678-1234-1234-1234-1234567890ab"
)
ORIGIN = "https://massskutiny.com"
CODE_URI = {
    "Bucket": "mr-lister-phase6-artifacts-dev-123456789012-us-west-2",
    "Key": "private/deployments/lambda/releases/release/phase6-lambda.zip",
    "Version": "ExactVersion-1",
}
BINDING = cast(
    Phase6CoreSamStagingBinding,
    SimpleNamespace(account_id=ACCOUNT_ID, application_origin=ORIGIN),
)


def _canonical(document: object) -> bytes:
    return (
        json.dumps(document, allow_nan=False, indent=2, separators=(",", ": "), sort_keys=True)
        + "\n"
    ).encode()


def _active_document() -> dict[str, object]:
    resources: dict[str, object] = {
        "DispatcherFunction": {
            "Properties": {
                "CodeUri": deepcopy(CODE_URI),
                "Handler": "phase6_lambda.dispatcher_handler",
                "Timeout": 120,
            },
            "Type": "AWS::Serverless::Function",
        },
        "SettlementFunction": {
            "Properties": {
                "CodeUri": deepcopy(CODE_URI),
                "Handler": "phase6_lambda.settlement_handler",
                "Timeout": 120,
            },
            "Type": "AWS::Serverless::Function",
        },
    }
    resources.update(
        {
            f"CoreResource{index:02d}": {
                "Properties": {"Name": f"core-{index:02d}"},
                "Type": "AWS::Logs::LogGroup",
            }
            for index in range(38)
        }
    )
    return {
        "AWSTemplateFormatVersion": "2010-09-09",
        "Description": "Exact active backend",
        "Globals": {
            "Function": {"Environment": {"Variables": {"MR_LISTER_PHASE6_SCAFFOLD_ONLY": "false"}}}
        },
        "Metadata": {
            "MrListerPhase6CoreRuntimeStaging": {
                "Format": "mr-lister-phase6-core-runtime-transition-v1",
                "Mode": "ACTIVE_DRAFT_ONLY",
            }
        },
        "Outputs": {
            "DeploymentReadiness": {
                "Description": "Backend active; web absent.",
                "Value": "CORE_RUNTIME_ACTIVE_DRAFT_ONLY",
            },
            **{f"CoreOutput{index}": {"Value": f"core-output-{index}"} for index in range(6)},
        },
        "Parameters": {
            "EnvironmentName": {
                "AllowedValues": ["dev"],
                "Default": "dev",
                "Type": "String",
            },
            **{
                f"CoreParameter{index}": {
                    "AllowedValues": [f"value-{index}"],
                    "Default": f"value-{index}",
                    "Type": "String",
                }
                for index in range(8)
            },
        },
        "Resources": resources,
        "Transform": "AWS::Serverless-2016-10-31",
    }


def _full_document(active: dict[str, object]) -> dict[str, object]:
    resources = deepcopy(active["Resources"])
    assert isinstance(resources, dict)
    for logical_id, handler in {
        "ReviewQueryApiFunction": "phase6_lambda.review_query_api_handler",
        "SellerCommandApiFunction": "phase6_lambda.seller_command_api_handler",
        "UploadApiFunction": "phase6_lambda.upload_api_handler",
    }.items():
        resources[logical_id] = {
            "Properties": {
                "CodeUri": deepcopy(CODE_URI),
                "Handler": handler,
            },
            "Type": "AWS::Serverless::Function",
        }
    resources["SellerHttpApi"] = {
        "Properties": {
            "DefinitionBody": {"openapi": "3.0.1", "paths": {}},
            "DisableExecuteApiEndpoint": True,
        },
        "Type": "AWS::Serverless::HttpApi",
    }
    resources["SellerWebDistribution"] = {
        "Properties": {"DistributionConfig": {"Enabled": False}},
        "Type": "AWS::CloudFront::Distribution",
    }
    resources["OperationalAlarmTopicKey"] = {
        "Properties": {
            "Tags": [
                {"Key": "Project", "Value": "MrLister"},
                {"Key": "Environment", "Value": {"Ref": "EnvironmentName"}},
            ]
        },
        "Type": "AWS::KMS::Key",
    }
    resources.update(
        {
            f"AddedResource{index:02d}": {
                "Properties": {"Name": f"added-{index:02d}"},
                "Type": "AWS::CloudWatch::Alarm",
            }
            for index in range(56)
        }
    )

    parameters = deepcopy(active["Parameters"])
    assert isinstance(parameters, dict)
    parameters["ApplicationCertificateArn"] = {
        "AllowedValues": [CERTIFICATE_ARN],
        "Default": CERTIFICATE_ARN,
        "Type": "String",
    }
    outputs = deepcopy(active["Outputs"])
    assert isinstance(outputs, dict)
    outputs.update(
        {f"WebOutput{index:02d}": {"Value": f"web-output-{index:02d}"} for index in range(12)}
    )
    return {
        **deepcopy(active),
        "Globals": {
            "Function": {"Environment": {"Variables": {"MR_LISTER_PHASE6_SCAFFOLD_ONLY": "true"}}}
        },
        "Metadata": {
            "MrListerPhase6StagedDeployment": {
                "Format": "mr-lister-phase6-sam-staged-v1",
                "SourceTemplateSha256": SOURCE_TEMPLATE_SHA256,
            }
        },
        "Outputs": outputs,
        "Parameters": parameters,
        "Resources": resources,
    }


def _options(repository: Path) -> dict[str, object]:
    repository.mkdir(parents=True, exist_ok=True)
    for filename in (
        "foundation.json",
        "endpoint.json",
        "agentcore-object.json",
        "runtime-v1.json",
        "lambda-object.json",
    ):
        (repository / filename).write_text("{}\n", encoding="utf-8")
    (repository / "deployment").mkdir(exist_ok=True)
    (repository / "artifacts").mkdir(exist_ok=True)
    return {
        "application_certificate_arn": CERTIFICATE_ARN,
        "foundation_binding_path": repository / "foundation.json",
        "agentcore_endpoint_observation_path": repository / "endpoint.json",
        "agentcore_object_evidence_path": repository / "agentcore-object.json",
        "agentcore_runtime_v1_evidence_path": repository / "runtime-v1.json",
        "lambda_object_evidence_path": repository / "lambda-object.json",
        "repository_root": repository,
        "deployment_root": repository / "deployment",
        "artifact_root": repository / "artifacts",
    }


@contextmanager
def _sealed_renderers(
    active: dict[str, object],
    full: dict[str, object],
) -> Iterator[None]:
    active_raw = _canonical(active)
    full_raw = _canonical(full)
    try:
        expected = web_transition._render_additive_target(
            active,
            full,
            active_sha256=sha256(active_raw).hexdigest(),
            full_staged_sha256=sha256(full_raw).hexdigest(),
            application_certificate_arn=CERTIFICATE_ARN,
            application_origin=ORIGIN,
        )
        target_sha256 = sha256(_canonical(expected)).hexdigest()
    except ValueError:
        target_sha256 = "0" * 64
    with (
        patch.object(
            web_transition,
            "ACTIVE_CORE_TEMPLATE_SHA256",
            sha256(active_raw).hexdigest(),
        ),
        patch.object(
            web_transition,
            "WEB_EDGE_TEMPLATE_SHA256",
            target_sha256,
        ),
        patch.object(
            web_transition.core_transition,
            "render_phase6_core_runtime_transition",
            return_value=active_raw,
        ),
        patch.object(
            web_transition,
            "_full_staging_binding",
            return_value=object(),
        ),
        patch.object(
            web_transition.full_staging,
            "render_phase6_sam_staged_template",
            return_value=full_raw,
        ),
    ):
        yield


def _render(
    repository: Path,
    *,
    active: dict[str, object] | None = None,
    full: dict[str, object] | None = None,
) -> dict[str, object]:
    selected_active = active or _active_document()
    selected_full = full or _full_document(selected_active)
    with _sealed_renderers(selected_active, selected_full):
        return json.loads(render_phase6_web_edge_transition(BINDING, **_options(repository)))


def test_render_adds_web_closure_without_mutating_active_resources(tmp_path: Path) -> None:
    active = _active_document()
    target = _render(tmp_path, active=active, full=_full_document(active))

    assert len(target["Resources"]) == 102
    assert len(target["Parameters"]) == 10
    assert len(target["Outputs"]) == 19
    for logical_id, resource in active["Resources"].items():
        assert target["Resources"][logical_id] == resource
    assert target["Parameters"]["ApplicationCertificateArn"] == {
        "AllowedValues": [CERTIFICATE_ARN],
        "Default": CERTIFICATE_ARN,
        "Type": "String",
    }
    assert (
        target["Resources"]["SellerWebDistribution"]["Properties"]["DistributionConfig"]["Enabled"]
        is True
    )
    assert "DisableExecuteApiEndpoint" not in target["Resources"]["SellerHttpApi"]["Properties"]
    assert target["Resources"]["OperationalAlarmTopicKey"]["Properties"]["Tags"] == [
        {"Key": "Project", "Value": "MrLister"},
        {"Key": "Environment", "Value": {"Ref": "EnvironmentName"}},
        {"Key": "DataClassification", "Value": "OperationalAlarmTransport"},
    ]
    assert target["Outputs"]["DeploymentReadiness"]["Value"] == WEB_EDGE_READINESS


def test_render_rejects_timeout_regression_even_when_input_is_resealed(tmp_path: Path) -> None:
    active = _active_document()
    active["Resources"]["DispatcherFunction"]["Properties"]["Timeout"] = 30

    with pytest.raises(Phase6WebEdgeTransitionError):
        _render(tmp_path, active=active, full=_full_document(active))


def test_render_rejects_new_function_code_drift(tmp_path: Path) -> None:
    active = _active_document()
    full = _full_document(active)
    full["Resources"]["UploadApiFunction"]["Properties"]["CodeUri"]["Version"] = "DifferentVersion"

    with pytest.raises(Phase6WebEdgeTransitionError):
        _render(tmp_path, active=active, full=full)


def test_render_rejects_missing_or_extra_resource(tmp_path: Path) -> None:
    active = _active_document()
    full = _full_document(active)
    full["Resources"].pop("AddedResource00")

    with pytest.raises(Phase6WebEdgeTransitionError):
        _render(tmp_path, active=active, full=full)


def test_render_rejects_source_authority_drift(tmp_path: Path) -> None:
    active = _active_document()
    full = _full_document(active)
    full["Metadata"]["MrListerPhase6StagedDeployment"]["SourceTemplateSha256"] = "f" * 64

    with pytest.raises(Phase6WebEdgeTransitionError):
        _render(tmp_path, active=active, full=full)


def test_render_rejects_scaffold_regression(tmp_path: Path) -> None:
    active = _active_document()
    active["Globals"]["Function"]["Environment"]["Variables"]["MR_LISTER_PHASE6_SCAFFOLD_ONLY"] = (
        "true"
    )

    with pytest.raises(Phase6WebEdgeTransitionError):
        _render(tmp_path, active=active, full=_full_document(active))


def test_render_rejects_dangling_intrinsic_reference(tmp_path: Path) -> None:
    active = _active_document()
    full = _full_document(active)
    full["Resources"]["AddedResource00"]["Properties"]["Target"] = {"Ref": "MissingResource"}

    with pytest.raises(Phase6WebEdgeTransitionError):
        _render(tmp_path, active=active, full=full)


def test_render_rejects_wrong_certificate_account_before_rendering(tmp_path: Path) -> None:
    options = _options(tmp_path)
    options["application_certificate_arn"] = CERTIFICATE_ARN.replace(ACCOUNT_ID, "999999999999")

    with (
        patch.object(
            web_transition.core_transition,
            "render_phase6_core_runtime_transition",
        ) as active_renderer,
        pytest.raises(Phase6WebEdgeTransitionError),
    ):
        render_phase6_web_edge_transition(BINDING, **options)
    active_renderer.assert_not_called()


@pytest.mark.parametrize(
    ("option_name", "is_directory"),
    (
        ("foundation_binding_path", False),
        ("agentcore_endpoint_observation_path", False),
        ("agentcore_object_evidence_path", False),
        ("agentcore_runtime_v1_evidence_path", False),
        ("lambda_object_evidence_path", False),
        ("deployment_root", True),
        ("artifact_root", True),
    ),
)
def test_render_rejects_every_input_outside_repository(
    tmp_path: Path,
    option_name: str,
    is_directory: bool,
) -> None:
    repository = tmp_path / "repository"
    options = _options(repository)
    external = tmp_path / f"external-{option_name}"
    if is_directory:
        external.mkdir()
    else:
        external.write_text("{}\n", encoding="utf-8")
    options[option_name] = external

    with (
        patch.object(
            web_transition.core_transition,
            "render_phase6_core_runtime_transition",
        ) as active_renderer,
        pytest.raises(Phase6WebEdgeTransitionError),
    ):
        render_phase6_web_edge_transition(BINDING, **options)
    active_renderer.assert_not_called()


def test_render_rejects_input_beneath_symlinked_repository_parent(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    options = _options(repository)
    external = tmp_path / "external"
    external.mkdir()
    (external / "foundation.json").write_text("{}\n", encoding="utf-8")
    linked_parent = repository / "linked"
    linked_parent.symlink_to(external, target_is_directory=True)
    options["foundation_binding_path"] = linked_parent / "foundation.json"

    with (
        patch.object(
            web_transition.core_transition,
            "render_phase6_core_runtime_transition",
        ) as active_renderer,
        pytest.raises(Phase6WebEdgeTransitionError),
    ):
        render_phase6_web_edge_transition(BINDING, **options)
    active_renderer.assert_not_called()


def test_render_rejects_noncanonical_upstream_bytes(tmp_path: Path) -> None:
    active = _active_document()
    with (
        patch.object(
            web_transition.core_transition,
            "render_phase6_core_runtime_transition",
            return_value=json.dumps(active).encode(),
        ),
        pytest.raises(Phase6WebEdgeTransitionError),
    ):
        render_phase6_web_edge_transition(BINDING, **_options(tmp_path))


def test_write_is_private_create_only_and_verify_is_exact(tmp_path: Path) -> None:
    target = _canonical({"sealed": True})
    options = _options(tmp_path)
    with patch.object(
        web_transition,
        "render_phase6_web_edge_transition",
        return_value=target,
    ):
        destination = write_phase6_web_edge_transition(BINDING, **options)
        assert destination == tmp_path / WEB_EDGE_TEMPLATE_OUTPUT
        assert destination.read_bytes() == target
        assert destination.stat().st_mode & 0o777 == 0o600
        verify_rendered_phase6_web_edge_transition(BINDING, **options)
        with pytest.raises(Phase6WebEdgeTransitionError):
            write_phase6_web_edge_transition(BINDING, **options)

        destination.write_bytes(_canonical({"sealed": False}))
        with pytest.raises(Phase6WebEdgeTransitionError):
            verify_rendered_phase6_web_edge_transition(BINDING, **options)
