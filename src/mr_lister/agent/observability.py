"""Sanitized Phase 3 agent audit records and sinks."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import IO, Literal, Protocol

from pydantic import Field

from mr_lister.agent.contracts import (
    AGENT_FRAMEWORK,
    PREPARATION_AGENT_ID,
    AgentFramework,
    PreparationAgentId,
)
from mr_lister.contracts import ContractModel


class AgentAuditRecord(ContractModel):
    framework: AgentFramework = AGENT_FRAMEWORK
    agent_id: PreparationAgentId = PREPARATION_AGENT_ID
    correlation_id: str = Field(pattern=r"^[a-f0-9]{24}$")
    mode: Literal["prepare", "review", "revise"]
    status: Literal["succeeded", "failed"]
    elapsed_ms: float = Field(ge=0)
    cycles: int = Field(default=0, ge=0)
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    total_tokens: int = Field(default=0, ge=0)
    tool_calls: tuple[str, ...] = ()
    next_action: Literal["human_review", "revise", "retry"] | None = None
    error_code: Literal["AGENT_EXECUTION_FAILED"] | None = None


class AgentAuditSink(Protocol):
    def write(self, record: AgentAuditRecord) -> None: ...


class NoOpAgentAuditSink:
    def write(self, record: AgentAuditRecord) -> None:
        del record


class InMemoryAgentAuditSink:
    def __init__(self) -> None:
        self.records: list[AgentAuditRecord] = []

    def write(self, record: AgentAuditRecord) -> None:
        self.records.append(record)


class LoggingAgentAuditSink:
    """Emit a sanitized JSON record for capture by AgentCore/CloudWatch logging."""

    def __init__(self, stream: IO[str] | None = None) -> None:
        # AgentCore configures its own logger hierarchy. Writing the bounded audit
        # contract directly to stdout ensures the runtime captures it regardless of
        # application logger handler configuration.
        self._stream = stream or sys.stdout

    def write(self, record: AgentAuditRecord) -> None:
        print(f"agent_audit={record.model_dump_json()}", file=self._stream, flush=True)


class FilesystemAgentAuditSink:
    """Append sanitized JSON lines to a private local artifact."""

    def __init__(self, destination: Path) -> None:
        self._destination = destination

    def write(self, record: AgentAuditRecord) -> None:
        self._destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if os.name == "posix":
            self._destination.parent.chmod(0o700)
        with self._destination.open("a", encoding="utf-8") as stream:
            json.dump(record.model_dump(mode="json"), stream, sort_keys=True)
            stream.write("\n")
        if os.name == "posix":
            self._destination.chmod(0o600)
