# Phase 7 guarded publication contract

Phase 7 extends an approved Mr Lister job through exactly one verified Etsy publication using the
seller's connected Printify channel. It does not change how artwork is uploaded, how Strands
orchestrates the pinned Gemma intelligence worker, how one unpublished Printify product is reused,
or how the seller approves the exact consolidated review.

This contract is frozen before implementation. The current application must continue to expose no
publication route, worker, role, or provider method until the activation gates at the end of this
document pass.

The deterministic machine-readable authority is
[`contracts/publication/phase7.0.json`](../contracts/publication/phase7.0.json), generated from the
capability-free models in
[`src/mr_lister/publication/contract.py`](../src/mr_lister/publication/contract.py). Its checked
`publication_enabled` value is `false`.

## Supported seller flow

```text
APPROVED
  -> seller confirms the exact approved authority
  -> PUBLICATION_REQUESTED
  -> one exact Printify publish POST is claimed
     -> PUBLICATION_VERIFYING       (provider definitely accepted the request)
     -> PUBLICATION_RECONCILING     (provider outcome may be unknown)
  -> read-only exact-product polling
     -> PUBLISHED                   (positive Etsy external proof)
     -> PUBLICATION_FAILED          (definitive failure, no second POST)
     -> PUBLICATION_OUTCOME_UNKNOWN (deadline without definitive proof, no second POST)
```

`PUBLISHED`, `PUBLICATION_FAILED`, and `PUBLICATION_OUTCOME_UNKNOWN` are terminal for the first
Phase 7 surface. Once `PUBLICATION_REQUESTED` is committed, cancellation and revision are
unavailable because the external mutation may already be in flight. A future withdrawal or
unpublish feature requires its own contract and human authority; it is not inferred here.

## Immutable publication snapshot

The publish command binds all seller-relevant and provider-relevant authority into one immutable
snapshot and fingerprint:

- exact owner and job IDs inside the private operational snapshot, with only their digests copied
  into reports;
- current job record version and exact `APPROVED` state;
- approval decision ID and approval fingerprint;
- review version and review fingerprint;
- product-sync ID and fingerprint, exact Printify shop/product/image IDs, and canonical payload
  fingerprint;
- pricing snapshot ID, pricing fingerprint, complete-evidence fingerprint, and freshness deadline;
- pinned profile ID, version, and fingerprint;
- expected connected sales channel `etsy`;
- exact publication body fingerprint;
- release-manifest fingerprint; and
- request, verification-deadline, and retention timestamps.

The snapshot is created only when the review is current and valid, pricing is complete and fresh,
the synchronized product is the exact current unpublished and unlocked draft, provider uncertainty
is false, and the exact approval fingerprint recomputes from all current authority. Publication
never relies on a browser-supplied product, shop, route, state, recovery action, or provider body.

## Seller command and transaction

The future command is `POST /v1/jobs/{job_id}/publish`. It requires authenticated owner scope, one
strong current `If-Match`, one idempotency key, exact expected job/review/approval versions and
fingerprints, and the literal confirmation `publish_exact_approved_listing`.

One DynamoDB transaction must condition the current owned `APPROVED` record and atomically write:

- the immutable publication snapshot;
- one immutable publication attempt;
- one `AVAILABLE` one-shot publication permit;
- one pending publication work request;
- the updated job and owner-job projection;
- one domain event; and
- one full-payload idempotency receipt.

The request transaction makes no Printify, Etsy, AgentCore, model, S3, or Step Functions call.
Exact replay returns the persisted receipt; changed-body reuse conflicts; concurrent commands have
one winner. The dispatcher remains the only Step Functions starter.

## Dedicated provider boundary

The publication worker uses a separate owner-bound secret resolution and transport. The allowed
network surface is closed to:

