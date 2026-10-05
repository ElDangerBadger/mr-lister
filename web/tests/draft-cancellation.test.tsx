import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, useLocation, useNavigate } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";
import browserFixtures from "../../contracts/browser/phase6.5.fixtures.json";
import { ApiError, type ApiPort, type DecodedResponse } from "../src/api/client";
import { AppRoutes } from "../src/App";
import { MemoryAuthSession, type AuthCoordinator } from "../src/auth/session";
import { sellerReviewSchema, type CommandResponse, type JobPage, type JobProgress, type SellerReview } from "../src/contracts";

describe("draft cancellation", () => {
  it("places local Cancel beside Submit, clears selected files without an API mutation, and allows a new selection", async () => {
    const createUpload = vi.fn<ApiPort["createUpload"]>();
    const authorizeUpload = vi.fn<ApiPort["authorizeUpload"]>();
    const completeUpload = vi.fn<ApiPort["completeUpload"]>();
    const cancelUpload = vi.fn<ApiPort["cancelUpload"]>();
    const runAction = vi.fn<ApiPort["runAction"]>();
    const app = dependencies(pendingReview(), { createUpload, authorizeUpload, completeUpload, cancelUpload, runAction });
    renderApp(app, "/");
    const input = await screen.findByLabelText(/Drag and drop PNG, SVG, or JPEG artwork/u);
    const user = userEvent.setup();
    await user.upload(input, png("first.png"));
    const submit = screen.getByRole("button", { name: "Submit" });
    const cancel = screen.getByRole("button", { name: "Cancel" });
    expect(cancel.parentElement).toBe(submit.parentElement);
    expect(cancel).toBeEnabled();
    await user.click(cancel);
    const notice = screen.getByRole("heading", { name: "Draft Canceled" }).closest('[role="status"]');
    expect(notice).toHaveAttribute("aria-live", "polite");
    expect(screen.queryByRole("button", { name: "Remove first.png" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Submit" })).not.toBeInTheDocument();
    expect(input).toHaveFocus();
    expect(createUpload).not.toHaveBeenCalled();
    expect(authorizeUpload).not.toHaveBeenCalled();
    expect(completeUpload).not.toHaveBeenCalled();
    expect(cancelUpload).not.toHaveBeenCalled();
    expect(runAction).not.toHaveBeenCalled();
    await user.upload(input, png("second.png"));
    expect(screen.queryByRole("heading", { name: "Draft Canceled" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Remove second.png" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Submit" })).toBeEnabled();
  });

  it("returns home with an accessible confirmation only after the matching terminal cancellation receipt", async () => {
    const review = pendingReview();
    const result = deferred<DecodedResponse<CommandResponse>>();
    const runAction = vi.fn<ApiPort["runAction"]>().mockReturnValue(result.promise);
    const app = dependencies(review, { runAction });
    renderApp(app);
    await userEvent.click(await screen.findByRole("button", { name: "Cancel draft" }));
    expect(runAction).toHaveBeenCalledExactlyOnceWith(review, "cancel_job", expect.stringMatching(/^web:cancel_job:/u));
    expect(screen.queryByRole("heading", { name: "Draft Canceled" })).not.toBeInTheDocument();
    expect(route()).toBe(`/jobs/${review.job_id}`);
    await act(async () => { result.resolve(command(review, "cancelled", 2)); await Promise.resolve(); });
    await expectHomeConfirmation();
  });

  it("keeps cancel_requested pending through a stale terminal readback, then returns home on fresh authoritative cancellation", async () => {
    const review = pendingReview();
    const stale = canceledReview(review, 2);
    const fresh = canceledReview(review, 3);
    const getReview = vi.fn<ApiPort["getReview"]>()
      .mockResolvedValueOnce(response(review))
      .mockResolvedValueOnce(response(stale))
      .mockResolvedValue(response(fresh));
    const getJob = vi.fn<ApiPort["getJob"]>().mockResolvedValue(progress(fresh));
    const runAction = vi.fn<ApiPort["runAction"]>().mockResolvedValue(command(review, "cancel_requested", 3));
    renderApp(dependencies(review, { getReview, getJob, runAction }));
    await userEvent.click(await screen.findByRole("button", { name: "Cancel draft" }));
    await waitFor(() => expect(getReview).toHaveBeenCalledTimes(2));
    expect(route()).toBe(`/jobs/${review.job_id}`);
    expect(screen.queryByRole("heading", { name: "Draft Canceled" })).not.toBeInTheDocument();
    expect(screen.getByText(/Cancellation requested. We’re checking draft status before confirming it./u)).toBeInTheDocument();
    await act(async () => { fireEvent.focus(window); await Promise.resolve(); });
    await expectHomeConfirmation();
    expect(getJob).toHaveBeenCalledWith(review.job_id);
  });

  it("does not equate a fresh cancelling projection with terminal cancellation", async () => {
    const review = pendingReview();
    let latest = sellerReviewSchema.parse({ ...review, record_version: 2, display_state: "cancelling", stage: "cancellation" });
    const getReview = vi.fn<ApiPort["getReview"]>()
      .mockResolvedValueOnce(response(review))
      .mockImplementation(() => Promise.resolve(response(latest)));
    const getJob = vi.fn<ApiPort["getJob"]>().mockImplementation(() => Promise.resolve(progress(latest)));
    const runAction = vi.fn<ApiPort["runAction"]>().mockResolvedValue(command(review, "cancel_requested", 2));
    renderApp(dependencies(review, { getReview, getJob, runAction }));
    await userEvent.click(await screen.findByRole("button", { name: "Cancel draft" }));
    await waitFor(() => expect(getReview).toHaveBeenCalledTimes(2));
    expect(route()).toBe(`/jobs/${review.job_id}`);
    expect(screen.queryByRole("heading", { name: "Draft Canceled" })).not.toBeInTheDocument();
    latest = canceledReview(review, 3);
    await act(async () => { fireEvent.focus(window); await Promise.resolve(); });
    await expectHomeConfirmation();
  });

  it.each([
    ["rejected request", new ApiError(400, "INVALID_ACTION", "Cancellation unavailable.", "request-rejected", null)],
    ["server failure", new ApiError(503, "UNAVAILABLE", "Try later.", "request-server", null)],
    ["unknown network outcome", new TypeError("Connection lost")],
  ])("does not confirm or redirect after %s", async (_label, failure) => {
    const review = pendingReview();
    const runAction = vi.fn<ApiPort["runAction"]>().mockRejectedValue(failure);
    renderApp(dependencies(review, { runAction }));
    await userEvent.click(await screen.findByRole("button", { name: "Cancel draft" }));
    expect(await screen.findByText(/We couldn’t confirm cancellation/u)).toBeInTheDocument();
    expect(route()).toBe(`/jobs/${review.job_id}`);
    expect(screen.queryByRole("heading", { name: "Draft Canceled" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Cancel draft" })).toBeEnabled();
  });

  it.each(["wrong job", "older record", "different terminal state"] as const)("rejects a %s command receipt", async (kind) => {
    const review = pendingReview();
    const receipt = command(review, "cancelled", 2);
    if (kind === "wrong job") receipt.value.job_id = "job_unrelated";
    if (kind === "older record") receipt.value.record_version = 0;
    if (kind === "different terminal state") receipt.value.state = "approved";
    const runAction = vi.fn<ApiPort["runAction"]>().mockResolvedValue(receipt);
    renderApp(dependencies(review, { runAction }));
    await userEvent.click(await screen.findByRole("button", { name: "Cancel draft" }));
    expect(await screen.findByText(/We couldn’t confirm cancellation/u)).toBeInTheDocument();
    expect(route()).toBe(`/jobs/${review.job_id}`);
    expect(screen.queryByRole("heading", { name: "Draft Canceled" })).not.toBeInTheDocument();
  });

  it("ignores a delayed cancellation receipt after navigating to a different draft", async () => {
    const first = pendingReview();
    const second = pendingReview("job_second_draft");
    const result = deferred<DecodedResponse<CommandResponse>>();
    const getReview = vi.fn<ApiPort["getReview"]>().mockImplementation((id) => Promise.resolve(response(id === first.job_id ? first : second)));
    const runAction = vi.fn<ApiPort["runAction"]>().mockReturnValue(result.promise);
    renderApp(dependencies(first, { getReview, runAction }));
    await userEvent.click(await screen.findByRole("button", { name: "Cancel draft" }));
    await userEvent.click(screen.getByRole("button", { name: "Switch to next draft" }));
    await waitFor(() => expect(getReview).toHaveBeenCalledWith(second.job_id));
    expect(await screen.findByRole("button", { name: "Cancel draft" })).toBeEnabled();
    await act(async () => { result.resolve(command(first, "cancelled", 2)); await Promise.resolve(); });
    expect(route()).toBe(`/jobs/${second.job_id}`);
    expect(screen.queryByRole("heading", { name: "Draft Canceled" })).not.toBeInTheDocument();
  });

  it("ignores a delayed cancellation receipt after sign-out and does not leak its notice into a fresh session", async () => {
    const review = pendingReview();
    const result = deferred<DecodedResponse<CommandResponse>>();
    const app = dependencies(review, { runAction: vi.fn<ApiPort["runAction"]>().mockReturnValue(result.promise) });
    renderApp(app);
    await userEvent.click(await screen.findByRole("button", { name: "Cancel draft" }));
    await userEvent.click(screen.getByRole("button", { name: "Sign out" }));
    expect(await screen.findByRole("heading", { name: "Restore your seller session." })).toBeInTheDocument();
    await act(async () => { result.resolve(command(review, "cancelled", 2)); await Promise.resolve(); });
    expect(route()).toBe(`/jobs/${review.job_id}`);
    expect(screen.queryByRole("heading", { name: "Draft Canceled" })).not.toBeInTheDocument();
    await act(async () => { app.session.set("next-account-access", 3600, "next-account-refresh"); await Promise.resolve(); });
    await userEvent.click(await screen.findByRole("link", { name: "Dashboard" }));
    expect(await screen.findByRole("heading", { name: "Let’s start with your artwork." })).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "Draft Canceled" })).not.toBeInTheDocument();
  });

  it("does not resurrect the canceled draft when a delayed recent-list response contains its old state", async () => {
    const review = pendingReview();
    const recent = deferred<DecodedResponse<JobPage>>();
    const listJobs = vi.fn<ApiPort["listJobs"]>().mockReturnValue(recent.promise);
    const runAction = vi.fn<ApiPort["runAction"]>().mockResolvedValue(command(review, "cancelled", 2));
    renderApp(dependencies(review, { listJobs, runAction }));
    await userEvent.click(await screen.findByRole("button", { name: "Cancel draft" }));
    await expectHomeConfirmation();
    await act(async () => { recent.resolve({ value: { jobs: [{ job_id: review.job_id, state: "awaiting_approval", record_version: 1, review_version: 0, created_at: review.created_at, updated_at: review.updated_at }], next_cursor: null }, requestId: "stale-history", etag: null }); await Promise.resolve(); });
    expect(screen.queryByRole("link", { name: /Open draft:/u })).not.toBeInTheDocument();
    expect(document.querySelector(`a[href="/jobs/${review.job_id}"]`)).toBeNull();
    expect(screen.getByRole("heading", { name: "Draft Canceled" })).toBeInTheDocument();
    expect(route()).toBe("/");
  });

  it("keeps cancellation unavailable while an edited draft is saving", async () => {
    const review = editableReview();
    const save = deferred<DecodedResponse<CommandResponse>>();
    const reviseListing = vi.fn<ApiPort["reviseListing"]>().mockReturnValue(save.promise);
    const runAction = vi.fn<ApiPort["runAction"]>();
    renderApp(dependencies(review, { reviseListing, runAction }));
    const title = await screen.findByRole("textbox", { name: /^Title/u });
    fireEvent.change(title, { target: { value: "Revised botanical artwork" } });
    await userEvent.click(screen.getByRole("button", { name: "Save listing revision" }));
    await waitFor(() => expect(reviseListing).toHaveBeenCalledTimes(1));
    const cancel = screen.getByRole("button", { name: "Cancel draft" });
    expect(cancel).toBeDisabled();
    expect(screen.getByText("Wait for your changes to finish saving.")).toBeInTheDocument();
    fireEvent.click(cancel);
    expect(runAction).not.toHaveBeenCalled();
    expect(route()).toBe(`/jobs/${review.job_id}`);
    await act(async () => { save.reject(new TypeError("Save interrupted for test")); await Promise.resolve(); });
    await waitFor(() => expect(cancel).toBeEnabled());
  });
});

function pendingReview(jobId = "job_browser_fixture"): SellerReview {
  const source = sellerReviewSchema.parse(browserFixtures.seller_review_pending);
  return sellerReviewSchema.parse({ ...source, job_id: jobId, actions: source.actions.map((action) => action.action === "cancel_job" ? { ...action, enabled: true, reason: "AVAILABLE", message: "Cancel this draft." } : action) });
}

function editableReview(): SellerReview {
  const source = pendingReview();
  return sellerReviewSchema.parse({ ...source, record_version: 7, review_version: 2, review_fingerprint: "a".repeat(64), review_authority_etag: "a".repeat(64), display_state: "needs_revision", stage: "seller_revision",
    actions: source.actions.map((action) => action.action === "edit_listing" ? { ...action, enabled: true, reason: "AVAILABLE", message: "Edit this draft." } : action),
    listing: { ...source.listing, readiness: "ready", title: "Moonlit botanical shirt", description: "A carefully prepared botanical design.", tags: Array.from({ length: 13 }, (_, index) => `botanical ${index + 1}`), audience: ["Nature lovers"] },
    validation: { ...source.validation, readiness: "ready", passed: true, issues: [] },
  });
}

function canceledReview(review: SellerReview, recordVersion: number): SellerReview {
  return sellerReviewSchema.parse({ ...review, record_version: recordVersion, display_state: "cancelled", stage: "complete", actions: review.actions.map((action) => ({ ...action, enabled: false, reason: "NOT_IN_CURRENT_STATE", message: "Draft is canceled." })) });
}

function response(review: SellerReview): DecodedResponse<SellerReview> {
  return { value: review, requestId: "request-review", etag: review.review_authority_etag === null ? null : `"${review.review_authority_etag}"` };
}

function progress(review: SellerReview): DecodedResponse<JobProgress> {
  const { contract_version, job_id, record_version, review_version, display_state, stage, authority_notice, actions, failure, provider_outcome_unconfirmed, created_at, updated_at } = review;
  return { value: { contract_version, job_id, record_version, review_version, display_state, stage, authority_notice, actions, failure, provider_outcome_unconfirmed, created_at, updated_at }, requestId: "request-progress", etag: null };
}

function command(review: SellerReview, state: CommandResponse["state"], recordVersion: number): DecodedResponse<CommandResponse> {
  return { value: { job_id: review.job_id, state, record_version: recordVersion, review_version: review.review_version }, requestId: "request-cancel", etag: null };
}

function dependencies(review: SellerReview, overrides: Partial<ApiPort> = {}) {
  const unexpected = () => Promise.reject(new Error("Unexpected test API call"));
  const api: ApiPort = {
    listJobs: vi.fn<ApiPort["listJobs"]>().mockResolvedValue({ value: { jobs: [], next_cursor: null }, requestId: "request-jobs", etag: null }),
    clearRecentJobs: vi.fn(unexpected), getUpload: vi.fn(unexpected),
    getJob: vi.fn<ApiPort["getJob"]>().mockResolvedValue(progress(review)),
    getReview: vi.fn<ApiPort["getReview"]>().mockResolvedValue(response(review)),
    createUpload: vi.fn(unexpected), authorizeUpload: vi.fn(unexpected), completeUpload: vi.fn(unexpected), cancelUpload: vi.fn(unexpected),
    reviseListing: vi.fn(unexpected), runAction: vi.fn(unexpected), fetchArtwork: vi.fn(unexpected),
    ...overrides,
  };
  const session = new MemoryAuthSession();
  session.set("account-access", 3600, "account-refresh");
  const auth: AuthCoordinator = { session, startSignIn: unexpected, completeSignIn: unexpected, signOut: () => session.clear() };
  return { api, auth, session };
}

function renderApp(app: ReturnType<typeof dependencies>, initialPath = "/jobs/job_browser_fixture") {
  return render(<MemoryRouter initialEntries={[initialPath]}><AppRoutes dependencies={app} /><RouteProbe /></MemoryRouter>);
}

function RouteProbe() {
  const location = useLocation();
  const navigate = useNavigate();
  return <><output aria-label="Current route">{location.pathname}</output><button type="button" onClick={() => { void navigate("/jobs/job_second_draft"); }}>Switch to next draft</button></>;
}

function route() { return screen.getByLabelText("Current route").textContent; }

async function expectHomeConfirmation() {
  expect(await screen.findByRole("heading", { name: "Let’s start with your artwork." })).toBeInTheDocument();
  const heading = screen.getByRole("heading", { name: "Draft Canceled" });
  const notice = heading.closest('[role="status"]');
  expect(notice).not.toBeNull();
  expect(within(notice as HTMLElement).getByText("You’re ready to start a new draft below.")).toBeInTheDocument();
  expect(route()).toBe("/");
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason: Error) => void;
  const promise = new Promise<T>((done, fail) => { resolve = done; reject = fail; });
  return { promise, resolve, reject };
}

function png(name: string): File {
  return new File([new Uint8Array([137, 80, 78, 71, 13, 10, 26, 10, 1])], name, { type: "image/png" });
}
