"""Regression coverage for Phase 6.2 provider mutation failure boundaries.

These tests intentionally live outside the economics suite.  They exercise the
application-owned one-shot permit boundary directly: an unused permit can be
retired, while a consumed permit can only be reconciled with provider GETs.
"""

from __future__ import annotations

import json
from datetime import timedelta

import pytest
import test_phase6_control_service as control_fixtures
import test_phase6_dynamodb_store as dynamo_fixtures
import test_phase6_provider_worker as provider_fixtures

from mr_lister.control.commands import (
    CancelJobCommand,
    RecordWorkerFailureCommand,
    SettleCancellationCommand,
    WorkerFailureCode,
)
from mr_lister.control.errors import InvalidControlStateError, WorkNotActiveError
from mr_lister.control.models import (
    ControlJobRecord,
    ControlJobState,
    ProviderCallPermit,
    ProviderCallPermitStatus,
    ProviderWriteAttempt,
    ProviderWriteOperation,
    WorkRequest,
    WorkRequestStatus,
    WorkType,
)
from mr_lister.control.service import SellerControlService
from mr_lister.control.store import InMemorySellerControlStore
from mr_lister.control.worker_commands import (
    BeginProviderUploadCommand,
    BeginProviderWriteCommand,
    RecordProviderUploadSuccessCommand,
    UploadedArtworkObservation,
)
from mr_lister.control.worker_service import WorkerControlService
from mr_lister.production.draft_sync import build_canonical_draft, job_correlation_token


def _claim_available_provider_permit(
    *,
    boundary: str,
    job_id: str,
) -> tuple[
    InMemorySellerControlStore,
    WorkerControlService,
    ControlJobRecord,
    WorkRequest,
    str,
]:
    store = InMemorySellerControlStore()
    syncing, _review, work = control_fixtures.seed_product_syncing(store, job_id=job_id)
    worker = WorkerControlService(store=store, clock=lambda: control_fixtures.NOW)
    source = store.get_source_artifact(job_id)
    file_name = worker.upload_file_name(job_id, source.content_sha256)
    worker.begin_provider_upload(
        BeginProviderUploadCommand(
            job_id=job_id,
            work_request_id=work.work_request_id,
            expected_record_version=syncing.record_version,
            source_artifact_fingerprint=source.fingerprint,
            file_name=file_name,
        )
    )
    upload_claim = store.get_job(job_id)
    upload_attempt_id = upload_claim.provider_upload_attempt_id
    assert upload_attempt_id is not None
    if boundary == "upload":
        return store, worker, upload_claim, work, upload_attempt_id

    assert boundary == "product"
    assert (
        worker.authorize_provider_upload(
            job_id=job_id,
            attempt_id=upload_attempt_id,
        )
        is not None
    )
    worker.record_provider_upload_success(
        RecordProviderUploadSuccessCommand(
            job_id=job_id,
            work_request_id=work.work_request_id,
            expected_record_version=upload_claim.record_version,
            attempt_id=upload_attempt_id,
            observation=UploadedArtworkObservation(
                image_id="printify_image_failure_boundary",
                file_name=file_name,
                width=3021,
                height=3927,
                size_bytes=source.size_bytes,
            ),
        )
    )
    uploaded = store.get_job(job_id)
    worker.begin_provider_write(
        BeginProviderWriteCommand(
            job_id=job_id,
            work_request_id=work.work_request_id,
            expected_record_version=uploaded.record_version,
            image_id="printify_image_failure_boundary",
            target_payload_fingerprint="d" * 64,
            correlation_token=job_correlation_token(job_id),
        )
    )
    product_claim = store.get_job(job_id)
    product_attempt_id = product_claim.provider_write_attempt_id
    assert product_attempt_id is not None
    return store, worker, product_claim, work, product_attempt_id


