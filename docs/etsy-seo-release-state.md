# Etsy SEO release — 2026-09-08

## Status

User authorized commit, push, merge to main, and deployment to `https://massskutiny.com`.
**DEPLOYED AND READBACK VERIFIED** on 2026-09-08. Source promotion, local verification,
push, merge to main and AWS deployment are complete. Subsequent seller live testing reached
Etsy successfully on 2026-09-09, as recorded under the publication-dialog update below.
Release source: `e197eec2aa9820aad0862f07ab87bf907b24d3e2`.
GitHub clean-checkout verification: [release CI run](https://github.com/ElDangerBadger/mr-lister/actions/runs/34272912104)
passed, as did [merged release-record CI](https://github.com/ElDangerBadger/mr-lister/actions/runs/34273072203).
The checkout remains on main. The seller has started the bounded
[Pass A performance program](pass-a-performance.md): A1 timing is recorded and the A2
one-call candidate was benchmarked but is not promoted: its median gain was under
one second with modest quality loss. The deployed SEO
release described here remains unchanged.

The Pass A **early local editor** was subsequently deployed and public-readback
verified on 2026-09-09 Pacific, source `45bb547c5033e847edeab96c31e1166149ee41f1`.
The current web bundle/index, green source CI and rollback tuple are recorded in
[Pass A static web release](pass-a-performance.md#static-web-release--2026-09-09-pacific).
This supersedes only the web bundle below. All backend/AgentCore/provider and
publication runtime release tuples on this page remain unchanged; final Pass A
live timing acceptance is still pending.

## CI dependency maintenance — 2026-09-09

The CI-blocking high development-dependency finding is fixed by a lockfile-only update from
`js-yaml` 4.3.1 to **4.3.2**, within ESLint's existing `^4.3.0` transitive dependency range.
The version, registry URL and verified package integrity are the only lockfile changes.
[Maintainer advisory](https://github.com/nodeca/js-yaml/security/advisories/GHSA-2883-xcg3-v3hh).
Application dependencies, `package.json`, CI configuration and its existing
`npm audit --audit-level=high` threshold are unchanged.

Fresh `npm ci`, the high-severity audit gate, all **174 web tests**, lint, typecheck and build
passed locally. Production-only audit reports zero findings. Rebuilt HTML, JavaScript, CSS and
favicon hashes match the deployed publication-dialog release exactly; no site/AWS redeployment
is needed. GitHub runs the same checks on the pushed maintenance checkpoint.

Two moderate development-only entries remain for `vitest` and `@vitest/mocker`, from one
[Vitest advisory](https://github.com/vitest-dev/vitest/security/advisories/GHSA-82fw-gwwq-j7x9).
Addressing those requires a coordinated major test-runner update, deferred from this bounded
CI unblock. They are not production dependencies and do not fail the existing high-severity
gate; they are not represented as resolved. No audit bypass or forced package override was used.
The earlier failed CI runs recorded below are historical results, not four artwork-job failures.

## Safari preview correction — 2026-09-09

**DEPLOYED, READBACK VERIFIED AND CONFIRMED IN LIVE SAFARI.** Source checkpoint:
`1e882b1f5d1af33f3c43f1e61681282566a09401` on main. The preview API and web update are
live on `https://massskutiny.com`. After seller MFA, the same Safari job loaded the original
artwork and all five mockups, and `Approve draft` became enabled. Record version remains 8.
No approval or publication was issued. The memory update below is the rollback predecessor.

Safari's authenticated cross-origin redirect caused an S3 preflight for `Authorization`,
which the existing bucket correctly rejected. The web client now obtains a same-origin,
owner-authorized `?format=json` preview grant and separately downloads its exact-version PNG
without a seller token, cookies or referrer. Query selection uses the existing CloudFront
forwarding policy; `Accept` is not forwarded. Legacy no-query clients retain their 302 response.
Owner isolation, immutable source validation, five-minute presigning, no-store behavior,
image decoding and the artwork/mockup approval gate are unchanged. No S3 CORS change is needed.

- Verification: **179 focused Python tests and all 173 web tests passed**; Python/web lint,
  Python formatting, TypeScript, production build and all three contract drift checks passed.
  Independent review found no blocker. Isolated WebKit reproduced the original 403 preflight;
  the corrected request returned HTTP 200, decoded the PNG and sent no cross-origin seller
  authorization. The deployed macOS Safari seller-session check also passed as described above;
  a physical iOS-device check was not performed.
- [GitHub CI](https://github.com/ElDangerBadger/mr-lister/actions/runs/34393879268): the complete
  Python/infrastructure job passed (**4,102 tests passed, 11 skipped**, lint, format, contracts,
  all SAM validations and package build). Overall CI is **not green**: the web job stopped at
  its dependency audit before running web verification. The high `js-yaml` advisory is in the
  dev-only ESLint dependency tree, not the shipped bundle; production-only dependency audit
  is clean. Two additional moderate findings are dev-only Vitest tooling. The complete local
  web check passed. No dependency update or CI bypass was included in this deployment.
- Sealed candidate: `6c82db059f8bb8537e0bf3df77b93490c10f81223edfd4f71631e3e33204f91b`;
  Lambda archive SHA-256 `cc98c85d0c9baec28cdac07f21a6aa2a2363a4ec3c0a650978a24ba7abc9868e`.
  Compared with the live SEO archive, only `cloud/api.py`, `cloud/preview.py` and three
  release manifests differ; dependency bytes and archive membership are unchanged.
- Prepared artifacts: `.mr_lister_private/safari-preview-20260909/candidate2/phase6-artifacts/`
  and `.mr_lister_private/safari-preview-20260909/web-release.json`. The earlier candidate
  outside `candidate2` is superseded and must not be deployed.
- Lambda VersionId: `fqZ0rqACYOEv7OD8LBE_QpJE9cASXYUp`. The existing runtime-role bootstrap is
  `EXPANDED` to that exact candidate object/version while retaining the unchanged live SEO
  archive tuple for the other functions and rollback. No wildcard archive grant was added.
- Current core template SHA-256: `02593facd958693b1a27d432a3bfa305cb7b12dc1195da42665e106d185ab7a2`;
  VersionId `pAqjeHCNmLqZ8i9RtxVcq1lDwMYOv4OD` at
  `private/deployments/cloudformation/core/safari-preview/02593facd958693b1a27d432a3bfa305cb7b12dc1195da42665e106d185ab7a2/core-template.json`.
  SAM validation passed. Change set `mr-lister-phase6-dev-safari-preview-6c82db059f8b` contained
  one direct Query code/environment modification plus the API's unchanged dependent Query ARN
  reference; no replacements. Stack is `UPDATE_COMPLETE` and matches this template exactly.
- Query was deployed first. ProviderDraft (including 1,024 MB memory), PreparationDispatch and
  SellerCommand configuration readbacks equal their predecessors exactly; Phase 7 stack is
  unchanged. No AgentCore, provider-processing, prompt, approval or publication update occurred.
- Web bundle SHA-256: `e1602068255c0051e33385d254c6c7d0e34fa7fe92f227620454d751b403af83`;
  JavaScript `assets/index-B6yt4Lkt.js`, SHA-256
  `ad37c7096b26eb6d2a8fc6933c9916a899529f5531eb686b5db54ddb985eeb3c`.
  Assets were uploaded/read back with versioned checksums and index last; public bytes match.
  CloudFront invalidation `I15E2U3J39B0VLHNVXOPHUVEIE` is `Completed`, scoped to `/`, `/index.html`
  and `/favicon.svg`. Public runtime-config SHA remains
  `d1f969e5545ba76f76b8f6ef8e66def4d162ae74330a34d64796aebd7f25b5aa`.
- Rollback: restore the prior web index from the immutable versions captured in
  `.mr_lister_private/safari-preview-20260909/web-before.json`, then the memory template below
  with retained parameters if Query rollback is needed. Previous web assets were not deleted.
  Prior web `index.html` VersionId: `Tgr3ZaKNAxwq.JKde2FO0To.TFM4la97`; deployed version:
  `k0yGwzG1ybOW71XM9O990_2n9Ow9feXj` in `mr-lister-phase6-web-dev-384627057108-us-west-2`.
  Full local before/after, object-version and deployment evidence is in that private directory.
- Live seller check passed on `job_1026b34e63d035c99f3f1d378b0a6295`: Safari accessibility
  readback shows `Original uploaded artwork for this seller review`, all five mockups and
  `Approve draft` without the disabled flag. The unchanged image-load approval gate confirms
  the source and mockups loaded. No new upload, backend processing, approval or publication
  was triggered. Readback recorded at approximately `2026-09-09T19:36Z`.

iOS keyboard handling, progress-map UI and latency Stage 1 are outside this correction.

## Publication dialog layout — 2026-09-09

The seller reports that the anglerfish job completed approval and publication and is visible
in Etsy, confirming the fresh end-to-end seller run. This is seller-reported live evidence;
no additional publication request was made by the agent.

**DEPLOYED AND PUBLIC READBACK VERIFIED.** Source checkpoint:
`2779d23f543e470447f027636f19f32f29a0b979` on main. The publication acknowledgement checkbox inherited
the full-width text-input rule inside a flex label, compressing its text. A publication-only
label class now gives the checkbox fixed sizing and left/top alignment. Dialog wording,
approval authority, acknowledgement requirement, focus behavior and publication semantics
are unchanged. No provider, infrastructure, model or dependency change is included.

The actual React component was checked with local-only fixtures in WebKit at 320×640,
390×844, 1280×800 and 667×320: the dialog remains within the viewport without horizontal
overflow, the checkbox is 20 px wide, and publishing stays disabled until acknowledgement.
Visual phone-width inspection passed. All **174 web tests**, ESLint, TypeScript and production
build passed, including a new label-click/keyboard/cancellation regression. The checks passed
again before deployment. [Source CI](https://github.com/ElDangerBadger/mr-lister/actions/runs/34400778424)
passed Python/infrastructure verification; the web job stopped at the existing development-only
`js-yaml` high advisory, with two moderate Vitest findings also reported. Overall CI is not green;
no dependency update or audit bypass was introduced.

- Deployment readback completed at `2026-09-09T21:00:39Z`; only the existing static web bucket
  was updated, with checksummed/versioned assets first and `index.html` last.
- Bundle SHA-256: `9658c819375bc7cee389dffb37141c801405394c19ed2eb9d411be6166af4a09`.
  JavaScript: `assets/index-2ubZw1FB.js`; CSS: `assets/index-C4GjAhvG.css`.
- Public `/`, `index.html`, JavaScript, CSS and favicon hashes match the manifest exactly.
  Runtime-config SHA-256 remains `d1f969e5545ba76f76b8f6ef8e66def4d162ae74330a34d64796aebd7f25b5aa`,
  with unchanged S3 VersionId `IdRmSDEqfGAjcfmiYHvjrUJ2SQ6s5wqx`.
- CloudFront invalidation `I6IZTDF3GTLRO1PAWJMLCTCR3J` is `Completed`, scoped to `/`,
  `/index.html` and `/favicon.svg`. Deployed index VersionId: `vIxCATS8mvUjW0jPpwfemBuDrxccCXLL`.
- Immediate rollback: restore the Safari predecessor index, VersionId
  `k0yGwzG1ybOW71XM9O990_2n9Ow9feXj`, in `mr-lister-phase6-web-dev-384627057108-us-west-2`
  and invalidate the entry paths. Prior assets were retained; no runtime rollback is needed.
- Local versioned rollback files, manifest, upload results and public verification:
  `.mr_lister_private/publication-dialog-20260909/`. WebKit reproduction/screenshot:
  `output/playwright/publication-dialog/`. The post-deployment check verified public bytes;
  no seller approval or publication was triggered for this layout deployment.

Next: latency **Stage 1 only**, from current main, preserving this deployed source as rollback.
[Stage 0](latency-waterfall-v1.md) measured 45.053 s median to editable review, 40.978 s to
synchronized draft, four model calls (two controller plus two Gemma) and two Strands cycles.
SEO, image-envelope and provider-memory changes since then need a small current-version control
measurement using existing instrumentation before claiming savings. Stage 1 targets one normal
multimodal intelligence call plus at most one bounded repair, retaining Strands and reviewed SEO
copy/tag semantics. The old speed branch is an ancestor of main, not a newer implementation.
No Stage 1 code, provider caching, broader workflow simplification or progress-map work was
included in this deployment.

## Memory-only runtime update — 2026-09-09

**DEPLOYED AND READBACK VERIFIED.** A real 6,984 × 6,545 PNG exposed an out-of-memory
failure in `mr-lister-phase6-dev-provider-draft`. With seller authorization, the only template
change was `Resources.ProviderDraftFunction.Properties.MemorySize`: inherited 256 MB to
explicit **1,024 MB**. Global memory defaults, the 600-second timeout, code, environment,
roles, prompts, approval/publication behavior and all other resource definitions are unchanged.
The frozen foundation template is not a deployable replacement for this current live template.

- Memory-update predecessor template SHA-256: `f8c37351b50ac3dac5f6b9d5214ec93b3f1f5f2ea6590ea2d3e3028b04400704`.
- Versioned artifact: `private/deployments/cloudformation/core/provider-memory/f8c37351b50ac3dac5f6b9d5214ec93b3f1f5f2ea6590ea2d3e3028b04400704/core-template.json`
  in the existing Phase 6 artifact bucket; VersionId `ooLWxZWnhKqm86HbGSk1ipOXxwUlTrc_`.
- Change set: `mr-lister-phase6-dev-provider-memory-f8c37351b50a`;
  stack `UPDATE_COMPLETE` at `2026-09-09T18:28:15Z`. Only ProviderDraftFunction received
  resource update events; dependent ARN references remained unchanged, with no replacements.
- Provider code SHA-256 remains `IF8GhvHDJLkgixNrYcfWxrcPNGpqB7hqggx4FfZY+aA=`.
- Verification: exact one-property template diff, independent local review, SAM lint/validation,
  inspected standard change set, exact live configuration/template readback, unchanged Phase 7
  stack, and successful automatic recovery of the same seller job. No application/test code
  changed and the complete regression suites were not rerun for this configuration-only update.
- Job `job_c39cabdb911ee1040ba410fc3f1a4024` recovered its existing Printify image, synchronized
  draft `6aa1a552c31539ee0a0d1308`, and reached `awaiting_approval` at
  `2026-09-09T18:28:51.089823Z`, record version 60. No manual retry, new upload, approval or
  publication was issued. Successful Lambda durations were 13.24 s (reconciliation), 7.76 s
  (draft synchronization) and 11.82 s (economics); reported environment peak memory was 582 MB.
  This recovery is not a fresh-job latency benchmark or seller copy-quality acceptance.
- Evidence: `.mr_lister_private/provider-memory-20260909/`, including rollback captures,
  target template, change sets, live verification and recovery logs/events.
- Memory-only rollback: the exact 2026-09-08 template below, SHA-256 `1c5855d7e065cdb0b3354060fb3b8fd39a85b04752eaee69956f6f648941c7c0`,
  VersionId `64j9PfTaauSLn1nrhZD.CDbQOSJWYlaL`, with current parameters retained. Restoring
  256 MB would reintroduce the known failure for this artwork.

Known limits remain: generic crash reconciliation can bypass its intended deadline; this
memory-only update does not change retry semantics. Safari preview is addressed by the separate
correction above; iOS editing remains unresolved. Latency Stage 1 remains paused. The historical SEO deployment tuple
below is retained as the memory update's predecessor, not the current template identity.

## Live release and readback

- Stack `mr-lister-phase6-dev`: `UPDATE_COMPLETE`; update started `2026-09-08T21:44:39Z`.
- Release fingerprint: `e7adb0e8709323af4a49a828635d4c74666471d9c7bf042718b88933355eb3ea`.
- AgentCore: runtime `mr_lister_phase6-4HoPmq2hCI`, immutable version **6**, endpoint
  `phase6_v6_dev`, `READY` with `liveVersion=6`.
- Runtime binding: `c1ba05785a5105e94637e7a370008d79abff6278926bf074d4aab316670a6bfe`.
- AgentCore archive SHA-256: `a1ba0d053566f152f7a7b535f549472d60e585f57ef39e7ff260c94e6daa4024`;
  S3 VersionId `TnFrRt62SRfAkgPts27q0Bz0ShEjXB0c`.
- Lambda archive SHA-256: `0914055544d75a83f4cbb49fa2a403008e9277983a2a52b94b11a3e738f2b8b0`;
  S3 VersionId `58boyAlACxjC1cJHOZnfCDytodYGY_lE`.
- Exact archive deployed to PreparationDispatch, ReviewQueryApi and SellerCommandApi. Saved
  readbacks verify their code hashes and expected environment changes, with other configuration
  unchanged. AgentCore role, network, protocol, lifecycle and workload identity are unchanged.
- Original deployed template matches the reviewed template exactly, SHA-256
  `1c5855d7e065cdb0b3354060fb3b8fd39a85b04752eaee69956f6f648941c7c0`, S3 VersionId
  `64j9PfTaauSLn1nrhZD.CDbQOSJWYlaL`. Dependent ARN references required no resource replacements.
- Phase 7's complete stack readback equals its predecessor. No provider, approval, publication,
  existing store product or web asset was changed. The public site returned HTTP 200.
- Private operational evidence: `.mr_lister_private/seo-release-20260908/readback/verification.json`
  plus eight hashed AWS responses. Independent local comparison also passed. These are deployment
  readbacks, not evidence of a new end-to-end seller job; the seller owns that next test.

## Exact approved content

- Release prompt: `2026-09-08.2-etsy-seo-plain-preview-tag-diversity`.
- SHA-256 fingerprint: `c91e5ed73eaa62754b00ae335189298548a593fe5662e3445c049efbebab6cd3`.
- The preview identifier is intentionally retained to preserve the reviewed byte fingerprint.
- V1's subject interpretation, design hook, audience and title instructions are retained. The
  approved description section requests direct, grounded copy without invented stories, slogans
  or embellished gifting language. The tag section adds accurate subject diversification and
  permits useful shared words across distinct search intents.
- Final tags use `2026-09-08.phrase-coverage-1`: exactly 13 complete original phrases, at most
  20 characters, stable ranking, no word-stripping, no filler, one bounded listing repair.
- Phase 6 composition selects this bundle explicitly. Generic adapter defaults, the original
  production bundle and the frozen v1 reference remain unchanged.
- Gemma's inspection-only rendition uses the successfully tested maximum side of 1,600 pixels
  and 750,000 bytes. The larger previous rendition produced a live request-buffer rejection.
  Original print artwork, native aspect ratio and downstream Printify assets are unchanged.

No model/settings changes, added inference calls, one-call optimization, UI/provider changes or
publication changes are included. Exact-version approval and explicit publication remain required.

## Evidence and limits

Local release checks: **4,092 Python tests passed**, 11 explicitly gated live tests skipped;
**162 web tests passed**. Python/web lint, formatting, TypeScript, web production build, Python
sdist/wheel build, all three contract checks, all eleven CI SAM validations and the web dependency
audit passed. GitHub CI verified the merged source separately; no live inference or product
publication was performed during release verification or deployment.

The direct-description V2 prompt was run on real Gemma with the badger and llama images. The
llama inspection plus listing took 12.323 seconds across two successful calls without repair after
the smaller inspection envelope was applied. The final tag-diversity sentence was checked offline
but has not had a subsequent model run; seller live testing is the next quality check. Unit tests
pin the exact promoted prompt and verify its runtime selection and unchanged model settings.

The earlier eleven-fixture historical candidate-pool replay remains unavailable, as documented in
[the selector checkpoint](etsy-seo-tag-baseline.md); it is not an MVP blocker. Lexical selection
cannot guarantee semantic truth. One llama preview described the transparency inspection
checkerboard as artwork; seller review remains necessary. No further prompt cycle is included.

The seller confirmed old Mr. Lister jobs are disposable tests. No compatibility migration is
included: reviews whose stored validation disagrees with the new rules may be unavailable.
New listings use the new rules. Existing on-store products are outside scope and untouched.

## Rollback and deployment boundary

- Retained rollback predecessor: Stage 0 source `cbfcd3a4ee7ef15bc4f43cac2b6503bf7d9efaaf`;
  AgentCore immutable version 5, endpoint `phase6_v5_dev`.
- Predecessor preparation release:
  `3ae13d46db5e11731a5f69f541175d5c540cd93db823629d38cc91a108d790cc`.
- Exact predecessor stack template, parameters, per-function archives and runtime endpoint
  are preserved. Restore those bindings for a full operational rollback. The saved original
  template exceeds the inline CloudFormation size limit; use an exact versioned S3 template URL.
- Original production prompt fingerprint:
  `c5b2a76ebcc9fff8bd5363beb2db2d1651ad554fab21340a6a4cdb1a166ac96f`.
- Frozen v1 fingerprint:
  `d72948fe5a7ea155f6fa5283428ffcd26011e1e871342086ecf89e27398c56c2`.
- Deployment reused existing locked Linux ARM64 dependency artifacts. The existing runtime-role
  bootstrap stack is `UPDATE_COMPLETE` and `CONTRACTED` to the new exact Lambda archive.
- Temporary policy `MrListerSeoArtifactTransition20260908` on the deployment role now permits
  only the three exact predecessor archive versions, expiring `2026-09-09T20:40:28Z`. After that
  date, a rollback needs renewed exact-version read permission; the archived artifacts remain.
- With explicit seller approval, retired endpoint `phase6_v4_dev` was removed to free the AWS
  endpoint-quota slot. Runtime version 4 was not deleted and its endpoint can be recreated.
  The immediately preceding live/rollback endpoint `phase6_v5_dev` remains intact.
- AWS login refresh failures interrupted staging; no application/IAM redesign was introduced.
  The approved deployment resumed from preserved checkpoints after renewed bootstrap login.
