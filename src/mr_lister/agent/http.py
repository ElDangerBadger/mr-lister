"""AgentCore-compatible HTTP transport for the bounded preparation agent."""

from __future__ import annotations

from collections.abc import Callable
from typing import Annotated

from fastapi import FastAPI, Header
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from mr_lister.agent.contracts import (
    AGENT_FRAMEWORK,
    PREPARATION_AGENT_ID,
    AgentCoreInvocation,
    AgentCoreResponse,
    PreparationDecision,
    PreparationRequest,
)
from mr_lister.agent.runtime import AgentExecutionError

PreparationRunner = Callable[[PreparationRequest], PreparationDecision]


def create_agentcore_app(runner: PreparationRunner) -> FastAPI:
    """Create the HTTP `/invocations` and `/ping` AgentCore service contract."""

    application = FastAPI(title="Mr Lister Preparation Agent", version="0.1.0")

    @application.exception_handler(RequestValidationError)
    async def request_validation_error(_request, _error) -> JSONResponse:
        return JSONResponse(
            status_code=422,
            content={
                "error": {
                    "code": "INVALID_AGENT_REQUEST",
                    "message": "The AgentCore request envelope is invalid",
                }
            },
        )

    @application.exception_handler(AgentExecutionError)
    async def agent_execution_error(_request, _error) -> JSONResponse:
        return JSONResponse(
            status_code=502,
            content={
                "error": {
                    "code": "AGENT_EXECUTION_FAILED",
                    "message": "The preparation agent could not complete safely",
                }
            },
        )

    @application.get("/ping")
    def ping() -> dict[str, str]:
        return {"status": "Healthy"}

    @application.post("/invocations", response_model=AgentCoreResponse)
    def invoke(
        invocation: AgentCoreInvocation,
        runtime_session_id: Annotated[
            str,
            Header(
                alias="X-Amzn-Bedrock-AgentCore-Runtime-Session-Id",
                min_length=1,
                max_length=100,
                pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,99}$",
            ),
        ],
    ) -> AgentCoreResponse:
        request = PreparationRequest(
            session_id=runtime_session_id,
            job_id=invocation.job_id,
            mode=invocation.mode,
            instruction=invocation.instruction,
        )
        return AgentCoreResponse(
            framework=AGENT_FRAMEWORK,
            agent_id=PREPARATION_AGENT_ID,
            decision=runner(request),
        )

    return application
