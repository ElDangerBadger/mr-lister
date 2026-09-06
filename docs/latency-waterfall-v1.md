# Latency Waterfall v1

## Checkpoint decision

**STAGE 0 COMPLETE — READY FOR REVIEW. STAGE 1 HAS NOT STARTED.**

The sealed workflow was observed without changing execution, approval, or publication semantics.
Five fresh authenticated single-artwork jobs all reached an editable Human review and an
unpublished synchronized Printify draft. No sampled job was approved or published.

The new live baseline is:

| Metric | Median | Minimum | P90 / maximum | Program target |
|---|---:|---:|---:|---:|
| Upload accepted → editable review available | 45.053 s | 38.942 s | 127.133 s | ≤30 s stretch |
| Upload accepted → synchronized draft | 40.978 s | 34.902 s | 113.633 s | ≤80 s minimum / ≤50 s strong |
| Model invocations | 4 | 4 | 4 | 1 normal / 2 repair maximum |
| Strands cycles | 2 | 2 | 2 | 1 bounded path |
| Printify requests | 14 | 14 | 14 | ≤6 |

The four warm runs meet the strong synchronized-draft target. The cold run does not meet the
minimum target, and editable review does not yet meet the 30-second stretch target. Stage 0 is a
measurement checkpoint, not an optimization claim.

## Release and sample authority

| Item | Value |
|---|---|
| Deployed source | `cbfcd3a4ee7ef15bc4f43cac2b6503bf7d9efaaf` |
| Phase 6 release fingerprint | `3ae13d46db5e11731a5f69f541175d5c540cd93db823629d38cc91a108d790cc` |
| Phase 6 stack | `mr-lister-phase6-dev`, `UPDATE_COMPLETE` |
| AgentCore endpoint | `phase6_v5_dev`, runtime version 5 |
| Web bundle | `78d050a917b77faf373c34713d9ea94a0a338ad4479d29b826ce355d491ae95b` |
| Observation window | 2026-09-06 16:07:41Z–16:18:26Z |
| Sample | Five fresh jobs using one controlled representative 1440×1440 PNG |
| Seller boundary | Authenticated MassSkutiny flow; editable review only; no approval/publication |

The controlled artwork was repeated intentionally so that infrastructure and provider variance
could be compared without changing image complexity between runs. Browser, Lambda, AgentCore,
Printify-boundary, and Step Functions timestamps were joined only through one-way run digests.
Raw job IDs and provider identities remain in ignored private evidence.

## Five-run waterfall

The rows below are chronological. P90 is the nearest-rank maximum for this five-run sample.

| Sequence | Run digest | Upload → Strands start | Strands | Strands → synchronized | Synchronized → editable | Upload → editable |
|---:|---|---:|---:|---:|---:|---:|
| 1 (cold) | `d91f63f345976ef8e556d344` | 45.442 s | 18.254 s | 49.937 s | 13.500 s | 127.133 s |
| 2 | `0a0bfb473af1b49477679b7c` | 7.624 s | 16.967 s | 10.311 s | 4.040 s | 38.942 s |
| 3 | `4c31b9d0659a7d7cac925bbf` | 6.945 s | 23.898 s | 10.301 s | 3.910 s | 45.053 s |
| 4 | `8287655fa886105f69ca8454` | 9.747 s | 21.147 s | 10.084 s | 4.140 s | 45.119 s |
| 5 | `87f2480844d65c8e54dc287d` | 7.159 s | 21.997 s | 10.956 s | 4.780 s | 44.892 s |
| Median | — | 7.624 s | 21.147 s | 10.311 s | 4.140 s | 45.053 s |

Browser work before the backend KPI was small: artwork normalization was 38.0 ms median and the
direct S3 artwork upload was 695.7 ms median.

## AI critical path

Every run used two Strands cycles and four total model invocations:

- two controller invocations recorded by Strands; and
- one multimodal artwork inspection plus one listing-draft intelligence invocation.

The combined `artwork_listing_intelligence` span was 19.206 seconds median. Total Strands
execution was 21.147 seconds median. This is the largest repeatable warm-path component and is the
measured input to Stage 1; no AI execution change was made in Stage 0.

## Printify request map

Every run made the same 14 requests:

