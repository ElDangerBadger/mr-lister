"""Capability-free Phase 7 operational-retention authority and orchestration.

The terminal publication settlement owns the exact +30/+90 timestamps.  This module neither
deletes rows nor changes provider data: it validates one terminal graph, derives the immutable TTL
plan, and delegates bounded assignment to an injected store.  A completion marker is returned only
after the store proves every row carries the same exact operational expiry.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Protocol

from pydantic import ValidationError, model_validator

from mr_lister.control.models import ControlJobRecord, ControlJobState
from mr_lister.control.publication_retention import (
    PublicationRetentionCompletionAuthority,
    validate_publication_retention_completion,
)
from mr_lister.publication.contract import PublicationState
from mr_lister.publication.execution_models import PublicationExecutionAuthority
from mr_lister.publication.fingerprints import canonical_fingerprint
from mr_lister.publication.models import Fingerprint, PublicationModel
from mr_lister.publication.retention_locator import (
    PublicationRequestReceiptLocator,
)

_TERMINAL_STATES = {
    PublicationState.PUBLISHED,
    PublicationState.PUBLICATION_FAILED,
    PublicationState.PUBLICATION_OUTCOME_UNKNOWN,
}


class PublicationRetentionError(RuntimeError):
    """Stable, identifier-free operational-retention failure."""


class PublicationRetentionBoundaryInvalidError(PublicationRetentionError):
    """A dependency returned data outside the closed retention contract."""


class PublicationRetentionDependencyUnavailableError(PublicationRetentionError):
    """A required durable retention operation could not be completed."""


class PublicationRetentionConflictError(PublicationRetentionError):
    """Exact terminal authority or a prior retention assignment differs."""


def publication_terminal_retention_authority_fingerprint(
    value: PublicationTerminalRetentionAuthority | dict[str, object],
) -> str:
    if isinstance(value, PublicationTerminalRetentionAuthority):
        payload = value.model_dump(
            mode="json",
            exclude={"contract_version", "fingerprint"},
        )
    else:
        payload = dict(value)
        payload.pop("contract_version", None)
        payload.pop("fingerprint", None)
    return canonical_fingerprint(
        {
            "contract_version": "7.0.1",
            "kind": "publication_terminal_retention_authority",
            "payload": payload,
        }
    )


class PublicationTerminalRetentionAuthority(PublicationModel):
    """Minimal exact terminal graph that authorizes TTL fanout and marker creation."""

    job: ControlJobRecord
    execution: PublicationExecutionAuthority
    receipt_locator: PublicationRequestReceiptLocator
    fingerprint: Fingerprint

    @property
    def aggregate(self):  # type: ignore[no-untyped-def]
        return self.execution.aggregate

    @property
    def report(self):  # type: ignore[no-untyped-def]
        assert self.execution.report is not None
        return self.execution.report

    @property
    def tombstone(self):  # type: ignore[no-untyped-def]
        assert self.execution.tombstone is not None
        return self.execution.tombstone

    @property
    def terminal_job_link(self):  # type: ignore[no-untyped-def]
        assert self.execution.terminal_job_link is not None
        return self.execution.terminal_job_link

    @model_validator(mode="after")
    def graph_is_one_exact_terminal_authority(self) -> PublicationTerminalRetentionAuthority:
        job = self.job
        execution = self.execution
        snapshot = execution.snapshot
        aggregate = execution.aggregate
        report = execution.report
        tombstone = execution.tombstone
        link = execution.terminal_job_link
        if (
            aggregate.state not in _TERMINAL_STATES
            or aggregate.terminal_at is None
            or aggregate.source_release_eligible_at is None
            or aggregate.operational_expires_at is None
            or report is None
            or tombstone is None
            or link is None
            or job.state is not ControlJobState.APPROVED
            or job.owner_id != snapshot.owner_id
            or job.job_id != snapshot.job_id
            or execution.phase6_record_version != job.record_version
            or execution.phase6_event_sequence != job.event_sequence
            or job.publication_aggregate_id != aggregate.aggregate_id
            or job.publication_terminal_state != aggregate.state.value
            or job.publication_terminal_at != aggregate.terminal_at
            or job.publication_source_release_eligible_at != aggregate.source_release_eligible_at
            or job.publication_operational_expires_at != aggregate.operational_expires_at
            or job.publication_report_id != report.report_id
            or job.publication_terminal_summary_fingerprint != link.terminal_summary_fingerprint
            or link.result_record_version != job.record_version
            or link.result_event_sequence != job.event_sequence
            or self.receipt_locator.aggregate_id != aggregate.aggregate_id
            or self.receipt_locator.owner_id != job.owner_id
            or self.receipt_locator.job_id != job.job_id
            or self.receipt_locator.receipt_id != aggregate.receipt_id
            or self.fingerprint != publication_terminal_retention_authority_fingerprint(self)
        ):
            raise ValueError("Publication terminal retention authority is invalid")
        return self


def build_publication_terminal_retention_authority(
    execution: PublicationExecutionAuthority,
    job: ControlJobRecord,
    receipt_locator: PublicationRequestReceiptLocator,
) -> PublicationTerminalRetentionAuthority:
    try:
        execution = PublicationExecutionAuthority.model_validate(
            execution.model_dump(mode="python"),
            strict=True,
        )
        job = ControlJobRecord.model_validate(job.model_dump(mode="python"), strict=True)
        locator = PublicationRequestReceiptLocator.model_validate(
            receipt_locator.model_dump(mode="python"),
            strict=True,
        )
        if (
            execution.report is None
            or execution.tombstone is None
            or execution.terminal_job_link is None
        ):
            raise ValueError
        values: dict[str, object] = {
            "job": job,
            "execution": execution,
            "receipt_locator": locator,
        }
        return PublicationTerminalRetentionAuthority(
            **values,
            fingerprint=publication_terminal_retention_authority_fingerprint(values),
        )
    except (AttributeError, TypeError, ValidationError, ValueError):
        raise PublicationRetentionBoundaryInvalidError(
            "Publication retention authority is invalid"
        ) from None


class PublicationOperationalRetentionStore(Protocol):
    def load_terminal_retention_authority(
        self,
        owner_id: str,
        aggregate_id: str,
    ) -> PublicationTerminalRetentionAuthority: ...

    def assign_terminal_retention(
        self,
        authority: PublicationTerminalRetentionAuthority,
        *,
        completed_at: datetime,
    ) -> PublicationRetentionCompletionAuthority: ...


class PublicationOperationalRetentionService:
    """Validate time/authority and invoke one bounded, replay-safe persistence fanout."""

    def __init__(
        self,
        store: PublicationOperationalRetentionStore,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._store = store
        self._clock = clock or (lambda: datetime.now(UTC))

    def assign(
        self,
        *,
        owner_id: str,
        aggregate_id: str,
    ) -> PublicationRetentionCompletionAuthority:
        now = self._now()
        try:
            authority = self._store.load_terminal_retention_authority(owner_id, aggregate_id)
        except PublicationRetentionError:
            raise
        except Exception:
            raise PublicationRetentionDependencyUnavailableError(
                "Publication retention dependency is unavailable"
            ) from None
        try:
            exact = PublicationTerminalRetentionAuthority.model_validate(
                authority.model_dump(mode="python"),
                strict=True,
            )
        except Exception:
            raise PublicationRetentionBoundaryInvalidError(
                "Publication retention authority is invalid"
            ) from None
        if (
            exact != authority
            or exact.job.owner_id != owner_id
            or exact.aggregate.aggregate_id != aggregate_id
        ):
            raise PublicationRetentionBoundaryInvalidError(
                "Publication retention authority is invalid"
            )
        assert exact.aggregate.terminal_at is not None
        assert exact.aggregate.operational_expires_at is not None
        if now < exact.aggregate.terminal_at or now >= exact.aggregate.operational_expires_at:
            raise PublicationRetentionConflictError(
                "Publication retention is outside its exact operational lifetime"
            )
        try:
            completion = self._store.assign_terminal_retention(exact, completed_at=now)
        except PublicationRetentionError:
            raise
        except Exception:
            raise PublicationRetentionDependencyUnavailableError(
                "Publication retention dependency is unavailable"
            ) from None
        try:
            parsed = validate_publication_retention_completion(exact.job, completion)
        except ValueError:
            raise PublicationRetentionBoundaryInvalidError(
                "Publication retention completion is invalid"
            ) from None
        if (
            parsed.aggregate_fingerprint != exact.aggregate.fingerprint
            or parsed.report_id != exact.report.report_id
            or parsed.report_fingerprint != exact.report.fingerprint
            or parsed.tombstone_fingerprint != exact.tombstone.fingerprint
            or parsed.terminal_job_link_fingerprint != exact.terminal_job_link.fingerprint
            or parsed.source_artifact_fingerprint != exact.job.source_artifact_fingerprint
            or parsed.completed_at > now
        ):
            raise PublicationRetentionBoundaryInvalidError(
                "Publication retention completion is invalid"
            )
        return parsed

    def _now(self) -> datetime:
        try:
            value = self._clock()
        except Exception:
            raise PublicationRetentionDependencyUnavailableError(
                "Publication retention dependency is unavailable"
            ) from None
        if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
            raise PublicationRetentionBoundaryInvalidError("Publication retention clock is invalid")
        return value.astimezone(UTC)


__all__ = [
    "PublicationOperationalRetentionService",
    "PublicationOperationalRetentionStore",
    "PublicationRetentionBoundaryInvalidError",
    "PublicationRetentionConflictError",
    "PublicationRetentionDependencyUnavailableError",
    "PublicationRetentionError",
    "PublicationTerminalRetentionAuthority",
    "build_publication_terminal_retention_authority",
    "publication_terminal_retention_authority_fingerprint",
]
