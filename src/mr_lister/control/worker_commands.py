"""Strict trusted-worker success envelopes for Phase 6.2.

These commands carry observations, never caller-selected lifecycle states.  The
application service derives every transition and persists the corresponding
immutable evidence in the same transaction.
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field, model_validator

from mr_lister.agent.contracts import PreparationDecision
from mr_lister.contracts import ArtworkAnalysis, ListingIntelligence
from mr_lister.control.economics import EtsyUsStandardEstimate
from mr_lister.control.models import (
    PHASE6_MAX_SOURCE_ARTWORK_BYTES,
    AgentToolName,
    ControlModel,
    Fingerprint,
    ProductMockupEvidence,
    ProductVariantEvidence,
    ReconciliationOutcome,
    SafeId,
)


class WorkerCommand(ControlModel):
    job_id: SafeId
    work_request_id: SafeId
    expected_record_version: int = Field(ge=0)


class BeginPreparationCommand(WorkerCommand):
    """Checkpoint application-owned entry into artwork analysis."""


class RecordPreparedReviewCommand(WorkerCommand):
    source_artifact_fingerprint: Fingerprint
    artwork_analysis: ArtworkAnalysis
    listing: ListingIntelligence
    product_profile_fingerprint: Fingerprint


class CompletePreparationWithAgentDecisionCommand(WorkerCommand):
    correlation_id: str = Field(pattern=r"^[a-f0-9]{24}$")
    controller_model_id: str = Field(min_length=1, max_length=300)
    tool_calls: tuple[AgentToolName, ...] = Field(min_length=1)
    cycles: int = Field(ge=1, le=4)
    input_tokens: int = Field(ge=0, le=12_000)
    output_tokens: int = Field(ge=0, le=2_500)
    total_tokens: int = Field(ge=0, le=12_000)
    decision: PreparationDecision
    requires_human_approval: Literal[True] = True
    publication_authorized: Literal[False] = False

    @model_validator(mode="after")
    def metrics_are_coherent(self) -> CompletePreparationWithAgentDecisionCommand:
        if self.total_tokens != self.input_tokens + self.output_tokens:
            raise ValueError("Agent token total must equal input plus output")
        if len(set(self.tool_calls)) != len(self.tool_calls):
            raise ValueError("Agent tool evidence cannot repeat tools")
        if self.tool_calls != ("record_prepared_review",):
            raise ValueError("Phase 6 preparation requires its exact Strands tool")
        return self


class BeginProviderWriteCommand(WorkerCommand):
    image_id: SafeId
    target_payload_fingerprint: Fingerprint
    correlation_token: str = Field(pattern=r"^ml-[a-f0-9]{24}$")


class BeginProviderUploadCommand(WorkerCommand):
    source_artifact_fingerprint: Fingerprint
    file_name: str = Field(pattern=r"^mr-lister-[a-f0-9]{24}-[a-f0-9]{16}\.png$")


class UploadedArtworkObservation(ControlModel):
    image_id: SafeId
    file_name: str = Field(pattern=r"^mr-lister-[a-f0-9]{24}-[a-f0-9]{16}\.png$")
    width: int = Field(gt=0)
    height: int = Field(gt=0)
    size_bytes: int = Field(gt=0, le=PHASE6_MAX_SOURCE_ARTWORK_BYTES)
    mime_type: Literal["image/png"] = "image/png"


class RecordProviderUploadSuccessCommand(WorkerCommand):
    attempt_id: SafeId
    observation: UploadedArtworkObservation


class RecordProviderUploadOutcomeUnknownCommand(WorkerCommand):
    attempt_id: SafeId
    code: Literal[
        "PROVIDER_TIMEOUT",
        "PROVIDER_CONNECTION_LOST",
        "PROVIDER_RESPONSE_INVALID",
    ]


class RecordUploadReconciliationObservationCommand(WorkerCommand):
    attempt_id: SafeId
    outcome: ReconciliationOutcome
    upload: UploadedArtworkObservation | None = None

    @model_validator(mode="after")
    def upload_proof_matches_outcome(self) -> RecordUploadReconciliationObservationCommand:
        if self.outcome not in {
            ReconciliationOutcome.TARGET_MATCH,
            ReconciliationOutcome.NO_MATCH,
            ReconciliationOutcome.MULTIPLE_MATCHES,
            ReconciliationOutcome.CONFLICT,
            ReconciliationOutcome.UNAVAILABLE,
        }:
            raise ValueError("Upload reconciliation outcome is outside its closed contract")
        if (self.outcome is ReconciliationOutcome.TARGET_MATCH) != (self.upload is not None):
            raise ValueError("Only an upload target match carries complete image evidence")
        return self


class ProductSyncObservation(ControlModel):
    product_id: SafeId
    image_id: SafeId
    request_fingerprint: Fingerprint
    response_fingerprint: Fingerprint
    mockups: tuple[ProductMockupEvidence, ...] = ()
    variants: tuple[ProductVariantEvidence, ...] = Field(min_length=1)
    provider_locked: Literal[False] = False
    provider_published: Literal[False] = False

    @model_validator(mode="after")
    def projection_evidence_is_bounded(self) -> ProductSyncObservation:
        if len(self.mockups) > 20:
            raise ValueError("Provider mockup evidence is outside its bounded contract")
        mockup_urls = tuple(mockup.url for mockup in self.mockups)
        if len(set(mockup_urls)) != len(mockup_urls):
            raise ValueError("Provider mockup evidence must have unique URLs")
        variant_ids = tuple(variant.variant_id for variant in self.variants)
        if len(set(variant_ids)) != len(variant_ids):
            raise ValueError("Provider variant evidence must be unique")
        if any(not set(mockup.variant_ids).issubset(set(variant_ids)) for mockup in self.mockups):
            raise ValueError("Provider mockup evidence references unknown variants")
        return self


class RecordProductSyncSuccessCommand(WorkerCommand):
    attempt_id: SafeId
    observation: ProductSyncObservation


class RecordPricingSuccessCommand(WorkerCommand):
    """Complete read-only economics evidence; lifecycle state remains application-owned."""

    estimate: EtsyUsStandardEstimate


class RecordProductWriteOutcomeUnknownCommand(WorkerCommand):
    attempt_id: SafeId
    code: Literal[
        "PROVIDER_TIMEOUT",
        "PROVIDER_CONNECTION_LOST",
        "PROVIDER_RESPONSE_INVALID",
    ]


class RecordReconciliationObservationCommand(WorkerCommand):
    attempt_id: SafeId
    outcome: ReconciliationOutcome
    product: ProductSyncObservation | None = None
    observed_payload_fingerprint: Fingerprint | None = None

    @model_validator(mode="after")
    def product_proof_matches_outcome(self) -> RecordReconciliationObservationCommand:
        target_match = self.outcome is ReconciliationOutcome.TARGET_MATCH
        prior_match = self.outcome is ReconciliationOutcome.PRIOR_MATCH
        if target_match != (self.product is not None):
            raise ValueError("Only a target match carries complete product evidence")
        if prior_match != (self.observed_payload_fingerprint is not None):
            raise ValueError("Only a prior match carries the exact observed prior fingerprint")
        return self
