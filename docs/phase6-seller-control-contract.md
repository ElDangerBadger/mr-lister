# Phase 6 seller-control contract

Phase 6 turns the proven preparation pipeline into a reviewable seller experience. It does not
publish a marketplace listing, submit an order, or send a product to fulfillment.

This document freezes the product, state, security, commercial, and acceptance boundaries before
implementation begins. ADR 0008 remains the authority rule: application commands and atomic
storage conditions decide whether a state transition is valid. ADR 0009 defines the persisted
human-review pause.

## Phase exit

An authenticated first-time seller uploads one supported owned PNG, receives real listing
intelligence and exactly one real unpublished Printify product, can recover the job after refresh,
review artwork, mockups, listing content, product settings, validation, and transparent economics,
make multiple validated edits without creating another product, and approve the exact current
version or cancel without operator access. No reachable Phase 6 route, command, task, client
operation, or ordinary application path can publish, order, or fulfill.

## Supported seller journey

```text
Sign in
  -> Upload one supported PNG
  -> Watch durable preparation progress
  -> Resolve validation errors, if any
  -> Review artwork, mockups, listing, product, and estimated proceeds
  -> Edit and synchronize the same Printify draft, if desired
  -> Approve the exact current version or cancel
  -> See an explicit approved-not-published or cancelled-not-published result
```

The authenticated home restores the seller's current or recent draft. A browser-local job URL is
not the recovery mechanism.

## Input envelope

The Phase 6 interface accepts:

- one seller-owned file whose basename ends in `.png` and whose declared type is `image/png`;
- `1..5 MiB` of fully decodable PNG bytes with a valid signature, IHDR, and checksum;
- equal width and height for the calibrated square-placement path;
- each dimension in `1..20,000` pixels and at most 100,000,000 decoded pixels;
- an alpha range whose minimum is below 255 and maximum is above zero—at least one transparent
  pixel and at least one visible pixel.

The browser uploads directly to a private S3 key. The API does not proxy artwork bytes. Phase 6
then uploads the exact pinned version through Printify's bounded base64 path. Files above 5 MiB are
deferred until a short-lived URL implementation binds the exact S3 bucket, key, and `VersionId`
and passes a live test; arbitrary artwork URLs are never accepted.

SVG remains a planned input type, not a Phase 6 control. Supporting it requires a separately
bounded PNG inspection rendition while preserving the SVG as the production artifact. Non-square
artwork remains deferred until placement `y` is computed deterministically from validated source
and print-canvas dimensions.

## Fixed product policy

The initial seller profile is read-only in Phase 6:

- Gildan 64000 Unisex Softstyle T-Shirt;
- SwiftPOD print provider;
- front DTG;
- Black, Charcoal, Dark Chocolate, Navy, and Sand;
- S, M, L, XL, 2XL, and 3XL;
- 30 verified variants;
- retail price `2999` cents for every variant;
- buyer-facing free shipping;
- calibrated square-artwork placement at `x=0.5`, `y=0.25`, `scale=0.65`, and `angle=0`.

These settings are presented as information, not disabled form controls. Product selection,
provider selection, variants, placement, price, and shipping policy are not editable in this
phase.

## Seller-visible states and actions

The API returns a seller projection rather than exposing orchestration details. Only upload byte
progress is numeric; preparation uses honest named stages.

| Display state | Meaning | Primary action | Other actions |
| --- | --- | --- | --- |
| Signed out | No authenticated session | Sign in | None |
| Upload ready | Seller can create a job | Create listing draft | Choose or replace PNG |
| Uploading | Browser is sending bytes to S3 | None | Cancel upload |
| Preparing | Intake, intelligence, validation, upload, or draft creation is running | None | Request cancellation |
| Needs revision | Current listing has blocking validation issues | Edit listing | Cancel draft |
| Synchronizing | A valid revision is updating the existing Printify draft | None | Request cancellation |
| Reconciling | A provider write may have completed and is being read back safely | None | Cancel draft, unless already requested |
| Ready for review | Current valid version is synchronized and awaiting a decision | Approve version N | Edit listing; Cancel draft |
| Refreshing estimate | Provider cost or shipping evidence is being refreshed | None | Request cancellation |
| Approved | Exact current version is authorized for later publication | None | None in Phase 6 |
| Cancelling | In-flight machine work must settle or reconcile | None | None |
| Cancelled | Job ended without publication | Start another draft | None |
| Retryable failure | A safe recovery command is available | Retry | Cancel draft |
| Terminal failure | The job cannot safely continue | Start another draft | None |
| Version conflict | Another command changed the review first | Review latest version | Preserve local edits for copy/reapply |

