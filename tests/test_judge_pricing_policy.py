from __future__ import annotations

import json
from dataclasses import FrozenInstanceError

import pytest

from mr_lister.control.commands import ApproveReviewCommand
from mr_lister.control.errors import InvalidControlStateError, NotFoundError
from mr_lister.control.fingerprints import review_content_fingerprint
from mr_lister.control.judge_pricing import (
    ENVIRONMENT_KEY,
    JudgePricingPolicy,
    JudgePricingPolicyError,
    load_judge_pricing_policy,
)
from mr_lister.control.models import ControlJobState, WorkType
from mr_lister.control.pricing import ReviewPricing, VariantPrice
from mr_lister.control.service import SellerControlService
from mr_lister.control.store import InMemorySellerControlStore
from mr_lister.control.worker_commands import BeginPreparationCommand, RecordPreparedReviewCommand
from mr_lister.control.worker_service import WorkerControlService
from tests.test_phase6_control_service import (
    NOW,
    OTHER_OWNER,
    OWNER,
    current_etag,
    revision_command,
    seed_reviewable,
)
from tests.test_phase6_worker_service import (
    PROFILE_FP,
    _activate,
    _analysis,
    _listing,
    _seed_preparation,
    _work,
)


def _pricing(*, free: bool = False, price: int = 3500, variant_price: int | None = None):
    return ReviewPricing(
        retail_price_cents=price,
        free_shipping=free,
        variant_prices=()
        if variant_price is None
        else (VariantPrice(color="Black", size="S", retail_price_cents=variant_price),),
    )


def test_absent_policy_is_disabled_and_present_policy_is_exact_immutable_owner() -> None:
    assert load_judge_pricing_policy({}) is None
    policy = load_judge_pricing_policy({ENVIRONMENT_KEY: json.dumps({"owner_id": OWNER})})
    assert policy == JudgePricingPolicy(OWNER)
    assert policy.defaults_for(OWNER) == _pricing()
    assert policy.defaults_for(OTHER_OWNER) is None
    policy.require_allowed(OTHER_OWNER, None)
    policy.require_allowed(OTHER_OWNER, _pricing(free=True, price=1))
    with pytest.raises(FrozenInstanceError):
        policy.owner_id = OTHER_OWNER


@pytest.mark.parametrize(
    "payload",
    [
        "",
        "null",
        "[]",
        "{}",
        '{"owner_id":"' + OWNER + '","owner_id":"' + OTHER_OWNER + '"}',
        json.dumps({"owner_id": OWNER, "free_shipping": False}),
        json.dumps({"owner_id": OWNER, "default_price_cents": None}),
        json.dumps({"owner_id": OWNER, "default_price_cents": True}),
        json.dumps({"owner_id": OWNER, "default_price_cents": "3500"}),
        json.dumps({"owner_id": OWNER, "default_price_cents": 0}),
        json.dumps({"owner_id": OWNER, "default_price_cents": 1_000_000}),
        json.dumps({"owner_id": OWNER, "minimum_price_cents": True}),
        json.dumps({"owner_id": OWNER, "minimum_price_cents": 3501}),
        json.dumps({"owner_id": OWNER.upper()}),
        json.dumps({"owner_id": "*"}),
        json.dumps({"owner_id": [OWNER]}),
    ],
)
def test_malformed_config_never_silently_disables_policy(payload: str) -> None:
    with pytest.raises(ValueError):
        load_judge_pricing_policy({ENVIRONMENT_KEY: payload})


@pytest.mark.parametrize("pricing", [None, _pricing(free=True)])
def test_judge_requires_explicit_standard_shipping(pricing) -> None:
    with pytest.raises(JudgePricingPolicyError, match="save"):
        JudgePricingPolicy(OWNER).require_allowed(OWNER, pricing)


def test_default_price_is_not_a_minimum_without_explicit_configuration() -> None:
    policy = JudgePricingPolicy(OWNER)
    policy.require_allowed(OWNER, _pricing(price=1, variant_price=2))
    assert policy.defaults_for(OWNER).retail_price_cents == 3500


@pytest.mark.parametrize("base,override", [(3499, None), (3500, 3499), (3499, 3500)])
def test_explicit_minimum_cannot_be_bypassed_by_base_or_variant(base, override) -> None:
    policy = JudgePricingPolicy(OWNER, minimum_price_cents=3500)
    with pytest.raises(JudgePricingPolicyError, match=r"at least \$35.00"):
        policy.require_allowed(OWNER, _pricing(price=base, variant_price=override))
    policy.require_allowed(OTHER_OWNER, _pricing(price=base, variant_price=override))
    policy.require_allowed(OWNER, _pricing(price=3500, variant_price=3600))


