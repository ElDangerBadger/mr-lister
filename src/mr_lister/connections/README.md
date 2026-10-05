# Guided personal-token connection integration

This stage supports one selected Etsy store per native account. It never redirects an
existing connection or job when a different shop is requested. Account records remain
immutable. No OAuth application, token refresh, rotation, disconnect, provider mutation,
or provider product cleanup is implemented here.

## Runtime entrypoints and IAM

All handlers are in `mr_lister.connections.entrypoint`. They reject every route except
that handler's exact route; JWT identity and the immutable account's `manage_connections`
entitlement are checked before constructing a connection service. Reserved legacy owners,
including judge, cannot use these APIs even if accidentally assigned the account group.

| Handler | Route | Additional capability |
| --- | --- | --- |
| `query_handler` | GET `/v1/store-setup` | DynamoDB GetItem on account and connection tables only |
| `validate_handler` | POST `/v1/connections/printify/validate` | Account GetItem; connection GetItem/PutItem/ConditionCheckItem; scoped Secrets Manager CreateSecret/GetSecretValue |
| `select_handler` | POST `/v1/connections/printify/select-shop` | Account GetItem; connection GetItem/PutItem/ConditionCheckItem; scoped GetSecretValue; fixed seller AdminAddUserToGroup on exact pool |
| `activate_handler` | POST `/v1/connections/printify/activate` | Account GetItem; connection GetItem/PutItem/ConditionCheckItem; fixed seller AdminAddUserToGroup; no secret or provider capability |
| `cleanup_handler` | Trusted one-minute schedule, no public route | Connection GetItem/PutItem/ConditionCheckItem; Query on exact cleanup index; DeleteSecret in owned namespace only |

Transactions call the TransactWriteItems API using the underlying PutItem and
ConditionCheckItem IAM actions; TransactWriteItems is not an IAM action.

There is no generic handler that accepts all routes. Query cannot mutate or grant a group.
Validation cannot grant seller. Activation constructs neither secret nor provider clients.
Secrets are created under a deterministic owner/candidate name and immutable version; no
PutSecretValue, ListSecrets, arbitrary ARN, legacy-secret fallback, or secret catalogue sweep
is needed. Secret failures are sanitized after leaving the dependency exception scope.

Require shared `MR_LISTER_ACCOUNT_*` authority/table/reserved-owner variables plus:

- `MR_LISTER_CONNECTION_ENABLED=true`; otherwise HTTP handlers return 503 and cleanup is inert.
- `MR_LISTER_CONNECTION_TABLE_NAME`: dedicated table with string partition key `PK`.
- `MR_LISTER_CONNECTION_SECRET_PREFIX`: exactly `mr-lister/<lowercase-env-slug>/connections/`.
- `MR_LISTER_CONNECTION_WORKFLOW_ENABLED=true`: keep false until the destination-pinned
  upload, job, approval, provider, and publication paths are active. Only true permits
  fixed native-subject seller membership and an active workflow binding.
- `MR_LISTER_CONNECTION_CLEANUP_INDEX=CandidateCleanupDue`: required by cleanup, never inferred.

Do not log API request bodies, Lambda events, Authorization headers, or secret responses.
API Gateway access logs must omit token bodies. Enable the cleanup schedule whenever token
validation is enabled. All SDK clients have bounded timeouts and one attempt per invocation.

## Wire contracts and retry behavior

Each account can reserve at most five new validation candidates in any fifteen-minute
window. A credential-free budget row stores only the owner hash and up to five attempt
timestamps. Candidate creation, budget charging, and an unchanged unbound setup condition
share one transaction before Secrets Manager writes or Printify calls. Failed credential
checks and failed secret writes count; exact idempotent replay and recovery after a lost
reservation response never charge twice. A new attempt over the limit returns the fixed
`429 CONNECTION_RATE_LIMITED` response. Expired orphan cleanup remains independent.

