# Judge demo access

The approved judge experience uses the full Mr. Lister application at `/judge/`,
with a dedicated login and workspace connected to the owner's existing Printify
store. Upload, review, pricing, approval, and confirmed publication remain available.
Publishing is a real store action, clearly stated in the interface. This is not the
historical evaluator policy that denied publication.

Implementation branch: `codex/judge-demo-access`, based on the tested pricing and
shipping release. Judge access shipped from `de50940`; the upload-verification
function and shared website now use `70b9314`. `main` is unchanged.
The dedicated account and expiring store grant are provisioned. Credentials and
operator receipts remain in private files excluded from Git.

Live acceptance passed password-only sign-in, a fresh upload, a saved review, a
fresh-session reopen, original artwork and five mockups, and both-stage logout.
Direct Printify readback confirmed the saved draft, prices, and shipping in the
intended shop. Judge reads of a known seller job were denied. The judge page links
to the prepared example, which remains unapproved and unpublished. Publication,
reverse access using a live seller session, and the seller's MFA sign-in were not
retested in this acceptance run; seller MFA configuration remains **ON**.

## Upload reliability correction — September 13, 2026

The judge and normal routes shared the latest website build when a 7487 × 7487 PNG
failed upload completion. The separate UploadApi Lambda still had 512 MB, despite
the earlier 1024 MB setting for preparation and provider workers. Its failure log
confirmed `Runtime.OutOfMemory`; this was not stale judge UI or an account error.

Upload verification now decodes the PNG once and scans transparency in bounded
tiles, avoiding additional full-resolution copies. Original bytes, checksum,
dimensions, integrity checks, and the existing pixel limit are preserved. UploadApi
is deployed at **1024 MB**, with its existing 30-second timeout. All other nine
Phase6 functions, AgentCore v7, authentication, store grants, and both public runtime
configuration objects retain their previous bindings.

The shared UI now shows activity during validation, hashing, upload reservation,
transfer, final verification, and initial review loading. Motion stops on completion
or failure and respects reduced-motion settings. Gateway failures retain a bounded
AWS support reference and instruct users to check status before retrying.

Validation passed 410 web tests and 103 focused backend/infrastructure tests, plus
Chromium and WebKit activity checks for both normal and judge routes. Public index,
JavaScript, and CSS checksums match the deployed release on both routes.

The original failed upload was recovered with one completion request (`202 Accepted`)
and no repeated file transfer. Live verification took 2.444 seconds and peaked at
401 MB of the allocated 1024 MB. Subsequent read-only judge sign-in confirmed the
same job ready for review, its full-resolution original image and all five mockups
decoded, and approval enabled. No approval or publication was performed. Preparation
finished before that fresh sign-in, so live motion was not observed in this recovery;
the compiled Chromium/WebKit checks provide the animation evidence.

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

The primary Cognito login also exposes a `MrListerJudge` provider button beside the
normal credential form. Selecting it preserves the original OAuth callback, which
may be `/auth/callback` when sign-in began on the main site. After that code exchange,
the browser recognizes the issuer-derived judge group as a presentation hint,
loads the strict `/judge/runtime-config.json`, and switches the router and logout
configuration to the judge workspace while retaining the same memory-only session.
The companion must match the original OAuth client, endpoints, and scopes. It does
not grant permissions; API token validation and owner checks remain authoritative.
Both popup and same-tab completion handle this transition before authenticated
pages request private data. Cancellation, timeout, or an invalid companion must
leave the session unauthenticated.

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
