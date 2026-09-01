from __future__ import annotations

import base64
import json
from hashlib import sha256
from pathlib import Path

import pytest

import tools.render_phase6_agentcore_direct_codezip as direct
from tools.render_phase6_agentcore_deployment import (
    AGENTCORE_OUTPUT,
    Phase6AgentCoreDeploymentBinding,
    render_phase6_agentcore_deployment,
)
from tools.verify_phase6_s3_release_object import VerifiedPhase6S3ReleaseObject

ROOT = Path(__file__).parents[1]
BOOTSTRAP = Path("infra/agentcore/mrlisterphase6/direct-codezip-bootstrap.json")
ACCOUNT = "123456789012"
RELEASE = "a" * 64
ARCHIVE_SHA = "b" * 64
VERSION_ID = "3HL4kqtJlcpXroDTDmJ+rmSpXd3dIbrHY+MTRCxf3vj="
RUNTIME_ID = "mr_lister_phase6-Ab12Cd34Ef"
RUNTIME_ARN = f"arn:aws:bedrock-agentcore:us-west-2:{ACCOUNT}:runtime/{RUNTIME_ID}"
WORKLOAD_IDENTITY_ARN = (
    f"arn:aws:bedrock-agentcore:us-west-2:{ACCOUNT}:"
    "workload-identity-directory/default/workload-identity/mr_lister_phase6"
)
EXPIRY = {"DateLessThan": {"aws:CurrentTime": {"Ref": "NotAfter"}}}


def _bootstrap() -> dict:
    return json.loads(BOOTSTRAP.read_text(encoding="utf-8"))


def _statements(policy: dict) -> dict[str, dict]:
    statements = policy["Statement"]
    assert len({statement["Sid"] for statement in statements}) == len(statements)
    return {statement["Sid"]: statement for statement in statements}


def _binding(**overrides: object) -> direct.Phase6AgentCoreDirectCodeZipBinding:
    values: dict[str, object] = {
        "account_id": ACCOUNT,
        "release_fingerprint": RELEASE,
        "agentcore_archive_sha256": ARCHIVE_SHA,
    }
    values.update(overrides)
    return direct.Phase6AgentCoreDirectCodeZipBinding(**values)  # type: ignore[arg-type]


def _archive(**overrides: object) -> direct.VerifiedAgentCoreArchive:
    values: dict[str, object] = {
        "sha256": ARCHIVE_SHA,
        "size_bytes": 96_306_014,
        "checksum_sha256_base64": base64.b64encode(bytes.fromhex(ARCHIVE_SHA)).decode("ascii"),
        "descriptor_sha256": "c" * 64,
    }
    values.update(overrides)
    return direct.VerifiedAgentCoreArchive(**values)  # type: ignore[arg-type]


def _remote(**overrides: object) -> VerifiedPhase6S3ReleaseObject:
    archive = _archive()
    binding = _binding()
    values: dict[str, object] = {
        "account_id": ACCOUNT,
        "region": "us-west-2",
        "environment": "dev",
        "component": "agentcore",
        "release_fingerprint": RELEASE,
        "archive_sha256": ARCHIVE_SHA,
        "size_bytes": archive.size_bytes,
        "checksum_sha256_base64": archive.checksum_sha256_base64,
        "bucket": binding.bucket,
        "key": binding.key,
        "version_id": VERSION_ID,
        "evidence_sha256": "d" * 64,
    }
    values.update(overrides)
    return VerifiedPhase6S3ReleaseObject(**values)  # type: ignore[arg-type]


def _runtime_documents() -> dict[Path, bytes]:
    return direct.render_phase6_agentcore_runtime_documents(
        _binding(),
        _archive(),
        _remote(),
    )


def _verified_runtime(**overrides: object) -> direct.VerifiedAgentCoreRuntimeV1:
    documents = _runtime_documents()
    values: dict[str, object] = {
        "runtime_id": RUNTIME_ID,
        "runtime_arn": RUNTIME_ARN,
        "evidence_sha256": "e" * 64,
        "runtime_create_input_sha256": sha256(documents[direct.RUNTIME_CREATE_OUTPUT]).hexdigest(),
        "runtime_render_manifest_sha256": sha256(
            documents[direct.RUNTIME_MANIFEST_OUTPUT]
        ).hexdigest(),
        "remote_object_evidence_sha256": _remote().evidence_sha256,
    }
    values.update(overrides)
    return direct.VerifiedAgentCoreRuntimeV1(**values)  # type: ignore[arg-type]


def _runtime_evidence_document(runtime_documents: dict[Path, bytes]) -> dict[str, object]:
    runtime_input = json.loads(runtime_documents[direct.RUNTIME_CREATE_OUTPUT])
    create_response = {
        "agentRuntimeArn": RUNTIME_ARN,
        "agentRuntimeId": RUNTIME_ID,
        "agentRuntimeVersion": "1",
        "createdAt": "2026-08-24T18:00:00+00:00",
        "status": "CREATING",
        "workloadIdentityDetails": {"workloadIdentityArn": WORKLOAD_IDENTITY_ARN},
    }
    get_response = {
        "agentRuntimeArn": RUNTIME_ARN,
        "agentRuntimeArtifact": runtime_input["agentRuntimeArtifact"],
        "agentRuntimeId": RUNTIME_ID,
        "agentRuntimeName": runtime_input["agentRuntimeName"],
        "agentRuntimeVersion": "1",
        "createdAt": "2026-08-24T18:00:12+00:00",
        "description": runtime_input["description"],
        "environmentVariables": runtime_input["environmentVariables"],
        "lastUpdatedAt": "2026-08-24T18:03:00+00:00",
        "lifecycleConfiguration": runtime_input["lifecycleConfiguration"],
        "metadataConfiguration": {"requireMMDSV2": True},
        "networkConfiguration": runtime_input["networkConfiguration"],
        "protocolConfiguration": runtime_input["protocolConfiguration"],
        "roleArn": runtime_input["roleArn"],
        "status": "READY",
        "workloadIdentityDetails": create_response["workloadIdentityDetails"],
    }
    return {
        "accountId": ACCOUNT,
        "createAgentRuntime": {
            "inputSHA256": sha256(runtime_documents[direct.RUNTIME_CREATE_OUTPUT]).hexdigest(),
            "response": create_response,
        },
        "format": direct.RUNTIME_V1_EVIDENCE_FORMAT,
        "getAgentRuntime": {
            "request": {"agentRuntimeId": RUNTIME_ID, "agentRuntimeVersion": "1"},
            "response": get_response,
        },
        "listTagsForResource": {
            "request": {"resourceArn": RUNTIME_ARN},
            "response": {"tags": _binding().tags},
        },
        "region": "us-west-2",
        "remoteObjectEvidenceSHA256": _remote().evidence_sha256,
        "runtimeRenderManifestSHA256": sha256(
            runtime_documents[direct.RUNTIME_MANIFEST_OUTPUT]
        ).hexdigest(),
    }


def _write_canonical(path: Path, document: object) -> None:
    path.write_text(
        json.dumps(document, allow_nan=False, indent=2, separators=(",", ": "), sort_keys=True)
        + "\n",
        encoding="utf-8",
    )


