"""Uncomposed read-only HTTP adapter for the disabled Phase 7 publication projection."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Any, Protocol

from pydantic import ValidationError

from mr_lister.cloud.auth import AccessDeniedError, AuthenticationRequiredError
from mr_lister.control.errors import NotFoundError
from mr_lister.publication.projection import PublicationProjectionUnavailableError
from mr_lister.publication.projection_models import SellerPublicationProjection

PUBLICATION_QUERY_ROUTE = "GET /v1/jobs/{job_id}/publication"

_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
_REQUEST_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


PublicationAuthenticationError = AuthenticationRequiredError
PublicationAccessDeniedError = AccessDeniedError


class PublicationRequestError(Exception):
    """The request differs from the single read-only route contract."""


class PublicationOwnerAuthenticator(Protocol):
    def authenticate(self, event: Mapping[str, Any]) -> str: ...


class PublicationProjectionPort(Protocol):
    def get(self, *, owner_id: str, job_id: str) -> SellerPublicationProjection: ...


class PublicationQueryApiAdapter:
    """Authenticate first, then return one owner-scoped no-store projection."""

    def __init__(
        self,
        *,
        authenticator: PublicationOwnerAuthenticator,
        projections: PublicationProjectionPort,
    ) -> None:
        self._authenticator = authenticator
        self._projections = projections

    def __call__(
        self,
        event: Mapping[str, Any],
        context: object | None = None,
    ) -> dict[str, Any]:
        del context
        request_id = _request_id(event)
        try:
            # Identity precedes every path/body/query read and every application lookup.
            owner_id = self._authenticator.authenticate(event)
            if not isinstance(owner_id, str) or re.fullmatch(r"[a-f0-9]{64}", owner_id) is None:
                raise PublicationAuthenticationError
            job_id = _validate_request(event)
            projection = _validate_projection(
                self._projections.get(owner_id=owner_id, job_id=job_id),
                expected_job_id=job_id,
            )
            return _response(
                200,
                projection.model_dump(mode="json"),
                request_id=request_id,
                etag=projection.etag,
            )
        except AuthenticationRequiredError:
            return _error(401, "AUTHENTICATION_REQUIRED", request_id)
        except AccessDeniedError:
            return _error(403, "FORBIDDEN", request_id)
        except (NotFoundError, PublicationRequestError):
            return _error(404, "NOT_FOUND", request_id)
        except PublicationProjectionUnavailableError:
            return _error(
                503,
                "PUBLICATION_PROJECTION_UNAVAILABLE",
                request_id,
                retry_after="2",
            )
        except Exception:
            return _error(500, "INTERNAL_ERROR", request_id)


def _validate_projection(
    value: SellerPublicationProjection,
    *,
    expected_job_id: str,
) -> SellerPublicationProjection:
    try:
        projection = SellerPublicationProjection.model_validate(value.model_dump(mode="python"))
    except (AttributeError, TypeError, ValidationError):
        raise PublicationProjectionUnavailableError from None
    if projection.job_id != expected_job_id:
        raise PublicationProjectionUnavailableError
    return projection


def _validate_request(event: Mapping[str, Any]) -> str:
    if event.get("version") != "2.0" or event.get("routeKey") != PUBLICATION_QUERY_ROUTE:
        raise PublicationRequestError
    params = event.get("pathParameters")
    if not isinstance(params, Mapping) or set(params) != {"job_id"}:
        raise PublicationRequestError
    job_id = params.get("job_id")
    if not isinstance(job_id, str) or _SAFE_ID.fullmatch(job_id) is None:
        raise PublicationRequestError
    expected_path = f"/v1/jobs/{job_id}/publication"
    request_context = event.get("requestContext")
    http = request_context.get("http") if isinstance(request_context, Mapping) else None
    if (
        event.get("rawPath") != expected_path
        or not isinstance(http, Mapping)
        or http.get("method") != "GET"
        or http.get("path") != expected_path
    ):
        raise PublicationRequestError
    if event.get("rawQueryString", "") != "" or event.get("queryStringParameters") is not None:
        raise PublicationRequestError
    body = event.get("body")
    encoded = event.get("isBase64Encoded")
    if (body is not None and body != "") or (encoded is not None and encoded is not False):
        raise PublicationRequestError
    return job_id


def _request_id(event: Mapping[str, Any]) -> str:
    context = event.get("requestContext")
    value = context.get("requestId") if isinstance(context, Mapping) else None
    return value if isinstance(value, str) and _REQUEST_ID.fullmatch(value) else "unavailable"


def _response(
    status: int,
    body: Mapping[str, Any],
    *,
    request_id: str,
    etag: str | None = None,
    retry_after: str | None = None,
) -> dict[str, Any]:
    headers = {
        "cache-control": "no-store",
        "content-type": "application/json",
        "x-content-type-options": "nosniff",
        "x-request-id": request_id,
    }
    if etag is not None:
        headers["etag"] = f'"{etag}"'
    if retry_after is not None:
        headers["retry-after"] = retry_after
    return {
        "statusCode": status,
        "headers": headers,
        "body": json.dumps(body, sort_keys=True, separators=(",", ":")),
        "isBase64Encoded": False,
    }


def _error(
    status: int,
    code: str,
    request_id: str,
    *,
    retry_after: str | None = None,
) -> dict[str, Any]:
    messages = {
        "AUTHENTICATION_REQUIRED": "Sign in is required to continue.",
        "FORBIDDEN": "This account cannot access the seller API.",
        "NOT_FOUND": "The requested resource was not found.",
        "PUBLICATION_PROJECTION_UNAVAILABLE": "Publication status is temporarily unavailable.",
        "INTERNAL_ERROR": "The request could not be completed.",
    }
    return _response(
        status,
        {"error": {"code": code, "message": messages[code]}},
        request_id=request_id,
        retry_after=retry_after,
    )
