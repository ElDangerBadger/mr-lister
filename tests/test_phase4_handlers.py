from __future__ import annotations

import json
from base64 import b64decode
from datetime import timedelta

import pytest
from botocore.exceptions import ClientError

from mr_lister.agent.contracts import AGENT_FRAMEWORK, PREPARATION_AGENT_ID
from mr_lister.contracts import JobState
from mr_lister.durable.handlers import (
    Phase4Services,
    RetryableWorkflowCommand,
    TerminalWorkflowCommand,
    _run_command,
    approval_handler,
    fake_publish_handler,
    fake_verify_handler,
    prepare_handler,
    register_approval_wait_handler,
)

SYNTHETIC_PNG = b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAIAAAACCAYAAABytg0kAAAAFUlEQVR4nGNU07X8z8DAwMAEIkAYABbVAY+Z/lCyAAAAAElFTkSuQmCC"
)


def submit(workflow, *, key: str):
    return workflow.submit(
        filename="geometric_badger.png",
        content_type="image/png",
        content=SYNTHETIC_PNG,
        idempotency_key=key,
        profile_id="synthetic_gildan_5000",
    )


class RecordingStepFunctions:
    def __init__(self, *, replay: bool = False) -> None:
        self.replay = replay
        self.successes: list[dict[str, str]] = []

    def send_task_success(self, **request: str) -> None:
        if self.replay:
            raise ClientError(
                {"Error": {"Code": "TaskDoesNotExist", "Message": "synthetic replay"}},
                "SendTaskSuccess",
            )
        self.successes.append(request)


class AgentCoreBody:
    def __init__(self, content: bytes) -> None:
        self.content = content

    def read(self, _amount: int) -> bytes:
        return self.content


class PreparingAgentCore:
    def __init__(
        self,
        workflow,
        *,
        malformed: bool = False,
        identity: str = "valid",
    ) -> None:
        self.workflow = workflow
        self.malformed = malformed
        self.identity = identity
        self.requests: list[dict[str, object]] = []

    def invoke_agent_runtime(self, **request: object) -> dict[str, object]:
        self.requests.append(request)
        payload = json.loads(request["payload"])
        self.workflow.prepare(payload["job_id"])
        if self.malformed:
            content = b'{"not":"the response contract"}'
        else:
            response = {
                "status": "success",
                "framework": AGENT_FRAMEWORK,
                "agent_id": PREPARATION_AGENT_ID,
                "decision": {
                    "summary": "The staged listing is ready for review.",
                    "recommendation": "Review the draft before approval.",
                    "next_action": "human_review",
                    "requires_human_approval": True,
                    "publication_authorized": False,
                },
            }
            if self.identity == "missing":
                response.pop("framework")
                response.pop("agent_id")
            elif self.identity == "wrong":
                response["framework"] = "not-strands"
                response["agent_id"] = "not-mr-lister"
            content = json.dumps(response).encode()
        return {"statusCode": 200, "response": AgentCoreBody(content)}


def _raise_client_error(code: str, message: str) -> None:
    raise ClientError({"Error": {"Code": code, "Message": message}}, "SyntheticOperation")


def test_command_boundary_sanitizes_terminal_aws_errors() -> None:
    with pytest.raises(TerminalWorkflowCommand, match="storage access was rejected") as rejected:
        _run_command(lambda: _raise_client_error("AccessDeniedException", "private detail"))

    assert rejected.value.__cause__ is None
    assert "private detail" not in str(rejected.value)


def test_command_boundary_classifies_transient_aws_errors_as_retryable() -> None:
    with pytest.raises(RetryableWorkflowCommand, match="temporarily unavailable") as retryable:
        _run_command(
            lambda: _raise_client_error("ProvisionedThroughputExceededException", "private detail")
        )

    assert retryable.value.__cause__ is None
    assert "private detail" not in str(retryable.value)


def test_handlers_drive_prepare_callback_publish_and_verify(workflow, now) -> None:
    intake = workflow.intake(
        filename="geometric_badger.png",
        content_type="image/png",
        content=SYNTHETIC_PNG,
        idempotency_key="phase4-handler-intake",
        profile_id="synthetic_gildan_5000",
    )
    callbacks = RecordingStepFunctions()
    services = Phase4Services(workflow=workflow, step_functions=callbacks)

    prepared = prepare_handler({"job_id": intake.job_id}, None, _services=services)
    registered = register_approval_wait_handler(
        {
            "job_id": intake.job_id,
            "review_version": prepared["review_version"],
            "task_token": "private-callback-token",
            "expires_in_seconds": 3600,
        },
        None,
        _services=services,
    )
    approved = approval_handler(
        {"job_id": intake.job_id, "review_version": prepared["review_version"]},
        None,
        _services=services,
    )
    published = fake_publish_handler({"job_id": intake.job_id}, None, _services=services)
    verified = fake_verify_handler({"job_id": intake.job_id}, None, _services=services)

    assert prepared["state"] == "awaiting_approval"
    assert registered == {
        "job_id": intake.job_id,
        "review_version": 1,
        "status": "pending",
    }
    assert approved["state"] == "approved"
    assert approved["callback_replayed"] is False
    assert published["state"] == "published"
    assert verified["state"] == "verified"
    assert workflow.get_job(intake.job_id).state is JobState.VERIFIED
    assert callbacks.successes[0]["taskToken"] == "private-callback-token"
    assert json.loads(callbacks.successes[0]["output"]) == {
        "job_id": intake.job_id,
        "review_version": 1,
        "state": "approved",
    }
    assert "private-callback-token" not in repr(
        [prepared, registered, approved, published, verified]
    )


