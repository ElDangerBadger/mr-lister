# Final cold-path exploration — accepted 1024 MB setting

## Status and boundary

**Seller decision: retain preparation at 1024 MB permanently.** Provider remains
1024 MB. The final supplied job completed successfully: **29.982 seconds to
validated text**, with both preparation and provider confirmed cold. This
supersedes the temporary-trial plan: **do not revert to
256 MB automatically and do not run the 1769 MB comparison.**

Last successful live readback at `2026-09-10T05:45:16Z` confirmed only preparation
memory changed from 256 to 1024 MB, with state Active and update Successful.
Provider configuration was byte-for-byte unchanged; no code, prompt, seller data,
publication or other infrastructure change was made. No preparation was running
before the revision-conditional update. Fresh readback on acceptance was blocked
by expired bootstrap OAuth authorization; no further AWS mutation occurred.

**CloudFormation persistence is prepared but not executed.** The current Original
template was captured and matched exact predecessor SHA-256 `02593facd958693b1a27d432a3bfa305cb7b12dc1195da42665e106d185ab7a2`.
The validated target changes only
`Resources.PreparationDispatchFunction.Properties.MemorySize = 1024`. The standard
change set is ready, with no replacements. AWS OAuth expired again before its
execution. No 256 MB restoration or other runtime mutation occurred.

The seller's acceptance does not establish a measured 30-second cold/browser
KPI. Capture the final supplied job's existing traces without reopening a broad
benchmark or acceptance program. Pass B remains out of scope.
The final supplied run digest is `354cc93551460bee352e7dda`; it reached
`awaiting_approval`, review version 1 / record version 8, with both uncertainty
flags false. No additional upload, approval or publication is required for this
checkpoint. No further performance optimization is recommended before demo/UI work.

## Final matched-artwork timing

| Metric | Before cold, preparation 256 MB | After cold, preparation 1024 MB | Historical warm, preparation 256 MB |
|---|---:|---:|---:|
| Upload → backend validated text | 55.172 s | **29.982 s** | 20.376 s |
| Upload → synchronized draft | 77.478 s | 50.695 s | 30.187 s |
| Upload → economics / Save ready | 88.316 s | 61.073 s | 32.436 s |
| First-use preparation setup | 32.056 s | **7.626 s** | 0.001 s |
| Strands execution | 14.629 s | 13.661 s | 13.377 s |
| Provider first-use setup | 7.767 s | 7.760 s | 0.001 s |
| Shipping HTTP request | 8.913 s | 7.981 s | 0.095 s |

All three use identical artwork SHA-256, 449,186 bytes and the same
`gildan_64000_swiftpod` v2 profile. Provider memory remains 1024 MB throughout.
The new sample is genuinely cold: preparation managed INIT 74.84 ms and provider
INIT 72.54 ms, followed by their measured first-use intervals. The preparation
REPORT confirms 1024 MB allocated / 175 MB peak used. The setup improvement is
**24.430 seconds**; most of the **25.190-second** text-readiness improvement lies
in that interval. No release verification was removed, deferred or bypassed.

One run is recorded per condition, not a statistical median. Actual browser
first-edit timing remains unobserved; backend validated text is not renamed as a
measured browser interaction. No 1024 MB warm sample or 1769 MB result exists; the
seller canceled further memory trials. This is sufficient for a practical
approximately-30-second backend preparation checkpoint, not a stricter browser SLA.

All runs retain **4 model calls, 2 Strands cycles and 14 Printify requests**.
There was no model repair or failed HTTP request. SEO/listing semantics and
publication/approval code were not changed, so no new copy-quality tradeoff was
introduced or claimed to have been independently re-scored. The existing seller
review, exact-version Save/approval and final publication confirmation remain.

Remaining ranked live contributors: Strands **13.661 s** (nested AI/controller
work), preparation setup **7.626 s**, and bridge-before-Strands **6.045 s** (aggregate
remote startup/authority/request interval, not an independently attributed
AgentCore INIT measure). After text readiness, provider startup **7.760 s**,
draft creation HTTP **7.435 s**, and shipping **7.981 s** still contribute to
draft/Save timing. These intervals are not all disjoint; do not sum nested spans.

