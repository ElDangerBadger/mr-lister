from __future__ import annotations

import json
from typing import Any

import pytest
from botocore.exceptions import ClientError
from test_phase71_publication_store import (
    OWNER_ID,
    _review_fingerprint,
    make_authority,
    make_transaction,
)

from mr_lister.control.fingerprints import review_etag
from mr_lister.control.models import ReviewContent
from mr_lister.publication import dynamodb as publication_dynamodb
from mr_lister.publication.commands import PublicationCommandReceipt
from mr_lister.publication.dynamodb import (
    MAX_DYNAMODB_ITEM_BYTES,
    MAX_DYNAMODB_TRANSACTION_BYTES,
    MAX_PUBLICATION_REQUEST_TRANSACTION_ITEMS,
    PUBLICATION_REQUEST_TRANSACTION_ITEMS,
    DynamoDBPublicationStore,
)
from mr_lister.publication.errors import (
    PublicationConflictError,
    PublicationErrorCode,
    PublicationIdempotencyConflictError,
    PublicationNotFoundError,
)
from mr_lister.publication.fingerprints import publication_command_receipt_fingerprint
from mr_lister.publication.store import PublicationRequestAuthority

TABLE_NAME = "MrListerPhase7PublicationTest"


def _client_error(code: str, *, reasons: list[dict[str, str]] | None = None) -> ClientError:
    response: dict[str, Any] = {"Error": {"Code": code, "Message": "synthetic transaction failure"}}
    if reasons is not None:
        response["CancellationReasons"] = reasons
    return ClientError(response, "TransactWriteItems")


def _record_item(
    *,
    job_id: str,
    sort_key: str,
    entity_type: str,
    record: Any,
) -> dict[str, Any]:
    return {
        "PK": {"S": f"JOB#{job_id}"},
        "SK": {"S": sort_key},
        "entity_type": {"S": entity_type},
        "contract_version": {"S": record.contract_version},
        "payload": {"S": record.model_dump_json()},
    }


class MemoryPublicationDynamoClient:
    """Small all-or-nothing low-level fake for the publication transaction shape."""

    def __init__(self) -> None:
        self.items: dict[tuple[str, str], dict[str, Any]] = {}
        self.get_requests: list[dict[str, Any]] = []
        self.transactions: list[dict[str, Any]] = []
        self.next_error: ClientError | None = None

    @staticmethod
    def _key(item: dict[str, Any]) -> tuple[str, str]:
        return item["PK"]["S"], item["SK"]["S"]

    def seed_authority(self, authority: PublicationRequestAuthority) -> None:
        job = authority.current_job
        items = (
            publication_dynamodb._job_item(job),
            _record_item(
                job_id=job.job_id,
                sort_key=f"REVIEW#{authority.review.review_version:020d}",
                entity_type="REVIEW",
                record=authority.review,
            ),
            _record_item(
                job_id=job.job_id,
                sort_key=f"DECISION#{authority.approval_decision.decision_id}",
                entity_type="REVIEW_DECISION",
                record=authority.approval_decision,
            ),
            _record_item(
                job_id=job.job_id,
                sort_key="SOURCE",
                entity_type="SOURCE_ARTIFACT",
                record=authority.source,
            ),
            _record_item(
                job_id=job.job_id,
                sort_key=f"PRODUCT_SYNC#{authority.product_sync.sync_id}",
                entity_type="PRODUCT_SYNC",
                record=authority.product_sync,
            ),
            _record_item(
                job_id=job.job_id,
                sort_key=f"PRICING#{authority.pricing_snapshot.snapshot_id}",
                entity_type="PRICING_SNAPSHOT",
                record=authority.pricing_snapshot,
            ),
            _record_item(
                job_id=job.job_id,
                sort_key=f"PRICING_EVIDENCE#{authority.pricing_evidence.snapshot_id}",
                entity_type="PRICING_EVIDENCE",
                record=authority.pricing_evidence,
            ),
        )
        for item in items:
            self.items[self._key(item)] = item

    def get_item(self, **request: Any) -> dict[str, Any]:
        self.get_requests.append(request)
        key = request["Key"]
        item = self.items.get((key["PK"]["S"], key["SK"]["S"]))
        return {} if item is None else {"Item": item}

    def transact_write_items(self, **request: Any) -> None:
        self.transactions.append(request)
        if self.next_error is not None:
            error = self.next_error
            self.next_error = None
            raise error
        actions = request["TransactItems"]
        if any(not self._condition_holds(action) for action in actions):
            raise _client_error(
                "TransactionCanceledException",
                reasons=[{"Code": "ConditionalCheckFailed"}],
            )
        pending = [action["Put"]["Item"] for action in actions if "Put" in action]
        for item in pending:
            self.items[self._key(item)] = item

    def _condition_holds(self, action: dict[str, Any]) -> bool:
        operation = action.get("Put") or action.get("ConditionCheck")
        assert operation is not None
        item = operation.get("Item")
        key = operation.get("Key")
        lookup = self._key(item) if item is not None else (key["PK"]["S"], key["SK"]["S"])
        existing = self.items.get(lookup)
        condition = operation["ConditionExpression"]
        if condition == "attribute_not_exists(PK)":
            return existing is None
        if existing is None:
            return False
        values = operation["ExpressionAttributeValues"]
        if "ConditionCheck" in action:
            return (
                existing.get("entity_type") == values[":entity_type"]
                and existing.get("contract_version") == values[":contract_version"]
                and existing.get("payload") == values[":expected_payload"]
            )
        expected = {
            "entity_type": values[":entity_type"],
            "contract_version": values[":contract_version"],
            "owner_id": values[":owner_id"],
            "owner_jobs_pk": values[":owner_jobs_pk"],
            "owner_jobs_sk": values[":owner_jobs_sk"],
            "record_version": values[":record_version"],
            "event_sequence": values[":event_sequence"],
            "state": values[":state"],
            "review_version": values[":review_version"],
            "cancellation_requested": values[":cancellation_requested"],
            "payload": values[":expected_payload"],
        }
        return all(existing.get(name) == value for name, value in expected.items())


