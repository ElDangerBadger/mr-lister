from __future__ import annotations

import json
from base64 import b64encode
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from urllib.parse import urlencode

import pytest

from mr_lister.cloud.api import (
    ReviewQueryApiAdapter,
    SellerCommandApiAdapter,
    UploadApiAdapter,
)
from mr_lister.cloud.auth import SellerClaimsPolicy
from mr_lister.cloud.preview import PreviewRedirect
from mr_lister.control.commands import (
    ApproveReviewCommand,
    CancelJobCommand,
    RefreshEconomicsCommand,
    RetryJobCommand,
    ReviseListingCommand,
)
from mr_lister.control.errors import NotFoundError
from mr_lister.control.models import CommandResponse, ControlJobRecord, ControlJobState
from mr_lister.control.projection_models import (
    ActionReason,
    ArtworkInterpretation,
    ArtworkPreview,
    EconomicsProjection,
    EconomicsReadiness,
    ListingProjection,
    ListingValidationProjection,
    MockupSetProjection,
    PlacementPresentation,
    ProductPolicyProjection,
    ProductSynchronizationProjection,
    ReviewDisplayState,
    ReviewStage,
    SectionReadiness,
    SellerAction,
    SellerActionCapability,
    SellerReviewProjection,
    StrandsProvenanceProjection,
)
from mr_lister.control.store import OwnerJobPage, encode_owner_job_cursor
from mr_lister.control.upload_models import (
    UploadAuthorization,
    UploadCommandType,
    UploadIntent,
    UploadIntentStatus,
    UploadReceipt,
)
from mr_lister.control.upload_service import UploadIntakeResult

NOW = datetime(2026, 8, 22, 12, 0, tzinfo=UTC)
ISSUER = "https://cognito-idp.us-west-2.amazonaws.com/us-west-2_phase64"
CLIENT_ID = "phase64publicclient"
SCOPE = "mr-lister-api/seller"
SUBJECT = "seller-phase64-subject"
OWNER = sha256(ISSUER.encode() + b"\0" + SUBJECT.encode()).hexdigest()
OTHER_OWNER = "f" * 64
JOB_ID = "job_phase64_api"
UPLOAD_ID = "upload_phase64_api"
REVIEW_FINGERPRINT = "a" * 64
REVIEW_ETAG = "b" * 64
SOURCE_FINGERPRINT = "c" * 64
CONTENT_SHA256 = "d" * 64
POLICY = SellerClaimsPolicy(
    issuer=ISSUER,
    client_id=CLIENT_ID,
    required_scope=SCOPE,
)

_PRIVATE_RESPONSE_FIELDS = {
    "owner_id",
    "log_owner_digest",
    "receipt_id",
    "work_request_id",
    "active_work_request_id",
    "bucket",
    "object_key",
    "version_id",
    "checksum_sha256_base64",
    "idempotency_key_digest",
    "request_fingerprint",
    "source_artifact_fingerprint",
    "product_profile_fingerprint",
}

_ROUTE_PATHS = {
    "GET /health": "/health",
    "POST /v1/uploads": "/v1/uploads",
    "GET /v1/uploads/{upload_id}": f"/v1/uploads/{UPLOAD_ID}",
    "POST /v1/uploads/{upload_id}/authorize": f"/v1/uploads/{UPLOAD_ID}/authorize",
    "POST /v1/uploads/{upload_id}/complete": f"/v1/uploads/{UPLOAD_ID}/complete",
    "POST /v1/uploads/{upload_id}/cancel": f"/v1/uploads/{UPLOAD_ID}/cancel",
    "GET /v1/jobs": "/v1/jobs",
    "GET /v1/jobs/{job_id}": f"/v1/jobs/{JOB_ID}",
    "GET /v1/jobs/{job_id}/review": f"/v1/jobs/{JOB_ID}/review",
    "PUT /v1/jobs/{job_id}/review/listing": f"/v1/jobs/{JOB_ID}/review/listing",
    "POST /v1/jobs/{job_id}/economics/refresh": f"/v1/jobs/{JOB_ID}/economics/refresh",
    "POST /v1/jobs/{job_id}/approve": f"/v1/jobs/{JOB_ID}/approve",
    "POST /v1/jobs/{job_id}/cancel": f"/v1/jobs/{JOB_ID}/cancel",
    "POST /v1/jobs/{job_id}/retry": f"/v1/jobs/{JOB_ID}/retry",
    "GET /v1/jobs/{job_id}/artwork-preview": f"/v1/jobs/{JOB_ID}/artwork-preview",
}


