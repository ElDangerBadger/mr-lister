"""Publication-retention completion authority shared without a Phase 7 dependency.

The control job already owns the primitive terminal publication summary.  This module adds the
single immutable proof that a separate Phase 7 retention worker completed every exact operational
TTL assignment.  Phase 6 source retention may read this record without importing any publication
runtime, provider, or persistence module.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from math import ceil
from typing import Annotated, Literal

from pydantic import AwareDatetime, Field, StrictInt, StringConstraints, model_validator

from mr_lister.contracts import ContractModel
from mr_lister.control.fingerprints import canonical_fingerprint

PUBLICATION_RETENTION_COMPLETION_CONTRACT_VERSION = "1.0.0"
PUBLICATION_RETENTION_SORT_KEY = "PUBLICATION_RETENTION"
PUBLICATION_RETENTION_ENTITY_TYPE = "PUBLICATION_RETENTION_COMPLETION"

SafeId = Annotated[
    str,
    StringConstraints(strip_whitespace=True, pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$"),
]
Fingerprint = Annotated[str, StringConstraints(pattern=r"^[a-f0-9]{64}$")]


def publication_operational_expiry_epoch(value: datetime) -> int:
    """Return the first whole epoch second not earlier than the exact expiry timestamp."""

    if not isinstance(value, datetime) or value.utcoffset() != timedelta(0):
        raise ValueError("Publication operational expiry must be UTC-aware")
    return ceil(value.timestamp())


def publication_retention_completion_fingerprint(
    value: PublicationRetentionCompletionAuthority | dict[str, object],
) -> str:
    if isinstance(value, PublicationRetentionCompletionAuthority):
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
            "contract_version": PUBLICATION_RETENTION_COMPLETION_CONTRACT_VERSION,
            "kind": "publication_retention_completion",
            "payload": payload,
        }
    )


class PublicationRetentionCompletionAuthority(ContractModel):
    """Marker written last, after every exact +90-day TTL assignment is proven."""

    contract_version: Literal["1.0.0"] = PUBLICATION_RETENTION_COMPLETION_CONTRACT_VERSION
    job_id: SafeId
    aggregate_id: SafeId
    job_record_version: StrictInt = Field(ge=1)
    terminal_state: Literal[
        "published",
        "publication_failed",
        "publication_outcome_unknown",
    ]
    terminal_at: AwareDatetime
    terminal_summary_fingerprint: Fingerprint
    source_artifact_fingerprint: Fingerprint
    aggregate_fingerprint: Fingerprint
    report_id: SafeId
    report_fingerprint: Fingerprint
    tombstone_fingerprint: Fingerprint
    terminal_job_link_fingerprint: Fingerprint
    source_release_eligible_at: AwareDatetime
    operational_expires_at: AwareDatetime
    expires_at_epoch_seconds: StrictInt = Field(ge=1, le=253_402_300_799)
    publication_row_count: StrictInt = Field(ge=12, le=943)
    ttl_assignment_count: StrictInt = Field(ge=14, le=945)
    inventory_fingerprint: Fingerprint
    completed_at: AwareDatetime
    fingerprint: Fingerprint

    @model_validator(mode="after")
    def completion_is_exact_and_not_early(self) -> PublicationRetentionCompletionAuthority:
        timestamps = (
            self.terminal_at,
            self.source_release_eligible_at,
            self.operational_expires_at,
            self.completed_at,
        )
        if any(value.utcoffset() != timedelta(0) for value in timestamps):
            raise ValueError("Publication retention completion timestamps must be UTC")
        if self.source_release_eligible_at != self.terminal_at + timedelta(days=30):
            raise ValueError("Publication source release must be terminal time plus 30 days")
        if self.operational_expires_at != self.terminal_at + timedelta(days=90):
            raise ValueError("Publication operational expiry must be terminal time plus 90 days")
        if not self.terminal_at <= self.completed_at < self.operational_expires_at:
            raise ValueError("Publication retention must complete during its operational lifetime")
        if self.expires_at_epoch_seconds != publication_operational_expiry_epoch(
            self.operational_expires_at
        ):
            raise ValueError(
                "Publication retention TTL epoch differs from exact operational expiry"
            )
        if self.ttl_assignment_count != self.publication_row_count + 2:
            raise ValueError("Publication retention assignment count is inconsistent")
        if self.fingerprint != publication_retention_completion_fingerprint(self):
            raise ValueError("Publication retention completion fingerprint is invalid")
        return self


def validate_publication_retention_completion(
    job: object,
    completion: object,
    *,
    source: object | None = None,
) -> PublicationRetentionCompletionAuthority:
    """Bind the publication-free marker to the exact current control-job terminal summary."""

    from mr_lister.control.models import ControlJobRecord, ControlJobState, SourceArtifactRecord

    try:
        parsed_job = ControlJobRecord.model_validate(job.model_dump(mode="python"), strict=True)
        parsed = PublicationRetentionCompletionAuthority.model_validate(
            completion.model_dump(mode="python"),
            strict=True,
        )
        parsed_source = (
            None
            if source is None
            else SourceArtifactRecord.model_validate(source.model_dump(mode="python"), strict=True)
        )
    except Exception:
        raise ValueError("Publication retention completion authority is invalid") from None
    if (
        parsed_job != job
        or parsed != completion
        or parsed_job.state is not ControlJobState.APPROVED
        or parsed_job.publication_aggregate_id != parsed.aggregate_id
        or parsed_job.job_id != parsed.job_id
        or parsed_job.record_version != parsed.job_record_version
        or parsed_job.publication_terminal_state != parsed.terminal_state
        or parsed_job.publication_terminal_at != parsed.terminal_at
        or parsed_job.publication_terminal_summary_fingerprint
        != parsed.terminal_summary_fingerprint
        or parsed_job.source_artifact_fingerprint != parsed.source_artifact_fingerprint
        or parsed_job.publication_report_id != parsed.report_id
        or parsed_job.publication_source_release_eligible_at != parsed.source_release_eligible_at
        or parsed_job.publication_operational_expires_at != parsed.operational_expires_at
        or (source is not None and parsed_source != source)
        or (
            parsed_source is not None
            and (
                parsed_source.job_id != parsed.job_id
                or parsed_source.fingerprint != parsed.source_artifact_fingerprint
            )
        )
    ):
        raise ValueError("Publication retention completion differs from the control job")
    return parsed


__all__ = [
    "PUBLICATION_RETENTION_COMPLETION_CONTRACT_VERSION",
    "PUBLICATION_RETENTION_ENTITY_TYPE",
    "PUBLICATION_RETENTION_SORT_KEY",
    "PublicationRetentionCompletionAuthority",
    "publication_operational_expiry_epoch",
    "publication_retention_completion_fingerprint",
    "validate_publication_retention_completion",
]
