async page => {
  const check = (condition, message) => {
    if (!condition) throw new Error(`Phase 6.6 upload selection gate: ${message}`);
  };
  const { fixtureOrigin, publicOrigin } = page.__phase66;
  const originalUrl = page.url();
  const originalViewport = page.viewportSize() ?? await page.evaluate(() => ({
    width: window.innerWidth,
    height: window.innerHeight,
  }));
  const uploadWrites = [];
  const recordWrite = request => {
    if (request.url().startsWith(`${publicOrigin}/v1/`)
      && !["GET", "HEAD", "OPTIONS"].includes(request.method())) {
      uploadWrites.push(request.method());
    }
  };
  page.on("request", recordWrite);

  // Reuse the local fixture image without requiring Node globals in the CLI sandbox.
  const imageResponse = await page.request.get(`${fixtureOrigin}/phase66/upload-selection.png`);
  check(imageResponse.ok(), "the local upload fixture image is unavailable");
  const png = await imageResponse.body();
  const names = [
    "moon-moth.png",
    "garden-fern.png",
    "a-very-long-artwork-filename-that-must-wrap-without-covering-the-individual-file-controls.png",
    "raven.png",
    "wildflower.png",
    "overflow.png",
  ];
  const choose = async (trigger, filenames) => {
    check(await trigger.isVisible() && await trigger.isEnabled(), "the artwork selection control is unavailable");
    const input = page.locator('input[type="file"][name="artwork"]');
    check(await input.evaluate(element => element.multiple), "the artwork input does not allow multiple files");
    // CLI chooser dialogs require a separate upload command. Exercise the same
    // input change here; native chooser activation is checked separately in Safari.
    await input.setInputFiles(filenames.map(name => ({ name, mimeType: "image/png", buffer: png })));
  };
  const queueNames = async () => page.locator(".selection-list .queue-file strong").allTextContents();
  const expectOrder = async expected => {
    await page.waitForFunction(filenames => {
      const actual = [...document.querySelectorAll(".selection-list .queue-file strong")]
        .map(element => element.textContent);
      return JSON.stringify(actual) === JSON.stringify(filenames);
    }, expected);
    check(JSON.stringify(await queueNames()) === JSON.stringify(expected), "selection order changed unexpectedly");
  };
  const geometry = async () => page.evaluate(() => {
    const rectangle = selector => {
      const element = document.querySelector(selector);
      if (!(element instanceof HTMLElement)) return null;
      const bounds = element.getBoundingClientRect();
      return { x: bounds.x, right: bounds.right, top: bounds.top, bottom: bounds.bottom, width: bounds.width };
    };
    return {
      viewportWidth: window.innerWidth,
      documentWidth: document.documentElement.scrollWidth,
      grid: rectangle(".upload-grid"),
      artwork: rectangle(".upload-main"),
      guide: rectangle(".upload-guide"),
      selection: rectangle(".upload-selection-column"),
      action: rectangle(".upload-actionbar"),
    };
  });

  try {
    await page.getByRole("link", { name: "Mr. Lister seller review home" }).click();
    await page.getByRole("heading", { name: "Let’s start with your artwork." }).waitFor();
    const emptyAction = page.getByRole("button", { name: "Choose artwork to continue", exact: true });
    check(await emptyAction.isDisabled(), "an empty queue can be submitted");
    const emptyLayout = await geometry();
    check(emptyLayout.grid !== null && emptyLayout.action !== null, "the empty upload layout is absent");
    check(Math.abs(emptyLayout.action.x - emptyLayout.grid.x) < 2
      && Math.abs(emptyLayout.action.width - emptyLayout.grid.width) < 2, "the empty action panel does not span the grid");

    await choose(page.locator(".upload-choose"), names.slice(0, 2));
    await expectOrder(names.slice(0, 2));
    check(await page.getByRole("button", { name: "Prepare 2 listings", exact: true }).isEnabled(), "two-file selection does not enable batch preparation");
    const addMore = page.getByRole("button", { name: "Add more artwork", exact: true });
    await choose(addMore, [names[2]]);
    await expectOrder(names.slice(0, 3));
    await page.getByRole("button", { name: `Move ${names[1]} earlier`, exact: true }).click();
    await expectOrder([names[1], names[0], names[2]]);
    await page.getByRole("button", { name: `Remove ${names[0]}`, exact: true }).click();
    await expectOrder([names[1], names[2]]);
    await choose(addMore, [names[0], names[3]]);
    const fourFiles = [names[1], names[2], names[0], names[3]];
    await expectOrder(fourFiles);

    await choose(addMore, names.slice(4, 6));
    await page.getByRole("alert").filter({ hasText: /(?:no more than|up to|limit|at most|maximum|room for).*5|5.*(?:file|batch)/iu }).waitFor();
    await expectOrder(fourFiles);
    check(await page.getByRole("button", { name: "Prepare 4 listings", exact: true }).isEnabled(), "an over-limit addition damaged the existing queue");
    await choose(addMore, [names[4]]);
    const fiveFiles = [...fourFiles, names[4]];
    await expectOrder(fiveFiles);
    check(await page.getByRole("button", { name: "Prepare 5 listings", exact: true }).isEnabled(), "the five-file limit cannot be reached");
    check(await addMore.isDisabled(), "Add more artwork stays enabled at the five-file limit");
    check(await page.getByRole("alert").count() === 0, "a valid addition did not clear the earlier selection error");

    const desktop = await geometry();
    check(desktop.artwork !== null && desktop.guide !== null
      && desktop.selection !== null && desktop.action !== null, "one of the four upload panels is absent");
    check(await page.locator(".upload-actionbar--selected").count() === 1, "the selected queue did not switch the action panel layout");
    if (desktop.viewportWidth >= 900) {
      check(Math.abs(desktop.artwork.top - desktop.guide.top) < 2, "the upload and guide panels are not aligned");
      check(Math.abs(desktop.selection.top - desktop.action.top) < 2, "the prepare panel dropped below the file queue");
      check(Math.abs(desktop.selection.x - desktop.artwork.x) < 2
        && Math.abs(desktop.selection.width - desktop.artwork.width) < 2, "the file queue is not aligned with the upload column");
      check(Math.abs(desktop.action.x - desktop.guide.x) < 2
        && Math.abs(desktop.action.width - desktop.guide.width) < 2, "the prepare panel is not aligned with the guide column");
      const uploadShare = desktop.artwork.width / (desktop.artwork.width + desktop.guide.width);
      check(Math.abs(uploadShare - 0.65) <= 0.03, "the approved upload/guide proportions changed");
    }

    await page.setViewportSize({ width: 360, height: 780 });
    const mobile = await geometry();
    check(mobile.selection !== null && mobile.action !== null, "mobile queue or preparation panel is absent");
    check(mobile.action.top >= mobile.selection.bottom - 2, "mobile preparation does not follow the file queue");
    check(Math.abs(mobile.selection.x - mobile.action.x) < 2
      && Math.abs(mobile.selection.width - mobile.action.width) < 2, "mobile queue and preparation panel do not share one column");
    check(mobile.documentWidth <= mobile.viewportWidth + 1, "the upload queue overflows the mobile viewport");
    for (const name of fiveFiles) {
      check(await page.getByRole("button", { name: `Remove ${name}`, exact: true }).isVisible(), "a mobile file removal control is hidden");
    }

    for (let index = 0; index < fiveFiles.length; index += 1) {
      await page.getByRole("button", { name: `Remove ${fiveFiles[index]}`, exact: true }).click();
      await expectOrder(fiveFiles.slice(index + 1));
    }
    check(await emptyAction.isDisabled(), "removing every file did not restore the disabled empty state");
    check(await page.locator(".selection-panel").count() === 0, "an empty file list remained on screen");
    check(await page.locator(".upload-actionbar--selected").count() === 0, "the empty action panel retained the selected layout");
    const fixtureState = await (await page.request.get(`${fixtureOrigin}/__fixture__/state`)).json();
    check(uploadWrites.length === 0, "editing the file queue issued an API mutation");
    check(fixtureState.provider_transport_attempts === 0, "editing the file queue invoked provider transport");

    return {
      multiFileInput: "passed",
      appendRemoveAndReorder: "passed",
      overLimitPreservesQueue: "passed",
      fiveFileLimit: "passed",
      fourPanelDesktopLayout: desktop.viewportWidth >= 900 ? "passed" : "desktop viewport not used",
      mobileStackAndLongFilename: "passed",
      removeAllRestoresEmptyState: "passed",
      uploadApiMutations: uploadWrites.length,
      providerTransportAttempts: fixtureState.provider_transport_attempts,
    };
  }
  finally {
    page.off("request", recordWrite);
    await page.setViewportSize(originalViewport);
    await page.goBack();
    await page.waitForURL(originalUrl);
  }
}
