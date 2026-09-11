# Approved UI and isolated evaluator release

The main application uses the Upload → Review → Publish workflow in the existing
React application and API clients. The first static release retained too much of the
original page layout; the seller identified that mismatch during live testing.
A local correction now follows the approved visual composition and awaits review.
Production activation and live judge access are separate release steps.

## Implemented

- The supplied Mr. Lister artwork replaces the initials in the header. The landing
  page leads to the existing hosted sign-in; invited users use their assigned account.
- Upload preserves the existing ordered batch, private transfer and recovery behavior.
  Completed transfers are described as preparation started, not finished listings.
- Review shows original artwork, all representative mockups, editable title,
  description and 13 tags, estimates, and expandable product settings.
- Milestones use section readiness and current synchronization version. No percent or
  ETA is invented. After 30 seconds of active preparation, conditional copy explains
  that an initial preparation may take longer; it does not claim cold-start detection.
- Activity starts collapsed and retains up to 20 distinct snapshots observed while
  this job is open. It is not a durable event history. Failure alerts remain visible.
- Saving, conflict recovery, image-load requirements, exact-version approval, and
  separate publication confirmation retain their existing server authority.
- The evaluator has a distinct, strictly disabled publication-status response. The
  same frontend shows the server's policy and offers no publication request.

Google/Apple sign-in and self-service store connection remain deferred. The seller
authorized using the existing connected Printify account for the first UI release and
real-store draft verification on September 11, 2026. A dedicated judge account/shop
will be provisioned later; no judge access has been granted. Credentials must not be
placed in browser configuration, source control, or judge instructions.

## Repository separation

The historical fake FastAPI entrypoint and synthetic AgentCore canary/builder live in
`tools/legacy/`. They are not the current application launch path. Historical evidence
and regression tests remain available. Shared domain code was retained after tracing
dependencies.

The production web build rejects imports of test, fixture, offline publication and
legacy/developer modules, including transitive and lazy imports. The existing Python
source-bundle allowlist continues to restrict deployment contents; the evaluator status
module is included in the query bundle without including the publication package.

## Evaluator setup

The eventual judge target is an independent environment: a dedicated invited owner, authentication,
state table, private artwork bucket, runtime and Printify shop/token. The Phase 7
publication stack is omitted. A second login to the production environment does not
provide this separation.

Two local tools prepare reviewable configuration without contacting AWS or Printify:

```sh
.venv/bin/python -m tools.prepare_evaluator_deployment \
  --production-identifiers /private/path/production-identifiers.json \
  --evaluator-identifiers /private/path/evaluator-identifiers.json \
  --output /private/path/evaluator-plan.json

.venv/bin/python -m tools.render_evaluator_publication_status \
  --production-identifiers /private/path/production-identifiers.json \
  --evaluator-identifiers /private/path/evaluator-identifiers.json \
  --output /private/path/evaluator-scaffold.json
```

Each identifier object contains exactly `environment_name`, `account_id`, `region`,
`application_origin`, `owner_id`, `printify_shop_id`, and `printify_secret_arn`.
The planner permits the evaluator owner/shop/secret fields to be `null` while unassigned.
The scaffold renderer requires all of them. Inputs contain identifiers, never tokens.
Production identifiers must be verified before relying on collision checks.

The tools reject production identifier reuse and unrecognized fields. Outputs are
private, create-only files. The scaffold pins the evaluator environment, origin,
secret reference and deployment target, and adds only the authenticated read-only
status route. Owner and shop binding must still be sealed into the evaluator runtime
and independently checked against the actual provider connection.

The output explicitly remains **not deployment-ready**. The existing release renderer
pins the original scaffold and does not activate this overlay. Different secret names
alone cannot establish that different credentials are stored in them.

## Release order

1. Release the approved UI through the existing static web release process, preserving
   the current backend and runtime configuration. Verify upload → edit → approval
   against the seller's existing connected Printify account, leaving the draft unpublished.
2. Later, designate a separate judge Printify shop and connection; assign the invited owner.
3. Seal the evaluator source/profile artifacts and exact runtime bindings, prepare
   its authentication/origin, and complete the reviewed activation path for the overlay.
4. Verify deployed route and IAM isolation, cross-owner denial, and a real upload →
   edit → approval flow against that shop. Confirm there is no publish command route.
