async page => {
  const check = (condition, message) => {
    if (!condition) throw new Error(`Phase 6.6 browser-restart gate: ${message}`);
  };
  const { fixtureOrigin, publicOrigin, cognitoOrigin } = page.__phase66;
  const protectedPath = "/jobs/job_browser_fixture";

  await page.goto(`${publicOrigin}${protectedPath}`);
  await page.getByRole("heading", { name: "Restore your seller session." }).waitFor();
  await page.getByRole("button", { name: "Continue securely" }).click();
  await page.waitForURL(`${cognitoOrigin}/oauth2/authorize**`);
  const state = await page.evaluate(() => new URL(location.href).searchParams.get("state"));
  check(state !== null, "the restarted browser omitted OAuth state");
  await page.goto(
    `${publicOrigin}/auth/callback?code=browser-restart&state=${encodeURIComponent(state)}`,
  );
  await page.waitForURL(`${publicOrigin}${protectedPath}`);
  await page.getByRole("heading", { name: "Moonlit botanical moth shirt" }).waitFor();
  await page.getByText("Approved", { exact: true }).waitFor();
  await page.getByText(/Current stage:\s*Complete/u).waitFor();
  check(
    await page.getByRole("button", { name: "Approve draft" }).isDisabled(),
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

  return {
    browserRestartRecovery: "passed",
    durableApprovedRecovery: "passed",
    approvalAttempts: 1,
    providerTransportAttempts: 0,
  };
}
