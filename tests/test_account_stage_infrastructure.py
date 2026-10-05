"""Native account setup cannot inherit the owner's store, seller role, or judge access."""

import json
import re
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from tools.prepare_account_stage_infrastructure import (
    ACCOUNT_GROUP,
    ACCOUNT_SCOPE,
    build_template,
    patch_application_template,
    validate_existing_state,
    write_private,
)
from tools.prepare_judge_application_update import prepare_application_template
from tools.prepare_judge_session_infrastructure import (
    patch_application_template as patch_judge_edge,
)

NOW = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)


def baseline():
    return json.loads((Path(__file__).parents[1] / "infra/phase6/template.json").read_text())


def capture():
    return {
        "captured_at": "2026-10-05T12:00:00Z",
        "user_pool_id": "us-west-2_Abc123",
        "client_id": "a" * 26,
        "seller_api_id": "abcdefghij",
        "seller_authorizer_id": "abc123",
        "group_names": ["seller", "judge"],
        "route_keys": ["GET /v1/jobs", "GET /v1/jobs/{job_id}", "POST /v1/judge-session/redeem"],
        "reserved_owner_ids": ["b" * 64, "a" * 64],
        "preexisting_user_count": 2,
    }


def actions(resource):
    policy = resource["Properties"]["Policies"][0]["PolicyDocument"]
    return {action for statement in policy["Statement"] for action in statement["Action"]}


def test_isolated_handlers_and_signup_default_disabled():
    services = build_template()
    for flag in ("AccountProvisionEnabled", "AccountQueryEnabled"):
        assert services["Parameters"][flag] == {
            "Type": "String",
            "Default": "false",
            "AllowedValues": ["false", "true"],
        }
    application = patch_application_template(baseline())
    for flag in ("AccountPostConfirmationEnabled", "SelfServiceSignupEnabled"):
        assert application["Parameters"][flag]["Default"] == "false"
    pool = application["Resources"]["SellerUserPool"]["Properties"]
    assert pool["AdminCreateUserConfig"]["AllowAdminCreateUserOnly"] == {
        "Fn::If": ["AllowSelfServiceSignup", False, True]
    }
    assert set(application["Parameters"]) - set(baseline()["Parameters"]) == {
        "AccountProvisionFunctionArn",
        "AccountPostConfirmationEnabled",
        "SelfServiceSignupEnabled",
    }
    assert application["Conditions"]["UseAccountPostConfirmation"] == {
        "Fn::Equals": [{"Ref": "AccountPostConfirmationEnabled"}, "true"]
    }
    for hook in ("PostConfirmation", "PreAuthentication"):
        assert pool["LambdaConfig"][hook] == {
            "Fn::If": [
                "UseAccountPostConfirmation",
                {"Ref": "AccountProvisionFunctionArn"},
                {"Ref": "AWS::NoValue"},
            ]
        }
    assertion = application["Rules"]["AccountSignupRequiresProvisionHook"]["Assertions"][0][
        "Assert"
    ]
    assert assertion == {
        "Fn::Or": [
            {"Fn::Equals": [{"Ref": "SelfServiceSignupEnabled"}, "false"]},
            {"Fn::Equals": [{"Ref": "AccountPostConfirmationEnabled"}, "true"]},
        ]
    }


def test_account_table_keeps_only_identity_partition_and_is_retained():
    table = build_template()["Resources"]["AccountTable"]
    assert table["DeletionPolicy"] == table["UpdateReplacePolicy"] == "Retain"
    properties = table["Properties"]
    assert properties["KeySchema"] == [{"AttributeName": "PK", "KeyType": "HASH"}]
    assert properties["AttributeDefinitions"] == [{"AttributeName": "PK", "AttributeType": "S"}]
    assert properties["BillingMode"] == "PAY_PER_REQUEST"
    assert properties["SSESpecification"] == {"SSEEnabled": True}
    assert properties["PointInTimeRecoverySpecification"] == {"PointInTimeRecoveryEnabled": True}
    assert properties["DeletionProtectionEnabled"] is True
    assert "GlobalSecondaryIndexes" not in properties
    assert "TimeToLiveSpecification" not in properties


def test_account_group_has_no_iam_role_or_seller_membership():
    group = build_template()["Resources"]["AccountGroup"]["Properties"]
    assert group["GroupName"] == ACCOUNT_GROUP == "account"
    assert group["UserPoolId"] == {"Ref": "ExistingUserPoolId"}
    assert "RoleArn" not in group and "Precedence" not in group
    assert not any(
        resource["Type"] == "AWS::Cognito::UserPoolUserToGroupAttachment"
        for resource in build_template()["Resources"].values()
    )


