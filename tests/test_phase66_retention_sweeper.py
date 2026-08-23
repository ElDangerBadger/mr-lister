from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from typing import Any

import pytest

from mr_lister.control.models import ControlJobRecord, ControlJobState, SourceArtifactRecord
from mr_lister.control.source_artwork import source_artifact_fingerprint
from mr_lister.production.retention import (
    MAX_RETENTION_SCAN_PAGES,
    MIN_UNREFERENCED_SOURCE_RELEASE_AGE,
    PHASE6_SOURCE_PREFIX,
    ListedSourceVersion,
    ReferenceAwareSourceVersionSweeper,
    RetentionBoundaryInvalidError,
    RetentionCheckpoint,
    RetentionDependencyUnavailableError,
    SourceAuthoritySnapshot,
    SourceVersionPage,
    SourceVersionTag,
    SourceVersionTags,
)

NOW = datetime(2026, 8, 22, 12, 0, tzinfo=UTC)
CREATED_AT = NOW - timedelta(days=500)
OWNER_ID = "a" * 64
OTHER_OWNER_ID = "b" * 64
JOB_ID = "job_phase66_retention"
BUCKET = "mr-lister-phase6-artifacts-dev"
VERSION_ID = "source-version-exact-1"
OTHER_VERSION_ID = "source-version-orphan-2"
OBJECT_KEY = f"private/owners/{OWNER_ID}/jobs/{JOB_ID}/source/source.png"


def _source(*, version_id: str = VERSION_ID) -> SourceArtifactRecord:
    material: dict[str, Any] = {
        "job_id": JOB_ID,
        "owner_id": OWNER_ID,
        "bucket": BUCKET,
        "object_key": OBJECT_KEY,
        "version_id": version_id,
        "content_sha256": "c" * 64,
        "size_bytes": 128,
        "media_type": "image/png",
        "product_profile_id": "gildan_64000_swiftpod",
        "product_profile_version": 2,
        "product_profile_fingerprint": "d" * 64,
        "created_at": CREATED_AT,
    }
    return SourceArtifactRecord(
        fingerprint=source_artifact_fingerprint(**material),
        **material,
    )


def _job(
    *,
    state: ControlJobState = ControlJobState.AWAITING_APPROVAL,
    updated_at: datetime = NOW - timedelta(days=1),
    source: SourceArtifactRecord | None = None,
) -> ControlJobRecord:
    exact_source = source or _source()
    values: dict[str, Any] = {
        "owner_id": OWNER_ID,
        "job_id": JOB_ID,
        "record_version": 4,
        "event_sequence": 5,
        "state": state,
        "review_version": 1,
        "review_fingerprint": "e" * 64,
        "review_validated": True,
        "source_artifact_fingerprint": exact_source.fingerprint,
        "created_at": CREATED_AT,
        "updated_at": updated_at,
    }
    if state in {ControlJobState.FAILED_RETRYABLE, ControlJobState.FAILED_TERMINAL}:
        values["failure_id"] = "failure_retention"
    if state in {ControlJobState.CANCEL_REQUESTED, ControlJobState.CANCELLED}:
        values["cancellation_requested_at"] = updated_at
    if state is ControlJobState.CANCEL_REQUESTED:
        values["active_work_request_id"] = "work_cancelling"
    if state is ControlJobState.APPROVED:
        values.update(
            {
                "product_id": "product_retention",
                "product_sync_id": "sync_retention",
                "synchronized_review_version": 1,
                "product_sync_fingerprint": "f" * 64,
                "pricing_snapshot_id": "pricing_retention",
                "pricing_snapshot_fingerprint": "1" * 64,
                "approval_decision_id": "decision_retention",
                "approved_review_version": 1,
                "approved_review_fingerprint": "e" * 64,
                "approval_fingerprint": "2" * 64,
            }
        )
    return ControlJobRecord.model_validate(values)


def _snapshot(
    *,
    state: ControlJobState = ControlJobState.AWAITING_APPROVAL,
    updated_at: datetime = NOW - timedelta(days=1),
    version_id: str = VERSION_ID,
) -> SourceAuthoritySnapshot:
    source = _source(version_id=version_id)
    return SourceAuthoritySnapshot(
        job=_job(state=state, updated_at=updated_at, source=source),
        source=source,
    )


