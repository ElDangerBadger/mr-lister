from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from mr_lister.control.dispatch import deterministic_execution_name, work_input_fingerprint
from mr_lister.control.dynamodb import DynamoDBSellerControlStore
from mr_lister.control.errors import (
    IdempotencyConflictError,
    InvalidControlStateError,
    NotFoundError,
)
from mr_lister.control.models import (
    CONTROL_NEW_WORK_BY_STATE,
    PHASE6_MAX_SOURCE_ARTWORK_BYTES,
    CancellationDecisionRecord,
    CommandReceipt,
    CommandResponse,
    ControlJobRecord,
    ControlJobState,
    DomainEvent,
    ReviewActor,
    ReviewContent,
    ReviewDecision,
    ReviewDecisionRecord,
    SourceArtifactRecord,
    WorkRequest,
    WorkRequestStatus,
    WorkType,
)
from mr_lister.control.source_artwork import source_artifact_fingerprint
from mr_lister.control.store import (
    CommandCommit,
    InMemorySellerControlStore,
    validate_command_commit,
    validate_new_work_request,
)

NOW = datetime(2026, 8, 21, 12, 0, tzinfo=UTC)
OWNER = "a" * 64
OTHER_OWNER = "f" * 64
REVIEW_FP = "b" * 64
REQUEST_FP = "c" * 64
KEY_DIGEST = "d" * 64


def _source_material(*, job_id: str, owner_id: str, created_at: datetime) -> dict[str, object]:
    return {
        "job_id": job_id,
        "owner_id": owner_id,
        "bucket": "mr-lister-phase6-artifacts-test",
        "object_key": f"private/owners/{owner_id}/jobs/{job_id}/source/source.png",
        "version_id": "source-version-1",
        "content_sha256": "1" * 64,
        "size_bytes": 128,
        "media_type": "image/png",
        "product_profile_id": "profile_test",
        "product_profile_version": 1,
        "product_profile_fingerprint": "2" * 64,
        "created_at": created_at,
    }


def _source_fingerprint(*, job_id: str, owner_id: str, created_at: datetime) -> str:
    return source_artifact_fingerprint(
        **_source_material(job_id=job_id, owner_id=owner_id, created_at=created_at)
    )


SOURCE_FP = _source_fingerprint(
    job_id="job_phase6_store",
    owner_id=OWNER,
    created_at=NOW,
)


class _NoDynamoWrites:
    def transact_write_items(self, **_request: object) -> None:
        raise AssertionError("A rejected initial state reached DynamoDB")


def make_non_initial_job(state: ControlJobState) -> ControlJobRecord:
    values: dict[str, object] = {
        "owner_id": OWNER,
        "job_id": f"job_initial_{state.value}",
        "state": state,
        "event_sequence": 1,
        "created_at": NOW,
        "updated_at": NOW,
    }
    if state in CONTROL_NEW_WORK_BY_STATE or state is ControlJobState.CANCEL_REQUESTED:
        values["active_work_request_id"] = "work_initial"
    if state in {ControlJobState.FAILED_RETRYABLE, ControlJobState.FAILED_TERMINAL}:
        values["failure_id"] = "failure_initial"
    if state in {ControlJobState.CANCEL_REQUESTED, ControlJobState.CANCELLED}:
        values["cancellation_requested_at"] = NOW
    if state is ControlJobState.APPROVED:
        values.update(
            {
                "review_version": 1,
                "review_fingerprint": "2" * 64,
                "review_validated": True,
                "product_id": "product_initial",
                "product_sync_id": "sync_initial",
                "synchronized_review_version": 1,
                "product_sync_fingerprint": "3" * 64,
                "pricing_snapshot_id": "pricing_initial",
                "pricing_snapshot_fingerprint": "4" * 64,
                "approval_decision_id": "decision_initial",
                "approved_review_version": 1,
                "approved_review_fingerprint": "2" * 64,
                "approval_fingerprint": "5" * 64,
            }
        )
    return ControlJobRecord.model_validate(values)


