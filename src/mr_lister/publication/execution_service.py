"""Provider-free Phase 7.2 execution transition service.

The service may create durable call claims and fresh single-use grants.  It never opens a socket,
constructs a provider client, dispatches workflow infrastructure, or changes a Phase 6 state.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from re import fullmatch
from typing import Protocol, TypeVar

from pydantic import ValidationError

from mr_lister.control.fingerprints import (
    canonical_fingerprint as control_fingerprint,
)
from mr_lister.control.fingerprints import (
    publication_terminal_summary_fingerprint,
)
from mr_lister.control.models import ControlJobRecord
from mr_lister.publication.contract import PublicationPermitState, PublicationState
from mr_lister.publication.errors import (
    PublicationConflictError,
    PublicationErrorCode,
    PublicationIdempotencyConflictError,
)
from mr_lister.publication.execution_commands import (
    ClaimProductGetCommand,
    ClaimPublicationMutationCommand,
    ClaimShopGetCommand,
    DispatchPublicationWorkCommand,
    PublicationExecutionCommand,
    ReconstructPublicationAuthorityCommand,
    RecordPublicationPostOutcomeCommand,
    RecordPublicationPreflightCommand,
    RecordPublicationProductObservationCommand,
    RecoverConsumedPublicationClaimCommand,
    SettlePublicationDeadlineCommand,
)
from mr_lister.publication.execution_fingerprints import (
    execution_record_fingerprint,
    execution_request_fingerprint,
    publication_mockup_fingerprint,
    safe_listing_link_fingerprint,
)
from mr_lister.publication.execution_models import (
    ExecutionPublicationAggregate,
    ExecutionPublicationAttempt,
    ExecutionPublicationPermit,
    ExecutionPublicationWork,
    ExpectedVariantEconomics,
    PublicationAggregateTombstone,
    PublicationAttemptStatus,
    PublicationCallClaim,
    PublicationCallKind,
    PublicationCallPurpose,
    PublicationExecutionAuthority,
    PublicationExecutionEvent,
    PublicationExecutionEventName,
    PublicationExecutionOperation,
    PublicationExecutionReceipt,
    PublicationExecutionWorkStatus,
    PublicationExternalEvidenceState,
    PublicationMutationClaim,
    PublicationNotification,
    PublicationPermitRetirementReason,
    PublicationPostObservation,
    PublicationPostOutcome,
    PublicationPreflightProof,
    PublicationProductObservation,
    PublicationProviderAuthority,
    PublicationPublishResponseCategory,
    PublicationReadOutcome,
    PublicationResult,
    PublicationTerminalJobLink,
    PublicationTerminalReason,
    PublicationTerminalReport,
)
from mr_lister.publication.execution_store import (
    PublicationExecutionCommit,
    PublicationExecutionCommitResult,
    PublicationExecutionStore,
    PublicationTerminalJobUpdate,
)
from mr_lister.publication.fingerprints import publication_body_fingerprint
from mr_lister.publication.models import PublicationModel
from mr_lister.publication.store import (
    PublicationRequestAuthority,
    validate_publication_request_authority,
)
from mr_lister.review_profile import ExactReviewProductProfile, ReviewProfileNotFoundError

CommandT = TypeVar("CommandT", bound=PublicationExecutionCommand)
RecordT = TypeVar("RecordT", bound=PublicationModel)


class PublicationExecutionProfileAuthority(Protocol):
    def get_exact(
        self,
        *,
        profile_id: str,
        profile_version: int,
    ) -> ExactReviewProductProfile: ...


class PublicationExecutionService:
    """Application-owned execution state machine with no provider capability."""

    def __init__(
        self,
        store: PublicationExecutionStore,
        *,
        profiles: PublicationExecutionProfileAuthority,
        release_manifest_fingerprint: str,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if (
            fullmatch(r"[a-f0-9]{64}", release_manifest_fingerprint) is None
            or release_manifest_fingerprint == "0" * 64
        ):
            raise ValueError("A nonzero release manifest fingerprint is required")
        self._store = store
        self._profiles = profiles
        self._release_manifest_fingerprint = release_manifest_fingerprint
        self._clock = clock or (lambda: datetime.now(UTC))

    def dispatch_work(
        self,
        command: DispatchPublicationWorkCommand,
    ) -> PublicationExecutionCommitResult:
        context = self._begin(command, PublicationExecutionOperation.DISPATCH)
        if isinstance(context, PublicationExecutionCommitResult):
            return context
        authority, request_fingerprint, now = context
        if authority.work.status is not PublicationExecutionWorkStatus.PENDING:
            self._invalid_transition("Publication work is not pending")
        aggregate = self._evolve_aggregate(authority, now=now)
        work = self._evolve_work(
            authority,
            now=now,
            status=PublicationExecutionWorkStatus.DISPATCHED,
            attempt_count=1,
            next_dispatch_at=None,
            dispatched_at=now,
        )
        return self._commit(
            command,
            authority,
            request_fingerprint,
            now,
            operation=PublicationExecutionOperation.DISPATCH,
            event_name=PublicationExecutionEventName.WORK_DISPATCHED,
            authority_record_id=work.work_request_id,
            authority_fingerprint=work.fingerprint,
            aggregate=aggregate,
            attempt=authority.attempt,
            permit=authority.permit,
            work=work,
        )

    def claim_shop_get(
        self,
        command: ClaimShopGetCommand,
    ) -> PublicationExecutionCommitResult:
        return self._claim_get(
            command,
            operation=PublicationExecutionOperation.CLAIM_SHOP_GET,
            kind=PublicationCallKind.SHOP_GET,
            purpose=PublicationCallPurpose.SHOP_PREFLIGHT,
        )

    def reconstruct_authority(
        self,
        command: ReconstructPublicationAuthorityCommand,
    ) -> PublicationExecutionCommitResult:
        context = self._begin(
            command,
            PublicationExecutionOperation.RECONSTRUCT_AUTHORITY,
        )
        if isinstance(context, PublicationExecutionCommitResult):
            return context
        authority, request_fingerprint, now = context
        self._require_requested_dispatched(authority)
        self._require_before_deadline(authority, now)
        if authority.provider_authority is not None or authority.call_claims:
            self._invalid_transition("Provider authority can be reconstructed only once pre-call")
        source = self._store.load_source_authority(
            command.owner_id,
            command.aggregate_id,
        )
        self._validate_live_provider_authority(authority, source, now)
        values = {
            "provider_authority_id": self._stable_id(
                "provider_authority",
                authority.aggregate.aggregate_id,
            ),
            "aggregate_id": authority.aggregate.aggregate_id,
            "attempt_id": authority.attempt.attempt_id,
            "snapshot_id": authority.snapshot.snapshot_id,
            "snapshot_fingerprint": authority.snapshot.fingerprint,
            "owner_id": authority.snapshot.owner_id,
            "job_id": authority.snapshot.job_id,
            "permit_id": authority.permit.permit_id,
            "work_request_id": authority.work.work_request_id,
            "phase6_record_version": authority.phase6_record_version,
            "approval_fingerprint": source.current_job.approval_fingerprint,
            "review_fingerprint": source.review.fingerprint,
            "product_sync_fingerprint": source.product_sync.fingerprint,
            "pricing_snapshot_fingerprint": source.pricing_snapshot.fingerprint,
            "pricing_evidence_fingerprint": source.pricing_evidence.fingerprint,
            "profile_fingerprint": source.source.product_profile_fingerprint,
            "release_manifest_fingerprint": self._release_manifest_fingerprint,
            "printify_shop_id": source.product_sync.printify_shop_id,
            "printify_product_id": source.product_sync.product_id,
            "printify_image_id": source.product_sync.image_id,
            "product_payload_fingerprint": source.product_sync.payload_fingerprint,
            "expected_variant_economics": tuple(
                ExpectedVariantEconomics(
                    variant_id=variant.variant_id,
                    retail_price_cents=variant.retail_price_cents,
                    production_cost_cents=variant.production_cost_cents,
                )
                for variant in source.product_sync.variants
            ),
            "expected_mockup_fingerprints": tuple(
                sorted(
                    publication_mockup_fingerprint(
                        url=mockup.url,
                        position=mockup.position,
                        variant_ids=mockup.variant_ids,
                    )
                    for mockup in source.product_sync.mockups
                )
            ),
            "expected_sales_channel": authority.snapshot.expected_sales_channel,
            "publication_body_fingerprint": publication_body_fingerprint(),
            "pricing_fresh_until": authority.snapshot.pricing_fresh_until,
            "reconstructed_at": now,
            "verification_deadline": authority.snapshot.verification_deadline,
        }
        provider_authority = self._record(
            PublicationProviderAuthority,
            "provider_authority",
            values,
        )
        aggregate = self._evolve_aggregate(authority, now=now)
        work = self._evolve_work(authority, now=now)
        return self._commit(
            command,
            authority,
            request_fingerprint,
            now,
            operation=PublicationExecutionOperation.RECONSTRUCT_AUTHORITY,
            event_name=PublicationExecutionEventName.PROVIDER_AUTHORITY_RECONSTRUCTED,
            authority_record_id=provider_authority.provider_authority_id,
            authority_fingerprint=provider_authority.fingerprint,
            aggregate=aggregate,
            attempt=authority.attempt,
            permit=authority.permit,
            work=work,
            new_provider_authority=provider_authority,
        )

    def claim_product_get(
        self,
        command: ClaimProductGetCommand,
    ) -> PublicationExecutionCommitResult:
        return self._claim_get(
            command,
            operation=PublicationExecutionOperation.CLAIM_PRODUCT_GET,
            kind=PublicationCallKind.PRODUCT_GET,
            purpose=command.purpose,
        )

    def record_preflight(
        self,
        command: RecordPublicationPreflightCommand,
    ) -> PublicationExecutionCommitResult:
        context = self._begin(command, PublicationExecutionOperation.RECORD_PREFLIGHT)
        if isinstance(context, PublicationExecutionCommitResult):
            return context
        authority, request_fingerprint, now = context
        self._require_requested_dispatched(authority)
        if authority.permit.status is not PublicationPermitState.AVAILABLE:
            self._permit_unavailable()
        if authority.preflight_proof is not None:
            self._invalid_transition("Preflight proof already exists")
        shop_evidence = command.shop_evidence
        product_evidence = command.product_evidence
        claims = {claim.authorization_id: claim for claim in authority.call_claims}
        shop_claim = claims.get(shop_evidence.call_claim_id)
        product_claim = claims.get(product_evidence.call_claim_id)
        if (
            shop_claim is None
            or shop_claim.call_kind is not PublicationCallKind.SHOP_GET
            or shop_claim.fingerprint != shop_evidence.call_claim_fingerprint
            or product_claim is None
            or product_claim.call_kind is not PublicationCallKind.PRODUCT_GET
            or product_claim.purpose is not PublicationCallPurpose.PRODUCT_PREFLIGHT
            or product_claim.fingerprint != product_evidence.call_claim_fingerprint
        ):
            self._invalid_authority("Preflight command does not bind exact GET claims")
        self._require_before_deadline(authority, now)
        provider_authority = authority.provider_authority
        if provider_authority is None:
            self._invalid_authority("Provider authority was not reconstructed")
        if (
            shop_evidence.provider_authority_id != provider_authority.provider_authority_id
            or shop_evidence.provider_authority_fingerprint != provider_authority.fingerprint
            or product_evidence.provider_authority_id != provider_authority.provider_authority_id
            or product_evidence.provider_authority_fingerprint != provider_authority.fingerprint
            or shop_evidence.printify_shop_id != authority.snapshot.printify_shop_id
            or product_evidence.printify_shop_id != authority.snapshot.printify_shop_id
            or product_evidence.printify_product_id != authority.snapshot.printify_product_id
            or product_evidence.canonical_payload_fingerprint
            != provider_authority.product_payload_fingerprint
            or not product_evidence.preflight_satisfied
            or product_evidence.read_outcome is PublicationReadOutcome.POSITIVE_PROOF
            or shop_evidence.observed_at < shop_claim.authorized_at
            or product_evidence.observed_at < product_claim.authorized_at
            or max(shop_evidence.observed_at, product_evidence.observed_at) > now
            or max(shop_evidence.observed_at, product_evidence.observed_at)
            >= authority.snapshot.verification_deadline
        ):
            self._invalid_authority("Provider evidence does not prove exact complete preflight")
        values = {
            "proof_id": self._stable_id("preflight", authority.aggregate.aggregate_id),
            "aggregate_id": authority.aggregate.aggregate_id,
            "attempt_id": authority.attempt.attempt_id,
            "snapshot_id": authority.snapshot.snapshot_id,
            "snapshot_fingerprint": authority.snapshot.fingerprint,
            "provider_authority_id": provider_authority.provider_authority_id,
            "provider_authority_fingerprint": provider_authority.fingerprint,
            "shop_evidence_fingerprint": shop_evidence.fingerprint,
            "product_evidence_fingerprint": product_evidence.fingerprint,
            "shop_observed_at": shop_evidence.observed_at,
            "product_observed_at": product_evidence.observed_at,
            "shop_call_claim_id": shop_claim.authorization_id,
            "shop_call_claim_fingerprint": shop_claim.fingerprint,
            "product_call_claim_id": product_claim.authorization_id,
            "product_call_claim_fingerprint": product_claim.fingerprint,
            "printify_shop_id": authority.snapshot.printify_shop_id,
            "printify_product_id": authority.snapshot.printify_product_id,
            "local_authority_reconstructed": True,
            "shop_connected_to_etsy": True,
            "exact_product_match": product_evidence.product_present,
            "canonical_content_match": product_evidence.canonical_content_match,
            "exact_variants_match": (
                product_evidence.exact_variant_economics
                and product_evidence.exact_placement_image
                and product_evidence.exact_mockups
            ),
            "product_unlocked": product_evidence.is_locked is False,
            "product_unpublished": (
                product_evidence.external_evidence is PublicationExternalEvidenceState.ABSENT
            ),
            "publication_body_fingerprint": publication_body_fingerprint(),
            "proven_at": now,
            "verification_deadline": authority.snapshot.verification_deadline,
        }
        proof = self._record(PublicationPreflightProof, "preflight_proof", values)
        aggregate = self._evolve_aggregate(authority, now=now)
        work = self._evolve_work(authority, now=now)
        return self._commit(
            command,
            authority,
            request_fingerprint,
            now,
            operation=PublicationExecutionOperation.RECORD_PREFLIGHT,
            event_name=PublicationExecutionEventName.PREFLIGHT_PROVEN,
            authority_record_id=proof.proof_id,
            authority_fingerprint=proof.fingerprint,
            aggregate=aggregate,
            attempt=authority.attempt,
            permit=authority.permit,
            work=work,
            new_preflight_proof=proof,
        )

    def claim_publish(
        self,
        command: ClaimPublicationMutationCommand,
    ) -> PublicationExecutionCommitResult:
        context = self._begin(command, PublicationExecutionOperation.CLAIM_PUBLISH)
        if isinstance(context, PublicationExecutionCommitResult):
            return context
        authority, request_fingerprint, now = context
        self._require_requested_dispatched(authority)
        self._require_before_deadline(authority, now)
        if authority.permit.status is not PublicationPermitState.AVAILABLE:
            self._permit_unavailable()
        proof = authority.preflight_proof
        if (
            proof is None
            or proof.proof_id != command.preflight_proof_id
            or proof.fingerprint != command.preflight_proof_fingerprint
        ):
            self._invalid_authority("Publish claim requires the exact durable preflight proof")
        source = self._store.load_source_authority(
            command.owner_id,
            command.aggregate_id,
        )
        self._validate_live_provider_authority(authority, source, now)
        if now >= authority.snapshot.pricing_fresh_until:
            raise PublicationConflictError(
                PublicationErrorCode.PRICING_NOT_FRESH,
                "Pricing authority expired before permit consumption",
            )
        if authority.attempt.publish_post_call_count != 0:
            self._budget_exhausted("The sole publish POST was already claimed")

        operation_id = command.operation_id
        call_claim_id = self._stable_id("publish_call", operation_id)
        mutation_claim_id = self._stable_id("mutation", authority.aggregate.aggregate_id)
        attempt = self._evolve_attempt(
            authority,
            publish_post_call_count=1,
            record_version=authority.attempt.record_version + 1,
        )
        permit = self._evolve_permit(
            authority,
            status=PublicationPermitState.CONSUMED,
            record_version=1,
            consumed_at=now,
            mutation_claim_id=mutation_claim_id,
        )
        claim_values = self._call_claim_values(
            authority,
            operation_id=operation_id,
            authorization_id=call_claim_id,
            kind=PublicationCallKind.PUBLISH_POST,
            purpose=PublicationCallPurpose.PUBLISH,
            ordinal=1,
            resulting_attempt_version=attempt.record_version,
            authorized_at=now,
            permit_fingerprint=permit.fingerprint,
        )
        call_claim = self._record(PublicationCallClaim, "call_claim", claim_values)
        mutation_values = {
            "mutation_claim_id": mutation_claim_id,
            "call_claim_id": call_claim.authorization_id,
            "call_claim_fingerprint": call_claim.fingerprint,
            "aggregate_id": authority.aggregate.aggregate_id,
            "attempt_id": authority.attempt.attempt_id,
            "snapshot_id": authority.snapshot.snapshot_id,
            "snapshot_fingerprint": authority.snapshot.fingerprint,
            "permit_id": authority.permit.permit_id,
            "work_request_id": authority.work.work_request_id,
            "preflight_proof_id": proof.proof_id,
            "preflight_proof_fingerprint": proof.fingerprint,
            "consumed_permit_fingerprint": permit.fingerprint,
            "publication_body_fingerprint": publication_body_fingerprint(),
            "ordinal": 1,
            "authorized_at": now,
            "verification_deadline": authority.snapshot.verification_deadline,
        }
        mutation = self._record(PublicationMutationClaim, "mutation_claim", mutation_values)
        aggregate = self._evolve_aggregate(authority, now=now)
        work = self._evolve_work(authority, now=now)
        return self._commit(
            command,
            authority,
            request_fingerprint,
            now,
            operation=PublicationExecutionOperation.CLAIM_PUBLISH,
            event_name=PublicationExecutionEventName.PUBLISH_CLAIMED,
            authority_record_id=mutation.mutation_claim_id,
            authority_fingerprint=mutation.fingerprint,
            aggregate=aggregate,
            attempt=attempt,
            permit=permit,
            work=work,
            new_call_claim=call_claim,
            new_mutation_claim=mutation,
        )

    def record_post_outcome(
        self,
        command: RecordPublicationPostOutcomeCommand,
    ) -> PublicationExecutionCommitResult:
        context = self._begin(command, PublicationExecutionOperation.RECORD_POST_OUTCOME)
        if isinstance(context, PublicationExecutionCommitResult):
            return context
        authority, request_fingerprint, now = context
        if (
            authority.aggregate.state is not PublicationState.PUBLICATION_REQUESTED
            or authority.permit.status is not PublicationPermitState.CONSUMED
            or authority.work.status is not PublicationExecutionWorkStatus.DISPATCHED
        ):
            self._invalid_transition("POST outcome can settle only the consumed requested state")
        evidence = command.evidence
        mutation = authority.mutation_claim
        provider_authority = authority.provider_authority
        publish_claim = next(
            (
                claim
                for claim in authority.call_claims
                if claim.call_kind is PublicationCallKind.PUBLISH_POST
            ),
            None,
        )
        if (
            mutation is None
            or provider_authority is None
            or publish_claim is None
            or mutation.mutation_claim_id != evidence.mutation_claim_id
            or mutation.fingerprint != evidence.mutation_claim_fingerprint
            or publish_claim.authorization_id != evidence.call_claim_id
            or publish_claim.fingerprint != evidence.call_claim_fingerprint
            or provider_authority.provider_authority_id != evidence.provider_authority_id
            or provider_authority.fingerprint != evidence.provider_authority_fingerprint
            or evidence.observed_at < mutation.authorized_at
            or evidence.observed_at > now
        ):
            self._invalid_authority("POST evidence does not bind exact audited mutation authority")
        values = {
            "observation_id": self._stable_id("post_observation", mutation.mutation_claim_id),
            "aggregate_id": authority.aggregate.aggregate_id,
            "attempt_id": authority.attempt.attempt_id,
            "mutation_claim_id": mutation.mutation_claim_id,
            "call_claim_id": publish_claim.authorization_id,
            "call_claim_fingerprint": publish_claim.fingerprint,
            "mutation_claim_fingerprint": mutation.fingerprint,
            "provider_authority_id": provider_authority.provider_authority_id,
            "provider_authority_fingerprint": provider_authority.fingerprint,
            "provider_evidence_fingerprint": evidence.fingerprint,
            "outcome": evidence.outcome,
            "response_category": evidence.response_category,
            "sanitized_response_fingerprint": evidence.sanitized_response_fingerprint,
            "provider_outcome_uncertain": (evidence.outcome is PublicationPostOutcome.AMBIGUOUS),
            "observed_at": evidence.observed_at,
        }
        observation = self._record(PublicationPostObservation, "post_observation", values)
        if evidence.outcome is PublicationPostOutcome.DEFINITELY_ACCEPTED:
            state = PublicationState.PUBLICATION_VERIFYING
            work_status = PublicationExecutionWorkStatus.VERIFYING
            event_name = PublicationExecutionEventName.PUBLICATION_VERIFYING
            aggregate = self._evolve_aggregate(
                authority,
                now=now,
                state=state,
                last_observation_fingerprint=observation.fingerprint,
            )
            work = self._evolve_work(authority, now=now, status=work_status)
            terminal: dict[str, object] = {}
            attempt = authority.attempt
        else:
            state = PublicationState.PUBLICATION_RECONCILING
            work_status = PublicationExecutionWorkStatus.RECONCILING
            event_name = PublicationExecutionEventName.PUBLICATION_RECONCILING
            aggregate = self._evolve_aggregate(
                authority,
                now=now,
                state=state,
                last_observation_fingerprint=observation.fingerprint,
            )
            work = self._evolve_work(authority, now=now, status=work_status)
            terminal = {}
            attempt = authority.attempt
        return self._commit(
            command,
            authority,
            request_fingerprint,
            now,
            operation=PublicationExecutionOperation.RECORD_POST_OUTCOME,
            event_name=event_name,
            authority_record_id=observation.observation_id,
            authority_fingerprint=observation.fingerprint,
            aggregate=aggregate,
            attempt=attempt,
            permit=authority.permit,
            work=work,
            new_post_observation=observation,
            **terminal,
        )

    def recover_consumed_claim(
        self,
        command: RecoverConsumedPublicationClaimCommand,
    ) -> PublicationExecutionCommitResult:
        """Conservatively reconcile a POST claim lost before boundary audit or wire."""

        context = self._begin(
            command,
            PublicationExecutionOperation.RECOVER_CONSUMED_CLAIM,
        )
        if isinstance(context, PublicationExecutionCommitResult):
            return context
        authority, request_fingerprint, now = context
        mutation = authority.mutation_claim
        provider_authority = authority.provider_authority
        publish_claim = next(
            (
                claim
                for claim in authority.call_claims
                if claim.call_kind is PublicationCallKind.PUBLISH_POST
            ),
            None,
        )
        if (
            authority.aggregate.state is not PublicationState.PUBLICATION_REQUESTED
            or authority.permit.status is not PublicationPermitState.CONSUMED
            or authority.work.status is not PublicationExecutionWorkStatus.DISPATCHED
            or mutation is None
            or provider_authority is None
            or publish_claim is None
            or mutation.mutation_claim_id != command.mutation_claim_id
            or mutation.fingerprint != command.mutation_claim_fingerprint
        ):
            self._invalid_transition(
                "Consumed-claim recovery requires one POST claim without durable response evidence"
            )
        values = {
            "observation_id": self._stable_id("post_observation", mutation.mutation_claim_id),
            "aggregate_id": authority.aggregate.aggregate_id,
            "attempt_id": authority.attempt.attempt_id,
            "mutation_claim_id": mutation.mutation_claim_id,
            "call_claim_id": publish_claim.authorization_id,
            "call_claim_fingerprint": publish_claim.fingerprint,
            "mutation_claim_fingerprint": mutation.fingerprint,
            "provider_authority_id": provider_authority.provider_authority_id,
            "provider_authority_fingerprint": provider_authority.fingerprint,
            "provider_evidence_fingerprint": None,
            "outcome": PublicationPostOutcome.AMBIGUOUS,
            "response_category": (
                PublicationPublishResponseCategory.CONSUMED_CLAIM_WITHOUT_DURABLE_BOUNDARY_OBSERVATION
            ),
            "sanitized_response_fingerprint": None,
            "provider_outcome_uncertain": True,
            "observed_at": now,
        }
        observation = self._record(PublicationPostObservation, "post_observation", values)
        aggregate = self._evolve_aggregate(
            authority,
            now=now,
            state=PublicationState.PUBLICATION_RECONCILING,
            last_observation_fingerprint=observation.fingerprint,
        )
        work = self._evolve_work(
            authority,
            now=now,
            status=PublicationExecutionWorkStatus.RECONCILING,
        )
        return self._commit(
            command,
            authority,
            request_fingerprint,
            now,
            operation=PublicationExecutionOperation.RECOVER_CONSUMED_CLAIM,
            event_name=PublicationExecutionEventName.PUBLICATION_RECONCILING,
            authority_record_id=observation.observation_id,
            authority_fingerprint=observation.fingerprint,
            aggregate=aggregate,
            attempt=authority.attempt,
            permit=authority.permit,
            work=work,
            new_post_observation=observation,
        )

    def record_product_observation(
        self,
        command: RecordPublicationProductObservationCommand,
    ) -> PublicationExecutionCommitResult:
        context = self._begin(
            command,
            PublicationExecutionOperation.RECORD_PRODUCT_OBSERVATION,
        )
        if isinstance(context, PublicationExecutionCommitResult):
            return context
        authority, request_fingerprint, now = context
        state_purpose = {
            PublicationState.PUBLICATION_VERIFYING: PublicationCallPurpose.VERIFICATION,
            PublicationState.PUBLICATION_RECONCILING: PublicationCallPurpose.RECONCILIATION,
        }
        purpose = state_purpose.get(authority.aggregate.state)
        if purpose is None or authority.permit.status is not PublicationPermitState.CONSUMED:
            self._invalid_transition("Product observations require verifying or reconciling state")
        evidence = command.evidence
        claim = next(
            (
                candidate
                for candidate in authority.call_claims
                if candidate.authorization_id == evidence.call_claim_id
            ),
            None,
        )
        if (
            claim is None
            or claim.call_kind is not PublicationCallKind.PRODUCT_GET
            or claim.purpose is not purpose
            or claim.fingerprint != evidence.call_claim_fingerprint
        ):
            self._invalid_authority("Product observation does not bind a state-specific GET claim")
        if any(
            observation.call_claim_id == claim.authorization_id
            for observation in authority.product_observations
        ):
            self._invalid_transition("Product GET claim already has an observation")
        provider_authority = authority.provider_authority
        if (
            provider_authority is None
            or evidence.provider_authority_id != provider_authority.provider_authority_id
            or evidence.provider_authority_fingerprint != provider_authority.fingerprint
            or evidence.printify_shop_id != authority.snapshot.printify_shop_id
            or evidence.printify_product_id != authority.snapshot.printify_product_id
            or (
                evidence.canonical_content_match
                and evidence.canonical_payload_fingerprint
                != provider_authority.product_payload_fingerprint
            )
            or evidence.observed_at < claim.authorized_at
            or evidence.observed_at > now
        ):
            self._invalid_authority("Product evidence differs from exact provider authority")
        if (
            evidence.read_outcome is PublicationReadOutcome.POSITIVE_PROOF
            and evidence.observed_at >= authority.snapshot.verification_deadline
        ):
            self._deadline_expired("Positive proof arrived after the fixed deadline")
        values = {
            "observation_id": self._stable_id("product_observation", claim.authorization_id),
            "aggregate_id": authority.aggregate.aggregate_id,
            "attempt_id": authority.attempt.attempt_id,
            "snapshot_id": authority.snapshot.snapshot_id,
            "snapshot_fingerprint": authority.snapshot.fingerprint,
            "call_claim_id": claim.authorization_id,
            "call_claim_fingerprint": claim.fingerprint,
            "provider_authority_id": provider_authority.provider_authority_id,
            "provider_authority_fingerprint": provider_authority.fingerprint,
            "provider_evidence_fingerprint": evidence.fingerprint,
            "sanitized_response_fingerprint": evidence.sanitized_response_fingerprint,
            "outcome": evidence.read_outcome,
            "exact_shop": evidence.printify_shop_id == authority.snapshot.printify_shop_id,
            "exact_product": (
                evidence.product_present
                and evidence.printify_product_id == authority.snapshot.printify_product_id
            ),
            "unlocked": evidence.is_locked is False,
            "visible": evidence.visible is True,
            "canonical_content_match": evidence.canonical_content_match,
            "single_etsy_external_reference": (
                evidence.external_evidence
                is PublicationExternalEvidenceState.SINGLE_NUMERIC_ETSY_REFERENCE
            ),
            "no_conflicting_external_reference": (
                evidence.external_evidence
                is not PublicationExternalEvidenceState.CONFLICTING_OR_INCOMPLETE
            ),
            "numeric_listing_id": evidence.numeric_listing_id,
            "verified_product_fingerprint": (
                evidence.fingerprint
                if evidence.read_outcome is PublicationReadOutcome.POSITIVE_PROOF
                else None
            ),
            "observed_at": evidence.observed_at,
            "verification_deadline": authority.snapshot.verification_deadline,
            "resulting_aggregate_record_version": authority.aggregate.record_version + 1,
        }
        observation = self._record(PublicationProductObservation, "product_observation", values)
        terminal: dict[str, object] = {}
        if evidence.read_outcome is PublicationReadOutcome.POSITIVE_PROOF:
            result_values = {
                "result_id": self._stable_id(
                    "publication_result", authority.aggregate.aggregate_id
                ),
                "aggregate_id": authority.aggregate.aggregate_id,
                "observation_id": observation.observation_id,
                "observation_fingerprint": observation.fingerprint,
                "numeric_listing_id": observation.numeric_listing_id,
                "canonical_link_fingerprint": safe_listing_link_fingerprint(
                    observation.numeric_listing_id  # type: ignore[arg-type]
                ),
                "verified_product_fingerprint": observation.verified_product_fingerprint,
                "verified_at": evidence.observed_at,
            }
            result = self._record(PublicationResult, "publication_result", result_values)
            notification_values = {
                "notification_id": self._stable_id(
                    "publication_notification",
                    result.result_id,
                ),
                "aggregate_id": authority.aggregate.aggregate_id,
                "result_id": result.result_id,
                "result_fingerprint": result.fingerprint,
                "channel": "authenticated_in_application",
                "created_at": now,
            }
            notification = self._record(
                PublicationNotification,
                "publication_notification",
                notification_values,
            )
            aggregate, attempt, work, terminal = self._terminal_records(
                authority,
                now=now,
                state=PublicationState.PUBLISHED,
                work_status=PublicationExecutionWorkStatus.SUCCEEDED,
                reason=PublicationTerminalReason.POSITIVE_PUBLICATION_PROOF,
                permit=authority.permit,
                observation=observation,
                result=result,
                notification=notification,
            )
            event_name = PublicationExecutionEventName.PUBLISHED
        elif now >= authority.snapshot.verification_deadline:
            aggregate, attempt, work, terminal = self._terminal_records(
                authority,
                now=now,
                state=PublicationState.PUBLICATION_OUTCOME_UNKNOWN,
                work_status=PublicationExecutionWorkStatus.OUTCOME_UNKNOWN,
                reason=PublicationTerminalReason.FIXED_DEADLINE_WITHOUT_POSITIVE_PROOF,
                permit=authority.permit,
                observation=observation,
            )
            event_name = PublicationExecutionEventName.PUBLICATION_OUTCOME_UNKNOWN
        else:
            aggregate = self._evolve_aggregate(
                authority,
                now=now,
                last_observation_fingerprint=observation.fingerprint,
            )
            work = self._evolve_work(authority, now=now)
            attempt = authority.attempt
            event_name = PublicationExecutionEventName.PUBLICATION_OBSERVED
        return self._commit(
            command,
            authority,
            request_fingerprint,
            now,
            operation=PublicationExecutionOperation.RECORD_PRODUCT_OBSERVATION,
            event_name=event_name,
            authority_record_id=observation.observation_id,
            authority_fingerprint=observation.fingerprint,
            aggregate=aggregate,
            attempt=attempt,
            permit=authority.permit,
            work=work,
            new_product_observation=observation,
            **terminal,
        )

    def settle_deadline(
        self,
        command: SettlePublicationDeadlineCommand,
    ) -> PublicationExecutionCommitResult:
        context = self._begin(command, PublicationExecutionOperation.SETTLE_DEADLINE)
        if isinstance(context, PublicationExecutionCommitResult):
            return context
        authority, request_fingerprint, now = context
        if now < authority.snapshot.verification_deadline:
            self._invalid_transition("The fixed publication deadline has not elapsed")
        if (
            authority.aggregate.state is PublicationState.PUBLICATION_REQUESTED
            and authority.permit.status is PublicationPermitState.AVAILABLE
        ):
            permit = self._evolve_permit(
                authority,
                status=PublicationPermitState.RETIRED,
                record_version=1,
                retired_at=now,
                retirement_reason=PublicationPermitRetirementReason.PRE_CALL_DEADLINE_EXPIRED,
            )
            state = PublicationState.PUBLICATION_FAILED
            work_status = PublicationExecutionWorkStatus.FAILED
            reason = PublicationTerminalReason.PRE_CALL_DEADLINE_EXPIRED
            event_name = PublicationExecutionEventName.PUBLICATION_FAILED
        elif (
            authority.aggregate.state
            in {
                PublicationState.PUBLICATION_VERIFYING,
                PublicationState.PUBLICATION_RECONCILING,
            }
            and authority.permit.status is PublicationPermitState.CONSUMED
        ):
            permit = authority.permit
            state = PublicationState.PUBLICATION_OUTCOME_UNKNOWN
            work_status = PublicationExecutionWorkStatus.OUTCOME_UNKNOWN
            reason = PublicationTerminalReason.FIXED_DEADLINE_WITHOUT_POSITIVE_PROOF
            event_name = PublicationExecutionEventName.PUBLICATION_OUTCOME_UNKNOWN
        elif (
            authority.aggregate.state is PublicationState.PUBLICATION_REQUESTED
            and authority.permit.status is PublicationPermitState.CONSUMED
        ):
            self._invalid_transition(
                "Consumed restart must first record ambiguous outcome and enter reconciliation"
            )
        else:
            self._invalid_transition("Publication deadline cannot settle the current state")
        observation = authority.last_product_observation or authority.post_observation
        aggregate, attempt, work, terminal = self._terminal_records(
            authority,
            now=now,
            state=state,
            work_status=work_status,
            reason=reason,
            permit=permit,
            observation=observation,
        )
        report = terminal["new_report"]
        return self._commit(
            command,
            authority,
            request_fingerprint,
            now,
            operation=PublicationExecutionOperation.SETTLE_DEADLINE,
            event_name=event_name,
            authority_record_id=report.report_id,  # type: ignore[union-attr]
            authority_fingerprint=report.fingerprint,  # type: ignore[union-attr]
            aggregate=aggregate,
            attempt=attempt,
            permit=permit,
            work=work,
            **terminal,
        )

    def _claim_get(
        self,
        command: ClaimShopGetCommand | ClaimProductGetCommand,
        *,
        operation: PublicationExecutionOperation,
        kind: PublicationCallKind,
        purpose: PublicationCallPurpose,
    ) -> PublicationExecutionCommitResult:
        context = self._begin(command, operation)
        if isinstance(context, PublicationExecutionCommitResult):
            return context
        authority, request_fingerprint, now = context
        self._require_before_deadline(authority, now)
        if authority.provider_authority is None:
            self._invalid_authority("Provider authority must be reconstructed before GET claims")
        if authority.attempt.status is not PublicationAttemptStatus.OPEN:
            self._invalid_transition("Root publication attempt is terminal")
        if kind is PublicationCallKind.SHOP_GET:
            self._require_requested_dispatched(authority)
            if authority.preflight_proof is not None:
                self._invalid_transition("Completed preflight cannot spend another shop GET")
            if authority.permit.status is not PublicationPermitState.AVAILABLE:
                self._permit_unavailable()
            count = authority.attempt.shop_get_call_count
            limit = authority.attempt.shop_get_call_limit
            attempt_updates = {"shop_get_call_count": count + 1}
        else:
            if purpose is PublicationCallPurpose.PRODUCT_PREFLIGHT:
                self._require_requested_dispatched(authority)
                if authority.preflight_proof is not None:
                    self._invalid_transition(
                        "Completed preflight cannot spend another product preflight GET"
                    )
                if authority.permit.status is not PublicationPermitState.AVAILABLE:
                    self._permit_unavailable()
            elif purpose is PublicationCallPurpose.VERIFICATION:
                if (
                    authority.aggregate.state is not PublicationState.PUBLICATION_VERIFYING
                    or authority.work.status is not PublicationExecutionWorkStatus.VERIFYING
                    or authority.permit.status is not PublicationPermitState.CONSUMED
                ):
                    self._invalid_transition("Verification GET requires verifying state")
            elif (
                authority.aggregate.state is not PublicationState.PUBLICATION_RECONCILING
                or authority.work.status is not PublicationExecutionWorkStatus.RECONCILING
                or authority.permit.status is not PublicationPermitState.CONSUMED
            ):
                self._invalid_transition("Reconciliation GET requires reconciling state")
            count = authority.attempt.product_get_call_count
            limit = authority.attempt.product_get_call_limit
            attempt_updates = {"product_get_call_count": count + 1}
        if count >= limit:
            self._budget_exhausted("Publication root-attempt GET budget is exhausted")
        attempt = self._evolve_attempt(
            authority,
            record_version=authority.attempt.record_version + 1,
            **attempt_updates,
        )
        call_claim_id = self._stable_id("call_claim", command.operation_id)
        claim_values = self._call_claim_values(
            authority,
            operation_id=command.operation_id,
            authorization_id=call_claim_id,
            kind=kind,
            purpose=purpose,
            ordinal=count + 1,
            resulting_attempt_version=attempt.record_version,
            authorized_at=now,
        )
        claim = self._record(PublicationCallClaim, "call_claim", claim_values)
        aggregate = self._evolve_aggregate(authority, now=now)
        work = self._evolve_work(authority, now=now)
        return self._commit(
            command,
            authority,
            request_fingerprint,
            now,
            operation=operation,
            event_name=PublicationExecutionEventName.PROVIDER_CALL_AUTHORIZED,
            authority_record_id=claim.authorization_id,
            authority_fingerprint=claim.fingerprint,
            aggregate=aggregate,
            attempt=attempt,
            permit=authority.permit,
            work=work,
            new_call_claim=claim,
        )

    def _begin(
        self,
        command: CommandT,
        operation: PublicationExecutionOperation,
    ) -> tuple[PublicationExecutionAuthority, str, datetime] | PublicationExecutionCommitResult:
        try:
            command = type(command).model_validate(command.model_dump(mode="python"))
        except (AttributeError, ValidationError, ValueError):
            self._invalid_authority("Publication execution command is invalid")
        request_fingerprint = execution_request_fingerprint(operation.value, command)
        receipt = self._store.resolve_execution_receipt(
            command.owner_id,
            command.aggregate_id,
            command.operation_id,
        )
        if receipt is not None:
            if receipt.request_fingerprint != request_fingerprint:
                raise PublicationIdempotencyConflictError()
            return PublicationExecutionCommitResult(receipt=receipt)
        authority = self._store.load_execution_authority(
            command.owner_id,
            command.aggregate_id,
        )
        expected_versions = (
            command.expected_aggregate_record_version,
            command.expected_attempt_record_version,
            command.expected_permit_record_version,
            command.expected_work_record_version,
        )
        actual_versions = (
            authority.aggregate.record_version,
            authority.attempt.record_version,
            authority.permit.record_version,
            authority.work.record_version,
        )
        if expected_versions != actual_versions:
            raise PublicationConflictError(
                PublicationErrorCode.CONCURRENT_WRITE,
                "Publication execution authority is stale",
            )
        now = self._clock()
        if now.tzinfo is None or now.utcoffset() != timedelta(0):
            self._invalid_authority("Publication execution clock must be UTC-aware")
        if now < authority.aggregate.updated_at:
            self._invalid_authority("Publication execution clock cannot move backwards")
        return authority, request_fingerprint, now

    def _commit(
        self,
        command: PublicationExecutionCommand,
        authority: PublicationExecutionAuthority,
        request_fingerprint: str,
        now: datetime,
        *,
        operation: PublicationExecutionOperation,
        event_name: PublicationExecutionEventName,
        authority_record_id: str,
        authority_fingerprint: str,
        aggregate: ExecutionPublicationAggregate,
        attempt: ExecutionPublicationAttempt,
        permit: ExecutionPublicationPermit,
        work: ExecutionPublicationWork,
        new_call_claim: PublicationCallClaim | None = None,
        new_provider_authority: PublicationProviderAuthority | None = None,
        new_preflight_proof: PublicationPreflightProof | None = None,
        new_mutation_claim: PublicationMutationClaim | None = None,
        new_post_observation: PublicationPostObservation | None = None,
        new_product_observation: PublicationProductObservation | None = None,
        new_result: PublicationResult | None = None,
        new_notification: PublicationNotification | None = None,
        new_report: PublicationTerminalReport | None = None,
        new_tombstone: PublicationAggregateTombstone | None = None,
        terminal_job_update: PublicationTerminalJobUpdate | None = None,
    ) -> PublicationExecutionCommitResult:
        event_values = {
            "aggregate_id": aggregate.aggregate_id,
            "owner_id": aggregate.owner_id,
            "job_id": aggregate.job_id,
            "sequence": aggregate.event_sequence,
            "name": event_name,
            "state": aggregate.state,
            "operation_id": command.operation_id,
            "authority_fingerprint": authority_fingerprint,
            "occurred_at": now,
        }
        event = self._record(PublicationExecutionEvent, "execution_event", event_values)
        receipt_values = {
            "receipt_id": self._stable_id("execution_receipt", command.operation_id),
            "operation_id": command.operation_id,
            "operation": operation,
            "aggregate_id": aggregate.aggregate_id,
            "owner_id": aggregate.owner_id,
            "job_id": aggregate.job_id,
            "request_fingerprint": request_fingerprint,
            "aggregate_state": aggregate.state,
            "aggregate_record_version": aggregate.record_version,
            "attempt_record_version": attempt.record_version,
            "permit_record_version": permit.record_version,
            "work_record_version": work.record_version,
            "authority_record_id": authority_record_id,
            "authority_fingerprint": authority_fingerprint,
            "created_at": now,
        }
        receipt = self._record(
            PublicationExecutionReceipt,
            "execution_receipt",
            receipt_values,
        )
        commit = PublicationExecutionCommit(
            expected=authority,
            updated_aggregate=aggregate,
            updated_attempt=attempt,
            updated_permit=permit,
            updated_work=work,
            new_call_claim=new_call_claim,
            new_provider_authority=new_provider_authority,
            new_preflight_proof=new_preflight_proof,
            new_mutation_claim=new_mutation_claim,
            new_post_observation=new_post_observation,
            new_product_observation=new_product_observation,
            new_result=new_result,
            new_notification=new_notification,
            new_report=new_report,
            new_tombstone=new_tombstone,
            terminal_job_update=terminal_job_update,
            event=event,
            receipt=receipt,
        )
        try:
            return self._store.commit_execution(commit)
        except PublicationConflictError:
            persisted = self._store.resolve_execution_receipt(
                command.owner_id,
                command.aggregate_id,
                command.operation_id,
            )
            if persisted is not None:
                if persisted.request_fingerprint == request_fingerprint:
                    # A recovered/replayed call claim intentionally has no fresh wire grant.
                    return PublicationExecutionCommitResult(receipt=persisted)
                raise PublicationIdempotencyConflictError() from None
            raise

    def _terminal_records(
        self,
        authority: PublicationExecutionAuthority,
        *,
        now: datetime,
        state: PublicationState,
        work_status: PublicationExecutionWorkStatus,
        reason: PublicationTerminalReason,
        permit: ExecutionPublicationPermit,
        observation: PublicationPostObservation | PublicationProductObservation | None,
        result: PublicationResult | None = None,
        notification: PublicationNotification | None = None,
    ) -> tuple[
        ExecutionPublicationAggregate,
        ExecutionPublicationAttempt,
        ExecutionPublicationWork,
        dict[str, object],
    ]:
        source_release = now + timedelta(days=30)
        operational_expiry = now + timedelta(days=90)
        attempt = self._evolve_attempt(
            authority,
            status=PublicationAttemptStatus.TERMINAL,
            terminal_at=now,
        )
        work = self._evolve_work(
            authority,
            now=now,
            status=work_status,
            terminal_at=now,
            next_dispatch_at=None,
        )
        report_id = self._stable_id("terminal_report", authority.aggregate.aggregate_id)
        tombstone_id = self._stable_id("aggregate_tombstone", authority.aggregate.aggregate_id)
        owner_digest, job_digest = PublicationTerminalReport.identity_digests(
            authority.snapshot.owner_id,
            authority.snapshot.job_id,
        )
        report_values = {
            "report_id": report_id,
            "aggregate_id": authority.aggregate.aggregate_id,
            "owner_digest": owner_digest,
            "job_digest": job_digest,
            "terminal_state": state,
            "terminal_reason": reason,
            "requested_at": authority.aggregate.requested_at,
            "terminal_at": now,
            "source_release_eligible_at": source_release,
            "operational_expires_at": operational_expiry,
            "shop_get_call_count": attempt.shop_get_call_count,
            "product_get_call_count": attempt.product_get_call_count,
            "publish_post_call_count": attempt.publish_post_call_count,
            "release_manifest_fingerprint": authority.snapshot.release_manifest_fingerprint,
            "snapshot_fingerprint": authority.snapshot.fingerprint,
            "attempt_fingerprint": attempt.fingerprint,
            "permit_fingerprint": permit.fingerprint,
            "observation_fingerprint": (
                observation.fingerprint if observation is not None else None
            ),
            "result_fingerprint": result.fingerprint if result is not None else None,
            "sanitized_audit_record_digests": tuple(
                binding.audit_record.fingerprint for binding in authority.provider_audits
            ),
        }
        report = self._record(PublicationTerminalReport, "terminal_report", report_values)
        tombstone_values = {
            "tombstone_id": tombstone_id,
            "owner_id": authority.snapshot.owner_id,
            "job_id": authority.snapshot.job_id,
            "aggregate_id": authority.aggregate.aggregate_id,
            "terminal_state": state,
            "terminal_at": now,
            "operational_expires_at": operational_expiry,
            "report_id": report.report_id,
            "report_fingerprint": report.fingerprint,
        }
        tombstone = self._record(
            PublicationAggregateTombstone,
            "aggregate_tombstone",
            tombstone_values,
        )
        job_link_values = {
            "owner_id": authority.snapshot.owner_id,
            "job_id": authority.snapshot.job_id,
            "aggregate_id": authority.aggregate.aggregate_id,
            "phase6_state": "approved",
            "expected_record_version": authority.phase6_record_version,
            "result_record_version": authority.phase6_record_version + 1,
            "expected_event_sequence": authority.phase6_event_sequence,
            "result_event_sequence": authority.phase6_event_sequence,
            "terminal_state": state,
            "terminal_at": now,
            "report_id": report.report_id,
            "report_fingerprint": report.fingerprint,
            "result_id": result.result_id if result is not None else None,
            "terminal_summary_fingerprint": publication_terminal_summary_fingerprint(
                aggregate_id=authority.aggregate.aggregate_id,
                terminal_state=state.value,
                terminal_at=now,
                source_release_eligible_at=source_release,
                operational_expires_at=operational_expiry,
                report_id=report.report_id,
                result_id=result.result_id if result is not None else None,
            ),
            "source_release_eligible_at": source_release,
            "operational_expires_at": operational_expiry,
        }
        terminal_job_link = self._record(
            PublicationTerminalJobLink,
            "terminal_job_link",
            job_link_values,
        )
        current_job = self._store.load_linked_job(
            authority.snapshot.owner_id,
            authority.aggregate.aggregate_id,
        )
        updated_job = ControlJobRecord.model_validate(
            {
                **current_job.model_dump(mode="python"),
                "record_version": terminal_job_link.result_record_version,
                "publication_terminal_state": terminal_job_link.terminal_state.value,
                "publication_terminal_at": terminal_job_link.terminal_at,
                "publication_source_release_eligible_at": (
                    terminal_job_link.source_release_eligible_at
                ),
                "publication_operational_expires_at": (terminal_job_link.operational_expires_at),
                "publication_report_id": terminal_job_link.report_id,
                "publication_result_id": terminal_job_link.result_id,
                "publication_terminal_summary_fingerprint": (
                    terminal_job_link.terminal_summary_fingerprint
                ),
                "updated_at": terminal_job_link.terminal_at,
            }
        )
        terminal_job_update = PublicationTerminalJobUpdate(
            expected_job=current_job,
            updated_job=updated_job,
            link=terminal_job_link,
        )
        aggregate = self._evolve_aggregate(
            authority,
            now=now,
            state=state,
            terminal_at=now,
            source_release_eligible_at=source_release,
            operational_expires_at=operational_expiry,
            last_observation_fingerprint=(
                observation.fingerprint if observation is not None else None
            ),
            result_id=result.result_id if result is not None else None,
            notification_id=(notification.notification_id if notification is not None else None),
            report_id=report.report_id,
            tombstone_id=tombstone.tombstone_id,
        )
        return (
            aggregate,
            attempt,
            work,
            {
                "new_result": result,
                "new_notification": notification,
                "new_report": report,
                "new_tombstone": tombstone,
                "terminal_job_update": terminal_job_update,
            },
        )

    def _call_claim_values(
        self,
        authority: PublicationExecutionAuthority,
        *,
        operation_id: str,
        authorization_id: str,
        kind: PublicationCallKind,
        purpose: PublicationCallPurpose,
        ordinal: int,
        resulting_attempt_version: int,
        authorized_at: datetime,
        permit_fingerprint: str | None = None,
    ) -> dict[str, object]:
        method = "POST" if kind is PublicationCallKind.PUBLISH_POST else "GET"
        if kind is PublicationCallKind.SHOP_GET:
            route = "/v1/shops.json"
            product_id = None
            limit = 3
        elif kind is PublicationCallKind.PRODUCT_GET:
            route = "/v1/shops/{shop_id}/products/{product_id}.json"
            product_id = authority.snapshot.printify_product_id
            limit = 100
        else:
            route = "/v1/shops/{shop_id}/products/{product_id}/publish.json"
            product_id = authority.snapshot.printify_product_id
            limit = 1
        return {
            "authorization_id": authorization_id,
            "operation_id": operation_id,
            "aggregate_id": authority.aggregate.aggregate_id,
            "attempt_id": authority.attempt.attempt_id,
            "snapshot_id": authority.snapshot.snapshot_id,
            "snapshot_fingerprint": authority.snapshot.fingerprint,
            "permit_id": authority.permit.permit_id,
            "work_request_id": authority.work.work_request_id,
            "owner_id": authority.snapshot.owner_id,
            "job_id": authority.snapshot.job_id,
            "call_kind": kind,
            "method": method,
            "route_template": route,
            "purpose": purpose,
            "printify_shop_id": authority.snapshot.printify_shop_id,
            "printify_product_id": product_id,
            "ordinal": ordinal,
            "call_limit": limit,
            "resulting_attempt_record_version": resulting_attempt_version,
            "permit_fingerprint": permit_fingerprint,
            "authorized_at": authorized_at,
            "verification_deadline": authority.snapshot.verification_deadline,
            "mutation_authorized": kind is PublicationCallKind.PUBLISH_POST,
        }

    @staticmethod
    def _record(record_type: type[RecordT], kind: str, values: dict[str, object]) -> RecordT:
        return record_type(
            **values,
            fingerprint=execution_record_fingerprint(kind, values),
        )

    @staticmethod
    def _stable_id(kind: str, authority: str) -> str:
        digest = sha256(f"phase7.2:{kind}:{authority}".encode()).hexdigest()[:40]
        return f"{kind}_{digest}"

    @staticmethod
    def _evolve(
        record: RecordT,
        record_type: type[RecordT],
        kind: str,
        updates: dict[str, object],
    ) -> RecordT:
        values = record.model_dump(
            mode="python",
            exclude={"contract_version", "fingerprint"},
        )
        values.update(updates)
        return PublicationExecutionService._record(record_type, kind, values)

    def _evolve_aggregate(
        self,
        authority: PublicationExecutionAuthority,
        *,
        now: datetime,
        **updates: object,
    ) -> ExecutionPublicationAggregate:
        return self._evolve(
            authority.aggregate,
            ExecutionPublicationAggregate,
            "execution_aggregate",
            {
                "record_version": authority.aggregate.record_version + 1,
                "event_sequence": authority.aggregate.event_sequence + 1,
                "updated_at": now,
                **updates,
            },
        )

    def _evolve_attempt(
        self,
        authority: PublicationExecutionAuthority,
        **updates: object,
    ) -> ExecutionPublicationAttempt:
        return self._evolve(
            authority.attempt,
            ExecutionPublicationAttempt,
            "execution_attempt",
            updates,
        )

    def _evolve_permit(
        self,
        authority: PublicationExecutionAuthority,
        **updates: object,
    ) -> ExecutionPublicationPermit:
        return self._evolve(
            authority.permit,
            ExecutionPublicationPermit,
            "execution_permit",
            updates,
        )

    def _evolve_work(
        self,
        authority: PublicationExecutionAuthority,
        *,
        now: datetime,
        **updates: object,
    ) -> ExecutionPublicationWork:
        return self._evolve(
            authority.work,
            ExecutionPublicationWork,
            "execution_work",
            {
                "record_version": authority.work.record_version + 1,
                "updated_at": now,
                **updates,
            },
        )

    def _validate_live_provider_authority(
        self,
        execution: PublicationExecutionAuthority,
        source: PublicationRequestAuthority,
        now: datetime,
    ) -> ExactReviewProductProfile:
        try:
            validate_publication_request_authority(source)
        except ValueError:
            self._invalid_authority("Current Phase 6 publication source authority is invalid")
        snapshot = execution.snapshot
        job = source.current_job
        sync = source.product_sync
        pricing = source.pricing_snapshot
        if (
            job.owner_id != snapshot.owner_id
            or job.job_id != snapshot.job_id
            or job.publication_aggregate_id != execution.aggregate.aggregate_id
            or job.record_version != execution.phase6_record_version
            or job.event_sequence != execution.phase6_event_sequence
            or job.publication_terminal_state is not None
            or job.approval_decision_id != snapshot.approval_decision_id
            or job.approval_fingerprint != snapshot.approval_fingerprint
            or source.review.review_version != snapshot.review_version
            or source.review.fingerprint != snapshot.review_fingerprint
            or sync.sync_id != snapshot.product_sync_id
            or sync.fingerprint != snapshot.product_sync_fingerprint
            or sync.printify_shop_id != snapshot.printify_shop_id
            or sync.product_id != snapshot.printify_product_id
            or sync.image_id != snapshot.printify_image_id
            or sync.payload_fingerprint != snapshot.product_payload_fingerprint
            or pricing.snapshot_id != snapshot.pricing_snapshot_id
            or pricing.fingerprint != snapshot.pricing_snapshot_fingerprint
            or source.pricing_evidence.fingerprint != snapshot.pricing_evidence_fingerprint
            or source.source.product_profile_id != snapshot.profile_id
            or source.source.product_profile_version != snapshot.profile_version
            or source.source.product_profile_fingerprint != snapshot.profile_fingerprint
            or self._release_manifest_fingerprint != snapshot.release_manifest_fingerprint
        ):
            self._invalid_authority("Re-read application authority differs from the snapshot")
        if now >= snapshot.pricing_fresh_until:
            raise PublicationConflictError(
                PublicationErrorCode.PRICING_NOT_FRESH,
                "Pricing authority is no longer fresh",
            )
        try:
            exact = self._profiles.get_exact(
                profile_id=snapshot.profile_id,
                profile_version=snapshot.profile_version,
            )
        except (ReviewProfileNotFoundError, LookupError, ValidationError):
            self._invalid_authority("The snapshotted product profile is unavailable")
        if not isinstance(exact, ExactReviewProductProfile):
            self._invalid_authority("The snapshotted product profile is unavailable")
        profile = exact.profile
        expected_profile_fingerprint = control_fingerprint(profile)
        estimate = source.pricing_evidence.estimate
        expected_variant_pairs = {
            (color, size) for color in profile.colors for size in profile.sizes
        }
        observed_variant_pairs = {(variant.color, variant.size) for variant in sync.variants}
        group_by_size = {
            size: group.group_id for group in profile.placement_groups for size in group.sizes
        }
        if (
            profile.profile_id != snapshot.profile_id
            or profile.profile_version != snapshot.profile_version
            or exact.fingerprint != expected_profile_fingerprint
            or exact.fingerprint != snapshot.profile_fingerprint
            or profile.publish_enabled is not True
            or estimate.blueprint_id != profile.blueprint_id
            or estimate.print_provider_id != profile.print_provider_id
            or any(
                variant.retail_price_cents != profile.retail_price_cents
                for variant in sync.variants
            )
            or any(
                variant.buyer_shipping_cents != profile.buyer_shipping_cents
                for variant in estimate.variants
            )
            or (
                profile.variant_ids
                and set(profile.variant_ids) != {variant.variant_id for variant in sync.variants}
            )
            or (
                not profile.variant_ids
                and (
                    observed_variant_pairs != expected_variant_pairs
                    or len(sync.variants) != len(expected_variant_pairs)
                    or any(
                        variant.placement_group_id != group_by_size.get(variant.size)
                        for variant in sync.variants
                    )
                )
            )
        ):
            self._invalid_authority("Current profile and economics differ from the snapshot")
        return exact

    @staticmethod
    def _require_requested_dispatched(authority: PublicationExecutionAuthority) -> None:
        if (
            authority.aggregate.state is not PublicationState.PUBLICATION_REQUESTED
            or authority.work.status is not PublicationExecutionWorkStatus.DISPATCHED
        ):
            PublicationExecutionService._invalid_transition(
                "Operation requires requested, dispatched publication work"
            )

    @staticmethod
    def _require_before_deadline(
        authority: PublicationExecutionAuthority,
        now: datetime,
    ) -> None:
        if now >= authority.snapshot.verification_deadline:
            PublicationExecutionService._deadline_expired(
                "The fixed publication deadline has elapsed"
            )

    @staticmethod
    def _invalid_transition(message: str) -> None:
        raise PublicationConflictError(PublicationErrorCode.INVALID_TRANSITION, message)

    @staticmethod
    def _invalid_authority(message: str) -> None:
        raise PublicationConflictError(PublicationErrorCode.INVALID_AUTHORITY, message)

    @staticmethod
    def _budget_exhausted(message: str) -> None:
        raise PublicationConflictError(PublicationErrorCode.CALL_BUDGET_EXHAUSTED, message)

    @staticmethod
    def _deadline_expired(message: str) -> None:
        raise PublicationConflictError(PublicationErrorCode.DEADLINE_EXPIRED, message)

    @staticmethod
    def _permit_unavailable() -> None:
        raise PublicationConflictError(
            PublicationErrorCode.PERMIT_UNAVAILABLE,
            "One-shot publication permit is no longer available",
        )
