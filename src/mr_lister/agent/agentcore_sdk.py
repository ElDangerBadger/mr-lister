"""Official AgentCore SDK boundary for the disposable Phase 3 runtime canary."""

from __future__ import annotations

import os
from base64 import b64decode
from collections.abc import Callable
from pathlib import Path

import boto3
from bedrock_agentcore import BedrockAgentCoreApp, RequestContext
from botocore.config import Config
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from strands.models import BedrockModel

from mr_lister.agent.contracts import (
    AgentCoreInvocation,
    AgentCoreResponse,
    PreparationDecision,
    PreparationRequest,
)
from mr_lister.agent.observability import LoggingAgentAuditSink
from mr_lister.agent.runtime import AgentExecutionError, StrandsPreparationRunner
from mr_lister.workflow.fakes import FakeIntelligenceAdapter, FakeProductionAdapter
from mr_lister.workflow.profiles import ProductProfileRepository
from mr_lister.workflow.service import ListingWorkflow
from mr_lister.workflow.store import InMemoryJobStore

NOVA_CONTROLLER_MODEL_ID = "us.amazon.nova-2-lite-v1:0"
SYNTHETIC_CANARY_JOB_ID = "job_agentcore_synthetic_canary"
SYNTHETIC_PNG = b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAIAAAACCAYAAABytg0kAAAAFUlEQVR4nGNU07X8z8DAwMAEIkAYABbVAY+Z/lCyAAAAAElFTkSuQmCC"
)
PreparationRunner = Callable[[PreparationRequest], PreparationDecision]


def _sanitized_error(status_code: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={"error": {"code": code, "message": message}},
    )


def create_agentcore_sdk_app(runner: PreparationRunner) -> BedrockAgentCoreApp:
    """Wrap the bounded runner in AWS's official AgentCore runtime application."""

    application = BedrockAgentCoreApp()

    @application.entrypoint
    def invoke(payload: dict, context: RequestContext):
        try:
            invocation = AgentCoreInvocation.model_validate(payload)
            if context.session_id is None:
                return _sanitized_error(
                    422,
                    "INVALID_AGENT_REQUEST",
                    "The AgentCore request envelope is invalid",
                )
            request = PreparationRequest(
                session_id=context.session_id,
                job_id=invocation.job_id,
                mode=invocation.mode,
                instruction=invocation.instruction,
            )
            return AgentCoreResponse(decision=runner(request)).model_dump(mode="json")
        except ValidationError:
            return _sanitized_error(
                422,
                "INVALID_AGENT_REQUEST",
                "The AgentCore request envelope is invalid",
            )
        except AgentExecutionError:
            return _sanitized_error(
                502,
                "AGENT_EXECUTION_FAILED",
                "The preparation agent could not complete safely",
            )
        except Exception:  # AgentCore must never serialize an unexpected provider exception.
            return _sanitized_error(
                502,
                "AGENT_EXECUTION_FAILED",
                "The preparation agent could not complete safely",
            )

    return application


def build_synthetic_canary_runtime() -> BedrockAgentCoreApp:
    """Build a non-durable, non-publishing deployment canary for Phase 3 only."""

    region = os.getenv("AWS_REGION", "us-west-2")
    session = boto3.Session(region_name=region)
    model = BedrockModel(
        boto_session=session,
        boto_client_config=Config(
            connect_timeout=10,
            read_timeout=120,
            retries={"max_attempts": 0, "mode": "standard"},
        ),
        model_id=NOVA_CONTROLLER_MODEL_ID,
        max_tokens=700,
        temperature=0.0,
        streaming=False,
        use_native_token_count=False,
    )
    workflow = ListingWorkflow(
        store=InMemoryJobStore(),
        profiles=ProductProfileRepository(Path("config/product_profiles")),
        intelligence=FakeIntelligenceAdapter(),
        production=FakeProductionAdapter(),
        job_id_factory=lambda: SYNTHETIC_CANARY_JOB_ID,
    )
    workflow.submit(
        filename="geometric_badger.png",
        content_type="image/png",
        content=SYNTHETIC_PNG,
        idempotency_key="agentcore-synthetic-canary",
        profile_id="synthetic_gildan_5000",
    )
    runner = StrandsPreparationRunner(
        workflow=workflow,
        model=model,
        audit_sink=LoggingAgentAuditSink(),
    )
    return create_agentcore_sdk_app(runner)
