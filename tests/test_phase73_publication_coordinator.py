"""Phase 7.3 coordinator recovery, authority-derivation, and one-shot POST gates."""

from __future__ import annotations

import inspect
from typing import Any

import pytest
from pydantic import SecretStr

from mr_lister.publication.application import DurablePublicationPreCallGuard
from mr_lister.publication.contract import PublicationState
from mr_lister.publication.errors import PublicationConflictError
from mr_lister.publication.evidence_provenance import (
    PublicationDefinitivePreflightEvidence,
)
from mr_lister.publication.execution_commands import RecordPublicationPostOutcomeCommand
from mr_lister.publication.execution_fingerprints import execution_record_fingerprint
from mr_lister.publication.execution_models import (
    PublicationCallPurpose,
    PublicationPreflightFailureReason,
)
from mr_lister.publication.provider_boundary import (
    StagedPrintifyPublicationBoundary,
)
from mr_lister.publication.provider_coordinator import (
    PublicationProviderCoordinator,
    PublicationProviderCoordinatorAction,
    PublicationProviderCoordinatorError,
)
from mr_lister.publication.provider_credentials import (
    issue_bound_publication_provider_credential,
)
from tests.test_phase71_publication_service import ProfileAuthority, _authority
from tests.test_phase71_publication_store import OWNER_ID
from tests.test_phase72_publication_execution import Harness
from tests.test_phase72_publication_provider_boundary import TOKEN, _json_response
from tests.test_phase73_publication_provider_boundary import (
    DurableAuditSink,
    OrderedTransport,
    _reader,
)


class ShopResponseBoundaryFactory:
    def __init__(self, harness: Harness, response: Any) -> None:
        self.harness = harness
        self.response = response
        self.calls = 0
        self.events: list[str] = []
        self.transport = OrderedTransport([response], self.events)

    @staticmethod
    def prepare_credential(*, execution_authority):  # type: ignore[no-untyped-def]
        authority = execution_authority.provider_authority
        assert authority is not None
        return issue_bound_publication_provider_credential(
            authority=authority,
            bearer_token=SecretStr(TOKEN),
        )

    def __call__(self, *, execution_authority, credential):  # type: ignore[no-untyped-def]
        self.calls += 1
        return StagedPrintifyPublicationBoundary(
            execution_authority=execution_authority,
            credential=credential,
            transport=self.transport,
            audit_sink=DurableAuditSink(self.harness, self.events),
            evidence_store=self.harness.store,
            authority_reader=_reader(self.harness),
            clock=self.harness.clock,
        )


class ExplodingBoundaryFactory:
    def __init__(self) -> None:
        self.calls = 0

    @staticmethod
    def prepare_credential(*, execution_authority):  # type: ignore[no-untyped-def]
        authority = execution_authority.provider_authority
        assert authority is not None
        return issue_bound_publication_provider_credential(
            authority=authority,
            bearer_token=SecretStr(TOKEN),
        )

    def __call__(self, *, execution_authority, credential):  # type: ignore[no-untyped-def]
        del execution_authority, credential
        self.calls += 1
        raise RuntimeError("provider worker stopped before a boundary result")


def _pre_call_guard(harness: Harness) -> DurablePublicationPreCallGuard:
    _, exact = _authority()
    return DurablePublicationPreCallGuard(
        store=harness.store,
        profiles=ProfileAuthority(exact),
        eligibility=harness.profile_eligibility,  # type: ignore[arg-type]
        release_manifest_fingerprint="b" * 64,
    )


def _coordinator(
    harness: Harness,
    factory: Any,
    *,
    pre_call_guard: Any | None = None,
) -> PublicationProviderCoordinator:
    return PublicationProviderCoordinator(
        store=harness.store,
        execution=harness.service,
        boundary_factory=factory,
        pre_call_guard=pre_call_guard or _pre_call_guard(harness),
        clock=harness.clock,
    )


