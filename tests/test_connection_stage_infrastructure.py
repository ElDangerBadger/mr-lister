"""Local structural proof of additive, disabled, least-capability connection infrastructure."""

from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime

import pytest

from tools.prepare_connection_stage_infrastructure import (
    APPLICATION_HANDLERS,
    INDEX,
    ROUTES,
    TemplateResolver,
    build_template,
    environment_size,
    patch_application_template,
    patch_runtime_role_template,
    template_digest,
    validate_existing_state,
)

NOW = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)
ACCOUNT = "123456789012"


def capture():
    return {
        "captured_at": "2026-10-05T12:00:00Z",
        "region": "us-west-2",
        "account_id": ACCOUNT,
        "environment_name": "dev",
        "user_pool_id": "us-west-2_Abc123",
        "client_id": "primaryclient123",
        "seller_api_id": "abcdefghij",
        "seller_authorizer_id": "abc123",
        "authorizer": {
            "type": "JWT",
            "issuer": "https://cognito-idp.us-west-2.amazonaws.com/us-west-2_Abc123",
            "audience": ["primaryclient123"],
        },
        "account_table_arn": f"arn:aws:dynamodb:us-west-2:{ACCOUNT}:table/mr-lister-account-dev",
        "reserved_owner_ids": ["b" * 64, "a" * 64],
        "preexisting_user_count": 2,
        "route_keys": ["GET /v1/account", "GET /v1/jobs"],
        "group_names": ["account", "seller", "judge"],
        "code": {
            "bucket": "mr-lister-versioned-code",
            "key": "sealed/lambda.zip",
            "version": "immutable-version-123",
        },
    }


def application():
    state = capture()
    template = {
        "Parameters": {"ExistingRelease": {"Type": "String"}},
        "Globals": {
            "Function": {
                "Environment": {
                    "Variables": {"MR_LISTER_RELEASE_FINGERPRINT": {"Ref": "ExistingRelease"}}
                }
            }
        },
        "Resources": {
            "UserPoolUntouched": {
                "Type": "AWS::Cognito::UserPool",
                "Properties": {"MfaConfiguration": "ON", "EnabledMfas": ["SOFTWARE_TOKEN_MFA"]},
            },
            "ClientUntouched": {
                "Type": "AWS::Cognito::UserPoolClient",
                "Properties": {"SupportedIdentityProviders": ["COGNITO", "MrListerJudge"]},
            },
            "ApiUntouched": {
                "Type": "AWS::Serverless::HttpApi",
                "Properties": {
                    "CorsConfiguration": {"AllowOrigins": ["https://seller.example.invalid"]}
                },
            },
            "CleanupUntouched": {
                "Type": "AWS::Lambda::Function",
                "Properties": {
                    "Handler": "phase6_lambda.terminal_operational_cleanup_handler",
                    "Timeout": 45,
                },
            },
        },
    }
    state["parameters"] = {"ExistingRelease": "d" * 64}
    state["references"] = {}
    state["functions"] = {}
    for index, handler in enumerate(APPLICATION_HANDLERS):
        role, function = f"CapturedRole{index}", f"CapturedHandler{index}"
        role_arn = f"arn:aws:iam::{ACCOUNT}:role/mr-lister-existing-{index}"
        state["references"][role + ".Arn"] = role_arn
        original_policy = {
            "PolicyName": "old-exact-capabilities",
            "PolicyDocument": {
                "Version": "2012-10-17",
                "Statement": [
                    {
                        "Effect": "Allow",
                        "Action": ["secretsmanager:GetSecretValue"]
                        if APPLICATION_HANDLERS[handler] == "provider"
                        else ["dynamodb:GetItem"],
                        "Resource": (
                            "arn:aws:secretsmanager:us-west-2:123456789012:"
                            "secret:old-singleton-AbCd12"
                        )
                        if APPLICATION_HANDLERS[handler] == "provider"
                        else "arn:aws:dynamodb:us-west-2:123456789012:table/old-table",
                    }
                ],
            },
        }
        template["Resources"][role] = {
            "Type": "AWS::IAM::Role",
            "Properties": {
                "AssumeRolePolicyDocument": {
                    "Version": "2012-10-17",
                    "Statement": [
                        {
                            "Effect": "Allow",
                            "Action": "sts:AssumeRole",
                            "Principal": {"Service": "lambda.amazonaws.com"},
                        }
                    ],
                },
                "PermissionsBoundary": "arn:aws:iam::123456789012:policy/existing-boundary",
                "Policies": [original_policy],
            },
        }
        environment = {"MR_LISTER_EXISTING_SETTING": "unchanged"}
        if (
            handler.endswith(
                ("upload_api_handler", "review_query_api_handler", "seller_command_api_handler")
            )
            or "phase718" in handler
        ):
            environment.update(
                MR_LISTER_COGNITO_ISSUER="https://cognito-idp.us-west-2.amazonaws.com/us-west-2_Abc123",
                MR_LISTER_COGNITO_CLIENT_ID="primaryclient123",
            )
        template["Resources"][function] = {
            "Type": "AWS::Serverless::Function",
            "Properties": {
                "Handler": handler,
                "FunctionName": f"mr-lister-existing-{index}",
                "Timeout": 31,
                "MemorySize": 768,
                "ReservedConcurrentExecutions": 2,
                "CodeUri": {"Bucket": "old-bucket", "Key": "old-key", "Version": "old-version"},
                "Role": {"Fn::GetAtt": [role, "Arn"]},
                "Environment": {"Variables": environment},
            },
        }
        state["functions"][function] = {
            "function_name": f"mr-lister-existing-{index}",
            "role_arn": role_arn,
            "environment": {"MR_LISTER_RELEASE_FINGERPRINT": "d" * 64, **environment},
        }
    state["application_template_sha256"] = template_digest(template)
    return template, state