def _write_runtime_authority(repository: Path, evidence_path: Path) -> dict[Path, bytes]:
    documents = direct.render_phase6_agentcore_runtime_documents(
        _binding(),
        _archive(),
        _remote(),
        repository_root=repository,
    )
    for relative, raw in documents.items():
        destination = repository / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(raw)
    _write_canonical(evidence_path, _runtime_evidence_document(documents))
    return documents


def _temporary_policy_resources() -> tuple[dict, dict]:
    resources = _bootstrap()["Resources"]
    return (
        resources["DeveloperUploadPolicy"],
        resources["DeveloperEvidenceReadbackPolicy"],
    )


def test_bootstrap_is_root_applied_dev_us_west_2_and_fingerprint_bound() -> None:
    template = _bootstrap()
    assert template["Metadata"]["MrListerDeployment"] == {
        "CreateAuthorization": "BLOCKED_UNTIL_SEPARATELY_REVIEWED",
        "DeploymentClass": "AGENTCORE_DIRECT_CODEZIP_BOOTSTRAP_ONLY",
        "Environment": "dev",
        "Region": "us-west-2",
        "RootApplied": True,
        "RuntimeName": "mr_lister_phase6",
    }
    assert set(template["Parameters"]) == {
        "AgentCoreArchiveSha256",
        "NotAfter",
        "ReleaseFingerprint",
    }
    for name in template["Parameters"]:
        assert "Default" not in template["Parameters"][name]
    assert template["Parameters"]["ReleaseFingerprint"]["AllowedPattern"] == ("^[a-f0-9]{64}$")
    assert template["Parameters"]["AgentCoreArchiveSha256"]["AllowedPattern"] == ("^[a-f0-9]{64}$")
    assert template["Rules"] == {
        "OnlyUsWest2": {
            "Assertions": [
                {
                    "Assert": {"Fn::Equals": [{"Ref": "AWS::Region"}, "us-west-2"]},
                    "AssertDescription": "This dev-only bootstrap must be created in us-west-2",
                }
            ]
        }
    }


def test_retained_execution_role_trust_is_exact() -> None:
    role = _bootstrap()["Resources"]["AgentCoreRuntimeExecutionRole"]
    assert role["Type"] == "AWS::IAM::Role"
    assert role["DeletionPolicy"] == role["UpdateReplacePolicy"] == "Retain"
    properties = role["Properties"]
    assert properties["RoleName"] == "mr-lister-phase6-agentcore-runtime-dev"
    assert properties["AssumeRolePolicyDocument"]["Statement"] == [
        {
            "Sid": "TrustOnlyPhase6AgentCoreRuntime",
            "Effect": "Allow",
            "Principal": {"Service": "bedrock-agentcore.amazonaws.com"},
            "Action": "sts:AssumeRole",
            "Condition": {
                "StringEquals": {"aws:SourceAccount": {"Ref": "AWS::AccountId"}},
                "ArnLike": {
                    "aws:SourceArn": {
                        "Fn::Sub": (
                            "arn:${AWS::Partition}:bedrock-agentcore:us-west-2:"
                            "${AWS::AccountId}:runtime/mr_lister_phase6-*"
                        )
                    }
                },
            },
        }
    ]
    assert properties["Tags"] == [
        {"Key": "DeploymentClass", "Value": "AGENTCORE_RUNTIME_EXECUTION"},
        {"Key": "Environment", "Value": "dev"},
        {"Key": "Project", "Value": "MrLister"},
        {"Key": "ReleaseFingerprint", "Value": {"Ref": "ReleaseFingerprint"}},
    ]


def test_execution_role_preserves_reviewed_worker_policy_and_exact_archive_read() -> None:
    role = _bootstrap()["Resources"]["AgentCoreRuntimeExecutionRole"]["Properties"]
    assert len(role["Policies"]) == 1
    policy = role["Policies"][0]
    assert policy["PolicyName"] == "RunOnlyMrListerPhase6Preparation"
    statements = _statements(policy["PolicyDocument"])
    assert set(statements) == {
        "ConfigureOnlyPhase6AgentCoreRuntimeLogs",
        "CreateAndInspectAgentCoreRuntimeLogs",
        "DiscoverOnlyAccountLogGroups",
        "InvokeNova2LiteOnlyThroughUSProfile",
        "InvokeNova2LiteUSProfile",
        "InvokeOnlyGemma327BIntelligence",
        "ReadAndCommitOnlyPhase6PreparationState",
        "ReadOnlyExactAgentCoreDeploymentArchive",
        "ReadOnlyPinnedPhase6SourceVersions",
        "WriteOnlyPhase6AgentCoreRuntimeLogs",
    }
    assert statements["ReadOnlyExactAgentCoreDeploymentArchive"] == {
        "Sid": "ReadOnlyExactAgentCoreDeploymentArchive",
        "Effect": "Allow",
        "Action": ["s3:GetObject", "s3:GetObjectVersion"],
        "Resource": {
            "Fn::Sub": (
                "arn:${AWS::Partition}:s3:::mr-lister-phase6-artifacts-dev-"
                "${AWS::AccountId}-us-west-2/private/deployments/agentcore/releases/"
                "${ReleaseFingerprint}/phase6-agentcore-${AgentCoreArchiveSha256}.zip"
            )
        },
    }
    log_prefix = (
        "arn:${AWS::Partition}:logs:us-west-2:${AWS::AccountId}:log-group:"
        "/aws/bedrock-agentcore/runtimes/mr_lister_phase6-*"
    )
    assert statements["CreateAndInspectAgentCoreRuntimeLogs"]["Resource"] == {"Fn::Sub": log_prefix}
    assert statements["ConfigureOnlyPhase6AgentCoreRuntimeLogs"]["Resource"] == {
        "Fn::Sub": log_prefix
    }
    assert statements["WriteOnlyPhase6AgentCoreRuntimeLogs"]["Resource"] == {
        "Fn::Sub": f"{log_prefix}:log-stream:*"
    }
    actions = {
        action
        for statement in statements.values()
        for action in (
            statement["Action"] if isinstance(statement["Action"], list) else [statement["Action"]]
        )
    }
    assert actions == {
        "bedrock:InvokeModel",
        "dynamodb:ConditionCheckItem",
        "dynamodb:GetItem",
        "dynamodb:PutItem",
        "logs:CreateLogGroup",
        "logs:CreateLogStream",
        "logs:DescribeLogGroups",
        "logs:DescribeLogStreams",
        "logs:PutLogEvents",
        "logs:PutResourcePolicy",
        "s3:GetObject",
        "s3:GetObjectVersion",
    }
    serialized = json.dumps(policy, sort_keys=True).casefold()
    assert "/runtimes/*mr_lister_phase6" not in serialized
    for forbidden in (
        "secretsmanager:",
        "states:",
        "execute-api:",
        "printify",
        "publication",
        "s3:putobject",
        "bedrock-agentcore:",
    ):
        assert forbidden not in serialized


