# Gemma 4 image-detail experiment

## Predeclared scope

The user requested a detail-handling comparison and supplied five new JPEGs. Keep the v3
inspection, writer and verification prompts frozen. Change only the PNG byte allowance used
for the whole-image inspection rendition, in a separate evaluator-only subclass. No production
code, model/sampling setting, schema, review gate, source upload ceiling or publication flow
changes. This experiment does not add a provider-specific `detail` parameter.

| Arm | Maximum PNG bytes | Maximum side | Whole request ceiling |
| --- | --- | --- | --- |
| Standard | 750,000 | 1600 pixels | 3,500,000 serialized bytes |
| Detail | 2,300,000 | 1600 pixels | 3,500,000 serialized bytes |

Both arms use the existing PNG preparation algorithm and transparency behavior. Neither crops,
upscales, alters original artwork, nor supplies expected image identities or reviewer notes to
inference. Both inspection and writing receive the same arm-specific rendition. Base64, prompts,
schemas and any repair history must still fit the original full-request ceiling; a larger image
budget does not authorize oversized repairs. The source JPEG/PNG limit remains 5 MiB.

## Cases and run order, declared before inference

Run two trials per arm on all five new images, plus the earlier marching-figure photo as one
explicitly known regression control. The five new images have no prior model outputs. Independent
assistant observations are recorded before inference and kept out of model context. The sixth
case was already reviewed and must not count toward fresh-image generalization.

Alternate arm order across cases, reversing it for the second trial of each case. Keep all 24
preparations, normally 48 and at most 120 model invocation attempts, including errors and review
stops. Do not change prompts, select the best answer or rerun a failed semantic result.

| Case | Standard rendition | Detail rendition | Role |
| --- | --- | --- | --- |
| New photo 1 | 1024 × 1024; 391,921 B | Identical | Same-input variation control |
| New photo 2 | 853 × 1280; 715,576 B | Identical | Same-input variation control |
| New photo 3 | 714 × 714; 631,262 B | 1024 × 1024; 1,249,041 B | Higher retained resolution |
| New photo 4 | 700 × 1051; 689,136 B | 853 × 1280; 900,478 B | Higher retained resolution |
| New photo 5 | 975 × 649; 724,693 B | 1280 × 853; 1,046,286 B | Higher retained resolution |
| Known marching figures | 579 × 869; 664,958 B | 853 × 1280; 1,317,535 B | Known higher-resolution diagnostic |

The seahorse fixture was measured offline: it is already 1024 × 1024 and only 14,164 PNG bytes
under either budget. No new seahorse calls are planned; this byte-budget change cannot add image
detail there. Changing its answer with identical pixels would not establish a resolution benefit.

## Measurements and interpretation

Record source and delivered-rendition hashes, dimensions and bytes, frozen prompt/schema/settings
identities, actual serialized request sizes, token usage, outcomes and measured call time. The
evaluator's detail preparation first reuses the base source-binding check/rendition, then prepares
the larger rendition; total wall time includes that extra local work. Measure delegate call time
separately so asymmetric preprocessing is not described as model latency. Neither measure is the
full upload-to-listing application latency, and missing provider latency remains unknown.

Assess central subject, exact lettering, defining details, depicted actions and relationships,
unsupported factual claims, relevant thematic search intent and justified review stops. Use the
originals for both arms. Unknown precise species or historical identities need not block an
accurate descriptive listing, but added pixels do not justify guesses. Thematic associations
are permissible without turning them into provenance or affiliation claims.

Compare both trials, including variation between identical-input controls. With two repeats and
only three new images whose resolution changes, any improvement is preliminary evidence about
these examples, not a general accuracy guarantee. Retain the prior v3 failures in the record.

Use temporary model/region-limited developer inference access and verify its removal afterward.
No live store, product, publishing, deployment, billing, commit or merge operation is in scope.

Frozen v3 full prompt fingerprint:
`0fda08954b93aa25c8b3b8ff158cbba124bc8d37cb30e311a229c328be64e7b9`.
Unmodified harness source SHA-256:
`09e2c8669f92b91c0b3812456e44b763604e9711b3ec17781d048445898ffe09`.
Detail evaluator SHA-256 before inference:
`2f4831ea9373af6bf2408bca2b3668c9d30471078819421282accf3718a988ed`.

