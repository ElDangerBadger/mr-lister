"""Adversarial tests for the read-only Phase 7.15 DynamoDB control plane."""

from __future__ import annotations

import copy
from datetime import UTC, datetime, timedelta
from math import ceil
from pathlib import Path
from typing import Any

import pytest

from mr_lister.publication.contract import PublicationState
from mr_lister.publication.execution_fingerprints import execution_record_fingerprint
from mr_lister.publication.execution_models import ExecutionPublicationAggregate
from mr_lister.publication.fingerprints import publication_work_input_fingerprint
from mr_lister.publication.models import PublicationWorkRequest
from mr_lister.publication.orchestration import publication_execution_name
from mr_lister.publication.orchestration_dynamodb import (
    DynamoDBPublicationDueWorkInventory,
    DynamoDBPublicationTerminalIdentityResolver,
    PublicationOrchestrationBoundaryInvalidError,
    PublicationOrchestrationDependencyUnavailableError,
    PublicationTerminalIdentity,
)

NOW = datetime(2026, 9, 1, 18, 0, tzinfo=UTC)
OWNER_ID = "a" * 64
TABLE_NAME = "MrListerPhase7PublicationTest"


def _s(value: str) -> dict[str, str]:
    return {"S": value}


def _n(value: int) -> dict[str, str]:
    return {"N": str(value)}


def _work(
    suffix: str,
    *,
    aggregate_id: str | None = None,
    created_at: datetime | None = None,
) -> PublicationWorkRequest:
    created = created_at or NOW - timedelta(seconds=1)
    work_request_id = f"publication_work_{suffix}"
    values: dict[str, Any] = {
        "work_request_id": work_request_id,
        "aggregate_id": aggregate_id or f"publication_{suffix}",
        "attempt_id": f"attempt_{suffix}",
        "snapshot_id": f"snapshot_{suffix}",
        "snapshot_fingerprint": "b" * 64,
        "permit_id": f"permit_{suffix}",
        "owner_id": OWNER_ID,
        "job_id": f"job_{suffix}",
        "receipt_id": f"receipt_{suffix}",
        "execution_name": publication_execution_name(work_request_id),
        "verification_deadline": created + timedelta(minutes=30),
        "next_dispatch_at": created,
        "created_at": created,
        "updated_at": created,
    }
    values["input_fingerprint"] = publication_work_input_fingerprint(values)
    return PublicationWorkRequest(**values)


def _due_item(work: PublicationWorkRequest) -> dict[str, dict[str, str]]:
    return {
        "PK": _s(f"PUBLICATION#{work.aggregate_id}"),
        "SK": _s(f"PUBLICATION_WORK#{work.work_request_id}"),
        "entity_type": _s("PUBLICATION_WORK_REQUEST"),
        "contract_version": _s(work.contract_version),
        "payload": _s(work.model_dump_json()),
        "work_status": _s("pending"),
        "work_request_id": _s(work.work_request_id),
        "dispatch_pk": _s("PUBLICATION_WORK_DUE#0"),
        "dispatch_sk": _s(f"{int(work.next_dispatch_at.timestamp()):020d}#{work.work_request_id}"),
    }


def _terminal_aggregate(
    *, state: PublicationState = PublicationState.PUBLICATION_FAILED
) -> ExecutionPublicationAggregate:
    requested_at = NOW - timedelta(minutes=30)
    terminal = state in {
        PublicationState.PUBLISHED,
        PublicationState.PUBLICATION_FAILED,
        PublicationState.PUBLICATION_OUTCOME_UNKNOWN,
    }
    values: dict[str, Any] = {
        "aggregate_id": "publication_terminal",
        "owner_id": OWNER_ID,
        "job_id": "job_terminal",
        "state": state,
        "record_version": 1,
        "event_sequence": 2,
        "snapshot_id": "snapshot_terminal",
        "snapshot_fingerprint": "b" * 64,
        "attempt_id": "attempt_terminal",
        "permit_id": "permit_terminal",
        "work_request_id": "work_terminal",
        "receipt_id": "receipt_terminal",
        "requested_at": requested_at,
        "verification_deadline": NOW,
        "updated_at": NOW if terminal else requested_at,
        "terminal_at": NOW if terminal else None,
        "source_release_eligible_at": NOW + timedelta(days=30) if terminal else None,
        "operational_expires_at": NOW + timedelta(days=90) if terminal else None,
        "last_observation_fingerprint": None,
        "result_id": None,
        "notification_id": None,
        "report_id": "report_terminal" if terminal else None,
        "tombstone_id": "tombstone_terminal" if terminal else None,
        "provider_audit_record_version": 0,
        "provider_evidence_record_version": 0,
    }
    values["fingerprint"] = execution_record_fingerprint("execution_aggregate", values)
    return ExecutionPublicationAggregate(**values)


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


