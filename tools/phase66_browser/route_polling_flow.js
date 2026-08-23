async page => {
  const check = (condition, message) => {
    if (!condition) throw new Error(`Phase 6.6 route/polling gate: ${message}`);
  };
  const { fixtureOrigin } = page.__phase66;
  const internalNavigate = async path => {
    await page.evaluate(nextPath => {
      history.pushState(null, "", nextPath);
      dispatchEvent(new PopStateEvent("popstate"));
    }, path);
  };
  const fixtureState = async () => (await (await page.request.get(`${fixtureOrigin}/__fixture__/state`)).json());
  const waitForProgressAbove = async previous => {
    const deadline = Date.now() + 5000;
    while (Date.now() < deadline) {
      const state = await fixtureState();
      if ((state.progress_requests.job_polling ?? 0) > previous) return;
      await page.waitForTimeout(50);
    }
    throw new Error("Phase 6.6 route/polling gate: polling did not resume");
  };

  const slowRequest = page.waitForRequest(request => request.url().includes("/v1/jobs/job_route_a/review"));
  await internalNavigate("/jobs/job_route_a");
  await slowRequest;
  await internalNavigate("/jobs/job_route_b");
  await page.getByRole("heading", { name: "Current route B artwork" }).waitFor();
  await page.waitForTimeout(950);
  check(page.url().endsWith("/jobs/job_route_b"), "the delayed A response changed the current route");
  check(await page.getByRole("heading", { name: "Current route B artwork" }).isVisible(), "route B evidence disappeared");
  check(await page.getByText("Delayed route A artwork", { exact: true }).count() === 0, "route A data contaminated route B");

  await internalNavigate("/jobs/job_polling");
  await page.getByRole("heading", { name: "Listing preparation" }).waitFor();
  const beforeOffline = await fixtureState();
  const initialProgress = beforeOffline.progress_requests.job_polling ?? 0;
  await page.context().setOffline(true);
  await page.waitForTimeout(3300);
  const whileOffline = await fixtureState();
  check((whileOffline.progress_requests.job_polling ?? 0) === initialProgress, "polling continued while offline");
  await page.context().setOffline(false);
  await waitForProgressAbove(initialProgress);

  const beforeHidden = await fixtureState();
  const visibleProgress = beforeHidden.progress_requests.job_polling ?? 0;
  await page.evaluate(() => {
    window.__phase66Visibility = "hidden";
    Object.defineProperty(document, "visibilityState", {
      configurable: true,
      get: () => window.__phase66Visibility,
    });
    document.dispatchEvent(new Event("visibilitychange"));
  });
  await page.waitForTimeout(3300);
  const whileHidden = await fixtureState();
  check((whileHidden.progress_requests.job_polling ?? 0) === visibleProgress, "polling continued while hidden");
  await page.evaluate(() => {
    window.__phase66Visibility = "visible";
    document.dispatchEvent(new Event("visibilitychange"));
  });
  await waitForProgressAbove(visibleProgress);

  return {
    routeAtoBIsolation: "passed",
    offlinePollingSuppressed: "passed",
    hiddenPollingSuppressed: "passed",
    resumePolling: "passed",
  };
}
