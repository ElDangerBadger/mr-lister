from __future__ import annotations

from base64 import b64encode
from datetime import UTC, datetime, timedelta
from hashlib import sha256

import pytest
from pydantic import ValidationError

from mr_lister.control.dispatch import deterministic_execution_name, work_input_fingerprint
from mr_lister.control.models import (
    ControlJobRecord,
    ControlJobState,
    DomainEvent,
    SourceArtifactRecord,
    WorkRequest,
    WorkType,
)
from mr_lister.control.source_artwork import source_artifact_fingerprint
from mr_lister.control.upload_models import (
    UPLOAD_AUTHORIZATION_TTL,
    UploadAuthorization,
    UploadCommandType,
    UploadCompletionCommit,
    UploadIntent,
    UploadIntentCommit,
    UploadIntentStatus,
    UploadReceipt,
)

NOW = datetime(2026, 8, 22, 10, 0, tzinfo=UTC)
COMPLETED_AT = NOW + timedelta(minutes=2)
OWNER = "a" * 64
UPLOAD_ID = "upload_phase64"
JOB_ID = "job_phase64"
WORK_ID = "work_phase64_prepare"
CONTENT_SHA256 = sha256(b"phase6-fixture").hexdigest()
PROFILE_FINGERPRINT = "b" * 64
KEY = f"private/owners/{OWNER}/jobs/{JOB_ID}/source/source.png"


def _intent(**updates: object) -> UploadIntent:
    values: dict[str, object] = {
        "owner_id": OWNER,
        "upload_id": UPLOAD_ID,
        "job_id": JOB_ID,
        "filename": "seller-art.png",
        "content_type": "image/png",
        "content_sha256": CONTENT_SHA256,
        "size_bytes": 1024,
        "bucket": "mr-lister-phase6-artifacts-dev",
        "object_key": KEY,
        "product_profile_id": "gildan_64000_swiftpod",
        "product_profile_version": 2,
        "product_profile_fingerprint": PROFILE_FINGERPRINT,
        "authorization_generation": 1,
        "authorization_issued_at": NOW,
        "authorization_expires_at": NOW + UPLOAD_AUTHORIZATION_TTL,
        "intent_expires_at": NOW + timedelta(days=1),
        "created_at": NOW,
        "updated_at": NOW,
    }
    values.update(updates)
    return UploadIntent.model_validate(values)


def _receipt(
    command_type: UploadCommandType,
    *,
    status: UploadIntentStatus,
    version: int,
    created_at: datetime,
    work_request_id: str | None = None,
) -> UploadReceipt:
    return UploadReceipt(
        receipt_id=f"receipt_{command_type.value}",
        owner_id=OWNER,
        upload_id=UPLOAD_ID,
        job_id=JOB_ID,
        command_type=command_type,
        idempotency_key_digest="c" * 64,
        request_fingerprint="d" * 64,
        status=status,
        record_version=version,
        work_request_id=work_request_id,
        created_at=created_at,
    )


def _authorization(**updates: object) -> UploadAuthorization:
    fields = {
        "key": KEY,
        "Content-Type": "image/png",
        "x-amz-checksum-algorithm": "SHA256",
        "x-amz-checksum-sha256": b64encode(bytes.fromhex(CONTENT_SHA256)).decode("ascii"),
        "x-amz-server-side-encryption": "AES256",
        "x-amz-tagging": "mr-lister-state=staged",
        "x-amz-algorithm": "AWS4-HMAC-SHA256",
        "x-amz-credential": "redacted-credential-scope",
        "x-amz-date": "20260822T170000Z",
        "policy": "redacted-policy",
        "x-amz-signature": "1" * 64,
    }
    values: dict[str, object] = {
        "owner_id": OWNER,
        "upload_id": UPLOAD_ID,
        "job_id": JOB_ID,
        "authorization_generation": 1,
        "url": "https://mr-lister-bucket.s3.us-west-2.amazonaws.com/",
        "form_fields": fields,
        "content_sha256": CONTENT_SHA256,
        "size_bytes": 1024,
        "issued_at": NOW,
        "expires_at": NOW + UPLOAD_AUTHORIZATION_TTL,
    }
    values.update(updates)
    return UploadAuthorization.model_validate(values)


