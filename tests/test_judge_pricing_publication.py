"""Judge commercial rules at the authoritative review and publication boundaries."""

from copy import deepcopy

import pytest

from mr_lister.control.judge_pricing import JudgePricingPolicy
from mr_lister.control.models import ControlJobState, WorkType
from mr_lister.control.pricing import ReviewPricing
from mr_lister.control.projection import SellerReviewProjectionService
from mr_lister.control.projection_models import ActionReason, SellerAction
from mr_lister.publication.contract import PublicationState
from mr_lister.publication.errors import PublicationAuthorityError, PublicationErrorCode
from mr_lister.publication.service import PublicationRequestService
from mr_lister.publication.store import InMemoryPublicationStore
from mr_lister.review_profile import ExactReviewProductProfile
from tests import test_phase6_review_projection as projection_fixture
from tests import test_phase71_publication_service as publication_fixture


def publication_service(authority, profile, *, policy):
    store = InMemoryPublicationStore((authority,))
    service = PublicationRequestService(
        store=store,
        profiles=publication_fixture.ProfileAuthority(profile),
        profile_eligibility=publication_fixture.profile_eligibility_authority(profile),
        release_manifest_fingerprint=publication_fixture.RELEASE_FINGERPRINT,
        clock=lambda: publication_fixture.NOW,
        judge_pricing_policy=policy,
    )
    return store, service


def persisted_rows(store):
    return deepcopy({name: value for name, value in vars(store).items() if name != "_lock"})


@pytest.mark.parametrize(
    "pricing",
    [None, ReviewPricing(retail_price_cents=3500, free_shipping=True)],
    ids=["legacy-unrecorded-shipping", "approved-free-shipping"],
)
def test_existing_approved_judge_listing_cannot_request_publication_with_unsafe_shipping(
    pricing,
):
    authority, profile = publication_fixture._authority(review_pricing=pricing)
    assert authority.current_job.state is ControlJobState.APPROVED
    store, service = publication_service(
        authority, profile, policy=JudgePricingPolicy(owner_id=publication_fixture.OWNER_ID)
    )
    before = persisted_rows(store)

    with pytest.raises(PublicationAuthorityError, match="standard shipping") as failure:
        service.request_publication(publication_fixture._command(authority))

    assert failure.value.code is PublicationErrorCode.INVALID_AUTHORITY
    assert persisted_rows(store) == before
    assert not store.aggregates and not store.work_requests and not store.receipts


def test_judge_compliant_pricing_can_create_one_intent_without_provider_execution():
    pricing = ReviewPricing(retail_price_cents=3500, free_shipping=False)
    authority, profile = publication_fixture._authority(review_pricing=pricing)
    store, service = publication_service(
        authority, profile, policy=JudgePricingPolicy(owner_id=publication_fixture.OWNER_ID)
    )

    response = service.request_publication(publication_fixture._command(authority))

    assert response.publication_state is PublicationState.PUBLICATION_REQUESTED
    assert len(store.aggregates) == len(store.work_requests) == len(store.receipts) == 1
    assert next(iter(store.attempts.values())).publish_post_call_count == 0
    assert authority.review.pricing == pricing
    assert profile.profile.retail_price_cents == 2999


@pytest.mark.parametrize("price", [3499, 3500])
def test_optional_judge_minimum_checks_effective_variant_prices_without_global_default_change(
    price,
):
    pricing = ReviewPricing(
        retail_price_cents=3500,
        free_shipping=False,
        variant_prices=({"color": "Black", "size": "S", "retail_price_cents": price},),
    )
    authority, profile = publication_fixture._authority(review_pricing=pricing)
    store, service = publication_service(
        authority,
        profile,
        policy=JudgePricingPolicy(owner_id=publication_fixture.OWNER_ID, minimum_price_cents=3500),
    )
    before = persisted_rows(store)
    if price < 3500:
        with pytest.raises(PublicationAuthorityError, match="at least"):
            service.request_publication(publication_fixture._command(authority))
        assert persisted_rows(store) == before
    else:
        service.request_publication(publication_fixture._command(authority))
        assert len(store.receipts) == 1


def test_judge_default_does_not_become_an_implicit_minimum():
    pricing = ReviewPricing(retail_price_cents=2999, free_shipping=False)
    authority, profile = publication_fixture._authority(review_pricing=pricing)
    store, service = publication_service(
        authority, profile, policy=JudgePricingPolicy(owner_id=publication_fixture.OWNER_ID)
    )
    service.request_publication(publication_fixture._command(authority))
    assert len(store.receipts) == 1


