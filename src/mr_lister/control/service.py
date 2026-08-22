"""Application-owned Phase 6 seller and worker commands.

Provider calls and workflow dispatch are deliberately absent. Commands validate authority and
atomically persist durable intent plus an optional transactional-outbox request.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from hashlib import sha256

from pydantic import ValidationError

from mr_lister.contracts import ListingIntelligence
from mr_lister.control.commands import (
    ApproveReviewCommand,
    CancelJobCommand,
    CommandType,
    RecordWorkerFailureCommand,
    RetryJobCommand,
    ReviseListingCommand,
    SettleCancellationCommand,
    WorkerFailureCode,
)
from mr_lister.control.dispatch import (
    deterministic_execution_name,
    work_input_fingerprint,
)
from mr_lister.control.errors import (
    ConcurrentControlModificationError,
    EconomicsStaleError,
    IdempotencyConflictError,
    InvalidControlStateError,
    RetryNotAllowedError,
    StaleReviewError,
    WorkNotActiveError,
)
from mr_lister.control.fingerprints import (
    canonical_fingerprint,
    command_request_fingerprint,
    idempotency_key_digest,
    review_etag,
)
from mr_lister.control.models import (
    CONTROL_TERMINAL_STATES,
    CancellationDecisionRecord,
    CommandReceipt,
    CommandResponse,
    ControlJobRecord,
    ControlJobState,
    DomainEvent,
    FailureRecord,
    PricingSnapshot,
    ProductSyncRecord,
    ProviderCallPermit,
    ProviderCallPermitStatus,
    RecoveryAction,
    ReviewActor,
    ReviewContent,
    ReviewDecision,
    ReviewDecisionRecord,
    WorkRequest,
    WorkRequestStatus,
    WorkType,
)
from mr_lister.control.store import CommandCommit, SellerControlStore
from mr_lister.workflow.validation import validate_listing


@dataclass(frozen=True)
class _FailurePolicy:
    recovery_action: RecoveryAction
    resume_state: ControlJobState
    work_type: WorkType


_RETRYABLE_FAILURES: Mapping[tuple[WorkType, WorkerFailureCode], _FailurePolicy] = {
    (WorkType.PREPARE, WorkerFailureCode.INTELLIGENCE_UNAVAILABLE): _FailurePolicy(
        RecoveryAction.RETRY_PREPARATION,
        ControlJobState.ANALYZING_ARTWORK,
        WorkType.PREPARE,
    ),
    (
        WorkType.SYNCHRONIZE_PRODUCT,
        WorkerFailureCode.PRODUCTION_UNAVAILABLE,
    ): _FailurePolicy(
        RecoveryAction.RETRY_PRODUCT_SYNC,
        ControlJobState.PRODUCT_DRAFT_SYNCING,
        WorkType.SYNCHRONIZE_PRODUCT,
    ),
    (
        WorkType.REFRESH_ECONOMICS,
        WorkerFailureCode.ECONOMICS_UNAVAILABLE,
    ): _FailurePolicy(
        RecoveryAction.RETRY_PRICING,
        ControlJobState.PRICING_REFRESHING,
        WorkType.REFRESH_ECONOMICS,
    ),
    (
        WorkType.REFRESH_ECONOMICS,
        WorkerFailureCode.PRODUCTION_UNAVAILABLE,
    ): _FailurePolicy(
        RecoveryAction.RETRY_PRICING,
        ControlJobState.PRICING_REFRESHING,
        WorkType.REFRESH_ECONOMICS,
    ),
}

_RETRY_AGENT_DECISION_FAILURE = _FailurePolicy(
    RecoveryAction.RETRY_AGENT_DECISION,
    ControlJobState.LISTING_DRAFTED,
    WorkType.PREPARE,
)

_PRODUCT_WRITE_WORK = frozenset({WorkType.SYNCHRONIZE_PRODUCT, WorkType.RECONCILE_PRODUCT})


class SellerControlService:
    """Readable application commands over one atomic seller-control store."""

    def __init__(
        self,
        *,
        store: SellerControlStore,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.store = store
        self._clock = clock or (lambda: datetime.now(UTC))

    def revise_listing(self, command: ReviseListingCommand) -> CommandResponse:
        command_type = CommandType.REVISE_LISTING.value
        request_fingerprint = self._seller_request_fingerprint(command_type, command)
        replay = self._resolve_seller_replay(command_type, command, request_fingerprint)
        if replay is not None:
            return replay

        current = self.store.get_job_for_owner(command.owner_id, command.job_id)
        self._require_record_version(current, command.expected_record_version)
        if current.state not in {
            ControlJobState.AWAITING_APPROVAL,
            ControlJobState.NEEDS_REVISION,
        }:
            raise InvalidControlStateError("The job cannot accept a listing revision")
        review, _sync, _pricing, current_etag = self._review_basis(current)
        self._require_review_authority(
            current,
            review,
            expected_version=command.expected_review_version,
            expected_fingerprint=command.expected_review_fingerprint,
            expected_etag=command.expected_review_etag,
            current_etag=current_etag,
        )

        listing: ListingIntelligence | None
        try:
            listing = ListingIntelligence(
                title=command.revision.title,
                description=command.revision.description,
                tags=command.revision.tags,
                audience=review.audience,
                title_rationale=review.title_rationale,
                tag_rationale=review.tag_rationale,
            )
        except ValidationError:
            listing = None
        if listing is None:
            raise InvalidControlStateError("The listing revision is outside its contract")
        validation = validate_listing(listing)
        now = self._now()
        next_version = current.review_version + 1
        review_material = {
            "contract_version": "2.0.0",
            "job_id": current.job_id,
            "review_version": next_version,
            "actor": ReviewActor.SELLER.value,
            "title": listing.title,
            "description": listing.description,
            "tags": listing.tags,
            "audience": listing.audience,
            "title_rationale": listing.title_rationale,
            "tag_rationale": listing.tag_rationale,
            "validation_passed": validation.passed,
            "validation_issue_codes": tuple(
                issue.code for issue in validation.issues if issue.severity.value == "error"
            ),
            "artwork_analysis_fingerprint": review.artwork_analysis_fingerprint,
            "product_profile_fingerprint": review.product_profile_fingerprint,
            "created_at": now.isoformat(),
        }
        next_review = ReviewContent(
            **review_material,
            fingerprint=canonical_fingerprint(review_material),
        )
        receipt_id = self._receipt_id(
            command.owner_id,
            command_type,
            command.job_id,
            idempotency_key_digest(command.idempotency_key),
        )
        work = None
        target = ControlJobState.NEEDS_REVISION
        work_id = None
        if validation.passed:
            target = ControlJobState.PRODUCT_DRAFT_SYNCING
            work_id = self._record_id("work", receipt_id, WorkType.SYNCHRONIZE_PRODUCT.value)
            work = self._work_request(
                owner_id=current.owner_id,
                job_id=current.job_id,
                receipt_id=receipt_id,
                work_request_id=work_id,
                work_type=WorkType.SYNCHRONIZE_PRODUCT,
                review_version=next_version,
                now=now,
            )

        updated = self._job_update(
            current,
            **{
                "state": target,
                "record_version": current.record_version + 1,
                "event_sequence": current.event_sequence + 1,
                "review_version": next_version,
                "review_fingerprint": next_review.fingerprint,
                "review_validated": validation.passed,
                # The prior synchronization no longer authorizes this new review, but remains
                # the immutable provider basis needed to reconstruct and reconcile the PUT.
                "pricing_snapshot_id": None,
                "pricing_snapshot_fingerprint": None,
                "approved_review_version": None,
                "approved_review_fingerprint": None,
                "approval_fingerprint": None,
                "active_work_request_id": work_id,
                "failure_id": None,
                "updated_at": now,
            },
        )
        response = self._response(updated, work_id=work_id)
        receipt = self._receipt(
            receipt_id=receipt_id,
            owner_id=current.owner_id,
            job_id=current.job_id,
            command_type=command_type,
            key_digest=idempotency_key_digest(command.idempotency_key),
            request_fingerprint=request_fingerprint,
            response=response,
            work_id=work_id,
            now=now,
        )
        decision = ReviewDecisionRecord(
            decision_id=self._record_id("decision", receipt_id),
            job_id=current.job_id,
            actor_owner_id=current.owner_id,
            decision=ReviewDecision.REVISE,
            review_version=review.review_version,
            review_fingerprint=review.fingerprint,
            command_receipt_id=receipt_id,
            decided_at=now,
        )
        commit = CommandCommit(
            current=current,
            updated=updated,
            event=self._event(updated, "LISTING_REVISION_SAVED", now),
            receipt=receipt,
            review=next_review,
            review_decision=decision,
            work_request=work,
        )
        return self._commit_or_replay(commit).response

    def approve_review(self, command: ApproveReviewCommand) -> CommandResponse:
        command_type = CommandType.APPROVE_REVIEW.value
        request_fingerprint = self._seller_request_fingerprint(command_type, command)
        replay = self._resolve_seller_replay(command_type, command, request_fingerprint)
        if replay is not None:
            return replay

        current = self.store.get_job_for_owner(command.owner_id, command.job_id)
        self._require_record_version(current, command.expected_record_version)
        if current.state is not ControlJobState.AWAITING_APPROVAL:
            raise InvalidControlStateError("The job is not awaiting approval")
        review, sync, pricing, current_etag = self._review_basis(current)
        self._require_review_authority(
            current,
            review,
            expected_version=command.expected_review_version,
            expected_fingerprint=command.expected_review_fingerprint,
            expected_etag=command.expected_review_etag,
            current_etag=current_etag,
        )
        if not review.validation_passed or not current.review_validated:
            raise InvalidControlStateError("An invalid review cannot be approved")
        if sync is None or pricing is None:
            raise EconomicsStaleError("Current product and economics evidence is required")
        if (
            current.synchronized_review_version != current.review_version
            or sync.review_version != current.review_version
            or current.product_id != sync.product_id
            or current.product_sync_fingerprint != sync.fingerprint
            or pricing.review_version != current.review_version
            or pricing.product_sync_fingerprint != sync.fingerprint
            or current.pricing_snapshot_fingerprint != pricing.fingerprint
        ):
            raise StaleReviewError("The synchronized review authority is stale")
        if sync.provider_locked or sync.provider_published:
            raise InvalidControlStateError(
                "The provider product is not an editable unpublished draft"
            )
        now = self._now()
        if now >= pricing.fresh_until:
            raise EconomicsStaleError("The economics estimate must be refreshed before approval")

        receipt_id = self._receipt_id(
            command.owner_id,
            command_type,
            command.job_id,
            idempotency_key_digest(command.idempotency_key),
        )
        updated = self._job_update(
            current,
            **{
                "state": ControlJobState.APPROVED,
                "record_version": current.record_version + 1,
                "event_sequence": current.event_sequence + 1,
                "approved_review_version": current.review_version,
                "approved_review_fingerprint": review.fingerprint,
                "approval_fingerprint": current_etag,
                "active_work_request_id": None,
                "failure_id": None,
                "updated_at": now,
            },
        )
        response = self._response(updated)
        receipt = self._receipt(
            receipt_id=receipt_id,
            owner_id=current.owner_id,
            job_id=current.job_id,
            command_type=command_type,
            key_digest=idempotency_key_digest(command.idempotency_key),
            request_fingerprint=request_fingerprint,
            response=response,
            work_id=None,
            now=now,
        )
        decision = ReviewDecisionRecord(
            decision_id=self._record_id("decision", receipt_id),
            job_id=current.job_id,
            actor_owner_id=current.owner_id,
            decision=ReviewDecision.APPROVE,
            review_version=review.review_version,
            review_fingerprint=review.fingerprint,
            approval_fingerprint=current_etag,
            command_receipt_id=receipt_id,
            decided_at=now,
        )
        return self._commit_or_replay(
            CommandCommit(
                current=current,
                updated=updated,
                event=self._event(updated, "REVIEW_APPROVED", now),
                receipt=receipt,
                review_decision=decision,
            )
        ).response

    def cancel_job(self, command: CancelJobCommand) -> CommandResponse:
        command_type = CommandType.CANCEL_JOB.value
        request_fingerprint = self._seller_request_fingerprint(command_type, command)
        replay = self._resolve_seller_replay(command_type, command, request_fingerprint)
        if replay is not None:
            return replay

        current = self.store.get_job_for_owner(command.owner_id, command.job_id)
        self._require_record_version(current, command.expected_record_version)
        if (
            current.state in CONTROL_TERMINAL_STATES
            or current.state is ControlJobState.CANCEL_REQUESTED
        ):
            raise InvalidControlStateError("The job cannot accept cancellation")
        now = self._now()
        existing_work = None
        work_update = None
        cancelled_pending_work = False
        if current.active_work_request_id is not None:
            existing_work = self.store.get_work_request(
                current.job_id, current.active_work_request_id
            )
            if (
                existing_work.status is WorkRequestStatus.PENDING
                and current.state is not ControlJobState.RECONCILIATION_REQUIRED
            ):
                cancelled_work = self._work_update(
                    existing_work,
                    **{
                        "status": WorkRequestStatus.CANCELLED,
                        "updated_at": now,
                    },
                )
                work_update = (existing_work, cancelled_work)
                cancelled_pending_work = True
            elif existing_work.status not in {
                WorkRequestStatus.PENDING,
                WorkRequestStatus.CLAIMED,
                WorkRequestStatus.DISPATCHED,
            }:
                raise InvalidControlStateError("The job references work that is no longer active")
        active = existing_work is not None and not cancelled_pending_work
        target = ControlJobState.CANCEL_REQUESTED if active else ControlJobState.CANCELLED
        permit_update = None
        if target is ControlJobState.CANCELLED:
            reconciliation_required, permit_update = self._cancellation_reconciliation_authority(
                current,
                existing_work,
                now=now,
            )
            if reconciliation_required:
                raise InvalidControlStateError(
                    "Consumed provider uncertainty requires active reconciliation"
                )
        receipt_id = self._receipt_id(
            command.owner_id,
            command_type,
            command.job_id,
            idempotency_key_digest(command.idempotency_key),
        )
        updated = self._job_update(
            current,
            **{
                "state": target,
                "record_version": current.record_version + 1,
                "event_sequence": current.event_sequence + 1,
                "active_work_request_id": (
                    current.active_work_request_id
                    if target is ControlJobState.CANCEL_REQUESTED
                    else None
                ),
                "cancellation_requested_at": now,
                "provider_outcome_unconfirmed": (
                    current.provider_outcome_unconfirmed
                    if target is ControlJobState.CANCEL_REQUESTED
                    else False
                ),
                "upload_outcome_unconfirmed": (
                    current.upload_outcome_unconfirmed
                    if target is ControlJobState.CANCEL_REQUESTED
                    else False
                ),
                "updated_at": now,
            },
        )
        response = self._response(updated)
        receipt = self._receipt(
            receipt_id=receipt_id,
            owner_id=current.owner_id,
            job_id=current.job_id,
            command_type=command_type,
            key_digest=idempotency_key_digest(command.idempotency_key),
            request_fingerprint=request_fingerprint,
            response=response,
            work_id=None,
            now=now,
        )
        cancellation = CancellationDecisionRecord(
            decision_id=self._record_id("cancellation", receipt_id),
            job_id=current.job_id,
            actor_owner_id=current.owner_id,
            expected_record_version=current.record_version,
            review_version=current.review_version or None,
            review_fingerprint=current.review_fingerprint,
            command_receipt_id=receipt_id,
            decided_at=now,
        )
        return self._commit_or_replay(
            CommandCommit(
                current=current,
                updated=updated,
                event=self._event(
                    updated,
                    (
                        "CANCELLATION_REQUESTED"
                        if target is ControlJobState.CANCEL_REQUESTED
                        else "JOB_CANCELLED"
                    ),
                    now,
                ),
                receipt=receipt,
                cancellation_decision=cancellation,
                provider_call_permit_update=permit_update,
                work_update=work_update,
            )
        ).response

    def retry_job(self, command: RetryJobCommand) -> CommandResponse:
        command_type = CommandType.RETRY_JOB.value
        request_fingerprint = self._seller_request_fingerprint(command_type, command)
        replay = self._resolve_seller_replay(command_type, command, request_fingerprint)
        if replay is not None:
            return replay

        current = self.store.get_job_for_owner(command.owner_id, command.job_id)
        self._require_record_version(current, command.expected_record_version)
        if (
            current.state is not ControlJobState.FAILED_RETRYABLE
            or current.failure_id is None
            or current.cancellation_requested_at is not None
        ):
            raise RetryNotAllowedError("The job has no advertised recovery action")
        failure = self.store.get_failure(current.job_id, current.failure_id)
        if (
            not failure.retryable
            or failure.resume_state is None
            or failure.work_type is None
            or failure.recovery_action is None
        ):
            raise RetryNotAllowedError("The persisted failure cannot be retried")
        now = self._now()
        receipt_id = self._receipt_id(
            command.owner_id,
            command_type,
            command.job_id,
            idempotency_key_digest(command.idempotency_key),
        )
        work_id = self._record_id("work", receipt_id, failure.work_type.value)
        work = self._work_request(
            owner_id=current.owner_id,
            job_id=current.job_id,
            receipt_id=receipt_id,
            work_request_id=work_id,
            work_type=failure.work_type,
            review_version=current.review_version or None,
            now=now,
        )
        updated = self._job_update(
            current,
            **{
                "state": failure.resume_state,
                "record_version": current.record_version + 1,
                "event_sequence": current.event_sequence + 1,
                "active_work_request_id": work_id,
                "failure_id": None,
                "updated_at": now,
            },
        )
        response = self._response(updated, work_id=work_id)
        receipt = self._receipt(
            receipt_id=receipt_id,
            owner_id=current.owner_id,
            job_id=current.job_id,
            command_type=command_type,
            key_digest=idempotency_key_digest(command.idempotency_key),
            request_fingerprint=request_fingerprint,
            response=response,
            work_id=work_id,
            now=now,
        )
        return self._commit_or_replay(
            CommandCommit(
                current=current,
                updated=updated,
                event=self._event(updated, "RECOVERY_REQUESTED", now),
                receipt=receipt,
                work_request=work,
            )
        ).response

    def record_worker_failure(self, command: RecordWorkerFailureCommand) -> CommandResponse:
        current = self.store.get_job(command.job_id)
        command_type = "record_worker_failure"
        key_digest = canonical_fingerprint(
            {"job_id": command.job_id, "work_request_id": command.work_request_id}
        )
        request_fingerprint = command_request_fingerprint(
            command_type=command_type,
            payload=command.model_dump(mode="json"),
        )
        replay = self._resolve_replay(
            owner_id=current.owner_id,
            command_type=command_type,
            job_id=current.job_id,
            key_digest=key_digest,
            request_fingerprint=request_fingerprint,
        )
        if replay is not None:
            return replay
        self._require_record_version(current, command.expected_record_version)
        expected_work = self._require_active_work(current, command.work_request_id)
        now = self._now()
        receipt_id = self._receipt_id(current.owner_id, command_type, current.job_id, key_digest)
        failure_code = command.code
        if command.code is WorkerFailureCode.PRODUCT_CREATE_OUTCOME_UNKNOWN:
            # Ambiguous mutations require the dedicated Phase 6.2 attempt + consumed-permit
            # evidence boundary. This generic failure command is only safe before a mutation.
            failure_code = WorkerFailureCode.UNCLASSIFIED_FAILURE
        settled_work = self._work_update(
            expected_work,
            **{
                "status": WorkRequestStatus.COMPLETED,
                "claim_id": None,
                "lease_expires_at": None,
                "last_error_code": failure_code.value,
                "updated_at": now,
            },
        )

        cancellation_dominates = current.cancellation_requested_at is not None
        unknown_outcome = self._consumed_provider_uncertainty(current, expected_work)
        reconciliation_transient = (
            expected_work.work_type is WorkType.RECONCILE_PRODUCT
            and failure_code is WorkerFailureCode.PRODUCTION_UNAVAILABLE
        )
        policy = _RETRYABLE_FAILURES.get((expected_work.work_type, failure_code))
        if (
            policy is not None
            and expected_work.work_type is WorkType.PREPARE
            and current.state is ControlJobState.LISTING_DRAFTED
        ):
            # The expensive intelligence checkpoint is already immutable. A controller or
            # AgentCore failure here resumes the decision step and must never repay inference.
            policy = _RETRY_AGENT_DECISION_FAILURE
        failure = None
        new_work = None
        work_id = None
        if reconciliation_transient:
            target = ControlJobState.RECONCILIATION_REQUIRED
        elif cancellation_dominates:
            target = (
                ControlJobState.RECONCILIATION_REQUIRED
                if unknown_outcome
                else ControlJobState.CANCELLED
            )
        elif unknown_outcome:
            target = ControlJobState.RECONCILIATION_REQUIRED
        elif policy is not None:
            target = ControlJobState.FAILED_RETRYABLE
        else:
            target = ControlJobState.FAILED_TERMINAL

        if target is ControlJobState.RECONCILIATION_REQUIRED:
            work_id = self._record_id("work", receipt_id, WorkType.RECONCILE_PRODUCT.value)
            new_work = self._work_request(
                owner_id=current.owner_id,
                job_id=current.job_id,
                receipt_id=receipt_id,
                work_request_id=work_id,
                work_type=WorkType.RECONCILE_PRODUCT,
                review_version=current.review_version or None,
                now=now,
            )
        elif target in {ControlJobState.FAILED_RETRYABLE, ControlJobState.FAILED_TERMINAL}:
            failure_id = self._record_id(
                "failure", current.job_id, command.work_request_id, failure_code.value
            )
            failure = FailureRecord(
                failure_id=failure_id,
                job_id=current.job_id,
                work_request_id=command.work_request_id,
                stage=current.state,
                code=failure_code.value,
                retryable=policy is not None,
                recovery_action=None if policy is None else policy.recovery_action,
                resume_state=None if policy is None else policy.resume_state,
                work_type=None if policy is None else policy.work_type,
                occurred_at=now,
            )

        permit_update = None
        if target is ControlJobState.CANCELLED:
            reconciliation_required, permit_update = self._cancellation_reconciliation_authority(
                current,
                expected_work,
                now=now,
            )
            if reconciliation_required:
                raise InvalidControlStateError(
                    "Consumed provider uncertainty requires reconciliation"
                )

        updated = self._job_update(
            current,
            **{
                "state": target,
                "record_version": current.record_version + 1,
                "event_sequence": current.event_sequence + 1,
                "active_work_request_id": work_id,
                "failure_id": None if failure is None else failure.failure_id,
                "provider_outcome_unconfirmed": (
                    False
                    if target is ControlJobState.CANCELLED
                    else current.provider_outcome_unconfirmed
                    or (cancellation_dominates and unknown_outcome)
                ),
                "upload_outcome_unconfirmed": (
                    False
                    if target is ControlJobState.CANCELLED
                    else current.upload_outcome_unconfirmed
                ),
                "updated_at": now,
            },
        )
        response = self._response(updated, work_id=work_id)
        receipt = self._receipt(
            receipt_id=receipt_id,
            owner_id=current.owner_id,
            job_id=current.job_id,
            command_type=command_type,
            key_digest=key_digest,
            request_fingerprint=request_fingerprint,
            response=response,
            work_id=work_id,
            now=now,
        )
        return self._commit_worker_or_replay(
            CommandCommit(
                current=current,
                updated=updated,
                event=self._event(updated, "WORK_FAILED", now),
                receipt=receipt,
                failure=failure,
                provider_call_permit_update=permit_update,
                work_request=new_work,
                work_update=(expected_work, settled_work),
            )
        ).response

    def _consumed_provider_uncertainty(
        self,
        current: ControlJobRecord,
        work: WorkRequest,
    ) -> bool:
        """Derive mutation ambiguity from the one-shot permit, never from a caller flag."""

        if work.work_type is not WorkType.SYNCHRONIZE_PRODUCT:
            return False
        if current.upload_outcome_unconfirmed:
            attempt_id = current.provider_upload_attempt_id
        elif current.provider_outcome_unconfirmed:
            attempt_id = current.provider_write_attempt_id
        else:
            return False
        if attempt_id is None:
            raise InvalidControlStateError(
                "Provider uncertainty has no immutable attempt authority"
            )
        permit = self.store.get_provider_call_permit(current.job_id, attempt_id)
        if permit.status is not ProviderCallPermitStatus.CONSUMED:
            return False
        if permit.consumed_work_request_id != work.work_request_id:
            raise InvalidControlStateError(
                "Consumed provider uncertainty does not bind the failing work"
            )
        return True

    def settle_cancellation(self, command: SettleCancellationCommand) -> CommandResponse:
        current = self.store.get_job(command.job_id)
        command_type = "settle_cancellation"
        key_digest = canonical_fingerprint(
            {"job_id": command.job_id, "work_request_id": command.work_request_id}
        )
        request_fingerprint = command_request_fingerprint(
            command_type=command_type,
            payload=command.model_dump(mode="json"),
        )
        replay = self._resolve_replay(
            owner_id=current.owner_id,
            command_type=command_type,
            job_id=current.job_id,
            key_digest=key_digest,
            request_fingerprint=request_fingerprint,
        )
        if replay is not None:
            return replay
        self._require_record_version(current, command.expected_record_version)
        if current.cancellation_requested_at is None:
            raise InvalidControlStateError("The job has no cancellation intent")
        expected_work = self._require_active_work(current, command.work_request_id)
        now = self._now()
        receipt_id = self._receipt_id(current.owner_id, command_type, current.job_id, key_digest)
        settled_work = self._work_update(
            expected_work,
            **{
                "status": WorkRequestStatus.COMPLETED,
                "claim_id": None,
                "lease_expires_at": None,
                "updated_at": now,
            },
        )
        new_work = None
        work_id = None
        target = ControlJobState.CANCELLED
        reconciliation_required, permit_update = self._cancellation_reconciliation_authority(
            current,
            expected_work,
            now=now,
        )
        if reconciliation_required:
            target = ControlJobState.RECONCILIATION_REQUIRED
            work_id = self._record_id("work", receipt_id, WorkType.RECONCILE_PRODUCT.value)
            new_work = self._work_request(
                owner_id=current.owner_id,
                job_id=current.job_id,
                receipt_id=receipt_id,
                work_request_id=work_id,
                work_type=WorkType.RECONCILE_PRODUCT,
                review_version=current.review_version or None,
                now=now,
            )
        updated = self._job_update(
            current,
            **{
                "state": target,
                "record_version": current.record_version + 1,
                "event_sequence": current.event_sequence + 1,
                "active_work_request_id": work_id,
                "provider_outcome_unconfirmed": (
                    current.provider_outcome_unconfirmed and reconciliation_required
                ),
                "upload_outcome_unconfirmed": (
                    current.upload_outcome_unconfirmed and reconciliation_required
                ),
                "updated_at": now,
            },
        )
        response = self._response(updated, work_id=work_id)
        receipt = self._receipt(
            receipt_id=receipt_id,
            owner_id=current.owner_id,
            job_id=current.job_id,
            command_type=command_type,
            key_digest=key_digest,
            request_fingerprint=request_fingerprint,
            response=response,
            work_id=work_id,
            now=now,
        )
        return self._commit_worker_or_replay(
            CommandCommit(
                current=current,
                updated=updated,
                event=self._event(updated, "CANCELLATION_SETTLED", now),
                receipt=receipt,
                provider_call_permit_update=permit_update,
                work_request=new_work,
                work_update=(expected_work, settled_work),
            )
        ).response

    def _cancellation_reconciliation_authority(
        self,
        current: ControlJobRecord,
        work: WorkRequest | None,
        *,
        now: datetime,
    ) -> tuple[bool, tuple[ProviderCallPermit, ProviderCallPermit] | None]:
        if work is not None and work.work_type not in _PRODUCT_WRITE_WORK:
            return False, None
        if current.upload_outcome_unconfirmed:
            attempt_id = current.provider_upload_attempt_id
        elif current.provider_outcome_unconfirmed:
            attempt_id = current.provider_write_attempt_id
        else:
            return False, None
        if attempt_id is None:
            raise InvalidControlStateError(
                "Provider uncertainty has no immutable attempt authority"
            )
        permit = self.store.get_provider_call_permit(current.job_id, attempt_id)
        if permit.job_id != current.job_id or permit.attempt_id != attempt_id:
            raise InvalidControlStateError(
                "Provider uncertainty does not match its one-shot permit"
            )
        if permit.status is ProviderCallPermitStatus.CONSUMED:
            return True, None
        if permit.status is ProviderCallPermitStatus.RETIRED:
            raise InvalidControlStateError(
                "Retired provider authority cannot remain marked uncertain"
            )
        retired = ProviderCallPermit.model_validate(
            {
                **permit.model_dump(mode="python"),
                "status": ProviderCallPermitStatus.RETIRED,
                "retired_at": now,
            }
        )
        return False, (permit, retired)

    def _review_basis(
        self, job: ControlJobRecord
    ) -> tuple[ReviewContent, ProductSyncRecord | None, PricingSnapshot | None, str]:
        if job.review_version == 0 or job.review_fingerprint is None:
            raise InvalidControlStateError("The job has no review")
        review = self.store.get_review(job.job_id, job.review_version)
        if review.fingerprint != job.review_fingerprint:
            raise StaleReviewError("The current review fingerprint does not match the job")
        sync = None
        if job.product_sync_id is not None:
            sync = self.store.get_product_sync(job.job_id, job.product_sync_id)
            if sync.fingerprint != job.product_sync_fingerprint:
                raise StaleReviewError("The product synchronization fingerprint does not match")
        pricing = None
        if job.pricing_snapshot_id is not None:
            pricing = self.store.get_pricing(job.job_id, job.pricing_snapshot_id)
            if pricing.fingerprint != job.pricing_snapshot_fingerprint:
                raise StaleReviewError("The pricing fingerprint does not match")
        etag = review_etag(
            job_id=job.job_id,
            review_version=review.review_version,
            review_fingerprint=review.fingerprint,
            product_id=job.product_id,
            product_sync_fingerprint=None if sync is None else sync.fingerprint,
            pricing_snapshot_id=None if pricing is None else pricing.snapshot_id,
            pricing_snapshot_fingerprint=None if pricing is None else pricing.fingerprint,
        )
        return review, sync, pricing, etag

    @staticmethod
    def _require_record_version(job: ControlJobRecord, expected: int) -> None:
        if job.record_version != expected:
            raise ConcurrentControlModificationError("The job changed before the command")

    @staticmethod
    def _require_review_authority(
        job: ControlJobRecord,
        review: ReviewContent,
        *,
        expected_version: int,
        expected_fingerprint: str,
        expected_etag: str,
        current_etag: str,
    ) -> None:
        if job.review_version != expected_version or review.review_version != expected_version:
            raise StaleReviewError("The command does not match the current review version")
        if (
            job.review_fingerprint != expected_fingerprint
            or review.fingerprint != expected_fingerprint
        ):
            raise StaleReviewError("The command does not match the current review fingerprint")
        if current_etag != expected_etag:
            raise StaleReviewError("The command does not match the current review ETag")

    def _resolve_seller_replay(
        self,
        command_type: str,
        command: ReviseListingCommand | ApproveReviewCommand | CancelJobCommand | RetryJobCommand,
        request_fingerprint: str,
    ) -> CommandResponse | None:
        return self._resolve_replay(
            owner_id=command.owner_id,
            command_type=command_type,
            job_id=command.job_id,
            key_digest=idempotency_key_digest(command.idempotency_key),
            request_fingerprint=request_fingerprint,
        )

    def _resolve_replay(
        self,
        *,
        owner_id: str,
        command_type: str,
        job_id: str,
        key_digest: str,
        request_fingerprint: str,
    ) -> CommandResponse | None:
        receipt = self.store.resolve_receipt(owner_id, command_type, job_id, key_digest)
        if receipt is None:
            return None
        if receipt.request_fingerprint != request_fingerprint:
            raise IdempotencyConflictError("The idempotency key was used for another request")
        if receipt.work_request_id is not None:
            self.store.nudge_pending_work(job_id, receipt.work_request_id, now=self._now())
        return receipt.response

    def _commit_or_replay(self, commit: CommandCommit) -> CommandReceipt:
        try:
            return self.store.commit_command(commit)
        except IdempotencyConflictError:
            receipt = self.store.resolve_receipt(
                commit.receipt.owner_id,
                commit.receipt.command_type,
                commit.receipt.job_id,
                commit.receipt.idempotency_key_digest,
            )
            if receipt is None or receipt.request_fingerprint != commit.receipt.request_fingerprint:
                raise
            if receipt.work_request_id is not None:
                self.store.nudge_pending_work(
                    receipt.job_id,
                    receipt.work_request_id,
                    now=self._now(),
                )
            return receipt

    def _commit_worker_or_replay(self, commit: CommandCommit) -> CommandReceipt:
        """Rebase once when dispatch acknowledgment races an exact worker settlement."""

        try:
            return self._commit_or_replay(commit)
        except ConcurrentControlModificationError:
            if commit.work_update is None:
                raise
            current = self.store.get_job(commit.current.job_id)
            if current != commit.current:
                raise
            expected, changed = commit.work_update
            latest = self.store.get_work_request(expected.job_id, expected.work_request_id)
            if current.active_work_request_id != latest.work_request_id or latest.status not in {
                WorkRequestStatus.CLAIMED,
                WorkRequestStatus.DISPATCHED,
            }:
                raise
            rebased = self._work_update(
                latest,
                **{
                    "status": WorkRequestStatus.COMPLETED,
                    "claim_id": None,
                    "lease_expires_at": None,
                    "last_error_code": changed.last_error_code,
                    "updated_at": max(changed.updated_at, latest.updated_at),
                },
            )
            return self._commit_or_replay(replace(commit, work_update=(latest, rebased)))

    @staticmethod
    def _seller_request_fingerprint(command_type: str, command: object) -> str:
        assert hasattr(command, "model_dump")
        payload = command.model_dump(mode="json", exclude={"idempotency_key"})
        return command_request_fingerprint(command_type=command_type, payload=payload)

    @staticmethod
    def _receipt_id(owner_id: str, command_type: str, job_id: str, key_digest: str) -> str:
        return SellerControlService._record_id(
            "receipt", owner_id, command_type, job_id, key_digest
        )

    @staticmethod
    def _record_id(prefix: str, *parts: str) -> str:
        digest = sha256("\x00".join(parts).encode()).hexdigest()[:40]
        return f"{prefix}_{digest}"

    @staticmethod
    def _response(job: ControlJobRecord, *, work_id: str | None = None) -> CommandResponse:
        return CommandResponse(
            job_id=job.job_id,
            state=job.state,
            record_version=job.record_version,
            review_version=job.review_version,
            work_request_id=work_id,
        )

    @staticmethod
    def _receipt(
        *,
        receipt_id: str,
        owner_id: str,
        job_id: str,
        command_type: str,
        key_digest: str,
        request_fingerprint: str,
        response: CommandResponse,
        work_id: str | None,
        now: datetime,
    ) -> CommandReceipt:
        return CommandReceipt(
            receipt_id=receipt_id,
            owner_id=owner_id,
            job_id=job_id,
            command_type=command_type,
            idempotency_key_digest=key_digest,
            request_fingerprint=request_fingerprint,
            response=response,
            work_request_id=work_id,
            created_at=now,
        )

    @staticmethod
    def _work_request(
        *,
        owner_id: str,
        job_id: str,
        receipt_id: str,
        work_request_id: str,
        work_type: WorkType,
        review_version: int | None,
        now: datetime,
    ) -> WorkRequest:
        input_fingerprint = work_input_fingerprint(
            work_type=work_type,
            job_id=job_id,
            work_request_id=work_request_id,
        )
        return WorkRequest(
            work_request_id=work_request_id,
            owner_id=owner_id,
            job_id=job_id,
            receipt_id=receipt_id,
            work_type=work_type,
            review_version=review_version,
            input_fingerprint=input_fingerprint,
            execution_name=deterministic_execution_name(work_request_id),
            next_dispatch_at=now,
            created_at=now,
            updated_at=now,
        )

    @staticmethod
    def _event(job: ControlJobRecord, name: str, now: datetime) -> DomainEvent:
        return DomainEvent(
            job_id=job.job_id,
            sequence=job.event_sequence,
            name=name,
            occurred_at=now,
            details={"state": job.state.value, "record_version": job.record_version},
        )

    def _require_active_work(self, job: ControlJobRecord, work_request_id: str) -> WorkRequest:
        if job.active_work_request_id != work_request_id:
            raise WorkNotActiveError("The worker no longer owns the current job operation")
        work = self.store.get_work_request(job.job_id, work_request_id)
        if work.status not in {
            WorkRequestStatus.CLAIMED,
            WorkRequestStatus.DISPATCHED,
        }:
            raise WorkNotActiveError("The work request has not been dispatched or is already done")
        return work

    @staticmethod
    def _job_update(current: ControlJobRecord, **updates: object) -> ControlJobRecord:
        payload = current.model_dump(mode="python")
        payload.update(updates)
        return ControlJobRecord.model_validate(payload)

    @staticmethod
    def _work_update(current: WorkRequest, **updates: object) -> WorkRequest:
        payload = current.model_dump(mode="python")
        payload.update(updates)
        return WorkRequest.model_validate(payload)

    def _now(self) -> datetime:
        now = self._clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise InvalidControlStateError("The control clock must return a timezone-aware value")
        return now