def _store_with_authority() -> tuple[
    DynamoDBPublicationStore,
    MemoryPublicationDynamoClient,
    PublicationRequestAuthority,
]:
    authority = make_authority()
    client = MemoryPublicationDynamoClient()
    client.seed_authority(authority)
    return (
        DynamoDBPublicationStore(client=client, table_name=TABLE_NAME),
        client,
        authority,
    )


def test_dynamo_loads_owner_first_with_strong_exact_authority_reads() -> None:
    store, client, authority = _store_with_authority()

    assert store.load_request_authority(OWNER_ID, authority.current_job.job_id) == authority
    assert len(client.get_requests) == 7
    assert all(request["ConsistentRead"] is True for request in client.get_requests)
    assert client.get_requests[0]["Key"] == {
        "PK": {"S": f"JOB#{authority.current_job.job_id}"},
        "SK": {"S": "META"},
    }

    client.get_requests.clear()
    with pytest.raises(PublicationNotFoundError):
        store.load_request_authority("f" * 64, authority.current_job.job_id)
    assert len(client.get_requests) == 1


def test_dynamo_request_is_exactly_fourteen_bounded_isolated_actions() -> None:
    store, client, authority = _store_with_authority()
    transaction = make_transaction(authority)

    assert store.commit_request(transaction) == transaction.commit.receipt
    request = client.transactions[0]
    actions = request["TransactItems"]

    assert len(actions) == PUBLICATION_REQUEST_TRANSACTION_ITEMS == 14
    assert len(actions) <= MAX_PUBLICATION_REQUEST_TRANSACTION_ITEMS == 25
    assert [next(iter(action)) for action in actions] == [
        "Put",
        "ConditionCheck",
        "ConditionCheck",
        "ConditionCheck",
        "ConditionCheck",
        "ConditionCheck",
        "ConditionCheck",
        "Put",
        "Put",
        "Put",
        "Put",
        "Put",
        "Put",
        "Put",
    ]
    expected_checks = [
        f"REVIEW#{authority.review.review_version:020d}",
        f"DECISION#{authority.approval_decision.decision_id}",
        "SOURCE",
        f"PRODUCT_SYNC#{authority.product_sync.sync_id}",
        f"PRICING#{authority.pricing_snapshot.snapshot_id}",
        f"PRICING_EVIDENCE#{authority.pricing_evidence.snapshot_id}",
    ]
    assert [action["ConditionCheck"]["Key"]["SK"]["S"] for action in actions[1:7]] == (
        expected_checks
    )
    put_items = [action["Put"]["Item"] for action in actions if "Put" in action]
    publication_items = put_items[1:]
    assert [item["SK"]["S"].split("#", 1)[0] for item in publication_items] == [
        "META",
        "SNAPSHOT",
        "ATTEMPT",
        "PERMIT",
        "PUBLICATION_WORK",
        "EVENT",
        "PUBLICATION_RECEIPT",
    ]
    work_item = next(
        item for item in publication_items if item["SK"]["S"].startswith("PUBLICATION_WORK#")
    )
    assert work_item["dispatch_pk"] == {"S": "PUBLICATION_WORK_DUE#0"}
    assert not any(item["SK"]["S"].startswith("WORK#") for item in publication_items)
    assert not any("expires_at" in item for item in put_items)
    updated_job = put_items[0]
    assert updated_job["event_sequence"] == {"N": str(authority.current_job.event_sequence)}
    assert updated_job["owner_jobs_pk"] == {"S": f"OWNER#{OWNER_ID}"}
    assert sum(item["entity_type"]["S"] == "CONTROL_JOB" for item in put_items) == 1
    event_item = next(
        item for item in publication_items if item["entity_type"]["S"] == "PUBLICATION_DOMAIN_EVENT"
    )
    assert event_item["PK"]["S"].startswith("PUBLICATION#")
    assert len(request["ClientRequestToken"]) == 32
    assert store.get_aggregate_for_owner(OWNER_ID, authority.current_job.job_id) == (
        transaction.commit.aggregate
    )


