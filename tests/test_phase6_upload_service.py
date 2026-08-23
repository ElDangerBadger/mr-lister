from __future__ import annotations

from base64 import b64encode
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from io import BytesIO
from pathlib import Path

import pytest
from PIL import Image

from mr_lister.contracts import ProductProfile
from mr_lister.control.errors import (
    ConcurrentControlModificationError,
    IdempotencyConflictError,
    InvalidControlStateError,
    NotFoundError,
)
from mr_lister.control.fingerprints import canonical_fingerprint
from mr_lister.control.models import ControlJobState, WorkRequestStatus, WorkType
from mr_lister.control.store import InMemorySellerControlStore
from mr_lister.control.upload_models import (
    UPLOAD_AUTHORIZATION_TTL,
    UploadAuthorization,
    UploadCommandType,
    UploadCompletionCommit,
    UploadIntent,
    UploadIntentStatus,
)
from mr_lister.control.upload_service import (
    CurrentUploadObject,
    UploadArtifactIntegrityError,
    UploadDependencyUnavailableError,
    UploadExpiredError,
    UploadIntakeService,
)
from mr_lister.review_profile import ExactReviewProductProfile

NOW = datetime(2026, 8, 22, 13, 0, tzinfo=UTC)
OWNER = "a" * 64
BUCKET = "mr-lister-phase6-artifacts-dev"
PROFILE = ProductProfile.model_validate_json(
    Path("config/product_profiles/gildan_64000_swiftpod.json").read_text(encoding="utf-8")
)
EXACT_PROFILE = ExactReviewProductProfile(
    profile=PROFILE,
    fingerprint=canonical_fingerprint(PROFILE),
)


def _png() -> bytes:
    image = Image.new("RGBA", (2, 2), (32, 64, 96, 255))
    image.putdata(
        (
            (32, 64, 96, 0),
            (32, 64, 96, 64),
            (32, 64, 96, 192),
            (32, 64, 96, 255),
        )
    )
    output = BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


@dataclass
class MutableClock:
    value: datetime = NOW

    def __call__(self) -> datetime:
        return self.value

    def advance(self, delta: timedelta) -> None:
        self.value += delta


class MutableProfileAuthority:
    def __init__(self, exact: ExactReviewProductProfile = EXACT_PROFILE) -> None:
        self.exact = exact
        self.calls: list[tuple[str, int]] = []

    def get_exact(self, *, profile_id: str, profile_version: int) -> ExactReviewProductProfile:
        self.calls.append((profile_id, profile_version))
        return self.exact


