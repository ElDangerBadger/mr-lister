from __future__ import annotations

from base64 import b64decode

import pytest

from mr_lister.contracts import JobRecord, JobState
from mr_lister.workflow.errors import ConcurrentModificationError, InvalidStateError
from mr_lister.workflow.models import WorkflowEvent
from mr_lister.workflow.store import InMemoryJobStore

SYNTHETIC_PNG = b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAIAAAACCAYAAABytg0kAAAAFUlEQVR4nGNU07X8z8DAwMAEIkAYABbVAY+Z/lCyAAAAAElFTkSuQmCC"
)


def updated_job(current: JobRecord, target: JobState, **updates: object) -> JobRecord:
    payload = current.model_dump()
    payload.update(updates)
    payload["state"] = target
    payload["record_version"] = current.record_version + 1
    payload["event_sequence"] = current.event_sequence + 1
    return JobRecord.model_validate(payload)


def transition_event(updated: JobRecord) -> WorkflowEvent:
    return WorkflowEvent(
        sequence=updated.event_sequence,
        occurred_at=updated.updated_at,
        name="state_changed",
    )


def test_workflow_transitions_increment_record_version(workflow) -> None:
    job = workflow.intake(
        filename="geometric_badger.png",
        content_type="image/png",
        content=SYNTHETIC_PNG,
        idempotency_key="versioned-intake",
        profile_id="synthetic_gildan_5000",
    )

    assert job.state is JobState.INTAKE_VALIDATED
    assert job.record_version == 1


def test_store_rejects_a_stale_compare_and_set(workflow) -> None:
    current = workflow.intake(
        filename="geometric_badger.png",
        content_type="image/png",
        content=SYNTHETIC_PNG,
        idempotency_key="stale-transition",
        profile_id="synthetic_gildan_5000",
    )
    store: InMemoryJobStore = workflow.store
    updated = updated_job(current, JobState.ANALYZING_ARTWORK)

    store.commit_transition(current=current, updated=updated, event=transition_event(updated))

    with pytest.raises(ConcurrentModificationError):
        store.commit_transition(current=current, updated=updated, event=transition_event(updated))


def test_store_rejects_transition_outside_application_graph(workflow) -> None:
    current = workflow.intake(
        filename="geometric_badger.png",
        content_type="image/png",
        content=SYNTHETIC_PNG,
        idempotency_key="illegal-transition",
        profile_id="synthetic_gildan_5000",
    )
    updated = updated_job(current, JobState.CANCELLED)

    with pytest.raises(InvalidStateError, match="Cannot transition"):
        workflow.store.commit_transition(
            current=current,
            updated=updated,
            event=transition_event(updated),
        )


def test_store_rejects_identity_changes_during_transition(workflow) -> None:
    current = workflow.intake(
        filename="geometric_badger.png",
        content_type="image/png",
        content=SYNTHETIC_PNG,
        idempotency_key="identity-transition",
        profile_id="synthetic_gildan_5000",
    )
    updated = updated_job(
        current,
        JobState.ANALYZING_ARTWORK,
        artwork_object_key="local/replaced.png",
    )

    with pytest.raises(InvalidStateError, match="immutable job identity"):
        workflow.store.commit_transition(
            current=current,
            updated=updated,
            event=transition_event(updated),
        )
