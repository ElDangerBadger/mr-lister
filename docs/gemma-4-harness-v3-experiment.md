# Gemma 4 harness refinement: experiment v3

## Scope and predeclared comparison

This prompt-only revision follows the user's corrections in the
[v2 review](gemma-4-harness-v2-experiment.md): a fictional laboratory caricature was an
acceptable subject interpretation, and UFO tags were relevant thematic associations for
crop-circle imagery. The remaining clear issues were a lost defining character motif and a
review stop caused by uncertainty about origin rather than uncertainty about visible content.

V3 instructs inspection to preserve defining depicted texture, material-like appearance,
character treatment and relationships. It distinguishes visual ambiguity from unknown creator,
cause or symbolic meaning. Writing retains relevant thematic searches without converting them
to factual provenance claims. Depicted appearance remains separate from shirt specifications.
Unclear handwriting must not be completed or authenticated.

The prompts contain no image-specific answers, filenames, character names or fixture labels.
V1 and v2 prompt bytes/fingerprints remain frozen. Schemas, settings, model, image preparation,
source binding, review gates, repair budgets and production composition remain unchanged.
An unresolved brief or writer disagreement still stops; this is not a postprocessing override.
V3 is explicit opt-in. Existing defaults stay v1 for the adapter/fixture CLI and v2 for the
independent-artwork CLI. No merge, deployment, account/store changes or commerce calls.

### Frozen identity

| Component | Version | SHA-256 fingerprint |
| --- | --- | --- |
| Evidence | `2026-10-04.3-evidence-brief` | `2cf84c8b8fde1add5affa0eb51ac67f72e1acc3658df5dc44c02773cfff5cba8` |
| Writer | `2026-10-04.3-evidence-writer` | `d489ffaaf9b05d592c4e353ce628434187d5e44f113ee5568442375b7ba56603` |
| Full | `2026-10-04.4-image-aware-harness` | `0fda08954b93aa25c8b3b8ff158cbba124bc8d37cb30e311a229c328be64e7b9` |

Harness source SHA-256 at freeze:
`09e2c8669f92b91c0b3812456e44b763604e9711b3ec17781d048445898ffe09`.

### Run plan, declared before model calls

1. Run the eight user photos once each, plus the earlier handwritten/printed patent photo once.
2. Run all eleven existing fixtures once each using full image inspection and writing.
3. Preserve every outcome, including errors, review stops, raw copy and self-verification claims.
   Compare with already recorded frozen v2 outputs. Do not select a best rerun or tune v3 after
   observing its results. No labels or reviewer notes enter inference.
4. Assess defining motif retention, justified uncertainty, irrelevant or unsupported claims,
   lettering, product truth and usable search intent. Existing keyword metrics stay unchanged
   and are not semantic correctness scores. A newly resolved subject may pass if accurate;
   preserving an old review stop is not the target.
5. These twenty images have all been observed previously and are regression cases, not unseen
   validation. Earlier user feedback informs evaluation, not model context. A future fresh
   batch and seller editorial review remain necessary before promotion.

Twenty trials allow at most 100 invocation attempts under the existing per-operation repair
caps. Use the non-root development identity with a temporary region/model-scoped grant,
expiration and verified removal. Keep private originals/results ignored by Git. The same
bounded source/inspection image and serialized request limits remain in effect.

## Results: useful change, but not a promotion candidate

All twenty single-trial runs completed with contract-valid drafts, forty invocation attempts,
zero repairs and no API failures. There were no review stops. That is not a semantic pass:
direct image review found material errors that the model's self-verification accepted.

| Group | Trials | Input / output tokens | Model-stage seconds, min / median / max |
| --- | --- | --- | --- |
| User photos, including the earlier patent | 9 | 23,738 / 3,773 | 6.91 / 7.59 / 8.95 |
| Existing fixtures | 11 | 29,060 / 4,419 | 4.95 / 6.53 / 7.67 |
| Total | 20 | 52,798 / 8,192 | 4.95 / 6.96 / 8.95 |

