# ADR 0011: Require owner-scoped identity for the cloud review boundary

- Status: Accepted
- Date: 2026-08-21

## Context

The Phase 1 HTTP surface is a local, in-memory development API. It has no user identity or job
ownership, reads upload bytes through the application process, and exposes publication. Phase 6
introduces a browser interface over private artwork, a live Printify connection, product mutation,
and seller approval authority. A job ID or object key cannot act as authorization.

The hackathon slice needs one seller, but retrofitting ownership after jobs, artifacts, commands,
and recent-job recovery are public would be unsafe and expensive.

## Decision

Phase 6 uses a minimal authenticated cloud boundary:

- one invite-only Cognito User Pool seller initially;
- authorization code with PKCE and API Gateway JWT authorization;
- immutable Cognito subject ownership on every new job and artifact;
- owner checks on every read, command, idempotency key, upload, and preview request;
- short-lived, exact-key direct upload to private S3;
- server-computed allowed actions and disabled reasons;
- no browser-visible AWS credential, Printify secret, task token, arbitrary storage key, or
  provider body; the short-lived upload form necessarily contains its one server-generated opaque
  key;
- no public signup, teams, roles, billing, multi-store onboarding, or user-managed Printify
  connection in this phase;
- no deployed publication route or Phase 6 publication IAM capability.

Phase 6 deploys new `mr-lister-phase6-*` resources and durable contract version `2.0.0`. Existing
ownerless Phase 4/5 `1.0.0` records remain immutable evidence and fail closed at the new API
boundary; they are not silently upcast.

## Consequences

- Cross-owner and unknown job access return the same not-found response.
- Idempotency keys are scoped to the authenticated owner.
- Artwork bytes do not traverse API Gateway or Step Functions.
- The query/upload APIs do not need Printify secret access; provider workers do not receive
  approval or publication capability.
- The first deployed interface is single-seller but does not create an anonymous security debt.
- A future account/connection onboarding phase can extend ownership without changing its meaning.
