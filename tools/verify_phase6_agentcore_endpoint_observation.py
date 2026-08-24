"""Verify the normalized AWS AgentCore endpoint observation used only by deployment tooling."""

from __future__ import annotations

from collections.abc import Mapping

from mr_lister.agent.runtime_binding import AgentCoreRuntimeBinding

_GENERIC_ERROR = "Phase 6 AgentCore endpoint observation is invalid"
_REQUIRED_FIELDS = frozenset(
    {
        "agentRuntimeArn",
        "agentRuntimeEndpointArn",
        "liveVersion",
        "name",
        "status",
    }
)
_OPTIONAL_FIELDS = frozenset({"failureReason", "targetVersion"})


class Phase6AgentCoreEndpointObservationError(RuntimeError):
    """A value-free error for incomplete, drifting, or expanded endpoint evidence."""


def verify_phase6_agentcore_endpoint_observation(
    binding: AgentCoreRuntimeBinding,
    observation: Mapping[str, object],
) -> None:
    """Require exact READY/live identity while preserving optional AWS field absence."""

    try:
        if not isinstance(binding, AgentCoreRuntimeBinding) or not isinstance(observation, Mapping):
            raise ValueError
        observed_fields = set(observation)
        if (
            not _REQUIRED_FIELDS.issubset(observed_fields)
            or not observed_fields.issubset(_REQUIRED_FIELDS | _OPTIONAL_FIELDS)
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
        raise Phase6AgentCoreEndpointObservationError(_GENERIC_ERROR) from None


__all__ = [
    "Phase6AgentCoreEndpointObservationError",
    "verify_phase6_agentcore_endpoint_observation",
]
