"""Atomic persistence boundary for Phase 6 seller-control commands."""

from __future__ import annotations

import re
from base64 import urlsafe_b64decode, urlsafe_b64encode
from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from threading import RLock
from types import MappingProxyType
from typing import Protocol

from mr_lister.control.dispatch import deterministic_execution_name, work_input_fingerprint
from mr_lister.control.errors import (
    ConcurrentControlModificationError,
    IdempotencyConflictError,
    InvalidControlStateError,
    NotFoundError,
)
from mr_lister.control.fingerprints import product_sync_record_fingerprint, review_etag
from mr_lister.control.models import (
    CONTROL_NEW_WORK_BY_STATE,
    CONTROL_RECOVERY_BINDINGS,
    AgentPreparationEvidence,
    ArtworkAnalysisRecord,
    CancellationDecisionRecord,
    CommandReceipt,
    ControlJobRecord,
    ControlJobState,
    DomainEvent,
    FailureRecord,
    PricingEvidenceRecord,
    PricingSnapshot,
    ProductSyncRecord,
    ProviderCallPermit,
    ProviderCallPermitStatus,
    ProviderUploadAttempt,
    ProviderWriteAttempt,
    ProviderWriteOperation,
    ReconciliationObservationRecord,
    ReviewContent,
    ReviewDecision,
    ReviewDecisionRecord,
    SourceArtifactRecord,
    UploadedArtworkRecord,
    UploadReconciliationObservationRecord,
    WorkRequest,
    WorkRequestStatus,
    WorkType,
    can_control_transition,
)
from mr_lister.control.source_artwork import validate_source_artifact_authority
from mr_lister.control.upload_models import (
    UploadCompletionCommit,
    UploadIntent,
    UploadIntentCommit,
    UploadReceipt,
)

_WORK_IDENTITY_FIELDS = (
    "contract_version",
    "work_request_id",
    "owner_id",
    "job_id",
    "receipt_id",
    "work_type",
    "review_version",
    "input_fingerprint",
    "execution_name",
    "created_at",
)
_COMMAND_WORK_TRANSITIONS = frozenset(
    {
        (WorkRequestStatus.PENDING, WorkRequestStatus.CANCELLED),
        (WorkRequestStatus.CLAIMED, WorkRequestStatus.COMPLETED),
        (WorkRequestStatus.DISPATCHED, WorkRequestStatus.COMPLETED),
    }
)
_SAFE_JOB_ID = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,127}$")
_OWNER_JOB_SORT_KEY = re.compile(r"^[0-9]{20}#[a-zA-Z0-9][a-zA-Z0-9_-]{0,127}$")


def owner_job_sort_key(job: ControlJobRecord) -> str:
    """Build the stable, descending-query sort key for a current job row."""

    if _SAFE_JOB_ID.fullmatch(job.job_id) is None:
        raise ValueError("The job identifier cannot be used as a page cursor")
    seconds = int(job.updated_at.timestamp())
    epoch_micros = seconds * 1_000_000 + job.updated_at.microsecond
    return f"{epoch_micros:020d}#{job.job_id}"


def encode_owner_job_cursor(job: ControlJobRecord) -> str:
    """Encode a bounded owner-index key as an opaque URL-safe page cursor."""

    material = owner_job_sort_key(job)
    return urlsafe_b64encode(material.encode("ascii")).decode("ascii").rstrip("=")


def decode_owner_job_cursor(cursor: str) -> tuple[str, str]:
    """Decode only canonical cursors emitted by :func:`encode_owner_job_cursor`."""

    if not cursor or len(cursor) > 200 or not cursor.isascii():
        raise ValueError("The owner job page cursor is invalid")
    try:
        decoded = urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)).decode("ascii")
    except (UnicodeDecodeError, ValueError):
        raise ValueError("The owner job page cursor is invalid") from None
    if _OWNER_JOB_SORT_KEY.fullmatch(decoded) is None:
        raise ValueError("The owner job page cursor is invalid")
    canonical = urlsafe_b64encode(decoded.encode("ascii")).decode("ascii").rstrip("=")
    if canonical != cursor:
        raise ValueError("The owner job page cursor is invalid")
    _timestamp, job_id = decoded.split("#", 1)
    return decoded, job_id


@dataclass(frozen=True)
class CommandCommit:
    """One all-or-nothing application command transaction."""

    current: ControlJobRecord
    updated: ControlJobRecord
    event: DomainEvent
    receipt: CommandReceipt
    review: ReviewContent | None = None
    artwork_analysis: ArtworkAnalysisRecord | None = None
    agent_evidence: AgentPreparationEvidence | None = None
    review_decision: ReviewDecisionRecord | None = None
    cancellation_decision: CancellationDecisionRecord | None = None
    product_sync: ProductSyncRecord | None = None
    provider_upload_attempt: ProviderUploadAttempt | None = None
    uploaded_artwork: UploadedArtworkRecord | None = None
    provider_write_attempt: ProviderWriteAttempt | None = None
    provider_write_retry_basis: ProviderWriteAttempt | None = None
    provider_call_permit: ProviderCallPermit | None = None
    provider_call_permit_update: tuple[ProviderCallPermit, ProviderCallPermit] | None = None
    reconciliation_observation: ReconciliationObservationRecord | None = None
    upload_reconciliation_observation: UploadReconciliationObservationRecord | None = None
    pricing_evidence: PricingEvidenceRecord | None = None
    pricing_snapshot: PricingSnapshot | None = None
    failure: FailureRecord | None = None
    work_request: WorkRequest | None = None
    work_update: tuple[WorkRequest, WorkRequest] | None = None


@dataclass(frozen=True)
class OwnerJobPage:
    """One bounded, owner-scoped page sorted by most recent durable change."""

    jobs: tuple[ControlJobRecord, ...]
    next_cursor: str | None = None


def revalidate_work_request(current: WorkRequest, **updates: object) -> WorkRequest:
    """Apply an operational update without bypassing the strict work contract."""

    payload = current.model_dump(mode="python")
    payload.update(updates)
    return WorkRequest.model_validate(payload)


def validate_new_work_request(job: ControlJobRecord, work: WorkRequest) -> None:
    """Prove a new outbox record is the only work authorized by the resulting state."""

    if work.status is not WorkRequestStatus.PENDING:
        raise InvalidControlStateError("New work must begin pending")
    if (
        work.attempt_count != 0
        or work.claim_id is not None
        or work.lease_expires_at is not None
        or work.execution_arn is not None
        or work.last_error_code is not None
        or work.updated_at != work.created_at
    ):
        raise InvalidControlStateError("New work must begin with pristine dispatch authority")
    if CONTROL_NEW_WORK_BY_STATE.get(job.state) is not work.work_type:
        raise InvalidControlStateError("The work type does not match the resulting job state")
    if work.work_type is not WorkType.PREPARE and (
        job.review_version < 1 or work.review_version != job.review_version
    ):
        raise InvalidControlStateError("Provider work must bind the exact current review")
    if work.review_version is not None and work.review_version != job.review_version:
        raise InvalidControlStateError("The work request does not bind the current review")
    if work.execution_name != deterministic_execution_name(work.work_request_id):
        raise InvalidControlStateError("The work execution name is not deterministic")
    if work.input_fingerprint != work_input_fingerprint(
        work_type=work.work_type,
        job_id=work.job_id,
        work_request_id=work.work_request_id,
    ):
        raise InvalidControlStateError("The work input fingerprint is not deterministic")


