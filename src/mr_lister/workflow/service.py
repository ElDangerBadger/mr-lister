"""Synchronous Phase 1 workflow with real guards and fake boundaries."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from hashlib import sha256
from uuid import uuid4

from pydantic import ValidationError

from mr_lister.contracts import (
    ApprovalStatus,
    ArtworkAnalysis,
    JobRecord,
    JobState,
    ListingIntelligence,
    ReviewSnapshot,
    can_transition,
)
from mr_lister.workflow.artifacts import ArtifactStore, InMemoryArtifactStore
from mr_lister.workflow.errors import (
    ApprovalWaitExpiredError,
    ExternalWritePendingError,
    IntelligenceConfigurationError,
    IntelligenceUnavailableError,
    InvalidGeneratedOutputError,
    InvalidStateError,
    StaleApprovalError,
)
from mr_lister.workflow.models import (
    ApprovalWaitRecord,
    ApprovalWaitStatus,
    ExternalWriteClaim,
    ExternalWriteStatus,
    ListingRevisionRequest,
    RunReport,
    WorkflowEvent,
)
from mr_lister.workflow.ports import IntelligencePort, ProductionPort
from mr_lister.workflow.profiles import ProductProfileRepository
from mr_lister.workflow.store import JobStore
from mr_lister.workflow.validation import validate_artwork, validate_listing


class ListingWorkflow:
    def __init__(
        self,
        *,
        store: JobStore,
        profiles: ProductProfileRepository,
        intelligence: IntelligencePort,
        production: ProductionPort,
        artifacts: ArtifactStore | None = None,
        clock: Callable[[], datetime] | None = None,
        job_id_factory: Callable[[], str] | None = None,
    ) -> None:
        self.store = store
        self.profiles = profiles
        self.intelligence = intelligence
        self.production = production
        self.artifacts = artifacts or InMemoryArtifactStore()
        self._clock = clock or (lambda: datetime.now(UTC))
        self._job_id_factory = job_id_factory or (lambda: f"job_{uuid4().hex}")

    def submit(
        self,
        *,
        filename: str,
        content_type: str,
        content: bytes,
        idempotency_key: str,
        profile_id: str,
    ) -> JobRecord:
        job = self.intake(
            filename=filename,
            content_type=content_type,
            content=content,
            idempotency_key=idempotency_key,
            profile_id=profile_id,
        )
        if job.state is JobState.INTAKE_VALIDATED:
            return self.prepare(job.job_id)
        return job

    def intake(
        self,
        *,
        filename: str,
        content_type: str,
        content: bytes,
        idempotency_key: str,
        profile_id: str,
    ) -> JobRecord:
        """Validate and persist one job without invoking intelligence or production."""

        artwork = validate_artwork(filename=filename, content_type=content_type, content=content)
        request_fingerprint = f"{artwork.content_sha256}:{profile_id}"
        existing = self.store.resolve_intake(idempotency_key, request_fingerprint)
        if existing is not None:
            return existing
        self.profiles.get(profile_id)
        object_key = self.artifacts.put_artwork(
            content_sha256=artwork.content_sha256,
            content=content,
        )
        job_id = self._job_id_factory()
        now = self._clock()
        job = JobRecord(
            job_id=job_id,
            event_sequence=1,
            state=JobState.UPLOADED,
            review_version=0,
            idempotency_key=idempotency_key,
            artwork_object_key=object_key,
            created_at=now,
            updated_at=now,
        )
        job, created = self.store.create_intake(
            job=job,
            artwork=artwork,
            profile_id=profile_id,
            request_fingerprint=request_fingerprint,
            event=WorkflowEvent(
                sequence=1,
                occurred_at=now,
                name="artwork_uploaded",
                details={"filename": artwork.filename},
            ),
        )
        if not created:
            return job

        self._transition(job_id, JobState.INTAKE_VALIDATED)
        return self.store.get_job(job_id)

    def prepare(self, job_id: str) -> JobRecord:
        """Resume preparation from the first incomplete durable checkpoint."""

        job = self.store.get_job(job_id)
        if job.state in {
            JobState.AWAITING_APPROVAL,
            JobState.NEEDS_REVISION,
            JobState.APPROVED,
            JobState.PUBLISHING,
            JobState.PUBLISHED,
            JobState.VERIFIED,
        }:
            return job
        if job.state in {JobState.INTAKE_VALIDATED, JobState.FAILED_RETRYABLE}:
            job = self._transition(job_id, JobState.ANALYZING_ARTWORK)
        if job.state is JobState.ANALYZING_ARTWORK:
            self._prepare_intelligence_checkpoints(job_id)
            job = self.store.get_job(job_id)
        if job.state is JobState.LISTING_DRAFTED:
            review = self.store.get_review(job_id)
            if not review.validation.passed:
                self._transition(job_id, JobState.NEEDS_REVISION)
                self._event(
                    job_id,
                    "listing_validation_failed",
                    {"issue_codes": [issue.code for issue in review.validation.issues]},
                )
                return self.store.get_job(job_id)
            job = self._transition(job_id, JobState.LISTING_VALIDATED)
        if job.state is JobState.LISTING_VALIDATED:
            job = self._transition(job_id, JobState.READY_FOR_PRODUCTION)
        if job.state is JobState.READY_FOR_PRODUCTION:
            self._prepare_product_draft(job_id)
            job = self.store.get_job(job_id)
        if job.state is JobState.PRINTIFY_DRAFT_CREATED:
            return self._transition(job_id, JobState.AWAITING_APPROVAL)
        raise InvalidStateError("Job is not ready for listing preparation")

    def _prepare_intelligence_checkpoints(self, job_id: str) -> None:
        job = self.store.get_job(job_id)
        artwork = self.store.get_artwork(job_id)
        content = self.artifacts.get_artwork(
            object_key=job.artwork_object_key,
            expected_sha256=artwork.content_sha256,
        )
        profile = self.profiles.get(self.store.get_profile_id(job_id))
        try:
            analysis = self.store.get_analysis_checkpoint(job_id)
            if analysis is None:
                analysis = ArtworkAnalysis.model_validate(
                    self.intelligence.inspect_artwork(artwork, content)
                )
                self.store.save_analysis_checkpoint(job_id, analysis)
            listing = self.store.get_listing_checkpoint(job_id)
            if listing is None:
                listing = ListingIntelligence.model_validate(
                    self.intelligence.draft_listing(artwork, content, analysis)
                )
                self.store.save_listing_checkpoint(job_id, listing)
        except IntelligenceUnavailableError:
            self._transition(job_id, JobState.FAILED_RETRYABLE)
            self._event(job_id, "intelligence_temporarily_unavailable", {})
            raise
        except IntelligenceConfigurationError:
            self._transition(job_id, JobState.FAILED_TERMINAL)
            self._event(job_id, "intelligence_configuration_rejected", {})
            raise
        except InvalidGeneratedOutputError:
            self._transition(job_id, JobState.FAILED_TERMINAL)
            self._event(job_id, "generated_output_rejected", {})
            raise
        except ValidationError as error:
            self._transition(job_id, JobState.FAILED_TERMINAL)
            self._event(job_id, "generated_output_rejected", {})
            raise InvalidGeneratedOutputError(
                "Intelligence adapter returned output outside the application contract"
            ) from error
        review = ReviewSnapshot(
            review_version=1,
            artwork_analysis=analysis,
            listing=listing,
            profile=profile,
            validation=validate_listing(listing),
        )
        self._transition(
            job_id,
            JobState.LISTING_DRAFTED,
            review=review,
            review_version=1,
        )

    def _prepare_product_draft(self, job_id: str) -> None:
        review = self.store.get_review(job_id)
        artwork = self.store.get_artwork(job_id)
        job = self.store.get_job(job_id)
        content = self.artifacts.get_artwork(
            object_key=job.artwork_object_key,
            expected_sha256=artwork.content_sha256,
        )

        upload_key = f"upload:{job_id}:{artwork.content_sha256}"
        upload_claim, upload_created = self._claim_write(
            job_id,
            operation="upload_artwork",
            idempotency_key=upload_key,
            request_material=f"{artwork.content_sha256}:{artwork.content_type}",
        )
        if upload_claim.status is ExternalWriteStatus.COMPLETED:
            assert upload_claim.result is not None
            image_id = upload_claim.result["external_id"]
        elif not upload_created:
            raise ExternalWritePendingError(
                "Artwork upload is already claimed and requires reconciliation"
            )
        else:
            try:
                image_id = self.production.upload_artwork(
                    job_id=job_id,
                    artwork=artwork,
                    content=content,
                )
            except Exception:
                self.store.require_external_write_reconciliation(
                    job_id,
                    idempotency_key=upload_key,
                    request_fingerprint=upload_claim.request_fingerprint,
                )
                raise
            self.store.complete_external_write(
                job_id,
                idempotency_key=upload_key,
                request_fingerprint=upload_claim.request_fingerprint,
                result={"external_id": image_id},
                completed_at=self._clock(),
            )

        draft_request_material = (
            f"{artwork.content_sha256}:{review.profile.profile_id}:"
            f"{review.profile.profile_version}:{review.review_version}:{image_id}"
        )
        draft_key = f"draft:{job_id}:{review.review_version}"
        claim, created = self._claim_write(
            job_id,
            operation="create_product_draft",
            idempotency_key=draft_key,
            request_material=draft_request_material,
        )
        if claim.status is ExternalWriteStatus.COMPLETED:
            assert claim.result is not None
            result = claim.result
        elif not created:
            raise ExternalWritePendingError(
                "Product draft write is already claimed and requires reconciliation"
            )
        else:
            try:
                product_id = self.production.create_product_draft(
                    job_id=job_id,
                    artwork=artwork,
                    listing=review.listing,
                    profile=review.profile,
                    image_id=image_id,
                )
            except Exception:
                self.store.require_external_write_reconciliation(
                    job_id,
                    idempotency_key=draft_key,
                    request_fingerprint=claim.request_fingerprint,
                )
                raise
            result = {"external_id": product_id}
            self.store.complete_external_write(
                job_id,
                idempotency_key=draft_key,
                request_fingerprint=claim.request_fingerprint,
                result=result,
                completed_at=self._clock(),
            )
        product_id = result["external_id"]
        review = review.model_copy(update={"printify_product_id": product_id})
        self._transition(
            job_id,
            JobState.PRINTIFY_DRAFT_CREATED,
            review=review,
            printify_image_id=image_id,
            printify_product_id=product_id,
        )

    def get_job(self, job_id: str) -> JobRecord:
        return self.store.get_job(job_id)

    def get_review(self, job_id: str) -> ReviewSnapshot:
        return self.store.get_review(job_id)

    def revise_listing(self, job_id: str, revision: ListingRevisionRequest) -> ReviewSnapshot:
        job = self.store.get_job(job_id)
        if job.state not in {
            JobState.AWAITING_APPROVAL,
            JobState.APPROVED,
            JobState.NEEDS_REVISION,
        }:
            raise InvalidStateError("Listing can only be revised from review or approved state")

        current = self.store.get_review(job_id)
        listing = ListingIntelligence.model_validate(revision.model_dump())
        validation = validate_listing(listing)
        next_version = current.review_version + 1
        review = ReviewSnapshot(
            review_version=next_version,
            artwork_analysis=current.artwork_analysis,
            listing=listing,
            profile=current.profile,
            validation=validation,
            printify_product_id=current.printify_product_id,
            approval_status=ApprovalStatus.INVALIDATED,
        )

        if job.state is not JobState.NEEDS_REVISION:
            self._transition(
                job_id,
                JobState.NEEDS_REVISION,
                approved_review_version=None,
            )
        self._transition(
            job_id,
            JobState.LISTING_DRAFTED,
            review=review,
            review_version=next_version,
        )
        if not validation.passed:
            self._transition(job_id, JobState.NEEDS_REVISION)
            self._event(
                job_id,
                "listing_validation_failed",
                {"issue_codes": [issue.code for issue in validation.issues]},
            )
            return review
        self._transition(job_id, JobState.LISTING_VALIDATED)
        self._transition(job_id, JobState.READY_FOR_PRODUCTION)
        self._prepare_product_draft(job_id)
        self._transition(job_id, JobState.AWAITING_APPROVAL)
        self._event(job_id, "review_revised", {"review_version": next_version})
        return self.store.get_review(job_id)

    def approve(self, job_id: str, review_version: int) -> JobRecord:
        job = self.store.get_job(job_id)
        review = self.store.get_review(job_id)
        if review_version != review.review_version:
            raise StaleApprovalError("Approval does not match the current review version")
        if job.state is JobState.APPROVED and job.approved_review_version == review_version:
            return job
        if job.state is not JobState.AWAITING_APPROVAL:
            raise InvalidStateError("Job is not awaiting approval")
        if not review.validation.passed:
            raise InvalidStateError("An invalid review cannot be approved")

        approved_review = review.model_copy(update={"approval_status": ApprovalStatus.APPROVED})
        approved = self._transition(
            job_id,
            JobState.APPROVED,
            review=approved_review,
            approved_review_version=review_version,
        )
        self._event(job_id, "review_approved", {"review_version": review_version})
        return self.store.get_job(approved.job_id)

    def register_approval_wait(
        self,
        job_id: str,
        *,
        review_version: int,
        task_token: str,
        expires_at: datetime,
    ) -> ApprovalWaitRecord:
        now = self._clock()
        return self.store.register_approval_wait(
            ApprovalWaitRecord(
                job_id=job_id,
                review_version=review_version,
                task_token=task_token,
                status=ApprovalWaitStatus.PENDING,
                created_at=now,
                expires_at=expires_at,
            )
        )

    def approve_waiting_job(self, job_id: str, review_version: int) -> tuple[JobRecord, str]:
        """Approve one version and consume its callback token in the same store transaction."""

        job = self.store.get_job(job_id)
        review = self.store.get_review(job_id)
        wait = self.store.get_approval_wait(job_id)
        if wait is None or wait.review_version != review_version:
            raise StaleApprovalError("Approval wait does not match the requested review version")
        now = self._clock()
        if now >= wait.expires_at:
            raise ApprovalWaitExpiredError("Approval wait has expired")
        if wait.status is ApprovalWaitStatus.CONSUMED:
            if job.state is JobState.APPROVED and job.approved_review_version == review_version:
                return job, wait.task_token
            raise InvalidStateError("Consumed approval wait does not match an approved job")
        if review_version != review.review_version:
            raise StaleApprovalError("Approval does not match the current review version")
        if job.state is not JobState.AWAITING_APPROVAL:
            raise InvalidStateError("Job is not awaiting approval")
        if not review.validation.passed:
            raise InvalidStateError("An invalid review cannot be approved")
        consumed_wait = wait.model_copy(
            update={"status": ApprovalWaitStatus.CONSUMED, "consumed_at": now}
        )
        approved_review = review.model_copy(update={"approval_status": ApprovalStatus.APPROVED})
        approved = self._transition(
            job_id,
            JobState.APPROVED,
            review=approved_review,
            approval_wait=(wait, consumed_wait),
            approved_review_version=review_version,
        )
        self._event(job_id, "review_approved", {"review_version": review_version})
        return self.store.get_job(approved.job_id), wait.task_token

    def publish(self, job_id: str) -> JobRecord:
        published = self.publish_draft(job_id)
        if published.state is JobState.VERIFIED:
            return published
        return self.verify_publication(job_id)

    def publish_draft(self, job_id: str) -> JobRecord:
        """Resume fake publication through the durable PUBLISHED checkpoint."""

        job = self.store.get_job(job_id)
        if job.state is JobState.VERIFIED:
            return job
        if job.state not in {JobState.APPROVED, JobState.PUBLISHING, JobState.PUBLISHED}:
            raise InvalidStateError("Job must be approved before publication")
        if job.approved_review_version != job.review_version:
            raise StaleApprovalError("Publication approval is stale")
        if not job.printify_product_id:
            raise InvalidStateError("Job has no prepared product draft")
        review = self.store.get_review(job_id)
        if not review.profile.publish_enabled:
            raise InvalidStateError("Product profile does not permit publication")

        if job.state is JobState.APPROVED:
            job = self._transition(job_id, JobState.PUBLISHING)
        if job.state is JobState.PUBLISHING:
            idempotency_key = f"publish:{job_id}:{job.review_version}"
            claim, created = self._claim_write(
                job_id,
                operation="publish_listing",
                idempotency_key=idempotency_key,
                request_material=(
                    f"{job.printify_product_id}:{job.review_version}:{job.approved_review_version}"
                ),
            )
            if claim.status is ExternalWriteStatus.COMPLETED:
                assert claim.result is not None
                listing_id = claim.result["external_id"]
            elif not created:
                raise ExternalWritePendingError(
                    "Publication write is already claimed and requires reconciliation"
                )
            else:
                try:
                    listing_id = self.production.publish(
                        job_id=job_id,
                        product_id=job.printify_product_id,
                    )
                except Exception:
                    self.store.require_external_write_reconciliation(
                        job_id,
                        idempotency_key=idempotency_key,
                        request_fingerprint=claim.request_fingerprint,
                    )
                    raise
                self.store.complete_external_write(
                    job_id,
                    idempotency_key=idempotency_key,
                    request_fingerprint=claim.request_fingerprint,
                    result={"external_id": listing_id},
                    completed_at=self._clock(),
                )
            job = self._transition(
                job_id,
                JobState.PUBLISHED,
                published_listing_id=listing_id,
            )
        return self.store.get_job(job.job_id)

    def verify_publication(self, job_id: str) -> JobRecord:
        """Verify the persisted fake publication in a separately retryable command."""

        job = self.store.get_job(job_id)
        if job.state is JobState.VERIFIED:
            return job
        if job.state is not JobState.PUBLISHED or not job.published_listing_id:
            raise InvalidStateError("Job must have a published listing before verification")
        verified = self._transition(job_id, JobState.VERIFIED)
        self._event(
            job_id,
            "publication_verified",
            {"listing_id": verified.published_listing_id},
        )
        return self.store.get_job(verified.job_id)

    def get_report(self, job_id: str) -> RunReport:
        return RunReport(
            job=self.store.get_job(job_id),
            artwork=self.store.get_artwork(job_id),
            review=self.store.get_review(job_id),
            external_writes=self.store.list_external_writes(job_id),
            events=self.store.list_events(job_id),
        )

    def _transition(
        self,
        job_id: str,
        target: JobState,
        *,
        review: ReviewSnapshot | None = None,
        approval_wait: tuple[ApprovalWaitRecord, ApprovalWaitRecord] | None = None,
        **updates: object,
    ) -> JobRecord:
        current = self.store.get_job(job_id)
        if not can_transition(current.state, target):
            raise InvalidStateError(f"Cannot transition from {current.state} to {target}")
        payload = current.model_dump()
        payload.update(updates)
        payload["state"] = target
        payload["record_version"] = current.record_version + 1
        payload["event_sequence"] = current.event_sequence + 1
        payload["updated_at"] = self._clock()
        updated = JobRecord.model_validate(payload)
        event = WorkflowEvent(
            sequence=updated.event_sequence,
            occurred_at=self._clock(),
            name="state_changed",
            details={"from": current.state, "to": target},
        )
        self.store.commit_transition(
            current=current,
            updated=updated,
            event=event,
            review=review,
            approval_wait=approval_wait,
        )
        return updated

    def _event(self, job_id: str, name: str, details: dict[str, object]) -> None:
        self.store.append_event(
            job_id,
            occurred_at=self._clock(),
            name=name,
            details=details,
        )

    def _claim_write(
        self,
        job_id: str,
        *,
        operation: str,
        idempotency_key: str,
        request_material: str,
    ) -> tuple[ExternalWriteClaim, bool]:
        fingerprint = sha256(request_material.encode()).hexdigest()
        return self.store.claim_external_write(
            job_id,
            ExternalWriteClaim(
                operation=operation,
                idempotency_key=idempotency_key,
                request_fingerprint=fingerprint,
                status=ExternalWriteStatus.CLAIMED,
                claimed_at=self._clock(),
            ),
        )
