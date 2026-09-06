"""Best-effort, data-minimized latency tracing for the optimization program.

The trace is deliberately observational.  It writes compact JSON to stdout, never
persists application authority, and must never change the result of the operation it
observes.  A one-way digest of the opaque job ID joins events across runtime boundaries
without logging seller, artwork, provider, or marketplace identities.
"""

from __future__ import annotations

import json
import re
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar, Token
from datetime import UTC, datetime
from hashlib import sha256
from time import perf_counter_ns
from typing import IO, Literal, Protocol
from uuid import uuid4

from pydantic import Field, model_validator

from mr_lister.contracts import ContractModel

LATENCY_TRACE_PREFIX = "latency_trace="
LATENCY_TRACE_SCHEMA_VERSION = "1.0.0"

LatencyEventKind = Literal["milestone", "span", "provider_request"]
LatencyOutcome = Literal["observed", "succeeded", "failed"]
LatencyHttpMethod = Literal["GET", "POST", "PUT"]

_SAFE_NAME = re.compile(r"^[a-z][a-z0-9_]{0,79}$")
_SAFE_COMPONENT = re.compile(r"^[a-z][a-z0-9_]{0,47}$")
_SAFE_MODEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}$")
_SAFE_ROUTES = frozenset(
    {
        "/v1/shops.json",
        "/v1/catalog/blueprints.json",
        "/v1/catalog/blueprints/{blueprint_id}/print_providers.json",
        ("/v1/catalog/blueprints/{blueprint_id}/print_providers/{print_provider_id}/variants.json"),
        "/v1/shops/{shop_id}/products.json",
        "/v1/shops/{shop_id}/products/{product_id}.json",
        "/v1/shops/{shop_id}/products/{product_id}/publish.json",
        "/v1/uploads/images.json",
        "/v1/uploads.json",
        "/v1/uploads/{image_id}.json",
        (
            "/v2/catalog/blueprints/{blueprint_id}/print_providers/"
            "{print_provider_id}/shipping/standard.json"
        ),
    }
)
_ACTIVE_RUN_ID: ContextVar[str | None] = ContextVar("mr_lister_latency_run_id", default=None)


class LatencyTraceEvent(ContractModel):
    """One allowlisted trace event safe for ordinary application logs."""

    schema_version: Literal["1.0.0"] = LATENCY_TRACE_SCHEMA_VERSION
    event_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    run_id: str = Field(pattern=r"^[a-f0-9]{24}$")
    kind: LatencyEventKind
    name: str = Field(min_length=1, max_length=80)
    component: str = Field(min_length=1, max_length=48)
    outcome: LatencyOutcome
    occurred_at: datetime | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    duration_ms: float | None = Field(default=None, ge=0)
    provider: Literal["printify"] | None = None
    method: LatencyHttpMethod | None = None
    route: str | None = None
    purpose: str | None = Field(default=None, min_length=1, max_length=80)
    attempt: int | None = Field(default=None, ge=1, le=100)
    cycle: int | None = Field(default=None, ge=1, le=100)
    model_id: str | None = Field(default=None, min_length=1, max_length=256)

    @model_validator(mode="after")
    def event_shape_is_closed_and_safe(self) -> LatencyTraceEvent:
        if _SAFE_NAME.fullmatch(self.name) is None:
            raise ValueError("Latency event name is invalid")
        if _SAFE_COMPONENT.fullmatch(self.component) is None:
            raise ValueError("Latency component is invalid")
        if self.purpose is not None and _SAFE_NAME.fullmatch(self.purpose) is None:
            raise ValueError("Latency purpose is invalid")
        if self.model_id is not None and _SAFE_MODEL.fullmatch(self.model_id) is None:
            raise ValueError("Latency model ID is invalid")
        if self.kind == "milestone":
            if (
                self.occurred_at is None
                or any(
                    value is not None
                    for value in (self.started_at, self.completed_at, self.duration_ms)
                )
                or any(value is not None for value in (self.provider, self.method, self.route))
                or self.outcome != "observed"
            ):
                raise ValueError("Latency milestone shape is invalid")
        else:
            if (
                self.occurred_at is not None
                or self.started_at is None
                or self.completed_at is None
                or self.duration_ms is None
                or self.completed_at < self.started_at
                or self.outcome == "observed"
            ):
                raise ValueError("Latency span shape is invalid")
            provider_fields = (self.provider, self.method, self.route, self.purpose)
            if self.kind == "provider_request":
                if any(value is None for value in provider_fields):
                    raise ValueError("Provider request trace is incomplete")
                assert self.route is not None
                if self.route not in _SAFE_ROUTES:
                    raise ValueError("Provider request route is invalid")
            elif any(value is not None for value in (self.provider, self.method, self.route)):
                raise ValueError("Ordinary latency spans cannot carry provider request fields")
        for value in (self.occurred_at, self.started_at, self.completed_at):
            if value is not None and value.utcoffset() is None:
                raise ValueError("Latency timestamps must be timezone-aware")
        return self


class LatencyTraceSink(Protocol):
    def write(self, event: LatencyTraceEvent) -> None: ...


class LoggingLatencyTraceSink:
    """Write compact trace events to the runtime's captured stdout stream."""

    def __init__(self, stream: IO[str] | None = None) -> None:
        self._stream = stream or sys.stdout

    def write(self, event: LatencyTraceEvent) -> None:
        payload = event.model_dump_json(exclude_none=True)
        print(f"{LATENCY_TRACE_PREFIX}{payload}", file=self._stream, flush=True)


