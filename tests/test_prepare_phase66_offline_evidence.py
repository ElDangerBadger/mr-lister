from __future__ import annotations

import io
import json
import stat
import xml.etree.ElementTree as ElementTree
import zipfile
from collections.abc import Callable, Iterator
from datetime import datetime
from hashlib import sha256
from pathlib import Path
from typing import Any

import pytest

from mr_lister.acceptance.phase6 import validate_phase66_evidence
from tools import prepare_phase66_offline_evidence as offline


def _render(value: object) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True) + "\n").encode()


def _write(path: Path, contents: bytes) -> tuple[str, int]:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(contents)
    return sha256(contents).hexdigest(), len(contents)


def _trace(engine: str, *, forbidden: bytes | None = None) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for index in range(2):
            body = (
                json.dumps({"type": "context-options", "engine": engine}).encode()
                + b"\n"
                + (forbidden or b'{"type":"event"}')
            )
            archive.writestr(f"session-{index}.trace", body)
        archive.writestr("resources/safe.json", b'{"result":"passed"}')
    return output.getvalue()


def _junit(case_names: tuple[str, ...], *, failed: bool = False) -> bytes:
    root = ElementTree.Element("testsuites")
    suite = ElementTree.SubElement(
        root,
        "testsuite",
        {
            "tests": str(len(case_names)),
            "failures": "1" if failed else "0",
            "errors": "0",
            "skipped": "0",
        },
    )
    for index, name in enumerate(case_names):
        case = ElementTree.SubElement(
            suite,
            "testcase",
            {"classname": "tests.fixed", "name": name, "time": "0.001"},
        )
        if failed and index == 0:
            ElementTree.SubElement(case, "failure")
    return ElementTree.tostring(root, encoding="utf-8", xml_declaration=True)


def _runner(
    transform: Callable[[offline._GateSelection], tuple[str, ...]] | None = None,
) -> Callable[[offline._GateSelection, Path], int]:
    def run(selection: offline._GateSelection, report_path: Path) -> int:
        cases = selection.expected_case_names if transform is None else transform(selection)
        report_path.write_bytes(_junit(cases))
        return 0

    return run


def _clock(values: tuple[str, ...]) -> Callable[[], datetime]:
    remaining: Iterator[str] = iter(values)

    def now() -> datetime:
        return datetime.fromisoformat(next(remaining).replace("Z", "+00:00"))

    return now


def _times(*minutes: int) -> tuple[str, ...]:
    return tuple(f"2026-08-29T22:{minute:02d}:00.000000Z" for minute in minutes)


def _browser_gate(bundle_digest: str, bundle_count: int, **changes: object) -> bytes:
    flow = {
        "accessibility_flow.js": {
            "forcedColors": "passed",
            "horizontalOverflow": 0,
            "reducedMotion": "passed",
            "reflowAt200PercentEquivalent": "passed at 360 CSS pixels",
        },
        "auth_review_flow.js": {
            "approvalAttempts": 1,
            "authRouteRecovery": "passed",
            "commerceControls": 0,
            "exactTagCount": 13,
            "listingValidation": "passed",
            "providerTransportAttempts": 0,
            "staleReadbackFocus": "passed",
            "strandsProminence": "passed",
            "tabRecovery": "passed",
            "unpublishedBoundary": "passed",
        },
        "browser_restart_flow.js": {
            "approvalAttempts": 1,
            "browserRestartRecovery": "passed",
            "durableApprovedRecovery": "passed",
            "providerTransportAttempts": 0,
        },
        "route_polling_flow.js": {
            "hiddenPollingSuppressed": "passed",
            "offlinePollingSuppressed": "passed",
            "resumePolling": "passed",
            "routeAtoBIsolation": "passed",
        },
    }
    value: dict[str, object] = {
        "attestation_eligible": True,
        "bundle_digest": bundle_digest,
        "bundle_file_count": bundle_count,
        "engines": [
            {
                "engine": engine,
                "flows": flow,
                "restart_setup": {
                    "fixtureReady": True,
                    "fixtureStatePreserved": True,
                    "proxyRoutes": 4,
                },
                "setup": {
                    "fixtureReady": True,
                    "providerSentinelVerified": True,
                    "proxyRoutes": 4,
                },
                "status": "passed",
                "trace_count": 2,
            }
            for engine in offline.ENGINE_ORDER
        ],
        "gate": "offline.browser_matrix",
        "generated_at": "2026-08-29T22:00:00.000000+00:00",
    }
    value.update(changes)
    return _render(value)


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    repository = tmp_path / "repository"
    private = repository / ".mr_lister_private/phase66-acceptance"
    (repository / "web/dist").mkdir(parents=True)
    (repository / "web/dist/index.html").write_bytes(b"exact production bundle")
    monkeypatch.setattr(offline, "REPOSITORY_ROOT", repository)
    monkeypatch.setattr(offline, "PRIVATE_WORKSPACE_ROOT", private)

    def source_authority(source_commit: str, source_commit_digest: str) -> None:
        if (
            source_commit != offline.SOURCE_COMMIT
            or source_commit_digest != offline.SOURCE_COMMIT_DIGEST
        ):
            raise offline.Phase66OfflineEvidenceError(
                "The exact Phase 6 source authority is required"
            )

    monkeypatch.setattr(offline, "_verify_source_authority", source_authority)
    bundle_digest, bundle_count = offline._bundle_authority()

    browser_root = repository / "output/playwright/phase66/fresh"
    gate_path = browser_root / "browser-gate.json"
    gate_digest, gate_size = _write(
        gate_path,
        _browser_gate(bundle_digest, bundle_count),
    )
    trace_paths: dict[str, Path] = {}
    trace_digests: dict[str, str] = {}
    trace_sizes: dict[str, int] = {}
    for engine in offline.ENGINE_ORDER:
        path = browser_root / engine / "browser-trace.zip"
        digest, size = _write(path, _trace(engine))
        trace_paths[engine] = path
        trace_digests[engine] = digest
        trace_sizes[engine] = size
    return {
        "private": private,
        "gate_path": gate_path,
        "gate_digest": gate_digest,
        "gate_size": gate_size,
        "trace_paths": trace_paths,
        "trace_digests": trace_digests,
        "trace_sizes": trace_sizes,
    }


