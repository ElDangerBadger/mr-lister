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
    PublicationRecoverySweepResult,
    PublicationWorkflowFailureEnvelope,
)

PUBLICATION_DUE_SWEEP_EVENT = {"kind": "publication_due_sweep"}
PUBLICATION_RECOVERY_SWEEP_EVENT = {"kind": "publication_recovery_sweep"}

_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
_MESSAGE_ID = re.compile(r"^[A-Za-z0-9_-]{1,80}$")
_MAX_DISPATCH_BATCH = 25
_STATE_MACHINE_ARN = re.compile(
    r"^arn:(aws|aws-us-gov|aws-cn):states:[a-z0-9-]+:\d{12}:"
    r"stateMachine:[A-Za-z0-9_-]{1,80}$"
)


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


class PublicationWorkflowRecoverySink(Protocol):
    def send(self, envelope: PublicationWorkflowFailureEnvelope) -> None: ...


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


class Phase7PublicationDispatcherHandler:
    """Treat valid KEYS_ONLY stream records as one wake-up for the bounded due query."""

    __slots__ = ("_deadline_sink", "_dispatcher", "_recovery_sink", "_state_machine_arn")

    def __init__(
        self,
        *,
        dispatcher: PublicationDispatcher,
        deadline_sink: PublicationDeadlineSettlementSink,
        recovery_sink: PublicationWorkflowRecoverySink,
        state_machine_arn: str,
    ) -> None:
        if _STATE_MACHINE_ARN.fullmatch(state_machine_arn) is None:
            raise ValueError("Phase 7 publication state-machine ARN is invalid")
        self._dispatcher = dispatcher
        self._deadline_sink = deadline_sink
        self._recovery_sink = recovery_sink
        self._state_machine_arn = state_machine_arn

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
                        if result.execution_arn is not None or result.recovery_status is not None:
                            raise ValueError
                        self._deadline_sink.send(
                            PublicationPreDispatchDeadlineEnvelope(
                                owner_id=result.owner_id,
                                aggregate_id=result.aggregate_id,
                                work_request_id=result.work_request_id,
                                verification_deadline=result.verification_deadline,
                            )
                        )
                    elif result.disposition is PublicationDispatchDisposition.RECOVERY_REQUIRED:
                        if (
                            not isinstance(result.execution_arn, str)
                            or not result.execution_arn
                            or result.recovery_status not in {"FAILED", "TIMED_OUT", "ABORTED"}
                        ):
                            raise ValueError
                        self._recovery_sink.send(
                            PublicationWorkflowFailureEnvelope(
                                execution_arn=result.execution_arn,
                                machine_arn=self._state_machine_arn,
                                status=result.recovery_status,
                            )
                        )
                    elif result.disposition is PublicationDispatchDisposition.RETRY_REQUIRED:
                        if result.execution_arn is not None or result.recovery_status is not None:
                            raise ValueError
                        retry_required = True
                    elif (
                        not isinstance(result.execution_arn, str)
                        or not result.execution_arn
                        or result.recovery_status is not None
                    ):
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
                "recovery_required_count": counts[PublicationDispatchDisposition.RECOVERY_REQUIRED],
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


class Phase7PublicationRecoverySweepHandler:
    """Run one exact scheduled, bounded recovery-index sweep and return counters only."""

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
            raise Phase7OperationsInvocationError(
                "Phase 7 publication recovery sweep invocation is invalid"
            )
        try:
            result = self._sweeper.sweep(limit=_MAX_DISPATCH_BATCH)
            if not isinstance(result, PublicationRecoverySweepResult):
                raise ValueError
            if (
                result.retry_required_count > 0
                or result.non_redrivable_count > 0
                or result.batch_limit_reached
            ):
                # EventBridge discards successful return values.  A failed invocation is the
                # durable alarm/DLQ signal for poison, dependency, non-redrivable work, or
                # first-page saturation.  Without durable continuation authority, saturation is
                # an operator stop rather than a claim that later index rows were inspected.
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
            raise Phase7OperationsExecutionError(
                "Phase 7 publication recovery sweep failed safely"
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
    "PUBLICATION_RECOVERY_SWEEP_EVENT",
    "Phase7OperationsError",
    "Phase7OperationsExecutionError",
    "Phase7OperationsInvocationError",
    "Phase7PublicationDispatcherHandler",
    "Phase7PublicationRecoveryHandler",
    "Phase7PublicationRecoverySweepHandler",
    "Phase7PublicationRetentionHandler",
    "PublicationDeadlineSettlementSink",
    "PublicationDispatcher",
    "PublicationRecoverySweeperBoundary",
    "PublicationRetentionBoundary",
    "PublicationTerminalIdentityResolver",
    "PublicationWorkflowRecoveryBoundary",
    "PublicationWorkflowRecoverySink",
    "build_disabled_phase7_operations_handler",
]
