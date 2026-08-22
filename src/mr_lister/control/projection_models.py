"""Strict seller-facing models for the consolidated Phase 6 review projection."""

from __future__ import annotations

from datetime import date, datetime
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import Field, StringConstraints, model_validator

from mr_lister.control.models import ControlModel

PublicId = Annotated[
    str,
    StringConstraints(strip_whitespace=True, pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,127}$"),
]
Fingerprint = Annotated[str, StringConstraints(pattern=r"^[a-f0-9]{64}$")]
PublicText = Annotated[str, StringConstraints(min_length=1, max_length=100_000)]
ShortPublicText = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=300),
]


class ReviewDisplayState(StrEnum):
    PREPARING = "preparing"
    NEEDS_REVISION = "needs_revision"
    SYNCHRONIZING = "synchronizing"
    READY = "ready_for_review"
    REFRESHING_ESTIMATE = "refreshing_estimate"
    RECONCILING = "reconciling"
    CANCELLING = "cancelling"
    RETRYABLE_FAILURE = "retryable_failure"
    TERMINAL_FAILURE = "terminal_failure"
    CANCELLED = "cancelled"
    APPROVED = "approved"


class ReviewStage(StrEnum):
    UPLOAD_VERIFIED = "upload_verified"
    ARTWORK_REVIEW = "artwork_review"
    LISTING_VALIDATION = "listing_validation"
    SELLER_REVISION = "seller_revision"
    PRODUCT_SYNC = "product_sync"
    HUMAN_REVIEW = "human_review"
    ECONOMICS_REFRESH = "economics_refresh"
    PROVIDER_RECONCILIATION = "provider_reconciliation"
    CANCELLATION = "cancellation"
    RECOVERY = "recovery"
    COMPLETE = "complete"


class SellerAction(StrEnum):
    EDIT_LISTING = "edit_listing"
    APPROVE_REVIEW = "approve_review"
    CANCEL_JOB = "cancel_job"
    RETRY_JOB = "retry_job"
    REFRESH_ECONOMICS = "refresh_economics"


class ActionReason(StrEnum):
    AVAILABLE = "AVAILABLE"
    NOT_IN_CURRENT_STATE = "NOT_IN_CURRENT_STATE"
    REVIEW_NOT_READY = "REVIEW_NOT_READY"
    REVIEW_INVALID = "REVIEW_INVALID"
    PRODUCT_NOT_CURRENT = "PRODUCT_NOT_CURRENT"
    PRODUCT_NOT_REVIEWABLE = "PRODUCT_NOT_REVIEWABLE"
    MOCKUPS_NOT_READY = "MOCKUPS_NOT_READY"
    ECONOMICS_MISSING = "ECONOMICS_MISSING"
    ECONOMICS_STALE = "ECONOMICS_STALE"
    PROVIDER_OUTCOME_UNCONFIRMED = "PROVIDER_OUTCOME_UNCONFIRMED"
    CANCELLATION_PENDING = "CANCELLATION_PENDING"
    RETRY_NOT_AVAILABLE = "RETRY_NOT_AVAILABLE"


class SectionReadiness(StrEnum):
    PENDING = "pending"
    READY = "ready"
    OUTDATED = "outdated"
    UNAVAILABLE = "unavailable"


class EconomicsReadiness(StrEnum):
    MISSING = "missing"
    REFRESHING = "refreshing"
    READY = "ready"
    STALE = "stale"
    OUTDATED = "outdated"
    UNAVAILABLE = "unavailable"


class SellerActionCapability(ControlModel):
    action: SellerAction
    enabled: bool
    reason: ActionReason
    message: ShortPublicText

    @model_validator(mode="after")
    def enabled_actions_have_available_reason(self) -> SellerActionCapability:
        if self.enabled != (self.reason is ActionReason.AVAILABLE):
            raise ValueError("Action availability and reason disagree")
        return self


class ArtworkPreview(ControlModel):
    readiness: SectionReadiness
    url: str | None = Field(default=None, max_length=2_048)
    expires_at: datetime | None = None

    @model_validator(mode="after")
    def ready_preview_has_complete_grant(self) -> ArtworkPreview:
        complete = self.url is not None and self.expires_at is not None
        if (self.readiness is SectionReadiness.READY) != complete:
            raise ValueError("A ready preview requires one complete short-lived grant")
        return self


