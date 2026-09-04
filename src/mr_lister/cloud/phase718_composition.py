"""Enabled Phase 7.18 query, request, and one-step worker composition."""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from datetime import datetime
from typing import Any, Literal, Protocol

from mr_lister.cloud.phase7_composition import (
    Phase7OwnerAuthenticator,
    PinnedPublicationProfileAuthority,
)
from mr_lister.cloud.phase718_configuration import (
    Phase718EnabledConfiguration,
    load_phase718_enabled_configuration,
    validate_phase718_enabled_configuration,
)
from mr_lister.control.dynamodb import DynamoDBSellerControlStore
from mr_lister.publication.application import DynamoPublicationProjectionStore
from mr_lister.publication.dynamodb import DynamoDBPublicationStore
from mr_lister.publication.enabled_api import (
    Phase718PublicationQueryApiAdapter,
    Phase718PublicationRequestApiAdapter,
)
from mr_lister.publication.enabled_projection import (
    Phase718SellerPublicationProjectionService,
)
from mr_lister.publication.execution_dynamodb import DynamoDBPublicationExecutionStore
from mr_lister.publication.execution_models import PublicationProviderAuditRecord
from mr_lister.publication.profile_eligibility import (
    PinnedPublicationProfileEligibilityAuthority,
)
from mr_lister.publication.provider_boundary import PublicationHttpTransport
from mr_lister.publication.provider_coordinator import (
    PublicationProviderCoordinator,
    PublicationProviderCoordinatorResult,
)
from mr_lister.publication.provider_credentials import (
    PublicationProviderCredentialAuthority,
)
from mr_lister.publication.request_api import PublicationRequestApiAdapter
from mr_lister.publication.service import PublicationRequestService

from .phase7_worker_composition import compose_publication_worker_graph

Phase718DynamoAwsService = Literal["dynamodb"]
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
_OWNER_ID = re.compile(r"^[a-f0-9]{64}$")


class Phase718DynamoClientFactory(Protocol):
    def __call__(
        self,
        service_name: Phase718DynamoAwsService,
        *,
        region_name: str,
    ) -> object: ...


class Phase718Handler(Protocol):
    def __call__(
        self,
        event: Mapping[str, Any],
        context: object | None = None,
    ) -> dict[str, Any]: ...


def compose_phase718_query_handler(
    configuration: Phase718EnabledConfiguration,
    *,
    client_factory: Phase718DynamoClientFactory,
) -> Phase718PublicationQueryApiAdapter:
    """Build the authenticated owner-scoped read graph without performing a read."""

    exact = validate_phase718_enabled_configuration(configuration)
    dynamodb = _client(
        client_factory,
        exact.region,
        required_methods=("get_item", "query"),
    )
    jobs = DynamoDBSellerControlStore(client=dynamodb, table_name=exact.state_table)
    execution = DynamoDBPublicationExecutionStore(client=dynamodb, table_name=exact.state_table)
    store = DynamoPublicationProjectionStore(jobs=jobs, execution=execution)
    return Phase718PublicationQueryApiAdapter(
        authenticator=Phase7OwnerAuthenticator(exact.foundation.claims_policy),
        projections=Phase718SellerPublicationProjectionService(store),
    )


def compose_phase718_request_handler(
    configuration: Phase718EnabledConfiguration,
    *,
    client_factory: Phase718DynamoClientFactory,
    clock: Callable[[], datetime] | None = None,
) -> Phase718PublicationRequestApiAdapter:
    """Build the existing atomic request service behind the enabled HTTP discriminator."""

    exact = validate_phase718_enabled_configuration(configuration)
    dynamodb = _client(
        client_factory,
        exact.region,
        required_methods=("get_item", "transact_write_items"),
    )
    jobs = DynamoDBSellerControlStore(client=dynamodb, table_name=exact.state_table)
    store = DynamoDBPublicationStore(client=dynamodb, table_name=exact.state_table)
    service = PublicationRequestService(
        store=store,
        profiles=PinnedPublicationProfileAuthority(exact.foundation.profile.exact),
        profile_eligibility=PinnedPublicationProfileEligibilityAuthority(
            exact.foundation.eligibility
        ),
        release_manifest_fingerprint=exact.application_release_fingerprint,
        clock=clock,
    )
    delegate = PublicationRequestApiAdapter(
        authenticator=Phase7OwnerAuthenticator(exact.foundation.claims_policy),
        approvals=jobs,
        requests=service,
    )
    return Phase718PublicationRequestApiAdapter(delegate)


