"""Sanitized application boundary for the private Phase 7 approval guard.

The guard runtime is an internal, read-only deployment attestor.  It can report that its sealed
configuration loaded, or ask the existing capability-free pre-call guard to re-read one exact
publication authority.  It never returns an owner, job, aggregate, provider identifier, durable
payload, or free-form failure detail.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import StrEnum
from hashlib import sha256
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from mr_lister.control.fingerprints import (
    canonical_fingerprint as control_fingerprint,
)
from mr_lister.control.fingerprints import (
    product_sync_record_fingerprint,
    review_etag,
)
from mr_lister.control.models import (
    ControlJobRecord,
    ControlJobState,
    PricingEvidenceRecord,
    PricingSnapshot,
    ProductSyncRecord,
    ReviewContent,
    ReviewDecision,
    ReviewDecisionRecord,
    SourceArtifactRecord,
)
from mr_lister.publication.contract import (
    PHASE7_PUBLICATION_CONTRACT_VERSION,
    PublicationActivationPhaseName,
    PublicationState,
    phase7_publication_contract,
    phase7_publication_contract_digest,
)
from mr_lister.publication.execution_models import PublicationExecutionAuthority
from mr_lister.publication.models import Fingerprint, OwnerId, SafeId
from mr_lister.publication.profile_eligibility import (
    PublicationProfileEligibilityAuthority,
    require_exact_publication_profile_eligibility,
)
from mr_lister.review_profile import ExactReviewProductProfile


class PublicationPreCallAuthorityError(RuntimeError):
    """Current durable authority no longer matches the immutable publication snapshot."""


@dataclass(frozen=True, slots=True)
class PublicationGuardSourceAuthority:
    """Exact Phase 6 records read by this guard, without importing a write-capable store."""

    current_job: ControlJobRecord
    review: ReviewContent
    approval_decision: ReviewDecisionRecord
    source: SourceArtifactRecord
    product_sync: ProductSyncRecord
    pricing_snapshot: PricingSnapshot
    pricing_evidence: PricingEvidenceRecord


class PublicationExecutionSourceStore(Protocol):
    def load_execution_authority(
        self,
        owner_id: str,
        aggregate_id: str,
    ) -> PublicationExecutionAuthority: ...

    def load_source_authority(
        self,
        owner_id: str,
        aggregate_id: str,
    ) -> PublicationGuardSourceAuthority: ...


class PublicationProfileAuthority(Protocol):
    def get_exact(
        self,
        *,
        profile_id: str,
        profile_version: int,
    ) -> ExactReviewProductProfile: ...


def _review_content_fingerprint(review: ReviewContent) -> str:
    return control_fingerprint(
        {
            "contract_version": review.contract_version,
            "job_id": review.job_id,
            "review_version": review.review_version,
            "actor": review.actor.value,
            "title": review.title,
            "description": review.description,
            "tags": review.tags,
            "audience": review.audience,
            "title_rationale": review.title_rationale,
            "tag_rationale": review.tag_rationale,
            "validation_passed": review.validation_passed,
            "validation_issue_codes": review.validation_issue_codes,
            "artwork_analysis_fingerprint": review.artwork_analysis_fingerprint,
            "product_profile_fingerprint": review.product_profile_fingerprint,
            "created_at": review.created_at.isoformat(),
        }
    )


def _source_artifact_fingerprint(source: SourceArtifactRecord) -> str:
    if source.created_at.utcoffset() is None or source.version_id == "null":
        raise ValueError("Pinned source artifact authority is invalid")
    if (source.width is None) != (source.height is None):
        raise ValueError("Pinned source artifact authority is invalid")
    material: dict[str, object] = {
        "contract_version": source.contract_version,
        "job_id": source.job_id,
        "owner_id": source.owner_id,
        "bucket": source.bucket,
        "object_key": source.object_key,
        "version_id": source.version_id,
        "content_sha256": source.content_sha256,
        "size_bytes": source.size_bytes,
        "media_type": source.media_type,
        "product_profile_id": source.product_profile_id,
        "product_profile_version": source.product_profile_version,
        "product_profile_fingerprint": source.product_profile_fingerprint,
        "created_at": source.created_at.isoformat(),
    }
    if source.width is not None and source.height is not None:
        material["width"] = source.width
        material["height"] = source.height
    return control_fingerprint(material)


def validate_publication_guard_source_authority(
    authority: PublicationGuardSourceAuthority,
) -> None:
    """Fail closed on the exact immutable Phase 6 authority without store imports."""

    try:
        revalidated = PublicationGuardSourceAuthority(
            current_job=ControlJobRecord.model_validate(
                authority.current_job.model_dump(mode="python")
            ),
            review=ReviewContent.model_validate(authority.review.model_dump(mode="python")),
            approval_decision=ReviewDecisionRecord.model_validate(
                authority.approval_decision.model_dump(mode="python")
            ),
            source=SourceArtifactRecord.model_validate(authority.source.model_dump(mode="python")),
            product_sync=ProductSyncRecord.model_validate(
                authority.product_sync.model_dump(mode="python")
            ),
            pricing_snapshot=PricingSnapshot.model_validate(
                authority.pricing_snapshot.model_dump(mode="python")
            ),
            pricing_evidence=PricingEvidenceRecord.model_validate(
                authority.pricing_evidence.model_dump(mode="python")
            ),
        )
    except Exception:
        raise ValueError("Publication guard source authority is invalid") from None
    if revalidated != authority:
        raise ValueError("Publication guard source authority is invalid")

    job = authority.current_job
    review = authority.review
    decision = authority.approval_decision
    source = authority.source
    sync = authority.product_sync
    pricing = authority.pricing_snapshot
    evidence = authority.pricing_evidence
    if (
        job.state is not ControlJobState.APPROVED
        or not job.review_validated
        or any(
            value is None
            for value in (
                job.review_fingerprint,
                job.source_artifact_fingerprint,
                job.artwork_analysis_fingerprint,
                job.product_id,
                job.provider_payload_fingerprint,
                job.product_sync_id,
                job.product_sync_fingerprint,
                job.pricing_snapshot_id,
                job.pricing_snapshot_fingerprint,
                job.approval_decision_id,
                job.approval_fingerprint,
                job.uploaded_image_id,
            )
        )
        or job.provider_outcome_unconfirmed
        or job.upload_outcome_unconfirmed
    ):
        raise ValueError("Publication guard source authority is invalid")
    if (
        review.job_id != job.job_id
        or review.review_version != job.review_version
        or review.fingerprint != job.review_fingerprint
        or review.fingerprint != _review_content_fingerprint(review)
        or review.artwork_analysis_fingerprint != job.artwork_analysis_fingerprint
        or not review.validation_passed
    ):
        raise ValueError("Publication guard source authority is invalid")
    if (
        decision.job_id != job.job_id
        or decision.actor_owner_id != job.owner_id
        or decision.decision is not ReviewDecision.APPROVE
        or decision.decision_id != job.approval_decision_id
        or decision.review_version != job.review_version
        or decision.review_fingerprint != job.review_fingerprint
        or decision.approval_fingerprint != job.approval_fingerprint
        or decision.decision_id
        != f"decision_{sha256(decision.command_receipt_id.encode()).hexdigest()[:40]}"
        or (job.publication_aggregate_id is None and decision.decided_at != job.updated_at)
    ):
        raise ValueError("Publication guard source authority is invalid")
    if (
        source.owner_id != job.owner_id
        or source.job_id != job.job_id
        or source.fingerprint != job.source_artifact_fingerprint
        or source.fingerprint != _source_artifact_fingerprint(source)
        or review.product_profile_fingerprint != source.product_profile_fingerprint
    ):
        raise ValueError("Publication guard source authority is invalid")
    if (
        sync.job_id != job.job_id
        or sync.sync_id != job.product_sync_id
        or sync.review_version != job.review_version
        or sync.product_id != job.product_id
        or sync.image_id != job.uploaded_image_id
        or sync.payload_fingerprint != job.provider_payload_fingerprint
        or sync.fingerprint != job.product_sync_fingerprint
        or sync.printify_shop_id is None
        or sync.provider_locked
        or sync.provider_published
    ):
        raise ValueError("Publication guard source authority is invalid")
    try:
        if sync.fingerprint != product_sync_record_fingerprint(sync):
            raise ValueError
    except ValueError:
        raise ValueError("Publication guard source authority is invalid") from None
    if (
        pricing.job_id != job.job_id
        or pricing.snapshot_id != job.pricing_snapshot_id
        or pricing.review_version != job.review_version
        or pricing.product_sync_fingerprint != sync.fingerprint
        or pricing.fingerprint != job.pricing_snapshot_fingerprint
        or evidence.job_id != job.job_id
        or evidence.snapshot_id != pricing.snapshot_id
        or evidence.review_version != pricing.review_version
        or evidence.product_sync_fingerprint != pricing.product_sync_fingerprint
        or evidence.fingerprint != pricing.fingerprint
        or evidence.created_at != pricing.created_at
        or evidence.estimate.fresh_until != pricing.fresh_until
        or evidence.estimate.calculated_at != pricing.created_at
        or evidence.estimate.fingerprint != pricing.fingerprint
    ):
        raise ValueError("Publication guard source authority is invalid")
    estimate_by_id = {variant.variant_id: variant for variant in evidence.estimate.variants}
    sync_by_id = {variant.variant_id: variant for variant in sync.variants}
    if set(estimate_by_id) != set(sync_by_id) or any(
        estimate_by_id[variant_id].retail_price_cents != sync_by_id[variant_id].retail_price_cents
        or estimate_by_id[variant_id].production_cost_cents
        != sync_by_id[variant_id].production_cost_cents
        for variant_id in sync_by_id
    ):
        raise ValueError("Publication guard source authority is invalid")
    if job.approval_fingerprint != review_etag(
        job_id=job.job_id,
        review_version=job.review_version,
        review_fingerprint=job.review_fingerprint,
        product_id=job.product_id,
        product_sync_fingerprint=job.product_sync_fingerprint,
        pricing_snapshot_id=job.pricing_snapshot_id,
        pricing_snapshot_fingerprint=job.pricing_snapshot_fingerprint,
    ):
        raise ValueError("Publication guard source authority is invalid")


class DurablePublicationPreCallGuard:
    """Re-read every approval/snapshot join before any outer runtime may coordinate work."""

    __slots__ = ("_eligibility", "_profiles", "_release_fingerprint", "_store")

    def __init__(
        self,
        *,
        store: PublicationExecutionSourceStore,
        profiles: PublicationProfileAuthority,
        eligibility: PublicationProfileEligibilityAuthority,
        release_manifest_fingerprint: str,
    ) -> None:
        if (
            not isinstance(release_manifest_fingerprint, str)
            or len(release_manifest_fingerprint) != 64
            or any(
                character not in "0123456789abcdef" for character in release_manifest_fingerprint
            )
            or release_manifest_fingerprint == "0" * 64
        ):
            raise ValueError("A nonzero release manifest fingerprint is required")
        self._store = store
        self._profiles = profiles
        self._eligibility = eligibility
        self._release_fingerprint = release_manifest_fingerprint

    def require_current(
        self,
        *,
        owner_id: str,
        aggregate_id: str,
    ) -> PublicationExecutionAuthority:
        try:
            execution = self._store.load_execution_authority(owner_id, aggregate_id)
            execution = PublicationExecutionAuthority.model_validate(
                execution.model_dump(mode="python")
            )
            source = self._store.load_source_authority(owner_id, aggregate_id)
            validate_publication_guard_source_authority(source)
            self._require_snapshot_match(
                execution,
                source,
                owner_id=owner_id,
                aggregate_id=aggregate_id,
            )
            exact = self._profiles.get_exact(
                profile_id=execution.snapshot.profile_id,
                profile_version=execution.snapshot.profile_version,
            )
            if not isinstance(exact, ExactReviewProductProfile):
                raise ValueError
            if (
                exact.profile.profile_id != execution.snapshot.profile_id
                or exact.profile.profile_version != execution.snapshot.profile_version
                or exact.fingerprint != control_fingerprint(exact.profile)
                or exact.fingerprint != execution.snapshot.profile_fingerprint
            ):
                raise ValueError
            eligibility = self._eligibility.get_exact(
                profile_id=exact.profile.profile_id,
                profile_version=exact.profile.profile_version,
                profile_fingerprint=exact.fingerprint,
                expected_sales_channel=execution.snapshot.expected_sales_channel,
                release_manifest_fingerprint=self._release_fingerprint,
                phase6_profile_publish_enabled=exact.profile.publish_enabled,
            )
            require_exact_publication_profile_eligibility(
                eligibility.model_dump(mode="python"),
                profile_id=exact.profile.profile_id,
                profile_version=exact.profile.profile_version,
                profile_fingerprint=exact.fingerprint,
                expected_sales_channel=execution.snapshot.expected_sales_channel,
                release_manifest_fingerprint=self._release_fingerprint,
                phase6_profile_publish_enabled=exact.profile.publish_enabled,
            )
            return execution
        except Exception:
            raise PublicationPreCallAuthorityError(
                "Publication pre-call authority is invalid"
            ) from None

    def _require_snapshot_match(
        self,
        execution: PublicationExecutionAuthority,
        source: PublicationGuardSourceAuthority,
        *,
        owner_id: str,
        aggregate_id: str,
    ) -> None:
        snapshot = execution.snapshot
        request_link = execution.request_job_link
        job = source.current_job
        review = source.review
        decision = source.approval_decision
        sync = source.product_sync
        pricing = source.pricing_snapshot
        evidence = source.pricing_evidence
        if (
            execution.aggregate.state
            in {
                PublicationState.PUBLISHED,
                PublicationState.PUBLICATION_FAILED,
                PublicationState.PUBLICATION_OUTCOME_UNKNOWN,
            }
            or snapshot.owner_id != owner_id
            or execution.aggregate.aggregate_id != aggregate_id
            or job.owner_id != snapshot.owner_id
            or job.job_id != snapshot.job_id
            or job.publication_aggregate_id != execution.aggregate.aggregate_id
            or request_link.expected_record_version != snapshot.expected_record_version
            or request_link.result_record_version != snapshot.expected_record_version + 1
            or execution.phase6_record_version != request_link.result_record_version
            or job.record_version != execution.phase6_record_version
            or job.event_sequence != execution.phase6_event_sequence
            or job.publication_terminal_state is not None
            or job.approval_decision_id != snapshot.approval_decision_id
            or decision.decision_id != snapshot.approval_decision_id
            or job.approval_fingerprint != snapshot.approval_fingerprint
            or decision.approval_fingerprint != snapshot.approval_fingerprint
            or review.review_version != snapshot.review_version
            or review.fingerprint != snapshot.review_fingerprint
            or sync.sync_id != snapshot.product_sync_id
            or sync.fingerprint != snapshot.product_sync_fingerprint
            or sync.printify_shop_id != snapshot.printify_shop_id
            or sync.product_id != snapshot.printify_product_id
            or sync.image_id != snapshot.printify_image_id
            or sync.payload_fingerprint != snapshot.product_payload_fingerprint
            or pricing.snapshot_id != snapshot.pricing_snapshot_id
            or pricing.fingerprint != snapshot.pricing_snapshot_fingerprint
            or evidence.fingerprint != snapshot.pricing_evidence_fingerprint
            or source.source.product_profile_id != snapshot.profile_id
            or source.source.product_profile_version != snapshot.profile_version
            or source.source.product_profile_fingerprint != snapshot.profile_fingerprint
            or snapshot.release_manifest_fingerprint != self._release_fingerprint
        ):
            raise ValueError


class PublicationGuardOperation(StrEnum):
    STATUS = "status"
    VERIFY_AUTHORITY = "verify_authority"


class PublicationGuardOutcome(StrEnum):
    SEALED_CONFIGURATION = "sealed_configuration"
    AUTHORITY_CURRENT = "authority_current"
    AUTHORITY_REJECTED = "authority_rejected"


class _GuardModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class PublicationGuardRuntimeActivation(_GuardModel):
    """Exact active-read tuple that grants neither request nor provider authority."""

    scaffold_only: Literal[False] = False
    approval_guard_enabled: Literal[True] = True
    query_enabled: Literal[False] = False
    request_enabled: Literal[False] = False
    publication_enabled: Literal[False] = False

    @field_validator(
        "scaffold_only",
        "query_enabled",
        "request_enabled",
        "publication_enabled",
        mode="before",
    )
    @classmethod
    def exact_false_flags(cls, value: object) -> object:
        if value is not False:
            raise ValueError("Phase 7 guard activation flags must be exact")
        return value

    @field_validator("approval_guard_enabled", mode="before")
    @classmethod
    def exact_guard_flag(cls, value: object) -> object:
        if value is not True:
            raise ValueError("Phase 7 guard activation flags must be exact")
        return value

    @model_validator(mode="after")
    def frozen_contract_remains_disabled(self) -> PublicationGuardRuntimeActivation:
        contract = phase7_publication_contract()
        if (
            contract.publication_enabled is not False
            or contract.current_activation_phase
            is not PublicationActivationPhaseName.OFFLINE_IMPLEMENTATION
        ):
            raise ValueError("Phase 7 guard requires the frozen publication-disabled contract")
        return self


class PublicationGuardRequest(_GuardModel):
    """Closed direct-invocation input; identity is required only for the authority read."""

    operation: PublicationGuardOperation
    owner_id: OwnerId | None = None
    aggregate_id: SafeId | None = None

    @field_validator("owner_id", "aggregate_id", mode="before")
    @classmethod
    def identities_are_exact_strings_or_absent(cls, value: object) -> object:
        if value is None:
            return value
        if type(value) is not str or not value or value != value.strip():
            raise ValueError("Publication guard authority identity is invalid")
        return value

    @field_validator("operation", mode="before")
    @classmethod
    def operation_is_one_exact_string(cls, value: object) -> PublicationGuardOperation:
        if isinstance(value, PublicationGuardOperation):
            return value
        if type(value) is not str:
            raise ValueError("Publication guard operation is invalid")
        try:
            return PublicationGuardOperation(value)
        except ValueError:
            raise ValueError("Publication guard operation is invalid") from None

    @model_validator(mode="after")
    def identity_shape_matches_operation(self) -> PublicationGuardRequest:
        has_identity = self.owner_id is not None or self.aggregate_id is not None
        if self.operation is PublicationGuardOperation.STATUS:
            if has_identity:
                raise ValueError("Publication guard status accepts no identity")
        elif self.owner_id is None or self.aggregate_id is None:
            raise ValueError("Publication guard authority identity is incomplete")
        return self


class PublicationGuardAttestation(_GuardModel):
    """Identifier-free result suitable for deployment evidence and closed logs."""

    contract_version: Literal["7.0.1"] = PHASE7_PUBLICATION_CONTRACT_VERSION
    contract_fingerprint: Fingerprint
    guard_release_fingerprint: Fingerprint
    profile_fingerprint: Fingerprint
    operation: PublicationGuardOperation
    outcome: PublicationGuardOutcome
    approval_authority_current: bool | None
    approval_guard_enabled: Literal[True] = True
    query_enabled: Literal[False] = False
    request_enabled: Literal[False] = False
    publication_enabled: Literal[False] = False
    provider_calls_authorized: Literal[0] = 0
    fingerprint: Fingerprint

    @field_validator("operation", mode="before")
    @classmethod
    def operation_is_exact(cls, value: object) -> PublicationGuardOperation:
        if isinstance(value, PublicationGuardOperation):
            return value
        if type(value) is not str:
            raise ValueError("Publication guard attestation is invalid")
        return PublicationGuardOperation(value)

    @field_validator("outcome", mode="before")
    @classmethod
    def outcome_is_exact(cls, value: object) -> PublicationGuardOutcome:
        if isinstance(value, PublicationGuardOutcome):
            return value
        if type(value) is not str:
            raise ValueError("Publication guard attestation is invalid")
        return PublicationGuardOutcome(value)

    @field_validator("approval_guard_enabled", mode="before")
    @classmethod
    def enabled_flag_is_exact_true(cls, value: object) -> object:
        if value is not True:
            raise ValueError("Publication guard attestation is invalid")
        return value

    @field_validator(
        "query_enabled",
        "request_enabled",
        "publication_enabled",
        mode="before",
    )
    @classmethod
    def disabled_flags_are_exact_false(cls, value: object) -> object:
        if value is not False:
            raise ValueError("Publication guard attestation is invalid")
        return value

    @field_validator("provider_calls_authorized", mode="before")
    @classmethod
    def provider_count_is_exact_zero(cls, value: object) -> object:
        if type(value) is not int or value != 0:
            raise ValueError("Publication guard attestation is invalid")
        return value

    @model_validator(mode="after")
    def result_is_closed_and_content_bound(self) -> PublicationGuardAttestation:
        expected_current = {
            PublicationGuardOutcome.SEALED_CONFIGURATION: None,
            PublicationGuardOutcome.AUTHORITY_CURRENT: True,
            PublicationGuardOutcome.AUTHORITY_REJECTED: False,
        }[self.outcome]
        expected_operation = (
            PublicationGuardOperation.STATUS
            if self.outcome is PublicationGuardOutcome.SEALED_CONFIGURATION
            else PublicationGuardOperation.VERIFY_AUTHORITY
        )
        if (
            self.operation is not expected_operation
            or self.approval_authority_current is not expected_current
            or self.contract_fingerprint != phase7_publication_contract_digest()
            or self.fingerprint != _attestation_fingerprint(self)
        ):
            raise ValueError("Publication guard attestation is invalid")
        return self


class PublicationApprovalGuard(Protocol):
    def require_current(self, *, owner_id: str, aggregate_id: str) -> object: ...


@dataclass(frozen=True, slots=True)
class PublicationGuardVerificationService:
    """Run no-data startup attestation or one exact current-authority verification."""

    guard: PublicationApprovalGuard
    activation: PublicationGuardRuntimeActivation
    guard_release_fingerprint: str
    profile_fingerprint: str

    def status(self) -> PublicationGuardAttestation:
        return self._attestation(
            operation=PublicationGuardOperation.STATUS,
            outcome=PublicationGuardOutcome.SEALED_CONFIGURATION,
            approval_authority_current=None,
        )

    def verify_authority(
        self,
        *,
        owner_id: str,
        aggregate_id: str,
    ) -> PublicationGuardAttestation:
        try:
            self.guard.require_current(owner_id=owner_id, aggregate_id=aggregate_id)
        except PublicationPreCallAuthorityError:
            return self._attestation(
                operation=PublicationGuardOperation.VERIFY_AUTHORITY,
                outcome=PublicationGuardOutcome.AUTHORITY_REJECTED,
                approval_authority_current=False,
            )
        except Exception:
            return self._attestation(
                operation=PublicationGuardOperation.VERIFY_AUTHORITY,
                outcome=PublicationGuardOutcome.AUTHORITY_REJECTED,
                approval_authority_current=False,
            )
        return self._attestation(
            operation=PublicationGuardOperation.VERIFY_AUTHORITY,
            outcome=PublicationGuardOutcome.AUTHORITY_CURRENT,
            approval_authority_current=True,
        )

    def handle(self, value: object) -> PublicationGuardAttestation:
        try:
            request = PublicationGuardRequest.model_validate(value)
        except Exception:
            return self._attestation(
                operation=PublicationGuardOperation.VERIFY_AUTHORITY,
                outcome=PublicationGuardOutcome.AUTHORITY_REJECTED,
                approval_authority_current=False,
            )
        if request.operation is PublicationGuardOperation.STATUS:
            return self.status()
        assert request.owner_id is not None and request.aggregate_id is not None
        return self.verify_authority(
            owner_id=request.owner_id,
            aggregate_id=request.aggregate_id,
        )

    def _attestation(
        self,
        *,
        operation: PublicationGuardOperation,
        outcome: PublicationGuardOutcome,
        approval_authority_current: bool | None,
    ) -> PublicationGuardAttestation:
        activation = PublicationGuardRuntimeActivation.model_validate(
            self.activation.model_dump(mode="python")
        )
        values = {
            "contract_fingerprint": phase7_publication_contract_digest(),
            "guard_release_fingerprint": self.guard_release_fingerprint,
            "profile_fingerprint": self.profile_fingerprint,
            "operation": operation,
            "outcome": outcome,
            "approval_authority_current": approval_authority_current,
            "approval_guard_enabled": activation.approval_guard_enabled,
            "query_enabled": activation.query_enabled,
            "request_enabled": activation.request_enabled,
            "publication_enabled": activation.publication_enabled,
            "provider_calls_authorized": 0,
        }
        return PublicationGuardAttestation(
            **values,
            fingerprint=_attestation_fingerprint(values),
        )


def _attestation_fingerprint(value: object) -> str:
    if isinstance(value, PublicationGuardAttestation):
        payload = value.model_dump(mode="json", exclude={"fingerprint"})
    elif isinstance(value, dict):
        payload = {
            "contract_version": PHASE7_PUBLICATION_CONTRACT_VERSION,
            **value,
        }
    else:
        raise ValueError("Publication guard attestation is invalid")
    encoded = json.dumps(
        payload,
        allow_nan=False,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return sha256(encoded).hexdigest()


__all__ = [
    "DurablePublicationPreCallGuard",
    "PublicationPreCallAuthorityError",
    "PublicationGuardAttestation",
    "PublicationGuardOperation",
    "PublicationGuardOutcome",
    "PublicationGuardRequest",
    "PublicationGuardRuntimeActivation",
    "PublicationGuardSourceAuthority",
    "PublicationGuardVerificationService",
    "validate_publication_guard_source_authority",
]
