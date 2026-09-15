"""Prepare an isolated judge-session API and cleanup stack; never deploy it.

The existing seller stack needs only an additive CloudFront origin/behavior patch.
Both new services and the cleanup schedule default to disabled. Secrets and invitations
are provisioned separately; this template contains no credential material.
"""

from __future__ import annotations

import argparse
import json
import os
import re
from copy import deepcopy
from pathlib import Path

APPLICATION_ORIGIN = "https://massskutiny.com"
COOKIE_NAME = "__Secure-mr-lister-judge"


def ref(name: str) -> dict:
    return {"Ref": name}


def sub(value: str, variables: dict | None = None) -> dict:
    return {"Fn::Sub": value if variables is None else [value, variables]}


def arn(name: str) -> dict:
    return {"Fn::GetAtt": [name, "Arn"]}


def build_template() -> dict:
    parameters = {
        "EnvironmentName": {
            "Type": "String",
            "Default": "dev",
            "AllowedPattern": "[a-z][a-z0-9-]{1,15}",
        },
        "CodeBucket": {"Type": "String"},
        "CodeKey": {"Type": "String"},
        "CodeVersion": {"Type": "String", "MinLength": 1},
        "ApplicationOrigin": {"Type": "String", "AllowedValues": [APPLICATION_ORIGIN]},
        "PrimaryIssuer": {
            "Type": "String",
            "AllowedPattern": "https://cognito-idp\\.us-west-2\\.amazonaws\\.com/us-west-2_[A-Za-z0-9]+",
        },
        "PrimaryClientId": {"Type": "String", "AllowedPattern": "[a-z0-9]{1,128}"},
        "JudgeSubject": {"Type": "String", "AllowedPattern": "[A-Za-z0-9_-]{1,128}"},
        "JudgeOwnerId": {"Type": "String", "AllowedPattern": "[a-f0-9]{64}"},
        "PrimaryOwnerId": {"Type": "String", "AllowedPattern": "[a-f0-9]{64}"},
        "CognitoOrigin": {
            "Type": "String",
            "AllowedPattern": "https://[a-z0-9-]+\\.auth\\.us-west-2\\.amazoncognito\\.com",
        },
        "CampaignId": {"Type": "String", "AllowedPattern": "[A-Za-z0-9_-]{1,64}"},
        "CampaignExpiresAtEpoch": {"Type": "Number", "MinValue": 1},
        "CampaignStartedAt": {
            "Type": "String",
            "AllowedPattern": "[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z",
        },
        "PrintifyShopId": {"Type": "Number", "MinValue": 1},
        "PrintifySecretArn": {
            "Type": "String",
            "AllowedPattern": (
                "arn:aws:secretsmanager:us-west-2:[0-9]{12}:secret:mr-lister/[A-Za-z0-9/_-]+"
            ),
        },
        "SourceTableName": {"Type": "String", "AllowedPattern": "mr-lister-phase6-[a-z0-9-]+"},
        "SessionEnabled": {
            "Type": "String",
            "Default": "false",
            "AllowedValues": ["false", "true"],
        },
        "CleanupDryRun": {"Type": "String", "Default": "true", "AllowedValues": ["true", "false"]},
        "CleanupScheduleEnabled": {
            "Type": "String",
            "Default": "false",
            "AllowedValues": ["false", "true"],
        },
        "CleanupActivationFingerprint": {
            "Type": "String",
            "Default": "",
            "AllowedPattern": "([a-f0-9]{64})?",
        },
        "AlarmTopicArn": {
            "Type": "String",
            "AllowedPattern": (
                "arn:aws:sns:us-west-2:[0-9]{12}:mr-lister-phase6-[a-z0-9-]+-operational-alarms"
            ),
        },
    }
    resources: dict = {}
    for logical, suffix in (("SessionTable", "sessions"), ("CleanupTable", "cleanup")):
        properties = {
            "TableName": sub(f"mr-lister-judge-${{EnvironmentName}}-{suffix}"),
            "BillingMode": "PAY_PER_REQUEST",
            "AttributeDefinitions": [
                {"AttributeName": key, "AttributeType": "S"} for key in ("PK", "SK")
            ],
            "KeySchema": [
                {"AttributeName": "PK", "KeyType": "HASH"},
                {"AttributeName": "SK", "KeyType": "RANGE"},
            ],
            "SSESpecification": {"SSEEnabled": True},
            "PointInTimeRecoverySpecification": {"PointInTimeRecoveryEnabled": True},
            "DeletionProtectionEnabled": True,
            "Tags": [{"Key": "Project", "Value": "MrLister"}],
        }
        if logical == "SessionTable":
            properties["AttributeDefinitions"] = [{"AttributeName": "PK", "AttributeType": "S"}]
            properties["KeySchema"] = [{"AttributeName": "PK", "KeyType": "HASH"}]
            properties["TimeToLiveSpecification"] = {"AttributeName": "expires_at", "Enabled": True}
        else:
            properties["AttributeDefinitions"].extend(
                [{"AttributeName": key, "AttributeType": "S"} for key in ("due_pk", "due_sk")]
            )
            properties["GlobalSecondaryIndexes"] = [
                {
                    "IndexName": "DueIndex",
                    "KeySchema": [
                        {"AttributeName": "due_pk", "KeyType": "HASH"},
                        {"AttributeName": "due_sk", "KeyType": "RANGE"},
                    ],
                    "Projection": {"ProjectionType": "ALL"},
                }
            ]
        resources[logical] = {
            "Type": "AWS::DynamoDB::Table",
            "DeletionPolicy": "Retain",
            "UpdateReplacePolicy": "Retain",
            "Properties": properties,
        }
    resources["SessionSeedSecret"] = {
        "Type": "AWS::SecretsManager::Secret",
        "DeletionPolicy": "Retain",
        "UpdateReplacePolicy": "Retain",
        "Properties": {
            "Name": sub("mr-lister/${EnvironmentName}/judge-session-seed"),
            "Description": "Server-only primary judge OAuth refresh token; operator populated",
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
    source_arn = (
        "arn:${AWS::Partition}:dynamodb:${AWS::Region}:${AWS::AccountId}:table/${SourceTableName}"
    )
    for kind in ("Session", "Cleanup"):
        log_name = sub(f"/aws/lambda/mr-lister-judge-${{EnvironmentName}}-{kind.lower()}")
        resources[f"{kind}Logs"] = {
            "Type": "AWS::Logs::LogGroup",
            "Properties": {"LogGroupName": log_name, "RetentionInDays": 30},
        }
        statements = [
            {
                "Effect": "Allow",
                "Action": ["logs:CreateLogStream", "logs:PutLogEvents"],
                "Resource": arn(f"{kind}Logs"),
            },
            {
                "Effect": "Allow",
                # Transactional Put/Update use the corresponding item permissions.
                "Action": ["dynamodb:GetItem", "dynamodb:PutItem"]
                + (["dynamodb:UpdateItem"] if kind == "Session" else ["dynamodb:Query"]),
                "Resource": arn(f"{kind}Table"),
            },
            {
                "Effect": "Allow",
                "Action": ["secretsmanager:GetSecretValue"],
                "Resource": ref("SessionSeedSecret")
                if kind == "Session"
                else ref("PrintifySecretArn"),
            },
        ]
        if kind == "Cleanup":
            statements.extend(
                [
                    {
                        "Effect": "Allow",
                        "Action": ["dynamodb:Query"],
                        "Resource": sub("${CleanupTable.Arn}/index/DueIndex"),
                    },
                    {
                        "Effect": "Allow",
                        "Action": ["dynamodb:GetItem", "dynamodb:Query"],
                        "Resource": sub(source_arn),
                    },
                    {
                        "Effect": "Allow",
                        "Action": ["dynamodb:Query"],
                        "Resource": sub(source_arn + "/index/OwnerJobsIndex"),
                    },
                ]
            )
        resources[f"{kind}Role"] = {
            "Type": "AWS::IAM::Role",
            "Properties": {
                "AssumeRolePolicyDocument": assume,
                "Policies": [
                    {
                        "PolicyName": f"judge-{kind.lower()}-only",
                        "PolicyDocument": {"Version": "2012-10-17", "Statement": statements},
                    }
                ],
            },
        }
        if kind == "Session":
            settings = {
                "ENABLED": ref("SessionEnabled"),
                "TABLE": ref("SessionTable"),
                "SEED_SECRET_ARN": ref("SessionSeedSecret"),
                "ISSUER": ref("PrimaryIssuer"),
                "CLIENT_ID": ref("PrimaryClientId"),
                "SUBJECT": ref("JudgeSubject"),
                "OWNER_ID": ref("JudgeOwnerId"),
                "COGNITO_ORIGIN": ref("CognitoOrigin"),
                "APPLICATION_ORIGIN": ref("ApplicationOrigin"),
                "CAMPAIGN_ID": ref("CampaignId"),
                "CAMPAIGN_EXPIRES_AT_EPOCH": ref("CampaignExpiresAtEpoch"),
                "DURATION_SECONDS": "14400",
            }
            environment = {
                "MR_LISTER_JUDGE_SESSION_" + key: value for key, value in settings.items()
            }
        else:
            environment = {
                "MR_LISTER_JUDGE_CLEANUP_CONFIG": sub(
                    '{"campaign_id":"${CampaignId}","judge_owner_id":"${JudgeOwnerId}",'
                    '"primary_owner_id":"${PrimaryOwnerId}",'
                    '"printify_shop_id":${PrintifyShopId},"campaign_started_at":"${CampaignStartedAt}",'
                    '"source_table_name":"${SourceTableName}","cleanup_table_name":"${CleanupTable}",'
                    '"printify_secret_arn":"${PrintifySecretArn}","dry_run":${CleanupDryRun},'
                    '"activation_fingerprint":${ActivationJson},"max_jobs_per_run":1,"page_size":25}',
                    {
                        "ActivationJson": {
                            "Fn::If": [
                                "HasCleanupFingerprint",
                                sub('"${CleanupActivationFingerprint}"'),
                                "null",
                            ]
                        }
                    },
                )
            }
        resources[f"{kind}Function"] = {
            "Type": "AWS::Lambda::Function",
            "Properties": {
                "FunctionName": sub(f"mr-lister-judge-${{EnvironmentName}}-{kind.lower()}"),
                "Runtime": "python3.12",
                "Architectures": ["arm64"],
                "Handler": (f"mr_lister.judge_{kind.lower()}.entrypoint.lambda_handler"),
                "Code": {
                    "S3Bucket": ref("CodeBucket"),
                    "S3Key": ref("CodeKey"),
                    "S3ObjectVersion": ref("CodeVersion"),
                },
                "Role": arn(f"{kind}Role"),
                "MemorySize": 256,
                "Timeout": 60 if kind == "Cleanup" else 20,
                "ReservedConcurrentExecutions": 1 if kind == "Cleanup" else 3,
                "Environment": {"Variables": environment},
            },
        }
        resources[f"{kind}ErrorAlarm"] = {
            "Type": "AWS::CloudWatch::Alarm",
            "Properties": {
                "AlarmName": sub(
                    f"mr-lister-phase6-${{EnvironmentName}}-judge-{kind.lower()}-errors"
                ),
                "AlarmDescription": "Judge service failed; inspect logs and cleanup receipts",
                "Namespace": "AWS/Lambda",
                "MetricName": "Errors",
                "Dimensions": [{"Name": "FunctionName", "Value": ref(f"{kind}Function")}],
                "Statistic": "Sum",
                "Period": 60,
                "EvaluationPeriods": 1,
                "Threshold": 0,
                "ComparisonOperator": "GreaterThanThreshold",
                "TreatMissingData": "notBreaching",
                "AlarmActions": [ref("AlarmTopicArn")],
            },
        }
    resources["SessionApiLogs"] = {
        "Type": "AWS::Logs::LogGroup",
        "Properties": {
            "LogGroupName": sub("/aws/apigateway/mr-lister-judge-${EnvironmentName}"),
            "RetentionInDays": 30,
        },
    }
    resources["SessionApi"] = {
        "Type": "AWS::ApiGatewayV2::Api",
        "Properties": {"Name": sub("mr-lister-judge-${EnvironmentName}"), "ProtocolType": "HTTP"},
    }
    resources["SessionApiIntegration"] = {
        "Type": "AWS::ApiGatewayV2::Integration",
        "Properties": {
            "ApiId": ref("SessionApi"),
            "IntegrationType": "AWS_PROXY",
            "IntegrationMethod": "POST",
            "IntegrationUri": arn("SessionFunction"),
            "PayloadFormatVersion": "2.0",
            "TimeoutInMillis": 20000,
        },
    }
    for action in ("redeem", "refresh", "logout"):
        resources[f"SessionRoute{action.title()}"] = {
            "Type": "AWS::ApiGatewayV2::Route",
            "Properties": {
                "ApiId": ref("SessionApi"),
                "RouteKey": f"POST /v1/judge-session/{action}",
                "AuthorizationType": "NONE",
                "Target": {"Fn::Join": ["/", ["integrations", ref("SessionApiIntegration")]]},
            },
        }
    resources["SessionApiStage"] = {
        "Type": "AWS::ApiGatewayV2::Stage",
        "Properties": {
            "ApiId": ref("SessionApi"),
            "StageName": "$default",
            "AutoDeploy": True,
            "DefaultRouteSettings": {
                "ThrottlingBurstLimit": 5,
                "ThrottlingRateLimit": 2,
                "DetailedMetricsEnabled": True,
            },
            "AccessLogSettings": {
                "DestinationArn": arn("SessionApiLogs"),
                "Format": (
                    '{"requestId":"$context.requestId","routeKey":"$context.routeKey",'
                    '"status":"$context.status"}'
                ),
            },
        },
    }
    resources["SessionApiInvoke"] = {
        "Type": "AWS::Lambda::Permission",
        "Properties": {
            "Action": "lambda:InvokeFunction",
            "FunctionName": ref("SessionFunction"),
            "Principal": "apigateway.amazonaws.com",
            "SourceAccount": ref("AWS::AccountId"),
            "SourceArn": sub(
                "arn:${AWS::Partition}:execute-api:${AWS::Region}:${AWS::AccountId}:${SessionApi}/*/POST/v1/judge-session/*"
            ),
        },
    }
    resources["SessionApiFailureAlarm"] = {
        "Type": "AWS::CloudWatch::Alarm",
        "Properties": {
            "AlarmName": sub("mr-lister-phase6-${EnvironmentName}-judge-session-api-errors"),
            "AlarmDescription": "Judge entry returned a server error; broker may need reseeding",
            "Namespace": "AWS/ApiGateway",
            "MetricName": "5xx",
            "Dimensions": [{"Name": "ApiId", "Value": ref("SessionApi")}],
            "Statistic": "Sum",
            "Period": 60,
            "EvaluationPeriods": 1,
            "Threshold": 0,
            "ComparisonOperator": "GreaterThanThreshold",
            "TreatMissingData": "notBreaching",
            "AlarmActions": [ref("AlarmTopicArn")],
        },
    }
    resources["CleanupSchedule"] = {
        "Type": "AWS::Events::Rule",
        "Properties": {
            "Name": sub("mr-lister-judge-${EnvironmentName}-cleanup"),
            "ScheduleExpression": "rate(1 minute)",
            "State": {"Fn::If": ["RunCleanup", "ENABLED", "DISABLED"]},
            "Targets": [
                {
                    "Id": "Cleanup",
                    "Arn": arn("CleanupFunction"),
                    "Input": "{}",
                    "RetryPolicy": {"MaximumEventAgeInSeconds": 300, "MaximumRetryAttempts": 2},
                }
            ],
        },
    }
    resources["CleanupScheduleInvoke"] = {
        "Type": "AWS::Lambda::Permission",
        "Properties": {
            "Action": "lambda:InvokeFunction",
            "FunctionName": ref("CleanupFunction"),
            "Principal": "events.amazonaws.com",
            "SourceAccount": ref("AWS::AccountId"),
            "SourceArn": arn("CleanupSchedule"),
        },
    }
    resources["CleanupScheduleFailureAlarm"] = {
        "Type": "AWS::CloudWatch::Alarm",
        "Properties": {
            "AlarmName": sub("mr-lister-phase6-${EnvironmentName}-judge-cleanup-delivery-errors"),
            "Namespace": "AWS/Events",
            "MetricName": "FailedInvocations",
            "Dimensions": [{"Name": "RuleName", "Value": ref("CleanupSchedule")}],
            "Statistic": "Sum",
            "Period": 60,
            "EvaluationPeriods": 1,
            "Threshold": 0,
            "ComparisonOperator": "GreaterThanThreshold",
            "TreatMissingData": "notBreaching",
            "AlarmActions": [ref("AlarmTopicArn")],
        },
    }
    return {
        "AWSTemplateFormatVersion": "2010-09-09",
        "Description": "Judge link sessions and Printify cleanup; disabled by default",
        "Parameters": parameters,
        "Conditions": {
            "RunCleanup": {"Fn::Equals": [ref("CleanupScheduleEnabled"), "true"]},
            "HasCleanupFingerprint": {
                "Fn::Not": [{"Fn::Equals": [ref("CleanupActivationFingerprint"), ""]}]
            },
        },
        "Resources": resources,
        "Outputs": {
            "SessionApiHostname": {
                "Value": sub("${SessionApi}.execute-api.${AWS::Region}.${AWS::URLSuffix}")
            },
            "SessionSeedSecretArn": {"Value": ref("SessionSeedSecret")},
            "SessionTableName": {"Value": ref("SessionTable")},
            "CleanupTableName": {"Value": ref("CleanupTable")},
        },
    }


def patch_application_template(baseline: dict, *, session_api_hostname: str) -> dict:
    """Add an exact no-cache cookie route; preserve seller auth and API transport."""
    if not re.fullmatch(
        r"[a-z0-9]{10}\.execute-api\.us-west-2\.amazonaws\.com", session_api_hostname
    ):
        raise ValueError("An exact regional judge API hostname is required")
    template = deepcopy(baseline)
    resources = template["Resources"]
    if resources["SellerUserPool"]["Properties"]["MfaConfiguration"] != "ON":
        raise ValueError("The owner's MFA must remain required")
    distribution = resources["SellerWebDistribution"]["Properties"]["DistributionConfig"]
    if (
        "JudgeSessionNoStoreCachePolicy" in resources
        or any(
            item["PathPattern"].startswith("/v1/judge-session")
            for item in distribution["CacheBehaviors"]
        )
        or any(item["Id"] == "JudgeSessionApi" for item in distribution["Origins"])
    ):
        raise ValueError("Judge session transport already exists")
    policies = {
        "CookiesConfig": {"CookieBehavior": "whitelist", "Cookies": [COOKIE_NAME]},
        "EnableAcceptEncodingBrotli": False,
        "EnableAcceptEncodingGzip": False,
        "HeadersConfig": {"HeaderBehavior": "whitelist", "Headers": ["Content-Type", "Origin"]},
        "QueryStringsConfig": {"QueryStringBehavior": "none"},
    }
    # A zero-TTL cache policy cannot forward headers/cookies. Forward them through a
    # dedicated origin-request policy instead; all three TTLs stay exactly zero.
    resources["JudgeSessionNoStoreCachePolicy"] = {
        "Type": "AWS::CloudFront::CachePolicy",
        "Properties": {
            "CachePolicyConfig": {
                "Name": sub("mr-lister-judge-${EnvironmentName}-${AWS::Region}-no-cache"),
                "DefaultTTL": 0,
                "MinTTL": 0,
                "MaxTTL": 0,
                "ParametersInCacheKeyAndForwardedToOrigin": {
                    **policies,
                    "CookiesConfig": {"CookieBehavior": "none"},
                    "HeadersConfig": {"HeaderBehavior": "none"},
                },
            }
        },
    }
    resources["JudgeSessionOriginRequestPolicy"] = {
        "Type": "AWS::CloudFront::OriginRequestPolicy",
        "Properties": {
            "OriginRequestPolicyConfig": {
                "Name": sub("mr-lister-judge-${EnvironmentName}-${AWS::Region}-session-cookie"),
                **{
                    key: value
                    for key, value in policies.items()
                    if not key.startswith("EnableAccept")
                },
            }
        },
    }
    distribution["Origins"].append(
        {
            "Id": "JudgeSessionApi",
            "DomainName": session_api_hostname,
            "CustomOriginConfig": {
                "HTTPSPort": 443,
                "OriginProtocolPolicy": "https-only",
                "OriginSSLProtocols": ["TLSv1.2"],
                "OriginReadTimeout": 30,
                "OriginKeepaliveTimeout": 5,
            },
        }
    )
    broad = next(
        index
        for index, item in enumerate(distribution["CacheBehaviors"])
        if item["PathPattern"] == "/v1/*"
    )
    distribution["CacheBehaviors"].insert(
        broad,
        {
            "PathPattern": "/v1/judge-session/*",
            "TargetOriginId": "JudgeSessionApi",
            "ViewerProtocolPolicy": "https-only",
            "AllowedMethods": ["DELETE", "GET", "HEAD", "OPTIONS", "PATCH", "POST", "PUT"],
            "CachedMethods": ["GET", "HEAD"],
            "Compress": False,
            "CachePolicyId": ref("JudgeSessionNoStoreCachePolicy"),
            "OriginRequestPolicyId": ref("JudgeSessionOriginRequestPolicy"),
            "ResponseHeadersPolicyId": ref("SellerWebNoStoreResponseHeadersPolicy"),
        },
    )
    return template


def write_private(path: Path, content: dict) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as stream:
        json.dump(content, stream, indent=2)
        stream.write("\n")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--baseline-template", type=Path)
    parser.add_argument("--session-api-hostname")
    args = parser.parse_args()
    if bool(args.baseline_template) != bool(args.session_api_hostname):
        parser.error("A baseline and hostname must be supplied together for the edge patch")
    result = (
        build_template()
        if args.baseline_template is None
        else patch_application_template(
            json.loads(args.baseline_template.read_text()),
            session_api_hostname=args.session_api_hostname,
        )
    )
    write_private(args.output, result)


if __name__ == "__main__":
    main()
