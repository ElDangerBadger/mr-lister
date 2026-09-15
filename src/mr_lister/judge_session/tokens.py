"""Refresh a pinned primary OAuth session and verify its access token before delivery."""

from __future__ import annotations

import json
import re
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener

import jwt
from jwt.algorithms import RSAAlgorithm

from .models import SCOPE, Config, Unavailable, exact_object, integer, strict_json


class Transport(Protocol):
    def request(self, url: str, *, method: str, body: bytes | None = None) -> bytes: ...


class SecretClient(Protocol):
    def get_secret_value(self, **kwargs: Any) -> Mapping[str, Any]: ...


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class BoundedTransport:
    """No redirects, proxies, unbounded bodies, cookies, or provider-response logging."""

    def request(self, url: str, *, method: str, body: bytes | None = None) -> bytes:
        from urllib.request import ProxyHandler

        headers = {"Accept": "application/json"}
        if method == "POST":
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        try:
            request = Request(url, data=body, headers=headers, method=method)
            with build_opener(ProxyHandler({}), _NoRedirect()).open(request, timeout=5) as response:
                if response.status != 200:
                    raise ValueError
                content = response.read(65537)
                if len(content) > 65536:
                    raise ValueError
                return content
        except Exception:
            pass
        raise Unavailable


@dataclass(frozen=True, repr=False)
class AccessToken:
    value: str
    expires_at: int

    def response(self, now: int) -> dict:
        remaining = self.expires_at - now
        if not 1 <= remaining <= 3600:
            raise Unavailable
        return {"access_token": self.value, "expires_in": remaining, "token_type": "Bearer"}


class TokenIssuer(Protocol):
    def issue(self) -> AccessToken: ...


