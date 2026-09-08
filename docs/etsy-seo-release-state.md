# Etsy SEO release — 2026-09-08

## Status

User authorized commit, push, merge to main, and deployment to `https://massskutiny.com`.
Source promotion, local verification, push and merge to main are complete. **Not yet deployed:**
AWS deployment is waiting on the existing bootstrap login/scoped permissions.
Release source: `e197eec2aa9820aad0862f07ab87bf907b24d3e2`.
GitHub clean-checkout verification: [release CI run](https://github.com/ElDangerBadger/mr-lister/actions/runs/34272912104)
(the linked run is authoritative for its current result).
The seller will run fresh live tests after deployment. Latency Stage 1 remains paused.

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
audit passed. GitHub CI will verify the merged source separately; no live inference or product
publication was performed for these checks.

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

- Current deployed predecessor: Stage 0 source `cbfcd3a4ee7ef15bc4f43cac2b6503bf7d9efaaf`;
  AgentCore immutable version 5, endpoint `phase6_v5_dev`.
- Predecessor preparation release:
  `3ae13d46db5e11731a5f69f541175d5c540cd93db823629d38cc91a108d790cc`.
- Preserve the exact predecessor stack template, parameters, per-function archives and runtime
  endpoint before deployment. Restore those bindings for a full operational rollback.
- Original production prompt fingerprint:
  `c5b2a76ebcc9fff8bd5363beb2db2d1651ad554fab21340a6a4cdb1a166ac96f`.
- Frozen v1 fingerprint:
  `d72948fe5a7ea155f6fa5283428ffcd26011e1e871342086ecf89e27398c56c2`.
- Update only AgentCore, PreparationDispatch, ReviewQueryApi and SellerCommandApi. No web asset
  deployment is needed for this backend change. Keep the working Phase 7 deployment unchanged.
- Locally built and sealed deployment candidate:
  `e7adb0e8709323af4a49a828635d4c74666471d9c7bf042718b88933355eb3ea`.
  Uses the existing locked Linux ARM64 dependency artifacts; nothing has been uploaded to AWS.
- Initial AWS preflight: dev identity verified; its previous temporary deployment permissions
  expired. Bootstrap login is required before the existing scoped deployment path can resume.

Deployed version and readback will be recorded here after the AWS deployment completes.