class FakeUploadArtifacts:
    def __init__(self) -> None:
        self.current: CurrentUploadObject | None = None
        self.authorization_calls: list[UploadIntent] = []
        self.read_calls: list[UploadIntent] = []
        self.pin_calls: list[tuple[UploadIntent, str]] = []
        self.release_calls: list[tuple[UploadIntent, str]] = []
        self.tag_calls: list[tuple[str, str]] = []
        self.before_pin: Callable[[], None] | None = None
        self.before_release: Callable[[], None] | None = None

    def issue_authorization(
        self,
        intent: UploadIntent,
        *,
        now: datetime,
    ) -> UploadAuthorization:
        self.authorization_calls.append(intent)
        return UploadAuthorization(
            owner_id=intent.owner_id,
            upload_id=intent.upload_id,
            job_id=intent.job_id,
            authorization_generation=intent.authorization_generation,
            url=f"https://{intent.bucket}.s3.us-west-2.amazonaws.com/",
            form_fields={
                "key": intent.object_key,
                "Content-Type": "image/png",
                "x-amz-checksum-algorithm": "SHA256",
                "x-amz-checksum-sha256": b64encode(bytes.fromhex(intent.content_sha256)).decode(
                    "ascii"
                ),
                "x-amz-server-side-encryption": "AES256",
                "x-amz-tagging": "mr-lister-state=staged",
                "x-amz-algorithm": "AWS4-HMAC-SHA256",
                "x-amz-credential": "redacted-credential-scope",
                "x-amz-date": "20260822T200000Z",
                "policy": "redacted-policy",
                "x-amz-signature": "1" * 64,
            },
            content_sha256=intent.content_sha256,
            size_bytes=intent.size_bytes,
            issued_at=now,
            expires_at=intent.authorization_expires_at,
        )

    def read_current_object(self, intent: UploadIntent) -> CurrentUploadObject:
        self.read_calls.append(intent)
        if self.current is None:
            raise FileNotFoundError(intent.object_key)
        return self.current

    def pin_object_version(self, intent: UploadIntent, version_id: str) -> None:
        callback = self.before_pin
        self.before_pin = None
        if callback is not None:
            callback()
        self.pin_calls.append((intent, version_id))
        self.tag_calls.append(("pinned", version_id))

    def release_unreferenced_version(self, intent: UploadIntent, version_id: str) -> None:
        callback = self.before_release
        self.before_release = None
        if callback is not None:
            callback()
        self.release_calls.append((intent, version_id))
        self.tag_calls.append(("staged", version_id))

    def stage(self, content: bytes, *, version_id: str = "source-version-1") -> None:
        self.current = CurrentUploadObject(
            version_id=version_id,
            content=content,
            content_length=len(content),
            content_type="image/png",
            checksum_sha256_base64=b64encode(sha256(content).digest()).decode("ascii"),
            server_side_encryption="AES256",
        )


class CancellationWinsStore(InMemorySellerControlStore):
    """Inject cancellation immediately before the completion transaction CAS."""

    def __init__(self) -> None:
        super().__init__()
        self.before_completion: Callable[[], None] | None = None

    def complete_upload(self, commit: UploadCompletionCommit):  # type: ignore[no-untyped-def]
        callback = self.before_completion
        self.before_completion = None
        if callback is not None:
            callback()
        return super().complete_upload(commit)


class IntentCommitInterleavingStore(InMemorySellerControlStore):
    """Run one competing command immediately before an intent commit acquires authority."""

    def __init__(self) -> None:
        super().__init__()
        self.before_intent_commit: Callable[[], None] | None = None

    def commit_upload_intent(self, commit):  # type: ignore[no-untyped-def]
        callback = self.before_intent_commit
        self.before_intent_commit = None
        if callback is not None:
            callback()
        return super().commit_upload_intent(commit)


class CompletionFailsStore(InMemorySellerControlStore):
    """Inject a definite persistence failure after the object version is pinned."""

    def complete_upload(self, commit: UploadCompletionCommit):  # type: ignore[no-untyped-def]
        del commit
        raise RuntimeError("raw adapter detail must not escape")


class UploadReadFailsStore(InMemorySellerControlStore):
    def get_upload_intent_for_owner(self, owner_id: str, upload_id: str) -> UploadIntent:
        del owner_id, upload_id
        raise RuntimeError("raw adapter detail must not escape")


class FirstCompletionFailsStore(InMemorySellerControlStore):
    """Fail one completion, then allow a concurrent same-version winner."""

    def __init__(self) -> None:
        super().__init__()
        self.fail_next_completion = True

    def complete_upload(self, commit: UploadCompletionCommit):  # type: ignore[no-untyped-def]
        if self.fail_next_completion:
            self.fail_next_completion = False
            raise RuntimeError("first completion failed before commit")
        return super().complete_upload(commit)


@dataclass
class Harness:
    service: UploadIntakeService
    store: InMemorySellerControlStore
    artifacts: FakeUploadArtifacts
    profiles: MutableProfileAuthority
    clock: MutableClock


