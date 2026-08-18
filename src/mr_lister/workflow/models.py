"""Phase 1 local workflow records."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import Field

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
