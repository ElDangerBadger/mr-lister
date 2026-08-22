from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from botocore.exceptions import ClientError

from mr_lister.control.dispatch import deterministic_execution_name, work_input_fingerprint
from mr_lister.control.dynamodb import DynamoDBSellerControlStore
from mr_lister.control.errors import (
    ConcurrentControlModificationError,
    IdempotencyConflictError,
    InvalidControlStateError,
)
from mr_lister.control.models import (
    CommandReceipt,
    CommandResponse,
    ControlJobRecord,
    ControlJobState,
    DomainEvent,
    ReviewActor,
    ReviewContent,
    WorkRequest,
    WorkRequestStatus,
    WorkType,
)
from mr_lister.control.store import CommandCommit

NOW = datetime(2026, 8, 21, 12, 0, tzinfo=UTC)
OWNER = "a" * 64
TABLE_NAME = "MrListerPhase6Control"


def _client_error(code: str, operation: str) -> ClientError:
    return ClientError(
        {"Error": {"Code": code, "Message": "synthetic conditional failure"}},
        operation,
    )


class MemoryLowLevelDynamoClient:
    """Small conditional-write fake for the control-store adapter contract."""

    def __init__(self) -> None:
        self.items: dict[tuple[str, str], dict[str, Any]] = {}
        self.transactions: list[dict[str, Any]] = []
        self.put_requests: list[dict[str, Any]] = []
        self.query_requests: list[dict[str, Any]] = []
        self.fail_next_put_condition = False
        self.concurrent_winner_on_next_put: dict[str, Any] | None = None

    @staticmethod
    def _key(item: dict[str, Any]) -> tuple[str, str]:
        return item["PK"]["S"], item["SK"]["S"]

    def transact_write_items(self, **request: Any) -> None:
        self.transactions.append(request)
        operations = request["TransactItems"]
        if any(not self._condition_holds(operation["Put"]) for operation in operations):
            raise _client_error("TransactionCanceledException", "TransactWriteItems")
        for operation in operations:
            item = operation["Put"]["Item"]
            self.items[self._key(item)] = item

    def get_item(self, **request: Any) -> dict[str, Any]:
        key = request["Key"]
        item = self.items.get((key["PK"]["S"], key["SK"]["S"]))
        return {} if item is None else {"Item": item}

    def query(self, **request: Any) -> dict[str, Any]:
        self.query_requests.append(request)
        values = request["ExpressionAttributeValues"]
        dispatch_pk = values[":dispatch_pk"]["S"]
        dispatch_sk = values[":dispatch_sk"]["S"]
        candidates = [
            item
            for item in self.items.values()
            if item.get("dispatch_pk", {}).get("S") == dispatch_pk
            and item.get("dispatch_sk", {}).get("S", "~") <= dispatch_sk
        ]
        candidates.sort(key=lambda item: item["dispatch_sk"]["S"])
        return {"Items": candidates[: request["Limit"]]}

    def put_item(self, **request: Any) -> None:
        self.put_requests.append(request)
        if self.concurrent_winner_on_next_put is not None:
            winner = self.concurrent_winner_on_next_put
            self.concurrent_winner_on_next_put = None
            self.items[self._key(winner)] = winner
            raise _client_error("ConditionalCheckFailedException", "PutItem")
        if self.fail_next_put_condition:
            self.fail_next_put_condition = False
            raise _client_error("ConditionalCheckFailedException", "PutItem")
        if not self._condition_holds(request):
            raise _client_error("ConditionalCheckFailedException", "PutItem")
        item = request["Item"]
        self.items[self._key(item)] = item

    def arrange_concurrent_work_winner(self, completed: WorkRequest) -> None:
        key = (f"JOB#{completed.job_id}", f"WORK#{completed.work_request_id}")
        winner = dict(self.items[key])
        winner["payload"] = {"S": completed.model_dump_json()}
        winner["work_status"] = {"S": completed.status.value}
        winner.pop("dispatch_pk", None)
        winner.pop("dispatch_sk", None)
        self.concurrent_winner_on_next_put = winner

    def _condition_holds(self, put: dict[str, Any]) -> bool:
        item = put["Item"]
        existing = self.items.get(self._key(item))
        condition = put.get("ConditionExpression")
        if condition is None:
            return True
        if condition == "attribute_not_exists(PK)":
            return existing is None
        if existing is None:
            return False

        values = put.get("ExpressionAttributeValues", {})
        if condition == "payload = :expected_payload":
            return existing.get("payload") == values[":expected_payload"]
        if condition == "work_status = :pending AND payload = :expected_payload":
            return (
                existing.get("work_status") == values[":pending"]
                and existing.get("payload") == values[":expected_payload"]
            )
        if "record_version = :record_version" in condition:
            expected_attributes = {
                "contract_version": values[":contract_version"],
                "owner_id": values[":owner_id"],
                "record_version": values[":record_version"],
                "event_sequence": values[":event_sequence"],
                "state": values[":state"],
                "review_version": values[":review_version"],
                "cancellation_requested": values[":cancellation_requested"],
                "payload": values[":expected_payload"],
            }
            return all(existing.get(name) == value for name, value in expected_attributes.items())
        raise AssertionError(f"Unsupported fake condition: {condition}")


