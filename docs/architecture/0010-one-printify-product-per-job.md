# ADR 0010: Preserve one unpublished Printify product across revisions

- Status: Accepted
- Date: 2026-08-21

## Context

Phase 5 creates one unpublished Printify product with a guarded POST. The current listing-revision
path returns through product preparation and claims a new create operation keyed by review version.
With the live adapter, every valid revision can therefore create another product. The fake adapter
hides this because it returns one stored product ID per job.

Printify supports retrieving and updating an existing product. Its product response also contains
mockup images, variant retail prices, and fulfillment costs that the Phase 6 review needs. See the
[official Printify API reference](https://developers.printify.com/API-Doc-RREdits.html/1000).

## Decision

One Mr Lister job owns at most one Printify product:

1. The first valid review with no persisted product ID may POST one unpublished product and must
   persist its ID before advancing. The first valid review can be a human revision after invalid
   generated content.
2. Every valid review with an existing product ID retrieves and PUTs a complete canonical payload
   to that exact product ID.
3. Update failure never falls back to product creation.
4. The initial canonical payload carries a deterministic non-secret correlation token in logical
   variant SKUs. The Printify create boundary maps each one to an exact 20-character alias derived
   from the token and variant ID for Etsy compatibility. An ambiguous POST is never blindly
   retried; list/read reconciliation must find exactly one matching logical or aliased SKU and
   canonical payload or leave the write unresolved only through a persisted deadline. Zero matches
   at that deadline produce an explicit terminal unknown outcome (or a cancelled result flagged as
   provider-outcome-unconfirmed when cancellation intent exists). The job is never allowed a
   second POST.
5. An ambiguous PUT enters reconciliation; a GET and canonical comparison determine whether the
   update completed before any retry. Only the same product at the exact prior canonical payload
   may retry the same idempotent PUT once. The retry inherits the root attempt's persisted deadline;
   a second ambiguity, expired deadline, missing product, or conflict fails terminally.
6. Provider synchronization is recorded separately from immutable review content and binds the
   product ID, review version, payload fingerprint, result fingerprint, mockups, costs, provider
   status, and timestamp.
7. Cancellation retains the unpublished product unless a future explicit destructive command is
   separately authorized and tested.

## Consequences

- Multiple edits preserve one product ID and refresh its mockups rather than creating clutter.
- A network-ambiguous first request may safely produce zero products; the invariant is at most one
  product per job, while exactly one is required before confirmed success.
- Approval requires the current valid review version to match the synchronized product version.
- A locked, published, wrong-shop, wrong-blueprint, or wrong-provider product fails closed.
- Fake adapters and tests distinguish product POST from PUT so they cannot conceal duplication.
- Mockups and cost data become seller projection inputs, not business-state authority.
- A cancelled job may leave an unpublished recoverable product in Printify, and the UI says so.
