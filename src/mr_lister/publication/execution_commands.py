"""Provider-free application commands for Phase 7.2 execution transitions."""

from __future__ import annotations

from typing import Literal

from pydantic import Field, StrictInt

from mr_lister.publication.execution_models import (
    Fingerprint,
    OwnerId,
    PublicationCallPurpose,
    PublicationModel,
    PublicationProductReadEvidence,
    PublicationPublishEvidence,
    PublicationShopPreflightEvidence,
    SafeId,
)


class PublicationExecutionCommand(PublicationModel):
    owner_id: OwnerId
    aggregate_id: SafeId
    operation_id: SafeId
    expected_aggregate_record_version: StrictInt = Field(ge=0)
    expected_attempt_record_version: StrictInt = Field(ge=0)
    expected_permit_record_version: StrictInt = Field(ge=0, le=1)
    expected_work_record_version: StrictInt = Field(ge=0)


class DispatchPublicationWorkCommand(PublicationExecutionCommand):
    pass


class ReconstructPublicationAuthorityCommand(PublicationExecutionCommand):
    pass


class ClaimShopGetCommand(PublicationExecutionCommand):
    pass


class ClaimProductGetCommand(PublicationExecutionCommand):
    purpose: Literal[
        PublicationCallPurpose.PRODUCT_PREFLIGHT,
        PublicationCallPurpose.VERIFICATION,
        PublicationCallPurpose.RECONCILIATION,
    ]


class RecordPublicationPreflightCommand(PublicationExecutionCommand):
    shop_evidence: PublicationShopPreflightEvidence
    product_evidence: PublicationProductReadEvidence


class ClaimPublicationMutationCommand(PublicationExecutionCommand):
    preflight_proof_id: SafeId
    preflight_proof_fingerprint: Fingerprint


class RecordPublicationPostOutcomeCommand(PublicationExecutionCommand):
    evidence: PublicationPublishEvidence


class RecoverConsumedPublicationClaimCommand(PublicationExecutionCommand):
    mutation_claim_id: SafeId
    mutation_claim_fingerprint: Fingerprint
    recovery_category: Literal["consumed_claim_without_durable_boundary_observation"] = (
        "consumed_claim_without_durable_boundary_observation"
    )


class RecordPublicationProductObservationCommand(PublicationExecutionCommand):
    evidence: PublicationProductReadEvidence


class SettlePublicationDeadlineCommand(PublicationExecutionCommand):
    confirmation: Literal["fixed_deadline_elapsed"] = "fixed_deadline_elapsed"
