from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import replace
from hashlib import sha256
from unittest.mock import Mock

import pytest
from botocore.exceptions import ClientError

from mr_lister.judge_session import entrypoint
from mr_lister.judge_session.http import JudgeSessionHandler, request_cookie
from mr_lister.judge_session.models import (
    COOKIE_NAME,
    Config,
    Denied,
    Invitation,
    Session,
    Unavailable,
    digest,
)
from mr_lister.judge_session.service import JudgeSessionService
from mr_lister.judge_session.store import DynamoSessionStore, decode, encode
from mr_lister.judge_session.tokens import AccessToken

NOW = 1_800_000_000
INVITATION = "i" * 43
COOKIE = "c" * 43


@pytest.fixture
def config():
    issuer = "https://cognito-idp.us-west-2.amazonaws.com/us-west-2_ExamplePool"
    subject = "00000000-0000-4000-8000-000000000001"
    return Config(
        table="judge-sessions-test",
        seed_secret_arn="arn:aws:secretsmanager:us-west-2:123456789012:secret:judge-seed-ABC123",
        issuer=issuer,
        client_id="primaryclient",
        subject=subject,
        owner_id=sha256((issuer + "\0" + subject).encode()).hexdigest(),
        cognito_origin="https://seller.auth.us-west-2.amazoncognito.com",
        application_origin="https://seller.example.com",
        campaign_id="judge-campaign",
        campaign_expires_at=NOW + 86400,
    )


def invitation_row(config, **changes):
    return {
        "PK": "INVITE#" + digest(INVITATION),
        "contract_version": "judge-session-invitation-v1",
        "campaign_id": config.campaign_id,
        "owner_id": config.owner_id,
        "expires_at": config.campaign_expires_at,
        "redemption_limit": 1,
        "redemptions": 0,
        "enabled": True,
        **changes,
    }


class MemoryStore:
    def __init__(self, config):
        self.config = config
        self.invites = {digest(INVITATION): invitation_row(config)}
        self.sessions = {}
        self.reject_reservation = False

    def invitation(self, key):
        return deepcopy(self.invites.get(key))

    def session(self, key):
        return deepcopy(self.sessions.get(key))

    def reserve(self, invitation, session, now):
        current = self.invites[invitation.invitation_digest]
        if (
            self.reject_reservation
            or current["redemptions"] >= current["redemption_limit"]
            or current["enabled"] is not True
            or current["expires_at"] <= now
            or session.session_digest in self.sessions
        ):
            raise Denied
        current["redemptions"] += 1
        self.sessions[session.session_digest] = session.row(self.config)

    def revoke(self, key):
        if key in self.sessions:
            self.sessions[key]["revoked"] = True


@pytest.fixture
def system(config):
    clock = Mock(return_value=NOW)
    store = MemoryStore(config)
    issuer = Mock()
    issuer.issue.return_value = AccessToken("verified-access-token", NOW + 3600)
    service = JudgeSessionService(
        config, store=store, issuer=issuer, clock=clock, random_token=lambda: COOKIE
    )
    return service, store, issuer, clock, JudgeSessionHandler(config, service)


def event(config, operation="redeem", *, body=None, cookie=None):
    path = "/v1/judge-session/" + operation
    result = {
        "version": "2.0",
        "rawPath": path,
        "rawQueryString": "",
        "routeKey": "POST " + path,
        "requestContext": {"http": {"method": "POST", "path": path}},
        "headers": {"origin": config.application_origin, "content-type": "application/json"},
        "isBase64Encoded": False,
        "body": json.dumps(
            body
            if body is not None
            else ({"invitation": INVITATION} if operation == "redeem" else {})
        ),
    }
    if cookie is not None:
        result["cookies"] = [f"{COOKIE_NAME}={cookie}"]
    return result


def test_redeem_returns_only_access_token_and_secure_cookie(config, system):
    _, store, _, _, handler = system
    result = handler(event(config))
    assert result["statusCode"] == 200
    assert json.loads(result["body"]) == {
        "access_token": "verified-access-token",
        "expires_in": 3600,
        "token_type": "Bearer",
    }
    assert result["headers"]["Cache-Control"] == "private, no-store, max-age=0"
    assert result["headers"]["Access-Control-Allow-Origin"] == config.application_origin
    assert result["cookies"] == [
        f"{COOKIE_NAME}={COOKIE}; Path=/v1/judge-session; Max-Age=14400; "
        "HttpOnly; Secure; SameSite=Strict"
    ]
    records = json.dumps([store.invites, store.sessions])
    assert INVITATION not in records and COOKIE not in records
    assert "verified-access-token" not in records
    assert store.invites[digest(INVITATION)]["redemptions"] == 1
    assert store.sessions[digest(COOKIE)]["expires_at"] == NOW + 14400