ABSENT = SourceAuthoritySnapshot()


class _Inventory:
    def __init__(self, responses: dict[str | None, object]) -> None:
        self.responses = responses
        self.calls: list[dict[str, object]] = []

    def list_source_versions(
        self,
        *,
        source_prefix: str,
        cursor: str | None,
        limit: int,
    ) -> SourceVersionPage:
        self.calls.append({"source_prefix": source_prefix, "cursor": cursor, "limit": limit})
        response = self.responses[cursor]
        if isinstance(response, Exception):
            raise response
        return response  # type: ignore[return-value]


class _Tags:
    def __init__(self, states: dict[tuple[str, str], str]) -> None:
        self.states = states
        self.get_calls: list[tuple[str, str]] = []
        self.set_calls: list[tuple[str, str, str]] = []
        self.get_overrides: dict[tuple[str, str], object] = {}
        self.on_set: Callable[[str, str, str], None] | None = None

    def get_version_tags(self, *, object_key: str, version_id: str) -> SourceVersionTags:
        identity = (object_key, version_id)
        self.get_calls.append(identity)
        response = self.get_overrides.get(identity)
        if isinstance(response, Exception):
            raise response
        if response is not None:
            return response  # type: ignore[return-value]
        return SourceVersionTags(
            tags=(
                SourceVersionTag(
                    key="mr-lister-state",
                    value=self.states[identity],
                ),
            )
        )

    def set_version_state(self, *, object_key: str, version_id: str, state: str) -> None:
        self.set_calls.append((object_key, version_id, state))
        if self.on_set is not None:
            self.on_set(object_key, version_id, state)
        self.states[(object_key, version_id)] = state


class _Authority:
    def __init__(self, responses: list[object]) -> None:
        self.responses = responses
        self.calls: list[str] = []

    def read_source_authority_strong(self, *, job_id: str) -> SourceAuthoritySnapshot:
        self.calls.append(job_id)
        response = self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]
        if isinstance(response, Exception):
            raise response
        return response  # type: ignore[return-value]


class _Checkpoints:
    def __init__(self, checkpoint: RetentionCheckpoint | None = None) -> None:
        self.checkpoint = checkpoint or RetentionCheckpoint()
        self.saves: list[tuple[RetentionCheckpoint, RetentionCheckpoint]] = []
        self.load_error: Exception | None = None
        self.save_error: Exception | None = None

    def load_checkpoint(self) -> RetentionCheckpoint:
        if self.load_error is not None:
            raise self.load_error
        return self.checkpoint

    def save_checkpoint(
        self,
        *,
        expected: RetentionCheckpoint,
        updated: RetentionCheckpoint,
    ) -> None:
        if self.save_error is not None:
            raise self.save_error
        if expected != self.checkpoint:
            raise RuntimeError("simulated checkpoint CAS conflict")
        self.saves.append((expected, updated))
        self.checkpoint = updated


def _version(
    version_id: str = VERSION_ID,
    *,
    object_key: str = OBJECT_KEY,
    last_modified: datetime = NOW - timedelta(days=3),
) -> ListedSourceVersion:
    return ListedSourceVersion(
        object_key=object_key,
        version_id=version_id,
        last_modified=last_modified,
    )


def _page(
    *versions: ListedSourceVersion,
    next_cursor: str | None = None,
) -> SourceVersionPage:
    return SourceVersionPage(
        observed_at=NOW,
        versions=versions,
        next_cursor=next_cursor,
    )


def _sweeper(
    *,
    inventory: _Inventory | None = None,
    tags: _Tags | None = None,
    authority: _Authority | None = None,
    checkpoints: _Checkpoints | None = None,
    **limits: int,
) -> tuple[
    ReferenceAwareSourceVersionSweeper,
    _Inventory,
    _Tags,
    _Authority,
    _Checkpoints,
]:
    exact_inventory = inventory or _Inventory({None: _page(_version())})
    exact_tags = tags or _Tags({(OBJECT_KEY, VERSION_ID): "pinned"})
    exact_authority = authority or _Authority([_snapshot()])
    exact_checkpoints = checkpoints or _Checkpoints()
    sweeper = ReferenceAwareSourceVersionSweeper(
        inventory=exact_inventory,
        tags=exact_tags,
        authority=exact_authority,
        checkpoints=exact_checkpoints,
        artifact_bucket=BUCKET,
        clock=lambda: NOW,
        **limits,
    )
    return sweeper, exact_inventory, exact_tags, exact_authority, exact_checkpoints


