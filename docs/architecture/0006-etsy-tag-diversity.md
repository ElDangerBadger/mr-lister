# ADR 0006: Etsy tag diversity policy

- Status: Accepted
- Date: 2026-08-18

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
Application code then selects exactly 13. Candidate alternatives may overlap because only a subset
is used, but the pool must contain enough relevant alternative vocabulary for a collision-free set.

Exact duplicate tags are an application-contract failure. Repeated normalized meaningful words
across different tags are a deterministic workflow-validation error and increment
`tag_keyword_reuse_count`. The provider now returns a ranked pool of 18–30 candidate phrases.
Application code deterministically selects the strongest feasible 13-tag subset without normalized
keyword collisions. The model receives a bounded repair opportunity only when its entire candidate
pool cannot produce a valid subset, but its rationale or claim of compliance is never treated as
evidence. A provider draft that still cannot be finalized after repair fails at the intelligence
boundary and performs no production write. A human-edited public listing with repetition is
preserved in `needs_revision` until corrected. Stop words are ignored, and the system must not
substitute irrelevant filler merely to make the metric pass.

## Consequences

- The generated set uses Etsy's combinatorial matching more efficiently.
- Human judgment remains authoritative through revision, while the automated ready-to-post gate
  stays consistent.
- Repetition is visible and measurable across providers and prompt versions.
- Invalid tag sets never cross the production adapter boundary.
- Changes to normalization or stop words require tests because they can change evaluation scores.

## References

- [Etsy Seller Handbook: Keywords 101](https://www.etsy.com/seller-handbook/article/382774281517)
- [Etsy Help: How to Use Tags to Get Found in Search](https://help.etsy.com/hc/en-gb/articles/360000336307-How-to-Use-Tags-to-Get-Found-in-Search)
