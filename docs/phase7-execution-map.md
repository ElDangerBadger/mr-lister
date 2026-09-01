# Phase 7 execution map

## Current decision

**PHASE 7 IN PROGRESS — ACTIVATION ZERO**

Phase 6 remains sealed at seller approval. Phase 7 source contains a substantial, tested
one-shot publication safety core, but no seller publication route, browser control, production
publication worker, or generally enabled publication contract exists. Etsy publication has not
been authorized by deployment.

This document is the authoritative map from the sealed Phase 6 release to a sealable Phase 7
release. It distinguishes code that is complete from capability that is actually deployed and
enabled.

## Protected starting point

| Authority | Current value |
| --- | --- |
| Repository source | `5509457faf8242d75ea1e47ff60a429cf38bd0a3` on `main` |
| Phase 6 runtime source | `15a4f2a657e4cf5809de7066d267455d65c8c835` |
| Phase 6 Provider component | `748e5c4a1e46c500215118685d1f70231b7f28b8bfe8e67cc804da1c33e7c347` |
| Phase 6 decision | `PHASE 6 COMPLETE AND SEALED` for the functional demo scope |
| Publication contract | `7.0.1`, fingerprint `548b710230618e73c20a509f2121799c415b50070e1e2ae7e1b82fe3c37e2981` |
| Contract activation phase | `offline_implementation` |
| Seller publication enabled | `false` |

Phase 7 must not modify the sealed Phase 6 bundle or grant publication capability to any Phase 6
role, route, state machine, agent, or browser control.

## What is already built

| Capability | State |
| --- | --- |
| Separate publication aggregate and immutable request authority | Complete and tested offline |
| Exact owner, approval decision, shop, product, profile, pricing, and release guards | Complete and tested offline |
| Atomic idempotent request transaction and direct receipt recovery | Complete and tested offline |
| One durable mutation permit and maximum-one-POST call budget | Complete and tested offline |
| Printify shop/product GET boundary and exact product publication POST boundary | Complete and tested offline |
| Provider evidence staging and one-step coordinator | Complete and tested offline |
| Positive-proof-only verification and deadline settlement | Complete and tested offline |
| Replay, conflict, partial-failure, consumed-claim, and unknown-outcome recovery logic | Complete and tested offline |
| Safe Etsy result link, immutable report, in-app notification, and retention models | Complete and tested offline |
| Owner-scoped query and publication-request adapters | Complete but unregistered and exact-disabled |
| Real request and worker dependency graphs | Complete but unregistered and exact-disabled |
| Read-only approval guard runtime, bundle builder, SAM template, and verifier | Implemented; current live release not proven |
| Isolated concurrency-one direct-invoke canary and gated request preparation | Implemented source-only; never deployed or invoked |

The existing source is not a publication scaffold in the ordinary sense. The safety-sensitive
domain, persistence, provider, reconciliation, and recovery behavior is already present. The
remaining work is controlled composition, deployment evidence, seller interaction, and live
acceptance.

## Remaining gates and ordered build path

### P7.13 — Recover zero-publication deployment authority

1. Inventory every account that may contain `mr-lister-phase7-dev` before creating or updating it.
2. Rebuild the read-only guard from current `main`; do not reuse the stale ignored private bundle.
3. Verify the exact nine-resource topology and exact-disabled query/request/publication tuple.
4. Deploy only the separate guard stack, with no secret, provider transport, application write,
   route, trigger, Function URL, or resource policy.
5. Capture the immutable artifact coordinates, stack state, Lambda code/configuration, IAM,
   alarms, absence proofs, status invocation, rejected-authority invocation, and rollback tuple.

This checkpoint cannot publish and does not advance contract `7.0.1` beyond
`offline_implementation` by itself.

### P7.14 — Complete the offline seller/API browser matrix

Build the publication request and status experience as a Phase 7-owned surface while keeping it
unregistered and unreachable in the sealed Phase 6 deployment. The matrix must cover:

- an explicit irreversible confirmation for the exact approved listing;
- strong approval ETag and idempotency binding;
- conflict recovery without silently resubmitting;
- reload/restart recovery from the durable status projection;
- requested, preflight, publishing, verifying, reconciling, published, failed, and
  outcome-unknown states;
- a safe verified Etsy link and in-app completion notification; and
- keyboard, focus, status announcement, failure, and retry basics.

No browser control becomes reachable while contract `7.0.1` remains generally disabled.

**Source checkpoint complete.** The strict contracts, authenticated GET/POST client, and
publication workspace live under `web/offline/phase7` outside the active Vite import graph. The
frozen activation composer fails before constructing a client, active `web/src` contains no
publication route or marker, and production-build verification rejects any leaked Phase 7 route,
confirmation literal, or workspace marker. The browser/API matrix covers explicit confirmation,
exactly one request, stable in-memory idempotency authority, conflict and response-loss recovery
through GET only, restart from every durable state, terminal unknown outcome, canonical Etsy
links, positive-verification-only notification, read-only retry, keyboard cancellation, and focus
return. Full web verification is green with 131 tests. This source checkpoint does not register or
deploy a route and does not change the current activation phase.

