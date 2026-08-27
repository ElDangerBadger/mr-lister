from __future__ import annotations

import importlib.util
import json
import re
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
    application_origin_pattern = parameters["ApplicationOrigin"]["AllowedPattern"]
    assert re.fullmatch(application_origin_pattern, "https://seller.example.test")
    assert not re.fullmatch(application_origin_pattern, "https://seller.example.test:8443")
    assert not re.fullmatch(application_origin_pattern, "https://Seller.example.test")
    assert "phase4" not in json.dumps(template).lower()
    assert "phase5" not in json.dumps(template).lower()


def test_identity_is_invite_only_totp_and_public_code_flow() -> None:
    resources = load_template()["Resources"]
    pool = resources["SellerUserPool"]
    pool_properties = pool["Properties"]

    assert pool["DeletionPolicy"] == "Retain"
    assert pool["UpdateReplacePolicy"] == "Retain"
    assert pool_properties["AdminCreateUserConfig"] == {"AllowAdminCreateUserOnly": True}
    assert pool_properties["MfaConfiguration"] == "ON"
    assert pool_properties["EnabledMfas"] == ["SOFTWARE_TOKEN_MFA"]
    assert pool_properties["DeletionProtection"] == "ACTIVE"
    assert pool_properties["UsernameAttributes"] == ["email"]
    assert pool_properties["AutoVerifiedAttributes"] == ["email"]

    group = resources["SellerUserPoolGroup"]["Properties"]
    assert group == {
        "Description": "Invite-only Mr Lister sellers",
        "GroupName": "seller",
        "Precedence": 0,
        "UserPoolId": {"Ref": "SellerUserPool"},
    }
    resource_server = resources["SellerApiResourceServer"]["Properties"]
    assert resource_server["Identifier"] == "mr-lister-api"
    assert resource_server["Scopes"] == [
        {
            "ScopeName": "seller",
            "ScopeDescription": "Operate only the authenticated seller's Mr Lister drafts",
        }
    ]

    client = resources["SellerUserPoolClient"]
    assert client["DependsOn"] == "SellerApiResourceServer"
    client_properties = client["Properties"]
    assert client_properties["GenerateSecret"] is False
    assert client_properties["AllowedOAuthFlowsUserPoolClient"] is True
    assert client_properties["AllowedOAuthFlows"] == ["code"]
    assert client_properties["AllowedOAuthScopes"] == ["openid", "mr-lister-api/seller"]
    assert client_properties["CallbackURLs"] == [{"Fn::Sub": "${ApplicationOrigin}/auth/callback"}]
    assert client_properties["LogoutURLs"] == [{"Fn::Sub": "${ApplicationOrigin}/"}]
    assert client_properties["SupportedIdentityProviders"] == ["COGNITO"]
    assert client_properties["AccessTokenValidity"] == 60
    assert client_properties["IdTokenValidity"] == 60
    assert client_properties["RefreshTokenValidity"] == 30
    assert client_properties["TokenValidityUnits"] == {
        "AccessToken": "minutes",
        "IdToken": "minutes",
        "RefreshToken": "days",
    }
    assert client_properties["EnableTokenRevocation"] is True
    assert client_properties["PreventUserExistenceErrors"] == "ENABLED"
    assert not any(
        resource["Type"] == "AWS::Cognito::IdentityPool" for resource in resources.values()
    )