def make_job(
    *,
    state: ControlJobState = ControlJobState.INTAKE_VALIDATED,
    active_work_request_id: str | None = None,
    event_sequence: int = 1,
) -> ControlJobRecord:
    return ControlJobRecord(
        owner_id=OWNER,
        job_id="job_phase6_store",
        state=state,
        event_sequence=event_sequence,
        source_artifact_fingerprint=SOURCE_FP,
        active_work_request_id=active_work_request_id,
        created_at=NOW,
        updated_at=NOW,
    )


def test_review_decision_lookup_is_owner_neutral_but_job_bound() -> None:
    store = InMemorySellerControlStore()
    job = make_job(active_work_request_id="work_lookup")
    other = job.model_copy(update={"job_id": "job_phase6_other"})
    decision = ReviewDecisionRecord(
        decision_id="decision_lookup",
        job_id=job.job_id,
        actor_owner_id=OWNER,
        decision=ReviewDecision.REVISE,
        review_version=1,
        review_fingerprint=REVIEW_FP,
        command_receipt_id="receipt_lookup",
        decided_at=NOW,
    )
    store._jobs[job.job_id] = job
    store._jobs[other.job_id] = other
    store._review_decisions[decision.decision_id] = decision

    assert store.get_review_decision(job.job_id, decision.decision_id) == decision
    with pytest.raises(NotFoundError, match="review decision"):
        store.get_review_decision(other.job_id, decision.decision_id)
    with pytest.raises(NotFoundError, match="review decision"):
        store.get_review_decision(job.job_id, "decision_unknown")


@pytest.mark.parametrize(
    ("current_id", "updated_id"),
    (
        (None, "publication_new"),
        ("publication_existing", None),
        ("publication_existing", "publication_replacement"),
    ),
)
def test_phase6_commits_cannot_mutate_publication_aggregate_authority(
    current_id: str | None,
    updated_id: str | None,
) -> None:
    current = make_non_initial_job(ControlJobState.APPROVED).model_copy(
        update={"publication_aggregate_id": current_id}
    )
    updated = current.model_copy(
        update={
            "record_version": current.record_version + 1,
            "event_sequence": current.event_sequence + 1,
            "publication_aggregate_id": updated_id,
        }
    )

    with pytest.raises(InvalidControlStateError, match="Phase 6 commands cannot change"):
        validate_command_commit(
            CommandCommit(
                current=current,
                updated=updated,
                event=make_event(updated),
                receipt=make_receipt(
                    updated,
                    receipt_id="receipt_publication_mutation",
                    command_type="forged_phase6_publication_mutation",
                ),
            )
        )


def test_phase6_commits_cannot_mutate_publication_terminal_authority() -> None:
    current = make_non_initial_job(ControlJobState.APPROVED).model_copy(
        update={"publication_aggregate_id": "publication_existing"}
    )
    terminal_at = current.updated_at + timedelta(days=1)
    updated = current.model_copy(
        update={
            "record_version": current.record_version + 1,
            "event_sequence": current.event_sequence + 1,
            "updated_at": terminal_at,
            "publication_terminal_state": "publication_failed",
            "publication_terminal_at": terminal_at,
            "publication_source_release_eligible_at": terminal_at + timedelta(days=30),
            "publication_operational_expires_at": terminal_at + timedelta(days=90),
            "publication_report_id": "report_forged",
            "publication_terminal_summary_fingerprint": "f" * 64,
        }
    )

    with pytest.raises(
        InvalidControlStateError,
        match="Phase 6 commands cannot change publication terminal authority",
    ):
        validate_command_commit(
            CommandCommit(
                current=current,
                updated=updated,
                event=make_event(updated),
                receipt=make_receipt(
                    updated,
                    receipt_id="receipt_publication_terminal_mutation",
                    command_type="forged_phase6_publication_terminal_mutation",
                ),
            )
        )


def make_source(job: ControlJobRecord) -> SourceArtifactRecord:
    material = _source_material(job_id=job.job_id, owner_id=job.owner_id, created_at=NOW)
    return SourceArtifactRecord(fingerprint=source_artifact_fingerprint(**material), **material)


