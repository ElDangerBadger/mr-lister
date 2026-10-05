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
exact prior developer permission baseline was verified restored. Sealed release preparation is
finishing; deployment readback will be recorded after execution.
The live preparation binding remains the prior v4/runtime 10 until the scoped rollout completes.
