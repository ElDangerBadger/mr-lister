from __future__ import annotations

import json
import re
from pathlib import Path

from mr_lister.cloud.phase6_operational_cleanup_composition import (
    TERMINAL_OPERATIONAL_CLEANUP_EVENT,
)
from mr_lister.control.execution_recovery import EXECUTION_RECOVERY_SWEEP_SOURCE
from mr_lister.control.execution_recovery_aws import (
    EXECUTION_RECOVERY_INDEX_NAME,
    EXECUTION_RECOVERY_METRIC_NAMESPACE,
    EXECUTION_RECOVERY_PARTITION_KEY,
)

ROOT = Path(__file__).parents[1]
TEMPLATE = ROOT / "infra" / "phase6" / "template.json"

PROFILE_ENV = {
    "MR_LISTER_PRODUCT_PROFILE_ID": "gildan_64000_swiftpod",
    "MR_LISTER_PRODUCT_PROFILE_VERSION": "2",
    "MR_LISTER_PRODUCT_PROFILE_FINGERPRINT": (
        "5de1257141cfdacb1731f68bb9113712957483b33d5b0f7115afdba86eb7476c"
    ),
    "MR_LISTER_PRODUCT_PROFILE_PATH": (
        "/var/task/config/product_profiles/gildan_64000_swiftpod.json"
    ),
}


def _template() -> dict:
    return json.loads(TEMPLATE.read_text(encoding="utf-8"))


def _resources() -> dict[str, dict]:
    return _template()["Resources"]


def _role_statements(role_name: str) -> dict[str, dict]:
    role = _resources()[role_name]
    statements = role["Properties"]["Policies"][0]["PolicyDocument"]["Statement"]
    return {statement["Sid"]: statement for statement in statements}


def _actions(statements: dict[str, dict]) -> set[str]:
    actions: set[str] = set()
    for statement in statements.values():
        raw = statement["Action"]
        actions.update(raw if isinstance(raw, list) else [raw])
    return actions


def _alarm(logical_id: str) -> dict:
    resource = _resources()[logical_id]
    assert resource["Type"] == "AWS::CloudWatch::Alarm"
    return resource["Properties"]


def test_release_and_agentcore_parameters_are_required_and_shape_closed() -> None:
    template = _template()
    parameters = template["Parameters"]
    required = {
        "ReleaseFingerprint",
        "AgentCoreRuntimeArn",
        "AgentCoreRuntimeEndpointArn",
        "AgentCoreRuntimeVersion",
        "AgentCoreRuntimeQualifier",
        "AgentCoreRuntimeBindingFingerprint",
    }
    assert all("Default" not in parameters[name] for name in required)

    runtime = (
        "arn:aws:bedrock-agentcore:us-west-2:123456789012:runtime/mr_lister_phase6_dev-AbCd123456"
    )
    endpoint = f"{runtime}/runtime-endpoint/phase6_v7_dev"
    assert re.fullmatch(parameters["AgentCoreRuntimeArn"]["AllowedPattern"], runtime)
    assert re.fullmatch(parameters["AgentCoreRuntimeEndpointArn"]["AllowedPattern"], endpoint)
    assert re.fullmatch(parameters["AgentCoreRuntimeVersion"]["AllowedPattern"], "7")
    assert re.fullmatch(parameters["AgentCoreRuntimeQualifier"]["AllowedPattern"], "phase6_v7_dev")
    assert not re.fullmatch(parameters["AgentCoreRuntimeQualifier"]["AllowedPattern"], "DEFAULT")
    assert not re.fullmatch(parameters["ReleaseFingerprint"]["AllowedPattern"], "0" * 64)
    assert re.fullmatch(parameters["ReleaseFingerprint"]["AllowedPattern"], "a" * 64)
    assert not re.fullmatch(
        parameters["AgentCoreRuntimeArn"]["AllowedPattern"],
        "arn:aws:bedrock-agentcore:us-west-2:123456789012:runtime/unshaped",
    )

    assert template["Globals"]["Function"]["Environment"]["Variables"] == {
        "MR_LISTER_PHASE6_SCAFFOLD_ONLY": "true",
        "MR_LISTER_ENVIRONMENT": {"Ref": "EnvironmentName"},
        "MR_LISTER_AWS_ACCOUNT_ID": {"Ref": "AWS::AccountId"},
        "MR_LISTER_RELEASE_FINGERPRINT": {"Ref": "ReleaseFingerprint"},
        "MR_LISTER_STATE_TABLE": {"Ref": "OperationalStateTable"},
        "MR_LISTER_ARTIFACT_BUCKET": {"Ref": "PrivateArtifactBucket"},
    }
    assert template["Outputs"]["DeploymentReadiness"]["Value"] == "SCAFFOLD_ONLY"


