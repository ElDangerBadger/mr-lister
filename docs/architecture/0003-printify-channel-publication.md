# ADR 0003: Publish Etsy listings through Printify

- Status: Accepted
- Date: 2026-08-18

## Context

Printify owns the product, variants, fulfillment configuration, and connection to the initial
Etsy shop. Creating an independent Etsy product model would duplicate state and increase the
risk of mismatched or duplicate listings.

## Decision

Mr Lister creates and publishes the initial Etsy listing through Printify's connected channel.
Direct Etsy integrations are reserved for verification or a future requirement that Printify
cannot support.

## Consequences

- Printify identifiers and payload fingerprints are persisted before retrying writes.
- Publication is idempotent and guarded by current approval.
- Etsy-specific automation does not enter the initial critical path.
- A future marketplace adapter must preserve the same application contracts and safety gates.
