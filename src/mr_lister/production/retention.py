"""Reference-aware retention for private Phase 6 source artwork versions.

The application boundary in this module deliberately knows nothing about boto3 response
shapes.  A deployment adapter may list only the configured source prefix, read tags for an
exact version, and replace that exact version's lifecycle state tag.  Object bytes, deletion,
and broader bucket listing are absent from the protocols.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from typing import Annotated, Literal, Protocol

from pydantic import AwareDatetime, Field, StringConstraints, model_validator

from mr_lister.contracts import ContractModel
from mr_lister.control.models import ControlJobRecord, ControlJobState, SourceArtifactRecord
from mr_lister.control.publication_retention import (
    PublicationRetentionCompletionAuthority,
    validate_publication_retention_completion,
)
from mr_lister.control.source_artwork import validate_source_artifact_authority
from mr_lister.control.upload_models import UPLOAD_INTENT_TTL

SOURCE_VERSION_RETENTION_CONTRACT_VERSION = "1.0.0"
SourceVersionRetentionContractVersion = Literal["1.0.0"]
SourceLifecycleState = Literal["staged", "pinned"]

PHASE6_SOURCE_PREFIX = "private/owners/"
DEFAULT_TERMINAL_SOURCE_RETENTION = timedelta(days=30)
# Upload intents can remain open for at most one day. A pinned version younger than this
# larger grace may still be between the pre-commit pin and its durable completion transaction.
MIN_UNREFERENCED_SOURCE_RELEASE_AGE = UPLOAD_INTENT_TTL + timedelta(days=1)
DEFAULT_RETENTION_CLOCK_SKEW = timedelta(minutes=5)
MAX_RETENTION_SCAN_PAGES = 4_096

_SOURCE_KEY = re.compile(
    r"^private/owners/(?P<owner_id>[a-f0-9]{64})/jobs/"
    r"(?P<job_id>[A-Za-z0-9][A-Za-z0-9_-]{0,127})/source/source\.png$"
)
_DIGEST = re.compile(r"^[a-f0-9]{64}$")
_BUCKET = re.compile(r"^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$")
_SAFE_VERSION = re.compile(r"^[\x21-\x7e]{1,1024}$")
_SAFE_CURSOR = re.compile(r"^[\x21-\x7e]{1,2048}$")


class RetentionSweepError(RuntimeError):
    """Stable, identifier-free failure at the retention boundary."""


class RetentionBoundaryInvalidError(RetentionSweepError):
    """A dependency returned data outside the closed retention contract."""


class RetentionDependencyUnavailableError(RetentionSweepError):
    """A required inventory, authority, checkpoint, or tag operation failed."""


class RetentionModel(ContractModel):
    contract_version: SourceVersionRetentionContractVersion = (
        SOURCE_VERSION_RETENTION_CONTRACT_VERSION
    )


class ListedSourceVersion(RetentionModel):
    """One exact object version from the private source-prefix inventory."""

    object_key: Annotated[str, StringConstraints(min_length=1, max_length=1024)]
    version_id: Annotated[str, StringConstraints(min_length=1, max_length=1024)]
    last_modified: AwareDatetime


class SourceVersionPage(RetentionModel):
    """One bounded page; adapters report delete markers without exposing their keys."""

    observed_at: AwareDatetime
    versions: tuple[ListedSourceVersion, ...] = ()
    delete_marker_count: int = Field(default=0, ge=0)
    next_cursor: Annotated[str, StringConstraints(min_length=1, max_length=2048)] | None = None


class SourceVersionTag(RetentionModel):
    key: Annotated[str, StringConstraints(min_length=1, max_length=128)]
    value: Annotated[str, StringConstraints(min_length=1, max_length=256)]


class SourceVersionTags(RetentionModel):
    tags: tuple[SourceVersionTag, ...]


class SourceAuthoritySnapshot(RetentionModel):
    """One strongly read job/source pair; both absent is authoritative nonexistence."""

    job: ControlJobRecord | None = None
    source: SourceArtifactRecord | None = None
    publication_retention: PublicationRetentionCompletionAuthority | None = None

    @model_validator(mode="after")
    def rows_are_both_present_or_both_absent(self) -> SourceAuthoritySnapshot:
        if (self.job is None) != (self.source is None):
            raise ValueError("Retention authority rows must be both present or both absent")
        if self.job is None:
            if self.publication_retention is not None:
                raise ValueError("Publication retention requires current job/source authority")
            return self
        if self.publication_retention is not None:
            assert self.source is not None
            validate_publication_retention_completion(
                self.job,
                self.publication_retention,
                source=self.source,
            )
        return self


class RetentionCheckpoint(RetentionModel):
    """CAS-protected, persisted continuation for one bounded whole-prefix scan."""

    revision: int = Field(default=0, ge=0)
    cursor: Annotated[str, StringConstraints(min_length=1, max_length=2048)] | None = None
    seen_cursor_digests: tuple[
        Annotated[str, StringConstraints(pattern=r"^[a-f0-9]{64}$")], ...
    ] = Field(default=(), max_length=MAX_RETENTION_SCAN_PAGES)
    scan_pages: int = Field(default=0, ge=0)
    scan_items: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def cursor_matches_scan_progress(self) -> RetentionCheckpoint:
        if self.cursor is None:
            if self.seen_cursor_digests or self.scan_pages or self.scan_items:
                raise ValueError("A completed scan cannot retain continuation state")
        else:
            if _cursor_digest(self.cursor) not in self.seen_cursor_digests:
                raise ValueError("The current cursor must be present in cycle-detection state")
            if self.scan_pages != len(self.seen_cursor_digests):
                raise ValueError("Retention checkpoint page progress is inconsistent")
            if self.scan_items < self.scan_pages:
                raise ValueError("Retention checkpoint item progress is inconsistent")
        if len(set(self.seen_cursor_digests)) != len(self.seen_cursor_digests):
            raise ValueError("Retention checkpoint cursor history must be unique")
        return self


class RetentionSweepResult(RetentionModel):
    """Sanitized operational evidence containing counters and no authority identifiers."""

    pages_scanned: int = Field(ge=0)
    versions_scanned: int = Field(ge=0)
    delete_markers_skipped: int = Field(ge=0)
    versions_reasserted_pinned: int = Field(ge=0)
    versions_released_to_staged: int = Field(ge=0)
    staged_versions_unchanged: int = Field(ge=0)
    scan_complete: bool

    @model_validator(mode="after")
    def counters_cover_the_scanned_versions(self) -> RetentionSweepResult:
        classified = (
            self.versions_reasserted_pinned
            + self.versions_released_to_staged
            + self.staged_versions_unchanged
        )
        if classified != self.versions_scanned:
            raise ValueError("Retention result counters are inconsistent")
        return self


class SourceVersionInventory(Protocol):
    """List only versions below the application-fixed private source prefix."""

    def list_source_versions(
        self,
        *,
        source_prefix: Literal["private/owners/"],
        cursor: str | None,
        limit: int,
    ) -> SourceVersionPage: ...


class SourceVersionTagStore(Protocol):
    """Inspect metadata and replace one exact version's lifecycle tag; never delete."""

    def get_version_tags(
        self,
        *,
        object_key: str,
        version_id: str,
    ) -> SourceVersionTags: ...

    def set_version_state(
        self,
        *,
        object_key: str,
        version_id: str,
        state: SourceLifecycleState,
    ) -> None: ...


