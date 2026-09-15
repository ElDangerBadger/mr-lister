from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import asdict
from hashlib import sha256
from unittest.mock import Mock
from urllib.parse import parse_qs

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.hashes import SHA256
from jwt.algorithms import RSAAlgorithm

from mr_lister.judge_session.models import PREFIX, SCOPE, Config, Unavailable
from mr_lister.judge_session.tokens import BoundedTransport, VerifiedOAuthIssuer, _NoRedirect

NOW = 1_800_000_000


@pytest.fixture(scope="module")
def key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


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


@pytest.fixture
def oauth(config, key):
    claims = {
        "iss": config.issuer,
        "sub": config.subject,
        "client_id": config.client_id,
        "token_use": "access",
        "scope": "openid " + SCOPE,
        "cognito:groups": ["seller", config.judge_group],
        "iat": NOW,
        "exp": NOW + 3600,
    }
    seed = {
        "contract_version": "judge-session-seed-v1",
        "issuer": config.issuer,
        "client_id": config.client_id,
        "subject": config.subject,
        "owner_id": config.owner_id,
        "campaign_id": config.campaign_id,
        "expires_at": NOW + 86400,
        "refresh_token": "private-refresh-fixture",
    }
    jwk = json.loads(RSAAlgorithm.to_jwk(key.public_key()))
    jwk.pop("key_ops", None)
    jwk.update(alg="RS256", use="sig", kid="public-key")
    secret = Mock()
    secret.get_secret_value.side_effect = lambda **kwargs: {
        "ARN": config.seed_secret_arn,
        "VersionStages": ["AWSCURRENT"],
        "SecretString": json.dumps(seed),
    }
    payload = {
        "access_token": jwt.encode(claims, key, algorithm="RS256", headers={"kid": "public-key"}),
        "expires_in": 3600,
        "token_type": "Bearer",
        "id_token": "unused-id-token",
    }
    jwks = {"keys": [jwk]}
    transport = Mock()

    def request(url, *, method, body=None):
        if url == config.token_endpoint and method == "POST":
            return json.dumps(payload).encode()
        if url == config.jwks_uri and method == "GET" and body is None:
            return json.dumps(jwks).encode()
        raise AssertionError("Unexpected network destination")

    transport.request.side_effect = request
    clock = Mock(return_value=NOW)
    issuer = VerifiedOAuthIssuer(config, secrets=secret, transport=transport, clock=clock)
    return issuer, seed, payload, claims, jwks, transport, secret, clock


def sign(payload, claims, key, *, headers=None):
    payload["access_token"] = jwt.encode(
        claims, key, algorithm="RS256", headers=headers or {"kid": "public-key"}
    )


def assert_unavailable(issuer):
    with pytest.raises(Unavailable) as failure:
        issuer.issue()
    assert str(failure.value) == ""
    assert failure.value.__context__ is None


def test_verified_primary_refresh_uses_fixed_destinations_and_fresh_secret(config, oauth):
    issuer, _, payload, _, _, transport, secret, _ = oauth
    issued = issuer.issue()
    assert issued.response(NOW) == {
        "access_token": payload["access_token"],
        "expires_in": 3600,
        "token_type": "Bearer",
    }
    assert "private-refresh" not in repr(issued)
    first, second = transport.request.call_args_list
    assert first.args == (config.token_endpoint,)
    assert first.kwargs["method"] == "POST"
    assert parse_qs(first.kwargs["body"].decode()) == {
        "grant_type": ["refresh_token"],
        "client_id": [config.client_id],
        "refresh_token": ["private-refresh-fixture"],
    }
    assert second.args == (config.jwks_uri,) and second.kwargs == {"method": "GET"}
    assert issuer.issue() == issued
    assert secret.get_secret_value.call_count == 2
    secret.get_secret_value.assert_called_with(
        SecretId=config.seed_secret_arn, VersionStage="AWSCURRENT"
    )
    assert transport.request.call_count == 3  # Only public JWKS is cached.


@pytest.mark.parametrize(
    "change",
    [
        {"issuer": "https://attacker.invalid"},
        {"subject": "another"},
        {"client_id": "another"},
        {"owner_id": "a" * 64},
        {"campaign_id": "another"},
        {"expires_at": NOW},
        {"expires_at": NOW + 86399},
        {"expires_at": True},
        {"refresh_token": ""},
        {"refresh_token": "a" * 8193},
        {"refresh_token": "private\nrefresh"},
        {"refresh_token": "nonasciié"},
        {"client_secret": "forbidden"},
        {"contract_version": "wrong"},
    ],
)
def test_rejects_misbound_or_unsafe_seed_before_network(oauth, change):
    issuer, seed, _, _, _, transport, _, _ = oauth
    seed.update(change)
    assert_unavailable(issuer)
    transport.request.assert_not_called()


