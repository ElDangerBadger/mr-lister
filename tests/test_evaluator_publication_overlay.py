"""The isolated evaluator overlay has exactly one additional authenticated GET."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest
from test_evaluator_deployment_plan import identifiers

from tools.prepare_evaluator_deployment import EvaluatorPlanError
from tools.render_evaluator_publication_status import (
    EVENT_NAME,
    POLICY_SETTING,
    render_evaluator_status_template,
    verify_evaluator_status_template,
)

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def pair():
    return identifiers("production", owner="a", shop=100), identifiers(
        "judges", owner="b", shop=200
    )


def test_overlay_is_pinned_read_only_authenticated_and_still_scaffold_only(pair):
    source = json.loads((ROOT / "infra/phase6/template.json").read_text())
    overlay = render_evaluator_status_template(*pair)
    verify_evaluator_status_template(overlay, *pair)
    assert overlay["Globals"] == source["Globals"]
    assert overlay["Metadata"]["MrListerEvaluatorPublication"]["DeploymentReady"] is False
    target = pair[1]
    for parameter, field in {
        "EnvironmentName": "environment_name",
        "ApplicationOrigin": "application_origin",
        "PrintifySecretArn": "printify_secret_arn",
    }.items():
        assert overlay["Parameters"][parameter]["AllowedValues"] == [target[field]]
        assert overlay["Parameters"][parameter]["Default"] == target[field]
    functions = overlay["Resources"]
    query = functions["ReviewQueryApiFunction"]["Properties"]
    assert query["Environment"]["Variables"][POLICY_SETTING] == "read_only"
    event = query["Events"][EVENT_NAME]["Properties"]
    assert event["Method"] == "GET"
    assert event["Path"] == "/v1/jobs/{job_id}/publication"
    assert event["Auth"] == {
        "Authorizer": "SellerJwtAuthorizer",
        "AuthorizationScopes": ["mr-lister-api/seller"],
    }
    for name, resource in functions.items():
        if name != "ReviewQueryApiFunction":
            assert resource == source["Resources"][name]
        for candidate in resource.get("Properties", {}).get("Events", {}).values():
            assert not candidate.get("Properties", {}).get("Path", "").endswith("/publish")
    # Remove the two local function additions: IAM, handlers and code must be identical.
    restored = copy.deepcopy(query)
    del restored["Events"][EVENT_NAME]
    del restored["Environment"]["Variables"][POLICY_SETTING]
    assert restored == source["Resources"]["ReviewQueryApiFunction"]["Properties"]
    assert source["Parameters"]["EnvironmentName"]["Default"] == "dev"


@pytest.mark.parametrize(
    "mutation",
    [
        "publish",
        "unauthenticated",
        "policy",
        "owner_store",
        "function_url",
        "iam",
        "unpin",
        "activation",
        "ready",
        "true_as_one",
    ],
)
def test_derivation_checker_refuses_any_extra_ingress_or_authority(pair, mutation):
    overlay = render_evaluator_status_template(*pair)
    query = overlay["Resources"]["ReviewQueryApiFunction"]["Properties"]
    if mutation == "publish":
        query["Events"][EVENT_NAME]["Properties"].update(
            {"Method": "POST", "Path": "/v1/jobs/{job_id}/publish"}
        )
    elif mutation == "unauthenticated":
        query["Events"][EVENT_NAME]["Properties"]["Auth"] = {"Authorizer": "NONE"}
    elif mutation == "policy":
        query["Environment"]["Variables"][POLICY_SETTING] = "disabled"
    elif mutation == "owner_store":
        query["Environment"]["Variables"]["MR_LISTER_STATE_TABLE"] = "mr-lister-phase6-production"
    elif mutation == "function_url":
        query["FunctionUrlConfig"] = {"AuthType": "NONE"}
    elif mutation == "iam":
        query["Policies"] = ["AdministratorAccess"]
    elif mutation == "unpin":
        del overlay["Parameters"]["EnvironmentName"]["AllowedValues"]
    elif mutation == "activation":
        overlay["Globals"]["Function"]["Environment"]["Variables"][
            "MR_LISTER_PHASE6_SCAFFOLD_ONLY"
        ] = "false"
    elif mutation == "true_as_one":
        overlay["Metadata"]["MrListerEvaluatorPublication"]["PublicationEnabled"] = 0
    else:
        overlay["Metadata"]["MrListerEvaluatorPublication"]["DeploymentReady"] = True
    with pytest.raises(EvaluatorPlanError):
        verify_evaluator_status_template(overlay, *pair)


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
def test_overlay_never_accepts_reused_production_identifiers(pair, field):
    production, evaluator = pair
    evaluator[field] = production[field]
    with pytest.raises(EvaluatorPlanError):
        render_evaluator_status_template(production, evaluator)


@pytest.mark.parametrize("field", ["owner_id", "printify_shop_id", "printify_secret_arn"])
def test_partial_plans_cannot_render_an_overlay(pair, field):
    pair[1][field] = None
    with pytest.raises(EvaluatorPlanError):
        render_evaluator_status_template(*pair)


def test_unreviewed_source_template_cannot_become_an_overlay(pair, tmp_path):
    source = ROOT / "infra/phase6/template.json"
    target = tmp_path / "infra/phase6/template.json"
    target.parent.mkdir(parents=True)
    target.write_text(source.read_text() + "\n")
    with pytest.raises(EvaluatorPlanError, match="scaffold changed"):
        render_evaluator_status_template(*pair, repository=tmp_path)