def _harness(
    *,
    store: InMemorySellerControlStore | None = None,
) -> Harness:
    selected_store = store or InMemorySellerControlStore()
    artifacts = FakeUploadArtifacts()
    profiles = MutableProfileAuthority()
    clock = MutableClock()
    return Harness(
        service=UploadIntakeService(
            store=selected_store,
            artifacts=artifacts,
            profiles=profiles,
            artifact_bucket=BUCKET,
            profile_id=PROFILE.profile_id,
            profile_version=PROFILE.profile_version,
            clock=clock,
        ),
        store=selected_store,
        artifacts=artifacts,
        profiles=profiles,
        clock=clock,
    )


def _create(harness: Harness, *, idempotency_key: str = "create-1"):
    content = _png()
    result = harness.service.create_upload(
        owner_id=OWNER,
        idempotency_key=idempotency_key,
        filename="seller-art.png",
        content_type="image/png",
        content_sha256=sha256(content).hexdigest(),
        size_bytes=len(content),
    )
    return content, result


def test_create_upload_replays_one_receipt_and_rejects_key_reuse_with_changed_input() -> None:
    harness = _harness()
    content, created = _create(harness)

    replayed = harness.service.create_upload(
        owner_id=OWNER,
        idempotency_key="create-1",
        filename="seller-art.png",
        content_type="image/png",
        content_sha256=sha256(content).hexdigest(),
        size_bytes=len(content),
    )

    assert replayed.receipt == created.receipt
    assert replayed.authorization is not None
    assert len(harness.artifacts.authorization_calls) == 2
    assert len(harness.store._upload_intents) == 1
    assert len(harness.store._upload_receipts) == 1

    with pytest.raises(IdempotencyConflictError):
        harness.service.create_upload(
            owner_id=OWNER,
            idempotency_key="create-1",
            filename="changed.png",
            content_type="image/png",
            content_sha256=sha256(content).hexdigest(),
            size_bytes=len(content),
        )
    assert len(harness.artifacts.authorization_calls) == 2


def test_upload_recovery_read_preserves_the_store_owner_boundary() -> None:
    harness = _harness()
    _content, created = _create(harness)

    recovered = harness.service.get_upload(
        owner_id=OWNER,
        upload_id=created.receipt.upload_id,
    )

    assert recovered.owner_id == OWNER
    assert recovered.upload_id == created.receipt.upload_id
    with pytest.raises(NotFoundError, match="requested upload"):
        harness.service.get_upload(
            owner_id="b" * 64,
            upload_id=created.receipt.upload_id,
        )


def test_upload_recovery_read_maps_unexpected_storage_failure_to_dependency_error() -> None:
    harness = _harness(store=UploadReadFailsStore())

    with pytest.raises(UploadDependencyUnavailableError) as unavailable:
        harness.service.get_upload(owner_id=OWNER, upload_id="upload_unavailable")

    assert unavailable.value.__cause__ is None
    assert "raw adapter detail" not in str(unavailable.value)


def test_concurrent_create_returns_only_the_winner_intent_authorization_window() -> None:
    store = IntentCommitInterleavingStore()
    harness = _harness(store=store)
    content = _png()
    early_clock = MutableClock(NOW)
    later_clock = MutableClock(NOW + timedelta(minutes=2))
    early_service = UploadIntakeService(
        store=store,
        artifacts=harness.artifacts,
        profiles=harness.profiles,
        artifact_bucket=BUCKET,
        profile_id=PROFILE.profile_id,
        profile_version=PROFILE.profile_version,
        clock=early_clock,
    )
    later_service = UploadIntakeService(
        store=store,
        artifacts=harness.artifacts,
        profiles=harness.profiles,
        artifact_bucket=BUCKET,
        profile_id=PROFILE.profile_id,
        profile_version=PROFILE.profile_version,
        clock=later_clock,
    )
    winner_results = []
    create_args = {
        "owner_id": OWNER,
        "idempotency_key": "concurrent-create",
        "filename": "seller-art.png",
        "content_type": "image/png",
        "content_sha256": sha256(content).hexdigest(),
        "size_bytes": len(content),
    }
    store.before_intent_commit = lambda: winner_results.append(
        early_service.create_upload(**create_args)
    )

    later = later_service.create_upload(**create_args)

    assert len(winner_results) == 1
    assert later.receipt == winner_results[0].receipt
    assert later.authorization is not None
    assert later.authorization.issued_at == later_clock.value
    assert later.authorization.expires_at == NOW + UPLOAD_AUTHORIZATION_TTL
    intent = store.get_upload_intent_for_owner(OWNER, later.receipt.upload_id)
    assert later.authorization.expires_at == intent.authorization_expires_at
    assert len(harness.store._upload_intents) == 1
    assert len(harness.store._upload_receipts) == 1


