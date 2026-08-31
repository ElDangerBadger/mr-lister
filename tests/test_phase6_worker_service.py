from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from hashlib import sha256

import pytest
from pydantic import ValidationError

from mr_lister.agent.contracts import PreparationDecision
from mr_lister.contracts import ArtworkAnalysis, ListingIntelligence
from mr_lister.control.commands import (
    CancelJobCommand,
    ListingRevision,
    RecordWorkerFailureCommand,
    RetryJobCommand,
    ReviseListingCommand,
    WorkerFailureCode,
)
from mr_lister.control.dispatch import deterministic_execution_name, work_input_fingerprint
from mr_lister.control.errors import (
    IdempotencyConflictError,
    InvalidControlStateError,
    WorkNotActiveError,
)
from mr_lister.control.fingerprints import product_sync_record_fingerprint, review_etag
from mr_lister.control.models import (
    CommandReceipt,
    CommandResponse,
    ControlJobRecord,
    ControlJobState,
    DomainEvent,
    ProductMockupEvidence,
    ProductVariantEvidence,
    ProviderCallPermitStatus,
    ProviderWriteOperation,
    ReconciliationOutcome,
    RecoveryAction,
    SourceArtifactRecord,
    WorkRequest,
    WorkRequestStatus,
    WorkType,
)
from mr_lister.control.service import SellerControlService
from mr_lister.control.source_artwork import source_artifact_fingerprint
from mr_lister.control.store import (
    CommandCommit,
    InMemorySellerControlStore,
    validate_command_commit,
)
from mr_lister.control.worker_commands import (
    BeginPreparationCommand,
    BeginProviderUploadCommand,
    BeginProviderWriteCommand,
    CompletePreparationWithAgentDecisionCommand,
    ProductSyncObservation,
    RecordPreparedReviewCommand,
    RecordPricingSuccessCommand,
    RecordProductSyncSuccessCommand,
    RecordProductWriteOutcomeUnknownCommand,
    RecordProviderUploadOutcomeUnknownCommand,
    RecordProviderUploadSuccessCommand,
    RecordReconciliationObservationCommand,
    RecordUploadReconciliationObservationCommand,
    UploadedArtworkObservation,
)
from mr_lister.control.worker_service import WorkerControlService
from mr_lister.production.economics import (
    EtsyUsStandardEstimate,
    ProductCostEvidence,
    ProductVariantCostEvidence,
    estimate_etsy_us_standard_proceeds,
)
from mr_lister.production.printify_shipping import parse_standard_us_shipping

NOW = datetime(2026, 8, 21, 17, 0, tzinfo=UTC)
OWNER = "a" * 64
PROFILE_FP = "c" * 64
TARGET_FP = "d" * 64
RESPONSE_FP = "e" * 64
UPDATED_TARGET_FP = "f" * 64
IMAGE_ID = "printify_image_001"
PRODUCT_ID = "printify_product_001"
PREPARE_WORK_ID = "work_prepare_phase62"
SOURCE_BUCKET = "mr-lister-phase6-artifacts-test"
SOURCE_VERSION_ID = "version_phase62"
SOURCE_CONTENT_SHA256 = "1" * 64
SOURCE_SIZE_BYTES = 512
SOURCE_MEDIA_TYPE = "image/png"
SOURCE_PROFILE_ID = "gildan_5000_test"
SOURCE_PROFILE_VERSION = 1

VALID_TAGS = (
    "badger",
    "woodland",
    "compass",
    "forest",
    "vintage",
    "outdoors",
    "nature",
    "crescent",
    "pine",
    "earthy",
    "camping",
    "wildlife",
    "retro",
)


@dataclass
class MutableClock:
    value: datetime = NOW

    def __call__(self) -> datetime:
        return self.value

    def set(self, value: datetime) -> None:
        self.value = value

    def advance(self, delta: timedelta) -> None:
        self.value += delta


def _response(job: ControlJobRecord, work_id: str | None = None) -> CommandResponse:
    return CommandResponse(
        job_id=job.job_id,
        state=job.state,
        record_version=job.record_version,
        review_version=job.review_version,
        work_request_id=work_id,
    )


def _receipt(
    job: ControlJobRecord,
    *,
    identity: str,
    work_id: str | None = None,
    at: datetime,
) -> CommandReceipt:
    digest = sha256(identity.encode()).hexdigest()
    return CommandReceipt(
        receipt_id=f"receipt_{identity}",
        owner_id=job.owner_id,
        job_id=job.job_id,
        command_type=f"fixture_{identity}",
        idempotency_key_digest=digest,
        request_fingerprint=digest,
        response=_response(job, work_id),
        work_request_id=work_id,
        created_at=at,
    )


def _event(job: ControlJobRecord, name: str, *, at: datetime) -> DomainEvent:
    return DomainEvent(
        job_id=job.job_id,
        sequence=job.event_sequence,
        name=name,
        occurred_at=at,
    )


def _source_fingerprint(
    *,
    job_id: str,
    owner_id: str,
    at: datetime,
    width: int | None = None,
    height: int | None = None,
) -> str:
    return source_artifact_fingerprint(
        job_id=job_id,
        owner_id=owner_id,
        bucket=SOURCE_BUCKET,
        object_key=f"private/owners/{owner_id}/jobs/{job_id}/source/source.png",
        version_id=SOURCE_VERSION_ID,
        content_sha256=SOURCE_CONTENT_SHA256,
        size_bytes=SOURCE_SIZE_BYTES,
        media_type=SOURCE_MEDIA_TYPE,
        product_profile_id=SOURCE_PROFILE_ID,
        product_profile_version=SOURCE_PROFILE_VERSION,
        product_profile_fingerprint=PROFILE_FP,
        created_at=at,
        width=width,
        height=height,
    )


def _source(
    job: ControlJobRecord,
    *,
    at: datetime,
    width: int | None = None,
    height: int | None = None,
) -> SourceArtifactRecord:
    return SourceArtifactRecord(
        job_id=job.job_id,
        owner_id=job.owner_id,
        fingerprint=_source_fingerprint(
            job_id=job.job_id,
            owner_id=job.owner_id,
            at=at,
            width=width,
            height=height,
        ),
        bucket=SOURCE_BUCKET,
        object_key=f"private/owners/{job.owner_id}/jobs/{job.job_id}/source/source.png",
        version_id=SOURCE_VERSION_ID,
        content_sha256=SOURCE_CONTENT_SHA256,
        size_bytes=SOURCE_SIZE_BYTES,
        media_type=SOURCE_MEDIA_TYPE,
        width=width,
        height=height,
        product_profile_id=SOURCE_PROFILE_ID,
        product_profile_version=SOURCE_PROFILE_VERSION,
        product_profile_fingerprint=PROFILE_FP,
        created_at=at,
    )


def _work(
    job: ControlJobRecord,
    *,
    work_id: str,
    receipt_id: str,
    work_type: WorkType,
    review_version: int | None,
    at: datetime,
) -> WorkRequest:
    return WorkRequest(
        work_request_id=work_id,
        owner_id=job.owner_id,
        job_id=job.job_id,
        receipt_id=receipt_id,
        work_type=work_type,
        review_version=review_version,
        input_fingerprint=work_input_fingerprint(
            work_type=work_type,
            job_id=job.job_id,
            work_request_id=work_id,
        ),
        execution_name=deterministic_execution_name(work_id),
        next_dispatch_at=at,
        created_at=at,
        updated_at=at,
    )


def _activate(
    store: InMemorySellerControlStore,
    job: ControlJobRecord,
    *,
    clock: MutableClock,
    dispatch: bool = True,
) -> WorkRequest:
    assert job.active_work_request_id is not None
    pending = store.get_work_request(job.job_id, job.active_work_request_id)
    if clock.value < pending.next_dispatch_at:
        clock.set(pending.next_dispatch_at)
    claim_id = f"claim_{pending.work_request_id}"
    claimed = store.claim_work(
        job.job_id,
        pending.work_request_id,
        now=clock.value,
        claim_id=claim_id,
        lease_expires_at=clock.value + timedelta(minutes=2),
    )
    assert claimed is not None
    if not dispatch:
        return claimed
    return store.mark_work_dispatched(
        job.job_id,
        pending.work_request_id,
        claim_id=claim_id,
        execution_arn=(
            "arn:aws:states:us-west-2:123456789012:execution:"
            f"mr-lister-phase6:{pending.work_request_id}"
        ),
        now=clock.value,
    )


def _seed_preparation(
    *,
    job_id: str = "job_phase62_worker",
    source_width: int | None = None,
    source_height: int | None = None,
) -> tuple[InMemorySellerControlStore, MutableClock, WorkerControlService, WorkRequest]:
    store = InMemorySellerControlStore()
    clock = MutableClock()
    source_fingerprint = _source_fingerprint(
        job_id=job_id,
        owner_id=OWNER,
        at=clock.value,
        width=source_width,
        height=source_height,
    )
    job = ControlJobRecord(
        owner_id=OWNER,
        job_id=job_id,
        event_sequence=1,
        state=ControlJobState.INTAKE_VALIDATED,
        source_artifact_fingerprint=source_fingerprint,
        active_work_request_id=PREPARE_WORK_ID,
        created_at=clock.value,
        updated_at=clock.value,
    )
    receipt = _receipt(job, identity=f"create_{job_id}", work_id=PREPARE_WORK_ID, at=clock.value)
    work = _work(
        job,
        work_id=PREPARE_WORK_ID,
        receipt_id=receipt.receipt_id,
        work_type=WorkType.PREPARE,
        review_version=None,
        at=clock.value,
    )
    store.create_job(
        job=job,
        event=_event(job, "INTAKE_VALIDATED", at=clock.value),
        receipt=receipt,
        work_request=work,
        source_artifact=_source(
            job,
            at=clock.value,
            width=source_width,
            height=source_height,
        ),
    )
    dispatched = _activate(store, job, clock=clock)
    return store, clock, WorkerControlService(store=store, clock=clock), dispatched


