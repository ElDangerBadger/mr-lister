"""Unregistered owner-scoped HTTP adapter for Phase 7 publication requests.

The adapter owns only browser-to-application translation.  It has no AWS, provider,
dispatcher, workflow, secret, or route-registration dependency.  In particular, the
browser cannot select the approval decision: that identifier is resolved through an
owner-first application authority immediately before the existing request service is
called.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, StrictInt, ValidationError

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
    PublicationDomainError,
    PublicationErrorCode,
    PublicationIdempotencyConflictError,
    PublicationNotFoundError,
)
from mr_lister.publication.models import Fingerprint

PUBLICATION_REQUEST_ROUTE = "POST /v1/jobs/{job_id}/publish"

_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
_OWNER_ID = re.compile(r"^[a-f0-9]{64}$")
_FINGERPRINT = re.compile(r"^[a-f0-9]{64}$")
_IDEMPOTENCY_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")
_REQUEST_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_MAX_BODY_BYTES = 16 * 1024


class PublicationRequestBody(BaseModel):
    """The complete and only browser-supplied publication authority."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    expected_record_version: StrictInt = Field(ge=0)
    expected_review_version: StrictInt = Field(ge=1)
    expected_review_fingerprint: Fingerprint
    confirmation: Literal["publish_exact_approved_listing"]


class PublicationOwnerAuthenticator(Protocol):
    def authenticate(self, event: Mapping[str, Any]) -> str: ...


class PublicationApprovalAuthority(Protocol):
    def get_job_for_owner(self, owner_id: str, job_id: str) -> ControlJobRecord: ...


class PublicationRequestPort(Protocol):
    def request_publication(
        self,
        command: RequestPublicationCommand,
    ) -> PublicationRequestResponse: ...


class PublicationRequestHandler(Protocol):
    def __call__(
        self,
        event: Mapping[str, Any],
        context: object | None = None,
    ) -> dict[str, Any]: ...


class _InvalidRequestError(Exception):
    pass


class _RouteNotFoundError(Exception):
    pass


class _RequestValidationError(Exception):
    pass


class _PreconditionRequiredError(Exception):
    pass


class PublicationRequestApiAdapter:
    """Authenticate first and freeze one exact approved job for publication."""

    def __init__(
        self,
        *,
        authenticator: PublicationOwnerAuthenticator,
        approvals: PublicationApprovalAuthority,
        requests: PublicationRequestPort,
    ) -> None:
        self._authenticator = authenticator
        self._approvals = approvals
        self._requests = requests

    def __call__(
        self,
        event: Mapping[str, Any],
        context: object | None = None,
    ) -> dict[str, Any]:
        del context

        # Do not inspect route, path, query, headers, or body before the caller has
        # crossed the injected authentication boundary.
        try:
            owner_id = self._authenticator.authenticate(event)
            if not isinstance(owner_id, str) or _OWNER_ID.fullmatch(owner_id) is None:
                raise AuthenticationRequiredError
        except Exception as error:
            return _error_response(error, request_id=_request_id(event))

        request_id = _request_id(event)
        try:
            job_id = _validate_request_shape(event)
            headers = _headers(event)
            review_and_approval_fingerprint = _strong_if_match(headers)
            idempotency_key = _idempotency_key(headers)
            body = _json_body(event)
            approval_decision_id = _approval_decision_id(
                self._approvals.get_job_for_owner(owner_id, job_id),
                owner_id=owner_id,
                job_id=job_id,
            )
            response = self._requests.request_publication(
                RequestPublicationCommand(
                    owner_id=owner_id,
                    job_id=job_id,
                    expected_record_version=body.expected_record_version,
                    expected_review_version=body.expected_review_version,
                    expected_review_fingerprint=body.expected_review_fingerprint,
                    expected_review_etag=review_and_approval_fingerprint,
                    expected_approval_decision_id=approval_decision_id,
                    expected_approval_fingerprint=review_and_approval_fingerprint,
                    confirmation=body.confirmation,
                    idempotency_key=idempotency_key,
                )
            )
            public_response = _validated_response(response, expected_job_id=job_id)
            return _json_response(
                202,
                public_response.model_dump(mode="json"),
                request_id=request_id,
            )
        except Exception as error:
            return _error_response(error, request_id=request_id)


