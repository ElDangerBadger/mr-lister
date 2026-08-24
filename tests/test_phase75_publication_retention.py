"""Pure Phase 7.5 terminal-retention authority and service tests."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta

import pytest
from test_phase71_publication_store import OWNER_ID
from test_phase72_publication_execution import Harness

from mr_lister.control.publication_retention import (
    PublicationRetentionCompletionAuthority,
    publication_operational_expiry_epoch,
    publication_retention_completion_fingerprint,
)
from mr_lister.publication.contract import PublicationState
from mr_lister.publication.execution_commands import (
    RecordPublicationPostOutcomeCommand,
    RecordPublicationProductObservationCommand,
    SettlePublicationDeadlineCommand,
)
from mr_lister.publication.execution_fingerprints import execution_record_fingerprint
from mr_lister.publication.execution_models import PublicationCallPurpose
from mr_lister.publication.retention import (
    PublicationOperationalRetentionService,
    PublicationRetentionBoundaryInvalidError,
    PublicationRetentionConflictError,
    PublicationRetentionDependencyUnavailableError,
    PublicationTerminalRetentionAuthority,
    build_publication_terminal_retention_authority,
    publication_terminal_retention_authority_fingerprint,
)
from mr_lister.publication.retention_locator import (
    build_publication_request_receipt_locator,
)


def _locator(harness: Harness):  # type: ignore[no-untyped-def]
    receipt = harness.transaction.commit.receipt
    return build_publication_request_receipt_locator(
        aggregate_id=receipt.aggregate_id,
        owner_id=receipt.owner_id,
        job_id=receipt.job_id,
        receipt_id=receipt.receipt_id,
        receipt_fingerprint=receipt.fingerprint,
        idempotency_key_digest=receipt.idempotency_key_digest,
    )


def _terminal_authority(harness: Harness) -> PublicationTerminalRetentionAuthority:
    return build_publication_terminal_retention_authority(
        harness.authority,
        harness.store.load_linked_job(OWNER_ID, harness.aggregate_id),
        _locator(harness),
    )


def _failed() -> tuple[Harness, PublicationTerminalRetentionAuthority]:
    harness = Harness()
    harness.clock.now = harness.authority.snapshot.verification_deadline
    harness.service.settle_deadline(
        harness.command(SettlePublicationDeadlineCommand, "phase75_failed")
    )
    return harness, _terminal_authority(harness)


def _published() -> tuple[Harness, PublicationTerminalRetentionAuthority]:
    harness = Harness()
    harness.dispatch_and_reconstruct()
    harness.complete_preflight()
    _, post_claim = harness.claim_publish()
    evidence = harness.publish_evidence(post_claim, accepted=True)
    harness.clock.tick()
    harness.service.record_post_outcome(
        harness.command(
            RecordPublicationPostOutcomeCommand,
            "phase75_published_post",
            evidence=evidence,
        )
    )
    harness.clock.tick()
    _, read_claim = harness.claim_product(PublicationCallPurpose.VERIFICATION)
    positive = harness.product_evidence(read_claim, positive=True)
    harness.clock.tick()
    harness.service.record_product_observation(
        harness.command(
            RecordPublicationProductObservationCommand,
            "phase75_published_read",
            evidence=positive,
        )
    )
    return harness, _terminal_authority(harness)


def _unknown() -> tuple[Harness, PublicationTerminalRetentionAuthority]:
    harness = Harness()
    harness.dispatch_and_reconstruct()
    harness.complete_preflight()
    _, post_claim = harness.claim_publish()
    evidence = harness.publish_evidence(post_claim, accepted=False)
    harness.clock.tick()
    harness.service.record_post_outcome(
        harness.command(
            RecordPublicationPostOutcomeCommand,
            "phase75_unknown_post",
            evidence=evidence,
        )
    )
    harness.clock.tick()
    _, read_claim = harness.claim_product(PublicationCallPurpose.RECONCILIATION)
    read = harness.product_evidence(read_claim)
    harness.clock.now = harness.authority.snapshot.verification_deadline
    harness.service.record_product_observation(
        harness.command(
            RecordPublicationProductObservationCommand,
            "phase75_unknown_read",
            evidence=read,
        )
    )
    return harness, _terminal_authority(harness)


def _completion(
    authority: PublicationTerminalRetentionAuthority,
    *,
    completed_at: datetime,
    changes: dict[str, object] | None = None,
) -> PublicationRetentionCompletionAuthority:
    values: dict[str, object] = {
        "job_id": authority.job.job_id,
        "aggregate_id": authority.aggregate.aggregate_id,
        "job_record_version": authority.job.record_version,
        "terminal_state": authority.aggregate.state.value,
        "terminal_at": authority.aggregate.terminal_at,
        "terminal_summary_fingerprint": (authority.terminal_job_link.terminal_summary_fingerprint),
        "source_artifact_fingerprint": authority.job.source_artifact_fingerprint,
        "aggregate_fingerprint": authority.aggregate.fingerprint,
        "report_id": authority.report.report_id,
        "report_fingerprint": authority.report.fingerprint,
        "tombstone_fingerprint": authority.tombstone.fingerprint,
        "terminal_job_link_fingerprint": authority.terminal_job_link.fingerprint,
        "source_release_eligible_at": authority.aggregate.source_release_eligible_at,
        "operational_expires_at": authority.aggregate.operational_expires_at,
        "expires_at_epoch_seconds": publication_operational_expiry_epoch(
            authority.aggregate.operational_expires_at  # type: ignore[arg-type]
        ),
        "publication_row_count": 12,
        "ttl_assignment_count": 14,
        "inventory_fingerprint": "a" * 64,
        "completed_at": completed_at,
    }
    values.update(changes or {})
    basis = PublicationRetentionCompletionAuthority.model_construct(
        **values,
        fingerprint="0" * 64,
    )
    return PublicationRetentionCompletionAuthority(
        **values,
        fingerprint=publication_retention_completion_fingerprint(basis),
    )


class Store:
    def __init__(
        self,
        authority: PublicationTerminalRetentionAuthority,
        *,
        completion_changes: dict[str, object] | None = None,
        load_error: Exception | None = None,
    ) -> None:
        self.authority = authority
        self.completion_changes = completion_changes
        self.load_error = load_error
        self.assign_calls = 0

    def load_terminal_retention_authority(
        self,
        owner_id: str,
        aggregate_id: str,
    ) -> PublicationTerminalRetentionAuthority:
        del owner_id, aggregate_id
        if self.load_error is not None:
            raise self.load_error
        return self.authority

    def assign_terminal_retention(
        self,
        authority: PublicationTerminalRetentionAuthority,
        *,
        completed_at: datetime,
    ) -> PublicationRetentionCompletionAuthority:
        self.assign_calls += 1
        return _completion(
            authority,
            completed_at=completed_at,
            changes=self.completion_changes,
        )


@pytest.mark.parametrize(
    ("factory", "state"),
    [
        (_published, PublicationState.PUBLISHED),
        (_failed, PublicationState.PUBLICATION_FAILED),
        (_unknown, PublicationState.PUBLICATION_OUTCOME_UNKNOWN),
    ],
)
def test_all_terminal_states_assign_exact_thirty_and_ninety_day_authority(
    factory: Callable[[], tuple[Harness, PublicationTerminalRetentionAuthority]],
    state: PublicationState,
) -> None:
    harness, authority = factory()
    store = Store(authority)
    service = PublicationOperationalRetentionService(store, clock=harness.clock)

    completion = service.assign(owner_id=OWNER_ID, aggregate_id=harness.aggregate_id)

    assert completion.terminal_state == state.value
    assert completion.source_release_eligible_at == completion.terminal_at + timedelta(days=30)
    assert completion.operational_expires_at == completion.terminal_at + timedelta(days=90)
    assert completion.expires_at_epoch_seconds == publication_operational_expiry_epoch(
        completion.operational_expires_at
    )
    assert store.assign_calls == 1


def test_preterminal_graph_cannot_become_retention_authority() -> None:
    harness = Harness()

    with pytest.raises(PublicationRetentionBoundaryInvalidError):
        build_publication_terminal_retention_authority(
            harness.authority,
            harness.store.load_linked_job(OWNER_ID, harness.aggregate_id),
            _locator(harness),
        )


@pytest.mark.parametrize("offset", [timedelta(microseconds=-1), timedelta(days=90)])
def test_service_refuses_before_terminal_and_at_operational_expiry(offset: timedelta) -> None:
    _, authority = _failed()
    assert authority.aggregate.terminal_at is not None
    clock_value = authority.aggregate.terminal_at + offset
    store = Store(authority)
    service = PublicationOperationalRetentionService(store, clock=lambda: clock_value)

    with pytest.raises(PublicationRetentionConflictError):
        service.assign(owner_id=OWNER_ID, aggregate_id=authority.aggregate.aggregate_id)

    assert store.assign_calls == 0


def test_revalidated_nested_graph_rejects_foreign_attempt_before_store_write() -> None:
    harness, authority = _failed()
    attempt = authority.execution.attempt
    attempt_values = {
        **attempt.model_dump(mode="python", exclude={"contract_version", "fingerprint"}),
        "owner_id": "f" * 64,
    }
    foreign_attempt = type(attempt)(
        **attempt_values,
        fingerprint=execution_record_fingerprint("execution_attempt", attempt_values),
    )
    foreign_execution = authority.execution.model_copy(update={"attempt": foreign_attempt})
    foreign_basis = authority.model_copy(update={"execution": foreign_execution})
    foreign = foreign_basis.model_copy(
        update={"fingerprint": publication_terminal_retention_authority_fingerprint(foreign_basis)}
    )
    store = Store(foreign)
    service = PublicationOperationalRetentionService(store, clock=harness.clock)

    with pytest.raises(PublicationRetentionBoundaryInvalidError):
        service.assign(owner_id=OWNER_ID, aggregate_id=harness.aggregate_id)

    assert store.assign_calls == 0


def test_completion_mismatch_is_rejected_after_store_boundary() -> None:
    harness, authority = _failed()
    store = Store(authority, completion_changes={"aggregate_fingerprint": "b" * 64})
    service = PublicationOperationalRetentionService(store, clock=harness.clock)

    with pytest.raises(PublicationRetentionBoundaryInvalidError):
        service.assign(owner_id=OWNER_ID, aggregate_id=harness.aggregate_id)

    assert store.assign_calls == 1


@pytest.mark.parametrize(
    ("clock", "error_type"),
    [
        (lambda: datetime(2026, 1, 1), PublicationRetentionBoundaryInvalidError),
        (
            lambda: (_ for _ in ()).throw(RuntimeError("private clock detail")),
            PublicationRetentionDependencyUnavailableError,
        ),
    ],
)
def test_clock_failures_are_closed_and_sanitized(
    clock: Callable[[], datetime],
    error_type: type[Exception],
) -> None:
    _, authority = _failed()
    store = Store(authority)
    service = PublicationOperationalRetentionService(store, clock=clock)

    with pytest.raises(error_type) as captured:
        service.assign(owner_id=OWNER_ID, aggregate_id=authority.aggregate.aggregate_id)

    assert "private" not in str(captured.value)
    assert captured.value.__cause__ is None
    assert store.assign_calls == 0


def test_dependency_failure_is_sanitized_without_store_write() -> None:
    harness, authority = _failed()
    store = Store(authority, load_error=RuntimeError("secret dependency identity"))
    service = PublicationOperationalRetentionService(store, clock=harness.clock)

    with pytest.raises(PublicationRetentionDependencyUnavailableError) as captured:
        service.assign(owner_id=OWNER_ID, aggregate_id=harness.aggregate_id)

    assert "secret" not in str(captured.value)
    assert captured.value.__cause__ is None
    assert store.assign_calls == 0
