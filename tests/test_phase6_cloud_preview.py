from __future__ import annotations

from datetime import UTC, datetime
from urllib.parse import quote, urlencode

import pytest

from mr_lister.cloud.preview import (
    AuthenticatedPreviewLinkIssuer,
    ExactVersionArtworkPreviewService,
    PreviewAuthorizationUnavailableError,
    PreviewRedirect,
    preview_redirect_response,
)
from mr_lister.control.errors import NotFoundError
from mr_lister.control.models import ControlJobRecord, ControlJobState, SourceArtifactRecord
from mr_lister.control.source_artwork import source_artifact_fingerprint

NOW = datetime(2026, 8, 22, 12, 0, tzinfo=UTC)
OWNER = "a" * 64
OTHER_OWNER = "b" * 64
JOB_ID = "job_phase64_preview"
BUCKET = "mr-lister-phase6-artifacts-test"
ORIGIN = f"https://{BUCKET}.s3.us-west-2.amazonaws.com"
SOURCE_MATERIAL = {
    "job_id": JOB_ID,
    "owner_id": OWNER,
    "bucket": BUCKET,
    "object_key": f"private/owners/{OWNER}/jobs/{JOB_ID}/source/source.png",
    "version_id": "pinned-version-1",
    "content_sha256": "d" * 64,
    "size_bytes": 1_024,
    "media_type": "image/png",
    "product_profile_id": "gildan_64000_swiftpod",
    "product_profile_version": 2,
    "product_profile_fingerprint": "e" * 64,
    "created_at": NOW,
}
SOURCE_FP = source_artifact_fingerprint(**SOURCE_MATERIAL)


def source_record() -> SourceArtifactRecord:
    return SourceArtifactRecord(fingerprint=SOURCE_FP, **SOURCE_MATERIAL)


def job_record() -> ControlJobRecord:
    return ControlJobRecord(
        owner_id=OWNER,
        job_id=JOB_ID,
        state=ControlJobState.INTAKE_VALIDATED,
        event_sequence=1,
        source_artifact_fingerprint=SOURCE_FP,
        active_work_request_id="work_prepare",
        created_at=NOW,
        updated_at=NOW,
    )


class FakeStore:
    def __init__(self) -> None:
        self.job = job_record()
        self.source = source_record()
        self.calls: list[tuple[str, str]] = []

    def get_job_for_owner(self, owner_id: str, job_id: str) -> ControlJobRecord:
        self.calls.append(("job", owner_id))
        if owner_id != OWNER or job_id != JOB_ID:
            raise NotFoundError("private detail")
        return self.job

    def get_source_artifact(self, job_id: str) -> SourceArtifactRecord:
        self.calls.append(("source", job_id))
        return self.source


class FakePresigner:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []
        self.location: str | None = None

    def generate_presigned_url(
        self,
        ClientMethod: str,
        Params: dict[str, str],
        ExpiresIn: int,
        HttpMethod: str,
    ) -> str:
        self.calls.append(
            {
                "method": ClientMethod,
                "params": Params,
                "expires": ExpiresIn,
                "http_method": HttpMethod,
            }
        )
        if self.location is not None:
            return self.location
        path = "/" + quote(Params["Key"], safe="/")
        query = urlencode(
            {
                "versionId": Params["VersionId"],
                "response-content-type": Params["ResponseContentType"],
                "response-cache-control": Params["ResponseCacheControl"],
                "X-Amz-Algorithm": "AWS4-HMAC-SHA256",
                "X-Amz-Expires": str(ExpiresIn),
                "X-Amz-Signature": "1" * 64,
            }
        )
        return f"{ORIGIN}{path}?{query}"


def service(store: FakeStore, signer: FakePresigner) -> ExactVersionArtworkPreviewService:
    return ExactVersionArtworkPreviewService(
        store=store,
        presigner=signer,
        artifact_bucket=BUCKET,
        artifact_origin=ORIGIN,
        clock=lambda: NOW,
    )


def test_projection_link_is_authenticated_app_route_without_bearer_or_storage_data() -> None:
    issuer = AuthenticatedPreviewLinkIssuer(
        application_origin="https://review.mr-lister.test",
        clock=lambda: NOW,
    )

    link = issuer.issue(source=source_record())

    assert link.url == f"https://review.mr-lister.test/v1/jobs/{JOB_ID}/artwork-preview"
    assert "?" not in link.url
    assert BUCKET not in link.url
    assert OWNER not in link.url
    assert link.source_artifact_fingerprint == SOURCE_FP
    assert (link.expires_at - NOW).total_seconds() == 300


def test_preview_owner_check_precedes_source_read_and_presigning() -> None:
    store = FakeStore()
    signer = FakePresigner()

    with pytest.raises(NotFoundError):
        service(store, signer).authorize(owner_id=OTHER_OWNER, job_id=JOB_ID)

    assert store.calls == [("job", OTHER_OWNER)]
    assert signer.calls == []


