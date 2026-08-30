"""Unregistered Phase 7.7 owner-scoped publication request adapter tests."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from mr_lister.cloud.auth import AccessDeniedError, AuthenticationRequiredError
from mr_lister.control.errors import NotFoundError
from mr_lister.control.models import ControlJobRecord, ControlJobState
from mr_lister.publication.commands import (
    PublicationRequestResponse,
    RequestPublicationCommand,
)
from mr_lister.publication.errors import (
    PublicationAuthorityError,
    PublicationConflictError,
    PublicationErrorCode,
    PublicationIdempotencyConflictError,
    PublicationNotFoundError,
)
from mr_lister.publication.request_api import (
    PUBLICATION_REQUEST_ROUTE,
    PublicationRequestApiAdapter,
)

OWNER_ID = "1" * 64
OTHER_OWNER_ID = "2" * 64
JOB_ID = "job_phase77_request"
REVIEW_FINGERPRINT = "a" * 64
APPROVAL_HEADER_FINGERPRINT = "b" * 64
APPROVAL_DECISION_ID = "decision_phase77"
NOW = datetime(2026, 8, 30, 12, 0, tzinfo=UTC)


def _event(
    *,
    job_id: str = JOB_ID,
    body: object | None = None,
    headers: Mapping[str, object] | None = None,
) -> dict[str, Any]:
    path = f"/v1/jobs/{job_id}/publish"
    request_body = body or {
        "expected_record_version": 7,
        "expected_review_version": 3,
        "expected_review_fingerprint": REVIEW_FINGERPRINT,
        "confirmation": "publish_exact_approved_listing",
    }
    return {
        "version": "2.0",
        "routeKey": PUBLICATION_REQUEST_ROUTE,
        "rawPath": path,
        "rawQueryString": "",
        "queryStringParameters": None,
        "pathParameters": {"job_id": job_id},
        "headers": {
            "content-type": "application/json",
            "if-match": f'"{APPROVAL_HEADER_FINGERPRINT}"',
            "idempotency-key": "publish.phase77-1",
            **dict(headers or {}),
        },
        "requestContext": {
            "requestId": "request-phase77",
            "http": {"method": "POST", "path": path},
        },
        "body": request_body if isinstance(request_body, str) else json.dumps(request_body),
        "isBase64Encoded": False,
    }


def _approved_job(
    *,
    owner_id: str = OWNER_ID,
    job_id: str = JOB_ID,
    state: ControlJobState = ControlJobState.APPROVED,
) -> ControlJobRecord:
    values: dict[str, object] = {
        "owner_id": owner_id,
        "job_id": job_id,
        "record_version": 7,
        "event_sequence": 8,
        "state": state,
        "review_version": 3,
        "review_fingerprint": REVIEW_FINGERPRINT,
        "review_validated": True,
        "product_id": "product_phase77",
        "product_sync_id": "sync_phase77",
        "synchronized_review_version": 3,
        "product_sync_fingerprint": "c" * 64,
        "pricing_snapshot_id": "pricing_phase77",
        "pricing_snapshot_fingerprint": "d" * 64,
        "approval_decision_id": APPROVAL_DECISION_ID,
        "approved_review_version": 3,
        "approved_review_fingerprint": REVIEW_FINGERPRINT,
        "approval_fingerprint": "e" * 64,
        "created_at": NOW - timedelta(minutes=5),
        "updated_at": NOW,
    }
    if state is not ControlJobState.APPROVED:
        for field in (
            "approval_decision_id",
            "approved_review_version",
            "approved_review_fingerprint",
            "approval_fingerprint",
        ):
            values.pop(field)
    return ControlJobRecord(**values)


def _response(*, job_id: str = JOB_ID) -> PublicationRequestResponse:
    return PublicationRequestResponse(
        job_id=job_id,
        publication_aggregate_id="publication_phase77",
        record_version=7,
        review_version=3,
        work_request_id="work_phase77",
        requested_at=NOW,
        verification_deadline=NOW + timedelta(minutes=30),
    )


@dataclass
class Authenticator:
    result: str | Exception = OWNER_ID
    calls: int = 0

    def authenticate(self, event: Mapping[str, Any]) -> str:
        self.calls += 1
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


@dataclass
class Approvals:
    result: ControlJobRecord | Exception
    calls: int = 0
    last: tuple[str, str] | None = None

    def get_job_for_owner(self, owner_id: str, job_id: str) -> ControlJobRecord:
        self.calls += 1
        self.last = (owner_id, job_id)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


@dataclass
class Requests:
    result: PublicationRequestResponse | Exception
    calls: int = 0
    command: RequestPublicationCommand | None = None

    def request_publication(
        self,
        command: RequestPublicationCommand,
    ) -> PublicationRequestResponse:
        self.calls += 1
        self.command = command
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def _adapter(
    *,
    auth: Authenticator | None = None,
    approvals: Approvals | None = None,
    requests: Requests | None = None,
) -> tuple[PublicationRequestApiAdapter, Authenticator, Approvals, Requests]:
    resolved_auth = auth or Authenticator()
    resolved_approvals = approvals or Approvals(_approved_job())
    resolved_requests = requests or Requests(_response())
    return (
        PublicationRequestApiAdapter(
            authenticator=resolved_auth,
            approvals=resolved_approvals,
            requests=resolved_requests,
        ),
        resolved_auth,
        resolved_approvals,
        resolved_requests,
    )


def _body(response: Mapping[str, Any]) -> dict[str, Any]:
    value = response["body"]
    assert isinstance(value, str)
    decoded = json.loads(value)
    assert isinstance(decoded, dict)
    return decoded


def test_exact_request_resolves_server_approval_and_returns_202_no_store() -> None:
    adapter, auth, approvals, requests = _adapter()

    result = adapter(_event())

    assert result["statusCode"] == 202
    assert result["headers"] == {
        "cache-control": "no-store",
        "content-type": "application/json",
        "x-content-type-options": "nosniff",
        "x-request-id": "request-phase77",
    }
    assert _body(result) == _response().model_dump(mode="json")
    assert auth.calls == approvals.calls == requests.calls == 1
    assert approvals.last == (OWNER_ID, JOB_ID)
    assert requests.command == RequestPublicationCommand(
        owner_id=OWNER_ID,
        job_id=JOB_ID,
        expected_record_version=7,
        expected_review_version=3,
        expected_review_fingerprint=REVIEW_FINGERPRINT,
        expected_review_etag=APPROVAL_HEADER_FINGERPRINT,
        expected_approval_decision_id=APPROVAL_DECISION_ID,
        expected_approval_fingerprint=APPROVAL_HEADER_FINGERPRINT,
        confirmation="publish_exact_approved_listing",
        idempotency_key="publish.phase77-1",
    )


@pytest.mark.parametrize(
    "browser_authority",
    [
        {"approval_decision_id": "browser_forgery"},
        {"expected_approval_decision_id": "browser_forgery"},
        {"expected_approval_fingerprint": "f" * 64},
        {"expected_review_etag": "f" * 64},
    ],
)
def test_browser_cannot_supply_server_side_authority(
    browser_authority: dict[str, object],
) -> None:
    body = json.loads(_event()["body"])
    body.update(browser_authority)
    adapter, _auth, approvals, requests = _adapter()

    result = adapter(_event(body=body))

    assert result["statusCode"] == 422
    assert _body(result)["error"]["code"] == "VALIDATION_FAILED"
    assert approvals.calls == requests.calls == 0
    assert "browser_forgery" not in result["body"]


class AuthenticationOrderedEvent(dict[str, Any]):
    authenticated = False

    def get(self, key: str, default: object = None) -> object:
        if (
            key
            in {
                "routeKey",
                "rawPath",
                "pathParameters",
                "rawQueryString",
                "queryStringParameters",
                "headers",
                "body",
            }
            and not self.authenticated
        ):
            raise AssertionError("request material read before authentication")
        return super().get(key, default)


class OrderingAuthenticator:
    def authenticate(self, event: Mapping[str, Any]) -> str:
        assert isinstance(event, AuthenticationOrderedEvent)
        event.authenticated = True
        return OWNER_ID


def test_authentication_precedes_route_path_query_header_and_body_reads() -> None:
    event = AuthenticationOrderedEvent(_event())
    adapter = PublicationRequestApiAdapter(
        authenticator=OrderingAuthenticator(),
        approvals=Approvals(_approved_job()),
        requests=Requests(_response()),
    )

    assert adapter(event)["statusCode"] == 202


@pytest.mark.parametrize(
    ("auth_error", "status", "code"),
    [
        (AuthenticationRequiredError(), 401, "AUTHENTICATION_REQUIRED"),
        (AccessDeniedError(), 403, "FORBIDDEN"),
    ],
)
def test_authentication_errors_do_not_read_request_or_authority(
    auth_error: Exception,
    status: int,
    code: str,
) -> None:
    event = AuthenticationOrderedEvent(_event())
    approvals = Approvals(_approved_job())
    requests = Requests(_response())
    adapter, _auth, _approvals, _requests = _adapter(
        auth=Authenticator(auth_error),
        approvals=approvals,
        requests=requests,
    )

    result = adapter(event)

    assert result["statusCode"] == status
    assert _body(result)["error"]["code"] == code
    assert approvals.calls == requests.calls == 0


@pytest.mark.parametrize(
    "mutate",
    [
        lambda event: event.update(version="1.0"),
        lambda event: event.update(rawPath="/v1/jobs/wrong/publish"),
        lambda event: event["requestContext"]["http"].update(method="GET"),
        lambda event: event.update(pathParameters={"job_id": JOB_ID, "owner_id": OWNER_ID}),
        lambda event: event.update(rawQueryString="force=true"),
        lambda event: event.update(queryStringParameters={"force": "true"}),
        lambda event: event.update(isBase64Encoded=True),
    ],
)
def test_nonexact_http_request_fails_closed_before_authority(
    mutate: Any,
) -> None:
    event = _event()
    mutate(event)
    adapter, auth, approvals, requests = _adapter()

    result = adapter(event)

    assert result["statusCode"] == 400
    assert _body(result)["error"]["code"] == "INVALID_REQUEST"
    assert auth.calls == 1
    assert approvals.calls == requests.calls == 0


def test_wrong_route_is_authenticated_then_hidden_as_not_found() -> None:
    event = _event()
    event["routeKey"] = "POST /v1/jobs/{job_id}/approve"
    adapter, auth, approvals, requests = _adapter()

    result = adapter(event)

    assert result["statusCode"] == 404
    assert _body(result)["error"]["code"] == "NOT_FOUND"
    assert auth.calls == 1
    assert approvals.calls == requests.calls == 0


@pytest.mark.parametrize(
    ("headers", "status", "code"),
    [
        ({"if-match": None}, 400, "INVALID_REQUEST"),
        ({"if-match": 'W/"' + "b" * 64 + '"'}, 400, "INVALID_REQUEST"),
        ({"if-match": APPROVAL_HEADER_FINGERPRINT}, 400, "INVALID_REQUEST"),
        ({"idempotency-key": " leading"}, 400, "INVALID_REQUEST"),
        ({"content-type": "application/json; charset=utf-8"}, 400, "INVALID_REQUEST"),
    ],
)
def test_header_contract_is_exact(
    headers: dict[str, object],
    status: int,
    code: str,
) -> None:
    adapter, _auth, approvals, requests = _adapter()

    result = adapter(_event(headers=headers))

    assert result["statusCode"] == status
    assert _body(result)["error"]["code"] == code
    assert approvals.calls == requests.calls == 0


def test_missing_if_match_is_precondition_required() -> None:
    event = _event()
    del event["headers"]["if-match"]
    adapter, _auth, approvals, requests = _adapter()

    result = adapter(event)

    assert result["statusCode"] == 428
    assert _body(result)["error"]["code"] == "PRECONDITION_REQUIRED"
    assert approvals.calls == requests.calls == 0


@pytest.mark.parametrize(
    ("body", "status", "code"),
    [
        ("{", 400, "INVALID_REQUEST"),
        (
            '{"expected_record_version":7,"expected_record_version":8}',
            400,
            "INVALID_REQUEST",
        ),
        ({"expected_record_version": True}, 422, "VALIDATION_FAILED"),
        ({"confirmation": "publish"}, 422, "VALIDATION_FAILED"),
    ],
)
def test_body_contract_is_strict_and_value_free(
    body: object,
    status: int,
    code: str,
) -> None:
    if isinstance(body, dict):
        complete = json.loads(_event()["body"])
        complete.update(body)
        body = complete
    adapter, _auth, approvals, requests = _adapter()

    result = adapter(_event(body=body))

    assert result["statusCode"] == status
    assert _body(result)["error"]["code"] == code
    assert approvals.calls == requests.calls == 0
    assert "publish_exact_approved_listing" not in result["body"]


@pytest.mark.parametrize("not_found", [NotFoundError(), PublicationNotFoundError()])
def test_foreign_and_unknown_owner_first_lookup_are_indistinguishable(
    not_found: Exception,
) -> None:
    adapter, _auth, approvals, requests = _adapter(
        approvals=Approvals(not_found),
    )

    result = adapter(_event())

    assert result["statusCode"] == 404
    assert _body(result)["error"] == {
        "code": "NOT_FOUND",
        "message": "The requested resource was not found.",
    }
    assert approvals.last == (OWNER_ID, JOB_ID)
    assert requests.calls == 0


def test_nonapproved_job_is_rejected_before_request_service() -> None:
    adapter, _auth, _approvals, requests = _adapter(
        approvals=Approvals(_approved_job(state=ControlJobState.AWAITING_APPROVAL)),
    )

    result = adapter(_event())

    assert result["statusCode"] == 409
    assert _body(result)["error"]["code"] == "PUBLICATION_NOT_APPROVED"
    assert requests.calls == 0


@pytest.mark.parametrize(
    "forgery",
    [
        _approved_job(owner_id=OTHER_OWNER_ID),
        _approved_job(job_id="job_wrong"),
        _approved_job().model_copy(update={"approval_decision_id": None}),
    ],
)
def test_mismatched_or_missing_server_authority_fails_closed(
    forgery: ControlJobRecord,
) -> None:
    adapter, _auth, _approvals, requests = _adapter(approvals=Approvals(forgery))

    result = adapter(_event())

    assert result["statusCode"] == 409
    assert _body(result)["error"]["code"] == "PUBLICATION_INVALID_AUTHORITY"
    assert requests.calls == 0


@pytest.mark.parametrize(
    ("error", "status", "code"),
    [
        (
            PublicationIdempotencyConflictError("private changed command"),
            409,
            "PUBLICATION_IDEMPOTENCY_CONFLICT",
        ),
        (
            PublicationConflictError(
                PublicationErrorCode.ALREADY_REQUESTED,
                "private aggregate",
            ),
            409,
            "PUBLICATION_ALREADY_REQUESTED",
        ),
        (
            PublicationConflictError(
                PublicationErrorCode.CONCURRENT_WRITE,
                "private record",
            ),
            409,
            "PUBLICATION_CONCURRENT_WRITE",
        ),
        (
            PublicationAuthorityError(
                PublicationErrorCode.STALE_RECORD,
                "private record version 99",
            ),
            412,
            "PUBLICATION_STALE_RECORD",
        ),
        (
            PublicationAuthorityError(
                PublicationErrorCode.STALE_REVIEW,
                "private review fingerprint",
            ),
            412,
            "PUBLICATION_STALE_REVIEW",
        ),
        (
            PublicationAuthorityError(
                PublicationErrorCode.STALE_APPROVAL,
                "private approval decision",
            ),
            412,
            "PUBLICATION_STALE_APPROVAL",
        ),
        (
            PublicationAuthorityError(
                PublicationErrorCode.INVALID_AUTHORITY,
                "private authority graph",
            ),
            409,
            "PUBLICATION_INVALID_AUTHORITY",
        ),
        (RuntimeError("private provider secret"), 500, "INTERNAL_ERROR"),
    ],
)
def test_service_errors_have_closed_safe_mappings(
    error: Exception,
    status: int,
    code: str,
) -> None:
    adapter, _auth, _approvals, requests = _adapter(requests=Requests(error))

    result = adapter(_event())

    assert result["statusCode"] == status
    assert _body(result)["error"]["code"] == code
    assert requests.calls == 1
    assert "private" not in result["body"]


def test_invalid_or_wrong_job_service_response_is_internal_and_not_leaked() -> None:
    adapter, _auth, _approvals, _requests = _adapter(
        requests=Requests(_response(job_id="job_other")),
    )

    result = adapter(_event())

    assert result["statusCode"] == 500
    assert _body(result)["error"] == {
        "code": "INTERNAL_ERROR",
        "message": "The request could not be completed.",
    }
    assert "job_other" not in result["body"]
