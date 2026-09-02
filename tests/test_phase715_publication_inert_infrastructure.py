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


def test_activation_parameter_allows_only_an_explicit_production_disabled_deployment() -> None:
    template = _template()
    activation = template["Parameters"]["ActivationMode"]

    assert activation == {
        "Type": "String",
        "Default": "SOURCE_ONLY_DISABLED",
        "AllowedValues": ["SOURCE_ONLY_DISABLED", "PRODUCTION_DISABLED"],
    }
    assert template["Conditions"][CONDITION] == {
        "Fn::Equals": [
            {"Ref": "ActivationMode"},
            "PRODUCTION_DISABLED",
        ]
    }
    assert all(resource.get("Condition") == CONDITION for resource in _resources().values())
    assert _template()["Outputs"] == {
        "DeploymentReadiness": {
            "Value": {
                "Fn::If": [
                    "InstantiateProductionCandidate",
                    "PRODUCTION_DISABLED",
                    "SOURCE_ONLY_DISABLED",
                ]
            }
        },
        "ResourceInstantiationPossible": {
            "Value": {"Fn::If": ["InstantiateProductionCandidate", "true", "false"]}
        },
        "PublicationQueryRegistered": {"Value": "false"},
        "PublicationRequestRegistered": {"Value": "false"},
        "PublicationWorkerTriggered": {"Value": "false"},
        "SellerPublicationEnabled": {"Value": "false"},
        "ProviderMutationEnabled": {"Value": "false"},
    }
    assert "GENERAL_AVAILABILITY_ENABLED" not in TEMPLATE_PATH.read_text(encoding="utf-8")


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
    assert types["PublicationAlarmKey"] == "AWS::KMS::Key"
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
    for capability_environment_name in (
        "MR_LISTER_PRINTIFY_SECRET_ARN",
        "MR_LISTER_PUBLICATION_RECOVERY_QUEUE_URL",
        "MR_LISTER_PUBLICATION_WORKFLOW_ARN",
    ):
        assert capability_environment_name not in serialized


