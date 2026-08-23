from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from mr_lister.agent.runtime_binding import agentcore_runtime_binding_fingerprint
from mr_lister.cloud.phase6_machine import (
    Phase6DispatcherHandler,
    Phase6PreparationHandler,
    Phase6ProviderHandler,
    Phase6SettlementHandler,
)
from mr_lister.cloud.phase6_machine_composition import (
    Phase6MachineConfigurationError,
    build_dispatcher_handler,
    build_preparation_handler,
    build_provider_handler,
    build_settlement_handler,
    load_dispatcher_configuration,
    load_preparation_configuration,
    load_provider_configuration,
    load_settlement_configuration,
)
from mr_lister.control.models import WorkType
from mr_lister.review_profile import FilesystemReviewProductAuthority

ROOT = Path(__file__).parents[1]
PROFILE_PATH = (ROOT / "config/product_profiles/gildan_64000_swiftpod.json").resolve()
PROFILE = FilesystemReviewProductAuthority(profile_directory=PROFILE_PATH.parent).get_exact(
    profile_id="gildan_64000_swiftpod",
    profile_version=2,
)
ACCOUNT = "123456789012"
REGION = "us-west-2"
ENVIRONMENT = "dev"
RELEASE = "d" * 64


def _base_environment() -> dict[str, object]:
    prefix = f"arn:aws:states:{REGION}:{ACCOUNT}:stateMachine:mr-lister-phase6-{ENVIRONMENT}"
    runtime_arn = (
        f"arn:aws:bedrock-agentcore:{REGION}:{ACCOUNT}:runtime/mr_lister_phase6_dev-AbCd123456"
    )
    qualifier = "phase6_v7_dev"
    endpoint_arn = f"{runtime_arn}/runtime-endpoint/{qualifier}"
    environment: dict[str, object] = {
        "AWS_REGION": REGION,
        "MR_LISTER_ENVIRONMENT": ENVIRONMENT,
        "MR_LISTER_AWS_ACCOUNT_ID": ACCOUNT,
        "MR_LISTER_STATE_TABLE": f"mr-lister-phase6-{ENVIRONMENT}",
        "MR_LISTER_RELEASE_FINGERPRINT": RELEASE,
        "MR_LISTER_PREPARE_MACHINE_ARN": f"{prefix}-prepare",
        "MR_LISTER_SYNCHRONIZE_PRODUCT_MACHINE_ARN": f"{prefix}-synchronize-product",
        "MR_LISTER_RECONCILE_PRODUCT_MACHINE_ARN": f"{prefix}-reconcile-product",
        "MR_LISTER_REFRESH_ECONOMICS_MACHINE_ARN": f"{prefix}-refresh-economics",
        "MR_LISTER_AGENTCORE_RUNTIME_ARN": runtime_arn,
        "MR_LISTER_AGENTCORE_RUNTIME_ENDPOINT_ARN": endpoint_arn,
        "MR_LISTER_AGENTCORE_RUNTIME_QUALIFIER": qualifier,
        "MR_LISTER_AGENTCORE_RUNTIME_VERSION": "7",
        "MR_LISTER_ARTIFACT_BUCKET": (
            f"mr-lister-phase6-artifacts-{ENVIRONMENT}-{ACCOUNT}-{REGION}"
        ),
        "MR_LISTER_PRINTIFY_SECRET_ARN": (
            f"arn:aws:secretsmanager:{REGION}:{ACCOUNT}:secret:mr-lister/phase6/dev/printify-AbCd12"
        ),
        "MR_LISTER_PRODUCT_PROFILE_ID": "gildan_64000_swiftpod",
        "MR_LISTER_PRODUCT_PROFILE_VERSION": "2",
        "MR_LISTER_PRODUCT_PROFILE_FINGERPRINT": PROFILE.fingerprint,
        "MR_LISTER_PRODUCT_PROFILE_PATH": PROFILE_PATH.as_posix(),
    }
    environment["MR_LISTER_AGENTCORE_RUNTIME_BINDING_FINGERPRINT"] = (
        agentcore_runtime_binding_fingerprint(
            runtime_arn=runtime_arn,
            endpoint_arn=endpoint_arn,
            qualifier=qualifier,
            runtime_version="7",
            release_fingerprint=RELEASE,
        )
    )
    return environment


