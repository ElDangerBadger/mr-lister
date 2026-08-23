"""Frozen, capability-free Phase 7 publication contract.

This module defines authority and acceptance shape only. It deliberately has no HTTP client,
secret lookup, store mutation, handler, worker, or feature flag that can publish a product.
"""

from __future__ import annotations

import json
from enum import StrEnum
from hashlib import sha256
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    StringConstraints,
    model_validator,
)

PHASE7_PUBLICATION_CONTRACT_VERSION = "7.0.1"

type ContractName = Annotated[
    str, StringConstraints(min_length=1, max_length=96, pattern=r"^[a-z][A-Za-z0-9_]*$")
]
type RouteTemplate = Annotated[
    str, StringConstraints(min_length=1, max_length=160, pattern=r"^/[-A-Za-z0-9_{}./]+$")
]


class PublicationState(StrEnum):
    APPROVED = "approved"
    PUBLICATION_REQUESTED = "publication_requested"
    PUBLICATION_VERIFYING = "publication_verifying"
    PUBLICATION_RECONCILING = "publication_reconciling"
    PUBLISHED = "published"
    PUBLICATION_FAILED = "publication_failed"
    PUBLICATION_OUTCOME_UNKNOWN = "publication_outcome_unknown"


class PublicationPermitState(StrEnum):
    AVAILABLE = "available"
    CONSUMED = "consumed"
    RETIRED = "retired"


class PublicationActivationPhaseName(StrEnum):
    OFFLINE_IMPLEMENTATION = "offline_implementation"
    DEPLOYED_READ_ONLY_VALIDATION = "deployed_read_only_validation"
    EXPLICIT_ONE_LISTING_CANARY = "explicit_one_listing_canary"
    GENERAL_AVAILABILITY = "general_availability"