def test_active_function_environment_contracts_are_complete_and_pinned() -> None:
    resources = _resources()
    functions = {
        name: resource
        for name, resource in resources.items()
        if resource["Type"] == "AWS::Serverless::Function"
    }
    assert all(function["Properties"]["CodeUri"] == "lambda/" for function in functions.values())

    preparation = functions["PreparationDispatchFunction"]["Properties"]["Environment"]["Variables"]
    assert preparation == {
        "MR_LISTER_AGENTCORE_RUNTIME_ARN": {"Ref": "AgentCoreRuntimeArn"},
        "MR_LISTER_AGENTCORE_RUNTIME_ENDPOINT_ARN": {"Ref": "AgentCoreRuntimeEndpointArn"},
        "MR_LISTER_AGENTCORE_RUNTIME_VERSION": {"Ref": "AgentCoreRuntimeVersion"},
        "MR_LISTER_AGENTCORE_RUNTIME_QUALIFIER": {"Ref": "AgentCoreRuntimeQualifier"},
        "MR_LISTER_AGENTCORE_RUNTIME_BINDING_FINGERPRINT": {
            "Ref": "AgentCoreRuntimeBindingFingerprint"
        },
    }
    invoke = _role_statements("PreparationDispatchFunctionRole")["InvokeConfiguredStrandsRuntime"]
    assert invoke["Resource"] == [
        {"Ref": "AgentCoreRuntimeArn"},
        {"Ref": "AgentCoreRuntimeEndpointArn"},
    ]

    provider = functions["ProviderDraftFunction"]["Properties"]["Environment"]["Variables"]
    assert provider == {"MR_LISTER_PRINTIFY_SECRET_ARN": {"Ref": "PrintifySecretArn"}} | (
        PROFILE_ENV
    )

    api_common = {
        "MR_LISTER_COGNITO_ISSUER": {
            "Fn::Sub": ("https://cognito-idp.${AWS::Region}.${AWS::URLSuffix}/${SellerUserPool}")
        },
        "MR_LISTER_COGNITO_CLIENT_ID": {"Ref": "SellerUserPoolClient"},
        "MR_LISTER_COGNITO_SCOPE": "mr-lister-api/seller",
        "MR_LISTER_COGNITO_GROUP": "seller",
        "MR_LISTER_APPLICATION_ORIGIN": {"Ref": "ApplicationOrigin"},
    }
    artifact = {
        "MR_LISTER_ARTIFACT_BUCKET_OWNER_ACCOUNT_ID": {"Ref": "AWS::AccountId"},
        "MR_LISTER_ARTIFACT_ORIGIN": {
            "Fn::Sub": ("https://${PrivateArtifactBucket}.s3.${AWS::Region}.${AWS::URLSuffix}")
        },
    }
    for function_name in ("UploadApiFunction", "ReviewQueryApiFunction"):
        variables = functions[function_name]["Properties"]["Environment"]["Variables"]
        assert variables == api_common | artifact | PROFILE_ENV
    assert (
        functions["SellerCommandApiFunction"]["Properties"]["Environment"]["Variables"]
        == api_common
    )


