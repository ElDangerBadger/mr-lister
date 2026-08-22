"""Strict Phase 6 seller-control records and closed lifecycle rules."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import Field, StringConstraints, model_validator

from mr_lister.contracts import ContractModel

CONTROL_CONTRACT_VERSION = "2.0.0"
ControlContractVersion = Literal["2.0.0"]

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
            ControlJobState.AWAITING_APPROVAL,
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
            ControlJobState.CANCEL_REQUESTED,
            ControlJobState.CANCELLED,
            ControlJobState.FAILED_TERMINAL,
        }
    ),
    ControlJobState.FAILED_RETRYABLE: frozenset(
        {
            ControlJobState.ANALYZING_ARTWORK,
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
    RETRY_PRODUCT_SYNC = "retry_product_sync"
    RETRY_RECONCILIATION = "retry_reconciliation"
    RETRY_PRICING = "retry_pricing"


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
    product_id: SafeId | None = None
    product_sync_id: SafeId | None = None
    synchronized_review_version: int | None = Field(default=None, ge=1)
    product_sync_fingerprint: Fingerprint | None = None
    pricing_snapshot_id: SafeId | None = None
    pricing_snapshot_fingerprint: Fingerprint | None = None
    approved_review_version: int | None = Field(default=None, ge=1)
    approved_review_fingerprint: Fingerprint | None = None
    approval_fingerprint: Fingerprint | None = None
    active_work_request_id: SafeId | None = None
    failure_id: SafeId | None = None
    cancellation_requested_at: datetime | None = None
    provider_outcome_unconfirmed: bool = False
    created_at: datetime
    updated_at: datetime

    @model_validator(mode="after")
    def authority_fields_are_coherent(self) -> ControlJobRecord:
        if self.updated_at < self.created_at:
            raise ValueError("updated_at cannot precede created_at")
        if (self.review_version == 0) != (self.review_fingerprint is None):
            raise ValueError("Review version zero must have no review fingerprint")
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


class ProductSyncRecord(ControlModel):
    sync_id: SafeId
    job_id: SafeId
    review_version: int = Field(ge=1)
    product_id: SafeId
    payload_fingerprint: Fingerprint
    fingerprint: Fingerprint
    provider_locked: bool = False
    provider_published: bool = False
    synchronized_at: datetime


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
        return self


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
