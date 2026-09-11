async page => {
  const check = (condition, message) => {
    if (!condition) throw new Error(`Phase 6.6 auth/review gate: ${message}`);
  };
  const { fixtureOrigin, publicOrigin, cognitoOrigin } = page.__phase66;
  const protectedPath = "/jobs/job_browser_fixture";

  await page.goto(`${publicOrigin}${protectedPath}`);
  await page.getByRole("heading", { name: "Restore your seller session." }).waitFor();
  await page.getByRole("button", { name: "Continue securely" }).click();
  await page.waitForURL(`${cognitoOrigin}/oauth2/authorize**`);
  const authorization = await page.evaluate(() => ({
    state: new URL(location.href).searchParams.get("state"),
    challengeMethod: new URL(location.href).searchParams.get("code_challenge_method"),
    redirectUri: new URL(location.href).searchParams.get("redirect_uri"),
  }));
  const state = authorization.state;
  check(state !== null, "the authorization request omitted state");
  check(authorization.challengeMethod === "S256", "PKCE is not S256");
  check(authorization.redirectUri === `${publicOrigin}/auth/callback`, "redirect URI drifted");

  await page.goto(`${publicOrigin}/auth/callback?code=one-use&state=${encodeURIComponent(state)}`);
  await page.waitForURL(`${publicOrigin}${protectedPath}`);
  await page.waitForFunction(() => (
    document.querySelector("#listing-title")?.value === "Moonlit botanical moth shirt"
  ));
  await page.getByAltText("Original uploaded artwork for this seller review").waitFor({ state: "visible" });
  await page.getByAltText("Black shirt with moon moth artwork").waitFor({ state: "visible" });
  const stored = await page.evaluate(() => ({
    local: Object.keys(localStorage),
    session: Object.keys(sessionStorage),
    serialized: `${JSON.stringify(localStorage)}${JSON.stringify(sessionStorage)}`,
  }));
  check(stored.local.length === 0 && stored.session.length === 0, "browser storage was not empty after callback");
  check(!/phase66-(?:access|refresh)-token/u.test(stored.serialized), "a token reached browser storage");

  const brand = page.getByRole("link", { name: "Mr. Lister seller review home" });
  check(await brand.isVisible(), "the branded home link is absent");
  await page.waitForFunction(() => {
    const icon = document.querySelector(".brand-icon");
    return icon instanceof HTMLImageElement && icon.complete && icon.naturalWidth > 0;
  });
  const workflow = page.getByRole("navigation", { name: "Listing workflow" });
  check(await workflow.isVisible(), "the listing workflow is absent");
  const steps = await workflow.locator("li").allTextContents();
  check(steps.length === 3 && ["Upload", "Review", "Publish"].every((step, index) => steps[index].includes(step)), "workflow steps are missing or out of order");
  check(await workflow.locator('[aria-current="step"]').count() === 1, "workflow must identify exactly one current step");
  check((await workflow.locator('[aria-current="step"]').textContent()).includes("Review"), "the ready draft is not in the Review step");

  const layout = await page.evaluate(() => {
    const rectangle = element => {
      if (!(element instanceof HTMLElement)) return null;
      const bounds = element.getBoundingClientRect();
      return { x: bounds.x, right: bounds.right, top: bounds.top, bottom: bounds.bottom, width: bounds.width, height: bounds.height };
    };
    const navigation = document.querySelector('nav[aria-label="Listing workflow"]');
    const grid = document.querySelector(".review-grid");
    const actionBar = document.querySelector(".action-panel");
    const activityPanel = document.querySelector(".activity-panel");
    const progress = document.querySelector('[aria-label="Preparation milestones"]');
    const milestoneItems = [...(progress?.querySelectorAll("li") ?? [])];
    return {
      viewportWidth: window.innerWidth,
      navigation: rectangle(navigation),
      steps: [...(navigation?.querySelectorAll("li") ?? [])].map(rectangle),
      artworkRail: rectangle(document.querySelector(".review-preview-rail")),
      editor: rectangle(document.querySelector(".review-editor-column")),
      progress: rectangle(progress),
      milestones: milestoneItems.map(rectangle),
      ruledMilestones: milestoneItems.every(item => Number.parseFloat(getComputedStyle(item).borderTopWidth) > 0),
      actionBarFollowsReview: grid?.nextElementSibling === actionBar,
      activityFollowsActionBar: actionBar?.nextElementSibling === activityPanel,
    };
  });
  check(layout.actionBarFollowsReview && layout.activityFollowsActionBar, "review, action bar, and activity are not in the approved order");
  if (layout.viewportWidth >= 900) {
    check(layout.navigation !== null && layout.steps.length === 3 && layout.steps.every(step => step !== null), "desktop workflow geometry is unavailable");
    const widths = layout.steps.map(step => step.width);
    check(Math.max(...widths) - Math.min(...widths) < 2, "desktop workflow tabs do not have equal widths");
    check(Math.abs(layout.steps[0].x - layout.navigation.x) < 8
      && Math.abs(layout.steps[2].right - layout.navigation.right) < 8, "workflow tabs do not span the workspace");
    check(layout.artworkRail !== null && layout.editor !== null, "the desktop artwork and editor columns are absent");
    const artworkShare = layout.artworkRail.width / (layout.artworkRail.width + layout.editor.width);
    check(Math.abs(artworkShare - 0.375) <= 0.03, "the artwork/editor proportions drifted from the approved layout");
    check(Math.abs(layout.artworkRail.top - layout.editor.top) < 2, "the artwork and editor no longer align at the top");
    check(layout.progress !== null && layout.milestones.length === 4 && layout.milestones.every(item => item !== null), "preparation milestones are incomplete");
    check(layout.ruledMilestones && layout.progress.height <= 65, "milestones no longer use compact horizontal rules");
    check(Math.abs(layout.milestones[0].x - layout.progress.x) < 2
      && Math.abs(layout.milestones[3].right - layout.progress.right) < 2
      && layout.milestones.every(item => Math.abs(item.top - layout.progress.top) < 2), "the milestone row became an inset card");
  }
  check(await page.getByRole("button", { name: "Save listing revision" }).count() === 0, "an unchanged listing exposes a redundant Save action");
  const listingTitle = page.locator("#listing-title");
  const originalTitle = await listingTitle.inputValue();
  await listingTitle.fill(`${originalTitle} — local edit`);
  await page.locator('.action-panel[data-listing-barrier="unsaved"]').waitFor({ state: "visible", timeout: 5000 });
  await page.getByRole("button", { name: "Save listing revision" }).waitFor({ state: "visible" });
  check(await page.evaluate(() => {
    const save = document.querySelector('#review-edit-actions button[type="submit"]');
    return save instanceof HTMLButtonElement && save.form?.contains(document.querySelector("#listing-title")) === true;
  }), "the action-bar save control lost its listing form association");
  check(await page.getByRole("button", { name: "Approve draft", exact: true }).count() === 0, "local edits expose a competing approval action");
  await page.getByRole("button", { name: "Discard edits" }).click();
  await page.locator('.action-panel[data-listing-barrier="none"]').waitFor({ state: "visible", timeout: 5000 });
  await page.getByRole("button", { name: "Approve draft", exact: true }).waitFor({ state: "visible", timeout: 5000 });
  check(await listingTitle.inputValue() === originalTitle, "discard did not restore the saved listing title");
  check(await page.getByRole("button", { name: "Save listing revision" }).count() === 0, "discard did not restore the pristine action bar");
  check(await page.getByRole("button", { name: "Approve draft", exact: true }).isVisible(), "discard did not restore the approval control");

  const activity = page.locator("details.activity-panel");
  check(await activity.getAttribute("open") === null, "activity should start collapsed");
  await activity.locator(":scope > summary").click();
  const strands = activity.getByRole("heading", { name: "Prepared with Strands Agents" });
  check(await strands.isVisible(), "Strands evidence is absent from expanded activity");
  check(await activity.getByText("job_browser_fixture", { exact: true }).isVisible(), "activity does not identify the current job");
  check(await activity.getByText("strands-agents", { exact: true }).isVisible(), "the recorded framework is absent");
  check(await page.getByText("Validation: Passed", { exact: true }).isVisible(), "listing validation is not visible");
  check(await page.getByRole("textbox", { name: /^Tag /u }).count() === 13, "the review does not expose exactly 13 tags");
  check(await page.getByText("Nature lovers", { exact: true }).isVisible(), "prepared audience is absent");
  check(
    await page.getByRole("code").filter({ hasText: "record_prepared_review" }).isVisible(),
    "Strands tool evidence is absent",
  );

  await activity.locator(":scope > summary").click();
  check(await activity.getAttribute("open") === null, "activity could not be collapsed after inspection");
  check(!await strands.isVisible(), "collapsed activity still exposes its detailed content");

  const approve = page.getByRole("button", { name: "Approve draft" });
  await approve.waitFor({ state: "visible" });
  await page.waitForFunction(() => {
    const button = [...document.querySelectorAll("button")].find(item => item.textContent?.trim() === "Approve draft");
    return button instanceof HTMLButtonElement && !button.disabled;
  });
  await approve.click();
  const confirm = page.getByRole("button", { name: "Approve draft — keep unpublished" });
  check(await page.getByText("Approval does not publish to Etsy.", { exact: true }).isVisible(), "the approval confirmation omits the unpublished boundary");
  await confirm.click();
  await page.waitForFunction(() => {
    const button = [...document.querySelectorAll("dialog button")].find(item => item.textContent?.includes("Approving"));
    return button instanceof HTMLButtonElement && button.disabled;
  });
  await page.evaluate(() => {
    const button = [...document.querySelectorAll("dialog button")].find(item => item.textContent?.includes("Approving"));
    if (button instanceof HTMLButtonElement) button.click();
  });
  await page.getByText(/Action accepted, but the latest status is unavailable/u).waitFor();
  const actionStatusFocused = await page.evaluate(() => (
    document.activeElement?.getAttribute("role") === "status"
    && document.activeElement?.textContent?.includes("latest status is unavailable") === true
  ));
  check(actionStatusFocused, "focus did not move to the stale-readback status");
  check(await approve.isDisabled(), "approval unlocked before authoritative readback caught up");

  const stateAfterApproval = await (await page.request.get(`${fixtureOrigin}/__fixture__/state`)).json();
  check(stateAfterApproval.approval_attempts === 1, "the approval double-submit lock sent more than one command");
  check(stateAfterApproval.approval_if_match_valid, "approval did not bind the exact review ETag");
  check(stateAfterApproval.approval_idempotency_present, "approval omitted its idempotency key");
  check(stateAfterApproval.api_authorization_valid, "an API request lacked the in-memory bearer token");
  check(stateAfterApproval.artwork_requests > 0, "the pinned artwork image was not fetched");
  check(stateAfterApproval.artwork_credentials_absent, "seller credentials or a referrer reached the artwork host");
  check(stateAfterApproval.provider_transport_attempts === 0, "the offline browser gate invoked provider transport");
  const commerceControls = await page.getByRole("button", { name: /publish|order|fulfill|send.*etsy/iu }).count();
  check(commerceControls === 0, "a commerce action is exposed");

  const recoveryTab = await page.context().newPage();
  await recoveryTab.goto(`${publicOrigin}${protectedPath}`);
  await recoveryTab.getByRole("heading", { name: "Restore your seller session." }).waitFor();
  await recoveryTab.getByRole("button", { name: "Continue securely" }).click();
  await recoveryTab.waitForURL(`${cognitoOrigin}/oauth2/authorize**`);
  const recoveryState = await recoveryTab.evaluate(() => new URL(location.href).searchParams.get("state"));
  check(recoveryState !== null, "the recovery tab omitted OAuth state");
  await recoveryTab.goto(`${publicOrigin}/auth/callback?code=tab-recovery&state=${encodeURIComponent(recoveryState)}`);
  await recoveryTab.waitForURL(`${publicOrigin}${protectedPath}`);
  await recoveryTab.waitForFunction(() => (
    document.querySelector("#listing-title")?.value === "Moonlit botanical moth shirt"
  ));
  await recoveryTab.getByText("Approved", { exact: true }).waitFor();
  await recoveryTab.getByRole("heading", { name: "Review approved", exact: true }).waitFor();
  check(await recoveryTab.getByRole("button", { name: "Approve draft", exact: true }).count() === 0, "tab recovery resurrected approval authority");
  await recoveryTab.close();

  return {
    authRouteRecovery: "passed",
    brandArtworkLoaded: "passed",
    artworkGrantAndCredentialBoundary: "passed",
    workflowSteps: "passed",
    approvedLayoutGeometry: layout.viewportWidth >= 900 ? "passed" : "desktop viewport not used",
    unifiedReviewActionBar: "passed",
    collapsedStrandsProvenance: "passed",
    unpublishedBoundary: "passed",
    listingValidation: "passed",
    exactTagCount: 13,
    approvalAttempts: stateAfterApproval.approval_attempts,
    staleReadbackFocus: "passed",
    tabRecovery: "passed",
    commerceControls: 0,
    providerTransportAttempts: 0,
  };
}
