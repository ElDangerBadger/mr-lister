"""Phase 7.3 durable provider-evidence provenance oracle tests."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import pytest
from pydantic import ValidationError

from mr_lister.publication.errors import PublicationConflictError
from mr_lister.publication.evidence_provenance import (
    InMemoryPublicationEvidenceCore,
    PublicationDefinitivePreflightEvidence,
    PublicationPreflightFailureReason,
    PublicationProviderEvidenceKind,
    PublicationProviderEvidenceType,
    build_provider_evidence_commit,
)
from mr_lister.publication.execution_fingerprints import execution_record_fingerprint
from mr_lister.publication.execution_models import PublicationExecutionOperation
from tests.test_phase72_publication_execution import Harness


def _staged_shop() -> tuple[Harness, InMemoryPublicationEvidenceCore, object]:
    harness = Harness()
    harness.dispatch_and_reconstruct()
    _, claim = harness.claim_shop(audit=False)
    _, audit = harness.audit(claim)
    evidence = harness.shop_evidence(claim)
    core = InMemoryPublicationEvidenceCore(
        lambda aggregate_id: (
            harness.authority
            if aggregate_id == harness.aggregate_id
            else (_ for _ in ()).throw(KeyError(aggregate_id))
        ),
        clock=harness.clock,
    )
    commit = build_provider_evidence_commit(
        harness.authority,
        claim,
        audit,
        evidence,
        staged_at=harness.clock.now,
    )
    return harness, core, commit


def test_audited_boundary_evidence_stages_once_and_replays_exactly() -> None:
    harness, core, commit = _staged_shop()

    first = core.stage_evidence(commit)
    replay = core.stage_evidence(commit)

    assert replay == first
    assert first.aggregate_id == harness.aggregate_id
    assert first.evidence_kind is PublicationProviderEvidenceKind.SHOP_PREFLIGHT
    assert first.evidence_type is PublicationProviderEvidenceType.SHOP_PREFLIGHT
    assert first.evidence_fingerprint == first.evidence.fingerprint
    assert core.get_stage(first.aggregate_id, first.stage_id) == first


def test_stage_requires_exact_current_claim_audit_and_provider_authority() -> None:
    harness = Harness()
    harness.dispatch_and_reconstruct()
    _, claim = harness.claim_shop(audit=False)
    evidence = harness.shop_evidence(claim)
    provider = harness.authority.provider_authority
    assert provider is not None

    with pytest.raises(PublicationConflictError):
        build_provider_evidence_commit(
            harness.authority,
            claim,
            # A structurally valid audit for another call cannot be synthesized from nothing.
            harness.audit(claim)[1].model_copy(update={"call_claim_id": "wrong_claim"}),
            evidence,
            staged_at=harness.clock.now,
        )


def test_one_claim_cannot_stage_changed_evidence() -> None:
    _, core, commit = _staged_shop()
    core.stage_evidence(commit)
    evidence = commit.stage.evidence.model_copy(update={"sanitized_response_fingerprint": "9" * 64})
    values = evidence.model_dump(mode="python", exclude={"contract_version", "fingerprint"})
    changed = type(evidence)(
        **values,
        fingerprint=execution_record_fingerprint("shop_preflight_evidence", values),
    )
    changed_commit = build_provider_evidence_commit(
        commit.expected,
        commit.expected.call_claims[-1],
        commit.expected.provider_audits[-1],
        changed,
        staged_at=commit.stage.staged_at,
    )

    with pytest.raises(PublicationConflictError) as error:
        core.stage_evidence(changed_commit)
    assert error.value.code.value == "PUBLICATION_CONCURRENT_WRITE"


def test_consumption_is_single_winner_and_exact_replay_is_idempotent() -> None:
    _, core, commit = _staged_shop()
    stage = core.stage_evidence(commit)

    def consume(operation_id: str):  # type: ignore[no-untyped-def]
        return core.consume_evidence(
            aggregate_id=stage.aggregate_id,
            stage_id=stage.stage_id,
            stage_fingerprint=stage.fingerprint,
            operation_id=operation_id,
            operation=PublicationExecutionOperation.RECORD_PREFLIGHT,
        )

    first = consume("record_preflight_once")
    assert consume("record_preflight_once") == first
    with pytest.raises(PublicationConflictError):
        consume("changed_operation")

    _, concurrent_core, concurrent_commit = _staged_shop()
    concurrent_stage = concurrent_core.stage_evidence(concurrent_commit)

    def race(operation_id: str):  # type: ignore[no-untyped-def]
        return concurrent_core.consume_evidence(
            aggregate_id=concurrent_stage.aggregate_id,
            stage_id=concurrent_stage.stage_id,
            stage_fingerprint=concurrent_stage.fingerprint,
            operation_id=operation_id,
            operation=PublicationExecutionOperation.RECORD_PREFLIGHT,
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(race, value) for value in ("winner_a", "winner_b")]
        outcomes: list[object] = []
        for future in futures:
            try:
                outcomes.append(future.result())
            except PublicationConflictError as error:
                outcomes.append(error)
    assert sum(isinstance(value, PublicationConflictError) for value in outcomes) == 1


def test_local_authority_failure_cannot_be_recast_as_provider_negative_evidence() -> None:
    harness = Harness()
    harness.dispatch_and_reconstruct()
    _, claim = harness.claim_shop(audit=False)
    provider = harness.authority.provider_authority
    assert provider is not None
    values = {
        "call_claim_id": claim.authorization_id,
        "call_claim_fingerprint": claim.fingerprint,
        "provider_authority_id": provider.provider_authority_id,
        "provider_authority_fingerprint": provider.fingerprint,
        "failure_reason": PublicationPreflightFailureReason.LOCAL_AUTHORITY_INVALID,
        "sanitized_response_fingerprint": "8" * 64,
        "observed_at": harness.clock.now,
    }

    with pytest.raises(ValidationError):
        PublicationDefinitivePreflightEvidence(
            **values,
            fingerprint=execution_record_fingerprint(
                "definitive_preflight_evidence",
                values,
            ),
        )


def test_stage_serialization_contains_no_secret_or_raw_transport_material() -> None:
    _, core, commit = _staged_shop()
    stage = core.stage_evidence(commit)
    serialized = stage.model_dump_json().casefold()

    for forbidden in (
        "authorization",
        "bearer",
        "token",
        "cookie",
        "response_body",
        "request_body",
        "headers",
        "api.printify.com",
    ):
        assert forbidden not in serialized


def test_isolated_core_rejects_a_second_stage_from_a_stale_root_watermark() -> None:
    harness = Harness()
    harness.dispatch_and_reconstruct()
    _, first_claim = harness.claim_shop(audit=False)
    _, first_audit = harness.audit(first_claim)
    _, second_claim = harness.claim_shop(audit=False)
    _, second_audit = harness.audit(second_claim)
    authority = harness.authority
    core = InMemoryPublicationEvidenceCore(
        lambda aggregate_id: (
            harness.authority
            if aggregate_id == harness.aggregate_id
            else (_ for _ in ()).throw(KeyError(aggregate_id))
        ),
        clock=harness.clock,
    )
    first = build_provider_evidence_commit(
        authority,
        first_claim,
        first_audit,
        harness.shop_evidence(first_claim),
        staged_at=harness.clock.now,
    )
    stale_second = build_provider_evidence_commit(
        authority,
        second_claim,
        second_audit,
        harness.shop_evidence(second_claim),
        staged_at=harness.clock.now,
    )

    core.stage_evidence(first)

    with pytest.raises(PublicationConflictError) as error:
        core.stage_evidence(stale_second)
    assert error.value.code.value == "PUBLICATION_CONCURRENT_WRITE"
    assert harness.authority.aggregate.provider_evidence_record_version == 0