class FakeDynamo:
    def get_item(self, **kwargs: Any) -> dict[str, Any]:
        del kwargs
        return {}

    def put_item(self, **kwargs: Any) -> dict[str, Any]:
        del kwargs
        return {}

    def query(self, **kwargs: Any) -> dict[str, Any]:
        del kwargs
        return {"Items": []}

    def transact_write_items(self, **kwargs: Any) -> dict[str, Any]:
        del kwargs
        return {}


class FakeStepFunctions:
    def start_execution(self, **kwargs: Any) -> dict[str, Any]:
        del kwargs
        return {}

    def describe_execution(self, **kwargs: Any) -> dict[str, Any]:
        del kwargs
        return {}


class FakeAgentCore:
    def invoke_agent_runtime(self, **kwargs: Any) -> dict[str, Any]:
        del kwargs
        return {}


class FakeS3:
    def get_object(self, **kwargs: Any) -> dict[str, Any]:
        del kwargs
        return {}


class FakeSecrets:
    def get_secret_value(self, **kwargs: Any) -> dict[str, Any]:
        del kwargs
        return {}


class RecordingFactory:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.clients: dict[str, object] = {
            "dynamodb": FakeDynamo(),
            "stepfunctions": FakeStepFunctions(),
            "bedrock-agentcore": FakeAgentCore(),
            "s3": FakeS3(),
            "secretsmanager": FakeSecrets(),
        }

    def __call__(self, service_name: str, *, region_name: str) -> object:
        self.calls.append((service_name, region_name))
        return self.clients[service_name]


def test_configuration_loaders_bind_one_release_account_region_and_table() -> None:
    environment = _base_environment()

    dispatcher = load_dispatcher_configuration(environment)
    preparation = load_preparation_configuration(environment)
    provider = load_provider_configuration(environment)
    settlement = load_settlement_configuration(environment)

    assert dispatcher.common == preparation.common == provider.common == settlement.common
    assert dispatcher.state_machine_arns[WorkType.PREPARE].endswith("-prepare")
    assert preparation.runtime_qualifier == "phase6_v7_dev"
    assert preparation.runtime_version == "7"
    assert preparation.runtime_endpoint_arn.endswith("/runtime-endpoint/phase6_v7_dev")
    assert provider.artifact_bucket.endswith(f"-{ACCOUNT}-{REGION}")
    assert provider.profile.exact.fingerprint == PROFILE.fingerprint


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("MR_LISTER_AWS_ACCOUNT_ID", "000000000000"),
        ("MR_LISTER_STATE_TABLE", "other-table"),
        ("MR_LISTER_RELEASE_FINGERPRINT", "0" * 64),
        ("MR_LISTER_ENVIRONMENT", "Dev"),
    ],
)
def test_every_role_rejects_common_configuration_drift(name: str, value: str) -> None:
    environment = _base_environment()
    environment[name] = value

    for loader in (
        load_dispatcher_configuration,
        load_preparation_configuration,
        load_provider_configuration,
        load_settlement_configuration,
    ):
        with pytest.raises(Phase6MachineConfigurationError) as captured:
            loader(environment)
        assert str(captured.value) == "Phase 6 machine configuration is invalid"
        assert value not in str(captured.value)


def test_cross_region_resource_configuration_fails_closed() -> None:
    environment = _base_environment()
    environment["AWS_REGION"] = "us-east-1"

    for loader in (
        load_dispatcher_configuration,
        load_preparation_configuration,
        load_provider_configuration,
    ):
        with pytest.raises(Phase6MachineConfigurationError):
            loader(environment)

    # The settlement role intentionally has no regional dependency beyond its regional table.
    assert load_settlement_configuration(environment).common.region == "us-east-1"


def test_dispatcher_rejects_cross_account_or_wrong_machine_name() -> None:
    for changed in (
        f"arn:aws:states:{REGION}:999999999999:stateMachine:mr-lister-phase6-dev-prepare",
        f"arn:aws:states:{REGION}:{ACCOUNT}:stateMachine:mr-lister-phase6-dev-publish",
    ):
        environment = _base_environment()
        environment["MR_LISTER_PREPARE_MACHINE_ARN"] = changed
        with pytest.raises(Phase6MachineConfigurationError):
            load_dispatcher_configuration(environment)


