"""Pure validation of owner-bound Printify secret envelopes, without clients or provider code."""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from pydantic import SecretStr

PRINTIFY_OWNER_SECRET_SCHEMA_VERSION = "phase6-printify-owner-v1"
PRINTIFY_DELEGATED_OWNER_SECRET_SCHEMA_VERSION = "phase6-printify-owner-v2"
MAX_PRINTIFY_DELEGATED_OWNER_GRANTS = 16
MAX_PRINTIFY_API_TOKEN_CHARS = 4_096

_MAX_SECRET_STRING_CHARS = 16_384
_SECRET_ARN = re.compile(
    r"^arn:(aws|aws-us-gov|aws-cn):secretsmanager:[a-z0-9-]+:[0-9]{12}:"
    r"secret:mr-lister/[A-Za-z0-9/_-]+-[A-Za-z0-9]{6}$"
)
_OWNER_ID = re.compile(r"^[a-f0-9]{64}$")
_API_TOKEN = re.compile(rf"^[\x21-\x7e]{{1,{MAX_PRINTIFY_API_TOKEN_CHARS}}}$")
_SECRET_FIELDS = frozenset({"schema_version", "owner_id", "shop_id", "api_token"})
_DELEGATED_SECRET_FIELDS = _SECRET_FIELDS | {"delegated_owner_grants"}
_GRANT_FIELDS = frozenset({"owner_id", "expires_at"})
_UTC_TIMESTAMP = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$")


@dataclass(frozen=True, slots=True)
class ValidatedPrintifySecret:
    """Validated request identity and connection values with a masked token."""

    owner_id: str
    shop_id: int
    api_token: SecretStr


def validate_printify_secret_arn(value: object) -> None:
    if not isinstance(value, str) or _SECRET_ARN.fullmatch(value) is None:
        raise ValueError("An exact Phase 6 Printify Secrets Manager ARN is required")


def valid_printify_owner_id(value: object) -> bool:
    return isinstance(value, str) and _OWNER_ID.fullmatch(value) is not None and value != "0" * 64


def parse_printify_owner_secret(
    response: object,
    *,
    secret_arn: str,
    owner_id: str,
    primary_only: bool,
    clock: Callable[[], datetime] | None = None,
) -> ValidatedPrintifySecret:
    """Validate one already-read envelope; callers sanitize errors at their boundary."""

    validate_printify_secret_arn(secret_arn)
    if not valid_printify_owner_id(owner_id) or type(primary_only) is not bool:
        raise ValueError("invalid requested owner policy")
    if not isinstance(response, Mapping) or "SecretBinary" in response:
        raise ValueError("invalid secret envelope")
    if "ARN" in response and response["ARN"] != secret_arn:
        raise ValueError("secret ARN drift")
    if "VersionStages" in response and response["VersionStages"] != ["AWSCURRENT"]:
        raise ValueError("secret version stage drift")
    secret_string = response.get("SecretString")
    if (
        not isinstance(secret_string, str)
        or not secret_string
        or len(secret_string) > _MAX_SECRET_STRING_CHARS
    ):
        raise ValueError("invalid SecretString")
    payload = json.loads(
        secret_string,
        object_pairs_hook=_unique_json_object,
        parse_constant=_reject_json_constant,
    )
    if not isinstance(payload, dict):
        raise ValueError("invalid owner secret schema")
    version = payload.get("schema_version")
    if version == PRINTIFY_OWNER_SECRET_SCHEMA_VERSION:
        expected_fields = _SECRET_FIELDS
    elif version == PRINTIFY_DELEGATED_OWNER_SECRET_SCHEMA_VERSION:
        expected_fields = _DELEGATED_SECRET_FIELDS
    else:
        raise ValueError("invalid owner secret schema version")
    if set(payload) != expected_fields:
        raise ValueError("invalid owner secret schema")
    secret_owner = payload["owner_id"]
    if not valid_printify_owner_id(secret_owner):
        raise ValueError("invalid primary owner")
    grants = (
        _delegated_grants(payload["delegated_owner_grants"], primary_owner=secret_owner)
        if version == PRINTIFY_DELEGATED_OWNER_SECRET_SCHEMA_VERSION
        else {}
    )
    if secret_owner != owner_id:
        if primary_only:
            raise ValueError("primary owner mismatch")
        expires_at = grants.get(owner_id)
        if expires_at is None:
            raise ValueError("owner mismatch")
        now = clock() if clock is not None else datetime.now(UTC)
        if (
            not isinstance(now, datetime)
            or now.tzinfo is None
            or now.utcoffset() is None
            or now >= expires_at
        ):
            raise ValueError("expired owner grant")
    shop_id = payload["shop_id"]
    if type(shop_id) is not int or shop_id <= 0:
        raise ValueError("invalid shop")
    api_token = payload["api_token"]
    if not isinstance(api_token, str) or _API_TOKEN.fullmatch(api_token) is None:
        raise ValueError("invalid token")
    return ValidatedPrintifySecret(
        owner_id=owner_id, shop_id=shop_id, api_token=SecretStr(api_token)
    )


def _delegated_grants(value: object, *, primary_owner: str) -> dict[str, datetime]:
    if not isinstance(value, list) or len(value) > MAX_PRINTIFY_DELEGATED_OWNER_GRANTS:
        raise ValueError("invalid delegated owner grants")
    grants: dict[str, datetime] = {}
    for grant in value:
        if not isinstance(grant, dict) or set(grant) != _GRANT_FIELDS:
            raise ValueError("invalid delegated owner grant")
        owner_id = grant["owner_id"]
        if not valid_printify_owner_id(owner_id) or owner_id == primary_owner or owner_id in grants:
            raise ValueError("invalid delegated owner")
        expires_at = grant["expires_at"]
        if not isinstance(expires_at, str) or _UTC_TIMESTAMP.fullmatch(expires_at) is None:
            raise ValueError("invalid delegated owner expiry")
        grants[owner_id] = datetime.strptime(expires_at, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    return grants


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> None:
    del value
    raise ValueError("nonstandard JSON constant")


__all__ = [
    "MAX_PRINTIFY_API_TOKEN_CHARS",
    "MAX_PRINTIFY_DELEGATED_OWNER_GRANTS",
    "PRINTIFY_DELEGATED_OWNER_SECRET_SCHEMA_VERSION",
    "PRINTIFY_OWNER_SECRET_SCHEMA_VERSION",
    "ValidatedPrintifySecret",
    "parse_printify_owner_secret",
    "valid_printify_owner_id",
    "validate_printify_secret_arn",
]