5. Release the same UI with the evaluator's own runtime configuration and smoke-test it.
6. Record a separately authorized real-store publication for the submission video and
   clearly state that interactive judge access creates drafts but cannot publish.

The initial WIP implementation did not change a live environment, store permissions, or listings.
The local visual-check harness is temporary test data outside the repository and is
not included in the application build.

## Local verification, September 11, 2026

- Python regression suite: 4,215 passed, 11 live-AWS tests skipped by default.
- Frontend lint, typecheck, 237 tests, production build and build-content checks passed.
- Python lint/format and all three existing contract-drift checks passed.
- The generated evaluator scaffold passed SAM lint validation using synthetic identifiers.
- Source/wheel packaging passed; retired API entrypoints and developer directories are
  absent from the wheel. Production source-bundle closure tests passed.
- Browser visual checks covered desktop and 360/390-pixel widths, editable fields,
  disclosure state, preserved local text, approval blocking and confirmation, landing/sign-in,
  and the evaluator's disabled-publication message. Test data was used; no provider was called.

## Connected-account release preparation, September 11, 2026

Work continues on `pass-b-ui-evaluator`; `main` remains at
`9509d2d2520a24f2ebaff37aee897e976a2dda23`. The frontend candidate comes from
`53dbed13398c2fd0a1bdc033c499d05457054080`. The evaluator backend overlay is not part
of this static release and must not be enabled on the current publication backend.

The existing web release preparer now accepts the exact approved icon as an optional
fifth file, with immutable PNG headers and a pinned content digest. Historical
four-file manifests remain supported. Unexpected images, changed icon bytes, missing
references, source maps, and bundled runtime configuration are rejected. All 16
focused release tests passed.

The retained browser fixture was updated to use the current JSON artwork grant and
separate pinned-image download, and to assert the approved layout's absence of an
Approve control after approval. It verifies that seller credentials and referrers
never reach the artwork host. Application source was unchanged by these corrections.

The fresh-build compiled-app gate passed Chromium, Firefox, and WebKit at
`2026-09-11T17:40:54.380868+00:00`, with five files and bundle digest
`986ef5d784a7d6cd2b8d49a12d1ce9ede34a886025780c9539fa887ba2406086`.
It covers authentication/recovery, artwork loading, milestones, activity disclosure,
exact-version approval, stale-readback locking, polling isolation, narrow reflow,
forced colors, and browser-process recovery. The full frontend check also passed:
237 tests, lint, typecheck, and production build.

The versioned pre-Pass-B frontend and current runtime configuration were captured
for rollback, together with digests of both backend stacks and Lambda settings.
Private release material is under `.mr_lister_private/pass-b-ui-20260911/`.
The deployment script enforces the exact source, passing browser-bundle digest,
conditional versioned uploads with index last, unchanged runtime/backend settings,
and both current S3-version and public-byte readback.

### Static UI deployed and publicly verified

The five-file frontend release was deployed and publicly verified at
`2026-09-11T17:52:29.110475+00:00` on `https://massskutiny.com`.
Both backend stack/template digests and all captured Lambda configuration digests
remain unchanged. Runtime configuration remains at VersionId
`IdRmSDEqfGAjcfmiYHvjrUJ2SQ6s5wqx`, SHA-256
`d1f969e5545ba76f76b8f6ef8e66def4d162ae74330a34d64796aebd7f25b5aa`.

- Release-manifest bundle digest (upload order):
  `3ff10825757b373b4c70b52cc53ea6c0965ddde7257edfa90bef4f5e5b2ae1b5`.
- Index VersionId: `bfyE58QPAn0PtR7t4uK5Sq29JnJDLubY`.
- Completed CloudFront invalidation: `IZ2D0J9K3GH8MZDAXPG7Z3VCW`.
- Rollback index VersionId: `sfg2im9_yuRF4ZMFUyjV0fpEHVlDkDc0`.
  Its referenced hashed assets remain present. Restoring this index and invalidating
  `/` and `/index.html` restores the prior frontend without a backend rollback.

Every candidate's current S3 VersionId, checksum, headers and metadata were checked,
and public `/`, index, JavaScript, CSS, icon, favicon and runtime-configuration bytes
matched the sealed release. The browser-gate digest above uses sorted file order;
the release manifest uses upload order, so the two bundle digests differ by design.

