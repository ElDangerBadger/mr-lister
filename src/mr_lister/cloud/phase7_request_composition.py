"""Exact-disabled Phase 7.7 publication-request composition oracle.

The real request graph can be assembled offline from the Phase 7.1 transaction service, but the
runtime handler returned by this module always refuses before it observes an event or constructs
an AWS client.  Nothing here registers a seller route, starts publication work, resolves a
provider credential, or grants a provider capability.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any, Literal, Protocol

from mr_lister.cloud.phase7_composition import (
    Phase7OwnerAuthenticator,
    Phase7ReadConfiguration,
    PinnedPublicationProfileAuthority,
    load_phase7_read_configuration,
)
from mr_lister.control.dynamodb import DynamoDBSellerControlStore
from mr_lister.publication.application import PublicationRuntimeActivation
from mr_lister.publication.dynamodb import DynamoDBPublicationStore
from mr_lister.publication.profile_eligibility import (
    PinnedPublicationProfileEligibilityAuthority,
)
from mr_lister.publication.request_api import PublicationRequestApiAdapter
from mr_lister.publication.service import PublicationRequestService

Phase7RequestAwsService = Literal["dynamodb"]


class Phase7RequestAwsClientFactory(Protocol):
    def __call__(
        self,
        service_name: Phase7RequestAwsService,
        *,
        region_name: str,
    ) -> object: ...


class Phase7RequestHandler(Protocol):
    def __call__(
        self,
        event: Mapping[str, Any],
        context: object | None = None,
    ) -> dict[str, Any]: ...


def compose_publication_request_adapter(
    environment: Mapping[str, object],
    *,
    client_factory: Phase7RequestAwsClientFactory,
) -> PublicationRequestApiAdapter:
    """Assemble the real request-only graph for offline composition verification.

    The shared Phase 7.4 configuration loader deliberately accepts only the frozen exact-disabled
    tuple.  Composition creates one DynamoDB client with the read and atomic-write methods needed
    by the request transaction; it performs no read or write itself.
    """

    configuration = load_phase7_read_configuration(environment)
    return _compose_publication_request_adapter(
        configuration,
        client_factory=client_factory,
    )


def _compose_publication_request_adapter(
    configuration: Phase7ReadConfiguration,
    *,
    client_factory: Phase7RequestAwsClientFactory,
) -> PublicationRequestApiAdapter:
    dynamodb = _client(
        client_factory,
        "dynamodb",
        configuration.region,
        required_methods=("get_item", "transact_write_items"),
    )
    jobs = DynamoDBSellerControlStore(
        client=dynamodb,
        table_name=configuration.state_table,
    )
    store = DynamoDBPublicationStore(
        client=dynamodb,
        table_name=configuration.state_table,
    )
    requests = PublicationRequestService(
        store=store,
        profiles=PinnedPublicationProfileAuthority(configuration.profile.exact),
        profile_eligibility=PinnedPublicationProfileEligibilityAuthority(configuration.eligibility),
        release_manifest_fingerprint=configuration.release_manifest_fingerprint,
    )
    return PublicationRequestApiAdapter(
        authenticator=Phase7OwnerAuthenticator(configuration.claims_policy),
        approvals=jobs,
        requests=requests,
    )


class _DisabledPublicationRequestHandler:
    """Retain the composition oracle while denying before data or capability access."""

    __slots__ = ("_activation", "_builder")

    def __init__(
        self,
        *,
        activation: PublicationRuntimeActivation,
        builder: Callable[[], PublicationRequestApiAdapter],
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


def build_disabled_publication_request_handler(
    environment: Mapping[str, object],
    *,
    client_factory: Phase7RequestAwsClientFactory | None = None,
) -> Phase7RequestHandler:
    """Return an unregistered handler that cannot create publication intent."""

    configuration = load_phase7_read_configuration(environment)
    factory = client_factory or _unavailable_client_factory
    return _DisabledPublicationRequestHandler(
        activation=configuration.activation,
        builder=lambda: _compose_publication_request_adapter(
            configuration,
            client_factory=factory,
        ),
    )


def _unavailable_client_factory(
    service_name: Phase7RequestAwsService,
    *,
    region_name: str,
) -> object:
    """Keep the oracle dependency-injected until a separately reviewed runtime exists."""

    del service_name, region_name
    raise RuntimeError("Phase 7 publication-request dependency is unavailable")


def _client(
    factory: Phase7RequestAwsClientFactory,
    service_name: Phase7RequestAwsService,
    region_name: str,
    *,
    required_methods: tuple[str, ...],
) -> object:
    client = factory(service_name, region_name=region_name)
    if any(not callable(getattr(client, method, None)) for method in required_methods):
        raise RuntimeError("Phase 7 publication-request dependency is unavailable")
    return client


__all__ = [
    "Phase7RequestHandler",
    "build_disabled_publication_request_handler",
    "compose_publication_request_adapter",
]
