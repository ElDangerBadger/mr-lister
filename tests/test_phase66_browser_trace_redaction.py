from __future__ import annotations

from tools.phase66_browser import run_gate


def test_trace_redaction_removes_local_paths_and_browser_authority() -> None:
    repository_path = str(run_gate.REPOSITORY_ROOT).encode("utf-8")
    home_path = str(run_gate.Path.home()).encode("utf-8")
    payload = b"\n".join(
        (
            b"repository=" + repository_path + b"/web/dist/index.html",
            b"home=" + home_path + b"/.codex/session",
            b"Authorization: Bearer phase66-access-token",
            b"refresh_token=phase66-refresh-token",
            b"safe=browser-flow-passed",
        )
    )

    redacted = run_gate._redact_trace_bytes(payload)

    assert repository_path not in redacted
    assert home_path not in redacted
    assert b"phase66-access-token" not in redacted
    assert b"phase66-refresh-token" not in redacted
    assert b"authorization" not in redacted.lower()
    assert b"bearer" not in redacted.lower()
    assert b"safe=browser-flow-passed" in redacted
    assert redacted.count(run_gate._REDACTED_PATH) == 2
