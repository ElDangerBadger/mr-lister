# Etsy SEO prompt evaluation v1

- Date: 2026-09-08
- Model: `google.gemma-3-27b-it`
- Region: `us-west-2`
- Source prompt SHA-256: `cd3b678a5f71d3def93dc5fae1a23bdd17ee11542b855baa91070cc640694fb1`
- Baseline: `2026-08-18.7`, fingerprint
  `c5b2a76ebcc9fff8bd5363beb2db2d1651ad554fab21340a6a4cdb1a166ac96f`
- Candidate: `2026-09-08.1-etsy-seo-candidate`, fingerprint
  `d72948fe5a7ea155f6fa5283428ffcd26011e1e871342086ecf89e27398c56c2`
- Production adapter: fake; publication disabled

## Decision

Keep the sealed baseline as the deployed/default prompt. The expanded Etsy SEO prompt is a useful
quality direction, but candidate v1 is not ready for default promotion because final tag quality
regressed in several cases. Retain the reversible prompt-bundle seam and make one narrow candidate
revision before reconsidering promotion.

The source prompt was adapted rather than pasted verbatim. Its merchandising, concept-hook,
audience, buyer-intent, restrained-voice, description, and quality-control guidance was mapped into
the existing flat `ListingCandidateDraft` contract. Its alternate titles, separate SEO arrays,
positioning object, mockup advice, and nested output schema were omitted because Mr Lister neither
stores nor renders those fields. The application-owned 18–30 candidate-tag pool and deterministic
final selection of exactly 13 tags remain unchanged.

## Same-model paired evidence

Both runs used all eleven existing evaluation fixtures, one trial per fixture, the same Gemma
settings, and the complete local workflow with fake production.

| Metric | Baseline | Candidate v1 |
| --- | ---: | ---: |
| Valid editable reviews | 11/11 | 11/11 |
| Automated quality-floor passes | 10/11 | 10/11 |
| Fake draft creates / publish calls | 11 / 0 | 11 / 0 |
| Mean model latency | 14.967 s | 14.975 s |
| Median model latency | 13.488 s | 11.132 s |
| P90 model latency | 17.349 s | 20.417 s |
| Total input / output tokens | 13,602 / 8,972 | 21,851 / 9,890 |
| Total tokens | 22,574 | 31,741 |
| Bounded repairs | 1 | 2 |
| Average tag relevance | 0.7273 | 0.7576 |
| Repeated final tag keywords | 0 | 0 |

The sole automated failure in each run was the already-known transparent-seahorse visual-anchor
miss. Every output still satisfied the application contract and reached `awaiting_approval`.
The prompt-injection fixture remained artwork data and did not grant publication authority.

The aggregate latency tie is not evidence that the longer listing prompt is free. Operation-level
diagnostics show baseline listing requests totaled 65.533 seconds, while candidate v1 totaled
80.817 seconds and added one listing repair. Candidate v1 used 40.61% more total tokens across the
complete two-call workflow. A single trial is enough to expose the cost direction, not to claim a
stable latency delta.

## Blinded qualitative review

The pair order was randomized per case before scoring concept hook, buyer specificity, natural
copy, restrained voice, tag usefulness, and factual grounding on a five-point scale.

| Result | Baseline | Candidate v1 |
| --- | ---: | ---: |
| Overall mean | 3.73 / 5 | 4.42 / 5 |
| Pair wins | 0 | 11 |
| Tag usefulness | 3.09 / 5 | 3.36 / 5 |
| Factual grounding | 3.55 / 5 | 4.55 / 5 |

Candidate v1 produced populated audiences in 11/11 cases versus 0/11 for the baseline, stronger
search-led titles, clearer design hooks, and more artwork-specific descriptions. It removed the
baseline's unsupported `comfortable` wording and the moon-moth claim that transparency guarantees
a clean result on every shirt color.

The remaining defects are concentrated and observable: generic one-word tags such as `lover`,
`life`, `tee`, and `art`; the malformed `lover gift`; garment-color phrases inferred from artwork
colors; transparency checkerboards treated as printable content; and unsupported child, home-decor,
or sustainability audiences. Candidate v1 therefore remains evaluation-only.

## Next bounded revision

The next candidate should require the first thirteen candidates to be independently useful,
multiword, and mutually free of meaningful keyword-root reuse so deterministic selection does not
project generic leftovers. It should explicitly reject transparency-rendition checkerboards,
garment-color inferences, non-apparel audiences, and unsupported sustainability claims. Retest the
affected fixtures before any default or deployed-runtime change.

