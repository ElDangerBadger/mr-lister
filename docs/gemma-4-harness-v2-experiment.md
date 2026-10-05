# Gemma 4 harness refinement: experiment v2

## Predeclared plan

This follows the [first harness experiment](gemma-4-harness-experiments.md). The v1 prompt
bytes, versions and results remain available. V2 is opt-in and changes prompts only: keep the
same model, sampling, schema, source images, request budget, product context and repair rules.
The factory used by production does not select this experimental harness.

1. Compare the released writer with the v2 writer on the same assistant-reviewed facts for
   badger, maker motto, mountain/wave, owl/lantern, bloom motto and seahorse. Use one trial per
   arm initially. Both arms use the same output contract without an image. Check fidelity and
   usefulness, not simply shorter titles. Keep user editorial approval pending.
2. If writing is promising, run the image-derived v2 harness first on the known difficult cases,
   then the remaining existing fixtures. Review object identity, roles, relationships, salient
   omissions and uncertainty alongside the unchanged keyword metrics. No answers from the
   manifest or checked briefs enter the inspection prompt.
3. Repeat selected uncertain or inconsistent cases with the frozen prompt, recording every
   outcome. Do not select only the best outputs. Repetition is a consistency check, not proof of
   population accuracy. Stop qualification if material errors remain.
4. Only a satisfactory regression pass advances to fresh artwork and user writing review.
   Existing files named “holdout” are already observed regression cases. Fresh user artwork is
   optional input; do not substitute relabeled existing fixtures or claim independent validation.

## Desired change and retained criteria

- Identify meaningful subjects and objects using the whole composition, rather than letting
  shape inventories replace their meaning. Preserve uncertainty instead of guessing a role.
- Preserve a design's distinguishing detail or prominent legible wording in its title. Generic
  “graphic illustration” wording and padding are both undesirable.
- Lead descriptions with the design, then a few useful distinguishing details in natural prose.
  Borders, palette fragments and object-part inventories should not displace buyer-useful content.
- Prefer relevant search phrases over component colors or invented associations.
- The image-aware writer reports unresolved details and contradictory claims. Agreement is still
  a model report, not a factual certificate. Check its actual copy directly against the image.
- Material subject/lettering errors, invented product facts or authority violations still block
  acceptance. Evaluate omissions and role errors with the same severity for both arms. Never
  weaken an existing check because a new output fails it.

No live stores, products, publishing, deployment, judge retirement or billing change is in scope.
Use only bounded developer inference with temporary model/region-limited IAM and private artifacts;
remove the grant after testing. No commit or merge is part of this experiment.

## Results

### October 4, 2026: writing improves; semantic qualification remains open

The v1 defaults and prompt fingerprints are unchanged. V2 was frozen before inference:

| Component | Version | Fingerprint |
| --- | --- | --- |
| Evidence | `2026-10-04.2-evidence-brief` | `388d18ad8425fc8eea117d017f451899a632c85a273a079208ad58b5f0e4b14b` |
| Writer | `2026-10-04.2-evidence-writer` | `9b53772e1e5d250b7aeda89202c64475aeeedee5adea4993f9c02211b456d1d5` |
| Full | `2026-10-04.3-image-aware-harness` | `a3eda38011a97db764ae03259c0e560ae51392bde324a69763bd76ec620d2696` |

**Controlled writing:** all twelve calls produced valid drafts without repair. An independent
assistant reviewed anonymous A/B pairs against the originals before opening the reveal mapping.
It preferred v2 on five pairs and the released writer on bloom. The bloom v2 description placed
the two objects beside the text although they are below it. No major subject, wording, product
specification or authority errors were observed in this facts-fed comparison. This is one
assistant's editorial judgment, not user approval or a statistical preference estimate.

| Writer | Mean title length | Median model-stage time | Input / output tokens |
| --- | --- | --- | --- |
| Released template | 85.7 characters | 5.34 seconds | 9,990 / 2,243 |
| V2 | 55.3 characters | 4.03 seconds | 6,102 / 1,770 |

V2 better retains recognizable subjects and slogans while trimming boilerplate. Some tags remain
repetitive or lose useful concrete terms. These are same-model, same-facts writing comparisons;
they say nothing about recognition from an unseen image.

**Initial full regression:** eleven images, ten accepted-for-evaluation drafts and one valid
review-required result, with no invocation errors. There were 25 invocation attempts, including
four actual repair attempts. Input/output usage was 31,679/5,522 tokens. Median measured time was
6.79 seconds, with a 35.37-second owl outlier. Existing keyword checks passed eight of ten
completed drafts; wave and fox missed title specificity. The seahorse had no listing to score.
Neither eight keyword passes nor ten valid drafts is a semantic pass rate.

