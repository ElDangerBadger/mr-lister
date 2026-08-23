async page => {
  const fixtureOrigin = page.url().split("/__fixture__/")[0];
  const health = await (await page.request.get(`${fixtureOrigin}/__fixture__/health`)).json();
  const publicOrigin = health.public_origin;
  const cognitoOrigin = "https://phase66.auth.us-west-2.amazoncognito.com";
  const proxy = async route => {
    const requested = route.request().url();
    const authorityEnd = requested.indexOf("/", requested.indexOf("://") + 3);
    const pathAndQuery = authorityEnd === -1 ? "/" : requested.slice(authorityEnd);
    const upstream = await route.fetch({ url: `${fixtureOrigin}${pathAndQuery}` });
    await route.fulfill({ response: upstream });
  };
  await page.context().route(`${publicOrigin}/**`, proxy);
  await page.context().route(`${cognitoOrigin}/**`, proxy);
  await page.context().route("https://images.printify.com/**", proxy);
  await page.context().route("https://api.printify.com/**", async route => {
    await page.request.post(`${fixtureOrigin}/__fixture__/provider-transport-attempt`);
    await route.abort("blockedbyclient");
  });
  await page.evaluate(async () => {
    try {
      await fetch("https://api.printify.com/phase66-browser-boundary-probe");
    }
    catch {
      // The route must be aborted after the fixture records the sentinel probe.
    }
  });
  const sentinelState = await (await page.request.get(`${fixtureOrigin}/__fixture__/state`)).json();
  if (sentinelState.provider_transport_attempts !== 1) {
    throw new Error("Phase 6.6 provider transport sentinel did not observe the blocked probe");
  }
  await page.request.post(`${fixtureOrigin}/__fixture__/reset`);
  page.__phase66 = { fixtureOrigin, publicOrigin, cognitoOrigin };
  return {
    fixtureReady: true,
    proxyRoutes: 4,
    providerSentinelVerified: true,
  };
}
