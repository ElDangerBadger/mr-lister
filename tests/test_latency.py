from __future__ import annotations

from datetime import UTC, datetime
from io import StringIO

import pytest
from pydantic import ValidationError

from mr_lister.latency import (
    InMemoryLatencyTraceSink,
    LoggingLatencyTraceSink,
    bind_latency_run,
    current_latency_run_id,
    emit_latency_milestone,
    latency_run_id,
    latency_span,
    parse_latency_trace_line,
)

JOB_ID = "job_126b45d46bb560e8641a6e43f2a925d6"


def test_run_digest_is_stable_and_does_not_expose_the_job() -> None:
    first = latency_run_id(JOB_ID)

    assert first == latency_run_id(JOB_ID)
    assert latency_run_id("job_example") == "d589007d5d7b454348fb3b5f"
    assert len(first) == 24
    assert JOB_ID not in first
    with pytest.raises(ValueError, match="identity"):
        latency_run_id("")


def test_bound_run_is_nested_and_restored() -> None:
    assert current_latency_run_id() is None
    with bind_latency_run(JOB_ID) as run_id:
        assert current_latency_run_id() == run_id
        with bind_latency_run("job_another") as nested:
            assert current_latency_run_id() == nested
        assert current_latency_run_id() == run_id
    assert current_latency_run_id() is None


def test_milestone_and_span_are_sanitized_and_parseable() -> None:
    sink = InMemoryLatencyTraceSink()
    occurred_at = datetime(2026, 9, 5, 12, 0, tzinfo=UTC)

    with bind_latency_run(JOB_ID):
        emit_latency_milestone(
            "upload_accepted",
            component="phase6_upload",
            sink=sink,
            occurred_at=occurred_at,
        )
        with latency_span(
            "printify_request",
            component="printify_draft",
            sink=sink,
            kind="provider_request",
            provider="printify",
            method="GET",
            route="/v1/shops/{shop_id}/products/{product_id}.json",
            purpose="draft_readback",
        ):
            pass

    milestone, request = sink.events
    assert milestone.name == "upload_accepted"
    assert milestone.occurred_at == occurred_at
    assert request.kind == "provider_request"
    assert request.duration_ms is not None
    serialized = request.model_dump_json()
    assert JOB_ID not in serialized
    assert "owner" not in serialized
    assert "Authorization" not in serialized

    stream = StringIO()
    LoggingLatencyTraceSink(stream).write(request)
    decoded = parse_latency_trace_line(stream.getvalue())
    assert decoded == request


def test_span_preserves_observed_exception_when_logging_succeeds() -> None:
    sink = InMemoryLatencyTraceSink()

    with pytest.raises(RuntimeError, match="original"):
        with (
            bind_latency_run(JOB_ID),
            latency_span(
                "model_invocation",
                component="bedrock_intelligence",
                sink=sink,
                attempt=1,
                model_id="google.gemma-3-27b-it",
            ),
        ):
            raise RuntimeError("original")

    assert sink.events[0].outcome == "failed"


def test_logging_failure_never_changes_operation_result() -> None:
    class BrokenSink:
        def write(self, _event: object) -> None:
            raise RuntimeError("logging broke")

    with bind_latency_run(JOB_ID):
        with latency_span("economics_calculation", component="phase6_economics", sink=BrokenSink()):
            result = 42
        emit_latency_milestone(
            "editable_review_available",
            component="phase6_review",
            sink=BrokenSink(),
        )

    assert result == 42


def test_event_schema_rejects_actual_provider_identifiers() -> None:
    sink = InMemoryLatencyTraceSink()

    with bind_latency_run(JOB_ID):
        with latency_span(
            "printify_request",
            component="printify_draft",
            sink=sink,
            kind="provider_request",
            provider="printify",
            method="GET",
            route="/v1/shops/123/products/actual-product.json",
            purpose="draft_readback",
        ):
            pass

    # The invalid event is discarded rather than leaking an identifier or failing work.
    assert sink.events == []

    with pytest.raises(ValidationError):
        parse_latency_trace_line(
            'latency_trace={"schema_version":"1.0.0","event_id":"'
            + "a" * 32
            + '","run_id":"'
            + "b" * 24
            + '","kind":"milestone","name":"upload_accepted",'
            '"component":"phase6_upload","outcome":"succeeded",'
            '"occurred_at":"2026-09-05T12:00:00Z"}'
        )
