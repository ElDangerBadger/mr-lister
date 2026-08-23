"""Application-owned direct-upload intake and atomic job-creation boundary."""

from __future__ import annotations

from base64 import b64encode
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from hashlib import sha256
from typing import Protocol

from mr_lister.control.dispatch import deterministic_execution_name, work_input_fingerprint
from mr_lister.control.errors import (
    ConcurrentControlModificationError,
    IdempotencyConflictError,
    InvalidControlStateError,
    NotFoundError,
)
from mr_lister.control.fingerprints import (
    canonical_fingerprint,
    command_request_fingerprint,
    idempotency_key_digest,
)
from mr_lister.control.models import (
    ControlJobRecord,
    ControlJobState,
    DomainEvent,
    SourceArtifactRecord,
    WorkRequest,
    WorkType,
)
from mr_lister.control.source_artwork import (
    Phase6SourceArtworkError,
    source_artifact_fingerprint,
    verify_phase6_source_artwork,
)
from mr_lister.control.store import SellerControlStore
from mr_lister.control.upload_models import (
    UPLOAD_AUTHORIZATION_TTL,
    UPLOAD_INTENT_TTL,
    UploadAuthorization,
    UploadCommandType,
    UploadCompletionCommit,
    UploadIntent,
    UploadIntentCommit,
    UploadIntentStatus,
    UploadReceipt,
)
from mr_lister.review_profile import ExactReviewProductProfile


class UploadExpiredError(Exception):
    code = "UPLOAD_EXPIRED"


class UploadArtifactIntegrityError(Exception):
    code = "ARTIFACT_INTEGRITY"


class UploadDependencyUnavailableError(Exception):
    code = "UPLOAD_UNAVAILABLE"


@dataclass(frozen=True, slots=True)
class CurrentUploadObject:
    """Bounded current-object evidence returned by the private artifact adapter."""

    version_id: str
    content: bytes = field(repr=False)
    content_length: int
    content_type: str
    checksum_sha256_base64: str
    server_side_encryption: str


class UploadArtifactPort(Protocol):
    def issue_authorization(
        self,
        intent: UploadIntent,
        *,
        now: datetime,
    ) -> UploadAuthorization: ...

    def read_current_object(self, intent: UploadIntent) -> CurrentUploadObject: ...

    def pin_object_version(self, intent: UploadIntent, version_id: str) -> None: ...

    def release_unreferenced_version(self, intent: UploadIntent, version_id: str) -> None: ...


class UploadProfileAuthority(Protocol):
    def get_exact(self, *, profile_id: str, profile_version: int) -> ExactReviewProductProfile: ...


@dataclass(frozen=True, slots=True)
class UploadIntakeResult:
    receipt: UploadReceipt
    authorization: UploadAuthorization | None = None