| Observed change | Interpretation |
| --- | --- |
| Mottos and wave/mountain imagery return to titles/copy | Better useful specificity than v1, although wave still misses the literal title rubric. |
| Owl listing now names its lantern | Better output; the brief's lantern/bag ambiguity vanishes without an explicit issue, so this does not prove the review gate worked. |
| Maker still describes tools as diagonal shapes | Meaningful recognition omission remains. The second call does not recover it. |
| Bloom brief still calls the red grip a head | The writer avoids that role claim in copy while reporting agreement; semantic verification still misses the inherited error. |
| Fox becomes generic animal | Lost specificity without an explicit review flag. |
| Seahorse stops for review | Safer than confidently labeling a bird; recognition itself is still unresolved. |

Two additional frozen-prompt trials each on owl, bloom and seahorse produced four drafts and two
review stops, ten calls, no repairs or errors, and 11,265/1,837 input/output tokens. Across all
three trials per case, the seahorse always stopped for review, the owl drafts named the lantern,
and the bloom drafts retained the motto and objects. This small consistency check does not
establish general reliability or make the remaining brief/verification problems disappear.

### Fresh user photo: explicit diagnostic, not promotion qualification

After prompt freeze, the user supplied a previously unseen JPEG and explicitly requested a test.
It was exercised despite the open regression gate as a separate diagnostic, not as evidence
that the whole candidate qualified. No expected subject or reference transcription was sent to
the model; the source filename was neutralized and the prompt was not tuned to this photo.

The original is 1,870,241 bytes, 2880 by 3840 pixels, SHA-256
`fa2a85e29aebb356b2c5356df7a645c28ef7efe0efad6a556eaeee7dceb3dcca`.
The separate evaluator preserves the exact JPEG bytes/hash/media type, validates the 5 MiB
ceiling and decoded image bounds, and lets the frozen harness derive its usual bounded inspection
PNG. This is labeled `direct_jpeg_diagnostic`: production's browser-normalized PNG input contract
was not changed or bypassed for live jobs. The photo and outputs stay private and ignored by Git.

All three trials produced usable drafts after small editorial corrections. They took 8.93,
14.25 and 8.67 seconds for the model stage, with two calls each and no repairs. The repeated runs
overlapped the regression-repeat process; timings are observations, not a cold/warm or end-to-end
benchmark. Combined usage was 7,347 input and 1,580 output tokens.

The model consistently identified the toilet-paper-roll patent illustration, printed S. Wheeler
name, number 465,588 and December 22, 1891 date. One representative raw title was:

> S. Wheeler 1891 Toilet Paper Roll Patent Drawing T-Shirt

Independent visual review found these caveats:

- Every brief confidently transcribed an uncertain handwritten witness name. That exact spelling
  could not be supported from the photo and should be omitted or marked uncertain. It did not
  enter the listing copy, which mentioned signatures generically.
- Copy added “original” or “official” to the printed patent number. Remove those authority words:
  the photo supports reporting its markings, not authenticating the document or its provenance.
- Two tag sets included “antique patent print.” Prefer phrasing clearly describing the shirt's
  imagery, rather than suggesting the sold item is an antique physical print.
- The copy correctly described artwork and did not promise the pictured frame, old paper,
  garment materials, fit, or manufacture. The T-shirt product type came from trusted application
  context; it is not inferred from the photo.

The repeated main-subject success is encouraging. The repeated uncertain handwriting and
authority phrasing also show why model agreement remains insufficient for approval.

### Eight additional user images: frozen fresh-input diagnostic

The user supplied eight more JPEGs after the prompt was frozen. Each was run once, sequentially,
with the unchanged v2 prompt, settings and image preprocessing. No expected labels or the
independent reviewers' observations were passed to inference. All outcomes were retained.
Two assistant reviewers recorded visual observations before seeing the outputs, then compared
their assigned four outputs to those observations. The user subsequently clarified the intended
subject of photo 6 and thematic relevance of UFO tags for the crop circles; the assessment below
incorporates that feedback. Other judgments remain assistant review, not blanket seller approval.
These are direct-JPEG model diagnostics, not browser/upload or commerce acceptance tests.

Seven images produced contract-valid drafts; one produced a valid review-required result.
There were 16 invocation attempts, including one tag-pool repair on photo 7, and no terminal
invocation failures. Input/output usage was 19,160/3,366 tokens. Completed drafts took 7.15–12.23
seconds (median 7.49); the review stop took 2.78 seconds. Provider latency was not reported;
these are measured model-stage wall times, not upload-to-listing times.

| Photo | Assistant assessment | Evidence |
| --- | --- | --- |
| 1 | Usable | Diamond perimeter, curved bands and field tracks retained. Tags repetitive; “agricultural field” avoids assuming grain species. |
| 2 | Usable with minor edits | Circular field geometry retained. Curved/scalloped sections are more precise than “spiral-like”; “mysterious” is unnecessary. |
| 3 | Usable; theme accepted by user | Star and grid retained. User confirms UFO is a relevant crop-circle theme. These thematic tags need not imply a visible spacecraft or make a claim about the formation's cause. |
| 4 | Excessive deferral | Accurately observes linked circles and field geometry, but stops over alternative explanations of its origin. A descriptive subject is available without resolving the cause. |
| 5 | Usable | SUBVERT, Uncle Sam imagery, pointing gesture, mask and circle-A preserved. No invented gun or affiliation. |
| 6 | Usable; subject accepted by user | User confirms the intended Dr. Zoidberg-esque caricature. Lab, coat, tentacled face and low-poly treatment capture that intent. “Octopus” remains an optional tag-precision edit, not a failed overall subject match. |
| 7 | Material omission; weak tags | Marching scene recognized, but gingerbread/cookie-like identity omitted. Seven of thirteen tags repeat sepia-related intent despite a tag-pool repair. |
| 8 | Usable | Orange retro car, front view, headlights and graphic style retained without guessing make/model/year. |

