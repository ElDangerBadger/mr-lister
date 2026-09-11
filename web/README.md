# Mr Lister seller web

This is the current React application served at [massskutiny.com](https://massskutiny.com).
`src/main.tsx` starts OAuth, the owner-scoped seller API client, and the active publication
client. The workflow is Upload → Review → Publish, with explicit seller approval and a separate
publication confirmation. It has no order, fulfillment, delete, or unpublish capability.

The browser accepts an ordered, memory-only queue of up to five PNG, compatible self-contained
SVG, or JPEG files. Each file creates an independent job; submission order does not imply
preparation completion order. SVG and JPEG convert locally to proportional PNG. PNG bytes,
native aspect ratios, and valid transparent or opaque backgrounds are preserved. The picker
and validation explain source-file restrictions.

## Development and verification

From this directory, using Node 22.12 or newer:

```sh
npm ci
npm run check
```

`npm run dev` starts the frontend server. The app requires valid, origin-matched public
configuration at `/runtime-config.json`, an approved OAuth callback, and same-origin `/v1`
API routing. A local Vite server alone does not provide those services. The Phase 1 fake API
in `tools/legacy/` does not implement the current seller contract.

Production receives its public, secret-free runtime configuration separately from the SAM
stack's `SellerRuntimeConfig` output. Keep it out of the Vite build. For an explicitly configured
local integration, use `runtime-config.example.json` as the schema example; a temporary
`public/runtime-config.json` must match that local environment and must be removed before
building. The build fails if runtime configuration or source maps enter `dist`.

Unit tests inject mock ports; `tests/` and `offline/phase7/` contain test support and the
historical disabled-publication contract. They are excluded from the production dependency
graph by a build-time guard. The active publication implementation is `src/publication/`.
The [browser gate](../tools/phase66_browser/README.md) exercises the exact compiled application
with deterministic local API responses. Passing offline checks does not verify a live deployment.

OAuth access and refresh tokens stay in memory. Session storage contains only one short-lived
PKCE transaction with state, verifier, and an allowlisted return path.
