import { StrictMode } from "react";
import { MemoryRouter } from "react-router-dom";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import browserFixtures from "../../contracts/browser/phase6.5.fixtures.json";
import { ActivityLog } from "../src/components/ActivityLog";
import { PreparationProgress } from "../src/components/PreparationProgress";
import { WorkflowSteps } from "../src/components/WorkflowSteps";
import { sellerReviewSchema, type SellerReview } from "../src/contracts";

describe("session-observed activity", () => {
  it("starts collapsed, explains its history limits, and records observation time once under StrictMode", async () => {
    const review = pendingReview();
    const openedAt = Date.now();
    const { container } = render(<StrictMode><ActivityLog review={review}><p>Preserved preparation evidence</p></ActivityLog></StrictMode>);
    const summary = screen.getByText(/^Activity & details/u);
    expect(summary.closest("details")).not.toHaveAttribute("open");
    expect(screen.getByText("Updates observed in this session")).toBeVisible();
    expect(screen.getByText("Preserved preparation evidence")).not.toBeVisible();
    const entries = container.querySelectorAll(".activity-list > li");
    expect(entries).toHaveLength(1);
    const observedAt = entries[0]?.querySelector("time")?.getAttribute("datetime");
    expect(observedAt).toBeDefined();
    expect(Date.parse(observedAt ?? "")).toBeGreaterThanOrEqual(openedAt);
    expect(observedAt).not.toBe(review.created_at);
    await userEvent.click(summary);
    expect(summary.closest("details")).toHaveAttribute("open");
    expect(screen.getByText(/not a full historical audit trail/u)).toBeVisible();
    expect(screen.getByText("Preserved preparation evidence")).toBeVisible();
  });

  it("deduplicates unchanged polling and records a meaningful stage change once", () => {
    const review = pendingReview();
    const { container, rerender } = render(<ActivityLog review={review}>{null}</ActivityLog>);
    const polled = sellerReviewSchema.parse({ ...review, record_version: review.record_version + 1, updated_at: "2026-08-22T12:05:00Z" });
    rerender(<ActivityLog review={polled}>{null}</ActivityLog>);
    expect(container.querySelectorAll(".activity-list > li")).toHaveLength(1);
    const syncing = sellerReviewSchema.parse({ ...polled, display_state: "synchronizing", stage: "product_sync" });
    rerender(<ActivityLog review={syncing}>{null}</ActivityLog>);
    rerender(<ActivityLog review={{ ...syncing }}>{null}</ActivityLog>);
    expect(container.querySelectorAll(".activity-list > li")).toHaveLength(2);
    expect(container.querySelector(".activity-list > li:last-child")).toHaveTextContent("synchronizing · product sync");
  });

  it("starts a fresh session log when the job-keyed review changes", () => {
    const first = sellerReviewSchema.parse({ ...pendingReview(), display_state: "synchronizing", stage: "product_sync" });
    const second = sellerReviewSchema.parse({ ...pendingReview(), job_id: "job_second_activity" });
    const { container, rerender } = render(<ActivityLog key={first.job_id} review={first}>{null}</ActivityLog>);
    rerender(<ActivityLog key={second.job_id} review={second}>{null}</ActivityLog>);
    expect(container.querySelectorAll(".activity-list > li")).toHaveLength(1);
    expect(container.querySelector(".activity-list")).not.toHaveTextContent("synchronizing · product sync");
    expect(screen.getByText(second.job_id)).toBeInTheDocument();
    expect(screen.queryByText(first.job_id)).not.toBeInTheDocument();
  });

  it("bounds a long session without counting record-only polling as activity", () => {
    const review = pendingReview();
    const { container, rerender } = render(<ActivityLog review={review}>{null}</ActivityLog>);
    for (let index = 0; index < 24; index += 1) {
      const next = sellerReviewSchema.parse({
        ...review, record_version: review.record_version + index + 1,
        display_state: index % 2 === 0 ? "synchronizing" : "preparing",
        stage: index % 2 === 0 ? "product_sync" : "listing_validation",
      });
      rerender(<ActivityLog review={next}>{null}</ActivityLog>);
    }
    expect(container.querySelectorAll(".activity-list > li")).toHaveLength(20);
  });
});

describe("accessible workflow and preparation status", () => {
  it("distinguishes the seller's current step from previously completed steps", () => {
    const { container, rerender } = render(<MemoryRouter><WorkflowSteps current="Review" /></MemoryRouter>);
    const navigation = screen.getByRole("navigation", { name: "Listing workflow" });
    expect(within(navigation).getAllByRole("listitem")).toHaveLength(3);
    expect(container.querySelectorAll('[aria-current="step"]')).toHaveLength(1);
    expect(container.querySelector('[aria-current="step"]')).toHaveTextContent("Review");
    expect(within(navigation).getByText("complete")).toBeInTheDocument();
    expect(within(navigation).getByRole("link", { name: "Upload artwork" })).toHaveAttribute("href", "/");
    rerender(<MemoryRouter><WorkflowSteps current="Publish" /></MemoryRouter>);
    expect(container.querySelectorAll('[aria-current="step"]')).toHaveLength(1);
    expect(container.querySelector('[aria-current="step"]')).toHaveTextContent("Publish");
    expect(within(navigation).getAllByText("complete")).toHaveLength(2);
  });

  it("expresses preparation status in text, and pauses the active marker when provider outcome is uncertain", () => {
    const review = sellerReviewSchema.parse({ ...pendingReview(), display_state: "preparing", stage: "listing_validation" });
    const { container, rerender } = render(<PreparationProgress review={review} />);
    const milestones = screen.getByRole("region", { name: "Preparation milestones" });
    const items = within(milestones).getAllByRole("listitem");
    expect(items).toHaveLength(4);
    expect(items[0]).toHaveTextContent("CompleteArtwork received");
    expect(items[1]).toHaveTextContent("In progressListing written");
    expect(items[1]).toHaveAttribute("aria-current", "step");
    expect(items[2]).toHaveTextContent("WaitingMockups prepared");
    expect(items[3]).toHaveTextContent("WaitingCosts checked");
    expect(screen.queryByRole("progressbar")).not.toBeInTheDocument();
    rerender(<PreparationProgress review={{ ...review, provider_outcome_unconfirmed: true }} />);
    expect(container.querySelector('[aria-current="step"]')).toBeNull();
    expect(within(milestones).queryByText("In progress")).not.toBeInTheDocument();
    expect(screen.queryByText(/first preparation after a quiet period/u)).not.toBeInTheDocument();
  });

  it.each(["retryable_failure", "terminal_failure", "cancelled", "approved"] as const)(
    "does not display active work or a waiting-time explanation for %s",
    (displayState) => {
      const review = sellerReviewSchema.parse({ ...pendingReview(), display_state: displayState, stage: "complete" });
      const { container } = render(<PreparationProgress review={review} />);
      expect(container.querySelector('[aria-current="step"]')).toBeNull();
      expect(screen.queryByText(/first preparation after a quiet period/u)).not.toBeInTheDocument();
      expect(screen.getAllByText("Complete")).toHaveLength(1);
    },
  );
});

function pendingReview(): SellerReview {
  return sellerReviewSchema.parse(browserFixtures.seller_review_pending);
}