Every review and result view carries a persistent `Unpublished — not on Etsy` authority banner.
There is no Phase 6 Publish control.

## Seller command contract

Every command is owner-scoped, version checked, idempotent, and committed by application code.
The authenticated owner is derived from the JWT and is never accepted from a request body.

| Command | Preconditions | Atomic application result | External side effect |
| --- | --- | --- | --- |
| Create upload intent | Authenticated seller; valid declared metadata; new owner-scoped idempotency key | Owned upload intent, reserved job ID, and exact S3 key; no job yet | Short-lived presigned upload only |
| Reauthorize upload | Owned active empty intent; prior form expired | Same stable upload intent | New short-lived form for the same exact key |
| Cancel upload | Owned unconsumed intent | Irreversible intent cancellation/expiry | Queued deletion of any staged object; no job or workflow |
| Complete upload | Owned open intent; S3 object and version match key, checksum, size, type, and decoded constraints | Intent consumed; job and intake claim created with deterministic preparation execution identity | Start preparation once |
| Save listing revision | `AWAITING_APPROVAL` or `NEEDS_REVISION`; exact review version and fingerprint | Immutable review N+1; prior version superseded; sync requested only if valid | One guarded initial POST when no product exists; otherwise bounded PUT of the same product |
| Refresh economics | `AWAITING_APPROVAL`; exact review version/fingerprint; economics missing or stale | `PRICING_REFRESHING` and one refresh work request; no new listing review | Read current product/catalog costs and shipping, then store a new pricing snapshot |
| Approve | `AWAITING_APPROVAL`; exact current version/fingerprint; validation passed; product synchronized; economics complete/current | Immutable approval decision and `APPROVED` | None |
| Cancel | Owned nonterminal pre-approval job; exact record version | `CANCEL_REQUESTED` while work is in flight, otherwise `CANCELLED`; terminal reason recorded | No deletion, publication, order, or fulfillment |
| Retry | Retryable state and server-advertised recovery action | One deterministic recovery claim | Only the bounded failed step |

Saving edits is the request-changes operation. `Discard edits` is browser-local and makes no API
call. `Cancel draft` is the only terminal rejection before approval. The unpublished Printify
product is retained by default and the confirmation/result copy states that behavior. `APPROVED`
is terminal for Phase 6; withdrawing an approval is deferred until Phase 7 can define its race
against publication.

Cancellation cannot make an in-flight third-party request disappear. The seller command stores an
immutable cancellation-intent decision that permanently disables edit, approval, and seller retry.
The job may remain `CANCEL_REQUESTED` while machine work settles. A worker that observes the intent
before its write skips the write. If a write outcome is already ambiguous, automatic readback runs
under `RECONCILIATION_REQUIRED`; the UI remains `Cancelling` until the unpublished provider state
is known and the job commits `CANCELLED`.

Every mutation carries an owner-scoped `Idempotency-Key`. Review-sensitive commands also carry the
expected review version and a public review ETag. The application transaction conditions on its
internal record version and atomically stores the command receipt, domain event, new review or
decision, job update, and bounded work request. Provider and AWS calls occur after that transaction
through claimed work; they never occur inside the seller-command transaction.

Each bounded work request is a transactional outbox record with a deterministic Standard Step
Functions execution name derived from its immutable work-request ID. A DynamoDB Stream dispatcher
claims and starts it; a scheduled sweeper re-drives due `PENDING` or expired claims. Replaying a
successful `CommandReceipt` conditionally marks any still-pending referenced request due now; only
the dispatcher starts executions. `ExecutionAlreadyExists` is success only after the dispatcher
verifies the exact execution name and input fingerprint. A failure between the application
transaction and `StartExecution` can therefore delay work, but cannot lose it or create a second
logical execution.

