import { describe, expect, it } from "vitest";
import browserFixtures from "../../contracts/browser/phase6.5.fixtures.json";
import { sellerReviewSchema, type SellerReview } from "../src/contracts";
import { preparationMilestones } from "../src/workflow";

describe("truthful preparation milestones", () => {
  it("confirms receipt without inventing finished artwork analysis or image availability", () => {
    const review = pendingReview();
    expect(review.preview.readiness).toBe("pending");
    const milestones = preparationMilestones(review);
    expect(milestones.map(({ id, label }) => ({ id, label }))).toEqual([
      { id: "artwork", label: "Artwork received" },
      { id: "listing", label: "Listing written" },
      { id: "mockups", label: "Mockups prepared" },
      { id: "costs", label: "Costs checked" },
    ]);
    expect(milestones.filter(({ state }) => state === "done").map(({ id }) => id)).toEqual(["artwork"]);
  });

  it("reports written copy without implying that its validation passed or approval is available", () => {
    const review = readyReview();
    const needsRevision = sellerReviewSchema.parse({
      ...review, display_state: "needs_revision", stage: "seller_revision",
      validation: { ...review.validation, readiness: "ready", passed: false, issues: [] },
    });
    expect(stateFor(needsRevision, "listing")).toBe("done");
    expect(preparationMilestones(needsRevision).some(({ state }) => state === "current")).toBe(false);
  });

  it("reports all confirmed current sections without a fabricated active step", () => {
    expect(preparationMilestones(readyReview()).map(({ state }) => state)).toEqual([
      "done", "done", "done", "done",
    ]);
  });

  it("removes mockup completion when a new review makes the synchronized version obsolete", () => {
    const review = readyReview();
    expect(stateFor(review, "mockups")).toBe("done");
    const next = sellerReviewSchema.parse({
      ...review, review_version: review.review_version + 1,
      display_state: "synchronizing", stage: "product_sync",
    });
    expect(stateFor(next, "mockups")).not.toBe("done");
    expect(stateFor(review, "mockups")).toBe("done");
  });

  it("does not count copied mockups as current when synchronization is outdated", () => {
    const review = readyReview();
    const next = sellerReviewSchema.parse({
      ...review, synchronization: { ...review.synchronization, readiness: "outdated" },
    });
    expect(stateFor(next, "mockups")).not.toBe("done");
  });

  it("does not count displayable but stale economics as checked", () => {
    const review = readyReview();
    const stale = sellerReviewSchema.parse({ ...review, economics: { ...review.economics, readiness: "stale" } });
    expect(stale.economics.minimum_cents).not.toBeNull();
    expect(stateFor(stale, "costs")).not.toBe("done");
  });

  it("marks the actual product preparation step current", () => {
    const review = readyReview();
    const pending = pendingReview();
    const syncing = sellerReviewSchema.parse({
      ...review, display_state: "synchronizing", stage: "product_sync",
      synchronization: pending.synchronization, mockups: pending.mockups, economics: pending.economics,
    });
    expect(preparationMilestones(syncing).filter(({ state }) => state === "current").map(({ id }) => id)).toEqual(["mockups"]);
    expect(stateFor(syncing, "costs")).toBe("pending");
  });

  it("marks cost calculation current only while that work is active", () => {
    const review = readyReview();
    const pending = pendingReview();
    const refreshing = sellerReviewSchema.parse({
      ...review, display_state: "refreshing_estimate", stage: "economics_refresh",
      economics: { ...pending.economics, readiness: "refreshing" },
    });
    expect(preparationMilestones(refreshing).filter(({ state }) => state === "current").map(({ id }) => id)).toEqual(["costs"]);
    const uncertain = sellerReviewSchema.parse({ ...refreshing, provider_outcome_unconfirmed: true });
    expect(preparationMilestones(uncertain).some(({ state }) => state === "current")).toBe(false);
  });

  it.each(["retryable_failure", "terminal_failure", "cancelled", "cancelling", "reconciling", "approved"] as const)(
    "does not imply ongoing preparation in %s or infer completion from a terminal stage",
    (displayState) => {
      const review = sellerReviewSchema.parse({ ...pendingReview(), display_state: displayState, stage: "complete" });
      expect(preparationMilestones(review).some(({ state }) => state === "current")).toBe(false);
      expect(preparationMilestones(review).filter(({ state }) => state === "done").map(({ id }) => id)).toEqual(["artwork"]);
    },
  );
});

function stateFor(review: SellerReview, id: string) {
  const milestone = preparationMilestones(review).find((item) => item.id === id);
  if (milestone === undefined) throw new Error(`Missing milestone ${id}`);
  return milestone.state;
}

function pendingReview(): SellerReview {
  return sellerReviewSchema.parse(browserFixtures.seller_review_pending);
}

function readyReview(): SellerReview {
  const pending = pendingReview();
  return sellerReviewSchema.parse({
    ...pending, record_version: 7, review_version: 2,
    review_fingerprint: "a".repeat(64), review_authority_etag: "b".repeat(64),
    display_state: "ready_for_review", stage: "human_review",
    listing: {
      ...pending.listing, readiness: "ready", title: "Botanical shirt", description: "Botanical illustration.",
      tags: Array.from({ length: 13 }, (_, index) => `botanical ${index + 1}`), audience: [],
    },
    validation: { ...pending.validation, readiness: "ready", passed: true, issues: [] },
    synchronization: {
      ...pending.synchronization, readiness: "ready", product_id: "printify_product_ready",
      synchronized_at: "2026-08-22T12:08:00Z", review_version: 2, editable_draft: true,
    },
    mockups: {
      ...pending.mockups, readiness: "ready",
      items: [{ contract_version: "2.0.0", url: "https://images.printify.com/review/front.png", alt_text: "Front representative mockup" }],
    },
    economics: {
      ...pending.economics, readiness: "ready", minimum_cents: 1299, maximum_cents: 1299,
      variants: [{
        contract_version: "2.0.0", color: "Black", size: "S", retail_price_cents: 2999, buyer_shipping_cents: 0,
        production_cost_cents: 900, production_shipping_cents: 500, marketplace_fees_cents: 300, estimated_proceeds_cents: 1299,
      }],
      calculated_at: "2026-08-22T12:10:00Z", fresh_until: "2026-08-22T13:10:00Z",
      production_cost_source: "Connected production product readback", production_cost_observed_at: "2026-08-22T12:09:00Z",
      production_shipping_source: "Connected production standard US shipping", production_shipping_observed_at: "2026-08-22T12:09:00Z",
      fee_policy_source: "Etsy US standard fee policy", fee_policy_id: "etsy-us-standard-v1", fee_policy_verified_on: "2026-08-22",
    },
  });
}
