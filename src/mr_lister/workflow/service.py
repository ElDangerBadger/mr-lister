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
from mr_lister.workflow.errors import (
    IntelligenceConfigurationError,
    IntelligenceUnavailableError,
    InvalidGeneratedOutputError,
    InvalidStateError,
    StaleApprovalError,
)
from mr_lister.workflow.models import (
    ExternalWriteRecord,
    ListingRevisionRequest,
    RunReport,
    WorkflowEvent,
)
from mr_lister.workflow.ports import IntelligencePort, ProductionPort
from mr_lister.workflow.profiles import ProductProfileRepository
from mr_lister.workflow.store import InMemoryJobStore
from mr_lister.workflow.validation import validate_artwork, validate_listing


class ListingWorkflow:
    def __init__(
        self,
        *,
        store: InMemoryJobStore,
        profiles: ProductProfileRepository,
        intelligence: IntelligencePort,
        production: ProductionPort,
        clock: Callable[[], datetime] | None = None,
        job_id_factory: Callable[[], str] | None = None,
    ) -> None:
        self.store = store
        self.profiles = profiles
        self.intelligence = intelligence
        self.production = production
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
        existing_job_id = self.store.resolve_intake(idempotency_key, request_fingerprint)
        if existing_job_id is not None:
            return self.store.get_job(existing_job_id)

        self.profiles.get(profile_id)
        job_id = self._job_id_factory()
        now = self._clock()
        job = JobRecord(
            job_id=job_id,
            state=JobState.UPLOADED,
            review_version=0,
            idempotency_key=idempotency_key,
            artwork_object_key=f"local/{artwork.content_sha256}.png",
            created_at=now,
            updated_at=now,
        )
        self.store.jobs[job_id] = job
        self.store.artworks[job_id] = artwork
        self.store.artwork_contents[job_id] = content
        self.store.profile_ids[job_id] = profile_id
        self.store.bind_intake(idempotency_key, request_fingerprint, job_id)
        self._event(job_id, "artwork_uploaded", {"filename": artwork.filename})

        self._transition(job_id, JobState.INTAKE_VALIDATED)
        return self.store.get_job(job_id)

    def prepare(self, job_id: str) -> JobRecord:
        """Create a validated, reviewable draft for one previously accepted intake."""

        job = self.store.get_job(job_id)
        if job.state is not JobState.INTAKE_VALIDATED:
            if job_id in self.store.reviews:
                return job
            raise InvalidStateError("Job is not ready for listing preparation")
        artwork = self.store.artworks[job_id]
        content = self.store.artwork_contents[job_id]
        profile = self.profiles.get(self.store.profile_ids[job_id])
        self._transition(job_id, JobState.ANALYZING_ARTWORK)
        try:
            analysis = ArtworkAnalysis.model_validate(
                self.intelligence.inspect_artwork(artwork, content)
            )
            listing = ListingIntelligence.model_validate(
                self.intelligence.draft_listing(artwork, content, analysis)
            )
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
        self._transition(job_id, JobState.LISTING_DRAFTED, review_version=1)

        validation = validate_listing(listing)
        review = ReviewSnapshot(
            review_version=1,
            artwork_analysis=analysis,
            listing=listing,
            profile=profile,
            validation=validation,
        )
        self.store.reviews[job_id] = review
        if not validation.passed:
            self._transition(job_id, JobState.NEEDS_REVISION)
            self._event(
                job_id,
                "listing_validation_failed",
                {"issue_codes": [issue.code for issue in validation.issues]},
            )
            return self.store.get_job(job_id)
        self._transition(job_id, JobState.LISTING_VALIDATED)
        self._transition(job_id, JobState.READY_FOR_PRODUCTION)

        image_id, product_id = self.production.create_draft(
            job_id=job_id,
            artwork=artwork,
            listing=listing,
            profile=profile,
        )
        self._record_write(
            job_id,
            operation="sync_product_draft",
            idempotency_key=f"draft:{job_id}:1",
            request_material=(
                f"{artwork.content_sha256}:{profile.profile_id}:{profile.profile_version}:1"
            ),
            external_id=product_id,
        )
        self._transition(
            job_id,
            JobState.PRINTIFY_DRAFT_CREATED,
            printify_image_id=image_id,
            printify_product_id=product_id,
        )
        self.store.reviews[job_id] = review.model_copy(update={"printify_product_id": product_id})
        return self._transition(job_id, JobState.AWAITING_APPROVAL)

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
        self.store.reviews[job_id] = review
        self._transition(job_id, JobState.LISTING_DRAFTED, review_version=next_version)
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
        artwork = self.store.artworks[job_id]
        image_id, product_id = self.production.create_draft(
            job_id=job_id,
            artwork=artwork,
            listing=listing,
            profile=current.profile,
        )
        self._record_write(
            job_id,
            operation="sync_product_draft",
            idempotency_key=f"draft:{job_id}:{next_version}",
            request_material=(
                f"{artwork.content_sha256}:{current.profile.profile_id}:"
                f"{current.profile.profile_version}:{next_version}"
            ),
            external_id=product_id,
        )
        self._transition(
            job_id,
            JobState.PRINTIFY_DRAFT_CREATED,
            printify_image_id=image_id,
            printify_product_id=product_id,
        )
        self._transition(job_id, JobState.AWAITING_APPROVAL)
        self._event(job_id, "review_revised", {"review_version": next_version})
        return review

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

        self.store.reviews[job_id] = review.model_copy(
            update={"approval_status": ApprovalStatus.APPROVED}
        )
        approved = self._transition(
            job_id,
            JobState.APPROVED,
            approved_review_version=review_version,
        )
        self._event(job_id, "review_approved", {"review_version": review_version})
        return approved

    def publish(self, job_id: str) -> JobRecord:
        job = self.store.get_job(job_id)
        if job.state is JobState.VERIFIED:
            return job
        if job.state is not JobState.APPROVED:
            raise InvalidStateError("Job must be approved before publication")
        if job.approved_review_version != job.review_version:
            raise StaleApprovalError("Publication approval is stale")
        if not job.printify_product_id:
            raise InvalidStateError("Job has no prepared product draft")
        review = self.store.get_review(job_id)
        if not review.profile.publish_enabled:
            raise InvalidStateError("Product profile does not permit publication")

        self._transition(job_id, JobState.PUBLISHING)
        listing_id = self.production.publish(
            job_id=job_id,
            product_id=job.printify_product_id,
        )
        self._record_write(
            job_id,
            operation="publish_listing",
            idempotency_key=f"publish:{job_id}:{job.review_version}",
            request_material=(
                f"{job.printify_product_id}:{job.review_version}:{job.approved_review_version}"
            ),
            external_id=listing_id,
        )
        self._transition(job_id, JobState.PUBLISHED, published_listing_id=listing_id)
        verified = self._transition(job_id, JobState.VERIFIED)
        self._event(job_id, "publication_verified", {"listing_id": listing_id})
        return verified

    def get_report(self, job_id: str) -> RunReport:
        return RunReport(
            job=self.store.get_job(job_id),
            artwork=self.store.artworks[job_id],
            review=self.store.get_review(job_id),
            external_writes=tuple(self.store.external_writes[job_id]),
            events=tuple(self.store.events[job_id]),
        )

    def _transition(self, job_id: str, target: JobState, **updates: object) -> JobRecord:
        current = self.store.get_job(job_id)
        if not can_transition(current.state, target):
            raise InvalidStateError(f"Cannot transition from {current.state} to {target}")
        payload = current.model_dump()
        payload.update(updates)
        payload["state"] = target
        payload["updated_at"] = self._clock()
        updated = JobRecord.model_validate(payload)
        self.store.jobs[job_id] = updated
        self._event(job_id, "state_changed", {"from": current.state, "to": target})
        return updated

    def _event(self, job_id: str, name: str, details: dict[str, object]) -> None:
        events = self.store.events[job_id]
        events.append(
            WorkflowEvent(
                sequence=len(events) + 1,
                occurred_at=self._clock(),
                name=name,
                details=details,
            )
        )

    def _record_write(
        self,
        job_id: str,
        *,
        operation: str,
        idempotency_key: str,
        request_material: str,
        external_id: str,
    ) -> None:
        fingerprint = sha256(request_material.encode()).hexdigest()
        records = self.store.external_writes[job_id]
        for record in records:
            if record.idempotency_key == idempotency_key:
                if record.request_fingerprint != fingerprint:
                    raise InvalidStateError("External write idempotency fingerprint changed")
                return
        records.append(
            ExternalWriteRecord(
                operation=operation,
                idempotency_key=idempotency_key,
                request_fingerprint=fingerprint,
                external_id=external_id,
                occurred_at=self._clock(),
            )
        )
