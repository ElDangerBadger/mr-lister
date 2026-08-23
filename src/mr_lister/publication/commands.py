"""Pure command and atomic-request DTOs for Phase 7 publication."""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal

from pydantic import Field, StringConstraints, model_validator

from mr_lister.publication.contract import PublicationState
from mr_lister.publication.fingerprints import publication_command_receipt_fingerprint
from mr_lister.publication.models import (
    Fingerprint,
    OwnerId,
    PublicationAggregate,
    PublicationAttempt,
    PublicationDomainEvent,
    PublicationJobLink,
    PublicationModel,
    PublicationPermit,
    PublicationSnapshot,
    PublicationWorkRequest,
    SafeId,
    UtcDateTime,
)

IdempotencyKey = Annotated[str, StringConstraints(min_length=1, max_length=256)]


class PublicationCommandType(StrEnum):
    REQUEST_PUBLICATION = "request_publication"


class RequestPublicationCommand(PublicationModel):
    """Seller authority only; provider identities and work choices are never caller supplied."""

    owner_id: OwnerId
    job_id: SafeId
    expected_record_version: int = Field(ge=0)
    expected_review_version: int = Field(ge=1)
    expected_review_fingerprint: Fingerprint
    expected_review_etag: Fingerprint
    expected_approval_decision_id: SafeId
    expected_approval_fingerprint: Fingerprint
    confirmation: Literal["publish_exact_approved_listing"]
    idempotency_key: IdempotencyKey = Field(repr=False)


class PublicationRequestResponse(PublicationModel):
    job_id: SafeId
    publication_aggregate_id: SafeId
    publication_state: Literal[PublicationState.PUBLICATION_REQUESTED] = (
        PublicationState.PUBLICATION_REQUESTED
    )
    record_version: int = Field(ge=1)
    review_version: int = Field(ge=1)
    work_request_id: SafeId
    requested_at: UtcDateTime
    verification_deadline: UtcDateTime


class PublicationCommandReceipt(PublicationModel):
    """Stable idempotency result bound to the complete semantic command payload."""

    receipt_id: SafeId
    owner_id: OwnerId
    job_id: SafeId
    aggregate_id: SafeId
    snapshot_id: SafeId
    attempt_id: SafeId
    permit_id: SafeId
    work_request_id: SafeId
    command_type: Literal[PublicationCommandType.REQUEST_PUBLICATION] = (
        PublicationCommandType.REQUEST_PUBLICATION
    )
    idempotency_key_digest: Fingerprint
    request_fingerprint: Fingerprint
    response: PublicationRequestResponse
    created_at: UtcDateTime
    fingerprint: Fingerprint

    @model_validator(mode="after")
    def response_matches_receipt_identity(self) -> PublicationCommandReceipt:
        if (
            self.response.job_id != self.job_id
            or self.response.publication_aggregate_id != self.aggregate_id
            or self.response.work_request_id != self.work_request_id
            or self.response.requested_at != self.created_at
        ):
            raise ValueError("Publication receipt response does not match its durable identity")
        if self.fingerprint != publication_command_receipt_fingerprint(self):
            raise ValueError("Publication command receipt fingerprint does not match its payload")
        return self


