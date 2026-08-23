from __future__ import annotations

import copy
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from typing import Any

import pytest

from mr_lister.control.models import ControlJobRecord, ControlJobState
from mr_lister.production.operational_cleanup import (
    OperationalCleanupAuthorityChangedError,
    OperationalCleanupBoundaryInvalidError,
    OperationalCleanupCheckpoint,
    OperationalCleanupDependencyUnavailableError,
    TerminalJobAuthority,
)
from mr_lister.production.operational_cleanup_aws import (
    DynamoDBOperationalCleanupCheckpointStore,
    DynamoDBOperationalJobInventory,
    DynamoDBTerminalOperationalExpiryStore,
)

NOW = datetime(2026, 8, 23, 12, tzinfo=UTC)
OWNER = "a" * 64
TABLE = "mr-lister-phase6-dev"
JOB_ID = "terminal_job"


def _s(value: str) -> dict[str, str]:
    return {"S": value}


def _n(value: int) -> dict[str, str]:
    return {"N": str(value)}


def _terminal_job(job_id: str = JOB_ID) -> ControlJobRecord:
    return ControlJobRecord.model_validate(
        {
            "owner_id": OWNER,
            "job_id": job_id,
            "record_version": 7,
            "event_sequence": 8,
            "state": ControlJobState.CANCELLED,
            "cancellation_requested_at": NOW - timedelta(days=100, hours=1),
            "created_at": NOW - timedelta(days=101),
            "updated_at": NOW - timedelta(days=100),
        }
    )


def _job_item(job: ControlJobRecord) -> dict[str, Any]:
    return {
        "PK": _s(f"JOB#{job.job_id}"),
        "SK": _s("META"),
        "entity_type": _s("CONTROL_JOB"),
        "contract_version": _s(job.contract_version),
        "owner_id": _s(job.owner_id),
        "state": _s(job.state.value),
        "record_version": _n(job.record_version),
        "event_sequence": _n(job.event_sequence),
        "payload": _s(job.model_dump_json()),
    }


def _projected_authority_item(
    authority: TerminalJobAuthority,
    *,
    state: str | None = None,
) -> dict[str, Any]:
    return {
        "PK": _s(f"JOB#{authority.job_id}"),
        "SK": _s("META"),
        "entity_type": _s("CONTROL_JOB"),
        "owner_id": _s(authority.owner_id),
        "state": _s(state or authority.state.value),
        "record_version": _n(authority.record_version),
        "event_sequence": _n(authority.event_sequence),
    }


def _row(
    sort_key: str,
    entity_type: str,
    *,
    partition_key: str | None = None,
    job_id: str | None = None,
    expires_at: int | None = None,
) -> dict[str, Any]:
    item: dict[str, Any] = {
        "PK": _s(partition_key or f"JOB#{JOB_ID}"),
        "SK": _s(sort_key),
        "entity_type": _s(entity_type),
    }
    if job_id is not None:
        item["job_id"] = _s(job_id)
    if expires_at is not None:
        item["expires_at"] = _n(expires_at)
    return item


def _scan_response(
    items: list[dict[str, Any]],
    *,
    scanned: int,
    last_key: dict[str, Any] | None = None,
) -> dict[str, Any]:
    response: dict[str, Any] = {
        "Items": items,
        "Count": len(items),
        "ScannedCount": scanned,
        "ResponseMetadata": {"HTTPHeaders": {"date": format_datetime(NOW, usegmt=True)}},
    }
    if last_key is not None:
        response["LastEvaluatedKey"] = last_key
    return response


def _query_response(
    items: list[dict[str, Any]],
    *,
    last_key: dict[str, Any] | None = None,
) -> dict[str, Any]:
    response: dict[str, Any] = {
        "Items": items,
        "Count": len(items),
        "ScannedCount": len(items),
    }
    if last_key is not None:
        response["LastEvaluatedKey"] = last_key
    return response


class TransactionCancelled(RuntimeError):
    def __init__(self, secret: str) -> None:
        super().__init__(secret)
        self.response = {"Error": {"Code": "TransactionCanceledException"}}


