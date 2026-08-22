"""DynamoDB transaction adapter for the Phase 6 seller-control boundary."""

from __future__ import annotations

from datetime import datetime
from hashlib import sha256
from typing import Any

from botocore.exceptions import ClientError

from mr_lister.control.errors import (
    ConcurrentControlModificationError,
    IdempotencyConflictError,
    NotFoundError,
)
from mr_lister.control.models import (
    CommandReceipt,
    ControlJobRecord,
    DomainEvent,
    FailureRecord,
    PricingSnapshot,
    ProductSyncRecord,
    ReviewContent,
    WorkRequest,
    WorkRequestStatus,
)
from mr_lister.control.store import (
    CommandCommit,
    revalidate_work_request,
    validate_command_commit,
    validate_initial_job,
)


def _s(value: str) -> dict[str, str]:
    return {"S": value}


def _n(value: int) -> dict[str, str]:
    return {"N": str(value)}


def _bool(value: bool) -> dict[str, bool]:
    return {"BOOL": value}


def _job_pk(job_id: str) -> str:
    return f"JOB#{job_id}"


def _owner_pk(owner_id: str) -> str:
    return f"OWNER#{owner_id}"


def _receipt_sk(command_type: str, job_id: str, key_digest: str) -> str:
    material = f"{command_type}\0{job_id}\0{key_digest}".encode()
    return f"RECEIPT#{sha256(material).hexdigest()}"


def _payload(model: Any) -> str:
    return model.model_dump_json()


def _job_item(job: ControlJobRecord) -> dict[str, dict[str, Any]]:
    return {
        "PK": _s(_job_pk(job.job_id)),
        "SK": _s("META"),
        "entity_type": _s("CONTROL_JOB"),
        "contract_version": _s(job.contract_version),
        "owner_id": _s(job.owner_id),
        "state": _s(job.state.value),
        "record_version": _n(job.record_version),
        "event_sequence": _n(job.event_sequence),
        "review_version": _n(job.review_version),
        "cancellation_requested": _bool(job.cancellation_requested_at is not None),
        "payload": _s(_payload(job)),
    }


def _record_item(
    *, job_id: str, sort_key: str, entity_type: str, record: Any
) -> dict[str, dict[str, Any]]:
    return {
        "PK": _s(_job_pk(job_id)),
        "SK": _s(sort_key),
        "entity_type": _s(entity_type),
        "contract_version": _s(record.contract_version),
        "payload": _s(_payload(record)),
    }


def _work_item(work: WorkRequest) -> dict[str, dict[str, Any]]:
    item = _record_item(
        job_id=work.job_id,
        sort_key=f"WORK#{work.work_request_id}",
        entity_type="WORK_REQUEST",
        record=work,
    )
    item["work_status"] = _s(work.status.value)
    item["work_request_id"] = _s(work.work_request_id)
    if work.status in {WorkRequestStatus.PENDING, WorkRequestStatus.CLAIMED}:
        due_at = (
            work.lease_expires_at
            if work.status is WorkRequestStatus.CLAIMED
            else work.next_dispatch_at
        )
        assert due_at is not None
        item["dispatch_pk"] = _s("WORK_DUE#0")
        item["dispatch_sk"] = _s(f"{int(due_at.timestamp()):020d}#{work.work_request_id}")
    return item


def _receipt_item(receipt: CommandReceipt) -> dict[str, dict[str, Any]]:
    return {
        "PK": _s(_owner_pk(receipt.owner_id)),
        "SK": _s(
            _receipt_sk(
                receipt.command_type,
                receipt.job_id,
                receipt.idempotency_key_digest,
            )
        ),
        "entity_type": _s("COMMAND_RECEIPT"),
        "contract_version": _s(receipt.contract_version),
        "job_id": _s(receipt.job_id),
        "command_type": _s(receipt.command_type),
        "key_digest": _s(receipt.idempotency_key_digest),
        "request_fingerprint": _s(receipt.request_fingerprint),
        "payload": _s(_payload(receipt)),
    }