def _listing(*, title: str = "Geometric Badger Graphic Tee") -> ListingIntelligence:
    return ListingIntelligence(
        title=title,
        description="A geometric woodland badger illustration for an everyday graphic tee.",
        tags=VALID_TAGS,
        audience=("woodland art fans",),
        title_rationale="Names the visible subject and product.",
        tag_rationale="Uses thirteen distinct buyer-facing concepts.",
    )


def _analysis() -> ArtworkAnalysis:
    return ArtworkAnalysis(
        subject="A geometric badger",
        visual_elements=("compass", "pine trees"),
        styles=("low poly",),
        themes=("woodland adventure",),
        confidence=0.94,
    )


def _agent_decision(next_action: str = "human_review") -> PreparationDecision:
    return PreparationDecision(
        summary="The listing checkpoint is complete.",
        recommendation="Send the complete draft to human review.",
        next_action=next_action,
    )


def _prepare_to_product_sync(
    *,
    job_id: str = "job_phase62_worker",
    source_width: int | None = None,
    source_height: int | None = None,
) -> tuple[InMemorySellerControlStore, MutableClock, WorkerControlService, WorkRequest]:
    store, clock, worker, prepare_work = _seed_preparation(
        job_id=job_id,
        source_width=source_width,
        source_height=source_height,
    )
    started = worker.begin_preparation(
        BeginPreparationCommand(
            job_id=job_id,
            work_request_id=prepare_work.work_request_id,
            expected_record_version=0,
        )
    )
    reviewed = worker.record_prepared_review(
        RecordPreparedReviewCommand(
            job_id=job_id,
            work_request_id=prepare_work.work_request_id,
            expected_record_version=started.record_version,
            source_artifact_fingerprint=store.get_source_artifact(job_id).fingerprint,
            artwork_analysis=_analysis(),
            listing=_listing(),
            product_profile_fingerprint=PROFILE_FP,
        )
    )
    routed = worker.complete_preparation_with_agent_decision(
        CompletePreparationWithAgentDecisionCommand(
            job_id=job_id,
            work_request_id=prepare_work.work_request_id,
            expected_record_version=reviewed.record_version,
            correlation_id="2" * 24,
            controller_model_id="google.gemma-3-27b-it",
            tool_calls=("record_prepared_review",),
            cycles=2,
            input_tokens=800,
            output_tokens=200,
            total_tokens=1_000,
            decision=_agent_decision(),
        )
    )
    sync_work = store.get_work_request(job_id, routed.work_request_id or "")
    return store, clock, worker, sync_work


def _observation(
    *,
    product_id: str = PRODUCT_ID,
    image_id: str = IMAGE_ID,
    request_fingerprint: str = TARGET_FP,
    response_fingerprint: str = RESPONSE_FP,
) -> ProductSyncObservation:
    return ProductSyncObservation(
        product_id=product_id,
        printify_shop_id=12_345,
        image_id=image_id,
        request_fingerprint=request_fingerprint,
        response_fingerprint=response_fingerprint,
        mockups=(
            ProductMockupEvidence(
                url="https://images.printify.com/mockup/front.png",
                position="front",
                variant_ids=(101, 102),
            ),
        ),
        variants=(
            ProductVariantEvidence(
                variant_id=101,
                color="Black",
                size="S",
                placement_group_id="small",
                retail_price_cents=2_999,
                production_cost_cents=1_125,
            ),
            ProductVariantEvidence(
                variant_id=102,
                color="Black",
                size="M",
                placement_group_id="medium",
                retail_price_cents=2_999,
                production_cost_cents=1_275,
            ),
        ),
    )


def test_product_sync_observation_requires_strict_positive_shop_authority() -> None:
    payload = _observation().model_dump(mode="python")
    payload.pop("printify_shop_id")
    with pytest.raises(ValidationError, match="Field required"):
        ProductSyncObservation.model_validate(payload)
    with pytest.raises(ValidationError, match="valid integer"):
        ProductSyncObservation.model_validate({**payload, "printify_shop_id": "12345"})
    with pytest.raises(ValidationError, match="greater than 0"):
        ProductSyncObservation.model_validate({**payload, "printify_shop_id": 0})


def _begin_create(
    store: InMemorySellerControlStore,
    clock: MutableClock,
    worker: WorkerControlService,
) -> tuple[ControlJobRecord, WorkRequest, str]:
    syncing = store.get_job("job_phase62_worker")
    sync_work = _activate(store, syncing, clock=clock)
    syncing = _checkpoint_upload(store, worker, syncing, sync_work)
    worker.begin_provider_write(
        BeginProviderWriteCommand(
            job_id=syncing.job_id,
            work_request_id=sync_work.work_request_id,
            expected_record_version=syncing.record_version,
            image_id=IMAGE_ID,
            target_payload_fingerprint=TARGET_FP,
            correlation_token=_correlation_token(syncing.job_id),
        )
    )
    claimed = store.get_job(syncing.job_id)
    assert claimed.provider_write_attempt_id is not None
    assert (
        worker.authorize_provider_call(
            job_id=claimed.job_id,
            attempt_id=claimed.provider_write_attempt_id,
        )
        is not None
    )
    return claimed, sync_work, claimed.provider_write_attempt_id


def _checkpoint_upload(
    store: InMemorySellerControlStore,
    worker: WorkerControlService,
    job: ControlJobRecord,
    work: WorkRequest,
) -> ControlJobRecord:
    if job.uploaded_artwork_id is not None:
        return job
    source = store.get_source_artifact(job.job_id)
    file_name = worker.upload_file_name(job.job_id, source.content_sha256)
    worker.begin_provider_upload(
        BeginProviderUploadCommand(
            job_id=job.job_id,
            work_request_id=work.work_request_id,
            expected_record_version=job.record_version,
            source_artifact_fingerprint=source.fingerprint,
            file_name=file_name,
        )
    )
    claimed = store.get_job(job.job_id)
    assert claimed.provider_upload_attempt_id is not None
    assert (
        worker.authorize_provider_upload(
            job_id=job.job_id,
            attempt_id=claimed.provider_upload_attempt_id,
        )
        is not None
    )
    worker.record_provider_upload_success(
        RecordProviderUploadSuccessCommand(
            job_id=job.job_id,
            work_request_id=work.work_request_id,
            expected_record_version=claimed.record_version,
            attempt_id=claimed.provider_upload_attempt_id,
            observation=UploadedArtworkObservation(
                image_id=IMAGE_ID,
                file_name=file_name,
                width=3021,
                height=3927,
                size_bytes=source.size_bytes,
            ),
        )
    )
    return store.get_job(job.job_id)


def _correlation_token(job_id: str) -> str:
    digest = sha256(f"mr-lister:provider-draft:{job_id}".encode()).hexdigest()[:24]
    return f"ml-{digest}"


def _begin_upload_only(
    store: InMemorySellerControlStore,
    clock: MutableClock,
    worker: WorkerControlService,
) -> tuple[ControlJobRecord, WorkRequest, str, str]:
    syncing = store.get_job("job_phase62_worker")
    work = _activate(store, syncing, clock=clock)
    source = store.get_source_artifact(syncing.job_id)
    file_name = worker.upload_file_name(syncing.job_id, source.content_sha256)
    worker.begin_provider_upload(
        BeginProviderUploadCommand(
            job_id=syncing.job_id,
            work_request_id=work.work_request_id,
            expected_record_version=syncing.record_version,
            source_artifact_fingerprint=source.fingerprint,
            file_name=file_name,
        )
    )
    claimed = store.get_job(syncing.job_id)
    assert claimed.provider_upload_attempt_id is not None
    return claimed, work, claimed.provider_upload_attempt_id, file_name


def test_upload_claim_is_durable_unique_and_checkpointed_before_product_write() -> None:
    store, clock, worker, _sync_work = _prepare_to_product_sync()
    claimed, work, attempt_id, file_name = _begin_upload_only(store, clock, worker)
    attempt = store.get_provider_upload_attempt(claimed.job_id, attempt_id)

    assert claimed.upload_outcome_unconfirmed is True
    assert claimed.provider_outcome_unconfirmed is False
    assert claimed.job_id not in file_name
    assert attempt.file_name == file_name
    assert worker.authorize_provider_upload(job_id=claimed.job_id, attempt_id=attempt_id) == attempt
    permit = store.get_provider_call_permit(claimed.job_id, attempt_id)
    assert permit.status is ProviderCallPermitStatus.CONSUMED
    assert permit.consumed_work_request_id == work.work_request_id

    source = store.get_source_artifact(claimed.job_id)
    worker.record_provider_upload_success(
        RecordProviderUploadSuccessCommand(
            job_id=claimed.job_id,
            work_request_id=work.work_request_id,
            expected_record_version=claimed.record_version,
            attempt_id=attempt_id,
            observation=UploadedArtworkObservation(
                image_id=IMAGE_ID,
                file_name=file_name,
                width=3021,
                height=3927,
                size_bytes=source.size_bytes,
            ),
        )
    )
    checkpointed = store.get_job(claimed.job_id)
    upload = store.get_uploaded_artwork(checkpointed.job_id, checkpointed.uploaded_artwork_id or "")
    assert checkpointed.uploaded_image_id == IMAGE_ID
    assert checkpointed.upload_outcome_unconfirmed is False
    assert upload.image_id == IMAGE_ID

    with pytest.raises(IdempotencyConflictError, match="identity was reused"):
        worker.begin_provider_upload(
            BeginProviderUploadCommand(
                job_id=checkpointed.job_id,
                work_request_id=work.work_request_id,
                expected_record_version=checkpointed.record_version,
                source_artifact_fingerprint=source.fingerprint,
                file_name=file_name,
            )
        )


