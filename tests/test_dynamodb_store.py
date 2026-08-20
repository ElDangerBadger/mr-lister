from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from botocore.exceptions import ClientError

from mr_lister.contracts import ApprovalStatus, JobRecord, JobState, ReviewSnapshot
from mr_lister.workflow.dynamodb import DynamoDBJobStore
from mr_lister.workflow.errors import ConcurrentModificationError, IdempotencyConflictError
from mr_lister.workflow.models import (
    ApprovalWaitRecord,
    ApprovalWaitStatus,
    ArtworkInput,
    ExternalWriteClaim,
    ExternalWriteStatus,
    WorkflowEvent,
)

NOW = datetime(2026, 8, 19, 12, 0, tzinfo=UTC)


class MemoryDynamoClient:
    def __init__(self) -> None:
        self.items: dict[tuple[str, str], dict[str, Any]] = {}
        self.transactions: list[dict[str, Any]] = []
        self.fail_transactions = False

    def transact_write_items(self, **request: Any) -> None:
        self.transactions.append(request)
        if self.fail_transactions:
            raise ClientError(
                {
                    "Error": {
                        "Code": "TransactionCanceledException",
                        "Message": "synthetic conditional failure",
                    }
                },
                "TransactWriteItems",
            )
        for operation in request["TransactItems"]:
            item = operation["Put"]["Item"]
            self.items[(item["PK"]["S"], item["SK"]["S"])] = item

    def get_item(self, **request: Any) -> dict[str, Any]:
        key = request["Key"]
        item = self.items.get((key["PK"]["S"], key["SK"]["S"]))
        return {} if item is None else {"Item": item}

    def query(self, **request: Any) -> dict[str, Any]:
        values = request["ExpressionAttributeValues"]
        partition_key = values[":pk"]["S"]
        prefix = values[":prefix"]["S"]
        items = [
            item
            for (pk, sk), item in sorted(self.items.items())
            if pk == partition_key and sk.startswith(prefix)
        ]
        return {"Items": items}

    def put_item(self, **request: Any) -> None:
        item = request["Item"]
        key = (item["PK"]["S"], item["SK"]["S"])
        existing = self.items.get(key)
        if request.get("ConditionExpression") == "attribute_not_exists(PK)" and existing:
            raise ClientError(
                {
                    "Error": {
                        "Code": "ConditionalCheckFailedException",
                        "Message": "synthetic existing item",
                    }
                },
                "PutItem",
            )
        self.items[key] = item


def intake_records() -> tuple[JobRecord, ArtworkInput, WorkflowEvent]:
    job = JobRecord(
        job_id="job_durable_test",
        state=JobState.UPLOADED,
        event_sequence=1,
        review_version=0,
        idempotency_key="durable-intake-key",
        artwork_object_key="private/artwork/sha256/aa/example.png",
        created_at=NOW,
        updated_at=NOW,
    )
    artwork = ArtworkInput(
        filename="example.png",
        content_type="image/png",
        content_sha256="a" * 64,
        size_bytes=1024,
    )
    event = WorkflowEvent(
        sequence=1,
        occurred_at=NOW,
        name="artwork_uploaded",
        details={"filename": artwork.filename},
    )
    return job, artwork, event


def transition_records(current: JobRecord, target: JobState) -> tuple[JobRecord, WorkflowEvent]:
    updated = current.model_copy(
        update={
            "state": target,
            "record_version": current.record_version + 1,
            "event_sequence": current.event_sequence + 1,
            "updated_at": NOW,
        }
    )
    event = WorkflowEvent(
        sequence=updated.event_sequence,
        occurred_at=NOW,
        name="state_changed",
        details={"from": current.state, "to": target},
    )
    return updated, event


def test_intake_transaction_atomically_writes_claim_job_artwork_and_event() -> None:
    client = MemoryDynamoClient()
    store = DynamoDBJobStore(client=client, table_name="MrListerState")
    job, artwork, event = intake_records()

    created, was_created = store.create_intake(
        job=job,
        artwork=artwork,
        profile_id="synthetic_gildan_5000",
        request_fingerprint=f"{artwork.content_sha256}:synthetic_gildan_5000",
        event=event,
    )

    assert was_created is True
    assert created == job
    request = client.transactions[0]
    assert len(request["TransactItems"]) == 4
    assert len(request["ClientRequestToken"]) == 32
    assert all(
        operation["Put"]["ConditionExpression"] == "attribute_not_exists(PK)"
        for operation in request["TransactItems"]
    )
    assert {
        operation["Put"]["Item"]["entity_type"]["S"] for operation in request["TransactItems"]
    } == {"IDEMPOTENCY", "JOB", "ARTWORK", "EVENT"}