class ArtworkInterpretation(ControlModel):
    readiness: SectionReadiness
    subject: ShortPublicText | None = None
    visual_elements: tuple[ShortPublicText, ...] = Field(default=(), max_length=20)
    styles: tuple[ShortPublicText, ...] = Field(default=(), max_length=20)
    themes: tuple[ShortPublicText, ...] = Field(default=(), max_length=20)
    visible_text: tuple[ShortPublicText, ...] = Field(default=(), max_length=20)
    safety_notes: tuple[ShortPublicText, ...] = Field(default=(), max_length=20)
    confidence: float | None = Field(default=None, ge=0, le=1)


class ListingProjection(ControlModel):
    readiness: SectionReadiness
    title: str | None = Field(default=None, max_length=140)
    description: str | None = Field(default=None, max_length=100_000)
    tags: tuple[str, ...] = Field(default=(), max_length=13)
    audience: tuple[ShortPublicText, ...] = Field(default=(), max_length=20)

    @model_validator(mode="after")
    def ready_listing_has_complete_content(self) -> ListingProjection:
        if self.readiness is SectionReadiness.READY:
            if self.title is None or self.description is None or len(self.tags) != 13:
                raise ValueError("A ready listing requires title, description, and thirteen tags")
        elif self.title is not None or self.description is not None or self.tags or self.audience:
            raise ValueError("A non-ready listing cannot expose partial content")
        return self


class PublicValidationIssue(ControlModel):
    code: str = Field(pattern=r"^[A-Z][A-Z0-9_]{0,99}$")
    path: str = Field(min_length=1, max_length=100)
    severity: Literal["error", "warning"]
    message: ShortPublicText


class ListingValidationProjection(ControlModel):
    readiness: SectionReadiness
    passed: bool | None = None
    issues: tuple[PublicValidationIssue, ...] = Field(default=(), max_length=50)

    @model_validator(mode="after")
    def ready_validation_has_result(self) -> ListingValidationProjection:
        if (self.readiness is SectionReadiness.READY) != (self.passed is not None):
            raise ValueError("Ready validation requires one deterministic result")
        return self


class PlacementPresentation(ControlModel):
    group_id: PublicId
    sizes: tuple[ShortPublicText, ...] = Field(min_length=1, max_length=20)
    position: ShortPublicText
    decoration_method: ShortPublicText
    x: float = Field(ge=0, le=1)
    y: float = Field(ge=0, le=1)
    scale: float = Field(gt=0, le=1)
    angle: int = Field(ge=-360, le=360)


class ProductPolicyProjection(ControlModel):
    product_name: ShortPublicText
    provider_name: ShortPublicText
    colors: tuple[ShortPublicText, ...] = Field(min_length=1, max_length=30)
    sizes: tuple[ShortPublicText, ...] = Field(min_length=1, max_length=30)
    placements: tuple[PlacementPresentation, ...] = Field(min_length=1, max_length=10)
    retail_price_cents: int = Field(gt=0)
    buyer_shipping_cents: int = Field(ge=0)
    currency: Literal["USD"] = "USD"


class ProductSynchronizationProjection(ControlModel):
    readiness: SectionReadiness
    product_id: PublicId | None = None
    synchronized_at: datetime | None = None
    review_version: int | None = Field(default=None, ge=1)
    editable_draft: bool | None = None


class MockupProjection(ControlModel):
    url: str = Field(min_length=1, max_length=2_048)
    alt_text: ShortPublicText


class MockupSetProjection(ControlModel):
    readiness: SectionReadiness
    items: tuple[MockupProjection, ...] = Field(default=(), max_length=5)

    @model_validator(mode="after")
    def ready_mockups_are_nonempty(self) -> MockupSetProjection:
        if (self.readiness is SectionReadiness.READY) != bool(self.items):
            raise ValueError("Ready mockups require a bounded nonempty set")
        return self


class VariantEconomicsProjection(ControlModel):
    color: ShortPublicText
    size: ShortPublicText
    retail_price_cents: int = Field(gt=0)
    buyer_shipping_cents: int = Field(ge=0)
    production_cost_cents: int = Field(ge=0)
    production_shipping_cents: int = Field(ge=0)
    marketplace_fees_cents: int = Field(ge=0)
    estimated_proceeds_cents: int


