from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from mr_lister.contracts import CONTRACT_VERSION, JobRecord, JobState
from mr_lister.control.models import (
    CONTROL_ALLOWED_TRANSITIONS,
    CONTROL_CONTRACT_VERSION,
    CONTROL_TERMINAL_STATES,
    CancellationDecisionRecord,
    ControlJobRecord,
    ControlJobState,
    ProductMockupEvidence,
    ProductSyncRecord,
    ProductVariantEvidence,
    WorkRequest,
    WorkRequestStatus,
    WorkType,
)

NOW = datetime(2026, 8, 21, 12, 0, tzinfo=UTC)
OWNER = "a" * 64
FINGERPRINT = "b" * 64


def job(**updates: object) -> ControlJobRecord:
    values: dict[str, object] = {
        "owner_id": OWNER,
        "job_id": "job_phase6_models",
        "record_version": 0,
        "event_sequence": 1,
        "state": ControlJobState.NEEDS_REVISION,
        "review_version": 1,
        "review_fingerprint": FINGERPRINT,
        "created_at": NOW,
        "updated_at": NOW,
    }
    values.update(updates)
    return ControlJobRecord.model_validate(values)


def test_phase6_contract_is_strictly_separate_from_legacy_v1() -> None:
    assert CONTRACT_VERSION == "1.0.0"
    assert CONTROL_CONTRACT_VERSION == "2.0.0"
    assert job().contract_version == "2.0.0"

    with pytest.raises(ValidationError):
        job(contract_version="1.0.0")
    with pytest.raises(ValidationError):
        JobRecord(
            contract_version="2.0.0",
            job_id="legacy_job",
            state=JobState.UPLOADED,
            review_version=0,
            idempotency_key="legacy-key",
            artwork_object_key="private/legacy.png",
            created_at=NOW,
            updated_at=NOW,
        )


def test_control_graph_is_exhaustive_closed_and_has_no_publication_state() -> None:
    assert set(CONTROL_ALLOWED_TRANSITIONS) == set(ControlJobState)
    assert all(not CONTROL_ALLOWED_TRANSITIONS[state] for state in CONTROL_TERMINAL_STATES)

    vocabulary = " ".join(state.value for state in ControlJobState)
    for forbidden in ("publishing", "published", "verified", "order", "fulfill"):
        assert forbidden not in vocabulary


def test_terminal_jobs_cannot_retain_active_work() -> None:
    with pytest.raises(ValidationError, match="may retain active work"):
        job(
            state=ControlJobState.CANCELLED,
            cancellation_requested_at=NOW,
            active_work_request_id="work_stale",
        )


def test_machine_states_require_work_and_cancellation_cannot_end_as_failure() -> None:
    with pytest.raises(ValidationError, match="require durable active work"):
        job(
            state=ControlJobState.ANALYZING_ARTWORK,
            review_version=0,
            review_fingerprint=None,
        )

    with pytest.raises(ValidationError, match="permanently disables normal job states"):
        job(
            state=ControlJobState.FAILED_TERMINAL,
            failure_id="failure_after_cancel",
            cancellation_requested_at=NOW,
        )


def test_approved_job_requires_composite_review_product_and_pricing_authority() -> None:
    with pytest.raises(ValidationError, match="current review to be synchronized"):
        job(
            state=ControlJobState.APPROVED,
            review_version=1,
            review_fingerprint=FINGERPRINT,
            review_validated=True,
            approved_review_version=1,
            approved_review_fingerprint=FINGERPRINT,
            approval_fingerprint="c" * 64,
            pricing_snapshot_id="pricing_1",
            pricing_snapshot_fingerprint="d" * 64,
        )


def test_pre_review_cancellation_record_does_not_manufacture_review_authority() -> None:
    decision = CancellationDecisionRecord(
        decision_id="cancel_1",
        job_id="job_phase6_models",
        actor_owner_id=OWNER,
        expected_record_version=0,
        command_receipt_id="receipt_cancel_1",
        decided_at=NOW,
    )

    assert decision.review_version is None
    assert decision.review_fingerprint is None

    with pytest.raises(ValidationError, match="optional as a pair"):
        CancellationDecisionRecord.model_validate({**decision.model_dump(), "review_version": 1})


def test_control_records_are_frozen_and_reject_unknown_fields() -> None:
    record = job()

    with pytest.raises(ValidationError):
        record.state = ControlJobState.CANCELLED  # type: ignore[misc]
    with pytest.raises(ValidationError):
        ControlJobRecord.model_validate({**record.model_dump(), "surprise": True})