class DynamoDBSellerControlStore:
    """Single-table adapter whose mutations mirror ``CommandCommit`` exactly."""

    def __init__(self, *, client: Any, table_name: str) -> None:
        self._client = client
        self._table_name = table_name

    def get_job(self, job_id: str) -> ControlJobRecord:
        item = self._get(_job_pk(job_id), "META")
        if item is None:
            raise NotFoundError("The requested job was not found")
        return ControlJobRecord.model_validate_json(item["payload"]["S"])

    def get_job_for_owner(self, owner_id: str, job_id: str) -> ControlJobRecord:
        job = self.get_job(job_id)
        if job.owner_id != owner_id:
            raise NotFoundError("The requested job was not found")
        return job

    def get_review(self, job_id: str, review_version: int) -> ReviewContent:
        return self._get_record(
            job_id,
            f"REVIEW#{review_version:020d}",
            ReviewContent,
            "review",
        )

    def get_product_sync(self, job_id: str, sync_id: str) -> ProductSyncRecord:
        return self._get_record(
            job_id, f"PRODUCT_SYNC#{sync_id}", ProductSyncRecord, "product synchronization"
        )

    def get_pricing(self, job_id: str, snapshot_id: str) -> PricingSnapshot:
        return self._get_record(
            job_id, f"PRICING#{snapshot_id}", PricingSnapshot, "pricing snapshot"
        )

    def get_failure(self, job_id: str, failure_id: str) -> FailureRecord:
        return self._get_record(job_id, f"FAILURE#{failure_id}", FailureRecord, "failure")

    def get_work_request(self, job_id: str, work_request_id: str) -> WorkRequest:
        return self._get_record(job_id, f"WORK#{work_request_id}", WorkRequest, "work request")

    def resolve_receipt(
        self, owner_id: str, command_type: str, job_id: str, key_digest: str
    ) -> CommandReceipt | None:
        item = self._get(
            _owner_pk(owner_id),
            _receipt_sk(command_type, job_id, key_digest),
        )
        return None if item is None else CommandReceipt.model_validate_json(item["payload"]["S"])

    def create_job(
        self,
        *,
        job: ControlJobRecord,
        event: DomainEvent,
        receipt: CommandReceipt,
        work_request: WorkRequest | None = None,
    ) -> CommandReceipt:
        validate_initial_job(job, event, receipt, work_request)
        items = [
            self._put_new(_job_item(job)),
            self._put_new(
                _record_item(
                    job_id=job.job_id,
                    sort_key=f"EVENT#{event.sequence:020d}",
                    entity_type="DOMAIN_EVENT",
                    record=event,
                )
            ),
            self._put_new(_receipt_item(receipt)),
        ]
        if work_request is not None:
            items.append(self._put_new(_work_item(work_request)))
        try:
            self._transact(items, receipt.receipt_id)
        except ClientError as error:
            if self._is_transaction_replay_error(error):
                return self._resolve_after_cancel(receipt)
            raise
        return receipt

    def commit_command(self, commit: CommandCommit) -> CommandReceipt:
        validate_command_commit(commit)
        current = commit.current
        job_put = {
            "Put": {
                "TableName": self._table_name,
                "Item": _job_item(commit.updated),
                "ConditionExpression": (
                    "contract_version = :contract_version AND owner_id = :owner_id AND "
                    "record_version = :record_version AND event_sequence = :event_sequence AND "
                    "#state = :state AND review_version = :review_version AND "
                    "cancellation_requested = :cancellation_requested AND "
                    "payload = :expected_payload"
                ),
                "ExpressionAttributeNames": {"#state": "state"},
                "ExpressionAttributeValues": {
                    ":contract_version": _s(current.contract_version),
                    ":owner_id": _s(current.owner_id),
                    ":record_version": _n(current.record_version),
                    ":event_sequence": _n(current.event_sequence),
                    ":state": _s(current.state.value),
                    ":review_version": _n(current.review_version),
                    ":cancellation_requested": _bool(current.cancellation_requested_at is not None),
                    ":expected_payload": _s(_payload(current)),
                },
            }
        }
        items: list[dict[str, Any]] = [
            job_put,
            self._put_new(
                _record_item(
                    job_id=commit.updated.job_id,
                    sort_key=f"EVENT#{commit.event.sequence:020d}",
                    entity_type="DOMAIN_EVENT",
                    record=commit.event,
                )
            ),
            self._put_new(_receipt_item(commit.receipt)),
        ]
        records: tuple[tuple[Any, str, str], ...] = (
            (
                commit.review,
                f"REVIEW#{commit.review.review_version:020d}" if commit.review else "",
                "REVIEW",
            ),
            (
                commit.review_decision,
                f"DECISION#{commit.review_decision.decision_id}" if commit.review_decision else "",
                "REVIEW_DECISION",
            ),
            (
                commit.cancellation_decision,
                f"CANCELLATION#{commit.cancellation_decision.decision_id}"
                if commit.cancellation_decision
                else "",
                "CANCELLATION_DECISION",
            ),
            (
                commit.product_sync,
                f"PRODUCT_SYNC#{commit.product_sync.sync_id}" if commit.product_sync else "",
                "PRODUCT_SYNC",
            ),
            (
                commit.pricing_snapshot,
                f"PRICING#{commit.pricing_snapshot.snapshot_id}" if commit.pricing_snapshot else "",
                "PRICING_SNAPSHOT",
            ),
            (
                commit.failure,
                f"FAILURE#{commit.failure.failure_id}" if commit.failure else "",
                "FAILURE",
            ),
        )
        for record, sort_key, entity_type in records:
            if record is not None:
                items.append(
                    self._put_new(
                        _record_item(
                            job_id=commit.updated.job_id,
                            sort_key=sort_key,
                            entity_type=entity_type,
                            record=record,
                        )
                    )
                )
        if commit.work_request is not None:
            items.append(self._put_new(_work_item(commit.work_request)))
        if commit.work_update is not None:
            expected, changed = commit.work_update
            items.append(
                {
                    "Put": {
                        "TableName": self._table_name,
                        "Item": _work_item(changed),
                        "ConditionExpression": "payload = :expected_payload",
                        "ExpressionAttributeValues": {":expected_payload": _s(_payload(expected))},
                    }
                }
            )
        try:
            self._transact(items, commit.receipt.receipt_id)
        except ClientError as error:
            if self._is_transaction_replay_error(error):
                existing = self.resolve_receipt(
                    commit.receipt.owner_id,
                    commit.receipt.command_type,
                    commit.receipt.job_id,
                    commit.receipt.idempotency_key_digest,
                )
                if existing is not None:
                    if existing.request_fingerprint == commit.receipt.request_fingerprint:
                        return existing
                    raise IdempotencyConflictError(
                        "The idempotency key was used for another request"
                    ) from error
                raise ConcurrentControlModificationError(
                    "The job changed before the command could commit"
                ) from error
            raise
        return commit.receipt

    def nudge_pending_work(
        self, job_id: str, work_request_id: str, *, now: datetime
    ) -> WorkRequest:
        current = self.get_work_request(job_id, work_request_id)
        if current.status is not WorkRequestStatus.PENDING or current.next_dispatch_at <= now:
            return current
        updated = revalidate_work_request(
            current,
            next_dispatch_at=now,
            updated_at=now,
        )
        try:
            self._client.put_item(
                TableName=self._table_name,
                Item=_work_item(updated),
                ConditionExpression="work_status = :pending AND payload = :expected_payload",
                ExpressionAttributeValues={
                    ":pending": _s(WorkRequestStatus.PENDING.value),
                    ":expected_payload": _s(_payload(current)),
                },
            )
        except ClientError as error:
            if error.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
                return self.get_work_request(job_id, work_request_id)
            raise
        return updated

    def list_due_work(self, *, now: datetime, limit: int = 100) -> tuple[WorkRequest, ...]:
        response = self._client.query(
            TableName=self._table_name,
            IndexName="DueWorkIndex",
            KeyConditionExpression="dispatch_pk = :dispatch_pk AND dispatch_sk <= :dispatch_sk",
            ExpressionAttributeValues={
                ":dispatch_pk": _s("WORK_DUE#0"),
                ":dispatch_sk": _s(f"{int(now.timestamp()):020d}#~"),
            },
            ScanIndexForward=True,
            Limit=limit,
        )
        return tuple(
            WorkRequest.model_validate_json(item["payload"]["S"])
            for item in response.get("Items", [])
        )

    def claim_work(
        self,
        job_id: str,
        work_request_id: str,
        *,
        now: datetime,
        claim_id: str,
        lease_expires_at: datetime,
    ) -> WorkRequest | None:
        current = self.get_work_request(job_id, work_request_id)
        claimable = (
            current.status is WorkRequestStatus.PENDING and current.next_dispatch_at <= now
        ) or (
            current.status is WorkRequestStatus.CLAIMED
            and current.lease_expires_at is not None
            and current.lease_expires_at <= now
        )
        if not claimable:
            return None
        claimed = revalidate_work_request(
            current,
            status=WorkRequestStatus.CLAIMED,
            attempt_count=current.attempt_count + 1,
            claim_id=claim_id,
            lease_expires_at=lease_expires_at,
            last_error_code=None,
            updated_at=now,
        )
        return self._replace_work_conditionally(current, claimed, return_none_on_conflict=True)

    def mark_work_dispatched(
        self,
        job_id: str,
        work_request_id: str,
        *,
        claim_id: str,
        execution_arn: str,
        now: datetime,
    ) -> WorkRequest:
        current = self.get_work_request(job_id, work_request_id)
        if current.status is WorkRequestStatus.COMPLETED:
            return current
        if current.status is not WorkRequestStatus.CLAIMED or current.claim_id != claim_id:
            raise ConcurrentControlModificationError("The work claim is no longer current")
        dispatched = revalidate_work_request(
            current,
            status=WorkRequestStatus.DISPATCHED,
            claim_id=None,
            lease_expires_at=None,
            execution_arn=execution_arn,
            updated_at=now,
        )
        try:
            result = self._replace_work_conditionally(current, dispatched)
        except ConcurrentControlModificationError:
            latest = self.get_work_request(job_id, work_request_id)
            if latest.status is WorkRequestStatus.COMPLETED:
                return latest
            raise
        assert result is not None
        return result

    def defer_claimed_work(
        self,
        job_id: str,
        work_request_id: str,
        *,
        claim_id: str,
        retry_at: datetime,
        error_code: str,
        now: datetime,
    ) -> WorkRequest:
        current = self.get_work_request(job_id, work_request_id)
        if current.status is WorkRequestStatus.COMPLETED:
            return current
        if current.status is not WorkRequestStatus.CLAIMED or current.claim_id != claim_id:
            raise ConcurrentControlModificationError("The work claim is no longer current")
        deferred = revalidate_work_request(
            current,
            lease_expires_at=retry_at,
            last_error_code=error_code,
            updated_at=now,
        )
        try:
            result = self._replace_work_conditionally(current, deferred)
        except ConcurrentControlModificationError:
            latest = self.get_work_request(job_id, work_request_id)
            if latest.status is WorkRequestStatus.COMPLETED:
                return latest
            raise
        assert result is not None
        return result

    def release_work(
        self,
        job_id: str,
        work_request_id: str,
        *,
        claim_id: str,
        next_dispatch_at: datetime,
        error_code: str,
        now: datetime,
    ) -> WorkRequest:
        current = self.get_work_request(job_id, work_request_id)
        if current.status is not WorkRequestStatus.CLAIMED or current.claim_id != claim_id:
            raise ConcurrentControlModificationError("The work claim is no longer current")
        released = revalidate_work_request(
            current,
            status=WorkRequestStatus.PENDING,
            claim_id=None,
            lease_expires_at=None,
            next_dispatch_at=next_dispatch_at,
            last_error_code=error_code,
            updated_at=now,
        )
        result = self._replace_work_conditionally(current, released)
        assert result is not None
        return result

    def _replace_work_conditionally(
        self,
        current: WorkRequest,
        updated: WorkRequest,
        *,
        return_none_on_conflict: bool = False,
    ) -> WorkRequest | None:
        try:
            self._client.put_item(
                TableName=self._table_name,
                Item=_work_item(updated),
                ConditionExpression="payload = :expected_payload",
                ExpressionAttributeValues={":expected_payload": _s(_payload(current))},
            )
        except ClientError as error:
            if error.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
                if return_none_on_conflict:
                    return None
                raise ConcurrentControlModificationError(
                    "The work request changed before it could be updated"
                ) from error
            raise
        return updated

    def _get_record(self, job_id: str, sort_key: str, model: Any, label: str) -> Any:
        self.get_job(job_id)
        item = self._get(_job_pk(job_id), sort_key)
        if item is None:
            raise NotFoundError(f"The requested {label} was not found")
        return model.model_validate_json(item["payload"]["S"])

    def _get(self, partition_key: str, sort_key: str) -> dict[str, Any] | None:
        response = self._client.get_item(
            TableName=self._table_name,
            Key={"PK": _s(partition_key), "SK": _s(sort_key)},
            ConsistentRead=True,
        )
        return response.get("Item")

    def _put_new(self, item: dict[str, Any]) -> dict[str, Any]:
        return {
            "Put": {
                "TableName": self._table_name,
                "Item": item,
                "ConditionExpression": "attribute_not_exists(PK)",
            }
        }

    def _transact(self, items: list[dict[str, Any]], identity: str) -> None:
        self._client.transact_write_items(
            TransactItems=items,
            ClientRequestToken=sha256(identity.encode()).hexdigest()[:32],
        )

    def _resolve_after_cancel(self, receipt: CommandReceipt) -> CommandReceipt:
        existing = self.resolve_receipt(
            receipt.owner_id,
            receipt.command_type,
            receipt.job_id,
            receipt.idempotency_key_digest,
        )
        if existing is not None and existing.request_fingerprint == receipt.request_fingerprint:
            return existing
        if existing is not None:
            raise IdempotencyConflictError("The idempotency key was used for another request")
        raise ConcurrentControlModificationError("The job could not be created atomically")

    @staticmethod
    def _is_transaction_replay_error(error: ClientError) -> bool:
        return error.response.get("Error", {}).get("Code") in {
            "TransactionCanceledException",
            "IdempotentParameterMismatchException",
        }
