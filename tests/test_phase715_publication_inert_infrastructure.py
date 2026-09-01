from __future__ import annotations

import json
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE_PATH = ROOT / "infra/phase7/production-disabled-template.json"
MACHINE_PATH = ROOT / "infra/phase7/statemachine/publication.asl.json"

CONDITION = "InstantiateProductionCandidate"
FUNCTION_NAMES = ("Query", "Request", "Dispatcher", "Worker", "Recovery", "Retention")


def _template() -> dict[str, Any]:
    return json.loads(TEMPLATE_PATH.read_text(encoding="utf-8"))


def _resources() -> dict[str, dict[str, Any]]:
    return _template()["Resources"]


def _machine() -> dict[str, Any]:
    return json.loads(MACHINE_PATH.read_text(encoding="utf-8"))


def _role_actions(role_name: str) -> set[str]:
    policies = _resources()[role_name]["Properties"]["Policies"]
    assert len(policies) == 1
    actions: set[str] = set()
    for statement in policies[0]["PolicyDocument"]["Statement"]:
        value = statement["Action"]
        actions.update([value] if isinstance(value, str) else value)
    return actions


def test_activation_parameter_makes_every_resource_impossible_to_instantiate() -> None:
    template = _template()
    activation = template["Parameters"]["ActivationMode"]

    assert activation == {
        "Type": "String",
        "Default": "SOURCE_ONLY_DISABLED",
        "AllowedValues": ["SOURCE_ONLY_DISABLED"],
    }
    assert template["Conditions"][CONDITION] == {
        "Fn::Equals": [
            {"Ref": "ActivationMode"},
            "GENERAL_AVAILABILITY_ENABLED",
        ]
    }
    assert all(resource.get("Condition") == CONDITION for resource in _resources().values())
    assert _template()["Outputs"] == {
        "DeploymentReadiness": {"Value": "SOURCE_ONLY_DISABLED"},
        "ResourceInstantiationPossible": {"Value": "false"},
        "PublicationQueryRegistered": {"Value": "false"},
        "PublicationRequestRegistered": {"Value": "false"},
        "PublicationWorkerTriggered": {"Value": "false"},
        "SellerPublicationEnabled": {"Value": "false"},
        "ProviderMutationEnabled": {"Value": "false"},
    }


def test_topology_is_separate_role_bounded_and_contains_no_seller_route() -> None:
    resources = _resources()
    types = {name: resource["Type"] for name, resource in resources.items()}

    for name in FUNCTION_NAMES:
        assert types[f"Publication{name}Function"] == "AWS::Serverless::Function"
        assert types[f"Publication{name}Role"] == "AWS::IAM::Role"
        assert types[f"Publication{name}LogGroup"] == "AWS::Logs::LogGroup"
    assert types["PublicationWorkflowStateMachine"] == "AWS::Serverless::StateMachine"
    assert types["PublicationWorkflowRole"] == "AWS::IAM::Role"
    assert types["PublicationWorkflowLogGroup"] == "AWS::Logs::LogGroup"
    assert types["PublicationWorkflowRecoveryQueue"] == "AWS::SQS::Queue"
    assert types["PublicationOperationsDeadLetterQueue"] == "AWS::SQS::Queue"

    forbidden_types = {
        "AWS::ApiGateway::RestApi",
        "AWS::ApiGatewayV2::Api",
        "AWS::CloudFront::Distribution",
        "AWS::Lambda::Url",
        "AWS::Serverless::Api",
        "AWS::Serverless::HttpApi",
    }
    assert not set(types.values()) & forbidden_types
    serialized = json.dumps(_template(), sort_keys=True)
    assert "/v1/" not in serialized
    assert "publish_exact_approved_listing" not in serialized
    assert "FunctionUrlConfig" not in serialized
    assert "Api" not in _template().get("Globals", {})