def test_provider_upload_dimensions_must_match_fingerprinted_source_geometry() -> None:
    store, clock, worker, _sync_work = _prepare_to_product_sync(
        source_width=2000,
        source_height=800,
    )
    claimed, work, attempt_id, file_name = _begin_upload_only(store, clock, worker)
    assert worker.authorize_provider_upload(job_id=claimed.job_id, attempt_id=attempt_id)
    source = store.get_source_artifact(claimed.job_id)

    with pytest.raises(InvalidControlStateError, match="pinned source"):
        worker.record_provider_upload_success(
            RecordProviderUploadSuccessCommand(
                job_id=claimed.job_id,
                work_request_id=work.work_request_id,
                expected_record_version=claimed.record_version,
                attempt_id=attempt_id,
                observation=UploadedArtworkObservation(
                    image_id=IMAGE_ID,
                    file_name=file_name,
                    width=1999,
                    height=800,
                    size_bytes=source.size_bytes,
                ),
            )
        )

    assert store.get_job(claimed.job_id).uploaded_artwork_id is None


def test_available_upload_permit_rebinds_only_to_exact_recovery_work() -> None:
    store, clock, worker, _sync_work = _prepare_to_product_sync()
    claimed, original_work, attempt_id, _file_name = _begin_upload_only(store, clock, worker)
    seller = SellerControlService(store=store, clock=clock)
    failed = seller.record_worker_failure(
        RecordWorkerFailureCommand(
            job_id=claimed.job_id,
            work_request_id=original_work.work_request_id,
            expected_record_version=claimed.record_version,
            code=WorkerFailureCode.PRODUCTION_UNAVAILABLE,
        )
    )
    retried = seller.retry_job(
        RetryJobCommand(
            job_id=claimed.job_id,
            owner_id=OWNER,
            expected_record_version=failed.record_version,
            idempotency_key="retry-unused-upload-permit",
        )
    )
    recovery_work = _activate(store, store.get_job(retried.job_id), clock=clock)

    attempt = worker.authorize_provider_upload(job_id=claimed.job_id, attempt_id=attempt_id)
    assert attempt is not None
    assert attempt.work_request_id == original_work.work_request_id
    permit = store.get_provider_call_permit(claimed.job_id, attempt_id)
    assert permit.work_request_id == original_work.work_request_id
    assert permit.consumed_work_request_id == recovery_work.work_request_id
    assert permit.status is ProviderCallPermitStatus.CONSUMED


def test_unknown_upload_uses_get_only_reconciliation_and_bounded_redrive() -> None:
    store, clock, worker, _sync_work = _prepare_to_product_sync()
    claimed, work, attempt_id, file_name = _begin_upload_only(store, clock, worker)
    assert worker.authorize_provider_upload(job_id=claimed.job_id, attempt_id=attempt_id)
    worker.record_provider_upload_outcome_unknown(
        RecordProviderUploadOutcomeUnknownCommand(
            job_id=claimed.job_id,
            work_request_id=work.work_request_id,
            expected_record_version=claimed.record_version,
            attempt_id=attempt_id,
            code="PROVIDER_CONNECTION_LOST",
        )
    )
    reconciling = store.get_job(claimed.job_id)
    recon_work = _activate(store, reconciling, clock=clock)
    redriven = worker.record_upload_reconciliation_observation(
        RecordUploadReconciliationObservationCommand(
            job_id=claimed.job_id,
            work_request_id=recon_work.work_request_id,
            expected_record_version=reconciling.record_version,
            attempt_id=attempt_id,
            outcome=ReconciliationOutcome.NO_MATCH,
        )
    )
    assert redriven.state is ControlJobState.RECONCILIATION_REQUIRED
    assert redriven.work_request_id != recon_work.work_request_id

    redrive_job = store.get_job(claimed.job_id)
    redrive_work = _activate(store, redrive_job, clock=clock)
    source = store.get_source_artifact(claimed.job_id)
    resumed = worker.record_upload_reconciliation_observation(
        RecordUploadReconciliationObservationCommand(
            job_id=claimed.job_id,
            work_request_id=redrive_work.work_request_id,
            expected_record_version=redrive_job.record_version,
            attempt_id=attempt_id,
            outcome=ReconciliationOutcome.TARGET_MATCH,
            upload=UploadedArtworkObservation(
                image_id=IMAGE_ID,
                file_name=file_name,
                width=3021,
                height=3927,
                size_bytes=source.size_bytes,
            ),
        )
    )
    assert resumed.state is ControlJobState.PRODUCT_DRAFT_SYNCING
    assert store.get_job(claimed.job_id).uploaded_image_id == IMAGE_ID


def test_unknown_upload_cannot_redrive_past_its_reconciliation_deadline() -> None:
    store, clock, worker, _sync_work = _prepare_to_product_sync()
    claimed, work, attempt_id, _file_name = _begin_upload_only(store, clock, worker)
    assert worker.authorize_provider_upload(job_id=claimed.job_id, attempt_id=attempt_id)
    worker.record_provider_upload_outcome_unknown(
        RecordProviderUploadOutcomeUnknownCommand(
            job_id=claimed.job_id,
            work_request_id=work.work_request_id,
            expected_record_version=claimed.record_version,
            attempt_id=attempt_id,
            code="PROVIDER_TIMEOUT",
        )
    )
    clock.advance(timedelta(minutes=16))
    reconciling = store.get_job(claimed.job_id)
    recon_work = _activate(store, reconciling, clock=clock)

    ended = worker.record_upload_reconciliation_observation(
        RecordUploadReconciliationObservationCommand(
            job_id=claimed.job_id,
            work_request_id=recon_work.work_request_id,
            expected_record_version=reconciling.record_version,
            attempt_id=attempt_id,
            outcome=ReconciliationOutcome.NO_MATCH,
        )
    )

    assert ended.state is ControlJobState.FAILED_TERMINAL
    terminal = store.get_job(claimed.job_id)
    assert terminal.active_work_request_id is None
    assert terminal.upload_outcome_unconfirmed is True
    assert terminal.failure_id is not None


def test_three_stage_prepare_checkpoints_are_atomic_and_exactly_replayable() -> None:
    store, _clock, worker, work = _seed_preparation()
    begin = BeginPreparationCommand(
        job_id="job_phase62_worker",
        work_request_id=work.work_request_id,
        expected_record_version=0,
    )

    started = worker.begin_preparation(begin)
    assert worker.begin_preparation(begin) == started
    assert started.state is ControlJobState.ANALYZING_ARTWORK
    source = store.get_source_artifact(started.job_id)

    prepared_command = RecordPreparedReviewCommand(
        job_id=started.job_id,
        work_request_id=work.work_request_id,
        expected_record_version=started.record_version,
        source_artifact_fingerprint=source.fingerprint,
        artwork_analysis=_analysis(),
        listing=_listing(),
        product_profile_fingerprint=PROFILE_FP,
    )
    prepared = worker.record_prepared_review(prepared_command)
    assert worker.record_prepared_review(prepared_command) == prepared
    assert prepared.state is ControlJobState.LISTING_DRAFTED
    assert prepared.review_version == 1

    completion_command = CompletePreparationWithAgentDecisionCommand(
        job_id=prepared.job_id,
        work_request_id=work.work_request_id,
        expected_record_version=prepared.record_version,
        correlation_id="2" * 24,
        controller_model_id="google.gemma-3-27b-it",
        tool_calls=("record_prepared_review",),
        cycles=2,
        input_tokens=800,
        output_tokens=200,
        total_tokens=1_000,
        decision=_agent_decision(),
    )
    completed = worker.complete_preparation_with_agent_decision(completion_command)
    assert worker.complete_preparation_with_agent_decision(completion_command) == completed

    job = store.get_job(completed.job_id)
    old_work = store.get_work_request(job.job_id, work.work_request_id)
    next_work = store.get_work_request(job.job_id, completed.work_request_id or "")
    evidence = store.get_agent_evidence(job.job_id, job.agent_evidence_id or "")
    assert job.state is ControlJobState.PRODUCT_DRAFT_SYNCING
    assert job.record_version == 3
    assert old_work.status is WorkRequestStatus.COMPLETED
    assert next_work.status is WorkRequestStatus.PENDING
    assert next_work.work_type is WorkType.SYNCHRONIZE_PRODUCT
    assert next_work.review_version == 1
    assert evidence.framework == "strands-agents"
    assert evidence.tool_calls == ("record_prepared_review",)
    assert evidence.fingerprint == evidence.authority_fingerprint
    assert [event.name for event in store.list_events(job.job_id)] == [
        "INTAKE_VALIDATED",
        "ARTWORK_ANALYSIS_STARTED",
        "PREPARED_REVIEW_RECORDED",
        "STRANDS_PREPARATION_COMPLETED",
    ]