def compose_phase718_worker_handler(
    configuration: Phase718EnabledConfiguration,
    *,
    dynamodb: object,
    credentials: PublicationProviderCredentialAuthority,
    transport: PublicationHttpTransport,
    rejected_audit_writer: Callable[[PublicationProviderAuditRecord], None],
    clock: Callable[[], datetime] | None = None,
) -> Phase718Handler:
    """Build one enabled coordinator step; construction performs no state, secret, or wire I/O."""

    configuration = validate_phase718_enabled_configuration(configuration)
    coordinator = compose_publication_worker_graph(
        state_table=configuration.state_table,
        release_manifest_fingerprint=configuration.application_release_fingerprint,
        exact_profile=configuration.foundation.profile.exact,
        eligibility=configuration.foundation.eligibility,
        dynamodb=dynamodb,
        credentials=credentials,
        transport=transport,
        rejected_audit_writer=rejected_audit_writer,
        clock=clock,
        user_agent="MrLister-Phase7/phase718-enabled",
    )
    return _Phase718PublicationWorkerHandler(coordinator)


def build_phase718_worker_handler(
    environment: Mapping[str, object],
    *,
    dynamodb: object,
    credentials: PublicationProviderCredentialAuthority,
    transport: PublicationHttpTransport,
    rejected_audit_writer: Callable[[PublicationProviderAuditRecord], None],
    clock: Callable[[], datetime] | None = None,
) -> Phase718Handler:
    return compose_phase718_worker_handler(
        load_phase718_enabled_configuration(environment),
        dynamodb=dynamodb,
        credentials=credentials,
        transport=transport,
        rejected_audit_writer=rejected_audit_writer,
        clock=clock,
    )


class _Phase718PublicationWorkerHandler:
    """Accept only the identifier-minimal payload emitted by the bounded workflow."""

    __slots__ = ("_coordinator",)

    def __init__(self, coordinator: PublicationProviderCoordinator) -> None:
        self._coordinator = coordinator

    def __call__(
        self,
        event: Mapping[str, Any],
        context: object | None = None,
    ) -> dict[str, Any]:
        del context
        try:
            if not isinstance(event, Mapping) or set(event) != {"owner_id", "aggregate_id"}:
                raise ValueError
            owner_id = event["owner_id"]
            aggregate_id = event["aggregate_id"]
            if (
                not isinstance(owner_id, str)
                or _OWNER_ID.fullmatch(owner_id) is None
                or not isinstance(aggregate_id, str)
                or _SAFE_ID.fullmatch(aggregate_id) is None
            ):
                raise ValueError
            result = self._coordinator.advance(
                owner_id=owner_id,
                aggregate_id=aggregate_id,
            )
            if not isinstance(result, PublicationProviderCoordinatorResult):
                raise ValueError
            return {
                "contract_version": "7.1.0",
                "action": result.action.value,
                "aggregate_state": result.aggregate_state.value,
            }
        except Exception:
            raise RuntimeError("Phase 7.18 publication step failed safely") from None


def build_phase718_query_handler(
    environment: Mapping[str, object],
    *,
    client_factory: Phase718DynamoClientFactory,
) -> Phase718PublicationQueryApiAdapter:
    return compose_phase718_query_handler(
        load_phase718_enabled_configuration(environment),
        client_factory=client_factory,
    )


def build_phase718_request_handler(
    environment: Mapping[str, object],
    *,
    client_factory: Phase718DynamoClientFactory,
    clock: Callable[[], datetime] | None = None,
) -> Phase718PublicationRequestApiAdapter:
    return compose_phase718_request_handler(
        load_phase718_enabled_configuration(environment),
        client_factory=client_factory,
        clock=clock,
    )


def _client(
    factory: Phase718DynamoClientFactory,
    region: str,
    *,
    required_methods: tuple[str, ...],
) -> object:
    client = factory("dynamodb", region_name=region)
    if any(not callable(getattr(client, method, None)) for method in required_methods):
        raise RuntimeError("Phase 7.18 DynamoDB dependency is unavailable")
    return client


__all__ = [
    "Phase718DynamoClientFactory",
    "Phase718Handler",
    "build_phase718_query_handler",
    "build_phase718_request_handler",
    "build_phase718_worker_handler",
    "compose_phase718_query_handler",
    "compose_phase718_request_handler",
    "compose_phase718_worker_handler",
]
