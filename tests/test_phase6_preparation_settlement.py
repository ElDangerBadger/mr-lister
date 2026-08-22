from __future__ import annotations

from datetime import UTC, datetime

import pytest

from mr_lister.control.commands import RecordWorkerFailureCommand
from mr_lister.control.dispatch import deterministic_execution_name, work_input_fingerprint
from mr_lister.control.errors import ConcurrentControlModificationError
from mr_lister.control.models import (
    AgentPreparationEvidence,
    CommandResponse,
    ControlJobRecord,
    ControlJobState,
    FailureRecord,
    WorkRequest,
    WorkRequestStatus,
    WorkType,
)
from mr_lister.control.settlement import (
    PreparationFailureReconciler,
    PreparationSettlementError,
)

NOW = datetime(2026, 8, 22, 12, tzinfo=UTC)
OWNER = "a" * 64
JOB_ID = "job_prepare_settlement"
WORK_ID = "work_prepare_settlement"
REVIEW_FP = "b" * 64
EVIDENCE_ID = "agent_prepare_settlement"
EVIDENCE_FP = "c" * 64


def _work(status: WorkRequestStatus) -> WorkRequest:
    return WorkRequest(
        work_request_id=WORK_ID,
        owner_id=OWNER,
        job_id=JOB_ID,
        receipt_id="receipt_prepare_settlement",
        work_type=WorkType.PREPARE,
        review_version=1,
        input_fingerprint=work_input_fingerprint(
            work_type=WorkType.PREPARE,
            job_id=JOB_ID,
            work_request_id=WORK_ID,
        ),
        execution_name=deterministic_execution_name(WORK_ID),
        status=status,
        attempt_count=1,
        execution_arn=(
            "arn:aws:states:us-west-2:123456789012:execution:"
            "mr-lister-phase6-prepare:prepare-settlement"
        ),
        next_dispatch_at=NOW,
        created_at=NOW,
        updated_at=NOW,
    )


def _evidence(*, fingerprint: str = EVIDENCE_FP) -> AgentPreparationEvidence:
    return AgentPreparationEvidence(
        evidence_id=EVIDENCE_ID,
        job_id=JOB_ID,
        work_request_id=WORK_ID,
        review_version=1,
        correlation_id="d" * 24,
        framework="strands-agents",
        agent_id="mr-lister-preparation",
        controller_model_id="amazon.nova-2-lite-v1:0",
        tool_calls=("record_prepared_review",),
        cycles=2,
        input_tokens=500,
        output_tokens=100,
        total_tokens=600,
        decision_fingerprint="e" * 64,
        fingerprint=fingerprint,
        created_at=NOW,
    )


def _completed_job(*, evidence_fingerprint: str = EVIDENCE_FP) -> ControlJobRecord:
    return ControlJobRecord(
        owner_id=OWNER,
        job_id=JOB_ID,
        record_version=3,
        event_sequence=4,
        state=ControlJobState.NEEDS_REVISION,
        review_version=1,
        review_fingerprint=REVIEW_FP,
        source_artifact_fingerprint="f" * 64,
        artwork_analysis_id="analysis_prepare_settlement",
        artwork_analysis_fingerprint="1" * 64,
        agent_evidence_id=EVIDENCE_ID,
        agent_evidence_fingerprint=evidence_fingerprint,
        created_at=NOW,
        updated_at=NOW,
    )


def _checkpoint_job() -> ControlJobRecord:
    return ControlJobRecord(
        owner_id=OWNER,
        job_id=JOB_ID,
        record_version=2,
        event_sequence=3,
        state=ControlJobState.LISTING_DRAFTED,
        review_version=1,
        review_fingerprint=REVIEW_FP,
        review_validated=False,
        source_artifact_fingerprint="f" * 64,
        artwork_analysis_id="analysis_prepare_settlement",
        artwork_analysis_fingerprint="1" * 64,
        active_work_request_id=WORK_ID,
        created_at=NOW,
        updated_at=NOW,
    )


