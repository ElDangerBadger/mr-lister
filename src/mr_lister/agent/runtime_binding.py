"""Immutable Phase 6 AgentCore endpoint/version binding.

The application role invokes only the data plane and cannot inspect or update endpoints.
Deployment tooling supplies the result of ``GetAgentRuntimeEndpoint`` to the pure
``verify_agentcore_endpoint_observation`` preflight before enabling the release.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from hashlib import sha256

_REGION = re.compile(r"^[a-z]{2}(?:-[a-z0-9]+){1,2}-[1-9][0-9]?$|^us-gov-[a-z]+-[1-9]$")
_ACCOUNT_ID = re.compile(r"^[0-9]{12}$")
_ENVIRONMENT = re.compile(r"^[a-z][a-z0-9-]{1,15}$")
_FINGERPRINT = re.compile(r"^[a-f0-9]{64}$")
_VERSION = re.compile(r"^[1-9][0-9]{0,4}$")
_RUNTIME_ARN = re.compile(
    r"^arn:(?P<partition>aws|aws-us-gov|aws-cn):bedrock-agentcore:"
    r"(?P<region>[a-z0-9-]+):(?P<account>[0-9]{12}):"
    r"runtime/(?P<runtime_id>[A-Za-z][A-Za-z0-9_]{0,47}-[A-Za-z0-9]{10})$"
)
_ENDPOINT_ARN = re.compile(
    r"^arn:(?P<partition>aws|aws-us-gov|aws-cn):bedrock-agentcore:"
    r"(?P<region>[a-z0-9-]+):(?P<account>[0-9]{12}):"
    r"runtime/(?P<runtime_id>[A-Za-z][A-Za-z0-9_]{0,47}-[A-Za-z0-9]{10})/"
    r"runtime-endpoint/(?P<endpoint_id>[A-Za-z][A-Za-z0-9_]{0,47})$"
)
_GENERIC_ERROR = "Phase 6 AgentCore runtime binding is invalid"


class AgentCoreRuntimeBindingError(RuntimeError):
    """A value-free failure for a mutable, cross-account, or drifting endpoint binding."""


@dataclass(frozen=True, slots=True)
class AgentCoreRuntimeBinding:
    """One release-bound custom endpoint targeting one immutable runtime version."""

    runtime_arn: str
    endpoint_arn: str
    qualifier: str
    runtime_version: str
    release_fingerprint: str
    binding_fingerprint: str


def agentcore_runtime_binding_fingerprint(
    *,
    runtime_arn: str,
    endpoint_arn: str,
    qualifier: str,
    runtime_version: str,
    release_fingerprint: str,
) -> str:
    """Return the canonical digest deployment must pass back as configuration authority."""

    payload = {
        "endpoint_arn": endpoint_arn,
        "format": "phase6-agentcore-runtime-binding-v1",
        "qualifier": qualifier,
        "release_fingerprint": release_fingerprint,
        "runtime_arn": runtime_arn,
        "runtime_version": runtime_version,
    }
    raw = (
        json.dumps(payload, allow_nan=False, separators=(",", ":"), sort_keys=True) + "\n"
    ).encode()
    return sha256(raw).hexdigest()


def load_agentcore_runtime_binding(
    environment: Mapping[str, object],
    *,
    region: str,
    account_id: str,
    environment_name: str,
    release_fingerprint: str,
) -> AgentCoreRuntimeBinding:
    """Load and cross-bind exact release, account, region, endpoint, and version settings."""

    try:
        if (
            not isinstance(environment, Mapping)
            or _REGION.fullmatch(region) is None
            or _ACCOUNT_ID.fullmatch(account_id) is None
            or account_id == "0" * 12
            or _ENVIRONMENT.fullmatch(environment_name) is None
            or _FINGERPRINT.fullmatch(release_fingerprint) is None
            or release_fingerprint == "0" * 64
        ):
            raise ValueError
        runtime_arn = _required(environment, "MR_LISTER_AGENTCORE_RUNTIME_ARN")
        runtime = _RUNTIME_ARN.fullmatch(runtime_arn)
        if (
            runtime is None
            or runtime.group("partition") != _partition(region)
            or runtime.group("region") != region
            or runtime.group("account") != account_id
            or "phase6" not in runtime.group("runtime_id").casefold()
        ):
            raise ValueError
        runtime_version = _required(environment, "MR_LISTER_AGENTCORE_RUNTIME_VERSION")
        if _VERSION.fullmatch(runtime_version) is None:
            raise ValueError
        qualifier = _required(environment, "MR_LISTER_AGENTCORE_RUNTIME_QUALIFIER")
        expected_qualifier = f"phase6_v{runtime_version}_{environment_name.replace('-', '_')}"
        if qualifier != expected_qualifier or qualifier == "DEFAULT":
            raise ValueError
        endpoint_arn = _required(environment, "MR_LISTER_AGENTCORE_RUNTIME_ENDPOINT_ARN")
        endpoint = _ENDPOINT_ARN.fullmatch(endpoint_arn)
        if (
            endpoint is None
            or endpoint.group("partition") != runtime.group("partition")
            or endpoint.group("region") != runtime.group("region")
            or endpoint.group("account") != runtime.group("account")
            or endpoint.group("runtime_id") != runtime.group("runtime_id")
            or endpoint.group("endpoint_id") == "DEFAULT"
        ):
            raise ValueError
        expected_binding = agentcore_runtime_binding_fingerprint(
            runtime_arn=runtime_arn,
            endpoint_arn=endpoint_arn,
            qualifier=qualifier,
            runtime_version=runtime_version,
            release_fingerprint=release_fingerprint,
        )
        binding_fingerprint = _required(
            environment,
            "MR_LISTER_AGENTCORE_RUNTIME_BINDING_FINGERPRINT",
        )
        if binding_fingerprint != expected_binding:
            raise ValueError
        return AgentCoreRuntimeBinding(
            runtime_arn=runtime_arn,
            endpoint_arn=endpoint_arn,
            qualifier=qualifier,
            runtime_version=runtime_version,
            release_fingerprint=release_fingerprint,
            binding_fingerprint=binding_fingerprint,
        )
    except Exception:
        raise AgentCoreRuntimeBindingError(_GENERIC_ERROR) from None


def verify_agentcore_endpoint_observation(
    binding: AgentCoreRuntimeBinding,
    observation: Mapping[str, object],
) -> None:
    """Validate a control-plane preflight response without making the AWS call here."""

    try:
        if not isinstance(binding, AgentCoreRuntimeBinding) or not isinstance(observation, Mapping):
            raise ValueError
        required_fields = {
            "agentRuntimeArn",
            "agentRuntimeEndpointArn",
            "liveVersion",
            "name",
            "status",
        }
        optional_fields = {"failureReason", "targetVersion"}
        observed_fields = set(observation)
        if (
            not required_fields.issubset(observed_fields)
            or not observed_fields.issubset(required_fields | optional_fields)
            or observation.get("agentRuntimeArn") != binding.runtime_arn
            or observation.get("agentRuntimeEndpointArn") != binding.endpoint_arn
            or observation.get("name") != binding.qualifier
            or observation.get("liveVersion") != binding.runtime_version
            or observation.get("status") != "READY"
            or (
                "targetVersion" in observation
                and observation.get("targetVersion") != binding.runtime_version
            )
            or observation.get("failureReason") not in {None, ""}
        ):
            raise ValueError
    except Exception:
        raise AgentCoreRuntimeBindingError(_GENERIC_ERROR) from None


def _partition(region: str) -> str:
    if region.startswith("cn-"):
        return "aws-cn"
    if region.startswith("us-gov-"):
        return "aws-us-gov"
    return "aws"


def _required(environment: Mapping[str, object], name: str) -> str:
    value = environment.get(name)
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 4096
        or value != value.strip()
        or "\x00" in value
    ):
        raise ValueError
    return value


__all__ = [
    "AgentCoreRuntimeBinding",
    "AgentCoreRuntimeBindingError",
    "agentcore_runtime_binding_fingerprint",
    "load_agentcore_runtime_binding",
    "verify_agentcore_endpoint_observation",
]
