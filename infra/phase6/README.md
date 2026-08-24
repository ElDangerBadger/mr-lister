# Phase 6.6 AWS infrastructure and private web-edge scaffold

This directory is a new SAM application. It does not modify or replace the retained Phase 4 or
Phase 5 evidence stacks.

The template defines the Phase 6 operational table with due-work and owner-job indexes, a private
versioned artwork bucket, the four bounded Standard Step Functions workflows, and separately
permissioned worker boundaries. It also declares the Phase 6.4 cloud edge: an invite-only Cognito
user pool with TOTP MFA, one public authorization-code browser client, a scoped JWT HTTP API, and
three capability-separated API Lambda boundaries.

Phase 6.5 adds a separate private, versioned web-asset bucket and an HTTPS-only CloudFront
distribution that reads it through SigV4 origin access control. It does not expose the private
artwork bucket. The distribution sends `/v1/*` and the exact public `/health` route to API Gateway.
It disables caching and cookies on both API-origin behaviors, disables automatic compression and
`Accept-Encoding` normalization so a strong review ETag remains strong for `If-Match`, forwards
the closed authorization/content/concurrency header set and all query strings on `/v1/*`, and
rejects unsupported protected-route methods at the viewer boundary. Only the documented `/`,
`/auth/callback`, `/jobs[/...]`, and `/uploads/...` browser routes rewrite to `index.html`; there is
no blanket error-page rewrite that could conceal API or missing-asset responses.

`AgentCoreRuntimeArn`, `PrintifySecretArn`, `ApplicationOrigin`, and
`ApplicationCertificateArn` are required, nonblank, exact-resource or exact-origin parameters.
The ACM certificate must be in `us-east-1`, as required by CloudFront. The template derives the
single Cognito callback (`${ApplicationOrigin}/auth/callback`), logout URL
(`${ApplicationOrigin}/`), and CloudFront alias from `ApplicationOrigin`, preventing them from
drifting independently. The browser upload CORS rule and API CORS policy accept only that origin;
neither uses a wildcard. The HTTP API has exactly fourteen authenticated `/v1` routes, including
the owner-scoped upload-recovery read, and one public, information-minimal `/health` route.

Content-addressed `/assets/*` files use a one-year immutable policy. `index.html`, the public
no-secret `runtime-config.json`, all other static paths, `/health`, and `/v1/*` use zero-TTL and
browser `no-store`. Both response-header policies enforce CSP without inline/eval execution, HSTS,
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
not. A separate prefix-only rule removes expired delete markers only after no object versions
remain; it has no tag filter and cannot delete a referenced version.

The review projection exposes an authenticated application preview route without an opaque query
grant. After JWT owner derivation and an owner-first job read, the query boundary returns a
bodyless, non-cacheable `302` to an S3 GET signed for the exact `VersionId` and no more than five
minutes. The signed GET also fixes the terminal S3 response to
`Cache-Control: private, no-store, max-age=0`. API Gateway does not carry the artwork bytes, and
this design adds no KMS dependency.

## Create-only foundation

`foundation.json` is the intentionally narrow first deployment for the eventual Phase 6 stack. It
contains exactly `OperationalStateTable`, `PrivateArtifactBucket`, and
`PrivateArtifactBucketPolicy`, with the same logical IDs, physical names, retention controls,
indexes, encryption, versioning, lifecycle, and deny-only bucket policy as `template.json`. The
only planned resource difference is that the foundation bucket has no browser CORS configuration:
the exact application origin does not exist yet. Updating the same stack with the full template
adds that closed CORS rule once `ApplicationOrigin` is available.

The template declares `FOUNDATION_ONLY` in both metadata and `DeploymentReadiness`. It is valid
only for a `CREATE` change set against the final Phase 6 stack name. It must never be applied as an
update: after the full template has added the runtime, reapplying the three-resource template would
ask CloudFormation to remove those later resources. Run the repository's create-only foundation
deployment verifier, `tools/verify_phase6_foundation_deployment.py`, immediately before creating
the reviewed change set; the verifier rejects an existing stack and every non-`CREATE` operation.
Do not bypass that guard with a direct `sam deploy`. The foundation outputs the table and bucket
names and ARNs for post-deployment identity checks, but it creates no IAM role, Lambda, API,
workflow, schedule, identity surface, provider transport, or secret access.

