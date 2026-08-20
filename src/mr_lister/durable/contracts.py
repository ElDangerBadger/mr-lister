"""Strict envelopes carried by Step Functions and Phase 4 Lambda commands."""

from __future__ import annotations

from typing import Annotated

from pydantic import Field, StringConstraints

from mr_lister.contracts import ContractModel

JobId = Annotated[str, StringConstraints(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_-]+$")]


class JobCommand(ContractModel):
    job_id: JobId


class ApprovalWaitCommand(JobCommand):
    review_version: int = Field(ge=1)
    task_token: str = Field(min_length=1, repr=False)
    expires_in_seconds: int = Field(default=604_800, ge=60, le=604_800)


class ApprovalCommand(JobCommand):
    review_version: int = Field(ge=1)
