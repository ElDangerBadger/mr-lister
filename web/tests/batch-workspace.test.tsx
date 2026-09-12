import { act, fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter, useLocation, useNavigate, type LinkProps } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import browserFixtures from "../../contracts/browser/phase6.5.fixtures.json";
import { ApiError, type ApiPort, type DecodedResponse } from "../src/api/client";
import { AppContext, type AppDependencies } from "../src/app-context";
import { MemoryAuthSession } from "../src/auth/session";
import { sellerReviewSchema, type JobProgress, type SellerReview } from "../src/contracts";
import { BatchNavigator, BatchWorkspaceProvider, useBatchWorkspace } from "../src/navigation/BatchWorkspace";
import type { BatchUploadItemState, UploadBatchState } from "../src/upload/upload-context";

const upload = vi.hoisted((): { batch: UploadBatchState } => ({ batch: { phase: "idle", items: [], message: "" } }));
vi.mock("../src/upload/upload-context", () => ({ useUpload: () => upload }));
vi.mock("../src/navigation/WorkspaceNavigation", async () => {
  const { Link } = await import("react-router-dom");
  return { WorkspaceLink: (props: LinkProps) => <Link {...props} /> };
});

beforeEach(() => {
  vi.useFakeTimers();
  upload.batch = { phase: "idle", items: [], message: "" };
  Object.defineProperty(document, "visibilityState", { configurable: true, value: "visible" });
  Object.defineProperty(navigator, "onLine", { configurable: true, value: true });
});
afterEach(() => { vi.useRealTimers(); });