def make_job(
    *,
    state: ControlJobState = ControlJobState.INTAKE_VALIDATED,
    record_version: int = 0,
    event_sequence: int = 1,
    review_version: int = 0,
    review_fingerprint: str | None = None,
    review_validated: bool = False,
    active_work_request_id: str | None = None,
    updated_at: datetime = NOW,
) -> ControlJobRecord:
    return ControlJobRecord(
        owner_id=OWNER,
        job_id="job_phase6_dynamo",
        record_version=record_version,
        event_sequence=event_sequence,
        state=state,
        review_version=review_version,
        review_fingerprint=review_fingerprint,
        review_validated=review_validated,
        active_work_request_id=active_work_request_id,
        created_at=NOW,
        updated_at=updated_at,
    )


def make_response(job: ControlJobRecord, *, work_id: str | None = None) -> CommandResponse:
    return CommandResponse(
        job_id=job.job_id,
        state=job.state,
        record_version=job.record_version,
        review_version=job.review_version,
        work_request_id=work_id,
    )


def make_receipt(
    job: ControlJobRecord,
    *,
    receipt_id: str,
    command_type: str,
    key_digest: str,
    request_fingerprint: str,
    work_id: str | None = None,
) -> CommandReceipt:
    return CommandReceipt(
        receipt_id=receipt_id,
        owner_id=job.owner_id,
        job_id=job.job_id,
        command_type=command_type,
        idempotency_key_digest=key_digest,
        request_fingerprint=request_fingerprint,
        response=make_response(job, work_id=work_id),
        work_request_id=work_id,
        created_at=job.updated_at,
    )


def make_event(job: ControlJobRecord, name: str) -> DomainEvent:
    return DomainEvent(
        job_id=job.job_id,
        sequence=job.event_sequence,
        name=name,
        occurred_at=job.updated_at,
    )


def make_work(job: ControlJobRecord, receipt: CommandReceipt, *, due_at: datetime) -> WorkRequest:
    assert receipt.work_request_id is not None
    work_id = receipt.work_request_id
    return WorkRequest(
        work_request_id=work_id,
        owner_id=job.owner_id,
        job_id=job.job_id,
        receipt_id=receipt.receipt_id,
        work_type=WorkType.PREPARE,
        input_fingerprint=work_input_fingerprint(
            work_type=WorkType.PREPARE,
            job_id=job.job_id,
            work_request_id=work_id,
        ),
        execution_name=deterministic_execution_name(work_id),
        next_dispatch_at=due_at,
        created_at=NOW,
        updated_at=NOW,
    )


def create_job_with_work(
    store: DynamoDBSellerControlStore,
    *,
    due_at: datetime = NOW,
) -> tuple[ControlJobRecord, CommandReceipt, WorkRequest]:
    work_id = "work_prepare"
    job = make_job(active_work_request_id=work_id)
    receipt = make_receipt(
        job,
        receipt_id="receipt_create",
        command_type="create_job",
        key_digest="1" * 64,
        request_fingerprint="2" * 64,
        work_id=work_id,
    )
    work = make_work(job, receipt, due_at=due_at)
    store.create_job(
        job=job,
        event=make_event(job, "JOB_CREATED"),
        receipt=receipt,
        work_request=work,
    )
    return job, receipt, work


