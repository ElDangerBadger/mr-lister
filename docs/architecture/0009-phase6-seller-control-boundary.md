# ADR 0009: Persist the Phase 6 seller decision in application state

- Status: Accepted
- Date: 2026-08-21

## Context

Phase 4 proved that a Standard Step Functions execution can pause durably on a task token and
resume after approval. Phase 5 proved that the preparation path can create one real unpublished
Printify product. Phase 6 exposes those capabilities through a seller interface and therefore
makes edit, approval, cancellation, and recovery semantics part of the product boundary.

The Phase 4 state machine currently routes an approval callback directly into fake publication.
The approval-wait record is also bound to one review version, while a valid listing revision
creates a new review version without resolving or replacing that wait.

Keeping an orchestration task token open across arbitrary seller think time, revisions, browser
sessions, cancellations, and edits after approval would require versioned token rotation,
callback-delivery reconciliation, and replacement execution recovery. That machinery would
duplicate authority already held by the application and DynamoDB.

## Decision

Phase 6 uses persisted application state as the durable human-review pause:

1. The preparation execution performs bounded machine work, commits `AWAITING_APPROVAL`,
   `NEEDS_REVISION`, or a durable failure/reconciliation state, and ends without waiting for a
   human callback.
2. Seller commands use application-owned, conditional DynamoDB transactions.
3. A valid revision creates an immutable review version and starts bounded synchronization work.
4. Approval commits an immutable decision for the exact current review version and fingerprint,
   then ends at `APPROVED`.
5. Cancellation commits an immutable terminal seller intent. The operational job may briefly stay
   in cancellation/reconciliation states until in-flight work settles, but it cannot return to
   edit or approval. Cancellation does not delete the unpublished Printify product automatically.
6. Phase 7 starts a separate, idempotent publication execution from an immutable approved
   snapshot.

Every command that requires asynchronous work writes a `WorkRequest` in the same DynamoDB
transaction as its receipt and state change. A DynamoDB Stream dispatcher plus scheduled due-work
sweeper starts a deterministic Standard Step Functions execution for that request. Receipt replay
marks still-pending work due now; only the dispatcher starts executions. A crash after commit but
before `StartExecution` therefore delays work without stranding the job or creating a second
logical execution.

Step Functions remains responsible for asynchronous machine work, retry routing, and observable
execution. It is not held open merely to represent human think time and remains unable to assign
business state directly. ADR 0008 continues to govern every transition. ADR 0010 governs
same-product synchronization, and ADR 0011 governs authenticated ownership.

## Consequences

- Approval is a successful Phase 6 terminal state and cannot invoke publication.
- No callback token can be stranded by a revision, cancellation, or browser session.
- `APPROVED` is terminal for Phase 6; approval withdrawal is deferred until Phase 7 can define its
  race against publication.
- Cancellation during active provider work may require reconciliation before the UI reports final
  cancellation; provider artifacts remain unpublished and recoverable.
- Publication roles, routes, and controls are absent from the Phase 6 surface.
- Phase 4 callback evidence remains valid, but its callback topology is not the Phase 6 product
  architecture.

## Rejected alternatives

### Keep one callback token and rebind it after each edit

This makes a token created for an older review authorize control flow for a newer review and does
not handle editing an already approved job without another execution.

### Create a new callback execution for every review version

This can be made safe with versioned waits and a transactional callback outbox, but it introduces
more failure states than a persisted human pause provides value for in the Phase 6 slice.
