"""Immutable campaign, publication evidence and fenced cleanup state."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from hashlib import sha256
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    StringConstraints,
    model_validator,
)

from mr_lister.cloud.printify_secret_contract import validate_printify_secret_arn
from mr_lister.control.models import ControlJobRecord
from mr_lister.publication.contract import PublicationState
from mr_lister.publication.execution_models import (
    ExecutionPublicationAggregate,
    PublicationProductObservation,
    PublicationProviderAuthority,
    PublicationReadOutcome,
    PublicationResult,
)
from mr_lister.publication.models import (
    Fingerprint,
    OwnerId,
    PublicationSnapshot,
    SafeId,
    UtcDateTime,
)

TableName = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_.-]{3,255}$")]


def digest(value: object) -> str:
    return sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


class Model(BaseModel):
    model_config = ConfigDict(
        extra="forbid", frozen=True, strict=True, revalidate_instances="always"
    )


class CleanupConfig(Model):
    campaign_id: SafeId
    judge_owner_id: OwnerId
    primary_owner_id: OwnerId
    printify_shop_id: StrictInt = Field(gt=0)
    campaign_started_at: UtcDateTime
    source_table_name: TableName
    cleanup_table_name: TableName
    printify_secret_arn: str
    dry_run: StrictBool = True
    activation_fingerprint: Fingerprint | None = None
    max_jobs_per_run: StrictInt = Field(default=1, ge=1, le=3)
    page_size: StrictInt = Field(default=5, ge=1, le=25)

    @property
    def campaign_fingerprint(self) -> str:
        return digest(
            self.model_dump(
                mode="json",
                exclude={
                    "dry_run",
                    "activation_fingerprint",
                    "max_jobs_per_run",
                    "page_size",
                },
            )
        )

    @model_validator(mode="after")
    def explicit_boundary(self) -> CleanupConfig:
        validate_printify_secret_arn(self.printify_secret_arn)
        if self.primary_owner_id == self.judge_owner_id:
            raise ValueError("Judge and primary owners must differ")
        if self.source_table_name == self.cleanup_table_name:
            raise ValueError("Cleanup requires a separate table")
        if self.activation_fingerprint not in (None, self.campaign_fingerprint):
            raise ValueError("Cleanup activation does not match the campaign")
        if not self.dry_run and self.activation_fingerprint != self.campaign_fingerprint:
            raise ValueError("Active cleanup requires the exact campaign fingerprint")
        return self


class PublicationEvidence(Model):
    """Retain exact existing validated records, never titles or discovered provider IDs."""

    job: ControlJobRecord
    aggregate: ExecutionPublicationAggregate
    snapshot: PublicationSnapshot
    provider: PublicationProviderAuthority
    observation: PublicationProductObservation
    result: PublicationResult

    @property
    def fingerprint(self) -> str:
        return digest(self.model_dump(mode="json"))

    @property
    def deadline(self) -> datetime:
        return self.result.verified_at + timedelta(seconds=1800)

    def require_campaign(self, config: CleanupConfig) -> None:
        if (
            self.job.owner_id != config.judge_owner_id
            or self.snapshot.printify_shop_id != config.printify_shop_id
            or self.snapshot.requested_at < config.campaign_started_at
        ):
            raise ValueError("Publication is outside the cleanup campaign")

    @model_validator(mode="after")
    def exact_published_graph(self) -> PublicationEvidence:
        j, a, s, p, o, r = (
            self.job,
            self.aggregate,
            self.snapshot,
            self.provider,
            self.observation,
            self.result,
        )
        if not (
            a.state is PublicationState.PUBLISHED and j.publication_terminal_state == "published"
        ):
            raise ValueError("Cleanup requires confirmed publication")
        if not (
            j.owner_id == a.owner_id == s.owner_id == p.owner_id
            and j.job_id == a.job_id == s.job_id == p.job_id
            and j.publication_aggregate_id
            == a.aggregate_id
            == p.aggregate_id
            == o.aggregate_id
            == r.aggregate_id
            and a.snapshot_id == s.snapshot_id == p.snapshot_id == o.snapshot_id
            and a.snapshot_fingerprint
            == s.fingerprint
            == p.snapshot_fingerprint
            == o.snapshot_fingerprint
            and a.attempt_id == p.attempt_id == o.attempt_id
            and a.permit_id == p.permit_id
            and a.work_request_id == p.work_request_id
            and j.publication_result_id == a.result_id == r.result_id
            and j.publication_terminal_at == a.terminal_at
            and j.publication_report_id == a.report_id
            and a.requested_at == s.requested_at
            and j.product_id == s.printify_product_id == p.printify_product_id
            and s.printify_shop_id == p.printify_shop_id
            and s.printify_image_id == p.printify_image_id
            and j.provider_payload_fingerprint
            == s.product_payload_fingerprint
            == p.product_payload_fingerprint
            and j.product_sync_id == s.product_sync_id
            and j.product_sync_fingerprint
            == s.product_sync_fingerprint
            == p.product_sync_fingerprint
            and j.approval_fingerprint == s.approval_fingerprint == p.approval_fingerprint
            and j.approved_review_fingerprint == s.review_fingerprint == p.review_fingerprint
            and s.release_manifest_fingerprint == p.release_manifest_fingerprint
            and o.provider_authority_id == p.provider_authority_id
            and o.provider_authority_fingerprint == p.fingerprint
            and r.observation_id == o.observation_id
            and r.observation_fingerprint == o.fingerprint
            and a.last_observation_fingerprint == o.fingerprint
            and o.outcome is PublicationReadOutcome.POSITIVE_PROOF
            and r.numeric_listing_id == o.numeric_listing_id
            and r.verified_product_fingerprint == o.verified_product_fingerprint
            and r.verified_at == o.observed_at
            and s.requested_at <= r.verified_at <= a.terminal_at
        ):
            raise ValueError("Publication evidence is not one exact graph")
        if len(self.model_dump_json().encode()) > 200_000:
            raise ValueError("Cleanup evidence exceeds its persistence bound")
        return self


CleanupStatus = Literal["pending", "leased", "retry", "deleted", "absent", "blocked", "exhausted"]
TERMINAL = frozenset({"deleted", "absent", "blocked", "exhausted"})


class CleanupRecord(Model):
    campaign_id: SafeId
    campaign_fingerprint: Fingerprint
    evidence: PublicationEvidence
    evidence_fingerprint: Fingerprint
    deadline: UtcDateTime
    status: CleanupStatus = "pending"
    version: StrictInt = Field(default=0, ge=0)
    attempts: StrictInt = Field(default=0, ge=0, le=8)
    next_attempt_at: UtcDateTime
    lease_id: SafeId | None = None
    lease_until: UtcDateTime | None = None
    last_outcome: Literal[
        "registered",
        "claimed",
        "provider_deleted",
        "provider_absent",
        "source_changed",
        "provider_mismatch",
        "dependency_unavailable",
        "retry_limit",
    ] = "registered"
    updated_at: UtcDateTime
    # Printify absence is not an independently authenticated Etsy readback.
    etsy_removal_verified: Literal[False] = False

    @model_validator(mode="after")
    def fixed_authority(self) -> CleanupRecord:
        if (
            self.evidence_fingerprint != self.evidence.fingerprint
            or self.deadline != self.evidence.deadline
        ):
            raise ValueError("Cleanup evidence or deadline changed")
        if self.next_attempt_at < self.deadline:
            raise ValueError("Cleanup cannot run before its fixed deadline")
        if (self.status == "leased") != (
            self.lease_id is not None and self.lease_until is not None
        ):
            raise ValueError("Cleanup lease is incomplete")
        if (self.lease_id is None) != (self.lease_until is None):
            raise ValueError("Cleanup lease fields must be present together")
        if self.status == "pending" and (self.attempts != 0 or self.version != 0):
            raise ValueError("Pending cleanup must be pristine")
        if self.status != "pending" and self.attempts < 1:
            raise ValueError("Cleanup outcomes require an audited attempt")
        if self.status == "leased" and (self.lease_until <= self.updated_at or self.attempts < 1):
            raise ValueError("Cleanup lease is invalid")
        return self