def test_limit_prevents_another_redemption_but_preserves_existing_session(config, system):
    _, _, issuer, _, handler = system
    assert handler(event(config))["statusCode"] == 200
    assert handler(event(config))["statusCode"] == 403
    assert issuer.issue.call_count == 1
    assert handler(event(config, "refresh", cookie=COOKIE))["statusCode"] == 200
    assert issuer.issue.call_count == 2


def test_conditional_reservation_failure_never_releases_token(config, system):
    _, store, _, _, handler = system
    store.reject_reservation = True
    result = handler(event(config))
    assert result["statusCode"] == 403
    assert "cookies" not in result and "verified-access-token" not in result["body"]
    assert store.sessions == {}


def test_issuer_failure_does_not_consume_invitation_or_leak_error(config, system):
    _, store, issuer, _, handler = system
    issuer.issue.side_effect = RuntimeError("private refresh-token details")
    result = handler(event(config))
    assert result["statusCode"] == 503
    assert "private refresh" not in json.dumps(result)
    assert store.invites[digest(INVITATION)]["redemptions"] == 0


@pytest.mark.parametrize(
    "change",
    [
        {"expires_at": NOW},
        {"enabled": False},
        {"redemptions": 1},
        {"owner_id": "a" * 64},
        {"campaign_id": "other"},
        {"redemption_limit": True},
        {"redemptions": -1},
        {"expires_at": NOW + 86401},
        {"contract_version": "wrong"},
        {"unexpected": "field"},
    ],
)
def test_invalid_or_unavailable_invitation_does_not_reach_oauth(config, system, change):
    _, store, issuer, _, handler = system
    store.invites[digest(INVITATION)].update(change)
    assert handler(event(config))["statusCode"] == 403
    issuer.issue.assert_not_called()


@pytest.mark.parametrize(
    "change",
    [
        {"revoked": True},
        {"expires_at": NOW},
        {"created_at": NOW + 1},
        {"expires_at": NOW + 14401},
        {"owner_id": "a" * 64},
        {"campaign_id": "other"},
    ],
)
def test_expired_revoked_or_misbound_session_cannot_refresh(config, system, change):
    _, store, issuer, _, handler = system
    assert handler(event(config))["statusCode"] == 200
    store.sessions[digest(COOKIE)].update(change)
    assert handler(event(config, "refresh", cookie=COOKIE))["statusCode"] == 403
    assert issuer.issue.call_count == 1


def test_refresh_rechecks_revocation_after_network_call(config, system):
    _, store, issuer, _, handler = system
    assert handler(event(config))["statusCode"] == 200

    def issue():
        store.revoke(digest(COOKIE))
        return AccessToken("late-token", NOW + 3600)

    issuer.issue.side_effect = issue
    result = handler(event(config, "refresh", cookie=COOKIE))
    assert result["statusCode"] == 403 and "late-token" not in result["body"]


def test_disabling_invitation_stops_existing_session_renewal(config, system):
    _, store, issuer, _, handler = system
    assert handler(event(config))["statusCode"] == 200
    store.invites[digest(INVITATION)]["enabled"] = False
    assert handler(event(config, "refresh", cookie=COOKIE))["statusCode"] == 403
    assert issuer.issue.call_count == 1


def test_session_cookie_expiry_is_fixed_and_bounded_by_invitation(config, system):
    _, store, _, clock, handler = system
    store.invites[digest(INVITATION)]["expires_at"] = NOW + 100
    issued = handler(event(config))
    assert "Max-Age=100" in issued["cookies"][0]
    assert json.loads(issued["body"])["expires_in"] == 100
    clock.return_value = NOW + 50
    refreshed = handler(event(config, "refresh", cookie=COOKIE))
    assert "Max-Age=50" in refreshed["cookies"][0]
    assert json.loads(refreshed["body"])["expires_in"] == 50
    clock.return_value = NOW + 100
    assert handler(event(config, "refresh", cookie=COOKIE))["statusCode"] == 403


