from __future__ import annotations

import json
import sys
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from copy import deepcopy
from hashlib import sha256
from pathlib import Path
from typing import cast
from unittest.mock import Mock, patch

import pytest

import tools.render_phase6_core_runtime_transition as transition
from tools.render_phase6_core_runtime_transition import (
    ACTIVE_DRAFT_ONLY_TEMPLATE_OUTPUT,
    CAPACITY_RELEASED_TEMPLATE_OUTPUT,
    Phase6CoreRuntimeTransitionError,
    Phase6CoreRuntimeTransitionTarget,
    render_phase6_core_runtime_transition,
    verify_rendered_phase6_core_runtime_transition,
    write_phase6_core_runtime_transition,
)
from tools.render_phase6_core_sam_staging import Phase6CoreSamStagingBinding

FUNCTIONS = frozenset(
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
MAINTENANCE_FUNCTIONS = frozenset(
    {
        "SourceVersionRetentionFunction",
        "StuckExecutionRecoveryFunction",
        "TerminalOperationalCleanupFunction",
    }
)
TRIGGER_LOCATIONS = (
    ("DispatcherFunction", "DueWorkSweep", "Schedule"),
    ("DispatcherFunction", "OperationalStateChanges", "DynamoDB"),
    ("SourceVersionRetentionFunction", "SourceVersionRetentionSweep", "Schedule"),
    (
        "TerminalOperationalCleanupFunction",
        "TerminalOperationalCleanupSweep",
        "Schedule",
    ),
)
RULE = "StuckExecutionRecoveryScheduleRule"
STAGED_DESCRIPTION = (
    "The exact sealed backend release is staged fail-closed; runtime and web traffic "
    "activation remain separate reviewed gates."
)
BINDING = cast(Phase6CoreSamStagingBinding, object())


def _canonical(document: object) -> bytes:
    return (
        json.dumps(document, allow_nan=False, indent=2, separators=(",", ": "), sort_keys=True)
        + "\n"
    ).encode()


def _disabled_triggers() -> dict[str, dict[str, object]]:
    return {
        **{
            f"{logical_id}.Events.{event_name}": {
                "Enabled": False,
                "Type": event_type,
            }
            for logical_id, event_name, event_type in TRIGGER_LOCATIONS
        },
        RULE: {"State": "DISABLED", "Type": "AWS::Events::Rule"},
    }


def _staged_document() -> dict[str, object]:
    code = {
        "Bucket": "mr-lister-phase6-artifacts-dev-123456789012-us-west-2",
        "Key": "private/deployments/lambda/releases/release/phase6-lambda.zip",
        "Version": "ExactVersion-1",
    }
    resources: dict[str, object] = {}
    for logical_id in sorted(FUNCTIONS):
        properties: dict[str, object] = {
            "Architectures": ["arm64"],
            "CodeUri": deepcopy(code),
            "Handler": f"phase6_lambda.{logical_id}",
            "Role": {"Fn::GetAtt": [f"{logical_id}Role", "Arn"]},
            "Runtime": "python3.12",
        }
        if logical_id in {"DispatcherFunction", "SettlementFunction"}:
            properties["Timeout"] = 120
        if logical_id in MAINTENANCE_FUNCTIONS:
            properties["ReservedConcurrentExecutions"] = 0
        resources[logical_id] = {
            "Properties": properties,
            "Type": "AWS::Serverless::Function",
        }

    function_events = {
        "DispatcherFunction": {
            "DueWorkSweep": {
                "Properties": {"Enabled": False, "Schedule": "rate(1 minute)"},
                "Type": "Schedule",
            },
            "OperationalStateChanges": {
                "Properties": {"Enabled": False, "StartingPosition": "LATEST"},
                "Type": "DynamoDB",
            },
        },
        "SourceVersionRetentionFunction": {
            "SourceVersionRetentionSweep": {
                "Properties": {"Enabled": False, "Schedule": "rate(15 minutes)"},
                "Type": "Schedule",
            }
        },
        "TerminalOperationalCleanupFunction": {
            "TerminalOperationalCleanupSweep": {
                "Properties": {"Enabled": False, "Schedule": "rate(1 day)"},
                "Type": "Schedule",
            }
        },
    }
    for logical_id, events in function_events.items():
        resources[logical_id]["Properties"]["Events"] = events  # type: ignore[index]

    resources.update(
        {
            "DispatcherFunctionRole": {
                "Properties": {"RoleName": "exact-dispatcher-role"},
                "Type": "AWS::IAM::Role",
            },
            "OperationalStateTable": {
                "Properties": {"TableName": "mr-lister-phase6-dev"},
                "Type": "AWS::DynamoDB::Table",
            },
            "PrepareStateMachine": {
                "Properties": {
                    "Definition": {"StartAt": "Prepare", "States": {"Prepare": {"End": True}}},
                    "Role": {"Fn::GetAtt": ["PrepareStateMachineRole", "Arn"]},
                },
                "Type": "AWS::Serverless::StateMachine",
            },
            "PrivateArtifactBucket": {
                "Properties": {"BucketName": "exact-private-bucket"},
                "Type": "AWS::S3::Bucket",
            },
            RULE: {
                "Properties": {"ScheduleExpression": "rate(5 minutes)", "State": "DISABLED"},
                "Type": "AWS::Events::Rule",
            },
        }
    )
    return {
        "AWSTemplateFormatVersion": "2010-09-09",
        "Description": "Exact sealed core staging fixture",
        "Globals": {
            "Function": {
                "Environment": {
                    "Variables": {
                        "MR_LISTER_PHASE6_SCAFFOLD_ONLY": "true",
                        "MR_LISTER_RELEASE_FINGERPRINT": {"Ref": "ReleaseFingerprint"},
                    }
                }
            }
        },
        "Metadata": {
            "MrListerPhase6CoreRuntimeStaging": {
                "DisabledTriggers": _disabled_triggers(),
                "Format": "mr-lister-phase6-core-sam-staged-v1",
                "Foundation": {"StackId": "exact-stack-id"},
                "LambdaArtifact": deepcopy(code),
                "Mode": "STAGED_FAIL_CLOSED",
                "Readiness": "CORE_RELEASE_BOUND_STAGED",
                "SourceTemplate": {
                    "Path": "infra/phase6/template.json",
                    "Sha256": "a" * 64,
                },
                "StateMachineDefinitions": {
                    "PrepareStateMachine": {
                        "Path": "infra/phase6/statemachine/prepare.asl.json",
                        "Sha256": "b" * 64,
                    }
                },
            }
        },
        "Outputs": {
            "DeploymentReadiness": {
                "Description": STAGED_DESCRIPTION,
                "Value": "CORE_RELEASE_BOUND_STAGED",
            },
            "StateTableName": {"Value": {"Ref": "OperationalStateTable"}},
        },
        "Parameters": {"ReleaseFingerprint": {"AllowedValues": ["c" * 64], "Default": "c" * 64}},
        "Resources": resources,
        "Transform": "AWS::Serverless-2016-10-31",
    }


def _options(repository: Path) -> dict[str, object]:
    evidence = repository / "evidence"
    return {
        "foundation_binding_path": evidence / "foundation.json",
        "agentcore_endpoint_observation_path": evidence / "endpoint.json",
        "agentcore_object_evidence_path": evidence / "agentcore-object.json",
        "agentcore_runtime_v1_evidence_path": evidence / "runtime-v1.json",
        "lambda_object_evidence_path": evidence / "lambda-object.json",
        "repository_root": repository,
        "deployment_root": repository / "deployment",
        "artifact_root": repository / "artifacts",
    }


@contextmanager
def _sealed_staging(
    document: Mapping[str, object],
) -> Iterator[tuple[Mock, Mock, Mock]]:
    raw = _canonical(document)
    with (
        patch.object(
            transition.core_staging,
            "render_phase6_core_sam_staged_template",
            return_value=raw,
        ) as renderer,
        patch.object(
            transition.core_staging,
            "verify_core_runtime_dependency_closure",
        ) as closure,
        patch.object(
            transition.core_staging,
            "verify_phase6_core_sam_staged_inertness",
        ) as inertness,
    ):
        yield renderer, closure, inertness


def _document(raw: bytes) -> dict[str, object]:
    value = json.loads(raw)
    assert isinstance(value, dict)
    return value


def _concurrency(document: Mapping[str, object]) -> dict[str, object]:
    return {
        logical_id: resource["Properties"]["ReservedConcurrentExecutions"]
        for logical_id, resource in document["Resources"].items()  # type: ignore[union-attr]
        if resource["Type"] == "AWS::Serverless::Function"
        and "ReservedConcurrentExecutions" in resource["Properties"]
    }


def _assert_trigger_state(document: Mapping[str, object], *, enabled: bool) -> None:
    resources = document["Resources"]
    for logical_id, event_name, _event_type in TRIGGER_LOCATIONS:
        assert (
            resources[logical_id]["Properties"]["Events"][event_name]["Properties"][  # type: ignore[index]
                "Enabled"
            ]
            is enabled
        )
    assert resources[RULE]["Properties"]["State"] == (  # type: ignore[index]
        "ENABLED" if enabled else "DISABLED"
    )


def _assert_timeout_authority(document: Mapping[str, object]) -> None:
    resources = document["Resources"]
    assert resources["DispatcherFunction"]["Properties"]["Timeout"] == 120  # type: ignore[index]
    assert resources["SettlementFunction"]["Properties"]["Timeout"] == 120  # type: ignore[index]


def test_capacity_released_inert_is_an_exact_sealed_staging_derivative(
    tmp_path: Path,
) -> None:
    staged = _staged_document()
    options = _options(tmp_path)
    with _sealed_staging(staged) as (base_renderer, closure, inertness):
        raw = render_phase6_core_runtime_transition(
            BINDING,
            target=Phase6CoreRuntimeTransitionTarget.CAPACITY_RELEASED_INERT,
            **options,  # type: ignore[arg-type]
        )
    rendered = _document(raw)

    base_renderer.assert_called_once_with(
        BINDING,
        foundation_binding_path=options["foundation_binding_path"],
        agentcore_endpoint_observation_path=options["agentcore_endpoint_observation_path"],
        agentcore_object_evidence_path=options["agentcore_object_evidence_path"],
        agentcore_runtime_v1_evidence_path=options["agentcore_runtime_v1_evidence_path"],
        lambda_object_evidence_path=options["lambda_object_evidence_path"],
        repository_root=tmp_path.resolve(),
        deployment_root=options["deployment_root"],
        artifact_root=options["artifact_root"],
    )
    assert closure.call_count == 2
    inertness.assert_called_once_with(staged)
    assert _concurrency(rendered) == {}
    _assert_trigger_state(rendered, enabled=False)
    _assert_timeout_authority(rendered)
    assert (
        rendered["Globals"]["Function"]["Environment"]["Variables"][  # type: ignore[index]
            "MR_LISTER_PHASE6_SCAFFOLD_ONLY"
        ]
        == "true"
    )
    assert rendered["Outputs"]["DeploymentReadiness"]["Value"] == (  # type: ignore[index]
        "CORE_CAPACITY_RELEASED_INERT"
    )
    metadata = rendered["Metadata"]["MrListerPhase6CoreRuntimeStaging"]  # type: ignore[index]
    assert metadata["Mode"] == "CAPACITY_RELEASED_INERT"
    assert metadata["DisabledTriggers"] == _disabled_triggers()
    assert metadata["StagedTemplateSha256"] == sha256(_canonical(staged)).hexdigest()

    assert set(rendered["Resources"]) == set(staged["Resources"])  # type: ignore[arg-type]
    assert rendered["Parameters"] == staged["Parameters"]
    for logical_id in FUNCTIONS:
        assert (
            rendered["Resources"][logical_id]["Properties"]["CodeUri"]
            == (  # type: ignore[index]
                staged["Resources"][logical_id]["Properties"]["CodeUri"]  # type: ignore[index]
            )
        )
        assert (
            rendered["Resources"][logical_id]["Properties"]["Role"]
            == (  # type: ignore[index]
                staged["Resources"][logical_id]["Properties"]["Role"]  # type: ignore[index]
            )
        )
    for logical_id in (
        "DispatcherFunctionRole",
        "OperationalStateTable",
        "PrepareStateMachine",
        "PrivateArtifactBucket",
    ):
        assert rendered["Resources"][logical_id] == staged["Resources"][logical_id]  # type: ignore[index]


def test_backend_active_is_derived_through_capacity_and_only_enables_reviewed_core(
    tmp_path: Path,
) -> None:
    staged = _staged_document()
    with _sealed_staging(staged):
        first = render_phase6_core_runtime_transition(
            BINDING,
            target="backend-active-draft-only",
            **_options(tmp_path),  # type: ignore[arg-type]
        )
        second = render_phase6_core_runtime_transition(
            BINDING,
            target=Phase6CoreRuntimeTransitionTarget.BACKEND_ACTIVE_DRAFT_ONLY,
            **_options(tmp_path),  # type: ignore[arg-type]
        )
    assert first == second
    rendered = _document(first)

    assert _concurrency(rendered) == {}
    _assert_trigger_state(rendered, enabled=True)
    _assert_timeout_authority(rendered)
    assert (
        rendered["Globals"]["Function"]["Environment"]["Variables"][  # type: ignore[index]
            "MR_LISTER_PHASE6_SCAFFOLD_ONLY"
        ]
        == "false"
    )
    assert rendered["Outputs"]["DeploymentReadiness"]["Value"] == (  # type: ignore[index]
        "CORE_RUNTIME_ACTIVE_DRAFT_ONLY"
    )
    metadata = rendered["Metadata"]["MrListerPhase6CoreRuntimeStaging"]  # type: ignore[index]
    assert metadata["Mode"] == "ACTIVE_DRAFT_ONLY"
    assert metadata["ActiveTriggers"] == {
        key: {
            **value,
            **({"Enabled": True} if "Enabled" in value else {"State": "ENABLED"}),
        }
        for key, value in _disabled_triggers().items()
    }
    assert "DisabledTriggers" not in metadata
    assert all(
        not resource["Type"].startswith(
            (
                "AWS::ApiGateway::",
                "AWS::ApiGatewayV2::",
                "AWS::CloudFront::",
                "AWS::Cognito::",
            )
        )
        for resource in rendered["Resources"].values()  # type: ignore[union-attr]
    )
    assert "publish" not in json.dumps(rendered).casefold()


def test_strict_diff_rejects_any_unreviewed_resource_mutation(tmp_path: Path) -> None:
    staged = _staged_document()
    original = transition._render_capacity_released_inert

    def broadened(
        document: Mapping[str, object],
        *,
        staged_sha256: str,
    ) -> dict[str, object]:
        rendered = original(document, staged_sha256=staged_sha256)
        rendered["Resources"]["DispatcherFunctionRole"]["Properties"]["RoleName"] = (  # type: ignore[index]
            "broadened-role"
        )
        return rendered

    with (
        _sealed_staging(staged),
        patch.object(transition, "_render_capacity_released_inert", side_effect=broadened),
        pytest.raises(Phase6CoreRuntimeTransitionError) as captured,
    ):
        render_phase6_core_runtime_transition(
            BINDING,
            target="capacity-released-inert",
            **_options(tmp_path),  # type: ignore[arg-type]
        )
    assert str(captured.value) == "Phase 6 core-runtime transition configuration is invalid"


@pytest.mark.parametrize(
    ("mutation", "logical_id"),
    (
        ("missing-function", "SettlementFunction"),
        ("extra-concurrency", "DispatcherFunction"),
        ("active-trigger", "DispatcherFunction"),
        ("web-resource", "SellerHttpApi"),
    ),
)
def test_drifted_staged_authority_fails_closed(
    tmp_path: Path,
    mutation: str,
    logical_id: str,
) -> None:
    staged = _staged_document()
    if mutation == "missing-function":
        staged["Resources"].pop(logical_id)  # type: ignore[union-attr]
    elif mutation == "extra-concurrency":
        staged["Resources"][logical_id]["Properties"]["ReservedConcurrentExecutions"] = 0  # type: ignore[index]
    elif mutation == "active-trigger":
        staged["Resources"][logical_id]["Properties"]["Events"]["DueWorkSweep"][  # type: ignore[index]
            "Properties"
        ]["Enabled"] = True
    else:
        staged["Resources"][logical_id] = {  # type: ignore[index]
            "Properties": {},
            "Type": "AWS::Serverless::HttpApi",
        }

    with _sealed_staging(staged), pytest.raises(Phase6CoreRuntimeTransitionError):
        render_phase6_core_runtime_transition(
            BINDING,
            target="capacity-released-inert",
            **_options(tmp_path),  # type: ignore[arg-type]
        )


@pytest.mark.parametrize(
    "target",
    [
        Phase6CoreRuntimeTransitionTarget.CAPACITY_RELEASED_INERT,
        Phase6CoreRuntimeTransitionTarget.BACKEND_ACTIVE_DRAFT_ONLY,
    ],
)
def test_fixed_private_outputs_are_create_only_and_byte_verified(
    tmp_path: Path,
    target: Phase6CoreRuntimeTransitionTarget,
) -> None:
    staged = _staged_document()
    options = _options(tmp_path)
    expected_relative = (
        CAPACITY_RELEASED_TEMPLATE_OUTPUT
        if target is Phase6CoreRuntimeTransitionTarget.CAPACITY_RELEASED_INERT
        else ACTIVE_DRAFT_ONLY_TEMPLATE_OUTPUT
    )
    with _sealed_staging(staged):
        destination = write_phase6_core_runtime_transition(
            BINDING,
            target=target,
            **options,  # type: ignore[arg-type]
        )
        assert destination == tmp_path / expected_relative
        assert destination.stat().st_mode & 0o777 == 0o600
        verify_rendered_phase6_core_runtime_transition(
            BINDING,
            target=target,
            **options,  # type: ignore[arg-type]
        )
        with pytest.raises(Phase6CoreRuntimeTransitionError):
            write_phase6_core_runtime_transition(
                BINDING,
                target=target,
                **options,  # type: ignore[arg-type]
            )
        destination.write_bytes(destination.read_bytes() + b" ")
        with pytest.raises(Phase6CoreRuntimeTransitionError):
            verify_rendered_phase6_core_runtime_transition(
                BINDING,
                target=target,
                **options,  # type: ignore[arg-type]
            )


@pytest.mark.parametrize("target", ["", "unknown", " capacity-released-inert", None, True])
def test_unknown_or_inexact_programmatic_target_fails_closed(
    tmp_path: Path,
    target: object,
) -> None:
    with pytest.raises(Phase6CoreRuntimeTransitionError):
        render_phase6_core_runtime_transition(
            BINDING,
            target=target,  # type: ignore[arg-type]
            **_options(tmp_path),  # type: ignore[arg-type]
        )


def test_noncanonical_or_private_base_failure_is_sanitized(tmp_path: Path) -> None:
    with (
        patch.object(
            transition.core_staging,
            "render_phase6_core_sam_staged_template",
            side_effect=RuntimeError("private authority path and identifier"),
        ),
        pytest.raises(Phase6CoreRuntimeTransitionError) as captured,
    ):
        render_phase6_core_runtime_transition(
            BINDING,
            target="capacity-released-inert",
            **_options(tmp_path),  # type: ignore[arg-type]
        )
    assert str(captured.value) == "Phase 6 core-runtime transition configuration is invalid"
    assert "private authority" not in str(captured.value)

    noncanonical = _canonical(_staged_document()) + b" "
    with (
        patch.object(
            transition.core_staging,
            "render_phase6_core_sam_staged_template",
            return_value=noncanonical,
        ),
        pytest.raises(Phase6CoreRuntimeTransitionError),
    ):
        render_phase6_core_runtime_transition(
            BINDING,
            target="capacity-released-inert",
            **_options(tmp_path),  # type: ignore[arg-type]
        )


def _cli_arguments(*actions: str) -> list[str]:
    return [
        "render_phase6_core_runtime_transition.py",
        "--target",
        "capacity-released-inert",
        "--account-id",
        "123456789012",
        "--region",
        "us-west-2",
        "--environment",
        "dev",
        "--foundation-stack-id",
        "stack-id",
        "--foundation-binding",
        "foundation.json",
        "--release-fingerprint",
        "a" * 64,
        "--agentcore-runtime-arn",
        "runtime-arn",
        "--agentcore-runtime-endpoint-arn",
        "endpoint-arn",
        "--agentcore-runtime-version",
        "1",
        "--agentcore-runtime-qualifier",
        "phase6_v1_dev",
        "--agentcore-runtime-binding-fingerprint",
        "b" * 64,
        "--agentcore-endpoint-observation",
        "endpoint.json",
        "--agentcore-object-evidence",
        "agentcore-object.json",
        "--agentcore-runtime-v1-evidence",
        "runtime-v1.json",
        "--printify-secret-arn",
        "secret-arn",
        "--application-origin",
        "https://example.com",
        "--lambda-artifact-bucket",
        "bucket",
        "--lambda-artifact-key",
        "key",
        "--lambda-artifact-version",
        "version",
        "--lambda-object-evidence",
        "lambda-object.json",
        *actions,
    ]


def test_cli_routes_exact_target_to_exclusive_write_action(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    destination = tmp_path / CAPACITY_RELEASED_TEMPLATE_OUTPUT
    with (
        patch.object(sys, "argv", _cli_arguments("--write")),
        patch.object(transition, "_binding_from_arguments", return_value=BINDING),
        patch.object(
            transition,
            "write_phase6_core_runtime_transition",
            return_value=destination,
        ) as writer,
    ):
        transition.main()
    assert capsys.readouterr().out.strip() == str(destination)
    assert writer.call_args.kwargs["target"] == "capacity-released-inert"


def test_cli_refuses_missing_or_multiple_actions(capsys: pytest.CaptureFixture[str]) -> None:
    for actions in ((), ("--write", "--verify")):
        with (
            patch.object(sys, "argv", _cli_arguments(*actions)),
            pytest.raises(SystemExit) as exit_,
        ):
            transition.main()
        assert exit_.value.code == 2
    assert "not allowed with argument" in capsys.readouterr().err