def test_http_api_uses_exact_jwt_scope_cors_and_minimal_access_logs() -> None:
    resources = load_template()["Resources"]
    api = resources["SellerHttpApi"]["Properties"]

    assert api["StageName"] == "$default"
    assert api["FailOnWarnings"] is True
    assert api["DefinitionBody"]["paths"] == {}
    assert api["Auth"] == {
        "DefaultAuthorizer": "SellerJwtAuthorizer",
        "Authorizers": {
            "SellerJwtAuthorizer": {
                "AuthorizationScopes": ["mr-lister-api/seller"],
                "IdentitySource": "$request.header.Authorization",
                "JwtConfiguration": {
                    "issuer": {
                        "Fn::Sub": (
                            "https://cognito-idp.${AWS::Region}.${AWS::URLSuffix}/${SellerUserPool}"
                        )
                    },
                    "audience": [{"Ref": "SellerUserPoolClient"}],
                },
            }
        },
    }
    assert api["CorsConfiguration"] == {
        "AllowCredentials": False,
        "AllowHeaders": ["Authorization", "Content-Type", "Idempotency-Key", "If-Match"],
        "AllowMethods": ["GET", "POST", "PUT", "OPTIONS"],
        "AllowOrigins": [{"Ref": "ApplicationOrigin"}],
        "ExposeHeaders": ["ETag", "Retry-After", "X-Request-Id"],
        "MaxAge": 300,
    }
    access_log = api["AccessLogSettings"]
    assert access_log["DestinationArn"] == {"Fn::GetAtt": ["SellerApiAccessLogGroup", "Arn"]}
    assert json.loads(access_log["Format"]) == {
        "requestId": "$context.requestId",
        "routeKey": "$context.routeKey",
        "status": "$context.status",
        "responseLength": "$context.responseLength",
        "integrationLatency": "$context.integrationLatency",
    }
    assert "authoriz" not in access_log["Format"].lower()
    assert "header" not in access_log["Format"].lower()


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
    assert properties["AttributeDefinitions"] == [
        {"AttributeName": "PK", "AttributeType": "S"},
        {"AttributeName": "SK", "AttributeType": "S"},
        {"AttributeName": "dispatch_pk", "AttributeType": "S"},
        {"AttributeName": "dispatch_sk", "AttributeType": "S"},
        {"AttributeName": "owner_jobs_pk", "AttributeType": "S"},
        {"AttributeName": "owner_jobs_sk", "AttributeType": "S"},
        {"AttributeName": "recovery_pk", "AttributeType": "S"},
        {"AttributeName": "recovery_sk", "AttributeType": "S"},
    ]
    assert properties["GlobalSecondaryIndexes"] == [
        {
            "IndexName": "DueWorkIndex",
            "KeySchema": [
                {"AttributeName": "dispatch_pk", "KeyType": "HASH"},
                {"AttributeName": "dispatch_sk", "KeyType": "RANGE"},
            ],
            "Projection": {"ProjectionType": "ALL"},
        },
        {
            "IndexName": "OwnerJobsIndex",
            "KeySchema": [
                {"AttributeName": "owner_jobs_pk", "KeyType": "HASH"},
                {"AttributeName": "owner_jobs_sk", "KeyType": "RANGE"},
            ],
            "Projection": {"ProjectionType": "ALL"},
        },
        {
            "IndexName": "ExecutionRecoveryIndex",
            "KeySchema": [
                {"AttributeName": "recovery_pk", "KeyType": "HASH"},
                {"AttributeName": "recovery_sk", "KeyType": "RANGE"},
            ],
            "Projection": {"ProjectionType": "KEYS_ONLY"},
        },
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
            },
            {
                "Id": "ExpireUnreferencedStagedArtwork",
                "Status": "Enabled",
                "TagFilters": [{"Key": "mr-lister-state", "Value": "staged"}],
                "ExpirationInDays": 1,
                "NoncurrentVersionExpiration": {"NoncurrentDays": 1},
            },
            {
                "Id": "RemoveExpiredPrivateSourceDeleteMarkers",
                "Status": "Enabled",
                "Prefix": "private/owners/",
                "ExpiredObjectDeleteMarker": True,
            },
        ]
    }
    assert all(properties["PublicAccessBlockConfiguration"].values())
    assert properties["OwnershipControls"] == {
        "Rules": [{"ObjectOwnership": "BucketOwnerEnforced"}]
    }
    assert properties["CorsConfiguration"] == {
        "CorsRules": [
            {
                "AllowedHeaders": [
                    "content-type",
                    "x-amz-checksum-sha256",
                    "x-amz-server-side-encryption",
                ],
                "AllowedMethods": ["GET", "POST"],
                "AllowedOrigins": [{"Ref": "ApplicationOrigin"}],
                "ExposedHeaders": ["ETag", "x-amz-version-id"],
                "MaxAge": 300,
            }
        ]
    }
    statement = resources["PrivateArtifactBucketPolicy"]["Properties"]["PolicyDocument"][
        "Statement"
    ]
    assert [item["Sid"] for item in statement] == [
        "DenyInsecureTransport",
        "DenyStaleBrowserUploadSignatures",
        "DenyUnencryptedBrowserUploads",
    ]
    assert statement[0] == {
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
    upload_resource = {
        "Fn::Sub": ("${PrivateArtifactBucket.Arn}/private/owners/*/jobs/*/source/source.png")
    }
    assert statement[1] == {
        "Sid": "DenyStaleBrowserUploadSignatures",
        "Effect": "Deny",
        "Principal": "*",
        "Action": "s3:PutObject",
        "Resource": upload_resource,
        "Condition": {"NumericGreaterThan": {"s3:signatureAge": "300000"}},
    }
    assert statement[2] == {
        "Sid": "DenyUnencryptedBrowserUploads",
        "Effect": "Deny",
        "Principal": "*",
        "Action": "s3:PutObject",
        "Resource": upload_resource,
        "Condition": {"StringNotEquals": {"s3:x-amz-server-side-encryption": "AES256"}},
    }


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
    assert len(log_groups) == 15
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
        "SourceVersionRetentionFunction",
        "TerminalOperationalCleanupFunction",
        "StuckExecutionRecoveryFunction",
        "DispatcherFunction",
        "PreparationDispatchFunction",
        "ProviderDraftFunction",
        "SettlementFunction",
        "UploadApiFunction",
        "ReviewQueryApiFunction",
        "SellerCommandApiFunction",
    }
    roles = {resource["Properties"]["Role"]["Fn::GetAtt"][0] for resource in functions.values()}
    assert roles == {
        "SourceVersionRetentionFunctionRole",
        "TerminalOperationalCleanupFunctionRole",
        "StuckExecutionRecoveryFunctionRole",
        "DispatcherFunctionRole",
        "PreparationDispatchFunctionRole",
        "ProviderDraftFunctionRole",
        "SettlementFunctionRole",
        "UploadApiFunctionRole",
        "ReviewQueryApiFunctionRole",
        "SellerCommandApiFunctionRole",
    }
    assert (
        template["Globals"]["Function"]["Environment"]["Variables"][
            "MR_LISTER_PHASE6_SCAFFOLD_ONLY"
        ]
        == "true"
    )
    assert template["Outputs"]["DeploymentReadiness"]["Value"] == "SCAFFOLD_ONLY"
    assert all("ManagedPolicyArns" not in resources[role]["Properties"] for role in roles)
    assert functions["DispatcherFunction"]["Properties"]["Timeout"] == 120
    assert functions["PreparationDispatchFunction"]["Properties"]["Timeout"] == 600
    assert functions["ProviderDraftFunction"]["Properties"]["Timeout"] == 600
    assert functions["SettlementFunction"]["Properties"]["Timeout"] == 120
    assert functions["ReviewQueryApiFunction"]["Properties"]["MemorySize"] == 512
    assert functions["ReviewQueryApiFunction"]["Properties"]["Timeout"] == 30
    assert functions["SourceVersionRetentionFunction"]["Properties"]["Timeout"] == 300
    assert functions["TerminalOperationalCleanupFunction"]["Properties"]["Timeout"] == 300
    assert functions["StuckExecutionRecoveryFunction"]["Properties"]["Timeout"] == 120


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
    assert (
        '"Resource": [{"Ref": "AgentCoreRuntimeArn"}, {"Ref": "AgentCoreRuntimeEndpointArn"}]'
        in preparation
    )
    assert "secretsmanager" not in preparation
    assert "dynamodb:TransactWriteItems" not in preparation
    assert "s3:GetObject" not in preparation

    assert "secretsmanager:GetSecretValue" in provider
    assert '"Resource": {"Ref": "PrintifySecretArn"}' in provider
    assert "dynamodb:ConditionCheckItem" in provider
    assert "dynamodb:PutItem" in provider
    assert '"Action": "s3:GetObjectVersion"' in provider
    assert '"s3:GetObject"' not in provider
    assert "private/owners/*/jobs/*/source/source.png" in provider
    assert "bedrock-agentcore" not in provider

    assert "dynamodb:ConditionCheckItem" not in settlement
    assert "dynamodb:PutItem" in settlement
    assert "bedrock-agentcore" not in settlement
    assert "s3:GetObject" not in settlement
    assert "secretsmanager" not in settlement