def statements(resource):
    return resource["Properties"]["Policies"][0]["PolicyDocument"]["Statement"]


def actions(resource):
    return {value for item in statements(resource) for value in item["Action"]}


def test_separate_stack_is_disabled_and_retains_encrypted_indexed_table_without_ttl():
    template = build_template()
    for name in ("ConnectionEnabled", "WorkflowEnabled", "CleanupEnabled"):
        assert template["Parameters"][name]["Default"] == "false"
    table = template["Resources"]["ConnectionTable"]
    assert table["DeletionPolicy"] == table["UpdateReplacePolicy"] == "Retain"
    props = table["Properties"]
    assert props["KeySchema"] == [{"AttributeName": "PK", "KeyType": "HASH"}]
    assert props["DeletionProtectionEnabled"] is True
    assert props["SSESpecification"] == {"SSEEnabled": True}
    assert props["PointInTimeRecoverySpecification"] == {"PointInTimeRecoveryEnabled": True}
    assert "TimeToLiveSpecification" not in props
    index = props["GlobalSecondaryIndexes"][0]
    assert index["IndexName"] == INDEX
    assert index["Projection"] == {"ProjectionType": "KEYS_ONLY"}
    assert index["KeySchema"] == [
        {"AttributeName": "cleanup_partition", "KeyType": "HASH"},
        {"AttributeName": "cleanup_due", "KeyType": "RANGE"},
    ]


def test_each_route_has_physical_function_exact_jwt_scope_and_source_account_permission():
    resources = build_template()["Resources"]
    for kind, (method, path, handler) in ROUTES.items():
        name = "Connection" + kind
        function = resources[name + "Function"]["Properties"]
        assert function["Handler"] == "mr_lister.connections.entrypoint." + handler
        assert function["Code"]["S3ObjectVersion"] == {"Ref": "CodeVersion"}
        route = resources[name + "Route"]["Properties"]
        assert route["RouteKey"] == method + " " + path
        assert route["AuthorizationType"] == "JWT"
        assert route["AuthorizerId"] == {"Ref": "ExistingSellerAuthorizerId"}
        assert route["AuthorizationScopes"] == ["mr-lister-api/seller"]
        permission = resources[name + "Invoke"]["Properties"]
        assert permission["SourceAccount"] == {"Ref": "AWS::AccountId"}
        assert permission["SourceArn"]["Fn::Sub"].endswith("/*/" + method + path)
    assert sum(item["Type"] == "AWS::Lambda::Function" for item in resources.values()) == 5