def test_temporary_policy_expires_every_statement_and_has_no_create_or_update_authority() -> None:
    upload_resource, readback_resource = _temporary_policy_resources()
    assert upload_resource["Properties"]["ManagedPolicyName"] == (
        "mr-lister-phase6-agentcore-direct-uploader-dev"
    )
    assert readback_resource["Properties"]["ManagedPolicyName"] == (
        "mr-lister-phase6-agentcore-direct-evidence-reader-dev"
    )
    for resource in (upload_resource, readback_resource):
        assert resource["Properties"]["Groups"] == ["mr-lister-developers"]
        assert (
            len(
                json.dumps(
                    resource["Properties"]["PolicyDocument"],
                    separators=(",", ":"),
                )
            )
            <= 6144
        )
    statements = {
        **_statements(upload_resource["Properties"]["PolicyDocument"]),
        **_statements(readback_resource["Properties"]["PolicyDocument"]),
    }
    assert statements
    for statement in statements.values():
        assert statement["Effect"] == "Allow"
        assert statement["Condition"]["DateLessThan"] == EXPIRY["DateLessThan"]

    actions = {
        action
        for statement in statements.values()
        for action in (
            statement["Action"] if isinstance(statement["Action"], list) else [statement["Action"]]
        )
    }
    assert "bedrock-agentcore:CreateAgentRuntime" not in actions
    assert "bedrock-agentcore:CreateAgentRuntimeEndpoint" not in actions
    assert "bedrock-agentcore:UpdateAgentRuntime" not in actions
    assert "bedrock-agentcore:UpdateAgentRuntimeEndpoint" not in actions
    assert "logs:PutRetentionPolicy" not in actions
    assert "logs:DescribeLogGroups" not in actions
    assert "bedrock:GetFoundationModelAvailability" not in actions
    assert (
        not {
            "bedrock-agentcore:ListAgentRuntimes",
            "bedrock-agentcore:ListAgentRuntimeVersions",
            "bedrock-agentcore:ListAgentRuntimeEndpoints",
        }
        & actions
    )
    serialized = json.dumps(
        [resource["Properties"]["PolicyDocument"] for resource in _temporary_policy_resources()],
        sort_keys=True,
    ).casefold()
    for forbidden in (
        "cloudformation:",
        "secretsmanager:",
        "s3:delete",
        "s3:*",
        "iam:create",
        "iam:put",
        "iam:attachrole",
        "iam:attachuser",
        "default endpoint",
        '"resource": "*"',
    ):
        assert forbidden not in serialized

    freeze = _bootstrap()["Resources"]["DeveloperReleaseFreezePolicy"]
    assert freeze["DeletionPolicy"] == freeze["UpdateReplacePolicy"] == "Retain"
    assert "Groups" not in freeze["Properties"]
    assert freeze["Properties"]["ManagedPolicyName"] == (
        "mr-lister-phase6-agentcore-release-freeze-dev"
    )
    assert freeze["Properties"]["PolicyDocument"]["Statement"] == [
        {
            "Sid": "FreezeExactPhase6ReleaseObject",
            "Effect": "Deny",
            "Action": ["s3:DeleteObject", "s3:DeleteObjectVersion", "s3:PutObject"],
            "Resource": {
                "Fn::Sub": (
                    "arn:${AWS::Partition}:s3:::mr-lister-phase6-artifacts-dev-"
                    "${AWS::AccountId}-us-west-2/private/deployments/agentcore/releases/"
                    "${ReleaseFingerprint}/phase6-agentcore-${AgentCoreArchiveSha256}.zip"
                )
            },
        }
    ]


def test_conditional_upload_and_exact_version_readback_are_content_addressed() -> None:
    upload_resource, readback_resource = _temporary_policy_resources()
    upload = _statements(upload_resource["Properties"]["PolicyDocument"])[
        "ConditionallyUploadOnlyExactAgentCoreArchive"
    ]
    statements = _statements(readback_resource["Properties"]["PolicyDocument"])
    exact_object = {
        "Fn::Sub": (
            "arn:${AWS::Partition}:s3:::mr-lister-phase6-artifacts-dev-"
            "${AWS::AccountId}-us-west-2/private/deployments/agentcore/releases/"
            "${ReleaseFingerprint}/phase6-agentcore-${AgentCoreArchiveSha256}.zip"
        )
    }
    assert upload["Action"] == "s3:PutObject"
    assert upload["Resource"] == exact_object
    assert upload["Condition"] == {
        **EXPIRY,
        "StringEquals": {
            "s3:if-none-match": "*",
            "s3:x-amz-server-side-encryption": "AES256",
        },
    }
    readback = statements["ReadBackOnlyExactAgentCoreArchiveVersion"]
    assert set(readback["Action"]) == {
        "s3:GetObjectVersion",
        "s3:GetObjectVersionAttributes",
    }
    assert readback["Resource"] == exact_object
    versions = statements["ListOnlyExactAgentCoreArchiveVersions"]
    assert versions["Action"] == "s3:ListBucketVersions"
    assert versions["Condition"]["StringEquals"] == {
        "s3:prefix": {
            "Fn::Sub": (
                "private/deployments/agentcore/releases/${ReleaseFingerprint}/"
                "phase6-agentcore-${AgentCoreArchiveSha256}.zip"
            )
        }
    }
    bucket_actions = set(statements["InspectOnlyFoundationArtifactBucket"]["Action"])
    assert bucket_actions == {
        "s3:GetBucketLocation",
        "s3:GetBucketOwnershipControls",
        "s3:GetBucketVersioning",
        "s3:GetEncryptionConfiguration",
    }


def test_temporary_runtime_role_model_log_canary_and_rollback_scopes_are_exact() -> None:
    policy = _bootstrap()["Resources"]["DeveloperEvidenceReadbackPolicy"]["Properties"]
    statements = _statements(policy["PolicyDocument"])
    pass_role = statements["PassOnlyTheRetainedAgentCoreRuntimeRole"]
    assert pass_role["Action"] == "iam:PassRole"
    assert pass_role["Resource"] == {"Fn::GetAtt": ["AgentCoreRuntimeExecutionRole", "Arn"]}
    assert pass_role["Condition"]["StringEquals"] == {
        "iam:PassedToService": "bedrock-agentcore.amazonaws.com"
    }
    assert set(statements["InspectOnlyTheRetainedAgentCoreRuntimeRole"]["Action"]) == {
        "iam:GetRole",
        "iam:GetRolePolicy",
        "iam:ListAttachedRolePolicies",
        "iam:ListRolePolicies",
    }
    models = statements["InspectOnlyCheckedBedrockModels"]
    assert set(models["Action"]) == {
        "bedrock:GetFoundationModel",
        "bedrock:GetInferenceProfile",
    }
    assert len(models["Resource"]) == 5
    assert {
        "Fn::Sub": (
            "arn:${AWS::Partition}:bedrock:us-west-2:${AWS::AccountId}:"
            "inference-profile/us.amazon.nova-2-lite-v1:0"
        )
    } in models["Resource"]

    tagged_sid = "ReadCanaryStopAndRollbackOnlyTaggedPhase6V1"
    expected_tags = {
        "aws:ResourceTag/DeploymentClass": "AGENTCORE_DIRECT_CODEZIP",
        "aws:ResourceTag/Environment": "dev",
        "aws:ResourceTag/Project": "MrLister",
        "aws:ResourceTag/ReleaseFingerprint": {"Ref": "ReleaseFingerprint"},
    }
    assert statements[tagged_sid]["Condition"]["StringEquals"] == expected_tags
    assert "mr_lister_phase6-" in json.dumps(statements[tagged_sid]["Resource"])
    assert set(statements[tagged_sid]["Action"]) == {
        "bedrock-agentcore:DeleteAgentRuntime",
        "bedrock-agentcore:DeleteAgentRuntimeEndpoint",
        "bedrock-agentcore:GetAgentRuntime",
        "bedrock-agentcore:GetAgentRuntimeEndpoint",
        "bedrock-agentcore:InvokeAgentRuntime",
        "bedrock-agentcore:ListTagsForResource",
        "bedrock-agentcore:StopRuntimeSession",
    }
    endpoint_resources = json.dumps(statements[tagged_sid]["Resource"])
    assert "runtime-endpoint/phase6_v1_dev" in endpoint_resources
    log_policy = json.dumps(statements["ReadOnlyExactPhase6V1RuntimeLogs"])
    assert "/aws/bedrock-agentcore/runtimes/mr_lister_phase6-*-phase6_v1_dev" in log_policy
    assert "/runtimes/*mr_lister_phase6" not in log_policy


