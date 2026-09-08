# Bounded Etsy SEO/tag checkpoint — 2026-09-08

## Decision and status

Candidate v1 is the frozen SEO copy-quality reference. The deterministic whole-phrase policy is
the local refinement baseline after green focused regressions. **The unavailable historical
eleven-fixture A/B is a known evidence limitation, not an MVP blocker.** The user explicitly
reaffirmed that nonessential evidence should not hold the working MVP or trigger another prompt
cycle. Do not claim that comparison passed or that this is a deployed release. Do not start
latency Stage 1 from this checkpoint without review.

- Copy reference: `2026-09-08.1-etsy-seo-candidate`, unchanged fingerprint
  `d72948fe5a7ea155f6fa5283428ffcd26011e1e871342086ecf89e27398c56c2`.
- Production/default rollback prompt: `2026-08-18.7`, unchanged fingerprint
  `c5b2a76ebcc9fff8bd5363beb2db2d1651ad554fab21340a6a4cdb1a166ac96f`.
- Source rollback point: `2d2ea61` on `refinement/etsy-seo-prompt-evaluation`.
- Tag policy: `2026-09-08.phrase-coverage-1`.
- No v3, inference runs, model changes, deployment, provider/UI/publication changes, category or
  attribute automation, or one-call optimization was performed.

## Implementation

Gemma still interprets artwork and produces creative copy plus ranked natural candidate phrases.
The existing two-operation intelligence path is unchanged. The immutable v1 prompt is retained
verbatim, including its historical stricter tag guidance; application validation now owns the
corrected policy. Neither prompt bundle's bytes were edited and the runtime default was not switched.

The selector returns exactly 13 complete original phrases of at most 20 characters. Stable
include-first selection honors model rank, skips generic-only/orphan candidates and redundant
phrases, and backtracks when needed for a complete set. Case/whitespace equivalents, ordinary
plural variants, and a small explicit set of generic paraphrases conflict. Shared words alone do
not conflict. No phrase is shortened, split, recombined, or invented.

An insufficient candidate pool uses the existing listing repair path at most once, even when the
older global model configuration allows two repairs. A tag-only repair cannot overwrite title,
description, audience, or either rationale: code retains the original fields. Another insufficient
pool fails before production writes. Artwork inspection's repair configuration is unchanged.

The shared validator and review error locations use actual redundant pairs (`TAG_REDUNDANCY`).
Root reuse remains diagnostic, not a rejection criterion. Current evaluation uses redundancy
counts; older scores without the new metric are explicitly unassessed, not recertified.

## Evidence and examples

All eleven existing v1 accepted analyses and listings are preserved byte-faithfully as structured
values in `tests/fixtures/etsy_seo_v1_reference.json`, with source/artwork hashes and pinned prose
digests. Its SHA-256 is `035e451c5ef528dc51b8be220e8d94724de6e4f3870d1748245ec46f077f8e01`.
Tests read that committed fixture, not ignored `.mr_lister_private` files.

The historical runs retained final tags only. All 24 corresponding v1 response diagnostics omit
raw model output; the original 18–30 ranked phrases are unavailable. Therefore **A: v1 + old
selector versus B: identical v1 output + new selector has not been executed across the eleven
artworks.** Final tags cannot be reverse-engineered into the missing candidate pools. No broader
useful search coverage or live SEO improvement is claimed for those artworks.

These are executed focused regression examples using identical small, explicitly synthetic pools,
the old selector from `2d2ea61`, and the new selector—not replacements for the missing v1 replay:

| Pool / requested count | Old selection | New selection |
| --- | --- | --- |
| `diamond ring`, `engagement ring` / 2 | `diamond ring`, `engagement` | `diamond ring`, `engagement ring` |
| `octopus art`, `octopus print`, `animal wall decor` / 2 | `octopus art`, `animal wall decor` | Unchanged: redundant alternative still skipped |
| `owl shirt`, `owl graphic tee`, `minimalist owl`, `night owl shirt`, `forest bird` / 4 | `owl shirt`, `graphic tee`, `minimalist`, `forest bird` | `owl shirt`, `minimalist owl`, `night owl shirt`, `forest bird` |

The owl case preserves specific phrases and adds the night-owl concept without outputting generic
remainders. Unit tests also exercise exactly 13 outputs, stable ordering, plural duplicates,
unusable pools, overlength skipping, one repair maximum, unchanged prose, and review boundaries.

## Verification

- Focused selector/adapter/workflow/review/reference/evaluation/Phase 7.9 suite: **202 passed**.
- Phase 6 and production-disabled Phase 7 packaging suites: **16 passed**.
- Total focused verification: **218 passed**; no AWS or model calls.
- Repository Ruff lint and formatting validation: passed; `git diff --check`: passed.
- An earlier full offline run produced 4,083 passes and four failures. The failures identified
  the new helper's missing Phase 6 bundle entry and expected source-closure/seal changes. All
  four were corrected and reverified in the focused suites above. The full suite was not rerun
  after those packaging/oracle corrections; do not report a final full-suite or CI result.

The shared validator belongs to the triggerless Phase 7.9 source artifact. Its closure was inspected:
only `workflow.validation` changed and `workflow.tag_policy` was added (47 → 48 modules).
Two independent local builds and their verifiers produced identical bytes before the test oracle
was updated:

- Manifest SHA-256: `d45bc407680156f9ef75f7983afef90a375e392ef324750d87a2bbc10b3d1fc3`.
- Archive SHA-256: `60fa1be144e46fb5f2a1aa34973e059ef90452089112ca5b9fbf81631f6c0406`.

This reconciles a local non-deployable source oracle, not the sealed deployed runtime. Existing
Phase 6 packaging must include the shared helper; the production-disabled source closure likewise
gains that one module. No publication behavior or permissions are changed.

## Known evidence limitation — not an MVP blocker

The only missing evidence for the requested comparison is the original eleven v1 candidate pools.
Provide an existing raw capture if one exists outside this checkout; otherwise a separately
authorized bounded rerun of the unchanged v1 evaluator would be needed to capture one pool per
artwork and replay both selectors against each identical response. Neither action is required
to close this local MVP refinement checkpoint. No further prompt design is required, and this
pass does not authorize an inference run to recover historical evidence.

Known limitations: lexical rules cannot establish arbitrary synonym equivalence, visual grounding,
or natural shopper intent; those remain Gemma/seller judgments. Saved v1 copy retains its documented
historical inaccuracies rather than silently rewriting the reference. Before any future deployment,
old stored reviews rejected solely by the previous rule need explicit handling because projection
checks stored validation against current validation. No stored review, approval, or product was
migrated or modified here.