def test_candidate_functions_are_version_bound_exact_disabled_and_unimplemented() -> None:
    template = _template()
    variables = template["Globals"]["Function"]["Environment"]["Variables"]
    assert variables == {
        "MR_LISTER_PHASE7_SCAFFOLD_ONLY": "true",
        "MR_LISTER_PHASE7_QUERY_ENABLED": "false",
        "MR_LISTER_PHASE7_REQUEST_ENABLED": "false",
        "MR_LISTER_PHASE7_PUBLICATION_ENABLED": "false",
        "MR_LISTER_PHASE7_PRODUCTION_CANDIDATE_ENABLED": "false",
    }
    expected_code = {
        "Bucket": {"Ref": "CandidateCodeS3Bucket"},
        "Key": {"Ref": "CandidateCodeS3Key"},
        "Version": {"Ref": "CandidateCodeS3ObjectVersion"},
    }
    for name in FUNCTION_NAMES:
        properties = _resources()[f"Publication{name}Function"]["Properties"]
        assert properties["CodeUri"] == expected_code
        assert properties["Role"] == {"Fn::GetAtt": [f"Publication{name}Role", "Arn"]}
        assert properties["Handler"].startswith("mr_lister.cloud.phase7_production_entrypoints.")
        assert "FunctionUrlConfig" not in properties
    assert "Events" not in _resources()["PublicationQueryFunction"]["Properties"]
    assert "Events" not in _resources()["PublicationRequestFunction"]["Properties"]
    assert not (ROOT / "src/mr_lister/cloud/phase7_production_entrypoints.py").exists()


def test_every_trigger_has_an_independent_exact_disabled_barrier() -> None:
    resources = _resources()
    stream_mappings = {
        name: resource
        for name, resource in resources.items()
        if resource["Type"] == "AWS::Lambda::EventSourceMapping"
    }
    assert set(stream_mappings) == {
        "PublicationDispatcherStreamMapping",
        "PublicationRetentionStreamMapping",
    }
    assert all(mapping["Properties"]["Enabled"] is False for mapping in stream_mappings.values())
    assert stream_mappings["PublicationDispatcherStreamMapping"]["Properties"]["BatchSize"] == 25
    dispatcher_filter = stream_mappings["PublicationDispatcherStreamMapping"]["Properties"][
        "FilterCriteria"
    ]["Filters"][0]["Pattern"]
    retention_filter = stream_mappings["PublicationRetentionStreamMapping"]["Properties"][
        "FilterCriteria"
    ]["Filters"][0]["Pattern"]
    assert "PUBLICATION_WORK#" in dispatcher_filter
    assert '"WORK#' not in dispatcher_filter.replace('"PUBLICATION_WORK#', "")
    assert "TERMINAL_JOB_LINK" in retention_filter
    assert "PUBLICATION_TERMINAL_JOB_LINK" in retention_filter

    rules = {
        name: resource
        for name, resource in resources.items()
        if resource["Type"] == "AWS::Events::Rule"
    }
    assert set(rules) == {"PublicationDueWorkSweepRule", "PublicationWorkflowFailureRule"}
    assert all(rule["Properties"]["State"] == "DISABLED" for rule in rules.values())

    recovery_event = resources["PublicationRecoveryFunction"]["Properties"]["Events"][
        "RecoveryQueue"
    ]["Properties"]
    assert recovery_event["Enabled"] is False
    assert recovery_event["BatchSize"] == 1


