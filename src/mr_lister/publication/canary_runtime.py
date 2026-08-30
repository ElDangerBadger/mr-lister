"""Exact-bound, triggerless runtime envelope for one reviewed Phase 7 canary.

This module is not a Lambda entrypoint and performs no client or credential construction. It
accepts only the owner and publication aggregate identities already required by the coordinator,
strongly re-reads the complete authority through the same injected coordinator graph, and refuses
any graph that differs from one immutable canary binding. Read-only preflight mode cannot cross a
durable preflight proof into publication; the one-POST mode requires a separately created binding
to that exact proof.
"""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from typing import Any, Protocol

from pydantic import model_validator

from mr_lister.publication.contract import PublicationPermitState, PublicationState
from mr_lister.publication.execution_fingerprints import (
    execution_record_fingerprint,
    safe_identity_digest,
)
from mr_lister.publication.execution_models import (
    Fingerprint,
    OwnerId,
    PublicationExecutionAuthority,
    PublicationExecutionWorkStatus,
    PublicationPermitRetirementReason,
    PublicationTerminalReason,
    SafeId,
    UtcDateTime,
)
from mr_lister.publication.models import PublicationModel
from mr_lister.publication.provider_coordinator import (
    PublicationProviderCoordinatorAction,
    PublicationProviderCoordinatorResult,
)

_GENERIC_ERROR = "Phase 7 canary authority is invalid"
_TERMINAL_STATES = {
    PublicationState.PUBLISHED,
    PublicationState.PUBLICATION_FAILED,
    PublicationState.PUBLICATION_OUTCOME_UNKNOWN,
}
_READ_ONLY_ACTIONS = {
    PublicationProviderCoordinatorAction.STAGED_SHOP_PREFLIGHT,
    PublicationProviderCoordinatorAction.STAGED_PRODUCT_PREFLIGHT,
    PublicationProviderCoordinatorAction.STAGED_DEFINITIVE_PREFLIGHT_FAILURE,
    PublicationProviderCoordinatorAction.RECORDED_PREFLIGHT,
    PublicationProviderCoordinatorAction.READ_ONLY_PREFLIGHT_COMPLETE,
    PublicationProviderCoordinatorAction.SETTLED_DEFINITIVE_PREFLIGHT_FAILURE,
    PublicationProviderCoordinatorAction.SETTLED_DEADLINE,
    PublicationProviderCoordinatorAction.TERMINAL,
}


class PublicationCanaryRuntimeError(RuntimeError):
    """Value-free refusal for malformed, foreign, stale, or mode-ineligible canary work."""


class PublicationCanaryMode(StrEnum):
    READ_ONLY_PREFLIGHT = "read_only_preflight"
    PUBLISH_ONCE = "publish_once"


class PublicationCanaryInvocation(PublicationModel):
    """The entire private invocation surface; all transition material stays server-owned."""

    owner_id: OwnerId
    aggregate_id: SafeId


class PublicationCanaryBinding(PublicationModel):
    """Sanitized immutable authority for one exact canary aggregate and release."""

    mode: PublicationCanaryMode
    owner_id_digest: Fingerprint
    aggregate_id_digest: Fingerprint
    job_id_digest: Fingerprint
    snapshot_fingerprint: Fingerprint
    permit_id_digest: Fingerprint
    work_request_id_digest: Fingerprint
    work_input_fingerprint: Fingerprint
    release_manifest_fingerprint: Fingerprint
    verification_deadline: UtcDateTime
    required_preflight_proof_fingerprint: Fingerprint | None
    fingerprint: Fingerprint

    @model_validator(mode="after")
    def exact_mode_and_fingerprint(self) -> PublicationCanaryBinding:
        if (
            self.mode is PublicationCanaryMode.READ_ONLY_PREFLIGHT
            and self.required_preflight_proof_fingerprint is not None
        ) or (
            self.mode is PublicationCanaryMode.PUBLISH_ONCE
            and self.required_preflight_proof_fingerprint is None
        ):
            raise ValueError("Canary mode and preflight authority differ")
        if self.fingerprint != execution_record_fingerprint(
            "publication_canary_binding",
            self,
        ):
            raise ValueError("Publication canary binding fingerprint is invalid")
        return self


class PublicationCanaryCoordinator(Protocol):
    def load_execution_authority(
        self,
        owner_id: str,
        aggregate_id: str,
    ) -> PublicationExecutionAuthority: ...

    def advance(
        self,
        *,
        owner_id: str,
        aggregate_id: str,
    ) -> PublicationProviderCoordinatorResult: ...

    def advance_read_only(
        self,
        *,
        owner_id: str,
        aggregate_id: str,
    ) -> PublicationProviderCoordinatorResult: ...


