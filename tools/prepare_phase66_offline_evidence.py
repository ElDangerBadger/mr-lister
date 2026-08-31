"""Produce the four-gate Phase 6.6 offline evidence fragment.

This repository-confined command runs the exact frozen replay, concurrency, and cross-owner
pytest selections, parses their real JUnit output, and consumes one separately produced fresh
three-engine browser gate.  It writes only normalized, sanitized artifacts plus the Phase 6.6
records and artifact index beneath ``.mr_lister_private/phase66-acceptance``.

It has no AWS, provider, HTTP, or browser execution capability.  Raw pytest output, raw browser
traces, Playwright logs, credentials, identifiers, and machine-local paths are never retained in
the evidence fragment.
"""

from __future__ import annotations

import argparse
import io
import json
import math
import os
import re
import secrets
import shutil
import stat
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ElementTree
import zipfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from itertools import pairwise
from pathlib import Path, PurePosixPath
from typing import Annotated, Any, Final, Literal

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    StrictInt,
    StringConstraints,
    field_validator,
    model_validator,
)

from mr_lister.acceptance.evidence_set import (
    Phase66ArtifactFile,
    _declared_artifacts,
    _validated_artifact_files,
    _validated_records,
    _verify_artifacts,
)
from mr_lister.acceptance.phase6 import (
    AcceptanceEvidenceClass,
    AcceptanceOutcome,
    ArtifactFormat,
    ArtifactKind,
    phase66_acceptance_manifest,
    phase66_manifest_digest,
    validate_phase66_evidence,
)

REPOSITORY_ROOT: Final = Path(__file__).resolve().parents[1]
PRIVATE_WORKSPACE_ROOT: Final = REPOSITORY_ROOT / ".mr_lister_private/phase66-acceptance"

SOURCE_COMMIT: Final = "7c4d668b2c322f1e4cad1802105567d4a38ee2c8"
SOURCE_COMMIT_DIGEST: Final = "e016852a0ab39e2e454878c5ee6030257bcd07faaaaf8d0a3841e5b5f67c4b11"
OFFLINE_GATE_ORDER: Final = (
    "offline.replay_matrix",
    "offline.concurrency_matrix",
    "offline.cross_owner_matrix",
    "offline.browser_matrix",
)
ENGINE_ORDER: Final = ("chromium", "firefox", "webkit")

RECORDS_FILENAME: Final = "records.json"
ARTIFACT_FILES_FILENAME: Final = "artifact-files.json"
BROWSER_REPORT_FILENAME: Final = "offline-browser-matrix-report.json"
BROWSER_TRACE_FILENAME: Final = "offline-browser-matrix-traces.zip"

_MAX_BROWSER_GATE_BYTES = 1024 * 1024
_MAX_TRACE_BYTES = 200 * 1024 * 1024
_MAX_TRACE_MEMBERS = 10_000
_MAX_TRACE_EXPANDED_BYTES = 512 * 1024 * 1024
_BROWSER_MAX_AGE = timedelta(hours=6)
_BROWSER_SOURCE_TIMESTAMP = re.compile(
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}"
    r"(?:\.[0-9]{1,6})?\+00:00$"
)
_CANONICAL_TIMESTAMP = re.compile(
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{6}Z$"
)
_FORBIDDEN_TRACE_PATTERNS = (
    re.compile(rb"/(?:Users|home)/", re.IGNORECASE),
    re.compile(rb"authorization", re.IGNORECASE),
    re.compile(rb"bearer", re.IGNORECASE),
    re.compile(rb"access[_-]?token", re.IGNORECASE),
    re.compile(rb"refresh[_-]?token", re.IGNORECASE),
    re.compile(rb"id[_-]?token", re.IGNORECASE),
    re.compile(rb"code_verifier", re.IGNORECASE),
    re.compile(rb"set-cookie", re.IGNORECASE),
    re.compile(rb"cookie", re.IGNORECASE),
    re.compile(rb"(?:AKIA|ASIA)[A-Z0-9]{12,}", re.IGNORECASE),
)

type Digest = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]


class Phase66OfflineEvidenceError(RuntimeError):
    """The offline run, browser input, confinement, or evidence validation failed closed."""


class _ClosedModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class _Setup(_ClosedModel):
    fixtureReady: Literal[True]
    providerSentinelVerified: Literal[True]
    proxyRoutes: Literal[4]


class _RestartSetup(_ClosedModel):
    fixtureReady: Literal[True]
    fixtureStatePreserved: Literal[True]
    proxyRoutes: Literal[4]


class _AccessibilityFlow(_ClosedModel):
    forcedColors: Literal["passed"]
    horizontalOverflow: Literal[0]
    reducedMotion: Literal["passed"]
    reflowAt200PercentEquivalent: Literal["passed at 360 CSS pixels"]


class _AuthReviewFlow(_ClosedModel):
    approvalAttempts: Literal[1]
    authRouteRecovery: Literal["passed"]
    commerceControls: Literal[0]
    exactTagCount: Literal[13]
    listingValidation: Literal["passed"]
    providerTransportAttempts: Literal[0]
    staleReadbackFocus: Literal["passed"]
    strandsProminence: Literal["passed"]
    tabRecovery: Literal["passed"]
    unpublishedBoundary: Literal["passed"]


class _BrowserRestartFlow(_ClosedModel):
    approvalAttempts: Literal[1]
    browserRestartRecovery: Literal["passed"]
    durableApprovedRecovery: Literal["passed"]
    providerTransportAttempts: Literal[0]


class _RoutePollingFlow(_ClosedModel):
    hiddenPollingSuppressed: Literal["passed"]
    offlinePollingSuppressed: Literal["passed"]
    resumePolling: Literal["passed"]
    routeAtoBIsolation: Literal["passed"]


class _BrowserFlows(_ClosedModel):
    accessibility_flow_js: _AccessibilityFlow = Field(alias="accessibility_flow.js")
    auth_review_flow_js: _AuthReviewFlow = Field(alias="auth_review_flow.js")
    browser_restart_flow_js: _BrowserRestartFlow = Field(alias="browser_restart_flow.js")
    route_polling_flow_js: _RoutePollingFlow = Field(alias="route_polling_flow.js")


