import { waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import {
  bufferedBrowserLatencyEvents,
  clearBufferedBrowserLatencyEvents,
  completeBrowserLatencySpan,
  latencyRunIdForJob,
  recordBrowserLatencyMilestone,
  recordBrowserLatencySpan,
  startBrowserLatencySpan,
} from "../src/observability/latency";

const JOB_ID = "job_example";

beforeEach(() => {
  clearBufferedBrowserLatencyEvents();
  vi.spyOn(console, "info").mockImplementation(() => undefined);
});

describe("browser latency observability", () => {
  it("uses the cross-runtime one-way job correlation convention", async () => {
    await expect(latencyRunIdForJob(JOB_ID)).resolves.toBe("d589007d5d7b454348fb3b5f");
    await expect(latencyRunIdForJob("")).rejects.toThrow("identity");
  });

  it("records parseable sanitized spans and milestones without blocking callers", async () => {
    const spanClock = startBrowserLatencySpan();
    recordBrowserLatencySpan(
      JOB_ID,
      "artwork_normalization",
      completeBrowserLatencySpan(spanClock),
      "succeeded",
    );
    recordBrowserLatencyMilestone(JOB_ID, "upload_response_received");

    await waitFor(() => expect(bufferedBrowserLatencyEvents()).toHaveLength(2));
    const events = bufferedBrowserLatencyEvents();
    const span = events.find((event) => event.kind === "span");
    const milestone = events.find((event) => event.kind === "milestone");
    expect(span).toMatchObject({
      schema_version: "1.0.0",
      run_id: "d589007d5d7b454348fb3b5f",
      kind: "span",
      name: "artwork_normalization",
      component: "seller_web",
      outcome: "succeeded",
    });
    expect(span).toHaveProperty("started_at");
    expect(span).toHaveProperty("completed_at");
    expect(span).toHaveProperty("duration_ms");
    expect(milestone).toMatchObject({
      schema_version: "1.0.0",
      run_id: "d589007d5d7b454348fb3b5f",
      kind: "milestone",
      name: "upload_response_received",
      component: "seller_web",
      outcome: "observed",
    });
    expect(milestone).toHaveProperty("occurred_at");

    const serialized = JSON.stringify(events);
    expect(serialized).not.toContain(JOB_ID);
    expect(serialized).not.toContain("filename");
    expect(serialized).not.toContain("upload_id");
    expect(console.info).toHaveBeenCalledTimes(2);
    for (const [line] of vi.mocked(console.info).mock.calls) {
      expect(line).toMatch(/^latency_trace=\{"schema_version":"1\.0\.0"/u);
      expect(() => {
        JSON.parse(String(line).slice("latency_trace=".length));
      }).not.toThrow();
    }
  });

  it("caps its process-local diagnostic buffer at the newest 100 events", async () => {
    for (let index = 0; index < 105; index += 1) {
      recordBrowserLatencyMilestone(`job_${index}`, "upload_response_received");
    }

    await waitFor(() => expect(console.info).toHaveBeenCalledTimes(105));
    const events = bufferedBrowserLatencyEvents();
    expect(events).toHaveLength(100);
  });

  it("records first browser editing separately from backend save readiness", async () => {
    recordBrowserLatencyMilestone(JOB_ID, "first_editable_review");
    await waitFor(() => expect(bufferedBrowserLatencyEvents()).toHaveLength(1));
    expect(bufferedBrowserLatencyEvents()[0]).toMatchObject({
      run_id: "d589007d5d7b454348fb3b5f",
      component: "seller_web",
      name: "first_editable_review",
      kind: "milestone",
      outcome: "observed",
    });
  });

  it("keeps console failures outside the observed workflow", async () => {
    vi.mocked(console.info).mockImplementation(() => {
      throw new Error("console unavailable");
    });

    expect(() => recordBrowserLatencyMilestone(JOB_ID, "upload_response_received")).not.toThrow();
    await waitFor(() => expect(bufferedBrowserLatencyEvents()).toHaveLength(1));
  });
});