class UploadIntakeService:
    """Own upload intent transitions and create a preparation job in one store transaction."""

    def __init__(
        self,
        *,
        store: SellerControlStore,
        artifacts: UploadArtifactPort,
        profiles: UploadProfileAuthority,
        artifact_bucket: str,
        profile_id: str,
        profile_version: int,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not artifact_bucket or not artifact_bucket.isascii() or "/" in artifact_bucket:
            raise ValueError("Artifact bucket configuration is invalid")
        self._store = store
        self._artifacts = artifacts
        self._profiles = profiles
        self._bucket = artifact_bucket
        self._profile_id = profile_id
        self._profile_version = profile_version
        self._clock = clock or (lambda: datetime.now(UTC))

    def get_upload(self, *, owner_id: str, upload_id: str) -> UploadIntent:
        """Return one durable owner-scoped intent for browser reload recovery."""

        try:
            return self._store.get_upload_intent_for_owner(owner_id, upload_id)
        except NotFoundError:
            raise
        except Exception:
            raise UploadDependencyUnavailableError from None

    def create_upload(
        self,
        *,
        owner_id: str,
        idempotency_key: str,
        filename: str,
        content_type: str,
        content_sha256: str,
        size_bytes: int,
    ) -> UploadIntakeResult:
        command_type = UploadCommandType.CREATE_UPLOAD
        key_digest = idempotency_key_digest(idempotency_key)
        upload_id = self._stable_id("upload", owner_id, key_digest)
        job_id = self._stable_id("job", owner_id, key_digest)
        request_fingerprint = command_request_fingerprint(
            command_type=command_type.value,
            payload={
                "owner_id": owner_id,
                "upload_id": upload_id,
                "job_id": job_id,
                "filename": filename,
                "content_type": content_type,
                "content_sha256": content_sha256,
                "size_bytes": size_bytes,
            },
        )
        replay = self._resolve_replay(
            owner_id=owner_id,
            command_type=command_type,
            upload_id=upload_id,
            key_digest=key_digest,
            request_fingerprint=request_fingerprint,
        )
        if replay is not None:
            intent = self._store.get_upload_intent_for_owner(owner_id, upload_id)
            return UploadIntakeResult(
                receipt=replay,
                authorization=self._replay_authorization(intent),
            )

        now = self._now()
        exact_profile = self._exact_profile()
        intent = UploadIntent(
            owner_id=owner_id,
            upload_id=upload_id,
            job_id=job_id,
            filename=filename,
            content_type=content_type,
            content_sha256=content_sha256,
            size_bytes=size_bytes,
            bucket=self._bucket,
            object_key=f"private/owners/{owner_id}/jobs/{job_id}/source/source.png",
            product_profile_id=exact_profile.profile.profile_id,
            product_profile_version=exact_profile.profile.profile_version,
            product_profile_fingerprint=exact_profile.fingerprint,
            authorization_generation=1,
            authorization_issued_at=now,
            authorization_expires_at=now + UPLOAD_AUTHORIZATION_TTL,
            intent_expires_at=now + UPLOAD_INTENT_TTL,
            created_at=now,
            updated_at=now,
        )
        receipt = self._receipt(
            command_type=command_type,
            intent=intent,
            key_digest=key_digest,
            request_fingerprint=request_fingerprint,
            now=now,
        )
        persisted = self._store.commit_upload_intent(
            UploadIntentCommit(updated=intent, receipt=receipt)
        )
        authoritative = self._authoritative_intent(owner_id, upload_id)
        return UploadIntakeResult(
            receipt=persisted,
            authorization=self._replay_authorization(authoritative),
        )

    def authorize_upload(
        self,
        *,
        owner_id: str,
        upload_id: str,
        idempotency_key: str,
    ) -> UploadIntakeResult:
        command_type = UploadCommandType.REAUTHORIZE_UPLOAD
        key_digest = idempotency_key_digest(idempotency_key)
        request_fingerprint = command_request_fingerprint(
            command_type=command_type.value,
            payload={"owner_id": owner_id, "upload_id": upload_id},
        )
        replay = self._resolve_replay(
            owner_id=owner_id,
            command_type=command_type,
            upload_id=upload_id,
            key_digest=key_digest,
            request_fingerprint=request_fingerprint,
        )
        if replay is not None:
            intent = self._store.get_upload_intent_for_owner(owner_id, upload_id)
            return UploadIntakeResult(
                receipt=replay,
                authorization=self._replay_authorization(intent),
            )

        current = self._store.get_upload_intent_for_owner(owner_id, upload_id)
        now = self._now()
        self._require_open(current, now)
        if current.authorization_expires_at is not None and current.authorization_expires_at > now:
            raise InvalidControlStateError("The current upload authorization is still active")
        updated = UploadIntent.model_validate(
            {
                **current.model_dump(mode="python"),
                "record_version": current.record_version + 1,
                "authorization_generation": current.authorization_generation + 1,
                "authorization_issued_at": now,
                "authorization_expires_at": min(
                    now + UPLOAD_AUTHORIZATION_TTL,
                    current.intent_expires_at,
                ),
                "updated_at": now,
            }
        )
        receipt = self._receipt(
            command_type=command_type,
            intent=updated,
            key_digest=key_digest,
            request_fingerprint=request_fingerprint,
            now=now,
        )
        persisted = self._store.commit_upload_intent(
            UploadIntentCommit(current=current, updated=updated, receipt=receipt)
        )
        authoritative = self._authoritative_intent(owner_id, upload_id)
        return UploadIntakeResult(
            receipt=persisted,
            authorization=self._replay_authorization(authoritative),
        )

    def complete_upload(
        self,
        *,
        owner_id: str,
        upload_id: str,
        idempotency_key: str,
    ) -> UploadIntakeResult:
        command_type = UploadCommandType.COMPLETE_UPLOAD
        key_digest = idempotency_key_digest(idempotency_key)
        request_fingerprint = command_request_fingerprint(
            command_type=command_type.value,
            payload={"owner_id": owner_id, "upload_id": upload_id},
        )
        replay = self._resolve_replay(
            owner_id=owner_id,
            command_type=command_type,
            upload_id=upload_id,
            key_digest=key_digest,
            request_fingerprint=request_fingerprint,
        )
        if replay is not None:
            return UploadIntakeResult(receipt=replay)

        current = self._store.get_upload_intent_for_owner(owner_id, upload_id)
        now = self._now()
        self._require_open(current, now)
        exact_profile = self._exact_profile()
        if (
            current.product_profile_id != exact_profile.profile.profile_id
            or current.product_profile_version != exact_profile.profile.profile_version
            or current.product_profile_fingerprint != exact_profile.fingerprint
        ):
            raise UploadArtifactIntegrityError from None
        try:
            observed = self._artifacts.read_current_object(current)
        except Exception:
            raise UploadDependencyUnavailableError from None
        self._verify_object(current, observed)
        source = self._source(current, observed, now)
        work_id = self._stable_id("work", owner_id, current.job_id)
        receipt = UploadReceipt(
            receipt_id=self._stable_id("receipt_complete", owner_id, f"{upload_id}:{key_digest}"),
            owner_id=owner_id,
            upload_id=upload_id,
            job_id=current.job_id,
            command_type=command_type,
            idempotency_key_digest=key_digest,
            request_fingerprint=request_fingerprint,
            status=UploadIntentStatus.COMPLETED,
            record_version=current.record_version + 1,
            work_request_id=work_id,
            created_at=now,
        )
        completed = UploadIntent.model_validate(
            {
                **current.model_dump(mode="python"),
                "record_version": current.record_version + 1,
                "status": UploadIntentStatus.COMPLETED,
                "completed_at": now,
                "completed_source_artifact_fingerprint": source.fingerprint,
                "completed_version_id": source.version_id,
                "completion_receipt_id": receipt.receipt_id,
                "updated_at": now,
            }
        )
        work = WorkRequest(
            work_request_id=work_id,
            owner_id=owner_id,
            job_id=current.job_id,
            receipt_id=receipt.receipt_id,
            work_type=WorkType.PREPARE,
            input_fingerprint=work_input_fingerprint(
                work_type=WorkType.PREPARE,
                job_id=current.job_id,
                work_request_id=work_id,
            ),
            execution_name=deterministic_execution_name(work_id),
            next_dispatch_at=now,
            created_at=now,
            updated_at=now,
        )
        job = ControlJobRecord(
            owner_id=owner_id,
            job_id=current.job_id,
            state=ControlJobState.INTAKE_VALIDATED,
            event_sequence=1,
            source_artifact_fingerprint=source.fingerprint,
            active_work_request_id=work_id,
            created_at=now,
            updated_at=now,
        )
        event = DomainEvent(
            job_id=current.job_id,
            sequence=1,
            name="UPLOAD_COMPLETED",
            occurred_at=now,
        )
        commit = UploadCompletionCommit(
            intent=UploadIntentCommit(current=current, updated=completed, receipt=receipt),
            job=job,
            source_artifact=source,
            event=event,
            work_request=work,
        )
        try:
            self._artifacts.pin_object_version(current, observed.version_id)
        except Exception:
            raise UploadDependencyUnavailableError from None
        try:
            persisted = self._store.complete_upload(commit)
        except (ConcurrentControlModificationError, IdempotencyConflictError):
            self._release_if_unreferenced(current, observed.version_id)
            raise
        except Exception:
            # Once the object version is pinned, every failed persistence path must attempt the
            # same authority-aware compensation.  An unclassified adapter failure is not safe to
            # expose and may represent an ambiguous transaction outcome, so report only the
            # stable dependency error after checking whether durable authority references it.
            self._release_if_unreferenced(current, observed.version_id)
            raise UploadDependencyUnavailableError from None
        # A same-key replay can resolve to a concurrent winner's receipt instead of raising.  Read
        # durable authority after every return so a differently observed version cannot remain
        # falsely pinned.
        self._release_if_unreferenced(current, observed.version_id)
        return UploadIntakeResult(receipt=persisted)

    def cancel_upload(
        self,
        *,
        owner_id: str,
        upload_id: str,
        idempotency_key: str,
    ) -> UploadIntakeResult:
        command_type = UploadCommandType.CANCEL_UPLOAD
        key_digest = idempotency_key_digest(idempotency_key)
        request_fingerprint = command_request_fingerprint(
            command_type=command_type.value,
            payload={"owner_id": owner_id, "upload_id": upload_id},
        )
        replay = self._resolve_replay(
            owner_id=owner_id,
            command_type=command_type,
            upload_id=upload_id,
            key_digest=key_digest,
            request_fingerprint=request_fingerprint,
        )
        if replay is not None:
            return UploadIntakeResult(receipt=replay)
        current = self._store.get_upload_intent_for_owner(owner_id, upload_id)
        now = self._now()
        self._require_open(current, now)
        updated = UploadIntent.model_validate(
            {
                **current.model_dump(mode="python"),
                "record_version": current.record_version + 1,
                "status": UploadIntentStatus.CANCELLED,
                "cancelled_at": now,
                "cancellation_receipt_id": self._stable_id(
                    "receipt_cancel", owner_id, f"{upload_id}:{key_digest}"
                ),
                "updated_at": now,
            }
        )
        receipt = UploadReceipt(
            receipt_id=updated.cancellation_receipt_id,
            owner_id=owner_id,
            upload_id=upload_id,
            job_id=current.job_id,
            command_type=command_type,
            idempotency_key_digest=key_digest,
            request_fingerprint=request_fingerprint,
            status=UploadIntentStatus.CANCELLED,
            record_version=updated.record_version,
            created_at=now,
        )
        persisted = self._store.commit_upload_intent(
            UploadIntentCommit(current=current, updated=updated, receipt=receipt)
        )
        return UploadIntakeResult(receipt=persisted)

    def _issue(self, intent: UploadIntent, *, now: datetime) -> UploadAuthorization:
        if intent.status is not UploadIntentStatus.OPEN:
            raise InvalidControlStateError("A terminal upload cannot receive authorization")
        try:
            authorization = self._artifacts.issue_authorization(intent, now=now)
        except Exception:
            raise UploadDependencyUnavailableError from None
        if (
            authorization.owner_id != intent.owner_id
            or authorization.upload_id != intent.upload_id
            or authorization.job_id != intent.job_id
            or authorization.authorization_generation != intent.authorization_generation
            or authorization.content_sha256 != intent.content_sha256
            or authorization.size_bytes != intent.size_bytes
            or authorization.issued_at != now
            or authorization.expires_at != intent.authorization_expires_at
            or authorization.form_fields.get("key") != intent.object_key
        ):
            raise UploadDependencyUnavailableError
        return authorization

    def _replay_authorization(self, intent: UploadIntent) -> UploadAuthorization | None:
        now = self._now()
        if (
            intent.status is not UploadIntentStatus.OPEN
            or now >= intent.intent_expires_at
            or intent.authorization_expires_at is None
            or now >= intent.authorization_expires_at
        ):
            return None
        if (intent.authorization_expires_at - now).total_seconds() < 1:
            return None
        return self._issue(intent, now=now)

    def _authoritative_intent(self, owner_id: str, upload_id: str) -> UploadIntent:
        try:
            return self._store.get_upload_intent_for_owner(owner_id, upload_id)
        except Exception:
            # A successful commit followed by an unavailable strong read is safely retried through
            # the persisted receipt.  Never issue a form from the pre-commit candidate instead.
            raise UploadDependencyUnavailableError from None

    def _release_if_unreferenced(self, intent: UploadIntent, version_id: str) -> None:
        try:
            latest = self._store.get_upload_intent_for_owner(intent.owner_id, intent.upload_id)
        except Exception:
            return
        if (
            latest.status is UploadIntentStatus.COMPLETED
            and latest.completed_version_id == version_id
        ):
            self._reassert_pinned(intent, version_id)
            return
        try:
            self._artifacts.release_unreferenced_version(intent, version_id)
        except Exception:
            # The reference-aware cleanup sweep remains the fail-safe for a failed compensation.
            return
        # A completion can commit the same version between the strong authority read above and the
        # compensating tag write.  Read once more and let durable authority win by restoring the
        # pinned tag.  If it commits after this read, its own successful return takes the branch
        # above and performs the same reassertion.
        try:
            latest = self._store.get_upload_intent_for_owner(intent.owner_id, intent.upload_id)
        except Exception:
            return
        if (
            latest.status is UploadIntentStatus.COMPLETED
            and latest.completed_version_id == version_id
        ):
            self._reassert_pinned(intent, version_id)

    def _reassert_pinned(self, intent: UploadIntent, version_id: str) -> None:
        try:
            self._artifacts.pin_object_version(intent, version_id)
        except Exception:
            # The reference-aware cleanup sweep must preserve any durably referenced version when
            # S3 is unavailable during this best-effort tag repair.
            return

    def _exact_profile(self) -> ExactReviewProductProfile:
        try:
            exact = self._profiles.get_exact(
                profile_id=self._profile_id,
                profile_version=self._profile_version,
            )
        except Exception:
            raise UploadDependencyUnavailableError from None
        if (
            exact.profile.profile_id != self._profile_id
            or exact.profile.profile_version != self._profile_version
            or exact.fingerprint != canonical_fingerprint(exact.profile)
        ):
            raise UploadDependencyUnavailableError
        return exact

    @staticmethod
    def _source(
        intent: UploadIntent,
        observed: CurrentUploadObject,
        now: datetime,
    ) -> SourceArtifactRecord:
        material = {
            "job_id": intent.job_id,
            "owner_id": intent.owner_id,
            "bucket": intent.bucket,
            "object_key": intent.object_key,
            "version_id": observed.version_id,
            "content_sha256": intent.content_sha256,
            "size_bytes": intent.size_bytes,
            "media_type": intent.content_type,
            "product_profile_id": intent.product_profile_id,
            "product_profile_version": intent.product_profile_version,
            "product_profile_fingerprint": intent.product_profile_fingerprint,
            "created_at": now,
        }
        return SourceArtifactRecord(
            fingerprint=source_artifact_fingerprint(**material),
            **material,
        )

    @staticmethod
    def _verify_object(intent: UploadIntent, observed: CurrentUploadObject) -> None:
        expected_checksum = b64encode(bytes.fromhex(intent.content_sha256)).decode("ascii")
        if (
            not isinstance(observed.version_id, str)
            or not observed.version_id
            or observed.version_id == "null"
            or not observed.version_id.isascii()
            or isinstance(observed.content_length, bool)
            or not isinstance(observed.content_length, int)
            or not isinstance(observed.content, bytes)
            or not isinstance(observed.content_type, str)
            or not isinstance(observed.checksum_sha256_base64, str)
            or not isinstance(observed.server_side_encryption, str)
            or observed.content_length != intent.size_bytes
            or observed.content_type != intent.content_type
            or observed.checksum_sha256_base64 != expected_checksum
            or observed.server_side_encryption != "AES256"
            or len(observed.content) != observed.content_length
        ):
            raise UploadArtifactIntegrityError
        try:
            verify_phase6_source_artwork(
                filename=intent.filename,
                content_type=intent.content_type,
                content=observed.content,
                expected_sha256=intent.content_sha256,
                expected_size_bytes=intent.size_bytes,
            )
        except Phase6SourceArtworkError:
            raise UploadArtifactIntegrityError from None

    def _resolve_replay(
        self,
        *,
        owner_id: str,
        command_type: UploadCommandType,
        upload_id: str,
        key_digest: str,
        request_fingerprint: str,
    ) -> UploadReceipt | None:
        existing = self._store.resolve_upload_receipt(
            owner_id,
            command_type.value,
            upload_id,
            key_digest,
        )
        if existing is None:
            return None
        if existing.request_fingerprint != request_fingerprint:
            raise IdempotencyConflictError(
                "The idempotency key was already used for another upload request"
            )
        return existing

    @staticmethod
    def _require_open(intent: UploadIntent, now: datetime) -> None:
        if intent.status is not UploadIntentStatus.OPEN:
            raise InvalidControlStateError("The upload intent is no longer open")
        if now >= intent.intent_expires_at:
            raise UploadExpiredError

    @staticmethod
    def _stable_id(prefix: str, owner_id: str, material: str) -> str:
        digest = sha256(f"{prefix}\0{owner_id}\0{material}".encode()).hexdigest()[:32]
        return f"{prefix}_{digest}"

    def _receipt(
        self,
        *,
        command_type: UploadCommandType,
        intent: UploadIntent,
        key_digest: str,
        request_fingerprint: str,
        now: datetime,
    ) -> UploadReceipt:
        return UploadReceipt(
            receipt_id=self._stable_id(
                f"receipt_{command_type.value}",
                intent.owner_id,
                f"{intent.upload_id}:{key_digest}",
            ),
            owner_id=intent.owner_id,
            upload_id=intent.upload_id,
            job_id=intent.job_id,
            command_type=command_type,
            idempotency_key_digest=key_digest,
            request_fingerprint=request_fingerprint,
            status=intent.status,
            record_version=intent.record_version,
            created_at=now,
        )

    def _now(self) -> datetime:
        now = self._clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("Upload clock must return a timezone-aware timestamp")
        return now


__all__ = [
    "CurrentUploadObject",
    "UploadArtifactIntegrityError",
    "UploadArtifactPort",
    "UploadDependencyUnavailableError",
    "UploadExpiredError",
    "UploadIntakeResult",
    "UploadIntakeService",
]
