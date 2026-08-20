from __future__ import annotations

import json
from pathlib import Path

STATE_MACHINE_PATH = Path("infra/phase4/statemachine/durable-workflow.asl.json")
TEMPLATE_PATH = Path("infra/phase4/durable-workflow.json")


def test_standard_workflow_has_separate_prepare_wait_publish_and_verify_tasks() -> None:
    definition = json.loads(STATE_MACHINE_PATH.read_text(encoding="utf-8"))
    states = definition["States"]

    assert definition["StartAt"] == "PrepareJob"
    assert states["PrepareJob"]["Resource"] == "arn:aws:states:::lambda:invoke"
    assert states["WaitForApproval"]["Resource"] == (
        "arn:aws:states:::lambda:invoke.waitForTaskToken"
    )
    assert states["WaitForApproval"]["Parameters"]["Payload"]["task_token.$"] == ("$$.Task.Token")
    assert states["FakePublish"]["Next"] == "FakeVerify"
    assert states["FakeVerify"]["Next"] == "WorkflowSucceeded"
    assert states["PreparationOutcome"]["Choices"][1] == {
        "Variable": "$.state",
        "StringEquals": "needs_revision",
        "Next": "NeedsRevision",
    }
    restart_routes = {
        choice["StringEquals"]: choice["Next"] for choice in states["PreparationOutcome"]["Choices"]
    }
    assert restart_routes == {
        "awaiting_approval": "WaitForApproval",
        "needs_revision": "NeedsRevision",
        "approved": "FakePublish",
        "publishing": "FakePublish",
        "published": "FakeVerify",
        "verified": "WorkflowSucceeded",
    }


def test_state_machine_payloads_contain_only_identifiers_and_callback_token() -> None:
    definition = json.loads(STATE_MACHINE_PATH.read_text(encoding="utf-8"))
    states = definition["States"]

    assert states["PrepareJob"]["Parameters"]["Payload"] == {"job_id.$": "$.job_id"}
    assert states["FakePublish"]["Parameters"]["Payload"] == {"job_id.$": "$.job_id"}
    assert states["FakeVerify"]["Parameters"]["Payload"] == {"job_id.$": "$.job_id"}
    serialized = STATE_MACHINE_PATH.read_text(encoding="utf-8").casefold()
    assert "artwork" not in serialized
    assert "prompt" not in serialized
    assert "credential" not in serialized
    assert "listing" not in serialized


def test_sam_template_scopes_callback_permission_and_disables_payload_logs() -> None:
    template = json.loads(TEMPLATE_PATH.read_text(encoding="utf-8"))
    resources = template["Resources"]

    assert template["Transform"] == "AWS::Serverless-2016-10-31"
    assert template["Parameters"]["AgentCoreRuntimeArn"]["Default"] == ""
    assert template["Conditions"]["UseAgentCoreRuntime"] == {
        "Fn::Not": [{"Fn::Equals": [{"Ref": "AgentCoreRuntimeArn"}, ""]}]
    }
    assert resources["DurableWorkflow"]["Properties"]["Type"] == "STANDARD"
    assert resources["DurableWorkflow"]["Properties"]["Logging"] == {
        "Destinations": [
            {
                "CloudWatchLogsLogGroup": {
                    "LogGroupArn": {
                        "Fn::Sub": (
                            "arn:${AWS::Partition}:logs:${AWS::Region}:${AWS::AccountId}:"
                            "log-group:/aws/vendedlogs/states/mr-lister-${EnvironmentName}:*"
                        )
                    }
                }
            }
        ],
        "IncludeExecutionData": False,
        "Level": "ERROR",
    }
    function_ids = {
        name
        for name, resource in resources.items()
        if resource["Type"] == "AWS::Serverless::Function"
    }
    assert function_ids == {
        "PrepareFunction",
        "RegisterApprovalWaitFunction",
        "ApprovalFunction",
        "FakePublishFunction",
        "FakeVerifyFunction",
    }
    assert all(
        resources[function_id]["Properties"]["CodeUri"] == "lambda/" for function_id in function_ids
    )
    callback_permission_holders = []
    for function_id in function_ids:
        role_id = resources[function_id]["Properties"]["Role"]["Fn::GetAtt"][0]
        role = resources[role_id]
        assert role["Type"] == "AWS::IAM::Role"
        assert "ManagedPolicyArns" not in role["Properties"]
        policies = role["Properties"]["Policies"]
        assert "AWSLambdaBasicExecutionRole" not in repr(policies)
        assert "logs:CreateLogStream" in repr(policies)
        assert "logs:PutLogEvents" in repr(policies)
        assert (
            "arn:${AWS::Partition}:logs:${AWS::Region}:${AWS::AccountId}:log-group:/aws/lambda/"
            in repr(policies)
        )
        if "states:SendTaskSuccess" in repr(policies):
            callback_permission_holders.append(function_id)
    assert callback_permission_holders == ["ApprovalFunction"]
    prepare_properties = resources["PrepareFunction"]["Properties"]
    assert prepare_properties["Environment"]["Variables"]["MR_LISTER_AGENTCORE_RUNTIME_ARN"] == {
        "Ref": "AgentCoreRuntimeArn"
    }
    assert "bedrock-agentcore:InvokeAgentRuntime" in repr(
        resources["PrepareFunctionRole"]["Properties"]["Policies"]
    )

    lambda_log_groups = {
        name: resource for name, resource in resources.items() if name.endswith("FunctionLogGroup")
    }
    assert len(lambda_log_groups) == 5
    assert all(
        resource["Properties"]["RetentionInDays"] == 14 for resource in lambda_log_groups.values()
    )
