"""Phase 1 local workflow records."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import Field, model_validator

from mr_lister.contracts import ContractModel, JobRecord, ReviewSnapshot


class ArtworkInput(ContractModel):
    filename: str = Field(min_length=1, max_length=255)
    content_type: Literal["image/png"]
    content_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    size_bytes: int = Field(gt=0, le=25 * 1024 * 1024)


class WorkflowEvent(ContractModel):
    sequence: int = Field(ge=1)
    occurred_at: datetime
    name: str = Field(min_length=1, max_length=100)
    details: dict[str, Any] = Field(default_factory=dict)


class ExternalWriteRecord(ContractModel):
    operation: Literal["sync_product_draft", "publish_listing"]
    idempotency_key: str = Field(min_length=1)
    request_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    external_id: str = Field(min_length=1)
    occurred_at: datetime


class ExternalWriteStatus(StrEnum):
    CLAIMED = "claimed"
    COMPLETED = "completed"
    RECONCILIATION_REQUIRED = "reconciliation_required"


class ExternalWriteClaim(ContractModel):
    operation: Literal["sync_product_draft", "publish_listing"]
    idempotency_key: str = Field(min_length=1)
    request_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    status: ExternalWriteStatus
    claimed_at: datetime
    result: dict[str, str] | None = None
    completed_at: datetime | None = None

    @model_validator(mode="after")
    def completion_fields_match_status(self) -> ExternalWriteClaim:
        is_completed = self.status is ExternalWriteStatus.COMPLETED
        if is_completed != (self.result is not None and self.completed_at is not None):
            raise ValueError("Completed writes require both result and completed_at")
        if self.result is not None and not self.result.get("external_id"):
            raise ValueError("Completed write result requires external_id")
        return self


class ApprovalWaitStatus(StrEnum):
    PENDING = "pending"
    CONSUMED = "consumed"


class ApprovalWaitRecord(ContractModel):
    job_id: str = Field(min_length=1)
    review_version: int = Field(ge=1)
    task_token: str = Field(min_length=1, repr=False)
    status: ApprovalWaitStatus
    created_at: datetime
    expires_at: datetime
    consumed_at: datetime | None = None

    @model_validator(mode="after")
    def wait_times_match_status(self) -> ApprovalWaitRecord:
        if self.expires_at <= self.created_at:
            raise ValueError("Approval wait must expire after it is created")
        if (self.status is ApprovalWaitStatus.CONSUMED) != (self.consumed_at is not None):
            raise ValueError("Consumed approval waits require consumed_at")
        return self


class RunReport(ContractModel):
    job: JobRecord
    artwork: ArtworkInput
    review: ReviewSnapshot
    external_writes: tuple[ExternalWriteRecord, ...]
    events: tuple[WorkflowEvent, ...]


class ApprovalRequest(ContractModel):
    review_version: int = Field(ge=1)


class ListingRevisionRequest(ContractModel):
    title: str = Field(min_length=1, max_length=140)
    description: str = Field(min_length=1)
    tags: tuple[str, ...] = Field(min_length=13, max_length=13)
    audience: tuple[str, ...] = ()
    title_rationale: str = Field(min_length=1)
    tag_rationale: str = Field(min_length=1)