### Root-only foundation IAM bootstrap

`bootstrap.json` is the temporary, root-applied IAM boundary for this one create. Deploy it only as
the `mr-lister-phase6-foundation-bootstrap` stack in `us-west-2`, with
`CAPABILITY_NAMED_IAM` and an explicit, near-term UTC `NotAfter` value. The parameter deliberately
has no default. The stack creates the exact
`mr-lister-phase6-foundation-cfn-dev` CloudFormation execution role and attaches the expiring
`mr-lister-phase6-foundation-deployer-dev` policy only to the existing
`mr-lister-developers` group used by `mr-lister-dev`.

The developer policy can create and execute only the fingerprint-named Phase 6 dev change set,
must pass the exact execution role, must submit the three foundation resource types and exact
foundation stack tags, and has only the configuration reads needed by the offline evidence
verifier. It grants no `CreateStack`, `UpdateStack`, `DeleteStack`, object, item, runtime, provider,
secret, Lambda, API, or workflow authority. The execution role is limited to creating,
stabilizing, tagging, and rollback-cleaning the exact retained table and bucket, applying the
deny-only bucket policy, and expanding the checked Serverless transform.

IAM cannot distinguish a CloudFormation `CREATE` change-set request from an `UPDATE` request: both
use `cloudformation:CreateChangeSet`. The exact name, stack, service role, tags, resource-type
conditions, short `NotAfter` window, and repository verifier therefore operate together. The
verifier must observe stack absence and `ChangeSetType=CREATE` before execution. After the accepted
evidence capture, root should delete the bootstrap stack to detach and remove the developer managed
policy; the execution role is intentionally retained because it is recorded on the foundation
stack. Follow `FOUNDATION_DEPLOYMENT.md` for the exact capture and verification sequence.

## Deployment gate

This stack is **not ready for cloud deployment**. Every Lambda entrypoint remains fail-closed under
the checked `MR_LISTER_PHASE6_SCAFFOLD_ONLY=true` marker. The thin shim now has closed delegation
surfaces for the API, dispatcher, preparation, provider, settlement, source-retention,
terminal-operational-cleanup, and stuck-execution-recovery roles,
but the checked `CodeUri` still contains only the scaffold package rather than the tested
application source and Linux ARM64 dependencies. The public health route therefore returns `503`
with only `{"status":"scaffold_only"}`. The stack output `DeploymentReadiness=SCAFFOLD_ONLY` makes
this condition inspectable.

Role-separated composition roots now construct the tested API, dispatcher, preparation, provider,
settlement, reference-aware source-retention, terminal operational-cleanup, and execution-recovery
boundaries. A dedicated Phase 6 AgentCore source
entrypoint visibly keeps Strands as controller over the pinned Gemma intelligence configuration,
and reproducible narrow source manifests isolate ordinary Lambda code from the AgentCore runtime.
The cleanup/recovery schedules, encrypted recovery DLQ, release/runtime bindings, least-capability
IAM, and closed alarm/SNS transport are present in this template. Recovery can describe an exact
execution and settle durable authority, but cannot start, stop, or redrive workflows. None of that
is a deployment claim while the application bundles and dependencies are absent from `CodeUri` and
the scaffold marker remains true.

Before deployment, produce the real controlled Linux ARM64 dependency artifacts, run their target
import smoke, seal both runtime trees, and wire those exact bytes into SAM and the separate
versioned AgentCore deployment. Then deploy through a reviewed change set, verify the configured
endpoint observation, alarms, cleanup/recovery schedules, static assets, and non-destructive smoke.
Remove the scaffold marker and change the readiness output only after those controls and explicitly
authorized live acceptance pass.

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