def test_open_upload_intent_binds_owner_job_key_metadata_profile_and_expiry() -> None:
    intent = _intent()

    assert intent.status is UploadIntentStatus.OPEN
    assert intent.object_key == KEY
    assert intent.authorization_generation == 1

    with pytest.raises(ValidationError, match="exact owner"):
        _intent(object_key=f"private/owners/{OWNER}/jobs/another/source/source.png")
    with pytest.raises(ValidationError, match="basename"):
        _intent(filename="folder/art.png")
    with pytest.raises(ValidationError, match="within one day"):
        _intent(intent_expires_at=NOW + timedelta(days=1, seconds=1))


def test_upload_intent_terminal_fields_are_exclusive_and_complete() -> None:
    with pytest.raises(ValidationError, match="pinned result"):
        _intent(status=UploadIntentStatus.COMPLETED, completed_at=COMPLETED_AT)
    with pytest.raises(ValidationError, match="cancellation"):
        _intent(status=UploadIntentStatus.CANCELLED, cancelled_at=COMPLETED_AT)
    with pytest.raises(ValidationError, match="expiry"):
        _intent(status=UploadIntentStatus.EXPIRED)


def test_upload_authorization_is_exact_short_lived_and_repr_redacts_presign_fields() -> None:
    authorization = _authorization()

    assert authorization.form_fields["key"] == KEY
    assert authorization.form_fields["x-amz-checksum-algorithm"] == "SHA256"
    assert authorization.form_fields["x-amz-checksum-sha256"] == b64encode(
        bytes.fromhex(CONTENT_SHA256)
    ).decode("ascii")
    assert "x-amz-signature" not in repr(authorization)
    assert "redacted-policy" not in repr(authorization)
    assert "owner_id" not in authorization.model_dump()

    with pytest.raises(ValidationError, match="five minutes"):
        _authorization(expires_at=NOW + UPLOAD_AUTHORIZATION_TTL + timedelta(seconds=1))
    wrong_fields = dict(authorization.form_fields)
    wrong_fields["x-amz-checksum-sha256"] = b64encode(b"wrong").decode("ascii")
    with pytest.raises(ValidationError, match="required object fields"):
        _authorization(form_fields=wrong_fields)
    wrong_fields = dict(authorization.form_fields)
    wrong_fields["x-amz-tagging"] = "mr-lister-state=pinned"
    with pytest.raises(ValidationError, match="required object fields"):
        _authorization(form_fields=wrong_fields)
    wrong_fields = dict(authorization.form_fields)
    wrong_fields["acl"] = "public-read"
    with pytest.raises(ValidationError, match="unsupported form fields"):
        _authorization(form_fields=wrong_fields)
    wrong_fields = dict(authorization.form_fields)
    wrong_fields.pop("x-amz-credential")
    with pytest.raises(ValidationError, match="unsupported form fields"):
        _authorization(form_fields=wrong_fields)


@pytest.mark.parametrize(
    "url",
    (
        "http://mr-lister-bucket.s3.us-west-2.amazonaws.com/",
        "https://evil.amazonaws.com/",
        "https://mr-lister-bucket.s3.us-west-2.amazonaws.com:443/",
        "https://user@mr-lister-bucket.s3.us-west-2.amazonaws.com/",
        "https://mr-lister-bucket.s3.us-west-2.amazonaws.com/?next=evil",
        "https://MR-LISTER-BUCKET.s3.us-west-2.amazonaws.com/",
        "https://mr-lister..bucket.s3.us-west-2.amazonaws.com/",
    ),
)
def test_upload_authorization_rejects_ambiguous_or_non_s3_targets(url: str) -> None:
    with pytest.raises(ValidationError, match="HTTPS S3"):
        _authorization(url=url)