@pytest.mark.parametrize("boundary", ("upload", "product"))
def test_cancellation_retires_available_provider_permit_and_finishes_cancelled(
    boundary: str,
) -> None:
    store, _worker, claimed, work, attempt_id = _claim_available_provider_permit(
        boundary=boundary,
        job_id=f"job_cancel_available_{boundary}",
    )
    seller = SellerControlService(store=store, clock=lambda: control_fixtures.NOW)

    cancelling = seller.cancel_job(
        CancelJobCommand(
            job_id=claimed.job_id,
            owner_id=claimed.owner_id,
            expected_record_version=claimed.record_version,
            idempotency_key=f"cancel-available-{boundary}",
        )
    )
    settled = seller.settle_cancellation(
        SettleCancellationCommand(
            job_id=claimed.job_id,
            work_request_id=work.work_request_id,
            expected_record_version=cancelling.record_version,
        )
    )

    permit = store.get_provider_call_permit(claimed.job_id, attempt_id)
    persisted = store.get_job(claimed.job_id)
    assert cancelling.state is ControlJobState.CANCEL_REQUESTED
    assert settled.state is ControlJobState.CANCELLED
    assert settled.work_request_id is None
    assert persisted.active_work_request_id is None
    assert persisted.upload_outcome_unconfirmed is False
    assert persisted.provider_outcome_unconfirmed is False
    assert store.get_work_request(claimed.job_id, work.work_request_id).status is (
        WorkRequestStatus.COMPLETED
    )
    assert permit.status is ProviderCallPermitStatus.RETIRED
    assert permit.retired_at == control_fixtures.NOW
    assert permit.consumed_at is None
    assert permit.consumed_work_request_id is None


@pytest.mark.parametrize("boundary", ("upload", "product"))
def test_generic_failure_after_consumed_provider_permit_goes_directly_to_get_only_reconciliation(
    boundary: str,
) -> None:
    store, worker, claimed, work, attempt_id = _claim_available_provider_permit(
        boundary=boundary,
        job_id=f"job_consumed_failure_{boundary}",
    )
    if boundary == "upload":
        assert (
            worker.authorize_provider_upload(
                job_id=claimed.job_id,
                attempt_id=attempt_id,
            )
            is not None
        )
    else:
        assert (
            worker.authorize_provider_call(
                job_id=claimed.job_id,
                attempt_id=attempt_id,
            )
            is not None
        )

    result = SellerControlService(
        store=store,
        clock=lambda: control_fixtures.NOW,
    ).record_worker_failure(
        RecordWorkerFailureCommand(
            job_id=claimed.job_id,
            work_request_id=work.work_request_id,
            expected_record_version=claimed.record_version,
            code=WorkerFailureCode.PRODUCTION_UNAVAILABLE,
        )
    )

    permit = store.get_provider_call_permit(claimed.job_id, attempt_id)
    original_work = store.get_work_request(claimed.job_id, work.work_request_id)
    assert result.state is ControlJobState.RECONCILIATION_REQUIRED
    assert result.state is not ControlJobState.FAILED_RETRYABLE
    assert result.work_request_id is not None
    assert result.work_request_id != work.work_request_id
    assert store.get_work_request(claimed.job_id, result.work_request_id).work_type is (
        WorkType.RECONCILE_PRODUCT
    )
    assert original_work.status is WorkRequestStatus.COMPLETED
    assert permit.status is ProviderCallPermitStatus.CONSUMED
    assert permit.consumed_work_request_id == work.work_request_id
    assert store.list_failures(claimed.job_id) == ()