def test_source_artifact_size_matches_phase6_upload_transport_boundary() -> None:
    job = make_job(active_work_request_id="work_prepare")
    material = make_source(job).model_dump(exclude={"fingerprint", "size_bytes"})
    maximum_material = {**material, "size_bytes": PHASE6_MAX_SOURCE_ARTWORK_BYTES}

    assert (
        SourceArtifactRecord(
            **maximum_material,
            fingerprint=source_artifact_fingerprint(**maximum_material),
        ).size_bytes
        == PHASE6_MAX_SOURCE_ARTWORK_BYTES
    )
    oversized_material = {
        **material,
        "size_bytes": PHASE6_MAX_SOURCE_ARTWORK_BYTES + 1,
    }
    with pytest.raises(ValueError):
        SourceArtifactRecord(
            **oversized_material,
            fingerprint=source_artifact_fingerprint(**oversized_material),
        )


def make_response(job: ControlJobRecord, *, work_id: str | None = None) -> CommandResponse:
    return CommandResponse(
        job_id=job.job_id,
        state=job.state,
        record_version=job.record_version,
        review_version=job.review_version,
        work_request_id=work_id,
    )


def make_receipt(
    job: ControlJobRecord,
    *,
    receipt_id: str,
    command_type: str,
    key_digest: str = KEY_DIGEST,
    request_fingerprint: str = REQUEST_FP,
    work_id: str | None = None,
) -> CommandReceipt:
    return CommandReceipt(
        receipt_id=receipt_id,
        owner_id=job.owner_id,
        job_id=job.job_id,
        command_type=command_type,
        idempotency_key_digest=key_digest,
        request_fingerprint=request_fingerprint,
        response=make_response(job, work_id=work_id),
        work_request_id=work_id,
        created_at=NOW,
    )


def make_event(job: ControlJobRecord, *, name: str = "JOB_CREATED") -> DomainEvent:
    return DomainEvent(
        job_id=job.job_id,
        sequence=job.event_sequence,
        name=name,
        occurred_at=job.updated_at,
    )


def make_work(job: ControlJobRecord, receipt: CommandReceipt, *, due: datetime) -> WorkRequest:
    assert receipt.work_request_id is not None
    work_id = receipt.work_request_id
    return WorkRequest(
        work_request_id=work_id,
        owner_id=job.owner_id,
        job_id=job.job_id,
        receipt_id=receipt.receipt_id,
        work_type=WorkType.PREPARE,
        input_fingerprint=work_input_fingerprint(
            work_type=WorkType.PREPARE,
            job_id=job.job_id,
            work_request_id=work_id,
        ),
        execution_name=deterministic_execution_name(work_id),
        next_dispatch_at=due,
        created_at=NOW,
        updated_at=NOW,
    )


def create_seed_job(
    store: InMemorySellerControlStore,
    *,
    state: ControlJobState = ControlJobState.INTAKE_VALIDATED,
    work_due: datetime | None = None,
) -> tuple[ControlJobRecord, CommandReceipt, WorkRequest | None]:
    if work_due is None and state in {
        ControlJobState.INTAKE_VALIDATED,
        ControlJobState.ANALYZING_ARTWORK,
        ControlJobState.LISTING_DRAFTED,
        ControlJobState.PRODUCT_DRAFT_SYNCING,
        ControlJobState.RECONCILIATION_REQUIRED,
        ControlJobState.PRICING_REFRESHING,
    }:
        work_due = NOW
    work_id = "work_initial" if work_due is not None else None
    job = make_job(state=state, active_work_request_id=work_id)
    receipt = make_receipt(
        job,
        receipt_id="receipt_create",
        command_type="create_job",
        key_digest="1" * 64,
        work_id=work_id,
    )
    work = None if work_due is None else make_work(job, receipt, due=work_due)
    store.create_job(
        job=job,
        event=make_event(job),
        receipt=receipt,
        work_request=work,
        source_artifact=make_source(job),
    )
    return job, receipt, work


