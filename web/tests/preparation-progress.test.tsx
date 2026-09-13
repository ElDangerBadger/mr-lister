import { act, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import browserFixtures from "../../contracts/browser/phase6.5.fixtures.json";
import { PreparationProgress } from "../src/components/PreparationProgress";
import { sellerReviewSchema, type SellerReview } from "../src/contracts";

afterEach(() => vi.useRealTimers());

describe("preparation activity and elapsed time", () => {
  it("follows the actual stage while the elapsed clock survives polling and returning to the listing", async () => {
    vi.useFakeTimers();
    vi.setSystemTime("2026-09-12T10:00:16Z");
    const review = activeReview();
    const { container, rerender, unmount } = render(<PreparationProgress review={review} />);
    const status = screen.getByRole("status");
    expect(status).toHaveTextContent("Reviewing your artwork");
    expect(status).toHaveAttribute("aria-live", "polite");
    expect(screen.getByRole("timer")).toHaveAttribute("aria-live", "off");
    expect(screen.getByRole("timer", { name: "Elapsed time since submission" })).toHaveTextContent("Since submission 0:16");
    expect(container.querySelector('[aria-current="step"]')).toHaveTextContent("Listing written");

    await act(async () => { await vi.advanceTimersByTimeAsync(4_000); });
    expect(screen.getByRole("timer")).toHaveTextContent("Since submission 0:20");
    expect(status).not.toHaveTextContent("0:20");
    const writing = { ...review, stage: "listing_validation" as const, record_version: review.record_version + 1 };
    rerender(<PreparationProgress review={writing} />);
    expect(screen.getByRole("status")).toHaveTextContent("Writing and checking your listing");
    expect(screen.getByRole("timer")).toHaveTextContent("Since submission 0:20");

    const syncing = { ...writing, display_state: "synchronizing" as const, stage: "product_sync" as const, provider_outcome_unconfirmed: true };
    rerender(<PreparationProgress review={syncing} />);
    expect(screen.getByRole("status")).toHaveTextContent("Preparing your product previews");
    expect(container.querySelector('[aria-current="step"]')).toHaveTextContent("Mockups prepared");
    expect(container.querySelector(".milestone--current")).toHaveTextContent("In progress");
    expect(screen.getByRole("timer")).toHaveTextContent("Since submission 0:20");
    unmount();
    expect(vi.getTimerCount()).toBe(0);
    await act(async () => { await vi.advanceTimersByTimeAsync(15_000); });

    const refreshing = { ...syncing, display_state: "refreshing_estimate" as const, stage: "economics_refresh" as const, provider_outcome_unconfirmed: false };
    const remounted = render(<PreparationProgress review={refreshing} />);
    expect(screen.getByRole("status")).toHaveTextContent("Checking costs and shipping");
    expect(screen.getByRole("timer")).toHaveTextContent("Since submission 0:35");
    expect(remounted.container.querySelector('[aria-current="step"]')).toHaveTextContent("Costs checked");
    remounted.rerender(<PreparationProgress review={{ ...refreshing, display_state: "ready_for_review", stage: "human_review" }} />);
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
    expect(screen.queryByRole("timer")).not.toBeInTheDocument();
    expect(remounted.container.querySelector(".milestone--current")).toBeNull();
    expect(vi.getTimerCount()).toBe(0);
  });

  it.each(["needs_revision", "retryable_failure", "terminal_failure", "cancelled", "approved"] as const)(
    "stops the active indication and clock when preparation changes to %s",
    (displayState) => {
      vi.useFakeTimers();
      vi.setSystemTime("2026-09-12T10:00:24Z");
      const review = activeReview();
      const { container, rerender } = render(<PreparationProgress review={review} />);
      expect(screen.getByRole("timer")).toHaveTextContent("Since submission 0:24");
      rerender(<PreparationProgress review={{ ...review, display_state: displayState, stage: "complete" }} />);
      expect(screen.queryByRole("status")).not.toBeInTheDocument();
      expect(screen.queryByRole("timer")).not.toBeInTheDocument();
      expect(container.querySelector(".milestone--current")).toBeNull();
      expect(vi.getTimerCount()).toBe(0);
    },
  );

  it("pauses on an unconfirmed provider outcome and resumes without resetting elapsed time", async () => {
    vi.useFakeTimers();
    vi.setSystemTime("2026-09-12T10:00:24Z");
    const review = activeReview();
    const { rerender } = render(<PreparationProgress review={review} />);
    rerender(<PreparationProgress review={{ ...review, provider_outcome_unconfirmed: true }} />);
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
    expect(screen.queryByRole("timer")).not.toBeInTheDocument();
    expect(vi.getTimerCount()).toBe(0);
    await act(async () => { await vi.advanceTimersByTimeAsync(60_000); });
    rerender(<PreparationProgress review={review} />);
    expect(screen.getByRole("timer")).toHaveTextContent("Since submission 1:24");
  });

  it("clamps clock skew and formats hour-long elapsed durations without implying an ETA", () => {
    vi.useFakeTimers();
    vi.setSystemTime("2026-09-12T09:59:58Z");
    const review = activeReview();
    const { rerender } = render(<PreparationProgress review={review} />);
    expect(screen.getByRole("timer")).toHaveTextContent("Since submission 0:00");
    vi.setSystemTime("2026-09-12T11:02:03Z");
    rerender(<PreparationProgress review={{ ...review, job_id: "job_other_preparation" }} />);
    expect(screen.getByRole("timer")).toHaveTextContent("Since submission 1:02:03");
    expect(screen.queryByRole("progressbar")).not.toBeInTheDocument();
  });
});

function activeReview(): SellerReview {
  return sellerReviewSchema.parse({
    ...browserFixtures.seller_review_pending,
    created_at: "2026-09-12T10:00:00Z",
    display_state: "preparing", stage: "artwork_review",
  });
}