def api_event(
    route_key: str,
    *,
    body: object | None = None,
    headers: dict[str, object] | None = None,
    query: dict[str, str] | None = None,
    authenticated: bool = True,
    base64_encoded: bool = False,
) -> dict[str, object]:
    request_headers: dict[str, object] = {"authorization": "Bearer browser-secret"}
    request_headers.update(headers or {})
    request_context: dict[str, object] = {
        "requestId": "request-phase64-api",
        "http": {"method": route_key.split(" ", 1)[0]},
    }
    if authenticated:
        request_context["authorizer"] = {
            "jwt": {
                "claims": {
                    "iss": ISSUER,
                    "sub": SUBJECT,
                    "token_use": "access",
                    "client_id": CLIENT_ID,
                    "scope": f"openid {SCOPE}",
                    "cognito:groups": '["seller"]',
                },
                "scopes": [SCOPE],
            }
        }
    event: dict[str, object] = {
        "version": "2.0",
        "routeKey": route_key,
        "rawPath": _ROUTE_PATHS.get(route_key, "/unrecognized"),
        "rawQueryString": urlencode(query or {}),
        "headers": request_headers,
        "queryStringParameters": query or None,
        "requestContext": request_context,
        "isBase64Encoded": base64_encoded,
    }
    if "{upload_id}" in route_key:
        event["pathParameters"] = {"upload_id": UPLOAD_ID}
    elif "{job_id}" in route_key:
        event["pathParameters"] = {"job_id": JOB_ID}
    if body is not None:
        event["body"] = body if isinstance(body, str) else json.dumps(body)
    return event


def response_body(response: dict[str, object]) -> dict[str, object]:
    body = response["body"]
    assert isinstance(body, str)
    return json.loads(body)


def recursive_keys(value: object) -> set[str]:
    if isinstance(value, dict):
        return set(value) | {
            nested_key for nested in value.values() for nested_key in recursive_keys(nested)
        }
    if isinstance(value, list):
        return {nested_key for nested in value for nested_key in recursive_keys(nested)}
    return set()


def job_record(
    *,
    owner_id: str = OWNER,
    job_id: str = JOB_ID,
    updated_at: datetime = NOW,
) -> ControlJobRecord:
    return ControlJobRecord(
        owner_id=owner_id,
        job_id=job_id,
        state=ControlJobState.INTAKE_VALIDATED,
        event_sequence=1,
        source_artifact_fingerprint=SOURCE_FINGERPRINT,
        active_work_request_id=f"work_{job_id}",
        created_at=NOW - timedelta(minutes=1),
        updated_at=updated_at,
    )


def review_projection() -> SellerReviewProjection:
    pending = SectionReadiness.PENDING
    return SellerReviewProjection(
        job_id=JOB_ID,
        record_version=7,
        review_version=3,
        review_fingerprint=REVIEW_FINGERPRINT,
        review_authority_etag=REVIEW_ETAG,
        display_state=ReviewDisplayState.PREPARING,
        stage=ReviewStage.ARTWORK_REVIEW,
        actions=tuple(
            SellerActionCapability(
                action=action,
                enabled=False,
                reason=ActionReason.NOT_IN_CURRENT_STATE,
                message="This action is not available yet.",
            )
            for action in SellerAction
        ),
        preview=ArtworkPreview(readiness=pending),
        artwork=ArtworkInterpretation(readiness=pending),
        listing=ListingProjection(readiness=pending),
        validation=ListingValidationProjection(readiness=pending),
        product_policy=ProductPolicyProjection(
            product_name="Gildan 64000",
            provider_name="SwiftPOD",
            colors=("Black",),
            sizes=("S",),
            placements=(
                PlacementPresentation(
                    group_id="placement_small",
                    sizes=("S",),
                    position="Centered below collar",
                    decoration_method="Direct to garment",
                    x=0.5,
                    y=0.0,
                    scale=0.65,
                    angle=0,
                ),
            ),
            retail_price_cents=2_999,
            buyer_shipping_cents=0,
        ),
        synchronization=ProductSynchronizationProjection(readiness=pending),
        mockups=MockupSetProjection(readiness=pending),
        economics=EconomicsProjection(readiness=EconomicsReadiness.MISSING),
        strands=StrandsProvenanceProjection(readiness=pending),
        created_at=NOW - timedelta(minutes=1),
        updated_at=NOW,
    )


