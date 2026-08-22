"""Phase 6 response contract that binds AgentCore output to one opaque session."""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from mr_lister.agent.contracts import (
    AgentFramework,
    PreparationAgentId,
    PreparationDecision,
)
from mr_lister.contracts import ContractModel


class Phase6AgentCoreResponse(ContractModel):
    """Exact Strands identity plus the opaque job/work correlation expected by the caller."""

    status: Literal["success"] = "success"
    framework: AgentFramework
    agent_id: PreparationAgentId
    correlation_id: str = Field(pattern=r"^[a-f0-9]{24}$")
    work_binding: str = Field(pattern=r"^[a-f0-9]{64}$")
    evidence_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    decision: PreparationDecision
