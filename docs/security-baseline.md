# Security and privacy baseline

## Credentials

- Store marketplace credentials in AWS Secrets Manager.
- Use the AWS default credential chain for development; never commit access keys.
- Never place credentials in prompts, job records, reports, traces, screenshots, or fixtures.
- Give each runtime component only the permissions required for its role.

## Artwork and generated content

- Store uploads in private, encrypted S3 buckets with public access blocked.
- Apply lifecycle deletion to demo uploads and transient model artifacts.
- Treat text visible in artwork as untrusted content, never as agent instructions.
- Use synthetic or owned artwork in public fixtures and demonstrations.
- Disable full Bedrock payload logging by default.

## External writes

- Default every environment to publication disabled.
- Separate fake, canary, and publication-capable environments.
- Bind approval to an immutable review version.
- Persist idempotency keys, payload fingerprints, and returned external IDs.
- Check existing external IDs before retrying a write.
- Never delete source artwork because a downstream write failed.

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
