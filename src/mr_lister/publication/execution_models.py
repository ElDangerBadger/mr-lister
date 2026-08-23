"""Strict immutable records for offline Phase 7.2 publication execution.

These records model application-owned authority only.  They contain no provider client, network
transport, dispatcher, route, secret lookup, or production composition.
"""

from __future__ import annotations

from datetime import timedelta
from enum import StrEnum
from typing import Literal

from pydantic import Field, StrictBool, StrictInt, model_validator

from mr_lister.publication.contract import PublicationPermitState, PublicationState
from mr_lister.publication.execution_fingerprints import (
    execution_record_fingerprint,
    safe_identity_digest,
    safe_listing_link_fingerprint,
)
from mr_lister.publication.fingerprints import publication_body_fingerprint
from mr_lister.publication.models import (
    Fingerprint,
    OwnerId,
    PublicationAggregate,
    PublicationAttempt,
    PublicationJobLink,
    PublicationModel,
    PublicationPermit,
    PublicationSnapshot,
    PublicationWorkRequest,
    SafeId,
    UtcDateTime,
)


class PublicationAttemptStatus(StrEnum):
    OPEN = "open"
    TERMINAL = "terminal"


class PublicationExecutionWorkStatus(StrEnum):
    PENDING = "pending"
    DISPATCHED = "dispatched"
    VERIFYING = "verifying"
    RECONCILING = "reconciling"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    OUTCOME_UNKNOWN = "outcome_unknown"


class PublicationCallKind(StrEnum):
    SHOP_GET = "shop_get"
    PRODUCT_GET = "product_get"
    PUBLISH_POST = "publish_post"


class PublicationCallPurpose(StrEnum):
    SHOP_PREFLIGHT = "etsy_shop_preflight"
    PRODUCT_PREFLIGHT = "product_preflight"
    VERIFICATION = "positive_verification"
    RECONCILIATION = "reconciliation"
    PUBLISH = "one_shot_connected_channel_publication"


class PublicationReadOutcome(StrEnum):
    POSITIVE_PROOF = "positive_publication_proof"
    NOT_YET_PROVEN = "publication_not_yet_proven"
    CONFLICTING_OR_INCOMPLETE = "conflicting_or_incomplete_evidence"


class PublicationPostOutcome(StrEnum):
    DEFINITELY_ACCEPTED = "definitely_accepted"
    DEFINITIVE_SYNCHRONOUS_REJECTION = "definitive_synchronous_rejection"
    AMBIGUOUS = "ambiguous"


class PublicationPublishResponseCategory(StrEnum):
    """Closed, sanitized classification emitted by the provider boundary."""

    VALIDATED_2XX = "validated_2xx"
    NON_2XX = "non_2xx"
    MALFORMED_2XX = "malformed_2xx"
    TRANSPORT_FAILURE = "transport_failure"
    CONSUMED_CLAIM_WITHOUT_DURABLE_BOUNDARY_OBSERVATION = (
        "consumed_claim_without_durable_boundary_observation"
    )


class PublicationExternalEvidenceState(StrEnum):
    ABSENT = "absent"
    SINGLE_NUMERIC_ETSY_REFERENCE = "single_numeric_etsy_reference"
    CONFLICTING_OR_INCOMPLETE = "conflicting_or_incomplete"


class PublicationPreflightFailureReason(StrEnum):
    SHOP_NOT_CONNECTED_TO_ETSY = "shop_not_connected_to_etsy"
    EXACT_PRODUCT_NOT_FOUND = "exact_product_not_found"
    PRODUCT_LOCKED = "product_locked"
    PRODUCT_ALREADY_PUBLISHED = "product_already_published"
    CANONICAL_CONTENT_MISMATCH = "canonical_content_mismatch"
    VARIANT_AUTHORITY_MISMATCH = "variant_authority_mismatch"
    LOCAL_AUTHORITY_INVALID = "local_authority_invalid"


class PublicationPermitRetirementReason(StrEnum):
    DEFINITIVE_PREFLIGHT_FAILURE = "definitive_preflight_failure"
    PRE_CALL_DEADLINE_EXPIRED = "pre_call_deadline_expired"


class PublicationTerminalReason(StrEnum):
    DEFINITIVE_PREFLIGHT_FAILURE = "definitive_preflight_failure"
    PRE_CALL_DEADLINE_EXPIRED = "pre_call_deadline_expired"
    DEFINITIVE_SYNCHRONOUS_REJECTION = "definitive_synchronous_rejection"
    POSITIVE_PUBLICATION_PROOF = "positive_publication_proof"
    FIXED_DEADLINE_WITHOUT_POSITIVE_PROOF = "fixed_deadline_without_positive_proof"


class PublicationExecutionOperation(StrEnum):
    DISPATCH = "dispatch"
    RECONSTRUCT_AUTHORITY = "reconstruct_authority"
    CLAIM_SHOP_GET = "claim_shop_get"
    CLAIM_PRODUCT_GET = "claim_product_get"
    RECORD_PREFLIGHT = "record_preflight"
    CLAIM_PUBLISH = "claim_publish"
    RECORD_POST_OUTCOME = "record_post_outcome"
    RECOVER_CONSUMED_CLAIM = "recover_consumed_claim"
    RECORD_PRODUCT_OBSERVATION = "record_product_observation"
    SETTLE_DEADLINE = "settle_deadline"


class PublicationProviderAuditDecision(StrEnum):
    ALLOWED = "allowed"
    REJECTED = "rejected"


class PublicationProviderAuditCategory(StrEnum):
    SHOP_GET_ALLOWED = "shop_get_allowed"
    PRODUCT_GET_ALLOWED = "product_get_allowed"
    PUBLISH_POST_ALLOWED = "publish_post_allowed"
    FORBIDDEN_METHOD = "forbidden_method"
    FORBIDDEN_ROUTE = "forbidden_route"
    CLAIM_MISMATCH = "claim_mismatch"
    STALE_OR_REPLAYED_GRANT = "stale_or_replayed_grant"
    MUTATION_CLAIM_MISMATCH = "mutation_claim_mismatch"


class PublicationExecutionEventName(StrEnum):
    WORK_DISPATCHED = "PUBLICATION_WORK_DISPATCHED"
    PROVIDER_AUTHORITY_RECONSTRUCTED = "PUBLICATION_PROVIDER_AUTHORITY_RECONSTRUCTED"
    PROVIDER_CALL_AUTHORIZED = "PUBLICATION_PROVIDER_CALL_AUTHORIZED"
    PREFLIGHT_PROVEN = "PUBLICATION_PREFLIGHT_PROVEN"
    PUBLISH_CLAIMED = "PUBLICATION_PUBLISH_CLAIMED"
    PUBLICATION_VERIFYING = "PUBLICATION_VERIFYING"
    PUBLICATION_RECONCILING = "PUBLICATION_RECONCILING"
    PUBLICATION_FAILED = "PUBLICATION_FAILED"
    PUBLICATION_OBSERVED = "PUBLICATION_OBSERVED"
    PUBLISHED = "PUBLISHED"
    PUBLICATION_OUTCOME_UNKNOWN = "PUBLICATION_OUTCOME_UNKNOWN"


class ExecutionPublicationAggregate(PublicationModel):
    """Current publication aggregate record after the pristine request transaction."""

    aggregate_id: SafeId
    owner_id: OwnerId
    job_id: SafeId
    state: PublicationState
    record_version: StrictInt = Field(ge=0)
    event_sequence: StrictInt = Field(ge=1)
    snapshot_id: SafeId
    snapshot_fingerprint: Fingerprint
    attempt_id: SafeId
    permit_id: SafeId
    work_request_id: SafeId
    receipt_id: SafeId
    requested_at: UtcDateTime
    verification_deadline: UtcDateTime
    updated_at: UtcDateTime
    terminal_at: UtcDateTime | None = None
    source_release_eligible_at: UtcDateTime | None = None
    operational_expires_at: UtcDateTime | None = None
    last_observation_fingerprint: Fingerprint | None = None
    result_id: SafeId | None = None
    notification_id: SafeId | None = None
    report_id: SafeId | None = None
    tombstone_id: SafeId | None = None
    provider_audit_record_version: StrictInt = Field(ge=0, le=104)
    fingerprint: Fingerprint

    @classmethod
    def from_request(
        cls,
        aggregate: PublicationAggregate,
        snapshot: PublicationSnapshot,
    ) -> ExecutionPublicationAggregate:
        values = {
            **aggregate.model_dump(
                mode="python",
                exclude={
                    "contract_version",
                    "terminal_at",
                    "source_release_eligible_at",
                    "operational_expires_at",
                    "fingerprint",
                },
            ),
            "event_sequence": 1,
            "verification_deadline": snapshot.verification_deadline,
            "last_observation_fingerprint": None,
            "result_id": None,
            "notification_id": None,
            "report_id": None,
            "tombstone_id": None,
            "provider_audit_record_version": 0,
            "terminal_at": None,
            "source_release_eligible_at": None,
            "operational_expires_at": None,
        }
        return cls(
            **values,
            fingerprint=execution_record_fingerprint("execution_aggregate", values),
        )

    @model_validator(mode="after")
    def state_and_retention_are_closed(self) -> ExecutionPublicationAggregate:
        if self.state is PublicationState.APPROVED:
            raise ValueError("APPROVED is a derived bridge source, not an aggregate state")
        if self.verification_deadline != self.requested_at + timedelta(seconds=1800):
            raise ValueError("Execution cannot recompute or extend the root deadline")
        if self.updated_at < self.requested_at:
            raise ValueError("Aggregate time cannot move backwards")
        if self.record_version != self.event_sequence - 1:
            raise ValueError(
                "Aggregate version and publication event sequence must advance together"
            )
        terminal_states = {
            PublicationState.PUBLISHED,
            PublicationState.PUBLICATION_FAILED,
            PublicationState.PUBLICATION_OUTCOME_UNKNOWN,
        }
        terminal_values = (
            self.terminal_at,
            self.source_release_eligible_at,
            self.operational_expires_at,
            self.report_id,
            self.tombstone_id,
        )
        if self.state in terminal_states:
            if any(value is None for value in terminal_values):
                raise ValueError("Terminal aggregates require complete settlement authority")
            assert self.terminal_at is not None
            if self.updated_at != self.terminal_at:
                raise ValueError("The winning terminal timestamp must also update the aggregate")
            if self.source_release_eligible_at != self.terminal_at + timedelta(days=30):
                raise ValueError("Source release eligibility must be terminal_at plus 30 days")
            if self.operational_expires_at != self.terminal_at + timedelta(days=90):
                raise ValueError("Operational expiry must be terminal_at plus 90 days")
            if self.state is PublicationState.PUBLISHED:
                if self.result_id is None or self.notification_id is None:
                    raise ValueError("Published settlement requires result and notification")
            elif self.result_id is not None or self.notification_id is not None:
                raise ValueError("Only positive verification can create result and notification")
        elif any(value is not None for value in terminal_values):
            raise ValueError("Nonterminal aggregates cannot own terminal retention authority")
        elif self.result_id is not None or self.notification_id is not None:
            raise ValueError("Nonterminal aggregates cannot own a publication result")
        if self.fingerprint != execution_record_fingerprint("execution_aggregate", self):
            raise ValueError("Execution aggregate fingerprint is invalid")
        return self


