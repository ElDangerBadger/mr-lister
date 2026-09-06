"""Assemble sanitized Stage 0 latency traces into one deterministic waterfall report.

The tool is intentionally offline.  It accepts only the narrow ``latency_trace`` contract,
performs no AWS or provider calls, and writes no files unless explicit output paths are supplied.
Raw owner, job, request, response, artwork, prompt, and credential material is outside the input
contract and therefore rejected by Pydantic's closed model.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import statistics
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

TRACE_PREFIX = "latency_trace="
TRACE_SCHEMA_VERSION = "1.0.0"
WATERFALL_FORMAT = "mr-lister-latency-waterfall-v1"

Digest = Annotated[str, StringConstraints(pattern=r"^[a-f0-9]{24,64}$")]
SafeName = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_]{0,95}$")]
SafeModelId = Annotated[
    str,
    StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}$"),
]

_MILESTONE_ALIASES = {
    "approval": "approval_recorded",
    "editable_review_availability": "editable_review_available",
    "etsy_visibility_confirmation": "etsy_visibility_confirmed",
    "product_synchronized": "synchronized_draft",
    "provider_publication_confirmation": "provider_publication_confirmed",
    "provider_publication_submission": "provider_publication_submitted",
    "publication_request": "publication_requested",
    "strands_end": "strands_completed",
    "strands_start": "strands_started",
    "synchronized_product_evidence": "synchronized_draft",
}

_REQUIRED_MILESTONES = frozenset(
    {"upload_accepted", "editable_review_available", "synchronized_draft"}
)
_OPTIONAL_MILESTONES = frozenset(
    {
        "approval_recorded",
        "artwork_normalization_completed",
        "economics_completed",
        "etsy_visibility_confirmed",
        "provider_publication_confirmed",
        "provider_publication_submitted",
        "publication_requested",
        "strands_completed",
        "strands_started",
        "upload_response_received",
    }
)
_KNOWN_MILESTONES = _REQUIRED_MILESTONES | _OPTIONAL_MILESTONES

_PRODUCT_ROUTE = "/v1/shops/{shop_id}/products/{product_id}.json"
_ROUTE_PURPOSES: dict[tuple[str, str], frozenset[str]] = {
    ("GET", "/v1/shops.json"): frozenset({"shop_identity", "etsy_shop_preflight"}),
    ("GET", "/v1/catalog/blueprints.json"): frozenset({"blueprint_catalog"}),
    (
        "GET",
        "/v1/catalog/blueprints/{blueprint_id}/print_providers.json",
    ): frozenset({"print_provider_catalog"}),
    (
        "GET",
        "/v1/catalog/blueprints/{blueprint_id}/print_providers/{print_provider_id}/variants.json",
    ): frozenset({"variant_catalog"}),
    ("POST", "/v1/uploads/images.json"): frozenset({"artwork_upload"}),
    ("GET", "/v1/uploads/{image_id}.json"): frozenset({"artwork_upload_readback"}),
    ("GET", "/v1/uploads.json"): frozenset({"artwork_upload_reconciliation"}),
    ("GET", "/v1/shops/{shop_id}/products.json"): frozenset({"product_reconciliation"}),
    ("POST", "/v1/shops/{shop_id}/products.json"): frozenset({"draft_create"}),
    ("PUT", _PRODUCT_ROUTE): frozenset({"draft_update"}),
    (
        "GET",
        _PRODUCT_ROUTE,
    ): frozenset(
        {
            "draft_readback",
            "positive_verification",
            "product_cost_readback",
            "product_preflight",
            "publication_reconciliation",
        }
    ),
    (
        "GET",
        "/v2/catalog/blueprints/{blueprint_id}/print_providers/"
        "{print_provider_id}/shipping/standard.json",
    ): frozenset({"standard_shipping"}),
    (
        "POST",
        "/v1/shops/{shop_id}/products/{product_id}/publish.json",
    ): frozenset({"one_shot_connected_channel_publication"}),
}

_STAGE_PAIRS = {
    "upload_to_strands_start": ("upload_accepted", "strands_started"),
    "strands_execution": ("strands_started", "strands_completed"),
    "strands_to_editable_review": ("strands_completed", "editable_review_available"),
    "strands_to_synchronized_draft": ("strands_completed", "synchronized_draft"),
    "upload_to_editable_content": ("upload_accepted", "editable_review_available"),
    "upload_to_synchronized_draft": ("upload_accepted", "synchronized_draft"),
    "synchronized_draft_to_economics": ("synchronized_draft", "economics_completed"),
    "approval_to_provider_submission": ("approval_recorded", "provider_publication_submitted"),
    "publication_request_to_provider_submission": (
        "publication_requested",
        "provider_publication_submitted",
    ),
    "provider_submission_to_confirmation": (
        "provider_publication_submitted",
        "provider_publication_confirmed",
    ),
    "provider_confirmation_to_etsy_visibility": (
        "provider_publication_confirmed",
        "etsy_visibility_confirmed",
    ),
}


class LatencyWaterfallError(RuntimeError):
    """One sanitized trace or report invariant was not satisfied."""


class LatencyTraceEvent(BaseModel):
    """Closed, identifier-safe trace record emitted by Stage 0 instrumentation."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["1.0.0"]
    run_id: Digest
    event_id: Digest
    kind: Literal["milestone", "span", "provider_request"]
    name: SafeName
    component: SafeName | None = None
    occurred_at: datetime | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    duration_ms: float | None = Field(default=None, ge=0, le=86_400_000)
    outcome: Literal["observed", "succeeded", "failed"]
    attempt: int | None = Field(default=None, ge=1, le=100)
    cycle: int | None = Field(default=None, ge=1, le=100)
    model_id: SafeModelId | None = None
    provider: Literal["printify"] | None = None
    method: Literal["GET", "POST", "PUT"] | None = None
    route: str | None = Field(default=None, min_length=1, max_length=255)
    purpose: SafeName | None = None
    ordinal: int | None = Field(default=None, ge=1, le=1_000)

    @model_validator(mode="after")
    def shape_matches_kind(self) -> LatencyTraceEvent:
        times = (self.occurred_at, self.started_at, self.completed_at)
        if any(value is not None and value.utcoffset() != UTC.utcoffset(value) for value in times):
            raise ValueError("latency timestamps must be UTC")
        if self.kind == "milestone":
            if (
                self.occurred_at is None
                or self.started_at is not None
                or self.completed_at is not None
                or self.duration_ms is not None
            ):
                raise ValueError("a milestone requires only occurred_at")
            normalized = _canonical_milestone_name(self.name)
            if normalized not in _KNOWN_MILESTONES:
                raise ValueError("milestone name is outside Latency Waterfall v1")
            if self.outcome != "observed":
                raise ValueError("a milestone outcome must be observed")
        else:
            if (
                self.occurred_at is not None
                or self.started_at is None
                or self.completed_at is None
                or self.duration_ms is None
            ):
                raise ValueError("a timed trace requires start, completion, and duration")
            if self.completed_at < self.started_at:
                raise ValueError("a timed trace cannot complete before it starts")
            if not math.isfinite(self.duration_ms):
                raise ValueError("trace duration must be finite")
            if self.outcome == "observed":
                raise ValueError("a timed trace must succeed or fail")

        provider_fields = (self.provider, self.method, self.route)
        if self.kind == "provider_request":
            if not all(value is not None for value in provider_fields):
                raise ValueError("a provider request requires provider, method, and route")
            assert self.method is not None and self.route is not None
            _classify_provider_purpose(
                method=self.method,
                route=self.route,
                supplied=self.purpose,
            )
        elif any(value is not None for value in (*provider_fields, self.purpose, self.ordinal)):
            raise ValueError("only provider requests may carry provider request fields")
        return self

    @property
    def canonical_name(self) -> str:
        return _canonical_milestone_name(self.name) if self.kind == "milestone" else self.name

    @property
    def canonical_purpose(self) -> str | None:
        if self.kind != "provider_request":
            return None
        assert self.method is not None and self.route is not None
        return _classify_provider_purpose(
            method=self.method,
            route=self.route,
            supplied=self.purpose,
        )