def test_reauthorization_waits_for_expiry_without_needing_bucket_list_authority() -> None:
    harness = _harness()
    content, created = _create(harness)
    upload_id = created.receipt.upload_id

    with pytest.raises(InvalidControlStateError, match="still active"):
        harness.service.authorize_upload(
            owner_id=OWNER,
            upload_id=upload_id,
            idempotency_key="reauthorize-too-early",
        )
    harness.clock.advance(UPLOAD_AUTHORIZATION_TTL)
    reauthorized = harness.service.authorize_upload(
        owner_id=OWNER,
        upload_id=upload_id,
        idempotency_key="reauthorize-1",
    )
    assert reauthorized.authorization is not None
    assert reauthorized.authorization.authorization_generation == 2
    assert reauthorized.receipt.record_version == 1

    # A current object does not require an existence probe: the new form remains bound to the
    # same exact key, size, checksum, media type, and encryption, while completion pins VersionId.
    harness.artifacts.stage(content)
    assert not hasattr(harness.artifacts, "current_object_exists")


def test_concurrent_reauthorization_uses_the_winner_intent_remaining_ttl() -> None:
    store = IntentCommitInterleavingStore()
    harness = _harness(store=store)
    _content, created = _create(harness)
    early_clock = MutableClock(NOW + UPLOAD_AUTHORIZATION_TTL + timedelta(minutes=1))
    later_clock = MutableClock(NOW + UPLOAD_AUTHORIZATION_TTL + timedelta(minutes=3))
    early_service = UploadIntakeService(
        store=store,
        artifacts=harness.artifacts,
        profiles=harness.profiles,
        artifact_bucket=BUCKET,
        profile_id=PROFILE.profile_id,
        profile_version=PROFILE.profile_version,
        clock=early_clock,
    )
    later_service = UploadIntakeService(
        store=store,
        artifacts=harness.artifacts,
        profiles=harness.profiles,
        artifact_bucket=BUCKET,
        profile_id=PROFILE.profile_id,
        profile_version=PROFILE.profile_version,
        clock=later_clock,
    )
    winner_results = []
    reauthorize_args = {
        "owner_id": OWNER,
        "upload_id": created.receipt.upload_id,
        "idempotency_key": "concurrent-reauthorize",
    }
    store.before_intent_commit = lambda: winner_results.append(
        early_service.authorize_upload(**reauthorize_args)
    )

    later = later_service.authorize_upload(**reauthorize_args)

    assert len(winner_results) == 1
    assert later.receipt == winner_results[0].receipt
    assert later.authorization is not None
    assert later.authorization.issued_at == later_clock.value
    assert later.authorization.expires_at == early_clock.value + UPLOAD_AUTHORIZATION_TTL
    intent = store.get_upload_intent_for_owner(OWNER, created.receipt.upload_id)
    assert later.authorization.authorization_generation == 2
    assert later.authorization.expires_at == intent.authorization_expires_at