class ExecutionPublicationAttempt(PublicationModel):
    """Durable root-attempt counters; authority can only decrease."""

    attempt_id: SafeId
    aggregate_id: SafeId
    owner_id: OwnerId
    job_id: SafeId
    snapshot_id: SafeId
    snapshot_fingerprint: Fingerprint
    root_attempt_number: Literal[1] = 1
    record_version: StrictInt = Field(ge=0)
    status: PublicationAttemptStatus = PublicationAttemptStatus.OPEN
    shop_get_call_limit: Literal[3] = 3
    shop_get_call_count: StrictInt = Field(ge=0, le=3)
    product_get_call_limit: Literal[100] = 100
    product_get_call_count: StrictInt = Field(ge=0, le=100)
    publish_post_call_limit: Literal[1] = 1
    publish_post_call_count: StrictInt = Field(ge=0, le=1)
    requested_at: UtcDateTime
    verification_deadline: UtcDateTime
    terminal_at: UtcDateTime | None = None
    fingerprint: Fingerprint

    @classmethod
    def from_request(cls, attempt: PublicationAttempt) -> ExecutionPublicationAttempt:
        values = {
            **attempt.model_dump(
                mode="python",
                exclude={"contract_version", "fingerprint"},
            ),
            "status": PublicationAttemptStatus.OPEN,
            "terminal_at": None,
        }
        return cls(
            **values,
            fingerprint=execution_record_fingerprint("execution_attempt", values),
        )

    @model_validator(mode="after")
    def budgets_and_deadline_are_fixed(self) -> ExecutionPublicationAttempt:
        if self.verification_deadline != self.requested_at + timedelta(seconds=1800):
            raise ValueError("Execution cannot recompute or extend the root deadline")
        if self.status is PublicationAttemptStatus.OPEN and self.terminal_at is not None:
            raise ValueError("An open attempt cannot have a terminal timestamp")
        if self.status is PublicationAttemptStatus.TERMINAL and self.terminal_at is None:
            raise ValueError("A terminal attempt requires its winning timestamp")
        expected_version = (
            self.shop_get_call_count + self.product_get_call_count + self.publish_post_call_count
        )
        if self.record_version != expected_version:
            raise ValueError("Attempt version must exactly account for calls and settlement")
        if self.fingerprint != execution_record_fingerprint("execution_attempt", self):
            raise ValueError("Execution attempt fingerprint is invalid")
        return self


class ExecutionPublicationPermit(PublicationModel):
    """Current one-shot permit; a terminal permit can never regain POST authority."""

    permit_id: SafeId
    aggregate_id: SafeId
    attempt_id: SafeId
    snapshot_id: SafeId
    snapshot_fingerprint: Fingerprint
    owner_id: OwnerId
    job_id: SafeId
    work_request_id: SafeId
    status: PublicationPermitState
    maximum_publish_posts_authorized: Literal[1] = 1
    record_version: StrictInt = Field(ge=0, le=1)
    created_at: UtcDateTime
    verification_deadline: UtcDateTime
    consumed_at: UtcDateTime | None = None
    retired_at: UtcDateTime | None = None
    mutation_claim_id: SafeId | None = None
    retirement_reason: PublicationPermitRetirementReason | None = None
    fingerprint: Fingerprint

    @classmethod
    def from_request(
        cls,
        permit: PublicationPermit,
        verification_deadline: UtcDateTime,
    ) -> ExecutionPublicationPermit:
        values = {
            **permit.model_dump(
                mode="python",
                exclude={"contract_version", "fingerprint"},
            ),
            "verification_deadline": verification_deadline,
            "consumed_at": None,
            "retired_at": None,
            "mutation_claim_id": None,
            "retirement_reason": None,
        }
        return cls(
            **values,
            fingerprint=execution_record_fingerprint("execution_permit", values),
        )

    @model_validator(mode="after")
    def one_shot_transition_is_closed(self) -> ExecutionPublicationPermit:
        if self.status is PublicationPermitState.AVAILABLE:
            if self.record_version != 0 or any(
                value is not None
                for value in (
                    self.consumed_at,
                    self.retired_at,
                    self.mutation_claim_id,
                    self.retirement_reason,
                )
            ):
                raise ValueError("An available permit must be pristine")
        elif self.status is PublicationPermitState.CONSUMED:
            if (
                self.record_version != 1
                or self.consumed_at is None
                or self.consumed_at >= self.verification_deadline
                or self.consumed_at < self.created_at
                or self.mutation_claim_id is None
                or self.retired_at is not None
                or self.retirement_reason is not None
            ):
                raise ValueError("A consumed permit requires one exact pre-call claim")
        elif (
            self.record_version != 1
            or self.retired_at is None
            or self.retired_at < self.created_at
            or self.retirement_reason is None
            or self.consumed_at is not None
            or self.mutation_claim_id is not None
        ):
            raise ValueError("A retired permit must prove zero mutation authority")
        if self.fingerprint != execution_record_fingerprint("execution_permit", self):
            raise ValueError("Execution permit fingerprint is invalid")
        return self


class ExecutionPublicationWork(PublicationModel):
    """Current publication work state, separate from every Phase 6 work type."""

    work_request_id: SafeId
    aggregate_id: SafeId
    attempt_id: SafeId
    snapshot_id: SafeId
    snapshot_fingerprint: Fingerprint
    permit_id: SafeId
    owner_id: OwnerId
    job_id: SafeId
    receipt_id: SafeId
    execution_name: SafeId
    status: PublicationExecutionWorkStatus
    record_version: StrictInt = Field(ge=0)
    attempt_count: StrictInt = Field(ge=0, le=1)
    input_fingerprint: Fingerprint
    fingerprint: Fingerprint
    verification_deadline: UtcDateTime
    next_dispatch_at: UtcDateTime | None
    created_at: UtcDateTime
    dispatched_at: UtcDateTime | None = None
    terminal_at: UtcDateTime | None = None
    updated_at: UtcDateTime

    @classmethod
    def from_request(cls, work: PublicationWorkRequest) -> ExecutionPublicationWork:
        values = {
            **work.model_dump(
                mode="python",
                exclude={"contract_version", "status"},
            ),
            "status": PublicationExecutionWorkStatus.PENDING,
            "dispatched_at": None,
            "terminal_at": None,
        }
        values["fingerprint"] = execution_record_fingerprint(
            "execution_work",
            values,
            excluded_fields=frozenset({"contract_version", "fingerprint"}),
        )
        return cls(**values)

    @model_validator(mode="after")
    def work_lifecycle_is_closed(self) -> ExecutionPublicationWork:
        terminal = {
            PublicationExecutionWorkStatus.SUCCEEDED,
            PublicationExecutionWorkStatus.FAILED,
            PublicationExecutionWorkStatus.OUTCOME_UNKNOWN,
        }
        if self.updated_at < self.created_at:
            raise ValueError("Publication work time cannot move backwards")
        if self.status is PublicationExecutionWorkStatus.PENDING:
            if not (
                self.record_version == 0
                and self.attempt_count == 0
                and self.next_dispatch_at == self.created_at
                and self.dispatched_at is None
                and self.terminal_at is None
            ):
                raise ValueError("Pending publication work must be pristine")
        else:
            if self.next_dispatch_at is not None:
                raise ValueError("Dispatched publication work cannot be queued again")
            if self.attempt_count == 0 and not (
                self.status is PublicationExecutionWorkStatus.FAILED and self.dispatched_at is None
            ):
                raise ValueError("Only pre-dispatch deadline retirement can skip dispatch")
            if self.attempt_count == 1 and self.dispatched_at is None:
                raise ValueError("Dispatched work requires its immutable dispatch timestamp")
            if self.dispatched_at is not None and self.dispatched_at < self.created_at:
                raise ValueError("Dispatch time cannot predate work creation")
            if self.status in terminal:
                if self.terminal_at is None or self.updated_at != self.terminal_at:
                    raise ValueError("Terminal work requires the winning settlement timestamp")
            elif self.terminal_at is not None:
                raise ValueError("Nonterminal work cannot have a terminal timestamp")
        if self.fingerprint != execution_record_fingerprint("execution_work", self):
            raise ValueError("Execution work fingerprint is invalid")
        return self