def advance_to_listing_drafted(
    store: DynamoDBSellerControlStore,
    initial: ControlJobRecord,
) -> ControlJobRecord:
    """Advance the pristine intake through only legal retained-PREPARE states."""

    current = initial
    for index, target in enumerate(
        (ControlJobState.ANALYZING_ARTWORK, ControlJobState.LISTING_DRAFTED),
        start=1,
    ):
        updated = make_job(
            state=target,
            record_version=current.record_version + 1,
            event_sequence=current.event_sequence + 1,
            active_work_request_id=current.active_work_request_id,
            updated_at=NOW,
        )
        receipt = make_receipt(
            updated,
            receipt_id=f"receipt_setup_{index}",
            command_type=f"setup_{target.value}",
            key_digest=f"{index + 2}" * 64,
            request_fingerprint=f"{index + 4}" * 64,
        )
        store.commit_command(
            CommandCommit(
                current=current,
                updated=updated,
                event=make_event(updated, f"SETUP_{target.value.upper()}"),
                receipt=receipt,
            )
        )
        current = updated
    return current


def make_review(job_id: str) -> ReviewContent:
    return ReviewContent(
        job_id=job_id,
        review_version=1,
        fingerprint="3" * 64,
        actor=ReviewActor.MODEL,
        title="Geometric Badger Shirt",
        description="A durable listing used to verify the Phase 6 DynamoDB boundary.",
        tags=tuple(f"tag {index}" for index in range(13)),
        title_rationale="Names the visible artwork and the product.",
        tag_rationale="Uses distinct buyer-facing phrases.",
        validation_passed=True,
        artwork_analysis_fingerprint="4" * 64,
        product_profile_fingerprint="5" * 64,
        created_at=NOW + timedelta(seconds=1),
    )


def make_listing_commit(
    current: ControlJobRecord,
    *,
    active_work: WorkRequest,
    request_fingerprint: str = "7" * 64,
) -> CommandCommit:
    review = make_review(current.job_id)
    sync_work_id = "work_product_sync"
    updated = make_job(
        state=ControlJobState.PRODUCT_DRAFT_SYNCING,
        record_version=current.record_version + 1,
        event_sequence=current.event_sequence + 1,
        review_version=1,
        review_fingerprint=review.fingerprint,
        review_validated=True,
        active_work_request_id=sync_work_id,
        updated_at=NOW + timedelta(seconds=1),
    )
    receipt = make_receipt(
        updated,
        receipt_id="receipt_listing",
        command_type="complete_preparation",
        key_digest="6" * 64,
        request_fingerprint=request_fingerprint,
        work_id=sync_work_id,
    )
    sync_work = WorkRequest(
        work_request_id=sync_work_id,
        owner_id=updated.owner_id,
        job_id=updated.job_id,
        receipt_id=receipt.receipt_id,
        work_type=WorkType.SYNCHRONIZE_PRODUCT,
        review_version=1,
        input_fingerprint=work_input_fingerprint(
            work_type=WorkType.SYNCHRONIZE_PRODUCT,
            job_id=updated.job_id,
            work_request_id=sync_work_id,
        ),
        execution_name=deterministic_execution_name(sync_work_id),
        next_dispatch_at=updated.updated_at,
        created_at=updated.updated_at,
        updated_at=updated.updated_at,
    )
    completed_work = WorkRequest.model_validate(
        {
            **active_work.model_dump(mode="python"),
            "status": WorkRequestStatus.COMPLETED,
            "updated_at": updated.updated_at,
        }
    )
    return CommandCommit(
        current=current,
        updated=updated,
        event=make_event(updated, "LISTING_DRAFTED"),
        receipt=receipt,
        review=review,
        work_request=sync_work,
        work_update=(active_work, completed_work),
    )


