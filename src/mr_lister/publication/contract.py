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

PHASE7_PUBLICATION_CONTRACT_VERSION = "7.0.0"

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


class PublicationTransition(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    source: PublicationState
    target: PublicationState
    authority: ContractName
    provider_mutation_count: StrictInt = Field(ge=0, le=1)


class PublicationProviderCall(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    method: Literal["GET", "POST"]
    route: RouteTemplate
    purpose: ContractName
    mutating: StrictBool
    maximum_calls: StrictInt = Field(ge=1, le=100)


class Phase7PublicationContract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    contract_version: Literal["7.0.0"] = PHASE7_PUBLICATION_CONTRACT_VERSION
    phase: Literal["7"] = "7"
    status: Literal["frozen"] = "frozen"
    publication_enabled: Literal[False] = False
    command_route: Literal["/v1/jobs/{job_id}/publish"]
    confirmation_literal: Literal["publish_exact_approved_listing"]
    verification_deadline_seconds: Literal[1800]
    maximum_publish_posts_per_job: Literal[1]
    notification_channel: Literal["authenticated_in_application"]
    source_release_after_terminal_days: Literal[30]
    operational_ttl_after_terminal_days: Literal[90]
    states: tuple[PublicationState, ...] = Field(min_length=7, max_length=7)
    terminal_states: tuple[PublicationState, ...] = Field(min_length=3, max_length=3)
    permit_states: tuple[PublicationPermitState, ...] = Field(min_length=3, max_length=3)
    transitions: tuple[PublicationTransition, ...] = Field(min_length=10, max_length=10)
    snapshot_fields: tuple[ContractName, ...] = Field(min_length=20, max_length=32)
    provider_calls: tuple[PublicationProviderCall, ...] = Field(min_length=3, max_length=3)
    publication_body_fields: tuple[ContractName, ...] = Field(min_length=7, max_length=7)
    forbidden_provider_operations: tuple[ContractName, ...] = Field(min_length=8, max_length=16)
    positive_verification_fields: tuple[ContractName, ...] = Field(min_length=6, max_length=12)
    activation_gates: tuple[ContractName, ...] = Field(min_length=7, max_length=12)

    @model_validator(mode="after")
    def matches_frozen_semantics(self) -> Phase7PublicationContract:
        expected = (
            _STATES,
            _TERMINAL_STATES,
            _PERMIT_STATES,
            _TRANSITIONS,
            _SNAPSHOT_FIELDS,
            _PROVIDER_CALLS,
            _PUBLICATION_BODY_FIELDS,
            _FORBIDDEN_PROVIDER_OPERATIONS,
            _POSITIVE_VERIFICATION_FIELDS,
            _ACTIVATION_GATES,
        )
        actual = (
            self.states,
            self.terminal_states,
            self.permit_states,
            self.transitions,
            self.snapshot_fields,
            self.provider_calls,
            self.publication_body_fields,
            self.forbidden_provider_operations,
            self.positive_verification_fields,
            self.activation_gates,
        )
        if actual != expected:
            raise ValueError("Phase 7 publication semantics differ from frozen authority")
        return self


_STATES = tuple(PublicationState)
_TERMINAL_STATES = (
    PublicationState.PUBLISHED,
    PublicationState.PUBLICATION_FAILED,
    PublicationState.PUBLICATION_OUTCOME_UNKNOWN,
)
_PERMIT_STATES = tuple(PublicationPermitState)
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
        authority="definitive_preflight_failure",
        provider_mutation_count=0,
    ),
    PublicationTransition(
        source=PublicationState.PUBLICATION_VERIFYING,
        target=PublicationState.PUBLISHED,
        authority="positive_exact_product_observation",
        provider_mutation_count=0,
    ),
    PublicationTransition(
        source=PublicationState.PUBLICATION_VERIFYING,
        target=PublicationState.PUBLICATION_FAILED,
        authority="definitive_provider_failure_observation",
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
        target=PublicationState.PUBLICATION_FAILED,
        authority="definitive_provider_failure_observation",
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
_PROVIDER_CALLS = (
    PublicationProviderCall(
        method="GET",
        route="/v1/shops.json",
        purpose="etsy_shop_preflight",
        mutating=False,
        maximum_calls=1,
    ),
    PublicationProviderCall(
        method="GET",
        route="/v1/shops/{shop_id}/products/{product_id}.json",
        purpose="product_preflight_and_verification",
        mutating=False,
        maximum_calls=100,
    ),
    PublicationProviderCall(
        method="POST",
        route="/v1/shops/{shop_id}/products/{product_id}/publish.json",
        purpose="one_shot_connected_channel_publication",
        mutating=True,
        maximum_calls=1,
    ),
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
    "phase6_deployed_non_destructive_acceptance",
    "immutable_release_and_agentcore_binding",
    "linux_arm64_artifact_inspection",
    "publication_domain_store_service_matrix",
    "publication_provider_one_shot_matrix",
    "publication_api_browser_matrix",
    "publication_infrastructure_and_alarm_matrix",
    "read_only_etsy_preflight",
    "explicit_one_listing_live_canary",
)


def phase7_publication_contract() -> Phase7PublicationContract:
    """Return the frozen publication-disabled Phase 7 contract."""

    return Phase7PublicationContract(
        command_route="/v1/jobs/{job_id}/publish",
        confirmation_literal="publish_exact_approved_listing",
        verification_deadline_seconds=1800,
        maximum_publish_posts_per_job=1,
        notification_channel="authenticated_in_application",
        source_release_after_terminal_days=30,
        operational_ttl_after_terminal_days=90,
        states=_STATES,
        terminal_states=_TERMINAL_STATES,
        permit_states=_PERMIT_STATES,
        transitions=_TRANSITIONS,
        snapshot_fields=_SNAPSHOT_FIELDS,
        provider_calls=_PROVIDER_CALLS,
        publication_body_fields=_PUBLICATION_BODY_FIELDS,
        forbidden_provider_operations=_FORBIDDEN_PROVIDER_OPERATIONS,
        positive_verification_fields=_POSITIVE_VERIFICATION_FIELDS,
        activation_gates=_ACTIVATION_GATES,
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
