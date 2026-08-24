"""Phase 7.3 execution integration for durable, single-use provider evidence."""

from __future__ import annotations

from datetime import timedelta

import pytest

from mr_lister.publication.contract import PublicationPermitState, PublicationState
from mr_lister.publication.errors import (
    PublicationConflictError,
    PublicationNotFoundError,
)
from mr_lister.publication.evidence_provenance import (
    PublicationDefinitivePreflightEvidence,
    PublicationProviderEvidenceKind,
    build_provider_evidence_commit,
)
from mr_lister.publication.execution_commands import (
    RecordPublicationPostOutcomeCommand,
    RecordPublicationPreflightCommand,
    RecordPublicationProductObservationCommand,
    SettleDefinitivePreflightFailureCommand,
    SettlePublicationDeadlineCommand,
)
from mr_lister.publication.execution_fingerprints import execution_record_fingerprint
from mr_lister.publication.execution_models import (
    PublicationCallPurpose,
    PublicationExecutionOperation,
    PublicationExecutionWorkStatus,
    PublicationPreflightFailureReason,
    PublicationTerminalReason,
)
from tests.test_phase71_publication_store import OWNER_ID
from tests.test_phase72_publication_execution import Harness


def _preflight_stages(harness: Harness):  # type: ignore[no-untyped-def]
    _, shop_claim = harness.claim_shop()
    shop_stage = harness.stage_evidence(harness.shop_evidence(shop_claim))
    harness.clock.tick()
    _, product_claim = harness.claim_product(PublicationCallPurpose.PRODUCT_PREFLIGHT)
    product_stage = harness.stage_evidence(harness.product_evidence(product_claim))
    harness.clock.tick()
    return shop_stage, product_stage


def _preflight_command(
    harness: Harness,
    shop_stage,  # type: ignore[no-untyped-def]
    product_stage,  # type: ignore[no-untyped-def]
    *,
    operation_id: str = "consume_preflight_stages",
) -> RecordPublicationPreflightCommand:
    return harness.command(
        RecordPublicationPreflightCommand,
        operation_id,
        shop_evidence_stage_id=shop_stage.stage_id,
        shop_evidence_stage_fingerprint=shop_stage.fingerprint,
        product_evidence_stage_id=product_stage.stage_id,
        product_evidence_stage_fingerprint=product_stage.fingerprint,
    )


