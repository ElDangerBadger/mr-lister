from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from tools.build_latency_waterfall import (
    LatencyWaterfallError,
    assemble_latency_waterfall,
    canonical_json,
    parse_latency_jsonl,
    render_markdown,
)

SOURCE_COMMIT = "a" * 40
RELEASE_FINGERPRINT = "b" * 64


def _timestamp(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def _event(run: int, event_sequence: int, **values: object) -> dict[str, object]:
    return {
        "schema_version": "1.0.0",
        "run_id": f"{run:024x}",
        "event_id": f"{run:024x}{event_sequence:08x}",
        "outcome": "succeeded",
        **values,
    }


def _milestone(run: int, ordinal: int, name: str, at: datetime) -> dict[str, object]:
    return _event(
        run,
        ordinal,
        kind="milestone",
        name=name,
        outcome="observed",
        occurred_at=_timestamp(at),
    )


def _span(
    run: int,
    ordinal: int,
    name: str,
    start: datetime,
    end: datetime,
    **values: object,
) -> dict[str, object]:
    return _event(
        run,
        ordinal,
        kind="span",
        name=name,
        started_at=_timestamp(start),
        completed_at=_timestamp(end),
        duration_ms=(end - start).total_seconds() * 1_000,
        **values,
    )


def _provider(
    run: int,
    ordinal: int,
    start: datetime,
    end: datetime,
    *,
    method: str,
    route: str,
    purpose: str | None = None,
) -> dict[str, object]:
    event = _event(
        run,
        ordinal,
        kind="provider_request",
        name="printify_request",
        component="printify",
        started_at=_timestamp(start),
        completed_at=_timestamp(end),
        duration_ms=(end - start).total_seconds() * 1_000,
        provider="printify",
        method=method,
        route=route,
        ordinal=ordinal,
    )
    if purpose is not None:
        event["purpose"] = purpose
    return event


def _five_run_jsonl() -> str:
    base = datetime(2026, 9, 5, 20, 0, tzinfo=UTC)
    lines: list[str] = []
    for run in range(1, 6):
        origin = base + timedelta(minutes=run * 10)
        events = [
            _milestone(run, 1, "upload_accepted", origin),
            _milestone(run, 2, "strands_start", origin + timedelta(seconds=10)),
            _milestone(run, 3, "strands_end", origin + timedelta(seconds=60)),
            _milestone(run, 4, "editable_review_available", origin + timedelta(seconds=70)),
            _milestone(run, 5, "product_synchronized", origin + timedelta(seconds=100)),
            _milestone(run, 10, "upload_response_received", origin + timedelta(seconds=1)),
            _span(
                run,
                11,
                "artwork_normalization",
                origin - timedelta(seconds=2),
                origin - timedelta(seconds=1),
                component="seller_web",
            ),
            _span(
                run,
                6,
                "dispatch_wait",
                origin,
                origin + timedelta(seconds=60),
                component="orchestration",
            ),
            # Nested overlap must not increase coverage beyond the surrounding interval.
            _span(
                run,
                7,
                "model_invocation",
                origin + timedelta(seconds=20),
                origin + timedelta(seconds=80),
                component="model",
                attempt=1,
                cycle=1,
                model_id="google.gemma-3-27b-it",
            ),
            _provider(
                run,
                8,
                origin + timedelta(seconds=80),
                origin + timedelta(seconds=90),
                method="GET",
                route="/v1/shops.json",
                purpose="shop_identity",
            ),
            _provider(
                run,
                9,
                origin + timedelta(seconds=85),
                origin + timedelta(seconds=90),
                method="GET",
                route="/v1/shops/{shop_id}/products/{product_id}.json",
                purpose="draft_readback",
            ),
        ]
        for index, event in enumerate(events):
            raw = json.dumps(event, separators=(",", ":"))
            if run == 1 and index == 0:
                lines.append(raw)
            elif run == 1 and index == 1:
                lines.append(f"2026-09-05T20:00:00Z {raw}".replace(raw, f"latency_trace={raw}"))
            elif run == 1 and index == 2:
                lines.append(json.dumps({"message": f"latency_trace={raw}"}))
            else:
                lines.append(f"latency_trace={raw}")
    return "\n".join(lines)


def test_assembles_five_runs_and_uses_interval_union_for_coverage() -> None:
    events = parse_latency_jsonl(_five_run_jsonl())

    document = assemble_latency_waterfall(
        events,
        source_commit=SOURCE_COMMIT,
        release_fingerprint=RELEASE_FINGERPRINT,
    )

    assert document["sample"]["run_count"] == 5
    first = document["runs"][0]
    assert first["stage_durations_ms"]["strands_execution"] == 50_000
    assert first["stage_durations_ms"]["upload_to_editable_content"] == 70_000
    assert first["stage_durations_ms"]["upload_to_synchronized_draft"] == 100_000
    assert first["milestones"]["artwork_normalization_completed"] == ("2026-09-05T20:09:59.000000Z")
    assert first["coverage"]["upload_to_synchronized_draft"] == {
        "window_ms": 100_000.0,
        "covered_ms": 90_000.0,
        "unexplained_ms": 10_000.0,
        "coverage_percent": 90.0,
        "covered_interval_count": 1,
        "unexplained_gaps": [
            {
                "started_at": "2026-09-05T20:11:30.000000Z",
                "completed_at": "2026-09-05T20:11:40.000000Z",
                "duration_ms": 10_000.0,
            }
        ],
    }
    assert first["counts"] == {
        "model_invocations": 1,
        "printify_requests": 2,
        "strands_cycles": 1,
    }
    assert (
        document["summary"]["stage_durations_ms"]["upload_to_synchronized_draft"]["median"]
        == 100_000
    )
    assert document["summary"]["printify_request_count"] == 10


def test_provider_call_map_classifies_and_summarizes_each_request_purpose() -> None:
    document = assemble_latency_waterfall(
        parse_latency_jsonl(_five_run_jsonl()),
        source_commit=SOURCE_COMMIT,
        release_fingerprint=RELEASE_FINGERPRINT,
    )

    call_map = document["summary"]["printify_call_map"]
    assert [(item["purpose"], item["request_count"]) for item in call_map] == [
        ("draft_readback", 5),
        ("shop_identity", 5),
    ]
    assert all(item["duration_ms"]["samples"] == 5 for item in call_map)
    assert [item["purpose"] for item in document["runs"][0]["printify_requests"]] == [
        "shop_identity",
        "draft_readback",
    ]


def test_canonical_json_and_markdown_are_deterministic_and_include_scorecard() -> None:
    document = assemble_latency_waterfall(
        parse_latency_jsonl(_five_run_jsonl()),
        source_commit=SOURCE_COMMIT,
        release_fingerprint=RELEASE_FINGERPRINT,
    )

    first_json = canonical_json(document)
    assert first_json == canonical_json(document)
    assert first_json.endswith("\n")
    assert json.loads(first_json)["format"] == "mr-lister-latency-waterfall-v1"

    markdown = render_markdown(document)
    assert "# Latency Waterfall v1" in markdown
    assert "| Upload → editable content | 70.000 s | 70.000 s | ≤30 s stretch |" in markdown
    assert "| Printify requests | 2 | 2 | ≤6 |" in markdown
    assert "overlapping and nested spans are never added twice" in markdown


def test_ambiguous_product_get_requires_explicit_purpose() -> None:
    origin = datetime(2026, 9, 5, 20, 0, tzinfo=UTC)
    invalid = _provider(
        1,
        1,
        origin,
        origin + timedelta(seconds=1),
        method="GET",
        route="/v1/shops/{shop_id}/products/{product_id}.json",
    )

    with pytest.raises(LatencyWaterfallError, match="outside the sanitized v1 contract"):
        parse_latency_jsonl(json.dumps(invalid))


def test_product_collection_read_is_classified_as_reconciliation() -> None:
    origin = datetime(2026, 9, 5, 20, 0, tzinfo=UTC)
    event = _provider(
        1,
        1,
        origin,
        origin + timedelta(seconds=1),
        method="GET",
        route="/v1/shops/{shop_id}/products.json",
        purpose="product_reconciliation",
    )

    [parsed] = parse_latency_jsonl(json.dumps(event))
    assert parsed.canonical_purpose == "product_reconciliation"


@pytest.mark.parametrize(
    "mutation",
    [
        lambda payload: payload.update({"job_id": "job_secret"}),
        lambda payload: payload.update({"duration_ms": float("nan")}),
        lambda payload: payload.update({"route": "/v1/shops/123/products/secret.json"}),
        lambda payload: payload.update({"model_id": "unsafe model identity"}),
    ],
)
def test_parser_rejects_unsanitized_or_nonfinite_provider_records(mutation) -> None:
    origin = datetime(2026, 9, 5, 20, 0, tzinfo=UTC)
    payload = _provider(
        1,
        1,
        origin,
        origin + timedelta(seconds=1),
        method="GET",
        route="/v1/shops.json",
        purpose="shop_identity",
    )
    mutation(payload)

    with pytest.raises(LatencyWaterfallError, match="outside the sanitized v1 contract"):
        parse_latency_jsonl(json.dumps(payload))


def test_assembler_fails_closed_on_incomplete_sample_or_duplicate_milestone() -> None:
    events = list(parse_latency_jsonl(_five_run_jsonl()))
    duplicate = events[0].model_copy(update={"event_id": "f" * 32})

    replayed = assemble_latency_waterfall(
        [*events, duplicate],
        source_commit=SOURCE_COMMIT,
        release_fingerprint=RELEASE_FINGERPRINT,
    )
    assert replayed["sample"]["run_count"] == 5

    conflicting = duplicate.model_copy(
        update={
            "event_id": "e" * 32,
            "occurred_at": duplicate.occurred_at + timedelta(milliseconds=1),
        }
    )
    with pytest.raises(LatencyWaterfallError, match="repeats milestone upload_accepted"):
        assemble_latency_waterfall(
            [*events, conflicting],
            source_commit=SOURCE_COMMIT,
            release_fingerprint=RELEASE_FINGERPRINT,
        )

    with pytest.raises(LatencyWaterfallError, match="requires 5 distinct runs"):
        assemble_latency_waterfall(
            [event for event in events if event.run_id != f"{5:024x}"],
            source_commit=SOURCE_COMMIT,
            release_fingerprint=RELEASE_FINGERPRINT,
        )
