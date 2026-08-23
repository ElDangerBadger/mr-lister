"""Local Phase 1 workflow services and ports.

The legacy workflow service is lazy so importing Phase 6 validation/models does not load its
broader Phase 1 publication surface into a least-capability Lambda or AgentCore runtime.
"""

from __future__ import annotations

from typing import Any

__all__ = ["ListingWorkflow"]


def __getattr__(name: str) -> Any:
    if name == "ListingWorkflow":
        from mr_lister.workflow.service import ListingWorkflow

        return ListingWorkflow
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
