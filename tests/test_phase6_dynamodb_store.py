from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from typing import Any

import pytest
from botocore.exceptions import ClientError
from pydantic import ValidationError

from mr_lister.control.dispatch import deterministic_execution_name, work_input_fingerprint
from mr_lister.control.dynamodb import DynamoDBSellerControlStore
from mr_lister.control.errors import (
    ConcurrentControlModificationError,
    IdempotencyConflictError,
    InvalidControlStateError,
    NotFoundError,
)
from mr_lister.control.models import (
    CommandReceipt,
    CommandResponse,
    ControlJobRecord,
    ControlJobState,
    DomainEvent,
    ProductVariantEvidence,
    ProviderCallPermitStatus,
    ReviewActor,
    ReviewContent,
    ReviewDecision,
    ReviewDecisionRecord,
    SourceArtifactRecord,
    WorkRequest,
    WorkRequestStatus,
    WorkType,
)
from mr_lister.control.source_artwork import source_artifact_fingerprint
from mr_lister.control.store import CommandCommit
from mr_lister.control.upload_models import (
    UploadCommandType,
    UploadCompletionCommit,
    UploadIntent,
    UploadIntentCommit,
    UploadIntentStatus,
    UploadReceipt,
)
from mr_lister.control.worker_commands import (
    BeginProviderUploadCommand,
    BeginProviderWriteCommand,
    ProductSyncObservation,
    RecordPricingSuccessCommand,
    RecordProductSyncSuccessCommand,
    RecordProviderUploadSuccessCommand,
    UploadedArtworkObservation,
)
from mr_lister.control.worker_service import WorkerControlService
from mr_lister.production.economics import (
    ProductCostEvidence,
    ProductVariantCostEvidence,
    estimate_etsy_us_standard_proceeds,
)
from mr_lister.production.printify_shipping import parse_standard_us_shipping

NOW = datetime(2026, 8, 21, 12, 0, tzinfo=UTC)
OWNER = "a" * 64
TABLE_NAME = "MrListerPhase6Control"


def _source_material(*, job_id: str, owner_id: str, created_at: datetime) -> dict[str, Any]:
    return {
        "job_id": job_id,
        "owner_id": owner_id,
        "bucket": "mr-lister-phase6-artifacts-test",
        "object_key": (f"private/owners/{owner_id}/jobs/{job_id}/source/source.png"),
        "version_id": "source-version-1",
        "content_sha256": "f" * 64,
        "size_bytes": 128,
        "media_type": "image/png",
        "product_profile_id": "profile_test",
        "product_profile_version": 1,
        "product_profile_fingerprint": "3" * 64,
        "created_at": created_at,
    }


def _source_fingerprint(*, job_id: str, owner_id: str, created_at: datetime) -> str:
    return source_artifact_fingerprint(
        **_source_material(job_id=job_id, owner_id=owner_id, created_at=created_at)
    )


SOURCE_FP = _source_fingerprint(job_id="job_phase6_dynamo", owner_id=OWNER, created_at=NOW)


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
        if any(not self._transaction_condition_holds(operation) for operation in operations):
            raise _client_error("TransactionCanceledException", "TransactWriteItems")
        for operation in operations:
            if "Put" not in operation:
                continue
            item = operation["Put"]["Item"]
            self.items[self._key(item)] = item

    def _transaction_condition_holds(self, operation: dict[str, Any]) -> bool:
        if "Put" in operation:
            return self._condition_holds(operation["Put"])
        if "ConditionCheck" not in operation:
            raise AssertionError(f"Unsupported fake transaction operation: {operation}")
        check = operation["ConditionCheck"]
        key = check["Key"]
        existing = self.items.get((key["PK"]["S"], key["SK"]["S"]))
        if existing is None:
            return False
        condition = check.get("ConditionExpression")
        values = check.get("ExpressionAttributeValues", {})
        if condition == "payload = :expected_job":
            return existing.get("payload") == values[":expected_job"]
        if condition == "payload = :expected_work":
            return existing.get("payload") == values[":expected_work"]
        raise AssertionError(f"Unsupported fake condition check: {condition}")

    def get_item(self, **request: Any) -> dict[str, Any]:
        key = request["Key"]
        item = self.items.get((key["PK"]["S"], key["SK"]["S"]))
        return {} if item is None else {"Item": item}

    def query(self, **request: Any) -> dict[str, Any]:
        self.query_requests.append(request)
        values = request["ExpressionAttributeValues"]
        if request["IndexName"] == "OwnerJobsIndex":
            owner_jobs_pk = values[":owner_jobs_pk"]["S"]
            candidates = [
                item
                for item in self.items.values()
                if item.get("owner_jobs_pk", {}).get("S") == owner_jobs_pk
            ]
            candidates.sort(
                key=lambda item: item["owner_jobs_sk"]["S"],
                reverse=not request["ScanIndexForward"],
            )
            start = 0
            exclusive = request.get("ExclusiveStartKey")
            if exclusive is not None:
                key = (exclusive["PK"]["S"], exclusive["SK"]["S"])
                matches = [index for index, item in enumerate(candidates) if self._key(item) == key]
                start = matches[0] + 1 if matches else len(candidates)
            selected = candidates[start : start + request["Limit"]]
            response: dict[str, Any] = {"Items": selected}
            if start + len(selected) < len(candidates):
                response["LastEvaluatedKey"] = {
                    name: selected[-1][name]
                    for name in ("PK", "SK", "owner_jobs_pk", "owner_jobs_sk")
                }
            return response
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
        if ":upload_status" in values:
            expected_attributes = {
                "owner_id": values[":owner_id"],
                "record_version": values[":record_version"],
                "upload_status": values[":upload_status"],
                "payload": values[":expected_payload"],
            }
            return all(existing.get(name) == value for name, value in expected_attributes.items())
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


