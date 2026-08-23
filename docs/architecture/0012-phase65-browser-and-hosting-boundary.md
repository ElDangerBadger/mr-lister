# ADR 0012: Keep the seller browser thin, same-origin, and server-directed

- Status: Accepted
- Date: 2026-08-22

## Context

Phase 6.4 established an owner-scoped Cognito, API Gateway, DynamoDB, and private-S3 boundary, but
the repository has no browser application or static-hosting stack. Phase 6.5 must present upload,
durable progress, consolidated review, and the five closed seller actions without creating a
second lifecycle in TypeScript or weakening the pre-publication boundary.

Several browser seams need an explicit answer before implementation:

- review projections advertise an authenticated artwork-preview URL on the application origin,
  while the protected API currently has a different origin;
- a browser reload after upload-intent creation but before completion cannot rediscover that
  intent from the recent-jobs projection;
- raw Job states are not a stable seller presentation contract;
- an OAuth public client needs one-use PKCE and CSRF state without creating durable token storage;
  and
- the browser needs runtime-verifiable public DTOs rather than importing private Python models or
  trusting unvalidated JSON.

## Decision

Phase 6.5 uses one isolated React and strict-TypeScript application under `web/`. It is a thin
client of application-owned projections and commands:

- CloudFront serves private static assets through origin access control and routes cache-disabled
  `/v1/*` requests to the Phase 6 API, giving the browser one configured application origin;
- the `/v1/*` behavior does not auto-compress or forward normalized `Accept-Encoding`; this keeps
  the server's strong review ETag strong and usable as the exact later `If-Match` authority;
- the SPA calls relative `/v1` paths and never attaches its bearer token to S3, Printify mockups,
  Cognito, or an arbitrary URL;
- artwork preview uses an authenticated fetch to the application-origin route, follows the exact-
  version S3 redirect, renders a short-lived Blob URL, and revokes that URL on replacement,
  navigation, logout, and unmount;
- `GET /v1/uploads/{upload_id}` returns a minimal owner-scoped recovery projection so a stable
  `/uploads/{upload_id}` route can resume completion, cancellation, or exact-file reauthorization;
- `GET /v1/jobs/{job_id}` returns a server-derived progress projection. The client never maps a
  `ControlJobState` to display state, stage, or capability;
- versioned public JSON schemas and golden fixtures define the browser contract. Every API response
  is runtime validated before it reaches UI state;
- Cognito uses authorization code plus S256 PKCE. Session storage contains only one one-use
  transaction `{state, verifier, return_path}` and is cleared after callback. Access and refresh
  tokens remain in memory;
- reload recovery may return through Cognito's managed session. No token, seller record, listing,
  review, artwork, or upload credential enters local storage, IndexedDB, a service worker, logs, or
  analytics;
- local unit and browser development use contract fixtures. Real Cognito/API testing uses a
  separately deployed development CloudFront origin rather than broadening production callback or
  CORS rules to an arbitrary localhost port;
- server-projected capability records, disabled reasons, ETags, record versions, review
  fingerprints, and idempotency receipts remain the only command authority;
- the browser exposes no publication, order, fulfillment, product-deletion, provider-control, or
  raw-storage operation.

The browser routes are deliberately small:

- `/` for authentication recovery, a new upload, and recent jobs;
- `/auth/callback` for one-use OAuth completion;
- `/uploads/{upload_id}` for active upload recovery; and
- `/jobs/{job_id}` for progress, review, recovery, and terminal result.

## Accessibility and recovery rules

- The target is WCAG 2.2 AA, including keyboard-only operation, visible focus, text alternatives,
  reduced motion, contrast, and usable layout at 200 percent zoom.
- The progress region announces only named stage changes; it never invents a percentage for model,
  provider, or reconciliation work.
- Listing tags are thirteen individually labelled native inputs. Server error paths link a focused
  summary to exact fields with `aria-invalid` and `aria-describedby`.
- Polling pauses when hidden or offline, honors `Retry-After`, and never overwrites a dirty form.
- A mutation keeps one idempotency key, body, ETag, and expected-version tuple across transport and
  authentication retries. An ambiguous result is reconciled by reading durable state before a new
  logical command is created.
- `409` and `412` preserve local edits, load the latest server projection, and require an explicit
  seller decision to copy or reapply them.
- Every review retains `Unpublished — not on Etsy` and a prominent, sanitized `Prepared with
  Strands Agents` provenance section.

## Consequences

- Phase 6.5 can be developed offline against versioned fixtures while Phase 6.4's deployed Lambda
  composition remains fail closed.
- Static application files live in a separate bucket from private seller artifacts.
- HTML and runtime configuration are non-cacheable; content-addressed assets are immutable.
- CloudFront owns CSP, HSTS, no-referrer, nosniff, framing, and permissions-policy response headers.
- The same-origin API behavior resolves the existing preview-link contract without leaking the API
  origin into application state.
- Phase 6.5 cannot be marked complete merely because a mocked SPA renders. Its browser,
  accessibility, hosting, and deployed non-destructive smoke gates must pass, and Phase 6.4 must
  still close its runtime-composition gate before a live end-to-end claim.
