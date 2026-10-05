"""Prepare the isolated native-account stage and its bounded application patch.

This local-only tool never calls AWS, deploys, activates signup, or reads credentials.
Both handlers and the existing pool's new hooks/signup remain disabled by default.
"""

from __future__ import annotations

import argparse
import json
import os
import re
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from pathlib import Path

ACCOUNT_SCOPE = "mr-lister-api/seller"
ACCOUNT_GROUP = "account"
MAX_RESERVED_OWNERS = 40
_ACCOUNT_SPA_ROUTES = ("/store-setup", "/privacy", "/terms", "/judge/privacy", "/judge/terms")
_ACCOUNT_SPA_LINE = (
    "\n  isSpaRoute = isSpaRoute || "
    + " || ".join(f"uri === '{path}'" for path in _ACCOUNT_SPA_ROUTES)
    + ";"
)
_SPA_LINE = (
    "  var isSpaRoute = uri === '/' || uri === '/auth/callback' || uri === '/jobs' "
    "|| uri.indexOf('/jobs/') === 0 || uri.indexOf('/uploads/') === 0;"
)
_PATCH_PARAMETERS = {
    "AccountProvisionFunctionArn",
    "AccountPostConfirmationEnabled",
    "SelfServiceSignupEnabled",
}
_PATCH_CONDITIONS = {"UseAccountPostConfirmation", "AllowSelfServiceSignup"}
_PATCH_RULE = "AccountSignupRequiresProvisionHook"


def ref(name: str) -> dict:
    return {"Ref": name}


def sub(value: str) -> dict:
    return {"Fn::Sub": value}


def arn(name: str) -> dict:
    return {"Fn::GetAtt": [name, "Arn"]}


def _enabled_parameter() -> dict:
    return {"Type": "String", "Default": "false", "AllowedValues": ["false", "true"]}


