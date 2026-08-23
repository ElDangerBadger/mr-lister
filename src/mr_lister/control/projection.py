"""Owner-scoped, read-only Phase 6 consolidated review projection."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol

from pydantic import ValidationError

from mr_lister.contracts import ListingIntelligence, ProductProfile
from mr_lister.control.dispatch import deterministic_execution_name, work_input_fingerprint
from mr_lister.control.errors import ControlError, NotFoundError
from mr_lister.control.fingerprints import (
    canonical_fingerprint,
    product_sync_record_fingerprint,
    review_etag,
)
from mr_lister.control.models import (
    AgentPreparationEvidence,
    ArtworkAnalysisRecord,
    ControlJobRecord,
    ControlJobState,
    FailureRecord,
    PricingEvidenceRecord,
    PricingSnapshot,
    ProductSyncRecord,
    ReviewActor,
    ReviewContent,
    SourceArtifactRecord,
    WorkRequest,
    WorkRequestStatus,
    WorkType,
)
from mr_lister.control.projection_models import (
    ActionReason,
    ArtworkInterpretation,
    ArtworkPreview,
    EconomicsProjection,
    EconomicsReadiness,
    FailureProjection,
    ListingProjection,
    ListingValidationProjection,
    MockupProjection,
    MockupSetProjection,
    PlacementPresentation,
    ProductPolicyProjection,
    ProductSynchronizationProjection,
    PublicValidationIssue,
    ReviewDisplayState,
    ReviewStage,
    SectionReadiness,
    SellerAction,
    SellerActionCapability,
    SellerReviewProjection,
    StrandsProvenanceProjection,
    VariantEconomicsProjection,
)
from mr_lister.control.source_artwork import validate_source_artifact_authority
from mr_lister.review_profile import ExactReviewProductProfile
from mr_lister.review_security import is_safe_mockup_url, is_safe_preview_url
from mr_lister.workflow.validation import (
    find_repeated_tag_keyword_locations,
    validate_listing,
)

MAX_PREVIEW_TTL = timedelta(minutes=5)


class ReviewProjectionUnavailableError(ControlError):
    """A joined authority record was absent or internally inconsistent."""

    code = "PROJECTION_UNAVAILABLE"


class ReviewProjectionStore(Protocol):
    def get_job_for_owner(self, owner_id: str, job_id: str) -> ControlJobRecord: ...

    def get_source_artifact(self, job_id: str) -> SourceArtifactRecord: ...

    def get_review(self, job_id: str, review_version: int) -> ReviewContent: ...

    def get_artwork_analysis(self, job_id: str, analysis_id: str) -> ArtworkAnalysisRecord: ...

    def get_agent_evidence(self, job_id: str, evidence_id: str) -> AgentPreparationEvidence: ...

    def get_product_sync(self, job_id: str, sync_id: str) -> ProductSyncRecord: ...

    def get_pricing(self, job_id: str, snapshot_id: str) -> PricingSnapshot: ...

    def get_pricing_evidence(self, job_id: str, snapshot_id: str) -> PricingEvidenceRecord: ...

    def get_failure(self, job_id: str, failure_id: str) -> FailureRecord: ...

    def get_work_request(self, job_id: str, work_request_id: str) -> WorkRequest: ...


class ReviewProductAuthority(Protocol):
    def get_exact(self, *, profile_id: str, profile_version: int) -> ExactReviewProductProfile: ...


@dataclass(frozen=True)
class PreviewGrant:
    url: str
    expires_at: datetime
    source_artifact_fingerprint: str


class ArtworkPreviewIssuer(Protocol):
    def issue(self, *, source: SourceArtifactRecord) -> PreviewGrant: ...


_DISPLAY_BY_STATE: dict[ControlJobState, tuple[ReviewDisplayState, ReviewStage]] = {
    ControlJobState.INTAKE_VALIDATED: (
        ReviewDisplayState.PREPARING,
        ReviewStage.UPLOAD_VERIFIED,
    ),
    ControlJobState.ANALYZING_ARTWORK: (
        ReviewDisplayState.PREPARING,
        ReviewStage.ARTWORK_REVIEW,
    ),
    ControlJobState.LISTING_DRAFTED: (
        ReviewDisplayState.PREPARING,
        ReviewStage.LISTING_VALIDATION,
    ),
    ControlJobState.NEEDS_REVISION: (
        ReviewDisplayState.NEEDS_REVISION,
        ReviewStage.SELLER_REVISION,
    ),
    ControlJobState.PRODUCT_DRAFT_SYNCING: (
        ReviewDisplayState.SYNCHRONIZING,
        ReviewStage.PRODUCT_SYNC,
    ),
    ControlJobState.AWAITING_APPROVAL: (
        ReviewDisplayState.READY,
        ReviewStage.HUMAN_REVIEW,
    ),
    ControlJobState.PRICING_REFRESHING: (
        ReviewDisplayState.REFRESHING_ESTIMATE,
        ReviewStage.ECONOMICS_REFRESH,
    ),
    ControlJobState.RECONCILIATION_REQUIRED: (
        ReviewDisplayState.RECONCILING,
        ReviewStage.PROVIDER_RECONCILIATION,
    ),
    ControlJobState.FAILED_RETRYABLE: (
        ReviewDisplayState.RETRYABLE_FAILURE,
        ReviewStage.RECOVERY,
    ),
    ControlJobState.FAILED_TERMINAL: (
        ReviewDisplayState.TERMINAL_FAILURE,
        ReviewStage.COMPLETE,
    ),
    ControlJobState.CANCEL_REQUESTED: (
        ReviewDisplayState.CANCELLING,
        ReviewStage.CANCELLATION,
    ),
    ControlJobState.CANCELLED: (
        ReviewDisplayState.CANCELLED,
        ReviewStage.COMPLETE,
    ),
    ControlJobState.APPROVED: (
        ReviewDisplayState.APPROVED,
        ReviewStage.COMPLETE,
    ),
}

_ACTION_MESSAGES: dict[ActionReason, str] = {
    ActionReason.AVAILABLE: "Available for the current review.",
    ActionReason.NOT_IN_CURRENT_STATE: "This action is not available at the current stage.",
    ActionReason.REVIEW_NOT_READY: "The listing review is not ready yet.",
    ActionReason.REVIEW_INVALID: "Resolve the blocking listing issues first.",
    ActionReason.PRODUCT_NOT_CURRENT: (
        "The current review has not finished product synchronization."
    ),
    ActionReason.PRODUCT_NOT_REVIEWABLE: "The staged product is not an editable draft.",
    ActionReason.MOCKUPS_NOT_READY: "Representative product mockups are not ready.",
    ActionReason.ECONOMICS_MISSING: "Estimated proceeds must be calculated first.",
    ActionReason.ECONOMICS_STALE: "Estimated proceeds must be refreshed first.",
    ActionReason.PROVIDER_OUTCOME_UNCONFIRMED: "A provider write is still being reconciled.",
    ActionReason.CANCELLATION_PENDING: "Cancellation is already in progress.",
    ActionReason.RETRY_NOT_AVAILABLE: "This failure has no retryable recovery.",
}

_FAILURE_MESSAGES: dict[str, str] = {
    "INTELLIGENCE_UNAVAILABLE": "Listing intelligence is temporarily unavailable.",
    "INTELLIGENCE_CONFIGURATION": "Listing intelligence needs configuration.",
    "INVALID_GENERATED_OUTPUT": "The generated listing did not pass its contract.",
    "ARTIFACT_INTEGRITY": "The uploaded artwork could not be verified.",
    "PRODUCTION_UNAVAILABLE": "Product staging is temporarily unavailable.",
    "PRODUCTION_CONFIGURATION": "Product staging needs configuration.",
    "PRODUCTION_INPUT": "The staged product input is not valid.",
    "ECONOMICS_UNAVAILABLE": "Estimated proceeds are temporarily unavailable.",
    "PRODUCT_CREATE_OUTCOME_UNKNOWN": "The product write is being reconciled.",
    "CONNECTION_REVOKED": "The seller connection must be restored.",
}


def _review_content_fingerprint(review: ReviewContent) -> str:
    """Rebuild the exact immutable material used when the review was created."""

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


def _product_sync_fingerprint(sync: ProductSyncRecord) -> str:
    """Rebuild the exact immutable material used when provider evidence was stored."""

    return product_sync_record_fingerprint(sync)


class SellerReviewProjectionService:
    """Join immutable authority into one seller-safe representation without writing state."""

    def __init__(
        self,
        *,
        store: ReviewProjectionStore,
        profiles: ReviewProductAuthority,
        clock: Callable[[], datetime] | None = None,
        preview_issuer: ArtworkPreviewIssuer | None = None,
        preview_origin: str | None = None,
    ) -> None:
        if (preview_issuer is None) != (preview_origin is None):
            raise ValueError("Preview issuer and origin are optional as a pair")
        self._store = store
        self._profiles = profiles
        self._clock = clock or (lambda: datetime.now(UTC))
        self._preview_issuer = preview_issuer
        self._preview_origin = preview_origin

    def get(self, *, owner_id: str, job_id: str) -> SellerReviewProjection:
        """Return the exact owner-scoped review; ownership is always checked first."""

        try:
            job = self._store.get_job_for_owner(owner_id, job_id)
        except ValidationError:
            raise ReviewProjectionUnavailableError(
                "The consolidated review is temporarily unavailable"
            ) from None
        try:
            return self._project(job)
        except (NotFoundError, ValidationError):
            raise ReviewProjectionUnavailableError(
                "The consolidated review is temporarily unavailable"
            ) from None

    def _project(self, job: ControlJobRecord) -> SellerReviewProjection:
        now = self._now()
        source = self._source(job)
        exact_profile = self._profile(source)
        work = self._active_work(job)
        review = self._review(job)
        if (
            review is not None
            and review.product_profile_fingerprint != source.product_profile_fingerprint
        ):
            raise ReviewProjectionUnavailableError(
                "The consolidated review is temporarily unavailable"
            )
        listing, validation = self._listing(review, job)
        analysis = self._analysis(job, source, review)
        failure = self._failure(job)
        strands = self._strands(job, review, failure)
        sync = self._sync(job, review, exact_profile.profile)
        mockups = self._mockups(sync, job, exact_profile.product_name)
        economics, pricing = self._economics(job, review, sync, exact_profile.profile, now)
        display_state, stage = self._display(job, economics)
        authority_etag = self._authority_etag(job, review, sync, pricing)
        if job.state is ControlJobState.APPROVED and job.approval_fingerprint != authority_etag:
            raise ReviewProjectionUnavailableError(
                "The consolidated review is temporarily unavailable"
            )
        actions = self._actions(
            job=job,
            review=review,
            sync=sync,
            mockups=mockups,
            economics=economics,
            failure=failure,
        )
        _ = work
        return SellerReviewProjection(
            job_id=job.job_id,
            record_version=job.record_version,
            review_version=job.review_version,
            review_fingerprint=job.review_fingerprint,
            review_authority_etag=authority_etag,
            display_state=display_state,
            stage=stage,
            actions=actions,
            preview=self._preview(source, now),
            artwork=analysis,
            listing=listing,
            validation=validation,
            product_policy=self._product_policy(exact_profile),
            synchronization=self._synchronization(job, sync),
            mockups=mockups,
            economics=economics,
            strands=strands,
            failure=failure,
            provider_outcome_unconfirmed=(
                job.provider_outcome_unconfirmed or job.upload_outcome_unconfirmed
            ),
            created_at=job.created_at,
            updated_at=job.updated_at,
        )

    def _source(self, job: ControlJobRecord) -> SourceArtifactRecord:
        source = self._store.get_source_artifact(job.job_id)
        try:
            validate_source_artifact_authority(source)
        except ValueError:
            raise ReviewProjectionUnavailableError(
                "The consolidated review is temporarily unavailable"
            ) from None
        if (
            source.job_id != job.job_id
            or source.owner_id != job.owner_id
            or source.fingerprint != job.source_artifact_fingerprint
        ):
            raise ReviewProjectionUnavailableError(
                "The consolidated review is temporarily unavailable"
            )
        return source

    def _profile(self, source: SourceArtifactRecord) -> ExactReviewProductProfile:
        try:
            exact = self._profiles.get_exact(
                profile_id=source.product_profile_id,
                profile_version=source.product_profile_version,
            )
        except Exception:
            raise ReviewProjectionUnavailableError(
                "The consolidated review is temporarily unavailable"
            ) from None
        if (
            exact.profile.profile_id != source.product_profile_id
            or exact.profile.profile_version != source.product_profile_version
            or exact.fingerprint != source.product_profile_fingerprint
            or canonical_fingerprint(exact.profile) != exact.fingerprint
            or not exact.product_name.strip()
            or not exact.provider_name.strip()
        ):
            raise ReviewProjectionUnavailableError(
                "The consolidated review is temporarily unavailable"
            )
        return exact

    def _active_work(self, job: ControlJobRecord) -> WorkRequest | None:
        if job.active_work_request_id is None:
            return None
        work = self._store.get_work_request(job.job_id, job.active_work_request_id)
        expected_type = {
            ControlJobState.INTAKE_VALIDATED: WorkType.PREPARE,
            ControlJobState.ANALYZING_ARTWORK: WorkType.PREPARE,
            ControlJobState.LISTING_DRAFTED: WorkType.PREPARE,
            ControlJobState.PRODUCT_DRAFT_SYNCING: WorkType.SYNCHRONIZE_PRODUCT,
            ControlJobState.RECONCILIATION_REQUIRED: WorkType.RECONCILE_PRODUCT,
            ControlJobState.PRICING_REFRESHING: WorkType.REFRESH_ECONOMICS,
        }.get(job.state)
        if (
            work.owner_id != job.owner_id
            or work.job_id != job.job_id
            or work.work_request_id != job.active_work_request_id
            or work.status in {WorkRequestStatus.COMPLETED, WorkRequestStatus.CANCELLED}
            or work.execution_name != deterministic_execution_name(work.work_request_id)
            or work.input_fingerprint
            != work_input_fingerprint(
                work_type=work.work_type,
                job_id=work.job_id,
                work_request_id=work.work_request_id,
            )
            or (
                work.work_type is WorkType.PREPARE
                and (
                    (job.review_version == 0 and work.review_version is not None)
                    or (
                        job.review_version > 0
                        and work.review_version not in {None, job.review_version}
                    )
                )
            )
            or (
                work.work_type is not WorkType.PREPARE and work.review_version != job.review_version
            )
            or (
                job.state is not ControlJobState.CANCEL_REQUESTED
                and work.work_type is not expected_type
            )
        ):
            raise ReviewProjectionUnavailableError(
                "The consolidated review is temporarily unavailable"
            )
        return work

    def _review(self, job: ControlJobRecord) -> ReviewContent | None:
        if job.review_version == 0:
            return None
        review = self._store.get_review(job.job_id, job.review_version)
        if (
            review.job_id != job.job_id
            or review.review_version != job.review_version
            or review.fingerprint != job.review_fingerprint
            or review.fingerprint != _review_content_fingerprint(review)
        ):
            raise ReviewProjectionUnavailableError(
                "The consolidated review is temporarily unavailable"
            )
        return review

    @staticmethod
    def _listing(
        review: ReviewContent | None, job: ControlJobRecord
    ) -> tuple[ListingProjection, ListingValidationProjection]:
        if review is None:
            pending = SectionReadiness.PENDING
            return ListingProjection(readiness=pending), ListingValidationProjection(
                readiness=pending
            )
        try:
            candidate = ListingIntelligence(
                title=review.title,
                description=review.description,
                tags=review.tags,
                audience=review.audience,
                title_rationale=review.title_rationale,
                tag_rationale=review.tag_rationale,
            )
        except ValidationError:
            raise ReviewProjectionUnavailableError(
                "The consolidated review is temporarily unavailable"
            ) from None
        result = validate_listing(candidate)
        blocking = tuple(issue.code for issue in result.issues if issue.severity.value == "error")
        if (
            result.passed != review.validation_passed
            or result.passed != job.review_validated
            or blocking != review.validation_issue_codes
        ):
            raise ReviewProjectionUnavailableError(
                "The consolidated review is temporarily unavailable"
            )
        issues: list[PublicValidationIssue] = []
        repeated_locations = find_repeated_tag_keyword_locations(candidate.tags)
        repeated_positions = sorted(
            {position for positions in repeated_locations.values() for position in positions}
        )
        for issue in result.issues:
            paths = (
                tuple(f"tags[{position}]" for position in repeated_positions)
                if issue.code == "TAG_KEYWORD_REPETITION"
                else (issue.field or "$",)
            )
            issues.extend(
                PublicValidationIssue(
                    code=issue.code,
                    path=path,
                    severity=issue.severity.value,
                    message=issue.message,
                )
                for path in paths
            )
        return (
            ListingProjection(
                readiness=SectionReadiness.READY,
                title=review.title,
                description=review.description,
                tags=review.tags,
                audience=review.audience,
            ),
            ListingValidationProjection(
                readiness=SectionReadiness.READY,
                passed=result.passed,
                issues=tuple(issues),
            ),
        )

    def _analysis(
        self,
        job: ControlJobRecord,
        source: SourceArtifactRecord,
        review: ReviewContent | None,
    ) -> ArtworkInterpretation:
        if job.artwork_analysis_id is None:
            if review is not None:
                raise ReviewProjectionUnavailableError(
                    "The consolidated review is temporarily unavailable"
                )
            return ArtworkInterpretation(readiness=SectionReadiness.PENDING)
        record = self._store.get_artwork_analysis(job.job_id, job.artwork_analysis_id)
        if (
            record.job_id != job.job_id
            or record.analysis_id != job.artwork_analysis_id
            or record.fingerprint != job.artwork_analysis_fingerprint
            or record.source_artifact_fingerprint != source.fingerprint
            or record.fingerprint
            != canonical_fingerprint(
                {
                    "job_id": record.job_id,
                    "source_artifact_fingerprint": record.source_artifact_fingerprint,
                    "analysis": record.analysis.model_dump(mode="json"),
                }
            )
            or (review is not None and review.artwork_analysis_fingerprint != record.fingerprint)
        ):
            raise ReviewProjectionUnavailableError(
                "The consolidated review is temporarily unavailable"
            )
        analysis = record.analysis
        return ArtworkInterpretation(
            readiness=SectionReadiness.READY,
            subject=analysis.subject,
            visual_elements=analysis.visual_elements[:20],
            styles=analysis.styles[:20],
            themes=analysis.themes[:20],
            visible_text=analysis.visible_text[:20],
            safety_notes=analysis.safety_flags[:20],
            confidence=analysis.confidence,
        )

    def _strands(
        self,
        job: ControlJobRecord,
        review: ReviewContent | None,
        failure: FailureProjection | None,
    ) -> StrandsProvenanceProjection:
        if job.agent_evidence_id is None:
            if job.state in {
                ControlJobState.FAILED_TERMINAL,
                ControlJobState.CANCEL_REQUESTED,
                ControlJobState.CANCELLED,
            }:
                return StrandsProvenanceProjection(readiness=SectionReadiness.UNAVAILABLE)
            if job.state not in {
                ControlJobState.INTAKE_VALIDATED,
                ControlJobState.ANALYZING_ARTWORK,
                ControlJobState.LISTING_DRAFTED,
                ControlJobState.FAILED_RETRYABLE,
            }:
                raise ReviewProjectionUnavailableError(
                    "The consolidated review is temporarily unavailable"
                )
            if job.state is ControlJobState.FAILED_RETRYABLE and (
                failure is None
                or failure.stage
                not in {
                    ReviewStage.UPLOAD_VERIFIED,
                    ReviewStage.ARTWORK_REVIEW,
                    ReviewStage.LISTING_VALIDATION,
                }
            ):
                raise ReviewProjectionUnavailableError(
                    "The consolidated review is temporarily unavailable"
                )
            return StrandsProvenanceProjection(readiness=SectionReadiness.PENDING)
        evidence = self._store.get_agent_evidence(job.job_id, job.agent_evidence_id)
        analysis = (
            None
            if job.artwork_analysis_id is None
            else self._store.get_artwork_analysis(job.job_id, job.artwork_analysis_id)
        )
        if (
            review is None
            or analysis is None
            or evidence.job_id != job.job_id
            or evidence.evidence_id != job.agent_evidence_id
            or evidence.fingerprint != job.agent_evidence_fingerprint
            or evidence.fingerprint != evidence.authority_fingerprint
            or evidence.review_version > review.review_version
            or evidence.work_request_id != analysis.work_request_id
            or (
                review.actor is ReviewActor.MODEL
                and evidence.review_version != review.review_version
            )
            or (
                review.actor is ReviewActor.SELLER
                and evidence.review_version >= review.review_version
            )
            or evidence.tool_calls != ("record_prepared_review",)
        ):
            raise ReviewProjectionUnavailableError(
                "The consolidated review is temporarily unavailable"
            )
        return StrandsProvenanceProjection(
            readiness=SectionReadiness.READY,
            framework=evidence.framework,
            agent_id=evidence.agent_id,
            prepared_review_version=evidence.review_version,
            correlation_id=evidence.correlation_id,
            tool_calls=evidence.tool_calls,
            completed_at=evidence.created_at,
        )

    def _sync(
        self,
        job: ControlJobRecord,
        review: ReviewContent | None,
        profile: ProductProfile,
    ) -> ProductSyncRecord | None:
        if job.product_sync_id is None:
            if job.state in {ControlJobState.AWAITING_APPROVAL, ControlJobState.APPROVED}:
                raise ReviewProjectionUnavailableError(
                    "The consolidated review is temporarily unavailable"
                )
            return None
        sync = self._store.get_product_sync(job.job_id, job.product_sync_id)
        if (
            review is None
            or sync.job_id != job.job_id
            or sync.sync_id != job.product_sync_id
            or sync.product_id != job.product_id
            or sync.payload_fingerprint != job.provider_payload_fingerprint
            or sync.review_version != job.synchronized_review_version
            or sync.fingerprint != job.product_sync_fingerprint
            or sync.fingerprint != _product_sync_fingerprint(sync)
            or sync.review_version > review.review_version
        ):
            raise ReviewProjectionUnavailableError(
                "The consolidated review is temporarily unavailable"
            )
        self._validate_sync_policy(sync, profile)
        if (
            job.state
            in {
                ControlJobState.AWAITING_APPROVAL,
                ControlJobState.PRICING_REFRESHING,
                ControlJobState.APPROVED,
            }
            and sync.review_version != review.review_version
        ):
            raise ReviewProjectionUnavailableError(
                "The consolidated review is temporarily unavailable"
            )
        return sync

    @staticmethod
    def _validate_sync_policy(sync: ProductSyncRecord, profile: ProductProfile) -> None:
        group_by_size = {
            size: group.group_id for group in profile.placement_groups for size in group.sizes
        }
        expected_pairs = {(color, size) for color in profile.colors for size in profile.sizes}
        observed_pairs = {(variant.color, variant.size) for variant in sync.variants}
        if (
            observed_pairs != expected_pairs
            or len(sync.variants) != len(expected_pairs)
            or any(
                variant.placement_group_id != group_by_size.get(variant.size)
                or variant.retail_price_cents != profile.retail_price_cents
                for variant in sync.variants
            )
        ):
            raise ReviewProjectionUnavailableError(
                "The consolidated review is temporarily unavailable"
            )

    @staticmethod
    def _mockups(
        sync: ProductSyncRecord | None,
        job: ControlJobRecord,
        product_name: str,
    ) -> MockupSetProjection:
        if sync is None:
            return MockupSetProjection(readiness=SectionReadiness.PENDING)
        if sync.review_version != job.review_version:
            return MockupSetProjection(readiness=SectionReadiness.OUTDATED)
        selected = sync.representative_mockups(limit=5)
        if not selected or any(not is_safe_mockup_url(item.url) for item in sync.mockups):
            return MockupSetProjection(readiness=SectionReadiness.UNAVAILABLE)
        return MockupSetProjection(
            readiness=SectionReadiness.READY,
            items=tuple(
                MockupProjection(
                    url=item.url,
                    alt_text=f"{product_name} {item.position or 'product'} mockup {index}",
                )
                for index, item in enumerate(selected, start=1)
            ),
        )

    def _economics(
        self,
        job: ControlJobRecord,
        review: ReviewContent | None,
        sync: ProductSyncRecord | None,
        profile: ProductProfile,
        now: datetime,
    ) -> tuple[EconomicsProjection, PricingSnapshot | None]:
        if job.pricing_snapshot_id is None:
            readiness = (
                EconomicsReadiness.REFRESHING
                if job.state is ControlJobState.PRICING_REFRESHING
                else EconomicsReadiness.MISSING
            )
            return EconomicsProjection(readiness=readiness), None
        pricing = self._store.get_pricing(job.job_id, job.pricing_snapshot_id)
        evidence = self._store.get_pricing_evidence(job.job_id, job.pricing_snapshot_id)
        if (
            review is None
            or sync is None
            or pricing.job_id != job.job_id
            or pricing.snapshot_id != job.pricing_snapshot_id
            or pricing.fingerprint != job.pricing_snapshot_fingerprint
            or evidence.job_id != job.job_id
            or evidence.snapshot_id != pricing.snapshot_id
            or evidence.review_version != pricing.review_version
            or evidence.product_sync_fingerprint != pricing.product_sync_fingerprint
            or evidence.fingerprint != pricing.fingerprint
            or evidence.estimate.fingerprint != pricing.fingerprint
            or pricing.review_version != review.review_version
            or pricing.product_sync_fingerprint != sync.fingerprint
            or evidence.estimate.blueprint_id != profile.blueprint_id
            or evidence.estimate.print_provider_id != profile.print_provider_id
            or evidence.estimate.fresh_until != pricing.fresh_until
            or evidence.estimate.calculated_at != pricing.created_at
        ):
            raise ReviewProjectionUnavailableError(
                "The consolidated review is temporarily unavailable"
            )
        estimate_by_id = {item.variant_id: item for item in evidence.estimate.variants}
        sync_by_id = {item.variant_id: item for item in sync.variants}
        if set(estimate_by_id) != set(sync_by_id):
            raise ReviewProjectionUnavailableError(
                "The consolidated review is temporarily unavailable"
            )
        rows: list[VariantEconomicsProjection] = []
        for color in profile.colors:
            for size in profile.sizes:
                variants = [
                    item for item in sync.variants if item.color == color and item.size == size
                ]
                if len(variants) != 1:
                    raise ReviewProjectionUnavailableError(
                        "The consolidated review is temporarily unavailable"
                    )
                sync_variant = variants[0]
                estimate = estimate_by_id[sync_variant.variant_id]
                if (
                    estimate.retail_price_cents != sync_variant.retail_price_cents
                    or estimate.production_cost_cents != sync_variant.production_cost_cents
                    or estimate.buyer_shipping_cents != profile.buyer_shipping_cents
                ):
                    raise ReviewProjectionUnavailableError(
                        "The consolidated review is temporarily unavailable"
                    )
                rows.append(
                    VariantEconomicsProjection(
                        color=color,
                        size=size,
                        retail_price_cents=estimate.retail_price_cents,
                        buyer_shipping_cents=estimate.buyer_shipping_cents,
                        production_cost_cents=estimate.production_cost_cents,
                        production_shipping_cents=estimate.production_shipping_cents,
                        marketplace_fees_cents=estimate.total_marketplace_fees_cents,
                        estimated_proceeds_cents=estimate.estimated_proceeds_cents,
                    )
                )
        readiness = (
            EconomicsReadiness.STALE if now >= pricing.fresh_until else EconomicsReadiness.READY
        )
        return (
            EconomicsProjection(
                readiness=readiness,
                minimum_cents=evidence.estimate.proceeds_range.minimum_cents,
                maximum_cents=evidence.estimate.proceeds_range.maximum_cents,
                variants=tuple(rows),
                calculated_at=pricing.created_at,
                fresh_until=pricing.fresh_until,
                production_cost_source="Connected production product readback",
                production_cost_observed_at=evidence.estimate.product_cost_observed_at,
                production_shipping_source="Connected production standard US shipping",
                production_shipping_observed_at=evidence.estimate.shipping_observed_at,
                fee_policy_source="Etsy US standard fee policy",
                fee_policy_id=evidence.estimate.policy.policy_id,
                fee_policy_verified_on=evidence.estimate.policy.verified_on,
                assumptions=(
                    "USD integer-cent estimate for a US seller and US buyer destination.",
                    "Buyer shipping is zero; seller-funded shipping is deducted.",
                    (
                        "Marketplace fees are estimates and exclude taxes, ads, refunds, "
                        "and currency conversion."
                    ),
                ),
            ),
            pricing,
        )

    def _failure(self, job: ControlJobRecord) -> FailureProjection | None:
        if job.failure_id is None:
            if job.state in {
                ControlJobState.FAILED_RETRYABLE,
                ControlJobState.FAILED_TERMINAL,
            }:
                raise ReviewProjectionUnavailableError(
                    "The consolidated review is temporarily unavailable"
                )
            return None
        if job.state not in {
            ControlJobState.FAILED_RETRYABLE,
            ControlJobState.FAILED_TERMINAL,
        }:
            raise ReviewProjectionUnavailableError(
                "The consolidated review is temporarily unavailable"
            )
        failure = self._store.get_failure(job.job_id, job.failure_id)
        if (
            failure.job_id != job.job_id
            or failure.failure_id != job.failure_id
            or failure.retryable != (job.state is ControlJobState.FAILED_RETRYABLE)
            or not failure.recovery_binding_is_valid
        ):
            raise ReviewProjectionUnavailableError(
                "The consolidated review is temporarily unavailable"
            )
        code = failure.code if failure.code in _FAILURE_MESSAGES else "WORKFLOW_FAILURE"
        message = _FAILURE_MESSAGES.get(failure.code, "The workflow could not be completed.")
        return FailureProjection(
            code=code,
            message=message,
            stage=self._stage_for_failure(failure),
            retryable=failure.retryable,
            recovery=SellerAction.RETRY_JOB if failure.retryable else None,
        )

    @staticmethod
    def _stage_for_failure(failure: FailureRecord) -> ReviewStage:
        return {
            ControlJobState.INTAKE_VALIDATED: ReviewStage.UPLOAD_VERIFIED,
            ControlJobState.ANALYZING_ARTWORK: ReviewStage.ARTWORK_REVIEW,
            ControlJobState.LISTING_DRAFTED: ReviewStage.LISTING_VALIDATION,
            ControlJobState.PRODUCT_DRAFT_SYNCING: ReviewStage.PRODUCT_SYNC,
            ControlJobState.PRICING_REFRESHING: ReviewStage.ECONOMICS_REFRESH,
            ControlJobState.RECONCILIATION_REQUIRED: ReviewStage.PROVIDER_RECONCILIATION,
        }.get(failure.stage, ReviewStage.RECOVERY)

    @staticmethod
    def _display(
        job: ControlJobRecord, economics: EconomicsProjection
    ) -> tuple[ReviewDisplayState, ReviewStage]:
        display, stage = _DISPLAY_BY_STATE[job.state]
        if job.state is ControlJobState.RECONCILIATION_REQUIRED and (
            job.cancellation_requested_at is not None
        ):
            return ReviewDisplayState.CANCELLING, ReviewStage.CANCELLATION
        if job.state is ControlJobState.AWAITING_APPROVAL and economics.readiness in {
            EconomicsReadiness.MISSING,
            EconomicsReadiness.STALE,
        }:
            return ReviewDisplayState.READY, ReviewStage.ECONOMICS_REFRESH
        return display, stage

    @staticmethod
    def _authority_etag(
        job: ControlJobRecord,
        review: ReviewContent | None,
        sync: ProductSyncRecord | None,
        pricing: PricingSnapshot | None,
    ) -> str | None:
        if review is None:
            return None
        return review_etag(
            job_id=job.job_id,
            review_version=review.review_version,
            review_fingerprint=review.fingerprint,
            product_id=job.product_id,
            product_sync_fingerprint=None if sync is None else sync.fingerprint,
            pricing_snapshot_id=None if pricing is None else pricing.snapshot_id,
            pricing_snapshot_fingerprint=None if pricing is None else pricing.fingerprint,
        )

    @staticmethod
    def _actions(
        *,
        job: ControlJobRecord,
        review: ReviewContent | None,
        sync: ProductSyncRecord | None,
        mockups: MockupSetProjection,
        economics: EconomicsProjection,
        failure: FailureProjection | None,
    ) -> tuple[SellerActionCapability, ...]:
        edit_enabled = job.state in {
            ControlJobState.NEEDS_REVISION,
            ControlJobState.AWAITING_APPROVAL,
        }
        cancel_enabled = (
            job.state
            not in {
                ControlJobState.APPROVED,
                ControlJobState.CANCELLED,
                ControlJobState.FAILED_TERMINAL,
                ControlJobState.CANCEL_REQUESTED,
            }
            and job.cancellation_requested_at is None
        )
        retry_enabled = (
            job.state is ControlJobState.FAILED_RETRYABLE
            and failure is not None
            and failure.retryable
        )
        refresh_enabled = (
            job.state is ControlJobState.AWAITING_APPROVAL
            and review is not None
            and review.validation_passed
            and job.review_validated
            and sync is not None
            and sync.review_version == job.review_version
            and economics.readiness in {EconomicsReadiness.MISSING, EconomicsReadiness.STALE}
            and not job.provider_outcome_unconfirmed
            and not job.upload_outcome_unconfirmed
        )
        approve_reason = SellerReviewProjectionService._approval_reason(
            job=job,
            review=review,
            sync=sync,
            mockups=mockups,
            economics=economics,
        )
        reasons = {
            SellerAction.EDIT_LISTING: (
                ActionReason.AVAILABLE if edit_enabled else ActionReason.NOT_IN_CURRENT_STATE
            ),
            SellerAction.APPROVE_REVIEW: approve_reason,
            SellerAction.CANCEL_JOB: (
                ActionReason.AVAILABLE
                if cancel_enabled
                else (
                    ActionReason.CANCELLATION_PENDING
                    if job.cancellation_requested_at is not None
                    else ActionReason.NOT_IN_CURRENT_STATE
                )
            ),
            SellerAction.RETRY_JOB: (
                ActionReason.AVAILABLE if retry_enabled else ActionReason.RETRY_NOT_AVAILABLE
            ),
            SellerAction.REFRESH_ECONOMICS: (
                ActionReason.AVAILABLE if refresh_enabled else ActionReason.NOT_IN_CURRENT_STATE
            ),
        }
        return tuple(
            SellerActionCapability(
                action=action,
                enabled=reasons[action] is ActionReason.AVAILABLE,
                reason=reasons[action],
                message=_ACTION_MESSAGES[reasons[action]],
            )
            for action in SellerAction
        )

    @staticmethod
    def _approval_reason(
        *,
        job: ControlJobRecord,
        review: ReviewContent | None,
        sync: ProductSyncRecord | None,
        mockups: MockupSetProjection,
        economics: EconomicsProjection,
    ) -> ActionReason:
        if job.state is not ControlJobState.AWAITING_APPROVAL:
            return ActionReason.NOT_IN_CURRENT_STATE
        if review is None:
            return ActionReason.REVIEW_NOT_READY
        if not review.validation_passed or not job.review_validated:
            return ActionReason.REVIEW_INVALID
        if (
            sync is None
            or sync.review_version != job.review_version
            or sync.printify_shop_id is None
        ):
            return ActionReason.PRODUCT_NOT_CURRENT
        if sync.provider_locked or sync.provider_published:
            return ActionReason.PRODUCT_NOT_REVIEWABLE
        if mockups.readiness is not SectionReadiness.READY:
            return ActionReason.MOCKUPS_NOT_READY
        if job.provider_outcome_unconfirmed or job.upload_outcome_unconfirmed:
            return ActionReason.PROVIDER_OUTCOME_UNCONFIRMED
        if economics.readiness is EconomicsReadiness.MISSING:
            return ActionReason.ECONOMICS_MISSING
        if economics.readiness is EconomicsReadiness.STALE:
            return ActionReason.ECONOMICS_STALE
        if economics.readiness is not EconomicsReadiness.READY:
            return ActionReason.REVIEW_NOT_READY
        return ActionReason.AVAILABLE

    @staticmethod
    def _product_policy(exact: ExactReviewProductProfile) -> ProductPolicyProjection:
        profile = exact.profile
        return ProductPolicyProjection(
            product_name=exact.product_name,
            provider_name=exact.provider_name,
            colors=profile.colors,
            sizes=profile.sizes,
            placements=tuple(
                PlacementPresentation(
                    group_id=group.group_id,
                    sizes=group.sizes,
                    position=group.position,
                    decoration_method=group.decoration_method,
                    x=group.placement.x,
                    y=group.placement.y,
                    scale=group.placement.scale,
                    angle=group.angle,
                )
                for group in profile.placement_groups
            ),
            retail_price_cents=profile.retail_price_cents,
            buyer_shipping_cents=profile.buyer_shipping_cents,
        )

    @staticmethod
    def _synchronization(
        job: ControlJobRecord, sync: ProductSyncRecord | None
    ) -> ProductSynchronizationProjection:
        if sync is None:
            return ProductSynchronizationProjection(readiness=SectionReadiness.PENDING)
        current = sync.review_version == job.review_version
        return ProductSynchronizationProjection(
            readiness=SectionReadiness.READY if current else SectionReadiness.OUTDATED,
            product_id=sync.product_id,
            synchronized_at=sync.synchronized_at,
            review_version=sync.review_version,
            editable_draft=not sync.provider_locked and not sync.provider_published,
        )

    def _preview(self, source: SourceArtifactRecord, now: datetime) -> ArtworkPreview:
        if self._preview_issuer is None or self._preview_origin is None:
            return ArtworkPreview(readiness=SectionReadiness.UNAVAILABLE)
        try:
            grant = self._preview_issuer.issue(source=source)
        except Exception:
            return ArtworkPreview(readiness=SectionReadiness.UNAVAILABLE)
        if (
            not isinstance(grant.url, str)
            or not isinstance(grant.expires_at, datetime)
            or not isinstance(grant.source_artifact_fingerprint, str)
            or grant.source_artifact_fingerprint != source.fingerprint
            or not self._aware(grant.expires_at)
            or grant.expires_at <= now
            or grant.expires_at - now > MAX_PREVIEW_TTL
            or not is_safe_preview_url(
                grant.url,
                exact_origin=self._preview_origin,
                job_id=source.job_id,
            )
        ):
            return ArtworkPreview(readiness=SectionReadiness.UNAVAILABLE)
        return ArtworkPreview(
            readiness=SectionReadiness.READY,
            url=grant.url,
            expires_at=grant.expires_at,
        )

    def _now(self) -> datetime:
        now = self._clock()
        if not self._aware(now):
            raise ValueError("Projection clock must return a timezone-aware timestamp")
        return now

    @staticmethod
    def _aware(value: object) -> bool:
        return (
            isinstance(value, datetime)
            and value.tzinfo is not None
            and value.utcoffset() is not None
        )


__all__ = [
    "ArtworkPreviewIssuer",
    "MAX_PREVIEW_TTL",
    "PreviewGrant",
    "ReviewProductAuthority",
    "ReviewProjectionStore",
    "ReviewProjectionUnavailableError",
    "SellerReviewProjectionService",
]