def test_dynamodb_cancellation_retires_available_permit_in_same_transaction() -> None:
    client = dynamo_fixtures.MemoryLowLevelDynamoClient()
    store = dynamo_fixtures.DynamoDBSellerControlStore(
        client=client,
        table_name=dynamo_fixtures.TABLE_NAME,
    )
    claimed, work, attempt_id = dynamo_fixtures.seed_provider_permit(
        store,
        dispatch_sync_work=True,
    )
    seller = SellerControlService(
        store=store,
        clock=lambda: dynamo_fixtures.NOW + timedelta(seconds=4),
    )
    cancelling = seller.cancel_job(
        CancelJobCommand(
            job_id=claimed.job_id,
            owner_id=claimed.owner_id,
            expected_record_version=claimed.record_version,
            idempotency_key="cancel-dynamo-available-provider-permit",
        )
    )

    settled = seller.settle_cancellation(
        SettleCancellationCommand(
            job_id=claimed.job_id,
            work_request_id=work.work_request_id,
            expected_record_version=cancelling.record_version,
        )
    )

    assert settled.state is ControlJobState.CANCELLED
    persisted_permit = store.get_provider_call_permit(claimed.job_id, attempt_id)
    assert persisted_permit.status is ProviderCallPermitStatus.RETIRED
    transaction = client.transactions[-1]["TransactItems"]
    permit_puts = [
        operation["Put"]
        for operation in transaction
        if "Put" in operation
        and operation["Put"]["Item"]["SK"]["S"] == f"PROVIDER_PERMIT#{attempt_id}"
    ]
    assert len(permit_puts) == 1
    permit_put = permit_puts[0]
    assert permit_put["ConditionExpression"] == "payload = :expected_payload"
    written = ProviderCallPermit.model_validate(json.loads(permit_put["Item"]["payload"]["S"]))
    expected = ProviderCallPermit.model_validate(
        json.loads(permit_put["ExpressionAttributeValues"][":expected_payload"]["S"])
    )
    assert expected.status is ProviderCallPermitStatus.AVAILABLE
    assert written.status is ProviderCallPermitStatus.RETIRED
    assert written.retired_at == dynamo_fixtures.NOW + timedelta(seconds=4)


def test_pending_dynamodb_cancellation_retires_available_permit_immediately() -> None:
    client = dynamo_fixtures.MemoryLowLevelDynamoClient()
    store = dynamo_fixtures.DynamoDBSellerControlStore(
        client=client,
        table_name=dynamo_fixtures.TABLE_NAME,
    )
    claimed, work, attempt_id = dynamo_fixtures.seed_provider_permit(
        store,
        dispatch_sync_work=False,
    )
    assert work.claim_id is not None
    now = dynamo_fixtures.NOW + timedelta(seconds=4)
    pending = store.release_work(
        claimed.job_id,
        work.work_request_id,
        claim_id=work.claim_id,
        next_dispatch_at=now + timedelta(minutes=1),
        error_code="PROVIDER_NOT_STARTED",
        now=now,
    )

    cancelled = SellerControlService(store=store, clock=lambda: now).cancel_job(
        CancelJobCommand(
            job_id=claimed.job_id,
            owner_id=claimed.owner_id,
            expected_record_version=claimed.record_version,
            idempotency_key="cancel-pending-dynamo-provider-permit",
        )
    )

    persisted = store.get_job(claimed.job_id)
    permit = store.get_provider_call_permit(claimed.job_id, attempt_id)
    assert pending.status is WorkRequestStatus.PENDING
    assert cancelled.state is ControlJobState.CANCELLED
    assert persisted.provider_outcome_unconfirmed is False
    assert persisted.upload_outcome_unconfirmed is False
    assert permit.status is ProviderCallPermitStatus.RETIRED
    assert permit.retired_at == now
    assert store.get_work_request(claimed.job_id, work.work_request_id).status is (
        WorkRequestStatus.CANCELLED
    )
    transaction = client.transactions[-1]["TransactItems"]
    permit_puts = [
        operation["Put"]
        for operation in transaction
        if "Put" in operation
        and operation["Put"]["Item"]["SK"]["S"] == f"PROVIDER_PERMIT#{attempt_id}"
    ]
    assert len(permit_puts) == 1
    assert permit_puts[0]["ConditionExpression"] == "payload = :expected_payload"