def test_api_roles_are_capability_separated_and_have_no_direct_orchestration() -> None:
    resources = load_template()["Resources"]
    upload = serialized_policies(resources["UploadApiFunctionRole"])
    query = serialized_policies(resources["ReviewQueryApiFunctionRole"])
    command = serialized_policies(resources["SellerCommandApiFunctionRole"])

    assert "OwnerJobsIndex" not in upload
    assert "dynamodb:PutItem" in upload
    assert '"Action": ["s3:PutObject", "s3:PutObjectTagging"]' in upload
    assert '"Action": ["s3:GetObject", "s3:PutObjectVersionTagging"]' in upload
    assert "s3:GetObjectVersion" not in upload
    assert "s3:ListBucket" not in upload

    assert "OwnerJobsIndex" in query
    assert '"Action": ["dynamodb:GetItem", "dynamodb:Query"]' in query
    assert '"Action": "s3:GetObjectVersion"' in query
    assert "dynamodb:PutItem" not in query
    assert "dynamodb:TransactWriteItems" not in query
    assert "kms:" not in query

    assert "dynamodb:PutItem" in command
    assert "s3:" not in command

    for policy in (upload, query, command):
        assert "states:" not in policy
        assert "bedrock" not in policy
        assert "secretsmanager" not in policy
        assert "lambda:InvokeFunction" not in policy

    all_roles = json.dumps(
        {
            name: resource
            for name, resource in resources.items()
            if resource["Type"] == "AWS::IAM::Role"
        },
        sort_keys=True,
    )
    assert "dynamodb:TransactGetItems" not in all_roles
    assert "dynamodb:TransactWriteItems" not in all_roles