def test_terminal_cleanup_is_daily_serial_and_dynamodb_ttl_only() -> None:
    resources = _resources()
    function = resources["TerminalOperationalCleanupFunction"]
    properties = function["Properties"]
    assert function["DependsOn"] == "TerminalOperationalCleanupLogGroup"
    assert properties["Handler"] == "phase6_lambda.terminal_operational_cleanup_handler"
    assert properties["Role"] == {"Fn::GetAtt": ["TerminalOperationalCleanupFunctionRole", "Arn"]}
    assert properties["ReservedConcurrentExecutions"] == 1
    assert properties["Timeout"] == 300
    assert properties["CodeUri"] == "lambda/"
    assert set(properties["Events"]) == {"TerminalOperationalCleanupSweep"}
    schedule = properties["Events"]["TerminalOperationalCleanupSweep"]
    assert schedule["Type"] == "Schedule"
    assert schedule["Properties"]["Schedule"] == "rate(1 day)"
    assert schedule["Properties"]["Enabled"] is True
    assert json.loads(schedule["Properties"]["Input"]) == TERMINAL_OPERATIONAL_CLEANUP_EVENT
    assert schedule["Properties"]["RetryPolicy"] == {
        "MaximumEventAgeInSeconds": 3600,
        "MaximumRetryAttempts": 2,
    }
    assert resources["TerminalOperationalCleanupLogGroup"]["Properties"]["RetentionInDays"] == 14

    statements = _role_statements("TerminalOperationalCleanupFunctionRole")
    assert set(statements) == {
        "WriteTerminalCleanupLogs",
        "ScanProjectedControlJobAuthority",
        "ReadAndAssignExactOperationalExpiry",
    }
    assert _actions(statements) == {
        "logs:CreateLogStream",
        "logs:PutLogEvents",
        "dynamodb:Scan",
        "dynamodb:GetItem",
        "dynamodb:PutItem",
        "dynamodb:Query",
        "dynamodb:ConditionCheckItem",
        "dynamodb:UpdateItem",
    }
    data_statements = {
        name: statement
        for name, statement in statements.items()
        if name != "WriteTerminalCleanupLogs"
    }
    assert all(
        statement["Resource"] == {"Fn::GetAtt": ["OperationalStateTable", "Arn"]}
        for statement in data_statements.values()
    )
    serialized = json.dumps(statements, sort_keys=True).casefold()
    for forbidden in (
        "deleteitem",
        "transactgetitems",
        "transactwriteitems",
        "s3:",
        "states:",
        "secret",
        "bedrock",
        "execute-api",
        "lambda:invoke",
    ):
        assert forbidden not in serialized


