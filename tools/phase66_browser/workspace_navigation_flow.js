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
  const synchronizingJobs = new Set();
  const completeJobs = new Set();
  const heldCompletions = new Map();
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
    const synchronizing = !ready && synchronizingJobs.has(jobId);
    const value = clone(ready || synchronizing ? readyTemplate : pendingTemplate);
    value.job_id = jobId;
    value.created_at = uploads.find(upload => upload.job_id === jobId).created_at;
    value.updated_at = value.created_at;
    // A verified upload has a pinned source preview before AI/product work runs.
    value.preview = clone(readyTemplate.preview);
    if (value.preview.url !== null) value.preview.url = `${publicOrigin}/v1/jobs/${jobId}/artwork-preview`;
    if (ready || synchronizing) value.listing.title = `Prepared ${uploads.find(upload => upload.job_id === jobId).filename}`;
    if (synchronizing) {
      value.display_state = "synchronizing";
      value.stage = "product_sync";
      value.provider_outcome_unconfirmed = true;
      value.synchronization = clone(pendingTemplate.synchronization);
      value.mockups = clone(pendingTemplate.mockups);
      value.economics = clone(pendingTemplate.economics);
      value.actions = clone(pendingTemplate.actions);
    }
    if (ready) value.record_version += 1;
    // Mockups retain their known local fixture route. No real store is reachable.
    return value;
  };
  const uploadRoute = async route => {
    const request = route.request();
    const pathname = publicPath(request.url());
    if (pathname === "/v1/uploads" && request.method() === "POST") {
      const body = request.postDataJSON();
      const index = uploads.length + 1;
      const upload = { ...body, upload_id: `upload_navigation_${index}`, job_id: `job_navigation_${index}`, created_at: new Date().toISOString() };
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
      if (upload.job_id === "job_navigation_1" || upload.job_id === "job_navigation_3") {
        await new Promise(resolve => heldCompletions.set(upload.job_id, resolve));
        heldCompletions.delete(upload.job_id);
      }
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
  const recentTemplate = await (await page.request.get(`${fixtureOrigin}/v1/jobs`, { headers: fixtureHeaders })).json();
  // Keep interception narrow: a predicate installs a page-wide pattern that
  // WebKit can remove while the context proxy is still fulfilling image reads.
  const recentJobsPattern = `${publicOrigin}/v1/jobs?**`;
  const recentJobsRoute = async route => {
    if (route.request().method() !== "GET") {
      unexpectedWrites += 1;
      await route.abort("blockedbyclient");
      return;
    }
    await json(route, { ...recentTemplate, jobs: [
      ...uploads.filter(upload => completeJobs.has(upload.job_id)).map(upload => {
        const progress = projection(upload.job_id);
        return { job_id: upload.job_id, state: readyJobs.has(upload.job_id) ? "awaiting_approval" : synchronizingJobs.has(upload.job_id) ? "product_draft_syncing" : "analyzing_artwork",
          record_version: progress.record_version, review_version: progress.review_version,
          created_at: upload.created_at, updated_at: upload.created_at };
      }),
      ...recentTemplate.jobs,
    ] });
  };
  await page.route(recentJobsPattern, recentJobsRoute);
  await page.route(`${publicOrigin}/v1/uploads**`, uploadRoute);
  await page.route(`${publicOrigin}/v1/jobs/job_navigation_**`, jobRoute);
  await page.route(`${storageOrigin}/**`, storageRoute);

  try {
    await home().click();
    await page.getByRole("heading", { name: "Let’s start with your artwork." }).waitFor();
    await choose(["moon-moth.png", "garden-fern.png"]);
    await page.getByRole("button", { name: "Prepare 2 listings", exact: true }).click();
    await waitUntil(() => heldCompletions.has("job_navigation_1"));
    check(publicPath(page.url()) === "/", "unverified artwork opened a listing");
    heldCompletions.get("job_navigation_1")();
    await page.waitForURL(`${publicOrigin}/jobs/job_navigation_1`, { timeout: 10000 });
    await waitUntil(() => completeJobs.size === 2 && (progressReads.get("job_navigation_1") ?? 0) > 0 && (progressReads.get("job_navigation_2") ?? 0) > 0);
    await page.getByRole("heading", { name: "Your listing is taking shape." }).waitFor();
    await page.waitForFunction(() => {
      const artwork = document.querySelector("img.artwork-preview");
      return artwork?.complete && artwork.naturalWidth > 0;
    });
    check(readyJobs.size === 0, "artwork preview waited for AI or product completion");
    await page.getByRole("timer", { name: "Elapsed time since submission" }).waitFor();
    const elapsedBefore = await page.locator(".preparation-elapsed time").getAttribute("datetime");
    await page.emulateMedia({ reducedMotion: "no-preference" });
    check(await page.locator(".preparation-activity-label").evaluate(element => getComputedStyle(element).animationName) === "preparation-shimmer", "active preparation has no shimmer");
    await page.emulateMedia({ reducedMotion: "reduce" });
    check(await page.locator(".preparation-activity-label").evaluate(element => getComputedStyle(element).animationName) === "none", "reduced motion did not stop the shimmer");
    await page.emulateMedia({ reducedMotion: null });
    await page.screenshot({ path: "workspace-navigation-preparing.png", fullPage: true });
    await page.waitForTimeout(3250);
    check(await page.locator(".preparation-elapsed time").getAttribute("datetime") !== elapsedBefore, "elapsed preparation counter did not advance");
    check(publicPath(page.url()) === "/jobs/job_navigation_1", "background preparation changed the active listing");
    synchronizingJobs.add("job_navigation_1");
    await page.locator('.milestone--current').filter({ hasText: "Mockups prepared" }).waitFor();
    const mockupMilestone = page.locator('.milestone--current').filter({ hasText: "Mockups prepared" });
    check((await mockupMilestone.textContent()).includes("In progress"), "an in-flight provider request reverted mockup preparation to Waiting");
    check(await page.getByRole("timer").isVisible(), "the elapsed timer disappeared during normal provider work");
    check(await page.getByText(/The Printify outcome is not confirmed/u).count() === 0, "normal provider work showed a reconciliation warning");
    const saveDuringPreparation = page.getByRole("button", { name: "Save listing revision", exact: true });
    check(await saveDuringPreparation.count() === 0 || await saveDuringPreparation.isDisabled(), "activity enabled saving before preparation finished");
    const approveDuringPreparation = page.getByRole("button", { name: "Approve draft", exact: true });
    check(await approveDuringPreparation.count() === 0 || await approveDuringPreparation.isDisabled(), "activity enabled approval before preparation finished");
    await page.emulateMedia({ reducedMotion: "no-preference" });
    const line = await mockupMilestone.evaluate(element => {
      const style = getComputedStyle(element, "::before");
      return { animation: style.animationName, display: style.display, height: style.height, position: style.backgroundPosition };
    });
    check(line.animation === "preparation-shimmer" && line.display !== "none" && line.height === "3px", "the active mockup line is not animated and visible");
    await page.waitForTimeout(1100);
    check(await mockupMilestone.evaluate(element => getComputedStyle(element, "::before").backgroundPosition) !== line.position, "the mockup line animation did not advance");
    await page.screenshot({ path: "workspace-mockups-in-progress.png", fullPage: true });
    await page.emulateMedia({ reducedMotion: "reduce" });
    check(await mockupMilestone.evaluate(element => getComputedStyle(element, "::before").display) === "none", "reduced motion did not stop the mockup line");
    await page.emulateMedia({ reducedMotion: null });
    readyJobs.add("job_navigation_1");
    await page.getByRole("timer").waitFor({ state: "hidden" });
    check(await page.getByRole("timer").count() === 0, "completed preparation left a running timer");
    const batch = page.getByRole("navigation", { name: "Listings in this batch" });
    await batch.locator("summary").click();
    check(await batch.getByRole("link", { name: "garden-fern.png", exact: true }).isVisible(), "the sibling listing is missing from the batch switcher");
    check(await batch.getByRole("link", { name: "moon-moth.png", exact: true }).getAttribute("aria-current") === "page", "the current batch listing is not identified");
    await page.locator("#listing-title").fill("An unsaved moon moth title");
    await page.locator('.action-panel[data-listing-barrier="unsaved"]').waitFor();
    readyJobs.add("job_navigation_2");
    await page.waitForTimeout(3250);
    check(publicPath(page.url()) === "/jobs/job_navigation_1", "a later-ready sibling stole the active review");
    check(await page.locator("#listing-title").inputValue() === "An unsaved moon moth title", "background readiness replaced unsaved edits");
    await home().click();
    const leaveDialog = page.getByRole("dialog", { name: "Leave your unsaved changes?" });
    await leaveDialog.waitFor();
    check(await leaveDialog.getByRole("button", { name: "Stay here" }).evaluate(element => document.activeElement === element), "confirmation did not focus the safe Stay control");
    await page.keyboard.press("Escape");
    await leaveDialog.waitFor({ state: "hidden" });
    check(await home().evaluate(element => document.activeElement === element), "closing confirmation did not restore navigation focus");
    check(await page.locator("#listing-title").inputValue() === "An unsaved moon moth title", "staying discarded the listing edits");
    await batch.getByRole("link", { name: "garden-fern.png", exact: true }).click();
    await leaveDialog.getByRole("button", { name: "Leave without saving" }).click();
    await page.waitForURL(`${publicOrigin}/jobs/job_navigation_2`);
    await waitForTitle("Prepared garden-fern.png");
    await page.getByRole("link", { name: "Previous listing: moon-moth.png", exact: true }).click();
    await waitForTitle("Prepared moon-moth.png");
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
    check(await page.locator('input[type="file"][name="artwork"]').isEnabled(), "a prepared batch kept the Upload picker locked");
    check(await page.getByRole("button", { name: "Choose another batch", exact: true }).count() === 0, "returning to Upload required a manual batch reset");
    check(await page.getByRole("link", { name: "Open listing: moon-moth.png", exact: true }).isVisible(), "the first completed listing disappeared from Your listings");
    check(await page.getByRole("link", { name: "Open listing: garden-fern.png", exact: true }).isVisible(), "the second completed listing disappeared from Your listings");

    // Explicitly leaving Home consumes automatic navigation for a new pending batch.
    await choose(["manual-navigation.png"]);
    await page.getByRole("button", { name: "Prepare 1 listing", exact: true }).click();
    await waitUntil(() => heldCompletions.has("job_navigation_3"));
    await page.locator('a[href="/jobs/job_route_b"]').first().click();
    await waitForTitle("Current route B artwork");
    await home().click();
    heldCompletions.get("job_navigation_3")();
    await waitUntil(() => completeJobs.has("job_navigation_3"));
    readyJobs.add("job_navigation_3");
    await page.waitForTimeout(3250);
    check(publicPath(page.url()) === "/", "a manually visited listing did not cancel the pending automatic hop");
    check(await page.getByRole("button", { name: "Choose another batch", exact: true }).isVisible(), "background preparation cleared the queue while already on Home");
    await page.getByRole("link", { name: "Open listing", exact: true }).click();
    await waitForTitle("Prepared manual-navigation.png");
    await home().click();
    await page.waitForURL(`${publicOrigin}/`);
    await page.waitForFunction(() => document.querySelector('input[type="file"][name="artwork"]')?.disabled === false);
    check(await page.locator('input[type="file"][name="artwork"]').isEnabled(), "Dashboard did not start a fresh selection after preparation");
    check(await page.getByRole("button", { name: "Choose another batch", exact: true }).count() === 0, "returning through Dashboard required a manual batch reset");
    await page.getByRole("link", { name: "Open listing: manual-navigation.png", exact: true }).waitFor();
    const state = await (await page.request.get(`${fixtureOrigin}/__fixture__/state`)).json();
    check(storageWrites === 3 && completeJobs.size === 3, "the local upload/verification lifecycle was incomplete");
    check(credentialsAbsent, "seller credentials reached the synthetic upload origin");
    check(unexpectedWrites === 0 && state.provider_transport_attempts === 0, "navigation attempted a store/provider mutation");
    await page.locator(`a[href="${originalPath}"]`).first().click();
    await page.waitForURL(`${publicOrigin}${originalPath}`);
    await page.waitForFunction(() => {
      const artwork = document.querySelector("img.artwork-preview");
      return artwork?.complete && artwork.naturalWidth > 0;
    });
    return {
      unverifiedUploadDoesNotNavigate: "passed", verifiedUploadOpensWorkspace: "passed",
      artworkPreviewBeforeListingReady: "passed",
      preparationShimmerAndElapsedTime: "passed", reducedMotionActivity: "passed",
      unconfirmedProductRequestKeepsAnimatedMilestone: "passed",
      oneAutomaticHopPerBatch: "passed", manualNavigationCancelsAutoOpen: "passed",
      preparedBatchStartsFreshOnReturn: "passed", completedListingsRemainAccessible: "passed",
      backgroundBatchRemainsVisible: "passed",
      batchSiblingNavigation: "passed", unsavedEditConfirmationAndFocus: "passed",
      isolatedListingDrafts: "passed", dashboardAndUploadLinks: "passed", mobileNavigationLayout: "passed",
      syntheticUploads: storageWrites, providerTransportAttempts: state.provider_transport_attempts,
    };
  } finally {
    await page.setViewportSize(originalViewport);
    await page.unroute(`${publicOrigin}/v1/uploads**`, uploadRoute);
    await page.unroute(`${publicOrigin}/v1/jobs/job_navigation_**`, jobRoute);
    await page.unroute(`${storageOrigin}/**`, storageRoute);
    await page.unroute(recentJobsPattern, recentJobsRoute);
  }
}
