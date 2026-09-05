# Phase 7 execution map

## Current decision

**PHASE 7 COMPLETE AND SEALED — FUNCTIONAL HACKATHON-DEMO SCOPE**

Phase 6 remains sealed at seller approval. The P7.15C production-disabled operations and rollback
checkpoint is closed, the P7.16 deployed GET-only validation passed, and one exact P7.17
concurrency-one canary completed a real Printify-to-Etsy publication on 2026-09-05. The P7.18
contract is frozen and the generally available seller routes, production worker, and control plane
are now deployed and have passed exact readback. The versioned seller-web release also passed
canonical readback, CloudFront activation, and public GET checks. The authenticated owner-scoped
status route then returned `200` through the live seller UI without a publication POST. Release
candidate `022bdb62b6d7e4e8ac3c129e943f48e4256a6c5c` passed `main` CI run `33985664447`. The
functional hackathon-demo seal is effective, with no demo blockers remaining.

The canary's external outcome is positive, but its durable aggregate did not reach the strict
`PUBLISHED` terminal evidence state before the immutable verification deadline. The positive Etsy
identity appeared in provider readback roughly two minutes after that deadline. The run is
therefore recorded as externally verified functional evidence, not as a passing
`verified_published` terminal-verifier result. No runtime or contract exception is implied.

This document is the authoritative map from the sealed Phase 6 release to a sealable Phase 7
release. It distinguishes code that is complete from capability that is actually deployed and
enabled.

## Protected starting point

| Authority | Current value |
| --- | --- |
| Original Phase 6 protected source baseline | `5509457faf8242d75ea1e47ff60a429cf38bd0a3` |
| Phase 7 P7.15B source checkpoint | `0e3c150cb9cfa11c8047db3a4670f8ec5aa6d864` on `main` |
| Phase 7 P7.15C deployed source checkpoint | `7c933dd2cfd76e418d57ce1e25d9f6ffe3c69d3f` on `main` |
| P7.15C production-disabled release | `9c4deca1813e5d1e8cc3f6747681b2194265f9c0b51b64fd9cf6b8afeb823c46` |
| P7.15C production-disabled archive | `43721a48802bd3bbc946671aff938b6df030b495975c8bc59839db18986da88f` |
| Phase 6 runtime source | `06484524ed8ff8b9211c5f5bd1f0bcc4d4f540bc` |
| Phase 6 Provider component | `a4f00b79d7b6f4ef676981b05a4cc369645d09f53921d8939e06a851e7e9b8f5` |
| Phase 6 application binding | `0c6211a5b0244e9c86d635e6c02e7bc49e5e948d68895b4aaa982c0b0b2e187b` (unchanged) |
| Phase 6 decision | `PHASE 6 COMPLETE AND SEALED` for the functional demo scope |
| Publication contract | `7.1.0`, fingerprint `5172926cb89f8c046247922d8311c3f8b6361a9d67a719aa3a19a1c0ef1ed678` |
| Contract activation phase | `general_availability` |
| Seller publication enabled | Backend and web release active; authenticated read acceptance passed |

Phase 7 must not modify the sealed Phase 6 bundle or grant publication capability to any Phase 6
role, route, state machine, agent, or browser control.

The Phase 6 provider component was resealed on 2026-09-04 after the live seller-revision path
exposed two narrow synchronization defects. The final authenticated MassSkutiny canary created an
editable unpublished draft at review 1, synchronized a controlled title-only review 2 to the same
product through Printify's partial-update boundary, retained five mockups and all 30 configured
variants, refreshed live economics, and reached explicit unpublished approval. This changed no
Phase 7 capability or activation state.

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
| Owner-scoped query and publication-request adapters | Registered, enabled, and read back under contract `7.1.0` |
| Real request and worker dependency graphs | Deployed in the exact P7.18 enabled release |
| Read-only approval guard runtime, bundle builder, SAM template, and verifier | Deployed and invoked for the exact P7.16 target; all four GET-only stages passed with no provider mutation |
| Isolated concurrency-one direct-invoke canary and gated request preparation | Invoked once for the exact approved P7.17 product, then its stack, function, and role were deleted; the 14-day log group remains |
| Provider-free dispatcher, same-ARN recovery, deadline settlement, and terminal-retention control plane | Enabled in P7.18 and passed exact deployment readback |
| Lost-event recovery index/sweep and failed-start readback | Complete locally; bounded at 25, same-ARN-only, no scan or provider construction |
| Six release-first production-disabled entrypoints and deterministic ARM64 artifact | Preserved as the verified rollback predecessor |
| Production-disabled SAM topology, target retry/DLQ, alarm KMS, and restricted redrive authority | Preserved as the verified rollback predecessor |
| Due/recovery preflight, one-message DLQ triage, and operations runbook | Complete as injected source boundaries; no default AWS adapter or live operations-drill evidence yet |