@pytest.mark.parametrize(
    "pricing", [None, ReviewPricing(retail_price_cents=2999, free_shipping=True)]
)
def test_other_owners_keep_their_existing_free_shipping_publication_behavior(pricing):
    authority, profile = publication_fixture._authority(review_pricing=pricing)
    store, service = publication_service(
        authority, profile, policy=JudgePricingPolicy(owner_id=publication_fixture.OTHER_OWNER_ID)
    )
    response = service.request_publication(publication_fixture._command(authority))
    assert response.publication_state is PublicationState.PUBLICATION_REQUESTED
    assert len(store.receipts) == 1


def project(store, preview, *, policy):
    service = SellerReviewProjectionService(
        store=store,
        profiles=projection_fixture.FakeProfiles(
            ExactReviewProductProfile(
                profile=projection_fixture.PROFILE, fingerprint=projection_fixture.PROFILE_FP
            )
        ),
        clock=lambda: projection_fixture.NOW,
        preview_issuer=preview,
        preview_origin="https://review.mr-lister.test",
        judge_pricing_policy=policy,
    )
    return service.get(owner_id=projection_fixture.OWNER, job_id=projection_fixture.JOB_ID)


def initial_job(store):
    work = projection_fixture._active_work(store, WorkType.PREPARE)
    store.work = work.model_copy(update={"review_version": None})
    store.job = store.job.model_copy(
        update={
            "state": ControlJobState.INTAKE_VALIDATED,
            "active_work_request_id": work.work_request_id,
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


@pytest.mark.parametrize("judge_owner", [True, False])
def test_initial_projection_uses_judge_35_paid_shipping_without_changing_owner_defaults(
    judge_owner,
):
    store, preview = projection_fixture._fixture()
    initial_job(store)
    before = store.job.model_dump(mode="json")
    result = project(
        store,
        preview,
        policy=JudgePricingPolicy(
            owner_id=projection_fixture.OWNER if judge_owner else projection_fixture.OTHER_OWNER
        ),
    )
    expected = (
        ReviewPricing(retail_price_cents=3500, free_shipping=False)
        if judge_owner
        else ReviewPricing(
            retail_price_cents=projection_fixture.PROFILE.retail_price_cents,
            free_shipping=projection_fixture.PROFILE.buyer_shipping_cents == 0,
        )
    )
    assert result.product_policy.pricing == expected
    assert result.product_policy.retail_price_cents == expected.retail_price_cents
    assert result.product_policy.pricing_saved is False
    assert store.job.model_dump(mode="json") == before
    assert "get_review" not in store.calls
    if not judge_owner:
        assert result == projection_fixture._service(store, preview).get(
            owner_id=projection_fixture.OWNER, job_id=projection_fixture.JOB_ID
        )


@pytest.mark.parametrize("judge_owner", [True, False])
@pytest.mark.parametrize(
    "pricing", [None, ReviewPricing(retail_price_cents=2999, free_shipping=True)]
)
def test_saved_free_shipping_projection_stays_honest_while_only_judge_approval_is_disabled(
    judge_owner, pricing
):
    store, preview = projection_fixture._fixture(review_pricing=pricing)
    before = deepcopy(store.reviews)
    result = project(
        store,
        preview,
        policy=JudgePricingPolicy(
            owner_id=projection_fixture.OWNER if judge_owner else projection_fixture.OTHER_OWNER
        ),
    )
    assert result.product_policy.pricing.free_shipping is True
    assert result.product_policy.pricing.retail_price_cents == 2999
    assert result.product_policy.pricing_saved is (pricing is not None)
    approval = next(row for row in result.actions if row.action is SellerAction.APPROVE_REVIEW)
    assert approval.enabled is not judge_owner
    if judge_owner:
        assert approval.reason is ActionReason.REVIEW_INVALID
        assert "standard shipping" in approval.message
    assert next(row for row in result.actions if row.action is SellerAction.EDIT_LISTING).enabled
    assert store.reviews == before
    if not judge_owner:
        assert result == projection_fixture._service(store, preview).get(
            owner_id=projection_fixture.OWNER, job_id=projection_fixture.JOB_ID
        )


def test_compliant_judge_projection_preserves_the_original_approval_gate():
    pricing = ReviewPricing(retail_price_cents=3500, free_shipping=False)
    store, preview = projection_fixture._fixture(review_pricing=pricing)
    result = project(store, preview, policy=JudgePricingPolicy(owner_id=projection_fixture.OWNER))
    assert result.product_policy.pricing == pricing
    assert next(row for row in result.actions if row.action is SellerAction.APPROVE_REVIEW).enabled
