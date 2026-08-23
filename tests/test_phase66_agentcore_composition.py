from __future__ import annotations

from hashlib import sha256
from pathlib import Path
from typing import Any

import pytest
from bedrock_agentcore import BedrockAgentCoreApp

from mr_lister.agent.phase6_composition import (
    PHASE6_GEMMA_MODEL_ID,
    PHASE6_STRANDS_CONTROLLER_MODEL_ID,
    ExactPinnedSourceS3,
    Phase6AgentCoreConfigurationError,
    Phase6AgentCoreDependencyError,
    compose_phase6_agentcore_runtime,
    load_phase6_agentcore_configuration,
)
from mr_lister.review_profile import FilesystemReviewProductAuthority

ROOT = Path(__file__).parents[1]
PROFILE_PATH = (ROOT / "config/product_profiles/gildan_64000_swiftpod.json").resolve()
GEMMA_PATH = (ROOT / "config/bedrock/google_gemma_3_27b_it.json").resolve()
PROFILE = FilesystemReviewProductAuthority(profile_directory=PROFILE_PATH.parent).get_exact(
    profile_id="gildan_64000_swiftpod",
    profile_version=2,
)
ACCOUNT = "123456789012"
REGION = "us-west-2"
OWNER = "a" * 64


def _environment() -> dict[str, object]:
    return {
        "AWS_REGION": REGION,
        "MR_LISTER_ENVIRONMENT": "dev",
        "MR_LISTER_AWS_ACCOUNT_ID": ACCOUNT,
        "MR_LISTER_STATE_TABLE": "mr-lister-phase6-dev",
        "MR_LISTER_ARTIFACT_BUCKET": f"mr-lister-phase6-artifacts-dev-{ACCOUNT}-{REGION}",
        "MR_LISTER_RELEASE_FINGERPRINT": "b" * 64,
        "MR_LISTER_PRODUCT_PROFILE_ID": "gildan_64000_swiftpod",
        "MR_LISTER_PRODUCT_PROFILE_VERSION": "2",
        "MR_LISTER_PRODUCT_PROFILE_FINGERPRINT": PROFILE.fingerprint,
        "MR_LISTER_PRODUCT_PROFILE_PATH": PROFILE_PATH.as_posix(),
        "MR_LISTER_GEMMA_CONFIG_PATH": GEMMA_PATH.as_posix(),
        "MR_LISTER_GEMMA_CONFIG_FINGERPRINT": sha256(GEMMA_PATH.read_bytes()).hexdigest(),
        "MR_LISTER_STRANDS_CONTROLLER_MODEL_ID": PHASE6_STRANDS_CONTROLLER_MODEL_ID,
    }


class FakeDynamo:
    def get_item(self, **kwargs: Any) -> dict[str, Any]:
        del kwargs
        return {}

    def put_item(self, **kwargs: Any) -> dict[str, Any]:
        del kwargs
        return {}

    def transact_write_items(self, **kwargs: Any) -> dict[str, Any]:
        del kwargs
        return {}


class FakeS3:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.response: dict[str, Any] = {
            "VersionId": "source-version-1",
            "ServerSideEncryption": "AES256",
            "ChecksumSHA256": "YWJjZA==",
            "ContentLength": 4,
            "ContentType": "image/png",
            "Body": object(),
        }
        self.error: Exception | None = None

    def get_object(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return self.response


class FakeIntelligence:
    def inspect_artwork(self, artwork: object, content: bytes) -> object:
        del artwork, content
        raise AssertionError("not invoked during composition")

    def draft_listing(self, artwork: object, content: bytes, analysis: object) -> object:
        del artwork, content, analysis
        raise AssertionError("not invoked during composition")


def test_configuration_pins_gemma_worker_and_nova_strands_controller() -> None:
    configuration = load_phase6_agentcore_configuration(_environment())

    assert configuration.intelligence.model_id == PHASE6_GEMMA_MODEL_ID
    assert configuration.intelligence.output_mode == "native_json_schema"
    assert configuration.intelligence.temperature == 0.0
    assert configuration.intelligence.max_repair_attempts == 2
    assert configuration.controller_model_id == PHASE6_STRANDS_CONTROLLER_MODEL_ID
    assert configuration.profile.exact.fingerprint == PROFILE.fingerprint


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("MR_LISTER_AWS_ACCOUNT_ID", "000000000000"),
        ("MR_LISTER_STATE_TABLE", "mr-lister-phase6-other"),
        ("MR_LISTER_ARTIFACT_BUCKET", "shared-artwork"),
        ("MR_LISTER_RELEASE_FINGERPRINT", "0" * 64),
        ("MR_LISTER_PRODUCT_PROFILE_FINGERPRINT", "c" * 64),
        ("MR_LISTER_GEMMA_CONFIG_FINGERPRINT", "d" * 64),
        ("MR_LISTER_STRANDS_CONTROLLER_MODEL_ID", "anthropic.other-model"),
    ],
)
def test_configuration_drift_is_one_generic_failure(name: str, value: str) -> None:
    environment = _environment()
    environment[name] = value

    with pytest.raises(Phase6AgentCoreConfigurationError) as captured:
        load_phase6_agentcore_configuration(environment)

    assert str(captured.value) == "Phase 6 AgentCore configuration is invalid"
    assert value not in str(captured.value)