| Method | Exact route | Purpose |
| --- | --- | --- |
| `GET` | `/v1/shops.json` | Prove the configured shop is still connected to Etsy |
| `GET` | `/v1/shops/{shop_id}/products/{product_id}.json` | Preflight and verification polling |
| `POST` | `/v1/shops/{shop_id}/products/{product_id}/publish.json` | The single authorized mutation |

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

The endpoint and product fields are based on the official
[Printify API reference](https://developers.printify.com/). A deployment must pass a read-only shop
and exact-product canary before publication is enabled because provider behavior remains external
and versionable.

## One-shot mutation and reconciliation

Only `AVAILABLE -> CONSUMED` authorizes the publication POST. The permit is persisted before the
call, bound to the snapshot/attempt/work, and can be consumed once. All local reconstruction and
read-only preflight precede consumption.

- A definitive pre-call dependency failure leaves the same permit available and may retry the same
  work authority.
- A definite accepted response records the response fingerprint and enters
  `PUBLICATION_VERIFYING`.
- Any failure after consumption that cannot prove the provider rejected the request records
  provider-outcome uncertainty and enters `PUBLICATION_RECONCILING`.
- Verification and reconciliation perform GET only. They cannot create another attempt or permit.
- The root attempt owns one fixed 30-minute verification deadline. Redrive, replay, seller retry,
  Lambda restart, and Step Functions restart cannot extend it.
- Deadline expiry with a provider-declared definitive failure produces `PUBLICATION_FAILED`.
  Missing, conflicting, or incomplete evidence produces `PUBLICATION_OUTCOME_UNKNOWN`.

No state after permit consumption can reach another publication POST. Operator investigation may
record a later observation, but cannot silently republish.

## Positive verification and result link

Success requires one strong GET observation of the exact product proving all of the following:

- the product still belongs to the expected owner-bound shop and has the snapshotted product ID;
- it is unlocked and visible;
- its canonical title, description, tags, enabled variants, retail prices, images, blueprint,
  provider, and print placement still match the publication snapshot;
- it exposes exactly one Etsy external reference with a bounded numeric listing identifier; and
- no conflicting external reference is present.

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

## Immutable report and retention

The run report binds only closed statuses, timestamps, aggregate call counts, release/snapshot/
attempt/permit/observation/result fingerprints, and sanitized audit-record digests. It contains no
token, raw owner identity, provider body/response, storage key, presigned URL, listing text,
artwork, email, or free-form error. A report cannot claim success without the exact durable result
and notification record.

Published, failed, and unknown publication operational records receive a 90-day terminal TTL.
Private source-version release becomes eligible 30 days after the publication terminal state; the
reference-aware sweeper still rechecks exact durable authority before changing tags. Mr Lister
never deletes or unpublishes the Printify/Etsy product as part of retention.

## Acceptance and activation gates

Publication remains disabled until all of these pass:

1. Phase 6 composed handlers, immutable AgentCore release binding, Linux ARM64 artifacts,
   operational cleanup, stuck-work recovery, alarms, and deployed non-destructive acceptance;
2. exhaustive offline publication command/store tests for stale, invalid, unapproved, replayed,
   changed-idempotency, and concurrent requests;
3. provider tests proving all local work precedes permit consumption, exactly one POST, no blind
   retry, GET-only reconciliation, fixed deadline, and zero forbidden methods/routes;
4. projection/API/browser tests for irreversible confirmation, conflict recovery, process restart,
   unknown outcome, safe links, and notification-after-verification only;
5. infrastructure tests for a separate publication role/function/machine, no order/fulfillment/
   unpublish/custom-status authority, bounded polling, DLQ/redrive, alarms, and payload-free logs;
6. a deployed read-only Etsy-shop/product preflight with zero provider mutations; and
7. one separately authorized live canary that publishes exactly one listing, verifies one external
   identity, records one POST and zero duplicate/forbidden mutations, and produces an immutable
   sanitized report.

The checked Phase 6 stack must remain `SCAFFOLD_ONLY`, and the Phase 6 browser must continue to
expose no publish control, until these gates are complete.
