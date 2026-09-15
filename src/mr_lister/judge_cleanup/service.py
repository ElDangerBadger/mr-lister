"""Bounded sweep, immutable deadlines, durable leases and audited retry decisions."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol
from uuid import uuid4

from mr_lister.judge_cleanup.models import (
    TERMINAL,
    CleanupConfig,
    CleanupRecord,
    PublicationEvidence,
)


class CleanupRunError(RuntimeError):
    """Sanitized failure surfaced to the scheduler and CloudWatch error alarm."""


class ProviderMismatchError(ValueError):
    pass


@dataclass(frozen=True)
class DiscoveryPage:
    job_ids: tuple[str, ...]
    next_cursor: str | None
    invalid_records: int = 0


class Source(Protocol):
    def discover(self, cursor: str | None, limit: int) -> DiscoveryPage: ...
    def load(self, job_id: str) -> PublicationEvidence: ...


class Store(Protocol):
    def get(self, job_id: str) -> CleanupRecord | None: ...
    def register(self, record: CleanupRecord) -> bool: ...
    def compare_and_swap(self, old: CleanupRecord, new: CleanupRecord) -> bool: ...
    def due(self, now: datetime, limit: int) -> tuple[CleanupRecord, ...]: ...
    def cursor(self) -> str | None: ...
    def advance_cursor(self, old: str | None, new: str | None) -> None: ...
    def discovery_failure(self, job_id: str | None, now: datetime) -> None: ...


class Provider(Protocol):
    def present(self, evidence: PublicationEvidence) -> bool: ...
    def delete(self, evidence: PublicationEvidence) -> None: ...


class JudgeCleanupService:
    def __init__(
        self,
        *,
        config: CleanupConfig,
        source: Source,
        store: Store,
        provider: Provider,
        clock: Callable[[], datetime] | None = None,
        remaining_seconds: Callable[[], float] | None = None,
    ) -> None:
        self.config = CleanupConfig.model_validate_json(config.model_dump_json())
        self.source, self.store, self.provider = source, store, provider
        self.clock = clock or (lambda: datetime.now(UTC))
        self.remaining = remaining_seconds or (lambda: 50.0)

    def sweep(self) -> dict[str, int | bool]:
        counts: dict[str, int | bool] = {
            "dry_run": self.config.dry_run,
            "registered": 0,
            "due": 0,
            "completed": 0,
            "failures": 0,
        }
        # Process existing due records first, so a busy discovery page cannot starve cleanup.
        for record in self.store.due(self.clock(), self.config.max_jobs_per_run):
            if self.remaining() < 25:
                break
            counts["due"] += 1
            if self.config.dry_run:
                continue
            result = self.process(record)
            counts["completed"] += int(result in {"deleted", "absent"})
            counts["failures"] += int(result in {"retry", "blocked", "exhausted"})
        if self.remaining() >= 15:
            cursor = self.store.cursor()
            page = self.source.discover(cursor, self.config.page_size)
            if page.invalid_records:
                self.store.discovery_failure(None, self.clock())
                counts["failures"] += page.invalid_records
            complete_page = True
            for job_id in page.job_ids:
                if self.remaining() < 15:
                    complete_page = False
                    break
                existing = self.store.get(job_id)
                if existing is not None:
                    self._require_campaign(existing)
                    continue
                try:
                    evidence = self._load(job_id)
                except Exception:
                    self.store.discovery_failure(job_id, self.clock())
                    counts["failures"] += 1
                    continue
                # Existing judge drafts newly published in this campaign are eligible.
                if evidence.snapshot.requested_at < self.config.campaign_started_at:
                    continue
                try:
                    evidence.require_campaign(self.config)
                except ValueError:
                    self.store.discovery_failure(job_id, self.clock())
                    counts["failures"] += 1
                    continue
                now = self.clock()
                record = CleanupRecord(
                    campaign_id=self.config.campaign_id,
                    campaign_fingerprint=self.config.campaign_fingerprint,
                    evidence=evidence,
                    evidence_fingerprint=evidence.fingerprint,
                    deadline=evidence.deadline,
                    next_attempt_at=evidence.deadline,
                    updated_at=now,
                )
                counts["registered"] += int(self.store.register(record))
            if complete_page:
                self.store.advance_cursor(cursor, page.next_cursor)
        if counts["failures"]:
            raise CleanupRunError("Judge cleanup recorded a failure; inspect its durable audit")
        return counts

    def _load(self, job_id: str) -> PublicationEvidence:
        evidence = self.source.load(job_id)
        # Revalidate even when a dependency returns a model_copy/model_construct instance.
        return PublicationEvidence.model_validate_json(evidence.model_dump_json())

    def _require_campaign(self, record: CleanupRecord) -> None:
        if (
            record.campaign_id != self.config.campaign_id
            or record.campaign_fingerprint != self.config.campaign_fingerprint
        ):
            raise CleanupRunError("Cleanup campaign binding changed")
        record.evidence.require_campaign(self.config)

    def process(self, record: CleanupRecord) -> str:
        record = CleanupRecord.model_validate_json(record.model_dump_json())
        self._require_campaign(record)
        now = self.clock()
        if self.config.dry_run or record.status in TERMINAL or now < record.next_attempt_at:
            return "skipped"
        if record.status == "leased" and now < record.lease_until:
            return "skipped"
        if record.attempts == 8:
            updated = self._change(
                record,
                status="exhausted",
                last_outcome="retry_limit",
                lease_id=None,
                lease_until=None,
                updated_at=now,
            )
            return "exhausted" if self.store.compare_and_swap(record, updated) else "skipped"
        lease_until = now + timedelta(seconds=90)
        leased = self._change(
            record,
            status="leased",
            attempts=record.attempts + 1,
            lease_id=uuid4().hex,
            lease_until=lease_until,
            next_attempt_at=lease_until,
            last_outcome="claimed",
            updated_at=now,
        )
        if not self.store.compare_and_swap(record, leased):
            return "skipped"
        status, outcome = "retry", "dependency_unavailable"
        try:
            if self._load(record.evidence.job.job_id) != record.evidence:
                status, outcome = "blocked", "source_changed"
            elif not self.provider.present(record.evidence):
                status, outcome = "absent", "provider_absent"
            else:
                # Re-read immutable source and persisted lease immediately before mutation.
                if self._load(record.evidence.job.job_id) != record.evidence:
                    status, outcome = "blocked", "source_changed"
                elif (
                    self.clock() + timedelta(seconds=25) >= lease_until
                    or self.remaining() < 25
                    or self.store.get(record.evidence.job.job_id) != leased
                ):
                    status, outcome = "retry", "dependency_unavailable"
                else:
                    self.provider.delete(record.evidence)
                    if not self.provider.present(record.evidence):
                        status, outcome = "deleted", "provider_deleted"
        except ProviderMismatchError:
            status, outcome = "blocked", "provider_mismatch"
        except ValueError:
            status, outcome = "blocked", "source_changed"
        except Exception:
            # Never persist provider bodies, credentials, URLs or dependency exception text.
            status, outcome = "retry", "dependency_unavailable"
        now = self.clock()
        if status == "retry" and leased.attempts == 8:
            status, outcome = "exhausted", "retry_limit"
        updated = self._change(
            leased,
            status=status,
            last_outcome=outcome,
            lease_id=None,
            lease_until=None,
            updated_at=now,
            next_attempt_at=max(
                record.deadline, now + timedelta(seconds=min(900, 30 * 2 ** (leased.attempts - 1)))
            ),
        )
        if not self.store.compare_and_swap(leased, updated):
            raise CleanupRunError("Cleanup completion lost its persisted lease")
        return status

    @staticmethod
    def _change(record: CleanupRecord, **changes: object) -> CleanupRecord:
        return CleanupRecord.model_validate(
            {**record.model_dump(), **changes, "version": record.version + 1}
        )