def test_prepare_handler_can_use_the_opt_in_agentcore_bridge(workflow) -> None:
    intake = workflow.intake(
        filename="geometric_badger.png",
        content_type="image/png",
        content=SYNTHETIC_PNG,
        idempotency_key="phase4-agentcore-bridge",
        profile_id="synthetic_gildan_5000",
    )
    agentcore = PreparingAgentCore(workflow)
    services = Phase4Services(
        workflow=workflow,
        step_functions=RecordingStepFunctions(),
        agentcore=agentcore,
        agentcore_runtime_arn="arn:aws:bedrock-agentcore:us-west-2:123456789012:runtime/test",
    )

    response = prepare_handler({"job_id": intake.job_id}, None, _services=services)

    assert response["state"] == "awaiting_approval"
    request = agentcore.requests[0]
    assert request["agentRuntimeArn"] == services.agentcore_runtime_arn
    assert request["qualifier"] == "DEFAULT"
    assert len(request["runtimeSessionId"]) >= 33
    assert json.loads(request["payload"]) == {
        "job_id": intake.job_id,
        "mode": "prepare",
        "instruction": "Prepare and validate this staged job for human review.",
    }


def test_prepare_handler_rejects_malformed_agentcore_output_without_echo(workflow) -> None:
    intake = workflow.intake(
        filename="geometric_badger.png",
        content_type="image/png",
        content=SYNTHETIC_PNG,
        idempotency_key="phase4-agentcore-malformed",
        profile_id="synthetic_gildan_5000",
    )
    services = Phase4Services(
        workflow=workflow,
        step_functions=RecordingStepFunctions(),
        agentcore=PreparingAgentCore(workflow, malformed=True),
        agentcore_runtime_arn="arn:aws:bedrock-agentcore:us-west-2:123456789012:runtime/test",
    )

    with pytest.raises(TerminalWorkflowCommand, match="outside its contract") as rejected:
        prepare_handler({"job_id": intake.job_id}, None, _services=services)

    assert rejected.value.__cause__ is None
    assert "not" not in str(rejected.value)


@pytest.mark.parametrize("identity", ["missing", "wrong"])
def test_prepare_handler_rejects_unproven_agent_identity(workflow, identity: str) -> None:
    intake = workflow.intake(
        filename="geometric_badger.png",
        content_type="image/png",
        content=SYNTHETIC_PNG,
        idempotency_key=f"phase4-agentcore-identity-{identity}",
        profile_id="synthetic_gildan_5000",
    )
    services = Phase4Services(
        workflow=workflow,
        step_functions=RecordingStepFunctions(),
        agentcore=PreparingAgentCore(workflow, identity=identity),
        agentcore_runtime_arn="arn:aws:bedrock-agentcore:us-west-2:123456789012:runtime/test",
    )

    with pytest.raises(TerminalWorkflowCommand, match="outside its contract") as rejected:
        prepare_handler({"job_id": intake.job_id}, None, _services=services)

    assert rejected.value.__cause__ is None
    assert identity not in str(rejected.value)


def test_approval_callback_replay_is_harmless(workflow) -> None:
    job = submit(workflow, key="phase4-callback-replay")
    workflow.register_approval_wait(
        job.job_id,
        review_version=1,
        task_token="replayed-token",
        expires_at=workflow._clock() + timedelta(days=1),
    )
    first_services = Phase4Services(
        workflow=workflow,
        step_functions=RecordingStepFunctions(),
    )
    approval_handler(
        {"job_id": job.job_id, "review_version": 1},
        None,
        _services=first_services,
    )
    replay_services = Phase4Services(
        workflow=workflow,
        step_functions=RecordingStepFunctions(replay=True),
    )

    response = approval_handler(
        {"job_id": job.job_id, "review_version": 1},
        None,
        _services=replay_services,
    )

    assert response["state"] == "approved"
    assert response["callback_replayed"] is True


def test_first_delivery_of_dead_callback_is_terminal(workflow) -> None:
    job = submit(workflow, key="phase4-dead-callback")
    workflow.register_approval_wait(
        job.job_id,
        review_version=1,
        task_token="dead-token",
        expires_at=workflow._clock() + timedelta(days=1),
    )
    services = Phase4Services(
        workflow=workflow,
        step_functions=RecordingStepFunctions(replay=True),
    )

    with pytest.raises(TerminalWorkflowCommand, match="no longer active"):
        approval_handler(
            {"job_id": job.job_id, "review_version": 1},
            None,
            _services=services,
        )


def test_handler_rejects_extra_or_missing_command_fields(workflow) -> None:
    services = Phase4Services(workflow=workflow, step_functions=RecordingStepFunctions())

    with pytest.raises(TerminalWorkflowCommand, match="envelope is invalid") as invalid:
        prepare_handler(
            {"job_id": "job_valid", "task_token": "must-not-pass"},
            None,
            _services=services,
        )
    assert invalid.value.__cause__ is None

    with pytest.raises(TerminalWorkflowCommand, match="envelope is invalid"):
        prepare_handler({}, None, _services=services)
