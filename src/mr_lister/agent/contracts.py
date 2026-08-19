"""Application-owned contracts for the Phase 3 agent boundary."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field, StringConstraints

from mr_lister.contracts import ContractModel

SafeIdentifier = Annotated[
    str,
    StringConstraints(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,99}$"),
]


class PreparationRequest(ContractModel):
    """Trusted application envelope supplied to the Strands preparation agent."""

    session_id: SafeIdentifier
    job_id: SafeIdentifier
    mode: Literal["prepare", "review", "revise"] = "review"
    instruction: str = Field(min_length=1, max_length=2_000)


class PreparationDecision(ContractModel):
    """Structured recommendation returned by the agent after tool use."""

    summary: str = Field(min_length=1, max_length=2_000)
    recommendation: str = Field(min_length=1, max_length=2_000)
    next_action: Literal["human_review", "revise", "retry"]
    requires_human_approval: Literal[True] = True
    publication_authorized: Literal[False] = False


class AgentCoreInvocation(ContractModel):
    """JSON body accepted by the AgentCore HTTP entry point."""

    job_id: SafeIdentifier
    mode: Literal["prepare", "review", "revise"] = "review"
    instruction: str = Field(min_length=1, max_length=2_000)


class AgentCoreResponse(ContractModel):
    """Non-streaming response returned through the AgentCore HTTP contract."""

    status: Literal["success"] = "success"
    decision: PreparationDecision
