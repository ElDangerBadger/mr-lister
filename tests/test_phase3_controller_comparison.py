from __future__ import annotations

import json

import pytest

from tools.phase3_controller_comparison import (
    _require_live_gate,
    _sanitized_failure,
    _write_artifact,
)


def test_live_gate_requires_explicit_flag_and_developer_profile(monkeypatch) -> None:
    monkeypatch.delenv("MR_LISTER_RUN_LIVE_BEDROCK", raising=False)
    monkeypatch.setenv("AWS_PROFILE", "mr-lister-dev")
    with pytest.raises(RuntimeError, match="MR_LISTER_RUN_LIVE_BEDROCK"):
        _require_live_gate("safe-run")

    monkeypatch.setenv("MR_LISTER_RUN_LIVE_BEDROCK", "1")
    monkeypatch.setenv("AWS_PROFILE", "root")
    with pytest.raises(RuntimeError, match="AWS_PROFILE"):
        _require_live_gate("safe-run")


def test_failure_artifact_does_not_serialize_provider_message() -> None:
    failure = _sanitized_failure(
        model_name="gemma",
        case_id="routine",
        error=RuntimeError("SECRET provider detail"),
    )

    assert "SECRET" not in json.dumps(failure)
    assert failure["error"]["type"] == "RuntimeError"


def test_private_artifact_is_create_only(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    destination = _write_artifact("safe-run", {"passed": True})

    assert json.loads(destination.read_text(encoding="utf-8")) == {"passed": True}
    with pytest.raises(FileExistsError):
        _write_artifact("safe-run", {"passed": False})