The approval fingerprint hashes the exact immutable review, product ID, synchronized product
fingerprint, and pricing-snapshot ID presented to the seller. Operational fields may change
without altering it, while any seller-relevant difference makes approval stale.

Stable seller-safe failures include `STALE_REVIEW`, `IDEMPOTENCY_CONFLICT`, `SYNC_IN_PROGRESS`,
`RECONCILIATION_REQUIRED`, `RETRY_NOT_ALLOWED`, `UPLOAD_EXPIRED`, and `ARTIFACT_INTEGRITY`.
Ownership mismatch and absence both return `NOT_FOUND`.

## Phase 6 state graph

Upload intent state is separate from the job and uses `UPLOAD_PENDING`, `VERIFIED`, and `EXPIRED`.
The active job path adds explicit synchronization, reconciliation, and cancellation states while
keeping Phase 7 publication states unreachable:

```text
INTAKE_VALIDATED
  -> ANALYZING_ARTWORK
  -> LISTING_DRAFTED
       | invalid -> NEEDS_REVISION
       ` valid   -> PRODUCT_DRAFT_SYNCING
                       | confirmed -> AWAITING_APPROVAL
                       | ambiguous -> RECONCILIATION_REQUIRED
                       ` transient -> FAILED_RETRYABLE

NEEDS_REVISION
  | invalid edit -> NEEDS_REVISION with immutable review N+1
  ` valid edit   -> PRODUCT_DRAFT_SYNCING with immutable review N+1

AWAITING_APPROVAL
  | valid edit   -> PRODUCT_DRAFT_SYNCING with immutable review N+1
  | invalid edit -> NEEDS_REVISION with immutable review N+1
  | stale price  -> PRICING_REFRESHING -> AWAITING_APPROVAL
  | approve      -> APPROVED
  ` cancel       -> CANCELLED

active machine work -> CANCEL_REQUESTED -> reconciled/settled -> CANCELLED
```

Additional legal recovery transitions are closed rather than open-ended:

- any nonterminal pre-approval state may accept cancellation; an idle state moves directly to
  `CANCELLED`, while active or ambiguous work moves to `CANCEL_REQUESTED`;
- `CANCEL_REQUESTED` may move only to `RECONCILIATION_REQUIRED` with cancellation intent or to
  `CANCELLED`;
- `RECONCILIATION_REQUIRED` may stay pending on transient read failure. For an ambiguous PUT, an
  existing product at the target payload moves to `AWAITING_APPROVAL`; the same product still at
  the exact prior canonical payload may resume the same idempotent PUT; a missing, wrong, or
  conflicting product fails terminally and never falls back to creation;
- an ambiguous initial POST with exactly one matching correlation token and canonical payload
  succeeds, while multiple/conflicting matches fail terminally. Zero matches are read again only
  until the persisted reconciliation deadline. At the deadline the job becomes
  `FAILED_TERMINAL/PRODUCT_CREATE_OUTCOME_UNKNOWN`, or `CANCELLED` with an explicit
  `provider_outcome_unconfirmed` flag when cancellation intent exists. Neither result may create,
  retry, edit, or approve that job;
- `PRICING_REFRESHING` may move only to `AWAITING_APPROVAL` with a new current
  `PricingSnapshot`, to `FAILED_RETRYABLE` with a pricing-refresh resume action, or through the
  cancellation path;
- `FAILED_RETRYABLE` may move only to its persisted resume state through `Retry`, or accept
  cancellation;
- once cancellation intent exists, no reconciliation result can restore edit or approval actions.

`APPROVED`, `CANCELLED`, and `FAILED_TERMINAL` are terminal in the Phase 6 surface. `PUBLISHING`,
`PUBLISHED`, and `VERIFIED` remain overall-domain states but cannot be requested or reached by any
Phase 6 route, role, task, or client method. `PRINTIFY_DRAFT_CREATED` is replaced in the new Phase 6
contract by synchronization state that also describes later product updates.

## Review and decision records

Phase 6 durable records use contract version `2.0.0`. Version `1.0.0` remains readable only by the
retained Phase 4/5 evidence tooling; the `/v1` seller API does not upcast ownerless legacy rows or
mix record versions in the Phase 6 table.

Phase 6 separates immutable review content from mutable operational pointers. The seller review
projection joins the current `ReviewContent`, `ProductSyncRecord`, and `PricingSnapshot`; none is
mutated to add information that arrives later:

