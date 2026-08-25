from __future__ import annotations

import json
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
BOOTSTRAP_PATH = ROOT / "infra/phase6/runtime-update-bootstrap.json"
SAM_TRANSFORM_ARN = (
    "arn:${AWS::Partition}:cloudformation:us-west-2:aws:transform/Serverless-2016-10-31"
)
CORE_FUNCTION_ARN_PREFIX = "arn:${AWS::Partition}:lambda:us-west-2:${AWS::AccountId}:function:"
CAPPED_CORE_FUNCTION_NAMES = (
    "mr-lister-phase6-dev-execution-recovery",
    "mr-lister-phase6-dev-source-retention",
    "mr-lister-phase6-dev-terminal-cleanup",
)


def _bootstrap() -> dict[str, Any]:
    value = json.loads(BOOTSTRAP_PATH.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def _statements(policy: dict[str, Any]) -> dict[str, dict[str, Any]]:
    statements = policy["PolicyDocument"]["Statement"]
    return {statement["Sid"]: statement for statement in statements}


def test_bootstrap_has_closed_two_stage_identity_contract() -> None:
    template = _bootstrap()

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
        "DeploymentClass": "CORE_RUNTIME_UPDATE_BOOTSTRAP_ONLY",
        "Environment": "dev",
        "Region": "us-west-2",
        "RootApplied": True,
        "Sequence": ["STAGE_A_UPLOAD_AND_FREEZE", "STAGE_B_EXACT_VERSION_DEPLOYER"],
    }
    parameters = template["Parameters"]
    assert {
        "CoreTemplateVersionId",
        "CoreTemplateVersionIdUrlEncoded",
        "ExactChangeSetName",
        "LambdaVersionId",
        "TargetTemplateFingerprint",
    } <= set(parameters)
    for name in (
        "CoreTemplateVersionId",
        "CoreTemplateVersionIdUrlEncoded",
        "ExactChangeSetName",
        "LambdaVersionId",
        "TargetTemplateFingerprint",
    ):
        assert parameters[name]["Default"] == "PENDING"
    assert template["Conditions"] == {
        "StageAUpload": {"Fn::Equals": [{"Ref": "CoreTemplateVersionId"}, "PENDING"]},
        "StageBExact": {"Fn::Not": [{"Fn::Equals": [{"Ref": "CoreTemplateVersionId"}, "PENDING"]}]},
    }
    assert (
        template["Rules"]["VersionsMoveTogether"]["Assertions"][0]["AssertDescription"]
        == "All Stage B immutable identities must replace PENDING together"
    )


def test_stage_a_matches_common_lambda_v2_names_keys_and_freeze() -> None:
    resources = _bootstrap()["Resources"]
    uploader = resources["DeveloperLambdaUploadPolicy"]
    reader = resources["DeveloperLambdaEvidenceReadbackPolicy"]
    freeze = resources["DeveloperLambdaReleaseFreezePolicy"]

    assert uploader["Condition"] == "StageAUpload"
    assert uploader["Properties"]["ManagedPolicyName"] == (
        "mr-lister-phase6-lambda-direct-uploader-dev"
    )
    assert uploader["Properties"]["Groups"] == ["mr-lister-developers"]
    upload_statements = _statements(uploader["Properties"])
    assert set(upload_statements) == {"ConditionallyUploadOnlyExactLambdaArchive"}
    lambda_upload = upload_statements["ConditionallyUploadOnlyExactLambdaArchive"]
    assert lambda_upload["Action"] == "s3:PutObject"
    assert lambda_upload["Condition"]["StringEquals"] == {
        "s3:if-none-match": "*",
        "s3:x-amz-server-side-encryption": "AES256",
    }
    lambda_resource = lambda_upload["Resource"]["Fn::Sub"]
    assert lambda_resource.endswith(
        "/private/deployments/lambda/releases/${ReleaseFingerprint}/"
        "phase6-lambda-${LambdaArchiveSha256}.zip"
    )

    assert reader["Properties"]["ManagedPolicyName"] == (
        "mr-lister-phase6-lambda-direct-evidence-reader-dev"
    )
    assert reader["Properties"]["Groups"] == ["mr-lister-developers"]
    reader_statements = _statements(reader["Properties"])
    assert reader_statements["DetachOnlyExactUploadAuthority"]["Condition"]["ArnEquals"][
        "iam:PolicyARN"
    ]["Fn::Sub"].endswith(":policy/mr-lister-phase6-lambda-direct-uploader-dev")
    assert reader_statements["AttachOnlyExactLambdaReleaseFreeze"]["Condition"]["ArnEquals"][
        "iam:PolicyARN"
    ] == {"Ref": "DeveloperLambdaReleaseFreezePolicy"}
    assert (
        reader_statements["ReadBackOnlyExactLambdaReleaseObject"]["Resource"]
        == (lambda_upload["Resource"])
    )
    assert all(
        "cloudformation/core" not in json.dumps(statement)
        for statement in reader_statements.values()
    )

    assert freeze["DeletionPolicy"] == "Retain"
    assert freeze["UpdateReplacePolicy"] == "Retain"
    assert freeze["Properties"]["ManagedPolicyName"] == (
        "mr-lister-phase6-lambda-release-freeze-dev"
    )
    assert freeze["Properties"]["PolicyDocument"] == {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "FreezeExactPhase6ReleaseObject",
                "Effect": "Deny",
                "Action": ["s3:DeleteObject", "s3:DeleteObjectVersion", "s3:PutObject"],
                "Resource": {
                    "Fn::Sub": (
                        "arn:${AWS::Partition}:s3:::mr-lister-phase6-artifacts-dev-"
                        "${AWS::AccountId}-us-west-2/private/deployments/lambda/releases/"
                        "${ReleaseFingerprint}/phase6-lambda-${LambdaArchiveSha256}.zip"
                    )
                },
            }
        ],
    }


