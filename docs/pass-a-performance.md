# Pass A — happy-path latency

## Current checkpoint

**A1 measured; A2 held; A3–A5 assessed; A6 deployed; A7 backend sample recorded.**
The early local editor is live on `https://massskutiny.com`. AI/provider execution
remains unchanged. The corrected-editor sample has one cold run and one warm run;
warm backend text/draft/Save readiness was **20.376 / 30.187 / 32.436 seconds**.
**Pass A is not yet complete:** actual browser first-edit timing and a warm median
have not been established. The seller reports the second run was noticeably
faster. No additional runtime changes or uploads are required for this readback.
UI simplification and publication changes are outside this pass.

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

Method clarification from the A7 source check: the upload milestone uses the
timestamp captured at the start of `complete_upload`, persisted on the completed
upload receipt and job. Direct S3 transfer and browser normalization precede it;
backend object verification/pinning and completion response follow it. It is not
reservation creation or the browser's drop/response time. Historical measured
intervals are unchanged. These samples have no captured browser-console
normalization/upload spans; do not invent them or fold normalization into this
KPI. The original editable milestone is backend readiness, not separately measured
browser poll/render lag. No new observability system was introduced.

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
| Upload → browser first edit | Not captured; prior backend Save-ready warm observation 41.495 s | Not captured; warm backend text ready 20.376 s, Save ready 32.436 s | Median ≤30 s; strong ≤25 s; stretch ≤20 s |
| Upload → synchronized draft | Warm observation 39.673 s; mixed median 56.421 s | Warm observation 30.187 s; execution unchanged | Secondary KPI |
| AI/Strands duration | 21.056 s mixed median; warm observation 16.897 s | Warm observation 13.377 s; execution unchanged | ≤15 s strong / ≤10 s stretch |
| Normal model calls | 4 | 4; held offline candidate 1 | 1 |
| Repair model calls | Legacy paths have separate repair budgets | Unchanged; held candidate ≤2 total | ≤2 total |
| Normal Strands cycles | 2 | 2; held candidate 1 | 1 bounded path |
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
not changed. A fresh isolated install then passed **4,130 tests, 11 skipped**,
and the corrected [source CI](https://github.com/ElDangerBadger/mr-lister/actions/runs/34420029221)
is fully green: the same Python totals, **186 web tests**, lint, format,
typecheck, all contract/SAM validations and both package/web builds. The earlier
failed run is retained as historical evidence, not represented as green.

## Static web release — 2026-09-09 Pacific

The initial release below is retained as history. The current serving web release
is the [active-write editor correction](#active-write-editor-correction--deployed)
recorded under A7; backend release tuples remain unchanged.

**DEPLOYED AND PUBLIC READBACK VERIFIED** at `2026-09-10T00:16:34Z`.
Release source on main: `45bb547c5033e847edeab96c31e1166149ee41f1`.
Logical checkpoints: `fc6f8b2` (baseline/offline experiment), `e396e36` (early
editor), `45bb547` (offline SDK compatibility). No branch divergence or merge was
needed. This release changes no Lambda/AgentCore code, runtime configuration,
permissions, provider operations, approval or publication behavior.

- Bucket: `mr-lister-phase6-web-dev-384627057108-us-west-2`; distribution
  `EXC2KQ0RRVWF0`.
- Bundle SHA-256:
  `d284922823a9702244f15b0f89ef3c36e3b3e5c4268ab275184b007d9bb755da`.
- JavaScript: `assets/index-ZNsNEEWR.js`, SHA-256
  `0880dd3a033903df47ffd51ef695f9c6f183453dce7ff8f3e3c679f3109d8038`.
  CSS and favicon bytes equal the predecessor exactly.
- Index SHA-256:
  `37f9d0414dbcd131d93f9b0c1ea17b4ac6626736df93c45220f5b30fa82bc88c`;
  VersionId `VS3NIO8LoofST2jpciTVbwiP2g.LQkhN`.
- Runtime configuration stayed at VersionId `IdRmSDEqfGAjcfmiYHvjrUJ2SQ6s5wqx`,
  SHA-256 `d1f969e5545ba76f76b8f6ef8e66def4d162ae74330a34d64796aebd7f25b5aa`.
- Invalidation `I8C8BB4OKW2OVJHKIVJRYUTKVB` completed. Public `/`, index, JavaScript,
  CSS, favicon and runtime-config bytes match the exact release manifest.
- Rollback: restore index VersionId `vIxCATS8mvUjW0jPpwfemBuDrxccCXLL` (previous
  publication-dialog bundle), then invalidate `/` and `/index.html`. Its hashed
  assets remain present; no backend rollback is required.
- Private release/readback evidence:
  `.mr_lister_private/pass-a-20260909/a6-web/`. `web-release-fixed.json` is the
  deployed manifest; the earlier `web-release.json` was prepared before the CI
  correction and was never deployed.

### Exact tracked files changed

- Web behavior: `web/src/pages/JobReviewPage.tsx`.
- Existing timing seam: `web/src/observability/latency.ts`,
  `tools/build_latency_waterfall.py`.
- Focused timing/editor tests: `web/tests/review-page.test.tsx`,
  `web/tests/latency-observability.test.ts`, `tests/test_build_latency_waterfall.py`.
- Held offline AI experiment: `src/mr_lister/intelligence/unified.py`,
  `tests/test_unified_intelligence.py`, `tests/evaluation/test_live_bedrock.py`,
  `tests/evaluation/test_execution_modes.py`. No production composition imports it.
- Records: `docs/pass-a-performance.md`, `docs/etsy-seo-release-state.md`,
  `docs/evidence/pass-a-baseline-latency.jsonl`.

## A7 — exact remaining checkpoint

### First sample and exposed editor defect

Sample digest `4df70f19867c5f5f6f9ba28a`, collected 2026-09-10 UTC against the
initial early-editor bundle, was **cold preparation and cold provider**, not a
warm-path acceptance result. The seller reported that text took about a minute
to appear; no browser `first_editable_review` event was supplied, so its actual
first-visible/first-editable timestamp is unknown.

| Initial preparation milestone | From upload accepted |
|---|---:|
| Strands started | 33.656 s |
| Validated listing recorded | 45.981 s |
| Strands completed | 47.376 s |
| First synchronized draft | 65.379 s |
| First economics / backend Save readiness | 76.235 s |

START/REPORT and handler traces identify **23.426 s** of first-use preparation
setup before the AgentCore bridge and **7.768 s** of first-use provider setup.
Managed Lambda INIT was only 47.79 / 70.63 ms. Strands took **13.720 s**; Gemma
analysis and copy calls took 5.259 / 5.013 s. Initial shipping GET took **8.706 s**.
The normal first pass retained 4 model calls, 2 cycles and 14 Printify requests.
The seller subsequently saved review version 2; its successful resynchronization
is excluded from the initial-preparation timing window, not counted as another
fresh sample or hidden as an initial-path retry.
Its normalized PNG was 449,186 bytes versus 3,595,318 bytes in A1, with the same
product profile/version. Exact-input parity is therefore not established; do not
present this as a matched before/after performance improvement.

Evidence: [44 initial-pass events](evidence/pass-a-first-cold-sample-latency.jsonl);
private collector report and START/REPORT evidence are
`.mr_lister_private/pass-a-20260909/a7-first-waterfall.*` and
`a7-first-platform.jsonl`. Application-only span coverage does not include lazy
initialization; the explicit platform readback supplies that explanation.

The source check also exposed an A6 defect: `provider_outcome_unconfirmed` is true
during normal authorized artwork upload and draft creation, not just failures.
The early editor incorrectly used it to deny local editing and continuity. This
can make fields read-only/conflicted during otherwise successful preparation;
it does **not** withhold listing text from the API and does not explain the entire
46-second initial wait. The scoped correction removes that one local eligibility
condition. Exact allowed stages, validated listing/version/content, failure and
server Save/approval/publication barriers remain unchanged. Focused tests now
exercise normal false→true→false provider-flag transitions and first-view editing
during a write, as well as blocked failure/reconciliation/cancellation states.
Full web check: **187 tests**, lint, typecheck and build passed. The correction is
now deployed and readback verified; remaining live timing samples may resume
after refreshing the site.

### Active-write editor correction — deployed

**DEPLOYED AND PUBLIC READBACK VERIFIED** at `2026-09-10T00:49:32Z`.
Source on main: `14a9afa0591aacda63bef96561509eb2814fb565`.
[Source CI](https://github.com/ElDangerBadger/mr-lister/actions/runs/34422098573)
is green: **4,130 Python tests passed, 11 skipped; 187 web tests passed**, plus
lint, formatting, typecheck, dependency audit, existing contract/SAM checks and
package/web builds. No backend, runtime configuration, provider, approval or
publication code was changed or deployed.

- Same static bucket and distribution as the initial release above.
- Bundle SHA-256:
  `903c63b35154326fd82a8c0ce5b4022ba18bd4908096936453149283191ca79d`.
- JavaScript: `assets/index-Dfo0LvLH.js`, SHA-256
  `3721efed1c868f1668269f428ec3d10bbdeb5ab437670c04249f7d2ee909ed74`.
  CSS and favicon bytes remain unchanged.
- Index SHA-256:
  `4819bd8c14803459a7483012f06c289287c11090a0160cdb1be53403da916ead`;
  VersionId `sfg2im9_yuRF4ZMFUyjV0fpEHVlDkDc0`.
- Invalidation `I28F44RKTZ696KA1DLPEBV3E1W` completed. Public `/`, index,
  JavaScript, CSS, favicon and runtime-config bytes match the release manifest.
  Runtime-config SHA and VersionId remain exactly those recorded above.
- Rollback: restore predecessor index VersionId
  `VS3NIO8LoofST2jpciTVbwiP2g.LQkhN`, then invalidate `/` and `/index.html`.
  Previous hashed assets remain present; no backend rollback is required.
- Exact correction files: `web/src/pages/JobReviewPage.tsx`,
  `web/tests/review-page.test.tsx`, this record and
  `docs/evidence/pass-a-first-cold-sample-latency.jsonl`. The release pointer in
  `docs/etsy-seo-release-state.md` is updated separately.
- Private versioned rollback, release manifest and public-readback evidence:
  `.mr_lister_private/pass-a-20260909/a6-active-write-web/`.

This corrects local editing availability during normal provider writes, not cold
startup latency. Browser timing and the Pass A performance target remain unproven.

### Corrected-editor sample — readback complete

The two requested individual submissions both reached `awaiting_approval`, review
version 1 / record version 8, with both provider uncertainty flags false. No
approval, publication, model or provider operation was initiated by this readback.
They used identical artwork SHA-256 and 449,186-byte content, with the same
`gildan_64000_swiftpod` v2 profile. The A1 artwork SHA-256 differs and its content
was 3,595,318 bytes, so this is not a matched A1/A7 input comparison.

| Milestone / duration | `67fe655f20f9176c9e3abf9a` — cold | `e1154a5f6cda6e99ee17174e` — warm |
|---|---:|---:|
| Upload → Strands start | 41.604 s | 8.390 s |
| Strands execution | 14.629 s | 13.377 s |
| Upload → backend validated text | 55.172 s | 20.376 s |
| Upload → synchronized draft | 77.478 s | 30.187 s |
| Upload → economics / Save readiness | 88.316 s | 32.436 s |
| Actual browser first-edit time | Not captured | Not captured |
| Provider synchronization span | 11.792 s | 7.855 s |
| Model calls / Strands cycles / Printify requests | 4 / 2 / 14 | 4 / 2 / 14 |

The first job was accepted at `2026-09-10T03:22:47.686203Z`; the second at
`2026-09-10T03:24:02.177775Z`, while the first was still completing provider work.
These were two individual submissions, **not strictly non-overlapping jobs**.
Their preparation and provider invocations reused the same respective Lambda
streams in order. START/REPORT records, rather than duration alone, confirm cold
first use followed by warm reuse. AgentCore emitted different runtime log streams;
the warm classification here refers to the preparation/provider Lambdas, not
proven AgentCore container reuse.

Cold preparation spent **32.056 s** between handler START and its first bridge
span, versus **1.037 ms** warm. Cold provider startup added **7.767 s**, versus
**1.400 ms** warm. Managed Lambda INIT was only **72.20 / 71.47 ms**; the long
delay is first-use setup inside the handler, not the managed INIT metric.
The first shipping request took **8.913 s** versus **0.095 s** on the warm job;
this provider-request latency is separate from the Lambda setup intervals. Draft
creation was also variable: **7.681 s** versus **4.331 s**. The source does not
establish why shipping was slower; do not attribute its entire delay to imports.

Both requests completed normally, without model repair or additional provider
requests. Both had two Nova controller and two Gemma calls. Warm Gemma analysis /
copy took **4.100 / 5.980 s**. The unchanged 14-request purpose map remains the A1
map above. Browser normalization/upload-transfer events were not supplied.

#### Before/after observations, not an attributed optimization benchmark

| Comparable endpoint | A1 fully warm, n=1 | Corrected-editor fully warm, n=1 | Observed difference |
|---|---:|---:|---:|
| Upload → backend validated text | 27.922 s | 20.376 s | −7.547 s |
| Upload → synchronized draft | 39.673 s | 30.187 s | −9.487 s |
| Upload → backend Save readiness | 41.495 s | 32.436 s | −9.059 s |
| Strands execution | 16.897 s | 13.377 s | −3.519 s |
| Provider synchronization span | 10.022 s | 7.855 s | −2.166 s |
| Model calls / cycles / Printify requests | 4 / 2 / 14 | 4 / 2 / 14 | No reduction |

Input size/content and natural model/provider variability prevent attributing
these reductions to the web change. A1 already recorded validated text before
Save; A6 exposes that existing interval. In the corrected warm run it is
**12.060 s**, before browser polling/render lag. It is not a demonstrated
12-second browser speedup. The two corrected runs have mixed-condition medians
**37.774 s** backend text / **53.832 s** draft / **60.376 s** Save; slowest is
**55.172 / 77.478 / 88.316 s**. These are not warm medians. The earlier cold sample
against the predecessor editor remains separate, rather than pooled across
different web releases.

Evidence: [88 sanitized events](evidence/pass-a-corrected-editor-latency.jsonl).
Existing offline collector succeeded; its report is at
`.mr_lister_private/pass-a-20260909/a7-pair-waterfall.{json,md}` and exact
START/REPORT records at `a7-pair-platform.jsonl`. The legacy collector label
"Upload → editable" means backend Save readiness when the optional browser
milestone is absent; this document labels it explicitly. Warm sync span coverage
is **90.81%**; cold application-only coverage is **42.35%**, excluding the
separately measured first-use setup. No new instrumentation was added.

Focused collector regression: **11 passed**. Existing release source CI remains
green (**4,130 Python / 187 web**); release-record CI
[34423031067](https://github.com/ElDangerBadger/mr-lister/actions/runs/34423031067)
is also green. This checkpoint adds only the sanitized evidence and this record;
no application, infrastructure, prompt, safety, or publication changes. Deployed
source and versioned rollback remain those in the correction release above.
There is no newly introduced listing-quality tradeoff because the model path and
SEO baseline were not changed; this readback did not independently score copy.

**Stop assessment:** the warm observation is promising, not a proven ≤30-second
browser median. The missing evidence is actual first-edit timing and repeated
warm observations; it is not an MVP functional failure. Existing console records,
if retained, can supply the first-edit event without another run. Reloading a job
cannot recover its original first-edit timestamp. Polling runs three seconds
after the preceding requests finish and pauses when hidden/offline; backend time
plus three seconds is not an upper bound.

The largest measured next opportunity is first-use preparation setup, followed
by the remaining AgentCore bridge/start interval and model work on the warm path.
Recommend a bounded startup profile/memory comparison before any warm-runtime
infrastructure purchase, not another SEO cycle or provider-cache project. No such
change is made here. Pass A remains unproven against its KPI; Pass B does not begin.