def test_upload_freeze_attachment_lifecycle_iam_authority_is_exact() -> None:
    policy = _bootstrap()["Resources"]["DeveloperEvidenceReadbackPolicy"]["Properties"]
    statements = _statements(policy["PolicyDocument"])
    group_arn = {
        "Fn::Sub": "arn:${AWS::Partition}:iam::${AWS::AccountId}:group/mr-lister-developers"
    }
    attach = statements["AttachOnlyExactReleaseFreezeToDevelopers"]
    assert attach["Action"] == "iam:AttachGroupPolicy"
    assert attach["Resource"] == group_arn
    assert attach["Condition"]["ArnEquals"] == {
        "iam:PolicyARN": {"Ref": "DeveloperReleaseFreezePolicy"}
    }
    detach = statements["DetachOnlyExactUploadAuthorityFromDevelopers"]
    assert detach["Action"] == "iam:DetachGroupPolicy"
    assert detach["Resource"] == group_arn
    assert detach["Condition"]["ArnEquals"] == {"iam:PolicyARN": {"Ref": "DeveloperUploadPolicy"}}
    listing = statements["ReadBackOnlyDeveloperPolicyAttachments"]
    assert set(listing["Action"]) == {"iam:GetGroup", "iam:ListAttachedGroupPolicies"}
    assert listing["Resource"] == group_arn
    freeze_read = statements["ReadBackOnlyExactReleaseFreezePolicy"]
    assert set(freeze_read["Action"]) == {"iam:GetPolicy", "iam:GetPolicyVersion"}
    assert freeze_read["Resource"] == {"Ref": "DeveloperReleaseFreezePolicy"}


def test_bootstrap_outputs_bind_role_policy_expiry_bucket_key_and_blocked_create() -> None:
    assert _bootstrap()["Outputs"] == {
        "AgentCoreRuntimeExecutionRoleArn": {
            "Value": {"Fn::GetAtt": ["AgentCoreRuntimeExecutionRole", "Arn"]}
        },
        "AgentCoreDeploymentBucket": {
            "Value": {"Fn::Sub": "mr-lister-phase6-artifacts-dev-${AWS::AccountId}-us-west-2"}
        },
        "AgentCoreDeploymentKey": {
            "Value": {
                "Fn::Sub": (
                    "private/deployments/agentcore/releases/${ReleaseFingerprint}/"
                    "phase6-agentcore-${AgentCoreArchiveSha256}.zip"
                )
            }
        },
        "DefaultAgentCoreCreateAuthorization": {"Value": "BLOCKED"},
        "DeveloperEvidenceReadbackPolicyArn": {"Value": {"Ref": "DeveloperEvidenceReadbackPolicy"}},
        "DeveloperReleaseFreezePolicyArn": {"Value": {"Ref": "DeveloperReleaseFreezePolicy"}},
        "DeveloperUploadPolicyArn": {"Value": {"Ref": "DeveloperUploadPolicy"}},
        "DeveloperDeploymentPolicyNotAfter": {"Value": {"Ref": "NotAfter"}},
    }


def test_runtime_renderer_is_deterministic_exact_version_bound_and_uses_existing_env() -> None:
    first = _runtime_documents()
    second = _runtime_documents()
    assert first == second
    assert set(first) == {
        direct.AUTHORIZATION_RESIDUAL_OUTPUT,
        direct.RUNTIME_CREATE_OUTPUT,
        direct.RUNTIME_MANIFEST_OUTPUT,
        direct.UPLOAD_PLAN_OUTPUT,
    }
    runtime = json.loads(first[direct.RUNTIME_CREATE_OUTPUT])
    assert runtime["agentRuntimeName"] == "mr_lister_phase6"
    assert runtime["agentRuntimeArtifact"] == {
        "codeConfiguration": {
            "code": {
                "s3": {
                    "bucket": f"mr-lister-phase6-artifacts-dev-{ACCOUNT}-us-west-2",
                    "prefix": (
                        f"private/deployments/agentcore/releases/{RELEASE}/"
                        f"phase6-agentcore-{ARCHIVE_SHA}.zip"
                    ),
                    "versionId": VERSION_ID,
                }
            },
            "entryPoint": ["main.py"],
            "runtime": "PYTHON_3_12",
        }
    }
    assert runtime["networkConfiguration"] == {"networkMode": "PUBLIC"}
    assert runtime["protocolConfiguration"] == {"serverProtocol": "HTTP"}
    assert runtime["lifecycleConfiguration"] == {
        "idleRuntimeSessionTimeout": 900,
        "maxLifetime": 3600,
    }
    assert runtime["roleArn"] == (
        f"arn:aws:iam::{ACCOUNT}:role/mr-lister-phase6-agentcore-runtime-dev"
    )
    assert runtime["tags"] == {
        "DeploymentClass": "AGENTCORE_DIRECT_CODEZIP",
        "Environment": "dev",
        "Project": "MrLister",
        "ReleaseFingerprint": RELEASE,
    }
    assert "authorizerConfiguration" not in runtime
    reviewed = render_phase6_agentcore_deployment(
        Phase6AgentCoreDeploymentBinding(
            account_id=ACCOUNT,
            region="us-west-2",
            environment="dev",
            release_fingerprint=RELEASE,
            runtime_version="1",
        )
    )
    reviewed_config = json.loads(reviewed[AGENTCORE_OUTPUT])
    [reviewed_runtime] = reviewed_config["runtimes"]
    assert runtime["environmentVariables"] == {
        item["name"]: item["value"] for item in reviewed_runtime["envVars"]
    }
    manifest = json.loads(first[direct.RUNTIME_MANIFEST_OUTPUT])
    assert manifest["proofClaims"] == {
        "byteIdentityBinding": "VERIFIED_AT_CAPTURE",
        "collisionHygiene": "VERIFIED_FOR_MR_LISTER_DEV_GROUP",
        "uploadAuthorityRevoked": "VERIFIED_FOR_EXACT_MR_LISTER_DEV_USER",
    }
    assert manifest["remoteObjectEvidenceSHA256"] == "d" * 64


