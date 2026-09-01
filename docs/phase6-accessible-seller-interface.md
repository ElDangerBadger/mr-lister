# Phase 6.5 accessible seller interface

Phase 6.5 renders the application-owned Phase 6 projections and commands. It does not introduce a
new lifecycle, infer business authority in the browser, publish to Etsy, or call Printify directly.

Present-state note (2026-09-01): the exact seller bundle is deployed and completed an authenticated
current-release upload-to-review walkthrough, including same-job Strands provenance, retryable
economics recovery, explicit keep-unpublished approval, and the final unpublished state. The
separate full manual screen-reader/contrast and moderated-user artifact gates are not claimed.

## Seller journey

1. Cognito restores or establishes the invite-only seller session with authorization code and
   S256 PKCE.
2. The seller selects or drops one through five supported PNG, safe self-contained SVG, or
   JPG/JPEG files. The browser validates and normalizes each through one proportional canonical-PNG
   path, computes SHA-256, creates one upload intent per accepted file, and sends each exact
   returned form directly to private S3.
3. Each upload completion pins one immutable S3 version and creates its own durable Job plus
   `PREPARE` work. Invalid files retain per-file feedback without blocking valid siblings.
4. The job route shows named server-projected stages while AgentCore and Strands coordinate the
   preparation path and the configured intelligence workers analyze the artwork and draft the
   listing.
5. The same route renders original artwork, interpretation, listing, validation, fixed product
   policy, representative Printify mockups, complete economics, failure/recovery, and sanitized
   Strands provenance.
6. The seller may use only the five projected capabilities: edit listing, refresh economics,
   approve, cancel, or retry. Every mutation is version-, ETag-, owner-, and idempotency-bound.
7. Approval ends at `APPROVED`. The interface continues to say `Unpublished — not on Etsy`; it has
   no publication control or client capability.

## Browser state

Server state is authoritative. The browser may add only presentation state such as authentication,
loading, upload byte progress, a dirty listing draft, a pending command, offline backoff, or a
conflict requiring seller attention. Client state cannot enable an action.

Machine stages poll a lightweight server projection with bounded backoff and page-visibility
awareness. Human and terminal stages stop polling and refresh on focus. Responses with an older
record version than the currently rendered projection are ignored.

## Security and privacy

- The SPA uses relative same-origin `/v1` requests through CloudFront.
- The authenticated `/v1/*` behavior disables cache-key `Accept-Encoding` normalization and
  automatic compression, preserving the projection's strong authority ETag byte-for-byte for the
  later `If-Match` command boundary. Static fingerprinted assets remain compressed.
- Access and refresh tokens are memory-only. The one-use PKCE transaction is the only session-
  storage entry and is deleted after callback.
- Direct S3 upload receives the exact server-created form unchanged. The application never logs or
  persists its URL, policy, signature, key, checksum, token, or authorization headers.
- Artwork is fetched through the authenticated preview route and rendered from a revoked Blob URL.
- Model and seller text use ordinary text nodes; inline HTML, `dangerouslySetInnerHTML`, inline
  scripts/styles, `unsafe-inline`, and `unsafe-eval` are forbidden.
- There is no service worker, client-side durable cache, third-party analytics, or browser commerce
  integration.

## Required browser evidence

- strict TypeScript typecheck and production build;
- runtime-decoder and Python-schema drift tests;
- component tests using role- and label-based queries;
- automated axe checks in every material seller state;
- Playwright flows in Chromium, Firefox, and WebKit for authentication recovery, upload, refresh,
  review, edit, conflict, approval, cancellation, retry, offline recovery, and logout;
- picker, single-file drag/drop, and multiple-file drag/drop across required PNG/SVG, transparent/
  opaque, and square/portrait/landscape input cases, including per-file failure isolation;
- keyboard, focus, screen-reader semantics, forced colors, reduced motion, contrast, and 200-percent
  zoom checks;
- recursive checks that no publication/order/fulfillment control or request exists;
- private static hosting, exact SPA routing, same-origin cache-disabled API behavior, and security-
  header assertions; and
- a deployed non-destructive authentication, CORS, header, preview, and route-recovery smoke after
  the Phase 6.4 handlers are composed.

## Offline implementation evidence — August 22, 2026

The Phase 6.5 source/core gate was green at this checkpoint:

- the full Python suite passed 846 tests with 11 explicitly gated live-Bedrock skips, and the
  Phase 6 subset passed 636 tests;
- the web gate passed ESLint, strict TypeScript, 62 Vitest/Testing Library tests, production build,
  and release-artifact hygiene checks;
- the versioned Python-to-browser schema/fixture drift check, Ruff lint and formatting,
  `compileall`, SAM lint/build, and `git diff --check` passed; and
- `npm audit --audit-level=high` reported zero vulnerabilities.

Chrome directly exercised managed-session recovery, keyboard focus, desktop and 360-pixel review,
the unpublished boundary, prominent Strands evidence, 13 tags, exact placement/synchronization
evidence, dirty-edit conflict/reapply, accepted-command readback lag, preview retry, Blob cleanup,
upload recovery, and invalid-origin fail-closed behavior. The accepted-approval/readback-lag
regression was reproduced and repaired; focus now moves to the action status rather than the
document body.

Phase 6.6 subsequently replaced the unstable fixture with a deterministic local server and ran one
digest-bound production bundle through Chromium, Firefox, and WebKit. All three engines passed the
same authentication/review/approval, stale-readback focus, tab recovery, route-race, hidden/offline
polling, forced-colors, reduced-motion, and 360-CSS-pixel reflow flows and produced trace ZIPs. The
sanitized summary records zero provider transport attempts and no commerce surface. Full manual
screen-reader/contrast coverage and the remaining command journeys remain formal hardening targets.
The backend and seller edge are active in draft-only mode. The current-release authenticated
walkthrough reached consolidated review, displayed five mockups and complete 30-variant economics,
recovered one retryable economics failure, and ended at `APPROVED` while still visibly
`Unpublished — not on Etsy`.

Phase 6.6 retains the exact provider-ledger canaries, deployed cross-owner/concurrency probes, full
manual accessibility journeys, and moderated first-time-seller evidence as unclaimed formal
hardening artifacts. The functional hackathon-demo seal does not represent those artifacts as
passed; see [`phase6-release-state.md`](phase6-release-state.md).
