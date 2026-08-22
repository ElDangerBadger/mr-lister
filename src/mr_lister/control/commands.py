"""Strict command envelopes for the Phase 6 application-owned control layer."""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated

from pydantic import Field, StringConstraints

from mr_lister.control.models import ControlModel

SafeId = Annotated[
    str,
    StringConstraints(strip_whitespace=True, pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,127}$"),
]
Fingerprint = Annotated[str, StringConstraints(pattern=r"^[a-f0-9]{64}$")]
OwnerId = Annotated[str, StringConstraints(pattern=r"^[a-f0-9]{64}$")]


class CommandType(StrEnum):
    REVISE_LISTING = "revise_listing"
    APPROVE_REVIEW = "approve_review"
    CANCEL_JOB = "cancel_job"
    RETRY_JOB = "retry_job"


class SellerCommand(ControlModel):
    job_id: SafeId
    owner_id: OwnerId
    expected_record_version: int = Field(ge=0)
    idempotency_key: str = Field(min_length=1, max_length=256, repr=False)


class ReviewSensitiveCommand(SellerCommand):
    expected_review_version: int = Field(ge=1)
    expected_review_fingerprint: Fingerprint
    expected_review_etag: Fingerprint


class ListingRevision(ControlModel):
    """The fields editable by a Phase 6 seller.

    Audience and model rationales remain immutable carry-forward metadata from the prior review.
    """

    title: str = Field(min_length=1, max_length=140)
    description: str = Field(min_length=1, max_length=100_000)
    tags: tuple[str, ...] = Field(min_length=13, max_length=13)


class ReviseListingCommand(ReviewSensitiveCommand):
    revision: ListingRevision


class ApproveReviewCommand(ReviewSensitiveCommand):
    pass


class CancelJobCommand(SellerCommand):
    """Cancellation deliberately has no mandatory review reference."""


class RetryJobCommand(SellerCommand):
    """The application, not the caller, selects the persisted recovery action."""


class WorkerFailureCode(StrEnum):
    INTELLIGENCE_UNAVAILABLE = "INTELLIGENCE_UNAVAILABLE"
    INTELLIGENCE_CONFIGURATION = "INTELLIGENCE_CONFIGURATION"
    INVALID_GENERATED_OUTPUT = "INVALID_GENERATED_OUTPUT"
    ARTIFACT_INTEGRITY = "ARTIFACT_INTEGRITY"
    PRODUCTION_UNAVAILABLE = "PRODUCTION_UNAVAILABLE"
    PRODUCTION_CONFIGURATION = "PRODUCTION_CONFIGURATION"
    PRODUCTION_INPUT = "PRODUCTION_INPUT"
    ECONOMICS_UNAVAILABLE = "ECONOMICS_UNAVAILABLE"
    PRODUCT_CREATE_OUTCOME_UNKNOWN = "PRODUCT_CREATE_OUTCOME_UNKNOWN"
    CONNECTION_REVOKED = "CONNECTION_REVOKED"
    UNCLASSIFIED_FAILURE = "UNCLASSIFIED_FAILURE"


class RecordWorkerFailureCommand(ControlModel):
    """Trusted internal failure signal; it carries no state or recovery choice."""

    job_id: SafeId
    work_request_id: SafeId
    expected_record_version: int = Field(ge=0)
    code: WorkerFailureCode


class SettleCancellationCommand(ControlModel):
    """Settle late work without permitting a normal success transition."""

    job_id: SafeId
    work_request_id: SafeId
    expected_record_version: int = Field(ge=0)
    provider_outcome_known: bool
