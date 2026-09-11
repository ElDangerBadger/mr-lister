import type { SellerReview } from "./contracts";

export interface PreparationMilestone {
  id: "artwork" | "listing" | "mockups" | "costs";
  label: string;
  state: "done" | "current" | "pending";
}

export function preparationMilestones(review: SellerReview): PreparationMilestone[] {
  const active = !review.provider_outcome_unconfirmed
    && ["preparing", "synchronizing", "refreshing_estimate"].includes(review.display_state);
  const listing = review.listing.readiness === "ready";
  const mockups = review.mockups.readiness === "ready"
    && review.synchronization.readiness === "ready"
    && review.synchronization.review_version === review.review_version;
  const costs = review.economics.readiness === "ready";
  return [
    { id: "artwork", label: "Artwork received", state: "done" },
    { id: "listing", label: "Listing written", state: listing ? "done" : active && ["upload_verified", "artwork_review", "listing_validation"].includes(review.stage) ? "current" : "pending" },
    { id: "mockups", label: "Mockups prepared", state: mockups ? "done" : active && review.stage === "product_sync" ? "current" : "pending" },
    { id: "costs", label: "Costs checked", state: costs ? "done" : active && review.stage === "economics_refresh" ? "current" : "pending" },
  ];
}