def _create_owned_job(
    store: InMemorySellerControlStore,
    *,
    owner_id: str,
    job_id: str,
    updated_at: datetime,
) -> ControlJobRecord:
    work_id = f"work_{job_id}"
    job = ControlJobRecord(
        owner_id=owner_id,
        job_id=job_id,
        state=ControlJobState.INTAKE_VALIDATED,
        event_sequence=1,
        source_artifact_fingerprint=_source_fingerprint(
            job_id=job_id,
            owner_id=owner_id,
            created_at=updated_at,
        ),
        active_work_request_id=work_id,
        created_at=updated_at,
        updated_at=updated_at,
    )
    receipt = CommandReceipt(
        receipt_id=f"receipt_{job_id}",
        owner_id=owner_id,
        job_id=job_id,
        command_type="create_job",
        idempotency_key_digest=(job_id.encode().hex() + "0" * 64)[:64],
        request_fingerprint=(job_id.encode().hex() + "1" * 64)[:64],
        response=make_response(job, work_id=work_id),
        work_request_id=work_id,
        created_at=updated_at,
    )
    work = WorkRequest(
        work_request_id=work_id,
        owner_id=owner_id,
        job_id=job_id,
        receipt_id=receipt.receipt_id,
        work_type=WorkType.PREPARE,
        input_fingerprint=work_input_fingerprint(
            work_type=WorkType.PREPARE,
            job_id=job_id,
            work_request_id=work_id,
        ),
        execution_name=deterministic_execution_name(work_id),
        next_dispatch_at=updated_at,
        created_at=updated_at,
        updated_at=updated_at,
    )
    material = _source_material(job_id=job_id, owner_id=owner_id, created_at=updated_at)
    source = SourceArtifactRecord(
        fingerprint=source_artifact_fingerprint(**material),
        **material,
    )
    store.create_job(
        job=job,
        event=make_event(job),
        receipt=receipt,
        work_request=work,
        source_artifact=source,
    )
    return job


def test_owner_job_listing_is_recent_bounded_paginated_and_cross_owner_closed() -> None:
    store = InMemorySellerControlStore()
    oldest = _create_owned_job(
        store,
        owner_id=OWNER,
        job_id="job_owner_oldest",
        updated_at=NOW,
    )
    middle = _create_owned_job(
        store,
        owner_id=OWNER,
        job_id="job_owner_middle",
        updated_at=NOW + timedelta(minutes=1),
    )
    newest = _create_owned_job(
        store,
        owner_id=OWNER,
        job_id="job_owner_newest",
        updated_at=NOW + timedelta(minutes=2),
    )
    _create_owned_job(
        store,
        owner_id=OTHER_OWNER,
        job_id="job_other_owner",
        updated_at=NOW + timedelta(minutes=3),
    )

    first = store.list_jobs_for_owner(OWNER, limit=2)
    assert first.jobs == (newest, middle)
    assert first.next_cursor is not None
    assert "job_owner_middle" not in first.next_cursor

    second = store.list_jobs_for_owner(OWNER, limit=2, cursor=first.next_cursor)
    assert second.jobs == (oldest,)
    assert second.next_cursor is None

    with pytest.raises(ValueError):
        store.list_jobs_for_owner(OWNER, cursor="not-a-canonical-cursor")


@pytest.mark.parametrize("adapter", ("memory", "dynamodb"))
@pytest.mark.parametrize(
    "state",
    tuple(state for state in ControlJobState if state is not ControlJobState.INTAKE_VALIDATED),
)
def test_create_job_rejects_every_non_initial_state(
    adapter: str,
    state: ControlJobState,
) -> None:
    store = (
        InMemorySellerControlStore()
        if adapter == "memory"
        else DynamoDBSellerControlStore(
            client=_NoDynamoWrites(),
            table_name="MrListerPhase6Control",
        )
    )
    job = make_non_initial_job(state)
    receipt = make_receipt(
        job,
        receipt_id=f"receipt_{state.value}",
        command_type="create_job",
    )

    with pytest.raises(
        (InvalidControlStateError, ValueError),
        match="INTAKE_VALIDATED",
    ):
        store.create_job(
            job=job,
            event=make_event(job),
            receipt=receipt,
        )