def test_agent_failure_after_review_resumes_decision_without_second_intelligence() -> None:
    store, clock, worker, work = _seed_preparation(job_id="job_prepare_checkpoint_retry")
    started = worker.begin_preparation(
        BeginPreparationCommand(
            job_id="job_prepare_checkpoint_retry",
            work_request_id=work.work_request_id,
            expected_record_version=0,
        )
    )
    source = store.get_source_artifact(started.job_id)
    prepared = worker.record_prepared_review(
        RecordPreparedReviewCommand(
            job_id=started.job_id,
            work_request_id=work.work_request_id,
            expected_record_version=started.record_version,
            source_artifact_fingerprint=source.fingerprint,
            artwork_analysis=_analysis(),
            listing=_listing(),
            product_profile_fingerprint=PROFILE_FP,
        )
    )
    checkpoint = store.get_job(prepared.job_id)
    seller = SellerControlService(store=store, clock=clock)

    failed = seller.record_worker_failure(
        RecordWorkerFailureCommand(
            job_id=checkpoint.job_id,
            work_request_id=work.work_request_id,
            expected_record_version=checkpoint.record_version,
            code=WorkerFailureCode.INTELLIGENCE_UNAVAILABLE,
        )
    )
    failure = store.get_failure(failed.job_id, store.get_job(failed.job_id).failure_id or "")
    retried = seller.retry_job(
        RetryJobCommand(
            job_id=failed.job_id,
            owner_id=OWNER,
            expected_record_version=failed.record_version,
            idempotency_key="retry-agent-decision-checkpoint",
        )
    )

    assert failure.recovery_action is RecoveryAction.RETRY_AGENT_DECISION
    assert failure.resume_state is ControlJobState.LISTING_DRAFTED
    assert retried.state is ControlJobState.LISTING_DRAFTED
    assert retried.review_version == checkpoint.review_version
    resumed_job = store.get_job(retried.job_id)
    assert resumed_job.artwork_analysis_id == checkpoint.artwork_analysis_id
    assert resumed_job.artwork_analysis_fingerprint == checkpoint.artwork_analysis_fingerprint
    assert len(store.list_reviews(retried.job_id)) == 1

    resumed_work = _activate(store, resumed_job, clock=clock)
    completed = worker.complete_preparation_with_agent_decision(
        CompletePreparationWithAgentDecisionCommand(
            job_id=resumed_job.job_id,
            work_request_id=resumed_work.work_request_id,
            expected_record_version=resumed_job.record_version,
            correlation_id="3" * 24,
            controller_model_id="google.gemma-3-27b-it",
            tool_calls=("record_prepared_review",),
            cycles=1,
            input_tokens=500,
            output_tokens=100,
            total_tokens=600,
            decision=_agent_decision(),
        )
    )

    assert completed.state is ControlJobState.PRODUCT_DRAFT_SYNCING
    assert len(store.list_reviews(retried.job_id)) == 1


def test_prepared_review_rejects_forged_pinned_source_without_mutation() -> None:
    store, _clock, worker, work = _seed_preparation()
    started = worker.begin_preparation(
        BeginPreparationCommand(
            job_id="job_phase62_worker",
            work_request_id=work.work_request_id,
            expected_record_version=0,
        )
    )
    events_before = store.list_events(started.job_id)

    with pytest.raises(InvalidControlStateError, match="pinned intake"):
        worker.record_prepared_review(
            RecordPreparedReviewCommand(
                job_id=started.job_id,
                work_request_id=work.work_request_id,
                expected_record_version=started.record_version,
                source_artifact_fingerprint="9" * 64,
                artwork_analysis=_analysis(),
                listing=_listing(),
                product_profile_fingerprint=PROFILE_FP,
            )
        )

    assert store.get_job(started.job_id).state is ControlJobState.ANALYZING_ARTWORK
    assert store.list_events(started.job_id) == events_before


def test_agent_completion_rejects_forged_non_phase6_tool_evidence() -> None:
    store, _clock, worker, work = _seed_preparation()
    started = worker.begin_preparation(
        BeginPreparationCommand(
            job_id="job_phase62_worker",
            work_request_id=work.work_request_id,
            expected_record_version=0,
        )
    )
    source = store.get_source_artifact(started.job_id)
    prepared = worker.record_prepared_review(
        RecordPreparedReviewCommand(
            job_id=started.job_id,
            work_request_id=work.work_request_id,
            expected_record_version=started.record_version,
            source_artifact_fingerprint=source.fingerprint,
            artwork_analysis=_analysis(),
            listing=_listing(),
            product_profile_fingerprint=PROFILE_FP,
        )
    )

    with pytest.raises(
        (InvalidControlStateError, ValueError),
        match="record_prepared_review|exact Strands tool",
    ):
        worker.complete_preparation_with_agent_decision(
            CompletePreparationWithAgentDecisionCommand(
                job_id=prepared.job_id,
                work_request_id=work.work_request_id,
                expected_record_version=prepared.record_version,
                correlation_id="2" * 24,
                controller_model_id="google.gemma-3-27b-it",
                tool_calls=("begin_preparation",),
                cycles=1,
                input_tokens=10,
                output_tokens=10,
                total_tokens=20,
                decision=_agent_decision(),
            )
        )

    assert store.get_job(prepared.job_id).state is ControlJobState.LISTING_DRAFTED


def test_agent_completion_cannot_override_application_validation_route() -> None:
    store, _clock, worker, work = _seed_preparation()
    started = worker.begin_preparation(
        BeginPreparationCommand(
            job_id="job_phase62_worker",
            work_request_id=work.work_request_id,
            expected_record_version=0,
        )
    )
    source = store.get_source_artifact(started.job_id)
    prepared = worker.record_prepared_review(
        RecordPreparedReviewCommand(
            job_id=started.job_id,
            work_request_id=work.work_request_id,
            expected_record_version=started.record_version,
            source_artifact_fingerprint=source.fingerprint,
            artwork_analysis=_analysis(),
            listing=_listing(),
            product_profile_fingerprint=PROFILE_FP,
        )
    )

    with pytest.raises(InvalidControlStateError, match="application validation"):
        worker.complete_preparation_with_agent_decision(
            CompletePreparationWithAgentDecisionCommand(
                job_id=prepared.job_id,
                work_request_id=work.work_request_id,
                expected_record_version=prepared.record_version,
                correlation_id="2" * 24,
                controller_model_id="google.gemma-3-27b-it",
                tool_calls=("record_prepared_review",),
                cycles=1,
                input_tokens=10,
                output_tokens=10,
                total_tokens=20,
                decision=_agent_decision("revise"),
            )
        )

    unchanged = store.get_job(prepared.job_id)
    assert unchanged.state is ControlJobState.LISTING_DRAFTED
    assert unchanged.agent_evidence_id is None


def test_initial_create_claim_is_immutable_and_cannot_be_replaced() -> None:
    store, clock, worker, _sync_work = _prepare_to_product_sync()
    syncing = store.get_job("job_phase62_worker")
    active = _activate(store, syncing, clock=clock)
    syncing = _checkpoint_upload(store, worker, syncing, active)
    command = BeginProviderWriteCommand(
        job_id=syncing.job_id,
        work_request_id=active.work_request_id,
        expected_record_version=syncing.record_version,
        image_id=IMAGE_ID,
        target_payload_fingerprint=TARGET_FP,
        correlation_token=_correlation_token(syncing.job_id),
    )

    first = worker.begin_provider_write(command)
    replay = worker.begin_provider_write(command)
    claimed = store.get_job(syncing.job_id)
    attempt = store.get_provider_write_attempt(
        claimed.job_id, claimed.product_create_attempt_id or ""
    )

    assert replay == first
    assert attempt.operation is ProviderWriteOperation.CREATE
    assert claimed.provider_write_attempt_id == attempt.attempt_id
    assert claimed.product_create_attempt_id == attempt.attempt_id
    assert len(store.list_events(claimed.job_id)) == 7

    authorized = worker.authorize_provider_call(
        job_id=claimed.job_id,
        attempt_id=attempt.attempt_id,
    )
    assert authorized == attempt
    assert (
        store.get_provider_call_permit(claimed.job_id, attempt.attempt_id).status
        is ProviderCallPermitStatus.CONSUMED
    )
    assert (
        worker.authorize_provider_call(
            job_id=claimed.job_id,
            attempt_id=attempt.attempt_id,
        )
        is None
    )

    forged_second_claim = BeginProviderWriteCommand(
        job_id=syncing.job_id,
        work_request_id=active.work_request_id,
        expected_record_version=claimed.record_version,
        image_id=IMAGE_ID,
        target_payload_fingerprint=UPDATED_TARGET_FP,
        correlation_token=_correlation_token(syncing.job_id),
    )
    with pytest.raises(IdempotencyConflictError, match="identity was reused"):
        worker.begin_provider_write(forged_second_claim)

    assert store.get_job(claimed.job_id).product_create_attempt_id == attempt.attempt_id


@pytest.mark.parametrize("dispatch", (False, True))
def test_provider_call_permit_consumes_for_exact_claimed_or_dispatched_work(
    dispatch: bool,
) -> None:
    store, clock, worker, _sync_work = _prepare_to_product_sync()
    syncing = store.get_job("job_phase62_worker")
    active = _activate(store, syncing, clock=clock, dispatch=dispatch)
    syncing = _checkpoint_upload(store, worker, syncing, active)
    worker.begin_provider_write(
        BeginProviderWriteCommand(
            job_id=syncing.job_id,
            work_request_id=active.work_request_id,
            expected_record_version=syncing.record_version,
            image_id=IMAGE_ID,
            target_payload_fingerprint=TARGET_FP,
            correlation_token=_correlation_token(syncing.job_id),
        )
    )
    claimed = store.get_job(syncing.job_id)
    attempt_id = claimed.provider_write_attempt_id or ""

    assert (
        worker.authorize_provider_call(
            job_id=claimed.job_id,
            attempt_id=attempt_id,
        )
        is not None
    )
    assert (
        store.get_provider_call_permit(claimed.job_id, attempt_id).status
        is ProviderCallPermitStatus.CONSUMED
    )
    assert (
        worker.authorize_provider_call(
            job_id=claimed.job_id,
            attempt_id=attempt_id,
        )
        is None
    )


