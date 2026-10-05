# Account-only backend integration

This stage enrolls native, verified-email signups into the fixed Cognito `account`
group. It never grants `seller`, resolves Printify credentials, or enables uploads.
`GET /v1/account` is a read-only account projection; store connection remains unavailable.

Entrypoints:

- Cognito PostConfirmation and PreAuthentication: `mr_lister.accounts.provision_entrypoint.lambda_handler`
- HTTP API v2: `mr_lister.accounts.query_entrypoint.lambda_handler`

Both require these exact environment variables when enabled:

| Variable | Meaning |
| --- | --- |
| `MR_LISTER_ACCOUNT_USER_POOL_ID` | Exact existing native-email Cognito pool; region and issuer derive from it. |
| `MR_LISTER_ACCOUNT_CLIENT_ID` | Exact public app client. |
| `MR_LISTER_ACCOUNT_TABLE_NAME` | Dedicated account table with string partition key `PK`. |
| `MR_LISTER_ACCOUNT_RESERVED_OWNER_IDS` | JSON array of 1–256 unique nonzero lowercase 64-hex owner hashes for **all pre-existing pool users**, including seller and judge. Activation preparation must capture the complete list. Deployment must also respect Lambda's total environment size limit. |

`MR_LISTER_ACCOUNT_PROVISION_ENABLED=true` enables provisioning. Missing, false,
or any other value returns the original trigger unchanged, without configuration
parsing or dependency construction. `MR_LISTER_ACCOUNT_QUERY_ENABLED=true` enables
the query; otherwise it returns a fixed 503 response. Flags default to disabled.
Do not enable public signup before the trigger, account group, reserved-owner list,
and table are active. Disabled provisioning cannot grant access to any application API.

Accepted mutation triggers are `PostConfirmation_ConfirmSignUp` and
`PreAuthentication_Authentication` version `1` from the exact configured
pool/client/region. Native email pools supply a UUID subject and either that
subject or the verified email as username. `email_verified` must be the literal
string `true` or boolean true. Linked/federated identities are skipped, including
users carrying an `identities` attribute. Forgot-password confirmation is a no-op.
ClientMetadata, pre-authentication validationData, custom attributes, and extra
fields cannot select a group, owner, record, entitlement, table, or provider.

Pre-authentication can repair a confirmed signup whose account write or group
assignment failed or returned an unknown outcome. Before any write or membership
operation, it performs one `AdminGetUser` on the exact configured pool and the
trusted native UUID subject. The response must show `Enabled=true`,
`UserStatus=CONFIRMED`, the same subject and canonical username, a verified email
matching the event's transient email digest, and no linked identities. Duplicate
or malformed returned attributes fail closed. This authoritative status check
works when the optional event status attribute is absent. It does not modify
passwords, user attributes, MFA, challenges, or token claims.

Pre-authentication returns the event unchanged for reserved pre-activation owners
and federated/linked identities, without constructing SDK dependencies. Exact
pool/client/region/source checks precede those exclusions, and `userNotFound=true`
or a malformed userNotFound value fails before dependency construction. Reserved
owners are still rejected by PostConfirmation. This preserves existing owner and
judge sign-in paths without enrolling them in account setup. The two triggers
share the same trusted Lambda and disabled-by-default hook flag. Cognito still
enforces password/authenticator challenges with pool MFA ON; a provisioning group
grant does not complete authentication or issue a token.

The owner is SHA256(issuer + NUL + subject), matching existing seller ownership.
The record contains no email, password, token, or raw username. The immutable
`merchant-account-v1` payload includes owner/issuer/subject/client, username digest,
first creation time, record_version=1, entitlements=[manage_connections], and
setup_state=connection_unavailable. There are no write/rotate/disconnect APIs yet;
the entitlement is private future authority, not a seller grant.

The exact DynamoDB envelope is `PK=OWNER#<owner_id>`, `entity_type=MERCHANT_ACCOUNT`,
and string `payload` containing canonical JSON. Provision uses PutItem with
`attribute_not_exists(PK)`. A collision is read consistently and accepted only
when its validated immutable identity is the same. Existing legacy/malformed or
foreign-identity rows fail closed and are never overwritten. A successful record
write precedes fixed AdminAddUserToGroup(account), targeting the immutable native
UUID subject rather than a mutable email alias. Lost responses and retries
reuse the record and idempotent membership call; no partial result creates seller
access. A later fresh native sign-in retries the account repair before the
trigger returns successfully, including after the browser has lost its original
signup state. Cognito does not run PreAuthentication on session renewal, so an
already established session requires a fresh sign-in for this recovery. Persistent
service/identity failures remain closed and sanitized; there is no browser
provisioning endpoint.

Provision IAM needs only GetItem/PutItem on the exact account table plus
AdminGetUser and AdminAddUserToGroup on the exact pool. AdminGetUser is used only
for native pre-authentication proof; trusted signup confirmation requires no
additional lookup. Query needs only GetItem on that table.
Neither role needs secret/provider access. Scope Cognito's Lambda invoke permission
to the exact pool ARN and account. The shared JWT authorizer must require the
existing `mr-lister-api/seller` OAuth scope; this handler additionally requires
`account` group and validates the exact issuer/client/access-token context before
reading only its derived owner's key. No body, query parameters, or foreign IDs
are accepted. Reserved owners remain denied even if accidentally assigned account.

Public success is exactly `{contract_version:"account-setup-v1",record_version:1,
state:"connection_unavailable",connection_method:"unavailable",store:null}`.
Responses are no-store. Errors are fixed and contain no provider/SDK messages;
there are no event, claim, attribute, or credential logs in this package.

Cognito references:

- [Post-confirmation event](https://docs.aws.amazon.com/cognito/latest/developerguide/user-pool-lambda-post-confirmation.html)
- [Pre-authentication event and session-renewal exclusion](https://docs.aws.amazon.com/cognito/latest/developerguide/user-pool-lambda-pre-authentication.html)
- [Common event fields and native/federated trigger sources](https://docs.aws.amazon.com/cognito/latest/developerguide/cognito-user-pools-working-with-lambda-triggers.html)
- [AdminGetUser request authority and documented response](https://docs.aws.amazon.com/cognito-user-identity-pools/latest/APIReference/API_AdminGetUser.html)
