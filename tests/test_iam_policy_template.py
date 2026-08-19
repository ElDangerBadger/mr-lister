from __future__ import annotations

import json
from pathlib import Path


def _load_policy(path: str) -> tuple[list[dict], str]:
    template = Path(path).read_text(encoding="utf-8")
    assert template.count("<AWS_ACCOUNT_ID>") == 2
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
