"""Exact HTTP API v2 broker routes; no credentials in errors or application logs."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from .models import COOKIE_NAME, COOKIE_PATH, Config, Denied, Unavailable, exact_object, strict_json
from .service import JudgeSessionService

ROUTES = {COOKIE_PATH + suffix for suffix in ("/redeem", "/refresh", "/logout")}


def response(
    status: int, body: object = None, *, origin: str | None = None, cookie: str | None = None
) -> dict:
    headers = {
        "Cache-Control": "private, no-store, max-age=0",
        "Pragma": "no-cache",
        "Referrer-Policy": "no-referrer",
        "X-Content-Type-Options": "nosniff",
        "Content-Security-Policy": "default-src 'none'; frame-ancestors 'none'",
    }
    if status != 204:
        headers["Content-Type"] = "application/json"
    if origin is not None:
        headers["Access-Control-Allow-Origin"] = origin
        headers["Access-Control-Allow-Credentials"] = "true"
        headers["Vary"] = "Origin"
    result = {
        "statusCode": status,
        "headers": headers,
        "body": "" if status == 204 else json.dumps(body, separators=(",", ":")),
        "isBase64Encoded": False,
    }
    if cookie is not None:
        result["cookies"] = [cookie]
    return result


def error_response(status: int, *, origin: str | None = None) -> dict:
    code, message = {
        400: ("INVALID_REQUEST", "The judge session request is not valid."),
        403: ("JUDGE_ACCESS_DENIED", "Judge access is unavailable or has expired."),
        404: ("NOT_FOUND", "The requested route is unavailable."),
        405: ("METHOD_NOT_ALLOWED", "This route requires POST."),
        503: ("JUDGE_SESSION_UNAVAILABLE", "Judge access is temporarily unavailable."),
    }[status]
    return response(status, {"error": {"code": code, "message": message}}, origin=origin)


def cookie_header(value: str, max_age: int) -> str:
    return (
        f"{COOKIE_NAME}={value}; Path={COOKIE_PATH}; Max-Age={max_age}; "
        "HttpOnly; Secure; SameSite=Strict"
    )


def request_headers(event: Mapping[str, Any]) -> dict[str, str]:
    raw = event.get("headers", {})
    if not isinstance(raw, dict) or len(raw) > 100:
        raise ValueError
    headers = {}
    size = 0
    for key, value in raw.items():
        if (
            not isinstance(key, str)
            or not isinstance(value, str)
            or key.lower() in headers
            or "\r" in value
            or "\n" in value
        ):
            raise ValueError
        headers[key.lower()] = value
        size += len(key) + len(value)
    if size > 16384:
        raise ValueError
    return headers


def request_cookie(event: Mapping[str, Any], headers: Mapping[str, str]) -> str | None:
    values = event.get("cookies", [])
    if (
        not isinstance(values, list)
        or len(values) > 50
        or not all(isinstance(v, str) for v in values)
    ):
        raise ValueError
    if not values and "cookie" in headers:
        values = [headers["cookie"]]
    elif values and "cookie" in headers and headers["cookie"] != "; ".join(values):
        raise ValueError
    if sum(len(value) for value in values) > 8192:
        raise ValueError
    found = []
    for value in values:
        if "\r" in value or "\n" in value:
            raise ValueError
        for item in value.split(";"):
            name, separator, credential = item.strip().partition("=")
            if name == COOKIE_NAME:
                if not separator:
                    raise ValueError
                found.append(credential)
    if len(found) > 1:
        raise ValueError
    return found[0] if found else None


class JudgeSessionHandler:
    def __init__(self, config: Config, service: JudgeSessionService) -> None:
        self.config = config
        self.service = service

    def __call__(self, event: Mapping[str, Any], context: object | None = None) -> dict:
        origin = None
        try:
            if not isinstance(event, Mapping) or event.get("version") != "2.0":
                return error_response(400)
            path = event.get("rawPath")
            if path not in ROUTES:
                return error_response(404)
            http = event.get("requestContext", {}).get("http", {})
            if http.get("method") != "POST":
                return error_response(405)
            if (
                event.get("routeKey") != "POST " + path
                or event.get("rawQueryString", "") != ""
                or event.get("isBase64Encoded", False) is not False
            ):
                return error_response(400)
            headers = request_headers(event)
            if headers.get("origin") != self.config.application_origin:
                return error_response(403)
            origin = self.config.application_origin
            body = event.get("body")
            if body is None:
                body = ""
            if not isinstance(body, str) or len(body.encode("utf-8")) > 4096:
                return error_response(400, origin=origin)
            if body and headers.get("content-type", "").lower().replace(" ", "") not in {
                "application/json",
                "application/json;charset=utf-8",
            }:
                return error_response(400, origin=origin)
            decoded = strict_json(body) if body else {}
            if path.endswith("/redeem"):
                payload = exact_object(decoded, {"invitation"})
                issued = self.service.redeem(payload["invitation"])
            else:
                exact_object(decoded, set())
                cookie = request_cookie(event, headers)
                if path.endswith("/logout"):
                    self.service.logout(cookie)
                    return response(204, origin=origin, cookie=cookie_header("", 0))
                if cookie is None:
                    raise Denied
                issued = self.service.refresh(cookie)
            now = self.service.clock()
            remaining = issued.session.expires_at - now
            if remaining <= 0:
                raise Denied
            token_response = issued.token.response(now)
            token_response["expires_in"] = min(token_response["expires_in"], remaining)
            return response(
                200,
                token_response,
                origin=origin,
                cookie=cookie_header(issued.cookie_value, remaining),
            )
        except Denied:
            return error_response(403, origin=origin)
        except (ValueError, TypeError, KeyError, AttributeError):
            return error_response(400, origin=origin)
        except Unavailable:
            return error_response(503, origin=origin)
        except Exception:
            return error_response(503, origin=origin)
