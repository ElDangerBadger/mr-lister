"""Race-safe expiry assignment for terminal Phase 6 operational records.

This boundary never deletes a row.  It assigns the table's ``expires_at`` TTL attribute only
after an exact, strongly observed job has remained overall-terminal for the full retention
window.  ``APPROVED`` is deliberately preserved because it is Phase 7 publication authority.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from typing import Annotated, Literal, Protocol

from pydantic import AwareDatetime, Field, StringConstraints, model_validator

from mr_lister.contracts import ContractModel
from mr_lister.control.models import ControlJobRecord, ControlJobState

OPERATIONAL_CLEANUP_CONTRACT_VERSION = "1.0.0"
OperationalCleanupContractVersion = Literal["1.0.0"]

DEFAULT_TERMINAL_OPERATIONAL_RETENTION = timedelta(days=90)
DEFAULT_OPERATIONAL_CLEANUP_CLOCK_SKEW = timedelta(minutes=5)
MAX_OPERATIONAL_SCAN_PAGES = 4_096
MAX_OPERATIONAL_ASSIGNMENT_PAGES = 4_096

_SAFE_CURSOR = re.compile(r"^[\x21-\x7e]{1,4096}$")
_DIGEST = re.compile(r"^[a-f0-9]{64}$")
_ELIGIBLE_STATES = frozenset({ControlJobState.CANCELLED, ControlJobState.FAILED_TERMINAL})


class OperationalCleanupError(RuntimeError):
    """Stable, identifier-free operational-cleanup failure."""


class OperationalCleanupBoundaryInvalidError(OperationalCleanupError):
    """A dependency returned data outside the closed cleanup contract."""


class OperationalCleanupDependencyUnavailableError(OperationalCleanupError):
    """A required inventory, checkpoint, or expiry operation failed."""


class OperationalCleanupAuthorityChangedError(OperationalCleanupError):
    """The exact terminal job authority changed before expiry assignment."""


class OperationalCleanupModel(ContractModel):
    contract_version: OperationalCleanupContractVersion = OPERATIONAL_CLEANUP_CONTRACT_VERSION


class OperationalJobSearchPage(OperationalCleanupModel):
    """A bounded strong table scan ending at the first control job, if any."""

    observed_at: AwareDatetime
    job: ControlJobRecord | None = None
    records_scanned: int = Field(ge=0)
    next_cursor: Annotated[str, StringConstraints(min_length=1, max_length=4096)] | None = None

    @model_validator(mode="after")
    def progress_is_coherent(self) -> OperationalJobSearchPage:
        if self.records_scanned == 0 and self.next_cursor is not None:
            raise ValueError("An empty operational scan cannot continue")
        if self.job is not None and self.records_scanned == 0:
            raise ValueError("An observed job requires scan progress")
        return self


class TerminalJobAuthority(OperationalCleanupModel):
    """Minimal immutable authority needed to condition every TTL write."""

    job_id: Annotated[
        str,
        StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$"),
    ]
    owner_id: Annotated[str, StringConstraints(pattern=r"^[a-f0-9]{64}$")]
    state: Literal[ControlJobState.CANCELLED, ControlJobState.FAILED_TERMINAL]
    record_version: int = Field(ge=0)
    event_sequence: int = Field(ge=0)
    terminal_updated_at: AwareDatetime


class OperationalExpiryPage(OperationalCleanupModel):
    """One bounded, authority-conditioned expiry-assignment page."""

    records_examined: int = Field(ge=0)
    records_assigned: int = Field(ge=0)
    next_cursor: Annotated[str, StringConstraints(min_length=1, max_length=4096)] | None = None

    @model_validator(mode="after")
    def assignments_are_bounded_by_examination(self) -> OperationalExpiryPage:
        if self.records_assigned > self.records_examined:
            raise ValueError("Operational expiry counters are inconsistent")
        # The owner-receipt phase may inspect an empty final page. It is still a valid end.
        if self.records_examined == 0 and self.next_cursor is not None:
            raise ValueError("Operational expiry continuation made no progress")
        return self


class OperationalCleanupCheckpoint(OperationalCleanupModel):
    """CAS-protected scan and per-job assignment continuation."""

    revision: int = Field(default=0, ge=0)
    scan_cursor: Annotated[str, StringConstraints(min_length=1, max_length=4096)] | None = None
    scan_cursor_digests: tuple[
        Annotated[str, StringConstraints(pattern=r"^[a-f0-9]{64}$")], ...
    ] = Field(default=(), max_length=MAX_OPERATIONAL_SCAN_PAGES)
    scan_pages: int = Field(default=0, ge=0)
    scan_items: int = Field(default=0, ge=0)
    active_authority: TerminalJobAuthority | None = None
    resume_cursor: Annotated[str, StringConstraints(min_length=1, max_length=4096)] | None = None
    assignment_cursor: Annotated[str, StringConstraints(min_length=1, max_length=4096)] | None = (
        None
    )
    assignment_cursor_digests: tuple[
        Annotated[str, StringConstraints(pattern=r"^[a-f0-9]{64}$")], ...
    ] = Field(default=(), max_length=MAX_OPERATIONAL_ASSIGNMENT_PAGES)
    assignment_pages: int = Field(default=0, ge=0)
    assignment_items: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def continuation_is_coherent(self) -> OperationalCleanupCheckpoint:
        if self.scan_cursor is None:
            if self.scan_cursor_digests or self.scan_pages or self.scan_items:
                if self.active_authority is None:
                    raise ValueError("A completed operational scan cannot retain progress")
        else:
            if _cursor_digest(self.scan_cursor) not in self.scan_cursor_digests:
                raise ValueError("The scan cursor must be present in cycle-detection state")
        if len(set(self.scan_cursor_digests)) != len(self.scan_cursor_digests):
            raise ValueError("Operational scan cursor history must be unique")
        if self.active_authority is None:
            if (
                self.resume_cursor is not None
                or self.assignment_cursor is not None
                or self.assignment_cursor_digests
                or self.assignment_pages
                or self.assignment_items
            ):
                raise ValueError("Idle cleanup checkpoints cannot retain assignment state")
        elif self.assignment_cursor is not None and (
            _cursor_digest(self.assignment_cursor) not in self.assignment_cursor_digests
        ):
            raise ValueError("The assignment cursor must be present in cycle-detection state")
        if len(set(self.assignment_cursor_digests)) != len(self.assignment_cursor_digests):
            raise ValueError("Operational assignment cursor history must be unique")
        return self


class OperationalCleanupResult(OperationalCleanupModel):
    """Sanitized counter-only evidence; no job, owner, row, or payload authority."""

    scan_pages: int = Field(ge=0)
    records_scanned: int = Field(ge=0)
    jobs_observed: int = Field(ge=0)
    approved_jobs_preserved: int = Field(ge=0)
    nonterminal_jobs_preserved: int = Field(ge=0)
    recent_terminal_jobs_preserved: int = Field(ge=0)
    authority_changes_preserved: int = Field(ge=0)
    terminal_jobs_completed: int = Field(ge=0)
    assignment_pages: int = Field(ge=0)
    records_examined_for_expiry: int = Field(ge=0)
    records_assigned_expiry: int = Field(ge=0)
    scan_complete: bool

    @model_validator(mode="after")
    def observed_jobs_are_classified(self) -> OperationalCleanupResult:
        classified = (
            self.approved_jobs_preserved
            + self.nonterminal_jobs_preserved
            + self.recent_terminal_jobs_preserved
        )
        if classified > self.jobs_observed:
            raise ValueError("Operational cleanup result counters are inconsistent")
        if self.records_assigned_expiry > self.records_examined_for_expiry:
            raise ValueError("Operational cleanup result counters are inconsistent")
        return self


class OperationalJobInventory(Protocol):
    """Strongly scan only projected operational metadata, never object or provider data."""

    def search_next_job(
        self,
        *,
        cursor: str | None,
        limit: int,
    ) -> OperationalJobSearchPage: ...


class TerminalOperationalExpiryStore(Protocol):
    """Conditionally assign TTL to one exact terminal job's operational rows."""

    def assign_terminal_expiry(
        self,
        *,
        authority: TerminalJobAuthority,
        expires_at_epoch_seconds: int,
        cursor: str | None,
        limit: int,
    ) -> OperationalExpiryPage: ...


