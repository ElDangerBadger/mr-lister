"""Atomic persistence boundary for pristine Phase 7 publication requests.

This module owns no provider client, dispatcher, route, or publication capability.  It defines
the complete authority read and the single all-or-nothing request commit that future application
services may use while the frozen Phase 7 contract remains disabled.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from hashlib import sha256
from threading import RLock
from types import MappingProxyType
from typing import Protocol

from pydantic import ValidationError

from mr_lister.control.fingerprints import (
    canonical_fingerprint,
    product_sync_record_fingerprint,
    review_etag,
)
from mr_lister.control.models import (
    ControlJobRecord,
    ControlJobState,
    PricingEvidenceRecord,
    PricingSnapshot,
    ProductSyncRecord,
    ReviewContent,
    ReviewDecision,
    ReviewDecisionRecord,
    SourceArtifactRecord,
)
from mr_lister.control.source_artwork import validate_source_artifact_authority
from mr_lister.publication.commands import (
    PublicationCommandReceipt,
    PublicationRequestCommit,
)
from mr_lister.publication.errors import (
    PublicationAuthorityError,
    PublicationConflictError,
    PublicationErrorCode,
    PublicationIdempotencyConflictError,
    PublicationNotFoundError,
)
from mr_lister.publication.models import (
    PublicationAggregate,
    PublicationAttempt,
    PublicationDomainEvent,
    PublicationPermit,
    PublicationSnapshot,
    PublicationWorkRequest,
)


@dataclass(frozen=True)
class PublicationRequestAuthority:
    """Complete immutable Phase 6 evidence required to request publication."""

    current_job: ControlJobRecord
    review: ReviewContent
    approval_decision: ReviewDecisionRecord
    source: SourceArtifactRecord
    product_sync: ProductSyncRecord
    pricing_snapshot: PricingSnapshot
    pricing_evidence: PricingEvidenceRecord


@dataclass(frozen=True)
class PublicationRequestTransaction:
    """Persistence-owned wrapper around the pure publication request commit."""

    authority: PublicationRequestAuthority
    updated_job: ControlJobRecord
    commit: PublicationRequestCommit


class PublicationStore(Protocol):
    """Minimal persistence protocol needed by a future Phase 7.1 request service."""

    def resolve_request_receipt(
        self,
        owner_id: str,
        job_id: str,
        key_digest: str,
    ) -> PublicationCommandReceipt | None: ...

    def load_request_authority(
        self,
        owner_id: str,
        job_id: str,
    ) -> PublicationRequestAuthority: ...

    def commit_request(
        self,
        transaction: PublicationRequestTransaction,
    ) -> PublicationCommandReceipt: ...

    def get_aggregate_for_owner(
        self,
        owner_id: str,
        job_id: str,
    ) -> PublicationAggregate: ...


def _invalid_authority(message: str) -> PublicationAuthorityError:
    return PublicationAuthorityError(PublicationErrorCode.INVALID_AUTHORITY, message)


def _not_approved(message: str = "The job is not approved for publication") -> None:
    raise PublicationAuthorityError(PublicationErrorCode.NOT_APPROVED, message)


def _already_requested() -> None:
    raise PublicationConflictError(
        PublicationErrorCode.ALREADY_REQUESTED,
        "Publication was already requested for this job",
    )


def _review_content_fingerprint(review: ReviewContent) -> str:
    """Rebuild the complete immutable review material written by Phase 6."""

    return canonical_fingerprint(
        {
            "contract_version": review.contract_version,
            "job_id": review.job_id,
            "review_version": review.review_version,
            "actor": review.actor.value,
            "title": review.title,
            "description": review.description,
            "tags": review.tags,
            "audience": review.audience,
            "title_rationale": review.title_rationale,
            "tag_rationale": review.tag_rationale,
            "validation_passed": review.validation_passed,
            "validation_issue_codes": review.validation_issue_codes,
            "artwork_analysis_fingerprint": review.artwork_analysis_fingerprint,
            "product_profile_fingerprint": review.product_profile_fingerprint,
            "created_at": review.created_at.isoformat(),
        }
    )


def validate_publication_request_authority(authority: PublicationRequestAuthority) -> None:
    """Fail closed unless every Phase 6 source record is the exact approved authority."""

    try:
        revalidated = PublicationRequestAuthority(
            current_job=ControlJobRecord.model_validate(
                authority.current_job.model_dump(mode="python")
            ),
            review=ReviewContent.model_validate(authority.review.model_dump(mode="python")),
            approval_decision=ReviewDecisionRecord.model_validate(
                authority.approval_decision.model_dump(mode="python")
            ),
            source=SourceArtifactRecord.model_validate(authority.source.model_dump(mode="python")),
            product_sync=ProductSyncRecord.model_validate(
                authority.product_sync.model_dump(mode="python")
            ),
            pricing_snapshot=PricingSnapshot.model_validate(
                authority.pricing_snapshot.model_dump(mode="python")
            ),
            pricing_evidence=PricingEvidenceRecord.model_validate(
                authority.pricing_evidence.model_dump(mode="python")
            ),
        )
    except (AttributeError, ValidationError, ValueError):
        raise _invalid_authority("The approved publication authority is invalid") from None
    if revalidated != authority:
        raise _invalid_authority("The approved publication authority is invalid")

    job = authority.current_job
    review = authority.review
    decision = authority.approval_decision
    source = authority.source
    sync = authority.product_sync
    pricing = authority.pricing_snapshot
    evidence = authority.pricing_evidence

    if job.state is not ControlJobState.APPROVED:
        _not_approved()
    if not job.review_validated:
        raise _invalid_authority("The approved job does not carry a validated review")
    required_job_authority = (
        job.review_fingerprint,
        job.source_artifact_fingerprint,
        job.artwork_analysis_fingerprint,
        job.product_id,
        job.provider_payload_fingerprint,
        job.product_sync_id,
        job.product_sync_fingerprint,
        job.pricing_snapshot_id,
        job.pricing_snapshot_fingerprint,
        job.approval_decision_id,
        job.approval_fingerprint,
        job.uploaded_image_id,
    )
    if any(value is None for value in required_job_authority):
        raise _invalid_authority("The approved job has incomplete publication authority")
    if job.provider_outcome_unconfirmed or job.upload_outcome_unconfirmed:
        raise _invalid_authority("The approved job has unresolved provider authority")

    if (
        review.job_id != job.job_id
        or review.review_version != job.review_version
        or review.fingerprint != job.review_fingerprint
        or review.fingerprint != _review_content_fingerprint(review)
        or review.artwork_analysis_fingerprint != job.artwork_analysis_fingerprint
        or not review.validation_passed
    ):
        raise _invalid_authority("The current review authority does not match the approved job")
    if (
        decision.job_id != job.job_id
        or decision.actor_owner_id != job.owner_id
        or decision.decision is not ReviewDecision.APPROVE
        or decision.decision_id != job.approval_decision_id
        or decision.review_version != job.review_version
        or decision.review_fingerprint != job.review_fingerprint
        or decision.approval_fingerprint != job.approval_fingerprint
        or decision.decision_id
        != f"decision_{sha256(decision.command_receipt_id.encode()).hexdigest()[:40]}"
        or (job.publication_aggregate_id is None and decision.decided_at != job.updated_at)
    ):
        raise _invalid_authority("The approval decision does not match the approved job")

    try:
        validate_source_artifact_authority(source)
    except ValueError:
        raise _invalid_authority("The pinned source artifact authority is invalid") from None
    if (
        source.owner_id != job.owner_id
        or source.job_id != job.job_id
        or source.fingerprint != job.source_artifact_fingerprint
        or review.product_profile_fingerprint != source.product_profile_fingerprint
    ):
        raise _invalid_authority("The pinned source artifact does not match the approved job")

    if (
        sync.job_id != job.job_id
        or sync.sync_id != job.product_sync_id
        or sync.review_version != job.review_version
        or sync.product_id != job.product_id
        or sync.image_id != job.uploaded_image_id
        or sync.payload_fingerprint != job.provider_payload_fingerprint
        or sync.fingerprint != job.product_sync_fingerprint
        or sync.printify_shop_id is None
        or sync.provider_locked
        or sync.provider_published
    ):
        raise _invalid_authority("The product synchronization does not match the approved job")
    try:
        expected_sync_fingerprint = product_sync_record_fingerprint(sync)
    except ValueError:
        raise _invalid_authority("The product synchronization fingerprint is invalid") from None
    if sync.fingerprint != expected_sync_fingerprint:
        raise _invalid_authority("The product synchronization fingerprint is invalid")

    if (
        pricing.job_id != job.job_id
        or pricing.snapshot_id != job.pricing_snapshot_id
        or pricing.review_version != job.review_version
        or pricing.product_sync_fingerprint != sync.fingerprint
        or pricing.fingerprint != job.pricing_snapshot_fingerprint
        or evidence.job_id != job.job_id
        or evidence.snapshot_id != pricing.snapshot_id
        or evidence.review_version != pricing.review_version
        or evidence.product_sync_fingerprint != pricing.product_sync_fingerprint
        or evidence.fingerprint != pricing.fingerprint
        or evidence.created_at != pricing.created_at
        or evidence.estimate.fresh_until != pricing.fresh_until
        or evidence.estimate.calculated_at != pricing.created_at
        or evidence.estimate.fingerprint != pricing.fingerprint
    ):
        raise _invalid_authority("The pricing authority does not match the approved job")
    estimate_by_id = {variant.variant_id: variant for variant in evidence.estimate.variants}
    sync_by_id = {variant.variant_id: variant for variant in sync.variants}
    if set(estimate_by_id) != set(sync_by_id) or any(
        estimate_by_id[variant_id].retail_price_cents != sync_by_id[variant_id].retail_price_cents
        or estimate_by_id[variant_id].production_cost_cents
        != sync_by_id[variant_id].production_cost_cents
        for variant_id in sync_by_id
    ):
        raise _invalid_authority("The pricing evidence does not cover exact synchronized variants")

    expected_approval = review_etag(
        job_id=job.job_id,
        review_version=job.review_version,
        review_fingerprint=job.review_fingerprint,
        product_id=job.product_id,
        product_sync_fingerprint=job.product_sync_fingerprint,
        pricing_snapshot_id=job.pricing_snapshot_id,
        pricing_snapshot_fingerprint=job.pricing_snapshot_fingerprint,
    )
    if job.approval_fingerprint != expected_approval:
        raise _invalid_authority("The approval fingerprint does not match current authority")


def validate_publication_request_transaction(
    transaction: PublicationRequestTransaction,
) -> None:
    """Prove that the wrapper changes only the Phase 6 publication link."""

    try:
        revalidated_commit = PublicationRequestCommit.model_validate(
            transaction.commit.model_dump(mode="python")
        )
        revalidated_updated_job = ControlJobRecord.model_validate(
            transaction.updated_job.model_dump(mode="python")
        )
    except (AttributeError, ValidationError, ValueError):
        raise _invalid_authority("The publication request transaction is invalid") from None
    if (
        revalidated_commit != transaction.commit
        or revalidated_updated_job != transaction.updated_job
    ):
        raise _invalid_authority("The publication request transaction is invalid")
    validate_publication_request_authority(transaction.authority)
    current = transaction.authority.current_job
    updated = transaction.updated_job
    commit = transaction.commit
    link = commit.job_link
    snapshot = commit.snapshot

    if current.publication_aggregate_id is not None:
        _already_requested()
    if link.linked_at < current.updated_at:
        raise _invalid_authority("The publication link cannot move job time backwards")
    expected_updated = ControlJobRecord.model_validate(
        {
            **current.model_dump(mode="python"),
            "record_version": current.record_version + 1,
            "publication_aggregate_id": commit.aggregate.aggregate_id,
            "updated_at": link.linked_at,
        }
    )
    if updated != expected_updated:
        raise _invalid_authority("The publication request changed Phase 6 job authority")
    if (
        link.owner_id != current.owner_id
        or link.job_id != current.job_id
        or link.expected_record_version != current.record_version
        or link.result_record_version != updated.record_version
        or link.expected_event_sequence != current.event_sequence
        or link.result_event_sequence != updated.event_sequence
        or link.publication_aggregate_id != updated.publication_aggregate_id
        or link.linked_at != updated.updated_at
    ):
        raise _invalid_authority("The publication job link does not match the Phase 6 rows")

    authority = transaction.authority
    sync = authority.product_sync
    pricing = authority.pricing_snapshot
    source = authority.source
    if (
        snapshot.owner_id != current.owner_id
        or snapshot.job_id != current.job_id
        or snapshot.expected_record_version != current.record_version
        or snapshot.approval_decision_id != authority.approval_decision.decision_id
        or snapshot.approval_fingerprint != current.approval_fingerprint
        or snapshot.review_version != authority.review.review_version
        or snapshot.review_fingerprint != authority.review.fingerprint
        or snapshot.product_sync_id != sync.sync_id
        or snapshot.product_sync_fingerprint != sync.fingerprint
        or snapshot.printify_shop_id != sync.printify_shop_id
        or snapshot.printify_product_id != sync.product_id
        or snapshot.printify_image_id != sync.image_id
        or snapshot.product_payload_fingerprint != sync.payload_fingerprint
        or snapshot.pricing_snapshot_id != pricing.snapshot_id
        or snapshot.pricing_snapshot_fingerprint != pricing.fingerprint
        or snapshot.pricing_evidence_fingerprint != authority.pricing_evidence.fingerprint
        or snapshot.pricing_fresh_until != pricing.fresh_until
        or snapshot.profile_id != source.product_profile_id
        or snapshot.profile_version != source.product_profile_version
        or snapshot.profile_fingerprint != source.product_profile_fingerprint
    ):
        raise _invalid_authority("The publication snapshot does not freeze the exact authority")


class InMemoryPublicationStore:
    """Thread-safe deterministic oracle for the Phase 7.1 request transaction."""

    def __init__(self, authorities: Iterable[PublicationRequestAuthority] = ()) -> None:
        self._lock = RLock()
        self._jobs: dict[str, ControlJobRecord] = {}
        self._reviews: dict[tuple[str, int], ReviewContent] = {}
        self._approval_decisions: dict[tuple[str, str], ReviewDecisionRecord] = {}
        self._sources: dict[str, SourceArtifactRecord] = {}
        self._product_syncs: dict[tuple[str, str], ProductSyncRecord] = {}
        self._pricing_snapshots: dict[tuple[str, str], PricingSnapshot] = {}
        self._pricing_evidence: dict[tuple[str, str], PricingEvidenceRecord] = {}
        self._aggregates: dict[str, PublicationAggregate] = {}
        self._snapshots: dict[tuple[str, str], PublicationSnapshot] = {}
        self._attempts: dict[tuple[str, str], PublicationAttempt] = {}
        self._permits: dict[tuple[str, str], PublicationPermit] = {}
        self._work_requests: dict[tuple[str, str], PublicationWorkRequest] = {}
        self._events: dict[tuple[str, int], PublicationDomainEvent] = {}
        self._receipts: dict[tuple[str, str, str], PublicationCommandReceipt] = {}
        for authority in authorities:
            self.seed_authority(authority)

    @property
    def jobs(self) -> Mapping[str, ControlJobRecord]:
        return MappingProxyType(self._jobs)

    @property
    def aggregates(self) -> Mapping[str, PublicationAggregate]:
        return MappingProxyType(self._aggregates)

    @property
    def snapshots(self) -> Mapping[tuple[str, str], PublicationSnapshot]:
        return MappingProxyType(self._snapshots)

    @property
    def attempts(self) -> Mapping[tuple[str, str], PublicationAttempt]:
        return MappingProxyType(self._attempts)

    @property
    def permits(self) -> Mapping[tuple[str, str], PublicationPermit]:
        return MappingProxyType(self._permits)

    @property
    def work_requests(self) -> Mapping[tuple[str, str], PublicationWorkRequest]:
        return MappingProxyType(self._work_requests)

    @property
    def events(self) -> Mapping[tuple[str, int], PublicationDomainEvent]:
        return MappingProxyType(self._events)

    @property
    def receipts(self) -> Mapping[tuple[str, str, str], PublicationCommandReceipt]:
        return MappingProxyType(self._receipts)

    def seed_authority(self, authority: PublicationRequestAuthority) -> None:
        """Seed one complete immutable Phase 6 graph for deterministic tests."""

        validate_publication_request_authority(authority)
        job = authority.current_job
        with self._lock:
            keys = (
                (self._jobs, job.job_id),
                (self._reviews, (job.job_id, authority.review.review_version)),
                (
                    self._approval_decisions,
                    (job.job_id, authority.approval_decision.decision_id),
                ),
                (self._sources, job.job_id),
                (self._product_syncs, (job.job_id, authority.product_sync.sync_id)),
                (
                    self._pricing_snapshots,
                    (job.job_id, authority.pricing_snapshot.snapshot_id),
                ),
                (
                    self._pricing_evidence,
                    (job.job_id, authority.pricing_evidence.snapshot_id),
                ),
            )
            if any(key in mapping for mapping, key in keys):
                raise ValueError("Publication request authority was already seeded")
            self._jobs[job.job_id] = job
            self._reviews[(job.job_id, authority.review.review_version)] = authority.review
            self._approval_decisions[(job.job_id, authority.approval_decision.decision_id)] = (
                authority.approval_decision
            )
            self._sources[job.job_id] = authority.source
            self._product_syncs[(job.job_id, authority.product_sync.sync_id)] = (
                authority.product_sync
            )
            self._pricing_snapshots[(job.job_id, authority.pricing_snapshot.snapshot_id)] = (
                authority.pricing_snapshot
            )
            self._pricing_evidence[(job.job_id, authority.pricing_evidence.snapshot_id)] = (
                authority.pricing_evidence
            )

    def resolve_request_receipt(
        self,
        owner_id: str,
        job_id: str,
        key_digest: str,
    ) -> PublicationCommandReceipt | None:
        with self._lock:
            return self._receipts.get((owner_id, job_id, key_digest))

    def load_request_authority(
        self,
        owner_id: str,
        job_id: str,
    ) -> PublicationRequestAuthority:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None or job.owner_id != owner_id:
                raise PublicationNotFoundError()
            if job.state is not ControlJobState.APPROVED:
                _not_approved()
            try:
                if (
                    job.approval_decision_id is None
                    or job.product_sync_id is None
                    or job.pricing_snapshot_id is None
                ):
                    raise KeyError
                authority = PublicationRequestAuthority(
                    current_job=job,
                    review=self._reviews[(job_id, job.review_version)],
                    approval_decision=self._approval_decisions[(job_id, job.approval_decision_id)],
                    source=self._sources[job_id],
                    product_sync=self._product_syncs[(job_id, job.product_sync_id)],
                    pricing_snapshot=self._pricing_snapshots[(job_id, job.pricing_snapshot_id)],
                    pricing_evidence=self._pricing_evidence[(job_id, job.pricing_snapshot_id)],
                )
            except KeyError:
                raise _invalid_authority(
                    "The approved publication authority is incomplete"
                ) from None
            validate_publication_request_authority(authority)
            return authority

    def commit_request(
        self,
        transaction: PublicationRequestTransaction,
    ) -> PublicationCommandReceipt:
        validate_publication_request_transaction(transaction)
        commit = transaction.commit
        receipt = commit.receipt
        with self._lock:
            receipt_key = (
                receipt.owner_id,
                receipt.job_id,
                receipt.idempotency_key_digest,
            )
            existing = self._receipts.get(receipt_key)
            if existing is not None:
                if existing.request_fingerprint == receipt.request_fingerprint:
                    return existing
                raise PublicationIdempotencyConflictError()

            persisted = self._authority_for_job(transaction.authority.current_job.job_id)
            if persisted != transaction.authority:
                raise PublicationConflictError(
                    PublicationErrorCode.CONCURRENT_WRITE,
                    "Publication authority changed before the request could commit",
                )

            aggregate_id = commit.aggregate.aggregate_id
            immutable_keys = (
                (self._aggregates, aggregate_id),
                (self._snapshots, (aggregate_id, commit.snapshot.snapshot_id)),
                (self._attempts, (aggregate_id, commit.attempt.attempt_id)),
                (self._permits, (aggregate_id, commit.permit.permit_id)),
                (self._work_requests, (aggregate_id, commit.work_request.work_request_id)),
                (self._events, (aggregate_id, commit.event.sequence)),
            )
            if any(key in mapping for mapping, key in immutable_keys):
                raise PublicationConflictError(
                    PublicationErrorCode.CONCURRENT_WRITE,
                    "Publication records changed before the request could commit",
                )

            self._jobs[transaction.updated_job.job_id] = transaction.updated_job
            self._aggregates[aggregate_id] = commit.aggregate
            self._snapshots[(aggregate_id, commit.snapshot.snapshot_id)] = commit.snapshot
            self._attempts[(aggregate_id, commit.attempt.attempt_id)] = commit.attempt
            self._permits[(aggregate_id, commit.permit.permit_id)] = commit.permit
            self._work_requests[(aggregate_id, commit.work_request.work_request_id)] = (
                commit.work_request
            )
            self._events[(aggregate_id, commit.event.sequence)] = commit.event
            self._receipts[receipt_key] = receipt
            return receipt

    def get_aggregate_for_owner(
        self,
        owner_id: str,
        job_id: str,
    ) -> PublicationAggregate:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None or job.owner_id != owner_id or job.publication_aggregate_id is None:
                raise PublicationNotFoundError()
            aggregate = self._aggregates.get(job.publication_aggregate_id)
            if aggregate is None or aggregate.owner_id != owner_id or aggregate.job_id != job_id:
                raise _invalid_authority("The publication aggregate link is incomplete")
            return aggregate

    def _authority_for_job(self, job_id: str) -> PublicationRequestAuthority | None:
        job = self._jobs.get(job_id)
        if (
            job is None
            or job.approval_decision_id is None
            or job.product_sync_id is None
            or job.pricing_snapshot_id is None
        ):
            return None
        records = (
            self._reviews.get((job_id, job.review_version)),
            self._approval_decisions.get((job_id, job.approval_decision_id)),
            self._sources.get(job_id),
            self._product_syncs.get((job_id, job.product_sync_id)),
            self._pricing_snapshots.get((job_id, job.pricing_snapshot_id)),
            self._pricing_evidence.get((job_id, job.pricing_snapshot_id)),
        )
        if any(record is None for record in records):
            return None
        review, decision, source, sync, pricing, evidence = records
        assert isinstance(review, ReviewContent)
        assert isinstance(decision, ReviewDecisionRecord)
        assert isinstance(source, SourceArtifactRecord)
        assert isinstance(sync, ProductSyncRecord)
        assert isinstance(pricing, PricingSnapshot)
        assert isinstance(evidence, PricingEvidenceRecord)
        return PublicationRequestAuthority(
            current_job=job,
            review=review,
            approval_decision=decision,
            source=source,
            product_sync=sync,
            pricing_snapshot=pricing,
            pricing_evidence=evidence,
        )
