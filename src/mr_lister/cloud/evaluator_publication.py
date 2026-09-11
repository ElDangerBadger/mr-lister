"""Read-only evaluator policy, separate from the active Phase 7 publication contract.

This projection has no command authority and reads no provider or publication store.
The evaluator SAM overlay deploys its authenticated GET only; production does not.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from hashlib import sha256
from typing import Literal

from pydantic import field_validator

from mr_lister.cloud.browser_contracts import BrowserContractModel, Fingerprint, PublicId
from mr_lister.control.errors import NotFoundError
from mr_lister.control.models import ControlJobRecord
from mr_lister.control.projection import ReviewProjectionUnavailableError

EVALUATOR_PUBLICATION_ROUTE = "GET /v1/jobs/{job_id}/publication"
EVALUATOR_PUBLICATION_SETTING = "MR_LISTER_EVALUATOR_PUBLICATION_STATUS"
EVALUATOR_PUBLICATION_MESSAGE = (
    "Publishing is disabled in this evaluation workspace. You can review and approve drafts, "
    "but this workspace cannot publish Etsy listings."
)


class EvaluatorPublicationProjection(BrowserContractModel):
    contract_version: Literal["evaluator-publication-v1"] = "evaluator-publication-v1"
    job_id: PublicId
    publication_enabled: Literal[False] = False
    request_enabled: Literal[False] = False
    request_disabled_reason: Literal["EVALUATOR_PUBLICATION_DISABLED"] = (
        "EVALUATOR_PUBLICATION_DISABLED"
    )
    request_disabled_message: Literal[
        "Publishing is disabled in this evaluation workspace. You can review and approve drafts, "
        "but this workspace cannot publish Etsy listings."
    ] = EVALUATOR_PUBLICATION_MESSAGE
    state: Literal["not_requested"] = "not_requested"
    stage: Literal["awaiting_activation"] = "awaiting_activation"
    aggregate_record_version: None = None
    attempt_status: None = None
    verification_deadline: None = None
    safe_listing_url: None = None
    verified_at: None = None
    report_id: None = None
    terminal_at: None = None
    notification_available: Literal[False] = False
    updated_at: datetime
    etag: Fingerprint

    @field_validator(
        "publication_enabled", "request_enabled", "notification_available", mode="before"
    )
    @classmethod
    def disabled_booleans_are_exact(cls, value: object) -> object:
        if value is not False:
            raise ValueError("Evaluator publication authority is always false")
        return value

    @field_validator("updated_at")
    @classmethod
    def time_is_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Evaluator status requires an aware job timestamp")
        return value.astimezone(UTC)


def evaluator_publication_projection(
    job: ControlJobRecord, *, owner_id: str, job_id: str
) -> EvaluatorPublicationProjection:
    """Project only an existing owned job with no publication history or authority."""

    if not isinstance(job, ControlJobRecord):
        raise ReviewProjectionUnavailableError
    if job.owner_id != owner_id or job.job_id != job_id:
        raise NotFoundError
    try:
        exact = ControlJobRecord.model_validate(job.model_dump(mode="python"))
        if exact.publication_aggregate_id is not None:
            raise ValueError("Evaluator cannot replace an existing publication status")
        if exact.updated_at.tzinfo is None or exact.updated_at.utcoffset() is None:
            raise ValueError("A timezone-aware job timestamp is required")
        updated_at = exact.updated_at.astimezone(UTC)
        projection = EvaluatorPublicationProjection(
            job_id=job_id, updated_at=updated_at, etag="0" * 64
        )
        authority = {
            "projection": projection.model_dump(mode="json", exclude={"etag"}),
            "owner_id": owner_id,
            "record_version": exact.record_version,
        }
        fingerprint = sha256(
            json.dumps(authority, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        return EvaluatorPublicationProjection(
            **projection.model_dump(exclude={"etag"}), etag=fingerprint
        )
    except Exception:
        raise ReviewProjectionUnavailableError from None