class OperationalCleanupCheckpointStore(Protocol):
    def load_checkpoint(self) -> OperationalCleanupCheckpoint: ...

    def save_checkpoint(
        self,
        *,
        expected: OperationalCleanupCheckpoint,
        updated: OperationalCleanupCheckpoint,
    ) -> None: ...


class TerminalOperationalRecordCleanup:
    """Bounded scan plus CAS/transactional TTL assignment for old terminal jobs."""

    def __init__(
        self,
        *,
        inventory: OperationalJobInventory,
        expiry_store: TerminalOperationalExpiryStore,
        checkpoints: OperationalCleanupCheckpointStore,
        clock: Callable[[], datetime] | None = None,
        terminal_retention: timedelta = DEFAULT_TERMINAL_OPERATIONAL_RETENTION,
        max_clock_skew: timedelta = DEFAULT_OPERATIONAL_CLEANUP_CLOCK_SKEW,
        scan_page_size: int = 100,
        assignment_page_size: int = 24,
        max_scan_pages_per_run: int = 5,
        max_assignment_pages_per_run: int = 20,
        max_scan_pages_per_cycle: int = MAX_OPERATIONAL_SCAN_PAGES,
        max_scan_items_per_cycle: int = 1_000_000,
        max_assignment_pages_per_job: int = MAX_OPERATIONAL_ASSIGNMENT_PAGES,
        max_assignment_items_per_job: int = 100_000,
    ) -> None:
        if not timedelta(0) < terminal_retention <= timedelta(days=365):
            raise ValueError("Operational retention is outside its safety bound")
        if not timedelta(0) < max_clock_skew <= timedelta(hours=1):
            raise ValueError("Operational cleanup clock-skew bound is invalid")
        if not 1 <= scan_page_size <= 1_000:
            raise ValueError("Operational scan page size is outside its safety bound")
        if not 1 <= assignment_page_size <= 24:
            raise ValueError("Operational assignment page size is outside its safety bound")
        if not 1 <= max_scan_pages_per_run <= 100:
            raise ValueError("Operational scan run bound is invalid")
        if not 1 <= max_assignment_pages_per_run <= 100:
            raise ValueError("Operational assignment run bound is invalid")
        if not max_scan_pages_per_run <= max_scan_pages_per_cycle <= MAX_OPERATIONAL_SCAN_PAGES:
            raise ValueError("Operational scan cycle bound is invalid")
        if not scan_page_size <= max_scan_items_per_cycle <= 1_000_000:
            raise ValueError("Operational scan item bound is invalid")
        if not (
            max_assignment_pages_per_run
            <= max_assignment_pages_per_job
            <= MAX_OPERATIONAL_ASSIGNMENT_PAGES
        ):
            raise ValueError("Operational assignment page bound is invalid")
        if not assignment_page_size <= max_assignment_items_per_job <= 100_000:
            raise ValueError("Operational assignment item bound is invalid")
        self._inventory = inventory
        self._expiry_store = expiry_store
        self._checkpoints = checkpoints
        self._clock = clock or (lambda: datetime.now(UTC))
        self._terminal_retention = terminal_retention
        self._max_clock_skew = max_clock_skew
        self._scan_page_size = scan_page_size
        self._assignment_page_size = assignment_page_size
        self._max_scan_pages_per_run = max_scan_pages_per_run
        self._max_assignment_pages_per_run = max_assignment_pages_per_run
        self._max_scan_pages_per_cycle = max_scan_pages_per_cycle
        self._max_scan_items_per_cycle = max_scan_items_per_cycle
        self._max_assignment_pages_per_job = max_assignment_pages_per_job
        self._max_assignment_items_per_job = max_assignment_items_per_job

    def sweep(self) -> OperationalCleanupResult:
        now = self._now()
        checkpoint = self._load_checkpoint()
        scan_pages = records_scanned = jobs_observed = 0
        approved = nonterminal = recent_terminal = authority_changes = 0
        terminal_completed = assignment_pages = assignment_examined = assignment_assigned = 0
        scan_complete = False

        while (
            scan_pages < self._max_scan_pages_per_run
            and assignment_pages < self._max_assignment_pages_per_run
        ):
            if checkpoint.active_authority is not None:
                try:
                    page = self._assign_page(checkpoint)
                except OperationalCleanupAuthorityChangedError:
                    authority_changes += 1
                    checkpoint, scan_complete = self._finish_active(checkpoint)
                    if scan_complete:
                        break
                    continue
                assignment_pages += 1
                assignment_examined += page.records_examined
                assignment_assigned += page.records_assigned
                checkpoint, completed, scan_complete = self._advance_assignment(
                    checkpoint,
                    page,
                )
                if completed:
                    terminal_completed += 1
                if scan_complete:
                    break
                continue

            page = self._search_page(checkpoint)
            scan_pages += 1
            records_scanned += page.records_scanned
            job = page.job
            next_pages = checkpoint.scan_pages + 1
            next_items = checkpoint.scan_items + page.records_scanned
            if (
                next_pages > self._max_scan_pages_per_cycle
                or next_items > self._max_scan_items_per_cycle
            ):
                raise OperationalCleanupBoundaryInvalidError(
                    "Operational cleanup scan exceeded its safety bound"
                )
            if job is None:
                checkpoint, scan_complete = self._advance_scan(
                    checkpoint,
                    next_cursor=page.next_cursor,
                    scan_pages=next_pages,
                    scan_items=next_items,
                )
                if scan_complete:
                    break
                continue

            jobs_observed += 1
            effective_now = min(now, page.observed_at)
            classification = self._classify(job, now=effective_now)
            if classification == "approved":
                approved += 1
            elif classification == "nonterminal":
                nonterminal += 1
            elif classification == "recent_terminal":
                recent_terminal += 1
            else:
                authority = TerminalJobAuthority(
                    job_id=job.job_id,
                    owner_id=job.owner_id,
                    state=job.state,
                    record_version=job.record_version,
                    event_sequence=job.event_sequence,
                    terminal_updated_at=job.updated_at,
                )
                checkpoint = self._start_assignment(
                    checkpoint,
                    authority=authority,
                    resume_cursor=page.next_cursor,
                    scan_pages=next_pages,
                    scan_items=next_items,
                )
                continue
            checkpoint, scan_complete = self._advance_scan(
                checkpoint,
                next_cursor=page.next_cursor,
                scan_pages=next_pages,
                scan_items=next_items,
            )
            if scan_complete:
                break

        return OperationalCleanupResult(
            scan_pages=scan_pages,
            records_scanned=records_scanned,
            jobs_observed=jobs_observed,
            approved_jobs_preserved=approved,
            nonterminal_jobs_preserved=nonterminal,
            recent_terminal_jobs_preserved=recent_terminal,
            authority_changes_preserved=authority_changes,
            terminal_jobs_completed=terminal_completed,
            assignment_pages=assignment_pages,
            records_examined_for_expiry=assignment_examined,
            records_assigned_expiry=assignment_assigned,
            scan_complete=scan_complete,
        )

    def _now(self) -> datetime:
        try:
            now = self._clock()
        except Exception:
            raise OperationalCleanupDependencyUnavailableError(
                "Operational cleanup dependency is unavailable"
            ) from None
        if not isinstance(now, datetime) or now.utcoffset() is None:
            raise OperationalCleanupBoundaryInvalidError(
                "Operational cleanup clock response is invalid"
            )
        return now.astimezone(UTC)

    def _load_checkpoint(self) -> OperationalCleanupCheckpoint:
        try:
            checkpoint = self._checkpoints.load_checkpoint()
        except Exception:
            raise OperationalCleanupDependencyUnavailableError(
                "Operational cleanup dependency is unavailable"
            ) from None
        return self._strict_checkpoint(checkpoint)

    def _search_page(
        self,
        checkpoint: OperationalCleanupCheckpoint,
    ) -> OperationalJobSearchPage:
        try:
            page = self._inventory.search_next_job(
                cursor=checkpoint.scan_cursor,
                limit=self._scan_page_size,
            )
        except Exception:
            raise OperationalCleanupDependencyUnavailableError(
                "Operational cleanup dependency is unavailable"
            ) from None
        if not isinstance(page, OperationalJobSearchPage):
            raise OperationalCleanupBoundaryInvalidError(
                "Operational cleanup inventory response is invalid"
            )
        try:
            page = OperationalJobSearchPage.model_validate(
                page.model_dump(mode="python"), strict=True
            )
        except Exception:
            raise OperationalCleanupBoundaryInvalidError(
                "Operational cleanup inventory response is invalid"
            ) from None
        now = self._now()
        if (
            page.observed_at.utcoffset() != UTC.utcoffset(page.observed_at)
            or abs(page.observed_at - now) > self._max_clock_skew
            or page.records_scanned > self._scan_page_size
        ):
            raise OperationalCleanupBoundaryInvalidError(
                "Operational cleanup inventory response is invalid"
            )
        self._validate_next_cursor(
            page.next_cursor,
            current=checkpoint.scan_cursor,
            seen=checkpoint.scan_cursor_digests,
            label="scan",
        )
        if page.job is not None:
            self._strict_job(page.job, observed_at=page.observed_at)
        return page

    def _assign_page(
        self,
        checkpoint: OperationalCleanupCheckpoint,
    ) -> OperationalExpiryPage:
        authority = checkpoint.active_authority
        assert authority is not None
        expiry = int((authority.terminal_updated_at + self._terminal_retention).timestamp())
        try:
            page = self._expiry_store.assign_terminal_expiry(
                authority=authority,
                expires_at_epoch_seconds=expiry,
                cursor=checkpoint.assignment_cursor,
                limit=self._assignment_page_size,
            )
        except OperationalCleanupAuthorityChangedError:
            raise
        except Exception:
            raise OperationalCleanupDependencyUnavailableError(
                "Operational cleanup dependency is unavailable"
            ) from None
        if not isinstance(page, OperationalExpiryPage):
            raise OperationalCleanupBoundaryInvalidError(
                "Operational cleanup expiry response is invalid"
            )
        try:
            page = OperationalExpiryPage.model_validate(page.model_dump(mode="python"), strict=True)
        except Exception:
            raise OperationalCleanupBoundaryInvalidError(
                "Operational cleanup expiry response is invalid"
            ) from None
        if page.records_examined > self._assignment_page_size:
            raise OperationalCleanupBoundaryInvalidError(
                "Operational cleanup expiry response exceeded its page bound"
            )
        self._validate_next_cursor(
            page.next_cursor,
            current=checkpoint.assignment_cursor,
            seen=checkpoint.assignment_cursor_digests,
            label="assignment",
        )
        return page

    def _classify(
        self,
        job: ControlJobRecord,
        *,
        now: datetime,
    ) -> Literal["approved", "nonterminal", "recent_terminal", "eligible"]:
        job = self._strict_job(job, observed_at=now)
        if job.state is ControlJobState.APPROVED:
            return "approved"
        if job.state not in _ELIGIBLE_STATES:
            return "nonterminal"
        if now - job.updated_at < self._terminal_retention:
            return "recent_terminal"
        return "eligible"

    @staticmethod
    def _strict_job(job: object, *, observed_at: datetime) -> ControlJobRecord:
        if not isinstance(job, ControlJobRecord):
            raise OperationalCleanupBoundaryInvalidError(
                "Operational cleanup job authority is invalid"
            )
        try:
            exact = ControlJobRecord.model_validate(job.model_dump(mode="python"), strict=True)
        except Exception:
            raise OperationalCleanupBoundaryInvalidError(
                "Operational cleanup job authority is invalid"
            ) from None
        if exact.updated_at.utcoffset() != UTC.utcoffset(exact.updated_at):
            raise OperationalCleanupBoundaryInvalidError(
                "Operational cleanup job authority is invalid"
            )
        if exact.updated_at > observed_at:
            raise OperationalCleanupBoundaryInvalidError(
                "Operational cleanup job authority is invalid"
            )
        return exact

    def _advance_scan(
        self,
        current: OperationalCleanupCheckpoint,
        *,
        next_cursor: str | None,
        scan_pages: int,
        scan_items: int,
    ) -> tuple[OperationalCleanupCheckpoint, bool]:
        if next_cursor is None:
            updated = OperationalCleanupCheckpoint(revision=current.revision + 1)
            complete = True
        else:
            updated = OperationalCleanupCheckpoint(
                revision=current.revision + 1,
                scan_cursor=next_cursor,
                scan_cursor_digests=(
                    *current.scan_cursor_digests,
                    _cursor_digest(next_cursor),
                ),
                scan_pages=scan_pages,
                scan_items=scan_items,
            )
            complete = False
        self._save_checkpoint(current, updated)
        return updated, complete

    def _start_assignment(
        self,
        current: OperationalCleanupCheckpoint,
        *,
        authority: TerminalJobAuthority,
        resume_cursor: str | None,
        scan_pages: int,
        scan_items: int,
    ) -> OperationalCleanupCheckpoint:
        cursor_digests = current.scan_cursor_digests
        if resume_cursor is not None:
            cursor_digests = (*cursor_digests, _cursor_digest(resume_cursor))
        updated = OperationalCleanupCheckpoint(
            revision=current.revision + 1,
            scan_cursor=resume_cursor,
            scan_cursor_digests=cursor_digests,
            scan_pages=scan_pages,
            scan_items=scan_items,
            active_authority=authority,
            resume_cursor=resume_cursor,
        )
        self._save_checkpoint(current, updated)
        return updated

    def _advance_assignment(
        self,
        current: OperationalCleanupCheckpoint,
        page: OperationalExpiryPage,
    ) -> tuple[OperationalCleanupCheckpoint, bool, bool]:
        next_pages = current.assignment_pages + 1
        next_items = current.assignment_items + page.records_examined
        if (
            next_pages > self._max_assignment_pages_per_job
            or next_items > self._max_assignment_items_per_job
        ):
            raise OperationalCleanupBoundaryInvalidError(
                "Operational cleanup assignment exceeded its safety bound"
            )
        if page.next_cursor is None:
            updated, scan_complete = self._finish_active(current)
            return updated, True, scan_complete
        updated = OperationalCleanupCheckpoint(
            revision=current.revision + 1,
            scan_cursor=current.scan_cursor,
            scan_cursor_digests=current.scan_cursor_digests,
            scan_pages=current.scan_pages,
            scan_items=current.scan_items,
            active_authority=current.active_authority,
            resume_cursor=current.resume_cursor,
            assignment_cursor=page.next_cursor,
            assignment_cursor_digests=(
                *current.assignment_cursor_digests,
                _cursor_digest(page.next_cursor),
            ),
            assignment_pages=next_pages,
            assignment_items=next_items,
        )
        self._save_checkpoint(current, updated)
        return updated, False, False

    def _finish_active(
        self,
        current: OperationalCleanupCheckpoint,
    ) -> tuple[OperationalCleanupCheckpoint, bool]:
        if current.resume_cursor is None:
            updated = OperationalCleanupCheckpoint(revision=current.revision + 1)
            complete = True
        else:
            updated = OperationalCleanupCheckpoint(
                revision=current.revision + 1,
                scan_cursor=current.resume_cursor,
                scan_cursor_digests=current.scan_cursor_digests,
                scan_pages=current.scan_pages,
                scan_items=current.scan_items,
            )
            complete = False
        self._save_checkpoint(current, updated)
        return updated, complete

    def _save_checkpoint(
        self,
        current: OperationalCleanupCheckpoint,
        updated: OperationalCleanupCheckpoint,
    ) -> None:
        try:
            self._checkpoints.save_checkpoint(expected=current, updated=updated)
        except Exception:
            raise OperationalCleanupDependencyUnavailableError(
                "Operational cleanup dependency is unavailable"
            ) from None

    @staticmethod
    def _strict_checkpoint(value: object) -> OperationalCleanupCheckpoint:
        if not isinstance(value, OperationalCleanupCheckpoint):
            raise OperationalCleanupBoundaryInvalidError(
                "Operational cleanup checkpoint is invalid"
            )
        try:
            checkpoint = OperationalCleanupCheckpoint.model_validate(
                value.model_dump(mode="python"), strict=True
            )
        except Exception:
            raise OperationalCleanupBoundaryInvalidError(
                "Operational cleanup checkpoint is invalid"
            ) from None
        if any(_DIGEST.fullmatch(item) is None for item in checkpoint.scan_cursor_digests):
            raise OperationalCleanupBoundaryInvalidError(
                "Operational cleanup checkpoint is invalid"
            )
        return checkpoint

    @staticmethod
    def _validate_next_cursor(
        cursor: str | None,
        *,
        current: str | None,
        seen: tuple[str, ...],
        label: str,
    ) -> None:
        if cursor is None:
            return
        if _SAFE_CURSOR.fullmatch(cursor) is None:
            raise OperationalCleanupBoundaryInvalidError(
                f"Operational cleanup {label} cursor is invalid"
            )
        digest = _cursor_digest(cursor)
        if cursor == current or digest in seen:
            raise OperationalCleanupBoundaryInvalidError(
                f"Operational cleanup {label} pagination cycled"
            )


def _cursor_digest(cursor: str) -> str:
    return sha256(cursor.encode("utf-8")).hexdigest()


__all__ = [
    "DEFAULT_OPERATIONAL_CLEANUP_CLOCK_SKEW",
    "DEFAULT_TERMINAL_OPERATIONAL_RETENTION",
    "MAX_OPERATIONAL_ASSIGNMENT_PAGES",
    "MAX_OPERATIONAL_SCAN_PAGES",
    "OPERATIONAL_CLEANUP_CONTRACT_VERSION",
    "OperationalCleanupAuthorityChangedError",
    "OperationalCleanupBoundaryInvalidError",
    "OperationalCleanupCheckpoint",
    "OperationalCleanupCheckpointStore",
    "OperationalCleanupDependencyUnavailableError",
    "OperationalCleanupError",
    "OperationalCleanupResult",
    "OperationalExpiryPage",
    "OperationalJobInventory",
    "OperationalJobSearchPage",
    "TerminalJobAuthority",
    "TerminalOperationalExpiryStore",
    "TerminalOperationalRecordCleanup",
]