class PublicationCallClaim(PublicationModel):
    """Durable call accounting record; this record is never reusable as wire authority."""

    authorization_id: SafeId
    operation_id: SafeId
    aggregate_id: SafeId
    attempt_id: SafeId
    snapshot_id: SafeId
    snapshot_fingerprint: Fingerprint
    permit_id: SafeId
    work_request_id: SafeId
    owner_id: OwnerId
    job_id: SafeId
    call_kind: PublicationCallKind
    method: Literal["GET", "POST"]
    route_template: Literal[
        "/v1/shops.json",
        "/v1/shops/{shop_id}/products/{product_id}.json",
        "/v1/shops/{shop_id}/products/{product_id}/publish.json",
    ]
    purpose: PublicationCallPurpose
    printify_shop_id: StrictInt = Field(gt=0)
    printify_product_id: SafeId | None = None
    ordinal: StrictInt = Field(ge=1, le=100)
    call_limit: Literal[1, 3, 100]
    resulting_attempt_record_version: StrictInt = Field(ge=1)
    permit_fingerprint: Fingerprint | None = None
    authorized_at: UtcDateTime
    verification_deadline: UtcDateTime
    mutation_authorized: StrictBool
    fingerprint: Fingerprint

    @model_validator(mode="after")
    def route_and_budget_are_closed(self) -> PublicationCallClaim:
        if self.authorized_at >= self.verification_deadline:
            raise ValueError("No provider call may be authorized at or after the fixed deadline")
        if self.call_kind is PublicationCallKind.SHOP_GET:
            valid = (
                self.method == "GET"
                and self.route_template == "/v1/shops.json"
                and self.purpose is PublicationCallPurpose.SHOP_PREFLIGHT
                and self.printify_product_id is None
                and self.call_limit == 3
                and self.ordinal <= 3
                and self.permit_fingerprint is None
                and not self.mutation_authorized
            )
        elif self.call_kind is PublicationCallKind.PRODUCT_GET:
            valid = (
                self.method == "GET"
                and self.route_template == "/v1/shops/{shop_id}/products/{product_id}.json"
                and self.purpose
                in {
                    PublicationCallPurpose.PRODUCT_PREFLIGHT,
                    PublicationCallPurpose.VERIFICATION,
                    PublicationCallPurpose.RECONCILIATION,
                }
                and self.printify_product_id is not None
                and self.call_limit == 100
                and self.permit_fingerprint is None
                and not self.mutation_authorized
            )
        else:
            valid = (
                self.method == "POST"
                and self.route_template == "/v1/shops/{shop_id}/products/{product_id}/publish.json"
                and self.purpose is PublicationCallPurpose.PUBLISH
                and self.printify_product_id is not None
                and self.call_limit == 1
                and self.ordinal == 1
                and self.permit_fingerprint is not None
                and self.mutation_authorized
            )
        if not valid:
            raise ValueError("Provider call authority differs from the frozen route matrix")
        if self.fingerprint != execution_record_fingerprint("call_claim", self):
            raise ValueError("Provider call claim fingerprint is invalid")
        return self


class PublicationProviderAuditRecord(PublicationModel):
    """Payload-free provider-boundary decision with no dynamic provider or owner identity."""

    decision: PublicationProviderAuditDecision
    method_category: Literal["GET", "POST", "FORBIDDEN"]
    route_template: Literal[
        "/v1/shops.json",
        "/v1/shops/{shop_id}/products/{product_id}.json",
        "/v1/shops/{shop_id}/products/{product_id}/publish.json",
        "forbidden_operation",
    ]
    category: PublicationProviderAuditCategory
    fingerprint: Fingerprint

    @model_validator(mode="after")
    def audit_payload_is_closed_and_sanitized(self) -> PublicationProviderAuditRecord:
        allowed_matrix = {
            PublicationProviderAuditCategory.SHOP_GET_ALLOWED: (
                "GET",
                "/v1/shops.json",
            ),
            PublicationProviderAuditCategory.PRODUCT_GET_ALLOWED: (
                "GET",
                "/v1/shops/{shop_id}/products/{product_id}.json",
            ),
            PublicationProviderAuditCategory.PUBLISH_POST_ALLOWED: (
                "POST",
                "/v1/shops/{shop_id}/products/{product_id}/publish.json",
            ),
        }
        if self.decision is PublicationProviderAuditDecision.ALLOWED:
            expected = allowed_matrix.get(self.category)
            if expected is None or (self.method_category, self.route_template) != expected:
                raise ValueError(
                    "Allowed provider audit record differs from the closed route matrix"
                )
        elif (
            self.category in allowed_matrix
            or self.method_category != "FORBIDDEN"
            or self.route_template != "forbidden_operation"
        ):
            raise ValueError("Rejected provider audit records cannot retain dynamic authority")
        if self.fingerprint != execution_record_fingerprint("provider_audit_record", self):
            raise ValueError("Provider audit record fingerprint is invalid")
        return self


class PublicationProviderAuditBinding(PublicationModel):
    """Private operational join kept outside the sanitized audit payload."""

    aggregate_id: SafeId
    call_claim_id: SafeId
    call_claim_fingerprint: Fingerprint
    durable_call_sequence: StrictInt = Field(ge=1, le=104)
    audit_record: PublicationProviderAuditRecord
    fingerprint: Fingerprint

    @model_validator(mode="after")
    def join_is_content_bound(self) -> PublicationProviderAuditBinding:
        if self.audit_record.decision is not PublicationProviderAuditDecision.ALLOWED:
            raise ValueError("Aggregate audit binding requires an allowed decision")
        if self.fingerprint != execution_record_fingerprint("provider_audit_binding", self):
            raise ValueError("Provider audit binding fingerprint is invalid")
        return self


class PublicationPreflightProof(PublicationModel):
    """Application-validated proof that every fallible pre-call check completed."""

    proof_id: SafeId
    aggregate_id: SafeId
    attempt_id: SafeId
    snapshot_id: SafeId
    snapshot_fingerprint: Fingerprint
    provider_authority_id: SafeId
    provider_authority_fingerprint: Fingerprint
    shop_evidence_fingerprint: Fingerprint
    product_evidence_fingerprint: Fingerprint
    shop_observed_at: UtcDateTime
    product_observed_at: UtcDateTime
    shop_call_claim_id: SafeId
    shop_call_claim_fingerprint: Fingerprint
    product_call_claim_id: SafeId
    product_call_claim_fingerprint: Fingerprint
    printify_shop_id: StrictInt = Field(gt=0)
    printify_product_id: SafeId
    local_authority_reconstructed: Literal[True] = True
    shop_connected_to_etsy: Literal[True] = True
    exact_product_match: Literal[True] = True
    canonical_content_match: Literal[True] = True
    exact_variants_match: Literal[True] = True
    product_unlocked: Literal[True] = True
    product_unpublished: Literal[True] = True
    publication_body_fingerprint: Fingerprint
    proven_at: UtcDateTime
    verification_deadline: UtcDateTime
    fingerprint: Fingerprint

    @model_validator(mode="after")
    def proof_is_pre_call_and_exact(self) -> PublicationPreflightProof:
        if self.proven_at >= self.verification_deadline:
            raise ValueError("Preflight proof must precede the fixed deadline")
        if max(self.shop_observed_at, self.product_observed_at) > self.proven_at:
            raise ValueError("Preflight proof cannot predate provider evidence")
        if self.publication_body_fingerprint != publication_body_fingerprint():
            raise ValueError("Preflight must bind the frozen exact publication body")
        if self.fingerprint != execution_record_fingerprint("preflight_proof", self):
            raise ValueError("Preflight proof fingerprint is invalid")
        return self


class ExpectedVariantEconomics(PublicationModel):
    """Capability-free exact economics reconstructed from immutable Phase 6 records."""

    variant_id: StrictInt = Field(gt=0)
    retail_price_cents: StrictInt = Field(gt=0)
    production_cost_cents: StrictInt = Field(ge=0)


class PublicationProviderAuthority(PublicationModel):
    """Re-read application-owned authority proven before any provider call is claimed."""

    provider_authority_id: SafeId
    aggregate_id: SafeId
    attempt_id: SafeId
    snapshot_id: SafeId
    snapshot_fingerprint: Fingerprint
    owner_id: OwnerId
    job_id: SafeId
    permit_id: SafeId
    work_request_id: SafeId
    phase6_record_version: StrictInt = Field(ge=1)
    approval_fingerprint: Fingerprint
    review_fingerprint: Fingerprint
    product_sync_fingerprint: Fingerprint
    pricing_snapshot_fingerprint: Fingerprint
    pricing_evidence_fingerprint: Fingerprint
    profile_fingerprint: Fingerprint
    release_manifest_fingerprint: Fingerprint
    printify_shop_id: StrictInt = Field(gt=0)
    printify_product_id: SafeId
    printify_image_id: SafeId
    product_payload_fingerprint: Fingerprint
    expected_variant_economics: tuple[ExpectedVariantEconomics, ...] = Field(
        min_length=1,
        max_length=100,
    )
    expected_mockup_fingerprints: tuple[Fingerprint, ...] = Field(
        min_length=1,
        max_length=20,
    )
    expected_sales_channel: Literal["etsy"] = "etsy"
    publication_body_fingerprint: Fingerprint
    pricing_fresh_until: UtcDateTime
    reconstructed_at: UtcDateTime
    verification_deadline: UtcDateTime
    fingerprint: Fingerprint

    @model_validator(mode="after")
    def authority_is_pre_deadline_and_content_bound(self) -> PublicationProviderAuthority:
        if self.reconstructed_at >= self.verification_deadline:
            raise ValueError("Provider authority must be reconstructed before the fixed deadline")
        if self.reconstructed_at >= self.pricing_fresh_until:
            raise ValueError("Provider authority requires live pricing authority")
        variant_ids = tuple(item.variant_id for item in self.expected_variant_economics)
        if len(set(variant_ids)) != len(variant_ids):
            raise ValueError("Expected publication variants must be unique")
        if len(set(self.expected_mockup_fingerprints)) != len(self.expected_mockup_fingerprints):
            raise ValueError("Expected publication mockups must be unique")
        if self.publication_body_fingerprint != publication_body_fingerprint():
            raise ValueError("Provider authority must bind the frozen publication body")
        if self.fingerprint != execution_record_fingerprint("provider_authority", self):
            raise ValueError("Provider authority fingerprint is invalid")
        return self