class _BrowserEngine(_ClosedModel):
    engine: Literal["chromium", "firefox", "webkit"]
    flows: _BrowserFlows
    restart_setup: _RestartSetup
    setup: _Setup
    status: Literal["passed"]
    trace_count: Literal[2]


class _BrowserGate(_ClosedModel):
    attestation_eligible: Literal[True]
    bundle_digest: Digest
    bundle_file_count: StrictInt = Field(ge=1, le=10_000)
    engines: tuple[_BrowserEngine, _BrowserEngine, _BrowserEngine]
    gate: Literal["offline.browser_matrix"]
    generated_at: AwareDatetime

    @field_validator("attestation_eligible", mode="before")
    @classmethod
    def eligibility_is_boolean(cls, value: object) -> object:
        if type(value) is not bool:
            raise ValueError("browser eligibility must be a boolean")
        return value

    @field_validator("engines", mode="before")
    @classmethod
    def engines_are_one_exact_json_array(cls, value: object) -> object:
        if not isinstance(value, list) or len(value) != 3:
            raise ValueError("browser engines must be one exact three-item array")
        return tuple(value)

    @field_validator("generated_at", mode="before")
    @classmethod
    def generated_at_is_explicit_utc_text(cls, value: object) -> object:
        if type(value) is not str or _BROWSER_SOURCE_TIMESTAMP.fullmatch(value) is None:
            raise ValueError("browser gate time must be text")
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            raise ValueError("browser gate time is invalid") from None
        if parsed.tzinfo is None or parsed.utcoffset() != UTC.utcoffset(parsed):
            raise ValueError("browser gate time must be UTC")
        return parsed

    @model_validator(mode="after")
    def exact_engine_order(self) -> _BrowserGate:
        if tuple(item.engine for item in self.engines) != ENGINE_ORDER:
            raise ValueError("browser engines are not the exact default matrix")
        if self.generated_at.utcoffset() != UTC.utcoffset(self.generated_at):
            raise ValueError("browser gate time must be UTC")
        return self


@dataclass(frozen=True, slots=True)
class _GateSelection:
    gate_id: str
    node_ids: tuple[str, ...]
    expected_case_names: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _InputFile:
    payload: bytes
    digest: str
    byte_count: int
    identity: tuple[int, int]


@dataclass(frozen=True, slots=True)
class _JUnitResult:
    case_names: tuple[str, ...]
    raw_digest: str
    raw_byte_count: int
    duration_ms: int
    completed_at: str


@dataclass(frozen=True, slots=True)
class _TraceInput:
    engine: str
    source: _InputFile
    members: tuple[tuple[str, bytes], ...]
    trace_count: int


_REPLAY_CASE_NAMES: Final = (
    "test_create_upload_replays_one_receipt_and_rejects_key_reuse_with_changed_input",
    "test_terminal_reauthorization_replay_never_reissues_a_presigned_form",
    "test_valid_png_completion_atomically_creates_exactly_one_preparation_graph",
    "test_exact_upload_cancel_replay_creates_one_terminal_receipt",
    "test_valid_revision_atomically_creates_review_decision_receipt_and_one_sync_work",
    "test_stale_economics_refresh_is_atomic_idempotent_and_enqueues_exact_work",
    "test_exact_approve_replay_returns_one_authority_transition",
    "test_exact_cancel_replay_returns_one_authority_transition",
    "test_exact_retry_replay_creates_one_recovery_work_request",
    "test_changed_listing_content_cannot_reuse_a_successful_idempotency_key",
)
_REPLAY_NODES: Final = (
    "tests/test_phase6_upload_service.py::" + _REPLAY_CASE_NAMES[0],
    "tests/test_phase6_upload_service.py::" + _REPLAY_CASE_NAMES[1],
    "tests/test_phase6_upload_service.py::" + _REPLAY_CASE_NAMES[2],
    "tests/test_phase66_offline_boundary.py::" + _REPLAY_CASE_NAMES[3],
    "tests/test_phase6_control_service.py::" + _REPLAY_CASE_NAMES[4],
    "tests/test_phase6_control_service.py::" + _REPLAY_CASE_NAMES[5],
    "tests/test_phase66_offline_boundary.py::" + _REPLAY_CASE_NAMES[6],
    "tests/test_phase66_offline_boundary.py::" + _REPLAY_CASE_NAMES[7],
    "tests/test_phase66_offline_boundary.py::" + _REPLAY_CASE_NAMES[8],
    "tests/test_phase66_offline_boundary.py::" + _REPLAY_CASE_NAMES[9],
)

_CONCURRENCY_CASE_NAMES: Final = (
    "test_revise_approve_cancel_barrier_has_exactly_one_authority_winner",
    "test_simultaneous_identical_command_returns_one_persisted_receipt_twice",
    "test_simultaneous_same_key_with_changed_request_has_one_success_and_one_conflict",
    "test_completion_loses_store_cas_when_cancellation_wins_the_race",
    "test_fast_worker_can_settle_claimed_work_before_start_acknowledgement",
    "test_settlement_rereads_completed_authority_after_worker_wins_cas_race",
)
_CONCURRENCY_NODES: Final = (
    "tests/test_phase66_offline_boundary.py::" + _CONCURRENCY_CASE_NAMES[0],
    "tests/test_phase6_concurrency.py::" + _CONCURRENCY_CASE_NAMES[1],
    "tests/test_phase6_concurrency.py::" + _CONCURRENCY_CASE_NAMES[2],
    "tests/test_phase6_upload_service.py::" + _CONCURRENCY_CASE_NAMES[3],
    "tests/test_phase6_dispatch.py::" + _CONCURRENCY_CASE_NAMES[4],
    "tests/test_phase66_machine_handlers.py::" + _CONCURRENCY_CASE_NAMES[5],
)

