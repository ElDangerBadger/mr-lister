# Phase 6.3 consolidated seller review evidence

Phase 6.3 adds one strict, owner-scoped, read-only application projection over the Phase 6 durable
authority records. It does not introduce another lifecycle, write to the operational store, call a
model or provider, or grant publication authority.

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

The original-artwork preview is a narrow port. A grant must bind the exact source fingerprint, use
the fixed `/v1/jobs/{job_id}/artwork-preview?grant={opaque}` route on one configured HTTPS
application origin, contain no storage coordinate, and expire within five minutes. Issuer failure
or any binding/URL/expiry mismatch produces `preview unavailable` without revealing storage
details. The AWS signer and authenticated preview route belong to Phase 6.4.

## Verification

Accepted offline verification on 2026-08-22:

- all tests: **638 passed**; 11 explicitly gated live-Bedrock tests skipped;
- Phase 6 tests: **428 passed**;
- consolidated projection, URL, and profile-authority tests: **58 passed**;
- Phase 6 SAM lint/build and Python wheel/source package builds: passed;
- Ruff lint and format checks: passed;
- `git diff --check`: passed.

The projection tests cover the complete ready review, all machine/human/terminal action classes,
retryable and terminal failures, exact economics staleness boundary and source/as-of evidence,
cross-owner indistinguishability, canonical analysis/review/Strands/sync fingerprints, stale
revision joins, hostile URLs, opaque preview grants, validation paths, authority mismatch, and
recursive response-field leakage.

## Deliberately open

Phase 6.3 is the application read model, not the cloud API or interface. Phase 6 remains open until:

- Phase 6.4 supplies Cognito ownership, direct private upload, and the concrete short-lived preview
  route;
- Phase 6.5 renders this projection as an accessible seller interface;
- the `SCAFFOLD_ONLY` Phase 6 Lambda adapters are composed and deployed; and
- live same-job acceptance proves upload through AgentCore/Strands, one unpublished product,
  consolidated review, and the human decision boundary.
