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
  check(await recoveryTab.getByRole("button", { name: "Approve draft" }).isDisabled(), "tab recovery resurrected approval authority");
  await recoveryTab.close();

  return {
    authRouteRecovery: "passed",
    brandArtworkLoaded: "passed",
    workflowSteps: "passed",
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
