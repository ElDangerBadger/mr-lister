from __future__ import annotations

from datetime import UTC, datetime, timedelta
from hashlib import sha256

import pytest

from mr_lister.control.economics import (
    EstimatedProceedsRange,
    EtsyUsStandardEstimate,
    EtsyUsStandardFeePolicy,
    VariantProceedsEvidence,
)
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
    ProductSyncRecord,
    ProductVariantEvidence,
    ReviewActor,
    ReviewContent,
    ReviewDecision,
    ReviewDecisionRecord,
    SourceArtifactRecord,
)
from mr_lister.control.source_artwork import source_artifact_fingerprint
from mr_lister.publication.commands import (
    PublicationCommandReceipt,
    PublicationCommandType,
    PublicationRequestCommit,
    PublicationRequestResponse,
)
from mr_lister.publication.contract import PublicationPermitState, PublicationState
from mr_lister.publication.errors import (
    PublicationAuthorityError,
    PublicationConflictError,
    PublicationErrorCode,
    PublicationIdempotencyConflictError,
    PublicationNotFoundError,
)
from mr_lister.publication.fingerprints import (
    idempotency_key_digest,
    publication_aggregate_fingerprint,
    publication_attempt_fingerprint,
    publication_body_fingerprint,
    publication_command_receipt_fingerprint,
    publication_event_fingerprint,
    publication_permit_fingerprint,
    publication_snapshot_fingerprint,
    publication_work_input_fingerprint,
)
from mr_lister.publication.models import (
    PublicationAggregate,
    PublicationAttempt,
    PublicationDomainEvent,
    PublicationJobLink,
    PublicationPermit,
    PublicationSnapshot,
    PublicationWorkRequest,
)
from mr_lister.publication.retention_locator import (
    build_publication_request_receipt_locator,
)
from mr_lister.publication.store import (
    InMemoryPublicationStore,
    PublicationRequestAuthority,
    PublicationRequestTransaction,
    validate_publication_request_authority,
)

NOW = datetime(2026, 8, 23, 19, 0, tzinfo=UTC)
OWNER_ID = "a" * 64


def _fp(character: str) -> str:
    return character * 64


def _review_fingerprint(review: ReviewContent) -> str:
    return canonical_fingerprint(
        {
            "contract_version": review.contract_version,
            "job_id": review.job_id,
            "review_version": review.review_version,
            "actor": review.actor.value,
            "title": review.title,
            "description": review.description,
            "tags": review.tags,
            "audience": review.audience,
            "title_rationale": review.title_rationale,
            "tag_rationale": review.tag_rationale,
            "validation_passed": review.validation_passed,
            "validation_issue_codes": review.validation_issue_codes,
            "artwork_analysis_fingerprint": review.artwork_analysis_fingerprint,
            "product_profile_fingerprint": review.product_profile_fingerprint,
            "created_at": review.created_at.isoformat(),
        }
    )