@pytest.mark.parametrize("version", ["null", "NULL", "latest", "Latest", "current", "", " "])
def test_account_code_requires_a_real_immutable_object_version(version):
    constraint = build_template()["Parameters"]["CodeVersion"]
    assert re.fullmatch(constraint["AllowedPattern"], version) is None
    assert re.fullmatch(constraint["AllowedPattern"], "3HL4kqtJlcpXroDTDmJ+rmSpXd3dIbrHY+M")


def test_query_role_is_readonly_and_provision_cannot_change_existing_accounts_or_credentials():
    resources = build_template()["Resources"]
    log_actions = {"logs:CreateLogStream", "logs:PutLogEvents"}
    assert actions(resources["AccountQueryRole"]) == log_actions | {"dynamodb:GetItem"}
    assert actions(resources["AccountProvisionRole"]) == log_actions | {
        "dynamodb:GetItem",
        "dynamodb:PutItem",
        "cognito-idp:AdminGetUser",
        "cognito-idp:AdminAddUserToGroup",
    }
    for kind in ("Query", "Provision"):
        statements = resources[f"Account{kind}Role"]["Properties"]["Policies"][0]["PolicyDocument"][
            "Statement"
        ]
        assert all(statement["Resource"] != "*" for statement in statements)
        table = next(
            statement for statement in statements if "dynamodb:GetItem" in statement["Action"]
        )
        assert table["Resource"] == {"Fn::GetAtt": ["AccountTable", "Arn"]}
    provision = resources["AccountProvisionRole"]["Properties"]["Policies"][0]["PolicyDocument"][
        "Statement"
    ]
    group = next(
        statement
        for statement in provision
        if "cognito-idp:AdminAddUserToGroup" in statement["Action"]
    )
    assert set(group["Action"]) == {
        "cognito-idp:AdminGetUser",
        "cognito-idp:AdminAddUserToGroup",
    }
    assert group["Resource"] == {
        "Fn::Sub": (
            "arn:${AWS::Partition}:cognito-idp:${AWS::Region}:${AWS::AccountId}:"
            "userpool/${ExistingUserPoolId}"
        )
    }


def test_handlers_have_secret_free_exact_environment_and_pinned_code():
    resources = build_template()["Resources"]
    for kind in ("Query", "Provision"):
        properties = resources[f"Account{kind}Function"]["Properties"]
        assert (
            properties["Handler"] == f"mr_lister.accounts.{kind.lower()}_entrypoint.lambda_handler"
        )
        assert properties["Code"] == {
            "S3Bucket": {"Ref": "CodeBucket"},
            "S3Key": {"Ref": "CodeKey"},
            "S3ObjectVersion": {"Ref": "CodeVersion"},
        }
        assert properties["Environment"]["Variables"] == {
            "MR_LISTER_ACCOUNT_USER_POOL_ID": {"Ref": "ExistingUserPoolId"},
            "MR_LISTER_ACCOUNT_CLIENT_ID": {"Ref": "ExistingClientId"},
            "MR_LISTER_ACCOUNT_TABLE_NAME": {"Ref": "AccountTable"},
            "MR_LISTER_ACCOUNT_RESERVED_OWNER_IDS": {"Ref": "ReservedOwnerIds"},
            f"MR_LISTER_ACCOUNT_{kind.upper()}_ENABLED": {"Ref": f"Account{kind}Enabled"},
        }
        assert properties["Timeout"] <= 10
    assert not any("Secret" in resource["Type"] for resource in resources.values())
    assert all(
        not action.startswith(("secretsmanager:", "s3:", "bedrock", "iam:", "states:"))
        for kind in ("Query", "Provision")
        for action in actions(resources[f"Account{kind}Role"])
    )


def test_only_exact_account_read_route_uses_existing_jwt_authorizer_and_scope():
    resources = build_template()["Resources"]
    routes = [
        resource["Properties"]
        for resource in resources.values()
        if resource["Type"] == "AWS::ApiGatewayV2::Route"
    ]
    assert len(routes) == 1
    route = routes[0]
    assert route["RouteKey"] == "GET /v1/account"
    assert route["ApiId"] == {"Ref": "ExistingSellerApiId"}
    assert route["AuthorizerId"] == {"Ref": "ExistingSellerAuthorizerId"}
    assert route["AuthorizationType"] == "JWT"
    assert route["AuthorizationScopes"] == [ACCOUNT_SCOPE]
    assert not any(
        resource["Type"]
        in {"AWS::ApiGatewayV2::Api", "AWS::ApiGatewayV2::Authorizer", "AWS::ApiGatewayV2::Stage"}
        for resource in resources.values()
    )