The safety-sensitive domain, persistence, provider, reconciliation, recovery, enabled backend, and
versioned web release are present. The final release record is committed and `main` CI is green.

## Completed gates and execution record

### P7.13 — Recover zero-publication deployment authority

1. Preserve the existing P7.15C `mr-lister-phase7-dev` stack and use the separate exact guard
   stack name `mr-lister-phase7-guard-${EnvironmentName}`.
2. Rebuild the read-only guard from current `main`; do not reuse the stale ignored private bundle.
3. Verify the exact nine-resource topology and exact-disabled query/request/publication tuple.
4. Deploy only that separate guard stack, with no secret, provider transport, application write,
   route, trigger, Function URL, or resource policy; bind `ApplicationReleaseFingerprint` to the
   exact `ReleaseFingerprint` read from `mr-lister-phase6-${EnvironmentName}` while retaining the
   guard archive's own independent `GuardReleaseFingerprint`.
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

**P7.15A inert-topology checkpoint was completed historically.** A separate
`infra/phase7/production-disabled-template.json` defined six role-separated functions, one bounded
Standard workflow, recovery and dead-letter queues, retention and dispatcher seams, payload-free
workflow logging, an encrypted alarm topic, and complete alarm categories. At that checkpoint all
40 resources shared an impossible condition and production entrypoints were absent. The workflow
invokes only one future worker, uses fixed 1-second action and 20-second verification waits, stops
after at most 91 one-step invocations, keeps a failed worker Task as the exact redrive origin while
execution data logging stays disabled, and has an absolute 1,860-second timeout. P7.15C
deliberately supersedes the template's instantiation model; P7.15A remains historical evidence
rather than a description of the current file.

**P7.15B provider-free control-plane checkpoint was completed at
`0e3c150cb9cfa11c8047db3a4670f8ec5aa6d864`.** The bounded due-work inventory and dispatcher use
one fixed Standard state machine, deterministic execution names, identifier-only input, exact
readback after ambiguous starts, a final per-item deadline check, and per-candidate retry results
so one unavailable execution cannot starve later work. Expired pristine work is sent through the
existing encrypted recovery queue and settles through the real replay-safe execution service
without starting or inspecting a workflow. Failed executions can only be described and redriven
on the same exact ARN; the retry horizon carries non-redrivable early failures through the fixed
30-minute settlement boundary. Terminal retention strongly resolves owner authority before the
existing marker-last TTL transaction. At that checkpoint the handlers and dependency graphs were
source-only, with no production entrypoint, runtime artifact, or activation path. P7.15C now
supersedes that packaging state while retaining its provider-free and disabled guarantees.

**P7.15C source and production-disabled deployment closure is complete at
`7c933dd2cfd76e418d57ce1e25d9f6ffe3c69d3f`.** The release contains six release-first refusal
entrypoints, a deterministic 74-module Python 3.12 ARM64 closure, the reviewed 14-wheel dependency
authority, exact contract/profile/topology/workflow binding, an activation-ready but inert
48-resource source SAM template and 49-resource processed topology, EventBridge target retry/DLQ
policy, customer-managed alarm encryption, restricted queue redrive, durable active-work recovery
indexing, a bounded recovery sweep, exact one-message DLQ triage, read-only preflight, and an
operations runbook. All functions retain zero reserved concurrency, all mappings/rules are
disabled, no API or Function URL exists, the worker role has no provider-secret authority, and
every packaged handler verifies the release before refusing without observing its event.

