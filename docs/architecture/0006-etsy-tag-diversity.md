# ADR 0006: Etsy tag diversity policy

- Status: Accepted
- Date: 2026-08-18; revised 2026-09-08 by the bounded SEO/tag decision

## Context

Etsy search can match words across a listing's tags, so repeating the same keyword in multiple
tags consumes scarce tag space without necessarily adding reach. Etsy's current seller guidance
recommends using all 13 tags, favoring relevant multiword phrases, adding variety, and making tags
as unique as possible. That guidance is a quality rule rather than an Etsy API constraint. Mr
Lister nevertheless needs a deterministic definition of "ready to post" that does not trust a
model's self-assessment.

## Decision

Mr Lister asks the model for 18–30 ranked, buyer-relevant candidate phrases covering the subject,
concrete visual elements, style or aesthetic, visible wording, theme, audience, and buyer intent.
Application code then selects exactly 13 complete input phrases, each at most 20 characters.
It preserves ranked order, skipping obvious fragments, generic-only phrases, duplicate inflections,
and low-information paraphrases. It backtracks only to find a complete nonredundant set. It never
strips, truncates, recombines, or invents words to fill tag slots.

The original absolute repeated-root prohibition is superseded by
`2026-09-08.phrase-coverage-1`. Shared meaningful words are allowed when phrases add distinct useful
intent: `diamond ring` and `engagement ring` are valid together; `octopus art` and `octopus print`
are redundant. The small explicit lexical policy normalizes case, whitespace, common inflections,
and generic product/artwork/audience heads. A phrase must contribute a specific concept beyond
generic heads. It is not a complete semantic model or proof of grounding; Gemma and seller review
still own interpretation, shopper-language relevance, and factual correctness.

Exact duplicates remain an application-contract failure. Material lexical redundancy is a
`TAG_REDUNDANCY` workflow-validation error with one-based tag locations in review feedback.
`tag_keyword_reuse_count` is retained solely as historical diagnostic telemetry; the current
evaluation gate uses `tag_redundancy_count`. Historical scores missing that metric are unassessed
under the new policy, not retrospectively certified.

If the pool cannot yield 13 eligible phrases, the existing listing invocation path permits at
most one repair, requesting additional relevant candidates and preserving all other copy fields.
Code preserves the original prose during a tag-only repair. No new tag inference operation is
introduced. An insufficient repaired pool fails before production writes; filler is never generated
by the selector. Human edits remain subject to exact-version review and approval.

## Consequences

- The selector prioritizes ranked, distinct whole phrases; actual search impact is not established
  by lexical metrics or offline tests.
- Human judgment remains authoritative through revision, while the automated ready-to-post gate
  stays consistent.
- Useful shared words no longer block review solely because their roots recur.
- Invalid tag sets never cross the production adapter boundary.
- Changes to normalization or stop words require tests because they can change evaluation scores.
- This source change does not deploy or migrate stored reviews. Previously rejected records whose
  stored validation reflects the old policy need explicit consideration before a future rollout.
- Candidate v1 is the frozen copy-quality reference; its bytes and the production rollback bundle
  are unchanged. See [the bounded checkpoint](../etsy-seo-tag-baseline.md) for the evidence gap.

## References

- [Etsy Seller Handbook: Keywords 101](https://www.etsy.com/seller-handbook/article/382774281517)
- [Etsy Help: How to Use Tags to Get Found in Search](https://help.etsy.com/hc/en-gb/articles/360000336307-How-to-Use-Tags-to-Get-Found-in-Search)
