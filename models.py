"""Canonical Phase 0 contracts.

These models are application-owned boundaries. Bedrock, Strands, persistence adapters,
and marketplace adapters must conform to them rather than redefining domain data.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

CONTRACT_VERSION = "1.0.0"
ContractVersion = Literal["1.0.0"]

NonEmptyText = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1),
]
ShortText = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=200),
]
EtsyTitle = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=140),
]
EtsyTag = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=20),
]


class ContractModel(BaseModel):
    """Strict, immutable base for data crossing system boundaries."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class ArtworkAnalysis(ContractModel):
    contract_version: ContractVersion = CONTRACT_VERSION
    subject: ShortText
    styles: tuple[ShortText, ...] = ()
    themes: tuple[ShortText, ...] = ()
    visible_text: tuple[ShortText, ...] = ()
    audience_hypotheses: tuple[ShortText, ...] = ()
    color_notes: tuple[ShortText, ...] = ()
    safety_flags: tuple[ShortText, ...] = ()
    confidence: float = Field(ge=0.0, le=1.0)


class ListingIntelligence(ContractModel):
    contract_version: ContractVersion = CONTRACT_VERSION
    title: EtsyTitle
    description: NonEmptyText
    tags: tuple[EtsyTag, ...] = Field(min_length=13, max_length=13)
    audience: tuple[ShortText, ...] = ()
    title_rationale: NonEmptyText
    tag_rationale: NonEmptyText

    @model_validator(mode="after")
    def tags_must_be_unique(self) -> ListingIntelligence:
        normalized = {" ".join(tag.casefold().split()) for tag in self.tags}
        if len(normalized) != len(self.tags):
            raise ValueError("Etsy tags must be unique after case and whitespace normalization")
        return self


class Placement(ContractModel):
    x: float = Field(ge=0.0, le=1.0)
    y: float = Field(ge=0.0, le=1.0)
    scale: float = Field(gt=0.0, le=1.0)


class ProductProfile(ContractModel):
    contract_version: ContractVersion = CONTRACT_VERSION
    profile_id: Annotated[str, StringConstraints(pattern=r"^[a-z0-9][a-z0-9_-]+$")]
    profile_version: int = Field(ge=1)
    blueprint_id: int = Field(gt=0)
    print_provider_id: int = Field(gt=0)
    variant_ids: tuple[int, ...] = Field(min_length=1)
    retail_price_cents: int = Field(gt=0)
    placement: Placement
    publish_enabled: bool = False

    @model_validator(mode="after")
    def variants_must_be_unique_and_positive(self) -> ProductProfile:
        if any(variant_id <= 0 for variant_id in self.variant_ids):
            raise ValueError("Variant IDs must be positive")
        if len(set(self.variant_ids)) != len(self.variant_ids):
            raise ValueError("Variant IDs must be unique")
        return self


class ValidationSeverity(StrEnum):
    ERROR = "error"
    WARNING = "warning"


class ValidationIssue(ContractModel):
    code: Annotated[str, StringConstraints(pattern=r"^[A-Z][A-Z0-9_]+$")]
    message: NonEmptyText
    field: ShortText | None = None
    severity: ValidationSeverity


class ValidationResult(ContractModel):
    contract_version: ContractVersion = CONTRACT_VERSION
    passed: bool
    issues: tuple[ValidationIssue, ...] = ()

    @model_validator(mode="after")
    def passed_must_match_errors(self) -> ValidationResult:
        has_errors = any(issue.severity is ValidationSeverity.ERROR for issue in self.issues)
        if self.passed == has_errors:
            raise ValueError("passed must be true exactly when no error-severity issues exist")
        return self


class ApprovalStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    INVALIDATED = "invalidated"


class JobState(StrEnum):
    UPLOADED = "uploaded"
    INTAKE_VALIDATED = "intake_validated"
    ANALYZING_ARTWORK = "analyzing_artwork"
    LISTING_DRAFTED = "listing_drafted"
    LISTING_VALIDATED = "listing_validated"
    READY_FOR_PRODUCTION = "ready_for_production"
    PRINTIFY_DRAFT_CREATED = "printify_draft_created"
    AWAITING_APPROVAL = "awaiting_approval"
    APPROVED = "approved"
    PUBLISHING = "publishing"
    PUBLISHED = "published"
    VERIFIED = "verified"
    NEEDS_REVISION = "needs_revision"
    FAILED_RETRYABLE = "failed_retryable"
    FAILED_TERMINAL = "failed_terminal"
    CANCELLED = "cancelled"


