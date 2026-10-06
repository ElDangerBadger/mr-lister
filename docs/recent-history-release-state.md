# Recent history release — September 14, 2026

## Operational repair — October 4, 2026

The upload/retention and publication-recovery fixes were deployed after this release.
The history API and all shared API routes remain unchanged.

- The upload API now signs S3 POST's `tagging` XML field. The previous
  `x-amz-tagging` form field left abandoned uploads without lifecycle tags.
  The compatible web bundle is `assets/index-u60MMuab.js`; it accepts both forms
  during rollout. Reload an already-open client before uploading.
- Source retention preserves empty-tag versions, reports their count, and continues
  its bounded sweep. Nonempty malformed tags still fail closed. A deployed sweep
  scanned 91 versions: 76 pinned, six released under the existing retention policy,
  and nine untagged versions preserved. The six old sources from cancelled/failed
  jobs were backed up locally with exact-version metadata and verified SHA-256
  before the sweep; no source objects were directly deleted by the repair.
- Scheduled publication recovery can settle an expired, strongly validated record
  when AWS explicitly returns `ExecutionDoesNotExist`. Other describe failures and
  malformed observations still fail closed. The September 5 canary settled to
  `publication_outcome_unknown`; recovery made no new provider call or workflow.

The changed Phase6 functions are `SourceVersionRetentionFunction` and
`UploadApiFunction`, using component release
`b553ae7a61866b4ea403b1d1fa2f470cd5c3e2b5f368004589c19225343f6931`
and archive SHA-256
`d2a837b88d8c666db5f028e0830e947b05527d535b6d0c044373890656e46371`.
Only `PublicationRecoveryFunction` changed in Phase7, using release
`4d3f2ad10c7cc2a7f63852dd82a601e64d95e472ecaee7602658e946982d8a02`
and archive SHA-256
`a3cc5a9738b0ca2dad2cb5b1a07035dc9738effd481da0d2e692e781f0713ee9`.
All other function code/configuration and both web runtime configurations were
verified unchanged. The web build used an isolated `c991603` source tree plus the
upload-contract fix, excluding unrelated local preview work. Backend archives
were sealed from the current source plus the repair; the source changes remain
uncommitted in the workspace.
AWS also advanced the three updated functions' managed Python runtime patch;
their existing `Auto` runtime-management mode was preserved and the exact before/
after runtime ARNs are retained in the private deployment receipts.

Phase7 used a scoped CloudFormation update. Phase6 used revision-guarded direct
Lambda updates to avoid reimporting the shared API, which previously removed
separately managed publication routes. **The two Phase6 functions intentionally
differ from CloudFormation in Code and `MR_LISTER_RELEASE_FINGERPRINT`.** Future
deployments must retain these fixes and reconcile those bindings without reimporting
an incomplete API definition. Do not blindly execute the saved reconciliation
templates or roll back to code that recreates these failures.

Private build/deployment/rollback receipts and the source backups are in
`.mr_lister_private/alarm-repair-20261004/`. The deployed retention result is
`retention-check-result.json`; Phase6's `phase6-repair/` records the intended drift
and exact rollback archives. A live 70-byte synthetic S3 POST verified the corrected
tag, checksum, and returned version; it is staged for existing lifecycle expiry.
Targeted backend suites (84 recovery, 29 composition/release, 231 upload/retention,
and 36 related smoke tests) passed, along with the three contract drift checks,
Ruff, and formatting checks. The isolated frontend passed 107 focused tests,
typechecking, lint, and production build. Public readback verified 14 objects;
the repaired upload Lambda initialized and returned 401 to an unauthenticated
request without creating an upload. Post-sweep S3 readback verified all 91 original
versions' content metadata and expected tags, with no delete markers.
The next scheduled retention run also reported zero errors. All three original
alarms returned automatically to `OK` on October 4 (Pacific): Phase6 Lambda errors
at 11:37:39, publication function errors at 11:41:12, and source retention errors
at 11:47:21. No alarm was disabled, reset manually, or given a higher threshold.

