"""Owner-first, read-only projection of the separate Phase 7 publication aggregate."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from pydantic import ValidationError, model_validator

from mr_lister.control.errors import NotFoundError
from mr_lister.control.models import ControlJobRecord, ControlJobState
from mr_lister.publication.contract import PublicationPermitState, PublicationState
from mr_lister.publication.execution_models import (
    ExecutionPublicationAggregate,
    ExecutionPublicationAttempt,
    ExecutionPublicationPermit,
    ExecutionPublicationWork,
    PublicationAttemptStatus,
    PublicationExecutionWorkStatus,
    PublicationMutationClaim,
    PublicationNotification,
    PublicationPostObservation,
    PublicationPostOutcome,
    PublicationProductObservation,
    PublicationReadOutcome,
    PublicationResult,
    PublicationTerminalReport,
)
from mr_lister.publication.fingerprints import canonical_fingerprint
from mr_lister.publication.models import (
    PublicationAggregate,
    PublicationAttempt,
    PublicationModel,
    PublicationPermit,
    PublicationSnapshot,
    PublicationWorkRequest,
)
from mr_lister.publication.projection_models import (
    SellerPublicationProjection,
    SellerPublicationStage,
)


class PublicationProjectionUnavailableError(Exception):
    """The joined publication authority is absent or internally inconsistent."""

    code = "PUBLICATION_PROJECTION_UNAVAILABLE"


class PublicationProjectionAuthority(PublicationModel):
    """Exact current rows needed to derive one seller-safe projection."""

    job: ControlJobRecord
    snapshot: PublicationSnapshot
    aggregate: PublicationAggregate | ExecutionPublicationAggregate
    attempt: PublicationAttempt | ExecutionPublicationAttempt
    permit: PublicationPermit | ExecutionPublicationPermit
    work: PublicationWorkRequest | ExecutionPublicationWork
    mutation_claim: PublicationMutationClaim | None = None
    post_observation: PublicationPostObservation | None = None
    observation: PublicationProductObservation | None = None
    result: PublicationResult | None = None
    notification: PublicationNotification | None = None
    report: PublicationTerminalReport | None = None

    @model_validator(mode="after")
    def rows_form_one_owner_scoped_graph(self) -> PublicationProjectionAuthority:
        aggregate = self.aggregate
        if (
            self.job.state is not ControlJobState.APPROVED
            or self.job.owner_id != self.snapshot.owner_id
            or self.job.job_id != self.snapshot.job_id
            or self.job.publication_aggregate_id != aggregate.aggregate_id
            or aggregate.owner_id != self.job.owner_id
            or aggregate.job_id != self.job.job_id
        ):
            raise ValueError("Publication projection differs from the linked approved job")
        records = (self.attempt, self.permit, self.work)
        if any(record.aggregate_id != aggregate.aggregate_id for record in records):
            raise ValueError("Publication projection rows bind different aggregates")
        if any(
            record.snapshot_id != self.snapshot.snapshot_id
            or record.snapshot_fingerprint != self.snapshot.fingerprint
            for record in records
        ):
            raise ValueError("Publication projection rows bind different snapshots")
        if self.result is not None and self.result.aggregate_id != aggregate.aggregate_id:
            raise ValueError("Publication result binds another aggregate")
        if self.observation is not None and self.observation.aggregate_id != aggregate.aggregate_id:
            raise ValueError("Publication observation binds another aggregate")
        if self.mutation_claim is not None and (
            self.mutation_claim.aggregate_id != aggregate.aggregate_id
        ):
            raise ValueError("Publication mutation claim binds another aggregate")
        if self.post_observation is not None and (
            self.post_observation.aggregate_id != aggregate.aggregate_id
        ):
            raise ValueError("Publication POST observation binds another aggregate")
        if self.notification is not None and (
            self.result is None
            or self.notification.aggregate_id != aggregate.aggregate_id
            or self.notification.result_id != self.result.result_id
            or self.notification.result_fingerprint != self.result.fingerprint
        ):
            raise ValueError("Publication notification differs from the verified result")
        if self.report is not None and self.report.aggregate_id != aggregate.aggregate_id:
            raise ValueError("Publication report binds another aggregate")
        _validate_root_links(self)
        if isinstance(aggregate, PublicationAggregate):
            if not (
                isinstance(self.attempt, PublicationAttempt)
                and isinstance(self.permit, PublicationPermit)
                and isinstance(self.work, PublicationWorkRequest)
                and self.mutation_claim is None
                and self.post_observation is None
                and self.observation is None
                and self.result is None
                and self.notification is None
                and self.report is None
            ):
                raise ValueError("Pristine publication projection cannot mix execution rows")
            return self
        if not (
            isinstance(self.attempt, ExecutionPublicationAttempt)
            and isinstance(self.permit, ExecutionPublicationPermit)
            and isinstance(self.work, ExecutionPublicationWork)
        ):
            raise ValueError("Evolved publication projection requires execution rows")
        _validate_evolved_lifecycle(self)
        terminal = aggregate.state in {
            PublicationState.PUBLISHED,
            PublicationState.PUBLICATION_FAILED,
            PublicationState.PUBLICATION_OUTCOME_UNKNOWN,
        }
        latest_observation_fingerprint = (
            self.observation.fingerprint
            if self.observation is not None
            else self.post_observation.fingerprint
            if self.post_observation is not None
            else None
        )
        if aggregate.last_observation_fingerprint != latest_observation_fingerprint:
            raise ValueError("Projection must load the aggregate's exact latest observation")
        if self.observation is not None and (
            self.post_observation is None
            or self.observation.attempt_id != self.attempt.attempt_id
            or self.observation.snapshot_id != self.snapshot.snapshot_id
            or self.observation.snapshot_fingerprint != self.snapshot.fingerprint
            or self.observation.verification_deadline != self.snapshot.verification_deadline
            or self.observation.observed_at < self.post_observation.observed_at
        ):
            raise ValueError("Projection observation differs from aggregate authority")
        if terminal != (self.report is not None):
            raise ValueError("Terminal publication projection requires its report")
        if self.report is not None and (
            self.report.observation_fingerprint != latest_observation_fingerprint
        ):
            raise ValueError("Publication report differs from the exact last observation")
        if aggregate.state is PublicationState.PUBLISHED:
            if (
                self.observation is None
                or self.result is None
                or self.notification is None
                or aggregate.result_id != self.result.result_id
                or aggregate.notification_id != self.notification.notification_id
                or aggregate.report_id != self.report.report_id
                or self.report.result_fingerprint != self.result.fingerprint
                or self.result.observation_id != self.observation.observation_id
                or self.result.observation_fingerprint != self.observation.fingerprint
                or self.result.numeric_listing_id != self.observation.numeric_listing_id
                or self.result.verified_product_fingerprint
                != self.observation.verified_product_fingerprint
                or self.result.verified_at != self.observation.observed_at
                or self.observation.outcome is not PublicationReadOutcome.POSITIVE_PROOF
                or aggregate.terminal_at < self.observation.observed_at
                or self.notification.created_at != aggregate.terminal_at
            ):
                raise ValueError("Published projection lacks exact positive authority")
        elif self.result is not None or self.notification is not None:
            raise ValueError("Non-published projection cannot expose a result")
        if terminal and (
            aggregate.terminal_at != self.report.terminal_at
            or aggregate.state is not self.report.terminal_state
            or self.job.publication_terminal_state != aggregate.state.value
            or self.job.publication_terminal_at != aggregate.terminal_at
            or self.job.publication_report_id != self.report.report_id
        ):
            raise ValueError("Terminal projection disagrees with durable job summary")
        return self


def _validate_root_links(authority: PublicationProjectionAuthority) -> None:
    job = authority.job
    snapshot = authority.snapshot
    aggregate = authority.aggregate
    attempt = authority.attempt
    permit = authority.permit
    work = authority.work
    if (
        aggregate.snapshot_id != snapshot.snapshot_id
        or aggregate.snapshot_fingerprint != snapshot.fingerprint
        or aggregate.attempt_id != attempt.attempt_id
        or aggregate.permit_id != permit.permit_id
        or aggregate.work_request_id != work.work_request_id
        or aggregate.receipt_id != work.receipt_id
        or aggregate.requested_at != snapshot.requested_at
    ):
        raise ValueError("Publication projection roots differ from the aggregate")
    for record in (attempt, permit, work):
        if (
            record.owner_id != job.owner_id
            or record.job_id != job.job_id
            or record.aggregate_id != aggregate.aggregate_id
            or record.snapshot_id != snapshot.snapshot_id
            or record.snapshot_fingerprint != snapshot.fingerprint
        ):
            raise ValueError("Publication projection child authority is not owner/job exact")
    if (
        attempt.requested_at != snapshot.requested_at
        or attempt.verification_deadline != snapshot.verification_deadline
        or permit.attempt_id != attempt.attempt_id
        or permit.work_request_id != work.work_request_id
        or permit.created_at != snapshot.requested_at
        or (
            isinstance(permit, ExecutionPublicationPermit)
            and permit.verification_deadline != snapshot.verification_deadline
        )
        or work.attempt_id != attempt.attempt_id
        or work.permit_id != permit.permit_id
        or work.verification_deadline != snapshot.verification_deadline
        or work.created_at != snapshot.requested_at
    ):
        raise ValueError("Publication projection child lifecycle differs from the snapshot")


def _validate_evolved_lifecycle(authority: PublicationProjectionAuthority) -> None:
    aggregate = authority.aggregate
    attempt = authority.attempt
    permit = authority.permit
    work = authority.work
    assert isinstance(aggregate, ExecutionPublicationAggregate)
    assert isinstance(attempt, ExecutionPublicationAttempt)
    assert isinstance(permit, ExecutionPublicationPermit)
    assert isinstance(work, ExecutionPublicationWork)
    mutation = authority.mutation_claim
    post = authority.post_observation
    if permit.status is PublicationPermitState.CONSUMED:
        if (
            mutation is None
            or mutation.attempt_id != attempt.attempt_id
            or mutation.snapshot_id != authority.snapshot.snapshot_id
            or mutation.snapshot_fingerprint != authority.snapshot.fingerprint
            or mutation.permit_id != permit.permit_id
            or mutation.work_request_id != work.work_request_id
            or mutation.mutation_claim_id != permit.mutation_claim_id
            or mutation.consumed_permit_fingerprint != permit.fingerprint
            or mutation.authorized_at != permit.consumed_at
            or mutation.verification_deadline != authority.snapshot.verification_deadline
        ):
            raise ValueError("Consumed projection lacks its exact mutation claim")
    elif mutation is not None:
        raise ValueError("Only a consumed permit can expose a mutation claim")
    if post is not None and (
        mutation is None
        or post.attempt_id != attempt.attempt_id
        or post.mutation_claim_id != mutation.mutation_claim_id
        or post.mutation_claim_fingerprint != mutation.fingerprint
        or post.call_claim_id != mutation.call_claim_id
        or post.call_claim_fingerprint != mutation.call_claim_fingerprint
        or post.observed_at < mutation.authorized_at
    ):
        raise ValueError("Publication POST observation differs from mutation authority")
    terminal_statuses = {
        PublicationState.PUBLISHED: PublicationExecutionWorkStatus.SUCCEEDED,
        PublicationState.PUBLICATION_FAILED: PublicationExecutionWorkStatus.FAILED,
        PublicationState.PUBLICATION_OUTCOME_UNKNOWN: (
            PublicationExecutionWorkStatus.OUTCOME_UNKNOWN
        ),
    }
    expected_terminal_work = terminal_statuses.get(aggregate.state)
    if aggregate.verification_deadline != authority.snapshot.verification_deadline:
        raise ValueError("Publication aggregate deadline differs from the snapshot")
    if expected_terminal_work is None:
        if (
            attempt.status is not PublicationAttemptStatus.OPEN
            or attempt.terminal_at is not None
            or work.terminal_at is not None
            or authority.job.publication_terminal_state is not None
        ):
            raise ValueError("Nonterminal projection carries terminal child authority")
        expected_work_statuses = {
            PublicationState.PUBLICATION_REQUESTED: {
                PublicationExecutionWorkStatus.PENDING,
                PublicationExecutionWorkStatus.DISPATCHED,
            },
            PublicationState.PUBLICATION_VERIFYING: {
                PublicationExecutionWorkStatus.VERIFYING,
            },
            PublicationState.PUBLICATION_RECONCILING: {
                PublicationExecutionWorkStatus.RECONCILING,
            },
        }[aggregate.state]
        if work.status not in expected_work_statuses:
            raise ValueError("Nonterminal publication work differs from aggregate state")
        expected_permit_statuses = {
            PublicationState.PUBLICATION_REQUESTED: {
                PublicationPermitState.AVAILABLE,
                PublicationPermitState.CONSUMED,
            },
            PublicationState.PUBLICATION_VERIFYING: {
                PublicationPermitState.CONSUMED,
            },
            PublicationState.PUBLICATION_RECONCILING: {
                PublicationPermitState.CONSUMED,
            },
        }[aggregate.state]
        if permit.status not in expected_permit_statuses:
            raise ValueError("Nonterminal publication permit differs from aggregate state")
        if aggregate.state is PublicationState.PUBLICATION_REQUESTED:
            if post is not None:
                raise ValueError("Requested publication cannot expose a settled POST observation")
        elif post is None:
            raise ValueError("Post-call publication state requires its POST observation")
        elif (
            aggregate.state is PublicationState.PUBLICATION_VERIFYING
            and post.outcome is not PublicationPostOutcome.DEFINITELY_ACCEPTED
        ) or (
            aggregate.state is PublicationState.PUBLICATION_RECONCILING
            and post.outcome is not PublicationPostOutcome.AMBIGUOUS
        ):
            raise ValueError("Post-call observation classification differs from aggregate state")
        return
    expected_terminal_permit_statuses = {
        PublicationState.PUBLISHED: {PublicationPermitState.CONSUMED},
        PublicationState.PUBLICATION_FAILED: {
            PublicationPermitState.RETIRED,
            PublicationPermitState.CONSUMED,
        },
        PublicationState.PUBLICATION_OUTCOME_UNKNOWN: {
            PublicationPermitState.CONSUMED,
        },
    }[aggregate.state]
    if (
        aggregate.state is PublicationState.PUBLICATION_OUTCOME_UNKNOWN
        and aggregate.terminal_at < aggregate.verification_deadline
    ):
        raise ValueError("Unknown publication outcome cannot settle before the fixed deadline")
    if (
        attempt.status is not PublicationAttemptStatus.TERMINAL
        or attempt.terminal_at != aggregate.terminal_at
        or permit.status not in expected_terminal_permit_statuses
        or work.status is not expected_terminal_work
        or work.terminal_at != aggregate.terminal_at
        or authority.report is None
        or authority.job.publication_terminal_state != aggregate.state.value
        or authority.job.publication_terminal_at != aggregate.terminal_at
        or authority.job.publication_source_release_eligible_at
        != aggregate.source_release_eligible_at
        or authority.job.publication_operational_expires_at != aggregate.operational_expires_at
        or authority.job.publication_report_id != aggregate.report_id
        or authority.job.publication_result_id != aggregate.result_id
    ):
        raise ValueError("Terminal publication roots disagree")
    if permit.status is PublicationPermitState.CONSUMED and post is None:
        raise ValueError("Consumed terminal publication requires its POST observation")
    if permit.status is PublicationPermitState.RETIRED and post is not None:
        raise ValueError("Retired publication cannot expose a POST observation")
    report = authority.report
    owner_digest, job_digest = report.identity_digests(
        authority.job.owner_id,
        authority.job.job_id,
    )
    result_fingerprint = authority.result.fingerprint if authority.result else None
    observation_fingerprint = (
        authority.observation.fingerprint
        if authority.observation
        else authority.post_observation.fingerprint
        if authority.post_observation
        else None
    )
    if (
        report.owner_digest != owner_digest
        or report.job_digest != job_digest
        or report.terminal_state is not aggregate.state
        or report.requested_at != aggregate.requested_at
        or report.terminal_at != aggregate.terminal_at
        or report.source_release_eligible_at != aggregate.source_release_eligible_at
        or report.operational_expires_at != aggregate.operational_expires_at
        or report.shop_get_call_count != attempt.shop_get_call_count
        or report.product_get_call_count != attempt.product_get_call_count
        or report.publish_post_call_count != attempt.publish_post_call_count
        or report.release_manifest_fingerprint != authority.snapshot.release_manifest_fingerprint
        or report.snapshot_fingerprint != authority.snapshot.fingerprint
        or report.attempt_fingerprint != attempt.fingerprint
        or report.permit_fingerprint != permit.fingerprint
        or report.observation_fingerprint != observation_fingerprint
        or report.result_fingerprint != result_fingerprint
    ):
        raise ValueError("Terminal publication report differs from exact root authority")


class PublicationProjectionStore(Protocol):
    def get_job_for_owner(self, owner_id: str, job_id: str) -> ControlJobRecord: ...

    def get_publication_authority(
        self,
        owner_id: str,
        aggregate_id: str,
    ) -> PublicationProjectionAuthority: ...


class SellerPublicationProjectionService:
    """Derive a disabled, owner-scoped projection without mutating publication state."""

    def __init__(self, store: PublicationProjectionStore) -> None:
        self._store = store

    def get(self, *, owner_id: str, job_id: str) -> SellerPublicationProjection:
        try:
            job = self._store.get_job_for_owner(owner_id, job_id)
        except NotFoundError:
            raise NotFoundError from None
        except ValidationError:
            raise PublicationProjectionUnavailableError(
                "Publication status is temporarily unavailable"
            ) from None
        try:
            job = ControlJobRecord.model_validate(job.model_dump(mode="python"))
        except (AttributeError, ValidationError, ValueError):
            raise PublicationProjectionUnavailableError(
                "Publication status is temporarily unavailable"
            ) from None
        if job.owner_id != owner_id or job.job_id != job_id:
            raise NotFoundError from None
        if job.publication_aggregate_id is None:
            return self._not_requested(job)
        try:
            authority = self._store.get_publication_authority(
                owner_id,
                job.publication_aggregate_id,
            )
            reparsed = PublicationProjectionAuthority.model_validate(
                authority.model_dump(mode="python")
            )
            if reparsed.job != job:
                raise ValueError("Projection store returned a different current job")
            return self._project(reparsed)
        except NotFoundError:
            raise PublicationProjectionUnavailableError(
                "Publication status is temporarily unavailable"
            ) from None
        except (ValidationError, ValueError):
            raise PublicationProjectionUnavailableError(
                "Publication status is temporarily unavailable"
            ) from None

    @staticmethod
    def _not_requested(job: ControlJobRecord) -> SellerPublicationProjection:
        etag = canonical_fingerprint(
            {
                "kind": "seller_publication_projection",
                "job_fingerprint": _job_fingerprint(job),
                "publication_aggregate_id": None,
            }
        )
        return SellerPublicationProjection(
            job_id=job.job_id,
            state="not_requested",
            stage=SellerPublicationStage.AWAITING_ACTIVATION,
            notification_available=False,
            updated_at=job.updated_at,
            etag=etag,
        )

    @staticmethod
    def _project(authority: PublicationProjectionAuthority) -> SellerPublicationProjection:
        aggregate = authority.aggregate
        if isinstance(aggregate, PublicationAggregate):
            state = PublicationState.PUBLICATION_REQUESTED
            aggregate_version = aggregate.record_version
            attempt_status = PublicationAttemptStatus.OPEN
            deadline = authority.snapshot.verification_deadline
            stage = SellerPublicationStage.QUEUED
            updated_at = aggregate.updated_at
            terminal_at: datetime | None = None
        else:
            state = aggregate.state
            aggregate_version = aggregate.record_version
            attempt_status = authority.attempt.status
            deadline = aggregate.verification_deadline
            updated_at = aggregate.updated_at
            terminal_at = aggregate.terminal_at
            stage = _stage_for(authority)
        result = authority.result
        report = authority.report
        etag = canonical_fingerprint(
            {
                "kind": "seller_publication_projection",
                "job_fingerprint": _job_fingerprint(authority.job),
                "snapshot_fingerprint": authority.snapshot.fingerprint,
                "aggregate_fingerprint": _record_fingerprint(aggregate),
                "attempt_fingerprint": _record_fingerprint(authority.attempt),
                "permit_fingerprint": _record_fingerprint(authority.permit),
                "work_fingerprint": _record_fingerprint(authority.work),
                "mutation_claim_fingerprint": (
                    authority.mutation_claim.fingerprint if authority.mutation_claim else None
                ),
                "post_observation_fingerprint": (
                    authority.post_observation.fingerprint if authority.post_observation else None
                ),
                "observation_fingerprint": (
                    authority.observation.fingerprint if authority.observation else None
                ),
                "result_fingerprint": result.fingerprint if result else None,
                "notification_fingerprint": (
                    authority.notification.fingerprint if authority.notification else None
                ),
                "report_fingerprint": report.fingerprint if report else None,
            }
        )
        return SellerPublicationProjection(
            job_id=authority.job.job_id,
            state=state,
            stage=stage,
            aggregate_record_version=aggregate_version,
            attempt_status=attempt_status,
            verification_deadline=deadline,
            safe_listing_url=result.safe_listing_url if result else None,
            verified_at=result.verified_at if result else None,
            report_id=report.report_id if report else None,
            terminal_at=terminal_at,
            notification_available=authority.notification is not None,
            updated_at=updated_at,
            etag=etag,
        )


def _stage_for(authority: PublicationProjectionAuthority) -> SellerPublicationStage:
    aggregate = authority.aggregate
    permit = authority.permit
    work = authority.work
    assert isinstance(aggregate, ExecutionPublicationAggregate)
    assert isinstance(permit, ExecutionPublicationPermit)
    assert isinstance(work, ExecutionPublicationWork)
    if aggregate.state in {
        PublicationState.PUBLISHED,
        PublicationState.PUBLICATION_FAILED,
        PublicationState.PUBLICATION_OUTCOME_UNKNOWN,
    }:
        return SellerPublicationStage.COMPLETE
    if aggregate.state is PublicationState.PUBLICATION_VERIFYING:
        return SellerPublicationStage.VERIFYING
    if aggregate.state is PublicationState.PUBLICATION_RECONCILING:
        return SellerPublicationStage.RECONCILING
    if work.status is PublicationExecutionWorkStatus.PENDING:
        return SellerPublicationStage.QUEUED
    if permit.status is PublicationPermitState.CONSUMED:
        return SellerPublicationStage.PUBLISHING
    return SellerPublicationStage.PREFLIGHT


def _job_fingerprint(job: ControlJobRecord) -> str:
    return canonical_fingerprint(
        {
            "kind": "publication_projection_control_job",
            "job": job.model_dump(mode="json"),
        }
    )


def _record_fingerprint(record: PublicationModel) -> str:
    fingerprint = getattr(record, "fingerprint", None)
    if isinstance(fingerprint, str):
        return fingerprint
    return canonical_fingerprint(
        {
            "kind": type(record).__name__,
            "record": record.model_dump(mode="json"),
        }
    )
