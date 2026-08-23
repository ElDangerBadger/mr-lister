"""Closed Lambda boundaries for Phase 6 asynchronous machine work.

Step Functions carries only opaque job/work identities.  These handlers never trust a
worker response as state authority: each operation re-reads application-owned records and
all settlement decisions are made from the durable job, work request, attempt, and permit
graph.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from mr_lister.control.agentcore import AgentCorePreparationBridge
from mr_lister.control.commands import (
    RecordWorkerFailureCommand,
    SettleCancellationCommand,
    WorkerFailureCode,
)
from mr_lister.control.errors import (
    ConcurrentControlModificationError,
    WorkNotActiveError,
)
from mr_lister.control.models import (
    CommandResponse,
    ControlJobRecord,
    ControlJobState,
    WorkRequest,
    WorkRequestStatus,
    WorkType,
)
from mr_lister.control.service import SellerControlService
from mr_lister.control.settlement import (
    PreparationFailureReconciler,
    PreparationSettlementError,
)
from mr_lister.production.phase6_worker import Phase6ProductMachineWorker

_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
_JOB_KEY = re.compile(r"^JOB#(?P<identifier>[A-Za-z0-9][A-Za-z0-9_-]{0,127})$")
_WORK_KEY = re.compile(r"^WORK#(?P<identifier>[A-Za-z0-9][A-Za-z0-9_-]{0,127})$")
_MAX_STREAM_RECORDS = 100
_MAX_SETTLEMENT_RECHECKS = 3
_DUE_SWEEP_SOURCE = "due-work-sweeper"

ProviderOperation = Literal[
    "synchronize_product",
    "reconcile_product",
    "refresh_economics",
]


class Phase6MachineInvocationError(RuntimeError):
    """The orchestration event is outside the closed Phase 6 machine contract."""


class Phase6MachineExecutionError(RuntimeError):
    """One value-free error emitted when a dependency cannot complete machine work."""


class DispatchBoundary(Protocol):
    def dispatch_due(self, *, limit: int = 25) -> tuple[WorkRequest, ...]: ...

    def dispatch_one(self, job_id: str, work_request_id: str) -> WorkRequest | None: ...


class MachineAuthorityStore(Protocol):
    def get_job(self, job_id: str) -> ControlJobRecord: ...

    def get_work_request(self, job_id: str, work_request_id: str) -> WorkRequest: ...


class Phase6DispatcherHandler:
    """Lambda-callable dispatcher boundary with no worker or provider capability."""

    __slots__ = ("_dispatcher",)

    def __init__(self, *, dispatcher: DispatchBoundary) -> None:
        self._dispatcher = dispatcher

    def __call__(
        self,
        event: Mapping[str, Any],
        _context: object | None = None,
    ) -> dict[str, int]:
        try:
            if _is_due_sweep(event):
                dispatched = self._dispatcher.dispatch_due(limit=25)
                return {"attempted": len(dispatched), "dispatched": len(dispatched)}
            identities = _stream_work_identities(event)
            dispatched_count = 0
            for job_id, work_request_id in identities:
                if self._dispatcher.dispatch_one(job_id, work_request_id) is not None:
                    dispatched_count += 1
            return {"attempted": len(identities), "dispatched": dispatched_count}
        except Phase6MachineInvocationError:
            raise
        except Exception:
            raise Phase6MachineExecutionError("Phase 6 dispatch failed safely") from None


class Phase6PreparationHandler:
    """Lambda-callable AgentCore bridge with read-only durable authority."""

    __slots__ = ("_preparation",)

    def __init__(self, *, preparation: AgentCorePreparationBridge) -> None:
        self._preparation = preparation

    def __call__(
        self,
        event: Mapping[str, Any],
        _context: object | None = None,
    ) -> dict[str, Any]:
        job_id, work_request_id = _machine_identity(event, allowed_extra=frozenset())
        try:
            result = self._preparation.invoke(
                job_id=job_id,
                work_request_id=work_request_id,
            )
            return result.model_dump(mode="json")
        except Exception:
            raise Phase6MachineExecutionError("Phase 6 preparation failed safely") from None


class Phase6ProviderHandler:
    """Lambda-callable draft worker with an exact three-operation allowlist."""

    __slots__ = ("_provider",)

    def __init__(self, *, provider: Phase6ProductMachineWorker) -> None:
        self._provider = provider

    def __call__(
        self,
        event: Mapping[str, Any],
        _context: object | None = None,
    ) -> dict[str, Any]:
        job_id, work_request_id = _machine_identity(
            event,
            allowed_extra=frozenset({"operation"}),
        )
        operation = event.get("operation")
        if operation not in {
            "synchronize_product",
            "reconcile_product",
            "refresh_economics",
        }:
            raise Phase6MachineInvocationError("Unsupported Phase 6 provider operation")
        try:
            if operation == "synchronize_product":
                response = self._provider.run_product_sync(
                    job_id=job_id,
                    work_request_id=work_request_id,
                )
            elif operation == "reconcile_product":
                response = self._provider.run_product_reconciliation(
                    job_id=job_id,
                    work_request_id=work_request_id,
                )
            else:
                response = self._provider.run_economics_refresh(
                    job_id=job_id,
                    work_request_id=work_request_id,
                )
            return CommandResponse.model_validate(response).model_dump(mode="json")
        except Exception:
            raise Phase6MachineExecutionError("Phase 6 provider work failed safely") from None


class Phase6SettlementHandler:
    """Lambda-callable strong-read settlement with no provider or AgentCore client."""

    __slots__ = ("_control", "_preparation_settlement", "_store")

    def __init__(
        self,
        *,
        store: MachineAuthorityStore,
        control: SellerControlService,
        preparation_settlement: PreparationFailureReconciler,
    ) -> None:
        self._store = store
        self._control = control
        self._preparation_settlement = preparation_settlement

    def __call__(
        self,
        event: Mapping[str, Any],
        _context: object | None = None,
    ) -> dict[str, Any]:
        job_id, work_request_id, work_type = _settlement_identity(event)
        try:
            for _ in range(_MAX_SETTLEMENT_RECHECKS):
                job = self._store.get_job(job_id)
                work = self._store.get_work_request(job_id, work_request_id)
                _require_settlement_binding(job, work, work_type, job_id, work_request_id)

                if work.status is WorkRequestStatus.COMPLETED:
                    if work_type is WorkType.PREPARE and not _is_settled_cancellation(job):
                        try:
                            result = self._preparation_settlement.settle_unavailable(
                                job_id=job_id,
                                work_request_id=work_request_id,
                            )
                        except PreparationSettlementError:
                            # Job and Work are separate strong reads. Re-read when a seller or
                            # worker won between them and made this snapshot internally stale.
                            continue
                        return result.response.model_dump(mode="json")
                    return _authority_response(job).model_dump(mode="json")

                if job.active_work_request_id != work_request_id or work.status not in {
                    WorkRequestStatus.CLAIMED,
                    WorkRequestStatus.DISPATCHED,
                }:
                    raise Phase6MachineInvocationError(
                        "Phase 6 settlement does not match active durable work"
                    )
                try:
                    if job.cancellation_requested_at is not None:
                        response = self._control.settle_cancellation(
                            SettleCancellationCommand(
                                job_id=job_id,
                                work_request_id=work_request_id,
                                expected_record_version=job.record_version,
                            )
                        )
                    elif work_type is WorkType.PREPARE:
                        result = self._preparation_settlement.settle_unavailable(
                            job_id=job_id,
                            work_request_id=work_request_id,
                        )
                        return result.response.model_dump(mode="json")
                    else:
                        response = self._control.record_worker_failure(
                            RecordWorkerFailureCommand(
                                job_id=job_id,
                                work_request_id=work_request_id,
                                expected_record_version=job.record_version,
                                code=_failure_code(work_type),
                            )
                        )
                except (
                    ConcurrentControlModificationError,
                    PreparationSettlementError,
                    WorkNotActiveError,
                ):
                    # A worker completion or seller cancellation may win after the strong read.
                    # Reconcile from the next durable snapshot instead of leaking a CAS race to
                    # Step Functions, whose retry policy intentionally covers service failures
                    # only.
                    continue
                return CommandResponse.model_validate(response).model_dump(mode="json")
            raise Phase6MachineExecutionError("Phase 6 settlement kept changing safely")
        except Phase6MachineExecutionError:
            raise
        except Phase6MachineInvocationError:
            raise
        except Exception:
            raise Phase6MachineExecutionError("Phase 6 settlement failed safely") from None


@dataclass(frozen=True, slots=True)
class Phase6MachineHandlers:
    """Role-separated callables for dispatcher, preparation, provider, and settlement."""

    dispatcher: DispatchBoundary
    preparation: AgentCorePreparationBridge
    provider: Phase6ProductMachineWorker
    store: MachineAuthorityStore
    control: SellerControlService
    preparation_settlement: PreparationFailureReconciler

    def dispatch(self, event: Mapping[str, Any], _context: object | None = None) -> dict[str, int]:
        """Dispatch an exact stream work row or run the bounded due-work backup sweep."""

        return Phase6DispatcherHandler(dispatcher=self.dispatcher)(event, _context)

    def prepare(self, event: Mapping[str, Any], _context: object | None = None) -> dict[str, Any]:
        """Invoke only the configured Strands preparation runtime for exact PREPARE work."""

        return Phase6PreparationHandler(preparation=self.preparation)(event, _context)

    def run_provider(
        self,
        event: Mapping[str, Any],
        _context: object | None = None,
    ) -> dict[str, Any]:
        """Run one allowlisted provider operation; no operation name is inferred."""

        return Phase6ProviderHandler(provider=self.provider)(event, _context)

    def settle(self, event: Mapping[str, Any], _context: object | None = None) -> dict[str, Any]:
        """Settle from strong durable authority, never from the worker payload or error text."""

        return Phase6SettlementHandler(
            store=self.store,
            control=self.control,
            preparation_settlement=self.preparation_settlement,
        )(event, _context)


def _is_due_sweep(event: Mapping[str, Any]) -> bool:
    return set(event) == {"source"} and event.get("source") == _DUE_SWEEP_SOURCE


def _stream_work_identities(event: Mapping[str, Any]) -> tuple[tuple[str, str], ...]:
    if set(event) != {"Records"}:
        raise Phase6MachineInvocationError("Invalid Phase 6 dispatcher event")
    records = event.get("Records")
    if (
        not isinstance(records, Sequence)
        or isinstance(records, (str, bytes, bytearray))
        or not 1 <= len(records) <= _MAX_STREAM_RECORDS
    ):
        raise Phase6MachineInvocationError("Invalid Phase 6 dispatcher batch")
    identities: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for raw_record in records:
        if not isinstance(raw_record, Mapping):
            raise Phase6MachineInvocationError("Invalid Phase 6 dispatcher record")
        if raw_record.get("eventSource") != "aws:dynamodb" or raw_record.get("eventName") not in {
            "INSERT",
            "MODIFY",
        }:
            raise Phase6MachineInvocationError("Invalid Phase 6 dispatcher record")
        dynamodb = raw_record.get("dynamodb")
        keys = dynamodb.get("Keys") if isinstance(dynamodb, Mapping) else None
        if not isinstance(keys, Mapping) or set(keys) != {"PK", "SK"}:
            raise Phase6MachineInvocationError("Invalid Phase 6 dispatcher keys")
        partition = _dynamodb_string(keys.get("PK"))
        sort = _dynamodb_string(keys.get("SK"))
        job_match = _JOB_KEY.fullmatch(partition)
        work_match = _WORK_KEY.fullmatch(sort)
        if job_match is None or work_match is None:
            raise Phase6MachineInvocationError("Invalid Phase 6 dispatcher identity")
        identity = (job_match.group("identifier"), work_match.group("identifier"))
        if identity not in seen:
            seen.add(identity)
            identities.append(identity)
    return tuple(identities)


def _dynamodb_string(value: object) -> str:
    if not isinstance(value, Mapping) or set(value) != {"S"}:
        raise Phase6MachineInvocationError("Invalid Phase 6 dispatcher key value")
    text = value.get("S")
    if not isinstance(text, str):
        raise Phase6MachineInvocationError("Invalid Phase 6 dispatcher key value")
    return text


def _machine_identity(
    event: Mapping[str, Any],
    *,
    allowed_extra: frozenset[str],
) -> tuple[str, str]:
    if not isinstance(event, Mapping) or set(event) != {
        "job_id",
        "work_request_id",
        *allowed_extra,
    }:
        raise Phase6MachineInvocationError("Invalid Phase 6 machine invocation")
    job_id = event.get("job_id")
    work_request_id = event.get("work_request_id")
    if (
        not isinstance(job_id, str)
        or _SAFE_ID.fullmatch(job_id) is None
        or not isinstance(work_request_id, str)
        or _SAFE_ID.fullmatch(work_request_id) is None
    ):
        raise Phase6MachineInvocationError("Invalid Phase 6 machine identity")
    return job_id, work_request_id


def _settlement_identity(event: Mapping[str, Any]) -> tuple[str, str, WorkType]:
    if not isinstance(event, Mapping):
        raise Phase6MachineInvocationError("Invalid Phase 6 settlement invocation")
    terminal_fields = {"worker_result", "worker_error_code"}.intersection(event)
    if len(terminal_fields) != 1:
        raise Phase6MachineInvocationError("Phase 6 settlement requires one terminal signal")
    terminal_field = next(iter(terminal_fields))
    if set(event) != {"job_id", "work_request_id", "work_type", terminal_field}:
        raise Phase6MachineInvocationError("Invalid Phase 6 settlement invocation")
    job_id, work_request_id = _machine_identity(
        {
            "job_id": event.get("job_id"),
            "work_request_id": event.get("work_request_id"),
        },
        allowed_extra=frozenset(),
    )
    work_type_raw = event.get("work_type")
    try:
        work_type = WorkType(work_type_raw)
    except (TypeError, ValueError):
        raise Phase6MachineInvocationError("Invalid Phase 6 settlement work type") from None
    if work_type not in {
        WorkType.PREPARE,
        WorkType.SYNCHRONIZE_PRODUCT,
        WorkType.RECONCILE_PRODUCT,
        WorkType.REFRESH_ECONOMICS,
    }:
        raise Phase6MachineInvocationError("Invalid Phase 6 settlement work type")
    if terminal_field == "worker_result":
        if not isinstance(event.get(terminal_field), Mapping):
            raise Phase6MachineInvocationError("Invalid Phase 6 worker result signal")
    else:
        error_code = event.get(terminal_field)
        if not isinstance(error_code, str) or not 1 <= len(error_code) <= 256:
            raise Phase6MachineInvocationError("Invalid Phase 6 worker error signal")
    return job_id, work_request_id, work_type


def _require_settlement_binding(
    job: ControlJobRecord,
    work: WorkRequest,
    work_type: WorkType,
    job_id: str,
    work_request_id: str,
) -> None:
    if (
        job.job_id != job_id
        or work.job_id != job_id
        or work.work_request_id != work_request_id
        or work.owner_id != job.owner_id
        or work.work_type is not work_type
    ):
        raise Phase6MachineInvocationError("Phase 6 settlement authority is inconsistent")


def _failure_code(work_type: WorkType) -> WorkerFailureCode:
    if work_type is WorkType.REFRESH_ECONOMICS:
        return WorkerFailureCode.ECONOMICS_UNAVAILABLE
    if work_type in {WorkType.SYNCHRONIZE_PRODUCT, WorkType.RECONCILE_PRODUCT}:
        return WorkerFailureCode.PRODUCTION_UNAVAILABLE
    raise Phase6MachineInvocationError("Unsupported Phase 6 settlement work type")


def _authority_response(job: ControlJobRecord) -> CommandResponse:
    return CommandResponse(
        job_id=job.job_id,
        state=job.state,
        record_version=job.record_version,
        review_version=job.review_version,
        work_request_id=job.active_work_request_id,
    )


def _is_settled_cancellation(job: ControlJobRecord) -> bool:
    return (
        job.state is ControlJobState.CANCELLED
        and job.cancellation_requested_at is not None
        and job.active_work_request_id is None
    )


__all__ = [
    "DispatchBoundary",
    "MachineAuthorityStore",
    "Phase6DispatcherHandler",
    "Phase6MachineExecutionError",
    "Phase6MachineHandlers",
    "Phase6MachineInvocationError",
    "Phase6PreparationHandler",
    "Phase6ProviderHandler",
    "Phase6SettlementHandler",
    "ProviderOperation",
]
