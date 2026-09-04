"""Release- and evidence-bound configuration for the Phase 7.18 enabled runtime."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict

from mr_lister.cloud.phase7_configuration import (
    Phase7ReadConfiguration,
    load_phase7_read_configuration,
    validate_phase7_read_configuration,
)
from mr_lister.publication.enabled_contract import (
    PHASE718_ENABLED_CONTRACT_VERSION,
    phase718_enabled_publication_contract,
    phase718_enabled_publication_contract_digest,
)

_FINGERPRINT = re.compile(r"^[a-f0-9]{64}$")
_GENERIC_ERROR = "Phase 7.18 enabled configuration is invalid"


class Phase718ConfigurationError(RuntimeError):
    """Value-free refusal for incomplete, disabled, or drifting activation authority."""


class Phase718RuntimeActivation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    activation_mode: Literal["GENERAL_AVAILABILITY"] = "GENERAL_AVAILABILITY"
    scaffold_only: Literal[False] = False
    query_enabled: Literal[True] = True
    request_enabled: Literal[True] = True
    publication_enabled: Literal[True] = True
    worker_enabled: Literal[True] = True
    dispatcher_enabled: Literal[True] = True
    recovery_enabled: Literal[True] = True
    retention_enabled: Literal[True] = True


@dataclass(frozen=True, slots=True)
class Phase718EnabledConfiguration:
    """Validated common authority shared by separate least-privilege runtime graphs."""

    foundation: Phase7ReadConfiguration
    enabled_release_fingerprint: str
    contract_fingerprint: str
    canary_evidence_fingerprint: str
    enablement_evidence_fingerprint: str
    activation: Phase718RuntimeActivation

    @property
    def region(self) -> str:
        return self.foundation.region

    @property
    def environment_name(self) -> str:
        return self.foundation.environment_name

    @property
    def state_table(self) -> str:
        return self.foundation.state_table

    @property
    def application_release_fingerprint(self) -> str:
        return self.foundation.release_manifest_fingerprint


def load_phase718_enabled_configuration(
    environment: Mapping[str, object],
) -> Phase718EnabledConfiguration:
    """Require the exact 7.1.0 tuple before constructing any runtime dependency."""

    try:
        if not isinstance(environment, Mapping):
            raise ValueError
        contract = phase718_enabled_publication_contract()
        contract_fingerprint = _required_fingerprint(
            environment,
            "MR_LISTER_PHASE7_CONTRACT_FINGERPRINT",
        )
        if (
            _required(environment, "MR_LISTER_PHASE7_CONTRACT_VERSION")
            != PHASE718_ENABLED_CONTRACT_VERSION
            or contract_fingerprint != phase718_enabled_publication_contract_digest()
            or _required(environment, "MR_LISTER_PHASE7_ACTIVATION_MODE") != "GENERAL_AVAILABILITY"
            or _required(environment, "MR_LISTER_PHASE7_SCAFFOLD_ONLY") != "false"
            or _required(environment, "MR_LISTER_PHASE7_QUERY_ENABLED") != "true"
            or _required(environment, "MR_LISTER_PHASE7_REQUEST_ENABLED") != "true"
            or _required(environment, "MR_LISTER_PHASE7_PUBLICATION_ENABLED") != "true"
            or _required(environment, "MR_LISTER_PHASE7_WORKER_ENABLED") != "true"
            or _required(environment, "MR_LISTER_PHASE7_DISPATCHER_ENABLED") != "true"
            or _required(environment, "MR_LISTER_PHASE7_RECOVERY_ENABLED") != "true"
            or _required(environment, "MR_LISTER_PHASE7_RETENTION_ENABLED") != "true"
            or contract.publication_enabled is not True
        ):
            raise ValueError

        # Reuse the complete capability-free Phase 7.4 identity/profile validator.  The copied
        # tuple is validation input only; the returned disabled activation is never exposed as
        # the Phase 7.18 activation authority.
        foundation_environment = dict(environment)
        foundation_environment.update(
            {
                "MR_LISTER_PHASE7_SCAFFOLD_ONLY": "true",
                "MR_LISTER_PHASE7_QUERY_ENABLED": "false",
                "MR_LISTER_PHASE7_REQUEST_ENABLED": "false",
                "MR_LISTER_PHASE7_PUBLICATION_ENABLED": "false",
            }
        )
        foundation = load_phase7_read_configuration(foundation_environment)
        configured = Phase718EnabledConfiguration(
            foundation=foundation,
            enabled_release_fingerprint=_required_fingerprint(
                environment,
                "MR_LISTER_PHASE7_ENABLED_RELEASE_FINGERPRINT",
            ),
            contract_fingerprint=contract_fingerprint,
            canary_evidence_fingerprint=_required_fingerprint(
                environment,
                "MR_LISTER_PHASE7_CANARY_EVIDENCE_FINGERPRINT",
            ),
            enablement_evidence_fingerprint=_required_fingerprint(
                environment,
                "MR_LISTER_PHASE7_ENABLEMENT_EVIDENCE_FINGERPRINT",
            ),
            activation=Phase718RuntimeActivation(),
        )
        return validate_phase718_enabled_configuration(configured)
    except Exception:
        raise Phase718ConfigurationError(_GENERIC_ERROR) from None


def validate_phase718_enabled_configuration(
    configuration: object,
) -> Phase718EnabledConfiguration:
    """Deep-reparse the enabled authority before any graph receives a client."""

    try:
        if not isinstance(configuration, Phase718EnabledConfiguration):
            raise ValueError
        foundation = validate_phase7_read_configuration(configuration.foundation)
        for value in (
            configuration.enabled_release_fingerprint,
            configuration.contract_fingerprint,
            configuration.canary_evidence_fingerprint,
            configuration.enablement_evidence_fingerprint,
        ):
            _validate_fingerprint(value)
        if configuration.contract_fingerprint != phase718_enabled_publication_contract_digest():
            raise ValueError
        activation = Phase718RuntimeActivation.model_validate(
            configuration.activation.model_dump(mode="python"),
            strict=True,
        )
        if activation != configuration.activation:
            raise ValueError
        return Phase718EnabledConfiguration(
            foundation=foundation,
            enabled_release_fingerprint=configuration.enabled_release_fingerprint,
            contract_fingerprint=configuration.contract_fingerprint,
            canary_evidence_fingerprint=configuration.canary_evidence_fingerprint,
            enablement_evidence_fingerprint=configuration.enablement_evidence_fingerprint,
            activation=activation,
        )
    except Exception:
        raise Phase718ConfigurationError(_GENERIC_ERROR) from None


def _required(environment: Mapping[str, object], name: str) -> str:
    value = environment.get(name)
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 4_096
        or value != value.strip()
        or "\x00" in value
    ):
        raise ValueError
    return value


def _required_fingerprint(environment: Mapping[str, object], name: str) -> str:
    value = _required(environment, name)
    _validate_fingerprint(value)
    return value


def _validate_fingerprint(value: object) -> None:
    if not isinstance(value, str) or _FINGERPRINT.fullmatch(value) is None or value == "0" * 64:
        raise ValueError


__all__ = [
    "Phase718ConfigurationError",
    "Phase718EnabledConfiguration",
    "Phase718RuntimeActivation",
    "load_phase718_enabled_configuration",
    "validate_phase718_enabled_configuration",
]
