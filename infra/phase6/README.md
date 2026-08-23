# Phase 6.5 AWS infrastructure and private web-edge scaffold

This directory is a new SAM application. It does not modify or replace the retained Phase 4 or
Phase 5 evidence stacks.

The template defines the Phase 6 operational table with due-work and owner-job indexes, a private
versioned artwork bucket, the four bounded Standard Step Functions workflows, and separately
permissioned worker boundaries. It also declares the Phase 6.4 cloud edge: an invite-only Cognito
user pool with TOTP MFA, one public authorization-code browser client, a scoped JWT HTTP API, and
three capability-separated API Lambda boundaries.

Phase 6.5 adds a separate private, versioned web-asset bucket and an HTTPS-only CloudFront
distribution that reads it through SigV4 origin access control. It does not expose the private
artwork bucket. The distribution sends only `/v1/*` to API Gateway, disables caching and cookies
on that behavior, disables automatic compression and `Accept-Encoding` normalization so a strong
review ETag remains strong for `If-Match`, forwards the closed authorization/content/concurrency
header set and all query strings, and rejects unsupported methods at the viewer boundary. Only the
documented `/`, `/auth/callback`, `/jobs[/...]`, and `/uploads/...` browser routes rewrite to
`index.html`; there is no blanket error-page rewrite that could conceal API or missing-asset
responses.

`AgentCoreRuntimeArn`, `PrintifySecretArn`, `ApplicationOrigin`, and
`ApplicationCertificateArn` are required, nonblank, exact-resource or exact-origin parameters.
The ACM certificate must be in `us-east-1`, as required by CloudFront. The template derives the
single Cognito callback (`${ApplicationOrigin}/auth/callback`), logout URL
(`${ApplicationOrigin}/`), and CloudFront alias from `ApplicationOrigin`, preventing them from
drifting independently. The browser upload CORS rule and API CORS policy accept only that origin;
neither uses a wildcard. The HTTP API has exactly fourteen authenticated `/v1` routes, including
the owner-scoped upload-recovery read, and one public, information-minimal `/health` route.

Content-addressed `/assets/*` files use a one-year immutable policy. `index.html`, the public
no-secret `runtime-config.json`, all other static paths, and `/v1/*` use zero-TTL and browser
`no-store`. Both response-header policies enforce CSP without inline/eval execution, HSTS,
`nosniff`, `DENY` framing, `no-referrer`, and a closed permissions policy. The
`SellerRuntimeConfig` stack output is the exact public JSON release tooling must upload under the
`SellerRuntimeConfigObjectKey`; it contains identifiers and origins, never credentials.

DNS remains externally managed: point the `ApplicationOrigin` host at
`SellerWebDistributionDomainName`. The stack intentionally does not assume that the authoritative
DNS zone is in this AWS account.

The offline application boundary issues a direct S3 POST for the server-derived source key only.
Each five-minute form fixes `image/png`, the declared SHA-256 value, `AES256`, an exact declared
size within `1..5 MiB`, and `mr-lister-state=staged`. Reauthorization after expiry repeats those
same constraints and requires no object-existence or `ListBucket` probe. Completion pins the exact
observed `VersionId` and atomically commits the consumed intent, job, source, event, receipt, and
pending `PREPARE` work. Open/cancelled intent rows use DynamoDB TTL; current and noncurrent S3
versions still tagged `staged` use the tag-filtered one-day lifecycle, while `pinned` versions do
not.

The review projection exposes an authenticated application preview route without an opaque query
grant. After JWT owner derivation and an owner-first job read, the query boundary returns a
bodyless, non-cacheable `302` to an S3 GET signed for the exact `VersionId` and no more than five
minutes. The signed GET also fixes the terminal S3 response to
`Cache-Control: private, no-store, max-age=0`. API Gateway does not carry the artwork bytes, and
this design adds no KMS dependency.

## Deployment gate

This stack is **not ready for cloud deployment**. All Lambda files, including the new upload,
review-query, and seller-command boundaries, are intentionally fail-closed scaffold handlers. They
make `sam build` verify the package topology without pretending that the tested application
adapters have been composed into deployable runtime handlers. The public health route returns `503`
with only `{"status":"scaffold_only"}`. The stack output `DeploymentReadiness=SCAFFOLD_ONLY` and
the `MR_LISTER_PHASE6_SCAFFOLD_ONLY=true` environment marker make this condition inspectable.

Before deployment, compose the tested owner derivation, upload-intent transactions, exact-version
preview redirect, consolidated review reads, and seller-command application adapters into the API
Lambda handlers. Replace the worker shims with adapters that construct the Phase 6 DynamoDB store,
invoke the checkpointed Strands/AgentCore preparation bridge, execute the draft-only Printify
synchronizer, and settle outcomes only through application-owned worker commands. Remove the
scaffold marker and change the readiness output only after all runtime compositions pass offline
tests and an approved live canary. The deploy-ready retention composition must also add the
reference-aware cleanup sweeper described in the frozen contract; the offline scaffold does not
claim that sweeper exists yet.

The query role may read and presign only the exact pinned S3 object version after application
ownership checks. It cannot write DynamoDB, call KMS, read a secret, or proxy artwork bytes through
API Gateway. The upload and command roles likewise have no AgentCore, provider-secret, or direct
Step Functions capability; the durable dispatcher remains the only execution starter.

## Local validation

Run from the repository root:

```shell
sam validate --lint --template-file infra/phase6/template.json
env PATH="$PWD/.venv/bin:$PATH" sam build --template-file infra/phase6/template.json --build-dir .aws-sam/phase6-build
python -m pytest -q tests/test_phase6_infrastructure.py tests/test_phase65_hosting_infrastructure.py
```
