"""Fail-closed import surface for the Phase 6.4 infrastructure scaffold.

These handlers deliberately cannot execute application work.  Replacing them
with adapters over ``mr_lister.control`` is a deployment prerequisite; the
template advertises that state through its ``SCAFFOLD_ONLY`` output and
environment marker.
"""

from __future__ import annotations

import json
from typing import Any


class Phase6ScaffoldNotReady(RuntimeError):
    """Prevent an infrastructure-only stack from mutating durable state."""


WORK_TYPE_STATE_MACHINE_ENV = {
    "prepare": "MR_LISTER_PREPARE_MACHINE_ARN",
    "synchronize_product": "MR_LISTER_SYNCHRONIZE_PRODUCT_MACHINE_ARN",
    "reconcile_product": "MR_LISTER_RECONCILE_PRODUCT_MACHINE_ARN",
    "refresh_economics": "MR_LISTER_REFRESH_ECONOMICS_MACHINE_ARN",
}

UPLOAD_API_ROUTE_KEYS = frozenset(
    {
        "POST /v1/uploads",
        "GET /v1/uploads/{upload_id}",
        "POST /v1/uploads/{upload_id}/authorize",
        "POST /v1/uploads/{upload_id}/complete",
        "POST /v1/uploads/{upload_id}/cancel",
    }
)

REVIEW_QUERY_API_ROUTE_KEYS = frozenset(
    {
        "GET /v1/jobs",
        "GET /v1/jobs/{job_id}",
        "GET /v1/jobs/{job_id}/review",
        "GET /v1/jobs/{job_id}/artwork-preview",
    }
)

SELLER_COMMAND_API_ROUTE_KEYS = frozenset(
    {
        "PUT /v1/jobs/{job_id}/review/listing",
        "POST /v1/jobs/{job_id}/economics/refresh",
        "POST /v1/jobs/{job_id}/approve",
        "POST /v1/jobs/{job_id}/cancel",
        "POST /v1/jobs/{job_id}/retry",
    }
)

HEALTH_ROUTE_KEY = "GET /health"


def dispatcher_handler(event: dict[str, Any], _context: Any) -> dict[str, Any]:
    """Expose only the frozen four-type dispatch map, then fail closed."""

    work_type = event.get("work_type")
    if isinstance(work_type, str) and work_type not in WORK_TYPE_STATE_MACHINE_ENV:
        raise ValueError("Unsupported Phase 6 work type")
    return _not_ready("dispatcher")


def preparation_dispatch_handler(_event: dict[str, Any], _context: Any) -> dict[str, Any]:
    return _not_ready("preparation_dispatch")


def provider_draft_handler(_event: dict[str, Any], _context: Any) -> dict[str, Any]:
    return _not_ready("provider_draft")


def settlement_handler(_event: dict[str, Any], _context: Any) -> dict[str, Any]:
    return _not_ready("settlement")


def upload_api_handler(event: dict[str, Any], _context: Any) -> dict[str, Any]:
    """Fail closed on the exact upload-intake route set until its adapter exists."""

    _require_route(event, UPLOAD_API_ROUTE_KEYS)
    return _not_ready("upload_api")


def review_query_api_handler(event: dict[str, Any], _context: Any) -> dict[str, Any]:
    """Expose only an information-minimal health result; all seller reads fail closed."""

    route_key = event.get("routeKey")
    if route_key == HEALTH_ROUTE_KEY:
        return {
            "statusCode": 503,
            "headers": {
                "Cache-Control": "no-store",
                "Content-Type": "application/json",
            },
            "body": json.dumps({"status": "scaffold_only"}, separators=(",", ":")),
        }
    _require_route(event, REVIEW_QUERY_API_ROUTE_KEYS)
    return _not_ready("review_query_api")


def seller_command_api_handler(event: dict[str, Any], _context: Any) -> dict[str, Any]:
    """Fail closed on the exact seller-command route set until its adapter exists."""

    _require_route(event, SELLER_COMMAND_API_ROUTE_KEYS)
    return _not_ready("seller_command_api")


def _require_route(event: dict[str, Any], allowed: frozenset[str]) -> None:
    route_key = event.get("routeKey")
    if not isinstance(route_key, str) or route_key not in allowed:
        raise ValueError("Unsupported Phase 6 API route")


def _not_ready(component: str) -> dict[str, Any]:
    raise Phase6ScaffoldNotReady(
        f"Phase 6 infrastructure component {component!r} has no deployed application adapter"
    )


__all__ = [
    "HEALTH_ROUTE_KEY",
    "REVIEW_QUERY_API_ROUTE_KEYS",
    "SELLER_COMMAND_API_ROUTE_KEYS",
    "UPLOAD_API_ROUTE_KEYS",
    "WORK_TYPE_STATE_MACHINE_ENV",
    "Phase6ScaffoldNotReady",
    "dispatcher_handler",
    "preparation_dispatch_handler",
    "provider_draft_handler",
    "review_query_api_handler",
    "seller_command_api_handler",
    "settlement_handler",
    "upload_api_handler",
]