- `JobRecord`: owner, current review version, approved version, product ID, synchronized version,
  active execution, record version, canonical state, cancellation intent, and failure/recovery code.
- `ReviewContent`: immutable artwork analysis, listing, product-profile snapshot, validation,
  actor, timestamp, and fingerprint.
- `ReviewDecisionRecord`: approve or revise; actor; mandatory exact review version/fingerprint;
  timestamp; and command idempotency key.
- `CancellationDecisionRecord`: actor, exact expected `JobRecord` version, optional current review
  reference when one exists, timestamp, command idempotency key, and immutable cancellation intent.
- `ProductSyncRecord`: immutable review version, provider product/image IDs, payload fingerprint,
  mockups, variant prices/costs, provider lock/publication flags, and synchronization timestamp.
- `PricingSnapshot`: immutable product-sync fingerprint, retail, production costs, shipping rate,
  marketplace-fee policy, currency, source/effective times, and estimated-proceeds range.
- `UploadIntent`: expected owner, key, checksum, size, type, expiry, and completion status.
- `CommandReceipt`: owner-scoped idempotency key, request fingerprint, and prior response.
- `WorkflowExecutionRecord`: deterministic execution identity, bounded purpose, and status.
- `WorkRequest`: transactional-outbox status, exact work type/input fingerprint, deterministic
  execution identity, claim lease, attempt metadata, and next dispatch time.
- `FailureRecord`: sanitized code, stage, retryability, and advertised recovery action.

Display state, named progress stage, and allowed actions are derived server-side from canonical
records. They are never persisted as a second transition authority.

Legacy Phase 4/5 rows are ownerless and use different approval/review semantics. They fail closed
at the Phase 6 boundary and are not silently migrated.

## Same-product synchronization

Product existence, not review number, selects the operation:

1. If the first valid review has no persisted product ID, synchronization claims the one allowed
   initial POST. This includes a valid human revision after an invalid generated review.
2. If the product ID exists, synchronization retrieves the product and fails closed if it is
   locked, published, belongs to another shop, or no longer matches the configured blueprint and
   provider.
3. Every such later synchronization PUTs the complete canonical payload to that exact product ID.
4. A successful operation persists the synchronized review version, request/response fingerprints,
   selected mockups, costs, provider state, and timestamp.

The canonical initial payload places a deterministic, non-secret job correlation token in each
variant SKU. If the POST result is ambiguous, no automatic POST retry is allowed. Reconciliation
lists recent shop products, matches the correlation token, and verifies the complete canonical
payload. Exactly one match completes the write; multiple or conflicting matches fail terminally.
No match remains `RECONCILIATION_REQUIRED` only until a persisted deadline, then ends as the
explicit unresolved result defined above. The one POST may therefore produce zero or one product,
but never authorizes a second POST for that job.

An ambiguous PUT is reconciled by GET and canonical-field comparison before retry. Only the exact
prior canonical payload may authorize one exact same-product PUT retry. That attempt inherits the
root write's persisted reconciliation deadline and increments an immutable retry count; an expired
deadline or second ambiguous PUT ends terminally without another mutation. There is no POST
fallback. Fake adapters count POST and PUT separately so an offline test cannot conceal duplicate
creation.

## Consolidated review projection

The review response contains only seller-facing, owner-authorized data:

- job ID, record version, review version, review fingerprint, display state, and named stage;
- allowed actions with disabled reasons;
- short-lived original-artwork preview URL;
- a bounded representative mockup set with readiness state;
- title, description, and exactly 13 tags;
- structured validation paths such as `title` and `tags[3]`;
- artwork interpretation, visible wording, visual elements, and safety notes;
- human-readable product/provider, colors, sizes, placement, and retail price;
- provider synchronization state and timestamp;
- estimated-proceeds snapshot and assumptions;
- sanitized failure and recovery information.

Outside the short-lived direct-upload form, the API never returns storage keys. It never returns
task tokens, provider credentials, authorization headers, raw provider bodies, or controls outside
the server-advertised capability set.

## Commercial estimate

The UI says `Estimated proceeds`, not `profit` or guaranteed margin:

