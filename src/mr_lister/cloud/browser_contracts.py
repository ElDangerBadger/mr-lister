"""Versioned, strict public contracts for the Phase 6.5 browser boundary.

This module is deliberately independent of a JavaScript toolchain.  It is the Python authority
that API adapters validate before serialization and that a TypeScript client can consume as JSON
Schema plus deterministic, credential-free golden fixtures.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator
from pydantic.json_schema import JsonSchemaMode, models_json_schema

from mr_lister.control.models import (
    CONTROL_CONTRACT_VERSION,
    PHASE6_MAX_SOURCE_ARTWORK_BYTES,
    ControlJobState,
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
    MockupSetProjection,
    PlacementPresentation,
    ProductPolicyProjection,
    ProductSynchronizationProjection,
    ReviewDisplayState,
    ReviewStage,
    SectionReadiness,
    SellerAction,
    SellerActionCapability,
    SellerReviewProjection,
    StrandsProvenanceProjection,
)
from mr_lister.control.upload_models import UploadIntentStatus

BROWSER_CONTRACT_VERSION = "6.5.0"

Fingerprint = Annotated[str, StringConstraints(pattern=r"^[a-f0-9]{64}$")]
PublicId = Annotated[
    str,
    StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$"),
]
Filename = Annotated[str, StringConstraints(min_length=1, max_length=255)]
Title = Annotated[str, StringConstraints(min_length=1, max_length=140)]
Description = Annotated[str, StringConstraints(min_length=1, max_length=100_000)]
Tag = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=20)]


class BrowserContractModel(BaseModel):
    """Strict immutable data allowed to cross the seller browser boundary."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class CreateUploadRequest(BrowserContractModel):
    filename: Filename = Field(description="PNG basename without path or control characters")
    content_type: Literal["image/png"]
    content_sha256: Fingerprint
    size_bytes: int = Field(gt=0, le=PHASE6_MAX_SOURCE_ARTWORK_BYTES)

    @field_validator("filename")
    @classmethod
    def filename_is_a_png_basename(cls, value: str) -> str:
        if (
            value != value.strip()
            or not value.casefold().endswith(".png")
            or "/" in value
            or "\\" in value
            or any(ord(character) < 32 or ord(character) == 127 for character in value)
        ):
            raise ValueError("filename is outside the public upload contract")
        return value


class ListingRequest(BrowserContractModel):
    title: Title
    description: Description
    tags: list[Tag] = Field(min_length=13, max_length=13)


class ReviewAuthorityRequest(BrowserContractModel):
    expected_record_version: int = Field(ge=0)
    expected_review_version: int = Field(ge=1)
    expected_review_fingerprint: Fingerprint


class ReviseListingRequest(ReviewAuthorityRequest):
    listing: ListingRequest


class RecordAuthorityRequest(BrowserContractModel):
    expected_record_version: int = Field(ge=0)


class UploadCommandProjection(BrowserContractModel):
    upload_id: PublicId
    job_id: PublicId
    status: UploadIntentStatus
    record_version: int = Field(ge=0)


class UploadAuthorizationProjection(BrowserContractModel):
    upload_id: PublicId
    job_id: PublicId
    authorization_generation: int = Field(ge=1)
    method: Literal["POST"] = "POST"
    url: str = Field(min_length=1, max_length=2_048)
    form_fields: dict[str, str] = Field(min_length=1, max_length=64)
    content_sha256: Fingerprint
    size_bytes: int = Field(gt=0, le=PHASE6_MAX_SOURCE_ARTWORK_BYTES)
    issued_at: datetime
    expires_at: datetime


class UploadMutationResponse(BrowserContractModel):
    upload: UploadCommandProjection
    authorization: UploadAuthorizationProjection | None = None


class UploadRecoveryProjection(BrowserContractModel):
    """Durable upload status without object authority or short-lived S3 credentials."""

    upload_id: PublicId
    job_id: PublicId
    status: UploadIntentStatus
    filename: Filename
    content_type: Literal["image/png"]
    size_bytes: int = Field(gt=0, le=PHASE6_MAX_SOURCE_ARTWORK_BYTES)
    record_version: int = Field(ge=0)
    authorization_expires_at: datetime | None = None
    intent_expires_at: datetime
    completed_at: datetime | None = None
    cancelled_at: datetime | None = None
    expired_at: datetime | None = None
    created_at: datetime
    updated_at: datetime


class JobSummaryProjection(BrowserContractModel):
    job_id: PublicId
    state: ControlJobState
    record_version: int = Field(ge=0)
    review_version: int = Field(ge=0)
    created_at: datetime
    updated_at: datetime


class JobPageProjection(BrowserContractModel):
    jobs: tuple[JobSummaryProjection, ...] = Field(max_length=100)
    next_cursor: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_-]{1,200}$")