def dispatch_initial_work(
    store: DynamoDBSellerControlStore,
    job: ControlJobRecord,
    work: WorkRequest,
) -> WorkRequest:
    claimed = store.claim_work(
        job.job_id,
        work.work_request_id,
        now=NOW,
        claim_id="claim_preparation",
        lease_expires_at=NOW + timedelta(minutes=1),
    )
    assert claimed is not None
    return store.mark_work_dispatched(
        job.job_id,
        work.work_request_id,
        claim_id="claim_preparation",
        execution_arn=("arn:aws:states:us-west-2:123456789012:execution:mr-lister-prepare:initial"),
        now=NOW,
    )


def completed_concurrent_winner(claimed: WorkRequest) -> WorkRequest:
    return WorkRequest.model_validate(
        {
            **claimed.model_dump(mode="python"),
            "status": WorkRequestStatus.COMPLETED,
            "claim_id": None,
            "lease_expires_at": None,
            "updated_at": NOW + timedelta(seconds=2),
        }
    )


def test_create_job_is_one_transaction_and_round_trips_from_a_fresh_store() -> None:
    client = MemoryLowLevelDynamoClient()
    store = DynamoDBSellerControlStore(client=client, table_name=TABLE_NAME)
    job, receipt, work = create_job_with_work(store)

    request = client.transactions[0]
    assert len(request["ClientRequestToken"]) == 32
    assert len(request["TransactItems"]) == 4
    assert all(
        operation["Put"]["ConditionExpression"] == "attribute_not_exists(PK)"
        for operation in request["TransactItems"]
    )
    assert {
        operation["Put"]["Item"]["entity_type"]["S"] for operation in request["TransactItems"]
    } == {"CONTROL_JOB", "DOMAIN_EVENT", "COMMAND_RECEIPT", "WORK_REQUEST"}

    reconstructed = DynamoDBSellerControlStore(client=client, table_name=TABLE_NAME)
    assert reconstructed.get_job(job.job_id) == job
    assert reconstructed.get_work_request(job.job_id, work.work_request_id) == work
    assert (
        reconstructed.resolve_receipt(
            OWNER,
            receipt.command_type,
            job.job_id,
            receipt.idempotency_key_digest,
        )
        == receipt
    )


def test_command_transaction_binds_job_cas_and_round_trips_immutable_review() -> None:
    client = MemoryLowLevelDynamoClient()
    store = DynamoDBSellerControlStore(client=client, table_name=TABLE_NAME)
    initial, _receipt, work = create_job_with_work(store)
    current = advance_to_listing_drafted(store, initial)
    dispatched = dispatch_initial_work(store, current, work)
    commit = make_listing_commit(current, active_work=dispatched)

    assert store.commit_command(commit) == commit.receipt

    request = client.transactions[-1]
    assert len(request["TransactItems"]) == 6
    job_put = request["TransactItems"][0]["Put"]
    assert job_put["ConditionExpression"] == (
        "contract_version = :contract_version AND owner_id = :owner_id AND "
        "record_version = :record_version AND event_sequence = :event_sequence AND "
        "#state = :state AND review_version = :review_version AND "
        "cancellation_requested = :cancellation_requested AND "
        "payload = :expected_payload"
    )
    assert job_put["ExpressionAttributeValues"] == {
        ":contract_version": {"S": "2.0.0"},
        ":owner_id": {"S": OWNER},
        ":record_version": {"N": "2"},
        ":event_sequence": {"N": "3"},
        ":state": {"S": ControlJobState.LISTING_DRAFTED.value},
        ":review_version": {"N": "0"},
        ":cancellation_requested": {"BOOL": False},
        ":expected_payload": {"S": current.model_dump_json()},
    }
    assert {
        operation["Put"]["Item"]["entity_type"]["S"] for operation in request["TransactItems"]
    } == {"CONTROL_JOB", "DOMAIN_EVENT", "COMMAND_RECEIPT", "REVIEW", "WORK_REQUEST"}
    work_put = request["TransactItems"][-1]["Put"]
    assert work_put["ConditionExpression"] == "payload = :expected_payload"
    assert work_put["Item"]["work_status"] == {"S": WorkRequestStatus.COMPLETED.value}

    reconstructed = DynamoDBSellerControlStore(client=client, table_name=TABLE_NAME)
    assert reconstructed.get_job(current.job_id) == commit.updated
    assert reconstructed.get_review(current.job_id, 1) == commit.review
    assert commit.work_update is not None
    assert (
        reconstructed.get_work_request(current.job_id, dispatched.work_request_id)
        == (commit.work_update[1])
    )


