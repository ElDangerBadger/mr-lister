# Locked demo target

Phase 0 freezes one narrow product path so later integrations can be calibrated and tested
against a stable target.

## Product path

- One or multiple seller-owned, visually nonempty PNG/SVG artworks (with JPG/JPEG normalized
  through the same ingestion path), transparent or opaque, in their native aspect ratios
- Gildan 64000 Unisex Softstyle T-Shirt
- Front print only
- SwiftPOD print provider
- Five verified colors and six verified sizes (30 variants)
- One Etsy shop connected through Printify
- One bounded Strands Agents SDK preparation loop on AgentCore: controller model, job-scoped
  tools, reasoning, and strict structured response
- One consolidated human review
- Explicit approval before publication
- Publication verification and immutable run report

Phase 6 stops after the review and approval state. Publication and publication verification are
Phase 7 demo steps and remain disabled during Phase 6 acceptance.

## Calibration baseline

- Retail price: `2999` cents for every variant
- Buyer-facing free shipping, with seller-funded fulfillment shipping modeled separately
- Horizontal placement: `x = 0.5`
- Vertical placement: `y = 0.25`
- Scale: `0.65`
- Angle: `0`
- Prices represented only as integer cents

The placement values are the live-accepted square-artwork baseline for Gildan 64000 blueprint
`145` and SwiftPOD provider `39`, not a universal product rule. Artwork width starts at the
calibrated `0.65` scale; its height stays proportional to the verified source dimensions, and the
vertical center is derived deterministically from that aspect ratio and the verified print canvas.
Only when a tall artwork's proportional height would exceed the canvas is width reduced to fit.
The configured selectors and all 30 variant IDs must still be resolved against the authorized
Printify account before a write.
They must never be invented or copied from stale reference data.

## Phase 0 boundary

No Phase 0 code connects to AWS, Bedrock, Printify, or Etsy. Synthetic identifiers used by
fixtures are clearly non-live and cannot authorize publication.

## Submission proof

The recorded demo must identify Strands in the first 30 seconds, show this same seller job enter
the AgentCore Strands preparation loop, and display a sanitized response or audit containing the
fixed framework and agent identity, cycles, selected tools, and structured next action. It must
then show the resulting staged listing at the application-owned human gate. Model reasoning is
not publication authority; the seller's explicit approval and deterministic lifecycle remain
visibly separate.
