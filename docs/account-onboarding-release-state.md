# Native account and Printify onboarding release

Released 2026-10-06 after approval of the account screenshots and the privacy/merchant
notices dated 2026-10-05. The support contact is `support@apartment-h-collective.com`.

## Live flow

Create account → verify email → set up authenticator MFA → connect a dedicated Printify
personal access token → explicitly choose a connected Etsy store → enter Mr. Lister.
Printify OAuth, additional product catalogs, account/store switching, and a placement
editor are separate milestones. The token connection is real, not the parked local
simulation.

The existing seller identity and judge session remain separate. Every identity present
before activation is reserved from native account provisioning. New accounts receive
only the `account` group until their own connection and selected store are validated.
Provider preparation and publication retain the original owner, connection, shop, and
binding epoch through retries and approval. No new account receives the owner's store.

## Deployment authority

AWS account `384627057108`, region `us-west-2`:

- Primary application: `mr-lister-phase6-dev`; all ten application binaries updated.
- Publication: `mr-lister-phase7-dev`; all six binaries updated so background workers
  can parse the same bound records as request handlers.
- Account services: `mr-lister-account-dev`; two handlers, retained account table,
  account group, one protected query route, and bounded Cognito invoke permission.
- Connection services: `mr-lister-connection-dev`; five handlers, retained indexed
  connection table, four protected routes, and bounded candidate cleanup.
- AgentCore runtime `mr_lister_phase6-4HoPmq2hCI`: immutable version `12`, endpoint
  `phase6_v12_dev`. Version `11` and `phase6_v11_dev` remain available for rollback.
  The unused version-10 endpoint was retired only after checking all live function
  references and running workflows; its recreation request is retained privately.
- Website distribution `EXC2KQ0RRVWF0`: the distribution configuration, existing
  API forwarding, and judge session origin are preserved. The shared SPA is updated;
  the judge runtime-config object is byte-identical to its predecessor.

Phase6 release fingerprint:
`a06d3c34546842cc16491df3fd59341f01d2000fe28d7c5f75306872b24b9ace`.
Publication enabled release fingerprint:
`e440083e5842850e4e7dc1fc5f853c3d5fb1bd351959553783bf27ed84a3c5b4`.
Website JS: `index-C9SQ-3vD.js`; CSS: `index-BXrTqfHu.css`.

The Gemma 4 configuration, all intelligence source files, and harness v6 prompt bytes
are identical to runtime 11. This release does not change model inference or listing
writing. The proven October 4 upload-tagging, untagged-version preservation, and
missing-execution recovery fixes are included.

## Important deployment drift

Do not redeploy the historical primary or publication templates wholesale. Their
recorded code/environment pins predate intentional live updates. Source preparation
uses a separate operational capture; the original templates are kept intact as evidence.

Account/connection additions are CloudFormation-managed. The existing primary stack
was changed only for the pool's signup and confirmation/pre-authentication controls.
The SPA function was updated directly with ETag guards: including it in the primary
change set would also update the distribution through a dynamic dependency and risk
replaying older routing. Function publication rotated CloudFront's ETag while leaving
the full distribution configuration identical.

Existing application functions use exact S3 archive versions and revision-guarded
code/full-environment updates. Eleven application functions receive connection
metadata; only provider workers receive access to the owned credential namespace.
Five other application workers receive compatible code/release pins without extra
connection permissions. The two judge functions are unchanged.

Existing IAM trust, boundaries, attached policies, and prior inline policies are
preserved. Distinct new policies provide only reviewed connection-table access,
provider-only credential reads, the exact runtime archive version, and its endpoint.
The temporary pool/routing deployment policy was removed; the CFN execution role's
prior permissions are restored exactly. Signup remains protected by required
authenticator MFA and both account hooks.

Private captures, checksums, immutable object versions, accepted revisions, change-set
reviews, and rollback requests are in the release worktree under
`.mr_lister_private/account-onboarding-release-20261006/`. Never commit those files,
credentials, OAuth state, or user information.

## Verification and remaining human acceptance

- Frontend: 603 tests, lint, typecheck, production build and build verification pass.
- Backend: 5,599 tests covered successfully; 11 opt-in live model tests skipped.
  The full run exposed one stale deterministic worker golden. Replacing only the
  preserved tagging module with its predecessor reproduced both former hashes;
  updating those two expected constants made the full ten-test module pass.
- All browser contract exports match their tracked artifacts.
- All 16 target configurations, all six publication handler seals, and the Phase6
  Lambda/AgentCore seals pass offline. Every intelligence byte matches runtime 11.
- Live Linux ARM64 startup probes pass for account/connection handlers and review
  query; unauthenticated requests return 401. Read-only synthetic missing-account
  probes exercise real table-read IAM and return the intended 404/403 refusals.
- Anonymous WebKit checks confirm the public notices render and Create account opens
  the native email/password signup form. Chrome was not used.
- Independent live AWS audit: 177/177 checks pass for deployed code/environments,
  original/new IAM capabilities, the preserved 18 API routes plus five protected new
  routes, authentication configuration, website/judge configuration, and runtime pins.

The remaining human check is a fresh native account's email verification, MFA setup,
explicit token/store connection, and a first draft. Passwords, authenticator secrets,
codes and personal tokens must be entered privately on the site. Infrastructure and
synthetic checks do not substitute for that end-to-end acceptance; no product was
created or published as part of the release probes.

## Rollback boundary

Close public signup and frontend feature flags first. Retain account/connection
records and encrypted credentials. Once a new account exists, keep account-aware
intake and provider binaries and the frozen legacy exclusions; reverting to old
unbound intake code could let a new identity use the legacy connection path.
Disable the connected workflow coherently to fail closed. Runtime 11 does not parse
modern bound records, so do not restore that preparation endpoint for bound jobs.
Restore only captured resources changed by this release, never old templates or an
arbitrary earlier commit.