class RecordingClient:
    def __init__(self) -> None:
        self.scan_responses: list[object] = []
        self.query_responses: list[object] = []
        self.get_response: object = {}
        self.put_response: object = {"ResponseMetadata": {"HTTPStatusCode": 200}}
        self.transact_responses: list[object] = []
        self.scan_calls: list[dict[str, Any]] = []
        self.query_calls: list[dict[str, Any]] = []
        self.get_calls: list[dict[str, Any]] = []
        self.put_calls: list[dict[str, Any]] = []
        self.transact_calls: list[dict[str, Any]] = []

    @staticmethod
    def _resolve(value: object) -> dict[str, Any]:
        if isinstance(value, Exception):
            raise value
        return copy.deepcopy(value)  # type: ignore[return-value]

    def scan(self, **kwargs: Any) -> dict[str, Any]:
        self.scan_calls.append(copy.deepcopy(kwargs))
        return self._resolve(self.scan_responses.pop(0))

    def query(self, **kwargs: Any) -> dict[str, Any]:
        self.query_calls.append(copy.deepcopy(kwargs))
        return self._resolve(self.query_responses.pop(0))

    def get_item(self, **kwargs: Any) -> dict[str, Any]:
        self.get_calls.append(copy.deepcopy(kwargs))
        return self._resolve(self.get_response)

    def put_item(self, **kwargs: Any) -> dict[str, Any]:
        self.put_calls.append(copy.deepcopy(kwargs))
        return self._resolve(self.put_response)

    def transact_write_items(self, **kwargs: Any) -> dict[str, Any]:
        self.transact_calls.append(copy.deepcopy(kwargs))
        value = (
            self.transact_responses.pop(0)
            if self.transact_responses
            else {"ResponseMetadata": {"HTTPStatusCode": 200}}
        )
        return self._resolve(value)


def _authority() -> TerminalJobAuthority:
    job = _terminal_job()
    return TerminalJobAuthority(
        job_id=job.job_id,
        owner_id=job.owner_id,
        state=job.state,
        record_version=job.record_version,
        event_sequence=job.event_sequence,
        terminal_updated_at=job.updated_at,
    )


def _expiry() -> int:
    return int((_authority().terminal_updated_at + timedelta(days=90)).timestamp())


def test_inventory_strongly_scans_only_projected_control_job_authority() -> None:
    first = _terminal_job("first_job")
    second = _terminal_job("second_job")
    client = RecordingClient()
    client.scan_responses = [
        _scan_response(
            [_job_item(first), _job_item(second)],
            scanned=19,
            last_key={"PK": _s("OWNER#" + OWNER), "SK": _s("RECEIPT#last")},
        ),
        _scan_response([_job_item(second)], scanned=4),
    ]
    inventory = DynamoDBOperationalJobInventory(client=client, table_name=TABLE)

    page = inventory.search_next_job(cursor=None, limit=25)
    assert page.job == first
    assert page.records_scanned == 19
    assert page.next_cursor is not None
    assert first.job_id not in page.next_cursor
    assert first.model_dump_json() not in page.next_cursor

    following = inventory.search_next_job(cursor=page.next_cursor, limit=25)
    assert following.job == second
    assert client.scan_calls[0] == {
        "TableName": TABLE,
        "ConsistentRead": True,
        "Limit": 25,
        "FilterExpression": "#entity_type = :control_job",
        "ProjectionExpression": (
            "PK, SK, #entity_type, contract_version, owner_id, #state, "
            "record_version, event_sequence, payload"
        ),
        "ExpressionAttributeNames": {
            "#entity_type": "entity_type",
            "#state": "state",
        },
        "ExpressionAttributeValues": {":control_job": _s("CONTROL_JOB")},
    }
    assert client.scan_calls[1]["ExclusiveStartKey"] == {
        "PK": _s("JOB#first_job"),
        "SK": _s("META"),
    }
    assert not hasattr(client, "delete_item")
    assert not hasattr(client, "batch_write_item")


