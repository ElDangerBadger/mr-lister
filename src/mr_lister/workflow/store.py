"""Application-owned persistence boundary and its in-memory implementation."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping
from datetime import datetime
from types import MappingProxyType
from typing import Protocol

from mr_lister.contracts import (
    ArtworkAnalysis,
    JobRecord,
    ListingIntelligence,
    ReviewSnapshot,
    can_transition,
)
from mr_lister.workflow.errors import (
    ConcurrentModificationError,
    IdempotencyConflictError,
    InvalidStateError,
    JobNotFoundError,
)
from mr_lister.workflow.models import (
    ApprovalWaitRecord,
    ApprovalWaitStatus,
    ArtworkInput,
    ExternalWriteClaim,
    ExternalWriteRecord,
    ExternalWriteStatus,
    WorkflowEvent,
)


def validate_transition_commit(
    *,
    current: JobRecord,
    updated: JobRecord,
    event: WorkflowEvent,
    review: ReviewSnapshot | None,
) -> None:
    """Validate one application-owned transition before any adapter commits it."""

    if updated.job_id != current.job_id:
        raise InvalidStateError("A transition cannot change the job identifier")
    if updated.record_version != current.record_version + 1:
        raise InvalidStateError("A transition must increment record_version exactly once")
    if updated.event_sequence != current.event_sequence + 1:
        raise InvalidStateError("A transition must increment event_sequence exactly once")
    if event.sequence != updated.event_sequence:
        raise InvalidStateError("Transition event sequence does not match the job record")
    if not can_transition(current.state, updated.state):
        raise InvalidStateError(f"Cannot transition from {current.state} to {updated.state}")

    immutable_fields = (
        "contract_version",
        "idempotency_key",
        "artwork_object_key",
        "created_at",
    )
    if any(getattr(updated, field) != getattr(current, field) for field in immutable_fields):
        raise InvalidStateError("A transition attempted to change immutable job identity")
    if review is not None and review.review_version != updated.review_version:
        raise InvalidStateError("Committed review does not match the job review version")


class JobStore(Protocol):
    """Persistence operations required by the application workflow.

    Orchestrators may request work, but only this application-facing boundary may commit
    state. Durable implementations must enforce ``record_version`` with an atomic conditional
    write rather than a read-then-write sequence.
    """

    def resolve_intake(
        self, idempotency_key: str, request_fingerprint: str
    ) -> JobRecord | None: ...

    def create_intake(
        self,
        *,
        job: JobRecord,
        artwork: ArtworkInput,
        profile_id: str,
        request_fingerprint: str,
        event: WorkflowEvent,
    ) -> tuple[JobRecord, bool]: ...

    def get_job(self, job_id: str) -> JobRecord: ...

    def commit_transition(
        self,
        *,
        current: JobRecord,
        updated: JobRecord,
        event: WorkflowEvent,
        review: ReviewSnapshot | None = None,
        approval_wait: tuple[ApprovalWaitRecord, ApprovalWaitRecord] | None = None,
    ) -> JobRecord: ...

    def get_artwork(self, job_id: str) -> ArtworkInput: ...

    def get_profile_id(self, job_id: str) -> str: ...

    def get_analysis_checkpoint(self, job_id: str) -> ArtworkAnalysis | None: ...

    def save_analysis_checkpoint(
        self, job_id: str, analysis: ArtworkAnalysis
    ) -> ArtworkAnalysis: ...

    def get_listing_checkpoint(self, job_id: str) -> ListingIntelligence | None: ...

    def save_listing_checkpoint(
        self, job_id: str, listing: ListingIntelligence
    ) -> ListingIntelligence: ...

    def has_review(self, job_id: str) -> bool: ...

    def get_review(self, job_id: str) -> ReviewSnapshot: ...

    def append_event(
        self, job_id: str, *, occurred_at: datetime, name: str, details: dict[str, object]
    ) -> WorkflowEvent: ...

    def list_events(self, job_id: str) -> tuple[WorkflowEvent, ...]: ...

    def claim_external_write(
        self, job_id: str, claim: ExternalWriteClaim
    ) -> tuple[ExternalWriteClaim, bool]: ...

    def complete_external_write(
        self,
        job_id: str,
        *,
        idempotency_key: str,
        request_fingerprint: str,
        result: dict[str, str],
        completed_at: datetime,
    ) -> ExternalWriteRecord: ...

    def require_external_write_reconciliation(
        self, job_id: str, *, idempotency_key: str, request_fingerprint: str
    ) -> ExternalWriteClaim: ...

    def list_external_writes(self, job_id: str) -> tuple[ExternalWriteRecord, ...]: ...

    def register_approval_wait(self, wait: ApprovalWaitRecord) -> ApprovalWaitRecord: ...

    def get_approval_wait(self, job_id: str) -> ApprovalWaitRecord | None: ...


class InMemoryJobStore:
    """Deterministic local adapter mirroring DynamoDB conditional-write behavior."""

    def __init__(self) -> None:
        self._jobs: dict[str, JobRecord] = {}
        self._artworks: dict[str, ArtworkInput] = {}
        self._profile_ids: dict[str, str] = {}
        self._analyses: dict[str, ArtworkAnalysis] = {}
        self._listing_checkpoints: dict[str, ListingIntelligence] = {}
        self._reviews: dict[str, ReviewSnapshot] = {}
        self._events: dict[str, list[WorkflowEvent]] = defaultdict(list)
        self._external_write_claims: dict[str, dict[str, ExternalWriteClaim]] = defaultdict(dict)
        self._intake_keys: dict[str, tuple[str, str]] = {}
        self._approval_waits: dict[str, ApprovalWaitRecord] = {}

    @property
    def jobs(self) -> Mapping[str, JobRecord]:
        return MappingProxyType(self._jobs)

    @property
    def artworks(self) -> Mapping[str, ArtworkInput]:
        return MappingProxyType(self._artworks)

    @property
    def reviews(self) -> Mapping[str, ReviewSnapshot]:
        return MappingProxyType(self._reviews)

    @property
    def events(self) -> Mapping[str, list[WorkflowEvent]]:
        return MappingProxyType(self._events)

    @property
    def external_writes(self) -> Mapping[str, list[ExternalWriteRecord]]:
        records = {job_id: list(self.list_external_writes(job_id)) for job_id in self._jobs}
        return MappingProxyType(records)

    def create_intake(
        self,
        *,
        job: JobRecord,
        artwork: ArtworkInput,
        profile_id: str,
        request_fingerprint: str,
        event: WorkflowEvent,
    ) -> tuple[JobRecord, bool]:
        existing = self._intake_keys.get(job.idempotency_key)
        if existing is not None:
            existing_fingerprint, existing_job_id = existing
            if existing_fingerprint != request_fingerprint:
                raise IdempotencyConflictError("Idempotency key was already used for other artwork")
            return self.get_job(existing_job_id), False
        if job.job_id in self._jobs:
            raise IdempotencyConflictError("Generated job identifier already exists")
        if job.event_sequence != 1 or event.sequence != 1:
            raise InvalidStateError("A new intake must atomically create its first event")

        self._jobs[job.job_id] = job
        self._artworks[job.job_id] = artwork
        self._profile_ids[job.job_id] = profile_id
        self._events[job.job_id].append(event)
        self._intake_keys[job.idempotency_key] = (request_fingerprint, job.job_id)
        return job, True

    def resolve_intake(self, idempotency_key: str, request_fingerprint: str) -> JobRecord | None:
        existing = self._intake_keys.get(idempotency_key)
        if existing is None:
            return None
        existing_fingerprint, existing_job_id = existing
        if existing_fingerprint != request_fingerprint:
            raise IdempotencyConflictError("Idempotency key was already used for other artwork")
        return self.get_job(existing_job_id)

    def get_job(self, job_id: str) -> JobRecord:
        try:
            return self._jobs[job_id]
        except KeyError as error:
            raise JobNotFoundError(f"Unknown job: {job_id}") from error

    def commit_transition(
        self,
        *,
        current: JobRecord,
        updated: JobRecord,
        event: WorkflowEvent,
        review: ReviewSnapshot | None = None,
        approval_wait: tuple[ApprovalWaitRecord, ApprovalWaitRecord] | None = None,
    ) -> JobRecord:
        persisted = self.get_job(current.job_id)
        if persisted.record_version != current.record_version or persisted != current:
            raise ConcurrentModificationError("Job changed before transition could be committed")
        validate_transition_commit(
            current=current,
            updated=updated,
            event=event,
            review=review,
        )
        if review is not None:
            existing = self._reviews.get(current.job_id)
            if existing is not None and existing.review_version == review.review_version:
                immutable_existing = existing.model_dump(
                    exclude={"approval_status", "printify_product_id"}
                )
                immutable_review = review.model_dump(
                    exclude={"approval_status", "printify_product_id"}
                )
                if immutable_existing != immutable_review:
                    raise InvalidStateError("A persisted review version is immutable")
        if approval_wait is not None:
            expected_wait, consumed_wait = approval_wait
            persisted_wait = self._approval_waits.get(current.job_id)
            if persisted_wait != expected_wait:
                raise ConcurrentModificationError("Approval wait changed before approval")
            if (
                expected_wait.status is not ApprovalWaitStatus.PENDING
                or consumed_wait.status is not ApprovalWaitStatus.CONSUMED
                or consumed_wait.review_version != updated.approved_review_version
                or consumed_wait.job_id != updated.job_id
                or consumed_wait.consumed_at is None
                or consumed_wait.consumed_at >= expected_wait.expires_at
            ):
                raise InvalidStateError("Approval wait consumption does not match approval")
        self._jobs[current.job_id] = updated
        if review is not None:
            self._reviews[current.job_id] = review
        if approval_wait is not None:
            self._approval_waits[current.job_id] = approval_wait[1]
        self._events[current.job_id].append(event)
        return updated

    def get_artwork(self, job_id: str) -> ArtworkInput:
        self.get_job(job_id)
        return self._artworks[job_id]

    def get_profile_id(self, job_id: str) -> str:
        self.get_job(job_id)
        return self._profile_ids[job_id]

    def get_analysis_checkpoint(self, job_id: str) -> ArtworkAnalysis | None:
        self.get_job(job_id)
        return self._analyses.get(job_id)

    def save_analysis_checkpoint(self, job_id: str, analysis: ArtworkAnalysis) -> ArtworkAnalysis:
        self.get_job(job_id)
        existing = self._analyses.get(job_id)
        if existing is not None and existing != analysis:
            raise ConcurrentModificationError("Artwork analysis checkpoint already differs")
        self._analyses[job_id] = analysis
        return analysis

    def get_listing_checkpoint(self, job_id: str) -> ListingIntelligence | None:
        self.get_job(job_id)
        return self._listing_checkpoints.get(job_id)

    def save_listing_checkpoint(
        self, job_id: str, listing: ListingIntelligence
    ) -> ListingIntelligence:
        self.get_job(job_id)
        existing = self._listing_checkpoints.get(job_id)
        if existing is not None and existing != listing:
            raise ConcurrentModificationError("Listing checkpoint already differs")
        self._listing_checkpoints[job_id] = listing
        return listing

    def has_review(self, job_id: str) -> bool:
        self.get_job(job_id)
        return job_id in self._reviews

    def get_review(self, job_id: str) -> ReviewSnapshot:
        self.get_job(job_id)
        return self._reviews[job_id]

    def append_event(
        self, job_id: str, *, occurred_at: datetime, name: str, details: dict[str, object]
    ) -> WorkflowEvent:
        self.get_job(job_id)
        events = self._events[job_id]
        current = self._jobs[job_id]
        event = WorkflowEvent(
            sequence=len(events) + 1,
            occurred_at=occurred_at,
            name=name,
            details=details,
        )
        if event.sequence != current.event_sequence + 1:
            raise ConcurrentModificationError("Job event sequence changed before append")
        self._jobs[job_id] = current.model_copy(update={"event_sequence": event.sequence})
        events.append(event)
        return event

    def list_events(self, job_id: str) -> tuple[WorkflowEvent, ...]:
        self.get_job(job_id)
        return tuple(self._events[job_id])

    def claim_external_write(
        self, job_id: str, claim: ExternalWriteClaim
    ) -> tuple[ExternalWriteClaim, bool]:
        self.get_job(job_id)
        existing = self._external_write_claims[job_id].get(claim.idempotency_key)
        if existing is not None:
            if existing.request_fingerprint != claim.request_fingerprint:
                raise InvalidStateError("External write idempotency fingerprint changed")
            return existing, False
        self._external_write_claims[job_id][claim.idempotency_key] = claim
        return claim, True

    def complete_external_write(
        self,
        job_id: str,
        *,
        idempotency_key: str,
        request_fingerprint: str,
        result: dict[str, str],
        completed_at: datetime,
    ) -> ExternalWriteRecord:
        self.get_job(job_id)
        claim = self._external_write_claims[job_id].get(idempotency_key)
        if claim is None or claim.request_fingerprint != request_fingerprint:
            raise InvalidStateError("External write was not claimed with this fingerprint")
        if claim.status is ExternalWriteStatus.COMPLETED:
            if claim.result != result:
                raise InvalidStateError("Completed external write result changed")
        else:
            claim = claim.model_copy(
                update={
                    "status": ExternalWriteStatus.COMPLETED,
                    "result": result,
                    "completed_at": completed_at,
                }
            )
            self._external_write_claims[job_id][idempotency_key] = claim
        return ExternalWriteRecord(
            operation=claim.operation,
            idempotency_key=claim.idempotency_key,
            request_fingerprint=claim.request_fingerprint,
            external_id=result["external_id"],
            occurred_at=claim.completed_at,
        )

    def list_external_writes(self, job_id: str) -> tuple[ExternalWriteRecord, ...]:
        self.get_job(job_id)
        claims = self._external_write_claims[job_id].values()
        return tuple(
            ExternalWriteRecord(
                operation=claim.operation,
                idempotency_key=claim.idempotency_key,
                request_fingerprint=claim.request_fingerprint,
                external_id=claim.result["external_id"],
                occurred_at=claim.completed_at,
            )
            for claim in claims
            if claim.status is ExternalWriteStatus.COMPLETED
            and claim.result is not None
            and claim.completed_at is not None
        )

    def require_external_write_reconciliation(
        self, job_id: str, *, idempotency_key: str, request_fingerprint: str
    ) -> ExternalWriteClaim:
        self.get_job(job_id)
        claim = self._external_write_claims[job_id].get(idempotency_key)
        if claim is None or claim.request_fingerprint != request_fingerprint:
            raise InvalidStateError("External write claim is unavailable for reconciliation")
        if claim.status is ExternalWriteStatus.COMPLETED:
            return claim
        reconciled = claim.model_copy(
            update={"status": ExternalWriteStatus.RECONCILIATION_REQUIRED}
        )
        self._external_write_claims[job_id][idempotency_key] = reconciled
        return reconciled

    def register_approval_wait(self, wait: ApprovalWaitRecord) -> ApprovalWaitRecord:
        job = self.get_job(wait.job_id)
        if job.state.value != "awaiting_approval" or job.review_version != wait.review_version:
            raise InvalidStateError("Approval wait does not match the reviewable job version")
        existing = self._approval_waits.get(wait.job_id)
        if existing is not None:
            if (
                existing.review_version != wait.review_version
                or existing.task_token != wait.task_token
            ):
                raise InvalidStateError("A different approval wait is already registered")
            return existing
        self._approval_waits[wait.job_id] = wait
        return wait

    def get_approval_wait(self, job_id: str) -> ApprovalWaitRecord | None:
        self.get_job(job_id)
        return self._approval_waits.get(job_id)
