from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from mr_lister.agent.contracts import PreparationRequest
from mr_lister.cloud.browser_contracts import ListingRequest
from mr_lister.control.commands import ListingRevision
from mr_lister.control.errors import (
    IdempotencyConflictError,
    InvalidControlStateError,
    NotFoundError,
    StaleReviewError,
)
from mr_lister.control.fingerprints import (
    canonical_fingerprint,
    review_content_fingerprint,
    review_etag,
)
from mr_lister.control.models import ControlJobState, ReviewActor, ReviewContent
from mr_lister.control.pricing import (
    ReviewPricing,
    VariantPrice,
    effective_review_pricing,
    retail_price_for_variant,
)
from mr_lister.control.projection import ReviewProjectionUnavailableError
from mr_lister.control.projection_models import SectionReadiness
from mr_lister.control.service import SellerControlService
from mr_lister.control.store import InMemorySellerControlStore
from tests.test_phase6_cloud_api import (
    REVIEW_ETAG,
    CommandSpy,
    api_event,
    command_adapter,
    review_authority_body,
)
from tests.test_phase6_control_service import (
    NOW,
    OTHER_OWNER,
    OWNER,
    VALID_TAGS,
    revision_command,
    seed_reviewable,
)
from tests.test_phase6_review_projection import JOB_ID, PROFILE, _fixture, _service
from tests.test_phase6_strands_runtime import JOB_ID as PREPARATION_JOB_ID
from tests.test_phase6_strands_runtime import durable_worker_runtime


def settings(*, retail: int = 3199, free: bool = True, overrides=()) -> ReviewPricing:
    return ReviewPricing(retail_price_cents=retail, variant_prices=overrides, free_shipping=free)


@pytest.mark.parametrize("value", [0, -1, 1_000_000, True, 2999.5, "2999"])
def test_price_requires_bounded_integer_cents(value) -> None:
    with pytest.raises(ValidationError):
        settings(retail=value)
    with pytest.raises(ValidationError):
        VariantPrice(color="Black", size="S", retail_price_cents=value)


@pytest.mark.parametrize("value", [0, 1, "true", None])
def test_free_shipping_is_a_real_boolean(value) -> None:
    with pytest.raises(ValidationError):
        settings(free=value)


def test_overrides_are_sparse_unique_canonical_and_profile_bound() -> None:
    black = VariantPrice(color=PROFILE.colors[0], size=PROFILE.sizes[0], retail_price_cents=3599)
    white = VariantPrice(color=PROFILE.colors[-1], size=PROFILE.sizes[-1], retail_price_cents=3799)
    first = settings(overrides=(black, white))
    second = settings(overrides=(white, black))
    assert canonical_fingerprint(first) == canonical_fingerprint(second)
    assert effective_review_pricing(first, PROFILE) == first
    assert retail_price_for_variant(first, color=black.color, size=black.size) == 3599
    assert retail_price_for_variant(first, color=PROFILE.colors[0], size=PROFILE.sizes[1]) == 3199
    assert effective_review_pricing(None, PROFILE) == settings(retail=2999)
    with pytest.raises(ValidationError, match="unique"):
        settings(overrides=(black, black))
    with pytest.raises(ValueError, match="selected product"):
        effective_review_pricing(
            settings(overrides=(VariantPrice(color="Unknown", size="S", retail_price_cents=3199),)),
            PROFILE,
        )


def test_legacy_review_fingerprint_and_request_payload_remain_byte_compatible() -> None:
    store, _ = _fixture()
    review = store.reviews[1]
    payload = review.model_dump(mode="json", exclude={"fingerprint"})
    assert "pricing" not in payload
    payload["created_at"] = review.created_at.isoformat()
    assert (
        review_content_fingerprint(review) == canonical_fingerprint(payload) == review.fingerprint
    )
    updated = review.model_copy(update={"pricing": settings()})
    assert review_content_fingerprint(updated) != review.fingerprint
    assert ReviewContent.model_validate_json(updated.model_dump_json()).pricing == settings()
    revision = ListingRevision(title="Title", description="Description", tags=VALID_TAGS)
    assert "pricing" not in revision.model_dump(mode="json")


def _priced_command(job, review, sync, pricing, selected: ReviewPricing):
    command = revision_command(job, review, sync, pricing)
    return command.model_copy(
        update={"revision": command.revision.model_copy(update={"pricing": selected})}
    )