@pytest.mark.parametrize(
    "state",
    (
        ControlJobState.AWAITING_APPROVAL,
        ControlJobState.FAILED_RETRYABLE,
        ControlJobState.CANCEL_REQUESTED,
        ControlJobState.APPROVED,
    ),
)
@pytest.mark.parametrize("observed_state", ("pinned", "staged"))
def test_current_durable_source_is_reasserted_pinned_for_every_retained_state(
    state: ControlJobState,
    observed_state: str,
) -> None:
    tags = _Tags({(OBJECT_KEY, VERSION_ID): observed_state})
    authority = _Authority([_snapshot(state=state, updated_at=NOW - timedelta(days=90))])
    sweeper, inventory, tags, authority, checkpoints = _sweeper(
        tags=tags,
        authority=authority,
    )

    result = sweeper.sweep()

    assert inventory.calls == [
        {"source_prefix": PHASE6_SOURCE_PREFIX, "cursor": None, "limit": 100}
    ]
    assert tags.set_calls == [(OBJECT_KEY, VERSION_ID, "pinned")]
    assert authority.calls == [JOB_ID]
    assert checkpoints.checkpoint == RetentionCheckpoint(revision=1)
    assert result.versions_reasserted_pinned == 1
    assert result.scan_complete is True


@pytest.mark.parametrize(
    ("state", "age", "expected_state"),
    (
        (ControlJobState.CANCELLED, timedelta(days=29, hours=23), "pinned"),
        (ControlJobState.FAILED_TERMINAL, timedelta(days=29, hours=23), "pinned"),
        (ControlJobState.CANCELLED, timedelta(days=30), "staged"),
        (ControlJobState.FAILED_TERMINAL, timedelta(days=45), "staged"),
        (ControlJobState.APPROVED, timedelta(days=365), "pinned"),
    ),
)
def test_terminal_grace_and_approved_phase7_authority_control_retention(
    state: ControlJobState,
    age: timedelta,
    expected_state: str,
) -> None:
    tags = _Tags({(OBJECT_KEY, VERSION_ID): "pinned"})
    authority = _Authority([_snapshot(state=state, updated_at=NOW - age)])
    sweeper, _inventory, tags, _authority, _checkpoints = _sweeper(
        tags=tags,
        authority=authority,
    )

    result = sweeper.sweep()

    assert tags.states[(OBJECT_KEY, VERSION_ID)] == expected_state
    if expected_state == "staged":
        assert tags.set_calls == [(OBJECT_KEY, VERSION_ID, "staged")]
        assert result.versions_released_to_staged == 1
    else:
        assert tags.set_calls == [(OBJECT_KEY, VERSION_ID, "pinned")]
        assert result.versions_reasserted_pinned == 1


def test_fast_application_clock_cannot_shorten_terminal_source_retention() -> None:
    server_observed_at = NOW - timedelta(minutes=4)
    job_updated_at = NOW - timedelta(days=30, minutes=1)
    inventory = _Inventory(
        {
            None: SourceVersionPage(
                observed_at=server_observed_at,
                versions=(_version(last_modified=server_observed_at - timedelta(days=3)),),
            )
        }
    )
    tags = _Tags({(OBJECT_KEY, VERSION_ID): "pinned"})
    sweeper, _inventory, tags, _authority, _checkpoints = _sweeper(
        inventory=inventory,
        tags=tags,
        authority=_Authority(
            [_snapshot(state=ControlJobState.CANCELLED, updated_at=job_updated_at)]
        ),
    )

    result = sweeper.sweep()

    assert server_observed_at - job_updated_at < timedelta(days=30)
    assert tags.states[(OBJECT_KEY, VERSION_ID)] == "pinned"
    assert tags.set_calls == [(OBJECT_KEY, VERSION_ID, "pinned")]
    assert result.versions_reasserted_pinned == 1
    assert result.versions_released_to_staged == 0


