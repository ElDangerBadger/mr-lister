"""Strict direct-upload records for the Phase 6 owner boundary.

These records contain durable authority only.  The short-lived S3 form is a
separate response object and therefore cannot accidentally enter an intent or
idempotency receipt.
"""

from __future__ import annotations

import re
from base64 import b64encode
from datetime import datetime, timedelta
from enum import StrEnum

from pydantic import Field, model_validator

from mr_lister.control.dispatch import deterministic_execution_name, work_input_fingerprint
from mr_lister.control.models import (
    PHASE6_MAX_SOURCE_ARTWORK_BYTES,
    ControlJobRecord,
    ControlJobState,
    ControlModel,
    DomainEvent,
    Fingerprint,
    NonEmptyText,
    OwnerId,
    SafeId,
    SourceArtifactRecord,
    WorkRequest,
    WorkRequestStatus,
    WorkType,
)
from mr_lister.control.source_artwork import validate_source_artifact_authority

UPLOAD_AUTHORIZATION_TTL = timedelta(minutes=5)
UPLOAD_INTENT_TTL = timedelta(days=1)
_DNS_LABEL = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
_S3_POST_URL = re.compile(
    rf"https://(?:(?:{_DNS_LABEL}\.)+)?s3(?:\.[a-z0-9-]+)?"
    r"\.amazonaws\.com(?:\.cn)?/"
)


class UploadIntentStatus(StrEnum):
    OPEN = "open"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    EXPIRED = "expired"


class UploadCommandType(StrEnum):
    CREATE_UPLOAD = "create_upload"
    REAUTHORIZE_UPLOAD = "reauthorize_upload"
    COMPLETE_UPLOAD = "complete_upload"
    CANCEL_UPLOAD = "cancel_upload"


class UploadIntent(ControlModel):
    """Durable owner-scoped reservation for one exact direct-upload object."""

    # Keep owner identity top-level and early so adapters can reject a caller
    # before parsing the rest of an untrusted/corrupt payload.
    owner_id: OwnerId
    upload_id: SafeId
    job_id: SafeId
    record_version: int = Field(default=0, ge=0)
    status: UploadIntentStatus = UploadIntentStatus.OPEN
    filename: str = Field(min_length=1, max_length=255)
    content_type: str = Field(pattern=r"^image/png$")
    content_sha256: Fingerprint
    size_bytes: int = Field(gt=0, le=PHASE6_MAX_SOURCE_ARTWORK_BYTES)
    bucket: NonEmptyText
    object_key: NonEmptyText
    product_profile_id: SafeId
    product_profile_version: int = Field(ge=1)
    product_profile_fingerprint: Fingerprint
    authorization_generation: int = Field(default=0, ge=0)
    authorization_issued_at: datetime | None = None
    authorization_expires_at: datetime | None = None
    intent_expires_at: datetime
    completed_at: datetime | None = None
    completed_source_artifact_fingerprint: Fingerprint | None = None
    completed_version_id: NonEmptyText | None = None
    completion_receipt_id: SafeId | None = None
    cancelled_at: datetime | None = None
    cancellation_receipt_id: SafeId | None = None
    expired_at: datetime | None = None
    created_at: datetime
    updated_at: datetime

    @model_validator(mode="after")
    def durable_authority_is_coherent(self) -> UploadIntent:
        timestamps = (
            self.created_at,
            self.updated_at,
            self.intent_expires_at,
            self.authorization_issued_at,
            self.authorization_expires_at,
            self.completed_at,
            self.cancelled_at,
            self.expired_at,
        )
        if any(value is not None and value.utcoffset() is None for value in timestamps):
            raise ValueError("Upload intent timestamps must be timezone-aware")
        if self.updated_at < self.created_at:
            raise ValueError("Upload intent time cannot move backwards")
        if not self.created_at < self.intent_expires_at <= self.created_at + UPLOAD_INTENT_TTL:
            raise ValueError("Upload intent expiry must be within one day of creation")
        if "/" in self.filename or "\\" in self.filename:
            raise ValueError("Upload filename must be a basename")
        if self.filename != self.filename.strip() or not self.filename.casefold().endswith(".png"):
            raise ValueError("Upload filename must be a PNG basename")
        if any(ord(character) < 32 or ord(character) == 127 for character in self.filename):
            raise ValueError("Upload filename contains control characters")
        expected_key = f"private/owners/{self.owner_id}/jobs/{self.job_id}/source/source.png"
        if self.object_key != expected_key:
            raise ValueError("Upload key must bind the exact owner and reserved job")

        authorization_times = (
            self.authorization_issued_at,
            self.authorization_expires_at,
        )
        if self.authorization_generation == 0:
            if any(value is not None for value in authorization_times):
                raise ValueError("An unissued upload cannot retain authorization time")
        elif not all(value is not None for value in authorization_times):
            raise ValueError("Issued upload authorization requires both timestamps")
        else:
            assert self.authorization_issued_at is not None
            assert self.authorization_expires_at is not None
            if not self.created_at <= self.authorization_issued_at <= self.updated_at:
                raise ValueError("Upload authorization time is outside its intent history")
            if not (
                self.authorization_issued_at
                < self.authorization_expires_at
                <= self.authorization_issued_at + UPLOAD_AUTHORIZATION_TTL
            ):
                raise ValueError("Upload authorization cannot exceed five minutes")
            if self.authorization_expires_at > self.intent_expires_at:
                raise ValueError("Upload authorization cannot outlive its intent")

        completed = (
            self.completed_at,
            self.completed_source_artifact_fingerprint,
            self.completed_version_id,
            self.completion_receipt_id,
        )
        cancelled = (self.cancelled_at, self.cancellation_receipt_id)
        if self.status is UploadIntentStatus.OPEN:
            if any(value is not None for value in (*completed, *cancelled, self.expired_at)):
                raise ValueError("An open upload cannot retain a terminal result")
        elif self.status is UploadIntentStatus.COMPLETED:
            if not all(value is not None for value in completed) or any(
                value is not None for value in (*cancelled, self.expired_at)
            ):
                raise ValueError("A completed upload requires one exclusive pinned result")
            if self.completed_at != self.updated_at:
                raise ValueError("Upload completion time must match its record update")
        elif self.status is UploadIntentStatus.CANCELLED:
            if not all(value is not None for value in cancelled) or any(
                value is not None for value in (*completed, self.expired_at)
            ):
                raise ValueError("A cancelled upload requires one exclusive cancellation")
            if self.cancelled_at != self.updated_at:
                raise ValueError("Upload cancellation time must match its record update")
        elif self.status is UploadIntentStatus.EXPIRED:
            if self.expired_at is None or any(
                value is not None for value in (*completed, *cancelled)
            ):
                raise ValueError("An expired upload requires one exclusive expiry")
            if self.expired_at != self.updated_at:
                raise ValueError("Upload expiry time must match its record update")
        return self