def test_roles_have_only_required_separate_capabilities_and_owned_secret_namespace():
    resources = build_template()["Resources"]
    logs = {"logs:CreateLogStream", "logs:PutLogEvents"}
    assert actions(resources["ConnectionQueryRole"]) == logs | {"dynamodb:GetItem"}
    assert actions(resources["ConnectionValidateRole"]) == logs | {
        "dynamodb:GetItem",
        "dynamodb:PutItem",
        "dynamodb:ConditionCheckItem",
        "secretsmanager:CreateSecret",
        "secretsmanager:GetSecretValue",
    }
    assert actions(resources["ConnectionActivateRole"]) == logs | {
        "dynamodb:GetItem",
        "dynamodb:PutItem",
        "dynamodb:ConditionCheckItem",
        "cognito-idp:AdminAddUserToGroup",
    }
    assert actions(resources["ConnectionSelectRole"]) == actions(
        resources["ConnectionActivateRole"]
    ) | {"secretsmanager:GetSecretValue"}
    assert actions(resources["ConnectionCleanupRole"]) == logs | {
        "dynamodb:GetItem",
        "dynamodb:PutItem",
        "dynamodb:ConditionCheckItem",
        "dynamodb:Query",
        "secretsmanager:DeleteSecret",
    }
    for kind in (*ROUTES, "Cleanup"):
        for item in statements(resources["Connection" + kind + "Role"]):
            assert item["Resource"] != "*"
            if any(action.startswith("secretsmanager:") for action in item["Action"]):
                pattern = item["Resource"]["Fn::Sub"]
                assert "/connections/" + "?" * 64 + "/candidate_" + "?" * 32 + "-??????" in pattern
                assert "*" not in pattern
    all_actions = set().union(
        *(actions(resources["Connection" + kind + "Role"]) for kind in (*ROUTES, "Cleanup"))
    )
    assert (
        not {
            "dynamodb:Scan",
            "secretsmanager:ListSecrets",
            "dynamodb:UpdateTimeToLive",
            "secretsmanager:PutSecretValue",
        }
        & all_actions
    )


def test_cleanup_schedule_and_delivery_role_are_exact_and_disabled():
    resources = build_template()["Resources"]
    schedule = resources["ConnectionCleanupSchedule"]["Properties"]
    assert schedule["ScheduleExpression"] == "rate(1 minute)"
    assert schedule["State"] == {"Fn::If": ["RunConnectionCleanup", "ENABLED", "DISABLED"]}
    assert schedule["Target"]["RetryPolicy"] == {
        "MaximumEventAgeInSeconds": 60,
        "MaximumRetryAttempts": 0,
    }
    delivery = resources["ConnectionCleanupDeliveryRole"]
    trust = delivery["Properties"]["AssumeRolePolicyDocument"]["Statement"][0]
    assert trust["Condition"]["StringEquals"] == {"aws:SourceAccount": {"Ref": "AWS::AccountId"}}
    assert trust["Condition"]["ArnEquals"] == {
        "aws:SourceArn": {"Fn::GetAtt": ["ConnectionCleanupScheduleGroup", "Arn"]}
    }
    assert statements(delivery) == [
        {
            "Effect": "Allow",
            "Action": ["lambda:InvokeFunction"],
            "Resource": {"Fn::GetAtt": ["ConnectionCleanupFunction", "Arn"]},
        }
    ]