class PublicationShopPreflightEvidence(PublicationModel):
    """Sanitized exact-shop evidence returned by the sealed provider boundary."""

    call_claim_id: SafeId
    call_claim_fingerprint: Fingerprint
    provider_authority_id: SafeId
    provider_authority_fingerprint: Fingerprint
    printify_shop_id: StrictInt = Field(gt=0)
    sales_channel: Literal["etsy"]
    sanitized_response_fingerprint: Fingerprint
    observed_at: UtcDateTime
    fingerprint: Fingerprint

    @model_validator(mode="after")
    def evidence_is_content_bound(self) -> PublicationShopPreflightEvidence:
        if self.fingerprint != execution_record_fingerprint("shop_preflight_evidence", self):
            raise ValueError("Shop preflight evidence fingerprint is invalid")
        return self


class PublicationProductReadEvidence(PublicationModel):
    """Sanitized exact-product read evidence; raw provider payloads are forbidden."""

    call_claim_id: SafeId
    call_claim_fingerprint: Fingerprint
    provider_authority_id: SafeId
    provider_authority_fingerprint: Fingerprint
    printify_shop_id: StrictInt = Field(gt=0)
    printify_product_id: SafeId
    sanitized_response_fingerprint: Fingerprint
    product_present: StrictBool
    canonical_payload_fingerprint: Fingerprint | None = None
    canonical_content_match: StrictBool
    exact_variant_economics: StrictBool
    exact_placement_image: StrictBool
    exact_mockups: StrictBool
    is_locked: StrictBool | None = None
    visible: StrictBool | None = None
    external_evidence: PublicationExternalEvidenceState
    numeric_listing_id: StrictInt | None = Field(default=None, ge=1, le=9_999_999_999_999)
    read_outcome: PublicationReadOutcome
    observed_at: UtcDateTime
    fingerprint: Fingerprint

    @property
    def preflight_satisfied(self) -> bool:
        return (
            self.product_present
            and self.canonical_content_match
            and self.exact_variant_economics
            and self.exact_placement_image
            and self.exact_mockups
            and self.is_locked is False
            and self.external_evidence is PublicationExternalEvidenceState.ABSENT
        )

    @property
    def safe_listing_url(self) -> str | None:
        if self.read_outcome is not PublicationReadOutcome.POSITIVE_PROOF:
            return None
        assert self.numeric_listing_id is not None
        return f"https://www.etsy.com/listing/{self.numeric_listing_id}"

    @model_validator(mode="after")
    def proof_classification_is_closed(self) -> PublicationProductReadEvidence:
        positive = (
            self.product_present
            and self.canonical_content_match
            and self.exact_variant_economics
            and self.exact_placement_image
            and self.exact_mockups
            and self.is_locked is False
            and self.visible is True
            and self.external_evidence
            is PublicationExternalEvidenceState.SINGLE_NUMERIC_ETSY_REFERENCE
            and self.numeric_listing_id is not None
            and self.canonical_payload_fingerprint is not None
        )
        if (self.read_outcome is PublicationReadOutcome.POSITIVE_PROOF) != positive:
            raise ValueError("Only complete exact-product evidence may prove publication")
        if (
            self.external_evidence
            is not PublicationExternalEvidenceState.SINGLE_NUMERIC_ETSY_REFERENCE
            and self.numeric_listing_id is not None
        ):
            raise ValueError("Non-positive external evidence cannot expose a listing identity")
        if self.product_present != (self.canonical_payload_fingerprint is not None):
            raise ValueError("Product presence must match sanitized canonical evidence")
        if self.fingerprint != execution_record_fingerprint("product_read_evidence", self):
            raise ValueError("Product read evidence fingerprint is invalid")
        return self


class PublicationPublishEvidence(PublicationModel):
    """Sanitized provider-boundary evidence for the sole publish POST."""

    call_claim_id: SafeId
    call_claim_fingerprint: Fingerprint
    mutation_claim_id: SafeId
    mutation_claim_fingerprint: Fingerprint
    provider_authority_id: SafeId
    provider_authority_fingerprint: Fingerprint
    outcome: Literal[
        PublicationPostOutcome.DEFINITELY_ACCEPTED,
        PublicationPostOutcome.AMBIGUOUS,
    ]
    response_category: Literal[
        PublicationPublishResponseCategory.VALIDATED_2XX,
        PublicationPublishResponseCategory.NON_2XX,
        PublicationPublishResponseCategory.MALFORMED_2XX,
        PublicationPublishResponseCategory.TRANSPORT_FAILURE,
    ]
    sanitized_response_fingerprint: Fingerprint | None = None
    observed_at: UtcDateTime
    fingerprint: Fingerprint

    @model_validator(mode="after")
    def classification_is_closed(self) -> PublicationPublishEvidence:
        accepted = self.outcome is PublicationPostOutcome.DEFINITELY_ACCEPTED
        if accepted != (self.response_category is PublicationPublishResponseCategory.VALIDATED_2XX):
            raise ValueError("Only a bounded parsed 2xx response is definite acceptance")
        if self.response_category is PublicationPublishResponseCategory.TRANSPORT_FAILURE:
            if self.sanitized_response_fingerprint is not None:
                raise ValueError("Transport failure cannot retain provider response evidence")
        elif self.sanitized_response_fingerprint is None:
            raise ValueError("Completed provider response requires a sanitized fingerprint")
        if self.fingerprint != execution_record_fingerprint("publish_evidence", self):
            raise ValueError("Publish evidence fingerprint is invalid")
        return self


class PublicationMutationClaim(PublicationModel):
    """Durable pre-mutation record created atomically with consuming the permit."""

    mutation_claim_id: SafeId
    call_claim_id: SafeId
    call_claim_fingerprint: Fingerprint
    aggregate_id: SafeId
    attempt_id: SafeId
    snapshot_id: SafeId
    snapshot_fingerprint: Fingerprint
    permit_id: SafeId
    work_request_id: SafeId
    preflight_proof_id: SafeId
    preflight_proof_fingerprint: Fingerprint
    consumed_permit_fingerprint: Fingerprint
    publication_body_fingerprint: Fingerprint
    ordinal: Literal[1] = 1
    authorized_at: UtcDateTime
    verification_deadline: UtcDateTime
    fingerprint: Fingerprint

    @model_validator(mode="after")
    def claim_is_one_shot_and_pre_deadline(self) -> PublicationMutationClaim:
        if self.authorized_at >= self.verification_deadline:
            raise ValueError("Publication mutation cannot be claimed after the fixed deadline")
        if self.publication_body_fingerprint != publication_body_fingerprint():
            raise ValueError("Mutation claim must bind the frozen exact publication body")
        if self.fingerprint != execution_record_fingerprint("mutation_claim", self):
            raise ValueError("Mutation claim fingerprint is invalid")
        return self


class PublicationPostObservation(PublicationModel):
    """Sanitized classification of the sole publish POST; raw responses are forbidden."""

    observation_id: SafeId
    aggregate_id: SafeId
    attempt_id: SafeId
    mutation_claim_id: SafeId
    call_claim_id: SafeId
    call_claim_fingerprint: Fingerprint
    mutation_claim_fingerprint: Fingerprint
    provider_authority_id: SafeId
    provider_authority_fingerprint: Fingerprint
    provider_evidence_fingerprint: Fingerprint | None = None
    outcome: PublicationPostOutcome
    response_category: PublicationPublishResponseCategory
    sanitized_response_fingerprint: Fingerprint | None = None
    provider_outcome_uncertain: StrictBool
    observed_at: UtcDateTime
    fingerprint: Fingerprint

    @model_validator(mode="after")
    def classification_is_fail_closed(self) -> PublicationPostObservation:
        recovery = (
            self.response_category.value == "consumed_claim_without_durable_boundary_observation"
        )
        if self.outcome is PublicationPostOutcome.DEFINITELY_ACCEPTED:
            valid = (
                self.response_category is PublicationPublishResponseCategory.VALIDATED_2XX
                and self.sanitized_response_fingerprint is not None
                and self.provider_evidence_fingerprint is not None
                and not self.provider_outcome_uncertain
            )
        elif self.outcome is PublicationPostOutcome.DEFINITIVE_SYNCHRONOUS_REJECTION:
            valid = False
        else:
            valid = (
                self.response_category
                in {
                    PublicationPublishResponseCategory.NON_2XX,
                    PublicationPublishResponseCategory.MALFORMED_2XX,
                    PublicationPublishResponseCategory.TRANSPORT_FAILURE,
                    PublicationPublishResponseCategory.CONSUMED_CLAIM_WITHOUT_DURABLE_BOUNDARY_OBSERVATION,
                }
                and self.provider_outcome_uncertain
                and (
                    self.provider_evidence_fingerprint is None
                    if recovery
                    else self.provider_evidence_fingerprint is not None
                )
                and (
                    self.sanitized_response_fingerprint is None
                    if self.response_category
                    in {
                        PublicationPublishResponseCategory.TRANSPORT_FAILURE,
                        PublicationPublishResponseCategory.CONSUMED_CLAIM_WITHOUT_DURABLE_BOUNDARY_OBSERVATION,
                    }
                    else self.sanitized_response_fingerprint is not None
                )
            )
        if not valid:
            raise ValueError("Publish response classification is not closed and fail-safe")
        if self.fingerprint != execution_record_fingerprint("post_observation", self):
            raise ValueError("Publish POST observation fingerprint is invalid")
        return self