def test_price_only_save_binds_review_invalidates_estimate_and_has_one_replayable_sync() -> None:
    store = InMemorySellerControlStore()
    job, review, sync, pricing = seed_reviewable(store)
    chosen = settings(
        free=False,
        overrides=(VariantPrice(color="Black", size="S", retail_price_cents=3599),),
    )
    command = _priced_command(job, review, sync, pricing, chosen)
    command = command.model_copy(
        update={"revision": command.revision.model_copy(update={"title": review.title})}
    )
    service = SellerControlService(store=store, clock=lambda: NOW)
    result = service.revise_listing(command)
    assert service.revise_listing(command) == result
    saved = store.get_review(job.job_id, 2)
    assert saved.pricing == chosen
    assert saved.product_profile_fingerprint == review.product_profile_fingerprint
    assert saved.title == review.title
    assert saved.fingerprint == review_content_fingerprint(saved)
    assert result.state is ControlJobState.PRODUCT_DRAFT_SYNCING
    current = store.get_job(job.job_id)
    assert current.product_sync_id == sync.sync_id
    assert current.pricing_snapshot_id is None
    assert current.approval_fingerprint is None
    assert len(store.list_reviews(job.job_id)) == 2
    assert len(store.list_review_decisions(job.job_id)) == 1
    changed = command.model_copy(
        update={"revision": command.revision.model_copy(update={"pricing": settings(retail=3999)})}
    )
    with pytest.raises(IdempotencyConflictError):
        service.revise_listing(changed)


def test_listing_only_save_preserves_existing_pricing() -> None:
    store = InMemorySellerControlStore()
    job, review, sync, pricing = seed_reviewable(store)
    chosen = settings()
    review = review.model_copy(update={"pricing": chosen})
    store._reviews[(job.job_id, 1)] = review
    command = revision_command(job, review, sync, pricing)
    SellerControlService(store=store, clock=lambda: NOW).revise_listing(command)
    assert store.get_review(job.job_id, 2).pricing == chosen


def test_unrecognized_variant_cannot_be_saved() -> None:
    store = InMemorySellerControlStore()
    job, review, sync, pricing = seed_reviewable(store)
    command = _priced_command(
        job,
        review,
        sync,
        pricing,
        settings(overrides=(VariantPrice(color="Other", size="S", retail_price_cents=3199),)),
    )
    with pytest.raises(InvalidControlStateError, match="synchronized product"):
        SellerControlService(store=store).revise_listing(command)
    assert store.get_job(job.job_id) == job
    assert len(store.list_reviews(job.job_id)) == 1


@pytest.mark.parametrize("has_override", [False, True])
def test_before_initial_sync_only_item_wide_pricing_can_be_saved(has_override: bool) -> None:
    store = InMemorySellerControlStore()
    job, review, sync, pricing = seed_reviewable(store)
    unsynchronized = job.model_copy(
        update={
            "state": ControlJobState.NEEDS_REVISION,
            "product_id": None,
            "provider_payload_fingerprint": None,
            "product_sync_id": None,
            "product_sync_fingerprint": None,
            "synchronized_review_version": None,
            "pricing_snapshot_id": None,
            "pricing_snapshot_fingerprint": None,
        }
    )
    store._jobs[job.job_id] = type(job).model_validate(unsynchronized.model_dump(mode="python"))
    selected = settings(
        overrides=(VariantPrice(color="Black", size="S", retail_price_cents=3599),)
        if has_override
        else (),
    )
    command = _priced_command(job, review, sync, pricing, selected).model_copy(
        update={
            "expected_review_etag": review_etag(
                job_id=job.job_id,
                review_version=review.review_version,
                review_fingerprint=review.fingerprint,
                product_id=None,
                product_sync_fingerprint=None,
                pricing_snapshot_id=None,
                pricing_snapshot_fingerprint=None,
            )
        }
    )
    service = SellerControlService(store=store, clock=lambda: NOW)
    if has_override:
        with pytest.raises(InvalidControlStateError, match="Wait for product synchronization"):
            service.revise_listing(command)
        assert len(store.list_reviews(job.job_id)) == 1
    else:
        assert service.revise_listing(command).state is ControlJobState.PRODUCT_DRAFT_SYNCING
        assert store.get_review(job.job_id, 2).pricing == selected


@pytest.mark.parametrize("mutation", ["owner", "review_etag", "approved"])
def test_price_save_does_not_bypass_existing_seller_authority(mutation: str) -> None:
    store = InMemorySellerControlStore()
    job, review, sync, pricing = seed_reviewable(store)
    command = _priced_command(job, review, sync, pricing, settings())
    if mutation == "owner":
        command = command.model_copy(update={"owner_id": OTHER_OWNER})
        error = NotFoundError
    elif mutation == "review_etag":
        command = command.model_copy(update={"expected_review_etag": "0" * 64})
        error = StaleReviewError
    else:
        store._jobs[job.job_id] = job.model_copy(update={"state": ControlJobState.APPROVED})
        error = InvalidControlStateError
    with pytest.raises(error):
        SellerControlService(store=store).revise_listing(command)
    assert len(store.list_reviews(job.job_id)) == 1


