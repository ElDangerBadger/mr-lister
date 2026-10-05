"""Uncomposed Phase 7 credential adapter over the existing owner-secret resolver.

The adapter creates no SDK client and no provider boundary.  It resolves the existing strict
Phase 6 owner secret afresh, verifies the returned owner and Printify shop against one fully
re-parsed publication authority, and returns only an opaque aggregate/snapshot-bound capability.
"""

from __future__ import annotations

from pydantic import SecretStr

from mr_lister.production.provider_resources import (
    OwnerPrintifyConnection,
    OwnerPrintifyConnectionResolver,
)
from mr_lister.production.provider_secrets import (
    SecretsManagerGetSecretValueClient,
    SecretsManagerOwnerPrintifyConnectionResolver,
)
from mr_lister.publication.execution_models import (
    PublicationExecutionAuthority,
    PublicationProviderAuthority,
)
from mr_lister.publication.provider_credentials import (
    BoundPublicationProviderCredential,
    PublicationProviderCredentialError,
    issue_bound_publication_provider_credential,
)

_UNAVAILABLE = "Publication provider credential is unavailable"


class ProductionPublicationProviderCredentialAuthority:
    """Adapt fresh owner-secret resolution to one exact publication authority."""

    __slots__ = ("_connections", "_legacy_owner_ids")

    def __init__(
        self,
        *,
        connections: OwnerPrintifyConnectionResolver,
        legacy_owner_ids: frozenset[str] = frozenset(),
    ) -> None:
        self._connections = connections
        self._legacy_owner_ids = frozenset(legacy_owner_ids)

    def resolve_exact(
        self,
        *,
        authority: PublicationProviderAuthority,
    ) -> BoundPublicationProviderCredential:
        try:
            exact = PublicationProviderAuthority.model_validate(authority.model_dump(mode="python"))
            if exact.store_binding is not None:
                resolved = self._connections.resolve_exact(binding=exact.store_binding)
            else:
                if exact.owner_id not in self._legacy_owner_ids:
                    raise ValueError
                resolved = self._connections.resolve(owner_id=exact.owner_id)
            if (
                not isinstance(resolved, OwnerPrintifyConnection)
                or resolved.owner_id != exact.owner_id
                or type(resolved.shop_id) is not int
                or resolved.shop_id != exact.printify_shop_id
                or not isinstance(resolved.api_token, SecretStr)
                or resolved.store_binding != exact.store_binding
            ):
                raise ValueError
            return issue_bound_publication_provider_credential(
                authority=exact,
                bearer_token=resolved.api_token,
            )
        except Exception:
            pass
        raise PublicationProviderCredentialError(_UNAVAILABLE) from None

    def prepare_credential(
        self,
        *,
        execution_authority: PublicationExecutionAuthority,
    ) -> BoundPublicationProviderCredential:
        """Resolve before a durable call claim from one fully re-parsed execution graph."""

        try:
            exact = PublicationExecutionAuthority.model_validate(
                execution_authority.model_dump(mode="python")
            )
            if exact.provider_authority is None:
                raise ValueError
            credential = self.resolve_exact(authority=exact.provider_authority)
        except Exception:
            pass
        else:
            return credential
        raise PublicationProviderCredentialError(_UNAVAILABLE) from None


def build_phase7_publication_provider_credential_authority(
    *,
    client: SecretsManagerGetSecretValueClient,
    secret_arn: str,
    legacy_owner_ids: frozenset[str] = frozenset(),
) -> ProductionPublicationProviderCredentialAuthority:
    """Build the injected adapter without constructing an AWS client or provider transport."""

    try:
        resolver = SecretsManagerOwnerPrintifyConnectionResolver(
            client=client,
            secret_arn=secret_arn,
        )
        return ProductionPublicationProviderCredentialAuthority(
            connections=resolver, legacy_owner_ids=legacy_owner_ids
        )
    except Exception:
        pass
    raise PublicationProviderCredentialError(
        "Publication provider credential configuration is invalid"
    ) from None


__all__ = [
    "ProductionPublicationProviderCredentialAuthority",
    "build_phase7_publication_provider_credential_authority",
]
