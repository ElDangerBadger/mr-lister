from __future__ import annotations

import ast
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path
from threading import Barrier

import pytest

from mr_lister.contracts import Placement, PlacementGroup, ProductProfile
from mr_lister.control.economics import ProductCostEvidence, ProductVariantCostEvidence
from mr_lister.control.fingerprints import (
    canonical_fingerprint,
    product_sync_record_fingerprint,
    review_etag,
)
from mr_lister.control.models import (
    ControlJobRecord,
    ControlJobState,
    PricingEvidenceRecord,
    PricingSnapshot,
    ProductMockupEvidence,
    ProductSyncRecord,
    ProductVariantEvidence,
    ReviewActor,
    ReviewContent,
    ReviewDecision,
    ReviewDecisionRecord,
    SourceArtifactRecord,
)
from mr_lister.control.source_artwork import source_artifact_fingerprint
from mr_lister.production.economics import estimate_etsy_us_standard_proceeds
from mr_lister.production.printify_shipping import parse_standard_us_shipping
from mr_lister.publication.commands import (
    PublicationCommandReceipt,
    RequestPublicationCommand,
)
from mr_lister.publication.contract import PublicationPermitState, PublicationState
from mr_lister.publication.errors import (
    PublicationAuthorityError,
    PublicationConflictError,
    PublicationErrorCode,
    PublicationIdempotencyConflictError,
    PublicationNotFoundError,
)
from mr_lister.publication.fingerprints import publication_command_receipt_fingerprint
from mr_lister.publication.service import PublicationRequestService
from mr_lister.publication.store import (
    InMemoryPublicationStore,
    PublicationRequestAuthority,
    PublicationRequestTransaction,
)
from mr_lister.review_profile import ExactReviewProductProfile, ReviewProfileNotFoundError