class InMemoryLatencyTraceSink:
    def __init__(self) -> None:
        self.events: list[LatencyTraceEvent] = []

    def write(self, event: LatencyTraceEvent) -> None:
        self.events.append(event)


def latency_run_id(job_id: str) -> str:
    """Return the stable, non-reversible run correlation used by every component."""

    if not isinstance(job_id, str) or not job_id or len(job_id) > 128 or not job_id.isascii():
        raise ValueError("Latency job identity is invalid")
    return sha256(f"mr-lister-latency-v1:{job_id}".encode()).hexdigest()[:24]


@contextmanager
def bind_latency_run(job_id: str) -> Iterator[str]:
    """Bind one job digest for nested adapters without retaining the raw job ID."""

    run_id = latency_run_id(job_id)
    token: Token[str | None] = _ACTIVE_RUN_ID.set(run_id)
    try:
        yield run_id
    finally:
        _ACTIVE_RUN_ID.reset(token)


def current_latency_run_id() -> str | None:
    return _ACTIVE_RUN_ID.get()


def emit_latency_milestone(
    name: str,
    *,
    component: str,
    sink: LatencyTraceSink | None = None,
    occurred_at: datetime | None = None,
) -> None:
    """Emit one best-effort point event when a run is currently bound."""

    run_id = current_latency_run_id()
    if run_id is None:
        return
    try:
        (sink or LoggingLatencyTraceSink()).write(
            LatencyTraceEvent(
                event_id=uuid4().hex,
                run_id=run_id,
                kind="milestone",
                name=name,
                component=component,
                outcome="observed",
                occurred_at=(occurred_at or datetime.now(UTC)).astimezone(UTC),
            )
        )
    except Exception:
        # Timing evidence can be absent; it can never change application authority.
        return


def emit_latency_span(
    name: str,
    *,
    component: str,
    started_at: datetime,
    completed_at: datetime,
    duration_ms: float,
    outcome: Literal["succeeded", "failed"],
    sink: LatencyTraceSink | None = None,
    kind: Literal["span", "provider_request"] = "span",
    provider: Literal["printify"] | None = None,
    method: LatencyHttpMethod | None = None,
    route: str | None = None,
    purpose: str | None = None,
    attempt: int | None = None,
    cycle: int | None = None,
    model_id: str | None = None,
) -> None:
    """Emit an already measured span without allowing evidence failure to escape."""

    run_id = current_latency_run_id()
    if run_id is None:
        return
    try:
        (sink or LoggingLatencyTraceSink()).write(
            LatencyTraceEvent(
                event_id=uuid4().hex,
                run_id=run_id,
                kind=kind,
                name=name,
                component=component,
                outcome=outcome,
                started_at=started_at.astimezone(UTC),
                completed_at=completed_at.astimezone(UTC),
                duration_ms=round(duration_ms, 3),
                provider=provider,
                method=method,
                route=route,
                purpose=purpose,
                attempt=attempt,
                cycle=cycle,
                model_id=model_id,
            )
        )
    except Exception:
        return


@contextmanager
def latency_span(
    name: str,
    *,
    component: str,
    sink: LatencyTraceSink | None = None,
    kind: Literal["span", "provider_request"] = "span",
    provider: Literal["printify"] | None = None,
    method: LatencyHttpMethod | None = None,
    route: str | None = None,
    purpose: str | None = None,
    attempt: int | None = None,
    cycle: int | None = None,
    model_id: str | None = None,
) -> Iterator[None]:
    """Measure an operation without changing its result or exception behavior."""

    run_id = current_latency_run_id()
    started_at = datetime.now(UTC)
    started_ns = perf_counter_ns()
    outcome: Literal["succeeded", "failed"] = "succeeded"
    try:
        yield
    except BaseException:
        outcome = "failed"
        raise
    finally:
        if run_id is not None:
            completed_at = datetime.now(UTC)
            duration_ms = round((perf_counter_ns() - started_ns) / 1_000_000, 3)
            emit_latency_span(
                name,
                component=component,
                started_at=started_at,
                completed_at=completed_at,
                duration_ms=duration_ms,
                outcome=outcome,
                sink=sink,
                kind=kind,
                provider=provider,
                method=method,
                route=route,
                purpose=purpose,
                attempt=attempt,
                cycle=cycle,
                model_id=model_id,
            )


def parse_latency_trace_line(line: str) -> LatencyTraceEvent:
    """Decode a direct stdout trace line for local verification and tooling."""

    marker = line.find(LATENCY_TRACE_PREFIX)
    if marker < 0:
        raise ValueError("Latency trace prefix is missing")
    raw = line[marker + len(LATENCY_TRACE_PREFIX) :].strip()
    return LatencyTraceEvent.model_validate(json.loads(raw))


__all__ = [
    "InMemoryLatencyTraceSink",
    "LATENCY_TRACE_PREFIX",
    "LATENCY_TRACE_SCHEMA_VERSION",
    "LatencyTraceEvent",
    "LatencyTraceSink",
    "LoggingLatencyTraceSink",
    "bind_latency_run",
    "current_latency_run_id",
    "emit_latency_milestone",
    "emit_latency_span",
    "latency_run_id",
    "latency_span",
    "parse_latency_trace_line",
]
