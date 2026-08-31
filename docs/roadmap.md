# Mr Lister roadmap and phase map

The phases describe dependency order. Later capabilities may be scaffolded early, but no phase
may bypass an earlier safety exit criterion.

## Phase 0: Foundation and proof ledger

Establish the public repository, license, product boundary, versioned contracts, architecture
decisions, demo target, security baseline, tests, and CI. No live service writes.

**Exit:** clean install, green tests and lint, no credentials, one frozen vertical-slice scope.

## Phase 1: Local vertical skeleton

Implement job state transitions, local upload/status/review/approve/publish/report APIs, fake
Bedrock and Printify adapters, idempotency ledger, and one complete fixture-driven integration
test.

**Exit:** one PNG traverses the fake workflow; invalid input and stale approval fail safely;
retries do not duplicate work.

## Phase 2: Bedrock listing intelligence

Add multimodal artwork inspection, schema-constrained listing generation, exactly 13 validated
tags, bounded repair, private diagnostics, and a representative evaluation set.

**Exit:** every accepted response conforms to application contracts; tests remain runnable
without live inference.

## Phase 3: Strands agent and AgentCore Runtime

Expose a bounded tool surface, enforce recommendation-versus-authorization instructions,
deploy the preparation path to AgentCore Runtime, and add sanitized observability.

**Exit:** the agent can inspect, draft, validate, revise, and explain but cannot bypass safety
guards.

## Phase 4: Durable AWS workflow

Persist private artifacts in S3 and operational state in DynamoDB. Add Step Functions for the
prepare, review wait, publish, and verify lifecycle; integrate Secrets Manager and least-
privilege IAM.

**Exit:** a job survives process exits, pauses for approval, resumes safely, and remains
idempotent.

## Phase 5: Real Printify draft

Add Printify authentication, image upload, the verified apparel profile, calibrated placement,
integer-cent pricing, real unpublished product creation, and external-ID persistence.

**Exit:** one owned PNG produces exactly one correct unpublished product across retries.

## Phase 6: Review and approval interface

Correct the human-decision lifecycle, preserve one unpublished Printify product across revisions,
add owner-scoped cloud upload and review APIs, and build upload, durable progress, consolidated
artwork/listing/product review, supported edits, validation, estimated-proceeds visibility,
explicit approval or cancellation, and clear result states.

**Exit:** an invited, authenticated first-time seller with a configured Printify connection can
submit one or multiple required PNG/SVG artworks without documentation and recover each job after
refresh. JPG/JPEG may enter through the same canonical PNG normalizer; PDF is not a Phase 6
blocker. Transparent/opaque and square/portrait/landscape inputs preserve their aspect ratios
without crop, padding, distortion, or square enforcement. Every revision updates the same
unpublished product for that job; approval ends at `APPROVED`; cancellation needs no operator; and
no Phase 6 path can publish, order, or fulfill. Every visible control works,
explains why it is unavailable, or is removed. The same seller job's durable `PREPARE` request
must pass fail-closed through the deployed AgentCore Strands runtime, produce a correlated
structured decision and sanitized audit, and have no direct non-Strands preparation fallback in
the submission deployment.

## Phase 7: Etsy publication through Printify

Add approval-version verification, guarded channel publication, status polling, safe partial-
failure recovery, result linking, and immutable run reports.

**Exit:** an approved product publishes once; stale, invalid, unapproved, or repeated requests
cannot produce another listing.

## Phase 8: Hardening and submission

Complete happy-path, failure, idempotency, privacy, and safety tests; run multi-style artwork
evaluation; document deployment and costs; finalize the architecture diagram, demo, backup
recording, public README, and submission materials.

**Exit:** a clean deployment repeatedly demonstrates artwork upload through verified
publication with no exposed credentials, dead controls, or unexplained manual steps. The public
README, architecture, evidence map, Devpost description, and demo independently identify Strands
as the core agent loop, and the recorded end-to-end run joins Strands tool evidence to the same
seller job that reaches human review and guarded publication.

## Outside the hackathon vertical slice

- durable bulk-job orchestration beyond the bounded in-browser multi-file submission;
- multiple product profiles or stores;
- Shopify and other marketplace adapters;
- custom mockup generation;
- performance-based keyword analytics;
- trend intelligence;
- experiments and optimization loops;
- agency workspaces and billing;
- autonomous bulk publication.
