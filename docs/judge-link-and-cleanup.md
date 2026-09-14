# Judge project link and automatic product cleanup

Status: local implementation and browser checks passed. This document is not a deployment receipt.
The existing credentialed judge site remains the live entry until the release is verified.

Local validation completed on 2026-09-14: 231 focused backend tests, frontend lint,
type checking and production build, 492 frontend tests before the final optional
cleanup-notice flag, and 45 focused frontend tests after that flag. An isolated
WebKit check of the compiled bundle passed entry, early fragment removal, no
pre-click redemption, judge routing, Light / Dark / Auto, real HTTPS cookie restore,
and sign-out followed by reload with a leftover cookie. It used local fixtures only;
there were no live seller writes. Live session issuance and Printify-to-Etsy cleanup
acceptance remain outstanding.

## Experience

The project URL opens `/judge/` with an opaque invitation in the URL fragment.
The app removes that fragment immediately, then offers **Enter judge workspace**.
Entering establishes the existing dedicated judge identity without asking the judge
to type a username, password, or authenticator code. The usual Upload → Review →
Publish workflow and Light / Dark / Auto display remain available. Publication
still creates a real listing in the owner's connected Printify/Etsy shop and requires
the normal saved review, approval, and final confirmation.

The invitation is itself an access credential. Someone with the complete URL can
redeem it until its expiry or redemption limit. It belongs in the hackathon access
instructions or project URL, never in source code, page markup, public runtime JSON,
analytics, or logs. Link previews do not redeem it. A direct `/judge/` visit can restore
an existing session but does not grant a new one without the invitation.

Each newly published judge product becomes eligible for deletion **30 minutes after
Mr. Lister first confirms the live listing**. The server checks due products every
minute and retries transient failures. This is an eligibility deadline, not a promise
that Printify and Etsy finish their asynchronous work at precisely 30:00. A judge
can close the browser without stopping cleanup.

Unpublished-draft retention is a separate policy awaiting the owner's decision.
The current cleanup boundary only accepts confirmed published products; it cannot
delete a product still being prepared, edited, approved, or published.

## Authentication boundary

