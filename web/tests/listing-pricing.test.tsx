import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";
import browserFixtures from "../../contracts/browser/phase6.5.fixtures.json";
import { AppRoutes } from "../src/App";
import { ApiError, type ApiPort, type ListingDraft } from "../src/api/client";
import { MemoryAuthSession, type AuthCoordinator } from "../src/auth/session";
import { sellerReviewSchema, type ListingPricing, type SellerReview } from "../src/contracts";

describe("listing pricing and shipping revisions", () => {
  it("applies the item price to every variant only when the seller explicitly clears custom prices", async () => {
    const review = readyReview();
    const reviseListing = interruptedSave();
    renderReview(review, { reviseListing });
    const price = await screen.findByRole("textbox", { name: "Item price" });
    await openVariants();
    expect(variant("Black", "S")).toHaveValue("31.99");
    expect(variant("Navy", "M")).toHaveValue("32.99");
    fireEvent.change(price, { target: { value: "24.50" } });
    expect(variant("Black", "M")).toHaveValue("24.50");
    expect(variant("Black", "S")).toHaveValue("31.99");
    expect(variant("Navy", "M")).toHaveValue("32.99");
    await userEvent.click(screen.getByRole("button", { name: "Apply to all variants" }));
    for (const color of review.product_policy.colors) {
      for (const size of review.product_policy.sizes) expect(variant(color, size)).toHaveValue("24.50");
    }
    expect(screen.queryByRole("button", { name: /^Reset .* to item price/u })).not.toBeInTheDocument();
    await save();
    expectSavedPricing(reviseListing, { retail_price_cents: 2450, variant_prices: [], free_shipping: true });
  });

  it("edits and resets individual variants without changing the other custom prices", async () => {
    const review = readyReview();
    const reviseListing = interruptedSave();
    renderReview(review, { reviseListing });
    await screen.findByRole("textbox", { name: "Item price" });
    await openVariants();
    fireEvent.change(variant("Black", "M"), { target: { value: "30.07" } });
    await userEvent.click(screen.getByRole("button", { name: "Reset Black / S to item price" }));
    expect(variant("Black", "S")).toHaveValue("29.99");
    expect(variant("Black", "M")).toHaveValue("30.07");
    expect(variant("Navy", "M")).toHaveValue("32.99");
    expect(variant("Navy", "S")).toHaveValue("29.99");
    await save();
    expectSavedPricing(reviseListing, {
      retail_price_cents: 2999,
      variant_prices: [
        { color: "Black", size: "M", retail_price_cents: 3007 },
        { color: "Navy", size: "M", retail_price_cents: 3299 },
      ],
      free_shipping: true,
    });
  });

  it("saves free-shipping changes and decimal prices as integer cents in the existing revision command", async () => {
    const reviseListing = interruptedSave();
    renderReview(readyReview(), { reviseListing });
    fireEvent.change(await screen.findByRole("textbox", { name: "Item price" }), { target: { value: "0.29" } });
    expect(screen.getByRole("switch", { name: "Free shipping" })).toBeEnabled();
    await userEvent.click(screen.getByRole("switch", { name: "Free shipping" }));
    await openVariants();
    fireEvent.change(variant("Black", "S"), { target: { value: "17.03" } });
    expect(screen.getByRole("switch", { name: "Free shipping" })).not.toBeChecked();
    await save();
    expectSavedPricing(reviseListing, {
      retail_price_cents: 29,
      variant_prices: [
        { color: "Black", size: "S", retail_price_cents: 1703 },
        { color: "Navy", size: "M", retail_price_cents: 3299 },
      ],
      free_shipping: false,
    });
    const payload = reviseListing.mock.calls[0]?.[1];
    expect(payload?.title).toBe("Botanical pricing listing");
    expect(payload?.tags).toHaveLength(13);
    expect(payload?.pricing).not.toHaveProperty("buyer_shipping_cents");
    expect(payload?.pricing).not.toHaveProperty("shipping_cost");
  });

  it.each([false, true])("shows authoritative judge shipping %s without a switch or changing saved data", async (freeShipping) => {
    const review = readyReview();
    const pricing = { ...defaultPricing(), free_shipping: freeShipping };
    review.product_policy.pricing = pricing;
    const originalPricing = structuredClone(pricing);
    const reviseListing = interruptedSave();
    renderReview(review, { reviseListing }, true);

    const price = await screen.findByRole("textbox", { name: "Item price" });
    expect(price).toHaveValue("29.99");
    expect(price).toBeEnabled();
    expect(screen.queryByRole("switch", { name: "Free shipping" })).not.toBeInTheDocument();
    expect(screen.getByText("Shipping is fixed for judge access.")).toBeVisible();
    if (freeShipping) {
      expect(screen.getByText("Free shipping is selected")).toBeVisible();
      expect(screen.getByText("Standard shipping is required before this judge listing can be published.")).toBeVisible();
    } else {
      expect(screen.getByText("Printify standard shipping", { selector: "strong" })).toBeVisible();
      expect(screen.queryByText("Free shipping is selected")).not.toBeInTheDocument();
    }
    expect(screen.queryByRole("button", { name: "Save listing revision" })).not.toBeInTheDocument();
    expect(reviseListing).not.toHaveBeenCalled();
    expect(review.product_policy.pricing).toEqual(originalPricing);

    if (freeShipping) {
      await userEvent.click(screen.getByRole("button", { name: "Use standard shipping" }));
      expect(screen.getByText("Save your revision to apply standard shipping before publication.")).toBeVisible();
      expect(screen.queryByText("Free shipping is selected")).not.toBeInTheDocument();
      expect(reviseListing).not.toHaveBeenCalled();
      expect(screen.queryByRole("button", { name: "Approve draft" })).not.toBeInTheDocument();
    } else {
      fireEvent.change(price, { target: { value: "35.00" } });
    }
    expect(screen.queryByRole("button", { name: "Use standard shipping" })).not.toBeInTheDocument();
    await save();
    expectSavedPricing(reviseListing, { ...originalPricing, retail_price_cents: freeShipping ? originalPricing.retail_price_cents : 3500, free_shipping: false });
    expect(review.product_policy.pricing).toEqual(originalPricing);
  });

  it("keeps the judge shipping correction disabled when the review is read-only", async () => {
    const base = readyReview();
    const review = sellerReviewSchema.parse({
      ...base, display_state: "approved", stage: "complete",
      actions: base.actions.map((action) => ({ ...action, enabled: false, reason: "NOT_IN_CURRENT_STATE", message: "This review is read-only." })),
    });
    const reviseListing = vi.fn<ApiPort["reviseListing"]>();
    renderReview(review, { reviseListing }, true);
    expect(await screen.findByRole("button", { name: "Use standard shipping" })).toBeDisabled();
    expect(screen.getByText("Free shipping is selected")).toBeVisible();
    expect(screen.queryByRole("switch", { name: "Free shipping" })).not.toBeInTheDocument();
    expect(reviseListing).not.toHaveBeenCalled();
  });

  it("displays a backend-projected $35 judge default without making a local revision", async () => {
    const review = readyReview();
    review.product_policy.pricing = { retail_price_cents: 3500, variant_prices: [], free_shipping: false };
    const reviseListing = vi.fn<ApiPort["reviseListing"]>();
    renderReview(review, { reviseListing }, true);
    expect(await screen.findByRole("textbox", { name: "Item price" })).toHaveValue("35.00");
    await openVariants();
    for (const color of review.product_policy.colors) {
      for (const size of review.product_policy.sizes) expect(variant(color, size)).toHaveValue("35.00");
    }
    expect(screen.queryByRole("button", { name: "Save listing revision" })).not.toBeInTheDocument();
    expect(reviseListing).not.toHaveBeenCalled();
  });

  it.each(["", "0", "-1", "1.001", "1e2", "NaN", "Infinity", "$12.00", "1,000.00", "10000"])(
    "rejects invalid item price %j without submitting a revision",
    async (input) => {
      const reviseListing = vi.fn<ApiPort["reviseListing"]>();
      renderReview(readyReview(), { reviseListing });
      const price = await screen.findByRole("textbox", { name: "Item price" });
      fireEvent.change(price, { target: { value: input } });
      await userEvent.click(screen.getByRole("button", { name: "Save listing revision" }));
      expect(price).toHaveAttribute("aria-invalid", "true");
      expect(price).toHaveAccessibleDescription(/Enter an item price/u);
      expect(screen.getByRole("button", { name: "Apply to all variants" })).toBeDisabled();
      await waitFor(() => expect(document.getElementById("listing-errors")).toHaveFocus());
      expect(reviseListing).not.toHaveBeenCalled();
    },
  );

  it("shows an invalid custom-price error on the correct variant and does not save", async () => {
    const reviseListing = vi.fn<ApiPort["reviseListing"]>();
    renderReview(readyReview(), { reviseListing });
    await screen.findByRole("textbox", { name: "Item price" });
    await openVariants();
    fireEvent.change(variant("Navy", "M"), { target: { value: "32.001" } });
    await userEvent.click(screen.getByText(/^Prices by variant/u));
    await userEvent.click(screen.getByRole("button", { name: "Save listing revision" }));
    const invalid = variant("Navy", "M");
    expect(invalid).toBeVisible();
    expect(invalid).toHaveAttribute("aria-invalid", "true");
    expect(invalid).toHaveAccessibleDescription(/Navy \/ M/u);
    expect(variant("Black", "S")).toHaveAttribute("aria-invalid", "false");
    expect(screen.getByRole("textbox", { name: "Item price" })).toHaveAttribute("aria-invalid", "false");
    expect(reviseListing).not.toHaveBeenCalled();
  });

  it("maps server pricing errors and summary links to the affected item and variant fields", async () => {
    const reviseListing = vi.fn<ApiPort["reviseListing"]>().mockRejectedValue(new ApiError(
      422, "INVALID_LISTING", "The provider rejected these prices.", "request-pricing-fields", null, [
        { path: "$.listing.pricing.retail_price_cents", code: "OUT_OF_RANGE", message: "The item price exceeds the permitted range." },
        { path: "$.listing.pricing.variant_prices[1].retail_price_cents", code: "OUT_OF_RANGE", message: "The Navy / M price exceeds the permitted range." },
      ],
    ));
    renderReview(readyReview(), { reviseListing });
    const itemPrice = await screen.findByRole("textbox", { name: "Item price" });
    fireEvent.change(itemPrice, { target: { value: "38.50" } });
    await userEvent.click(screen.getByRole("button", { name: "Save listing revision" }));
    await screen.findByText("The provider rejected these prices.");
    expect(itemPrice).toHaveAttribute("aria-invalid", "true");
    expect(itemPrice).toHaveAccessibleDescription(/item price exceeds the permitted range/u);
    const customPrice = variant("Navy", "M");
    expect(customPrice).toBeVisible();
    expect(customPrice).toHaveAttribute("aria-invalid", "true");
    expect(customPrice).toHaveAccessibleDescription(/Navy \/ M price exceeds the permitted range/u);
    expect(screen.getByRole("link", { name: "The item price exceeds the permitted range." })).toHaveAttribute("href", `#${itemPrice.id}`);
    expect(screen.getByRole("link", { name: "The Navy / M price exceeds the permitted range." })).toHaveAttribute("href", `#${customPrice.id}`);
    expect(variant("Black", "S")).toHaveAttribute("aria-invalid", "false");
    expect(reviseListing).toHaveBeenCalledTimes(1);
  });

  it("keeps variant correction visible and never transfers an old error to a new custom-price row", async () => {
    const reviseListing = interruptedSave();
    renderReview(readyReview(), { reviseListing });
    await screen.findByRole("textbox", { name: "Item price" });
    await openVariants();
    const customPrice = variant("Navy", "M");
    fireEvent.change(customPrice, { target: { value: "32.001" } });
    await userEvent.click(screen.getByRole("button", { name: "Save listing revision" }));
    expect(customPrice).toHaveAttribute("aria-invalid", "true");
    await userEvent.click(screen.getByRole("button", { name: "Reset Black / S to item price" }));
    expect(customPrice).toBeVisible();
    fireEvent.change(variant("Black", "M"), { target: { value: "33.00" } });
    expect(variant("Black", "M")).toHaveAttribute("aria-invalid", "false");
    await userEvent.click(screen.getByRole("button", { name: "Save listing revision" }));
    expect(customPrice).toHaveAttribute("aria-invalid", "true");
    expect(variant("Black", "M")).toHaveAttribute("aria-invalid", "false");
    expect(reviseListing).not.toHaveBeenCalled();
    fireEvent.change(customPrice, { target: { value: "32.00" } });
    expect(customPrice).toBeVisible();
    expect(customPrice).toHaveAttribute("aria-invalid", "false");
    await save();
    expectSavedPricing(reviseListing, { retail_price_cents: 2999, variant_prices: [
      { color: "Navy", size: "M", retail_price_cents: 3200 },
      { color: "Black", size: "M", retail_price_cents: 3300 },
    ], free_shipping: true });
  });

  it("blocks approval and protects navigation for pricing-only edits until deliberate discard", async () => {
    const review = readyReview();
    const reviseListing = vi.fn<ApiPort["reviseListing"]>();
    renderReview(review, { reviseListing });
    const price = await screen.findByRole("textbox", { name: "Item price" });
    await loadApprovalEvidence();
    const approve = screen.getByRole("button", { name: "Approve draft" });
    await waitFor(() => expect(approve).toBeEnabled());
    fireEvent.change(price, { target: { value: "38.50" } });
    expect(screen.queryByRole("button", { name: "Approve draft" })).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole("link", { name: "Dashboard" }));
    expect(screen.getByRole("dialog", { name: "Leave your unsaved changes?" })).toBeVisible();
    await userEvent.click(screen.getByRole("button", { name: "Stay here" }));
    expect(price).toHaveValue("38.50");
    await userEvent.click(screen.getByRole("button", { name: "Discard edits" }));
    expect(price).toHaveValue("29.99");
    await waitFor(() => expect(screen.getByRole("button", { name: "Approve draft" })).toBeEnabled());
    await userEvent.click(screen.getByRole("link", { name: "Dashboard" }));
    expect(await screen.findByRole("heading", { name: "Let’s start with your artwork." })).toBeVisible();
    expect(reviseListing).not.toHaveBeenCalled();
  });

  it.each(["approved", "cancelled"] as const)("keeps pricing and shipping uneditable for a %s review", async (displayState) => {
    const base = readyReview();
    const review = sellerReviewSchema.parse({
      ...base, display_state: displayState, stage: "complete",
      actions: base.actions.map((action) => ({ ...action, enabled: false, reason: "NOT_IN_CURRENT_STATE", message: "This review is read-only." })),
    });
    const reviseListing = vi.fn<ApiPort["reviseListing"]>();
    renderReview(review, { reviseListing });
    expect(await screen.findByRole("textbox", { name: "Item price" })).toBeDisabled();
    expect(screen.getByRole("switch", { name: "Free shipping" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Apply to all variants" })).toBeDisabled();
    await openVariants();
    expect(variant("Black", "S")).toBeDisabled();
    expect(screen.getByRole("button", { name: "Reset Black / S to item price" })).toBeDisabled();
    expect(screen.queryByRole("button", { name: "Save listing revision" })).not.toBeInTheDocument();
    expect(reviseListing).not.toHaveBeenCalled();
  });

  it("retains pricing through saving and accepted readback, and restores editing only after the current revision arrives", async () => {
    const original = readyReview();
    const acceptedPricing: ListingPricing = { retail_price_cents: 2450, variant_prices: [], free_shipping: false };
    const current = advancedReview(original, acceptedPricing);
    const saveResult = deferred<Awaited<ReturnType<ApiPort["reviseListing"]>>>();
    const readback = deferred<Awaited<ReturnType<ApiPort["getReview"]>>>();
    const getReview = vi.fn<ApiPort["getReview"]>().mockResolvedValueOnce(reviewResponse(original)).mockReturnValue(readback.promise);
    const reviseListing = vi.fn<ApiPort["reviseListing"]>().mockReturnValue(saveResult.promise);
    renderReview(original, { getReview, reviseListing });
    const price = await screen.findByRole("textbox", { name: "Item price" });
    fireEvent.change(price, { target: { value: "24.50" } });
    await userEvent.click(screen.getByRole("button", { name: "Apply to all variants" }));
    await userEvent.click(screen.getByRole("switch", { name: "Free shipping" }));
    await userEvent.click(screen.getByRole("button", { name: "Save listing revision" }));
    expect(price).toHaveValue("24.50");
    expect(price).toBeDisabled();
    expect(screen.getByRole("switch", { name: "Free shipping" })).toBeDisabled();
    await userEvent.click(screen.getByRole("link", { name: "Dashboard" }));
    expect(screen.getByRole("dialog", { name: "Your changes are saving" })).toBeVisible();
    expect(screen.queryByRole("button", { name: "Leave without saving" })).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Stay here" }));
    await act(async () => { saveResult.resolve(acceptedResponse(original)); await saveResult.promise; });
    await waitFor(() => expect(getReview).toHaveBeenCalledTimes(2));
    expect(price).toHaveValue("24.50");
    expect(price).toBeDisabled();
    await userEvent.click(screen.getByRole("link", { name: "Dashboard" }));
    expect(screen.getByRole("dialog", { name: "Your listing is updating" })).toBeVisible();
    await userEvent.click(screen.getByRole("button", { name: "Stay here" }));
    await act(async () => { readback.resolve(reviewResponse(current)); await readback.promise; });
    await screen.findByText(/Authoritative record 9 ·/u);
    await waitFor(() => expect(price).toBeEnabled());
    expect(price).toHaveValue("24.50");
    expect(screen.getByRole("switch", { name: "Free shipping" })).not.toBeChecked();
    expect(screen.queryByRole("button", { name: "Save listing revision" })).not.toBeInTheDocument();
    expectSavedPricing(reviseListing, acceptedPricing);
  });

  it("preserves accepted pricing when readback fails and recovers through an explicit authoritative refresh", async () => {
    const original = readyReview();
    const expected = { ...defaultPricing(), retail_price_cents: 3850 };
    const getReview = vi.fn<ApiPort["getReview"]>()
      .mockResolvedValueOnce(reviewResponse(original))
      .mockRejectedValueOnce(new TypeError("pricing readback unavailable"))
      .mockResolvedValue(reviewResponse(advancedReview(original, expected)));
    const reviseListing = vi.fn<ApiPort["reviseListing"]>().mockResolvedValue(acceptedResponse(original));
    renderReview(original, { getReview, reviseListing });
    const price = await screen.findByRole("textbox", { name: "Item price" });
    fireEvent.change(price, { target: { value: "38.50" } });
    await userEvent.click(screen.getByRole("button", { name: "Save listing revision" }));
    await screen.findByText(/latest authoritative review is temporarily unavailable/u);
    expect(price).toHaveValue("38.50");
    expect(price).toBeDisabled();
    await userEvent.click(screen.getByRole("button", { name: "Refresh authoritative review" }));
    await screen.findByText(/Authoritative record 9 ·/u);
    await waitFor(() => expect(price).toBeEnabled());
    expect(price).toHaveValue("38.50");
    expectSavedPricing(reviseListing, expected);
  });

  it.each(["item price", "variant price", "free shipping"] as const)(
    "does not discard a lost-response local revision when newer text matches but its %s differs",
    async (mismatch) => {
      const original = readyReview();
      const local = changedPricing();
      const different: ListingPricing = mismatch === "item price" ? { ...local, retail_price_cents: 3151 }
        : mismatch === "free shipping" ? { ...local, free_shipping: true }
          : { ...local, variant_prices: local.variant_prices.map((item, index) => index === 0 ? { ...item, retail_price_cents: item.retail_price_cents + 1 } : item) };
      const latest = advancedReview(original, different);
      const getReview = vi.fn<ApiPort["getReview"]>().mockResolvedValueOnce(reviewResponse(original)).mockResolvedValue(reviewResponse(latest));
      const getJob = vi.fn<ApiPort["getJob"]>().mockResolvedValue(progressResponse(latest));
      const reviseListing = interruptedSave();
      renderReview(original, { getReview, getJob, reviseListing });
      await makeChangedPricing();
      await save();
      await refreshByFocus();
      await screen.findByText(/Authoritative record 9 ·/u);
      expectLocalChangedPricing();
      expect(screen.queryByText(/contains this exact revision/u)).not.toBeInTheDocument();
      expect(screen.getByRole("button", { name: "Save listing revision" })).toBeDisabled();
      expect(screen.queryByRole("button", { name: "Approve draft" })).not.toBeInTheDocument();
      expect(screen.getByRole("button", { name: "Reapply revision to latest review" })).toBeEnabled();
      expect(reviseListing).toHaveBeenCalledTimes(1);
      if (mismatch === "item price") {
        await userEvent.click(screen.getByRole("button", { name: "Reapply revision to latest review" }));
        await save();
        expect(reviseListing).toHaveBeenCalledTimes(2);
        expect(reviseListing.mock.calls[1]?.[0]).toEqual(expect.objectContaining({ record_version: 9, review_version: 3, review_authority_etag: latest.review_authority_etag }));
        expect(reviseListing.mock.calls[1]?.[1].pricing).toEqual(local);
      }
    },
  );

  it("recognizes an exact pricing revision after a lost response even when override order differs", async () => {
    const original = readyReview();
    const local = changedPricing();
    const latest = advancedReview(original, { ...local, variant_prices: [...local.variant_prices].reverse() });
    const getReview = vi.fn<ApiPort["getReview"]>().mockResolvedValueOnce(reviewResponse(original)).mockResolvedValue(reviewResponse(latest));
    const getJob = vi.fn<ApiPort["getJob"]>().mockResolvedValue(progressResponse(latest));
    const reviseListing = interruptedSave();
    renderReview(original, { getReview, getJob, reviseListing });
    await makeChangedPricing();
    await save();
    await refreshByFocus();
    await screen.findByText(/contains this exact revision/u);
    expectLocalChangedPricing();
    expect(screen.queryByRole("button", { name: "Reapply revision to latest review" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Save listing revision" })).not.toBeInTheDocument();
    expectSavedPricing(reviseListing, local);
  });

  it("preserves pricing after an accepted save if the minimum-version readback contains a different price", async () => {
    const original = readyReview();
    const latest = advancedReview(original, { ...defaultPricing(), retail_price_cents: 3950 });
    const getReview = vi.fn<ApiPort["getReview"]>().mockResolvedValueOnce(reviewResponse(original)).mockResolvedValue(reviewResponse(latest));
    const reviseListing = vi.fn<ApiPort["reviseListing"]>().mockResolvedValue(acceptedResponse(original));
    renderReview(original, { getReview, reviseListing });
    const price = await screen.findByRole("textbox", { name: "Item price" });
    fireEvent.change(price, { target: { value: "38.50" } });
    await userEvent.click(screen.getByRole("button", { name: "Save listing revision" }));
    await screen.findByText(/Authoritative record 9 ·/u);
    expect(price).toHaveValue("38.50");
    expect(screen.queryByRole("button", { name: "Approve draft" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Save listing revision" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Reapply revision to latest review" })).toBeEnabled();
    expectSavedPricing(reviseListing, { ...defaultPricing(), retail_price_cents: 3850 });
  });

  it("keeps legacy reviews compatible without advertising an unavailable pricing editor", async () => {
    const review = readyReview();
    delete review.product_policy.pricing;
    const reviseListing = interruptedSave();
    renderReview(sellerReviewSchema.parse(review), { reviseListing });
    const title = await screen.findByRole("textbox", { name: /^Title/u });
    expect(screen.queryByRole("textbox", { name: "Item price" })).not.toBeInTheDocument();
    expect(screen.queryByRole("switch", { name: "Free shipping" })).not.toBeInTheDocument();
    fireEvent.change(title, { target: { value: "Changed legacy title" } });
    await save();
    expect(reviseListing.mock.calls[0]?.[1]).not.toHaveProperty("pricing");
  });
});

function defaultPricing(): ListingPricing {
  return { retail_price_cents: 2999, variant_prices: [
    { color: "Black", size: "S", retail_price_cents: 3199 },
    { color: "Navy", size: "M", retail_price_cents: 3299 },
  ], free_shipping: true };
}

function changedPricing(): ListingPricing {
  return { retail_price_cents: 3150, variant_prices: [
    { color: "Black", size: "S", retail_price_cents: 3010 },
    { color: "Navy", size: "M", retail_price_cents: 3299 },
  ], free_shipping: false };
}

function readyReview(): SellerReview {
  const pending = sellerReviewSchema.parse(browserFixtures.seller_review_pending);
  const colors = ["Black", "Navy"];
  const sizes = ["S", "M"];
  const pricing = defaultPricing();
  const timestamp = new Date().toISOString();
  return sellerReviewSchema.parse({
    ...pending, record_version: 7, review_version: 2,
    review_fingerprint: "a".repeat(64), review_authority_etag: "b".repeat(64),
    display_state: "ready_for_review", stage: "human_review",
    actions: pending.actions.map((action) => ["edit_listing", "approve_review", "refresh_economics"].includes(action.action)
      ? { ...action, enabled: true, reason: "AVAILABLE", message: "This action is available." } : action),
    listing: { ...pending.listing, readiness: "ready", title: "Botanical pricing listing", description: "A botanical illustration for everyday wear.", tags: Array.from({ length: 13 }, (_, index) => `botanical ${index + 1}`), audience: [] },
    validation: { ...pending.validation, readiness: "ready", passed: true, issues: [] },
    artwork: { ...pending.artwork, readiness: "ready", subject: "Botanical illustration", confidence: 0.98 },
    preview: { ...pending.preview, readiness: "ready", url: `${window.location.origin}/v1/jobs/${pending.job_id}/artwork-preview`, expires_at: new Date(Date.now() + 3_600_000).toISOString() },
    product_policy: { ...pending.product_policy, colors, sizes, retail_price_cents: pricing.retail_price_cents, buyer_shipping_cents: 0, pricing },
    synchronization: { ...pending.synchronization, readiness: "ready", product_id: "printify_pricing_product", synchronized_at: timestamp, review_version: 2, editable_draft: true },
    mockups: { ...pending.mockups, readiness: "ready", items: [
      { contract_version: "2.0.0", url: "https://images.printify.com/review/front.png", alt_text: "Front representative mockup" },
      { contract_version: "2.0.0", url: "https://images.printify.com/review/back.png", alt_text: "Back representative mockup" },
    ] },
    economics: { ...pending.economics, readiness: "ready", minimum_cents: 1199, maximum_cents: 1499,
      variants: colors.flatMap((color) => sizes.map((size) => ({ contract_version: "2.0.0", color, size,
        retail_price_cents: pricing.variant_prices.find((item) => item.color === color && item.size === size)?.retail_price_cents ?? pricing.retail_price_cents,
        buyer_shipping_cents: 0, production_cost_cents: 1000, production_shipping_cents: 500, marketplace_fees_cents: 300, estimated_proceeds_cents: 1199,
      }))),
      calculated_at: timestamp, fresh_until: new Date(Date.now() + 3_600_000).toISOString(),
      production_cost_source: "Connected production product readback", production_cost_observed_at: timestamp,
      production_shipping_source: "Connected production standard US shipping", production_shipping_observed_at: timestamp,
      fee_policy_source: "Etsy US standard fee policy", fee_policy_id: "etsy-us-standard-v1", fee_policy_verified_on: "2026-09-13",
    },
  });
}

function advancedReview(original: SellerReview, pricing: ListingPricing): SellerReview {
  return sellerReviewSchema.parse({ ...original, record_version: 9, review_version: 3,
    review_fingerprint: "c".repeat(64), review_authority_etag: "d".repeat(64),
    product_policy: { ...original.product_policy, retail_price_cents: pricing.retail_price_cents, buyer_shipping_cents: pricing.free_shipping ? 0 : 500, pricing },
    synchronization: { ...original.synchronization, review_version: 3 },
  });
}

function reviewResponse(review: SellerReview) {
  return { value: review, requestId: "request-pricing-review", etag: `"${review.review_authority_etag ?? ""}"` };
}

function progressResponse(review: SellerReview) {
  const { contract_version, job_id, record_version, review_version, display_state, stage, authority_notice, actions, failure, provider_outcome_unconfirmed, created_at, updated_at } = review;
  return { value: { contract_version, job_id, record_version, review_version, display_state, stage, authority_notice, actions, failure, provider_outcome_unconfirmed, created_at, updated_at }, requestId: "request-pricing-progress", etag: null };
}

function acceptedResponse(review: SellerReview): Awaited<ReturnType<ApiPort["reviseListing"]>> {
  return { value: { job_id: review.job_id, state: "product_draft_syncing", record_version: 8, review_version: 3 }, requestId: "request-pricing-save", etag: null };
}

function renderReview(review: SellerReview, overrides: Partial<ApiPort> = {}, judgeMode = false) {
  const session = new MemoryAuthSession();
  session.set("access-token", 3600, "refresh-token");
  const never = () => Promise.reject(new Error("Unexpected pricing test request"));
  const api: ApiPort = {
    listJobs: vi.fn().mockResolvedValue({ value: { jobs: [], next_cursor: null }, requestId: "request-pricing-jobs", etag: null }),
    getJob: vi.fn().mockResolvedValue(progressResponse(review)), getUpload: never,
    getReview: vi.fn().mockResolvedValue(reviewResponse(review)), createUpload: never, authorizeUpload: never,
    completeUpload: never, cancelUpload: never, reviseListing: never, runAction: never,
    fetchArtwork: vi.fn().mockResolvedValue(new Blob(["png"], { type: "image/png" })), ...overrides,
  };
  const auth: AuthCoordinator = { session, startSignIn: never, completeSignIn: never, signOut: vi.fn() };
  return render(<MemoryRouter initialEntries={[`/jobs/${review.job_id}`]}><AppRoutes dependencies={{ api, auth, ...(judgeMode ? { judgeAccess: {} } : {}) }} /></MemoryRouter>);
}

function interruptedSave() {
  return vi.fn<ApiPort["reviseListing"]>().mockRejectedValue(new TypeError("pricing response interrupted"));
}

function expectSavedPricing(reviseListing: ReturnType<typeof interruptedSave>, pricing: ListingPricing) {
  expect(reviseListing).toHaveBeenCalledTimes(1);
  const payload: ListingDraft | undefined = reviseListing.mock.calls[0]?.[1];
  const canonical = (value: ListingPricing | undefined) => value === undefined ? undefined : {
    ...value,
    variant_prices: [...value.variant_prices].sort((left, right) => left.color.localeCompare(right.color) || left.size.localeCompare(right.size)),
  };
  expect(canonical(payload?.pricing)).toEqual(canonical(pricing));
  expect(Number.isInteger(payload?.pricing?.retail_price_cents)).toBe(true);
  for (const item of payload?.pricing?.variant_prices ?? []) expect(Number.isInteger(item.retail_price_cents)).toBe(true);
}

async function openVariants() {
  const summary = screen.getByText(/^Prices by variant/u);
  if (!summary.closest("details")?.open) await userEvent.click(summary);
}

function variant(color: string, size: string) {
  return screen.getByRole("textbox", { name: `${color} / ${size} price` });
}

async function save() {
  await userEvent.click(screen.getByRole("button", { name: "Save listing revision" }));
  await screen.findByText("pricing response interrupted");
}

async function loadApprovalEvidence() {
  fireEvent.load(await screen.findByRole("img", { name: "Original uploaded artwork for this seller review" }));
  for (const image of screen.getAllByRole("img", { name: /representative mockup/u })) fireEvent.load(image);
}

async function makeChangedPricing() {
  fireEvent.change(await screen.findByRole("textbox", { name: "Item price" }), { target: { value: "31.50" } });
  await userEvent.click(screen.getByRole("switch", { name: "Free shipping" }));
  await openVariants();
  fireEvent.change(variant("Black", "S"), { target: { value: "30.10" } });
}

function expectLocalChangedPricing() {
  expect(screen.getByRole("textbox", { name: "Item price" })).toHaveValue("31.50");
  expect(variant("Black", "S")).toHaveValue("30.10");
  expect(screen.getByRole("switch", { name: "Free shipping" })).not.toBeChecked();
}

async function refreshByFocus() {
  await act(async () => { window.dispatchEvent(new Event("focus")); await Promise.resolve(); });
}

function deferred<T>() {
  let complete: ((value: T) => void) | undefined;
  const promise = new Promise<T>((resolve) => { complete = resolve; });
  return { promise, resolve: (value: T) => { if (complete === undefined) throw new Error("Deferred result is unavailable"); complete(value); } };
}