def test_workflow_is_bounded_payload_closed_and_invokes_only_one_worker() -> None:
    machine = _machine()
    states = machine["States"]
    tasks = [state for state in states.values() if state["Type"] == "Task"]
    waits = [state for state in states.values() if state["Type"] == "Wait"]

    assert machine["TimeoutSeconds"] == 1860
    assert len(tasks) == 1
    task = tasks[0]
    assert task["Resource"] == "arn:aws:states:::lambda:invoke"
    assert task["Parameters"] == {
        "FunctionName": "${PublicationWorkerFunctionArn}",
        "Payload": {"owner_id.$": "$.owner_id", "aggregate_id.$": "$.aggregate_id"},
    }
    assert task["ResultSelector"] == {"aggregate_state.$": "$.Payload.aggregate_state"}
    assert task["Retry"] == [
        {
            "ErrorEquals": [
                "Lambda.ServiceException",
                "Lambda.AWSLambdaException",
                "Lambda.SdkClientException",
                "Lambda.TooManyRequestsException",
            ],
            "IntervalSeconds": 2,
            "MaxAttempts": 2,
            "BackoffRate": 2,
        }
    ]
    assert waits == [
        {"Type": "Wait", "Seconds": 1, "Next": "RunOnePublicationStep"},
        {"Type": "Wait", "Seconds": 20, "Next": "RunOnePublicationStep"},
    ]
    assert states["CheckFastPollBudget"]["Choices"] == [
        {
            "Variable": "$.poll_count",
            "NumericLessThan": 90,
            "Next": "IncrementFastPollCount",
        }
    ]
    assert states["CheckSlowPollBudget"]["Choices"] == [
        {
            "Variable": "$.poll_count",
            "NumericLessThan": 90,
            "Next": "IncrementSlowPollCount",
        }
    ]
    assert {name for name, state in states.items() if state["Type"] == "Succeed"} == {
        "Published",
        "PublicationFailed",
        "PublicationOutcomeUnknown",
    }
    serialized = json.dumps(machine, sort_keys=True).casefold()
    for forbidden in ("waitfortasktoken", "states:start", '"type": "map"', '"type": "parallel"'):
        assert forbidden not in serialized

    workflow = _resources()["PublicationWorkflowStateMachine"]["Properties"]
    assert workflow["Type"] == "STANDARD"
    assert workflow["DefinitionUri"] == "statemachine/publication.asl.json"
    assert workflow["Logging"]["IncludeExecutionData"] is False
    assert workflow["Logging"]["Level"] == "ERROR"


def test_roles_keep_provider_mutation_and_control_plane_authority_separate() -> None:
    query = _role_actions("PublicationQueryRole")
    request = _role_actions("PublicationRequestRole")
    dispatcher = _role_actions("PublicationDispatcherRole")
    worker = _role_actions("PublicationWorkerRole")
    recovery = _role_actions("PublicationRecoveryRole")
    retention = _role_actions("PublicationRetentionRole")
    workflow = _role_actions("PublicationWorkflowRole")

    assert query == {
        "dynamodb:GetItem",
        "dynamodb:Query",
        "logs:CreateLogStream",
        "logs:PutLogEvents",
    }
    assert request == {
        "dynamodb:ConditionCheckItem",
        "dynamodb:GetItem",
        "dynamodb:PutItem",
        "logs:CreateLogStream",
        "logs:PutLogEvents",
    }
    assert "states:StartExecution" in dispatcher
    assert "states:DescribeExecution" in dispatcher
    assert "sqs:SendMessage" in dispatcher
    assert "secretsmanager:GetSecretValue" not in dispatcher
    dispatcher_policy = _resources()["PublicationDispatcherRole"]["Properties"]["Policies"][0]
    due_query = next(
        statement
        for statement in dispatcher_policy["PolicyDocument"]["Statement"]
        if statement["Sid"] == "QueryOnlyDuePublicationWork"
    )
    assert due_query["Resource"] == {"Fn::Sub": "${StateTableArn}/index/DueWorkIndex"}
    assert due_query["Condition"] == {
        "ForAllValues:StringLike": {"dynamodb:LeadingKeys": ["PUBLICATION_WORK_DUE#0"]}
    }
    assert "secretsmanager:GetSecretValue" in worker
    assert not worker & {
        "states:StartExecution",
        "states:RedriveExecution",
        "lambda:InvokeFunction",
    }
    assert "states:RedriveExecution" in recovery
    assert "states:StartExecution" not in recovery
    assert "secretsmanager:GetSecretValue" not in recovery
    assert "dynamodb:UpdateItem" in retention
    assert "sqs:SendMessage" in retention
    assert not retention & {"dynamodb:DeleteItem", "secretsmanager:GetSecretValue"}
    assert "lambda:InvokeFunction" in workflow
    assert not workflow & {
        "dynamodb:GetItem",
        "secretsmanager:GetSecretValue",
        "states:StartExecution",
    }

    serialized = json.dumps(
        {
            name: resource
            for name, resource in _resources().items()
            if resource["Type"] == "AWS::IAM::Role"
        },
        sort_keys=True,
    ).casefold()
    for forbidden in (
        "execute-api:",
        "bedrock",
        "agentcore",
        "s3:",
        "dynamodb:scan",
        "dynamodb:deleteitem",
        "orders",
        "fulfillment",
        "unpublish",
    ):
        assert forbidden not in serialized