def build_template() -> dict:
    """Create additive resources referencing the existing pool/client/API by exact ID."""
    parameters = {
        "EnvironmentName": {
            "Type": "String",
            "Default": "dev",
            "AllowedPattern": "[a-z][a-z0-9-]{1,15}",
        },
        "CodeBucket": {"Type": "String", "MinLength": 1},
        "CodeKey": {"Type": "String", "MinLength": 1},
        "CodeVersion": {
            "Type": "String",
            "MinLength": 1,
            "MaxLength": 1024,
            "AllowedPattern": r"(?!(?i:null|latest|current)$)[A-Za-z0-9._~+/=-]{1,1024}",
            "ConstraintDescription": (
                "Use the exact immutable S3 object version, never a mutable placeholder."
            ),
        },
        "ExistingUserPoolId": {"Type": "String", "AllowedPattern": "us-west-2_[A-Za-z0-9]+"},
        "ExistingClientId": {"Type": "String", "AllowedPattern": "[a-z0-9]{1,128}"},
        "ExistingSellerApiId": {"Type": "String", "AllowedPattern": "[a-z0-9]{10}"},
        "ExistingSellerAuthorizerId": {"Type": "String", "AllowedPattern": "[a-z0-9]{6,10}"},
        "ReservedOwnerIds": {
            "Type": "String",
            "MaxLength": 2681,
            "AllowedPattern": r'\["(?!0{64}")[a-f0-9]{64}"(?:,"(?!0{64}")[a-f0-9]{64}"){0,39}\]',
            "ConstraintDescription": (
                "Canonical unique hashes of every pre-existing pool user; no email or token."
            ),
        },
        "AccountProvisionEnabled": _enabled_parameter(),
        "AccountQueryEnabled": _enabled_parameter(),
    }
    resources = {
        "AccountTable": {
            "Type": "AWS::DynamoDB::Table",
            "DeletionPolicy": "Retain",
            "UpdateReplacePolicy": "Retain",
            "Properties": {
                "TableName": sub("mr-lister-account-${EnvironmentName}"),
                "BillingMode": "PAY_PER_REQUEST",
                "AttributeDefinitions": [{"AttributeName": "PK", "AttributeType": "S"}],
                "KeySchema": [{"AttributeName": "PK", "KeyType": "HASH"}],
                "SSESpecification": {"SSEEnabled": True},
                "PointInTimeRecoverySpecification": {"PointInTimeRecoveryEnabled": True},
                "DeletionProtectionEnabled": True,
                "Tags": [{"Key": "Project", "Value": "MrLister"}],
            },
        },
        "AccountGroup": {
            "Type": "AWS::Cognito::UserPoolGroup",
            "Properties": {
                "UserPoolId": ref("ExistingUserPoolId"),
                "GroupName": ACCOUNT_GROUP,
                "Description": "Store-setup account only; no seller or judge privileges",
            },
        },
    }
    assume = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Principal": {"Service": "lambda.amazonaws.com"},
                "Action": "sts:AssumeRole",
            }
        ],
    }
    pool_arn = sub(
        "arn:${AWS::Partition}:cognito-idp:${AWS::Region}:${AWS::AccountId}:userpool/${ExistingUserPoolId}"
    )
    for kind in ("Query", "Provision"):
        name = f"Account{kind}"
        function_name = sub(f"mr-lister-account-${{EnvironmentName}}-{kind.lower()}")
        resources[f"{name}Logs"] = {
            "Type": "AWS::Logs::LogGroup",
            "Properties": {
                "LogGroupName": sub(
                    f"/aws/lambda/mr-lister-account-${{EnvironmentName}}-{kind.lower()}"
                ),
                "RetentionInDays": 30,
            },
        }
        statements = [
            {
                "Effect": "Allow",
                "Action": ["logs:CreateLogStream", "logs:PutLogEvents"],
                "Resource": arn(f"{name}Logs"),
            },
            {
                "Effect": "Allow",
                "Action": ["dynamodb:GetItem"]
                + (["dynamodb:PutItem"] if kind == "Provision" else []),
                "Resource": arn("AccountTable"),
            },
        ]
        if kind == "Provision":
            statements.append(
                {
                    "Effect": "Allow",
                    "Action": ["cognito-idp:AdminGetUser", "cognito-idp:AdminAddUserToGroup"],
                    "Resource": pool_arn,
                }
            )
        resources[f"{name}Role"] = {
            "Type": "AWS::IAM::Role",
            "Properties": {
                "AssumeRolePolicyDocument": deepcopy(assume),
                "Policies": [
                    {
                        "PolicyName": f"account-{kind.lower()}-only",
                        "PolicyDocument": {"Version": "2012-10-17", "Statement": statements},
                    }
                ],
            },
        }
        resources[f"{name}Function"] = {
            "Type": "AWS::Lambda::Function",
            "DependsOn": [f"{name}Logs"] + (["AccountGroup"] if kind == "Provision" else []),
            "Properties": {
                "FunctionName": function_name,
                "Runtime": "python3.12",
                "Architectures": ["arm64"],
                "Handler": f"mr_lister.accounts.{kind.lower()}_entrypoint.lambda_handler",
                "Code": {
                    "S3Bucket": ref("CodeBucket"),
                    "S3Key": ref("CodeKey"),
                    "S3ObjectVersion": ref("CodeVersion"),
                },
                "Role": arn(f"{name}Role"),
                "MemorySize": 256,
                "Timeout": 5 if kind == "Provision" else 10,
                "ReservedConcurrentExecutions": 3,
                "Environment": {
                    "Variables": {
                        "MR_LISTER_ACCOUNT_USER_POOL_ID": ref("ExistingUserPoolId"),
                        "MR_LISTER_ACCOUNT_CLIENT_ID": ref("ExistingClientId"),
                        "MR_LISTER_ACCOUNT_TABLE_NAME": ref("AccountTable"),
                        "MR_LISTER_ACCOUNT_RESERVED_OWNER_IDS": ref("ReservedOwnerIds"),
                        f"MR_LISTER_ACCOUNT_{kind.upper()}_ENABLED": ref(f"Account{kind}Enabled"),
                    }
                },
            },
        }
    resources["AccountQueryIntegration"] = {
        "Type": "AWS::ApiGatewayV2::Integration",
        "Properties": {
            "ApiId": ref("ExistingSellerApiId"),
            "IntegrationType": "AWS_PROXY",
            "IntegrationMethod": "POST",
            "IntegrationUri": arn("AccountQueryFunction"),
            "PayloadFormatVersion": "2.0",
            "TimeoutInMillis": 10000,
        },
    }
    resources["AccountQueryRoute"] = {
        "Type": "AWS::ApiGatewayV2::Route",
        "Properties": {
            "ApiId": ref("ExistingSellerApiId"),
            "RouteKey": "GET /v1/account",
            "AuthorizationType": "JWT",
            "AuthorizerId": ref("ExistingSellerAuthorizerId"),
            "AuthorizationScopes": [ACCOUNT_SCOPE],
            "Target": {"Fn::Join": ["/", ["integrations", ref("AccountQueryIntegration")]]},
        },
    }
    resources["AccountQueryInvoke"] = {
        "Type": "AWS::Lambda::Permission",
        "Properties": {
            "Action": "lambda:InvokeFunction",
            "FunctionName": ref("AccountQueryFunction"),
            "Principal": "apigateway.amazonaws.com",
            "SourceAccount": ref("AWS::AccountId"),
            "SourceArn": sub(
                "arn:${AWS::Partition}:execute-api:${AWS::Region}:${AWS::AccountId}:${ExistingSellerApiId}/*/GET/v1/account"
            ),
        },
    }
    resources["AccountProvisionInvoke"] = {
        "Type": "AWS::Lambda::Permission",
        "Properties": {
            "Action": "lambda:InvokeFunction",
            "FunctionName": ref("AccountProvisionFunction"),
            "Principal": "cognito-idp.amazonaws.com",
            "SourceAccount": ref("AWS::AccountId"),
            "SourceArn": pool_arn,
        },
    }
    return {
        "AWSTemplateFormatVersion": "2010-09-09",
        "Description": (
            "Native account stage only; no provider or publishing authority; disabled by default"
        ),
        "Parameters": parameters,
        "Rules": {
            "AccountRegionBinding": {
                "Assertions": [
                    {
                        "Assert": {"Fn::Equals": [ref("AWS::Region"), "us-west-2"]},
                        "AssertDescription": "Use the existing application's us-west-2 region.",
                    }
                ]
            }
        },
        "Resources": resources,
        "Outputs": {
            "AccountTableName": {"Value": ref("AccountTable")},
            "AccountProvisionFunctionArn": {"Value": arn("AccountProvisionFunction")},
            "AccountQueryFunctionArn": {"Value": arn("AccountQueryFunction")},
        },
    }