def validate_initial_job(
    job: ControlJobRecord,
    event: DomainEvent,
    receipt: CommandReceipt,
    work: WorkRequest | None,
    source_artifact: SourceArtifactRecord | None,
) -> None:
    """Restrict job creation to the upload-completion intake transaction."""

    if job.state is not ControlJobState.INTAKE_VALIDATED:
        raise InvalidControlStateError("A new job must begin in INTAKE_VALIDATED")
    if job.record_version != 0 or job.event_sequence != 1 or event.sequence != 1:
        raise InvalidControlStateError("A new job must atomically create its first event")
    if (
        job.review_version != 0
        or job.review_fingerprint is not None
        or job.review_validated
        or job.artwork_analysis_id is not None
        or job.agent_evidence_id is not None
        or job.product_id is not None
        or job.provider_payload_fingerprint is not None
        or job.product_sync_id is not None
        or job.pricing_snapshot_id is not None
        or job.approval_decision_id is not None
        or job.approval_fingerprint is not None
        or job.publication_aggregate_id is not None
        or job.failure_id is not None
        or job.provider_upload_attempt_id is not None
        or job.uploaded_artwork_id is not None
        or job.uploaded_image_id is not None
        or job.uploaded_artwork_fingerprint is not None
        or job.provider_write_attempt_id is not None
        or job.product_create_attempt_id is not None
        or job.cancellation_requested_at is not None
        or job.provider_outcome_unconfirmed
        or job.upload_outcome_unconfirmed
        or job.updated_at != job.created_at
    ):
        raise InvalidControlStateError("A new intake job must begin with pristine authority")
    if work is None:
        raise InvalidControlStateError("INTAKE_VALIDATED requires its preparation work")
    if source_artifact is None:
        raise InvalidControlStateError("INTAKE_VALIDATED requires its pinned source artifact")
    try:
        validate_source_artifact_authority(source_artifact)
    except ValueError:
        raise InvalidControlStateError("The initial source artifact authority is invalid") from None
    if (
        source_artifact.job_id != job.job_id
        or source_artifact.owner_id != job.owner_id
        or job.source_artifact_fingerprint != source_artifact.fingerprint
    ):
        raise InvalidControlStateError("The initial source artifact does not match the job")
    if work.work_type is not WorkType.PREPARE or work.review_version is not None:
        raise InvalidControlStateError("Initial work type must be unbound PREPARE work")
    if (
        event.job_id != job.job_id
        or receipt.owner_id != job.owner_id
        or receipt.job_id != job.job_id
        or receipt.response.job_id != job.job_id
        or receipt.response.state is not job.state
        or receipt.response.record_version != job.record_version
        or receipt.response.review_version != job.review_version
        or work.job_id != job.job_id
        or work.owner_id != job.owner_id
        or work.receipt_id != receipt.receipt_id
        or job.active_work_request_id != work.work_request_id
        or receipt.work_request_id != work.work_request_id
        or receipt.response.work_request_id != work.work_request_id
    ):
        raise InvalidControlStateError("The initial event, receipt, work, and job do not match")
    validate_new_work_request(job, work)