class StrongSourceAuthorityReader(Protocol):
    """Return one strongly consistent job/source snapshot for an exact job id."""

    def read_source_authority_strong(self, *, job_id: str) -> SourceAuthoritySnapshot: ...


class RetentionCheckpointStore(Protocol):
    def load_checkpoint(self) -> RetentionCheckpoint: ...

    def save_checkpoint(
        self,
        *,
        expected: RetentionCheckpoint,
        updated: RetentionCheckpoint,
    ) -> None: ...


class ReferenceAwareSourceVersionSweeper:
    """Repair referenced versions and release only safely proven retention orphans."""

    def __init__(
        self,
        *,
        inventory: SourceVersionInventory,
        tags: SourceVersionTagStore,
        authority: StrongSourceAuthorityReader,
        checkpoints: RetentionCheckpointStore,
        artifact_bucket: str,
        clock: Callable[[], datetime] | None = None,
        terminal_source_retention: timedelta = DEFAULT_TERMINAL_SOURCE_RETENTION,
        max_clock_skew: timedelta = DEFAULT_RETENTION_CLOCK_SKEW,
        page_size: int = 100,
        max_pages_per_run: int = 5,
        max_items_per_run: int = 500,
        max_pages_per_scan: int = MAX_RETENTION_SCAN_PAGES,
        max_items_per_scan: int = 100_000,
    ) -> None:
        if _BUCKET.fullmatch(artifact_bucket) is None:
            raise ValueError("Artifact bucket configuration is invalid")
        if not timedelta(0) < terminal_source_retention <= timedelta(days=365):
            raise ValueError("Terminal source retention is outside its safety bound")
        if not timedelta(0) < max_clock_skew <= timedelta(hours=1):
            raise ValueError("Retention clock-skew bound is invalid")
        if not 1 <= page_size <= 1_000:
            raise ValueError("Retention page size is outside its safety bound")
        if not 1 <= max_pages_per_run <= 100:
            raise ValueError("Retention run page bound is invalid")
        if not 1 <= max_items_per_run <= 10_000:
            raise ValueError("Retention run item bound is invalid")
        if max_pages_per_scan < max_pages_per_run or max_pages_per_scan > MAX_RETENTION_SCAN_PAGES:
            raise ValueError("Retention scan page bound is invalid")
        if max_items_per_scan < max_items_per_run or max_items_per_scan > 100_000:
            raise ValueError("Retention scan item bound is invalid")
        self._inventory = inventory
        self._tags = tags
        self._authority = authority
        self._checkpoints = checkpoints
        self._artifact_bucket = artifact_bucket
        self._clock = clock or (lambda: datetime.now(UTC))
        self._terminal_source_retention = terminal_source_retention
        self._max_clock_skew = max_clock_skew
        self._page_size = page_size
        self._max_pages_per_run = max_pages_per_run
        self._max_items_per_run = max_items_per_run
        self._max_pages_per_scan = max_pages_per_scan
        self._max_items_per_scan = max_items_per_scan

    def sweep(self) -> RetentionSweepResult:
        """Process a bounded continuation and persist progress after every complete page."""

        now = self._now()
        checkpoint = self._load_checkpoint()
        pages = 0
        scanned = 0
        entries_scanned = 0
        delete_markers = 0
        pinned = 0
        released = 0
        unchanged = 0
        scan_complete = False

        while pages < self._max_pages_per_run and entries_scanned < self._max_items_per_run:
            remaining = self._max_items_per_run - entries_scanned
            request_limit = min(self._page_size, remaining)
            page = self._list_page(cursor=checkpoint.cursor, limit=request_limit)
            versions = self._validate_page(
                page,
                now=now,
                current_cursor=checkpoint.cursor,
                seen_cursor_digests=checkpoint.seen_cursor_digests,
                request_limit=request_limit,
            )
            # A locally fast clock must never shorten the promised retention window. The
            # inventory adapter's server observation is the conservative authority whenever it
            # trails the application clock within the accepted skew bound.
            effective_now = min(now, page.observed_at)
            next_pages = checkpoint.scan_pages + 1
            page_entries = len(versions) + page.delete_marker_count
            next_items = checkpoint.scan_items + page_entries
            if next_pages > self._max_pages_per_scan or next_items > self._max_items_per_scan:
                raise RetentionBoundaryInvalidError("Retention scan exceeded its safety bound")

            observed_states = tuple(self._get_state(version) for version in versions)
            for version, observed_state in zip(versions, observed_states, strict=True):
                outcome = self._reconcile_version(
                    version,
                    observed_state,
                    now=effective_now,
                )
                if outcome == "pinned":
                    pinned += 1
                elif outcome == "released":
                    released += 1
                else:
                    unchanged += 1

            pages += 1
            scanned += len(versions)
            entries_scanned += page_entries
            delete_markers += page.delete_marker_count
            checkpoint, scan_complete = self._advance_checkpoint(
                checkpoint,
                next_cursor=page.next_cursor,
                scan_pages=next_pages,
                scan_items=next_items,
            )
            if scan_complete:
                break

        return RetentionSweepResult(
            pages_scanned=pages,
            versions_scanned=scanned,
            delete_markers_skipped=delete_markers,
            versions_reasserted_pinned=pinned,
            versions_released_to_staged=released,
            staged_versions_unchanged=unchanged,
            scan_complete=scan_complete,
        )

    def _now(self) -> datetime:
        try:
            now = self._clock()
        except Exception:
            raise RetentionDependencyUnavailableError(
                "Retention dependency is unavailable"
            ) from None
        if not isinstance(now, datetime) or now.utcoffset() is None:
            raise RetentionBoundaryInvalidError("Retention clock response is invalid")
        return now

    def _load_checkpoint(self) -> RetentionCheckpoint:
        try:
            checkpoint = self._checkpoints.load_checkpoint()
        except Exception:
            raise RetentionDependencyUnavailableError(
                "Retention dependency is unavailable"
            ) from None
        if not isinstance(checkpoint, RetentionCheckpoint):
            raise RetentionBoundaryInvalidError("Retention checkpoint is invalid")
        try:
            checkpoint = RetentionCheckpoint.model_validate(
                checkpoint.model_dump(mode="python"),
                strict=True,
            )
        except Exception:
            raise RetentionBoundaryInvalidError("Retention checkpoint is invalid") from None
        self._validate_checkpoint(checkpoint)
        if (
            checkpoint.scan_pages > self._max_pages_per_scan
            or checkpoint.scan_items > self._max_items_per_scan
        ):
            raise RetentionBoundaryInvalidError("Retention checkpoint exceeded its safety bound")
        return checkpoint

    def _list_page(self, *, cursor: str | None, limit: int) -> SourceVersionPage:
        try:
            page = self._inventory.list_source_versions(
                source_prefix=PHASE6_SOURCE_PREFIX,
                cursor=cursor,
                limit=limit,
            )
        except Exception:
            raise RetentionDependencyUnavailableError(
                "Retention dependency is unavailable"
            ) from None
        if not isinstance(page, SourceVersionPage):
            raise RetentionBoundaryInvalidError("Retention inventory response is invalid")
        try:
            return SourceVersionPage.model_validate(
                page.model_dump(mode="python"),
                strict=True,
            )
        except Exception:
            raise RetentionBoundaryInvalidError("Retention inventory response is invalid") from None

    def _validate_page(
        self,
        page: SourceVersionPage,
        *,
        now: datetime,
        current_cursor: str | None,
        seen_cursor_digests: tuple[str, ...],
        request_limit: int,
    ) -> tuple[ListedSourceVersion, ...]:
        if (
            page.observed_at.utcoffset() != UTC.utcoffset(page.observed_at)
            or abs(page.observed_at - now) > self._max_clock_skew
        ):
            raise RetentionBoundaryInvalidError("Retention inventory clock is invalid")
        page_entries = len(page.versions) + page.delete_marker_count
        if page_entries > request_limit:
            raise RetentionBoundaryInvalidError("Retention inventory exceeded its page bound")
        if page.next_cursor is not None and page_entries == 0:
            raise RetentionBoundaryInvalidError("Retention inventory continuation is invalid")
        if page.next_cursor is not None:
            if _SAFE_CURSOR.fullmatch(page.next_cursor) is None:
                raise RetentionBoundaryInvalidError("Retention inventory cursor is invalid")
            digest = _cursor_digest(page.next_cursor)
            if page.next_cursor == current_cursor or digest in seen_cursor_digests:
                raise RetentionBoundaryInvalidError("Retention inventory pagination cycled")

        identities: set[tuple[str, str]] = set()
        for version in page.versions:
            if not isinstance(version, ListedSourceVersion):
                raise RetentionBoundaryInvalidError("Retention inventory entry is invalid")
            if _SOURCE_KEY.fullmatch(version.object_key) is None:
                raise RetentionBoundaryInvalidError("Retention inventory key is invalid")
            if _SAFE_VERSION.fullmatch(version.version_id) is None or version.version_id == "null":
                raise RetentionBoundaryInvalidError("Retention inventory version is invalid")
            if version.last_modified.utcoffset() != UTC.utcoffset(version.last_modified):
                raise RetentionBoundaryInvalidError("Retention inventory timestamp is invalid")
            if version.last_modified > page.observed_at:
                raise RetentionBoundaryInvalidError("Retention inventory timestamp is invalid")
            identity = (version.object_key, version.version_id)
            if identity in identities:
                raise RetentionBoundaryInvalidError("Retention inventory contains duplicates")
            identities.add(identity)
        return page.versions

    def _get_state(self, version: ListedSourceVersion) -> SourceLifecycleState:
        try:
            response = self._tags.get_version_tags(
                object_key=version.object_key,
                version_id=version.version_id,
            )
        except Exception:
            raise RetentionDependencyUnavailableError(
                "Retention dependency is unavailable"
            ) from None
        if not isinstance(response, SourceVersionTags):
            raise RetentionBoundaryInvalidError("Retention version tags are invalid")
        try:
            response = SourceVersionTags.model_validate(
                response.model_dump(mode="python"),
                strict=True,
            )
        except Exception:
            raise RetentionBoundaryInvalidError("Retention version tags are invalid") from None
        if len(response.tags) != 1:
            raise RetentionBoundaryInvalidError("Retention version tags are invalid")
        tag = response.tags[0]
        if (
            not isinstance(tag, SourceVersionTag)
            or tag.key != "mr-lister-state"
            or tag.value not in {"staged", "pinned"}
        ):
            raise RetentionBoundaryInvalidError("Retention version tags are invalid")
        return tag.value

    def _reconcile_version(
        self,
        version: ListedSourceVersion,
        observed_state: SourceLifecycleState,
        *,
        now: datetime,
    ) -> Literal["pinned", "released", "unchanged"]:
        if version.last_modified > now:
            raise RetentionBoundaryInvalidError("Retention inventory timestamp is invalid")
        try:
            initial_retained = self._is_retained(version, now=now)
        except RetentionSweepError:
            # A missing/partial/mismatched publication marker must never leave a staged version
            # unpinned merely because classification failed before the release write.
            if observed_state == "staged":
                self._repair_pinned_after_uncertainty(version)
            raise
        if initial_retained:
            self._set_state(version, "pinned")
            return "pinned"
        if observed_state == "staged":
            return "unchanged"
        if now - version.last_modified < MIN_UNREFERENCED_SOURCE_RELEASE_AGE:
            # Completion pins before its Dynamo transaction. Preserving every recent orphan
            # makes a process loss after that transaction safe: a later scan will observe the
            # source, while an expired intent cannot newly reference a version old enough to
            # cross this release boundary.
            self._set_state(version, "pinned")
            return "pinned"

        # A reference may appear after the first lookup but before the releasing write.
        if self._is_retained(version, now=now):
            self._set_state(version, "pinned")
            return "pinned"
        try:
            self._set_state(version, "staged")
        except RetentionSweepError:
            # The provider may have applied an uncertain write before returning an error.
            self._repair_pinned_after_uncertainty(version)
            raise

        try:
            retained_after_write = self._is_retained(version, now=now)
        except RetentionSweepError:
            # Without the post-write strong read, preservation wins over cleanup.
            self._repair_pinned_after_uncertainty(version)
            raise
        if retained_after_write:
            self._set_state(version, "pinned")
            return "pinned"
        return "released"

    def _is_retained(self, version: ListedSourceVersion, *, now: datetime) -> bool:
        match = _SOURCE_KEY.fullmatch(version.object_key)
        if match is None:  # The page validator should make this unreachable.
            raise RetentionBoundaryInvalidError("Retention inventory key is invalid")
        try:
            snapshot = self._authority.read_source_authority_strong(job_id=match.group("job_id"))
        except Exception:
            raise RetentionDependencyUnavailableError(
                "Retention dependency is unavailable"
            ) from None
        if not isinstance(snapshot, SourceAuthoritySnapshot):
            raise RetentionBoundaryInvalidError("Retention authority response is invalid")
        if snapshot.job is None and snapshot.source is None:
            return False
        if snapshot.job is None or snapshot.source is None:
            raise RetentionBoundaryInvalidError("Retention authority response is inconsistent")

        try:
            job = ControlJobRecord.model_validate(
                snapshot.job.model_dump(mode="python"),
                strict=True,
            )
            source = SourceArtifactRecord.model_validate(
                snapshot.source.model_dump(mode="python"),
                strict=True,
            )
            validate_source_artifact_authority(source)
        except Exception:
            raise RetentionBoundaryInvalidError("Retention authority response is invalid") from None
        if (
            job.job_id != match.group("job_id")
            or source.job_id != job.job_id
            or job.owner_id != match.group("owner_id")
            or source.owner_id != job.owner_id
            or source.bucket != self._artifact_bucket
            or source.object_key != version.object_key
            or job.source_artifact_fingerprint != source.fingerprint
            or job.updated_at.utcoffset() is None
            or job.updated_at > now
        ):
            raise RetentionBoundaryInvalidError("Retention authority response is inconsistent")

        if source.version_id != version.version_id:
            return False
        if job.state is ControlJobState.APPROVED:
            completion = snapshot.publication_retention
            if completion is None:
                return True
            try:
                completion = validate_publication_retention_completion(
                    job,
                    completion,
                    source=source,
                )
            except ValueError:
                raise RetentionBoundaryInvalidError(
                    "Retention authority response is invalid"
                ) from None
            return now < completion.source_release_eligible_at
        if job.state not in {ControlJobState.CANCELLED, ControlJobState.FAILED_TERMINAL}:
            return True
        return now - job.updated_at < self._terminal_source_retention

    def _set_state(
        self,
        version: ListedSourceVersion,
        state: SourceLifecycleState,
    ) -> None:
        try:
            self._tags.set_version_state(
                object_key=version.object_key,
                version_id=version.version_id,
                state=state,
            )
        except Exception:
            raise RetentionDependencyUnavailableError(
                "Retention dependency is unavailable"
            ) from None

    def _repair_pinned_after_uncertainty(self, version: ListedSourceVersion) -> None:
        try:
            self._tags.set_version_state(
                object_key=version.object_key,
                version_id=version.version_id,
                state="pinned",
            )
        except Exception:
            raise RetentionDependencyUnavailableError(
                "Retention dependency is unavailable"
            ) from None

    def _advance_checkpoint(
        self,
        current: RetentionCheckpoint,
        *,
        next_cursor: str | None,
        scan_pages: int,
        scan_items: int,
    ) -> tuple[RetentionCheckpoint, bool]:
        if next_cursor is None:
            updated = RetentionCheckpoint(revision=current.revision + 1)
            complete = True
        else:
            updated = RetentionCheckpoint(
                revision=current.revision + 1,
                cursor=next_cursor,
                seen_cursor_digests=(
                    *current.seen_cursor_digests,
                    _cursor_digest(next_cursor),
                ),
                scan_pages=scan_pages,
                scan_items=scan_items,
            )
            complete = False
        try:
            self._checkpoints.save_checkpoint(expected=current, updated=updated)
        except Exception:
            raise RetentionDependencyUnavailableError(
                "Retention dependency is unavailable"
            ) from None
        return updated, complete

    @staticmethod
    def _validate_checkpoint(checkpoint: RetentionCheckpoint) -> None:
        if checkpoint.cursor is not None and _SAFE_CURSOR.fullmatch(checkpoint.cursor) is None:
            raise RetentionBoundaryInvalidError("Retention checkpoint is invalid")
        if any(_DIGEST.fullmatch(item) is None for item in checkpoint.seen_cursor_digests):
            raise RetentionBoundaryInvalidError("Retention checkpoint is invalid")


def _cursor_digest(cursor: str) -> str:
    return sha256(cursor.encode("utf-8")).hexdigest()


__all__ = [
    "DEFAULT_RETENTION_CLOCK_SKEW",
    "DEFAULT_TERMINAL_SOURCE_RETENTION",
    "ListedSourceVersion",
    "MAX_RETENTION_SCAN_PAGES",
    "MIN_UNREFERENCED_SOURCE_RELEASE_AGE",
    "PHASE6_SOURCE_PREFIX",
    "ReferenceAwareSourceVersionSweeper",
    "RetentionBoundaryInvalidError",
    "RetentionCheckpoint",
    "RetentionCheckpointStore",
    "RetentionDependencyUnavailableError",
    "RetentionSweepError",
    "RetentionSweepResult",
    "SourceAuthoritySnapshot",
    "PublicationRetentionCompletionAuthority",
    "SourceVersionInventory",
    "SourceVersionPage",
    "SourceVersionTag",
    "SourceVersionTags",
    "SourceVersionTagStore",
    "StrongSourceAuthorityReader",
]