class UploadReceipt(ControlModel):
    """Stable idempotency result; it intentionally cannot contain a presigned form."""

    receipt_id: SafeId
    owner_id: OwnerId
    upload_id: SafeId
    job_id: SafeId
    command_type: UploadCommandType
    idempotency_key_digest: Fingerprint
    request_fingerprint: Fingerprint
    status: UploadIntentStatus
    record_version: int = Field(ge=0)
    work_request_id: SafeId | None = None
    created_at: datetime

    @model_validator(mode="after")
    def response_matches_command(self) -> UploadReceipt:
        if self.created_at.utcoffset() is None:
            raise ValueError("Upload receipt timestamp must be timezone-aware")
        expected_status = {
            UploadCommandType.CREATE_UPLOAD: UploadIntentStatus.OPEN,
            UploadCommandType.REAUTHORIZE_UPLOAD: UploadIntentStatus.OPEN,
            UploadCommandType.COMPLETE_UPLOAD: UploadIntentStatus.COMPLETED,
            UploadCommandType.CANCEL_UPLOAD: UploadIntentStatus.CANCELLED,
        }[self.command_type]
        if self.status is not expected_status:
            raise ValueError("Upload receipt status does not match its command")
        if (self.command_type is UploadCommandType.COMPLETE_UPLOAD) != (
            self.work_request_id is not None
        ):
            raise ValueError("Only upload completion carries preparation work")
        return self


