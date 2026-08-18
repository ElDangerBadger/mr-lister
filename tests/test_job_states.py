from __future__ import annotations

from mr_lister.contracts import ALLOWED_JOB_TRANSITIONS, JobState, can_transition


def test_happy_path_transitions_are_allowed() -> None:
    path = (
        JobState.UPLOADED,
        JobState.INTAKE_VALIDATED,
        JobState.ANALYZING_ARTWORK,
        JobState.LISTING_DRAFTED,
        JobState.LISTING_VALIDATED,
        JobState.READY_FOR_PRODUCTION,
        JobState.PRINTIFY_DRAFT_CREATED,
        JobState.AWAITING_APPROVAL,
        JobState.APPROVED,
        JobState.PUBLISHING,
        JobState.PUBLISHED,
        JobState.VERIFIED,
    )

    assert all(
        can_transition(current, target) for current, target in zip(path, path[1:], strict=False)
    )


def test_retryable_failure_cannot_jump_directly_to_publishing() -> None:
    assert not can_transition(JobState.FAILED_RETRYABLE, JobState.PUBLISHING)
    assert can_transition(JobState.FAILED_RETRYABLE, JobState.APPROVED)


def test_terminal_states_have_no_outbound_transitions() -> None:
    terminal_states = (JobState.VERIFIED, JobState.FAILED_TERMINAL, JobState.CANCELLED)

    assert all(not ALLOWED_JOB_TRANSITIONS[state] for state in terminal_states)


def test_transition_table_covers_every_state() -> None:
    assert set(ALLOWED_JOB_TRANSITIONS) == set(JobState)