def test_upload_authorization_binds_its_owner_and_reserved_job_key() -> None:
    authorization = _authorization()
    wrong_fields = dict(authorization.form_fields)
    wrong_fields["key"] = f"private/owners/{'e' * 64}/jobs/{JOB_ID}/source/source.png"

    with pytest.raises(ValidationError, match="reserved job key"):
        _authorization(form_fields=wrong_fields)


def test_durable_intent_and_receipt_cannot_persist_presigned_form_material() -> None:
    intent_fields = set(UploadIntent.model_fields)
    receipt_fields = set(UploadReceipt.model_fields)

    for forbidden in ("url", "form_fields", "policy", "signature"):
        assert forbidden not in intent_fields
        assert forbidden not in receipt_fields


def test_create_and_reauthorize_commits_are_closed_and_versioned() -> None:
    current = _intent()
    created = UploadIntentCommit(
        updated=current,
        receipt=_receipt(
            UploadCommandType.CREATE_UPLOAD,
            status=UploadIntentStatus.OPEN,
            version=0,
            created_at=NOW,
        ),
    )
    assert created.current is None

    reauthorized_at = NOW + timedelta(minutes=6)
    updated = UploadIntent.model_validate(
        {
            **current.model_dump(),
            "record_version": 1,
            "authorization_generation": 2,
            "authorization_issued_at": reauthorized_at,
            "authorization_expires_at": reauthorized_at + UPLOAD_AUTHORIZATION_TTL,
            "updated_at": reauthorized_at,
        }
    )
    commit = UploadIntentCommit(
        current=current,
        updated=updated,
        receipt=_receipt(
            UploadCommandType.REAUTHORIZE_UPLOAD,
            status=UploadIntentStatus.OPEN,
            version=1,
            created_at=reauthorized_at,
        ),
    )
    assert commit.updated.authorization_generation == 2

    overlapping_at = NOW + timedelta(minutes=4)
    overlapping = UploadIntent.model_validate(
        {
            **current.model_dump(),
            "record_version": 1,
            "authorization_generation": 2,
            "authorization_issued_at": overlapping_at,
            "authorization_expires_at": overlapping_at + UPLOAD_AUTHORIZATION_TTL,
            "updated_at": overlapping_at,
        }
    )
    with pytest.raises(ValidationError, match="grant authority"):
        UploadIntentCommit(
            current=current,
            updated=overlapping,
            receipt=_receipt(
                UploadCommandType.REAUTHORIZE_UPLOAD,
                status=UploadIntentStatus.OPEN,
                version=1,
                created_at=overlapping_at,
            ),
        )

    changed = updated.model_copy(update={"filename": "other.png"})
    with pytest.raises(ValidationError, match="immutable"):
        UploadIntentCommit(
            current=current,
            updated=changed,
            receipt=commit.receipt,
        )

    expired_at = current.intent_expires_at
    expired = UploadIntent.model_validate(
        {
            **current.model_dump(),
            "record_version": 1,
            "status": UploadIntentStatus.CANCELLED,
            "cancelled_at": expired_at,
            "cancellation_receipt_id": "receipt_cancel_upload",
            "updated_at": expired_at,
        }
    )
    with pytest.raises(ValidationError, match="expired upload intent"):
        UploadIntentCommit(
            current=current,
            updated=expired,
            receipt=_receipt(
                UploadCommandType.CANCEL_UPLOAD,
                status=UploadIntentStatus.CANCELLED,
                version=1,
                created_at=expired_at,
            ),
        )


