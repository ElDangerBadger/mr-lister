# Phase 6.6 browser gate

This harness runs the exact `web/dist` production bundle through Playwright CLI without adding
`@playwright/test` to the application. A local Python standard-library server owns deterministic
API state, authority headers, delayed responses, and static files. Six compact CLI flow files
exercise browser behavior; a generic context route only maps production-like HTTPS origins to the
loopback server.

Landing-page coverage starts without a seller session, checks Light/Dark/Auto including
system-theme changes and the matching decoded logo, and captures desktop/mobile layouts.
The real popup callback opens the existing authenticated upload home; signing out returns
to the official landing page with the same display preference. Hosted sign-in is fulfilled
entirely by the local fixture, and anonymous visitors must not read private APIs.

Upload-selection coverage sets local PNG fixtures on the multiple-file input and checks
append, remove, ordering, limits, and desktop/mobile geometry without submitting uploads.
Native file-chooser activation is checked separately in Safari; CLI chooser dialogs require
their own interactive upload command and cannot remain open inside an attested flow.

Workspace-navigation coverage adds three synthetic uploads through locally fulfilled routes.
It holds upload verification before releasing the first handoff, then proves the original
artwork is visible while AI/product preparation is still pending. It verifies one automatic
handoff per batch and checks sibling links, manual-navigation cancellation, unsaved-edit confirmation,
draft isolation, and narrow-screen navigation. The fake upload origin accepts bytes only inside
the intercepted browser fixture; it does not call S3, AWS, or a connected store.
It also verifies the animated mockup milestone during an unconfirmed provider request, reduced
motion, and disabled save/approval controls. Returning from a fully prepared batch starts a fresh
upload selection while retaining listing links; background completion alone keeps the queue visible.

Run all three engines from the repository root:

```shell
.venv/bin/python -m tools.phase66_browser.run_gate
```

For quick harness iteration against an already-built bundle:

```shell
.venv/bin/python -m tools.phase66_browser.run_gate --skip-build --engine chromium
```

Only the default fresh-build, all-three-engine command emits an `offline.browser_matrix`
`browser-gate.json`. Engine subsets, explicit engine selections, and `--skip-build` are iteration
runs; they emit a non-attestable `iteration.browser_harness` record instead.

The command requires `npx` and the Codex Playwright wrapper at
`~/.codex/skills/playwright/scripts/playwright_cli.sh`. It deliberately does not install browsers,
contact AWS, call Printify, or expose publication, order, or fulfillment actions. A missing browser
binary is reported as an engine-specific environment blocker.

Each run gets a UTC-stamped directory under `output/playwright/phase66/`. That ignored directory
contains the per-engine CLI snapshot, screenshot, privacy-scanned redacted trace ZIP, raw
non-attested diagnostic trace, diagnostic log, and a compact `browser-gate.json` summary. The
attested summary binds every engine to one deterministic SHA-256 of the exact `web/dist` files and
contains only closed engine/scenario statuses and counters. A second browser process proves route
recovery after process loss; a dedicated sentinel blocks and records any direct Printify API
attempt. Detailed local URLs, commands, raw traces, and paths remain non-attested diagnostics. No
artifact is written outside `output/playwright/phase66/`.

The redacted trace policy removes fixture credential values and authorization/bearer,
access/refresh/ID-token, PKCE-verifier, and cookie field material from every archived trace entry
and resource, then scans the completed ZIP for the same closed patterns. Raw Playwright traces are
never acceptance artifacts; they exist only for local harness diagnosis and contain synthetic
fixture data, never live seller or provider authority.
