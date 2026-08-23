# Phase 6.6 browser gate

This harness runs the exact `web/dist` production bundle through Playwright CLI without adding
`@playwright/test` to the application. A local Python standard-library server owns deterministic
API state, authority headers, delayed responses, and static files. Three compact CLI flow files
exercise browser behavior; a generic context route only maps production-like HTTPS origins to the
loopback server.

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
