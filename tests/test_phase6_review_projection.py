from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from mr_lister.contracts import ArtworkAnalysis, ProductProfile
from mr_lister.contracts.presentation import ProductMockupEvidence
from mr_lister.control.dispatch import deterministic_execution_name, work_input_fingerprint
from mr_lister.control.errors import NotFoundError
from mr_lister.control.fingerprints import canonical_fingerprint, review_etag
from mr_lister.control.models import (
    AgentPreparationEvidence,
    ArtworkAnalysisRecord,
    ControlJobRecord,
    ControlJobState,
    FailureRecord,
    PricingEvidenceRecord,
    PricingSnapshot,
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
from mr_lister.control.projection import (
    PreviewGrant,
    ReviewProjectionUnavailableError,
    SellerReviewProjectionService,
    _product_sync_fingerprint,
    _review_content_fingerprint,
)
from mr_lister.control.projection_models import (
    ActionReason,
    EconomicsReadiness,
    ReviewDisplayState,
    ReviewStage,
    SectionReadiness,
    SellerAction,
)
from mr_lister.production.economics import (
    ProductCostEvidence,
    ProductVariantCostEvidence,
    estimate_etsy_us_standard_proceeds,
)
from mr_lister.production.printify_shipping import parse_standard_us_shipping
from mr_lister.review_profile import ExactReviewProductProfile

NOW = datetime(2026, 8, 22, 12, 0, tzinfo=UTC)
OWNER = "a" * 64
OTHER_OWNER = "b" * 64
JOB_ID = "job_projection"
SOURCE_FP = "1" * 64
PAYLOAD_FP = "5" * 64
PROFILE = ProductProfile.model_validate_json(
    Path("config/product_profiles/gildan_64000_swiftpod.json").read_text()
)
PROFILE_FP = canonical_fingerprint(PROFILE)
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


class FakeProjectionStore:
    def __init__(
        self,
        *,
        job: ControlJobRecord,
        source: SourceArtifactRecord,
        review: ReviewContent,
        analysis: ArtworkAnalysisRecord,
        agent: AgentPreparationEvidence,
        sync: ProductSyncRecord,
        pricing: PricingSnapshot,
        pricing_evidence: PricingEvidenceRecord,
    ) -> None:
        self.job = job
        self.source = source
        self.reviews = {review.review_version: review}
        self.analysis = analysis
        self.agent = agent
        self.sync = sync
        self.pricing = pricing
        self.pricing_evidence = pricing_evidence
        self.failure: FailureRecord | None = None
        self.work: WorkRequest | None = None
        self.calls: list[str] = []

    def get_job_for_owner(self, owner_id: str, job_id: str) -> ControlJobRecord:
        self.calls.append("get_job_for_owner")
        if owner_id != self.job.owner_id or job_id != self.job.job_id:
            raise NotFoundError("The requested job was not found")
        return self.job

    def get_source_artifact(self, job_id: str) -> SourceArtifactRecord:
        self.calls.append("get_source_artifact")
        return self.source

    def get_review(self, job_id: str, review_version: int) -> ReviewContent:
        self.calls.append("get_review")
        try:
            return self.reviews[review_version]
        except KeyError as error:
            raise NotFoundError("The requested review was not found") from error

    def get_artwork_analysis(self, job_id: str, analysis_id: str) -> ArtworkAnalysisRecord:
        self.calls.append("get_artwork_analysis")
        return self.analysis

    def get_agent_evidence(self, job_id: str, evidence_id: str) -> AgentPreparationEvidence:
        self.calls.append("get_agent_evidence")
        return self.agent

    def get_product_sync(self, job_id: str, sync_id: str) -> ProductSyncRecord:
        self.calls.append("get_product_sync")
        return self.sync

    def get_pricing(self, job_id: str, snapshot_id: str) -> PricingSnapshot:
        self.calls.append("get_pricing")
        return self.pricing

    def get_pricing_evidence(self, job_id: str, snapshot_id: str) -> PricingEvidenceRecord:
        self.calls.append("get_pricing_evidence")
        return self.pricing_evidence

    def get_failure(self, job_id: str, failure_id: str) -> FailureRecord:
        self.calls.append("get_failure")
        if self.failure is None:
            raise NotFoundError("The requested failure was not found")
        return self.failure

    def get_work_request(self, job_id: str, work_request_id: str) -> WorkRequest:
        self.calls.append("get_work_request")
        if self.work is None:
            raise NotFoundError("The requested work was not found")
        return self.work


class FakeProfiles:
    def __init__(self, exact: ExactReviewProductProfile) -> None:
        self.exact = exact

    def get_exact(self, *, profile_id: str, profile_version: int) -> ExactReviewProductProfile:
        assert (profile_id, profile_version) == (PROFILE.profile_id, PROFILE.profile_version)
        return self.exact


class FakePreviewIssuer:
    def __init__(self) -> None:
        self.calls = 0
        self.grant = PreviewGrant(
            url=(
                "https://review.mr-lister.test/v1/jobs/job_projection/artwork-preview"
                "?grant=opaque_preview_grant_12345"
            ),
            expires_at=NOW + timedelta(minutes=5),
            source_artifact_fingerprint=SOURCE_FP,
        )

    def issue(self, *, source: SourceArtifactRecord) -> PreviewGrant:
        self.calls += 1
        assert source.fingerprint == SOURCE_FP
        return self.grant


def _shipping_resource(variant_id: int, amount: int) -> dict[str, object]:
    return {
        "type": "variant_shipping_standard_us",
        "id": str(variant_id),
        "attributes": {
            "shippingType": "standard",
            "country": {"code": "US"},
            "variantId": variant_id,
            "shippingPlanId": f"plan-{variant_id}",
            "handlingTime": {"from": 4, "to": 8},
            "shippingCost": {
                "firstItem": {"amount": amount, "currency": "USD"},
                "additionalItems": {"amount": 219, "currency": "USD"},
            },
        },
    }


def _fixture() -> tuple[FakeProjectionStore, FakePreviewIssuer]:
    variant_rows: list[ProductVariantEvidence] = []
    variant_id = 10_000
    group_by_size = {
        size: group.group_id for group in PROFILE.placement_groups for size in group.sizes
    }
    for color in PROFILE.colors:
        for size in PROFILE.sizes:
            variant_rows.append(
                ProductVariantEvidence(
                    variant_id=variant_id,
                    color=color,
                    size=size,
                    placement_group_id=group_by_size[size],
                    retail_price_cents=PROFILE.retail_price_cents,
                    production_cost_cents=1100 + (variant_id % 7),
                )
            )
            variant_id += 1
    sync = ProductSyncRecord(
        sync_id="sync_projection",
        job_id=JOB_ID,
        review_version=1,
        product_id="product_projection",
        image_id="image_projection",
        payload_fingerprint=PAYLOAD_FP,
        response_fingerprint="6" * 64,
        fingerprint="4" * 64,
        mockups=(
            ProductMockupEvidence(
                url="https://images.printify.com/mockup/front.png?x=1",
                position="front",
                variant_ids=tuple(item.variant_id for item in variant_rows[:15]),
            ),
            ProductMockupEvidence(
                url="https://images.printify.com/mockup/back.png",
                position="back",
                variant_ids=tuple(item.variant_id for item in variant_rows[15:]),
            ),
        ),
        variants=tuple(variant_rows),
        synchronized_at=NOW - timedelta(minutes=5),
    )
    sync = sync.model_copy(update={"fingerprint": _product_sync_fingerprint(sync)})
    observed_at = NOW - timedelta(minutes=4)
    product_costs = ProductCostEvidence(
        product_sync_fingerprint=sync.fingerprint,
        observed_at=observed_at,
        variants=tuple(
            ProductVariantCostEvidence(
                variant_id=item.variant_id,
                retail_price_cents=item.retail_price_cents,
                production_cost_cents=item.production_cost_cents,
            )
            for item in variant_rows
        ),
    )
    shipping = parse_standard_us_shipping(
        {
            "data": [
                _shipping_resource(item.variant_id, 399 + (item.variant_id % 3) * 50)
                for item in variant_rows
            ]
        },
        blueprint_id=PROFILE.blueprint_id,
        print_provider_id=PROFILE.print_provider_id,
        expected_variant_ids=tuple(item.variant_id for item in variant_rows),
        observed_at=observed_at,
    )
    estimate = estimate_etsy_us_standard_proceeds(
        product_costs=product_costs,
        shipping=shipping,
        calculated_at=NOW,
    )
    pricing = PricingSnapshot(
        snapshot_id="pricing_projection",
        job_id=JOB_ID,
        review_version=1,
        product_sync_fingerprint=sync.fingerprint,
        fingerprint=estimate.fingerprint,
        fresh_until=estimate.fresh_until,
        created_at=estimate.calculated_at,
    )
    review = ReviewContent(
        job_id=JOB_ID,
        review_version=1,
        fingerprint="7" * 64,
        actor=ReviewActor.MODEL,
        title="Geometric Badger Graphic Tee",
        description="A geometric woodland badger illustration for an everyday graphic tee.",
        tags=VALID_TAGS,
        audience=("woodland art fans",),
        title_rationale="Names the visible subject and product.",
        tag_rationale="Uses distinct buyer-facing phrases.",
        validation_passed=True,
        artwork_analysis_fingerprint="2" * 64,
        product_profile_fingerprint=PROFILE_FP,
        created_at=NOW - timedelta(minutes=15),
    )
    source = SourceArtifactRecord(
        job_id=JOB_ID,
        owner_id=OWNER,
        fingerprint=SOURCE_FP,
        bucket="private-review-fixture",
        object_key=f"private/owners/{OWNER}/jobs/{JOB_ID}/source/source.png",
        version_id="private-source-version",
        content_sha256="8" * 64,
        size_bytes=512,
        product_profile_id=PROFILE.profile_id,
        product_profile_version=PROFILE.profile_version,
        product_profile_fingerprint=PROFILE_FP,
        created_at=NOW - timedelta(minutes=20),
    )
    analysis = ArtworkAnalysisRecord(
        analysis_id="analysis_projection",
        job_id=JOB_ID,
        work_request_id="work_prepare_projection",
        source_artifact_fingerprint=SOURCE_FP,
        fingerprint="2" * 64,
        analysis=ArtworkAnalysis(
            subject="Geometric woodland badger",
            visual_elements=("badger", "compass", "pine trees"),
            styles=("geometric", "vintage"),
            themes=("woodland", "adventure"),
            visible_text=(),
            audience_hypotheses=("outdoor enthusiasts",),
            color_notes=("earth tones",),
            safety_flags=(),
            confidence=0.94,
        ),
        created_at=NOW - timedelta(minutes=14),
    )
    analysis = analysis.model_copy(
        update={
            "fingerprint": canonical_fingerprint(
                {
                    "job_id": analysis.job_id,
                    "source_artifact_fingerprint": analysis.source_artifact_fingerprint,
                    "analysis": analysis.analysis.model_dump(mode="json"),
                }
            )
        }
    )
    review = review.model_copy(update={"artwork_analysis_fingerprint": analysis.fingerprint})
    review = review.model_copy(update={"fingerprint": _review_content_fingerprint(review)})
    agent = AgentPreparationEvidence(
        evidence_id="agent_projection",
        job_id=JOB_ID,
        work_request_id="work_prepare_projection",
        review_version=1,
        correlation_id="9" * 24,
        framework="strands-agents",
        agent_id="mr-lister-preparation",
        controller_model_id="amazon.nova-controller",
        tool_calls=("record_prepared_review",),
        cycles=1,
        input_tokens=300,
        output_tokens=100,
        total_tokens=400,
        decision_fingerprint="a" * 64,
        fingerprint="3" * 64,
        created_at=NOW - timedelta(minutes=10),
    )
    agent = agent.model_copy(update={"fingerprint": agent.authority_fingerprint})
    job = ControlJobRecord(
        owner_id=OWNER,
        job_id=JOB_ID,
        record_version=10,
        event_sequence=10,
        state=ControlJobState.AWAITING_APPROVAL,
        review_version=1,
        review_fingerprint=review.fingerprint,
        review_validated=True,
        source_artifact_fingerprint=SOURCE_FP,
        artwork_analysis_id=analysis.analysis_id,
        artwork_analysis_fingerprint=analysis.fingerprint,
        agent_evidence_id=agent.evidence_id,
        agent_evidence_fingerprint=agent.fingerprint,
        product_id=sync.product_id,
        provider_payload_fingerprint=PAYLOAD_FP,
        product_sync_id=sync.sync_id,
        synchronized_review_version=1,
        product_sync_fingerprint=sync.fingerprint,
        pricing_snapshot_id=pricing.snapshot_id,
        pricing_snapshot_fingerprint=pricing.fingerprint,
        created_at=NOW - timedelta(minutes=20),
        updated_at=NOW,
    )
    store = FakeProjectionStore(
        job=job,
        source=source,
        review=review,
        analysis=analysis,
        agent=agent,
        sync=sync,
        pricing=pricing,
        pricing_evidence=PricingEvidenceRecord(
            snapshot_id=pricing.snapshot_id,
            job_id=JOB_ID,
            review_version=1,
            product_sync_fingerprint=sync.fingerprint,
            fingerprint=estimate.fingerprint,
            estimate=estimate,
            created_at=estimate.calculated_at,
        ),
    )
    return store, FakePreviewIssuer()


def _service(
    store: FakeProjectionStore,
    preview: FakePreviewIssuer,
    *,
    now: datetime = NOW,
) -> SellerReviewProjectionService:
    return SellerReviewProjectionService(
        store=store,
        profiles=FakeProfiles(
            ExactReviewProductProfile(
                profile=PROFILE,
                fingerprint=PROFILE_FP,
            )
        ),
        clock=lambda: now,
        preview_issuer=preview,
        preview_origin="https://review.mr-lister.test",
    )


def _enabled(projection) -> set[SellerAction]:
    return {item.action for item in projection.actions if item.enabled}


def _active_work(store: FakeProjectionStore, work_type: WorkType) -> WorkRequest:
    work_id = f"work_{work_type.value}"
    return WorkRequest(
        work_request_id=work_id,
        owner_id=OWNER,
        job_id=JOB_ID,
        receipt_id=f"receipt_{work_type.value}",
        work_type=work_type,
        review_version=None if work_type is WorkType.PREPARE else store.job.review_version,
        input_fingerprint=work_input_fingerprint(
            work_type=work_type,
            job_id=JOB_ID,
            work_request_id=work_id,
        ),
        execution_name=deterministic_execution_name(work_id),
        status=WorkRequestStatus.PENDING,
        next_dispatch_at=NOW,
        created_at=NOW,
        updated_at=NOW,
    )


def test_complete_projection_joins_one_safe_human_review() -> None:
    store, preview = _fixture()

    result = _service(store, preview).get(owner_id=OWNER, job_id=JOB_ID)

    assert store.calls[0] == "get_job_for_owner"
    assert result.display_state is ReviewDisplayState.READY
    assert result.stage is ReviewStage.HUMAN_REVIEW
    assert result.authority_notice == "Unpublished — not on Etsy"
    assert _enabled(result) == {
        SellerAction.EDIT_LISTING,
        SellerAction.APPROVE_REVIEW,
        SellerAction.CANCEL_JOB,
    }
    assert result.preview.readiness is SectionReadiness.READY
    assert preview.calls == 1
    assert result.artwork.subject == "Geometric woodland badger"
    assert result.listing.tags == VALID_TAGS
    assert result.validation.passed is True
    assert result.product_policy.product_name == "Product profile gildan_64000_swiftpod"
    assert result.product_policy.provider_name == "Print provider 39"
    assert result.product_policy.colors == PROFILE.colors
    assert result.product_policy.sizes == PROFILE.sizes
    assert tuple(item.group_id for item in result.product_policy.placements) == (
        "small",
        "medium",
        "large",
    )
    assert result.product_policy.retail_price_cents == 2999
    assert result.product_policy.buyer_shipping_cents == 0
    assert len(result.mockups.items) == 2
    assert result.economics.readiness is EconomicsReadiness.READY
    assert len(result.economics.variants) == 30
    assert tuple((item.color, item.size) for item in result.economics.variants) == tuple(
        (color, size) for color in PROFILE.colors for size in PROFILE.sizes
    )
    assert all(item.retail_price_cents == 2999 for item in result.economics.variants)
    assert result.economics.minimum_cents == min(
        item.estimated_proceeds_cents for item in result.economics.variants
    )
    assert result.economics.maximum_cents == max(
        item.estimated_proceeds_cents for item in result.economics.variants
    )
    assert result.economics.production_cost_source == "Connected production product readback"
    assert (
        result.economics.production_cost_observed_at
        == store.pricing_evidence.estimate.product_cost_observed_at
    )
    assert (
        result.economics.production_shipping_source == "Connected production standard US shipping"
    )
    assert (
        result.economics.production_shipping_observed_at
        == store.pricing_evidence.estimate.shipping_observed_at
    )
    assert result.economics.fee_policy_source == "Etsy US standard fee policy"
    assert result.economics.fee_policy_id == "etsy-us-standard-v1"
    assert (
        result.economics.fee_policy_verified_on
        == store.pricing_evidence.estimate.policy.verified_on
    )
    assert result.strands.framework == "strands-agents"
    assert result.strands.prepared_review_version == 1
    assert result.review_authority_etag == review_etag(
        job_id=JOB_ID,
        review_version=1,
        review_fingerprint=store.job.review_fingerprint,
        product_id=store.job.product_id,
        product_sync_fingerprint=store.sync.fingerprint,
        pricing_snapshot_id=store.pricing.snapshot_id,
        pricing_snapshot_fingerprint=store.pricing.fingerprint,
    )


def test_wrong_owner_is_indistinguishable_and_stops_before_any_join_or_preview() -> None:
    store, preview = _fixture()

    with pytest.raises(NotFoundError):
        _service(store, preview).get(owner_id=OTHER_OWNER, job_id=JOB_ID)

    assert store.calls == ["get_job_for_owner"]
    assert preview.calls == 0


def test_malformed_owned_job_is_sanitized_before_any_subordinate_join() -> None:
    store, preview = _fixture()
    marker = "private-root-row-marker"

    def malformed_job(_owner_id: str, _job_id: str) -> ControlJobRecord:
        return ControlJobRecord.model_validate(
            {"owner_id": OWNER, "job_id": marker, "state": "not-a-state"}
        )

    store.get_job_for_owner = malformed_job  # type: ignore[method-assign]

    with pytest.raises(ReviewProjectionUnavailableError) as caught:
        _service(store, preview).get(owner_id=OWNER, job_id=JOB_ID)

    assert caught.value.__cause__ is None
    assert marker not in str(caught.value)
    assert preview.calls == 0


def test_stale_boundary_disables_approval_and_enables_real_refresh_command() -> None:
    store, preview = _fixture()
    now = store.pricing.fresh_until

    result = _service(store, preview, now=now).get(owner_id=OWNER, job_id=JOB_ID)

    assert result.economics.readiness is EconomicsReadiness.STALE
    assert _enabled(result) == {
        SellerAction.EDIT_LISTING,
        SellerAction.CANCEL_JOB,
        SellerAction.REFRESH_ECONOMICS,
    }
    approval = next(item for item in result.actions if item.action is SellerAction.APPROVE_REVIEW)
    assert approval.reason is ActionReason.ECONOMICS_STALE


def test_one_hostile_persisted_mockup_hides_the_entire_set_and_blocks_approval() -> None:
    store, preview = _fixture()
    hostile = ProductMockupEvidence.model_construct(
        url="https://images.printify.com.evil.test/leak.png",
        position="front",
        variant_ids=(),
    )
    store.sync = store.sync.model_copy(update={"mockups": (*store.sync.mockups, hostile)})
    store.sync = store.sync.model_copy(
        update={"fingerprint": _product_sync_fingerprint(store.sync)}
    )
    store.job = store.job.model_copy(
        update={
            "product_sync_fingerprint": store.sync.fingerprint,
            "pricing_snapshot_id": None,
            "pricing_snapshot_fingerprint": None,
        }
    )

    result = _service(store, preview).get(owner_id=OWNER, job_id=JOB_ID)

    assert result.mockups.readiness is SectionReadiness.UNAVAILABLE
    assert result.mockups.items == ()
    approval = next(item for item in result.actions if item.action is SellerAction.APPROVE_REVIEW)
    assert approval.reason is ActionReason.MOCKUPS_NOT_READY


def test_invalid_preview_grant_never_exposes_a_storage_reference() -> None:
    store, preview = _fixture()
    preview.grant = PreviewGrant(
        url="https://evil.test/private/source.png",
        expires_at=NOW + timedelta(minutes=1),
        source_artifact_fingerprint=SOURCE_FP,
    )

    result = _service(store, preview).get(owner_id=OWNER, job_id=JOB_ID)

    assert result.preview.readiness is SectionReadiness.UNAVAILABLE
    assert result.preview.url is None
    assert result.preview.expires_at is None


@pytest.mark.parametrize(
    ("url", "expires_at"),
    (
        (42, NOW + timedelta(minutes=1)),
        (
            "https://review.mr-lister.test/v1/jobs/job_projection/artwork-preview"
            "?grant=opaque_preview_grant_12345",
            "not-a-timestamp",
        ),
    ),
)
def test_malformed_preview_grant_types_fail_closed(url: object, expires_at: object) -> None:
    store, preview = _fixture()
    preview.grant = PreviewGrant(  # type: ignore[arg-type]
        url=url,
        expires_at=expires_at,
        source_artifact_fingerprint=SOURCE_FP,
    )

    result = _service(store, preview).get(owner_id=OWNER, job_id=JOB_ID)

    assert result.preview.readiness is SectionReadiness.UNAVAILABLE
    assert result.preview.url is None
    assert result.preview.expires_at is None


def test_revision_keeps_strands_provenance_but_marks_prior_product_evidence_outdated() -> None:
    store, preview = _fixture()
    current = store.reviews[1]
    review2 = current.model_copy(
        update={
            "review_version": 2,
            "actor": ReviewActor.SELLER,
            "title": "Seller Edited Geometric Badger Tee",
        }
    )
    review2 = review2.model_copy(update={"fingerprint": _review_content_fingerprint(review2)})
    store.reviews[2] = review2
    store.job = store.job.model_copy(
        update={
            "state": ControlJobState.NEEDS_REVISION,
            "review_version": 2,
            "review_fingerprint": review2.fingerprint,
            "pricing_snapshot_id": None,
            "pricing_snapshot_fingerprint": None,
        }
    )

    result = _service(store, preview).get(owner_id=OWNER, job_id=JOB_ID)

    assert result.strands.prepared_review_version == 1
    assert result.synchronization.readiness is SectionReadiness.OUTDATED
    assert result.mockups.readiness is SectionReadiness.OUTDATED
    assert result.economics.readiness is EconomicsReadiness.MISSING
    assert _enabled(result) == {SellerAction.EDIT_LISTING, SellerAction.CANCEL_JOB}


def test_mismatched_join_fails_closed_without_raw_internal_value() -> None:
    store, preview = _fixture()
    store.analysis = store.analysis.model_copy(update={"fingerprint": "c" * 64})

    with pytest.raises(ReviewProjectionUnavailableError) as caught:
        _service(store, preview).get(owner_id=OWNER, job_id=JOB_ID)

    assert caught.value.code == "PROJECTION_UNAVAILABLE"
    assert "cccc" not in str(caught.value)


def test_mutated_artwork_analysis_under_old_fingerprint_fails_closed() -> None:
    store, preview = _fixture()
    store.analysis = store.analysis.model_copy(
        update={
            "analysis": store.analysis.analysis.model_copy(
                update={"subject": "private-mutated-analysis-marker"}
            )
        }
    )

    with pytest.raises(ReviewProjectionUnavailableError) as caught:
        _service(store, preview).get(owner_id=OWNER, job_id=JOB_ID)

    assert "private-mutated-analysis-marker" not in str(caught.value)


def test_mutated_review_or_sync_under_old_fingerprint_fails_closed() -> None:
    store, preview = _fixture()
    store.reviews[1] = store.reviews[1].model_copy(update={"title": "private-mutated-title-marker"})
    with pytest.raises(ReviewProjectionUnavailableError) as review_error:
        _service(store, preview).get(owner_id=OWNER, job_id=JOB_ID)
    assert "private-mutated-title-marker" not in str(review_error.value)

    store, preview = _fixture()
    changed = store.sync.mockups[0].model_copy(
        update={"url": "https://images.printify.com/private-mutated-mockup.png"}
    )
    store.sync = store.sync.model_copy(update={"mockups": (changed, *store.sync.mockups[1:])})
    with pytest.raises(ReviewProjectionUnavailableError) as sync_error:
        _service(store, preview).get(owner_id=OWNER, job_id=JOB_ID)
    assert "private-mutated-mockup" not in str(sync_error.value)


def test_corrupt_listing_contract_does_not_chain_or_expose_raw_seller_text() -> None:
    store, preview = _fixture()
    marker = "private-seller-marker"
    review = store.reviews[1].model_copy(update={"tags": (marker * 10, *VALID_TAGS[1:])})
    store.reviews[1] = review

    with pytest.raises(ReviewProjectionUnavailableError) as caught:
        _service(store, preview).get(owner_id=OWNER, job_id=JOB_ID)

    assert caught.value.__cause__ is None
    assert marker not in str(caught.value)


def test_projection_output_validation_never_exposes_valid_private_review_content() -> None:
    store, preview = _fixture()
    marker = "private-audience-marker"
    review = store.reviews[1].model_copy(update={"audience": (marker,) * 21})
    review = review.model_copy(update={"fingerprint": _review_content_fingerprint(review)})
    store.reviews[1] = review
    store.job = store.job.model_copy(update={"review_fingerprint": review.fingerprint})

    with pytest.raises(ReviewProjectionUnavailableError) as caught:
        _service(store, preview).get(owner_id=OWNER, job_id=JOB_ID)

    assert caught.value.__cause__ is None
    assert marker not in str(caught.value)


def test_cross_owner_or_nondeterministic_active_work_fails_closed() -> None:
    store, preview = _fixture()
    work = _active_work(store, WorkType.SYNCHRONIZE_PRODUCT)
    store.work = work.model_copy(update={"owner_id": OTHER_OWNER})
    store.job = store.job.model_copy(
        update={
            "state": ControlJobState.PRODUCT_DRAFT_SYNCING,
            "active_work_request_id": work.work_request_id,
            "pricing_snapshot_id": None,
            "pricing_snapshot_fingerprint": None,
        }
    )
    with pytest.raises(ReviewProjectionUnavailableError):
        _service(store, preview).get(owner_id=OWNER, job_id=JOB_ID)

    store.work = work.model_copy(update={"input_fingerprint": "f" * 64})
    with pytest.raises(ReviewProjectionUnavailableError):
        _service(store, preview).get(owner_id=OWNER, job_id=JOB_ID)


def test_stale_failure_pointer_outside_a_failure_state_fails_closed() -> None:
    store, preview = _fixture()
    store.failure = FailureRecord(
        failure_id="failure_stale_projection",
        job_id=JOB_ID,
        work_request_id="work_stale_failure",
        stage=ControlJobState.PRODUCT_DRAFT_SYNCING,
        code="PRODUCTION_UNAVAILABLE",
        retryable=True,
        recovery_action=RecoveryAction.RETRY_PRODUCT_SYNC,
        resume_state=ControlJobState.PRODUCT_DRAFT_SYNCING,
        work_type=WorkType.SYNCHRONIZE_PRODUCT,
        occurred_at=NOW,
    )
    store.job = store.job.model_copy(update={"failure_id": store.failure.failure_id})

    with pytest.raises(ReviewProjectionUnavailableError):
        _service(store, preview).get(owner_id=OWNER, job_id=JOB_ID)


def test_corrupt_retry_binding_is_never_advertised() -> None:
    store, preview = _fixture()
    failure = FailureRecord(
        failure_id="failure_corrupt_binding",
        job_id=JOB_ID,
        work_request_id="work_corrupt_binding",
        stage=ControlJobState.PRODUCT_DRAFT_SYNCING,
        code="PRODUCTION_UNAVAILABLE",
        retryable=True,
        recovery_action=RecoveryAction.RETRY_PRODUCT_SYNC,
        resume_state=ControlJobState.PRODUCT_DRAFT_SYNCING,
        work_type=WorkType.SYNCHRONIZE_PRODUCT,
        occurred_at=NOW,
    )
    store.failure = failure.model_copy(update={"recovery_action": RecoveryAction.RETRY_PREPARATION})
    store.job = store.job.model_copy(
        update={
            "state": ControlJobState.FAILED_RETRYABLE,
            "failure_id": failure.failure_id,
            "active_work_request_id": None,
        }
    )

    with pytest.raises(ReviewProjectionUnavailableError):
        _service(store, preview).get(owner_id=OWNER, job_id=JOB_ID)


def test_terminal_review_without_agent_evidence_is_unavailable_not_pending() -> None:
    store, preview = _fixture()
    store.job = store.job.model_copy(
        update={
            "state": ControlJobState.CANCELLED,
            "cancellation_requested_at": NOW,
            "agent_evidence_id": None,
            "agent_evidence_fingerprint": None,
        }
    )

    result = _service(store, preview).get(owner_id=OWNER, job_id=JOB_ID)

    assert result.strands.readiness is SectionReadiness.UNAVAILABLE


def test_provider_retry_cannot_advertise_without_prior_strands_evidence() -> None:
    store, preview = _fixture()
    store.failure = FailureRecord(
        failure_id="failure_missing_strands",
        job_id=JOB_ID,
        work_request_id="work_missing_strands",
        stage=ControlJobState.PRODUCT_DRAFT_SYNCING,
        code="PRODUCTION_UNAVAILABLE",
        retryable=True,
        recovery_action=RecoveryAction.RETRY_PRODUCT_SYNC,
        resume_state=ControlJobState.PRODUCT_DRAFT_SYNCING,
        work_type=WorkType.SYNCHRONIZE_PRODUCT,
        occurred_at=NOW,
    )
    store.job = store.job.model_copy(
        update={
            "state": ControlJobState.FAILED_RETRYABLE,
            "failure_id": store.failure.failure_id,
            "agent_evidence_id": None,
            "agent_evidence_fingerprint": None,
        }
    )

    with pytest.raises(ReviewProjectionUnavailableError):
        _service(store, preview).get(owner_id=OWNER, job_id=JOB_ID)


def test_pre_agent_retry_remains_visible_before_strands_evidence_exists() -> None:
    store, preview = _fixture()
    store.failure = FailureRecord(
        failure_id="failure_pre_agent",
        job_id=JOB_ID,
        work_request_id="work_pre_agent",
        stage=ControlJobState.INTAKE_VALIDATED,
        code="INTELLIGENCE_UNAVAILABLE",
        retryable=True,
        recovery_action=RecoveryAction.RETRY_PREPARATION,
        resume_state=ControlJobState.ANALYZING_ARTWORK,
        work_type=WorkType.PREPARE,
        occurred_at=NOW,
    )
    store.job = store.job.model_copy(
        update={
            "state": ControlJobState.FAILED_RETRYABLE,
            "failure_id": store.failure.failure_id,
            "agent_evidence_id": None,
            "agent_evidence_fingerprint": None,
        }
    )

    result = _service(store, preview).get(owner_id=OWNER, job_id=JOB_ID)

    assert result.failure is not None
    assert result.failure.stage is ReviewStage.UPLOAD_VERIFIED
    assert result.strands.readiness is SectionReadiness.PENDING
    assert _enabled(result) == {SellerAction.RETRY_JOB, SellerAction.CANCEL_JOB}


def test_mutated_strands_provenance_under_old_fingerprint_fails_closed() -> None:
    store, preview = _fixture()
    store.agent = store.agent.model_copy(update={"correlation_id": "8" * 24})

    with pytest.raises(ReviewProjectionUnavailableError):
        _service(store, preview).get(owner_id=OWNER, job_id=JOB_ID)


def test_refresh_economics_is_not_advertised_for_an_invalid_review() -> None:
    store, preview = _fixture()
    review = store.reviews[1].model_copy(
        update={
            "tags": ("badger portrait", "badger token", *VALID_TAGS[2:]),
            "validation_passed": False,
            "validation_issue_codes": ("TAG_KEYWORD_REPETITION",),
        }
    )
    review = review.model_copy(update={"fingerprint": _review_content_fingerprint(review)})
    store.reviews[1] = review
    store.job = store.job.model_copy(
        update={
            "review_fingerprint": review.fingerprint,
            "review_validated": False,
            "pricing_snapshot_id": None,
            "pricing_snapshot_fingerprint": None,
        }
    )

    result = _service(store, preview).get(owner_id=OWNER, job_id=JOB_ID)

    assert SellerAction.REFRESH_ECONOMICS not in _enabled(result)


def test_pricing_refresh_requires_product_sync_for_the_exact_current_review() -> None:
    store, preview = _fixture()
    current = store.reviews[1]
    review2 = current.model_copy(
        update={
            "review_version": 2,
            "actor": ReviewActor.SELLER,
            "title": "Current seller revision",
        }
    )
    review2 = review2.model_copy(update={"fingerprint": _review_content_fingerprint(review2)})
    store.reviews[2] = review2
    store.job = store.job.model_copy(
        update={
            "state": ControlJobState.PRICING_REFRESHING,
            "review_version": 2,
            "review_fingerprint": review2.fingerprint,
            "pricing_snapshot_id": None,
            "pricing_snapshot_fingerprint": None,
        }
    )
    store.work = _active_work(store, WorkType.REFRESH_ECONOMICS)
    store.job = store.job.model_copy(update={"active_work_request_id": store.work.work_request_id})

    with pytest.raises(ReviewProjectionUnavailableError):
        _service(store, preview).get(owner_id=OWNER, job_id=JOB_ID)


def test_serialized_projection_contains_no_private_or_orchestration_fields() -> None:
    store, preview = _fixture()
    payload = _service(store, preview).get(owner_id=OWNER, job_id=JOB_ID).model_dump(mode="json")

    forbidden = {
        "owner_id",
        "bucket",
        "object_key",
        "version_id",
        "content_sha256",
        "source_artifact_fingerprint",
        "work_request_id",
        "execution_arn",
        "receipt_id",
        "attempt_id",
        "permit",
        "image_id",
        "variant_id",
        "input_tokens",
        "output_tokens",
        "total_tokens",
        "raw_response",
        "secret",
        "authorization",
        "shop_id",
        "product_sync_fingerprint",
        "pricing_snapshot_id",
        "pricing_snapshot_fingerprint",
        "payload_fingerprint",
        "response_fingerprint",
        "controller_model_id",
        "title_rationale",
        "tag_rationale",
    }

    def keys(value: object) -> set[str]:
        if isinstance(value, dict):
            return set(value).union(*(keys(item) for item in value.values()))
        if isinstance(value, list):
            return set().union(*(keys(item) for item in value))
        return set()

    assert keys(payload).isdisjoint(forbidden)
    action_values = {item["action"] for item in payload["actions"]}
    assert action_values == {item.value for item in SellerAction}


@pytest.mark.parametrize(
    ("state", "work_type", "display", "stage", "enabled"),
    (
        (
            ControlJobState.INTAKE_VALIDATED,
            WorkType.PREPARE,
            ReviewDisplayState.PREPARING,
            ReviewStage.UPLOAD_VERIFIED,
            {SellerAction.CANCEL_JOB},
        ),
        (
            ControlJobState.ANALYZING_ARTWORK,
            WorkType.PREPARE,
            ReviewDisplayState.PREPARING,
            ReviewStage.ARTWORK_REVIEW,
            {SellerAction.CANCEL_JOB},
        ),
        (
            ControlJobState.LISTING_DRAFTED,
            WorkType.PREPARE,
            ReviewDisplayState.PREPARING,
            ReviewStage.LISTING_VALIDATION,
            {SellerAction.CANCEL_JOB},
        ),
        (
            ControlJobState.PRODUCT_DRAFT_SYNCING,
            WorkType.SYNCHRONIZE_PRODUCT,
            ReviewDisplayState.SYNCHRONIZING,
            ReviewStage.PRODUCT_SYNC,
            {SellerAction.CANCEL_JOB},
        ),
        (
            ControlJobState.PRICING_REFRESHING,
            WorkType.REFRESH_ECONOMICS,
            ReviewDisplayState.REFRESHING_ESTIMATE,
            ReviewStage.ECONOMICS_REFRESH,
            {SellerAction.CANCEL_JOB},
        ),
        (
            ControlJobState.RECONCILIATION_REQUIRED,
            WorkType.RECONCILE_PRODUCT,
            ReviewDisplayState.RECONCILING,
            ReviewStage.PROVIDER_RECONCILIATION,
            {SellerAction.CANCEL_JOB},
        ),
        (
            ControlJobState.CANCEL_REQUESTED,
            WorkType.SYNCHRONIZE_PRODUCT,
            ReviewDisplayState.CANCELLING,
            ReviewStage.CANCELLATION,
            set(),
        ),
    ),
)
def test_machine_state_matrix_has_only_server_derived_capabilities(
    state: ControlJobState,
    work_type: WorkType,
    display: ReviewDisplayState,
    stage: ReviewStage,
    enabled: set[SellerAction],
) -> None:
    store, preview = _fixture()
    work = _active_work(store, work_type)
    updates: dict[str, object] = {
        "state": state,
        "active_work_request_id": work.work_request_id,
    }
    if state in {
        ControlJobState.INTAKE_VALIDATED,
        ControlJobState.ANALYZING_ARTWORK,
    }:
        updates.update(
            {
                "review_version": 0,
                "review_fingerprint": None,
                "review_validated": False,
                "artwork_analysis_id": None,
                "artwork_analysis_fingerprint": None,
                "agent_evidence_id": None,
                "agent_evidence_fingerprint": None,
                "product_id": None,
                "provider_payload_fingerprint": None,
                "product_sync_id": None,
                "synchronized_review_version": None,
                "product_sync_fingerprint": None,
                "pricing_snapshot_id": None,
                "pricing_snapshot_fingerprint": None,
            }
        )
        work = work.model_copy(update={"review_version": None})
    elif state is ControlJobState.LISTING_DRAFTED:
        updates.update(
            {
                "agent_evidence_id": None,
                "agent_evidence_fingerprint": None,
                "product_id": None,
                "provider_payload_fingerprint": None,
                "product_sync_id": None,
                "synchronized_review_version": None,
                "product_sync_fingerprint": None,
                "pricing_snapshot_id": None,
                "pricing_snapshot_fingerprint": None,
            }
        )
    elif state in {
        ControlJobState.PRODUCT_DRAFT_SYNCING,
        ControlJobState.PRICING_REFRESHING,
        ControlJobState.RECONCILIATION_REQUIRED,
        ControlJobState.CANCEL_REQUESTED,
    }:
        updates.update({"pricing_snapshot_id": None, "pricing_snapshot_fingerprint": None})
    if state is ControlJobState.CANCEL_REQUESTED:
        updates["cancellation_requested_at"] = NOW
    store.work = work
    store.job = store.job.model_copy(update=updates)

    result = _service(store, preview).get(owner_id=OWNER, job_id=JOB_ID)

    assert result.display_state is display
    assert result.stage is stage
    assert _enabled(result) == enabled


@pytest.mark.parametrize(
    ("state", "retryable", "enabled"),
    (
        (
            ControlJobState.FAILED_RETRYABLE,
            True,
            {SellerAction.RETRY_JOB, SellerAction.CANCEL_JOB},
        ),
        (ControlJobState.FAILED_TERMINAL, False, set()),
    ),
)
def test_failure_projection_uses_only_sanitized_codes_and_persisted_recovery(
    state: ControlJobState,
    retryable: bool,
    enabled: set[SellerAction],
) -> None:
    store, preview = _fixture()
    store.failure = FailureRecord(
        failure_id="failure_projection",
        job_id=JOB_ID,
        work_request_id="work_failed_projection",
        stage=ControlJobState.PRODUCT_DRAFT_SYNCING,
        code="PRODUCTION_UNAVAILABLE" if retryable else "UNKNOWN_PRIVATE_EXCEPTION",
        retryable=retryable,
        recovery_action=RecoveryAction.RETRY_PRODUCT_SYNC if retryable else None,
        resume_state=ControlJobState.PRODUCT_DRAFT_SYNCING if retryable else None,
        work_type=WorkType.SYNCHRONIZE_PRODUCT if retryable else None,
        occurred_at=NOW,
    )
    store.job = store.job.model_copy(
        update={
            "state": state,
            "failure_id": store.failure.failure_id,
            "active_work_request_id": None,
        }
    )

    result = _service(store, preview).get(owner_id=OWNER, job_id=JOB_ID)

    assert _enabled(result) == enabled
    assert result.failure is not None
    assert result.failure.retryable is retryable
    if retryable:
        assert result.failure.code == "PRODUCTION_UNAVAILABLE"
    else:
        assert result.failure.code == "WORKFLOW_FAILURE"
        assert "PRIVATE" not in result.failure.message


def test_repeated_tag_keywords_return_deterministic_exact_paths() -> None:
    store, preview = _fixture()
    review = store.reviews[1]
    tags = ("badger portrait", "badger token", *review.tags[2:])
    invalid = review.model_copy(
        update={
            "tags": tags,
            "validation_passed": False,
            "validation_issue_codes": ("TAG_KEYWORD_REPETITION",),
        }
    )
    invalid = invalid.model_copy(update={"fingerprint": _review_content_fingerprint(invalid)})
    store.reviews[1] = invalid
    store.job = store.job.model_copy(
        update={
            "state": ControlJobState.NEEDS_REVISION,
            "review_fingerprint": invalid.fingerprint,
            "review_validated": False,
            "product_id": None,
            "provider_payload_fingerprint": None,
            "product_sync_id": None,
            "synchronized_review_version": None,
            "product_sync_fingerprint": None,
            "pricing_snapshot_id": None,
            "pricing_snapshot_fingerprint": None,
        }
    )

    result = _service(store, preview).get(owner_id=OWNER, job_id=JOB_ID)

    assert result.validation.passed is False
    assert tuple(item.path for item in result.validation.issues) == ("tags[1]", "tags[2]")
    assert SellerAction.APPROVE_REVIEW not in _enabled(result)


def test_wrong_active_work_type_fails_closed() -> None:
    store, preview = _fixture()
    store.work = _active_work(store, WorkType.PREPARE)
    store.job = store.job.model_copy(
        update={
            "state": ControlJobState.PRODUCT_DRAFT_SYNCING,
            "active_work_request_id": store.work.work_request_id,
            "pricing_snapshot_id": None,
            "pricing_snapshot_fingerprint": None,
        }
    )

    with pytest.raises(ReviewProjectionUnavailableError):
        _service(store, preview).get(owner_id=OWNER, job_id=JOB_ID)


@pytest.mark.parametrize(
    ("state", "display", "stage", "enabled"),
    (
        (
            ControlJobState.NEEDS_REVISION,
            ReviewDisplayState.NEEDS_REVISION,
            ReviewStage.SELLER_REVISION,
            {SellerAction.EDIT_LISTING, SellerAction.CANCEL_JOB},
        ),
        (
            ControlJobState.CANCELLED,
            ReviewDisplayState.CANCELLED,
            ReviewStage.COMPLETE,
            set(),
        ),
        (
            ControlJobState.APPROVED,
            ReviewDisplayState.APPROVED,
            ReviewStage.COMPLETE,
            set(),
        ),
    ),
)
def test_terminal_and_human_state_capabilities_are_server_derived(
    state: ControlJobState,
    display: ReviewDisplayState,
    stage: ReviewStage,
    enabled: set[SellerAction],
) -> None:
    store, preview = _fixture()
    updates: dict[str, object] = {"state": state}
    if state is ControlJobState.CANCELLED:
        updates["cancellation_requested_at"] = NOW
    if state is ControlJobState.APPROVED:
        updates.update(
            {
                "approved_review_version": 1,
                "approved_review_fingerprint": store.job.review_fingerprint,
                "approval_fingerprint": review_etag(
                    job_id=JOB_ID,
                    review_version=1,
                    review_fingerprint=store.job.review_fingerprint,
                    product_id=store.job.product_id,
                    product_sync_fingerprint=store.sync.fingerprint,
                    pricing_snapshot_id=store.pricing.snapshot_id,
                    pricing_snapshot_fingerprint=store.pricing.fingerprint,
                ),
            }
        )
    store.job = store.job.model_copy(update=updates)

    result = _service(store, preview).get(owner_id=OWNER, job_id=JOB_ID)

    assert result.display_state is display
    assert result.stage is stage
    assert _enabled(result) == enabled
