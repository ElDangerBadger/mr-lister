from __future__ import annotations

from datetime import UTC, datetime, timedelta
from hashlib import sha256

import pytest

from mr_lister.control.commands import (
    ApproveReviewCommand,
    CancelJobCommand,
    ListingRevision,
    RecordWorkerFailureCommand,
    RefreshEconomicsCommand,
    RetryJobCommand,
    ReviseListingCommand,
    SettleCancellationCommand,
    WorkerFailureCode,
)
from mr_lister.control.dispatch import deterministic_execution_name, work_input_fingerprint
from mr_lister.control.errors import (
    EconomicsStaleError,
    InvalidControlStateError,
    NotFoundError,
    RetryNotAllowedError,
    StaleReviewError,
)
from mr_lister.control.fingerprints import product_sync_record_fingerprint, review_etag
from mr_lister.control.models import (
    CancellationDecisionRecord,
    CommandReceipt,
    CommandResponse,
    ControlJobRecord,
    ControlJobState,
    DomainEvent,
    FailureRecord,
    PricingEvidenceRecord,
    PricingSnapshot,
    ProductMockupEvidence,
    ProductSyncRecord,
    ProductVariantEvidence,
    RecoveryAction,
    ReviewActor,
    ReviewContent,
    SourceArtifactRecord,
    WorkRequest,
    WorkRequestStatus,
    WorkType,
)
from mr_lister.control.service import SellerControlService
from mr_lister.control.source_artwork import source_artifact_fingerprint
from mr_lister.control.store import CommandCommit, InMemorySellerControlStore
from mr_lister.control.worker_commands import (
    BeginProviderUploadCommand,
    BeginProviderWriteCommand,
    RecordProductWriteOutcomeUnknownCommand,
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
OTHER_OWNER = "b" * 64
VALID_TAGS = (
    "badger portrait",
    "woodland explorer",
    "compass artwork",
    "forest adventure",
    "vintage drawing",
    "outdoor apparel",
    "nature lover gift",
    "crescent moon",
    "pine silhouette",
    "earthy palette",
    "camping keepsake",
    "wildlife design",
    "retro shirt",
)


def _source_material(*, job_id: str, owner_id: str = OWNER) -> dict[str, object]:
    return {
        "job_id": job_id,
        "owner_id": owner_id,
        "bucket": "mr-lister-phase6-artifacts-test",
        "object_key": f"private/owners/{owner_id}/jobs/{job_id}/source/source.png",
        "version_id": "source-version-1",
        "content_sha256": "8" * 64,
        "size_bytes": 128,
        "media_type": "image/png",
        "product_profile_id": "profile_test",
        "product_profile_version": 1,
        "product_profile_fingerprint": "3" * 64,
        "created_at": NOW,
    }


def _source_fingerprint(job_id: str, *, owner_id: str = OWNER) -> str:
    return source_artifact_fingerprint(**_source_material(job_id=job_id, owner_id=owner_id))


def _source(job: ControlJobRecord) -> SourceArtifactRecord:
    material = _source_material(job_id=job.job_id, owner_id=job.owner_id)
    return SourceArtifactRecord(fingerprint=source_artifact_fingerprint(**material), **material)


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
) -> CommandReceipt:
    identity_fingerprint = sha256(identity.encode("utf-8")).hexdigest()
    return CommandReceipt(
        receipt_id=f"receipt_{identity}",
        owner_id=job.owner_id,
        job_id=job.job_id,
        command_type=f"setup_{identity}",
        idempotency_key_digest=identity_fingerprint,
        request_fingerprint=identity_fingerprint,
        response=_response(job, work_id),
        work_request_id=work_id,
        created_at=NOW,
    )


def _event(job: ControlJobRecord, name: str) -> DomainEvent:
    return DomainEvent(
        job_id=job.job_id,
        sequence=job.event_sequence,
        name=name,
        occurred_at=NOW,
    )


def _work(
    job: ControlJobRecord,
    *,
    work_id: str,
    receipt_id: str,
    work_type: WorkType = WorkType.SYNCHRONIZE_PRODUCT,
    review_version: int | None = 1,
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
        next_dispatch_at=NOW,
        created_at=NOW,
        updated_at=NOW,
    )


def _review(job_id: str) -> ReviewContent:
    return ReviewContent(
        job_id=job_id,
        review_version=1,
        fingerprint="1" * 64,
        actor=ReviewActor.MODEL,
        title="Geometric Badger Graphic Tee",
        description="A geometric woodland badger illustration for an everyday graphic tee.",
        tags=VALID_TAGS,
        audience=("woodland art fans",),
        title_rationale="Names the visible subject and product.",
        tag_rationale="Uses distinct buyer-facing phrases.",
        validation_passed=True,
        artwork_analysis_fingerprint="2" * 64,
        product_profile_fingerprint="3" * 64,
        created_at=NOW,
    )


