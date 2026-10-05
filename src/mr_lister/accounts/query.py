"""Read-only HTTP API v2 account projection from verified account-group claims."""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping

from mr_lister.cloud.auth import (
    AccessDeniedError,
    AuthenticationRequiredError,
    SellerClaimsPolicy,
    authenticate_seller,
)

from .models import ACCOUNT_GROUP, ACCOUNT_SCOPE, AccountConfig, MerchantAccount
from .store import AccountReader

ROUTE = "GET /v1/account"
PATH = "/v1/account"


def error_response(status: int, request_id: str = "unavailable") -> dict:
    code, message = {
        400: ("INVALID_REQUEST", "The account request is not valid."),
        401: ("AUTHENTICATION_REQUIRED", "Sign in to view your account."),
        403: ("FORBIDDEN", "Account access is unavailable."),
        404: ("NOT_FOUND", "The requested account is unavailable."),
        503: ("ACCOUNT_UNAVAILABLE", "Account setup is temporarily unavailable."),
    }[status]
    return response(status, {"error": {"code": code, "message": message, "request_id": request_id}})


def response(status: int, body: dict) -> dict:
    return {
        "statusCode": status,
        "headers": {
            "Content-Type": "application/json",
            "Cache-Control": "private, no-store, max-age=0",
            "Pragma": "no-cache",
            "Referrer-Policy": "no-referrer",
            "X-Content-Type-Options": "nosniff",
        },
        "body": json.dumps(body, separators=(",", ":")),
        "isBase64Encoded": False,
    }


class AccountQueryHandler:
    def __init__(
        self, config: AccountConfig, *, reader_factory: Callable[[], AccountReader]
    ) -> None:
        self._config = config
        self._reader_factory = reader_factory
        self._policy = SellerClaimsPolicy(
            issuer=config.issuer,
            client_id=config.client_id,
            required_scope=ACCOUNT_SCOPE,
            required_group=ACCOUNT_GROUP,
        )

    def __call__(self, event: object, context: object | None = None) -> dict:
        request_id = "unavailable"
        try:
            if not isinstance(event, Mapping) or event.get("version") != "2.0":
                return error_response(400)
            request_context = event.get("requestContext")
            if not isinstance(request_context, Mapping):
                return error_response(400)
            supplied_id = request_context.get("requestId")
            if isinstance(supplied_id, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,128}", supplied_id):
                request_id = supplied_id
            if event.get("routeKey") != ROUTE or event.get("rawPath") != PATH:
                return error_response(404, request_id)
            http = request_context.get("http")
            if (
                not isinstance(http, Mapping)
                or http.get("method") != "GET"
                or http.get("path") != PATH
                or event.get("rawQueryString", "") != ""
                or event.get("queryStringParameters") not in (None, {})
                or event.get("pathParameters") not in (None, {})
                or event.get("body") not in (None, "")
                or event.get("isBase64Encoded", False) is not False
            ):
                return error_response(400, request_id)
            seller = authenticate_seller(event, policy=self._policy)
            if seller.owner_id in self._config.reserved_owner_ids:
                return error_response(403, request_id)
            account = self._reader_factory().get(seller.owner_id)
            if account is None:
                return error_response(404, request_id)
            account = MerchantAccount.from_payload(account.payload())
            account.identity.require_configuration(self._config)
            if account.owner_id != seller.owner_id:
                return error_response(503, request_id)
            return response(200, account.public_projection())
        except AuthenticationRequiredError:
            return error_response(401, request_id)
        except AccessDeniedError:
            return error_response(403, request_id)
        except Exception:
            return error_response(503, request_id)