## Results

Completed on October 4, 2026 (Pacific), run `image-detail-20261004-a`: all 24 preparations
returned schema-valid drafts in 48 invocation attempts, with no repairs or transport errors.
This is structural acceptance, not 24 accurate listings. Original/rendition bindings, frozen
settings and prompt identities, and actual serialized request limits were checked for every run.

| Observation | Standard | Detail |
| --- | --- | --- |
| Preparations / invocation attempts | 12 / 24 | 12 / 24 |
| Reported input / output tokens | 31,641 / 5,274 | 31,631 / 5,218 |
| Median summed delegate time per preparation | 6.736 s | 7.370 s |
| Summed delegate time range per preparation | 4.636–11.894 s | 5.976–17.295 s |
| Largest serialized request | 973,286 B | 1,763,830 B |

Delegate times sum the inspection and writing roundtrips, excluding local image preparation.
They are not provider-only latency or end-to-end app timing. The batch is too small for a
performance benchmark. Reported inspection input tokens were identical within each case across
all four runs despite the changed image sizes. This does not establish how the provider handles
image resolution internally, and reported usage is not a dollar-cost estimate.

| Image | Observed result across both trials |
| --- | --- |
| Lobster | Both arms preserve subject, orange facets, claws and antennae. Identical input pixels; tie. |
| Bird in tracksuit | Both preserve Afro, orange glasses and red/yellow tracksuit. Identical input pixels; tie. |
| Meditating llama | All four misread `NO PROBLLAMA` as `NO PROBLLLAMA`. Standard trial 1 invents four arms; detail avoids that error but still fails the central lettering. Neither is ready without correction. |
| Bat and cactus | Both preserve the principal subjects. Detail mentions red fruit in both trials; standard mentions it in one. Detail trial 2 adds incorrect `saguaro art` for the paddle cactus. Standard preferred overall. |
| Historical scene | Standard preserves a Lincoln-like figure, uniformed man and flag. Detail trial 1 invents a tiered cake from ambiguous pale stacked objects. Detail trial 2 avoids it but is more generic. Standard preferred overall. |
| Known marching figures | All omit the cookie-like/gingerbread appearance and icing-like trim. Both detail trials say the figures march toward the distant rock, reversing the pictured direction. Standard trial 2 also adds stone/clay and ancient-army associations. Neither is consistently dependable. |

Independent assistant reviewers assessed A/B outputs against originals and locked their judgments
before opening the arm mapping. The fresh cases produced two ties, two standard preferences and
one neither (detail less wrong on anatomy). The known control also received neither. This was
not a human seller review. Masking was imperfect: original-image paths contained profile names;
the fresh-case reviewer reported ignoring them. Future packs should use neutral source paths.
Neither reviewer found unsupported garment specifications in these outputs. Legitimate thematic
tags remain allowed; the cactus and stone-material errors are not grounds for banning themes.

## Decision and verification

Keep the larger image allowance experimental. It fits safely for this batch but does not reliably
improve recognition, fix exact lettering, or prevent unsupported details. Do not promote v3 or
change production from these results. Five fresh images (only three with changed resolution) and
two repeats cannot establish broad accuracy. Earlier v3 failures remain part of the record.

The next bounded experiment should isolate a focused text/detail view alongside the whole image,
with the same request ceiling and unchanged original artwork, and test whether it catches errors
the writer currently accepts. A separate text check is worth evaluating for exact lettering.
Those treatments were not implemented or run here; they need fresh cases as well as retained
regressions rather than being tuned to these five answers.

226 targeted tests passed across the detail evaluator, candidate harness, artwork evaluation,
Mantle adapter and experiment gates. Ruff passed. Tests include unchanged standard pixels, source
binding, no upscaling, consistent inspection/writer images, frozen v3, offline/live gating, safe
telemetry, and rejection of oversized requests including repair history before network access.
Temporary model/region-scoped inference access was detached and deleted after the batch, with
absence verified. No deployment, commerce mutation, commit or merge occurred.

Private artifacts (ignored by Git) are under
`.mr_lister_private/detail-experiments/image-detail-20261004-a/`: `review.html` is the self-contained
visual report; `review-pack.json`, per-arm folders, `wire-metrics.json` files, `plan.json`, masked
review packs and locked reviewer judgments preserve all outputs and measurements.