class UploadAuthorization(ControlModel):
    """One ephemeral exact-object S3 form returned only by create/authorize."""

    owner_id: OwnerId = Field(repr=False, exclude=True)
    upload_id: SafeId
    job_id: SafeId
    authorization_generation: int = Field(ge=1)
    method: str = Field(default="POST", pattern=r"^POST$")
    url: str = Field(min_length=1, max_length=2_048, repr=False)
    form_fields: dict[str, str] = Field(min_length=1, max_length=64, repr=False)
    content_sha256: Fingerprint
    size_bytes: int = Field(gt=0, le=PHASE6_MAX_SOURCE_ARTWORK_BYTES)
    issued_at: datetime
    expires_at: datetime

    @model_validator(mode="after")
    def form_is_exact_and_short_lived(self) -> UploadAuthorization:
        if self.issued_at.utcoffset() is None or self.expires_at.utcoffset() is None:
            raise ValueError("Upload authorization timestamps must be timezone-aware")
        if not self.issued_at < self.expires_at <= self.issued_at + UPLOAD_AUTHORIZATION_TTL:
            raise ValueError("Upload authorization cannot exceed five minutes")
        if not self.url.isascii() or _S3_POST_URL.fullmatch(self.url) is None:
            raise ValueError("Upload authorization target must be an exact HTTPS S3 origin")
        if any(
            not isinstance(key, str)
            or not isinstance(value, str)
            or not key
            or len(key) > 128
            or not value
            or len(value) > 16_384
            for key, value in self.form_fields.items()
        ):
            raise ValueError("Upload authorization form fields are invalid")
        expected_checksum = b64encode(bytes.fromhex(self.content_sha256)).decode("ascii")
        expected_fields = {
            "Content-Type": "image/png",
            "x-amz-checksum-algorithm": "SHA256",
            "x-amz-checksum-sha256": expected_checksum,
            "x-amz-server-side-encryption": "AES256",
            "x-amz-tagging": "mr-lister-state=staged",
        }
        if any(self.form_fields.get(key) != value for key, value in expected_fields.items()):
            raise ValueError("Upload authorization does not bind the required object fields")
        signing_fields = {
            "x-amz-algorithm",
            "x-amz-credential",
            "x-amz-date",
            "policy",
            "x-amz-signature",
        }
        allowed_fields = {
            "key",
            *expected_fields,
            *signing_fields,
            "x-amz-security-token",
        }
        if not signing_fields.issubset(self.form_fields) or not set(self.form_fields).issubset(
            allowed_fields
        ):
            raise ValueError("Upload authorization contains unsupported form fields")
        key = self.form_fields.get("key")
        if (
            self.form_fields_owner != self.owner_id
            or key != f"private/owners/{self.owner_id}/jobs/{self.job_id}/source/source.png"
        ):
            raise ValueError("Upload authorization does not bind its reserved job key")
        return self

    @property
    def form_fields_owner(self) -> str:
        """Extract the owner segment without returning it in the public response."""

        key = self.form_fields.get("key", "")
        parts = key.split("/")
        if len(parts) == 7 and parts[:2] == ["private", "owners"] and parts[3] == "jobs":
            return parts[2]
        return ""


class UploadIntentCommit(ControlModel):
    """One conditional intent mutation plus its stable idempotency result."""

    current: UploadIntent | None = None
    updated: UploadIntent
    receipt: UploadReceipt

    @model_validator(mode="after")
    def transition_is_closed(self) -> UploadIntentCommit:
        if (
            self.receipt.owner_id != self.updated.owner_id
            or self.receipt.upload_id != self.updated.upload_id
            or self.receipt.job_id != self.updated.job_id
            or self.receipt.status is not self.updated.status
            or self.receipt.record_version != self.updated.record_version
            or self.receipt.created_at != self.updated.updated_at
        ):
            raise ValueError("Upload receipt does not match the committed intent")
        if self.current is None:
            if (
                self.receipt.command_type is not UploadCommandType.CREATE_UPLOAD
                or self.updated.status is not UploadIntentStatus.OPEN
                or self.updated.record_version != 0
                or self.updated.authorization_generation != 1
                or self.updated.created_at != self.updated.updated_at
                or self.updated.authorization_issued_at != self.updated.created_at
            ):
                raise ValueError("Upload creation must establish one pristine authorized intent")
            return self

        current = self.current
        updated = self.updated
        immutable_fields = (
            "contract_version",
            "owner_id",
            "upload_id",
            "job_id",
            "filename",
            "content_type",
            "content_sha256",
            "size_bytes",
            "bucket",
            "object_key",
            "product_profile_id",
            "product_profile_version",
            "product_profile_fingerprint",
            "intent_expires_at",
            "created_at",
        )
        if any(getattr(current, field) != getattr(updated, field) for field in immutable_fields):
            raise ValueError("Upload intent authority is immutable")
        if current.status is not UploadIntentStatus.OPEN:
            raise ValueError("A terminal upload intent cannot transition")
        if updated.record_version != current.record_version + 1:
            raise ValueError("Upload intent must increment record_version exactly once")
        if updated.updated_at < current.updated_at:
            raise ValueError("Upload intent time cannot move backwards")
        if updated.updated_at >= current.intent_expires_at:
            raise ValueError("An expired upload intent cannot accept a seller command")

        if self.receipt.command_type is UploadCommandType.REAUTHORIZE_UPLOAD:
            if (
                updated.status is not UploadIntentStatus.OPEN
                or updated.authorization_generation != current.authorization_generation + 1
                or current.authorization_expires_at is None
                or current.authorization_expires_at > updated.updated_at
                or updated.authorization_issued_at != updated.updated_at
                or updated.completed_at is not None
                or updated.cancelled_at is not None
            ):
                raise ValueError("Upload reauthorization changed more than grant authority")
        elif self.receipt.command_type is UploadCommandType.COMPLETE_UPLOAD:
            if (
                updated.status is not UploadIntentStatus.COMPLETED
                or updated.authorization_generation != current.authorization_generation
                or updated.authorization_issued_at != current.authorization_issued_at
                or updated.authorization_expires_at != current.authorization_expires_at
                or updated.completed_at != updated.updated_at
                or updated.completion_receipt_id != self.receipt.receipt_id
            ):
                raise ValueError("Upload completion did not bind one pinned result")
        elif self.receipt.command_type is UploadCommandType.CANCEL_UPLOAD:
            if (
                updated.status is not UploadIntentStatus.CANCELLED
                or updated.authorization_generation != current.authorization_generation
                or updated.authorization_issued_at != current.authorization_issued_at
                or updated.authorization_expires_at != current.authorization_expires_at
                or updated.cancelled_at != updated.updated_at
                or updated.cancellation_receipt_id != self.receipt.receipt_id
            ):
                raise ValueError("Upload cancellation did not establish terminal intent")
        else:
            raise ValueError("Existing upload intent received an invalid command")
        return self