def test_recovery_is_same_execution_only_and_failure_event_is_sanitized() -> None:
    resources = _resources()
    queue = resources["PublicationWorkflowRecoveryQueue"]["Properties"]
    assert queue["RedrivePolicy"] == {
        "deadLetterTargetArn": {"Fn::GetAtt": ["PublicationOperationsDeadLetterQueue", "Arn"]},
        "maxReceiveCount": 3,
    }
    assert queue["SqsManagedSseEnabled"] is True
    assert (
        resources["PublicationOperationsDeadLetterQueue"]["Properties"]["SqsManagedSseEnabled"]
        is True
    )

    target = resources["PublicationWorkflowFailureRule"]["Properties"]["Targets"][0]
    transformer = target["InputTransformer"]
    assert transformer["InputPathsMap"] == {
        "execution_arn": "$.detail.executionArn",
        "machine_arn": "$.detail.stateMachineArn",
        "status": "$.detail.status",
    }
    assert set(
        json.loads(
            transformer["InputTemplate"]
            .replace("<execution_arn>", '"x"')
            .replace("<machine_arn>", '"y"')
            .replace("<status>", '"FAILED"')
        )
    ) == {
        "execution_arn",
        "machine_arn",
        "status",
    }
    serialized = json.dumps(target, sort_keys=True).casefold()
    assert "input.$" not in serialized
    assert "output.$" not in serialized


def test_alarm_matrix_is_encrypted_closed_and_payload_free() -> None:
    resources = _resources()
    topic = resources["PublicationAlarmTopic"]["Properties"]
    assert topic["KmsMasterKeyId"] == "alias/aws/sns"
    alarms = {
        name: resource["Properties"]
        for name, resource in resources.items()
        if resource["Type"] == "AWS::CloudWatch::Alarm"
    }
    assert set(alarms) == {
        "PublicationFunctionsErrorsAlarm",
        "PublicationWorkerThrottlesAlarm",
        "PublicationWorkerDurationAlarm",
        "PublicationWorkflowFailuresAlarm",
        "PublicationDispatcherIteratorAgeAlarm",
        "PublicationRecoveryQueueDepthAlarm",
        "PublicationRecoveryQueueAgeAlarm",
        "PublicationOperationsDeadLetterQueueDepthAlarm",
        "PublicationOutcomeUnknownAlarm",
    }
    for alarm in alarms.values():
        assert alarm["ActionsEnabled"] is True
        assert alarm["AlarmActions"] == [{"Ref": "PublicationAlarmTopic"}]
        assert alarm["TreatMissingData"] == "notBreaching"
        assert alarm["EvaluationPeriods"] == 1
        assert alarm["DatapointsToAlarm"] == 1

    metric_filter = resources["PublicationOutcomeUnknownMetric"]["Properties"]
    assert metric_filter["FilterPattern"] == (
        '{ $.publication_state = "publication_outcome_unknown" }'
    )
    assert metric_filter["MetricTransformations"] == [
        {
            "MetricNamespace": "MrLister/Phase7",
            "MetricName": "PublicationOutcomeUnknown",
            "MetricValue": "1",
            "DefaultValue": 0,
        }
    ]
    template_text = TEMPLATE_PATH.read_text(encoding="utf-8").casefold()
    for forbidden in ("owner_id.$", "job_id.$", "product_id.$", "listing_text", "artwork"):
        assert forbidden not in template_text
