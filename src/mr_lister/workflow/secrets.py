"""Credential boundary reserved for real marketplace adapters in Phase 5."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol


class SecretReader(Protocol):
    """Return one secret without exposing provider-specific response envelopes."""

    def get_secret(self, secret_arn: str) -> str: ...


@dataclass(frozen=True)
class SecretsManagerSecretReader:
    """Narrow AWS Secrets Manager adapter with no listing or workflow authority."""

    client: Any

    def get_secret(self, secret_arn: str) -> str:
        if not secret_arn.startswith("arn:aws:secretsmanager:"):
            raise ValueError("A Secrets Manager ARN is required")
        response = self.client.get_secret_value(SecretId=secret_arn)
        value = response.get("SecretString")
        if not isinstance(value, str) or not value:
            raise ValueError("The secret must contain a non-empty SecretString")
        return value
