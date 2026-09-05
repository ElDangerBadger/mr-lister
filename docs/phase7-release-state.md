# Phase 7 release state

## Decision

**PHASE 7 COMPLETE AND SEALED — FUNCTIONAL HACKATHON-DEMO SCOPE**

The Phase 7.18 backend is deployed in general-availability mode and its exact readback passed on
2026-09-05. The seller-web bundle's immutable S3 versions also passed canonical readback,
CloudFront invalidation completed, and public object GETs returned `200`. The authenticated
owner-scoped status route also returned `200` through the live seller UI. Release candidate
`022bdb62b6d7e4e8ac3c129e943f48e4256a6c5c` passed final `main` CI run `33985664447`; the
functional hackathon-demo seal is effective and no demo blocker remains.

The intended demo boundary is now implemented: an authenticated seller may explicitly confirm
publication of one exact approved job. Each job has one root attempt and at most one Printify
publication POST. Mr Lister still has no unpublish, product-delete, order, fulfillment, or custom
channel-status capability. The sealed Phase 6 preparation, draft, review, and approval runtime was
not changed by the Phase 7 deployment.

## Authoritative release identity

| Item | Authority |
| --- | --- |
| Runtime source baseline | `6e910934cf37ad4aab075dc08477916627bec334` |
| Deployment-evidence verifier checkpoint | `6d3ad48122c4858ed0ca2636f4a7bb30c3bccc5e` |
| Final release candidate commit | `022bdb62b6d7e4e8ac3c129e943f48e4256a6c5c` |
| Final green `main` CI | Run `33985664447` |
| Contract | `7.1.0`, SHA-256 `5172926cb89f8c046247922d8311c3f8b6361a9d67a719aa3a19a1c0ef1ed678` |
| Activation | `GENERAL_AVAILABILITY`; query, request, dispatcher, worker, recovery, and retention enabled |
| Phase 6 application binding | `0c6211a5b0244e9c86d635e6c02e7bc49e5e948d68895b4aaa982c0b0b2e187b` (unchanged) |
| Phase 6 stack authority | `mr-lister-phase6-dev`, last update `2026-09-05T07:02:32.182Z` before and after Phase 7 activation |
| Canary evidence | `d581000dd72ab5d73b01037baab9d78cab08cc9302edec66560a5b52b48fd34d` |
| General-availability approval | `e626f67557890e1603130f62520ef930574815214a42e0e10edfb83eb31bf68f` |
| Enabled runtime release | `b167db6f3dc5b8fef73c89959e0eff5ffdaee50b0739845232718456684cb130` |
| Enabled archive | SHA-256 `d12fa495c2b9ef428ef5d830535e031adadc86bc2df59b3cdd4bfabdc0136ad1`; S3 VersionId `x7kljmXkkD8cKQlZKsjGyQwGX38d66Tt` |
| Enabled template | SHA-256 `8966030f5f03b3b1da15bb15b1872323bca5042db39ace03e5443ae4ffe61517` |
| Backend stack | `mr-lister-phase7-dev`, `UPDATE_COMPLETE` at `2026-09-05T16:43:44.502Z` |
| Change set | `mr-lister-phase7-dev-enabled-b167db6f3dc5-r2` |
| Seller routes | Authenticated owner-scoped `GET /v1/jobs/{job_id}/publication` and `POST /v1/jobs/{job_id}/publish` |
| Prepared web release | source `6d3ad48122c4858ed0ca2636f4a7bb30c3bccc5e`; manifest aggregate `f3f624682796bc7e53c5328392ce33b5a1dae872dda776ee663e55d7224f20e1`; bundle `5592e1a958913268748bf57d453745c4bbddfd040f5c740614afcd096e95831d` |
| Web object versions | CSS `gzGmg7nYblKJ39tLa8__6CakgwMi3aG2`; JavaScript `ffv1spri_gFnSEwM94R6pjtHakciBpxv`; favicon `ksNFHHZN.zG3z9dIfvMjJzdxGSx1xq43`; index `1NJRgdIuUUhgZyxRwa3pkUWu1.5PWcmk` |
| Preserved runtime configuration | S3 VersionId `IdRmSDEqfGAjcfmiYHvjrUJ2SQ6s5wqx` |
| CloudFront activation | Invalidation `I2IL8HK2239SSHEA2WI7WK2L9C`, `Completed`; public release GETs returned `200` |

