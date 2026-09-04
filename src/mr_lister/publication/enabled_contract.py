"""Frozen Phase 7.18 general-availability activation contract.

Contract 7.1.0 is a deliberately small activation successor to the capability-free 7.0.1
authority.  It does not redefine the one-shot publication domain.  Instead, it binds the exact
7.0.1 bytes and enables only the already-defined owner-scoped query, request, and worker surfaces.
"""

from __future__ import annotations

import json
from hashlib import sha256
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from mr_lister.publication.contract import (
    PHASE7_PUBLICATION_CONTRACT_VERSION,
    PublicationActivationPhaseName,
    phase7_publication_contract_digest,
)

PHASE718_ENABLED_CONTRACT_VERSION = "7.1.0"
PHASE718_PREDECESSOR_CONTRACT_FINGERPRINT = (
    "548b710230618e73c20a509f2121799c415b50070e1e2ae7e1b82fe3c37e2981"
)

type ContractGate = Annotated[
    str, StringConstraints(min_length=1, max_length=96, pattern=r"^[a-z][a-z0-9_]*$")
]


class Phase718SellerRoute(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    method: Literal["GET", "POST"]
    route: Literal["/v1/jobs/{job_id}/publication", "/v1/jobs/{job_id}/publish"]
    owner_scoped: Literal[True] = True
    authenticated: Literal[True] = True


class Phase718EnabledPublicationContract(BaseModel):
    """Exact authority added after the one-listing canary succeeds."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    contract_version: Literal["7.1.0"] = PHASE718_ENABLED_CONTRACT_VERSION
    phase: Literal["7"] = "7"
    status: Literal["frozen"] = "frozen"
    predecessor_contract_version: Literal["7.0.1"] = PHASE7_PUBLICATION_CONTRACT_VERSION
    predecessor_contract_fingerprint: Literal[
        "548b710230618e73c20a509f2121799c415b50070e1e2ae7e1b82fe3c37e2981"
    ] = PHASE718_PREDECESSOR_CONTRACT_FINGERPRINT
    current_activation_phase: Literal[PublicationActivationPhaseName.GENERAL_AVAILABILITY] = (
        PublicationActivationPhaseName.GENERAL_AVAILABILITY
    )
    publication_enabled: Literal[True] = True
    query_enabled: Literal[True] = True
    request_enabled: Literal[True] = True
    worker_enabled: Literal[True] = True
    dispatcher_enabled: Literal[True] = True
    recovery_enabled: Literal[True] = True
    retention_enabled: Literal[True] = True
    scaffold_only: Literal[False] = False
    phase6_runtime_unchanged: Literal[True] = True
    seller_routes: tuple[Phase718SellerRoute, ...] = Field(min_length=2, max_length=2)
    confirmation_literal: Literal["publish_exact_approved_listing"] = (
        "publish_exact_approved_listing"
    )
    publication_scope: Literal["one_shot_per_approved_job"] = "one_shot_per_approved_job"
    maximum_root_attempts_per_job: Literal[1] = 1
    maximum_publish_posts_per_job: Literal[1] = 1
    activation_evidence: tuple[ContractGate, ...] = Field(min_length=2, max_length=2)
    forbidden_capabilities_preserved: tuple[ContractGate, ...] = Field(
        min_length=5,
        max_length=5,
    )

    @model_validator(mode="after")
    def matches_reviewed_activation(self) -> Phase718EnabledPublicationContract:
        if self.seller_routes != _SELLER_ROUTES:
            raise ValueError("Phase 7.18 seller routes differ from reviewed authority")
        if self.activation_evidence != _ACTIVATION_EVIDENCE:
            raise ValueError("Phase 7.18 activation evidence differs from reviewed authority")
        if self.forbidden_capabilities_preserved != _FORBIDDEN_CAPABILITIES:
            raise ValueError("Phase 7.18 exclusions differ from reviewed authority")
        if phase7_publication_contract_digest() != self.predecessor_contract_fingerprint:
            raise ValueError("Phase 7.18 predecessor contract binding is stale")
        return self


_SELLER_ROUTES = (
    Phase718SellerRoute(method="GET", route="/v1/jobs/{job_id}/publication"),
    Phase718SellerRoute(method="POST", route="/v1/jobs/{job_id}/publish"),
)
_ACTIVATION_EVIDENCE = (
    "explicit_one_listing_live_canary",
    "explicit_general_availability_enablement",
)
_FORBIDDEN_CAPABILITIES = (
    "unpublish",
    "order",
    "fulfillment",
    "delete_product",
    "custom_channel_status",
)


def phase718_enabled_publication_contract() -> Phase718EnabledPublicationContract:
    return Phase718EnabledPublicationContract(
        seller_routes=_SELLER_ROUTES,
        activation_evidence=_ACTIVATION_EVIDENCE,
        forbidden_capabilities_preserved=_FORBIDDEN_CAPABILITIES,
    )


def phase718_enabled_publication_contract_bytes() -> bytes:
    payload = phase718_enabled_publication_contract().model_dump(mode="json")
    return (
        json.dumps(payload, sort_keys=True, indent=2, ensure_ascii=True, allow_nan=False) + "\n"
    ).encode()


def phase718_enabled_publication_contract_digest() -> str:
    return sha256(phase718_enabled_publication_contract_bytes()).hexdigest()


def validate_phase718_enabled_publication_contract_json(
    payload: bytes | str,
) -> Phase718EnabledPublicationContract:
    contract = Phase718EnabledPublicationContract.model_validate_json(payload, strict=True)
    if contract != phase718_enabled_publication_contract():
        raise ValueError("Phase 7.18 enabled contract differs from frozen authority")
    return contract


__all__ = [
    "PHASE718_ENABLED_CONTRACT_VERSION",
    "PHASE718_PREDECESSOR_CONTRACT_FINGERPRINT",
    "Phase718EnabledPublicationContract",
    "Phase718SellerRoute",
    "phase718_enabled_publication_contract",
    "phase718_enabled_publication_contract_bytes",
    "phase718_enabled_publication_contract_digest",
    "validate_phase718_enabled_publication_contract_json",
]