The server holds one primary Cognito OAuth refresh token for the existing dedicated
judge identity in Secrets Manager. An operator obtains it once through the existing
federated authorization-code flow; no native password is assigned to the federated
primary user. Cognito supports refreshing these sessions at its
[token endpoint](https://docs.aws.amazon.com/cognito/latest/developerguide/token-endpoint.html).

Every token delivery validates the signature against the configured issuer's JWKS,
the exact primary subject/client/issuer, seller scope, and expected judge groups.
Existing seller API JWT validation and owner checks remain authoritative. The owner's
MFA, credentials, identity, connection, and seller login are unchanged.

The browser receives only an access token held in memory. An opaque Secure, HttpOnly,
SameSite=Strict cookie supports reloads and renewal. Refresh tokens never leave the
server. Separate DynamoDB records hold invitation and cookie digests, explicit
expiry, revocation, and bounded redemption counts. Conditional transactions prevent
concurrent redemptions exceeding the limit. Database TTL removes old records eventually;
authorization always checks expiry directly.

Sessions last at most four hours and cannot outlive their invitation or campaign.
Signing out revokes that broker session, not the shared primary refresh token.
Refresh rereads the session after the Cognito call so a concurrent logout wins.
The UI also blocks automatic restoration after cancellation or logout, including
a delayed entry response that might otherwise set a new cookie.

An already issued primary access token remains usable until its own expiry (currently
up to one hour). Invitation/session revocation stops further broker issuance; it does
not claim immediate revocation of existing bearer tokens at the unchanged seller API.
The 30-minute product viewing window is independent of these login lifetimes.

## Cleanup boundary

The worker reads only the configured judge owner's Mr. Lister jobs. It never searches
the Printify catalog for unfamiliar names or infers ownership from designs. Eligibility
requires an exact, validated chain of job, publication snapshot, provider authority,
positive publication observation, and result. Both the owner and shop must match the
configured campaign, and publication must have been requested after its start.
An old judge draft newly published during the campaign can qualify; previously
published products are not swept retroactively.

Each cleanup record freezes the product ID, job, shop, Etsy listing identity, evidence,
and deadline. A separate table retains conditional leases, retries, and audit history.
Before deleting, the worker rereads the source records and checks Printify's exact
product against its original content, artwork, variants, identifying SKUs, and channel
listing. A mismatch stops deletion and raises an operational failure for review.
Concurrent or duplicate invocations cannot independently spend the same live lease.

The worker resolves the primary shop connection so cleanup can continue after the
judge's delegated access expires. This does not authorize deleting primary-owner
jobs: the separate immutable judge publication evidence remains mandatory.

Deletion uses Printify's exact product DELETE endpoint. Printify documents that deleting
there also removes the connected sales-channel listing, including Etsy. No separate
Etsy connection is introduced. A controlled live acceptance must establish the API's
channel behavior before activation. A Printify 404 is recorded as provider absence,
not independently verified Etsy removal.
[Printify deletion guidance](https://help.printify.com/hc/en-us/articles/4483616585873-How-do-I-delete-a-product),
[Printify API](https://developers.printify.com/).

## Infrastructure and operator procedure

`tools/prepare_judge_session_infrastructure.py` generates a separate stack containing
the broker API, two narrowly scoped Lambda roles/functions, session and cleanup tables,
an empty seed secret, a one-minute schedule, and operational alarms. Sessions and the
schedule start disabled; cleanup starts in dry-run mode. The worker requires a
fingerprint matching the exact cleanup campaign before active deletion is accepted.
The existing seller stack receives only a dedicated CloudFront origin and exact
`/v1/judge-session/*` behavior. Its cookie and Origin forwarding policy is separate
from the existing seller API, with every cache TTL set to zero.

1. Capture the current stacks, source commit, runtime configurations, Cognito client
   settings, and exact owner/shop bindings. Build an ARM64 Python 3.12 package containing
   this source and explicit `PyJWT[crypto]` dependencies. Preserve the deployed seller
   functions, Cognito configuration, and normal runtime JSON.
2. Review the additive change set, then create the disabled stack. Populate the new
   seed secret from a controlled primary judge OAuth session. Obtaining and storing
   this credential requires the owner's explicit approval: automatic approval review
   rejected the initial attempt because the general design approval did not cover
   that concrete storage step. The planned setup retains a private 0600 temporary
   token-response file, transfers the refresh token to Secrets Manager, then removes
   the temporary credential file after successful seeding. Never commit it.
   Seed schema: `contract_version=judge-session-seed-v1`, `issuer`, `client_id`, `subject`,
   `owner_id`, `campaign_id`, `expires_at` (actual refresh-token expiry epoch), and
   `refresh_token`. The seed must remain valid through campaign expiry. Never print it.
3. Use `tools/prepare_judge_invitation.py` to create private 0600 files under
   `.mr_lister_private`. It defaults to 50 redemptions, caps the operator-created
   invitation at 100, and permits at most 31 days. Upload only its digest record with
   `attribute_not_exists(PK)`; keep the project URL private until acceptance succeeds.
4. Apply the bounded edge patch. Enable the broker, verify missing/invalid/expired
   invitation rejection, valid judge identity and scopes, logout/renewal, cache and
   cross-origin behavior. Enable only `judge_access.session_entry=true` in the judge
   runtime configuration after the matching web bundle is deployed. Set
   `judge_access.cleanup_after_minutes=30` only after the actual cleanup schedule is
   active; otherwise the UI does not promise timed removal.
5. Run the cleanup schedule in dry-run mode against live source data. Its activation
   cutoff excludes old publications. Confirm eligible IDs and deadline, and prove a
   known owner product never becomes a candidate.
6. Publish one clearly identified judge test through the normal workflow, retain its
   exact Printify and Etsy IDs, and wait for the real viewing window. Activate cleanup
   for the reviewed campaign only after all local and dry-run checks pass. Verify the
   targeted removal in Printify and its channel result; retain the receipt. Never
   substitute an arbitrary existing shop product for the controlled test.
7. Share the verified project URL. Retire invitation issuance and the broker at campaign
   end. Keep cleanup running until outstanding eligible products are removed or their
   failures have been reviewed. Then disable the schedule and retain its audit table.

No broad catalog sweep, Etsy credentials, general signup, self-service store connections,
or product-catalog expansion is included in this release.
