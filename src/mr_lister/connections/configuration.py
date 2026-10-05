"""Metadata-only connection configuration for cloud functions and AgentCore.

No provider, credential reader, HTTP handler, or SDK client is imported or constructed here.
The exact account configuration validates expected issuer/client strings by equality.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol

from mr_lister.accounts.models import AccountConfig

from .models import ConnectionConfig, ConnectionError
from .store import DynamoConnectionDirectory


class ConnectionClaimsAuthority(Protocol):
    issuer: str
    client_id: str


@dataclass(frozen=True, slots=True)
class _ExpectedClaims:
    issuer: str
    client_id: str


CONNECTION_ENVIRONMENT_NAMES = (
    "MR_LISTER_CONNECTION_ENABLED",
    "MR_LISTER_CONNECTION_TABLE_NAME",
    "MR_LISTER_CONNECTION_SECRET_PREFIX",
    "MR_LISTER_CONNECTION_WORKFLOW_ENABLED",
    "MR_LISTER_ACCOUNT_USER_POOL_ID",
    "MR_LISTER_ACCOUNT_CLIENT_ID",
    "MR_LISTER_ACCOUNT_TABLE_NAME",
    "MR_LISTER_ACCOUNT_RESERVED_OWNER_IDS",
)


def validate_connection_configuration(
    config: ConnectionConfig,
    *,
    region: str,
    environment_name: str,
    claims_policy: ConnectionClaimsAuthority,
) -> ConnectionConfig:
    try:
        accounts = AccountConfig(
            user_pool_id=config.accounts.user_pool_id,
            client_id=config.accounts.client_id,
            table_name=config.accounts.table_name,
            reserved_owner_ids=config.accounts.reserved_owner_ids,
        )
        exact = ConnectionConfig(
            accounts, config.table_name, config.secret_prefix, config.workflow_enabled
        )
        if (
            accounts.region != region
            or accounts.issuer != claims_policy.issuer
            or accounts.client_id != claims_policy.client_id
            or accounts.table_name != f"mr-lister-account-{environment_name}"
            or exact.table_name != f"mr-lister-connections-{environment_name}"
            or exact.secret_prefix != f"mr-lister/{environment_name}/connections/"
            or len(accounts.reserved_owner_ids) > 40
        ):
            raise ValueError
        return exact
    except Exception:
        pass
    raise ConnectionError


def load_connection_configuration(
    environment: Mapping[str, object],
    *,
    region: str,
    environment_name: str,
    claims_policy: ConnectionClaimsAuthority | None = None,
) -> ConnectionConfig | None:
    try:
        flag = environment.get("MR_LISTER_CONNECTION_ENABLED")
        if flag in (None, "false"):
            return None
        if flag != "true" or environment.get("MR_LISTER_CONNECTION_WORKFLOW_ENABLED") not in (
            "true",
            "false",
        ):
            raise ValueError
        if claims_policy is None:
            issuer = environment["MR_LISTER_COGNITO_ISSUER"]
            client_id = environment["MR_LISTER_COGNITO_CLIENT_ID"]
            if not isinstance(issuer, str) or not isinstance(client_id, str):
                raise ValueError
            claims_policy = _ExpectedClaims(issuer=issuer, client_id=client_id)
        return validate_connection_configuration(
            ConnectionConfig.from_environment(environment),
            region=region,
            environment_name=environment_name,
            claims_policy=claims_policy,
        )
    except Exception:
        pass
    raise ConnectionError


def connection_directory(
    config: ConnectionConfig | None, dynamodb: object
) -> DynamoConnectionDirectory | None:
    if config is None:
        return None
    if not callable(getattr(dynamodb, "get_item", None)):
        raise ConnectionError
    return DynamoConnectionDirectory(dynamodb, config)