class UploadSpy:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []

    def create_upload(self, **values: object) -> UploadIntakeResult:
        self.calls.append(("create", values))
        return self._result(UploadCommandType.CREATE_UPLOAD, values, authorization=True)

    def authorize_upload(self, **values: object) -> UploadIntakeResult:
        self.calls.append(("authorize", values))
        return self._result(UploadCommandType.REAUTHORIZE_UPLOAD, values, authorization=True)

    def complete_upload(self, **values: object) -> UploadIntakeResult:
        self.calls.append(("complete", values))
        return self._result(UploadCommandType.COMPLETE_UPLOAD, values)

    def cancel_upload(self, **values: object) -> UploadIntakeResult:
        self.calls.append(("cancel", values))
        return self._result(UploadCommandType.CANCEL_UPLOAD, values)

    def get_upload(self, *, owner_id: str, upload_id: str) -> UploadIntent:
        self.calls.append(("get", {"owner_id": owner_id, "upload_id": upload_id}))
        return upload_intent(owner_id=owner_id, upload_id=upload_id)

    @staticmethod
    def _result(
        command_type: UploadCommandType,
        values: dict[str, object],
        *,
        authorization: bool = False,
    ) -> UploadIntakeResult:
        owner_id = values["owner_id"]
        assert isinstance(owner_id, str)
        upload_id = values.get("upload_id", UPLOAD_ID)
        assert isinstance(upload_id, str)
        status = {
            UploadCommandType.CREATE_UPLOAD: UploadIntentStatus.OPEN,
            UploadCommandType.REAUTHORIZE_UPLOAD: UploadIntentStatus.OPEN,
            UploadCommandType.COMPLETE_UPLOAD: UploadIntentStatus.COMPLETED,
            UploadCommandType.CANCEL_UPLOAD: UploadIntentStatus.CANCELLED,
        }[command_type]
        receipt = UploadReceipt(
            receipt_id=f"receipt_{command_type.value}",
            owner_id=owner_id,
            upload_id=upload_id,
            job_id=JOB_ID,
            command_type=command_type,
            idempotency_key_digest="e" * 64,
            request_fingerprint="f" * 64,
            status=status,
            record_version=1,
            work_request_id=("work_prepare" if status is UploadIntentStatus.COMPLETED else None),
            created_at=NOW,
        )
        form = None
        if authorization:
            checksum = b64encode(bytes.fromhex(CONTENT_SHA256)).decode("ascii")
            form = UploadAuthorization(
                owner_id=owner_id,
                upload_id=upload_id,
                job_id=JOB_ID,
                authorization_generation=1,
                url="https://phase64-bucket.s3.us-west-2.amazonaws.com/",
                form_fields={
                    "key": f"private/owners/{owner_id}/jobs/{JOB_ID}/source/source.png",
                    "Content-Type": "image/png",
                    "x-amz-checksum-algorithm": "SHA256",
                    "x-amz-checksum-sha256": checksum,
                    "x-amz-server-side-encryption": "AES256",
                    "x-amz-tagging": "mr-lister-state=staged",
                    "x-amz-algorithm": "AWS4-HMAC-SHA256",
                    "x-amz-credential": "ephemeral-credential",
                    "x-amz-date": "20260822T120000Z",
                    "policy": "ephemeral-policy",
                    "x-amz-signature": "ephemeral-signature",
                },
                content_sha256=CONTENT_SHA256,
                size_bytes=1_024,
                issued_at=NOW,
                expires_at=NOW + timedelta(minutes=5),
            )
        return UploadIntakeResult(receipt=receipt, authorization=form)


def upload_intent(
    *,
    owner_id: str = OWNER,
    upload_id: str = UPLOAD_ID,
) -> UploadIntent:
    return UploadIntent(
        owner_id=owner_id,
        upload_id=upload_id,
        job_id=JOB_ID,
        filename="artwork.png",
        content_type="image/png",
        content_sha256=CONTENT_SHA256,
        size_bytes=1_024,
        bucket="phase64-private-artifacts",
        object_key=f"private/owners/{owner_id}/jobs/{JOB_ID}/source/source.png",
        product_profile_id="gildan_64000_swiftpod",
        product_profile_version=1,
        product_profile_fingerprint="e" * 64,
        authorization_generation=1,
        authorization_issued_at=NOW,
        authorization_expires_at=NOW + timedelta(minutes=5),
        intent_expires_at=NOW + timedelta(days=1),
        created_at=NOW,
        updated_at=NOW,
    )


class QueryStoreSpy:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []
        self.jobs = (
            job_record(),
            job_record(job_id="job_older", updated_at=NOW - timedelta(minutes=1)),
        )

    def list_jobs_for_owner(
        self,
        owner_id: str,
        *,
        limit: int = 25,
        cursor: str | None = None,
    ) -> OwnerJobPage:
        self.calls.append(("list", (owner_id, limit, cursor)))
        return OwnerJobPage(jobs=self.jobs[:limit])

    def get_job_for_owner(self, owner_id: str, job_id: str) -> ControlJobRecord:
        self.calls.append(("get", (owner_id, job_id)))
        return self.jobs[0]


@dataclass(frozen=True)
class ProjectionStub:
    owner_id: str = OTHER_OWNER

    def model_dump(self, *, mode: str) -> dict[str, object]:
        assert mode == "json"
        payload = review_projection().model_dump(mode="json")
        payload["owner_id"] = self.owner_id
        return payload


