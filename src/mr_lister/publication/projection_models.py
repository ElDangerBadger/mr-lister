"""Seller-safe, publication-disabled read models for the Phase 7 aggregate."""

from __future__ import annotations

import re
from enum import StrEnum
from typing import Literal

from pydantic import Field, StrictBool, StrictInt, model_validator

from mr_lister.publication.contract import PublicationState
from mr_lister.publication.execution_models import PublicationAttemptStatus
from mr_lister.publication.models import Fingerprint, PublicationModel, SafeId, UtcDateTime


class SellerPublicationStage(StrEnum):
    AWAITING_ACTIVATION = "awaiting_activation"
    QUEUED = "queued"
    PREFLIGHT = "preflight"
    PUBLISHING = "publishing"
    VERIFYING = "verifying"
    RECONCILING = "reconciling"
    COMPLETE = "complete"


class SellerPublicationProjection(PublicationModel):
    """Closed projection that deliberately grants no publication capability."""

    job_id: SafeId
    publication_enabled: Literal[False] = False
    request_enabled: Literal[False] = False
    request_disabled_reason: Literal["PUBLICATION_NOT_ACTIVATED"] = "PUBLICATION_NOT_ACTIVATED"
    state: Literal[
        "not_requested",
        PublicationState.PUBLICATION_REQUESTED,
        PublicationState.PUBLICATION_VERIFYING,
        PublicationState.PUBLICATION_RECONCILING,
        PublicationState.PUBLISHED,
        PublicationState.PUBLICATION_FAILED,
        PublicationState.PUBLICATION_OUTCOME_UNKNOWN,
    ]
    stage: SellerPublicationStage
    aggregate_record_version: StrictInt | None = Field(default=None, ge=0)
    attempt_status: PublicationAttemptStatus | None = None
    verification_deadline: UtcDateTime | None = None
    safe_listing_url: str | None = None
    verified_at: UtcDateTime | None = None
    report_id: SafeId | None = None
    terminal_at: UtcDateTime | None = None
    notification_available: StrictBool
    updated_at: UtcDateTime
    etag: Fingerprint

    @model_validator(mode="after")
    def state_has_exact_public_fields(self) -> SellerPublicationProjection:
        not_requested = self.state == "not_requested"
        terminal = self.state in {
            PublicationState.PUBLISHED,
            PublicationState.PUBLICATION_FAILED,
            PublicationState.PUBLICATION_OUTCOME_UNKNOWN,
        }
        published = self.state is PublicationState.PUBLISHED
        if not_requested:
            if (
                self.stage is not SellerPublicationStage.AWAITING_ACTIVATION
                or self.aggregate_record_version is not None
                or self.attempt_status is not None
                or self.verification_deadline is not None
                or self.report_id is not None
                or self.terminal_at is not None
                or self.safe_listing_url is not None
                or self.verified_at is not None
                or self.notification_available
            ):
                raise ValueError("An unrequested publication cannot expose aggregate authority")
            return self
        if (
            self.aggregate_record_version is None
            or self.attempt_status is None
            or self.verification_deadline is None
        ):
            raise ValueError("Requested publication projections require aggregate authority")
        expected_stages = {
            PublicationState.PUBLICATION_REQUESTED: {
                SellerPublicationStage.QUEUED,
                SellerPublicationStage.PREFLIGHT,
                SellerPublicationStage.PUBLISHING,
            },
            PublicationState.PUBLICATION_VERIFYING: {SellerPublicationStage.VERIFYING},
            PublicationState.PUBLICATION_RECONCILING: {SellerPublicationStage.RECONCILING},
            PublicationState.PUBLISHED: {SellerPublicationStage.COMPLETE},
            PublicationState.PUBLICATION_FAILED: {SellerPublicationStage.COMPLETE},
            PublicationState.PUBLICATION_OUTCOME_UNKNOWN: {SellerPublicationStage.COMPLETE},
        }
        if self.stage not in expected_stages[self.state]:
            raise ValueError("Publication projection stage differs from aggregate state")
        expected_attempt_status = (
            PublicationAttemptStatus.TERMINAL if terminal else PublicationAttemptStatus.OPEN
        )
        if self.attempt_status is not expected_attempt_status:
            raise ValueError("Publication projection attempt status differs from aggregate state")
        if terminal:
            if self.report_id is None or self.terminal_at is None:
                raise ValueError("Terminal publication projections require report authority")
            if self.updated_at != self.terminal_at:
                raise ValueError("Terminal publication projections must expose the winning time")
        elif self.report_id is not None or self.terminal_at is not None:
            raise ValueError("Nonterminal publication projections cannot expose report authority")
        if published:
            if (
                self.safe_listing_url is None
                or self.verified_at is None
                or not self.notification_available
            ):
                raise ValueError("Published projections require a complete verified result")
            assert self.verification_deadline is not None
            assert self.verified_at is not None and self.terminal_at is not None
            if (
                self.verified_at >= self.verification_deadline
                or self.terminal_at < self.verified_at
            ):
                raise ValueError("Published projection proof is outside its fixed time authority")
        elif (
            self.safe_listing_url is not None
            or self.verified_at is not None
            or self.notification_available
        ):
            raise ValueError("Only verified publication can expose a listing result")
        if (
            self.safe_listing_url is not None
            and re.fullmatch(
                r"https://www\.etsy\.com/listing/[1-9][0-9]{0,12}",
                self.safe_listing_url,
            )
            is None
        ):
            raise ValueError("Publication projection listing URL is not canonical")
        return self