def test_both_invoke_grants_require_source_account_and_exact_source():
    resources = build_template()["Resources"]
    query = resources["AccountQueryInvoke"]["Properties"]
    assert query["Principal"] == "apigateway.amazonaws.com"
    assert query["SourceAccount"] == {"Ref": "AWS::AccountId"}
    assert query["SourceArn"] == {
        "Fn::Sub": (
            "arn:${AWS::Partition}:execute-api:${AWS::Region}:${AWS::AccountId}:"
            "${ExistingSellerApiId}/*/GET/v1/account"
        )
    }
    cognito_grants = [
        resource["Properties"]
        for resource in resources.values()
        if resource["Type"] == "AWS::Lambda::Permission"
        and resource["Properties"]["Principal"] == "cognito-idp.amazonaws.com"
    ]
    provision = resources["AccountProvisionInvoke"]["Properties"]
    assert cognito_grants == [provision]
    assert provision["FunctionName"] == {"Ref": "AccountProvisionFunction"}
    assert provision["Principal"] == "cognito-idp.amazonaws.com"
    assert provision["SourceAccount"] == {"Ref": "AWS::AccountId"}
    assert provision["SourceArn"] == {
        "Fn::Sub": (
            "arn:${AWS::Partition}:cognito-idp:${AWS::Region}:${AWS::AccountId}:"
            "userpool/${ExistingUserPoolId}"
        )
    }


@pytest.mark.parametrize("with_judge", [False, True])
def test_patch_preserves_owner_client_mfa_api_and_judge_resources_byte_for_byte(with_judge):
    before = baseline()
    if with_judge:
        before = prepare_application_template(before, origin="https://massskutiny.com")
        before = patch_judge_edge(
            before, session_api_hostname="abcdefghij.execute-api.us-west-2.amazonaws.com"
        )
    after = patch_application_template(before)
    assert set(after["Resources"]) == set(before["Resources"])
    for name, resource in before["Resources"].items():
        if name not in {"SellerUserPool", "SellerSpaRouteFunction"}:
            assert after["Resources"][name] == resource
    assert after["Resources"]["SellerUserPoolClient"] == before["Resources"]["SellerUserPoolClient"]
    after_pool = deepcopy(after["Resources"]["SellerUserPool"])
    after_pool["Properties"]["AdminCreateUserConfig"] = before["Resources"]["SellerUserPool"][
        "Properties"
    ]["AdminCreateUserConfig"]
    for hook in ("PostConfirmation", "PreAuthentication"):
        del after_pool["Properties"]["LambdaConfig"][hook]
    if not after_pool["Properties"]["LambdaConfig"]:
        del after_pool["Properties"]["LambdaConfig"]
    assert after_pool == before["Resources"]["SellerUserPool"]
    code = after["Resources"]["SellerSpaRouteFunction"]["Properties"]["FunctionCode"]
    assert code.count("uri === '/store-setup'") == 1
    for path in ("/privacy", "/terms", "/judge/privacy", "/judge/terms"):
        assert code.count(f"uri === '{path}'") == 1
    assert (
        code.replace(
            "\n  isSpaRoute = isSpaRoute || uri === '/store-setup' || uri === '/privacy' || "
            "uri === '/terms' || uri === '/judge/privacy' || uri === '/judge/terms';",
            "",
        )
        == before["Resources"]["SellerSpaRouteFunction"]["Properties"]["FunctionCode"]
    )
    assert baseline() == json.loads(
        (Path(__file__).parents[1] / "infra/phase6/template.json").read_text()
    )


def test_patch_preserves_other_cognito_hooks_and_invite_configuration():
    before = baseline()
    pool = before["Resources"]["SellerUserPool"]["Properties"]
    pool["LambdaConfig"] = {
        "CustomMessage": "arn:aws:lambda:us-west-2:123456789012:function:messages"
    }
    pool["AdminCreateUserConfig"]["InviteMessageTemplate"] = {
        "EmailMessage": "Existing message {username} {####}"
    }
    after = patch_application_template(before)["Resources"]["SellerUserPool"]["Properties"]
    assert after["LambdaConfig"]["CustomMessage"] == pool["LambdaConfig"]["CustomMessage"]
    assert (
        after["AdminCreateUserConfig"]["InviteMessageTemplate"]
        == pool["AdminCreateUserConfig"]["InviteMessageTemplate"]
    )


