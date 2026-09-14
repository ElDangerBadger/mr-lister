# Judge access through the existing seller pool

Status: implementation only; not deployed or live-tested. `template.json` creates an
admin-provisioned Lite-tier judge directory, confidential OIDC client, classic hosted domain, broker
secret, and optional `MrListerJudge` identity provider in the supplied primary seller pool.
It creates no API, IAM role, user, seller group membership, account link, or provider grant.

The seller pool keeps MFA **ON** for its local users. The judge pool has MFA **OFF** and
administrator-only password provisioning/recovery. AWS delegates federated authentication
to its IdP without adding another MFA factor. [AWS MFA behavior](https://docs.aws.amazon.com/cognito/latest/developerguide/user-pool-settings-mfa.html)
Cognito pools are OIDC issuers, so the primary pool can broker this directory and issue its
existing tokens. The API issuer, browser client ID, seller scope, and owner derivation stay
unchanged. [AWS federation endpoints](https://docs.aws.amazon.com/cognito/latest/developerguide/federation-endpoints.html)

## Staged creation and secret binding

Supply `EnvironmentName`, the exact existing `PrimaryUserPoolId`, `PrimarySignInOrigin`,
`ApplicationOrigin`, and an unused `JudgeDomainPrefix`. Check the primary pool/domain tuple
against fresh live configuration in the same account and Region. Neither origin includes a
trailing slash. Keep these identifiers stable on subsequent updates.

1. Create a change set with `EnableFederation=false` and an empty `BrokerSecretVersionId`.
   This creates only the judge pool, client, domain, and empty secret. Review resource scope
   and deploy it separately from the existing application stack.
2. Resolve the new pool/client/secret ARN from stack outputs. In an operator SDK process,
   call `DescribeUserPoolClient` for that exact pair and transfer its generated `ClientSecret`
   directly to `PutSecretValue` on that exact secret ARN. Store JSON containing `client_id`
   and `client_secret`; check the returned client ID and use a unique request token. Keep
   values in memory and record only the tuple and returned secret VersionId. Do not capture
   the raw client or IdP describe response: those APIs can return the secret.
3. Update this stack with `EnableFederation=true` and that exact `BrokerSecretVersionId`.
   The IdP uses a Secrets Manager dynamic reference pinned to the populated version. The
   CloudFormation execution identity needs read access to that exact broker secret.
   Confirm OIDC discovery is available before enabling the primary browser client.

CloudFormation's app-client resource supports `ClientId`, not a `ClientSecret` GetAtt.
The empty-secret stage avoids inventing a client secret or exposing it in outputs.
[App-client return values](https://docs.aws.amazon.com/AWSCloudFormation/latest/TemplateReference/aws-resource-cognito-userpoolclient.html#aws-resource-cognito-userpoolclient-return-values),
[empty Secrets Manager resource](https://docs.aws.amazon.com/AWSCloudFormation/latest/TemplateReference/aws-resource-secretsmanager-secret.html#cfn-secretsmanager-secret-secretstring).
Secret rotation or client replacement requires transferring the matching new value and
updating the pinned version; changing a secret alone does not update the IdP.
[Dynamic reference updates](https://docs.aws.amazon.com/AWSCloudFormation/latest/UserGuide/dynamic-references-secretsmanager.html)

## Existing-client and application integration

Make a separate reviewed update from the fresh live primary app-client configuration,
preserving every unrelated property and its existing client identity:

- Add `MrListerJudge` to `SupportedIdentityProviders`, retaining `COGNITO` and other providers.
- Add `${ApplicationOrigin}/judge/auth/callback` to callbacks, retaining the normal callback.
- Add `${ApplicationOrigin}/judge/` and `${ApplicationOrigin}/judge/signout` to logout URLs,
  retaining the normal logout destination.
- Confirm its mapped `email` and `email_verified` attributes are writable. Preserve existing
  default attribute permissions when the client has no explicit list; do not replace them
  with a narrowed list that breaks existing sign-in.

The judge broker client's callback is **the primary Cognito domain's
`/oauth2/idpresponse`**, not the application callback. The judge UI at `/judge/` loads the
same application build and primary authorize/token endpoints and client ID; its runtime
configuration adds `identity_provider=MrListerJudge` and its own application callback.
Retain PKCE and state validation. The browser never needs the broker secret or tokens issued
by the judge pool. [OIDC provider configuration](https://docs.aws.amazon.com/cognito/latest/developerguide/cognito-user-pools-oidc-idp.html)

Provision a judge account through admin APIs with a dedicated email distinct from existing
sellers, a verified email attribute, and a permanent password. Do not enable self-sign-up,
install an auto-link trigger, call `AdminLinkProviderForUser`, or assign a native password to
the broker-generated primary-pool user. On first federation, inspect the generated primary
user and its `identities` entry; verify the `MrListerJudge` provider and upstream subject.
Use its actual primary-pool `sub`, not the upstream subject or inferred username, for
`SHA256(primary issuer + NUL + primary sub)` ownership. Cognito prefixes federated usernames
and may normalize case. [Attribute mapping](https://docs.aws.amazon.com/cognito/latest/developerguide/cognito-user-pools-specifying-attribute-mapping.html)

Manually add only that verified broker user to the primary `seller` group and provision its
exact owner connection grant. Preserve the existing owner's identity, data, and connection.
Live Cognito identity metadata can encode the primary marker as Boolean `true` or the
exact string `"true"`; validate the provider and upstream subject independently. Cognito
also creates a pool/provider-named group for the federated user. Preserve that exact
expected group after confirming it has no IAM role, and reject unrelated groups.
Obtain fresh primary tokens after group assignment. This judge is authorized for the full
seller workflow, including publication through the normal approval and publication guards;
do not attach the evaluator deny-publication policy. No group or provider authority comes
from an email match, browser parameter, or this template.

## Clear both sign-in sessions

The primary `/logout` endpoint does not clear an OIDC provider session.
[AWS logout behavior](https://docs.aws.amazon.com/cognito/latest/developerguide/logout-endpoint.html)
Use a fixed two-hop browser flow:

1. Clear the judge application's token and pending-auth state. Navigate to the primary
   `/logout` with the existing primary client ID and the encoded logout destination
   `${ApplicationOrigin}/judge/signout`.
2. That public relay navigates to the judge domain's `/logout`, supplying the public
   `JudgeBrokerClientId` and encoded destination `${ApplicationOrigin}/judge/`.

The relay must use trusted configured URLs, accept no arbitrary return URL, and avoid
automatic sign-in on the final page. No secret is needed for either logout request. This
keeps nested query-string URLs out of Cognito's allowlists. Browser session cleanup does not
revoke already issued tokens; retiring judge access also requires disabling the judge user,
revoking its sessions, and removing its exact group/connection grants under the normal
operational procedure. Retain the seller account and its MFA configuration.

## Validation and deployment boundary

Run `.venv/bin/python infra/phase6/judge-access/test_template.py` for local auth/secret and
resource-boundary checks. The six checks and local `cfn-lint --regions us-west-2` passed during
implementation. Validate the parameterized change set before deployment; local checks do not
prove federation or hosted UI availability.

The live check must prove password-only judge login, primary-issuer tokens with the existing
client/scope and verified seller group, separate owner-scoped jobs, the intended connection,
both-cookie logout, and unchanged MFA sign-in for the original seller. Publication is
available by policy; a test must not claim it was exercised unless a real approved publish
was explicitly performed. This directory performs none of those live operations.
