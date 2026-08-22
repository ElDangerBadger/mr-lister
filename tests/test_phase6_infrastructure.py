from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
PHASE6 = ROOT / "infra" / "phase6"
TEMPLATE = PHASE6 / "template.json"
MACHINES = PHASE6 / "statemachine"


def load_template() -> dict:
    return json.loads(TEMPLATE.read_text(encoding="utf-8"))


def policies(resource: dict) -> list[dict]:
    return resource["Properties"].get("Policies", [])


def serialized_policies(resource: dict) -> str:
    return json.dumps(policies(resource), sort_keys=True)


def test_phase6_is_a_separate_required_configuration_sam_application() -> None:
    template = load_template()
    parameters = template["Parameters"]

    assert template["Transform"] == "AWS::Serverless-2016-10-31"
    for name in ("AgentCoreRuntimeArn", "PrintifySecretArn"):
        assert "Default" not in parameters[name]
        assert "^$" not in parameters[name]["AllowedPattern"]
    assert "phase4" not in json.dumps(template).lower()
    assert "phase5" not in json.dumps(template).lower()


def test_table_has_durable_due_work_topology() -> None:
    table = load_template()["Resources"]["OperationalStateTable"]
    properties = table["Properties"]

    assert table["DeletionPolicy"] == "Retain"
    assert table["UpdateReplacePolicy"] == "Retain"
    assert properties["BillingMode"] == "PAY_PER_REQUEST"
    assert properties["KeySchema"] == [
        {"AttributeName": "PK", "KeyType": "HASH"},
        {"AttributeName": "SK", "KeyType": "RANGE"},
    ]
    assert properties["GlobalSecondaryIndexes"] == [
        {
            "IndexName": "DueWorkIndex",
            "KeySchema": [
                {"AttributeName": "dispatch_pk", "KeyType": "HASH"},
                {"AttributeName": "dispatch_sk", "KeyType": "RANGE"},
            ],
            "Projection": {"ProjectionType": "ALL"},
        }
    ]
    assert properties["StreamSpecification"] == {"StreamViewType": "KEYS_ONLY"}
    assert properties["PointInTimeRecoverySpecification"] == {"PointInTimeRecoveryEnabled": True}

    stream_event = load_template()["Resources"]["DispatcherFunction"]["Properties"]["Events"][
        "OperationalStateChanges"
    ]["Properties"]
    filters = stream_event["FilterCriteria"]["Filters"]
    assert len(filters) == 1
    assert json.loads(filters[0]["Pattern"]) == {
        "dynamodb": {"Keys": {"SK": {"S": [{"prefix": "WORK#"}]}}}
    }


def test_artifact_bucket_is_private_encrypted_versioned_and_tls_only() -> None:
    resources = load_template()["Resources"]
    bucket = resources["PrivateArtifactBucket"]
    properties = bucket["Properties"]

    assert bucket["DeletionPolicy"] == "Retain"
    assert bucket["UpdateReplacePolicy"] == "Retain"
    assert properties["BucketEncryption"] == {
        "ServerSideEncryptionConfiguration": [
            {"ServerSideEncryptionByDefault": {"SSEAlgorithm": "AES256"}}
        ]
    }
    assert properties["VersioningConfiguration"] == {"Status": "Enabled"}
    assert properties["LifecycleConfiguration"] == {
        "Rules": [
            {
                "Id": "AbortIncompleteMultipartUploads",
                "Status": "Enabled",
                "AbortIncompleteMultipartUpload": {"DaysAfterInitiation": 7},
            }
        ]
    }
    assert all(properties["PublicAccessBlockConfiguration"].values())
    assert properties["OwnershipControls"] == {
        "Rules": [{"ObjectOwnership": "BucketOwnerEnforced"}]
    }
    statement = resources["PrivateArtifactBucketPolicy"]["Properties"]["PolicyDocument"][
        "Statement"
    ]
    assert statement == [
        {
            "Sid": "DenyInsecureTransport",
            "Effect": "Deny",
            "Principal": "*",
            "Action": "s3:*",
            "Resource": [
                {"Fn::GetAtt": ["PrivateArtifactBucket", "Arn"]},
                {"Fn::Sub": "${PrivateArtifactBucket.Arn}/*"},
            ],
            "Condition": {"Bool": {"aws:SecureTransport": "false"}},
        }
    ]