class PublicationProductObservation(PublicationModel):
    """Positive-proof-only exact-product GET observation."""

    observation_id: SafeId
    aggregate_id: SafeId
    attempt_id: SafeId
    snapshot_id: SafeId
    snapshot_fingerprint: Fingerprint
    call_claim_id: SafeId
    call_claim_fingerprint: Fingerprint
    provider_authority_id: SafeId
    provider_authority_fingerprint: Fingerprint
    provider_evidence_fingerprint: Fingerprint
    sanitized_response_fingerprint: Fingerprint
    outcome: PublicationReadOutcome
    exact_shop: StrictBool
    exact_product: StrictBool
    unlocked: StrictBool
    visible: StrictBool
    canonical_content_match: StrictBool
    single_etsy_external_reference: StrictBool
    no_conflicting_external_reference: StrictBool
    numeric_listing_id: StrictInt | None = Field(default=None, ge=1, le=9_999_999_999_999)
    verified_product_fingerprint: Fingerprint | None = None
    observed_at: UtcDateTime
    verification_deadline: UtcDateTime
    resulting_aggregate_record_version: StrictInt = Field(ge=1)
    fingerprint: Fingerprint

    @model_validator(mode="after")
    def only_complete_positive_evidence_can_prove_success(
        self,
    ) -> PublicationProductObservation:
        positive_fields = (
            self.exact_shop,
            self.exact_product,
            self.unlocked,
            self.visible,
            self.canonical_content_match,
            self.single_etsy_external_reference,
            self.no_conflicting_external_reference,
        )
        if self.outcome is PublicationReadOutcome.POSITIVE_PROOF:
            if (
                not all(positive_fields)
                or self.numeric_listing_id is None
                or self.verified_product_fingerprint is None
                or self.observed_at >= self.verification_deadline
            ):
                raise ValueError("Success requires complete positive proof before the deadline")
        elif self.numeric_listing_id is not None or self.verified_product_fingerprint is not None:
            raise ValueError("Non-positive observations cannot retain product or listing proof")
        if self.fingerprint != execution_record_fingerprint("product_observation", self):
            raise ValueError("Product observation fingerprint is invalid")
        return self


class PublicationResult(PublicationModel):
    """Safe immutable result derived only from a positive product observation."""

    result_id: SafeId
    aggregate_id: SafeId
    observation_id: SafeId
    observation_fingerprint: Fingerprint
    numeric_listing_id: StrictInt = Field(ge=1, le=9_999_999_999_999)
    canonical_link_fingerprint: Fingerprint
    verified_product_fingerprint: Fingerprint
    verified_at: UtcDateTime
    fingerprint: Fingerprint

    @property
    def safe_listing_url(self) -> str:
        return f"https://www.etsy.com/listing/{self.numeric_listing_id}"

    @model_validator(mode="after")
    def link_is_application_derived(self) -> PublicationResult:
        if self.canonical_link_fingerprint != safe_listing_link_fingerprint(
            self.numeric_listing_id
        ):
            raise ValueError("Result link differs from the application-derived Etsy URL")
        if self.fingerprint != execution_record_fingerprint("publication_result", self):
            raise ValueError("Publication result fingerprint is invalid")
        return self


class PublicationNotification(PublicationModel):
    """Authenticated in-application notification created only with a positive result."""

    notification_id: SafeId
    aggregate_id: SafeId
    result_id: SafeId
    result_fingerprint: Fingerprint
    channel: Literal["authenticated_in_application"] = "authenticated_in_application"
    created_at: UtcDateTime
    fingerprint: Fingerprint

    @model_validator(mode="after")
    def notification_is_content_bound(self) -> PublicationNotification:
        if self.fingerprint != execution_record_fingerprint("publication_notification", self):
            raise ValueError("Publication notification fingerprint is invalid")
        return self


class PublicationTerminalReport(PublicationModel):
    """Immutable payload-free terminal report."""

    report_id: SafeId
    aggregate_id: SafeId
    owner_digest: Fingerprint
    job_digest: Fingerprint
    terminal_state: Literal[
        PublicationState.PUBLISHED,
        PublicationState.PUBLICATION_FAILED,
        PublicationState.PUBLICATION_OUTCOME_UNKNOWN,
    ]
    terminal_reason: PublicationTerminalReason
    requested_at: UtcDateTime
    terminal_at: UtcDateTime
    source_release_eligible_at: UtcDateTime
    operational_expires_at: UtcDateTime
    shop_get_call_count: StrictInt = Field(ge=0, le=3)
    product_get_call_count: StrictInt = Field(ge=0, le=100)
    publish_post_call_count: StrictInt = Field(ge=0, le=1)
    release_manifest_fingerprint: Fingerprint
    snapshot_fingerprint: Fingerprint
    attempt_fingerprint: Fingerprint
    permit_fingerprint: Fingerprint
    observation_fingerprint: Fingerprint | None = None
    result_fingerprint: Fingerprint | None = None
    sanitized_audit_record_digests: tuple[Fingerprint, ...] = Field(max_length=104)
    fingerprint: Fingerprint

    @classmethod
    def identity_digests(cls, owner_id: str, job_id: str) -> tuple[str, str]:
        return (
            safe_identity_digest("publication_owner", owner_id),
            safe_identity_digest("publication_job", job_id),
        )

    @model_validator(mode="after")
    def report_is_sanitized_and_retention_bound(self) -> PublicationTerminalReport:
        if self.terminal_at < self.requested_at:
            raise ValueError("Terminal report time cannot predate the request")
        if self.source_release_eligible_at != self.terminal_at + timedelta(days=30):
            raise ValueError("Report source release time is not terminal anchored")
        if self.operational_expires_at != self.terminal_at + timedelta(days=90):
            raise ValueError("Report expiry is not terminal anchored")
        if self.terminal_state is PublicationState.PUBLISHED:
            if (
                self.terminal_reason is not PublicationTerminalReason.POSITIVE_PUBLICATION_PROOF
                or self.observation_fingerprint is None
                or self.result_fingerprint is None
            ):
                raise ValueError("Success report requires positive observation and result")
        elif self.terminal_state is PublicationState.PUBLICATION_FAILED:
            if self.terminal_reason not in {
                PublicationTerminalReason.DEFINITIVE_PREFLIGHT_FAILURE,
                PublicationTerminalReason.PRE_CALL_DEADLINE_EXPIRED,
                PublicationTerminalReason.DEFINITIVE_SYNCHRONOUS_REJECTION,
            }:
                raise ValueError("Failed report reason is not a definitive failure authority")
            if self.result_fingerprint is not None:
                raise ValueError("A failed report cannot contain a publication result")
        elif (
            self.terminal_reason
            is not PublicationTerminalReason.FIXED_DEADLINE_WITHOUT_POSITIVE_PROOF
            or self.result_fingerprint is not None
        ):
            raise ValueError("Unknown outcome requires deadline expiry without positive proof")
        if self.fingerprint != execution_record_fingerprint("terminal_report", self):
            raise ValueError("Terminal report fingerprint is invalid")
        return self


class PublicationAggregateTombstone(PublicationModel):
    """Job-level duplicate-prevention authority retained through operational expiry."""

    tombstone_id: SafeId
    owner_id: OwnerId
    job_id: SafeId
    aggregate_id: SafeId
    terminal_state: Literal[
        PublicationState.PUBLISHED,
        PublicationState.PUBLICATION_FAILED,
        PublicationState.PUBLICATION_OUTCOME_UNKNOWN,
    ]
    terminal_at: UtcDateTime
    operational_expires_at: UtcDateTime
    report_id: SafeId
    report_fingerprint: Fingerprint
    fingerprint: Fingerprint

    @model_validator(mode="after")
    def tombstone_covers_operational_lifetime(self) -> PublicationAggregateTombstone:
        if self.operational_expires_at != self.terminal_at + timedelta(days=90):
            raise ValueError("Aggregate tombstone must survive through operational expiry")
        if self.fingerprint != execution_record_fingerprint("aggregate_tombstone", self):
            raise ValueError("Aggregate tombstone fingerprint is invalid")
        return self


class PublicationTerminalJobLink(PublicationModel):
    """Pure terminal-summary instruction for the still-APPROVED Phase 6 job/projection."""

    owner_id: OwnerId
    job_id: SafeId
    aggregate_id: SafeId
    phase6_state: Literal["approved"] = "approved"
    expected_record_version: StrictInt = Field(ge=0)
    result_record_version: StrictInt = Field(ge=1)
    expected_event_sequence: StrictInt = Field(ge=0)
    result_event_sequence: StrictInt = Field(ge=0)
    terminal_state: Literal[
        PublicationState.PUBLISHED,
        PublicationState.PUBLICATION_FAILED,
        PublicationState.PUBLICATION_OUTCOME_UNKNOWN,
    ]
    terminal_at: UtcDateTime
    report_id: SafeId
    report_fingerprint: Fingerprint
    result_id: SafeId | None = None
    terminal_summary_fingerprint: Fingerprint
    source_release_eligible_at: UtcDateTime
    operational_expires_at: UtcDateTime
    fingerprint: Fingerprint

    @model_validator(mode="after")
    def link_changes_only_terminal_publication_summary(self) -> PublicationTerminalJobLink:
        if self.result_record_version != self.expected_record_version + 1:
            raise ValueError("Terminal publication summary increments Phase 6 version once")
        if self.result_event_sequence != self.expected_event_sequence:
            raise ValueError("Separate publication settlement cannot change Phase 6 event sequence")
        if self.source_release_eligible_at != self.terminal_at + timedelta(days=30):
            raise ValueError("Terminal job source release must be terminal_at plus 30 days")
        if self.operational_expires_at != self.terminal_at + timedelta(days=90):
            raise ValueError("Terminal job tombstone must survive operational expiry")
        if self.terminal_state is PublicationState.PUBLISHED:
            if self.result_id is None:
                raise ValueError("Published job summary requires the safe result identity")
        elif self.result_id is not None:
            raise ValueError("Only published job summary can carry a result identity")
        if self.fingerprint != execution_record_fingerprint("terminal_job_link", self):
            raise ValueError("Terminal publication job link fingerprint is invalid")
        return self


class PublicationExecutionEvent(PublicationModel):
    aggregate_id: SafeId
    owner_id: OwnerId
    job_id: SafeId
    sequence: StrictInt = Field(ge=2)
    name: PublicationExecutionEventName
    state: PublicationState
    operation_id: SafeId
    authority_fingerprint: Fingerprint
    occurred_at: UtcDateTime
    fingerprint: Fingerprint

    @model_validator(mode="after")
    def event_is_content_bound(self) -> PublicationExecutionEvent:
        if self.state is PublicationState.APPROVED:
            raise ValueError("Publication events cannot enter the Phase 6 state machine")
        if self.fingerprint != execution_record_fingerprint("execution_event", self):
            raise ValueError("Publication execution event fingerprint is invalid")
        return self


