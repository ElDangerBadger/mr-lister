"""Rotation-safe, owner-bound Printify credentials from AWS Secrets Manager."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Any, Protocol

from mr_lister.cloud.printify_secret_contract import (
    MAX_PRINTIFY_API_TOKEN_CHARS,
    MAX_PRINTIFY_DELEGATED_OWNER_GRANTS,
    PRINTIFY_DELEGATED_OWNER_SECRET_SCHEMA_VERSION,
    PRINTIFY_OWNER_SECRET_SCHEMA_VERSION,
    parse_printify_owner_secret,
    valid_printify_owner_id,
    validate_printify_secret_arn,
)
from mr_lister.production.printify import PrintifyAuthenticationError
from mr_lister.production.provider_resources import OwnerPrintifyConnection

_UNAVAILABLE = "Owner-bound Printify credential is unavailable"


class SecretsManagerGetSecretValueClient(Protocol):
    def get_secret_value(self, **kwargs: Any) -> Mapping[str, Any]: ...


class SecretsManagerOwnerPrintifyConnectionResolver:
    """Resolve one primary owner's connection and explicit expiring grants afresh."""

    __slots__ = ("_client", "_secret_arn", "_clock")

    def __init__(
        self,
        *,
        client: SecretsManagerGetSecretValueClient,
        secret_arn: str,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        validate_printify_secret_arn(secret_arn)
        self._client = client
        self._secret_arn = secret_arn
        self._clock = clock or (lambda: datetime.now(UTC))

    def resolve(self, *, owner_id: str) -> OwnerPrintifyConnection:
        """Keep the requesting owner identity; collapse every failure to one safe error."""

        return self._resolve(owner_id=owner_id, primary_only=False)

    def resolve_primary(self, *, owner_id: str) -> OwnerPrintifyConnection:
        """Require the primary owner even when the secret permits delegated workflows."""

        return self._resolve(owner_id=owner_id, primary_only=True)

    def _resolve(self, *, owner_id: str, primary_only: bool) -> OwnerPrintifyConnection:
        try:
            return self._resolve_exact(owner_id=owner_id, primary_only=primary_only)
        except Exception:
            # Leave the dependency exception scope before raising so neither ``__cause__`` nor
            # ``__context__`` retains a provider response or secret-bearing error object.
            pass
        raise PrintifyAuthenticationError(_UNAVAILABLE)

    def _resolve_exact(self, *, owner_id: str, primary_only: bool) -> OwnerPrintifyConnection:
        if not valid_printify_owner_id(owner_id):
            raise ValueError("invalid owner")
        response = self._client.get_secret_value(SecretId=self._secret_arn)
        connection = parse_printify_owner_secret(
            response,
            secret_arn=self._secret_arn,
            owner_id=owner_id,
            primary_only=primary_only,
            clock=self._clock,
        )
        return OwnerPrintifyConnection(
            owner_id=connection.owner_id,
            shop_id=connection.shop_id,
            api_token=connection.api_token,
        )


__all__ = [
    "MAX_PRINTIFY_API_TOKEN_CHARS",
    "MAX_PRINTIFY_DELEGATED_OWNER_GRANTS",
    "PRINTIFY_DELEGATED_OWNER_SECRET_SCHEMA_VERSION",
    "PRINTIFY_OWNER_SECRET_SCHEMA_VERSION",
    "SecretsManagerGetSecretValueClient",
    "SecretsManagerOwnerPrintifyConnectionResolver",
]
