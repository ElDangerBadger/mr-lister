# ADR 0013: Make channel publication one-shot and verification-bound

- Status: Accepted
- Date: 2026-08-23

Contract `7.0.1` clarifies this decision before implementation and supersedes the pre-implementation
`7.0.0` artifact without enabling publication. Git history preserves the original artifact bytes.

## Context

Phase 6 ends at an exact, version-bound human approval and deliberately has no publication
capability. Phase 7 must turn that approval into one Etsy listing through the seller's connected
Printify shop. A publication request is externally mutating and asynchronously completed. A
timeout can occur after Printify accepted the request, so retrying the POST cannot be treated like
an ordinary transport retry.

Printify exposes one product publication endpoint and reports channel identity on the shop plus
lock and external-reference fields on the product. Its similarly named `publishing_succeeded`,
`publishing_failed`, and `unpublish` endpoints are custom-channel status operations, not the
connected-Etsy publication path Mr Lister is authorizing.

## Decision

Phase 7 uses a separate application-owned `PublicationAggregate`. The Phase 6 control job remains
in terminal `ControlJobState.APPROVED`; `APPROVED` is only the conditioned bridge source for
creating the aggregate in `PUBLICATION_REQUESTED`. Phase 7 does not add states or work types to the
Phase 6 control graph or dispatcher.

The publication boundary operates as follows:

1. The seller explicitly confirms publication of the exact current approval authority. The API
   atomically persists an immutable publication snapshot, one attempt, one `AVAILABLE` mutation
   permit, one `PublicationWorkRequest`, one event, and one `PublicationCommandReceipt` before any
   provider call. The approved job must retain the exact approval decision ID, and synchronized
   product evidence must retain the owner-bound Printify shop ID; neither can come from the browser.
2. A dedicated publication worker re-reads the exact owner, approval, product synchronization,
   pricing, shop, and snapshot authority. It performs all fallible local validation and read-only
   Printify preflight before consuming the permit.
3. `AVAILABLE -> CONSUMED` is the only permit transition that authorizes one exact
   `POST /v1/shops/{shop_id}/products/{product_id}/publish.json`. The POST body and its fingerprint
   are fixed by the snapshot. The application never retries that POST. `AVAILABLE -> RETIRED` is
   allowed only when terminal settlement proves that no POST was claimed; consumed and retired are
   permanent.
4. A definite accepted response enters verification. A complete, closed-classifier synchronous
   rejection may fail terminally. A timeout, disconnect, malformed or retryable response, crash
   after consumption, or otherwise ambiguous response enters read-only reconciliation. Both paths
   poll the exact product with GET; neither can mint another aggregate, attempt, permit, work, or
   mutation.
5. Publication succeeds only after a positive, application-validated product observation proves
   the exact product is unlocked, visible, content-consistent, and carries one valid Etsy external
   identifier. The safe seller link is derived from that identifier, not copied from an arbitrary
   provider URL.
6. Exact-product GET is positive-proof-only: it can prove `PUBLISHED`, never
   `PUBLICATION_FAILED`. Once the permit is consumed, expiry without positive proof is always
   `PUBLICATION_OUTCOME_UNKNOWN`. A definitive preflight failure retires the available permit and
   may settle `PUBLICATION_FAILED` with zero POSTs.
7. Seller notification and the immutable run report are created only after the verified result is
   durably committed. The initial notification channel is authenticated in-application status and
   contains no email or other new PII.
8. GET and POST counts are durable budgets owned by the root attempt: three shop GETs, 100 total
   exact-product GETs, and one publish POST. Every wire request counts; transport retry, Lambda
   restart, state-machine restart, and redrive cannot reset them. The root deadline is created once
   as `requested_at + 1800 seconds` and cannot be extended.
9. Terminal settlement creates the only `terminal_at`, derives 30-day source-release eligibility
   and 90-day operational expiry, and preserves an immutable job-level aggregate tombstone until
   that job expires so TTL cannot restore publication authority.

Phase 7 publication is a new capability surface. The Phase 6 draft-only client and roles remain
unchanged and unable to reach publication, order, fulfillment, deletion, unpublish, webhook, or
custom-channel status routes. Legacy fake Phase 1 publication code is never a Phase 7 dependency
or deployment input.

## Consequences

- A transport failure may reduce liveness, but cannot authorize a duplicate listing.
- Stale approval, product, pricing, shop, or release authority fails before mutation.
- A seller cannot cancel an accepted publication command after its atomic commit; the confirmation
  explains that the external action may already be in progress.
- Step Functions coordinates attempts and polls but never owns publication authority.
- The provider token's coarse `products.write` scope is narrowed by a dedicated route allowlist,
  role, one-shot permit, and audited call ledger.
- Phase 6 activation is independent and remains draft-only. A separately authorized, one-listing
  canary boundary may exercise one permit only after offline and read-only gates pass; it exposes no
  seller route and disables itself when the permit settles.
- A private read-only approval guard may be sealed and directly invoked before seller activation.
  It must re-read the exact durable approval/snapshot graph, authorize zero provider calls, expose
  no route or trigger, and remain a mandatory coordinator prerequisite before credentials or call
  claims. Deploying that attestor alone does not advance the publication activation phase.
- Contract `7.0.1` keeps seller-facing publication disabled. Enabling it requires a later reviewed
  contract and release after the Phase 7 domain, provider, API, infrastructure, and live acceptance
  gates all pass.