def test_stage_a_template_key_is_release_bound_before_target_render() -> None:
    resources = _bootstrap()["Resources"]
    uploader = resources["DeveloperTemplateUploadPolicy"]
    uploader_statements = _statements(uploader["Properties"])
    assert uploader["Condition"] == "StageAUpload"
    assert uploader["Properties"]["ManagedPolicyName"] == (
        "mr-lister-phase6-core-template-direct-uploader-dev"
    )
    assert set(uploader_statements) == {"ConditionallyUploadOnlyReleaseBoundCoreTemplate"}
    template_upload = uploader_statements["ConditionallyUploadOnlyReleaseBoundCoreTemplate"]
    template_key = template_upload["Resource"]["Fn::Sub"]

    assert template_key.endswith(
        "/private/deployments/cloudformation/core/releases/${ReleaseFingerprint}/core-template.json"
    )
    assert "TargetTemplateFingerprint" not in template_key
    assert template_upload["Condition"]["StringEquals"]["s3:if-none-match"] == "*"
    template_freeze = resources["DeveloperTemplateReleaseFreezePolicy"]
    assert template_freeze["DeletionPolicy"] == "Retain"
    assert (
        template_freeze["Properties"]["PolicyDocument"]["Statement"][0]["Resource"]["Fn::Sub"]
        == template_key
    )

    reader = resources["DeveloperTemplateEvidenceReadbackPolicy"]
    assert reader["Properties"]["ManagedPolicyName"] == (
        "mr-lister-phase6-core-template-direct-evidence-reader-dev"
    )
    reader_statements = _statements(reader["Properties"])
    assert reader_statements["AttachOnlyExactCoreTemplateReleaseFreeze"]["Condition"]["ArnEquals"][
        "iam:PolicyARN"
    ] == {"Ref": "DeveloperTemplateReleaseFreezePolicy"}
    assert reader_statements["DetachOnlyExactCoreTemplateUploadAuthority"]["Condition"][
        "ArnEquals"
    ]["iam:PolicyARN"]["Fn::Sub"].endswith(
        ":policy/mr-lister-phase6-core-template-direct-uploader-dev"
    )
    assert all(
        "deployments/lambda" not in json.dumps(statement)
        for statement in reader_statements.values()
    )


def test_lambda_and_template_upload_authority_can_be_revoked_independently() -> None:
    resources = _bootstrap()["Resources"]
    lambda_reader = _statements(resources["DeveloperLambdaEvidenceReadbackPolicy"]["Properties"])
    template_reader = _statements(
        resources["DeveloperTemplateEvidenceReadbackPolicy"]["Properties"]
    )

    lambda_detach = lambda_reader["DetachOnlyExactUploadAuthority"]["Condition"]["ArnEquals"][
        "iam:PolicyARN"
    ]["Fn::Sub"]
    template_detach = template_reader["DetachOnlyExactCoreTemplateUploadAuthority"]["Condition"][
        "ArnEquals"
    ]["iam:PolicyARN"]["Fn::Sub"]
    assert lambda_detach.endswith(":policy/mr-lister-phase6-lambda-direct-uploader-dev")
    assert template_detach.endswith(":policy/mr-lister-phase6-core-template-direct-uploader-dev")
    assert lambda_detach != template_detach