@pytest.mark.parametrize(
    "mutation",
    [
        "PostConfirmation",
        "PreAuthentication",
        "mfa",
        "signup",
        "username",
        "spa",
        "group",
        "sam_route",
        "direct_route",
        "openapi_route",
        "resource",
    ],
)
def test_patch_refuses_conflicting_or_unreviewed_application_state(mutation):
    before = baseline()
    resources = before["Resources"]
    pool = resources["SellerUserPool"]["Properties"]
    if mutation in {"PostConfirmation", "PreAuthentication"}:
        pool["LambdaConfig"] = {mutation: "arn:existing"}
    elif mutation == "mfa":
        pool["MfaConfiguration"] = "OPTIONAL"
    elif mutation == "signup":
        pool["AdminCreateUserConfig"]["AllowAdminCreateUserOnly"] = False
    elif mutation == "username":
        pool["UsernameAttributes"] = ["phone_number"]
    elif mutation == "spa":
        resources["SellerSpaRouteFunction"]["Properties"]["FunctionCode"] += "// /store-setup"
    elif mutation == "group":
        resources["OtherGroup"] = {
            "Type": "AWS::Cognito::UserPoolGroup",
            "Properties": {"GroupName": "account"},
        }
    elif mutation == "sam_route":
        resources["OtherFunction"] = {
            "Type": "AWS::Serverless::Function",
            "Properties": {
                "Events": {
                    "Account": {
                        "Type": "HttpApi",
                        "Properties": {"Path": "/v1/account", "Method": "GET"},
                    }
                }
            },
        }
    elif mutation == "direct_route":
        resources["OtherRoute"] = {
            "Type": "AWS::ApiGatewayV2::Route",
            "Properties": {"RouteKey": "GET /v1/account"},
        }
    elif mutation == "openapi_route":
        resources["SellerHttpApi"]["Properties"]["DefinitionBody"]["paths"]["/v1/account"] = {
            "get": {}
        }
    else:
        resources["AccountTable"] = {"Type": "AWS::DynamoDB::Table", "Properties": {}}
    with pytest.raises(ValueError):
        patch_application_template(before)


def test_repeat_patch_is_rejected():
    with pytest.raises(ValueError):
        patch_application_template(patch_application_template(baseline()))


def test_state_capture_binds_existing_resources_and_reserves_every_identity():
    result = validate_existing_state(capture(), now=NOW)
    assert result == {
        "ExistingUserPoolId": "us-west-2_Abc123",
        "ExistingClientId": "a" * 26,
        "ExistingSellerApiId": "abcdefghij",
        "ExistingSellerAuthorizerId": "abc123",
        "ReservedOwnerIds": '["' + "a" * 64 + '","' + "b" * 64 + '"]',
    }
    parameter = build_template()["Parameters"]["ReservedOwnerIds"]
    assert re.fullmatch(parameter["AllowedPattern"], result["ReservedOwnerIds"])
    assert "Default" not in parameter


@pytest.mark.parametrize(
    "mutation",
    [
        "email",
        "token",
        "group",
        "route",
        "count",
        "duplicate",
        "zero",
        "too_many",
        "bad_hash",
        "region",
        "api",
        "authorizer",
        "old",
        "future",
    ],
)
def test_state_capture_refuses_private_fields_conflicts_missing_legacy_identities_and_stale_binding(
    mutation,
):
    state = capture()
    if mutation in {"email", "token"}:
        state[mutation] = "must not be written"
    elif mutation == "group":
        state["group_names"].append("account")
    elif mutation == "route":
        state["route_keys"].append("POST /v1/account")
    elif mutation == "count":
        state["preexisting_user_count"] = 3
    elif mutation == "duplicate":
        state["reserved_owner_ids"] = ["a" * 64, "a" * 64]
    elif mutation == "zero":
        state["reserved_owner_ids"] = ["0" * 64, "a" * 64]
    elif mutation == "too_many":
        state["reserved_owner_ids"] = [f"{i:064x}" for i in range(1, 42)]
        state["preexisting_user_count"] = 41
    elif mutation == "bad_hash":
        state["reserved_owner_ids"] = ["Alice@example.test", "a" * 64]
    elif mutation == "region":
        state["user_pool_id"] = "us-east-1_Abc123"
    elif mutation == "api":
        state["seller_api_id"] = "https://abcdefghij.execute-api.us-west-2.amazonaws.com"
    elif mutation == "authorizer":
        state["seller_authorizer_id"] = "*"
    elif mutation == "old":
        state["captured_at"] = (NOW - timedelta(minutes=16)).strftime("%Y-%m-%dT%H:%M:%SZ")
    else:
        state["captured_at"] = (NOW + timedelta(seconds=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    with pytest.raises(ValueError):
        validate_existing_state(state, now=NOW)


def test_template_files_are_private_and_cannot_overwrite_a_previous_capture(tmp_path):
    output = tmp_path / "account-template.json"
    write_private(output, build_template())
    assert output.stat().st_mode & 0o777 == 0o600
    assert json.loads(output.read_text()) == build_template()
    with pytest.raises(FileExistsError):
        write_private(output, {"replace": True})
