async page => {
  const check = (condition, message) => {
    if (!condition) throw new Error(`Workspace navigation gate: ${message}`);
  };
  const { fixtureOrigin, publicOrigin } = page.__phase66;
  const publicPath = url => url.slice(publicOrigin.length).split(/[?#]/u)[0];
  const originalPath = publicPath(page.url());
  const originalViewport = page.viewportSize() ?? { width: 1280, height: 900 };
  const fixtureHeaders = { Authorization: "Bearer phase66-access-token" };
  const readyTemplate = await (await page.request.get(`${fixtureOrigin}/v1/jobs/job_route_b/review`, { headers: fixtureHeaders })).json();
  const pendingTemplate = await (await page.request.get(`${fixtureOrigin}/v1/jobs/job_polling/review`, { headers: fixtureHeaders })).json();
  const image = await (await page.request.get(`${fixtureOrigin}/phase66/upload-selection.png`)).body();
  const uploads = [];
  const readyJobs = new Set();
  const completeJobs = new Set();
  const progressReads = new Map();
  const storageOrigin = "https://phase66-navigation.s3.us-west-2.amazonaws.com";
  let storageWrites = 0;
  let credentialsAbsent = true;
  let unexpectedWrites = 0;
  const clone = value => JSON.parse(JSON.stringify(value));
  const progressKeys = ["contract_version", "job_id", "record_version", "review_version", "display_state", "stage", "authority_notice", "actions", "failure", "provider_outcome_unconfirmed", "created_at", "updated_at"];
  const checksumBase64 = hex => {
    const alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
    const bytes = hex.match(/../gu).map(value => Number.parseInt(value, 16));
    let encoded = "";
    for (let index = 0; index < bytes.length; index += 3) {
      const word = (bytes[index] << 16) | ((bytes[index + 1] ?? 0) << 8) | (bytes[index + 2] ?? 0);
      encoded += alphabet[(word >>> 18) & 63] + alphabet[(word >>> 12) & 63]
        + (index + 1 < bytes.length ? alphabet[(word >>> 6) & 63] : "=")
        + (index + 2 < bytes.length ? alphabet[word & 63] : "=");
    }
    return encoded;
  };
  const json = (route, value, extra = {}) => route.fulfill({
    status: 200, contentType: "application/json",
    headers: { "Cache-Control": "no-store", "X-Request-Id": "request-navigation-fixture", ...extra },
    body: JSON.stringify(value),
  });
  const projection = jobId => {
    const ready = readyJobs.has(jobId);
    const value = clone(ready ? readyTemplate : pendingTemplate);
    value.job_id = jobId;
    if (value.preview.url !== null) value.preview.url = `${publicOrigin}/v1/jobs/${jobId}/artwork-preview`;
    if (ready) value.listing.title = `Prepared ${uploads.find(upload => upload.job_id === jobId).filename}`;
    // Mockups retain their known local fixture route. No real store is reachable.
    return value;
  };
  const uploadRoute = async route => {
    const request = route.request();
    const pathname = publicPath(request.url());
    if (pathname === "/v1/uploads" && request.method() === "POST") {
      const body = request.postDataJSON();
      const index = uploads.length + 1;
      const upload = { ...body, upload_id: `upload_navigation_${index}`, job_id: `job_navigation_${index}` };
      uploads.push(upload);
      const now = Date.now();
      await json(route, {
        upload: { upload_id: upload.upload_id, job_id: upload.job_id, status: "open", record_version: 1 },
        authorization: {
          upload_id: upload.upload_id, job_id: upload.job_id, authorization_generation: 1, method: "POST",
          url: `${storageOrigin}/`, content_sha256: upload.content_sha256, size_bytes: upload.size_bytes,
          issued_at: new Date(now).toISOString(), expires_at: new Date(now + 300000).toISOString(),
          form_fields: {
            key: `private/owners/${"a".repeat(64)}/jobs/${upload.job_id}/source/source.png`,
            "Content-Type": "image/png", "x-amz-checksum-algorithm": "SHA256",
            "x-amz-checksum-sha256": checksumBase64(upload.content_sha256),
            "x-amz-server-side-encryption": "AES256", "x-amz-tagging": "mr-lister-state=staged",
            "x-amz-algorithm": "AWS4-HMAC-SHA256", "x-amz-credential": "local-fixture-only",
            "x-amz-date": "20260911T120000Z", policy: "local-fixture-policy", "x-amz-signature": "local-fixture-signature",
          },
        },
      });
      return;
    }
    const upload = uploads.find(item => pathname === `/v1/uploads/${item.upload_id}/complete`);
    if (upload !== undefined && request.method() === "POST") {
      completeJobs.add(upload.job_id);
      await json(route, { upload: { upload_id: upload.upload_id, job_id: upload.job_id, status: "completed", record_version: 2 }, authorization: null });
      return;
    }
    unexpectedWrites += 1;
    await route.abort("blockedbyclient");
  };
  const jobRoute = async route => {
    const request = route.request();
    const parts = publicPath(request.url()).split("/");
    const jobId = parts[3];
    if (request.method() !== "GET") {
      unexpectedWrites += 1;
      await route.abort("blockedbyclient");
      return;
    }
    const value = projection(jobId);
    if (parts.length === 4) {
      progressReads.set(jobId, (progressReads.get(jobId) ?? 0) + 1);
      await json(route, Object.fromEntries(progressKeys.map(key => [key, value[key]])));
    } else if (parts[4] === "review") {
      await json(route, value, value.review_authority_etag ? { ETag: `"${value.review_authority_etag}"` } : {});
    } else if (parts[4] === "artwork-preview") {
      await json(route, { url: `${storageOrigin}/private/owners/${"a".repeat(64)}/jobs/${jobId}/source/source.png?versionId=local-navigation-source`, expires_at: "2030-01-01T00:00:00Z" });
    } else {
      // Publication is deliberately absent from these preparation-only fixtures.
      await route.fulfill({ status: 404, contentType: "application/json", body: JSON.stringify({ error: { code: "NOT_FOUND", message: "Local preparation fixture only.", request_id: "request-navigation-fixture" } }) });
    }
  };
  const storageRoute = async route => {
    const request = route.request();
    if (request.method() === "POST") storageWrites += 1;
    credentialsAbsent = credentialsAbsent && !request.headers().authorization && !request.headers().cookie;
    await route.fulfill({ status: request.method() === "GET" ? 200 : 204,
      headers: { "Access-Control-Allow-Origin": publicOrigin, "Access-Control-Allow-Methods": "GET,POST,OPTIONS" },
      ...(request.method() === "GET" ? { contentType: "image/png", body: image } : { body: "" }) });
  };
  const waitUntil = async predicate => {
    const deadline = Date.now() + 10000;
    while (Date.now() < deadline) {
      if (predicate()) return;
      await page.waitForTimeout(50);
    }
    throw new Error("Workspace navigation gate: fixture handoff did not complete");
  };
  const waitForTitle = title => page.waitForFunction(expected => document.querySelector("#listing-title")?.value === expected, title);
  const home = () => page.getByRole("link", { name: "Dashboard", exact: true });
  const choose = names => page.locator('input[type="file"][name="artwork"]').setInputFiles(names.map(name => ({ name, mimeType: "image/png", buffer: image })));
  await page.route(`${publicOrigin}/v1/uploads**`, uploadRoute);
  await page.route(`${publicOrigin}/v1/jobs/job_navigation_**`, jobRoute);
  await page.route(`${storageOrigin}/**`, storageRoute);

  try {
    await home().click();
    await page.getByRole("heading", { name: "Let’s start with your artwork." }).waitFor();
    await choose(["moon-moth.png", "garden-fern.png"]);
    await page.getByRole("button", { name: "Prepare 2 listings", exact: true }).click();
    await waitUntil(() => completeJobs.size === 2 && (progressReads.get("job_navigation_1") ?? 0) > 0 && (progressReads.get("job_navigation_2") ?? 0) > 0);
    check(publicPath(page.url()) === "/", "upload completion opened a listing before review readiness");
    await page.waitForTimeout(3250);
    check(publicPath(page.url()) === "/", "preparing status routed away from the upload dashboard");
    readyJobs.add("job_navigation_2");
    await page.waitForURL(`${publicOrigin}/jobs/job_navigation_2`, { timeout: 10000 });
    await waitForTitle("Prepared garden-fern.png");
    const batch = page.getByRole("navigation", { name: "Listings in this batch" });
    await batch.locator("summary").click();
    check(await batch.getByRole("link", { name: "moon-moth.png", exact: true }).isVisible(), "the sibling listing is missing from the batch switcher");
    check(await batch.getByRole("link", { name: "garden-fern.png", exact: true }).getAttribute("aria-current") === "page", "the current batch listing is not identified");
    await page.locator("#listing-title").fill("An unsaved garden fern title");
    await page.locator('.action-panel[data-listing-barrier="unsaved"]').waitFor();
    readyJobs.add("job_navigation_1");
    await page.waitForTimeout(3250);
    check(publicPath(page.url()) === "/jobs/job_navigation_2", "a later-ready sibling stole the active review");
    check(await page.locator("#listing-title").inputValue() === "An unsaved garden fern title", "background readiness replaced unsaved edits");
    await home().click();
    const leaveDialog = page.getByRole("dialog", { name: "Leave your unsaved changes?" });
    await leaveDialog.waitFor();
    check(await leaveDialog.getByRole("button", { name: "Stay here" }).evaluate(element => document.activeElement === element), "confirmation did not focus the safe Stay control");
    await page.keyboard.press("Escape");
    await leaveDialog.waitFor({ state: "hidden" });
    check(await home().evaluate(element => document.activeElement === element), "closing confirmation did not restore navigation focus");
    check(await page.locator("#listing-title").inputValue() === "An unsaved garden fern title", "staying discarded the listing edits");
    await batch.getByRole("link", { name: "moon-moth.png", exact: true }).click();
    await leaveDialog.getByRole("button", { name: "Leave without saving" }).click();
    await page.waitForURL(`${publicOrigin}/jobs/job_navigation_1`);
    await waitForTitle("Prepared moon-moth.png");
    await page.getByRole("link", { name: "Next listing: garden-fern.png", exact: true }).click();
    await waitForTitle("Prepared garden-fern.png");
    await page.screenshot({ path: "workspace-navigation-desktop.png", fullPage: true });
    await page.setViewportSize({ width: 360, height: 780 });
    const mobile = await page.evaluate(() => ({ viewport: innerWidth, document: document.documentElement.scrollWidth }));
    check(mobile.document <= mobile.viewport + 1, "the mobile header or batch switcher overflows the viewport");
    check(await home().isVisible(), "Dashboard is inaccessible on mobile");
    check(await page.getByRole("link", { name: "Upload artwork", exact: true }).isVisible(), "the Upload start-page link is inaccessible on mobile");
    await page.screenshot({ path: "workspace-navigation-mobile.png", fullPage: true });
    await page.getByRole("link", { name: "Upload artwork", exact: true }).click();
    await page.waitForURL(`${publicOrigin}/`);
    await page.waitForTimeout(3250);
    check(publicPath(page.url()) === "/", "returning to Upload replayed the automatic navigation");
    await page.getByRole("button", { name: "Choose another batch", exact: true }).click();

    // Explicitly leaving Home consumes automatic navigation for a new pending batch.
    await choose(["manual-navigation.png"]);
    await page.getByRole("button", { name: "Prepare 1 listing", exact: true }).click();
    await waitUntil(() => completeJobs.has("job_navigation_3"));
    await page.locator('a[href="/jobs/job_route_b"]').first().click();
    await waitForTitle("Current route B artwork");
    await home().click();
    readyJobs.add("job_navigation_3");
    await page.waitForTimeout(3250);
    check(publicPath(page.url()) === "/", "a manually visited listing did not cancel the pending automatic hop");
    await page.getByRole("button", { name: "Choose another batch", exact: true }).click();
    const state = await (await page.request.get(`${fixtureOrigin}/__fixture__/state`)).json();
    check(storageWrites === 3 && completeJobs.size === 3, "the local upload/verification lifecycle was incomplete");
    check(credentialsAbsent, "seller credentials reached the synthetic upload origin");
    check(unexpectedWrites === 0 && state.provider_transport_attempts === 0, "navigation attempted a store/provider mutation");
    await page.locator(`a[href="${originalPath}"]`).first().click();
    await page.waitForURL(`${publicOrigin}${originalPath}`);
    return {
      uploadCompletionDoesNotNavigate: "passed", firstReadyListingAutoOpen: "passed",
      oneAutomaticHopPerBatch: "passed", manualNavigationCancelsAutoOpen: "passed",
      batchSiblingNavigation: "passed", unsavedEditConfirmationAndFocus: "passed",
      isolatedListingDrafts: "passed", dashboardAndUploadLinks: "passed", mobileNavigationLayout: "passed",
      syntheticUploads: storageWrites, providerTransportAttempts: state.provider_transport_attempts,
    };
  } finally {
    await page.setViewportSize(originalViewport);
    await page.unroute(`${publicOrigin}/v1/uploads**`, uploadRoute);
    await page.unroute(`${publicOrigin}/v1/jobs/job_navigation_**`, jobRoute);
    await page.unroute(`${storageOrigin}/**`, storageRoute);
  }
}