The actual first Printify HTTP request was shop identity, **0.284 s**. Shipping
was request 14, following ~40 ms of credential resolution; its repeated slow
response does not establish HTTP/client-construction or Lambda INIT as its cause.
No provider-session/cache optimization is included.

### Memory and cost comparison

| Preparation allocation | Cold setup | Billed duration | Billed compute |
|---|---:|---:|---:|
| 256 MB reference | 32.056 s | 53.149 s | 13.28725 GB-s |
| 1024 MB accepted | 7.626 s | 27.472 s | 27.472 GB-s |
| 1769 MB | Not tested; canceled | — | — |

Preparation billed compute increased **106.75%**, despite shorter duration,
because the larger allocation remains billed while waiting for remote AI.
Combined preparation plus provider compute was **56.182 vs 43.45525 GB-s**,
approximately **29.29% higher** for these cold samples. These figures exclude API
Lambdas, AgentCore, model and external-provider costs; they are not a whole-job
dollar-price estimate. No new inference, provisioned capacity or infrastructure
service was added. Current observation shows no retry/failure regression, but is
not a broad reliability study.

Evidence: [44 sanitized final-run events](evidence/cold-path-1024-final-latency.jsonl),
private `final-platform.jsonl` and `final-waterfall.{json,md}` under the directory
below. Existing collector succeeds and its **11 regression tests pass**. Its
legacy “editable” label denotes backend Save readiness when no browser milestone
was captured. Existing release CI is green; no application change required a reseal.

## Ranked setup attribution

The observed cold preparation interval is 32.056 seconds, versus about 1 ms in
the warm worker. Existing logs do not split its internal operations. Source and
local measurements identify the following candidates; **local values are not
Lambda attribution percentages or Lambda duration predictions**.

| Component | Local median, seconds | Interpretation |
|---|---:|---|
| Two full packaged-release verifications | 1.769 | Dominant measured local component; repeated inventory/path/hash work |
| boto3 import | 0.273 | Process-cached after first use |
| Release module import | 0.267 | Process-cached after first use |
| Remaining cloud entrypoint imports | 0.217 | Process-cached after first use |
| First DynamoDB client | 0.039 | Already cached in the lazy handler |
| AgentCore client | 0.009 | Already cached in the lazy handler |

Measurements used Python 3.12.14 on macOS ARM64. Import trials were fresh
processes with an unused bytecode prefix and writes disabled; this also excludes
stdlib bytecode that Lambda may have, so import comparisons are approximate.
The exact sealed Lambda tree contains 2,557 files / 61,802,627 bytes. Release:
`e7adb0e8709323af4a49a828635d4c74666471d9c7bf042718b88933355eb3ea`.
The local repository verifier is byte-identical to this release's verifier.
Independent repeated double-verification measurements were 1.772 / 1.679 /
1.768 seconds. No sealed files were edited or imported as native macOS code.

The shim verifies before importing production composition; `_environment()`
verifies again before constructing the cached handler. Across both checks there
are four full inventories and roughly 15,312 file hashes. A profiled single
verification spent 1.668 of 2.122 seconds in inventories; nested path operations
accounted for 1.050 seconds. Profiler overhead means these absolute values are
not comparable to unprofiled medians. Do not remove a verification boundary
without preserving direct-entrypoint validation and the immutable release binding.

Preparation Lambda builds DynamoDB and AgentCore clients, not Strands or Gemma.
The separate AgentCore runtime reuses application/model clients but creates
job-bound Strands state per request. Preserve that isolation. Configuration,
imports and AWS clients are already reused within each environment. Moving them
to module scope alone would shift the timing label, not remove cold wall time.
Job/work checks and owner-bound provider credential resolution remain per request.

## Accepted setting and remaining confirmation

The original plan was a bounded 256 / 1024 / 1769 MB comparison followed by
restoration pending user review. The seller has now completed that review by
accepting 1024 MB; the higher-memory trial and automatic restoration are canceled.
No result at 1769 MB exists or is required.

For the seller's final supplied job, use the existing traces to record
upload-to-text, draft, Save, setup/AI/provider intervals and billed duration.
Classify cold/warm from platform records and separately label actual browser
first-edit timing if available. Do not mistake allocation acceptance for measured
speedup, or demand additional runs solely to make the sample statistically ideal.