class RecordingReadClient:
    def __init__(self) -> None:
        self.query_response: object = {"Items": [], "Count": 0, "ScannedCount": 0}
        self.get_response: object = {}
        self.query_error: Exception | None = None
        self.get_error: Exception | None = None
        self.query_calls: list[dict[str, Any]] = []
        self.get_calls: list[dict[str, Any]] = []

    def query(self, **request: Any) -> Any:
        self.query_calls.append(request)
        if self.query_error is not None:
            raise self.query_error
        return self.query_response

    def get_item(self, **request: Any) -> Any:
        self.get_calls.append(request)
        if self.get_error is not None:
            raise self.get_error
        return self.get_response


def _inventory(client: RecordingReadClient) -> DynamoDBPublicationDueWorkInventory:
    return DynamoDBPublicationDueWorkInventory(client=client, table_name=TABLE_NAME)


def _resolver(client: RecordingReadClient) -> DynamoDBPublicationTerminalIdentityResolver:
    return DynamoDBPublicationTerminalIdentityResolver(client=client, table_name=TABLE_NAME)


def test_due_inventory_uses_only_exact_bounded_gsi_query_and_stable_order() -> None:
    client = RecordingReadClient()
    first = _work("first", created_at=NOW - timedelta(seconds=2))
    second = _work("second", created_at=NOW - timedelta(seconds=1))
    client.query_response = {
        "Items": [_due_item(first), _due_item(second)],
        "Count": 2,
        "ScannedCount": 2,
    }

    candidates = _inventory(client).list_due_publication_work(now=NOW, limit=2)

    assert [candidate.aggregate_id for candidate in candidates] == [
        first.aggregate_id,
        second.aggregate_id,
    ]
    assert [candidate.owner_id for candidate in candidates] == [OWNER_ID, OWNER_ID]
    assert client.query_calls == [
        {
            "TableName": TABLE_NAME,
            "IndexName": "DueWorkIndex",
            "KeyConditionExpression": (
                "dispatch_pk = :dispatch_pk AND dispatch_sk <= :dispatch_sk"
            ),
            "ExpressionAttributeValues": {
                ":dispatch_pk": _s("PUBLICATION_WORK_DUE#0"),
                ":dispatch_sk": _s(f"{int(NOW.timestamp()):020d}#~"),
            },
            "ScanIndexForward": True,
            "Limit": 2,
        }
    ]
    assert client.get_calls == []


@pytest.mark.parametrize("limit", [0, 26, True, 1.0, "1"])
def test_due_inventory_rejects_invalid_bounds_before_query(limit: object) -> None:
    client = RecordingReadClient()

    with pytest.raises(ValueError, match="between 1 and 25"):
        _inventory(client).list_due_publication_work(now=NOW, limit=limit)  # type: ignore[arg-type]

    assert client.query_calls == []


def test_due_inventory_rejects_non_utc_cutoff_before_query() -> None:
    client = RecordingReadClient()

    with pytest.raises(ValueError, match="UTC-aware"):
        _inventory(client).list_due_publication_work(now=NOW.replace(tzinfo=None), limit=1)

    assert client.query_calls == []


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("PK", _s("PUBLICATION#foreign")),
        ("SK", _s("PUBLICATION_WORK#foreign")),
        ("entity_type", _s("PUBLICATION_EXECUTION_WORK")),
        ("contract_version", _s("7.0.0")),
        ("payload", _s("{}")),
        ("work_status", _s("dispatched")),
        ("work_request_id", _s("foreign")),
        ("dispatch_pk", _s("PUBLICATION_WORK_DUE#1")),
        ("dispatch_sk", _s("00000000000000000000#foreign")),
    ],
)
def test_due_inventory_rejects_any_row_or_payload_drift(
    field: str,
    replacement: dict[str, str],
) -> None:
    client = RecordingReadClient()
    item = _due_item(_work("one"))
    item[field] = replacement
    client.query_response = {"Items": [item], "Count": 1, "ScannedCount": 1}

    with pytest.raises(PublicationOrchestrationBoundaryInvalidError):
        _inventory(client).list_due_publication_work(now=NOW, limit=1)