def test_preview_presigns_only_the_exact_pinned_version_for_five_minutes() -> None:
    store = FakeStore()
    signer = FakePresigner()

    redirect = service(store, signer).authorize(owner_id=OWNER, job_id=JOB_ID)

    assert signer.calls == [
        {
            "method": "get_object",
            "params": {
                "Bucket": BUCKET,
                "Key": source_record().object_key,
                "VersionId": "pinned-version-1",
                "ResponseContentType": "image/png",
                "ResponseCacheControl": "private, no-store, max-age=0",
            },
            "expires": 300,
            "http_method": "GET",
        }
    ]
    assert "versionId=pinned-version-1" in redirect.location
    assert (redirect.expires_at - NOW).total_seconds() == 300

    response = preview_redirect_response(redirect, request_id="request-preview-1")
    assert response["statusCode"] == 302
    assert response["body"] == ""
    assert response["headers"]["Location"] == redirect.location
    assert response["headers"]["Cache-Control"] == "private, no-store, max-age=0"
    assert response["headers"]["Referrer-Policy"] == "no-referrer"


@pytest.mark.parametrize(
    "location",
    (
        (
            "https://evil.test/private/source.png?versionId=pinned-version-1&"
            "response-content-type=image%2Fpng&response-cache-control=private%2C+no-store%2C+"
            "max-age%3D0&X-Amz-Expires=300"
        ),
        (
            f"{ORIGIN}/private/owners/{OWNER}/jobs/other/source/source.png?"
            "versionId=pinned-version-1&response-content-type=image%2Fpng&"
            "response-cache-control=private%2C+no-store%2C+max-age%3D0&X-Amz-Expires=300"
        ),
        (
            f"{ORIGIN}/private/owners/{OWNER}/jobs/{JOB_ID}/source/source.png?"
            "versionId=latest&response-content-type=image%2Fpng&response-cache-control=private%2C+"
            "no-store%2C+max-age%3D0&X-Amz-Expires=300"
        ),
        (
            f"{ORIGIN}/private/owners/{OWNER}/jobs/{JOB_ID}/source/source.png?"
            "versionId=pinned-version-1&response-content-type=image%2Fpng&"
            "response-cache-control=private%2C+no-store%2C+max-age%3D0&X-Amz-Expires=301"
        ),
        (
            f"{ORIGIN}/private/owners/{OWNER}/jobs/{JOB_ID}/source/source.png?"
            "versionId=pinned-version-1&response-cache-control=private%2C+no-store%2C+"
            "max-age%3D0&X-Amz-Expires=300"
        ),
        (
            f"{ORIGIN}/private/owners/{OWNER}/jobs/{JOB_ID}/source/source.png?"
            "versionId=pinned-version-1&response-content-type=image%2Fpng&"
            "response-cache-control=public%2C+max-age%3D300&X-Amz-Expires=300"
        ),
    ),
)
def test_preview_rejects_host_path_version_ttl_and_content_type_drift(location: str) -> None:
    store = FakeStore()
    signer = FakePresigner()
    signer.location = location

    with pytest.raises(PreviewAuthorizationUnavailableError):
        service(store, signer).authorize(owner_id=OWNER, job_id=JOB_ID)


@pytest.mark.parametrize("control", ("\r", "\n", "\t", "\x00", "\x7f", "\\"))
def test_preview_rejects_header_controls_before_and_at_redirect_emission(control: str) -> None:
    store = FakeStore()
    signer = FakePresigner()
    valid = signer.generate_presigned_url(
        "get_object",
        Params={
            "Bucket": BUCKET,
            "Key": source_record().object_key,
            "VersionId": source_record().version_id,
            "ResponseContentType": "image/png",
            "ResponseCacheControl": "private, no-store, max-age=0",
        },
        ExpiresIn=300,
        HttpMethod="GET",
    )
    hostile = f"{valid}{control}X-Injected: yes"
    signer.location = hostile

    with pytest.raises(PreviewAuthorizationUnavailableError):
        service(store, signer).authorize(owner_id=OWNER, job_id=JOB_ID)
    with pytest.raises(PreviewAuthorizationUnavailableError):
        preview_redirect_response(
            PreviewRedirect(location=hostile, expires_at=NOW),
            request_id="request-preview-1",
        )


def test_preview_rejects_source_authority_mismatch_without_presigning() -> None:
    store = FakeStore()
    signer = FakePresigner()
    store.source = store.source.model_copy(update={"version_id": ""})

    with pytest.raises(PreviewAuthorizationUnavailableError):
        service(store, signer).authorize(owner_id=OWNER, job_id=JOB_ID)

    assert signer.calls == []
