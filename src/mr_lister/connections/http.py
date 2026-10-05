"""Verified identity precedes account lookup, credential access, and transport construction."""

from __future__ import annotations

from collections.abc import Callable, Mapping

from mr_lister.accounts.models import ACCOUNT_GROUP, ACCOUNT_SCOPE, MerchantAccount, strict_json
from mr_lister.accounts.query import error_response as account_error_response
from mr_lister.accounts.query import response
from mr_lister.cloud.auth import (
    AccessDeniedError,
    AuthenticationRequiredError,
    SellerClaimsPolicy,
    authenticate_seller,
)

from .models import (
    MERCHANT_AUTHORIZATION_VERSION,
    ConnectionConfig,
    ConnectionError,
    InvalidConnectionRequest,
)

ROUTES = {
    "GET /v1/store-setup": "get",
    "POST /v1/connections/printify/validate": "validate",
    "POST /v1/connections/printify/select-shop": "select",
    "POST /v1/connections/printify/activate": "activate",
}


def unavailable() -> dict:
    return connection_error(ConnectionError())


def connection_error(error: ConnectionError) -> dict:
    return response(
        error.status,
        {"error": {"code": error.code, "message": str(error), "request_id": "unavailable"}},
    )


class ConnectionHttpHandler:
    def __init__(
        self,
        *,
        config: ConnectionConfig,
        account_reader_factory: Callable,
        service_factory: Callable,
        allowed_operation: str,
    ) -> None:
        self.config = config
        self._accounts = account_reader_factory
        self._service = service_factory
        self._allowed = allowed_operation
        self._policy = SellerClaimsPolicy(
            issuer=config.accounts.issuer,
            client_id=config.accounts.client_id,
            required_scope=ACCOUNT_SCOPE,
            required_group=ACCOUNT_GROUP,
        )

    def __call__(self, event: object, context: object = None) -> dict:
        try:
            if not isinstance(event, Mapping) or event.get("version") != "2.0":
                raise InvalidConnectionRequest
            seller = authenticate_seller(event, policy=self._policy)
            if seller.owner_id in self.config.accounts.reserved_owner_ids:
                raise AccessDeniedError
            operation = ROUTES.get(event.get("routeKey"))
            if operation is None or operation != self._allowed:
                return account_error_response(404)
            method, path = event["routeKey"].split(" ")
            http = event.get("requestContext", {}).get("http", {})
            if (
                event.get("rawPath") != path
                or http.get("method") != method
                or http.get("path") != path
                or event.get("rawQueryString", "") != ""
                or event.get("queryStringParameters") not in (None, {})
                or event.get("pathParameters") not in (None, {})
                or event.get("isBase64Encoded", False) is not False
            ):
                raise InvalidConnectionRequest
            raw = event.get("body")
            if operation == "get":
                if raw not in (None, ""):
                    raise InvalidConnectionRequest
                body = {}
            else:
                body = strict_json(raw, limit=8192)
                required = {
                    "validate": {"token", "authorization"},
                    "select": {"candidate_id", "shop_id", "expected_setup_version"},
                    "activate": set(),
                }[operation]
                if not isinstance(body, dict) or set(body) != required:
                    raise InvalidConnectionRequest
                if operation == "validate":
                    authorization = body["authorization"]
                    if (
                        not isinstance(authorization, dict)
                        or set(authorization) != {"accepted", "terms_version", "privacy_version"}
                        or authorization["accepted"] is not True
                        or authorization["terms_version"] != MERCHANT_AUTHORIZATION_VERSION
                        or authorization["privacy_version"] != MERCHANT_AUTHORIZATION_VERSION
                    ):
                        raise InvalidConnectionRequest
            headers = event.get("headers", {})
            if not isinstance(headers, Mapping):
                raise InvalidConnectionRequest
            idem_headers = [
                value
                for key, value in headers.items()
                if isinstance(key, str) and key.lower() == "idempotency-key"
            ]
            if operation in ("validate", "select"):
                if len(idem_headers) != 1:
                    raise InvalidConnectionRequest
                from .models import operation_id

                operation_id(seller.owner_id, idem_headers[0], operation)
            account = self._accounts().get(seller.owner_id)
            if account is None:
                raise AccessDeniedError
            account = MerchantAccount.from_payload(account.payload())
            account.identity.require_configuration(self.config.accounts)
            if account.owner_id != seller.owner_id:
                raise AccessDeniedError
            service = self._service()
            if operation == "get":
                result = service.get(account)
            elif operation == "validate":
                result = service.validate(
                    account, token=body["token"], idempotency_key=idem_headers[0]
                )
            elif operation == "select":
                result = service.select(account, **body, idempotency_key=idem_headers[0])
            else:
                result = service.activate(account)
            return response(200, result)
        except AuthenticationRequiredError:
            return account_error_response(401)
        except AccessDeniedError:
            return account_error_response(403)
        except ConnectionError as error:
            return connection_error(error)
        except (ValueError, TypeError):
            return connection_error(InvalidConnectionRequest())
        except Exception:
            return unavailable()
