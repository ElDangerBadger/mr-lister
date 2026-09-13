import type { SellerReview } from "./contracts";

export interface PreparationMilestone {
  id: "artwork" | "listing" | "mockups" | "costs";
  label: string;
  state: "done" | "current" | "pending";
}

export function isPreparationActive(review: SellerReview): boolean {
  // A normal Printify upload/write is unconfirmed until its response arrives.
  // Ambiguous outcomes move to reconciliation, which remains inactive here.
  const productRequest = review.display_state === "synchronizing" && review.stage === "product_sync";
  return review.failure === null
    && (!review.provider_outcome_unconfirmed || productRequest)
    && ["preparing", "synchronizing", "refreshing_estimate"].includes(review.display_state);
}

export function preparationMilestones(review: SellerReview): PreparationMilestone[] {
  const active = isPreparationActive(review);
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
