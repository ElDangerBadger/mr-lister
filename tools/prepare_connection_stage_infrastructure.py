"""Prepare retained connection infrastructure and bounded application patches; no AWS calls."""

from __future__ import annotations

import argparse
import json
import re
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from pathlib import Path

from tools.prepare_account_stage_infrastructure import arn, ref, sub, write_private

SCOPE = "mr-lister-api/seller"
INDEX = "CandidateCleanupDue"
ROUTES = {
    "Query": ("GET", "/v1/store-setup", "query_handler"),
    "Validate": ("POST", "/v1/connections/printify/validate", "validate_handler"),
    "Select": ("POST", "/v1/connections/printify/select-shop", "select_handler"),
    "Activate": ("POST", "/v1/connections/printify/activate", "activate_handler"),
}
# Discovery is by exact handler and existing role reference, never a guessed logical resource name.
APPLICATION_HANDLERS = {
    "phase6_lambda.upload_api_handler": "write",
    "phase6_lambda.review_query_api_handler": "read",
    "phase6_lambda.seller_command_api_handler": "write",
    "phase6_lambda.dispatcher_handler": "read",
    "phase6_lambda.preparation_dispatch_handler": "read",
    "phase6_lambda.provider_draft_handler": "provider",
    "phase6_lambda.settlement_handler": "write",
    "phase6_lambda.stuck_execution_recovery_handler": "write",
    "mr_lister.cloud.phase718_entrypoints.publication_query_handler": "read",
    "mr_lister.cloud.phase718_entrypoints.publication_request_handler": "write",
    "mr_lister.cloud.phase718_entrypoints.publication_worker_handler": "provider",
}

APPLICATION_HANDLER_GROUPS = (
    frozenset(handler for handler in APPLICATION_HANDLERS if handler.startswith("phase6_lambda.")),
    frozenset(handler for handler in APPLICATION_HANDLERS if "phase718_entrypoints." in handler),
    frozenset(APPLICATION_HANDLERS),
)


def flag() -> dict:
    return {"Type": "String", "Default": "false", "AllowedValues": ["false", "true"]}


def statement(actions: list[str], resource: object) -> dict:
    return {"Effect": "Allow", "Action": actions, "Resource": resource}