def validate_command_commit(commit: CommandCommit) -> None:
    """Validate the application-owned transition before an adapter writes anything."""

    current = commit.current
    updated = commit.updated
    retired_permit_attempt_id = None
    if commit.provider_call_permit_update is not None:
        if commit.provider_call_permit is not None:
            raise InvalidControlStateError(
                "A command cannot create and retire a provider permit together"
            )
        expected_permit, retired_permit = commit.provider_call_permit_update
        unchanged_identity = (
            expected_permit.attempt_id == retired_permit.attempt_id
            and expected_permit.job_id == retired_permit.job_id == current.job_id
            and expected_permit.work_request_id == retired_permit.work_request_id
            and expected_permit.created_at == retired_permit.created_at
        )
        if (
            not unchanged_identity
            or expected_permit.status is not ProviderCallPermitStatus.AVAILABLE
            or retired_permit.status is not ProviderCallPermitStatus.RETIRED
            or retired_permit.retired_at is None
            or retired_permit.consumed_at is not None
            or retired_permit.consumed_work_request_id is not None
            or updated.state is not ControlJobState.CANCELLED
            or updated.cancellation_requested_at is None
            or expected_permit.attempt_id
            not in {
                current.provider_upload_attempt_id,
                current.provider_write_attempt_id,
            }
        ):
            raise InvalidControlStateError(
                "Cancellation permit retirement does not match durable provider authority"
            )
        retired_permit_attempt_id = expected_permit.attempt_id
    if updated.job_id != current.job_id or updated.owner_id != current.owner_id:
        raise InvalidControlStateError("A command cannot change job identity or ownership")
    if updated.publication_aggregate_id != current.publication_aggregate_id:
        raise InvalidControlStateError(
            "Phase 6 commands cannot change publication aggregate authority"
        )
    if updated.record_version != current.record_version + 1:
        raise InvalidControlStateError("A command must increment record_version exactly once")
    if updated.event_sequence != current.event_sequence + 1:
        raise InvalidControlStateError("A command must increment event_sequence exactly once")
    if updated.created_at != current.created_at:
        raise InvalidControlStateError("A command cannot change job creation time")
    if updated.source_artifact_fingerprint != current.source_artifact_fingerprint:
        raise InvalidControlStateError("A command cannot change pinned source authority")
    if updated.updated_at < current.updated_at:
        raise InvalidControlStateError("A command cannot move job time backwards")
    if not can_control_transition(current.state, updated.state):
        raise InvalidControlStateError(
            f"Cannot transition from {current.state.value} to {updated.state.value}"
        )
    if updated.state in CONTROL_NEW_WORK_BY_STATE and updated.active_work_request_id is None:
        raise InvalidControlStateError("The resulting machine state requires durable work")
    if current.cancellation_requested_at is not None:
        if updated.cancellation_requested_at != current.cancellation_requested_at:
            raise InvalidControlStateError("Cancellation intent is immutable")
        if updated.state not in {
            ControlJobState.CANCEL_REQUESTED,
            ControlJobState.RECONCILIATION_REQUIRED,
            ControlJobState.CANCELLED,
        }:
            raise InvalidControlStateError("Cancellation intent forbids normal job recovery")
    elif updated.cancellation_requested_at is not None:
        if commit.cancellation_decision is None:
            raise InvalidControlStateError("Cancellation intent requires an immutable decision")
    elif commit.cancellation_decision is not None:
        raise InvalidControlStateError("A cancellation decision must establish intent")
    if commit.event.job_id != updated.job_id or commit.event.sequence != updated.event_sequence:
        raise InvalidControlStateError("The event does not match the committed job sequence")
    receipt = commit.receipt
    if receipt.owner_id != updated.owner_id or receipt.job_id != updated.job_id:
        raise InvalidControlStateError("The command receipt does not match the job owner")
    if receipt.response.job_id != updated.job_id:
        raise InvalidControlStateError("The receipt response does not match the job")
    if (
        receipt.response.state is not updated.state
        or receipt.response.record_version != updated.record_version
        or receipt.response.review_version != updated.review_version
    ):
        raise InvalidControlStateError("The receipt response does not match the committed result")

    if commit.review is not None:
        review = commit.review
        if review.job_id != updated.job_id:
            raise InvalidControlStateError("The review does not match the job")
        if review.review_version != current.review_version + 1:
            raise InvalidControlStateError(
                "A new review must increment review_version exactly once"
            )
        if (
            updated.review_version != review.review_version
            or updated.review_fingerprint != review.fingerprint
            or updated.review_validated != review.validation_passed
        ):
            raise InvalidControlStateError("The job does not point to the committed review")
    elif (
        updated.review_version != current.review_version
        or updated.review_fingerprint != current.review_fingerprint
        or updated.review_validated != current.review_validated
    ):
        raise InvalidControlStateError("Review authority changed without an immutable review")

    current_analysis = (current.artwork_analysis_id, current.artwork_analysis_fingerprint)
    updated_analysis = (updated.artwork_analysis_id, updated.artwork_analysis_fingerprint)
    if current_analysis != updated_analysis:
        analysis = commit.artwork_analysis
        if analysis is None:
            raise InvalidControlStateError("Artwork analysis authority requires immutable proof")
        if (
            analysis.job_id != updated.job_id
            or analysis.analysis_id != updated.artwork_analysis_id
            or analysis.fingerprint != updated.artwork_analysis_fingerprint
            or analysis.source_artifact_fingerprint != updated.source_artifact_fingerprint
        ):
            raise InvalidControlStateError("Artwork analysis proof does not match the job")
        if (
            current.state is not ControlJobState.ANALYZING_ARTWORK
            or commit.review is None
            or analysis.work_request_id != current.active_work_request_id
        ):
            raise InvalidControlStateError("Artwork analysis must checkpoint active preparation")
    elif commit.artwork_analysis is not None:
        raise InvalidControlStateError("Artwork analysis proof must advance job authority")

    current_agent = (current.agent_evidence_id, current.agent_evidence_fingerprint)
    updated_agent = (updated.agent_evidence_id, updated.agent_evidence_fingerprint)
    if current_agent != updated_agent:
        evidence = commit.agent_evidence
        if evidence is None:
            raise InvalidControlStateError("Agent evidence authority requires immutable proof")
        if (
            evidence.job_id != updated.job_id
            or evidence.evidence_id != updated.agent_evidence_id
            or evidence.fingerprint != updated.agent_evidence_fingerprint
            or evidence.fingerprint != evidence.authority_fingerprint
            or evidence.review_version != updated.review_version
        ):
            raise InvalidControlStateError("Agent preparation evidence does not match the job")
        if (
            current.state is not ControlJobState.LISTING_DRAFTED
            or commit.work_update is None
            or evidence.work_request_id != commit.work_update[0].work_request_id
            or commit.work_update[0].work_type is not WorkType.PREPARE
        ):
            raise InvalidControlStateError("Agent evidence must settle exact preparation work")
    elif commit.agent_evidence is not None:
        raise InvalidControlStateError("Agent evidence proof must advance job authority")

    if commit.review_decision is not None:
        decision = commit.review_decision
        if (
            decision.job_id != updated.job_id
            or decision.actor_owner_id != updated.owner_id
            or decision.command_receipt_id != receipt.receipt_id
        ):
            raise InvalidControlStateError("The review decision does not match the command")
        if (
            decision.review_version != current.review_version
            or decision.review_fingerprint != current.review_fingerprint
        ):
            raise InvalidControlStateError("The decision does not bind the exact current review")
        if decision.approval_fingerprint != updated.approval_fingerprint:
            raise InvalidControlStateError("The decision does not bind the committed approval")
        if decision.decision is ReviewDecision.APPROVE:
            if (
                updated.state is not ControlJobState.APPROVED
                or updated.approval_decision_id != decision.decision_id
                or commit.review is not None
            ):
                raise InvalidControlStateError("Approval decision does not match the transition")
        elif current.review_version == 0 or commit.review is None:
            raise InvalidControlStateError("Revision decision requires a prior and new review")
    if commit.cancellation_decision is not None:
        decision = commit.cancellation_decision
        if (
            decision.job_id != updated.job_id
            or decision.actor_owner_id != updated.owner_id
            or decision.command_receipt_id != receipt.receipt_id
            or decision.expected_record_version != current.record_version
        ):
            raise InvalidControlStateError("The cancellation decision does not match the command")
        if (
            decision.review_version != (current.review_version or None)
            or decision.review_fingerprint != current.review_fingerprint
        ):
            raise InvalidControlStateError(
                "The cancellation decision does not bind the current review"
            )
        if decision.decided_at != updated.cancellation_requested_at:
            raise InvalidControlStateError("Cancellation intent and decision time must match")

    if current.product_id is not None and updated.product_id != current.product_id:
        raise InvalidControlStateError("A job cannot replace or clear its provider product")
    if (
        current.product_id is None
        and updated.product_id is not None
        and commit.product_sync is None
    ):
        raise InvalidControlStateError("The first provider product requires synchronization proof")
    new_upload_attempt = current.provider_upload_attempt_id != updated.provider_upload_attempt_id
    if new_upload_attempt:
        attempt = commit.provider_upload_attempt
        if current.provider_upload_attempt_id is not None:
            raise InvalidControlStateError("A job may claim its artwork upload only once")
        if (
            attempt is None
            or attempt.job_id != updated.job_id
            or attempt.attempt_id != updated.provider_upload_attempt_id
            or attempt.work_request_id != updated.active_work_request_id
            or attempt.source_artifact_fingerprint != updated.source_artifact_fingerprint
            or current.uploaded_artwork_id is not None
            or current.state is not ControlJobState.PRODUCT_DRAFT_SYNCING
            or not updated.upload_outcome_unconfirmed
            or updated.provider_outcome_unconfirmed
        ):
            raise InvalidControlStateError("Provider upload attempt does not match job authority")
        permit = commit.provider_call_permit
        if (
            permit is None
            or permit.attempt_id != attempt.attempt_id
            or permit.job_id != attempt.job_id
            or permit.work_request_id != attempt.work_request_id
            or permit.status is not ProviderCallPermitStatus.AVAILABLE
            or permit.consumed_at is not None
            or permit.consumed_work_request_id is not None
        ):
            raise InvalidControlStateError("Provider upload requires a pristine one-shot permit")
    elif commit.provider_upload_attempt is not None:
        raise InvalidControlStateError("Provider upload proof must advance attempt authority")

    current_upload_authority = (
        current.uploaded_artwork_id,
        current.uploaded_image_id,
        current.uploaded_artwork_fingerprint,
    )
    updated_upload_authority = (
        updated.uploaded_artwork_id,
        updated.uploaded_image_id,
        updated.uploaded_artwork_fingerprint,
    )
    if current_upload_authority != updated_upload_authority:
        upload = commit.uploaded_artwork
        if any(value is not None for value in current_upload_authority):
            raise InvalidControlStateError("Confirmed uploaded artwork is immutable for the job")
        if (
            upload is None
            or upload.job_id != updated.job_id
            or upload.attempt_id != updated.provider_upload_attempt_id
            or upload.upload_id != updated.uploaded_artwork_id
            or upload.image_id != updated.uploaded_image_id
            or upload.fingerprint != updated.uploaded_artwork_fingerprint
            or upload.source_artifact_fingerprint != updated.source_artifact_fingerprint
            or updated.upload_outcome_unconfirmed
            or updated.provider_outcome_unconfirmed
        ):
            raise InvalidControlStateError("Uploaded artwork proof does not match the job")
    elif commit.uploaded_artwork is not None:
        raise InvalidControlStateError("Uploaded artwork proof must advance job authority")

    if updated.upload_outcome_unconfirmed and updated.provider_outcome_unconfirmed:
        raise InvalidControlStateError("Upload and product-write uncertainty cannot overlap")
    if (
        not current.upload_outcome_unconfirmed
        and updated.upload_outcome_unconfirmed
        and not new_upload_attempt
    ):
        raise InvalidControlStateError("Only a new upload claim can establish upload uncertainty")
    if (
        current.upload_outcome_unconfirmed
        and not updated.upload_outcome_unconfirmed
        and commit.uploaded_artwork is None
        and retired_permit_attempt_id != current.provider_upload_attempt_id
    ):
        raise InvalidControlStateError(
            "Only confirmed upload evidence can clear upload uncertainty"
        )
    if (
        current.provider_outcome_unconfirmed
        and not updated.provider_outcome_unconfirmed
        and commit.product_sync is None
        and commit.reconciliation_observation is None
        and retired_permit_attempt_id != current.provider_write_attempt_id
    ):
        raise InvalidControlStateError(
            "Only provider evidence or permit retirement can clear product uncertainty"
        )
    if current.product_create_attempt_id is not None and (
        updated.product_create_attempt_id != current.product_create_attempt_id
    ):
        raise InvalidControlStateError("A job cannot replace or clear its initial create claim")
    if current.provider_write_attempt_id != updated.provider_write_attempt_id:
        attempt = commit.provider_write_attempt
        if attempt is None:
            raise InvalidControlStateError("Provider write authority requires an immutable attempt")
        if (
            attempt.job_id != updated.job_id
            or attempt.attempt_id != updated.provider_write_attempt_id
            or attempt.work_request_id != updated.active_work_request_id
            or attempt.review_version != updated.review_version
            or attempt.image_id != updated.uploaded_image_id
        ):
            raise InvalidControlStateError("Provider write attempt does not match the job")
        if attempt.operation.value == "create":
            if current.product_create_attempt_id is not None:
                raise InvalidControlStateError("A job may claim its initial product create once")
            if updated.product_create_attempt_id != attempt.attempt_id:
                raise InvalidControlStateError("The initial create claim is not persisted")
        elif updated.product_create_attempt_id != current.product_create_attempt_id:
            raise InvalidControlStateError("An update cannot change initial create authority")
        retry_basis = commit.provider_write_retry_basis
        if attempt.exact_retry_count == 0:
            if retry_basis is not None:
                raise InvalidControlStateError("A first provider write cannot carry retry proof")
        elif (
            retry_basis is None
            or retry_basis.attempt_id != current.provider_write_attempt_id
            or retry_basis.operation is not ProviderWriteOperation.UPDATE
            or attempt.operation is not ProviderWriteOperation.UPDATE
            or attempt.exact_retry_count != retry_basis.exact_retry_count + 1
            or attempt.job_id != retry_basis.job_id
            or attempt.review_version != retry_basis.review_version
            or attempt.product_id != retry_basis.product_id
            or attempt.image_id != retry_basis.image_id
            or attempt.target_payload_fingerprint != retry_basis.target_payload_fingerprint
            or attempt.prior_payload_fingerprint != retry_basis.prior_payload_fingerprint
            or attempt.correlation_token != retry_basis.correlation_token
            or attempt.reconciliation_deadline != retry_basis.reconciliation_deadline
        ):
            raise InvalidControlStateError(
                "An exact provider update retry must inherit immutable attempt authority"
            )
        permit = commit.provider_call_permit
        if (
            permit is None
            or permit.attempt_id != attempt.attempt_id
            or permit.job_id != attempt.job_id
            or permit.work_request_id != attempt.work_request_id
            or permit.status is not ProviderCallPermitStatus.AVAILABLE
            or permit.consumed_at is not None
            or permit.consumed_work_request_id is not None
        ):
            raise InvalidControlStateError("Provider write requires a pristine one-shot permit")
        if (
            (attempt.operation.value == "create") != (current.product_id is None)
            or attempt.product_id != current.product_id
            or attempt.prior_payload_fingerprint != current.provider_payload_fingerprint
            or not updated.provider_outcome_unconfirmed
            or updated.upload_outcome_unconfirmed
        ):
            raise InvalidControlStateError("Provider write operation does not match job authority")
    elif commit.provider_write_attempt is not None:
        raise InvalidControlStateError("Provider write proof must advance attempt authority")
    elif commit.provider_write_retry_basis is not None:
        raise InvalidControlStateError("Provider retry proof requires a new write attempt")
    elif commit.provider_call_permit is not None and not new_upload_attempt:
        raise InvalidControlStateError("A provider permit requires a new write attempt")
    if (
        current.product_create_attempt_id is None
        and updated.product_create_attempt_id is not None
        and commit.provider_write_attempt is None
    ):
        raise InvalidControlStateError("Initial create authority requires an immutable attempt")
    current_sync_authority = (
        current.product_sync_id,
        current.synchronized_review_version,
        current.product_sync_fingerprint,
    )
    updated_sync_authority = (
        updated.product_sync_id,
        updated.synchronized_review_version,
        updated.product_sync_fingerprint,
    )
    if current_sync_authority != updated_sync_authority:
        if updated.product_sync_id is None:
            if commit.review is None:
                raise InvalidControlStateError(
                    "Synchronization authority may clear only for a new review"
                )
        elif commit.product_sync is None:
            raise InvalidControlStateError(
                "New synchronization authority requires an immutable record"
            )
    elif commit.product_sync is not None:
        raise InvalidControlStateError("Synchronization proof must advance job authority")
    if commit.product_sync is not None:
        sync = commit.product_sync
        if (
            sync.job_id != updated.job_id
            or updated.product_sync_id != sync.sync_id
            or updated.product_id != sync.product_id
            or updated.synchronized_review_version != sync.review_version
            or updated.product_sync_fingerprint != sync.fingerprint
            or updated.provider_payload_fingerprint != sync.payload_fingerprint
            or sync.printify_shop_id is None
            or sync.fingerprint != product_sync_record_fingerprint(sync)
        ):
            raise InvalidControlStateError("The product synchronization does not match the job")
    elif updated.provider_payload_fingerprint != current.provider_payload_fingerprint:
        raise InvalidControlStateError("Provider payload authority requires synchronization proof")
    if commit.reconciliation_observation is not None:
        observation = commit.reconciliation_observation
        if (
            observation.job_id != updated.job_id
            or observation.attempt_id != current.provider_write_attempt_id
            or commit.work_update is None
            or observation.work_request_id != commit.work_update[0].work_request_id
        ):
            raise InvalidControlStateError("Reconciliation evidence does not bind exact work")
    if commit.upload_reconciliation_observation is not None:
        observation = commit.upload_reconciliation_observation
        if (
            observation.job_id != updated.job_id
            or observation.attempt_id != current.provider_upload_attempt_id
            or commit.work_update is None
            or observation.work_request_id != commit.work_update[0].work_request_id
        ):
            raise InvalidControlStateError(
                "Upload reconciliation evidence does not bind exact work"
            )
    if (commit.pricing_snapshot is None) != (commit.pricing_evidence is None):
        raise InvalidControlStateError(
            "Pricing authority requires both its snapshot and complete evidence"
        )
    if commit.pricing_snapshot is not None:
        pricing = commit.pricing_snapshot
        evidence = commit.pricing_evidence
        assert evidence is not None
        if (
            pricing.job_id != updated.job_id
            or updated.pricing_snapshot_id != pricing.snapshot_id
            or updated.pricing_snapshot_fingerprint != pricing.fingerprint
            or pricing.review_version != updated.review_version
            or pricing.product_sync_fingerprint != updated.product_sync_fingerprint
            or evidence.snapshot_id != pricing.snapshot_id
            or evidence.job_id != pricing.job_id
            or evidence.review_version != pricing.review_version
            or evidence.product_sync_fingerprint != pricing.product_sync_fingerprint
            or evidence.fingerprint != pricing.fingerprint
            or evidence.created_at != pricing.created_at
            or evidence.estimate.fresh_until != pricing.fresh_until
        ):
            raise InvalidControlStateError(
                "The pricing snapshot and complete evidence do not match the job"
            )
    current_pricing_authority = (
        current.pricing_snapshot_id,
        current.pricing_snapshot_fingerprint,
    )
    updated_pricing_authority = (
        updated.pricing_snapshot_id,
        updated.pricing_snapshot_fingerprint,
    )
    if current_pricing_authority != updated_pricing_authority:
        if updated.pricing_snapshot_id is None:
            if (
                commit.review is None
                and commit.product_sync is None
                and updated.state is not ControlJobState.PRICING_REFRESHING
            ):
                raise InvalidControlStateError(
                    "Pricing authority may clear only for review, sync, or refresh work"
                )
        elif commit.pricing_snapshot is None:
            raise InvalidControlStateError("New pricing authority requires an immutable snapshot")
    elif commit.pricing_snapshot is not None:
        raise InvalidControlStateError("Pricing proof must advance job authority")
    if commit.failure is not None:
        failure = commit.failure
        if (
            failure.job_id != updated.job_id
            or updated.failure_id != failure.failure_id
            or failure.stage is not current.state
            or commit.work_update is None
        ):
            raise InvalidControlStateError("The failure record does not match the job")
        expected_work, changed_work = commit.work_update
        if (
            failure.work_request_id != expected_work.work_request_id
            or changed_work.last_error_code != failure.code
        ):
            raise InvalidControlStateError("The failure does not bind the settled work")
        expected_failure_state = (
            ControlJobState.FAILED_RETRYABLE
            if failure.retryable
            else ControlJobState.FAILED_TERMINAL
        )
        if updated.state is not expected_failure_state:
            raise InvalidControlStateError("Failure retryability does not match the job state")
        if failure.retryable and CONTROL_RECOVERY_BINDINGS.get(failure.recovery_action) != (
            failure.resume_state,
            failure.work_type,
        ):
            raise InvalidControlStateError("The failure recovery specification is not allowed")
        if failure.retryable and failure.work_type is not expected_work.work_type:
            raise InvalidControlStateError("Recovery work does not match the failed operation")

    if updated.state is ControlJobState.APPROVED and (
        commit.review_decision is None
        or commit.review_decision.decision is not ReviewDecision.APPROVE
    ):
        raise InvalidControlStateError("Approval requires an immutable approval decision")
    if updated.state is ControlJobState.APPROVED:
        expected_approval = review_etag(
            job_id=current.job_id,
            review_version=current.review_version,
            review_fingerprint=current.review_fingerprint or "",
            product_id=current.product_id,
            product_sync_fingerprint=current.product_sync_fingerprint,
            pricing_snapshot_id=current.pricing_snapshot_id,
            pricing_snapshot_fingerprint=current.pricing_snapshot_fingerprint,
        )
        if (
            commit.review_decision is None
            or updated.approval_decision_id != commit.review_decision.decision_id
            or updated.approval_fingerprint != expected_approval
            or commit.review_decision.approval_fingerprint != expected_approval
        ):
            raise InvalidControlStateError("Approval does not bind the exact composite authority")
    if (
        commit.review is not None
        and current.review_version > 0
        and (
            commit.review_decision is None
            or commit.review_decision.decision is not ReviewDecision.REVISE
        )
    ):
        raise InvalidControlStateError("A seller revision requires an immutable decision")
    if updated.failure_id != current.failure_id:
        if updated.failure_id is not None and commit.failure is None:
            raise InvalidControlStateError("A failure transition requires an immutable record")
        if updated.failure_id is None and commit.failure is not None:
            raise InvalidControlStateError("A cleared failure cannot create another failure record")

    if commit.work_request is not None:
        work = commit.work_request
        if (
            work.job_id != updated.job_id
            or work.owner_id != updated.owner_id
            or work.receipt_id != receipt.receipt_id
            or updated.active_work_request_id != work.work_request_id
            or receipt.work_request_id != work.work_request_id
            or receipt.response.work_request_id != work.work_request_id
        ):
            raise InvalidControlStateError("The work request does not match the command result")
        validate_new_work_request(updated, work)
        if work.review_version is not None and work.review_version != updated.review_version:
            raise InvalidControlStateError("The work request does not bind the current review")
    elif receipt.work_request_id is not None or receipt.response.work_request_id is not None:
        raise InvalidControlStateError("A receipt cannot reference absent new work")

    if commit.work_update is not None:
        expected, changed = commit.work_update
        if (
            expected.work_request_id != changed.work_request_id
            or expected.job_id != updated.job_id
            or changed.job_id != updated.job_id
            or expected.owner_id != updated.owner_id
            or changed.owner_id != updated.owner_id
            or current.active_work_request_id != expected.work_request_id
        ):
            raise InvalidControlStateError("The work update does not match the job")
        if (expected.status, changed.status) not in _COMMAND_WORK_TRANSITIONS:
            raise InvalidControlStateError("The command uses an illegal work status transition")
        if any(
            getattr(expected, field) != getattr(changed, field) for field in _WORK_IDENTITY_FIELDS
        ):
            raise InvalidControlStateError("A command cannot change work identity")
        if changed.attempt_count != expected.attempt_count:
            raise InvalidControlStateError("A command cannot change dispatch attempts")
        if changed.execution_arn != expected.execution_arn:
            raise InvalidControlStateError("A command cannot change execution identity")
        if changed.updated_at < expected.updated_at:
            raise InvalidControlStateError("A work update cannot move time backwards")
        expected_active = (
            None if commit.work_request is None else commit.work_request.work_request_id
        )
        if updated.active_work_request_id != expected_active:
            raise InvalidControlStateError("The job does not point to the resulting active work")

    if current.active_work_request_id != updated.active_work_request_id:
        if current.active_work_request_id is not None and commit.work_update is None:
            raise InvalidControlStateError("Active work changed without settling prior work")
        if updated.active_work_request_id is not None and commit.work_request is None:
            raise InvalidControlStateError("Active work changed without immutable new work")
    elif commit.work_request is not None or commit.work_update is not None:
        raise InvalidControlStateError("Work records changed without changing active work")
    elif (
        updated.active_work_request_id is not None
        and updated.state is not ControlJobState.CANCEL_REQUESTED
    ):
        current_type = CONTROL_NEW_WORK_BY_STATE.get(current.state)
        updated_type = CONTROL_NEW_WORK_BY_STATE.get(updated.state)
        if current_type is None or updated_type is not current_type:
            raise InvalidControlStateError(
                "Retained work does not match the resulting machine state"
            )


