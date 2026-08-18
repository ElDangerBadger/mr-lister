# ADR 0002: Version-bound human approval

- Status: Accepted
- Date: 2026-08-18

## Context

Publishing changes a seller's live storefront. Approval of one listing draft must not silently
authorize a later revision, and agent intent must not substitute for a human decision.

## Decision

Every review is an immutable, numbered snapshot. Approval records the exact `review_version`.
Editing an approved snapshot creates a new version and invalidates the earlier approval.
Publication requires the job to be in `APPROVED` and the approved version to equal the current
review version.

## Consequences

- Mr Lister remains intentionally human-in-the-loop.
- Publication tools repeat state and approval checks at the write boundary.
- Retries return through `APPROVED` rather than bypassing authorization.
- Audit records can identify exactly what a seller approved.
