"""Closed HTTP metadata, error, route, and logging rules for the Phase 6 cloud API."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from pydantic import ValidationError

from mr_lister.cloud.auth import AccessDeniedError, AuthenticationRequiredError
from mr_lister.cloud.preview import PreviewAuthorizationUnavailableError
from mr_lister.control.errors import (
    ConcurrentControlModificationError,
    ControlError,
    EconomicsStaleError,
    IdempotencyConflictError,
    InvalidControlStateError,
    NotFoundError,
    ReconciliationRequiredError,
    RetryNotAllowedError,
    StaleReviewError,
    SyncInProgressError,
)
from mr_lister.control.projection import ReviewProjectionUnavailableError
from mr_lister.control.upload_service import (
    UploadArtifactIntegrityError,
    UploadDependencyUnavailableError,
    UploadExpiredError,
)

PUBLIC_ROUTE_KEYS = frozenset({"GET /health"})
PROTECTED_ROUTE_KEYS = frozenset(
    {
        "POST /v1/uploads",
        "POST /v1/uploads/{upload_id}/authorize",
        "POST /v1/uploads/{upload_id}/complete",
        "POST /v1/uploads/{upload_id}/cancel",
        "GET /v1/jobs",
        "GET /v1/jobs/{job_id}",
        "GET /v1/jobs/{job_id}/review",
        "PUT /v1/jobs/{job_id}/review/listing",
        "POST /v1/jobs/{job_id}/economics/refresh",
        "POST /v1/jobs/{job_id}/approve",
        "POST /v1/jobs/{job_id}/cancel",
        "POST /v1/jobs/{job_id}/retry",
        "GET /v1/jobs/{job_id}/artwork-preview",
    }
)
ALL_ROUTE_KEYS = PUBLIC_ROUTE_KEYS | PROTECTED_ROUTE_KEYS

_IDEMPOTENCY_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")
_STRONG_ETAG = re.compile(r'^"([a-f0-9]{64})"$')
_REQUEST_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_LOG_OWNER = re.compile(r"^[a-f0-9]{16}$")


class InvalidRequestError(Exception):
    code = "INVALID_REQUEST"


class PreconditionRequiredError(Exception):
    code = "PRECONDITION_REQUIRED"


class RouteNotFoundError(Exception):
    code = "NOT_FOUND"


@dataclass(frozen=True, slots=True)
class _ErrorSpec:
    status_code: int
    code: str
    message: str
    retry_after: str | None = None


_ERROR_SPECS = {
    "AUTHENTICATION_REQUIRED": _ErrorSpec(
        401,
        "AUTHENTICATION_REQUIRED",
        "Sign in is required to continue.",
    ),
    "FORBIDDEN": _ErrorSpec(403, "FORBIDDEN", "This account cannot access the seller API."),
    "NOT_FOUND": _ErrorSpec(404, "NOT_FOUND", "The requested resource was not found."),
    "INVALID_REQUEST": _ErrorSpec(400, "INVALID_REQUEST", "The request is not valid."),
    "VALIDATION_FAILED": _ErrorSpec(
        422,
        "VALIDATION_FAILED",
        "One or more request fields are not valid.",
    ),
    "ARTIFACT_INTEGRITY": _ErrorSpec(
        422,
        "ARTIFACT_INTEGRITY",
        "The uploaded artwork does not match the declared source.",
    ),
    "UPLOAD_EXPIRED": _ErrorSpec(
        410,
        "UPLOAD_EXPIRED",
        "This upload has expired. Start a new upload to continue.",
    ),
    "UPLOAD_UNAVAILABLE": _ErrorSpec(
        503,
        "UPLOAD_UNAVAILABLE",
        "Artwork upload is temporarily unavailable.",
        retry_after="2",
    ),
    "PRECONDITION_REQUIRED": _ErrorSpec(
        428,
        "PRECONDITION_REQUIRED",
        "Reload the current review before changing it.",
    ),
    "VERSION_CONFLICT": _ErrorSpec(
        409,
        "VERSION_CONFLICT",
        "The job changed. Reload its current state and try again.",
    ),
    "STALE_REVIEW": _ErrorSpec(
        412,
        "STALE_REVIEW",
        "The review changed. Reload the latest version and try again.",
    ),
    "IDEMPOTENCY_CONFLICT": _ErrorSpec(
        409,
        "IDEMPOTENCY_CONFLICT",
        "That request key was already used for a different operation.",
    ),
    "INVALID_STATE": _ErrorSpec(
        409,
        "INVALID_STATE",
        "This action is not available at the current stage.",
    ),
    "ECONOMICS_STALE": _ErrorSpec(
        409,
        "ECONOMICS_STALE",
        "Refresh the estimated proceeds before continuing.",
    ),
    "RETRY_NOT_ALLOWED": _ErrorSpec(
        409,
        "RETRY_NOT_ALLOWED",
        "This job does not have an available retry.",
    ),
    "SYNC_IN_PROGRESS": _ErrorSpec(
        409,
        "SYNC_IN_PROGRESS",
        "Product synchronization is still in progress.",
    ),
    "RECONCILIATION_REQUIRED": _ErrorSpec(
        409,
        "RECONCILIATION_REQUIRED",
        "The product outcome must be reconciled before continuing.",
    ),
    "PROJECTION_UNAVAILABLE": _ErrorSpec(
        503,
        "PROJECTION_UNAVAILABLE",
        "The consolidated review is temporarily unavailable.",
        retry_after="2",
    ),
    "INTERNAL_ERROR": _ErrorSpec(
        500,
        "INTERNAL_ERROR",
        "The request could not be completed.",
    ),
}


def parse_idempotency_key(headers: Mapping[str, Any] | None) -> str:
    """Require exactly one bounded opaque caller key without normalizing unsafe input."""

    value = _exact_header(headers, "idempotency-key", required=True)
    assert value is not None
    if not value.isascii() or _IDEMPOTENCY_KEY.fullmatch(value) is None:
        raise InvalidRequestError
    return value


def parse_strong_if_match(headers: Mapping[str, Any] | None) -> str:
    """Return one unquoted 64-hex review authority token from a strong If-Match header."""

    value = _exact_header(headers, "if-match", required=False)
    if value is None:
        raise PreconditionRequiredError
    match = _STRONG_ETAG.fullmatch(value)
    if match is None:
        raise InvalidRequestError
    return match.group(1)


def require_exact_route_key(event: Mapping[str, Any], *, protected: bool | None = None) -> str:
    """Accept only one explicitly deployed route; proxy and default routes are impossible."""

    route_key = event.get("routeKey") if isinstance(event, Mapping) else None
    allowed = ALL_ROUTE_KEYS
    if protected is True:
        allowed = PROTECTED_ROUTE_KEYS
    elif protected is False:
        allowed = PUBLIC_ROUTE_KEYS
    if not isinstance(route_key, str) or route_key not in allowed:
        raise RouteNotFoundError
    return route_key


def request_id_from_event(event: Mapping[str, Any]) -> str:
    """Return only a bounded API Gateway request identifier safe to echo and log."""

    context = event.get("requestContext") if isinstance(event, Mapping) else None
    value = context.get("requestId") if isinstance(context, Mapping) else None
    if not isinstance(value, str) or _REQUEST_ID.fullmatch(value) is None:
        return "unavailable"
    return value


def error_response(error: BaseException, *, request_id: object) -> dict[str, Any]:
    """Translate an exception to a closed seller-safe API Gateway v2 response."""

    safe_request_id = (
        request_id
        if isinstance(request_id, str) and _REQUEST_ID.fullmatch(request_id)
        else "unavailable"
    )
    spec = _error_spec(error)
    headers = {
        "Cache-Control": "no-store",
        "Content-Type": "application/json",
        "X-Content-Type-Options": "nosniff",
        "X-Request-Id": safe_request_id,
    }
    if spec.retry_after is not None:
        headers["Retry-After"] = spec.retry_after
    body = {
        "error": {
            "code": spec.code,
            "message": spec.message,
            "request_id": safe_request_id,
        }
    }
    return {
        "statusCode": spec.status_code,
        "headers": headers,
        "body": json.dumps(body, sort_keys=True, separators=(",", ":")),
        "isBase64Encoded": False,
    }


def build_safe_request_log(
    event: Mapping[str, Any],
    *,
    status_code: int,
    duration_ms: int,
    owner_log_digest: str | None = None,
    error_code: str | None = None,
) -> dict[str, object]:
    """Build an allowlisted structured record without consulting headers, body, or query data."""

    if (
        isinstance(status_code, bool)
        or not isinstance(status_code, int)
        or not 100 <= status_code <= 599
    ):
        raise ValueError("HTTP log status is invalid")
    if isinstance(duration_ms, bool) or not isinstance(duration_ms, int) or duration_ms < 0:
        raise ValueError("HTTP log duration is invalid")
    if owner_log_digest is not None and _LOG_OWNER.fullmatch(owner_log_digest) is None:
        raise ValueError("HTTP log owner digest is invalid")
    if error_code is not None and re.fullmatch(r"[A-Z][A-Z0-9_]{0,99}", error_code) is None:
        raise ValueError("HTTP log error code is invalid")
    try:
        route_key = require_exact_route_key(event)
    except RouteNotFoundError:
        route_key = "UNRECOGNIZED"
    record: dict[str, object] = {
        "request_id": request_id_from_event(event),
        "route_key": route_key,
        "status_code": status_code,
        "duration_ms": duration_ms,
    }
    if owner_log_digest is not None:
        record["owner"] = owner_log_digest
    if error_code is not None:
        record["error_code"] = error_code
    return record


def _exact_header(headers: Mapping[str, Any] | None, name: str, *, required: bool) -> str | None:
    if headers is None:
        if required:
            raise InvalidRequestError
        return None
    if not isinstance(headers, Mapping):
        raise InvalidRequestError
    matches = [(key, value) for key, value in headers.items() if str(key).casefold() == name]
    if len(matches) != 1:
        if not matches and not required:
            return None
        raise InvalidRequestError
    key, value = matches[0]
    if not isinstance(key, str) or not isinstance(value, str):
        raise InvalidRequestError
    return value


def _error_spec(error: BaseException) -> _ErrorSpec:
    if isinstance(error, AuthenticationRequiredError):
        return _ERROR_SPECS["AUTHENTICATION_REQUIRED"]
    if isinstance(error, AccessDeniedError):
        return _ERROR_SPECS["FORBIDDEN"]
    if isinstance(error, (NotFoundError, RouteNotFoundError)):
        return _ERROR_SPECS["NOT_FOUND"]
    if isinstance(error, InvalidRequestError):
        return _ERROR_SPECS["INVALID_REQUEST"]
    if isinstance(error, PreconditionRequiredError):
        return _ERROR_SPECS["PRECONDITION_REQUIRED"]
    if isinstance(error, ValidationError):
        return _ERROR_SPECS["VALIDATION_FAILED"]
    if isinstance(error, UploadArtifactIntegrityError):
        return _ERROR_SPECS["ARTIFACT_INTEGRITY"]
    if isinstance(error, UploadExpiredError):
        return _ERROR_SPECS["UPLOAD_EXPIRED"]
    if isinstance(error, UploadDependencyUnavailableError):
        return _ERROR_SPECS["UPLOAD_UNAVAILABLE"]
    if isinstance(
        error,
        (ReviewProjectionUnavailableError, PreviewAuthorizationUnavailableError),
    ):
        return _ERROR_SPECS["PROJECTION_UNAVAILABLE"]
    if isinstance(
        error,
        (
            ConcurrentControlModificationError,
            StaleReviewError,
            IdempotencyConflictError,
            InvalidControlStateError,
            EconomicsStaleError,
            RetryNotAllowedError,
            SyncInProgressError,
            ReconciliationRequiredError,
        ),
    ):
        return _ERROR_SPECS[error.code]
    if isinstance(error, ControlError):
        # Internal worker-only ControlError subclasses must never expose their class, code, or text
        # if they accidentally reach an internet-facing adapter.
        return _ERROR_SPECS["INTERNAL_ERROR"]
    return _ERROR_SPECS["INTERNAL_ERROR"]


__all__ = [
    "ALL_ROUTE_KEYS",
    "PROTECTED_ROUTE_KEYS",
    "PUBLIC_ROUTE_KEYS",
    "InvalidRequestError",
    "PreconditionRequiredError",
    "RouteNotFoundError",
    "build_safe_request_log",
    "error_response",
    "parse_idempotency_key",
    "parse_strong_if_match",
    "request_id_from_event",
    "require_exact_route_key",
]