describe("batch listing navigation", () => {
  it("opens after upload verification without waiting for listing readiness, only once", async () => {
    const getJob = vi.fn().mockResolvedValue(progress("job_one", "preparing"));
    const harness = mount(getJob);
    upload.batch = batch([item("one", "uploading")], "running");
    harness.refresh();
    expect(screen.getByTestId("auto-open")).toHaveTextContent("true");
    expect(screen.getByTestId("route")).toHaveTextContent(/^\/$/u);
    expect(getJob).not.toHaveBeenCalled();
    upload.batch = batch([item("one", "complete")]);
    harness.refresh();
    await flush();
    expect(screen.getByTestId("route")).toHaveTextContent("/jobs/job_one");
    expect(screen.getByTestId("workspace")).toHaveTextContent("preparing");
    getJob.mockResolvedValue(progress("job_one", "ready_for_review", 2));
    await act(async () => { await vi.advanceTimersByTimeAsync(3_000); });
    expect(screen.getByTestId("route")).toHaveTextContent("/jobs/job_one");
    fireEvent.click(screen.getByText("Home"));
    await act(async () => { await vi.advanceTimersByTimeAsync(12_000); });
    expect(screen.getByTestId("route")).toHaveTextContent(/^\/$/u);
    expect(getJob).toHaveBeenCalledTimes(2);
  });

  it("does not redirect after the user leaves Home and returns", async () => {
    const waiting = deferred<DecodedResponse<JobProgress>>();
    const getJob = vi.fn().mockReturnValue(waiting.promise);
    const harness = mount(getJob);
    upload.batch = batch([item("one", "uploading")], "running");
    harness.refresh();
    fireEvent.click(screen.getByText("Elsewhere"));
    fireEvent.click(screen.getByText("Home"));
    upload.batch = batch([item("one", "complete")]);
    harness.refresh();
    await act(async () => { waiting.resolve(progress("job_one", "ready_for_review")); await Promise.resolve(); });
    expect(screen.getByTestId("route")).toHaveTextContent(/^\/$/u);
    expect(screen.getByTestId("auto-open")).toHaveTextContent("false");
  });

  it("opens the first verified upload while siblings upload, without switching later", async () => {
    const getJob = vi.fn().mockImplementation((jobId: string) => Promise.resolve(progress(jobId,
      "preparing")));
    const harness = mount(getJob);
    upload.batch = batch([item("one", "uploading"), item("two", "complete")], "running");
    harness.refresh();
    await flush();
    expect(screen.getByTestId("route")).toHaveTextContent("/jobs/job_two");
    upload.batch = batch([item("one", "complete"), item("two", "complete")]);
    harness.refresh();
    await flush();
    getJob.mockImplementation((jobId: string) => Promise.resolve(progress(jobId, "ready_for_review", 2)));
    await act(async () => { await vi.advanceTimersByTimeAsync(3_000); });
    expect(screen.getByTestId("route")).toHaveTextContent("/jobs/job_two");
    expect(getJob.mock.calls.filter(([jobId]) => jobId === "job_two")).toHaveLength(3);
    expect(getJob.mock.calls.filter(([jobId]) => jobId === "job_one")).toHaveLength(2);
  });

  it("opens a ready sibling despite another file failing, retaining recovery and sibling links", async () => {
    const getJob = vi.fn().mockImplementation((jobId: string) => Promise.resolve(progress(jobId, jobId === "job_two" ? "needs_revision" : "preparing")));
    const harness = mount(getJob);
    upload.batch = batch([item("one", "error"), item("two", "complete"), item("three", "complete")], "running");
    harness.refresh();
    await flush();
    expect(screen.getByTestId("route")).toHaveTextContent("/jobs/job_two");
    fireEvent.click(screen.getByText("Listing 2 of 3"));
    expect(screen.getByRole("link", { name: "one.png" })).toHaveAttribute("href", "/uploads/upload_one");
    expect(screen.getByRole("link", { name: "two.png" })).toHaveAttribute("aria-current", "page");
    expect(screen.getByText("Ready to edit")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("link", { name: "Next listing: three.png" }));
    expect(screen.getByTestId("route")).toHaveTextContent("/jobs/job_three");
    expect(screen.getByRole("link", { name: "Previous listing: two.png" })).toHaveAttribute("href", "/jobs/job_two");
    expect(getJob).not.toHaveBeenCalledWith("job_one");
  });

  it("ignores stale progress after a new batch replaces a previously completed batch", async () => {
    const stale = deferred<DecodedResponse<JobProgress>>();
    const getJob = vi.fn().mockImplementation((jobId: string) => jobId === "job_one" ? stale.promise : Promise.resolve(progress(jobId, "preparing")));
    upload.batch = batch([item("one", "complete")]);
    const harness = mount(getJob);
    upload.batch = batch([item("two", "uploading")], "running");
    harness.refresh();
    await flush();
    await act(async () => { stale.resolve(progress("job_one", "ready_for_review")); await Promise.resolve(); });
    expect(screen.getByTestId("route")).toHaveTextContent(/^\/$/u);
    expect(screen.getByTestId("workspace")).not.toHaveTextContent("ready_for_review");
    expect(screen.getByTestId("workspace")).toHaveTextContent("two.png");
  });

  it("does not overlap requests across timer, focus, and completed-file changes", async () => {
    const pending = deferred<DecodedResponse<JobProgress>>();
    const getJob = vi.fn().mockImplementation((jobId: string) => jobId === "job_one" ? pending.promise : Promise.resolve(progress(jobId, "preparing")));
    const harness = mount(getJob);
    upload.batch = batch([item("one", "complete")], "running");
    harness.refresh();
    fireEvent.focus(window);
    upload.batch = batch([item("one", "complete"), item("two", "complete")], "running");
    harness.refresh();
    await act(async () => { await vi.advanceTimersByTimeAsync(9_000); });
    expect(getJob.mock.calls.filter(([jobId]) => jobId === "job_one")).toHaveLength(1);
    await act(async () => { pending.resolve(progress("job_one", "preparing")); await Promise.resolve(); });
    await act(async () => { await vi.advanceTimersByTimeAsync(3_000); });
    expect(getJob.mock.calls.filter(([jobId]) => jobId === "job_one")).toHaveLength(2);
  });

  it("pauses while hidden or offline, resumes on visibility/online, and stops terminal polling", async () => {
    Object.defineProperty(document, "visibilityState", { configurable: true, value: "hidden" });
    const getJob = vi.fn().mockResolvedValue(progress("job_one", "terminal_failure"));
    const harness = mount(getJob);
    upload.batch = batch([item("one", "complete")], "running");
    harness.refresh();
    expect(getJob).not.toHaveBeenCalled();
    Object.defineProperty(document, "visibilityState", { configurable: true, value: "visible" });
    Object.defineProperty(navigator, "onLine", { configurable: true, value: false });
    fireEvent(document, new Event("visibilitychange"));
    expect(getJob).not.toHaveBeenCalled();
    Object.defineProperty(navigator, "onLine", { configurable: true, value: true });
    fireEvent(window, new Event("online"));
    await flush();
    upload.batch = batch([item("one", "complete")]);
    harness.refresh();
    await act(async () => { await vi.advanceTimersByTimeAsync(30_000); });
    expect(getJob).toHaveBeenCalledTimes(1);
    expect(screen.getByTestId("auto-open")).toHaveTextContent("false");
    expect(screen.getByTestId("route")).toHaveTextContent("/jobs/job_one");
  });

  it("opens the uploaded workspace even while background status checks back off", async () => {
    const getJob = vi.fn().mockRejectedValueOnce(new ApiError(429, "THROTTLED", "Try later", "request", 10))
      .mockResolvedValue(progress("job_one", "ready_for_review"));
    const harness = mount(getJob);
    upload.batch = batch([item("one", "complete")], "running");
    harness.refresh();
    await flush();
    expect(screen.getByTestId("workspace")).toHaveTextContent("Status is temporarily unavailable");
    await act(async () => { await vi.advanceTimersByTimeAsync(9_000); });
    expect(getJob).toHaveBeenCalledTimes(1);
    expect(screen.getByTestId("route")).toHaveTextContent("/jobs/job_one");
    await act(async () => { await vi.advanceTimersByTimeAsync(1_000); });
    expect(screen.getByTestId("route")).toHaveTextContent("/jobs/job_one");
    expect(screen.getByTestId("workspace")).not.toHaveTextContent("Status is temporarily unavailable");
  });

  it("does not arm automatic navigation for a previously completed batch", async () => {
    upload.batch = batch([item("one", "complete")]);
    mount(vi.fn().mockResolvedValue(progress("job_one", "ready_for_review")));
    await flush();
    expect(screen.getByTestId("route")).toHaveTextContent(/^\/$/u);
    expect(screen.getByTestId("auto-open")).toHaveTextContent("false");
  });

  it("clears session labels and ignores pending responses on sign-out", async () => {
    const pending = deferred<DecodedResponse<JobProgress>>();
    const harness = mount(vi.fn().mockReturnValue(pending.promise));
    upload.batch = batch([item("one", "complete")], "running");
    harness.refresh();
    expect(screen.getByTestId("workspace")).toHaveTextContent("one.png");
    act(() => { harness.session.clear(); });
    await act(async () => { pending.resolve(progress("job_one", "ready_for_review")); await Promise.resolve(); });
    expect(screen.getByTestId("workspace")).not.toHaveTextContent("one.png");
    expect(screen.getByTestId("workspace")).not.toHaveTextContent("ready_for_review");
    expect(screen.getByTestId("route")).toHaveTextContent("/jobs/job_one");
  });

  it("stays on Home when every upload fails verification", async () => {
    const getJob = vi.fn();
    const harness = mount(getJob);
    upload.batch = batch([item("one", "finalizing"), item("two", "uploading")], "running");
    harness.refresh();
    upload.batch = batch([item("one", "error"), item("two", "expired")]);
    harness.refresh();
    await flush();
    expect(screen.getByTestId("route")).toHaveTextContent(/^\/$/u);
    expect(screen.getByTestId("auto-open")).toHaveTextContent("false");
    expect(getJob).not.toHaveBeenCalled();
  });

  it("counts queued artwork in the batch and retains updated approval status after switching listings", async () => {
    const harness = mount(vi.fn().mockImplementation((jobId: string) => Promise.resolve(progress(jobId, "ready_for_review"))), "/jobs/job_one");
    upload.batch = batch([item("one", "complete"), item("two", "uploading")], "running");
    harness.refresh();
    await flush();
    expect(screen.getByText("Listing 1 of 2")).toBeInTheDocument();
    harness.showReview(reviewFor("job_one", "approved", 2));
    fireEvent.click(screen.getByText("Listing 1 of 2"));
    expect(screen.getByText("Review approved")).toBeInTheDocument();
    fireEvent.click(screen.getByText("Elsewhere"));
    expect(screen.getByText("Review approved")).toBeInTheDocument();
    expect(screen.getByTestId("workspace")).toHaveTextContent('"display_state":"approved"');
  });

  it("does not regress newer cached status or ingest a review from another route/session", async () => {
    const harness = mount(vi.fn().mockResolvedValue(progress("job_one", "approved", 4)), "/jobs/job_one");
    upload.batch = batch([item("one", "complete")]);
    harness.refresh();
    await flush();
    harness.showReview(reviewFor("job_one", "ready_for_review", 3));
    expect(screen.getByTestId("workspace")).toHaveTextContent('"display_state":"approved"');
    harness.showReview(reviewFor("job_other", "ready_for_review", 5));
    expect(screen.getByTestId("workspace")).not.toHaveTextContent("job_other");
    act(() => { harness.session.clear(); });
    harness.showReview(reviewFor("job_one", "ready_for_review", 6));
    expect(screen.getByTestId("workspace")).not.toHaveTextContent("job_one");
  });

  it("resumes preparation polling if a review edit moves a stopped ready job back into synchronization", async () => {
    const getJob = vi.fn().mockResolvedValue(progress("job_one", "ready_for_review"));
    const harness = mount(getJob, "/jobs/job_one");
    upload.batch = batch([item("one", "complete")]);
    harness.refresh();
    await flush();
    expect(getJob).toHaveBeenCalledTimes(1);
    getJob.mockResolvedValue(progress("job_one", "synchronizing", 2));
    harness.showReview(reviewFor("job_one", "synchronizing", 2));
    await flush();
    expect(getJob).toHaveBeenCalledTimes(2);
    getJob.mockResolvedValue(progress("job_one", "ready_for_review", 3));
    fireEvent.click(screen.getByText("Elsewhere"));
    await act(async () => { await vi.advanceTimersByTimeAsync(3_000); });
    expect(getJob).toHaveBeenCalledTimes(3);
    expect(screen.getByTestId("workspace")).toHaveTextContent('"display_state":"ready_for_review"');
  });
});

