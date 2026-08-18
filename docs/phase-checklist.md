# Mr Lister phase checklist

This is the running execution checklist for the original AWS-native phase map. A phase is marked
complete only after its deliverables and exit criteria have been verified. Detailed scope remains
in [`roadmap.md`](roadmap.md); this file records execution status and evidence.

## Progress

- [x] Phase 0 — Foundation and proof ledger
  - Evidence: clean install/build, CI verification, versioned contracts, architecture decisions,
    synthetic fixtures, security baseline, and frozen demo scope.
  - Commit: `6ab3626` (`Complete Phase 0 foundation`)
- [x] Phase 1 — Local vertical skeleton
  - [x] Explicit job states and guarded transitions
  - [x] Upload, status, review, revise, approve, publish, and report APIs
  - [x] Fake Bedrock-intelligence and Printify-production adapters
  - [x] Deterministic artwork and listing validation
  - [x] Configuration-backed product-profile loader
  - [x] Intake idempotency and external-write ledger
  - [x] Fixture-driven end-to-end API and workflow tests
  - [x] Final clean-install, lint, format, test, and build verification
  - Evidence: 32 tests pass; lint and format checks pass; source distribution and wheel build.
- [ ] Phase 2 — Bedrock listing intelligence
  - [ ] Bedrock Converse adapter and multimodal artwork inspection
  - [ ] Schema-constrained artwork and listing output
  - [ ] Exactly 13 validated tags
  - [ ] Bounded repair loop for invalid model output
  - [ ] Private, redacted raw-response diagnostics
  - [ ] Representative evaluation set and explicit live canary
- [ ] Phase 3 — Strands agent and AgentCore Runtime
  - [ ] Bounded Strands tool surface
  - [ ] Recommendation-versus-authorization instructions
  - [ ] Structured result handling and recoverable tool failures
  - [ ] AgentCore-compatible local entry point and Runtime deployment
  - [ ] Session/job correlation and sanitized observability
- [ ] Phase 4 — AWS persistence and durable workflow
  - [ ] Private S3 artifact storage and lifecycle policy
  - [ ] DynamoDB operational records
  - [ ] Step Functions prepare/review/publish/verify lifecycle
  - [ ] Least-privilege IAM and Secrets Manager integration
  - [ ] Pause/resume and retry/idempotency acceptance
- [ ] Phase 5 — Real Printify draft creation
  - [ ] Printify authentication and artwork upload
  - [ ] Verified product profile, placement, variants, and integer-cent pricing
  - [ ] Idempotent unpublished product creation and external-ID persistence
- [ ] Phase 6 — Review and approval interface
  - [ ] Upload, progress, consolidated review, and supported edits
  - [ ] Validation and margin visibility
  - [ ] Explicit approval and clear result states
  - [ ] Usability acceptance without external documentation
- [ ] Phase 7 — Etsy publication through Printify
  - [ ] Approval-version and publish guard verification
  - [ ] Channel publication and status polling
  - [ ] Partial-failure recovery, result links, and immutable reports
- [ ] Phase 8 — Hardening, evaluation, and submission
  - [ ] Security, failure-mode, privacy, and cost review
  - [ ] Evaluation evidence and demo rehearsal
  - [ ] Architecture, setup, disclosure, and submission materials
