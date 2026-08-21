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
- [x] Phase 2 — Bedrock listing intelligence
  - [x] Bedrock Converse adapter and multimodal artwork inspection
  - [x] Schema-constrained artwork and listing output
  - [x] Exactly 13 application-validated tags
  - [x] Configuration-bounded repair (Nova maximum two) for invalid or low-diversity output
  - [x] Private diagnostics with redaction and explicit raw-output opt-in
  - [x] Eleven-case calibration/regression/holdout evaluation set and cost-gated live harness
  - [x] Concrete visual-element inventory before listing generation
  - [x] Etsy-aware tag diversification prompt, deterministic ready-to-post gate, and zero-reuse
    evaluation metric
  - [x] Provider candidate pool and deterministic final 13-tag selector
  - [x] One-to-three-trial run capture and comparable provider/run summaries
  - [x] Provider-capability split: prompted JSON for Nova/Luna, native structured output for Claude
  - [x] Least-privilege Nova, Luna, and Claude invoke-policy templates
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
  - [x] OpenAI GPT-5.6 Luna comparison deferred because the account tier blocks model access;
    retained as optional future evidence, not a Phase 2 exit criterion
  - [x] Google Gemma 3 27B canary and six-case known regression comparison
  - [x] Repeated meaningful tag keywords route the job to `needs_revision` before production
  - [x] Original v5 holdouts preserved as regressions after immutable first-look evidence
  - [x] Fresh v6 holdouts frozen before prompt v6 inference
  - [x] Prompt v7 original-failure regressions pass the revised quality floor
  - [x] Prompt v7 fresh holdouts preserved in an immutable first-look run
  - [x] Prompt v7 holdout disposition recorded: 2/3 passed the strict semantic floor and 3/3
    produced safe, complete listings; the seahorse secondary-object miss is an accepted limitation
  - [x] Full eleven-case Nova rerun superseded by the selected Gemma pathway; retained as optional
    comparative work, not a Phase 2 exit criterion
  - Claude, Luna, and additional Nova benchmarks are comparative evidence, not Phase 2 exit
    criteria.
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
    or receive approval.
  - Current Gemma v7 evidence: the canary and six known regressions have passing evidence under the
    deterministic selector. The immutable v6 holdout first look produced safe, complete listings
    for 3/3 and passed the strict quality floor for 2/3; transparent seahorse missed the intended
    kelp and bubble semantics. Every accepted result stopped at human approval with zero publish
    calls. Current offline verification: 102 tests pass; lint and format checks pass.
  - Accepted limitation: Gemma can describe small abstract secondary elements literally without
    inferring their intended semantic object. In the transparent seahorse holdout it identified the
    subject but called the intended kelp and bubbles `plant-like shapes` and `circular shapes`.
    Human review remains the authority for correcting these secondary details; the limitation does
    not weaken contract validation, approval gating, or the prohibition on autonomous publication.
  - Exit evidence: every accepted response conforms to application contracts; the complete offline
    suite requires no live inference; all live evaluation results stopped at human approval with
    fake production and zero publish calls. Detailed evidence is in
    [`phase2-gemma-v7-evaluation.md`](phase2-gemma-v7-evaluation.md).
- [x] Phase 3 — Strands agent and AgentCore Runtime
  - [x] Bounded, single-job Strands tool surface with capability-scoped review/revise modes
  - [x] Recommendation-versus-authorization instructions and no approval/publication tools
  - [x] Structured result handling and sanitized recoverable tool failures
  - [x] AgentCore-compatible local HTTP entry point (`/invocations` and `/ping`)
  - [x] Preparation/drafting tool path from validated intake to staged review
  - [x] AgentCore Runtime packaging and deployment
    - [x] Narrow explicit CodeZip bundle, schema validation, and ARM64 package
    - [x] Direct CodeZip deployment in `us-west-2` and deployed Nova invocation canary
  - [x] Session/job correlation and sanitized application observability
  - Exit evidence: 129 offline tests pass; lint and format checks pass. Nova passed both routine
    and visible-prompt-injection controller cases, selected inspection and validation tools, and
    stopped at human review. Gemma preserved the authority boundary but failed tool selection in
    both cases, so it remains the image/listing worker rather than the controller. The official SDK
    local smoke passed. AgentCore Runtime version 2 and its default endpoint reached `READY`. The
    live deployed review completed in 2.541 seconds, used only `inspect_staged_review` and
    `validate_staged_listing`, and returned `human_review`, `requires_human_approval=true`, and
    `publication_authorized=false`. Its sanitized audit recorded 3 cycles, 4,942 input tokens, 195
    output tokens, and a correlation digest without raw session/job IDs or prompt content.
    Automatic OTel prompt/tool tracing is disabled. AWS vended payload capture was used only with
    synthetic canary data, removed before the Phase 3 freeze, and produced no event in the final
    zero-model-cost privacy check.
  - Deferred refinement: reduce repeated tool/schema context below the current 5,137-token deployed
    canary baseline after the Phase 3 freeze; this is an efficiency improvement, not a safety or
    correctness blocker.