class PublicationRequestCommit(PublicationModel):
    """Complete pure-domain portion of one all-or-nothing request transaction."""

    job_link: PublicationJobLink
    aggregate: PublicationAggregate
    snapshot: PublicationSnapshot
    attempt: PublicationAttempt
    permit: PublicationPermit
    work_request: PublicationWorkRequest
    event: PublicationDomainEvent
    receipt: PublicationCommandReceipt

    @model_validator(mode="after")
    def request_records_are_one_closed_transaction(self) -> PublicationRequestCommit:
        owner_id = self.snapshot.owner_id
        job_id = self.snapshot.job_id
        aggregate_id = self.aggregate.aggregate_id
        snapshot_id = self.snapshot.snapshot_id
        attempt_id = self.attempt.attempt_id
        permit_id = self.permit.permit_id
        work_request_id = self.work_request.work_request_id
        receipt_id = self.receipt.receipt_id
        requested_at = self.snapshot.requested_at
        verification_deadline = self.snapshot.verification_deadline

        identities = (
            (self.job_link.owner_id, self.job_link.job_id),
            (self.aggregate.owner_id, self.aggregate.job_id),
            (self.attempt.owner_id, self.attempt.job_id),
            (self.permit.owner_id, self.permit.job_id),
            (self.work_request.owner_id, self.work_request.job_id),
            (self.event.owner_id, self.event.job_id),
            (self.receipt.owner_id, self.receipt.job_id),
        )
        if any(identity != (owner_id, job_id) for identity in identities):
            raise ValueError("Publication request records must bind one owner and job")

        if (
            self.job_link.publication_aggregate_id != aggregate_id
            or self.attempt.aggregate_id != aggregate_id
            or self.permit.aggregate_id != aggregate_id
            or self.work_request.aggregate_id != aggregate_id
            or self.event.aggregate_id != aggregate_id
            or self.receipt.aggregate_id != aggregate_id
        ):
            raise ValueError("Publication request records must bind one separate aggregate")

        if (
            self.aggregate.snapshot_id != snapshot_id
            or self.attempt.snapshot_id != snapshot_id
            or self.permit.snapshot_id != snapshot_id
            or self.work_request.snapshot_id != snapshot_id
            or self.event.snapshot_id != snapshot_id
            or self.receipt.snapshot_id != snapshot_id
        ):
            raise ValueError("Publication request records must bind one immutable snapshot")
        if (
            self.aggregate.snapshot_fingerprint != self.snapshot.fingerprint
            or self.attempt.snapshot_fingerprint != self.snapshot.fingerprint
            or self.permit.snapshot_fingerprint != self.snapshot.fingerprint
            or self.work_request.snapshot_fingerprint != self.snapshot.fingerprint
        ):
            raise ValueError("Publication records must bind the exact snapshot fingerprint")

        if (
            self.aggregate.attempt_id != attempt_id
            or self.permit.attempt_id != attempt_id
            or self.work_request.attempt_id != attempt_id
            or self.event.attempt_id != attempt_id
            or self.receipt.attempt_id != attempt_id
        ):
            raise ValueError("Publication request records must bind one root attempt")
        if (
            self.aggregate.permit_id != permit_id
            or self.work_request.permit_id != permit_id
            or self.event.permit_id != permit_id
            or self.receipt.permit_id != permit_id
        ):
            raise ValueError("Publication request records must bind one available permit")
        if (
            self.aggregate.work_request_id != work_request_id
            or self.permit.work_request_id != work_request_id
            or self.event.work_request_id != work_request_id
            or self.receipt.work_request_id != work_request_id
        ):
            raise ValueError("Publication request records must bind one pending work request")
        if self.aggregate.receipt_id != receipt_id or self.work_request.receipt_id != receipt_id:
            raise ValueError("Publication request records must bind one command receipt")

        if (
            self.snapshot.expected_record_version != self.job_link.expected_record_version
            or self.receipt.response.record_version != self.job_link.result_record_version
            or self.receipt.response.review_version != self.snapshot.review_version
        ):
            raise ValueError("Publication response must match the conditioned Phase 6 authority")
        if self.event.sequence != 1:
            raise ValueError("Publication aggregate event stream must begin at sequence one")

        timestamps = (
            self.job_link.linked_at,
            self.aggregate.requested_at,
            self.aggregate.updated_at,
            self.attempt.requested_at,
            self.permit.created_at,
            self.work_request.created_at,
            self.work_request.updated_at,
            self.work_request.next_dispatch_at,
            self.event.occurred_at,
            self.receipt.created_at,
            self.receipt.response.requested_at,
        )
        if any(timestamp != requested_at for timestamp in timestamps):
            raise ValueError("Publication request transaction must use one request timestamp")
        if (
            self.attempt.verification_deadline != verification_deadline
            or self.work_request.verification_deadline != verification_deadline
            or self.receipt.response.verification_deadline != verification_deadline
        ):
            raise ValueError("Publication request records cannot move the root deadline")
        return self