def _canonical_milestone_name(name: str) -> str:
    return _MILESTONE_ALIASES.get(name, name)


def _classify_provider_purpose(*, method: str, route: str, supplied: str | None) -> str:
    allowed = _ROUTE_PURPOSES.get((method, route))
    if allowed is None:
        raise ValueError("provider route is not a sanitized supported template")
    if supplied is None:
        if len(allowed) != 1:
            raise ValueError("ambiguous provider route requires an explicit purpose")
        return next(iter(allowed))
    if supplied not in allowed:
        raise ValueError("provider purpose does not match method and route")
    return supplied


def _reject_json_constant(_value: str) -> None:
    raise ValueError("non-finite JSON constant")


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON member")
        result[key] = value
    return result


def _decode_json(text: str) -> Any:
    return json.loads(
        text,
        parse_constant=_reject_json_constant,
        object_pairs_hook=_unique_json_object,
    )


def parse_latency_jsonl(contents: str) -> tuple[LatencyTraceEvent, ...]:
    """Parse raw JSONL, ``latency_trace=`` log lines, or CloudWatch message envelopes."""

    events: list[LatencyTraceEvent] = []
    for line_number, raw_line in enumerate(contents.splitlines(), start=1):
        line = raw_line.strip()
        if not line:
            continue
        try:
            payload = _decode_trace_line(line)
            events.append(LatencyTraceEvent.model_validate(payload))
        except Exception:
            raise LatencyWaterfallError(
                f"Latency trace line {line_number} is outside the sanitized v1 contract"
            ) from None
    if not events:
        raise LatencyWaterfallError("Latency trace input is empty")
    return tuple(events)