class ReviewSpy:
    def __init__(self, projection: SellerReviewProjection | ProjectionStub | None = None) -> None:
        self.calls: list[tuple[str, str]] = []
        self.projection = projection or review_projection()

    def get(self, *, owner_id: str, job_id: str) -> SellerReviewProjection | ProjectionStub:
        self.calls.append((owner_id, job_id))
        return self.projection


class PreviewSpy:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def authorize(self, *, owner_id: str, job_id: str) -> PreviewRedirect:
        self.calls.append((owner_id, job_id))
        return PreviewRedirect(
            location=(
                "https://phase64-bucket.s3.us-west-2.amazonaws.com/source.png?"
                "versionId=version-1&X-Amz-Expires=300&X-Amz-Signature=signed"
            ),
            expires_at=NOW + timedelta(minutes=5),
        )


class CommandSpy:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []

    def revise_listing(self, command: ReviseListingCommand) -> CommandResponse:
        return self._respond("revise", command)

    def refresh_economics(self, command: RefreshEconomicsCommand) -> CommandResponse:
        return self._respond("refresh", command)

    def approve_review(self, command: ApproveReviewCommand) -> CommandResponse:
        return self._respond("approve", command)

    def cancel_job(self, command: CancelJobCommand) -> CommandResponse:
        return self._respond("cancel", command)

    def retry_job(self, command: RetryJobCommand) -> CommandResponse:
        return self._respond("retry", command)

    def _respond(self, name: str, command: object) -> CommandResponse:
        self.calls.append((name, command))
        return CommandResponse(
            job_id=JOB_ID,
            state=ControlJobState.AWAITING_APPROVAL,
            record_version=8,
            review_version=3,
            work_request_id=None,
        )


def upload_adapter(spy: UploadSpy) -> UploadApiAdapter:
    return UploadApiAdapter(claims_policy=POLICY, uploads=spy)


def query_adapter(
    store: QueryStoreSpy,
    reviews: ReviewSpy,
    previews: PreviewSpy,
) -> ReviewQueryApiAdapter:
    return ReviewQueryApiAdapter(
        claims_policy=POLICY,
        store=store,
        reviews=reviews,
        previews=previews,
    )


def command_adapter(spy: CommandSpy) -> SellerCommandApiAdapter:
    return SellerCommandApiAdapter(claims_policy=POLICY, commands=spy)


def create_upload_body() -> dict[str, object]:
    return {
        "filename": "artwork.png",
        "content_type": "image/png",
        "content_sha256": CONTENT_SHA256,
        "size_bytes": 1_024,
    }


def review_authority_body() -> dict[str, object]:
    return {
        "expected_record_version": 7,
        "expected_review_version": 3,
        "expected_review_fingerprint": REVIEW_FINGERPRINT,
    }


def test_authentication_precedes_upload_parsing_and_service_invocation() -> None:
    spy = UploadSpy()
    event = api_event(
        "POST /v1/uploads",
        body="not-json",
        headers={"content-type": "application/json", "idempotency-key": "create-1"},
        authenticated=False,
    )

    response = upload_adapter(spy).handle(event)

    assert response["statusCode"] == 401
    assert response_body(response)["error"]["code"] == "AUTHENTICATION_REQUIRED"
    assert spy.calls == []


def test_create_upload_uses_only_jwt_owner_and_returns_a_minimal_ephemeral_form() -> None:
    spy = UploadSpy()
    response = upload_adapter(spy)(
        api_event(
            "POST /v1/uploads",
            body=create_upload_body(),
            headers={"Content-Type": "application/json", "Idempotency-Key": "create-1"},
        )
    )

    assert response["statusCode"] == 201
    assert response["headers"]["Cache-Control"] == "no-store"
    assert spy.calls == [
        (
            "create",
            {
                "owner_id": OWNER,
                "idempotency_key": "create-1",
                **create_upload_body(),
            },
        )
    ]
    payload = response_body(response)
    assert payload["upload"] == {
        "upload_id": UPLOAD_ID,
        "job_id": JOB_ID,
        "status": "open",
        "record_version": 1,
    }
    authorization = payload["authorization"]
    assert authorization["method"] == "POST"
    assert authorization["form_fields"]["key"].endswith(f"/{JOB_ID}/source/source.png")
    assert recursive_keys(payload).isdisjoint(_PRIVATE_RESPONSE_FIELDS)
    # The exact direct-S3 form is the sole public location for the reserved object key and its
    # checksum binding; neither is copied from a durable receipt or storage record.
    assert "key" not in recursive_keys(payload["upload"])
    assert authorization["form_fields"]["x-amz-checksum-sha256"] == b64encode(
        bytes.fromhex(CONTENT_SHA256)
    ).decode("ascii")
    serialized = response["body"]
    assert "owner_id" not in serialized
    assert "receipt_id" not in serialized
    assert "idempotency_key_digest" not in serialized
    assert "browser-secret" not in serialized