def _store_with_pricing(selected):
    store = InMemorySellerControlStore()
    job, review, sync, estimate = seed_reviewable(store)
    if selected is not None:
        review = review.model_copy(update={"pricing": selected})
        review = review.model_copy(update={"fingerprint": review_content_fingerprint(review)})
        job = job.model_copy(update={"review_fingerprint": review.fingerprint})
        store._jobs[job.job_id] = job
        store._reviews[(job.job_id, review.review_version)] = review
    return store, job, review, sync, estimate


@pytest.mark.parametrize("selected", [None, _pricing(free=True)])
@pytest.mark.parametrize("explicit_free_request", [False, True])
def test_existing_judge_draft_cannot_preserve_or_request_free_shipping_on_save(
    selected, explicit_free_request
) -> None:
    store, job, review, sync, estimate = _store_with_pricing(selected)
    command = revision_command(job, review, sync, estimate)
    if explicit_free_request:
        command = command.model_copy(
            update={
                "revision": command.revision.model_copy(update={"pricing": _pricing(free=True)})
            }
        )
    events_before = store.list_events(job.job_id)
    with pytest.raises(InvalidControlStateError, match="standard shipping"):
        SellerControlService(
            store=store, judge_pricing_policy=JudgePricingPolicy(OWNER)
        ).revise_listing(command)
    assert store.get_job(job.job_id) == job
    assert store.list_reviews(job.job_id) == (review,)
    assert store.list_events(job.job_id) == events_before


@pytest.mark.parametrize("selected", [None, _pricing(free=True)])
def test_explicit_standard_shipping_revision_migrates_existing_judge_draft(selected) -> None:
    store, job, review, sync, estimate = _store_with_pricing(selected)
    command = revision_command(job, review, sync, estimate)
    command = command.model_copy(
        update={"revision": command.revision.model_copy(update={"pricing": _pricing()})}
    )
    service = SellerControlService(
        store=store, clock=lambda: NOW, judge_pricing_policy=JudgePricingPolicy(OWNER)
    )
    result = service.revise_listing(command)
    assert service.revise_listing(command) == result
    assert result.state is ControlJobState.PRODUCT_DRAFT_SYNCING
    assert store.get_review(job.job_id, 2).pricing == _pricing()
    assert store.get_review(job.job_id, 1) == review
    assert store.get_job(job.job_id).pricing_snapshot_id is None
    assert store.get_job(job.job_id).approval_fingerprint is None
    assert (
        store.get_review(job.job_id, 2).product_profile_fingerprint
        == review.product_profile_fingerprint
    )


def test_text_revision_preserves_compliant_judge_price_instead_of_resetting_default() -> None:
    selected = _pricing(price=4000, variant_price=4500)
    store, job, review, sync, estimate = _store_with_pricing(selected)
    SellerControlService(
        store=store, clock=lambda: NOW, judge_pricing_policy=JudgePricingPolicy(OWNER)
    ).revise_listing(revision_command(job, review, sync, estimate))
    assert store.get_review(job.job_id, 2).pricing == selected


def _approve_command(job, review, sync, estimate):
    return ApproveReviewCommand(
        job_id=job.job_id,
        owner_id=OWNER,
        expected_record_version=job.record_version,
        expected_review_version=review.review_version,
        expected_review_fingerprint=review.fingerprint,
        expected_review_etag=current_etag(job, review, sync, estimate),
        idempotency_key="judge-pricing-approve",
    )


@pytest.mark.parametrize("selected", [None, _pricing(free=True)])
def test_judge_approval_is_blocked_before_any_durable_authorization(selected) -> None:
    store, job, review, sync, estimate = _store_with_pricing(selected)
    with pytest.raises(InvalidControlStateError, match="standard shipping"):
        SellerControlService(
            store=store, clock=lambda: NOW, judge_pricing_policy=JudgePricingPolicy(OWNER)
        ).approve_review(_approve_command(job, review, sync, estimate))
    assert store.get_job(job.job_id) == job
    assert store.list_review_decisions(job.job_id) == ()


def test_nonjudge_owner_keeps_legacy_approval_and_free_shipping_controls() -> None:
    store, job, review, sync, estimate = _store_with_pricing(None)
    result = SellerControlService(
        store=store, clock=lambda: NOW, judge_pricing_policy=JudgePricingPolicy(OTHER_OWNER)
    ).approve_review(_approve_command(job, review, sync, estimate))
    assert result.state is ControlJobState.APPROVED
    store, job, review, sync, estimate = _store_with_pricing(_pricing(free=True))
    SellerControlService(
        store=store, clock=lambda: NOW, judge_pricing_policy=JudgePricingPolicy(OTHER_OWNER)
    ).revise_listing(revision_command(job, review, sync, estimate))
    assert store.get_review(job.job_id, 2).pricing.free_shipping is True