def test_exact_four_standard_machines_have_private_error_logs() -> None:
    resources = load_template()["Resources"]
    machines = {
        name: resource
        for name, resource in resources.items()
        if resource["Type"] == "AWS::Serverless::StateMachine"
    }
    assert set(machines) == {
        "PrepareStateMachine",
        "SynchronizeProductStateMachine",
        "ReconcileProductStateMachine",
        "RefreshEconomicsStateMachine",
    }
    for machine in machines.values():
        properties = machine["Properties"]
        assert properties["Type"] == "STANDARD"
        assert properties["Logging"]["IncludeExecutionData"] is False
        assert properties["Logging"]["Level"] == "ERROR"
        definition = json.loads((PHASE6 / properties["DefinitionUri"]).read_text())
        assert not any(state.get("Type") == "Wait" for state in definition["States"].values())

    log_groups = [
        resource for resource in resources.values() if resource["Type"] == "AWS::Logs::LogGroup"
    ]
    assert len(log_groups) == 8
    assert all(group["Properties"]["RetentionInDays"] == 14 for group in log_groups)


def test_functions_have_distinct_explicit_roles_and_scaffold_gate() -> None:
    template = load_template()
    resources = template["Resources"]
    functions = {
        name: resource
        for name, resource in resources.items()
        if resource["Type"] == "AWS::Serverless::Function"
    }
    assert set(functions) == {
        "DispatcherFunction",
        "PreparationDispatchFunction",
        "ProviderDraftFunction",
        "SettlementFunction",
    }
    roles = {resource["Properties"]["Role"]["Fn::GetAtt"][0] for resource in functions.values()}
    assert roles == {
        "DispatcherFunctionRole",
        "PreparationDispatchFunctionRole",
        "ProviderDraftFunctionRole",
        "SettlementFunctionRole",
    }
    assert (
        template["Globals"]["Function"]["Environment"]["Variables"][
            "MR_LISTER_PHASE6_SCAFFOLD_ONLY"
        ]
        == "true"
    )
    assert template["Outputs"]["DeploymentReadiness"]["Value"] == "SCAFFOLD_ONLY"
    assert all("ManagedPolicyArns" not in resources[role]["Properties"] for role in roles)
    assert functions["PreparationDispatchFunction"]["Properties"]["Timeout"] == 600
    assert functions["ProviderDraftFunction"]["Properties"]["Timeout"] == 600
    assert functions["SettlementFunction"]["Properties"]["Timeout"] == 30


def test_iam_keeps_agentcore_secret_and_state_authority_separate() -> None:
    resources = load_template()["Resources"]
    dispatcher = serialized_policies(resources["DispatcherFunctionRole"])
    preparation = serialized_policies(resources["PreparationDispatchFunctionRole"])
    provider = serialized_policies(resources["ProviderDraftFunctionRole"])
    settlement = serialized_policies(resources["SettlementFunctionRole"])

    assert "states:StartExecution" in dispatcher
    assert "DueWorkIndex" in dispatcher
    assert "dynamodb:PutItem" in dispatcher
    assert "dynamodb:UpdateItem" not in dispatcher
    assert "bedrock-agentcore:InvokeAgentRuntime" not in dispatcher
    assert "secretsmanager:GetSecretValue" not in dispatcher

    assert "bedrock-agentcore:InvokeAgentRuntime" in preparation
    assert '"Resource": {"Ref": "AgentCoreRuntimeArn"}' in preparation
    assert "secretsmanager" not in preparation
    assert "dynamodb:TransactWriteItems" not in preparation
    assert "s3:GetObject" not in preparation

    assert "secretsmanager:GetSecretValue" in provider
    assert '"Resource": {"Ref": "PrintifySecretArn"}' in provider
    assert "dynamodb:PutItem" in provider
    assert '"Action": "s3:GetObjectVersion"' in provider
    assert '"s3:GetObject"' not in provider
    assert "private/owners/*/jobs/*/source/source.png" in provider
    assert "bedrock-agentcore" not in provider

    assert "dynamodb:TransactWriteItems" in settlement
    assert "dynamodb:PutItem" in settlement
    assert "bedrock-agentcore" not in settlement
    assert "s3:GetObject" not in settlement
    assert "secretsmanager" not in settlement