def build_template() -> dict:
    parameters = {
        "EnvironmentName": {
            "Type": "String",
            "Default": "dev",
            "AllowedPattern": "[a-z][a-z0-9-]{1,15}",
        },
        "CodeBucket": {"Type": "String", "AllowedPattern": "[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]"},
        "CodeKey": {"Type": "String", "MinLength": 1, "MaxLength": 1024},
        "CodeVersion": {
            "Type": "String",
            "AllowedPattern": (
                r"(?!(?i:null|none|latest|pending|current|default)$)"
                r"[A-Za-z0-9._~+/=-]{3,1024}"
            ),
        },
        "ExistingUserPoolId": {"Type": "String", "AllowedPattern": "us-west-2_[A-Za-z0-9]+"},
        "ExistingClientId": {"Type": "String", "AllowedPattern": "[a-z0-9]{1,128}"},
        "ExistingSellerApiId": {"Type": "String", "AllowedPattern": "[a-z0-9]{10}"},
        "ExistingSellerAuthorizerId": {"Type": "String", "AllowedPattern": "[a-z0-9]{6,10}"},
        "ExistingAccountTableArn": {
            "Type": "String",
            "AllowedPattern": (
                r"arn:aws:dynamodb:us-west-2:[0-9]{12}:table/"
                r"mr-lister-account-[a-z][a-z0-9-]{1,15}"
            ),
        },
        "ReservedOwnerIds": {
            "Type": "String",
            "MaxLength": 2681,
            "AllowedPattern": r'\["(?!0{64}")[a-f0-9]{64}"(?:,"(?!0{64}")[a-f0-9]{64}"){0,39}\]',
        },
        "ConnectionEnabled": flag(),
        "WorkflowEnabled": flag(),
        "CleanupEnabled": flag(),
    }
    resources = {
        "ConnectionTable": {
            "Type": "AWS::DynamoDB::Table",
            "DeletionPolicy": "Retain",
            "UpdateReplacePolicy": "Retain",
            "Properties": {
                "TableName": sub("mr-lister-connections-${EnvironmentName}"),
                "BillingMode": "PAY_PER_REQUEST",
                "DeletionProtectionEnabled": True,
                "SSESpecification": {"SSEEnabled": True},
                "PointInTimeRecoverySpecification": {"PointInTimeRecoveryEnabled": True},
                "AttributeDefinitions": [
                    {"AttributeName": "PK", "AttributeType": "S"},
                    {"AttributeName": "cleanup_partition", "AttributeType": "S"},
                    {"AttributeName": "cleanup_due", "AttributeType": "N"},
                ],
                "KeySchema": [{"AttributeName": "PK", "KeyType": "HASH"}],
                "GlobalSecondaryIndexes": [
                    {
                        "IndexName": INDEX,
                        "KeySchema": [
                            {"AttributeName": "cleanup_partition", "KeyType": "HASH"},
                            {"AttributeName": "cleanup_due", "KeyType": "RANGE"},
                        ],
                        "Projection": {"ProjectionType": "KEYS_ONLY"},
                    }
                ],
            },
        }
    }
    secret_arn = sub(
        "arn:${AWS::Partition}:secretsmanager:${AWS::Region}:${AWS::AccountId}:secret:mr-lister/${EnvironmentName}/connections/"
        + "?" * 64
        + "/candidate_"
        + "?" * 32
        + "-??????"
    )
    pool_arn = sub(
        "arn:${AWS::Partition}:cognito-idp:${AWS::Region}:${AWS::AccountId}:userpool/${ExistingUserPoolId}"
    )
    for kind in (*ROUTES, "Cleanup"):
        name = "Connection" + kind
        handler = ROUTES[kind][2] if kind in ROUTES else "cleanup_handler"
        statements = [statement(["logs:CreateLogStream", "logs:PutLogEvents"], arn(name + "Logs"))]
        if kind != "Cleanup":
            statements.append(statement(["dynamodb:GetItem"], ref("ExistingAccountTableArn")))
        actions = ["dynamodb:GetItem"]
        if kind != "Query":
            actions += ["dynamodb:PutItem", "dynamodb:ConditionCheckItem"]
        statements.append(statement(actions, arn("ConnectionTable")))
        if kind == "Cleanup":
            statements.append(
                statement(
                    ["dynamodb:Query"], sub("${ConnectionTable.Arn}/index/CandidateCleanupDue")
                )
            )
        secret_actions = {
            "Validate": ["secretsmanager:CreateSecret", "secretsmanager:GetSecretValue"],
            "Select": ["secretsmanager:GetSecretValue"],
            "Cleanup": ["secretsmanager:DeleteSecret"],
        }.get(kind)
        if secret_actions:
            statements.append(statement(secret_actions, secret_arn))
        if kind in ("Select", "Activate"):
            statements.append(statement(["cognito-idp:AdminAddUserToGroup"], pool_arn))
        resources[name + "Logs"] = {
            "Type": "AWS::Logs::LogGroup",
            "Properties": {
                "LogGroupName": sub(
                    f"/aws/lambda/mr-lister-connections-${{EnvironmentName}}-{kind.lower()}"
                ),
                "RetentionInDays": 30,
            },
        }
        resources[name + "Role"] = {
            "Type": "AWS::IAM::Role",
            "Properties": {
                "AssumeRolePolicyDocument": {
                    "Version": "2012-10-17",
                    "Statement": [
                        {
                            "Effect": "Allow",
                            "Principal": {"Service": "lambda.amazonaws.com"},
                            "Action": "sts:AssumeRole",
                        }
                    ],
                },
                "Policies": [
                    {
                        "PolicyName": f"connection-{kind.lower()}-only",
                        "PolicyDocument": {"Version": "2012-10-17", "Statement": statements},
                    }
                ],
            },
        }
        variables = {
            "MR_LISTER_ACCOUNT_USER_POOL_ID": ref("ExistingUserPoolId"),
            "MR_LISTER_ACCOUNT_CLIENT_ID": ref("ExistingClientId"),
            "MR_LISTER_ACCOUNT_TABLE_NAME": sub("mr-lister-account-${EnvironmentName}"),
            "MR_LISTER_ACCOUNT_RESERVED_OWNER_IDS": ref("ReservedOwnerIds"),
            "MR_LISTER_CONNECTION_ENABLED": ref("ConnectionEnabled"),
            "MR_LISTER_CONNECTION_WORKFLOW_ENABLED": ref("WorkflowEnabled"),
            "MR_LISTER_CONNECTION_TABLE_NAME": ref("ConnectionTable"),
            "MR_LISTER_CONNECTION_SECRET_PREFIX": sub("mr-lister/${EnvironmentName}/connections/"),
        }
        if kind == "Cleanup":
            variables["MR_LISTER_CONNECTION_CLEANUP_INDEX"] = INDEX
        resources[name + "Function"] = {
            "Type": "AWS::Lambda::Function",
            "DependsOn": [name + "Logs"],
            "Properties": {
                "FunctionName": sub(f"mr-lister-connections-${{EnvironmentName}}-{kind.lower()}"),
                "Runtime": "python3.12",
                "Architectures": ["arm64"],
                "MemorySize": 256,
                "Timeout": 10 if kind == "Query" else (60 if kind == "Cleanup" else 29),
                "ReservedConcurrentExecutions": 1 if kind == "Cleanup" else 3,
                "Handler": "mr_lister.connections.entrypoint." + handler,
                "Code": {
                    "S3Bucket": ref("CodeBucket"),
                    "S3Key": ref("CodeKey"),
                    "S3ObjectVersion": ref("CodeVersion"),
                },
                "Role": arn(name + "Role"),
                "Environment": {"Variables": variables},
            },
        }
        if kind in ROUTES:
            method, path, _ = ROUTES[kind]
            resources[name + "Integration"] = {
                "Type": "AWS::ApiGatewayV2::Integration",
                "Properties": {
                    "ApiId": ref("ExistingSellerApiId"),
                    "IntegrationType": "AWS_PROXY",
                    "IntegrationMethod": "POST",
                    "IntegrationUri": arn(name + "Function"),
                    "PayloadFormatVersion": "2.0",
                    "TimeoutInMillis": 10000 if kind == "Query" else 29000,
                },
            }
            resources[name + "Route"] = {
                "Type": "AWS::ApiGatewayV2::Route",
                "Properties": {
                    "ApiId": ref("ExistingSellerApiId"),
                    "RouteKey": method + " " + path,
                    "AuthorizationType": "JWT",
                    "AuthorizerId": ref("ExistingSellerAuthorizerId"),
                    "AuthorizationScopes": [SCOPE],
                    "Target": {"Fn::Join": ["/", ["integrations", ref(name + "Integration")]]},
                },
            }
            resources[name + "Invoke"] = {
                "Type": "AWS::Lambda::Permission",
                "Properties": {
                    "Action": "lambda:InvokeFunction",
                    "FunctionName": ref(name + "Function"),
                    "Principal": "apigateway.amazonaws.com",
                    "SourceAccount": ref("AWS::AccountId"),
                    "SourceArn": sub(
                        "arn:${AWS::Partition}:execute-api:${AWS::Region}:${AWS::AccountId}:${ExistingSellerApiId}/*/"
                        + method
                        + path
                    ),
                },
            }
    resources["ConnectionCleanupScheduleGroup"] = {
        "Type": "AWS::Scheduler::ScheduleGroup",
        "Properties": {"Name": sub("mr-lister-connections-${EnvironmentName}")},
    }
    resources["ConnectionCleanupDeliveryRole"] = {
        "Type": "AWS::IAM::Role",
        "Properties": {
            "AssumeRolePolicyDocument": {
                "Version": "2012-10-17",
                "Statement": [
                    {
                        "Effect": "Allow",
                        "Principal": {"Service": "scheduler.amazonaws.com"},
                        "Action": "sts:AssumeRole",
                        "Condition": {
                            "StringEquals": {"aws:SourceAccount": ref("AWS::AccountId")},
                            "ArnEquals": {"aws:SourceArn": arn("ConnectionCleanupScheduleGroup")},
                        },
                    }
                ],
            },
            "Policies": [
                {
                    "PolicyName": "invoke-exact-connection-cleanup",
                    "PolicyDocument": {
                        "Version": "2012-10-17",
                        "Statement": [
                            statement(["lambda:InvokeFunction"], arn("ConnectionCleanupFunction"))
                        ],
                    },
                }
            ],
        },
    }
    resources["ConnectionCleanupSchedule"] = {
        "Type": "AWS::Scheduler::Schedule",
        "Properties": {
            "Name": sub("mr-lister-connections-${EnvironmentName}-cleanup"),
            "GroupName": ref("ConnectionCleanupScheduleGroup"),
            "ScheduleExpression": "rate(1 minute)",
            "FlexibleTimeWindow": {"Mode": "OFF"},
            "State": {"Fn::If": ["RunConnectionCleanup", "ENABLED", "DISABLED"]},
            "Target": {
                "Arn": arn("ConnectionCleanupFunction"),
                "RoleArn": arn("ConnectionCleanupDeliveryRole"),
                "Input": "{}",
                "RetryPolicy": {"MaximumEventAgeInSeconds": 60, "MaximumRetryAttempts": 0},
            },
        },
    }
    return {
        "AWSTemplateFormatVersion": "2010-09-09",
        "Description": "Private guided-token connection stage; disabled by default",
        "Parameters": parameters,
        "Conditions": {"RunConnectionCleanup": {"Fn::Equals": [ref("CleanupEnabled"), "true"]}},
        "Rules": {
            "ConnectionActivationCoherence": {
                "Assertions": [
                    {
                        "Assert": {
                            "Fn::Or": [
                                {"Fn::Equals": [ref("WorkflowEnabled"), "false"]},
                                {"Fn::Equals": [ref("ConnectionEnabled"), "true"]},
                            ]
                        },
                        "AssertDescription": "Workflow requires connection services.",
                    },
                    {
                        "Assert": {
                            "Fn::Or": [
                                {"Fn::Equals": [ref("ConnectionEnabled"), "false"]},
                                {"Fn::Equals": [ref("CleanupEnabled"), "true"]},
                            ]
                        },
                        "AssertDescription": "Token validation requires scheduled cleanup.",
                    },
                    {
                        "Assert": {"Fn::Equals": [ref("AWS::Region"), "us-west-2"]},
                        "AssertDescription": "Use the exact existing region.",
                    },
                ]
            }
        },
        "Resources": resources,
        "Outputs": {
            "ConnectionTableName": {"Value": ref("ConnectionTable")},
            "ConnectionTableArn": {"Value": arn("ConnectionTable")},
        },
    }


