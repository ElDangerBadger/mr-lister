"""Atomic persistence boundary for Phase 6 seller-control commands."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from threading import RLock
from types import MappingProxyType
from typing import Protocol

from mr_lister.control.dispatch import deterministic_execution_name, work_input_fingerprint
from mr_lister.control.errors import (
    ConcurrentControlModificationError,
    IdempotencyConflictError,
    InvalidControlStateError,
    NotFoundError,
)
from mr_lister.control.fingerprints import review_etag
from mr_lister.control.models import (
    CONTROL_NEW_WORK_BY_STATE,
    CONTROL_RECOVERY_BINDINGS,
    CancellationDecisionRecord,
    CommandReceipt,
    ControlJobRecord,
    ControlJobState,
    DomainEvent,
    FailureRecord,
    PricingSnapshot,
    ProductSyncRecord,
    ReviewContent,
    ReviewDecision,
    ReviewDecisionRecord,
    WorkRequest,
    WorkRequestStatus,
    WorkType,
    can_control_transition,
)

_WORK_IDENTITY_FIELDS = (
    "contract_version",
    "work_request_id",
    "owner_id",
    "job_id",
    "receipt_id",
    "work_type",
    "review_version",
    "input_fingerprint",
    "execution_name",
    "created_at",
)
_COMMAND_WORK_TRANSITIONS = frozenset(
    {
        (WorkRequestStatus.PENDING, WorkRequestStatus.CANCELLED),
        (WorkRequestStatus.CLAIMED, WorkRequestStatus.COMPLETED),
        (WorkRequestStatus.DISPATCHED, WorkRequestStatus.COMPLETED),
    }
)


@dataclass(frozen=True)
class CommandCommit:
    """One all-or-nothing application command transaction."""

    current: ControlJobRecord
    updated: ControlJobRecord
    event: DomainEvent
    receipt: CommandReceipt
    review: ReviewContent | None = None
    review_decision: ReviewDecisionRecord | None = None
    cancellation_decision: CancellationDecisionRecord | None = None
    product_sync: ProductSyncRecord | None = None
    pricing_snapshot: PricingSnapshot | None = None
    failure: FailureRecord | None = None
    work_request: WorkRequest | None = None
    work_update: tuple[WorkRequest, WorkRequest] | None = None


def revalidate_work_request(current: WorkRequest, **updates: object) -> WorkRequest:
    """Apply an operational update without bypassing the strict work contract."""

    payload = current.model_dump(mode="python")
    payload.update(updates)
    return WorkRequest.model_validate(payload)


def validate_new_work_request(job: ControlJobRecord, work: WorkRequest) -> None:
    """Prove a new outbox record is the only work authorized by the resulting state."""

    if work.status is not WorkRequestStatus.PENDING:
        raise InvalidControlStateError("New work must begin pending")
    if (
        work.attempt_count != 0
        or work.claim_id is not None
        or work.lease_expires_at is not None
        or work.execution_arn is not None
        or work.last_error_code is not None
        or work.updated_at != work.created_at
    ):
        raise InvalidControlStateError("New work must begin with pristine dispatch authority")
    if CONTROL_NEW_WORK_BY_STATE.get(job.state) is not work.work_type:
        raise InvalidControlStateError("The work type does not match the resulting job state")
    if work.work_type is not WorkType.PREPARE and (
        job.review_version < 1 or work.review_version != job.review_version
    ):
        raise InvalidControlStateError("Provider work must bind the exact current review")
    if work.review_version is not None and work.review_version != job.review_version:
        raise InvalidControlStateError("The work request does not bind the current review")
    if work.execution_name != deterministic_execution_name(work.work_request_id):
        raise InvalidControlStateError("The work execution name is not deterministic")
    if work.input_fingerprint != work_input_fingerprint(
        work_type=work.work_type,
        job_id=work.job_id,
        work_request_id=work.work_request_id,
    ):
        raise InvalidControlStateError("The work input fingerprint is not deterministic")


def validate_initial_job(
    job: ControlJobRecord,
    event: DomainEvent,
    receipt: CommandReceipt,
    work: WorkRequest | None,
) -> None:
    """Restrict job creation to the upload-completion intake transaction."""

    if job.state is not ControlJobState.INTAKE_VALIDATED:
        raise InvalidControlStateError("A new job must begin in INTAKE_VALIDATED")
    if job.record_version != 0 or job.event_sequence != 1 or event.sequence != 1:
        raise InvalidControlStateError("A new job must atomically create its first event")
    if (
        job.review_version != 0
        or job.review_fingerprint is not None
        or job.review_validated
        or job.product_id is not None
        or job.product_sync_id is not None
        or job.pricing_snapshot_id is not None
        or job.approval_fingerprint is not None
        or job.failure_id is not None
        or job.cancellation_requested_at is not None
        or job.provider_outcome_unconfirmed
        or job.updated_at != job.created_at
    ):
        raise InvalidControlStateError("A new intake job must begin with pristine authority")
    if work is None:
        raise InvalidControlStateError("INTAKE_VALIDATED requires its preparation work")
    if work.work_type is not WorkType.PREPARE or work.review_version is not None:
        raise InvalidControlStateError("Initial work type must be unbound PREPARE work")
    if (
        event.job_id != job.job_id
        or receipt.owner_id != job.owner_id
        or receipt.job_id != job.job_id
        or receipt.response.job_id != job.job_id
        or receipt.response.state is not job.state
        or receipt.response.record_version != job.record_version
        or receipt.response.review_version != job.review_version
        or work.job_id != job.job_id
        or work.owner_id != job.owner_id
        or work.receipt_id != receipt.receipt_id
        or job.active_work_request_id != work.work_request_id
        or receipt.work_request_id != work.work_request_id
        or receipt.response.work_request_id != work.work_request_id
    ):
        raise InvalidControlStateError("The initial event, receipt, work, and job do not match")
    validate_new_work_request(job, work)


def validate_command_commit(commit: CommandCommit) -> None:
    """Validate the application-owned transition before an adapter writes anything."""

    current = commit.current
    updated = commit.updated
    if updated.job_id != current.job_id or updated.owner_id != current.owner_id:
        raise InvalidControlStateError("A command cannot change job identity or ownership")
    if updated.record_version != current.record_version + 1:
        raise InvalidControlStateError("A command must increment record_version exactly once")
    if updated.event_sequence != current.event_sequence + 1:
        raise InvalidControlStateError("A command must increment event_sequence exactly once")
    if updated.created_at != current.created_at:
        raise InvalidControlStateError("A command cannot change job creation time")
    if updated.updated_at < current.updated_at:
        raise InvalidControlStateError("A command cannot move job time backwards")
    if not can_control_transition(current.state, updated.state):
        raise InvalidControlStateError(
            f"Cannot transition from {current.state.value} to {updated.state.value}"
        )
    if updated.state in CONTROL_NEW_WORK_BY_STATE and updated.active_work_request_id is None:
        raise InvalidControlStateError("The resulting machine state requires durable work")
    if current.cancellation_requested_at is not None:
        if updated.cancellation_requested_at != current.cancellation_requested_at:
            raise InvalidControlStateError("Cancellation intent is immutable")
        if updated.state not in {
            ControlJobState.CANCEL_REQUESTED,
            ControlJobState.RECONCILIATION_REQUIRED,
            ControlJobState.CANCELLED,
        }:
            raise InvalidControlStateError("Cancellation intent forbids normal job recovery")
    elif updated.cancellation_requested_at is not None:
        if commit.cancellation_decision is None:
            raise InvalidControlStateError("Cancellation intent requires an immutable decision")
    elif commit.cancellation_decision is not None:
        raise InvalidControlStateError("A cancellation decision must establish intent")
    if commit.event.job_id != updated.job_id or commit.event.sequence != updated.event_sequence:
        raise InvalidControlStateError("The event does not match the committed job sequence")
    receipt = commit.receipt
    if receipt.owner_id != updated.owner_id or receipt.job_id != updated.job_id:
        raise InvalidControlStateError("The command receipt does not match the job owner")
    if receipt.response.job_id != updated.job_id:
        raise InvalidControlStateError("The receipt response does not match the job")
    if (
        receipt.response.state is not updated.state
        or receipt.response.record_version != updated.record_version
        or receipt.response.review_version != updated.review_version
    ):
        raise InvalidControlStateError("The receipt response does not match the committed result")

    if commit.review is not None:
        review = commit.review
        if review.job_id != updated.job_id:
            raise InvalidControlStateError("The review does not match the job")
        if review.review_version != current.review_version + 1:
            raise InvalidControlStateError(
                "A new review must increment review_version exactly once"
            )
        if (
            updated.review_version != review.review_version
            or updated.review_fingerprint != review.fingerprint
            or updated.review_validated != review.validation_passed
        ):
            raise InvalidControlStateError("The job does not point to the committed review")
    elif (
        updated.review_version != current.review_version
        or updated.review_fingerprint != current.review_fingerprint
        or updated.review_validated != current.review_validated
    ):
        raise InvalidControlStateError("Review authority changed without an immutable review")

    if commit.review_decision is not None:
        decision = commit.review_decision
        if (
            decision.job_id != updated.job_id
            or decision.actor_owner_id != updated.owner_id
            or decision.command_receipt_id != receipt.receipt_id
        ):
            raise InvalidControlStateError("The review decision does not match the command")
        if (
            decision.review_version != current.review_version
            or decision.review_fingerprint != current.review_fingerprint
        ):
            raise InvalidControlStateError("The decision does not bind the exact current review")
        if decision.approval_fingerprint != updated.approval_fingerprint:
            raise InvalidControlStateError("The decision does not bind the committed approval")
        if decision.decision is ReviewDecision.APPROVE:
            if updated.state is not ControlJobState.APPROVED or commit.review is not None:
                raise InvalidControlStateError("Approval decision does not match the transition")
        elif current.review_version == 0 or commit.review is None:
            raise InvalidControlStateError("Revision decision requires a prior and new review")
    if commit.cancellation_decision is not None:
        decision = commit.cancellation_decision
        if (
            decision.job_id != updated.job_id
            or decision.actor_owner_id != updated.owner_id
            or decision.command_receipt_id != receipt.receipt_id
            or decision.expected_record_version != current.record_version
        ):
            raise InvalidControlStateError("The cancellation decision does not match the command")
        if (
            decision.review_version != (current.review_version or None)
            or decision.review_fingerprint != current.review_fingerprint
        ):
            raise InvalidControlStateError(
                "The cancellation decision does not bind the current review"
            )
        if decision.decided_at != updated.cancellation_requested_at:
            raise InvalidControlStateError("Cancellation intent and decision time must match")

    if current.product_id is not None and updated.product_id != current.product_id:
        raise InvalidControlStateError("A job cannot replace or clear its provider product")
    if (
        current.product_id is None
        and updated.product_id is not None
        and commit.product_sync is None
    ):
        raise InvalidControlStateError("The first provider product requires synchronization proof")
    current_sync_authority = (
        current.product_sync_id,
        current.synchronized_review_version,
        current.product_sync_fingerprint,
    )
    updated_sync_authority = (
        updated.product_sync_id,
        updated.synchronized_review_version,
        updated.product_sync_fingerprint,
    )
    if current_sync_authority != updated_sync_authority:
        if updated.product_sync_id is None:
            if commit.review is None:
                raise InvalidControlStateError(
                    "Synchronization authority may clear only for a new review"
                )
        elif commit.product_sync is None:
            raise InvalidControlStateError(
                "New synchronization authority requires an immutable record"
            )
    elif commit.product_sync is not None:
        raise InvalidControlStateError("Synchronization proof must advance job authority")
    if commit.product_sync is not None:
        sync = commit.product_sync
        if (
            sync.job_id != updated.job_id
            or updated.product_sync_id != sync.sync_id
            or updated.product_id != sync.product_id
            or updated.synchronized_review_version != sync.review_version
            or updated.product_sync_fingerprint != sync.fingerprint
        ):
            raise InvalidControlStateError("The product synchronization does not match the job")
    if commit.pricing_snapshot is not None:
        pricing = commit.pricing_snapshot
        if (
            pricing.job_id != updated.job_id
            or updated.pricing_snapshot_id != pricing.snapshot_id
            or updated.pricing_snapshot_fingerprint != pricing.fingerprint
            or pricing.review_version != updated.review_version
            or pricing.product_sync_fingerprint != updated.product_sync_fingerprint
        ):
            raise InvalidControlStateError("The pricing snapshot does not match the job")
    current_pricing_authority = (
        current.pricing_snapshot_id,
        current.pricing_snapshot_fingerprint,
    )
    updated_pricing_authority = (
        updated.pricing_snapshot_id,
        updated.pricing_snapshot_fingerprint,
    )
    if current_pricing_authority != updated_pricing_authority:
        if updated.pricing_snapshot_id is None:
            if (
                commit.review is None
                and commit.product_sync is None
                and updated.state is not ControlJobState.PRICING_REFRESHING
            ):
                raise InvalidControlStateError(
                    "Pricing authority may clear only for review, sync, or refresh work"
                )
        elif commit.pricing_snapshot is None:
            raise InvalidControlStateError("New pricing authority requires an immutable snapshot")
    elif commit.pricing_snapshot is not None:
        raise InvalidControlStateError("Pricing proof must advance job authority")
    if commit.failure is not None:
        failure = commit.failure
        if (
            failure.job_id != updated.job_id
            or updated.failure_id != failure.failure_id
            or failure.stage is not current.state
            or commit.work_update is None
        ):
            raise InvalidControlStateError("The failure record does not match the job")
        expected_work, changed_work = commit.work_update
        if (
            failure.work_request_id != expected_work.work_request_id
            or changed_work.last_error_code != failure.code
        ):
            raise InvalidControlStateError("The failure does not bind the settled work")
        expected_failure_state = (
            ControlJobState.FAILED_RETRYABLE
            if failure.retryable
            else ControlJobState.FAILED_TERMINAL
        )
        if updated.state is not expected_failure_state:
            raise InvalidControlStateError("Failure retryability does not match the job state")
        if failure.retryable and CONTROL_RECOVERY_BINDINGS.get(failure.recovery_action) != (
            failure.resume_state,
            failure.work_type,
        ):
            raise InvalidControlStateError("The failure recovery specification is not allowed")
        if failure.retryable and failure.work_type is not expected_work.work_type:
            raise InvalidControlStateError("Recovery work does not match the failed operation")

    if updated.state is ControlJobState.APPROVED and (
        commit.review_decision is None
        or commit.review_decision.decision is not ReviewDecision.APPROVE
    ):
        raise InvalidControlStateError("Approval requires an immutable approval decision")
    if updated.state is ControlJobState.APPROVED:
        expected_approval = review_etag(
            job_id=current.job_id,
            review_version=current.review_version,
            review_fingerprint=current.review_fingerprint or "",
            product_id=current.product_id,
            product_sync_fingerprint=current.product_sync_fingerprint,
            pricing_snapshot_id=current.pricing_snapshot_id,
            pricing_snapshot_fingerprint=current.pricing_snapshot_fingerprint,
        )
        if (
            updated.approval_fingerprint != expected_approval
            or commit.review_decision is None
            or commit.review_decision.approval_fingerprint != expected_approval
        ):
            raise InvalidControlStateError("Approval does not bind the exact composite authority")
    if (
        commit.review is not None
        and current.review_version > 0
        and (
            commit.review_decision is None
            or commit.review_decision.decision is not ReviewDecision.REVISE
        )
    ):
        raise InvalidControlStateError("A seller revision requires an immutable decision")
    if updated.failure_id != current.failure_id:
        if updated.failure_id is not None and commit.failure is None:
            raise InvalidControlStateError("A failure transition requires an immutable record")
        if updated.failure_id is None and commit.failure is not None:
            raise InvalidControlStateError("A cleared failure cannot create another failure record")

    if commit.work_request is not None:
        work = commit.work_request
        if (
            work.job_id != updated.job_id
            or work.owner_id != updated.owner_id
            or work.receipt_id != receipt.receipt_id
            or updated.active_work_request_id != work.work_request_id
            or receipt.work_request_id != work.work_request_id
            or receipt.response.work_request_id != work.work_request_id
        ):
            raise InvalidControlStateError("The work request does not match the command result")
        validate_new_work_request(updated, work)
        if work.review_version is not None and work.review_version != updated.review_version:
            raise InvalidControlStateError("The work request does not bind the current review")
    elif receipt.work_request_id is not None or receipt.response.work_request_id is not None:
        raise InvalidControlStateError("A receipt cannot reference absent new work")

    if commit.work_update is not None:
        expected, changed = commit.work_update
        if (
            expected.work_request_id != changed.work_request_id
            or expected.job_id != updated.job_id
            or changed.job_id != updated.job_id
            or expected.owner_id != updated.owner_id
            or changed.owner_id != updated.owner_id
            or current.active_work_request_id != expected.work_request_id
        ):
            raise InvalidControlStateError("The work update does not match the job")
        if (expected.status, changed.status) not in _COMMAND_WORK_TRANSITIONS:
            raise InvalidControlStateError("The command uses an illegal work status transition")
        if any(
            getattr(expected, field) != getattr(changed, field) for field in _WORK_IDENTITY_FIELDS
        ):
            raise InvalidControlStateError("A command cannot change work identity")
        if changed.attempt_count != expected.attempt_count:
            raise InvalidControlStateError("A command cannot change dispatch attempts")
        if changed.execution_arn != expected.execution_arn:
            raise InvalidControlStateError("A command cannot change execution identity")
        if changed.updated_at < expected.updated_at:
            raise InvalidControlStateError("A work update cannot move time backwards")
        expected_active = (
            None if commit.work_request is None else commit.work_request.work_request_id
        )
        if updated.active_work_request_id != expected_active:
            raise InvalidControlStateError("The job does not point to the resulting active work")

    if current.active_work_request_id != updated.active_work_request_id:
        if current.active_work_request_id is not None and commit.work_update is None:
            raise InvalidControlStateError("Active work changed without settling prior work")
        if updated.active_work_request_id is not None and commit.work_request is None:
            raise InvalidControlStateError("Active work changed without immutable new work")
    elif commit.work_request is not None or commit.work_update is not None:
        raise InvalidControlStateError("Work records changed without changing active work")
    elif (
        updated.active_work_request_id is not None
        and updated.state is not ControlJobState.CANCEL_REQUESTED
    ):
        current_type = CONTROL_NEW_WORK_BY_STATE.get(current.state)
        updated_type = CONTROL_NEW_WORK_BY_STATE.get(updated.state)
        if current_type is None or updated_type is not current_type:
            raise InvalidControlStateError(
                "Retained work does not match the resulting machine state"
            )


class SellerControlStore(Protocol):
    def get_job(self, job_id: str) -> ControlJobRecord: ...

    def get_job_for_owner(self, owner_id: str, job_id: str) -> ControlJobRecord: ...

    def get_review(self, job_id: str, review_version: int) -> ReviewContent: ...

    def get_product_sync(self, job_id: str, sync_id: str) -> ProductSyncRecord: ...

    def get_pricing(self, job_id: str, snapshot_id: str) -> PricingSnapshot: ...

    def get_failure(self, job_id: str, failure_id: str) -> FailureRecord: ...

    def get_work_request(self, job_id: str, work_request_id: str) -> WorkRequest: ...

    def resolve_receipt(
        self, owner_id: str, command_type: str, job_id: str, key_digest: str
    ) -> CommandReceipt | None: ...

    def create_job(
        self,
        *,
        job: ControlJobRecord,
        event: DomainEvent,
        receipt: CommandReceipt,
        work_request: WorkRequest | None = None,
    ) -> CommandReceipt: ...

    def commit_command(self, commit: CommandCommit) -> CommandReceipt: ...

    def nudge_pending_work(
        self, job_id: str, work_request_id: str, *, now: datetime
    ) -> WorkRequest: ...


class InMemorySellerControlStore:
    """Thread-safe deterministic oracle for the Phase 6 DynamoDB transaction contract."""

    def __init__(self) -> None:
        self._lock = RLock()
        self._jobs: dict[str, ControlJobRecord] = {}
        self._reviews: dict[tuple[str, int], ReviewContent] = {}
        self._review_decisions: dict[str, ReviewDecisionRecord] = {}
        self._cancellation_decisions: dict[str, CancellationDecisionRecord] = {}
        self._product_syncs: dict[tuple[str, str], ProductSyncRecord] = {}
        self._pricing: dict[tuple[str, str], PricingSnapshot] = {}
        self._failures: dict[tuple[str, str], FailureRecord] = {}
        self._work: dict[tuple[str, str], WorkRequest] = {}
        self._events: dict[str, list[DomainEvent]] = defaultdict(list)
        self._receipts: dict[tuple[str, str, str, str], CommandReceipt] = {}

    @property
    def jobs(self) -> Mapping[str, ControlJobRecord]:
        return MappingProxyType(self._jobs)

    def get_job(self, job_id: str) -> ControlJobRecord:
        with self._lock:
            try:
                return self._jobs[job_id]
            except KeyError as error:
                raise NotFoundError("The requested job was not found") from error

    def get_job_for_owner(self, owner_id: str, job_id: str) -> ControlJobRecord:
        job = self.get_job(job_id)
        if job.owner_id != owner_id:
            raise NotFoundError("The requested job was not found")
        return job

    def get_review(self, job_id: str, review_version: int) -> ReviewContent:
        self.get_job(job_id)
        try:
            return self._reviews[(job_id, review_version)]
        except KeyError as error:
            raise NotFoundError("The requested review was not found") from error

    def get_product_sync(self, job_id: str, sync_id: str) -> ProductSyncRecord:
        self.get_job(job_id)
        try:
            return self._product_syncs[(job_id, sync_id)]
        except KeyError as error:
            raise NotFoundError("The requested product synchronization was not found") from error

    def get_pricing(self, job_id: str, snapshot_id: str) -> PricingSnapshot:
        self.get_job(job_id)
        try:
            return self._pricing[(job_id, snapshot_id)]
        except KeyError as error:
            raise NotFoundError("The requested pricing snapshot was not found") from error

    def get_failure(self, job_id: str, failure_id: str) -> FailureRecord:
        self.get_job(job_id)
        try:
            return self._failures[(job_id, failure_id)]
        except KeyError as error:
            raise NotFoundError("The requested failure was not found") from error

    def get_work_request(self, job_id: str, work_request_id: str) -> WorkRequest:
        self.get_job(job_id)
        try:
            return self._work[(job_id, work_request_id)]
        except KeyError as error:
            raise NotFoundError("The requested work request was not found") from error

    def resolve_receipt(
        self, owner_id: str, command_type: str, job_id: str, key_digest: str
    ) -> CommandReceipt | None:
        with self._lock:
            return self._receipts.get((owner_id, command_type, job_id, key_digest))

    def create_job(
        self,
        *,
        job: ControlJobRecord,
        event: DomainEvent,
        receipt: CommandReceipt,
        work_request: WorkRequest | None = None,
    ) -> CommandReceipt:
        with self._lock:
            validate_initial_job(job, event, receipt, work_request)
            key = self._receipt_key(receipt)
            existing_receipt = self._receipts.get(key)
            if existing_receipt is not None:
                if existing_receipt.request_fingerprint == receipt.request_fingerprint:
                    return existing_receipt
                raise IdempotencyConflictError(
                    "The idempotency key was already used for another request"
                )
            if job.job_id in self._jobs:
                raise IdempotencyConflictError("The generated job identifier already exists")
            if work_request is not None:
                if (job.job_id, work_request.work_request_id) in self._work:
                    raise IdempotencyConflictError("The work request already exists")
            self._jobs[job.job_id] = job
            self._events[job.job_id].append(event)
            self._receipts[key] = receipt
            if work_request is not None:
                self._work[(job.job_id, work_request.work_request_id)] = work_request
            return receipt

    def commit_command(self, commit: CommandCommit) -> CommandReceipt:
        validate_command_commit(commit)
        with self._lock:
            receipt_key = self._receipt_key(commit.receipt)
            existing_receipt = self._receipts.get(receipt_key)
            if existing_receipt is not None:
                if existing_receipt.request_fingerprint != commit.receipt.request_fingerprint:
                    raise IdempotencyConflictError(
                        "The idempotency key was already used for another request"
                    )
                return existing_receipt
            persisted = self._jobs.get(commit.current.job_id)
            if persisted != commit.current:
                raise ConcurrentControlModificationError(
                    "The job changed before the command could commit"
                )

            immutable_keys: list[tuple[object, object]] = []
            if commit.review is not None:
                immutable_keys.append(
                    (self._reviews, (commit.updated.job_id, commit.review.review_version))
                )
            if commit.review_decision is not None:
                immutable_keys.append((self._review_decisions, commit.review_decision.decision_id))
            if commit.cancellation_decision is not None:
                immutable_keys.append(
                    (self._cancellation_decisions, commit.cancellation_decision.decision_id)
                )
            if commit.product_sync is not None:
                immutable_keys.append(
                    (self._product_syncs, (commit.updated.job_id, commit.product_sync.sync_id))
                )
            if commit.pricing_snapshot is not None:
                immutable_keys.append(
                    (self._pricing, (commit.updated.job_id, commit.pricing_snapshot.snapshot_id))
                )
            if commit.failure is not None:
                immutable_keys.append(
                    (self._failures, (commit.updated.job_id, commit.failure.failure_id))
                )
            if commit.work_request is not None:
                immutable_keys.append(
                    (self._work, (commit.updated.job_id, commit.work_request.work_request_id))
                )
            if any(key in mapping for mapping, key in immutable_keys):
                raise IdempotencyConflictError("An immutable command record already exists")
            if commit.work_update is not None:
                expected, _changed = commit.work_update
                if self._work.get((expected.job_id, expected.work_request_id)) != expected:
                    raise ConcurrentControlModificationError(
                        "The work request changed before the command could commit"
                    )

            self._jobs[commit.updated.job_id] = commit.updated
            self._events[commit.updated.job_id].append(commit.event)
            self._receipts[receipt_key] = commit.receipt
            if commit.review is not None:
                self._reviews[(commit.updated.job_id, commit.review.review_version)] = commit.review
            if commit.review_decision is not None:
                self._review_decisions[commit.review_decision.decision_id] = commit.review_decision
            if commit.cancellation_decision is not None:
                self._cancellation_decisions[commit.cancellation_decision.decision_id] = (
                    commit.cancellation_decision
                )
            if commit.product_sync is not None:
                self._product_syncs[(commit.updated.job_id, commit.product_sync.sync_id)] = (
                    commit.product_sync
                )
            if commit.pricing_snapshot is not None:
                self._pricing[(commit.updated.job_id, commit.pricing_snapshot.snapshot_id)] = (
                    commit.pricing_snapshot
                )
            if commit.failure is not None:
                self._failures[(commit.updated.job_id, commit.failure.failure_id)] = commit.failure
            if commit.work_request is not None:
                self._work[(commit.updated.job_id, commit.work_request.work_request_id)] = (
                    commit.work_request
                )
            if commit.work_update is not None:
                _expected, changed = commit.work_update
                self._work[(changed.job_id, changed.work_request_id)] = changed
            return commit.receipt

    def nudge_pending_work(
        self, job_id: str, work_request_id: str, *, now: datetime
    ) -> WorkRequest:
        with self._lock:
            work = self.get_work_request(job_id, work_request_id)
            if work.status is not WorkRequestStatus.PENDING or work.next_dispatch_at <= now:
                return work
            nudged = revalidate_work_request(
                work,
                next_dispatch_at=now,
                updated_at=now,
            )
            self._work[(job_id, work_request_id)] = nudged
            return nudged

    def list_due_work(self, *, now: datetime, limit: int = 100) -> tuple[WorkRequest, ...]:
        with self._lock:
            due = [
                work
                for work in self._work.values()
                if (work.status is WorkRequestStatus.PENDING and work.next_dispatch_at <= now)
                or (
                    work.status is WorkRequestStatus.CLAIMED
                    and work.lease_expires_at is not None
                    and work.lease_expires_at <= now
                )
            ]
            return tuple(
                sorted(due, key=lambda item: (item.next_dispatch_at, item.work_request_id))[:limit]
            )

    def claim_work(
        self,
        job_id: str,
        work_request_id: str,
        *,
        now: datetime,
        claim_id: str,
        lease_expires_at: datetime,
    ) -> WorkRequest | None:
        with self._lock:
            work = self.get_work_request(job_id, work_request_id)
            claimable = (
                work.status is WorkRequestStatus.PENDING and work.next_dispatch_at <= now
            ) or (
                work.status is WorkRequestStatus.CLAIMED
                and work.lease_expires_at is not None
                and work.lease_expires_at <= now
            )
            if not claimable:
                return None
            claimed = revalidate_work_request(
                work,
                status=WorkRequestStatus.CLAIMED,
                attempt_count=work.attempt_count + 1,
                claim_id=claim_id,
                lease_expires_at=lease_expires_at,
                last_error_code=None,
                updated_at=now,
            )
            self._work[(job_id, work_request_id)] = claimed
            return claimed

    def mark_work_dispatched(
        self,
        job_id: str,
        work_request_id: str,
        *,
        claim_id: str,
        execution_arn: str,
        now: datetime,
    ) -> WorkRequest:
        with self._lock:
            work = self.get_work_request(job_id, work_request_id)
            if work.status is WorkRequestStatus.COMPLETED:
                return work
            if work.status is not WorkRequestStatus.CLAIMED or work.claim_id != claim_id:
                raise ConcurrentControlModificationError("The work claim is no longer current")
            dispatched = revalidate_work_request(
                work,
                status=WorkRequestStatus.DISPATCHED,
                execution_arn=execution_arn,
                claim_id=None,
                lease_expires_at=None,
                updated_at=now,
            )
            self._work[(job_id, work_request_id)] = dispatched
            return dispatched

    def defer_claimed_work(
        self,
        job_id: str,
        work_request_id: str,
        *,
        claim_id: str,
        retry_at: datetime,
        error_code: str,
        now: datetime,
    ) -> WorkRequest:
        with self._lock:
            work = self.get_work_request(job_id, work_request_id)
            if work.status is WorkRequestStatus.COMPLETED:
                return work
            if work.status is not WorkRequestStatus.CLAIMED or work.claim_id != claim_id:
                raise ConcurrentControlModificationError("The work claim is no longer current")
            deferred = revalidate_work_request(
                work,
                lease_expires_at=retry_at,
                last_error_code=error_code,
                updated_at=now,
            )
            self._work[(job_id, work_request_id)] = deferred
            return deferred

    def release_work(
        self,
        job_id: str,
        work_request_id: str,
        *,
        claim_id: str,
        next_dispatch_at: datetime,
        error_code: str,
        now: datetime,
    ) -> WorkRequest:
        with self._lock:
            work = self.get_work_request(job_id, work_request_id)
            if work.status is not WorkRequestStatus.CLAIMED or work.claim_id != claim_id:
                raise ConcurrentControlModificationError("The work claim is no longer current")
            released = revalidate_work_request(
                work,
                status=WorkRequestStatus.PENDING,
                claim_id=None,
                lease_expires_at=None,
                next_dispatch_at=next_dispatch_at,
                last_error_code=error_code,
                updated_at=now,
            )
            self._work[(job_id, work_request_id)] = released
            return released

    def list_reviews(self, job_id: str) -> tuple[ReviewContent, ...]:
        self.get_job(job_id)
        return tuple(
            review
            for (stored_job_id, _version), review in sorted(self._reviews.items())
            if stored_job_id == job_id
        )

    def list_review_decisions(self, job_id: str) -> tuple[ReviewDecisionRecord, ...]:
        self.get_job(job_id)
        return tuple(item for item in self._review_decisions.values() if item.job_id == job_id)

    def list_cancellation_decisions(self, job_id: str) -> tuple[CancellationDecisionRecord, ...]:
        self.get_job(job_id)
        return tuple(
            item for item in self._cancellation_decisions.values() if item.job_id == job_id
        )

    def list_failures(self, job_id: str) -> tuple[FailureRecord, ...]:
        self.get_job(job_id)
        return tuple(
            item
            for (stored_job_id, _key), item in self._failures.items()
            if stored_job_id == job_id
        )

    def list_work_requests(self, job_id: str) -> tuple[WorkRequest, ...]:
        self.get_job(job_id)
        return tuple(
            item for (stored_job_id, _key), item in self._work.items() if stored_job_id == job_id
        )

    def list_events(self, job_id: str) -> tuple[DomainEvent, ...]:
        self.get_job(job_id)
        return tuple(self._events[job_id])

    @staticmethod
    def _receipt_key(receipt: CommandReceipt) -> tuple[str, str, str, str]:
        return (
            receipt.owner_id,
            receipt.command_type,
            receipt.job_id,
            receipt.idempotency_key_digest,
        )
