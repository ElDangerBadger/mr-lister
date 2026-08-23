from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from typing import Any

import pytest

from mr_lister.cloud import phase6_retention_entrypoint
from mr_lister.cloud.phase6_retention_composition import SOURCE_VERSION_RETENTION_EVENT
from mr_lister.production.retention_aws import RETENTION_CHECKPOINT_PARTITION_KEY

ROOT = Path(__file__).parents[1]
TEMPLATE = ROOT / "infra" / "phase6" / "template.json"
SHIM = ROOT / "infra" / "phase6" / "lambda" / "phase6_lambda.py"


def _template() -> dict:
    return json.loads(TEMPLATE.read_text(encoding="utf-8"))


def _statements() -> dict[str, dict]:
    role = _template()["Resources"]["SourceVersionRetentionFunctionRole"]
    statements = role["Properties"]["Policies"][0]["PolicyDocument"]["Statement"]
    return {statement["Sid"]: statement for statement in statements}


def _actions(statements: dict[str, dict]) -> set[str]:
    actions: set[str] = set()
    for statement in statements.values():
        raw = statement["Action"]
        actions.update(raw if isinstance(raw, list) else [raw])
    return actions


def _shim(name: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, SHIM)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_retention_function_has_one_bounded_exact_input_schedule_and_stays_scaffolded() -> None:
    template = _template()
    resources = template["Resources"]
    function = resources["SourceVersionRetentionFunction"]
    properties = function["Properties"]

    assert function["Type"] == "AWS::Serverless::Function"
    assert function["DependsOn"] == "SourceVersionRetentionLogGroup"
    assert properties["Handler"] == "phase6_lambda.source_version_retention_handler"
    assert properties["Role"] == {"Fn::GetAtt": ["SourceVersionRetentionFunctionRole", "Arn"]}
    assert properties["ReservedConcurrentExecutions"] == 1
    assert properties["Timeout"] == 300
    assert properties["Environment"]["Variables"] == {
        "MR_LISTER_ENVIRONMENT": {"Ref": "EnvironmentName"},
        "MR_LISTER_AWS_ACCOUNT_ID": {"Ref": "AWS::AccountId"},
    }

    assert set(properties["Events"]) == {"SourceVersionRetentionSweep"}
    event = properties["Events"]["SourceVersionRetentionSweep"]
    assert event["Type"] == "Schedule"
    schedule = event["Properties"]
    assert schedule["Name"] == {"Fn::Sub": "mr-lister-phase6-${EnvironmentName}-source-retention"}
    assert schedule["Schedule"] == "rate(15 minutes)"
    assert schedule["Enabled"] is True
    assert json.loads(schedule["Input"]) == SOURCE_VERSION_RETENTION_EVENT
    assert schedule["RetryPolicy"] == {
        "MaximumEventAgeInSeconds": 3600,
        "MaximumRetryAttempts": 2,
    }

    assert (
        template["Globals"]["Function"]["Environment"]["Variables"][
            "MR_LISTER_PHASE6_SCAFFOLD_ONLY"
        ]
        == "true"
    )
    assert template["Outputs"]["DeploymentReadiness"]["Value"] == "SCAFFOLD_ONLY"


def test_retention_shim_is_inert_until_exact_false_then_delegates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _shim("phase6_retention_infrastructure_shim")
    event = dict(SOURCE_VERSION_RETENTION_EVENT)
    monkeypatch.setenv("MR_LISTER_PHASE6_SCAFFOLD_ONLY", "true")
    with pytest.raises(module.Phase6ScaffoldNotReady):
        module.source_version_retention_handler(event, None)

    calls: list[dict[str, Any]] = []
    monkeypatch.setenv("MR_LISTER_PHASE6_SCAFFOLD_ONLY", "false")
    monkeypatch.setattr(module, "_require_release_authority", lambda: None)
    monkeypatch.setattr(
        phase6_retention_entrypoint,
        "source_version_retention_handler",
        lambda received, _context=None: calls.append(received) or {"delegated": True},
    )

    assert module.source_version_retention_handler(event, None) == {"delegated": True}
    assert calls == [event]


def test_retention_role_has_only_closed_inventory_tag_authority_and_checkpoint_actions() -> None:
    statements = _statements()

    assert set(statements) == {
        "WriteSourceRetentionLogs",
        "ListExactPrivateSourcePrefixVersions",
        "ReconcileExactPrivateSourceVersionTags",
        "StrongReadJobSourceAuthority",
        "ReadSourceRetentionCheckpoint",
        "WriteSourceRetentionCheckpoint",
    }
    assert _actions(statements) == {
        "logs:CreateLogStream",
        "logs:PutLogEvents",
        "s3:ListBucketVersions",
        "s3:GetObjectVersionTagging",
        "s3:PutObjectVersionTagging",
        "dynamodb:TransactGetItems",
        "dynamodb:GetItem",
        "dynamodb:PutItem",
    }

    serialized = json.dumps(statements, sort_keys=True).casefold()
    for forbidden in (
        "deleteobject",
        "deleteitem",
        'getobjectversion"',
        "getsecretvalue",
        "secretsmanager",
        "bedrock",
        "states:",
        "execute-api",
        "transactwriteitems",
        "updateitem",
        "query",
        "scan",
    ):
        assert forbidden not in serialized


def test_retention_role_enforces_exact_prefix_object_shape_and_checkpoint_partition() -> None:
    statements = _statements()
    inventory = statements["ListExactPrivateSourcePrefixVersions"]
    assert inventory["Resource"] == {"Fn::GetAtt": ["PrivateArtifactBucket", "Arn"]}
    assert inventory["Condition"] == {
        "StringEquals": {"s3:prefix": "private/owners/"},
        "NumericLessThanEquals": {"s3:max-keys": "1000"},
    }

    tags = statements["ReconcileExactPrivateSourceVersionTags"]
    assert tags["Resource"] == {
        "Fn::Sub": "${PrivateArtifactBucket.Arn}/private/owners/*/jobs/*/source/source.png"
    }

    authority = statements["StrongReadJobSourceAuthority"]
    assert authority["Condition"] == {
        "ForAllValues:StringLike": {"dynamodb:LeadingKeys": ["JOB#*"]},
        "Null": {"dynamodb:LeadingKeys": "false"},
    }
    for sid in ("ReadSourceRetentionCheckpoint", "WriteSourceRetentionCheckpoint"):
        assert statements[sid]["Condition"] == {
            "ForAllValues:StringEquals": {
                "dynamodb:LeadingKeys": [RETENTION_CHECKPOINT_PARTITION_KEY]
            },
            "Null": {"dynamodb:LeadingKeys": "false"},
        }


def test_source_delete_marker_lifecycle_rule_is_preserved_and_sweeper_cannot_delete() -> None:
    template = _template()
    rules = template["Resources"]["PrivateArtifactBucket"]["Properties"]["LifecycleConfiguration"][
        "Rules"
    ]
    delete_marker = next(
        rule for rule in rules if rule["Id"] == "RemoveExpiredPrivateSourceDeleteMarkers"
    )

    assert delete_marker == {
        "Id": "RemoveExpiredPrivateSourceDeleteMarkers",
        "Status": "Enabled",
        "Prefix": "private/owners/",
        "ExpiredObjectDeleteMarker": True,
    }
    assert not any(action.startswith("s3:Delete") for action in _actions(_statements()))
