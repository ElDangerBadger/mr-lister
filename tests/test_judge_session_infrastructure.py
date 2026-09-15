"""The judge link and deletion worker cannot widen the owner's application authority."""

import json
from copy import deepcopy
from pathlib import Path

import pytest

from tools.prepare_judge_session_infrastructure import (
    COOKIE_NAME,
    build_template,
    patch_application_template,
)


def test_services_start_closed_and_only_schedule_exact_cleanup():
    template = build_template()
    parameters, resources = template["Parameters"], template["Resources"]
    assert parameters["SessionEnabled"]["Default"] == "false"
    assert parameters["CleanupDryRun"]["Default"] == "true"
    assert parameters["CleanupScheduleEnabled"]["Default"] == "false"
    assert parameters["CleanupActivationFingerprint"]["Default"] == ""
    schedule = resources["CleanupSchedule"]["Properties"]
    assert schedule["ScheduleExpression"] == "rate(1 minute)"
    assert schedule["State"] == {"Fn::If": ["RunCleanup", "ENABLED", "DISABLED"]}
    assert schedule["Targets"][0]["Input"] == "{}"
    assert resources["CleanupFunction"]["Properties"]["ReservedConcurrentExecutions"] == 1
    assert "SecretString" not in resources["SessionSeedSecret"]["Properties"]
    assert "GenerateSecretString" not in resources["SessionSeedSecret"]["Properties"]


def test_auth_state_expires_but_cleanup_evidence_is_retained():
    resources = build_template()["Resources"]
    for name in ("SessionTable", "CleanupTable", "SessionSeedSecret"):
        assert resources[name]["DeletionPolicy"] == "Retain"
        assert resources[name]["UpdateReplacePolicy"] == "Retain"
    session = resources["SessionTable"]["Properties"]
    assert session["KeySchema"] == [{"AttributeName": "PK", "KeyType": "HASH"}]
    assert session["TimeToLiveSpecification"] == {"AttributeName": "expires_at", "Enabled": True}
    cleanup = resources["CleanupTable"]["Properties"]
    assert "TimeToLiveSpecification" not in cleanup
    assert cleanup["GlobalSecondaryIndexes"][0]["IndexName"] == "DueIndex"


def test_broker_and_cleanup_roles_have_separate_narrow_authority():
    resources = build_template()["Resources"]
    for name in ("Session", "Cleanup"):
        policy = resources[name + "Role"]["Properties"]["Policies"][0]["PolicyDocument"]
        statements = policy["Statement"]
        assert all(statement["Resource"] != "*" for statement in statements)
        actions = {action for statement in statements for action in statement["Action"]}
        assert not actions & {
            "dynamodb:DeleteItem",
            "dynamodb:Scan",
            "secretsmanager:PutSecretValue",
        }
        assert not any(action.startswith(("cognito", "s3:", "iam:")) for action in actions)
        secret = next(s for s in statements if "secretsmanager:GetSecretValue" in s["Action"])
        assert secret["Resource"] == {
            "Ref": "SessionSeedSecret" if name == "Session" else "PrintifySecretArn"
        }
        writes = [s for s in statements if "dynamodb:PutItem" in s["Action"]]
        assert len(writes) == 1
        assert writes[0]["Resource"] == {"Fn::GetAtt": [name + "Table", "Arn"]}


def test_broker_api_is_throttled_post_only_and_has_no_body_or_cookie_logs():
    resources = build_template()["Resources"]
    routes = [
        r["Properties"] for r in resources.values() if r["Type"] == "AWS::ApiGatewayV2::Route"
    ]
    assert {r["RouteKey"] for r in routes} == {
        "POST /v1/judge-session/redeem",
        "POST /v1/judge-session/refresh",
        "POST /v1/judge-session/logout",
    }
    stage = resources["SessionApiStage"]["Properties"]
    assert stage["DefaultRouteSettings"]["ThrottlingBurstLimit"] == 5
    assert stage["DefaultRouteSettings"]["ThrottlingRateLimit"] == 2
    log = json.loads(stage["AccessLogSettings"]["Format"])
    assert set(log) == {"requestId", "routeKey", "status"}
    assert "CorsConfiguration" not in resources["SessionApi"]["Properties"]


def baseline():
    return json.loads((Path(__file__).parents[1] / "infra/phase6/template.json").read_text())


def test_edge_patch_preserves_all_existing_authority_and_cookies_stay_separate():
    before = baseline()
    after = patch_application_template(
        before, session_api_hostname="abcdefghij.execute-api.us-west-2.amazonaws.com"
    )
    added = {"JudgeSessionNoStoreCachePolicy", "JudgeSessionOriginRequestPolicy"}
    assert set(after["Resources"]) - set(before["Resources"]) == added
    for name, resource in before["Resources"].items():
        if name != "SellerWebDistribution":
            assert after["Resources"][name] == resource
    restored = deepcopy(after)
    for name in added:
        del restored["Resources"][name]
    restored["Resources"]["SellerWebDistribution"] = before["Resources"]["SellerWebDistribution"]
    assert restored == before
    config = after["Resources"]["SellerWebDistribution"]["Properties"]["DistributionConfig"]
    behaviors = config["CacheBehaviors"]
    new_index = next(
        i for i, b in enumerate(behaviors) if b["PathPattern"] == "/v1/judge-session/*"
    )
    assert behaviors[new_index + 1]["PathPattern"] == "/v1/*"
    assert "FunctionAssociations" not in behaviors[new_index]
    assert config["Origins"][-1]["Id"] == "JudgeSessionApi"
    cache = after["Resources"]["JudgeSessionNoStoreCachePolicy"]["Properties"]["CachePolicyConfig"]
    assert all(cache[key] == 0 for key in ("MinTTL", "MaxTTL", "DefaultTTL"))
    forward = after["Resources"]["JudgeSessionOriginRequestPolicy"]["Properties"][
        "OriginRequestPolicyConfig"
    ]
    assert forward["CookiesConfig"] == {"CookieBehavior": "whitelist", "Cookies": [COOKIE_NAME]}
    assert forward["HeadersConfig"]["Headers"] == ["Content-Type", "Origin"]
    assert forward["QueryStringsConfig"] == {"QueryStringBehavior": "none"}


@pytest.mark.parametrize(
    "host",
    [
        "evil.example",
        "abcdefghij.execute-api.us-east-1.amazonaws.com",
        "https://abcdefghij.execute-api.us-west-2.amazonaws.com",
        "abcdefghij.execute-api.us-west-2.amazonaws.com/path",
        "abcdefghij.execute-api.us-west-2.amazonaws.com@evil.test",
    ],
)
def test_edge_rejects_unbound_origin(host):
    with pytest.raises(ValueError):
        patch_application_template(baseline(), session_api_hostname=host)


def test_edge_refuses_repeated_patch_or_relaxed_owner_mfa():
    before = baseline()
    host = "abcdefghij.execute-api.us-west-2.amazonaws.com"
    after = patch_application_template(before, session_api_hostname=host)
    with pytest.raises(ValueError):
        patch_application_template(after, session_api_hostname=host)
    before["Resources"]["SellerUserPool"]["Properties"]["MfaConfiguration"] = "OFF"
    with pytest.raises(ValueError):
        patch_application_template(before, session_api_hostname=host)