def _prepare(
    workspace: dict[str, Any],
    *,
    name: str = "fresh-offline",
    runner: Callable[[offline._GateSelection, Path], int] | None = None,
    clock: Callable[[], datetime] | None = None,
) -> dict[str, object]:
    return offline.prepare_phase66_offline_evidence(
        run_root=workspace["private"] / name,
        source_commit=offline.SOURCE_COMMIT,
        source_commit_digest=offline.SOURCE_COMMIT_DIGEST,
        browser_gate_path=workspace["gate_path"],
        browser_gate_sha256=workspace["gate_digest"],
        browser_gate_size=workspace["gate_size"],
        browser_trace_paths=workspace["trace_paths"],
        browser_trace_sha256=workspace["trace_digests"],
        browser_trace_sizes=workspace["trace_sizes"],
        pytest_runner=runner or _runner(),
        clock=clock or _clock(_times(1, 2, 3, 4, 5, 6)),
    )


def test_producer_emits_exact_owner_only_four_record_fragment(
    workspace: dict[str, Any],
) -> None:
    summary = _prepare(workspace)
    root = workspace["private"] / "fresh-offline"

    assert summary["result"] == "passed"
    assert summary["gate_count"] == 4
    assert summary["artifact_count"] == 5
    assert summary["source_commit_digest"] == offline.SOURCE_COMMIT_DIGEST
    assert {path.name for path in root.iterdir()} == {
        "offline-replay-matrix.junit.xml",
        "offline-concurrency-matrix.junit.xml",
        "offline-cross-owner-matrix.junit.xml",
        offline.BROWSER_REPORT_FILENAME,
        offline.BROWSER_TRACE_FILENAME,
        offline.RECORDS_FILENAME,
        offline.ARTIFACT_FILES_FILENAME,
    }
    assert stat.S_IMODE(root.stat().st_mode) == 0o700
    assert all(stat.S_IMODE(path.stat().st_mode) == 0o600 for path in root.iterdir())
    assert all(path.stat().st_nlink == 1 for path in root.iterdir())

    records = json.loads((root / offline.RECORDS_FILENAME).read_bytes())
    parsed = [validate_phase66_evidence(value) for value in records]
    assert [record.gate_id for record in parsed] == list(offline.OFFLINE_GATE_ORDER)
    assert [record.recorded_at.minute for record in parsed] == [2, 3, 4, 6]
    assert all(record.source_commit_digest == offline.SOURCE_COMMIT_DIGEST for record in parsed)

    with zipfile.ZipFile(root / offline.BROWSER_TRACE_FILENAME) as archive:
        names = archive.namelist()
        assert all(name.split("/", 1)[0] in offline.ENGINE_ORDER for name in names)
        assert sum(name.endswith(".trace") for name in names) == 6
        assert not any("raw" in name or "cli.log" in name for name in names)