def test_dynamo_conditional_cancel_resolves_replay_conflict_and_changed_key() -> None:
    store, client, authority = _store_with_authority()
    transaction = make_transaction(authority)
    first = store.commit_request(transaction)

    assert store.commit_request(transaction) == first
    assert (
        client.transactions[0]["ClientRequestToken"] == client.transactions[1]["ClientRequestToken"]
    )
    changed_body = make_transaction(
        authority,
        suffix="2",
        idempotency_key="publish-key-1",
        request_fingerprint="c" * 64,
    )
    with pytest.raises(PublicationIdempotencyConflictError) as idempotency_error:
        store.commit_request(changed_body)
    assert idempotency_error.value.__cause__ is None
    assert idempotency_error.value.__suppress_context__ is True

    changed_key = make_transaction(authority, suffix="3", idempotency_key="publish-key-2")
    with pytest.raises(PublicationConflictError) as concurrent_error:
        store.commit_request(changed_key)
    assert concurrent_error.value.code is PublicationErrorCode.CONCURRENT_WRITE
    assert concurrent_error.value.__cause__ is None
    assert concurrent_error.value.__suppress_context__ is True


def test_dynamo_nonconditional_cancellation_is_retryable_and_writes_nothing() -> None:
    store, client, authority = _store_with_authority()
    transaction = make_transaction(authority)
    original = dict(client.items)
    dependency_error = _client_error(
        "TransactionCanceledException",
        reasons=[{"Code": "ProvisionedThroughputExceeded"}],
    )
    client.next_error = dependency_error

    with pytest.raises(ClientError) as error:
        store.commit_request(transaction)
    assert error.value is dependency_error
    assert client.items == original
    assert (
        store.resolve_request_receipt(
            OWNER_ID,
            authority.current_job.job_id,
            transaction.commit.receipt.idempotency_key_digest,
        )
        is None
    )


def test_dynamo_stale_child_condition_causes_no_partial_write() -> None:
    store, client, authority = _store_with_authority()
    transaction = make_transaction(authority)
    review_key = (
        f"JOB#{authority.current_job.job_id}",
        f"REVIEW#{authority.review.review_version:020d}",
    )
    client.items[review_key] = {
        **client.items[review_key],
        "payload": {
            "S": authority.review.model_copy(update={"title": "changed"}).model_dump_json()
        },
    }
    original = dict(client.items)

    with pytest.raises(PublicationConflictError) as error:
        store.commit_request(transaction)
    assert error.value.code is PublicationErrorCode.CONCURRENT_WRITE
    assert client.items == original
    assert not any(key[0].startswith("PUBLICATION#") for key in client.items)