def _decode_trace_line(line: str) -> Any:
    if line.startswith("{"):
        payload = _decode_json(line)
        if isinstance(payload, Mapping) and set(payload) == {"message"}:
            message = payload["message"]
            if not isinstance(message, str) or TRACE_PREFIX not in message:
                raise ValueError
            return _decode_json(message.split(TRACE_PREFIX, 1)[1].strip())
        return payload
    if TRACE_PREFIX in line:
        return _decode_json(line.split(TRACE_PREFIX, 1)[1].strip())
    raise ValueError


def assemble_latency_waterfall(
    events: Sequence[LatencyTraceEvent],
    *,
    source_commit: str,
    release_fingerprint: str,
    expected_runs: int = 5,
) -> dict[str, Any]:
    """Validate and group one complete Stage 0 sample without double-counting overlap."""

    if re.fullmatch(r"[0-9a-f]{40}", source_commit) is None:
        raise LatencyWaterfallError("source commit must be a full lowercase Git commit")
    if re.fullmatch(r"[0-9a-f]{64}", release_fingerprint) is None:
        raise LatencyWaterfallError("release fingerprint must be a lowercase SHA-256 digest")
    if expected_runs < 1 or expected_runs > 100:
        raise LatencyWaterfallError("expected run count is outside the supported bound")
    if len({event.event_id for event in events}) != len(events):
        raise LatencyWaterfallError("latency trace event IDs must be globally unique")

    by_run: dict[str, list[LatencyTraceEvent]] = defaultdict(list)
    for event in events:
        by_run[event.run_id].append(event)
    if len(by_run) != expected_runs:
        raise LatencyWaterfallError(
            f"Latency Waterfall v1 requires {expected_runs} distinct runs; observed {len(by_run)}"
        )

    runs = [_assemble_run(run_id, by_run[run_id]) for run_id in sorted(by_run)]
    summary = _summarize(runs)
    observed_times = [
        _parse_output_time(value)
        for run in runs
        for value in run["milestones"].values()
        if value is not None
    ]
    return {
        "format": WATERFALL_FORMAT,
        "schema_version": TRACE_SCHEMA_VERSION,
        "source_commit": source_commit,
        "release_fingerprint": release_fingerprint,
        "sample": {
            "run_count": len(runs),
            "window_started_at": _timestamp(min(observed_times)),
            "window_completed_at": _timestamp(max(observed_times)),
        },
        "runs": runs,
        "summary": summary,
    }