@pytest.mark.parametrize(
    ("body", "base64_encoded", "expected_status"),
    (
        ({**create_upload_body(), "owner_id": OTHER_OWNER}, False, 422),
        ('{"filename":"one.png","filename":"two.png"}', False, 400),
        (create_upload_body(), True, 400),
    ),
)
def test_create_upload_rejects_identity_extra_duplicate_json_and_base64_body(
    body: object,
    base64_encoded: bool,
    expected_status: int,
) -> None:
    spy = UploadSpy()
    response = upload_adapter(spy).handle(
        api_event(
            "POST /v1/uploads",
            body=body,
            headers={"content-type": "application/json", "idempotency-key": "create-1"},
            base64_encoded=base64_encoded,
        )
    )

    assert response["statusCode"] == expected_status
    assert spy.calls == []


def test_request_validation_exposes_only_bounded_safe_field_metadata() -> None:
    spy = UploadSpy()
    private_value = "not-a-digest-private-value"
    response = upload_adapter(spy).handle(
        api_event(
            "POST /v1/uploads",
            body={
                "filename": "artwork.png",
                "content_type": "image/png",
                "content_sha256": private_value,
                "size_bytes": 0,
                **{f"extra_{index}": "private" for index in range(30)},
            },
            headers={"Content-Type": "application/json", "Idempotency-Key": "create-1"},
        )
    )

    assert response["statusCode"] == 422
    payload = response_body(response)
    assert payload["error"]["code"] == "VALIDATION_FAILED"
    assert len(payload["error"]["fields"]) == 25
    assert payload["error"]["fields"][:2] == [
        {
            "path": "$.content_sha256",
            "code": "INVALID_FORMAT",
            "message": "Use the required format.",
        },
        {
            "path": "$.size_bytes",
            "code": "OUT_OF_RANGE",
            "message": "Use a value within the allowed range.",
        },
    ]
    assert private_value not in response["body"]
    assert "private" not in response["body"]
    assert spy.calls == []


@pytest.mark.parametrize(
    ("route", "operation", "status", "has_authorization"),
    (
        ("POST /v1/uploads/{upload_id}/authorize", "authorize", 200, True),
        ("POST /v1/uploads/{upload_id}/complete", "complete", 202, False),
        ("POST /v1/uploads/{upload_id}/cancel", "cancel", 200, False),
    ),
)
def test_upload_followup_routes_are_owner_scoped_and_bodyless(
    route: str,
    operation: str,
    status: int,
    has_authorization: bool,
) -> None:
    spy = UploadSpy()
    response = upload_adapter(spy).handle(
        api_event(route, headers={"Idempotency-Key": f"{operation}-1"})
    )

    assert response["statusCode"] == status
    assert spy.calls == [
        (
            operation,
            {
                "owner_id": OWNER,
                "upload_id": UPLOAD_ID,
                "idempotency_key": f"{operation}-1",
            },
        )
    ]
    payload = response_body(response)
    assert (payload["authorization"] is not None) is has_authorization
    assert recursive_keys(payload).isdisjoint(_PRIVATE_RESPONSE_FIELDS)


def test_upload_followup_rejects_any_request_body_before_service() -> None:
    spy = UploadSpy()
    response = upload_adapter(spy).handle(
        api_event(
            "POST /v1/uploads/{upload_id}/complete",
            body={"owner_id": OTHER_OWNER},
            headers={"Idempotency-Key": "complete-1", "Content-Type": "application/json"},
        )
    )

    assert response["statusCode"] == 400
    assert spy.calls == []


def test_upload_recovery_is_owner_scoped_bodyless_and_contains_no_object_authority() -> None:
    spy = UploadSpy()
    response = upload_adapter(spy).handle(api_event("GET /v1/uploads/{upload_id}"))

    assert response["statusCode"] == 200
    assert spy.calls == [("get", {"owner_id": OWNER, "upload_id": UPLOAD_ID})]
    payload = response_body(response)
    assert payload == {
        "authorization_expires_at": "2026-08-22T12:05:00Z",
        "cancelled_at": None,
        "completed_at": None,
        "content_type": "image/png",
        "created_at": "2026-08-22T12:00:00Z",
        "expired_at": None,
        "filename": "artwork.png",
        "intent_expires_at": "2026-08-23T12:00:00Z",
        "job_id": JOB_ID,
        "record_version": 0,
        "size_bytes": 1_024,
        "status": "open",
        "updated_at": "2026-08-22T12:00:00Z",
        "upload_id": UPLOAD_ID,
    }
    assert recursive_keys(payload).isdisjoint(_PRIVATE_RESPONSE_FIELDS)
    assert "content_sha256" not in response["body"]
    assert "phase64-private-artifacts" not in response["body"]
    assert "source.png" not in response["body"]