def test_upload_plan_binds_local_hash_size_base64_checksum_and_conditional_readback() -> None:
    plan = json.loads(_runtime_documents()[direct.UPLOAD_PLAN_OUTPUT])
    assert plan["artifact"] == {
        "checksumSHA256Base64": _archive().checksum_sha256_base64,
        "descriptorSHA256": "c" * 64,
        "localPath": ".mr_lister_private/phase6-artifacts/phase6-agentcore.zip",
        "sha256": ARCHIVE_SHA,
        "sizeBytes": 96_306_014,
    }
    assert plan["conditionalWrite"] == {
        "checksumAlgorithm": "SHA256",
        "expectedBucketOwner": ACCOUNT,
        "ifNoneMatch": "*",
        "metadata": {
            "mr-lister-archive-sha256": ARCHIVE_SHA,
            "mr-lister-component": "agentcore",
            "mr-lister-release-fingerprint": RELEASE,
            "mr-lister-size-bytes": "96306014",
        },
        "required": True,
        "serverSideEncryption": "AES256",
    }
    assert plan["s3"] == {"bucket": _binding().bucket, "key": _binding().key}
    assert plan["postUploadRenderStatus"] == ("BLOCKED_UNTIL_CLOSED_OBJECT_EVIDENCE_VERIFIES")
    assert "--if-none-match" in plan["putObjectArguments"]
    assert plan["putObjectArguments"][plan["putObjectArguments"].index("--if-none-match") + 1] == (
        "*"
    )
    assert (
        plan["putObjectArguments"][plan["putObjectArguments"].index("--server-side-encryption") + 1]
        == "AES256"
    )
    evidence = plan["requiredClosedEvidence"]
    assert evidence["format"] == "mr-lister-phase6-s3-release-object-evidence-v2"
    assert evidence["putObjectResponseFields"] == [
        "ChecksumSHA256",
        "ChecksumType",
        "ETag",
        "ServerSideEncryption",
        "VersionId",
    ]
    assert "Size" not in evidence["putObjectResponseFields"]
    assert evidence["byteIdentityRequirements"] == {
        "exactVersionHeadRepeatedAfterUploadAuthorityRevocation": True,
        "fullObjectChecksumSHA256Base64": _archive().checksum_sha256_base64,
        "metadata": plan["conditionalWrite"]["metadata"],
        "serverSideEncryption": "AES256",
        "sizeBytes": 96_306_014,
    }
    assert evidence["collisionHygieneRequirements"] == {
        "completeExactPrefixVersionListing": True,
        "conditionalCreate": True,
        "exactKeyDeleteAndDeleteVersionExplicitlyDenied": True,
        "noDeleteMarkers": True,
        "singletonCurrentVersion": True,
        "uploadAuthorityDetachedAndAccessDenied": True,
    }
    lifecycle = evidence["uploadAuthorityRevocation"]
    assert evidence["requiredUploadCallerArn"] == (f"arn:aws:iam::{ACCOUNT}:user/mr-lister-dev")
    assert lifecycle["attachFreezePolicyArguments"][:2] == ["iam", "attach-group-policy"]
    assert lifecycle["detachUploadPolicyArguments"][:2] == ["iam", "detach-group-policy"]
    assert lifecycle["getGroupMembershipArguments"] == [
        "iam",
        "get-group",
        "--group-name",
        "mr-lister-developers",
    ]
    assert lifecycle["denyProbeExpected"] == {
        "ErrorCode": "AccessDenied",
        "HTTPStatusCode": 403,
    }
    assert VERSION_ID not in json.dumps(plan)


def test_upload_plan_is_unbound_and_runtime_render_requires_verified_remote_proof() -> None:
    binding = _binding()
    plan = json.loads(direct.render_phase6_agentcore_upload_plan(binding, _archive()))
    assert "versionId" not in json.dumps(plan)
    with pytest.raises(direct.Phase6AgentCoreDirectCodeZipError):
        direct.render_phase6_agentcore_runtime_documents(
            binding,
            _archive(),
            None,  # type: ignore[arg-type]
        )


def test_binding_and_cli_offer_no_bare_s3_version_id_render_path() -> None:
    with pytest.raises(TypeError):
        direct.Phase6AgentCoreDirectCodeZipBinding(
            account_id=ACCOUNT,
            release_fingerprint=RELEASE,
            agentcore_archive_sha256=ARCHIVE_SHA,
            s3_version_id=VERSION_ID,  # type: ignore[call-arg]
        )
    with pytest.raises(SystemExit):
        direct.main(
            [
                "--account-id",
                ACCOUNT,
                "--release-fingerprint",
                RELEASE,
                "--agentcore-archive-sha256",
                ARCHIVE_SHA,
                "--s3-version-id",
                VERSION_ID,
                "--write-runtime",
            ]
        )
    with pytest.raises(SystemExit):
        direct.main(
            [
                "--account-id",
                ACCOUNT,
                "--release-fingerprint",
                RELEASE,
                "--agentcore-archive-sha256",
                ARCHIVE_SHA,
                "--object-binding-evidence",
                "closed.json",
                "--agent-runtime-id",
                RUNTIME_ID,
                "--write-endpoint",
            ]
        )
    with pytest.raises(SystemExit):
        direct.main(
            [
                "--account-id",
                ACCOUNT,
                "--release-fingerprint",
                RELEASE,
                "--agentcore-archive-sha256",
                ARCHIVE_SHA,
                "--write-runtime",
            ]
        )


def test_runtime_render_rejects_valid_but_drifting_remote_binding() -> None:
    with pytest.raises(direct.Phase6AgentCoreDirectCodeZipError):
        direct.render_phase6_agentcore_runtime_documents(
            _binding(),
            _archive(),
            _remote(size_bytes=_archive().size_bytes - 1),
        )
    other_release = "e" * 64
    with pytest.raises(direct.Phase6AgentCoreDirectCodeZipError):
        direct.render_phase6_agentcore_runtime_documents(
            _binding(),
            _archive(),
            _remote(
                release_fingerprint=other_release,
                key=(
                    f"private/deployments/agentcore/releases/{other_release}/"
                    f"phase6-agentcore-{ARCHIVE_SHA}.zip"
                ),
            ),
        )


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("account_id", "000000000000"),
        ("account_id", "123"),
        ("release_fingerprint", "0" * 64),
        ("release_fingerprint", "A" * 64),
        ("agentcore_archive_sha256", "0" * 64),
        ("agentcore_archive_sha256", "b" * 63),
    ),
)
def test_zero_malformed_release_and_archive_bindings_are_rejected(
    field: str,
    value: str,
) -> None:
    with pytest.raises(direct.Phase6AgentCoreDirectCodeZipError):
        _binding(**{field: value})