The exact versioned archive was uploaded, the processed 49-add change set was reviewed, and stack
`mr-lister-phase7-dev` reached `CREATE_COMPLETE`. Exact artifact, Lambda, IAM, mapping, schedule,
queue, workflow, log, alarm, and negative-capability readback passed. Two temporally separated idle
samples from `2026-09-02T02:06:27Z` through `02:16:59Z` found no Lambda invocation datapoints,
workflow executions, queued work, or indexed work, while Phase 6 remained unchanged.

**P7.15C deployment and rollback closure passed on 2026-09-04 for the functional demo scope.** The
existing production-disabled predecessor was captured, the exact provider-free operations release
`4bab97bc3d35e55ad872b6049b332d9e7710d08e840798f4402f54e3acc2da00` was deployed and read back,
and an independently reviewed rollback change set restored the predecessor. The canonical
preflight, deployment, and rollback proofs all report `passed`; the rollback proof binds the
predecessor and restored processed template, stack authority, and both affected Lambda
configurations. The live stack is therefore left on the production-disabled predecessor rather
than on the temporary operations runtime.

Same-ARN redrive timing, EventBridge exhaustion through the DLQ and alarm receiver,
recovery-index-loss/saturation fault injection, and retention/backfill-duration drills remain
unclaimed post-demo hardening. They are not represented as successful acceptance evidence and do
not require changes to the sealed runtime for the hackathon demo. A saturated 25-row recovery page
remains an explicit alarm/operator stop; provably fair automatic traversal beyond that page is not
claimed.

### P7.16 — Deployed read-only validation

After the two offline matrices pass, reconcile the sealed Phase 6 component-bound deployment
authority, invoke the zero-call guard, and run the separately approved GET-only Printify/Etsy
preflight. Publication must be structurally impossible. Record immutable evidence before changing
the contract activation phase. The deployed guard must retain its own immutable
`GuardReleaseFingerprint`, while its independent `ApplicationReleaseFingerprint` must match the
exact deployed Phase 6 stack `ReleaseFingerprint` used by the stored publication snapshots.

**Functional checkpoint complete on 2026-09-05.** The exact fresh-job validation passed
`staged_shop_preflight`, `staged_product_preflight`, `recorded_preflight`, and
`read_only_preflight_complete`. It was bound to Phase 6 application release
`0c6211a5b0244e9c86d635e6c02e7bc49e5e948d68895b4aaa982c0b0b2e187b`, canary release
`b3c8c00d0fd5021dc25f13c6320498f173b67d7962bfe55d7024944e9677850c`, and canary binding
`c72624f7226c6bddbcc616d5c4ef93a10bfbe8fba17cf44e93319c3457209bf3`. The deployment readback
verifier passed; no publication POST was authorized during this checkpoint.

### P7.17 — One-listing MassSkutiny canary

First close two operator-tooling gaps: mint a `publish_once` binding only from a completed durable
read-only preflight proof, and add deterministic canary deployment/readback plus terminal-evidence
verification. Then stop for explicit approval of one exact MassSkutiny job and listing.

The authorized canary has reserved concurrency one, one aggregate, one product, one mutation
permit, at most one publication POST, exact-product polling, and no retry POST. Success requires a
positively verified Etsy identity, safe result link, immutable report, and in-app notification.
Infrastructure rollback cannot unpublish the external Etsy listing, so this step is intentionally
irreversible.

**Functional live-publication milestone completed on 2026-09-05.** The authenticated seller run
used job `job_126b45d46bb560e8641a6e43f2a925d6`, Printify product
`6a9c3253a93f54bb45068670`, and the connected Etsy shop **Apartment H Collective**. The owner
explicitly approved that exact product. The publish-once runtime used release
`92ee88dc46ff8499b2f98ae27e216eab31b86da648b24549b0f1dbb6938b20e9` and binding
`e03471ca44f89fba19e57f77f22901e3f6b25f25e04b9fe00b9be47a0cd9255c`. It issued exactly one
publication POST; subsequent observations were GET-only. Provider readback and seller
confirmation showed the product visible with no provider error and Etsy listing
`4569583958` at
`https://www.etsy.com/listing/4569583958/polygonal-llama-t-shirt-geometric-animal`.