def test_near_campaign_end_response_never_advertises_longer_than_session(config, system):
    _, _, issuer, clock, handler = system
    now = config.campaign_expires_at - 30
    clock.return_value = now
    issuer.issue.return_value = AccessToken("verified-access-token", now + 3600)
    result = handler(event(config))
    assert result["statusCode"] == 200
    assert json.loads(result["body"])["expires_in"] == 30
    assert "Max-Age=30" in result["cookies"][0]


def test_campaign_expiry_rejects_before_dependency_calls(config, system):
    _, _, issuer, clock, handler = system
    clock.return_value = config.campaign_expires_at
    assert handler(event(config))["statusCode"] == 403
    issuer.issue.assert_not_called()


def test_logout_is_idempotent_and_only_revokes_its_cookie_session(config, system):
    _, store, issuer, _, handler = system
    assert handler(event(config))["statusCode"] == 200
    store.sessions["other"] = {"revoked": False}
    for cookie in (COOKIE, COOKIE, None):
        result = handler(event(config, "logout", cookie=cookie))
        assert result["statusCode"] == 204 and result["body"] == ""
        assert "Max-Age=0" in result["cookies"][0]
    assert store.sessions[digest(COOKIE)]["revoked"] is True
    assert store.sessions["other"]["revoked"] is False
    assert issuer.issue.call_count == 1
    assert handler(event(config, "refresh", cookie=COOKIE))["statusCode"] == 403


@pytest.mark.parametrize("operation", ["redeem", "refresh", "logout"])
@pytest.mark.parametrize(
    "origin", [None, "null", "https://attacker.example", "https://seller.example.com/"]
)
def test_every_operation_requires_exact_origin(config, system, operation, origin):
    _, _, issuer, _, handler = system
    request = event(config, operation, cookie=COOKIE)
    if origin is None:
        del request["headers"]["origin"]
    else:
        request["headers"]["origin"] = origin
    result = handler(request)
    assert result["statusCode"] == 403
    assert "Access-Control-Allow-Origin" not in result["headers"]
    issuer.issue.assert_not_called()


@pytest.mark.parametrize(
    "change,status",
    [
        ({"version": "1.0"}, 400),
        ({"rawPath": "/v1/jobs"}, 404),
        ({"requestContext": {"http": {"method": "GET"}}}, 405),
        ({"routeKey": "POST /other"}, 400),
        ({"rawQueryString": "invitation=secret"}, 400),
        ({"isBase64Encoded": True}, 400),
        ({"body": "x" * 4097}, 400),
        ({"body": '{"invitation":"a","invitation":"b"}'}, 400),
        ({"body": "[]"}, 400),
        ({"body": '{"invitation":NaN}'}, 400),
        ({"body": False}, 400),
    ],
)
def test_closed_http_contract(config, system, change, status):
    _, _, issuer, _, handler = system
    assert handler({**event(config), **change})["statusCode"] == status
    issuer.issue.assert_not_called()


@pytest.mark.parametrize(
    "body",
    [
        {"invitation": "short"},
        {"invitation": "a" * 257},
        {"invitation": None},
        {"invitation": "a" * 42 + "="},
    ],
)
def test_untrusted_invitation_shape(config, system, body):
    _, _, issuer, _, handler = system
    assert handler(event(config, body=body))["statusCode"] == 403
    issuer.issue.assert_not_called()


def test_duplicate_origin_or_cookie_and_form_post_rejected(config, system):
    _, _, issuer, _, handler = system
    request = event(config)
    request["headers"]["Origin"] = config.application_origin
    assert handler(request)["statusCode"] == 400
    request = event(config)
    request["headers"]["content-type"] = "application/x-www-form-urlencoded"
    assert handler(request)["statusCode"] == 400
    request = event(config, "refresh", cookie=COOKIE)
    request["cookies"].append(f"{COOKIE_NAME}={COOKIE}")
    assert handler(request)["statusCode"] == 400
    issuer.issue.assert_not_called()
    with pytest.raises(ValueError):
        request_cookie({"cookies": [f"{COOKIE_NAME}={COOKIE}\r\nInjected=x"]}, {})


