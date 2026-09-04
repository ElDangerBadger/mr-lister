"""Owner-scoped HTTP adapters for the Phase 7.18 enabled contract."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Any, Protocol

from pydantic import ValidationError

from mr_lister.cloud.auth import AccessDeniedError, AuthenticationRequiredError
from mr_lister.control.errors import NotFoundError
from mr_lister.publication.enabled_projection import Phase718SellerPublicationProjection
from mr_lister.publication.projection import PublicationProjectionUnavailableError
from mr_lister.publication.query_api import (
    PublicationQueryApiAdapter,
    PublicationRequestError,
)
from mr_lister.publication.request_api import PublicationRequestApiAdapter

_OWNER_ID = re.compile(r"^[a-f0-9]{64}$")


class Phase718PublicationProjectionPort(Protocol):
    def get(self, *, owner_id: str, job_id: str) -> Phase718SellerPublicationProjection: ...


class Phase718PublicationQueryApiAdapter(PublicationQueryApiAdapter):
    """Use the established HTTP boundary with the enabled 7.1.0 projection."""

    def __init__(self, *, authenticator: object, projections: Phase718PublicationProjectionPort):
        super().__init__(authenticator=authenticator, projections=projections)  # type: ignore[arg-type]

    def __call__(
        self,
        event: Mapping[str, Any],
        context: object | None = None,
    ) -> dict[str, Any]:
        del context
        # Reuse the frozen adapter's exact request/response utilities while validating the
        # successor projection explicitly.  This keeps route and error behavior byte-compatible.
        from mr_lister.publication import query_api

        request_id = query_api._request_id(event)
        try:
            owner_id = self._authenticator.authenticate(event)
            if not isinstance(owner_id, str) or _OWNER_ID.fullmatch(owner_id) is None:
                raise AuthenticationRequiredError
            job_id = query_api._validate_request(event)
            value = self._projections.get(owner_id=owner_id, job_id=job_id)
            try:
                projection = Phase718SellerPublicationProjection.model_validate(
                    value.model_dump(mode="python")
                )
            except (AttributeError, TypeError, ValidationError):
                raise PublicationProjectionUnavailableError from None
            if projection.job_id != job_id:
                raise PublicationProjectionUnavailableError
            return query_api._response(
                200,
                projection.model_dump(mode="json"),
                request_id=request_id,
                etag=projection.etag,
            )
        except AuthenticationRequiredError:
            return query_api._error(401, "AUTHENTICATION_REQUIRED", request_id)
        except AccessDeniedError:
            return query_api._error(403, "FORBIDDEN", request_id)
        except (NotFoundError, PublicationRequestError):
            return query_api._error(404, "NOT_FOUND", request_id)
        except PublicationProjectionUnavailableError:
            return query_api._error(
                503,
                "PUBLICATION_PROJECTION_UNAVAILABLE",
                request_id,
                retry_after="2",
            )
        except Exception:
            return query_api._error(500, "INTERNAL_ERROR", request_id)


class Phase718PublicationRequestApiAdapter:
    """Expose 7.1.0 at the HTTP edge without changing immutable 7.0.1 receipt rows."""

    __slots__ = ("_delegate",)

    def __init__(self, delegate: PublicationRequestApiAdapter) -> None:
        self._delegate = delegate

    def __call__(
        self,
        event: Mapping[str, Any],
        context: object | None = None,
    ) -> dict[str, Any]:
        response = self._delegate(event, context)
        if response.get("statusCode") != 202:
            return response
        try:
            body = json.loads(response["body"])
            if not isinstance(body, dict) or body.get("contract_version") != "7.0.1":
                raise ValueError
            body["contract_version"] = "7.1.0"
            exact = dict(response)
            exact["body"] = json.dumps(body, sort_keys=True, separators=(",", ":"))
            return exact
        except Exception:
            return {
                "statusCode": 500,
                "headers": {
                    "cache-control": "no-store",
                    "content-type": "application/json",
                    "x-content-type-options": "nosniff",
                    "x-request-id": _response_request_id(response),
                },
                "body": json.dumps(
                    {
                        "error": {
                            "code": "INTERNAL_ERROR",
                            "message": "The request could not be completed.",
                        }
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                "isBase64Encoded": False,
            }


def _response_request_id(response: Mapping[str, Any]) -> str:
    headers = response.get("headers")
    value = headers.get("x-request-id") if isinstance(headers, Mapping) else None
    return value if isinstance(value, str) else "unavailable"


__all__ = [
    "Phase718PublicationProjectionPort",
    "Phase718PublicationQueryApiAdapter",
    "Phase718PublicationRequestApiAdapter",
]
