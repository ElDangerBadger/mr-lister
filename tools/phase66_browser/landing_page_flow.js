async page => {
  const check = (condition, message) => {
    if (!condition) throw new Error(`Phase 6.6 landing gate: ${message}`);
  };
  const { fixtureOrigin, publicOrigin, cognitoOrigin } = page.__phase66;
  const originalViewport = page.viewportSize();
  const logoName = "Mr. Lister, the friendly price-tag character, holding a product listing";
  const themeKey = "mr-lister-display-theme";
  const heading = () => page.getByRole("heading", { level: 1, name: /Your artwork\.\s*Your next listing\s*\./u });
  const chooseTheme = async label => {
    await page.getByRole("button", { name: /^Display theme:/u }).click();
    await page.getByRole("menuitemradio", { name: label, exact: true }).click();
    check(await page.getByRole("button", { name: `Display theme: ${label}`, exact: true }).isVisible(), `${label} did not remain selected`);
  };
  const resolvedTheme = async theme => {
    await page.waitForFunction(expected => document.documentElement.dataset.theme === expected, theme);
  };
  const visibleLogo = async () => {
    const logo = page.getByRole("img", { name: logoName, exact: true });
    check(await logo.count() === 1, "the resolved display theme must expose one logo");
    await logo.evaluate(image => image.decode());
    const geometry = await logo.boundingBox();
    check(geometry !== null && geometry.width > 0 && geometry.height > 0, "the selected logo is not rendered");
    return { ...geometry, src: await logo.getAttribute("src") };
  };

  await page.setViewportSize({ width: 1280, height: 900 });
  await page.emulateMedia({ colorScheme: "light" });
  await page.goto(`${publicOrigin}/`);
  await heading().waitFor();
  await page.evaluate(() => document.fonts.ready);
  check(await page.getByRole("main").count() === 1, "public landing must have one main landmark");
  check(await page.getByRole("button", { name: "Choose artwork", exact: true }).count() === 0, "anonymous landing exposes the upload workspace");
  const anonymousState = await (await page.request.get(`${fixtureOrigin}/__fixture__/state`)).json();
  check(anonymousState.request_log.length === 0, "the anonymous landing read private APIs");
  check(anonymousState.provider_transport_attempts === 0, "the anonymous landing invoked provider transport");

  await chooseTheme("Light");
  await resolvedTheme("light");
  const lightLogo = await visibleLogo();
  await page.screenshot({ path: "landing-light-desktop.png", fullPage: true, animations: "disabled" });
  await page.emulateMedia({ colorScheme: "dark" });
  await resolvedTheme("light");

  await chooseTheme("Dark");
  await resolvedTheme("dark");
  const darkLogo = await visibleLogo();
  check(lightLogo.src !== darkLogo.src, "Light and Dark reuse the same logo asset");
  check(Math.abs(lightLogo.width - darkLogo.width) < 1 && Math.abs(lightLogo.height - darkLogo.height) < 1, "switching display theme shifts the logo layout");
  check(Math.abs(darkLogo.width - darkLogo.height) < 1, "the square logo has been distorted");
  await page.screenshot({ path: "landing-dark-desktop.png", fullPage: true, animations: "disabled" });
  await page.emulateMedia({ colorScheme: "light" });
  await resolvedTheme("dark");

  await chooseTheme("Auto");
  await resolvedTheme("light");
  check((await visibleLogo()).src === lightLogo.src, "Auto does not select the light logo with a light system theme");
  await page.emulateMedia({ colorScheme: "dark" });
  await resolvedTheme("dark");
  check((await visibleLogo()).src === darkLogo.src, "Auto did not react to a system theme change");
  check(await page.getByRole("button", { name: "Display theme: Auto", exact: true }).isVisible(), "Auto was replaced by a fixed theme after a system change");

  await page.setViewportSize({ width: 360, height: 800 });
  const narrow = await page.evaluate(() => ({
    width: document.documentElement.clientWidth,
    scrollWidth: document.documentElement.scrollWidth,
    clippedControls: [...document.querySelectorAll("a,button")].filter(element => {
      const bounds = element.getBoundingClientRect();
      return bounds.width > 0 && (bounds.left < -1 || bounds.right > document.documentElement.clientWidth + 1);
    }).length,
  }));
  check(narrow.width === 360 && narrow.scrollWidth <= narrow.width + 1, "the landing page overflows at 360 CSS pixels");
  check(narrow.clippedControls === 0, "landing controls are clipped at 360 CSS pixels");
  await visibleLogo();
  await page.screenshot({ path: "landing-dark-mobile.png", fullPage: true, animations: "disabled" });
  await chooseTheme("Light");
  await resolvedTheme("light");
  await visibleLogo();
  await page.screenshot({ path: "landing-light-mobile.png", fullPage: true, animations: "disabled" });
  await chooseTheme("Auto");
  await resolvedTheme("dark");
  await page.setViewportSize({ width: 1280, height: 900 });

  const popupOpened = page.waitForEvent("popup");
  await page.getByRole("main").getByRole("button", { name: "Open seller workspace", exact: true }).click();
  const popup = await popupOpened;
  await popup.waitForURL(`${cognitoOrigin}/oauth2/authorize**`);
  check(page.url() === `${publicOrigin}/`, "landing sign-in replaced the original page before authentication");
  const popupClosed = popup.waitForEvent("close");
  await popup.getByRole("button", { name: "Complete fixture sign-in", exact: true }).click();
  await popupClosed;
  await page.getByRole("heading", { name: "Let’s start with your artwork.", exact: true }).waitFor();
  check(page.url() === `${publicOrigin}/`, "landing sign-in did not return to the local upload workspace");
  check(await page.getByRole("link", { name: "Dashboard", exact: true }).isVisible(), "authenticated workspace navigation is missing");
  check(await heading().count() === 0, "marketing content remained over the authenticated workspace");
  check(await page.getByRole("button", { name: "Display theme: Auto", exact: true }).isVisible(), "sign-in reset the display preference");
  await resolvedTheme("dark");

  await page.getByRole("button", { name: "Sign out", exact: true }).click();
  await page.waitForURL(`${cognitoOrigin}/logout**`);
  const logoutReturn = await page.evaluate(() => new URL(location.href).searchParams.get("logout_uri"));
  check(logoutReturn === `${publicOrigin}/`, "sign-out does not return to the official landing page");
  // The isolated hosted-sign-in fixture ends at its logout page. Follow the actual
  // configured return URI to model the provider's final redirect without a live account.
  await page.goto(logoutReturn);
  await heading().waitFor();
  await resolvedTheme("dark");
  check(await page.getByRole("button", { name: "Display theme: Auto", exact: true }).isVisible(), "sign-out reset the display preference");
  const stored = await page.evaluate(() => ({
    local: Object.entries(localStorage),
    session: Object.entries(sessionStorage),
  }));
  check(JSON.stringify(stored.local) === JSON.stringify([[themeKey, "auto"]]), "landing persisted data other than the display preference");
  check(stored.session.length === 0, "landing sign-in left session storage behind");

  // Existing auth/review coverage begins with intentionally empty browser storage.
  // Remove only the preference introduced by this scenario, never an unknown key.
  await page.evaluate(key => { localStorage.removeItem(key); }, themeKey);
  await page.emulateMedia({ colorScheme: "light" });
  await page.setViewportSize(originalViewport ?? { width: 1280, height: 720 });
  return {
    anonymousPrivateApiRequests: 0,
    lightDarkAndAuto: "passed",
    autoSystemChange: "passed",
    matchingLogoGeometry: "passed",
    mobileReflow: "passed at 360 CSS pixels",
    popupToAuthenticatedUpload: "passed",
    signOutLandingReturn: "passed",
    sharedDisplayPreference: "passed",
    providerTransportAttempts: 0,
  };
}