class PublicationExecutionReceipt(PublicationModel):
    """Idempotent result for one internal application-owned execution transition."""

    receipt_id: SafeId
    operation_id: SafeId
    operation: PublicationExecutionOperation
    aggregate_id: SafeId
    owner_id: OwnerId
    job_id: SafeId
    request_fingerprint: Fingerprint
    aggregate_state: PublicationState
    aggregate_record_version: StrictInt = Field(ge=0)
    attempt_record_version: StrictInt = Field(ge=0)
    permit_record_version: StrictInt = Field(ge=0, le=1)
    work_record_version: StrictInt = Field(ge=0)
    authority_record_id: SafeId
    authority_fingerprint: Fingerprint
    created_at: UtcDateTime
    fingerprint: Fingerprint

    @model_validator(mode="after")
    def receipt_is_content_bound(self) -> PublicationExecutionReceipt:
        if self.aggregate_state is PublicationState.APPROVED:
            raise ValueError("Execution receipt cannot reference a Phase 6 state")
        if self.fingerprint != execution_record_fingerprint("execution_receipt", self):
            raise ValueError("Publication execution receipt fingerprint is invalid")
        return self


class PublicationExecutionAuthority(PublicationModel):
    """Complete current authority loaded under owner scope for one transition."""

    snapshot: PublicationSnapshot
    request_job_link: PublicationJobLink
    phase6_record_version: StrictInt = Field(ge=1)
    phase6_event_sequence: StrictInt = Field(ge=0)
    expected_aggregate: PublicationAggregate | ExecutionPublicationAggregate
    expected_attempt: PublicationAttempt | ExecutionPublicationAttempt
    expected_permit: PublicationPermit | ExecutionPublicationPermit
    expected_work: PublicationWorkRequest | ExecutionPublicationWork
    aggregate: ExecutionPublicationAggregate
    attempt: ExecutionPublicationAttempt
    permit: ExecutionPublicationPermit
    work: ExecutionPublicationWork
    call_claims: tuple[PublicationCallClaim, ...] = ()
    provider_audits: tuple[PublicationProviderAuditBinding, ...] = ()
    provider_authority: PublicationProviderAuthority | None = None
    preflight_proof: PublicationPreflightProof | None = None
    mutation_claim: PublicationMutationClaim | None = None
    post_observation: PublicationPostObservation | None = None
    product_observations: tuple[PublicationProductObservation, ...] = ()
    last_product_observation: PublicationProductObservation | None = None
    result: PublicationResult | None = None
    notification: PublicationNotification | None = None
    report: PublicationTerminalReport | None = None
    tombstone: PublicationAggregateTombstone | None = None
    terminal_job_link: PublicationTerminalJobLink | None = None

    @model_validator(mode="after")
    def graph_is_one_closed_authority(self) -> PublicationExecutionAuthority:
        snapshot = self.snapshot
        request_link = self.request_job_link
        aggregate = self.aggregate
        attempt = self.attempt
        permit = self.permit
        work = self.work
        if (
            request_link.owner_id != snapshot.owner_id
            or request_link.job_id != snapshot.job_id
            or request_link.publication_aggregate_id != aggregate.aggregate_id
            or request_link.linked_at != aggregate.requested_at
        ):
            raise ValueError("Execution authority must retain the exact Phase 7.1 job link")
        pristine_basis = isinstance(self.expected_aggregate, PublicationAggregate)
        expected_records = (
            self.expected_aggregate,
            self.expected_attempt,
            self.expected_permit,
            self.expected_work,
        )
        if pristine_basis:
            if not (
                isinstance(self.expected_attempt, PublicationAttempt)
                and isinstance(self.expected_permit, PublicationPermit)
                and isinstance(self.expected_work, PublicationWorkRequest)
                and aggregate
                == ExecutionPublicationAggregate.from_request(
                    self.expected_aggregate,
                    snapshot,
                )
                and attempt == ExecutionPublicationAttempt.from_request(self.expected_attempt)
                and permit
                == ExecutionPublicationPermit.from_request(
                    self.expected_permit,
                    snapshot.verification_deadline,
                )
                and work == ExecutionPublicationWork.from_request(self.expected_work)
            ):
                raise ValueError("Pristine execution CAS basis does not match the Phase 7.1 rows")
        elif not (
            isinstance(self.expected_aggregate, ExecutionPublicationAggregate)
            and isinstance(self.expected_attempt, ExecutionPublicationAttempt)
            and isinstance(self.expected_permit, ExecutionPublicationPermit)
            and isinstance(self.expected_work, ExecutionPublicationWork)
            and self.expected_aggregate == aggregate
            and self.expected_attempt == attempt
            and self.expected_permit == permit
            and self.expected_work == work
        ):
            raise ValueError("Evolved execution CAS basis does not match current records")
        if pristine_basis != any(
            isinstance(
                record,
                (
                    PublicationAggregate,
                    PublicationAttempt,
                    PublicationPermit,
                    PublicationWorkRequest,
                ),
            )
            for record in expected_records[1:]
        ):
            raise ValueError("Execution CAS basis cannot mix pristine and evolved rows")
        identities = (
            (aggregate.owner_id, aggregate.job_id),
            (attempt.owner_id, attempt.job_id),
            (permit.owner_id, permit.job_id),
            (work.owner_id, work.job_id),
        )
        if any(identity != (snapshot.owner_id, snapshot.job_id) for identity in identities):
            raise ValueError("Execution records must bind the exact owner and job")
        if any(
            value != aggregate.aggregate_id
            for value in (attempt.aggregate_id, permit.aggregate_id, work.aggregate_id)
        ):
            raise ValueError("Execution records must bind one aggregate")
        if any(
            value != snapshot.snapshot_id
            for value in (
                aggregate.snapshot_id,
                attempt.snapshot_id,
                permit.snapshot_id,
                work.snapshot_id,
            )
        ) or any(
            value != snapshot.fingerprint
            for value in (
                aggregate.snapshot_fingerprint,
                attempt.snapshot_fingerprint,
                permit.snapshot_fingerprint,
                work.snapshot_fingerprint,
            )
        ):
            raise ValueError("Execution records must bind the immutable snapshot")
        if (
            aggregate.attempt_id != attempt.attempt_id
            or permit.attempt_id != attempt.attempt_id
            or work.attempt_id != attempt.attempt_id
            or aggregate.permit_id != permit.permit_id
            or work.permit_id != permit.permit_id
            or aggregate.work_request_id != work.work_request_id
            or permit.work_request_id != work.work_request_id
        ):
            raise ValueError("Execution identities are inconsistent")
        if any(
            deadline != snapshot.verification_deadline
            for deadline in (
                aggregate.verification_deadline,
                attempt.verification_deadline,
                permit.verification_deadline,
                work.verification_deadline,
            )
        ):
            raise ValueError("Execution records cannot move the root deadline")
        if (
            permit.status is PublicationPermitState.AVAILABLE
            and aggregate.state is not PublicationState.PUBLICATION_REQUESTED
        ):
            raise ValueError("An available permit is valid only before publication is claimed")
        if (
            permit.status is PublicationPermitState.RETIRED
            and aggregate.state is not PublicationState.PUBLICATION_FAILED
        ):
            raise ValueError("Permit retirement and pre-call failure must settle atomically")
        if (
            aggregate.state
            in {
                PublicationState.PUBLICATION_VERIFYING,
                PublicationState.PUBLICATION_RECONCILING,
                PublicationState.PUBLISHED,
                PublicationState.PUBLICATION_OUTCOME_UNKNOWN,
            }
            and permit.status is not PublicationPermitState.CONSUMED
        ):
            raise ValueError("Every post-call state requires the consumed permit")
        if permit.status is PublicationPermitState.CONSUMED and self.mutation_claim is None:
            raise ValueError("Consumed permit requires the durable mutation claim")
        if (
            aggregate.state is PublicationState.PUBLICATION_FAILED
            and permit.status is PublicationPermitState.AVAILABLE
        ):
            raise ValueError("No terminal state may retain an available permit")

        claims_by_id = {claim.authorization_id: claim for claim in self.call_claims}
        if len(claims_by_id) != len(self.call_claims):
            raise ValueError("Provider call claims must be unique")
        if self.call_claims and self.provider_authority is None:
            raise ValueError("Provider calls require re-read application authority")
        for claim in self.call_claims:
            if (
                claim.aggregate_id != aggregate.aggregate_id
                or claim.attempt_id != attempt.attempt_id
                or claim.snapshot_id != snapshot.snapshot_id
                or claim.snapshot_fingerprint != snapshot.fingerprint
                or claim.permit_id != permit.permit_id
                or claim.work_request_id != work.work_request_id
                or claim.owner_id != snapshot.owner_id
                or claim.job_id != snapshot.job_id
                or claim.printify_shop_id != snapshot.printify_shop_id
                or claim.verification_deadline != snapshot.verification_deadline
                or (
                    claim.call_kind is not PublicationCallKind.SHOP_GET
                    and claim.printify_product_id != snapshot.printify_product_id
                )
            ):
                raise ValueError("Provider call claim differs from immutable execution authority")
        for kind, expected_count in (
            (PublicationCallKind.SHOP_GET, attempt.shop_get_call_count),
            (PublicationCallKind.PRODUCT_GET, attempt.product_get_call_count),
            (PublicationCallKind.PUBLISH_POST, attempt.publish_post_call_count),
        ):
            ordinals = sorted(
                claim.ordinal for claim in self.call_claims if claim.call_kind is kind
            )
            if ordinals != list(range(1, expected_count + 1)):
                raise ValueError("Durable call claims must exactly account for attempt counters")
        total_calls = (
            attempt.shop_get_call_count
            + attempt.product_get_call_count
            + attempt.publish_post_call_count
        )
        if sorted(claim.resulting_attempt_record_version for claim in self.call_claims) != list(
            range(1, total_calls + 1)
        ):
            raise ValueError("Call claims must bind every counter-version transition exactly once")
        expected_audit = {
            PublicationCallKind.SHOP_GET: (
                "GET",
                "/v1/shops.json",
                PublicationProviderAuditCategory.SHOP_GET_ALLOWED,
            ),
            PublicationCallKind.PRODUCT_GET: (
                "GET",
                "/v1/shops/{shop_id}/products/{product_id}.json",
                PublicationProviderAuditCategory.PRODUCT_GET_ALLOWED,
            ),
            PublicationCallKind.PUBLISH_POST: (
                "POST",
                "/v1/shops/{shop_id}/products/{product_id}/publish.json",
                PublicationProviderAuditCategory.PUBLISH_POST_ALLOWED,
            ),
        }
        audited_claim_ids: set[str] = set()
        audit_sequences: list[int] = []
        for binding in self.provider_audits:
            claim = claims_by_id.get(binding.call_claim_id)
            audit = binding.audit_record
            if (
                claim is None
                or binding.aggregate_id != aggregate.aggregate_id
                or binding.call_claim_fingerprint != claim.fingerprint
                or binding.durable_call_sequence != claim.resulting_attempt_record_version
            ):
                raise ValueError("Provider audit binding differs from its durable call claim")
            method, route, category = expected_audit[claim.call_kind]
            if (
                audit.decision is not PublicationProviderAuditDecision.ALLOWED
                or audit.method_category != method
                or audit.route_template != route
                or audit.category is not category
            ):
                raise ValueError("Provider audit order differs from durable call claims")
            audited_claim_ids.add(claim.authorization_id)
            audit_sequences.append(binding.durable_call_sequence)
        if len(audited_claim_ids) != len(self.provider_audits) or audit_sequences != sorted(
            audit_sequences
        ):
            raise ValueError("Provider audit bindings must be unique and in durable call order")
        if aggregate.provider_audit_record_version != len(self.provider_audits):
            raise ValueError("Provider audit watermark must exactly count durable audit records")
        publish_claims = tuple(
            claim
            for claim in self.call_claims
            if claim.call_kind is PublicationCallKind.PUBLISH_POST
        )
        if permit.status in {
            PublicationPermitState.AVAILABLE,
            PublicationPermitState.RETIRED,
        }:
            if attempt.publish_post_call_count != 0 or publish_claims or self.mutation_claim:
                raise ValueError("Unconsumed permit must prove zero POST authority")
        elif (
            attempt.publish_post_call_count != 1
            or len(publish_claims) != 1
            or self.mutation_claim is None
        ):
            raise ValueError("Consumed permit must bind exactly one durable POST claim")

        if self.preflight_proof is not None:
            proof = self.preflight_proof
            shop_claim = claims_by_id.get(proof.shop_call_claim_id)
            product_claim = claims_by_id.get(proof.product_call_claim_id)
            if (
                proof.aggregate_id != aggregate.aggregate_id
                or proof.attempt_id != attempt.attempt_id
                or proof.snapshot_id != snapshot.snapshot_id
                or proof.snapshot_fingerprint != snapshot.fingerprint
                or proof.printify_shop_id != snapshot.printify_shop_id
                or proof.printify_product_id != snapshot.printify_product_id
                or proof.verification_deadline != snapshot.verification_deadline
                or self.provider_authority is None
                or proof.provider_authority_id != self.provider_authority.provider_authority_id
                or proof.provider_authority_fingerprint != self.provider_authority.fingerprint
                or shop_claim is None
                or shop_claim.call_kind is not PublicationCallKind.SHOP_GET
                or shop_claim.fingerprint != proof.shop_call_claim_fingerprint
                or proof.shop_observed_at < shop_claim.authorized_at
                or product_claim is None
                or product_claim.call_kind is not PublicationCallKind.PRODUCT_GET
                or product_claim.purpose is not PublicationCallPurpose.PRODUCT_PREFLIGHT
                or product_claim.fingerprint != proof.product_call_claim_fingerprint
                or proof.product_observed_at < product_claim.authorized_at
                or shop_claim.authorization_id not in audited_claim_ids
                or product_claim.authorization_id not in audited_claim_ids
            ):
                raise ValueError("Preflight proof does not bind the exact pre-call claims")
        if self.provider_authority is not None:
            provider_authority = self.provider_authority
            expected_provider_job_version = self.phase6_record_version - (
                1 if aggregate.terminal_at is not None else 0
            )
            if (
                provider_authority.aggregate_id != aggregate.aggregate_id
                or provider_authority.attempt_id != attempt.attempt_id
                or provider_authority.snapshot_id != snapshot.snapshot_id
                or provider_authority.snapshot_fingerprint != snapshot.fingerprint
                or provider_authority.owner_id != snapshot.owner_id
                or provider_authority.job_id != snapshot.job_id
                or provider_authority.permit_id != permit.permit_id
                or provider_authority.work_request_id != work.work_request_id
                or provider_authority.phase6_record_version != expected_provider_job_version
                or provider_authority.approval_fingerprint != snapshot.approval_fingerprint
                or provider_authority.review_fingerprint != snapshot.review_fingerprint
                or provider_authority.product_sync_fingerprint != snapshot.product_sync_fingerprint
                or provider_authority.pricing_snapshot_fingerprint
                != snapshot.pricing_snapshot_fingerprint
                or provider_authority.pricing_evidence_fingerprint
                != snapshot.pricing_evidence_fingerprint
                or provider_authority.profile_fingerprint != snapshot.profile_fingerprint
                or provider_authority.release_manifest_fingerprint
                != snapshot.release_manifest_fingerprint
                or provider_authority.printify_shop_id != snapshot.printify_shop_id
                or provider_authority.printify_product_id != snapshot.printify_product_id
                or provider_authority.printify_image_id != snapshot.printify_image_id
                or provider_authority.product_payload_fingerprint
                != snapshot.product_payload_fingerprint
                or provider_authority.expected_sales_channel != snapshot.expected_sales_channel
                or provider_authority.pricing_fresh_until != snapshot.pricing_fresh_until
                or provider_authority.verification_deadline != snapshot.verification_deadline
            ):
                raise ValueError("Reconstructed provider authority differs from the snapshot")
            if self.call_claims and provider_authority.reconstructed_at > min(
                claim.authorized_at for claim in self.call_claims
            ):
                raise ValueError("Provider authority must be reconstructed before every call")
        if self.preflight_proof is not None and self.provider_authority is None:
            raise ValueError("Preflight proof requires re-read provider authority")
        if self.mutation_claim is not None:
            mutation = self.mutation_claim
            publish_claim = claims_by_id.get(mutation.call_claim_id)
            if (
                self.preflight_proof is None
                or mutation.aggregate_id != aggregate.aggregate_id
                or mutation.attempt_id != attempt.attempt_id
                or mutation.snapshot_id != snapshot.snapshot_id
                or mutation.snapshot_fingerprint != snapshot.fingerprint
                or mutation.permit_id != permit.permit_id
                or mutation.work_request_id != work.work_request_id
                or mutation.preflight_proof_id != self.preflight_proof.proof_id
                or mutation.preflight_proof_fingerprint != self.preflight_proof.fingerprint
                or mutation.consumed_permit_fingerprint != permit.fingerprint
                or mutation.verification_deadline != snapshot.verification_deadline
                or publish_claim is None
                or publish_claim.call_kind is not PublicationCallKind.PUBLISH_POST
                or publish_claim.fingerprint != mutation.call_claim_fingerprint
                or publish_claim.permit_fingerprint != permit.fingerprint
                or mutation.authorized_at != permit.consumed_at
                or publish_claim.authorized_at != mutation.authorized_at
            ):
                raise ValueError("Mutation claim does not bind consumed permit and exact preflight")
        if self.post_observation is not None:
            post = self.post_observation
            publish_claim = publish_claims[0] if len(publish_claims) == 1 else None
            recovery = (
                post.response_category.value
                == "consumed_claim_without_durable_boundary_observation"
            )
            if (
                self.mutation_claim is None
                or post.aggregate_id != aggregate.aggregate_id
                or post.attempt_id != attempt.attempt_id
                or post.mutation_claim_id != self.mutation_claim.mutation_claim_id
                or post.mutation_claim_fingerprint != self.mutation_claim.fingerprint
                or publish_claim is None
                or post.call_claim_id != publish_claim.authorization_id
                or post.call_claim_fingerprint != publish_claim.fingerprint
                or self.provider_authority is None
                or post.provider_authority_id != self.provider_authority.provider_authority_id
                or post.provider_authority_fingerprint != self.provider_authority.fingerprint
                or (not recovery and publish_claim.authorization_id not in audited_claim_ids)
            ):
                raise ValueError("POST observation does not bind the sole mutation claim")
        if self.last_product_observation != (
            self.product_observations[-1] if self.product_observations else None
        ):
            raise ValueError("Latest product observation must follow durable settlement order")
        observed_call_ids: set[str] = set()
        settlement_order: list[tuple[int, str]] = []
        for observation in self.product_observations:
            product_claim = claims_by_id.get(observation.call_claim_id)
            if (
                observation.aggregate_id != aggregate.aggregate_id
                or observation.attempt_id != attempt.attempt_id
                or observation.snapshot_id != snapshot.snapshot_id
                or observation.snapshot_fingerprint != snapshot.fingerprint
                or observation.verification_deadline != snapshot.verification_deadline
                or product_claim is None
                or product_claim.call_kind is not PublicationCallKind.PRODUCT_GET
                or product_claim.purpose
                not in {
                    PublicationCallPurpose.VERIFICATION,
                    PublicationCallPurpose.RECONCILIATION,
                }
                or product_claim.fingerprint != observation.call_claim_fingerprint
                or self.provider_authority is None
                or observation.provider_authority_id
                != self.provider_authority.provider_authority_id
                or observation.provider_authority_fingerprint != self.provider_authority.fingerprint
                or product_claim.authorization_id not in audited_claim_ids
            ):
                raise ValueError("Product observation does not bind one exact GET claim")
            if observation.call_claim_id in observed_call_ids:
                raise ValueError("One product GET claim can create only one observation")
            observed_call_ids.add(observation.call_claim_id)
            settlement_order.append(
                (
                    observation.resulting_aggregate_record_version,
                    observation.observation_id,
                )
            )
        if len(set(settlement_order)) != len(settlement_order) or settlement_order != sorted(
            settlement_order
        ):
            raise ValueError("Product observations must follow durable settlement order")
        if self.preflight_proof is not None:
            shop_claim = claims_by_id[self.preflight_proof.shop_call_claim_id]
            product_claim = claims_by_id[self.preflight_proof.product_call_claim_id]
            if self.preflight_proof.proven_at < max(
                shop_claim.authorized_at,
                product_claim.authorized_at,
            ):
                raise ValueError("Preflight proof cannot predate its GET claims")
        if self.mutation_claim is not None and self.preflight_proof is not None:
            if self.mutation_claim.authorized_at < self.preflight_proof.proven_at:
                raise ValueError("Mutation claim cannot predate complete preflight proof")
        if self.post_observation is not None and self.mutation_claim is not None:
            if self.post_observation.observed_at < self.mutation_claim.authorized_at:
                raise ValueError("POST observation cannot predate the mutation claim")

        if aggregate.state is PublicationState.PUBLICATION_VERIFYING and (
            self.post_observation is None
            or self.post_observation.outcome is not PublicationPostOutcome.DEFINITELY_ACCEPTED
        ):
            raise ValueError("Verification requires a definite accepted POST observation")
        if aggregate.state is PublicationState.PUBLICATION_RECONCILING and (
            self.post_observation is None
            or self.post_observation.outcome is not PublicationPostOutcome.AMBIGUOUS
        ):
            raise ValueError("Reconciliation requires explicit provider-outcome uncertainty")
        if aggregate.state in {
            PublicationState.PUBLISHED,
            PublicationState.PUBLICATION_OUTCOME_UNKNOWN,
        } and (
            self.post_observation is None
            or self.post_observation.outcome
            not in {
                PublicationPostOutcome.DEFINITELY_ACCEPTED,
                PublicationPostOutcome.AMBIGUOUS,
            }
        ):
            raise ValueError("Post-call terminal state requires compatible POST evidence")
        latest_observation_fingerprint = (
            self.last_product_observation.fingerprint
            if self.last_product_observation is not None
            else (self.post_observation.fingerprint if self.post_observation is not None else None)
        )
        if aggregate.last_observation_fingerprint != latest_observation_fingerprint:
            raise ValueError("Aggregate must point to its latest sanitized observation")
        terminal = aggregate.terminal_at is not None
        if terminal != (attempt.status is PublicationAttemptStatus.TERMINAL):
            raise ValueError("Aggregate and root attempt must settle together")
        if terminal != (work.terminal_at is not None):
            raise ValueError("Aggregate and publication work must settle together")
        if terminal != (
            self.report is not None
            and self.tombstone is not None
            and self.terminal_job_link is not None
        ):
            raise ValueError("Terminal state requires report and tombstone in the same settlement")
        if aggregate.state is PublicationState.PUBLISHED:
            if self.result is None or self.notification is None:
                raise ValueError("Published state requires result and notification")
            observation = self.last_product_observation
            if (
                observation is None
                or observation.outcome is not PublicationReadOutcome.POSITIVE_PROOF
                or self.result.observation_id != observation.observation_id
                or self.result.observation_fingerprint != observation.fingerprint
                or self.result.numeric_listing_id != observation.numeric_listing_id
                or self.result.verified_product_fingerprint
                != observation.verified_product_fingerprint
                or self.result.verified_at != observation.observed_at
                or self.result.aggregate_id != aggregate.aggregate_id
                or self.notification.result_id != self.result.result_id
                or self.notification.result_fingerprint != self.result.fingerprint
                or self.notification.aggregate_id != aggregate.aggregate_id
                or self.notification.created_at != aggregate.terminal_at
                or aggregate.terminal_at is None
                or aggregate.terminal_at < observation.observed_at
            ):
                raise ValueError("Published graph does not match its positive observation")
        elif self.result is not None or self.notification is not None:
            raise ValueError("Only published state may expose result or notification")
        expected_work_status = {
            PublicationState.PUBLICATION_REQUESTED: {
                PublicationExecutionWorkStatus.PENDING,
                PublicationExecutionWorkStatus.DISPATCHED,
            },
            PublicationState.PUBLICATION_VERIFYING: {PublicationExecutionWorkStatus.VERIFYING},
            PublicationState.PUBLICATION_RECONCILING: {PublicationExecutionWorkStatus.RECONCILING},
            PublicationState.PUBLISHED: {PublicationExecutionWorkStatus.SUCCEEDED},
            PublicationState.PUBLICATION_FAILED: {PublicationExecutionWorkStatus.FAILED},
            PublicationState.PUBLICATION_OUTCOME_UNKNOWN: {
                PublicationExecutionWorkStatus.OUTCOME_UNKNOWN
            },
        }[aggregate.state]
        if work.status not in expected_work_status:
            raise ValueError("Publication work status differs from aggregate state")
        if terminal:
            assert aggregate.terminal_at is not None
            assert self.report is not None and self.tombstone is not None
            assert self.terminal_job_link is not None
            if not (
                attempt.terminal_at == aggregate.terminal_at
                and work.terminal_at == aggregate.terminal_at
                and self.report.terminal_state is aggregate.state
                and self.report.requested_at == aggregate.requested_at
                and self.report.terminal_at == aggregate.terminal_at
                and self.report.source_release_eligible_at == aggregate.source_release_eligible_at
                and self.report.operational_expires_at == aggregate.operational_expires_at
                and self.report.shop_get_call_count == attempt.shop_get_call_count
                and self.report.product_get_call_count == attempt.product_get_call_count
                and self.report.publish_post_call_count == attempt.publish_post_call_count
                and self.report.release_manifest_fingerprint
                == snapshot.release_manifest_fingerprint
                and self.report.snapshot_fingerprint == snapshot.fingerprint
                and self.report.attempt_fingerprint == attempt.fingerprint
                and self.report.permit_fingerprint == permit.fingerprint
                and self.report.sanitized_audit_record_digests
                == tuple(binding.audit_record.fingerprint for binding in self.provider_audits)
                and self.tombstone.owner_id == snapshot.owner_id
                and self.tombstone.job_id == snapshot.job_id
                and self.tombstone.aggregate_id == aggregate.aggregate_id
                and self.tombstone.terminal_state is aggregate.state
                and self.tombstone.terminal_at == aggregate.terminal_at
                and self.tombstone.operational_expires_at == aggregate.operational_expires_at
                and self.tombstone.report_id == self.report.report_id
                and self.tombstone.report_fingerprint == self.report.fingerprint
            ):
                raise ValueError("Terminal report or tombstone differs from the settled graph")
            if not (
                self.terminal_job_link.owner_id == snapshot.owner_id
                and self.terminal_job_link.job_id == snapshot.job_id
                and self.terminal_job_link.aggregate_id == aggregate.aggregate_id
                and self.terminal_job_link.expected_record_version == self.phase6_record_version - 1
                and self.terminal_job_link.result_record_version == self.phase6_record_version
                and self.terminal_job_link.expected_event_sequence == self.phase6_event_sequence
                and self.terminal_job_link.result_event_sequence == self.phase6_event_sequence
                and self.terminal_job_link.terminal_state is aggregate.state
                and self.terminal_job_link.terminal_at == aggregate.terminal_at
                and self.terminal_job_link.report_id == self.report.report_id
                and self.terminal_job_link.report_fingerprint == self.report.fingerprint
                and self.terminal_job_link.result_id
                == (self.result.result_id if self.result is not None else None)
                and self.terminal_job_link.source_release_eligible_at
                == aggregate.source_release_eligible_at
                and self.terminal_job_link.operational_expires_at
                == aggregate.operational_expires_at
            ):
                raise ValueError("Terminal Phase 6 summary differs from publication settlement")
            if (
                aggregate.report_id != self.report.report_id
                or aggregate.tombstone_id != self.tombstone.tombstone_id
                or aggregate.result_id
                != (self.result.result_id if self.result is not None else None)
                or aggregate.notification_id
                != (self.notification.notification_id if self.notification is not None else None)
                or self.report.result_fingerprint
                != (self.result.fingerprint if self.result is not None else None)
            ):
                raise ValueError("Aggregate terminal pointers differ from immutable records")
            if (
                aggregate.last_observation_fingerprint != latest_observation_fingerprint
                or self.report.observation_fingerprint != latest_observation_fingerprint
            ):
                raise ValueError("Terminal observation pointers are inconsistent")
            owner_digest, job_digest = PublicationTerminalReport.identity_digests(
                snapshot.owner_id,
                snapshot.job_id,
            )
            if self.report.owner_digest != owner_digest or self.report.job_digest != job_digest:
                raise ValueError("Terminal report identity digests are invalid")
            if permit.status is PublicationPermitState.RETIRED:
                if attempt.publish_post_call_count != 0 or self.report.terminal_reason not in {
                    PublicationTerminalReason.DEFINITIVE_PREFLIGHT_FAILURE,
                    PublicationTerminalReason.PRE_CALL_DEADLINE_EXPIRED,
                }:
                    raise ValueError("Retired terminal settlement must prove zero POSTs")
            elif (
                aggregate.state is PublicationState.PUBLICATION_FAILED
                and self.report.terminal_reason
                is not PublicationTerminalReason.DEFINITIVE_SYNCHRONOUS_REJECTION
            ):
                raise ValueError("Consumed failed settlement requires definitive rejection")
            if (
                aggregate.state is PublicationState.PUBLICATION_FAILED
                and permit.status is PublicationPermitState.CONSUMED
                and (
                    self.post_observation is None
                    or self.post_observation.outcome
                    is not PublicationPostOutcome.DEFINITIVE_SYNCHRONOUS_REJECTION
                )
            ):
                raise ValueError("Consumed failure requires definitive synchronous rejection")
            if (
                aggregate.state is PublicationState.PUBLICATION_OUTCOME_UNKNOWN
                and aggregate.terminal_at < aggregate.verification_deadline
            ):
                raise ValueError("Unknown outcome cannot settle before the fixed deadline")
        return self