class PublicationTransition(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    source: PublicationState
    target: PublicationState
    authority: ContractName
    provider_mutation_count: StrictInt = Field(ge=0, le=1)


class PublicationPermitTransition(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    source: PublicationPermitState
    target: PublicationPermitState
    authority: ContractName
    maximum_publish_posts_authorized: StrictInt = Field(ge=0, le=1)


class PublicationProviderCall(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    method: Literal["GET", "POST"]
    route: RouteTemplate
    purpose: ContractName
    mutating: StrictBool
    maximum_calls_per_root_attempt: StrictInt = Field(ge=1, le=100)
    bounded_by_fixed_deadline: Literal[True] = True


class PublicationActivationPhase(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    name: PublicationActivationPhaseName
    required_gates: tuple[ContractName, ...] = Field(min_length=1, max_length=8)
    bounded_provider_mutation_allowed: StrictBool
    seller_publication_route_allowed: StrictBool
    requires_new_enabled_contract: StrictBool


class Phase7PublicationContract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    contract_version: Literal["7.0.1"] = PHASE7_PUBLICATION_CONTRACT_VERSION
    phase: Literal["7"] = "7"
    status: Literal["frozen"] = "frozen"
    publication_enabled: Literal[False] = False
    aggregate_boundary: Literal["separate_publication_aggregate"]
    phase6_control_state_during_publication: Literal["approved"]
    bridge_source_state: Literal[PublicationState.APPROVED]
    current_activation_phase: Literal[PublicationActivationPhaseName.OFFLINE_IMPLEMENTATION]
    command_route: Literal["/v1/jobs/{job_id}/publish"]
    confirmation_literal: Literal["publish_exact_approved_listing"]
    verification_deadline_seconds: Literal[1800]
    verification_deadline_anchor: Literal["root_attempt_requested_at"]
    verification_deadline_extension_allowed: Literal[False]
    provider_call_budget_scope: Literal["root_publication_attempt"]
    maximum_root_attempts_per_job: Literal[1]
    maximum_publish_posts_per_job: Literal[1]
    notification_channel: Literal["authenticated_in_application"]
    retention_anchor: Literal["publication_terminal_at"]
    source_release_after_terminal_days: Literal[30]
    operational_ttl_after_terminal_days: Literal[90]
    terminal_settlement_fields: tuple[ContractName, ...] = Field(min_length=3, max_length=3)
    duplicate_prevention_retention_invariant: Literal[
        "job_aggregate_tombstone_until_operational_expiry"
    ]
    states: tuple[PublicationState, ...] = Field(min_length=7, max_length=7)
    persisted_aggregate_states: tuple[PublicationState, ...] = Field(min_length=6, max_length=6)
    terminal_states: tuple[PublicationState, ...] = Field(min_length=3, max_length=3)
    permit_states: tuple[PublicationPermitState, ...] = Field(min_length=3, max_length=3)
    permit_transitions: tuple[PublicationPermitTransition, ...] = Field(min_length=2, max_length=2)
    transitions: tuple[PublicationTransition, ...] = Field(min_length=9, max_length=9)
    snapshot_fields: tuple[ContractName, ...] = Field(min_length=20, max_length=32)
    phase71_authority_prerequisites: tuple[ContractName, ...] = Field(min_length=2, max_length=2)
    provider_calls: tuple[PublicationProviderCall, ...] = Field(min_length=3, max_length=3)
    read_observation_outcomes: tuple[ContractName, ...] = Field(min_length=3, max_length=3)
    publication_body_fields: tuple[ContractName, ...] = Field(min_length=7, max_length=7)
    forbidden_provider_operations: tuple[ContractName, ...] = Field(min_length=8, max_length=16)
    positive_verification_fields: tuple[ContractName, ...] = Field(min_length=6, max_length=12)
    activation_gates: tuple[ContractName, ...] = Field(min_length=7, max_length=12)
    activation_phases: tuple[PublicationActivationPhase, ...] = Field(min_length=4, max_length=4)

    @model_validator(mode="after")
    def matches_frozen_semantics(self) -> Phase7PublicationContract:
        expected = (
            _STATES,
            _PERSISTED_AGGREGATE_STATES,
            _TERMINAL_STATES,
            _PERMIT_STATES,
            _PERMIT_TRANSITIONS,
            _TRANSITIONS,
            _SNAPSHOT_FIELDS,
            _PHASE71_AUTHORITY_PREREQUISITES,
            _PROVIDER_CALLS,
            _READ_OBSERVATION_OUTCOMES,
            _TERMINAL_SETTLEMENT_FIELDS,
            _PUBLICATION_BODY_FIELDS,
            _FORBIDDEN_PROVIDER_OPERATIONS,
            _POSITIVE_VERIFICATION_FIELDS,
            _ACTIVATION_GATES,
            _ACTIVATION_PHASES,
        )
        actual = (
            self.states,
            self.persisted_aggregate_states,
            self.terminal_states,
            self.permit_states,
            self.permit_transitions,
            self.transitions,
            self.snapshot_fields,
            self.phase71_authority_prerequisites,
            self.provider_calls,
            self.read_observation_outcomes,
            self.terminal_settlement_fields,
            self.publication_body_fields,
            self.forbidden_provider_operations,
            self.positive_verification_fields,
            self.activation_gates,
            self.activation_phases,
        )
        if actual != expected:
            raise ValueError("Phase 7 publication semantics differ from frozen authority")
        phase_gates = tuple(
            gate
            for activation_phase in self.activation_phases
            for gate in activation_phase.required_gates
        )
        if phase_gates != self.activation_gates or len(set(phase_gates)) != len(phase_gates):
            raise ValueError("Phase 7 activation phases must partition the frozen gates exactly")
        return self


_STATES = tuple(PublicationState)
_PERSISTED_AGGREGATE_STATES = tuple(
    state for state in PublicationState if state is not PublicationState.APPROVED
)
_TERMINAL_STATES = (
    PublicationState.PUBLISHED,
    PublicationState.PUBLICATION_FAILED,
    PublicationState.PUBLICATION_OUTCOME_UNKNOWN,
)
_PERMIT_STATES = tuple(PublicationPermitState)
_PERMIT_TRANSITIONS = (
    PublicationPermitTransition(
        source=PublicationPermitState.AVAILABLE,
        target=PublicationPermitState.CONSUMED,
        authority="atomic_pre_call_publish_authorization",
        maximum_publish_posts_authorized=1,
    ),
    PublicationPermitTransition(
        source=PublicationPermitState.AVAILABLE,
        target=PublicationPermitState.RETIRED,
        authority="definitive_pre_call_terminal_settlement",
        maximum_publish_posts_authorized=0,
    ),
)
_TRANSITIONS = (
    PublicationTransition(
        source=PublicationState.APPROVED,
        target=PublicationState.PUBLICATION_REQUESTED,
        authority="exact_publish_command_transaction",
        provider_mutation_count=0,
    ),
    PublicationTransition(
        source=PublicationState.PUBLICATION_REQUESTED,
        target=PublicationState.PUBLICATION_VERIFYING,
        authority="consumed_permit_and_definite_acceptance",
        provider_mutation_count=1,
    ),
    PublicationTransition(
        source=PublicationState.PUBLICATION_REQUESTED,
        target=PublicationState.PUBLICATION_RECONCILING,
        authority="consumed_permit_and_ambiguous_outcome",
        provider_mutation_count=1,
    ),
    PublicationTransition(
        source=PublicationState.PUBLICATION_REQUESTED,
        target=PublicationState.PUBLICATION_FAILED,
        authority="definitive_pre_call_terminal_settlement",
        provider_mutation_count=0,
    ),
    PublicationTransition(
        source=PublicationState.PUBLICATION_REQUESTED,
        target=PublicationState.PUBLICATION_FAILED,
        authority="consumed_permit_and_definitive_synchronous_rejection",
        provider_mutation_count=1,
    ),
    PublicationTransition(
        source=PublicationState.PUBLICATION_VERIFYING,
        target=PublicationState.PUBLISHED,
        authority="positive_exact_product_observation",
        provider_mutation_count=0,
    ),
    PublicationTransition(
        source=PublicationState.PUBLICATION_VERIFYING,
        target=PublicationState.PUBLICATION_OUTCOME_UNKNOWN,
        authority="fixed_deadline_without_positive_proof",
        provider_mutation_count=0,
    ),
    PublicationTransition(
        source=PublicationState.PUBLICATION_RECONCILING,
        target=PublicationState.PUBLISHED,
        authority="positive_exact_product_observation",
        provider_mutation_count=0,
    ),
    PublicationTransition(
        source=PublicationState.PUBLICATION_RECONCILING,
        target=PublicationState.PUBLICATION_OUTCOME_UNKNOWN,
        authority="fixed_deadline_without_definitive_proof",
        provider_mutation_count=0,
    ),
)
_SNAPSHOT_FIELDS = (
    "owner_id",
    "job_id",
    "expected_record_version",
    "approval_decision_id",
    "approval_fingerprint",
    "review_version",
    "review_fingerprint",
    "product_sync_id",
    "product_sync_fingerprint",
    "printify_shop_id",
    "printify_product_id",
    "printify_image_id",
    "product_payload_fingerprint",
    "pricing_snapshot_id",
    "pricing_snapshot_fingerprint",
    "pricing_evidence_fingerprint",
    "pricing_fresh_until",
    "profile_id",
    "profile_version",
    "profile_fingerprint",
    "expected_sales_channel",
    "publication_body_fingerprint",
    "release_manifest_fingerprint",
    "requested_at",
    "verification_deadline",
)
_PHASE71_AUTHORITY_PREREQUISITES = (
    "approval_decision_id",
    "printify_shop_id",
)
_PROVIDER_CALLS = (
    PublicationProviderCall(
        method="GET",
        route="/v1/shops.json",
        purpose="etsy_shop_preflight",
        mutating=False,
        maximum_calls_per_root_attempt=3,
    ),
    PublicationProviderCall(
        method="GET",
        route="/v1/shops/{shop_id}/products/{product_id}.json",
        purpose="product_preflight_and_positive_verification",
        mutating=False,
        maximum_calls_per_root_attempt=100,
    ),
    PublicationProviderCall(
        method="POST",
        route="/v1/shops/{shop_id}/products/{product_id}/publish.json",
        purpose="one_shot_connected_channel_publication",
        mutating=True,
        maximum_calls_per_root_attempt=1,
    ),
)
_READ_OBSERVATION_OUTCOMES = (
    "positive_publication_proof",
    "publication_not_yet_proven",
    "conflicting_or_incomplete_evidence",
)
_TERMINAL_SETTLEMENT_FIELDS = (
    "terminal_at",
    "source_release_eligible_at",
    "operational_expires_at",
)
_PUBLICATION_BODY_FIELDS = (
    "title",
    "description",
    "images",
    "variants",
    "tags",
    "keyFeatures",
    "shipping_template",
)
_FORBIDDEN_PROVIDER_OPERATIONS = (
    "product_create",
    "product_update",
    "product_delete",
    "image_upload",
    "image_archive",
    "publishing_succeeded",
    "publishing_failed",
    "unpublish",
    "order",
    "fulfillment",
    "webhook",
)
_POSITIVE_VERIFICATION_FIELDS = (
    "exact_shop",
    "exact_product",
    "unlocked",
    "visible",
    "canonical_content_match",
    "single_etsy_external_id",
    "numeric_listing_id",
    "safe_etsy_link",
)
_ACTIVATION_GATES = (
    "phase71_authority_prerequisites",
    "publication_domain_store_service_matrix",
    "publication_provider_one_shot_matrix",
    "publication_api_browser_matrix",
    "publication_infrastructure_and_alarm_matrix",
    "phase6_deployed_non_destructive_acceptance",
    "immutable_release_and_agentcore_binding",
    "linux_arm64_artifact_inspection",
    "read_only_etsy_preflight",
    "explicit_one_listing_live_canary",
    "explicit_general_availability_enablement",
)
_ACTIVATION_PHASES = (
    PublicationActivationPhase(
        name=PublicationActivationPhaseName.OFFLINE_IMPLEMENTATION,
        required_gates=(
            "phase71_authority_prerequisites",
            "publication_domain_store_service_matrix",
            "publication_provider_one_shot_matrix",
            "publication_api_browser_matrix",
            "publication_infrastructure_and_alarm_matrix",
        ),
        bounded_provider_mutation_allowed=False,
        seller_publication_route_allowed=False,
        requires_new_enabled_contract=False,
    ),
    PublicationActivationPhase(
        name=PublicationActivationPhaseName.DEPLOYED_READ_ONLY_VALIDATION,
        required_gates=(
            "phase6_deployed_non_destructive_acceptance",
            "immutable_release_and_agentcore_binding",
            "linux_arm64_artifact_inspection",
            "read_only_etsy_preflight",
        ),
        bounded_provider_mutation_allowed=False,
        seller_publication_route_allowed=False,
        requires_new_enabled_contract=False,
    ),
    PublicationActivationPhase(
        name=PublicationActivationPhaseName.EXPLICIT_ONE_LISTING_CANARY,
        required_gates=("explicit_one_listing_live_canary",),
        bounded_provider_mutation_allowed=True,
        seller_publication_route_allowed=False,
        requires_new_enabled_contract=False,
    ),
    PublicationActivationPhase(
        name=PublicationActivationPhaseName.GENERAL_AVAILABILITY,
        required_gates=("explicit_general_availability_enablement",),
        bounded_provider_mutation_allowed=True,
        seller_publication_route_allowed=True,
        requires_new_enabled_contract=True,
    ),
)


def phase7_publication_contract() -> Phase7PublicationContract:
    """Return the frozen publication-disabled Phase 7 contract."""

    return Phase7PublicationContract(
        aggregate_boundary="separate_publication_aggregate",
        phase6_control_state_during_publication="approved",
        bridge_source_state=PublicationState.APPROVED,
        current_activation_phase=PublicationActivationPhaseName.OFFLINE_IMPLEMENTATION,
        command_route="/v1/jobs/{job_id}/publish",
        confirmation_literal="publish_exact_approved_listing",
        verification_deadline_seconds=1800,
        verification_deadline_anchor="root_attempt_requested_at",
        verification_deadline_extension_allowed=False,
        provider_call_budget_scope="root_publication_attempt",
        maximum_root_attempts_per_job=1,
        maximum_publish_posts_per_job=1,
        notification_channel="authenticated_in_application",
        retention_anchor="publication_terminal_at",
        source_release_after_terminal_days=30,
        operational_ttl_after_terminal_days=90,
        terminal_settlement_fields=_TERMINAL_SETTLEMENT_FIELDS,
        duplicate_prevention_retention_invariant=(
            "job_aggregate_tombstone_until_operational_expiry"
        ),
        states=_STATES,
        persisted_aggregate_states=_PERSISTED_AGGREGATE_STATES,
        terminal_states=_TERMINAL_STATES,
        permit_states=_PERMIT_STATES,
        permit_transitions=_PERMIT_TRANSITIONS,
        transitions=_TRANSITIONS,
        snapshot_fields=_SNAPSHOT_FIELDS,
        phase71_authority_prerequisites=_PHASE71_AUTHORITY_PREREQUISITES,
        provider_calls=_PROVIDER_CALLS,
        read_observation_outcomes=_READ_OBSERVATION_OUTCOMES,
        publication_body_fields=_PUBLICATION_BODY_FIELDS,
        forbidden_provider_operations=_FORBIDDEN_PROVIDER_OPERATIONS,
        positive_verification_fields=_POSITIVE_VERIFICATION_FIELDS,
        activation_gates=_ACTIVATION_GATES,
        activation_phases=_ACTIVATION_PHASES,
    )


def phase7_publication_contract_bytes() -> bytes:
    """Return deterministic checked-artifact bytes."""

    payload = phase7_publication_contract().model_dump(mode="json")
    return (
        json.dumps(payload, sort_keys=True, indent=2, ensure_ascii=True, allow_nan=False) + "\n"
    ).encode()


def phase7_publication_contract_digest() -> str:
    return sha256(phase7_publication_contract_bytes()).hexdigest()


def validate_phase7_publication_contract_json(payload: bytes | str) -> Phase7PublicationContract:
    """Strictly validate a serialized contract and require exact frozen semantic equality."""

    if isinstance(payload, str):
        payload = payload.encode()
    contract = Phase7PublicationContract.model_validate_json(payload, strict=True)
    if contract != phase7_publication_contract():
        raise ValueError("Phase 7 publication contract differs from frozen authority")
    return contract