def _assemble_run(run_id: str, events: Sequence[LatencyTraceEvent]) -> dict[str, Any]:
    milestones: dict[str, datetime] = {}
    timed: list[LatencyTraceEvent] = []
    for event in events:
        if event.kind == "milestone":
            name = event.canonical_name
            if name in milestones:
                if milestones[name] == event.occurred_at:
                    # An idempotent HTTP replay can mirror the same durable milestone again.
                    # Coalesce only an identical timestamp; conflicting evidence remains invalid.
                    continue
                raise LatencyWaterfallError(f"run {run_id} repeats milestone {name}")
            assert event.occurred_at is not None
            milestones[name] = event.occurred_at
        else:
            timed.append(event)
    if "artwork_normalization_completed" not in milestones:
        normalization_completions = [
            event.completed_at
            for event in timed
            if event.name == "artwork_normalization"
            and event.outcome == "succeeded"
            and event.completed_at is not None
        ]
        if normalization_completions:
            milestones["artwork_normalization_completed"] = min(normalization_completions)
    missing_required = sorted(_REQUIRED_MILESTONES - milestones.keys())
    if missing_required:
        raise LatencyWaterfallError(
            f"run {run_id} is missing required milestones: {', '.join(missing_required)}"
        )

    stage_durations: dict[str, float | None] = {}
    for stage, (start_name, end_name) in _STAGE_PAIRS.items():
        start = milestones.get(start_name)
        end = milestones.get(end_name)
        if start is None or end is None:
            stage_durations[stage] = None
            continue
        duration = _milliseconds(end - start)
        if duration < 0:
            raise LatencyWaterfallError(f"run {run_id} has reversed stage {stage}")
        stage_durations[stage] = duration

    timed.sort(key=lambda item: (item.started_at, item.completed_at, item.event_id))
    provider = sorted(
        (event for event in timed if event.kind == "provider_request"),
        key=lambda item: (
            item.started_at,
            item.ordinal if item.ordinal is not None else 1_001,
            item.completed_at,
            item.event_id,
        ),
    )
    supplied_ordinals = [event.ordinal for event in provider if event.ordinal is not None]
    if len(supplied_ordinals) != len(set(supplied_ordinals)):
        raise LatencyWaterfallError(f"run {run_id} repeats a provider request ordinal")

    intervals = [
        (event.started_at, event.completed_at)
        for event in timed
        if event.started_at is not None and event.completed_at is not None
    ]
    coverage = {
        "upload_to_editable_content": _coverage(
            milestones["upload_accepted"],
            milestones["editable_review_available"],
            intervals,
        ),
        "upload_to_synchronized_draft": _coverage(
            milestones["upload_accepted"],
            milestones["synchronized_draft"],
            intervals,
        ),
    }

    model_events = [event for event in timed if event.model_id is not None]
    explicit_cycles = [event for event in timed if event.name == "strands_cycle"]
    cycle_values = {event.cycle for event in timed if event.cycle is not None}
    cycle_count = len(explicit_cycles) if explicit_cycles else len(cycle_values)

    return {
        "run_id": run_id,
        "milestones": {
            name: _timestamp(milestones[name]) if name in milestones else None
            for name in sorted(_KNOWN_MILESTONES)
        },
        "missing_optional_milestones": sorted(_OPTIONAL_MILESTONES - milestones.keys()),
        "stage_durations_ms": stage_durations,
        "coverage": coverage,
        "counts": {
            "model_invocations": len(model_events),
            "printify_requests": len(provider),
            "strands_cycles": cycle_count,
        },
        "spans": [_render_span(event) for event in timed if event.kind == "span"],
        "printify_requests": [
            _render_provider_request(event, sequence=index)
            for index, event in enumerate(provider, start=1)
        ],
    }


def _render_span(event: LatencyTraceEvent) -> dict[str, Any]:
    assert event.started_at is not None and event.completed_at is not None
    return {
        "event_id": event.event_id,
        "name": event.name,
        "component": event.component,
        "started_at": _timestamp(event.started_at),
        "completed_at": _timestamp(event.completed_at),
        "duration_ms": _round(event.duration_ms),
        "outcome": event.outcome,
        "attempt": event.attempt,
        "cycle": event.cycle,
        "model_id": event.model_id,
    }


def _render_provider_request(event: LatencyTraceEvent, *, sequence: int) -> dict[str, Any]:
    assert event.started_at is not None and event.completed_at is not None
    assert event.method is not None and event.route is not None
    return {
        "event_id": event.event_id,
        "sequence": sequence,
        "reported_ordinal": event.ordinal,
        "component": event.component,
        "started_at": _timestamp(event.started_at),
        "completed_at": _timestamp(event.completed_at),
        "duration_ms": _round(event.duration_ms),
        "outcome": event.outcome,
        "method": event.method,
        "route": event.route,
        "purpose": event.canonical_purpose,
        "attempt": event.attempt,
    }


