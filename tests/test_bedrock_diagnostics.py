from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from hashlib import sha256

import pytest

from mr_lister.intelligence.diagnostics import (
    REDACTED,
    REDACTED_BINARY,
    BedrockDiagnosticRecord,
    CompositeDiagnosticSink,
    FilesystemDiagnosticSink,
    InMemoryDiagnosticSink,
    NoOpDiagnosticSink,
    redact_sensitive_data,
)


def diagnostic_record() -> BedrockDiagnosticRecord:
    return BedrockDiagnosticRecord(
        diagnostic_id="diagnostic-001",
        occurred_at=datetime(2026, 8, 18, 18, 30, tzinfo=UTC),
        operation="inspect_artwork",
        model_id="us.anthropic.claude-sonnet-4-6",
        status="success",
        prompt_version="phase2-v1",
        latency_ms=321.5,
        request_id="request-abc",
        job_id="job-123",
        artwork_sha256="a" * 64,
        usage={"inputTokens": 101, "outputTokens": 52, "totalTokens": 153},
        metadata={
            "attempt": 1,
            "nested": {
                "Authorization": "Bearer not-for-disk",
                "api_key": "not-for-disk",
                "prompt": "private prompt",
                "artwork": "base64 artwork",
                "opaque_blob": b"artwork bytes",
            },
            "image_dimensions": {"width": 1200, "height": 1200},
        },
        raw_model_output='{"subject":"private result"}',
    )


def test_record_defaults_to_redacted_metadata_and_no_raw_output() -> None:
    payload = diagnostic_record().to_payload()

    assert payload["request_id"] == "request-abc"
    assert payload["prompt_version"] == "phase2-v1"
    assert payload["attempt"] == 1
    assert payload["artwork_sha256"] == "a" * 64
    assert payload["response_sha256"] == sha256(b'{"subject":"private result"}').hexdigest()
    assert payload["latency_ms"] == 321.5
    assert payload["usage"] == {"inputTokens": 101, "outputTokens": 52, "totalTokens": 153}
    assert payload["metadata"] == {
        "attempt": 1,
        "nested": {
            "Authorization": REDACTED,
            "api_key": REDACTED,
            "prompt": REDACTED,
            "artwork": REDACTED,
            "opaque_blob": REDACTED_BINARY,
        },
        "image_dimensions": {"width": 1200, "height": 1200},
    }
    assert "raw_model_output" not in payload


def test_binary_data_is_redacted_without_relying_on_its_key_name() -> None:
    assert redact_sensitive_data({"innocent_name": memoryview(b"image")}) == {
        "innocent_name": REDACTED_BINARY
    }


def test_json_is_deterministic_and_raw_output_requires_explicit_opt_in() -> None:
    record = diagnostic_record()

    assert "private result" not in repr(record)
    assert "private prompt" not in repr(record)
    assert "not-for-disk" not in repr(record)
    assert record.to_json() == record.to_json()
    default_document = json.loads(record.to_json())
    private_document = json.loads(record.to_json(include_raw_output=True))

    assert "raw_model_output" not in default_document
    assert private_document["raw_model_output"] == '{"subject":"private result"}'


def test_provider_error_message_is_never_serialized_or_exposed_by_repr() -> None:
    record = BedrockDiagnosticRecord(
        operation="draft_listing",
        model_id="model",
        status="error",
        prompt_version="phase2-v1",
        error_type="AccessDeniedException",
        error_code="BEDROCK_ACCESS_DENIED",
        error_message="Authorization: Bearer secret; echoed private prompt",
    )

    assert record.to_payload()["error_message"] == REDACTED
    assert record.to_payload()["error_code"] == "BEDROCK_ACCESS_DENIED"
    assert "secret" not in repr(record)


def test_in_memory_sink_controls_raw_output_retention() -> None:
    record = diagnostic_record()
    safe_sink = InMemoryDiagnosticSink()
    private_sink = InMemoryDiagnosticSink(include_raw_output=True)

    safe_sink.emit(record)
    private_sink.write(record)

    assert "raw_model_output" not in safe_sink.records[0]
    assert private_sink.records[0]["raw_model_output"] == '{"subject":"private result"}'


def test_no_op_sink_accepts_records() -> None:
    sink = NoOpDiagnosticSink()

    assert sink.emit(diagnostic_record()) is None
    assert sink.write(diagnostic_record()) is None


def test_composite_sink_fans_out_without_weakening_each_sink_policy() -> None:
    safe_sink = InMemoryDiagnosticSink()
    private_sink = InMemoryDiagnosticSink(include_raw_output=True)
    composite = CompositeDiagnosticSink(safe_sink, private_sink)

    composite.emit(diagnostic_record())

    assert "raw_model_output" not in safe_sink.records[0]
    assert private_sink.records[0]["raw_model_output"] == '{"subject":"private result"}'


def test_composite_sink_requires_a_destination() -> None:
    with pytest.raises(ValueError, match="at least one sink"):
        CompositeDiagnosticSink()


def test_filesystem_sink_writes_private_redacted_json(tmp_path) -> None:
    diagnostic_directory = tmp_path / "private" / "bedrock"
    sink = FilesystemDiagnosticSink(diagnostic_directory)

    sink.emit(diagnostic_record())

    destination = diagnostic_directory / "diagnostic-001.json"
    document = json.loads(destination.read_text(encoding="utf-8"))
    assert document["metadata"]["nested"]["Authorization"] == REDACTED
    assert document["metadata"]["nested"]["opaque_blob"] == REDACTED_BINARY
    assert "raw_model_output" not in document
    if os.name == "posix":
        assert diagnostic_directory.stat().st_mode & 0o777 == 0o700
        assert destination.stat().st_mode & 0o777 == 0o600


def test_filesystem_sink_raw_output_is_an_explicit_private_setting(tmp_path) -> None:
    sink = FilesystemDiagnosticSink(tmp_path / "bedrock", include_raw_output=True)

    sink.write(diagnostic_record())

    document = json.loads((tmp_path / "bedrock" / "diagnostic-001.json").read_text())
    assert document["raw_model_output"] == '{"subject":"private result"}'


def test_filesystem_sink_never_overwrites_an_existing_diagnostic(tmp_path) -> None:
    sink = FilesystemDiagnosticSink(tmp_path / "bedrock")
    sink.emit(diagnostic_record())

    with pytest.raises(FileExistsError):
        sink.emit(diagnostic_record())


def test_record_rejects_naive_time_and_negative_latency() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        BedrockDiagnosticRecord(
            operation="draft_listing",
            model_id="model",
            status="success",
            prompt_version="phase2-v1",
            occurred_at=datetime(2026, 8, 18),
        )
    with pytest.raises(ValueError, match="negative"):
        BedrockDiagnosticRecord(
            operation="draft_listing",
            model_id="model",
            status="success",
            prompt_version="phase2-v1",
            latency_ms=-0.1,
        )