def test_maximum_review_stays_below_dynamo_item_and_transaction_envelopes() -> None:
    authority = make_authority()
    large_review = ReviewContent.model_validate(
        {
            **authority.review.model_dump(mode="python"),
            "description": "x" * 100_000,
            "fingerprint": "0" * 64,
        }
    )
    large_review = ReviewContent.model_validate(
        {
            **large_review.model_dump(mode="python"),
            "fingerprint": _review_fingerprint(large_review),
        }
    )
    approval_fingerprint = review_etag(
        job_id=authority.current_job.job_id,
        review_version=large_review.review_version,
        review_fingerprint=large_review.fingerprint,
        product_id=authority.current_job.product_id,
        product_sync_fingerprint=authority.current_job.product_sync_fingerprint,
        pricing_snapshot_id=authority.current_job.pricing_snapshot_id,
        pricing_snapshot_fingerprint=authority.current_job.pricing_snapshot_fingerprint,
    )
    large_authority = PublicationRequestAuthority(
        **{
            **authority.__dict__,
            "review": large_review,
            "approval_decision": authority.approval_decision.model_copy(
                update={
                    "review_fingerprint": large_review.fingerprint,
                    "approval_fingerprint": approval_fingerprint,
                }
            ),
            "current_job": authority.current_job.model_copy(
                update={
                    "review_fingerprint": large_review.fingerprint,
                    "approved_review_fingerprint": large_review.fingerprint,
                    "approval_fingerprint": approval_fingerprint,
                }
            ),
        }
    )
    transaction = make_transaction(large_authority)
    client = MemoryPublicationDynamoClient()
    client.seed_authority(large_authority)
    store = DynamoDBPublicationStore(client=client, table_name=TABLE_NAME)

    store.commit_request(transaction)
    actions = client.transactions[0]["TransactItems"]

    def rendered(value: Any) -> int:
        return len(json.dumps(value, sort_keys=True, separators=(",", ":")).encode())

    assert rendered(actions) < MAX_DYNAMODB_TRANSACTION_BYTES
    for action in actions:
        if "Put" in action:
            assert rendered(action["Put"]["Item"]) < MAX_DYNAMODB_ITEM_BYTES
        elif ":expected_payload" in action["ConditionCheck"]["ExpressionAttributeValues"]:
            expected = action["ConditionCheck"]["ExpressionAttributeValues"][":expected_payload"]
            assert rendered(expected) < MAX_DYNAMODB_ITEM_BYTES


def test_job_entity_marker_is_part_of_the_publication_cas() -> None:
    store, client, authority = _store_with_authority()
    transaction = make_transaction(authority)
    job_key = (f"JOB#{authority.current_job.job_id}", "META")
    client.items[job_key] = {
        **client.items[job_key],
        "entity_type": {"S": "FORGED_JOB"},
    }

    with pytest.raises(PublicationConflictError):
        store.commit_request(transaction)


@pytest.mark.parametrize(
    "tamper",
    ("payload_digest", "top_level_contract", "top_level_request_fingerprint"),
)
def test_receipt_resolution_rejects_metadata_payload_parity_tampering(tamper: str) -> None:
    store, client, authority = _store_with_authority()
    transaction = make_transaction(authority)
    store.commit_request(transaction)
    receipt_key = next(
        key
        for key, item in client.items.items()
        if item.get("entity_type", {}).get("S") == "PUBLICATION_COMMAND_RECEIPT"
    )
    item = client.items[receipt_key]
    if tamper == "payload_digest":
        receipt_values = transaction.commit.receipt.model_dump(
            mode="python", exclude={"fingerprint"}
        )
        receipt_values["idempotency_key_digest"] = "f" * 64
        tampered_receipt = PublicationCommandReceipt(
            **receipt_values,
            fingerprint=publication_command_receipt_fingerprint(receipt_values),
        )
        item = {**item, "payload": {"S": tampered_receipt.model_dump_json()}}
    elif tamper == "top_level_contract":
        item = {**item, "contract_version": {"S": "7.0.0"}}
    else:
        item = {**item, "request_fingerprint": {"S": "e" * 64}}
    client.items[receipt_key] = item

    assert (
        store.resolve_request_receipt(
            OWNER_ID,
            authority.current_job.job_id,
            transaction.commit.receipt.idempotency_key_digest,
        )
        is None
    )
    with pytest.raises(PublicationConflictError) as error:
        store.commit_request(transaction)
    assert error.value.code is PublicationErrorCode.CONCURRENT_WRITE