def _assert_absent(baseline: dict) -> None:
    resources = baseline.get("Resources")
    if not isinstance(resources, dict):
        raise ValueError("A captured application template is required")
    if set(resources) & set(build_template()["Resources"]):
        raise ValueError("Conflicting account resources already exist")
    for resource in resources.values():
        properties = resource.get("Properties", {})
        if (
            resource.get("Type") == "AWS::Cognito::UserPoolGroup"
            and properties.get("GroupName") == ACCOUNT_GROUP
        ):
            raise ValueError("An account group already exists")
        if properties.get("RouteKey", "").split(" ", 1)[-1] == "/v1/account":
            raise ValueError("An account API route already exists")
        if "/v1/account" in properties.get("DefinitionBody", {}).get("paths", {}):
            raise ValueError("An account API route already exists")
        for event in properties.get("Events", {}).values():
            if event.get("Properties", {}).get("Path") == "/v1/account":
                raise ValueError("An account API route already exists")


def patch_application_template(baseline: dict) -> dict:
    """Add disabled signup/hooks and SPA routes; leave all other authority intact."""
    _assert_absent(baseline)
    if (
        set(baseline.get("Parameters", {})) & _PATCH_PARAMETERS
        or set(baseline.get("Conditions", {})) & _PATCH_CONDITIONS
        or _PATCH_RULE in baseline.get("Rules", {})
    ):
        raise ValueError("The account-stage patch already exists")
    template = deepcopy(baseline)
    try:
        pool = template["Resources"]["SellerUserPool"]["Properties"]
        client = template["Resources"]["SellerUserPoolClient"]["Properties"]
        function = template["Resources"]["SellerSpaRouteFunction"]["Properties"]
    except KeyError as exc:
        raise ValueError("The captured seller application is incomplete") from exc
    if pool.get("MfaConfiguration") != "ON" or pool.get("EnabledMfas") != ["SOFTWARE_TOKEN_MFA"]:
        raise ValueError("The owner's required authenticator MFA must remain intact")
    if pool.get("AdminCreateUserConfig", {}).get("AllowAdminCreateUserOnly") is not True:
        raise ValueError("Self-service signup must start disabled")
    if "email" not in pool.get("AutoVerifiedAttributes", []) or pool.get("UsernameAttributes") != [
        "email"
    ]:
        raise ValueError("Existing native email verification is required")
    if "COGNITO" not in client.get("SupportedIdentityProviders", ["COGNITO"]):
        raise ValueError("The existing native account provider is required")
    if {"PostConfirmation", "PreAuthentication"} & set(pool.get("LambdaConfig", {})):
        raise ValueError(
            "Pre-existing PostConfirmation or PreAuthentication hooks must not be overwritten"
        )
    code = function.get("FunctionCode", "")
    if code.count(_SPA_LINE) != 1 or any(path in code for path in _ACCOUNT_SPA_ROUTES):
        raise ValueError("SPA routing differs from the reviewed application or is already patched")
    template.setdefault("Parameters", {}).update(
        {
            "AccountProvisionFunctionArn": {
                "Type": "String",
                "AllowedPattern": (
                    "arn:aws:lambda:us-west-2:[0-9]{12}:function:"
                    "mr-lister-account-[a-z][a-z0-9-]{1,15}-provision"
                ),
            },
            "AccountPostConfirmationEnabled": _enabled_parameter(),
            "SelfServiceSignupEnabled": _enabled_parameter(),
        }
    )
    template.setdefault("Conditions", {}).update(
        {
            "UseAccountPostConfirmation": {
                "Fn::Equals": [ref("AccountPostConfirmationEnabled"), "true"]
            },
            "AllowSelfServiceSignup": {"Fn::Equals": [ref("SelfServiceSignupEnabled"), "true"]},
        }
    )
    template.setdefault("Rules", {})[_PATCH_RULE] = {
        "Assertions": [
            {
                "Assert": {
                    "Fn::Or": [
                        {"Fn::Equals": [ref("SelfServiceSignupEnabled"), "false"]},
                        {"Fn::Equals": [ref("AccountPostConfirmationEnabled"), "true"]},
                    ]
                },
                "AssertDescription": (
                    "Self-service signup cannot open without the account provision hooks."
                ),
            }
        ]
    }
    pool["AdminCreateUserConfig"]["AllowAdminCreateUserOnly"] = {
        "Fn::If": ["AllowSelfServiceSignup", False, True]
    }
    for hook in ("PostConfirmation", "PreAuthentication"):
        pool.setdefault("LambdaConfig", {})[hook] = {
            "Fn::If": [
                "UseAccountPostConfirmation",
                ref("AccountProvisionFunctionArn"),
                ref("AWS::NoValue"),
            ]
        }
    function["FunctionCode"] = code.replace(_SPA_LINE, _SPA_LINE + _ACCOUNT_SPA_LINE)
    return template