_TARGETED_OWNER_CASES: Final = (
    "GET /v1/uploads/{upload_id}-upload",
    "POST /v1/uploads/{upload_id}/authorize-upload",
    "POST /v1/uploads/{upload_id}/complete-upload",
    "POST /v1/uploads/{upload_id}/cancel-upload",
    "GET /v1/jobs/{job_id}-review",
    "GET /v1/jobs/{job_id}/review-review",
    "PUT /v1/jobs/{job_id}/review/listing-command",
    "POST /v1/jobs/{job_id}/economics/refresh-command",
    "POST /v1/jobs/{job_id}/approve-command",
    "POST /v1/jobs/{job_id}/cancel-command",
    "POST /v1/jobs/{job_id}/retry-command",
    "GET /v1/jobs/{job_id}/artwork-preview-preview",
)
_OWNER_PARAMETRIZED_NAME: Final = (
    "test_targeted_cloud_routes_make_foreign_and_unknown_resources_indistinguishable"
)
_CROSS_OWNER_CASE_NAMES: Final = tuple(
    f"{_OWNER_PARAMETRIZED_NAME}[{case}]" for case in _TARGETED_OWNER_CASES
) + (
    "test_upload_collection_rejects_any_caller_owner_before_write_or_presign",
    "test_job_collection_hides_foreign_jobs_exactly_like_an_empty_owner_index",
    "test_phase66_owner_boundary_matrix_covers_every_protected_cloud_route",
)
_CROSS_OWNER_NODES: Final = (
    "tests/test_phase66_offline_boundary.py::" + _OWNER_PARAMETRIZED_NAME,
    "tests/test_phase66_offline_boundary.py::" + _CROSS_OWNER_CASE_NAMES[-3],
    "tests/test_phase66_offline_boundary.py::" + _CROSS_OWNER_CASE_NAMES[-2],
    "tests/test_phase66_offline_boundary.py::" + _CROSS_OWNER_CASE_NAMES[-1],
)

_SELECTIONS: Final = (
    _GateSelection("offline.replay_matrix", _REPLAY_NODES, _REPLAY_CASE_NAMES),
    _GateSelection("offline.concurrency_matrix", _CONCURRENCY_NODES, _CONCURRENCY_CASE_NAMES),
    _GateSelection("offline.cross_owner_matrix", _CROSS_OWNER_NODES, _CROSS_OWNER_CASE_NAMES),
)
_SOURCE_AUTHORITY_PATHS: Final = tuple(
    sorted(
        {
            *(node.split("::", 1)[0] for selection in _SELECTIONS for node in selection.node_ids),
            "tools/phase66_browser",
            "web",
        }
    )
)


def _canonical(value: object) -> bytes:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _render(value: object) -> bytes:
    return (
        json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=True,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")


def _digest(value: object) -> str:
    return sha256(_canonical(value)).hexdigest()


def _canonical_now(clock: Callable[[], datetime]) -> str:
    value = clock()
    if value.tzinfo is None or value.utcoffset() is None:
        raise Phase66OfflineEvidenceError("The evidence clock must provide aware UTC time")
    return value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _parse_canonical_timestamp(value: str) -> datetime:
    if _CANONICAL_TIMESTAMP.fullmatch(value) is None:
        raise Phase66OfflineEvidenceError(
            "Evidence completion time is not canonical microsecond UTC text"
        )
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _confined_repository_path(path: Path) -> Path:
    candidate = Path(os.path.abspath(path))
    try:
        candidate.relative_to(REPOSITORY_ROOT)
    except ValueError:
        raise Phase66OfflineEvidenceError(
            "Offline evidence inputs must stay in the repository"
        ) from None
    current = REPOSITORY_ROOT
    for component in candidate.relative_to(REPOSITORY_ROOT).parts[:-1]:
        current /= component
        try:
            metadata = current.lstat()
        except OSError:
            raise Phase66OfflineEvidenceError(
                "An offline evidence input parent is unavailable"
            ) from None
        if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
            raise Phase66OfflineEvidenceError(
                "An offline evidence input parent is not a stable directory"
            )
    return candidate


def _confined_private_path(path: Path) -> Path:
    candidate = _confined_repository_path(path)
    try:
        candidate.relative_to(PRIVATE_WORKSPACE_ROOT)
    except ValueError:
        raise Phase66OfflineEvidenceError(
            "Offline evidence outputs must stay in the private acceptance workspace"
        ) from None
    return candidate


def _ensure_private_workspace() -> None:
    current = REPOSITORY_ROOT
    for component in PRIVATE_WORKSPACE_ROOT.relative_to(REPOSITORY_ROOT).parts:
        current /= component
        try:
            metadata = current.lstat()
        except FileNotFoundError:
            try:
                current.mkdir(mode=0o700)
            except OSError:
                raise Phase66OfflineEvidenceError(
                    "The private acceptance workspace could not be created"
                ) from None
            metadata = current.lstat()
        if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
            raise Phase66OfflineEvidenceError(
                "The private acceptance workspace is not a stable directory"
            )
        try:
            current.chmod(0o700)
        except OSError:
            raise Phase66OfflineEvidenceError(
                "The private acceptance workspace could not be secured"
            ) from None


def _read_exact_input(
    path: Path, expected_digest: str, expected_size: int, limit: int
) -> _InputFile:
    if re.fullmatch(r"[0-9a-f]{64}", expected_digest) is None:
        raise Phase66OfflineEvidenceError("An offline evidence input digest is invalid")
    if type(expected_size) is not int or not 1 <= expected_size <= limit:
        raise Phase66OfflineEvidenceError("An offline evidence input size is invalid")
    candidate = _confined_repository_path(path)
    descriptor: int | None = None
    try:
        descriptor = os.open(candidate, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or before.st_size != expected_size
        ):
            raise OSError
        chunks: list[bytes] = []
        remaining = before.st_size
        while remaining:
            chunk = os.read(descriptor, min(1024 * 1024, remaining))
            if not chunk:
                raise OSError
            chunks.append(chunk)
            remaining -= len(chunk)
        after = os.fstat(descriptor)
    except OSError:
        raise Phase66OfflineEvidenceError(
            "An offline evidence input is not one exact stable regular file"
        ) from None
    finally:
        if descriptor is not None:
            os.close(descriptor)
    if (
        before.st_dev,
        before.st_ino,
        before.st_size,
        before.st_mtime_ns,
        before.st_ctime_ns,
    ) != (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
        after.st_ctime_ns,
    ):
        raise Phase66OfflineEvidenceError("An offline evidence input changed during its read")
    payload = b"".join(chunks)
    actual = sha256(payload).hexdigest()
    if not secrets.compare_digest(actual, expected_digest):
        raise Phase66OfflineEvidenceError("An offline evidence input SHA-256 does not match")
    return _InputFile(payload, actual, len(payload), (before.st_dev, before.st_ino))


def _read_generated_junit(path: Path) -> bytes:
    descriptor: int | None = None
    try:
        path.chmod(0o600)
        descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or before.st_mode & 0o077
            or not 1 <= before.st_size <= 16 * 1024 * 1024
        ):
            raise OSError
        chunks: list[bytes] = []
        remaining = before.st_size
        while remaining:
            chunk = os.read(descriptor, min(1024 * 1024, remaining))
            if not chunk:
                raise OSError
            chunks.append(chunk)
            remaining -= len(chunk)
        payload = b"".join(chunks)
        after = os.fstat(descriptor)
    except OSError:
        raise Phase66OfflineEvidenceError(
            "Pytest did not produce one stable private JUnit file"
        ) from None
    finally:
        if descriptor is not None:
            os.close(descriptor)
    if len(payload) != before.st_size or (
        before.st_dev,
        before.st_ino,
        before.st_size,
        before.st_mtime_ns,
        before.st_ctime_ns,
    ) != (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
        after.st_ctime_ns,
    ):
        raise Phase66OfflineEvidenceError("Pytest JUnit output changed during its read")
    return payload