The backend verifier authenticated the versioned archive, stack outputs, all six Lambda
configurations and concurrency settings, role policies, mappings, EventBridge rules, bounded
workflow, JWT routes, and Lambda resource policies. Its before/after comparison proved that the
Phase 6 stack's exact stable authority tuple remained unchanged.

## Live publication evidence

The exact MassSkutiny canary used job `job_126b45d46bb560e8641a6e43f2a925d6` and Printify product
`6a9c3253a93f54bb45068670`. After explicit seller approval, the isolated concurrency-one runtime
issued exactly one publication POST. Provider GET readback and seller confirmation showed the
product live as Etsy listing `4569583958`:

`https://www.etsy.com/listing/4569583958/polygonal-llama-t-shirt-geometric-animal`

The external identity first appeared approximately two minutes after the canary's immutable
verification deadline. Its durable aggregate therefore remained `publication_verifying`, and the
strict P7.17 terminal verifier did **not** pass. The successful external publication is accepted as
the functional hackathon-demo canary; strict terminal evidence and the resulting live in-app
notification remain explicitly deferred. The canonical sanitized record is
[`phase717-live-publication-milestone.json`](evidence/phase717-live-publication-milestone.json).

On 2026-09-05, the signed-in seller opened that same job through the production web application.
The deployed owner-scoped `GET /v1/jobs/{job_id}/publication` returned `200` under contract
`7.1.0`. The projection safely reported `publication_verifying`, disabled another request with
`PUBLICATION_ALREADY_REQUESTED`, and issued no publication POST. This proves the generally
available authenticated read and polling path without creating a second external listing.

## Verification state

| Gate | State |
| --- | --- |
| P7.15C provider-free operations deployment and rollback | Passed for the functional demo scope |
| P7.16 deployed read-only validation | Passed |
| P7.17 exact one-listing external publication | Functionally passed; strict terminal verifier deferred |
| P7.18 enabled backend deployment/readback | Passed |
| Phase 6 non-delta | Passed |
| Local Python suite | 4,015 passed; 11 credential-gated live Bedrock tests skipped |
| Local web suite | 148 tests plus lint, typecheck, and production build passed |
| P7.18 web immutable-version readback and public activation | Passed |
| Isolated publish-once canary cleanup | Stack, function, and role deleted; 14-day log group retained |
| Authenticated post-GA publication-status read | Passed: owner job returned `200` under contract `7.1.0`; no POST |
| Final source/seal commit on `main` and GitHub CI | Passed: candidate `022bdb62b6d7e4e8ac3c129e943f48e4256a6c5c`; run `33985664447` green |

The prior baseline CI run `33977352912` is green at `6e910934`. Final `main` CI run `33985664447`
is green for release candidate `022bdb62b6d7e4e8ac3c129e943f48e4256a6c5c`.

## Rollback authority

The backend predecessor is the verified P7.15C production-disabled stack release
`9c4deca1813e5d1e8cc3f6747681b2194265f9c0b51b64fd9cf6b8afeb823c46`; its exact processed
template, stack parameters, and Lambda configurations were captured before activation. Backend
rollback returns seller publication, provider mutation, routes, triggers, and function concurrency
to their production-disabled state without changing Phase 6.

The versioned web bucket preserves the prior Phase 6 objects. The canonical web verifier recorded
the four candidate VersionIds above as the exact rollback set; only those candidate versions may
be removed to expose the preserved predecessors. `runtime-config.json` was not replaced.

## Seal result

No blockers remain for the functional hackathon-demo scope.

Same-ARN redrive timing, DLQ/alarm fault injection, recovery saturation, exhaustive live failure
matrices, strict late-terminal recovery evidence, manual screen-reader studies, and cosmetic polish
are post-demo hardening. They are not represented as passed and do not reopen the functional MVP.