def test_valid_png_completion_atomically_creates_exactly_one_preparation_graph() -> None:
    harness = _harness()
    content, created = _create(harness)
    harness.artifacts.stage(content)

    completed = harness.service.complete_upload(
        owner_id=OWNER,
        upload_id=created.receipt.upload_id,
        idempotency_key="complete-1",
    )
    replayed = harness.service.complete_upload(
        owner_id=OWNER,
        upload_id=created.receipt.upload_id,
        idempotency_key="complete-1",
    )

    assert completed.receipt == replayed.receipt
    assert completed.authorization is None
    assert completed.receipt.command_type is UploadCommandType.COMPLETE_UPLOAD
    assert completed.receipt.status is UploadIntentStatus.COMPLETED
    assert len(harness.store.jobs) == 1
    job = harness.store.get_job_for_owner(OWNER, completed.receipt.job_id)
    source = harness.store.get_source_artifact(job.job_id)
    events = harness.store.list_events(job.job_id)
    work = harness.store.list_work_requests(job.job_id)
    assert job.state is ControlJobState.INTAKE_VALIDATED
    assert job.source_artifact_fingerprint == source.fingerprint
    assert job.active_work_request_id == completed.receipt.work_request_id
    assert source.version_id == "source-version-1"
    assert source.content_sha256 == sha256(content).hexdigest()
    assert len(events) == 1
    assert events[0].name == "UPLOAD_COMPLETED"
    assert len(work) == 1
    assert work[0].work_type is WorkType.PREPARE
    assert work[0].status is WorkRequestStatus.PENDING
    assert work[0].work_request_id == completed.receipt.work_request_id
    assert len(harness.artifacts.read_calls) == 1
    # Pin before the atomic commit, then reassert after durable authority wins so a concurrent
    # compensation cannot leave the referenced version staged.
    assert [version for _intent, version in harness.artifacts.pin_calls] == [
        "source-version-1",
        "source-version-1",
    ]


def test_completion_expiring_during_the_pin_is_released_before_any_job_commit() -> None:
    harness = _harness()
    content, created = _create(harness)
    harness.artifacts.stage(content)
    intent = harness.store.get_upload_intent_for_owner(OWNER, created.receipt.upload_id)
    harness.artifacts.before_pin = lambda: setattr(
        harness.clock,
        "value",
        intent.intent_expires_at,
    )

    with pytest.raises(UploadExpiredError):
        harness.service.complete_upload(
            owner_id=OWNER,
            upload_id=created.receipt.upload_id,
            idempotency_key="complete-expires-during-pin",
        )

    assert len(harness.artifacts.pin_calls) == 1
    assert harness.artifacts.release_calls == harness.artifacts.pin_calls
    assert harness.store.jobs == {}


def test_terminal_create_replay_never_reissues_a_presigned_form() -> None:
    harness = _harness()
    content, created = _create(harness)
    harness.artifacts.stage(content)
    harness.service.complete_upload(
        owner_id=OWNER,
        upload_id=created.receipt.upload_id,
        idempotency_key="complete-1",
    )
    issued_before_replay = len(harness.artifacts.authorization_calls)

    replayed_create = harness.service.create_upload(
        owner_id=OWNER,
        idempotency_key="create-1",
        filename="seller-art.png",
        content_type="image/png",
        content_sha256=sha256(content).hexdigest(),
        size_bytes=len(content),
    )

    assert replayed_create.receipt == created.receipt
    assert replayed_create.authorization is None
    assert len(harness.artifacts.authorization_calls) == issued_before_replay


def test_terminal_reauthorization_replay_never_reissues_a_presigned_form() -> None:
    harness = _harness()
    content, created = _create(harness)
    harness.clock.advance(UPLOAD_AUTHORIZATION_TTL)
    reauthorized = harness.service.authorize_upload(
        owner_id=OWNER,
        upload_id=created.receipt.upload_id,
        idempotency_key="reauthorize-before-completion",
    )
    assert reauthorized.authorization is not None
    harness.artifacts.stage(content)
    harness.service.complete_upload(
        owner_id=OWNER,
        upload_id=created.receipt.upload_id,
        idempotency_key="complete-after-reauthorization",
    )
    issued_before_replay = len(harness.artifacts.authorization_calls)

    replayed = harness.service.authorize_upload(
        owner_id=OWNER,
        upload_id=created.receipt.upload_id,
        idempotency_key="reauthorize-before-completion",
    )

    assert replayed.receipt == reauthorized.receipt
    assert replayed.authorization is None
    assert len(harness.artifacts.authorization_calls) == issued_before_replay


