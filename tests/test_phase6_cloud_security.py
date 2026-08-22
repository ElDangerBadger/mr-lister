from __future__ import annotations

import json
from hashlib import sha256

import pytest

from mr_lister.cloud.auth import (
    AccessDeniedError,
    AuthenticationRequiredError,
    SellerClaimsPolicy,
    authenticate_and_invoke,
    authenticate_seller,
)
from mr_lister.cloud.http import (
    ALL_ROUTE_KEYS,
    PROTECTED_ROUTE_KEYS,
    InvalidRequestError,
    PreconditionRequiredError,
    RouteNotFoundError,
    build_safe_request_log,
    error_response,
    parse_idempotency_key,
    parse_strong_if_match,
    require_exact_route_key,
)
from mr_lister.cloud.preview import PreviewAuthorizationUnavailableError
from mr_lister.control.errors import (
    ConcurrentControlModificationError,
    IdempotencyConflictError,
    NotFoundError,
    StaleReviewError,
    WorkNotActiveError,
)
from mr_lister.control.projection import ReviewProjectionUnavailableError
from mr_lister.control.upload_service import (
    UploadArtifactIntegrityError,
    UploadDependencyUnavailableError,
    UploadExpiredError,
)

ISSUER = "https://cognito-idp.us-west-2.amazonaws.com/us-west-2_example"
CLIENT_ID = "4exampleclientid9"
SCOPE = "mr-lister/review"
SUBJECT = "924ca29a-f4af-4a2d-b7cb-2944b8dceb95"
POLICY = SellerClaimsPolicy(issuer=ISSUER, client_id=CLIENT_ID, required_scope=SCOPE)


def event(*, claim_updates: dict[str, object] | None = None) -> dict[str, object]:
    claims: dict[str, object] = {
        "iss": ISSUER,
        "sub": SUBJECT,
        "token_use": "access",
        "client_id": CLIENT_ID,
        "scope": f"openid {SCOPE}",
        "cognito:groups": '["seller"]',
    }
    claims.update(claim_updates or {})
    return {
        "version": "2.0",
        "routeKey": "GET /v1/jobs/{job_id}/review",
        "headers": {
            "authorization": "Bearer secret-token",
            "idempotency-key": "secret-request-key",
        },
        "queryStringParameters": {"grant": "secret-preview-grant"},
        "body": '{"secret":"request-body"}',
        "requestContext": {
            "requestId": "request-123",
            "authorizer": {"jwt": {"claims": claims, "scopes": [SCOPE]}},
        },
    }


def test_valid_verified_claims_derive_only_opaque_owner_material() -> None:
    seller = authenticate_seller(event(), policy=POLICY)

    expected_owner = sha256(ISSUER.encode() + b"\0" + SUBJECT.encode()).hexdigest()
    assert seller.owner_id == expected_owner
    assert (
        seller.log_owner_digest
        == sha256(b"mr-lister-log\0" + expected_owner.encode()).hexdigest()[:16]
    )
    assert seller.scopes == frozenset({"openid", SCOPE})
    assert not hasattr(seller, "sub")
    assert not hasattr(seller, "claims")


def test_authentication_happens_before_operation_invocation() -> None:
    calls: list[object] = []

    with pytest.raises(AccessDeniedError):
        authenticate_and_invoke(
            event(claim_updates={"cognito:groups": '["viewer"]'}),
            policy=POLICY,
            operation=lambda seller: calls.append(seller),
        )

    assert calls == []


@pytest.mark.parametrize(
    ("claim", "value"),
    (
        ("iss", "https://cognito-idp.us-east-1.amazonaws.com/other"),
        ("sub", "subject with spaces"),
        ("token_use", "id"),
        ("client_id", "other-client"),
        ("scope", "openid profile"),
        ("scope", f"openid  {SCOPE}"),
        ("cognito:groups", '["viewer"]'),
        ("cognito:groups", '["seller","seller"]'),
    ),
)
def test_every_claim_mismatch_fails_closed(claim: str, value: object) -> None:
    with pytest.raises(AccessDeniedError):
        authenticate_seller(event(claim_updates={claim: value}), policy=POLICY)


@pytest.mark.parametrize(
    "groups",
    (
        ["seller", "auditor"],
        "seller",
        "[seller]",
        "[seller auditor]",
        "[ seller auditor ]",
    ),
)
def test_group_claim_accepts_verified_api_gateway_shapes(groups: object) -> None:
    assert authenticate_seller(event(claim_updates={"cognito:groups": groups}), policy=POLICY)


@pytest.mark.parametrize(
    "groups",
    (
        "[]",
        "[ ]",
        "[seller ]",
        "[ seller]",
        "[seller  auditor]",
        "[seller\tauditor]",
        "[seller seller]",
    ),
)
def test_group_claim_rejects_malformed_bracketed_shapes(groups: str) -> None:
    with pytest.raises(AccessDeniedError):
        authenticate_seller(event(claim_updates={"cognito:groups": groups}), policy=POLICY)


@pytest.mark.parametrize(
    "malformed",
    (
        {},
        {"requestContext": {}},
        {"requestContext": {"authorizer": {}}},
        {"requestContext": {"authorizer": {"jwt": {}}}},
    ),
)
def test_missing_verified_authorizer_context_requires_authentication(malformed: dict) -> None:
    with pytest.raises(AuthenticationRequiredError):
        authenticate_seller(malformed, policy=POLICY)


def test_idempotency_key_parser_is_case_insensitive_but_not_normalizing() -> None:
    assert parse_idempotency_key({"Idempotency-Key": "upload.create:abc_123"}) == (
        "upload.create:abc_123"
    )