def _stage_preflight(
    harness: Harness,
    purpose: PublicationCallPurpose,
    *,
    negative: bool,
):  # type: ignore[no-untyped-def]
    if purpose is PublicationCallPurpose.SHOP_PREFLIGHT:
        _, claim = harness.claim_shop()
    else:
        _, claim = harness.claim_product(PublicationCallPurpose.PRODUCT_PREFLIGHT)
    if not negative:
        evidence = (
            harness.shop_evidence(claim)
            if purpose is PublicationCallPurpose.SHOP_PREFLIGHT
            else harness.product_evidence(claim)
        )
        return harness.stage_evidence(evidence)
    provider = harness.authority.provider_authority
    assert provider is not None
    values = {
        "call_claim_id": claim.authorization_id,
        "call_claim_fingerprint": claim.fingerprint,
        "provider_authority_id": provider.provider_authority_id,
        "provider_authority_fingerprint": provider.fingerprint,
        "failure_reason": (
            PublicationPreflightFailureReason.SHOP_NOT_CONNECTED_TO_ETSY
            if purpose is PublicationCallPurpose.SHOP_PREFLIGHT
            else PublicationPreflightFailureReason.EXACT_PRODUCT_NOT_FOUND
        ),
        "sanitized_response_fingerprint": "7" * 64,
        "observed_at": harness.clock.now,
    }
    evidence = PublicationDefinitivePreflightEvidence(
        **values,
        fingerprint=execution_record_fingerprint(
            "definitive_preflight_evidence",
            values,
        ),
    )
    return harness.stage_evidence(evidence)


def test_coordinator_accepts_only_owner_and_aggregate_not_commands_or_provider_material() -> None:
    parameters = inspect.signature(PublicationProviderCoordinator.advance).parameters

    assert tuple(parameters) == ("self", "owner_id", "aggregate_id")
    assert {
        "credential",
        "token",
        "route",
        "body",
        "purpose",
        "operation_id",
        "expected_record_version",
        "stage_id",
    }.isdisjoint(parameters)


class RecordingCurrentGuard:
    def __init__(self, harness: Harness, events: list[str]) -> None:
        self.harness = harness
        self.events = events

    def require_current(self, *, owner_id: str, aggregate_id: str):  # type: ignore[no-untyped-def]
        self.events.append("guard")
        return self.harness.store.load_execution_authority(owner_id, aggregate_id)


class RejectingGuard:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def require_current(self, *, owner_id: str, aggregate_id: str):  # type: ignore[no-untyped-def]
        self.calls.append((owner_id, aggregate_id))
        raise RuntimeError("private stale approval material")


class FixedGuard:
    def __init__(self, authority: object) -> None:
        self.authority = authority

    def require_current(self, *, owner_id: str, aggregate_id: str):  # type: ignore[no-untyped-def]
        del owner_id, aggregate_id
        return self.authority


class CountingBoundaryFactory:
    def __init__(self) -> None:
        self.prepare_calls = 0
        self.boundary_calls = 0

    def prepare_credential(self, *, execution_authority):  # type: ignore[no-untyped-def]
        del execution_authority
        self.prepare_calls += 1
        raise AssertionError("credential preparation must follow the pre-call guard")

    def __call__(self, **_values: object) -> object:
        self.boundary_calls += 1
        raise AssertionError("provider boundary must follow the pre-call guard")