def test_command_cannot_clear_active_work_without_settling_it_atomically() -> None:
    client = MemoryLowLevelDynamoClient()
    store = DynamoDBSellerControlStore(client=client, table_name=TABLE_NAME)
    initial, _receipt, work = create_job_with_work(store)
    current = advance_to_listing_drafted(store, initial)
    dispatched = dispatch_initial_work(store, current, work)
    complete = make_listing_commit(current, active_work=dispatched)
    orphaning = CommandCommit(
        current=complete.current,
        updated=complete.updated,
        event=complete.event,
        receipt=complete.receipt,
        review=complete.review,
        work_request=complete.work_request,
    )
    transaction_count = len(client.transactions)

    with pytest.raises(InvalidControlStateError, match="settling prior work"):
        store.commit_command(orphaning)

    assert len(client.transactions) == transaction_count
    assert store.get_job(current.job_id) == current


def test_transaction_cancellation_resolves_exact_replay_but_rejects_changed_request() -> None:
    client = MemoryLowLevelDynamoClient()
    store = DynamoDBSellerControlStore(client=client, table_name=TABLE_NAME)
    initial, _receipt, work = create_job_with_work(store)
    current = advance_to_listing_drafted(store, initial)
    dispatched = dispatch_initial_work(store, current, work)
    commit = make_listing_commit(current, active_work=dispatched)

    assert store.commit_command(commit) == commit.receipt
    transaction_count = len(client.transactions)

    # The job CAS is now stale, so DynamoDB cancels the replay transaction. The durable
    # receipt proves that this exact request already committed.
    assert store.commit_command(commit) == commit.receipt
    assert len(client.transactions) == transaction_count + 1

    changed_request = CommandCommit(
        current=commit.current,
        updated=commit.updated,
        event=commit.event,
        receipt=commit.receipt.model_copy(update={"request_fingerprint": "8" * 64}),
        review=commit.review,
        work_request=commit.work_request,
        work_update=commit.work_update,
    )
    with pytest.raises(IdempotencyConflictError, match="another request"):
        store.commit_command(changed_request)


def test_stale_cas_without_a_matching_receipt_is_a_concurrency_error_and_writes_nothing() -> None:
    client = MemoryLowLevelDynamoClient()
    store = DynamoDBSellerControlStore(client=client, table_name=TABLE_NAME)
    initial, _receipt, work = create_job_with_work(store)
    current = advance_to_listing_drafted(store, initial)
    dispatched = dispatch_initial_work(store, current, work)
    first = make_listing_commit(current, active_work=dispatched)
    store.commit_command(first)

    stale = make_listing_commit(current, active_work=dispatched)
    stale_work = WorkRequest.model_validate(
        {
            **stale.work_request.model_dump(mode="python"),
            "receipt_id": "receipt_stale_new_command",
        }
    )
    stale = CommandCommit(
        current=stale.current,
        updated=stale.updated,
        event=stale.event,
        receipt=make_receipt(
            stale.updated,
            receipt_id="receipt_stale_new_command",
            command_type="complete_preparation",
            key_digest="9" * 64,
            request_fingerprint="a" * 64,
            work_id=stale_work.work_request_id,
        ),
        review=stale.review,
        work_request=stale_work,
        work_update=stale.work_update,
    )
    keys_before = set(client.items)

    with pytest.raises(ConcurrentControlModificationError, match="job changed"):
        store.commit_command(stale)

    assert set(client.items) == keys_before
    assert store.resolve_receipt(OWNER, "complete_preparation", current.job_id, "9" * 64) is None


