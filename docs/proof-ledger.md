# Product proof ledger

This ledger distinguishes validated product assumptions from claims that still require
evidence. It is updated when a repeatable test, canary, user session, or external source changes
the confidence of an assumption.

## Validated assumptions

| ID | Statement | Evidence class | Status |
|---|---|---|---|
| PF-001 | Artwork-to-listing production can be decomposed into testable steps. | Working prototype evidence | Validated |
| PF-002 | Image-aware listing generation can produce design-specific titles, descriptions, and tags. | Repeated model-assisted runs | Validated |
| PF-003 | Strict application validation catches successful but unusable model responses. | Observed failure and repair | Validated |
| PF-004 | Printify can create real products and publish them to a connected Etsy channel. | Live canary runs | Validated |
| PF-005 | Batch processing can isolate a failed item and continue with the next. | Multi-item workflow runs | Validated |
| PF-006 | Product placement can be represented as a reusable calibrated profile. | Apparel calibration runs | Validated |

## Open product hypotheses

| ID | Hypothesis | Evidence needed | Status |
|---|---|---|---|
| HP-001 | New POD sellers will trust a guided approval workflow. | Five moderated user sessions | Open |
| HP-002 | Active sellers consider listing production a top-three scaling constraint. | Structured seller interviews | Open |
| HP-003 | Image-aware listing drafts materially reduce time-to-publish. | Timed manual versus assisted comparison | Open |
| HP-004 | Sellers will pay for one-at-a-time automation before bulk features exist. | Pricing interviews or preorder test | Open |
| HP-005 | Recommended tags are more useful than a seller's unaided first draft. | Blind qualitative evaluation | Open |

Phase 6 can provide the first usability evidence for HP-001. Automated browser tests, developer
canaries, and a successful live provider run do not validate seller trust by themselves; the
hypothesis remains open until five moderated first-time-seller sessions are recorded.

## Claim policy

Mr Lister may promise relevant, compliant listing assistance and operational time savings that
the product can demonstrate. It must not guarantee search rank, traffic, conversion, or sales.