- [x] Phase 4 — AWS persistence and durable workflow
  - [x] Application-owned persistence boundary and optimistic record-version guard
  - [x] Architecture decision: Step Functions requests work; application code and atomic storage
    conditions authorize state changes
  - [x] Private S3 artifact boundary and lifecycle-policy infrastructure baseline
  - [x] DynamoDB operational records
    - [x] On-demand encrypted single-table infrastructure baseline
    - [x] Atomic intake claim, job, artwork metadata, and first event transaction
    - [x] Conditional job/review/event transition transaction with shared application validation
    - [x] Fresh-store reconstruction and transaction-shape tests
    - [x] Restartable analysis and listing checkpoints
    - [x] External-write claim/finalize and reconciliation records
    - [x] Version-bound approval-wait records with TTL and atomic approval consumption
  - [x] Step Functions prepare/review/publish/verify lifecycle
    - [x] Standard ASL definition with identifier-only command payloads
    - [x] Strict prepare, approval-wait, approval-callback, fake-publish, and fake-verify handlers
    - [x] Durable restart routes for approved, publishing, published, and verified checkpoints
    - [x] Bounded retry rules, sanitized terminal failure, and execution-data logging disabled
    - [x] SAM 1.165 lint/build with CPython 3.13 Linux ARM64 artifacts
    - [x] Deploy the SAM stack and pass one explicitly gated AWS canary through `VERIFIED`
  - [x] Least-privilege IAM and Secrets Manager integration
    - [x] Command-specific DynamoDB/S3/Lambda/callback policy baseline
    - [x] CloudWatch log groups with 14-day retention
    - [x] Secrets Manager interface and exact-ARN Phase 5 credential boundary
    - [x] Resource-scoped administrator bootstrap and developer change-set policy template
    - [x] Apply the one-time bootstrap stack in `us-west-2`
  - [x] Pause/resume and retry/idempotency acceptance
    - [x] Offline approval pause, callback replay, checkpoint resume, and exactly-once fake writes
    - [x] Live workflow-history privacy inspection and process-exit canary
  - Exit evidence: the bootstrap and application stacks are deployed in `us-west-2`; the standard
    SAM update path reports the stack is current. One execution remained durable across client
    process exit, Lambda code/IAM updates, and a replay-safe approval callback before reaching
    `VERIFIED`. A second untouched canary independently reached `VERIFIED` with record version 11,
    14 ordered events, and exactly two completed idempotent fake-write records. Its 27-event Step
    Functions history contained no PNG bytes, listing body, prompt text, or credential indicators.
    The expected opaque callback-token field existed only at the approval wait boundary. AgentCore
    remained disabled, no paid model inference ran, and the final offline suite passes 177 tests.
- [x] Phase 5 — Real Printify draft creation
  - [x] Printify authentication and artwork upload
    - [x] PNG/SVG-preserving writer and independently durable upload checkpoint
    - [x] Secrets Manager runtime configuration and one gated live upload
  - [x] Verified product profile, placement, variants, and integer-cent pricing
    - [x] Live catalog selection: Gildan 64000 blueprint 145, SwiftPOD 39, five colors,
      six sizes
    - [x] Thirty exact variants and three size-specific print canvases
    - [x] $29.99 integer-cent price and centered front-placement policy
    - [x] Live mockup calibration: profile v2 top-aligns square artwork at `y=0.25`
    - [x] Second live canary visually accepted the corrected top alignment
    - [x] Read-only, fail-closed catalog preflight contract
  - [x] Idempotent unpublished product creation and external-ID persistence
  - Live canary evidence: the deployed `us-west-2` workflow read the exact seller secret through
    the prepare role, uploaded one frozen PNG, and created unpublished Printify product
    `6a88bb49f2c2450fa1065afd`. Job `job_0b6c7b32a2794c5682964d498817edb1` stopped at
    `AWAITING_APPROVAL` with separate completed upload and product-create records. No approval,
    publication, order, or fulfillment call occurred.
  - Exit evidence: profile v2 canary product `6a88bd96cf106ff5b30727c5` was visually accepted
    with correct sizing and top alignment. Both canary executions were subsequently stopped and
    verified `ABORTED`; their unpublished products and immutable workflow evidence were retained.
- [ ] Phase 6 — Review and approval interface
  - [ ] Upload, progress, consolidated review, and supported edits
  - [ ] Validation and margin visibility
  - [ ] Explicit approval and clear result states
  - [ ] Usability acceptance without external documentation
- [ ] Phase 7 — Etsy publication through Printify
  - [ ] Approval-version and publish guard verification
  - [ ] Channel publication and status polling
  - [ ] Partial-failure recovery, result links, and immutable reports
  - [ ] Notify the seller only after publication is positively verified complete
- [ ] Phase 8 — Hardening, evaluation, and submission
  - [ ] Security, failure-mode, privacy, and cost review
  - [ ] Evaluation evidence and demo rehearsal
  - [ ] Architecture, setup, disclosure, and submission materials