def _verify_source_authority(source_commit: str, source_commit_digest: str) -> None:
    if (
        source_commit != SOURCE_COMMIT
        or source_commit_digest != SOURCE_COMMIT_DIGEST
        or sha256(source_commit.encode("ascii")).hexdigest() != source_commit_digest
    ):
        raise Phase66OfflineEvidenceError("The exact Phase 6 source authority is required")
    try:
        commit = subprocess.run(
            ("git", "cat-file", "-e", f"{SOURCE_COMMIT}^{{commit}}"),
            cwd=REPOSITORY_ROOT,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=15,
        )
        drift = subprocess.run(
            ("git", "diff", "--quiet", SOURCE_COMMIT, "--", *_SOURCE_AUTHORITY_PATHS),
            cwd=REPOSITORY_ROOT,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        raise Phase66OfflineEvidenceError("Source authority could not be verified") from None
    if commit.returncode != 0 or drift.returncode != 0:
        raise Phase66OfflineEvidenceError(
            "The tested offline/browser source differs from the bound Phase 6 authority"
        )


def _bundle_authority() -> tuple[str, int]:
    root = REPOSITORY_ROOT / "web/dist"
    try:
        files = tuple(
            path for path in sorted(root.rglob("*")) if path.is_file() and not path.is_symlink()
        )
    except OSError:
        raise Phase66OfflineEvidenceError("The production seller bundle is unavailable") from None
    if not files or any(path.is_symlink() for path in root.rglob("*")):
        raise Phase66OfflineEvidenceError("The production seller bundle is empty or unstable")
    digest = sha256()
    for path in files:
        relative = path.relative_to(root).as_posix().encode("utf-8")
        contents = path.read_bytes()
        digest.update(len(relative).to_bytes(4, "big"))
        digest.update(relative)
        digest.update(len(contents).to_bytes(8, "big"))
        digest.update(sha256(contents).digest())
    return digest.hexdigest(), len(files)


def _default_pytest_runner(selection: _GateSelection, report_path: Path) -> int:
    environment = {
        "PATH": os.defpath,
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
        "PYTHONHASHSEED": "0",
    }
    try:
        completed = subprocess.run(
            (
                sys.executable,
                "-m",
                "pytest",
                "-q",
                "-p",
                "no:cacheprovider",
                "--tb=no",
                f"--junitxml={report_path}",
                *selection.node_ids,
            ),
            cwd=REPOSITORY_ROOT,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=300,
        )
    except (OSError, subprocess.TimeoutExpired):
        return 124
    return completed.returncode


def _parse_junit(
    payload: bytes,
    selection: _GateSelection,
    *,
    completed_at: str,
) -> _JUnitResult:
    lowered = payload.lower()
    if b"<!doctype" in lowered or b"<!entity" in lowered:
        raise Phase66OfflineEvidenceError("Pytest JUnit contains forbidden XML authority")
    try:
        root = ElementTree.fromstring(payload)
    except (ElementTree.ParseError, UnicodeError):
        raise Phase66OfflineEvidenceError("Pytest did not produce valid JUnit XML") from None
    if root.tag.rsplit("}", 1)[-1] not in {"testsuite", "testsuites"}:
        raise Phase66OfflineEvidenceError("Pytest JUnit has an unexpected root")
    suites = tuple(
        element for element in root.iter() if element.tag.rsplit("}", 1)[-1] == "testsuite"
    )
    if len(suites) != 1:
        raise Phase66OfflineEvidenceError("Pytest JUnit must contain one exact suite")
    cases: list[str] = []
    duration_seconds = 0.0
    failure_count = 0
    skipped_count = 0
    for element in root.iter():
        name = element.tag.rsplit("}", 1)[-1]
        if name == "testcase":
            case_name = element.attrib.get("name")
            if type(case_name) is not str or not case_name:
                raise Phase66OfflineEvidenceError("Pytest JUnit contains an unnamed case")
            cases.append(case_name)
            try:
                duration_seconds += float(element.attrib.get("time", "0"))
            except ValueError:
                raise Phase66OfflineEvidenceError(
                    "Pytest JUnit contains invalid duration"
                ) from None
        elif name in {"failure", "error"}:
            failure_count += 1
        elif name == "skipped":
            skipped_count += 1
    if (
        failure_count
        or skipped_count
        or len(cases) != len(set(cases))
        or tuple(cases) != selection.expected_case_names
        or duration_seconds < 0
        or not math.isfinite(duration_seconds)
    ):
        raise Phase66OfflineEvidenceError(
            f"The exact {selection.gate_id} pytest selection did not pass"
        )
    duration_ms = min(86_400_000, round(duration_seconds * 1000))
    suite = suites[0]
    try:
        declared_counts = tuple(
            int(suite.attrib.get(name, "-1")) for name in ("tests", "failures", "errors", "skipped")
        )
    except ValueError:
        raise Phase66OfflineEvidenceError("Pytest JUnit suite counts are invalid") from None
    if declared_counts != (len(cases), 0, 0, 0):
        raise Phase66OfflineEvidenceError("Pytest JUnit suite counts do not match its cases")
    return _JUnitResult(
        case_names=tuple(cases),
        raw_digest=sha256(payload).hexdigest(),
        raw_byte_count=len(payload),
        duration_ms=duration_ms,
        completed_at=completed_at,
    )


def _normalized_junit(
    selection: _GateSelection,
    result: _JUnitResult,
) -> bytes:
    root = ElementTree.Element("testsuites", {"name": "phase6.6-offline"})
    suite = ElementTree.SubElement(
        root,
        "testsuite",
        {
            "name": selection.gate_id,
            "tests": str(len(result.case_names)),
            "failures": "0",
            "errors": "0",
            "skipped": "0",
            "time": f"{result.duration_ms / 1000:.3f}",
            "timestamp": result.completed_at,
        },
    )
    properties = ElementTree.SubElement(suite, "properties")
    for name, value in (
        ("artifact_contract", "phase6.6-sanitized-pytest-junit-v1"),
        ("manifest_digest", phase66_manifest_digest()),
        ("raw_junit_byte_count", str(result.raw_byte_count)),
        ("raw_junit_digest", result.raw_digest),
        ("selection_digest", _digest(selection.node_ids)),
        ("source_commit_digest", SOURCE_COMMIT_DIGEST),
    ):
        ElementTree.SubElement(properties, "property", {"name": name, "value": value})
    for case_name in result.case_names:
        ElementTree.SubElement(
            suite,
            "testcase",
            {"classname": "phase66.offline", "name": case_name, "time": "0.000"},
        )
    ElementTree.indent(root, space="  ")
    return ElementTree.tostring(root, encoding="utf-8", xml_declaration=True) + b"\n"


def _canonical_archive_member(name: str) -> bool:
    if not name or "\x00" in name or "\\" in name or name.startswith("/"):
        return False
    candidate = name[:-1] if name.endswith("/") else name
    parts = candidate.split("/")
    return (
        bool(candidate)
        and ":" not in parts[0]
        and all(part not in {"", ".", ".."} for part in parts)
        and PurePosixPath(candidate).as_posix() == candidate
    )


def _validated_trace(engine: str, source: _InputFile, expected_trace_count: int) -> _TraceInput:
    members: list[tuple[str, bytes]] = []
    names: set[str] = set()
    trace_count = 0
    expanded = 0
    recognized_trace = False
    try:
        with zipfile.ZipFile(io.BytesIO(source.payload)) as archive:
            infos = archive.infolist()
            if not infos or len(infos) > _MAX_TRACE_MEMBERS or archive.testzip() is not None:
                raise ValueError
            for info in infos:
                if info.is_dir():
                    continue
                folded = info.filename.casefold()
                if (
                    not _canonical_archive_member(info.filename)
                    or folded in names
                    or info.flag_bits & 0x1
                    or stat.S_ISLNK(info.external_attr >> 16)
                ):
                    raise ValueError
                names.add(folded)
                expanded += info.file_size
                if expanded > _MAX_TRACE_EXPANDED_BYTES:
                    raise ValueError
                contents = archive.read(info)
                if any(
                    pattern.search(contents) is not None for pattern in _FORBIDDEN_TRACE_PATTERNS
                ):
                    raise ValueError
                if info.filename.endswith(".trace") and contents:
                    trace_count += 1
                    first_line = contents.splitlines()[0]
                    if len(first_line) <= 1_000_000:
                        candidate = json.loads(first_line)
                        recognized_trace = recognized_trace or (
                            isinstance(candidate, dict) and type(candidate.get("type")) is str
                        )
                members.append((info.filename, contents))
    except (
        IndexError,
        json.JSONDecodeError,
        KeyError,
        OSError,
        UnicodeError,
        ValueError,
        zipfile.BadZipFile,
    ):
        raise Phase66OfflineEvidenceError(
            f"The {engine} browser trace is not a sanitized Playwright trace"
        ) from None
    if trace_count != expected_trace_count or not recognized_trace:
        raise Phase66OfflineEvidenceError(
            f"The {engine} browser trace count does not match its gate report"
        )
    return _TraceInput(engine, source, tuple(members), trace_count)


def _combined_trace(traces: Sequence[_TraceInput]) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for trace in traces:
            for source_name, contents in sorted(trace.members):
                info = zipfile.ZipInfo(
                    filename=f"{trace.engine}/{source_name}",
                    date_time=(1980, 1, 1, 0, 0, 0),
                )
                info.compress_type = zipfile.ZIP_DEFLATED
                info.create_system = 3
                info.external_attr = (stat.S_IFREG | 0o600) << 16
                archive.writestr(info, contents)
    combined = output.getvalue()
    synthetic = _InputFile(combined, sha256(combined).hexdigest(), len(combined), (-1, -1))
    _validated_trace("combined", synthetic, sum(trace.trace_count for trace in traces))
    return combined


def _strict_json(payload: bytes) -> object:
    def reject_constant(_value: str) -> None:
        raise ValueError

    def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        value: dict[str, Any] = {}
        for key, nested in pairs:
            if key in value:
                raise ValueError
            value[key] = nested
        return value

    try:
        return json.loads(
            payload,
            parse_constant=reject_constant,
            object_pairs_hook=unique_object,
        )
    except (RecursionError, UnicodeDecodeError, ValueError, json.JSONDecodeError):
        raise Phase66OfflineEvidenceError("Browser gate must be strict JSON") from None


def _validated_browser_gate(
    source: _InputFile,
    trace_sources: Mapping[str, _InputFile],
    *,
    verified_at: str,
) -> tuple[_BrowserGate, tuple[_TraceInput, ...], bytes]:
    try:
        gate = _BrowserGate.model_validate(_strict_json(source.payload))
    except ValueError:
        raise Phase66OfflineEvidenceError(
            "Browser gate does not match the exact three-engine contract"
        ) from None
    bundle_digest, file_count = _bundle_authority()
    if gate.bundle_digest != bundle_digest or gate.bundle_file_count != file_count:
        raise Phase66OfflineEvidenceError("Browser gate does not bind the current exact bundle")
    verified = _parse_canonical_timestamp(verified_at)
    generated = gate.generated_at.astimezone(UTC)
    if generated > verified or verified - generated > _BROWSER_MAX_AGE:
        raise Phase66OfflineEvidenceError("Browser gate is not fresh for this evidence run")
    traces = tuple(
        _validated_trace(engine.engine, trace_sources[engine.engine], engine.trace_count)
        for engine in gate.engines
    )
    if len({trace.source.identity for trace in traces}) != len(traces):
        raise Phase66OfflineEvidenceError("Browser trace input inode reuse is forbidden")
    return gate, traces, _combined_trace(traces)


def _artifact(
    *,
    filename: str,
    contents: bytes,
    kind: ArtifactKind,
    artifact_format: ArtifactFormat,
) -> tuple[dict[str, object], dict[str, object]]:
    digest = sha256(contents).hexdigest()
    return (
        {
            "artifact_digest": digest,
            "artifact_format": artifact_format.value,
            "byte_count": len(contents),
            "kind": kind.value,
            "redaction_verified": True,
        },
        Phase66ArtifactFile.model_validate(
            {
                "artifact_digest": digest,
                "artifact_format": artifact_format,
                "kind": kind,
                "relative_path": filename,
            }
        ).model_dump(mode="json"),
    )


def _privacy() -> dict[str, object]:
    return {
        "forbidden_field_match_count": 0,
        "free_text_value_count": 0,
        "sanitizer_contract": "phase6.6-sanitized-evidence-v1",
        "sensitive_value_match_count": 0,
    }


_OBSERVED_COUNTS: Final = {
    "offline.replay_matrix": {
        "nine_mutation_routes_replay_exactly": 9,
        "changed_requests_conflict": 2,
        "one_job_graph_is_created": 1,
        "one_logical_work_graph_is_created": 1,
        "provider_transport_is_not_invoked": 0,
    },
    "offline.concurrency_matrix": {
        "revise_approve_cancel_have_one_winner": 1,
        "identical_requests_share_one_receipt": 1,
        "changed_requests_have_one_conflict": 1,
        "upload_completion_and_cancel_have_one_winner": 1,
        "dispatcher_and_worker_races_settle_once": 2,
        "provider_transport_is_not_invoked": 0,
    },
    "offline.cross_owner_matrix": {
        "fourteen_protected_routes_are_covered": 14,
        "foreign_resources_match_absence": 12,
        "foreign_job_list_is_empty": 0,
        "foreign_commands_write_nothing": 0,
        "identity_injection_is_rejected": 2,
        "provider_transport_is_not_invoked": 0,
    },
    "offline.browser_matrix": {
        "chromium_flow_passes": 1,
        "firefox_flow_passes": 1,
        "webkit_flow_passes": 1,
        "accessibility_matrix_passes": 3,
        "browser_restart_and_tab_recovery_pass": 3,
        "commerce_surface_is_absent": 0,
        "provider_transport_is_not_invoked": 0,
    },
}


def _record(
    *,
    gate_id: str,
    artifacts: Sequence[dict[str, object]],
    recorded_at: str,
    authority_digest: str,
    duration_ms: int,
) -> dict[str, object]:
    frozen = next(gate for gate in phase66_acceptance_manifest().gates if gate.gate_id == gate_id)
    run_digest = _digest(
        {
            "artifact_digests": [artifact["artifact_digest"] for artifact in artifacts],
            "authority_digest": authority_digest,
            "contract": "phase6.6-offline-evidence-run-v2",
            "gate_id": gate_id,
            "recorded_at": recorded_at,
            "source_commit_digest": SOURCE_COMMIT_DIGEST,
        }
    )
    assertions = []
    for assertion_id in frozen.required_assertions:
        observed_count = _OBSERVED_COUNTS[gate_id][assertion_id]
        assertions.append(
            {
                "assertion_id": assertion_id,
                "duration_ms": duration_ms,
                "observation_digest": _digest(
                    {
                        "assertion_id": assertion_id,
                        "authority_digest": authority_digest,
                        "observed_count": observed_count,
                        "run_digest": run_digest,
                    }
                ),
                "observed_count": observed_count,
                "passed": True,
            }
        )
    model = validate_phase66_evidence(
        {
            "actor_digests": [],
            "artifacts": list(artifacts),
            "assertions": assertions,
            "correlation_digest": None,
            "deployment_digest": None,
            "evidence_class": AcceptanceEvidenceClass.OFFLINE.value,
            "gate_id": gate_id,
            "job_digest": None,
            "manifest_digest": phase66_manifest_digest(),
            "moderated_session": None,
            "outcome": AcceptanceOutcome.PASSED.value,
            "privacy": _privacy(),
            "provider_call_summary": None,
            "provider_gate_attestation": None,
            "recorded_at": recorded_at,
            "run_digest": run_digest,
            "schema_version": "6.6.0",
            "source_commit_digest": SOURCE_COMMIT_DIGEST,
            "work_digest": None,
        }
    )
    return model.model_dump(mode="json")


def _create_fresh_output_root(run_root: Path) -> Path:
    candidate = _confined_private_path(run_root)
    if candidate == PRIVATE_WORKSPACE_ROOT:
        raise Phase66OfflineEvidenceError("The private workspace itself cannot be an evidence run")
    parent = candidate.parent
    if parent != PRIVATE_WORKSPACE_ROOT:
        raise Phase66OfflineEvidenceError(
            "Offline evidence run roots must be direct fresh children"
        )
    if candidate.exists() or candidate.is_symlink():
        raise Phase66OfflineEvidenceError("Offline evidence output root must be fresh")
    try:
        candidate.mkdir(mode=0o700)
    except OSError:
        raise Phase66OfflineEvidenceError(
            "Offline evidence output root could not be created"
        ) from None
    metadata = candidate.lstat()
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or metadata.st_mode & 0o077
    ):
        raise Phase66OfflineEvidenceError("Offline evidence output root is not owner-only")
    return candidate