class EconomicsProjection(ControlModel):
    readiness: EconomicsReadiness
    currency: Literal["USD"] = "USD"
    label: Literal["Estimated proceeds"] = "Estimated proceeds"
    minimum_cents: int | None = None
    maximum_cents: int | None = None
    variants: tuple[VariantEconomicsProjection, ...] = Field(default=(), max_length=100)
    calculated_at: datetime | None = None
    fresh_until: datetime | None = None
    production_cost_source: Literal["Connected production product readback"] | None = None
    production_cost_observed_at: datetime | None = None
    production_shipping_source: Literal["Connected production standard US shipping"] | None = None
    production_shipping_observed_at: datetime | None = None
    fee_policy_source: Literal["Etsy US standard fee policy"] | None = None
    fee_policy_id: Literal["etsy-us-standard-v1"] | None = None
    fee_policy_verified_on: date | None = None
    assumptions: tuple[ShortPublicText, ...] = Field(default=(), max_length=20)

    @model_validator(mode="after")
    def ready_or_stale_economics_are_complete(self) -> EconomicsProjection:
        displayable = self.readiness in {EconomicsReadiness.READY, EconomicsReadiness.STALE}
        complete = (
            self.minimum_cents is not None
            and self.maximum_cents is not None
            and bool(self.variants)
            and self.calculated_at is not None
            and self.fresh_until is not None
            and self.production_cost_source is not None
            and self.production_cost_observed_at is not None
            and self.production_shipping_source is not None
            and self.production_shipping_observed_at is not None
            and self.fee_policy_source is not None
            and self.fee_policy_id is not None
            and self.fee_policy_verified_on is not None
        )
        if displayable != complete:
            raise ValueError("Displayable economics require complete immutable evidence")
        return self


class StrandsProvenanceProjection(ControlModel):
    readiness: SectionReadiness
    framework: Literal["strands-agents"] | None = None
    agent_id: Literal["mr-lister-preparation"] | None = None
    prepared_review_version: int | None = Field(default=None, ge=1)
    correlation_id: str | None = Field(default=None, pattern=r"^[a-f0-9]{24}$")
    tool_calls: tuple[Literal["record_prepared_review"], ...] = Field(default=(), max_length=1)
    completed_at: datetime | None = None


class FailureProjection(ControlModel):
    code: str = Field(pattern=r"^[A-Z][A-Z0-9_]{0,99}$")
    message: ShortPublicText
    stage: ReviewStage
    retryable: bool
    recovery: SellerAction | None = None


class SellerReviewProjection(ControlModel):
    job_id: PublicId
    record_version: int = Field(ge=0)
    review_version: int = Field(ge=0)
    review_fingerprint: Fingerprint | None = None
    review_authority_etag: Fingerprint | None = None
    display_state: ReviewDisplayState
    stage: ReviewStage
    authority_notice: Literal["Unpublished — not on Etsy"] = "Unpublished — not on Etsy"
    actions: tuple[SellerActionCapability, ...] = Field(min_length=5, max_length=5)
    preview: ArtworkPreview
    artwork: ArtworkInterpretation
    listing: ListingProjection
    validation: ListingValidationProjection
    product_policy: ProductPolicyProjection
    synchronization: ProductSynchronizationProjection
    mockups: MockupSetProjection
    economics: EconomicsProjection
    strands: StrandsProvenanceProjection
    failure: FailureProjection | None = None
    provider_outcome_unconfirmed: bool = False
    created_at: datetime
    updated_at: datetime

    @model_validator(mode="after")
    def actions_cover_closed_seller_surface(self) -> SellerReviewProjection:
        if tuple(item.action for item in self.actions) != tuple(SellerAction):
            raise ValueError("Projection actions must cover the exact closed seller surface")
        if (self.review_version == 0) != (self.review_fingerprint is None):
            raise ValueError("Review authority is incomplete")
        if (self.review_version == 0) != (self.review_authority_etag is None):
            raise ValueError("Review ETag authority is incomplete")
        return self


__all__ = [
    "ActionReason",
    "ArtworkInterpretation",
    "ArtworkPreview",
    "EconomicsProjection",
    "EconomicsReadiness",
    "FailureProjection",
    "ListingProjection",
    "ListingValidationProjection",
    "MockupProjection",
    "MockupSetProjection",
    "PlacementPresentation",
    "ProductPolicyProjection",
    "ProductSynchronizationProjection",
    "PublicValidationIssue",
    "ReviewDisplayState",
    "ReviewStage",
    "SectionReadiness",
    "SellerAction",
    "SellerActionCapability",
    "SellerReviewProjection",
    "StrandsProvenanceProjection",
    "VariantEconomicsProjection",
]