def test_authorization_residual_is_machine_readable_and_explicitly_blocks_create() -> None:
    residual = json.loads(_runtime_documents()[direct.AUTHORIZATION_RESIDUAL_OUTPUT])
    assert residual["status"] == "BLOCKED_UNTIL_SEPARATELY_REVIEWED"
    assert residual["blockedCreateOperations"] == [
        {
            "action": "bedrock-agentcore:CreateAgentRuntime",
            "defaultDeveloperPolicyGrant": False,
            "requiredValue": "mr_lister_phase6",
            "unsupportedIamDimensions": ["agentRuntimeName"],
        },
        {
            "action": "bedrock-agentcore:CreateAgentRuntimeEndpoint",
            "defaultDeveloperPolicyGrant": False,
            "requiredValues": {
                "agentRuntimeVersion": "1",
                "endpointName": "phase6_v1_dev",
            },
            "unsupportedIamDimensions": ["endpointName", "agentRuntimeVersion"],
        },
    ]
    assert residual["crossingRequires"] == [
        "separately-reviewed-one-time-manual-root-execution",
        "explicit-user-approved-tag-and-time-scoped-exception",
    ]
    fail_closed = {
        entry.get("action", tuple(entry.get("actions", ())))
        for entry in residual["failClosedReadbackAndRetentionResiduals"]
    }
    assert "logs:PutRetentionPolicy" in fail_closed
    assert "logs:DescribeLogGroups" in fail_closed
    assert "bedrock:GetFoundationModelAvailability" in fail_closed
    assert (
        "bedrock-agentcore:ListAgentRuntimes",
        "bedrock-agentcore:ListAgentRuntimeVersions",
        "bedrock-agentcore:ListAgentRuntimeEndpoints",
    ) in fail_closed
    [protection] = residual["remoteObjectProtectionResiduals"]
    assert protection["accountWideObjectLock"] is False
    assert protection["retainedFreezePolicyActions"] == [
        "s3:DeleteObject",
        "s3:DeleteObjectVersion",
        "s3:PutObject",
    ]
    assert "outside mr-lister-developers" in protection["risk"]