## September 14 release

The durable **Clear recent list** change is deployed to the seller and judge
application at https://massskutiny.com. Application source is merged `main`
commit `02048daeb626f052f740de24d839755dd78c8879` (PR #7).

Clearing recent history now saves an account-level cutoff. Older rows stay absent
across reloads and refreshed sessions; new uploads remain eligible to appear.
The action does not delete jobs, provider products, or publication/cleanup records.

## Deployed components

- `ReviewQueryApiFunction` and `SellerCommandApiFunction` use release fingerprint
  `a6edb81f95d16c5b785ee91c994e4b09b212e9f72e824bece40c86ae06fc4c16`.
- Lambda archive SHA-256:
  `991a007e3271f7d1b4460f6173c433e98097d699382e633a3210be81f28c442f`.
- `POST /v1/jobs/recent/clear` uses a standalone integration and route with the
  existing JWT authorizer, seller scope, and an exact Lambda invoke permission.
- The verified web bundle uses `assets/index-CLw8pdDH.js` and index SHA-256
  `45e121cd4e42df886111e35c43d74531bf0366bcb4cae1c5c07e9d5b1b57e9ee`.
- Both runtime configurations retain their exact prior bytes and S3 versions.
  The other 14 Phase 6/7 Lambda configurations and code remain unchanged.

## Verification

Before release, 4,778 Python tests and 514 frontend tests passed; 11 deliberately
opt-in live AWS tests were skipped. The exact production bundle passed a local
WebKit flow covering clear, reload, a fresh sign-in, and a subsequent new upload.
That browser test used a mock backend and is distinct from the live checks below.

The authenticated production check verified the dedicated judge identity, cleared
11 prior history entries, and read an empty list both immediately and after a
broker session refresh. A prior job remained accessible by its direct endpoint.
The check made no upload, product, approval, or publication changes.

Both stacks reached `UPDATE_COMPLETE`. All 17 pre-existing routes and integrations
were preserved by the final history release; the new history endpoint brings each
count to 18. Unauthenticated history-clear and publishing requests returned 401.
All 14 checked public pages/assets/configurations matched their expected hashes,
and CloudFront invalidation completed. The live judge landing page rendered in
the in-app browser. A new production upload or publication was not needed for
this release and was not performed.

## Deployment repair and future upgrades

The first attempt reimported the Phase 6 OpenAPI definition and rolled back after
a missing read permission for an existing CloudFront function. That import also
removed two routes managed separately by Phase 7: publication status and publish.

Those two routes and their integrations were restored through a narrowly reviewed
Phase 7 CloudFormation update, using the logical names
`PublicationQueryIntegrationRestored20260915`,
`PublicationQueryRouteRestored20260915`,
`PublicationRequestIntegrationRestored20260915`, and
`PublicationRequestRouteRestored20260915`. Their JWT authorization, scopes, Lambda
targets, and integration settings match their predecessors; they remain managed
by Phase 7. Other Phase 7 resources, settings, and outputs were unchanged.

The successful retry leaves the existing Phase 6 API definition and its SAM events
unchanged and adds `HistoryClearIntegration` and `HistoryClearRoute` separately.
AWS's reviewed retry plan contains no modification to `SellerHttpApi`.

**Do not reimport the Phase 6 API definition during an upgrade without explicitly
preserving routes owned outside that definition.** Future deployment candidates
must retain the current managed resource layout, including the restored Phase 7
logical names, and verify the complete route/integration inventory. A local
source-template change alone is not proof that an existing shared API can safely
be reimported.

Both attempts' temporary IAM grants were removed; the execution role's original
inline-policy inventory and contents were verified restored. The previous Lambda
archive and web index versions remain available for rollback. Rollback must also
preserve the shared API's external routes.

Private, credential-free release receipts and captured configuration are retained
under the ignored `.mr_lister_private/history-clear-20260914/` and
`.mr_lister_private/history-clear-v2-20260914/` directories. Invitations, cookies,
and access tokens are not included in this document or committed to Git.