ALLOWED_JOB_TRANSITIONS: dict[JobState, frozenset[JobState]] = {
    JobState.UPLOADED: frozenset({JobState.INTAKE_VALIDATED, JobState.FAILED_TERMINAL}),
    JobState.INTAKE_VALIDATED: frozenset(
        {JobState.ANALYZING_ARTWORK, JobState.FAILED_RETRYABLE, JobState.FAILED_TERMINAL}
    ),
    JobState.ANALYZING_ARTWORK: frozenset(
        {JobState.LISTING_DRAFTED, JobState.FAILED_RETRYABLE, JobState.FAILED_TERMINAL}
    ),
    JobState.LISTING_DRAFTED: frozenset(
        {JobState.LISTING_VALIDATED, JobState.NEEDS_REVISION, JobState.FAILED_TERMINAL}
    ),
    JobState.LISTING_VALIDATED: frozenset(
        {JobState.READY_FOR_PRODUCTION, JobState.NEEDS_REVISION}
    ),
    JobState.READY_FOR_PRODUCTION: frozenset(
        {JobState.PRINTIFY_DRAFT_CREATED, JobState.FAILED_RETRYABLE, JobState.FAILED_TERMINAL}
    ),
    JobState.PRINTIFY_DRAFT_CREATED: frozenset({JobState.AWAITING_APPROVAL}),
    JobState.AWAITING_APPROVAL: frozenset(
        {JobState.APPROVED, JobState.NEEDS_REVISION, JobState.CANCELLED}
    ),
    JobState.APPROVED: frozenset(
        {JobState.PUBLISHING, JobState.NEEDS_REVISION, JobState.CANCELLED}
    ),
    JobState.PUBLISHING: frozenset(
        {JobState.PUBLISHED, JobState.FAILED_RETRYABLE, JobState.FAILED_TERMINAL}
    ),
    JobState.PUBLISHED: frozenset(
        {JobState.VERIFIED, JobState.FAILED_RETRYABLE, JobState.FAILED_TERMINAL}
    ),
    JobState.NEEDS_REVISION: frozenset({JobState.LISTING_DRAFTED, JobState.CANCELLED}),
    JobState.FAILED_RETRYABLE: frozenset(
        {
            JobState.INTAKE_VALIDATED,
            JobState.ANALYZING_ARTWORK,
            JobState.READY_FOR_PRODUCTION,
            JobState.APPROVED,
            JobState.PUBLISHED,
            JobState.FAILED_TERMINAL,
            JobState.CANCELLED,
        }
    ),
    JobState.VERIFIED: frozenset(),
    JobState.FAILED_TERMINAL: frozenset(),
    JobState.CANCELLED: frozenset(),
}


class ReviewSnapshot(ContractModel):
    contract_version: ContractVersion = CONTRACT_VERSION
    review_version: int = Field(ge=1)
    artwork_analysis: ArtworkAnalysis
    listing: ListingIntelligence
    profile: ProductProfile
    validation: ValidationResult
    printify_product_id: str | None = None
    approval_status: ApprovalStatus = ApprovalStatus.PENDING

    @model_validator(mode="after")
    def approval_requires_valid_listing(self) -> ReviewSnapshot:
        if self.approval_status is ApprovalStatus.APPROVED and not self.validation.passed:
            raise ValueError("An invalid review snapshot cannot be approved")
        return self


class JobRecord(ContractModel):
    contract_version: ContractVersion = CONTRACT_VERSION
    job_id: Annotated[str, StringConstraints(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_-]+$")]
    state: JobState
    review_version: int = Field(ge=0)
    approved_review_version: int | None = Field(default=None, ge=1)
    idempotency_key: NonEmptyText
    artwork_object_key: NonEmptyText
    printify_image_id: str | None = None
    printify_product_id: str | None = None
    published_listing_id: str | None = None
    created_at: datetime
    updated_at: datetime

    @model_validator(mode="after")
    def approval_version_must_be_current_when_approved(self) -> JobRecord:
        approval_bound_states = {
            JobState.APPROVED,
            JobState.PUBLISHING,
            JobState.PUBLISHED,
            JobState.VERIFIED,
        }
        approval_is_current = self.approved_review_version == self.review_version
        if self.state in approval_bound_states and not approval_is_current:
            raise ValueError(
                "Approved and publishing states require approval for the current review"
            )
        if self.updated_at < self.created_at:
            raise ValueError("updated_at cannot precede created_at")
        return self
