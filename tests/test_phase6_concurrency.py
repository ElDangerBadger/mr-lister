from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Barrier

import pytest

from mr_lister.control.dispatch import deterministic_execution_name, work_input_fingerprint
from mr_lister.control.errors import (
    ConcurrentControlModificationError,
    IdempotencyConflictError,
    InvalidControlStateError,
)
from mr_lister.control.fingerprints import review_etag
from mr_lister.control.models import (
    CancellationDecisionRecord,
    CommandReceipt,
    CommandResponse,
    ControlJobRecord,
    ControlJobState,
    DomainEvent,
    ReviewDecision,
    ReviewDecisionRecord,
    WorkRequest,
    WorkType,
)
from mr_lister.control.store import CommandCommit, InMemorySellerControlStore

NOW = datetime(2026, 8, 21, 12, 0, tzinfo=UTC)
OWNER = "a" * 64
REVIEW_FP = "b" * 64
SYNC_FP = "c" * 64
PRICING_FP = "d" * 64
APPROVAL_FP = review_etag(
    job_id="job_phase6_race",
    review_version=1,
    review_fingerprint=REVIEW_FP,
    product_id="product_1",
    product_sync_fingerprint=SYNC_FP,
    pricing_snapshot_id="pricing_1",
    pricing_snapshot_fingerprint=PRICING_FP,
)


def response(job: ControlJobRecord) -> CommandResponse:
    return CommandResponse(
        job_id=job.job_id,
        state=job.state,
        record_version=job.record_version,
        review_version=job.review_version,
    )


def receipt(
    job: ControlJobRecord,
    *,
    receipt_id: str,
    command_type: str,
    key_digest: str,
    request_fingerprint: str,
    work_id: str | None = None,
) -> CommandReceipt:
    return CommandReceipt(
        receipt_id=receipt_id,
        owner_id=OWNER,
        job_id=job.job_id,
        command_type=command_type,
        idempotency_key_digest=key_digest,
        request_fingerprint=request_fingerprint,
        response=CommandResponse(
            **{
                **response(job).model_dump(mode="python"),
                "work_request_id": work_id,
            }
        ),
        work_request_id=work_id,
        created_at=NOW,
    )


def event(job: ControlJobRecord, name: str) -> DomainEvent:
    return DomainEvent(
        job_id=job.job_id,
        sequence=job.event_sequence,
        name=name,
        occurred_at=job.updated_at,
    )


def seeded_reviewable_store() -> tuple[InMemorySellerControlStore, ControlJobRecord]:
    """Hydrate the mature state that these store-CAS unit tests race.

    Lifecycle construction is covered by the service suite. This deliberately
    isolated helper avoids obscuring the concurrency assertion with every prior
    legal transition while never exercising the guarded ``create_job`` path.
    """

    store = InMemorySellerControlStore()
    current = ControlJobRecord(
        owner_id=OWNER,
        job_id="job_phase6_race",
        state=ControlJobState.AWAITING_APPROVAL,
        event_sequence=1,
        review_version=1,
        review_fingerprint=REVIEW_FP,
        review_validated=True,
        product_id="product_1",
        product_sync_id="sync_1",
        synchronized_review_version=1,
        product_sync_fingerprint=SYNC_FP,
        pricing_snapshot_id="pricing_1",
        pricing_snapshot_fingerprint=PRICING_FP,
        created_at=NOW,
        updated_at=NOW,
    )
    store._jobs[current.job_id] = current
    store._events[current.job_id].append(event(current, "REVIEW_READY"))
    return store, current


