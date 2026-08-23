"""Lazy production entrypoints for the seven Phase 6 Lambda functions.

This module is imported only by a packaged Lambda when the infrastructure marker is
explicitly switched away from ``SCAFFOLD_ONLY``.  Builders cache immutable dependencies
across warm invocations but never retain an event, JWT claim, seller identity, or command.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from threading import Lock
from typing import Any, Protocol

from mr_lister.cloud.http import RouteNotFoundError, error_response, request_id_from_event
from mr_lister.cloud.phase6_composition import (
    build_command_api_handler,
    build_health_readiness_handler,
    build_query_api_handler,
    build_upload_api_handler,
)
from mr_lister.cloud.phase6_machine import Phase6MachineExecutionError
from mr_lister.cloud.phase6_machine_composition import (
    build_dispatcher_handler,
    build_preparation_handler,
    build_provider_handler,
    build_settlement_handler,
)

HEALTH_ROUTE_KEY = "GET /health"


class LambdaHandler(Protocol):
    def __call__(
        self,
        event: Mapping[str, Any],
        context: object | None = None,
    ) -> dict[str, Any]: ...


class _LazyHandler:
    __slots__ = ("_builder", "_delegate", "_lock", "_public_http")

    def __init__(self, builder: Callable[[], LambdaHandler], *, public_http: bool) -> None:
        self._builder = builder
        self._delegate: LambdaHandler | None = None
        self._lock = Lock()
        self._public_http = public_http

    def __call__(
        self,
        event: Mapping[str, Any],
        context: object | None = None,
    ) -> dict[str, Any]:
        try:
            return self._get()(event, context)
        except Exception:
            if self._public_http:
                return error_response(RuntimeError(), request_id=request_id_from_event(event))
            raise Phase6MachineExecutionError("Phase 6 machine composition failed safely") from None

    def _get(self) -> LambdaHandler:
        delegate = self._delegate
        if delegate is not None:
            return delegate
        with self._lock:
            if self._delegate is None:
                self._delegate = self._builder()
            return self._delegate


def _environment() -> Mapping[str, object]:
    # Return a fresh shallow copy so a builder cannot retain a mutable process environment view.
    return dict(os.environ)


_dispatcher = _LazyHandler(
    lambda: build_dispatcher_handler(_environment()),
    public_http=False,
)
_preparation = _LazyHandler(
    lambda: build_preparation_handler(_environment()),
    public_http=False,
)
_provider = _LazyHandler(
    lambda: build_provider_handler(_environment()),
    public_http=False,
)
_settlement = _LazyHandler(
    lambda: build_settlement_handler(_environment()),
    public_http=False,
)
_upload_api = _LazyHandler(
    lambda: build_upload_api_handler(_environment()),
    public_http=True,
)
_query_api = _LazyHandler(
    lambda: build_query_api_handler(_environment()),
    public_http=True,
)
_health = _LazyHandler(
    lambda: build_health_readiness_handler(_environment()),
    public_http=True,
)
_command_api = _LazyHandler(
    lambda: build_command_api_handler(_environment()),
    public_http=True,
)


def dispatcher_handler(event: Mapping[str, Any], context: object | None = None) -> dict[str, Any]:
    return _dispatcher(event, context)


def preparation_dispatch_handler(
    event: Mapping[str, Any],
    context: object | None = None,
) -> dict[str, Any]:
    return _preparation(event, context)


def provider_draft_handler(
    event: Mapping[str, Any],
    context: object | None = None,
) -> dict[str, Any]:
    return _provider(event, context)


def settlement_handler(event: Mapping[str, Any], context: object | None = None) -> dict[str, Any]:
    return _settlement(event, context)


def upload_api_handler(event: Mapping[str, Any], context: object | None = None) -> dict[str, Any]:
    return _upload_api(event, context)


def review_query_api_handler(
    event: Mapping[str, Any],
    context: object | None = None,
) -> dict[str, Any]:
    route_key = event.get("routeKey") if isinstance(event, Mapping) else None
    if route_key == HEALTH_ROUTE_KEY:
        return _health(event, context)
    if isinstance(route_key, str) and route_key.startswith("GET /v1/jobs"):
        return _query_api(event, context)
    return error_response(
        RouteNotFoundError(),
        request_id=request_id_from_event(event),
    )


def seller_command_api_handler(
    event: Mapping[str, Any],
    context: object | None = None,
) -> dict[str, Any]:
    return _command_api(event, context)


__all__ = [
    "HEALTH_ROUTE_KEY",
    "dispatcher_handler",
    "preparation_dispatch_handler",
    "provider_draft_handler",
    "review_query_api_handler",
    "seller_command_api_handler",
    "settlement_handler",
    "upload_api_handler",
]
