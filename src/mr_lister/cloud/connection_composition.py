"""Cloud provider routing with compatible reexports of metadata-only configuration."""

from __future__ import annotations

import json
from collections.abc import Mapping

from mr_lister.accounts.models import AccountConfig, strict_json, valid_owner
from mr_lister.cloud.auth import SellerClaimsPolicy
from mr_lister.connections.binding import StoreBindingAuthority
from mr_lister.connections.configuration import (
    CONNECTION_ENVIRONMENT_NAMES,
)
from mr_lister.connections.configuration import (
    connection_directory as connection_directory,
)
from mr_lister.connections.configuration import (
    load_connection_configuration as load_connection_configuration,
)
from mr_lister.connections.configuration import (
    validate_connection_configuration as validate_connection_configuration,
)
from mr_lister.connections.models import ConnectionConfig, ConnectionError
from mr_lister.connections.store import DynamoConnectionDirectory


def load_upload_legacy_owner_ids(
    environment: Mapping[str, object],
    *,
    region: str,
    environment_name: str,
    claims_policy: SellerClaimsPolicy,
) -> frozenset[str] | None:
    """A staged whitelist survives disabling the new connection capability.

    Only a truly pre-account configuration may lack the whitelist. Native account-group
    tokens still require a bound destination at the HTTP adapter, including after rollback.
    """
    try:
        staged = any(name in environment for name in CONNECTION_ENVIRONMENT_NAMES)
        if not staged:
            if "MR_LISTER_LEGACY_OWNER_IDS" in environment:
                return load_legacy_owner_ids(environment, None)
            return None
        flag = environment.get("MR_LISTER_CONNECTION_ENABLED")
        if flag not in ("true", "false"):
            raise ValueError
        accounts = AccountConfig.from_environment(environment)
        if (
            accounts.region != region
            or accounts.issuer != claims_policy.issuer
            or accounts.client_id != claims_policy.client_id
            or accounts.table_name != f"mr-lister-account-{environment_name}"
            or len(accounts.reserved_owner_ids) > 40
        ):
            raise ValueError
        if (
            "MR_LISTER_LEGACY_OWNER_IDS" in environment
            and load_legacy_owner_ids(environment, None) != accounts.reserved_owner_ids
        ):
            raise ValueError
        return accounts.reserved_owner_ids
    except Exception:
        pass
    raise ConnectionError


class RoutedConnectionResolver:
    """Legacy owners use their strict old resolver; modern jobs require exact original bindings."""

    def __init__(
        self,
        *,
        legacy: object,
        directory: DynamoConnectionDirectory,
        secrets: object,
        config: ConnectionConfig,
    ) -> None:
        from mr_lister.connections.credentials import (
            ExactConnectionResolver,
            SecretsManagerCredentialReader,
        )

        self._legacy = legacy
        self._legacy_owner_ids = config.accounts.reserved_owner_ids
        self._modern = ExactConnectionResolver(
            directory=directory, credentials=SecretsManagerCredentialReader(secrets, config)
        )

    def resolve(self, *, owner_id: str):
        try:
            if owner_id not in self._legacy_owner_ids:
                raise ValueError
            return self._legacy.resolve(owner_id=owner_id)
        except Exception:
            pass
        raise ConnectionError

    def resolve_exact(self, *, binding: StoreBindingAuthority):
        try:
            binding = StoreBindingAuthority.model_validate(binding.model_dump())
            if binding.owner_id in self._legacy_owner_ids:
                raise ValueError
            return self._modern.resolve_exact(binding=binding)
        except Exception:
            pass
        raise ConnectionError


def load_legacy_owner_ids(
    environment: Mapping[str, object], connections: ConnectionConfig | None
) -> frozenset[str]:
    try:
        raw = environment.get("MR_LISTER_LEGACY_OWNER_IDS")
        if raw is None and connections is not None:
            return connections.accounts.reserved_owner_ids
        values = strict_json(raw, limit=3072)
        if (
            not isinstance(values, list)
            or not 1 <= len(values) <= 40
            or any(not valid_owner(value) for value in values)
            or len(set(values)) != len(values)
            or raw != json.dumps(sorted(values), separators=(",", ":"))
        ):
            raise ValueError
        owners = frozenset(values)
        if connections is not None and owners != connections.accounts.reserved_owner_ids:
            raise ValueError
        return owners
    except Exception:
        pass
    raise ConnectionError