@pytest.mark.parametrize(
    "change",
    [
        {"VersionStages": ["AWSCURRENT", "extra"]},
        {"VersionStages": ["AWSPREVIOUS"]},
        {"ARN": "arn:wrong"},
        {"SecretString": {}},
        {"SecretString": "{}"},
    ],
)
def test_rejects_secret_reference_or_stage_mismatch(oauth, change):
    issuer, _, _, _, _, transport, secret, _ = oauth
    record = secret.get_secret_value()
    record.update(change)
    secret.get_secret_value.side_effect = None
    secret.get_secret_value.return_value = record
    assert_unavailable(issuer)
    transport.request.assert_not_called()


@pytest.mark.parametrize(
    "change",
    [
        {"iss": "https://attacker.invalid"},
        {"sub": "somebody-else"},
        {"client_id": "another"},
        {"token_use": "id"},
        {"scope": "openid"},
        {"scope": SCOPE},
        {"scope": "openid " + SCOPE + " aws.cognito.signin.user.admin"},
        {"scope": "openid openid " + SCOPE},
        {"scope": "openid\t" + SCOPE},
        {"cognito:groups": ["seller"]},
        {"cognito:groups": ["seller", "attacker"]},
        {"cognito:groups": "seller"},
        {"cognito:groups": ["seller", "seller"]},
        {"iat": NOW + 31},
        {"exp": NOW},
        {"exp": NOW + 3601},
        {"exp": True},
        {"iat": NOW - 1},
        {"nbf": NOW + 1},
        {"nbf": True},
    ],
)
def test_signed_token_must_match_exact_primary_judge_authority(oauth, key, change):
    issuer, _, payload, claims, _, _, _, _ = oauth
    claims.update(change)
    sign(payload, claims, key)
    assert_unavailable(issuer)


def test_scope_order_is_not_authority(oauth, key):
    issuer, _, payload, claims, _, _, _, _ = oauth
    claims["scope"] = SCOPE + " openid"
    claims["cognito:groups"].reverse()
    sign(payload, claims, key)
    assert issuer.issue().expires_at == NOW + 3600


@pytest.mark.parametrize(
    "field", ["iss", "sub", "exp", "iat", "client_id", "token_use", "scope", "cognito:groups"]
)
def test_required_claims_cannot_be_omitted(oauth, key, field):
    issuer, _, payload, claims, _, _, _, _ = oauth
    del claims[field]
    sign(payload, claims, key)
    assert_unavailable(issuer)


@pytest.mark.parametrize(
    "change",
    [
        {"refresh_token": "rotation-not-supported"},
        {"expires_in": 3601},
        {"expires_in": True},
        {"expires_in": 0},
        {"access_token": ""},
        {"access_token": "x" * 16385},
        {"token_type": "bearer"},
        {"client_secret": "not-permitted"},
    ],
)
def test_token_endpoint_response_is_bounded_and_never_rotates_seed(oauth, change):
    issuer, _, payload, _, _, _, _, _ = oauth
    payload.update(change)
    assert_unavailable(issuer)


@pytest.mark.parametrize(
    "header",
    [
        {"kid": "public-key", "jku": "https://attacker.invalid/jwks"},
        {"kid": "public-key", "typ": "OTHER"},
        {"kid": "https://attacker.invalid/key"},
        {"kid": "unknown"},
        {"kid": "x" * 257},
    ],
)
def test_untrusted_token_cannot_choose_key_url_or_algorithm(oauth, key, header):
    issuer, _, payload, claims, _, transport, _, _ = oauth
    sign(payload, claims, key, headers=header)
    assert_unavailable(issuer)
    assert all(
        call.args[0] in {issuer.config.token_endpoint, issuer.config.jwks_uri}
        for call in transport.request.call_args_list
    )


def test_rejects_wrong_signature_and_hmac_algorithm(oauth):
    issuer, _, payload, claims, _, _, _, _ = oauth
    other_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    sign(payload, claims, other_key)
    assert_unavailable(issuer)
    payload["access_token"] = jwt.encode(
        claims, "attacker" * 8, algorithm="HS256", headers={"kid": "public-key"}
    )
    assert_unavailable(issuer)


