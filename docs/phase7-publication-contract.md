# Phase 7 guarded publication contract

Phase 7 extends an approved Mr Lister job through exactly one verified Etsy publication using the
seller's connected Printify channel. It does not change how artwork is uploaded, how Strands
orchestrates the pinned Gemma intelligence worker, how one unpublished Printify product is reused,
or how the seller approves the exact consolidated review.

This contract is frozen before implementation. Contract `7.0.1` is a pre-implementation
clarification that supersedes `7.0.0`; Git history preserves the original `7.0.0` bytes. It closes
aggregate ownership, permit settlement, provider-observation, call-budget, retention, and
activation ambiguities without enabling publication.

The active Phase 6 production artifacts and seller browser continue to expose no reachable
publication route, worker, role, provider method, or control. The repository's explicitly fake
Phase 1 publication demonstration is not production authority, is never reusable for Phase 7, and
must remain excluded from every Phase 6 and Phase 7 deployment bundle.

The deterministic machine-readable authority is
[`contracts/publication/phase7.0.1.json`](../contracts/publication/phase7.0.1.json), generated from
the capability-free models in
[`src/mr_lister/publication/contract.py`](../src/mr_lister/publication/contract.py). Its checked
`publication_enabled` value remains `false`.

## Separate publication aggregate

Phase 7 does not reopen or extend the Phase 6 control graph. `ControlJobState.APPROVED` remains the
terminal Phase 6 state, retains the immutable approval authority, and never becomes a Phase 6
publication work type. `APPROVED` in the publication transition contract is a derived bridge
source: the request transaction conditions the exact approved control job, then creates one
separate `PublicationAggregate` in `PUBLICATION_REQUESTED`. It is not a persisted pre-request
publication state.

The `PublicationAggregate` exclusively owns publication state. Its `PublicationWorkRequest` and
future publication dispatcher are separate from the Phase 6 `WorkType`, dispatcher, recovery map,
and state machines. The control job and owner-job projection must retain an immutable aggregate
reference and terminal summary for projection, cleanup, and duplicate prevention, but their Phase 6
state remains `APPROVED`.

## Supported seller flow

```text
Phase 6 ControlJobState.APPROVED (derived bridge source; remains APPROVED)
  -> seller confirms the exact approved authority
  -> PublicationAggregate.PUBLICATION_REQUESTED
     -> PUBLICATION_FAILED          (definitive preflight failure; zero publish POSTs)
     -> one exact Printify publish POST is claimed
        -> PUBLICATION_FAILED          (complete, definitive synchronous rejection)
        -> PUBLICATION_VERIFYING       (provider definitely accepted the request)
           -> read-only exact-product polling
              -> PUBLISHED                   (positive Etsy external proof)
              -> PUBLICATION_OUTCOME_UNKNOWN (no positive proof by the fixed deadline)
        -> PUBLICATION_RECONCILING     (provider outcome may be unknown)
           -> read-only exact-product polling
              -> PUBLISHED                   (positive Etsy external proof)
              -> PUBLICATION_OUTCOME_UNKNOWN (no positive proof by the fixed deadline)
```

`PUBLISHED`, `PUBLICATION_FAILED`, and `PUBLICATION_OUTCOME_UNKNOWN` are terminal publication
aggregate states for the first Phase 7 surface. None can create another aggregate, attempt, permit,
work request, or publish POST for the job. Once `PUBLICATION_REQUESTED` is committed, cancellation
and revision are unavailable because the external mutation may already be in flight. A future
withdrawal or unpublish feature requires its own contract and human authority; it is not inferred
here.

## Immutable publication snapshot

The publish command binds all seller-relevant and provider-relevant authority into one immutable
snapshot and fingerprint:

- exact owner and job IDs inside the private operational snapshot, with only their digests copied
  into reports;
- current control-job record version and exact `APPROVED` state;
- immutable approval decision ID and approval fingerprint;
- review version and review fingerprint;
- product-sync ID and fingerprint, exact Printify shop/product/image IDs, and canonical payload
  fingerprint;