def test_fresh_capture_resolves_exact_bindings_and_complete_environments():
    values = validate_existing_state(capture(), now=NOW)
    assert values["ReservedOwnerIds"] == '["' + "a" * 64 + '","' + "b" * 64 + '"]'
    assert (
        values["ConnectionEnabled"]
        == values["WorkflowEnabled"]
        == values["CleanupEnabled"]
        == "false"
    )
    forty = capture()
    forty["reserved_owner_ids"] = [f"{i:064x}" for i in range(1, 41)]
    forty["preexisting_user_count"] = 40
    assert validate_existing_state(forty, now=NOW)


@pytest.mark.parametrize(
    "mutation",
    [
        "stale",
        "future",
        "foreignaccount",
        "foreignregion",
        "foreign_table",
        "incomplete",
        "duplicate",
        "overflow",
        "unversioned",
        "existingroute",
        "nogroup",
    ],
)
def test_invalid_capture_cannot_prepare_bindings(mutation):
    state = capture()
    if mutation == "stale":
        state["captured_at"] = "2026-10-05T11:44:59Z"
    if mutation == "future":
        state["captured_at"] = "2026-10-05T12:00:01Z"
    if mutation == "foreignaccount":
        state["account_id"] = "999999999999"
    if mutation == "foreignregion":
        state["region"] = "us-east-1"
    if mutation == "foreign_table":
        state["account_table_arn"] += "-other"
    if mutation == "incomplete":
        state["preexisting_user_count"] = 3
    if mutation == "duplicate":
        state["reserved_owner_ids"] = ["a" * 64, "a" * 64]
    if mutation == "overflow":
        state["reserved_owner_ids"] = [f"{i:064x}" for i in range(1, 42)]
        state["preexisting_user_count"] = 41
    if mutation == "unversioned":
        state["code"]["version"] = "null"
    if mutation == "existingroute":
        state["route_keys"].append("GET /v1/store-setup")
    if mutation == "nogroup":
        state["group_names"].remove("account")
    with pytest.raises(ValueError):
        validate_existing_state(state, now=NOW)


def test_application_patch_preserves_all_existing_authority_and_only_adds_bound_capabilities():
    original, state = application()
    before = deepcopy(original)
    patched = patch_application_template(original, state, now=NOW)
    assert original == before
    for name in ("UserPoolUntouched", "ClientUntouched", "ApiUntouched", "CleanupUntouched"):
        assert patched["Resources"][name] == original["Resources"][name]
    assert patched["Globals"] == original["Globals"]
    for index, handler in enumerate(APPLICATION_HANDLERS):
        function, role = f"CapturedHandler{index}", f"CapturedRole{index}"
        old_props = original["Resources"][function]["Properties"]
        new_props = patched["Resources"][function]["Properties"]
        assert {k: v for k, v in new_props.items() if k != "Environment"} == {
            k: v for k, v in old_props.items() if k != "Environment"
        }
        old_role = original["Resources"][role]["Properties"]
        new_role = patched["Resources"][role]["Properties"]
        assert new_role["Policies"][:-1] == old_role["Policies"]
        assert {k: v for k, v in new_role.items() if k != "Policies"} == {
            k: v for k, v in old_role.items() if k != "Policies"
        }
        condition, policy, disabled = new_role["Policies"][-1]["Fn::If"]
        assert condition == "UseStoreConnections"
        assert disabled == {"Ref": "AWS::NoValue"}
        grants = {
            action for item in policy["PolicyDocument"]["Statement"] for action in item["Action"]
        }
        if APPLICATION_HANDLERS[handler] == "provider":
            assert "secretsmanager:GetSecretValue" in grants
            assert (
                new_props["Environment"]["Variables"]["MR_LISTER_LEGACY_OWNER_IDS"]
                == '["' + "a" * 64 + '","' + "b" * 64 + '"]'
            )
        else:
            assert not any(value.startswith("secretsmanager:") for value in grants)
        assert not {"dynamodb:PutItem", "dynamodb:UpdateItem", "dynamodb:DeleteItem"} & grants