def _write_once(path: Path, contents: bytes) -> None:
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(12)}.tmp")
    descriptor: int | None = None
    try:
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
            0o600,
        )
        with os.fdopen(descriptor, "wb", closefd=True) as stream:
            descriptor = None
            stream.write(contents)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, path, follow_symlinks=False)
        temporary.unlink()
        path.chmod(0o600)
    except OSError:
        raise Phase66OfflineEvidenceError(
            "An immutable offline evidence file could not be written"
        ) from None
    finally:
        if descriptor is not None:
            os.close(descriptor)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _verify_fragment(run_root: Path, records: object, files: object) -> dict[str, object]:
    if not isinstance(records, list) or not isinstance(files, list):
        raise Phase66OfflineEvidenceError("Offline evidence indexes must be arrays")
    try:
        validated = _validated_records(records)
        if tuple(record.gate_id for record in validated) != OFFLINE_GATE_ORDER:
            raise ValueError
        if any(record.source_commit_digest != SOURCE_COMMIT_DIGEST for record in validated):
            raise ValueError
        if len({record.run_digest for record in validated}) != 4:
            raise ValueError
        times = {record.gate_id: record.recorded_at for record in validated}
        gate_index = {gate.gate_id: gate for gate in phase66_acceptance_manifest().gates}
        for record in validated:
            for prerequisite in gate_index[record.gate_id].prerequisites:
                if prerequisite in times and record.recorded_at < times[prerequisite]:
                    raise ValueError
        declared = _declared_artifacts(validated)
        references = _validated_artifact_files(files)
        artifact_bytes = _verify_artifacts(declared, references, run_root)
    except ValueError:
        raise Phase66OfflineEvidenceError(
            "The four-record offline evidence fragment failed closed validation"
        ) from None
    if len(declared) != 5 or len(references) != 5:
        raise Phase66OfflineEvidenceError(
            "The offline fragment does not bind exactly five artifacts"
        )
    return {
        "artifact_byte_count": artifact_bytes,
        "artifact_count": len(declared),
        "gate_count": len(validated),
        "manifest_digest": phase66_manifest_digest(),
        "record_set_digest": _digest(records),
        "result": "passed",
        "source_commit_digest": SOURCE_COMMIT_DIGEST,
    }