ROOT = Path(__file__).resolve().parents[1]
OWNER_ID = "a" * 64
OTHER_OWNER_ID = "b" * 64
SOURCE_AT = datetime(2026, 8, 23, 16, 0, tzinfo=UTC)
REVIEW_AT = SOURCE_AT + timedelta(minutes=5)
ECONOMICS_AT = SOURCE_AT + timedelta(minutes=10)
APPROVED_AT = SOURCE_AT + timedelta(minutes=15)
NOW = SOURCE_AT + timedelta(hours=1)
RELEASE_FINGERPRINT = "f" * 64
TAGS = (
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


def _profile(*, publish_enabled: bool = True) -> ProductProfile:
    return ProductProfile(
        profile_id="phase71_profile",
        profile_version=1,
        blueprint_id=145,
        print_provider_id=39,
        variant_ids=(),
        colors=("Black",),
        sizes=("S",),
        retail_price_cents=2999,
        buyer_shipping_cents=0,
        placement_groups=(
            PlacementGroup(
                group_id="small",
                sizes=("S",),
                canvas_width=3021,
                canvas_height=3927,
                placement=Placement(x=0.5, y=0.25, scale=0.65),
            ),
        ),
        publish_enabled=publish_enabled,
    )


def _review_fingerprint(review_values: dict[str, object]) -> str:
    return canonical_fingerprint(review_values)


def _authority(
    *,
    profile: ProductProfile | None = None,
    job_id: str = "job_phase71_service",
    variant_color: str = "Black",
    variant_size: str = "S",
    variant_group: str = "small",
) -> tuple[PublicationRequestAuthority, ExactReviewProductProfile]:
    selected_profile = profile or _profile()
    profile_fingerprint = canonical_fingerprint(selected_profile)
    exact_profile = ExactReviewProductProfile(
        profile=selected_profile,
        fingerprint=profile_fingerprint,
    )
    source_values: dict[str, object] = {
        "job_id": job_id,
        "owner_id": OWNER_ID,
        "bucket": "mr-lister-phase71-artifacts-test",
        "object_key": f"private/owners/{OWNER_ID}/jobs/{job_id}/source/source.png",
        "version_id": "source-version-1",
        "content_sha256": "8" * 64,
        "size_bytes": 256,
        "media_type": "image/png",
        "product_profile_id": selected_profile.profile_id,
        "product_profile_version": selected_profile.profile_version,
        "product_profile_fingerprint": profile_fingerprint,
        "created_at": SOURCE_AT,
    }
    source = SourceArtifactRecord(
        **source_values,
        fingerprint=source_artifact_fingerprint(**source_values),
    )
    review_values: dict[str, object] = {
        "contract_version": "2.0.0",
        "job_id": job_id,
        "review_version": 1,
        "actor": ReviewActor.MODEL.value,
        "title": "Geometric Badger Graphic Tee",
        "description": "A geometric woodland badger illustration for an everyday graphic tee.",
        "tags": TAGS,
        "audience": ("woodland art fans",),
        "title_rationale": "Names the visible subject and product.",
        "tag_rationale": "Uses distinct buyer-facing phrases.",
        "validation_passed": True,
        "validation_issue_codes": (),
        "artwork_analysis_fingerprint": "2" * 64,
        "product_profile_fingerprint": profile_fingerprint,
        "created_at": REVIEW_AT.isoformat(),
    }
    review = ReviewContent(
        **{**review_values, "created_at": REVIEW_AT},
        fingerprint=_review_fingerprint(review_values),
    )
    sync = ProductSyncRecord(
        sync_id="sync_phase71",
        job_id=job_id,
        review_version=1,
        product_id="product_phase71",
        image_id="image_phase71",
        printify_shop_id=987654,
        payload_fingerprint="4" * 64,
        response_fingerprint="5" * 64,
        fingerprint="6" * 64,
        mockups=(
            ProductMockupEvidence(
                url="https://images.printify.com/product_phase71/front.jpg",
                position="front",
                variant_ids=(1000,),
            ),
        ),
        variants=(
            ProductVariantEvidence(
                variant_id=1000,
                color=variant_color,
                size=variant_size,
                placement_group_id=variant_group,
                retail_price_cents=2999,
                production_cost_cents=1200,
            ),
        ),
        synchronized_at=ECONOMICS_AT,
    )
    sync = sync.model_copy(update={"fingerprint": product_sync_record_fingerprint(sync)})
    product_costs = ProductCostEvidence(
        product_sync_fingerprint=sync.fingerprint,
        observed_at=ECONOMICS_AT,
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
        blueprint_id=selected_profile.blueprint_id,
        print_provider_id=selected_profile.print_provider_id,
        expected_variant_ids=(1000,),
        observed_at=ECONOMICS_AT,
    )
    estimate = estimate_etsy_us_standard_proceeds(
        product_costs=product_costs,
        shipping=shipping,
        calculated_at=ECONOMICS_AT,
    )
    pricing = PricingSnapshot(
        snapshot_id="pricing_phase71",
        job_id=job_id,
        review_version=1,
        product_sync_fingerprint=sync.fingerprint,
        fingerprint=estimate.fingerprint,
        fresh_until=estimate.fresh_until,
        created_at=estimate.calculated_at,
    )
    evidence = PricingEvidenceRecord(
        snapshot_id=pricing.snapshot_id,
        job_id=job_id,
        review_version=1,
        product_sync_fingerprint=sync.fingerprint,
        fingerprint=estimate.fingerprint,
        estimate=estimate,
        created_at=estimate.calculated_at,
    )
    approval_fingerprint = review_etag(
        job_id=job_id,
        review_version=review.review_version,
        review_fingerprint=review.fingerprint,
        product_id=sync.product_id,
        product_sync_fingerprint=sync.fingerprint,
        pricing_snapshot_id=pricing.snapshot_id,
        pricing_snapshot_fingerprint=pricing.fingerprint,
    )
    approval_receipt_id = "approval_receipt_phase71"
    decision = ReviewDecisionRecord(
        decision_id=f"decision_{sha256(approval_receipt_id.encode()).hexdigest()[:40]}",
        job_id=job_id,
        actor_owner_id=OWNER_ID,
        decision=ReviewDecision.APPROVE,
        review_version=1,
        review_fingerprint=review.fingerprint,
        approval_fingerprint=approval_fingerprint,
        command_receipt_id=approval_receipt_id,
        decided_at=APPROVED_AT,
    )
    job = ControlJobRecord(
        owner_id=OWNER_ID,
        job_id=job_id,
        record_version=12,
        event_sequence=8,
        state=ControlJobState.APPROVED,
        review_version=1,
        review_fingerprint=review.fingerprint,
        review_validated=True,
        source_artifact_fingerprint=source.fingerprint,
        artwork_analysis_id="analysis_phase71",
        artwork_analysis_fingerprint=review.artwork_analysis_fingerprint,
        product_id=sync.product_id,
        provider_payload_fingerprint=sync.payload_fingerprint,
        product_sync_id=sync.sync_id,
        synchronized_review_version=1,
        product_sync_fingerprint=sync.fingerprint,
        pricing_snapshot_id=pricing.snapshot_id,
        pricing_snapshot_fingerprint=pricing.fingerprint,
        approval_decision_id=decision.decision_id,
        approved_review_version=1,
        approved_review_fingerprint=review.fingerprint,
        approval_fingerprint=approval_fingerprint,
        provider_upload_attempt_id="upload_attempt_phase71",
        uploaded_artwork_id="uploaded_artwork_phase71",
        uploaded_image_id=sync.image_id,
        uploaded_artwork_fingerprint="7" * 64,
        created_at=SOURCE_AT,
        updated_at=APPROVED_AT,
    )
    return (
        PublicationRequestAuthority(
            current_job=job,
            review=review,
            approval_decision=decision,
            source=source,
            product_sync=sync,
            pricing_snapshot=pricing,
            pricing_evidence=evidence,
        ),
        exact_profile,
    )


class ProfileAuthority:
    def __init__(self, exact: ExactReviewProductProfile) -> None:
        self.exact = exact
        self.calls = 0
        self.fail = False

    def get_exact(self, *, profile_id: str, profile_version: int) -> ExactReviewProductProfile:
        self.calls += 1
        if self.fail:
            raise AssertionError("Profile lookup must not run during receipt replay")
        if (
            self.exact.profile.profile_id != profile_id
            or self.exact.profile.profile_version != profile_version
        ):
            raise ReviewProfileNotFoundError()
        return self.exact


class CountingClock:
    def __init__(self, now: datetime) -> None:
        self.now = now
        self.calls = 0
        self.fail = False

    def __call__(self) -> datetime:
        self.calls += 1
        if self.fail:
            raise AssertionError("Clock must not run during receipt replay")
        return self.now


class AuthorityStore:
    """Small protocol oracle used to expose malformed authority without prevalidation."""

    def __init__(self, authority: PublicationRequestAuthority) -> None:
        self.authority = authority
        self.transaction: PublicationRequestTransaction | None = None
        self.receipt: PublicationCommandReceipt | None = None
        self.commit_error: PublicationConflictError | None = None

    def resolve_request_receipt(
        self,
        owner_id: str,
        job_id: str,
        key_digest: str,
    ) -> PublicationCommandReceipt | None:
        del owner_id, job_id, key_digest
        return self.receipt

    def load_request_authority(
        self,
        owner_id: str,
        job_id: str,
    ) -> PublicationRequestAuthority:
        del owner_id, job_id
        return self.authority

    def commit_request(
        self,
        transaction: PublicationRequestTransaction,
    ) -> PublicationCommandReceipt:
        self.transaction = transaction
        if self.commit_error is not None:
            raise self.commit_error
        self.receipt = transaction.commit.receipt
        return transaction.commit.receipt

    def get_aggregate_for_owner(self, owner_id: str, job_id: str) -> object:
        raise AssertionError(f"Unexpected aggregate read for {owner_id}/{job_id}")


def _command(
    authority: PublicationRequestAuthority,
    **updates: object,
) -> RequestPublicationCommand:
    job = authority.current_job
    values: dict[str, object] = {
        "owner_id": job.owner_id,
        "job_id": job.job_id,
        "expected_record_version": job.record_version,
        "expected_review_version": authority.review.review_version,
        "expected_review_fingerprint": authority.review.fingerprint,
        "expected_review_etag": job.approval_fingerprint,
        "expected_approval_decision_id": authority.approval_decision.decision_id,
        "expected_approval_fingerprint": job.approval_fingerprint,
        "confirmation": "publish_exact_approved_listing",
        "idempotency_key": "phase71-request-key",
    }
    values.update(updates)
    return RequestPublicationCommand(**values)


def _service(
    store: object,
    exact_profile: ExactReviewProductProfile,
    *,
    clock: CountingClock | None = None,
) -> tuple[PublicationRequestService, ProfileAuthority, CountingClock]:
    profiles = ProfileAuthority(exact_profile)
    selected_clock = clock or CountingClock(NOW)
    service = PublicationRequestService(
        store=store,  # type: ignore[arg-type]
        profiles=profiles,
        release_manifest_fingerprint=RELEASE_FINGERPRINT,
        clock=selected_clock,
    )
    return service, profiles, selected_clock


def test_request_atomically_freezes_exact_authority_without_changing_phase6_state() -> None:
    authority, exact_profile = _authority()
    store = InMemoryPublicationStore((authority,))
    service, profiles, clock = _service(store, exact_profile)

    response = service.request_publication(_command(authority))

    updated = store.jobs[authority.current_job.job_id]
    aggregate = store.get_aggregate_for_owner(OWNER_ID, authority.current_job.job_id)
    snapshot = next(iter(store.snapshots.values()))
    attempt = next(iter(store.attempts.values()))
    permit = next(iter(store.permits.values()))
    work = next(iter(store.work_requests.values()))
    event = next(iter(store.events.values()))
    receipt = next(iter(store.receipts.values()))
    assert response == receipt.response
    assert updated.state is ControlJobState.APPROVED
    assert updated.record_version == authority.current_job.record_version + 1
    assert updated.event_sequence == authority.current_job.event_sequence
    assert updated.publication_aggregate_id == aggregate.aggregate_id
    assert aggregate.state is PublicationState.PUBLICATION_REQUESTED
    assert snapshot.printify_shop_id == authority.product_sync.printify_shop_id
    assert snapshot.printify_product_id == authority.product_sync.product_id
    assert snapshot.printify_image_id == authority.product_sync.image_id
    assert snapshot.profile_fingerprint == exact_profile.fingerprint
    assert snapshot.release_manifest_fingerprint == RELEASE_FINGERPRINT
    assert snapshot.requested_at == NOW
    assert snapshot.verification_deadline == NOW + timedelta(seconds=1800)
    assert attempt.publish_post_call_count == 0
    assert permit.status is PublicationPermitState.AVAILABLE
    assert work.status.value == "pending"
    assert event.sequence == 1
    assert profiles.calls == clock.calls == 1


def test_exact_replay_is_receipt_first_and_never_reloads_mutable_authority() -> None:
    authority, exact_profile = _authority()
    store = InMemoryPublicationStore((authority,))
    service, profiles, clock = _service(store, exact_profile)
    command = _command(authority)
    first = service.request_publication(command)
    profiles.fail = True
    clock.fail = True

    replay = service.request_publication(command)

    assert replay == first
    assert profiles.calls == clock.calls == 1
    assert len(store.aggregates) == len(store.receipts) == 1


def test_same_key_changed_command_conflicts_before_authority_or_clock_reads() -> None:
    authority, exact_profile = _authority()
    store = InMemoryPublicationStore((authority,))
    service, profiles, clock = _service(store, exact_profile)
    service.request_publication(_command(authority))
    profiles.fail = True
    clock.fail = True

    with pytest.raises(PublicationIdempotencyConflictError) as captured:
        service.request_publication(_command(authority, expected_approval_fingerprint="e" * 64))

    assert captured.value.code is PublicationErrorCode.IDEMPOTENCY_CONFLICT
    assert profiles.calls == clock.calls == 1


def test_different_key_after_success_is_rejected_as_already_requested() -> None:
    authority, exact_profile = _authority()
    store = InMemoryPublicationStore((authority,))
    service, _profiles, _clock = _service(store, exact_profile)
    service.request_publication(_command(authority))

    with pytest.raises(PublicationConflictError) as captured:
        service.request_publication(_command(authority, idempotency_key="different-key"))

    assert captured.value.code is PublicationErrorCode.ALREADY_REQUESTED
    assert len(store.aggregates) == len(store.receipts) == 1


@pytest.mark.parametrize(
    ("updates", "expected_code"),
    [
        ({"expected_record_version": 11}, PublicationErrorCode.STALE_RECORD),
        ({"expected_review_version": 2}, PublicationErrorCode.STALE_REVIEW),
        ({"expected_review_fingerprint": "c" * 64}, PublicationErrorCode.STALE_REVIEW),
        ({"expected_review_etag": "c" * 64}, PublicationErrorCode.STALE_REVIEW),
        (
            {"expected_approval_decision_id": "different_decision"},
            PublicationErrorCode.STALE_APPROVAL,
        ),
        ({"expected_approval_fingerprint": "c" * 64}, PublicationErrorCode.STALE_APPROVAL),
    ],
)
def test_stale_command_authority_is_classified_without_committing(
    updates: dict[str, object],
    expected_code: PublicationErrorCode,
) -> None:
    authority, exact_profile = _authority()
    store = AuthorityStore(authority)
    service, _profiles, _clock = _service(store, exact_profile)

    with pytest.raises(PublicationAuthorityError) as captured:
        service.request_publication(_command(authority, **updates))

    assert captured.value.code is expected_code
    assert store.transaction is None


def test_wrong_owner_uses_the_same_not_found_boundary_as_an_unknown_job() -> None:
    authority, exact_profile = _authority()
    store = InMemoryPublicationStore((authority,))
    service, _profiles, _clock = _service(store, exact_profile)

    with pytest.raises(PublicationNotFoundError) as wrong_owner:
        service.request_publication(_command(authority, owner_id=OTHER_OWNER_ID))
    with pytest.raises(PublicationNotFoundError) as unknown:
        service.request_publication(_command(authority, job_id="unknown_job"))

    assert str(wrong_owner.value) == str(unknown.value)


def test_nonapproved_and_previously_linked_jobs_are_rejected_before_deeper_reads() -> None:
    authority, exact_profile = _authority()
    not_approved = replace(
        authority,
        current_job=authority.current_job.model_copy(
            update={"state": ControlJobState.AWAITING_APPROVAL}
        ),
    )
    linked = replace(
        authority,
        current_job=authority.current_job.model_copy(
            update={"publication_aggregate_id": "publication_existing"}
        ),
    )
    service, _profiles, _clock = _service(AuthorityStore(not_approved), exact_profile)
    with pytest.raises(PublicationAuthorityError) as state_error:
        service.request_publication(_command(not_approved))
    assert state_error.value.code is PublicationErrorCode.NOT_APPROVED

    service, _profiles, _clock = _service(AuthorityStore(linked), exact_profile)
    with pytest.raises(PublicationConflictError) as linked_error:
        service.request_publication(_command(linked))
    assert linked_error.value.code is PublicationErrorCode.ALREADY_REQUESTED


@pytest.mark.parametrize("legacy_field", ["approval_decision_id", "printify_shop_id"])
def test_legacy_authority_missing_phase71_prerequisites_fails_closed(legacy_field: str) -> None:
    authority, exact_profile = _authority()
    if legacy_field == "approval_decision_id":
        changed = replace(
            authority,
            current_job=authority.current_job.model_copy(update={"approval_decision_id": None}),
        )
    else:
        changed = replace(
            authority,
            product_sync=authority.product_sync.model_copy(update={"printify_shop_id": None}),
        )
    service, _profiles, _clock = _service(AuthorityStore(changed), exact_profile)

    with pytest.raises(PublicationAuthorityError) as captured:
        service.request_publication(_command(changed))

    assert captured.value.code is PublicationErrorCode.INVALID_AUTHORITY


def test_review_and_pricing_content_are_recomputed_instead_of_trusting_stored_hashes() -> None:
    authority, exact_profile = _authority()
    changed_review = replace(
        authority,
        review=authority.review.model_copy(update={"title": "Fingerprint bypass attempt"}),
    )
    changed_estimate = authority.pricing_evidence.estimate.model_copy(
        update={
            "fresh_until": authority.pricing_evidence.estimate.fresh_until + timedelta(minutes=1)
        }
    )
    changed_pricing = replace(
        authority,
        pricing_evidence=authority.pricing_evidence.model_copy(
            update={"estimate": changed_estimate}
        ),
    )
    for changed in (changed_review, changed_pricing):
        service, _profiles, _clock = _service(AuthorityStore(changed), exact_profile)
        with pytest.raises(PublicationAuthorityError) as captured:
            service.request_publication(_command(changed))
        assert captured.value.code is PublicationErrorCode.INVALID_AUTHORITY


def test_expired_pricing_is_rejected_at_the_exact_request_instant() -> None:
    authority, exact_profile = _authority()
    clock = CountingClock(authority.pricing_snapshot.fresh_until)
    service, _profiles, _clock = _service(
        AuthorityStore(authority),
        exact_profile,
        clock=clock,
    )

    with pytest.raises(PublicationAuthorityError) as captured:
        service.request_publication(_command(authority))

    assert captured.value.code is PublicationErrorCode.PRICING_NOT_FRESH


def test_profile_must_be_exact_and_explicitly_publication_enabled() -> None:
    disabled_authority, disabled_exact = _authority(profile=_profile(publish_enabled=False))
    disabled_service, _profiles, _clock = _service(
        AuthorityStore(disabled_authority),
        disabled_exact,
    )
    with pytest.raises(PublicationAuthorityError) as disabled:
        disabled_service.request_publication(_command(disabled_authority))
    assert disabled.value.code is PublicationErrorCode.INVALID_AUTHORITY

    authority, _exact = _authority()
    mismatched_profile = _profile(publish_enabled=False)
    mismatched_exact = ExactReviewProductProfile(
        profile=mismatched_profile,
        fingerprint=canonical_fingerprint(mismatched_profile),
    )
    mismatch_service, _profiles, _clock = _service(
        AuthorityStore(authority),
        mismatched_exact,
    )
    with pytest.raises(PublicationAuthorityError) as mismatch:
        mismatch_service.request_publication(_command(authority))
    assert mismatch.value.code is PublicationErrorCode.INVALID_AUTHORITY


@pytest.mark.parametrize(
    "variant_authority",
    [
        {"variant_color": "Navy"},
        {"variant_size": "XL"},
        {"variant_group": "wrong_group"},
    ],
)
def test_live_selector_profile_must_match_exact_variant_policy(
    variant_authority: dict[str, str],
) -> None:
    authority, exact_profile = _authority(**variant_authority)
    service, _profiles, _clock = _service(AuthorityStore(authority), exact_profile)

    with pytest.raises(PublicationAuthorityError) as captured:
        service.request_publication(_command(authority))

    assert captured.value.code is PublicationErrorCode.INVALID_AUTHORITY


def test_release_manifest_and_clock_are_fail_closed_configuration_authority() -> None:
    authority, exact_profile = _authority()
    with pytest.raises(ValueError, match="nonzero release manifest"):
        PublicationRequestService(
            store=AuthorityStore(authority),
            profiles=ProfileAuthority(exact_profile),
            release_manifest_fingerprint="0" * 64,
        )

    service, _profiles, _clock = _service(
        AuthorityStore(authority),
        exact_profile,
        clock=CountingClock(NOW.replace(tzinfo=None)),
    )
    with pytest.raises(PublicationAuthorityError) as captured:
        service.request_publication(_command(authority))
    assert captured.value.code is PublicationErrorCode.INVALID_AUTHORITY


class CommittedThenConflictedStore(InMemoryPublicationStore):
    def __init__(self, authority: PublicationRequestAuthority) -> None:
        super().__init__((authority,))
        self.injected = False

    def commit_request(
        self,
        transaction: PublicationRequestTransaction,
    ) -> PublicationCommandReceipt:
        receipt = super().commit_request(transaction)
        if not self.injected:
            self.injected = True
            raise PublicationConflictError(
                PublicationErrorCode.CONCURRENT_WRITE,
                "Injected post-commit response loss",
            )
        return receipt


class BarrierPublicationStore(InMemoryPublicationStore):
    """Force two callers to load the same pre-commit authority before either can write."""

    def __init__(self, authority: PublicationRequestAuthority) -> None:
        super().__init__((authority,))
        self.load_barrier = Barrier(2)

    def load_request_authority(
        self,
        owner_id: str,
        job_id: str,
    ) -> PublicationRequestAuthority:
        authority = super().load_request_authority(owner_id, job_id)
        self.load_barrier.wait(timeout=5)
        return authority


def test_response_loss_after_atomic_commit_resolves_the_exact_durable_receipt() -> None:
    authority, exact_profile = _authority()
    store = CommittedThenConflictedStore(authority)
    service, _profiles, _clock = _service(store, exact_profile)

    response = service.request_publication(_command(authority))

    assert response == next(iter(store.receipts.values())).response
    assert len(store.aggregates) == len(store.receipts) == 1


def test_two_threads_with_the_same_key_return_one_exact_durable_result() -> None:
    authority, exact_profile = _authority()
    store = BarrierPublicationStore(authority)
    service, _profiles, _clock = _service(store, exact_profile)
    command = _command(authority)

    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = tuple(pool.map(service.request_publication, (command, command)))

    assert responses[0] == responses[1]
    assert len(store.aggregates) == 1
    assert len(store.work_requests) == 1
    assert len(store.receipts) == 1


def test_two_threads_with_different_keys_persist_exactly_one_publication_graph() -> None:
    authority, exact_profile = _authority()
    store = BarrierPublicationStore(authority)
    service, _profiles, _clock = _service(store, exact_profile)
    commands = (
        _command(authority, idempotency_key="concurrent-key-a"),
        _command(authority, idempotency_key="concurrent-key-b"),
    )

    def request(command: RequestPublicationCommand) -> object:
        try:
            return service.request_publication(command)
        except PublicationConflictError as error:
            return error

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = tuple(pool.map(request, commands))

    successes = tuple(item for item in outcomes if not isinstance(item, Exception))
    conflicts = tuple(item for item in outcomes if isinstance(item, PublicationConflictError))
    assert len(successes) == len(conflicts) == 1
    assert conflicts[0].code in {
        PublicationErrorCode.ALREADY_REQUESTED,
        PublicationErrorCode.CONCURRENT_WRITE,
    }
    assert len(store.aggregates) == 1
    assert len(store.work_requests) == 1
    assert len(store.receipts) == 1


def test_concurrent_write_without_own_receipt_is_not_misreported_as_replay() -> None:
    authority, exact_profile = _authority()
    store = AuthorityStore(authority)
    store.commit_error = PublicationConflictError(
        PublicationErrorCode.CONCURRENT_WRITE,
        "Injected losing transaction",
    )
    service, _profiles, _clock = _service(store, exact_profile)

    with pytest.raises(PublicationConflictError) as captured:
        service.request_publication(_command(authority))

    assert captured.value.code is PublicationErrorCode.CONCURRENT_WRITE


def test_only_receipt_id_depends_on_idempotency_key_among_persisted_identifiers() -> None:
    authority, exact_profile = _authority()
    first_store = AuthorityStore(authority)
    second_store = AuthorityStore(authority)
    first_service, _profiles, _clock = _service(first_store, exact_profile)
    second_service, _profiles, _clock = _service(second_store, exact_profile)
    first_service.request_publication(_command(authority, idempotency_key="first-key"))
    second_service.request_publication(_command(authority, idempotency_key="second-key"))
    assert first_store.transaction is not None
    assert second_store.transaction is not None
    first = first_store.transaction.commit
    second = second_store.transaction.commit

    assert (
        first.aggregate.aggregate_id,
        first.snapshot.snapshot_id,
        first.attempt.attempt_id,
        first.permit.permit_id,
        first.work_request.work_request_id,
        first.work_request.execution_name,
    ) == (
        second.aggregate.aggregate_id,
        second.snapshot.snapshot_id,
        second.attempt.attempt_id,
        second.permit.permit_id,
        second.work_request.work_request_id,
        second.work_request.execution_name,
    )
    assert first.receipt.receipt_id != second.receipt.receipt_id


def test_receipt_first_replay_rejects_self_consistent_forged_record_identifiers() -> None:
    authority, exact_profile = _authority()
    real_store = InMemoryPublicationStore((authority,))
    service, _profiles, _clock = _service(real_store, exact_profile)
    command = _command(authority)
    service.request_publication(command)
    real_receipt = next(iter(real_store.receipts.values()))
    forged_values = real_receipt.model_dump(mode="python", exclude={"fingerprint"})
    forged_values.update(
        {
            "receipt_id": "publication_receipt_forged",
            "aggregate_id": "publication_forged",
            "snapshot_id": "publication_snapshot_forged",
            "attempt_id": "publication_attempt_forged",
            "permit_id": "publication_permit_forged",
            "work_request_id": "publication_work_forged",
        }
    )
    response = dict(forged_values["response"])
    response.update(
        {
            "publication_aggregate_id": "publication_forged",
            "work_request_id": "publication_work_forged",
        }
    )
    forged_values["response"] = response
    forged = PublicationCommandReceipt(
        **forged_values,
        fingerprint=publication_command_receipt_fingerprint(forged_values),
    )
    replay_store = AuthorityStore(authority)
    replay_store.receipt = forged
    replay_service, profiles, clock = _service(replay_store, exact_profile)
    profiles.fail = True
    clock.fail = True

    with pytest.raises(PublicationAuthorityError) as captured:
        replay_service.request_publication(command)

    assert captured.value.code is PublicationErrorCode.INVALID_AUTHORITY
    assert profiles.calls == clock.calls == 0


@pytest.mark.parametrize("response_field", ["record_version", "review_version", "deadline"])
def test_receipt_first_replay_rejects_self_consistent_forged_response_authority(
    response_field: str,
) -> None:
    authority, exact_profile = _authority()
    real_store = InMemoryPublicationStore((authority,))
    service, _profiles, _clock = _service(real_store, exact_profile)
    command = _command(authority)
    service.request_publication(command)
    real_receipt = next(iter(real_store.receipts.values()))
    forged_values = real_receipt.model_dump(mode="python", exclude={"fingerprint"})
    response = dict(forged_values["response"])
    if response_field == "record_version":
        response["record_version"] = real_receipt.response.record_version + 1
    elif response_field == "review_version":
        response["review_version"] = real_receipt.response.review_version + 1
    else:
        response["verification_deadline"] = real_receipt.response.verification_deadline + timedelta(
            seconds=1
        )
    forged_values["response"] = response
    forged = PublicationCommandReceipt(
        **forged_values,
        fingerprint=publication_command_receipt_fingerprint(forged_values),
    )
    replay_store = AuthorityStore(authority)
    replay_store.receipt = forged
    replay_service, profiles, clock = _service(replay_store, exact_profile)
    profiles.fail = True
    clock.fail = True

    with pytest.raises(PublicationAuthorityError) as captured:
        replay_service.request_publication(command)

    assert captured.value.code is PublicationErrorCode.INVALID_AUTHORITY
    assert profiles.calls == clock.calls == 0


def test_service_imports_no_provider_cloud_network_or_execution_capability() -> None:
    tree = ast.parse((ROOT / "src" / "mr_lister" / "publication" / "service.py").read_text())
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    forbidden = {
        "boto3",
        "botocore",
        "requests",
        "httpx",
        "urllib",
        "mr_lister.cloud",
        "mr_lister.provider",
        "mr_lister.production",
    }

    assert not any(
        module == blocked or module.startswith(f"{blocked}.")
        for module in imported
        for blocked in forbidden
    )
