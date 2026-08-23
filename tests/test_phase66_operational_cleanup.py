from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from mr_lister.control.models import ControlJobRecord, ControlJobState
from mr_lister.production.operational_cleanup import (
    OperationalCleanupAuthorityChangedError,
    OperationalCleanupCheckpoint,
    OperationalCleanupDependencyUnavailableError,
    OperationalExpiryPage,
    OperationalJobSearchPage,
    TerminalJobAuthority,
    TerminalOperationalRecordCleanup,
)

NOW = datetime(2026, 8, 23, 12, tzinfo=UTC)
OWNER = "a" * 64


def _job(
    state: ControlJobState,
    *,
    job_id: str,
    updated_at: datetime,
) -> ControlJobRecord:
    values: dict[str, Any] = {
        "owner_id": OWNER,
        "job_id": job_id,
        "record_version": 7,
        "event_sequence": 8,
        "state": state,
        "created_at": updated_at - timedelta(days=1),
        "updated_at": updated_at,
    }
    if state is ControlJobState.CANCELLED:
        values["cancellation_requested_at"] = updated_at - timedelta(hours=1)
    if state is ControlJobState.FAILED_TERMINAL:
        values["failure_id"] = f"failure_{job_id}"
    if state is ControlJobState.APPROVED:
        values.update(
            {
                "review_version": 1,
                "review_fingerprint": "b" * 64,
                "review_validated": True,
                "product_id": "product_1",
                "provider_payload_fingerprint": "c" * 64,
                "product_sync_id": "sync_1",
                "synchronized_review_version": 1,
                "product_sync_fingerprint": "d" * 64,
                "pricing_snapshot_id": "pricing_1",
                "pricing_snapshot_fingerprint": "e" * 64,
                "approved_review_version": 1,
                "approved_review_fingerprint": "b" * 64,
                "approval_fingerprint": "f" * 64,
            }
        )
    return ControlJobRecord.model_validate(values)


class QueueInventory:
    def __init__(self, pages: list[OperationalJobSearchPage]) -> None:
        self.pages = pages
        self.calls: list[tuple[str | None, int]] = []

    def search_next_job(self, *, cursor: str | None, limit: int) -> OperationalJobSearchPage:
        self.calls.append((cursor, limit))
        return self.pages.pop(0)


class QueueExpiry:
    def __init__(self, pages: list[OperationalExpiryPage | Exception]) -> None:
        self.pages = pages
        self.calls: list[dict[str, Any]] = []

    def assign_terminal_expiry(self, **kwargs: Any) -> OperationalExpiryPage:
        self.calls.append(kwargs)
        value = self.pages.pop(0)
        if isinstance(value, Exception):
            raise value
        return value


class MemoryCheckpointStore:
    def __init__(self, checkpoint: OperationalCleanupCheckpoint | None = None) -> None:
        self.checkpoint = checkpoint or OperationalCleanupCheckpoint()
        self.saves: list[tuple[OperationalCleanupCheckpoint, OperationalCleanupCheckpoint]] = []
        self.fail_save = False

    def load_checkpoint(self) -> OperationalCleanupCheckpoint:
        return self.checkpoint

    def save_checkpoint(
        self,
        *,
        expected: OperationalCleanupCheckpoint,
        updated: OperationalCleanupCheckpoint,
    ) -> None:
        if self.fail_save or expected != self.checkpoint:
            raise RuntimeError("concurrent checkpoint with private job material")
        self.saves.append((expected, updated))
        self.checkpoint = updated


def _page(
    job: ControlJobRecord | None,
    *,
    cursor: str | None,
    observed_at: datetime = NOW,
    scanned: int = 1,
) -> OperationalJobSearchPage:
    return OperationalJobSearchPage(
        observed_at=observed_at,
        job=job,
        records_scanned=scanned,
        next_cursor=cursor,
    )


def test_approved_nonterminal_and_recent_terminal_jobs_are_preserved() -> None:
    inventory = QueueInventory(
        [
            _page(
                _job(
                    ControlJobState.APPROVED,
                    job_id="approved_job",
                    updated_at=NOW - timedelta(days=200),
                ),
                cursor="scan.approved",
            ),
            _page(
                _job(
                    ControlJobState.AWAITING_APPROVAL,
                    job_id="active_job",
                    updated_at=NOW - timedelta(days=200),
                ),
                cursor="scan.active",
            ),
            _page(
                _job(
                    ControlJobState.CANCELLED,
                    job_id="recent_job",
                    updated_at=NOW - timedelta(days=89),
                ),
                cursor=None,
            ),
        ]
    )
    expiry = QueueExpiry([])
    checkpoints = MemoryCheckpointStore()

    result = TerminalOperationalRecordCleanup(
        inventory=inventory,
        expiry_store=expiry,
        checkpoints=checkpoints,
        clock=lambda: NOW,
    ).sweep()

    assert result.scan_complete is True
    assert result.jobs_observed == 3
    assert result.approved_jobs_preserved == 1
    assert result.nonterminal_jobs_preserved == 1
    assert result.recent_terminal_jobs_preserved == 1
    assert result.terminal_jobs_completed == 0
    assert expiry.calls == []
    assert checkpoints.checkpoint.active_authority is None
    assert checkpoints.checkpoint.scan_cursor is None