def test_only_orphaned_pinned_versions_are_released_and_staged_versions_are_untouched() -> None:
    inventory = _Inventory({None: _page(_version(), _version(OTHER_VERSION_ID))})
    tags = _Tags(
        {
            (OBJECT_KEY, VERSION_ID): "pinned",
            (OBJECT_KEY, OTHER_VERSION_ID): "staged",
        }
    )
    authority = _Authority([ABSENT])
    sweeper, _inventory, tags, authority, _checkpoints = _sweeper(
        inventory=inventory,
        tags=tags,
        authority=authority,
    )

    result = sweeper.sweep()

    assert tags.get_calls == [
        (OBJECT_KEY, VERSION_ID),
        (OBJECT_KEY, OTHER_VERSION_ID),
    ]
    assert tags.set_calls == [(OBJECT_KEY, VERSION_ID, "staged")]
    assert authority.calls == [JOB_ID, JOB_ID, JOB_ID, JOB_ID]
    assert result.versions_released_to_staged == 1
    assert result.staged_versions_unchanged == 1
    assert not hasattr(tags, "delete_object")
    assert not hasattr(tags, "get_object")


def test_recent_precommit_pin_is_preserved_past_the_upload_intent_lifetime() -> None:
    recent = NOW - MIN_UNREFERENCED_SOURCE_RELEASE_AGE + timedelta(seconds=1)
    inventory = _Inventory({None: _page(_version(last_modified=recent))})
    tags = _Tags({(OBJECT_KEY, VERSION_ID): "pinned"})
    sweeper, _inventory, tags, authority, _checkpoints = _sweeper(
        inventory=inventory,
        tags=tags,
        authority=_Authority([ABSENT]),
    )

    result = sweeper.sweep()

    assert authority.calls == [JOB_ID]
    assert tags.set_calls == [(OBJECT_KEY, VERSION_ID, "pinned")]
    assert result.versions_reasserted_pinned == 1
    assert result.versions_released_to_staged == 0


def test_future_inventory_timestamp_fails_before_a_release_mutation() -> None:
    inventory = _Inventory({None: _page(_version(last_modified=NOW + timedelta(seconds=1)))})
    tags = _Tags({(OBJECT_KEY, VERSION_ID): "pinned"})
    sweeper, _inventory, tags, _authority, checkpoints = _sweeper(
        inventory=inventory,
        tags=tags,
        authority=_Authority([ABSENT]),
    )

    with pytest.raises(RetentionBoundaryInvalidError, match="timestamp"):
        sweeper.sweep()

    assert tags.set_calls == []
    assert checkpoints.saves == []


@pytest.mark.parametrize(
    "observed_at",
    (NOW - timedelta(minutes=5, seconds=1), NOW + timedelta(minutes=5, seconds=1)),
)
def test_inventory_server_clock_drift_fails_before_tag_reads(
    observed_at: datetime,
) -> None:
    inventory = _Inventory(
        {
            None: SourceVersionPage(
                observed_at=observed_at,
                versions=(_version(last_modified=observed_at - timedelta(days=3)),),
            )
        }
    )
    tags = _Tags({(OBJECT_KEY, VERSION_ID): "pinned"})
    sweeper, _inventory, tags, _authority, checkpoints = _sweeper(
        inventory=inventory,
        tags=tags,
    )

    with pytest.raises(RetentionBoundaryInvalidError, match="clock"):
        sweeper.sweep()

    assert tags.get_calls == []
    assert checkpoints.saves == []


def test_only_the_exact_authoritative_version_is_pinned_for_a_shared_source_key() -> None:
    current = _snapshot(version_id=VERSION_ID)
    inventory = _Inventory({None: _page(_version(OTHER_VERSION_ID), _version(VERSION_ID))})
    tags = _Tags(
        {
            (OBJECT_KEY, OTHER_VERSION_ID): "pinned",
            (OBJECT_KEY, VERSION_ID): "pinned",
        }
    )
    sweeper, _inventory, tags, _authority, _checkpoints = _sweeper(
        inventory=inventory,
        tags=tags,
        authority=_Authority([current]),
    )

    result = sweeper.sweep()

    assert tags.set_calls == [
        (OBJECT_KEY, OTHER_VERSION_ID, "staged"),
        (OBJECT_KEY, VERSION_ID, "pinned"),
    ]
    assert result.versions_released_to_staged == 1
    assert result.versions_reasserted_pinned == 1


