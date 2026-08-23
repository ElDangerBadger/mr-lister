"""Rotation-safe, owner-bound Printify credentials from AWS Secrets Manager."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Any, Protocol

from mr_lister.production.printify import PrintifyAuthenticationError
from mr_lister.production.provider_resources import OwnerPrintifyConnection

PRINTIFY_OWNER_SECRET_SCHEMA_VERSION = "phase6-printify-owner-v1"
MAX_PRINTIFY_API_TOKEN_CHARS = 4_096

_MAX_SECRET_STRING_CHARS = 16_384
_UNAVAILABLE = "Owner-bound Printify credential is unavailable"
_SECRET_ARN = re.compile(
    r"^arn:(aws|aws-us-gov|aws-cn):secretsmanager:[a-z0-9-]+:[0-9]{12}:"
    r"secret:mr-lister/[A-Za-z0-9/_-]+-[A-Za-z0-9]{6}$"
)
_OWNER_ID = re.compile(r"^[a-f0-9]{64}$")
_API_TOKEN = re.compile(rf"^[\x21-\x7e]{{1,{MAX_PRINTIFY_API_TOKEN_CHARS}}}$")
_SECRET_FIELDS = frozenset({"schema_version", "owner_id", "shop_id", "api_token"})


class SecretsManagerGetSecretValueClient(Protocol):
    def get_secret_value(self, **kwargs: Any) -> Mapping[str, Any]: ...


class SecretsManagerOwnerPrintifyConnectionResolver:
    """Resolve and validate one exact owner secret afresh on every call."""

    __slots__ = ("_client", "_secret_arn")

    def __init__(
        self,
        *,
        client: SecretsManagerGetSecretValueClient,
        secret_arn: str,
    ) -> None:
        if not isinstance(secret_arn, str) or _SECRET_ARN.fullmatch(secret_arn) is None:
            raise ValueError("An exact Phase 6 Printify Secrets Manager ARN is required")
        self._client = client
        self._secret_arn = secret_arn

    def resolve(self, *, owner_id: str) -> OwnerPrintifyConnection:
        """Return only an exact owner match; collapse every failure to one safe error."""

        try:
            return self._resolve_exact(owner_id=owner_id)
        except Exception:
            # Leave the dependency exception scope before raising so neither ``__cause__`` nor
            # ``__context__`` retains a provider response or secret-bearing error object.
            pass
        raise PrintifyAuthenticationError(_UNAVAILABLE)

    def _resolve_exact(self, *, owner_id: str) -> OwnerPrintifyConnection:
        if _OWNER_ID.fullmatch(owner_id) is None or owner_id == "0" * 64:
            raise ValueError("invalid owner")
        response = self._client.get_secret_value(SecretId=self._secret_arn)
        if not isinstance(response, Mapping) or "SecretBinary" in response:
            raise ValueError("invalid secret envelope")
        if "ARN" in response and response["ARN"] != self._secret_arn:
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
        if not isinstance(payload, dict) or set(payload) != _SECRET_FIELDS:
            raise ValueError("invalid owner secret schema")
        if payload["schema_version"] != PRINTIFY_OWNER_SECRET_SCHEMA_VERSION:
            raise ValueError("invalid owner secret schema version")
        secret_owner = payload["owner_id"]
        if (
            not isinstance(secret_owner, str)
            or _OWNER_ID.fullmatch(secret_owner) is None
            or secret_owner == "0" * 64
            or secret_owner != owner_id
        ):
            raise ValueError("owner mismatch")
        shop_id = payload["shop_id"]
        if type(shop_id) is not int or shop_id <= 0:
            raise ValueError("invalid shop")
        api_token = payload["api_token"]
        if not isinstance(api_token, str) or _API_TOKEN.fullmatch(api_token) is None:
            raise ValueError("invalid token")
        return OwnerPrintifyConnection(
            owner_id=secret_owner,
            shop_id=shop_id,
            api_token=api_token,
        )


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
    "PRINTIFY_OWNER_SECRET_SCHEMA_VERSION",
    "SecretsManagerGetSecretValueClient",
    "SecretsManagerOwnerPrintifyConnectionResolver",
]
