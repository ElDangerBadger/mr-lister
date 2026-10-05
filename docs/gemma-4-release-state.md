# Gemma 4 draft-generation release

## Release decision

The user accepted the single-subject artwork results as a useful editable-draft milestone and
authorized freeze, merge, push and deployment on October 4, 2026. This accepts the documented
limitations of model interpretation; it does not turn all evaluated outputs into semantic passes.

The release pins `google.gemma-4-31b` through AWS Mantle in `us-west-2`, with frozen harness v3
fingerprint `0fda08954b93aa25c8b3b8ff158cbba124bc8d37cb30e311a229c328be64e7b9`.
The active configuration is `config/bedrock/google_gemma_4_31b.json`, SHA-256
`820fde6058777abc7f17d54702e5898e7e6b0b668e98620fbe74476faa706ec3`.
Inspection retains the existing 1600-pixel/750,000-byte PNG budget and the full 3,500,000-byte
request ceiling. Original artwork and the 5 MiB source-upload contract are unchanged.

The production adapter runs evidence inspection and image-aware writing atomically, without
cross-request caches. Only a source/prompt-bound, schema-valid result without explicit reported
disagreement becomes a draft. Uncertain or rejected results use the existing preparation failure
path; they do not silently become accepted drafts. Application-owned review, approval and explicit
publication remain separate. No model result grants publication authority.

The existing analysis storage shape remains compatible. A specific internal marker identifies
the uncalibrated legacy confidence placeholder; the review projection hides that marker and
returns nullable confidence, preserving all other notes and old records. No frontend change is
needed. Exact lettering and ambiguous/complex visual details still require seller review.

## Deployment scope

The frozen source was merged and pushed to `main` at
`e7cb8f15068a5b8c0d9a9fc32c32169491204f1f`. The scoped AWS rollout completed on October 4, 2026
(October 5 UTC). Infrastructure readback and startup checks passed. A new authenticated
upload-through-review canary remains pending normal website sign-in; the available browser
session was signed out. Do not treat the startup check as proof of live model inference.

| Release identity | Verified value |
| --- | --- |
| Component release | `71a64fed4ec30cce992bffdc9c6ed227ac0c73a19f6a770da68d3abe6a5f1501` |
| AgentCore runtime | `mr_lister_phase6-4HoPmq2hCI`, immutable version `9` |
| Production preparation endpoint | `phase6_v9_dev` |
| Runtime binding | `6c2cb0075abbfd37433430b23a07c1e91cfc1aef3e6adb1f01834258e1da2555` |
| AgentCore archive SHA-256 | `db99325196cfa071c538db4dcc461e08d16bf3da80dccca5d32bfd8878b62433` |
| Lambda archive SHA-256 | `29b7993e4a727ddd4e92a9ded85784b1dc28d3db8b897e94ab941c2493a9a15a` |
| Retained rollback endpoint | `phase6_v8_dev`, immutable version `8` |

The complete offline suite passed: 5,045 tests, with 11 explicitly gated live-model tests skipped.
The subsequently completed deployment planner passed its 23 focused tests. Ruff and whitespace
checks passed. Existing dependency deprecation warnings remain; no dependency upgrade is included.

- Created a new immutable AgentCore version and custom endpoint; retained version 8 and its
  `phase6_v8_dev` endpoint as the current-compatible Gemma 3 rollback.
- Updated only `PreparationDispatchFunction` and `ReviewQueryApiFunction` with the new sealed
  Lambda archive/component release and preparation's exact runtime binding. A separate narrow
  invocation grant enables v9 while retaining v8 authorization.
- Kept Nova 2 Lite as controller, with narrow model/region-scoped Mantle inference permission.
- Preserved the actual code/environment of all other functions, including the separately deployed
  October 4 upload, retention and publication-recovery fixes. Their working-tree source changes
  are outside this branch. Preserve all 18 API routes/integrations and existing frontend assets.
- Kept judge access, cleanup timers, account setup, live store connections and pricing unchanged.

Fresh readback verified both target code hashes and complete environments, all 14 other functions
unchanged, all 18 routes and 18 integrations plus the API authorizer unchanged, and both stacks'
Original/Processed templates unchanged. Runtime v9 is READY with the exact config/prompt pins;
v8 remains READY. The new runtime log group retains logs for 14 days. Runtime and preparation
IAM changes are additive; pre-existing policy contents, trust policies and attached policies are
unchanged. The temporary CloudFormation archive grant was removed and its baseline rechecked.

The homepage, judge page, health endpoint and both runtime-config endpoints returned HTTP 200.
An IAM-authenticated empty-envelope request to v9 returned the expected runtime HTTP 422,
confirming startup and fail-closed request validation before any job access or model inference.
No canary product was created, approved or published during these deployment checks.

AWS refreshed the two updated functions' managed Python 3.12 patch under their existing runtime
management setting. Language version, architecture, memory, timeout and all other application
settings remained unchanged. This provider-managed patch is captured in the private receipt.

## Deployment method and intentional stack drift

The prepared CloudFormation change set was **not executed**. Its basic change list propagated the
query function update into `SellerHttpApi.Body` and state-machine dependencies. Reimporting the
shared API would add unnecessary risk to routes maintained by the publication stack. The change
set was deleted, and the release used exact, revision-guarded direct Lambda updates instead.
Query was updated and verified first, then preparation. Code and matching release environment
were applied consecutively, with exact rollback on failure; that transition is not atomic and may
briefly fail closed. Both pairs were verified successful after cutover.

Consequently, CloudFormation still records the prior prep/query code and v8 binding. This is
intentional drift, alongside separately deployed fixes elsewhere. Before any future stack update,
capture actual live function/environment state and incorporate this release without overwriting
those fixes. Never deploy the unchanged historical stack template to reconcile this drift.

AWS's custom-endpoint quota required retiring the unused `phase6_v7_dev` endpoint. Fresh stack and
16-function checks showed no references before retirement. The immutable v7 runtime and an exact
endpoint recreation request are retained; the active v8 rollback endpoint was never removed.

`tools.prepare_gemma4_release` renders the narrowly scoped plan from fresh captured live
Original/Processed templates and sealed, versioned artifacts. It performs no AWS calls. Historical
Gemma 3 deployment renderers are not authority for this release. The shared API must not be
reimported, and existing direct-update drift elsewhere must not be reconciled incidentally.

## Rollback

Restore the captured prep/query archives, complete environments and preparation permission with
the captured version 8 endpoint/binding. Keep the captured current runtime and exact archive
available until replacement has passed live preparation. Do not restore an arbitrary old main
commit or alter product, approval, publication, pricing, cleanup or account records.

Private release evidence is under `.mr_lister_private/gemma4-release-20261004/` and is ignored by
Git. Raw artwork, model outputs, credentials and AWS captures are not committed.