def test_dynamo_reservation_pins_invitation_and_atomically_creates_session(config):
    client = Mock()
    store = DynamoSessionStore(client, config)
    invitation = Invitation.read(invitation_row(config), config, digest(INVITATION), NOW)
    session = Session(digest(COOKIE), digest(INVITATION), NOW, NOW + 14400)
    store.reserve(invitation, session, NOW)
    actions = client.transact_write_items.call_args.kwargs["TransactItems"]
    assert len(actions) == 2
    update = actions[0]["Update"]
    assert update["TableName"] == config.table
    assert update["Key"] == {"PK": {"S": "INVITE#" + digest(INVITATION)}}
    condition = update["ConditionExpression"]
    for guard in (
        "campaign_id = :campaign",
        "owner_id = :owner",
        "enabled = :yes",
        "expires_at > :now",
        "redemptions < redemption_limit",
    ):
        assert guard in condition
    assert decode(update["ExpressionAttributeValues"])[":limit"] == 1
    assert actions[1]["Put"]["ConditionExpression"] == "attribute_not_exists(PK)"
    assert decode(actions[1]["Put"]["Item"]) == session.row(config)


def test_dynamo_conditional_and_transport_errors_are_sanitized(config):
    client = Mock()
    store = DynamoSessionStore(client, config)
    invitation = Invitation.read(invitation_row(config), config, digest(INVITATION), NOW)
    session = Session(digest(COOKIE), digest(INVITATION), NOW, NOW + 10)
    client.transact_write_items.side_effect = ClientError(
        {
            "Error": {"Code": "TransactionCanceledException", "Message": "sensitive detail"},
            "CancellationReasons": [{"Code": "ConditionalCheckFailed"}],
        },
        "TransactWriteItems",
    )
    with pytest.raises(Denied) as caught:
        store.reserve(invitation, session, NOW)
    assert caught.value.__context__ is None
    client.get_item.side_effect = RuntimeError("sensitive provider content")
    with pytest.raises(Unavailable) as caught:
        store.session(digest(COOKIE))
    assert caught.value.__context__ is None
    assert client.get_item.call_args.kwargs["ConsistentRead"] is True


def test_dynamo_logout_does_not_create_session(config):
    client = Mock()
    client.update_item.side_effect = ClientError(
        {
            "Error": {
                "Code": "ConditionalCheckFailedException",
                "Message": "missing",
            }
        },
        "UpdateItem",
    )
    DynamoSessionStore(client, config).revoke(digest(COOKIE))
    assert "attribute_exists(PK)" in client.update_item.call_args.kwargs["ConditionExpression"]


@pytest.mark.parametrize(
    "changes",
    [
        {"owner_id": "a" * 64},
        {"issuer": "https://attacker.example/pool"},
        {"cognito_origin": "https://attacker.example"},
        {"duration_seconds": 14401},
        {"duration_seconds": True},
        {"application_origin": "https://seller.example.com/path"},
        {"seed_secret_arn": "arn:aws:secretsmanager:us-east-1:123456789012:secret:judge-ABC123"},
    ],
)
def test_invalid_runtime_binding_rejected(config, changes):
    with pytest.raises(ValueError):
        replace(config, **changes)


def test_disabled_entrypoint_never_initializes_aws(monkeypatch):
    monkeypatch.delenv("MR_LISTER_JUDGE_SESSION_ENABLED", raising=False)
    build = Mock(side_effect=AssertionError("No AWS initialization allowed"))
    monkeypatch.setattr(entrypoint, "build_handler", build)
    assert entrypoint.lambda_handler({})["statusCode"] == 503
    build.assert_not_called()


def test_explicit_enable_with_missing_runtime_is_unavailable(monkeypatch):
    monkeypatch.setenv("MR_LISTER_JUDGE_SESSION_ENABLED", "true")
    monkeypatch.delenv("MR_LISTER_JUDGE_SESSION_TABLE", raising=False)
    assert entrypoint.lambda_handler({})["statusCode"] == 503


def test_durable_encoding_rejects_non_integer_numbers():
    assert decode(encode({"key": "value", "count": 1, "flag": True})) == {
        "key": "value",
        "count": 1,
        "flag": True,
    }
    with pytest.raises(ValueError):
        decode({"expires_at": {"N": "1.5"}})