[AWS memory documentation](https://docs.aws.amazon.com/lambda/latest/dg/configuration-memory.html)
states CPU allocation scales with memory and 1769 MB corresponds to one vCPU.
Compare measured billed GB-seconds, not runtime duration alone. Increasing memory
also increases the charge while the bridge waits for remote AI. Pricing/cost
estimates must distinguish Lambda from unchanged model/provider costs.

## Shipping penalty: repeated, but not explained by Lambda initialization

Existing shipping GET times: 7.934 / 0.091 / 0.096 seconds in A1; 8.706 seconds
in the first A7 sample; 8.913 / 0.095 seconds in the matched pair. The second A1
provider container was cold yet its shipping GET was fast.

The timed interval begins after credential resolution and HTTP client/opener
construction, and ends before JSON/schema validation. It includes network
connection, response-header wait and body transfer. Shipping follows thirteen
other provider requests; it is not the first Printify HTTP request. Credential
resolution was tens of milliseconds, not seconds.

Four earlier batch shipping GETs lasted 8.663 / 7.733 / 6.197 / 5.138 seconds,
started over 3.5 seconds, and all finished within 5 ms of
`2026-09-09T21:51:29.97Z`. A shared upstream operation or external bottleneck is
plausible, **not proven Printify behavior**. Current evidence does not justify
transport/session/cache changes. Retain this as recurrent external-request
variability; the final 7.981-second shipping request reproduces the slow response
without identifying its external cause. Do not attribute it to client creation.

## Rollback / verification

The accepted preparation target is **1024 MB**. The old 256 MB capture remains
available only as an explicitly authorized rollback option; it is not a pending
action. No automatic rollback scheduler was installed. Future updates must retain
1024 MB and must not overwrite unexpected concurrent configuration/code drift.

Exact private baseline and successful 1024 MB readback:
`.mr_lister_private/pass-a-20260909/cold-memory-20260910/`.
Preparation code SHA-256 remains
`CRQFVUTXWoP0y7SfoqQDAI6Sd5g6KlK5SxGj5zjyuLA=`; timeout 600 seconds,
Python 3.12 ARM64. Provider remains 1024 MB with its original configuration.
The 1769 MB trial is canceled. Current serving source and web rollback remain in
[Pass A performance](pass-a-performance.md#active-write-editor-correction--deployed).

Prepared persistence artifact:

- Template SHA-256 `a505a639bd881f677f416a296760fb6686d72f5f21dcdf657f95a8bfb0ef52b3`.
- S3 key `private/deployments/cloudformation/core/preparation-memory/a505a639bd881f677f416a296760fb6686d72f5f21dcdf657f95a8bfb0ef52b3/core-template.json`,
  existing Phase 6 artifact bucket; VersionId `61a72toPlUm8KNAN57NnbXarcSjRpNfq`.
- Change set `mr-lister-phase6-dev-preparation-memory-a505a639bd88`,
  ID `825a59d8-3f32-4516-b3da-c25b49a210ea`, currently unexecuted.
- Exact single-property diff, independent review and SAM lint/validation passed.
  Retain all ten existing parameters and the existing runtime CloudFormation role.
  Standard inspection shows only the memory modification and dependent, unchanged
  preparation-function/role ARN references; no replacements.
- Expanded property-value inspection reproduced the prior provider-memory
  deployment's known `cloudfront:GetFunction` read limitation and unresolved
  CloudFront/SNS values. Its warning is retained in `persist-change-set.json`.
  The standard view plus exact Original-template diff is the established
  deployment check; no IAM expansion or changed CloudFront/SNS property is intended.
- Historical rollback template: Safari predecessor `02593fac…`, version
  `pAqjeHCNmLqZ8i9RtxVcq1lDwMYOv4OD`; deploying it would restore preparation to
  256 MB and therefore requires explicit rollback approval, not automatic use.

Remaining work is only execution and exact configuration/template readback of
this prepared persistence change after renewed AWS authorization. Final timing
readback is complete. No further memory optimization is authorized or recommended.