def test_inventory_dependency_failure_is_identifier_free() -> None:
    client = RecordingClient()
    client.scan_responses = [RuntimeError("payload for private_job")]

    with pytest.raises(OperationalCleanupDependencyUnavailableError) as captured:
        DynamoDBOperationalJobInventory(client=client, table_name=TABLE).search_next_job(
            cursor=None,
            limit=10,
        )

    assert str(captured.value) == "Operational cleanup dependency is unavailable"
    assert "private_job" not in str(captured.value)
    assert captured.value.__cause__ is None


def test_job_partition_expiry_is_exact_transactional_and_idempotent() -> None:
    authority = _authority()
    expiry = _expiry()
    meta = _projected_authority_item(authority)
    work = _row("WORK#one", "WORK_REQUEST")
    client = RecordingClient()
    client.query_responses = [
        _query_response([work, meta]),
        _query_response([work, meta]),
    ]
    store = DynamoDBTerminalOperationalExpiryStore(client=client, table_name=TABLE)

    first = store.assign_terminal_expiry(
        authority=authority,
        expires_at_epoch_seconds=expiry,
        cursor=None,
        limit=24,
    )
    replay = store.assign_terminal_expiry(
        authority=authority,
        expires_at_epoch_seconds=expiry,
        cursor=None,
        limit=24,
    )

    assert first == replay
    assert first.records_assigned == 2
    assert first.next_cursor is not None
    assert len(client.transact_calls) == 2
    assert client.transact_calls[0] == client.transact_calls[1]
    operations = client.transact_calls[0]["TransactItems"]
    assert len(operations) == 2
    assert all("Update" in operation for operation in operations)
    assert not any("Delete" in operation for operation in operations)
    meta_update = next(
        operation["Update"]
        for operation in operations
        if operation["Update"]["Key"]["SK"] == _s("META")
    )
    assert "#state = :state" in meta_update["ConditionExpression"]
    assert meta_update["ExpressionAttributeValues"][":state"] == _s("cancelled")
    assert meta_update["ExpressionAttributeValues"][":expiry"] == _n(expiry)
    assert "payload" not in repr(client.transact_calls).casefold()


def test_owner_phase_assigns_only_matching_receipts_under_same_authority() -> None:
    authority = _authority()
    expiry = _expiry()
    client = RecordingClient()
    client.query_responses = [
        _query_response([_projected_authority_item(authority)]),
        _query_response(
            [
                _row(
                    "RECEIPT#same",
                    "COMMAND_RECEIPT",
                    partition_key=f"OWNER#{OWNER}",
                    job_id=JOB_ID,
                ),
                _row(
                    "RECEIPT#other",
                    "COMMAND_RECEIPT",
                    partition_key=f"OWNER#{OWNER}",
                    job_id="another_job",
                    expires_at=expiry + 12_345,
                ),
            ]
        ),
    ]
    store = DynamoDBTerminalOperationalExpiryStore(client=client, table_name=TABLE)

    job_page = store.assign_terminal_expiry(
        authority=authority,
        expires_at_epoch_seconds=expiry,
        cursor=None,
        limit=24,
    )
    owner_page = store.assign_terminal_expiry(
        authority=authority,
        expires_at_epoch_seconds=expiry,
        cursor=job_page.next_cursor,
        limit=24,
    )

    assert owner_page.records_examined == 2
    assert owner_page.records_assigned == 1
    assert owner_page.next_cursor is None
    assert client.query_calls[1]["ExpressionAttributeValues"] == {
        ":partition_key": _s(f"OWNER#{OWNER}")
    }
    owner_operations = client.transact_calls[1]["TransactItems"]
    assert "ConditionCheck" in owner_operations[0]
    assert len(owner_operations) == 2
    receipt_update = owner_operations[1]["Update"]
    assert receipt_update["Key"]["SK"] == _s("RECEIPT#same")
    assert receipt_update["ExpressionAttributeValues"][":job_id"] == _s(JOB_ID)


