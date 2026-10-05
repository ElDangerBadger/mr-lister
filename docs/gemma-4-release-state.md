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

At the source freeze, deployment verification is pending. This document is updated with actual
release identities only after readback succeeds.

The complete offline suite passed: 5,045 tests, with 11 explicitly gated live-model tests skipped.
The subsequently completed deployment planner passed its 23 focused tests. Ruff and whitespace
checks passed. Existing dependency deprecation warnings remain; no dependency upgrade is included.

- Create a new immutable AgentCore version and custom endpoint; retain version 8 and its
  `phase6_v8_dev` endpoint as the current-compatible Gemma 3 rollback.
- Update only `PreparationDispatchFunction` and `ReviewQueryApiFunction` with the new sealed
  Lambda archive/component release. Update preparation's exact runtime binding and permission.
- Keep Nova 2 Lite as controller, with narrow model/region-scoped Mantle inference permission.
- Preserve the actual code/environment of all other functions, including the separately deployed
  October 4 upload, retention and publication-recovery fixes. Their working-tree source changes
  are outside this branch. Preserve all 18 API routes/integrations and existing frontend assets.
- Keep judge access, cleanup timers, account setup, live store connections and pricing unchanged.

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