def competing_commits(current: ControlJobRecord) -> tuple[CommandCommit, CommandCommit]:
    changed_at = NOW + timedelta(seconds=1)
    approved = current.model_copy(
        update={
            "state": ControlJobState.APPROVED,
            "record_version": 1,
            "event_sequence": 2,
            "approved_review_version": 1,
            "approved_review_fingerprint": REVIEW_FP,
            "approval_fingerprint": APPROVAL_FP,
            "updated_at": changed_at,
        }
    )
    approval_receipt = receipt(
        approved,
        receipt_id="receipt_approve",
        command_type="approve_review",
        key_digest="2" * 64,
        request_fingerprint="3" * 64,
    )
    approval = CommandCommit(
        current=current,
        updated=approved,
        event=event(approved, "REVIEW_APPROVED"),
        receipt=approval_receipt,
        review_decision=ReviewDecisionRecord(
            decision_id="decision_approve",
            job_id=current.job_id,
            actor_owner_id=OWNER,
            decision=ReviewDecision.APPROVE,
            review_version=1,
            review_fingerprint=REVIEW_FP,
            approval_fingerprint=APPROVAL_FP,
            command_receipt_id=approval_receipt.receipt_id,
            decided_at=changed_at,
        ),
    )

    cancelled = current.model_copy(
        update={
            "state": ControlJobState.CANCELLED,
            "record_version": 1,
            "event_sequence": 2,
            "cancellation_requested_at": changed_at,
            "updated_at": changed_at,
        }
    )
    cancellation_receipt = receipt(
        cancelled,
        receipt_id="receipt_cancel",
        command_type="cancel_job",
        key_digest="4" * 64,
        request_fingerprint="5" * 64,
    )
    cancellation = CommandCommit(
        current=current,
        updated=cancelled,
        event=event(cancelled, "CANCELLATION_DECIDED"),
        receipt=cancellation_receipt,
        cancellation_decision=CancellationDecisionRecord(
            decision_id="decision_cancel",
            job_id=current.job_id,
            actor_owner_id=OWNER,
            expected_record_version=0,
            review_version=1,
            review_fingerprint=REVIEW_FP,
            command_receipt_id=cancellation_receipt.receipt_id,
            decided_at=changed_at,
        ),
    )
    return approval, cancellation


def run_together(store: InMemorySellerControlStore, commits: tuple[CommandCommit, CommandCommit]):
    gate = Barrier(2)

    def run(commit: CommandCommit):
        gate.wait()
        try:
            return store.commit_command(commit)
        except Exception as error:  # returned for exact type assertions below
            return error

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(run, commit) for commit in commits]
        return tuple(future.result(timeout=5) for future in futures)


def test_competing_approve_and_cancel_have_exactly_one_cas_winner() -> None:
    store, current = seeded_reviewable_store()
    approval, cancellation = competing_commits(current)

    results = run_together(store, (approval, cancellation))

    successes = [result for result in results if isinstance(result, CommandReceipt)]
    conflicts = [
        result for result in results if isinstance(result, ConcurrentControlModificationError)
    ]
    assert len(successes) == 1
    assert len(conflicts) == 1
    assert store.get_job(current.job_id).state in {
        ControlJobState.APPROVED,
        ControlJobState.CANCELLED,
    }
    assert len(store.list_events(current.job_id)) == 2
    assert (
        len(store.list_review_decisions(current.job_id))
        + len(store.list_cancellation_decisions(current.job_id))
        == 1
    )


def test_simultaneous_identical_command_returns_one_persisted_receipt_twice() -> None:
    store, current = seeded_reviewable_store()
    approval, _cancellation = competing_commits(current)

    results = run_together(store, (approval, approval))

    assert results == (approval.receipt, approval.receipt)
    assert len(store.list_events(current.job_id)) == 2
    assert store.list_review_decisions(current.job_id) == (approval.review_decision,)


def test_simultaneous_same_key_with_changed_request_has_one_success_and_one_conflict() -> None:
    store, current = seeded_reviewable_store()
    approval, _cancellation = competing_commits(current)
    changed = CommandCommit(
        **{
            **approval.__dict__,
            "receipt": approval.receipt.model_copy(update={"request_fingerprint": "9" * 64}),
        }
    )

    results = run_together(store, (approval, changed))

    assert sum(isinstance(result, CommandReceipt) for result in results) == 1
    assert sum(isinstance(result, IdempotencyConflictError) for result in results) == 1
    assert len(store.list_events(current.job_id)) == 2


