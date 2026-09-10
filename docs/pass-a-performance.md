# Pass A — happy-path latency

## Current checkpoint

**A1 measured; A2 benchmarked and held; A3–A5 assessed; A6 locally verified.**
No Pass A code has been deployed. The released AI/provider execution remains
unchanged. UI simplification and publication changes are outside this pass.

Source rollback: `d71218d982b68a078b02ce3914f96ed2523da948` on main,
[green CI](https://github.com/ElDangerBadger/mr-lister/actions/runs/34412919494).
The deployed SEO/prompt, Safari, provider-memory and publication-query fixes remain
as recorded in [SEO release state](etsy-seo-release-state.md) and
[Phase 7 release state](phase7-release-state.md). AgentCore remains version 6,
endpoint `phase6_v6_dev`; no deployment rollback operation has been needed.

## A1 — 2026-09-09 baseline

Three seller-operated, sequential, fresh single-artwork jobs used identical
3,595,318-byte normalized PNG content and the exact `gildan_64000_swiftpod` v2
profile. Existing traces were collected without changing instrumentation. The
sampling window was 22:54:39Z–23:04:49Z. All three reached editable-review backend
readiness. The sampling procedure requested no approval or publication.

| Run digest | Runtime condition | Upload → Strands | Strands | Strands → synchronized draft | Draft → editable | Upload → editable |
|---|---|---:|---:|---:|---:|---:|
| `878da8c4e427ee17481c6378` | Preparation and provider cold | 48.056 s | 21.057 s | 22.866 s | 9.900 s | 101.878 s |
| `794d92d938d3df64b6638825` | Preparation warm, provider cold | 14.041 s | 22.483 s | 19.897 s | 2.401 s | 58.822 s |
| `8b54001fcc9ba30b783bea54` | Both warm | 12.295 s | 16.897 s | 10.481 s | 1.822 s | 41.495 s |

Mixed-condition median: **58.822 s** editable, **56.421 s** synchronized draft;
slowest: **101.878 s** editable. The single fully warm observation is **41.495 s**
editable / **39.673 s** synchronized, consistent with the previous practical warm
range. One warm observation is not a new statistically meaningful warm median.
Keep both the complete sample and runtime classification in the A7 comparison.

Cold classification is from observed Lambda streams, START/REPORT records, and
first handler-span timestamps, not inferred from slow results. First-use setup
inside the handler took **31.938 s** in preparation and **7.417 s** in provider
for run 1; a different provider container added **7.933 s** in run 2. Run 3 reused
the run-2 containers, with about 1 ms before each handler span. Managed Lambda
initialization itself was only 66–74 ms. Shipping added **7.934 s** in run 1,
versus 91–96 ms in runs 2–3.

Every run used **4 actual model invocations** (2 Nova controller + 2 Gemma),
**2 Strands cycles**, and **14 Printify HTTP requests**. Strands duration median
across the three conditions was **21.056 s**. Gemma analysis/listing plus local
intelligence preparation took **17.286 s** median; this nested interval must not
be added again to the enclosing Strands interval.

| Printify purpose | Calls per job | Classification |
|---|---:|---|
| Shop identity | 2 | Stable seller state, revalidated twice |
| Blueprint catalog | 2 | Stable catalog metadata |
| Provider catalog | 2 | Stable catalog metadata |
| Variant catalog | 2 | Stable catalog metadata |
| Artwork upload | 1 | Per-job required |
| Artwork readback | 1 | Defensive verification |
| Draft creation | 1 | Per-job required |
| Draft readback | 1 | Synchronized authoritative evidence |
| Product cost readback | 1 | Immediate reread for economics |
| Standard shipping | 1 | Provider metadata for economics |

Method limitations: the existing upload milestone is the reservation's creation
time, before direct S3 transfer completes. Browser normalization precedes that
milestone. These manually operated samples have no captured browser-console
normalization/upload spans; do not invent them or fold normalization into this
KPI. The editable milestone is backend readiness, not separately measured browser
poll/render lag. No new observability system was introduced.

Sanitized application traces: [A1 input](evidence/pass-a-baseline-latency.jsonl),
readable by the existing `tools.build_latency_waterfall` collector. Private raw
platform/readback material belongs under `.mr_lister_private/pass-a-20260909/`;
regression tests must not depend on that directory.

## A2 — benchmarked candidate, not promoted

The ordinary candidate path is the actual Strands direct preparation tool,
exact-version source/profile verification, one structured multimodal Gemma call,
unchanged deterministic tag selection, and the existing durable completion.
One shared schema/tag repair is allowed; no Nova controller call is needed.
The released SEO prompt bundle is unchanged and remains the rollback reference;
only the combined response's transport framing changes.

The model cannot select tools. Genuine SDK hooks observe the direct tool call;
genuine SDK metrics supply cycles and cumulative tokens. A resumed
`LISTING_DRAFTED` checkpoint uses a real application-selected tool-only cycle
with zero inference/tokens, not fabricated model evidence. The existing atomic
completion, seller approval and publication boundaries are unchanged.

The existing eleven-artwork evaluator ran each fixture once against the released
two-call reference and once against the candidate using real Gemma through the
existing `mr-lister-dev` guard. Provider operations were fake: **no Printify or
publication calls**. No prompt redesign, threshold changes or fixture reruns were
used to address quality failures.

| Intelligence benchmark | Released reference | One-call candidate |
|---|---:|---:|
| Median wall time | 10.851 s | 10.459 s |
| Slowest fixture | 47.842 s | 15.491 s |
| Gemma calls, ordinary fixture | 2 | 1 |
| Gemma calls, whole sample | 23 | 11 |
| Repairing fixtures | 1 | 0 |
| Actual candidate Strands cycles | Not measured by reference evaluator | 1 each |
| Valid prepared reviews / exactly 13 tags | 11/11 | 11/11 |
| Existing automated quality checks | 10/11 | 9/11 |

The median improvement is **0.392 s (3.6%)**, below the practical two-second
complexity threshold. The old moon-moth case required a repair; its outlier
improved, but this single sample does not establish a reliable tail improvement.
The reference evaluator measures Gemma intelligence, not the live Nova controller;
do not add the A1 controller durations to claim a measured full-stage improvement.

Quality inspection found modest abstract/geometric search-coverage loss and more
generic jellyfish tags. Some automated misses are lexical/synonym differences,
not wrong artwork interpretation. The old seahorse reference also missed an
existing visual-anchor check. Checkerboard interpretation is a shared existing
limitation. These findings do not trigger another SEO design cycle.

**Decision: retain the released AI path.** The experimental runtime wiring was
removed from the working source, not merely left behind an activation setting.
A recoverable patch is parked at
`.mr_lister_private/pass-a-20260909/a2-held/a2-runtime-wiring.patch`, based on the
source rollback above. Only the offline evaluator imports the retained unified
intelligence experiment; no production composition does. Focused tests prove one
call/one cycle and at most one shared repair; they do not override measured
quality/performance results.

Reproduction uses `tests/evaluation/test_live_bedrock.py` with execution modes
`two_call` and `one_call`, prompt
`2026-09-08.2-etsy-seo-plain-preview-tag-diversity`, reference fingerprint
`c91e5ed73eaa62754b00ae335189298548a593fe5662e3445c049efbebab6cd3`.
Candidate transport fingerprint:
`3b740eae2e949eff6888de994ee10325e6eb6dc5a0d2a18e7d6b4377679f22aa`.
Private per-fixture accepted outputs and timings are in
`.mr_lister_private/evaluation-results/pass-a-two-call-baseline-20260909/` and
`pass-a-one-call-candidate-20260909/`; ordinary tests do not depend on them.

## A3–A5 — measured scope decisions

- **A3, provider rediscovery: defer cache.** All eight catalog/shop GETs together
  took 0.821 s in the fully warm run (0.901 s in run 2). A new owner/shop/credential/
  profile-bound TTL cache could reduce 14 requests to 6 on a warm hit, but saves
  under one second. Shipping was 0.091–0.096 s warm; a cache would not avoid its
  first-use 7.934 s outlier. Request count alone does not justify the complexity.
- **A4, authoritative cost reuse: defer.** Existing synchronized records retain
  provider costs and timestamps. Reusing them and omitting the second preflight
  could save about 0.983 s median. Correctly distinguishing automatic economics
  from explicit Refresh requires origin/freshness checks; neither can be inferred
  just from a missing pricing snapshot. The live Refresh path remains unchanged.
- **A5, overlap: scope boundary.** Upload authorization requires the durable
  synchronize work created by Strands completion. Preparation has no provider
  credential/capability. Overlapping these needs another work lifecycle, join,
  cancellation/reconciliation handling and deployment-role changes. That exceeds
  this pass. Narrow same-worker overlaps save at most about half a second.

These are assessed opportunities, **not shipped optimizations or achieved call
targets**. All 14 provider requests and the working publication path remain.

## A6 — earlier local editing

The current projection already returns validated listing content before provider
draft/economics completion. The scoped candidate enables the existing local
title/description/tag editor at that point, while Save and Approve keep their
existing server authority gates. No backend work/state, approval, publication,
price capability or UI architecture change is included. Unsaved text survives
monotonic provider progress only for the same job, listing content, review version
and fingerprint. Changed versions, failures or interrupted processing keep the
existing deliberate conflict/reapply boundary. Ordinary ready-state conflicts
remain unchanged. The Save handler itself checks the server capability.
Changes stay in the page until the seller deliberately saves after readiness.
The optional `first_editable_review` browser milestone measures actual first
editing availability separately from `editable_review_available`, the retained
backend save/approval-readiness milestone. This extends the existing collector;
it does not infer browser timing from backend timestamps.

All **186 web tests**, lint, typecheck and production build passed, including
focused early-edit/progress/conflict/blocked-state tests. Independent safety review
found no blocker. Live timing remains to be measured; this is not yet an achieved
KPI. The connected-browser service was unavailable, so live browser timing requires
a seller-operated sample or a connected authenticated browser.

## Pass A scorecard

| Metric | A1 baseline | Current live | Goal |
|---|---:|---:|---:|
| Upload → editable review | Warm observation 41.495 s; mixed median 58.822 s | Unchanged | Median ≤30 s; strong ≤25 s; stretch ≤20 s |
| Upload → synchronized draft | Warm observation 39.673 s; mixed median 56.421 s | Unchanged | Secondary KPI |
| AI/Strands duration | 21.056 s mixed median | Unchanged | ≤15 s strong / ≤10 s stretch |
| Normal model calls | 4 | 4; local candidate 1 | 1 |
| Repair model calls | Legacy paths have separate repair budgets | Unchanged; local candidate ≤2 total | ≤2 total |
| Normal Strands cycles | 2 | 2; local candidate 1 | 1 bounded path |
| Printify requests | 14 | 14 | ≤6 / stretch ≤5 |

No measured performance win or Pass A completion is claimed at this checkpoint.

Local verification after parking the runtime experiment: complete Python suite
**4,128 passed, 11 live tests skipped**; Ruff lint/format, all three existing
contract checks and Python package build passed. Web verification is above;
the existing high-severity dependency audit passes with the two previously known
moderate development-only Vitest findings unchanged. Separate live quality
evaluator failures are reported above. The first
[Pass A CI run](https://github.com/ElDangerBadger/mr-lister/actions/runs/34419399765)
passed web, lint, formatting, contract checks and SAM validation, but 21 new
offline-experiment tests failed because clean installation selected Strands
1.55.1 versus the original local 1.52.0. Its model formatter supplies one new
positional argument. The isolated adapter now accepts the unchanged zero-default
case and rejects unsupported nonzero values. All 27 focused evaluator/adapter
tests pass with both SDK versions; dependencies and production composition were
not changed. Fresh-environment full verification and CI rerun are pending.
No candidate has been deployed yet.
