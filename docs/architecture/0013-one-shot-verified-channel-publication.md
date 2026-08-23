# ADR 0013: Make channel publication one-shot and verification-bound

- Status: Accepted
- Date: 2026-08-23

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

Phase 7 uses a separate application-owned publication boundary:

1. The seller explicitly confirms publication of the exact current approval authority. The API
   atomically persists an immutable publication snapshot, one attempt, one `AVAILABLE` mutation
   permit, one work request, one event, and one idempotency receipt before any provider call.
2. A dedicated publication worker re-reads the exact owner, approval, product synchronization,
   pricing, shop, and snapshot authority. It performs all fallible local validation and read-only
   Printify preflight before consuming the permit.
3. One consumed permit authorizes at most one exact
   `POST /v1/shops/{shop_id}/products/{product_id}/publish.json`. The POST body and its fingerprint
   are fixed by the snapshot. The application never blindly retries that POST.
4. A definite accepted response enters verification. A timeout, disconnect, or otherwise
   ambiguous response enters read-only reconciliation. Both paths poll the exact product with GET;
   neither can mint another mutation permit.
5. Publication succeeds only after a positive, application-validated product observation proves
   the exact product is unlocked, visible, content-consistent, and carries one valid Etsy external
   identifier. The safe seller link is derived from that identifier, not copied from an arbitrary
   provider URL.
6. Expiry without positive proof is terminal unknown or terminal failed according to the persisted
   observations. It never returns to a state that can issue another publication POST.
7. Seller notification and the immutable run report are created only after the verified result is
   durably committed. The initial notification channel is authenticated in-application status and
   contains no email or other new PII.

Phase 7 publication is a new capability surface. The Phase 6 draft-only client and roles remain
unchanged and unable to reach publication, order, fulfillment, deletion, unpublish, webhook, or
custom-channel status routes.

## Consequences

- A transport failure may reduce liveness, but cannot authorize a duplicate listing.
- Stale approval, product, pricing, shop, or release authority fails before mutation.
- A seller cannot cancel an accepted publication command after its atomic commit; the confirmation
  explains that the external action may already be in progress.
- Step Functions coordinates attempts and polls but never owns publication authority.
- The provider token's coarse `products.write` scope is narrowed by a dedicated route allowlist,
  role, one-shot permit, and audited call ledger.
- Publication remains disabled until the Phase 7 domain, provider, API, infrastructure, and live
  acceptance gates all pass.