def test_execution_recovery_index_schedule_dlq_and_closed_role_are_exact() -> None:
    resources = _resources()
    table = resources["OperationalStateTable"]["Properties"]
    recovery_index = next(
        index
        for index in table["GlobalSecondaryIndexes"]
        if index["IndexName"] == EXECUTION_RECOVERY_INDEX_NAME
    )
    assert recovery_index == {
        "IndexName": "ExecutionRecoveryIndex",
        "KeySchema": [
            {"AttributeName": "recovery_pk", "KeyType": "HASH"},
            {"AttributeName": "recovery_sk", "KeyType": "RANGE"},
        ],
        "Projection": {"ProjectionType": "KEYS_ONLY"},
    }

    function = resources["StuckExecutionRecoveryFunction"]
    properties = function["Properties"]
    assert function["DependsOn"] == "StuckExecutionRecoveryLogGroup"
    assert properties["CodeUri"] == "lambda/"
    assert properties["Handler"] == "phase6_lambda.stuck_execution_recovery_handler"
    assert properties["Role"] == {"Fn::GetAtt": ["StuckExecutionRecoveryFunctionRole", "Arn"]}
    assert properties["ReservedConcurrentExecutions"] == 1
    assert properties["Timeout"] == 120
    assert properties["Environment"]["Variables"] == {
        "MR_LISTER_EXECUTION_RECOVERY_STALE_SECONDS": "1200",
        "MR_LISTER_EXECUTION_RECOVERY_BATCH_LIMIT": "25",
        "MR_LISTER_EXECUTION_RECOVERY_MAX_CAS_RECHECKS": "2",
        "MR_LISTER_PREPARE_MACHINE_ARN": {"Ref": "PrepareStateMachine"},
        "MR_LISTER_SYNCHRONIZE_PRODUCT_MACHINE_ARN": {"Ref": "SynchronizeProductStateMachine"},
        "MR_LISTER_RECONCILE_PRODUCT_MACHINE_ARN": {"Ref": "ReconcileProductStateMachine"},
        "MR_LISTER_REFRESH_ECONOMICS_MACHINE_ARN": {"Ref": "RefreshEconomicsStateMachine"},
    }
    assert resources["StuckExecutionRecoveryLogGroup"]["Properties"]["RetentionInDays"] == 14

    rule = resources["StuckExecutionRecoveryScheduleRule"]["Properties"]
    assert rule["ScheduleExpression"] == "rate(5 minutes)"
    assert rule["State"] == "ENABLED"
    assert len(rule["Targets"]) == 1
    target = rule["Targets"][0]
    assert target["Arn"] == {"Fn::GetAtt": ["StuckExecutionRecoveryFunction", "Arn"]}
    assert json.loads(target["Input"]) == {"source": EXECUTION_RECOVERY_SWEEP_SOURCE}
    assert target["RetryPolicy"] == {
        "MaximumEventAgeInSeconds": 3600,
        "MaximumRetryAttempts": 2,
    }
    assert target["DeadLetterConfig"] == {
        "Arn": {"Fn::GetAtt": ["StuckExecutionRecoveryDeadLetterQueue", "Arn"]}
    }

    queue = resources["StuckExecutionRecoveryDeadLetterQueue"]["Properties"]
    assert queue["MessageRetentionPeriod"] == 1_209_600
    assert queue["SqsManagedSseEnabled"] is True
    queue_statement = resources["StuckExecutionRecoveryDeadLetterQueuePolicy"]["Properties"][
        "PolicyDocument"
    ]["Statement"]
    assert len(queue_statement) == 1
    assert queue_statement[0]["Principal"] == {"Service": "events.amazonaws.com"}
    assert queue_statement[0]["Action"] == "sqs:SendMessage"
    assert queue_statement[0]["Condition"] == {
        "ArnEquals": {
            "aws:SourceArn": {"Fn::GetAtt": ["StuckExecutionRecoveryScheduleRule", "Arn"]}
        },
        "StringEquals": {"aws:SourceAccount": {"Ref": "AWS::AccountId"}},
    }

    statements = _role_statements("StuckExecutionRecoveryFunctionRole")
    assert set(statements) == {
        "WriteExecutionRecoveryLogsAndMetrics",
        "QueryOnlyExecutionRecoveryIndex",
        "StrongReadAndSettleExactJobAuthority",
        "DescribeOnlyPhase6Executions",
    }
    query = statements["QueryOnlyExecutionRecoveryIndex"]
    assert query["Action"] == "dynamodb:Query"
    assert query["Resource"] == {
        "Fn::Sub": "${OperationalStateTable.Arn}/index/ExecutionRecoveryIndex"
    }
    assert query["Condition"] == {
        "ForAllValues:StringEquals": {"dynamodb:LeadingKeys": [EXECUTION_RECOVERY_PARTITION_KEY]},
        "Null": {"dynamodb:LeadingKeys": "false"},
    }
    settle = statements["StrongReadAndSettleExactJobAuthority"]
    assert set(settle["Action"]) == {
        "dynamodb:GetItem",
        "dynamodb:PutItem",
    }
    assert settle["Condition"] == {
        "ForAllValues:StringLike": {"dynamodb:LeadingKeys": ["JOB#*", "OWNER#*"]},
        "Null": {"dynamodb:LeadingKeys": "false"},
    }
    observe = statements["DescribeOnlyPhase6Executions"]
    assert observe["Action"] == "states:DescribeExecution"
    assert len(observe["Resource"]) == 4
    assert all("execution:mr-lister-phase6-" in item["Fn::Sub"] for item in observe["Resource"])
    serialized = json.dumps(statements, sort_keys=True).casefold()
    for forbidden in (
        "states:startexecution",
        "states:stopexecution",
        "states:redriveexecution",
        "s3:",
        "secretsmanager",
        "bedrock",
        "agentcore",
        "lambda:invokefunction",
        "cloudwatch:putmetricdata",
        "dynamodb:deleteitem",
        "dynamodb:updateitem",
        "dynamodb:conditioncheckitem",
        "dynamodb:transactgetitems",
        "dynamodb:transactwriteitems",
        "sqs:",
    ):
        assert forbidden not in serialized


