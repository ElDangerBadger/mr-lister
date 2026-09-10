# Final cold-path exploration — accepted 1024 MB setting

## Status and boundary

**Seller decision: retain preparation at 1024 MB permanently.** Provider remains
1024 MB. The seller explicitly accepts the increased allocation and will run one
final confirmation. This supersedes the temporary-trial plan: **do not revert to
256 MB automatically and do not run the 1769 MB comparison.**

Last successful live readback at `2026-09-10T05:45:16Z` confirmed only preparation
memory changed from 256 to 1024 MB, with state Active and update Successful.
Provider configuration was byte-for-byte unchanged; no code, prompt, seller data,
publication or other infrastructure change was made. No preparation was running
before the revision-conditional update. Fresh readback on acceptance was blocked
by expired bootstrap OAuth authorization; no further AWS mutation occurred.

**CloudFormation persistence remains pending.** The live trial used a direct
Lambda configuration update, not a stack-template update. After renewed
`aws login --profile mr-lister-bootstrap --region us-west-2`, capture the current
Original template and persist only
`Resources.PreparationDispatchFunction.Properties.MemorySize = 1024` using the
existing one-property, versioned-template/change-set process. Preserve provider
1024 MB, all code/environment bindings, parameters and service role. Do not
deploy the frozen foundation template or claim template synchronization complete
until readback verifies it. This closes configuration drift, not a new experiment.
The browser skill also found no connected browser; fresh matched submissions
will require the seller-operated site or a newly connected authenticated browser.
Do not substitute synthetic seller authorization or modify acceptance runners.

The seller's acceptance does not establish a measured 30-second cold/browser
KPI. Capture the final supplied job's existing traces without reopening a broad
benchmark or acceptance program. Pass B remains out of scope.
The final supplied run digest is `354cc93551460bee352e7dda`; its live state/timings
have not yet been read because AWS authorization remains unavailable. Do not
request another upload merely because this readback is pending.

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
variability pending the controlled runs; do not attribute it to client creation.

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
Remaining work: exact stack-template persistence and readback after AWS login,
then the seller's final-job timing readback. No further memory optimization is
authorized by this acceptance decision.