def test_pending_or_cancelled_work_cannot_consume_provider_call_permit() -> None:
    store, clock, worker, _sync_work = _prepare_to_product_sync()
    syncing = store.get_job("job_phase62_worker")
    claimed_work = _activate(store, syncing, clock=clock, dispatch=False)
    syncing = _checkpoint_upload(store, worker, syncing, claimed_work)
    worker.begin_provider_write(
        BeginProviderWriteCommand(
            job_id=syncing.job_id,
            work_request_id=claimed_work.work_request_id,
            expected_record_version=syncing.record_version,
            image_id=IMAGE_ID,
            target_payload_fingerprint=TARGET_FP,
            correlation_token=_correlation_token(syncing.job_id),
        )
    )
    claimed_job = store.get_job(syncing.job_id)
    attempt_id = claimed_job.provider_write_attempt_id or ""
    assert claimed_work.claim_id is not None
    pending = store.release_work(
        claimed_job.job_id,
        claimed_work.work_request_id,
        claim_id=claimed_work.claim_id,
        next_dispatch_at=clock.value + timedelta(minutes=5),
        error_code="PROVIDER_NOT_STARTED",
        now=clock.value,
    )

    assert pending.status is WorkRequestStatus.PENDING
    assert (
        worker.authorize_provider_call(
            job_id=claimed_job.job_id,
            attempt_id=attempt_id,
        )
        is None
    )
    assert (
        store.get_provider_call_permit(claimed_job.job_id, attempt_id).status
        is ProviderCallPermitStatus.AVAILABLE
    )

    cancelled = SellerControlService(store=store, clock=clock).cancel_job(
        CancelJobCommand(
            job_id=claimed_job.job_id,
            owner_id=OWNER,
            expected_record_version=claimed_job.record_version,
            idempotency_key="cancel-before-provider-permit",
        )
    )
    cancelled_work = store.get_work_request(claimed_job.job_id, pending.work_request_id)
    assert cancelled.state is ControlJobState.CANCELLED
    assert cancelled_work.status is WorkRequestStatus.CANCELLED
    assert (
        worker.authorize_provider_call(
            job_id=claimed_job.job_id,
            attempt_id=attempt_id,
        )
        is None
    )
    retired = store.get_provider_call_permit(claimed_job.job_id, attempt_id)
    persisted = store.get_job(claimed_job.job_id)
    assert retired.status is ProviderCallPermitStatus.RETIRED
    assert retired.retired_at == clock.value
    assert persisted.provider_outcome_unconfirmed is False
    assert persisted.upload_outcome_unconfirmed is False


def test_retry_work_safely_consumes_the_original_unused_provider_call_permit() -> None:
    store, clock, worker, _sync_work = _prepare_to_product_sync()
    syncing = store.get_job("job_phase62_worker")
    active = _activate(store, syncing, clock=clock)
    syncing = _checkpoint_upload(store, worker, syncing, active)
    worker.begin_provider_write(
        BeginProviderWriteCommand(
            job_id=syncing.job_id,
            work_request_id=active.work_request_id,
            expected_record_version=syncing.record_version,
            image_id=IMAGE_ID,
            target_payload_fingerprint=TARGET_FP,
            correlation_token=_correlation_token(syncing.job_id),
        )
    )
    claimed_job = store.get_job(syncing.job_id)
    attempt_id = claimed_job.provider_write_attempt_id or ""
    seller = SellerControlService(store=store, clock=clock)
    failed = seller.record_worker_failure(
        RecordWorkerFailureCommand(
            job_id=claimed_job.job_id,
            work_request_id=active.work_request_id,
            expected_record_version=claimed_job.record_version,
            code=WorkerFailureCode.PRODUCTION_UNAVAILABLE,
        )
    )
    retry = seller.retry_job(
        RetryJobCommand(
            job_id=claimed_job.job_id,
            owner_id=OWNER,
            expected_record_version=failed.record_version,
            idempotency_key="retry-superseded-provider-work",
        )
    )

    assert retry.state is ControlJobState.PRODUCT_DRAFT_SYNCING
    assert retry.work_request_id is not None
    assert retry.work_request_id != active.work_request_id
    retrying = store.get_job(claimed_job.job_id)
    active_retry = _activate(store, retrying, clock=clock)
    assert (
        worker.authorize_provider_call(
            job_id=claimed_job.job_id,
            attempt_id=attempt_id,
        )
        is not None
    )
    permit = store.get_provider_call_permit(claimed_job.job_id, attempt_id)
    assert permit.status is ProviderCallPermitStatus.CONSUMED
    assert permit.work_request_id == active.work_request_id
    assert permit.consumed_work_request_id == active_retry.work_request_id

    completed = worker.record_product_sync_success(
        RecordProductSyncSuccessCommand(
            job_id=retrying.job_id,
            work_request_id=active_retry.work_request_id,
            expected_record_version=retrying.record_version,
            attempt_id=attempt_id,
            observation=_observation(),
        )
    )
    assert completed.state is ControlJobState.PRICING_REFRESHING
    assert store.get_job(retrying.job_id).product_id == PRODUCT_ID


def test_product_sync_success_persists_evidence_and_creates_pricing_work() -> None:
    store, clock, worker, _sync_work = _prepare_to_product_sync()
    claimed, active, attempt_id = _begin_create(store, clock, worker)
    command = RecordProductSyncSuccessCommand(
        job_id=claimed.job_id,
        work_request_id=active.work_request_id,
        expected_record_version=claimed.record_version,
        attempt_id=attempt_id,
        observation=_observation(),
    )

    completed = worker.record_product_sync_success(command)
    assert worker.record_product_sync_success(command) == completed
    job = store.get_job(claimed.job_id)
    sync = store.get_product_sync(job.job_id, job.product_sync_id or "")
    pricing_work = store.get_work_request(job.job_id, job.active_work_request_id or "")

    assert job.state is ControlJobState.PRICING_REFRESHING
    assert job.product_id == PRODUCT_ID
    assert job.provider_payload_fingerprint == TARGET_FP
    assert job.provider_outcome_unconfirmed is False
    assert sync.image_id == IMAGE_ID
    assert sync.printify_shop_id == 12_345
    assert sync.response_fingerprint == RESPONSE_FP
    assert sync.fingerprint == product_sync_record_fingerprint(sync)
    assert tuple(item.variant_id for item in sync.variants) == (101, 102)
    assert pricing_work.work_type is WorkType.REFRESH_ECONOMICS
    assert pricing_work.review_version == job.review_version
    assert (
        store.get_work_request(job.job_id, active.work_request_id).status
        is WorkRequestStatus.COMPLETED
    )


def test_product_sync_commit_rejects_new_shopless_legacy_shape() -> None:
    store, clock, worker, _sync_work = _prepare_to_product_sync()
    claimed, active, attempt_id = _begin_create(store, clock, worker)
    captured: list[CommandCommit] = []
    original_commit = store.commit_command

    def capture(commit: CommandCommit):  # type: ignore[no-untyped-def]
        captured.append(commit)
        return original_commit(commit)

    store.commit_command = capture  # type: ignore[method-assign]
    worker.record_product_sync_success(
        RecordProductSyncSuccessCommand(
            job_id=claimed.job_id,
            work_request_id=active.work_request_id,
            expected_record_version=claimed.record_version,
            attempt_id=attempt_id,
            observation=_observation(),
        )
    )
    valid = captured[-1]
    assert valid.product_sync is not None
    shopless = valid.product_sync.model_copy(update={"printify_shop_id": None})
    shopless = shopless.model_copy(
        update={"fingerprint": product_sync_record_fingerprint(shopless)}
    )
    forged = CommandCommit(
        **{
            **valid.__dict__,
            "updated": valid.updated.model_copy(
                update={"product_sync_fingerprint": shopless.fingerprint}
            ),
            "product_sync": shopless,
        }
    )
    assert forged.updated.product_sync_fingerprint == shopless.fingerprint
    assert shopless.fingerprint == product_sync_record_fingerprint(shopless)

    with pytest.raises(InvalidControlStateError, match="product synchronization"):
        validate_command_commit(forged)


def test_pricing_success_atomically_persists_full_evidence_and_settles_exact_work() -> None:
    store, clock, worker, _sync_work = _prepare_to_product_sync()
    claimed, active, attempt_id = _begin_create(store, clock, worker)
    worker.record_product_sync_success(
        RecordProductSyncSuccessCommand(
            job_id=claimed.job_id,
            work_request_id=active.work_request_id,
            expected_record_version=claimed.record_version,
            attempt_id=attempt_id,
            observation=_observation(),
        )
    )
    pricing_job = store.get_job(claimed.job_id)
    pricing_work = _activate(store, pricing_job, clock=clock)
    estimate = _estimate_for_current_sync(
        store,
        pricing_job,
        calculated_at=clock.value,
        production_overrides={101: 1_200, 102: 1_350},
    )
    command = RecordPricingSuccessCommand(
        job_id=pricing_job.job_id,
        work_request_id=pricing_work.work_request_id,
        expected_record_version=pricing_job.record_version,
        estimate=estimate,
    )

    completed = worker.record_pricing_success(command)
    assert worker.record_pricing_success(command) == completed

    job = store.get_job(pricing_job.job_id)
    snapshot = store.get_pricing(job.job_id, job.pricing_snapshot_id or "")
    evidence = store.get_pricing_evidence(job.job_id, snapshot.snapshot_id)
    assert completed.state is ControlJobState.AWAITING_APPROVAL
    assert job.active_work_request_id is None
    assert snapshot.fingerprint == evidence.fingerprint == estimate.fingerprint
    assert evidence.estimate == estimate
    assert tuple(item.production_cost_cents for item in evidence.estimate.variants) == (
        1_200,
        1_350,
    )
    assert (
        store.get_work_request(job.job_id, pricing_work.work_request_id).status
        is WorkRequestStatus.COMPLETED
    )


@pytest.mark.parametrize(
    ("estimate_kwargs", "match"),
    (
        ({"variant_ids": (101,)}, "exact synchronized variants"),
        ({"retail_overrides": {101: 3_099}}, "exact synchronized variants"),
        ({"product_sync_fingerprint": "9" * 64}, "product sync authority"),
    ),
)
def test_pricing_success_rejects_forged_sync_variant_or_retail_evidence(
    estimate_kwargs: dict[str, object],
    match: str,
) -> None:
    store, clock, worker, _sync_work = _prepare_to_product_sync()
    claimed, active, attempt_id = _begin_create(store, clock, worker)
    worker.record_product_sync_success(
        RecordProductSyncSuccessCommand(
            job_id=claimed.job_id,
            work_request_id=active.work_request_id,
            expected_record_version=claimed.record_version,
            attempt_id=attempt_id,
            observation=_observation(),
        )
    )
    pricing_job = store.get_job(claimed.job_id)
    pricing_work = _activate(store, pricing_job, clock=clock)
    estimate = _estimate_for_current_sync(
        store,
        pricing_job,
        calculated_at=clock.value,
        **estimate_kwargs,  # type: ignore[arg-type]
    )

    with pytest.raises(InvalidControlStateError, match=match):
        worker.record_pricing_success(
            RecordPricingSuccessCommand(
                job_id=pricing_job.job_id,
                work_request_id=pricing_work.work_request_id,
                expected_record_version=pricing_job.record_version,
                estimate=estimate,
            )
        )

    unchanged = store.get_job(pricing_job.job_id)
    assert unchanged == pricing_job
    assert unchanged.pricing_snapshot_id is None


