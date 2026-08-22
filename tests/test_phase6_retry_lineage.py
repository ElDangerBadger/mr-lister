from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from botocore.exceptions import ClientError

from mr_lister.control.dynamodb import DynamoDBSellerControlStore
from mr_lister.control.errors import ConcurrentControlModificationError
from mr_lister.control.models import (
    CommandReceipt,
    CommandResponse,
    ControlJobRecord,
    ControlJobState,
    DomainEvent,
    ProviderCallPermit,
    ProviderWriteAttempt,
    ProviderWriteOperation,
)
from mr_lister.control.store import (
    CommandCommit,
    InMemorySellerControlStore,
    validate_command_commit,
)

NOW = datetime(2026, 8, 22, 12, 0, tzinfo=UTC)
OWNER = "a" * 64


def _retry_commit() -> tuple[CommandCommit, ProviderWriteAttempt]:
    basis = ProviderWriteAttempt(
        attempt_id="attempt_update_initial",
        job_id="job_retry_lineage",
        work_request_id="work_update_initial",
        review_version=2,
        operation=ProviderWriteOperation.UPDATE,
        product_id="product_existing",
        image_id="image_confirmed",
        target_payload_fingerprint="2" * 64,
        prior_payload_fingerprint="1" * 64,
        correlation_token=f"ml-{'3' * 24}",
        exact_retry_count=0,
        reconciliation_deadline=NOW + timedelta(minutes=10),
        started_at=NOW - timedelta(minutes=5),
    )
    current = ControlJobRecord(
        owner_id=OWNER,
        job_id=basis.job_id,
        record_version=7,
        event_sequence=8,
        state=ControlJobState.PRODUCT_DRAFT_SYNCING,
        review_version=2,
        review_fingerprint="4" * 64,
        review_validated=True,
        source_artifact_fingerprint="5" * 64,
        product_id=basis.product_id,
        provider_payload_fingerprint=basis.prior_payload_fingerprint,
        active_work_request_id="work_update_retry",
        provider_upload_attempt_id="attempt_upload",
        uploaded_artwork_id="upload_confirmed",
        uploaded_image_id=basis.image_id,
        uploaded_artwork_fingerprint="6" * 64,
        provider_write_attempt_id=basis.attempt_id,
        product_create_attempt_id="attempt_create_initial",
        created_at=NOW - timedelta(hours=1),
        updated_at=NOW,
    )
    retry = ProviderWriteAttempt(
        attempt_id="attempt_update_retry",
        job_id=current.job_id,
        work_request_id=current.active_work_request_id or "",
        review_version=current.review_version,
        operation=ProviderWriteOperation.UPDATE,
        product_id=current.product_id,
        image_id=current.uploaded_image_id or "",
        target_payload_fingerprint=basis.target_payload_fingerprint,
        prior_payload_fingerprint=current.provider_payload_fingerprint,
        correlation_token=basis.correlation_token,
        exact_retry_count=1,
        reconciliation_deadline=basis.reconciliation_deadline,
        started_at=NOW + timedelta(seconds=1),
    )
    permit = ProviderCallPermit(
        attempt_id=retry.attempt_id,
        job_id=retry.job_id,
        work_request_id=retry.work_request_id,
        created_at=retry.started_at,
    )
    updated = ControlJobRecord.model_validate(
        {
            **current.model_dump(mode="python"),
            "record_version": current.record_version + 1,
            "event_sequence": current.event_sequence + 1,
            "provider_write_attempt_id": retry.attempt_id,
            "provider_outcome_unconfirmed": True,
            "updated_at": retry.started_at,
        }
    )
    receipt = CommandReceipt(
        receipt_id="receipt_update_retry",
        owner_id=updated.owner_id,
        job_id=updated.job_id,
        command_type="begin_provider_write",
        idempotency_key_digest="7" * 64,
        request_fingerprint="8" * 64,
        response=CommandResponse(
            job_id=updated.job_id,
            state=updated.state,
            record_version=updated.record_version,
            review_version=updated.review_version,
        ),
        created_at=updated.updated_at,
    )
    commit = CommandCommit(
        current=current,
        updated=updated,
        event=DomainEvent(
            job_id=updated.job_id,
            sequence=updated.event_sequence,
            name="PROVIDER_WRITE_CLAIMED",
            occurred_at=updated.updated_at,
        ),
        receipt=receipt,
        provider_write_attempt=retry,
        provider_write_retry_basis=basis,
        provider_call_permit=permit,
    )
    validate_command_commit(commit)
    return commit, basis


def _forged_basis(basis: ProviderWriteAttempt) -> ProviderWriteAttempt:
    return ProviderWriteAttempt.model_validate(
        {
            **basis.model_dump(mode="python"),
            "started_at": basis.started_at - timedelta(seconds=1),
        }
    )


def test_in_memory_retry_requires_the_exact_persisted_attempt_basis() -> None:
    commit, basis = _retry_commit()
    store = InMemorySellerControlStore()
    store._jobs[commit.current.job_id] = commit.current
    store._provider_write_attempts[(basis.job_id, basis.attempt_id)] = basis
    forged = replace(commit, provider_write_retry_basis=_forged_basis(basis))
    validate_command_commit(forged)

    with pytest.raises(ConcurrentControlModificationError, match="retry basis changed"):
        store.commit_command(forged)

    assert store.get_job(commit.current.job_id) == commit.current
    assert store.commit_command(commit) == commit.receipt
    assert store.get_provider_write_attempt(commit.updated.job_id, basis.attempt_id) == basis
    assert (
        store.get_provider_write_attempt(
            commit.updated.job_id,
            commit.provider_write_attempt.attempt_id,
        )
        == commit.provider_write_attempt
    )


class _RetryLineageDynamoClient:
    def __init__(self, basis: ProviderWriteAttempt) -> None:
        self.expected_basis_payload = basis.model_dump_json()
        self.requests: list[dict[str, Any]] = []

    def transact_write_items(self, **request: Any) -> None:
        self.requests.append(request)
        checks = [
            item["ConditionCheck"] for item in request["TransactItems"] if "ConditionCheck" in item
        ]
        if len(checks) != 1:
            raise AssertionError("An exact update retry requires one basis condition check")
        expected = checks[0]["ExpressionAttributeValues"][":expected_retry_basis"]["S"]
        if expected != self.expected_basis_payload:
            raise ClientError(
                {"Error": {"Code": "TransactionCanceledException", "Message": "synthetic"}},
                "TransactWriteItems",
            )

    def get_item(self, **_request: Any) -> dict[str, Any]:
        return {}


def test_dynamodb_retry_transaction_cas_binds_the_persisted_attempt_payload() -> None:
    commit, basis = _retry_commit()
    client = _RetryLineageDynamoClient(basis)
    store = DynamoDBSellerControlStore(client=client, table_name="MrListerPhase6Control")
    forged = replace(commit, provider_write_retry_basis=_forged_basis(basis))
    validate_command_commit(forged)

    with pytest.raises(ConcurrentControlModificationError, match="job changed"):
        store.commit_command(forged)

    assert store.commit_command(commit) == commit.receipt
    check = next(
        item["ConditionCheck"]
        for item in client.requests[-1]["TransactItems"]
        if "ConditionCheck" in item
    )
    assert check["Key"] == {
        "PK": {"S": f"JOB#{commit.current.job_id}"},
        "SK": {"S": f"PROVIDER_ATTEMPT#{basis.attempt_id}"},
    }
    assert check["ConditionExpression"] == "payload = :expected_retry_basis"
    assert check["ExpressionAttributeValues"][":expected_retry_basis"] == {
        "S": basis.model_dump_json()
    }
