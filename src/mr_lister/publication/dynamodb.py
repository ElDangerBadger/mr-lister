"""DynamoDB adapter for the disabled Phase 7.1 publication-request boundary.

The adapter can persist a pristine publication request but exposes no provider transport,
dispatcher, route, or permit-consumption operation.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from hashlib import sha256
from typing import Any

from botocore.exceptions import ClientError
from pydantic import ValidationError

from mr_lister.control.models import (
    ControlJobRecord,
    ControlJobState,
    PricingEvidenceRecord,
    PricingSnapshot,
    ProductSyncRecord,
    ReviewContent,
    ReviewDecisionRecord,
    SourceArtifactRecord,
)
from mr_lister.control.store import owner_job_sort_key
from mr_lister.publication.commands import (
    PublicationCommandReceipt,
    PublicationRequestCommit,
)
from mr_lister.publication.errors import (
    PublicationAuthorityError,
    PublicationConflictError,
    PublicationErrorCode,
    PublicationIdempotencyConflictError,
    PublicationNotFoundError,
)
from mr_lister.publication.models import (
    PublicationAggregate,
    PublicationWorkRequest,
)
from mr_lister.publication.retention_locator import (
    PUBLICATION_REQUEST_RECEIPT_LOCATOR_ENTITY_TYPE,
    PUBLICATION_REQUEST_RECEIPT_LOCATOR_SORT_KEY,
    build_publication_request_receipt_locator,
    publication_request_receipt_sort_key,
)
from mr_lister.publication.store import (
    PublicationRequestAuthority,
    PublicationRequestTransaction,
    validate_publication_request_authority,
    validate_publication_request_transaction,
)

MAX_PUBLICATION_REQUEST_TRANSACTION_ITEMS = 25
PUBLICATION_REQUEST_TRANSACTION_ITEMS = 15
MAX_DYNAMODB_ITEM_BYTES = 400 * 1024
MAX_DYNAMODB_TRANSACTION_BYTES = 4 * 1024 * 1024


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


def _publication_pk(aggregate_id: str) -> str:
    return f"PUBLICATION#{aggregate_id}"


def _payload(model: Any) -> str:
    return model.model_dump_json()


def _job_item(job: ControlJobRecord) -> dict[str, dict[str, Any]]:
    return {
        "PK": _s(_job_pk(job.job_id)),
        "SK": _s("META"),
        "entity_type": _s("CONTROL_JOB"),
        "contract_version": _s(job.contract_version),
        "owner_id": _s(job.owner_id),
        "owner_jobs_pk": _s(_owner_pk(job.owner_id)),
        "owner_jobs_sk": _s(owner_job_sort_key(job)),
        "state": _s(job.state.value),
        "record_version": _n(job.record_version),
        "event_sequence": _n(job.event_sequence),
        "review_version": _n(job.review_version),
        "cancellation_requested": _bool(job.cancellation_requested_at is not None),
        "payload": _s(_payload(job)),
    }


def _publication_record_item(
    *,
    aggregate_id: str,
    sort_key: str,
    entity_type: str,
    record: Any,
) -> dict[str, dict[str, Any]]:
    return {
        "PK": _s(_publication_pk(aggregate_id)),
        "SK": _s(sort_key),
        "entity_type": _s(entity_type),
        "contract_version": _s(record.contract_version),
        "payload": _s(_payload(record)),
    }


def _aggregate_item(aggregate: PublicationAggregate) -> dict[str, dict[str, Any]]:
    item = _publication_record_item(
        aggregate_id=aggregate.aggregate_id,
        sort_key="META",
        entity_type="PUBLICATION_AGGREGATE",
        record=aggregate,
    )
    item.update(
        {
            "owner_id": _s(aggregate.owner_id),
            "job_id": _s(aggregate.job_id),
            "publication_state": _s(aggregate.state.value),
            "record_version": _n(aggregate.record_version),
        }
    )
    return item


def _work_item(work: PublicationWorkRequest) -> dict[str, dict[str, Any]]:
    item = _publication_record_item(
        aggregate_id=work.aggregate_id,
        sort_key=f"PUBLICATION_WORK#{work.work_request_id}",
        entity_type="PUBLICATION_WORK_REQUEST",
        record=work,
    )
    item.update(
        {
            "work_status": _s(work.status.value),
            "work_request_id": _s(work.work_request_id),
            "dispatch_pk": _s("PUBLICATION_WORK_DUE#0"),
            "dispatch_sk": _s(
                f"{int(work.next_dispatch_at.timestamp()):020d}#{work.work_request_id}"
            ),
        }
    )
    return item


def _receipt_item(receipt: PublicationCommandReceipt) -> dict[str, dict[str, Any]]:
    return {
        "PK": _s(_owner_pk(receipt.owner_id)),
        "SK": _s(
            publication_request_receipt_sort_key(
                receipt.job_id,
                receipt.idempotency_key_digest,
            )
        ),
        "entity_type": _s("PUBLICATION_COMMAND_RECEIPT"),
        "contract_version": _s(receipt.contract_version),
        "job_id": _s(receipt.job_id),
        "command_type": _s(receipt.command_type.value),
        "key_digest": _s(receipt.idempotency_key_digest),
        "request_fingerprint": _s(receipt.request_fingerprint),
        "payload": _s(_payload(receipt)),
    }


def _put_new(item: dict[str, Any], table_name: str) -> dict[str, Any]:
    return {
        "Put": {
            "TableName": table_name,
            "Item": item,
            "ConditionExpression": "attribute_not_exists(PK)",
        }
    }


def _authority_condition(
    *,
    table_name: str,
    job_id: str,
    sort_key: str,
    entity_type: str,
    record: Any,
) -> dict[str, Any]:
    return {
        "ConditionCheck": {
            "TableName": table_name,
            "Key": {"PK": _s(_job_pk(job_id)), "SK": _s(sort_key)},
            "ConditionExpression": (
                "entity_type = :entity_type AND contract_version = :contract_version "
                "AND payload = :expected_payload"
            ),
            "ExpressionAttributeValues": {
                ":entity_type": _s(entity_type),
                ":contract_version": _s(record.contract_version),
                ":expected_payload": _s(_payload(record)),
            },
        }
    }


def _transaction_client_token(items: list[dict[str, Any]]) -> str:
    """Bind SDK replay identity to the exact rendered transaction, including server time."""

    encoded = json.dumps(
        items,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode()
    return sha256(encoded).hexdigest()[:32]


def _rendered_size(value: Any) -> int:
    return len(
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode()
    )


def _validate_transaction_envelope(items: list[dict[str, Any]]) -> None:
    """Enforce conservative rendered bounds below DynamoDB's item and transaction limits."""

    for action in items:
        if "Put" in action and _rendered_size(action["Put"]["Item"]) >= MAX_DYNAMODB_ITEM_BYTES:
            raise ValueError("A publication request item exceeds the DynamoDB item bound")
        if "ConditionCheck" in action:
            expected_payload = action["ConditionCheck"]["ExpressionAttributeValues"].get(
                ":expected_payload"
            )
            if expected_payload is not None and _rendered_size(expected_payload) >= (
                MAX_DYNAMODB_ITEM_BYTES
            ):
                raise ValueError("A publication authority payload exceeds the DynamoDB item bound")
    if _rendered_size(items) >= MAX_DYNAMODB_TRANSACTION_BYTES:
        raise ValueError("Publication request transaction exceeds the DynamoDB transaction bound")


