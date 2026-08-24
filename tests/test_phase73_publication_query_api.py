"""Uncomposed owner-scoped Phase 7.3 publication query adapter tests."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

import pytest

from mr_lister.cloud.auth import AccessDeniedError, AuthenticationRequiredError
from mr_lister.control.errors import NotFoundError
from mr_lister.publication.contract import PublicationState
from mr_lister.publication.execution_models import PublicationAttemptStatus
from mr_lister.publication.projection import PublicationProjectionUnavailableError
from mr_lister.publication.projection_models import (
    SellerPublicationProjection,
    SellerPublicationStage,
)
from mr_lister.publication.query_api import (
    PUBLICATION_QUERY_ROUTE,
    PublicationAuthenticationError,
    PublicationQueryApiAdapter,
)
from tests.test_phase71_publication_store import NOW, OWNER_ID


def _event(job_id: str = "job_publication_query") -> dict[str, Any]:
    path = f"/v1/jobs/{job_id}/publication"
    return {
        "version": "2.0",
        "routeKey": PUBLICATION_QUERY_ROUTE,
        "rawPath": path,
        "rawQueryString": "",
        "pathParameters": {"job_id": job_id},
        "requestContext": {
            "requestId": "request-phase73",
            "http": {"method": "GET", "path": path},
        },
        "isBase64Encoded": False,
    }


def _projection(job_id: str = "job_publication_query") -> SellerPublicationProjection:
    return SellerPublicationProjection(
        job_id=job_id,
        state="not_requested",
        stage=SellerPublicationStage.AWAITING_ACTIVATION,
        notification_available=False,
        updated_at=NOW,
        etag="a" * 64,
    )


def _requested_projection() -> SellerPublicationProjection:
    return SellerPublicationProjection(
        job_id="job_publication_query",
        state=PublicationState.PUBLICATION_REQUESTED,
        stage=SellerPublicationStage.QUEUED,
        aggregate_record_version=0,
        attempt_status=PublicationAttemptStatus.OPEN,
        verification_deadline=NOW,
        notification_available=False,
        updated_at=NOW,
        etag="b" * 64,
    )


def _published_projection() -> SellerPublicationProjection:
    verified_at = NOW + timedelta(seconds=1)
    terminal_at = NOW + timedelta(seconds=2)
    return SellerPublicationProjection(
        job_id="job_publication_query",
        state=PublicationState.PUBLISHED,
        stage=SellerPublicationStage.COMPLETE,
        aggregate_record_version=1,
        attempt_status=PublicationAttemptStatus.TERMINAL,
        verification_deadline=NOW + timedelta(seconds=10),
        safe_listing_url="https://www.etsy.com/listing/777",
        verified_at=verified_at,
        report_id="published_report",
        terminal_at=terminal_at,
        notification_available=True,
        updated_at=terminal_at,
        etag="c" * 64,
    )


@dataclass
class Authenticator:
    result: str | Exception = OWNER_ID
    calls: int = 0

    def authenticate(self, event: object) -> str:
        self.calls += 1
        assert isinstance(event, dict)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


@dataclass
class Projections:
    result: SellerPublicationProjection | Exception
    calls: int = 0
    last: tuple[str, str] | None = None

    def get(self, *, owner_id: str, job_id: str) -> SellerPublicationProjection:
        self.calls += 1
        self.last = (owner_id, job_id)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def test_exact_read_returns_disabled_projection_with_strong_etag_and_no_store() -> None:
    projection = _projection()
    auth = Authenticator()
    projections = Projections(projection)
    response = PublicationQueryApiAdapter(
        authenticator=auth,
        projections=projections,
    )(_event())

    assert response["statusCode"] == 200
    assert response["headers"]["etag"] == f'"{projection.etag}"'
    assert response["headers"]["cache-control"] == "no-store"
    assert json.loads(response["body"]) == projection.model_dump(mode="json")
    assert projections.last == (OWNER_ID, projection.job_id)


@pytest.mark.parametrize(
    "mutation",
    [
        lambda event: event.update(routeKey="POST /v1/jobs/{job_id}/publication"),
        lambda event: event.update(rawQueryString="x=1"),
        lambda event: event.update(queryStringParameters={"x": "1"}),
        lambda event: event.update(body="{}"),
        lambda event: event.update(rawPath="/v1/jobs/wrong/publication"),
        lambda event: event.update(pathParameters={"job_id": "job", "owner_id": OWNER_ID}),
    ],
)
def test_request_shape_is_closed_after_authentication(mutation: Any) -> None:
    event = _event()
    mutation(event)
    auth = Authenticator()
    projections = Projections(_projection())

    response = PublicationQueryApiAdapter(
        authenticator=auth,
        projections=projections,
    )(event)

    assert auth.calls == 1
    assert projections.calls == 0
    assert response["statusCode"] == 404
    assert json.loads(response["body"])["error"]["code"] == "NOT_FOUND"


def test_authentication_failure_precedes_path_and_store_reads() -> None:
    auth = Authenticator(PublicationAuthenticationError())
    projections = Projections(_projection())
    event = {"totally": "untrusted"}

    response = PublicationQueryApiAdapter(
        authenticator=auth,
        projections=projections,
    )(event)

    assert response["statusCode"] == 401
    assert projections.calls == 0
    assert response["headers"]["cache-control"] == "no-store"


@pytest.mark.parametrize(
    ("error", "status", "code"),
    [
        (AuthenticationRequiredError(), 401, "AUTHENTICATION_REQUIRED"),
        (AccessDeniedError(), 403, "FORBIDDEN"),
    ],
)
def test_cloud_authentication_errors_keep_existing_http_semantics(
    error: Exception,
    status: int,
    code: str,
) -> None:
    response = PublicationQueryApiAdapter(
        authenticator=Authenticator(error),
        projections=Projections(_projection()),
    )(_event())

    assert response["statusCode"] == status
    assert json.loads(response["body"])["error"]["code"] == code


def test_foreign_and_unknown_are_exactly_indistinguishable() -> None:
    event = _event()
    foreign = PublicationQueryApiAdapter(
        authenticator=Authenticator(),
        projections=Projections(NotFoundError()),
    )(event)
    unknown = PublicationQueryApiAdapter(
        authenticator=Authenticator(),
        projections=Projections(NotFoundError()),
    )(event)

    assert foreign == unknown
    assert foreign["statusCode"] == 404


def test_projection_unavailable_is_closed_and_retryable() -> None:
    response = PublicationQueryApiAdapter(
        authenticator=Authenticator(),
        projections=Projections(
            PublicationProjectionUnavailableError("raw internal record identity")
        ),
    )(_event())

    assert response["statusCode"] == 503
    assert response["headers"]["retry-after"] == "2"
    assert "raw internal record identity" not in response["body"]


@pytest.mark.parametrize(
    "projection",
    [
        _projection().model_copy(update={"publication_enabled": True}),
        _projection("job_other"),
    ],
)
def test_projection_is_deep_reparsed_and_bound_to_the_requested_job(
    projection: SellerPublicationProjection,
) -> None:
    response = PublicationQueryApiAdapter(
        authenticator=Authenticator(),
        projections=Projections(projection),
    )(_event())

    assert response["statusCode"] == 503
    assert json.loads(response["body"])["error"]["code"] == ("PUBLICATION_PROJECTION_UNAVAILABLE")


@pytest.mark.parametrize(
    "partial_update",
    [
        {"safe_listing_url": "https://www.etsy.com/listing/777"},
        {"verified_at": NOW},
        {"notification_available": True},
        {"report_id": "partial_report"},
        {"terminal_at": NOW},
    ],
)
def test_partial_result_or_terminal_fields_fail_closed_after_deep_reparse(
    partial_update: dict[str, Any],
) -> None:
    forged = _requested_projection().model_copy(update=partial_update)

    response = PublicationQueryApiAdapter(
        authenticator=Authenticator(),
        projections=Projections(forged),
    )(_event())

    assert response["statusCode"] == 503
    body = json.loads(response["body"])
    assert body["error"]["code"] == "PUBLICATION_PROJECTION_UNAVAILABLE"


@pytest.mark.parametrize(
    "inconsistent_update",
    [
        {"stage": SellerPublicationStage.AWAITING_ACTIVATION},
        {"stage": SellerPublicationStage.VERIFYING},
        {"aggregate_record_version": -1},
        {"attempt_status": PublicationAttemptStatus.TERMINAL},
    ],
)
def test_inconsistent_requested_state_fields_fail_closed_after_deep_reparse(
    inconsistent_update: dict[str, Any],
) -> None:
    forged = _requested_projection().model_copy(update=inconsistent_update)

    response = PublicationQueryApiAdapter(
        authenticator=Authenticator(),
        projections=Projections(forged),
    )(_event())

    assert response["statusCode"] == 503
    body = json.loads(response["body"])
    assert body["error"]["code"] == "PUBLICATION_PROJECTION_UNAVAILABLE"


@pytest.mark.parametrize(
    "invalid_time_update",
    [
        {"verified_at": NOW + timedelta(seconds=11)},
        {"terminal_at": NOW},
        {"updated_at": NOW},
    ],
)
def test_published_time_authority_fails_closed_after_deep_reparse(
    invalid_time_update: dict[str, Any],
) -> None:
    forged = _published_projection().model_copy(update=invalid_time_update)

    response = PublicationQueryApiAdapter(
        authenticator=Authenticator(),
        projections=Projections(forged),
    )(_event())

    assert response["statusCode"] == 503
    body = json.loads(response["body"])
    assert body["error"]["code"] == "PUBLICATION_PROJECTION_UNAVAILABLE"


def test_adapter_is_read_only_and_not_registered_in_phase6_routes() -> None:
    from mr_lister.cloud.http import ALL_ROUTE_KEYS

    assert PUBLICATION_QUERY_ROUTE not in ALL_ROUTE_KEYS
    public_methods = {name for name in vars(PublicationQueryApiAdapter) if not name.startswith("_")}
    assert public_methods == set()
