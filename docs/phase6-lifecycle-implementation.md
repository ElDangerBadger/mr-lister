# Phase 6.1 seller-control lifecycle evidence

Phase 6.1 introduces a separate `2.0.0` application-control boundary under
`mr_lister.control`. The retained Phase 4/5 workflow and its `1.0.0` evidence remain unchanged.
This slice does not call Printify, publish, order, fulfill, expose an HTTP API, or deploy cloud
resources.

Present-state note (2026-09-01): this historical slice now underpins the deployed draft-only Phase 6
release. An authenticated current-release job exercised retry and exact-version approval through
this lifecycle and ended at `APPROVED` with its provider draft still editable and unpublished. The
separately frozen formal Phase 6.6 evidence artifact set is not claimed as passed here.

## Authority boundary

The application owns the legal lifecycle graph and validates every command bundle before either
store can write it. A successful mutation atomically binds the exact prior Job CAS to one updated
job, one ordered domain event, one owner-scoped command receipt, any required immutable decision or
evidence record, and at most one bounded work request. The in-memory store is the deterministic
oracle for the DynamoDB adapter.

The boundary now enforces these invariants:

- job creation is only `INTAKE_VALIDATED` with pristine deterministic `PREPARE` work;
- seller revise, approve, cancel, and retry commands are owner-, version-, and idempotency-bound;
- approval is terminal and binds the current review, one immutable product ID, exact product-sync
  evidence, and current pricing evidence through one composite fingerprint;
- cancellation intent is immutable, dominates late worker results, and cannot restore edit or
  approval authority;
- retry uses only the closed recovery action persisted with the exact failure record;
- one product ID can be established once and can never be cleared or replaced;
- machine states always retain work of the exact allowed type, while terminal and human-wait states
  cannot retain work;
- no command transaction can orphan, replace, or silently mutate active work.

## Transactional outbox and dispatch handshake

Work requests bind one fixed work type, identifier-only input, and deterministic Standard Step
Functions execution name. The dispatcher can select only from its complete static work-type ARN
allowlist. It verifies an existing execution by exact ARN, name, state machine, and input before
treating `ExecutionAlreadyExists` as success.

The launch handshake closes both sides of the AWS acknowledgment race:

- an exact active `CLAIMED` request is worker-settleable, so an execution may finish before
  `StartExecution` returns;
- after any uncertain start attempt, the request remains claimed and due after a bounded lease
  instead of returning to `PENDING`;
- deterministic redrive reuses the same execution identity;
- dispatcher acknowledgment treats an already completed request as success;
- worker settlement rebases once when a concurrent dispatch acknowledgment is the only persisted
  change, while any Job-authority change still fails the CAS.

## Verification boundary

Offline tests cover the state graph, immutable authority records, exact receipt replay and changed
payload conflicts, competing seller commands, cancellation/failure dominance, fresh-store DynamoDB
round trips, full-payload CAS, outbox due/claim/defer/dispatch operations, fast-worker and ambiguous
start races, owner isolation, sanitized failures, and a package-wide prohibition on Phase 6
publication/provider-call surfaces.

Phase 6.2 now implements the offline same-product PUT/readback workers, one-shot upload and write
permits, bounded GET-only reconciliation, immutable economics evidence, checkpointed Strands
preparation bridge, and four Standard Step Functions definitions. Phase 6.1 remains the authority
foundation beneath that work. The Phase 6.2 path is now deployed and has completed the functional
same-job walkthrough; its current release state and still-unclaimed formal hardening evidence are
recorded in
[`phase6-provider-integration.md`](phase6-provider-integration.md).