| Purpose | Calls per run | Median request duration |
|---|---:|---:|
| Shop identity | 2 | 263 ms |
| Blueprint catalog | 2 | 82 ms |
| Print-provider catalog | 2 | 75 ms |
| Variant catalog | 2 | 50 ms |
| Artwork upload | 1 | 755 ms |
| Artwork-upload readback | 1 | 262 ms |
| Draft create | 1 | 4.342 s |
| Draft readback | 1 | 498 ms |
| Product-cost readback | 1 | 528 ms |
| Standard shipping | 1 | 196 ms |

The median aggregate Printify HTTP time was 7.772 seconds per run. Credential resolution occurred
seven times per run but totaled only 334 ms at the median. The cold run's provider HTTP time was
19.188 seconds, including an 8.805-second standard-shipping outlier and a 6.912-second draft
create. The repeated stable catalog/shop discovery is measured here only; its removal belongs to
Stage 2.

## Wall-clock explanation

Direct application spans explain 94.85%–95.98% of the editable-review window for the four warm
runs. Their remaining 1.7–2.3 seconds are exact workflow launch, scheduling, and settlement
transitions.

The cold run initially had only 41.82% direct-span coverage. Step Functions execution timestamps
and Lambda `START` records close the two large gaps:

| Cold-run category | Upload → synchronized | Upload → editable |
|---|---:|---:|
| Instrumented application spans | 40.115 s | 53.166 s |
| Workflow control-plane transitions | 3.131 s | 3.580 s |
| First-use lazy handler setup | 70.387 s | 70.387 s |
| Total classified | 113.633 s | 127.133 s |

The preparation Lambda spent about 35.012 seconds after Lambda entry and before its first
application span. The provider Lambda then spent about 35.377 seconds at the same first-use
boundary. Source inspection places both intervals inside the sealed entrypoint's release
verification/import and `_LazyHandler._get()` dependency construction before the instrumented
handlers. AWS reported only 89.56 ms and 90.48 ms of Lambda
`Init Duration`, respectively, so the 70.4-second penalty is not the managed Python runtime init.
Its internal composition is deliberately left for the ordered Stage 6 cold-start audit.

With these exact platform boundaries, 100% of every sampled KPI window is assigned to a major
operation or control-plane/setup interval. Four runs also independently exceed 94% direct timed-
span coverage. This satisfies the Stage 0 requirement to explain at least about 90% of observed
wall-clock latency without adding instrumentation solely to chase the cold outlier.

## Historical comparison

The prior directional live evidence recorded 118.811 seconds from backend job creation to the
prepared review and another 45.628 seconds to the synchronized draft: 164.439 seconds total. The
new 40.978-second median is 75.08% lower, while the cold 113.633-second run is 30.90% lower.

This is not credited as a Stage 0 optimization. The historical number is one older run, begins at
a different durable boundary, and has no model- or request-level detail. Stage 0 establishes the
new five-run baseline from which later changes must be measured.

## Evidence boundaries and observations

- `editable_review_available` is the authoritative backend readiness timestamp. All five reviews
  were visibly opened and editable in the authenticated browser, but browser render/poll lag is
  not a separate trace event.
- One seller projection poll during run 4 returned a transient HTTP `503`; the next poll recovered
  without seller action and the job reached editable review.
- Approval, publication request, provider publication confirmation, and Etsy visibility were not
  re-exercised for these five baseline jobs because doing so would create external listings. They
  remain `not observed` in the v1 scorecard. The sealed Phase 7 canary remains the existing
  directional publication evidence; no publication runtime was changed merely to improve Stage 0
  evidence.
- The five jobs all used the normal success path. No repair model call, provider reconciliation,
  seller retry, approval, or publication occurred.

## Canonical artifacts

- Machine-readable waterfall: [`evidence/latency-waterfall-v1.json`](evidence/latency-waterfall-v1.json)
- Platform gap attribution: [`evidence/latency-waterfall-v1-platform.json`](evidence/latency-waterfall-v1-platform.json)

The machine artifact is generated by the fail-closed offline collector. It contains 235 sanitized
events across exactly five run IDs: 15 browser events and 220 CloudWatch events, with 47 events per
run and no duplicate event IDs. The collector's focused tests, Ruff check, and formatting check
passed after its optional `prepared_review_recorded` milestone was aligned with the deployed trace
contract.

## Stage 0 stop

Stage 0 provides a complete baseline and identifies three measured later-stage inputs: the
four-call/two-cycle AI path, 14 provider requests, and a 70.4-second cold first-use setup penalty.
No execution optimization, cache, concurrency change, progressive review behavior, or Stage 1
implementation is included in this checkpoint.
