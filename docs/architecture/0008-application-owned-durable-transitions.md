# ADR 0008: Application-owned durable state transitions

- Status: Accepted
- Date: 2026-08-19

## Context

Phase 4 adds Step Functions and DynamoDB to a workflow whose safety depends on versioned human
approval, deterministic validation, and exactly-once marketplace intent. A state-machine definition
is useful for orchestration, but it is deployment configuration: it can be retried, replaced,
started twice, or invoked with stale data. Model output is untrusted recommendation data. Neither
is a safe authority for business transitions.

The Phase 1 in-memory workflow also wrote directly to adapter dictionaries. That shape cannot be
implemented safely by a durable store and leaves no atomic point at which to reject concurrent or
stale work.

## Decision

Application contracts define the allowed job-state graph and application commands validate the
business preconditions for every transition. The persistence boundary commits a fully validated
transition only when an atomic compare-and-set condition succeeds. Every `JobRecord` carries a
monotonically increasing `record_version`; a DynamoDB implementation must condition each update on
the expected version and state, then increment the version exactly once.

No normal caller—including Step Functions, Lambda handlers, AgentCore, model tools, retries, or the
review interface—may directly assign job state. These callers can request an application command;
they cannot commit its result. Step Functions coordinates when to attempt work and how to route or
retry the result. It does not define whether approval, revision, publication, or verification is
valid.

Approval and publication commands must additionally bind their conditional write to the current
review version and other relevant invariants. DynamoDB expressions are the atomic enforcement of
decisions made by readable, tested application code; they are not a second, independent source of
business rules.

## Consequences

- Two workers cannot both commit a transition from the same record version.
- A stale approval callback cannot approve a newer review.
- Replaying a Step Functions task or callback becomes a handled idempotency/concurrency outcome,
  not an alternate path through the state graph.
- Models remain recommendation-only and receive no state-assignment capability.
- Store adapters are more explicit, and transition tests can run entirely in memory before live AWS
  deployment.
- Multi-item operations such as review replacement plus job transition will require DynamoDB
  transactions or an equivalent application-owned atomic command boundary.
