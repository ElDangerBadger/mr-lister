"""Exact-bound, triggerless Phase 7.10 canary runtime tests."""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

import pytest

from mr_lister.publication.canary_runtime import (
    PublicationCanaryMode,
    PublicationCanaryRuntime,
    PublicationCanaryRuntimeError,
    build_publication_canary_binding,
)
from mr_lister.publication.contract import PublicationPermitState, PublicationState
from mr_lister.publication.execution_fingerprints import execution_record_fingerprint
from mr_lister.publication.provider_coordinator import (
    PublicationProviderCoordinator,
    PublicationProviderCoordinatorAction,
    PublicationProviderCoordinatorResult,
)
from tests.test_phase71_publication_store import OWNER_ID
from tests.test_phase72_publication_execution import Harness
from tests.test_phase73_publication_coordinator import (
    ExplodingBoundaryFactory,
    _coordinator,
    _pre_call_guard,
)

ROOT = Path(__file__).resolve().parents[1]
GENERIC_ERROR = "Phase 7 canary authority is invalid"


class RecordingCoordinator:
    def __init__(
        self,
        authority: Any,
        result: PublicationProviderCoordinatorResult,
    ) -> None:
        self.authority = authority
        self.result = result
        self.load_calls: list[tuple[str, str]] = []
        self.advance_calls: list[tuple[str, str]] = []
        self.read_only_calls: list[tuple[str, str]] = []

    def load_execution_authority(self, owner_id: str, aggregate_id: str) -> Any:
        self.load_calls.append((owner_id, aggregate_id))
        return self.authority

    def advance(
        self,
        *,
        owner_id: str,
        aggregate_id: str,
    ) -> PublicationProviderCoordinatorResult:
        self.advance_calls.append((owner_id, aggregate_id))
        return self.result

    def advance_read_only(
        self,
        *,
        owner_id: str,
        aggregate_id: str,
    ) -> PublicationProviderCoordinatorResult:
        self.read_only_calls.append((owner_id, aggregate_id))
        return self.result


class ProofAppearsStore:
    """Expose a pristine read once, then the durable proof-complete authority."""

    def __init__(self, pristine: Any, delegate: Any) -> None:
        self.pristine = pristine
        self.delegate = delegate
        self.load_calls = 0

    def load_execution_authority(self, owner_id: str, aggregate_id: str) -> Any:
        self.load_calls += 1
        if self.load_calls == 1:
            return self.pristine
        return self.delegate.load_execution_authority(owner_id, aggregate_id)

    def __getattr__(self, name: str) -> Any:
        return getattr(self.delegate, name)


class NoProviderBoundaryFactory:
    def __init__(self) -> None:
        self.credential_calls = 0
        self.boundary_calls = 0

    def prepare_credential(self, *, execution_authority: Any) -> Any:
        del execution_authority
        self.credential_calls += 1
        raise AssertionError("read-only canary cannot prepare publication credentials")

    def __call__(self, **_values: object) -> Any:
        self.boundary_calls += 1
        raise AssertionError("read-only canary cannot construct a publication boundary")


def _event(harness: Harness) -> dict[str, str]:
    return {"owner_id": OWNER_ID, "aggregate_id": harness.aggregate_id}


def _error(call: Any) -> None:
    with pytest.raises(PublicationCanaryRuntimeError) as error:
        call()
    assert str(error.value) == GENERIC_ERROR