def test_fresh_store_instance_reconstructs_intake_and_committed_transition() -> None:
    client = MemoryDynamoClient()
    first_store = DynamoDBJobStore(client=client, table_name="MrListerState")
    job, artwork, event = intake_records()
    fingerprint = f"{artwork.content_sha256}:synthetic_gildan_5000"
    first_store.create_intake(
        job=job,
        artwork=artwork,
        profile_id="synthetic_gildan_5000",
        request_fingerprint=fingerprint,
        event=event,
    )
    updated, transition_event = transition_records(job, JobState.INTAKE_VALIDATED)
    first_store.commit_transition(
        current=job,
        updated=updated,
        event=transition_event,
    )

    reconstructed = DynamoDBJobStore(client=client, table_name="MrListerState")

    assert reconstructed.get_job(job.job_id) == updated
    assert reconstructed.get_artwork(job.job_id) == artwork
    assert reconstructed.get_profile_id(job.job_id) == "synthetic_gildan_5000"
    assert reconstructed.resolve_intake(job.idempotency_key, fingerprint) == updated
    assert reconstructed.list_events(job.job_id) == (event, transition_event)


def test_transition_transaction_conditions_on_state_record_and_event_versions() -> None:
    client = MemoryDynamoClient()
    store = DynamoDBJobStore(client=client, table_name="MrListerState")
    current, _artwork, _initial_event = intake_records()
    updated, event = transition_records(current, JobState.INTAKE_VALIDATED)

    store.commit_transition(current=current, updated=updated, event=event)

    job_put = client.transactions[0]["TransactItems"][0]["Put"]
    assert job_put["ConditionExpression"] == (
        "record_version = :record_version AND event_sequence = :event_sequence AND #state = :state"
    )
    assert job_put["ExpressionAttributeValues"] == {
        ":record_version": {"N": "0"},
        ":event_sequence": {"N": "1"},
        ":state": {"S": "uploaded"},
    }
    assert client.transactions[0]["TransactItems"][1]["Put"]["Item"]["SK"] == {
        "S": "EVENT#00000000000000000002"
    }


def test_review_and_listing_drafted_transition_share_one_transaction(
    artwork_analysis, listing, product_profile, valid_result
) -> None:
    client = MemoryDynamoClient()
    store = DynamoDBJobStore(client=client, table_name="MrListerState")
    intake, _artwork, _event = intake_records()
    current = intake.model_copy(
        update={
            "state": JobState.ANALYZING_ARTWORK,
            "record_version": 2,
            "event_sequence": 3,
        }
    )
    updated, event = transition_records(current, JobState.LISTING_DRAFTED)
    updated = updated.model_copy(update={"review_version": 1})
    review = ReviewSnapshot(
        review_version=1,
        artwork_analysis=artwork_analysis,
        listing=listing,
        profile=product_profile,
        validation=valid_result,
    )

    store.commit_transition(
        current=current,
        updated=updated,
        event=event,
        review=review,
    )

    request = client.transactions[0]
    assert len(request["TransactItems"]) == 3
    review_put = request["TransactItems"][2]["Put"]
    assert review_put["Item"]["SK"] == {"S": "REVIEW#00000000000000000001"}
    assert review_put["ConditionExpression"] == (
        "attribute_not_exists(PK) OR review_fingerprint = :fingerprint"
    )
    assert store.get_review(current.job_id) == review


def test_transaction_cancellation_is_a_concurrency_error() -> None:
    client = MemoryDynamoClient()
    client.fail_transactions = True
    store = DynamoDBJobStore(client=client, table_name="MrListerState")
    current, _artwork, _initial_event = intake_records()
    updated, event = transition_records(current, JobState.INTAKE_VALIDATED)

    with pytest.raises(ConcurrentModificationError):
        store.commit_transition(current=current, updated=updated, event=event)


def test_intake_claim_does_not_store_the_raw_idempotency_key() -> None:
    client = MemoryDynamoClient()
    store = DynamoDBJobStore(client=client, table_name="MrListerState")
    job, artwork, event = intake_records()
    fingerprint = f"{artwork.content_sha256}:synthetic_gildan_5000"
    store.create_intake(
        job=job,
        artwork=artwork,
        profile_id="synthetic_gildan_5000",
        request_fingerprint=fingerprint,
        event=event,
    )

    claim = client.transactions[0]["TransactItems"][0]["Put"]["Item"]

    assert job.idempotency_key not in repr(claim)
    with pytest.raises(IdempotencyConflictError):
        store.resolve_intake(job.idempotency_key, "different-fingerprint")