def test_dispatcher_and_machine_routes_are_exactly_allowlisted() -> None:
    module_path = PHASE6 / "lambda" / "phase6_lambda.py"
    spec = importlib.util.spec_from_file_location("phase6_lambda", module_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    assert module.WORK_TYPE_STATE_MACHINE_ENV == {
        "prepare": "MR_LISTER_PREPARE_MACHINE_ARN",
        "synchronize_product": "MR_LISTER_SYNCHRONIZE_PRODUCT_MACHINE_ARN",
        "reconcile_product": "MR_LISTER_RECONCILE_PRODUCT_MACHINE_ARN",
        "refresh_economics": "MR_LISTER_REFRESH_ECONOMICS_MACHINE_ARN",
    }
    dispatcher_variables = load_template()["Resources"]["DispatcherFunction"]["Properties"][
        "Environment"
    ]["Variables"]
    assert set(dispatcher_variables) == set(module.WORK_TYPE_STATE_MACHINE_ENV.values())
    assert {value["Ref"] for value in dispatcher_variables.values()} == {
        "PrepareStateMachine",
        "SynchronizeProductStateMachine",
        "ReconcileProductStateMachine",
        "RefreshEconomicsStateMachine",
    }
    with pytest.raises(ValueError, match="Unsupported Phase 6 work type"):
        module.dispatcher_handler({"work_type": "activate_marketplace"}, None)
    with pytest.raises(module.Phase6ScaffoldNotReady):
        module.dispatcher_handler({"work_type": "prepare"}, None)


def test_no_forbidden_external_capability_is_present() -> None:
    deployment_text = "\n".join(
        path.read_text(encoding="utf-8")
        for path in sorted(PHASE6.rglob("*"))
        if path.is_file() and path.name != "README.md" and path.suffix in {".json", ".py", ".txt"}
    ).lower()
    for forbidden in (
        "waitfortasktoken",
        "sendtasksuccess",
        "sendtaskfailure",
        "callback",
        "publish",
        "fulfill",
        "createorder",
        "orders.json",
    ):
        assert forbidden not in deployment_text


def test_state_machine_roles_invoke_only_their_declared_functions() -> None:
    resources = load_template()["Resources"]
    expected = {
        "PrepareStateMachineRole": {"PreparationDispatchFunction", "SettlementFunction"},
        "SynchronizeProductStateMachineRole": {"ProviderDraftFunction", "SettlementFunction"},
        "ReconcileProductStateMachineRole": {"ProviderDraftFunction", "SettlementFunction"},
        "RefreshEconomicsStateMachineRole": {"ProviderDraftFunction", "SettlementFunction"},
    }
    for role_name, logical_ids in expected.items():
        statements = policies(resources[role_name])[0]["PolicyDocument"]["Statement"]
        invoke = next(
            statement for statement in statements if statement["Action"] == "lambda:InvokeFunction"
        )
        raw_resources = invoke["Resource"]
        if not isinstance(raw_resources, list):
            raw_resources = [raw_resources]
        actual = {resource["Fn::GetAtt"][0] for resource in raw_resources}
        assert actual == logical_ids