function Probe({ currentReview }: { currentReview?: SellerReview }) {
  const workspace = useBatchWorkspace();
  const location = useLocation();
  const navigate = useNavigate();
  return <>
    <output data-testid="route">{location.pathname}</output>
    <output data-testid="auto-open">{String(workspace.autoOpenPending)}</output>
    <output data-testid="workspace">{JSON.stringify(workspace)}</output>
    <button type="button" onClick={() => { void navigate("/"); }}>Home</button>
    <button type="button" onClick={() => { void navigate("/jobs/elsewhere"); }}>Elsewhere</button>
    <BatchNavigator {...(currentReview === undefined ? {} : { currentReview })} />
  </>;
}

function mount(getJob: ApiPort["getJob"], initialPath = "/") {
  const session = new MemoryAuthSession();
  session.set("access", 3600, "refresh");
  const unexpected = () => Promise.reject(new Error("Unexpected API call"));
  const dependencies: AppDependencies = {
    api: { getJob, listJobs: unexpected, getUpload: unexpected, getReview: unexpected,
      createUpload: unexpected, authorizeUpload: unexpected, completeUpload: unexpected, cancelUpload: unexpected,
      reviseListing: unexpected, runAction: unexpected, fetchArtwork: unexpected },
    auth: { session, startSignIn: unexpected, completeSignIn: unexpected, signOut: () => { session.clear(); } },
  };
  let currentReview: SellerReview | undefined;
  const tree = () => <MemoryRouter initialEntries={[initialPath]}><AppContext.Provider value={dependencies}>
    <BatchWorkspaceProvider><Probe {...(currentReview === undefined ? {} : { currentReview })} /></BatchWorkspaceProvider>
  </AppContext.Provider></MemoryRouter>;
  const result = render(tree());
  return { session, refresh: () => result.rerender(tree()), showReview: (review: SellerReview) => {
    currentReview = review;
    result.rerender(tree());
  } };
}