def test_junit_artifacts_bind_actual_exact_case_selections(workspace: dict[str, Any]) -> None:
    _prepare(workspace)
    root = workspace["private"] / "fresh-offline"

    for selection in offline._SELECTIONS:
        filename = selection.gate_id.replace(".", "-").replace("_", "-") + ".junit.xml"
        xml = ElementTree.fromstring((root / filename).read_bytes())
        cases = tuple(element.attrib["name"] for element in xml.iter("testcase"))
        properties = {
            element.attrib["name"]: element.attrib["value"] for element in xml.iter("property")
        }
        assert cases == selection.expected_case_names
        assert properties["selection_digest"] == offline._digest(selection.node_ids)
        assert properties["source_commit_digest"] == offline.SOURCE_COMMIT_DIGEST
        assert len(properties["raw_junit_digest"]) == 64


def test_missing_or_failed_junit_case_fails_before_output(workspace: dict[str, Any]) -> None:
    def missing(selection: offline._GateSelection) -> tuple[str, ...]:
        if selection.gate_id == "offline.concurrency_matrix":
            return selection.expected_case_names[:-1]
        return selection.expected_case_names

    with pytest.raises(offline.Phase66OfflineEvidenceError, match="exact.*selection"):
        _prepare(workspace, runner=_runner(missing))

    assert not (workspace["private"] / "fresh-offline").exists()


def test_browser_contract_rejects_nonpassing_or_stale_report(workspace: dict[str, Any]) -> None:
    report = json.loads(workspace["gate_path"].read_bytes())
    report["engines"][1]["status"] = "failed"
    digest, size = _write(workspace["gate_path"], _render(report))
    workspace["gate_digest"] = digest
    workspace["gate_size"] = size
    with pytest.raises(offline.Phase66OfflineEvidenceError, match="three-engine contract"):
        _prepare(workspace)

    report["engines"][1]["status"] = "passed"
    report["generated_at"] = "2026-08-28T22:00:00.000000+00:00"
    digest, size = _write(workspace["gate_path"], _render(report))
    workspace["gate_digest"] = digest
    workspace["gate_size"] = size
    with pytest.raises(offline.Phase66OfflineEvidenceError, match="not fresh"):
        _prepare(workspace, name="stale-offline")


def test_each_trace_is_bound_and_privacy_rescanned(workspace: dict[str, Any]) -> None:
    firefox = workspace["trace_paths"]["firefox"]
    digest, size = _write(firefox, _trace("firefox", forbidden=b"Authorization: Bearer nope"))
    workspace["trace_digests"]["firefox"] = digest
    workspace["trace_sizes"]["firefox"] = size

    with pytest.raises(offline.Phase66OfflineEvidenceError, match="firefox.*sanitized"):
        _prepare(workspace)

    assert not (workspace["private"] / "fresh-offline").exists()


def test_clock_must_advance_naturally_without_synthetic_offsets(workspace: dict[str, Any]) -> None:
    clock = _clock(_times(1, 2, 3, 2, 5, 6))

    with pytest.raises(offline.Phase66OfflineEvidenceError, match="clock moved backward"):
        _prepare(workspace, clock=clock)

    assert not (workspace["private"] / "fresh-offline").exists()


def test_exact_source_authority_and_fresh_output_are_fail_closed(
    workspace: dict[str, Any],
) -> None:
    with pytest.raises(offline.Phase66OfflineEvidenceError, match="source authority"):
        offline.prepare_phase66_offline_evidence(
            run_root=workspace["private"] / "wrong-source",
            source_commit="0" * 40,
            source_commit_digest=offline.SOURCE_COMMIT_DIGEST,
            browser_gate_path=workspace["gate_path"],
            browser_gate_sha256=workspace["gate_digest"],
            browser_gate_size=workspace["gate_size"],
            browser_trace_paths=workspace["trace_paths"],
            browser_trace_sha256=workspace["trace_digests"],
            browser_trace_sizes=workspace["trace_sizes"],
            pytest_runner=_runner(),
            clock=_clock(_times(1, 2, 3, 4, 5, 6)),
        )

    _prepare(workspace)
    with pytest.raises(offline.Phase66OfflineEvidenceError, match="must be fresh"):
        _prepare(workspace)