def test_candidate_functions_are_fingerprint_bound_and_exact_disabled() -> None:
    template = _template()
    variables = template["Globals"]["Function"]["Environment"]["Variables"]
    assert template["Globals"]["Function"]["ReservedConcurrentExecutions"] == 0
    assert variables == {
        "MR_LISTER_PHASE7_SCAFFOLD_ONLY": "true",
        "MR_LISTER_PHASE7_QUERY_ENABLED": "false",
        "MR_LISTER_PHASE7_REQUEST_ENABLED": "false",
        "MR_LISTER_PHASE7_PUBLICATION_ENABLED": "false",
        "MR_LISTER_PHASE7_PRODUCTION_CANDIDATE_ENABLED": "false",
        "MR_LISTER_PHASE7_PRODUCTION_RELEASE_FINGERPRINT": {"Ref": "CandidateReleaseFingerprint"},
        "MR_LISTER_RELEASE_FINGERPRINT": {"Ref": "CandidateReleaseFingerprint"},
        "MR_LISTER_PHASE7_CONTRACT_FINGERPRINT": (
            "548b710230618e73c20a509f2121799c415b50070e1e2ae7e1b82fe3c37e2981"
        ),
        "MR_LISTER_PHASE7_CONTRACT_VERSION": "7.0.1",
        "MR_LISTER_PHASE7_ACTIVATION_MODE": "SOURCE_ONLY_DISABLED",
        "MR_LISTER_STATE_TABLE": {"Fn::Sub": "mr-lister-phase6-${EnvironmentName}"},
        "MR_LISTER_COGNITO_ISSUER": {
            "Fn::Sub": ("https://cognito-idp.${AWS::Region}.${AWS::URLSuffix}/${SellerUserPoolId}")
        },
        "MR_LISTER_COGNITO_CLIENT_ID": {"Ref": "SellerUserPoolClientId"},
        "MR_LISTER_COGNITO_SCOPE": "mr-lister-api/seller",
        "MR_LISTER_COGNITO_GROUP": "seller",
        "MR_LISTER_PRODUCT_PROFILE_ID": "gildan_64000_swiftpod",
        "MR_LISTER_PRODUCT_PROFILE_VERSION": "2",
        "MR_LISTER_PRODUCT_PROFILE_FINGERPRINT": (
            "5de1257141cfdacb1731f68bb9113712957483b33d5b0f7115afdba86eb7476c"
        ),
        "MR_LISTER_PRODUCT_PROFILE_PATH": (
            "/var/task/config/product_profiles/gildan_64000_swiftpod.json"
        ),
    }
    assert "CandidateCodeS3Key" not in template["Parameters"]
    assert template["Parameters"]["CandidateReleaseFingerprint"] == {
        "Type": "String",
        "AllowedPattern": "^(?!0{64}$)[a-f0-9]{64}$",
    }
    expected_code = {
        "Bucket": {"Ref": "CandidateCodeS3Bucket"},
        "Key": {
            "Fn::Sub": ("phase7/candidates/${CandidateReleaseFingerprint}/production-disabled.zip")
        },
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
    assert (ROOT / "src/mr_lister/cloud/phase7_production_entrypoints.py").is_file()


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
    assert json.loads(retention_filter) == {
        "eventName": ["INSERT"],
        "dynamodb": {
            "Keys": {
                "PK": {"S": [{"prefix": "PUBLICATION#"}]},
                "SK": {"S": ["TERMINAL_JOB_LINK"]},
            },
            "StreamViewType": ["KEYS_ONLY"],
        },
    }
    assert "NewImage" not in retention_filter

    rules = {
        name: resource
        for name, resource in resources.items()
        if resource["Type"] == "AWS::Events::Rule"
    }
    assert set(rules) == {
        "PublicationDueWorkSweepRule",
        "PublicationRecoverySweepRule",
        "PublicationWorkflowFailureRule",
    }
    assert all(rule["Properties"]["State"] == "DISABLED" for rule in rules.values())

    expected_delivery = {
        "RetryPolicy": {"MaximumEventAgeInSeconds": 3600, "MaximumRetryAttempts": 2},
        "DeadLetterConfig": {
            "Arn": {"Fn::GetAtt": ["PublicationOperationsDeadLetterQueue", "Arn"]}
        },
    }
    for rule in rules.values():
        target = rule["Properties"]["Targets"][0]
        assert {key: target[key] for key in expected_delivery} == expected_delivery

    assert rules["PublicationRecoverySweepRule"]["Properties"]["ScheduleExpression"] == (
        "rate(1 minute)"
    )
    assert rules["PublicationRecoverySweepRule"]["Properties"]["Targets"][0]["Input"] == (
        '{"kind":"publication_recovery_sweep"}'
    )

    permissions = {
        name: resource["Properties"]
        for name, resource in resources.items()
        if resource["Type"] == "AWS::Lambda::Permission"
    }
    assert permissions == {
        "PublicationDueWorkSweepPermission": {
            "Action": "lambda:InvokeFunction",
            "FunctionName": {"Ref": "PublicationDispatcherFunction"},
            "Principal": "events.amazonaws.com",
            "SourceAccount": {"Ref": "AWS::AccountId"},
            "SourceArn": {"Fn::GetAtt": ["PublicationDueWorkSweepRule", "Arn"]},
        },
        "PublicationRecoverySweepPermission": {
            "Action": "lambda:InvokeFunction",
            "FunctionName": {"Ref": "PublicationRecoveryFunction"},
            "Principal": "events.amazonaws.com",
            "SourceAccount": {"Ref": "AWS::AccountId"},
            "SourceArn": {"Fn::GetAtt": ["PublicationRecoverySweepRule", "Arn"]},
        },
    }

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
    # An unhandled Task failure leaves the failed state as the redrive origin. Catching
    # into a Fail state would make Step Functions redrive re-enter that Fail state and
    # could never rerun the exact worker Task.
    assert "Catch" not in task
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
    assert workflow["Logging"]["Destinations"] == [
        {
            "CloudWatchLogsLogGroup": {
                "LogGroupArn": {"Fn::GetAtt": ["PublicationWorkflowLogGroup", "Arn"]}
            }
        }
    ]


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
    assert "secretsmanager:GetSecretValue" not in worker
    assert not worker & {
        "states:StartExecution",
        "states:RedriveExecution",
        "lambda:InvokeFunction",
    }
    assert "states:RedriveExecution" in recovery
    assert "states:StartExecution" not in recovery
    assert "secretsmanager:GetSecretValue" not in recovery
    recovery_policy = _resources()["PublicationRecoveryRole"]["Properties"]["Policies"][0]
    recovery_query = next(
        statement
        for statement in recovery_policy["PolicyDocument"]["Statement"]
        if statement["Sid"] == "QueryOnlyActivePublicationRecovery"
    )
    assert recovery_query["Resource"] == {
        "Fn::Sub": "${StateTableArn}/index/ExecutionRecoveryIndex"
    }
    assert recovery_query["Condition"] == {
        "ForAllValues:StringLike": {"dynamodb:LeadingKeys": ["PUBLICATION_WORK_RECOVERY#0"]}
    }
    assert {"dynamodb:ConditionCheckItem", "dynamodb:PutItem"}.issubset(retention)
    assert "dynamodb:UpdateItem" not in retention
    assert "sqs:SendMessage" in retention
    assert not retention & {"dynamodb:DeleteItem", "secretsmanager:GetSecretValue"}
    assert "lambda:InvokeFunction" in workflow
    assert not workflow & {
        "dynamodb:GetItem",
        "secretsmanager:GetSecretValue",
        "states:StartExecution",
    }
    assert "PrintifySecretArn" not in _template()["Parameters"]

    for role_name, exact_stream_sid in (
        ("PublicationDispatcherRole", "ReadPublicationStream"),
        ("PublicationRetentionRole", "ReadTerminalPublicationStream"),
    ):
        statements = _resources()[role_name]["Properties"]["Policies"][0]["PolicyDocument"][
            "Statement"
        ]
        exact_stream = next(
            statement for statement in statements if statement["Sid"] == exact_stream_sid
        )
        assert exact_stream["Action"] == [
            "dynamodb:DescribeStream",
            "dynamodb:GetRecords",
            "dynamodb:GetShardIterator",
        ]
        assert exact_stream["Resource"] == {"Ref": "StateTableStreamArn"}
        discover = next(
            statement
            for statement in statements
            if statement["Sid"] == "DiscoverPublicationStreamsInRegion"
        )
        assert discover == {
            "Sid": "DiscoverPublicationStreamsInRegion",
            "Effect": "Allow",
            "Action": "dynamodb:ListStreams",
            "Resource": "*",
            "Condition": {"StringEquals": {"aws:RequestedRegion": {"Ref": "AWS::Region"}}},
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
        "maxReceiveCount": 12,
    }
    # A non-redrivable early failure remains recoverable until the immutable 30-minute
    # publication deadline, when the same message can settle durable authority without provider I/O.
    assert queue["VisibilityTimeout"] * (queue["RedrivePolicy"]["maxReceiveCount"] - 1) >= 1800
    assert queue["SqsManagedSseEnabled"] is True
    assert (
        resources["PublicationOperationsDeadLetterQueue"]["Properties"]["SqsManagedSseEnabled"]
        is True
    )
    assert resources["PublicationOperationsDeadLetterQueue"]["Properties"][
        "RedriveAllowPolicy"
    ] == {
        "redrivePermission": "byQueue",
        "sourceQueueArns": [
            {
                "Fn::Sub": (
                    "arn:${AWS::Partition}:sqs:${AWS::Region}:${AWS::AccountId}:"
                    "mr-lister-phase7-${EnvironmentName}-publication-recovery"
                )
            }
        ],
    }

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

    operations_policy = resources["PublicationOperationsDeadLetterQueuePolicy"]["Properties"]
    assert operations_policy["Queues"] == [{"Ref": "PublicationOperationsDeadLetterQueue"}]
    assert operations_policy["PolicyDocument"]["Statement"] == [
        {
            "Sid": "AcceptOnlyPhase7EventBridgeDeliveryFailures",
            "Effect": "Allow",
            "Principal": {"Service": "events.amazonaws.com"},
            "Action": "sqs:SendMessage",
            "Resource": {"Fn::GetAtt": ["PublicationOperationsDeadLetterQueue", "Arn"]},
            "Condition": {
                "StringEquals": {"aws:SourceAccount": {"Ref": "AWS::AccountId"}},
                "ArnEquals": {
                    "aws:SourceArn": [
                        {"Fn::GetAtt": ["PublicationDueWorkSweepRule", "Arn"]},
                        {"Fn::GetAtt": ["PublicationRecoverySweepRule", "Arn"]},
                        {"Fn::GetAtt": ["PublicationWorkflowFailureRule", "Arn"]},
                    ]
                },
            },
        }
    ]


def test_alarm_matrix_is_encrypted_closed_and_payload_free() -> None:
    resources = _resources()
    key = resources["PublicationAlarmKey"]
    topic = resources["PublicationAlarmTopic"]["Properties"]
    assert key["DeletionPolicy"] == "Retain"
    assert key["UpdateReplacePolicy"] == "Retain"
    assert key["Properties"]["EnableKeyRotation"] is True
    assert key["Properties"]["KeySpec"] == "SYMMETRIC_DEFAULT"
    assert key["Properties"]["KeyUsage"] == "ENCRYPT_DECRYPT"
    assert key["Properties"]["KeyPolicy"]["Statement"] == [
        {
            "Sid": "EnableAccountKeyAdministration",
            "Effect": "Allow",
            "Principal": {"AWS": {"Fn::Sub": "arn:${AWS::Partition}:iam::${AWS::AccountId}:root"}},
            "Action": "kms:*",
            "Resource": "*",
        },
        {
            "Sid": "AllowOnlyPhase7PublicationCloudWatchAlarms",
            "Effect": "Allow",
            "Principal": {"Service": "cloudwatch.amazonaws.com"},
            "Action": ["kms:Decrypt", "kms:GenerateDataKey*"],
            "Resource": "*",
            "Condition": {
                "StringEquals": {"aws:SourceAccount": {"Ref": "AWS::AccountId"}},
                "ArnLike": {
                    "aws:SourceArn": {
                        "Fn::Sub": (
                            "arn:${AWS::Partition}:cloudwatch:${AWS::Region}:"
                            "${AWS::AccountId}:alarm:mr-lister-phase7-${EnvironmentName}-"
                            "publication-*"
                        )
                    }
                },
            },
        },
        {
            "Sid": "AllowSnsEncryptionForOnlyPhase7PublicationAlarms",
            "Effect": "Allow",
            "Principal": {"Service": "sns.amazonaws.com"},
            "Action": ["kms:Decrypt", "kms:GenerateDataKey*"],
            "Resource": "*",
            "Condition": {
                "StringEquals": {
                    "aws:SourceAccount": {"Ref": "AWS::AccountId"},
                    "kms:EncryptionContext:aws:sns:topicArn": {
                        "Fn::Sub": (
                            "arn:${AWS::Partition}:sns:${AWS::Region}:${AWS::AccountId}:"
                            "mr-lister-phase7-${EnvironmentName}-publication-alarms"
                        )
                    },
                },
                "ArnLike": {
                    "aws:SourceArn": {
                        "Fn::Sub": (
                            "arn:${AWS::Partition}:cloudwatch:${AWS::Region}:"
                            "${AWS::AccountId}:alarm:mr-lister-phase7-${EnvironmentName}-"
                            "publication-*"
                        )
                    }
                },
            },
        },
    ]
    assert topic["KmsMasterKeyId"] == {"Fn::GetAtt": ["PublicationAlarmKey", "Arn"]}
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
        "PublicationDueWorkSweepDeliveryAlarm",
        "PublicationRecoverySweepDeliveryAlarm",
        "PublicationWorkflowFailureDeliveryAlarm",
        "PublicationOutcomeUnknownAlarm",
    }
    for alarm in alarms.values():
        assert alarm["ActionsEnabled"] is True
        assert alarm["AlarmActions"] == [{"Ref": "PublicationAlarmTopic"}]
        assert alarm["TreatMissingData"] == "notBreaching"
        assert alarm["EvaluationPeriods"] == 1
        assert alarm["DatapointsToAlarm"] == 1

    for name, rule in {
        "PublicationDueWorkSweepDeliveryAlarm": "PublicationDueWorkSweepRule",
        "PublicationRecoverySweepDeliveryAlarm": "PublicationRecoverySweepRule",
        "PublicationWorkflowFailureDeliveryAlarm": "PublicationWorkflowFailureRule",
    }.items():
        metrics = alarms[name]["Metrics"]
        assert metrics[0] == {
            "Id": "total",
            "Expression": "SUM(METRICS())",
            "ReturnData": True,
        }
        assert {metric["MetricStat"]["Metric"]["MetricName"] for metric in metrics[1:]} == {
            "FailedInvocations",
            "InvocationsFailedToBeSentToDlq",
        }
        assert all(
            metric["MetricStat"]["Metric"]["Dimensions"]
            == [{"Name": "RuleName", "Value": {"Ref": rule}}]
            for metric in metrics[1:]
        )

    metric_filter = resources["PublicationOutcomeUnknownMetric"]["Properties"]
    assert metric_filter["LogGroupName"] == {"Ref": "PublicationRetentionLogGroup"}
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
