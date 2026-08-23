import axe from "axe-core";
import { render, screen, waitFor, type RenderResult } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";
import browserFixtures from "../../contracts/browser/phase6.5.fixtures.json";
import type { ApiPort } from "../src/api/client";
import { AppRoutes } from "../src/App";
import { MemoryAuthSession, type AuthCoordinator } from "../src/auth/session";
import { sellerReviewSchema, type SellerReview } from "../src/contracts";

describe("accessible application states", () => {
  it("covers the signed-out protected-route restoration state", async () => {
    const result = renderApp("/jobs/job_browser_fixture", false);
    await screen.findByRole("heading", { name: "Restore your seller session." });
    await expectNoViolations(result);
  });

  it("covers the signed-in upload home", async () => {
    const listJobs = vi.fn().mockResolvedValue({ value: { jobs: [], next_cursor: null }, requestId: "request-jobs", etag: null });
    const result = renderApp("/", true, { listJobs });
    await screen.findByRole("heading", { name: "Start with one artwork." });
    await waitFor(() => expect(listJobs).toHaveBeenCalledTimes(1));
    await expectNoViolations(result);
  });

  it("covers owner-scoped upload recovery failure", async () => {
    const getUpload = vi.fn().mockRejectedValue(new TypeError("Recovery service unavailable"));
    const result = renderApp("/uploads/upload_browser_fixture", true, { getUpload });
    await screen.findByRole("alert");
    await expectNoViolations(result);
  });

  it("covers a pending preparation review", async () => {
    const review = sellerReviewSchema.parse(browserFixtures.seller_review_pending);
    const result = renderReview(review);
    await screen.findByRole("heading", { level: 1, name: "Listing preparation" });
    await expectNoViolations(result);
  });

  it("covers a retryable preparation failure", async () => {
    const base = sellerReviewSchema.parse(browserFixtures.seller_review_pending);
    const review = sellerReviewSchema.parse({
      ...base,
      display_state: "retryable_failure",
      stage: "recovery",
      actions: base.actions.map((action) => action.action === "retry_job"
        ? { ...action, enabled: true, reason: "AVAILABLE", message: "Retry the bounded preparation stage." }
        : action),
      failure: {
        contract_version: "2.0.0",
        code: "PROVIDER_TEMPORARY_FAILURE",
        message: "The connected provider is temporarily unavailable.",
        stage: "product_sync",
        retryable: true,
        recovery: "retry_job",
      },
    });
    const result = renderReview(review);
    await screen.findByRole("alert");
    await expectNoViolations(result);
  });

  it("covers a terminal read-only listing review", async () => {
    const base = sellerReviewSchema.parse(browserFixtures.seller_review_pending);
    const fingerprint = "d".repeat(64);
    const review = sellerReviewSchema.parse({
      ...base,
      record_version: 12,
      review_version: 4,
      review_fingerprint: fingerprint,
      review_authority_etag: fingerprint,
      display_state: "approved",
      stage: "complete",
      actions: base.actions.map((action) => ({
        ...action,
        enabled: false,
        reason: "NOT_IN_CURRENT_STATE",
        message: "This preparation is complete and read-only.",
      })),
      listing: {
        ...base.listing,
        readiness: "ready",
        title: "Approved botanical draft",
        description: "The preserved authoritative listing remains available for review and copying.",
        tags: Array.from({ length: 13 }, (_, index) => `approved ${index + 1}`),
        audience: ["Nature lovers"],
      },
      validation: { ...base.validation, readiness: "ready", passed: true, issues: [] },
      strands: {
        ...base.strands,
        readiness: "ready",
        framework: "strands-agents",
        agent_id: "mr-lister-preparation",
        prepared_review_version: 4,
        correlation_id: "d".repeat(24),
        tool_calls: ["record_prepared_review"],
        completed_at: "2026-08-22T12:00:00Z",
      },
    });
    const result = renderReview(review);
    const title = await screen.findByRole("textbox", { name: /^Title/u });
    expect(title).toHaveAttribute("readonly");
    expect(title).not.toBeDisabled();
    await expectNoViolations(result);
  });
});

function renderReview(review: SellerReview): RenderResult {
  return renderApp(`/jobs/${review.job_id}`, true, {
    getReview: vi.fn().mockResolvedValue({
      value: review,
      requestId: "request-review",
      etag: review.review_authority_etag === null ? null : `"${review.review_authority_etag}"`,
    }),
  });
}

function renderApp(route: string, authenticated: boolean, overrides: Partial<ApiPort> = {}): RenderResult {
  const session = new MemoryAuthSession();
  if (authenticated) session.set("access-token", 3600, "refresh-token");
  const never = () => Promise.reject(new Error("Unexpected accessibility test request"));
  const api: ApiPort = {
    listJobs: never,
    getJob: never,
    getUpload: never,
    getReview: never,
    createUpload: never,
    authorizeUpload: never,
    completeUpload: never,
    cancelUpload: never,
    reviseListing: never,
    runAction: never,
    fetchArtwork: never,
    ...overrides,
  };
  const auth: AuthCoordinator = {
    session,
    startSignIn: never,
    completeSignIn: never,
    signOut: vi.fn(),
  };
  return render(
    <MemoryRouter initialEntries={[route]}>
      <AppRoutes dependencies={{ api, auth }} />
    </MemoryRouter>,
  );
}

async function expectNoViolations({ container }: RenderResult): Promise<void> {
  const results = await axe.run(container, { rules: { "color-contrast": { enabled: false } } });
  expect(results.violations).toEqual([]);
}
