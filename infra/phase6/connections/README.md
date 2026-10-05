# Guided Printify token connection infrastructure

`tools/prepare_connection_stage_infrastructure.py` prepares a separate connection stack and bounded patches to existing application/runtime-role templates. It performs no AWS or Printify calls and does not build an archive, change credentials, deploy resources, or enable a feature. Outputs are new mode-0600 files; existing files are never overwritten.

The account stage must already exist. This stage reuses its immutable account records, exact primary Cognito pool/client, `account` and `seller` groups, HTTP API, JWT authorizer, and `mr-lister-api/seller` OAuth scope. It does not change MFA, self-service signup, identity providers, hosted login, judging, existing cleanup, provider model settings, or the existing singleton-secret policies.

## Separate resources and capabilities

The generated stack retains `mr-lister-connections-${EnvironmentName}` with deletion protection, encryption, point-in-time recovery, and on-demand billing. It uses string partition key `PK` and the `CandidateCleanupDue` index (`cleanup_partition` string; `cleanup_due` number). The index projects keys only: cleanup queries at most ten due keys, then strongly rereads and validates each exact candidate and original binding. TTL is deliberately absent. Expiration is an application authorization check, never DynamoDB TTL.

Each route has a separate function, role, integration, and API invocation permission constrained by source account and exact API/method/path. All routes require the existing JWT authorizer and seller OAuth scope; handlers additionally require a verified native account identity and account entitlement. An OAuth scope alone does not authorize a seller workflow.

| Function / handler suffix | Route | Additional capabilities beyond exact log writes |
| --- | --- | --- |
| Query / `query_handler` | `GET /v1/store-setup` | GetItem on AccountTable and ConnectionTable |
| Validate / `validate_handler` | `POST /v1/connections/printify/validate` | Account GetItem; connection GetItem, transactional PutItem and ConditionCheckItem; CreateSecret and GetSecretValue in the owned candidate namespace |
| Select / `select_handler` | `POST /v1/connections/printify/select-shop` | Account GetItem; connection GetItem, transactional PutItem and ConditionCheckItem; candidate GetSecretValue; AdminAddUserToGroup on the primary pool |
| Activate / `activate_handler` | `POST /v1/connections/printify/activate` | Same metadata/group permissions as Select, with no Secrets Manager grant |
| Cleanup / `cleanup_handler` | Scheduler only | Connection GetItem, transactional PutItem and ConditionCheckItem; Query on the exact cleanup index; DeleteSecret in the candidate namespace |

All handlers are in `mr_lister.connections.entrypoint`. No role receives Scan, ListSecrets, PutSecretValue, legacy-owner-secret access, catalog mutations, orders, Etsy, IAM administration, or broad model access. The store uses conditional transactional Put operations, so UpdateItem is unnecessary. Cognito IAM constrains group assignment to the primary pool; the backend fixes the group name to `seller` and accepts no browser-supplied group or identity.

Secrets are restricted to the ARN namespace corresponding to `mr-lister/${EnvironmentName}/connections/<64-character-owner>/candidate_<32-character-id>`, including the six-character Secrets Manager ARN suffix. Single-character IAM wildcards bound those lengths; backend validation additionally requires the canonical owner and candidate identifiers. Tokens remain in Secrets Manager. Metadata contains no token or arbitrary secret ARN.

The one-minute Scheduler schedule starts disabled. Its delivery role trusts Scheduler only for the exact schedule group and source account, and invokes only the cleanup function. This IAM role invocation needs no Lambda resource-policy grant. Cleanup claims an exact expired, unpromoted candidate before requesting targeted deletion with a seven-day recovery window. It never forces deletion, lists secrets, or deletes an active connection's credential. Failed disposal leaves the candidate available for a later bounded retry. Consumed candidates leave the cleanup index atomically; pending metadata remains until disposal succeeds.

Before any secret write, one transaction reserves a candidate, charges a credential-free per-owner budget, and checks that the setup is still unbound. The rolling budget admits at most five new candidates in fifteen minutes, including failed validation; exact idempotent replay does not charge again. Selected stores reject further validation because credential rotation is outside this milestone. The budget uses the same retained table and existing GetItem/PutItem/ConditionCheckItem grants; it has no cleanup index or token fields.

