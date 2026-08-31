"""Application-owned success boundary for Phase 6 machine work."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from hashlib import sha256

from mr_lister.contracts import ListingIntelligence
from mr_lister.control.dispatch import deterministic_execution_name, work_input_fingerprint
from mr_lister.control.economics import ProductCostEvidence, ProductVariantCostEvidence
from mr_lister.control.errors import (
    ConcurrentControlModificationError,
    IdempotencyConflictError,
    InvalidControlStateError,
    WorkNotActiveError,
)
from mr_lister.control.fingerprints import (
    agent_preparation_evidence_fingerprint,
    canonical_fingerprint,
    command_request_fingerprint,
    product_sync_record_fingerprint,
)
from mr_lister.control.models import (
    AgentPreparationEvidence,
    ArtworkAnalysisRecord,
    CommandReceipt,
    CommandResponse,
    ControlJobRecord,
    ControlJobState,
    DomainEvent,
    FailureRecord,
    PricingEvidenceRecord,
    PricingSnapshot,
    ProductSyncRecord,
    ProviderCallPermit,
    ProviderCallPermitStatus,
    ProviderUploadAttempt,
    ProviderWriteAttempt,
    ProviderWriteOperation,
    ReconciliationObservationRecord,
    ReconciliationOutcome,
    ReviewActor,
    ReviewContent,
    UploadedArtworkRecord,
    UploadReconciliationObservationRecord,
    WorkRequest,
    WorkRequestStatus,
    WorkType,
)
from mr_lister.control.store import CommandCommit, SellerControlStore
from mr_lister.control.worker_commands import (
    BeginPreparationCommand,
    BeginProviderUploadCommand,
    BeginProviderWriteCommand,
    CompletePreparationWithAgentDecisionCommand,
    ProductSyncObservation,
    RecordPreparedReviewCommand,
    RecordPricingSuccessCommand,
    RecordProductSyncSuccessCommand,
    RecordProductWriteOutcomeUnknownCommand,
    RecordProviderUploadOutcomeUnknownCommand,
    RecordProviderUploadSuccessCommand,
    RecordReconciliationObservationCommand,
    RecordUploadReconciliationObservationCommand,
    UploadedArtworkObservation,
)
from mr_lister.workflow.validation import validate_listing

_CREATE_UNKNOWN_CODE = "PRODUCT_CREATE_OUTCOME_UNKNOWN"
_UPDATE_UNKNOWN_CODE = "PRODUCT_UPDATE_OUTCOME_UNKNOWN"
_RECONCILIATION_CONFLICT_CODE = "PRODUCT_RECONCILIATION_CONFLICT"
_UPLOAD_UNKNOWN_CODE = "ARTWORK_UPLOAD_OUTCOME_UNKNOWN"
_UPLOAD_CONFLICT_CODE = "ARTWORK_UPLOAD_RECONCILIATION_CONFLICT"
_UPDATE_RETRY_EXHAUSTED_CODE = "PRODUCT_UPDATE_RETRY_EXHAUSTED"
_MAX_EXACT_UPDATE_RETRIES = 1


class WorkerControlService:
    """Trusted observations in; deterministic transitions and atomic evidence out."""

    def __init__(
        self,
        *,
        store: SellerControlStore,
        clock: Callable[[], datetime] | None = None,
        reconciliation_window: timedelta = timedelta(minutes=15),
        reconciliation_delay: timedelta = timedelta(seconds=30),
    ) -> None:
        if reconciliation_window <= timedelta(0) or reconciliation_delay <= timedelta(0):
            raise ValueError("Reconciliation timing must be positive")
        self.store = store
        self._clock = clock or (lambda: datetime.now(UTC))
        self._reconciliation_window = reconciliation_window
        self._reconciliation_delay = reconciliation_delay

    def begin_preparation(self, command: BeginPreparationCommand) -> CommandResponse:
        current, work, replay = self._begin_worker_command("begin_preparation", command)
        if replay is not None:
            return replay
        if current.state is not ControlJobState.INTAKE_VALIDATED:
            raise InvalidControlStateError("Preparation can begin only from validated intake")
        now = self._now()
        updated = self._job_update(
            current,
            state=ControlJobState.ANALYZING_ARTWORK,
            record_version=current.record_version + 1,
            event_sequence=current.event_sequence + 1,
            updated_at=now,
        )
        return self._commit_simple(
            command_type="begin_preparation",
            command=command,
            current=current,
            updated=updated,
            event_name="ARTWORK_ANALYSIS_STARTED",
            now=now,
        ).response

    def record_prepared_review(self, command: RecordPreparedReviewCommand) -> CommandResponse:
        current, _work, replay = self._begin_worker_command("record_prepared_review", command)
        if replay is not None:
            return replay
        if current.state is not ControlJobState.ANALYZING_ARTWORK:
            raise InvalidControlStateError("Prepared review requires artwork analysis state")
        source = self.store.get_source_artifact(current.job_id)
        if (
            source.fingerprint != current.source_artifact_fingerprint
            or source.fingerprint != command.source_artifact_fingerprint
            or source.product_profile_fingerprint != command.product_profile_fingerprint
        ):
            raise InvalidControlStateError("Prepared review does not bind the pinned intake")
        now = self._now()
        validation = validate_listing(command.listing)
        issue_codes = tuple(
            issue.code for issue in validation.issues if issue.severity.value == "error"
        )
        persisted_analysis = None
        persisted_review = None
        review_created_at = now
        if current.review_version > 0:
            if current.artwork_analysis_id is None:
                raise InvalidControlStateError("Preparation checkpoint is incomplete")
            persisted_analysis = self.store.get_artwork_analysis(
                current.job_id, current.artwork_analysis_id
            )
            persisted_review = self.store.get_review(current.job_id, current.review_version)
            review_created_at = persisted_review.created_at
        analysis_material = {
            "job_id": current.job_id,
            "source_artifact_fingerprint": source.fingerprint,
            "analysis": command.artwork_analysis.model_dump(mode="json"),
        }
        analysis_fingerprint = canonical_fingerprint(analysis_material)
        analysis_id = self._record_id("analysis", current.job_id, analysis_fingerprint)
        next_version = current.review_version or 1
        review_material = {
            "contract_version": "2.0.0",
            "job_id": current.job_id,
            "review_version": next_version,
            "actor": ReviewActor.MODEL.value,
            "title": command.listing.title,
            "description": command.listing.description,
            "tags": command.listing.tags,
            "audience": command.listing.audience,
            "title_rationale": command.listing.title_rationale,
            "tag_rationale": command.listing.tag_rationale,
            "validation_passed": validation.passed,
            "validation_issue_codes": issue_codes,
            "artwork_analysis_fingerprint": analysis_fingerprint,
            "product_profile_fingerprint": source.product_profile_fingerprint,
            "created_at": review_created_at.isoformat(),
        }
        review_fingerprint = canonical_fingerprint(review_material)
        analysis = ArtworkAnalysisRecord(
            analysis_id=analysis_id,
            job_id=current.job_id,
            work_request_id=command.work_request_id,
            source_artifact_fingerprint=source.fingerprint,
            fingerprint=analysis_fingerprint,
            analysis=command.artwork_analysis,
            created_at=review_created_at,
        )
        review = ReviewContent(
            **review_material,
            fingerprint=review_fingerprint,
        )

        commit_analysis: ArtworkAnalysisRecord | None = analysis
        commit_review: ReviewContent | None = review
        if persisted_analysis is not None and persisted_review is not None:
            if (
                persisted_analysis.fingerprint != analysis.fingerprint
                or persisted_review.fingerprint != review.fingerprint
            ):
                raise InvalidControlStateError("Preparation checkpoint conflicts with prior proof")
            commit_analysis = None
            commit_review = None

        updated = self._job_update(
            current,
            state=ControlJobState.LISTING_DRAFTED,
            record_version=current.record_version + 1,
            event_sequence=current.event_sequence + 1,
            review_version=review.review_version,
            review_fingerprint=review.fingerprint,
            review_validated=review.validation_passed,
            artwork_analysis_id=analysis.analysis_id,
            artwork_analysis_fingerprint=analysis.fingerprint,
            updated_at=now,
        )
        receipt = self._receipt_for(
            command_type="record_prepared_review",
            command=command,
            updated=updated,
            work_id=None,
            now=now,
        )
        return self._commit_worker_or_replay(
            CommandCommit(
                current=current,
                updated=updated,
                event=self._event(updated, "PREPARED_REVIEW_RECORDED", now),
                receipt=receipt,
                review=commit_review,
                artwork_analysis=commit_analysis,
            )
        ).response

    def complete_preparation_with_agent_decision(
        self, command: CompletePreparationWithAgentDecisionCommand
    ) -> CommandResponse:
        current, work, replay = self._begin_worker_command(
            "complete_preparation_with_agent_decision", command
        )
        if replay is not None:
            return replay
        if current.state is not ControlJobState.LISTING_DRAFTED:
            raise InvalidControlStateError("Agent completion requires a prepared review")
        review = self.store.get_review(current.job_id, current.review_version)
        listing = ListingIntelligence(
            title=review.title,
            description=review.description,
            tags=review.tags,
            audience=review.audience,
            title_rationale=review.title_rationale,
            tag_rationale=review.tag_rationale,
        )
        validation = validate_listing(listing)
        if validation.passed != review.validation_passed:
            raise InvalidControlStateError("Prepared review validation changed before routing")
        expected_action = "human_review" if validation.passed else "revise"
        if command.decision.next_action != expected_action:
            raise InvalidControlStateError("Agent decision does not match application validation")
        now = self._now()
        evidence_id = self._record_id("agent", current.job_id, command.work_request_id)
        decision_fingerprint = canonical_fingerprint(command.decision.model_dump(mode="json"))
        evidence_fingerprint = agent_preparation_evidence_fingerprint(
            evidence_id=evidence_id,
            job_id=current.job_id,
            work_request_id=work.work_request_id,
            review_version=current.review_version,
            correlation_id=command.correlation_id,
            framework="strands-agents",
            agent_id="mr-lister-preparation",
            controller_model_id=command.controller_model_id,
            tool_calls=command.tool_calls,
            cycles=command.cycles,
            input_tokens=command.input_tokens,
            output_tokens=command.output_tokens,
            total_tokens=command.total_tokens,
            decision_fingerprint=decision_fingerprint,
            requires_human_approval=True,
            publication_authorized=False,
            created_at=now,
        )
        evidence = AgentPreparationEvidence(
            evidence_id=evidence_id,
            job_id=current.job_id,
            work_request_id=work.work_request_id,
            review_version=current.review_version,
            correlation_id=command.correlation_id,
            framework="strands-agents",
            agent_id="mr-lister-preparation",
            controller_model_id=command.controller_model_id,
            tool_calls=command.tool_calls,
            cycles=command.cycles,
            input_tokens=command.input_tokens,
            output_tokens=command.output_tokens,
            total_tokens=command.total_tokens,
            decision_fingerprint=decision_fingerprint,
            fingerprint=evidence_fingerprint,
            created_at=now,
        )
        receipt = self._receipt_for(
            command_type="complete_preparation_with_agent_decision",
            command=command,
            updated=current,
            work_id=None,
            now=now,
            response_override=False,
        )
        next_work = None
        target = ControlJobState.NEEDS_REVISION
        work_id = None
        if validation.passed:
            target = ControlJobState.PRODUCT_DRAFT_SYNCING
            work_id = self._record_id(
                "work", receipt.receipt_id, WorkType.SYNCHRONIZE_PRODUCT.value
            )
            next_work = self._work_request(
                current,
                receipt_id=receipt.receipt_id,
                work_id=work_id,
                work_type=WorkType.SYNCHRONIZE_PRODUCT,
                review_version=current.review_version,
                due_at=now,
            )
        updated = self._job_update(
            current,
            state=target,
            record_version=current.record_version + 1,
            event_sequence=current.event_sequence + 1,
            agent_evidence_id=evidence.evidence_id,
            agent_evidence_fingerprint=evidence.fingerprint,
            active_work_request_id=work_id,
            updated_at=now,
        )
        receipt = self._receipt_for(
            command_type="complete_preparation_with_agent_decision",
            command=command,
            updated=updated,
            work_id=work_id,
            now=now,
        )
        if next_work is not None:
            next_work = WorkRequest.model_validate(
                {**next_work.model_dump(mode="python"), "receipt_id": receipt.receipt_id}
            )
        settled = self._settled_work(work, now=now)
        return self._commit_worker_or_replay(
            CommandCommit(
                current=current,
                updated=updated,
                event=self._event(updated, "STRANDS_PREPARATION_COMPLETED", now),
                receipt=receipt,
                agent_evidence=evidence,
                work_request=next_work,
                work_update=(work, settled),
            )
        ).response

    def begin_provider_upload(self, command: BeginProviderUploadCommand) -> CommandResponse:
        """Persist the job's only artwork-upload claim before any provider POST."""

        current, work, replay = self._begin_worker_command("begin_provider_upload", command)
        if replay is not None:
            return replay
        if (
            current.state is not ControlJobState.PRODUCT_DRAFT_SYNCING
            or work.work_type is not WorkType.SYNCHRONIZE_PRODUCT
        ):
            raise InvalidControlStateError("Artwork upload requires product synchronization work")
        if (
            current.provider_upload_attempt_id is not None
            or current.uploaded_artwork_id is not None
        ):
            raise InvalidControlStateError("The job's artwork upload was already claimed")
        if current.provider_outcome_unconfirmed:
            raise InvalidControlStateError("Product-write uncertainty forbids an artwork upload")
        source = self.store.get_source_artifact(current.job_id)
        if (
            source.fingerprint != current.source_artifact_fingerprint
            or command.source_artifact_fingerprint != source.fingerprint
            or command.file_name != self.upload_file_name(current.job_id, source.content_sha256)
        ):
            raise InvalidControlStateError("Artwork upload does not bind the pinned source")
        now = self._now()
        attempt = ProviderUploadAttempt(
            attempt_id=self._record_id("upload_attempt", current.job_id, source.fingerprint),
            job_id=current.job_id,
            work_request_id=work.work_request_id,
            source_artifact_fingerprint=source.fingerprint,
            file_name=command.file_name,
            reconciliation_deadline=now + self._reconciliation_window,
            started_at=now,
        )
        permit = ProviderCallPermit(
            attempt_id=attempt.attempt_id,
            job_id=current.job_id,
            work_request_id=work.work_request_id,
            created_at=now,
        )
        updated = self._job_update(
            current,
            state=ControlJobState.PRODUCT_DRAFT_SYNCING,
            record_version=current.record_version + 1,
            event_sequence=current.event_sequence + 1,
            provider_upload_attempt_id=attempt.attempt_id,
            upload_outcome_unconfirmed=True,
            updated_at=now,
        )
        receipt = self._receipt_for(
            command_type="begin_provider_upload",
            command=command,
            updated=updated,
            work_id=None,
            now=now,
        )
        return self._commit_worker_or_replay(
            CommandCommit(
                current=current,
                updated=updated,
                event=self._event(updated, "PROVIDER_UPLOAD_CLAIMED", now),
                receipt=receipt,
                provider_upload_attempt=attempt,
                provider_call_permit=permit,
            )
        ).response

    def authorize_provider_upload(
        self, *, job_id: str, attempt_id: str
    ) -> ProviderUploadAttempt | None:
        """Consume the exact upload permit immediately before the only upload POST."""

        current = self.store.get_job(job_id)
        if (
            current.state is not ControlJobState.PRODUCT_DRAFT_SYNCING
            or current.provider_upload_attempt_id != attempt_id
            or current.uploaded_artwork_id is not None
            or not current.upload_outcome_unconfirmed
            or current.provider_outcome_unconfirmed
            or current.active_work_request_id is None
        ):
            return None
        attempt = self.store.get_provider_upload_attempt(job_id, attempt_id)
        if attempt.source_artifact_fingerprint != current.source_artifact_fingerprint:
            return None
        work = self.store.get_work_request(job_id, current.active_work_request_id)
        if (
            work.work_type is not WorkType.SYNCHRONIZE_PRODUCT
            or work.review_version != current.review_version
            or work.status not in {WorkRequestStatus.CLAIMED, WorkRequestStatus.DISPATCHED}
        ):
            return None
        if attempt.work_request_id != work.work_request_id:
            origin = self.store.get_work_request(job_id, attempt.work_request_id)
            permit = self.store.get_provider_call_permit(job_id, attempt_id)
            if (
                origin.work_type is not WorkType.SYNCHRONIZE_PRODUCT
                or origin.status is not WorkRequestStatus.COMPLETED
                or permit.status is not ProviderCallPermitStatus.AVAILABLE
                or permit.consumed_at is not None
                or permit.consumed_work_request_id is not None
            ):
                return None
        consumed = self.store.consume_provider_call_permit(
            current,
            work,
            attempt_id,
            now=self._now(),
        )
        return attempt if consumed is not None else None

    def record_provider_upload_success(
        self, command: RecordProviderUploadSuccessCommand
    ) -> CommandResponse:
        current, work, replay = self._begin_worker_command(
            "record_provider_upload_success", command
        )
        if replay is not None:
            return replay
        if current.state is not ControlJobState.PRODUCT_DRAFT_SYNCING:
            raise InvalidControlStateError("Direct upload success requires synchronization state")
        attempt = self._require_upload_attempt(current, command.attempt_id, work)
        self._require_consumed_provider_call_permit_id(
            current.job_id, attempt.attempt_id, expected_work_request_id=work.work_request_id
        )
        now = self._now()
        upload = self._uploaded_artwork_record(
            current=current,
            attempt=attempt,
            observation=command.observation,
            now=now,
        )
        updated = self._job_update(
            current,
            state=ControlJobState.PRODUCT_DRAFT_SYNCING,
            record_version=current.record_version + 1,
            event_sequence=current.event_sequence + 1,
            uploaded_artwork_id=upload.upload_id,
            uploaded_image_id=upload.image_id,
            uploaded_artwork_fingerprint=upload.fingerprint,
            upload_outcome_unconfirmed=False,
            updated_at=now,
        )
        receipt = self._receipt_for(
            command_type="record_provider_upload_success",
            command=command,
            updated=updated,
            work_id=None,
            now=now,
        )
        return self._commit_worker_or_replay(
            CommandCommit(
                current=current,
                updated=updated,
                event=self._event(updated, "PROVIDER_UPLOAD_CONFIRMED", now),
                receipt=receipt,
                uploaded_artwork=upload,
            )
        ).response

    def record_provider_upload_outcome_unknown(
        self, command: RecordProviderUploadOutcomeUnknownCommand
    ) -> CommandResponse:
        current, work, replay = self._begin_worker_command(
            "record_provider_upload_outcome_unknown", command
        )
        if replay is not None:
            return replay
        attempt = self._require_upload_attempt(current, command.attempt_id, work)
        self._require_consumed_provider_call_permit_id(
            current.job_id, attempt.attempt_id, expected_work_request_id=work.work_request_id
        )
        if (
            current.state
            not in {
                ControlJobState.PRODUCT_DRAFT_SYNCING,
                ControlJobState.CANCEL_REQUESTED,
            }
            or not current.upload_outcome_unconfirmed
            or current.provider_outcome_unconfirmed
        ):
            raise InvalidControlStateError("Unknown upload outcome is not legal in this state")
        now = self._now()
        receipt = self._receipt_for(
            command_type="record_provider_upload_outcome_unknown",
            command=command,
            updated=current,
            work_id=None,
            now=now,
            response_override=False,
        )
        new_work_id = self._record_id("work", receipt.receipt_id, "reconcile_upload")
        new_work = self._work_request(
            current,
            receipt_id=receipt.receipt_id,
            work_id=new_work_id,
            work_type=WorkType.RECONCILE_PRODUCT,
            review_version=current.review_version,
            due_at=now,
        )
        updated = self._job_update(
            current,
            state=ControlJobState.RECONCILIATION_REQUIRED,
            record_version=current.record_version + 1,
            event_sequence=current.event_sequence + 1,
            active_work_request_id=new_work_id,
            upload_outcome_unconfirmed=True,
            provider_outcome_unconfirmed=False,
            updated_at=now,
        )
        receipt = self._receipt_for(
            command_type="record_provider_upload_outcome_unknown",
            command=command,
            updated=updated,
            work_id=new_work_id,
            now=now,
        )
        new_work = WorkRequest.model_validate(
            {**new_work.model_dump(mode="python"), "receipt_id": receipt.receipt_id}
        )
        return self._commit_worker_or_replay(
            CommandCommit(
                current=current,
                updated=updated,
                event=self._event(updated, "PROVIDER_UPLOAD_OUTCOME_UNKNOWN", now),
                receipt=receipt,
                work_request=new_work,
                work_update=(work, self._settled_work(work, now=now, error_code=command.code)),
            )
        ).response

    def begin_provider_write(self, command: BeginProviderWriteCommand) -> CommandResponse:
        current, work, replay = self._begin_worker_command("begin_provider_write", command)
        if replay is not None:
            return replay
        if current.state is not ControlJobState.PRODUCT_DRAFT_SYNCING:
            raise InvalidControlStateError("Provider writes require product synchronization state")
        if (
            current.uploaded_artwork_id is None
            or current.uploaded_image_id != command.image_id
            or current.upload_outcome_unconfirmed
            or current.provider_upload_attempt_id is None
        ):
            raise InvalidControlStateError(
                "Provider writes require confirmed job artwork authority"
            )
        upload = self.store.get_uploaded_artwork(current.job_id, current.uploaded_artwork_id)
        if (
            upload.image_id != current.uploaded_image_id
            or upload.fingerprint != current.uploaded_artwork_fingerprint
            or upload.source_artifact_fingerprint != current.source_artifact_fingerprint
        ):
            raise InvalidControlStateError(
                "Confirmed artwork checkpoint changed before product write"
            )
        expected_token = self._correlation_token(current.job_id)
        if command.correlation_token != expected_token:
            raise InvalidControlStateError("Provider correlation does not bind the job")
        if current.provider_outcome_unconfirmed:
            if current.provider_write_attempt_id is None:
                raise InvalidControlStateError(
                    "Provider uncertainty has no immutable write attempt"
                )
            existing_permit = self.store.get_provider_call_permit(
                current.job_id, current.provider_write_attempt_id
            )
            if existing_permit.status is ProviderCallPermitStatus.AVAILABLE:
                raise InvalidControlStateError(
                    "The existing unused provider write claim must be resumed"
                )
            raise InvalidControlStateError(
                "A consumed provider write claim requires GET-only reconciliation"
            )
        now = self._now()
        retry_basis = None
        exact_retry_count = 0
        reconciliation_deadline = now + self._reconciliation_window
        if current.product_id is None:
            if current.product_create_attempt_id is not None:
                raise InvalidControlStateError(
                    "The initial product create was already claimed; reconcile without POST"
                )
            operation = ProviderWriteOperation.CREATE
            prior_fingerprint = None
        else:
            if current.provider_payload_fingerprint is None:
                raise InvalidControlStateError("Existing product has no prior payload authority")
            operation = ProviderWriteOperation.UPDATE
            prior_fingerprint = current.provider_payload_fingerprint
            if current.provider_write_attempt_id is not None:
                previous_attempt = self.store.get_provider_write_attempt(
                    current.job_id, current.provider_write_attempt_id
                )
                exact_retry = (
                    previous_attempt.operation is ProviderWriteOperation.UPDATE
                    and previous_attempt.review_version == current.review_version
                )
                if exact_retry:
                    if (
                        command.target_payload_fingerprint
                        != previous_attempt.target_payload_fingerprint
                        or command.image_id != previous_attempt.image_id
                    ):
                        raise InvalidControlStateError(
                            "A reconciled update retry must preserve the exact prior target"
                        )
                    if (
                        previous_attempt.exact_retry_count >= _MAX_EXACT_UPDATE_RETRIES
                        or now >= previous_attempt.reconciliation_deadline
                    ):
                        raise InvalidControlStateError(
                            "The bounded exact provider update retry is exhausted"
                        )
                    retry_basis = previous_attempt
                    exact_retry_count = previous_attempt.exact_retry_count + 1
                    reconciliation_deadline = previous_attempt.reconciliation_deadline
        attempt_id = self._record_id(
            "attempt", current.job_id, work.work_request_id, command.target_payload_fingerprint
        )
        attempt = ProviderWriteAttempt(
            attempt_id=attempt_id,
            job_id=current.job_id,
            work_request_id=work.work_request_id,
            review_version=current.review_version,
            operation=operation,
            product_id=current.product_id,
            image_id=command.image_id,
            target_payload_fingerprint=command.target_payload_fingerprint,
            prior_payload_fingerprint=prior_fingerprint,
            correlation_token=command.correlation_token,
            exact_retry_count=exact_retry_count,
            reconciliation_deadline=reconciliation_deadline,
            started_at=now,
        )
        permit = ProviderCallPermit(
            attempt_id=attempt.attempt_id,
            job_id=current.job_id,
            work_request_id=work.work_request_id,
            created_at=now,
        )
        updated = self._job_update(
            current,
            state=ControlJobState.PRODUCT_DRAFT_SYNCING,
            record_version=current.record_version + 1,
            event_sequence=current.event_sequence + 1,
            provider_write_attempt_id=attempt.attempt_id,
            product_create_attempt_id=(
                attempt.attempt_id
                if operation is ProviderWriteOperation.CREATE
                else current.product_create_attempt_id
            ),
            provider_outcome_unconfirmed=True,
            updated_at=now,
        )
        receipt = self._receipt_for(
            command_type="begin_provider_write",
            command=command,
            updated=updated,
            work_id=None,
            now=now,
        )
        return self._commit_worker_or_replay(
            CommandCommit(
                current=current,
                updated=updated,
                event=self._event(updated, "PROVIDER_WRITE_CLAIMED", now),
                receipt=receipt,
                provider_write_attempt=attempt,
                provider_write_retry_basis=retry_basis,
                provider_call_permit=permit,
            )
        ).response

    def authorize_provider_call(
        self, *, job_id: str, attempt_id: str
    ) -> ProviderWriteAttempt | None:
        """Consume the exact one-shot permit immediately before POST or PUT."""

        current = self.store.get_job(job_id)
        if (
            current.state is not ControlJobState.PRODUCT_DRAFT_SYNCING
            or current.provider_write_attempt_id != attempt_id
            or current.active_work_request_id is None
            or not current.provider_outcome_unconfirmed
            or current.upload_outcome_unconfirmed
        ):
            return None
        attempt = self.store.get_provider_write_attempt(job_id, attempt_id)
        if attempt.review_version != current.review_version:
            return None
        work = self.store.get_work_request(job_id, current.active_work_request_id)
        if (
            work.work_type is not WorkType.SYNCHRONIZE_PRODUCT
            or work.review_version != current.review_version
            or work.status not in {WorkRequestStatus.CLAIMED, WorkRequestStatus.DISPATCHED}
        ):
            return None
        permit = self.store.get_provider_call_permit(job_id, attempt_id)
        if attempt.work_request_id != work.work_request_id:
            origin = self.store.get_work_request(job_id, attempt.work_request_id)
            recoverable = (
                origin.work_type is WorkType.SYNCHRONIZE_PRODUCT
                and origin.status is WorkRequestStatus.COMPLETED
                and permit.status is ProviderCallPermitStatus.AVAILABLE
                and permit.consumed_at is None
                and permit.consumed_work_request_id is None
                and current.uploaded_image_id == attempt.image_id
                and (
                    (
                        attempt.operation is ProviderWriteOperation.CREATE
                        and current.product_id is None
                        and current.product_create_attempt_id == attempt.attempt_id
                    )
                    or (
                        attempt.operation is ProviderWriteOperation.UPDATE
                        and current.product_id == attempt.product_id
                        and current.provider_payload_fingerprint
                        == attempt.prior_payload_fingerprint
                    )
                )
            )
            if not recoverable:
                return None
        consumed = self.store.consume_provider_call_permit(
            current,
            work,
            attempt_id,
            now=self._now(),
        )
        return attempt if consumed is not None else None

    def record_product_sync_success(
        self, command: RecordProductSyncSuccessCommand
    ) -> CommandResponse:
        return self._record_product_success(
            command_type="record_product_sync_success",
            command=command,
            observation=command.observation,
        )

    def record_pricing_success(self, command: RecordPricingSuccessCommand) -> CommandResponse:
        """Persist complete read-only economics evidence and settle its exact work."""

        current, work, replay = self._begin_worker_command("record_pricing_success", command)
        if replay is not None:
            return replay
        if current.state not in {
            ControlJobState.PRICING_REFRESHING,
            ControlJobState.CANCEL_REQUESTED,
        }:
            raise InvalidControlStateError("Pricing success is not legal in this state")
        if (
            work.work_type is not WorkType.REFRESH_ECONOMICS
            or work.review_version != current.review_version
        ):
            raise InvalidControlStateError("Pricing success does not bind exact economics work")
        if (
            current.product_sync_id is None
            or current.product_sync_fingerprint is None
            or current.synchronized_review_version != current.review_version
        ):
            raise InvalidControlStateError("Pricing success requires the current product sync")
        sync = self.store.get_product_sync(current.job_id, current.product_sync_id)
        if (
            sync.job_id != current.job_id
            or sync.review_version != current.review_version
            or sync.fingerprint != current.product_sync_fingerprint
            or command.estimate.product_sync_fingerprint != sync.fingerprint
        ):
            raise InvalidControlStateError("Pricing evidence changed product sync authority")

        actual_by_variant = {item.variant_id: item for item in command.estimate.variants}
        observed_costs = ProductCostEvidence(
            product_sync_fingerprint=sync.fingerprint,
            observed_at=command.estimate.product_cost_observed_at,
            variants=tuple(
                ProductVariantCostEvidence(
                    variant_id=item.variant_id,
                    retail_price_cents=actual_by_variant[item.variant_id].retail_price_cents,
                    production_cost_cents=(
                        actual_by_variant[item.variant_id].production_cost_cents
                    ),
                )
                for item in sync.variants
                if item.variant_id in actual_by_variant
            ),
        )
        if (
            command.estimate.product_cost_evidence_fingerprint != observed_costs.fingerprint
            or set(actual_by_variant) != {item.variant_id for item in sync.variants}
            or any(
                actual_by_variant[item.variant_id].retail_price_cents != item.retail_price_cents
                for item in sync.variants
            )
        ):
            raise InvalidControlStateError(
                "Pricing evidence does not cover the exact synchronized variants"
            )

        now = self._now()
        if command.estimate.calculated_at > now or command.estimate.fresh_until <= now:
            raise InvalidControlStateError("Pricing evidence is not fresh at settlement")
        snapshot_id = self._record_id(
            "pricing", current.job_id, work.work_request_id, command.estimate.fingerprint
        )
        evidence = PricingEvidenceRecord(
            snapshot_id=snapshot_id,
            job_id=current.job_id,
            review_version=current.review_version,
            product_sync_fingerprint=sync.fingerprint,
            fingerprint=command.estimate.fingerprint,
            estimate=command.estimate,
            created_at=command.estimate.calculated_at,
        )
        snapshot = PricingSnapshot(
            snapshot_id=snapshot_id,
            job_id=current.job_id,
            review_version=current.review_version,
            product_sync_fingerprint=sync.fingerprint,
            fingerprint=evidence.fingerprint,
            fresh_until=command.estimate.fresh_until,
            created_at=command.estimate.calculated_at,
        )
        cancelled = current.cancellation_requested_at is not None
        updated = self._job_update(
            current,
            state=(ControlJobState.CANCELLED if cancelled else ControlJobState.AWAITING_APPROVAL),
            record_version=current.record_version + 1,
            event_sequence=current.event_sequence + 1,
            pricing_snapshot_id=snapshot.snapshot_id,
            pricing_snapshot_fingerprint=snapshot.fingerprint,
            active_work_request_id=None,
            updated_at=now,
        )
        receipt = self._receipt_for(
            command_type="record_pricing_success",
            command=command,
            updated=updated,
            work_id=None,
            now=now,
        )
        return self._commit_worker_or_replay(
            CommandCommit(
                current=current,
                updated=updated,
                event=self._event(
                    updated,
                    "PRICING_REFRESH_CANCELLED" if cancelled else "PRICING_REFRESHED",
                    now,
                ),
                receipt=receipt,
                pricing_evidence=evidence,
                pricing_snapshot=snapshot,
                work_update=(work, self._settled_work(work, now=now)),
            )
        ).response

    def record_product_write_outcome_unknown(
        self, command: RecordProductWriteOutcomeUnknownCommand
    ) -> CommandResponse:
        current, work, replay = self._begin_worker_command(
            "record_product_write_outcome_unknown", command
        )
        if replay is not None:
            return replay
        attempt = self._require_attempt(current, command.attempt_id, work)
        self._require_consumed_provider_call_permit(attempt, work=work)
        if current.state not in {
            ControlJobState.PRODUCT_DRAFT_SYNCING,
            ControlJobState.CANCEL_REQUESTED,
        }:
            raise InvalidControlStateError("Unknown write outcome is not legal in this state")
        now = self._now()
        receipt = self._receipt_for(
            command_type="record_product_write_outcome_unknown",
            command=command,
            updated=current,
            work_id=None,
            now=now,
            response_override=False,
        )
        new_work_id = self._record_id("work", receipt.receipt_id, WorkType.RECONCILE_PRODUCT.value)
        new_work = self._work_request(
            current,
            receipt_id=receipt.receipt_id,
            work_id=new_work_id,
            work_type=WorkType.RECONCILE_PRODUCT,
            review_version=current.review_version,
            due_at=now,
        )
        updated = self._job_update(
            current,
            state=ControlJobState.RECONCILIATION_REQUIRED,
            record_version=current.record_version + 1,
            event_sequence=current.event_sequence + 1,
            active_work_request_id=new_work_id,
            provider_outcome_unconfirmed=True,
            updated_at=now,
        )
        receipt = self._receipt_for(
            command_type="record_product_write_outcome_unknown",
            command=command,
            updated=updated,
            work_id=new_work_id,
            now=now,
        )
        new_work = WorkRequest.model_validate(
            {**new_work.model_dump(mode="python"), "receipt_id": receipt.receipt_id}
        )
        settled = self._settled_work(work, now=now, error_code=command.code)
        return self._commit_worker_or_replay(
            CommandCommit(
                current=current,
                updated=updated,
                event=self._event(updated, "PROVIDER_WRITE_OUTCOME_UNKNOWN", now),
                receipt=receipt,
                work_request=new_work,
                work_update=(work, settled),
            )
        ).response

    def record_upload_reconciliation_observation(
        self, command: RecordUploadReconciliationObservationCommand
    ) -> CommandResponse:
        """Apply a bounded GET-only upload observation through application authority."""

        current, work, replay = self._begin_worker_command(
            "record_upload_reconciliation_observation", command
        )
        if replay is not None:
            return replay
        if (
            current.state
            not in {
                ControlJobState.RECONCILIATION_REQUIRED,
                ControlJobState.CANCEL_REQUESTED,
            }
            or not current.upload_outcome_unconfirmed
            or current.provider_outcome_unconfirmed
        ):
            raise InvalidControlStateError("Upload reconciliation authority is not active")
        attempt = self._require_upload_attempt(current, command.attempt_id, work)
        now = self._now()
        observation_record = UploadReconciliationObservationRecord(
            observation_id=self._record_id(
                "upload_observation", current.job_id, work.work_request_id, command.outcome.value
            ),
            job_id=current.job_id,
            work_request_id=work.work_request_id,
            attempt_id=attempt.attempt_id,
            outcome=command.outcome,
            observed_image_id=None if command.upload is None else command.upload.image_id,
            observed_at=now,
        )
        cancellation = current.cancellation_requested_at is not None
        if command.outcome is ReconciliationOutcome.TARGET_MATCH:
            assert command.upload is not None
            upload = self._uploaded_artwork_record(
                current=current,
                attempt=attempt,
                observation=command.upload,
                now=now,
            )
            receipt = self._receipt_for(
                command_type="record_upload_reconciliation_observation",
                command=command,
                updated=current,
                work_id=None,
                now=now,
                response_override=False,
            )
            next_work = None
            work_id = None
            target = (
                ControlJobState.CANCELLED if cancellation else ControlJobState.PRODUCT_DRAFT_SYNCING
            )
            if not cancellation:
                work_id = self._record_id("work", receipt.receipt_id, "resume_product_sync")
                next_work = self._work_request(
                    current,
                    receipt_id=receipt.receipt_id,
                    work_id=work_id,
                    work_type=WorkType.SYNCHRONIZE_PRODUCT,
                    review_version=current.review_version,
                    due_at=now,
                )
            updated = self._job_update(
                current,
                state=target,
                record_version=current.record_version + 1,
                event_sequence=current.event_sequence + 1,
                uploaded_artwork_id=upload.upload_id,
                uploaded_image_id=upload.image_id,
                uploaded_artwork_fingerprint=upload.fingerprint,
                upload_outcome_unconfirmed=False,
                active_work_request_id=work_id,
                updated_at=now,
            )
            receipt = self._receipt_for(
                command_type="record_upload_reconciliation_observation",
                command=command,
                updated=updated,
                work_id=work_id,
                now=now,
            )
            if next_work is not None:
                next_work = WorkRequest.model_validate(
                    {**next_work.model_dump(mode="python"), "receipt_id": receipt.receipt_id}
                )
            return self._commit_worker_or_replay(
                CommandCommit(
                    current=current,
                    updated=updated,
                    event=self._event(updated, "PROVIDER_UPLOAD_RECONCILED", now),
                    receipt=receipt,
                    uploaded_artwork=upload,
                    upload_reconciliation_observation=observation_record,
                    work_request=next_work,
                    work_update=(work, self._settled_work(work, now=now)),
                )
            ).response

        retryable_read = command.outcome in {
            ReconciliationOutcome.NO_MATCH,
            ReconciliationOutcome.UNAVAILABLE,
        }
        if retryable_read and now < attempt.reconciliation_deadline and not cancellation:
            receipt = self._receipt_for(
                command_type="record_upload_reconciliation_observation",
                command=command,
                updated=current,
                work_id=None,
                now=now,
                response_override=False,
            )
            next_id = self._record_id("work", receipt.receipt_id, "upload_reconcile_redrive")
            next_work = self._work_request(
                current,
                receipt_id=receipt.receipt_id,
                work_id=next_id,
                work_type=WorkType.RECONCILE_PRODUCT,
                review_version=current.review_version,
                due_at=now + self._reconciliation_delay,
            )
            updated = self._job_update(
                current,
                state=ControlJobState.RECONCILIATION_REQUIRED,
                record_version=current.record_version + 1,
                event_sequence=current.event_sequence + 1,
                active_work_request_id=next_id,
                updated_at=now,
            )
            receipt = self._receipt_for(
                command_type="record_upload_reconciliation_observation",
                command=command,
                updated=updated,
                work_id=next_id,
                now=now,
            )
            next_work = WorkRequest.model_validate(
                {**next_work.model_dump(mode="python"), "receipt_id": receipt.receipt_id}
            )
            return self._commit_worker_or_replay(
                CommandCommit(
                    current=current,
                    updated=updated,
                    event=self._event(updated, "PROVIDER_UPLOAD_RECONCILIATION_DEFERRED", now),
                    receipt=receipt,
                    upload_reconciliation_observation=observation_record,
                    work_request=next_work,
                    work_update=(work, self._settled_work(work, now=now)),
                )
            ).response

        target = ControlJobState.CANCELLED if cancellation else ControlJobState.FAILED_TERMINAL
        code = (
            _UPLOAD_UNKNOWN_CODE
            if command.outcome
            in {ReconciliationOutcome.NO_MATCH, ReconciliationOutcome.UNAVAILABLE}
            else _UPLOAD_CONFLICT_CODE
        )
        failure = None
        failure_id = None
        if not cancellation:
            failure_id = self._record_id(
                "failure", current.job_id, observation_record.observation_id
            )
            failure = FailureRecord(
                failure_id=failure_id,
                job_id=current.job_id,
                work_request_id=work.work_request_id,
                stage=current.state,
                code=code,
                retryable=False,
                occurred_at=now,
            )
        updated = self._job_update(
            current,
            state=target,
            record_version=current.record_version + 1,
            event_sequence=current.event_sequence + 1,
            active_work_request_id=None,
            failure_id=failure_id,
            updated_at=now,
        )
        receipt = self._receipt_for(
            command_type="record_upload_reconciliation_observation",
            command=command,
            updated=updated,
            work_id=None,
            now=now,
        )
        return self._commit_worker_or_replay(
            CommandCommit(
                current=current,
                updated=updated,
                event=self._event(updated, "PROVIDER_UPLOAD_RECONCILIATION_ENDED", now),
                receipt=receipt,
                upload_reconciliation_observation=observation_record,
                failure=failure,
                work_update=(work, self._settled_work(work, now=now, error_code=code)),
            )
        ).response

    def record_reconciliation_observation(
        self, command: RecordReconciliationObservationCommand
    ) -> CommandResponse:
        current, work, replay = self._begin_worker_command(
            "record_reconciliation_observation", command
        )
        if replay is not None:
            return replay
        if current.state not in {
            ControlJobState.RECONCILIATION_REQUIRED,
            ControlJobState.CANCEL_REQUESTED,
        }:
            raise InvalidControlStateError("Reconciliation evidence requires reconciliation state")
        attempt = self._require_attempt(current, command.attempt_id, work)
        now = self._now()
        observation_record = ReconciliationObservationRecord(
            observation_id=self._record_id(
                "observation", current.job_id, work.work_request_id, command.outcome.value
            ),
            job_id=current.job_id,
            work_request_id=work.work_request_id,
            attempt_id=attempt.attempt_id,
            outcome=command.outcome,
            observed_product_id=None if command.product is None else command.product.product_id,
            observed_payload_fingerprint=(
                command.observed_payload_fingerprint
                if command.product is None
                else command.product.request_fingerprint
            ),
            observed_at=now,
        )
        if command.outcome is ReconciliationOutcome.TARGET_MATCH:
            assert command.product is not None
            return self._record_product_success(
                command_type="record_reconciliation_observation",
                command=command,
                observation=command.product,
                reconciliation_observation=observation_record,
                already_loaded=(current, work, attempt, now),
            )

        cancellation = current.cancellation_requested_at is not None
        if command.outcome is ReconciliationOutcome.PRIOR_MATCH:
            if attempt.operation is not ProviderWriteOperation.UPDATE:
                raise InvalidControlStateError("Only an update can match a prior payload")
            if command.observed_payload_fingerprint != attempt.prior_payload_fingerprint:
                raise InvalidControlStateError(
                    "Prior-match evidence does not bind the prior provider payload"
                )
            if cancellation:
                return self._complete_reconciliation_terminal(
                    command=command,
                    current=current,
                    work=work,
                    observation=observation_record,
                    now=now,
                    cancelled=True,
                    provider_outcome_unconfirmed=False,
                )
            if (
                attempt.exact_retry_count >= _MAX_EXACT_UPDATE_RETRIES
                or now >= attempt.reconciliation_deadline
            ):
                return self._complete_reconciliation_terminal(
                    command=command,
                    current=current,
                    work=work,
                    observation=observation_record,
                    now=now,
                    cancelled=False,
                    code_override=_UPDATE_RETRY_EXHAUSTED_CODE,
                    provider_outcome_unconfirmed=False,
                )
            return self._retry_exact_update(
                command=command,
                current=current,
                work=work,
                observation=observation_record,
                now=now,
            )

        retryable_read = command.outcome in {
            ReconciliationOutcome.NO_MATCH,
            ReconciliationOutcome.UNAVAILABLE,
        }
        if command.outcome is ReconciliationOutcome.NO_MATCH and (
            attempt.operation is not ProviderWriteOperation.CREATE
        ):
            raise InvalidControlStateError("Only an initial create can have no correlated match")
        if command.outcome is ReconciliationOutcome.MISSING and (
            attempt.operation is not ProviderWriteOperation.UPDATE
        ):
            raise InvalidControlStateError("Only an existing product update can be missing")
        if retryable_read and now < attempt.reconciliation_deadline and not cancellation:
            return self._redrive_reconciliation(
                command=command,
                current=current,
                work=work,
                observation=observation_record,
                now=now,
            )
        return self._complete_reconciliation_terminal(
            command=command,
            current=current,
            work=work,
            observation=observation_record,
            now=now,
            cancelled=cancellation,
        )

    def _record_product_success(
        self,
        *,
        command_type: str,
        command: RecordProductSyncSuccessCommand | RecordReconciliationObservationCommand,
        observation: ProductSyncObservation,
        reconciliation_observation: ReconciliationObservationRecord | None = None,
        already_loaded: tuple[ControlJobRecord, WorkRequest, ProviderWriteAttempt, datetime]
        | None = None,
    ) -> CommandResponse:
        if already_loaded is None:
            current, work, replay = self._begin_worker_command(command_type, command)
            if replay is not None:
                return replay
            attempt = self._require_attempt(current, command.attempt_id, work)
            self._require_consumed_provider_call_permit(attempt, work=work)
            now = self._now()
        else:
            current, work, attempt, now = already_loaded
        if observation.request_fingerprint != attempt.target_payload_fingerprint:
            raise InvalidControlStateError("Provider success does not match the target payload")
        if observation.image_id != attempt.image_id:
            raise InvalidControlStateError("Provider success changed the uploaded image")
        if attempt.product_id is not None and observation.product_id != attempt.product_id:
            raise InvalidControlStateError("Provider success changed immutable product identity")
        cancellation = current.cancellation_requested_at is not None
        receipt = self._receipt_for(
            command_type=command_type,
            command=command,
            updated=current,
            work_id=None,
            now=now,
            response_override=False,
        )
        sync_material = {
            "job_id": current.job_id,
            "review_version": current.review_version,
            "product_id": observation.product_id,
            "image_id": observation.image_id,
            "printify_shop_id": observation.printify_shop_id,
            "payload_fingerprint": observation.request_fingerprint,
            "response_fingerprint": observation.response_fingerprint,
            "mockups": [item.model_dump(mode="json") for item in observation.mockups],
            "variants": [item.model_dump(mode="json") for item in observation.variants],
            "provider_locked": False,
            "provider_published": False,
            "synchronized_at": now.isoformat(),
        }
        sync = ProductSyncRecord(
            sync_id=self._record_id("sync", current.job_id, attempt.attempt_id),
            fingerprint=product_sync_record_fingerprint(sync_material),
            **sync_material,
        )
        next_work = None
        work_id = None
        target = ControlJobState.CANCELLED if cancellation else ControlJobState.PRICING_REFRESHING
        if not cancellation:
            work_id = self._record_id("work", receipt.receipt_id, WorkType.REFRESH_ECONOMICS.value)
            next_work = self._work_request(
                current,
                receipt_id=receipt.receipt_id,
                work_id=work_id,
                work_type=WorkType.REFRESH_ECONOMICS,
                review_version=current.review_version,
                due_at=now,
            )
        updated = self._job_update(
            current,
            state=target,
            record_version=current.record_version + 1,
            event_sequence=current.event_sequence + 1,
            product_id=observation.product_id,
            provider_payload_fingerprint=observation.request_fingerprint,
            product_sync_id=sync.sync_id,
            synchronized_review_version=current.review_version,
            product_sync_fingerprint=sync.fingerprint,
            pricing_snapshot_id=None,
            pricing_snapshot_fingerprint=None,
            active_work_request_id=work_id,
            provider_outcome_unconfirmed=False,
            updated_at=now,
        )
        receipt = self._receipt_for(
            command_type=command_type,
            command=command,
            updated=updated,
            work_id=work_id,
            now=now,
        )
        if next_work is not None:
            next_work = WorkRequest.model_validate(
                {**next_work.model_dump(mode="python"), "receipt_id": receipt.receipt_id}
            )
        settled = self._settled_work(work, now=now)
        return self._commit_worker_or_replay(
            CommandCommit(
                current=current,
                updated=updated,
                event=self._event(updated, "PRODUCT_DRAFT_SYNCHRONIZED", now),
                receipt=receipt,
                product_sync=sync,
                reconciliation_observation=reconciliation_observation,
                work_request=next_work,
                work_update=(work, settled),
            )
        ).response

    def _retry_exact_update(
        self,
        *,
        command: RecordReconciliationObservationCommand,
        current: ControlJobRecord,
        work: WorkRequest,
        observation: ReconciliationObservationRecord,
        now: datetime,
    ) -> CommandResponse:
        receipt = self._receipt_for(
            command_type="record_reconciliation_observation",
            command=command,
            updated=current,
            work_id=None,
            now=now,
            response_override=False,
        )
        next_id = self._record_id("work", receipt.receipt_id, "retry_update")
        next_work = self._work_request(
            current,
            receipt_id=receipt.receipt_id,
            work_id=next_id,
            work_type=WorkType.SYNCHRONIZE_PRODUCT,
            review_version=current.review_version,
            due_at=now,
        )
        updated = self._job_update(
            current,
            state=ControlJobState.PRODUCT_DRAFT_SYNCING,
            record_version=current.record_version + 1,
            event_sequence=current.event_sequence + 1,
            active_work_request_id=next_id,
            provider_outcome_unconfirmed=False,
            updated_at=now,
        )
        receipt = self._receipt_for(
            command_type="record_reconciliation_observation",
            command=command,
            updated=updated,
            work_id=next_id,
            now=now,
        )
        next_work = WorkRequest.model_validate(
            {**next_work.model_dump(mode="python"), "receipt_id": receipt.receipt_id}
        )
        return self._commit_worker_or_replay(
            CommandCommit(
                current=current,
                updated=updated,
                event=self._event(updated, "PRODUCT_UPDATE_RETRY_REQUESTED", now),
                receipt=receipt,
                reconciliation_observation=observation,
                work_request=next_work,
                work_update=(work, self._settled_work(work, now=now)),
            )
        ).response

    def _redrive_reconciliation(
        self,
        *,
        command: RecordReconciliationObservationCommand,
        current: ControlJobRecord,
        work: WorkRequest,
        observation: ReconciliationObservationRecord,
        now: datetime,
    ) -> CommandResponse:
        receipt = self._receipt_for(
            command_type="record_reconciliation_observation",
            command=command,
            updated=current,
            work_id=None,
            now=now,
            response_override=False,
        )
        next_id = self._record_id("work", receipt.receipt_id, "reconcile_redrive")
        next_work = self._work_request(
            current,
            receipt_id=receipt.receipt_id,
            work_id=next_id,
            work_type=WorkType.RECONCILE_PRODUCT,
            review_version=current.review_version,
            due_at=now + self._reconciliation_delay,
        )
        updated = self._job_update(
            current,
            state=ControlJobState.RECONCILIATION_REQUIRED,
            record_version=current.record_version + 1,
            event_sequence=current.event_sequence + 1,
            active_work_request_id=next_id,
            updated_at=now,
        )
        receipt = self._receipt_for(
            command_type="record_reconciliation_observation",
            command=command,
            updated=updated,
            work_id=next_id,
            now=now,
        )
        next_work = WorkRequest.model_validate(
            {**next_work.model_dump(mode="python"), "receipt_id": receipt.receipt_id}
        )
        return self._commit_worker_or_replay(
            CommandCommit(
                current=current,
                updated=updated,
                event=self._event(updated, "PRODUCT_RECONCILIATION_DEFERRED", now),
                receipt=receipt,
                reconciliation_observation=observation,
                work_request=next_work,
                work_update=(work, self._settled_work(work, now=now)),
            )
        ).response

    def _complete_reconciliation_terminal(
        self,
        *,
        command: RecordReconciliationObservationCommand,
        current: ControlJobRecord,
        work: WorkRequest,
        observation: ReconciliationObservationRecord,
        now: datetime,
        cancelled: bool,
        code_override: str | None = None,
        provider_outcome_unconfirmed: bool = True,
    ) -> CommandResponse:
        target = ControlJobState.CANCELLED if cancelled else ControlJobState.FAILED_TERMINAL
        code = code_override or (
            _CREATE_UNKNOWN_CODE
            if command.outcome
            in {ReconciliationOutcome.NO_MATCH, ReconciliationOutcome.UNAVAILABLE}
            else _RECONCILIATION_CONFLICT_CODE
        )
        failure = None
        failure_id = None
        if not cancelled:
            failure_id = self._record_id("failure", current.job_id, observation.observation_id)
            failure = FailureRecord(
                failure_id=failure_id,
                job_id=current.job_id,
                work_request_id=work.work_request_id,
                stage=current.state,
                code=code,
                retryable=False,
                occurred_at=now,
            )
        updated = self._job_update(
            current,
            state=target,
            record_version=current.record_version + 1,
            event_sequence=current.event_sequence + 1,
            active_work_request_id=None,
            failure_id=failure_id,
            provider_outcome_unconfirmed=provider_outcome_unconfirmed,
            updated_at=now,
        )
        receipt = self._receipt_for(
            command_type="record_reconciliation_observation",
            command=command,
            updated=updated,
            work_id=None,
            now=now,
        )
        settled = self._settled_work(work, now=now, error_code=code)
        return self._commit_worker_or_replay(
            CommandCommit(
                current=current,
                updated=updated,
                event=self._event(updated, "PRODUCT_RECONCILIATION_ENDED", now),
                receipt=receipt,
                reconciliation_observation=observation,
                failure=failure,
                work_update=(work, settled),
            )
        ).response

    def _begin_worker_command(
        self, command_type: str, command: object
    ) -> tuple[ControlJobRecord, WorkRequest, CommandResponse | None]:
        assert hasattr(command, "job_id") and hasattr(command, "work_request_id")
        current = self.store.get_job(command.job_id)
        key_digest = self._worker_key(command_type, command.job_id, command.work_request_id)
        request_fingerprint = command_request_fingerprint(
            command_type=command_type,
            payload=command.model_dump(mode="json"),
        )
        receipt = self.store.resolve_receipt(
            current.owner_id, command_type, current.job_id, key_digest
        )
        if receipt is not None:
            if receipt.request_fingerprint != request_fingerprint:
                raise IdempotencyConflictError("Worker command identity was reused")
            if receipt.work_request_id is not None:
                self.store.nudge_pending_work(
                    receipt.job_id, receipt.work_request_id, now=self._now()
                )
            return (
                current,
                self.store.get_work_request(command.job_id, command.work_request_id),
                receipt.response,
            )
        if current.record_version != command.expected_record_version:
            raise ConcurrentControlModificationError("The job changed before worker settlement")
        work = self._require_active_work(current, command.work_request_id)
        return current, work, None

    def _commit_simple(
        self,
        *,
        command_type: str,
        command: object,
        current: ControlJobRecord,
        updated: ControlJobRecord,
        event_name: str,
        now: datetime,
    ) -> CommandReceipt:
        receipt = self._receipt_for(
            command_type=command_type,
            command=command,
            updated=updated,
            work_id=None,
            now=now,
        )
        return self._commit_worker_or_replay(
            CommandCommit(
                current=current,
                updated=updated,
                event=self._event(updated, event_name, now),
                receipt=receipt,
            )
        )

    def _receipt_for(
        self,
        *,
        command_type: str,
        command: object,
        updated: ControlJobRecord,
        work_id: str | None,
        now: datetime,
        response_override: bool = True,
    ) -> CommandReceipt:
        del response_override
        key_digest = self._worker_key(command_type, command.job_id, command.work_request_id)
        request_fingerprint = command_request_fingerprint(
            command_type=command_type,
            payload=command.model_dump(mode="json"),
        )
        receipt_id = self._record_id(
            "receipt", updated.owner_id, command_type, updated.job_id, key_digest
        )
        return CommandReceipt(
            receipt_id=receipt_id,
            owner_id=updated.owner_id,
            job_id=updated.job_id,
            command_type=command_type,
            idempotency_key_digest=key_digest,
            request_fingerprint=request_fingerprint,
            response=CommandResponse(
                job_id=updated.job_id,
                state=updated.state,
                record_version=updated.record_version,
                review_version=updated.review_version,
                work_request_id=work_id,
            ),
            work_request_id=work_id,
            created_at=now,
        )

    def _commit_worker_or_replay(self, commit: CommandCommit) -> CommandReceipt:
        try:
            return self._commit_or_replay(commit)
        except ConcurrentControlModificationError:
            if commit.work_update is None:
                raise
            latest_job = self.store.get_job(commit.current.job_id)
            if latest_job != commit.current:
                raise
            expected, changed = commit.work_update
            latest = self.store.get_work_request(expected.job_id, expected.work_request_id)
            if latest_job.active_work_request_id != latest.work_request_id or latest.status not in {
                WorkRequestStatus.CLAIMED,
                WorkRequestStatus.DISPATCHED,
            }:
                raise
            rebased = self._settled_work(
                latest,
                now=max(changed.updated_at, latest.updated_at),
                error_code=changed.last_error_code,
            )
            return self._commit_or_replay(replace(commit, work_update=(latest, rebased)))

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
                    receipt.job_id, receipt.work_request_id, now=self._now()
                )
            return receipt

    def _require_attempt(
        self, job: ControlJobRecord, attempt_id: str, work: WorkRequest
    ) -> ProviderWriteAttempt:
        if job.provider_write_attempt_id != attempt_id:
            raise InvalidControlStateError("Provider evidence does not bind the active attempt")
        attempt = self.store.get_provider_write_attempt(job.job_id, attempt_id)
        if (
            attempt.work_request_id != work.work_request_id
            and work.work_type is not WorkType.RECONCILE_PRODUCT
        ):
            permit = self.store.get_provider_call_permit(job.job_id, attempt_id)
            recovery_consume = (
                work.work_type is WorkType.SYNCHRONIZE_PRODUCT
                and permit.status is ProviderCallPermitStatus.CONSUMED
                and permit.consumed_work_request_id == work.work_request_id
            )
            if not recovery_consume:
                raise InvalidControlStateError("Provider attempt does not bind the active work")
        if attempt.review_version != job.review_version:
            raise InvalidControlStateError("Provider attempt does not bind the current review")
        return attempt

    def _require_upload_attempt(
        self, job: ControlJobRecord, attempt_id: str, work: WorkRequest
    ) -> ProviderUploadAttempt:
        if job.provider_upload_attempt_id != attempt_id:
            raise InvalidControlStateError("Upload evidence does not bind the active attempt")
        attempt = self.store.get_provider_upload_attempt(job.job_id, attempt_id)
        if attempt.work_request_id != work.work_request_id:
            permit = self.store.get_provider_call_permit(job.job_id, attempt_id)
            recovery_consume = (
                work.work_type is WorkType.SYNCHRONIZE_PRODUCT
                and permit.status is ProviderCallPermitStatus.CONSUMED
                and permit.consumed_work_request_id == work.work_request_id
            )
            if work.work_type is not WorkType.RECONCILE_PRODUCT and not recovery_consume:
                raise InvalidControlStateError("Upload attempt does not bind the active work")
        if attempt.source_artifact_fingerprint != job.source_artifact_fingerprint:
            raise InvalidControlStateError("Upload attempt changed pinned source authority")
        return attempt

    def _require_consumed_provider_call_permit(
        self, attempt: ProviderWriteAttempt, *, work: WorkRequest
    ) -> None:
        self._require_consumed_provider_call_permit_id(
            attempt.job_id,
            attempt.attempt_id,
            expected_work_request_id=work.work_request_id,
        )

    def _require_consumed_provider_call_permit_id(
        self,
        job_id: str,
        attempt_id: str,
        *,
        expected_work_request_id: str,
    ) -> None:
        permit = self.store.get_provider_call_permit(job_id, attempt_id)
        if (
            permit.status is not ProviderCallPermitStatus.CONSUMED
            or permit.consumed_at is None
            or permit.consumed_work_request_id != expected_work_request_id
        ):
            raise InvalidControlStateError(
                "Provider outcome evidence requires a consumed one-shot call permit"
            )

    def _uploaded_artwork_record(
        self,
        *,
        current: ControlJobRecord,
        attempt: ProviderUploadAttempt,
        observation: UploadedArtworkObservation,
        now: datetime,
    ) -> UploadedArtworkRecord:
        source = self.store.get_source_artifact(current.job_id)
        if (
            source.fingerprint != current.source_artifact_fingerprint
            or attempt.source_artifact_fingerprint != source.fingerprint
            or observation.file_name != attempt.file_name
            or observation.size_bytes != source.size_bytes
            or observation.mime_type != source.media_type
            or (
                source.width is not None
                and source.height is not None
                and (observation.width, observation.height) != (source.width, source.height)
            )
        ):
            raise InvalidControlStateError("Provider upload evidence does not match pinned source")
        material = {
            "job_id": current.job_id,
            "attempt_id": attempt.attempt_id,
            "source_artifact_fingerprint": source.fingerprint,
            "image_id": observation.image_id,
            "file_name": observation.file_name,
            "width": observation.width,
            "height": observation.height,
            "size_bytes": observation.size_bytes,
            "mime_type": observation.mime_type,
        }
        return UploadedArtworkRecord(
            upload_id=self._record_id("upload", current.job_id, attempt.attempt_id),
            fingerprint=canonical_fingerprint(material),
            confirmed_at=now,
            **material,
        )

    def _require_active_work(self, job: ControlJobRecord, work_request_id: str) -> WorkRequest:
        if job.active_work_request_id != work_request_id:
            raise WorkNotActiveError("The worker no longer owns the current job operation")
        work = self.store.get_work_request(job.job_id, work_request_id)
        if work.status not in {WorkRequestStatus.CLAIMED, WorkRequestStatus.DISPATCHED}:
            raise WorkNotActiveError("The work request is not active")
        return work

    def _work_request(
        self,
        job: ControlJobRecord,
        *,
        receipt_id: str,
        work_id: str,
        work_type: WorkType,
        review_version: int | None,
        due_at: datetime,
    ) -> WorkRequest:
        created_at = self._now()
        return WorkRequest(
            work_request_id=work_id,
            owner_id=job.owner_id,
            job_id=job.job_id,
            receipt_id=receipt_id,
            work_type=work_type,
            review_version=review_version,
            input_fingerprint=work_input_fingerprint(
                work_type=work_type,
                job_id=job.job_id,
                work_request_id=work_id,
            ),
            execution_name=deterministic_execution_name(work_id),
            next_dispatch_at=due_at,
            created_at=created_at,
            updated_at=created_at,
        )

    @staticmethod
    def _settled_work(
        work: WorkRequest,
        *,
        now: datetime,
        error_code: str | None = None,
    ) -> WorkRequest:
        return WorkRequest.model_validate(
            {
                **work.model_dump(mode="python"),
                "status": WorkRequestStatus.COMPLETED,
                "claim_id": None,
                "lease_expires_at": None,
                "last_error_code": error_code,
                "updated_at": now,
            }
        )

    @staticmethod
    def _job_update(job: ControlJobRecord, **updates: object) -> ControlJobRecord:
        return ControlJobRecord.model_validate({**job.model_dump(mode="python"), **updates})

    @staticmethod
    def _event(job: ControlJobRecord, name: str, now: datetime) -> DomainEvent:
        return DomainEvent(
            job_id=job.job_id,
            sequence=job.event_sequence,
            name=name,
            occurred_at=now,
            details={"state": job.state.value, "record_version": job.record_version},
        )

    @staticmethod
    def _record_id(prefix: str, *parts: str) -> str:
        return f"{prefix}_{sha256(chr(0).join(parts).encode()).hexdigest()[:40]}"

    @staticmethod
    def upload_file_name(job_id: str, content_sha256: str) -> str:
        """Return a source-bound provider filename without exposing the raw job id."""

        identity = sha256(f"{job_id}\0{content_sha256}".encode()).hexdigest()[:24]
        return f"mr-lister-{identity}-{content_sha256[:16]}.png"

    @staticmethod
    def _worker_key(command_type: str, job_id: str, work_request_id: str) -> str:
        return canonical_fingerprint(
            {
                "command_type": command_type,
                "job_id": job_id,
                "work_request_id": work_request_id,
            }
        )

    @staticmethod
    def _correlation_token(job_id: str) -> str:
        digest = sha256(f"mr-lister:provider-draft:{job_id}".encode()).hexdigest()[:24]
        return f"ml-{digest}"

    def _now(self) -> datetime:
        now = self._clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise InvalidControlStateError("The control clock must return a timezone-aware value")
        return now