### P7.15 — Complete the production infrastructure and alarm matrix

Compose and seal separate publication query/request functions, dispatcher, bounded polling
workflow, worker, dead-letter/recovery path, retention integration, least-privilege roles, logs,
alarms, and operational notification. Keep every seller route and provider mutation disabled
during source and infrastructure verification.

**P7.15A inert-topology checkpoint complete.** A separate
`infra/phase7/production-disabled-template.json` defines the intended six role-separated
functions, one bounded Standard workflow, recovery and dead-letter queues, retention and
dispatcher seams, payload-free workflow logging, encrypted alarm topic, and complete alarm
categories. Every one of its 41 resources shares a condition that cannot be true for any allowed
parameter value; its stream/SQS mappings and EventBridge rules are independently disabled; it has
no API, Function URL, seller route, enabled output, or active handler. The workflow invokes only
one future worker, uses fixed 1-second action and 20-second verification waits, stops after at most
91 one-step invocations, and has an absolute 1,860-second timeout. JSON, structural/IAM/alarm
tests, Ruff, and SAM lint pass. Provider-free dispatcher/recovery/retention implementations and
separate release closures remain P7.15B/C work, so the overall infrastructure matrix is not yet
closed and nothing from this template is deployable.

### P7.16 — Deployed read-only validation

After the two offline matrices pass, reconcile the sealed Phase 6 component-bound deployment
authority, invoke the zero-call guard, and run the separately approved GET-only Printify/Etsy
preflight. Publication must be structurally impossible. Record immutable evidence before changing
the contract activation phase.

### P7.17 — One-listing MassSkutiny canary

First close two operator-tooling gaps: mint a `publish_once` binding only from a completed durable
read-only preflight proof, and add deterministic canary deployment/readback plus terminal-evidence
verification. Then stop for explicit approval of one exact MassSkutiny job and listing.

The authorized canary has reserved concurrency one, one aggregate, one product, one mutation
permit, at most one publication POST, exact-product polling, and no retry POST. Success requires a
positively verified Etsy identity, safe result link, immutable report, and in-app notification.
Infrastructure rollback cannot unpublish the external Etsy listing, so this step is intentionally
irreversible.

### P7.18 — General availability and seal

Only after a successful canary:

1. freeze a new reviewed contract that explicitly permits general availability;
2. register the owner-scoped routes and enable the already-tested seller control;
3. deploy the production worker/poller/recovery/notification topology;
4. exercise positive, stale-approval, repeat, cross-owner, race, failure, restart, accessibility,
   and authenticated seller acceptance;
5. capture source, artifact, deployment, CI, canary, rollback, and operational evidence; and
6. declare either `PHASE 7 COMPLETE AND SEALED` or the smallest exact blocker list.

## Live-state inventory at map creation

On 2026-09-01, the authenticated `mr-lister-bootstrap` profile reported no CloudFormation stack
whose name contains `phase7`; `mr-lister-phase7-dev` does not exist in that account. The
`mr-lister-dev` session was expired, so its live state remains unverified until that exact profile
is authenticated. The repository contains no authoritative Phase 7 deployment release record,
and the ignored private guard and worker artifacts are stale against current source. Therefore no
Phase 7 deployment or rollback tuple is currently claimed.

A replacement zero-publication guard candidate was then rebuilt from `main` and verified locally:

| Candidate authority | Value |
| --- | --- |
| Guard release | `625eeb88fff6f9f801d7e2320efa08a1d2567f077145b394229b9a2c33717fe3` |
| Archive SHA-256 | `f77b3bb41b86ae4afcdd17428de140c8def78551ef92bfd5cefa9dc1fafeac84` |
| Archive size | `30,870,582` bytes |
| Deployment-manifest SHA-256 | `26fc7e187fd66670c367d23a78c7c5cba160a89be72ce8f9547ce9f449664a64` |
| Product-profile fingerprint | `5de1257141cfdacb1731f68bb9113712957483b33d5b0f7115afdba86eb7476c` |

The checked dependency artifact, deterministic archive, release-first verifier, and focused
read-only infrastructure tests passed. This candidate is retained only in the ignored private
release area. It has not been uploaded or deployed and is not yet a rollback point.

## Stop lines

- A guard deployment is not publication authorization.
- A GET-only preflight is not publication authorization.
- Creating the canary publication aggregate is durable application authority and requires an
  exact reviewed target.
- Invoking `publish_once` is the first provider mutation and requires explicit operator approval.
- Contract `7.0.1` cannot authorize a seller-facing publication route or general availability.
- No Phase 7 deployment may add publication, order, fulfillment, deletion, or unpublish authority
  to the sealed Phase 6 runtime.
