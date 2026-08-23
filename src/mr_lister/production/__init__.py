"""Marketplace adapters with lazy legacy exports.

Importing a narrow Phase 6 module must not eagerly load the older broad production adapter.
Legacy public names remain available through explicit attribute access.
"""

from __future__ import annotations

from typing import Any

__all__ = [
    "PrintifyProductionAdapter",
    "PrintifyConnection",
    "PrintifyAuthenticationError",
    "PrintifyCatalogClient",
    "PrintifyCatalogMismatchError",
    "PrintifyDraftProduct",
    "PrintifyInputError",
    "PrintifyPlacementGroup",
    "PrintifyProductionClient",
    "PrintifyProductProfile",
    "PrintifyResolvedProfile",
    "PrintifyResolvedVariant",
    "PrintifyUnavailableError",
    "PrintifyUploadedImage",
    "UrllibPrintifyTransport",
    "load_printify_connection",
]

_PRINTIFY_EXPORTS = frozenset(
    {
        "PrintifyAuthenticationError",
        "PrintifyCatalogClient",
        "PrintifyCatalogMismatchError",
        "PrintifyDraftProduct",
        "PrintifyInputError",
        "PrintifyPlacementGroup",
        "PrintifyProductionClient",
        "PrintifyProductProfile",
        "PrintifyResolvedProfile",
        "PrintifyResolvedVariant",
        "PrintifyUnavailableError",
        "PrintifyUploadedImage",
        "UrllibPrintifyTransport",
    }
)


def __getattr__(name: str) -> Any:
    if name == "PrintifyProductionAdapter":
        from mr_lister.production.adapter import PrintifyProductionAdapter

        return PrintifyProductionAdapter
    if name in _PRINTIFY_EXPORTS:
        from mr_lister.production import printify

        return getattr(printify, name)
    if name in {"PrintifyConnection", "load_printify_connection"}:
        from mr_lister.production import settings

        return getattr(settings, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
