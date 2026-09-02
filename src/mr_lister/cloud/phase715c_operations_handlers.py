"""Closed Lambda handlers for the provider-free Phase 7.15C operations drill.

Only recovery-queue, recovery-index sweep, and terminal-retention events are accepted.  The
handlers expose identifier-minimal results and never construct a dispatcher, provider, secret,
HTTP transport, seller route, or AWS client.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping
from typing import Any, Protocol

from mr_lister.control.publication_retention import PublicationRetentionCompletionAuthority
from mr_lister.publication.orchestration_dynamodb import PublicationTerminalIdentity
from mr_lister.publication.orchestration_recovery import (
    PublicationPreDispatchDeadlineEnvelope,
    PublicationRecoveryDisposition,
    PublicationRecoveryResult,
    PublicationRecoverySweepResult,
    PublicationWorkflowFailureEnvelope,
)

PUBLICATION_RECOVERY_SWEEP_EVENT = {"kind": "publication_recovery_sweep"}

_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
_MESSAGE_ID = re.compile(r"^[A-Za-z0-9_-]{1,80}$")
_MAX_RECOVERY_BATCH = 25


class Phase715cOperationsError(RuntimeError):
    """Base identifier-free operations-boundary failure."""


class Phase715cOperationsInvocationError(Phase715cOperationsError):
    """An invocation did not match one closed operations contract."""


class Phase715cOperationsExecutionError(Phase715cOperationsError):
    """An injected provider-free operation failed safely."""


class PublicationRecoverySweeperBoundary(Protocol):
    def sweep(self, *, limit: int) -> PublicationRecoverySweepResult: ...


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


class Phase715cPublicationRecoverySweepHandler:
    """Run one exact max-25 recovery-index sweep and emit aggregate counters only."""

    __slots__ = ("_sweeper",)

    def __init__(self, *, sweeper: PublicationRecoverySweeperBoundary) -> None:
        self._sweeper = sweeper

    def __call__(
        self,
        event: Mapping[str, Any],
        context: object | None = None,
    ) -> dict[str, Any]:
        del context
        if not isinstance(event, Mapping) or dict(event) != PUBLICATION_RECOVERY_SWEEP_EVENT:
            raise Phase715cOperationsInvocationError(
                "Phase 7.15C recovery sweep invocation is invalid"
            )
        try:
            result = self._sweeper.sweep(limit=_MAX_RECOVERY_BATCH)
            if not isinstance(result, PublicationRecoverySweepResult):
                raise ValueError
            if (
                result.retry_required_count > 0
                or result.non_redrivable_count > 0
                or result.batch_limit_reached
            ):
                raise RuntimeError
            return {
                "contract_version": "7.0.1",
                "source": "recovery_sweep",
                "candidate_count": result.candidate_count,
                "batch_limit_reached": result.batch_limit_reached,
                "running_count": result.running_count,
                "pending_redrive_count": result.pending_redrive_count,
                "terminal_count": result.terminal_count,
                "stale_hint_count": result.stale_hint_count,
                "redriven_count": result.redriven_count,
                "deadline_settled_count": result.deadline_settled_count,
                "non_redrivable_count": result.non_redrivable_count,
                "retry_required_count": result.retry_required_count,
                "retry_required": False,
            }
        except Exception:
            raise Phase715cOperationsExecutionError(
                "Phase 7.15C recovery sweep failed safely"
            ) from None


class Phase715cPublicationRecoveryQueueHandler:
    """Return only the exact partial-batch response for one recovery-queue message."""

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
        return {"batchItemFailures": ([{"itemIdentifier": message_id}] if failed else [])}

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
            raise Phase715cOperationsInvocationError(
                "Phase 7.15C recovery-queue invocation is invalid"
            ) from None


class Phase715cPublicationRetentionHandler:
    """Strongly rebind a terminal stream key before replay-safe marker-last retention."""

    __slots__ = ("_metric_logger", "_resolver", "_retention")

    def __init__(
        self,
        *,
        resolver: PublicationTerminalIdentityResolver,
        retention: PublicationRetentionBoundary,
        metric_logger: Callable[[str], object] = print,
    ) -> None:
        self._resolver = resolver
        self._retention = retention
        self._metric_logger = metric_logger

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
            if exact.terminal_state == "publication_outcome_unknown":
                self._metric_logger(
                    json.dumps(
                        {"publication_state": exact.terminal_state},
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                )
            return {
                "contract_version": "7.0.1",
                "source": "terminal_stream",
                "retention_assigned": True,
                "publication_row_count": exact.publication_row_count,
                "ttl_assignment_count": exact.ttl_assignment_count,
            }
        except Exception:
            raise Phase715cOperationsExecutionError(
                "Phase 7.15C publication retention failed safely"
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
            return _terminal_publication_stream_key(record)
        except Exception:
            raise Phase715cOperationsInvocationError(
                "Phase 7.15C publication retention invocation is invalid"
            ) from None


def _terminal_publication_stream_key(record: Mapping[str, Any]) -> str:
    dynamodb = record.get("dynamodb")
    if not isinstance(dynamodb, Mapping) or dynamodb.get("StreamViewType") != "KEYS_ONLY":
        raise ValueError
    keys = dynamodb.get("Keys")
    if not isinstance(keys, Mapping) or set(keys) != {"PK", "SK"}:
        raise ValueError
    partition_key = _attribute_string(keys["PK"])
    sort_key = _attribute_string(keys["SK"])
    if not partition_key.startswith("PUBLICATION#") or sort_key != "TERMINAL_JOB_LINK":
        raise ValueError
    aggregate_id = partition_key.removeprefix("PUBLICATION#")
    if _SAFE_ID.fullmatch(aggregate_id) is None:
        raise ValueError
    return aggregate_id


def _attribute_string(value: object) -> str:
    if not isinstance(value, Mapping) or set(value) != {"S"} or not isinstance(value.get("S"), str):
        raise ValueError
    return value["S"]


__all__ = [
    "PUBLICATION_RECOVERY_SWEEP_EVENT",
    "Phase715cOperationsError",
    "Phase715cOperationsExecutionError",
    "Phase715cOperationsInvocationError",
    "Phase715cPublicationRecoveryQueueHandler",
    "Phase715cPublicationRecoverySweepHandler",
    "Phase715cPublicationRetentionHandler",
    "PublicationRecoverySweeperBoundary",
    "PublicationRetentionBoundary",
    "PublicationTerminalIdentityResolver",
    "PublicationWorkflowRecoveryBoundary",
]