def test_reference_appearing_before_release_write_prevents_staging() -> None:
    authority = _Authority([ABSENT, _snapshot()])
    sweeper, _inventory, tags, authority, _checkpoints = _sweeper(authority=authority)

    result = sweeper.sweep()

    assert authority.calls == [JOB_ID, JOB_ID]
    assert tags.set_calls == [(OBJECT_KEY, VERSION_ID, "pinned")]
    assert result.versions_reasserted_pinned == 1


def test_reference_appearing_during_release_write_is_repaired_to_pinned() -> None:
    authority = _Authority([ABSENT, ABSENT, _snapshot()])
    sweeper, _inventory, tags, authority, _checkpoints = _sweeper(authority=authority)

    result = sweeper.sweep()

    assert authority.calls == [JOB_ID, JOB_ID, JOB_ID]
    assert tags.set_calls == [
        (OBJECT_KEY, VERSION_ID, "staged"),
        (OBJECT_KEY, VERSION_ID, "pinned"),
    ]
    assert tags.states[(OBJECT_KEY, VERSION_ID)] == "pinned"
    assert result.versions_reasserted_pinned == 1
    assert result.versions_released_to_staged == 0


def test_replay_is_idempotent_after_orphan_release() -> None:
    tags = _Tags({(OBJECT_KEY, VERSION_ID): "pinned"})
    sweeper, _inventory, tags, _authority, checkpoints = _sweeper(
        tags=tags,
        authority=_Authority([ABSENT]),
    )

    first = sweeper.sweep()
    second = sweeper.sweep()

    assert tags.states[(OBJECT_KEY, VERSION_ID)] == "staged"
    assert tags.set_calls == [(OBJECT_KEY, VERSION_ID, "staged")]
    assert first.versions_released_to_staged == 1
    assert second.staged_versions_unchanged == 1
    assert checkpoints.checkpoint == RetentionCheckpoint(revision=2)


def test_persisted_cursor_continues_a_bounded_scan_and_then_resets() -> None:
    cursor = "opaque-page-2"
    inventory = _Inventory(
        {
            None: _page(_version(), next_cursor=cursor),
            cursor: _page(_version(OTHER_VERSION_ID)),
        }
    )
    tags = _Tags(
        {
            (OBJECT_KEY, VERSION_ID): "staged",
            (OBJECT_KEY, OTHER_VERSION_ID): "staged",
        }
    )
    sweeper, inventory, _tags, _authority, checkpoints = _sweeper(
        inventory=inventory,
        tags=tags,
        authority=_Authority([ABSENT]),
        max_pages_per_run=1,
        max_pages_per_scan=4,
    )

    first = sweeper.sweep()
    persisted = checkpoints.checkpoint
    second = sweeper.sweep()

    assert first.scan_complete is False
    assert persisted.cursor == cursor
    assert persisted.scan_pages == 1
    assert persisted.scan_items == 1
    assert second.scan_complete is True
    assert checkpoints.checkpoint == RetentionCheckpoint(revision=2)
    assert inventory.calls == [
        {"source_prefix": PHASE6_SOURCE_PREFIX, "cursor": None, "limit": 100},
        {"source_prefix": PHASE6_SOURCE_PREFIX, "cursor": cursor, "limit": 100},
    ]


def test_pagination_cycle_fails_without_advancing_the_cycling_page() -> None:
    cursor = "cycle-cursor"
    inventory = _Inventory(
        {
            None: _page(_version(), next_cursor=cursor),
            cursor: _page(_version(OTHER_VERSION_ID), next_cursor=cursor),
        }
    )
    tags = _Tags(
        {
            (OBJECT_KEY, VERSION_ID): "staged",
            (OBJECT_KEY, OTHER_VERSION_ID): "staged",
        }
    )
    sweeper, _inventory, tags, _authority, checkpoints = _sweeper(
        inventory=inventory,
        tags=tags,
        authority=_Authority([ABSENT]),
        max_pages_per_run=2,
        max_pages_per_scan=4,
    )

    with pytest.raises(RetentionBoundaryInvalidError, match="pagination cycled"):
        sweeper.sweep()

    assert tags.get_calls == [(OBJECT_KEY, VERSION_ID)]
    assert checkpoints.checkpoint.cursor == cursor
    assert len(checkpoints.saves) == 1


