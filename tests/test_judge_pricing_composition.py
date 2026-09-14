"""Prove policy loading and real constructor wiring without cloud or provider I/O."""

import json
from unittest.mock import Mock

import pytest

from mr_lister.agent import phase6_composition as agent
from mr_lister.cloud import phase6_composition as api
from mr_lister.cloud import phase718_composition as publication
from mr_lister.cloud.phase718_configuration import (
    Phase718ConfigurationError,
    load_phase718_enabled_configuration,
)
from mr_lister.control.judge_pricing import ENVIRONMENT_KEY, JudgePricingPolicy
from tests import test_phase66_agentcore_composition as agent_fixture
from tests import test_phase66_api_composition as api_fixture
from tests import test_phase718_enabled_backend as publication_fixture

OWNER = "a" * 64
POLICY = {"owner_id": OWNER, "default_price_cents": 3500, "minimum_price_cents": 3500}
EXPECTED = JudgePricingPolicy(**POLICY)

LOADERS = [
    (
        api.load_upload_api_configuration,
        api_fixture.exact_environment,
        api.Phase6ApiConfigurationError,
    ),
    (
        api.load_query_api_configuration,
        api_fixture.exact_environment,
        api.Phase6ApiConfigurationError,
    ),
    (
        api.load_command_api_configuration,
        api_fixture.exact_environment,
        api.Phase6ApiConfigurationError,
    ),
    (
        agent.load_phase6_agentcore_configuration,
        agent_fixture._environment,
        agent.Phase6AgentCoreConfigurationError,
    ),
    (
        load_phase718_enabled_configuration,
        publication_fixture.exact_environment,
        Phase718ConfigurationError,
    ),
]


def with_policy(environment):
    return {**environment(), ENVIRONMENT_KEY: json.dumps(POLICY)}


def configured_policy(configuration):
    common = getattr(configuration, "common", configuration)
    return common.judge_pricing_policy


@pytest.mark.parametrize(("loader", "environment", "error"), LOADERS)
def test_each_runtime_preserves_the_exact_configured_judge_policy(loader, environment, error):
    del error
    assert configured_policy(loader(with_policy(environment))) == EXPECTED
    assert configured_policy(loader(environment())) is None


@pytest.mark.parametrize(("loader", "environment", "error"), LOADERS)
@pytest.mark.parametrize(
    "bad",
    [
        "",
        "null",
        '{"owner_id":"private-malformed-value"}',
        json.dumps({**POLICY, "default_price_cents": True}),
    ],
)
def test_malformed_present_judge_configuration_fails_startup_instead_of_disabling_policy(
    loader, environment, error, bad
):
    values = {**environment(), ENVIRONMENT_KEY: bad}
    with pytest.raises(error) as failure:
        loader(values)
    assert "configuration is invalid" in str(failure.value)
    assert "private-malformed-value" not in str(failure.value)
    assert OWNER not in str(failure.value)


def test_query_and_command_composition_deliver_policy_to_actual_services_without_io():
    query_factory = api_fixture.RecordingClientFactory()
    command_factory = api_fixture.RecordingClientFactory()
    query = api.compose_query_api_adapter(
        api.load_query_api_configuration(with_policy(api_fixture.exact_environment)),
        client_factory=query_factory,
    )
    commands = api.compose_command_api_adapter(
        api.load_command_api_configuration(with_policy(api_fixture.exact_environment)),
        client_factory=command_factory,
    )
    assert query._reviews._judge_pricing_policy == EXPECTED
    assert commands._commands._judge_pricing_policy == EXPECTED
    assert query_factory.calls == [("dynamodb", api_fixture.REGION), ("s3", api_fixture.REGION)]
    assert command_factory.calls == [("dynamodb", api_fixture.REGION)]
    assert not query_factory.dynamodb.operations and not query_factory.s3.operations
    assert not command_factory.dynamodb.operations


def test_enabled_publication_composition_keeps_policy_after_deep_configuration_validation():
    factory = publication_fixture._Factory()
    adapter = publication.compose_phase718_request_handler(
        load_phase718_enabled_configuration(with_policy(publication_fixture.exact_environment)),
        client_factory=factory,
    )
    assert adapter._delegate._requests._judge_pricing_policy == EXPECTED
    assert factory.calls == [("dynamodb", "us-west-2")]
    assert not factory.client.calls


def test_agent_composition_passes_policy_into_the_preparation_worker(monkeypatch):
    runtime = Mock(return_value=object())
    monkeypatch.setattr(agent, "create_phase6_agentcore_runtime", runtime)
    s3 = agent_fixture.FakeS3()
    dynamodb = api_fixture.RecordingDynamoClient()
    configured = agent.load_phase6_agentcore_configuration(with_policy(agent_fixture._environment))
    result = agent.compose_phase6_agentcore_runtime(
        configured,
        dynamodb_client=dynamodb,
        s3_client=s3,
        intelligence=agent_fixture.FakeIntelligence(),
        controller_model=agent.PHASE6_STRANDS_CONTROLLER_MODEL_ID,
    )
    assert result is runtime.return_value
    runtime.assert_called_once()
    worker = runtime.call_args.kwargs["backend"]._service._worker
    assert worker._judge_pricing_policy == EXPECTED
    assert not s3.calls
    assert not dynamodb.operations
