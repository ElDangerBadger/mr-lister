"""Capability-reduced composition for the Phase 7.15C non-provider operations drill.

The module deliberately omits the dispatcher, SQS send path, StartExecution, provider clients,
credential resolution, seller routes, and default AWS client construction.  Callers may inject
only DynamoDB and same-execution Step Functions read/redrive collaborators.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta

from mr_lister.publication.execution_dynamodb import DynamoDBPublicationExecutionStore
from mr_lister.publication.execution_service import PublicationExecutionService
from mr_lister.publication.orchestration_dynamodb import (
    DynamoDBPublicationRecoveryInventory,
    DynamoDBPublicationTerminalIdentityResolver,
)
from mr_lister.publication.orchestration_recovery import (
    DEFAULT_PUBLICATION_RECOVERY_STALE_AFTER,
    PublicationRecoverySweeper,
    PublicationWorkflowRecovery,
)
from mr_lister.publication.profile_eligibility import (
    PinnedPublicationProfileEligibilityAuthority,
    PublicationProfileEligibility,
)
from mr_lister.publication.retention import PublicationOperationalRetentionService
from mr_lister.publication.retention_dynamodb import (
    DynamoDBPublicationOperationalRetentionStore,
)
from mr_lister.review_profile import ExactReviewProductProfile, ReviewProfileNotFoundError

from .phase715c_operations_handlers import (
    Phase715cPublicationRecoveryQueueHandler,
    Phase715cPublicationRecoverySweepHandler,
    Phase715cPublicationRetentionHandler,
)


class _PinnedOperationsProfileAuthority:
    """Expose one sealed profile without directory-wide or seller-query authority."""

    __slots__ = ("_exact",)

    def __init__(self, exact: ExactReviewProductProfile) -> None:
        self._exact = exact

    def get_exact(
        self,
        *,
        profile_id: str,
        profile_version: int,
    ) -> ExactReviewProductProfile:
        if (
            profile_id != self._exact.profile.profile_id
            or profile_version != self._exact.profile.profile_version
        ):
            raise ReviewProfileNotFoundError("The review product profile was not found")
        return self._exact


def compose_publication_recovery_queue_handler(
    *,
    state_table: str,
    state_machine_arn: str,
    release_manifest_fingerprint: str,
    exact_profile: ExactReviewProductProfile,
    eligibility: PublicationProfileEligibility,
    dynamodb: object,
    step_functions: object,
    clock: Callable[[], datetime] | None = None,
) -> Phase715cPublicationRecoveryQueueHandler:
    """Compose only same-ARN redrive and provider-free deadline settlement authority."""

    _require_methods(
        dynamodb,
        ("get_item", "query", "transact_write_items"),
        "Phase 7.15C recovery dependency is unavailable",
    )
    _require_methods(
        step_functions,
        ("describe_execution", "redrive_execution"),
        "Phase 7.15C recovery dependency is unavailable",
    )
    return Phase715cPublicationRecoveryQueueHandler(
        recovery=_compose_publication_workflow_recovery(
            state_table=state_table,
            state_machine_arn=state_machine_arn,
            release_manifest_fingerprint=release_manifest_fingerprint,
            exact_profile=exact_profile,
            eligibility=eligibility,
            dynamodb=dynamodb,
            step_functions=step_functions,
            clock=clock,
        )
    )


def compose_publication_recovery_sweep_handler(
    *,
    state_table: str,
    state_machine_arn: str,
    release_manifest_fingerprint: str,
    exact_profile: ExactReviewProductProfile,
    eligibility: PublicationProfileEligibility,
    dynamodb: object,
    step_functions: object,
    clock: Callable[[], datetime] | None = None,
    stale_after: timedelta = DEFAULT_PUBLICATION_RECOVERY_STALE_AFTER,
) -> Phase715cPublicationRecoverySweepHandler:
    """Compose one max-25 recovery query with same-ARN recovery and no start seam."""

    _require_methods(
        dynamodb,
        ("get_item", "query", "transact_write_items"),
        "Phase 7.15C recovery dependency is unavailable",
    )
    _require_methods(
        step_functions,
        ("describe_execution", "redrive_execution"),
        "Phase 7.15C recovery dependency is unavailable",
    )
    inventory = DynamoDBPublicationRecoveryInventory(
        client=dynamodb,  # type: ignore[arg-type]
        table_name=state_table,
    )
    recovery = _compose_publication_workflow_recovery(
        state_table=state_table,
        state_machine_arn=state_machine_arn,
        release_manifest_fingerprint=release_manifest_fingerprint,
        exact_profile=exact_profile,
        eligibility=eligibility,
        dynamodb=dynamodb,
        step_functions=step_functions,
        clock=clock,
    )
    return Phase715cPublicationRecoverySweepHandler(
        sweeper=PublicationRecoverySweeper(
            inventory=inventory,
            recovery=recovery,
            clock=clock,
            stale_after=stale_after,
        )
    )


def _compose_publication_workflow_recovery(
    *,
    state_table: str,
    state_machine_arn: str,
    release_manifest_fingerprint: str,
    exact_profile: ExactReviewProductProfile,
    eligibility: PublicationProfileEligibility,
    dynamodb: object,
    step_functions: object,
    clock: Callable[[], datetime] | None,
) -> PublicationWorkflowRecovery:
    store = DynamoDBPublicationExecutionStore(
        client=dynamodb,
        table_name=state_table,
    )
    execution = PublicationExecutionService(
        store,
        profiles=_PinnedOperationsProfileAuthority(exact_profile),
        profile_eligibility=PinnedPublicationProfileEligibilityAuthority(eligibility),
        release_manifest_fingerprint=release_manifest_fingerprint,
        clock=clock,
    )
    return PublicationWorkflowRecovery(
        store=store,
        execution=execution,
        step_functions=step_functions,  # type: ignore[arg-type]
        state_machine_arn=state_machine_arn,
        clock=clock,
    )


def compose_publication_retention_handler(
    *,
    state_table: str,
    dynamodb: object,
    clock: Callable[[], datetime] | None = None,
    metric_logger: Callable[[str], object] = print,
) -> Phase715cPublicationRetentionHandler:
    """Join strong terminal identity resolution to marker-last TTL assignment."""

    _require_methods(
        dynamodb,
        ("get_item", "query", "transact_write_items"),
        "Phase 7.15C retention dependency is unavailable",
    )
    resolver = DynamoDBPublicationTerminalIdentityResolver(
        client=dynamodb,  # type: ignore[arg-type]
        table_name=state_table,
    )
    store = DynamoDBPublicationOperationalRetentionStore(
        client=dynamodb,
        table_name=state_table,
    )
    retention = PublicationOperationalRetentionService(store, clock=clock)
    return Phase715cPublicationRetentionHandler(
        resolver=resolver,
        retention=retention,
        metric_logger=metric_logger,
    )


def _require_methods(value: object, methods: tuple[str, ...], message: str) -> None:
    if any(not callable(getattr(value, method, None)) for method in methods):
        raise RuntimeError(message)


__all__ = [
    "compose_publication_recovery_queue_handler",
    "compose_publication_recovery_sweep_handler",
    "compose_publication_retention_handler",
]
