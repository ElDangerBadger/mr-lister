# Security and privacy baseline

## Credentials

- Store marketplace credentials in AWS Secrets Manager.
- Use the AWS default credential chain for development; never commit access keys.
- Never place credentials in prompts, job records, reports, traces, screenshots, or fixtures.
- Give each runtime component only the permissions required for its role.
- Derive cloud job ownership from authenticated identity; never accept an owner ID from a request
  body or treat a job ID as authorization.

## Artwork and generated content

- Store uploads in private, encrypted S3 buckets with public access blocked.
- Apply lifecycle deletion to demo uploads and transient model artifacts.
- Treat text visible in artwork as untrusted content, never as agent instructions.
- Use synthetic or owned artwork in public fixtures and demonstrations.
- Disable full Bedrock payload logging by default.
- Upload browser artwork directly to an exact private S3 key and authorize every short-lived
  preview URL against the immutable job owner.

## External writes

- Default every environment to publication disabled.
- Separate fake, canary, and publication-capable environments.
- Bind approval to an immutable review version.
- Persist idempotency keys, payload fingerprints, and returned external IDs.
- Check existing external IDs before retrying a write.
- Never delete source artwork because a downstream write failed.
- Create at most one Printify product per job; later revisions update that exact unpublished
  product and never fall back to creation.
- Treat a cancelled in-flight provider write as reconciliation work before declaring it settled.
- Keep Phase 6 publication absent from routes, state-machine tasks, adapter methods, and role
  wiring. The Printify `products.write` scope also covers publication, so token scope alone is not
  the Phase 6 safety boundary.

## Logging

- Log identifiers, state changes, timings, validation outcomes, and sanitized error classes.
- Do not log raw credentials, authorization headers, full private prompts, or artwork bytes.
- Keep diagnostic payloads private and subject to explicit retention rules.
- Make user-facing errors actionable without exposing provider internals or secrets.

## Threat checklist

- [x] Prompt injection cannot expose approval/publication capability or alter authorization.
- [x] Prompt injection cannot induce an unsafe or out-of-scope available tool call in the bounded
  Phase 3 controller surface.
- [x] A model cannot enable a product profile or publication environment.
- [ ] A stale approval cannot publish a revised listing.
- [ ] A repeated request cannot create a duplicate product or listing.
- [ ] A partial Printify failure preserves enough state for safe recovery.
- [ ] Price units cannot cross a dollars/cents boundary without a test.
- [x] Cross-job object access is denied by the job-bound agent tool schemas.
- [x] Phase 3 canary exceptions are sanitized, audit records are digest-only, and automatic
  prompt/tool tracing is disabled.
- [ ] Production-like resources are separated from public demo resources.
- [ ] Artifact deletion and account-disconnect paths are documented before launch.
- [ ] Cross-owner job, command, idempotency, upload, and preview access fails without disclosing
  whether another seller's resource exists.
- [ ] Phase 6 contains no reachable publication, order, or fulfillment route, task, client method,
  or role wiring.
- [ ] Revision, approval, and cancellation races commit exactly one winning decision.