def make_review(version: int = 1) -> ReviewContent:
    return ReviewContent(
        job_id="job_phase6_store",
        review_version=version,
        fingerprint=REVIEW_FP,
        actor=ReviewActor.MODEL,
        title="Geometric Badger Shirt",
        description="A synthetic listing used to test the durable control boundary.",
        tags=tuple(f"tag {index}" for index in range(13)),
        title_rationale="Synthetic title rationale.",
        tag_rationale="Synthetic tag rationale.",
        validation_passed=True,
        artwork_analysis_fingerprint="2" * 64,
        product_profile_fingerprint="3" * 64,
        created_at=NOW,
    )


def test_create_job_atomically_stores_job_event_receipt_and_pending_work() -> None:
    store = InMemorySellerControlStore()
    job, receipt, work = create_seed_job(store, work_due=NOW + timedelta(minutes=5))
    assert work is not None

    assert store.get_job(job.job_id) == job
    assert store.list_events(job.job_id) == (make_event(job),)
    assert (
        store.resolve_receipt(
            OWNER,
            receipt.command_type,
            job.job_id,
            receipt.idempotency_key_digest,
        )
        == receipt
    )
    assert store.get_work_request(job.job_id, work.work_request_id) == work
    with pytest.raises(NotFoundError):
        store.get_job_for_owner(OTHER_OWNER, job.job_id)


def test_create_job_exact_replay_returns_receipt_and_changed_request_conflicts() -> None:
    store = InMemorySellerControlStore()
    job, receipt, work = create_seed_job(store, work_due=NOW)
    assert work is not None

    assert (
        store.create_job(
            job=job,
            event=make_event(job),
            receipt=receipt,
            work_request=work,
            source_artifact=make_source(job),
        )
        == receipt
    )
    assert len(store.list_events(job.job_id)) == 1

    changed = receipt.model_copy(update={"request_fingerprint": "9" * 64})
    with pytest.raises(IdempotencyConflictError, match="another request"):
        store.create_job(
            job=job,
            event=make_event(job),
            receipt=changed,
            work_request=work,
            source_artifact=make_source(job),
        )


def test_create_job_rejects_work_for_the_wrong_machine() -> None:
    store = InMemorySellerControlStore()
    job = make_job(active_work_request_id="work_wrong_machine")
    receipt = make_receipt(
        job,
        receipt_id="receipt_wrong_machine",
        command_type="create_job",
        work_id="work_wrong_machine",
    )
    work = make_work(job, receipt, due=NOW)
    wrong_work = WorkRequest.model_validate(
        {
            **work.model_dump(mode="python"),
            "work_type": WorkType.RECONCILE_PRODUCT,
            "input_fingerprint": work_input_fingerprint(
                work_type=WorkType.RECONCILE_PRODUCT,
                job_id=job.job_id,
                work_request_id=work.work_request_id,
            ),
        }
    )

    with pytest.raises(InvalidControlStateError, match="work type"):
        store.create_job(
            job=job,
            event=make_event(job),
            receipt=receipt,
            work_request=wrong_work,
            source_artifact=make_source(job),
        )


@pytest.mark.parametrize(
    "updates",
    (
        {"attempt_count": 1},
        {"execution_arn": "arn:aws:states:us-west-2:123456789012:execution:test:old"},
        {"last_error_code": "DISPATCH_TRANSIENT"},
        {"updated_at": NOW + timedelta(seconds=1)},
    ),
)
def test_create_job_rejects_non_pristine_outbox_work(updates: dict[str, object]) -> None:
    store = InMemorySellerControlStore()
    job = make_job(active_work_request_id="work_non_pristine")
    receipt = make_receipt(
        job,
        receipt_id="receipt_non_pristine",
        command_type="complete_upload",
        work_id="work_non_pristine",
    )
    work = make_work(job, receipt, due=NOW)
    non_pristine = WorkRequest.model_validate({**work.model_dump(mode="python"), **updates})

    with pytest.raises(InvalidControlStateError, match="pristine"):
        store.create_job(
            job=job,
            event=make_event(job),
            receipt=receipt,
            work_request=non_pristine,
            source_artifact=make_source(job),
        )

    with pytest.raises(NotFoundError):
        store.get_job(job.job_id)