def _coverage(
    window_start: datetime,
    window_end: datetime,
    intervals: Iterable[tuple[datetime, datetime]],
) -> dict[str, Any]:
    if window_end < window_start:
        raise LatencyWaterfallError("coverage window is reversed")
    clipped = sorted(
        (max(start, window_start), min(end, window_end))
        for start, end in intervals
        if end > window_start and start < window_end and end > start
    )
    merged: list[tuple[datetime, datetime]] = []
    for start, end in clipped:
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    covered_ms = _round(sum(_milliseconds(end - start) for start, end in merged))
    total_ms = _milliseconds(window_end - window_start)
    unexplained_ms = _round(max(0.0, total_ms - covered_ms))
    gaps: list[dict[str, Any]] = []
    cursor = window_start
    for start, end in merged:
        if start > cursor:
            gaps.append(_gap(cursor, start))
        cursor = max(cursor, end)
    if cursor < window_end:
        gaps.append(_gap(cursor, window_end))
    coverage_percent = 100.0 if total_ms == 0 else _round(covered_ms / total_ms * 100, 2)
    return {
        "window_ms": _round(total_ms),
        "covered_ms": covered_ms,
        "unexplained_ms": unexplained_ms,
        "coverage_percent": coverage_percent,
        "covered_interval_count": len(merged),
        "unexplained_gaps": gaps,
    }


def _gap(start: datetime, end: datetime) -> dict[str, Any]:
    return {
        "started_at": _timestamp(start),
        "completed_at": _timestamp(end),
        "duration_ms": _round(_milliseconds(end - start)),
    }


