# Phase 6.5 accessible seller interface

Phase 6.5 renders the application-owned Phase 6 projections and commands. It does not introduce a
new lifecycle, infer business authority in the browser, publish to Etsy, or call Printify directly.

## Seller journey

1. Cognito restores or establishes the invite-only seller session with authorization code and
   S256 PKCE.
2. The seller selects one supported PNG. The browser performs bounded convenience checks, computes
   SHA-256, creates an upload intent, and sends the exact returned form directly to private S3.
3. Upload completion pins one immutable S3 version and creates the durable Job plus `PREPARE` work.
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
- keyboard, focus, screen-reader semantics, forced colors, reduced motion, contrast, and 200-percent
  zoom checks;
- recursive checks that no publication/order/fulfillment control or request exists;
- private static hosting, exact SPA routing, same-origin cache-disabled API behavior, and security-
  header assertions; and
- a deployed non-destructive authentication, CORS, header, preview, and route-recovery smoke after
  the Phase 6.4 handlers are composed.

## Offline implementation evidence — August 22, 2026

The Phase 6.5 source/core gate is green:

- the full Python suite passes 846 tests with 11 explicitly gated live-Bedrock skips, and the
  Phase 6 subset passes 636 tests;
- the web gate passes ESLint, strict TypeScript, 62 Vitest/Testing Library tests, production build,
  and release-artifact hygiene checks;
- the versioned Python-to-browser schema/fixture drift check, Ruff lint and formatting,
  `compileall`, SAM lint/build, and `git diff --check` pass; and
- `npm audit --audit-level=high` reports zero vulnerabilities.

Chrome directly exercised managed-session recovery, keyboard focus, desktop and 360-pixel review,
the unpublished boundary, prominent Strands evidence, 13 tags, exact placement/synchronization
evidence, dirty-edit conflict/reapply, accepted-command readback lag, preview retry, Blob cleanup,
upload recovery, and invalid-origin fail-closed behavior. The accepted-approval/readback-lag
regression was reproduced and repaired; focus now moves to the action status rather than the
document body.

This is intentionally not the final browser or deployment gate. The newest bundle's added
validation-status rendering, pending-dialog double-submit lock, route-race isolation, and hidden-
tab behavior remain source/component-tested after the fixture CLI failed to reload the final
bundle. Firefox, WebKit, forced colors, full contrast evidence, 200-percent zoom, and the deployed
non-destructive smoke also remain open. No Phase 6 Lambda `SCAFFOLD_ONLY` marker was removed.

Phase 6.6 retains the destructive/provider live canaries, cross-owner/concurrency probes, browser-
restart acceptance, and moderated first-time-seller evidence.
