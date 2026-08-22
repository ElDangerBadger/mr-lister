"""Fail-closed import surface for the Phase 6.2 infrastructure scaffold.

These handlers deliberately cannot execute application work.  Replacing them
with adapters over ``mr_lister.control`` is a deployment prerequisite; the
template advertises that state through its ``SCAFFOLD_ONLY`` output and
environment marker.
"""

from __future__ import annotations

from typing import Any


class Phase6ScaffoldNotReady(RuntimeError):
    """Prevent an infrastructure-only stack from mutating durable state."""


WORK_TYPE_STATE_MACHINE_ENV = {
    "prepare": "MR_LISTER_PREPARE_MACHINE_ARN",
    "synchronize_product": "MR_LISTER_SYNCHRONIZE_PRODUCT_MACHINE_ARN",
    "reconcile_product": "MR_LISTER_RECONCILE_PRODUCT_MACHINE_ARN",
    "refresh_economics": "MR_LISTER_REFRESH_ECONOMICS_MACHINE_ARN",
}


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


def _not_ready(component: str) -> dict[str, Any]:
    raise Phase6ScaffoldNotReady(
        f"Phase 6 infrastructure component {component!r} has no deployed application adapter"
    )


__all__ = [
    "WORK_TYPE_STATE_MACHINE_ENV",
    "Phase6ScaffoldNotReady",
    "dispatcher_handler",
    "preparation_dispatch_handler",
    "provider_draft_handler",
    "settlement_handler",
]