def test_approval_requires_a_decision_and_exact_composite_authority() -> None:
    store, current = seeded_reviewable_store()
    approval, _cancellation = competing_commits(current)

    with pytest.raises(InvalidControlStateError, match="immutable approval decision"):
        store.commit_command(
            CommandCommit(
                current=approval.current,
                updated=approval.updated,
                event=approval.event,
                receipt=approval.receipt,
            )
        )

    forged_fingerprint = "f" * 64
    forged_job = ControlJobRecord.model_validate(
        {
            **approval.updated.model_dump(mode="python"),
            "approval_fingerprint": forged_fingerprint,
        }
    )
    assert approval.review_decision is not None
    forged_decision = approval.review_decision.model_copy(
        update={"approval_fingerprint": forged_fingerprint}
    )
    forged_receipt = approval.receipt.model_copy(update={"response": response(forged_job)})
    with pytest.raises(InvalidControlStateError, match="exact composite authority"):
        store.commit_command(
            CommandCommit(
                current=current,
                updated=forged_job,
                event=approval.event,
                receipt=forged_receipt,
                review_decision=forged_decision,
            )
        )


def test_existing_product_id_cannot_be_replaced() -> None:
    store, current = seeded_reviewable_store()
    approval, _cancellation = competing_commits(current)
    replacement_fingerprint = review_etag(
        job_id=current.job_id,
        review_version=current.review_version,
        review_fingerprint=current.review_fingerprint or "",
        product_id="product_2",
        product_sync_fingerprint=current.product_sync_fingerprint,
        pricing_snapshot_id=current.pricing_snapshot_id,
        pricing_snapshot_fingerprint=current.pricing_snapshot_fingerprint,
    )
    replacement = ControlJobRecord.model_validate(
        {
            **approval.updated.model_dump(mode="python"),
            "product_id": "product_2",
            "approval_fingerprint": replacement_fingerprint,
        }
    )
    assert approval.review_decision is not None
    decision = approval.review_decision.model_copy(
        update={"approval_fingerprint": replacement_fingerprint}
    )
    replacement_receipt = approval.receipt.model_copy(update={"response": response(replacement)})

    with pytest.raises(InvalidControlStateError, match="cannot replace"):
        store.commit_command(
            CommandCommit(
                current=current,
                updated=replacement,
                event=approval.event,
                receipt=replacement_receipt,
                review_decision=decision,
            )
        )


def test_pricing_authority_cannot_advance_without_an_immutable_snapshot() -> None:
    store, current = seeded_reviewable_store()
    work_id = "work_refresh_economics"
    changed_at = NOW + timedelta(seconds=1)
    updated = ControlJobRecord.model_validate(
        {
            **current.model_dump(mode="python"),
            "state": ControlJobState.PRICING_REFRESHING,
            "record_version": 1,
            "event_sequence": 2,
            "pricing_snapshot_id": "pricing_2",
            "pricing_snapshot_fingerprint": "f" * 64,
            "active_work_request_id": work_id,
            "updated_at": changed_at,
        }
    )
    command_receipt = receipt(
        updated,
        receipt_id="receipt_refresh",
        command_type="refresh_economics",
        key_digest="6" * 64,
        request_fingerprint="7" * 64,
        work_id=work_id,
    )
    work = WorkRequest(
        work_request_id=work_id,
        owner_id=OWNER,
        job_id=current.job_id,
        receipt_id=command_receipt.receipt_id,
        work_type=WorkType.REFRESH_ECONOMICS,
        review_version=1,
        input_fingerprint=work_input_fingerprint(
            work_type=WorkType.REFRESH_ECONOMICS,
            job_id=current.job_id,
            work_request_id=work_id,
        ),
        execution_name=deterministic_execution_name(work_id),
        next_dispatch_at=changed_at,
        created_at=changed_at,
        updated_at=changed_at,
    )

    with pytest.raises(InvalidControlStateError, match="immutable snapshot"):
        store.commit_command(
            CommandCommit(
                current=current,
                updated=updated,
                event=event(updated, "ECONOMICS_REFRESH_REQUESTED"),
                receipt=command_receipt,
                work_request=work,
            )
        )