def test_binding_modes_pin_the_exact_authority_stage_without_raw_identifiers() -> None:
    harness = Harness()
    pristine = harness.authority

    read_only = build_publication_canary_binding(
        pristine,
        mode=PublicationCanaryMode.READ_ONLY_PREFLIGHT,
    )
    _error(
        lambda: build_publication_canary_binding(
            pristine,
            mode=PublicationCanaryMode.PUBLISH_ONCE,
        )
    )

    serialized = read_only.model_dump_json()
    assert read_only.fingerprint == execution_record_fingerprint(
        "publication_canary_binding",
        read_only,
    )
    assert read_only.required_preflight_proof_fingerprint is None
    for raw_identifier in {
        pristine.snapshot.owner_id,
        pristine.snapshot.job_id,
        pristine.aggregate.aggregate_id,
        pristine.permit.permit_id,
        pristine.work.work_request_id,
    }:
        assert raw_identifier not in serialized

    harness.dispatch_and_reconstruct()
    harness.complete_preflight()
    publish_once = build_publication_canary_binding(
        harness.authority,
        mode=PublicationCanaryMode.PUBLISH_ONCE,
    )
    assert publish_once.required_preflight_proof_fingerprint == (
        harness.authority.preflight_proof.fingerprint
    )
    _error(
        lambda: build_publication_canary_binding(
            harness.authority,
            mode=PublicationCanaryMode.READ_ONLY_PREFLIGHT,
        )
    )

    harness.claim_publish()
    _error(
        lambda: build_publication_canary_binding(
            harness.authority,
            mode=PublicationCanaryMode.PUBLISH_ONCE,
        )
    )


@pytest.mark.parametrize(
    "event",
    [
        {},
        {"owner_id": OWNER_ID},
        {"owner_id": OWNER_ID, "aggregate_id": "publication_1", "extra": "forbidden"},
        {
            "owner_id": OWNER_ID,
            "aggregate_id": "publication_1",
            "contract_version": "7.0.1",
        },
        {"owner_id": "f" * 64, "aggregate_id": "publication_1"},
        {"owner_id": OWNER_ID, "aggregate_id": "publication_foreign"},
    ],
)
def test_invocation_is_two_key_and_digest_bound_before_any_authority_read(
    event: dict[str, str],
) -> None:
    harness = Harness()
    binding = build_publication_canary_binding(
        harness.authority,
        mode=PublicationCanaryMode.READ_ONLY_PREFLIGHT,
    )
    coordinator = RecordingCoordinator(
        harness.authority,
        PublicationProviderCoordinatorResult(
            action=PublicationProviderCoordinatorAction.STAGED_SHOP_PREFLIGHT,
            aggregate_state=PublicationState.PUBLICATION_REQUESTED,
        ),
    )
    runtime = PublicationCanaryRuntime(binding=binding, coordinator=coordinator)

    _error(lambda: runtime.invoke(event))

    assert coordinator.load_calls == []
    assert coordinator.advance_calls == []
    assert coordinator.read_only_calls == []


def test_read_only_runtime_uses_only_the_structural_read_only_coordinator_step() -> None:
    harness = Harness()
    binding = build_publication_canary_binding(
        harness.authority,
        mode=PublicationCanaryMode.READ_ONLY_PREFLIGHT,
    )
    result = PublicationProviderCoordinatorResult(
        action=PublicationProviderCoordinatorAction.STAGED_SHOP_PREFLIGHT,
        aggregate_state=PublicationState.PUBLICATION_REQUESTED,
    )
    coordinator = RecordingCoordinator(harness.authority, result)
    runtime = PublicationCanaryRuntime(binding=binding, coordinator=coordinator)

    assert runtime.invoke(_event(harness)) == {
        "action": "staged_shop_preflight",
        "aggregate_state": "publication_requested",
    }
    assert coordinator.load_calls == [
        (OWNER_ID, harness.aggregate_id),
        (OWNER_ID, harness.aggregate_id),
    ]
    assert coordinator.read_only_calls == [(OWNER_ID, harness.aggregate_id)]
    assert coordinator.advance_calls == []


def test_read_only_runtime_stops_at_existing_preflight_proof() -> None:
    harness = Harness()
    binding = build_publication_canary_binding(
        harness.authority,
        mode=PublicationCanaryMode.READ_ONLY_PREFLIGHT,
    )
    harness.dispatch_and_reconstruct()
    harness.complete_preflight()
    coordinator = RecordingCoordinator(
        harness.authority,
        PublicationProviderCoordinatorResult(
            action=PublicationProviderCoordinatorAction.STAGED_PUBLISH_OUTCOME,
            aggregate_state=PublicationState.PUBLICATION_REQUESTED,
        ),
    )

    assert PublicationCanaryRuntime(binding=binding, coordinator=coordinator).invoke(
        _event(harness)
    ) == {
        "action": "read_only_preflight_complete",
        "aggregate_state": "publication_requested",
    }
    assert coordinator.load_calls == [(OWNER_ID, harness.aggregate_id)]
    assert coordinator.read_only_calls == []
    assert coordinator.advance_calls == []
    assert harness.authority.permit.status is PublicationPermitState.AVAILABLE
    assert harness.authority.attempt.publish_post_call_count == 0


