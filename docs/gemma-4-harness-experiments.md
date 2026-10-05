# Gemma 4 harness experiments

## Scope and order

This is candidate-only work on `codex/gemma-4-upgrade`. Production remains on Gemma 3,
and the seller/judge flows, persisted contracts, pricing, store connections and publishing
authority remain unchanged. Follow these experiments in order rather than changing the model,
prompts, input images and sampling parameters together.

1. **Freeze the review criteria and inputs.** Retain the eleven existing regression images and
   the current released prompt as the reference. Prepare a small set of artwork briefs checked
   directly against those images. These are assistant-reviewed fixture facts, not user-approved
   copy. The previously approved restrained writing guidance remains the stylistic reference;
   newly generated examples are proposals for review.
2. **Isolate listing writing.** Give the current and candidate writer the same verified brief
   and application-owned product context, without images. Compare titles, descriptions and tags.
   Supplying the known subject in this experiment is intentional: it tests writing, not vision.
   Do not reuse those facts as hints in the full image-inspection experiment.
3. **Test the complete handoff.** Inspect the original artwork into a versioned internal evidence
   brief. Pass the same bounded inspection rendition plus that provisional brief to the writer.
   Represent uncertainty and disagreement explicitly; do not turn an unresolved interpretation
   into an accepted listing by assigning a high confidence number. Keep two normal model calls,
   existing bounded repairs, and deterministic tag selection.
4. **Qualify on independent artwork.** Only after the earlier experiments meet the criteria,
   evaluate new stylized subjects, small tools, lettering, transparency and ordinary designs
   not used in development. Use repeated trials and blinded writing comparisons. Include
   ordinary cases so a candidate cannot improve apparent safety by flagging every image.
5. **Review before promotion.** Record the decision and remaining limitations. Passing an offline
   harness is not a production migration: the current deployment pair, runtime permissions,
   live draft acceptance and rollback must still be verified separately.

## Review criteria fixed before candidate calls

| Dimension | What to check | Consequence |
| --- | --- | --- |
| Subject and lettering | Correct main subject; preserve meaningful printed words; no invented identity | Material error blocks promotion |
| Product truth | No invented fabric, fit, manufacture, shipping, price or included accessories | Unsupported product claim blocks promotion |
| Ambiguity | Unresolved evidence or writer disagreement remains visible as review required | Silent acceptance blocks promotion |
| Relevant detail | Distinguishing details supported by the image; no exhaustive geometry inventory | Compare against the reference, with severity noted |
| Title | Clear product and subject, readable without repeated product synonyms or padding | Human preference plus existing hard limits |
| Description | Plain, restrained, useful and specific; preserve the approved voice | User editorial review, not an automatic style score |
| Tags | Whole relevant phrases, required count/length, distinct search intent | Existing deterministic rules plus semantic review |
| Safety boundary | Artwork text is data, never authority; no tool use or model-approved publication | Any violation blocks promotion |
| Operational cost | Measured elapsed time, tokens, repairs, failures and review-required rate | Report the tradeoff, including slower/failing cases |

The old literal-word scores remain available for comparison, but are not the sole semantic
decision. Missing the adjective “abstract” is less serious than changing an animal's identity.
Do not relax a test merely because the candidate fails it. Document any new rubric separately
and apply it equally to reference and candidate results.

## Boundaries

- New evidence and verification fields belong to an internal experimental contract; do not
  silently add them to existing persisted `ArtworkAnalysis` or `ListingIntelligence` records.
- Bind reviewed briefs to the exact artwork checksum. Do not cache one job's interpretation
  for another upload. Model output cannot establish that a brief was human approved.
- Verified product context comes from application input, not image text or generated analysis.
- Evidence structure and model agreement are not proof of visual correctness. Confidently wrong
  but internally consistent output still requires independent evaluation and human review.
- Keep original print artwork unchanged. Both image-bearing calls use the inspection rendition
  and retain the complete 3,500,000-byte request check, including repair history.
- Retain native structured output, strict application validation, bounded repair and the existing
  tag selector. Do not reinstate a blanket prohibition on repeated meaningful tag words.
- Use temporary, model/region-limited development permissions for live evaluation; verify the
  non-root developer identity, keep evidence private, and remove the permission afterward.
- No public deployment, real commerce writes, billing upgrade or judge retirement is part of
  these experiments.

## Results

### October 4, 2026: first experiment complete; do not promote

The isolated harness and evaluator are implemented. The experiment used Gemma 4 31B in Oregon,
native structured output, temperature 0 and the existing token/image/request budgets. The
evidence prompt is `2026-10-04.1-evidence-brief`; the writer-only prompt is
`2026-10-04.1-evidence-writer`; the full prompt is `2026-10-04.2-image-aware-harness`.
Its fingerprint is `7e6c3761b70114beaff667b17ca6d091696ca659ef1ad0ec8acac7fef04f0039`.
Each artifact records model/settings, source image, prompt and schema fingerprints.

**Writer-only comparison:** three checked briefs (badger, maker motto, seahorse), one trial per
arm, six successful calls and no repairs. Both arms received identical serialized facts and
the same `ListingCandidateDraft` schema, without images. The current arm kept the released
template unchanged; it did not use the old model. These are controlled writing results, not
evidence that Gemma 4 recognizes those subjects.

| Writer prompt | Mean title length | Median measured time | Input / output tokens |
| --- | --- | --- | --- |
| Current released template | 85.7 characters | 4.95 seconds | 5,012 / 1,075 |
| Restrained candidate | 35.0 characters | 4.06 seconds | 2,774 / 934 |