def _negative_shop_stage(harness: Harness):  # type: ignore[no-untyped-def]
    _, claim = harness.claim_shop()
    provider = harness.authority.provider_authority
    assert provider is not None
    values = {
        "call_claim_id": claim.authorization_id,
        "call_claim_fingerprint": claim.fingerprint,
        "provider_authority_id": provider.provider_authority_id,
        "provider_authority_fingerprint": provider.fingerprint,
        "failure_reason": PublicationPreflightFailureReason.SHOP_NOT_CONNECTED_TO_ETSY,
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


def test_preflight_consumes_two_exact_stages_in_one_execution_commit() -> None:
    harness = Harness(capture_commits=True)
    harness.dispatch_and_reconstruct()
    shop_stage, product_stage = _preflight_stages(harness)
    command = _preflight_command(harness, shop_stage, product_stage)

    result = harness.service.record_preflight(command)
    replay = harness.service.record_preflight(command)

    proof = harness.authority.preflight_proof
    assert proof is not None
    assert replay.receipt == result.receipt
    assert proof.shop_evidence_fingerprint == shop_stage.evidence_fingerprint
    assert proof.product_evidence_fingerprint == product_stage.evidence_fingerprint
    assert (
        harness.store.list_unconsumed_provider_evidence(
            OWNER_ID,
            harness.aggregate_id,
        )
        == ()
    )
    consumptions = harness.store.provider_evidence_consumptions
    assert set(consumptions) == {
        (harness.aggregate_id, shop_stage.stage_id),
        (harness.aggregate_id, product_stage.stage_id),
    }
    for stage in (shop_stage, product_stage):
        consumption = consumptions[(harness.aggregate_id, stage.stage_id)]
        assert consumption.stage_fingerprint == stage.fingerprint
        assert consumption.evidence_fingerprint == stage.evidence_fingerprint
        assert consumption.operation is PublicationExecutionOperation.RECORD_PREFLIGHT
        assert consumption.operation_id == result.receipt.operation_id
        assert consumption.receipt_id == result.receipt.receipt_id


def test_post_and_product_transitions_each_consume_one_state_specific_stage() -> None:
    harness = Harness()
    harness.dispatch_and_reconstruct()
    harness.complete_preflight()
    _, post_claim = harness.claim_publish()
    post_stage = harness.stage_evidence(harness.publish_evidence(post_claim, accepted=True))
    harness.clock.tick()

    post_result = harness.service.record_post_outcome(
        harness.command(
            RecordPublicationPostOutcomeCommand,
            "consume_publish_stage",
            evidence_stage_id=post_stage.stage_id,
            evidence_stage_fingerprint=post_stage.fingerprint,
        )
    )
    post_consumption = harness.store.provider_evidence_consumptions[
        (harness.aggregate_id, post_stage.stage_id)
    ]
    assert post_consumption.operation is PublicationExecutionOperation.RECORD_POST_OUTCOME
    assert post_consumption.receipt_id == post_result.receipt.receipt_id

    harness.clock.tick()
    _, product_claim = harness.claim_product(PublicationCallPurpose.VERIFICATION)
    product_stage = harness.stage_evidence(harness.product_evidence(product_claim, positive=True))
    harness.clock.tick()
    product_result = harness.service.record_product_observation(
        harness.command(
            RecordPublicationProductObservationCommand,
            "consume_verification_stage",
            evidence_stage_id=product_stage.stage_id,
            evidence_stage_fingerprint=product_stage.fingerprint,
        )
    )
    product_consumption = harness.store.provider_evidence_consumptions[
        (harness.aggregate_id, product_stage.stage_id)
    ]
    assert product_consumption.operation is (
        PublicationExecutionOperation.RECORD_PRODUCT_OBSERVATION
    )
    assert product_consumption.receipt_id == product_result.receipt.receipt_id
    assert harness.authority.aggregate.state is PublicationState.PUBLISHED


def test_missing_second_preflight_stage_writes_neither_consumption_nor_proof() -> None:
    harness = Harness()
    harness.dispatch_and_reconstruct()
    shop_stage, product_stage = _preflight_stages(harness)
    command = _preflight_command(harness, shop_stage, product_stage).model_copy(
        update={
            "product_evidence_stage_id": "missing_product_stage",
            "product_evidence_stage_fingerprint": "9" * 64,
        }
    )

    with pytest.raises(PublicationNotFoundError):
        harness.service.record_preflight(command)

    assert harness.authority.preflight_proof is None
    assert harness.store.provider_evidence_consumptions == {}
    assert harness.store.list_unconsumed_provider_evidence(
        OWNER_ID,
        harness.aggregate_id,
    ) == (shop_stage, product_stage)


def test_owner_scoped_stage_lookup_and_restart_discovery_hide_other_owners() -> None:
    harness = Harness()
    harness.dispatch_and_reconstruct()
    shop_stage, product_stage = _preflight_stages(harness)

    assert (
        harness.store.get_provider_evidence_stage(
            OWNER_ID,
            harness.aggregate_id,
            shop_stage.stage_id,
        )
        == shop_stage
    )
    assert harness.store.list_unconsumed_provider_evidence(
        OWNER_ID,
        harness.aggregate_id,
    ) == (shop_stage, product_stage)
    with pytest.raises(PublicationNotFoundError):
        harness.store.get_provider_evidence_stage(
            "b" * 32,
            harness.aggregate_id,
            shop_stage.stage_id,
        )
    with pytest.raises(PublicationNotFoundError):
        harness.store.list_unconsumed_provider_evidence(
            "b" * 32,
            harness.aggregate_id,
        )


def test_stage_commit_loses_cas_if_execution_authority_changes() -> None:
    harness = Harness()
    harness.dispatch_and_reconstruct()
    _, claim = harness.claim_shop()
    authority = harness.authority
    audit = authority.provider_audits[-1]
    commit = build_provider_evidence_commit(
        authority,
        claim,
        audit,
        harness.shop_evidence(claim),
        staged_at=harness.clock.now,
    )
    harness.clock.tick()
    harness.claim_product(PublicationCallPurpose.PRODUCT_PREFLIGHT)

    with pytest.raises(PublicationConflictError) as error:
        harness.store.stage_evidence(commit)

    assert error.value.code.value == "PUBLICATION_CONCURRENT_WRITE"
    assert harness.store.provider_evidence_stages == {}


def test_stage_increments_root_watermark_once_and_exact_replay_is_read_only() -> None:
    harness = Harness()
    harness.dispatch_and_reconstruct()
    _, claim = harness.claim_shop()
    evidence = harness.shop_evidence(claim)
    before = harness.authority.aggregate.provider_evidence_record_version

    first = harness.stage_evidence(evidence)
    after_first = harness.authority.aggregate.provider_evidence_record_version
    replay = harness.stage_evidence(evidence)

    assert replay == first
    assert after_first == before + 1
    assert harness.authority.aggregate.provider_evidence_record_version == after_first


def test_stage_watermark_invalidates_stale_deadline_settlement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = Harness()
    harness.dispatch_and_reconstruct()
    harness.complete_preflight()
    _, post_claim = harness.claim_publish()
    harness.service.record_post_outcome(
        harness.command(
            RecordPublicationPostOutcomeCommand,
            "watermark_post",
            evidence=harness.publish_evidence(post_claim, accepted=True),
        )
    )
    harness.clock.tick()
    _, product_claim = harness.claim_product(PublicationCallPurpose.VERIFICATION)
    positive = harness.product_evidence(product_claim, positive=True)
    harness.clock.now = harness.authority.snapshot.verification_deadline

    deferred: list[object] = []
    original_commit = harness.store.commit_execution

    class DeferredCommit(Exception):
        pass

    def capture(commit: object) -> None:
        deferred.append(commit)
        raise DeferredCommit

    monkeypatch.setattr(harness.store, "commit_execution", capture)
    with pytest.raises(DeferredCommit):
        harness.service.settle_deadline(
            harness.command(SettlePublicationDeadlineCommand, "stale_deadline")
        )
    monkeypatch.setattr(harness.store, "commit_execution", original_commit)
    assert len(deferred) == 1

    stage = harness.stage_evidence(positive)
    with pytest.raises(PublicationConflictError) as error:
        original_commit(deferred[0])  # type: ignore[arg-type]

    assert error.value.code.value == "PUBLICATION_CONCURRENT_WRITE"
    assert harness.authority.aggregate.state is PublicationState.PUBLICATION_VERIFYING
    assert harness.store.list_unconsumed_provider_evidence(
        OWNER_ID,
        harness.aggregate_id,
    ) == (stage,)


def test_definitive_negative_retires_available_permit_with_zero_post_calls() -> None:
    harness = Harness()
    harness.dispatch_and_reconstruct()
    stage = _negative_shop_stage(harness)
    harness.clock.tick()

    result = harness.service.settle_definitive_preflight_failure(
        harness.command(
            SettleDefinitivePreflightFailureCommand,
            "definitive_shop_negative",
            evidence_stage_id=stage.stage_id,
            evidence_stage_fingerprint=stage.fingerprint,
        )
    )

    terminal = harness.authority
    assert result.receipt.operation is (
        PublicationExecutionOperation.SETTLE_DEFINITIVE_PREFLIGHT_FAILURE
    )
    assert terminal.aggregate.state is PublicationState.PUBLICATION_FAILED
    assert terminal.work.status is PublicationExecutionWorkStatus.FAILED
    assert terminal.permit.status is PublicationPermitState.RETIRED
    assert terminal.attempt.publish_post_call_count == 0
    assert terminal.preflight_proof is None
    assert terminal.mutation_claim is None
    assert terminal.post_observation is None
    assert terminal.report.terminal_reason is (
        PublicationTerminalReason.DEFINITIVE_PREFLIGHT_FAILURE
    )
    assert terminal.aggregate.terminal_at < terminal.snapshot.verification_deadline
    consumption = harness.store.provider_evidence_consumptions[
        (harness.aggregate_id, stage.stage_id)
    ]
    assert consumption.evidence_kind is (
        PublicationProviderEvidenceKind.DEFINITIVE_PREFLIGHT_NEGATIVE
    )
    assert consumption.receipt_id == result.receipt.receipt_id


def test_definitive_negative_cannot_settle_at_deadline_and_remains_unconsumed() -> None:
    harness = Harness()
    harness.dispatch_and_reconstruct()
    stage = _negative_shop_stage(harness)
    harness.clock.now = harness.authority.snapshot.verification_deadline

    with pytest.raises(PublicationConflictError) as error:
        harness.service.settle_definitive_preflight_failure(
            harness.command(
                SettleDefinitivePreflightFailureCommand,
                "late_definitive_shop_negative",
                evidence_stage_id=stage.stage_id,
                evidence_stage_fingerprint=stage.fingerprint,
            )
        )

    assert error.value.code.value == "PUBLICATION_DEADLINE_EXPIRED"
    assert harness.authority.aggregate.state is PublicationState.PUBLICATION_REQUESTED
    assert harness.authority.permit.status is PublicationPermitState.AVAILABLE
    assert harness.store.provider_evidence_consumptions == {}
    assert harness.store.list_unconsumed_provider_evidence(
        OWNER_ID,
        harness.aggregate_id,
    ) == (stage,)


def test_definitive_negative_observed_at_deadline_is_not_trusted() -> None:
    harness = Harness()
    harness.dispatch_and_reconstruct()
    _, claim = harness.claim_shop()
    provider = harness.authority.provider_authority
    assert provider is not None
    deadline = harness.authority.snapshot.verification_deadline
    values = {
        "call_claim_id": claim.authorization_id,
        "call_claim_fingerprint": claim.fingerprint,
        "provider_authority_id": provider.provider_authority_id,
        "provider_authority_fingerprint": provider.fingerprint,
        "failure_reason": PublicationPreflightFailureReason.SHOP_NOT_CONNECTED_TO_ETSY,
        "sanitized_response_fingerprint": "8" * 64,
        "observed_at": deadline,
    }
    evidence = PublicationDefinitivePreflightEvidence(
        **values,
        fingerprint=execution_record_fingerprint(
            "definitive_preflight_evidence",
            values,
        ),
    )
    harness.clock.now = deadline + timedelta(seconds=1)
    stage = harness.stage_evidence(evidence)

    with pytest.raises(PublicationConflictError):
        harness.service.settle_definitive_preflight_failure(
            harness.command(
                SettleDefinitivePreflightFailureCommand,
                "negative_observed_at_deadline",
                evidence_stage_id=stage.stage_id,
                evidence_stage_fingerprint=stage.fingerprint,
            )
        )
    assert harness.store.provider_evidence_consumptions == {}