def test_work_request_requires_a_coherent_dispatch_lease() -> None:
    base = {
        "work_request_id": "work_1",
        "owner_id": OWNER,
        "job_id": "job_phase6_models",
        "receipt_id": "receipt_1",
        "work_type": WorkType.PREPARE,
        "input_fingerprint": FINGERPRINT,
        "execution_name": "mrw-work-1",
        "next_dispatch_at": NOW,
        "created_at": NOW,
        "updated_at": NOW,
    }
    pending = WorkRequest.model_validate(base)

    assert pending.status is WorkRequestStatus.PENDING
    with pytest.raises(ValidationError, match="requires a lease"):
        WorkRequest.model_validate(
            {
                **base,
                "status": WorkRequestStatus.CLAIMED,
                "attempt_count": 1,
            }
        )
    claimed = WorkRequest.model_validate(
        {
            **base,
            "status": WorkRequestStatus.CLAIMED,
            "attempt_count": 1,
            "claim_id": "claim_1",
            "lease_expires_at": NOW + timedelta(minutes=1),
        }
    )
    assert claimed.claim_id == "claim_1"


@pytest.mark.parametrize(
    "url",
    (
        "http://images.printify.com/product/front.jpg",
        "//images.printify.com/product/front.jpg",
        "https://images-api.printify.com/product/front.jpg",
        "https://images.printify.com.evil.test/product/front.jpg",
        "https://images.printify.com@evil.test/product/front.jpg",
        "https://images.printify.com:443/product/front.jpg",
        "https://IMAGES.PRINTIFY.COM/product/front.jpg",
        "https://images.printify.com/product/front.jpg#fragment",
        "https://images.printify.com/product\\front.jpg",
        "https://images.printify.com/product/%broken.jpg",
        "https://images.printify.com/product/front.jpg\n",
    ),
)
def test_product_mockup_evidence_rejects_ambiguous_or_hostile_urls(url: str) -> None:
    with pytest.raises(ValidationError, match="mockup URL"):
        ProductMockupEvidence(url=url, position="front", variant_ids=(101,))


def test_product_sync_selects_deterministic_variant_covering_mockups() -> None:
    variants = (
        ProductVariantEvidence(
            variant_id=101,
            color="Black",
            size="S",
            placement_group_id="small",
            retail_price_cents=2999,
            production_cost_cents=1100,
        ),
        ProductVariantEvidence(
            variant_id=102,
            color="Navy",
            size="M",
            placement_group_id="medium",
            retail_price_cents=2999,
            production_cost_cents=1200,
        ),
    )
    mockups = (
        ProductMockupEvidence(
            url="https://images.printify.com/product/side.jpg",
            position="side",
            variant_ids=(101,),
        ),
        ProductMockupEvidence(
            url="https://images.printify.com/product/front-navy.jpg",
            position="front",
            variant_ids=(102,),
        ),
        ProductMockupEvidence(
            url="https://images.printify.com/product/front-all.jpg?quality=90",
            position="front",
            variant_ids=(101, 102),
        ),
    )
    sync = ProductSyncRecord(
        sync_id="sync_projection",
        job_id="job_phase6_models",
        review_version=1,
        product_id="product_projection",
        image_id="image_projection",
        payload_fingerprint="c" * 64,
        response_fingerprint="d" * 64,
        fingerprint="e" * 64,
        mockups=tuple(reversed(mockups)),
        variants=variants,
        synchronized_at=NOW,
    )

    selected = sync.representative_mockups(limit=2)

    assert tuple(mockup.url for mockup in selected) == (
        "https://images.printify.com/product/front-all.jpg?quality=90",
        "https://images.printify.com/product/front-navy.jpg",
    )
    assert selected == sync.model_copy(update={"mockups": mockups}).representative_mockups(limit=2)


def test_product_sync_rejects_duplicate_labels_and_unknown_mockup_variants() -> None:
    variant = ProductVariantEvidence(
        variant_id=101,
        color="Black",
        size="S",
        placement_group_id="small",
        retail_price_cents=2999,
        production_cost_cents=1100,
    )
    base = {
        "sync_id": "sync_projection",
        "job_id": "job_phase6_models",
        "review_version": 1,
        "product_id": "product_projection",
        "image_id": "image_projection",
        "payload_fingerprint": "c" * 64,
        "response_fingerprint": "d" * 64,
        "fingerprint": "e" * 64,
        "variants": (variant,),
        "synchronized_at": NOW,
    }

    with pytest.raises(ValidationError, match="unknown variants"):
        ProductSyncRecord(
            **base,
            mockups=(
                ProductMockupEvidence(
                    url="https://images.printify.com/product/front.jpg",
                    variant_ids=(999,),
                ),
            ),
        )
    with pytest.raises(ValidationError, match="color and size pairs"):
        ProductSyncRecord(
            **{
                **base,
                "variants": (
                    variant,
                    variant.model_copy(update={"variant_id": 102}),
                ),
            }
        )
