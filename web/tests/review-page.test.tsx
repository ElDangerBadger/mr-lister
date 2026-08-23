import axe from "axe-core";
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, useNavigate } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";
import browserFixtures from "../../contracts/browser/phase6.5.fixtures.json";
import type { ApiPort } from "../src/api/client";
import { AppRoutes } from "../src/App";
import { MemoryAuthSession, type AuthCoordinator } from "../src/auth/session";
import { sellerReviewSchema, type SellerReview } from "../src/contracts";

describe("authoritative seller review", () => {
  it("makes the unpublished and Strands boundaries prominent and exposes exactly 13 labeled tags", async () => {
    const review = readyReview();
    render(<MemoryRouter initialEntries={[`/jobs/${review.job_id}`]}><AppRoutes dependencies={dependencies(review)} /></MemoryRouter>);
    expect(await screen.findByRole("heading", { name: review.listing.title ?? "" })).toBeInTheDocument();
    expect(screen.getAllByText("Unpublished — not on Etsy").length).toBeGreaterThan(0);
    expect(screen.getByRole("heading", { name: "Prepared with Strands Agents" })).toBeInTheDocument();
    expect(screen.getAllByText("record_prepared_review").length).toBeGreaterThan(0);
    expect(screen.getByText("a".repeat(24))).toBeInTheDocument();
    expect(screen.getByText("Review exact print placements")).toBeInTheDocument();
    expect(await screen.findAllByRole("textbox", { name: /^Tag \d+$/u })).toHaveLength(13);
    expect(await screen.findByText("Nature lovers")).toBeInTheDocument();
    expect(screen.getByText("Validation: Passed")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /publish|order|fulfill/iu })).not.toBeInTheDocument();
  });

  it("shows pending deterministic validation before listing content exists", async () => {
    const review = sellerReviewSchema.parse(browserFixtures.seller_review_pending);
    render(<MemoryRouter initialEntries={[`/jobs/${review.job_id}`]}><AppRoutes dependencies={dependencies(review)} /></MemoryRouter>);
    expect(await screen.findByText("Validation: Pending")).toBeInTheDocument();
  });

  it("renders the complete ready review without dropping approval evidence", async () => {
    const review = completeReadyReview();
    const fetchArtwork = vi.fn().mockResolvedValue(new Blob(["png"], { type: "image/png" }));
    render(<MemoryRouter initialEntries={[`/jobs/${review.job_id}`]}><AppRoutes dependencies={dependencies(review, { fetchArtwork })} /></MemoryRouter>);
    await screen.findByRole("heading", { name: review.listing.title ?? "" });
    expect(screen.getByText("c".repeat(24))).toBeInTheDocument();
    expect(await screen.findByText("Nature lovers")).toBeInTheDocument();
    expect(screen.getByText("printify_product_ready")).toBeInTheDocument();
    expect(screen.getByText("Synchronized at").parentElement).toHaveTextContent("2026");
    await userEvent.click(screen.getByText("Review exact print placements"));
    expect(screen.getByText("placement_large")).toBeInTheDocument();
    expect(screen.getByText("0.5 / 0.12")).toBeInTheDocument();
    expect(screen.getByText("0.72 / 0°")).toBeInTheDocument();
    await userEvent.click(screen.getByText("Review all 30 variant estimates"));
    const table = screen.getByRole("table", { name: /Estimated proceeds by product color and size/u });
    expect(within(table).getAllByRole("row")).toHaveLength(31);
    expect(screen.getByText("$12.70–$12.99")).toBeInTheDocument();
    expect(screen.getByText("Connected production product readback")).toBeInTheDocument();
    expect(screen.getByText("Connected production standard US shipping")).toBeInTheDocument();
    expect(screen.getByText(/Etsy US standard fee policy · etsy-us-standard-v1/u)).toBeInTheDocument();
    expect(screen.getByText("Calculated at").parentElement).toHaveTextContent("2026");
    expect(screen.getByText("Production cost observed").parentElement).toHaveTextContent("2026");
    expect(screen.getByText("Shipping observed").parentElement).toHaveTextContent("2026");
    expect(screen.getByText("Fresh until").parentElement).toHaveTextContent("2026");
    expect(screen.getByText("Provider prices can change before a later order.")).toBeInTheDocument();
    expect(screen.getAllByRole("img", { name: /representative mockup/u })).toHaveLength(2);
  });

  it("puts the skip link first in the keyboard focus order", async () => {
    const review = readyReview();
    render(<MemoryRouter initialEntries={[`/jobs/${review.job_id}`]}><AppRoutes dependencies={dependencies(review)} /></MemoryRouter>);
    const user = userEvent.setup();
    await user.tab();
    expect(screen.getByRole("link", { name: "Skip to main content" })).toHaveFocus();
  });

  it("has no automatically detectable accessibility violations in the ready review", async () => {
    const review = readyReview();
    const { container } = render(<MemoryRouter initialEntries={[`/jobs/${review.job_id}`]}><AppRoutes dependencies={dependencies(review)} /></MemoryRouter>);
    await screen.findByRole("heading", { name: review.listing.title ?? "" });
    await screen.findByRole("textbox", { name: "Tag 13" });
    const results = await axe.run(container, { rules: { "color-contrast": { enabled: false } } });
    expect(results.violations).toEqual([]);
  });

  it("binds projected validation errors to the exact listing field", async () => {
    const base = readyReview();
    const review = sellerReviewSchema.parse({
      ...base,
      validation: {
        ...base.validation,
        passed: false,
        issues: [{ contract_version: "2.0.0", code: "TAG_REPETITION", path: "tags[2]", severity: "error", message: "Replace this repeated tag." }],
      },
    });
    render(<MemoryRouter initialEntries={[`/jobs/${review.job_id}`]}><AppRoutes dependencies={dependencies(review)} /></MemoryRouter>);
    const tag = await screen.findByRole("textbox", { name: "Tag 3" });
    expect(screen.getByText("Validation: Needs revision")).toBeInTheDocument();
    expect(tag).toHaveAttribute("aria-invalid", "true");
    expect(tag).toHaveAccessibleDescription("Replace this repeated tag.");
  });

  it("uses a new listing key only when the authority-bound payload changes", async () => {
    const review = readyReview();
    const calls: Array<{ title: string; key: string }> = [];
    const reviseListing = vi.fn().mockImplementation((_review: SellerReview, listing: { title: string }, key: string) => {
      calls.push({ title: listing.title, key });
      return Promise.reject(new TypeError("network interrupted"));
    });
    render(<MemoryRouter initialEntries={[`/jobs/${review.job_id}`]}><AppRoutes dependencies={dependencies(review, { reviseListing })} /></MemoryRouter>);
    const user = userEvent.setup();
    const title = await screen.findByRole("textbox", { name: /^Title/u });
    await user.clear(title);
    await user.type(title, "First exact title");
    await user.click(screen.getByRole("button", { name: "Save listing revision" }));
    await screen.findByText("network interrupted");
    await user.clear(title);
    await user.type(title, "Second exact title");
    await user.click(screen.getByRole("button", { name: "Save listing revision" }));
    await screen.findByText("network interrupted");
    await user.click(screen.getByRole("button", { name: "Save listing revision" }));
    expect(calls).toHaveLength(3);
    expect(calls[0]?.key).not.toBe(calls[1]?.key);
    expect(calls[1]?.key).toBe(calls[2]?.key);
  });

  it("focuses the committed validation summary after the first invalid submit", async () => {
    const review = readyReview();
    render(<MemoryRouter initialEntries={[`/jobs/${review.job_id}`]}><AppRoutes dependencies={dependencies(review)} /></MemoryRouter>);
    const user = userEvent.setup();
    const title = await screen.findByRole("textbox", { name: /^Title/u });
    await user.clear(title);
    await user.click(screen.getByRole("button", { name: "Save listing revision" }));
    await waitFor(() => expect(document.getElementById("listing-errors")).toHaveFocus());
    expect(title).toHaveAttribute("aria-invalid", "true");
  });

  it("preserves an exact protected route through managed-session recovery", async () => {
    const session = new MemoryAuthSession();
    const startSignIn = vi.fn().mockResolvedValue(undefined);
    const auth: AuthCoordinator = { session, startSignIn, completeSignIn: vi.fn(), signOut: vi.fn() };
    const user = userEvent.setup();
    render(<MemoryRouter initialEntries={["/jobs/job_browser_fixture"]}><AppRoutes dependencies={{ api: dependencies(readyReview()).api, auth }} /></MemoryRouter>);
    await user.click(screen.getByRole("button", { name: "Continue securely" }));
    expect(startSignIn).toHaveBeenCalledWith("/jobs/job_browser_fixture");
  });

  it("moves route focus without turning polling updates into navigation announcements", async () => {
    const review = readyReview();
    const listJobs = vi.fn().mockResolvedValue({ value: { jobs: [], next_cursor: null }, requestId: "request-jobs", etag: null });
    render(<MemoryRouter initialEntries={[`/jobs/${review.job_id}`]}><AppRoutes dependencies={dependencies(review, { listJobs })} /></MemoryRouter>);
    const user = userEvent.setup();
    await screen.findByRole("heading", { name: review.listing.title ?? "" });
    await user.click(screen.getByRole("link", { name: "Mr. Lister seller review home" }));
    await waitFor(() => expect(document.getElementById("main-content")).toHaveFocus());
    expect(document.title).toBe("Uploads | Mr. Lister");
  });

  it("fails closed while a same-route job identifier changes", async () => {
    const first = readyReview();
    const second = sellerReviewSchema.parse({ ...first, job_id: "job_second", listing: { ...first.listing, title: "Second listing" } });
    let resolveSecond: ((value: { value: SellerReview; requestId: string; etag: string }) => void) | undefined;
    const secondResponse = new Promise<{ value: SellerReview; requestId: string; etag: string }>((resolve) => { resolveSecond = resolve; });
    const getReview = vi.fn().mockImplementation((jobId: string) => jobId === first.job_id
      ? Promise.resolve({ value: first, requestId: "request-first", etag: `"${first.review_authority_etag ?? ""}"` })
      : secondResponse);
    render(
      <MemoryRouter initialEntries={[`/jobs/${first.job_id}`]}>
        <NavigateToSecond />
        <AppRoutes dependencies={dependencies(first, { getReview })} />
      </MemoryRouter>,
    );
    const user = userEvent.setup();
    await screen.findByRole("heading", { name: first.listing.title ?? "" });
    await user.click(screen.getByRole("button", { name: "Open second route" }));
    expect(screen.queryByRole("heading", { name: first.listing.title ?? "" })).not.toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Preparing your review…" })).toBeInTheDocument();
    resolveSecond?.({ value: second, requestId: "request-second", etag: `"${second.review_authority_etag ?? ""}"` });
    expect(await screen.findByRole("heading", { name: "Second listing" })).toBeInTheDocument();
  });

  it("ignores delayed progress from job A after job B has become authoritative", async () => {
    const first = readyReview();
    const second = sellerReviewSchema.parse({ ...first, job_id: "job_second", listing: { ...first.listing, title: "Second listing" } });
    const staleProgress = {
      value: {
        contract_version: "2.0.0" as const,
        job_id: first.job_id,
        record_version: 99,
        review_version: first.review_version,
        display_state: "preparing" as const,
        stage: "product_sync" as const,
        authority_notice: first.authority_notice,
        actions: first.actions,
        failure: null,
        provider_outcome_unconfirmed: false,
        created_at: first.created_at,
        updated_at: first.updated_at,
      },
      requestId: "request-stale-progress",
      etag: null,
    };
    let resolveProgress: ((value: typeof staleProgress) => void) | undefined;
    const progress = new Promise<typeof staleProgress>((resolve) => { resolveProgress = resolve; });
    const getJob = vi.fn().mockReturnValue(progress);
    const getReview = vi.fn().mockImplementation((jobId: string) => Promise.resolve({
      value: jobId === first.job_id ? first : second,
      requestId: jobId === first.job_id ? "request-first" : "request-second",
      etag: `"${first.review_authority_etag ?? ""}"`,
    }));
    render(
      <MemoryRouter initialEntries={[`/jobs/${first.job_id}`]}>
        <NavigateToSecond />
        <AppRoutes dependencies={dependencies(first, { getJob, getReview })} />
      </MemoryRouter>,
    );
    const user = userEvent.setup();
    await screen.findByRole("heading", { name: first.listing.title ?? "" });
    window.dispatchEvent(new Event("focus"));
    await waitFor(() => expect(getJob).toHaveBeenCalledWith(first.job_id));
    await user.click(screen.getByRole("button", { name: "Open second route" }));
    expect(await screen.findByRole("heading", { name: "Second listing" })).toBeInTheDocument();
    await act(async () => {
      resolveProgress?.(staleProgress);
      await Promise.resolve();
    });
    expect(getReview).toHaveBeenCalledTimes(2);
    expect(getReview).toHaveBeenNthCalledWith(1, first.job_id);
    expect(getReview).toHaveBeenNthCalledWith(2, second.job_id);
    expect(screen.getByRole("heading", { name: "Second listing" })).toBeInTheDocument();
  });

  it("preserves an accepted listing draft when authoritative readback fails", async () => {
    const review = readyReview();
    const getReview = vi.fn()
      .mockResolvedValueOnce({ value: review, requestId: "request-review", etag: `"${review.review_authority_etag ?? ""}"` })
      .mockRejectedValueOnce(new TypeError("readback interrupted"));
    const reviseListing = vi.fn().mockResolvedValue({
      value: { job_id: review.job_id, state: "needs_revision", record_version: 8, review_version: 3 },
      requestId: "request-save",
      etag: null,
    });
    render(<MemoryRouter initialEntries={[`/jobs/${review.job_id}`]}><AppRoutes dependencies={dependencies(review, { getReview, reviseListing })} /></MemoryRouter>);
    const user = userEvent.setup();
    const title = await screen.findByRole("textbox", { name: /^Title/u });
    await user.clear(title);
    await user.type(title, "Accepted local revision");
    await user.click(screen.getByRole("button", { name: "Save listing revision" }));
    expect(await screen.findByText(/accepted text remains visible/u)).toBeInTheDocument();
    expect(title).toHaveValue("Accepted local revision");
    expect(screen.getByRole("button", { name: "Refresh authoritative review" })).toBeInTheDocument();
    expect(reviseListing).toHaveBeenCalledTimes(1);
  });

  it("preserves a dirty draft across polling authority changes until deliberate reapplication", async () => {
    const original = readyReview();
    const fingerprint = "b".repeat(64);
    const latest = sellerReviewSchema.parse({
      ...original,
      record_version: 8,
      review_version: 3,
      review_fingerprint: fingerprint,
      review_authority_etag: fingerprint,
      updated_at: "2026-08-22T12:05:00Z",
      listing: { ...original.listing, title: "New authoritative server title" },
      strands: { ...original.strands, prepared_review_version: 3 },
    });
    const getReview = vi.fn()
      .mockResolvedValueOnce(reviewResponse(original, "request-original"))
      .mockResolvedValueOnce(reviewResponse(latest, "request-latest"));
    const getJob = vi.fn().mockResolvedValue(progressResponse(latest));
    const reviseListing = vi.fn().mockRejectedValue(new TypeError("submission interrupted"));
    render(<MemoryRouter initialEntries={[`/jobs/${original.job_id}`]}><AppRoutes dependencies={dependencies(original, { getReview, getJob, reviseListing })} /></MemoryRouter>);
    const user = userEvent.setup();
    const title = await screen.findByRole("textbox", { name: /^Title/u });
    await user.clear(title);
    await user.type(title, "Preserved local revision");
    window.dispatchEvent(new Event("focus"));
    expect(await screen.findByText(/newer authoritative review is available/u)).toBeInTheDocument();
    expect(title).toHaveValue("Preserved local revision");
    expect(screen.getByRole("button", { name: "Save listing revision" })).toBeDisabled();
    expect(reviseListing).not.toHaveBeenCalled();
    await user.click(screen.getByRole("button", { name: "Reapply revision to latest review" }));
    await user.click(screen.getByRole("button", { name: "Save listing revision" }));
    await screen.findByText("submission interrupted");
    expect(reviseListing).toHaveBeenCalledTimes(1);
    expect(reviseListing).toHaveBeenCalledWith(
      expect.objectContaining({ record_version: 8, review_version: 3, review_authority_etag: fingerprint }),
      expect.objectContaining({ title: "Preserved local revision" }),
      expect.any(String),
    );
  });

  it("reconciles an exact authoritative draft after a lost save response without resubmitting it", async () => {
    const original = readyReview();
    const acceptedTitle = "Accepted despite lost response";
    const fingerprint = "e".repeat(64);
    const latest = sellerReviewSchema.parse({
      ...original,
      record_version: 8,
      review_version: 3,
      review_fingerprint: fingerprint,
      review_authority_etag: fingerprint,
      listing: { ...original.listing, title: acceptedTitle },
      strands: { ...original.strands, prepared_review_version: 3 },
    });
    const getReview = vi.fn()
      .mockResolvedValueOnce(reviewResponse(original, "request-original"))
      .mockResolvedValueOnce(reviewResponse(latest, "request-latest"));
    const getJob = vi.fn().mockResolvedValue(progressResponse(latest));
    const keys: string[] = [];
    const reviseListing = vi.fn().mockImplementation((_review: SellerReview, _listing: unknown, key: string) => {
      keys.push(key);
      return Promise.reject(new TypeError("response lost"));
    });
    render(<MemoryRouter initialEntries={[`/jobs/${original.job_id}`]}><AppRoutes dependencies={dependencies(original, { getReview, getJob, reviseListing })} /></MemoryRouter>);
    const user = userEvent.setup();
    const title = await screen.findByRole("textbox", { name: /^Title/u });
    await user.clear(title);
    await user.type(title, acceptedTitle);
    await user.click(screen.getByRole("button", { name: "Save listing revision" }));
    await screen.findByText("response lost");
    window.dispatchEvent(new Event("focus"));
    expect(await screen.findByText(/contains this exact revision/u)).toBeInTheDocument();
    expect(title).toHaveValue(acceptedTitle);
    expect(reviseListing).toHaveBeenCalledTimes(1);
    expect(screen.queryByRole("button", { name: "Reapply revision to latest review" })).not.toBeInTheDocument();
    await user.clear(title);
    await user.type(title, "A distinct follow-up revision");
    await user.click(screen.getByRole("button", { name: "Save listing revision" }));
    await waitFor(() => expect(reviseListing).toHaveBeenCalledTimes(2));
    expect(reviseListing.mock.calls[1]?.[0]).toEqual(expect.objectContaining({ record_version: 8, review_version: 3 }));
    expect(keys[1]).not.toBe(keys[0]);
  });

  it("refreshes human-review status on focus through the lightweight progress route", async () => {
    const base = readyReview();
    const review = sellerReviewSchema.parse({ ...base, display_state: "ready_for_review", stage: "human_review" });
    const getReview = vi.fn().mockResolvedValue({ value: review, requestId: "request-review", etag: `"${review.review_authority_etag ?? ""}"` });
    const getJob = vi.fn().mockResolvedValue({
      value: {
        contract_version: "2.0.0",
        job_id: review.job_id,
        record_version: review.record_version,
        review_version: review.review_version,
        display_state: review.display_state,
        stage: review.stage,
        authority_notice: review.authority_notice,
        actions: review.actions,
        failure: review.failure,
        provider_outcome_unconfirmed: review.provider_outcome_unconfirmed,
        created_at: review.created_at,
        updated_at: review.updated_at,
      },
      requestId: "request-progress",
      etag: null,
    });
    render(<MemoryRouter initialEntries={[`/jobs/${review.job_id}`]}><AppRoutes dependencies={dependencies(review, { getReview, getJob })} /></MemoryRouter>);
    await screen.findByRole("heading", { name: review.listing.title ?? "" });
    window.dispatchEvent(new Event("focus"));
    await waitFor(() => expect(getJob).toHaveBeenCalledTimes(1));
    expect(getReview).toHaveBeenCalledTimes(1);
  });

  it("does not start a review read after in-flight progress resolves post-unmount", async () => {
    const review = sellerReviewSchema.parse({ ...readyReview(), display_state: "ready_for_review", stage: "human_review" });
    let resolveProgress: ((value: ReturnType<typeof progressResponse>) => void) | undefined;
    const progress = new Promise<ReturnType<typeof progressResponse>>((resolve) => { resolveProgress = resolve; });
    const getJob = vi.fn().mockReturnValue(progress);
    const getReview = vi.fn().mockResolvedValue(reviewResponse(review, "request-review"));
    const { unmount } = render(<MemoryRouter initialEntries={[`/jobs/${review.job_id}`]}><AppRoutes dependencies={dependencies(review, { getJob, getReview })} /></MemoryRouter>);
    await screen.findByRole("heading", { name: review.listing.title ?? "" });
    window.dispatchEvent(new Event("focus"));
    await waitFor(() => expect(getJob).toHaveBeenCalledTimes(1));
    unmount();
    const changed = sellerReviewSchema.parse({ ...review, record_version: review.record_version + 1 });
    await act(async () => {
      resolveProgress?.(progressResponse(changed));
      await Promise.resolve();
    });
    expect(getReview).toHaveBeenCalledTimes(1);
  });

  it("locks stale action controls after acceptance until readback is current", async () => {
    const base = readyReview();
    const review = sellerReviewSchema.parse({
      ...base,
      display_state: "ready_for_review",
      stage: "human_review",
      preview: {
        ...base.preview,
        readiness: "ready",
        url: `${window.location.origin}/v1/jobs/${base.job_id}/artwork-preview`,
        expires_at: new Date(Date.now() + 5 * 60_000).toISOString(),
      },
      mockups: {
        ...base.mockups,
        readiness: "ready",
        items: [{ contract_version: "2.0.0", url: "https://images.printify.com/review/front.png", alt_text: "Front representative mockup" }],
      },
      actions: base.actions.map((item) => item.action === "approve_review"
        ? { ...item, enabled: true, reason: "AVAILABLE", message: "Approve this exact review." }
        : item),
    });
    const getReview = vi.fn()
      .mockResolvedValueOnce({ value: review, requestId: "request-review", etag: `"${review.review_authority_etag ?? ""}"` })
      .mockRejectedValueOnce(new TypeError("readback interrupted"));
    let resolveAction: ((value: Awaited<ReturnType<ApiPort["runAction"]>>) => void) | undefined;
    const actionResponse = new Promise<Awaited<ReturnType<ApiPort["runAction"]>>>((resolve) => { resolveAction = resolve; });
    const runAction = vi.fn().mockReturnValue(actionResponse);
    const fetchArtwork = vi.fn().mockResolvedValue(new Blob(["png"], { type: "image/png" }));
    render(<MemoryRouter initialEntries={[`/jobs/${review.job_id}`]}><AppRoutes dependencies={dependencies(review, { getReview, runAction, fetchArtwork })} /></MemoryRouter>);
    const user = userEvent.setup();
    fireEvent.load(await screen.findByRole("img", { name: "Original uploaded artwork for this seller review" }));
    fireEvent.load(screen.getByRole("img", { name: "Front representative mockup" }));
    await waitFor(() => expect(screen.getByRole("button", { name: "Approve draft" })).toBeEnabled());
    await user.click(await screen.findByRole("button", { name: "Approve draft" }));
    await user.click(screen.getByRole("button", { name: "Approve draft — keep unpublished" }));
    const pendingApproval = await screen.findByRole("button", { name: "Approving…" });
    expect(pendingApproval).toBeDisabled();
    expect(screen.getByRole("button", { name: "Go back" })).toBeDisabled();
    await user.click(pendingApproval);
    expect(runAction).toHaveBeenCalledTimes(1);
    await act(async () => {
      resolveAction?.({
        value: { job_id: review.job_id, state: "approved", record_version: 8, review_version: 2 },
        requestId: "request-approve",
        etag: null,
      });
      await actionResponse;
    });
    const acceptedStatus = await screen.findByText(/latest status is unavailable/u);
    expect(acceptedStatus).toBeInTheDocument();
    await waitFor(() => expect(acceptedStatus).toHaveFocus());
    expect(screen.getByRole("button", { name: "Approve draft" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Refresh current status" })).toBeInTheDocument();
    expect(runAction).toHaveBeenCalledTimes(1);
  });

  it("retries a failed authenticated artwork preview and revokes its Blob URL", async () => {
    const base = readyReview();
    const review = sellerReviewSchema.parse({
      ...base,
      preview: {
        ...base.preview,
        readiness: "ready",
        url: `${window.location.origin}/v1/jobs/${base.job_id}/artwork-preview`,
        expires_at: new Date(Date.now() + 5 * 60_000).toISOString(),
      },
    });
    const fetchArtwork = vi.fn()
      .mockRejectedValueOnce(new TypeError("preview interrupted"))
      .mockResolvedValueOnce(new Blob(["png"], { type: "image/png" }));
    const revoke = vi.spyOn(URL, "revokeObjectURL");
    const { unmount } = render(<MemoryRouter initialEntries={[`/jobs/${review.job_id}`]}><AppRoutes dependencies={dependencies(review, { fetchArtwork })} /></MemoryRouter>);
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "Retry artwork preview" }));
    expect(await screen.findByRole("img", { name: "Original uploaded artwork for this seller review" })).toBeInTheDocument();
    expect(fetchArtwork).toHaveBeenCalledTimes(2);
    unmount();
    expect(revoke).toHaveBeenCalledWith("blob:preview");
  });

  it("requires the Blob-backed artwork image to decode before approval", async () => {
    const review = completeReadyReview();
    const fetchArtwork = vi.fn().mockResolvedValue(new Blob(["not-a-decodable-png"], { type: "image/png" }));
    render(<MemoryRouter initialEntries={[`/jobs/${review.job_id}`]}><AppRoutes dependencies={dependencies(review, { fetchArtwork })} /></MemoryRouter>);
    const user = userEvent.setup();
    const artwork = await screen.findByRole("img", { name: "Original uploaded artwork for this seller review" });
    for (const mockup of screen.getAllByRole("img", { name: /representative mockup/u })) fireEvent.load(mockup);
    expect(screen.getByRole("button", { name: "Approve draft" })).toBeDisabled();
    fireEvent.error(artwork);
    expect(await screen.findByRole("button", { name: "Retry artwork preview" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Approve draft" })).toBeDisabled();
    await user.click(screen.getByRole("button", { name: "Retry artwork preview" }));
    fireEvent.load(await screen.findByRole("img", { name: "Original uploaded artwork for this seller review" }));
    expect(screen.getByRole("button", { name: "Approve draft" })).toBeEnabled();
  });

  it("never carries a late job A preview grant into job B approval", async () => {
    const base = readyReview();
    const makeReview = (jobId: string, title: string) => sellerReviewSchema.parse({
      ...base,
      job_id: jobId,
      display_state: "ready_for_review",
      stage: "human_review",
      listing: { ...base.listing, title },
      preview: {
        ...base.preview,
        readiness: "ready",
        url: `${window.location.origin}/v1/jobs/${jobId}/artwork-preview`,
        expires_at: new Date(Date.now() + 5 * 60_000).toISOString(),
      },
      mockups: {
        ...base.mockups,
        readiness: "ready",
        items: [{ contract_version: "2.0.0", url: "https://images.printify.com/review/front.png", alt_text: "Front representative mockup" }],
      },
      actions: base.actions.map((item) => item.action === "approve_review"
        ? { ...item, enabled: true, reason: "AVAILABLE", message: "Approve this exact review." }
        : item),
    });
    const first = makeReview(base.job_id, "First listing");
    const second = makeReview("job_second", "Second listing");
    let resolveFirstPreview: ((blob: Blob) => void) | undefined;
    let resolveSecondPreview: ((blob: Blob) => void) | undefined;
    const firstPreview = new Promise<Blob>((resolve) => { resolveFirstPreview = resolve; });
    const secondPreview = new Promise<Blob>((resolve) => { resolveSecondPreview = resolve; });
    const fetchArtwork = vi.fn().mockImplementation((url: string) => url.includes(first.job_id) ? firstPreview : secondPreview);
    const getReview = vi.fn().mockImplementation((jobId: string) => Promise.resolve({
      value: jobId === first.job_id ? first : second,
      requestId: `request-${jobId}`,
      etag: `"${base.review_authority_etag ?? ""}"`,
    }));
    render(
      <MemoryRouter initialEntries={[`/jobs/${first.job_id}`]}>
        <NavigateToSecond />
        <AppRoutes dependencies={dependencies(first, { getReview, fetchArtwork })} />
      </MemoryRouter>,
    );
    const user = userEvent.setup();
    await screen.findByRole("heading", { name: "First listing" });
    await user.click(screen.getByRole("button", { name: "Open second route" }));
    await screen.findByRole("heading", { name: "Second listing" });
    fireEvent.load(screen.getByRole("img", { name: "Front representative mockup" }));
    await act(async () => {
      resolveFirstPreview?.(new Blob(["first"], { type: "image/png" }));
      await Promise.resolve();
    });
    expect(screen.getByRole("button", { name: "Approve draft" })).toBeDisabled();
    expect(screen.queryByRole("img", { name: "Original uploaded artwork for this seller review" })).not.toBeInTheDocument();
    await act(async () => {
      resolveSecondPreview?.(new Blob(["second"], { type: "image/png" }));
      await Promise.resolve();
    });
    const secondArtwork = await screen.findByRole("img", { name: "Original uploaded artwork for this seller review" });
    expect(screen.getByRole("button", { name: "Approve draft" })).toBeDisabled();
    fireEvent.load(secondArtwork);
    expect(screen.getByRole("button", { name: "Approve draft" })).toBeEnabled();
  });

  it("does not render partial economics as a monetary estimate", async () => {
    const base = readyReview();
    const review = sellerReviewSchema.parse({
      ...base,
      economics: {
        ...base.economics,
        readiness: "refreshing",
        variants: [{
          contract_version: "2.0.0",
          color: "PartialOnly",
          size: "S",
          retail_price_cents: 2999,
          buyer_shipping_cents: 0,
          production_cost_cents: 1000,
          production_shipping_cents: 500,
          marketplace_fees_cents: 300,
          estimated_proceeds_cents: 1199,
        }],
      },
    });
    render(<MemoryRouter initialEntries={[`/jobs/${review.job_id}`]}><AppRoutes dependencies={dependencies(review)} /></MemoryRouter>);
    expect(await screen.findByText("Estimated proceeds are refreshing; no monetary estimate is presented.")).toBeInTheDocument();
    expect(screen.queryByText("PartialOnly")).not.toBeInTheDocument();
    expect(screen.queryByRole("table", { name: /Estimated proceeds by product color/u })).not.toBeInTheDocument();
  });
});

function NavigateToSecond() {
  const navigate = useNavigate();
  return <button type="button" onClick={() => void navigate("/jobs/job_second")}>Open second route</button>;
}

function readyReview(): SellerReview {
  const pending = sellerReviewSchema.parse(browserFixtures.seller_review_pending);
  const fingerprint = "a".repeat(64);
  return sellerReviewSchema.parse({
    ...pending,
    record_version: 7,
    review_version: 2,
    review_fingerprint: fingerprint,
    review_authority_etag: fingerprint,
    display_state: "needs_revision",
    stage: "seller_revision",
    actions: pending.actions.map((item) => item.action === "edit_listing" ? { ...item, enabled: true, reason: "AVAILABLE", message: "Edit this listing." } : item),
    listing: {
      ...pending.listing,
      readiness: "ready",
      title: "Moonlit botanical shirt",
      description: "A carefully prepared botanical design.",
      tags: Array.from({ length: 13 }, (_, index) => `botanical ${index + 1}`),
      audience: ["Nature lovers"],
    },
    validation: { ...pending.validation, readiness: "ready", passed: true, issues: [] },
    strands: {
      ...pending.strands,
      readiness: "ready",
      framework: "strands-agents",
      agent_id: "mr-lister-preparation",
      prepared_review_version: 2,
      correlation_id: "a".repeat(24),
      tool_calls: ["record_prepared_review"],
      completed_at: "2026-08-22T12:00:00Z",
    },
  });
}

function completeReadyReview(): SellerReview {
  const base = readyReview();
  const colors = ["Black", "Navy", "Forest", "Maroon", "Charcoal"];
  const sizes = ["S", "M", "L", "XL", "2XL", "3XL"];
  const variants = colors.flatMap((color) => sizes.map((size, sizeIndex) => {
    const index = colors.indexOf(color) * sizes.length + sizeIndex;
    return {
      contract_version: "2.0.0" as const,
      color,
      size,
      retail_price_cents: 2999,
      buyer_shipping_cents: 0,
      production_cost_cents: 900 + index,
      production_shipping_cents: 500,
      marketplace_fees_cents: 300,
      estimated_proceeds_cents: 1299 - index,
    };
  }));
  return sellerReviewSchema.parse({
    ...base,
    display_state: "ready_for_review",
    stage: "human_review",
    actions: base.actions.map((item) => {
      if (item.action === "approve_review") return { ...item, enabled: true, reason: "AVAILABLE", message: "Approve this exact review." };
      return { ...item, enabled: false, reason: "NOT_IN_CURRENT_STATE", message: "This action is not available during human review." };
    }),
    artwork: {
      ...base.artwork,
      readiness: "ready",
      subject: "Moonlit botanical illustration",
      visual_elements: ["Moon", "Wildflowers"],
      styles: ["Hand drawn"],
      themes: ["Nature"],
      visible_text: [],
      safety_notes: [],
      confidence: 0.98,
    },
    preview: {
      ...base.preview,
      readiness: "ready",
      url: `${window.location.origin}/v1/jobs/${base.job_id}/artwork-preview`,
      expires_at: "2026-08-22T12:30:00Z",
    },
    product_policy: {
      ...base.product_policy,
      colors,
      sizes,
      placements: [
        {
          contract_version: "2.0.0",
          group_id: "placement_small",
          sizes: ["S", "M", "L"],
          position: "Centered below collar",
          decoration_method: "Direct to garment",
          x: 0.5,
          y: 0.08,
          scale: 0.68,
          angle: 0,
        },
        {
          contract_version: "2.0.0",
          group_id: "placement_large",
          sizes: ["XL", "2XL", "3XL"],
          position: "Centered below collar",
          decoration_method: "Direct to garment",
          x: 0.5,
          y: 0.12,
          scale: 0.72,
          angle: 0,
        },
      ],
    },
    synchronization: {
      ...base.synchronization,
      readiness: "ready",
      product_id: "printify_product_ready",
      synchronized_at: "2026-08-22T12:08:00Z",
      review_version: base.review_version,
      editable_draft: true,
    },
    mockups: {
      ...base.mockups,
      readiness: "ready",
      items: [
        { contract_version: "2.0.0", url: "https://images.printify.com/review/front.png", alt_text: "Front representative mockup" },
        { contract_version: "2.0.0", url: "https://images.printify.com/review/back.png", alt_text: "Back representative mockup" },
      ],
    },
    economics: {
      ...base.economics,
      readiness: "ready",
      minimum_cents: 1270,
      maximum_cents: 1299,
      variants,
      calculated_at: "2026-08-22T12:10:00Z",
      fresh_until: "2026-08-22T13:10:00Z",
      production_cost_source: "Connected production product readback",
      production_cost_observed_at: "2026-08-22T12:09:00Z",
      production_shipping_source: "Connected production standard US shipping",
      production_shipping_observed_at: "2026-08-22T12:09:00Z",
      fee_policy_source: "Etsy US standard fee policy",
      fee_policy_id: "etsy-us-standard-v1",
      fee_policy_verified_on: "2026-08-22",
      assumptions: ["Provider prices can change before a later order."],
    },
    strands: {
      ...base.strands,
      correlation_id: "c".repeat(24),
      completed_at: "2026-08-22T12:07:00Z",
    },
  });
}

function reviewResponse(review: SellerReview, requestId: string) {
  return { value: review, requestId, etag: `"${review.review_authority_etag ?? ""}"` };
}

function progressResponse(review: SellerReview) {
  return {
    value: {
      contract_version: "2.0.0" as const,
      job_id: review.job_id,
      record_version: review.record_version,
      review_version: review.review_version,
      display_state: review.display_state,
      stage: review.stage,
      authority_notice: review.authority_notice,
      actions: review.actions,
      failure: review.failure,
      provider_outcome_unconfirmed: review.provider_outcome_unconfirmed,
      created_at: review.created_at,
      updated_at: review.updated_at,
    },
    requestId: "request-progress",
    etag: null,
  };
}

function dependencies(review: SellerReview, overrides: Partial<ApiPort> = {}): { api: ApiPort; auth: AuthCoordinator } {
  const session = new MemoryAuthSession();
  session.set("test-token", 3600, "refresh-token");
  const never = () => Promise.reject(new Error("Unexpected test call"));
  const api: ApiPort = {
    listJobs: never,
    getJob: never,
    getUpload: never,
    getReview: vi.fn().mockResolvedValue({ value: review, requestId: "request-review", etag: `"${review.review_authority_etag ?? ""}"` }),
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
  return { api, auth };
}
