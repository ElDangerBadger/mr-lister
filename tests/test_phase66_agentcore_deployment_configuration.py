from __future__ import annotations

import json
from hashlib import sha256
from pathlib import Path

import pytest

from mr_lister.review_profile import FilesystemReviewProductAuthority
from tools.render_phase6_agentcore_deployment import (
    AGENTCORE_OUTPUT,
    AWS_TARGETS_OUTPUT,
    DEPLOYMENT_PLAN_OUTPUT,
    LOG_RETENTION_POLICY_OUTPUT,
    RENDER_MANIFEST_OUTPUT,
    RUNTIME_POLICY_OUTPUT,
    RUNTIME_TRUST_POLICY_OUTPUT,
    Phase6AgentCoreDeploymentBinding,
    Phase6AgentCoreDeploymentError,
    render_phase6_agentcore_deployment,
    verify_phase6_agentcore_release,
    write_phase6_agentcore_deployment,
)

ROOT = Path(__file__).parents[1]
ACCOUNT = "123456789012"
REGION = "us-west-2"
ENVIRONMENT = "prod-west"
RELEASE = "a" * 64
VERSION = "17"

TEMPLATES = (
    Path("infra/agentcore/mrlisterphase6/agentcore/agentcore.json.tmpl"),
    Path("infra/agentcore/mrlisterphase6/agentcore/aws-targets.json.tmpl"),
    Path("infra/agentcore/mrlisterphase6/deployment-plan.json.tmpl"),
    Path("infra/iam/phase6-agentcore-runtime-policy.json.tmpl"),
    Path("infra/iam/phase6-agentcore-runtime-trust-policy.json.tmpl"),
    Path("infra/iam/phase6-agentcore-log-retention-policy.json.tmpl"),
)


def _binding(**overrides: object) -> Phase6AgentCoreDeploymentBinding:
    values: dict[str, object] = {
        "account_id": ACCOUNT,
        "region": REGION,
        "environment": ENVIRONMENT,
        "release_fingerprint": RELEASE,
        "runtime_version": VERSION,
    }
    values.update(overrides)
    return Phase6AgentCoreDeploymentBinding(**values)  # type: ignore[arg-type]


def _rendered() -> dict[Path, bytes]:
    return render_phase6_agentcore_deployment(_binding())


def _json_document(documents: dict[Path, bytes], path: Path) -> object:
    return json.loads(documents[path])


def test_phase6_agentcore_template_is_schema_shaped_and_release_bound() -> None:
    documents = _rendered()
    config = _json_document(documents, AGENTCORE_OUTPUT)
    assert isinstance(config, dict)
    assert config["name"] == "mrlisterphase6"
    assert config["managedBy"] == "CDK"
    assert config["tags"] == {
        "mr-lister:component": "preparation-runtime",
        "mr-lister:environment": ENVIRONMENT,
        "mr-lister:release-fingerprint": RELEASE,
    }

    [runtime] = config["runtimes"]
    assert runtime["name"] == "mr_lister_phase6"
    assert runtime["build"] == "CodeZip"
    assert runtime["entrypoint"] == "main.py"
    assert runtime["codeLocation"] == ("../../../.mr_lister_private/phase6-deployment/agentcore")
    assert runtime["runtimeVersion"] == "PYTHON_3_12"
    assert runtime["networkMode"] == "PUBLIC"
    assert runtime["protocol"] == "HTTP"
    assert runtime["authorizerType"] == "AWS_IAM"
    assert runtime["instrumentation"] == {"enableOtel": False}
    assert runtime["executionRoleArn"] == (
        f"arn:aws:iam::{ACCOUNT}:role/mr-lister-phase6-agentcore-runtime-{ENVIRONMENT}"
    )
    assert runtime["endpoints"] == {
        "phase6_v17_prod_west": {
            "description": "Immutable Phase 6 version 17 endpoint for prod-west",
            "version": 17,
        }
    }

    env_vars = {item["name"]: item["value"] for item in runtime["envVars"]}
    assert env_vars["AWS_REGION"] == REGION
    assert env_vars["MR_LISTER_ENVIRONMENT"] == ENVIRONMENT
    assert env_vars["MR_LISTER_AWS_ACCOUNT_ID"] == ACCOUNT
    assert env_vars["MR_LISTER_RELEASE_FINGERPRINT"] == RELEASE
    assert env_vars["MR_LISTER_STATE_TABLE"] == f"mr-lister-phase6-{ENVIRONMENT}"
    assert env_vars["MR_LISTER_ARTIFACT_BUCKET"] == (
        f"mr-lister-phase6-artifacts-{ENVIRONMENT}-{ACCOUNT}-{REGION}"
    )
    assert env_vars["MR_LISTER_GEMMA_CONFIG_PATH"].startswith("/var/task/")
    assert env_vars["MR_LISTER_PRODUCT_PROFILE_PATH"].startswith("/var/task/")
    assert env_vars["MR_LISTER_STRANDS_CONTROLLER_MODEL_ID"] == ("us.amazon.nova-2-lite-v1:0")

    for empty_resource in (
        "memories",
        "knowledgeBases",
        "credentials",
        "evaluators",
        "onlineEvalConfigs",
        "agentCoreGateways",
        "policyEngines",
        "configBundles",
        "abTests",
        "harnesses",
        "datasets",
        "payments",
    ):
        assert config[empty_resource] == []
    assert "connections" not in runtime
    assert "additionalPolicies" not in runtime

    targets = _json_document(documents, AWS_TARGETS_OUTPUT)
    assert targets == [
        {
            "account": ACCOUNT,
            "description": f"Release-bound Mr Lister Phase 6 target for {ENVIRONMENT}",
            "name": ENVIRONMENT,
            "region": REGION,
        }
    ]


