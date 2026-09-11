"""Boundary tests for nonsecret evaluator planning; no live clients or credentials."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from tools.prepare_evaluator_deployment import EvaluatorPlanError, build_plan, main, read_json

ROOT = Path(__file__).resolve().parents[1]


def identifiers(environment: str, *, owner: str, shop: int) -> dict[str, object]:
    return {
        "environment_name": environment,
        "account_id": "111122223333",
        "region": "us-west-2",
        "application_origin": f"https://{environment}.example.com",
        "owner_id": owner * 64,
        "printify_shop_id": shop,
        "printify_secret_arn": (
            f"arn:aws:secretsmanager:us-west-2:111122223333:"
            f"secret:mr-lister/{environment}/printify/primary-AbCdEf"
        ),
    }


@pytest.fixture
def pair() -> tuple[dict[str, object], dict[str, object]]:
    return identifiers("production", owner="a", shop=100), identifiers(
        "judges", owner="b", shop=200
    )


def test_complete_identifier_plan_still_does_not_claim_deployment_or_verification(pair):
    plan = build_plan(*pair)
    assert plan["deployment_ready"] is False
    assert plan["cloud_operations_performed"] is False
    assert plan["publication_enabled"] is False
    assert plan["missing_runtime_bindings"]
    assert all("/publish" not in route for route in plan["phase6_route_inventory"])


def test_unassigned_judge_credentials_and_owner_remain_explicitly_pending(pair):
    baseline, target = pair
    for name in ("owner_id", "printify_shop_id", "printify_secret_arn"):
        target[name] = None
    plan = build_plan(baseline, target)
    assert plan["unassigned_identifiers"] == ["owner_id", "printify_secret_arn", "printify_shop_id"]
    assert plan["deployment_ready"] is False


@pytest.mark.parametrize(
    "field",
    [
        "environment_name",
        "application_origin",
        "owner_id",
        "printify_shop_id",
        "printify_secret_arn",
    ],
)
def test_production_reuse_is_refused(pair, field):
    baseline, target = pair
    target[field] = baseline[field]
    with pytest.raises(EvaluatorPlanError):
        build_plan(baseline, target)


@pytest.mark.parametrize(
    "field,value",
    [
        ("api_token", "NEVER_ECHO_CREDENTIAL"),
        ("publication_enabled", True),
        ("mode", "simulated"),
        ("state_table", "arbitrary-table"),
    ],
)
def test_unknown_fields_cannot_smuggle_credentials_or_override_authority(pair, field, value):
    baseline, target = pair
    target[field] = value
    with pytest.raises(EvaluatorPlanError) as caught:
        build_plan(baseline, target)
    assert "NEVER_ECHO_CREDENTIAL" not in str(caught.value)


@pytest.mark.parametrize(
    "field,value",
    [
        ("printify_shop_id", True),
        ("owner_id", "0" * 64),
        ("application_origin", "https://judges.example.com/path"),
        ("account_id", "999988887777"),
    ],
)
def test_malformed_or_cross_account_binding_is_refused(pair, field, value):
    baseline, target = pair
    target[field] = value
    with pytest.raises(EvaluatorPlanError):
        build_plan(baseline, target)


def test_missing_production_owner_cannot_disable_collision_check(pair):
    baseline, target = pair
    baseline["owner_id"] = None
    with pytest.raises(EvaluatorPlanError):
        build_plan(baseline, target)


@pytest.mark.parametrize("change", ["publish", "remove_auth", "direct_route", "embedded_path"])
def test_changed_template_route_authority_requires_review(pair, tmp_path, change):
    template = read_json(ROOT / "infra/phase6/template.json")
    events = template["Resources"]["SellerCommandApiFunction"]["Properties"]["Events"]
    event = copy.deepcopy(next(iter(events.values())))
    if change == "publish":
        event["Properties"]["Path"] = "/v1/jobs/{job_id}/publish"
        event["Properties"]["Method"] = "POST"
        events["UnexpectedPublish"] = event
    elif change == "remove_auth":
        next(iter(events.values()))["Properties"]["Auth"] = {"Authorizer": "NONE"}
    elif change == "direct_route":
        template["Resources"]["BypassRoute"] = {
            "Type": "AWS::ApiGatewayV2::Route",
            "Properties": {"RouteKey": "POST /v1/jobs/{job_id}/publish"},
        }
    else:
        api = template["Resources"]["SellerHttpApi"]["Properties"]
        api["DefinitionBody"]["paths"] = {"/v1/jobs/{job_id}/publish": {"post": {}}}
    path = tmp_path / "infra/phase6/template.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(template))
    with pytest.raises(EvaluatorPlanError):
        build_plan(*pair, repository=tmp_path)


def test_duplicate_fields_rejected_without_echoing_input(tmp_path):
    source = tmp_path / "input.json"
    source.write_text('{"api_token":"NEVER_ECHO_CREDENTIAL","api_token":"second"}')
    with pytest.raises(EvaluatorPlanError) as caught:
        read_json(source)
    assert "NEVER_ECHO_CREDENTIAL" not in str(caught.value)


def test_cli_writes_private_create_only_plan(pair, tmp_path):
    baseline, target = pair
    production = tmp_path / "production.json"
    evaluator = tmp_path / "evaluator.json"
    output = tmp_path / "plan.json"
    production.write_text(json.dumps(baseline))
    evaluator.write_text(json.dumps(target))
    args = [
        "--production-identifiers",
        str(production),
        "--evaluator-identifiers",
        str(evaluator),
        "--output",
        str(output),
        "--repository",
        str(ROOT),
    ]
    assert main(args) == 0
    before = output.read_bytes()
    assert output.stat().st_mode & 0o777 == 0o600
    with pytest.raises(SystemExit) as caught:
        main(args)
    assert caught.value.code == 2
    assert output.read_bytes() == before
