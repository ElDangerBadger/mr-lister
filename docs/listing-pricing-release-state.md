# Listing pricing and shipping release — September 13, 2026

**Deployed and AWS readback verified** at [massskutiny.com](https://massskutiny.com/).
All ten Phase6 Lambda functions were verified at `2026-09-13T17:32:01.766705+00:00`.
The web bundle, six publication functions, and pinned AgentCore runtime were also verified.

Application source is `d8111804d34f90e07259f9505af3692136206627` on
`codex/listing-pricing-shipping`. The branch is pushed; [PR #6](https://github.com/ElDangerBadger/mr-lister/pull/6)
remains a draft. `main` remains `e515f4fbfec68f748cb6066bb8c9ab097c0d2c58`; this release
was not merged to main. Subsequent documentation-only commits record the deployment and
are not different application builds.

[Exact-source CI](https://github.com/ElDangerBadger/mr-lister/actions/runs/34769847768)
passed both backend and web jobs. Local validation included 375 web tests, the backend suite,
contract drift checks, Ruff, and the compiled Chromium/Firefox/WebKit browser matrix.
See [feature behavior and local validation](listing-pricing-and-shipping.md).

## Deployed components

| Component | Verified release |
| --- | --- |
| Phase6 Lambda + AgentCore source release | `6f12b4796e6fdda90de906949ca6059565551db96918ea642d602d35035c3eb7` |
| Phase6 Lambda archive SHA-256 | `bd360b3901401f913d35d12198737f3f6d5ccaebe22a2420caf3e6eab4538d41` |
| Phase6 Lambda S3 VersionId | `s0HQKMd9AvB9dC_DQ3hxHUficaLMYAsB` |
| AgentCore archive SHA-256 | `684a0a884b71abe9561aaeb441c7c91ee1106fe21cd45b98da24a19058bff667` |
| AgentCore archive S3 VersionId | `wIvq19hhaPvJkA9h_hSlJ_vKOG9OfOS7` |
| AgentCore runtime / endpoint | Version **7**, `phase6_v7_dev`, READY |
| AgentCore binding fingerprint | `edad5a422f9d85e5e496310982de98673b8e16ae32e6c9973c18994b2f5a8558` |
| Final Phase6 Original template SHA-256 | `a1966f2d857a44db042dcfa87ac13322d20d271467ae9966876dfc0b023b546e` |
| Phase7 enabled release | `e86bf1ba96c1c63f960445cf3ebdf0d40272c41ff34b17b412e7c2caa7862fcb` |
| Phase7 archive SHA-256 | `203173bea848ed1721040800361e91fb102d511d5d14cbc91b1f33dc46d8acf8` |
| Phase7 archive S3 VersionId | `RY.MlkA.s57QWMMKzCe0OYlcb5nNJ.ET` |
| Web release bundle fingerprint | `ef68a565f9a4191abd98404cd755c4a30f24626374a111b4ba1a850409dbe773` |
| Web index S3 VersionId | `wRiyvakTptD3.oOuE1P4zC0mwc_CbkHS` |
| CloudFront invalidation | `I4GGSJCM8AIQSH2BFCHYTOS3F0`, Completed |

The JavaScript asset is `assets/index-DUDqDeMJ.js`; CSS is
`assets/index-BcWhqQ2O.css`. The complete public asset bytes matched the reviewed manifest.
Runtime configuration remains at VersionId `IdRmSDEqfGAjcfmiYHvjrUJ2SQ6s5wqx` and SHA-256
`d1f969e5545ba76f76b8f6ef8e66def4d162ae74330a34d64796aebd7f25b5aa`.
An already-open browser tab must reload to receive the compatible new client.

## Scope and rollout

The compatible web went first, followed by all six publication consumers, seven core consumers,
and finally preparation plus the review/query and seller-command functions. No preparation
execution was active immediately before the final switch. Exact processed-template comparisons
proved the code/configuration scope; unchanged dynamic dependencies retained their identities.
The existing preparation invoke permission changed only from endpoint v6 to v7.

Preparation and provider memory remain **1024 MB**. Publication query timeout remains
**25 seconds**. Product-profile defaults, store binding, Cognito settings, web runtime
configuration, application identity, canary and enablement bindings were preserved.
All core functions now use the same verified archive, with explicit component release settings;
the global application release identity remains unchanged.

CloudFormation's Phase7 `UsePreviousTemplate` update materialized its prior processed template
as the new Original template. Both live Original and Processed now match that captured processed
template exactly, SHA-256 `c6dca3c00d6b9f863fcf51ca0e3a6ee120ebf494539bb7885d6f5ad2e536aefa`.
Future updates must start from fresh live templates, rather than assuming the old SAM Original
shape. AWS also advanced the six publication Lambdas' managed Python 3.12 runtime patch from
`8e339e4b8f1ed61cb2a625080dc0250e8ffb3832fe1cf18907c712cdf6de04b0` to
`86c3e2cbb9771ce0a3ed238525644193d48de840693b1533deee44d57a2c65c1`.
Their captured runtime-management mode is `Auto`; the exact patch pair and all other unchanged
settings are recorded separately.

The named-endpoint quota required retiring the unused **v5 endpoint**, after checking all
16 application functions and live stack bindings. Immutable runtime v5 and endpoint v6 remain;
the exact v5 endpoint recreation request is retained privately. The v7 READY response omits
`targetVersion`; verification matched its `liveVersion`, runtime/endpoint ARNs, requested
version, sealed runtime environment, and binding fingerprint directly.

The expired SEO artifact transition grant was replaced temporarily with exact candidate and
predecessor archive-version grants. After deployment, the permanent role binding was contracted
to the new archive and the temporary policy removed. No broad archive grant remains.

## Live verification and limits

Direct IAM handler checks used the documented administrative test convention, with real Cognito
subject/group metadata matched to an existing test job's owner. They did not create a JWT or
exercise an interactive seller sign-in.

- Health returned 200; unauthenticated review and publication requests returned 401.
- Owner-scoped review and publication GETs returned 200.
- The review projected exact legacy pricing defaults: $29.99, no variant overrides, free shipping.
- The entire existing job partition was identical before and after the checks.
- The public `https://massskutiny.com/v1/jobs` route also returned 401 without authentication.

These checks verify deployed handler initialization, ownership-bound reads, and pricing projection.
They do **not** verify a fresh price-save/Printify round trip or an interactive seller login.
No listing was edited or published during deployment. A fresh signed-in save, including variant
pricing and both shipping choices, remains the live functional follow-up; simulated provider
save/readback and publication checks passed before release.

## Rollback constraints and evidence

Once a pricing revision is saved, older strict backend readers cannot necessarily parse it.
Prefer a forward fix or a rollback that retains the new pricing readers. Never remove pricing
from fingerprinted persisted records. The preserved `main` checkpoint is not a blanket safe
backend rollback after new pricing data exists. The old web client also cannot parse the new
projection fields; retain the compatible client with the new backend.

Private receipts are under `.mr_lister_private/pricing-shipping-20260913/`, including:
`build-receipt.json`, `ci.json`, `web/verification.json`,
`phase7-readback-verification.json`, `phase7-runtime-management-verification.json`,
`phase6-consumers-verified.json`, `phase6-writers-verified.json`, `runtime-verified.json`,
`phase6-role-contracted.json`, `phase6-transition-removed.json`,
`readonly-smoke/verification.json`, and `public-api-auth-verification.json`.
Raw AWS snapshots, owner metadata, and rollback objects remain private and are not committed.