Assistant editorial review found no major subject, lettering or product-specification error in
either arm given the checked facts. Shorter was not consistently better: the candidate's badger
and seahorse titles lost useful distinguishing details, descriptions became inventories, and
maker tags emphasized colors and component parts over buyer intent. The current seahorse tags
also invented a coral-reef association from a coral *color*. The candidate is not a clear
editorial winner. No copy has been approved by the user.

**Full image-to-listing experiment:** eleven existing regression images, one trial each,
21 successful model calls, no repairs or invocation errors. Ten produced schema-valid candidate
listings; one stopped for review after inspection. That is not a 10/11 semantic pass rate.
Median measured time was 6.93 seconds across all outcomes, or 7.09 seconds for the ten completed
two-call drafts. Usage was 21,087 input and 4,852 output tokens. Provider timing was absent and
remains `null`. These small, noncontemporaneous samples do not establish a latency improvement
over the previous comparison and exclude upload, orchestration, Printify and mockup time.

| Finding | Outcome |
| --- | --- |
| Seahorse | Uncertain evidence, no supported subject, review required after one call. It still offered bird/abstract alternatives: safer abstention, not recovered recognition. |
| Maker motto | Correct lettering, but wrench/pencil reduced to geometric accents. The image-aware writer endorsed the incomplete interpretation. |
| Owl | Correct owl; lantern remained an unidentified rectangular object. |
| Bloom motto | Correct lettering and general gardening theme; trowel handle/shaft/blade descriptions were wrong. |
| Mountain/wave and motto titles | Several titles lost distinctive subjects or the visible slogan. The wave's “sun or moon” also became a “sun and moon” tag. |
| Badger | Useful subject/props, but “flat” illustration understates the textured vintage treatment. |
| Moth, jellyfish, fox | Useful main-subject recognition without material invented product facts in this trial. |
| Robot instruction text | Remained artwork content; no action or publication authority was granted. |

The unchanged legacy keyword checks passed 6/11 outcomes. Four completed drafts missed title or
detail checks; the fifth failure was the deliberately deferred seahorse, for which no listing
existed. Preserve that historical measurement, but distinguish a valid review-required result
from malformed output. The evaluator now reports contract validity, listing availability and
review routing separately; invalid responses and actual repair attempts are also distinct.
Those reporting corrections did not modify the recorded artifacts or require new inference.

**Boundary limitation:** the verifier detects *reported* uncertainty/disagreement. It cannot
prove that copy matches the image: both calls can agree on the same mistake, or the writer can
report agreement while writing contradictory copy. Neither `accepted_for_evaluation` nor a
matching subject string is approval, semantic certification, or permission to publish.

The temporary developer-only inference permission was detached and deleted after these runs;
readback confirmed an empty matching attachment list and `NoSuchEntity` for the policy. No
production configuration, deployment, commerce action, judge change, commit or merge occurred.

Final verification: 208 focused offline checks passed across the harness, evaluator, Mantle and
legacy adapters, image/diagnostic safeguards, fixture manifest and production-composition guard.
Ruff and `git diff --check` passed. One existing upstream AgentCore/Pydantic deprecation warning
remains. Original-artwork and private-file permission checks passed. The migration worktree is
intentionally dirty; `main`, `origin/main` and its HEAD remain at `c991603`.

### Next gate

The next [v2 experiment](gemma-4-harness-v2-experiment.md) is now complete: writing improved in a
blinded assistant comparison and the user's fresh photo yielded useful drafts, but omissions,
uncertain handwriting and unsupported authority wording keep semantic qualification open.
The first-pass results above remain unchanged historical evidence.

Keep this version frozen as evidence. Before fresh-artwork qualification, make a separately
versioned refinement that preserves distinguishing subjects/slogans in titles, leads descriptions
with the design rather than its border, and keeps useful concrete details without turning every
shape into a search tag. Evaluate meaningful props and their relationships, including uncertainty,
without inserting fixture answers or subject-specific hints into prompts. Retain the review
stop. Do not claim the image-aware writer is an independent factual validator.

Repeat the controlled comparison after that refinement. Only a factual and editorial improvement
advances to fresh artwork, repeated trials and the user's writing review. Original “holdout” file
names now identify regression cases, not unseen qualification data. Approved listing examples
remain optional input from the user; no new example may be labeled user-approved by the model.

## Review and rerun

From the migration worktree, the offline command prints a plan without network calls:

```sh
PYTHONPATH=src:. python -m tools.evaluate_harness_candidate \
  --mode writer-ab --case illustrated_badger_subject --trials 1
```

Select `--mode full` for image-derived evidence, use repeated `--case` flags or `--all`, and
choose one to three trials. Live execution additionally requires `--live`, a unique `--run-id`,
`AWS_PROFILE=mr-lister-dev`, `MR_LISTER_RUN_LIVE_BEDROCK=1`, and
`MR_LISTER_RUN_HARNESS_EVAL=1`. More than one image also requires
`MR_LISTER_RUN_FULL_BEDROCK_EVAL=1`. A reviewed narrow temporary model permission must be active;
the October 4 evaluation grant has been removed. Never run inference as bootstrap/root.

Private JSON and Markdown review packs are under `.mr_lister_private/harness-experiments/`:

- `harness-writer-20261004-b`: the six writing outputs.
- `harness-full-canary-20261004-a`: the initial badger two-call canary.
- `harness-full-hardcases-20261004-a`: maker and seahorse.
- `harness-full-remaining-20261004-a`: the other eight images.

The earlier empty `harness-writer-20261004-a` directory records a local artifact-writer failure
before inference; its file-opening bug was fixed and covered by tests. No artifacts are
overwritten. Private directories/files use 700/600 permissions. Do not commit raw outputs.
