"""Explicitly gated end-to-end canary for the deployed Phase 4 fake workflow."""

from __future__ import annotations

import json
import os
import time
from base64 import b64decode
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import boto3

from mr_lister.contracts import JobState
from mr_lister.workflow.artifacts import S3ArtifactStore
from mr_lister.workflow.dynamodb import DynamoDBJobStore
from mr_lister.workflow.fakes import FakeIntelligenceAdapter, FakeProductionAdapter
from mr_lister.workflow.profiles import ProductProfileRepository
from mr_lister.workflow.service import ListingWorkflow

SYNTHETIC_PNG = b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAIAAAACCAYAAABytg0kAAAAFUlEQVR4nGNU07X8z8DAwMAEIkAYABbVAY+Z/lCyAAAAAElFTkSuQmCC"
)


def _stack_outputs(client: Any, stack_name: str) -> dict[str, str]:
    stack = client.describe_stacks(StackName=stack_name)["Stacks"][0]
    return {output["OutputKey"]: output["OutputValue"] for output in stack["Outputs"]}


def _workflow(session: boto3.Session, outputs: dict[str, str]) -> ListingWorkflow:
    return ListingWorkflow(
        store=DynamoDBJobStore(
            client=session.client("dynamodb"),
            table_name=outputs["OperationalStateTableName"],
        ),
        artifacts=S3ArtifactStore(
            client=session.client("s3"),
            bucket=outputs["PrivateArtifactBucketName"],
        ),
        profiles=ProductProfileRepository(Path("config/product_profiles")),
        intelligence=FakeIntelligenceAdapter(),
        production=FakeProductionAdapter(),
    )


def _wait_until(predicate: Any, *, timeout_seconds: int = 180) -> Any:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        result = predicate()
        if result:
            return result
        time.sleep(2)
    raise TimeoutError("The Phase 4 AWS canary did not reach its expected checkpoint")


def main() -> None:
    if os.environ.get("MR_LISTER_RUN_PHASE4_AWS_CANARY") != "1":
        raise SystemExit("Set MR_LISTER_RUN_PHASE4_AWS_CANARY=1 to permit AWS writes")

    profile = os.environ.get("AWS_PROFILE", "mr-lister-dev")
    region = os.environ.get("AWS_DEFAULT_REGION", "us-west-2")
    stack_name = os.environ.get("MR_LISTER_PHASE4_STACK", "mr-lister-phase4-dev")
    session = boto3.Session(profile_name=profile, region_name=region)
    caller = session.client("sts").get_caller_identity()
    if caller["Arn"].endswith(":root"):
        raise RuntimeError("The Phase 4 canary must never run with root credentials")

    outputs = _stack_outputs(session.client("cloudformation"), stack_name)
    workflow = _workflow(session, outputs)
    run_id = uuid4().hex
    job = workflow.intake(
        filename="phase4_synthetic_badger.png",
        content_type="image/png",
        content=SYNTHETIC_PNG,
        idempotency_key=f"phase4-aws-canary-{run_id}",
        profile_id="synthetic_gildan_5000",
    )

    step_functions = session.client("stepfunctions")
    execution = step_functions.start_execution(
        stateMachineArn=outputs["DurableWorkflowArn"],
        name=f"phase4-canary-{run_id}",
        input=json.dumps({"job_id": job.job_id}, separators=(",", ":")),
    )
    print(
        json.dumps(
            {
                "checkpoint": "execution_started",
                "job_id": job.job_id,
                "execution_arn": execution["executionArn"],
            },
            sort_keys=True,
        ),
        flush=True,
    )
    dynamodb = session.client("dynamodb")

    def approval_wait_is_registered() -> bool:
        execution_status = step_functions.describe_execution(
            executionArn=execution["executionArn"]
        )["status"]
        if execution_status in {"FAILED", "TIMED_OUT", "ABORTED"}:
            raise RuntimeError(
                "The Phase 4 execution ended before registering the approval wait "
                f"with status {execution_status}"
            )
        item = dynamodb.get_item(
            TableName=outputs["OperationalStateTableName"],
            Key={"PK": {"S": f"JOB#{job.job_id}"}, "SK": {"S": "APPROVAL_WAIT"}},
            ProjectionExpression="wait_status, review_version",
            ConsistentRead=True,
        ).get("Item")
        return bool(item and item.get("wait_status", {}).get("S") == "pending")

    _wait_until(approval_wait_is_registered)
    current = _workflow(session, outputs).get_job(job.job_id)
    response = session.client("lambda").invoke(
        FunctionName=outputs["ApprovalFunctionArn"],
        InvocationType="RequestResponse",
        Payload=json.dumps(
            {"job_id": job.job_id, "review_version": current.review_version}
        ).encode(),
    )
    if response.get("FunctionError"):
        raise RuntimeError("The approval Lambda rejected the synthetic canary")

    def execution_succeeded() -> bool:
        status = step_functions.describe_execution(executionArn=execution["executionArn"])["status"]
        if status in {"FAILED", "TIMED_OUT", "ABORTED"}:
            raise RuntimeError(f"The Phase 4 execution ended with status {status}")
        return status == "SUCCEEDED"

    _wait_until(execution_succeeded)
    reconstructed = _workflow(session, outputs)
    final_job = reconstructed.get_job(job.job_id)
    if final_job.state is not JobState.VERIFIED:
        raise RuntimeError("The reconstructed job did not reach VERIFIED")
    report = reconstructed.get_report(job.job_id)
    completed_writes = [write for write in report.external_writes if write.external_id]
    if len(completed_writes) != 2:
        raise RuntimeError("The canary did not preserve exactly one fake draft and publication")

    print(
        json.dumps(
            {
                "account": caller["Account"],
                "region": region,
                "stack": stack_name,
                "job_id": final_job.job_id,
                "state": final_job.state.value,
                "record_version": final_job.record_version,
                "event_sequence": final_job.event_sequence,
                "completed_external_writes": len(completed_writes),
                "completed_at": datetime.now(UTC).isoformat(),
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