class JobProgressProjection(BrowserContractModel):
    """Server-derived seller progress; canonical machine state is intentionally absent."""

    contract_version: Literal["2.0.0"] = CONTROL_CONTRACT_VERSION
    job_id: PublicId
    record_version: int = Field(ge=0)
    review_version: int = Field(ge=0)
    display_state: ReviewDisplayState
    stage: ReviewStage
    authority_notice: Literal["Unpublished — not on Etsy"] = "Unpublished — not on Etsy"
    actions: tuple[SellerActionCapability, ...] = Field(min_length=5, max_length=5)
    failure: FailureProjection | None = None
    provider_outcome_unconfirmed: bool = False
    created_at: datetime
    updated_at: datetime


class SellerCommandResponse(BrowserContractModel):
    job_id: PublicId
    state: ControlJobState
    record_version: int = Field(ge=0)
    review_version: int = Field(ge=0)


class RequestFieldError(BrowserContractModel):
    path: str = Field(pattern=r"^\$(?:\.[a-z_][a-z0-9_]{0,63}|\[[0-9]{1,3}\])*$", max_length=160)
    code: Literal[
        "REQUIRED",
        "UNEXPECTED_FIELD",
        "INVALID_TYPE",
        "INVALID_LENGTH",
        "INVALID_FORMAT",
        "OUT_OF_RANGE",
        "INVALID_VALUE",
    ]
    message: str = Field(min_length=1, max_length=100)


class ErrorDetail(BrowserContractModel):
    code: str = Field(pattern=r"^[A-Z][A-Z0-9_]{0,99}$")
    message: str = Field(min_length=1, max_length=300)
    request_id: str = Field(min_length=1, max_length=128)
    fields: tuple[RequestFieldError, ...] | None = Field(default=None, max_length=25)


class ErrorEnvelope(BrowserContractModel):
    error: ErrorDetail


class HealthResponse(BrowserContractModel):
    status: Literal["ok"] = "ok"


_SCHEMA_MODELS: tuple[tuple[type[BaseModel], JsonSchemaMode], ...] = (
    (CreateUploadRequest, "validation"),
    (ReviseListingRequest, "validation"),
    (ReviewAuthorityRequest, "validation"),
    (RecordAuthorityRequest, "validation"),
    (UploadMutationResponse, "serialization"),
    (UploadRecoveryProjection, "serialization"),
    (JobPageProjection, "serialization"),
    (JobProgressProjection, "serialization"),
    (SellerReviewProjection, "serialization"),
    (SellerCommandResponse, "serialization"),
    (ErrorEnvelope, "serialization"),
    (HealthResponse, "serialization"),
)


def browser_contract_schema() -> dict[str, Any]:
    """Return one deterministic JSON Schema document with route-to-model references."""

    schema_map, definitions = models_json_schema(
        _SCHEMA_MODELS,
        title="Mr. Lister Phase 6.5 browser contract",
        ref_template="#/$defs/{model}",
    )
    references = {
        model.__name__: schema_map[(model, mode)]["$ref"] for model, mode in _SCHEMA_MODELS
    }
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": "https://mr-lister.invalid/contracts/browser-6.5.0.schema.json",
        "title": definitions["title"],
        "$defs": definitions.get("$defs", {}),
        "x-mr-lister-contract-version": BROWSER_CONTRACT_VERSION,
        "x-mr-lister-models": references,
        "x-mr-lister-routes": {
            "GET /health": {"response": references["HealthResponse"]},
            "POST /v1/uploads": {
                "request": references["CreateUploadRequest"],
                "response": references["UploadMutationResponse"],
            },
            "GET /v1/uploads/{upload_id}": {
                "response": references["UploadRecoveryProjection"],
            },
            "POST /v1/uploads/{upload_id}/authorize": {
                "response": references["UploadMutationResponse"],
            },
            "POST /v1/uploads/{upload_id}/complete": {
                "response": references["UploadMutationResponse"],
            },
            "POST /v1/uploads/{upload_id}/cancel": {
                "response": references["UploadMutationResponse"],
            },
            "GET /v1/jobs": {"response": references["JobPageProjection"]},
            "GET /v1/jobs/{job_id}": {
                "response": references["JobProgressProjection"],
            },
            "GET /v1/jobs/{job_id}/review": {
                "response": references["SellerReviewProjection"],
            },
            "PUT /v1/jobs/{job_id}/review/listing": {
                "request": references["ReviseListingRequest"],
                "response": references["SellerCommandResponse"],
            },
            "POST /v1/jobs/{job_id}/economics/refresh": {
                "request": references["ReviewAuthorityRequest"],
                "response": references["SellerCommandResponse"],
            },
            "POST /v1/jobs/{job_id}/approve": {
                "request": references["ReviewAuthorityRequest"],
                "response": references["SellerCommandResponse"],
            },
            "POST /v1/jobs/{job_id}/cancel": {
                "request": references["RecordAuthorityRequest"],
                "response": references["SellerCommandResponse"],
            },
            "POST /v1/jobs/{job_id}/retry": {
                "request": references["RecordAuthorityRequest"],
                "response": references["SellerCommandResponse"],
            },
            "GET /v1/jobs/{job_id}/artwork-preview": {
                "response-kind": "bodyless-redirect",
            },
            "*": {"error": references["ErrorEnvelope"]},
        },
    }