def test_unknown_and_cross_owner_upload_recovery_are_identical_not_found() -> None:
    class ClosedUploadSpy(UploadSpy):
        def __init__(self, intent: UploadIntent | None) -> None:
            super().__init__()
            self.intent = intent

        def get_upload(self, *, owner_id: str, upload_id: str) -> UploadIntent:
            self.calls.append(("get", {"owner_id": owner_id, "upload_id": upload_id}))
            if self.intent is None or self.intent.owner_id != owner_id:
                raise NotFoundError("private storage detail")
            return self.intent

    unknown = upload_adapter(ClosedUploadSpy(None)).handle(api_event("GET /v1/uploads/{upload_id}"))
    foreign = upload_adapter(ClosedUploadSpy(upload_intent(owner_id=OTHER_OWNER))).handle(
        api_event("GET /v1/uploads/{upload_id}")
    )

    assert unknown["statusCode"] == foreign["statusCode"] == 404
    assert unknown["body"] == foreign["body"]
    assert response_body(unknown)["error"]["code"] == "NOT_FOUND"
    assert "private storage detail" not in unknown["body"]
    assert OTHER_OWNER not in foreign["body"]


@pytest.mark.parametrize("mutation", ("body", "query"))
def test_upload_recovery_rejects_body_or_query_before_read(mutation: str) -> None:
    spy = UploadSpy()
    event = api_event("GET /v1/uploads/{upload_id}")
    if mutation == "body":
        event["body"] = "{}"
    else:
        event["queryStringParameters"] = {"owner_id": OTHER_OWNER}
        event["rawQueryString"] = f"owner_id={OTHER_OWNER}"

    response = upload_adapter(spy).handle(event)

    assert response["statusCode"] == 400
    assert spy.calls == []


def test_recent_jobs_uses_owner_index_pagination_and_exposes_no_internal_authority() -> None:
    store = QueryStoreSpy()
    reviews = ReviewSpy()
    previews = PreviewSpy()
    cursor = encode_owner_job_cursor(store.jobs[1])
    response = query_adapter(store, reviews, previews).handle(
        api_event("GET /v1/jobs", query={"limit": "2", "cursor": cursor})
    )

    assert response["statusCode"] == 200
    assert store.calls == [("list", (OWNER, 2, cursor))]
    payload = response_body(response)
    assert [job["job_id"] for job in payload["jobs"]] == [JOB_ID, "job_older"]
    assert payload["jobs"][0]["state"] == "intake_validated"
    assert "source_artifact_fingerprint" not in response["body"]
    assert "active_work_request_id" not in response["body"]
    assert OWNER not in response["body"]
    assert recursive_keys(payload).isdisjoint(_PRIVATE_RESPONSE_FIELDS)


@pytest.mark.parametrize(
    "query",
    (
        {"limit": "0"},
        {"limit": "101"},
        {"cursor": "not-a-canonical-cursor"},
        {"owner_id": OTHER_OWNER},
    ),
)
def test_recent_jobs_rejects_invalid_or_identity_bearing_query(
    query: dict[str, str],
) -> None:
    store = QueryStoreSpy()
    response = query_adapter(store, ReviewSpy(), PreviewSpy()).handle(
        api_event("GET /v1/jobs", query=query)
    )

    assert response["statusCode"] == 400
    assert store.calls == []


def test_recent_jobs_rejects_duplicate_raw_query_parameter() -> None:
    store = QueryStoreSpy()
    event = api_event("GET /v1/jobs", query={"limit": "2"})
    event["rawQueryString"] = "limit=1&limit=2"

    response = query_adapter(store, ReviewSpy(), PreviewSpy()).handle(event)

    assert response["statusCode"] == 400
    assert store.calls == []


def test_job_status_read_uses_server_derived_progress_and_exact_owner() -> None:
    store = QueryStoreSpy()
    reviews = ReviewSpy()
    response = query_adapter(store, reviews, PreviewSpy()).handle(
        api_event("GET /v1/jobs/{job_id}")
    )

    assert response["statusCode"] == 200
    assert store.calls == []
    assert reviews.calls == [(OWNER, JOB_ID)]
    payload = response_body(response)
    assert payload["job_id"] == JOB_ID
    assert payload["display_state"] == "preparing"
    assert payload["stage"] == "artwork_review"
    assert payload["authority_notice"] == "Unpublished — not on Etsy"
    assert [action["action"] for action in payload["actions"]] == [
        action.value for action in SellerAction
    ]
    assert "state" not in payload
    assert "listing" not in payload
    assert recursive_keys(payload).isdisjoint(_PRIVATE_RESPONSE_FIELDS)


