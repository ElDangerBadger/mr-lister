const LATENCY_TRACE_PREFIX = "latency_trace=";
const LATENCY_TRACE_SCHEMA_VERSION = "1.0.0";
const LATENCY_TRACE_BUFFER_LIMIT = 100;
const LATENCY_RUN_DOMAIN = "mr-lister-latency-v1:";

export type BrowserLatencySpanName = "artwork_normalization" | "direct_artwork_upload";
export type BrowserLatencyMilestoneName = "upload_response_received";

interface BrowserLatencyEventBase {
  schema_version: typeof LATENCY_TRACE_SCHEMA_VERSION;
  event_id: string;
  run_id: string;
  component: "seller_web";
}

export interface BrowserLatencySpanEvent extends BrowserLatencyEventBase {
  kind: "span";
  name: BrowserLatencySpanName;
  outcome: "succeeded" | "failed";
  started_at: string;
  completed_at: string;
  duration_ms: number;
}

export interface BrowserLatencyMilestoneEvent extends BrowserLatencyEventBase {
  kind: "milestone";
  name: BrowserLatencyMilestoneName;
  outcome: "observed";
  occurred_at: string;
}

export type BrowserLatencyEvent = BrowserLatencySpanEvent | BrowserLatencyMilestoneEvent;

export interface BrowserLatencySpanClock {
  startedAt: string;
  startedMonotonicMs: number;
}

export interface BrowserLatencySpanTiming {
  startedAt: string;
  completedAt: string;
  durationMs: number;
}

const eventBuffer: BrowserLatencyEvent[] = [];

export function startBrowserLatencySpan(): BrowserLatencySpanClock {
  return {
    startedAt: new Date().toISOString(),
    startedMonotonicMs: performance.now(),
  };
}

export function completeBrowserLatencySpan(
  clock: BrowserLatencySpanClock,
): BrowserLatencySpanTiming {
  return {
    startedAt: clock.startedAt,
    completedAt: new Date().toISOString(),
    durationMs: Math.max(0, roundMilliseconds(performance.now() - clock.startedMonotonicMs)),
  };
}

export function recordBrowserLatencySpan(
  jobId: string,
  name: BrowserLatencySpanName,
  timing: BrowserLatencySpanTiming,
  outcome: "succeeded" | "failed",
): void {
  void emitForJob(jobId, (runId) => ({
    schema_version: LATENCY_TRACE_SCHEMA_VERSION,
    event_id: newEventId(),
    run_id: runId,
    kind: "span",
    name,
    component: "seller_web",
    outcome,
    started_at: timing.startedAt,
    completed_at: timing.completedAt,
    duration_ms: timing.durationMs,
  }));
}

export function recordBrowserLatencyMilestone(
  jobId: string,
  name: BrowserLatencyMilestoneName,
): void {
  const occurredAt = new Date().toISOString();
  void emitForJob(jobId, (runId) => ({
    schema_version: LATENCY_TRACE_SCHEMA_VERSION,
    event_id: newEventId(),
    run_id: runId,
    kind: "milestone",
    name,
    component: "seller_web",
    outcome: "observed",
    occurred_at: occurredAt,
  }));
}

export async function latencyRunIdForJob(jobId: string): Promise<string> {
  if (jobId.length < 1 || jobId.length > 128 || !/^[\x20-\x7E]+$/u.test(jobId)) {
    throw new Error("Latency job identity is invalid.");
  }
  const input = new TextEncoder().encode(`${LATENCY_RUN_DOMAIN}${jobId}`);
  const digest = new Uint8Array(await crypto.subtle.digest("SHA-256", input));
  return [...digest]
    .slice(0, 12)
    .map((value) => value.toString(16).padStart(2, "0"))
    .join("");
}

export function bufferedBrowserLatencyEvents(): readonly BrowserLatencyEvent[] {
  return eventBuffer.map((event) => ({ ...event }));
}

export function clearBufferedBrowserLatencyEvents(): void {
  eventBuffer.length = 0;
}

async function emitForJob(
  jobId: string,
  build: (runId: string) => BrowserLatencyEvent,
): Promise<void> {
  try {
    const event = build(await latencyRunIdForJob(jobId));
    eventBuffer.push(event);
    if (eventBuffer.length > LATENCY_TRACE_BUFFER_LIMIT) {
      eventBuffer.splice(0, eventBuffer.length - LATENCY_TRACE_BUFFER_LIMIT);
    }
    try {
      console.info(`${LATENCY_TRACE_PREFIX}${JSON.stringify(event)}`);
    } catch {
      // Timing diagnostics are best-effort and cannot affect the upload workflow.
    }
  } catch {
    // Hashing or event construction failures are observational only.
  }
}

function newEventId(): string {
  return crypto.randomUUID().replaceAll("-", "");
}

function roundMilliseconds(value: number): number {
  return Math.round(value * 1_000) / 1_000;
}