def browser_contract_fixtures() -> dict[str, Any]:
    """Return deterministic JSON values for client decoder and rendering contract tests."""

    now = datetime(2026, 8, 22, 12, 0, tzinfo=UTC)
    pending = SectionReadiness.PENDING
    actions = tuple(
        SellerActionCapability(
            action=action,
            enabled=False,
            reason=ActionReason.NOT_IN_CURRENT_STATE,
            message="This action is not available yet.",
        )
        for action in SellerAction
    )
    review = SellerReviewProjection(
        job_id="job_browser_fixture",
        record_version=1,
        review_version=0,
        display_state=ReviewDisplayState.PREPARING,
        stage=ReviewStage.ARTWORK_REVIEW,
        actions=actions,
        preview=ArtworkPreview(readiness=pending),
        artwork=ArtworkInterpretation(readiness=pending),
        listing=ListingProjection(readiness=pending),
        validation=ListingValidationProjection(readiness=pending),
        product_policy=ProductPolicyProjection(
            product_name="Gildan 64000",
            provider_name="SwiftPOD",
            colors=("Black",),
            sizes=("S",),
            placements=(
                PlacementPresentation(
                    group_id="placement_small",
                    sizes=("S",),
                    position="Centered below collar",
                    decoration_method="Direct to garment",
                    x=0.5,
                    y=0,
                    scale=0.65,
                    angle=0,
                ),
            ),
            retail_price_cents=2_999,
            buyer_shipping_cents=0,
        ),
        synchronization=ProductSynchronizationProjection(readiness=pending),
        mockups=MockupSetProjection(readiness=pending),
        economics=EconomicsProjection(readiness=EconomicsReadiness.MISSING),
        strands=StrandsProvenanceProjection(readiness=pending),
        created_at=now - timedelta(minutes=1),
        updated_at=now,
    )
    progress = JobProgressProjection(
        job_id=review.job_id,
        record_version=review.record_version,
        review_version=review.review_version,
        display_state=review.display_state,
        stage=review.stage,
        actions=review.actions,
        failure=review.failure,
        provider_outcome_unconfirmed=review.provider_outcome_unconfirmed,
        created_at=review.created_at,
        updated_at=review.updated_at,
    )
    recovery = UploadRecoveryProjection(
        upload_id="upload_browser_fixture",
        job_id=review.job_id,
        status=UploadIntentStatus.OPEN,
        filename="artwork.png",
        content_type="image/png",
        size_bytes=1_024,
        record_version=0,
        authorization_expires_at=now + timedelta(minutes=5),
        intent_expires_at=now + timedelta(days=1),
        created_at=now,
        updated_at=now,
    )
    validation_error = ErrorEnvelope(
        error=ErrorDetail(
            code="VALIDATION_FAILED",
            message="One or more request fields are not valid.",
            request_id="request-browser-fixture",
            fields=(
                RequestFieldError(
                    path="$.listing.tags",
                    code="INVALID_LENGTH",
                    message="Use the required number of items.",
                ),
            ),
        )
    )
    fixtures = {
        "upload_recovery": recovery.model_dump(mode="json"),
        "job_progress": progress.model_dump(mode="json"),
        "seller_review_pending": review.model_dump(mode="json"),
        "validation_error": validation_error.model_dump(mode="json", exclude_none=True),
    }
    # A JSON round trip proves the fixture surface contains no Python-only values and returns a
    # fresh structure to callers that may feed it into code generators.
    return json.loads(json.dumps(fixtures, sort_keys=True, separators=(",", ":")))


__all__ = [
    "BROWSER_CONTRACT_VERSION",
    "BrowserContractModel",
    "CreateUploadRequest",
    "ErrorDetail",
    "ErrorEnvelope",
    "HealthResponse",
    "JobPageProjection",
    "JobProgressProjection",
    "JobSummaryProjection",
    "ListingRequest",
    "RecordAuthorityRequest",
    "RequestFieldError",
    "ReviewAuthorityRequest",
    "ReviseListingRequest",
    "SellerCommandResponse",
    "UploadAuthorizationProjection",
    "UploadCommandProjection",
    "UploadMutationResponse",
    "UploadRecoveryProjection",
    "browser_contract_fixtures",
    "browser_contract_schema",
]