@pytest.mark.parametrize(
    "bad_page",
    (
        SourceVersionPage(
            observed_at=NOW,
            versions=(_version(object_key="private/not-a-source.png"),),
        ),
        SourceVersionPage(observed_at=NOW, versions=(_version("null"),)),
        SourceVersionPage(observed_at=NOW, versions=(_version(), _version())),
        SourceVersionPage(
            observed_at=NOW,
            versions=(),
            next_cursor="unexpected-empty-continuation",
        ),
    ),
)
def test_malformed_keys_versions_duplicates_and_empty_continuations_fail_closed(
    bad_page: SourceVersionPage,
) -> None:
    sweeper, _inventory, tags, authority, checkpoints = _sweeper(
        inventory=_Inventory({None: bad_page})
    )

    with pytest.raises(RetentionBoundaryInvalidError):
        sweeper.sweep()

    assert tags.get_calls == []
    assert tags.set_calls == []
    assert authority.calls == []
    assert checkpoints.saves == []


def test_lifecycle_delete_markers_are_bounded_skipped_and_checkpointed() -> None:
    cursor = "after-delete-markers"
    inventory = _Inventory(
        {
            None: SourceVersionPage(
                observed_at=NOW,
                delete_marker_count=2,
                next_cursor=cursor,
            ),
            cursor: SourceVersionPage(
                observed_at=NOW,
                versions=(_version(),),
                delete_marker_count=1,
            ),
        }
    )
    tags = _Tags({(OBJECT_KEY, VERSION_ID): "staged"})
    sweeper, _inventory, tags, authority, checkpoints = _sweeper(
        inventory=inventory,
        tags=tags,
        authority=_Authority([ABSENT]),
        max_pages_per_run=2,
    )

    result = sweeper.sweep()

    assert result.pages_scanned == 2
    assert result.versions_scanned == 1
    assert result.delete_markers_skipped == 3
    assert result.staged_versions_unchanged == 1
    assert result.scan_complete is True
    assert tags.get_calls == [(OBJECT_KEY, VERSION_ID)]
    assert authority.calls == [JOB_ID]
    assert checkpoints.saves[0][1].scan_items == 2
    assert checkpoints.checkpoint == RetentionCheckpoint(revision=2)


def test_inventory_cannot_return_more_items_than_the_requested_bound() -> None:
    versions = tuple(_version(f"version-{index}") for index in range(3))
    sweeper, _inventory, tags, _authority, checkpoints = _sweeper(
        inventory=_Inventory({None: SourceVersionPage(observed_at=NOW, versions=versions)}),
        tags=_Tags({(OBJECT_KEY, item.version_id): "staged" for item in versions}),
        page_size=2,
        max_items_per_run=2,
    )

    with pytest.raises(RetentionBoundaryInvalidError, match="page bound"):
        sweeper.sweep()

    assert tags.get_calls == []
    assert checkpoints.saves == []


@pytest.mark.parametrize(
    "bad_tags",
    (
        SourceVersionTags(tags=()),
        SourceVersionTags(tags=(SourceVersionTag(key="unexpected", value="pinned"),)),
        SourceVersionTags(tags=(SourceVersionTag(key="mr-lister-state", value="other"),)),
        SourceVersionTags(
            tags=(
                SourceVersionTag(key="mr-lister-state", value="pinned"),
                SourceVersionTag(key="extra", value="value"),
            )
        ),
    ),
)
def test_missing_duplicate_or_unexpected_version_tags_fail_before_any_mutation(
    bad_tags: SourceVersionTags,
) -> None:
    tags = _Tags({(OBJECT_KEY, VERSION_ID): "pinned"})
    tags.get_overrides[(OBJECT_KEY, VERSION_ID)] = bad_tags
    sweeper, _inventory, tags, authority, checkpoints = _sweeper(tags=tags)

    with pytest.raises(RetentionBoundaryInvalidError, match="tags are invalid"):
        sweeper.sweep()

    assert tags.set_calls == []
    assert authority.calls == []
    assert checkpoints.saves == []