def build_publication_canary_binding(
    authority: PublicationExecutionAuthority,
    *,
    mode: PublicationCanaryMode,
) -> PublicationCanaryBinding:
    """Freeze one sanitized read-only or one-POST canary authority from a strong read."""

    try:
        exact = _exact_authority(authority)
        if (
            exact.aggregate.state is not PublicationState.PUBLICATION_REQUESTED
            or exact.permit.status is not PublicationPermitState.AVAILABLE
            or exact.mutation_claim is not None
            or exact.post_observation is not None
        ):
            raise ValueError
        if mode is PublicationCanaryMode.READ_ONLY_PREFLIGHT:
            if (
                exact.preflight_proof is not None
                or exact.work.status is not PublicationExecutionWorkStatus.PENDING
                or exact.provider_authority is not None
                or exact.call_claims
                or exact.provider_audits
            ):
                raise ValueError
            proof_fingerprint = None
        elif mode is PublicationCanaryMode.PUBLISH_ONCE:
            proof = exact.preflight_proof
            if proof is None or exact.work.status is not PublicationExecutionWorkStatus.DISPATCHED:
                raise ValueError
            proof_fingerprint = proof.fingerprint
        else:
            raise ValueError
        values = _binding_values(exact, mode=mode, proof_fingerprint=proof_fingerprint)
        return PublicationCanaryBinding(
            **values,
            fingerprint=execution_record_fingerprint(
                "publication_canary_binding",
                values,
            ),
        )
    except Exception:
        raise PublicationCanaryRuntimeError(_GENERIC_ERROR) from None


class PublicationCanaryRuntime:
    """Advance one exact-bound canary by at most one coordinator/provider step."""

    __slots__ = ("_binding", "_coordinator")

    def __init__(
        self,
        *,
        binding: PublicationCanaryBinding,
        coordinator: PublicationCanaryCoordinator,
    ) -> None:
        try:
            exact_binding = PublicationCanaryBinding.model_validate(
                binding.model_dump(mode="python")
            )
            if exact_binding != binding:
                raise ValueError
            required_methods = {"load_execution_authority", "advance"}
            if exact_binding.mode is PublicationCanaryMode.READ_ONLY_PREFLIGHT:
                required_methods.add("advance_read_only")
            if any(not callable(getattr(coordinator, name, None)) for name in required_methods):
                raise TypeError
        except Exception:
            raise PublicationCanaryRuntimeError(_GENERIC_ERROR) from None
        self._binding = exact_binding
        self._coordinator = coordinator

    def invoke(self, event: Mapping[str, Any]) -> dict[str, str]:
        """Validate one two-key envelope, re-read authority, and perform at most one step."""

        try:
            if not isinstance(event, Mapping) or set(event) != {"owner_id", "aggregate_id"}:
                raise ValueError
            invocation = PublicationCanaryInvocation.model_validate(event)
            self._require_invocation_binding(invocation)
            authority = self._load_exact(invocation)
            self._require_authority_binding(authority)

            if self._binding.mode is PublicationCanaryMode.READ_ONLY_PREFLIGHT:
                if authority.aggregate.state in _TERMINAL_STATES:
                    return _result_payload(
                        PublicationProviderCoordinatorAction.TERMINAL.value,
                        authority.aggregate.state,
                    )
                if authority.preflight_proof is not None:
                    return _result_payload(
                        "read_only_preflight_complete",
                        authority.aggregate.state,
                    )
            elif authority.aggregate.state in _TERMINAL_STATES:
                return _result_payload(
                    PublicationProviderCoordinatorAction.TERMINAL.value,
                    authority.aggregate.state,
                )

            advance = (
                self._coordinator.advance_read_only
                if self._binding.mode is PublicationCanaryMode.READ_ONLY_PREFLIGHT
                else self._coordinator.advance
            )
            result = advance(owner_id=invocation.owner_id, aggregate_id=invocation.aggregate_id)
            if not isinstance(result, PublicationProviderCoordinatorResult):
                raise TypeError
            if (
                self._binding.mode is PublicationCanaryMode.READ_ONLY_PREFLIGHT
                and result.action not in _READ_ONLY_ACTIONS
            ):
                raise ValueError
            current = self._load_exact(invocation)
            self._require_authority_binding(current)
            if current.aggregate.state is not result.aggregate_state:
                raise ValueError
            return _result_payload(result.action.value, result.aggregate_state)
        except PublicationCanaryRuntimeError:
            raise
        except Exception:
            raise PublicationCanaryRuntimeError(_GENERIC_ERROR) from None

    def _load_exact(
        self,
        invocation: PublicationCanaryInvocation,
    ) -> PublicationExecutionAuthority:
        return _exact_authority(
            self._coordinator.load_execution_authority(
                invocation.owner_id,
                invocation.aggregate_id,
            )
        )

    def _require_invocation_binding(self, invocation: PublicationCanaryInvocation) -> None:
        if (
            safe_identity_digest("owner_id", invocation.owner_id) != self._binding.owner_id_digest
            or safe_identity_digest("publication_aggregate_id", invocation.aggregate_id)
            != self._binding.aggregate_id_digest
        ):
            raise PublicationCanaryRuntimeError(_GENERIC_ERROR)

    def _require_authority_binding(self, authority: PublicationExecutionAuthority) -> None:
        snapshot = authority.snapshot
        if (
            safe_identity_digest("owner_id", snapshot.owner_id) != self._binding.owner_id_digest
            or safe_identity_digest("publication_aggregate_id", authority.aggregate.aggregate_id)
            != self._binding.aggregate_id_digest
            or safe_identity_digest("job_id", snapshot.job_id) != self._binding.job_id_digest
            or snapshot.fingerprint != self._binding.snapshot_fingerprint
            or safe_identity_digest("publication_permit_id", authority.permit.permit_id)
            != self._binding.permit_id_digest
            or safe_identity_digest("publication_work_request_id", authority.work.work_request_id)
            != self._binding.work_request_id_digest
            or authority.work.input_fingerprint != self._binding.work_input_fingerprint
            or snapshot.release_manifest_fingerprint != self._binding.release_manifest_fingerprint
            or snapshot.verification_deadline != self._binding.verification_deadline
        ):
            raise PublicationCanaryRuntimeError(_GENERIC_ERROR)

        if self._binding.mode is PublicationCanaryMode.READ_ONLY_PREFLIGHT:
            if authority.aggregate.state is PublicationState.PUBLICATION_REQUESTED:
                if (
                    authority.permit.status is not PublicationPermitState.AVAILABLE
                    or authority.mutation_claim is not None
                    or authority.post_observation is not None
                ):
                    raise PublicationCanaryRuntimeError(_GENERIC_ERROR)
            elif not (
                authority.aggregate.state is PublicationState.PUBLICATION_FAILED
                and authority.permit.status is PublicationPermitState.RETIRED
                and authority.mutation_claim is None
                and authority.post_observation is None
            ):
                raise PublicationCanaryRuntimeError(_GENERIC_ERROR)
        else:
            proof = authority.preflight_proof
            pre_post_deadline_failure = (
                authority.aggregate.state is PublicationState.PUBLICATION_FAILED
                and authority.permit.status is PublicationPermitState.RETIRED
                and authority.permit.retirement_reason
                is PublicationPermitRetirementReason.PRE_CALL_DEADLINE_EXPIRED
                and authority.mutation_claim is None
                and authority.post_observation is None
                and authority.report is not None
                and authority.report.terminal_reason
                is PublicationTerminalReason.PRE_CALL_DEADLINE_EXPIRED
            )
            if (
                proof is None
                or proof.fingerprint != self._binding.required_preflight_proof_fingerprint
                or (
                    authority.permit.status
                    not in {PublicationPermitState.AVAILABLE, PublicationPermitState.CONSUMED}
                    and not pre_post_deadline_failure
                )
                or authority.aggregate.state
                not in {
                    PublicationState.PUBLICATION_REQUESTED,
                    PublicationState.PUBLICATION_VERIFYING,
                    PublicationState.PUBLICATION_RECONCILING,
                    *_TERMINAL_STATES,
                }
            ):
                raise PublicationCanaryRuntimeError(_GENERIC_ERROR)