- pricing snapshot ID, pricing fingerprint, complete-evidence fingerprint, and freshness deadline;
- pinned profile ID, version, and fingerprint;
- expected connected sales channel `etsy`;
- exact publication body fingerprint;
- release-manifest fingerprint; and
- request timestamp and the root attempt's exact verification deadline.

The approved control job must already retain its `approval_decision_id`, and the immutable product
synchronization evidence must already retain the owner-bound `printify_shop_id`. Existing records
missing either authority fail closed for publication. The browser cannot supply or override them.

The snapshot is created only when the review is current and valid, pricing is complete and fresh,
the synchronized product is the exact current unpublished and unlocked draft, provider uncertainty
is false, and the exact approval fingerprint recomputes from all current authority. Publication
never relies on a browser-supplied product, shop, route, state, recovery action, deadline, work or
permit identity, or provider body. Terminal and retention timestamps are not request-time snapshot
fields because they do not exist until a terminal settlement wins.

## Seller command and transaction

The future command is `POST /v1/jobs/{job_id}/publish`. It requires authenticated owner scope, one
strong current `If-Match`, one idempotency key, the exact expected control-job record version,
review version and fingerprint, approval decision ID and approval fingerprint, and the literal
confirmation `publish_exact_approved_listing`.

One DynamoDB transaction must condition the current owned `APPROVED` record and atomically write:

- the immutable publication snapshot;
- one immutable publication attempt;
- one `AVAILABLE` one-shot publication permit;
- one pending publication work request;
- the control job's immutable publication-aggregate reference and matching owner-job projection,
  without changing its Phase 6 `APPROVED` state;
- one domain event; and
- one full-payload idempotency receipt.

The request transaction makes no Printify, Etsy, AgentCore, model, S3, or Step Functions call.
Exact replay returns the persisted `PublicationCommandReceipt`; changed-body reuse conflicts;
concurrent commands have one winner. A future dedicated Phase 7 dispatcher remains the only
publication Step Functions starter. The Phase 6 dispatcher cannot recognize or start publication
work.

## Dedicated provider boundary and root-attempt budgets

The publication worker uses a separate owner-bound secret resolution and transport. The allowed
network surface is closed to:

| Method | Exact route | Root-attempt budget | Purpose |
| --- | --- | ---: | --- |
| `GET` | `/v1/shops.json` | `3` total | Prove the configured shop is still connected to Etsy |
| `GET` | `/v1/shops/{shop_id}/products/{product_id}.json` | `100` total | Preflight and verification polling |
| `POST` | `/v1/shops/{shop_id}/products/{product_id}/publish.json` | `1` total | The single authorized mutation |

The exact POST body sets `title`, `description`, `images`, `variants`, `tags`, `keyFeatures`, and
`shipping_template` to `true`; extra or caller-selected fields are forbidden. The worker validates
the configured shop, exact product, canonical content, variants, unlocked/unpublished state, and
all local payload material before atomically consuming the permit.

The publication role and client deny every other route, including product create/update/delete,
image upload/archive, `publishing_succeeded`, `publishing_failed`, `unpublish`, orders,
fulfillment, webhooks, and arbitrary collection reads. The custom-channel status endpoints are not
used to declare success for a connected Etsy shop. The provider audit sink records only a closed
method/route template and allowed-or-rejected category; it has no dynamic IDs, query values,
headers, bodies, tokens, owner identity, or provider response.

Every budget is durable and scoped to the root `PublicationAttempt`, not to a Lambda invocation,
Step Functions execution, redrive, or HTTP-client retry. Each actual wire request consumes one
count; the HTTP transport performs no hidden retry. Restart and redrive re-read the same counters.
The shops budget permits bounded safe retry before permit consumption; the exact-product budget
includes its preflight GET and every verification or reconciliation poll. No execution may trade an
unused GET count for another POST. The root deadline and every counter are created once and can
only decrease remaining authority.