class DynamoDBPublicationStore:
    """Single-table adapter whose request write is exactly fifteen atomic actions."""

    def __init__(self, *, client: Any, table_name: str) -> None:
        self._client = client
        self._table_name = table_name

    def resolve_request_receipt(
        self,
        owner_id: str,
        job_id: str,
        key_digest: str,
    ) -> PublicationCommandReceipt | None:
        item = self._get(
            _owner_pk(owner_id),
            publication_request_receipt_sort_key(job_id, key_digest),
        )
        if item is None:
            return None
        payload = item.get("payload", {}).get("S")
        if (
            item.get("entity_type", {}).get("S") != "PUBLICATION_COMMAND_RECEIPT"
            or item.get("job_id", {}).get("S") != job_id
            or item.get("command_type", {}).get("S") != "request_publication"
            or item.get("key_digest", {}).get("S") != key_digest
            or not payload
        ):
            return None
        try:
            receipt = PublicationCommandReceipt.model_validate_json(payload)
        except (ValidationError, ValueError):
            return None
        if (
            receipt.owner_id != owner_id
            or receipt.job_id != job_id
            or receipt.command_type.value != item["command_type"]["S"]
            or receipt.idempotency_key_digest != key_digest
            or receipt.idempotency_key_digest != item["key_digest"]["S"]
            or receipt.request_fingerprint != item.get("request_fingerprint", {}).get("S")
            or receipt.contract_version != item.get("contract_version", {}).get("S")
        ):
            return None
        locator = build_publication_request_receipt_locator(
            aggregate_id=receipt.aggregate_id,
            owner_id=receipt.owner_id,
            job_id=receipt.job_id,
            receipt_id=receipt.receipt_id,
            receipt_fingerprint=receipt.fingerprint,
            idempotency_key_digest=receipt.idempotency_key_digest,
        )
        locator_item = self._get(
            _publication_pk(receipt.aggregate_id),
            PUBLICATION_REQUEST_RECEIPT_LOCATOR_SORT_KEY,
        )
        expected_locator_item = _publication_record_item(
            aggregate_id=receipt.aggregate_id,
            sort_key=PUBLICATION_REQUEST_RECEIPT_LOCATOR_SORT_KEY,
            entity_type=PUBLICATION_REQUEST_RECEIPT_LOCATOR_ENTITY_TYPE,
            record=locator,
        )
        if locator_item is None:
            return None
        unexpected_fields = set(locator_item) - {*expected_locator_item, "expires_at"}
        expires_at = locator_item.get("expires_at")
        if (
            unexpected_fields
            or any(locator_item.get(key) != value for key, value in expected_locator_item.items())
            or (
                expires_at is not None
                and (
                    not isinstance(expires_at, Mapping)
                    or set(expires_at) != {"N"}
                    or not isinstance(expires_at.get("N"), str)
                    or not expires_at["N"].isdigit()
                    or int(expires_at["N"]) < 1
                    or str(int(expires_at["N"])) != expires_at["N"]
                )
            )
        ):
            return None
        return receipt

    def load_request_authority(
        self,
        owner_id: str,
        job_id: str,
    ) -> PublicationRequestAuthority:
        item = self._get(_job_pk(job_id), "META")
        payload = None if item is None else item.get("payload", {}).get("S")
        if (
            item is None
            or item.get("entity_type", {}).get("S") != "CONTROL_JOB"
            or item.get("owner_id", {}).get("S") != owner_id
            or not payload
        ):
            raise PublicationNotFoundError()
        try:
            job = ControlJobRecord.model_validate_json(payload)
        except (ValidationError, ValueError):
            raise PublicationNotFoundError() from None
        if job.owner_id != owner_id or job.job_id != job_id:
            raise PublicationNotFoundError()
        if job.state is not ControlJobState.APPROVED:
            raise PublicationAuthorityError(
                PublicationErrorCode.NOT_APPROVED,
                "The job is not approved for publication",
            )
        if (
            job.approval_decision_id is None
            or job.product_sync_id is None
            or job.pricing_snapshot_id is None
        ):
            raise PublicationAuthorityError(
                PublicationErrorCode.INVALID_AUTHORITY,
                "The approved publication authority is incomplete",
            )

        authority = PublicationRequestAuthority(
            current_job=job,
            review=self._load_authority_record(
                job_id=job_id,
                sort_key=f"REVIEW#{job.review_version:020d}",
                entity_type="REVIEW",
                model=ReviewContent,
            ),
            approval_decision=self._load_authority_record(
                job_id=job_id,
                sort_key=f"DECISION#{job.approval_decision_id}",
                entity_type="REVIEW_DECISION",
                model=ReviewDecisionRecord,
            ),
            source=self._load_authority_record(
                job_id=job_id,
                sort_key="SOURCE",
                entity_type="SOURCE_ARTIFACT",
                model=SourceArtifactRecord,
            ),
            product_sync=self._load_authority_record(
                job_id=job_id,
                sort_key=f"PRODUCT_SYNC#{job.product_sync_id}",
                entity_type="PRODUCT_SYNC",
                model=ProductSyncRecord,
            ),
            pricing_snapshot=self._load_authority_record(
                job_id=job_id,
                sort_key=f"PRICING#{job.pricing_snapshot_id}",
                entity_type="PRICING_SNAPSHOT",
                model=PricingSnapshot,
            ),
            pricing_evidence=self._load_authority_record(
                job_id=job_id,
                sort_key=f"PRICING_EVIDENCE#{job.pricing_snapshot_id}",
                entity_type="PRICING_EVIDENCE",
                model=PricingEvidenceRecord,
            ),
        )
        validate_publication_request_authority(authority)
        return authority

    def commit_request(
        self,
        transaction: PublicationRequestTransaction,
    ) -> PublicationCommandReceipt:
        validate_publication_request_transaction(transaction)
        commit = transaction.commit
        items = self._request_transaction_items(transaction)
        if len(items) != PUBLICATION_REQUEST_TRANSACTION_ITEMS:
            raise ValueError("Publication request transaction must contain exactly 15 actions")
        if len(items) > MAX_PUBLICATION_REQUEST_TRANSACTION_ITEMS:
            raise ValueError("Publication request transaction exceeds its conservative bound")
        _validate_transaction_envelope(items)
        try:
            self._client.transact_write_items(
                TransactItems=items,
                ClientRequestToken=_transaction_client_token(items),
            )
        except ClientError as error:
            if not self._requires_receipt_resolution(error):
                raise
            existing = self.resolve_request_receipt(
                commit.receipt.owner_id,
                commit.receipt.job_id,
                commit.receipt.idempotency_key_digest,
            )
            if existing is not None:
                if existing.request_fingerprint == commit.receipt.request_fingerprint:
                    return existing
                raise PublicationIdempotencyConflictError() from None
            if not self._is_concurrency_cancellation(error):
                raise
            raise PublicationConflictError(
                PublicationErrorCode.CONCURRENT_WRITE,
                "Publication authority changed before the request could commit",
            ) from None
        return commit.receipt

    def get_aggregate_for_owner(
        self,
        owner_id: str,
        job_id: str,
    ) -> PublicationAggregate:
        item = self._get(_job_pk(job_id), "META")
        payload = None if item is None else item.get("payload", {}).get("S")
        if (
            item is None
            or item.get("entity_type", {}).get("S") != "CONTROL_JOB"
            or item.get("owner_id", {}).get("S") != owner_id
            or not payload
        ):
            raise PublicationNotFoundError()
        try:
            job = ControlJobRecord.model_validate_json(payload)
        except (ValidationError, ValueError):
            raise PublicationNotFoundError() from None
        if job.owner_id != owner_id or job.job_id != job_id or job.publication_aggregate_id is None:
            raise PublicationNotFoundError()
        aggregate_item = self._get(_publication_pk(job.publication_aggregate_id), "META")
        aggregate_payload = (
            None if aggregate_item is None else aggregate_item.get("payload", {}).get("S")
        )
        if (
            aggregate_item is None
            or aggregate_item.get("entity_type", {}).get("S") != "PUBLICATION_AGGREGATE"
            or aggregate_item.get("owner_id", {}).get("S") != owner_id
            or aggregate_item.get("job_id", {}).get("S") != job_id
            or not aggregate_payload
        ):
            raise PublicationAuthorityError(
                PublicationErrorCode.INVALID_AUTHORITY,
                "The publication aggregate link is incomplete",
            )
        try:
            aggregate = PublicationAggregate.model_validate_json(aggregate_payload)
        except (ValidationError, ValueError):
            raise PublicationAuthorityError(
                PublicationErrorCode.INVALID_AUTHORITY,
                "The publication aggregate link is incomplete",
            ) from None
        if (
            aggregate.aggregate_id != job.publication_aggregate_id
            or aggregate.owner_id != owner_id
            or aggregate.job_id != job_id
        ):
            raise PublicationAuthorityError(
                PublicationErrorCode.INVALID_AUTHORITY,
                "The publication aggregate link is incomplete",
            )
        return aggregate

    def _request_transaction_items(
        self,
        transaction: PublicationRequestTransaction,
    ) -> list[dict[str, Any]]:
        authority = transaction.authority
        current = authority.current_job
        commit: PublicationRequestCommit = transaction.commit
        receipt_locator = build_publication_request_receipt_locator(
            aggregate_id=commit.receipt.aggregate_id,
            owner_id=commit.receipt.owner_id,
            job_id=commit.receipt.job_id,
            receipt_id=commit.receipt.receipt_id,
            receipt_fingerprint=commit.receipt.fingerprint,
            idempotency_key_digest=commit.receipt.idempotency_key_digest,
        )
        job_put = {
            "Put": {
                "TableName": self._table_name,
                "Item": _job_item(transaction.updated_job),
                "ConditionExpression": (
                    "entity_type = :entity_type AND contract_version = :contract_version AND "
                    "owner_id = :owner_id AND owner_jobs_pk = :owner_jobs_pk AND "
                    "owner_jobs_sk = :owner_jobs_sk AND "
                    "record_version = :record_version AND event_sequence = :event_sequence AND "
                    "#state = :state AND review_version = :review_version AND "
                    "cancellation_requested = :cancellation_requested AND "
                    "payload = :expected_payload"
                ),
                "ExpressionAttributeNames": {"#state": "state"},
                "ExpressionAttributeValues": {
                    ":entity_type": _s("CONTROL_JOB"),
                    ":contract_version": _s(current.contract_version),
                    ":owner_id": _s(current.owner_id),
                    ":owner_jobs_pk": _s(_owner_pk(current.owner_id)),
                    ":owner_jobs_sk": _s(owner_job_sort_key(current)),
                    ":record_version": _n(current.record_version),
                    ":event_sequence": _n(current.event_sequence),
                    ":state": _s(current.state.value),
                    ":review_version": _n(current.review_version),
                    ":cancellation_requested": _bool(current.cancellation_requested_at is not None),
                    ":expected_payload": _s(_payload(current)),
                },
            }
        }
        authority_checks = [
            _authority_condition(
                table_name=self._table_name,
                job_id=current.job_id,
                sort_key=f"REVIEW#{authority.review.review_version:020d}",
                entity_type="REVIEW",
                record=authority.review,
            ),
            _authority_condition(
                table_name=self._table_name,
                job_id=current.job_id,
                sort_key=f"DECISION#{authority.approval_decision.decision_id}",
                entity_type="REVIEW_DECISION",
                record=authority.approval_decision,
            ),
            _authority_condition(
                table_name=self._table_name,
                job_id=current.job_id,
                sort_key="SOURCE",
                entity_type="SOURCE_ARTIFACT",
                record=authority.source,
            ),
            _authority_condition(
                table_name=self._table_name,
                job_id=current.job_id,
                sort_key=f"PRODUCT_SYNC#{authority.product_sync.sync_id}",
                entity_type="PRODUCT_SYNC",
                record=authority.product_sync,
            ),
            _authority_condition(
                table_name=self._table_name,
                job_id=current.job_id,
                sort_key=f"PRICING#{authority.pricing_snapshot.snapshot_id}",
                entity_type="PRICING_SNAPSHOT",
                record=authority.pricing_snapshot,
            ),
            _authority_condition(
                table_name=self._table_name,
                job_id=current.job_id,
                sort_key=f"PRICING_EVIDENCE#{authority.pricing_evidence.snapshot_id}",
                entity_type="PRICING_EVIDENCE",
                record=authority.pricing_evidence,
            ),
        ]
        aggregate_id = commit.aggregate.aggregate_id
        new_records = [
            _put_new(_aggregate_item(commit.aggregate), self._table_name),
            _put_new(
                _publication_record_item(
                    aggregate_id=aggregate_id,
                    sort_key=f"SNAPSHOT#{commit.snapshot.snapshot_id}",
                    entity_type="PUBLICATION_SNAPSHOT",
                    record=commit.snapshot,
                ),
                self._table_name,
            ),
            _put_new(
                _publication_record_item(
                    aggregate_id=aggregate_id,
                    sort_key=f"ATTEMPT#{commit.attempt.attempt_id}",
                    entity_type="PUBLICATION_ATTEMPT",
                    record=commit.attempt,
                ),
                self._table_name,
            ),
            _put_new(
                _publication_record_item(
                    aggregate_id=aggregate_id,
                    sort_key=f"PERMIT#{commit.permit.permit_id}",
                    entity_type="PUBLICATION_PERMIT",
                    record=commit.permit,
                ),
                self._table_name,
            ),
            _put_new(_work_item(commit.work_request), self._table_name),
            _put_new(
                _publication_record_item(
                    aggregate_id=aggregate_id,
                    sort_key=f"EVENT#{commit.event.sequence:020d}",
                    entity_type="PUBLICATION_DOMAIN_EVENT",
                    record=commit.event,
                ),
                self._table_name,
            ),
            _put_new(_receipt_item(commit.receipt), self._table_name),
            _put_new(
                _publication_record_item(
                    aggregate_id=aggregate_id,
                    sort_key=PUBLICATION_REQUEST_RECEIPT_LOCATOR_SORT_KEY,
                    entity_type=PUBLICATION_REQUEST_RECEIPT_LOCATOR_ENTITY_TYPE,
                    record=receipt_locator,
                ),
                self._table_name,
            ),
        ]
        return [job_put, *authority_checks, *new_records]

    def _load_authority_record(
        self,
        *,
        job_id: str,
        sort_key: str,
        entity_type: str,
        model: Any,
    ) -> Any:
        item = self._get(_job_pk(job_id), sort_key)
        payload = None if item is None else item.get("payload", {}).get("S")
        if item is None or item.get("entity_type", {}).get("S") != entity_type or not payload:
            raise PublicationAuthorityError(
                PublicationErrorCode.INVALID_AUTHORITY,
                "The approved publication authority is incomplete",
            )
        try:
            return model.model_validate_json(payload)
        except (ValidationError, ValueError):
            raise PublicationAuthorityError(
                PublicationErrorCode.INVALID_AUTHORITY,
                "The approved publication authority is incomplete",
            ) from None

    def _get(self, partition_key: str, sort_key: str) -> dict[str, Any] | None:
        response = self._client.get_item(
            TableName=self._table_name,
            Key={"PK": _s(partition_key), "SK": _s(sort_key)},
            ConsistentRead=True,
        )
        return response.get("Item")

    @staticmethod
    def _requires_receipt_resolution(error: ClientError) -> bool:
        return error.response.get("Error", {}).get("Code") in {
            "TransactionCanceledException",
            "IdempotentParameterMismatchException",
        }

    @staticmethod
    def _is_concurrency_cancellation(error: ClientError) -> bool:
        code = error.response.get("Error", {}).get("Code")
        if code == "IdempotentParameterMismatchException":
            return True
        reasons = error.response.get("CancellationReasons")
        if not isinstance(reasons, list) or not reasons:
            return False
        reason_codes = {
            reason.get("Code")
            for reason in reasons
            if isinstance(reason, dict) and reason.get("Code") not in {None, "None"}
        }
        return bool(reason_codes) and reason_codes.issubset(
            {"ConditionalCheckFailed", "TransactionConflict"}
        )