def test_completion_commit_atomically_binds_intent_source_job_event_and_prepare_work() -> None:
    current = _intent()
    source_material = {
        "job_id": JOB_ID,
        "owner_id": OWNER,
        "bucket": current.bucket,
        "object_key": KEY,
        "version_id": "version-pinned-1",
        "content_sha256": current.content_sha256,
        "size_bytes": current.size_bytes,
        "media_type": "image/png",
        "product_profile_id": current.product_profile_id,
        "product_profile_version": current.product_profile_version,
        "product_profile_fingerprint": current.product_profile_fingerprint,
        "created_at": COMPLETED_AT,
    }
    source_fingerprint = source_artifact_fingerprint(**source_material)
    source = SourceArtifactRecord(fingerprint=source_fingerprint, **source_material)
    completed = UploadIntent.model_validate(
        {
            **current.model_dump(),
            "record_version": 1,
            "status": UploadIntentStatus.COMPLETED,
            "completed_at": COMPLETED_AT,
            "completed_source_artifact_fingerprint": source.fingerprint,
            "completed_version_id": source.version_id,
            "completion_receipt_id": "receipt_complete_upload",
            "updated_at": COMPLETED_AT,
        }
    )
    receipt = _receipt(
        UploadCommandType.COMPLETE_UPLOAD,
        status=UploadIntentStatus.COMPLETED,
        version=1,
        created_at=COMPLETED_AT,
        work_request_id=WORK_ID,
    )
    intent_commit = UploadIntentCommit(
        current=current,
        updated=completed,
        receipt=receipt,
    )
    work = WorkRequest(
        work_request_id=WORK_ID,
        owner_id=OWNER,
        job_id=JOB_ID,
        receipt_id=receipt.receipt_id,
        work_type=WorkType.PREPARE,
        input_fingerprint=work_input_fingerprint(
            work_type=WorkType.PREPARE,
            job_id=JOB_ID,
            work_request_id=WORK_ID,
        ),
        execution_name=deterministic_execution_name(WORK_ID),
        next_dispatch_at=COMPLETED_AT,
        created_at=COMPLETED_AT,
        updated_at=COMPLETED_AT,
    )
    job = ControlJobRecord(
        owner_id=OWNER,
        job_id=JOB_ID,
        state=ControlJobState.INTAKE_VALIDATED,
        event_sequence=1,
        source_artifact_fingerprint=source.fingerprint,
        active_work_request_id=work.work_request_id,
        created_at=COMPLETED_AT,
        updated_at=COMPLETED_AT,
    )
    event = DomainEvent(
        job_id=JOB_ID,
        sequence=1,
        name="UPLOAD_COMPLETED",
        occurred_at=COMPLETED_AT,
    )

    commit = UploadCompletionCommit(
        intent=intent_commit,
        job=job,
        source_artifact=source,
        event=event,
        work_request=work,
    )

    assert commit.job.source_artifact_fingerprint == source.fingerprint
    assert commit.intent.receipt.work_request_id == work.work_request_id

    wrong_source = source.model_copy(update={"version_id": "other-version"})
    with pytest.raises(ValidationError):
        UploadCompletionCommit(
            intent=intent_commit,
            job=job,
            source_artifact=wrong_source,
            event=event,
            work_request=work,
        )

    polluted_event = event.model_copy(update={"name": "OTHER_EVENT"})
    with pytest.raises(ValidationError, match="pristine preparation"):
        UploadCompletionCommit(
            intent=intent_commit,
            job=job,
            source_artifact=source,
            event=polluted_event,
            work_request=work,
        )

    polluted_job = job.model_copy(update={"product_id": "unexpected_product"})
    with pytest.raises(ValidationError, match="pristine preparation"):
        UploadCompletionCommit(
            intent=intent_commit,
            job=polluted_job,
            source_artifact=source,
            event=event,
            work_request=work,
        )


def test_upload_receipt_command_status_and_work_contract_is_closed() -> None:
    with pytest.raises(ValidationError, match="status"):
        _receipt(
            UploadCommandType.CANCEL_UPLOAD,
            status=UploadIntentStatus.OPEN,
            version=1,
            created_at=NOW,
        )
    with pytest.raises(ValidationError, match="Only upload completion"):
        _receipt(
            UploadCommandType.CREATE_UPLOAD,
            status=UploadIntentStatus.OPEN,
            version=0,
            created_at=NOW,
            work_request_id=WORK_ID,
        )