def test_due_work_claim_release_nudge_and_dispatch_are_payload_cas_updates() -> None:
    client = MemoryLowLevelDynamoClient()
    store = DynamoDBSellerControlStore(client=client, table_name=TABLE_NAME)
    future = NOW + timedelta(hours=1)
    job, _receipt, work = create_job_with_work(store, due_at=future)

    assert store.list_due_work(now=NOW) == ()
    nudged = store.nudge_pending_work(job.job_id, work.work_request_id, now=NOW)
    assert store.list_due_work(now=NOW) == (nudged,)
    assert client.put_requests[-1]["ConditionExpression"] == (
        "work_status = :pending AND payload = :expected_payload"
    )

    lease_expires = NOW + timedelta(minutes=2)
    claimed = store.claim_work(
        job.job_id,
        work.work_request_id,
        now=NOW,
        claim_id="claim_first",
        lease_expires_at=lease_expires,
    )
    assert claimed is not None
    assert claimed.status is WorkRequestStatus.CLAIMED
    assert claimed.attempt_count == 1
    assert store.list_due_work(now=NOW) == ()
    assert store.list_due_work(now=lease_expires) == (claimed,)
    assert client.put_requests[-1]["ConditionExpression"] == "payload = :expected_payload"

    next_dispatch = NOW + timedelta(minutes=5)
    released = store.release_work(
        job.job_id,
        work.work_request_id,
        claim_id="claim_first",
        next_dispatch_at=next_dispatch,
        error_code="STEP_FUNCTIONS_THROTTLED",
        now=NOW + timedelta(seconds=1),
    )
    assert released.status is WorkRequestStatus.PENDING
    assert released.last_error_code == "STEP_FUNCTIONS_THROTTLED"
    assert released.claim_id is None
    assert store.list_due_work(now=NOW) == ()

    nudged_again = store.nudge_pending_work(
        job.job_id,
        work.work_request_id,
        now=NOW + timedelta(seconds=2),
    )
    claimed_again = store.claim_work(
        job.job_id,
        work.work_request_id,
        now=NOW + timedelta(seconds=2),
        claim_id="claim_second",
        lease_expires_at=NOW + timedelta(minutes=3),
    )
    assert claimed_again is not None
    dispatched = store.mark_work_dispatched(
        job.job_id,
        work.work_request_id,
        claim_id="claim_second",
        execution_arn=("arn:aws:states:us-west-2:123456789012:execution:mr-lister-prepare:phase6"),
        now=NOW + timedelta(seconds=3),
    )

    assert nudged_again.status is WorkRequestStatus.PENDING
    assert dispatched.status is WorkRequestStatus.DISPATCHED
    assert dispatched.execution_arn is not None
    assert dispatched.claim_id is None
    assert dispatched.lease_expires_at is None
    assert "dispatch_pk" not in client.items[(f"JOB#{job.job_id}", f"WORK#{work.work_request_id}")]
    assert store.list_due_work(now=NOW + timedelta(days=1)) == ()


def test_work_update_conditional_failures_have_operation_specific_results() -> None:
    client = MemoryLowLevelDynamoClient()
    store = DynamoDBSellerControlStore(client=client, table_name=TABLE_NAME)
    job, _receipt, work = create_job_with_work(store, due_at=NOW + timedelta(minutes=5))

    client.fail_next_put_condition = True
    unchanged = store.nudge_pending_work(job.job_id, work.work_request_id, now=NOW)
    assert unchanged == work

    client.fail_next_put_condition = True
    assert (
        store.claim_work(
            job.job_id,
            work.work_request_id,
            now=NOW + timedelta(minutes=5),
            claim_id="claim_lost",
            lease_expires_at=NOW + timedelta(minutes=6),
        )
        is None
    )

    claimed = store.claim_work(
        job.job_id,
        work.work_request_id,
        now=NOW + timedelta(minutes=5),
        claim_id="claim_current",
        lease_expires_at=NOW + timedelta(minutes=6),
    )
    assert claimed is not None
    client.fail_next_put_condition = True
    with pytest.raises(ConcurrentControlModificationError, match="work request changed"):
        store.mark_work_dispatched(
            job.job_id,
            work.work_request_id,
            claim_id="claim_current",
            execution_arn=(
                "arn:aws:states:us-west-2:123456789012:execution:mr-lister-prepare:lost"
            ),
            now=NOW + timedelta(minutes=5, seconds=1),
        )


