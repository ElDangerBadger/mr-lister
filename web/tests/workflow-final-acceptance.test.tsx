import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import browserFixtures from "../../contracts/browser/phase6.5.fixtures.json";
import { AppRoutes } from "../src/App";
import type { ApiPort } from "../src/api/client";
import { MemoryAuthSession, type AuthCoordinator } from "../src/auth/session";
import { ActivityLog } from "../src/components/ActivityLog";
import { PreparationProgress } from "../src/components/PreparationProgress";
import { sellerReviewSchema, type SellerReview } from "../src/contracts";

afterEach(() => vi.useRealTimers());

describe("delayed preparation guidance", () => {
  it("waits 30 seconds across unchanged polling without manufacturing completed milestones", async () => {
    vi.useFakeTimers();
    const review = activeReview();
    const { rerender } = render(<PreparationProgress review={review} />);
    const milestones = screen.getByRole("region", { name: "Preparation milestones" });
    const initialStates = within(milestones).getAllByRole("listitem").map((item) => item.textContent);
    expect(screen.getByText(/Listing text becomes editable as it arrives/u)).toBeVisible();
    expect(screen.queryByText(/first preparation after a quiet period/u)).not.toBeInTheDocument();
    await act(async () => { await vi.advanceTimersByTimeAsync(29_999); });
    expect(screen.queryByText(/first preparation after a quiet period/u)).not.toBeInTheDocument();
    rerender(<PreparationProgress review={{ ...review, record_version: review.record_version + 1 }} />);
    await act(async () => { await vi.advanceTimersByTimeAsync(1); });
    expect(screen.getByText(/first preparation after a quiet period may take a little longer/u)).toBeVisible();
    expect(within(milestones).getAllByRole("listitem").map((item) => item.textContent)).toEqual(initialStates);
    expect(within(milestones).getAllByText("Complete")).toHaveLength(1);
    expect(screen.queryByRole("progressbar")).not.toBeInTheDocument();
  });

  it("cancels the timer on uncertainty, starts a fresh interval on recovery, and removes a visible hint if uncertainty returns", async () => {
    vi.useFakeTimers();
    const review = activeReview();
    const uncertain = { ...review, provider_outcome_unconfirmed: true };
    const { container, rerender } = render(<PreparationProgress review={review} />);
    await act(async () => { await vi.advanceTimersByTimeAsync(15_000); });
    rerender(<PreparationProgress review={uncertain} />);
    await act(async () => { await vi.advanceTimersByTimeAsync(60_000); });
    expect(screen.queryByText(/first preparation after a quiet period/u)).not.toBeInTheDocument();
    expect(container.querySelector('[aria-current="step"]')).toBeNull();
    expect(screen.getAllByText("Complete")).toHaveLength(1);
    rerender(<PreparationProgress review={review} />);
    await act(async () => { await vi.advanceTimersByTimeAsync(29_999); });
    expect(screen.queryByText(/first preparation after a quiet period/u)).not.toBeInTheDocument();
    await act(async () => { await vi.advanceTimersByTimeAsync(1); });
    expect(screen.getByText(/first preparation after a quiet period/u)).toBeVisible();
    rerender(<PreparationProgress review={uncertain} />);
    expect(screen.queryByText(/first preparation after a quiet period/u)).not.toBeInTheDocument();
    expect(container.querySelector('[aria-current="step"]')).toBeNull();
    expect(screen.getAllByText("Complete")).toHaveLength(1);
  });
});

describe("actionable session activity", () => {
  it("visibly distinguishes provider uncertainty and failure from ordinary preparation", async () => {
    const review = activeReview();
    const { container, rerender } = render(<ActivityLog review={review}>{null}</ActivityLog>);
    await userEvent.click(screen.getByText(/^Activity & details/u));
    const uncertain = { ...review, provider_outcome_unconfirmed: true };
    rerender(<ActivityLog review={uncertain}>{null}</ActivityLog>);
    expect(screen.getByText(/awaiting provider confirmation/u)).toBeVisible();
    expect(container.querySelectorAll(".activity-list > li")).toHaveLength(2);
    rerender(<ActivityLog review={{ ...uncertain }}>{null}</ActivityLog>);
    expect(container.querySelectorAll(".activity-list > li")).toHaveLength(2);
    const failed = sellerReviewSchema.parse({
      ...review, display_state: "retryable_failure", stage: "recovery",
      failure: {
        contract_version: "2.0.0", code: "PROVIDER_TEMPORARY_FAILURE",
        message: "The connected provider is temporarily unavailable.",
        stage: "product_sync", retryable: true, recovery: "retry_job",
      },
    });
    rerender(<ActivityLog review={failed}>{null}</ActivityLog>);
    const latestEntry = container.querySelector(".activity-list > li:last-child");
    expect(latestEntry).toHaveTextContent("The connected provider is temporarily unavailable.");
    expect(latestEntry).toBeVisible();
    expect(container.querySelectorAll(".activity-list > li")).toHaveLength(3);
  });
});