def test_pricing_success_honors_cancellation_but_keeps_completed_evidence() -> None:
    store, clock, worker, _sync_work = _prepare_to_product_sync()
    claimed, active, attempt_id = _begin_create(store, clock, worker)
    worker.record_product_sync_success(
        RecordProductSyncSuccessCommand(
            job_id=claimed.job_id,
            work_request_id=active.work_request_id,
            expected_record_version=claimed.record_version,
            attempt_id=attempt_id,
            observation=_observation(),
        )
    )
    pricing_job = store.get_job(claimed.job_id)
    pricing_work = _activate(store, pricing_job, clock=clock)
    estimate = _estimate_for_current_sync(store, pricing_job, calculated_at=clock.value)
    cancelled = SellerControlService(store=store, clock=clock).cancel_job(
        CancelJobCommand(
            job_id=pricing_job.job_id,
            owner_id=OWNER,
            expected_record_version=pricing_job.record_version,
            idempotency_key="cancel-active-economics",
        )
    )
    assert cancelled.state is ControlJobState.CANCEL_REQUESTED

    completed = worker.record_pricing_success(
        RecordPricingSuccessCommand(
            job_id=pricing_job.job_id,
            work_request_id=pricing_work.work_request_id,
            expected_record_version=cancelled.record_version,
            estimate=estimate,
        )
    )

    job = store.get_job(pricing_job.job_id)
    evidence = store.get_pricing_evidence(job.job_id, job.pricing_snapshot_id or "")
    assert completed.state is ControlJobState.CANCELLED
    assert evidence.estimate == estimate
    assert job.active_work_request_id is None


def test_pricing_success_rejects_evidence_that_expired_before_settlement() -> None:
    store, clock, worker, _sync_work = _prepare_to_product_sync()
    claimed, active, attempt_id = _begin_create(store, clock, worker)
    worker.record_product_sync_success(
        RecordProductSyncSuccessCommand(
            job_id=claimed.job_id,
            work_request_id=active.work_request_id,
            expected_record_version=claimed.record_version,
            attempt_id=attempt_id,
            observation=_observation(),
        )
    )
    pricing_job = store.get_job(claimed.job_id)
    pricing_work = _activate(store, pricing_job, clock=clock)
    estimate = _estimate_for_current_sync(store, pricing_job, calculated_at=clock.value)
    clock.set(estimate.fresh_until)

    with pytest.raises(InvalidControlStateError, match="not fresh"):
        worker.record_pricing_success(
            RecordPricingSuccessCommand(
                job_id=pricing_job.job_id,
                work_request_id=pricing_work.work_request_id,
                expected_record_version=pricing_job.record_version,
                estimate=estimate,
            )
        )

    assert store.get_job(pricing_job.job_id) == pricing_job


def test_provider_outcome_is_rejected_until_one_shot_permit_is_consumed() -> None:
    store, clock, worker, _sync_work = _prepare_to_product_sync()
    syncing = store.get_job("job_phase62_worker")
    active = _activate(store, syncing, clock=clock)
    syncing = _checkpoint_upload(store, worker, syncing, active)
    worker.begin_provider_write(
        BeginProviderWriteCommand(
            job_id=syncing.job_id,
            work_request_id=active.work_request_id,
            expected_record_version=syncing.record_version,
            image_id=IMAGE_ID,
            target_payload_fingerprint=TARGET_FP,
            correlation_token=_correlation_token(syncing.job_id),
        )
    )
    claimed = store.get_job(syncing.job_id)
    attempt_id = claimed.provider_write_attempt_id or ""

    with pytest.raises(InvalidControlStateError, match="consumed one-shot call permit"):
        worker.record_product_sync_success(
            RecordProductSyncSuccessCommand(
                job_id=claimed.job_id,
                work_request_id=active.work_request_id,
                expected_record_version=claimed.record_version,
                attempt_id=attempt_id,
                observation=_observation(),
            )
        )
    with pytest.raises(InvalidControlStateError, match="consumed one-shot call permit"):
        worker.record_product_write_outcome_unknown(
            RecordProductWriteOutcomeUnknownCommand(
                job_id=claimed.job_id,
                work_request_id=active.work_request_id,
                expected_record_version=claimed.record_version,
                attempt_id=attempt_id,
                code="PROVIDER_TIMEOUT",
            )
        )

    assert store.get_job(claimed.job_id) == claimed
    assert (
        store.get_provider_call_permit(claimed.job_id, attempt_id).status
        is ProviderCallPermitStatus.AVAILABLE
    )


@pytest.mark.parametrize(
    ("observation", "match"),
    (
        (_observation(image_id="different_image"), "uploaded image"),
        (_observation(request_fingerprint="7" * 64), "target payload"),
    ),
)
def test_product_sync_success_rejects_forged_provider_evidence(
    observation: ProductSyncObservation,
    match: str,
) -> None:
    store, clock, worker, _sync_work = _prepare_to_product_sync()
    claimed, active, attempt_id = _begin_create(store, clock, worker)

    with pytest.raises(InvalidControlStateError, match=match):
        worker.record_product_sync_success(
            RecordProductSyncSuccessCommand(
                job_id=claimed.job_id,
                work_request_id=active.work_request_id,
                expected_record_version=claimed.record_version,
                attempt_id=attempt_id,
                observation=observation,
            )
        )

    unchanged = store.get_job(claimed.job_id)
    assert unchanged == claimed
    assert unchanged.product_sync_id is None


def _unknown_create_reconciliation(
    *,
    job_id: str = "job_phase62_worker",
) -> tuple[
    InMemorySellerControlStore,
    MutableClock,
    WorkerControlService,
    ControlJobRecord,
    WorkRequest,
    str,
]:
    store, clock, worker, _sync_work = _prepare_to_product_sync(job_id=job_id)
    syncing = store.get_job(job_id)
    active = _activate(store, syncing, clock=clock)
    syncing = _checkpoint_upload(store, worker, syncing, active)
    worker.begin_provider_write(
        BeginProviderWriteCommand(
            job_id=job_id,
            work_request_id=active.work_request_id,
            expected_record_version=syncing.record_version,
            image_id=IMAGE_ID,
            target_payload_fingerprint=TARGET_FP,
            correlation_token=_correlation_token(job_id),
        )
    )
    claimed = store.get_job(job_id)
    attempt_id = claimed.provider_write_attempt_id or ""
    assert worker.authorize_provider_call(job_id=job_id, attempt_id=attempt_id) is not None
    worker.record_product_write_outcome_unknown(
        RecordProductWriteOutcomeUnknownCommand(
            job_id=job_id,
            work_request_id=active.work_request_id,
            expected_record_version=claimed.record_version,
            attempt_id=attempt_id,
            code="PROVIDER_TIMEOUT",
        )
    )
    reconciling = store.get_job(job_id)
    reconciliation_work = _activate(store, reconciling, clock=clock)
    return store, clock, worker, reconciling, reconciliation_work, attempt_id


def test_unknown_create_outcome_redrives_zero_match_then_fails_at_deadline() -> None:
    store, clock, worker, reconciling, work, attempt_id = _unknown_create_reconciliation()
    first = worker.record_reconciliation_observation(
        RecordReconciliationObservationCommand(
            job_id=reconciling.job_id,
            work_request_id=work.work_request_id,
            expected_record_version=reconciling.record_version,
            attempt_id=attempt_id,
            outcome=ReconciliationOutcome.NO_MATCH,
        )
    )
    deferred = store.get_job(reconciling.job_id)
    assert first.state is ControlJobState.RECONCILIATION_REQUIRED
    assert deferred.active_work_request_id != work.work_request_id
    assert deferred.provider_outcome_unconfirmed is True

    next_work = _activate(store, deferred, clock=clock)
    deadline = store.get_provider_write_attempt(deferred.job_id, attempt_id).reconciliation_deadline
    clock.set(deadline)
    terminal = worker.record_reconciliation_observation(
        RecordReconciliationObservationCommand(
            job_id=deferred.job_id,
            work_request_id=next_work.work_request_id,
            expected_record_version=deferred.record_version,
            attempt_id=attempt_id,
            outcome=ReconciliationOutcome.NO_MATCH,
        )
    )

    job = store.get_job(deferred.job_id)
    failure = store.get_failure(job.job_id, job.failure_id or "")
    assert terminal.state is ControlJobState.FAILED_TERMINAL
    assert job.active_work_request_id is None
    assert job.provider_outcome_unconfirmed is True
    assert failure.code == "PRODUCT_CREATE_OUTCOME_UNKNOWN"
    assert (
        store.get_work_request(job.job_id, next_work.work_request_id).status
        is WorkRequestStatus.COMPLETED
    )


def test_target_match_reconciliation_succeeds_without_another_create_claim() -> None:
    store, _clock, worker, reconciling, work, attempt_id = _unknown_create_reconciliation()
    original_create_claim = reconciling.product_create_attempt_id

    completed = worker.record_reconciliation_observation(
        RecordReconciliationObservationCommand(
            job_id=reconciling.job_id,
            work_request_id=work.work_request_id,
            expected_record_version=reconciling.record_version,
            attempt_id=attempt_id,
            outcome=ReconciliationOutcome.TARGET_MATCH,
            product=_observation(),
        )
    )
    job = store.get_job(reconciling.job_id)

    assert completed.state is ControlJobState.PRICING_REFRESHING
    assert job.product_id == PRODUCT_ID
    assert job.product_create_attempt_id == original_create_claim == attempt_id
    assert job.provider_outcome_unconfirmed is False


