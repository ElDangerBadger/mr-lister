"""Strict, immutable records for a pristine Phase 7 publication request."""

from __future__ import annotations

from datetime import datetime, timedelta
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    StrictInt,
    StringConstraints,
    model_validator,
)

from mr_lister.publication.contract import (
    PHASE7_PUBLICATION_CONTRACT_VERSION,
    PublicationPermitState,
    PublicationState,
)
from mr_lister.publication.fingerprints import (
    publication_aggregate_fingerprint,
    publication_attempt_fingerprint,
    publication_body_fingerprint,
    publication_event_fingerprint,
    publication_permit_fingerprint,
    publication_snapshot_fingerprint,
    publication_work_input_fingerprint,
)

PublicationContractVersion = Literal["7.0.1"]
SafeId = Annotated[
    str,
    StringConstraints(strip_whitespace=True, pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,127}$"),
]
OwnerId = Annotated[str, StringConstraints(pattern=r"^[a-f0-9]{64}$")]
Fingerprint = Annotated[str, StringConstraints(pattern=r"^[a-f0-9]{64}$")]


def _require_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError("Publication timestamps must be UTC-aware")
    return value


UtcDateTime = Annotated[datetime, AfterValidator(_require_utc)]


class PublicationModel(BaseModel):
    """Strict, immutable base with no provider, persistence, or Phase 6 dependency."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    contract_version: PublicationContractVersion = PHASE7_PUBLICATION_CONTRACT_VERSION


class PublicationWorkStatus(StrEnum):
    """The only work status authorized by the request-creation slice."""

    PENDING = "pending"


class PublicationEventName(StrEnum):
    PUBLICATION_REQUESTED = "PUBLICATION_REQUESTED"


class PublicationSnapshot(PublicationModel):
    """The exact immutable authority frozen before any publication work can run."""

    snapshot_id: SafeId
    fingerprint: Fingerprint
    owner_id: OwnerId
    job_id: SafeId
    expected_record_version: StrictInt = Field(ge=0)
    approval_decision_id: SafeId
    approval_fingerprint: Fingerprint
    review_version: StrictInt = Field(ge=1)
    review_fingerprint: Fingerprint
    product_sync_id: SafeId
    product_sync_fingerprint: Fingerprint
    printify_shop_id: StrictInt = Field(gt=0)
    printify_product_id: SafeId
    printify_image_id: SafeId
    product_payload_fingerprint: Fingerprint
    pricing_snapshot_id: SafeId
    pricing_snapshot_fingerprint: Fingerprint
    pricing_evidence_fingerprint: Fingerprint
    pricing_fresh_until: UtcDateTime
    profile_id: SafeId
    profile_version: StrictInt = Field(ge=1)
    profile_fingerprint: Fingerprint
    expected_sales_channel: Literal["etsy"] = "etsy"
    publication_body_fingerprint: Fingerprint
    release_manifest_fingerprint: Fingerprint
    requested_at: UtcDateTime
    verification_deadline: UtcDateTime

    @model_validator(mode="after")
    def authority_is_exact_and_current(self) -> PublicationSnapshot:
        if self.verification_deadline != self.requested_at + timedelta(seconds=1800):
            raise ValueError("Verification deadline must equal requested_at plus 1800 seconds")
        if self.pricing_fresh_until <= self.requested_at:
            raise ValueError("Pricing authority must be fresh when publication is requested")
        if self.publication_body_fingerprint != publication_body_fingerprint():
            raise ValueError("Publication body fingerprint differs from the frozen exact body")
        if self.fingerprint != publication_snapshot_fingerprint(self):
            raise ValueError("Publication snapshot fingerprint does not match its authority")
        return self


class PublicationAttempt(PublicationModel):
    """Pristine root attempt with fixed deadline and durable, unspent call budgets."""

    attempt_id: SafeId
    aggregate_id: SafeId
    owner_id: OwnerId
    job_id: SafeId
    snapshot_id: SafeId
    snapshot_fingerprint: Fingerprint
    root_attempt_number: Literal[1] = 1
    record_version: Literal[0] = 0
    shop_get_call_limit: Literal[3] = 3
    shop_get_call_count: Literal[0] = 0
    product_get_call_limit: Literal[100] = 100
    product_get_call_count: Literal[0] = 0
    publish_post_call_limit: Literal[1] = 1
    publish_post_call_count: Literal[0] = 0
    requested_at: UtcDateTime
    verification_deadline: UtcDateTime
    fingerprint: Fingerprint

    @model_validator(mode="after")
    def deadline_and_fingerprint_are_fixed(self) -> PublicationAttempt:
        if self.verification_deadline != self.requested_at + timedelta(seconds=1800):
            raise ValueError("Root attempt deadline must equal requested_at plus 1800 seconds")
        if self.fingerprint != publication_attempt_fingerprint(self):
            raise ValueError("Publication attempt fingerprint does not match its authority")
        return self


class PublicationPermit(PublicationModel):
    """Pristine one-shot permit; this module exposes no consumption operation."""

    permit_id: SafeId
    aggregate_id: SafeId
    attempt_id: SafeId
    snapshot_id: SafeId
    snapshot_fingerprint: Fingerprint
    owner_id: OwnerId
    job_id: SafeId
    work_request_id: SafeId
    status: Literal[PublicationPermitState.AVAILABLE] = PublicationPermitState.AVAILABLE
    maximum_publish_posts_authorized: Literal[1] = 1
    record_version: Literal[0] = 0
    created_at: UtcDateTime
    fingerprint: Fingerprint

    @model_validator(mode="after")
    def fingerprint_matches_available_authority(self) -> PublicationPermit:
        if self.fingerprint != publication_permit_fingerprint(self):
            raise ValueError("Publication permit fingerprint does not match its authority")
        return self


class PublicationWorkRequest(PublicationModel):
    """Pristine publication outbox record for a future dedicated dispatcher."""

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
    status: Literal[PublicationWorkStatus.PENDING] = PublicationWorkStatus.PENDING
    record_version: Literal[0] = 0
    attempt_count: Literal[0] = 0
    input_fingerprint: Fingerprint
    verification_deadline: UtcDateTime
    next_dispatch_at: UtcDateTime
    created_at: UtcDateTime
    updated_at: UtcDateTime

    @model_validator(mode="after")
    def pending_work_is_pristine(self) -> PublicationWorkRequest:
        if not (
            self.next_dispatch_at == self.created_at
            and self.updated_at == self.created_at
            and self.created_at < self.verification_deadline
        ):
            raise ValueError("New publication work must be pristine and within its deadline")
        if self.input_fingerprint != publication_work_input_fingerprint(self):
            raise ValueError("Publication work input fingerprint does not match its authority")
        return self


class PublicationAggregate(PublicationModel):
    """The separately owned publication aggregate at its first persisted state."""

    aggregate_id: SafeId
    owner_id: OwnerId
    job_id: SafeId
    state: Literal[PublicationState.PUBLICATION_REQUESTED] = PublicationState.PUBLICATION_REQUESTED
    record_version: Literal[0] = 0
    snapshot_id: SafeId
    snapshot_fingerprint: Fingerprint
    attempt_id: SafeId
    permit_id: SafeId
    work_request_id: SafeId
    receipt_id: SafeId
    requested_at: UtcDateTime
    updated_at: UtcDateTime
    terminal_at: None = None
    source_release_eligible_at: None = None
    operational_expires_at: None = None
    fingerprint: Fingerprint

    @model_validator(mode="after")
    def requested_aggregate_is_pristine(self) -> PublicationAggregate:
        if self.updated_at != self.requested_at:
            raise ValueError("New publication aggregate timestamps must be identical")
        if self.fingerprint != publication_aggregate_fingerprint(self):
            raise ValueError("Publication aggregate fingerprint does not match its authority")
        return self


class PublicationJobLink(PublicationModel):
    """Pure instruction for linking an approved Phase 6 row to one aggregate.

    This is not a ninth publication record. A persistence adapter applies it to the complete
    current control row and its owner projection in the same atomic transaction.
    """

    owner_id: OwnerId
    job_id: SafeId
    phase6_state: Literal["approved"] = "approved"
    expected_record_version: StrictInt = Field(ge=0)
    result_record_version: StrictInt = Field(ge=1)
    expected_event_sequence: StrictInt = Field(ge=0)
    result_event_sequence: StrictInt = Field(ge=0)
    publication_aggregate_id: SafeId
    linked_at: UtcDateTime

    @model_validator(mode="after")
    def phase6_link_changes_only_record_version(self) -> PublicationJobLink:
        if self.result_record_version != self.expected_record_version + 1:
            raise ValueError("Publication link must increment Phase 6 record_version exactly once")
        if self.result_event_sequence != self.expected_event_sequence:
            raise ValueError("Separate publication events cannot change Phase 6 event_sequence")
        return self


class PublicationDomainEvent(PublicationModel):
    """First event in the publication aggregate's own event stream."""

    aggregate_id: SafeId
    owner_id: OwnerId
    job_id: SafeId
    sequence: Literal[1] = 1
    name: Literal[PublicationEventName.PUBLICATION_REQUESTED] = (
        PublicationEventName.PUBLICATION_REQUESTED
    )
    state: Literal[PublicationState.PUBLICATION_REQUESTED] = PublicationState.PUBLICATION_REQUESTED
    snapshot_id: SafeId
    attempt_id: SafeId
    permit_id: SafeId
    work_request_id: SafeId
    occurred_at: UtcDateTime
    fingerprint: Fingerprint

    @model_validator(mode="after")
    def event_payload_is_content_bound(self) -> PublicationDomainEvent:
        if self.fingerprint != publication_event_fingerprint(self):
            raise ValueError("Publication event fingerprint does not match its payload")
        return self