def test_defer_claimed_work_retains_claim_identity_and_becomes_due_at_backoff() -> None:
    client = MemoryLowLevelDynamoClient()
    store = DynamoDBSellerControlStore(client=client, table_name=TABLE_NAME)
    job, _receipt, work = create_job_with_work(store)
    initial_lease = NOW + timedelta(minutes=1)
    claimed = store.claim_work(
        job.job_id,
        work.work_request_id,
        now=NOW,
        claim_id="claim_handshake",
        lease_expires_at=initial_lease,
    )
    assert claimed is not None
    retry_at = NOW + timedelta(minutes=5)

    deferred = store.defer_claimed_work(
        job.job_id,
        work.work_request_id,
        claim_id="claim_handshake",
        retry_at=retry_at,
        error_code="EXECUTION_NOT_VISIBLE",
        now=NOW + timedelta(seconds=1),
    )

    assert deferred.status is WorkRequestStatus.CLAIMED
    assert deferred.claim_id == claimed.claim_id
    assert deferred.lease_expires_at == retry_at
    assert deferred.lease_expires_at > initial_lease
    assert deferred.last_error_code == "EXECUTION_NOT_VISIBLE"
    assert deferred.attempt_count == claimed.attempt_count
    assert (
        deferred.work_request_id,
        deferred.owner_id,
        deferred.job_id,
        deferred.receipt_id,
        deferred.work_type,
        deferred.review_version,
        deferred.input_fingerprint,
        deferred.execution_name,
        deferred.created_at,
    ) == (
        claimed.work_request_id,
        claimed.owner_id,
        claimed.job_id,
        claimed.receipt_id,
        claimed.work_type,
        claimed.review_version,
        claimed.input_fingerprint,
        claimed.execution_name,
        claimed.created_at,
    )
    assert store.list_due_work(now=retry_at - timedelta(seconds=1)) == ()
    assert store.list_due_work(now=retry_at) == (deferred,)
    assert store.get_work_request(job.job_id, work.work_request_id) == deferred


def test_mark_dispatched_returns_completed_when_worker_wins_payload_cas() -> None:
    client = MemoryLowLevelDynamoClient()
    store = DynamoDBSellerControlStore(client=client, table_name=TABLE_NAME)
    job, _receipt, work = create_job_with_work(store)
    claimed = store.claim_work(
        job.job_id,
        work.work_request_id,
        now=NOW,
        claim_id="claim_mark_race",
        lease_expires_at=NOW + timedelta(minutes=1),
    )
    assert claimed is not None
    completed = completed_concurrent_winner(claimed)
    client.arrange_concurrent_work_winner(completed)

    result = store.mark_work_dispatched(
        job.job_id,
        work.work_request_id,
        claim_id="claim_mark_race",
        execution_arn=("arn:aws:states:us-west-2:123456789012:execution:mr-lister-prepare:race"),
        now=NOW + timedelta(seconds=1),
    )

    assert result == completed
    assert store.get_work_request(job.job_id, work.work_request_id) == completed


def test_defer_returns_completed_when_worker_wins_payload_cas() -> None:
    client = MemoryLowLevelDynamoClient()
    store = DynamoDBSellerControlStore(client=client, table_name=TABLE_NAME)
    job, _receipt, work = create_job_with_work(store)
    claimed = store.claim_work(
        job.job_id,
        work.work_request_id,
        now=NOW,
        claim_id="claim_defer_race",
        lease_expires_at=NOW + timedelta(minutes=1),
    )
    assert claimed is not None
    completed = completed_concurrent_winner(claimed)
    client.arrange_concurrent_work_winner(completed)

    result = store.defer_claimed_work(
        job.job_id,
        work.work_request_id,
        claim_id="claim_defer_race",
        retry_at=NOW + timedelta(minutes=5),
        error_code="EXECUTION_NOT_VISIBLE",
        now=NOW + timedelta(seconds=1),
    )

    assert result == completed
    assert store.get_work_request(job.job_id, work.work_request_id) == completed