def _binding_values(
    authority: PublicationExecutionAuthority,
    *,
    mode: PublicationCanaryMode,
    proof_fingerprint: str | None,
) -> dict[str, object]:
    snapshot = authority.snapshot
    return {
        "mode": mode,
        "owner_id_digest": safe_identity_digest("owner_id", snapshot.owner_id),
        "aggregate_id_digest": safe_identity_digest(
            "publication_aggregate_id",
            authority.aggregate.aggregate_id,
        ),
        "job_id_digest": safe_identity_digest("job_id", snapshot.job_id),
        "snapshot_fingerprint": snapshot.fingerprint,
        "permit_id_digest": safe_identity_digest(
            "publication_permit_id",
            authority.permit.permit_id,
        ),
        "work_request_id_digest": safe_identity_digest(
            "publication_work_request_id",
            authority.work.work_request_id,
        ),
        "work_input_fingerprint": authority.work.input_fingerprint,
        "release_manifest_fingerprint": snapshot.release_manifest_fingerprint,
        "verification_deadline": snapshot.verification_deadline,
        "required_preflight_proof_fingerprint": proof_fingerprint,
    }


def _exact_authority(authority: object) -> PublicationExecutionAuthority:
    if not isinstance(authority, PublicationExecutionAuthority):
        raise TypeError
    exact = PublicationExecutionAuthority.model_validate(authority.model_dump(mode="python"))
    if exact != authority:
        raise ValueError
    return exact


def _result_payload(action: str, state: PublicationState) -> dict[str, str]:
    if state is PublicationState.APPROVED:
        raise ValueError
    return {"action": action, "aggregate_state": state.value}


__all__ = [
    "PublicationCanaryBinding",
    "PublicationCanaryInvocation",
    "PublicationCanaryMode",
    "PublicationCanaryRuntime",
    "PublicationCanaryRuntimeError",
    "build_publication_canary_binding",
]
