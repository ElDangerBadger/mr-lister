# Native account stage

This stage prepares a verified native MassSkutiny account and a store-setup profile. It does not connect Printify, add seller privileges, inherit the owner's store, or change judge access. The local preparation tool does not call AWS or deploy anything.

`tools/prepare_account_stage_infrastructure.py` produces two separate artifacts:

1. An additive stack with a retained, encrypted, single-partition `AccountTable`, an `account` Cognito group without an IAM role, a read-only `AccountQuery` Lambda, and an `AccountProvision` Lambda invoked by the existing pool. It adds exactly `GET /v1/account` to the existing seller HTTP API, protected by its existing JWT authorizer and `mr-lister-api/seller` scope. It creates no replacement API, authorizer, or stage.
2. A bounded patch to a freshly captured original application template. Only the pool's disabled self-service flag, optional post-confirmation and pre-authentication hooks, and the reviewed store-setup/legal SPA routes change. Existing client OAuth settings, identity providers, owner MFA, email settings, API transport, functions, provider credentials, and judge session/cleanup resources remain intact. An existing `PostConfirmation` or `PreAuthentication` hook, account group/resource/route, or repeat patch causes preparation to fail; other Cognito hooks are preserved.

Both new handler flags default to `false`. `AccountPostConfirmationEnabled` and `SelfServiceSignupEnabled` also default to `false`, leaving public signup closed and both new hooks absent. The existing `AccountPostConfirmationEnabled` parameter and `UseAccountPostConfirmation` condition control both hooks together, each referencing the same `AccountProvisionFunctionArn`; there is no separate pre-authentication enable path. A CloudFormation rule prevents enabling self-service signup without attaching both hooks. Attaching the hooks alone does not enable the provision handler. Provisioning, hooks, and signup are activated separately after end-to-end verification; generating these files never activates them. The disabled provision handler passes trigger events through unchanged without account reads or writes.

## Identity and permissions

The provision handler accepts trusted `PostConfirmation_ConfirmSignUp` and `PreAuthentication_Authentication` events from the exact existing pool/client. Post-confirmation provisions a verified native email account. Before a later native sign-in issues tokens, pre-authentication idempotently repairs a missing account record or `account` group membership. Every eligible native pre-authentication event first performs `AdminGetUser` for its exact trusted subject in the existing pool. The returned user must be enabled and `CONFIRMED`, have the matching subject and username, have verified email matching the trusted event through a transient digest, and have no external-provider identities. The event itself may omit `cognito:user_status`; the authoritative user read is required before any account write or group grant. It reuses the same conditional immutable record and fixed `account` group operations; it never adds `seller` or judge membership.

Both hooks share the existing Lambda invocation permission restricted to that pool's exact ARN and source account. **Every pre-existing pool identity**, including owner and judge identities, is excluded using canonical derived owner hashes captured immediately before activation. Reserved and federated pre-authentication events pass through unchanged before dependency construction; unknown-user events fail before any lookup or write. Cognito does not invoke the pre-authentication hook for token refresh, so refresh alone cannot repair provisioning; an eligible native sign-in is required.

IAM separates query and provisioning: query can only `GetItem` from the new table; provision can `GetItem`/`PutItem` there and `AdminGetUser`/`AdminAddUserToGroup` on the one existing pool. Only `AccountProvisionRole` receives the user-read permission; `AccountQueryRole` has no Cognito permissions. Cognito's group-add IAM permission is pool-scoped; the fixed group name is enforced in the provision handler. Neither role can read provider secrets, scan/delete accounts, change user attributes, administer IAM, invoke listing workers, or publish products.

The account record uses `PK=OWNER#<derived owner id>` and stores an immutable `merchant-account-v1` JSON payload with setup-only entitlement and an unavailable connection state. No email, password, token, or provider credential is stored here. Query identity comes from the gateway's verified JWT claims and independently checks the expected issuer, client, seller scope, and exact account group. Existing seller APIs continue enforcing seller membership, so account-only users cannot create drafts against the owner's connection.

Handlers accept only these shared environment variables: `MR_LISTER_ACCOUNT_USER_POOL_ID`, `MR_LISTER_ACCOUNT_CLIENT_ID`, `MR_LISTER_ACCOUNT_TABLE_NAME`, `MR_LISTER_ACCOUNT_RESERVED_OWNER_IDS`, plus the individual `MR_LISTER_ACCOUNT_QUERY_ENABLED` or `MR_LISTER_ACCOUNT_PROVISION_ENABLED` flag. Code is bound to an exact S3 object version. No owner/store secret identifier is part of this stage.

## Prepare locally

Use the **captured live original** application template for a future release, not a processed SAM template or a historical repository template. Capture the existing pool/client/API/JWT-authorizer bindings and the complete current group/route/user inventories read-only. Do not capture email addresses, credentials, or invitation links in the preparation record.

```sh
python -m tools.prepare_account_stage_infrastructure \
  --mode services --baseline-template /private/path/application-original.json \
  --output /private/path/account-stage.json

python -m tools.prepare_account_stage_infrastructure \
  --mode application --baseline-template /private/path/application-original.json \
  --output /private/path/application-account-patch.json
```

Both outputs are newly created mode-0600 files and will not overwrite an earlier capture. The services stack requires exact code-bucket/key/version and existing pool/client/API/authorizer parameters. Its `ReservedOwnerIds` parameter has no default.

For a bound parameter artifact, add `--existing-state /private/path/account-state.json --parameters-output /private/path/account-bindings.json` to the services command. The capture has exactly these fields:

```json
{
  "captured_at": "2026-10-05T12:00:00Z",
  "user_pool_id": "us-west-2_Abc123",
  "client_id": "existingclientid",
  "seller_api_id": "abcdefghij",
  "seller_authorizer_id": "abc123",
  "group_names": ["seller"],
  "route_keys": ["GET /v1/jobs"],
  "reserved_owner_ids": ["replace-with-derived-existing-owner-hashes"],
  "preexisting_user_count": 1
}
```

The example hash placeholder is deliberately invalid. A real capture must be at most 15 minutes old, include every existing pool user once, and contain no account group or `/v1/account` route. Deployment is capped at 40 existing identity hashes so the complete environment remains under Lambda's 4 KB limit. Larger pools require a table-backed exclusion design; preparation rejects them rather than truncating the exclusion list. Re-capture immediately before signup activation and verify all bindings against the existing issuer/client/API; this local validator cannot prove an operator-supplied inventory came from AWS.

The account stage must exist, its pool-scoped invocation permission must be verified, and the application patch must receive that exact `AccountProvisionFunctionArn` before activating both hooks. Keep self-service signup closed while checking the disabled handlers, IAM, native confirmation, pre-authentication repair, reserved/federated pass-through, and mobile/account-only routing. Keep owner MFA `ON` with `SOFTWARE_TOKEN_MFA` throughout. Disconnect and queued-job/provider binding will belong to the subsequent own-store connection stage. No account-only user may be promoted to seller until that connection stage is ready.
