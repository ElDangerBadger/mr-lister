# Phase 6.3 consolidated seller review evidence

Phase 6.3 adds one strict, owner-scoped, read-only application projection over the Phase 6 durable
authority records. It does not introduce another lifecycle, write to the operational store, call a
model or provider, or grant publication authority.

Present-state note (2026-09-01): this projection is deployed in the exact draft-only closure
release. An authenticated current-release walkthrough exercised the same-job review, retry, and
approval path while preserving the unpublished boundary. Formal Phase 6.6 evidence artifacts not
produced by that walkthrough remain unclaimed.

## Seller review assembled

[`SellerReviewProjectionService`](../src/mr_lister/control/projection.py) checks ownership first,
then joins and verifies the exact current:

- pinned source and exact application-owned product profile;
- artwork interpretation;
- review content and deterministic validation result;
- Strands preparation evidence and prepared review version;
- same-product synchronization evidence;
- bounded representative mockups;
- complete estimated-proceeds snapshot and all 30 color/size rows; and
- sanitized failure and recovery evidence when applicable.

The projection's strict response models live in
[`projection_models.py`](../src/mr_lister/control/projection_models.py). They return the review
version/fingerprint and composite authority ETag separately from the Job record-version CAS. The
ETag remains a command/approval authority token; the future API must send `Cache-Control: private,
no-store` rather than treat it as a general representation cache validator.

## Durable presentation evidence

Product synchronization now persists the seller-readable facts that cannot be reconstructed safely
at query time:

- every enabled variant's trusted color, size, placement group, retail price, and observed cost;
- structured mockup URL, position, and variant coverage; and
- a deterministic `representative_mockups(limit=5)` selection that maximizes explicit variant
  coverage while retaining a stable result.

The exact configured profile remains authority for the five colors, six sizes, three placement
groups, 2,999-cent retail price, zero buyer shipping, and draft-only policy. Product and provider
display identities are derived deterministically from the fingerprinted profile ID and print-
provider ID. There is no separately mutable presentation file that could change what the seller
sees under an unchanged review ETag. A future friendly-name catalog must be explicitly versioned
and added to approval authority rather than silently rewriting the accepted Phase 5 profile v2.

## Seller capabilities remain application-owned

The response always contains exactly five possible capability names:

- edit listing;
- approve review;
- cancel job;
- retry job; and
- refresh economics.

Each carries an enabled flag and a stable server-owned reason. No commerce action is part of the
schema. `Refresh economics` is now a real owner-, Job-version-, review-, ETag-, and idempotency-bound
seller command. It is legal only for an exact current synchronized review with missing or expired
economics, and atomically queues `REFRESH_ECONOMICS` work. Approval independently requires at
least one representative mockup plus complete fresh economics; hiding a button is not the safety
boundary.

## Privacy and URL boundary

The response excludes owner identity, bucket/key/version, source checksum, image and variant IDs,
work/execution/receipt/attempt/permit IDs, credentials, authorization headers, provider bodies,
raw prompts or agent output, model rationales, token counts, and exception text.

Mockup URLs are revalidated at the read boundary. Only ASCII HTTPS URLs on the exact
`images.printify.com` authority are eligible; user information, ports, fragments, controls,
backslashes, malformed escaping, suffix hosts, and deceptive authorities are rejected. If any
stored mockup URL fails, the complete mockup set becomes unavailable and none of its URLs leave the
projection.

The original-artwork preview is a narrow authenticated port. The projection exposes only the fixed
`/v1/jobs/{job_id}/artwork-preview` route on one configured HTTPS application origin; it contains
no bearer grant, query string, or storage coordinate. The Phase 6.4 query boundary authenticates
the request, derives the owner from the JWT, checks job ownership before loading source authority,
and presigns only the source's exact bucket, key, and `VersionId` for at most five minutes. It then
returns a bodyless, non-cacheable `302` to that exact S3 GET, whose terminal response is also fixed
to `Cache-Control: private, no-store, max-age=0`. Artwork bytes do not pass through API Gateway, and
no KMS signing key or separately replayable preview grant is introduced. Any ownership,
source-authority, signing, URL, or expiry mismatch produces a fixed unavailable/not-found result
without revealing storage details.

## Verification

Accepted offline verification on 2026-08-22:

- all tests: **638 passed**; 11 explicitly gated live-Bedrock tests skipped;
- Phase 6 tests: **428 passed**;
- consolidated projection, URL, and profile-authority tests: **58 passed**;
- Phase 6 SAM lint/build and Python wheel/source package builds: passed;
- Ruff lint and format checks: passed;
- `git diff --check`: passed.

The Phase 6.3 projection tests cover the complete ready review, all machine/human/terminal action
classes, retryable and terminal failures, exact economics staleness boundary and source/as-of
evidence, cross-owner indistinguishability, canonical analysis/review/Strands/sync fingerprints,
stale revision joins, hostile URLs, preview-port binding validation, authority mismatch, and
recursive response-field leakage. Phase 6.4 cloud-boundary coverage separately exercises the
authenticated no-query endpoint, owner-first exact-version signing, hostile signer results, and
bodyless redirect response.

## Current completion boundary

Phase 6.3 remains the application read-model layer rather than an independent deployment or
acceptance claim. Since this slice was completed, the Phase 6.4 adapters and Phase 6.5 seller
interface have been composed and deployed as the active draft-only backend. The checked source
template remains a fail-closed validation/build input; it is not a claim that the deployed stack is
still scaffold-only.

The current release's authenticated same-job walkthrough joined AgentCore/Strands provenance,
listing and artwork review, one editable unpublished Printify draft, five mockups, production and
standard-U.S.-shipping readback, and an estimated-proceeds range across all 30 variants. One retry
restored the missing economics without another upload or product, and the explicit
keep-unpublished action ended at `APPROVED`. Publication remains outside Phase 6.

The frozen deployed cross-owner/concurrency canaries, exact provider-ledger gate, and moderated
manual accessibility/first-time-seller artifacts are not claimed as passed. Their formal
hardening status is distinct from the accepted functional hackathon-demo path and is recorded in
[`phase6-release-state.md`](phase6-release-state.md).