def test_proof_appearing_between_reads_cannot_cross_into_publish() -> None:
    harness = Harness()
    pristine = harness.authority
    binding = build_publication_canary_binding(
        pristine,
        mode=PublicationCanaryMode.READ_ONLY_PREFLIGHT,
    )
    harness.dispatch_and_reconstruct()
    harness.complete_preflight()
    switching_store = ProofAppearsStore(pristine, harness.store)
    factory = NoProviderBoundaryFactory()
    coordinator = PublicationProviderCoordinator(
        store=switching_store,
        execution=harness.service,
        boundary_factory=factory,
        pre_call_guard=_pre_call_guard(harness),
        clock=harness.clock,
    )

    assert PublicationCanaryRuntime(binding=binding, coordinator=coordinator).invoke(
        _event(harness)
    ) == {
        "action": "read_only_preflight_complete",
        "aggregate_state": "publication_requested",
    }
    assert switching_store.load_calls == 3
    assert factory.credential_calls == 0
    assert factory.boundary_calls == 0
    assert harness.authority.permit.status is PublicationPermitState.AVAILABLE
    assert harness.authority.attempt.publish_post_call_count == 0


def test_publish_once_recovers_a_consumed_claim_without_a_second_post() -> None:
    harness = Harness()
    harness.dispatch_and_reconstruct()
    harness.complete_preflight()
    binding = build_publication_canary_binding(
        harness.authority,
        mode=PublicationCanaryMode.PUBLISH_ONCE,
    )
    factory = ExplodingBoundaryFactory()
    runtime = PublicationCanaryRuntime(
        binding=binding,
        coordinator=_coordinator(harness, factory),
    )

    _error(lambda: runtime.invoke(_event(harness)))
    assert harness.authority.permit.status is PublicationPermitState.CONSUMED
    assert harness.authority.attempt.publish_post_call_count == 1
    assert factory.calls == 1

    assert runtime.invoke(_event(harness)) == {
        "action": "recovered_consumed_publish_claim",
        "aggregate_state": "publication_reconciling",
    }
    assert harness.authority.attempt.publish_post_call_count == 1
    assert factory.calls == 1


def test_publish_once_returns_safe_pre_post_deadline_settlement() -> None:
    harness = Harness()
    harness.dispatch_and_reconstruct()
    harness.complete_preflight()
    binding = build_publication_canary_binding(
        harness.authority,
        mode=PublicationCanaryMode.PUBLISH_ONCE,
    )
    factory = ExplodingBoundaryFactory()
    runtime = PublicationCanaryRuntime(
        binding=binding,
        coordinator=_coordinator(harness, factory),
    )
    harness.clock.now = harness.authority.snapshot.verification_deadline

    assert runtime.invoke(_event(harness)) == {
        "action": "settled_deadline",
        "aggregate_state": "publication_failed",
    }
    assert harness.authority.permit.status is PublicationPermitState.RETIRED
    assert harness.authority.mutation_claim is None
    assert harness.authority.attempt.publish_post_call_count == 0
    assert runtime.invoke(_event(harness)) == {
        "action": "terminal",
        "aggregate_state": "publication_failed",
    }
    assert factory.calls == 0


def test_canary_runtime_source_has_no_entrypoint_client_or_deployment_capability() -> None:
    path = ROOT / "src" / "mr_lister" / "publication" / "canary_runtime.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    imports = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    } | {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module is not None
    }
    definitions = {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }

    assert not imports.intersection({"boto3", "httpx", "requests", "urllib"})
    assert not definitions.intersection({"handler", "lambda_handler", "entrypoint"})
    assert "get_secret_value" not in path.read_text(encoding="utf-8")