def test_transaction_race_preserves_rows_when_terminal_authority_changed() -> None:
    authority = _authority()
    client = RecordingClient()
    client.query_responses = [_query_response([_row("EVENT#1", "DOMAIN_EVENT")])]
    client.transact_responses = [TransactionCancelled("private race payload")]
    client.get_response = {"Item": _projected_authority_item(authority, state="approved")}

    with pytest.raises(OperationalCleanupAuthorityChangedError) as captured:
        DynamoDBTerminalOperationalExpiryStore(
            client=client,
            table_name=TABLE,
        ).assign_terminal_expiry(
            authority=authority,
            expires_at_epoch_seconds=_expiry(),
            cursor=None,
            limit=24,
        )

    assert str(captured.value) == "Operational cleanup authority changed"
    assert "private" not in str(captured.value)
    assert len(client.transact_calls) == 1
    assert len(client.get_calls) == 1


def test_transaction_failure_with_unchanged_authority_fails_closed() -> None:
    authority = _authority()
    client = RecordingClient()
    client.query_responses = [_query_response([_row("EVENT#1", "DOMAIN_EVENT")])]
    client.transact_responses = [TransactionCancelled("secret child conflict")]
    client.get_response = {"Item": _projected_authority_item(authority)}

    with pytest.raises(OperationalCleanupDependencyUnavailableError) as captured:
        DynamoDBTerminalOperationalExpiryStore(
            client=client,
            table_name=TABLE,
        ).assign_terminal_expiry(
            authority=authority,
            expires_at_epoch_seconds=_expiry(),
            cursor=None,
            limit=24,
        )

    assert str(captured.value) == "Operational cleanup dependency is unavailable"
    assert "secret" not in str(captured.value)
    assert captured.value.__cause__ is None


def test_adapter_rejects_short_expiry_and_conflicting_existing_ttl_before_write() -> None:
    authority = _authority()
    client = RecordingClient()
    store = DynamoDBTerminalOperationalExpiryStore(client=client, table_name=TABLE)

    with pytest.raises(OperationalCleanupBoundaryInvalidError):
        store.assign_terminal_expiry(
            authority=authority,
            expires_at_epoch_seconds=_expiry() - 1,
            cursor=None,
            limit=24,
        )
    assert client.query_calls == []

    client.query_responses = [
        _query_response([_row("EVENT#1", "DOMAIN_EVENT", expires_at=_expiry() + 1)])
    ]
    with pytest.raises(OperationalCleanupBoundaryInvalidError):
        store.assign_terminal_expiry(
            authority=authority,
            expires_at_epoch_seconds=_expiry(),
            cursor=None,
            limit=24,
        )
    assert client.transact_calls == []


def test_checkpoint_create_and_replace_use_exact_payload_cas() -> None:
    client = RecordingClient()
    store = DynamoDBOperationalCleanupCheckpointStore(client=client, table_name=TABLE)
    initial = store.load_checkpoint()
    first = OperationalCleanupCheckpoint(revision=1)

    store.save_checkpoint(expected=initial, updated=first)
    assert client.put_calls[0]["ConditionExpression"] == "attribute_not_exists(PK)"
    assert "ExpressionAttributeNames" not in client.put_calls[0]

    client.get_response = {"Item": copy.deepcopy(client.put_calls[0]["Item"])}
    loaded = store.load_checkpoint()
    second = OperationalCleanupCheckpoint(revision=2)
    store.save_checkpoint(expected=loaded, updated=second)

    assert loaded == first
    assert "revision = :expected_revision" in client.put_calls[1]["ConditionExpression"]
    assert client.put_calls[1]["ExpressionAttributeValues"][":expected_revision"] == _n(1)
    assert client.put_calls[1]["ExpressionAttributeValues"][":expected_payload"] == _s(
        first.model_dump_json()
    )


def test_checkpoint_conditional_failure_is_sanitized() -> None:
    client = RecordingClient()
    client.put_response = RuntimeError("checkpoint for private terminal_job")

    with pytest.raises(OperationalCleanupDependencyUnavailableError) as captured:
        DynamoDBOperationalCleanupCheckpointStore(
            client=client,
            table_name=TABLE,
        ).save_checkpoint(
            expected=OperationalCleanupCheckpoint(),
            updated=OperationalCleanupCheckpoint(revision=1),
        )

    assert "terminal_job" not in str(captured.value)
    assert captured.value.__cause__ is None