def test_failure_after_cancellation_retires_available_permit_atomically() -> None:
    store, _worker, claimed, work, attempt_id = _claim_available_provider_permit(
        boundary="product",
        job_id="job_cancelled_failure_available_product",
    )
    seller = SellerControlService(store=store, clock=lambda: control_fixtures.NOW)
    cancelling = seller.cancel_job(
        CancelJobCommand(
            job_id=claimed.job_id,
            owner_id=claimed.owner_id,
            expected_record_version=claimed.record_version,
            idempotency_key="cancel-before-available-provider-failure",
        )
    )

    settled = seller.record_worker_failure(
        RecordWorkerFailureCommand(
            job_id=claimed.job_id,
            work_request_id=work.work_request_id,
            expected_record_version=cancelling.record_version,
            code=WorkerFailureCode.PRODUCTION_UNAVAILABLE,
        )
    )

    persisted = store.get_job(claimed.job_id)
    permit = store.get_provider_call_permit(claimed.job_id, attempt_id)
    assert cancelling.state is ControlJobState.CANCEL_REQUESTED
    assert settled.state is ControlJobState.CANCELLED
    assert persisted.provider_outcome_unconfirmed is False
    assert persisted.upload_outcome_unconfirmed is False
    assert permit.status is ProviderCallPermitStatus.RETIRED


def test_retryable_failure_cancellation_retires_available_permit_without_work() -> None:
    store, _worker, claimed, work, attempt_id = _claim_available_provider_permit(
        boundary="product",
        job_id="job_cancel_retryable_available_product",
    )
    seller = SellerControlService(store=store, clock=lambda: control_fixtures.NOW)
    failed = seller.record_worker_failure(
        RecordWorkerFailureCommand(
            job_id=claimed.job_id,
            work_request_id=work.work_request_id,
            expected_record_version=claimed.record_version,
            code=WorkerFailureCode.PRODUCTION_UNAVAILABLE,
        )
    )
    assert failed.state is ControlJobState.FAILED_RETRYABLE

    cancelled = seller.cancel_job(
        CancelJobCommand(
            job_id=claimed.job_id,
            owner_id=claimed.owner_id,
            expected_record_version=failed.record_version,
            idempotency_key="cancel-retryable-available-product",
        )
    )

    persisted = store.get_job(claimed.job_id)
    permit = store.get_provider_call_permit(claimed.job_id, attempt_id)
    assert cancelled.state is ControlJobState.CANCELLED
    assert persisted.active_work_request_id is None
    assert persisted.provider_outcome_unconfirmed is False
    assert persisted.upload_outcome_unconfirmed is False
    assert permit.status is ProviderCallPermitStatus.RETIRED


def test_update_reconstructs_exact_prior_before_consuming_permit() -> None:
    prior_sync, prior_payload_fingerprint = provider_fixtures._prior_sync()
    synchronizer = provider_fixtures.FakeSynchronizer()
    job = provider_fixtures._job(
        review_version=2,
        product_id="product_1",
        prior_payload_fingerprint=prior_payload_fingerprint,
        product_sync_id=prior_sync.sync_id,
        product_sync_fingerprint=prior_sync.fingerprint,
        synchronized_review_version=1,
    )
    worker, store, _control, _resources = provider_fixtures._worker(
        job=job,
        work=provider_fixtures._work(
            work_type=WorkType.SYNCHRONIZE_PRODUCT,
            work_id=provider_fixtures.SYNC_WORK_ID,
            review_version=2,
        ),
        synchronizer=synchronizer,
    )
    # The job points at prior authority that is unavailable.  A PUT must not be
    # authorized while exact reconstruction of that authority is impossible.
    assert prior_sync.sync_id not in store.syncs

    with pytest.raises(KeyError):
        worker.run_product_sync(
            job_id=provider_fixtures.JOB_ID,
            work_request_id=provider_fixtures.SYNC_WORK_ID,
        )

    attempt_id = store.job.provider_write_attempt_id
    assert attempt_id is not None
    permit = store.get_provider_call_permit(provider_fixtures.JOB_ID, attempt_id)
    assert permit.status is ProviderCallPermitStatus.AVAILABLE
    assert permit.consumed_at is None
    assert permit.consumed_work_request_id is None
    assert synchronizer.mutations == []