@pytest.mark.parametrize("duplicate", ["header", "claim"])
def test_rejects_duplicate_signed_json_fields(oauth, key, duplicate):
    issuer, _, payload, claims, _, _, _, _ = oauth
    header = '{"alg":"RS256","kid":"public-key","typ":"JWT"}'
    body = json.dumps(claims)
    if duplicate == "header":
        header = header[:-1] + ',"alg":"RS256"}'
    else:
        body = body[:-1] + ',"sub":' + json.dumps(claims["sub"]) + "}"
    encoded = (
        jwt.utils.base64url_encode(header.encode())
        + b"."
        + jwt.utils.base64url_encode(body.encode())
    )
    signature = key.sign(encoded, padding.PKCS1v15(), SHA256())
    payload["access_token"] = (encoded + b"." + jwt.utils.base64url_encode(signature)).decode()
    assert_unavailable(issuer)


@pytest.mark.parametrize(
    "change",
    [
        {"kty": "EC"},
        {"alg": "HS256"},
        {"use": "enc"},
        {"d": "private-key-material"},
        {"n": "invalid"},
        {"kid": None},
        {"e": None},
    ],
)
def test_jwks_is_public_rsa_signing_keys_only(oauth, change):
    issuer, _, _, _, jwks, _, _, _ = oauth
    jwks["keys"][0].update(change)
    assert_unavailable(issuer)


def test_rejects_duplicate_and_weak_jwks_keys(oauth):
    issuer, _, _, _, jwks, _, _, _ = oauth
    jwks["keys"].append(deepcopy(jwks["keys"][0]))
    assert_unavailable(issuer)
    weak = rsa.generate_private_key(public_exponent=65537, key_size=1024)
    jwk = json.loads(RSAAlgorithm.to_jwk(weak.public_key()))
    jwk.pop("key_ops", None)
    jwk.update(alg="RS256", use="sig", kid="public-key")
    jwks["keys"] = [jwk]
    assert_unavailable(issuer)


def test_refresh_failure_cannot_expose_provider_exception(oauth):
    issuer, _, _, _, _, transport, _, _ = oauth
    transport.request.side_effect = RuntimeError("private-refresh-fixture token response")
    assert_unavailable(issuer)


def test_campaign_checked_again_after_provider_and_token_lifetime_is_conservative(oauth):
    issuer, _, payload, _, _, _, _, clock = oauth
    payload["expires_in"] = 300
    assert issuer.issue().expires_at == NOW + 300
    clock.side_effect = [NOW, NOW, issuer.config.campaign_expires_at]
    assert_unavailable(issuer)


def test_bounded_transport_disables_proxy_and_redirect_and_limits_reads(monkeypatch):
    opener = Mock()
    reply = Mock(status=200)
    reply.read.return_value = b"{}"
    opener.open.return_value.__enter__ = Mock(return_value=reply)
    opener.open.return_value.__exit__ = Mock(return_value=False)
    build = Mock(return_value=opener)
    monkeypatch.setattr("mr_lister.judge_session.tokens.build_opener", build)
    assert (
        BoundedTransport().request("https://fixed.invalid/token", method="POST", body=b"form")
        == b"{}"
    )
    assert build.call_args.args[0].proxies == {}
    assert isinstance(build.call_args.args[1], _NoRedirect)
    request = opener.open.call_args.args[0]
    assert request.get_method() == "POST" and request.data == b"form"
    assert opener.open.call_args.kwargs == {"timeout": 5}
    reply.read.assert_called_once_with(65537)
    assert _NoRedirect().redirect_request(None, None, 302, "", {}, "https://other.invalid") is None
    reply.read.return_value = b"x" * 65537
    with pytest.raises(Unavailable) as failure:
        BoundedTransport().request("https://fixed.invalid/token", method="GET")
    assert failure.value.__context__ is None
    reply.status = 302
    with pytest.raises(Unavailable):
        BoundedTransport().request("https://fixed.invalid/token", method="GET")


def test_environment_contract_round_trips_with_fixed_default_duration(config):
    fields = asdict(config)
    expiry = fields.pop("campaign_expires_at")
    fields.pop("duration_seconds")
    env = {PREFIX + key.upper(): str(value) for key, value in fields.items()}
    env[PREFIX + "ENABLED"] = "true"
    env[PREFIX + "CAMPAIGN_EXPIRES_AT_EPOCH"] = str(expiry)
    assert Config.from_environment(env) == config