def _validate_request_shape(event: Mapping[str, Any]) -> str:
    if not isinstance(event, Mapping) or event.get("version") != "2.0":
        raise _InvalidRequestError
    if event.get("routeKey") != PUBLICATION_REQUEST_ROUTE:
        raise _RouteNotFoundError

    parameters = event.get("pathParameters")
    if not isinstance(parameters, Mapping) or set(parameters) != {"job_id"}:
        raise _InvalidRequestError
    job_id = parameters.get("job_id")
    if not isinstance(job_id, str) or not job_id.isascii() or _SAFE_ID.fullmatch(job_id) is None:
        raise _InvalidRequestError

    path = f"/v1/jobs/{job_id}/publish"
    request_context = event.get("requestContext")
    http = request_context.get("http") if isinstance(request_context, Mapping) else None
    if (
        event.get("rawPath") != path
        or not isinstance(http, Mapping)
        or http.get("method") != "POST"
        or http.get("path") != path
    ):
        raise _InvalidRequestError
    if event.get("rawQueryString") != "" or event.get("queryStringParameters") is not None:
        raise _InvalidRequestError
    if event.get("isBase64Encoded") is not False:
        raise _InvalidRequestError
    return job_id


def _headers(event: Mapping[str, Any]) -> Mapping[str, Any]:
    headers = event.get("headers")
    if not isinstance(headers, Mapping):
        raise _InvalidRequestError
    return headers


def _exact_header(
    headers: Mapping[str, Any],
    name: str,
    *,
    required: bool = True,
) -> str | None:
    matches = [
        (key, value)
        for key, value in headers.items()
        if isinstance(key, str) and key.casefold() == name
    ]
    if not matches and not required:
        return None
    if len(matches) != 1 or not isinstance(matches[0][1], str):
        raise _InvalidRequestError
    return matches[0][1]


def _strong_if_match(headers: Mapping[str, Any]) -> str:
    value = _exact_header(headers, "if-match", required=False)
    if value is None:
        raise _PreconditionRequiredError
    if len(value) != 66 or not value.startswith('"') or not value.endswith('"'):
        raise _InvalidRequestError
    fingerprint = value[1:-1]
    if _FINGERPRINT.fullmatch(fingerprint) is None:
        raise _InvalidRequestError
    return fingerprint


def _idempotency_key(headers: Mapping[str, Any]) -> str:
    value = _exact_header(headers, "idempotency-key")
    assert value is not None
    if not value.isascii() or _IDEMPOTENCY_KEY.fullmatch(value) is None:
        raise _InvalidRequestError
    return value


def _json_body(event: Mapping[str, Any]) -> PublicationRequestBody:
    if _exact_header(_headers(event), "content-type") != "application/json":
        raise _InvalidRequestError
    body = event.get("body")
    if not isinstance(body, str) or not body:
        raise _InvalidRequestError
    try:
        encoded = body.encode("utf-8")
    except UnicodeEncodeError:
        raise _InvalidRequestError from None
    if len(encoded) > _MAX_BODY_BYTES:
        raise _InvalidRequestError
    try:
        value = json.loads(
            body,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_json_constant,
        )
    except (TypeError, ValueError):
        raise _InvalidRequestError from None
    if not isinstance(value, dict):
        raise _InvalidRequestError
    try:
        return PublicationRequestBody.model_validate(value)
    except ValidationError:
        raise _RequestValidationError from None


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate JSON key")
        value[key] = item
    return value


def _reject_json_constant(value: str) -> None:
    del value
    raise ValueError("non-finite number")


def _approval_decision_id(
    value: ControlJobRecord,
    *,
    owner_id: str,
    job_id: str,
) -> str:
    try:
        job = ControlJobRecord.model_validate(value.model_dump(mode="python"))
    except (AttributeError, TypeError, ValidationError):
        raise PublicationAuthorityError(
            PublicationErrorCode.INVALID_AUTHORITY,
            "Publication approval authority is invalid",
        ) from None
    if job.owner_id != owner_id or job.job_id != job_id:
        raise PublicationAuthorityError(
            PublicationErrorCode.INVALID_AUTHORITY,
            "Publication approval authority is invalid",
        )
    if job.state is not ControlJobState.APPROVED:
        raise PublicationAuthorityError(
            PublicationErrorCode.NOT_APPROVED,
            "The job is not approved for publication",
        )
    if job.approval_decision_id is None:
        raise PublicationAuthorityError(
            PublicationErrorCode.INVALID_AUTHORITY,
            "Publication approval authority is invalid",
        )
    return job.approval_decision_id