```text
customer retail price
- Printify production cost by variant
- seller-funded standard first-item shipping to the United States
- versioned Etsy US marketplace-fee estimate
= estimated seller proceeds before taxes, ads, discounts, refunds, and currency effects
```

Phase 6 freezes pricing policy `etsy-us-standard-v1`:

- seller bank country, buyer destination, listing currency, and payment-account currency: United
  States / United States / USD / USD;
- retail price: `2999` cents and buyer-charged shipping: `0` cents;
- production cost: live Printify product variant `cost`;
- fulfillment shipping: live standard first-item US rate for the configured blueprint, provider,
  and variant;
- Etsy listing/auto-renew allocation: `20` cents per sold item;
- Etsy transaction fee: 6.5 percent of item price plus buyer-charged shipping;
- Etsy Payments estimate for a US bank account: 3 percent of item price plus buyer-charged
  shipping, plus `25` cents;
- percentage components round independently to cents using integer half-up arithmetic;
- sales tax in the payment-processing basis, Offsite Ads, regulatory fees, VAT, deposit fees,
  currency conversion, ads, discounts, and refunds are excluded and disclosed.

The fee constants were checked on 2026-08-21 against Etsy's
[Fees & Payments Policy](https://www.etsy.com/legal/fees/) and
[Etsy Payments Policy](https://www.etsy.com/legal/etsy-payments/). They are estimates, not a claim
about the seller's eventual statement.

Additional rules:

- all values use integer minor units and one declared currency;
- the projection shows a range across the 30 variants and an expandable per-size breakdown;
- buyer shipping is shown as `$0` while seller-funded fulfillment shipping is separate;
- every component carries a source and as-of/effective time;
- provider cost/shipping data older than 24 hours is stale at approval;
- missing or stale components remain unknown and block approval;
- stale economics advertises `Refresh estimate`; that owner/version-checked command enters
  `PRICING_REFRESHING` and obtains a new snapshot without fabricating a listing revision;
- an approval attempt against stale economics returns `ECONOMICS_STALE` and the refresh action; it
  does not record approval or silently start external work;
- before approval, a policy or provider-cost change creates a new immutable `PricingSnapshot` and
  therefore a new review ETag;
- after approval, the approved pricing snapshot remains immutable. Phase 7 must revalidate current
  economics before publication and refuse on drift until a future withdrawal/reapproval command is
  defined.

## Cloud and identity boundary

The Phase 6 cloud surface is deliberately small:

```text
Cognito managed sign-in
  -> private S3/CloudFront static application
  -> API Gateway HTTP API with JWT authorization
       -> upload API Lambda
       -> review-query API Lambda
       -> seller-command API Lambda
       -> bounded preparation/synchronization workers
            -> DynamoDB, private S3, AgentCore/Bedrock, Printify
```

Initial identity scope:

- one invite-only Cognito User Pool seller;
- authorization code plus PKCE through Cognito managed login;
- a public SPA client with no client secret or Cognito Identity Pool;
- public signup disabled, membership in the `seller` group required, and TOTP MFA required;
- access tokens, not ID tokens, authorize the dedicated Mr Lister API scope;
- 60-minute access/ID tokens and a 30-day refresh token;
- exact production callback/logout origins, with localhost present only in the development stack;
- immutable `owner_id = sha256(issuer + NUL + sub)` on jobs and owner-prefixed S3 keys;
- ownership checks before reads, commands, presigning, or preview signing;
- identical `404` response for unknown and cross-owner job access;
- the concrete browser transport and content policy below;
- no AWS or Printify credential in browser code;
- only an information-minimal `/health` route is public.

Create-upload idempotency is keyed by owner, `CREATE_UPLOAD`, and caller key, then atomically binds
the generated intent and reserved job ID. Job mutations add command type and job ID. A command
receipt stores only stable resource data—never expiring presigned fields. Reauthorization returns
a new form for the same active intent. An owner GSI supports recent-job recovery without scanning
the table. Internal workers receive a job ID, load ownership from DynamoDB, and never trust identity
forwarded in a workflow payload. Logs use only a stable owner digest and never email addresses or
tokens.

The SPA holds access and refresh tokens in memory only and sends the access token in the
`Authorization: Bearer` header. Tokens never enter URLs, local storage, session storage, IndexedDB,
logs, or analytics. Session storage may hold the one-use PKCE verifier and a validated same-origin
return route only until callback completion. Reload can return through Cognito's managed session to
the same route.

API CORS permits only the exact configured application origin, methods `GET`, `POST`, `PUT`, and
`OPTIONS`, request headers `Authorization`, `Content-Type`, `Idempotency-Key`, and `If-Match`, and
exposed headers `ETag`, `Retry-After`, and `X-Request-Id`. Credentials are disabled and wildcard
origins are forbidden. S3 CORS separately permits only the exact app origin and headers/methods
required by the presigned upload.

CloudFront sets `Referrer-Policy: no-referrer` and this CSP, with deployment placeholders resolved
to exact origins:

```text
default-src 'self';
script-src 'self';
style-src 'self';
img-src 'self' data: blob: https://images.printify.com https://<artifact-bucket-origin>;
connect-src 'self' https://<api-origin> https://<cognito-origin> https://<artifact-bucket-origin>;
form-action 'self' https://<cognito-origin> https://<artifact-bucket-origin>;
font-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none';
```

`unsafe-inline` and `unsafe-eval` are forbidden. Mockup URLs must use HTTPS and the exact
`images.printify.com` host; accepted URLs render directly under the no-referrer policy, while every
other host is rejected and projected as unavailable.

### Private upload and retention

The server generates the exact source key:

```text
private/owners/{owner_id}/jobs/{job_id}/source/source.png
```

Original filenames are display metadata and never become key segments. The private encrypted
bucket has versioning enabled, public access blocked, bucket-owner-enforced ownership, TLS-only
access, and no runtime `ListBucket`. A five-minute presigned POST fixes that key, `image/png`,
checksum, server-side encryption, and a `1..5 MiB` content-length range; a bucket policy rejects a
signature age beyond five minutes. The form necessarily contains this one exact opaque key and no
arbitrary-key capability.

Completion reads the object once, independently validates its bytes, checksum, dimensions,
transparency, and visual content, and requires a non-null S3 `VersionId` before creating the job.
Every worker and preview thereafter reads the pinned version, so a later overwrite cannot change
the reviewed artifact.

Abandoned upload intents and their objects expire after one day. Active, retryable, and `APPROVED`
jobs retain their pinned source and operational records; `APPROVED` is not overall-terminal because
Phase 7 still needs it. A reference-aware sweeper—not a blanket noncurrent-version rule—deletes the
exact pinned source 30 days after an overall `CANCELLED` or `FAILED_TERMINAL` result and deletes
operational records after 90 days. Application logs retain for 14 days. Provider mockup URLs are
metadata, not copied into a public bucket.

Fixed-connection revocation is an administrator runbook: disable new intake/sync first, let
in-flight writes settle or reconcile, revoke the PAT in Printify, remove the Secrets Manager value,
and place remaining dependent jobs in a sanitized non-retryable `CONNECTION_REVOKED` result. It
never deletes source artwork or provider products as a side effect.

### Runtime capability split

| Component | Required capabilities | Explicitly absent |
| --- | --- | --- |
| Upload API | Owner-scoped upload/job transactions, exact-key presigning, pinned-object verification, start exact preparation workflow | Secrets, AgentCore, Bedrock, Printify |
| Review query API | Owner-scoped DynamoDB query/get and exact-version preview signing | Writes, workflow starts, secrets |
| Seller command API | Owner/version-conditional transactions and due-now nudges for its exact outbox records | Artwork bytes, secrets, AgentCore, provider calls, workflow starts |
| Work dispatcher/sweeper | Claim due outbox records and start/describe deterministic executions in the exact Phase 6 state machines | Seller commands, S3, secrets, AgentCore, provider calls, business-state assignment |
| Preparation dispatcher | Owner/job reads and exact AgentCore invocation | Artwork bytes, Printify secret, provider calls |
| Draft-sync worker | Pinned artwork read, workflow transactions, exact Printify secret | AgentCore/Bedrock and publication surface |
| AgentCore runtime | Job-scoped workflow code, Phase 6 table transactions, pinned source read, exact controller and intelligence model invocation | Secrets Manager, Step Functions, Printify, approval, publication |
| State machine | Invoke exact Phase 6 machine-work handlers | S3, secrets, provider access, business-state assignment |
| Developer/deployment roles | Package and deploy only named Phase 6 resources through the execution role | Seller artifacts/table rows, secret values, seller commands, production canaries outside the authenticated API |

The preparation dispatcher sends only the job ID and fixed mode to the exact AgentCore runtime.
The runtime builds the bounded Strands tools over Phase 6 DynamoDB/S3 adapters and the selected
Bedrock intelligence adapter. Its role can read only pinned source objects and operate on the Phase
6 workflow table; it has no marketplace credential or approval command. The model receives tool
results, not AWS credentials, S3 authorization, or arbitrary object access. Product upload/create/
update runs only afterward in the separate draft-sync worker.

This is mandatory in the submission deployment: `PREPARE` fails closed if the exact AgentCore
Strands runtime is unavailable. It cannot bypass the runtime or fall back to a direct model,
deterministic preparation implementation, or another non-Strands path. Application code and
DynamoDB remain authoritative for every state transition before and after the agent call.

Phase 6 deploys into separately named `mr-lister-phase6-*` application resources and a new table;
Phase 4/5 data remains read-only evidence. One administrator-side bootstrap update creates the
Phase 6 CloudFormation execution role and developer deployment policy. All normal packaging,
change-set review, deployment, canaries, and operations then return to the scoped developer
identity.

The internet-facing read API does not need Printify secret access. Only the draft-sync worker can
read the exact Printify secret. The [Printify API reference](https://developers.printify.com/API-Doc-RREdits.html/1000)
places create, update, delete, and publish under the Product resource, so IAM and token scope alone
cannot express the negative boundary. The Phase 6 deployment therefore omits
publish/order/fulfillment routes and tasks, exposes a draft-only client surface with an asserted
method/path allowlist, and gives no other component the secret.

The token-attaching HTTP boundary fixes the production base URL, rejects redirects, and permits
only required shop/catalog reads, artwork upload, initial product POST, product GET, and product
PUT. It denies product DELETE, publish/unpublish/publishing-status paths, orders, express production,
webhooks, and every unrecognized method/path pair. The PAT includes exactly `shops.read`,
`catalog.read`, `print_providers.read`, `products.read`, `products.write`, `uploads.read`, and
`uploads.write`. It omits every order and webhook scope.

Residual risk is explicit: compromise or malicious replacement of the trusted draft-sync worker
could misuse `products.write`. Phase 6 prevents unsafe seller, model, retry, and ordinary
application paths; it cannot make a bearer token safe from arbitrary code running inside its sole
trusted credential boundary.

## Public API shape

The initial versioned surface is:

- `POST /v1/uploads` — create an owned upload intent and reserve a job ID;
- `POST /v1/uploads/{upload_id}/authorize` — reissue upload authorization for an active intent;
- `POST /v1/uploads/{upload_id}/complete` — validate/pin the object, create the job, and start
  preparation;
- `POST /v1/uploads/{upload_id}/cancel` — cancel the intent and queue staged-object cleanup;
- `GET /v1/jobs` — retrieve the seller's current/recent jobs;
- `GET /v1/jobs/{job_id}` — durable progress/result projection;
- `GET /v1/jobs/{job_id}/review` — consolidated current review;
- `PUT /v1/jobs/{job_id}/review/listing` — save title, description, and tags with expected version;
- `POST /v1/jobs/{job_id}/economics/refresh` — refresh missing or stale economics for the exact
  current review;
- `POST /v1/jobs/{job_id}/approve` — approve the exact current review;
- `POST /v1/jobs/{job_id}/cancel` — request or complete cancellation;
- `POST /v1/jobs/{job_id}/retry` — perform only an advertised recovery;
- `GET /v1/jobs/{job_id}/artwork-preview` — authorize and issue a short-lived preview.

There is no deployed `/publish`, order, fulfillment, raw report, or arbitrary object route.

## Interface and accessibility contract

- Upload progress reports measured bytes; preparation reports ordered named stages.
- Stage changes use a polite live region and are not announced on every poll.
- Failed saves focus a validation summary whose entries link to the exact affected fields.
- Fields use native labels, `aria-invalid`, and `aria-describedby`.
- Tags are 13 labeled inputs, not chip-only controls.
- Errors and warnings use text and icons as well as color.
- Core actions remain keyboard operable with visible focus, WCAG AA contrast, reduced motion, and
  usable layout at 200 percent zoom.
- Model and seller text is rendered as text, never unsanitized HTML.
- Session renewal returns the seller to the same route and job.
- Unsupported controls are omitted. A disabled visible action must include a server-provided reason.

## Acceptance gates

Phase 6 cannot close until all of the following are evidenced:

- [ ] The same owner-scoped job's durable `PREPARE` work invokes the exact AgentCore Strands
      runtime, returns a strict structured decision, and emits a sanitized correlation joined to
      the consolidated review; an unavailable runtime fails closed with no non-Strands fallback.
- [ ] A valid upload survives refresh and reaches consolidated review.
- [ ] The deployed path proves the advertised 5 MiB maximum end to end; any future larger-file URL
      path must bind the exact S3 `VersionId` and pass a separate live acceptance test.
- [ ] Duplicate clicks and network retries create one job and at most one Printify product;
      ambiguous initial-create tests cover both zero-product and one-product outcomes without a
      second POST.
- [ ] Confirmed initial creation performs one POST and produces exactly one product; later valid
      revisions perform bounded PUTs and preserve the same product ID.
- [ ] Original artwork, representative mockups, listing, fixed product policy, validation, and
      estimated proceeds appear in one review.
- [ ] Invalid fields are identified precisely and cannot be approved.
- [ ] A stale edit or approval fails without discarding the seller's local edits.
- [ ] Concurrent revise, approve, and cancel commands have exactly one winner.
- [ ] Cancellation from processing, review, and retryable failure needs no
      administrator and leaves no running Phase 6 execution.
- [ ] Cancellation before the first `ReviewContent` exists is guarded by the exact job record
      version and does not require or manufacture a review fingerprint.
- [ ] Approval is idempotent, ends at `APPROVED`, and causes no publication, order, or fulfillment.
- [ ] Refresh, browser restart, another tab, and sign-in renewal recover correct durable state.
- [ ] Cross-owner reads, commands, and preview requests fail closed without resource disclosure.
- [ ] Missing, expired, wrong-audience, wrong-scope, and non-seller access tokens fail before
      application commands run.
- [ ] Modified or expired upload authorization, wrong bytes, and post-finalize object overwrite
      cannot change the pinned source artifact.
- [ ] Missing/stale economics is never shown as zero and cannot be approved.
- [ ] A stale estimate can be refreshed without changing listing content; a dispatch failure after
      its command transaction is recovered by the outbox sweeper without duplicate logical work.
- [ ] Draft-only client tests reject every unrecognized Printify method/path and specifically deny
      delete, publish, unpublish, publishing-status, order, fulfillment, and webhook requests.
- [ ] Policy assertions prove only the draft-sync worker can read the Printify secret and no secret,
      bearer token, raw owner identity, or authorization header enters responses or logs.
- [ ] Every visible control works, explains why it is unavailable, or is absent.
- [ ] Core flows pass keyboard, screen-reader, contrast, focus, and 200-percent-zoom checks.
- [ ] A gated live canary creates one product, edits it twice in place, and approves it. A final
      exact-product GET records the provider's unpublished/unlocked state, and the audited
      draft-client call ledger records zero publish, unpublish, publishing-status, order, or
      fulfillment method/path attempts. Any direct connected-shop inspection is supplemental,
      recorded manual evidence rather than an automated Phase 6 assertion.
- [ ] A second live canary cancels without root or administrator access.
- [ ] At least one first-time seller completes the deployed flow without external documentation;
      five moderated attempts are the evidence target.

## Deferred scope

- Etsy publication, verification polling, listing links, and publication-complete notification;
- orders and fulfillment;
- public signup, multiple sellers, stores, teams, roles, billing, or credential onboarding;
- editable product, provider, variant, placement, price, or shipping policy;
- SVG inspection and non-square placement;
- custom mockup generation or background replacement;
- arbitrary listing attributes and personalization;
- WebSockets, AppSync, bulk queues, analytics, trends, or keyword-performance promises;
- automatic deletion of an unpublished Printify draft during ordinary cancellation.
