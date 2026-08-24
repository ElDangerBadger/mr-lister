"""Fail-closed import surface for the isolated Phase 7.4 query scaffold.

The checked SAM template supplies the one exact disabled environment tuple.  It registers no
event source and packages no application dependencies, so a direct invocation cannot read
DynamoDB.  A later sealed bundle may include the named cloud entrypoint, but contract 7.0.1 still
requires that entrypoint to refuse before constructing an adapter or making a read.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Mapping
from typing import Any, Final

# Lambda's code mount is read-only.  Keeping bytecode disabled makes a local scaffold invocation
# exercise the same import behavior and prevents an unmanifested cache from becoming executable.
sys.dont_write_bytecode = True


class Phase7ReadOnlyScaffoldNotReady(RuntimeError):
    """The disabled infrastructure scaffold has no readable application capability."""


REQUIRED_DISABLED_ENVIRONMENT: Final[dict[str, str]] = {
    "MR_LISTER_PHASE7_SCAFFOLD_ONLY": "true",
    "MR_LISTER_PHASE7_PUBLICATION_ENABLED": "false",
    "MR_LISTER_PHASE7_REQUEST_ENABLED": "false",
    "MR_LISTER_PHASE7_QUERY_ENABLED": "false",
}

PRODUCTION_ENTRYPOINT: Final[str] = (
    "mr_lister.cloud.phase7_entrypoints.publication_query_api_handler"
)


def publication_query_api_handler(
    event: Mapping[str, Any],
    context: object | None = None,
) -> dict[str, Any]:
    """Reach only the exact-disabled query entrypoint; every drifted marker fails locally."""

    _require_exact_disabled_environment()
    try:
        from mr_lister.cloud.phase7_entrypoints import (
            publication_query_api_handler as handler,
        )
    except Exception:
        raise Phase7ReadOnlyScaffoldNotReady(
            "Phase 7 publication-status query application is unavailable"
        ) from None
    return handler(event, context)


def _require_exact_disabled_environment() -> None:
    if any(
        os.environ.get(name) != expected for name, expected in REQUIRED_DISABLED_ENVIRONMENT.items()
    ):
        raise Phase7ReadOnlyScaffoldNotReady("Phase 7 publication-status query remains disabled")


__all__ = [
    "PRODUCTION_ENTRYPOINT",
    "REQUIRED_DISABLED_ENVIRONMENT",
    "Phase7ReadOnlyScaffoldNotReady",
    "publication_query_api_handler",
]