@pytest.mark.parametrize(
    "change", ["template", "environment", "role", "function", "missing", "shared", "budget"]
)
def test_application_patch_rejects_drift_and_complete_environment_overflow(change):
    baseline, state = application()
    key = "CapturedHandler0"
    if change == "template":
        baseline["Description"] = "changed"
    if change == "environment":
        state["functions"][key]["environment"]["UNKNOWN"] = "changed"
    if change == "role":
        state["functions"][key]["role_arn"] += "-other"
    if change == "function":
        state["functions"][key]["function_name"] += "-other"
    if change == "missing":
        state["functions"].pop(key)
    if change == "shared":
        baseline["Resources"]["UnrelatedShared"] = deepcopy(baseline["Resources"][key])
        baseline["Resources"]["UnrelatedShared"]["Properties"]["Handler"] = "unrelated.handler"
        state["application_template_sha256"] = template_digest(baseline)
    if change == "budget":
        baseline["Resources"][key]["Properties"]["Environment"]["Variables"]["LARGE"] = "x" * 3700
        state["functions"][key]["environment"]["LARGE"] = "x" * 3700
        state["application_template_sha256"] = template_digest(baseline)
    with pytest.raises(ValueError):
        patch_application_template(baseline, state, now=NOW)


def test_runtime_patch_preserves_inference_and_trust_and_adds_no_secret_action():
    state = capture()
    role_arn = f"arn:aws:iam::{ACCOUNT}:role/existing-runtime"
    baseline = {
        "Resources": {
            "ExactRuntimeRole": {
                "Type": "AWS::IAM::Role",
                "Properties": {
                    "AssumeRolePolicyDocument": {
                        "Statement": [
                            {
                                "Principal": {"Service": "bedrock-agentcore.amazonaws.com"},
                                "Action": "sts:AssumeRole",
                            }
                        ]
                    },
                    "Policies": [
                        {
                            "PolicyName": "inference",
                            "PolicyDocument": {
                                "Statement": [
                                    {
                                        "Action": ["bedrock:InvokeModel"],
                                        "Resource": "exact-existing-model",
                                    }
                                ]
                            },
                        }
                    ],
                },
            }
        }
    }
    state.update(
        runtime_template_sha256=template_digest(baseline),
        runtime_role_arn=role_arn,
        references={"ExactRuntimeRole.Arn": role_arn},
    )
    patched = patch_runtime_role_template(
        baseline, state, role_logical_id="ExactRuntimeRole", now=NOW
    )
    props = patched["Resources"]["ExactRuntimeRole"]["Properties"]
    assert (
        props["Policies"][:-1]
        == baseline["Resources"]["ExactRuntimeRole"]["Properties"]["Policies"]
    )
    assert props["Policies"][-1]["Fn::If"][1]["PolicyDocument"]["Statement"][0]["Action"] == [
        "dynamodb:GetItem",
        "dynamodb:ConditionCheckItem",
    ]
    assert (
        props["AssumeRolePolicyDocument"]
        == baseline["Resources"]["ExactRuntimeRole"]["Properties"]["AssumeRolePolicyDocument"]
    )


def test_environment_byte_budget_is_not_character_count_and_unknown_intrinsic_fails():
    with pytest.raises(ValueError):
        environment_size({"KEY": "é" * 2047})
    resolver = TemplateResolver({}, capture())
    with pytest.raises(ValueError):
        resolver.resolve({"Fn::ImportValue": "unknown"})
    with pytest.raises(ValueError):
        resolver.resolve({"Ref": "missing"})


@pytest.mark.parametrize(
    "key,value",
    [
        ("account_id", 123),
        ("environment_name", []),
        ("code", []),
        ("reserved_owner_ids", [{}]),
        ("group_names", ["account", "seller", {}]),
        ("route_keys", [{}]),
        ("user_pool_id", None),
    ],
)
def test_malformed_nested_capture_is_rejected_without_type_errors(key, value):
    state = capture()
    state[key] = value
    with pytest.raises(ValueError):
        validate_existing_state(state, now=NOW)


