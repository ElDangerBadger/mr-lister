# Judge demo access

The approved judge experience uses the full Mr. Lister application at `/judge/`,
with a dedicated login and workspace connected to the owner's existing Printify
store. Upload, review, pricing, approval, and confirmed publication remain available.
Publishing is a real store action, clearly stated in the interface. This is not the
historical evaluator policy that denied publication.

Implementation branch: `codex/judge-demo-access`, based on the tested pricing and
shipping release. `main` is unchanged. Live provisioning and end-to-end acceptance
are pending; this document does not claim a working judge account yet.

## Login and workspace

A separate administrator-provisioned Cognito directory provides password-only judge
authentication. The existing seller directory brokers that login through the
`MrListerJudge` OIDC provider and issues the existing API tokens. The seller's MFA
requirement remains **ON**. The judge directory does not allow public registration
or self-service account recovery.

The federated judge user must remain separate from the existing seller user. There
is no automatic account linking or email-based grant. An operator verifies the
upstream identity before granting the exact broker user the existing seller group.
The primary issuer and that user's actual subject derive its own owner ID, so the
existing API ownership checks protect both workspaces.

The UI keeps the same Light / Dark / Auto control and popup sign-in. Judge runtime
configuration contains public OAuth identifiers only. Its fixed callback and
two-stage logout remain inside the approved application and Cognito origins. No
password, broker secret, or Printify token belongs in public files.

See the [judge directory deployment procedure](../infra/phase6/judge-access/README.md).
`tools/prepare_judge_application_update.py` prepares the bounded existing-client and
CloudFront route changes from a fresh live template; it preserves seller MFA,
attribute permissions, API configuration, and unrelated resources.

## Explicit connection grant

The existing `phase6-printify-owner-v1` secret remains supported. The strict v2
format adds `delegated_owner_grants`, containing at most 16 distinct owner IDs and
canonical UTC expiration timestamps. It retains the primary owner's shop and token.
The requesting owner's identity is never replaced with the primary owner's ID.

Every provider resolution reads the current secret afresh. Missing, malformed,
removed, or expired grants fail closed. Publication also requires the normal exact
owner/shop match, saved review, approval, and separate confirmation. The synthetic
publication canary remains primary-owner-only.

Deploy compatible provider readers before introducing a v2 secret. For this release,
only Phase6's ProviderDraft function needs the new behavior; PreparationDispatch and
its AgentCore v7 endpoint/binding remain unchanged. The standard Phase718 release
updates its shared function package. Reverting to a v1-only reader requires first
removing delegated access and restoring a valid primary-only v1 secret.

## Judge handoff and acceptance

The account needs an email address or alias controlled by the owner, distinct from
their normal seller login. A generated judge password belongs only in the private
testing instructions. Judges do not need mailbox access, Printify credentials, or
an authenticator setup.

Provide sample artwork and a prepared listing owned by the judge identity. A seller's
existing listing is not a substitute for that prepared example. Keep access available
through judging; the intended connection grant ends after October 8, 2026. Afterwards,
disable the judge account, revoke sessions, and remove its seller group and connection
grant. Grant expiry itself stops new provider operations, not Cognito sign-in.

Before handoff, verify password-only login, fresh judge upload and saved review,
cross-owner access denial, real-store binding, both-cookie logout, and unchanged
seller MFA. Leave the sample unpublished unless a real publication is deliberately
approved. Record which live actions were actually exercised; enabled publication
alone does not prove a publication test occurred.

General signup, social login, self-service store connections, and product catalog
selection remain outside this demo release.
