"""DynamoDB implementation of the application-owned job persistence boundary."""

from __future__ import annotations

import json
from datetime import datetime
from hashlib import sha256
from typing import Any

from botocore.exceptions import ClientError

from mr_lister.contracts import ArtworkAnalysis, JobRecord, ListingIntelligence, ReviewSnapshot
from mr_lister.workflow.errors import (
    ConcurrentModificationError,
    IdempotencyConflictError,
    InvalidStateError,
    JobNotFoundError,
)
from mr_lister.workflow.models import (
    ApprovalWaitRecord,
    ApprovalWaitStatus,
    ArtworkInput,
    ExternalWriteClaim,
    ExternalWriteRecord,
    ExternalWriteStatus,
    WorkflowEvent,
)
from mr_lister.workflow.store import validate_transition_commit


def _s(value: str) -> dict[str, str]:
    return {"S": value}


def _n(value: int) -> dict[str, str]:
    return {"N": str(value)}


def _job_pk(job_id: str) -> str:
    return f"JOB#{job_id}"


def _intake_pk(idempotency_key: str) -> str:
    return f"IDEMPOTENCY#{sha256(idempotency_key.encode()).hexdigest()}"


def _review_sk(review_version: int) -> str:
    return f"REVIEW#{review_version:020d}"


def _event_sk(sequence: int) -> str:
    return f"EVENT#{sequence:020d}"


def _write_sk(idempotency_key: str) -> str:
    return f"WRITE#{sha256(idempotency_key.encode()).hexdigest()}"


def _payload(model: Any) -> str:
    return model.model_dump_json()


def _review_fingerprint(review: ReviewSnapshot) -> str:
    immutable = review.model_dump(
        mode="json",
        exclude={"approval_status", "printify_product_id"},
    )
    encoded = json.dumps(immutable, sort_keys=True, separators=(",", ":")).encode()
    return sha256(encoded).hexdigest()


def _job_item(job: JobRecord) -> dict[str, dict[str, str]]:
    return {
        "PK": _s(_job_pk(job.job_id)),
        "SK": _s("META"),
        "entity_type": _s("JOB"),
        "state": _s(job.state.value),
        "record_version": _n(job.record_version),
        "event_sequence": _n(job.event_sequence),
        "payload": _s(_payload(job)),
    }


def _artwork_item(job_id: str, artwork: ArtworkInput, profile_id: str) -> dict[str, dict[str, str]]:
    return {
        "PK": _s(_job_pk(job_id)),
        "SK": _s("ARTWORK"),
        "entity_type": _s("ARTWORK"),
        "profile_id": _s(profile_id),
        "content_sha256": _s(artwork.content_sha256),
        "payload": _s(_payload(artwork)),
    }


def _review_item(job_id: str, review: ReviewSnapshot) -> dict[str, dict[str, str]]:
    return {
        "PK": _s(_job_pk(job_id)),
        "SK": _s(_review_sk(review.review_version)),
        "entity_type": _s("REVIEW"),
        "review_version": _n(review.review_version),
        "review_fingerprint": _s(_review_fingerprint(review)),
        "payload": _s(_payload(review)),
    }


def _event_item(job_id: str, event: WorkflowEvent) -> dict[str, dict[str, str]]:
    return {
        "PK": _s(_job_pk(job_id)),
        "SK": _s(_event_sk(event.sequence)),
        "entity_type": _s("EVENT"),
        "sequence": _n(event.sequence),
        "payload": _s(_payload(event)),
    }


