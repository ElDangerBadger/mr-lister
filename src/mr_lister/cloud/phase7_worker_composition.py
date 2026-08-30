"""Exact-disabled Phase 7.8 publication-worker composition oracle.

This module joins the already-tested request execution store, execution service, Phase 7.6
approval guard, credential boundary, staged provider boundary, and coordinator.  The graph is
dependency-injected and performs no I/O while it is assembled.  Its runtime wrapper remains
unregistered and refuses before observing invocation data or constructing the graph.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Any, Protocol

from mr_lister.cloud.phase7_configuration import (
    PinnedPublicationProfileAuthority,
    load_phase7_read_configuration,
)
from mr_lister.publication.application import PublicationRuntimeActivation
from mr_lister.publication.execution_dynamodb import DynamoDBPublicationExecutionStore
from mr_lister.publication.execution_models import (
    PublicationExecutionAuthority,
    PublicationProviderAuditRecord,
)
from mr_lister.publication.execution_service import PublicationExecutionService
from mr_lister.publication.guard_verification import (
    DurablePublicationPreCallGuard,
    PublicationGuardSourceAuthority,
)
from mr_lister.publication.profile_eligibility import (
    PinnedPublicationProfileEligibilityAuthority,
    PublicationProfileEligibility,
)
from mr_lister.publication.provider_boundary import PublicationHttpTransport
from mr_lister.publication.provider_coordinator import PublicationProviderCoordinator
from mr_lister.publication.provider_credentials import (
    PublicationProviderCredentialAuthority,
)
from mr_lister.publication.provider_runtime import PublicationProviderRuntimeFactory
from mr_lister.review_profile import ExactReviewProductProfile

PHASE7_WORKER_TIMEOUT_SECONDS = 15.0
PHASE7_WORKER_USER_AGENT = "MrLister-Phase7/phase78-disabled"


class Phase7WorkerHandler(Protocol):
    def __call__(
        self,
        event: Mapping[str, Any],
        context: object | None = None,
    ) -> dict[str, Any]: ...


class PublicationGuardStoreAdapter:
    """Translate the execution store's request graph into the stricter Phase 7.6 guard DTO."""

    __slots__ = ("_store",)

    def __init__(self, store: DynamoDBPublicationExecutionStore) -> None:
        self._store = store

    def load_execution_authority(
        self,
        owner_id: str,
        aggregate_id: str,
    ) -> PublicationExecutionAuthority:
        return self._store.load_execution_authority(owner_id, aggregate_id)

    def load_source_authority(
        self,
        owner_id: str,
        aggregate_id: str,
    ) -> PublicationGuardSourceAuthority:
        source = self._store.load_source_authority(owner_id, aggregate_id)
        return PublicationGuardSourceAuthority(
            current_job=source.current_job,
            review=source.review,
            approval_decision=source.approval_decision,
            source=source.source,
            product_sync=source.product_sync,
            pricing_snapshot=source.pricing_snapshot,
            pricing_evidence=source.pricing_evidence,
        )


def compose_publication_worker(
    environment: Mapping[str, object],
    *,
    dynamodb: object,
    credentials: PublicationProviderCredentialAuthority,
    transport: PublicationHttpTransport,
    rejected_audit_writer: Callable[[PublicationProviderAuditRecord], None],
    clock: Callable[[], datetime] | None = None,
) -> PublicationProviderCoordinator:
    """Assemble the real coordinator graph without reading state, secrets, or the provider."""

    configuration = load_phase7_read_configuration(environment)
    return compose_publication_worker_graph(
        state_table=configuration.state_table,
        release_manifest_fingerprint=configuration.release_manifest_fingerprint,
        exact_profile=configuration.profile.exact,
        eligibility=configuration.eligibility,
        dynamodb=dynamodb,
        credentials=credentials,
        transport=transport,
        rejected_audit_writer=rejected_audit_writer,
        clock=clock,
    )


def compose_publication_worker_graph(
    *,
    state_table: str,
    release_manifest_fingerprint: str,
    exact_profile: ExactReviewProductProfile,
    eligibility: PublicationProfileEligibility,
    dynamodb: object,
    credentials: PublicationProviderCredentialAuthority,
    transport: PublicationHttpTransport,
    rejected_audit_writer: Callable[[PublicationProviderAuditRecord], None],
    clock: Callable[[], datetime] | None = None,
    timeout_seconds: float = PHASE7_WORKER_TIMEOUT_SECONDS,
    user_agent: str = PHASE7_WORKER_USER_AGENT,
) -> PublicationProviderCoordinator:
    """Join a validated configuration to the worker graph without constructing capability."""

    _require_methods(
        dynamodb,
        ("get_item", "query", "transact_write_items"),
        "Phase 7 publication-worker state dependency is unavailable",
    )
    _require_methods(
        credentials,
        ("resolve_exact",),
        "Phase 7 publication-worker credential dependency is unavailable",
    )
    _require_methods(
        transport,
        ("request",),
        "Phase 7 publication-worker transport dependency is unavailable",
    )

    selected_clock = clock or (lambda: datetime.now(UTC))
    profiles = PinnedPublicationProfileAuthority(exact_profile)
    eligibility_authority = PinnedPublicationProfileEligibilityAuthority(eligibility)
    store = DynamoDBPublicationExecutionStore(
        client=dynamodb,
        table_name=state_table,
    )
    execution = PublicationExecutionService(
        store,
        profiles=profiles,
        profile_eligibility=eligibility_authority,
        release_manifest_fingerprint=release_manifest_fingerprint,
        clock=selected_clock,
    )
    guard = DurablePublicationPreCallGuard(
        store=PublicationGuardStoreAdapter(store),
        profiles=profiles,
        eligibility=eligibility_authority,
        release_manifest_fingerprint=release_manifest_fingerprint,
    )
    provider = PublicationProviderRuntimeFactory(
        store=store,
        credentials=credentials,
        transport=transport,
        release_manifest_fingerprint=release_manifest_fingerprint,
        rejected_audit_writer=rejected_audit_writer,
        clock=selected_clock,
        timeout_seconds=timeout_seconds,
        user_agent=user_agent,
    )
    return PublicationProviderCoordinator(
        store=store,
        execution=execution,
        boundary_factory=provider,
        pre_call_guard=guard,
        clock=selected_clock,
    )


class _DisabledPublicationWorkerHandler:
    """Retain the complete builder but deny before invocation material or capability access."""

    __slots__ = ("_activation", "_builder")

    def __init__(
        self,
        *,
        activation: PublicationRuntimeActivation,
        builder: Callable[[], PublicationProviderCoordinator],
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


def build_disabled_publication_worker_handler(
    environment: Mapping[str, object],
    *,
    builder: Callable[[], PublicationProviderCoordinator],
) -> Phase7WorkerHandler:
    """Return an unregistered worker wrapper pinned to the frozen disabled tuple."""

    configuration = load_phase7_read_configuration(environment)
    return _DisabledPublicationWorkerHandler(
        activation=configuration.activation,
        builder=builder,
    )


def _require_methods(value: object, methods: tuple[str, ...], message: str) -> None:
    if any(not callable(getattr(value, method, None)) for method in methods):
        raise RuntimeError(message)


__all__ = [
    "Phase7WorkerHandler",
    "PublicationGuardStoreAdapter",
    "build_disabled_publication_worker_handler",
    "compose_publication_worker",
    "compose_publication_worker_graph",
]