function item(key: string, phase: BatchUploadItemState["phase"]): BatchUploadItemState {
  return { id: key, position: 1, filename: `${key}.png`, preparedFilename: null, sizeBytes: 100,
    sourceFormat: "png", phase, progress: phase === "complete" ? 100 : 0,
    jobId: `job_${key}`, uploadId: `upload_${key}`, message: "", error: null, requestId: null };
}
function batch(items: BatchUploadItemState[], phase: UploadBatchState["phase"] = "complete"): UploadBatchState {
  return { items: items.map((entry, index) => ({ ...entry, position: index + 1 })), phase, message: "" };
}
function progress(jobId: string, displayState: JobProgress["display_state"], version = 1): DecodedResponse<JobProgress> {
  const review = sellerReviewSchema.parse(browserFixtures.seller_review_pending);
  return { value: { contract_version: review.contract_version, job_id: jobId, record_version: version,
    review_version: 0, display_state: displayState, stage: "artwork_review", authority_notice: review.authority_notice,
    actions: review.actions, failure: null, provider_outcome_unconfirmed: false,
    created_at: review.created_at, updated_at: review.updated_at }, requestId: "request", etag: null };
}
function reviewFor(jobId: string, displayState: SellerReview["display_state"], version: number): SellerReview {
  return { ...sellerReviewSchema.parse(browserFixtures.seller_review_pending), job_id: jobId, display_state: displayState, record_version: version };
}
function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((complete) => { resolve = complete; });
  return { promise, resolve };
}
async function flush() { await act(async () => { await Promise.resolve(); await Promise.resolve(); }); }