def test_exact_public_and_protected_http_routes_are_closed() -> None:
    resources = load_template()["Resources"]
    api_functions = {
        name: resources[name]
        for name in (
            "UploadApiFunction",
            "ReviewQueryApiFunction",
            "SellerCommandApiFunction",
        )
    }
    protected: dict[str, dict] = {}
    public: dict[str, dict] = {}
    for function in api_functions.values():
        for event in function["Properties"]["Events"].values():
            assert event["Type"] == "HttpApi"
            properties = event["Properties"]
            assert properties["ApiId"] == {"Ref": "SellerHttpApi"}
            assert properties["PayloadFormatVersion"] == "2.0"
            route_key = f"{properties['Method']} {properties['Path']}"
            if properties["Auth"] == {"Authorizer": "NONE"}:
                public[route_key] = properties
            else:
                assert properties["Auth"] == {
                    "Authorizer": "SellerJwtAuthorizer",
                    "AuthorizationScopes": ["mr-lister-api/seller"],
                }
                protected[route_key] = properties

    assert set(public) == {"GET /health"}
    assert set(protected) == {
        "POST /v1/uploads",
        "GET /v1/uploads/{upload_id}",
        "POST /v1/uploads/{upload_id}/authorize",
        "POST /v1/uploads/{upload_id}/complete",
        "POST /v1/uploads/{upload_id}/cancel",
        "GET /v1/jobs",
        "GET /v1/jobs/{job_id}",
        "GET /v1/jobs/{job_id}/review",
        "GET /v1/jobs/{job_id}/artwork-preview",
        "PUT /v1/jobs/{job_id}/review/listing",
        "POST /v1/jobs/{job_id}/economics/refresh",
        "POST /v1/jobs/{job_id}/approve",
        "POST /v1/jobs/{job_id}/cancel",
        "POST /v1/jobs/{job_id}/retry",
    }
    assert all("owner" not in route.casefold() for route in protected)
    assert all("report" not in route.casefold() for route in protected)


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

    assert module.UPLOAD_API_ROUTE_KEYS == {
        "POST /v1/uploads",
        "GET /v1/uploads/{upload_id}",
        "POST /v1/uploads/{upload_id}/authorize",
        "POST /v1/uploads/{upload_id}/complete",
        "POST /v1/uploads/{upload_id}/cancel",
    }
    assert module.REVIEW_QUERY_API_ROUTE_KEYS == {
        "GET /v1/jobs",
        "GET /v1/jobs/{job_id}",
        "GET /v1/jobs/{job_id}/review",
        "GET /v1/jobs/{job_id}/artwork-preview",
    }
    assert module.SELLER_COMMAND_API_ROUTE_KEYS == {
        "PUT /v1/jobs/{job_id}/review/listing",
        "POST /v1/jobs/{job_id}/economics/refresh",
        "POST /v1/jobs/{job_id}/approve",
        "POST /v1/jobs/{job_id}/cancel",
        "POST /v1/jobs/{job_id}/retry",
    }
    with pytest.raises(module.Phase6ScaffoldNotReady):
        module.upload_api_handler({"routeKey": "POST /v1/uploads"}, None)
    with pytest.raises(module.Phase6ScaffoldNotReady):
        module.review_query_api_handler({"routeKey": "GET /v1/jobs/{job_id}/review"}, None)
    with pytest.raises(module.Phase6ScaffoldNotReady):
        module.seller_command_api_handler({"routeKey": "POST /v1/jobs/{job_id}/approve"}, None)
    with pytest.raises(ValueError, match="Unsupported Phase 6 API route"):
        module.seller_command_api_handler({"routeKey": "POST /v1/jobs/job_1/export"}, None)

    health = module.review_query_api_handler({"routeKey": "GET /health"}, None)
    assert health == {
        "statusCode": 503,
        "headers": {"Cache-Control": "no-store", "Content-Type": "application/json"},
        "body": '{"status":"scaffold_only"}',
    }


def test_no_forbidden_external_capability_is_present() -> None:
    deployment_parts: list[str] = []
    for path in sorted(PHASE6.rglob("*")):
        if (
            not path.is_file()
            or path.name == "README.md"
            or path.suffix not in {".json", ".py", ".txt"}
        ):
            continue
        if path == TEMPLATE:
            template = load_template()
            cloudfront_functions = [
                resource
                for resource in template["Resources"].values()
                if resource["Type"] == "AWS::CloudFront::Function"
            ]
            assert cloudfront_functions
            for resource in cloudfront_functions:
                assert resource["Properties"].pop("AutoPublish") is True
            deployment_parts.append(json.dumps(template, sort_keys=True))
        else:
            deployment_parts.append(path.read_text(encoding="utf-8"))
    deployment_text = "\n".join(deployment_parts).lower()
    # Operational alarm delivery and activation of the checked CloudFront viewer functions are
    # the only non-marketplace uses of this verb.
    deployment_text = deployment_text.replace('"sns:publish"', "")
    deployment_text = deployment_text.replace('"cloudfront:publishfunction"', "")
    for forbidden in (
        "waitfortasktoken",
        "sendtasksuccess",
        "sendtaskfailure",
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