def test_checkpoints_and_completed_write_survive_store_reconstruction(
    artwork_analysis, listing
) -> None:
    client = MemoryDynamoClient()
    store = DynamoDBJobStore(client=client, table_name="MrListerState")
    job, artwork, event = intake_records()
    store.create_intake(
        job=job,
        artwork=artwork,
        profile_id="synthetic_gildan_5000",
        request_fingerprint=f"{artwork.content_sha256}:synthetic_gildan_5000",
        event=event,
    )
    store.save_analysis_checkpoint(job.job_id, artwork_analysis)
    store.save_listing_checkpoint(job.job_id, listing)
    claim = ExternalWriteClaim(
        operation="sync_product_draft",
        idempotency_key=f"draft:{job.job_id}:1",
        request_fingerprint="b" * 64,
        status=ExternalWriteStatus.CLAIMED,
        claimed_at=NOW,
    )
    stored, created = store.claim_external_write(job.job_id, claim)
    store.complete_external_write(
        job.job_id,
        idempotency_key=claim.idempotency_key,
        request_fingerprint=claim.request_fingerprint,
        result={"external_id": "product-1", "image_id": "image-1"},
        completed_at=NOW,
    )

    reconstructed = DynamoDBJobStore(client=client, table_name="MrListerState")
    replayed, replay_created = reconstructed.claim_external_write(job.job_id, claim)

    assert created is True
    assert stored == claim
    assert reconstructed.get_analysis_checkpoint(job.job_id) == artwork_analysis
    assert reconstructed.get_listing_checkpoint(job.job_id) == listing
    assert replay_created is False
    assert replayed.status is ExternalWriteStatus.COMPLETED
    assert replayed.result == {"external_id": "product-1", "image_id": "image-1"}
    assert reconstructed.list_external_writes(job.job_id)[0].external_id == "product-1"


def test_approval_and_wait_consumption_are_one_transaction(
    artwork_analysis, listing, product_profile, valid_result
) -> None:
    client = MemoryDynamoClient()
    store = DynamoDBJobStore(client=client, table_name="MrListerState")
    job, artwork, initial_event = intake_records()
    store.create_intake(
        job=job,
        artwork=artwork,
        profile_id="synthetic_gildan_5000",
        request_fingerprint=f"{artwork.content_sha256}:synthetic_gildan_5000",
        event=initial_event,
    )
    review = ReviewSnapshot(
        review_version=1,
        artwork_analysis=artwork_analysis,
        listing=listing,
        profile=product_profile,
        validation=valid_result,
        printify_product_id="product-1",
    )
    current = job
    for target in (
        JobState.INTAKE_VALIDATED,
        JobState.ANALYZING_ARTWORK,
        JobState.LISTING_DRAFTED,
        JobState.LISTING_VALIDATED,
        JobState.READY_FOR_PRODUCTION,
        JobState.PRINTIFY_DRAFT_CREATED,
        JobState.AWAITING_APPROVAL,
    ):
        updated, event = transition_records(current, target)
        committed_review = None
        if target is JobState.LISTING_DRAFTED:
            updated = updated.model_copy(update={"review_version": 1})
            committed_review = review
        if target is JobState.PRINTIFY_DRAFT_CREATED:
            updated = updated.model_copy(
                update={"printify_image_id": "image-1", "printify_product_id": "product-1"}
            )
            committed_review = review
        store.commit_transition(
            current=current,
            updated=updated,
            event=event,
            review=committed_review,
        )
        current = updated
    wait = ApprovalWaitRecord(
        job_id=job.job_id,
        review_version=1,
        task_token="private-step-functions-task-token",
        status=ApprovalWaitStatus.PENDING,
        created_at=NOW,
        expires_at=NOW + timedelta(hours=1),
    )
    store.register_approval_wait(wait)
    consumed = wait.model_copy(update={"status": ApprovalWaitStatus.CONSUMED, "consumed_at": NOW})
    approved, approval_event = transition_records(current, JobState.APPROVED)
    approved = approved.model_copy(update={"approved_review_version": 1})
    approved_review = review.model_copy(update={"approval_status": ApprovalStatus.APPROVED})

    store.commit_transition(
        current=current,
        updated=approved,
        event=approval_event,
        review=approved_review,
        approval_wait=(wait, consumed),
    )

    approval_transaction = client.transactions[-1]
    assert len(approval_transaction["TransactItems"]) == 4
    wait_put = approval_transaction["TransactItems"][-1]["Put"]
    assert wait_put["Item"]["SK"] == {"S": "APPROVAL_WAIT"}
    assert wait_put["ConditionExpression"] == (
        "review_version = :review_version AND wait_status = :pending AND expires_at > :consumed_at"
    )
    reconstructed = DynamoDBJobStore(client=client, table_name="MrListerState")
    assert reconstructed.get_job(job.job_id).state is JobState.APPROVED
    assert reconstructed.get_approval_wait(job.job_id) == consumed
