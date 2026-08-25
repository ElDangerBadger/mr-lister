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
  - [x] Phase 6.0 product, state, security, commercial, and acceptance contracts frozen
    - [x] Persisted application-state human pause and Phase 6/7 authority boundary
    - [x] One-Printify-product-per-job revision policy
    - [x] Invite-only, owner-scoped cloud review boundary
    - [x] Supported input, seller journey, actions, API, accessibility, and non-goals specified
    - Evidence: [`phase6-seller-control-contract.md`](phase6-seller-control-contract.md) and ADRs
      [0009](architecture/0009-phase6-seller-control-boundary.md),
      [0010](architecture/0010-one-printify-product-per-job.md), and
      [0011](architecture/0011-owner-scoped-cloud-review-boundary.md).
  - [x] Phase 6.1 application-owned revise, approve, cancel, retry, and failure lifecycle
    - [x] Isolated `2.0.0` lifecycle, command, immutable-evidence, and safe-error contracts
    - [x] Atomic Job CAS, event, receipt, decision/evidence, and outbox command boundary
    - [x] In-memory authority oracle and full-payload-CAS DynamoDB adapter parity
    - [x] Deterministic allowlisted dispatcher with fast-worker and ambiguous-start recovery
    - [x] Terminal approval, cancellation dominance, closed retry policy, and one-product authority
    - Evidence: [`phase6-lifecycle-implementation.md`](phase6-lifecycle-implementation.md), 100
      focused Phase 6.1 tests. No provider, deployment, publication, order, or fulfillment call ran
      in that slice.
  - [ ] Required Strands production and submission path (blocking Phase 6 exit)
    - [x] Real `strands.Agent`, four job-scoped `@tool` functions, bounded structured output, and
      deployed AgentCore synthetic canary
    - [x] Public architecture loop, evidence map, sanitized canary summary, and explicit
      `strands-agents` runtime/audit identity
    - [ ] Durable `PREPARE` work invokes the exact AgentCore Strands runtime fail-closed, with no
      direct non-Strands preparation fallback in the submission deployment
    - [ ] Strands response/audit correlation is joined to the same owner-scoped job displayed in
      consolidated seller review
    - [ ] End-to-end acceptance proves upload -> Strands model/tool loop -> structured preparation
      decision -> staged listing -> human decision gate
  - [ ] Phase 6.2 same-product Printify synchronization and reconciliation
    - [x] Pinned-source producer and checkpointed real-Strands preparation path with exact
      AgentCore response/correlation contracts and no non-Strands fallback
    - [x] One-shot, source-bound upload with exact GET readback, revision reuse, permit retirement,
      and bounded GET-only ambiguity recovery
    - [x] Exactly one initial product POST, later same-ID PUT, exact-prior drift guard, exact final
      readback, and bounded reconciliation without blind mutation retry
    - [x] Read-only current-cost and standard-U.S. shipping retrieval with immutable
      `etsy-us-standard-v1` per-variant proceeds evidence
    - [x] Separately named Phase 6 SAM topology, least-capability role assertions, keys-only filtered
      dispatch stream, four Standard machines, SAM lint, and SAM build
    - [x] Offline gate: 557 tests passed, including 347 Phase 6 tests; 11 explicitly gated live
      Bedrock tests skipped; Ruff lint/format and `git diff --check` passed
    - [ ] Replace fail-closed `SCAFFOLD_ONLY` Lambda shims, deploy the Phase 6 runtime/stack, and pass
      same-job Strands plus one-unpublished-product live acceptance
    - Evidence: [`phase6-provider-integration.md`](phase6-provider-integration.md).
  - [x] Phase 6.3 consolidated artwork, listing, mockup, product, validation, and
    estimated-proceeds application projection
    - [x] Owner-first read-only join across exact source, review, analysis, Strands, product,
      representative mockup, pricing, and failure authority
    - [x] Durable color/size/placement evidence for all 30 variants and product/provider labels
      derived only from immutable profile authority
    - [x] Closed seller capability matrix with real ETag-bound economics refresh and authoritative
      approval gate for safe mockups plus fresh complete economics
    - [x] Five-minute exact-origin artwork-preview port, hostile mockup URL fail-closed checks, and
      recursive private-field leakage assertions
    - [x] Offline gate: 638 tests passed, including 428 Phase 6 tests; 11 live-Bedrock tests skipped;
      SAM lint/build, Python package builds, Ruff lint/format, and `git diff --check` passed
    - Evidence: [`phase6-review-projection.md`](phase6-review-projection.md).
  - [ ] Phase 6.4 Cognito identity, private direct upload, and owner-scoped cloud API
    - [x] Invite-only Cognito/JWT contract with claims-derived owner identity, exact protected route
      allowlists, owner-first reads, safe errors, CORS, and privacy-safe logs
    - [x] Exact-key `1..5 MiB` direct POST with checksum/size/type/AES256 constraints,
      `staged`/`pinned` version tags, safe post-expiry reauthorization without `ListBucket`, and
      DynamoDB/S3 one-day cleanup boundaries
    - [x] Atomic upload completion creates the consumed intent, job, canonical source, event,
      receipt, and pending `PREPARE` work; owner-indexed recent-job recovery avoids table scans
    - [x] Authenticated owner-checked artwork-preview endpoint returns a bodyless, non-cacheable
      `302` to an exact-`VersionId` S3 GET valid for at most five minutes, with no opaque grant,
      KMS dependency, or API artwork-byte proxy
    - [x] Offline owner-scoped upload, query, seller-command, preview, and SAM contracts implemented
    - [x] Offline gate: 824 tests passed, including 614 Phase 6 tests; 11 live-Bedrock tests skipped;
      SAM lint/build, Python wheel/source builds, compileall, Ruff lint/format, and
      `git diff --check` passed
    - [ ] Compose the tested adapters into the fail-closed Lambda handlers, remove
      `SCAFFOLD_ONLY`, and deploy the Phase 6 stack
    - [ ] Pass deployed cross-owner, upload-expiry, exact-version preview, concurrency, and same-job
      Strands live acceptance; no Phase 6.4 live/deployment gate is claimed by the offline slice
  - [ ] Phase 6.5 accessible seller interface with refresh and conflict recovery
    - [x] Browser, same-origin hosting, PKCE storage, upload recovery, preview, accessibility, and
      server-directed authority decisions frozen in
      [`phase6-accessible-seller-interface.md`](phase6-accessible-seller-interface.md) and ADR
      [0012](architecture/0012-phase65-browser-and-hosting-boundary.md)
    - [x] Strict TypeScript/Vite application, versioned runtime-validated browser contracts, and
      frontend CI
    - [x] Memory-only Cognito PKCE session recovery and owner-scoped recent-job/upload recovery
    - [x] Direct upload with measured bytes, expiry/cancellation recovery, and durable completion
    - [x] Consolidated progress/review with prominent Strands provenance, thirteen labelled tags,
      mockups, complete economics, and persistent unpublished authority
    - [x] Version/ETag/idempotency-bound edit, refresh, approve, cancel, and retry with local-edit
      conflict preservation
    - [ ] WCAG 2.2 AA component/browser gates across keyboard, focus, screen-reader semantics,
      contrast, reduced motion, and 200-percent zoom
      - [x] Semantic component/axe coverage and partial real-Chrome desktop/mobile, keyboard, focus,
        reduced-motion, conflict, preview-recovery, and fail-closed evidence
      - [x] Re-run one digest-bound final bundle in Chromium, Firefox, and WebKit for forced colors,
        reduced motion, route-race isolation, hidden-tab polling, and 360-CSS-pixel reflow
      - [ ] Complete manual screen-reader and contrast evidence plus the remaining edit, refresh,
        cancel, retry, upload, and logout browser journeys
    - [ ] Private static hosting, same-origin cache-disabled `/v1/*`, CSP/security headers, SPA
      routing, observability, and deployed non-destructive smoke
      - [x] Offline private-S3/OAC/CloudFront topology, exact SPA routes, security headers, and
        uncompressed strong-ETag `/v1/*` behavior
      - [ ] Compose the `SCAFFOLD_ONLY` handlers, deploy, and pass the non-destructive smoke
    - Evidence: 846 Python tests passed, including 636 Phase 6 tests; 11 gated live-Bedrock tests
      skipped. The web gate passes 62 tests, lint, strict typecheck, production build, artifact
      hygiene, and an audit with zero high-severity vulnerabilities. Ruff, contract drift,
      compileall, SAM lint/build, and `git diff --check` pass. The later Phase 6.6 tri-engine matrix
      is recorded in [`phase6-accessible-seller-interface.md`](phase6-accessible-seller-interface.md),
      while full WCAG and deployed evidence remain open.
  - [ ] Phase 6.6 replay, concurrency, cross-owner, live-canary, and first-time-user acceptance
    - [x] Freeze the 12-gate manifest, closed structural evidence schema, and authoritative runtime
      semantic validator, including deterministic checked artifacts and CI drift detection
    - [x] Prove exact command replay, changed-body idempotency conflicts, forced three-way
      revise/approve/cancel concurrency, and foreign-versus-unknown behavior across all 14
      protected routes in the offline oracle
    - [x] Implement fresh owner-bound Printify secret resolution, exact-version source reads,
      draft-only provider resources, and an identifier-free allowed/rejected provider call ledger
    - [x] Implement role-separated upload/query/command API composition roots with exact pinned
      profile/config authority while leaving the SAM handlers fail closed
    - [x] Implement the reference-aware source-retention core with lifecycle delete-marker
      pagination, bounded durable checkpoints, trusted inventory time, and recent-pin preservation
    - [x] Verify one exact final browser bundle in Chromium, Firefox, and WebKit with a shared
      SHA-256 authority, a sanitized report, and credential-free per-engine trace evidence
    - [x] Add the evidence-set/artifact verifier that closes prerequisites, record counts,
      cross-record run/job bindings, and on-disk artifact digests
    - [x] Add concrete exact-prefix retention AWS adapters, bounded schedule, checkpoint, and
      least-capability IAM without object-byte read or delete authority
    - [x] Add the separate 90-day terminal operational-record cleanup boundary, including bounded
      completed-upload intent and upload-receipt TTLs
    - [x] Implement role-separated dispatcher, preparation, provider, settlement, API, and
      retention composition roots; add a dedicated Phase 6 AgentCore source entrypoint that
      visibly runs Strands over pinned Gemma; and generate reproducible narrow source manifests
    - [x] Bind both sealed source trees and an immutable versioned AgentCore endpoint into release
      authority; add deterministic Linux ARM64 build requests, native-artifact inspection, and
      cross-component release sealing
    - [x] Produce the real controlled Linux ARM64 dependency artifacts, run target import smoke,
      wire the sealed Lambda `CodeUri` and AgentCore release, and deploy the composed handlers in
      fail-closed inert core staging
    - [x] Add read-before-settle stuck-execution recovery, bounded schedules/DLQ, and closed
      operational alarms without workflow-redrive or provider authority
    - [x] Complete the reviewed inert core deployment: `CORE_RELEASE_BOUND_STAGED`, 47 complete
      resources, seven exact-release Lambdas, four active Standard state machines, five disabled
      triggers, exact zero concurrency on the three maintenance functions, retained private
      foundation resources, and no public web surface
    - [ ] Implement and review the activation evidence gate: remove the exact three zero
      concurrency settings while triggers remain disabled, verify their live absence, and only
      then remove `SCAFFOLD_ONLY` and enable the reviewed triggers in a later update
    - [ ] Run explicitly authorized deployed non-destructive, double-gated unpublished Printify,
      and moderated first-time-seller acceptance and attach sanitized evidence
    - Evidence: the quota-compatible deployment checkpoint at source commit `678ea4f` passed 2,318
      Python tests with 11 gated live-Bedrock skips plus the 62-test web gate, lint, strict
      typecheck, production build, SAM lint, and diff hygiene. The live `us-west-2` stack reached
      `UPDATE_COMPLETE` with no failure or rollback event and remains deliberately unactivated.
      Acceptance details: [`phase6-acceptance-hardening.md`](phase6-acceptance-hardening.md).