def test_endpoint_renderer_requires_verified_runtime_evidence_and_pins_v1(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    monkeypatch.setattr(
        direct,
        "_existing_phase6_environment",
        lambda *_args, **_kwargs: {"MR_LISTER_RELEASE_FINGERPRINT": RELEASE},
    )
    evidence_path = tmp_path / "runtime-v1-evidence.json"
    _write_runtime_authority(repository, evidence_path)
    first = direct.render_phase6_agentcore_endpoint_documents(
        _binding(),
        _archive(),
        _remote(),
        runtime_v1_evidence_path=evidence_path,
        repository_root=repository,
    )
    second = direct.render_phase6_agentcore_endpoint_documents(
        _binding(),
        _archive(),
        _remote(),
        runtime_v1_evidence_path=evidence_path,
        repository_root=repository,
    )
    assert first == second
    endpoint = json.loads(first[direct.ENDPOINT_CREATE_OUTPUT])
    assert endpoint["agentRuntimeId"] == RUNTIME_ID
    assert endpoint["agentRuntimeVersion"] == "1"
    assert endpoint["name"] == "phase6_v1_dev"
    assert endpoint["tags"] == _binding().tags
    assert "DEFAULT" not in json.dumps(endpoint)


@pytest.mark.parametrize(
    "runtime_id",
    (
        "",
        "mr_lister_phase6",
        "mr_lister_phase6-short",
        "other_runtime-Ab12Cd34Ef",
        "mr_lister_phase6-Ab12Cd34E!",
        "<AGENT_RUNTIME_ID>",
    ),
)
def test_empty_malformed_moving_or_wrong_runtime_ids_are_rejected(runtime_id: str) -> None:
    with pytest.raises(direct.Phase6AgentCoreDirectCodeZipError):
        _verified_runtime(
            runtime_id=runtime_id,
            runtime_arn=(f"arn:aws:bedrock-agentcore:us-west-2:{ACCOUNT}:runtime/{runtime_id}"),
        )


def test_endpoint_rejects_runtime_manifest_for_a_different_version_binding(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    monkeypatch.setattr(
        direct,
        "_existing_phase6_environment",
        lambda *_args, **_kwargs: {"MR_LISTER_RELEASE_FINGERPRINT": RELEASE},
    )
    evidence_path = tmp_path / "runtime-v1-evidence.json"
    _write_runtime_authority(repository, evidence_path)
    wrong = direct.render_phase6_agentcore_runtime_documents(
        _binding(),
        _archive(),
        _remote(
            version_id="different-literal-version-id",
            evidence_sha256="e" * 64,
        ),
        repository_root=repository,
    )
    (repository / direct.RUNTIME_MANIFEST_OUTPUT).write_bytes(wrong[direct.RUNTIME_MANIFEST_OUTPUT])
    with pytest.raises(direct.Phase6AgentCoreDirectCodeZipError):
        direct.render_phase6_agentcore_endpoint_documents(
            _binding(),
            _archive(),
            _remote(),
            runtime_v1_evidence_path=evidence_path,
            repository_root=repository,
        )


def test_runtime_v1_evidence_joins_create_get_tags_and_rejects_authority_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    monkeypatch.setattr(
        direct,
        "_existing_phase6_environment",
        lambda *_args, **_kwargs: {"MR_LISTER_RELEASE_FINGERPRINT": RELEASE},
    )
    documents = direct.render_phase6_agentcore_runtime_documents(
        _binding(),
        _archive(),
        _remote(),
        repository_root=repository,
    )
    for relative, raw in documents.items():
        destination = repository / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(raw)
    evidence_path = tmp_path / "runtime-v1-evidence.json"
    evidence = _runtime_evidence_document(documents)
    _write_canonical(evidence_path, evidence)

    verified = direct.verify_phase6_agentcore_runtime_v1_evidence(
        _binding(),
        _archive(),
        _remote(),
        runtime_v1_evidence_path=evidence_path,
        repository_root=repository,
    )
    assert verified.runtime_id == RUNTIME_ID
    assert verified.runtime_arn == RUNTIME_ARN
    assert verified.evidence_sha256 == sha256(evidence_path.read_bytes()).hexdigest()

    def mutate(path: tuple[str, ...], value: object) -> dict[str, object]:
        altered = json.loads(json.dumps(evidence))
        target = altered
        for component in path[:-1]:
            target = target[component]
        target[path[-1]] = value
        return altered

    equal_timestamps = mutate(
        ("getAgentRuntime", "response", "createdAt"),
        evidence["createAgentRuntime"]["response"]["createdAt"],
    )
    _write_canonical(evidence_path, equal_timestamps)
    direct.verify_phase6_agentcore_runtime_v1_evidence(
        _binding(),
        _archive(),
        _remote(),
        runtime_v1_evidence_path=evidence_path,
        repository_root=repository,
    )

    adversarial = (
        mutate(("createAgentRuntime", "inputSHA256"), "0" * 64),
        mutate(("createAgentRuntime", "response", "agentRuntimeArn"), RUNTIME_ARN + "x"),
        mutate(("getAgentRuntime", "request", "agentRuntimeVersion"), "2"),
        mutate(("getAgentRuntime", "response", "agentRuntimeArn"), RUNTIME_ARN + "x"),
        mutate(
            ("getAgentRuntime", "response", "agentRuntimeId"),
            "mr_lister_phase6-substitut1",
        ),
        mutate(("getAgentRuntime", "response", "agentRuntimeVersion"), "2"),
        mutate(
            ("getAgentRuntime", "response", "workloadIdentityDetails"),
            {
                "workloadIdentityArn": (
                    f"arn:aws:bedrock-agentcore:us-west-2:{ACCOUNT}:"
                    "workload-identity-directory/default/workload-identity/"
                    "mr_lister_phase6-substitut1"
                )
            },
        ),
        mutate(
            ("getAgentRuntime", "response", "createdAt"),
            "2026-08-24T17:59:59+00:00",
        ),
        mutate(
            ("getAgentRuntime", "response", "lastUpdatedAt"),
            "2026-08-24T18:00:11+00:00",
        ),
        mutate(("getAgentRuntime", "response", "status"), "UPDATING"),
        mutate(
            ("getAgentRuntime", "response", "roleArn"),
            f"arn:aws:iam::{ACCOUNT}:role/substituted",
        ),
        mutate(
            (
                "getAgentRuntime",
                "response",
                "agentRuntimeArtifact",
                "codeConfiguration",
                "code",
                "s3",
                "versionId",
            ),
            "substituted-version",
        ),
        mutate(
            ("getAgentRuntime", "response", "environmentVariables"),
            {"MR_LISTER_RELEASE_FINGERPRINT": "f" * 64},
        ),
        mutate(
            ("getAgentRuntime", "response", "networkConfiguration"),
            {"networkMode": "VPC"},
        ),
        mutate(
            ("getAgentRuntime", "response", "lifecycleConfiguration"),
            {"idleRuntimeSessionTimeout": 901, "maxLifetime": 3600},
        ),
        mutate(
            ("getAgentRuntime", "response", "protocolConfiguration"),
            {"serverProtocol": "MCP"},
        ),
        mutate(
            ("listTagsForResource", "response", "tags"),
            {**_binding().tags, "ReleaseFingerprint": "f" * 64},
        ),
        mutate(("runtimeRenderManifestSHA256",), "0" * 64),
        {**json.loads(json.dumps(evidence)), "unexpected": True},
    )
    for altered in adversarial:
        _write_canonical(evidence_path, altered)
        with pytest.raises(direct.Phase6AgentCoreDirectCodeZipError):
            direct.verify_phase6_agentcore_runtime_v1_evidence(
                _binding(),
                _archive(),
                _remote(),
                runtime_v1_evidence_path=evidence_path,
                repository_root=repository,
            )

    for unsafe_metadata in ("absent", None, {"requireMMDSV2": False}):
        altered = json.loads(json.dumps(evidence))
        get_response = altered["getAgentRuntime"]["response"]
        if unsafe_metadata == "absent":
            get_response.pop("metadataConfiguration")
        else:
            get_response["metadataConfiguration"] = unsafe_metadata
        _write_canonical(evidence_path, altered)
        with pytest.raises(direct.Phase6AgentCoreDirectCodeZipError):
            direct.verify_phase6_agentcore_runtime_v1_evidence(
                _binding(),
                _archive(),
                _remote(),
                runtime_v1_evidence_path=evidence_path,
                repository_root=repository,
            )

    evidence_path.write_text(json.dumps(evidence), encoding="utf-8")
    with pytest.raises(direct.Phase6AgentCoreDirectCodeZipError):
        direct.verify_phase6_agentcore_runtime_v1_evidence(
            _binding(),
            _archive(),
            _remote(),
            runtime_v1_evidence_path=evidence_path,
            repository_root=repository,
        )

    target = tmp_path / "real-evidence.json"
    _write_canonical(target, evidence)
    evidence_path.unlink()
    evidence_path.symlink_to(target)
    with pytest.raises(direct.Phase6AgentCoreDirectCodeZipError):
        direct.verify_phase6_agentcore_runtime_v1_evidence(
            _binding(),
            _archive(),
            _remote(),
            runtime_v1_evidence_path=evidence_path,
            repository_root=repository,
        )


def test_artifact_verifier_checks_descriptor_release_archive_hash_and_size(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    deployment = tmp_path / "phase6-deployment"
    artifacts = tmp_path / "phase6-artifacts"
    deployment.mkdir()
    artifacts.mkdir()
    raw_archive = b"sealed-agentcore-zip-bytes"
    archive_sha = sha256(raw_archive).hexdigest()
    (artifacts / "phase6-agentcore.zip").write_bytes(raw_archive)
    (artifacts / "deployment-descriptor.json").write_text("{}\n", encoding="utf-8")
    descriptor = {
        "release_fingerprint": RELEASE,
        "components": {
            "agentcore": {
                "archive": {
                    "path": "phase6-agentcore.zip",
                    "sha256": archive_sha,
                    "size_bytes": len(raw_archive),
                },
                "architecture": "arm64",
                "component": "agentcore",
                "deployment_manifest_sha256": "d" * 64,
                "package_format": "zip",
                "runtime": "python3.12",
            },
            "lambda": {},
        },
    }
    monkeypatch.setattr(
        direct,
        "verify_phase6_deployment_artifacts",
        lambda *_args, **_kwargs: descriptor,
    )
    binding = _binding(agentcore_archive_sha256=archive_sha)
    verified = direct.verify_phase6_agentcore_direct_codezip_artifact(
        binding,
        deployment_root=deployment,
        artifact_root=artifacts,
    )
    assert verified.sha256 == archive_sha
    assert verified.size_bytes == len(raw_archive)
    assert verified.checksum_sha256_base64 == base64.b64encode(sha256(raw_archive).digest()).decode(
        "ascii"
    )
    plan = json.loads(direct.render_phase6_agentcore_upload_plan(binding, verified))
    expected_archive_path = (artifacts / "phase6-agentcore.zip").resolve().as_posix()
    assert plan["artifact"]["localPath"] == expected_archive_path
    assert plan["putObjectArguments"][plan["putObjectArguments"].index("--body") + 1] == (
        expected_archive_path
    )

    with pytest.raises(direct.Phase6AgentCoreDirectCodeZipError):
        direct.verify_phase6_agentcore_direct_codezip_artifact(
            _binding(agentcore_archive_sha256="e" * 64),
            deployment_root=deployment,
            artifact_root=artifacts,
        )
    descriptor["release_fingerprint"] = "f" * 64
    with pytest.raises(direct.Phase6AgentCoreDirectCodeZipError):
        direct.verify_phase6_agentcore_direct_codezip_artifact(
            binding,
            deployment_root=deployment,
            artifact_root=artifacts,
        )


def test_artifact_verifier_rejects_symlinked_archive(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    deployment = tmp_path / "phase6-deployment"
    artifacts = tmp_path / "phase6-artifacts"
    deployment.mkdir()
    artifacts.mkdir()
    real = tmp_path / "real.zip"
    real.write_bytes(b"bytes")
    (artifacts / "phase6-agentcore.zip").symlink_to(real)
    (artifacts / "deployment-descriptor.json").write_text("{}\n", encoding="utf-8")
    monkeypatch.setattr(
        direct,
        "verify_phase6_deployment_artifacts",
        lambda *_args, **_kwargs: {},
    )
    with pytest.raises(direct.Phase6AgentCoreDirectCodeZipError):
        direct.verify_phase6_agentcore_direct_codezip_artifact(
            _binding(),
            deployment_root=deployment,
            artifact_root=artifacts,
        )


def test_write_and_verify_are_exclusive_and_fail_closed_on_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "repository"
    root.mkdir()
    monkeypatch.setattr(
        direct,
        "verify_phase6_agentcore_direct_codezip_artifact",
        lambda *_args, **_kwargs: _archive(),
    )
    monkeypatch.setattr(
        direct,
        "_existing_phase6_environment",
        lambda *_args, **_kwargs: {"MR_LISTER_RELEASE_FINGERPRINT": RELEASE},
    )
    monkeypatch.setattr(direct, "_verify_remote_evidence", lambda *_args: _remote())
    evidence = tmp_path / "closed-evidence.json"
    written = direct.write_phase6_agentcore_runtime_documents(
        _binding(),
        object_binding_evidence=evidence,
        repository_root=root,
    )
    assert set(written) == {root / path for path in _runtime_documents()}
    direct.verify_phase6_agentcore_runtime_documents(
        _binding(),
        object_binding_evidence=evidence,
        repository_root=root,
    )
    with pytest.raises(direct.Phase6AgentCoreDirectCodeZipError):
        direct.write_phase6_agentcore_runtime_documents(
            _binding(),
            object_binding_evidence=evidence,
            repository_root=root,
        )
    (root / direct.RUNTIME_CREATE_OUTPUT).write_bytes(b"{}\n")
    with pytest.raises(direct.Phase6AgentCoreDirectCodeZipError):
        direct.verify_phase6_agentcore_runtime_documents(
            _binding(),
            object_binding_evidence=evidence,
            repository_root=root,
        )


def test_preexisting_runtime_output_blocks_every_new_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "repository"
    destination = root / direct.RUNTIME_CREATE_OUTPUT
    destination.parent.mkdir(parents=True)
    destination.write_bytes(b"owned\n")
    monkeypatch.setattr(
        direct,
        "verify_phase6_agentcore_direct_codezip_artifact",
        lambda *_args, **_kwargs: _archive(),
    )
    monkeypatch.setattr(
        direct,
        "_existing_phase6_environment",
        lambda *_args, **_kwargs: {"MR_LISTER_RELEASE_FINGERPRINT": RELEASE},
    )
    monkeypatch.setattr(direct, "_verify_remote_evidence", lambda *_args: _remote())
    with pytest.raises(direct.Phase6AgentCoreDirectCodeZipError):
        direct.write_phase6_agentcore_runtime_documents(
            _binding(),
            object_binding_evidence=tmp_path / "closed-evidence.json",
            repository_root=root,
        )
    assert destination.read_bytes() == b"owned\n"
    for relative in (
        direct.UPLOAD_PLAN_OUTPUT,
        direct.AUTHORIZATION_RESIDUAL_OUTPUT,
        direct.RUNTIME_MANIFEST_OUTPUT,
    ):
        assert not (root / relative).exists()


def test_symlinked_runtime_output_blocks_every_new_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "repository"
    destination = root / direct.RUNTIME_CREATE_OUTPUT
    destination.parent.mkdir(parents=True)
    target = tmp_path / "outside.json"
    target.write_bytes(b"owned\n")
    destination.symlink_to(target)
    monkeypatch.setattr(
        direct,
        "verify_phase6_agentcore_direct_codezip_artifact",
        lambda *_args, **_kwargs: _archive(),
    )
    monkeypatch.setattr(
        direct,
        "_existing_phase6_environment",
        lambda *_args, **_kwargs: {"MR_LISTER_RELEASE_FINGERPRINT": RELEASE},
    )
    monkeypatch.setattr(direct, "_verify_remote_evidence", lambda *_args: _remote())
    with pytest.raises(direct.Phase6AgentCoreDirectCodeZipError):
        direct.write_phase6_agentcore_runtime_documents(
            _binding(),
            object_binding_evidence=tmp_path / "closed-evidence.json",
            repository_root=root,
        )
    assert target.read_bytes() == b"owned\n"
    assert destination.is_symlink()


def test_endpoint_write_and_verify_require_intact_runtime_stage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "repository"
    root.mkdir()
    monkeypatch.setattr(
        direct,
        "verify_phase6_agentcore_direct_codezip_artifact",
        lambda *_args, **_kwargs: _archive(),
    )
    monkeypatch.setattr(
        direct,
        "_existing_phase6_environment",
        lambda *_args, **_kwargs: {"MR_LISTER_RELEASE_FINGERPRINT": RELEASE},
    )
    monkeypatch.setattr(direct, "_verify_remote_evidence", lambda *_args: _remote())
    evidence = tmp_path / "closed-evidence.json"
    direct.write_phase6_agentcore_runtime_documents(
        _binding(),
        object_binding_evidence=evidence,
        repository_root=root,
    )
    runtime_documents = {
        path: (root / path).read_bytes()
        for path in (
            direct.RUNTIME_CREATE_OUTPUT,
            direct.RUNTIME_MANIFEST_OUTPUT,
        )
    }
    runtime_evidence = tmp_path / "runtime-v1-evidence.json"
    _write_canonical(runtime_evidence, _runtime_evidence_document(runtime_documents))
    written = direct.write_phase6_agentcore_endpoint_documents(
        _binding(),
        object_binding_evidence=evidence,
        runtime_v1_evidence=runtime_evidence,
        repository_root=root,
    )
    assert set(written) == {
        root / direct.ENDPOINT_CREATE_OUTPUT,
        root / direct.ENDPOINT_MANIFEST_OUTPUT,
    }
    direct.verify_phase6_agentcore_endpoint_documents(
        _binding(),
        object_binding_evidence=evidence,
        runtime_v1_evidence=runtime_evidence,
        repository_root=root,
    )
    with pytest.raises(direct.Phase6AgentCoreDirectCodeZipError):
        direct.write_phase6_agentcore_endpoint_documents(
            _binding(),
            object_binding_evidence=evidence,
            runtime_v1_evidence=runtime_evidence,
            repository_root=root,
        )
    (root / direct.RUNTIME_MANIFEST_OUTPUT).write_bytes(b"{}\n")
    with pytest.raises(direct.Phase6AgentCoreDirectCodeZipError):
        direct.verify_phase6_agentcore_endpoint_documents(
            _binding(),
            object_binding_evidence=evidence,
            runtime_v1_evidence=runtime_evidence,
            repository_root=root,
        )


def test_renderer_source_is_offline_and_does_not_use_agentcore_packager() -> None:
    source = Path(direct.__file__).read_text(encoding="utf-8")
    assert "import boto" not in source
    assert "boto3.client" not in source
    assert "subprocess" not in source
    assert "agentcore deploy" not in source.casefold()
    assert "UpdateAgentRuntime" not in json.dumps(
        json.loads(_runtime_documents()[direct.RUNTIME_CREATE_OUTPUT])
    )
