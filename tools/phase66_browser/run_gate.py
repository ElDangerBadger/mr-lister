"""Run the Phase 6.6 exact-bundle browser gate through Playwright CLI."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from tools.phase66_browser.fixture_server import create_server

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
HARNESS_ROOT = Path(__file__).resolve().parent
DEFAULT_PLAYWRIGHT_WRAPPER = (
    Path.home() / ".codex" / "skills" / "playwright" / "scripts" / "playwright_cli.sh"
)
ENGINE_MAP = {"chromium": "chrome", "firefox": "firefox", "webkit": "webkit"}
FLOW_FILES = ("auth_review_flow.js", "route_polling_flow.js", "accessibility_flow.js")
_TRACE_REDACTIONS = (
    re.compile(rb"phase66-(?:access|refresh)-token", re.IGNORECASE),
    re.compile(rb"authorization", re.IGNORECASE),
    re.compile(rb"bearer", re.IGNORECASE),
    re.compile(rb"access[_-]?token", re.IGNORECASE),
    re.compile(rb"refresh[_-]?token", re.IGNORECASE),
    re.compile(rb"id[_-]?token", re.IGNORECASE),
    re.compile(rb"code_verifier", re.IGNORECASE),
    re.compile(rb"set-cookie", re.IGNORECASE),
    re.compile(rb"cookie", re.IGNORECASE),
)


class GateFailure(RuntimeError):
    """A browser engine or harness command failed."""


def _bundle_authority() -> tuple[str, int]:
    files = tuple(
        path for path in sorted((REPOSITORY_ROOT / "web" / "dist").rglob("*")) if path.is_file()
    )
    if not files:
        raise GateFailure("the production seller bundle is empty")
    digest = hashlib.sha256()
    for path in files:
        relative = path.relative_to(REPOSITORY_ROOT / "web" / "dist").as_posix().encode()
        contents = path.read_bytes()
        digest.update(len(relative).to_bytes(4, "big"))
        digest.update(relative)
        digest.update(len(contents).to_bytes(8, "big"))
        digest.update(hashlib.sha256(contents).digest())
    return digest.hexdigest(), len(files)


def _redact_trace_bytes(contents: bytes) -> bytes:
    redacted = contents
    for pattern in _TRACE_REDACTIONS:
        redacted = pattern.sub(b"redacted", redacted)
    return redacted


def _archive_trace(engine_root: Path) -> tuple[Path, int]:
    trace_root = engine_root / ".playwright-cli" / "traces"
    files = tuple(path for path in sorted(trace_root.rglob("*")) if path.is_file())
    if not files:
        raise GateFailure("browser trace artifact was not produced")
    raw_archive = engine_root / "browser-trace.raw.zip"
    archive = engine_root / "browser-trace.zip"
    with (
        zipfile.ZipFile(raw_archive, "w", compression=zipfile.ZIP_DEFLATED) as raw_output,
        zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as output,
    ):
        for path in files:
            relative = path.relative_to(trace_root)
            contents = path.read_bytes()
            raw_output.writestr(relative.as_posix(), contents)
            output.writestr(relative.as_posix(), _redact_trace_bytes(contents))
    trace_count = sum(path.suffix == ".trace" for path in files)
    if archive.stat().st_size <= 0 or raw_archive.stat().st_size <= 0 or trace_count < 1:
        raise GateFailure("browser trace artifact was not produced")
    with zipfile.ZipFile(archive) as sanitized:
        if sanitized.testzip() is not None:
            raise GateFailure("sanitized browser trace is corrupt")
        for name in sanitized.namelist():
            contents = sanitized.read(name)
            if any(pattern.search(contents) is not None for pattern in _TRACE_REDACTIONS):
                raise GateFailure("sanitized browser trace retained authority material")
    return archive, trace_count


def _run(
    command: list[str],
    *,
    cwd: Path,
    timeout: int = 180,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    process = subprocess.Popen(
        command,
        cwd=cwd,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGTERM)
        try:
            process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.communicate()
        raise GateFailure("browser command timed out") from None
    result = subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
    if check and result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise GateFailure(f"command failed ({result.returncode}): {' '.join(command)}\n{detail}")
    return result


def _cli(
    wrapper: Path,
    session: str,
    arguments: list[str],
    *,
    cwd: Path,
    raw: bool = False,
    timeout: int = 90,
) -> subprocess.CompletedProcess[str]:
    command = [str(wrapper)]
    if raw:
        command.append("--raw")
    command.extend([f"-s={session}", *arguments])
    return _run(command, cwd=cwd, timeout=timeout)


def _parse_raw_json(result: subprocess.CompletedProcess[str]) -> dict[str, Any]:
    try:
        value = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise GateFailure(
            f"Playwright CLI returned non-JSON evidence: {result.stdout!r}"
        ) from error
    if not isinstance(value, dict):
        raise GateFailure("Playwright CLI evidence must be a JSON object")
    return value


def _run_engine(
    label: str,
    cli_browser: str,
    wrapper: Path,
    bootstrap_url: str,
    artifact_root: Path,
) -> dict[str, Any]:
    engine_root = artifact_root / label
    engine_root.mkdir(parents=True, exist_ok=False)
    base_session = f"phase66-{label}-{os.getpid()}"
    session = base_session
    log: list[str] = []
    result: dict[str, Any] = {"engine": label, "status": "failed"}
    opened = False
    tracing = False
    try:
        opened_result = _cli(
            wrapper,
            session,
            ["open", bootstrap_url, "--browser", cli_browser],
            cwd=engine_root,
        )
        opened = True
        log.append(opened_result.stdout)
        setup = _parse_raw_json(
            _cli(
                wrapper,
                session,
                ["run-code", "--filename", str(HARNESS_ROOT / "setup.js")],
                cwd=engine_root,
                raw=True,
            )
        )
        trace_started = _cli(wrapper, session, ["tracing-start"], cwd=engine_root)
        log.append(trace_started.stdout)
        tracing = True
        flow_evidence: dict[str, Any] = {}
        for flow_file in FLOW_FILES:
            flow_evidence[flow_file] = _parse_raw_json(
                _cli(
                    wrapper,
                    session,
                    ["run-code", "--filename", str(HARNESS_ROOT / flow_file)],
                    cwd=engine_root,
                    raw=True,
                )
            )
        trace_stopped = _cli(wrapper, session, ["tracing-stop"], cwd=engine_root)
        log.append(trace_stopped.stdout)
        tracing = False
        snapshot = _cli(wrapper, session, ["snapshot"], cwd=engine_root)
        screenshot = _cli(wrapper, session, ["screenshot"], cwd=engine_root)
        log.extend([snapshot.stdout, screenshot.stdout])
        closed = _cli(wrapper, session, ["close"], cwd=engine_root, timeout=30)
        log.append(closed.stdout or closed.stderr)
        opened = False

        # A second tab does not prove recovery after browser-process loss. Open a new CLI
        # session/process, re-establish only the route fixtures, and perform the full PKCE return.
        session = f"{base_session}-restart"
        reopened = _cli(
            wrapper,
            session,
            ["open", bootstrap_url, "--browser", cli_browser],
            cwd=engine_root,
        )
        log.append(reopened.stdout)
        opened = True
        restart_setup = _parse_raw_json(
            _cli(
                wrapper,
                session,
                ["run-code", "--filename", str(HARNESS_ROOT / "restart_setup.js")],
                cwd=engine_root,
                raw=True,
            )
        )
        trace_started = _cli(wrapper, session, ["tracing-start"], cwd=engine_root)
        log.append(trace_started.stdout)
        tracing = True
        flow_evidence["browser_restart_flow.js"] = _parse_raw_json(
            _cli(
                wrapper,
                session,
                ["run-code", "--filename", str(HARNESS_ROOT / "browser_restart_flow.js")],
                cwd=engine_root,
                raw=True,
            )
        )
        trace_stopped = _cli(wrapper, session, ["tracing-stop"], cwd=engine_root)
        log.append(trace_stopped.stdout)
        tracing = False
        restart_snapshot = _cli(wrapper, session, ["snapshot"], cwd=engine_root)
        log.append(restart_snapshot.stdout)
        _archive, trace_count = _archive_trace(engine_root)
        result.update(
            {
                "status": "passed",
                "setup": setup,
                "restart_setup": restart_setup,
                "flows": flow_evidence,
                "trace_count": trace_count,
            }
        )
    except GateFailure as error:
        result["failure_class"] = (
            "timeout" if "timed out" in str(error) else "browser_or_harness_failure"
        )
        log.append(str(error))
    finally:
        if tracing:
            try:
                stopped = _cli(
                    wrapper,
                    session,
                    ["tracing-stop"],
                    cwd=engine_root,
                    timeout=30,
                )
                log.append(stopped.stdout or stopped.stderr)
            except GateFailure as error:
                log.append(str(error))
        if opened:
            try:
                closed = _cli(
                    wrapper,
                    session,
                    ["close"],
                    cwd=engine_root,
                    timeout=30,
                )
                log.append(closed.stdout or closed.stderr)
            except GateFailure as error:
                log.append(str(error))
        (engine_root / "cli.log").write_text("\n".join(log), encoding="utf-8")
    return result


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--engine",
        action="append",
        choices=tuple(ENGINE_MAP),
        dest="engines",
        help="Run one engine; repeat for multiple engines. Defaults to all three.",
    )
    parser.add_argument(
        "--skip-build",
        action="store_true",
        help="Use the existing web/dist bundle. Intended only for harness iteration.",
    )
    parser.add_argument(
        "--playwright-wrapper",
        type=Path,
        default=DEFAULT_PLAYWRIGHT_WRAPPER,
        help="Path to the repository-external Playwright CLI wrapper.",
    )
    return parser.parse_args()


def main() -> int:
    args = _arguments()
    engines = args.engines or list(ENGINE_MAP)
    attestation_eligible = args.engines is None and not args.skip_build
    if shutil.which("npx") is None:
        print("npx is required by the Playwright CLI wrapper", file=sys.stderr)
        return 2
    wrapper = args.playwright_wrapper.expanduser().resolve()
    if not wrapper.is_file():
        print(f"Playwright CLI wrapper was not found: {wrapper}", file=sys.stderr)
        return 2
    if not args.skip_build:
        print("[phase66] building the exact production seller bundle", flush=True)
        _run(["npm", "run", "build"], cwd=REPOSITORY_ROOT / "web", timeout=300)
    bundle_digest, bundle_file_count = _bundle_authority()

    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    artifact_root = REPOSITORY_ROOT / "output" / "playwright" / "phase66" / stamp
    artifact_root.mkdir(parents=True, exist_ok=False)
    server = create_server()
    server_thread = threading.Thread(
        target=server.serve_forever, name="phase66-fixture", daemon=True
    )
    server_thread.start()
    bootstrap_url = f"http://127.0.0.1:{server.server_port}/__fixture__/health"
    results: list[dict[str, Any]] = []
    try:
        for engine in engines:
            print(f"[phase66] running {engine}", flush=True)
            evidence = _run_engine(
                engine,
                ENGINE_MAP[engine],
                wrapper,
                bootstrap_url,
                artifact_root,
            )
            results.append(evidence)
            print(f"[phase66] {engine}: {evidence['status']}", flush=True)
    finally:
        server.shutdown()
        server.server_close()
        server_thread.join(timeout=5)

    evidence = {
        "gate": ("offline.browser_matrix" if attestation_eligible else "iteration.browser_harness"),
        "attestation_eligible": attestation_eligible,
        "bundle_digest": bundle_digest,
        "bundle_file_count": bundle_file_count,
        "generated_at": datetime.now(UTC).isoformat(),
        "engines": results,
    }
    evidence_path = artifact_root / (
        "browser-gate.json" if attestation_eligible else "browser-iteration.json"
    )
    evidence_path.write_text(
        json.dumps(evidence, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(f"[phase66] evidence: {evidence_path}", flush=True)
    return 0 if all(item["status"] == "passed" for item in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