def test_alarm_fabric_covers_every_function_workflow_and_shared_dependency() -> None:
    resources = _resources()
    topic = resources["OperationalAlarmTopic"]
    assert topic["Type"] == "AWS::SNS::Topic"
    assert topic["Properties"]["KmsMasterKeyId"] == {
        "Fn::GetAtt": ["OperationalAlarmTopicKey", "Arn"]
    }
    key = resources["OperationalAlarmTopicKey"]["Properties"]
    assert key["EnableKeyRotation"] is True
    key_statements = {statement["Sid"]: statement for statement in key["KeyPolicy"]["Statement"]}
    cloudwatch_key = key_statements["AllowOnlyPhase6CloudWatchAlarmEncryption"]
    assert cloudwatch_key["Principal"] == {"Service": "cloudwatch.amazonaws.com"}
    assert set(cloudwatch_key["Action"]) == {"kms:Decrypt", "kms:GenerateDataKey*"}
    assert cloudwatch_key["Condition"] == {
        "ArnLike": {
            "aws:SourceArn": {
                "Fn::Sub": (
                    "arn:${AWS::Partition}:cloudwatch:${AWS::Region}:${AWS::AccountId}:"
                    "alarm:mr-lister-phase6-${EnvironmentName}-*"
                )
            }
        },
        "StringEquals": {"aws:SourceAccount": {"Ref": "AWS::AccountId"}},
    }
    sns_key = key_statements["AllowOnlyOperationalTopicEncryptionContext"]
    assert sns_key["Principal"] == {"Service": "sns.amazonaws.com"}
    assert sns_key["Condition"] == {
        "StringEquals": {
            "kms:EncryptionContext:aws:sns:topicArn": {
                "Fn::Sub": (
                    "arn:${AWS::Partition}:sns:${AWS::Region}:${AWS::AccountId}:"
                    "mr-lister-phase6-${EnvironmentName}-operational-alarms"
                )
            }
        }
    }
    topic_policy = resources["OperationalAlarmTopicPolicy"]["Properties"]
    assert topic_policy["Topics"] == [{"Ref": "OperationalAlarmTopic"}]
    publish = topic_policy["PolicyDocument"]["Statement"]
    assert len(publish) == 1
    assert publish[0]["Principal"] == {"Service": "cloudwatch.amazonaws.com"}
    assert publish[0]["Action"] == "sns:Publish"
    assert publish[0]["Resource"] == {"Ref": "OperationalAlarmTopic"}
    assert publish[0]["Condition"] == cloudwatch_key["Condition"]
    alarms = {
        name: resource["Properties"]
        for name, resource in resources.items()
        if resource["Type"] == "AWS::CloudWatch::Alarm"
    }
    assert alarms
    assert all(alarm["ActionsEnabled"] is True for alarm in alarms.values())
    assert all(
        alarm["AlarmActions"] == [{"Ref": "OperationalAlarmTopic"}] for alarm in alarms.values()
    )

    functions = {
        name
        for name, resource in resources.items()
        if resource["Type"] == "AWS::Serverless::Function"
    }
    for logical_id, metric_name in (
        ("Phase6LambdaErrorsAlarm", "Errors"),
        ("Phase6LambdaThrottlesAlarm", "Throttles"),
        ("Phase6LambdaDurationAlarm", "Duration"),
    ):
        metrics = _alarm(logical_id)["Metrics"]
        covered = {
            metric["MetricStat"]["Metric"]["Dimensions"][0]["Value"]["Ref"]
            for metric in metrics
            if "MetricStat" in metric
        }
        assert covered == functions
        assert {
            metric["MetricStat"]["Metric"]["MetricName"]
            for metric in metrics
            if "MetricStat" in metric
        } == {metric_name}

    expected_workflows = {
        "PrepareStateMachine",
        "SynchronizeProductStateMachine",
        "ReconcileProductStateMachine",
        "RefreshEconomicsStateMachine",
    }
    workflow_coverage: set[tuple[str, str]] = set()
    for alarm in alarms.values():
        if alarm.get("Namespace") != "AWS/States":
            continue
        dimension = alarm["Dimensions"][0]
        workflow_coverage.add((dimension["Value"]["Ref"], alarm["MetricName"]))
    assert workflow_coverage == {
        (workflow, metric)
        for workflow in expected_workflows
        for metric in ("ExecutionsFailed", "ExecutionsTimedOut", "ExecutionsAborted")
    }

    ddb = _alarm("OperationalStateTableThrottlesAlarm")
    assert {
        metric["MetricStat"]["Metric"]["MetricName"]
        for metric in ddb["Metrics"]
        if "MetricStat" in metric
    } == {"ReadThrottleEvents", "WriteThrottleEvents"}
    api = _alarm("SellerApiServerErrorsAlarm")
    assert api["Namespace"] == "AWS/ApiGateway"
    assert api["MetricName"] == "5xx"
    assert api["Dimensions"] == [
        {"Name": "ApiId", "Value": {"Ref": "SellerHttpApi"}},
        {"Name": "Stage", "Value": "$default"},
    ]

    for logical_id in (
        "SourceVersionRetentionErrorsAlarm",
        "SourceVersionRetentionLivenessAlarm",
        "TerminalOperationalCleanupErrorsAlarm",
        "TerminalOperationalCleanupLivenessAlarm",
    ):
        assert _alarm(logical_id)["Namespace"] == "AWS/Lambda"


