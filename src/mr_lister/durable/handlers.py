"""Thin Lambda command handlers for the Phase 4 durable workflow."""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from hashlib import sha256
from pathlib import Path
from typing import Any

import boto3
from botocore.exceptions import ClientError
from pydantic import ValidationError

from mr_lister.agent.contracts import AgentCoreResponse
from mr_lister.durable.contracts import ApprovalCommand, ApprovalWaitCommand, JobCommand
from mr_lister.workflow.artifacts import S3ArtifactStore
from mr_lister.workflow.dynamodb import DynamoDBJobStore
from mr_lister.workflow.errors import (
    ConcurrentModificationError,
    IntelligenceUnavailableError,
    WorkflowError,
)
from mr_lister.workflow.fakes import FakeIntelligenceAdapter, FakeProductionAdapter
from mr_lister.workflow.models import ApprovalWaitStatus
from mr_lister.workflow.profiles import ProductProfileRepository
from mr_lister.workflow.service import ListingWorkflow


class RetryableWorkflowCommand(Exception):
    """Sanitized error name used by Step Functions retry rules."""


class TerminalWorkflowCommand(Exception):
    """Sanitized error name routed to the terminal workflow failure state."""


@dataclass(frozen=True)
class Phase4Services:
    workflow: ListingWorkflow
    step_functions: Any
    agentcore: Any | None = None
    agentcore_runtime_arn: str | None = None


@lru_cache(maxsize=1)
def build_services() -> Phase4Services:
    region = os.environ.get("AWS_REGION", "us-west-2")
    session = boto3.Session(region_name=region)
    dynamodb = session.client("dynamodb")
    s3 = session.client("s3")
    table_name = os.environ["MR_LISTER_STATE_TABLE"]
    artifact_bucket = os.environ["MR_LISTER_ARTIFACT_BUCKET"]
    agentcore_runtime_arn = os.environ.get("MR_LISTER_AGENTCORE_RUNTIME_ARN", "").strip()
    profile_directory = Path(
        os.environ.get("MR_LISTER_PROFILE_DIRECTORY", "config/product_profiles")
    )
    workflow = ListingWorkflow(
        store=DynamoDBJobStore(client=dynamodb, table_name=table_name),
        artifacts=S3ArtifactStore(client=s3, bucket=artifact_bucket),
        profiles=ProductProfileRepository(profile_directory),
        intelligence=FakeIntelligenceAdapter(),
        production=FakeProductionAdapter(),
    )
    return Phase4Services(
        workflow=workflow,
        step_functions=session.client("stepfunctions"),
        agentcore=(session.client("bedrock-agentcore") if agentcore_runtime_arn else None),
        agentcore_runtime_arn=agentcore_runtime_arn or None,
    )


def prepare_handler(
    event: dict[str, Any], _context: Any, *, _services: Phase4Services | None = None
) -> dict[str, Any]:
    command = _validate(JobCommand, event)
    services = _services or build_services()
    if services.agentcore_runtime_arn:
        job = _invoke_agentcore_preparation(services, command.job_id)
    else:
        job = _run_command(lambda: services.workflow.prepare(command.job_id))
    return {
        "job_id": job.job_id,
        "state": job.state.value,
        "review_version": job.review_version,
    }


def register_approval_wait_handler(
    event: dict[str, Any], _context: Any, *, _services: Phase4Services | None = None
) -> dict[str, Any]:
    command = _validate(ApprovalWaitCommand, event)
    services = _services or build_services()
    now = datetime.now(UTC)
    wait = _run_command(
        lambda: services.workflow.register_approval_wait(
            command.job_id,
            review_version=command.review_version,
            task_token=command.task_token,
            expires_at=now + timedelta(seconds=command.expires_in_seconds),
        )
    )
    return {
        "job_id": wait.job_id,
        "review_version": wait.review_version,
        "status": wait.status.value,
    }


def approval_handler(
    event: dict[str, Any], _context: Any, *, _services: Phase4Services | None = None
) -> dict[str, Any]:
    command = _validate(ApprovalCommand, event)
    services = _services or build_services()
    existing_wait = services.workflow.store.get_approval_wait(command.job_id)
    was_already_consumed = (
        existing_wait is not None and existing_wait.status is ApprovalWaitStatus.CONSUMED
    )
    job, task_token = _run_command(
        lambda: services.workflow.approve_waiting_job(
            command.job_id,
            command.review_version,
        )
    )
    callback_output = {
        "job_id": job.job_id,
        "review_version": job.review_version,
        "state": job.state.value,
    }
    replayed = False
    try:
        services.step_functions.send_task_success(
            taskToken=task_token,
            output=_compact_json(callback_output),
        )
    except ClientError as error:
        code = error.response.get("Error", {}).get("Code")
        if code in {"TaskDoesNotExist", "TaskTimedOut"}:
            if not was_already_consumed:
                raise TerminalWorkflowCommand("The approval callback is no longer active") from None
            replayed = True
        else:
            raise RetryableWorkflowCommand(
                "Approval callback delivery temporarily failed"
            ) from None
    return {**callback_output, "callback_replayed": replayed}