- [ ] Phase 7 — Etsy publication through Printify
  - [x] Freeze publication-disabled contract 7.0.1: separate aggregate authority, complete
    one-shot permit semantics, positive-proof-only GET reconciliation, verified safe link,
    notification, terminal-settlement retention, and three-scope activation
    ([ADR 0013](architecture/0013-one-shot-verified-channel-publication.md),
    [detailed contract](phase7-publication-contract.md))
  - [x] Phase 7.1 authority prerequisites: new approvals retain the immutable decision ID,
    new product synchronizations retain the owner-bound Printify shop ID, and legacy rows remain
    readable while failing closed for publication when either prerequisite is absent
  - [x] Phase 7.1 offline request authority: strict immutable snapshot/attempt/permit/work/event/
    receipt records, receipt-first idempotency, one separate aggregate, and the exact atomic
    15-action DynamoDB request transaction, including its store-derived direct receipt locator;
    publication remains uncomposed and disabled
  - [x] Phase 7.2 offline/uncomposed checkpoint: provider-free one-shot execution models, store,
    and service plus an isolated sealed three-route Printify boundary form an offline oracle only,
    not a runnable or activatable publication path. Shared evidence DTOs and their fingerprints are
    capability-free and caller-computable, so they do not prove provider provenance; package
    exports and Phase 6 bundles, API, UI, SAM, IAM, and state machines remain publication-free,
    publication stays disabled, and no live provider call ran
  - [x] Phase 7.3 offline durable provider-evidence staging/coordinator: the sealed boundary stages
    only claim-, authority-, kind-, audit-, and fingerprint-bound sanitized evidence, and the outer
    coordinator derives every command from durable authority. Staging and execution share the same
    atomic store boundary, each stage is consumed at most once with its state transition, a replay
    mints no new wire grant, and seller/API paths cannot submit execution-record commands
  - [x] Phase 7.3 trusted negative-evidence classifier: an exact provider-bound structured negative
    stage may retire AVAILABLE authority before the deadline only for the closed shop/channel,
    product-missing, locked, already-published, canonical, variant, placement, or mockup mismatch
    set. Authentication, throttling, server, malformed-response, and transport failures cannot use
    this path; absent trusted evidence still waits for `PRE_CALL_DEADLINE_EXPIRED`
  - [x] Phase 7.3 offline persistence/read checkpoint: the injected DynamoDB adapter renders exact
    same-key CAS transactions for execution, audit watermarks, stages, and stage consumptions; the
    owner-first seller projection and exact GET adapter expose disabled status with no-store/ETag
    semantics. None is composed into Phase 6 Lambda, API, IAM, browser, dispatcher, or bundles;
    `publication_enabled` and `request_enabled` remain false and no live provider call ran
  - [x] Phase 7.4 offline draft-profile eligibility and disabled read scaffold: the exact checked
    Phase 6 profile remains `publish_enabled=false`, while a separate immutable release/profile/
    channel eligibility record grants neither a seller request nor a provider mutation. Request
    and execution services require that record, and the capability-free pre-call guard re-reads the
    exact approval version, snapshot, shop, pricing, profile, release, and eligibility authority.
    A separate Phase 7 SAM scaffold contains one unregistered query Lambda with only bounded
    `GetItem`/`Query` authority and exact-false query/request/publication flags; it packages no
    application bundle and cannot read or mutate provider or application state
  - [x] Phase 7.5 offline retention and credential containment: after exact terminal settlement, an
    injected adapter assigns the immutable +90-day TTL to every publication row, the linked job,
    and the directly located owner receipt before writing one marker-last terminal-graph proof. The
    existing Phase 6 source sweeper remains the only retention tag writer and requires that marker
    plus a strong exact terminal-aggregate reread before the +30-day source release. Provider
    credentials are resolved fresh and bound to the exact owner, shop, aggregate, snapshot, and
    provider authority before any durable call claim, then revalidated through an opaque,
    non-serializable capability. The retention and credential adapters remain offline and
    uncomposed; all activation flags remain false, with no route, UI, provider/mutation IAM,
    scheduler, deploy, or live provider call. The only IAM change narrowly expands the existing
    Phase 6 retention role's same-table transactional reads from `JOB#*` to `JOB#*` plus
    `PUBLICATION#*`
  - [ ] Compose, seal, deploy, and verify the approval-version and publish guard in a runtime
  - [ ] Channel publication and status polling
  - [ ] Partial-failure recovery, result links, and immutable reports
  - [ ] Notify the seller only after publication is positively verified complete
- [ ] Phase 8 — Hardening, evaluation, and submission
  - [ ] Security, failure-mode, privacy, and cost review
  - [ ] Evaluation evidence and demo rehearsal
  - [x] Public README callout, core Strands loop diagram, code/test traceability map, and sanitized
    AgentCore canary summary
  - [ ] Devpost description contains **How Mr Lister Uses Strands Agents** and links the evidence map
  - [ ] Demo names Strands in the first 30 seconds and shows the same job's sanitized tool trace,
    structured response, staged listing, and human gate
  - [ ] Architecture, setup, disclosure, and remaining submission materials