class SellerControlStore(Protocol):
    def get_job(self, job_id: str) -> ControlJobRecord: ...

    def get_job_for_owner(self, owner_id: str, job_id: str) -> ControlJobRecord: ...

    def list_jobs_for_owner(
        self,
        owner_id: str,
        *,
        limit: int = 25,
        cursor: str | None = None,
    ) -> OwnerJobPage: ...

    def get_upload_intent_for_owner(self, owner_id: str, upload_id: str) -> UploadIntent: ...

    def resolve_upload_receipt(
        self,
        owner_id: str,
        command_type: str,
        upload_id: str,
        key_digest: str,
    ) -> UploadReceipt | None: ...

    def commit_upload_intent(self, commit: UploadIntentCommit) -> UploadReceipt: ...

    def complete_upload(self, commit: UploadCompletionCommit) -> UploadReceipt: ...

    def get_review(self, job_id: str, review_version: int) -> ReviewContent: ...

    def get_review_decision(self, job_id: str, decision_id: str) -> ReviewDecisionRecord: ...

    def get_source_artifact(self, job_id: str) -> SourceArtifactRecord: ...

    def get_artwork_analysis(self, job_id: str, analysis_id: str) -> ArtworkAnalysisRecord: ...

    def get_agent_evidence(self, job_id: str, evidence_id: str) -> AgentPreparationEvidence: ...

    def get_product_sync(self, job_id: str, sync_id: str) -> ProductSyncRecord: ...

    def get_provider_upload_attempt(
        self, job_id: str, attempt_id: str
    ) -> ProviderUploadAttempt: ...

    def get_uploaded_artwork(self, job_id: str, upload_id: str) -> UploadedArtworkRecord: ...

    def get_pricing(self, job_id: str, snapshot_id: str) -> PricingSnapshot: ...

    def get_pricing_evidence(self, job_id: str, snapshot_id: str) -> PricingEvidenceRecord: ...

    def get_failure(self, job_id: str, failure_id: str) -> FailureRecord: ...

    def get_provider_write_attempt(self, job_id: str, attempt_id: str) -> ProviderWriteAttempt: ...

    def get_provider_call_permit(self, job_id: str, attempt_id: str) -> ProviderCallPermit: ...

    def consume_provider_call_permit(
        self,
        job: ControlJobRecord,
        work: WorkRequest,
        attempt_id: str,
        *,
        now: datetime,
    ) -> ProviderCallPermit | None: ...

    def get_work_request(self, job_id: str, work_request_id: str) -> WorkRequest: ...

    def resolve_receipt(
        self, owner_id: str, command_type: str, job_id: str, key_digest: str
    ) -> CommandReceipt | None: ...

    def create_job(
        self,
        *,
        job: ControlJobRecord,
        event: DomainEvent,
        receipt: CommandReceipt,
        work_request: WorkRequest | None = None,
        source_artifact: SourceArtifactRecord | None = None,
    ) -> CommandReceipt: ...

    def commit_command(self, commit: CommandCommit) -> CommandReceipt: ...

    def nudge_pending_work(
        self, job_id: str, work_request_id: str, *, now: datetime
    ) -> WorkRequest: ...


