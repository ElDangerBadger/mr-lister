"""Durable, capability-free provenance for sanitized Phase 7 provider evidence."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from enum import StrEnum
from hashlib import sha256
from threading import RLock
from typing import Literal, Protocol

from pydantic import model_validator

from mr_lister.publication.contract import PublicationState
from mr_lister.publication.errors import (
    PublicationConflictError,
    PublicationErrorCode,
    PublicationNotFoundError,
)
from mr_lister.publication.execution_fingerprints import execution_record_fingerprint
from mr_lister.publication.execution_models import (
    ExecutionPublicationAggregate,
    Fingerprint,
    PublicationCallClaim,
    PublicationCallKind,
    PublicationCallPurpose,
    PublicationExecutionAuthority,
    PublicationExecutionOperation,
    PublicationModel,
    PublicationPreflightFailureReason,
    PublicationProductReadEvidence,
    PublicationProviderAuditBinding,
    PublicationPublishEvidence,
    PublicationShopPreflightEvidence,
    SafeId,
    UtcDateTime,
)


class PublicationProviderEvidenceKind(StrEnum):
    SHOP_PREFLIGHT = "shop_preflight"
    PRODUCT_PREFLIGHT = "product_preflight"
    PUBLISH_OUTCOME = "publish_outcome"
    PRODUCT_VERIFICATION = "product_verification"
    PRODUCT_RECONCILIATION = "product_reconciliation"
    DEFINITIVE_PREFLIGHT_NEGATIVE = "definitive_preflight_negative"


class PublicationProviderEvidenceType(StrEnum):
    SHOP_PREFLIGHT = "shop_preflight_evidence"
    PRODUCT_READ = "product_read_evidence"
    PUBLISH_OUTCOME = "publish_evidence"
    DEFINITIVE_PREFLIGHT_FAILURE = "definitive_preflight_evidence"


class PublicationDefinitivePreflightEvidence(PublicationModel):
    """Closed provider-derived reason that may retire an unused permit before deadline."""

    call_claim_id: SafeId
    call_claim_fingerprint: Fingerprint
    provider_authority_id: SafeId
    provider_authority_fingerprint: Fingerprint
    failure_reason: Literal[
        PublicationPreflightFailureReason.SHOP_NOT_CONNECTED_TO_ETSY,
        PublicationPreflightFailureReason.EXACT_PRODUCT_NOT_FOUND,
        PublicationPreflightFailureReason.PRODUCT_LOCKED,
        PublicationPreflightFailureReason.PRODUCT_ALREADY_PUBLISHED,
        PublicationPreflightFailureReason.CANONICAL_CONTENT_MISMATCH,
        PublicationPreflightFailureReason.VARIANT_AUTHORITY_MISMATCH,
    ]
    sanitized_response_fingerprint: Fingerprint
    observed_at: UtcDateTime
    fingerprint: Fingerprint

    @model_validator(mode="after")
    def content_is_bound(self) -> PublicationDefinitivePreflightEvidence:
        if self.fingerprint != execution_record_fingerprint(
            "definitive_preflight_evidence",
            self,
        ):
            raise ValueError("Definitive preflight evidence fingerprint is invalid")
        return self


type PublicationProviderEvidence = (
    PublicationShopPreflightEvidence
    | PublicationProductReadEvidence
    | PublicationPublishEvidence
    | PublicationDefinitivePreflightEvidence
)


class PublicationProviderEvidenceDescriptor(PublicationModel):
    """Digest-only evidence identity suitable for receipts and indexes."""

    evidence_id: SafeId
    evidence_type: PublicationProviderEvidenceType
    evidence_fingerprint: Fingerprint
    observed_at: UtcDateTime
    fingerprint: Fingerprint

    @model_validator(mode="after")
    def descriptor_is_content_bound(self) -> PublicationProviderEvidenceDescriptor:
        if self.fingerprint != execution_record_fingerprint(
            "provider_evidence_descriptor",
            self,
        ):
            raise ValueError("Provider evidence descriptor fingerprint is invalid")
        return self


class PublicationProviderEvidenceStage(PublicationModel):
    """Immutable provider evidence staged after an allowed audited wire attempt."""

    stage_id: SafeId
    aggregate_id: SafeId
    call_claim_id: SafeId
    call_claim_fingerprint: Fingerprint
    call_kind: PublicationCallKind
    call_purpose: PublicationCallPurpose
    provider_authority_id: SafeId
    provider_authority_fingerprint: Fingerprint
    evidence_kind: PublicationProviderEvidenceKind
    evidence_type: PublicationProviderEvidenceType
    evidence_id: SafeId
    evidence_fingerprint: Fingerprint
    evidence: PublicationProviderEvidence
    allowed_audit_binding_fingerprint: Fingerprint
    observed_at: UtcDateTime
    staged_at: UtcDateTime
    fingerprint: Fingerprint

    @model_validator(mode="after")
    def staged_evidence_is_exact(self) -> PublicationProviderEvidenceStage:
        expected_type = _evidence_type(self.evidence)
        if (
            self.evidence_type is not expected_type
            or self.evidence_fingerprint != self.evidence.fingerprint
            or self.observed_at != self.evidence.observed_at
            or self.staged_at < self.observed_at
            or self.call_claim_id != self.evidence.call_claim_id
            or self.call_claim_fingerprint != self.evidence.call_claim_fingerprint
            or self.provider_authority_id != self.evidence.provider_authority_id
            or self.provider_authority_fingerprint != self.evidence.provider_authority_fingerprint
            or self.evidence_id != _stable_id("provider_evidence", self.evidence.fingerprint)
        ):
            raise ValueError("Provider evidence stage differs from its sanitized evidence")
        if self.fingerprint != execution_record_fingerprint("provider_evidence_stage", self):
            raise ValueError("Provider evidence stage fingerprint is invalid")
        return self


class PublicationProviderEvidenceConsumption(PublicationModel):
    """Immutable single-use join written atomically with an execution transition."""

    consumption_id: SafeId
    aggregate_id: SafeId
    stage_id: SafeId
    stage_fingerprint: Fingerprint
    call_claim_id: SafeId
    call_claim_fingerprint: Fingerprint
    provider_authority_id: SafeId
    provider_authority_fingerprint: Fingerprint
    evidence_kind: PublicationProviderEvidenceKind
    evidence_type: PublicationProviderEvidenceType
    evidence_id: SafeId
    evidence_fingerprint: Fingerprint
    allowed_audit_binding_fingerprint: Fingerprint
    operation_id: SafeId
    operation: PublicationExecutionOperation
    receipt_id: SafeId
    consumed_at: UtcDateTime
    fingerprint: Fingerprint

    @model_validator(mode="after")
    def consumption_is_content_bound(self) -> PublicationProviderEvidenceConsumption:
        if self.consumption_id != _stable_id(
            "provider_evidence_consumption",
            self.aggregate_id,
            self.stage_id,
            self.operation_id,
        ):
            raise ValueError("Provider evidence consumption identity is invalid")
        if self.fingerprint != execution_record_fingerprint(
            "provider_evidence_consumption",
            self,
        ):
            raise ValueError("Provider evidence consumption fingerprint is invalid")
        return self


class PublicationProviderEvidenceCommit(PublicationModel):
    """Exact authority and immutable stage for one audited boundary result."""

    expected: PublicationExecutionAuthority
    expected_aggregate: ExecutionPublicationAggregate
    updated_aggregate: ExecutionPublicationAggregate
    stage: PublicationProviderEvidenceStage

    @model_validator(mode="after")
    def stage_matches_expected_authority(self) -> PublicationProviderEvidenceCommit:
        _validate_stage_against_authority(self.expected, self.stage)
        before = self.expected_aggregate
        after = self.updated_aggregate
        if (
            before != self.expected.aggregate
            or self.stage.aggregate_id != before.aggregate_id
            or after.provider_evidence_record_version != before.provider_evidence_record_version + 1
        ):
            raise ValueError("Provider evidence must increment the exact root watermark once")
        expected_after = ExecutionPublicationAggregate.model_validate(
            {
                **before.model_dump(
                    mode="python",
                    exclude={"contract_version", "fingerprint"},
                ),
                "provider_evidence_record_version": after.provider_evidence_record_version,
                "fingerprint": after.fingerprint,
            }
        )
        if expected_after != after:
            raise ValueError("Provider evidence may change only its root watermark")
        return self


class PublicationEvidenceAuthorityReader(Protocol):
    def __call__(self, aggregate_id: str) -> PublicationExecutionAuthority: ...


class PublicationProviderEvidenceStore(Protocol):
    def stage_evidence(
        self,
        commit: PublicationProviderEvidenceCommit,
    ) -> PublicationProviderEvidenceStage: ...

    def get_stage(
        self,
        aggregate_id: str,
        stage_id: str,
    ) -> PublicationProviderEvidenceStage: ...

    def consume_evidence(
        self,
        *,
        aggregate_id: str,
        stage_id: str,
        stage_fingerprint: str,
        operation_id: str,
        operation: PublicationExecutionOperation,
    ) -> PublicationProviderEvidenceConsumption: ...

    def get_consumption(
        self,
        aggregate_id: str,
        stage_id: str,
    ) -> PublicationProviderEvidenceConsumption | None: ...


class InMemoryPublicationEvidenceCore:
    """Thread-safe provenance oracle, not an atomic execution-store composition."""

    def __init__(
        self,
        authority_reader: PublicationEvidenceAuthorityReader,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._authority_reader = authority_reader
        self._clock = clock or (lambda: datetime.now(UTC))
        self._lock = RLock()
        self._stages: dict[tuple[str, str], PublicationProviderEvidenceStage] = {}
        self._claim_stages: dict[tuple[str, str], str] = {}
        self._consumptions: dict[tuple[str, str], PublicationProviderEvidenceConsumption] = {}
        self._aggregate_evidence_heads: dict[str, int] = {}

    def stage_evidence(
        self,
        commit: PublicationProviderEvidenceCommit,
    ) -> PublicationProviderEvidenceStage:
        reparsed = PublicationProviderEvidenceCommit.model_validate(
            commit.model_dump(mode="python")
        )
        if reparsed != commit:
            _invalid("Provider evidence commit failed strict revalidation")
        stage = commit.stage
        with self._lock:
            key = (stage.aggregate_id, stage.stage_id)
            existing = self._stages.get(key)
            if existing is not None:
                if existing == stage:
                    return existing
                _conflict("Provider evidence stage identity was reused")
            claim_key = (stage.aggregate_id, stage.call_claim_id)
            prior_stage_id = self._claim_stages.get(claim_key)
            if prior_stage_id is not None:
                prior = self._stages[(stage.aggregate_id, prior_stage_id)]
                if prior == stage:
                    return prior
                _conflict("Provider call already owns staged evidence")
            current = self._authority_reader(stage.aggregate_id)
            if current != commit.expected:
                _conflict("Provider authority changed before evidence staging")
            evidence_head = self._aggregate_evidence_heads.get(stage.aggregate_id)
            expected_head = commit.expected_aggregate.provider_evidence_record_version
            if evidence_head is not None and evidence_head != expected_head:
                _conflict("Provider evidence root watermark changed before staging")
            _validate_stage_against_authority(current, stage)
            if self._now() < stage.staged_at:
                _invalid("Provider evidence cannot be staged in the future")
            self._stages[key] = stage
            self._claim_stages[claim_key] = stage.stage_id
            self._aggregate_evidence_heads[stage.aggregate_id] = (
                commit.updated_aggregate.provider_evidence_record_version
            )
            return stage

    def get_stage(
        self,
        aggregate_id: str,
        stage_id: str,
    ) -> PublicationProviderEvidenceStage:
        with self._lock:
            stage = self._stages.get((aggregate_id, stage_id))
            if stage is None:
                raise PublicationNotFoundError()
            return stage

    def consume_evidence(
        self,
        *,
        aggregate_id: str,
        stage_id: str,
        stage_fingerprint: str,
        operation_id: str,
        operation: PublicationExecutionOperation,
    ) -> PublicationProviderEvidenceConsumption:
        with self._lock:
            stage = self._stages.get((aggregate_id, stage_id))
            if stage is None or stage.fingerprint != stage_fingerprint:
                raise PublicationNotFoundError()
            key = (aggregate_id, stage_id)
            existing = self._consumptions.get(key)
            if existing is not None:
                if existing.operation_id == operation_id and existing.operation is operation:
                    return existing
                _conflict("Provider evidence was already consumed")
            consumed_at = self._now()
            values = _consumption_values(
                stage,
                operation_id=operation_id,
                operation=operation,
                receipt_id=_stable_id("provider_evidence_receipt", operation_id),
                consumed_at=consumed_at,
            )
            consumption = PublicationProviderEvidenceConsumption(
                **values,
                fingerprint=execution_record_fingerprint(
                    "provider_evidence_consumption",
                    values,
                ),
            )
            self._consumptions[key] = consumption
            return consumption

    def get_consumption(
        self,
        aggregate_id: str,
        stage_id: str,
    ) -> PublicationProviderEvidenceConsumption | None:
        with self._lock:
            return self._consumptions.get((aggregate_id, stage_id))

    def _now(self) -> datetime:
        value = self._clock()
        if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
            raise RuntimeError("Publication evidence clock must be timezone-aware")
        return value.astimezone(UTC)


def build_provider_evidence_commit(
    authority: PublicationExecutionAuthority,
    call_claim: PublicationCallClaim,
    audit_binding: PublicationProviderAuditBinding,
    evidence: PublicationProviderEvidence,
    *,
    staged_at: datetime,
) -> PublicationProviderEvidenceCommit:
    """Build one exact immutable stage from audited, sanitized boundary evidence."""

    try:
        call_claim = PublicationCallClaim.model_validate(call_claim.model_dump(mode="python"))
        audit_binding = PublicationProviderAuditBinding.model_validate(
            audit_binding.model_dump(mode="python")
        )
        evidence = type(evidence).model_validate(evidence.model_dump(mode="python"))
    except Exception:
        _invalid("Provider evidence input failed strict revalidation")
    current_claim = next(
        (
            candidate
            for candidate in authority.call_claims
            if candidate.authorization_id == call_claim.authorization_id
        ),
        None,
    )
    if current_claim != call_claim:
        _invalid("Provider evidence lacks its exact durable call claim")
    provider_authority = authority.provider_authority
    if provider_authority is None:
        _invalid("Provider evidence lacks reconstructed application authority")
    if (
        audit_binding.aggregate_id != authority.aggregate.aggregate_id
        or audit_binding.call_claim_id != call_claim.authorization_id
        or audit_binding.call_claim_fingerprint != call_claim.fingerprint
    ):
        _invalid("Provider evidence lacks its exact allowed audit binding")
    if not isinstance(staged_at, datetime) or staged_at.tzinfo is None:
        _invalid("Provider evidence stage time must be timezone-aware")
    staged_at = staged_at.astimezone(UTC)
    kind = _evidence_kind(call_claim, evidence)
    evidence_type = _evidence_type(evidence)
    evidence_id = _stable_id("provider_evidence", evidence.fingerprint)
    stage_id = _stable_id(
        "provider_evidence_stage",
        authority.aggregate.aggregate_id,
        call_claim.authorization_id,
    )
    values = {
        "stage_id": stage_id,
        "aggregate_id": authority.aggregate.aggregate_id,
        "call_claim_id": call_claim.authorization_id,
        "call_claim_fingerprint": call_claim.fingerprint,
        "call_kind": call_claim.call_kind,
        "call_purpose": call_claim.purpose,
        "provider_authority_id": provider_authority.provider_authority_id,
        "provider_authority_fingerprint": provider_authority.fingerprint,
        "evidence_kind": kind,
        "evidence_type": evidence_type,
        "evidence_id": evidence_id,
        "evidence_fingerprint": evidence.fingerprint,
        "evidence": evidence,
        "allowed_audit_binding_fingerprint": audit_binding.fingerprint,
        "observed_at": evidence.observed_at,
        "staged_at": staged_at,
    }
    stage = PublicationProviderEvidenceStage(
        **values,
        fingerprint=execution_record_fingerprint("provider_evidence_stage", values),
    )
    aggregate_values = {
        **authority.aggregate.model_dump(
            mode="python",
            exclude={"contract_version", "fingerprint"},
        ),
        "provider_evidence_record_version": (
            authority.aggregate.provider_evidence_record_version + 1
        ),
    }
    updated_aggregate = ExecutionPublicationAggregate(
        **aggregate_values,
        fingerprint=execution_record_fingerprint("execution_aggregate", aggregate_values),
    )
    return PublicationProviderEvidenceCommit(
        expected=authority,
        expected_aggregate=authority.aggregate,
        updated_aggregate=updated_aggregate,
        stage=stage,
    )


def build_provider_evidence_consumption(
    stage: PublicationProviderEvidenceStage,
    *,
    operation_id: str,
    operation: PublicationExecutionOperation,
    receipt_id: str,
    consumed_at: datetime,
) -> PublicationProviderEvidenceConsumption:
    """Build the exact consumption written inside an execution transaction."""

    values = _consumption_values(
        stage,
        operation_id=operation_id,
        operation=operation,
        receipt_id=receipt_id,
        consumed_at=consumed_at,
    )
    return PublicationProviderEvidenceConsumption(
        **values,
        fingerprint=execution_record_fingerprint(
            "provider_evidence_consumption",
            values,
        ),
    )


def _consumption_values(
    stage: PublicationProviderEvidenceStage,
    *,
    operation_id: str,
    operation: PublicationExecutionOperation,
    receipt_id: str,
    consumed_at: datetime,
) -> dict[str, object]:
    return {
        "consumption_id": _stable_id(
            "provider_evidence_consumption",
            stage.aggregate_id,
            stage.stage_id,
            operation_id,
        ),
        "aggregate_id": stage.aggregate_id,
        "stage_id": stage.stage_id,
        "stage_fingerprint": stage.fingerprint,
        "call_claim_id": stage.call_claim_id,
        "call_claim_fingerprint": stage.call_claim_fingerprint,
        "provider_authority_id": stage.provider_authority_id,
        "provider_authority_fingerprint": stage.provider_authority_fingerprint,
        "evidence_kind": stage.evidence_kind,
        "evidence_type": stage.evidence_type,
        "evidence_id": stage.evidence_id,
        "evidence_fingerprint": stage.evidence_fingerprint,
        "allowed_audit_binding_fingerprint": stage.allowed_audit_binding_fingerprint,
        "operation_id": operation_id,
        "operation": operation,
        "receipt_id": receipt_id,
        "consumed_at": consumed_at,
    }


def _validate_stage_against_authority(
    authority: PublicationExecutionAuthority,
    stage: PublicationProviderEvidenceStage,
) -> None:
    provider = authority.provider_authority
    claim = next(
        (
            candidate
            for candidate in authority.call_claims
            if candidate.authorization_id == stage.call_claim_id
        ),
        None,
    )
    audit = next(
        (
            candidate
            for candidate in authority.provider_audits
            if candidate.call_claim_id == stage.call_claim_id
        ),
        None,
    )
    if (
        authority.aggregate.state
        in {
            PublicationState.PUBLISHED,
            PublicationState.PUBLICATION_FAILED,
            PublicationState.PUBLICATION_OUTCOME_UNKNOWN,
        }
        or stage.aggregate_id != authority.aggregate.aggregate_id
        or claim is None
        or stage.call_claim_fingerprint != claim.fingerprint
        or stage.call_kind is not claim.call_kind
        or stage.call_purpose is not claim.purpose
        or provider is None
        or stage.provider_authority_id != provider.provider_authority_id
        or stage.provider_authority_fingerprint != provider.fingerprint
        or audit is None
        or stage.allowed_audit_binding_fingerprint != audit.fingerprint
        or stage.observed_at < claim.authorized_at
        or stage.staged_at < stage.observed_at
        or stage.evidence_kind is not _evidence_kind(claim, stage.evidence)
        or not _evidence_matches_provider_authority(
            stage.evidence,
            authority,
        )
    ):
        _invalid("Staged evidence differs from exact audited provider authority")


def _evidence_matches_provider_authority(
    evidence: PublicationProviderEvidence,
    authority: PublicationExecutionAuthority,
) -> bool:
    assert authority.provider_authority is not None
    current = authority.provider_authority
    if isinstance(evidence, PublicationShopPreflightEvidence):
        return evidence.printify_shop_id == current.printify_shop_id
    if isinstance(evidence, PublicationProductReadEvidence):
        return (
            evidence.printify_shop_id == current.printify_shop_id
            and evidence.printify_product_id == current.printify_product_id
        )
    if isinstance(evidence, PublicationPublishEvidence):
        mutation = authority.mutation_claim
        return (
            mutation is not None
            and evidence.mutation_claim_id == mutation.mutation_claim_id
            and evidence.mutation_claim_fingerprint == mutation.fingerprint
        )
    return True


def _evidence_kind(
    claim: PublicationCallClaim,
    evidence: PublicationProviderEvidence,
) -> PublicationProviderEvidenceKind:
    if isinstance(evidence, PublicationShopPreflightEvidence):
        if not (
            claim.call_kind is PublicationCallKind.SHOP_GET
            and claim.purpose is PublicationCallPurpose.SHOP_PREFLIGHT
        ):
            _invalid("Shop evidence differs from its provider call purpose")
        return PublicationProviderEvidenceKind.SHOP_PREFLIGHT
    if isinstance(evidence, PublicationProductReadEvidence):
        if claim.call_kind is not PublicationCallKind.PRODUCT_GET:
            _invalid("Product evidence differs from its provider call kind")
        mapping = {
            PublicationCallPurpose.PRODUCT_PREFLIGHT: (
                PublicationProviderEvidenceKind.PRODUCT_PREFLIGHT
            ),
            PublicationCallPurpose.VERIFICATION: (
                PublicationProviderEvidenceKind.PRODUCT_VERIFICATION
            ),
            PublicationCallPurpose.RECONCILIATION: (
                PublicationProviderEvidenceKind.PRODUCT_RECONCILIATION
            ),
        }
        kind = mapping.get(claim.purpose)
        if kind is None:
            _invalid("Product evidence differs from its provider call purpose")
        return kind
    if isinstance(evidence, PublicationPublishEvidence):
        if not (
            claim.call_kind is PublicationCallKind.PUBLISH_POST
            and claim.purpose is PublicationCallPurpose.PUBLISH
        ):
            _invalid("Publish evidence differs from its provider call purpose")
        return PublicationProviderEvidenceKind.PUBLISH_OUTCOME
    if claim.purpose not in {
        PublicationCallPurpose.SHOP_PREFLIGHT,
        PublicationCallPurpose.PRODUCT_PREFLIGHT,
    }:
        _invalid("Definitive negative evidence is valid only during preflight")
    shop_reason = (
        evidence.failure_reason is PublicationPreflightFailureReason.SHOP_NOT_CONNECTED_TO_ETSY
    )
    if shop_reason != (claim.purpose is PublicationCallPurpose.SHOP_PREFLIGHT):
        _invalid("Definitive preflight reason differs from its provider route")
    return PublicationProviderEvidenceKind.DEFINITIVE_PREFLIGHT_NEGATIVE


def _evidence_type(
    evidence: PublicationProviderEvidence,
) -> PublicationProviderEvidenceType:
    if isinstance(evidence, PublicationShopPreflightEvidence):
        return PublicationProviderEvidenceType.SHOP_PREFLIGHT
    if isinstance(evidence, PublicationProductReadEvidence):
        return PublicationProviderEvidenceType.PRODUCT_READ
    if isinstance(evidence, PublicationPublishEvidence):
        return PublicationProviderEvidenceType.PUBLISH_OUTCOME
    return PublicationProviderEvidenceType.DEFINITIVE_PREFLIGHT_FAILURE


def _stable_id(kind: str, *parts: str) -> str:
    digest = sha256("\0".join((kind, *parts)).encode("utf-8")).hexdigest()
    return f"{kind[:32]}_{digest[:48]}"


def _invalid(message: str) -> None:
    raise PublicationConflictError(PublicationErrorCode.INVALID_AUTHORITY, message)


def _conflict(message: str) -> None:
    raise PublicationConflictError(PublicationErrorCode.CONCURRENT_WRITE, message)