def test_provider_work_requires_the_exact_positive_review_version() -> None:
    job = ControlJobRecord(
        owner_id=OWNER,
        job_id="job_provider_work",
        state=ControlJobState.PRODUCT_DRAFT_SYNCING,
        review_version=1,
        review_fingerprint=REVIEW_FP,
        review_validated=True,
        active_work_request_id="work_provider",
        created_at=NOW,
        updated_at=NOW,
    )
    work = WorkRequest(
        work_request_id="work_provider",
        owner_id=OWNER,
        job_id=job.job_id,
        receipt_id="receipt_provider",
        work_type=WorkType.SYNCHRONIZE_PRODUCT,
        review_version=None,
        input_fingerprint=work_input_fingerprint(
            work_type=WorkType.SYNCHRONIZE_PRODUCT,
            job_id=job.job_id,
            work_request_id="work_provider",
        ),
        execution_name=deterministic_execution_name("work_provider"),
        next_dispatch_at=NOW,
        created_at=NOW,
        updated_at=NOW,
    )

    with pytest.raises(InvalidControlStateError, match="exact current review"):
        validate_new_work_request(job, work)


def test_commit_command_atomically_stores_job_review_event_and_receipt() -> None:
    store = InMemorySellerControlStore()
    intake, _receipt, _work = create_seed_job(store)
    current = ControlJobRecord.model_validate(
        {
            **intake.model_dump(mode="python"),
            "state": ControlJobState.ANALYZING_ARTWORK,
            "record_version": 1,
            "event_sequence": 2,
            "updated_at": NOW + timedelta(seconds=1),
        }
    )
    started_receipt = make_receipt(
        current,
        receipt_id="receipt_started",
        command_type="preparation_started",
    )
    store.commit_command(
        CommandCommit(
            current=intake,
            updated=current,
            event=make_event(current, name="PREPARATION_STARTED"),
            receipt=started_receipt,
        )
    )
    review = make_review()
    updated = current.model_copy(
        update={
            "state": ControlJobState.LISTING_DRAFTED,
            "record_version": 2,
            "event_sequence": 3,
            "review_version": 1,
            "review_fingerprint": review.fingerprint,
            "review_validated": True,
            "updated_at": NOW + timedelta(seconds=1),
        }
    )
    receipt = make_receipt(
        updated,
        receipt_id="receipt_prepare",
        command_type="complete_preparation",
    )
    event = make_event(updated, name="LISTING_DRAFTED")
    commit = CommandCommit(
        current=current,
        updated=updated,
        event=event,
        receipt=receipt,
        review=review,
    )

    assert store.commit_command(commit) == receipt
    assert store.get_job(current.job_id) == updated
    assert store.get_review(current.job_id, 1) == review
    assert store.list_events(current.job_id) == (
        make_event(intake),
        make_event(current, name="PREPARATION_STARTED"),
        event,
    )
    assert store.resolve_receipt(OWNER, receipt.command_type, current.job_id, KEY_DIGEST) == receipt


def test_invalid_atomic_bundle_changes_nothing() -> None:
    store = InMemorySellerControlStore()
    current, _receipt, _work = create_seed_job(store)
    updated = current.model_copy(
        update={
            "state": ControlJobState.ANALYZING_ARTWORK,
            "record_version": 1,
            "event_sequence": 2,
            "updated_at": NOW + timedelta(seconds=1),
        }
    )
    receipt = make_receipt(updated, receipt_id="receipt_invalid", command_type="prepare")
    invalid = CommandCommit(
        current=current,
        updated=updated,
        event=DomainEvent(
            job_id=current.job_id,
            sequence=99,
            name="PREPARATION_STARTED",
            occurred_at=updated.updated_at,
        ),
        receipt=receipt,
    )

    with pytest.raises(InvalidControlStateError, match="event"):
        store.commit_command(invalid)

    assert store.get_job(current.job_id) == current
    assert store.list_events(current.job_id) == (make_event(current),)
    assert store.resolve_receipt(OWNER, "prepare", current.job_id, KEY_DIGEST) is None


