# Final cold-path exploration — working checkpoint

## Status and boundary

Step 1 local attribution and historical provider-request review are complete.
**Live memory comparison has not started.** The first read-only configuration
request failed because `mr-lister-bootstrap` OAuth authorization expired. No AWS
configuration, runtime, prompt, seller data, or publication change was made.
Resume with `aws login --profile mr-lister-bootstrap --region us-west-2`.
The browser skill also found no connected browser; fresh matched submissions
will require the seller-operated site or a newly connected authenticated browser.
Do not substitute synthetic seller authorization or modify acceptance runners.

This is not the final A/B/C recommendation. Pass B remains out of scope.

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

## Bounded experiment to run next

1. Capture current live preparation/provider configuration, code hashes,
   revisions and rollback settings before any mutation. Expected preparation
   memory is 256 MB; provider remains 1024 MB. Verify, do not assume, these values.
2. Compare preparation at **256 / 1024 / 1769 MB**, unchanged sealed code and
   matched artwork. Change no other function or infrastructure setting. Record
   actual cold/warm START/REPORT evidence; do not assume a configuration change
   guarantees a particular execution environment.
3. Record upload-to-text, draft, Save, setup/AI/provider intervals and billed
   duration. Separately label actual browser first-edit timing if captured.
4. Restore exact original memory and read back code/configuration. No permanent
   promotion before user review. Stop on auth/permission denial; no IAM redesign.

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

No application or infrastructure change has been made in this checkpoint, so no
rollback is necessary. Current serving source and rollback remain in
[Pass A performance](pass-a-performance.md#active-write-editor-correction--deployed).
The memory table and final A/B/C decision remain pending actual measurements.
