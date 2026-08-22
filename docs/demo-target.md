# Locked demo target

Phase 0 freezes one narrow product path so later integrations can be calibrated and tested
against a stable target.

## Product path

- Seller-owned, visually nonempty, square transparent PNG artwork
- Gildan 64000 Unisex Softstyle T-Shirt
- Front print only
- SwiftPOD print provider
- Five verified colors and six verified sizes (30 variants)
- One Etsy shop connected through Printify
- One consolidated human review
- Explicit approval before publication
- Publication verification and immutable run report

## Calibration baseline

- Retail price: `2999` cents for every variant
- Buyer-facing free shipping, with seller-funded fulfillment shipping modeled separately
- Horizontal placement: `x = 0.5`
- Vertical placement: `y = 0.25`
- Scale: `0.65`
- Angle: `0`
- Prices represented only as integer cents

The placement values are the live-accepted square-artwork baseline for Gildan 64000 blueprint
`145` and SwiftPOD provider `39`, not a universal product rule. The configured selectors and all
30 variant IDs must still be resolved against the authorized Printify account before a write.
They must never be invented or copied from stale reference data. Non-square placement remains
unsupported until its center position is calculated deterministically from the validated artwork
and print-canvas dimensions.

## Phase 0 boundary

No Phase 0 code connects to AWS, Bedrock, Printify, or Etsy. Synthetic identifiers used by
fixtures are clearly non-live and cannot authorize publication.
