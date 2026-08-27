from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
BOOTSTRAP = ROOT / "infra/phase6/web-edge-role-bootstrap.json"
RETAINED_EXECUTION_ROLE = "mr-lister-phase6-runtime-cfn-dev"
APPLICATION_ORIGIN = "https://massskutiny.com"
CERTIFICATE_ARN = (
    "arn:aws:acm:us-east-1:384627057108:certificate/28b8cddb-a0d7-4dc8-98de-26fd87cb5b79"
)
TARGET_TEMPLATE_FINGERPRINT = "0ab2c8f016afb513d7de5dd65aefd975eeaf827800aa19ceb31d0f64c02748c8"
CHANGE_SET_NAME = "mr-lister-phase6-dev-web-edge-0ab2c8f016af"
REVIEWED_CHANGE_SET_ID = (
    "arn:aws:cloudformation:us-west-2:384627057108:changeSet/"
    f"{CHANGE_SET_NAME}/12345678-1234-1234-1234-123456789abc"
)


def _template() -> dict[str, Any]:
    value = json.loads(BOOTSTRAP.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def _statements(policy_properties: dict[str, Any]) -> dict[str, dict[str, Any]]:
    documents = _expand_policy_document(policy_properties["PolicyDocument"])
    assert len(documents) == 1
    statements = documents[0]["Statement"]
    return {statement["Sid"]: statement for statement in statements}


def _actions(statement: dict[str, Any]) -> set[str]:
    value = statement["Action"]
    return {value} if isinstance(value, str) else set(value)


def _all_permission_statements(template: dict[str, Any]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for resource in template["Resources"].values():
        properties = resource.get("Properties", {})
        for key in ("AssumeRolePolicyDocument", "PolicyDocument"):
            if key in properties:
                for document in _expand_policy_document(properties[key]):
                    result.extend(document["Statement"])
        for policy in properties.get("Policies", []):
            for document in _expand_policy_document(policy["PolicyDocument"]):
                result.extend(document["Statement"])
    return result


def _expand_policy_document(document: dict[str, Any]) -> list[dict[str, Any]]:
    if set(document) != {"Fn::If"}:
        return [document]
    condition, true_document, false_document = document["Fn::If"]
    assert condition == "ExecutionAuthorized"
    return [true_document, false_document]


def _control_documents(template: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    policy = template["Resources"]["WebEdgeChangeSetControlPolicy"]
    assert policy["Type"] == "AWS::IAM::ManagedPolicy"
    assert "Condition" not in policy
    documents = _expand_policy_document(policy["Properties"]["PolicyDocument"])
    assert len(documents) == 2
    return documents[1], documents[0]


def _document_statements(document: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {statement["Sid"]: statement for statement in document["Statement"]}


def test_bootstrap_is_exact_temporary_root_applied_contract() -> None:
    template = _template()

    assert set(template) == {
        "AWSTemplateFormatVersion",
        "Conditions",
        "Description",
        "Metadata",
        "Outputs",
        "Parameters",
        "Resources",
        "Rules",
    }
    assert template["Metadata"]["MrListerDeployment"] == {
        "DeploymentClass": "WEB_EDGE_UPDATE_BOOTSTRAP_ONLY",
        "Environment": "dev",
        "Region": "us-west-2",
        "RootApplied": True,
        "RetainedExecutionRoleName": RETAINED_EXECUTION_ROLE,
        "Sequence": [
            "PREPARE_EXACT_CHANGE_SET",
            "REVIEW_AND_AUTHORIZE_EXECUTION",
            "DELETE_BOOTSTRAP_TO_DETACH_TEMPORARY_AUTHORITY",
        ],
    }

    parameters = template["Parameters"]
    assert parameters["BootstrapStage"]["AllowedValues"] == ["PREPARE", "EXECUTE"]
    assert "Default" not in parameters["BootstrapStage"]
    assert "Default" not in parameters["NotAfter"]
    assert parameters["ApplicationOrigin"]["AllowedValues"] == [APPLICATION_ORIGIN]
    assert parameters["ApplicationCertificateArn"]["AllowedValues"] == [CERTIFICATE_ARN]
    assert parameters["FoundationStackId"]["AllowedValues"] == [
        "arn:aws:cloudformation:us-west-2:384627057108:stack/mr-lister-phase6-dev/"
        "f3456970-9fdc-11f1-b448-06b81627db1d"
    ]
    assert parameters["ReleaseFingerprint"]["AllowedValues"] == [
        "0c6211a5b0244e9c86d635e6c02e7bc49e5e948d68895b4aaa982c0b0b2e187b"
    ]
    assert parameters["LambdaArchiveSha256"]["AllowedValues"] == [
        "baf152b732ce8574b6a6925bae7ab4ff849c1b83d4137076c52c6682553f9d48"
    ]
    assert parameters["LambdaVersionId"]["AllowedValues"] == ["pHutjLzKNpukwJ75Qs9s8YzXUAvgxZuS"]
    assert parameters["TargetTemplateFingerprint"]["AllowedValues"] == [TARGET_TEMPLATE_FINGERPRINT]
    assert parameters["ExactChangeSetName"]["AllowedValues"] == [CHANGE_SET_NAME]
    assert "Default" not in parameters["ReviewedChangeSetId"]
    reviewed_pattern = parameters["ReviewedChangeSetId"]["AllowedPattern"]
    assert re.fullmatch(reviewed_pattern, "PREPARE_NOT_REVIEWED")
    assert re.fullmatch(reviewed_pattern, REVIEWED_CHANGE_SET_ID)
    assert (
        re.fullmatch(reviewed_pattern, REVIEWED_CHANGE_SET_ID.replace(CHANGE_SET_NAME, "other"))
        is None
    )
    assert template["Conditions"] == {
        "ExecutionAuthorized": {"Fn::Equals": [{"Ref": "BootstrapStage"}, "EXECUTE"]}
    }

    assertions = template["Rules"]["OnlyTargetAccountAndRegion"]["Assertions"]
    assert assertions == [
        {
            "Assert": {"Fn::Equals": [{"Ref": "AWS::AccountId"}, "384627057108"]},
            "AssertDescription": "This bootstrap is fixed to AWS account 384627057108",
        },
        {
            "Assert": {"Fn::Equals": [{"Ref": "AWS::Region"}, "us-west-2"]},
            "AssertDescription": "This bootstrap must be created in us-west-2",
        },
    ]
    assert template["Rules"]["PrepareRequiresUnreviewedSentinel"] == {
        "RuleCondition": {"Fn::Equals": [{"Ref": "BootstrapStage"}, "PREPARE"]},
        "Assertions": [
            {
                "Assert": {
                    "Fn::Equals": [
                        {"Ref": "ReviewedChangeSetId"},
                        "PREPARE_NOT_REVIEWED",
                    ]
                },
                "AssertDescription": "PREPARE must use the unreviewed change-set sentinel",
            }
        ],
    }
    assert template["Rules"]["ExecuteRequiresReviewedChangeSetArn"] == {
        "RuleCondition": {"Fn::Equals": [{"Ref": "BootstrapStage"}, "EXECUTE"]},
        "Assertions": [
            {
                "Assert": {
                    "Fn::Not": [
                        {
                            "Fn::Equals": [
                                {"Ref": "ReviewedChangeSetId"},
                                "PREPARE_NOT_REVIEWED",
                            ]
                        }
                    ]
                },
                "AssertDescription": "EXECUTE requires the exact full reviewed change-set ARN",
            }
        ],
    }


def test_bootstrap_attaches_temporary_policies_without_owning_retained_role() -> None:
    resources = _template()["Resources"]

    assert set(resources) == {
        "DeveloperAssumeWebEdgeDeployerPolicy",
        "TemporaryCloudFrontExecutionPolicy",
        "TemporaryWebEdgeExecutionPolicy",
        "TemporaryWebObservabilityExecutionPolicy",
        "WebEdgeChangeSetControlPolicy",
        "WebEdgeUpdateDeployerRole",
    }
    assert all(
        resource.get("Properties", {}).get("RoleName") != RETAINED_EXECUTION_ROLE
        for resource in resources.values()
    )
    for name in (
        "TemporaryCloudFrontExecutionPolicy",
        "TemporaryWebEdgeExecutionPolicy",
        "TemporaryWebObservabilityExecutionPolicy",
    ):
        resource = resources[name]
        assert resource["Type"] == "AWS::IAM::ManagedPolicy"
        assert resource["Properties"]["Roles"] == [RETAINED_EXECUTION_ROLE]
        assert "DeletionPolicy" not in resource
        assert "UpdateReplacePolicy" not in resource

    outputs = _template()["Outputs"]
    assert outputs["DefaultChangeSetExecutionAuthorization"]["Value"] == (
        "BLOCKED_UNLESS_STAGE_EXECUTE"
    )
    temporary_refs = outputs["TemporaryExecutionPolicyArns"]["Value"]["Fn::Join"][1]
    assert temporary_refs == [
        {"Ref": "TemporaryWebEdgeExecutionPolicy"},
        {"Ref": "TemporaryCloudFrontExecutionPolicy"},
        {"Ref": "TemporaryWebObservabilityExecutionPolicy"},
    ]
    assert outputs["WebEdgeChangeSetControlPolicyArn"]["Value"] == {
        "Ref": "WebEdgeChangeSetControlPolicy"
    }
    assert outputs["ReviewedChangeSetId"]["Value"] == {"Ref": "ReviewedChangeSetId"}


def test_every_temporary_permission_expires_and_managed_policies_fit_iam_limit() -> None:
    template = _template()
    resources = template["Resources"]

    for statement in _all_permission_statements(template):
        assert statement["Condition"]["DateLessThan"] == {"aws:CurrentTime": {"Ref": "NotAfter"}}

    for name, resource in resources.items():
        policy = resource.get("Properties", {}).get("PolicyDocument")
        if policy is None:
            continue
        for document in _expand_policy_document(policy):
            serialized = json.dumps(document, separators=(",", ":"))
            assert len(serialized) <= 6144, name


def test_private_web_bucket_authority_has_no_object_data_plane() -> None:
    properties = _template()["Resources"]["TemporaryWebEdgeExecutionPolicy"]["Properties"]
    statements = _statements(properties)
    create = statements["CreateExactPrivateWebAssetBucket"]
    configure = statements["ConfigureAndRollbackExactPrivateWebAssetBucket"]

    bucket_arn = "arn:${AWS::Partition}:s3:::mr-lister-phase6-web-dev-${AWS::AccountId}-us-west-2"
    assert create["Action"] == "s3:CreateBucket"
    assert create["Resource"] == {"Fn::Sub": bucket_arn}
    assert create["Condition"]["StringEquals"] == {"s3:LocationConstraint": "us-west-2"}
    assert configure["Resource"] == {"Fn::Sub": bucket_arn}
    assert {
        "s3:DeleteBucket",
        "s3:GetBucketOwnershipControls",
        "s3:GetBucketPublicAccessBlock",
        "s3:GetBucketVersioning",
        "s3:GetEncryptionConfiguration",
        "s3:PutBucketOwnershipControls",
        "s3:PutBucketPolicy",
        "s3:PutBucketPublicAccessBlock",
        "s3:PutBucketTagging",
        "s3:PutBucketVersioning",
        "s3:PutEncryptionConfiguration",
    } <= _actions(configure)
    assert not any("Object" in action for action in _actions(create) | _actions(configure))


def test_cloudfront_creation_is_temporary_and_management_stays_in_account() -> None:
    resource = _template()["Resources"]["TemporaryCloudFrontExecutionPolicy"]
    properties = resource["Properties"]
    statements = _statements(properties)
    create = statements["CreateOnlyReviewedCloudFrontResourceKinds"]
    manage = statements["ManageOnlyAccountCloudFrontWebEdgeResources"]

    assert _actions(create) == {
        "cloudfront:CreateCachePolicy",
        "cloudfront:CreateDistribution",
        "cloudfront:CreateDistributionWithTags",
        "cloudfront:CreateFunction",
        "cloudfront:CreateOriginAccessControl",
        "cloudfront:CreateResponseHeadersPolicy",
    }
    # The local cfn-lint IAM catalog omits the real CloudFront
    # CreateDistributionWithTags API action. Keep the suppression on this one resource only.
    assert resource["Metadata"] == {"cfn-lint": {"config": {"ignore_checks": ["W3037"]}}}
    assert create["Resource"] == "*"
    assert manage["Resource"] == [
        {"Fn::Sub": "arn:${AWS::Partition}:cloudfront::${AWS::AccountId}:cache-policy/*"},
        {"Fn::Sub": "arn:${AWS::Partition}:cloudfront::${AWS::AccountId}:distribution/*"},
        {
            "Fn::Sub": (
                "arn:${AWS::Partition}:cloudfront::${AWS::AccountId}:"
                "function/mr-lister-phase6-dev-*"
            )
        },
        {
            "Fn::Sub": (
                "arn:${AWS::Partition}:cloudfront::${AWS::AccountId}:origin-access-control/*"
            )
        },
        {
            "Fn::Sub": (
                "arn:${AWS::Partition}:cloudfront::${AWS::AccountId}:response-headers-policy/*"
            )
        },
    ]
    assert "cloudfront:CreateInvalidation" not in _actions(create) | _actions(manage)


def test_cognito_authority_is_tagged_control_plane_without_seller_admin() -> None:
    properties = _template()["Resources"]["TemporaryWebEdgeExecutionPolicy"]["Properties"]
    statements = _statements(properties)
    create = statements["CreateOnlyTaggedSellerUserPool"]
    manage = statements["ManageOnlyTaggedSellerUserPoolsAndChildren"]

    assert create["Action"] == "cognito-idp:CreateUserPool"
    assert create["Resource"] == "*"
    assert create["Condition"]["StringEquals"] == {
        "aws:RequestTag/DataClassification": "SellerIdentity",
        "aws:RequestTag/Environment": "dev",
        "aws:RequestTag/Project": "MrLister",
        "aws:RequestedRegion": "us-west-2",
    }
    assert manage["Resource"] == {
        "Fn::Sub": ("arn:${AWS::Partition}:cognito-idp:us-west-2:${AWS::AccountId}:userpool/*")
    }
    assert manage["Condition"]["StringEquals"] == {
        "aws:ResourceTag/DataClassification": "SellerIdentity",
        "aws:ResourceTag/Environment": "dev",
        "aws:ResourceTag/Project": "MrLister",
        "aws:RequestedRegion": "us-west-2",
    }
    assert not any(
        action.startswith("cognito-idp:Admin") for action in _actions(create) | _actions(manage)
    )
    assert "iam:CreateServiceLinkedRole" not in _actions(create) | _actions(manage)


def test_http_api_and_access_log_authority_is_regional_and_path_scoped() -> None:
    properties = _template()["Resources"]["TemporaryWebEdgeExecutionPolicy"]["Properties"]
    statements = _statements(properties)
    api = statements["ManageOnlyRegionalSellerHttpApi"]
    log_group = statements["ManageOnlySellerApiAccessLogGroup"]
    log_delivery = statements["ConfigureOnlyRegionalHttpApiLogDelivery"]

    assert _actions(api) == {
        "apigateway:DELETE",
        "apigateway:GET",
        "apigateway:PATCH",
        "apigateway:POST",
        "apigateway:PUT",
    }
    assert api["Resource"] == [
        {"Fn::Sub": "arn:${AWS::Partition}:apigateway:us-west-2::/apis"},
        {"Fn::Sub": "arn:${AWS::Partition}:apigateway:us-west-2::/apis/*"},
        {"Fn::Sub": "arn:${AWS::Partition}:apigateway:us-west-2::/tags/*"},
    ]
    assert api["Condition"]["StringEquals"] == {"aws:RequestedRegion": "us-west-2"}
    assert log_group["Resource"]["Fn::Sub"].endswith(
        ":log-group:/aws/apigateway/mr-lister-phase6-dev-seller-api"
    )
    assert _actions(log_delivery) == {
        "logs:CreateLogDelivery",
        "logs:DeleteLogDelivery",
        "logs:DescribeResourcePolicies",
        "logs:GetLogDelivery",
        "logs:ListLogDeliveries",
        "logs:PutResourcePolicy",
        "logs:UpdateLogDelivery",
    }
    assert log_delivery["Resource"] == "*"
    assert log_delivery["Condition"]["StringEquals"] == {"aws:RequestedRegion": "us-west-2"}


def test_observability_authority_is_tag_or_name_scoped() -> None:
    properties = _template()["Resources"]["TemporaryWebObservabilityExecutionPolicy"]["Properties"]
    statements = _statements(properties)

    key_create = statements["CreateOnlyTaggedOperationalAlarmKey"]
    key_manage = statements["ManageOnlyTaggedOperationalAlarmKeys"]
    assert key_create["Action"] == "kms:CreateKey"
    assert key_create["Condition"]["StringEquals"] == {
        "aws:RequestTag/DataClassification": "OperationalAlarmTransport",
        "aws:RequestTag/Environment": "dev",
        "aws:RequestTag/Project": "MrLister",
        "aws:RequestedRegion": "us-west-2",
    }
    assert key_create["Condition"]["ForAllValues:StringEquals"] == {
        "aws:TagKeys": ["DataClassification", "Environment", "Project"]
    }
    assert key_manage["Resource"] == {
        "Fn::Sub": "arn:${AWS::Partition}:kms:us-west-2:${AWS::AccountId}:key/*"
    }
    assert key_manage["Condition"]["StringEquals"] == {
        "aws:ResourceTag/DataClassification": "OperationalAlarmTransport",
        "aws:ResourceTag/Environment": "dev",
        "aws:ResourceTag/Project": "MrLister",
        "aws:RequestedRegion": "us-west-2",
    }

    topic = statements["ManageOnlyExactOperationalAlarmTopic"]
    alarms = statements["ManageOnlyNamedPhase6Alarms"]
    assert topic["Resource"]["Fn::Sub"].endswith(":mr-lister-phase6-dev-operational-alarms")
    assert alarms["Resource"]["Fn::Sub"].endswith(":alarm:mr-lister-phase6-dev-*")
    assert "kms:ScheduleKeyDeletion" in _actions(key_manage)
    assert "sns:Publish" not in _actions(topic)
    assert "cloudwatch:SetAlarmState" not in _actions(alarms)


def test_prepare_deployer_is_exact_template_version_and_cannot_execute() -> None:
    template = _template()
    role = template["Resources"]["WebEdgeUpdateDeployerRole"]
    properties = role["Properties"]
    control = template["Resources"]["WebEdgeChangeSetControlPolicy"]
    prepare_document, _execute_document = _control_documents(template)
    statements = _document_statements(prepare_document)

    assert properties["RoleName"] == "mr-lister-phase6-web-edge-update-deployer-dev"
    assert "Policies" not in properties
    assert properties["AssumeRolePolicyDocument"]["Statement"] == [
        {
            "Effect": "Allow",
            "Principal": {
                "AWS": {
                    "Fn::Sub": ("arn:${AWS::Partition}:iam::${AWS::AccountId}:user/mr-lister-dev")
                }
            },
            "Action": "sts:AssumeRole",
            "Condition": {"DateLessThan": {"aws:CurrentTime": {"Ref": "NotAfter"}}},
        }
    ]
    assert "Condition" not in control
    assert control["Properties"]["Roles"] == [{"Ref": "WebEdgeUpdateDeployerRole"}]
    create = statements["CreateExactReviewedWebEdgeUpdate"]
    assert create["Resource"] == [
        {"Ref": "FoundationStackId"},
        {
            "Fn::Sub": (
                "arn:${AWS::Partition}:cloudformation:us-west-2:aws:transform/Serverless-2016-10-31"
            )
        },
    ]
    assert create["Condition"]["StringEquals"] == {
        "aws:RequestTag/DeploymentClass": "FOUNDATION_ONLY",
        "aws:RequestTag/Environment": "dev",
        "aws:RequestTag/Project": "MrLister",
        "cloudformation:ChangeSetName": {"Ref": "ExactChangeSetName"},
        "cloudformation:RoleArn": {
            "Fn::Sub": (
                "arn:${AWS::Partition}:iam::${AWS::AccountId}:role/mr-lister-phase6-runtime-cfn-dev"
            )
        },
        "cloudformation:TemplateUrl": {
            "Fn::Sub": (
                "https://mr-lister-phase6-artifacts-dev-${AWS::AccountId}-us-west-2."
                "s3.us-west-2.${AWS::URLSuffix}/private/deployments/cloudformation/"
                "web-edge/releases/${ReleaseFingerprint}/web-edge-template-"
                "${TargetTemplateFingerprint}.json?versionId=${WebTemplateVersionIdUrlEncoded}"
            )
        },
    }

    read = statements["ReadOnlyExactReviewedWebTemplateVersion"]
    assert read["Action"] == "s3:GetObjectVersion"
    assert read["Condition"]["StringEquals"] == {"s3:VersionId": {"Ref": "WebTemplateVersionId"}}
    prepare_actions = set().union(*(_actions(statement) for statement in statements.values()))
    assert "cloudformation:ExecuteChangeSet" not in prepare_actions
    assert "cloudformation:UpdateStack" not in prepare_actions
    assert "cloudformation:DeleteStack" not in prepare_actions


def test_execution_requires_separate_execute_stage_and_exact_change_set() -> None:
    template = _template()
    prepare_document, execute_document = _control_documents(template)
    prepare = _document_statements(prepare_document)
    execute = _document_statements(execute_document)

    assert set(execute) == {
        "ExecuteOnlyExactReviewedWebEdgeChangeSet",
        "ReadOnlyExactPhase6Stack",
        "ReadOnlyExactReviewedChangeSet",
    }
    execution = execute["ExecuteOnlyExactReviewedWebEdgeChangeSet"]
    assert execution == {
        "Sid": "ExecuteOnlyExactReviewedWebEdgeChangeSet",
        "Effect": "Allow",
        "Action": "cloudformation:ExecuteChangeSet",
        "Resource": {"Ref": "ReviewedChangeSetId"},
        "Condition": {
            "DateLessThan": {"aws:CurrentTime": {"Ref": "NotAfter"}},
        },
    }
    assert execute["ReadOnlyExactReviewedChangeSet"]["Resource"] == [
        {"Ref": "FoundationStackId"},
        {"Ref": "ReviewedChangeSetId"},
    ]
    execute_actions = set().union(*(_actions(statement) for statement in execute.values()))
    assert execute_actions == {
        "cloudformation:DescribeChangeSet",
        "cloudformation:DescribeStacks",
        "cloudformation:ExecuteChangeSet",
        "cloudformation:GetTemplate",
        "cloudformation:ListStackResources",
    }
    assert not execute_actions.intersection(
        {
            "cloudformation:CreateChangeSet",
            "cloudformation:DeleteChangeSet",
            "iam:PassRole",
            "s3:GetObjectVersion",
        }
    )
    prepare_actions = set().union(*(_actions(statement) for statement in prepare.values()))
    assert "cloudformation:ExecuteChangeSet" not in prepare_actions
    assert {
        "cloudformation:CreateChangeSet",
        "cloudformation:DeleteChangeSet",
        "iam:PassRole",
        "s3:GetObjectVersion",
    } <= prepare_actions


def test_no_runtime_data_plane_provider_dns_or_asset_upload_authority() -> None:
    template = _template()
    actions = set().union(
        *(_actions(statement) for statement in _all_permission_statements(template))
    )

    forbidden_exact = {
        "cloudformation:CreateStack",
        "cloudformation:DeleteStack",
        "cloudformation:UpdateStack",
        "cloudfront:CreateInvalidation",
        "dynamodb:GetItem",
        "dynamodb:PutItem",
        "dynamodb:Query",
        "dynamodb:Scan",
        "dynamodb:TransactWriteItems",
        "execute-api:Invoke",
        "lambda:InvokeFunction",
        "s3:DeleteObject",
        "s3:GetObject",
        "s3:PutObject",
        "sns:Publish",
        "states:StartExecution",
        "states:StopExecution",
    }
    assert not actions & forbidden_exact
    assert not any(action.startswith("bedrock") for action in actions)
    assert not any(action.startswith("route53:") for action in actions)
    assert not any(action.startswith("secretsmanager:") for action in actions)
    assert not any(action.startswith("cognito-idp:Admin") for action in actions)

    pass_role_statements = [
        statement
        for statement in _all_permission_statements(template)
        if "iam:PassRole" in _actions(statement)
    ]
    assert len(pass_role_statements) == 1
    assert pass_role_statements[0]["Resource"] == {
        "Fn::Sub": (
            "arn:${AWS::Partition}:iam::${AWS::AccountId}:role/mr-lister-phase6-runtime-cfn-dev"
        )
    }
    assert pass_role_statements[0]["Condition"]["StringEquals"] == {
        "iam:PassedToService": "cloudformation.amazonaws.com"
    }