class DynamoDBJobStore:
    """Single-table durable store using atomic conditional transactions."""

    def __init__(self, *, client: Any, table_name: str) -> None:
        self._client = client
        self._table_name = table_name

    def resolve_intake(self, idempotency_key: str, request_fingerprint: str) -> JobRecord | None:
        response = self._client.get_item(
            TableName=self._table_name,
            Key={"PK": _s(_intake_pk(idempotency_key)), "SK": _s("CLAIM")},
            ConsistentRead=True,
        )
        item = response.get("Item")
        if item is None:
            return None
        if item["request_fingerprint"]["S"] != request_fingerprint:
            raise IdempotencyConflictError("Idempotency key was already used for other artwork")
        return self.get_job(item["job_id"]["S"])

    def create_intake(
        self,
        *,
        job: JobRecord,
        artwork: ArtworkInput,
        profile_id: str,
        request_fingerprint: str,
        event: WorkflowEvent,
    ) -> tuple[JobRecord, bool]:
        if job.event_sequence != 1 or event.sequence != 1:
            raise InvalidStateError("A new intake must atomically create its first event")
        claim = {
            "PK": _s(_intake_pk(job.idempotency_key)),
            "SK": _s("CLAIM"),
            "entity_type": _s("IDEMPOTENCY"),
            "request_fingerprint": _s(request_fingerprint),
            "job_id": _s(job.job_id),
        }
        transact_items = [
            self._put_new(claim),
            self._put_new(_job_item(job)),
            self._put_new(_artwork_item(job.job_id, artwork, profile_id)),
            self._put_new(_event_item(job.job_id, event)),
        ]
        try:
            self._client.transact_write_items(
                TransactItems=transact_items,
                ClientRequestToken=self._token(f"intake:{job.job_id}:{request_fingerprint}"),
            )
        except ClientError as error:
            if error.response.get("Error", {}).get("Code") != "TransactionCanceledException":
                raise
            existing = self.resolve_intake(job.idempotency_key, request_fingerprint)
            if existing is not None:
                return existing, False
            raise IdempotencyConflictError("Durable intake transaction was rejected") from error
        return job, True

    def get_job(self, job_id: str) -> JobRecord:
        item = self._get(job_id, "META")
        if item is None:
            raise JobNotFoundError(f"Unknown job: {job_id}")
        return JobRecord.model_validate_json(item["payload"]["S"])

    def commit_transition(
        self,
        *,
        current: JobRecord,
        updated: JobRecord,
        event: WorkflowEvent,
        review: ReviewSnapshot | None = None,
        approval_wait: tuple[ApprovalWaitRecord, ApprovalWaitRecord] | None = None,
    ) -> JobRecord:
        validate_transition_commit(
            current=current,
            updated=updated,
            event=event,
            review=review,
        )
        job_put = {
            "Put": {
                "TableName": self._table_name,
                "Item": _job_item(updated),
                "ConditionExpression": (
                    "record_version = :record_version AND "
                    "event_sequence = :event_sequence AND #state = :state"
                ),
                "ExpressionAttributeNames": {"#state": "state"},
                "ExpressionAttributeValues": {
                    ":record_version": _n(current.record_version),
                    ":event_sequence": _n(current.event_sequence),
                    ":state": _s(current.state.value),
                },
            }
        }
        transact_items = [job_put, self._put_new(_event_item(updated.job_id, event))]
        if review is not None:
            review_item = _review_item(updated.job_id, review)
            transact_items.append(
                {
                    "Put": {
                        "TableName": self._table_name,
                        "Item": review_item,
                        "ConditionExpression": (
                            "attribute_not_exists(PK) OR review_fingerprint = :fingerprint"
                        ),
                        "ExpressionAttributeValues": {
                            ":fingerprint": review_item["review_fingerprint"]
                        },
                    }
                }
            )
        if approval_wait is not None:
            expected_wait, consumed_wait = approval_wait
            if (
                expected_wait.status is not ApprovalWaitStatus.PENDING
                or consumed_wait.status is not ApprovalWaitStatus.CONSUMED
                or consumed_wait.consumed_at is None
                or consumed_wait.review_version != updated.approved_review_version
                or consumed_wait.job_id != updated.job_id
            ):
                raise InvalidStateError("Approval wait consumption does not match approval")
            transact_items.append(
                {
                    "Put": {
                        "TableName": self._table_name,
                        "Item": self._approval_wait_item(consumed_wait),
                        "ConditionExpression": (
                            "review_version = :review_version AND wait_status = :pending "
                            "AND expires_at > :consumed_at"
                        ),
                        "ExpressionAttributeValues": {
                            ":review_version": _n(expected_wait.review_version),
                            ":pending": _s(ApprovalWaitStatus.PENDING.value),
                            ":consumed_at": _n(int(consumed_wait.consumed_at.timestamp())),
                        },
                    }
                }
            )
        try:
            self._client.transact_write_items(
                TransactItems=transact_items,
                ClientRequestToken=self._token(
                    f"transition:{updated.job_id}:{updated.record_version}:{updated.event_sequence}"
                ),
            )
        except ClientError as error:
            if error.response.get("Error", {}).get("Code") == "TransactionCanceledException":
                raise ConcurrentModificationError(
                    "Job changed before transition could be committed"
                ) from error
            raise
        return updated

    def get_artwork(self, job_id: str) -> ArtworkInput:
        item = self._get(job_id, "ARTWORK")
        if item is None:
            raise JobNotFoundError(f"Artwork metadata is unavailable for job: {job_id}")
        return ArtworkInput.model_validate_json(item["payload"]["S"])

    def get_profile_id(self, job_id: str) -> str:
        item = self._get(job_id, "ARTWORK")
        if item is None:
            raise JobNotFoundError(f"Artwork metadata is unavailable for job: {job_id}")
        return item["profile_id"]["S"]

    def get_analysis_checkpoint(self, job_id: str) -> ArtworkAnalysis | None:
        item = self._get(job_id, "ANALYSIS")
        return None if item is None else ArtworkAnalysis.model_validate_json(item["payload"]["S"])

    def save_analysis_checkpoint(self, job_id: str, analysis: ArtworkAnalysis) -> ArtworkAnalysis:
        self._put_immutable_checkpoint(job_id, "ANALYSIS", "ANALYSIS", analysis)
        return analysis

    def get_listing_checkpoint(self, job_id: str) -> ListingIntelligence | None:
        item = self._get(job_id, "LISTING_CHECKPOINT")
        return (
            None if item is None else ListingIntelligence.model_validate_json(item["payload"]["S"])
        )

    def save_listing_checkpoint(
        self, job_id: str, listing: ListingIntelligence
    ) -> ListingIntelligence:
        self._put_immutable_checkpoint(job_id, "LISTING_CHECKPOINT", "LISTING_CHECKPOINT", listing)
        return listing

    def has_review(self, job_id: str) -> bool:
        job = self.get_job(job_id)
        return (
            job.review_version > 0 and self._get(job_id, _review_sk(job.review_version)) is not None
        )

    def get_review(self, job_id: str) -> ReviewSnapshot:
        job = self.get_job(job_id)
        item = self._get(job_id, _review_sk(job.review_version))
        if item is None:
            raise JobNotFoundError(f"Review is unavailable for job: {job_id}")
        return ReviewSnapshot.model_validate_json(item["payload"]["S"])

    def append_event(
        self, job_id: str, *, occurred_at: datetime, name: str, details: dict[str, object]
    ) -> WorkflowEvent:
        current = self.get_job(job_id)
        event = WorkflowEvent(
            sequence=current.event_sequence + 1,
            occurred_at=occurred_at,
            name=name,
            details=details,
        )
        updated = current.model_copy(update={"event_sequence": event.sequence})
        job_put = {
            "Put": {
                "TableName": self._table_name,
                "Item": _job_item(updated),
                "ConditionExpression": (
                    "record_version = :record_version AND "
                    "event_sequence = :event_sequence AND #state = :state"
                ),
                "ExpressionAttributeNames": {"#state": "state"},
                "ExpressionAttributeValues": {
                    ":record_version": _n(current.record_version),
                    ":event_sequence": _n(current.event_sequence),
                    ":state": _s(current.state.value),
                },
            }
        }
        try:
            self._client.transact_write_items(
                TransactItems=[job_put, self._put_new(_event_item(job_id, event))],
                ClientRequestToken=self._token(f"event:{job_id}:{event.sequence}"),
            )
        except ClientError as error:
            if error.response.get("Error", {}).get("Code") == "TransactionCanceledException":
                raise ConcurrentModificationError(
                    "Job changed before event could be appended"
                ) from error
            raise
        return event

    def list_events(self, job_id: str) -> tuple[WorkflowEvent, ...]:
        items = self._query_prefix(job_id, "EVENT#")
        return tuple(WorkflowEvent.model_validate_json(item["payload"]["S"]) for item in items)

    def claim_external_write(
        self, job_id: str, claim: ExternalWriteClaim
    ) -> tuple[ExternalWriteClaim, bool]:
        self.get_job(job_id)
        item = {
            "PK": _s(_job_pk(job_id)),
            "SK": _s(_write_sk(claim.idempotency_key)),
            "entity_type": _s("WRITE"),
            "request_fingerprint": _s(claim.request_fingerprint),
            "write_status": _s(claim.status.value),
            "payload": _s(_payload(claim)),
        }
        try:
            self._client.put_item(
                TableName=self._table_name,
                Item=item,
                ConditionExpression="attribute_not_exists(PK)",
            )
        except ClientError as error:
            if error.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
                existing = self._get(job_id, _write_sk(claim.idempotency_key))
                if existing is None:
                    raise ConcurrentModificationError("External write claim changed") from error
                stored = ExternalWriteClaim.model_validate_json(existing["payload"]["S"])
                if stored.request_fingerprint != claim.request_fingerprint:
                    raise InvalidStateError(
                        "External write idempotency fingerprint changed"
                    ) from error
                return stored, False
            raise
        return claim, True

    def complete_external_write(
        self,
        job_id: str,
        *,
        idempotency_key: str,
        request_fingerprint: str,
        result: dict[str, str],
        completed_at: datetime,
    ) -> ExternalWriteRecord:
        existing = self._get(job_id, _write_sk(idempotency_key))
        if existing is None:
            raise InvalidStateError("External write was not claimed")
        claim = ExternalWriteClaim.model_validate_json(existing["payload"]["S"])
        if claim.request_fingerprint != request_fingerprint:
            raise InvalidStateError("External write idempotency fingerprint changed")
        if claim.status is ExternalWriteStatus.COMPLETED:
            if claim.result != result:
                raise InvalidStateError("Completed external write result changed")
            completed = claim
        else:
            completed = claim.model_copy(
                update={
                    "status": ExternalWriteStatus.COMPLETED,
                    "result": result,
                    "completed_at": completed_at,
                }
            )
            item = dict(existing)
            item["write_status"] = _s(ExternalWriteStatus.COMPLETED.value)
            item["payload"] = _s(_payload(completed))
            try:
                self._client.put_item(
                    TableName=self._table_name,
                    Item=item,
                    ConditionExpression=(
                        "request_fingerprint = :fingerprint AND write_status = :claimed"
                    ),
                    ExpressionAttributeValues={
                        ":fingerprint": _s(request_fingerprint),
                        ":claimed": _s(ExternalWriteStatus.CLAIMED.value),
                    },
                )
            except ClientError as error:
                if error.response.get("Error", {}).get("Code") == (
                    "ConditionalCheckFailedException"
                ):
                    raise ConcurrentModificationError(
                        "External write claim changed before completion"
                    ) from error
                raise
        assert completed.result is not None and completed.completed_at is not None
        return ExternalWriteRecord(
            operation=completed.operation,
            idempotency_key=completed.idempotency_key,
            request_fingerprint=completed.request_fingerprint,
            external_id=completed.result["external_id"],
            occurred_at=completed.completed_at,
        )

    def list_external_writes(self, job_id: str) -> tuple[ExternalWriteRecord, ...]:
        items = self._query_prefix(job_id, "WRITE#")
        return tuple(
            ExternalWriteRecord(
                operation=claim.operation,
                idempotency_key=claim.idempotency_key,
                request_fingerprint=claim.request_fingerprint,
                external_id=claim.result["external_id"],
                occurred_at=claim.completed_at,
            )
            for item in items
            if (claim := ExternalWriteClaim.model_validate_json(item["payload"]["S"])).status
            is ExternalWriteStatus.COMPLETED
            and claim.result is not None
            and claim.completed_at is not None
        )

    def require_external_write_reconciliation(
        self, job_id: str, *, idempotency_key: str, request_fingerprint: str
    ) -> ExternalWriteClaim:
        existing = self._get(job_id, _write_sk(idempotency_key))
        if existing is None:
            raise InvalidStateError("External write claim is unavailable for reconciliation")
        claim = ExternalWriteClaim.model_validate_json(existing["payload"]["S"])
        if claim.request_fingerprint != request_fingerprint:
            raise InvalidStateError("External write idempotency fingerprint changed")
        if claim.status is ExternalWriteStatus.COMPLETED:
            return claim
        reconciled = claim.model_copy(
            update={"status": ExternalWriteStatus.RECONCILIATION_REQUIRED}
        )
        item = dict(existing)
        item["write_status"] = _s(ExternalWriteStatus.RECONCILIATION_REQUIRED.value)
        item["payload"] = _s(_payload(reconciled))
        try:
            self._client.put_item(
                TableName=self._table_name,
                Item=item,
                ConditionExpression=(
                    "request_fingerprint = :fingerprint AND write_status = :claimed"
                ),
                ExpressionAttributeValues={
                    ":fingerprint": _s(request_fingerprint),
                    ":claimed": _s(ExternalWriteStatus.CLAIMED.value),
                },
            )
        except ClientError as error:
            if error.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
                raise ConcurrentModificationError(
                    "External write claim changed before reconciliation"
                ) from error
            raise
        return reconciled

    def register_approval_wait(self, wait: ApprovalWaitRecord) -> ApprovalWaitRecord:
        job = self.get_job(wait.job_id)
        if job.state.value != "awaiting_approval" or job.review_version != wait.review_version:
            raise InvalidStateError("Approval wait does not match the reviewable job version")
        item = self._approval_wait_item(wait)
        try:
            self._client.put_item(
                TableName=self._table_name,
                Item=item,
                ConditionExpression="attribute_not_exists(PK)",
            )
        except ClientError as error:
            if error.response.get("Error", {}).get("Code") != "ConditionalCheckFailedException":
                raise
            existing = self.get_approval_wait(wait.job_id)
            if (
                existing is None
                or existing.review_version != wait.review_version
                or existing.task_token != wait.task_token
            ):
                raise InvalidStateError(
                    "A different approval wait is already registered"
                ) from error
            return existing
        return wait

    def get_approval_wait(self, job_id: str) -> ApprovalWaitRecord | None:
        item = self._get(job_id, "APPROVAL_WAIT")
        if item is None:
            return None
        payload = ApprovalWaitRecord.model_validate_json(item["payload"]["S"])
        return payload.model_copy(update={"task_token": item["task_token"]["S"]})

    def _put_immutable_checkpoint(
        self, job_id: str, sort_key: str, entity_type: str, model: Any
    ) -> None:
        self.get_job(job_id)
        payload = _payload(model)
        fingerprint = sha256(payload.encode()).hexdigest()
        item = {
            "PK": _s(_job_pk(job_id)),
            "SK": _s(sort_key),
            "entity_type": _s(entity_type),
            "checkpoint_fingerprint": _s(fingerprint),
            "payload": _s(payload),
        }
        try:
            self._client.put_item(
                TableName=self._table_name,
                Item=item,
                ConditionExpression=(
                    "attribute_not_exists(PK) OR checkpoint_fingerprint = :fingerprint"
                ),
                ExpressionAttributeValues={":fingerprint": _s(fingerprint)},
            )
        except ClientError as error:
            if error.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
                raise ConcurrentModificationError(
                    "Durable preparation checkpoint already differs"
                ) from error
            raise

    @staticmethod
    def _approval_wait_item(wait: ApprovalWaitRecord) -> dict[str, dict[str, str]]:
        sanitized = wait.model_copy(update={"task_token": "stored-separately"})
        return {
            "PK": _s(_job_pk(wait.job_id)),
            "SK": _s("APPROVAL_WAIT"),
            "entity_type": _s("APPROVAL_WAIT"),
            "review_version": _n(wait.review_version),
            "wait_status": _s(wait.status.value),
            "expires_at": _n(int(wait.expires_at.timestamp())),
            "task_token": _s(wait.task_token),
            "payload": _s(_payload(sanitized)),
        }

    def _get(self, job_id: str, sort_key: str) -> dict[str, Any] | None:
        response = self._client.get_item(
            TableName=self._table_name,
            Key={"PK": _s(_job_pk(job_id)), "SK": _s(sort_key)},
            ConsistentRead=True,
        )
        return response.get("Item")

    def _query_prefix(self, job_id: str, prefix: str) -> list[dict[str, Any]]:
        response = self._client.query(
            TableName=self._table_name,
            KeyConditionExpression="PK = :pk AND begins_with(SK, :prefix)",
            ExpressionAttributeValues={
                ":pk": _s(_job_pk(job_id)),
                ":prefix": _s(prefix),
            },
            ConsistentRead=True,
            ScanIndexForward=True,
        )
        return response.get("Items", [])

    def _put_new(self, item: dict[str, Any]) -> dict[str, Any]:
        return {
            "Put": {
                "TableName": self._table_name,
                "Item": item,
                "ConditionExpression": "attribute_not_exists(PK)",
            }
        }

    @staticmethod
    def _token(material: str) -> str:
        return sha256(material.encode()).hexdigest()[:32]