def test_due_inventory_rejects_extra_fields_malformed_counts_and_dependency_failure() -> None:
    client = RecordingReadClient()
    item = _due_item(_work("one"))
    item["unexpected"] = _s("authority-expansion")
    client.query_response = {"Items": [item], "Count": 1, "ScannedCount": 1}
    with pytest.raises(PublicationOrchestrationBoundaryInvalidError, match="exact payload"):
        _inventory(client).list_due_publication_work(now=NOW, limit=1)

    client.query_response = {"Items": [], "Count": True, "ScannedCount": 0}
    with pytest.raises(PublicationOrchestrationBoundaryInvalidError, match="count"):
        _inventory(client).list_due_publication_work(now=NOW, limit=1)

    client.query_error = RuntimeError("private dependency detail")
    with pytest.raises(
        PublicationOrchestrationDependencyUnavailableError,
        match="inventory is unavailable",
    ) as captured:
        _inventory(client).list_due_publication_work(now=NOW, limit=1)
    assert "private" not in str(captured.value)


def test_due_inventory_rejects_unordered_duplicate_and_future_authority() -> None:
    client = RecordingReadClient()
    first = _work("first", created_at=NOW - timedelta(seconds=2))
    second = _work("second", created_at=NOW - timedelta(seconds=1))
    client.query_response = {
        "Items": [_due_item(second), _due_item(first)],
        "Count": 2,
        "ScannedCount": 2,
    }
    with pytest.raises(PublicationOrchestrationBoundaryInvalidError, match="unordered"):
        _inventory(client).list_due_publication_work(now=NOW, limit=2)

    duplicate_one = _work(
        "duplicate_one",
        aggregate_id="publication_duplicate",
        created_at=NOW - timedelta(seconds=2),
    )
    duplicate_two = _work(
        "duplicate_two",
        aggregate_id="publication_duplicate",
        created_at=NOW - timedelta(seconds=1),
    )
    client.query_response = {
        "Items": [_due_item(duplicate_one), _due_item(duplicate_two)],
        "Count": 2,
        "ScannedCount": 2,
    }
    with pytest.raises(PublicationOrchestrationBoundaryInvalidError, match="duplicate"):
        _inventory(client).list_due_publication_work(now=NOW, limit=2)

    future = _work("future", created_at=NOW + timedelta(microseconds=1))
    client.query_response = {"Items": [_due_item(future)], "Count": 1, "ScannedCount": 1}
    with pytest.raises(PublicationOrchestrationBoundaryInvalidError, match="cutoff"):
        _inventory(client).list_due_publication_work(now=NOW, limit=1)


def test_terminal_resolver_uses_one_exact_strong_meta_read() -> None:
    client = RecordingReadClient()
    aggregate = _terminal_aggregate()
    client.get_response = {"Item": _terminal_item(aggregate)}

    identity = _resolver(client).resolve_terminal_identity(aggregate.aggregate_id)

    assert identity == PublicationTerminalIdentity(
        aggregate_id=aggregate.aggregate_id,
        owner_id=OWNER_ID,
    )
    assert client.get_calls == [
        {
            "TableName": TABLE_NAME,
            "Key": {
                "PK": _s(f"PUBLICATION#{aggregate.aggregate_id}"),
                "SK": _s("META"),
            },
            "ConsistentRead": True,
        }
    ]
    assert client.query_calls == []


