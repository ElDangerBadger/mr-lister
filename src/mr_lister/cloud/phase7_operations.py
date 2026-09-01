"""Strict source-only Lambda boundaries for the Phase 7 publication control plane.

These handlers accept only identifier-minimal DynamoDB, EventBridge, and SQS envelopes.  They are
not registered by an entrypoint or template.  Their collaborators are injected, and every public
result is either counter-only or the exact SQS partial-batch response shape.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping
from typing import Any, Protocol

from mr_lister.control.publication_retention import PublicationRetentionCompletionAuthority
from mr_lister.publication.application import PublicationRuntimeActivation
from mr_lister.publication.orchestration import (
    PublicationDispatchDisposition,
    PublicationDispatchResult,
)
from mr_lister.publication.orchestration_dynamodb import PublicationTerminalIdentity
from mr_lister.publication.orchestration_recovery import (
    PublicationPreDispatchDeadlineEnvelope,
    PublicationRecoveryDisposition,
    PublicationRecoveryResult,
    PublicationWorkflowFailureEnvelope,
)

PUBLICATION_DUE_SWEEP_EVENT = {"kind": "publication_due_sweep"}

_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
_MESSAGE_ID = re.compile(r"^[A-Za-z0-9_-]{1,80}$")
_MAX_DISPATCH_BATCH = 25


class Phase7OperationsError(RuntimeError):
    """Base identifier-free operations-boundary failure."""


class Phase7OperationsInvocationError(Phase7OperationsError):
    """An event did not match one closed source-only invocation contract."""


class Phase7OperationsExecutionError(Phase7OperationsError):
    """An injected control-plane operation failed safely."""


class PublicationDispatcher(Protocol):
    def dispatch_due(self, *, limit: int) -> tuple[PublicationDispatchResult, ...]: ...


class PublicationDeadlineSettlementSink(Protocol):
    def send(self, envelope: PublicationPreDispatchDeadlineEnvelope) -> None: ...


class PublicationWorkflowRecoveryBoundary(Protocol):
    def recover(
        self,
        envelope: PublicationWorkflowFailureEnvelope,
    ) -> PublicationRecoveryResult: ...

    def settle_pre_dispatch_deadline(
        self,
        envelope: PublicationPreDispatchDeadlineEnvelope,
    ) -> PublicationRecoveryResult: ...


class PublicationTerminalIdentityResolver(Protocol):
    def resolve_terminal_identity(self, aggregate_id: str) -> PublicationTerminalIdentity: ...


class PublicationRetentionBoundary(Protocol):
    def assign(
        self,
        *,
        owner_id: str,
        aggregate_id: str,
    ) -> PublicationRetentionCompletionAuthority: ...


class Phase7PublicationDispatcherHandler:
    """Treat valid KEYS_ONLY stream records as one wake-up for the bounded due query."""

    __slots__ = ("_deadline_sink", "_dispatcher")

    def __init__(
        self,
        *,
        dispatcher: PublicationDispatcher,
        deadline_sink: PublicationDeadlineSettlementSink,
    ) -> None:
        self._dispatcher = dispatcher
        self._deadline_sink = deadline_sink

    def __call__(
        self,
        event: Mapping[str, Any],
        context: object | None = None,
    ) -> dict[str, Any]:
        del context
        try:
            source = self._source(event)
            results = self._dispatcher.dispatch_due(limit=_MAX_DISPATCH_BATCH)
            if not isinstance(results, tuple) or len(results) > _MAX_DISPATCH_BATCH:
                raise ValueError
            counts = {disposition: 0 for disposition in PublicationDispatchDisposition}
            retry_required = False
            for result in results:
                try:
                    if not isinstance(result, PublicationDispatchResult):
                        raise ValueError
                    if result.disposition is PublicationDispatchDisposition.DEADLINE_EXPIRED:
                        if result.execution_arn is not None:
                            raise ValueError
                        self._deadline_sink.send(
                            PublicationPreDispatchDeadlineEnvelope(
                                owner_id=result.owner_id,
                                aggregate_id=result.aggregate_id,
                                work_request_id=result.work_request_id,
                                verification_deadline=result.verification_deadline,
                            )
                        )
                    elif result.disposition is PublicationDispatchDisposition.RETRY_REQUIRED:
                        if result.execution_arn is not None:
                            raise ValueError
                        retry_required = True
                    elif not isinstance(result.execution_arn, str) or not result.execution_arn:
                        raise ValueError
                    counts[result.disposition] += 1
                except Exception:
                    retry_required = True
            if retry_required:
                raise RuntimeError
            return {
                "contract_version": "7.0.1",
                "source": source,
                "candidate_count": len(results),
                "started_count": counts[PublicationDispatchDisposition.STARTED],
                "confirmed_existing_count": counts[
                    PublicationDispatchDisposition.CONFIRMED_EXISTING
                ],
                "deadline_expired_count": counts[PublicationDispatchDisposition.DEADLINE_EXPIRED],
            }
        except Phase7OperationsInvocationError:
            raise
        except Exception:
            raise Phase7OperationsExecutionError(
                "Phase 7 publication dispatch failed safely"
            ) from None

    @staticmethod
    def _source(event: Mapping[str, Any]) -> str:
        if not isinstance(event, Mapping):
            raise Phase7OperationsInvocationError(
                "Phase 7 publication dispatcher invocation is invalid"
            )
        if dict(event) == PUBLICATION_DUE_SWEEP_EVENT:
            return "due_sweep"
        try:
            if set(event) != {"Records"}:
                raise ValueError
            records = event["Records"]
            if not isinstance(records, list) or not 1 <= len(records) <= _MAX_DISPATCH_BATCH:
                raise ValueError
            event_ids: set[str] = set()
            for record in records:
                if not isinstance(record, Mapping):
                    raise ValueError
                event_id = record.get("eventID")
                if (
                    not isinstance(event_id, str)
                    or not event_id
                    or event_id in event_ids
                    or record.get("eventName") not in {"INSERT", "MODIFY"}
                    or record.get("eventSource") != "aws:dynamodb"
                ):
                    raise ValueError
                event_ids.add(event_id)
                _publication_stream_key(record, work=True)
            return "dynamodb_stream"
        except Exception:
            raise Phase7OperationsInvocationError(
                "Phase 7 publication dispatcher invocation is invalid"
            ) from None


class Phase7PublicationRecoveryHandler:
    """Return only the SQS partial-batch response for one exact recovery envelope."""

    __slots__ = ("_recovery",)

    def __init__(self, *, recovery: PublicationWorkflowRecoveryBoundary) -> None:
        self._recovery = recovery

    def __call__(
        self,
        event: Mapping[str, Any],
        context: object | None = None,
    ) -> dict[str, list[dict[str, str]]]:
        del context
        message_id, envelope = self._record(event)
        failed = False
        try:
            if isinstance(envelope, PublicationWorkflowFailureEnvelope):
                result = self._recovery.recover(envelope)
            else:
                result = self._recovery.settle_pre_dispatch_deadline(envelope)
            if not isinstance(result, PublicationRecoveryResult):
                raise ValueError
            failed = result.disposition is PublicationRecoveryDisposition.NON_REDRIVABLE
        except Exception:
            failed = True
        return {
            "batchItemFailures": ([{"itemIdentifier": message_id}] if failed else []),
        }

    @staticmethod
    def _record(
        event: Mapping[str, Any],
    ) -> tuple[
        str,
        PublicationWorkflowFailureEnvelope | PublicationPreDispatchDeadlineEnvelope,
    ]:
        try:
            if not isinstance(event, Mapping) or set(event) != {"Records"}:
                raise ValueError
            records = event["Records"]
            if not isinstance(records, list) or len(records) != 1:
                raise ValueError
            record = records[0]
            if not isinstance(record, Mapping) or record.get("eventSource") != "aws:sqs":
                raise ValueError
            message_id = record.get("messageId")
            body = record.get("body")
            if (
                not isinstance(message_id, str)
                or _MESSAGE_ID.fullmatch(message_id) is None
                or not isinstance(body, str)
            ):
                raise ValueError
            decoded = json.loads(body)
            if not isinstance(decoded, dict):
                raise ValueError
            if set(decoded) == {"execution_arn", "machine_arn", "status"}:
                envelope: (
                    PublicationWorkflowFailureEnvelope | PublicationPreDispatchDeadlineEnvelope
                ) = PublicationWorkflowFailureEnvelope.model_validate_json(body, strict=True)
            elif set(decoded) == {
                "kind",
                "owner_id",
                "aggregate_id",
                "work_request_id",
                "verification_deadline",
            }:
                envelope = PublicationPreDispatchDeadlineEnvelope.model_validate_json(
                    body,
                    strict=True,
                )
            else:
                raise ValueError
            return message_id, envelope
        except Exception:
            raise Phase7OperationsInvocationError(
                "Phase 7 publication recovery invocation is invalid"
            ) from None


class Phase7PublicationRetentionHandler:
    """Strongly resolve a terminal stream key before replay-safe TTL assignment."""

    __slots__ = ("_resolver", "_retention")

    def __init__(
        self,
        *,
        resolver: PublicationTerminalIdentityResolver,
        retention: PublicationRetentionBoundary,
    ) -> None:
        self._resolver = resolver
        self._retention = retention

    def __call__(
        self,
        event: Mapping[str, Any],
        context: object | None = None,
    ) -> dict[str, Any]:
        del context
        aggregate_id = self._aggregate_id(event)
        try:
            identity = self._resolver.resolve_terminal_identity(aggregate_id)
            if (
                not isinstance(identity, PublicationTerminalIdentity)
                or identity.aggregate_id != aggregate_id
            ):
                raise ValueError
            completion = self._retention.assign(
                owner_id=identity.owner_id,
                aggregate_id=identity.aggregate_id,
            )
            exact = PublicationRetentionCompletionAuthority.model_validate(
                completion.model_dump(mode="python"),
                strict=True,
            )
            if exact != completion or exact.aggregate_id != aggregate_id:
                raise ValueError
            return {
                "contract_version": "7.0.1",
                "source": "terminal_stream",
                "retention_assigned": True,
                "publication_row_count": exact.publication_row_count,
                "ttl_assignment_count": exact.ttl_assignment_count,
            }
        except Exception:
            raise Phase7OperationsExecutionError(
                "Phase 7 publication retention failed safely"
            ) from None

    @staticmethod
    def _aggregate_id(event: Mapping[str, Any]) -> str:
        try:
            if not isinstance(event, Mapping) or set(event) != {"Records"}:
                raise ValueError
            records = event["Records"]
            if not isinstance(records, list) or len(records) != 1:
                raise ValueError
            record = records[0]
            if (
                not isinstance(record, Mapping)
                or record.get("eventName") != "INSERT"
                or record.get("eventSource") != "aws:dynamodb"
            ):
                raise ValueError
            return _publication_stream_key(record, work=False)
        except Exception:
            raise Phase7OperationsInvocationError(
                "Phase 7 publication retention invocation is invalid"
            ) from None


class _DisabledPhase7OperationsHandler:
    """Deny before reading an invocation or constructing any injected control-plane graph."""

    __slots__ = ("_activation", "_builder")

    def __init__(
        self,
        *,
        activation: PublicationRuntimeActivation,
        builder: Callable[[], object],
    ) -> None:
        self._activation = PublicationRuntimeActivation.model_validate(
            activation.model_dump(mode="python"),
            strict=True,
        )
        self._builder = builder

    def __call__(
        self,
        event: Mapping[str, Any],
        context: object | None = None,
    ) -> dict[str, Any]:
        del event, context
        self._activation.deny_runtime()


def build_disabled_phase7_operations_handler(
    activation: PublicationRuntimeActivation,
    *,
    builder: Callable[[], object],
) -> Callable[[Mapping[str, Any], object | None], dict[str, Any]]:
    """Build only a frozen refusal wrapper; the operation graph remains untouched."""

    return _DisabledPhase7OperationsHandler(activation=activation, builder=builder)


def _publication_stream_key(record: Mapping[str, Any], *, work: bool) -> str:
    dynamodb = record.get("dynamodb")
    if not isinstance(dynamodb, Mapping) or dynamodb.get("StreamViewType") != "KEYS_ONLY":
        raise ValueError
    keys = dynamodb.get("Keys")
    if not isinstance(keys, Mapping) or set(keys) != {"PK", "SK"}:
        raise ValueError
    partition_key = _attribute_string(keys["PK"])
    sort_key = _attribute_string(keys["SK"])
    if not partition_key.startswith("PUBLICATION#"):
        raise ValueError
    aggregate_id = partition_key.removeprefix("PUBLICATION#")
    if _SAFE_ID.fullmatch(aggregate_id) is None:
        raise ValueError
    if work:
        if not sort_key.startswith("PUBLICATION_WORK#"):
            raise ValueError
        work_id = sort_key.removeprefix("PUBLICATION_WORK#")
        if _SAFE_ID.fullmatch(work_id) is None:
            raise ValueError
    elif sort_key != "TERMINAL_JOB_LINK":
        raise ValueError
    return aggregate_id


def _attribute_string(value: object) -> str:
    if not isinstance(value, Mapping) or set(value) != {"S"} or not isinstance(value.get("S"), str):
        raise ValueError
    return value["S"]


__all__ = [
    "PUBLICATION_DUE_SWEEP_EVENT",
    "Phase7OperationsError",
    "Phase7OperationsExecutionError",
    "Phase7OperationsInvocationError",
    "Phase7PublicationDispatcherHandler",
    "Phase7PublicationRecoveryHandler",
    "Phase7PublicationRetentionHandler",
    "PublicationDeadlineSettlementSink",
    "PublicationDispatcher",
    "PublicationRetentionBoundary",
    "PublicationTerminalIdentityResolver",
    "PublicationWorkflowRecoveryBoundary",
    "build_disabled_phase7_operations_handler",
]
