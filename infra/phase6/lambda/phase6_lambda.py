"""Fail-closed import surface for the Phase 6 infrastructure package.

The checked SAM template keeps ``MR_LISTER_PHASE6_SCAFFOLD_ONLY=true`` and every
handler therefore remains inert.  A release package may switch the marker to the
exact string ``false`` only after it includes the tested :mod:`mr_lister` production
composition and closes the deployment gates.  Any missing, malformed, or differently
cased marker fails closed.
"""

from __future__ import annotations

import json
import os
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
    """Expose only the frozen four-type dispatch map and gated production adapter."""

    work_type = event.get("work_type")
    if isinstance(work_type, str) and work_type not in WORK_TYPE_STATE_MACHINE_ENV:
        raise ValueError("Unsupported Phase 6 work type")
    return _delegate("dispatcher_handler", event, _context, component="dispatcher")


def preparation_dispatch_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    return _delegate(
        "preparation_dispatch_handler",
        event,
        context,
        component="preparation_dispatch",
    )


def provider_draft_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    return _delegate("provider_draft_handler", event, context, component="provider_draft")


def settlement_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    return _delegate("settlement_handler", event, context, component="settlement")


def source_version_retention_handler(
    event: dict[str, Any],
    context: Any,
) -> dict[str, Any]:
    """Run only the gated, reference-aware exact-version retention adapter."""

    if not _production_enabled():
        return _not_ready("source_version_retention")
    try:
        from mr_lister.cloud.phase6_retention_entrypoint import (
            source_version_retention_handler as handler,
        )
    except Exception:
        raise Phase6ScaffoldNotReady(
            "Phase 6 production component 'source_version_retention' is unavailable"
        ) from None
    return handler(event, context)


def upload_api_handler(event: dict[str, Any], _context: Any) -> dict[str, Any]:
    """Fail closed on the exact upload-intake route set until its adapter exists."""

    _require_route(event, UPLOAD_API_ROUTE_KEYS)
    return _delegate("upload_api_handler", event, _context, component="upload_api")


def review_query_api_handler(event: dict[str, Any], _context: Any) -> dict[str, Any]:
    """Expose an information-minimal scaffold health result or the gated read adapter."""

    route_key = event.get("routeKey")
    if route_key == HEALTH_ROUTE_KEY:
        if _production_enabled():
            return _delegate(
                "review_query_api_handler",
                event,
                _context,
                component="review_query_api",
            )
        return {
            "statusCode": 503,
            "headers": {
                "Cache-Control": "no-store",
                "Content-Type": "application/json",
            },
            "body": json.dumps({"status": "scaffold_only"}, separators=(",", ":")),
        }
    _require_route(event, REVIEW_QUERY_API_ROUTE_KEYS)
    return _delegate(
        "review_query_api_handler",
        event,
        _context,
        component="review_query_api",
    )


def seller_command_api_handler(event: dict[str, Any], _context: Any) -> dict[str, Any]:
    """Fail closed on the exact seller-command route set until its adapter exists."""

    _require_route(event, SELLER_COMMAND_API_ROUTE_KEYS)
    return _delegate(
        "seller_command_api_handler",
        event,
        _context,
        component="seller_command_api",
    )


def _require_route(event: dict[str, Any], allowed: frozenset[str]) -> None:
    route_key = event.get("routeKey")
    if not isinstance(route_key, str) or route_key not in allowed:
        raise ValueError("Unsupported Phase 6 API route")


def _not_ready(component: str) -> dict[str, Any]:
    raise Phase6ScaffoldNotReady(
        f"Phase 6 infrastructure component {component!r} has no deployed application adapter"
    )


def _production_enabled() -> bool:
    return os.environ.get("MR_LISTER_PHASE6_SCAFFOLD_ONLY") == "false"


def _delegate(
    handler_name: str,
    event: dict[str, Any],
    context: Any,
    *,
    component: str,
) -> dict[str, Any]:
    if not _production_enabled():
        return _not_ready(component)
    try:
        from mr_lister.cloud import phase6_entrypoints

        handler = getattr(phase6_entrypoints, handler_name)
    except Exception:
        # A partial or drifted release package is never allowed to fall back to scaffold logic.
        raise Phase6ScaffoldNotReady(
            f"Phase 6 production component {component!r} is unavailable"
        ) from None
    return handler(event, context)


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
    "source_version_retention_handler",
    "upload_api_handler",
]
