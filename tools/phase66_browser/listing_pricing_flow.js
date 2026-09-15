async page => {
  const check = (condition, message) => {
    if (!condition) throw new Error(`Listing pricing gate: ${message}`);
  };
  const { fixtureOrigin, publicOrigin } = page.__phase66;
  const fixtureHeaders = { Authorization: "Bearer phase66-access-token" };
  let review = await (await page.request.get(`${fixtureOrigin}/v1/jobs/job_route_b/review`, { headers: fixtureHeaders })).json();
  review.product_policy.colors = ["Black", "Charcoal", "Dark Chocolate", "Navy", "Sand"];
  review.product_policy.sizes = ["S", "M", "L", "XL", "2XL", "3XL"];
  review.product_policy.pricing = { retail_price_cents: 2999, variant_prices: [], free_shipping: true };
  review.product_policy.pricing_saved = true;
  const saved = [];
  const routePattern = `${publicOrigin}/v1/jobs/job_route_b/review**`;
  const handler = async route => {
    const request = route.request();
    if (request.method() === "PUT") {
      const body = request.postDataJSON();
      check(request.headers()["if-match"] === `"${review.review_authority_etag}"`, "price save omitted exact authority");
      check(body.expected_review_version === review.review_version, "price save lost review version");
      check(request.headers()["idempotency-key"], "price save omitted its idempotency key");
      saved.push(body);
      review = JSON.parse(JSON.stringify(review));
      review.record_version += 1;
      review.review_version += 1;
      review.review_fingerprint = "d".repeat(64);
      review.review_authority_etag = "e".repeat(64);
      review.product_policy.pricing = body.listing.pricing;
      review.product_policy.retail_price_cents = body.listing.pricing.retail_price_cents;
      review.synchronization.review_version = review.review_version;
      await route.fulfill({ status: 200, contentType: "application/json", headers: { "X-Request-Id": "request-pricing-save" }, body: JSON.stringify({ job_id: review.job_id, state: "product_draft_syncing", record_version: review.record_version, review_version: review.review_version }) });
    } else {
      await route.fulfill({ status: 200, contentType: "application/json", headers: { "X-Request-Id": "request-pricing-review", ETag: `"${review.review_authority_etag}"` }, body: JSON.stringify(review) });
    }
  };
  await page.route(routePattern, handler);
  try {
    await page.evaluate(() => {
      history.pushState(null, "", "/jobs/job_route_b");
      dispatchEvent(new PopStateEvent("popstate"));
    });
    const itemPrice = page.getByRole("textbox", { name: "Item price", exact: true });
    await itemPrice.waitFor();
    await itemPrice.fill("32.50");
    await page.locator(".variant-prices > summary").click();
    check(await page.locator(".variant-prices tbody tr").count() === 30, "the full product variant set is absent");
    const color = review.product_policy.colors[0];
    const size = review.product_policy.sizes[0];
    const variant = page.getByRole("textbox", { name: `${color} / ${size} price`, exact: true });
    await variant.fill("35.00");
    check(await page.getByRole("button", { name: `Reset ${color} / ${size} to item price` }).isVisible(), "custom price has no reset");
    await page.getByRole("button", { name: "Apply to all variants", exact: true }).click();
    check(await variant.inputValue() === "32.50", "global price did not replace override");
    await variant.fill("35.01");
    await page.getByRole("switch", { name: "Free shipping", exact: true }).uncheck();
    check(await page.getByRole("button", { name: "Approve draft", exact: true }).count() === 0, "unsaved pricing allowed approval");
    await page.getByRole("button", { name: "Save listing revision", exact: true }).click();
    await page.locator('.action-panel[data-listing-barrier="none"]').waitFor();
    check(saved.length === 1, "price save was missing or duplicated");
    check(saved[0].listing.pricing.retail_price_cents === 3250 && saved[0].listing.pricing.free_shipping === false, "price or shipping was not sent correctly");
    check(saved[0].listing.pricing.variant_prices[0].retail_price_cents === 3501, "variant lost integer-cent precision");
    check(await itemPrice.inputValue() === "32.50" && await variant.inputValue() === "35.01", "saved prices did not survive readback");
    check(!(await page.getByRole("switch", { name: "Free shipping" }).isChecked()), "saved shipping did not survive readback");

    for (const mode of ["light", "dark"]) {
      await page.getByRole("button", { name: /^Display theme:/u }).click();
      await page.getByRole("menuitemradio", { name: mode === "light" ? "Light" : "Dark", exact: true }).click();
      await page.locator(".pricing-fields").screenshot({ path: `pricing-${mode}.png` });
    }
    await page.setViewportSize({ width: 390, height: 844 });
    await page.locator(".pricing-fields").scrollIntoViewIfNeeded();
    check(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth + 1), "pricing caused mobile page overflow");
    await page.locator(".pricing-fields").screenshot({ path: "pricing-mobile.png" });
    await page.setViewportSize({ width: 1280, height: 900 });
    await page.getByRole("button", { name: /^Display theme:/u }).click();
    await page.getByRole("menuitemradio", { name: "Auto", exact: true }).click();
    return { globalAndVariantPrices: "passed", freeShipping: "passed", authoritativeSave: "passed", integerCents: "passed", themesAndMobile: "passed" };
  } finally {
    await page.unroute(routePattern, handler);
  }
}