def test_conflicting_reconciliation_is_terminal_and_never_creates_product_authority() -> None:
    store, _clock, worker, reconciling, work, attempt_id = _unknown_create_reconciliation()

    terminal = worker.record_reconciliation_observation(
        RecordReconciliationObservationCommand(
            job_id=reconciling.job_id,
            work_request_id=work.work_request_id,
            expected_record_version=reconciling.record_version,
            attempt_id=attempt_id,
            outcome=ReconciliationOutcome.MULTIPLE_MATCHES,
        )
    )
    job = store.get_job(reconciling.job_id)

    assert terminal.state is ControlJobState.FAILED_TERMINAL
    assert job.product_id is None
    assert job.product_sync_id is None
    assert (
        store.get_failure(job.job_id, job.failure_id or "").code
        == "PRODUCT_RECONCILIATION_CONFLICT"
    )


def _estimate_for_current_sync(
    store: InMemorySellerControlStore,
    job: ControlJobRecord,
    *,
    calculated_at: datetime,
    variant_ids: tuple[int, ...] | None = None,
    product_sync_fingerprint: str | None = None,
    retail_overrides: dict[int, int] | None = None,
    production_overrides: dict[int, int] | None = None,
) -> EtsyUsStandardEstimate:
    sync = store.get_product_sync(job.job_id, job.product_sync_id or "")
    synchronized = {item.variant_id: item for item in sync.variants}
    selected_ids = variant_ids or tuple(synchronized)
    retail_overrides = retail_overrides or {}
    production_overrides = production_overrides or {}
    costs = ProductCostEvidence(
        product_sync_fingerprint=product_sync_fingerprint or sync.fingerprint,
        observed_at=calculated_at,
        variants=tuple(
            ProductVariantCostEvidence(
                variant_id=variant_id,
                retail_price_cents=retail_overrides.get(
                    variant_id, synchronized[variant_id].retail_price_cents
                ),
                production_cost_cents=production_overrides.get(
                    variant_id, synchronized[variant_id].production_cost_cents
                ),
            )
            for variant_id in selected_ids
        ),
    )
    shipping = parse_standard_us_shipping(
        {
            "data": [
                {
                    "type": "variant_shipping_standard_us",
                    "id": str(variant_id),
                    "attributes": {
                        "shippingType": "standard",
                        "country": {"code": "US"},
                        "variantId": variant_id,
                        "shippingPlanId": "standard-us",
                        "handlingTime": {"from": 2, "to": 5},
                        "shippingCost": {
                            "firstItem": {"amount": 399, "currency": "USD"},
                            "additionalItems": {"amount": 200, "currency": "USD"},
                        },
                    },
                }
                for variant_id in selected_ids
            ]
        },
        blueprint_id=145,
        print_provider_id=39,
        expected_variant_ids=selected_ids,
        observed_at=calculated_at,
    )
    return estimate_etsy_us_standard_proceeds(
        product_costs=costs,
        shipping=shipping,
        calculated_at=calculated_at,
    )


def _settle_pricing_for_review(
    store: InMemorySellerControlStore,
    clock: MutableClock,
    worker: WorkerControlService,
) -> ControlJobRecord:
    pricing_job = store.get_job("job_phase62_worker")
    pricing_work = _activate(store, pricing_job, clock=clock)
    estimate = _estimate_for_current_sync(store, pricing_job, calculated_at=clock.value)
    worker.record_pricing_success(
        RecordPricingSuccessCommand(
            job_id=pricing_job.job_id,
            work_request_id=pricing_work.work_request_id,
            expected_record_version=pricing_job.record_version,
            estimate=estimate,
        )
    )
    return store.get_job(pricing_job.job_id)


def _existing_product_update() -> tuple[
    InMemorySellerControlStore,
    MutableClock,
    WorkerControlService,
    ControlJobRecord,
    WorkRequest,
    str,
]:
    store, clock, worker, _sync_work = _prepare_to_product_sync()
    claimed, active, attempt_id = _begin_create(store, clock, worker)
    worker.record_product_sync_success(
        RecordProductSyncSuccessCommand(
            job_id=claimed.job_id,
            work_request_id=active.work_request_id,
            expected_record_version=claimed.record_version,
            attempt_id=attempt_id,
            observation=_observation(),
        )
    )
    reviewable = _settle_pricing_for_review(store, clock, worker)
    review = store.get_review(reviewable.job_id, reviewable.review_version)
    etag = review_etag(
        job_id=reviewable.job_id,
        review_version=reviewable.review_version,
        review_fingerprint=review.fingerprint,
        product_id=reviewable.product_id,
        product_sync_fingerprint=reviewable.product_sync_fingerprint,
        pricing_snapshot_id=reviewable.pricing_snapshot_id,
        pricing_snapshot_fingerprint=reviewable.pricing_snapshot_fingerprint,
    )
    revised = SellerControlService(store=store, clock=clock).revise_listing(
        ReviseListingCommand(
            job_id=reviewable.job_id,
            owner_id=OWNER,
            expected_record_version=reviewable.record_version,
            idempotency_key="seller-revision-phase62",
            expected_review_version=reviewable.review_version,
            expected_review_fingerprint=review.fingerprint,
            expected_review_etag=etag,
            revision=ListingRevision(
                title="Revised Geometric Badger Tee",
                description=review.description,
                tags=review.tags,
            ),
        )
    )
    revised_job = store.get_job(revised.job_id)
    update_work = _activate(store, revised_job, clock=clock)
    worker.begin_provider_write(
        BeginProviderWriteCommand(
            job_id=revised_job.job_id,
            work_request_id=update_work.work_request_id,
            expected_record_version=revised_job.record_version,
            image_id=IMAGE_ID,
            target_payload_fingerprint=UPDATED_TARGET_FP,
            correlation_token=_correlation_token(revised_job.job_id),
        )
    )
    updating = store.get_job(revised_job.job_id)
    assert (
        worker.authorize_provider_call(
            job_id=updating.job_id,
            attempt_id=updating.provider_write_attempt_id or "",
        )
        is not None
    )
    return (
        store,
        clock,
        worker,
        updating,
        update_work,
        updating.provider_write_attempt_id or "",
    )


def test_existing_product_uses_same_product_update_authority() -> None:
    store, _clock, worker, updating, work, attempt_id = _existing_product_update()
    attempt = store.get_provider_write_attempt(updating.job_id, attempt_id)
    original_create_claim = updating.product_create_attempt_id

    assert attempt.operation is ProviderWriteOperation.UPDATE
    assert attempt.product_id == PRODUCT_ID
    assert attempt.prior_payload_fingerprint == TARGET_FP
    assert attempt.target_payload_fingerprint == UPDATED_TARGET_FP
    assert original_create_claim is not None and original_create_claim != attempt.attempt_id

    with pytest.raises(InvalidControlStateError, match="immutable product identity"):
        worker.record_product_sync_success(
            RecordProductSyncSuccessCommand(
                job_id=updating.job_id,
                work_request_id=work.work_request_id,
                expected_record_version=updating.record_version,
                attempt_id=attempt_id,
                observation=_observation(
                    product_id="different_product",
                    request_fingerprint=UPDATED_TARGET_FP,
                ),
            )
        )
    assert store.get_job(updating.job_id) == updating

    worker.record_product_sync_success(
        RecordProductSyncSuccessCommand(
            job_id=updating.job_id,
            work_request_id=work.work_request_id,
            expected_record_version=updating.record_version,
            attempt_id=attempt_id,
            observation=_observation(request_fingerprint=UPDATED_TARGET_FP),
        )
    )
    completed = store.get_job(updating.job_id)
    assert completed.product_id == PRODUCT_ID
    assert completed.product_create_attempt_id == original_create_claim
    assert completed.provider_payload_fingerprint == UPDATED_TARGET_FP


def test_prior_match_reconciliation_authorizes_only_an_exact_same_product_put_retry() -> None:
    store, clock, worker, updating, work, attempt_id = _existing_product_update()
    original_attempt = store.get_provider_write_attempt(updating.job_id, attempt_id)
    worker.record_product_write_outcome_unknown(
        RecordProductWriteOutcomeUnknownCommand(
            job_id=updating.job_id,
            work_request_id=work.work_request_id,
            expected_record_version=updating.record_version,
            attempt_id=attempt_id,
            code="PROVIDER_CONNECTION_LOST",
        )
    )
    reconciling = store.get_job(updating.job_id)
    reconciliation_work = _activate(store, reconciling, clock=clock)

    retry = worker.record_reconciliation_observation(
        RecordReconciliationObservationCommand(
            job_id=reconciling.job_id,
            work_request_id=reconciliation_work.work_request_id,
            expected_record_version=reconciling.record_version,
            attempt_id=attempt_id,
            outcome=ReconciliationOutcome.PRIOR_MATCH,
            observed_payload_fingerprint=TARGET_FP,
        )
    )
    retrying = store.get_job(updating.job_id)
    retry_work = store.get_work_request(retrying.job_id, retry.work_request_id or "")

    assert retry.state is ControlJobState.PRODUCT_DRAFT_SYNCING
    assert retrying.product_id == PRODUCT_ID
    assert retrying.provider_payload_fingerprint == TARGET_FP
    assert retrying.provider_outcome_unconfirmed is False
    assert retry_work.work_type is WorkType.SYNCHRONIZE_PRODUCT

    active_retry = _activate(store, retrying, clock=clock)
    with pytest.raises(InvalidControlStateError, match="exact prior target"):
        worker.begin_provider_write(
            BeginProviderWriteCommand(
                job_id=retrying.job_id,
                work_request_id=active_retry.work_request_id,
                expected_record_version=retrying.record_version,
                image_id=IMAGE_ID,
                target_payload_fingerprint="7" * 64,
                correlation_token=_correlation_token(retrying.job_id),
            )
        )
    assert store.get_job(retrying.job_id) == retrying

    worker.begin_provider_write(
        BeginProviderWriteCommand(
            job_id=retrying.job_id,
            work_request_id=active_retry.work_request_id,
            expected_record_version=retrying.record_version,
            image_id=IMAGE_ID,
            target_payload_fingerprint=UPDATED_TARGET_FP,
            correlation_token=_correlation_token(retrying.job_id),
        )
    )
    exact_retry = store.get_job(retrying.job_id)
    exact_attempt = store.get_provider_write_attempt(
        exact_retry.job_id,
        exact_retry.provider_write_attempt_id or "",
    )
    assert exact_attempt.operation is ProviderWriteOperation.UPDATE
    assert exact_attempt.product_id == PRODUCT_ID
    assert exact_attempt.target_payload_fingerprint == UPDATED_TARGET_FP
    assert exact_attempt.prior_payload_fingerprint == TARGET_FP
    assert exact_attempt.exact_retry_count == 1
    assert exact_attempt.reconciliation_deadline == original_attempt.reconciliation_deadline