The deployed landing page, signed-in Upload workspace, and existing hosted sign-in
recovery were observed. The 360 × 360 icon decoded successfully, and the live browser
reported no warnings or errors. The seller subsequently confirmed use of the branding
icon as product test artwork and began a fresh live test. Independent read-only draft
verification stopped at deployed-connection metadata; it did not read the job or call
the provider. A verified real-store result has therefore not yet been recorded.
Dedicated judge access remains deferred.

## Layout fidelity correction, September 11, 2026

The reference is the formally approved `mr-lister-layout-v1-branded.html` artifact
under the September 11 Codex visualization directory. The correction changes the
existing application, with a temporary local sample preview outside the repository.
The sample preview has no store connection and is excluded from the production build.

- Three equal-width workflow tabs with subtitles and an active underline.
- Four compact milestones with horizontal status rules.
- An artwork/mockup column beside a wider listing editor, in the approved 0.75:1.25
  proportions; product and pricing details are collapsed beneath the previews.
- A single contextual action bar before the collapsed Activity disclosure.
- A dedicated publication panel beside the artwork after approval, with the saved
  listing available in a read-only disclosure.
- The approved upload composition: artwork drop area, three-step guide, and a
  full-width preparation action bar.

The implementation retains private artwork loading, edit/save and conflict recovery,
exact-version approval, visible validation failures, and separate publication
confirmation. Desktop visual inspection compares the actual components with the exact
approved HTML reference. The compiled browser gate also checks tab widths, column
proportions, milestone geometry, section order and the action-bar Save button's native
form association, in addition to the existing authority and accessibility checks.

Final validation passed frontend lint, typecheck, all 237 tests, production build and
build-content checks. The corrected bundle passed Chromium, Firefox and WebKit at
`2026-09-11T18:30:51.880787+00:00`, with bundle digest
`68e88d0ddc91e4ebf8fd714dcc285e38b41bb2b8cfffa1c6dae699fce82324dc`.
Evidence is at `output/playwright/phase66/20260911T182856Z/browser-gate.json`.
All three engines passed the layout geometry and contextual action-bar checks,
360-pixel reflow with zero horizontal overflow, expanded Activity, reduced motion,
forced colors, exact-version approval and browser restart recovery. These checks used
local fixtures and made zero provider transport attempts.

This correction is local on `pass-b-ui-evaluator`. It has not been deployed while the
seller tests the current live version. `main` remains at `9509d2d`; the current branch
still contains uncommitted work and needs a reviewed release checkpoint before a new
deployment.

### Display preference addition

The seller approved the corrected layout and requested a compact Light / Dark / Auto
control. The header now provides a standard sun, moon or monitor icon with a three-option
menu. Auto is the default and follows system appearance changes. Only the display
preference is stored locally; blocked storage still permits switching for the current
page. The choice also synchronizes between tabs on the same origin.

Light preserves the approved palette. Dark adapts surfaces, controls, alerts, tables
and preview surrounds without filtering the supplied icon or product artwork.
Manual local browser checks covered Light after refresh, Dark selection with arrow
keys and Enter, Escape dismissal with focus restoration, and Auto following the system
appearance on Review and Upload. Frontend lint, typecheck, all 237 tests, production
build and build-content checks passed after this addition. The earlier three-engine
attestation identifies the pre-theme bundle; regenerate it for the next release.
The theme addition remains local with the other reviewed UI changes.

The seller subsequently approved and froze this design, including Light / Dark / Auto.
The final contrast adjustment applies only to the temporary local sample toolbar:
its navigation links retain dark text on the cream background in either theme. That
toolbar is outside the application and is not included in the production build.

### Frozen frontend release authorization

The seller authorized deployment of the frozen layout and display themes on September
11. The reviewed changes are being checkpointed on `pass-b-ui-evaluator` before the
release checks and static deployment. `main` remains the pre-Pass-B production
checkpoint at `9509d2d`. This release uses the existing application and connected
account; the dedicated evaluator deployment remains deferred.

The candidate review found no blockers in the UI, edit/approval safeguards, theme
control or release tooling. The five-file build contains no local sample toolbar,
fixture credentials, test job IDs or temporary preview paths. The 16 focused static
release tests pass. The current deployed index still matches the first Pass B release
at VersionId `bfyE58QPAn0PtR7t4uK5Sq29JnJDLubY`, which will be preserved for rollback.
Fresh checks and deployment evidence for the frozen checkpoint will be recorded below.
