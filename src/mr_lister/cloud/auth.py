"""Fail-closed Cognito claim translation for the Phase 6 seller API.

API Gateway remains responsible for JWT signature, expiry, issuer, audience, and route-scope
verification.  This module is the application-side defense in depth: it accepts only the exact
verified-authorizer event shape, rechecks the immutable seller claims, and derives ownership before
an application service can be invoked.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from hashlib import sha256
from hmac import compare_digest
from typing import Any

_CLAIM_TEXT = re.compile(r"^[\x20-\x7e]{1,512}$")
_SUBJECT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
_OAUTH_TOKEN = re.compile(r"^[\x21-\x24\x26-\x5b\x5d-\x7e]{1,256}$")
_GROUP = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:@-]{0,127}$")
_OWNER_ID = re.compile(r"^[a-f0-9]{64}$")


class AuthenticationRequiredError(Exception):
    """The request did not carry the verified API Gateway JWT context."""

    code = "AUTHENTICATION_REQUIRED"


class AccessDeniedError(Exception):
    """The verified token is not authorized for the seller application."""

    code = "FORBIDDEN"


@dataclass(frozen=True, slots=True)
class SellerClaimsPolicy:
    """Exact immutable claims expected from the configured Cognito application."""

    issuer: str
    client_id: str
    required_scope: str
    required_group: str = "seller"

    def __post_init__(self) -> None:
        if not _is_claim_text(self.issuer) or not self.issuer.startswith("https://"):
            raise ValueError("Cognito issuer configuration is invalid")
        if not _OAUTH_TOKEN.fullmatch(self.client_id):
            raise ValueError("Cognito client configuration is invalid")
        if not _OAUTH_TOKEN.fullmatch(self.required_scope):
            raise ValueError("Cognito scope configuration is invalid")
        if not _GROUP.fullmatch(self.required_group):
            raise ValueError("Cognito group configuration is invalid")


@dataclass(frozen=True, slots=True)
class AuthenticatedSeller:
    """The only identity material allowed to cross into seller application services."""

    owner_id: str
    log_owner_digest: str
    scopes: frozenset[str]

    def __post_init__(self) -> None:
        if not _OWNER_ID.fullmatch(self.owner_id):
            raise ValueError("Derived owner identity is invalid")
        if not re.fullmatch(r"[a-f0-9]{16}", self.log_owner_digest):
            raise ValueError("Logging owner digest is invalid")
        if not self.scopes or any(_OAUTH_TOKEN.fullmatch(scope) is None for scope in self.scopes):
            raise ValueError("Authorized scope set is invalid")


def authenticate_seller(
    event: Mapping[str, Any], *, policy: SellerClaimsPolicy
) -> AuthenticatedSeller:
    """Translate one API Gateway HTTP API JWT context into an opaque owner identity.

    Missing authorizer material is an authentication failure.  Once API Gateway reports a JWT,
    every claim mismatch is an authorization failure.  No raw claim is retained in the result.
    """

    claims = _verified_claims(event)
    issuer = _claim(claims, "iss")
    subject = _claim(claims, "sub")
    token_use = _claim(claims, "token_use")
    client_id = _claim(claims, "client_id")
    scope_text = _claim(claims, "scope")

    if not compare_digest(issuer, policy.issuer):
        raise AccessDeniedError
    if token_use != "access" or not compare_digest(client_id, policy.client_id):
        raise AccessDeniedError
    if _SUBJECT.fullmatch(subject) is None:
        raise AccessDeniedError

    scopes = _parse_scopes(scope_text)
    if policy.required_scope not in scopes:
        raise AccessDeniedError
    if policy.required_group not in _parse_groups(claims.get("cognito:groups")):
        raise AccessDeniedError

    owner_id = sha256(policy.issuer.encode() + b"\0" + subject.encode()).hexdigest()
    log_owner_digest = sha256(b"mr-lister-log\0" + owner_id.encode()).hexdigest()[:16]
    return AuthenticatedSeller(
        owner_id=owner_id,
        log_owner_digest=log_owner_digest,
        scopes=frozenset(scopes),
    )


def authenticate_and_invoke[T](
    event: Mapping[str, Any],
    *,
    policy: SellerClaimsPolicy,
    operation: Callable[[AuthenticatedSeller], T],
) -> T:
    """Make the identity-before-service ordering explicit at every Lambda adapter seam."""

    seller = authenticate_seller(event, policy=policy)
    return operation(seller)


def _verified_claims(event: Mapping[str, Any]) -> Mapping[str, Any]:
    if not isinstance(event, Mapping):
        raise AuthenticationRequiredError
    request_context = event.get("requestContext")
    if not isinstance(request_context, Mapping):
        raise AuthenticationRequiredError
    authorizer = request_context.get("authorizer")
    if not isinstance(authorizer, Mapping):
        raise AuthenticationRequiredError
    jwt = authorizer.get("jwt")
    if not isinstance(jwt, Mapping):
        raise AuthenticationRequiredError
    claims = jwt.get("claims")
    if not isinstance(claims, Mapping):
        raise AuthenticationRequiredError
    return claims


def _claim(claims: Mapping[str, Any], name: str) -> str:
    value = claims.get(name)
    if not _is_claim_text(value):
        raise AccessDeniedError
    return value


def _is_claim_text(value: object) -> bool:
    return isinstance(value, str) and value.isascii() and _CLAIM_TEXT.fullmatch(value) is not None


def _parse_scopes(value: str) -> frozenset[str]:
    # OAuth access-token scopes are separated by exactly one ASCII space.  Empty entries, duplicate
    # entries, and alternate whitespace fail closed instead of being normalized.
    parts = value.split(" ")
    if (
        not parts
        or any(_OAUTH_TOKEN.fullmatch(part) is None for part in parts)
        or len(set(parts)) != len(parts)
    ):
        raise AccessDeniedError
    return frozenset(parts)


def _parse_groups(value: object) -> frozenset[str]:
    groups: Sequence[object]
    if isinstance(value, str):
        if not _is_claim_text(value):
            raise AccessDeniedError
        if _GROUP.fullmatch(value) is not None:
            groups = (value,)
        else:
            try:
                decoded = json.loads(value)
            except (TypeError, ValueError):
                groups = _parse_bracketed_groups(value)
            else:
                if not isinstance(decoded, list):
                    raise AccessDeniedError
                groups = decoded
    elif isinstance(value, (list, tuple)):
        groups = value
    else:
        raise AccessDeniedError
    if not groups or len(groups) > 20 or any(not isinstance(group, str) for group in groups):
        raise AccessDeniedError
    normalized = tuple(groups)
    if any(_GROUP.fullmatch(group) is None for group in normalized):
        raise AccessDeniedError
    if len(set(normalized)) != len(normalized):
        raise AccessDeniedError
    return frozenset(normalized)


def _parse_bracketed_groups(value: str) -> tuple[str, ...]:
    """Parse API Gateway's bounded ``[seller auditor]`` array-claim rendering."""

    if not value.startswith("[") or not value.endswith("]"):
        raise AccessDeniedError
    inner = value[1:-1]
    if inner.startswith(" ") and inner.endswith(" "):
        inner = inner[1:-1]
    elif inner.startswith(" ") or inner.endswith(" "):
        raise AccessDeniedError
    if not inner or "  " in inner:
        raise AccessDeniedError
    groups = tuple(inner.split(" "))
    if any(not group for group in groups):
        raise AccessDeniedError
    return groups


__all__ = [
    "AccessDeniedError",
    "AuthenticatedSeller",
    "AuthenticationRequiredError",
    "SellerClaimsPolicy",
    "authenticate_and_invoke",
    "authenticate_seller",
]