def test_prior_match_after_root_deadline_is_terminal_without_another_put_work() -> None:
    store, clock, worker, updating, work, attempt_id = _existing_product_update()
    worker.record_product_write_outcome_unknown(
        RecordProductWriteOutcomeUnknownCommand(
            job_id=updating.job_id,
            work_request_id=work.work_request_id,
            expected_record_version=updating.record_version,
            attempt_id=attempt_id,
            code="PROVIDER_CONNECTION_LOST",
        )
    )
    reconciling = store.get_job(updating.job_id)
    reconciliation_work = _activate(store, reconciling, clock=clock)
    deadline = store.get_provider_write_attempt(updating.job_id, attempt_id).reconciliation_deadline
    clock.set(deadline)

    terminal = worker.record_reconciliation_observation(
        RecordReconciliationObservationCommand(
            job_id=reconciling.job_id,
            work_request_id=reconciliation_work.work_request_id,
            expected_record_version=reconciling.record_version,
            attempt_id=attempt_id,
            outcome=ReconciliationOutcome.PRIOR_MATCH,
            observed_payload_fingerprint=TARGET_FP,
        )
    )

    persisted = store.get_job(updating.job_id)
    failure = store.get_failure(persisted.job_id, persisted.failure_id or "")
    assert terminal.state is ControlJobState.FAILED_TERMINAL
    assert persisted.active_work_request_id is None
    assert persisted.provider_outcome_unconfirmed is False
    assert persisted.provider_write_attempt_id == attempt_id
    assert failure.code == "PRODUCT_UPDATE_RETRY_EXHAUSTED"


def test_second_ambiguous_update_cannot_request_a_second_exact_put_retry() -> None:
    store, clock, worker, updating, work, first_attempt_id = _existing_product_update()
    first_attempt = store.get_provider_write_attempt(updating.job_id, first_attempt_id)
    worker.record_product_write_outcome_unknown(
        RecordProductWriteOutcomeUnknownCommand(
            job_id=updating.job_id,
            work_request_id=work.work_request_id,
            expected_record_version=updating.record_version,
            attempt_id=first_attempt_id,
            code="PROVIDER_CONNECTION_LOST",
        )
    )
    reconciling = store.get_job(updating.job_id)
    reconciliation_work = _activate(store, reconciling, clock=clock)
    worker.record_reconciliation_observation(
        RecordReconciliationObservationCommand(
            job_id=reconciling.job_id,
            work_request_id=reconciliation_work.work_request_id,
            expected_record_version=reconciling.record_version,
            attempt_id=first_attempt_id,
            outcome=ReconciliationOutcome.PRIOR_MATCH,
            observed_payload_fingerprint=TARGET_FP,
        )
    )
    retrying = store.get_job(updating.job_id)
    retry_work = _activate(store, retrying, clock=clock)
    worker.begin_provider_write(
        BeginProviderWriteCommand(
            job_id=retrying.job_id,
            work_request_id=retry_work.work_request_id,
            expected_record_version=retrying.record_version,
            image_id=IMAGE_ID,
            target_payload_fingerprint=UPDATED_TARGET_FP,
            correlation_token=_correlation_token(retrying.job_id),
        )
    )
    retried = store.get_job(updating.job_id)
    retry_attempt_id = retried.provider_write_attempt_id or ""
    retry_attempt = store.get_provider_write_attempt(retried.job_id, retry_attempt_id)
    assert retry_attempt.attempt_id != first_attempt.attempt_id
    assert retry_attempt.exact_retry_count == 1
    assert retry_attempt.reconciliation_deadline == first_attempt.reconciliation_deadline
    assert (
        worker.authorize_provider_call(
            job_id=retried.job_id,
            attempt_id=retry_attempt_id,
        )
        is not None
    )
    worker.record_product_write_outcome_unknown(
        RecordProductWriteOutcomeUnknownCommand(
            job_id=retried.job_id,
            work_request_id=retry_work.work_request_id,
            expected_record_version=retried.record_version,
            attempt_id=retry_attempt_id,
            code="PROVIDER_CONNECTION_LOST",
        )
    )
    second_reconciling = store.get_job(updating.job_id)
    second_reconciliation_work = _activate(store, second_reconciling, clock=clock)

    terminal = worker.record_reconciliation_observation(
        RecordReconciliationObservationCommand(
            job_id=second_reconciling.job_id,
            work_request_id=second_reconciliation_work.work_request_id,
            expected_record_version=second_reconciling.record_version,
            attempt_id=retry_attempt_id,
            outcome=ReconciliationOutcome.PRIOR_MATCH,
            observed_payload_fingerprint=TARGET_FP,
        )
    )

    persisted = store.get_job(updating.job_id)
    failure = store.get_failure(persisted.job_id, persisted.failure_id or "")
    assert terminal.state is ControlJobState.FAILED_TERMINAL
    assert persisted.active_work_request_id is None
    assert persisted.provider_write_attempt_id == retry_attempt_id
    assert persisted.provider_outcome_unconfirmed is False
    assert failure.code == "PRODUCT_UPDATE_RETRY_EXHAUSTED"


def test_cancellation_dominates_late_target_match_but_preserves_definitive_sync_evidence() -> None:
    store, clock, worker, reconciling, work, attempt_id = _unknown_create_reconciliation()
    cancelled = SellerControlService(store=store, clock=clock).cancel_job(
        CancelJobCommand(
            job_id=reconciling.job_id,
            owner_id=OWNER,
            expected_record_version=reconciling.record_version,
            idempotency_key="cancel-during-reconciliation",
        )
    )
    assert cancelled.state is ControlJobState.CANCEL_REQUESTED

    settled = worker.record_reconciliation_observation(
        RecordReconciliationObservationCommand(
            job_id=reconciling.job_id,
            work_request_id=work.work_request_id,
            expected_record_version=cancelled.record_version,
            attempt_id=attempt_id,
            outcome=ReconciliationOutcome.TARGET_MATCH,
            product=_observation(),
        )
    )
    job = store.get_job(reconciling.job_id)

    assert settled.state is ControlJobState.CANCELLED
    assert job.cancellation_requested_at is not None
    assert job.active_work_request_id is None
    assert job.product_id == PRODUCT_ID
    assert job.product_sync_id is not None
    assert job.provider_outcome_unconfirmed is False
    assert (
        store.get_work_request(job.job_id, work.work_request_id).status
        is WorkRequestStatus.COMPLETED
    )


def test_cancellation_with_exact_prior_match_clears_update_uncertainty() -> None:
    store, clock, worker, updating, work, attempt_id = _existing_product_update()
    worker.record_product_write_outcome_unknown(
        RecordProductWriteOutcomeUnknownCommand(
            job_id=updating.job_id,
            work_request_id=work.work_request_id,
            expected_record_version=updating.record_version,
            attempt_id=attempt_id,
            code="PROVIDER_CONNECTION_LOST",
        )
    )
    reconciling = store.get_job(updating.job_id)
    reconciliation_work = _activate(store, reconciling, clock=clock)
    cancelled = SellerControlService(store=store, clock=clock).cancel_job(
        CancelJobCommand(
            job_id=reconciling.job_id,
            owner_id=OWNER,
            expected_record_version=reconciling.record_version,
            idempotency_key="cancel-prior-match-update",
        )
    )
    assert cancelled.state is ControlJobState.CANCEL_REQUESTED

    settled = worker.record_reconciliation_observation(
        RecordReconciliationObservationCommand(
            job_id=reconciling.job_id,
            work_request_id=reconciliation_work.work_request_id,
            expected_record_version=cancelled.record_version,
            attempt_id=attempt_id,
            outcome=ReconciliationOutcome.PRIOR_MATCH,
            observed_payload_fingerprint=TARGET_FP,
        )
    )
    job = store.get_job(reconciling.job_id)

    assert settled.state is ControlJobState.CANCELLED
    assert job.provider_outcome_unconfirmed is False
    assert job.active_work_request_id is None
    assert job.provider_payload_fingerprint == TARGET_FP
    assert (
        store.get_work_request(job.job_id, reconciliation_work.work_request_id).status
        is WorkRequestStatus.COMPLETED
    )


def test_worker_commands_reject_wrong_active_work_and_do_not_mutate_job() -> None:
    store, _clock, worker, work = _seed_preparation()
    current = store.get_job("job_phase62_worker")

    with pytest.raises(WorkNotActiveError, match="no longer owns"):
        worker.begin_preparation(
            BeginPreparationCommand(
                job_id=current.job_id,
                work_request_id="work_forged",
                expected_record_version=current.record_version,
            )
        )

    assert store.get_job(current.job_id) == current
    assert (
        store.get_work_request(current.job_id, work.work_request_id).status
        is WorkRequestStatus.DISPATCHED
    )
