"""Double-gated deployed canary that stops at human review after a real Printify draft."""

from __future__ import annotations

import json
import os
import time
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

CANARY_ASSET = Path("tests/evaluation/assets/holdout_owl_lantern.png")


def _stack(client: Any, stack_name: str) -> tuple[dict[str, str], dict[str, str]]:
    stack = client.describe_stacks(StackName=stack_name)["Stacks"][0]
    outputs = {item["OutputKey"]: item["OutputValue"] for item in stack["Outputs"]}
    parameters = {item["ParameterKey"]: item["ParameterValue"] for item in stack["Parameters"]}
    return outputs, parameters


def _workflow(session: boto3.Session, outputs: dict[str, str]) -> ListingWorkflow:
    return ListingWorkflow(
        store=DynamoDBJobStore(
            client=session.client("dynamodb"), table_name=outputs["OperationalStateTableName"]
        ),
        artifacts=S3ArtifactStore(
            client=session.client("s3"), bucket=outputs["PrivateArtifactBucketName"]
        ),
        profiles=ProductProfileRepository(Path("config/product_profiles")),
        intelligence=FakeIntelligenceAdapter(),
        production=FakeProductionAdapter(),
    )


def main() -> None:
    if os.environ.get("MR_LISTER_RUN_PHASE5_PRINTIFY_CANARY") != "1":
        raise SystemExit("Set MR_LISTER_RUN_PHASE5_PRINTIFY_CANARY=1 to permit AWS writes")
    if os.environ.get("MR_LISTER_CONFIRM_UNPUBLISHED_PRODUCT_WRITE") != "YES":
        raise SystemExit("Set MR_LISTER_CONFIRM_UNPUBLISHED_PRODUCT_WRITE=YES to create one draft")

    expected_secret_arn = os.environ.get("MR_LISTER_PRINTIFY_SECRET_ARN", "").strip()
    if not expected_secret_arn:
        raise SystemExit("MR_LISTER_PRINTIFY_SECRET_ARN must name the expected deployed secret")
    profile_name = os.environ.get("AWS_PROFILE", "mr-lister-dev")
    region = os.environ.get("AWS_DEFAULT_REGION", "us-west-2")
    if region != "us-west-2":
        raise RuntimeError("The Phase 5 canary is pinned to us-west-2")
    session = boto3.Session(profile_name=profile_name, region_name=region)
    caller = session.client("sts").get_caller_identity()
    if str(caller.get("Arn", "")).endswith(":root"):
        raise RuntimeError("The Phase 5 canary must never run with root credentials")

    stack_name = os.environ.get("MR_LISTER_PHASE4_STACK", "mr-lister-phase4-dev")
    outputs, parameters = _stack(session.client("cloudformation"), stack_name)
    if parameters.get("PrintifySecretArn") != expected_secret_arn:
        raise RuntimeError("The deployed Printify secret ARN does not match the expected ARN")

    workflow = _workflow(session, outputs)
    run_id = uuid4().hex
    content = CANARY_ASSET.read_bytes()
    job = workflow.intake(
        filename=CANARY_ASSET.name,
        content_type="image/png",
        content=content,
        idempotency_key=f"phase5-printify-canary-{run_id}",
        profile_id="gildan_64000_swiftpod",
    )
    execution = session.client("stepfunctions").start_execution(
        stateMachineArn=outputs["DurableWorkflowArn"],
        name=f"phase5-printify-{run_id}",
        input=json.dumps({"job_id": job.job_id}, separators=(",", ":")),
    )

    deadline = time.monotonic() + 180
    while time.monotonic() < deadline:
        current = _workflow(session, outputs).get_job(job.job_id)
        if current.state is JobState.AWAITING_APPROVAL:
            report = _workflow(session, outputs).get_report(job.job_id)
            operations = [write.operation for write in report.external_writes]
            if len(operations) != 2 or set(operations) != {
                "upload_artwork",
                "create_product_draft",
            }:
                raise RuntimeError("The canary did not persist both production checkpoints")
            print(
                json.dumps(
                    {
                        "account": caller["Account"],
                        "region": region,
                        "job_id": current.job_id,
                        "state": current.state.value,
                        "printify_image_id": current.printify_image_id,
                        "printify_product_id": current.printify_product_id,
                        "execution_arn": execution["executionArn"],
                        "human_approval_required": True,
                    },
                    sort_keys=True,
                )
            )
            return
        status = session.client("stepfunctions").describe_execution(
            executionArn=execution["executionArn"]
        )["status"]
        if status in {"FAILED", "TIMED_OUT", "ABORTED"}:
            raise RuntimeError(f"The Phase 5 canary execution ended with status {status}")
        time.sleep(2)
    raise TimeoutError("The Phase 5 canary did not reach human review")


if __name__ == "__main__":
    main()