class FakeStore:
    def __init__(self, *, job: ControlJobRecord, work: WorkRequest) -> None:
        self.job = job
        self.work = work
        self.evidence = _evidence()
        self.failure: FailureRecord | None = None

    def get_job(self, job_id: str) -> ControlJobRecord:
        assert job_id == JOB_ID
        return self.job

    def get_work_request(self, job_id: str, work_request_id: str) -> WorkRequest:
        assert (job_id, work_request_id) == (JOB_ID, WORK_ID)
        return self.work

    def get_agent_evidence(self, job_id: str, evidence_id: str) -> AgentPreparationEvidence:
        assert (job_id, evidence_id) == (JOB_ID, EVIDENCE_ID)
        return self.evidence

    def get_failure(self, job_id: str, failure_id: str) -> FailureRecord:
        assert job_id == JOB_ID and self.failure is not None
        assert failure_id == self.failure.failure_id
        return self.failure


class NeverFailControl:
    def record_worker_failure(self, command: RecordWorkerFailureCommand) -> CommandResponse:
        del command
        raise AssertionError("completed Strands authority must not record a failure")


class RecordingFailureControl:
    def __init__(self) -> None:
        self.commands: list[RecordWorkerFailureCommand] = []

    def record_worker_failure(self, command: RecordWorkerFailureCommand) -> CommandResponse:
        self.commands.append(command)
        return CommandResponse(
            job_id=JOB_ID,
            state=ControlJobState.FAILED_RETRYABLE,
            record_version=3,
            review_version=1,
        )


def test_timeout_after_runtime_commit_reconciles_completed_strands_route() -> None:
    store = FakeStore(job=_completed_job(), work=_work(WorkRequestStatus.COMPLETED))

    result = PreparationFailureReconciler(
        store=store,
        control=NeverFailControl(),
    ).settle_unavailable(job_id=JOB_ID, work_request_id=WORK_ID)

    assert result.outcome == "completed_readback"
    assert result.response.state is ControlJobState.NEEDS_REVISION
    assert result.response.record_version == 3


def test_checkpoint_only_failure_records_stage_aware_retryable_failure() -> None:
    store = FakeStore(job=_checkpoint_job(), work=_work(WorkRequestStatus.DISPATCHED))
    control = RecordingFailureControl()

    result = PreparationFailureReconciler(
        store=store,
        control=control,
    ).settle_unavailable(job_id=JOB_ID, work_request_id=WORK_ID)

    assert result.outcome == "failure_recorded"
    assert len(control.commands) == 1
    assert control.commands[0].expected_record_version == 2
    assert control.commands[0].code.value == "INTELLIGENCE_UNAVAILABLE"


class RuntimeWinsRaceControl:
    def __init__(self, store: FakeStore) -> None:
        self.store = store
        self.calls = 0

    def record_worker_failure(self, command: RecordWorkerFailureCommand) -> CommandResponse:
        del command
        self.calls += 1
        self.store.job = _completed_job()
        self.store.work = _work(WorkRequestStatus.COMPLETED)
        raise ConcurrentControlModificationError("runtime completed first")


def test_runtime_completion_winning_failure_cas_is_rechecked_as_success() -> None:
    store = FakeStore(job=_checkpoint_job(), work=_work(WorkRequestStatus.DISPATCHED))
    control = RuntimeWinsRaceControl(store)

    result = PreparationFailureReconciler(
        store=store,
        control=control,
    ).settle_unavailable(job_id=JOB_ID, work_request_id=WORK_ID)

    assert control.calls == 1
    assert result.outcome == "completed_readback"
    assert result.response.state is ControlJobState.NEEDS_REVISION


def test_completed_work_with_mismatched_evidence_fails_closed() -> None:
    store = FakeStore(
        job=_completed_job(evidence_fingerprint="9" * 64),
        work=_work(WorkRequestStatus.COMPLETED),
    )

    with pytest.raises(PreparationSettlementError, match="Strands evidence"):
        PreparationFailureReconciler(
            store=store,
            control=NeverFailControl(),
        ).settle_unavailable(job_id=JOB_ID, work_request_id=WORK_ID)
