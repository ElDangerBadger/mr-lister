"""Uncomposed Phase 7.3 coordinator for sealed provider evidence execution.

The public invocation surface accepts only the owner and publication aggregate identities.  It
derives every command, optimistic version, provider purpose, claim, and evidence-stage reference
from durable application authority.  One invocation performs at most one provider wire request.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from hashlib import sha256
from typing import Protocol

from mr_lister.publication.contract import PublicationPermitState, PublicationState
from mr_lister.publication.evidence_provenance import (
    PublicationProviderEvidenceKind,
    PublicationProviderEvidenceStage,
)
from mr_lister.publication.execution_commands import (
    ClaimProductGetCommand,
    ClaimPublicationMutationCommand,
    ClaimShopGetCommand,
    DispatchPublicationWorkCommand,
    ReconstructPublicationAuthorityCommand,
    RecordPublicationPostOutcomeCommand,
    RecordPublicationPreflightCommand,
    RecordPublicationProductObservationCommand,
    RecoverConsumedPublicationClaimCommand,
    SettleDefinitivePreflightFailureCommand,
    SettlePublicationDeadlineCommand,
)
from mr_lister.publication.execution_models import (
    PublicationCallClaim,
    PublicationCallKind,
    PublicationCallPurpose,
    PublicationExecutionAuthority,
    PublicationExecutionWorkStatus,
)
from mr_lister.publication.execution_service import PublicationExecutionService
from mr_lister.publication.execution_store import (
    FreshPublicationMutationGrant,
    PublicationExecutionStore,
)
from mr_lister.publication.provider_boundary import StagedPublicationProviderBoundary
from mr_lister.publication.provider_credentials import (
    BoundPublicationProviderCredential,
    build_publication_provider_credential_binding,
)


class PublicationProviderCoordinatorError(RuntimeError):
    """Internal authority was incomplete or contradicted the one-shot state machine."""


class PublicationProviderCoordinatorAction(StrEnum):
    STAGED_SHOP_PREFLIGHT = "staged_shop_preflight"
    STAGED_PRODUCT_PREFLIGHT = "staged_product_preflight"
    STAGED_DEFINITIVE_PREFLIGHT_FAILURE = "staged_definitive_preflight_failure"
    RECORDED_PREFLIGHT = "recorded_preflight"
    SETTLED_DEFINITIVE_PREFLIGHT_FAILURE = "settled_definitive_preflight_failure"
    STAGED_PUBLISH_OUTCOME = "staged_publish_outcome"
    RECORDED_PUBLISH_OUTCOME = "recorded_publish_outcome"
    RECOVERED_CONSUMED_PUBLISH_CLAIM = "recovered_consumed_publish_claim"
    STAGED_PRODUCT_OBSERVATION = "staged_product_observation"
    RECORDED_PRODUCT_OBSERVATION = "recorded_product_observation"
    SETTLED_DEADLINE = "settled_deadline"
    TERMINAL = "terminal"


@dataclass(frozen=True, slots=True)
class PublicationProviderCoordinatorResult:
    action: PublicationProviderCoordinatorAction
    aggregate_state: PublicationState
    operation_id: str | None = None
    stage_id: str | None = None


class PublicationProviderBoundaryFactory(Protocol):
    """Trusted composition seam that owns credentials, routes, transport, audit, and staging."""

    def prepare_credential(
        self,
        *,
        execution_authority: PublicationExecutionAuthority,
    ) -> BoundPublicationProviderCredential: ...

    def __call__(
        self,
        *,
        execution_authority: PublicationExecutionAuthority,
        credential: BoundPublicationProviderCredential,
    ) -> StagedPublicationProviderBoundary: ...


class PublicationPreCallAuthorityGuard(Protocol):
    """Re-read the exact application authority before any coordinator transition."""

    def require_current(
        self,
        *,
        owner_id: str,
        aggregate_id: str,
    ) -> PublicationExecutionAuthority: ...


class PublicationProviderCoordinator:
    """Advance one publication using durable authority and at most one provider request."""

    _TERMINAL_STATES = {
        PublicationState.PUBLISHED,
        PublicationState.PUBLICATION_FAILED,
        PublicationState.PUBLICATION_OUTCOME_UNKNOWN,
    }

    def __init__(
        self,
        *,
        store: PublicationExecutionStore,
        execution: PublicationExecutionService,
        boundary_factory: PublicationProviderBoundaryFactory,
        pre_call_guard: PublicationPreCallAuthorityGuard,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._store = store
        self._execution = execution
        self._boundary_factory = boundary_factory
        self._pre_call_guard = pre_call_guard
        self._clock = clock or (lambda: datetime.now(UTC))

    def advance(
        self,
        *,
        owner_id: str,
        aggregate_id: str,
    ) -> PublicationProviderCoordinatorResult:
        """Advance through local setup/recovery and perform no more than one provider wire."""

        authority = self._store.load_execution_authority(owner_id, aggregate_id)
        if authority.aggregate.state in self._TERMINAL_STATES:
            return self._result(PublicationProviderCoordinatorAction.TERMINAL, authority)
        authority = self._require_current_pre_call_authority(
            authority,
            owner_id=owner_id,
            aggregate_id=aggregate_id,
        )

        now = self._now()
        stages = self._store.list_unconsumed_provider_evidence(owner_id, aggregate_id)

        recovered = self._recover_post_call_stage(authority, stages)
        if recovered is not None:
            return recovered

        if now >= authority.snapshot.verification_deadline:
            if (
                authority.aggregate.state is PublicationState.PUBLICATION_REQUESTED
                and authority.permit.status is PublicationPermitState.CONSUMED
                and authority.post_observation is None
            ):
                return self._recover_consumed_publish_claim(authority)
            operation_id = self._operation_id("deadline", authority)
            self._execution.settle_deadline(
                SettlePublicationDeadlineCommand(
                    **self._command_basis(authority, operation_id),
                )
            )
            current = self._reload(authority)
            return self._result(
                PublicationProviderCoordinatorAction.SETTLED_DEADLINE,
                current,
                operation_id=operation_id,
            )

        authority = self._prepare_local_authority(authority)
        stages = self._store.list_unconsumed_provider_evidence(owner_id, aggregate_id)
        recovered = self._recover_pre_call_stage(authority, stages)
        if recovered is not None:
            return recovered

        if authority.aggregate.state is PublicationState.PUBLICATION_REQUESTED:
            if authority.permit.status is PublicationPermitState.CONSUMED:
                return self._recover_consumed_publish_claim(authority)
            if authority.preflight_proof is None:
                return self._advance_preflight(authority, stages)
            return self._publish_once(authority)

        if authority.aggregate.state in {
            PublicationState.PUBLICATION_VERIFYING,
            PublicationState.PUBLICATION_RECONCILING,
        }:
            return self._poll_once(authority)

        raise PublicationProviderCoordinatorError(
            "Publication coordinator found an unsupported nonterminal state"
        )

    def _require_current_pre_call_authority(
        self,
        observed: PublicationExecutionAuthority,
        *,
        owner_id: str,
        aggregate_id: str,
    ) -> PublicationExecutionAuthority:
        guarded: PublicationExecutionAuthority | None = None
        try:
            candidate = self._pre_call_guard.require_current(
                owner_id=owner_id,
                aggregate_id=aggregate_id,
            )
            exact = PublicationExecutionAuthority.model_validate(
                candidate.model_dump(mode="python")
            )
            if (
                exact != candidate
                or exact != observed
                or exact.snapshot.owner_id != owner_id
                or exact.aggregate.aggregate_id != aggregate_id
            ):
                raise ValueError
            guarded = exact
        except Exception:
            pass
        if guarded is None:
            raise PublicationProviderCoordinatorError(
                "Publication pre-call authority is unavailable"
            ) from None
        return guarded

    def _prepare_local_authority(
        self,
        authority: PublicationExecutionAuthority,
    ) -> PublicationExecutionAuthority:
        if authority.work.status is PublicationExecutionWorkStatus.PENDING:
            operation_id = self._operation_id("dispatch", authority)
            self._execution.dispatch_work(
                DispatchPublicationWorkCommand(
                    **self._command_basis(authority, operation_id),
                )
            )
            authority = self._reload(authority)
        if authority.provider_authority is None:
            if authority.work.status is not PublicationExecutionWorkStatus.DISPATCHED:
                raise PublicationProviderCoordinatorError(
                    "Publication provider authority requires dispatched work"
                )
            operation_id = self._operation_id("reconstruct", authority)
            self._execution.reconstruct_authority(
                ReconstructPublicationAuthorityCommand(
                    **self._command_basis(authority, operation_id),
                )
            )
            authority = self._reload(authority)
        return authority

    def _recover_post_call_stage(
        self,
        authority: PublicationExecutionAuthority,
        stages: tuple[PublicationProviderEvidenceStage, ...],
    ) -> PublicationProviderCoordinatorResult | None:
        if (
            authority.aggregate.state is PublicationState.PUBLICATION_REQUESTED
            and authority.permit.status is PublicationPermitState.CONSUMED
            and authority.post_observation is None
        ):
            stage = self._latest_stage(stages, PublicationProviderEvidenceKind.PUBLISH_OUTCOME)
            if stage is not None:
                operation_id = self._operation_id("record_post", authority, stage.stage_id)
                self._execution.record_post_outcome(
                    RecordPublicationPostOutcomeCommand(
                        **self._command_basis(authority, operation_id),
                        evidence_stage_id=stage.stage_id,
                        evidence_stage_fingerprint=stage.fingerprint,
                    )
                )
                current = self._reload(authority)
                return self._result(
                    PublicationProviderCoordinatorAction.RECORDED_PUBLISH_OUTCOME,
                    current,
                    operation_id=operation_id,
                    stage=stage,
                )

        state_kind = {
            PublicationState.PUBLICATION_VERIFYING: (
                PublicationProviderEvidenceKind.PRODUCT_VERIFICATION
            ),
            PublicationState.PUBLICATION_RECONCILING: (
                PublicationProviderEvidenceKind.PRODUCT_RECONCILIATION
            ),
        }
        kind = state_kind.get(authority.aggregate.state)
        if kind is None:
            return None
        stage = self._latest_stage(stages, kind)
        if stage is None:
            return None
        operation_id = self._operation_id("record_product", authority, stage.stage_id)
        self._execution.record_product_observation(
            RecordPublicationProductObservationCommand(
                **self._command_basis(authority, operation_id),
                evidence_stage_id=stage.stage_id,
                evidence_stage_fingerprint=stage.fingerprint,
            )
        )
        current = self._reload(authority)
        return self._result(
            PublicationProviderCoordinatorAction.RECORDED_PRODUCT_OBSERVATION,
            current,
            operation_id=operation_id,
            stage=stage,
        )

    def _recover_pre_call_stage(
        self,
        authority: PublicationExecutionAuthority,
        stages: tuple[PublicationProviderEvidenceStage, ...],
    ) -> PublicationProviderCoordinatorResult | None:
        if (
            authority.aggregate.state is not PublicationState.PUBLICATION_REQUESTED
            or authority.permit.status is not PublicationPermitState.AVAILABLE
            or authority.preflight_proof is not None
        ):
            return None
        shop = self._latest_preflight_stage(
            stages,
            PublicationCallPurpose.SHOP_PREFLIGHT,
        )
        product = self._latest_preflight_stage(
            stages,
            PublicationCallPurpose.PRODUCT_PREFLIGHT,
        )
        negative = next(
            (
                stage
                for stage in (shop, product)
                if stage is not None
                and stage.evidence_kind
                is PublicationProviderEvidenceKind.DEFINITIVE_PREFLIGHT_NEGATIVE
            ),
            None,
        )
        if negative is not None:
            operation_id = self._operation_id(
                "settle_preflight_negative",
                authority,
                negative.stage_id,
            )
            self._execution.settle_definitive_preflight_failure(
                SettleDefinitivePreflightFailureCommand(
                    **self._command_basis(authority, operation_id),
                    evidence_stage_id=negative.stage_id,
                    evidence_stage_fingerprint=negative.fingerprint,
                )
            )
            current = self._reload(authority)
            return self._result(
                PublicationProviderCoordinatorAction.SETTLED_DEFINITIVE_PREFLIGHT_FAILURE,
                current,
                operation_id=operation_id,
                stage=negative,
            )
        if shop is None or product is None:
            return None
        operation_id = self._operation_id(
            "record_preflight",
            authority,
            f"{shop.stage_id}:{product.stage_id}",
        )
        self._execution.record_preflight(
            RecordPublicationPreflightCommand(
                **self._command_basis(authority, operation_id),
                shop_evidence_stage_id=shop.stage_id,
                shop_evidence_stage_fingerprint=shop.fingerprint,
                product_evidence_stage_id=product.stage_id,
                product_evidence_stage_fingerprint=product.fingerprint,
            )
        )
        current = self._reload(authority)
        return self._result(
            PublicationProviderCoordinatorAction.RECORDED_PREFLIGHT,
            current,
            operation_id=operation_id,
        )

    def _advance_preflight(
        self,
        authority: PublicationExecutionAuthority,
        stages: tuple[PublicationProviderEvidenceStage, ...],
    ) -> PublicationProviderCoordinatorResult:
        shop = self._latest_preflight_stage(
            stages,
            PublicationCallPurpose.SHOP_PREFLIGHT,
        )
        if shop is None:
            credential = self._prepare_credential(authority)
            operation_id = self._operation_id("claim_shop", authority)
            result = self._execution.claim_shop_get(
                ClaimShopGetCommand(
                    **self._command_basis(authority, operation_id),
                )
            )
            claim_authority = self._reload(authority)
            claim = self._latest_claim(claim_authority, PublicationCallKind.SHOP_GET)
            grant = result.fresh_call_grant
            if grant is None:
                raise PublicationProviderCoordinatorError(
                    "Fresh shop claim did not return one-use provider authority"
                )
            candidate = self._boundary_factory(
                execution_authority=claim_authority,
                credential=credential,
            ).preflight_shop(call_claim=claim, fresh_grant=grant)
            stage = self._require_durable_stage(
                claim_authority,
                candidate,
                call_claim=claim,
                expected_kinds={
                    PublicationProviderEvidenceKind.SHOP_PREFLIGHT,
                    PublicationProviderEvidenceKind.DEFINITIVE_PREFLIGHT_NEGATIVE,
                },
            )
            return self._result(
                self._preflight_stage_action(
                    stage,
                    PublicationProviderCoordinatorAction.STAGED_SHOP_PREFLIGHT,
                ),
                self._reload(claim_authority),
                operation_id=operation_id,
                stage=stage,
            )

        product = self._latest_preflight_stage(
            stages,
            PublicationCallPurpose.PRODUCT_PREFLIGHT,
        )
        if product is None:
            credential = self._prepare_credential(authority)
            operation_id = self._operation_id("claim_product_preflight", authority)
            result = self._execution.claim_product_get(
                ClaimProductGetCommand(
                    **self._command_basis(authority, operation_id),
                    purpose=PublicationCallPurpose.PRODUCT_PREFLIGHT,
                )
            )
            claim_authority = self._reload(authority)
            claim = self._latest_claim(claim_authority, PublicationCallKind.PRODUCT_GET)
            grant = result.fresh_call_grant
            if grant is None:
                raise PublicationProviderCoordinatorError(
                    "Fresh product claim did not return one-use provider authority"
                )
            candidate = self._boundary_factory(
                execution_authority=claim_authority,
                credential=credential,
            ).preflight_exact_product(call_claim=claim, fresh_grant=grant)
            stage = self._require_durable_stage(
                claim_authority,
                candidate,
                call_claim=claim,
                expected_kinds={
                    PublicationProviderEvidenceKind.PRODUCT_PREFLIGHT,
                    PublicationProviderEvidenceKind.DEFINITIVE_PREFLIGHT_NEGATIVE,
                },
            )
            return self._result(
                self._preflight_stage_action(
                    stage,
                    PublicationProviderCoordinatorAction.STAGED_PRODUCT_PREFLIGHT,
                ),
                self._reload(claim_authority),
                operation_id=operation_id,
                stage=stage,
            )
        raise PublicationProviderCoordinatorError(
            "Complete staged preflight was not consumed before another provider call"
        )

    def _publish_once(
        self,
        authority: PublicationExecutionAuthority,
    ) -> PublicationProviderCoordinatorResult:
        proof = authority.preflight_proof
        if proof is None:
            raise PublicationProviderCoordinatorError(
                "Publication mutation requires durable preflight proof"
            )
        credential = self._prepare_credential(authority)
        operation_id = self._operation_id("claim_publish", authority)
        result = self._execution.claim_publish(
            ClaimPublicationMutationCommand(
                **self._command_basis(authority, operation_id),
                preflight_proof_id=proof.proof_id,
                preflight_proof_fingerprint=proof.fingerprint,
            )
        )
        claim_authority = self._reload(authority)
        claim = self._latest_claim(claim_authority, PublicationCallKind.PUBLISH_POST)
        mutation = claim_authority.mutation_claim
        exact_proof = claim_authority.preflight_proof
        grant = result.fresh_call_grant
        if (
            mutation is None
            or exact_proof is None
            or not isinstance(grant, FreshPublicationMutationGrant)
        ):
            raise PublicationProviderCoordinatorError(
                "Publish claim did not return exact one-use mutation authority"
            )
        candidate = self._boundary_factory(
            execution_authority=claim_authority,
            credential=credential,
        ).publish_exact_product(
            call_claim=claim,
            mutation_claim=mutation,
            preflight_proof=exact_proof,
            fresh_grant=grant,
        )
        stage = self._require_durable_stage(
            claim_authority,
            candidate,
            call_claim=claim,
            expected_kinds={PublicationProviderEvidenceKind.PUBLISH_OUTCOME},
        )
        return self._result(
            PublicationProviderCoordinatorAction.STAGED_PUBLISH_OUTCOME,
            self._reload(claim_authority),
            operation_id=operation_id,
            stage=stage,
        )

    def _recover_consumed_publish_claim(
        self,
        authority: PublicationExecutionAuthority,
    ) -> PublicationProviderCoordinatorResult:
        mutation = authority.mutation_claim
        if mutation is None:
            raise PublicationProviderCoordinatorError(
                "Consumed publication permit lacks its immutable mutation claim"
            )
        operation_id = self._operation_id("recover_publish", authority)
        self._execution.recover_consumed_claim(
            RecoverConsumedPublicationClaimCommand(
                **self._command_basis(authority, operation_id),
                mutation_claim_id=mutation.mutation_claim_id,
                mutation_claim_fingerprint=mutation.fingerprint,
            )
        )
        current = self._reload(authority)
        return self._result(
            PublicationProviderCoordinatorAction.RECOVERED_CONSUMED_PUBLISH_CLAIM,
            current,
            operation_id=operation_id,
        )

    def _poll_once(
        self,
        authority: PublicationExecutionAuthority,
    ) -> PublicationProviderCoordinatorResult:
        purpose = {
            PublicationState.PUBLICATION_VERIFYING: PublicationCallPurpose.VERIFICATION,
            PublicationState.PUBLICATION_RECONCILING: PublicationCallPurpose.RECONCILIATION,
        }[authority.aggregate.state]
        credential = self._prepare_credential(authority)
        operation_id = self._operation_id(f"claim_{purpose.value}", authority)
        result = self._execution.claim_product_get(
            ClaimProductGetCommand(
                **self._command_basis(authority, operation_id),
                purpose=purpose,
            )
        )
        claim_authority = self._reload(authority)
        claim = self._latest_claim(claim_authority, PublicationCallKind.PRODUCT_GET)
        grant = result.fresh_call_grant
        if grant is None:
            raise PublicationProviderCoordinatorError(
                "Fresh product poll did not return one-use provider authority"
            )
        candidate = self._boundary_factory(
            execution_authority=claim_authority,
            credential=credential,
        ).poll_exact_product(call_claim=claim, fresh_grant=grant)
        expected_kind = {
            PublicationCallPurpose.VERIFICATION: (
                PublicationProviderEvidenceKind.PRODUCT_VERIFICATION
            ),
            PublicationCallPurpose.RECONCILIATION: (
                PublicationProviderEvidenceKind.PRODUCT_RECONCILIATION
            ),
        }[purpose]
        stage = self._require_durable_stage(
            claim_authority,
            candidate,
            call_claim=claim,
            expected_kinds={expected_kind},
        )
        return self._result(
            PublicationProviderCoordinatorAction.STAGED_PRODUCT_OBSERVATION,
            self._reload(claim_authority),
            operation_id=operation_id,
            stage=stage,
        )

    def _reload(
        self,
        authority: PublicationExecutionAuthority,
    ) -> PublicationExecutionAuthority:
        return self._store.load_execution_authority(
            authority.snapshot.owner_id,
            authority.aggregate.aggregate_id,
        )

    def _prepare_credential(
        self,
        authority: PublicationExecutionAuthority,
    ) -> BoundPublicationProviderCredential:
        try:
            provider_authority = authority.provider_authority
            if provider_authority is None:
                raise ValueError
            credential = self._boundary_factory.prepare_credential(
                execution_authority=authority,
            )
            if (
                type(credential) is not BoundPublicationProviderCredential
                or credential.binding
                != build_publication_provider_credential_binding(provider_authority)
            ):
                raise ValueError
        except Exception:
            pass
        else:
            return credential
        raise PublicationProviderCoordinatorError(
            "Publication provider credential is unavailable"
        ) from None

    def _require_durable_stage(
        self,
        authority: PublicationExecutionAuthority,
        candidate: PublicationProviderEvidenceStage,
        *,
        call_claim: PublicationCallClaim,
        expected_kinds: set[PublicationProviderEvidenceKind],
    ) -> PublicationProviderEvidenceStage:
        stage: PublicationProviderEvidenceStage | None = None
        invalid = False
        try:
            exact = PublicationProviderEvidenceStage.model_validate(
                candidate.model_dump(mode="python")
            )
            persisted = self._store.get_provider_evidence_stage(
                authority.snapshot.owner_id,
                authority.aggregate.aggregate_id,
                exact.stage_id,
            )
            if persisted == exact:
                stage = exact
        except Exception:
            invalid = True
        if (
            invalid
            or stage is None
            or stage.aggregate_id != authority.aggregate.aggregate_id
            or stage.call_claim_id != call_claim.authorization_id
            or stage.call_claim_fingerprint != call_claim.fingerprint
            or stage.evidence_kind not in expected_kinds
        ):
            raise PublicationProviderCoordinatorError(
                "Provider boundary did not return its exact durable evidence stage"
            ) from None
        return stage

    @staticmethod
    def _preflight_stage_action(
        stage: PublicationProviderEvidenceStage,
        positive_action: PublicationProviderCoordinatorAction,
    ) -> PublicationProviderCoordinatorAction:
        if stage.evidence_kind is PublicationProviderEvidenceKind.DEFINITIVE_PREFLIGHT_NEGATIVE:
            return PublicationProviderCoordinatorAction.STAGED_DEFINITIVE_PREFLIGHT_FAILURE
        return positive_action

    @staticmethod
    def _latest_stage(
        stages: tuple[PublicationProviderEvidenceStage, ...],
        kind: PublicationProviderEvidenceKind,
    ) -> PublicationProviderEvidenceStage | None:
        matching = tuple(stage for stage in stages if stage.evidence_kind is kind)
        return matching[-1] if matching else None

    @staticmethod
    def _latest_preflight_stage(
        stages: tuple[PublicationProviderEvidenceStage, ...],
        purpose: PublicationCallPurpose,
    ) -> PublicationProviderEvidenceStage | None:
        positive_kind = {
            PublicationCallPurpose.SHOP_PREFLIGHT: PublicationProviderEvidenceKind.SHOP_PREFLIGHT,
            PublicationCallPurpose.PRODUCT_PREFLIGHT: (
                PublicationProviderEvidenceKind.PRODUCT_PREFLIGHT
            ),
        }[purpose]
        matching = tuple(
            stage
            for stage in stages
            if stage.call_purpose is purpose
            and stage.evidence_kind
            in {
                positive_kind,
                PublicationProviderEvidenceKind.DEFINITIVE_PREFLIGHT_NEGATIVE,
            }
        )
        return matching[-1] if matching else None

    @staticmethod
    def _latest_claim(
        authority: PublicationExecutionAuthority,
        kind: PublicationCallKind,
    ) -> PublicationCallClaim:
        matching = tuple(claim for claim in authority.call_claims if claim.call_kind is kind)
        if not matching:
            raise PublicationProviderCoordinatorError(
                "Execution transition did not persist its provider call claim"
            )
        return matching[-1]

    @staticmethod
    def _command_basis(
        authority: PublicationExecutionAuthority,
        operation_id: str,
    ) -> dict[str, object]:
        return {
            "owner_id": authority.snapshot.owner_id,
            "aggregate_id": authority.aggregate.aggregate_id,
            "operation_id": operation_id,
            "expected_aggregate_record_version": authority.aggregate.record_version,
            "expected_aggregate_fingerprint": authority.aggregate.fingerprint,
            "expected_provider_evidence_record_version": (
                authority.aggregate.provider_evidence_record_version
            ),
            "expected_attempt_record_version": authority.attempt.record_version,
            "expected_permit_record_version": authority.permit.record_version,
            "expected_work_record_version": authority.work.record_version,
        }

    @staticmethod
    def _operation_id(
        action: str,
        authority: PublicationExecutionAuthority,
        discriminator: str = "",
    ) -> str:
        material = ":".join(
            (
                "phase7.3",
                action,
                authority.aggregate.aggregate_id,
                str(authority.aggregate.record_version),
                str(authority.attempt.record_version),
                str(authority.permit.record_version),
                str(authority.work.record_version),
                discriminator,
            )
        )
        digest = sha256(material.encode("utf-8")).hexdigest()[:40]
        return f"phase73_{action[:32]}_{digest}"

    @staticmethod
    def _result(
        action: PublicationProviderCoordinatorAction,
        authority: PublicationExecutionAuthority,
        *,
        operation_id: str | None = None,
        stage: PublicationProviderEvidenceStage | None = None,
    ) -> PublicationProviderCoordinatorResult:
        return PublicationProviderCoordinatorResult(
            action=action,
            aggregate_state=authority.aggregate.state,
            operation_id=operation_id,
            stage_id=stage.stage_id if stage is not None else None,
        )

    def _now(self) -> datetime:
        value = self._clock()
        if (
            not isinstance(value, datetime)
            or value.tzinfo is None
            or value.utcoffset() != timedelta(0)
        ):
            raise PublicationProviderCoordinatorError(
                "Publication coordinator clock must be UTC-aware"
            )
        return value.astimezone(UTC)
