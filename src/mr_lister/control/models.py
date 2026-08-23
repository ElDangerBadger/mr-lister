"""Strict Phase 6 seller-control records and closed lifecycle rules."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import Field, StrictInt, StringConstraints, model_validator

from mr_lister.contracts import ArtworkAnalysis, ContractModel
from mr_lister.contracts.presentation import ProductMockupEvidence
from mr_lister.control.economics import EtsyUsStandardEstimate
from mr_lister.control.fingerprints import agent_preparation_evidence_fingerprint

CONTROL_CONTRACT_VERSION = "2.0.0"
ControlContractVersion = Literal["2.0.0"]
PHASE6_MAX_SOURCE_ARTWORK_BYTES = 5 * 1024 * 1024

SafeId = Annotated[
    str,
    StringConstraints(strip_whitespace=True, pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,127}$"),
]
OwnerId = Annotated[str, StringConstraints(pattern=r"^[a-f0-9]{64}$")]
Fingerprint = Annotated[str, StringConstraints(pattern=r"^[a-f0-9]{64}$")]
StableCode = Annotated[str, StringConstraints(pattern=r"^[A-Z][A-Z0-9_]{0,99}$")]
NonEmptyText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class ControlModel(ContractModel):
    """Base for records that must never be confused with legacy contract 1.0.0."""

    contract_version: ControlContractVersion = CONTROL_CONTRACT_VERSION


class ControlJobState(StrEnum):
    INTAKE_VALIDATED = "intake_validated"
    ANALYZING_ARTWORK = "analyzing_artwork"
    LISTING_DRAFTED = "listing_drafted"
    NEEDS_REVISION = "needs_revision"
    PRODUCT_DRAFT_SYNCING = "product_draft_syncing"
    AWAITING_APPROVAL = "awaiting_approval"
    PRICING_REFRESHING = "pricing_refreshing"
    RECONCILIATION_REQUIRED = "reconciliation_required"
    FAILED_RETRYABLE = "failed_retryable"
    FAILED_TERMINAL = "failed_terminal"
    CANCEL_REQUESTED = "cancel_requested"
    CANCELLED = "cancelled"
    APPROVED = "approved"


CONTROL_ALLOWED_TRANSITIONS: dict[ControlJobState, frozenset[ControlJobState]] = {
    ControlJobState.INTAKE_VALIDATED: frozenset(
        {
            ControlJobState.ANALYZING_ARTWORK,
            ControlJobState.CANCEL_REQUESTED,
            ControlJobState.CANCELLED,
            ControlJobState.FAILED_RETRYABLE,
            ControlJobState.FAILED_TERMINAL,
        }
    ),
    ControlJobState.ANALYZING_ARTWORK: frozenset(
        {
            ControlJobState.LISTING_DRAFTED,
            ControlJobState.CANCEL_REQUESTED,
            ControlJobState.CANCELLED,
            ControlJobState.FAILED_RETRYABLE,
            ControlJobState.FAILED_TERMINAL,
        }
    ),
    ControlJobState.LISTING_DRAFTED: frozenset(
        {
            ControlJobState.NEEDS_REVISION,
            ControlJobState.PRODUCT_DRAFT_SYNCING,
            ControlJobState.CANCEL_REQUESTED,
            ControlJobState.CANCELLED,
            ControlJobState.FAILED_RETRYABLE,
            ControlJobState.FAILED_TERMINAL,
        }
    ),
    ControlJobState.NEEDS_REVISION: frozenset(
        {
            ControlJobState.NEEDS_REVISION,
            ControlJobState.PRODUCT_DRAFT_SYNCING,
            ControlJobState.CANCELLED,
        }
    ),
    ControlJobState.PRODUCT_DRAFT_SYNCING: frozenset(
        {
            ControlJobState.PRODUCT_DRAFT_SYNCING,
            ControlJobState.AWAITING_APPROVAL,
            ControlJobState.PRICING_REFRESHING,
            ControlJobState.RECONCILIATION_REQUIRED,
            ControlJobState.CANCEL_REQUESTED,
            ControlJobState.CANCELLED,
            ControlJobState.FAILED_RETRYABLE,
            ControlJobState.FAILED_TERMINAL,
        }
    ),
    ControlJobState.AWAITING_APPROVAL: frozenset(
        {
            ControlJobState.NEEDS_REVISION,
            ControlJobState.PRODUCT_DRAFT_SYNCING,
            ControlJobState.PRICING_REFRESHING,
            ControlJobState.APPROVED,
            ControlJobState.CANCELLED,
        }
    ),
    ControlJobState.PRICING_REFRESHING: frozenset(
        {
            ControlJobState.AWAITING_APPROVAL,
            ControlJobState.CANCEL_REQUESTED,
            ControlJobState.CANCELLED,
            ControlJobState.FAILED_RETRYABLE,
            ControlJobState.FAILED_TERMINAL,
        }
    ),
    ControlJobState.RECONCILIATION_REQUIRED: frozenset(
        {
            ControlJobState.RECONCILIATION_REQUIRED,
            ControlJobState.PRODUCT_DRAFT_SYNCING,
            ControlJobState.AWAITING_APPROVAL,
            ControlJobState.PRICING_REFRESHING,
            ControlJobState.CANCEL_REQUESTED,
            ControlJobState.CANCELLED,
            ControlJobState.FAILED_TERMINAL,
        }
    ),
    ControlJobState.FAILED_RETRYABLE: frozenset(
        {
            ControlJobState.ANALYZING_ARTWORK,
            ControlJobState.LISTING_DRAFTED,
            ControlJobState.PRODUCT_DRAFT_SYNCING,
            ControlJobState.PRICING_REFRESHING,
            ControlJobState.RECONCILIATION_REQUIRED,
            ControlJobState.CANCELLED,
        }
    ),
    ControlJobState.CANCEL_REQUESTED: frozenset(
        {ControlJobState.RECONCILIATION_REQUIRED, ControlJobState.CANCELLED}
    ),
    ControlJobState.APPROVED: frozenset(),
    ControlJobState.CANCELLED: frozenset(),
    ControlJobState.FAILED_TERMINAL: frozenset(),
}

CONTROL_TERMINAL_STATES = frozenset(
    {ControlJobState.APPROVED, ControlJobState.CANCELLED, ControlJobState.FAILED_TERMINAL}
)


def can_control_transition(current: ControlJobState, target: ControlJobState) -> bool:
    return target in CONTROL_ALLOWED_TRANSITIONS[current]


class ReviewActor(StrEnum):
    MODEL = "model"
    SELLER = "seller"


class ReviewDecision(StrEnum):
    REVISE = "revise"
    APPROVE = "approve"


class WorkType(StrEnum):
    PREPARE = "prepare"
    SYNCHRONIZE_PRODUCT = "synchronize_product"
    RECONCILE_PRODUCT = "reconcile_product"
    REFRESH_ECONOMICS = "refresh_economics"


class WorkRequestStatus(StrEnum):
    PENDING = "pending"
    CLAIMED = "claimed"
    DISPATCHED = "dispatched"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


class RecoveryAction(StrEnum):
    RETRY_PREPARATION = "retry_preparation"
    RETRY_AGENT_DECISION = "retry_agent_decision"
    RETRY_PRODUCT_SYNC = "retry_product_sync"
    RETRY_RECONCILIATION = "retry_reconciliation"
    RETRY_PRICING = "retry_pricing"


class ProviderWriteOperation(StrEnum):
    CREATE = "create"
    UPDATE = "update"


class ProviderCallPermitStatus(StrEnum):
    AVAILABLE = "available"
    CONSUMED = "consumed"
    RETIRED = "retired"


class ReconciliationOutcome(StrEnum):
    TARGET_MATCH = "target_match"
    PRIOR_MATCH = "prior_match"
    NO_MATCH = "no_match"
    MISSING = "missing"
    MULTIPLE_MATCHES = "multiple_matches"
    CONFLICT = "conflict"
    UNAVAILABLE = "unavailable"


CONTROL_NEW_WORK_BY_STATE: dict[ControlJobState, WorkType] = {
    ControlJobState.INTAKE_VALIDATED: WorkType.PREPARE,
    ControlJobState.ANALYZING_ARTWORK: WorkType.PREPARE,
    ControlJobState.LISTING_DRAFTED: WorkType.PREPARE,
    ControlJobState.PRODUCT_DRAFT_SYNCING: WorkType.SYNCHRONIZE_PRODUCT,
    ControlJobState.RECONCILIATION_REQUIRED: WorkType.RECONCILE_PRODUCT,
    ControlJobState.PRICING_REFRESHING: WorkType.REFRESH_ECONOMICS,
}

CONTROL_RECOVERY_BINDINGS: dict[RecoveryAction, tuple[ControlJobState, WorkType]] = {
    RecoveryAction.RETRY_PREPARATION: (
        ControlJobState.ANALYZING_ARTWORK,
        WorkType.PREPARE,
    ),
    RecoveryAction.RETRY_AGENT_DECISION: (
        ControlJobState.LISTING_DRAFTED,
        WorkType.PREPARE,
    ),
    RecoveryAction.RETRY_PRODUCT_SYNC: (
        ControlJobState.PRODUCT_DRAFT_SYNCING,
        WorkType.SYNCHRONIZE_PRODUCT,
    ),
    RecoveryAction.RETRY_RECONCILIATION: (
        ControlJobState.RECONCILIATION_REQUIRED,
        WorkType.RECONCILE_PRODUCT,
    ),
    RecoveryAction.RETRY_PRICING: (
        ControlJobState.PRICING_REFRESHING,
        WorkType.REFRESH_ECONOMICS,
    ),
}


class ControlJobRecord(ControlModel):
    owner_id: OwnerId
    job_id: SafeId
    record_version: int = Field(default=0, ge=0)
    event_sequence: int = Field(default=0, ge=0)
    state: ControlJobState
    review_version: int = Field(default=0, ge=0)
    review_fingerprint: Fingerprint | None = None
    review_validated: bool = False
    source_artifact_fingerprint: Fingerprint | None = None
    artwork_analysis_id: SafeId | None = None
    artwork_analysis_fingerprint: Fingerprint | None = None
    agent_evidence_id: SafeId | None = None
    agent_evidence_fingerprint: Fingerprint | None = None
    product_id: SafeId | None = None
    provider_payload_fingerprint: Fingerprint | None = None
    product_sync_id: SafeId | None = None
    synchronized_review_version: int | None = Field(default=None, ge=1)
    product_sync_fingerprint: Fingerprint | None = None
    pricing_snapshot_id: SafeId | None = None
    pricing_snapshot_fingerprint: Fingerprint | None = None
    approval_decision_id: SafeId | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    approved_review_version: int | None = Field(default=None, ge=1)
    approved_review_fingerprint: Fingerprint | None = None
    approval_fingerprint: Fingerprint | None = None
    publication_aggregate_id: SafeId | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    active_work_request_id: SafeId | None = None
    provider_upload_attempt_id: SafeId | None = None
    uploaded_artwork_id: SafeId | None = None
    uploaded_image_id: SafeId | None = None
    uploaded_artwork_fingerprint: Fingerprint | None = None
    provider_write_attempt_id: SafeId | None = None
    product_create_attempt_id: SafeId | None = None
    failure_id: SafeId | None = None
    cancellation_requested_at: datetime | None = None
    provider_outcome_unconfirmed: bool = False
    upload_outcome_unconfirmed: bool = False
    created_at: datetime
    updated_at: datetime

    @model_validator(mode="after")
    def authority_fields_are_coherent(self) -> ControlJobRecord:
        if self.updated_at < self.created_at:
            raise ValueError("updated_at cannot precede created_at")
        if (self.review_version == 0) != (self.review_fingerprint is None):
            raise ValueError("Review version zero must have no review fingerprint")
        analysis_fields = (self.artwork_analysis_id, self.artwork_analysis_fingerprint)
        if any(value is not None for value in analysis_fields) and not all(
            value is not None for value in analysis_fields
        ):
            raise ValueError("Artwork analysis authority fields are all-or-none")
        agent_fields = (self.agent_evidence_id, self.agent_evidence_fingerprint)
        if any(value is not None for value in agent_fields) and not all(
            value is not None for value in agent_fields
        ):
            raise ValueError("Agent evidence authority fields are all-or-none")
        upload_fields = (
            self.uploaded_artwork_id,
            self.uploaded_image_id,
            self.uploaded_artwork_fingerprint,
        )
        if any(value is not None for value in upload_fields) and not all(
            value is not None for value in upload_fields
        ):
            raise ValueError("Uploaded artwork authority fields are all-or-none")
        if self.uploaded_artwork_id is not None and self.provider_upload_attempt_id is None:
            raise ValueError("Uploaded artwork requires its immutable upload attempt")
        if self.upload_outcome_unconfirmed and self.uploaded_artwork_id is not None:
            raise ValueError("An unconfirmed upload cannot already have confirmed authority")
        if self.upload_outcome_unconfirmed and self.provider_upload_attempt_id is None:
            raise ValueError("Upload uncertainty requires an immutable upload attempt")
        if self.upload_outcome_unconfirmed and self.provider_outcome_unconfirmed:
            raise ValueError("Upload and product-write uncertainty are mutually exclusive")
        sync_fields = (
            self.product_sync_id,
            self.synchronized_review_version,
            self.product_sync_fingerprint,
        )
        if any(value is not None for value in sync_fields) and not all(
            value is not None for value in sync_fields
        ):
            raise ValueError("Product synchronization authority fields are all-or-none")
        if self.synchronized_review_version is not None:
            if self.product_id is None or self.synchronized_review_version > self.review_version:
                raise ValueError(
                    "Product synchronization must bind an existing current-or-prior review"
                )
        pricing_fields = (self.pricing_snapshot_id, self.pricing_snapshot_fingerprint)
        if any(value is not None for value in pricing_fields) and not all(
            value is not None for value in pricing_fields
        ):
            raise ValueError("Pricing authority fields are all-or-none")
        approval_fields = (
            self.approved_review_version,
            self.approved_review_fingerprint,
            self.approval_fingerprint,
        )
        if any(value is not None for value in approval_fields) and not all(
            value is not None for value in approval_fields
        ):
            raise ValueError("Approval authority fields are all-or-none")
        if self.state is ControlJobState.APPROVED:
            if self.approved_review_version != self.review_version:
                raise ValueError("Approval must bind the current review version")
            if self.approved_review_fingerprint != self.review_fingerprint:
                raise ValueError("Approval must bind the current review fingerprint")
            if self.synchronized_review_version != self.review_version:
                raise ValueError("Approval requires the current review to be synchronized")
            if self.pricing_snapshot_id is None:
                raise ValueError("Approval requires a pricing snapshot")
        elif any(value is not None for value in approval_fields):
            raise ValueError("Only an approved job may carry approval authority")
        if self.approval_decision_id is not None and self.state is not ControlJobState.APPROVED:
            raise ValueError("Only an approved job may reference an approval decision")
        if self.publication_aggregate_id is not None and self.state is not ControlJobState.APPROVED:
            raise ValueError("Only an approved job may reference a publication aggregate")
        if self.state in CONTROL_NEW_WORK_BY_STATE and self.active_work_request_id is None:
            raise ValueError("Machine states require durable active work")
        if self.state is ControlJobState.CANCEL_REQUESTED and self.active_work_request_id is None:
            raise ValueError("Cancellation in progress requires durable active work")
        if self.active_work_request_id is not None and self.state not in {
            *CONTROL_NEW_WORK_BY_STATE,
            ControlJobState.CANCEL_REQUESTED,
        }:
            raise ValueError("Only machine or cancelling states may retain active work")
        if self.state in {ControlJobState.FAILED_RETRYABLE, ControlJobState.FAILED_TERMINAL}:
            if self.failure_id is None:
                raise ValueError("Failure states require an immutable failure record")
        if self.state in {ControlJobState.CANCEL_REQUESTED, ControlJobState.CANCELLED}:
            if self.cancellation_requested_at is None:
                raise ValueError("Cancellation states require immutable cancellation intent")
        if self.cancellation_requested_at is not None and self.state not in {
            ControlJobState.CANCEL_REQUESTED,
            ControlJobState.RECONCILIATION_REQUIRED,
            ControlJobState.CANCELLED,
        }:
            raise ValueError("Cancellation intent permanently disables normal job states")
        return self


class ReviewContent(ControlModel):
    job_id: SafeId
    review_version: int = Field(ge=1)
    fingerprint: Fingerprint
    actor: ReviewActor
    title: str = Field(min_length=1, max_length=140)
    description: str = Field(min_length=1, max_length=100_000)
    tags: tuple[str, ...] = Field(min_length=13, max_length=13)
    audience: tuple[str, ...] = ()
    title_rationale: str = Field(min_length=1)
    tag_rationale: str = Field(min_length=1)
    validation_passed: bool
    validation_issue_codes: tuple[StableCode, ...] = ()
    artwork_analysis_fingerprint: Fingerprint
    product_profile_fingerprint: Fingerprint
    created_at: datetime

    @model_validator(mode="after")
    def validation_matches_issues(self) -> ReviewContent:
        if self.validation_passed == bool(self.validation_issue_codes):
            raise ValueError("A valid review has no blocking issue codes")
        return self


class SourceArtifactRecord(ControlModel):
    """Pinned, owner-scoped source used by the durable preparation runtime."""

    job_id: SafeId
    owner_id: OwnerId
    fingerprint: Fingerprint
    bucket: NonEmptyText
    object_key: NonEmptyText
    version_id: NonEmptyText
    content_sha256: Fingerprint
    size_bytes: int = Field(gt=0, le=PHASE6_MAX_SOURCE_ARTWORK_BYTES)
    media_type: Literal["image/png"] = "image/png"
    product_profile_id: SafeId
    product_profile_version: int = Field(ge=1)
    product_profile_fingerprint: Fingerprint
    created_at: datetime

    @model_validator(mode="after")
    def object_key_is_scoped_to_exact_owner_and_job(self) -> SourceArtifactRecord:
        expected = f"private/owners/{self.owner_id}/jobs/{self.job_id}/source/source.png"
        if self.object_key != expected:
            raise ValueError("Source artifact key must bind the exact owner and job")
        return self


class ArtworkAnalysisRecord(ControlModel):
    analysis_id: SafeId
    job_id: SafeId
    work_request_id: SafeId
    source_artifact_fingerprint: Fingerprint
    fingerprint: Fingerprint
    analysis: ArtworkAnalysis
    created_at: datetime


AgentToolName = Literal["record_prepared_review"]


class AgentPreparationEvidence(ControlModel):
    evidence_id: SafeId
    job_id: SafeId
    work_request_id: SafeId
    review_version: int = Field(ge=1)
    correlation_id: Annotated[str, StringConstraints(pattern=r"^[a-f0-9]{24}$")]
    framework: Literal["strands-agents"]
    agent_id: Literal["mr-lister-preparation"]
    controller_model_id: NonEmptyText
    tool_calls: tuple[AgentToolName, ...] = Field(min_length=1)
    cycles: int = Field(ge=1, le=4)
    input_tokens: int = Field(ge=0, le=12_000)
    output_tokens: int = Field(ge=0, le=2_500)
    total_tokens: int = Field(ge=0, le=12_000)
    decision_fingerprint: Fingerprint
    fingerprint: Fingerprint
    requires_human_approval: Literal[True] = True
    publication_authorized: Literal[False] = False
    created_at: datetime

    @model_validator(mode="after")
    def token_totals_and_tools_are_coherent(self) -> AgentPreparationEvidence:
        if self.total_tokens != self.input_tokens + self.output_tokens:
            raise ValueError("Agent token total must equal input plus output")
        if len(set(self.tool_calls)) != len(self.tool_calls):
            raise ValueError("Agent tool evidence cannot repeat tool names")
        if self.tool_calls != ("record_prepared_review",):
            raise ValueError("Phase 6 evidence requires the exact Strands tool")
        return self

    @property
    def authority_fingerprint(self) -> str:
        """Recompute the fingerprint from every persisted provenance field."""

        return agent_preparation_evidence_fingerprint(
            evidence_id=self.evidence_id,
            job_id=self.job_id,
            work_request_id=self.work_request_id,
            review_version=self.review_version,
            correlation_id=self.correlation_id,
            framework=self.framework,
            agent_id=self.agent_id,
            controller_model_id=self.controller_model_id,
            tool_calls=self.tool_calls,
            cycles=self.cycles,
            input_tokens=self.input_tokens,
            output_tokens=self.output_tokens,
            total_tokens=self.total_tokens,
            decision_fingerprint=self.decision_fingerprint,
            requires_human_approval=self.requires_human_approval,
            publication_authorized=self.publication_authorized,
            created_at=self.created_at,
        )


class ProviderWriteAttempt(ControlModel):
    attempt_id: SafeId
    job_id: SafeId
    work_request_id: SafeId
    review_version: int = Field(ge=1)
    operation: ProviderWriteOperation
    product_id: SafeId | None = None
    image_id: SafeId
    target_payload_fingerprint: Fingerprint
    prior_payload_fingerprint: Fingerprint | None = None
    correlation_token: Annotated[str, StringConstraints(pattern=r"^ml-[a-f0-9]{24}$")]
    exact_retry_count: int = Field(default=0, ge=0, le=1)
    reconciliation_deadline: datetime
    started_at: datetime

    @model_validator(mode="after")
    def operation_authority_is_coherent(self) -> ProviderWriteAttempt:
        if self.reconciliation_deadline <= self.started_at:
            raise ValueError("Provider reconciliation deadline must follow the write start")
        if self.operation is ProviderWriteOperation.CREATE:
            if (
                self.product_id is not None
                or self.prior_payload_fingerprint is not None
                or self.exact_retry_count != 0
            ):
                raise ValueError("Initial create cannot carry prior product authority")
        elif self.product_id is None or self.prior_payload_fingerprint is None:
            raise ValueError("Provider update must bind the exact prior product payload")
        return self


class ProviderUploadAttempt(ControlModel):
    """Immutable authority for the only provider artwork POST allowed for a job."""

    attempt_id: SafeId
    job_id: SafeId
    work_request_id: SafeId
    source_artifact_fingerprint: Fingerprint
    file_name: Annotated[
        str,
        StringConstraints(pattern=r"^mr-lister-[a-f0-9]{24}-[a-f0-9]{16}\.png$"),
    ]
    reconciliation_deadline: datetime
    started_at: datetime

    @model_validator(mode="after")
    def deadline_follows_start(self) -> ProviderUploadAttempt:
        if self.reconciliation_deadline <= self.started_at:
            raise ValueError("Upload reconciliation deadline must follow the upload start")
        return self


class ProviderCallPermit(ControlModel):
    """One-shot authorization consumed immediately before an external mutation."""

    attempt_id: SafeId
    job_id: SafeId
    work_request_id: SafeId
    status: ProviderCallPermitStatus = ProviderCallPermitStatus.AVAILABLE
    created_at: datetime
    consumed_at: datetime | None = None
    consumed_work_request_id: SafeId | None = None
    retired_at: datetime | None = None

    @model_validator(mode="after")
    def consumption_time_matches_status(self) -> ProviderCallPermit:
        consumed = self.status is ProviderCallPermitStatus.CONSUMED
        retired = self.status is ProviderCallPermitStatus.RETIRED
        if consumed != (self.consumed_at is not None and self.consumed_work_request_id is not None):
            raise ValueError("Consumed provider call permits require time and active work proof")
        if not consumed and (
            self.consumed_at is not None or self.consumed_work_request_id is not None
        ):
            raise ValueError("Unconsumed provider call permits cannot carry consumption proof")
        if retired != (self.retired_at is not None):
            raise ValueError("Retired provider call permits require retirement time proof")
        if consumed and self.retired_at is not None:
            raise ValueError("Consumed provider call permits cannot also be retired")
        if self.consumed_at is not None and self.consumed_at < self.created_at:
            raise ValueError("Provider call permit cannot be consumed before creation")
        if self.retired_at is not None and self.retired_at < self.created_at:
            raise ValueError("Provider call permit cannot be retired before creation")
        return self


class UploadedArtworkRecord(ControlModel):
    """Confirmed provider image authority, reused for every revision of one job."""

    upload_id: SafeId
    attempt_id: SafeId
    job_id: SafeId
    source_artifact_fingerprint: Fingerprint
    image_id: SafeId
    file_name: Annotated[
        str,
        StringConstraints(pattern=r"^mr-lister-[a-f0-9]{24}-[a-f0-9]{16}\.png$"),
    ]
    width: int = Field(gt=0)
    height: int = Field(gt=0)
    size_bytes: int = Field(gt=0, le=PHASE6_MAX_SOURCE_ARTWORK_BYTES)
    mime_type: Literal["image/png"] = "image/png"
    fingerprint: Fingerprint
    confirmed_at: datetime


class UploadReconciliationObservationRecord(ControlModel):
    observation_id: SafeId
    job_id: SafeId
    work_request_id: SafeId
    attempt_id: SafeId
    outcome: ReconciliationOutcome
    observed_image_id: SafeId | None = None
    observed_at: datetime


class ProductVariantEvidence(ControlModel):
    variant_id: int = Field(gt=0)
    color: NonEmptyText
    size: NonEmptyText
    placement_group_id: SafeId
    retail_price_cents: int = Field(gt=0)
    production_cost_cents: int = Field(ge=0)


class ReconciliationObservationRecord(ControlModel):
    observation_id: SafeId
    job_id: SafeId
    work_request_id: SafeId
    attempt_id: SafeId
    outcome: ReconciliationOutcome
    observed_product_id: SafeId | None = None
    observed_payload_fingerprint: Fingerprint | None = None
    observed_at: datetime


class ProductSyncRecord(ControlModel):
    sync_id: SafeId
    job_id: SafeId
    review_version: int = Field(ge=1)
    product_id: SafeId
    image_id: SafeId
    printify_shop_id: StrictInt | None = Field(
        default=None,
        gt=0,
        exclude_if=lambda value: value is None,
    )
    payload_fingerprint: Fingerprint
    response_fingerprint: Fingerprint
    fingerprint: Fingerprint
    mockups: tuple[ProductMockupEvidence, ...] = ()
    variants: tuple[ProductVariantEvidence, ...] = Field(min_length=1)
    provider_locked: bool = False
    provider_published: bool = False
    synchronized_at: datetime

    @model_validator(mode="after")
    def variant_evidence_is_unique(self) -> ProductSyncRecord:
        variant_ids = tuple(variant.variant_id for variant in self.variants)
        if len(set(variant_ids)) != len(variant_ids):
            raise ValueError("Product synchronization variants must be unique")
        color_size_pairs = tuple((variant.color, variant.size) for variant in self.variants)
        if len(set(color_size_pairs)) != len(color_size_pairs):
            raise ValueError("Product synchronization color and size pairs must be unique")
        if len(self.mockups) > 20:
            raise ValueError("Product synchronization mockup evidence is outside its bound")
        mockup_urls = tuple(mockup.url for mockup in self.mockups)
        if len(set(mockup_urls)) != len(mockup_urls):
            raise ValueError("Product synchronization mockups must have unique URLs")
        variant_id_set = set(variant_ids)
        if any(not set(mockup.variant_ids).issubset(variant_id_set) for mockup in self.mockups):
            raise ValueError("Product synchronization mockups reference unknown variants")
        if self.provider_locked or self.provider_published:
            raise ValueError("Only editable unpublished draft evidence can synchronize")
        return self

    def representative_mockups(self, *, limit: int = 5) -> tuple[ProductMockupEvidence, ...]:
        """Select a deterministic bounded set maximizing explicit variant coverage."""

        if limit < 1 or limit > 5:
            raise ValueError("Representative mockup limit must be between one and five")
        remaining = list(self.mockups)
        uncovered = {variant.variant_id for variant in self.variants}
        selected: list[ProductMockupEvidence] = []
        while remaining and len(selected) < limit:
            chosen = min(
                remaining,
                key=lambda mockup: (
                    -len(uncovered.intersection(mockup.variant_ids)),
                    mockup.position != "front",
                    mockup.position or "",
                    mockup.url,
                ),
            )
            selected.append(chosen)
            uncovered.difference_update(chosen.variant_ids)
            remaining.remove(chosen)
        return tuple(selected)


class PricingSnapshot(ControlModel):
    snapshot_id: SafeId
    job_id: SafeId
    review_version: int = Field(ge=1)
    product_sync_fingerprint: Fingerprint
    fingerprint: Fingerprint
    currency: Literal["USD"] = "USD"
    fresh_until: datetime
    created_at: datetime

    @model_validator(mode="after")
    def expiry_follows_creation(self) -> PricingSnapshot:
        if self.fresh_until <= self.created_at:
            raise ValueError("Pricing freshness must end after snapshot creation")
        return self


class PricingEvidenceRecord(ControlModel):
    """Immutable, complete proceeds evidence paired one-to-one with a pricing snapshot."""

    snapshot_id: SafeId
    job_id: SafeId
    review_version: int = Field(ge=1)
    product_sync_fingerprint: Fingerprint
    fingerprint: Fingerprint
    estimate: EtsyUsStandardEstimate
    created_at: datetime

    @model_validator(mode="after")
    def estimate_matches_record_authority(self) -> PricingEvidenceRecord:
        if self.product_sync_fingerprint != self.estimate.product_sync_fingerprint:
            raise ValueError("Pricing evidence changed product synchronization authority")
        if self.fingerprint != self.estimate.fingerprint:
            raise ValueError("Pricing evidence fingerprint must cover the complete estimate")
        if self.created_at != self.estimate.calculated_at:
            raise ValueError("Pricing evidence creation time must match its calculation")
        return self


class ReviewDecisionRecord(ControlModel):
    decision_id: SafeId
    job_id: SafeId
    actor_owner_id: OwnerId
    decision: ReviewDecision
    review_version: int = Field(ge=1)
    review_fingerprint: Fingerprint
    approval_fingerprint: Fingerprint | None = None
    command_receipt_id: SafeId
    decided_at: datetime

    @model_validator(mode="after")
    def approval_fingerprint_matches_decision(self) -> ReviewDecisionRecord:
        if (self.decision is ReviewDecision.APPROVE) != (self.approval_fingerprint is not None):
            raise ValueError("Only approval decisions carry an approval fingerprint")
        return self


class CancellationDecisionRecord(ControlModel):
    decision_id: SafeId
    job_id: SafeId
    actor_owner_id: OwnerId
    expected_record_version: int = Field(ge=0)
    review_version: int | None = Field(default=None, ge=1)
    review_fingerprint: Fingerprint | None = None
    command_receipt_id: SafeId
    decided_at: datetime

    @model_validator(mode="after")
    def optional_review_reference_is_paired(self) -> CancellationDecisionRecord:
        if (self.review_version is None) != (self.review_fingerprint is None):
            raise ValueError("Cancellation review version and fingerprint are optional as a pair")
        return self


class FailureRecord(ControlModel):
    failure_id: SafeId
    job_id: SafeId
    work_request_id: SafeId
    stage: ControlJobState
    code: StableCode
    retryable: bool
    recovery_action: RecoveryAction | None = None
    resume_state: ControlJobState | None = None
    work_type: WorkType | None = None
    occurred_at: datetime

    @model_validator(mode="after")
    def retry_authority_is_all_or_none(self) -> FailureRecord:
        recovery = (self.recovery_action, self.resume_state, self.work_type)
        if self.retryable != all(value is not None for value in recovery):
            raise ValueError("Retryable failures require one complete recovery specification")
        if not self.retryable and any(value is not None for value in recovery):
            raise ValueError("Terminal failures cannot advertise recovery")
        if self.retryable and not self.recovery_binding_is_valid:
            raise ValueError("Retryable failure authority does not match its stage")
        return self

    @property
    def recovery_binding_is_valid(self) -> bool:
        if not self.retryable:
            return (
                self.recovery_action is None
                and self.resume_state is None
                and self.work_type is None
            )
        if self.recovery_action is None or self.resume_state is None or self.work_type is None:
            return False
        return (
            CONTROL_RECOVERY_BINDINGS.get(self.recovery_action)
            == (self.resume_state, self.work_type)
            and CONTROL_NEW_WORK_BY_STATE.get(self.stage) is self.work_type
        )


class CommandResponse(ControlModel):
    job_id: SafeId
    state: ControlJobState
    record_version: int = Field(ge=0)
    review_version: int = Field(ge=0)
    work_request_id: SafeId | None = None


class CommandReceipt(ControlModel):
    receipt_id: SafeId
    owner_id: OwnerId
    job_id: SafeId
    command_type: str = Field(min_length=1, max_length=64)
    idempotency_key_digest: Fingerprint
    request_fingerprint: Fingerprint
    response: CommandResponse
    work_request_id: SafeId | None = None
    created_at: datetime


class WorkRequest(ControlModel):
    work_request_id: SafeId
    owner_id: OwnerId
    job_id: SafeId
    receipt_id: SafeId
    work_type: WorkType
    review_version: int | None = Field(default=None, ge=1)
    input_fingerprint: Fingerprint
    execution_name: SafeId
    status: WorkRequestStatus = WorkRequestStatus.PENDING
    attempt_count: int = Field(default=0, ge=0)
    next_dispatch_at: datetime
    claim_id: SafeId | None = None
    lease_expires_at: datetime | None = None
    execution_arn: str | None = None
    last_error_code: StableCode | None = None
    created_at: datetime
    updated_at: datetime

    @model_validator(mode="after")
    def dispatch_fields_match_status(self) -> WorkRequest:
        if self.updated_at < self.created_at:
            raise ValueError("Work request updated_at cannot precede created_at")
        if self.status is not WorkRequestStatus.CLAIMED:
            if self.claim_id is not None or self.lease_expires_at is not None:
                raise ValueError("Only claimed work may retain a claim lease")
        if self.status is WorkRequestStatus.CLAIMED:
            if self.claim_id is None or self.lease_expires_at is None or self.attempt_count < 1:
                raise ValueError("Claimed work requires a lease and positive attempt count")
        if self.status is WorkRequestStatus.DISPATCHED and self.execution_arn is None:
            raise ValueError("Dispatched work requires its execution ARN")
        return self


class DomainEvent(ControlModel):
    job_id: SafeId
    sequence: int = Field(ge=1)
    name: StableCode
    occurred_at: datetime
    details: dict[str, str | int | bool | None] = Field(default_factory=dict)