def seed_product_syncing(
    store: InMemorySellerControlStore,
    *,
    job_id: str = "job_phase6_service",
    dispatch_sync: bool = True,
) -> tuple[ControlJobRecord, ReviewContent, WorkRequest]:
    prepare_work_id = "work_initial_prepare"
    initial = ControlJobRecord(
        owner_id=OWNER,
        job_id=job_id,
        event_sequence=1,
        state=ControlJobState.INTAKE_VALIDATED,
        source_artifact_fingerprint=_source_fingerprint(job_id),
        active_work_request_id=prepare_work_id,
        created_at=NOW,
        updated_at=NOW,
    )
    prepare_receipt = _receipt(
        initial,
        identity="prepare_seed",
        work_id=prepare_work_id,
    )
    prepare_work = _work(
        initial,
        work_id=prepare_work_id,
        receipt_id=prepare_receipt.receipt_id,
        work_type=WorkType.PREPARE,
        review_version=None,
    )
    store.create_job(
        job=initial,
        event=_event(initial, "SETUP_CREATED"),
        receipt=prepare_receipt,
        work_request=prepare_work,
        source_artifact=_source(initial),
    )
    claimed_prepare = store.claim_work(
        job_id,
        prepare_work_id,
        now=NOW,
        claim_id="claim_initial_prepare",
        lease_expires_at=NOW + timedelta(minutes=1),
    )
    assert claimed_prepare is not None
    dispatched_prepare = store.mark_work_dispatched(
        job_id,
        prepare_work_id,
        claim_id="claim_initial_prepare",
        execution_arn=("arn:aws:states:us-west-2:123456789012:execution:mr-lister-prepare:initial"),
        now=NOW,
    )
    analyzing = ControlJobRecord.model_validate(
        {
            **initial.model_dump(mode="python"),
            "state": ControlJobState.ANALYZING_ARTWORK,
            "record_version": 1,
            "event_sequence": 2,
        }
    )
    store.commit_command(
        CommandCommit(
            current=initial,
            updated=analyzing,
            event=_event(analyzing, "ARTWORK_ANALYSIS_STARTED"),
            receipt=_receipt(analyzing, identity="analysis"),
        )
    )
    review = _review(job_id)
    listed = ControlJobRecord.model_validate(
        {
            **analyzing.model_dump(mode="python"),
            "state": ControlJobState.LISTING_DRAFTED,
            "record_version": 2,
            "event_sequence": 3,
            "review_version": 1,
            "review_fingerprint": review.fingerprint,
            "review_validated": True,
        }
    )
    listed_receipt = _receipt(listed, identity="review")
    store.commit_command(
        CommandCommit(
            current=analyzing,
            updated=listed,
            event=_event(listed, "GENERATED_REVIEW_RECORDED"),
            receipt=listed_receipt,
            review=review,
        )
    )
    work_id = "work_initial_sync"
    updated = ControlJobRecord.model_validate(
        {
            **listed.model_dump(mode="python"),
            "state": ControlJobState.PRODUCT_DRAFT_SYNCING,
            "record_version": 3,
            "event_sequence": 4,
            "active_work_request_id": work_id,
        }
    )
    receipt = _receipt(updated, identity="sync", work_id=work_id)
    work = _work(updated, work_id=work_id, receipt_id=receipt.receipt_id)
    completed_prepare = WorkRequest.model_validate(
        {
            **dispatched_prepare.model_dump(mode="python"),
            "status": WorkRequestStatus.COMPLETED,
            "updated_at": NOW,
        }
    )
    store.commit_command(
        CommandCommit(
            current=listed,
            updated=updated,
            event=_event(updated, "PRODUCT_SYNCHRONIZATION_REQUESTED"),
            receipt=receipt,
            work_request=work,
            work_update=(dispatched_prepare, completed_prepare),
        )
    )
    claimed = store.claim_work(
        job_id,
        work_id,
        now=NOW,
        claim_id="claim_initial_sync",
        lease_expires_at=NOW + timedelta(minutes=1),
    )
    assert claimed is not None
    if not dispatch_sync:
        return store.get_job(job_id), review, claimed
    dispatched = store.mark_work_dispatched(
        job_id,
        work_id,
        claim_id="claim_initial_sync",
        execution_arn=("arn:aws:states:us-west-2:123456789012:execution:mr-lister-sync:initial"),
        now=NOW,
    )
    return store.get_job(job_id), review, dispatched


class _DispatchAcknowledgementRaceStore(InMemorySellerControlStore):
    """Inject one exact CLAIMED-to-DISPATCHED race at the worker commit boundary."""

    def __init__(self) -> None:
        super().__init__()
        self.race_work: WorkRequest | None = None
        self.worker_commit_attempts = 0

    def commit_command(self, commit: CommandCommit) -> CommandReceipt:
        if self.race_work is not None and commit.work_update is not None:
            self.worker_commit_attempts += 1
            if self.worker_commit_attempts == 1:
                work = self.race_work
                assert work.claim_id is not None
                self.mark_work_dispatched(
                    work.job_id,
                    work.work_request_id,
                    claim_id=work.claim_id,
                    execution_arn=(
                        "arn:aws:states:us-west-2:123456789012:execution:"
                        "mr-lister-sync:dispatch-race"
                    ),
                    now=NOW,
                )
        return super().commit_command(commit)


def test_worker_rebases_once_when_dispatch_acknowledgement_wins_work_cas() -> None:
    store = _DispatchAcknowledgementRaceStore()
    syncing, _review_content, claimed = seed_product_syncing(
        store,
        job_id="job_dispatch_acknowledgement_race",
        dispatch_sync=False,
    )
    assert claimed.status is WorkRequestStatus.CLAIMED
    store.race_work = claimed
    events_before = len(store.list_events(syncing.job_id))
    command = RecordWorkerFailureCommand(
        job_id=syncing.job_id,
        work_request_id=claimed.work_request_id,
        expected_record_version=syncing.record_version,
        code=WorkerFailureCode.PRODUCTION_UNAVAILABLE,
    )
    service = SellerControlService(store=store, clock=lambda: NOW)

    first = service.record_worker_failure(command)
    replay = service.record_worker_failure(command)

    completed = store.get_work_request(syncing.job_id, claimed.work_request_id)
    assert first == replay
    assert first.state is ControlJobState.FAILED_RETRYABLE
    assert store.worker_commit_attempts == 2
    assert len(store.list_events(syncing.job_id)) == events_before + 1
    assert len(store.list_failures(syncing.job_id)) == 1
    assert completed.status is WorkRequestStatus.COMPLETED
    assert completed.execution_arn is not None