## Feature flags and environment

`ConnectionEnabled`, `WorkflowEnabled`, and `CleanupEnabled` all default to `false`. Rules require workflow to imply connection services and connection services to imply scheduled cleanup. With the default parameters, handlers are inert and no new IAM policy is added to existing application/runtime roles. New connection roles exist but are attached only to their dedicated disabled handlers.

The connection functions receive these exact environment keys:

- `MR_LISTER_ACCOUNT_USER_POOL_ID`, `MR_LISTER_ACCOUNT_CLIENT_ID`, `MR_LISTER_ACCOUNT_TABLE_NAME`, `MR_LISTER_ACCOUNT_RESERVED_OWNER_IDS`
- `MR_LISTER_CONNECTION_ENABLED`, `MR_LISTER_CONNECTION_WORKFLOW_ENABLED`, `MR_LISTER_CONNECTION_TABLE_NAME`, `MR_LISTER_CONNECTION_SECRET_PREFIX`
- Cleanup alone also receives `MR_LISTER_CONNECTION_CLEANUP_INDEX=CandidateCleanupDue`.

Existing application functions receive the same connection/account configuration plus exact `MR_LISTER_COGNITO_ISSUER` and `MR_LISTER_COGNITO_CLIENT_ID`. Provider and publication-worker functions additionally receive canonical `MR_LISTER_LEGACY_OWNER_IDS`. The legacy allowlist never grants secret access: the original legacy credential resolver still verifies its owner/delegation authority. The complete reservation set excludes every identity predating account-stage activation, including judge identities.

The upload boundary preserves the staged reserved-owner whitelist when connection capabilities are disabled. Verified native `account` group tokens always require an active server-resolved binding, including in a flag-absent rollback configuration; a partial seller-group grant cannot open unbound upload intake. Never recompute the frozen pre-activation whitelist to include later native accounts.

The reserved list is capped at forty unique nonzero owner hashes. That is an upper bound, not a promise that forty entries fit every function: preparation resolves the **entire** existing plus new environment and rejects any value set exceeding 4096 UTF-8 bytes. Provider functions carry both reserved and legacy lists, so their effective bound may be lower. Never truncate an owner list to fit; a larger set needs a separately reviewed configuration design.

## Capture and prepare locally

Capture read-only state immediately before preparation. Use each current original application template, its parameter values, and actual Lambda configurations; do not substitute a historical repository template or silently normalize template/live drift. A capture must be at most fifteen minutes old. The tool checks consistency of supplied data but cannot prove that an operator-provided capture originated from AWS.

Every mode requires this common capture shape:

```json
{
  "captured_at": "2026-10-05T12:00:00Z",
  "region": "us-west-2",
  "account_id": "123456789012",
  "environment_name": "dev",
  "user_pool_id": "us-west-2_Abc123",
  "client_id": "existingprimaryclient",
  "seller_api_id": "abcdefghij",
  "seller_authorizer_id": "abc123",
  "authorizer": {
    "type": "JWT",
    "issuer": "https://cognito-idp.us-west-2.amazonaws.com/us-west-2_Abc123",
    "audience": ["existingprimaryclient"]
  },
  "account_table_arn": "arn:aws:dynamodb:us-west-2:123456789012:table/mr-lister-account-dev",
  "reserved_owner_ids": ["replace-with-all-derived-pre-existing-owner-hashes"],
  "preexisting_user_count": 1,
  "route_keys": ["GET /v1/account", "GET /v1/jobs"],
  "group_names": ["account", "seller", "judge"],
  "code": {
    "bucket": "existing-versioned-code-bucket",
    "key": "sealed/lambda.zip",
    "version": "actual-immutable-object-version"
  }
}
```

Example identifiers are illustrative and the owner-hash placeholder is deliberately invalid. The existing account-table ARN must match the capture's account, region, and environment. The authorizer must be JWT with exactly the captured issuer and primary client audience. The code archive requires an exact immutable S3 version; `null`, `latest`, and similar placeholders are rejected. The four new route keys must be absent. `preexisting_user_count` is the complete reserved pre-account-stage population, not later accounts provisioned through this stage.