def make_authority(
    *,
    owner_id: str = OWNER_ID,
    job_id: str = "job_publication_71",
    record_version: int = 12,
    updated_at: datetime = NOW - timedelta(minutes=5),
) -> PublicationRequestAuthority:
    review = ReviewContent(
        job_id=job_id,
        review_version=4,
        fingerprint=_fp("0"),
        actor=ReviewActor.SELLER,
        title="Exact approved shirt",
        description="Exact approved description",
        tags=tuple(f"tag{index}" for index in range(13)),
        audience=("shirt buyers",),
        title_rationale="Exact title authority",
        tag_rationale="Exact tag authority",
        validation_passed=True,
        validation_issue_codes=(),
        artwork_analysis_fingerprint=_fp("1"),
        product_profile_fingerprint=_fp("2"),
        created_at=NOW - timedelta(hours=2),
    )
    review = ReviewContent.model_validate(
        {**review.model_dump(mode="python"), "fingerprint": _review_fingerprint(review)}
    )

    source_values = {
        "job_id": job_id,
        "owner_id": owner_id,
        "bucket": "mr-lister-phase6-artifacts-test",
        "object_key": f"private/owners/{owner_id}/jobs/{job_id}/source/source.png",
        "version_id": "source-version-1",
        "content_sha256": _fp("3"),
        "size_bytes": 1024,
        "media_type": "image/png",
        "product_profile_id": "shirt_profile",
        "product_profile_version": 3,
        "product_profile_fingerprint": review.product_profile_fingerprint,
        "created_at": NOW - timedelta(hours=3),
    }
    source = SourceArtifactRecord(
        **source_values,
        fingerprint=source_artifact_fingerprint(**source_values),
    )

    variant = ProductVariantEvidence(
        variant_id=101,
        color="Black",
        size="M",
        placement_group_id="standard_front",
        retail_price_cents=3000,
        production_cost_cents=1000,
    )
    sync = ProductSyncRecord(
        sync_id="sync_4",
        job_id=job_id,
        review_version=review.review_version,
        product_id="product_4",
        image_id="image_4",
        printify_shop_id=987654,
        payload_fingerprint=_fp("4"),
        response_fingerprint=_fp("5"),
        fingerprint=_fp("0"),
        variants=(variant,),
        synchronized_at=NOW - timedelta(hours=1),
    )
    sync = ProductSyncRecord.model_validate(
        {
            **sync.model_dump(mode="python"),
            "fingerprint": product_sync_record_fingerprint(sync),
        }
    )

    proceeds = VariantProceedsEvidence(
        variant_id=variant.variant_id,
        retail_price_cents=variant.retail_price_cents,
        production_cost_cents=variant.production_cost_cents,
        production_shipping_cents=500,
        shipping_plan_id="standard_us",
        handling_from_days=2,
        handling_to_days=5,
        transaction_fee_cents=195,
        payment_processing_percentage_cents=90,
        payment_processing_fee_cents=115,
        total_marketplace_fees_cents=330,
        estimated_proceeds_cents=1170,
    )
    estimate = EtsyUsStandardEstimate(
        policy=EtsyUsStandardFeePolicy(),
        product_sync_fingerprint=sync.fingerprint,
        product_cost_evidence_fingerprint=_fp("6"),
        shipping_evidence_fingerprint=_fp("7"),
        blueprint_id=12,
        print_provider_id=34,
        shipping_source_path="/v1/catalog/blueprints/12/print_providers/34/shipping.json",
        product_cost_observed_at=NOW - timedelta(hours=1),
        shipping_observed_at=NOW - timedelta(hours=1),
        calculated_at=NOW - timedelta(minutes=30),
        fresh_until=NOW + timedelta(hours=1),
        variants=(proceeds,),
        proceeds_range=EstimatedProceedsRange(
            minimum_cents=1170,
            maximum_cents=1170,
            minimum_variant_ids=(variant.variant_id,),
            maximum_variant_ids=(variant.variant_id,),
        ),
    )
    pricing = PricingSnapshot(
        snapshot_id="pricing_4",
        job_id=job_id,
        review_version=review.review_version,
        product_sync_fingerprint=sync.fingerprint,
        fingerprint=estimate.fingerprint,
        fresh_until=estimate.fresh_until,
        created_at=estimate.calculated_at,
    )
    pricing_evidence = PricingEvidenceRecord(
        snapshot_id=pricing.snapshot_id,
        job_id=job_id,
        review_version=review.review_version,
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
    approval_receipt_id = "approval_receipt_4"
    decision = ReviewDecisionRecord(
        decision_id=f"decision_{sha256(approval_receipt_id.encode()).hexdigest()[:40]}",
        job_id=job_id,
        actor_owner_id=owner_id,
        decision=ReviewDecision.APPROVE,
        review_version=review.review_version,
        review_fingerprint=review.fingerprint,
        approval_fingerprint=approval_fingerprint,
        command_receipt_id=approval_receipt_id,
        decided_at=updated_at,
    )
    job = ControlJobRecord(
        owner_id=owner_id,
        job_id=job_id,
        record_version=record_version,
        event_sequence=8,
        state=ControlJobState.APPROVED,
        review_version=review.review_version,
        review_fingerprint=review.fingerprint,
        review_validated=True,
        source_artifact_fingerprint=source.fingerprint,
        artwork_analysis_id="analysis_4",
        artwork_analysis_fingerprint=review.artwork_analysis_fingerprint,
        agent_evidence_id="agent_evidence_4",
        agent_evidence_fingerprint=_fp("8"),
        product_id=sync.product_id,
        provider_payload_fingerprint=sync.payload_fingerprint,
        product_sync_id=sync.sync_id,
        synchronized_review_version=sync.review_version,
        product_sync_fingerprint=sync.fingerprint,
        pricing_snapshot_id=pricing.snapshot_id,
        pricing_snapshot_fingerprint=pricing.fingerprint,
        approval_decision_id=decision.decision_id,
        approved_review_version=review.review_version,
        approved_review_fingerprint=review.fingerprint,
        approval_fingerprint=approval_fingerprint,
        provider_upload_attempt_id="upload_attempt_4",
        uploaded_artwork_id="uploaded_artwork_4",
        uploaded_image_id=sync.image_id,
        uploaded_artwork_fingerprint=_fp("9"),
        provider_write_attempt_id="write_attempt_4",
        product_create_attempt_id="create_attempt_4",
        created_at=NOW - timedelta(days=1),
        updated_at=updated_at,
    )
    return PublicationRequestAuthority(
        current_job=job,
        review=review,
        approval_decision=decision,
        source=source,
        product_sync=sync,
        pricing_snapshot=pricing,
        pricing_evidence=pricing_evidence,
    )


def make_transaction(
    authority: PublicationRequestAuthority,
    *,
    suffix: str = "1",
    idempotency_key: str = "publish-key-1",
    request_fingerprint: str = _fp("a"),
) -> PublicationRequestTransaction:
    job = authority.current_job
    snapshot_values = {
        "owner_id": job.owner_id,
        "job_id": job.job_id,
        "expected_record_version": job.record_version,
        "approval_decision_id": authority.approval_decision.decision_id,
        "approval_fingerprint": job.approval_fingerprint,
        "review_version": authority.review.review_version,
        "review_fingerprint": authority.review.fingerprint,
        "product_sync_id": authority.product_sync.sync_id,
        "product_sync_fingerprint": authority.product_sync.fingerprint,
        "printify_shop_id": authority.product_sync.printify_shop_id,
        "printify_product_id": authority.product_sync.product_id,
        "printify_image_id": authority.product_sync.image_id,
        "product_payload_fingerprint": authority.product_sync.payload_fingerprint,
        "pricing_snapshot_id": authority.pricing_snapshot.snapshot_id,
        "pricing_snapshot_fingerprint": authority.pricing_snapshot.fingerprint,
        "pricing_evidence_fingerprint": authority.pricing_evidence.fingerprint,
        "pricing_fresh_until": authority.pricing_snapshot.fresh_until,
        "profile_id": authority.source.product_profile_id,
        "profile_version": authority.source.product_profile_version,
        "profile_fingerprint": authority.source.product_profile_fingerprint,
        "expected_sales_channel": "etsy",
        "publication_body_fingerprint": publication_body_fingerprint(),
        "release_manifest_fingerprint": _fp("b"),
        "requested_at": NOW,
        "verification_deadline": NOW + timedelta(seconds=1800),
    }
    snapshot = PublicationSnapshot(
        snapshot_id=f"snapshot_{suffix}",
        fingerprint=publication_snapshot_fingerprint(snapshot_values),
        **snapshot_values,
    )
    aggregate_id = f"publication_{suffix}"
    attempt_values = {
        "attempt_id": f"attempt_{suffix}",
        "aggregate_id": aggregate_id,
        "owner_id": job.owner_id,
        "job_id": job.job_id,
        "snapshot_id": snapshot.snapshot_id,
        "snapshot_fingerprint": snapshot.fingerprint,
        "root_attempt_number": 1,
        "record_version": 0,
        "shop_get_call_limit": 3,
        "shop_get_call_count": 0,
        "product_get_call_limit": 100,
        "product_get_call_count": 0,
        "publish_post_call_limit": 1,
        "publish_post_call_count": 0,
        "requested_at": NOW,
        "verification_deadline": snapshot.verification_deadline,
    }
    attempt = PublicationAttempt(
        **attempt_values,
        fingerprint=publication_attempt_fingerprint(attempt_values),
    )
    work_id = f"publication_work_{suffix}"
    permit_values = {
        "permit_id": f"permit_{suffix}",
        "aggregate_id": aggregate_id,
        "attempt_id": attempt.attempt_id,
        "snapshot_id": snapshot.snapshot_id,
        "snapshot_fingerprint": snapshot.fingerprint,
        "owner_id": job.owner_id,
        "job_id": job.job_id,
        "work_request_id": work_id,
        "status": PublicationPermitState.AVAILABLE,
        "maximum_publish_posts_authorized": 1,
        "record_version": 0,
        "created_at": NOW,
    }
    permit = PublicationPermit(
        **permit_values,
        fingerprint=publication_permit_fingerprint(permit_values),
    )
    receipt_id = f"publication_receipt_{suffix}"
    work_values = {
        "work_request_id": work_id,
        "aggregate_id": aggregate_id,
        "attempt_id": attempt.attempt_id,
        "snapshot_id": snapshot.snapshot_id,
        "snapshot_fingerprint": snapshot.fingerprint,
        "permit_id": permit.permit_id,
        "owner_id": job.owner_id,
        "job_id": job.job_id,
        "receipt_id": receipt_id,
        "execution_name": f"publication_execution_{suffix}",
        "verification_deadline": snapshot.verification_deadline,
        "next_dispatch_at": NOW,
        "created_at": NOW,
        "updated_at": NOW,
    }
    work = PublicationWorkRequest(
        **work_values,
        input_fingerprint=publication_work_input_fingerprint(work_values),
    )
    aggregate_values = {
        "aggregate_id": aggregate_id,
        "owner_id": job.owner_id,
        "job_id": job.job_id,
        "state": PublicationState.PUBLICATION_REQUESTED,
        "record_version": 0,
        "snapshot_id": snapshot.snapshot_id,
        "snapshot_fingerprint": snapshot.fingerprint,
        "attempt_id": attempt.attempt_id,
        "permit_id": permit.permit_id,
        "work_request_id": work.work_request_id,
        "receipt_id": receipt_id,
        "requested_at": NOW,
        "updated_at": NOW,
        "terminal_at": None,
        "source_release_eligible_at": None,
        "operational_expires_at": None,
    }
    aggregate = PublicationAggregate(
        **aggregate_values,
        fingerprint=publication_aggregate_fingerprint(aggregate_values),
    )
    event_values = {
        "aggregate_id": aggregate_id,
        "owner_id": job.owner_id,
        "job_id": job.job_id,
        "sequence": 1,
        "name": "PUBLICATION_REQUESTED",
        "state": PublicationState.PUBLICATION_REQUESTED,
        "snapshot_id": snapshot.snapshot_id,
        "attempt_id": attempt.attempt_id,
        "permit_id": permit.permit_id,
        "work_request_id": work.work_request_id,
        "occurred_at": NOW,
    }
    event = PublicationDomainEvent(
        **event_values,
        fingerprint=publication_event_fingerprint(event_values),
    )
    response = PublicationRequestResponse(
        job_id=job.job_id,
        publication_aggregate_id=aggregate_id,
        record_version=job.record_version + 1,
        review_version=job.review_version,
        work_request_id=work.work_request_id,
        requested_at=NOW,
        verification_deadline=snapshot.verification_deadline,
    )
    receipt_values = {
        "receipt_id": receipt_id,
        "owner_id": job.owner_id,
        "job_id": job.job_id,
        "aggregate_id": aggregate_id,
        "snapshot_id": snapshot.snapshot_id,
        "attempt_id": attempt.attempt_id,
        "permit_id": permit.permit_id,
        "work_request_id": work.work_request_id,
        "command_type": PublicationCommandType.REQUEST_PUBLICATION,
        "idempotency_key_digest": idempotency_key_digest(idempotency_key),
        "request_fingerprint": request_fingerprint,
        "response": response,
        "created_at": NOW,
    }
    receipt = PublicationCommandReceipt(
        **receipt_values,
        fingerprint=publication_command_receipt_fingerprint(receipt_values),
    )
    commit = PublicationRequestCommit(
        job_link=PublicationJobLink(
            owner_id=job.owner_id,
            job_id=job.job_id,
            expected_record_version=job.record_version,
            result_record_version=job.record_version + 1,
            expected_event_sequence=job.event_sequence,
            result_event_sequence=job.event_sequence,
            publication_aggregate_id=aggregate_id,
            linked_at=NOW,
        ),
        aggregate=aggregate,
        snapshot=snapshot,
        attempt=attempt,
        permit=permit,
        work_request=work,
        event=event,
        receipt=receipt,
    )
    updated = ControlJobRecord.model_validate(
        {
            **job.model_dump(mode="python"),
            "record_version": job.record_version + 1,
            "publication_aggregate_id": aggregate_id,
            "updated_at": NOW,
        }
    )
    return PublicationRequestTransaction(
        authority=authority,
        updated_job=updated,
        commit=commit,
    )


def test_in_memory_request_commit_is_atomic_and_preserves_phase6_event_sequence() -> None:
    authority = make_authority()
    transaction = make_transaction(authority)
    store = InMemoryPublicationStore((authority,))

    receipt = store.commit_request(transaction)

    assert receipt == transaction.commit.receipt
    assert store.jobs[authority.current_job.job_id] == transaction.updated_job
    assert transaction.updated_job.record_version == authority.current_job.record_version + 1
    assert transaction.updated_job.event_sequence == authority.current_job.event_sequence
    assert store.get_aggregate_for_owner(OWNER_ID, authority.current_job.job_id) == (
        transaction.commit.aggregate
    )
    assert len(store.aggregates) == len(store.snapshots) == len(store.attempts) == 1
    assert len(store.permits) == len(store.work_requests) == len(store.events) == 1
    assert len(store.receipts) == 1


def test_in_memory_exact_replay_changed_body_and_concurrent_key_are_closed() -> None:
    authority = make_authority()
    transaction = make_transaction(authority)
    store = InMemoryPublicationStore((authority,))
    first = store.commit_request(transaction)

    assert store.commit_request(transaction) is first
    changed_body = make_transaction(
        authority,
        suffix="2",
        idempotency_key="publish-key-1",
        request_fingerprint=_fp("c"),
    )
    with pytest.raises(PublicationIdempotencyConflictError):
        store.commit_request(changed_body)
    changed_key = make_transaction(authority, suffix="3", idempotency_key="publish-key-2")
    with pytest.raises(PublicationConflictError) as error:
        store.commit_request(changed_key)
    assert error.value.code is PublicationErrorCode.CONCURRENT_WRITE
    assert len(store.aggregates) == len(store.receipts) == 1


@pytest.mark.parametrize("tamper", ["missing", "different"])
def test_in_memory_replay_requires_exact_derived_receipt_locator(tamper: str) -> None:
    authority = make_authority()
    transaction = make_transaction(authority)
    store = InMemoryPublicationStore((authority,))
    receipt = store.commit_request(transaction)
    if tamper == "missing":
        store._receipt_locators.pop(receipt.aggregate_id)  # noqa: SLF001
    else:
        store._receipt_locators[receipt.aggregate_id] = (  # noqa: SLF001
            build_publication_request_receipt_locator(
                aggregate_id=receipt.aggregate_id,
                owner_id=receipt.owner_id,
                job_id=receipt.job_id,
                receipt_id=receipt.receipt_id,
                receipt_fingerprint="f" * 64,
                idempotency_key_digest=receipt.idempotency_key_digest,
            )
        )

    assert (
        store.resolve_request_receipt(
            OWNER_ID,
            authority.current_job.job_id,
            receipt.idempotency_key_digest,
        )
        is None
    )
    with pytest.raises(PublicationIdempotencyConflictError):
        store.commit_request(transaction)


def test_in_memory_stale_authority_writes_nothing_and_owner_mismatch_is_not_found() -> None:
    stale = make_authority()
    current = make_authority(record_version=13, updated_at=NOW - timedelta(minutes=1))
    transaction = make_transaction(stale)
    store = InMemoryPublicationStore((current,))

    with pytest.raises(PublicationConflictError) as error:
        store.commit_request(transaction)
    assert error.value.code is PublicationErrorCode.CONCURRENT_WRITE
    assert not store.aggregates
    assert not store.receipts
    with pytest.raises(PublicationNotFoundError):
        store.load_request_authority("f" * 64, stale.current_job.job_id)


def test_authority_rejects_forged_review_and_mismatched_variant_economics() -> None:
    authority = make_authority()
    forged_review = authority.review.model_copy(update={"title": "Forged title"})
    with pytest.raises(PublicationAuthorityError) as review_error:
        validate_publication_request_authority(
            PublicationRequestAuthority(**{**authority.__dict__, "review": forged_review})
        )
    assert review_error.value.code is PublicationErrorCode.INVALID_AUTHORITY

    estimate = authority.pricing_evidence.estimate
    mismatched_variant = VariantProceedsEvidence(
        variant_id=estimate.variants[0].variant_id,
        retail_price_cents=3100,
        production_cost_cents=1000,
        production_shipping_cents=500,
        shipping_plan_id="standard_us",
        handling_from_days=2,
        handling_to_days=5,
        transaction_fee_cents=202,
        payment_processing_percentage_cents=93,
        payment_processing_fee_cents=118,
        total_marketplace_fees_cents=340,
        estimated_proceeds_cents=1260,
    )
    forged_estimate = EtsyUsStandardEstimate.model_validate(
        {
            **estimate.model_dump(mode="python"),
            "variants": (mismatched_variant,),
            "proceeds_range": EstimatedProceedsRange(
                minimum_cents=1260,
                maximum_cents=1260,
                minimum_variant_ids=(mismatched_variant.variant_id,),
                maximum_variant_ids=(mismatched_variant.variant_id,),
            ),
        }
    )
    forged_pricing = authority.pricing_snapshot.model_copy(
        update={"fingerprint": forged_estimate.fingerprint}
    )
    forged_evidence = authority.pricing_evidence.model_copy(
        update={"estimate": forged_estimate, "fingerprint": forged_estimate.fingerprint}
    )
    forged_approval = review_etag(
        job_id=authority.current_job.job_id,
        review_version=authority.current_job.review_version,
        review_fingerprint=authority.review.fingerprint,
        product_id=authority.product_sync.product_id,
        product_sync_fingerprint=authority.product_sync.fingerprint,
        pricing_snapshot_id=forged_pricing.snapshot_id,
        pricing_snapshot_fingerprint=forged_pricing.fingerprint,
    )
    forged_decision = authority.approval_decision.model_copy(
        update={"approval_fingerprint": forged_approval}
    )
    forged_job = authority.current_job.model_copy(
        update={
            "pricing_snapshot_fingerprint": forged_pricing.fingerprint,
            "approval_fingerprint": forged_approval,
        }
    )
    with pytest.raises(PublicationAuthorityError) as pricing_error:
        validate_publication_request_authority(
            PublicationRequestAuthority(
                **{
                    **authority.__dict__,
                    "current_job": forged_job,
                    "approval_decision": forged_decision,
                    "pricing_snapshot": forged_pricing,
                    "pricing_evidence": forged_evidence,
                }
            )
        )
    assert pricing_error.value.code is PublicationErrorCode.INVALID_AUTHORITY


def test_authority_rejects_forged_approval_receipt_identity_and_time() -> None:
    authority = make_authority()
    forged_receipt = authority.approval_decision.model_copy(
        update={"command_receipt_id": "different_approval_receipt"}
    )
    with pytest.raises(PublicationAuthorityError) as receipt_error:
        validate_publication_request_authority(
            PublicationRequestAuthority(
                **{**authority.__dict__, "approval_decision": forged_receipt}
            )
        )
    assert receipt_error.value.code is PublicationErrorCode.INVALID_AUTHORITY

    forged_time = authority.approval_decision.model_copy(
        update={"decided_at": authority.approval_decision.decided_at - timedelta(seconds=1)}
    )
    with pytest.raises(PublicationAuthorityError) as time_error:
        validate_publication_request_authority(
            PublicationRequestAuthority(**{**authority.__dict__, "approval_decision": forged_time})
        )
    assert time_error.value.code is PublicationErrorCode.INVALID_AUTHORITY


def test_authority_requires_job_level_review_validation() -> None:
    authority = make_authority()
    invalid_job = authority.current_job.model_copy(update={"review_validated": False})

    with pytest.raises(PublicationAuthorityError) as error:
        validate_publication_request_authority(
            PublicationRequestAuthority(**{**authority.__dict__, "current_job": invalid_job})
        )
    assert error.value.code is PublicationErrorCode.INVALID_AUTHORITY


def test_transaction_revalidates_model_copy_bypassed_publication_records() -> None:
    authority = make_authority()
    transaction = make_transaction(authority)
    forged_permit = transaction.commit.permit.model_copy(
        update={"maximum_publish_posts_authorized": 2}
    )
    forged_commit = transaction.commit.model_copy(update={"permit": forged_permit})
    forged_transaction = PublicationRequestTransaction(
        authority=authority,
        updated_job=transaction.updated_job,
        commit=forged_commit,
    )
    store = InMemoryPublicationStore((authority,))

    with pytest.raises(PublicationAuthorityError) as error:
        store.commit_request(forged_transaction)
    assert error.value.code is PublicationErrorCode.INVALID_AUTHORITY
    assert not store.permits
    assert not store.aggregates