def test_core_execution_role_is_separate_retained_and_lambda_version_scoped() -> None:
    role = _bootstrap()["Resources"]["CoreRuntimeExecutionRole"]
    properties = role["Properties"]

    assert role["DeletionPolicy"] == "Retain"
    assert role["UpdateReplacePolicy"] == "Retain"
    assert properties["RoleName"] == "mr-lister-phase6-runtime-cfn-dev"
    assert properties["AssumeRolePolicyDocument"] == {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Principal": {"Service": "cloudformation.amazonaws.com"},
                "Action": "sts:AssumeRole",
            }
        ],
    }
    assert len(properties["Policies"]) == 1
    assert properties["Policies"][0]["PolicyName"] == ("mr-lister-phase6-runtime-execution-dev")
    statements = _statements(properties["Policies"][0])
    assert statements["UseOnlyPhase6SamTransform"] == {
        "Sid": "UseOnlyPhase6SamTransform",
        "Effect": "Allow",
        "Action": "cloudformation:CreateChangeSet",
        "Resource": {"Fn::Sub": SAM_TRANSFORM_ARN},
    }
    assert statements["ManageOnlyCappedCoreLambdaConcurrency"] == {
        "Sid": "ManageOnlyCappedCoreLambdaConcurrency",
        "Effect": "Allow",
        "Action": [
            "lambda:DeleteFunctionConcurrency",
            "lambda:GetFunctionConcurrency",
            "lambda:PutFunctionConcurrency",
        ],
        "Resource": [
            {"Fn::Sub": CORE_FUNCTION_ARN_PREFIX + name} for name in CAPPED_CORE_FUNCTION_NAMES
        ],
    }
    artifact = statements["ReadOnlyExactLambdaDeploymentArchiveVersion"]
    assert artifact["Action"] == "s3:GetObjectVersion"
    assert artifact["Condition"] == {"StringEquals": {"s3:VersionId": {"Ref": "LambdaVersionId"}}}
    assert artifact["Resource"]["Fn::Sub"].endswith(
        "/private/deployments/lambda/releases/${ReleaseFingerprint}/"
        "phase6-lambda-${LambdaArchiveSha256}.zip"
    )
    assert all(
        "cloudfront" not in json.dumps(statement).lower() for statement in statements.values()
    )
    assert all("cognito" not in json.dumps(statement).lower() for statement in statements.values())


def test_core_execution_role_covers_exact_sam_generated_trigger_closure() -> None:
    role = _bootstrap()["Resources"]["CoreRuntimeExecutionRole"]
    statements = _statements(role["Properties"]["Policies"][0])

    create_mapping = statements["CreateOnlyDispatcherEventSourceMapping"]
    assert create_mapping == {
        "Sid": "CreateOnlyDispatcherEventSourceMapping",
        "Effect": "Allow",
        "Action": "lambda:CreateEventSourceMapping",
        "Resource": "*",
        "Condition": {
            "ArnEquals": {
                "lambda:FunctionArn": {
                    "Fn::Sub": (
                        "arn:${AWS::Partition}:lambda:us-west-2:${AWS::AccountId}:"
                        "function:mr-lister-phase6-dev-dispatcher"
                    )
                }
            },
            "StringEquals": {"aws:RequestedRegion": "us-west-2"},
        },
    }
    manage_mapping = statements["ManageOnlyDispatcherEventSourceMapping"]
    assert manage_mapping["Action"] == [
        "lambda:DeleteEventSourceMapping",
        "lambda:GetEventSourceMapping",
        "lambda:UpdateEventSourceMapping",
    ]
    assert manage_mapping["Resource"]["Fn::Sub"] == (
        "arn:${AWS::Partition}:lambda:us-west-2:${AWS::AccountId}:event-source-mapping:*"
    )
    assert manage_mapping["Condition"] == create_mapping["Condition"]
    mapping_tags = statements["TagOnlyCoreEventSourceMappings"]
    assert mapping_tags["Action"] == [
        "lambda:ListTags",
        "lambda:TagResource",
        "lambda:UntagResource",
    ]
    assert mapping_tags["Resource"] == manage_mapping["Resource"]
    assert mapping_tags["Condition"] == {"StringEquals": {"aws:RequestedRegion": "us-west-2"}}

    rules = statements["ManageOnlyCoreEventRules"]
    assert rules["Resource"]["Fn::Sub"] == (
        "arn:${AWS::Partition}:events:us-west-2:${AWS::AccountId}:rule/mr-lister-phase6-dev-*"
    )
    assert rules["Action"] == [
        "events:DeleteRule",
        "events:DescribeRule",
        "events:ListTagsForResource",
        "events:ListTargetsByRule",
        "events:PutRule",
        "events:PutTargets",
        "events:RemoveTargets",
        "events:TagResource",
        "events:UntagResource",
    ]