@pytest.mark.parametrize(
    "state",
    [ControlJobState.CANCELLED, ControlJobState.FAILED_TERMINAL],
)
def test_only_old_overall_terminal_jobs_receive_exact_90_day_expiry(
    state: ControlJobState,
) -> None:
    terminal = _job(
        state,
        job_id=f"old_{state.value}",
        updated_at=NOW - timedelta(days=91),
    )
    inventory = QueueInventory([_page(terminal, cursor=None, scanned=17)])
    expiry = QueueExpiry(
        [
            OperationalExpiryPage(
                records_examined=3,
                records_assigned=3,
                next_cursor="assignment.owner",
            ),
            OperationalExpiryPage(records_examined=1, records_assigned=1),
        ]
    )

    result = TerminalOperationalRecordCleanup(
        inventory=inventory,
        expiry_store=expiry,
        checkpoints=MemoryCheckpointStore(),
        clock=lambda: NOW,
    ).sweep()

    assert result.scan_complete is True
    assert result.terminal_jobs_completed == 1
    assert result.assignment_pages == 2
    assert result.records_examined_for_expiry == 4
    assert result.records_assigned_expiry == 4
    assert len(expiry.calls) == 2
    expected_expiry = int((terminal.updated_at + timedelta(days=90)).timestamp())
    assert {call["expires_at_epoch_seconds"] for call in expiry.calls} == {expected_expiry}
    assert expiry.calls[0]["cursor"] is None
    assert expiry.calls[1]["cursor"] == "assignment.owner"
    assert all(call["limit"] == 24 for call in expiry.calls)


def test_trusted_inventory_time_prevents_a_fast_clock_from_shortening_retention() -> None:
    observed_at = NOW - timedelta(minutes=4)
    terminal = _job(
        ControlJobState.CANCELLED,
        job_id="clock_safe_job",
        updated_at=NOW - timedelta(days=90, minutes=-2),
    )
    expiry = QueueExpiry([])

    result = TerminalOperationalRecordCleanup(
        inventory=QueueInventory([_page(terminal, cursor=None, observed_at=observed_at)]),
        expiry_store=expiry,
        checkpoints=MemoryCheckpointStore(),
        clock=lambda: NOW,
    ).sweep()

    assert result.recent_terminal_jobs_preserved == 1
    assert expiry.calls == []


def test_authority_change_during_assignment_preserves_records_and_advances() -> None:
    authority = TerminalJobAuthority(
        job_id="racing_job",
        owner_id=OWNER,
        state=ControlJobState.CANCELLED,
        record_version=4,
        event_sequence=5,
        terminal_updated_at=NOW - timedelta(days=100),
    )
    checkpoint = OperationalCleanupCheckpoint(
        revision=3,
        active_authority=authority,
    )
    store = MemoryCheckpointStore(checkpoint)
    expiry = QueueExpiry([OperationalCleanupAuthorityChangedError("private racing job changed")])

    result = TerminalOperationalRecordCleanup(
        inventory=QueueInventory([]),
        expiry_store=expiry,
        checkpoints=store,
        clock=lambda: NOW,
    ).sweep()

    assert result.authority_changes_preserved == 1
    assert result.records_assigned_expiry == 0
    assert result.scan_complete is True
    assert store.checkpoint.active_authority is None


def test_checkpoint_cas_failure_is_stable_and_does_not_leak_authority() -> None:
    secret_job = "sensitive_job_identity"
    checkpoints = MemoryCheckpointStore()
    checkpoints.fail_save = True
    cleanup = TerminalOperationalRecordCleanup(
        inventory=QueueInventory(
            [
                _page(
                    _job(
                        ControlJobState.AWAITING_APPROVAL,
                        job_id=secret_job,
                        updated_at=NOW - timedelta(days=1),
                    ),
                    cursor=None,
                )
            ]
        ),
        expiry_store=QueueExpiry([]),
        checkpoints=checkpoints,
        clock=lambda: NOW,
    )

    with pytest.raises(OperationalCleanupDependencyUnavailableError) as captured:
        cleanup.sweep()

    assert str(captured.value) == "Operational cleanup dependency is unavailable"
    assert secret_job not in str(captured.value)
    assert captured.value.__cause__ is None


def test_sanitized_result_contains_no_job_owner_or_payload_authority() -> None:
    result = TerminalOperationalRecordCleanup(
        inventory=QueueInventory(
            [
                _page(
                    _job(
                        ControlJobState.APPROVED,
                        job_id="never_in_result",
                        updated_at=NOW - timedelta(days=100),
                    ),
                    cursor=None,
                )
            ]
        ),
        expiry_store=QueueExpiry([]),
        checkpoints=MemoryCheckpointStore(),
        clock=lambda: NOW,
    ).sweep()

    serialized = result.model_dump_json().casefold()
    assert "never_in_result" not in serialized
    assert OWNER not in serialized
    assert "payload" not in serialized


def test_assignment_pagination_is_bounded_per_run_and_resumes_from_checkpoint() -> None:
    terminal = _job(
        ControlJobState.FAILED_TERMINAL,
        job_id="large_partition",
        updated_at=NOW - timedelta(days=100),
    )
    inventory = QueueInventory([_page(terminal, cursor="scan.resume")])
    expiry = QueueExpiry(
        [
            OperationalExpiryPage(
                records_examined=24,
                records_assigned=24,
                next_cursor="assignment.next",
            ),
            OperationalExpiryPage(records_examined=2, records_assigned=2),
        ]
    )
    checkpoints = MemoryCheckpointStore()
    cleanup = TerminalOperationalRecordCleanup(
        inventory=inventory,
        expiry_store=expiry,
        checkpoints=checkpoints,
        clock=lambda: NOW,
        max_assignment_pages_per_run=1,
    )

    first = cleanup.sweep()
    second = cleanup.sweep()

    assert first.scan_complete is False
    assert first.assignment_pages == 1
    assert checkpoints.checkpoint.scan_cursor == "scan.resume"
    assert second.terminal_jobs_completed == 1
    assert expiry.calls[1]["cursor"] == "assignment.next"
