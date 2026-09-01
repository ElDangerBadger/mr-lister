"""Read-only DynamoDB boundaries for Phase 7 publication orchestration.

The due-work inventory can query only the frozen due-work GSI.  The terminal identity
resolver performs one strong root read for a stream-selected aggregate.  Neither adapter
constructs an SDK client or exposes a persistence mutation.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from math import ceil
from typing import Any, Protocol

from mr_lister.publication.contract import PublicationState
from mr_lister.publication.execution_models import (
    ExecutionPublicationAggregate,
    PublicationExecutionWorkStatus,
)
from mr_lister.publication.models import PublicationWorkRequest
from mr_lister.publication.orchestration import (
    PublicationDispatchCandidate,
    PublicationDispatchConfigurationError,
    PublicationDispatchDependencyUnavailableError,
)

PUBLICATION_DUE_WORK_INDEX = "DueWorkIndex"
PUBLICATION_DUE_WORK_PARTITION = "PUBLICATION_WORK_DUE#0"
MAX_PUBLICATION_DUE_WORK_BATCH = 25

_TABLE_NAME = re.compile(r"^[A-Za-z0-9_.-]{3,255}$")
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
_MAX_EPOCH_SECOND = 253402300799
_TERMINAL_STATES = frozenset(
    {
        PublicationState.PUBLISHED,
        PublicationState.PUBLICATION_FAILED,
        PublicationState.PUBLICATION_OUTCOME_UNKNOWN,
    }
)


class PublicationOrchestrationBoundaryInvalidError(PublicationDispatchConfigurationError):
    """A DynamoDB read returned data outside the closed orchestration contract."""


class PublicationOrchestrationDependencyUnavailableError(
    PublicationDispatchDependencyUnavailableError
):
    """A required read-only DynamoDB operation was unavailable."""


class DynamoDBQueryClient(Protocol):
    def query(self, **request: Any) -> Mapping[str, Any]: ...


class DynamoDBGetItemClient(Protocol):
    def get_item(self, **request: Any) -> Mapping[str, Any]: ...


@dataclass(frozen=True, slots=True)
class PublicationTerminalIdentity:
    """Minimum stream-recovery identity proven by the strong terminal root read."""

    aggregate_id: str
    owner_id: str


def _s(value: str) -> dict[str, str]:
    return {"S": value}


def _n(value: int) -> dict[str, str]:
    return {"N": str(value)}


def _validated_table_name(table_name: str) -> str:
    if not isinstance(table_name, str) or _TABLE_NAME.fullmatch(table_name) is None:
        raise ValueError("Publication orchestration table name is invalid")
    return table_name


def _utc_epoch_second(value: datetime) -> int:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError("Publication due-work cutoff must be UTC-aware")
    epoch_second = int(value.timestamp())
    if not 0 <= epoch_second <= _MAX_EPOCH_SECOND:
        raise ValueError("Publication due-work cutoff is outside the supported epoch")
    return epoch_second


def _av_string(item: Mapping[str, Any], field: str) -> str:
    raw = item.get(field)
    if not isinstance(raw, dict) or set(raw) != {"S"} or not isinstance(raw.get("S"), str):
        raise PublicationOrchestrationBoundaryInvalidError(
            "Publication orchestration attribute value is invalid"
        )
    return raw["S"]


def _work_item(work: PublicationWorkRequest) -> dict[str, dict[str, str]]:
    return {
        "PK": _s(f"PUBLICATION#{work.aggregate_id}"),
        "SK": _s(f"PUBLICATION_WORK#{work.work_request_id}"),
        "entity_type": _s("PUBLICATION_WORK_REQUEST"),
        "contract_version": _s(work.contract_version),
        "payload": _s(work.model_dump_json()),
        "work_status": _s(work.status.value),
        "work_request_id": _s(work.work_request_id),
        "dispatch_pk": _s(PUBLICATION_DUE_WORK_PARTITION),
        "dispatch_sk": _s(f"{int(work.next_dispatch_at.timestamp()):020d}#{work.work_request_id}"),
    }


def _terminal_item(aggregate: ExecutionPublicationAggregate) -> dict[str, dict[str, str]]:
    return {
        "PK": _s(f"PUBLICATION#{aggregate.aggregate_id}"),
        "SK": _s("META"),
        "entity_type": _s("PUBLICATION_EXECUTION_AGGREGATE"),
        "contract_version": _s(aggregate.contract_version),
        "payload": _s(aggregate.model_dump_json()),
        "owner_id": _s(aggregate.owner_id),
        "job_id": _s(aggregate.job_id),
        "publication_state": _s(aggregate.state.value),
        "record_version": _n(aggregate.record_version),
        "provider_audit_record_version": _n(aggregate.provider_audit_record_version),
        "provider_evidence_record_version": _n(aggregate.provider_evidence_record_version),
    }


class DynamoDBPublicationDueWorkInventory:
    """Bounded, read-only inventory of pristine publication work that is due now."""

    __slots__ = ("_client", "_table_name")

    def __init__(self, *, client: DynamoDBQueryClient, table_name: str) -> None:
        if client is None:
            raise ValueError("Publication due-work client is required")
        self._client = client
        self._table_name = _validated_table_name(table_name)

    def list_due_publication_work(
        self,
        *,
        now: datetime,
        limit: int,
    ) -> tuple[PublicationDispatchCandidate, ...]:
        """Return at most 25 exact pristine rows in their deterministic GSI order."""

        if type(limit) is not int or not 1 <= limit <= MAX_PUBLICATION_DUE_WORK_BATCH:
            raise ValueError("Publication due-work limit must be between 1 and 25")
        cutoff_epoch = _utc_epoch_second(now)
        cutoff_sort_key = f"{cutoff_epoch:020d}#~"
        request = {
            "TableName": self._table_name,
            "IndexName": PUBLICATION_DUE_WORK_INDEX,
            "KeyConditionExpression": (
                "dispatch_pk = :dispatch_pk AND dispatch_sk <= :dispatch_sk"
            ),
            "ExpressionAttributeValues": {
                ":dispatch_pk": _s(PUBLICATION_DUE_WORK_PARTITION),
                ":dispatch_sk": _s(cutoff_sort_key),
            },
            "ScanIndexForward": True,
            "Limit": limit,
        }
        try:
            response = self._client.query(**request)
        except Exception:
            raise PublicationOrchestrationDependencyUnavailableError(
                "Publication due-work inventory is unavailable"
            ) from None
        return self._parse_due_response(
            response,
            now=now,
            cutoff_sort_key=cutoff_sort_key,
            limit=limit,
        )

    @staticmethod
    def _parse_due_response(
        response: object,
        *,
        now: datetime,
        cutoff_sort_key: str,
        limit: int,
    ) -> tuple[PublicationDispatchCandidate, ...]:
        if not isinstance(response, Mapping):
            raise PublicationOrchestrationBoundaryInvalidError(
                "Publication due-work response is invalid"
            )
        raw_items = response.get("Items")
        if not isinstance(raw_items, list) or len(raw_items) > limit:
            raise PublicationOrchestrationBoundaryInvalidError(
                "Publication due-work response exceeds its bounded contract"
            )
        count = response.get("Count")
        if count is not None and (type(count) is not int or count != len(raw_items)):
            raise PublicationOrchestrationBoundaryInvalidError(
                "Publication due-work response count is invalid"
            )
        scanned_count = response.get("ScannedCount")
        if scanned_count is not None and (
            type(scanned_count) is not int or scanned_count < len(raw_items)
        ):
            raise PublicationOrchestrationBoundaryInvalidError(
                "Publication due-work scanned count is invalid"
            )

        candidates: list[PublicationDispatchCandidate] = []
        seen_authority: set[tuple[str, str]] = set()
        previous_sort_key: str | None = None
        for raw_item in raw_items:
            if not isinstance(raw_item, dict):
                raise PublicationOrchestrationBoundaryInvalidError(
                    "Publication due-work row is invalid"
                )
            try:
                work = PublicationWorkRequest.model_validate_json(
                    _av_string(raw_item, "payload"),
                    strict=True,
                )
            except PublicationOrchestrationBoundaryInvalidError:
                raise
            except Exception:
                raise PublicationOrchestrationBoundaryInvalidError(
                    "Publication due-work payload is invalid"
                ) from None
            expected = _work_item(work)
            if raw_item != expected:
                raise PublicationOrchestrationBoundaryInvalidError(
                    "Publication due-work row differs from its exact payload"
                )
            sort_key = _av_string(raw_item, "dispatch_sk")
            if (
                sort_key > cutoff_sort_key
                or work.next_dispatch_at > now
                or previous_sort_key is not None
                and sort_key <= previous_sort_key
            ):
                raise PublicationOrchestrationBoundaryInvalidError(
                    "Publication due-work rows are unordered or outside the cutoff"
                )
            identity = (work.owner_id, work.aggregate_id)
            if identity in seen_authority:
                raise PublicationOrchestrationBoundaryInvalidError(
                    "Publication due-work response contains duplicate authority"
                )
            previous_sort_key = sort_key
            seen_authority.add(identity)
            try:
                candidate = PublicationDispatchCandidate(
                    owner_id=work.owner_id,
                    aggregate_id=work.aggregate_id,
                    work_request_id=work.work_request_id,
                    execution_name=work.execution_name,
                    verification_deadline=work.verification_deadline,
                    status=PublicationExecutionWorkStatus.PENDING,
                )
            except Exception:
                raise PublicationOrchestrationBoundaryInvalidError(
                    "Publication due-work execution identity is invalid"
                ) from None
            candidates.append(candidate)
        return tuple(candidates)


class DynamoDBPublicationTerminalIdentityResolver:
    """Resolve a stream-selected aggregate to one strongly proven terminal owner identity."""

    __slots__ = ("_client", "_table_name")

    def __init__(self, *, client: DynamoDBGetItemClient, table_name: str) -> None:
        if client is None:
            raise ValueError("Publication terminal-identity client is required")
        self._client = client
        self._table_name = _validated_table_name(table_name)

    def resolve_terminal_identity(self, aggregate_id: str) -> PublicationTerminalIdentity:
        """Strongly read the exact terminal META row and return only its owner identity."""

        if not isinstance(aggregate_id, str) or _SAFE_ID.fullmatch(aggregate_id) is None:
            raise ValueError("Publication aggregate identity is invalid")
        request = {
            "TableName": self._table_name,
            "Key": {
                "PK": _s(f"PUBLICATION#{aggregate_id}"),
                "SK": _s("META"),
            },
            "ConsistentRead": True,
        }
        try:
            response = self._client.get_item(**request)
        except Exception:
            raise PublicationOrchestrationDependencyUnavailableError(
                "Publication terminal identity is unavailable"
            ) from None
        if not isinstance(response, Mapping) or not isinstance(response.get("Item"), dict):
            raise PublicationOrchestrationBoundaryInvalidError(
                "Publication terminal aggregate is missing or invalid"
            )
        item = response["Item"]
        try:
            if _av_string(item, "entity_type") != "PUBLICATION_EXECUTION_AGGREGATE":
                raise ValueError
            aggregate = ExecutionPublicationAggregate.model_validate_json(
                _av_string(item, "payload"),
                strict=True,
            )
        except PublicationOrchestrationBoundaryInvalidError:
            raise
        except Exception:
            raise PublicationOrchestrationBoundaryInvalidError(
                "Publication terminal aggregate payload is invalid"
            ) from None
        if aggregate.aggregate_id != aggregate_id or aggregate.state not in _TERMINAL_STATES:
            raise PublicationOrchestrationBoundaryInvalidError(
                "Publication aggregate is not the selected terminal authority"
            )

        exact_item = dict(item)
        raw_expiry = exact_item.pop("expires_at", None)
        if exact_item != _terminal_item(aggregate):
            raise PublicationOrchestrationBoundaryInvalidError(
                "Publication terminal aggregate differs from its exact payload"
            )
        if raw_expiry is not None:
            assert aggregate.operational_expires_at is not None
            expected_expiry = ceil(aggregate.operational_expires_at.timestamp())
            if raw_expiry != _n(expected_expiry):
                raise PublicationOrchestrationBoundaryInvalidError(
                    "Publication terminal aggregate TTL is invalid"
                )
        return PublicationTerminalIdentity(
            aggregate_id=aggregate.aggregate_id,
            owner_id=aggregate.owner_id,
        )


__all__ = [
    "DynamoDBPublicationDueWorkInventory",
    "DynamoDBPublicationTerminalIdentityResolver",
    "MAX_PUBLICATION_DUE_WORK_BATCH",
    "PUBLICATION_DUE_WORK_INDEX",
    "PUBLICATION_DUE_WORK_PARTITION",
    "PublicationOrchestrationBoundaryInvalidError",
    "PublicationOrchestrationDependencyUnavailableError",
    "PublicationTerminalIdentity",
]
