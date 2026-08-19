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
- [ ] Phase 2 — Bedrock listing intelligence (prompt v5 live rebaseline pending)
  - [x] Bedrock Converse adapter and multimodal artwork inspection
  - [x] Schema-constrained artwork and listing output
  - [x] Exactly 13 application-validated tags
  - [x] Configuration-bounded repair (Nova maximum two) for invalid or low-diversity output
  - [x] Private diagnostics with redaction and explicit raw-output opt-in
  - [x] Eight-case calibration/regression/holdout evaluation set and cost-gated live harness
  - [x] Concrete visual-element inventory before listing generation
  - [x] Etsy-aware tag diversification prompt, deterministic ready-to-post gate, and zero-reuse
    evaluation metric
  - [x] One-to-three-trial run capture and comparable provider/run summaries
  - [x] Provider-capability split: prompted JSON for Nova, native structured output for Claude
  - [x] Least-privilege Nova and Claude invoke-policy templates
  - [x] One live Nova 2 Lite canary reaches human review with fake production
  - [x] Prompt v1 five-case Nova evaluation passed its Phase 2 quality floor
  - [x] Prompt v2 calibration exposed tag reuse after schema repair without touching holdouts
  - [x] Prompt v3 calibration eliminated tag reuse and exposed a generic repaired title
  - [x] Prompt v4 accepted-output capture identified a badger/raccoon grounding error
  - [x] Prompt v5 calibration restored badger grounding and showed one tag cleanup was insufficient
  - [x] Prompt v5 two-repair canary passes with correct badger grounding and zero tag reuse
  - [x] Prompt v5 regression split passes 3/3, including prompt injection and transparency
  - [x] Prompt v5 untouched holdout first-look run preserved without prompt or rubric tuning
  - [x] Claude Sonnet 4.6 Bedrock activation and one live comparison canary
  - [x] Repeated meaningful tag keywords route the job to `needs_revision` before production
  - [ ] Prompt v5 full eight-case Nova evaluation passes the revised quality floor
  - The Claude quality benchmark is comparative evidence, not a Phase 2 exit criterion.
  - Historical v1 evidence: all five explicitly gated Nova cases reached human review
    with zero repairs and no publish calls. Across the accepted set: 8,033 total tokens, 19.683
    seconds aggregate model latency, 100% title specificity, visible-text recall, and tag diversity,
    73.33% average visual-anchor recall, and 53.33% average tag-concept recall. Evaluation assets
    and manifest validate; `mr-lister-dev` identity and `us-west-2` Region were verified. Prompt v5
    changes the acceptance target, so that earlier run remains evidence but no longer closes the
    phase by itself. The first v2 calibration canary reached human review but recorded four reused
    tag keywords; that finding led to a repair-aware, still-nonblocking tag-diversity check. The v3
    canary reduced reuse to zero but omitted the known badger subject from its repaired title.
    Accepted-output capture in v4 confirmed that omission came from a badger/raccoon visual
    misclassification and also exposed one remaining generic `art` repetition.
  - Current v5 evidence: all eight cases produced valid contracts, reached human review, and made
    zero publish calls. Four of eight passed every strict quality threshold; six of eight achieved
    zero tag-keyword reuse. All three regression cases passed. The three untouched holdouts failed
    the strict floor: owl and gardening each retained one repeated keyword after two repairs, while
    jellyfish named only one of three frozen visual anchors. The second calibration typography case
    read and used its exact text but described the frozen tool/lettering anchors generically. Total
    accepted-set telemetry was 25,902 input tokens, 7,930 output tokens, 33,832 total tokens, 12
    semantic repairs, and 45.191 seconds aggregate provider-reported model latency.
  - Claude comparison canary: Sonnet 4.6 correctly grounded every frozen visual anchor and generated
    a complete contract in 8,320 tokens (6,275 input and 2,045 output) with one semantic repair. It
    nevertheless repeated four meaningful tag keywords while claiming the set contained no reuse.
    That finding promoted tag-keyword repetition from a warning to a deterministic workflow error:
    unresolved drafts remain available for human correction, but cannot create a production draft
    or receive approval. Current offline verification: 89 tests pass; lint and format checks pass.
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
