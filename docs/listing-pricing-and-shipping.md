# Editable listing prices and shipping

Status: deployed from `d8111804` on `codex/listing-pricing-shipping`, September 13, 2026.
See [verified release state and live-check limits](listing-pricing-release-state.md).

Sellers can change the USD item price, apply that price to every variant, override individual
color/size prices, reset an override, and choose free shipping in the existing review editor.
The configured product, provider, colors, sizes, and placements remain the production profile.
Settings belong to one review/listing; they do not change defaults for other listings.

## Save and publication behavior

- Money is validated as integer cents, from 1 to 999,999 cents. Empty, zero, negative,
  fractional-cent, and nonnumeric prices cannot be saved.
- Optional `listing.pricing` uses the existing owner-, version-, ETag-, and idempotency-bound
  listing revision command. Omission preserves saved choices. Unknown/duplicate variants fail
  validation; selectors cannot alter the fixed product catalog.
- Pricing or shipping changes create a new review revision, invalidate prior estimates and
  approval, synchronize the same Printify draft, and recalculate proceeds. Unsaved/saving/conflicted
  edits prevent approval and publication. A mismatched saved readback preserves local edits.
- Canonical provider payloads include reviewed variant prices and
  `sales_channel_properties.free_shipping`. Readback, reconciliation, publication preflight,
  and the publication guard bind these choices. Publication already sends `shipping_template: true`.
- With free shipping off, estimated buyer shipping equals the provider's first-item standard US
  rate. Fees include that estimated shipping revenue. This is an estimate assumption; actual Etsy
  checkout shipping is not observed or guaranteed. Free shipping on deducts provider delivery cost
  from the seller's proceeds.

Printify documents these fields in its [product API](https://developers.printify.com/#products).

## Compatibility and release scope

New prepared reviews explicitly record profile-derived defaults and send the free-shipping choice
to Printify. Historical records without pricing retain their original fingerprints, payloads,
and recovery behavior. Their projected shipping choice is labeled as a preset until the seller
explicitly saves pricing and synchronizes. `product_policy.pricing_saved` distinguishes this case;
it does not assert that a still-running synchronization has completed.

This is a backend and web change. Release the seller API/control functions, product/economics
workers, AgentCore preparation runtime, publication request/execution/guard components, and web
bundle from the same reviewed source. The new `control/pricing.py` is included in the guard's
explicit source allowlist. The frontend can read older responses without pricing controls.

Browser checks exercise the compiled application with local fixture responses. They cover
editing, cent precision, authoritative saves, theme variants, and mobile layout. Backend tests
exercise provider payloads/readback and publication safeguards with simulated transports.
The deployed handlers passed read-only health, authentication, ownership and pricing-projection
checks, with the existing job partition unchanged. A fresh signed-in price save and provider
round trip remain the live follow-up. No real Printify/Etsy product was changed or published
during deployment.

## Local verification — September 13, 2026

- Web lint, typecheck, 375 tests, and production build passed.
- The full backend run passed 4,289 tests and skipped 11 opt-in live AWS tests. Its three failures
  were the expected source-closure counts and deterministic package hashes changed by the new
  shared module. Those expectations now explicitly include `control.pricing`; all 18 tests in
  the affected release suites passed on rerun. No behavioral test remained failing.
- Browser contract regeneration check, Ruff, and whitespace checks passed.
- Fresh-build Chromium, Firefox, and WebKit matrix passed. Local evidence is
  `output/playwright/phase66/20260913T163559Z/browser-gate.json`; that run also contains light,
  dark, and mobile pricing screenshots for each engine.