def test_runtime_policy_has_only_composed_phase6_data_and_model_capabilities() -> None:
    documents = _rendered()
    policy = _json_document(documents, RUNTIME_POLICY_OUTPUT)
    assert isinstance(policy, dict)
    statements = {statement["Sid"]: statement for statement in policy["Statement"]}
    actions = {
        action
        for statement in statements.values()
        for action in (
            statement["Action"] if isinstance(statement["Action"], list) else [statement["Action"]]
        )
    }
    assert actions == {
        "logs:DescribeLogStreams",
        "logs:CreateLogGroup",
        "logs:PutResourcePolicy",
        "logs:DescribeLogGroups",
        "logs:CreateLogStream",
        "logs:PutLogEvents",
        "dynamodb:ConditionCheckItem",
        "dynamodb:GetItem",
        "dynamodb:PutItem",
        "s3:GetObjectVersion",
        "bedrock:InvokeModel",
    }
    dynamodb = statements["ReadAndCommitOnlyPhase6PreparationState"]
    assert dynamodb["Resource"] == (
        f"arn:aws:dynamodb:{REGION}:{ACCOUNT}:table/mr-lister-phase6-{ENVIRONMENT}"
    )
    assert dynamodb["Condition"] == {
        "ForAllValues:StringLike": {"dynamodb:LeadingKeys": ["JOB#*", "OWNER#*"]},
        "Null": {"dynamodb:LeadingKeys": "false"},
    }
    assert statements["ReadOnlyPinnedPhase6SourceVersions"]["Resource"] == (
        f"arn:aws:s3:::mr-lister-phase6-artifacts-{ENVIRONMENT}-{ACCOUNT}-{REGION}/"
        "private/owners/*/jobs/*/source/source.png"
    )
    runtime_log_prefix = (
        f"arn:aws:logs:{REGION}:{ACCOUNT}:log-group:"
        "/aws/bedrock-agentcore/runtimes/mr_lister_phase6-*"
    )
    assert statements["CreateAndInspectAgentCoreRuntimeLogs"]["Resource"] == runtime_log_prefix
    assert statements["ConfigureOnlyPhase6AgentCoreRuntimeLogs"]["Resource"] == (runtime_log_prefix)
    assert statements["WriteOnlyPhase6AgentCoreRuntimeLogs"]["Resource"] == (
        f"{runtime_log_prefix}:log-stream:*"
    )

    bedrock = [
        statement
        for statement in statements.values()
        if statement["Action"] == "bedrock:InvokeModel"
    ]
    assert len(bedrock) == 3
    serialized_bedrock = json.dumps(bedrock, sort_keys=True)
    assert "us.amazon.nova-2-lite-v1:0" in serialized_bedrock
    assert "google.gemma-3-27b-it" in serialized_bedrock
    assert "anthropic" not in serialized_bedrock
    assert "InvokeModelWithResponseStream" not in serialized_bedrock

    serialized = json.dumps(policy, sort_keys=True).casefold()
    assert "/runtimes/*mr_lister_phase6" not in serialized
    for forbidden in (
        "secretsmanager:",
        "states:",
        "execute-api:",
        "apigateway:",
        "printify",
        "etsy",
        "publication",
        "dynamodb:transactgetitems",
        "dynamodb:transactwriteitems",
    ):
        assert forbidden not in serialized