def _fresh(state: dict, now: datetime | None) -> None:
    current = now or datetime.now(UTC)
    try:
        captured = datetime.strptime(state["captured_at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
        if not timedelta(0) <= current - captured <= timedelta(minutes=15):
            raise ValueError
    except Exception:
        raise ValueError(
            "A fresh complete capture no older than fifteen minutes is required"
        ) from None


def environment_size(environment: dict) -> int:
    if not all(isinstance(k, str) and isinstance(v, str) for k, v in environment.items()):
        raise ValueError("Resolve every complete environment value before preparation")
    size = sum(len(k.encode()) + len(v.encode()) for k, v in environment.items())
    if size > 4096:
        raise ValueError(
            "The complete environment exceeds 4096 bytes; never truncate reserved owners"
        )
    return size


def validate_existing_state(state: dict, *, now: datetime | None = None) -> dict:
    required = {
        "captured_at",
        "region",
        "account_id",
        "environment_name",
        "user_pool_id",
        "client_id",
        "seller_api_id",
        "seller_authorizer_id",
        "authorizer",
        "account_table_arn",
        "reserved_owner_ids",
        "preexisting_user_count",
        "route_keys",
        "group_names",
        "code",
    }
    if not isinstance(state, dict) or not required <= set(state):
        raise ValueError("An exact connection-state capture is required")
    _fresh(state, now)
    if (
        state["region"] != "us-west-2"
        or not isinstance(state["account_id"], str)
        or re.fullmatch(r"[0-9]{12}", state["account_id"]) is None
        or state["account_id"] == "0" * 12
    ):
        raise ValueError("Exact existing region/account required")
    if (
        not isinstance(state["environment_name"], str)
        or re.fullmatch(r"[a-z][a-z0-9-]{1,15}", state["environment_name"]) is None
    ):
        raise ValueError("Exact existing environment required")
    params = {
        "EnvironmentName": state["environment_name"],
        "ExistingUserPoolId": state["user_pool_id"],
        "ExistingClientId": state["client_id"],
        "ExistingSellerApiId": state["seller_api_id"],
        "ExistingSellerAuthorizerId": state["seller_authorizer_id"],
        "ExistingAccountTableArn": state["account_table_arn"],
    }
    if state["authorizer"] != {
        "type": "JWT",
        "issuer": "https://cognito-idp.us-west-2.amazonaws.com/" + str(state["user_pool_id"]),
        "audience": [state["client_id"]],
    }:
        raise ValueError("The existing authorizer must use the exact primary issuer/client")
    if state["account_table_arn"] != (
        f"arn:aws:dynamodb:{state['region']}:{state['account_id']}:table/"
        f"mr-lister-account-{state['environment_name']}"
    ):
        raise ValueError("Account table binding differs")
    if not isinstance(state["code"], dict) or set(state["code"]) != {"bucket", "key", "version"}:
        raise ValueError("Exact versioned deployment archive required")
    for key in ("bucket", "key", "version"):
        value = state["code"].get(key)
        if (
            not isinstance(value, str)
            or not value
            or value != value.strip()
            or any(ord(c) < 32 for c in value)
        ):
            raise ValueError("Exact versioned deployment archive required")
        params[{"bucket": "CodeBucket", "key": "CodeKey", "version": "CodeVersion"}[key]] = value
    for name, value in params.items():
        definition = build_template()["Parameters"][name]
        if (
            not isinstance(value, str)
            or len(value) < definition.get("MinLength", 0)
            or len(value) > definition.get("MaxLength", 4096)
            or (
                "AllowedPattern" in definition
                and re.fullmatch(definition["AllowedPattern"], value) is None
            )
        ):
            raise ValueError("Captured resource binding differs from the connection contract")
    owners = state["reserved_owner_ids"]
    if (
        not isinstance(owners, list)
        or not 1 <= len(owners) <= 40
        or any(
            not isinstance(owner, str)
            or re.fullmatch(r"[a-f0-9]{64}", owner) is None
            or owner == "0" * 64
            for owner in owners
        )
        or len(set(owners)) != len(owners)
        or type(state["preexisting_user_count"]) is not int
        or state["preexisting_user_count"] != len(owners)
    ):
        raise ValueError("Reserve every pre-existing owner exactly once")
    params["ReservedOwnerIds"] = json.dumps(sorted(owners), separators=(",", ":"))
    if (
        not isinstance(state["group_names"], list)
        or not all(isinstance(value, str) for value in state["group_names"])
        or not {"account", "seller"} <= set(state["group_names"])
        or len(set(state["group_names"])) != len(state["group_names"])
    ):
        raise ValueError("The existing account and seller groups are required")
    if (
        not isinstance(state["route_keys"], list)
        or not all(isinstance(value, str) for value in state["route_keys"])
        or len(set(state["route_keys"])) != len(state["route_keys"])
        or any(method + " " + path in state["route_keys"] for method, path, _ in ROUTES.values())
    ):
        raise ValueError("Connection routes must be absent before additive preparation")
    params.update(ConnectionEnabled="false", WorkflowEnabled="false", CleanupEnabled="false")
    resolver = TemplateResolver(
        build_template(),
        {
            **state,
            "parameters": params,
            "references": {"ConnectionTable": "mr-lister-connections-" + state["environment_name"]},
        },
    )
    for kind in (*ROUTES, "Cleanup"):
        environment_size(
            resolver.resolve(
                build_template()["Resources"]["Connection" + kind + "Function"]["Properties"][
                    "Environment"
                ]["Variables"]
            )
        )
    return params


_ABSENT = object()


class TemplateResolver:
    """Resolve supported captured intrinsics; unknown/missing values are a hard stop."""

    def __init__(self, template: dict, state: dict) -> None:
        self.template = template
        self.parameters = {
            name: definition["Default"]
            for name, definition in template.get("Parameters", {}).items()
            if "Default" in definition
        }
        self.parameters.update(state.get("parameters", {}))
        self.references = dict(state.get("references", {}))
        self.references.update(
            {
                "AWS::Region": state["region"],
                "AWS::AccountId": state["account_id"],
                "AWS::Partition": "aws",
                "AWS::URLSuffix": "amazonaws.com",
                "AWS::NoValue": _ABSENT,
            }
        )

    def value(self, name: str):
        if name in self.parameters:
            return self.parameters[name]
        if name not in self.references:
            raise ValueError("A captured reference is missing")
        return self.references[name]

    def condition(self, value):
        if isinstance(value, str):
            return self.condition(self.template["Conditions"][value])
        if not isinstance(value, dict) or len(value) != 1:
            raise ValueError("Unsupported captured condition")
        name, args = next(iter(value.items()))
        if name == "Fn::Equals":
            return self.resolve(args[0]) == self.resolve(args[1])
        if name == "Fn::Not":
            return not self.condition(args[0])
        if name == "Fn::And":
            return all(self.condition(arg) for arg in args)
        if name == "Fn::Or":
            return any(self.condition(arg) for arg in args)
        raise ValueError("Unsupported captured condition")

    def resolve(self, value):
        if isinstance(value, list):
            return [result for v in value if (result := self.resolve(v)) is not _ABSENT]
        if not isinstance(value, dict):
            return value
        if set(value) == {"Ref"}:
            return self.value(value["Ref"])
        if set(value) == {"Fn::GetAtt"}:
            target = value["Fn::GetAtt"]
            return self.value(target if isinstance(target, str) else ".".join(target))
        if set(value) == {"Fn::If"}:
            name, yes, no = value["Fn::If"]
            return self.resolve(yes if self.condition(name) else no)
        if set(value) == {"Fn::Join"}:
            delimiter, parts = value["Fn::Join"]
            return delimiter.join(self.resolve(part) for part in parts)
        if set(value) == {"Fn::Sub"}:
            arg = value["Fn::Sub"]
            text, overrides = (arg, {}) if isinstance(arg, str) else arg

            def replace(match):
                name = match[1]
                resolved = self.resolve(overrides[name]) if name in overrides else self.value(name)
                if not isinstance(resolved, (str, int)):
                    raise ValueError("Substitution is not a captured scalar")
                return str(resolved)

            return re.sub(r"\$\{([^}]+)\}", replace, text)
        if any(key.startswith("Fn::") for key in value):
            raise ValueError("Unsupported captured intrinsic")
        result = {}
        for key, item in value.items():
            resolved = self.resolve(item)
            if resolved is not _ABSENT:
                result[key] = resolved
        return result


def template_digest(template: dict) -> str:
    from hashlib import sha256

    return sha256(json.dumps(template, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _new_environment(state: dict, parameters: dict) -> dict:
    env = state["environment_name"]
    return {
        "MR_LISTER_ACCOUNT_USER_POOL_ID": state["user_pool_id"],
        "MR_LISTER_ACCOUNT_CLIENT_ID": state["client_id"],
        "MR_LISTER_ACCOUNT_TABLE_NAME": "mr-lister-account-" + env,
        "MR_LISTER_ACCOUNT_RESERVED_OWNER_IDS": parameters["ReservedOwnerIds"],
        "MR_LISTER_CONNECTION_ENABLED": ref("ConnectionEnabled"),
        "MR_LISTER_CONNECTION_WORKFLOW_ENABLED": ref("WorkflowEnabled"),
        "MR_LISTER_CONNECTION_TABLE_NAME": "mr-lister-connections-" + env,
        "MR_LISTER_CONNECTION_SECRET_PREFIX": "mr-lister/" + env + "/connections/",
        "MR_LISTER_COGNITO_ISSUER": "https://cognito-idp.us-west-2.amazonaws.com/"
        + state["user_pool_id"],
        "MR_LISTER_COGNITO_CLIENT_ID": state["client_id"],
    }


def _patch_flags(template: dict) -> None:
    if (
        {"ConnectionEnabled", "WorkflowEnabled"} & set(template.get("Parameters", {}))
        or "UseStoreConnections" in template.get("Conditions", {})
        or "WorkflowRequiresStoreConnections" in template.get("Rules", {})
    ):
        raise ValueError("Connection flags already exist; review a deliberate update instead")
    template.setdefault("Parameters", {}).update(ConnectionEnabled=flag(), WorkflowEnabled=flag())
    template.setdefault("Conditions", {})["UseStoreConnections"] = {
        "Fn::Equals": [ref("ConnectionEnabled"), "true"]
    }
    template.setdefault("Rules", {})["WorkflowRequiresStoreConnections"] = {
        "Assertions": [
            {
                "Assert": {
                    "Fn::Or": [
                        {"Fn::Equals": [ref("WorkflowEnabled"), "false"]},
                        {"Fn::Equals": [ref("ConnectionEnabled"), "true"]},
                    ]
                },
                "AssertDescription": "Workflow requires reviewed connection authority.",
            }
        ]
    }


def _conditional(statement_value: dict) -> dict:
    return {"Fn::If": ["UseStoreConnections", statement_value, ref("AWS::NoValue")]}


def patch_application_template(baseline: dict, state: dict, *, now: datetime | None = None) -> dict:
    params = validate_existing_state(state, now=now)
    if state.get("application_template_sha256") != template_digest(baseline):
        raise ValueError("The application template differs from its fresh capture")
    template = deepcopy(baseline)
    _patch_flags(template)
    resources = template.get("Resources", {})
    original_resolver = TemplateResolver(baseline, state)
    patched_resolver = TemplateResolver(
        template,
        {
            **state,
            "parameters": {
                **state.get("parameters", {}),
                "ConnectionEnabled": "false",
                "WorkflowEnabled": "false",
            },
        },
    )
    inventory = state.get("functions")
    if not isinstance(inventory, dict):
        raise ValueError("Captured function configurations are required")
    selected = {}
    for logical_id, resource in resources.items():
        if resource.get("Type") not in ("AWS::Lambda::Function", "AWS::Serverless::Function"):
            continue
        properties = resource["Properties"]
        handler = properties.get("Handler")
        if handler in APPLICATION_HANDLERS:
            if handler in selected:
                raise ValueError("Duplicate application handler authority")
            selected[handler] = logical_id
    if frozenset(selected) not in APPLICATION_HANDLER_GROUPS or set(inventory) != set(
        selected.values()
    ):
        raise ValueError("Capture every function in one complete known application stack group")
    connection_arn = (
        f"arn:aws:dynamodb:{state['region']}:{state['account_id']}:table/"
        f"mr-lister-connections-{state['environment_name']}"
    )
    secret_arn = (
        f"arn:aws:secretsmanager:{state['region']}:{state['account_id']}:secret:mr-lister/{state['environment_name']}/connections/"
        + "?" * 64
        + "/candidate_"
        + "?" * 32
        + "-??????"
    )
    for handler, logical_id in selected.items():
        resource = resources[logical_id]
        properties = resource["Properties"]
        role_ref = properties.get("Role", {}).get("Fn::GetAtt")
        if not isinstance(role_ref, list) or len(role_ref) != 2 or role_ref[1] != "Arn":
            raise ValueError("Each function must reference its exact existing local execution role")
        role_id = role_ref[0]
        role = resources.get(role_id)
        if not isinstance(role, dict) or role.get("Type") != "AWS::IAM::Role":
            raise ValueError("The existing execution role is unavailable")
        expected_role_arn = original_resolver.resolve(properties["Role"])
        for name, candidate in resources.items():
            if name == logical_id or candidate.get("Type") not in (
                "AWS::Lambda::Function",
                "AWS::Serverless::Function",
            ):
                continue
            candidate_role = candidate.get("Properties", {}).get("Role")
            if candidate_role is None and candidate["Type"] == "AWS::Serverless::Function":
                candidate_role = baseline.get("Globals", {}).get("Function", {}).get("Role")
            if (
                candidate_role is not None
                and original_resolver.resolve(candidate_role) == expected_role_arn
            ):
                raise ValueError(
                    "Shared execution roles need a separately reviewed capability split"
                )
        live = inventory[logical_id]
        if not isinstance(live, dict) or set(live) != {"function_name", "role_arn", "environment"}:
            raise ValueError("Captured function identity/environment is incomplete")
        function_name = original_resolver.resolve(properties.get("FunctionName", ref(logical_id)))
        role_arn = original_resolver.resolve(properties["Role"])
        if (
            live["function_name"] != function_name
            or live["role_arn"] != role_arn
            or not re.fullmatch(
                f"arn:aws:iam::{state['account_id']}:role/[A-Za-z0-9_+=,.@/-]+", role_arn
            )
        ):
            raise ValueError("Captured function or execution-role identity differs")
        combined = {}
        if resource["Type"] == "AWS::Serverless::Function":
            combined.update(
                baseline.get("Globals", {})
                .get("Function", {})
                .get("Environment", {})
                .get("Variables", {})
            )
        combined.update(properties.get("Environment", {}).get("Variables", {}))
        actual = original_resolver.resolve(combined)
        if actual != live["environment"]:
            raise ValueError("Captured live environment differs from the resolved template")
        additions = _new_environment(state, params)
        if APPLICATION_HANDLERS[handler] == "provider":
            additions["MR_LISTER_LEGACY_OWNER_IDS"] = params["ReservedOwnerIds"]
        for key in set(actual) & set(additions):
            if actual[key] != patched_resolver.resolve(additions[key]):
                raise ValueError("An existing connection/identity environment field would change")
        environment_size({**actual, **patched_resolver.resolve(additions)})
        properties.setdefault("Environment", {}).setdefault("Variables", {}).update(additions)
        actions = ["dynamodb:GetItem"]
        if APPLICATION_HANDLERS[handler] != "read":
            actions.append("dynamodb:ConditionCheckItem")
        statements = [statement(actions, connection_arn)]
        if APPLICATION_HANDLERS[handler] == "provider":
            statements.append(statement(["secretsmanager:GetSecretValue"], secret_arn))
        policies = role["Properties"].setdefault("Policies", [])
        if any(policy.get("PolicyName") == "bound-store-connections-only" for policy in policies):
            raise ValueError("Connection execution policy already exists")
        policies.append(
            _conditional(
                {
                    "PolicyName": "bound-store-connections-only",
                    "PolicyDocument": {"Version": "2012-10-17", "Statement": statements},
                }
            )
        )
    return template


def patch_runtime_role_template(
    baseline: dict, state: dict, *, role_logical_id: str, now: datetime | None = None
) -> dict:
    validate_existing_state(state, now=now)
    if state.get("runtime_template_sha256") != template_digest(baseline):
        raise ValueError("The runtime template differs from its fresh capture")
    template = deepcopy(baseline)
    role = template.get("Resources", {}).get(role_logical_id)
    if not isinstance(role, dict) or role.get("Type") != "AWS::IAM::Role":
        raise ValueError("Exact existing AgentCore execution role required")
    trust = role["Properties"].get("AssumeRolePolicyDocument", {}).get("Statement", [])
    if not any(
        item.get("Principal", {}).get("Service") == "bedrock-agentcore.amazonaws.com"
        for item in trust
    ):
        raise ValueError("Never patch a deployment or non-runtime role")
    resolver = TemplateResolver(baseline, state)
    runtime_arn = resolver.resolve(arn(role_logical_id))
    if (
        not isinstance(runtime_arn, str)
        or re.fullmatch(
            f"arn:aws:iam::{state['account_id']}:role/[A-Za-z0-9_+=,.@/-]+", runtime_arn
        )
        is None
        or runtime_arn != state.get("runtime_role_arn")
    ):
        raise ValueError("Runtime execution role differs from capture")
    _patch_flags(template)
    table_arn = (
        f"arn:aws:dynamodb:{state['region']}:{state['account_id']}:table/"
        f"mr-lister-connections-{state['environment_name']}"
    )
    policies = role["Properties"].setdefault("Policies", [])
    if any(item.get("PolicyName") == "bound-store-connections-only" for item in policies):
        raise ValueError("Runtime binding policy already exists")
    policies.append(
        _conditional(
            {
                "PolicyName": "bound-store-connections-only",
                "PolicyDocument": {
                    "Version": "2012-10-17",
                    "Statement": [
                        statement(["dynamodb:GetItem", "dynamodb:ConditionCheckItem"], table_arn)
                    ],
                },
            }
        )
    )
    return template


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--mode", choices=("services", "application", "runtime-role"), required=True
    )
    parser.add_argument("--existing-state", type=Path, required=True)
    parser.add_argument("--baseline-template", type=Path)
    parser.add_argument("--role-logical-id")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--parameters-output", type=Path)
    args = parser.parse_args()
    if args.parameters_output and args.mode != "services":
        parser.error("Parameter output is only supported for the separate services stack")
    state = json.loads(args.existing_state.read_text())
    parameters = validate_existing_state(state)
    if args.mode == "services":
        template = build_template()
    else:
        if args.baseline_template is None:
            parser.error("A fresh captured baseline template is required")
        baseline = json.loads(args.baseline_template.read_text())
        if args.mode == "application":
            template = patch_application_template(baseline, state)
        else:
            if not args.role_logical_id:
                parser.error("An exact runtime execution role logical ID is required")
            template = patch_runtime_role_template(
                baseline, state, role_logical_id=args.role_logical_id
            )
    write_private(args.output, template)
    if args.parameters_output:
        write_private(args.parameters_output, parameters)


if __name__ == "__main__":
    main()
