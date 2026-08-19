from __future__ import annotations

import json
from pathlib import Path


def _load_policy(path: str, *, account_id_occurrences: int = 2) -> tuple[list[dict], str]:
    template = Path(path).read_text(encoding="utf-8")
    assert template.count("<AWS_ACCOUNT_ID>") == account_id_occurrences
    account_id = "123456789012"
    policy = json.loads(template.replace("<AWS_ACCOUNT_ID>", account_id))
    return policy["Statement"], json.dumps(policy)


def _assert_narrow(statements: list[dict], serialized: str) -> None:
    assert len(statements) == 2
    assert {statement["Action"] for statement in statements} == {"bedrock:InvokeModel"}
    assert "*" not in serialized


def test_claude_policy_is_narrow_and_renderable() -> None:
    statements, serialized = _load_policy(
        "infra/iam/bedrock-claude-sonnet-4-6-invoke-policy.json.tmpl"
    )
    _assert_narrow(statements, serialized)

    profile_arn = (
        "arn:aws:bedrock:us-west-2:123456789012:inference-profile/us.anthropic.claude-sonnet-4-6"
    )
    assert statements[0]["Resource"] == profile_arn
    assert statements[1]["Resource"] == [
        "arn:aws:bedrock:us-east-1::foundation-model/anthropic.claude-sonnet-4-6",
        "arn:aws:bedrock:us-east-2::foundation-model/anthropic.claude-sonnet-4-6",
        "arn:aws:bedrock:us-west-2::foundation-model/anthropic.claude-sonnet-4-6",
    ]
    assert statements[1]["Condition"] == {
        "StringEquals": {"bedrock:InferenceProfileArn": profile_arn}
    }


def test_nova_policy_is_narrow_and_renderable() -> None:
    statements, serialized = _load_policy("infra/iam/bedrock-nova-2-lite-invoke-policy.json.tmpl")
    _assert_narrow(statements, serialized)
    profile_arn = (
        "arn:aws:bedrock:us-west-2:123456789012:inference-profile/us.amazon.nova-2-lite-v1:0"
    )
    assert statements[0]["Resource"] == profile_arn
    assert statements[1]["Resource"] == [
        "arn:aws:bedrock:us-east-1::foundation-model/amazon.nova-2-lite-v1:0",
        "arn:aws:bedrock:us-east-2::foundation-model/amazon.nova-2-lite-v1:0",
        "arn:aws:bedrock:us-west-2::foundation-model/amazon.nova-2-lite-v1:0",
    ]
    assert statements[1]["Condition"] == {
        "StringEquals": {"bedrock:InferenceProfileArn": profile_arn}
    }


def test_luna_policy_is_narrow_and_renderable() -> None:
    statements, serialized = _load_policy(
        "infra/iam/bedrock-openai-gpt-5-6-luna-invoke-policy.json.tmpl",
        account_id_occurrences=3,
    )
    assert len(statements) == 3
    assert {statement["Action"] for statement in statements} == {"bedrock:InvokeModel"}
    assert "*" not in serialized

    profile_arn = "arn:aws:bedrock:us-west-2:123456789012:inference-profile/us.openai.gpt-5.6-luna"
    assert statements[0]["Resource"] == profile_arn
    assert statements[1]["Resource"] == [
        "arn:aws:bedrock:us-east-1::foundation-model/openai.gpt-5.6-luna",
        "arn:aws:bedrock:us-east-2::foundation-model/openai.gpt-5.6-luna",
        "arn:aws:bedrock:us-west-2::foundation-model/openai.gpt-5.6-luna",
    ]
    assert statements[1]["Condition"] == {
        "StringEquals": {"bedrock:InferenceProfileArn": profile_arn}
    }
    assert statements[2]["Resource"] == ("arn:aws:bedrock:us-west-2:123456789012:project/default")


def test_gemma_policy_is_direct_in_region_and_narrow() -> None:
    statements, serialized = _load_policy(
        "infra/iam/bedrock-google-gemma-3-27b-it-invoke-policy.json",
        account_id_occurrences=0,
    )
    assert len(statements) == 1
    assert statements[0]["Action"] == "bedrock:InvokeModel"
    assert statements[0]["Resource"] == (
        "arn:aws:bedrock:us-west-2::foundation-model/google.gemma-3-27b-it"
    )
    assert "*" not in serialized


def test_agentcore_runtime_trusts_only_the_runtime_service() -> None:
    template = Path("infra/iam/agentcore-runtime-trust-policy.json.tmpl").read_text(
        encoding="utf-8"
    )
    assert template.count("<AWS_ACCOUNT_ID>") == 2
    policy = json.loads(template.replace("<AWS_ACCOUNT_ID>", "123456789012"))

    assert policy["Statement"] == [
        {
            "Sid": "TrustOnlyThisAccountsAgentCoreResources",
            "Effect": "Allow",
            "Principal": {"Service": "bedrock-agentcore.amazonaws.com"},
            "Action": "sts:AssumeRole",
            "Condition": {
                "StringEquals": {"aws:SourceAccount": "123456789012"},
                "ArnLike": {
                    "aws:SourceArn": ("arn:aws:bedrock-agentcore:us-west-2:123456789012:*")
                },
            },
        }
    ]


def test_agentcore_runtime_policy_has_logs_and_narrow_nova_only() -> None:
    statements, serialized = _load_policy(
        "infra/iam/agentcore-runtime-policy.json.tmpl",
        account_id_occurrences=6,
    )

    actions = {
        action
        for statement in statements
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
        "bedrock:InvokeModel",
    }
    assert "bedrock:*" not in serialized
    assert "InvokeModelWithResponseStream" not in serialized


def test_agentcore_developer_policy_is_runtime_and_log_scoped() -> None:
    statements, serialized = _load_policy(
        "infra/iam/agentcore-phase3-deployer-policy.json.tmpl",
        account_id_occurrences=10,
    )

    assert len(statements) == 8
    assert statements[0]["Action"] == [
        "bedrock-agentcore:ListAgentRuntimes",
        "bedrock-agentcore:ListAgentRuntimeEndpoints",
    ]
    assert statements[0]["Resource"] == "*"
    assert statements[1]["Resource"].endswith(":runtime/*")
    assert statements[2]["Resource"] == [
        "arn:aws:bedrock-agentcore:us-west-2:123456789012:runtime/*",
        ("arn:aws:bedrock-agentcore:us-west-2:123456789012:runtime/*/runtime-endpoint/*"),
    ]
    assert statements[3]["Action"] == "bedrock-agentcore:InvokeAgentRuntime"
    assert statements[4]["Action"] == "bedrock-agentcore:StopRuntimeSession"
    assert statements[6]["Action"] == [
        "logs:DescribeLogStreams",
        "logs:FilterLogEvents",
    ]
    assert statements[7]["Action"] == "logs:GetLogEvents"
    assert all(resource.endswith(":log-stream:*") for resource in statements[7]["Resource"])
    assert "/aws/vendedlogs/bedrock-agentcore/" in serialized
    assert "iam:*" not in serialized
    assert "cloudformation:*" not in serialized