def test_pre_call_guard_runs_before_every_transition_credential_claim_audit_and_wire(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = Harness()
    factory = ShopResponseBoundaryFactory(
        harness,
        _json_response(200, [{"id": 987654, "sales_channel": "disconnected"}]),
    )
    events = factory.events
    guard = RecordingCurrentGuard(harness, events)

    def record(name: str, operation):  # type: ignore[no-untyped-def]
        def wrapped(*args, **kwargs):  # type: ignore[no-untyped-def]
            events.append(name)
            return operation(*args, **kwargs)

        return wrapped

    monkeypatch.setattr(
        harness.service,
        "dispatch_work",
        record("dispatch", harness.service.dispatch_work),
    )
    monkeypatch.setattr(
        harness.service,
        "reconstruct_authority",
        record("reconstruct", harness.service.reconstruct_authority),
    )
    monkeypatch.setattr(
        harness.service,
        "claim_shop_get",
        record("claim", harness.service.claim_shop_get),
    )
    prepare = factory.prepare_credential

    def prepare_credential(*, execution_authority):  # type: ignore[no-untyped-def]
        events.append("credential")
        return prepare(execution_authority=execution_authority)

    monkeypatch.setattr(factory, "prepare_credential", prepare_credential)

    _coordinator(harness, factory, pre_call_guard=guard).advance(
        owner_id=OWNER_ID,
        aggregate_id=harness.aggregate_id,
    )

    assert events == [
        "guard",
        "dispatch",
        "reconstruct",
        "credential",
        "claim",
        "audit",
        "wire",
    ]


def test_pre_call_guard_error_fails_closed_before_any_durable_or_provider_work() -> None:
    harness = Harness()
    before = harness.authority
    guard = RejectingGuard()
    factory = CountingBoundaryFactory()

    with pytest.raises(PublicationProviderCoordinatorError) as captured:
        _coordinator(harness, factory, pre_call_guard=guard).advance(
            owner_id=OWNER_ID,
            aggregate_id=harness.aggregate_id,
        )

    assert str(captured.value) == "Publication pre-call authority is unavailable"
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None
    assert "private" not in str(captured.value)
    assert guard.calls == [(OWNER_ID, harness.aggregate_id)]
    assert harness.authority == before
    assert harness.authority.call_claims == ()
    assert harness.authority.provider_audits == ()
    assert harness.authority.provider_authority is None
    assert factory.prepare_calls == 0
    assert factory.boundary_calls == 0


def test_pre_call_guard_cannot_return_a_stale_but_individually_valid_authority() -> None:
    harness = Harness()
    harness.dispatch_and_reconstruct()
    stale = harness.authority
    harness.claim_shop()
    before = harness.authority
    factory = CountingBoundaryFactory()

    with pytest.raises(PublicationProviderCoordinatorError) as captured:
        _coordinator(harness, factory, pre_call_guard=FixedGuard(stale)).advance(
            owner_id=OWNER_ID,
            aggregate_id=harness.aggregate_id,
        )

    assert str(captured.value) == "Publication pre-call authority is unavailable"
    assert harness.authority == before
    assert factory.prepare_calls == 0
    assert factory.boundary_calls == 0


@pytest.mark.parametrize("returned", [None, object()])
def test_pre_call_guard_return_is_deep_validated(returned: object) -> None:
    harness = Harness()
    before = harness.authority
    factory = CountingBoundaryFactory()

    with pytest.raises(PublicationProviderCoordinatorError):
        _coordinator(harness, factory, pre_call_guard=FixedGuard(returned)).advance(
            owner_id=OWNER_ID,
            aggregate_id=harness.aggregate_id,
        )

    assert harness.authority == before
    assert factory.prepare_calls == 0
    assert factory.boundary_calls == 0


def test_coordinator_derives_setup_claim_and_negative_stage_then_recovers_without_rewire() -> None:
    harness = Harness()
    factory = ShopResponseBoundaryFactory(
        harness,
        _json_response(200, [{"id": 987654, "sales_channel": "disconnected"}]),
    )
    coordinator = _coordinator(harness, factory)

    staged = coordinator.advance(owner_id=OWNER_ID, aggregate_id=harness.aggregate_id)

    assert staged.action is (
        PublicationProviderCoordinatorAction.STAGED_DEFINITIVE_PREFLIGHT_FAILURE
    )
    assert staged.stage_id is not None
    assert staged.aggregate_state is PublicationState.PUBLICATION_REQUESTED
    assert factory.calls == 1
    assert factory.events == ["audit", "wire"]
    assert len(factory.transport.calls) == 1

    settled = coordinator.advance(owner_id=OWNER_ID, aggregate_id=harness.aggregate_id)

    assert settled.action is (
        PublicationProviderCoordinatorAction.SETTLED_DEFINITIVE_PREFLIGHT_FAILURE
    )
    assert settled.aggregate_state is PublicationState.PUBLICATION_FAILED
    assert factory.calls == 1
    assert len(factory.transport.calls) == 1


@pytest.mark.parametrize(
    ("purpose", "latest_is_negative"),
    [
        (PublicationCallPurpose.SHOP_PREFLIGHT, False),
        (PublicationCallPurpose.SHOP_PREFLIGHT, True),
        (PublicationCallPurpose.PRODUCT_PREFLIGHT, False),
        (PublicationCallPurpose.PRODUCT_PREFLIGHT, True),
    ],
)
def test_latest_durable_stage_for_each_preflight_purpose_controls_settlement(
    purpose: PublicationCallPurpose,
    latest_is_negative: bool,
) -> None:
    harness = Harness()
    harness.dispatch_and_reconstruct()
    counterpart = None
    if purpose is PublicationCallPurpose.PRODUCT_PREFLIGHT:
        counterpart = _stage_preflight(
            harness,
            PublicationCallPurpose.SHOP_PREFLIGHT,
            negative=False,
        )
    first = _stage_preflight(harness, purpose, negative=not latest_is_negative)
    harness.clock.tick()
    latest = _stage_preflight(harness, purpose, negative=latest_is_negative)
    if purpose is PublicationCallPurpose.SHOP_PREFLIGHT:
        harness.clock.tick()
        counterpart = _stage_preflight(
            harness,
            PublicationCallPurpose.PRODUCT_PREFLIGHT,
            negative=False,
        )
    assert counterpart is not None

    result = _coordinator(harness, ExplodingBoundaryFactory()).advance(
        owner_id=OWNER_ID,
        aggregate_id=harness.aggregate_id,
    )

    if latest_is_negative:
        assert result.action is (
            PublicationProviderCoordinatorAction.SETTLED_DEFINITIVE_PREFLIGHT_FAILURE
        )
        assert harness.authority.aggregate.state is PublicationState.PUBLICATION_FAILED
        assert result.stage_id == latest.stage_id
    else:
        assert result.action is PublicationProviderCoordinatorAction.RECORDED_PREFLIGHT
        assert harness.authority.aggregate.state is PublicationState.PUBLICATION_REQUESTED
        proof = harness.authority.preflight_proof
        assert proof is not None
        latest_claim_field = (
            proof.shop_call_claim_id
            if purpose is PublicationCallPurpose.SHOP_PREFLIGHT
            else proof.product_call_claim_id
        )
        assert latest_claim_field == latest.call_claim_id
        assert first.stage_id in {
            stage.stage_id
            for stage in harness.store.list_unconsumed_provider_evidence(
                OWNER_ID,
                harness.aggregate_id,
            )
        }
    assert harness.authority.attempt.publish_post_call_count == 0


def test_staged_post_outcome_is_consumed_before_boundary_factory_or_another_post() -> None:
    harness = Harness()
    harness.dispatch_and_reconstruct()
    harness.complete_preflight()
    _result, post_claim = harness.claim_publish()
    evidence = harness.publish_evidence(post_claim, accepted=True)
    stage = harness.stage_evidence(evidence)
    factory = ExplodingBoundaryFactory()
    coordinator = _coordinator(harness, factory)

    result = coordinator.advance(owner_id=OWNER_ID, aggregate_id=harness.aggregate_id)

    assert result.action is PublicationProviderCoordinatorAction.RECORDED_PUBLISH_OUTCOME
    assert result.stage_id == stage.stage_id
    assert result.aggregate_state is PublicationState.PUBLICATION_VERIFYING
    assert factory.calls == 0
    assert harness.authority.attempt.publish_post_call_count == 1
    assert (
        harness.store.list_unconsumed_provider_evidence(
            OWNER_ID,
            harness.aggregate_id,
        )
        == ()
    )


def test_crash_after_publish_claim_never_reissues_post_and_enters_reconciliation() -> None:
    harness = Harness()
    harness.dispatch_and_reconstruct()
    harness.complete_preflight()
    factory = ExplodingBoundaryFactory()
    coordinator = _coordinator(harness, factory)

    with pytest.raises(RuntimeError, match="stopped"):
        coordinator.advance(owner_id=OWNER_ID, aggregate_id=harness.aggregate_id)

    claimed = harness.authority
    assert claimed.attempt.publish_post_call_count == 1
    assert claimed.mutation_claim is not None
    assert claimed.post_observation is None
    assert factory.calls == 1

    recovered = coordinator.advance(owner_id=OWNER_ID, aggregate_id=harness.aggregate_id)

    assert recovered.action is (
        PublicationProviderCoordinatorAction.RECOVERED_CONSUMED_PUBLISH_CLAIM
    )
    assert recovered.aggregate_state is PublicationState.PUBLICATION_RECONCILING
    assert factory.calls == 1
    assert harness.authority.attempt.publish_post_call_count == 1


def test_existing_consumed_claim_without_stage_recovers_without_constructing_boundary() -> None:
    harness = Harness()
    harness.dispatch_and_reconstruct()
    harness.complete_preflight()
    harness.claim_publish(audit=False)
    factory = ExplodingBoundaryFactory()
    coordinator = _coordinator(harness, factory)

    result = coordinator.advance(owner_id=OWNER_ID, aggregate_id=harness.aggregate_id)

    assert result.action is (PublicationProviderCoordinatorAction.RECOVERED_CONSUMED_PUBLISH_CLAIM)
    assert result.aggregate_state is PublicationState.PUBLICATION_RECONCILING
    assert factory.calls == 0
    assert harness.authority.attempt.publish_post_call_count == 1


def test_staged_positive_product_proof_is_consumed_before_deadline_settlement() -> None:
    harness = Harness()
    harness.dispatch_and_reconstruct()
    harness.complete_preflight()
    _publish_result, post_claim = harness.claim_publish()
    accepted = harness.publish_evidence(post_claim, accepted=True)
    harness.service.record_post_outcome(
        harness.command(
            RecordPublicationPostOutcomeCommand,
            harness.next_operation("accepted"),
            evidence=accepted,
        )
    )
    harness.clock.tick()
    _claim_result, product_claim = harness.claim_product(PublicationCallPurpose.VERIFICATION)
    positive = harness.product_evidence(product_claim, positive=True)
    stage = harness.stage_evidence(positive)
    harness.clock.now = harness.authority.snapshot.verification_deadline
    factory = ExplodingBoundaryFactory()
    coordinator = _coordinator(harness, factory)

    result = coordinator.advance(owner_id=OWNER_ID, aggregate_id=harness.aggregate_id)

    assert result.action is PublicationProviderCoordinatorAction.RECORDED_PRODUCT_OBSERVATION
    assert result.stage_id == stage.stage_id
    assert result.aggregate_state is PublicationState.PUBLISHED
    assert factory.calls == 0


def test_stage_landing_before_service_begin_rejects_stale_deadline_then_recovers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = Harness()
    harness.dispatch_and_reconstruct()
    harness.complete_preflight()
    _publish_result, post_claim = harness.claim_publish()
    harness.service.record_post_outcome(
        harness.command(
            RecordPublicationPostOutcomeCommand,
            harness.next_operation("accepted"),
            evidence=harness.publish_evidence(post_claim, accepted=True),
        )
    )
    harness.clock.tick()
    _claim_result, product_claim = harness.claim_product(PublicationCallPurpose.VERIFICATION)
    positive = harness.product_evidence(product_claim, positive=True)
    harness.clock.now = harness.authority.snapshot.verification_deadline
    factory = ExplodingBoundaryFactory()
    coordinator = _coordinator(harness, factory)
    original_settle = harness.service.settle_deadline
    staged = []

    def stage_before_service_begin(command):  # type: ignore[no-untyped-def]
        staged.append(harness.stage_evidence(positive))
        return original_settle(command)

    monkeypatch.setattr(harness.service, "settle_deadline", stage_before_service_begin)

    with pytest.raises(PublicationConflictError) as error:
        coordinator.advance(owner_id=OWNER_ID, aggregate_id=harness.aggregate_id)

    assert error.value.code.value == "PUBLICATION_CONCURRENT_WRITE"
    assert harness.authority.aggregate.state is PublicationState.PUBLICATION_VERIFYING
    assert harness.authority.aggregate.terminal_at is None
    assert len(staged) == 1

    monkeypatch.setattr(harness.service, "settle_deadline", original_settle)
    recovered = coordinator.advance(owner_id=OWNER_ID, aggregate_id=harness.aggregate_id)

    assert recovered.action is PublicationProviderCoordinatorAction.RECORDED_PRODUCT_OBSERVATION
    assert recovered.stage_id == staged[0].stage_id
    assert recovered.aggregate_state is PublicationState.PUBLISHED
    assert factory.calls == 0


def test_deadline_at_equality_settles_without_dispatch_reconstruction_or_provider_factory() -> None:
    harness = Harness()
    harness.clock.now = harness.authority.snapshot.verification_deadline
    factory = ExplodingBoundaryFactory()
    coordinator = _coordinator(harness, factory)

    result = coordinator.advance(owner_id=OWNER_ID, aggregate_id=harness.aggregate_id)

    assert result.action is PublicationProviderCoordinatorAction.SETTLED_DEADLINE
    assert result.aggregate_state is PublicationState.PUBLICATION_FAILED
    assert factory.calls == 0
    assert harness.authority.attempt.publish_post_call_count == 0


def test_consumed_staged_product_observation_is_not_recovered_twice() -> None:
    harness = Harness()
    harness.dispatch_and_reconstruct()
    harness.complete_preflight()
    _publish_result, post_claim = harness.claim_publish()
    accepted = harness.publish_evidence(post_claim, accepted=True)
    harness.service.record_post_outcome(
        harness.command(
            RecordPublicationPostOutcomeCommand,
            harness.next_operation("accepted"),
            evidence=accepted,
        )
    )
    harness.clock.tick()
    _claim_result, product_claim = harness.claim_product(PublicationCallPurpose.VERIFICATION)
    product = harness.product_evidence(product_claim, positive=False)
    stage = harness.stage_evidence(product)
    factory = ExplodingBoundaryFactory()
    coordinator = _coordinator(harness, factory)

    first = coordinator.advance(owner_id=OWNER_ID, aggregate_id=harness.aggregate_id)
    assert first.action is PublicationProviderCoordinatorAction.RECORDED_PRODUCT_OBSERVATION
    assert first.stage_id == stage.stage_id

    assert (
        harness.store.list_unconsumed_provider_evidence(
            OWNER_ID,
            harness.aggregate_id,
        )
        == ()
    )
    with pytest.raises(RuntimeError, match="stopped"):
        coordinator.advance(owner_id=OWNER_ID, aggregate_id=harness.aggregate_id)
    assert factory.calls == 1


def test_coordinator_never_accepts_a_loose_product_dto_as_a_durable_stage() -> None:
    harness = Harness()
    harness.dispatch_and_reconstruct()
    harness.complete_preflight()
    _publish_result, post_claim = harness.claim_publish()
    accepted = harness.publish_evidence(post_claim, accepted=True)
    harness.service.record_post_outcome(
        harness.command(
            RecordPublicationPostOutcomeCommand,
            harness.next_operation("accepted"),
            evidence=accepted,
        )
    )
    harness.clock.tick()
    claim_result, product_claim = harness.claim_product(PublicationCallPurpose.VERIFICATION)
    assert claim_result.fresh_call_grant is not None
    loose = harness.product_evidence(product_claim, positive=False)

    class LooseBoundary:
        def poll_exact_product(self, **_values: Any):  # type: ignore[no-untyped-def]
            return loose

    class LooseFactory:
        @staticmethod
        def prepare_credential(*, execution_authority):  # type: ignore[no-untyped-def]
            authority = execution_authority.provider_authority
            assert authority is not None
            return issue_bound_publication_provider_credential(
                authority=authority,
                bearer_token=SecretStr(TOKEN),
            )

        def __call__(self, **_values: Any) -> LooseBoundary:
            return LooseBoundary()

    # A prior claim with no stage is intentionally abandoned; the next coordinator claim is fresh.
    coordinator = _coordinator(harness, LooseFactory())
    with pytest.raises(Exception, match="durable evidence stage|fresh"):
        coordinator.advance(owner_id=OWNER_ID, aggregate_id=harness.aggregate_id)
