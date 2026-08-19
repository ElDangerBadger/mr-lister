"""FastAPI transport for the Phase 1 local vertical slice."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

from fastapi import FastAPI, File, Form, Header, UploadFile
from fastapi.responses import JSONResponse

from mr_lister.contracts import JobRecord, ReviewSnapshot
from mr_lister.workflow.errors import (
    IdempotencyConflictError,
    IntelligenceConfigurationError,
    IntelligenceUnavailableError,
    InvalidArtworkError,
    InvalidGeneratedOutputError,
    InvalidStateError,
    JobNotFoundError,
    ProfileNotFoundError,
    StaleApprovalError,
    WorkflowError,
)
from mr_lister.workflow.fakes import FakeIntelligenceAdapter, FakeProductionAdapter
from mr_lister.workflow.models import (
    ApprovalRequest,
    ListingRevisionRequest,
    RunReport,
)
from mr_lister.workflow.profiles import ProductProfileRepository
from mr_lister.workflow.service import ListingWorkflow
from mr_lister.workflow.store import InMemoryJobStore


def build_local_workflow(profile_directory: Path | None = None) -> ListingWorkflow:
    directory = profile_directory or Path.cwd() / "config" / "product_profiles"
    return ListingWorkflow(
        store=InMemoryJobStore(),
        profiles=ProductProfileRepository(directory),
        intelligence=FakeIntelligenceAdapter(),
        production=FakeProductionAdapter(),
    )


def create_app(workflow: ListingWorkflow | None = None) -> FastAPI:
    application = FastAPI(title="Mr Lister Local API", version="0.1.0")
    service = workflow or build_local_workflow()
    application.state.workflow = service

    @application.exception_handler(WorkflowError)
    async def workflow_error_handler(_request, error: WorkflowError) -> JSONResponse:
        status_code = 422
        if isinstance(error, JobNotFoundError):
            status_code = 404
        elif isinstance(error, (IdempotencyConflictError, InvalidStateError, StaleApprovalError)):
            status_code = 409
        elif isinstance(error, IntelligenceUnavailableError):
            status_code = 503
        elif isinstance(error, IntelligenceConfigurationError):
            status_code = 502
        elif isinstance(
            error,
            (InvalidArtworkError, InvalidGeneratedOutputError, ProfileNotFoundError),
        ):
            status_code = 422
        return JSONResponse(
            status_code=status_code,
            content={"error": {"code": error.code, "message": str(error)}},
        )

    @application.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok", "mode": "phase-1-fake"}

    @application.post("/jobs", response_model=JobRecord, status_code=201)
    async def submit_job(
        artwork: Annotated[UploadFile, File(description="One transparent PNG artwork file")],
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=1)],
        profile_id: Annotated[str, Form()] = "synthetic_gildan_5000",
    ) -> JobRecord:
        content = await artwork.read()
        return service.submit(
            filename=artwork.filename or "",
            content_type=artwork.content_type or "",
            content=content,
            idempotency_key=idempotency_key,
            profile_id=profile_id,
        )

    @application.get("/jobs/{job_id}", response_model=JobRecord)
    def get_job(job_id: str) -> JobRecord:
        return service.get_job(job_id)

    @application.get("/jobs/{job_id}/review", response_model=ReviewSnapshot)
    def get_review(job_id: str) -> ReviewSnapshot:
        return service.get_review(job_id)

    @application.put("/jobs/{job_id}/review/listing", response_model=ReviewSnapshot)
    def revise_listing(job_id: str, revision: ListingRevisionRequest) -> ReviewSnapshot:
        return service.revise_listing(job_id, revision)

    @application.post("/jobs/{job_id}/approve", response_model=JobRecord)
    def approve(job_id: str, approval: ApprovalRequest) -> JobRecord:
        return service.approve(job_id, approval.review_version)

    @application.post("/jobs/{job_id}/publish", response_model=JobRecord)
    def publish(job_id: str) -> JobRecord:
        return service.publish(job_id)

    @application.get("/jobs/{job_id}/report", response_model=RunReport)
    def get_report(job_id: str) -> RunReport:
        return service.get_report(job_id)

    return application


app = create_app()