def test_trust_and_log_retention_are_separate_and_version_scoped() -> None:
    documents = _rendered()
    trust = _json_document(documents, RUNTIME_TRUST_POLICY_OUTPUT)
    assert trust == {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Action": "sts:AssumeRole",
                "Condition": {
                    "ArnLike": {
                        "aws:SourceArn": (
                            f"arn:aws:bedrock-agentcore:{REGION}:{ACCOUNT}:"
                            "runtime/mr_lister_phase6-*"
                        )
                    },
                    "StringEquals": {"aws:SourceAccount": ACCOUNT},
                },
                "Effect": "Allow",
                "Principal": {"Service": "bedrock-agentcore.amazonaws.com"},
                "Sid": "TrustOnlyPhase6AgentCoreRuntime",
            }
        ],
    }

    runtime_policy = _json_document(documents, RUNTIME_POLICY_OUTPUT)
    runtime_actions = json.dumps(runtime_policy)
    assert "logs:PutRetentionPolicy" not in runtime_actions

    retention = _json_document(documents, LOG_RETENTION_POLICY_OUTPUT)
    assert retention["Statement"][0] == {
        "Action": "logs:DescribeLogGroups",
        "Effect": "Allow",
        "Resource": "*",
        "Sid": "DiscoverAgentCoreRuntimeLogGroupsForRetention",
    }
    assert retention["Statement"][1]["Action"] == "logs:PutRetentionPolicy"
    assert retention["Statement"][1]["Resource"] == (
        f"arn:aws:logs:{REGION}:{ACCOUNT}:log-group:/aws/bedrock-agentcore/runtimes/"
        "mr_lister_phase6-??????????-phase6_v17_prod_west:*"
    )

    plan = _json_document(documents, DEPLOYMENT_PLAN_OUTPUT)
    assert plan["logRetention"] == {
        "applyAction": "logs:PutRetentionPolicy",
        "logGroupNamePattern": (
            "/aws/bedrock-agentcore/runtimes/mr_lister_phase6-??????????-phase6_v17_prod_west"
        ),
        "requiredBeforeTraffic": True,
        "retentionInDays": 14,
        "verifyAction": "logs:DescribeLogGroups",
    }
    assert plan["runtime"]["immutableVersion"] == VERSION
    assert plan["runtime"]["endpointName"] == "phase6_v17_prod_west"


def test_render_is_canonical_deterministic_and_manifest_bound() -> None:
    first = _rendered()
    second = _rendered()
    assert first == second
    assert set(first) == {
        AGENTCORE_OUTPUT,
        AWS_TARGETS_OUTPUT,
        DEPLOYMENT_PLAN_OUTPUT,
        RUNTIME_POLICY_OUTPUT,
        RUNTIME_TRUST_POLICY_OUTPUT,
        LOG_RETENTION_POLICY_OUTPUT,
        RENDER_MANIFEST_OUTPUT,
    }
    for path, content in first.items():
        assert content.endswith(b"\n")
        assert json.dumps(json.loads(content), allow_nan=False)
        if path != RENDER_MANIFEST_OUTPUT:
            assert b"<AWS_" not in content

    manifest = _json_document(first, RENDER_MANIFEST_OUTPUT)
    assert manifest["binding"] == {
        "accountId": ACCOUNT,
        "endpointName": "phase6_v17_prod_west",
        "environment": ENVIRONMENT,
        "region": REGION,
        "releaseFingerprint": RELEASE,
        "runtimeName": "mr_lister_phase6",
        "runtimeVersion": VERSION,
    }
    assert manifest["logRetentionInDays"] == 14
    assert manifest["documents"] == {
        path.as_posix(): sha256(content).hexdigest()
        for path, content in first.items()
        if path != RENDER_MANIFEST_OUTPUT
    }

    serialized = b"\n".join(first.values()).decode("utf-8").casefold()
    assert "default" not in serialized
    assert "phase3" not in serialized
    assert "placeholder" not in serialized