def fake_publish_handler(
    event: dict[str, Any], _context: Any, *, _services: Phase4Services | None = None
) -> dict[str, Any]:
    command = _validate(JobCommand, event)
    services = _services or build_services()
    job = _run_command(lambda: services.workflow.publish_draft(command.job_id))
    return {
        "job_id": job.job_id,
        "state": job.state.value,
        "review_version": job.review_version,
        "published_listing_id": job.published_listing_id,
    }


def fake_verify_handler(
    event: dict[str, Any], _context: Any, *, _services: Phase4Services | None = None
) -> dict[str, Any]:
    command = _validate(JobCommand, event)
    services = _services or build_services()
    job = _run_command(lambda: services.workflow.verify_publication(command.job_id))
    return {
        "job_id": job.job_id,
        "state": job.state.value,
        "review_version": job.review_version,
        "published_listing_id": job.published_listing_id,
    }


def _validate(model: Any, event: dict[str, Any]) -> Any:
    try:
        return model.model_validate(event)
    except ValidationError:
        raise TerminalWorkflowCommand("The durable command envelope is invalid") from None


def _run_command(command: Callable[[], Any]) -> Any:
    try:
        return command()
    except (IntelligenceUnavailableError, ConcurrentModificationError):
        raise RetryableWorkflowCommand("The durable command can be retried safely") from None
    except ClientError as error:
        code = error.response.get("Error", {}).get("Code", "")
        if code in {
            "InternalServerError",
            "InternalServerException",
            "ProvisionedThroughputExceededException",
            "RequestLimitExceeded",
            "ServiceUnavailable",
            "ServiceUnavailableException",
            "ThrottlingException",
        }:
            raise RetryableWorkflowCommand("Durable storage is temporarily unavailable") from None
        raise TerminalWorkflowCommand("Durable storage access was rejected") from None
    except WorkflowError as error:
        raise TerminalWorkflowCommand(
            f"The durable command was rejected with code {error.code}"
        ) from None


def _compact_json(value: dict[str, Any]) -> str:
    import json

    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _invoke_agentcore_preparation(services: Phase4Services, job_id: str) -> Any:
    if services.agentcore is None or services.agentcore_runtime_arn is None:
        raise TerminalWorkflowCommand("The AgentCore preparation bridge is not configured")
    payload = {
        "job_id": job_id,
        "mode": "prepare",
        "instruction": "Prepare and validate this staged job for human review.",
    }
    session_id = f"mr-lister-phase4-{sha256(job_id.encode()).hexdigest()}"
    try:
        response = services.agentcore.invoke_agent_runtime(
            agentRuntimeArn=services.agentcore_runtime_arn,
            runtimeSessionId=session_id,
            qualifier="DEFAULT",
            contentType="application/json",
            accept="application/json",
            payload=_compact_json(payload).encode(),
        )
        if response.get("statusCode") != 200:
            raise TerminalWorkflowCommand("The AgentCore preparation response was rejected")
        raw_response = _read_bounded_response(response.get("response"))
        AgentCoreResponse.model_validate_json(raw_response)
        return services.workflow.get_job(job_id)
    except TerminalWorkflowCommand:
        raise
    except ValidationError:
        raise TerminalWorkflowCommand(
            "The AgentCore preparation response was outside its contract"
        ) from None
    except ClientError as error:
        code = error.response.get("Error", {}).get("Code", "")
        if code in {
            "ThrottlingException",
            "TooManyRequestsException",
            "ServiceUnavailableException",
            "InternalServerException",
        }:
            raise RetryableWorkflowCommand(
                "AgentCore preparation is temporarily unavailable"
            ) from None
        raise TerminalWorkflowCommand("AgentCore preparation was rejected") from None
    except (OSError, TimeoutError):
        raise RetryableWorkflowCommand("AgentCore preparation is temporarily unavailable") from None


def _read_bounded_response(body: Any, *, maximum_bytes: int = 1_000_000) -> bytes:
    if body is None:
        raise TerminalWorkflowCommand("The AgentCore preparation response was empty")
    if hasattr(body, "read"):
        content = body.read(maximum_bytes + 1)
    else:
        content = b"".join(body)
    if not isinstance(content, bytes) or not content or len(content) > maximum_bytes:
        raise TerminalWorkflowCommand("The AgentCore preparation response was invalid")
    return content