@pytest.mark.parametrize(
    "headers",
    (
        None,
        {},
        {"Idempotency-Key": " leading"},
        {"Idempotency-Key": "trailing "},
        {"Idempotency-Key": "unicode-\N{SNOWMAN}"},
        {"Idempotency-Key": 'quoted"value'},
        {"Idempotency-Key": "a" * 257},
        {"Idempotency-Key": "one", "idempotency-key": "two"},
    ),
)
def test_idempotency_key_parser_rejects_missing_or_unsafe_values(headers) -> None:
    with pytest.raises(InvalidRequestError):
        parse_idempotency_key(headers)


def test_if_match_parser_returns_only_one_unquoted_strong_authority_token() -> None:
    authority = "a" * 64
    assert parse_strong_if_match({"IF-MATCH": f'"{authority}"'}) == authority


def test_missing_if_match_requires_a_precondition() -> None:
    with pytest.raises(PreconditionRequiredError):
        parse_strong_if_match({})


@pytest.mark.parametrize(
    "value",
    (
        "a" * 64,
        'W/"' + "a" * 64 + '"',
        "*",
        '"' + "A" * 64 + '"',
        '"' + "a" * 64 + '","' + "b" * 64 + '"',
        ' "' + "a" * 64 + '"',
    ),
)
def test_if_match_rejects_weak_wildcard_list_and_normalized_values(value: str) -> None:
    with pytest.raises(InvalidRequestError):
        parse_strong_if_match({"If-Match": value})


def test_route_surface_is_exact_and_contains_no_publication_or_proxy_route() -> None:
    assert require_exact_route_key(event(), protected=True) == ("GET /v1/jobs/{job_id}/review")
    assert "GET /health" not in PROTECTED_ROUTE_KEYS
    assert all("publish" not in route.casefold() for route in ALL_ROUTE_KEYS)
    assert all(
        "proxy" not in route.casefold() and "$default" not in route for route in ALL_ROUTE_KEYS
    )
    with pytest.raises(RouteNotFoundError):
        require_exact_route_key({"routeKey": "POST /v1/jobs/{job_id}/publish"})


@pytest.mark.parametrize(
    ("error", "status", "code"),
    (
        (AuthenticationRequiredError("token-details"), 401, "AUTHENTICATION_REQUIRED"),
        (AccessDeniedError("claim-details"), 403, "FORBIDDEN"),
        (NotFoundError("private-owner-details"), 404, "NOT_FOUND"),
        (InvalidRequestError("request-details"), 400, "INVALID_REQUEST"),
        (PreconditionRequiredError("etag-details"), 428, "PRECONDITION_REQUIRED"),
        (ConcurrentControlModificationError("version-details"), 409, "VERSION_CONFLICT"),
        (StaleReviewError("review-details"), 412, "STALE_REVIEW"),
        (IdempotencyConflictError("key-details"), 409, "IDEMPOTENCY_CONFLICT"),
        (UploadArtifactIntegrityError("artifact-details"), 422, "ARTIFACT_INTEGRITY"),
        (UploadExpiredError("expiry-details"), 410, "UPLOAD_EXPIRED"),
        (UploadDependencyUnavailableError("s3-details"), 503, "UPLOAD_UNAVAILABLE"),
        (ReviewProjectionUnavailableError("record-details"), 503, "PROJECTION_UNAVAILABLE"),
        (PreviewAuthorizationUnavailableError("s3-details"), 503, "PROJECTION_UNAVAILABLE"),
        (WorkNotActiveError("worker-details"), 500, "INTERNAL_ERROR"),
        (RuntimeError("unexpected-secret-details"), 500, "INTERNAL_ERROR"),
    ),
)
def test_error_mapping_is_closed_and_never_renders_exception_text(error, status, code) -> None:
    response = error_response(error, request_id="request-123")
    body = json.loads(response["body"])

    assert response["statusCode"] == status
    assert body["error"]["code"] == code
    assert body["error"]["request_id"] == "request-123"
    assert str(error) not in response["body"]
    assert response["headers"]["Cache-Control"] == "no-store"
    assert response["headers"]["X-Content-Type-Options"] == "nosniff"
    if status == 503:
        assert response["headers"]["Retry-After"] == "2"


def test_untrusted_request_id_is_not_echoed() -> None:
    response = error_response(RuntimeError("secret"), request_id="bad\nheader: injected")
    assert response["headers"]["X-Request-Id"] == "unavailable"
    assert "injected" not in response["body"]
    assert (
        error_response(RuntimeError("secret"), request_id=None)["headers"]["X-Request-Id"]
        == "unavailable"
    )


def test_structured_log_is_a_strict_allowlist_and_ignores_sensitive_event_fields() -> None:
    unsafe_event = event()
    record = build_safe_request_log(
        unsafe_event,
        status_code=412,
        duration_ms=18,
        owner_log_digest="a" * 16,
        error_code="STALE_REVIEW",
    )
    serialized = json.dumps(record, sort_keys=True)

    assert record == {
        "request_id": "request-123",
        "route_key": "GET /v1/jobs/{job_id}/review",
        "status_code": 412,
        "duration_ms": 18,
        "owner": "a" * 16,
        "error_code": "STALE_REVIEW",
    }
    for secret in (
        "secret-token",
        "secret-request-key",
        "secret-preview-grant",
        "request-body",
        SUBJECT,
    ):
        assert secret not in serialized


def test_unrecognized_route_is_not_copied_into_logs() -> None:
    record = build_safe_request_log(
        {"routeKey": "POST /private/secret", "requestContext": {"requestId": "request-1"}},
        status_code=404,
        duration_ms=0,
    )
    assert record["route_key"] == "UNRECOGNIZED"
    assert "private" not in json.dumps(record)
