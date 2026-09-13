"""Judge routes preserve the production authentication and API boundaries."""

import json
from copy import deepcopy
from pathlib import Path

import pytest

from tools.prepare_judge_application_update import prepare_application_template

ROOT = Path(__file__).resolve().parents[1]


def baseline():
    return json.loads((ROOT / "infra/phase6/template.json").read_text())


def test_judge_patch_changes_only_client_routes_spa_and_no_store_behavior():
    before = baseline()
    after = prepare_application_template(before, origin="https://massskutiny.com")
    touched = {"SellerUserPoolClient", "SellerSpaRouteFunction", "SellerWebDistribution"}
    for name, resource in before["Resources"].items():
        if name not in touched:
            assert after["Resources"][name] == resource
    restored = deepcopy(after)
    for name in touched:
        restored["Resources"][name] = before["Resources"][name]
    assert restored == before
    client = after["Resources"]["SellerUserPoolClient"]["Properties"]
    assert client["SupportedIdentityProviders"] == ["COGNITO", "MrListerJudge"]
    assert (
        client["CallbackURLs"][:-1]
        == before["Resources"]["SellerUserPoolClient"]["Properties"]["CallbackURLs"]
    )
    assert client["CallbackURLs"][-1] == "https://massskutiny.com/judge/auth/callback"
    assert after["Resources"]["SellerUserPool"]["Properties"]["MfaConfiguration"] == "ON"
    behaviors = after["Resources"]["SellerWebDistribution"]["Properties"]["DistributionConfig"][
        "CacheBehaviors"
    ]
    normal = next(item for item in behaviors if item["PathPattern"] == "/runtime-config.json")
    judge = next(item for item in behaviors if item["PathPattern"] == "/judge/runtime-config.json")
    assert judge == {**normal, "PathPattern": "/judge/runtime-config.json"}


@pytest.mark.parametrize(
    "origin",
    [
        "http://massskutiny.com",
        "https://evil@massskutiny.com",
        "https://massskutiny.com/",
        "https://massskutiny.com?redirect=evil",
        "https://misbound.invalid",
    ],
)
def test_rejects_noncanonical_origins(origin):
    with pytest.raises(ValueError):
        prepare_application_template(baseline(), origin=origin)


def test_refuses_mfa_drift_and_repeat_patching():
    before = baseline()
    before["Resources"]["SellerUserPool"]["Properties"]["MfaConfiguration"] = "OPTIONAL"
    with pytest.raises(ValueError):
        prepare_application_template(before, origin="https://massskutiny.com")
    after = prepare_application_template(baseline(), origin="https://massskutiny.com")
    with pytest.raises(ValueError):
        prepare_application_template(after, origin="https://massskutiny.com")


def test_requires_existing_mapping_permissions_without_expanding_them():
    before = baseline()
    client = before["Resources"]["SellerUserPoolClient"]["Properties"]
    client["WriteAttributes"] = ["email"]
    with pytest.raises(ValueError):
        prepare_application_template(before, origin="https://massskutiny.com")
    client["WriteAttributes"] = ["email", "email_verified", "name"]
    after = prepare_application_template(before, origin="https://massskutiny.com")
    assert (
        after["Resources"]["SellerUserPoolClient"]["Properties"]["WriteAttributes"]
        == client["WriteAttributes"]
    )
