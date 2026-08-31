# Phase 6.2 provider and Strands integration evidence

Phase 6.2 implements the offline application and worker core for durable preparation, one-product
draft synchronization, reconciliation, and estimated-proceeds evidence. It does **not** declare the
Phase 6 cloud path deployed. The new SAM application remains deliberately fail-closed with
`DeploymentReadiness=SCAFFOLD_ONLY` until its Lambda composition adapters are implemented, deployed,
and accepted through an explicitly authorized canary.

## Application authority

The `2.0.0` control boundary remains authoritative over every lifecycle decision. Models, Strands,
Step Functions, and provider responses can supply bounded evidence; none can select an application
state. Every worker success or failure settles through an application command that atomically
updates the exact Job CAS, immutable evidence, ordered event, idempotency receipt, and current or
next work request.

The Phase 6 worker boundary now includes:

- pinned source-artifact, artwork-analysis, review, Strands-agent, provider-upload,
  provider-write, product-sync, reconciliation, pricing, and proceeds-evidence records;
- application-derived cancellation dominance and stage-aware recovery;
- one-shot provider-call permits with atomic consume or retirement;
- full-payload CAS parity between the in-memory authority oracle and DynamoDB;
- an immutable pricing snapshot paired in the same transaction with the complete per-variant
  economics evidence it summarizes.

## Durable Strands preparation

[`agent/phase6.py`](../src/mr_lister/agent/phase6.py) constructs the real `strands.Agent` and exposes
exactly one Phase 6 `@tool`, `record_prepared_review`. The durable preparation path is checkpointed:

1. application code begins the exact active `PREPARE` work;
2. a pinned S3 `VersionId`, size, SHA-256, original PNG dimensions, mixed-alpha visibility, and
   exact product profile are revalidated before inference;
3. the artwork analysis and complete listing are stored once as immutable evidence;
4. Strands returns a bounded structured decision with fixed framework and agent identity;
5. application code revalidates the result and chooses `NEEDS_REVISION` or queues exact product
   synchronization work.

The AgentCore bridge binds owner, job, work, input fingerprint, opaque session correlation,
`framework=strands-agents`, and `agent_id=mr-lister-preparation`. There is no direct-model or
deterministic preparation fallback. A failure after the intelligence checkpoint resumes the agent
decision without buying a second artwork/listing inference.

This source path is credential-free tested through the genuine Strands loop. The still-open live
gate is proving the same correlation on the deployed seller job.

## One upload and one product

The draft worker is deliberately asymmetric:

- one deterministic, source-bound filename may produce one provider upload for the Job;
- one initial product `POST` may establish the Job's immutable product ID;
- later review versions may only `PUT` that same product ID;
- every update performs an immediate GET and requires the exact prior canonical draft before PUT,
  so manual or external drift fails without mutation;
- every successful upload and product write requires exact provider GET readback before application
  evidence is committed;
- a consumed permit, ambiguous response, process crash, or timeout can only route to bounded
  GET-only reconciliation—never another blind upload, POST, or PUT;
- one exact-prior PUT retry may occur within the original attempt's inherited deadline; a second
  ambiguity or expired deadline ends terminally without another mutation;
- an available but unused permit can be resumed safely or atomically retired by cancellation;
- a consumed unresolved permit can never mint a replacement attempt.

The Phase 6 source envelope is capped at 5 MiB because the implemented provider path uses bounded
base64 upload. The older 25 MiB intake capability remains intact outside Phase 6. Supporting larger
Phase 6 artwork is deferred until a short-lived URL can be proven to bind the exact private S3
Bucket, Key, and `VersionId` end to end.

## Estimated proceeds

Economics refresh is read-only. It joins exact current product-cost readback with the Printify V2
standard-U.S. shipping response and applies the frozen `etsy-us-standard-v1` policy using integer
cents and integer half-up fee rounding. Complete evidence includes each variant's retail price,
production cost, shipping cost, fee components, estimated proceeds, freshness window, and minimum
and maximum range. It is an estimate for seller review, not accounting or payout authority.

The immutable evidence contracts live in [`control/economics.py`](../src/mr_lister/control/economics.py);
provider-specific retrieval and calculation depend inward on that boundary. The control package
does not import Printify or another production adapter.

## Infrastructure scaffold

[`infra/phase6`](../infra/phase6) defines separately named Phase 6 resources without modifying the
retained Phase 4/5 evidence stacks:

- a private, versioned artwork bucket and retained encrypted DynamoDB table;
- a `DueWorkIndex`, keys-only stream, and `WORK#` stream filter;
- four short-lived Standard machines: prepare, synchronize product, reconcile product, and refresh
  economics;
- distinct dispatcher, preparation, provider-draft, and settlement roles;
- identifier-only state-machine inputs, execution-data logging disabled, and 14-day log retention;
- no approval callback, publication, order, fulfillment, or archive task.

The Lambda handlers still raise `Phase6ScaffoldNotReady`. Deployment is blocked until real adapters
construct the DynamoDB store and the exact Strands/provider/settlement services, the readiness marker
is deliberately changed, and offline plus approved live acceptance passes.

## Verification

The current offline gate covers genuine Strands execution, bridge correlation, pinned-source
production, application/store/DynamoDB parity, dispatch races, upload and product ambiguity,
same-product revision, manual provider drift, cancellation, immutable economics, static no-commerce
authority, SAM policy/topology assertions, and legacy regression.

Latest accepted local verification:

- all tests: 557 passed, including 347 Phase 6 tests; 11 explicitly gated live-Bedrock tests
  skipped;
- SAM template: `sam validate --lint` passed;
- SAM package: `sam build` passed for all four scaffold functions;
- Ruff lint, Ruff format check, and `git diff --check`: passed.

The seven warnings in the full suite are deprecations emitted by installed Bedrock AgentCore
dependencies, not application failures.

## Open Phase 6.2 deployment gate

- replace the four fail-closed Lambda shims with tested composition adapters;
- deploy the separately named Phase 6 stack and Phase 6 AgentCore runtime;
- prove the same owner-scoped Job traverses pinned upload, AgentCore/Strands, structured preparation,
  one unpublished provider product, economics projection, and the human decision boundary;
- verify exact IAM, secret rotation/revocation, provider eventual consistency, privacy, latency, and
  cost through explicitly authorized live canaries;
- retain zero Phase 6 publication, order, or fulfillment calls.
