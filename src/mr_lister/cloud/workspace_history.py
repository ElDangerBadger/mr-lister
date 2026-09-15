"""Account-scoped recent-list preferences, isolated from durable listing authority."""

from __future__ import annotations

import re
from collections.abc import Callable
from datetime import UTC, datetime
from hashlib import sha256
from typing import Any, Protocol

from botocore.exceptions import ClientError

from mr_lister.control.errors import NotFoundError
from mr_lister.control.models import ControlJobRecord
from mr_lister.control.store import OwnerJobPage

_MARKER = "WORKSPACE_HISTORY"
_RECEIPT_PREFIX = "WORKSPACE_HISTORY_CLEAR#"
_OWNER = re.compile(r"^[a-f0-9]{64}$")
_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")


class WorkspaceHistoryPort(Protocol):
    def cleared_before(self, owner_id: str) -> datetime | None: ...

    def clear(self, *, owner_id: str, idempotency_key: str) -> datetime: ...


class OwnerJobQueryPort(Protocol):
    def get_job_for_owner(self, owner_id: str, job_id: str) -> ControlJobRecord: ...

    def list_jobs_for_owner(
        self, owner_id: str, *, limit: int = 25, cursor: str | None = None
    ) -> OwnerJobPage: ...


class HistoryFilteredJobQuery:
    """Filter only recent-list pages; direct job access and all job state stay intact."""

    def __init__(self, *, store: OwnerJobQueryPort, history: WorkspaceHistoryPort) -> None:
        self._store = store
        self._history = history

    def get_job_for_owner(self, owner_id: str, job_id: str) -> ControlJobRecord:
        return self._store.get_job_for_owner(owner_id, job_id)

    def list_jobs_for_owner(
        self, owner_id: str, *, limit: int = 25, cursor: str | None = None
    ) -> OwnerJobPage:
        cutoff = self._history.cleared_before(owner_id)
        page = self._store.list_jobs_for_owner(owner_id, limit=limit, cursor=cursor)
        if any(job.owner_id != owner_id for job in page.jobs):
            raise NotFoundError("The requested job page was not found")
        return OwnerJobPage(
            jobs=tuple(job for job in page.jobs if cutoff is None or job.created_at > cutoff),
            # This is the last raw index row, even when every visible row was cleared.
            next_cursor=page.next_cursor,
        )


class DynamoWorkspaceHistory:
    """Atomic monotonic owner marker and permanent per-request replay receipt.

    These rows have no job index keys or TTL, and never overwrite any job, approval,
    publication, provider, or cleanup record. Only GET and two-item transactions are used.
    """

    def __init__(
        self,
        *,
        client: Any,
        table_name: str,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._client = client
        self._table_name = table_name
        self._clock = clock or (lambda: datetime.now(UTC))

    def cleared_before(self, owner_id: str) -> datetime | None:
        return self._read(owner_id, _MARKER)

    def clear(self, *, owner_id: str, idempotency_key: str) -> datetime:
        if not isinstance(idempotency_key, str) or _KEY.fullmatch(idempotency_key) is None:
            raise ValueError("Workspace history request key is invalid")
        receipt_key = _RECEIPT_PREFIX + sha256(idempotency_key.encode("ascii")).hexdigest()
        # The requested cutoff is fixed even if another clear wins a conditional race.
        requested = _utc(self._clock())
        for _attempt in range(4):
            previous_receipt = self._read(owner_id, receipt_key)
            if previous_receipt is not None:
                return previous_receipt
            previous = self.cleared_before(owner_id)
            effective = requested if previous is None else max(requested, previous)
            marker: dict[str, Any] = {
                "TableName": self._table_name,
                "Item": _item(owner_id, _MARKER, effective),
                "ConditionExpression": "attribute_not_exists(PK)",
            }
            if previous is not None:
                marker.update(
                    ConditionExpression="cleared_before = :previous",
                    ExpressionAttributeValues={":previous": {"S": _text(previous)}},
                )
            try:
                self._client.transact_write_items(
                    TransactItems=[
                        {"Put": marker},
                        {
                            "Put": {
                                "TableName": self._table_name,
                                "Item": _item(owner_id, receipt_key, effective),
                                "ConditionExpression": "attribute_not_exists(PK)",
                            }
                        },
                    ]
                )
                return effective
            except ClientError as error:
                if error.response.get("Error", {}).get("Code") != "TransactionCanceledException":
                    raise
                reasons = error.response.get("CancellationReasons", [])
                codes = [reason.get("Code") for reason in reasons]
                if not codes or not set(codes) <= {"None", "ConditionalCheckFailed"}:
                    raise
                if "ConditionalCheckFailed" not in codes:
                    raise
        raise RuntimeError("Workspace history changed concurrently; retry the same request")

    def _read(self, owner_id: str, sort_key: str) -> datetime | None:
        partition_key = _owner_key(owner_id)
        response = self._client.get_item(
            TableName=self._table_name,
            Key={"PK": {"S": partition_key}, "SK": {"S": sort_key}},
            ConsistentRead=True,
        )
        item = response.get("Item")
        if item is None:
            return None
        try:
            value = item["cleared_before"]["S"]
            parsed = _utc(datetime.fromisoformat(value))
            if value != _text(parsed) or item != _item(owner_id, sort_key, parsed):
                raise ValueError
            return parsed
        except (KeyError, TypeError, ValueError):
            raise ValueError("Workspace history preference is invalid") from None


def _owner_key(owner_id: str) -> str:
    if not isinstance(owner_id, str) or _OWNER.fullmatch(owner_id) is None:
        raise ValueError("Workspace history owner is invalid")
    return f"OWNER#{owner_id}"


def _utc(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Workspace history time must include a timezone")
    return value.astimezone(UTC)


def _text(value: datetime) -> str:
    return _utc(value).isoformat(timespec="microseconds")


def _item(owner_id: str, sort_key: str, cutoff: datetime) -> dict[str, dict[str, str]]:
    return {
        "PK": {"S": _owner_key(owner_id)},
        "SK": {"S": sort_key},
        "entity_type": {"S": _MARKER if sort_key == _MARKER else "WORKSPACE_HISTORY_CLEAR"},
        "owner_id": {"S": owner_id},
        "cleared_before": {"S": _text(cutoff)},
    }