def seed_reviewable(
    store: InMemorySellerControlStore,
    *,
    fresh_until: datetime | None = None,
    mockups: tuple[ProductMockupEvidence, ...] | None = None,
    printify_shop_id: int | None = 42,
) -> tuple[ControlJobRecord, ReviewContent, ProductSyncRecord, PricingSnapshot]:
    syncing, review, work = seed_product_syncing(store)
    sync = ProductSyncRecord(
        sync_id="sync_initial",
        job_id=syncing.job_id,
        review_version=1,
        product_id="product_initial",
        printify_shop_id=printify_shop_id,
        image_id="image_initial",
        payload_fingerprint="4" * 64,
        response_fingerprint="9" * 64,
        fingerprint="5" * 64,
        mockups=(
            mockups
            if mockups is not None
            else (
                ProductMockupEvidence(
                    url="https://images.printify.com/product_initial/front.jpg",
                    position="front",
                    variant_ids=(1000,),
                ),
            )
        ),
        variants=(
            ProductVariantEvidence(
                variant_id=1000,
                color="Black",
                size="S",
                placement_group_id="small",
                retail_price_cents=2999,
                production_cost_cents=1200,
            ),
        ),
        synchronized_at=NOW,
    )
    sync = sync.model_copy(update={"fingerprint": product_sync_record_fingerprint(sync)})
    desired_fresh_until = fresh_until or NOW + timedelta(hours=24)
    observed_at = desired_fresh_until - timedelta(hours=24)
    calculated_at = min(NOW, desired_fresh_until - timedelta(microseconds=1))
    product_costs = ProductCostEvidence(
        product_sync_fingerprint=sync.fingerprint,
        observed_at=observed_at,
        variants=(
            ProductVariantCostEvidence(
                variant_id=1000,
                retail_price_cents=2999,
                production_cost_cents=1200,
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
    pricing = PricingSnapshot(
        snapshot_id="pricing_initial",
        job_id=syncing.job_id,
        review_version=1,
        product_sync_fingerprint=sync.fingerprint,
        fingerprint=estimate.fingerprint,
        fresh_until=estimate.fresh_until,
        created_at=estimate.calculated_at,
    )
    pricing_evidence = PricingEvidenceRecord(
        snapshot_id=pricing.snapshot_id,
        job_id=pricing.job_id,
        review_version=pricing.review_version,
        product_sync_fingerprint=pricing.product_sync_fingerprint,
        fingerprint=pricing.fingerprint,
        estimate=estimate,
        created_at=pricing.created_at,
    )
    updated = ControlJobRecord.model_validate(
        {
            **syncing.model_dump(mode="python"),
            "state": ControlJobState.AWAITING_APPROVAL,
            "record_version": syncing.record_version + 1,
            "event_sequence": syncing.event_sequence + 1,
            "product_id": sync.product_id,
            "provider_payload_fingerprint": sync.payload_fingerprint,
            "product_sync_id": sync.sync_id,
            "synchronized_review_version": 1,
            "product_sync_fingerprint": sync.fingerprint,
            "pricing_snapshot_id": pricing.snapshot_id,
            "pricing_snapshot_fingerprint": pricing.fingerprint,
            "active_work_request_id": None,
        }
    )
    receipt = _receipt(updated, identity="ready")
    completed = WorkRequest.model_validate(
        {
            **work.model_dump(mode="python"),
            "status": WorkRequestStatus.COMPLETED,
            "updated_at": NOW,
        }
    )
    event = _event(updated, "REVIEW_READY")
    commit = CommandCommit(
        current=syncing,
        updated=updated,
        event=event,
        receipt=receipt,
        product_sync=sync,
        pricing_evidence=pricing_evidence,
        pricing_snapshot=pricing,
        work_update=(work, completed),
    )
    if printify_shop_id is None:
        # Reconstruct a valid historical row without reopening the current write boundary.
        store._jobs[updated.job_id] = updated
        store._events[updated.job_id].append(event)
        store._receipts[store._receipt_key(receipt)] = receipt
        store._product_syncs[(updated.job_id, sync.sync_id)] = sync
        store._pricing_evidence[(updated.job_id, pricing.snapshot_id)] = pricing_evidence
        store._pricing[(updated.job_id, pricing.snapshot_id)] = pricing
        store._work[(completed.job_id, completed.work_request_id)] = completed
    else:
        store.commit_command(commit)
    return updated, review, sync, pricing


def current_etag(
    job: ControlJobRecord,
    review: ReviewContent,
    sync: ProductSyncRecord,
    pricing: PricingSnapshot,
) -> str:
    return review_etag(
        job_id=job.job_id,
        review_version=review.review_version,
        review_fingerprint=review.fingerprint,
        product_id=sync.product_id,
        product_sync_fingerprint=sync.fingerprint,
        pricing_snapshot_id=pricing.snapshot_id,
        pricing_snapshot_fingerprint=pricing.fingerprint,
    )


def revision_command(
    job: ControlJobRecord,
    review: ReviewContent,
    sync: ProductSyncRecord,
    pricing: PricingSnapshot,
    *,
    key: str = "revise-key",
    title: str = "Revised Geometric Badger Graphic Tee",
    tags: tuple[str, ...] = VALID_TAGS,
) -> ReviseListingCommand:
    return ReviseListingCommand(
        job_id=job.job_id,
        owner_id=OWNER,
        expected_record_version=job.record_version,
        expected_review_version=review.review_version,
        expected_review_fingerprint=review.fingerprint,
        expected_review_etag=current_etag(job, review, sync, pricing),
        idempotency_key=key,
        revision=ListingRevision(
            title=title,
            description=review.description,
            tags=tags,
        ),
    )


def test_valid_revision_atomically_creates_review_decision_receipt_and_one_sync_work() -> None:
    store = InMemorySellerControlStore()
    job, review, sync, pricing = seed_reviewable(store)
    service = SellerControlService(store=store, clock=lambda: NOW)
    command = revision_command(job, review, sync, pricing)

    first = service.revise_listing(command)
    replayed = service.revise_listing(command)

    assert replayed == first
    assert first.state is ControlJobState.PRODUCT_DRAFT_SYNCING
    assert first.review_version == 2
    assert store.get_review(job.job_id, 2).validation_passed is True
    revised_job = store.get_job(job.job_id)
    assert revised_job.product_id == job.product_id
    assert revised_job.product_sync_id == sync.sync_id
    assert revised_job.synchronized_review_version == sync.review_version
    assert revised_job.product_sync_fingerprint == sync.fingerprint
    assert revised_job.provider_payload_fingerprint == sync.payload_fingerprint
    assert revised_job.pricing_snapshot_id is None
    assert len(store.list_review_decisions(job.job_id)) == 1
    pending = [
        item
        for item in store.list_work_requests(job.job_id)
        if item.status is WorkRequestStatus.PENDING
    ]
    assert [item.work_request_id for item in pending] == [first.work_request_id]


def test_invalid_revision_persists_review_without_external_work() -> None:
    store = InMemorySellerControlStore()
    job, review, sync, pricing = seed_reviewable(store)
    service = SellerControlService(store=store, clock=lambda: NOW)
    repeated = ("badger art", "badger gift", *VALID_TAGS[2:])

    result = service.revise_listing(revision_command(job, review, sync, pricing, tags=repeated))

    revised = store.get_review(job.job_id, 2)
    assert result.state is ControlJobState.NEEDS_REVISION
    assert result.work_request_id is None
    assert revised.validation_passed is False
    assert revised.validation_issue_codes == ("TAG_KEYWORD_REPETITION",)
    assert not [
        item
        for item in store.list_work_requests(job.job_id)
        if item.status is WorkRequestStatus.PENDING
    ]


def test_warning_only_revision_remains_valid_and_stores_only_blocking_issue_codes() -> None:
    store = InMemorySellerControlStore()
    job, review, sync, pricing = seed_reviewable(store)

    result = SellerControlService(store=store, clock=lambda: NOW).revise_listing(
        revision_command(
            job,
            review,
            sync,
            pricing,
            title="Badger Badger Graphic Tee",
        )
    )

    revised = store.get_review(job.job_id, 2)
    assert result.state is ControlJobState.PRODUCT_DRAFT_SYNCING
    assert revised.validation_passed is True
    assert revised.validation_issue_codes == ()


def test_invalid_revision_contract_does_not_chain_raw_seller_content() -> None:
    store = InMemorySellerControlStore()
    job, review, sync, pricing = seed_reviewable(store)
    raw_marker = "RAW_PRIVATE_MARKER_" + ("x" * 30)
    command = revision_command(
        job,
        review,
        sync,
        pricing,
        tags=(raw_marker, *VALID_TAGS[1:]),
    )

    with pytest.raises(InvalidControlStateError) as captured:
        SellerControlService(store=store, clock=lambda: NOW).revise_listing(command)

    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None
    assert raw_marker not in str(captured.value)


def test_approval_binds_exact_composite_authority_and_ends_without_work() -> None:
    store = InMemorySellerControlStore()
    job, review, sync, pricing = seed_reviewable(store)
    etag = current_etag(job, review, sync, pricing)
    service = SellerControlService(store=store, clock=lambda: NOW)

    result = service.approve_review(
        ApproveReviewCommand(
            job_id=job.job_id,
            owner_id=OWNER,
            expected_record_version=job.record_version,
            expected_review_version=review.review_version,
            expected_review_fingerprint=review.fingerprint,
            expected_review_etag=etag,
            idempotency_key="approve-key",
        )
    )

    approved = store.get_job(job.job_id)
    assert result.state is ControlJobState.APPROVED
    assert result.work_request_id is None
    assert approved.approval_fingerprint == etag
    decision = store.list_review_decisions(job.job_id)[0]
    assert decision.approval_fingerprint == etag
    assert approved.approval_decision_id == decision.decision_id
    assert store.get_review_decision(job.job_id, decision.decision_id) == decision
    with pytest.raises(InvalidControlStateError):
        service.cancel_job(
            CancelJobCommand(
                job_id=job.job_id,
                owner_id=OWNER,
                expected_record_version=approved.record_version,
                idempotency_key="late-cancel",
            )
        )


def test_approval_fails_closed_for_legacy_sync_without_shop_authority() -> None:
    store = InMemorySellerControlStore()
    job, review, sync, pricing = seed_reviewable(store, printify_shop_id=None)

    with pytest.raises(StaleReviewError, match="synchronized review authority is stale"):
        SellerControlService(store=store, clock=lambda: NOW).approve_review(
            ApproveReviewCommand(
                job_id=job.job_id,
                owner_id=OWNER,
                expected_record_version=job.record_version,
                expected_review_version=review.review_version,
                expected_review_fingerprint=review.fingerprint,
                expected_review_etag=current_etag(job, review, sync, pricing),
                idempotency_key="approve-legacy-sync",
            )
        )


def test_approval_requires_at_least_one_reviewable_structured_mockup() -> None:
    store = InMemorySellerControlStore()
    job, review, sync, pricing = seed_reviewable(store, mockups=())
    command = ApproveReviewCommand(
        job_id=job.job_id,
        owner_id=OWNER,
        expected_record_version=job.record_version,
        expected_review_version=review.review_version,
        expected_review_fingerprint=review.fingerprint,
        expected_review_etag=current_etag(job, review, sync, pricing),
        idempotency_key="approve-without-mockup",
    )

    with pytest.raises(InvalidControlStateError, match="reviewable product mockup"):
        SellerControlService(store=store, clock=lambda: NOW).approve_review(command)

    assert store.get_job(job.job_id) == job
    assert store.list_review_decisions(job.job_id) == ()


def test_approval_requires_the_complete_pricing_evidence_record() -> None:
    store = InMemorySellerControlStore()
    job, review, sync, pricing = seed_reviewable(store)
    store._pricing_evidence.pop((job.job_id, pricing.snapshot_id))
    command = ApproveReviewCommand(
        job_id=job.job_id,
        owner_id=OWNER,
        expected_record_version=job.record_version,
        expected_review_version=review.review_version,
        expected_review_fingerprint=review.fingerprint,
        expected_review_etag=current_etag(job, review, sync, pricing),
        idempotency_key="approve-without-pricing-evidence",
    )

    with pytest.raises(EconomicsStaleError, match="Complete economics evidence"):
        SellerControlService(store=store, clock=lambda: NOW).approve_review(command)

    assert store.get_job(job.job_id) == job
    assert store.list_review_decisions(job.job_id) == ()


def test_approval_recomputes_complete_pricing_evidence_authority() -> None:
    store = InMemorySellerControlStore()
    job, review, sync, pricing = seed_reviewable(store)
    evidence = store.get_pricing_evidence(job.job_id, pricing.snapshot_id)
    changed_estimate = evidence.estimate.model_copy(
        update={"fresh_until": evidence.estimate.fresh_until + timedelta(minutes=1)}
    )
    store._pricing_evidence[(job.job_id, pricing.snapshot_id)] = evidence.model_copy(
        update={"estimate": changed_estimate}
    )
    command = ApproveReviewCommand(
        job_id=job.job_id,
        owner_id=OWNER,
        expected_record_version=job.record_version,
        expected_review_version=review.review_version,
        expected_review_fingerprint=review.fingerprint,
        expected_review_etag=current_etag(job, review, sync, pricing),
        idempotency_key="approve-with-corrupt-pricing-evidence",
    )

    with pytest.raises(StaleReviewError, match="complete economics evidence"):
        SellerControlService(store=store, clock=lambda: NOW).approve_review(command)

    assert store.get_job(job.job_id) == job
    assert store.list_review_decisions(job.job_id) == ()


def test_approval_rejects_a_corrupted_persisted_mockup_before_projection() -> None:
    store = InMemorySellerControlStore()
    job, review, sync, pricing = seed_reviewable(store)
    unsafe = sync.mockups[0].model_copy(
        update={"url": "HTTPS://images.printify.com/mockup/front.png"}
    )
    corrupted = sync.model_copy(update={"mockups": (unsafe, *sync.mockups[1:])})
    store._product_syncs[(job.job_id, sync.sync_id)] = corrupted
    command = ApproveReviewCommand(
        job_id=job.job_id,
        owner_id=OWNER,
        expected_record_version=job.record_version,
        expected_review_version=review.review_version,
        expected_review_fingerprint=review.fingerprint,
        expected_review_etag=current_etag(job, review, sync, pricing),
        idempotency_key="approve-with-hidden-mockup",
    )

    with pytest.raises(StaleReviewError, match="synchronized review authority is stale"):
        SellerControlService(store=store, clock=lambda: NOW).approve_review(command)

    assert store.get_job(job.job_id) == job
    assert store.list_review_decisions(job.job_id) == ()


def test_stale_etag_and_expired_economics_cannot_approve() -> None:
    store = InMemorySellerControlStore()
    job, review, sync, pricing = seed_reviewable(store, fresh_until=NOW)
    service = SellerControlService(store=store, clock=lambda: NOW)
    base = dict(
        job_id=job.job_id,
        owner_id=OWNER,
        expected_record_version=job.record_version,
        expected_review_version=review.review_version,
        expected_review_fingerprint=review.fingerprint,
        idempotency_key="approve-key",
    )

    with pytest.raises(StaleReviewError):
        service.approve_review(ApproveReviewCommand(**base, expected_review_etag="f" * 64))
    with pytest.raises(EconomicsStaleError):
        service.approve_review(
            ApproveReviewCommand(
                **base,
                expected_review_etag=current_etag(job, review, sync, pricing),
            )
        )
    assert store.get_job(job.job_id) == job


def refresh_command(
    job: ControlJobRecord,
    review: ReviewContent,
    sync: ProductSyncRecord,
    pricing: PricingSnapshot | None,
    *,
    owner_id: str = OWNER,
    key: str = "refresh-economics-key",
) -> RefreshEconomicsCommand:
    etag = review_etag(
        job_id=job.job_id,
        review_version=review.review_version,
        review_fingerprint=review.fingerprint,
        product_id=sync.product_id,
        product_sync_fingerprint=sync.fingerprint,
        pricing_snapshot_id=None if pricing is None else pricing.snapshot_id,
        pricing_snapshot_fingerprint=None if pricing is None else pricing.fingerprint,
    )
    return RefreshEconomicsCommand(
        job_id=job.job_id,
        owner_id=owner_id,
        expected_record_version=job.record_version,
        expected_review_version=review.review_version,
        expected_review_fingerprint=review.fingerprint,
        expected_review_etag=etag,
        idempotency_key=key,
    )


def test_stale_economics_refresh_is_atomic_idempotent_and_enqueues_exact_work() -> None:
    store = InMemorySellerControlStore()
    job, review, sync, pricing = seed_reviewable(store, fresh_until=NOW)
    service = SellerControlService(store=store, clock=lambda: NOW)
    command = refresh_command(job, review, sync, pricing)

    first = service.refresh_economics(command)
    replay = service.refresh_economics(command)

    refreshed = store.get_job(job.job_id)
    pending = tuple(
        work
        for work in store.list_work_requests(job.job_id)
        if work.status is WorkRequestStatus.PENDING
    )
    assert replay == first
    assert first.state is ControlJobState.PRICING_REFRESHING
    assert first.work_request_id is not None
    assert refreshed.pricing_snapshot_id is None
    assert refreshed.pricing_snapshot_fingerprint is None
    assert refreshed.active_work_request_id == first.work_request_id
    assert tuple(work.work_request_id for work in pending) == (first.work_request_id,)
    assert pending[0].work_type is WorkType.REFRESH_ECONOMICS
    assert pending[0].review_version == review.review_version
    assert store.get_pricing(job.job_id, pricing.snapshot_id) == pricing
    assert store.list_events(job.job_id)[-1].name == "ECONOMICS_REFRESH_REQUESTED"


def test_missing_pricing_pointer_can_refresh_without_fabricating_authority() -> None:
    store = InMemorySellerControlStore()
    job, review, sync, _pricing = seed_reviewable(store)
    missing = ControlJobRecord.model_validate(
        {
            **job.model_dump(mode="python"),
            "pricing_snapshot_id": None,
            "pricing_snapshot_fingerprint": None,
        }
    )
    store._jobs[job.job_id] = missing
    service = SellerControlService(store=store, clock=lambda: NOW)

    result = service.refresh_economics(refresh_command(missing, review, sync, None))

    refreshed = store.get_job(job.job_id)
    assert result.state is ControlJobState.PRICING_REFRESHING
    assert refreshed.pricing_snapshot_id is None
    assert refreshed.active_work_request_id == result.work_request_id


def test_fresh_wrong_owner_or_stale_review_cannot_refresh_economics() -> None:
    store = InMemorySellerControlStore()
    job, review, sync, pricing = seed_reviewable(store)
    service = SellerControlService(store=store, clock=lambda: NOW)

    with pytest.raises(InvalidControlStateError, match="still fresh"):
        service.refresh_economics(refresh_command(job, review, sync, pricing))
    with pytest.raises(NotFoundError):
        service.refresh_economics(
            refresh_command(job, review, sync, pricing, owner_id=OTHER_OWNER, key="wrong-owner")
        )
    stale = refresh_command(job, review, sync, pricing, key="stale-review").model_copy(
        update={"expected_review_etag": "f" * 64}
    )
    with pytest.raises(StaleReviewError):
        service.refresh_economics(stale)
    assert store.get_job(job.job_id) == job


def test_pre_review_cancellation_cancels_pending_work_without_review_reference() -> None:
    store = InMemorySellerControlStore()
    work_id = "work_prepare_pending"
    initial = ControlJobRecord(
        owner_id=OWNER,
        job_id="job_pre_review_cancel",
        event_sequence=1,
        state=ControlJobState.INTAKE_VALIDATED,
        source_artifact_fingerprint=_source_fingerprint("job_pre_review_cancel"),
        active_work_request_id=work_id,
        created_at=NOW,
        updated_at=NOW,
    )
    receipt = _receipt(initial, identity="prepare", work_id=work_id)
    work = WorkRequest(
        work_request_id=work_id,
        owner_id=OWNER,
        job_id=initial.job_id,
        receipt_id=receipt.receipt_id,
        work_type=WorkType.PREPARE,
        input_fingerprint=work_input_fingerprint(
            work_type=WorkType.PREPARE,
            job_id=initial.job_id,
            work_request_id=work_id,
        ),
        execution_name=deterministic_execution_name(work_id),
        next_dispatch_at=NOW,
        created_at=NOW,
        updated_at=NOW,
    )
    store.create_job(
        job=initial,
        event=_event(initial, "INTAKE_COMPLETED"),
        receipt=receipt,
        work_request=work,
        source_artifact=_source(initial),
    )

    result = SellerControlService(store=store, clock=lambda: NOW).cancel_job(
        CancelJobCommand(
            job_id=initial.job_id,
            owner_id=OWNER,
            expected_record_version=0,
            idempotency_key="cancel-pre-review",
        )
    )

    decisions: tuple[CancellationDecisionRecord, ...] = store.list_cancellation_decisions(
        initial.job_id
    )
    assert result.state is ControlJobState.CANCELLED
    assert store.get_work_request(initial.job_id, work_id).status is WorkRequestStatus.CANCELLED
    assert decisions[0].review_version is None
    assert decisions[0].review_fingerprint is None


def test_idle_review_state_cancels_terminally_without_stranded_work() -> None:
    store = InMemorySellerControlStore()
    job, _review, _sync, _pricing = seed_reviewable(store)

    result = SellerControlService(store=store, clock=lambda: NOW).cancel_job(
        CancelJobCommand(
            job_id=job.job_id,
            owner_id=OWNER,
            expected_record_version=job.record_version,
            idempotency_key="cancel-idle-review",
        )
    )

    assert result.state is ControlJobState.CANCELLED
    assert store.get_job(job.job_id).active_work_request_id is None


def seed_reconciliation(
    store: InMemorySellerControlStore,
    *,
    dispatched: bool,
) -> tuple[ControlJobRecord, WorkRequest]:
    syncing, _review, sync_work = seed_product_syncing(
        store,
        job_id="job_reconciliation",
    )
    worker = WorkerControlService(store=store, clock=lambda: NOW)
    target_fingerprint = "4" * 64
    correlation_digest = sha256(f"mr-lister:provider-draft:{syncing.job_id}".encode()).hexdigest()[
        :24
    ]
    source = store.get_source_artifact(syncing.job_id)
    file_name = worker.upload_file_name(syncing.job_id, source.content_sha256)
    worker.begin_provider_upload(
        BeginProviderUploadCommand(
            job_id=syncing.job_id,
            work_request_id=sync_work.work_request_id,
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
            work_request_id=sync_work.work_request_id,
            expected_record_version=upload_claim.record_version,
            attempt_id=upload_attempt_id,
            observation=UploadedArtworkObservation(
                image_id="image_reconciliation_seed",
                file_name=file_name,
                width=3021,
                height=3927,
                size_bytes=source.size_bytes,
            ),
        )
    )
    syncing = store.get_job(syncing.job_id)
    worker.begin_provider_write(
        BeginProviderWriteCommand(
            job_id=syncing.job_id,
            work_request_id=sync_work.work_request_id,
            expected_record_version=syncing.record_version,
            image_id="image_reconciliation_seed",
            target_payload_fingerprint=target_fingerprint,
            correlation_token=f"ml-{correlation_digest}",
        )
    )
    claimed_write = store.get_job(syncing.job_id)
    attempt_id = claimed_write.provider_write_attempt_id or ""
    assert (
        worker.authorize_provider_call(
            job_id=syncing.job_id,
            attempt_id=attempt_id,
        )
        is not None
    )
    response = worker.record_product_write_outcome_unknown(
        RecordProductWriteOutcomeUnknownCommand(
            job_id=syncing.job_id,
            work_request_id=sync_work.work_request_id,
            expected_record_version=claimed_write.record_version,
            attempt_id=attempt_id,
            code="PROVIDER_TIMEOUT",
        )
    )
    assert response.work_request_id is not None
    job = store.get_job(syncing.job_id)
    work_id = response.work_request_id
    work = store.get_work_request(job.job_id, work_id)
    if not dispatched:
        return job, work
    claimed = store.claim_work(
        job.job_id,
        work_id,
        now=NOW,
        claim_id="claim_reconcile",
        lease_expires_at=NOW + timedelta(minutes=1),
    )
    assert claimed is not None
    work = store.mark_work_dispatched(
        job.job_id,
        work_id,
        claim_id="claim_reconcile",
        execution_arn=("arn:aws:states:us-west-2:123456789012:execution:mr-lister-reconcile:first"),
        now=NOW,
    )
    return job, work


def test_cancelling_pending_reconciliation_retains_work_and_intent() -> None:
    store = InMemorySellerControlStore()
    job, work = seed_reconciliation(store, dispatched=False)

    result = SellerControlService(store=store, clock=lambda: NOW).cancel_job(
        CancelJobCommand(
            job_id=job.job_id,
            owner_id=OWNER,
            expected_record_version=job.record_version,
            idempotency_key="cancel-reconciliation",
        )
    )

    assert result.state is ControlJobState.CANCEL_REQUESTED
    assert store.get_work_request(job.job_id, work.work_request_id).status is (
        WorkRequestStatus.PENDING
    )
    assert store.get_job(job.job_id).cancellation_requested_at == NOW


def test_transient_reconciliation_failure_redrives_without_restoring_seller_actions() -> None:
    store = InMemorySellerControlStore()
    job, work = seed_reconciliation(store, dispatched=True)
    service = SellerControlService(store=store, clock=lambda: NOW)
    cancelling = service.cancel_job(
        CancelJobCommand(
            job_id=job.job_id,
            owner_id=OWNER,
            expected_record_version=job.record_version,
            idempotency_key="cancel-active-reconciliation",
        )
    )

    result = service.record_worker_failure(
        RecordWorkerFailureCommand(
            job_id=job.job_id,
            work_request_id=work.work_request_id,
            expected_record_version=cancelling.record_version,
            code=WorkerFailureCode.PRODUCTION_UNAVAILABLE,
        )
    )

    persisted = store.get_job(job.job_id)
    assert result.state is ControlJobState.RECONCILIATION_REQUIRED
    assert persisted.cancellation_requested_at == NOW
    assert result.work_request_id is not None
    assert store.get_work_request(job.job_id, result.work_request_id).work_type is (
        WorkType.RECONCILE_PRODUCT
    )

    with pytest.raises(InvalidControlStateError, match="cannot accept cancellation"):
        service.cancel_job(
            CancelJobCommand(
                job_id=job.job_id,
                owner_id=OWNER,
                expected_record_version=persisted.record_version,
                idempotency_key="second-cancel-must-not-reopen-intent",
            )
        )


def test_dispatched_work_cancellation_intent_dominates_late_worker_settlement() -> None:
    store = InMemorySellerControlStore()
    syncing, _review, work = seed_product_syncing(store)
    service = SellerControlService(store=store, clock=lambda: NOW)

    cancelling = service.cancel_job(
        CancelJobCommand(
            job_id=syncing.job_id,
            owner_id=OWNER,
            expected_record_version=syncing.record_version,
            idempotency_key="cancel-active",
        )
    )
    settled = service.settle_cancellation(
        SettleCancellationCommand(
            job_id=syncing.job_id,
            work_request_id=work.work_request_id,
            expected_record_version=cancelling.record_version,
        )
    )

    assert cancelling.state is ControlJobState.CANCEL_REQUESTED
    assert settled.state is ControlJobState.CANCELLED
    assert store.get_work_request(syncing.job_id, work.work_request_id).status is (
        WorkRequestStatus.COMPLETED
    )


def seed_dispatched_prepare(
    store: InMemorySellerControlStore,
    *,
    job_id: str,
) -> tuple[ControlJobRecord, WorkRequest]:
    work_id = f"work_{job_id}"
    job = ControlJobRecord(
        owner_id=OWNER,
        job_id=job_id,
        event_sequence=1,
        state=ControlJobState.INTAKE_VALIDATED,
        source_artifact_fingerprint=_source_fingerprint(job_id),
        active_work_request_id=work_id,
        created_at=NOW,
        updated_at=NOW,
    )
    receipt = _receipt(job, identity=f"prepare_{job_id}", work_id=work_id)
    work = _work(
        job,
        work_id=work_id,
        receipt_id=receipt.receipt_id,
        work_type=WorkType.PREPARE,
        review_version=None,
    )
    store.create_job(
        job=job,
        event=_event(job, "INTAKE_COMPLETED"),
        receipt=receipt,
        work_request=work,
        source_artifact=_source(job),
    )
    claimed = store.claim_work(
        job_id,
        work_id,
        now=NOW,
        claim_id=f"claim_{job_id}",
        lease_expires_at=NOW + timedelta(minutes=1),
    )
    assert claimed is not None
    dispatched = store.mark_work_dispatched(
        job_id,
        work_id,
        claim_id=f"claim_{job_id}",
        execution_arn=(
            f"arn:aws:states:us-west-2:123456789012:execution:mr-lister-prepare:{job_id}"
        ),
        now=NOW,
    )
    return job, dispatched


def test_dispatched_intake_cancels_safely_and_cannot_invent_product_ambiguity() -> None:
    store = InMemorySellerControlStore()
    job, work = seed_dispatched_prepare(store, job_id="job_prepare_wrong_code")
    service = SellerControlService(store=store, clock=lambda: NOW)
    cancelling = service.cancel_job(
        CancelJobCommand(
            job_id=job.job_id,
            owner_id=OWNER,
            expected_record_version=job.record_version,
            idempotency_key="cancel-prepare",
        )
    )

    result = service.record_worker_failure(
        RecordWorkerFailureCommand(
            job_id=job.job_id,
            work_request_id=work.work_request_id,
            expected_record_version=cancelling.record_version,
            code=WorkerFailureCode.PRODUCT_CREATE_OUTCOME_UNKNOWN,
        )
    )

    assert cancelling.state is ControlJobState.CANCEL_REQUESTED
    assert result.state is ControlJobState.CANCELLED
    assert result.work_request_id is None
    assert store.get_work_request(job.job_id, work.work_request_id).last_error_code == (
        WorkerFailureCode.UNCLASSIFIED_FAILURE.value
    )


def test_non_product_cancellation_settlement_never_schedules_reconciliation() -> None:
    store = InMemorySellerControlStore()
    job, work = seed_dispatched_prepare(store, job_id="job_prepare_settlement")
    service = SellerControlService(store=store, clock=lambda: NOW)
    cancelling = service.cancel_job(
        CancelJobCommand(
            job_id=job.job_id,
            owner_id=OWNER,
            expected_record_version=job.record_version,
            idempotency_key="cancel-prepare-settlement",
        )
    )

    result = service.settle_cancellation(
        SettleCancellationCommand(
            job_id=job.job_id,
            work_request_id=work.work_request_id,
            expected_record_version=cancelling.record_version,
        )
    )

    assert result.state is ControlJobState.CANCELLED
    assert result.work_request_id is None
    assert store.get_job(job.job_id).provider_outcome_unconfirmed is False


def test_retry_uses_only_the_persisted_failure_recovery_step() -> None:
    store = InMemorySellerControlStore()
    syncing, _review, work = seed_product_syncing(store)
    service = SellerControlService(store=store, clock=lambda: NOW)
    failed = service.record_worker_failure(
        RecordWorkerFailureCommand(
            job_id=syncing.job_id,
            work_request_id=work.work_request_id,
            expected_record_version=syncing.record_version,
            code=WorkerFailureCode.PRODUCTION_UNAVAILABLE,
        )
    )

    retried = service.retry_job(
        RetryJobCommand(
            job_id=syncing.job_id,
            owner_id=OWNER,
            expected_record_version=failed.record_version,
            idempotency_key="retry-sync",
        )
    )

    assert failed.state is ControlJobState.FAILED_RETRYABLE
    assert retried.state is ControlJobState.PRODUCT_DRAFT_SYNCING
    assert retried.work_request_id is not None
    assert store.get_work_request(syncing.job_id, retried.work_request_id).work_type is (
        WorkType.SYNCHRONIZE_PRODUCT
    )


def test_retry_revalidates_corrupt_persisted_recovery_authority() -> None:
    store = InMemorySellerControlStore()
    syncing, _review, work = seed_product_syncing(store)
    service = SellerControlService(store=store, clock=lambda: NOW)
    failed = service.record_worker_failure(
        RecordWorkerFailureCommand(
            job_id=syncing.job_id,
            work_request_id=work.work_request_id,
            expected_record_version=syncing.record_version,
            code=WorkerFailureCode.PRODUCTION_UNAVAILABLE,
        )
    )
    failure = store.list_failures(syncing.job_id)[0]
    store._failures[(syncing.job_id, failure.failure_id)] = failure.model_copy(
        update={"recovery_action": RecoveryAction.RETRY_PREPARATION}
    )

    with pytest.raises(RetryNotAllowedError, match="persisted failure"):
        service.retry_job(
            RetryJobCommand(
                job_id=syncing.job_id,
                owner_id=OWNER,
                expected_record_version=failed.record_version,
                idempotency_key="retry-corrupt-authority",
            )
        )


def test_terminal_failure_is_sanitized_and_cannot_be_retried() -> None:
    store = InMemorySellerControlStore()
    syncing, _review, work = seed_product_syncing(store)
    service = SellerControlService(store=store, clock=lambda: NOW)

    failed = service.record_worker_failure(
        RecordWorkerFailureCommand(
            job_id=syncing.job_id,
            work_request_id=work.work_request_id,
            expected_record_version=syncing.record_version,
            code=WorkerFailureCode.PRODUCTION_CONFIGURATION,
        )
    )

    failure = store.list_failures(syncing.job_id)[0]
    assert failed.state is ControlJobState.FAILED_TERMINAL
    assert failure.code == "PRODUCTION_CONFIGURATION"
    assert failure.retryable is False
    assert "message" not in failure.model_dump()
    with pytest.raises(RetryNotAllowedError):
        service.retry_job(
            RetryJobCommand(
                job_id=syncing.job_id,
                owner_id=OWNER,
                expected_record_version=failed.record_version,
                idempotency_key="retry-terminal",
            )
        )


def test_failure_transition_requires_exact_record_and_closed_recovery_specification() -> None:
    store = InMemorySellerControlStore()
    syncing, _review, work = seed_product_syncing(store)
    completed = WorkRequest.model_validate(
        {
            **work.model_dump(mode="python"),
            "status": WorkRequestStatus.COMPLETED,
            "last_error_code": WorkerFailureCode.PRODUCTION_UNAVAILABLE.value,
            "updated_at": NOW,
        }
    )
    updated = ControlJobRecord.model_validate(
        {
            **syncing.model_dump(mode="python"),
            "state": ControlJobState.FAILED_RETRYABLE,
            "record_version": syncing.record_version + 1,
            "event_sequence": syncing.event_sequence + 1,
            "active_work_request_id": None,
            "failure_id": "failure_exact",
        }
    )
    receipt = _receipt(updated, identity="failure_exact")
    base = CommandCommit(
        current=syncing,
        updated=updated,
        event=_event(updated, "WORK_FAILED"),
        receipt=receipt,
        work_update=(work, completed),
    )

    with pytest.raises(InvalidControlStateError, match="immutable record"):
        store.commit_command(base)

    valid_failure = FailureRecord(
        failure_id="failure_exact",
        job_id=syncing.job_id,
        work_request_id=work.work_request_id,
        stage=syncing.state,
        code=WorkerFailureCode.PRODUCTION_UNAVAILABLE.value,
        retryable=True,
        recovery_action=RecoveryAction.RETRY_PRODUCT_SYNC,
        resume_state=ControlJobState.PRODUCT_DRAFT_SYNCING,
        work_type=WorkType.SYNCHRONIZE_PRODUCT,
        occurred_at=NOW,
    )
    mismatched = valid_failure.model_copy(update={"recovery_action": RecoveryAction.RETRY_PRICING})
    with pytest.raises(InvalidControlStateError, match="recovery specification"):
        store.commit_command(
            CommandCommit(
                **{
                    **base.__dict__,
                    "failure": mismatched,
                }
            )
        )


def test_cross_owner_lookup_is_indistinguishable_from_absence() -> None:
    store = InMemorySellerControlStore()
    job, _review, _sync, _pricing = seed_reviewable(store)

    with pytest.raises(NotFoundError) as wrong_owner:
        SellerControlService(store=store, clock=lambda: NOW).cancel_job(
            CancelJobCommand(
                job_id=job.job_id,
                owner_id=OTHER_OWNER,
                expected_record_version=job.record_version,
                idempotency_key="cross-owner",
            )
        )
    with pytest.raises(NotFoundError) as absent:
        store.get_job_for_owner(OWNER, "job_absent")

    assert wrong_owner.value.code == absent.value.code == "NOT_FOUND"