def _summarize(runs: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    stage_samples: dict[str, list[float]] = defaultdict(list)
    coverage_samples: dict[str, list[float]] = defaultdict(list)
    count_samples: dict[str, list[float]] = defaultdict(list)
    call_counts: Counter[tuple[str, str, str]] = Counter()
    call_durations: dict[tuple[str, str, str], list[float]] = defaultdict(list)

    for run in runs:
        for stage, duration in run["stage_durations_ms"].items():
            if duration is not None:
                stage_samples[stage].append(duration)
        for window, details in run["coverage"].items():
            coverage_samples[window].append(details["coverage_percent"])
        for name, count in run["counts"].items():
            count_samples[name].append(float(count))
        for request in run["printify_requests"]:
            key = (request["purpose"], request["method"], request["route"])
            call_counts[key] += 1
            call_durations[key].append(request["duration_ms"])

    provider_map = []
    for key in sorted(call_counts):
        purpose, method, route = key
        provider_map.append(
            {
                "purpose": purpose,
                "method": method,
                "route": route,
                "request_count": call_counts[key],
                "duration_ms": _statistics(call_durations[key]),
            }
        )
    optional_observation_counts = {
        milestone: sum(run["milestones"][milestone] is not None for run in runs)
        for milestone in sorted(_OPTIONAL_MILESTONES)
    }
    return {
        "stage_durations_ms": {
            stage: _statistics(stage_samples[stage]) for stage in sorted(stage_samples)
        },
        "coverage_percent": {
            window: _statistics(coverage_samples[window]) for window in sorted(coverage_samples)
        },
        "counts_per_run": {
            name: _statistics(count_samples[name]) for name in sorted(count_samples)
        },
        "printify_call_map": provider_map,
        "printify_request_count": sum(call_counts.values()),
        "optional_milestone_observation_counts": optional_observation_counts,
    }


def _statistics(values: Sequence[float]) -> dict[str, float | int]:
    ordered = sorted(values)
    if not ordered:
        raise LatencyWaterfallError("cannot summarize an empty sample")
    rank = max(0, math.ceil(0.9 * len(ordered)) - 1)
    return {
        "samples": len(ordered),
        "minimum": _round(ordered[0]),
        "median": _round(statistics.median(ordered)),
        "p90_nearest_rank": _round(ordered[rank]),
        "maximum": _round(ordered[-1]),
    }


def canonical_json(document: Mapping[str, Any]) -> str:
    return json.dumps(document, allow_nan=False, indent=2, sort_keys=True) + "\n"


def render_markdown(document: Mapping[str, Any]) -> str:
    """Render the deterministic human review plus the program scorecard."""

    summary = document["summary"]
    runs = document["runs"]
    lines = [
        "# Latency Waterfall v1",
        "",
        f"Source commit: `{document['source_commit']}`  ",
        f"Release fingerprint: `{document['release_fingerprint']}`  ",
        f"Representative single-artwork runs: **{document['sample']['run_count']}**",
        "",
        "## Run waterfall",
        "",
        (
            "| Run | Upload → editable | Upload → synchronized draft | AI calls | "
            "Strands cycles | Printify requests | Sync coverage |"
        ),
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for run in runs:
        stages = run["stage_durations_ms"]
        counts = run["counts"]
        coverage = run["coverage"]["upload_to_synchronized_draft"]
        lines.append(
            "| "
            + " | ".join(
                (
                    f"`{run['run_id']}`",
                    _format_ms(stages["upload_to_editable_content"]),
                    _format_ms(stages["upload_to_synchronized_draft"]),
                    str(counts["model_invocations"]),
                    str(counts["strands_cycles"]),
                    str(counts["printify_requests"]),
                    f"{coverage['coverage_percent']:.2f}%",
                )
            )
            + " |"
        )

    lines.extend(
        [
            "",
            "## Printify call map",
            "",
            "| Purpose | Method | Sanitized route | Calls | Median duration |",
            "|---|---|---|---:|---:|",
        ]
    )
    for call in summary["printify_call_map"]:
        lines.append(
            f"| {call['purpose']} | {call['method']} | `{call['route']}` | "
            f"{call['request_count']} | {_format_ms(call['duration_ms']['median'])} |"
        )

    stage_stats = summary["stage_durations_ms"]
    count_stats = summary["counts_per_run"]
    lines.extend(
        [
            "",
            "## Scorecard",
            "",
            "| Metric | Baseline | Current | Goal |",
            "|---|---:|---:|---:|",
            _scorecard_row(
                "Upload → editable content",
                _median_or_na(stage_stats, "upload_to_editable_content", duration=True),
                "≤30 s stretch",
            ),
            _scorecard_row(
                "Upload → synchronized draft",
                _median_or_na(stage_stats, "upload_to_synchronized_draft", duration=True),
                "≤80 s minimum / ≤50 s strong",
            ),
            _scorecard_row(
                "AI calls normal path",
                _median_or_na(count_stats, "model_invocations"),
                "1",
            ),
            _scorecard_row(
                "Strands normal cycles",
                _median_or_na(count_stats, "strands_cycles"),
                "1 bounded path",
            ),
            _scorecard_row(
                "Printify requests",
                _median_or_na(count_stats, "printify_requests"),
                "≤6",
            ),
            (
                "| Five-file processing | serial (historical) | not measured in Stage 0 | "
                "bounded parallel |"
            ),
            _scorecard_row(
                "Approval → provider submission",
                _median_or_na(stage_stats, "approval_to_provider_submission", duration=True),
                "near-immediate",
            ),
            "",
            "Coverage uses the union of timed intervals clipped to each KPI window; overlapping "
            "and nested spans are never added twice. P90 uses the nearest-rank method.",
            "",
        ]
    )
    return "\n".join(lines)


def _scorecard_row(metric: str, current: str, goal: str) -> str:
    return f"| {metric} | {current} | {current} | {goal} |"


def _median_or_na(
    statistics_by_name: Mapping[str, Mapping[str, float | int]],
    name: str,
    *,
    duration: bool = False,
) -> str:
    statistics_value = statistics_by_name.get(name)
    if statistics_value is None:
        return "not observed"
    median = statistics_value["median"]
    return _format_ms(float(median)) if duration else f"{float(median):g}"


def _format_ms(value: float | None) -> str:
    if value is None:
        return "not observed"
    if value >= 1_000:
        return f"{value / 1_000:.3f} s"
    return f"{value:.3f} ms"


def _milliseconds(delta: Any) -> float:
    return delta.total_seconds() * 1_000


def _round(value: float | None, digits: int = 3) -> float | None:
    return None if value is None else round(float(value), digits)


def _timestamp(value: datetime) -> str:
    normalized = value.astimezone(UTC)
    return normalized.isoformat(timespec="microseconds").replace("+00:00", "Z")


def _parse_output_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--json-output", type=Path)
    parser.add_argument("--markdown-output", type=Path)
    parser.add_argument("--source-commit", required=True)
    parser.add_argument("--release-fingerprint", required=True)
    parser.add_argument("--expected-runs", type=int, default=5)
    return parser


def _write_output(path: Path, contents: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(contents, encoding="utf-8")


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        events = parse_latency_jsonl(arguments.input.read_text(encoding="utf-8"))
        document = assemble_latency_waterfall(
            events,
            source_commit=arguments.source_commit,
            release_fingerprint=arguments.release_fingerprint,
            expected_runs=arguments.expected_runs,
        )
        rendered_json = canonical_json(document)
        rendered_markdown = render_markdown(document)
        if arguments.json_output is not None:
            _write_output(arguments.json_output, rendered_json)
        if arguments.markdown_output is not None:
            _write_output(arguments.markdown_output, rendered_markdown)
    except (LatencyWaterfallError, OSError, UnicodeError, ValueError) as error:
        raise SystemExit(str(error)) from None
    if arguments.json_output is None and arguments.markdown_output is None:
        print(rendered_json, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
