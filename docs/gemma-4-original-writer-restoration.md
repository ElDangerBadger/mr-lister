# Gemma 4 original writer restoration

## Requested change

After the Gemma 4 release, the user reported plainer descriptions and tags and requested the
original listing-writing prompt. Harness v4 restores the editorial and SEO body of the released
`2026-09-08.2-etsy-seo-plain-preview-tag-diversity` prompt verbatim. This includes design hook,
voice, niche interests, buyer motivation, tag variety, useful description depth, and the original
restrained-copy and unsupported-claim rules. It does not restore the earlier flowery experiment.

Only the input preamble and data label adapt that body to the current evidence brief. The full
writer still views the artwork, treats inspection as provisional, and appends the unchanged v3
image-verification instructions. Inspection, schemas, deterministic tag selection, model settings,
source/image/request budgets, repair caps, and review/publication gates remain unchanged.
Older prompt revisions remain frozen. Account onboarding and judge removal remain later work.

| Component | Version | SHA-256 |
| --- | --- | --- |
| Inspection | `2026-10-04.3-evidence-brief` | `2cf84c8b8fde1add5affa0eb51ac67f72e1acc3658df5dc44c02773cfff5cba8` |
| Original writer, evidence only | `2026-10-04.4-original-writer` | `dcdd2e248371f5dbf9380d0e869332657f4270155f8c2d230c74aa8759a71f68` |
| Production image-aware harness | `2026-10-04.5-original-writer-harness` | `fef64b014379eceae1d8bd421010acef01105be4e85a2c75b584beb3a5ac536e` |

## Same-artwork comparison

The user's first live v3 job was retrieved read-only, including its original generated review and
exact versioned source image. Two full v4 trials used those original bytes, with no subject hints,
expected wording, original listing, or reviewer notes supplied to inference. Both produced
contract-valid editable drafts with no repairs, in 7.14 and 11.95 seconds of model-stage wall time.
These times do not measure end-to-end website preparation or establish a speed improvement.

The restored writer expanded the description from two factual sentences to four and added a
general audience sentence. Direct image review found no critical subject or product-claim error.
However, this sample **does not demonstrate better nuance**: the extra text mainly repeats visual
observations. Titles are longer and repeat retro/70s/vintage language. Both final tag sets lose
the exact slogan and distressed-design tag while repeating broad apparel phrases; one adds an
unmotivated gendered gift tag. Existing imprecise "bubble lettering" terminology persists.

This is a faithful restoration requested by the user, not a claim of superior search performance
or general editorial quality. Seller review is still necessary. No additional prompt tuning was
folded into this restoration. Existing saved listings were not regenerated or modified.

Initial comparison attempts failed before generation: the earlier temporary developer inference
grant had been removed, and requests were still rejected immediately after reattachment,
consistent with permission propagation. The failed runs were retained separately. A minimal
developer invocation subsequently succeeded, followed by the two
successful artwork trials. All inference used `mr-lister-dev`; bootstrap was used only for scoped
administration and readback. The one-hour model/region-limited test policy was detached/deleted,
and the exact prior developer policy baseline was verified restored.

Private source/results are under `.mr_lister_private/original-writer-20261004/` and
`.mr_lister_private/artwork-experiments/` (run `original-writer-truckin-v4-20261004` and its
`-b` and `-c` follow-ups). Raw outputs and artwork remain ignored by Git.

## Verification and release scope

All 280 focused tests passed across harness revisions, exact writer preservation, production
composition, sealed bundles, evaluator contracts and the historical v3 release planner. One
existing upstream Pydantic deprecation warning remains. Prompt pins reject stale v3 results;
uncertainty, disagreement, tainted repair and publication-boundary regressions are covered.

The historical `tools.prepare_gemma4_release` remains a v3-only renderer and must not render this
release. The v4 release requires a new plan based on actual runtime and Lambda readback, because
CloudFormation still intentionally records older deployment state. Never replay the historical
stack template to deploy this prompt.

## Deployment

The restoration was merged and pushed to `main` at
`574379ae9a181a5cb64f306329d104241272bbda`, then deployed October 4, 2026 (October 5 UTC).

| Release identity | Value |
| --- | --- |
| Component release | `bcae9f6dc9215ffb3c1ee342252bf6e6ed198288eb026eb7d8c6a499b3ca002f` |
| Runtime | `mr_lister_phase6-4HoPmq2hCI`, immutable version `10` |
| Preparation endpoint | `phase6_v10_dev` |
| Binding | `a8ee69aaf636d9171ee7452582f66af317d9331c5da898837680fd48f9eb442f` |
| AgentCore archive SHA-256 | `c5e13ae6b75ecb076c844b8a91bdec6f81a6e8d425a4bf308add96f6d29fd033` |
| Lambda archive SHA-256 | `5ddc1a8a3fc2e19624a26f5e66a3afb3a9e6021be53e4a6bb89ee17d130c3747` |
| Immediate rollback | `phase6_v9_dev`, immutable runtime `9` |

Only the preparation Lambda's sealed code/environment pair was switched. Query projection,
the other 15 functions, API, frontend, account, store, pricing, judge cleanup and publication
were outside the update. Two additive IAM policies grant only the exact new archive version
and exact v10 endpoint; existing policies remain required and preserved. The new runtime's
log retention is 14 days.

AWS's two-custom-endpoint quota required retiring unused `phase6_v8_dev`; its immutable runtime,
exact archive and endpoint recreation request remain retained. Fresh function/API checks found
no active v8 reference or running preparation workflow. Historical CloudFormation references
to v8 still exist: preserve the intentional drift and do not replay those templates. Runtime 9
and its unchanged endpoint remain ready for immediate rollback.

The Lambda code update completed with a different revision identifier from its initial accepted
response. The guard stopped before configuration rather than assuming ownership of an unknown
revision. Readback matched the accepted code response in every field except the AWS update-status
fields and revision identifier, with the original full environment and all other settings intact.
The matching configuration was then applied using the freshly read revision and verified. This
code/environment transition is not atomic and can briefly fail closed; the private receipt records
the recovery. No product, approval, publication or saved listing was modified by verification.

An IAM-authenticated empty request to the exact v10 endpoint returned the expected runtime HTTP
422 before job/model access. This confirms startup and envelope rejection, not positive deployed
inference. The two successful model comparisons ran under the developer identity. A fresh normal
website upload remains the final end-to-end confirmation of the deployed writer.

Independent postdeployment verification passed: preparation's exact archive hash and full
environment match the plan; all 15 other functions, both stacks/templates/resources, 18 API
routes, 18 integrations and the authorizer are unchanged. Baseline IAM is preserved with exactly
the two planned additive policies. Runtime 10 and its prompt pins match; runtime 9 and its
endpoint are unchanged; immutable v8 and all five candidate/rollback archives remain recoverable.
The homepage, judge page, health endpoint and both runtime-config endpoints returned HTTP 200.
Ruff and whitespace checks passed, and temporary developer inference access was removed.

The private plan, exact-version archive receipts, before/after captures, rollback requests and
verification evidence are in `.mr_lister_private/original-writer-20261004/` and remain ignored.
