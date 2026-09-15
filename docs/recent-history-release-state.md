# Recent history release — September 14, 2026

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