class UploadCompletionCommit(ControlModel):
    """Atomic intent consumption and pristine preparation-job creation DTO."""

    intent: UploadIntentCommit
    job: ControlJobRecord
    source_artifact: SourceArtifactRecord
    event: DomainEvent
    work_request: WorkRequest

    @model_validator(mode="after")
    def completion_authority_is_one_transaction(self) -> UploadCompletionCommit:
        if self.intent.receipt.command_type is not UploadCommandType.COMPLETE_UPLOAD:
            raise ValueError("Upload completion commit requires a completion intent")
        completed = self.intent.updated
        source = validate_source_artifact_authority(self.source_artifact)
        if (
            self.job.owner_id != completed.owner_id
            or self.job.job_id != completed.job_id
            or source.owner_id != completed.owner_id
            or source.job_id != completed.job_id
            or source.bucket != completed.bucket
            or source.object_key != completed.object_key
            or source.content_sha256 != completed.content_sha256
            or source.size_bytes != completed.size_bytes
            or source.product_profile_id != completed.product_profile_id
            or source.product_profile_version != completed.product_profile_version
            or source.product_profile_fingerprint != completed.product_profile_fingerprint
            or source.version_id != completed.completed_version_id
            or source.fingerprint != completed.completed_source_artifact_fingerprint
            or self.job.source_artifact_fingerprint != source.fingerprint
        ):
            raise ValueError("Upload completion changed source or job authority")
        assert completed.completed_at is not None
        expected_job = ControlJobRecord(
            owner_id=completed.owner_id,
            job_id=completed.job_id,
            state=ControlJobState.INTAKE_VALIDATED,
            event_sequence=1,
            source_artifact_fingerprint=source.fingerprint,
            active_work_request_id=self.work_request.work_request_id,
            created_at=completed.completed_at,
            updated_at=completed.completed_at,
        )
        expected_event = DomainEvent(
            job_id=completed.job_id,
            sequence=1,
            name="UPLOAD_COMPLETED",
            occurred_at=completed.completed_at,
        )
        expected_work = WorkRequest(
            work_request_id=self.work_request.work_request_id,
            owner_id=completed.owner_id,
            job_id=completed.job_id,
            receipt_id=self.intent.receipt.receipt_id,
            work_type=WorkType.PREPARE,
            input_fingerprint=work_input_fingerprint(
                work_type=WorkType.PREPARE,
                job_id=completed.job_id,
                work_request_id=self.work_request.work_request_id,
            ),
            execution_name=deterministic_execution_name(self.work_request.work_request_id),
            status=WorkRequestStatus.PENDING,
            next_dispatch_at=completed.completed_at,
            created_at=completed.completed_at,
            updated_at=completed.completed_at,
        )
        if (
            self.job != expected_job
            or self.event != expected_event
            or self.work_request != expected_work
            or self.intent.receipt.work_request_id != self.work_request.work_request_id
            or source.created_at != completed.completed_at
        ):
            raise ValueError("Upload completion did not create pristine preparation work")
        return self


__all__ = [
    "UPLOAD_AUTHORIZATION_TTL",
    "UPLOAD_INTENT_TTL",
    "UploadAuthorization",
    "UploadCommandType",
    "UploadCompletionCommit",
    "UploadIntent",
    "UploadIntentCommit",
    "UploadIntentStatus",
    "UploadReceipt",
]