def test_receipt_lookup_distinguishes_replay_from_changed_request() -> None:
    store = InMemorySellerControlStore()
    current, _receipt, _work = create_seed_job(store)
    updated = current.model_copy(
        update={
            "state": ControlJobState.ANALYZING_ARTWORK,
            "record_version": 1,
            "event_sequence": 2,
            "updated_at": NOW + timedelta(seconds=1),
        }
    )
    receipt = make_receipt(updated, receipt_id="receipt_prepare", command_type="prepare")
    commit = CommandCommit(
        current=current,
        updated=updated,
        event=make_event(updated, name="PREPARATION_REQUESTED"),
        receipt=receipt,
    )
    assert store.commit_command(commit) == receipt

    # The exact request is replay-safe even though its original Job CAS is now stale.
    assert store.commit_command(commit) == receipt
    assert len(store.list_events(current.job_id)) == 2

    conflicting = CommandCommit(
        current=current,
        updated=updated,
        event=commit.event,
        receipt=receipt.model_copy(update={"request_fingerprint": "9" * 64}),
    )
    with pytest.raises(IdempotencyConflictError, match="another request"):
        store.commit_command(conflicting)


def test_nudge_moves_only_pending_future_work_due_now_without_duplication() -> None:
    store = InMemorySellerControlStore()
    future = NOW + timedelta(hours=1)
    job, _receipt, work = create_seed_job(store, work_due=future)
    assert work is not None

    nudged = store.nudge_pending_work(job.job_id, work.work_request_id, now=NOW)

    assert nudged.next_dispatch_at == NOW
    assert nudged.status is WorkRequestStatus.PENDING
    assert store.list_work_requests(job.job_id) == (nudged,)
    assert store.list_due_work(now=NOW) == (nudged,)


def test_pre_review_cancellation_commits_without_review_fingerprint() -> None:
    store = InMemorySellerControlStore()
    current, _receipt, work = create_seed_job(store)
    assert work is not None
    cancelled = current.model_copy(
        update={
            "state": ControlJobState.CANCELLED,
            "record_version": 1,
            "event_sequence": 2,
            "cancellation_requested_at": NOW + timedelta(seconds=1),
            "active_work_request_id": None,
            "updated_at": NOW + timedelta(seconds=1),
        }
    )
    receipt = make_receipt(cancelled, receipt_id="receipt_cancel", command_type="cancel_job")
    decision = CancellationDecisionRecord(
        decision_id="decision_cancel",
        job_id=current.job_id,
        actor_owner_id=OWNER,
        expected_record_version=0,
        command_receipt_id=receipt.receipt_id,
        decided_at=cancelled.updated_at,
    )
    event = make_event(cancelled, name="CANCELLATION_DECIDED")
    cancelled_work = WorkRequest.model_validate(
        {
            **work.model_dump(mode="python"),
            "status": WorkRequestStatus.CANCELLED,
            "updated_at": cancelled.updated_at,
        }
    )

    store.commit_command(
        CommandCommit(
            current=current,
            updated=cancelled,
            event=event,
            receipt=receipt,
            cancellation_decision=decision,
            work_update=(work, cancelled_work),
        )
    )

    assert store.get_job(current.job_id).state is ControlJobState.CANCELLED
    assert store.list_cancellation_decisions(current.job_id) == (decision,)
    assert decision.review_version is None
    assert decision.review_fingerprint is None


def test_validate_command_rejects_work_not_bound_to_job_and_receipt() -> None:
    store = InMemorySellerControlStore()
    current, _receipt, _work = create_seed_job(store)
    updated = current.model_copy(
        update={
            "state": ControlJobState.ANALYZING_ARTWORK,
            "record_version": 1,
            "event_sequence": 2,
            "active_work_request_id": "work_prepare",
            "updated_at": NOW + timedelta(seconds=1),
        }
    )
    receipt = make_receipt(
        updated,
        receipt_id="receipt_prepare",
        command_type="prepare",
        work_id="work_prepare",
    )
    wrong_work = make_work(updated, receipt, due=NOW).model_copy(
        update={"receipt_id": "receipt_wrong"}
    )
    commit = CommandCommit(
        current=current,
        updated=updated,
        event=make_event(updated, name="PREPARATION_REQUESTED"),
        receipt=receipt,
        work_request=wrong_work,
    )

    with pytest.raises(InvalidControlStateError, match="work request"):
        validate_command_commit(commit)
