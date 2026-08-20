from __future__ import annotations

import json
from pathlib import Path

import pytest

from mr_lister.workflow.secrets import SecretsManagerSecretReader

SECRET_ARN = "arn:aws:secretsmanager:us-west-2:123456789012:secret:mr-lister/dev/printify-x"


class RecordingSecretsManager:
    def __init__(self, value: object) -> None:
        self.value = value
        self.requests: list[dict[str, str]] = []

    def get_secret_value(self, **request: str) -> dict[str, object]:
        self.requests.append(request)
        return {"SecretString": self.value}


def test_secret_reader_requests_only_the_exact_arn() -> None:
    client = RecordingSecretsManager("private-value")

    value = SecretsManagerSecretReader(client).get_secret(SECRET_ARN)

    assert value == "private-value"
    assert client.requests == [{"SecretId": SECRET_ARN}]


@pytest.mark.parametrize("value", [None, "", b"binary-secret"])
def test_secret_reader_rejects_missing_or_binary_values(value: object) -> None:
    with pytest.raises(ValueError, match="SecretString"):
        SecretsManagerSecretReader(RecordingSecretsManager(value)).get_secret(SECRET_ARN)


def test_phase4_secret_policy_is_read_only_and_placeholder_scoped() -> None:
    policy = json.loads(
        Path("infra/iam/phase4-marketplace-secret-read-policy.json.tmpl").read_text()
    )

    assert policy["Statement"] == [
        {
            "Sid": "ReadOnlyTheConfiguredMarketplaceSecret",
            "Effect": "Allow",
            "Action": [
                "secretsmanager:DescribeSecret",
                "secretsmanager:GetSecretValue",
            ],
            "Resource": "<MARKETPLACE_SECRET_ARN>",
        }
    ]