def test_recovery_alarms_bind_exact_emf_schedule_dlq_and_lambda_metrics() -> None:
    custom = {
        "ExecutionRecoveryAlarmSignalsAlarm": "AlarmSignals",
        "ExecutionRecoveryAuthorityConflictsAlarm": "AuthorityConflicts",
        "ExecutionRecoveryDependencyUnavailableAlarm": "DependencyUnavailable",
        "ExecutionRecoverySettlementExhaustedAlarm": "SettlementExhausted",
        "ExecutionRecoveryRunningPastBoundAlarm": "RunningPastBound",
        "ExecutionRecoveryBatchSaturationAlarm": "BatchLimitReached",
    }
    for logical_id, metric_name in custom.items():
        alarm = _alarm(logical_id)
        assert alarm["Namespace"] == EXECUTION_RECOVERY_METRIC_NAMESPACE
        assert alarm["MetricName"] == metric_name
        assert alarm["Dimensions"] == [{"Name": "Environment", "Value": {"Ref": "EnvironmentName"}}]
        assert alarm["Threshold"] == 1
        assert alarm["ComparisonOperator"] == "GreaterThanOrEqualToThreshold"
    saturation = _alarm("ExecutionRecoveryBatchSaturationAlarm")
    assert saturation["EvaluationPeriods"] == 3
    assert saturation["DatapointsToAlarm"] == 3

    recovery_lambda = {
        "StuckExecutionRecoveryErrorsAlarm": ("Errors", 0),
        "StuckExecutionRecoveryThrottlesAlarm": ("Throttles", 0),
        "StuckExecutionRecoveryDurationAlarm": ("Duration", 96_000),
    }
    for logical_id, (metric_name, threshold) in recovery_lambda.items():
        alarm = _alarm(logical_id)
        assert alarm["Namespace"] == "AWS/Lambda"
        assert alarm["MetricName"] == metric_name
        assert alarm["Dimensions"] == [
            {"Name": "FunctionName", "Value": {"Ref": "StuckExecutionRecoveryFunction"}}
        ]
        assert alarm["Threshold"] == threshold

    schedule = _alarm("StuckExecutionRecoveryScheduleFailuresAlarm")
    assert schedule["Namespace"] == "AWS/Events"
    assert schedule["MetricName"] == "FailedInvocations"
    assert schedule["Dimensions"] == [
        {"Name": "RuleName", "Value": {"Ref": "StuckExecutionRecoveryScheduleRule"}}
    ]
    dead_letters = _alarm("StuckExecutionRecoveryDeadLettersAlarm")
    assert dead_letters["Namespace"] == "AWS/SQS"
    assert dead_letters["MetricName"] == "ApproximateNumberOfMessagesVisible"
    assert dead_letters["Dimensions"] == [
        {
            "Name": "QueueName",
            "Value": {"Fn::GetAtt": ["StuckExecutionRecoveryDeadLetterQueue", "QueueName"]},
        }
    ]