The immutable verification deadline was `2026-09-05T15:56:23.127389Z`; the positive external
identity was first observed at approximately `15:58:25Z`. Consequently, the durable aggregate
remained `publication_verifying` and the strict P7.17 terminal verifier is **not satisfied** for
this run. This does not erase the successful real-world vertical slice, but it prevents claiming
formal terminal-evidence acceptance. The checkpoint is the optimization baseline; it is not a
Phase 7 seal or a general-availability deployment at that checkpoint; the subsequent P7.18
activation and seal are recorded below.

### P7.18 — General availability and seal

Only after a successful canary:

1. freeze a new reviewed contract that explicitly permits general availability;
2. register the owner-scoped routes and enable the already-tested seller control;
3. deploy the production worker/poller/recovery/notification topology;
4. exercise positive, stale-approval, repeat, cross-owner, race, failure, restart, accessibility,
   and authenticated seller acceptance;
5. capture source, artifact, deployment, CI, canary, rollback, and operational evidence; and
6. declare either `PHASE 7 COMPLETE AND SEALED` or the smallest exact blocker list.

**PHASE 7 COMPLETE AND SEALED — FUNCTIONAL HACKATHON-DEMO SCOPE.** Contract `7.1.0` and enabled
release `b167db6f3dc5b8fef73c89959e0eff5ffdaee50b0739845232718456684cb130` are deployed on
`mr-lister-phase7-dev`. Exact archive, Lambda, IAM, trigger, workflow, JWT route, and Phase 6
non-delta readback passed. The versioned web release, public activation, and authenticated
owner-scoped status read also passed without another publication POST. Release candidate
`022bdb62b6d7e4e8ac3c129e943f48e4256a6c5c` passed `main` CI run `33985664447`; no functional
demo blocker remains. See
[`phase7-release-state.md`](phase7-release-state.md).

## Production-disabled predecessor record

On 2026-09-02, the authenticated `mr-lister-dev` profile deployed and read back the following
production-disabled release in account `384627057108`, region `us-west-2`:

| Deployment authority | Value |
| --- | --- |
| Source commit | `7c933dd2cfd76e418d57ce1e25d9f6ffe3c69d3f` |
| Release manifest | `9c4deca1813e5d1e8cc3f6747681b2194265f9c0b51b64fd9cf6b8afeb823c46` |
| Archive SHA-256 | `43721a48802bd3bbc946671aff938b6df030b495975c8bc59839db18986da88f` |
| Archive size | `62,982,212` bytes |
| Versioned archive | `phase7/candidates/9c4deca1813e5d1e8cc3f6747681b2194265f9c0b51b64fd9cf6b8afeb823c46/production-disabled.zip`, version `6ix.miylQqgEZyV392IenODAlQvbAp4F` |
| Deployment manifest | `068a9956609c70ab01059e0a7c08b8499dbf1edc67dedcb4032bd0a6dd3459ab` |
| Source manifest | `58fb8526f8767549347a1a90452cd144867a593fbd3a7e78218ebe84b6ebc4f9` |
| Dependency manifest | `4945e5c68931676783932eb33b933f40e107765296fcfdd9f2ea6363ef6ce04f` |
| Topology binding | `f26d28b96664415facbb153c74364b9b0e4b2478af1a20431918b96e498de3b8` |
| Deployment descriptor | `fdfc0797b3fc7b3b750108a76ae38f854c65cf0dfd6ac9abdb653b2456ea2708` |
| Disabled template | `2a98ab2a7cf3fb04590f9f8cd3a30cc6c2e373421e70c70220be419b80ca7df2` |
| Publication workflow | `9a6112c85b35e775d1e60681a0ca14e6740cd0aea82b2ac33b5aa74b86fc3098` |
| Packaged template | `phase7/sam/templates/2a6f45a790e554e3680e23c4d35abf4d8a2a99611a20e301c66d2a61a284b9db.yaml`, version `fvTXvRtq9r.JtdyorhIzV.PZGLei9w4D` |
| Stack | `mr-lister-phase7-dev`, `CREATE_COMPLETE` |
| Change set | `mr-lister-phase7-dev-production-disabled-create-9c4deca1813e` |
| CI | GitHub Actions run `33580123287`, green |