def test_core_execution_role_uses_valid_bucket_policy_actions() -> None:
    role = _bootstrap()["Resources"]["CoreRuntimeExecutionRole"]
    statements = _statements(role["Properties"]["Policies"][0])
    bucket_actions = statements["UpdateOnlyFoundationArtifactBucketConfiguration"]["Action"]

    assert "s3:GetBucketPublicAccessBlock" in bucket_actions
    assert "s3:PutBucketPublicAccessBlock" in bucket_actions
    assert "s3:GetPublicAccessBlock" not in bucket_actions
    assert "s3:PutPublicAccessBlock" not in bucket_actions
    assert "s3:DeleteBucketTagging" not in bucket_actions


def test_every_unavoidable_wildcard_is_region_scoped() -> None:
    resources = _bootstrap()["Resources"]
    documents = [
        resources["CoreRuntimeExecutionRole"]["Properties"]["Policies"][0]["PolicyDocument"],
        resources["RuntimeUpdateDeployerRole"]["Properties"]["Policies"][0]["PolicyDocument"],
    ]

    wildcard_statements = [
        statement
        for document in documents
        for statement in document["Statement"]
        if statement["Resource"] == "*"
    ]
    assert {statement["Sid"] for statement in wildcard_statements} == {
        "ConfigureOnlyRegionalStepFunctionsLogDelivery",
        "CreateOnlyDispatcherEventSourceMapping",
        "ReadOnlyRegionalCreateEventHistory",
    }
    for statement in wildcard_statements:
        assert statement["Condition"]["StringEquals"]["aws:RequestedRegion"] == ("us-west-2")


def test_stage_b_deployer_is_exact_expiring_readback_only_and_cannot_execute() -> None:
    resources = _bootstrap()["Resources"]
    role = resources["RuntimeUpdateDeployerRole"]
    assume = resources["DeveloperAssumeDeployerPolicy"]

    assert role["Condition"] == "StageBExact"
    assert assume["Condition"] == "StageBExact"
    assert role["Properties"]["RoleName"] == "mr-lister-phase6-runtime-update-deployer-dev"
    assert role["Properties"]["AssumeRolePolicyDocument"] == {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Principal": {
                    "AWS": {
                        "Fn::Sub": "arn:${AWS::Partition}:iam::${AWS::AccountId}:user/mr-lister-dev"
                    }
                },
                "Action": "sts:AssumeRole",
            }
        ],
    }
    policy = role["Properties"]["Policies"][0]
    assert policy["PolicyName"] == "mr-lister-phase6-runtime-update-deployer-dev"
    statements = _statements(policy)
    create = statements["CreateExactReviewedRuntimeUpdate"]
    exact = create["Condition"]["StringEquals"]
    assert exact["cloudformation:ChangeSetName"] == {"Ref": "ExactChangeSetName"}
    assert exact["cloudformation:RoleArn"] == {"Fn::GetAtt": ["CoreRuntimeExecutionRole", "Arn"]}
    assert exact["cloudformation:TemplateUrl"]["Fn::Sub"].endswith(
        "/private/deployments/cloudformation/core/releases/${ReleaseFingerprint}/"
        "core-template.json?versionId=${CoreTemplateVersionIdUrlEncoded}"
    )
    assert statements["PassExactRuntimeExecutionRole"]["Condition"]["StringEquals"] == {
        "iam:PassedToService": "cloudformation.amazonaws.com"
    }
    assert statements["ReadOnlyExactReviewedTemplateVersion"]["Condition"]["StringEquals"] == {
        "s3:VersionId": {"Ref": "CoreTemplateVersionId"}
    }
    assert statements["ReadBackOnlyExactDeploymentRoles"]["Resource"] == [
        {"Fn::GetAtt": ["CoreRuntimeExecutionRole", "Arn"]},
        {
            "Fn::Sub": (
                "arn:${AWS::Partition}:iam::${AWS::AccountId}:role/"
                "mr-lister-phase6-runtime-update-deployer-dev"
            )
        },
    ]
    actions = json.dumps(policy["PolicyDocument"])
    assert "cloudformation:ExecuteChangeSet" not in actions
    assert "cloudformation:DeleteStack" not in actions
    for statement in statements.values():
        assert statement["Condition"]["DateLessThan"] == {"aws:CurrentTime": {"Ref": "NotAfter"}}

    assume_statement = assume["Properties"]["PolicyDocument"]["Statement"][0]
    assert assume_statement["Action"] == "sts:AssumeRole"
    assert assume_statement["Resource"] == {"Fn::GetAtt": ["RuntimeUpdateDeployerRole", "Arn"]}
    assert assume_statement["Condition"]["DateLessThan"] == {"aws:CurrentTime": {"Ref": "NotAfter"}}