def store_as_legacy_job_payload(
    client: MemoryLowLevelDynamoClient,
    job_id: str,
) -> str:
    """Physically remove Phase 7.1 fields from an existing Dynamo payload fixture."""

    item = client.items[(f"JOB#{job_id}", "META")]
    payload = json.loads(item["payload"]["S"])
    payload["approval_decision_id"] = None
    payload["publication_aggregate_id"] = None
    item["payload"] = {"S": json.dumps(payload, separators=(",", ":"))}

    stored = json.loads(item["payload"]["S"])
    assert stored.pop("approval_decision_id") is None
    assert stored.pop("publication_aggregate_id") is None
    legacy_payload = json.dumps(stored, separators=(",", ":"))
    item["payload"] = {"S": legacy_payload}
    return legacy_payload


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
        source_artifact_fingerprint=SOURCE_FP,
        review_version=review_version,
        review_fingerprint=review_fingerprint,
        review_validated=review_validated,
        active_work_request_id=active_work_request_id,
        created_at=NOW,
        updated_at=updated_at,
    )


def make_source(job: ControlJobRecord) -> SourceArtifactRecord:
    material = _source_material(
        job_id=job.job_id,
        owner_id=job.owner_id,
        created_at=job.created_at,
    )
    return SourceArtifactRecord(
        fingerprint=source_artifact_fingerprint(**material),
        **material,
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
        source_artifact=make_source(job),
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


UPLOAD_ID = "upload_phase6_dynamo"
UPLOAD_JOB_ID = "job_phase6_dynamo_upload"
UPLOAD_WORK_ID = "work_phase6_dynamo_prepare"
UPLOAD_BUCKET = "mr-lister-phase6-artifacts-test"
UPLOAD_KEY = f"private/owners/{OWNER}/jobs/{UPLOAD_JOB_ID}/source/source.png"


def make_upload_create_commit() -> UploadIntentCommit:
    intent = UploadIntent(
        owner_id=OWNER,
        upload_id=UPLOAD_ID,
        job_id=UPLOAD_JOB_ID,
        filename="seller-art.png",
        content_type="image/png",
        content_sha256="b" * 64,
        size_bytes=256,
        bucket=UPLOAD_BUCKET,
        object_key=UPLOAD_KEY,
        product_profile_id="profile_test",
        product_profile_version=1,
        product_profile_fingerprint="c" * 64,
        authorization_generation=1,
        authorization_issued_at=NOW,
        authorization_expires_at=NOW + timedelta(minutes=5),
        intent_expires_at=NOW + timedelta(days=1),
        created_at=NOW,
        updated_at=NOW,
    )
    receipt = UploadReceipt(
        receipt_id="receipt_upload_create",
        owner_id=OWNER,
        upload_id=UPLOAD_ID,
        job_id=UPLOAD_JOB_ID,
        command_type=UploadCommandType.CREATE_UPLOAD,
        idempotency_key_digest="d" * 64,
        request_fingerprint="e" * 64,
        status=UploadIntentStatus.OPEN,
        record_version=0,
        created_at=NOW,
    )
    return UploadIntentCommit(updated=intent, receipt=receipt)


def make_upload_completion_commit(current: UploadIntent) -> UploadCompletionCommit:
    completed_at = NOW + timedelta(minutes=1)
    source_material = {
        "job_id": current.job_id,
        "owner_id": current.owner_id,
        "bucket": current.bucket,
        "object_key": current.object_key,
        "version_id": "source-version-upload-1",
        "content_sha256": current.content_sha256,
        "size_bytes": current.size_bytes,
        "media_type": current.content_type,
        "product_profile_id": current.product_profile_id,
        "product_profile_version": current.product_profile_version,
        "product_profile_fingerprint": current.product_profile_fingerprint,
        "created_at": completed_at,
    }
    source = SourceArtifactRecord(
        fingerprint=source_artifact_fingerprint(**source_material),
        **source_material,
    )
    receipt = UploadReceipt(
        receipt_id="receipt_upload_complete",
        owner_id=current.owner_id,
        upload_id=current.upload_id,
        job_id=current.job_id,
        command_type=UploadCommandType.COMPLETE_UPLOAD,
        idempotency_key_digest="f" * 64,
        request_fingerprint="1" * 64,
        status=UploadIntentStatus.COMPLETED,
        record_version=current.record_version + 1,
        work_request_id=UPLOAD_WORK_ID,
        created_at=completed_at,
    )
    completed = UploadIntent.model_validate(
        {
            **current.model_dump(mode="python"),
            "record_version": current.record_version + 1,
            "status": UploadIntentStatus.COMPLETED,
            "completed_at": completed_at,
            "completed_source_artifact_fingerprint": source.fingerprint,
            "completed_version_id": source.version_id,
            "completion_receipt_id": receipt.receipt_id,
            "updated_at": completed_at,
        }
    )
    job = ControlJobRecord(
        owner_id=current.owner_id,
        job_id=current.job_id,
        state=ControlJobState.INTAKE_VALIDATED,
        event_sequence=1,
        source_artifact_fingerprint=source.fingerprint,
        active_work_request_id=UPLOAD_WORK_ID,
        created_at=completed_at,
        updated_at=completed_at,
    )
    event = DomainEvent(
        job_id=current.job_id,
        sequence=1,
        name="UPLOAD_COMPLETED",
        occurred_at=completed_at,
    )
    work = WorkRequest(
        work_request_id=UPLOAD_WORK_ID,
        owner_id=current.owner_id,
        job_id=current.job_id,
        receipt_id=receipt.receipt_id,
        work_type=WorkType.PREPARE,
        input_fingerprint=work_input_fingerprint(
            work_type=WorkType.PREPARE,
            job_id=current.job_id,
            work_request_id=UPLOAD_WORK_ID,
        ),
        execution_name=deterministic_execution_name(UPLOAD_WORK_ID),
        next_dispatch_at=completed_at,
        created_at=completed_at,
        updated_at=completed_at,
    )
    return UploadCompletionCommit(
        intent=UploadIntentCommit(current=current, updated=completed, receipt=receipt),
        job=job,
        source_artifact=source,
        event=event,
        work_request=work,
    )


def make_upload_cancel_commit(current: UploadIntent) -> UploadIntentCommit:
    cancelled_at = NOW + timedelta(seconds=30)
    receipt = UploadReceipt(
        receipt_id="receipt_upload_cancel",
        owner_id=current.owner_id,
        upload_id=current.upload_id,
        job_id=current.job_id,
        command_type=UploadCommandType.CANCEL_UPLOAD,
        idempotency_key_digest="2" * 64,
        request_fingerprint="3" * 64,
        status=UploadIntentStatus.CANCELLED,
        record_version=current.record_version + 1,
        created_at=cancelled_at,
    )
    cancelled = UploadIntent.model_validate(
        {
            **current.model_dump(mode="python"),
            "record_version": current.record_version + 1,
            "status": UploadIntentStatus.CANCELLED,
            "cancelled_at": cancelled_at,
            "cancellation_receipt_id": receipt.receipt_id,
            "updated_at": cancelled_at,
        }
    )
    return UploadIntentCommit(current=current, updated=cancelled, receipt=receipt)


def test_create_job_is_one_transaction_and_round_trips_from_a_fresh_store() -> None:
    client = MemoryLowLevelDynamoClient()
    store = DynamoDBSellerControlStore(client=client, table_name=TABLE_NAME)
    job, receipt, work = create_job_with_work(store)

    request = client.transactions[0]
    assert len(request["ClientRequestToken"]) == 32
    assert len(request["TransactItems"]) == 5
    assert all(
        operation["Put"]["ConditionExpression"] == "attribute_not_exists(PK)"
        for operation in request["TransactItems"]
    )
    assert {
        operation["Put"]["Item"]["entity_type"]["S"] for operation in request["TransactItems"]
    } == {
        "CONTROL_JOB",
        "DOMAIN_EVENT",
        "COMMAND_RECEIPT",
        "SOURCE_ARTIFACT",
        "WORK_REQUEST",
    }

    reconstructed = DynamoDBSellerControlStore(client=client, table_name=TABLE_NAME)
    assert reconstructed.get_job(job.job_id) == job
    assert reconstructed.get_source_artifact(job.job_id) == make_source(job)
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


def test_review_decision_lookup_round_trips_and_rejects_cross_job_payloads() -> None:
    client = MemoryLowLevelDynamoClient()
    store = DynamoDBSellerControlStore(client=client, table_name=TABLE_NAME)
    job, _receipt, _work = create_job_with_work(store)
    other_job = job.model_copy(update={"job_id": "job_phase6_dynamo_other"})
    other_meta = dict(client.items[(f"JOB#{job.job_id}", "META")])
    other_meta.update(
        {
            "PK": {"S": f"JOB#{other_job.job_id}"},
            "payload": {"S": other_job.model_dump_json()},
        }
    )
    client.items[(f"JOB#{other_job.job_id}", "META")] = other_meta

    decision = ReviewDecisionRecord(
        decision_id="decision_dynamo_lookup",
        job_id=job.job_id,
        actor_owner_id=OWNER,
        decision=ReviewDecision.REVISE,
        review_version=1,
        review_fingerprint="b" * 64,
        command_receipt_id="receipt_dynamo_lookup",
        decided_at=NOW,
    )
    for partition_job_id in (job.job_id, other_job.job_id):
        client.items[(f"JOB#{partition_job_id}", f"DECISION#{decision.decision_id}")] = {
            "PK": {"S": f"JOB#{partition_job_id}"},
            "SK": {"S": f"DECISION#{decision.decision_id}"},
            "entity_type": {"S": "REVIEW_DECISION"},
            "contract_version": {"S": decision.contract_version},
            "payload": {"S": decision.model_dump_json()},
        }

    assert store.get_review_decision(job.job_id, decision.decision_id) == decision
    with pytest.raises(NotFoundError, match="review decision"):
        store.get_review_decision(other_job.job_id, decision.decision_id)
    with pytest.raises(NotFoundError, match="review decision"):
        store.get_review_decision(job.job_id, "decision_unknown")


def test_owner_scoped_job_read_checks_raw_owner_before_payload_and_binds_partition() -> None:
    client = MemoryLowLevelDynamoClient()
    store = DynamoDBSellerControlStore(client=client, table_name=TABLE_NAME)
    job, _receipt, _work = create_job_with_work(store)
    item = client.items[(f"JOB#{job.job_id}", "META")]
    item["payload"] = {"S": "{"}

    with pytest.raises(NotFoundError):
        store.get_job_for_owner("b" * 64, job.job_id)
    with pytest.raises(ValidationError):
        store.get_job_for_owner(OWNER, job.job_id)

    item["payload"] = {"S": job.model_copy(update={"job_id": "another_job"}).model_dump_json()}
    with pytest.raises(NotFoundError):
        store.get_job_for_owner(OWNER, job.job_id)


def test_owner_jobs_index_is_bounded_recent_and_uses_an_opaque_cursor() -> None:
    client = MemoryLowLevelDynamoClient()
    store = DynamoDBSellerControlStore(client=client, table_name=TABLE_NAME)
    first, _receipt, _work = create_job_with_work(store)

    second = first.model_copy(
        update={
            "job_id": "job_phase6_dynamo_second",
            "active_work_request_id": "work_prepare_second",
            "created_at": NOW + timedelta(minutes=1),
            "updated_at": NOW + timedelta(minutes=1),
            "source_artifact_fingerprint": _source_fingerprint(
                job_id="job_phase6_dynamo_second",
                owner_id=OWNER,
                created_at=NOW + timedelta(minutes=1),
            ),
        }
    )
    second_receipt = make_receipt(
        second,
        receipt_id="receipt_create_second",
        command_type="create_job",
        key_digest="8" * 64,
        request_fingerprint="9" * 64,
        work_id="work_prepare_second",
    )
    second_work = make_work(second, second_receipt, due_at=second.updated_at)
    second_source = make_source(second)
    store.create_job(
        job=second,
        event=make_event(second, "JOB_CREATED"),
        receipt=second_receipt,
        work_request=second_work,
        source_artifact=second_source,
    )

    page = store.list_jobs_for_owner(OWNER, limit=1)
    assert page.jobs == (second,)
    assert page.next_cursor is not None
    assert second.job_id not in page.next_cursor
    assert client.query_requests[-1]["IndexName"] == "OwnerJobsIndex"
    assert client.query_requests[-1]["ScanIndexForward"] is False

    following = store.list_jobs_for_owner(OWNER, limit=1, cursor=page.next_cursor)
    assert following.jobs == (first,)
    assert following.next_cursor is None


def test_upload_intent_owner_read_rejects_raw_owner_before_parsing_payload() -> None:
    client = MemoryLowLevelDynamoClient()
    store = DynamoDBSellerControlStore(client=client, table_name=TABLE_NAME)
    creation = make_upload_create_commit()
    store.commit_upload_intent(creation)
    item = client.items[(f"UPLOAD#{UPLOAD_ID}", "META")]
    item["payload"] = {"S": "{"}

    with pytest.raises(NotFoundError):
        store.get_upload_intent_for_owner("9" * 64, UPLOAD_ID)
    with pytest.raises(ValidationError):
        store.get_upload_intent_for_owner(OWNER, UPLOAD_ID)

    item["payload"] = {
        "S": creation.updated.model_copy(update={"upload_id": "upload_other"}).model_dump_json()
    }
    with pytest.raises(NotFoundError):
        store.get_upload_intent_for_owner(OWNER, UPLOAD_ID)


def test_upload_create_is_atomic_and_receipt_replay_is_fingerprint_bound() -> None:
    client = MemoryLowLevelDynamoClient()
    store = DynamoDBSellerControlStore(client=client, table_name=TABLE_NAME)
    creation = make_upload_create_commit()

    assert store.commit_upload_intent(creation) == creation.receipt
    request = client.transactions[-1]
    assert len(request["ClientRequestToken"]) == 32
    assert len(request["TransactItems"]) == 2
    assert {
        operation["Put"]["Item"]["entity_type"]["S"] for operation in request["TransactItems"]
    } == {"UPLOAD_INTENT", "UPLOAD_RECEIPT"}
    intent_item = next(
        operation["Put"]["Item"]
        for operation in request["TransactItems"]
        if operation["Put"]["Item"]["entity_type"]["S"] == "UPLOAD_INTENT"
    )
    assert intent_item["expires_at"] == {
        "N": str(int(creation.updated.intent_expires_at.timestamp()))
    }
    receipt_item = next(
        operation["Put"]["Item"]
        for operation in request["TransactItems"]
        if operation["Put"]["Item"]["entity_type"]["S"] == "UPLOAD_RECEIPT"
    )
    assert receipt_item["expires_at"] == {
        "N": str(int((creation.receipt.created_at + timedelta(days=90)).timestamp()))
    }
    assert all(
        operation["Put"]["ConditionExpression"] == "attribute_not_exists(PK)"
        for operation in request["TransactItems"]
    )

    reconstructed = DynamoDBSellerControlStore(client=client, table_name=TABLE_NAME)
    assert reconstructed.get_upload_intent_for_owner(OWNER, UPLOAD_ID) == creation.updated
    assert (
        reconstructed.resolve_upload_receipt(
            OWNER,
            UploadCommandType.CREATE_UPLOAD.value,
            UPLOAD_ID,
            creation.receipt.idempotency_key_digest,
        )
        == creation.receipt
    )

    assert store.commit_upload_intent(creation) == creation.receipt
    changed = UploadIntentCommit(
        updated=creation.updated,
        receipt=creation.receipt.model_copy(update={"request_fingerprint": "4" * 64}),
    )
    with pytest.raises(IdempotencyConflictError, match="another upload request"):
        store.commit_upload_intent(changed)


def test_upload_completion_is_one_six_item_intent_cas_and_round_trips_graph() -> None:
    client = MemoryLowLevelDynamoClient()
    store = DynamoDBSellerControlStore(client=client, table_name=TABLE_NAME)
    creation = make_upload_create_commit()
    store.commit_upload_intent(creation)
    completion = make_upload_completion_commit(creation.updated)

    assert store.complete_upload(completion) == completion.intent.receipt

    request = client.transactions[-1]
    assert len(request["ClientRequestToken"]) == 32
    assert len(request["TransactItems"]) == 6
    assert [
        operation["Put"]["Item"]["entity_type"]["S"] for operation in request["TransactItems"]
    ] == [
        "UPLOAD_INTENT",
        "CONTROL_JOB",
        "SOURCE_ARTIFACT",
        "DOMAIN_EVENT",
        "UPLOAD_RECEIPT",
        "WORK_REQUEST",
    ]
    intent_put = request["TransactItems"][0]["Put"]
    assert intent_put["Item"]["expires_at"] == {
        "N": str(int((completion.intent.updated.completed_at + timedelta(days=90)).timestamp()))
    }
    assert intent_put["ConditionExpression"] == (
        "owner_id = :owner_id AND record_version = :record_version AND "
        "upload_status = :upload_status AND payload = :expected_payload"
    )
    assert intent_put["ExpressionAttributeValues"] == {
        ":owner_id": {"S": OWNER},
        ":record_version": {"N": "0"},
        ":upload_status": {"S": UploadIntentStatus.OPEN.value},
        ":expected_payload": {"S": creation.updated.model_dump_json()},
    }
    completion_receipt_item = request["TransactItems"][4]["Put"]["Item"]
    assert completion_receipt_item["expires_at"] == {
        "N": str(int((completion.intent.receipt.created_at + timedelta(days=90)).timestamp()))
    }
    assert all(
        operation["Put"]["ConditionExpression"] == "attribute_not_exists(PK)"
        for operation in request["TransactItems"][1:]
    )
    job_item = request["TransactItems"][1]["Put"]["Item"]
    assert job_item["owner_id"] == {"S": OWNER}
    assert job_item["owner_jobs_pk"] == {"S": f"OWNER#{OWNER}"}
    assert job_item["PK"] == {"S": f"JOB#{UPLOAD_JOB_ID}"}
    assert request["TransactItems"][2]["Put"]["Item"]["SK"] == {"S": "SOURCE"}
    assert request["TransactItems"][3]["Put"]["Item"]["SK"] == {"S": "EVENT#00000000000000000001"}
    work_item = request["TransactItems"][5]["Put"]["Item"]
    assert work_item["work_status"] == {"S": WorkRequestStatus.PENDING.value}
    assert work_item["dispatch_pk"] == {"S": "WORK_DUE#0"}

    reconstructed = DynamoDBSellerControlStore(client=client, table_name=TABLE_NAME)
    assert reconstructed.get_upload_intent_for_owner(OWNER, UPLOAD_ID) == completion.intent.updated
    assert reconstructed.get_job_for_owner(OWNER, UPLOAD_JOB_ID) == completion.job
    assert reconstructed.get_source_artifact(UPLOAD_JOB_ID) == completion.source_artifact
    event_item = client.items[(f"JOB#{UPLOAD_JOB_ID}", "EVENT#00000000000000000001")]
    assert DomainEvent.model_validate_json(event_item["payload"]["S"]) == completion.event
    assert reconstructed.get_work_request(UPLOAD_JOB_ID, UPLOAD_WORK_ID) == completion.work_request
    assert (
        reconstructed.resolve_upload_receipt(
            OWNER,
            UploadCommandType.COMPLETE_UPLOAD.value,
            UPLOAD_ID,
            completion.intent.receipt.idempotency_key_digest,
        )
        == completion.intent.receipt
    )


def test_upload_completion_replay_is_exact_and_changed_request_conflicts() -> None:
    client = MemoryLowLevelDynamoClient()
    store = DynamoDBSellerControlStore(client=client, table_name=TABLE_NAME)
    creation = make_upload_create_commit()
    store.commit_upload_intent(creation)
    completion = make_upload_completion_commit(creation.updated)
    store.complete_upload(completion)

    assert store.complete_upload(completion) == completion.intent.receipt
    changed = completion.model_copy(
        update={
            "intent": completion.intent.model_copy(
                update={
                    "receipt": completion.intent.receipt.model_copy(
                        update={"request_fingerprint": "5" * 64}
                    )
                }
            )
        }
    )
    with pytest.raises(IdempotencyConflictError, match="another upload request"):
        store.complete_upload(changed)


def test_cancelled_upload_wins_stale_completion_cas_without_partial_graph() -> None:
    client = MemoryLowLevelDynamoClient()
    store = DynamoDBSellerControlStore(client=client, table_name=TABLE_NAME)
    creation = make_upload_create_commit()
    store.commit_upload_intent(creation)
    completion = make_upload_completion_commit(creation.updated)
    cancellation = make_upload_cancel_commit(creation.updated)
    store.commit_upload_intent(cancellation)
    keys_before = set(client.items)

    with pytest.raises(ConcurrentControlModificationError, match="upload changed"):
        store.complete_upload(completion)

    assert set(client.items) == keys_before
    assert store.get_upload_intent_for_owner(OWNER, UPLOAD_ID) == cancellation.updated
    assert not any(key[0] == f"JOB#{UPLOAD_JOB_ID}" for key in client.items)
    assert (
        store.resolve_upload_receipt(
            OWNER,
            UploadCommandType.COMPLETE_UPLOAD.value,
            UPLOAD_ID,
            completion.intent.receipt.idempotency_key_digest,
        )
        is None
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
    assert "recovery_pk" not in work_put["Item"]
    assert "recovery_sk" not in work_put["Item"]

    reconstructed = DynamoDBSellerControlStore(client=client, table_name=TABLE_NAME)
    assert reconstructed.get_job(current.job_id) == commit.updated
    assert reconstructed.get_review(current.job_id, 1) == commit.review
    assert commit.work_update is not None
    assert (
        reconstructed.get_work_request(current.job_id, dispatched.work_request_id)
        == (commit.work_update[1])
    )


def test_legacy_raw_job_payload_round_trips_and_completes_legal_command_cas() -> None:
    client = MemoryLowLevelDynamoClient()
    store = DynamoDBSellerControlStore(client=client, table_name=TABLE_NAME)
    initial, _receipt, _work = create_job_with_work(store)
    legacy_payload = store_as_legacy_job_payload(client, initial.job_id)

    reloaded = store.get_job(initial.job_id)
    assert reloaded.approval_decision_id is None
    assert reloaded.publication_aggregate_id is None
    assert reloaded.model_dump_json() == legacy_payload

    drafted = advance_to_listing_drafted(store, reloaded)

    assert drafted.state is ControlJobState.LISTING_DRAFTED
    first_job_put = client.transactions[1]["TransactItems"][0]["Put"]
    assert first_job_put["ExpressionAttributeValues"][":expected_payload"] == {"S": legacy_payload}
    assert store.get_job(initial.job_id) == drafted


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
    dispatched_item = client.items[(f"JOB#{job.job_id}", f"WORK#{work.work_request_id}")]
    assert "dispatch_pk" not in dispatched_item
    assert dispatched_item["recovery_pk"] == {"S": "WORK_RECOVERY#0"}
    assert dispatched_item["recovery_sk"] == {
        "S": f"{int(dispatched.updated_at.timestamp()):020d}#{work.work_request_id}"
    }
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


def seed_provider_permit(
    store: DynamoDBSellerControlStore,
    *,
    dispatch_sync_work: bool,
) -> tuple[ControlJobRecord, WorkRequest, str]:
    initial, _receipt, prepare_work = create_job_with_work(store)
    drafted = advance_to_listing_drafted(store, initial)
    dispatched_prepare = dispatch_initial_work(store, drafted, prepare_work)
    listing_commit = make_listing_commit(drafted, active_work=dispatched_prepare)
    store.commit_command(listing_commit)
    assert listing_commit.work_request is not None
    syncing = listing_commit.updated
    sync_work = listing_commit.work_request
    claimed = store.claim_work(
        syncing.job_id,
        sync_work.work_request_id,
        now=NOW + timedelta(seconds=1),
        claim_id="claim_product_sync",
        lease_expires_at=NOW + timedelta(minutes=2),
    )
    assert claimed is not None
    active = claimed
    if dispatch_sync_work:
        active = store.mark_work_dispatched(
            syncing.job_id,
            sync_work.work_request_id,
            claim_id="claim_product_sync",
            execution_arn=(
                "arn:aws:states:us-west-2:123456789012:execution:mr-lister-sync:provider-permit"
            ),
            now=NOW + timedelta(seconds=1),
        )
    correlation = sha256(f"mr-lister:provider-draft:{syncing.job_id}".encode()).hexdigest()[:24]
    worker = WorkerControlService(
        store=store,
        clock=lambda: NOW + timedelta(seconds=2),
    )
    source = store.get_source_artifact(syncing.job_id)
    file_name = worker.upload_file_name(syncing.job_id, source.content_sha256)
    worker.begin_provider_upload(
        BeginProviderUploadCommand(
            job_id=syncing.job_id,
            work_request_id=active.work_request_id,
            expected_record_version=syncing.record_version,
            source_artifact_fingerprint=source.fingerprint,
            file_name=file_name,
        )
    )
    upload_claim = store.get_job(syncing.job_id)
    upload_attempt_id = upload_claim.provider_upload_attempt_id or ""
    assert (
        worker.authorize_provider_upload(job_id=syncing.job_id, attempt_id=upload_attempt_id)
        is not None
    )
    worker.record_provider_upload_success(
        RecordProviderUploadSuccessCommand(
            job_id=syncing.job_id,
            work_request_id=active.work_request_id,
            expected_record_version=upload_claim.record_version,
            attempt_id=upload_attempt_id,
            observation=UploadedArtworkObservation(
                image_id="printify_image_dynamo",
                file_name=file_name,
                width=3021,
                height=3927,
                size_bytes=source.size_bytes,
            ),
        )
    )
    uploading = store.get_job(syncing.job_id)
    worker.begin_provider_write(
        BeginProviderWriteCommand(
            job_id=syncing.job_id,
            work_request_id=active.work_request_id,
            expected_record_version=uploading.record_version,
            image_id="printify_image_dynamo",
            target_payload_fingerprint="d" * 64,
            correlation_token=f"ml-{correlation}",
        )
    )
    claimed_job = store.get_job(syncing.job_id)
    assert claimed_job.provider_write_attempt_id is not None
    return claimed_job, active, claimed_job.provider_write_attempt_id


@pytest.mark.parametrize("dispatch_sync_work", (False, True))
def test_provider_permit_transaction_consumes_for_exact_claimed_or_dispatched_work(
    dispatch_sync_work: bool,
) -> None:
    client = MemoryLowLevelDynamoClient()
    store = DynamoDBSellerControlStore(client=client, table_name=TABLE_NAME)
    job, work, attempt_id = seed_provider_permit(
        store,
        dispatch_sync_work=dispatch_sync_work,
    )

    consumed = store.consume_provider_call_permit(
        job,
        work,
        attempt_id,
        now=NOW + timedelta(seconds=3),
    )

    assert consumed is not None
    assert consumed.status is ProviderCallPermitStatus.CONSUMED
    assert consumed.consumed_work_request_id == work.work_request_id
    request = client.transactions[-1]
    assert len(request["TransactItems"]) == 3
    job_check = request["TransactItems"][0]["ConditionCheck"]
    work_check = request["TransactItems"][1]["ConditionCheck"]
    permit_put = request["TransactItems"][2]["Put"]
    assert job_check["Key"] == {
        "PK": {"S": f"JOB#{job.job_id}"},
        "SK": {"S": "META"},
    }
    assert job_check["ExpressionAttributeValues"][":expected_job"] == {"S": job.model_dump_json()}
    assert work_check["Key"] == {
        "PK": {"S": f"JOB#{job.job_id}"},
        "SK": {"S": f"WORK#{work.work_request_id}"},
    }
    assert work_check["ExpressionAttributeValues"][":expected_work"] == {
        "S": work.model_dump_json()
    }
    assert permit_put["ConditionExpression"] == "payload = :expected_payload"
    assert (
        store.consume_provider_call_permit(
            job,
            work,
            attempt_id,
            now=NOW + timedelta(seconds=4),
        )
        is None
    )


def test_legacy_raw_job_payload_completes_worker_permit_cas() -> None:
    client = MemoryLowLevelDynamoClient()
    store = DynamoDBSellerControlStore(client=client, table_name=TABLE_NAME)
    job, work, attempt_id = seed_provider_permit(store, dispatch_sync_work=True)
    legacy_payload = store_as_legacy_job_payload(client, job.job_id)
    reloaded = store.get_job(job.job_id)
    assert reloaded.model_dump_json() == legacy_payload

    consumed = store.consume_provider_call_permit(
        reloaded,
        work,
        attempt_id,
        now=NOW + timedelta(seconds=3),
    )

    assert consumed is not None
    job_check = client.transactions[-1]["TransactItems"][0]["ConditionCheck"]
    assert job_check["ExpressionAttributeValues"] == {":expected_job": {"S": legacy_payload}}


@pytest.mark.parametrize(
    ("field_name", "authority_id"),
    (
        ("approval_decision_id", "decision_forged"),
        ("publication_aggregate_id", "publication_forged"),
    ),
)
def test_legacy_job_cas_never_omits_non_null_authority(
    field_name: str,
    authority_id: str,
) -> None:
    client = MemoryLowLevelDynamoClient()
    store = DynamoDBSellerControlStore(client=client, table_name=TABLE_NAME)
    job, work, attempt_id = seed_provider_permit(store, dispatch_sync_work=True)
    legacy_payload = store_as_legacy_job_payload(client, job.job_id)
    reloaded = store.get_job(job.job_id)
    forged_expected = reloaded.model_copy(update={field_name: authority_id})
    assert authority_id in forged_expected.model_dump_json()
    assert forged_expected.model_dump_json() != legacy_payload

    assert (
        store.consume_provider_call_permit(
            forged_expected,
            work,
            attempt_id,
            now=NOW + timedelta(seconds=3),
        )
        is None
    )
    assert (
        store.get_provider_call_permit(job.job_id, attempt_id).status
        is ProviderCallPermitStatus.AVAILABLE
    )


def test_provider_permit_transaction_rejects_stale_active_work_payload() -> None:
    client = MemoryLowLevelDynamoClient()
    store = DynamoDBSellerControlStore(client=client, table_name=TABLE_NAME)
    job, claimed, attempt_id = seed_provider_permit(
        store,
        dispatch_sync_work=False,
    )
    assert claimed.claim_id is not None
    dispatched = store.mark_work_dispatched(
        job.job_id,
        claimed.work_request_id,
        claim_id=claimed.claim_id,
        execution_arn=(
            "arn:aws:states:us-west-2:123456789012:execution:mr-lister-sync:work-cas-winner"
        ),
        now=NOW + timedelta(seconds=3),
    )

    assert (
        store.consume_provider_call_permit(
            job,
            claimed,
            attempt_id,
            now=NOW + timedelta(seconds=4),
        )
        is None
    )
    assert (
        store.get_provider_call_permit(job.job_id, attempt_id).status
        is ProviderCallPermitStatus.AVAILABLE
    )
    assert (
        store.consume_provider_call_permit(
            job,
            dispatched,
            attempt_id,
            now=NOW + timedelta(seconds=4),
        )
        is not None
    )


def test_pricing_settlement_writes_and_round_trips_snapshot_with_complete_evidence() -> None:
    client = MemoryLowLevelDynamoClient()
    store = DynamoDBSellerControlStore(client=client, table_name=TABLE_NAME)
    claimed_job, sync_work, attempt_id = seed_provider_permit(
        store,
        dispatch_sync_work=True,
    )
    sync_clock = NOW + timedelta(seconds=5)
    worker = WorkerControlService(store=store, clock=lambda: sync_clock)
    assert (
        worker.authorize_provider_call(
            job_id=claimed_job.job_id,
            attempt_id=attempt_id,
        )
        is not None
    )
    worker.record_product_sync_success(
        RecordProductSyncSuccessCommand(
            job_id=claimed_job.job_id,
            work_request_id=sync_work.work_request_id,
            expected_record_version=claimed_job.record_version,
            attempt_id=attempt_id,
            observation=ProductSyncObservation(
                product_id="printify_product_dynamo",
                printify_shop_id=12_345,
                image_id="printify_image_dynamo",
                request_fingerprint="d" * 64,
                response_fingerprint="9" * 64,
                variants=(
                    ProductVariantEvidence(
                        variant_id=1000,
                        color="Black",
                        size="S",
                        placement_group_id="small",
                        retail_price_cents=2999,
                        production_cost_cents=1100,
                    ),
                ),
            ),
        )
    )
    pricing_job = store.get_job(claimed_job.job_id)
    sync = store.get_product_sync(pricing_job.job_id, pricing_job.product_sync_id or "")
    pricing_work = store.get_work_request(
        pricing_job.job_id, pricing_job.active_work_request_id or ""
    )
    claimed_pricing = store.claim_work(
        pricing_job.job_id,
        pricing_work.work_request_id,
        now=sync_clock,
        claim_id="claim_pricing",
        lease_expires_at=sync_clock + timedelta(minutes=1),
    )
    assert claimed_pricing is not None
    observed_at = sync_clock + timedelta(seconds=1)
    calculated_at = observed_at + timedelta(seconds=1)
    product_costs = ProductCostEvidence(
        product_sync_fingerprint=sync.fingerprint,
        observed_at=observed_at,
        variants=(
            ProductVariantCostEvidence(
                variant_id=1000,
                retail_price_cents=2999,
                production_cost_cents=1175,
            ),
        ),
    )
    shipping = parse_standard_us_shipping(
        {
            "data": [
                {
                    "type": "variant_shipping_standard_us",
                    "id": "1000",
                    "attributes": {
                        "shippingType": "standard",
                        "country": {"code": "US"},
                        "variantId": 1000,
                        "shippingPlanId": "standard-us",
                        "handlingTime": {"from": 2, "to": 5},
                        "shippingCost": {
                            "firstItem": {"amount": 399, "currency": "USD"},
                            "additionalItems": {"amount": 200, "currency": "USD"},
                        },
                    },
                }
            ]
        },
        blueprint_id=145,
        print_provider_id=39,
        expected_variant_ids=(1000,),
        observed_at=observed_at,
    )
    estimate = estimate_etsy_us_standard_proceeds(
        product_costs=product_costs,
        shipping=shipping,
        calculated_at=calculated_at,
    )
    settlement_worker = WorkerControlService(store=store, clock=lambda: calculated_at)

    completed = settlement_worker.record_pricing_success(
        RecordPricingSuccessCommand(
            job_id=pricing_job.job_id,
            work_request_id=claimed_pricing.work_request_id,
            expected_record_version=pricing_job.record_version,
            estimate=estimate,
        )
    )

    assert completed.state is ControlJobState.AWAITING_APPROVAL
    transaction = client.transactions[-1]
    entity_types = {
        operation["Put"]["Item"]["entity_type"]["S"]
        for operation in transaction["TransactItems"]
        if "Put" in operation
    }
    assert "PRICING_SNAPSHOT" in entity_types
    assert "PRICING_EVIDENCE" in entity_types
    sort_keys = {
        operation["Put"]["Item"]["SK"]["S"]
        for operation in transaction["TransactItems"]
        if "Put" in operation
    }
    settled = store.get_job(pricing_job.job_id)
    snapshot_id = settled.pricing_snapshot_id or ""
    assert f"PRICING#{snapshot_id}" in sort_keys
    assert f"PRICING_EVIDENCE#{snapshot_id}" in sort_keys

    reconstructed = DynamoDBSellerControlStore(client=client, table_name=TABLE_NAME)
    snapshot = reconstructed.get_pricing(settled.job_id, snapshot_id)
    evidence = reconstructed.get_pricing_evidence(settled.job_id, snapshot_id)
    assert snapshot.fingerprint == evidence.fingerprint == estimate.fingerprint
    assert evidence.estimate == estimate
    assert evidence.estimate.variants[0].production_cost_cents == 1175
    assert evidence.estimate.variants[0].estimated_proceeds_cents == 1095
    assert (
        reconstructed.get_work_request(settled.job_id, claimed_pricing.work_request_id).status
        is WorkRequestStatus.COMPLETED
    )