@pytest.mark.parametrize(
    "overrides",
    [
        {"account_id": "000000000000"},
        {"account_id": "123"},
        {"region": "us-east-1"},
        {"environment": "DEFAULT"},
        {"environment": "phase3-prod"},
        {"environment": "placeholder"},
        {"release_fingerprint": "0" * 64},
        {"release_fingerprint": "A" * 64},
        {"runtime_version": 0},
        {"runtime_version": "01"},
        {"runtime_version": "DEFAULT"},
        {"runtime_version": True},
        {"runtime_version": 100000},
    ],
)
def test_binding_refuses_moving_placeholder_or_cross_environment_values(
    overrides: dict[str, object],
) -> None:
    with pytest.raises(Phase6AgentCoreDeploymentError) as captured:
        _binding(**overrides)

    assert str(captured.value) == "Phase 6 AgentCore deployment configuration is invalid"


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ("mr_lister_phase6", "mr_lister_phase3"),
        ("preparation-runtime", "<UNKNOWN_PLACEHOLDER>"),
        (
            "../../../.mr_lister_private/phase6-deployment/agentcore",
            "../../../some-other-bundle",
        ),
    ],
)
def test_template_identity_or_unresolved_token_drift_fails_closed(
    tmp_path: Path,
    old: str,
    new: str,
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    for relative in TEMPLATES:
        destination = repository / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes((ROOT / relative).read_bytes())
    target = repository / TEMPLATES[0]
    template = target.read_text(encoding="utf-8")
    assert old in template
    target.write_text(template.replace(old, new, 1), encoding="utf-8")

    with pytest.raises(Phase6AgentCoreDeploymentError):
        render_phase6_agentcore_deployment(_binding(), repository_root=repository)


def test_reviewed_templates_are_json_and_fixed_fingerprints_match_inputs() -> None:
    for relative in TEMPLATES:
        assert json.loads((ROOT / relative).read_text(encoding="utf-8")) is not None

    documents = _rendered()
    config = _json_document(documents, AGENTCORE_OUTPUT)
    [runtime] = config["runtimes"]
    environment = {item["name"]: item["value"] for item in runtime["envVars"]}

    profile_path = ROOT / "config/product_profiles/gildan_64000_swiftpod.json"
    profile = FilesystemReviewProductAuthority(profile_directory=profile_path.parent).get_exact(
        profile_id="gildan_64000_swiftpod",
        profile_version=2,
    )
    gemma_path = ROOT / "config/bedrock/google_gemma_3_27b_it.json"
    assert environment["MR_LISTER_PRODUCT_PROFILE_FINGERPRINT"] == profile.fingerprint
    assert (
        environment["MR_LISTER_GEMMA_CONFIG_FINGERPRINT"]
        == sha256(gemma_path.read_bytes()).hexdigest()
    )


def test_release_verification_refuses_any_other_artifact_path_before_writing(
    tmp_path: Path,
) -> None:
    invalid_artifact = tmp_path / "other-artifact"
    invalid_artifact.mkdir()
    with pytest.raises(Phase6AgentCoreDeploymentError):
        verify_phase6_agentcore_release(_binding(), artifact_root=invalid_artifact)

    repository = tmp_path / "repository"
    repository.mkdir()
    with pytest.raises(Phase6AgentCoreDeploymentError):
        write_phase6_agentcore_deployment(
            _binding(),
            repository_root=repository,
            artifact_root=invalid_artifact,
        )
    assert list(repository.iterdir()) == []


def test_readme_states_deployed_runtime_and_external_retention_controls() -> None:
    readme = (ROOT / "infra/agentcore/mrlisterphase6/README.md").read_text(encoding="utf-8")
    assert "deployed Phase 6 preparation runtime" in readme
    assert "phase6_v4_dev" in readme
    assert "retentionInDays=14" in readme
    assert "mandatory before traffic" in readme
    assert "physical runtime ID" in readme
    assert "model" in readme.casefold()
    assert "iam:PassRole" in readme