def test_inconsistent_owner_job_source_authority_fails_closed() -> None:
    foreign_key = f"private/owners/{OTHER_OWNER_ID}/jobs/{JOB_ID}/source/source.png"
    inventory = _Inventory({None: _page(_version(object_key=foreign_key))})
    tags = _Tags({(foreign_key, VERSION_ID): "pinned"})
    sweeper, _inventory, tags, _authority, checkpoints = _sweeper(
        inventory=inventory,
        tags=tags,
        authority=_Authority([_snapshot()]),
    )

    with pytest.raises(RetentionBoundaryInvalidError, match="inconsistent"):
        sweeper.sweep()

    assert tags.set_calls == []
    assert checkpoints.saves == []


def test_partial_authority_snapshot_fails_closed() -> None:
    partial = SourceAuthoritySnapshot.model_construct(job=_job(), source=None)
    sweeper, _inventory, tags, _authority, checkpoints = _sweeper(authority=_Authority([partial]))

    with pytest.raises(RetentionBoundaryInvalidError, match="inconsistent"):
        sweeper.sweep()

    assert tags.set_calls == []
    assert checkpoints.saves == []


@pytest.mark.parametrize("failure_seam", ("checkpoint_load", "inventory", "tags", "authority"))
def test_read_dependency_failures_are_closed_and_redacted(failure_seam: str) -> None:
    secret = "private/owners/secret/jobs/secret/source/source.png"
    inventory = _Inventory({None: _page(_version())})
    tags = _Tags({(OBJECT_KEY, VERSION_ID): "pinned"})
    authority = _Authority([_snapshot()])
    checkpoints = _Checkpoints()
    if failure_seam == "checkpoint_load":
        checkpoints.load_error = RuntimeError(secret)
    elif failure_seam == "inventory":
        inventory.responses[None] = RuntimeError(secret)
    elif failure_seam == "tags":
        tags.get_overrides[(OBJECT_KEY, VERSION_ID)] = RuntimeError(secret)
    else:
        authority = _Authority([RuntimeError(secret)])
    sweeper, _inventory, tags, _authority, checkpoints = _sweeper(
        inventory=inventory,
        tags=tags,
        authority=authority,
        checkpoints=checkpoints,
    )

    with pytest.raises(RetentionDependencyUnavailableError) as captured:
        sweeper.sweep()

    assert secret not in str(captured.value)
    assert tags.set_calls == []
    assert checkpoints.saves == []


def test_uncertain_release_tag_failure_is_repaired_pinned_and_not_checkpointed() -> None:
    secret = "source-version-secret"
    tags = _Tags({(OBJECT_KEY, VERSION_ID): "pinned"})
    raised = False

    def fail_after_applying(object_key: str, version_id: str, state: str) -> None:
        nonlocal raised
        tags.states[(object_key, version_id)] = state
        if state == "staged" and not raised:
            raised = True
            raise RuntimeError(secret)

    tags.on_set = fail_after_applying
    sweeper, _inventory, tags, _authority, checkpoints = _sweeper(
        tags=tags,
        authority=_Authority([ABSENT]),
    )

    with pytest.raises(RetentionDependencyUnavailableError) as captured:
        sweeper.sweep()

    assert secret not in str(captured.value)
    assert tags.set_calls == [
        (OBJECT_KEY, VERSION_ID, "staged"),
        (OBJECT_KEY, VERSION_ID, "pinned"),
    ]
    assert tags.states[(OBJECT_KEY, VERSION_ID)] == "pinned"
    assert checkpoints.saves == []


def test_post_release_authority_failure_is_repaired_pinned_and_not_checkpointed() -> None:
    authority = _Authority([ABSENT, ABSENT, RuntimeError("private source authority leaked")])
    sweeper, _inventory, tags, _authority, checkpoints = _sweeper(authority=authority)

    with pytest.raises(RetentionDependencyUnavailableError):
        sweeper.sweep()

    assert tags.set_calls == [
        (OBJECT_KEY, VERSION_ID, "staged"),
        (OBJECT_KEY, VERSION_ID, "pinned"),
    ]
    assert tags.states[(OBJECT_KEY, VERSION_ID)] == "pinned"
    assert checkpoints.saves == []


