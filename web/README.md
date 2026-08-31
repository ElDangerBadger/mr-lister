# Mr. Lister seller web

Phase 6.5 provides a production-shaped React browser client for the owner-scoped seller API. The browser can submit an ordered, memory-only queue of up to five PNG, compatible self-contained SVG, or JPG/JPEG artwork files, observe each independent preparation, revise a generated listing, and record bounded human decisions. SVG and JPEG input are normalized locally to proportional PNG without crop, padding, distortion, or square enforcement; only canonical PNG is fingerprinted or uploaded. SVG linked resources, text, filters, masks, patterns, and animation are rejected. Transparent and opaque backgrounds are valid; artwork must contain at least one visible pixel. It has no Etsy publish, order, or fulfillment capability.

Each selected file creates one existing upload intent and one listing job. Queue order is submission order in the current browser session, not a durable batch or a promise that preparation will finish in the same order. API behavior in unit tests is injected through mock ports; passing local tests is not evidence of a deployed service.

## Local checks

Use the repository-supported Node version (22.12 or newer), then run:

```sh
npm ci
npm run check
npm run dev
```

The app loads `/runtime-config.json` at startup. Production receives that public, no-secret object separately from the SAM stack's `SellerRuntimeConfig` output; it is deliberately absent from the Vite build. For deliberate local mock work only, copy `runtime-config.example.json` to `public/runtime-config.json`, replace every placeholder with the local deployment values, and remove the copy afterward. `public/runtime-config.json` is ignored by Git and the build verification fails if any runtime config is emitted.

OAuth access and refresh tokens exist only in memory. Session storage is limited to one short-lived PKCE transaction containing state, verifier, and an allowlisted return path.
