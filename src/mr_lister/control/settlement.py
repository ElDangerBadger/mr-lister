"""Strong-read settlement for ambiguous Phase 6 PREPARE task outcomes.

AgentCore may complete its transactional Strands route even when the invoking Lambda loses the
response.  This boundary reconciles durable application authority before recording a failure, so
orchestration cannot overwrite or misreport an already completed preparation.
"""

from __future__ import annotations

from typing import Literal, Protocol

from mr_lister.control.commands import RecordWorkerFailureCommand, WorkerFailureCode
from mr_lister.control.errors import (
    ConcurrentControlModificationError,
    InvalidControlStateError,
    WorkNotActiveError,
)
from mr_lister.control.models import (
    AgentPreparationEvidence,
    CommandResponse,
    ControlJobRecord,
    ControlJobState,
    ControlModel,
    FailureRecord,
    WorkRequest,
    WorkRequestStatus,
    WorkType,
)


class PreparationSettlementStore(Protocol):
    def get_job(self, job_id: str) -> ControlJobRecord: ...

    def get_work_request(self, job_id: str, work_request_id: str) -> WorkRequest: ...

    def get_agent_evidence(
        self,
        job_id: str,
        evidence_id: str,
    ) -> AgentPreparationEvidence: ...

    def get_failure(self, job_id: str, failure_id: str) -> FailureRecord: ...


class PreparationFailureControl(Protocol):
    def record_worker_failure(self, command: RecordWorkerFailureCommand) -> CommandResponse: ...


class PreparationSettlementError(Exception):
    """Durable PREPARE authority cannot be reconciled safely."""


class PreparationSettlementResult(ControlModel):
    outcome: Literal["completed_readback", "failure_recorded"]
    response: CommandResponse


class PreparationFailureReconciler:
    """Reconcile timeout/failure only after an exact strong read of Job, Work, and evidence."""

    def __init__(
        self,
        *,
        store: PreparationSettlementStore,
        control: PreparationFailureControl,
        maximum_cas_rechecks: int = 2,
    ) -> None:
        if not 1 <= maximum_cas_rechecks <= 3:
            raise ValueError("PREPARE settlement permits between one and three CAS rechecks")
        self._store = store
        self._control = control
        self._maximum_cas_rechecks = maximum_cas_rechecks

    def settle_unavailable(
        self,
        *,
        job_id: str,
        work_request_id: str,
    ) -> PreparationSettlementResult:
        """Return completed authority or atomically record one sanitized retryable failure."""

        for _ in range(self._maximum_cas_rechecks + 1):
            job = self._store.get_job(job_id)
            work = self._store.get_work_request(job_id, work_request_id)
            completed = self._completed_readback(job=job, work=work)
            if completed is not None:
                return PreparationSettlementResult(
                    outcome="completed_readback",
                    response=completed,
                )
            persisted_failure = self._persisted_failure_readback(job=job, work=work)
            if persisted_failure is not None:
                return PreparationSettlementResult(
                    outcome="failure_recorded",
                    response=persisted_failure,
                )
            if (
                job.active_work_request_id != work_request_id
                or job.state
                not in {
                    ControlJobState.INTAKE_VALIDATED,
                    ControlJobState.ANALYZING_ARTWORK,
                    ControlJobState.LISTING_DRAFTED,
                }
                or work.work_type is not WorkType.PREPARE
                or work.status not in {WorkRequestStatus.CLAIMED, WorkRequestStatus.DISPATCHED}
            ):
                raise PreparationSettlementError(
                    "PREPARE failure does not match active or completed application authority"
                )
            try:
                response = self._control.record_worker_failure(
                    RecordWorkerFailureCommand(
                        job_id=job_id,
                        work_request_id=work_request_id,
                        expected_record_version=job.record_version,
                        code=WorkerFailureCode.INTELLIGENCE_UNAVAILABLE,
                    )
                )
            except (ConcurrentControlModificationError, WorkNotActiveError):
                # The runtime may have checkpointed or completed between the strong read and CAS.
                continue
            except InvalidControlStateError as error:
                raise PreparationSettlementError(
                    "PREPARE failure could not be settled against durable authority"
                ) from error
            return PreparationSettlementResult(outcome="failure_recorded", response=response)
        raise PreparationSettlementError(
            "PREPARE authority kept changing during bounded settlement"
        )

    def _completed_readback(
        self,
        *,
        job: ControlJobRecord,
        work: WorkRequest,
    ) -> CommandResponse | None:
        if work.status is not WorkRequestStatus.COMPLETED or work.work_type is not WorkType.PREPARE:
            return None
        if work.last_error_code is not None or job.agent_evidence_id is None:
            return None
        if job.state not in {
            ControlJobState.PRODUCT_DRAFT_SYNCING,
            ControlJobState.NEEDS_REVISION,
        }:
            return None
        evidence = self._store.get_agent_evidence(job.job_id, job.agent_evidence_id)
        exact = (
            evidence.evidence_id == job.agent_evidence_id
            and evidence.fingerprint == job.agent_evidence_fingerprint
            and evidence.job_id == job.job_id
            and evidence.work_request_id == work.work_request_id
            and evidence.review_version == job.review_version
            and evidence.framework == "strands-agents"
            and evidence.agent_id == "mr-lister-preparation"
            and evidence.tool_calls == ("record_prepared_review",)
        )
        if not exact:
            raise PreparationSettlementError(
                "Completed PREPARE work does not match immutable Strands evidence"
            )
        if job.state is ControlJobState.NEEDS_REVISION:
            if job.active_work_request_id is not None:
                raise PreparationSettlementError(
                    "Revision routing cannot retain active machine work"
                )
            next_work_id = None
        else:
            next_work_id = job.active_work_request_id
            if next_work_id is None or next_work_id == work.work_request_id:
                raise PreparationSettlementError(
                    "Completed preparation did not create product synchronization work"
                )
            next_work = self._store.get_work_request(job.job_id, next_work_id)
            if (
                next_work.owner_id != job.owner_id
                or next_work.job_id != job.job_id
                or next_work.work_type is not WorkType.SYNCHRONIZE_PRODUCT
                or next_work.review_version != job.review_version
                or next_work.status
                not in {
                    WorkRequestStatus.PENDING,
                    WorkRequestStatus.CLAIMED,
                    WorkRequestStatus.DISPATCHED,
                }
            ):
                raise PreparationSettlementError(
                    "Completed preparation has invalid follow-up work authority"
                )
        return CommandResponse(
            job_id=job.job_id,
            state=job.state,
            record_version=job.record_version,
            review_version=job.review_version,
            work_request_id=next_work_id,
        )

    def _persisted_failure_readback(
        self,
        *,
        job: ControlJobRecord,
        work: WorkRequest,
    ) -> CommandResponse | None:
        if (
            job.state not in {ControlJobState.FAILED_RETRYABLE, ControlJobState.FAILED_TERMINAL}
            or job.failure_id is None
            or work.status is not WorkRequestStatus.COMPLETED
        ):
            return None
        failure = self._store.get_failure(job.job_id, job.failure_id)
        if (
            failure.job_id != job.job_id
            or failure.work_request_id != work.work_request_id
            or failure.code != work.last_error_code
        ):
            raise PreparationSettlementError(
                "Persisted PREPARE failure does not match completed work"
            )
        return CommandResponse(
            job_id=job.job_id,
            state=job.state,
            record_version=job.record_version,
            review_version=job.review_version,
            work_request_id=None,
        )