class VerifiedOAuthIssuer:
    """Secrets are read afresh; only public issuer JWKS may be cached in process memory."""

    def __init__(
        self,
        config: Config,
        *,
        secrets: SecretClient,
        transport: Transport,
        clock: Callable[[], int],
    ) -> None:
        self.config = config
        self.secrets = secrets
        self.transport = transport
        self.clock = clock
        self._keys: dict[str, object] = {}
        self._keys_until = 0
        self._keys_lock = threading.Lock()

    def issue(self) -> AccessToken:
        try:
            return self._issue()
        except Exception:
            # Drop exception context so dependency messages cannot retain token-bearing bodies.
            pass
        raise Unavailable

    def _issue(self) -> AccessToken:
        now = self.clock()
        if now >= self.config.campaign_expires_at:
            raise ValueError
        response = self.secrets.get_secret_value(
            SecretId=self.config.seed_secret_arn, VersionStage="AWSCURRENT"
        )
        if (
            response.get("ARN") != self.config.seed_secret_arn
            or response.get("VersionStages") != ["AWSCURRENT"]
            or not isinstance(response.get("SecretString"), str)
        ):
            raise ValueError
        seed = exact_object(
            strict_json(response["SecretString"], limit=16384),
            {
                "contract_version",
                "issuer",
                "client_id",
                "subject",
                "owner_id",
                "campaign_id",
                "expires_at",
                "refresh_token",
            },
        )
        if seed["contract_version"] != "judge-session-seed-v1":
            raise ValueError
        for field in ("issuer", "client_id", "subject", "owner_id", "campaign_id"):
            if seed[field] != getattr(self.config, field):
                raise ValueError
        if integer(seed["expires_at"], self.config.campaign_expires_at) <= now:
            raise ValueError
        refresh = seed["refresh_token"]
        if (
            not isinstance(refresh, str)
            or not 1 <= len(refresh) <= 8192
            or not refresh.isascii()
            or any(ord(char) < 33 or ord(char) > 126 for char in refresh)
        ):
            raise ValueError
        body = urlencode(
            {
                "grant_type": "refresh_token",
                "client_id": self.config.client_id,
                "refresh_token": refresh,
            }
        ).encode("ascii")
        payload = strict_json(
            self.transport.request(self.config.token_endpoint, method="POST", body=body)
        )
        if not isinstance(payload, dict) or set(payload) - {
            "access_token",
            "id_token",
            "expires_in",
            "token_type",
            "scope",
        }:
            # A rotated refresh token requires an explicit rotation-capable seed writer.
            raise ValueError
        if payload.get("token_type") != "Bearer":
            raise ValueError
        lifetime = integer(payload.get("expires_in"), 1, 3600)
        token = payload.get("access_token")
        if not isinstance(token, str) or not 1 <= len(token) <= 16384:
            raise ValueError
        header = strict_json(jwt.utils.base64url_decode(token.split(".")[0]))
        if not isinstance(header, dict):
            raise ValueError
        if (
            set(header) - {"alg", "kid", "typ"}
            or header.get("alg") != "RS256"
            or header.get("typ", "JWT") != "JWT"
            or not isinstance(header.get("kid"), str)
            or re.fullmatch(r"[A-Za-z0-9_+/=-]{1,256}", header["kid"]) is None
        ):
            raise ValueError
        key = self._key(header["kid"])
        claims = jwt.decode(
            token,
            key=key,
            algorithms=["RS256"],
            issuer=self.config.issuer,
            options={
                "verify_aud": False,
                "verify_exp": False,
                "verify_iat": False,
                "verify_nbf": False,
                "require": [
                    "iss",
                    "sub",
                    "exp",
                    "iat",
                    "client_id",
                    "token_use",
                    "scope",
                    "cognito:groups",
                ],
            },
        )
        # Decode with the closed JSON parser too: duplicate signed fields remain ambiguous.
        strict_json(jwt.utils.base64url_decode(token.split(".")[1]))
        checked_now = self.clock()
        expiry = integer(claims["exp"], checked_now + 1, checked_now + 3630)
        issued = integer(claims["iat"], 1, checked_now + 30)
        if (
            expiry <= issued
            or expiry - issued > 3600
            or claims["sub"] != self.config.subject
            or claims["client_id"] != self.config.client_id
            or claims["token_use"] != "access"
        ):
            raise ValueError
        if "nbf" in claims and integer(claims["nbf"]) > checked_now:
            raise ValueError
        groups = claims["cognito:groups"]
        if (
            not isinstance(groups, list)
            or len(groups) != 2
            or not all(isinstance(group, str) for group in groups)
            or set(groups) != {"seller", self.config.judge_group}
        ):
            raise ValueError
        scopes = claims["scope"]
        if not isinstance(scopes, str) or sorted(scopes.split(" ")) != sorted(["openid", SCOPE]):
            raise ValueError
        if checked_now >= self.config.campaign_expires_at:
            raise ValueError
        return AccessToken(token, min(expiry, now + lifetime))

    def _key(self, kid: str) -> object:
        with self._keys_lock:
            now = self.clock()
            if now < self._keys_until and kid in self._keys:
                return self._keys[kid]
            # Each issuer verification performs at most one bounded, fixed-origin JWKS read.
            value = exact_object(
                strict_json(self.transport.request(self.config.jwks_uri, method="GET")), {"keys"}
            )
            rows = value["keys"]
            if not isinstance(rows, list) or not 1 <= len(rows) <= 10:
                raise ValueError
            keys = {}
            for row in rows:
                if (
                    not isinstance(row, dict)
                    or row.get("kty") != "RSA"
                    or row.get("alg") != "RS256"
                    or row.get("use") != "sig"
                    or set(row) - {"kty", "alg", "use", "kid", "n", "e"}
                    or not isinstance(row.get("kid"), str)
                    or row["kid"] in keys
                ):
                    raise ValueError
                key = RSAAlgorithm.from_jwk(json.dumps(row))
                if not 2048 <= key.key_size <= 8192:
                    raise ValueError
                keys[row["kid"]] = key
            self._keys = keys
            self._keys_until = now + 900
            return keys[kid]