```sh
python -m tools.prepare_connection_stage_infrastructure \
  --mode services --existing-state /private/path/connection-state.json \
  --output /private/path/connection-stack.json \
  --parameters-output /private/path/connection-parameters.json
```

`--parameters-output` is supported only for the new services stack. It emits bound parameters with all flags false; it is not a deployment request.

For an application patch, add these fields to a fresh common capture:

- `application_template_sha256`: `template_digest(baseline)` from this module, hashing canonical sorted compact JSON.
- `parameters`: every existing stack parameter needed to resolve its template.
- `references`: exact captured resource values referenced by affected function names and full environments, plus every Lambda role expression in the baseline (including unrelated functions, to detect shared resolved roles); keys use `LogicalId` for Ref and `LogicalId.Attribute` for GetAtt.
- `functions`: map each affected current Lambda logical ID to exactly `{ "function_name": "...", "role_arn": "...", "environment": { "every": "actual environment value" } }`.

The patch discovers resources by the exact production handlers and verifies their existing local role references. It accepts the complete eight-function Phase 6 group or complete three-function Phase 7.18 publication group, separately. A complete combined template is also supported; partial or duplicate groups fail. Run the patch once per actual stack, with its own capture and digest. Missing intrinsic references, changed actual environments, shared resolved roles, reused connection parameter/condition/rule names, and repeat patches fail closed.

```sh
python -m tools.prepare_connection_stage_infrastructure \
  --mode application --existing-state /private/path/phase6-state.json \
  --baseline-template /private/path/phase6-original.json \
  --output /private/path/phase6-connection-patch.json

python -m tools.prepare_connection_stage_infrastructure \
  --mode application --existing-state /private/path/phase718-state.json \
  --baseline-template /private/path/phase718-original.json \
  --output /private/path/phase718-connection-patch.json
```

Only exact connection-table metadata access is added to application roles. Query and preparation/dispatch roles receive GetItem; mutation/provider roles also receive ConditionCheckItem to fence writes against the original connection epoch. Only the provider-draft and publication-worker roles receive new namespace SecretGet. Query, upload, and command roles never receive that grant. New policies are conditional as whole policy objects, so a false feature flag leaves no empty IAM policy behind. All existing policies, role trust and boundaries, function code/settings, API settings, MFA, client/providers, judge resources, and unrelated cleanup resources remain unchanged.

For an existing AgentCore role template, provide `runtime_template_sha256`, `runtime_role_arn`, and the needed `parameters`/`references`. The selected logical resource must be the exact existing AgentCore execution role. The runtime patch adds only conditional GetItem/ConditionCheckItem on ConnectionTable. It preserves inference and model grants and adds no secret access or package/runtime changes.

```sh
python -m tools.prepare_connection_stage_infrastructure \
  --mode runtime-role --existing-state /private/path/runtime-state.json \
  --baseline-template /private/path/runtime-original.json \
  --role-logical-id ActualCapturedRuntimeRole \
  --output /private/path/runtime-connection-patch.json
```

## Activation remains a separate release

Review generated diffs and exact code/archive records before deployment. Stage explicit legacy owner configuration together with compatible application code; update the AgentCore package so frozen store bindings survive runtime serialization. Keep every new flag false until the account/connection tables, metadata guards, provider and publication authorities, route IAM, startup behavior, and full pipeline tests are verified.

Connection services may then be enabled only alongside scheduled cleanup. Leave workflow disabled until destination pinning and publication fences are proven. Finally enable workflow coherently across services and application/runtime roles. An account with a pending fixed seller-group grant can retry `/activate`; query is read-only and never assigns groups. New upload authority requires an active exact owned binding and completed activation marker, so a group-add partial success cannot bypass setup. No activation or live provider call is performed by this tool.

Local structural verification:

```sh
PYTHONPATH=src python -m pytest -q \
  tests/test_connection_stage_infrastructure.py tests/test_account_stage_infrastructure.py
```

These tests validate generated structure, capability separation, drift rejection, disabled policy elimination, split-stack inventories, and full environment budgets. They are not AWS template validation or evidence of a deployed stack.