class InMemorySellerControlStore:
    """Thread-safe deterministic oracle for the Phase 6 DynamoDB transaction contract."""

    def __init__(self) -> None:
        self._lock = RLock()
        self._jobs: dict[str, ControlJobRecord] = {}
        self._sources: dict[str, SourceArtifactRecord] = {}
        self._artwork_analyses: dict[tuple[str, str], ArtworkAnalysisRecord] = {}
        self._agent_evidence: dict[tuple[str, str], AgentPreparationEvidence] = {}
        self._reviews: dict[tuple[str, int], ReviewContent] = {}
        self._review_decisions: dict[str, ReviewDecisionRecord] = {}
        self._cancellation_decisions: dict[str, CancellationDecisionRecord] = {}
        self._product_syncs: dict[tuple[str, str], ProductSyncRecord] = {}
        self._provider_upload_attempts: dict[tuple[str, str], ProviderUploadAttempt] = {}
        self._uploaded_artwork: dict[tuple[str, str], UploadedArtworkRecord] = {}
        self._provider_write_attempts: dict[tuple[str, str], ProviderWriteAttempt] = {}
        self._provider_call_permits: dict[tuple[str, str], ProviderCallPermit] = {}
        self._reconciliation_observations: dict[
            tuple[str, str], ReconciliationObservationRecord
        ] = {}
        self._upload_reconciliation_observations: dict[
            tuple[str, str], UploadReconciliationObservationRecord
        ] = {}
        self._pricing: dict[tuple[str, str], PricingSnapshot] = {}
        self._pricing_evidence: dict[tuple[str, str], PricingEvidenceRecord] = {}
        self._failures: dict[tuple[str, str], FailureRecord] = {}
        self._work: dict[tuple[str, str], WorkRequest] = {}
        self._events: dict[str, list[DomainEvent]] = defaultdict(list)
        self._receipts: dict[tuple[str, str, str, str], CommandReceipt] = {}
        self._upload_intents: dict[str, UploadIntent] = {}
        self._upload_receipts: dict[tuple[str, str, str, str], UploadReceipt] = {}

    @property
    def jobs(self) -> Mapping[str, ControlJobRecord]:
        return MappingProxyType(self._jobs)

    def get_job(self, job_id: str) -> ControlJobRecord:
        with self._lock:
            try:
                return self._jobs[job_id]
            except KeyError as error:
                raise NotFoundError("The requested job was not found") from error

    def get_job_for_owner(self, owner_id: str, job_id: str) -> ControlJobRecord:
        job = self.get_job(job_id)
        if job.owner_id != owner_id:
            raise NotFoundError("The requested job was not found")
        return job

    def list_jobs_for_owner(
        self,
        owner_id: str,
        *,
        limit: int = 25,
        cursor: str | None = None,
    ) -> OwnerJobPage:
        if not 1 <= limit <= 100:
            raise ValueError("Owner job page limit must be between 1 and 100")
        recent = sorted(
            (job for job in self._jobs.values() if job.owner_id == owner_id),
            key=lambda job: (job.updated_at, job.job_id),
            reverse=True,
        )
        start = 0
        if cursor is not None:
            cursor_sort_key, cursor_job_id = decode_owner_job_cursor(cursor)
            matches = [
                index
                for index, job in enumerate(recent)
                if job.job_id == cursor_job_id and owner_job_sort_key(job) == cursor_sort_key
            ]
            if not matches:
                raise NotFoundError("The requested job page was not found")
            start = matches[0] + 1
        selected = tuple(recent[start : start + limit])
        has_more = start + len(selected) < len(recent)
        return OwnerJobPage(
            jobs=selected,
            next_cursor=(encode_owner_job_cursor(selected[-1]) if selected and has_more else None),
        )

    def get_upload_intent_for_owner(self, owner_id: str, upload_id: str) -> UploadIntent:
        with self._lock:
            intent = self._upload_intents.get(upload_id)
            if intent is None or intent.owner_id != owner_id:
                raise NotFoundError("The requested upload was not found")
            return intent

    def resolve_upload_receipt(
        self,
        owner_id: str,
        command_type: str,
        upload_id: str,
        key_digest: str,
    ) -> UploadReceipt | None:
        with self._lock:
            return self._upload_receipts.get((owner_id, command_type, upload_id, key_digest))

    def commit_upload_intent(self, commit: UploadIntentCommit) -> UploadReceipt:
        with self._lock:
            receipt_key = self._upload_receipt_key(commit.receipt)
            existing_receipt = self._upload_receipts.get(receipt_key)
            if existing_receipt is not None:
                if existing_receipt.request_fingerprint == commit.receipt.request_fingerprint:
                    return existing_receipt
                raise IdempotencyConflictError(
                    "The idempotency key was already used for another upload request"
                )
            persisted = self._upload_intents.get(commit.updated.upload_id)
            if commit.current is None:
                if persisted is not None:
                    raise IdempotencyConflictError("The generated upload identifier already exists")
            elif persisted != commit.current:
                raise ConcurrentControlModificationError(
                    "The upload intent changed before the command could commit"
                )
            self._upload_intents[commit.updated.upload_id] = commit.updated
            self._upload_receipts[receipt_key] = commit.receipt
            return commit.receipt

    def complete_upload(self, commit: UploadCompletionCommit) -> UploadReceipt:
        with self._lock:
            receipt = commit.intent.receipt
            receipt_key = self._upload_receipt_key(receipt)
            existing_receipt = self._upload_receipts.get(receipt_key)
            if existing_receipt is not None:
                if existing_receipt.request_fingerprint == receipt.request_fingerprint:
                    return existing_receipt
                raise IdempotencyConflictError(
                    "The idempotency key was already used for another upload request"
                )
            current = commit.intent.current
            assert current is not None
            if self._upload_intents.get(current.upload_id) != current:
                raise ConcurrentControlModificationError(
                    "The upload intent changed before completion could commit"
                )
            if commit.job.job_id in self._jobs:
                raise IdempotencyConflictError("The reserved job identifier already exists")
            if (commit.job.job_id, commit.work_request.work_request_id) in self._work:
                raise IdempotencyConflictError("The preparation work already exists")
            self._upload_intents[current.upload_id] = commit.intent.updated
            self._upload_receipts[receipt_key] = receipt
            self._jobs[commit.job.job_id] = commit.job
            self._sources[commit.job.job_id] = commit.source_artifact
            self._events[commit.job.job_id].append(commit.event)
            self._work[(commit.job.job_id, commit.work_request.work_request_id)] = (
                commit.work_request
            )
            return receipt

    def get_review(self, job_id: str, review_version: int) -> ReviewContent:
        self.get_job(job_id)
        try:
            return self._reviews[(job_id, review_version)]
        except KeyError as error:
            raise NotFoundError("The requested review was not found") from error

    def get_review_decision(self, job_id: str, decision_id: str) -> ReviewDecisionRecord:
        self.get_job(job_id)
        decision = self._review_decisions.get(decision_id)
        if decision is None or decision.job_id != job_id or decision.decision_id != decision_id:
            raise NotFoundError("The requested review decision was not found")
        return decision

    def get_source_artifact(self, job_id: str) -> SourceArtifactRecord:
        self.get_job(job_id)
        try:
            source = self._sources[job_id]
        except KeyError as error:
            raise NotFoundError("The requested source artifact was not found") from error
        return validate_source_artifact_authority(source)

    def get_artwork_analysis(self, job_id: str, analysis_id: str) -> ArtworkAnalysisRecord:
        self.get_job(job_id)
        try:
            return self._artwork_analyses[(job_id, analysis_id)]
        except KeyError as error:
            raise NotFoundError("The requested artwork analysis was not found") from error

    def get_agent_evidence(self, job_id: str, evidence_id: str) -> AgentPreparationEvidence:
        self.get_job(job_id)
        try:
            return self._agent_evidence[(job_id, evidence_id)]
        except KeyError as error:
            raise NotFoundError("The requested agent evidence was not found") from error

    def get_product_sync(self, job_id: str, sync_id: str) -> ProductSyncRecord:
        self.get_job(job_id)
        try:
            return self._product_syncs[(job_id, sync_id)]
        except KeyError as error:
            raise NotFoundError("The requested product synchronization was not found") from error

    def get_provider_upload_attempt(self, job_id: str, attempt_id: str) -> ProviderUploadAttempt:
        self.get_job(job_id)
        try:
            return self._provider_upload_attempts[(job_id, attempt_id)]
        except KeyError as error:
            raise NotFoundError("The requested provider upload attempt was not found") from error

    def get_uploaded_artwork(self, job_id: str, upload_id: str) -> UploadedArtworkRecord:
        self.get_job(job_id)
        try:
            return self._uploaded_artwork[(job_id, upload_id)]
        except KeyError as error:
            raise NotFoundError("The requested uploaded artwork was not found") from error

    def get_pricing(self, job_id: str, snapshot_id: str) -> PricingSnapshot:
        self.get_job(job_id)
        try:
            return self._pricing[(job_id, snapshot_id)]
        except KeyError as error:
            raise NotFoundError("The requested pricing snapshot was not found") from error

    def get_pricing_evidence(self, job_id: str, snapshot_id: str) -> PricingEvidenceRecord:
        self.get_job(job_id)
        try:
            return self._pricing_evidence[(job_id, snapshot_id)]
        except KeyError as error:
            raise NotFoundError("The requested pricing evidence was not found") from error

    def get_failure(self, job_id: str, failure_id: str) -> FailureRecord:
        self.get_job(job_id)
        try:
            return self._failures[(job_id, failure_id)]
        except KeyError as error:
            raise NotFoundError("The requested failure was not found") from error

    def get_provider_write_attempt(self, job_id: str, attempt_id: str) -> ProviderWriteAttempt:
        self.get_job(job_id)
        try:
            return self._provider_write_attempts[(job_id, attempt_id)]
        except KeyError as error:
            raise NotFoundError("The requested provider write attempt was not found") from error

    def get_provider_call_permit(self, job_id: str, attempt_id: str) -> ProviderCallPermit:
        self.get_job(job_id)
        try:
            return self._provider_call_permits[(job_id, attempt_id)]
        except KeyError as error:
            raise NotFoundError("The requested provider call permit was not found") from error

    def consume_provider_call_permit(
        self,
        job: ControlJobRecord,
        work: WorkRequest,
        attempt_id: str,
        *,
        now: datetime,
    ) -> ProviderCallPermit | None:
        with self._lock:
            if (
                self._jobs.get(job.job_id) != job
                or self._work.get((job.job_id, work.work_request_id)) != work
                or job.active_work_request_id != work.work_request_id
                or work.status not in {WorkRequestStatus.CLAIMED, WorkRequestStatus.DISPATCHED}
            ):
                return None
            permit = self.get_provider_call_permit(job.job_id, attempt_id)
            if permit.status is not ProviderCallPermitStatus.AVAILABLE:
                return None
            consumed = ProviderCallPermit.model_validate(
                {
                    **permit.model_dump(mode="python"),
                    "status": ProviderCallPermitStatus.CONSUMED,
                    "consumed_at": now,
                    "consumed_work_request_id": work.work_request_id,
                }
            )
            self._provider_call_permits[(job.job_id, attempt_id)] = consumed
            return consumed

    def get_work_request(self, job_id: str, work_request_id: str) -> WorkRequest:
        self.get_job(job_id)
        try:
            return self._work[(job_id, work_request_id)]
        except KeyError as error:
            raise NotFoundError("The requested work request was not found") from error

    def resolve_receipt(
        self, owner_id: str, command_type: str, job_id: str, key_digest: str
    ) -> CommandReceipt | None:
        with self._lock:
            return self._receipts.get((owner_id, command_type, job_id, key_digest))

    def create_job(
        self,
        *,
        job: ControlJobRecord,
        event: DomainEvent,
        receipt: CommandReceipt,
        work_request: WorkRequest | None = None,
        source_artifact: SourceArtifactRecord | None = None,
    ) -> CommandReceipt:
        with self._lock:
            validate_initial_job(job, event, receipt, work_request, source_artifact)
            key = self._receipt_key(receipt)
            existing_receipt = self._receipts.get(key)
            if existing_receipt is not None:
                if existing_receipt.request_fingerprint == receipt.request_fingerprint:
                    return existing_receipt
                raise IdempotencyConflictError(
                    "The idempotency key was already used for another request"
                )
            if job.job_id in self._jobs:
                raise IdempotencyConflictError("The generated job identifier already exists")
            if work_request is not None:
                if (job.job_id, work_request.work_request_id) in self._work:
                    raise IdempotencyConflictError("The work request already exists")
            self._jobs[job.job_id] = job
            assert source_artifact is not None
            self._sources[job.job_id] = source_artifact
            self._events[job.job_id].append(event)
            self._receipts[key] = receipt
            if work_request is not None:
                self._work[(job.job_id, work_request.work_request_id)] = work_request
            return receipt

    def commit_command(self, commit: CommandCommit) -> CommandReceipt:
        validate_command_commit(commit)
        with self._lock:
            receipt_key = self._receipt_key(commit.receipt)
            existing_receipt = self._receipts.get(receipt_key)
            if existing_receipt is not None:
                if existing_receipt.request_fingerprint != commit.receipt.request_fingerprint:
                    raise IdempotencyConflictError(
                        "The idempotency key was already used for another request"
                    )
                return existing_receipt
            persisted = self._jobs.get(commit.current.job_id)
            if persisted != commit.current:
                raise ConcurrentControlModificationError(
                    "The job changed before the command could commit"
                )
            if commit.provider_write_retry_basis is not None:
                retry_basis = commit.provider_write_retry_basis
                if (
                    self._provider_write_attempts.get(
                        (commit.current.job_id, retry_basis.attempt_id)
                    )
                    != retry_basis
                ):
                    raise ConcurrentControlModificationError(
                        "The provider retry basis changed before the command could commit"
                    )

            immutable_keys: list[tuple[object, object]] = []
            if commit.review is not None:
                immutable_keys.append(
                    (self._reviews, (commit.updated.job_id, commit.review.review_version))
                )
            if commit.artwork_analysis is not None:
                immutable_keys.append(
                    (
                        self._artwork_analyses,
                        (commit.updated.job_id, commit.artwork_analysis.analysis_id),
                    )
                )
            if commit.agent_evidence is not None:
                immutable_keys.append(
                    (
                        self._agent_evidence,
                        (commit.updated.job_id, commit.agent_evidence.evidence_id),
                    )
                )
            if commit.review_decision is not None:
                immutable_keys.append((self._review_decisions, commit.review_decision.decision_id))
            if commit.cancellation_decision is not None:
                immutable_keys.append(
                    (self._cancellation_decisions, commit.cancellation_decision.decision_id)
                )
            if commit.product_sync is not None:
                immutable_keys.append(
                    (self._product_syncs, (commit.updated.job_id, commit.product_sync.sync_id))
                )
            if commit.provider_upload_attempt is not None:
                immutable_keys.append(
                    (
                        self._provider_upload_attempts,
                        (commit.updated.job_id, commit.provider_upload_attempt.attempt_id),
                    )
                )
            if commit.uploaded_artwork is not None:
                immutable_keys.append(
                    (
                        self._uploaded_artwork,
                        (commit.updated.job_id, commit.uploaded_artwork.upload_id),
                    )
                )
            if commit.provider_write_attempt is not None:
                immutable_keys.append(
                    (
                        self._provider_write_attempts,
                        (commit.updated.job_id, commit.provider_write_attempt.attempt_id),
                    )
                )
            if commit.provider_call_permit is not None:
                immutable_keys.append(
                    (
                        self._provider_call_permits,
                        (commit.updated.job_id, commit.provider_call_permit.attempt_id),
                    )
                )
            if commit.reconciliation_observation is not None:
                immutable_keys.append(
                    (
                        self._reconciliation_observations,
                        (
                            commit.updated.job_id,
                            commit.reconciliation_observation.observation_id,
                        ),
                    )
                )
            if commit.upload_reconciliation_observation is not None:
                immutable_keys.append(
                    (
                        self._upload_reconciliation_observations,
                        (
                            commit.updated.job_id,
                            commit.upload_reconciliation_observation.observation_id,
                        ),
                    )
                )
            if commit.pricing_snapshot is not None:
                immutable_keys.append(
                    (self._pricing, (commit.updated.job_id, commit.pricing_snapshot.snapshot_id))
                )
            if commit.pricing_evidence is not None:
                immutable_keys.append(
                    (
                        self._pricing_evidence,
                        (commit.updated.job_id, commit.pricing_evidence.snapshot_id),
                    )
                )
            if commit.failure is not None:
                immutable_keys.append(
                    (self._failures, (commit.updated.job_id, commit.failure.failure_id))
                )
            if commit.work_request is not None:
                immutable_keys.append(
                    (self._work, (commit.updated.job_id, commit.work_request.work_request_id))
                )
            if any(key in mapping for mapping, key in immutable_keys):
                raise IdempotencyConflictError("An immutable command record already exists")
            if commit.work_update is not None:
                expected, _changed = commit.work_update
                if self._work.get((expected.job_id, expected.work_request_id)) != expected:
                    raise ConcurrentControlModificationError(
                        "The work request changed before the command could commit"
                    )
            if commit.provider_call_permit_update is not None:
                expected_permit, _retired_permit = commit.provider_call_permit_update
                if (
                    self._provider_call_permits.get(
                        (expected_permit.job_id, expected_permit.attempt_id)
                    )
                    != expected_permit
                ):
                    raise ConcurrentControlModificationError(
                        "The provider call permit changed before cancellation could commit"
                    )

            self._jobs[commit.updated.job_id] = commit.updated
            self._events[commit.updated.job_id].append(commit.event)
            self._receipts[receipt_key] = commit.receipt
            if commit.review is not None:
                self._reviews[(commit.updated.job_id, commit.review.review_version)] = commit.review
            if commit.artwork_analysis is not None:
                self._artwork_analyses[
                    (commit.updated.job_id, commit.artwork_analysis.analysis_id)
                ] = commit.artwork_analysis
            if commit.agent_evidence is not None:
                self._agent_evidence[(commit.updated.job_id, commit.agent_evidence.evidence_id)] = (
                    commit.agent_evidence
                )
            if commit.review_decision is not None:
                self._review_decisions[commit.review_decision.decision_id] = commit.review_decision
            if commit.cancellation_decision is not None:
                self._cancellation_decisions[commit.cancellation_decision.decision_id] = (
                    commit.cancellation_decision
                )
            if commit.product_sync is not None:
                self._product_syncs[(commit.updated.job_id, commit.product_sync.sync_id)] = (
                    commit.product_sync
                )
            if commit.provider_upload_attempt is not None:
                self._provider_upload_attempts[
                    (commit.updated.job_id, commit.provider_upload_attempt.attempt_id)
                ] = commit.provider_upload_attempt
            if commit.uploaded_artwork is not None:
                self._uploaded_artwork[
                    (commit.updated.job_id, commit.uploaded_artwork.upload_id)
                ] = commit.uploaded_artwork
            if commit.provider_write_attempt is not None:
                self._provider_write_attempts[
                    (commit.updated.job_id, commit.provider_write_attempt.attempt_id)
                ] = commit.provider_write_attempt
            if commit.provider_call_permit is not None:
                self._provider_call_permits[
                    (commit.updated.job_id, commit.provider_call_permit.attempt_id)
                ] = commit.provider_call_permit
            if commit.provider_call_permit_update is not None:
                _expected_permit, retired_permit = commit.provider_call_permit_update
                self._provider_call_permits[(retired_permit.job_id, retired_permit.attempt_id)] = (
                    retired_permit
                )
            if commit.reconciliation_observation is not None:
                self._reconciliation_observations[
                    (
                        commit.updated.job_id,
                        commit.reconciliation_observation.observation_id,
                    )
                ] = commit.reconciliation_observation
            if commit.upload_reconciliation_observation is not None:
                self._upload_reconciliation_observations[
                    (
                        commit.updated.job_id,
                        commit.upload_reconciliation_observation.observation_id,
                    )
                ] = commit.upload_reconciliation_observation
            if commit.pricing_snapshot is not None:
                self._pricing[(commit.updated.job_id, commit.pricing_snapshot.snapshot_id)] = (
                    commit.pricing_snapshot
                )
            if commit.pricing_evidence is not None:
                self._pricing_evidence[
                    (commit.updated.job_id, commit.pricing_evidence.snapshot_id)
                ] = commit.pricing_evidence
            if commit.failure is not None:
                self._failures[(commit.updated.job_id, commit.failure.failure_id)] = commit.failure
            if commit.work_request is not None:
                self._work[(commit.updated.job_id, commit.work_request.work_request_id)] = (
                    commit.work_request
                )
            if commit.work_update is not None:
                _expected, changed = commit.work_update
                self._work[(changed.job_id, changed.work_request_id)] = changed
            return commit.receipt

    def nudge_pending_work(
        self, job_id: str, work_request_id: str, *, now: datetime
    ) -> WorkRequest:
        with self._lock:
            work = self.get_work_request(job_id, work_request_id)
            if work.status is not WorkRequestStatus.PENDING or work.next_dispatch_at <= now:
                return work
            nudged = revalidate_work_request(
                work,
                next_dispatch_at=now,
                updated_at=now,
            )
            self._work[(job_id, work_request_id)] = nudged
            return nudged

    def list_due_work(self, *, now: datetime, limit: int = 100) -> tuple[WorkRequest, ...]:
        with self._lock:
            due = [
                work
                for work in self._work.values()
                if (work.status is WorkRequestStatus.PENDING and work.next_dispatch_at <= now)
                or (
                    work.status is WorkRequestStatus.CLAIMED
                    and work.lease_expires_at is not None
                    and work.lease_expires_at <= now
                )
            ]
            return tuple(
                sorted(due, key=lambda item: (item.next_dispatch_at, item.work_request_id))[:limit]
            )

    def claim_work(
        self,
        job_id: str,
        work_request_id: str,
        *,
        now: datetime,
        claim_id: str,
        lease_expires_at: datetime,
    ) -> WorkRequest | None:
        with self._lock:
            work = self.get_work_request(job_id, work_request_id)
            claimable = (
                work.status is WorkRequestStatus.PENDING and work.next_dispatch_at <= now
            ) or (
                work.status is WorkRequestStatus.CLAIMED
                and work.lease_expires_at is not None
                and work.lease_expires_at <= now
            )
            if not claimable:
                return None
            claimed = revalidate_work_request(
                work,
                status=WorkRequestStatus.CLAIMED,
                attempt_count=work.attempt_count + 1,
                claim_id=claim_id,
                lease_expires_at=lease_expires_at,
                last_error_code=None,
                updated_at=now,
            )
            self._work[(job_id, work_request_id)] = claimed
            return claimed

    def mark_work_dispatched(
        self,
        job_id: str,
        work_request_id: str,
        *,
        claim_id: str,
        execution_arn: str,
        now: datetime,
    ) -> WorkRequest:
        with self._lock:
            work = self.get_work_request(job_id, work_request_id)
            if work.status is WorkRequestStatus.COMPLETED:
                return work
            if work.status is not WorkRequestStatus.CLAIMED or work.claim_id != claim_id:
                raise ConcurrentControlModificationError("The work claim is no longer current")
            dispatched = revalidate_work_request(
                work,
                status=WorkRequestStatus.DISPATCHED,
                execution_arn=execution_arn,
                claim_id=None,
                lease_expires_at=None,
                updated_at=now,
            )
            self._work[(job_id, work_request_id)] = dispatched
            return dispatched

    def defer_claimed_work(
        self,
        job_id: str,
        work_request_id: str,
        *,
        claim_id: str,
        retry_at: datetime,
        error_code: str,
        now: datetime,
    ) -> WorkRequest:
        with self._lock:
            work = self.get_work_request(job_id, work_request_id)
            if work.status is WorkRequestStatus.COMPLETED:
                return work
            if work.status is not WorkRequestStatus.CLAIMED or work.claim_id != claim_id:
                raise ConcurrentControlModificationError("The work claim is no longer current")
            deferred = revalidate_work_request(
                work,
                lease_expires_at=retry_at,
                last_error_code=error_code,
                updated_at=now,
            )
            self._work[(job_id, work_request_id)] = deferred
            return deferred

    def release_work(
        self,
        job_id: str,
        work_request_id: str,
        *,
        claim_id: str,
        next_dispatch_at: datetime,
        error_code: str,
        now: datetime,
    ) -> WorkRequest:
        with self._lock:
            work = self.get_work_request(job_id, work_request_id)
            if work.status is not WorkRequestStatus.CLAIMED or work.claim_id != claim_id:
                raise ConcurrentControlModificationError("The work claim is no longer current")
            released = revalidate_work_request(
                work,
                status=WorkRequestStatus.PENDING,
                claim_id=None,
                lease_expires_at=None,
                next_dispatch_at=next_dispatch_at,
                last_error_code=error_code,
                updated_at=now,
            )
            self._work[(job_id, work_request_id)] = released
            return released

    def list_reviews(self, job_id: str) -> tuple[ReviewContent, ...]:
        self.get_job(job_id)
        return tuple(
            review
            for (stored_job_id, _version), review in sorted(self._reviews.items())
            if stored_job_id == job_id
        )

    def list_review_decisions(self, job_id: str) -> tuple[ReviewDecisionRecord, ...]:
        self.get_job(job_id)
        return tuple(item for item in self._review_decisions.values() if item.job_id == job_id)

    def list_cancellation_decisions(self, job_id: str) -> tuple[CancellationDecisionRecord, ...]:
        self.get_job(job_id)
        return tuple(
            item for item in self._cancellation_decisions.values() if item.job_id == job_id
        )

    def list_failures(self, job_id: str) -> tuple[FailureRecord, ...]:
        self.get_job(job_id)
        return tuple(
            item
            for (stored_job_id, _key), item in self._failures.items()
            if stored_job_id == job_id
        )

    def list_work_requests(self, job_id: str) -> tuple[WorkRequest, ...]:
        self.get_job(job_id)
        return tuple(
            item for (stored_job_id, _key), item in self._work.items() if stored_job_id == job_id
        )

    def list_events(self, job_id: str) -> tuple[DomainEvent, ...]:
        self.get_job(job_id)
        return tuple(self._events[job_id])

    @staticmethod
    def _receipt_key(receipt: CommandReceipt) -> tuple[str, str, str, str]:
        return (
            receipt.owner_id,
            receipt.command_type,
            receipt.job_id,
            receipt.idempotency_key_digest,
        )

    @staticmethod
    def _upload_receipt_key(receipt: UploadReceipt) -> tuple[str, str, str, str]:
        return (
            receipt.owner_id,
            receipt.command_type.value,
            receipt.upload_id,
            receipt.idempotency_key_digest,
        )