def test_job_status_rejects_route_path_disagreement_without_store_read() -> None:
    store = QueryStoreSpy()
    reviews = ReviewSpy()
    event = api_event("GET /v1/jobs/{job_id}")
    event["rawPath"] = "/v1/jobs/a-different-job"

    response = query_adapter(store, reviews, PreviewSpy()).handle(event)

    assert response["statusCode"] == 400
    assert store.calls == []
    assert reviews.calls == []


def test_review_read_returns_strong_etag_and_owner_safe_projection() -> None:
    reviews = ReviewSpy()
    response = query_adapter(QueryStoreSpy(), reviews, PreviewSpy()).handle(
        api_event("GET /v1/jobs/{job_id}/review")
    )

    assert response["statusCode"] == 200
    assert response["headers"]["ETag"] == f'"{REVIEW_ETAG}"'
    assert reviews.calls == [(OWNER, JOB_ID)]
    payload = response_body(response)
    assert payload["authority_notice"] == "Unpublished — not on Etsy"
    assert recursive_keys(payload).isdisjoint(_PRIVATE_RESPONSE_FIELDS)


def test_review_read_reconstructs_exact_schema_and_rejects_custom_private_fields() -> None:
    reviews = ReviewSpy(ProjectionStub())

    response = query_adapter(QueryStoreSpy(), reviews, PreviewSpy()).handle(
        api_event("GET /v1/jobs/{job_id}/review")
    )

    assert response["statusCode"] == 500
    assert response_body(response)["error"]["code"] == "INTERNAL_ERROR"
    assert "owner_id" not in response["body"]
    assert OTHER_OWNER not in response["body"]
    assert reviews.calls == [(OWNER, JOB_ID)]


def test_preview_route_returns_only_bodyless_no_referrer_redirect() -> None:
    previews = PreviewSpy()
    response = query_adapter(QueryStoreSpy(), ReviewSpy(), previews).handle(
        api_event("GET /v1/jobs/{job_id}/artwork-preview")
    )

    assert response["statusCode"] == 302
    assert response["body"] == ""
    assert response["headers"]["Cache-Control"] == "private, no-store, max-age=0"
    assert response["headers"]["Referrer-Policy"] == "no-referrer"
    assert previews.calls == [(OWNER, JOB_ID)]


def test_health_is_information_minimal_and_requires_no_identity() -> None:
    store = QueryStoreSpy()
    reviews = ReviewSpy()
    previews = PreviewSpy()
    response = query_adapter(store, reviews, previews).handle(
        api_event("GET /health", authenticated=False)
    )

    assert response["statusCode"] == 200
    assert response_body(response) == {"status": "ok"}
    assert store.calls == []
    assert reviews.calls == []
    assert previews.calls == []


def test_review_sensitive_commands_bind_path_owner_body_authority_and_if_match() -> None:
    spy = CommandSpy()
    body = {
        **review_authority_body(),
        "listing": {
            "title": "A seller title",
            "description": "A seller description",
            "tags": [f"tag {index}" for index in range(13)],
        },
    }
    response = command_adapter(spy).handle(
        api_event(
            "PUT /v1/jobs/{job_id}/review/listing",
            body=body,
            headers={
                "Content-Type": "application/json",
                "Idempotency-Key": "revise-1",
                "If-Match": f'"{REVIEW_ETAG}"',
            },
        )
    )

    assert response["statusCode"] == 200
    assert response_body(response) == {
        "job_id": JOB_ID,
        "state": "awaiting_approval",
        "record_version": 8,
        "review_version": 3,
    }
    assert recursive_keys(response_body(response)).isdisjoint(_PRIVATE_RESPONSE_FIELDS)
    assert len(spy.calls) == 1
    name, command = spy.calls[0]
    assert name == "revise"
    assert isinstance(command, ReviseListingCommand)
    assert command.owner_id == OWNER
    assert command.job_id == JOB_ID
    assert command.expected_review_etag == REVIEW_ETAG
    assert command.idempotency_key == "revise-1"
    assert command.revision.tags == tuple(body["listing"]["tags"])


@pytest.mark.parametrize(
    ("route", "operation", "command_type"),
    (
        (
            "POST /v1/jobs/{job_id}/economics/refresh",
            "refresh",
            RefreshEconomicsCommand,
        ),
        ("POST /v1/jobs/{job_id}/approve", "approve", ApproveReviewCommand),
    ),
)
def test_review_commands_require_strong_etag(
    route: str,
    operation: str,
    command_type: type[RefreshEconomicsCommand] | type[ApproveReviewCommand],
) -> None:
    spy = CommandSpy()
    response = command_adapter(spy).handle(
        api_event(
            route,
            body=review_authority_body(),
            headers={
                "Content-Type": "application/json",
                "Idempotency-Key": f"{operation}-1",
                "If-Match": f'"{REVIEW_ETAG}"',
            },
        )
    )

    assert response["statusCode"] == 200
    assert len(spy.calls) == 1
    assert spy.calls[0][0] == operation
    command = spy.calls[0][1]
    assert isinstance(command, command_type)
    assert command.owner_id == OWNER
    assert command.expected_review_etag == REVIEW_ETAG