def prepare_phase66_offline_evidence(
    *,
    run_root: Path,
    source_commit: str,
    source_commit_digest: str,
    browser_gate_path: Path,
    browser_gate_sha256: str,
    browser_gate_size: int,
    browser_trace_paths: Mapping[str, Path],
    browser_trace_sha256: Mapping[str, str],
    browser_trace_sizes: Mapping[str, int],
    pytest_runner: Callable[[_GateSelection, Path], int] = _default_pytest_runner,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> dict[str, object]:
    """Run exact offline selections and write one fresh, byte-verified evidence fragment."""

    _ensure_private_workspace()
    _verify_source_authority(source_commit, source_commit_digest)
    started_at = _canonical_now(clock)
    gate_input = _read_exact_input(
        browser_gate_path,
        browser_gate_sha256,
        browser_gate_size,
        _MAX_BROWSER_GATE_BYTES,
    )
    if any(
        set(values) != set(ENGINE_ORDER)
        for values in (
            browser_trace_paths,
            browser_trace_sha256,
            browser_trace_sizes,
        )
    ):
        raise Phase66OfflineEvidenceError("Exactly three named browser trace bindings are required")
    trace_inputs = {
        engine: _read_exact_input(
            browser_trace_paths[engine],
            browser_trace_sha256[engine],
            browser_trace_sizes[engine],
            _MAX_TRACE_BYTES,
        )
        for engine in ENGINE_ORDER
    }
    if len({gate_input.identity, *(item.identity for item in trace_inputs.values())}) != 4:
        raise Phase66OfflineEvidenceError("Browser report and trace inode reuse is forbidden")

    staging = Path(tempfile.mkdtemp(prefix=".offline-evidence-stage-", dir=PRIVATE_WORKSPACE_ROOT))
    staging.chmod(0o700)
    junit_results: dict[str, _JUnitResult] = {}
    try:
        for index, selection in enumerate(_SELECTIONS):
            raw_path = staging / f"selection-{index}.xml"
            return_code = pytest_runner(selection, raw_path)
            completed_at = _canonical_now(clock)
            if return_code != 0:
                raise Phase66OfflineEvidenceError(
                    f"The exact {selection.gate_id} pytest selection failed"
                )
            raw = _read_generated_junit(raw_path)
            junit_results[selection.gate_id] = _parse_junit(
                raw,
                selection,
                completed_at=completed_at,
            )

        browser_observed_at = _canonical_now(clock)
        browser_gate, traces, combined_trace = _validated_browser_gate(
            gate_input,
            trace_inputs,
            verified_at=browser_observed_at,
        )
        browser_completed_at = _canonical_now(clock)
        generated = browser_gate.generated_at.astimezone(UTC)
        completed = _parse_canonical_timestamp(browser_completed_at)
        if generated > completed or completed - generated > _BROWSER_MAX_AGE:
            raise Phase66OfflineEvidenceError("Browser gate is not fresh for this evidence run")
    finally:
        shutil.rmtree(staging, ignore_errors=True)

    ordered_times = (
        started_at,
        *(junit_results[selection.gate_id].completed_at for selection in _SELECTIONS),
        browser_observed_at,
        browser_completed_at,
    )
    parsed_times = tuple(_parse_canonical_timestamp(value) for value in ordered_times)
    if any(later < earlier for earlier, later in pairwise(parsed_times)):
        raise Phase66OfflineEvidenceError("The evidence clock moved backward during the run")

    outputs: dict[str, bytes] = {}
    references: list[dict[str, object]] = []
    records: list[dict[str, object]] = []
    for selection in _SELECTIONS:
        result = junit_results[selection.gate_id]
        filename = selection.gate_id.replace(".", "-").replace("_", "-") + ".junit.xml"
        contents = _normalized_junit(selection, result)
        evidence, reference = _artifact(
            filename=filename,
            contents=contents,
            kind=ArtifactKind.TEST_REPORT,
            artifact_format=ArtifactFormat.JUNIT_XML,
        )
        outputs[filename] = contents
        references.append(reference)
        records.append(
            _record(
                gate_id=selection.gate_id,
                artifacts=(evidence,),
                recorded_at=result.completed_at,
                authority_digest=_digest(
                    {
                        "raw_junit_digest": result.raw_digest,
                        "selection_digest": _digest(selection.node_ids),
                    }
                ),
                duration_ms=result.duration_ms,
            )
        )

    browser_report = {
        "artifact_contract": "phase6.6-sanitized-three-engine-browser-report-v2",
        "browser_bundle_digest": browser_gate.bundle_digest,
        "browser_gate_generated_at": browser_gate.generated_at.astimezone(UTC)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z"),
        "browser_gate_source_byte_count": gate_input.byte_count,
        "browser_gate_source_digest": gate_input.digest,
        "engine_pass_count": 3,
        "failed_engine_count": 0,
        "gate": "offline.browser_matrix",
        "manifest_digest": phase66_manifest_digest(),
        "provider_transport_attempt_count": 0,
        "result": "passed",
        "source_commit_digest": SOURCE_COMMIT_DIGEST,
        "trace_sources": {
            trace.engine: {
                "byte_count": trace.source.byte_count,
                "digest": trace.source.digest,
                "trace_count": trace.trace_count,
            }
            for trace in traces
        },
        "verified_at": browser_completed_at,
    }
    browser_report_bytes = _render(browser_report)
    report_evidence, report_reference = _artifact(
        filename=BROWSER_REPORT_FILENAME,
        contents=browser_report_bytes,
        kind=ArtifactKind.TEST_REPORT,
        artifact_format=ArtifactFormat.JSON,
    )
    trace_evidence, trace_reference = _artifact(
        filename=BROWSER_TRACE_FILENAME,
        contents=combined_trace,
        kind=ArtifactKind.BROWSER_TRACE,
        artifact_format=ArtifactFormat.ZIP,
    )
    outputs[BROWSER_REPORT_FILENAME] = browser_report_bytes
    outputs[BROWSER_TRACE_FILENAME] = combined_trace
    references.extend((report_reference, trace_reference))
    records.append(
        _record(
            gate_id="offline.browser_matrix",
            artifacts=(report_evidence, trace_evidence),
            recorded_at=browser_completed_at,
            authority_digest=_digest(
                {
                    "browser_gate_digest": gate_input.digest,
                    "combined_trace_digest": trace_evidence["artifact_digest"],
                    "source_trace_digests": [trace.source.digest for trace in traces],
                }
            ),
            duration_ms=0,
        )
    )
    if tuple(record["gate_id"] for record in records) != OFFLINE_GATE_ORDER:
        raise Phase66OfflineEvidenceError("Offline evidence record order drifted")

    outputs[RECORDS_FILENAME] = _render(records)
    outputs[ARTIFACT_FILES_FILENAME] = _render(references)
    output_root = _create_fresh_output_root(run_root)
    for filename, contents in outputs.items():
        _write_once(output_root / filename, contents)
    records_value = _strict_json(outputs[RECORDS_FILENAME])
    files_value = _strict_json(outputs[ARTIFACT_FILES_FILENAME])
    summary = _verify_fragment(output_root, records_value, files_value)
    for path in output_root.iterdir():
        metadata = path.lstat()
        if (
            not stat.S_ISREG(metadata.st_mode)
            or stat.S_ISLNK(metadata.st_mode)
            or metadata.st_mode & 0o077
            or metadata.st_nlink != 1
        ):
            raise Phase66OfflineEvidenceError(
                "An offline evidence output is not immutable owner-only"
            )
    return summary


def _positive_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("value must be an integer") from None
    if parsed < 1:
        raise argparse.ArgumentTypeError("value must be positive")
    return parsed


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-root", required=True, type=Path)
    parser.add_argument("--source-commit", required=True)
    parser.add_argument("--source-commit-digest", required=True)
    parser.add_argument("--browser-gate", required=True, type=Path)
    parser.add_argument("--browser-gate-sha256", required=True)
    parser.add_argument("--browser-gate-size", required=True, type=_positive_int)
    for engine in ENGINE_ORDER:
        parser.add_argument(f"--{engine}-trace", required=True, type=Path)
        parser.add_argument(f"--{engine}-trace-sha256", required=True)
        parser.add_argument(f"--{engine}-trace-size", required=True, type=_positive_int)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    arguments = parser.parse_args(argv)
    try:
        summary = prepare_phase66_offline_evidence(
            run_root=arguments.run_root,
            source_commit=arguments.source_commit,
            source_commit_digest=arguments.source_commit_digest,
            browser_gate_path=arguments.browser_gate,
            browser_gate_sha256=arguments.browser_gate_sha256,
            browser_gate_size=arguments.browser_gate_size,
            browser_trace_paths={
                engine: getattr(arguments, f"{engine}_trace") for engine in ENGINE_ORDER
            },
            browser_trace_sha256={
                engine: getattr(arguments, f"{engine}_trace_sha256") for engine in ENGINE_ORDER
            },
            browser_trace_sizes={
                engine: getattr(arguments, f"{engine}_trace_size") for engine in ENGINE_ORDER
            },
        )
    except Phase66OfflineEvidenceError as error:
        parser.error(str(error))
    print(_render(summary).decode("utf-8"), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