The processed change set contained 49 additions and no modifications or removals. Readback proved
all six functions use the exact archive and reserved concurrency zero; all mappings and rules are
disabled; no Function URL, API, provider secret, or provider permission exists; queues and live
work partitions are empty; the state machine has no execution; and all seven log groups contain
zero stored bytes. Two idle samples from `2026-09-02T02:06:27Z` through `02:16:59Z` also found no
Lambda invocation datapoints. Stack outputs report `PRODUCTION_DISABLED`, seller publication
false, provider mutation false, and worker triggering false. The Phase 6 stack ID, status, update
timestamp, and `WEB_EDGE_ACTIVE_DRAFT_ONLY` readiness remained unchanged.

An earlier release `267b291ad3649ab047ec00eb3367e9e248b0e1196d99450de1dfc6d621119b2c`
failed during create because the workflow log destination appended a second `:*` to the log-group
ARN. The stack rolled back; all other created resources were removed, while its retained KMS key
is pending scheduled deletion. The corrected direct-ARN behavior is regression-tested in the
deployed source. That failed candidate is historical evidence, not a live release or rollback
point. The tested September 4 operations deployment and rollback described above closes the
production-disabled predecessor rollback tuple for the functional demo scope.

The intentional recovery-index and strong aggregate-rebind changes also resealed the historical
P7.9 triggerless worker source checkpoint. Its manifest changed from
`ca44431e5cfe3b0222560bc8bd8f6d7aa58468257760eae2768de5234a3412e8` to
`7dd3d5b7c2e2cf691aa9a2d7234cd211d7fb5d8419710724a7bbb5a4788d361c`; its archive changed from
`9f8a6916b4bf6cc3c7fe384de1d5b5d6a7a3b8244e5d2701b8cdf1e5c47cfa2e` to
`747cb0719a9242cb85531f2152746b60fefcca9e79e145cd415f9e03ab064c96`.
This is a reviewed consequence of the active-work recovery attributes and strong rebind, not a
suppressed drift check.

The later, narrowly scoped Printify readback normalization fix deliberately resealed that same
triggerless closure. The manifest/archive advanced from
`7dd3d5b7c2e2cf691aa9a2d7234cd211d7fb5d8419710724a7bbb5a4788d361c` /
`747cb0719a9242cb85531f2152746b60fefcca9e79e145cd415f9e03ab064c96` to
`eb506fee3a0deb9d2cc9077af51094f4754fe8e83ca54e737762152566e7746f` /
`a8b54e21e287d3a1fb4236b37612f56e0edfa5e8df736f898fe96904b6ebb5ad` after two independent
builds and artifact verification. The source change only normalizes provider ordering and inert,
disabled catalog expansion against the Phase 6 authority; it adds no publication capability.

The Etsy-safe SKU boundary then deliberately resealed the same triggerless closure from
`eb506fee3a0deb9d2cc9077af51094f4754fe8e83ca54e737762152566e7746f` /
`a8b54e21e287d3a1fb4236b37612f56e0edfa5e8df736f898fe96904b6ebb5ad` to
`6ef0ad14098598337a739608e5a55f70ca8622077d30098c7b736655f5fa0789` /
`50ef471e9f20e90ccfbe08e2393c35bace29da0b4d8707a3c83e8cbc682aca4e` after two
deterministic builds and explicit artifact verification. It recognizes the exact 20-character
provider SKU representation without changing stored Phase 6 correlation or publication
capabilities.

## Stop lines

- A guard deployment is not publication authorization.
- A GET-only preflight is not publication authorization.
- Creating the canary publication aggregate is durable application authority and requires an
  exact reviewed target.
- Invoking `publish_once` is the first provider mutation and requires explicit operator approval.
- Contract `7.0.1` cannot authorize a seller-facing publication route or general availability.
- No Phase 7 deployment may add publication, order, fulfillment, deletion, or unpublish authority
  to the sealed Phase 6 runtime.
