"""Disabled, read-only Phase 7.4 publication-status composition.

This module builds only the owner-scoped publication projection graph. Configuration authority
lives in a capability-free sibling so request and worker composition do not inherit this module's
optional default DynamoDB client factory.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any, Literal, Protocol

from mr_lister.cloud.auth import SellerClaimsPolicy, authenticate_seller
from mr_lister.cloud.phase7_configuration import (
    SELLER_GROUP,
    SELLER_SCOPE,
    Phase7ReadConfiguration,
    Phase7ReadConfigurationError,
    PinnedPublicationProfileAuthority,
    load_phase7_read_configuration,
    validate_phase7_read_configuration,
)
from mr_lister.control.dynamodb import DynamoDBSellerControlStore
from mr_lister.publication.application import (
    DynamoPublicationProjectionStore,
    PublicationRuntimeActivation,
)
from mr_lister.publication.execution_dynamodb import DynamoDBPublicationExecutionStore
from mr_lister.publication.projection import SellerPublicationProjectionService
from mr_lister.publication.query_api import PublicationQueryApiAdapter

Phase7ReadAwsService = Literal["dynamodb"]


class Phase7ReadAwsClientFactory(Protocol):
    def __call__(
        self,
        service_name: Phase7ReadAwsService,
        *,
        region_name: str,
    ) -> object: ...


class Phase7QueryHandler(Protocol):
    def __call__(
        self,
        event: Mapping[str, Any],
        context: object | None = None,
    ) -> dict[str, Any]: ...


class Phase7OwnerAuthenticator:
    """Return only the opaque owner ID derived from the verified seller JWT context."""

    __slots__ = ("_policy",)

    def __init__(self, policy: SellerClaimsPolicy) -> None:
        self._policy = policy

    def authenticate(self, event: Mapping[str, Any]) -> str:
        return authenticate_seller(event, policy=self._policy).owner_id


def compose_publication_query_adapter(
    configuration: Phase7ReadConfiguration,
    *,
    client_factory: Phase7ReadAwsClientFactory,
) -> PublicationQueryApiAdapter:
    """Build only owner-scoped strongly consistent reads and the safe projection adapter."""

    configuration = validate_phase7_read_configuration(configuration)
    dynamodb = _client(
        client_factory,
        "dynamodb",
        configuration.region,
        required_methods=("get_item", "query"),
    )
    jobs = DynamoDBSellerControlStore(client=dynamodb, table_name=configuration.state_table)
    execution = DynamoDBPublicationExecutionStore(
        client=dynamodb,
        table_name=configuration.state_table,
    )
    projection_store = DynamoPublicationProjectionStore(jobs=jobs, execution=execution)
    projections = SellerPublicationProjectionService(projection_store)
    return PublicationQueryApiAdapter(
        authenticator=Phase7OwnerAuthenticator(configuration.claims_policy),
        projections=projections,
    )


class _DisabledPublicationQueryHandler:
    """Retain a future read-only builder but refuse before it or request data is observed."""

    __slots__ = ("_activation", "_builder")

    def __init__(
        self,
        *,
        activation: PublicationRuntimeActivation,
        builder: Callable[[], PublicationQueryApiAdapter],
    ) -> None:
        self._activation = activation
        self._builder = builder

    def __call__(
        self,
        event: Mapping[str, Any],
        context: object | None = None,
    ) -> dict[str, Any]:
        del event, context
        self._activation.deny_runtime()


def build_disabled_publication_query_handler(
    environment: Mapping[str, object],
    *,
    client_factory: Phase7ReadAwsClientFactory | None = None,
) -> Phase7QueryHandler:
    """Return the unregistered exact-disabled query entrypoint for contract 7.0.1."""

    configuration = load_phase7_read_configuration(environment)
    factory = client_factory or default_aws_client_factory
    return _DisabledPublicationQueryHandler(
        activation=configuration.activation,
        builder=lambda: compose_publication_query_adapter(
            configuration,
            client_factory=factory,
        ),
    )


def default_aws_client_factory(
    service_name: Phase7ReadAwsService,
    *,
    region_name: str,
) -> object:
    """Create only the future read role's regional DynamoDB client, never at import time."""

    if service_name != "dynamodb":
        raise ValueError("Unsupported Phase 7 read-only AWS client")
    import boto3

    return boto3.client("dynamodb", region_name=region_name)


def _client(
    factory: Phase7ReadAwsClientFactory,
    service_name: Phase7ReadAwsService,
    region_name: str,
    *,
    required_methods: tuple[str, ...],
) -> object:
    client = factory(service_name, region_name=region_name)
    if any(not callable(getattr(client, method, None)) for method in required_methods):
        raise RuntimeError("Phase 7 read-only dependency is unavailable")
    return client


__all__ = [
    "SELLER_GROUP",
    "SELLER_SCOPE",
    "Phase7OwnerAuthenticator",
    "Phase7ReadConfiguration",
    "Phase7ReadConfigurationError",
    "PinnedPublicationProfileAuthority",
    "build_disabled_publication_query_handler",
    "compose_publication_query_adapter",
    "load_phase7_read_configuration",
]
