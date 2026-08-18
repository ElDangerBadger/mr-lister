# Locked demo target

Phase 0 freezes one narrow product path so later integrations can be calibrated and tested
against a stable target.

## Product path

- Seller-owned transparent PNG artwork
- Gildan 5000 unisex heavy cotton T-shirt
- Front print only
- One verified Printify print provider
- One controlled set of variants
- One Etsy shop connected through Printify
- One consolidated human review
- Explicit approval before publication
- Publication verification and immutable run report

## Calibration baseline

- Approximate design width: 8.0-8.5 inches
- Horizontal placement: `x = 0.5`
- Vertical placement: `y = 0.3183`
- Scale: `0.65`
- Prices represented only as integer cents

The placement values are an initial evidence-backed baseline, not a universal product rule.
The live `blueprint_id`, `print_provider_id`, and `variant_ids` must be discovered from the
authorized Printify account and verified with an unpublished canary. They must never be
invented or copied from stale reference data.

## Phase 0 boundary

No Phase 0 code connects to AWS, Bedrock, Printify, or Etsy. Synthetic identifiers used by
fixtures are clearly non-live and cannot authorize publication.
