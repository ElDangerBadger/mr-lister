from __future__ import annotations

from copy import deepcopy

import pytest

from mr_lister.agent.runtime_binding import (
    AgentCoreRuntimeBindingError,
    agentcore_runtime_binding_fingerprint,
    load_agentcore_runtime_binding,
    verify_agentcore_endpoint_observation,
)

REGION = "us-west-2"
ACCOUNT = "123456789012"
ENVIRONMENT = "dev"
RELEASE = "a" * 64
RUNTIME_ARN = (
    f"arn:aws:bedrock-agentcore:{REGION}:{ACCOUNT}:runtime/mr_lister_phase6_dev-AbCd123456"
)
VERSION = "7"
QUALIFIER = "phase6_v7_dev"
ENDPOINT_ARN = f"{RUNTIME_ARN}/runtime-endpoint/{QUALIFIER}"


def _environment() -> dict[str, object]:
    binding = agentcore_runtime_binding_fingerprint(
        runtime_arn=RUNTIME_ARN,
        endpoint_arn=ENDPOINT_ARN,
        qualifier=QUALIFIER,
        runtime_version=VERSION,
        release_fingerprint=RELEASE,
    )
    return {
        "MR_LISTER_AGENTCORE_RUNTIME_ARN": RUNTIME_ARN,
        "MR_LISTER_AGENTCORE_RUNTIME_ENDPOINT_ARN": ENDPOINT_ARN,
        "MR_LISTER_AGENTCORE_RUNTIME_QUALIFIER": QUALIFIER,
        "MR_LISTER_AGENTCORE_RUNTIME_VERSION": VERSION,
        "MR_LISTER_AGENTCORE_RUNTIME_BINDING_FINGERPRINT": binding,
    }


def _binding():
    return load_agentcore_runtime_binding(
        _environment(),
        region=REGION,
        account_id=ACCOUNT,
        environment_name=ENVIRONMENT,
        release_fingerprint=RELEASE,
    )


def test_binding_pins_release_runtime_custom_endpoint_and_immutable_version() -> None:
    binding = _binding()

    assert binding.runtime_arn == RUNTIME_ARN
    assert binding.endpoint_arn == ENDPOINT_ARN
    assert binding.qualifier == QUALIFIER
    assert binding.runtime_version == VERSION
    assert binding.release_fingerprint == RELEASE
    assert (
        binding.binding_fingerprint
        == _environment()["MR_LISTER_AGENTCORE_RUNTIME_BINDING_FINGERPRINT"]
    )


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("MR_LISTER_AGENTCORE_RUNTIME_QUALIFIER", "DEFAULT"),
        ("MR_LISTER_AGENTCORE_RUNTIME_QUALIFIER", "phase6_v8_dev"),
        ("MR_LISTER_AGENTCORE_RUNTIME_VERSION", "8"),
        (
            "MR_LISTER_AGENTCORE_RUNTIME_ENDPOINT_ARN",
            f"{RUNTIME_ARN}/runtime-endpoint/DEFAULT",
        ),
        ("MR_LISTER_AGENTCORE_RUNTIME_BINDING_FINGERPRINT", "b" * 64),
        (
            "MR_LISTER_AGENTCORE_RUNTIME_ARN",
            "arn:aws:bedrock-agentcore:us-west-2:999999999999:"
            "runtime/mr_lister_phase6_dev-AbCd123456",
        ),
    ],
)
def test_binding_rejects_mutable_or_cross_authority_configuration(
    name: str,
    value: str,
) -> None:
    environment = _environment()
    environment[name] = value

    with pytest.raises(AgentCoreRuntimeBindingError) as captured:
        load_agentcore_runtime_binding(
            environment,
            region=REGION,
            account_id=ACCOUNT,
            environment_name=ENVIRONMENT,
            release_fingerprint=RELEASE,
        )

    assert str(captured.value) == "Phase 6 AgentCore runtime binding is invalid"
    assert value not in str(captured.value)


def test_ready_endpoint_observation_accepts_live_response_with_omitted_optional_fields() -> None:
    binding = _binding()
    observation = {
        "agentRuntimeArn": RUNTIME_ARN,
        "agentRuntimeEndpointArn": ENDPOINT_ARN,
        "name": QUALIFIER,
        "liveVersion": VERSION,
        "status": "READY",
    }

    verify_agentcore_endpoint_observation(binding, observation)


@pytest.mark.parametrize(
    "optional_fields",
    (
        {"targetVersion": VERSION},
        {"failureReason": None},
        {"failureReason": ""},
        {"failureReason": None, "targetVersion": VERSION},
    ),
)
def test_ready_endpoint_observation_validates_optional_fields_when_present(
    optional_fields: dict[str, object],
) -> None:
    observation = {
        "agentRuntimeArn": RUNTIME_ARN,
        "agentRuntimeEndpointArn": ENDPOINT_ARN,
        "name": QUALIFIER,
        "liveVersion": VERSION,
        "status": "READY",
        **optional_fields,
    }

    verify_agentcore_endpoint_observation(_binding(), observation)


def test_ready_endpoint_observation_rejects_drift_unknowns_and_missing_required_fields() -> None:
    binding = _binding()
    observation = {
        "agentRuntimeArn": RUNTIME_ARN,
        "agentRuntimeEndpointArn": ENDPOINT_ARN,
        "name": QUALIFIER,
        "liveVersion": VERSION,
        "status": "READY",
    }

    for field, changed in (
        ("agentRuntimeArn", RUNTIME_ARN.replace(ACCOUNT, "999999999999")),
        ("agentRuntimeEndpointArn", ENDPOINT_ARN + "_other"),
        ("name", "DEFAULT"),
        ("liveVersion", "8"),
        ("targetVersion", "8"),
        ("status", "UPDATING"),
        ("failureReason", "private deployment error"),
    ):
        drifted = deepcopy(observation)
        drifted[field] = changed
        with pytest.raises(AgentCoreRuntimeBindingError) as captured:
            verify_agentcore_endpoint_observation(binding, drifted)
        assert "private deployment error" not in str(captured.value)

    for missing in observation:
        incomplete = deepcopy(observation)
        del incomplete[missing]
        with pytest.raises(AgentCoreRuntimeBindingError):
            verify_agentcore_endpoint_observation(binding, incomplete)

    with pytest.raises(AgentCoreRuntimeBindingError):
        verify_agentcore_endpoint_observation(binding, observation | {"unexpected": "value"})


def test_binding_digest_changes_with_every_authority_field() -> None:
    baseline = _environment()["MR_LISTER_AGENTCORE_RUNTIME_BINDING_FINGERPRINT"]
    assert isinstance(baseline, str)
    changes = (
        {"runtime_version": "8", "qualifier": "phase6_v8_dev"},
        {"release_fingerprint": "b" * 64},
        {
            "runtime_arn": RUNTIME_ARN.replace("AbCd123456", "ZyXw987654"),
            "endpoint_arn": ENDPOINT_ARN.replace("AbCd123456", "ZyXw987654"),
        },
    )
    defaults = {
        "runtime_arn": RUNTIME_ARN,
        "endpoint_arn": ENDPOINT_ARN,
        "qualifier": QUALIFIER,
        "runtime_version": VERSION,
        "release_fingerprint": RELEASE,
    }

    for changed in changes:
        assert agentcore_runtime_binding_fingerprint(**(defaults | changed)) != baseline