def validate_existing_state(state: dict, *, now: datetime | None = None) -> dict:
    """Validate a secret-free read-only capture before emitting deployment parameters.

    The capture must be made by an operator; no account or API inventory is fetched here.
    Reserved hashes must represent ALL pre-existing pool users, never a truncated subset.
    """
    expected = {
        "captured_at",
        "user_pool_id",
        "client_id",
        "seller_api_id",
        "seller_authorizer_id",
        "group_names",
        "route_keys",
        "reserved_owner_ids",
        "preexisting_user_count",
    }
    if not isinstance(state, dict) or set(state) != expected:
        raise ValueError("Only the exact secret-free account-state capture is accepted")
    now = datetime.now(UTC) if now is None else now
    try:
        captured = datetime.strptime(state["captured_at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    except (TypeError, ValueError) as exc:
        raise ValueError("A UTC capture timestamp is required") from exc
    if not timedelta(0) <= now - captured <= timedelta(minutes=15):
        raise ValueError("Capture the application state again immediately before preparation")
    mapping = {
        "ExistingUserPoolId": "user_pool_id",
        "ExistingClientId": "client_id",
        "ExistingSellerApiId": "seller_api_id",
        "ExistingSellerAuthorizerId": "seller_authorizer_id",
    }
    parameters = build_template()["Parameters"]
    result = {}
    for parameter, field in mapping.items():
        value = state[field]
        if not isinstance(value, str) or not re.fullmatch(
            parameters[parameter]["AllowedPattern"], value
        ):
            raise ValueError("An exact existing account/API binding is required")
        result[parameter] = value
    groups, routes = state["group_names"], state["route_keys"]
    if not all(
        isinstance(items, list) and all(isinstance(item, str) for item in items)
        for items in (groups, routes)
    ):
        raise ValueError("Exact existing group and route inventories are required")
    if ACCOUNT_GROUP in groups or any(route.split(" ", 1)[-1] == "/v1/account" for route in routes):
        raise ValueError("An account group or route already exists; do not overwrite it")
    owners = state["reserved_owner_ids"]
    if (
        not isinstance(owners, list)
        or not 1 <= len(owners) <= MAX_RESERVED_OWNERS
        or any(
            not isinstance(owner, str)
            or not re.fullmatch(r"[a-f0-9]{64}", owner)
            or owner == "0" * 64
            for owner in owners
        )
        or len(set(owners)) != len(owners)
        or type(state["preexisting_user_count"]) is not int
        or state["preexisting_user_count"] != len(owners)
    ):
        raise ValueError("Reserve every pre-existing pool identity once; never truncate the list")
    result["ReservedOwnerIds"] = json.dumps(sorted(owners), separators=(",", ":"))
    # AWS limits the entire environment to 4 KB, including names and resolved values.
    for kind in ("PROVISION", "QUERY"):
        environment = {
            "MR_LISTER_ACCOUNT_USER_POOL_ID": result["ExistingUserPoolId"],
            "MR_LISTER_ACCOUNT_CLIENT_ID": result["ExistingClientId"],
            "MR_LISTER_ACCOUNT_TABLE_NAME": "mr-lister-account-" + "x" * 16,
            "MR_LISTER_ACCOUNT_RESERVED_OWNER_IDS": result["ReservedOwnerIds"],
            f"MR_LISTER_ACCOUNT_{kind}_ENABLED": "false",
        }
        if (
            sum(len(key.encode()) + len(value.encode()) for key, value in environment.items())
            > 4096
        ):
            raise ValueError(
                "The complete reserved identity list does not fit Lambda's environment"
            )
    return result


def write_private(path: Path, content: dict) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as stream:
        json.dump(content, stream, indent=2)
        stream.write("\n")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("services", "application"), required=True)
    parser.add_argument("--baseline-template", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--existing-state", type=Path)
    parser.add_argument("--parameters-output", type=Path)
    args = parser.parse_args()
    if bool(args.existing_state) != bool(args.parameters_output):
        parser.error("Existing-state and parameters-output must be supplied together")
    if args.mode == "application" and args.existing_state:
        parser.error("Existing-state parameter binding is only for the isolated services stack")
    baseline = json.loads(args.baseline_template.read_text())
    _assert_absent(baseline)
    parameters = (
        validate_existing_state(json.loads(args.existing_state.read_text()))
        if args.existing_state
        else None
    )
    template = build_template() if args.mode == "services" else patch_application_template(baseline)
    write_private(args.output, template)
    if parameters is not None:
        write_private(args.parameters_output, parameters)


if __name__ == "__main__":
    main()