def test_consumed_unresolved_update_retry_cannot_mint_a_new_attempt_or_put() -> None:
    prior_sync, prior_payload_fingerprint = provider_fixtures._prior_sync()
    recovery_work_id = "work_retry_consumed_update"
    original_work_id = provider_fixtures.SYNC_WORK_ID
    job = provider_fixtures._job(
        review_version=2,
        work_id=recovery_work_id,
        product_id="product_1",
        prior_payload_fingerprint=prior_payload_fingerprint,
        product_sync_id=prior_sync.sync_id,
        product_sync_fingerprint=prior_sync.fingerprint,
        synchronized_review_version=1,
    ).model_copy(
        update={
            "provider_write_attempt_id": "attempt_consumed_update",
            "provider_outcome_unconfirmed": True,
        }
    )
    synchronizer = provider_fixtures.FakeSynchronizer()
    worker, store, _control, _resources = provider_fixtures._worker(
        job=job,
        work=provider_fixtures._work(
            work_type=WorkType.SYNCHRONIZE_PRODUCT,
            work_id=recovery_work_id,
            review_version=2,
        ),
        synchronizer=synchronizer,
    )
    store.syncs[prior_sync.sync_id] = prior_sync
    store.works[original_work_id] = provider_fixtures._work(
        work_type=WorkType.SYNCHRONIZE_PRODUCT,
        work_id=original_work_id,
        review_version=2,
    ).model_copy(
        update={
            "status": WorkRequestStatus.COMPLETED,
            "execution_arn": None,
            "updated_at": provider_fixtures.NOW,
        }
    )
    target = build_canonical_draft(
        job_id=provider_fixtures.JOB_ID,
        listing=provider_fixtures._listing(provider_fixtures._review(2)),
        profile=provider_fixtures._profile(),
        resolved=provider_fixtures._resolved(),
        image_id="image_old",
    )
    old_attempt = ProviderWriteAttempt(
        attempt_id="attempt_consumed_update",
        job_id=provider_fixtures.JOB_ID,
        work_request_id=original_work_id,
        review_version=2,
        operation=ProviderWriteOperation.UPDATE,
        product_id="product_1",
        image_id="image_old",
        target_payload_fingerprint=target.payload_fingerprint,
        prior_payload_fingerprint=prior_payload_fingerprint,
        correlation_token=job_correlation_token(provider_fixtures.JOB_ID),
        reconciliation_deadline=provider_fixtures.NOW + timedelta(minutes=15),
        started_at=provider_fixtures.NOW,
    )
    store.attempts[old_attempt.attempt_id] = old_attempt
    store.permits[old_attempt.attempt_id] = ProviderCallPermit(
        attempt_id=old_attempt.attempt_id,
        job_id=provider_fixtures.JOB_ID,
        work_request_id=original_work_id,
        status=ProviderCallPermitStatus.CONSUMED,
        consumed_at=provider_fixtures.NOW,
        consumed_work_request_id=original_work_id,
        created_at=provider_fixtures.NOW,
    )

    try:
        worker.run_product_sync(
            job_id=provider_fixtures.JOB_ID,
            work_request_id=recovery_work_id,
        )
    except (InvalidControlStateError, WorkNotActiveError):
        # Rejecting an impossible legacy state is also safe.  What is forbidden
        # is creating a second one-shot attempt and issuing another provider PUT.
        pass

    assert tuple(store.attempts) == (old_attempt.attempt_id,)
    assert tuple(store.permits) == (old_attempt.attempt_id,)
    assert store.permits[old_attempt.attempt_id].status is ProviderCallPermitStatus.CONSUMED
    assert synchronizer.mutations == []