describe("secondary actions preserve listing-edit barriers", () => {
  it("blocks refresh inside More actions until dirty edits are deliberately discarded", async () => {
    const review = editableReview();
    const runAction = vi.fn<ApiPort["runAction"]>().mockRejectedValue(new TypeError("deliberate refresh request reached API"));
    renderReview(review, { runAction });
    const title = await screen.findByRole("textbox", { name: /^Title/u });
    const summary = screen.getByText("More actions");
    expect(summary.closest("details")).not.toHaveAttribute("open");
    expect(screen.getByText("Refresh estimate")).not.toBeVisible();
    await userEvent.click(summary);
    const refresh = screen.getByRole("button", { name: "Refresh estimate" });
    expect(refresh).toBeEnabled();
    fireEvent.change(title, { target: { value: "Unsaved seller title" } });
    expect(refresh).toBeDisabled();
    await userEvent.click(summary);
    await userEvent.click(summary);
    expect(screen.getByRole("textbox", { name: /^Title/u })).toHaveValue("Unsaved seller title");
    expect(refresh).toBeDisabled();
    await userEvent.click(refresh);
    expect(runAction).not.toHaveBeenCalled();
    await userEvent.click(screen.getByRole("button", { name: "Discard edits" }));
    expect(title).toHaveValue(review.listing.title);
    expect(refresh).toBeEnabled();
    await userEvent.click(refresh);
    await waitFor(() => expect(runAction).toHaveBeenCalledExactlyOnceWith(
      expect.objectContaining({ record_version: review.record_version, review_version: review.review_version, review_authority_etag: review.review_authority_etag }),
      "refresh_economics", expect.any(String),
    ));
  });

  it("keeps refresh blocked while saving and after acceptance until authoritative readback is current", async () => {
    const review = editableReview();
    const current = sellerReviewSchema.parse({
      ...review, record_version: 9, review_version: 3,
      review_fingerprint: "c".repeat(64), review_authority_etag: "d".repeat(64),
      listing: { ...review.listing, title: "Saved seller title" },
    });
    const save = deferred<Awaited<ReturnType<ApiPort["reviseListing"]>>>();
    const readback = deferred<Awaited<ReturnType<ApiPort["getReview"]>>>();
    const getReview = vi.fn<ApiPort["getReview"]>()
      .mockResolvedValueOnce(reviewResponse(review))
      .mockReturnValue(readback.promise);
    const reviseListing = vi.fn<ApiPort["reviseListing"]>().mockReturnValue(save.promise);
    const runAction = vi.fn<ApiPort["runAction"]>();
    renderReview(review, { getReview, reviseListing, runAction });
    const title = await screen.findByRole("textbox", { name: /^Title/u });
    await userEvent.click(screen.getByText("More actions"));
    const refresh = screen.getByRole("button", { name: "Refresh estimate" });
    expect(refresh).toBeEnabled();
    fireEvent.change(title, { target: { value: current.listing.title } });
    await userEvent.click(screen.getByRole("button", { name: "Save listing revision" }));
    expect(reviseListing).toHaveBeenCalledTimes(1);
    expect(title).toBeDisabled();
    expect(refresh).toBeDisabled();
    await userEvent.click(refresh);
    expect(runAction).not.toHaveBeenCalled();
    await act(async () => {
      save.resolve({
        value: { job_id: review.job_id, state: "product_draft_syncing", record_version: 8, review_version: 3 },
        requestId: "request-save", etag: null,
      });
      await save.promise;
    });
    await waitFor(() => expect(getReview).toHaveBeenCalledTimes(2));
    expect(title).toHaveAttribute("readonly");
    expect(refresh).toBeDisabled();
    await userEvent.click(refresh);
    expect(runAction).not.toHaveBeenCalled();
    await act(async () => {
      readback.resolve(reviewResponse(current));
      await readback.promise;
    });
    await screen.findByText(/Authoritative record 9 ·/u);
    expect(title).toHaveValue(current.listing.title);
    await waitFor(() => expect(refresh).toBeEnabled());
    expect(runAction).not.toHaveBeenCalled();
    expect(reviseListing).toHaveBeenCalledTimes(1);
  });
});

function activeReview(): SellerReview {
  return sellerReviewSchema.parse({ ...browserFixtures.seller_review_pending, display_state: "preparing", stage: "listing_validation" });
}

function editableReview(): SellerReview {
  const pending = activeReview();
  return sellerReviewSchema.parse({
    ...pending, record_version: 7, review_version: 2,
    review_fingerprint: "a".repeat(64), review_authority_etag: "b".repeat(64),
    display_state: "ready_for_review", stage: "human_review",
    listing: {
      ...pending.listing, readiness: "ready", title: "Original seller title", description: "Original seller description.",
      tags: Array.from({ length: 13 }, (_, index) => `botanical ${index + 1}`), audience: [],
    },
    validation: { ...pending.validation, readiness: "ready", passed: true, issues: [] },
    actions: pending.actions.map((action) => action.action === "edit_listing" || action.action === "refresh_economics"
      ? { ...action, enabled: true, reason: "AVAILABLE", message: "This action is available." }
      : action),
  });
}

function reviewResponse(review: SellerReview) {
  return { value: review, requestId: "request-review", etag: `"${review.review_authority_etag ?? ""}"` };
}

function deferred<T>() {
  let complete: ((value: T) => void) | undefined;
  const promise = new Promise<T>((resolve) => { complete = resolve; });
  return { promise, resolve: (value: T) => { if (complete === undefined) throw new Error("Deferred result was not initialized"); complete(value); } };
}

function renderReview(review: SellerReview, overrides: Partial<ApiPort>) {
  const session = new MemoryAuthSession();
  session.set("access-token", 3600, "refresh-token");
  const never = () => Promise.reject(new Error("Unexpected final-acceptance test request"));
  const api: ApiPort = {
    listJobs: never, getUpload: never,
    getJob: vi.fn().mockResolvedValue({ value: review, requestId: "request-progress", etag: null }),
    getReview: vi.fn().mockResolvedValue(reviewResponse(review)),
    createUpload: never, authorizeUpload: never, completeUpload: never, cancelUpload: never,
    reviseListing: never, runAction: never, fetchArtwork: never,
    ...overrides,
  };
  const auth: AuthCoordinator = { session, startSignIn: never, completeSignIn: never, signOut: vi.fn() };
  return render(<MemoryRouter initialEntries={[`/jobs/${review.job_id}`]}><AppRoutes dependencies={{ api, auth }} /></MemoryRouter>);
}