These are measured model-stage wall times, not provider latency or end-to-end application
latency. The comparison uses saved initial v2 trials, not concurrent randomized runs; timing
differences do not establish a general performance improvement. Sources and prompts were not
changed between cases and no result was rerun or discarded.

### Main observations against v2

- The linked-circle field photo now yields a usable descriptive listing instead of deferring
  over alternative origin explanations. It retains circles, connecting paths, field and layout.
- Crop-circle copy stays descriptive while tags include relevant UFO/paranormal interests.
  The user confirmed this theme is appropriate. Many tags omit apparel terms in their final
  selection: a search-intent consideration, not a demonstrated search-ranking regression.
- The laboratory caricature keeps the user-approved interpretation, explicitly describes its
  claws, and drops the narrower octopus tag. This is a modest improvement.
- The marching figures still lose their gingerbread-like motif. V3 now commits to stone/clay
  appearance and incorrectly says they march toward the distant rock formation; they approach
  the viewer. More detail amplified an incorrect interpretation rather than fixing recognition.
- The previously deferred seahorse fixture is now confidently labeled a bird in its title,
  copy and tags, with writer agreement and no issues. This is a major regression. A lower
  deferral rate must not be treated as an improvement in isolation.
- The maker fixture still loses a recognizable tool motif. The owl becomes a generic nocturnal
  character even though its lantern survives. Both remain specificity problems.
- Several already-good cases stay usable. The moth retains more of its depicted wing/pearl-like
  treatment, bloom identifies the tool's handle/blade correctly, and the fox regains a specific
  subject. Fox copy slightly overstates its gaze through the telescope. Improvements do not
  cancel the errors.
- The car adds an unsupported coupe body style despite only a front view. The earlier patent
  still yields uncertain cursive as a confident transcription in its internal brief, and final
  copy says “original patent labels and signatures.” Neither issue is solved by the prompt.

The unchanged legacy keyword checks pass seven of eleven fixture drafts. Owl, wave, jellyfish
and seahorse fail one or more keyword criteria. These are supporting signals only: the maker
passes those checks despite the observed omission. Do not combine these scores into a claim
that seven images were semantically correct.

### Decision

Retain v3 as an explicit experimental revision and v2 as the prior comparison; promote neither
from this result. The origin-versus-visible-content distinction is useful, but this combined
prompt does not meet the desired recognition/review behavior. Do not quietly relax checks or
add the known answers to prompts. The next isolated experiment should target visual evidence
quality/interpretation and measure both correct specificity and justified review stops, rather
than adding more copy-writing instructions. Fresh held-out artwork and the user's editorial
judgment remain needed; all twenty images here are known regressions.

## Verification and artifacts

238 focused offline checks passed across the harness, both evaluators, Mantle adapter, Gemma 4
candidate and production composition boundaries. Ruff and `git diff --check` passed. One existing
AgentCore/Pydantic deprecation warning remains. These tests verify software boundaries and
revision routing, not a model's visual recognition. V1/v2 fingerprints and production factory
behavior are preserved; schemas, request/repair budgets and source images are unchanged.

Private runs under `.mr_lister_private/`:

- `artwork-experiments/harness-v3-20261004-photo1` through `photo9` retain all user-photo results.
- `harness-experiments/harness-v3-full-20261004-a` retains all eleven fixture results.
- `artwork-experiments/harness-v3-20261004-review` contains a consolidated comparison with saved
  initial v2 outputs and separately labeled assistant review. No raw output is rewritten.

The temporary model/region-limited developer policy was detached and deleted after inference;
readback confirmed no matching attachment and `NoSuchEntity`. No production/store/product writes,
publishing, deployment, billing changes, commits or merges occurred. Seller approval is pending.

## Reproduction

Both commands are offline plans unless `--live` and the existing identity/environment gates are
explicitly enabled. Completed temporary access is removed.

```sh
PYTHONPATH=src:. python -m tools.evaluate_harness_artwork \
  --revision v3 --artwork /path/to/artwork.jpg --trials 1
PYTHONPATH=src:. python -m tools.evaluate_harness_candidate \
  --revision v3 --mode full --all --trials 1
```