The initial assistant assessment grouped four drafts as usable, three as requiring material
corrections and one as an unnecessary stop. The user corrected two judgments: photo 3's UFO
theme is relevant, and photo 6's fictional-character interpretation matches the intended
caricature. After this feedback, six drafts are usable with ordinary or minor editing, photo 7
still has a material omission, and photo 4 unnecessarily stops. The original assessment is
retained in the private report history; no inference output was changed or rerun. This is an
editorial grouping for this small selected batch, not a general accuracy estimate. No material
OCR or physical-product claims were found in the final drafts. Self-verification still failed
to surface the gingerbread-like motif omission. The evaluation should distinguish reasonable
thematic/search associations from asserted visual or provenance facts, and account for a
seller's intended fictional subject rather than treating taxonomy as the sole criterion.

Runs are private under `.mr_lister_private/artwork-experiments/`, named
`harness-v2-eight-20261004-photo1` through `photo8`. A separate
`harness-v2-eight-20261004-review/index.html` contains original images, unchanged generated copy,
and clearly labeled assistant assessments plus the user's corrections. Raw trial files are
unchanged; this feedback is not blanket approval of every draft or deployment. Original JPEG
hashes, frozen prompt fingerprints and private permissions were
checked. The batch-specific temporary inference policy was detached and deleted; readback
returned no matching attachment and `NoSuchEntity` for the policy.

## Verification and boundaries

267 focused offline checks passed, including v1 preservation, v2 opt-in, matched A/B inputs,
private/blinded reports, request and repair limits, review routing, JPEG/source integrity and
production factory isolation. Ruff and `git diff --check` passed. One existing upstream
AgentCore/Pydantic deprecation warning remains.

The temporary Gemma-4-only Oregon inference policy was detached and deleted after all runs.
Readback confirmed no matching attachment and `NoSuchEntity` for the policy. Private directories
and files verified as 700/600. Original artwork remained unchanged. No production configuration,
store/product write, publishing, deployment, billing change, judge retirement, commit or merge
occurred. Work remains isolated and uncommitted on `codex/gemma-4-upgrade`.

## Next decision

Retain v2 as the more promising writing candidate, with user preference still pending. Do not
promote its visual interpretation or self-verification as proven. After the user's corrections,
the eight fresh images leave two clear targets: distinguish uncertainty about visible content
from uncertainty about origin, and preserve defining character/material motifs. Judge thematic
tags by relevant search intent while continuing to check factual claims in copy; do not forbid
reasonable associations solely because they are not literally pictured. Provenance wording and uncertain handwriting
also remain open from the earlier photo. A separate revision should address these general
failure classes, then face new held-out material as well as these retained regressions. Do not
inject these images' answers into prompts, relax existing checks, or quietly rerun until a
preferred result appears. No further prompt change or promotion was made for this batch.

## Files and reproduction

The experimental module is `src/mr_lister/intelligence/harness_candidate.py`. The fixture runner
is `tools/evaluate_harness_candidate.py`; the single-artwork runner is
`tools/evaluate_harness_artwork.py`. Tests are in `tests/test_harness_candidate.py`,
`tests/evaluation/test_harness_experiments.py` and `tests/evaluation/test_harness_artwork.py`.

Offline examples (no network/model calls):

```sh
PYTHONPATH=src:. python -m tools.evaluate_harness_candidate \
  --revision v2 --mode writer-ab --case illustrated_badger_subject --trials 1
PYTHONPATH=src:. python -m tools.evaluate_harness_artwork \
  --revision v2 --artwork /path/to/new-artwork.jpg --trials 1
```

Live execution requires the same explicit developer profile/environment gates documented in
[the first experiment](gemma-4-harness-experiments.md#review-and-rerun), `--live`, a unique run
identifier and a fresh reviewed narrow inference grant. The completed grant is no longer active.

Private fixture runs under `.mr_lister_private/harness-experiments/`:

- `harness-v2-writer-20261004-a`: twelve outputs, anonymous paired-review files, separate reveal
  mapping and the assistant's recorded pre-reveal preferences.
- `harness-v2-full-controls-20261004-a` and `harness-v2-full-remaining-20261004-a`: all eleven images.
- `harness-v2-full-repeat-20261004-a`: six additional outcomes; none discarded.

Private photo runs under `.mr_lister_private/artwork-experiments/`:

- `harness-v2-fresh-photo-20261004-a` and `harness-v2-fresh-photo-repeat-20261004-a`.

Raw outputs, original source copies and live/private captures must not be committed.
