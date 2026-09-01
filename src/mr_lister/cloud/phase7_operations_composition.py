"""Injected-only composition for source-only Phase 7 publication operations.

This module has no default AWS client factory, provider transport, credential resolver, handler
entrypoint, or module-level graph.  Construction validates shapes and joins existing components
without performing I/O; the production template remains impossible to instantiate.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping
from datetime import datetime
from hashlib import md5
from typing import Any, Protocol

from mr_lister.cloud.phase7_configuration import PinnedPublicationProfileAuthority
from mr_lister.publication.execution_dynamodb import DynamoDBPublicationExecutionStore
from mr_lister.publication.execution_service import PublicationExecutionService
from mr_lister.publication.orchestration import PublicationWorkDispatcher
from mr_lister.publication.orchestration_dynamodb import (
    DynamoDBPublicationDueWorkInventory,
    DynamoDBPublicationTerminalIdentityResolver,
)
from mr_lister.publication.orchestration_recovery import (
    PublicationPreDispatchDeadlineEnvelope,
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
from mr_lister.review_profile import ExactReviewProductProfile

from .phase7_operations import (
    Phase7PublicationDispatcherHandler,
    Phase7PublicationRecoveryHandler,
    Phase7PublicationRetentionHandler,
)

_QUEUE_URL = re.compile(
    r"^https://sqs\.[a-z0-9-]+\.amazonaws\.com(?:\.cn)?/"
    r"\d{12}/[A-Za-z0-9_-]{1,80}$"
)
_MESSAGE_ID = re.compile(r"^[A-Za-z0-9_-]{1,80}$")


class _SqsSendMessageClient(Protocol):
    def send_message(self, **request: Any) -> Mapping[str, Any]: ...


class _SqsPublicationDeadlineSettlementSink:
    """Send only the exact encrypted-queue envelope for pre-dispatch expiry."""

    __slots__ = ("_client", "_queue_url")

    def __init__(self, *, client: _SqsSendMessageClient, queue_url: str) -> None:
        if _QUEUE_URL.fullmatch(queue_url) is None:
            raise RuntimeError("Phase 7 recovery queue configuration is invalid")
        self._client = client
        self._queue_url = queue_url

    def send(self, envelope: PublicationPreDispatchDeadlineEnvelope) -> None:
        try:
            exact = PublicationPreDispatchDeadlineEnvelope.model_validate(
                envelope.model_dump(mode="python"),
                strict=True,
            )
            if exact != envelope:
                raise ValueError
            body = json.dumps(
                exact.model_dump(mode="json", exclude={"contract_version"}),
                sort_keys=True,
                separators=(",", ":"),
            )
            response = self._client.send_message(
                QueueUrl=self._queue_url,
                MessageBody=body,
            )
            expected_md5 = md5(body.encode("utf-8"), usedforsecurity=False).hexdigest()
            if (
                not isinstance(response, Mapping)
                or _MESSAGE_ID.fullmatch(str(response.get("MessageId", ""))) is None
                or response.get("MD5OfMessageBody") != expected_md5
            ):
                raise ValueError
        except Exception:
            raise RuntimeError("Phase 7 deadline recovery enqueue failed safely") from None


def compose_publication_dispatcher_handler(
    *,
    state_table: str,
    state_machine_arn: str,
    dynamodb: object,
    step_functions: object,
    sqs: object,
    recovery_queue_url: str,
    clock: Callable[[], datetime] | None = None,
) -> Phase7PublicationDispatcherHandler:
    """Join the exact due GSI to one fixed workflow without a persistence write seam."""

    _require_methods(dynamodb, ("query",), "Phase 7 dispatcher dependency is unavailable")
    _require_methods(
        step_functions,
        ("start_execution", "describe_execution"),
        "Phase 7 dispatcher dependency is unavailable",
    )
    _require_methods(sqs, ("send_message",), "Phase 7 dispatcher dependency is unavailable")
    inventory = DynamoDBPublicationDueWorkInventory(
        client=dynamodb,  # type: ignore[arg-type]
        table_name=state_table,
    )
    dispatcher = PublicationWorkDispatcher(
        locator=inventory,
        step_functions=step_functions,  # type: ignore[arg-type]
        state_machine_arn=state_machine_arn,
        clock=clock,
    )
    deadline_sink = _SqsPublicationDeadlineSettlementSink(
        client=sqs,  # type: ignore[arg-type]
        queue_url=recovery_queue_url,
    )
    return Phase7PublicationDispatcherHandler(
        dispatcher=dispatcher,
        deadline_sink=deadline_sink,
    )


def compose_publication_recovery_handler(
    *,
    state_table: str,
    state_machine_arn: str,
    release_manifest_fingerprint: str,
    exact_profile: ExactReviewProductProfile,
    eligibility: PublicationProfileEligibility,
    dynamodb: object,
    step_functions: object,
    clock: Callable[[], datetime] | None = None,
) -> Phase7PublicationRecoveryHandler:
    """Compose only same-ARN redrive and provider-free deadline settlement authority."""

    _require_methods(
        dynamodb,
        ("get_item", "query", "transact_write_items"),
        "Phase 7 recovery dependency is unavailable",
    )
    _require_methods(
        step_functions,
        ("describe_execution", "redrive_execution"),
        "Phase 7 recovery dependency is unavailable",
    )
    selected_clock = clock
    store = DynamoDBPublicationExecutionStore(
        client=dynamodb,
        table_name=state_table,
    )
    execution = PublicationExecutionService(
        store,
        profiles=PinnedPublicationProfileAuthority(exact_profile),
        profile_eligibility=PinnedPublicationProfileEligibilityAuthority(eligibility),
        release_manifest_fingerprint=release_manifest_fingerprint,
        clock=selected_clock,
    )
    recovery = PublicationWorkflowRecovery(
        store=store,
        execution=execution,
        step_functions=step_functions,  # type: ignore[arg-type]
        state_machine_arn=state_machine_arn,
        clock=selected_clock,
    )
    return Phase7PublicationRecoveryHandler(recovery=recovery)


def compose_publication_retention_handler(
    *,
    state_table: str,
    dynamodb: object,
    clock: Callable[[], datetime] | None = None,
) -> Phase7PublicationRetentionHandler:
    """Join strong terminal identity resolution to the existing marker-last TTL service."""

    _require_methods(
        dynamodb,
        ("get_item", "query", "transact_write_items"),
        "Phase 7 retention dependency is unavailable",
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
    return Phase7PublicationRetentionHandler(
        resolver=resolver,
        retention=retention,
    )


def _require_methods(value: object, methods: tuple[str, ...], message: str) -> None:
    if any(not callable(getattr(value, method, None)) for method in methods):
        raise RuntimeError(message)


__all__ = [
    "compose_publication_dispatcher_handler",
    "compose_publication_recovery_handler",
    "compose_publication_retention_handler",
]