def test_terminal_resolver_accepts_only_the_exact_authoritative_ttl_on_replay() -> None:
    client = RecordingReadClient()
    aggregate = _terminal_aggregate()
    item = _terminal_item(aggregate)
    assert aggregate.operational_expires_at is not None
    item["expires_at"] = _n(ceil(aggregate.operational_expires_at.timestamp()))
    client.get_response = {"Item": item}

    assert _resolver(client).resolve_terminal_identity(aggregate.aggregate_id).owner_id == OWNER_ID

    item["expires_at"] = _n(ceil(aggregate.operational_expires_at.timestamp()) + 1)
    with pytest.raises(PublicationOrchestrationBoundaryInvalidError, match="TTL"):
        _resolver(client).resolve_terminal_identity(aggregate.aggregate_id)


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("PK", _s("PUBLICATION#foreign")),
        ("SK", _s("FOREIGN")),
        ("entity_type", _s("PUBLICATION_AGGREGATE")),
        ("contract_version", _s("7.0.0")),
        ("owner_id", _s("c" * 64)),
        ("job_id", _s("job_foreign")),
        ("publication_state", _s("published")),
        ("record_version", _n(99)),
        ("provider_audit_record_version", _n(1)),
        ("provider_evidence_record_version", _n(1)),
    ],
)
def test_terminal_resolver_rejects_every_envelope_or_owner_drift(
    field: str,
    replacement: dict[str, str],
) -> None:
    client = RecordingReadClient()
    aggregate = _terminal_aggregate()
    item = _terminal_item(aggregate)
    item[field] = replacement
    client.get_response = {"Item": item}

    with pytest.raises(PublicationOrchestrationBoundaryInvalidError):
        _resolver(client).resolve_terminal_identity(aggregate.aggregate_id)


def test_terminal_resolver_rejects_nonterminal_missing_extra_and_dependency_failure() -> None:
    client = RecordingReadClient()
    nonterminal = _terminal_aggregate(state=PublicationState.PUBLICATION_RECONCILING)
    client.get_response = {"Item": _terminal_item(nonterminal)}
    with pytest.raises(PublicationOrchestrationBoundaryInvalidError, match="not the selected"):
        _resolver(client).resolve_terminal_identity(nonterminal.aggregate_id)

    client.get_response = {}
    with pytest.raises(PublicationOrchestrationBoundaryInvalidError, match="missing"):
        _resolver(client).resolve_terminal_identity("publication_terminal")

    item = _terminal_item(_terminal_aggregate())
    item["unexpected"] = _s("authority-expansion")
    client.get_response = {"Item": item}
    with pytest.raises(PublicationOrchestrationBoundaryInvalidError, match="exact payload"):
        _resolver(client).resolve_terminal_identity("publication_terminal")

    client.get_error = RuntimeError("private dependency detail")
    with pytest.raises(
        PublicationOrchestrationDependencyUnavailableError,
        match="identity is unavailable",
    ) as captured:
        _resolver(client).resolve_terminal_identity("publication_terminal")
    assert "private" not in str(captured.value)


def test_invalid_configuration_and_identity_never_touch_dependencies() -> None:
    client = RecordingReadClient()
    with pytest.raises(ValueError, match="table name"):
        DynamoDBPublicationDueWorkInventory(client=client, table_name="x")
    with pytest.raises(ValueError, match="aggregate identity"):
        _resolver(client).resolve_terminal_identity("unsafe/aggregate")
    assert client.query_calls == []
    assert client.get_calls == []


def test_adapter_source_has_no_client_construction_or_mutation_capability() -> None:
    source = Path("src/mr_lister/publication/orchestration_dynamodb.py").read_text()

    assert "boto3" not in source
    assert "secretsmanager" not in source.casefold()
    for forbidden in (
        ".put_item(",
        ".update_item(",
        ".delete_item(",
        ".transact_write_items(",
        "start_execution(",
        "redrive_execution(",
    ):
        assert forbidden not in source

    client = RecordingReadClient()
    client.query_response = copy.deepcopy(client.query_response)
    DynamoDBPublicationDueWorkInventory(client=client, table_name=TABLE_NAME)
    DynamoDBPublicationTerminalIdentityResolver(client=client, table_name=TABLE_NAME)
    assert client.query_calls == []
    assert client.get_calls == []