@pytest.mark.parametrize(
    ("route", "operation", "command_type"),
    (
        ("POST /v1/jobs/{job_id}/cancel", "cancel", CancelJobCommand),
        ("POST /v1/jobs/{job_id}/retry", "retry", RetryJobCommand),
    ),
)
def test_record_version_commands_expose_no_state_or_recovery_choice(
    route: str,
    operation: str,
    command_type: type[CancelJobCommand] | type[RetryJobCommand],
) -> None:
    spy = CommandSpy()
    response = command_adapter(spy).handle(
        api_event(
            route,
            body={"expected_record_version": 7},
            headers={
                "Content-Type": "application/json",
                "Idempotency-Key": f"{operation}-1",
            },
        )
    )

    assert response["statusCode"] == 200
    assert len(spy.calls) == 1
    command = spy.calls[0][1]
    assert isinstance(command, command_type)
    assert command.owner_id == OWNER
    assert command.job_id == JOB_ID
    assert command.expected_record_version == 7


def test_command_body_cannot_override_owner_job_or_idempotency_authority() -> None:
    spy = CommandSpy()
    response = command_adapter(spy).handle(
        api_event(
            "POST /v1/jobs/{job_id}/cancel",
            body={
                "expected_record_version": 7,
                "owner_id": OTHER_OWNER,
                "job_id": "another-job",
                "idempotency_key": "body-key",
                "state": "approved",
            },
            headers={
                "Content-Type": "application/json",
                "Idempotency-Key": "header-key",
            },
        )
    )

    assert response["statusCode"] == 422
    assert spy.calls == []


def test_missing_review_if_match_returns_precondition_required_without_command() -> None:
    spy = CommandSpy()
    response = command_adapter(spy).handle(
        api_event(
            "POST /v1/jobs/{job_id}/approve",
            body=review_authority_body(),
            headers={
                "Content-Type": "application/json",
                "Idempotency-Key": "approve-1",
            },
        )
    )

    assert response["statusCode"] == 428
    assert spy.calls == []


def test_unrecognized_publication_route_is_closed_before_any_service() -> None:
    upload = UploadSpy()
    event = api_event("POST /v1/jobs/{job_id}/publish")
    response = upload_adapter(upload).handle(event)

    assert response["statusCode"] == 404
    assert upload.calls == []
    assert "publish" not in response["body"]


def test_adapter_fails_closed_if_upload_service_returns_another_owner() -> None:
    class MismatchedUploadSpy(UploadSpy):
        def complete_upload(self, **values: object) -> UploadIntakeResult:
            return self._result(
                UploadCommandType.COMPLETE_UPLOAD,
                {**values, "owner_id": OTHER_OWNER},
            )

    response = upload_adapter(MismatchedUploadSpy()).handle(
        api_event(
            "POST /v1/uploads/{upload_id}/complete",
            headers={"Idempotency-Key": "complete-1"},
        )
    )

    assert response["statusCode"] == 500
    assert response_body(response)["error"]["code"] == "INTERNAL_ERROR"
    assert OTHER_OWNER not in response["body"]


def test_adapter_fails_closed_if_owner_index_returns_a_foreign_job() -> None:
    store = QueryStoreSpy()
    store.jobs = (job_record(owner_id=OTHER_OWNER, job_id="job_foreign"),)

    response = query_adapter(store, ReviewSpy(), PreviewSpy()).handle(api_event("GET /v1/jobs"))

    assert response["statusCode"] == 500
    assert response_body(response)["error"]["code"] == "INTERNAL_ERROR"
    assert "job_foreign" not in response["body"]


def test_adapter_fails_closed_if_command_service_returns_a_different_job() -> None:
    class MismatchedCommandSpy(CommandSpy):
        def _respond(self, name: str, command: object) -> CommandResponse:
            self.calls.append((name, command))
            return CommandResponse(
                job_id="job_foreign",
                state=ControlJobState.AWAITING_APPROVAL,
                record_version=8,
                review_version=3,
            )

    response = command_adapter(MismatchedCommandSpy()).handle(
        api_event(
            "POST /v1/jobs/{job_id}/cancel",
            body={"expected_record_version": 7},
            headers={
                "Content-Type": "application/json",
                "Idempotency-Key": "cancel-1",
            },
        )
    )

    assert response["statusCode"] == 500
    assert response_body(response)["error"]["code"] == "INTERNAL_ERROR"
    assert "job_foreign" not in response["body"]