def test_pricing_policy_does_not_reveal_or_override_job_ownership() -> None:
    store, job, review, sync, estimate = _store_with_pricing(None)
    command = revision_command(job, review, sync, estimate).model_copy(
        update={"owner_id": OTHER_OWNER}
    )
    with pytest.raises(NotFoundError):
        SellerControlService(
            store=store, judge_pricing_policy=JudgePricingPolicy(OWNER)
        ).revise_listing(command)


@pytest.mark.parametrize("policy_owner", [OWNER, OTHER_OWNER, None])
def test_only_new_exact_judge_checkpoint_receives_policy_defaults(policy_owner) -> None:
    store, clock, _, work = _seed_preparation()
    policy = None if policy_owner is None else JudgePricingPolicy(policy_owner)
    worker = WorkerControlService(store=store, clock=clock, judge_pricing_policy=policy)
    started = worker.begin_preparation(
        BeginPreparationCommand(
            job_id=work.job_id, work_request_id=work.work_request_id, expected_record_version=0
        )
    )
    source = store.get_source_artifact(started.job_id)
    original = _pricing(price=2999, free=True, variant_price=3000)
    command = RecordPreparedReviewCommand(
        job_id=started.job_id,
        work_request_id=work.work_request_id,
        expected_record_version=started.record_version,
        source_artifact_fingerprint=source.fingerprint,
        artwork_analysis=_analysis(),
        listing=_listing(),
        product_profile_fingerprint=PROFILE_FP,
        pricing=original,
    )
    result = worker.record_prepared_review(command)
    review = store.get_review(started.job_id, 1)
    assert review.pricing == (_pricing() if policy_owner == OWNER else original)
    assert review.fingerprint == review_content_fingerprint(review)
    assert review.product_profile_fingerprint == PROFILE_FP
    changed_policy = JudgePricingPolicy(OWNER, default_price_cents=4200)
    assert (
        WorkerControlService(
            store=store, clock=clock, judge_pricing_policy=changed_policy
        ).record_prepared_review(command)
        == result
    )
    assert store.get_review(started.job_id, 1) == review


@pytest.mark.parametrize("original", [None, _pricing(free=True), _pricing(price=4200)])
def test_resumed_checkpoint_preserves_original_pricing_and_fingerprint(original) -> None:
    store, clock, worker, work = _seed_preparation()
    started = worker.begin_preparation(
        BeginPreparationCommand(
            job_id=work.job_id, work_request_id=work.work_request_id, expected_record_version=0
        )
    )
    source = store.get_source_artifact(work.job_id)
    command = RecordPreparedReviewCommand(
        job_id=work.job_id,
        work_request_id=work.work_request_id,
        expected_record_version=started.record_version,
        source_artifact_fingerprint=source.fingerprint,
        artwork_analysis=_analysis(),
        listing=_listing(),
        product_profile_fingerprint=PROFILE_FP,
        pricing=original,
    )
    worker.record_prepared_review(command)
    review = store.get_review(work.job_id, 1)
    # Reconstruct a resumed machine operation against the already durable checkpoint.
    resumed = store.get_job(work.job_id).model_copy(
        update={
            "state": ControlJobState.ANALYZING_ARTWORK,
            "active_work_request_id": "work_resumed",
        }
    )
    store._jobs[work.job_id] = resumed
    resumed_work = _work(
        resumed,
        work_id="work_resumed",
        receipt_id="receipt_resume_fixture",
        work_type=WorkType.PREPARE,
        review_version=1,
        at=clock.value,
    )
    store._work[(work.job_id, resumed_work.work_request_id)] = resumed_work
    _activate(store, resumed, clock=clock)
    result = WorkerControlService(
        store=store, clock=clock, judge_pricing_policy=JudgePricingPolicy(OWNER)
    ).record_prepared_review(
        command.model_copy(
            update={
                "work_request_id": resumed_work.work_request_id,
                "expected_record_version": resumed.record_version,
                "pricing": _pricing(),
            }
        )
    )
    assert result.review_version == 1
    assert store.get_review(work.job_id, 1) == review
    assert store.get_review(work.job_id, 1).fingerprint == review_content_fingerprint(review)


@pytest.mark.parametrize("selected", [_pricing(price=3499), _pricing(variant_price=3499)])
def test_configured_minimum_blocks_both_revision_and_approval_atomically(selected) -> None:
    store, job, review, sync, estimate = _store_with_pricing(selected)
    service = SellerControlService(
        store=store,
        clock=lambda: NOW,
        judge_pricing_policy=JudgePricingPolicy(OWNER, minimum_price_cents=3500),
    )
    with pytest.raises(InvalidControlStateError, match=r"at least \$35.00"):
        service.revise_listing(revision_command(job, review, sync, estimate))
    with pytest.raises(InvalidControlStateError, match=r"at least \$35.00"):
        service.approve_review(_approve_command(job, review, sync, estimate))
    assert store.get_job(job.job_id) == job
    assert store.list_reviews(job.job_id) == (review,)
    assert store.list_review_decisions(job.job_id) == ()
