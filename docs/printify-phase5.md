# Phase 5 Printify draft boundary

Phase 5 creates one real, unpublished Printify product. It does not publish a marketplace listing,
submit an order, or send anything to production.

## Artwork formats

Mr Lister preserves and uploads validated `PNG` and `SVG` source artwork. SVG is never converted
into the production file sent to Printify. Before pass-through, SVG input is parsed and rejected if
it contains DTDs, entities, scripts, `foreignObject`, event handlers, imported styles, or external
resources. The exact accepted bytes remain the authoritative print artifact.

Bedrock does not accept SVG as an image block. A future SVG intake slice must therefore create a
separate bounded PNG inspection rendition for visual inference while retaining the untouched SVG
for Printify. That derivative is diagnostic input, not the production asset.

Printify recommends URL upload for files over 5 MB. The writer therefore supports bounded base64
upload and credential-free HTTPS URL upload as separate methods.

## Verified initial seller profile

The first profile was selected from the live Printify catalog on 2026-08-21:

- Gildan 64000 Unisex Softstyle T-Shirt, blueprint `145`;
- SwiftPOD, print provider `39`;
- colors `Black`, `Charcoal`, `Dark Chocolate`, `Navy`, and `Sand`;
- sizes `S`, `M`, `L`, `XL`, `2XL`, and `3XL`;
- exactly 30 current color/size combinations;
- retail price `2999` integer cents for every variant;
- buyer-facing free-shipping intent, modeled separately from Printify fulfillment cost;
- front DTG placement at `x=0.5`, `y=0.25`, `scale=0.65`, and `angle=0`.

The provider exposes three proportional front-print canvases:

| Group | Sizes | Canvas |
| --- | --- | --- |
| small | S | 3021 x 3927 |
| medium | M | 3356 x 4364 |
| large | L, XL, 2XL, 3XL | 3692 x 4800 |

These values are seller defaults, not platform-wide policy. Future sellers can define their own
profiles without granting a model authority over blueprint, provider, variants, placement, price,
shipping, or publication.

Profile v2 changed `y` from `0.3183` to `0.25` after the first live mockup showed a 6.83% top gap.
For square artwork at this scale and these proportional canvases, `0.25` top-aligns the rendered
design. A later refinement must calculate center `y` deterministically from validated dimensions
for non-square sources; the model must not choose placement geometry.

## Read-only preflight

`PrintifyCatalogClient` performs four authenticated GET requests before any write is allowed:

1. confirm the configured shop exists;
2. confirm the blueprint exists;
3. confirm the provider still offers the blueprint;
4. resolve every requested color/size pair and verify its print canvas.

The preflight fails closed if any of the 30 combinations disappears, becomes duplicated, loses its
front DTG placeholder, or changes print dimensions. Its resolved result is the deterministic input
to the later artwork-upload and product-create commands.

After loading `PRINTIFY_API_TOKEN` and `PRINTIFY_SHOP_ID` into the current shell, run the live
read-only preflight with:

```zsh
MR_LISTER_RUN_LIVE_PRINTIFY=1 \
  .venv/bin/python tools/printify_catalog_preflight.py
```

The explicit gate prevents an accidental network call. The tool has no write method and prints no
credential. It reports the resolved variant IDs because those identifiers are not secrets.

The runtime token will come from the existing Secrets Manager boundary. The local token file is a
developer convenience only and is neither read by application code nor committed to Git.

The canonical seller profile now lives with the workflow-owned profiles at
`config/product_profiles/gildan_64000_swiftpod.json`. The adapter resolves its live selectors at
write time; provider response data never becomes state-transition authority.

Artwork upload and unpublished product creation are separate durable external-write claims. A
completed upload is persisted before product creation is attempted, and a completed product ID is
persisted before the workflow advances to `PRINTIFY_DRAFT_CREATED`. Ambiguous outcomes stop for
reconciliation instead of repeating a potentially successful provider write.

## Deferred follow-up

- Extend application intake and private artifact storage to preserve validated SVG alongside PNG,
  with a separately rendered Bedrock inspection artifact.

## First deployed canary

The first double-gated canary completed on 2026-08-21. The deployed workflow retrieved the exact
seller secret using only its prepare role, uploaded the frozen owl fixture, and created unpublished
product `6a88bb49f2c2450fa1065afd`. The application persisted separate upload and product-create write
records, then stopped at `AWAITING_APPROVAL`. It made no publication, order, or fulfillment call.

After visual review found the v1 artwork 6.83% below the desired top line, profile v2 changed the
square-artwork baseline from `y=0.3183` to `y=0.25`. A second double-gated canary created unpublished
product `6a88bd96cf106ff5b30727c5`, again stopped at `AWAITING_APPROVAL`, and the seller visually
accepted its top alignment as spot on. Printify reused the identical uploaded image ID while
creating a distinct product draft, consistent with provider-side content deduplication.

After review, both Step Functions executions were explicitly stopped and verified `ABORTED` so no
approval callback remained active. Their DynamoDB evidence and unpublished Printify drafts were
retained. The short-lived bootstrap login used only for cancellation was then logged out.

`orders.write` is intentionally absent. Phase 5 must be structurally unable to purchase or fulfill
a physical product.