def test_cancelled_upload_rejects_completion_without_reading_or_pinning() -> None:
    harness = _harness()
    content, created = _create(harness)
    harness.artifacts.stage(content)

    cancelled = harness.service.cancel_upload(
        owner_id=OWNER,
        upload_id=created.receipt.upload_id,
        idempotency_key="cancel-1",
    )

    assert cancelled.receipt.status is UploadIntentStatus.CANCELLED
    with pytest.raises(InvalidControlStateError, match="no longer open"):
        harness.service.complete_upload(
            owner_id=OWNER,
            upload_id=created.receipt.upload_id,
            idempotency_key="complete-after-cancel",
        )
    assert not harness.artifacts.read_calls
    assert not harness.artifacts.pin_calls
    assert not harness.store.jobs


def test_completion_loses_store_cas_when_cancellation_wins_the_race() -> None:
    race_store = CancellationWinsStore()
    harness = _harness(store=race_store)
    content, created = _create(harness)
    harness.artifacts.stage(content)
    race_store.before_completion = lambda: harness.service.cancel_upload(
        owner_id=OWNER,
        upload_id=created.receipt.upload_id,
        idempotency_key="cancel-wins-race",
    )

    with pytest.raises(ConcurrentControlModificationError, match="changed before completion"):
        harness.service.complete_upload(
            owner_id=OWNER,
            upload_id=created.receipt.upload_id,
            idempotency_key="complete-loses-race",
        )

    intent = harness.store.get_upload_intent_for_owner(OWNER, created.receipt.upload_id)
    assert intent.status is UploadIntentStatus.CANCELLED
    assert not harness.store.jobs
    assert len(harness.artifacts.pin_calls) == 1
    assert harness.artifacts.release_calls == harness.artifacts.pin_calls


def test_completion_store_failure_compensates_pin_and_returns_safe_error() -> None:
    harness = _harness(store=CompletionFailsStore())
    content, created = _create(harness)
    harness.artifacts.stage(content)

    with pytest.raises(UploadDependencyUnavailableError) as caught:
        harness.service.complete_upload(
            owner_id=OWNER,
            upload_id=created.receipt.upload_id,
            idempotency_key="complete-store-failure",
        )

    assert caught.value.__cause__ is None
    assert "raw adapter detail" not in str(caught.value)
    assert harness.artifacts.release_calls == harness.artifacts.pin_calls
    assert not harness.store.jobs


def test_same_version_completion_winning_during_compensation_remains_pinned() -> None:
    harness = _harness(store=FirstCompletionFailsStore())
    content, created = _create(harness)
    harness.artifacts.stage(content, version_id="same-source-version")
    nested_results = []

    harness.artifacts.before_release = lambda: nested_results.append(
        harness.service.complete_upload(
            owner_id=OWNER,
            upload_id=created.receipt.upload_id,
            idempotency_key="completion-winner",
        )
    )

    with pytest.raises(UploadDependencyUnavailableError):
        harness.service.complete_upload(
            owner_id=OWNER,
            upload_id=created.receipt.upload_id,
            idempotency_key="completion-loser",
        )

    assert len(nested_results) == 1
    intent = harness.store.get_upload_intent_for_owner(OWNER, created.receipt.upload_id)
    assert intent.status is UploadIntentStatus.COMPLETED
    assert intent.completed_version_id == "same-source-version"
    assert harness.artifacts.tag_calls[-1] == ("pinned", "same-source-version")


