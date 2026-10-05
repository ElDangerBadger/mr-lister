# Gemma 4 tag coverage and length handling

## Change

The user requested implementation of shorter, more specific tag generation and improved
selection. The old internal candidate contract allowed 60 characters even though final tags
were limited to 20. Both baseline runs proposed a 21-character slogan/product phrase; the
selector discarded it and filled the final set with broader style searches.

The application now validates every candidate at 20 characters, including spaces and
punctuation. Mantle's native JSON schema also includes that bound for candidate strings.
Only this constraint is restored to Mantle's otherwise unchanged provider schema; inspection
and Converse retain their existing schema behavior. Application validation remains authoritative.

The shared schema translator previously removed string bounds. AWS's
[structured-output documentation](https://docs.aws.amazon.com/bedrock/latest/userguide/structured-output.html)
describes the restricted Bedrock schema subset. A separate direct Mantle capability canary
accepted `maxLength: 20` and returned 20 characters against a request for 30. The final artwork
tests exercised the same constraint on the actual listing and verification schemas. This
observation is specific to the tested Mantle endpoint/model, not a claim about all Bedrock APIs.

Prompt revision v6 replaces only the tag section of the restored v4 writer. It prioritizes
the defining phrase/subject and concrete details, asks for short complete phrases with headroom,
and discourages interchangeable aesthetic wording and unsupported recipient guesses. Fitting
exact slogans may use the full 20 characters. Title, description, evidence, system and image
verification instructions remain byte-identical to v4; generated prose can still vary.

Selection keeps the highest-ranked feasible anchor, then favors candidates adding specific
lexical coverage over generic apparel/style/decade variations. Existing hard eligibility and
redundancy rules are preserved. Shared words remain allowed. Ranked fallback produces a valid
set when new coverage is exhausted; phrases are never truncated, rewritten or invented. The
selected strings retain their original ranking. This heuristic is not a semantic search model.

Length-only repairs preserve validated non-tag copy and verification fields, fully revalidate
the repaired contract, and retain every reported disagreement. Feedback includes computed
character counts/overage by index without echoing raw tag text. There is still at most one
listing repair; no dedicated tag-generation call was added.

| Identity | Value |
| --- | --- |
| Production harness | `v6`, `2026-10-05.4-tag-length-harness` |
| Full prompt SHA-256 | `5e0e88750e43f8acbda97ff23cf9876dad34c47c1a37bc3d50cfcba062139b56` |
| Writer prompt SHA-256 | `8aff34e7be99641c86945e33cb21d0a91b135e47d4fa65f43cb21a113ef74153` |
| Evidence SHA-256 | unchanged `2cf84c8b8fde1add5affa0eb51ac67f72e1acc3658df5dc44c02773cfff5cba8` |
| Selection preference | `2026-10-05.specific-coverage-2` |

Gemma 4 model/settings, image/source/request budgets, repair caps and review/publication
boundaries retain their existing values. Older prompt bytes/fingerprints remain frozen.
Account onboarding and judge access are outside this change.

## Controlled comparisons

Each batch used two trials on the user's exact source image and one each on the existing
badger, maker-motto and moth fixtures. Full inspection/writing received no expected subjects,
reviewer judgments or baseline listings. The v4 baseline imported source from the verified prior
sealed source bundle, preserving its old schema and selector. These are known regression cases.

| Stage | Accepted / trials | Listing repair episodes | Outcome |
| --- | --- | --- | --- |
| Frozen v4 baseline | 5 / 5 | 0 | Long candidates silently filtered; slogan lost in both user-image trials |
| v5, strict local length + coverage | 4 / 5 | 3 | One user-image trial still exceeded length after repair; not promoted |
| v6 length headroom + precise repair feedback | 4 / 5 | 1 | Maker trial still exceeded length after repair; not promoted alone |
| v6 with native Mantle bound | 5 / 5 | 0 | All final tags within 20; defining phrases retained |

The native stage used ten model invocations, plus a prior tiny capability canary. Across all
stages, 45 invocation attempts stayed within the declared 50-attempt ceiling. Every failed
run was retained. No unsuccessful trial was replaced with a selected best rerun; native
schema enforcement was a separately declared reliability correction after observed failures.

Both final user-image trials retained the exact slogan, and the maker trial retained its motto.
Distressed/weathered and other concrete details gained coverage in some cases. Broad style
repetition remains, and gains are not uniform: the badger's final selected tags omit the compass,
and moth themes overlap. This is not evidence of improved search ranking or sales.

Five accepted contracts are not five semantic passes. Independent review flagged the maker's
stylized tool reading: its pencil became a "teal arrow" and the other motif remained "key-like."
Those recognition weaknesses already appeared in earlier evidence; here they reach the copy.
The badger's depicted-jacket tag can imply an unintended product search, and "gold foil style"
in the moth tags needs seller judgment. The accepted milestone remains an editable draft with
explicit human review. No automatic approval/publication or claimed factual-certainty gate was
introduced, and the sample's existing recognition limitations were not patched with answers.

Raw artwork, outputs, prompts, diagnostics and before/after deployment evidence remain private
under `.mr_lister_private/tag-coverage-20261005/` and the named `tag-v4-*`, `tag-v5-*`, and
`tag-v6-*` experiment directories. Evaluator fingerprints now record the actual Mantle schema,
selection version and selection/policy source hashes in addition to existing evidence.

## Release state

Implementation and model comparisons are complete. The full regression run reported 5,202
passes, 11 gated live-model skips and two compatibility failures. The publication source-hash
fixture was updated only after independently verifying its byte delta; the experimental unified
path was corrected to preserve original analysis/copy across length-only repair. The relevant
292 and 10 targeted checks then passed, along with lint and whitespace checks. No publication
worker or experimental unified path is activated by this rollout.

Bootstrap authentication was renewed, the temporary test policy was detached/deleted, and the
exact prior developer permission baseline was verified restored. Source was merged/pushed to
`main` at `8f1985111363536a2f0305e2fde83d41d51ec5ed` and deployed October 5, 2026.

| Release identity | Verified value |
| --- | --- |
| Sealed component release | `4cb9cae8f4ae708a2be753ad48a445a9e44a423e3bb4f9b75e52cb3aa86b913f` |
| AgentCore runtime | `mr_lister_phase6-4HoPmq2hCI`, immutable version `11` |
| Preparation endpoint | `phase6_v11_dev` |
| Runtime binding | `be4cf5ebdb7d2d7b4bb10da7210f4211889eec3563029aa7cd5b7695927c2c57` |
| AgentCore archive SHA-256 | `3ff9fc2f938d95edbd9f87fb6ec4be6ed5da1f44438804665c29261cd98721d1` |
| Lambda archive SHA-256 | `f7ba60c7436c70211e0a3376c5ddd7777efbdc2610b2f05f6732679425f98b6a` |
| Immediate rollback | Immutable runtime `10`, unchanged `phase6_v10_dev` endpoint |

The rollout updated only preparation's sealed code/full environment pair and added exact-version
archive and exact-endpoint IAM permissions. Current runtime 10 remains ready for rollback.
AWS's two-custom-endpoint quota required retiring unused v9's endpoint; immutable v9, its exact
archive and recreation request remain retained. Stale CloudFormation references to v8 still
exist: do not reconcile the historical stack templates as part of this rollout.

Runtime 11 matched the plan and rejected an empty envelope with the expected HTTP 422 before
job/model access. Its log retention is 14 days. Cutover guards compared full accepted/completed
Lambda state while allowing AWS's completion revision/status transition, then used the freshly
read revision for the next update. The code/environment switch is not atomic and can briefly
fail closed. Exact captured runtime 10 code/environment remains the guarded rollback.

These checks verify deployed startup and binding, not a positive authenticated website job.
A fresh normal upload is the final end-to-end confirmation. No listing was created, approved,
published or rewritten by deployment verification; saved drafts retain their existing copy/tags.

Independent postdeployment readback passed without relaxed checks: preparation's exact code/full
environment, runtime 11/prompt pins, all 15 other functions, both stacks/templates, API routes,
integrations and authorizer, and every baseline IAM policy match their expected states. Only the
two planned narrow IAM policies were added. Runtime 10 and its endpoint are unchanged; all five
new/rollback archives and immutable v9 remain recoverable. Startup/log-retention receipts match
the plan. Home, judge, health and both runtime-config endpoints returned HTTP 200. The private
receipt is `.mr_lister_private/tag-coverage-20261005/verification-v6.json`.