def test_checked_claude_config_cannot_replace_gemma_without_detection() -> None:
    claude = (ROOT / "config/bedrock/claude_sonnet_4_6.json").resolve()
    environment = _environment()
    environment["MR_LISTER_GEMMA_CONFIG_PATH"] = claude.as_posix()
    environment["MR_LISTER_GEMMA_CONFIG_FINGERPRINT"] = sha256(claude.read_bytes()).hexdigest()

    with pytest.raises(Phase6AgentCoreConfigurationError):
        load_phase6_agentcore_configuration(environment)


def test_runtime_composition_has_real_phase6_backend_without_provider_client() -> None:
    configuration = load_phase6_agentcore_configuration(_environment())

    application = compose_phase6_agentcore_runtime(
        configuration,
        dynamodb_client=FakeDynamo(),
        s3_client=FakeS3(),
        intelligence=FakeIntelligence(),
        controller_model=PHASE6_STRANDS_CONTROLLER_MODEL_ID,
    )

    assert isinstance(application, BedrockAgentCoreApp)
    assert "printify" not in repr(application).casefold()


def test_runtime_rejects_wrong_controller_or_missing_dependency() -> None:
    configuration = load_phase6_agentcore_configuration(_environment())

    with pytest.raises(Phase6AgentCoreDependencyError):
        compose_phase6_agentcore_runtime(
            configuration,
            dynamodb_client=FakeDynamo(),
            s3_client=FakeS3(),
            intelligence=FakeIntelligence(),
            controller_model="us.amazon.nova-pro-v1:0",
        )
    with pytest.raises(Phase6AgentCoreDependencyError):
        compose_phase6_agentcore_runtime(
            configuration,
            dynamodb_client=object(),  # type: ignore[arg-type]
            s3_client=FakeS3(),
            intelligence=FakeIntelligence(),
            controller_model=PHASE6_STRANDS_CONTROLLER_MODEL_ID,
        )


def test_exact_source_reader_adds_checksum_and_expected_owner() -> None:
    client = FakeS3()
    reader = ExactPinnedSourceS3(
        client=client,
        bucket=f"mr-lister-phase6-artifacts-dev-{ACCOUNT}-{REGION}",
        bucket_owner_account_id=ACCOUNT,
    )
    key = f"private/owners/{OWNER}/jobs/job_1/source/source.png"

    response = reader.get_object(
        Bucket=f"mr-lister-phase6-artifacts-dev-{ACCOUNT}-{REGION}",
        Key=key,
        VersionId="source-version-1",
    )

    assert response["VersionId"] == "source-version-1"
    assert client.calls == [
        {
            "Bucket": f"mr-lister-phase6-artifacts-dev-{ACCOUNT}-{REGION}",
            "Key": key,
            "VersionId": "source-version-1",
            "ChecksumMode": "ENABLED",
            "ExpectedBucketOwner": ACCOUNT,
        }
    ]


@pytest.mark.parametrize(
    ("bucket", "key", "version"),
    [
        ("attacker", f"private/owners/{OWNER}/jobs/job_1/source/source.png", "version-1"),
        (
            f"mr-lister-phase6-artifacts-dev-{ACCOUNT}-{REGION}",
            "private/other.png",
            "version-1",
        ),
        (
            f"mr-lister-phase6-artifacts-dev-{ACCOUNT}-{REGION}",
            f"private/owners/{OWNER}/jobs/job_1/source/source.png",
            "null",
        ),
    ],
)
def test_exact_source_reader_rejects_authority_drift_before_s3(
    bucket: str,
    key: str,
    version: str,
) -> None:
    client = FakeS3()
    reader = ExactPinnedSourceS3(
        client=client,
        bucket=f"mr-lister-phase6-artifacts-dev-{ACCOUNT}-{REGION}",
        bucket_owner_account_id=ACCOUNT,
    )

    with pytest.raises(Phase6AgentCoreDependencyError):
        reader.get_object(Bucket=bucket, Key=key, VersionId=version)

    assert client.calls == []


def test_exact_source_reader_masks_dependency_and_response_drift() -> None:
    client = FakeS3()
    reader = ExactPinnedSourceS3(
        client=client,
        bucket=f"mr-lister-phase6-artifacts-dev-{ACCOUNT}-{REGION}",
        bucket_owner_account_id=ACCOUNT,
    )
    request = {
        "Bucket": f"mr-lister-phase6-artifacts-dev-{ACCOUNT}-{REGION}",
        "Key": f"private/owners/{OWNER}/jobs/job_1/source/source.png",
        "VersionId": "source-version-1",
    }
    client.error = RuntimeError("private AWS response")
    with pytest.raises(Phase6AgentCoreDependencyError) as captured:
        reader.get_object(**request)
    assert "private AWS response" not in str(captured.value)
    assert captured.value.__cause__ is None

    client.error = None
    client.response["ServerSideEncryption"] = "aws:kms"
    with pytest.raises(Phase6AgentCoreDependencyError):
        reader.get_object(**request)
