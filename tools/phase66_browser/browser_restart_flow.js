async page => {
  const check = (condition, message) => {
    if (!condition) throw new Error(`Phase 6.6 browser-restart gate: ${message}`);
  };
  const { fixtureOrigin, publicOrigin, cognitoOrigin } = page.__phase66;
  const protectedPath = "/jobs/job_browser_fixture";

  await page.goto(`${publicOrigin}${protectedPath}`);
  await page.getByRole("heading", { name: "Restore your seller session." }).waitFor();
  const popupOpened = page.waitForEvent("popup");
  await page.getByRole("button", { name: "Continue securely" }).click();
  const signInPopup = await popupOpened;
  await signInPopup.waitForURL(`${cognitoOrigin}/oauth2/authorize**`);
  const state = await signInPopup.evaluate(() => new URL(location.href).searchParams.get("state"));
  check(state !== null, "the restarted browser omitted OAuth state");
  const popupClosed = signInPopup.waitForEvent("close");
  await signInPopup.getByRole("button", { name: "Complete fixture sign-in", exact: true }).click();
  await popupClosed;
  await page.waitForURL(`${publicOrigin}${protectedPath}`);
  await page.waitForFunction(() => (
    document.querySelector("#listing-title")?.value === "Moonlit botanical moth shirt"
  ));
  await page.getByText("Approved", { exact: true }).waitFor();
  const workflow = page.getByRole("navigation", { name: "Listing workflow" });
  await page.waitForFunction(() => (
    document.querySelector('.workflow [aria-current="step"]')?.textContent?.includes("Publish") === true
  ));
  check(await workflow.locator('[aria-current="step"]').count() === 1, "restart did not retain exactly one current workflow step");
  const readOnlyListing = await page.locator('#listing-title, #listing-description, input[id^="listing-tag-"]').evaluateAll(fields => (
    fields.length === 15 && fields.every(field => (
      (field instanceof HTMLInputElement || field instanceof HTMLTextAreaElement) && field.readOnly
    ))
  ));
  check(readOnlyListing, "restart reopened editing of the approved listing");
  await page.getByRole("heading", { name: "Review approved", exact: true }).waitFor();
  check(
    await page.getByRole("button", { name: "Approve draft", exact: true }).count() === 0,
    "browser restart resurrected approval authority",
  );
  const stored = await page.evaluate(() => ({
    local: Object.keys(localStorage),
    session: Object.keys(sessionStorage),
  }));
  check(stored.local.length === 0 && stored.session.length === 0, "restart left browser storage");
  const fixtureState = await (await page.request.get(`${fixtureOrigin}/__fixture__/state`)).json();
  check(fixtureState.approval_committed === true, "browser restart lost the durable approval");
  check(fixtureState.approval_attempts === 1, "browser restart repeated approval");
  check(fixtureState.provider_transport_attempts === 0, "browser restart invoked provider transport");
  check(fixtureState.artwork_credentials_absent, "browser restart leaked seller credentials to the artwork host");

  return {
    browserRestartRecovery: "passed",
    durableApprovedRecovery: "passed",
    approvedWorkflowAndReadOnlyFields: "passed",
    approvalAttempts: 1,
    providerTransportAttempts: 0,
  };
}
