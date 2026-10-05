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

The intended update is a new immutable AgentCore runtime and preparation Lambda's sealed
code/environment pair. Query projection and the other 15 functions, API, frontend, account,
store, pricing, judge cleanup and publication behavior remain unchanged. Current v9 is the
immediate rollback. Deployment/readback results will be recorded here after execution.
