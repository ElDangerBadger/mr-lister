from __future__ import annotations

from copy import deepcopy

import pytest

from mr_lister.agent.runtime_binding import (
    agentcore_runtime_binding_fingerprint,
    load_agentcore_runtime_binding,
)
from tools.verify_phase6_agentcore_endpoint_observation import (
    Phase6AgentCoreEndpointObservationError,
    verify_phase6_agentcore_endpoint_observation,
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


def _binding():
    fingerprint = agentcore_runtime_binding_fingerprint(
        runtime_arn=RUNTIME_ARN,
        endpoint_arn=ENDPOINT_ARN,
        qualifier=QUALIFIER,
        runtime_version=VERSION,
        release_fingerprint=RELEASE,
    )
    return load_agentcore_runtime_binding(
        {
            "MR_LISTER_AGENTCORE_RUNTIME_ARN": RUNTIME_ARN,
            "MR_LISTER_AGENTCORE_RUNTIME_BINDING_FINGERPRINT": fingerprint,
            "MR_LISTER_AGENTCORE_RUNTIME_ENDPOINT_ARN": ENDPOINT_ARN,
            "MR_LISTER_AGENTCORE_RUNTIME_QUALIFIER": QUALIFIER,
            "MR_LISTER_AGENTCORE_RUNTIME_VERSION": VERSION,
        },
        region=REGION,
        account_id=ACCOUNT,
        environment_name=ENVIRONMENT,
        release_fingerprint=RELEASE,
    )


def _observation() -> dict[str, object]:
    return {
        "agentRuntimeArn": RUNTIME_ARN,
        "agentRuntimeEndpointArn": ENDPOINT_ARN,
        "liveVersion": VERSION,
        "name": QUALIFIER,
        "status": "READY",
    }


@pytest.mark.parametrize(
    "optional_fields",
    (
        {},
        {"targetVersion": VERSION},
        {"failureReason": None},
        {"failureReason": ""},
        {"failureReason": None, "targetVersion": VERSION},
    ),
)
def test_endpoint_observation_preserves_and_validates_optional_aws_fields(
    optional_fields: dict[str, object],
) -> None:
    verify_phase6_agentcore_endpoint_observation(
        _binding(),
        _observation() | optional_fields,
    )


def test_endpoint_observation_rejects_drift_unknowns_and_missing_required_fields() -> None:
    observation = _observation()
    for field, changed in (
        ("agentRuntimeArn", RUNTIME_ARN.replace(ACCOUNT, "999999999999")),
        ("agentRuntimeEndpointArn", ENDPOINT_ARN + "_other"),
        ("name", "DEFAULT"),
        ("liveVersion", "8"),
        ("targetVersion", "8"),
        ("status", "UPDATING"),
        ("failureReason", "private deployment error"),
        ("unexpected", "value"),
    ):
        drifted = deepcopy(observation)
        drifted[field] = changed
        with pytest.raises(Phase6AgentCoreEndpointObservationError) as captured:
            verify_phase6_agentcore_endpoint_observation(_binding(), drifted)
        assert "private deployment error" not in str(captured.value)

    for missing in observation:
        incomplete = deepcopy(observation)
        del incomplete[missing]
        with pytest.raises(Phase6AgentCoreEndpointObservationError):
            verify_phase6_agentcore_endpoint_observation(_binding(), incomplete)