def test_concurrent_completion_replay_releases_a_different_unreferenced_version() -> None:
    race_store = CancellationWinsStore()
    harness = _harness(store=race_store)
    content, created = _create(harness)
    harness.artifacts.stage(content, version_id="source-version-outer")
    nested_results = []

    def complete_newer_version() -> None:
        harness.artifacts.stage(content, version_id="source-version-winner")
        nested_results.append(
            harness.service.complete_upload(
                owner_id=OWNER,
                upload_id=created.receipt.upload_id,
                idempotency_key="same-completion-key",
            )
        )

    race_store.before_completion = complete_newer_version
    outer = harness.service.complete_upload(
        owner_id=OWNER,
        upload_id=created.receipt.upload_id,
        idempotency_key="same-completion-key",
    )

    assert len(nested_results) == 1
    assert outer.receipt == nested_results[0].receipt
    source = harness.store.get_source_artifact(created.receipt.job_id)
    assert source.version_id == "source-version-winner"
    assert [version for _intent, version in harness.artifacts.pin_calls] == [
        "source-version-outer",
        "source-version-winner",
        "source-version-winner",
    ]
    assert [version for _intent, version in harness.artifacts.release_calls] == [
        "source-version-outer"
    ]


@pytest.mark.parametrize(
    "corrupt",
    (
        lambda current: replace(current, content_length=current.content_length + 1),
        lambda current: replace(current, content_type="application/octet-stream"),
        lambda current: replace(current, checksum_sha256_base64=b64encode(b"wrong").decode()),
        lambda current: replace(current, server_side_encryption="aws:kms"),
        lambda current: replace(
            current,
            content=bytes([current.content[0] ^ 1]) + current.content[1:],
        ),
    ),
)
def test_completion_rejects_corrupt_object_body_or_metadata_before_pinning(
    corrupt: Callable[[CurrentUploadObject], CurrentUploadObject],
) -> None:
    harness = _harness()
    content, created = _create(harness)
    harness.artifacts.stage(content)
    assert harness.artifacts.current is not None
    harness.artifacts.current = corrupt(harness.artifacts.current)

    with pytest.raises(UploadArtifactIntegrityError):
        harness.service.complete_upload(
            owner_id=OWNER,
            upload_id=created.receipt.upload_id,
            idempotency_key="complete-corrupt",
        )

    assert not harness.artifacts.pin_calls
    assert not harness.store.jobs


def test_completion_rejects_profile_authority_drift_before_reading_the_object() -> None:
    harness = _harness()
    content, created = _create(harness)
    harness.artifacts.stage(content)
    changed_profile = PROFILE.model_copy(update={"retail_price_cents": 3999})
    harness.profiles.exact = ExactReviewProductProfile(
        profile=changed_profile,
        fingerprint=canonical_fingerprint(changed_profile),
    )

    with pytest.raises(UploadArtifactIntegrityError):
        harness.service.complete_upload(
            owner_id=OWNER,
            upload_id=created.receipt.upload_id,
            idempotency_key="complete-profile-drift",
        )
    assert not harness.artifacts.read_calls
    assert not harness.artifacts.pin_calls


def test_completion_rejects_s3_null_version_before_pinning_or_persistence() -> None:
    harness = _harness()
    content, created = _create(harness)
    harness.artifacts.stage(content, version_id="null")

    with pytest.raises(UploadArtifactIntegrityError):
        harness.service.complete_upload(
            owner_id=OWNER,
            upload_id=created.receipt.upload_id,
            idempotency_key="complete-null-version",
        )

    assert not harness.artifacts.pin_calls
    assert not harness.store.jobs


def test_create_rejects_a_noncanonical_profile_authority_before_issuing_upload() -> None:
    harness = _harness()
    harness.profiles.exact = ExactReviewProductProfile(
        profile=PROFILE,
        fingerprint="f" * 64,
    )
    content = _png()

    with pytest.raises(UploadDependencyUnavailableError):
        harness.service.create_upload(
            owner_id=OWNER,
            idempotency_key="create-invalid-profile",
            filename="seller-art.png",
            content_type="image/png",
            content_sha256=sha256(content).hexdigest(),
            size_bytes=len(content),
        )
    assert not harness.artifacts.authorization_calls
    assert not harness.store._upload_intents