def test_preparation_requires_a_same_account_phase6_runtime() -> None:
    for changed in (
        f"arn:aws:bedrock-agentcore:{REGION}:{ACCOUNT}:runtime/mr_lister_phase3-AbCd1234",
        (f"arn:aws:bedrock-agentcore:{REGION}:999999999999:runtime/mr_lister_phase6-AbCd1234"),
    ):
        environment = _base_environment()
        environment["MR_LISTER_AGENTCORE_RUNTIME_ARN"] = changed
        with pytest.raises(Phase6MachineConfigurationError):
            load_preparation_configuration(environment)


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("MR_LISTER_AGENTCORE_RUNTIME_QUALIFIER", "DEFAULT"),
        ("MR_LISTER_AGENTCORE_RUNTIME_QUALIFIER", "phase6_v8_dev"),
        ("MR_LISTER_AGENTCORE_RUNTIME_QUALIFIER", "phase6_v7_prod"),
        ("MR_LISTER_AGENTCORE_RUNTIME_VERSION", "0"),
        ("MR_LISTER_AGENTCORE_RUNTIME_VERSION", "8"),
    ],
)
def test_preparation_requires_an_environment_and_version_named_endpoint(
    name: str,
    value: str,
) -> None:
    environment = _base_environment()
    environment[name] = value

    with pytest.raises(Phase6MachineConfigurationError):
        load_preparation_configuration(environment)


def test_provider_rejects_bucket_secret_or_profile_drift() -> None:
    changes = {
        "MR_LISTER_ARTIFACT_BUCKET": "attacker-bucket",
        "MR_LISTER_PRINTIFY_SECRET_ARN": (
            "arn:aws:secretsmanager:us-west-2:999999999999:"
            "secret:mr-lister/phase6/dev/printify-AbCd12"
        ),
        "MR_LISTER_PRODUCT_PROFILE_FINGERPRINT": "e" * 64,
        "MR_LISTER_PRODUCT_PROFILE_VERSION": "3",
    }
    for name, value in changes.items():
        environment = _base_environment()
        environment[name] = value
        with pytest.raises(Phase6MachineConfigurationError):
            load_provider_configuration(environment)


def test_role_composition_constructs_only_its_allowed_clients() -> None:
    environment = _base_environment()

    dispatcher_factory = RecordingFactory()
    dispatcher = build_dispatcher_handler(environment, client_factory=dispatcher_factory)
    assert isinstance(dispatcher, Phase6DispatcherHandler)
    assert dispatcher_factory.calls == [("dynamodb", REGION), ("stepfunctions", REGION)]

    preparation_factory = RecordingFactory()
    preparation = build_preparation_handler(environment, client_factory=preparation_factory)
    assert isinstance(preparation, Phase6PreparationHandler)
    assert preparation_factory.calls == [("dynamodb", REGION), ("bedrock-agentcore", REGION)]

    provider_factory = RecordingFactory()
    provider = build_provider_handler(environment, client_factory=provider_factory)
    assert isinstance(provider, Phase6ProviderHandler)
    assert provider_factory.calls == [
        ("dynamodb", REGION),
        ("s3", REGION),
        ("secretsmanager", REGION),
    ]

    settlement_factory = RecordingFactory()
    settlement = build_settlement_handler(environment, client_factory=settlement_factory)
    assert isinstance(settlement, Phase6SettlementHandler)
    assert settlement_factory.calls == [("dynamodb", REGION)]


def test_missing_role_method_fails_before_returning_a_handler() -> None:
    factory = RecordingFactory()
    factory.clients["secretsmanager"] = object()

    with pytest.raises(RuntimeError, match="dependency is unavailable"):
        build_provider_handler(_base_environment(), client_factory=factory)


def test_role_configs_do_not_retain_unneeded_secret_or_runtime_values() -> None:
    environment = _base_environment()
    dispatcher = load_dispatcher_configuration(environment)
    settlement = load_settlement_configuration(environment)

    serialized = repr((dispatcher, settlement))
    assert "printify-AbCd12" not in serialized
    assert "mr_lister_phase6_dev" not in serialized
    assert "PRODUCT_PROFILE_PATH" not in serialized
