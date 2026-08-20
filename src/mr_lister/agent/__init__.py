"""Bounded agent contracts with lazy access to the optional Strands runtime."""

from __future__ import annotations

from typing import Any

from mr_lister.agent.contracts import PreparationDecision, PreparationRequest
from mr_lister.agent.observability import AgentAuditRecord, InMemoryAgentAuditSink

__all__ = [
    "PreparationDecision",
    "PreparationRequest",
    "AgentAuditRecord",
    "InMemoryAgentAuditSink",
    "build_preparation_agent",
    "preparation_prompt",
]


def __getattr__(name: str) -> Any:
    """Load Strands-backed helpers only when a caller explicitly requests them."""

    if name in {"build_preparation_agent", "preparation_prompt"}:
        from mr_lister.agent.runtime import build_preparation_agent, preparation_prompt

        return {
            "build_preparation_agent": build_preparation_agent,
            "preparation_prompt": preparation_prompt,
        }[name]
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
