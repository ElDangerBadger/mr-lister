import { act, renderHook, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import type { ApiPort, DecodedResponse } from "../src/api/client";
import { MemoryAuthSession, type SessionStatus } from "../src/auth/session";
import type { JobPage, JobSummary } from "../src/contracts";
import { useRecentJobs } from "../src/navigation/use-recent-jobs";

const acknowledgment = { value: { cleared_before: "2026-09-14T20:00:00Z" }, requestId: "clear", etag: null };
const oldJob = job("old");
const newJob = job("new");

describe("account recent history", () => {
  it("keeps new uploads returned after clearing old history", async () => {
    const api = fixture();
    api.listJobs.mockResolvedValueOnce(page([oldJob])).mockResolvedValueOnce(page([newJob]));
    const { result } = mount(api);
    await waitFor(() => expect(result.current.jobs).toEqual([oldJob]));
    await act(async () => { await result.current.clear(); });
    expect(result.current.jobs).toEqual([newJob]);
    expect(result.current.cleared).toBe(true);
    expect(api.clearRecentJobs).toHaveBeenCalledTimes(1);
  });

  it("preserves the visible list after an uncertain clear and retries the same operation", async () => {
    const api = fixture();
    api.listJobs.mockResolvedValueOnce(page([oldJob])).mockResolvedValueOnce(page([newJob]));
    api.clearRecentJobs.mockRejectedValueOnce(new TypeError("Connection interrupted"));
    const { result } = mount(api);
    await waitFor(() => expect(result.current.jobs).toEqual([oldJob]));
    await act(async () => { await result.current.clear(); });
    expect(result.current.jobs).toEqual([oldJob]);
    expect(result.current.clearError).toContain("couldn’t confirm");
    expect(result.current.cleared).toBe(false);
    await act(async () => { await result.current.clear(); });
    expect(api.clearRecentJobs.mock.calls[0]).toEqual(api.clearRecentJobs.mock.calls[1]);
    expect(result.current.jobs).toEqual([newJob]);
    expect(result.current.clearError).toBeNull();
    await act(async () => { await result.current.clear(); });
    expect(api.clearRecentJobs.mock.calls[2]).not.toEqual(api.clearRecentJobs.mock.calls[1]);
  });

  it("deduplicates clearing while the server acknowledgment is pending", async () => {
    const pending = deferred<typeof acknowledgment>();
    const api = fixture();
    api.clearRecentJobs.mockReturnValue(pending.promise);
    const { result } = mount(api);
    await waitFor(() => expect(result.current.loading).toBe(false));
    let first: Promise<void>;
    act(() => { first = result.current.clear(); void result.current.clear(); });
    expect(result.current.clearing).toBe(true);
    expect(api.clearRecentJobs).toHaveBeenCalledTimes(1);
    await act(async () => { pending.resolve(acknowledgment); await first; });
    expect(result.current.clearing).toBe(false);
  });

  it("ignores a delayed pre-clear history response", async () => {
    const previousRead = deferred<DecodedResponse<JobPage>>();
    const api = fixture();
    api.listJobs.mockResolvedValueOnce(page([oldJob])).mockReturnValueOnce(previousRead.promise)
      .mockResolvedValueOnce(page([newJob]));
    const { result, rerender } = mount(api);
    await waitFor(() => expect(result.current.jobs).toEqual([oldJob]));
    rerender({ status: "authenticated", refresh: true });
    await waitFor(() => expect(api.listJobs).toHaveBeenCalledTimes(2));
    await act(async () => { await result.current.clear(); });
    expect(result.current.jobs).toEqual([newJob]);
    await act(async () => { previousRead.resolve(page([oldJob])); await previousRead.promise; });
    expect(result.current.jobs).toEqual([newJob]);
  });

  it("keeps acknowledged history cleared if the following read fails", async () => {
    const api = fixture();
    api.listJobs.mockResolvedValueOnce(page([oldJob])).mockRejectedValueOnce(new Error("Read unavailable"));
    const { result } = mount(api);
    await waitFor(() => expect(result.current.jobs).toEqual([oldJob]));
    await act(async () => { await result.current.clear(); });
    expect(result.current.jobs).toEqual([]);
    expect(result.current.cleared).toBe(true);
    expect(result.current.error).toBe("Read unavailable");
    expect(result.current.clearError).toBeNull();
    await act(async () => { await result.current.load(); });
    expect(result.current.error).toBeNull();
    expect(api.clearRecentJobs).toHaveBeenCalledTimes(1);
  });

  it("ignores clear completion after sign-out without making an authenticated follow-up read", async () => {
    const pending = deferred<typeof acknowledgment>();
    const api = fixture();
    api.listJobs.mockResolvedValue(page([oldJob]));
    api.clearRecentJobs.mockReturnValue(pending.promise);
    const { result, rerender, session } = mount(api);
    await waitFor(() => expect(result.current.jobs).toEqual([oldJob]));
    let clearing: Promise<void>;
    act(() => { clearing = result.current.clear(); });
    session.clear();
    rerender({ status: "anonymous", refresh: false });
    await act(async () => { pending.resolve(acknowledgment); await clearing; });
    expect(result.current.jobs).toEqual([]);
    expect(result.current.cleared).toBe(false);
    expect(api.listJobs).toHaveBeenCalledTimes(1);
  });

  it("does not restore another account's jobs from a delayed read after the account changes", async () => {
    const oldRead = deferred<DecodedResponse<JobPage>>();
    const firstApi = fixture();
    firstApi.listJobs.mockReturnValue(oldRead.promise);
    const firstSession = authenticatedSession();
    const secondApi = fixture();
    secondApi.listJobs.mockResolvedValue(page([newJob]));
    const secondSession = authenticatedSession();
    const { result, rerender } = renderHook(({ api, session }) => useRecentJobs(api, session, "authenticated", false), {
      initialProps: { api: firstApi, session: firstSession },
    });
    rerender({ api: secondApi, session: secondSession });
    await waitFor(() => expect(result.current.jobs).toEqual([newJob]));
    await act(async () => { oldRead.resolve(page([oldJob])); await oldRead.promise; });
    expect(result.current.jobs).toEqual([newJob]);
  });

  it("follows empty filtered pages until a visible upload is found", async () => {
    const api = fixture();
    api.listJobs.mockResolvedValueOnce(page([], "cursor_1")).mockResolvedValueOnce(page([], "cursor_2"))
      .mockResolvedValueOnce(page([newJob]));
    const { result } = mount(api);
    await waitFor(() => expect(result.current.jobs).toEqual([newJob]));
    expect(api.listJobs.mock.calls).toEqual([[undefined], ["cursor_1"], ["cursor_2"]]);
    expect(result.current.nextCursor).toBeNull();
  });

  it("bounds empty-page loading and permits explicit continuation without duplicating rows", async () => {
    const api = fixture();
    for (let index = 1; index <= 4; index += 1) api.listJobs.mockResolvedValueOnce(page([], `cursor_${index}`));
    api.listJobs.mockResolvedValueOnce(page([newJob], "cursor_5"))
      .mockResolvedValueOnce(page([newJob, oldJob]));
    const { result } = mount(api);
    await waitFor(() => expect(result.current.nextCursor).toBe("cursor_4"));
    expect(api.listJobs).toHaveBeenCalledTimes(4);
    expect(result.current.jobs).toEqual([]);
    await act(async () => { await result.current.load(result.current.nextCursor!); });
    expect(result.current.jobs).toEqual([newJob]);
    await act(async () => { await result.current.load(result.current.nextCursor!); });
    expect(result.current.jobs).toEqual([newJob, oldJob]);
    expect(result.current.nextCursor).toBeNull();
  });

  it("stops if a history cursor repeats instead of issuing an unbounded query", async () => {
    const api = fixture();
    api.listJobs.mockResolvedValue(page([], "cursor_repeated"));
    const { result } = mount(api);
    await waitFor(() => expect(result.current.error).toContain("could not be loaded"));
    expect(api.listJobs).toHaveBeenCalledTimes(2);
    expect(result.current.loading).toBe(false);
  });
});

function fixture() {
  return {
    listJobs: vi.fn<ApiPort["listJobs"]>().mockResolvedValue(page([])),
    clearRecentJobs: vi.fn<ApiPort["clearRecentJobs"]>().mockResolvedValue(acknowledgment),
  };
}

function mount(api: ReturnType<typeof fixture>) {
  const session = authenticatedSession();
  const hook = renderHook(({ status, refresh }: { status: SessionStatus; refresh: boolean }) => useRecentJobs(api, session, status, refresh), {
    initialProps: { status: "authenticated" as SessionStatus, refresh: false },
  });
  return { ...hook, session };
}

function authenticatedSession() {
  const session = new MemoryAuthSession();
  session.set("access", 3600, "refresh");
  return session;
}

function job(suffix: string): JobSummary {
  return { job_id: `job_${suffix}`, state: "approved", record_version: 1, review_version: 1, created_at: "2026-09-14T20:00:00Z", updated_at: "2026-09-14T20:00:00Z" };
}

function page(jobs: JobSummary[], cursor: string | null = null): DecodedResponse<JobPage> {
  return { value: { jobs, next_cursor: cursor }, requestId: "jobs", etag: null };
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => { resolve = done; });
  return { promise, resolve };
}
