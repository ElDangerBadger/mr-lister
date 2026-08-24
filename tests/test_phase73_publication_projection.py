"""Read-only Phase 7.3 seller publication projection tests."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

import pytest

from mr_lister.control.errors import NotFoundError
from mr_lister.control.fingerprints import publication_terminal_summary_fingerprint
from mr_lister.control.models import ControlJobRecord
from mr_lister.publication.contract import PublicationState
from mr_lister.publication.execution_commands import (
    RecordPublicationPostOutcomeCommand,
    RecordPublicationProductObservationCommand,
    SettlePublicationDeadlineCommand,
)
from mr_lister.publication.execution_fingerprints import execution_record_fingerprint
from mr_lister.publication.execution_models import (
    ExecutionPublicationAttempt,
    PublicationAttemptStatus,
    PublicationCallPurpose,
    PublicationMutationClaim,
    PublicationNotification,
    PublicationPostObservation,
    PublicationResult,
    PublicationTerminalReport,
    safe_listing_link_fingerprint,
)
from mr_lister.publication.projection import (
    PublicationProjectionAuthority,
    PublicationProjectionUnavailableError,
    SellerPublicationProjectionService,
)
from mr_lister.publication.projection_models import SellerPublicationStage
from tests.test_phase71_publication_store import OWNER_ID
from tests.test_phase72_publication_execution import Harness


@dataclass
class ProjectionStore:
    job: ControlJobRecord
    authority: PublicationProjectionAuthority | None = None
    job_reads: int = 0
    publication_reads: int = 0

    def get_job_for_owner(self, owner_id: str, job_id: str) -> ControlJobRecord:
        self.job_reads += 1
        if self.job.owner_id != owner_id or self.job.job_id != job_id:
            raise NotFoundError
        return self.job

    def get_publication_authority(
        self,
        owner_id: str,
        aggregate_id: str,
    ) -> PublicationProjectionAuthority:
        self.publication_reads += 1
        if (
            self.authority is None
            or self.authority.job.owner_id != owner_id
            or self.authority.aggregate.aggregate_id != aggregate_id
        ):
            raise NotFoundError
        return self.authority


def _authority(harness: Harness, *, use_stored_rows: bool) -> PublicationProjectionAuthority:
    current = harness.authority
    aggregate_id = harness.aggregate_id
    if use_stored_rows:
        aggregate = harness.store.aggregates[aggregate_id]
        attempt = harness.store.attempts[aggregate_id]
        permit = harness.store.permits[aggregate_id]
        work = harness.store.work[aggregate_id]
    else:
        aggregate = current.aggregate
        attempt = current.attempt
        permit = current.permit
        work = current.work
    return PublicationProjectionAuthority(
        job=harness.store.jobs[current.snapshot.job_id],
        snapshot=current.snapshot,
        aggregate=aggregate,
        attempt=attempt,
        permit=permit,
        work=work,
        mutation_claim=current.mutation_claim,
        post_observation=current.post_observation,
        observation=current.last_product_observation,
        result=current.result,
        notification=current.notification,
        report=current.report,
    )


def test_unrequested_projection_checks_owner_first_and_remains_disabled() -> None:
    harness = Harness()
    job = harness.transaction.authority.current_job
    store = ProjectionStore(job)
    service = SellerPublicationProjectionService(store)

    projection = service.get(owner_id=OWNER_ID, job_id=job.job_id)

    assert projection.state == "not_requested"
    assert projection.stage is SellerPublicationStage.AWAITING_ACTIVATION
    assert projection.publication_enabled is False
    assert projection.request_enabled is False
    assert projection.request_disabled_reason == "PUBLICATION_NOT_ACTIVATED"
    assert store.job_reads == 1
    assert store.publication_reads == 0

    with pytest.raises(NotFoundError):
        service.get(owner_id="b" * 64, job_id=job.job_id)
    assert store.publication_reads == 0


def test_unrequested_projection_deep_reparses_current_job_before_fast_path() -> None:
    harness = Harness()
    job = harness.transaction.authority.current_job
    forged = job.model_copy(update={"publication_terminal_state": "published"})

    with pytest.raises(PublicationProjectionUnavailableError):
        SellerPublicationProjectionService(ProjectionStore(forged)).get(
            owner_id=OWNER_ID,
            job_id=forged.job_id,
        )


def test_pristine_request_projects_queued_without_mutating_or_exposing_provider_ids() -> None:
    harness = Harness()
    authority = _authority(harness, use_stored_rows=True)
    store = ProjectionStore(authority.job, authority)

    projection = SellerPublicationProjectionService(store).get(
        owner_id=OWNER_ID,
        job_id=authority.job.job_id,
    )

    assert projection.state is PublicationState.PUBLICATION_REQUESTED
    assert projection.stage is SellerPublicationStage.QUEUED
    assert projection.aggregate_record_version == 0
    assert projection.safe_listing_url is None
    assert projection.notification_available is False
    serialized = projection.model_dump_json()
    assert "printify" not in serialized.casefold()
    assert OWNER_ID not in serialized


def test_dispatched_requested_projection_is_preflight_and_etag_changes_with_authority() -> None:
    harness = Harness()
    initial_authority = _authority(harness, use_stored_rows=True)
    initial = SellerPublicationProjectionService(
        ProjectionStore(initial_authority.job, initial_authority)
    ).get(owner_id=OWNER_ID, job_id=initial_authority.job.job_id)

    harness.dispatch_and_reconstruct()
    current_authority = _authority(harness, use_stored_rows=False)
    current = SellerPublicationProjectionService(
        ProjectionStore(current_authority.job, current_authority)
    ).get(owner_id=OWNER_ID, job_id=current_authority.job.job_id)

    assert current.state is PublicationState.PUBLICATION_REQUESTED
    assert current.stage is SellerPublicationStage.PREFLIGHT
    assert current.aggregate_record_version > initial.aggregate_record_version
    assert current.etag != initial.etag


def test_pre_dispatch_deadline_projects_closed_failed_terminal_summary() -> None:
    harness = Harness()
    harness.clock.now = harness.authority.snapshot.verification_deadline
    from mr_lister.publication.execution_commands import SettlePublicationDeadlineCommand

    harness.service.settle_deadline(
        harness.command(SettlePublicationDeadlineCommand, "projection_deadline")
    )
    authority = _authority(harness, use_stored_rows=False)

    projection = SellerPublicationProjectionService(ProjectionStore(authority.job, authority)).get(
        owner_id=OWNER_ID, job_id=authority.job.job_id
    )

    assert projection.state is PublicationState.PUBLICATION_FAILED
    assert projection.stage is SellerPublicationStage.COMPLETE
    assert projection.report_id == authority.report.report_id
    assert projection.terminal_at == authority.aggregate.terminal_at
    assert projection.safe_listing_url is None
    assert projection.notification_available is False


def test_published_projection_derives_etsy_link_from_exact_positive_observation() -> None:
    harness = Harness()
    harness.dispatch_and_reconstruct()
    harness.complete_preflight()
    _, post_claim = harness.claim_publish()
    harness.service.record_post_outcome(
        harness.command(
            RecordPublicationPostOutcomeCommand,
            "projection_accepted_post",
            evidence=harness.publish_evidence(post_claim, accepted=True),
        )
    )
    _, product_claim = harness.claim_product(PublicationCallPurpose.VERIFICATION)
    harness.service.record_product_observation(
        harness.command(
            RecordPublicationProductObservationCommand,
            "projection_positive_product",
            evidence=harness.product_evidence(product_claim, positive=True),
        )
    )
    authority = _authority(harness, use_stored_rows=False)

    projection = SellerPublicationProjectionService(ProjectionStore(authority.job, authority)).get(
        owner_id=OWNER_ID,
        job_id=authority.job.job_id,
    )

    assert projection.state is PublicationState.PUBLISHED
    assert projection.safe_listing_url == "https://www.etsy.com/listing/123456789"
    assert projection.verified_at == authority.observation.observed_at
    assert projection.notification_available is True


def test_accepted_post_projects_verifying_before_any_product_observation() -> None:
    harness = Harness()
    harness.dispatch_and_reconstruct()
    harness.complete_preflight()
    _, post_claim = harness.claim_publish()
    harness.service.record_post_outcome(
        harness.command(
            RecordPublicationPostOutcomeCommand,
            "projection_verifying_post",
            evidence=harness.publish_evidence(post_claim, accepted=True),
        )
    )
    authority = _authority(harness, use_stored_rows=False)

    projection = SellerPublicationProjectionService(ProjectionStore(authority.job, authority)).get(
        owner_id=OWNER_ID,
        job_id=authority.job.job_id,
    )

    assert authority.observation is None
    assert projection.state is PublicationState.PUBLICATION_VERIFYING
    assert projection.stage is SellerPublicationStage.VERIFYING
    assert projection.safe_listing_url is None


def test_published_projection_rejects_refingerprinted_result_not_derived_from_observation() -> None:
    harness = Harness()
    harness.dispatch_and_reconstruct()
    harness.complete_preflight()
    _, post_claim = harness.claim_publish()
    harness.service.record_post_outcome(
        harness.command(
            RecordPublicationPostOutcomeCommand,
            "projection_forged_post",
            evidence=harness.publish_evidence(post_claim, accepted=True),
        )
    )
    _, product_claim = harness.claim_product(PublicationCallPurpose.VERIFICATION)
    harness.service.record_product_observation(
        harness.command(
            RecordPublicationProductObservationCommand,
            "projection_forged_product",
            evidence=harness.product_evidence(product_claim, positive=True),
        )
    )
    authority = _authority(harness, use_stored_rows=False)
    assert authority.result is not None
    assert authority.notification is not None
    assert authority.report is not None

    result_values = authority.result.model_dump(
        mode="python",
        exclude={
            "contract_version",
            "numeric_listing_id",
            "canonical_link_fingerprint",
            "fingerprint",
        },
    )
    result_values.update(
        numeric_listing_id=987654321,
        canonical_link_fingerprint=safe_listing_link_fingerprint(987654321),
    )
    forged_result = PublicationResult(
        **result_values,
        fingerprint=execution_record_fingerprint("publication_result", result_values),
    )
    notification_values = authority.notification.model_dump(
        mode="python",
        exclude={"contract_version", "result_fingerprint", "fingerprint"},
    )
    notification_values["result_fingerprint"] = forged_result.fingerprint
    forged_notification = PublicationNotification(
        **notification_values,
        fingerprint=execution_record_fingerprint(
            "publication_notification",
            notification_values,
        ),
    )
    report_values = authority.report.model_dump(
        mode="python",
        exclude={"contract_version", "result_fingerprint", "fingerprint"},
    )
    report_values["result_fingerprint"] = forged_result.fingerprint
    forged_report = PublicationTerminalReport(
        **report_values,
        fingerprint=execution_record_fingerprint("terminal_report", report_values),
    )
    forged_authority = authority.model_copy(
        update={
            "result": forged_result,
            "notification": forged_notification,
            "report": forged_report,
        }
    )

    service = SellerPublicationProjectionService(ProjectionStore(authority.job, forged_authority))
    with pytest.raises(PublicationProjectionUnavailableError):
        service.get(owner_id=OWNER_ID, job_id=authority.job.job_id)


def test_published_projection_rejects_cross_owner_open_attempt_root() -> None:
    harness = Harness()
    harness.dispatch_and_reconstruct()
    harness.complete_preflight()
    _, post_claim = harness.claim_publish()
    harness.service.record_post_outcome(
        harness.command(
            RecordPublicationPostOutcomeCommand,
            "projection_attempt_post",
            evidence=harness.publish_evidence(post_claim, accepted=True),
        )
    )
    _, product_claim = harness.claim_product(PublicationCallPurpose.VERIFICATION)
    harness.service.record_product_observation(
        harness.command(
            RecordPublicationProductObservationCommand,
            "projection_attempt_product",
            evidence=harness.product_evidence(product_claim, positive=True),
        )
    )
    authority = _authority(harness, use_stored_rows=False)
    attempt_values = authority.attempt.model_dump(
        mode="python",
        exclude={"contract_version", "owner_id", "status", "terminal_at", "fingerprint"},
    )
    attempt_values.update(
        owner_id="b" * 64,
        status=PublicationAttemptStatus.OPEN,
        terminal_at=None,
    )
    forged_attempt = ExecutionPublicationAttempt(
        **attempt_values,
        fingerprint=execution_record_fingerprint("execution_attempt", attempt_values),
    )
    forged_authority = authority.model_copy(update={"attempt": forged_attempt})

    with pytest.raises(PublicationProjectionUnavailableError):
        SellerPublicationProjectionService(ProjectionStore(authority.job, forged_authority)).get(
            owner_id=OWNER_ID, job_id=authority.job.job_id
        )


def test_published_projection_rejects_refingerprinted_mutation_time_chain() -> None:
    harness = Harness()
    harness.dispatch_and_reconstruct()
    harness.complete_preflight()
    _, post_claim = harness.claim_publish()
    harness.service.record_post_outcome(
        harness.command(
            RecordPublicationPostOutcomeCommand,
            "projection_mutation_time_post",
            evidence=harness.publish_evidence(post_claim, accepted=True),
        )
    )
    _, product_claim = harness.claim_product(PublicationCallPurpose.VERIFICATION)
    harness.service.record_product_observation(
        harness.command(
            RecordPublicationProductObservationCommand,
            "projection_mutation_time_product",
            evidence=harness.product_evidence(product_claim, positive=True),
        )
    )
    authority = _authority(harness, use_stored_rows=False)
    mutation = authority.mutation_claim
    post = authority.post_observation
    assert mutation is not None and post is not None

    mutation_values = mutation.model_dump(
        mode="python",
        exclude={"contract_version", "authorized_at", "fingerprint"},
    )
    mutation_values["authorized_at"] = mutation.authorized_at + timedelta(seconds=1)
    forged_mutation = PublicationMutationClaim(
        **mutation_values,
        fingerprint=execution_record_fingerprint("mutation_claim", mutation_values),
    )
    post_values = post.model_dump(
        mode="python",
        exclude={"contract_version", "mutation_claim_fingerprint", "fingerprint"},
    )
    post_values["mutation_claim_fingerprint"] = forged_mutation.fingerprint
    forged_post = PublicationPostObservation(
        **post_values,
        fingerprint=execution_record_fingerprint("post_observation", post_values),
    )
    forged_authority = authority.model_copy(
        update={"mutation_claim": forged_mutation, "post_observation": forged_post}
    )

    with pytest.raises(PublicationProjectionUnavailableError):
        SellerPublicationProjectionService(ProjectionStore(authority.job, forged_authority)).get(
            owner_id=OWNER_ID, job_id=authority.job.job_id
        )


def test_outcome_unknown_projection_cannot_settle_before_fixed_deadline() -> None:
    harness = Harness()
    harness.dispatch_and_reconstruct()
    harness.complete_preflight()
    _, post_claim = harness.claim_publish()
    harness.service.record_post_outcome(
        harness.command(
            RecordPublicationPostOutcomeCommand,
            "projection_unknown_post",
            evidence=harness.publish_evidence(post_claim, accepted=False),
        )
    )
    harness.clock.now = harness.authority.snapshot.verification_deadline
    harness.service.settle_deadline(
        harness.command(SettlePublicationDeadlineCommand, "projection_unknown_deadline")
    )
    authority = _authority(harness, use_stored_rows=False)
    terminal_at = authority.snapshot.verification_deadline - timedelta(seconds=1)
    source_release_at = terminal_at + timedelta(days=30)
    operational_expires_at = terminal_at + timedelta(days=90)

    aggregate_values = authority.aggregate.model_dump(
        mode="python",
        exclude={
            "contract_version",
            "terminal_at",
            "source_release_eligible_at",
            "operational_expires_at",
            "updated_at",
            "fingerprint",
        },
    )
    aggregate_values.update(
        terminal_at=terminal_at,
        source_release_eligible_at=source_release_at,
        operational_expires_at=operational_expires_at,
        updated_at=terminal_at,
    )
    forged_aggregate = type(authority.aggregate)(
        **aggregate_values,
        fingerprint=execution_record_fingerprint("execution_aggregate", aggregate_values),
    )
    attempt_values = authority.attempt.model_dump(
        mode="python",
        exclude={"contract_version", "terminal_at", "fingerprint"},
    )
    attempt_values["terminal_at"] = terminal_at
    forged_attempt = type(authority.attempt)(
        **attempt_values,
        fingerprint=execution_record_fingerprint("execution_attempt", attempt_values),
    )
    work_values = authority.work.model_dump(
        mode="python",
        exclude={"contract_version", "terminal_at", "updated_at", "fingerprint"},
    )
    work_values.update(terminal_at=terminal_at, updated_at=terminal_at)
    forged_work = type(authority.work)(
        **work_values,
        fingerprint=execution_record_fingerprint("execution_work", work_values),
    )
    assert authority.report is not None
    report_values = authority.report.model_dump(
        mode="python",
        exclude={
            "contract_version",
            "terminal_at",
            "source_release_eligible_at",
            "operational_expires_at",
            "attempt_fingerprint",
            "fingerprint",
        },
    )
    report_values.update(
        terminal_at=terminal_at,
        source_release_eligible_at=source_release_at,
        operational_expires_at=operational_expires_at,
        attempt_fingerprint=forged_attempt.fingerprint,
    )
    forged_report = PublicationTerminalReport(
        **report_values,
        fingerprint=execution_record_fingerprint("terminal_report", report_values),
    )
    job = authority.job
    job_values = job.model_dump(
        mode="python",
        exclude={
            "contract_version",
            "publication_terminal_at",
            "publication_source_release_eligible_at",
            "publication_operational_expires_at",
            "publication_terminal_summary_fingerprint",
            "updated_at",
        },
    )
    job_values.update(
        publication_terminal_at=terminal_at,
        publication_source_release_eligible_at=source_release_at,
        publication_operational_expires_at=operational_expires_at,
        publication_terminal_summary_fingerprint=publication_terminal_summary_fingerprint(
            aggregate_id=job.publication_aggregate_id,
            terminal_state=job.publication_terminal_state,
            terminal_at=terminal_at,
            source_release_eligible_at=source_release_at,
            operational_expires_at=operational_expires_at,
            report_id=job.publication_report_id,
            result_id=job.publication_result_id,
        ),
        updated_at=terminal_at,
    )
    forged_job = ControlJobRecord(**job_values)
    forged_authority = authority.model_copy(
        update={
            "job": forged_job,
            "aggregate": forged_aggregate,
            "attempt": forged_attempt,
            "work": forged_work,
            "report": forged_report,
        }
    )

    with pytest.raises(PublicationProjectionUnavailableError):
        SellerPublicationProjectionService(ProjectionStore(forged_job, forged_authority)).get(
            owner_id=OWNER_ID,
            job_id=forged_job.job_id,
        )


def test_published_projection_rejects_job_summary_for_another_result() -> None:
    harness = Harness()
    harness.dispatch_and_reconstruct()
    harness.complete_preflight()
    _, post_claim = harness.claim_publish()
    harness.service.record_post_outcome(
        harness.command(
            RecordPublicationPostOutcomeCommand,
            "projection_job_post",
            evidence=harness.publish_evidence(post_claim, accepted=True),
        )
    )
    _, product_claim = harness.claim_product(PublicationCallPurpose.VERIFICATION)
    harness.service.record_product_observation(
        harness.command(
            RecordPublicationProductObservationCommand,
            "projection_job_product",
            evidence=harness.product_evidence(product_claim, positive=True),
        )
    )
    authority = _authority(harness, use_stored_rows=False)
    job = authority.job
    assert job.publication_terminal_state is not None
    assert job.publication_terminal_at is not None
    assert job.publication_source_release_eligible_at is not None
    assert job.publication_operational_expires_at is not None
    assert job.publication_report_id is not None
    job_values = job.model_dump(
        mode="python",
        exclude={
            "contract_version",
            "publication_result_id",
            "publication_terminal_summary_fingerprint",
        },
    )
    job_values["publication_result_id"] = "foreign_result"
    job_values["publication_terminal_summary_fingerprint"] = (
        publication_terminal_summary_fingerprint(
            aggregate_id=job.publication_aggregate_id,
            terminal_state=job.publication_terminal_state,
            terminal_at=job.publication_terminal_at,
            source_release_eligible_at=job.publication_source_release_eligible_at,
            operational_expires_at=job.publication_operational_expires_at,
            report_id=job.publication_report_id,
            result_id="foreign_result",
        )
    )
    forged_job = ControlJobRecord(**job_values)
    forged_authority = authority.model_copy(update={"job": forged_job})

    with pytest.raises(PublicationProjectionUnavailableError):
        SellerPublicationProjectionService(ProjectionStore(forged_job, forged_authority)).get(
            owner_id=OWNER_ID, job_id=forged_job.job_id
        )


def test_missing_or_corrupt_child_authority_is_generic_unavailable() -> None:
    harness = Harness()
    authority = _authority(harness, use_stored_rows=True)
    service = SellerPublicationProjectionService(ProjectionStore(authority.job, None))

    with pytest.raises(PublicationProjectionUnavailableError) as missing:
        service.get(owner_id=OWNER_ID, job_id=authority.job.job_id)
    assert str(missing.value) == "Publication status is temporarily unavailable"

    forged = authority.model_copy(
        update={"snapshot": authority.snapshot.model_copy(update={"job_id": "wrong_job"})}
    )
    corrupt = SellerPublicationProjectionService(ProjectionStore(authority.job, forged))
    with pytest.raises(PublicationProjectionUnavailableError) as invalid:
        corrupt.get(owner_id=OWNER_ID, job_id=authority.job.job_id)
    assert str(invalid.value) == str(missing.value)


def test_projection_surface_contains_no_write_or_provider_capability() -> None:
    import mr_lister.publication.projection as projection_module

    source = __import__("inspect").getsource(projection_module)
    forbidden = {
        "publish_exact_product",
        "commit_execution",
        "commit_provider_audit",
        "SecretStr",
        "boto3",
        "urllib",
    }
    assert not any(token in source for token in forbidden)
    assert not {
        name
        for name in vars(SellerPublicationProjectionService)
        if name.startswith(("publish", "request", "stage", "consume", "commit"))
    }