Validation accepts only `{token}` plus `Idempotency-Key` (16–128 ASCII letters, digits,
underscore, or hyphen). It makes only HTTPS GET `https://api.printify.com/v1/shops.json`,
with no redirects, user-selected URL, proxy environment, or provider mutation. Responses
are limited to 64 KiB and 100 unique shops. Only sales_channel `etsy` is eligible.

Candidate IDs match `candidate_[a-f0-9]{32}`. Public `expires_at` is UTC ISO text; authorization
uses the internal Unix deadline exactly 900 seconds after initial reservation. Candidate
`record_version` is the setup version to send as `expected_setup_version` with selection.
The response includes safe store id strings, name, sales_channel, eligible, and the nullable
`unsupported_channel` disabled_reason. No token, hash, secret name, or secret version is public.

Selection accepts only `{candidate_id,shop_id,expected_setup_version}` plus its owned
Idempotency-Key. It reloads the exact secret version and rechecks current Etsy shop membership.
One conditional transaction consumes the candidate, creates the immutable connection/binding,
advances selected setup, and records the exact owned operation receipt. A different destination
or consumed candidate is an explicit conflict. An already selected store rejects further
validation before secret or provider access; this milestone has no credential rotation.
Exact selection replay uses the committed receipt without another provider or secret request.

After durable selection, workflow-enabled activation adds only fixed `seller` to the verified
immutable native account's Cognito subject. A fenced transaction marks connection active and
seller grant complete. An uncertain response is recovered by strong reads. A page reload can
POST exact `{}` to `/activate`, independent of candidate expiry or lost browser operation state.
It performs no secret/provider request and repeats only the idempotent fixed group grant.
The frontend must renew its access token before opening the workflow.

Public setup is `account-setup-v1`. With no store it is `connection_required` or `choose_store`.
A selected destination with workflow disabled is `connection_unavailable` with null store.
With workflow enabled, a selected destination awaiting only the group/marker completion is
`reconnect_required` with a safe store, displayed as **Finish connecting**. In this initial
phase that state means activation recovery only, not a revoked token or token rotation.
A completed active connection is `ready`. Future credential rotation needs a separately
versioned lifecycle that preserves original connection/binding identity; never overload a
new selected-store preference to silently reroute an old job.

## Original destination authority and cleanup

`DynamoConnectionDirectory(client, config).get_active_binding(owner_id, shop_binding_id,
expected_setup_version)` is for new uploads. `require_current(binding)` and
`assert_current(binding)` validate the exact original connection, independently of mutable
selected setup. `current_epoch_condition(binding)` returns a full DynamoDB ConditionCheck
for inclusion in the SAME provider-claim transaction. `ExactConnectionResolver(directory=...,
credentials=...).resolve_exact(binding=...)` resolves only that exact active authority and
returns `OwnerPrintifyConnection` carrying the matching complete store_binding.

Connection rows use `OWNER#<owner>#SETUP#BINDING#<binding-id>` and cross-check strict payload
against top-level owner_id, connection_id, shop_binding_id, shop_id, authorization_epoch,
binding_fingerprint, and status. Those fields and the exact payload are fenced in conditions.
No current selection or shop-id-only fallback is accepted by the resolver.

Candidates retain cleanup metadata. GSI `CandidateCleanupDue` has string partition key
`cleanup_partition` (fixed `CANDIDATE`) and numeric sort key `cleanup_due` (expires_at + 901).
Projection KEYS_ONLY is sufficient. Pending/validated/deleting records are indexed;
consumed/deleted records remove both keys atomically. DynamoDB TTL must remain OFF.
The extra 901 seconds exceed the maximum remaining lifetime of an in-flight AWS Lambda
when the candidate expires, preventing delayed validation from recreating a disposed secret.

Each cleanup run queries at most 10 due rows, re-reads each exact candidate, and claims it
with a conditional transaction that also requires its original binding row to be absent.
Even retries recheck the original binding. Promoted candidates are never deleted. It schedules
7-day recoverable secret deletion, never forced deletion, and retains a metadata tombstone.
Failures retain the indexed deleting record for retry; missing secrets can be tombstoned after
the grace period. Eventual-index lag cannot authorize disposal. No browser cleanup endpoint exists.