def test_archive_key_length_is_validated_before_rendering():
    state = capture()
    state["code"]["key"] = "a" * 1025
    with pytest.raises(ValueError):
        validate_existing_state(state, now=NOW)


def test_disabled_application_has_no_empty_or_added_policies():
    baseline, state = application()
    patched = patch_application_template(baseline, state, now=NOW)
    resolver = TemplateResolver(patched, state)
    for index in range(len(APPLICATION_HANDLERS)):
        role = f"CapturedRole{index}"
        assert (
            resolver.resolve(patched["Resources"][role]["Properties"]["Policies"])
            == (baseline["Resources"][role]["Properties"]["Policies"])
        )


def test_patch_does_not_overwrite_a_preexisting_rule_with_the_same_name():
    baseline, state = application()
    baseline["Rules"] = {"WorkflowRequiresStoreConnections": {"Assertions": []}}
    state["application_template_sha256"] = template_digest(baseline)
    with pytest.raises(ValueError):
        patch_application_template(baseline, state, now=NOW)


@pytest.mark.parametrize("role_form", ["literal", "sub", "getatt_string", "global"])
def test_application_rejects_shared_resolved_role_even_with_different_expression(role_form):
    baseline, state = application()
    role_arn = state["functions"]["CapturedHandler0"]["role_arn"]
    role = {
        "literal": role_arn,
        "sub": {"Fn::Sub": "arn:aws:iam::${AWS::AccountId}:role/mr-lister-existing-0"},
        "getatt_string": {"Fn::GetAtt": "CapturedRole0.Arn"},
        "global": None,
    }[role_form]
    extra = {"Type": "AWS::Serverless::Function", "Properties": {"Handler": "other.handler"}}
    if role is not None:
        extra["Properties"]["Role"] = role
    else:
        baseline["Globals"]["Function"]["Role"] = role_arn
    baseline["Resources"]["OtherFunction"] = extra
    state["application_template_sha256"] = template_digest(baseline)
    with pytest.raises(ValueError, match="Shared execution roles"):
        patch_application_template(baseline, state, now=NOW)


@pytest.mark.parametrize("group", ["phase6", "publication"])
def test_separate_real_stack_groups_can_be_patched_without_inventing_combined_stack(group):
    baseline, state = application()
    selected_handlers = {
        handler
        for handler in APPLICATION_HANDLERS
        if handler.startswith("phase6_lambda.") == (group == "phase6")
    }
    for index, handler in enumerate(APPLICATION_HANDLERS):
        if handler not in selected_handlers:
            baseline["Resources"].pop(f"CapturedHandler{index}")
            baseline["Resources"].pop(f"CapturedRole{index}")
            state["functions"].pop(f"CapturedHandler{index}")
    state["application_template_sha256"] = template_digest(baseline)
    patched = patch_application_template(baseline, state, now=NOW)
    assert set(patched["Resources"]) == set(baseline["Resources"])
    assert len(state["functions"]) == (8 if group == "phase6" else 3)
    # A missing role's function cannot turn this into an accepted partial-stack capture.
    name = next(iter(state["functions"]))
    baseline["Resources"].pop(name)
    state["functions"].pop(name)
    state["application_template_sha256"] = template_digest(baseline)
    with pytest.raises(ValueError):
        patch_application_template(baseline, state, now=NOW)


@pytest.mark.parametrize(
    "field,value",
    [
        ("type", "REQUEST"),
        ("issuer", "https://other.example.invalid/pool"),
        ("audience", ["primaryclient123", "other-client"]),
    ],
)
def test_captured_authorizer_must_match_exact_primary_pool_client(field, value):
    state = capture()
    state["authorizer"][field] = value
    with pytest.raises(ValueError, match="exact primary issuer/client"):
        validate_existing_state(state, now=NOW)