def _validated_response(
    value: PublicationRequestResponse,
    *,
    expected_job_id: str,
) -> PublicationRequestResponse:
    try:
        response = PublicationRequestResponse.model_validate(value.model_dump(mode="python"))
    except (AttributeError, TypeError, ValidationError):
        raise RuntimeError("invalid publication response") from None
    if response.job_id != expected_job_id:
        raise RuntimeError("mismatched publication response")
    return response


def _request_id(event: Mapping[str, Any]) -> str:
    if not isinstance(event, Mapping):
        return "unavailable"
    context = event.get("requestContext")
    value = context.get("requestId") if isinstance(context, Mapping) else None
    return value if isinstance(value, str) and _REQUEST_ID.fullmatch(value) else "unavailable"


def _json_response(
    status_code: int,
    body: Mapping[str, Any],
    *,
    request_id: str,
) -> dict[str, Any]:
    return {
        "statusCode": status_code,
        "headers": {
            "cache-control": "no-store",
            "content-type": "application/json",
            "x-content-type-options": "nosniff",
            "x-request-id": request_id,
        },
        "body": json.dumps(body, sort_keys=True, separators=(",", ":")),
        "isBase64Encoded": False,
    }


def _error_response(error: BaseException, *, request_id: str) -> dict[str, Any]:
    status_code, code, message = _error_spec(error)
    return _json_response(
        status_code,
        {"error": {"code": code, "message": message}},
        request_id=request_id,
    )


def _error_spec(error: BaseException) -> tuple[int, str, str]:
    if isinstance(error, AuthenticationRequiredError):
        return 401, "AUTHENTICATION_REQUIRED", "Sign in is required to continue."
    if isinstance(error, AccessDeniedError):
        return 403, "FORBIDDEN", "This account cannot access the seller API."
    if isinstance(error, (_RouteNotFoundError, NotFoundError, PublicationNotFoundError)):
        return 404, "NOT_FOUND", "The requested resource was not found."
    if isinstance(error, _PreconditionRequiredError):
        return 428, "PRECONDITION_REQUIRED", "Reload the approved listing before publishing."
    if isinstance(error, _RequestValidationError):
        return 422, "VALIDATION_FAILED", "One or more request fields are not valid."
    if isinstance(error, _InvalidRequestError):
        return 400, "INVALID_REQUEST", "The request is not valid."
    if isinstance(error, PublicationIdempotencyConflictError):
        return (
            409,
            "PUBLICATION_IDEMPOTENCY_CONFLICT",
            "That request key was already used for a different publication request.",
        )
    if isinstance(error, PublicationDomainError):
        specs: dict[PublicationErrorCode, tuple[int, str]] = {
            PublicationErrorCode.NOT_FOUND: (404, "The requested resource was not found."),
            PublicationErrorCode.NOT_APPROVED: (
                409,
                "This job is not approved for publication.",
            ),
            PublicationErrorCode.ALREADY_REQUESTED: (
                409,
                "Publication was already requested for this job.",
            ),
            PublicationErrorCode.STALE_RECORD: (
                412,
                "The approved job changed. Reload it before publishing.",
            ),
            PublicationErrorCode.STALE_REVIEW: (
                412,
                "The approved review changed. Reload it before publishing.",
            ),
            PublicationErrorCode.STALE_APPROVAL: (
                412,
                "The approval changed. Reload it before publishing.",
            ),
            PublicationErrorCode.INVALID_AUTHORITY: (
                409,
                "The publication authority is no longer valid.",
            ),
            PublicationErrorCode.PRICING_NOT_FRESH: (
                409,
                "Refresh the estimated proceeds before publishing.",
            ),
            PublicationErrorCode.CONCURRENT_WRITE: (
                409,
                "The job changed. Reload it before publishing.",
            ),
            PublicationErrorCode.INVALID_TRANSITION: (
                409,
                "Publication is not available at the current stage.",
            ),
        }
        spec = specs.get(error.code)
        if spec is not None:
            status, safe_message = spec
            public_code = (
                "NOT_FOUND" if error.code is PublicationErrorCode.NOT_FOUND else error.code.value
            )
            return status, public_code, safe_message
    if isinstance(error, PublicationConflictError):
        return 409, "PUBLICATION_CONFLICT", "The publication request conflicts with current state."
    return 500, "INTERNAL_ERROR", "The request could not be completed."


__all__ = [
    "PUBLICATION_REQUEST_ROUTE",
    "PublicationApprovalAuthority",
    "PublicationOwnerAuthenticator",
    "PublicationRequestApiAdapter",
    "PublicationRequestBody",
    "PublicationRequestHandler",
    "PublicationRequestPort",
]
