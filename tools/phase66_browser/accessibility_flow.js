async page => {
  const check = (condition, message) => {
    if (!condition) throw new Error(`Phase 6.6 accessibility gate: ${message}`);
  };
  await page.evaluate(() => {
    history.pushState(null, "", "/jobs/job_browser_fixture");
    dispatchEvent(new PopStateEvent("popstate"));
  });
  await page.getByRole("heading", { name: "Moonlit botanical moth shirt" }).waitFor();
  await page.getByAltText("Original uploaded artwork for this seller review").waitFor({ state: "visible" });

  await page.setViewportSize({ width: 360, height: 800 });
  const narrow = await page.evaluate(() => ({
    clientWidth: document.documentElement.clientWidth,
    scrollWidth: document.documentElement.scrollWidth,
    controlsOutsideViewport: [...document.querySelectorAll("a,button,input,textarea,summary")].filter(element => {
      const box = element.getBoundingClientRect();
      return box.left < -1 || box.right > document.documentElement.clientWidth + 1;
    }).length,
  }));
  check(narrow.clientWidth === 360, "the narrow viewport was not applied");
  check(narrow.scrollWidth <= narrow.clientWidth + 1, "the 360 CSS-pixel view has horizontal overflow");
  check(narrow.controlsOutsideViewport === 0, "an interactive control is clipped at 360 CSS pixels");

  await page.emulateMedia({ reducedMotion: "reduce" });
  const reducedMotion = await page.evaluate(() => {
    const duration = getComputedStyle(document.querySelector(".skip-link")).transitionDuration;
    const value = Number.parseFloat(duration);
    return {
      matches: matchMedia("(prefers-reduced-motion: reduce)").matches,
      transitionDurationMs: duration.endsWith("ms") ? value : value * 1000,
    };
  });
  check(reducedMotion.matches, "reduced-motion emulation did not apply");
  check(reducedMotion.transitionDurationMs <= 0.01, "motion was not reduced");

  await page.emulateMedia({ reducedMotion: "reduce", forcedColors: "active" });
  const forcedColors = await page.evaluate(() => ({
    matches: matchMedia("(forced-colors: active)").matches,
    bannerBorder: getComputedStyle(document.querySelector(".authority-banner")).borderTopWidth,
    primaryBorder: getComputedStyle(document.querySelector(".button--primary")).borderTopWidth,
  }));
  check(forcedColors.matches, "forced-colors emulation did not apply");
  check(forcedColors.bannerBorder !== "0px", "the authority banner loses its boundary in forced colors");
  check(forcedColors.primaryBorder !== "0px", "the primary action loses its boundary in forced colors");

  return {
    reflowAt200PercentEquivalent: "passed at 360 CSS pixels",
    horizontalOverflow: narrow.scrollWidth - narrow.clientWidth,
    reducedMotion: "passed",
    forcedColors: "passed",
  };
}