def test_checkpoint_failure_does_not_hide_completed_idempotent_tag_work() -> None:
    checkpoints = _Checkpoints()
    checkpoints.save_error = RuntimeError("checkpoint-secret")
    tags = _Tags({(OBJECT_KEY, VERSION_ID): "pinned"})
    sweeper, _inventory, tags, _authority, checkpoints = _sweeper(
        tags=tags,
        authority=_Authority([ABSENT]),
        checkpoints=checkpoints,
    )

    with pytest.raises(RetentionDependencyUnavailableError):
        sweeper.sweep()

    assert tags.states[(OBJECT_KEY, VERSION_ID)] == "staged"
    assert checkpoints.checkpoint == RetentionCheckpoint()
    checkpoints.save_error = None
    replay = sweeper.sweep()
    assert replay.staged_versions_unchanged == 1
    assert tags.set_calls == [(OBJECT_KEY, VERSION_ID, "staged")]


def test_sanitized_result_has_only_bounded_counters_and_completion_state() -> None:
    sweeper, _inventory, _tags, _authority, _checkpoints = _sweeper()

    result = sweeper.sweep()
    serialized = result.model_dump_json()

    assert set(result.model_dump()) == {
        "contract_version",
        "pages_scanned",
        "versions_scanned",
        "delete_markers_skipped",
        "versions_reasserted_pinned",
        "versions_released_to_staged",
        "staged_versions_unchanged",
        "scan_complete",
    }
    assert OBJECT_KEY not in serialized
    assert VERSION_ID not in serialized
    assert OWNER_ID not in serialized
    assert JOB_ID not in serialized


def test_naive_clock_and_whole_scan_bound_fail_without_tag_mutation() -> None:
    inventory = _Inventory({"continued": _page(_version())})
    checkpoint = RetentionCheckpoint(
        revision=4,
        cursor="continued",
        seen_cursor_digests=(
            sha256(b"prior").hexdigest(),
            sha256(b"continued").hexdigest(),
        ),
        scan_pages=2,
        scan_items=2,
    )
    tags = _Tags({(OBJECT_KEY, VERSION_ID): "pinned"})
    sweeper, _inventory, tags, _authority, checkpoints = _sweeper(
        inventory=inventory,
        tags=tags,
        authority=_Authority([ABSENT]),
        checkpoints=_Checkpoints(checkpoint),
        max_pages_per_run=1,
        max_pages_per_scan=2,
    )

    with pytest.raises(RetentionBoundaryInvalidError, match="safety bound"):
        sweeper.sweep()

    assert tags.get_calls == []
    assert checkpoints.saves == []

    naive = ReferenceAwareSourceVersionSweeper(
        inventory=_Inventory({None: _page()}),
        tags=_Tags({}),
        authority=_Authority([ABSENT]),
        checkpoints=_Checkpoints(),
        artifact_bucket=BUCKET,
        clock=lambda: NOW.replace(tzinfo=None),
    )
    with pytest.raises(RetentionBoundaryInvalidError, match="clock"):
        naive.sweep()


def test_checkpoint_history_and_configured_scan_pages_fit_the_durable_item_bound() -> None:
    digests = tuple(
        sha256(str(index).encode()).hexdigest() for index in range(MAX_RETENTION_SCAN_PAGES)
    )
    checkpoint = RetentionCheckpoint(
        revision=MAX_RETENTION_SCAN_PAGES,
        cursor="last-cursor",
        seen_cursor_digests=(*digests[:-1], sha256(b"last-cursor").hexdigest()),
        scan_pages=MAX_RETENTION_SCAN_PAGES,
        scan_items=MAX_RETENTION_SCAN_PAGES,
    )
    assert len(checkpoint.model_dump_json().encode("utf-8")) < 350 * 1024

    with pytest.raises(ValueError, match="at most"):
        RetentionCheckpoint(
            revision=MAX_RETENTION_SCAN_PAGES + 1,
            cursor="last-cursor",
            seen_cursor_digests=(
                *digests,
                sha256(b"last-cursor").hexdigest(),
            ),
            scan_pages=MAX_RETENTION_SCAN_PAGES + 1,
            scan_items=MAX_RETENTION_SCAN_PAGES + 1,
        )

    with pytest.raises(ValueError, match="scan page bound"):
        _sweeper(max_pages_per_scan=MAX_RETENTION_SCAN_PAGES + 1)