def test_projection_keeps_prior_sync_bound_to_prior_prices_during_price_revision() -> None:
    store, preview = _fixture()
    prior = store.reviews[1]
    chosen = settings(free=False)
    review = prior.model_copy(
        update={"review_version": 2, "actor": ReviewActor.SELLER, "pricing": chosen}
    )
    review = review.model_copy(update={"fingerprint": review_content_fingerprint(review)})
    store.reviews[2] = review
    store.job = store.job.model_copy(
        update={
            "state": ControlJobState.NEEDS_REVISION,
            "review_version": 2,
            "review_fingerprint": review.fingerprint,
            "pricing_snapshot_id": None,
            "pricing_snapshot_fingerprint": None,
        }
    )
    result = _service(store, preview).get(owner_id=OWNER, job_id=JOB_ID)
    assert result.product_policy.pricing == chosen
    assert result.product_policy.pricing_saved is True
    assert result.product_policy.retail_price_cents == 3199
    assert result.synchronization.readiness is SectionReadiness.OUTDATED
    store.reviews[1] = prior.model_copy(update={"pricing": chosen})
    with pytest.raises(ReviewProjectionUnavailableError):
        _service(store, preview).get(owner_id=OWNER, job_id=JOB_ID)


@pytest.mark.parametrize("free", [True, False])
def test_ready_projection_uses_saved_variant_prices_and_shipping_choice(free: bool) -> None:
    variant = VariantPrice(color=PROFILE.colors[0], size=PROFILE.sizes[0], retail_price_cents=3599)
    chosen = settings(free=free, overrides=(variant,))
    store, preview = _fixture(review_pricing=chosen)
    result = _service(store, preview).get(owner_id=OWNER, job_id=JOB_ID)
    assert result.product_policy.pricing == chosen
    assert result.product_policy.pricing_saved is True
    rows = result.economics.variants
    assert rows[0].retail_price_cents == 3599
    assert all(row.retail_price_cents == 3199 for row in rows[1:])
    assert all(
        row.buyer_shipping_cents == (0 if free else row.production_shipping_cents) for row in rows
    )
    assert ("storefront charge may differ" in " ".join(result.economics.assumptions)) is not free


def test_projection_rejects_shipping_evidence_for_another_saved_shipping_choice() -> None:
    store, preview = _fixture(review_pricing=settings(free=False))
    review = store.reviews[1].model_copy(update={"pricing": settings(free=True)})
    review = review.model_copy(update={"fingerprint": review_content_fingerprint(review)})
    store.reviews[1] = review
    store.job = store.job.model_copy(update={"review_fingerprint": review.fingerprint})
    with pytest.raises(ReviewProjectionUnavailableError):
        _service(store, preview).get(owner_id=OWNER, job_id=JOB_ID)


def test_cloud_adapter_carries_price_choice_under_existing_owner_and_review_authority() -> None:
    spy = CommandSpy()
    chosen = settings(free=False)
    listing = {
        "title": "Seller title",
        "description": "Seller description",
        "tags": list(VALID_TAGS),
        "pricing": chosen.model_dump(mode="json"),
    }
    assert ListingRequest.model_validate_json(json.dumps(listing)).pricing == chosen
    response = command_adapter(spy).handle(
        api_event(
            "PUT /v1/jobs/{job_id}/review/listing",
            body={**review_authority_body(), "listing": listing},
            headers={
                "Content-Type": "application/json",
                "Idempotency-Key": "price-save",
                "If-Match": f'"{REVIEW_ETAG}"',
            },
        )
    )
    assert response["statusCode"] == 200
    assert spy.calls[0][1].revision.pricing == chosen
    listing["pricing"]["owner_id"] = OTHER_OWNER
    with pytest.raises(ValidationError):
        ListingRequest.model_validate_json(json.dumps(listing))


def test_preparation_adapter_persists_explicit_profile_defaults(monkeypatch) -> None:
    runner, store, worker, producer, _ = durable_worker_runtime()
    produce = producer.prepare_review
    defaults = settings(retail=2999)

    def prepare_with_profile_defaults(job_id: str, work_request_id: str):
        return produce(job_id, work_request_id).model_copy(update={"pricing": defaults})

    monkeypatch.setattr(producer, "prepare_review", prepare_with_profile_defaults)
    runner(
        PreparationRequest(
            session_id="session_explicit_default_pricing",
            job_id=PREPARATION_JOB_ID,
            mode="prepare",
            instruction="Prepare safely.",
        )
    )
    assert worker.commands[1].pricing == defaults
    review = store.get_review(PREPARATION_JOB_ID, 1)
    assert review.pricing == defaults
    assert review.fingerprint == review_content_fingerprint(review)