The endpoint and product fields are based on the official
[Printify API reference](https://developers.printify.com/). A deployment must pass a read-only shop
and exact-product canary before publication is enabled because provider behavior remains external
and versionable.

## One-shot mutation and reconciliation

The permit has exactly two legal transitions:

- `AVAILABLE -> CONSUMED` uses `atomic_pre_call_publish_authorization`: an atomic, durable claim
  immediately before the only publish POST that authorizes exactly one POST. A consumed permit
  remains consumed forever.
- `AVAILABLE -> RETIRED` uses `definitive_pre_call_terminal_settlement`: an atomic terminal
  settlement authorizing zero POSTs and proving that no publish POST was claimed. It is allowed
  only for a definitive preflight authority failure or expiry of the root deadline while the permit
  is still available. A retired permit can never be consumed.

The permit is persisted before provider work and is bound to the snapshot, aggregate, attempt, and
publication work. All fallible local reconstruction and read-only preflight precede consumption.
The aggregate/permit invariants are closed: verification, reconciliation, published, unknown, and
any post-call failed result require the one consumed permit; a preflight-failed result requires the
one retired permit. No terminal state may coexist with an available permit.

- A transient dependency failure that is definitively before consumption leaves the same permit
  available and may retry only the same root work authority within its remaining GET budgets and
  deadline.
- A definitive preflight authority failure atomically retires the available permit and commits
  `PUBLICATION_FAILED`, proving zero publish POSTs.
- A definite accepted response records a sanitized response fingerprint and enters
  `PUBLICATION_VERIFYING`.
- A complete synchronous response may enter `PUBLICATION_FAILED` only when the closed provider
  response classifier proves rejection. Timeout, disconnect, malformed response, retryable status,
  or any classification doubt is never definitive rejection.
- Any other condition after consumption records provider-outcome uncertainty and enters
  `PUBLICATION_RECONCILING`.
- Verification and reconciliation perform exact-product GET only. They cannot create another
  aggregate, attempt, permit, work request, or POST.
- The root attempt owns one fixed 30-minute deadline equal to `requested_at + 1800 seconds`.
  Redrive, replay, seller retry, Lambda restart, Step Functions restart, clock skew, and deployment
  replacement cannot extend or recompute it.
- After consumption, only positive proof can settle `PUBLISHED`. Without that proof, deadline
  expiry settles `PUBLICATION_OUTCOME_UNKNOWN` regardless of unlocked, missing, conflicting, or
  incomplete product data.

No state after permit consumption can reach another publication POST. Operator investigation may
record a later observation, but cannot silently republish.

## Positive verification and result link

Success requires one complete application-validated GET observation of the exact product proving
all of the following:

- the product still belongs to the expected owner-bound shop and has the snapshotted product ID;
- it is unlocked and visible;
- its canonical title, description, tags, enabled variants, retail prices, images, blueprint,
  provider, and print placement still match the publication snapshot;
- it exposes exactly one Etsy external reference with a bounded numeric listing identifier; and
- no conflicting external reference is present.

An exact-product GET produces only `positive_publication_proof`, `publication_not_yet_proven`, or
`conflicting_or_incomplete_evidence`. Only the first can prove success. An absent external
reference, unlocked product, invisible product, missing product, changed content, provider error,
or conflicting response is not proof that the publish POST was rejected or failed; after permit
consumption it can lead only to more bounded GET polling or terminal
`PUBLICATION_OUTCOME_UNKNOWN`. The forbidden custom-channel `publishing_failed` POST is never used
as evidence.

The application derives `https://www.etsy.com/listing/{numeric_id}` from the validated identifier.
It never forwards an arbitrary external handle as a response header or link. The immutable result
stores the numeric external identifier, canonical-link fingerprint, verified product fingerprint,
observation fingerprint, and verification time. Raw provider responses are neither stored nor
returned.

## Seller projection and notification

The server projects publication state, named stage, disabled reason, attempt status, verification
deadline, safe result link, and immutable report reference. The browser never infers publication
authority from a Phase 6 state. It retains the exact strong ETag/idempotency/reload/race rules and
shows an irreversible-action confirmation before issuing the command.

An authenticated in-application notification record is created in the same transaction that
commits `PUBLISHED`. It is projected only after positive verification and is idempotent for the
publication result. Email, SMS, webhooks, and new seller contact data are outside this phase.

## Offline Phase 7.3 provider-evidence provenance

The offline Phase 7.3 checkpoint closes the trust gap between a provider response and the
provider-free execution service without activating publication. The sealed three-route boundary
first consumes a fresh one-use call grant, persists its sanitized allowed-route audit record, makes
at most one wire request, classifies the bounded response, and stages an immutable evidence record.
The stage binds the exact aggregate, durable call claim, provider authority, evidence kind,
sanitized evidence fingerprint, audit binding, and observation time. It contains no credential,
raw request or response, header, owner identity, listing text, or arbitrary provider URL.

Execution commands accept only a stage ID and fingerprint. The store loads the exact stage and
atomically writes its immutable consumption record with the resulting aggregate transition; a
caller-computed evidence DTO is not execution authority, a consumed stage cannot be reused, and a
replayed claim never mints another wire grant. The outer coordinator accepts only owner and
aggregate identity, derives every internal operation from current durable authority, recovers a
staged response before making another call, and performs at most one provider request per
invocation.

Structured negative evidence may retire an available permit before the fixed deadline only for
the closed provider-derived shop/channel, product-missing, locked, already-published, canonical,
variant, placement, or mockup mismatch set. Authentication, throttling, server, transport, and
malformed-response failures cannot prove definitive rejection. Without an exact trusted negative
stage, pre-call authority remains requested until its original deadline.

This checkpoint also provides an injected offline DynamoDB transaction adapter and an owner-first
read projection/query adapter. Neither is composed into Lambda, API Gateway, IAM, Step Functions,
the browser, the Phase 6 dispatcher, or a source bundle. Both publication and request enablement
remain false, and no live provider call is authorized by this checkpoint.

## Offline Phase 7.4 eligibility and disabled read composition

Phase 6 must build an unpublished draft, so its exact checked product profile remains
`publish_enabled=false`. Phase 7 no longer treats that draft-safety field as publication
eligibility. A separate immutable eligibility record binds the exact profile ID, version,
fingerprint, Etsy channel, and sealed release fingerprint. It states that the profile is eligible
while fixing seller-request and provider-mutation enablement to false. The existing publication
snapshot already persists every constituent of that decision, so no replaceable policy identifier
or unbound flag is introduced.

The request service verifies this eligibility before freezing intent. The execution service
rechecks it against the durable snapshot and current release before reconstructing provider
authority or consuming the permit. A capability-free pre-call guard additionally re-reads the
approved job, approval decision, review, synchronized product and owner-bound shop, pricing,
profile, release, and eligibility joins. Any stale, missing, foreign, or changed value fails before
an outer provider boundary could be reached.

The Phase 7.4 infrastructure is a separate SAM scaffold with one reserved-concurrency publication-
status query Lambda. It has no event source or addressable route. Its role has only log writes and
same-table `dynamodb:GetItem`/`dynamodb:Query` for `JOB#*` and `PUBLICATION#*`; it has no DynamoDB
write, scan, secret, object-store, workflow, invocation, provider, or network-management authority.
The checked package contains only a thin shim, and the scaffold, query, request, and publication
markers are fixed to disabled values. A sealed application bundle, exact runtime environment,
authenticated route registration, deployment, and read validation remain later gates.

This checkpoint does not compose a request service, coordinator, provider boundary, credential
resolver, dispatcher, state machine, browser control, or notification delivery surface. It makes
no AWS or Printify call and does not advance the contract beyond `offline_implementation`.

## Immutable report and retention

The run report binds only closed statuses, timestamps, aggregate call counts, release/snapshot/
attempt/permit/observation/result fingerprints, and sanitized audit-record digests. It contains no
token, raw owner identity, provider body/response, storage key, presigned URL, listing text,
artwork, email, or free-form error. A report cannot claim success without the exact durable result
and notification record.

The transaction that first commits a terminal `PublicationAggregate` state owns the one immutable
`terminal_at` timestamp. It derives, in that same settlement, exactly
`source_release_eligible_at = terminal_at + 30 days` and
`operational_expires_at = terminal_at + 90 days`. Request time, retries, later observations,
operator activity, and DynamoDB's eventual deletion time cannot move those dates.

The 90-day expiry covers the publication aggregate, snapshot, attempt, permit, publication work,
observations, terminal result, publication command receipts, notification, report reference, and
the matching job/owner operational projections. The control-job row retains the immutable
aggregate reference and terminal publication summary until it expires, so asynchronous TTL removal
of a child row can never make the still-addressable `APPROVED` job eligible for a second aggregate
or permit. A request requires both the owned control job and absence of any prior publication
aggregate authority; after the job row expires it cannot be addressed or recreated through the
publication command. The frozen invariant is
`job_aggregate_tombstone_until_operational_expiry`.

Private source-version release becomes eligible at the derived 30-day timestamp; the
reference-aware sweeper still rechecks the exact terminal aggregate and durable source authority
before changing tags. TTL assignment and source release never delete or unpublish the
Printify/Etsy product.

## Acceptance and three activation scopes

Activation has three separate scopes. Passing one never implies the next:

1. **Phase 6 activation.** The Phase 6 release may replace `SCAFFOLD_ONLY` only after its own Linux
   ARM64, deployment, non-destructive, unpublished-provider, and moderated acceptance closes. That
   stack remains draft-only and has no publication route, worker, role, client method, state
   machine, or browser control.
2. **Isolated Phase 7 canary authority.** After the offline publication and read-only deployment
   gates pass, a separately deployed canary-only boundary may receive one explicit run approval,
   one preselected job/aggregate, one one-shot permit, and a role incapable of serving the seller
   route. It can perform at most one publish POST and disables itself when that permit settles. Its
   existence does not change `publication_enabled=false` for the application or expose a browser
   control.
3. **Seller-facing Phase 7 activation.** A later reviewed contract and release may change the
   seller application from disabled to enabled only after the canary evidence and all gates below
   close. Contract `7.0.1` cannot authorize this change.

The machine-readable contract expresses progress through four ordered activation phases nested
inside those three external scopes:

1. `offline_implementation` is the current phase. It permits no provider mutation and no seller
   publication route. It requires:

   - `phase71_authority_prerequisites`: approved jobs retain `approval_decision_id`, synchronized
     product evidence retains owner-bound `printify_shop_id`, and missing legacy authority fails
     closed;
   - `publication_domain_store_service_matrix`: exhaustive stale, invalid, unapproved, replayed,
     changed-idempotency, concurrent-request, rollback, and foreign-owner tests;
   - `publication_provider_one_shot_matrix`: all local work precedes permit consumption, budgets are
     per root attempt, one POST is possible, no POST retry exists, GET is positive-proof-only, and
     every forbidden method/route fails closed;
   - `publication_api_browser_matrix`: irreversible confirmation, conflict recovery, restart,
     unknown outcome, safe-link, and notification-after-positive-verification tests; and
   - `publication_infrastructure_and_alarm_matrix`: separate publication roles/functions/machines,
     bounded polling, DLQ/recovery/alarms, payload-free logs, and no order, fulfillment, unpublish,
     custom-status, or legacy-fake authority.
2. `deployed_read_only_validation` still permits no provider mutation and no seller publication
   route. It requires `phase6_deployed_non_destructive_acceptance`,
   `immutable_release_and_agentcore_binding`, `linux_arm64_artifact_inspection`, and
   `read_only_etsy_preflight`. The Phase 6 release may already be active here, but stays draft-only.
3. `explicit_one_listing_canary` allows only the separately authorized bounded canary mutation. It
   requires `explicit_one_listing_live_canary`: exactly one listing, one publish POST, one verified
   external identity, zero duplicate or forbidden mutations, and one immutable sanitized report.
4. `general_availability` is not authorized by this contract. It requires a later reviewed release
   and `explicit_general_availability_enablement` before a seller publication route or general
   mutation authority may exist.

Phase 6 activation is independent of Phase 7 activation and does not wait for the Phase 7 canary.
The Phase 6 browser always exposes no publish control. The general-availability Phase 7 route, IAM
role, state machine, and browser control remain absent or fail closed until a later enabled contract
is reviewed after every frozen prerequisite phase and gate passes.
