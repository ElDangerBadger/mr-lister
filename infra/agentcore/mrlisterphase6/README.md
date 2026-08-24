# Phase 6 AgentCore deployment configuration

This is an isolated, production-intent configuration slice for the Phase 6 preparation runtime.
It has not been deployed and contains no cloud credentials or observed AWS identifiers.

The reviewed templates declare one `mr_lister_phase6` CodeZip runtime. Its only code location is
`../../../.mr_lister_private/phase6-deployment/agentcore`, its interpreter is Python 3.12 (matching
the sealed `cp312` Linux ARM64 artifact), its network mode is public, inbound authorization is AWS
IAM, and automatic OpenTelemetry instrumentation is disabled. The runtime has no memory,
credential, gateway, connection, payment, evaluator, or policy-engine resource. A custom endpoint
named `phase6_v<version>_<environment>` targets one explicit immutable runtime version. Deployment
traffic must never use the service-managed moving `DEFAULT` endpoint.

The execution role is external to the AgentCore configuration on purpose. It may be created as a
retained resource by the separately reviewed dev direct-CodeZip bootstrap described below. Rendered
`phase6-agentcore-runtime-policy.local.json` grants the standard AgentCore runtime log writes plus
only these application calls:

- DynamoDB `ConditionCheckItem`, `GetItem`, and `PutItem` on the exact environment table, restricted
  to the `JOB#*` and `OWNER#*` leading-key families used by preparation composition;
- S3 `GetObjectVersion` on the exact private owner/job source-object prefix;
- Bedrock `InvokeModel` for the Nova 2 Lite US inference profile and its three profile destinations,
  plus direct in-region Gemma 3 27B invocation.

It grants no provider secret, state-machine, public API, marketplace, approval, fulfillment, or
publication capability. IAM policies are additive, so deployment acceptance must also prove that
the runtime role has no broader attached or inline policy.

## Deterministic render boundary

Run the renderer from the repository root only after a sealed AgentCore artifact exists:

```shell
.venv/bin/python -m tools.render_phase6_agentcore_deployment \
  --account-id 123456789012 \
  --region us-west-2 \
  --environment prod \
  --release-fingerprint <64-lowercase-hex-release-fingerprint> \
  --runtime-version <immutable-positive-version> \
  --write
```

The example account and angle-bracket values above are documentation, not accepted deployment
defaults. Supply real values. The renderer supports only `us-west-2` because the checked Gemma
configuration is pinned to that Region. It verifies every byte of the exact
`.mr_lister_private/phase6-deployment/agentcore` artifact against the release fingerprint before
writing anything. It rejects zero identities, malformed or moving versions, `DEFAULT`, unresolved
template tokens, any Phase 3 identity, and pre-existing output files.

Rendered account-specific files are ignored by Git:

- `agentcore/agentcore.json`
- `agentcore/aws-targets.json`
- `deployment-plan.local.json`
- `render-manifest.local.json`
- `../../iam/phase6-agentcore-*.local.json`

The render manifest binds the account, Region, environment, release fingerprint, runtime version,
endpoint name, and SHA-256 of every rendered document. Re-run the same command with `--verify`
instead of `--write` to reject artifact or rendered-byte drift. The tool is local-only: it imports
no AWS SDK, creates no client, and makes no AWS call.

## Fail-closed dev direct-CodeZip path

`direct-codezip-bootstrap.json` and `tools.render_phase6_agentcore_direct_codezip` form a separate,
first-release-only path for the exact `dev` runtime in `us-west-2`. This path consumes the preserved
`phase6-agentcore.zip` directly. It must not invoke `agentcore deploy`, the AgentCore packager, CDK,
or another ZIP builder because any of those can replace the sealed bytes.

The bootstrap is a one-time, root-applied CloudFormation stack. It requires three parameters and
has no defaults:

- a near-term `NotAfter` UTC timestamp;
- the exact 64-lowercase-hex Phase 6 release fingerprint;
- the exact 64-lowercase-hex AgentCore archive SHA-256.

It retains `mr-lister-phase6-agentcore-runtime-dev`, whose trust is limited to
`bedrock-agentcore.amazonaws.com`, this account, and a runtime ARN whose resource starts directly
with `runtime/mr_lister_phase6-`. Its log-group resources likewise start directly with the
documented `/aws/bedrock-agentcore/runtimes/mr_lister_phase6-` runtime-ID prefix and have no
identifier-leading wildcard. Its worker policy preserves the reviewed Phase 6 DynamoDB,
source-version, model, and runtime-log capabilities and adds read access only to this
content-addressed deployment object:

```text
s3://mr-lister-phase6-artifacts-dev-<account>-us-west-2/
private/deployments/agentcore/releases/<release-fingerprint>/
phase6-agentcore-<agentcore-archive-sha256>.zip
```

Two temporary managed policies are attached only to `mr-lister-developers`, and every Allow
statement expires at `NotAfter`. The first grants only an AES256 `s3:PutObject` for the exact key
when `If-None-Match: *` is present. The second retains exact-version evidence reads, exact
role/model/bucket inspection, tagged Phase 6 runtime/endpoint read/canary/stop/rollback, and exact
Phase 6 v1 log reads after upload authority is removed. It also allows only the exact IAM lifecycle
needed to attach the reviewed release-freeze policy, detach the exact uploader policy, read those
attachments and the exact freeze document, and prove `mr-lister-dev` membership in
`mr-lister-developers`. It cannot attach or detach another policy or target another group.

The bootstrap also creates an initially unattached, retained
`mr-lister-phase6-agentcore-release-freeze-dev` managed policy. Its only statement explicitly
denies `PutObject`, `DeleteObject`, and `DeleteObjectVersion` for the exact content-addressed key.
After the one successful upload, `mr-lister-dev` attaches that freeze to
`mr-lister-developers` and detaches the uploader policy before any runtime input can render. This
split keeps checksum HeadObject and version-list evidence readable while the same uploading user
must receive `AccessDenied` on a nondestructive repeated conditional Put. It grants no secrets,
publication, CloudFormation runtime deployment, general S3/IAM, or AgentCore update authority.

The proof keeps two claims separate. The checksum-enabled exact-version captures bind the local
sealed bytes to the runtime's literal VersionId. The singleton lists, conditional Put, freeze
attachment, group membership, denied re-Put, and repeated lists prove collision hygiene for the
exact `mr-lister-dev` user. This is not S3 Object Lock or an account-wide deny: root and other
privileged principals outside `mr-lister-developers` could still delete that version. The rendered
authorization residual records that retention risk explicitly; broader protection requires a
separately reviewed foundation-bucket change and is not invented here.

The default policy deliberately does **not** grant `CreateAgentRuntime` or
`CreateAgentRuntimeEndpoint`. AWS currently exposes no IAM request condition for the runtime name,
or for the endpoint name and target version. It also does not grant `PutRetentionPolicy` because
IAM cannot constrain `retentionInDays` to 14. Account-wide AgentCore list operations,
`DescribeLogGroups`, and unscoped model-availability reads are omitted for the same reason. The
renderer records every such limitation in `authorization-residuals.local.json`. Only a separately
reviewed one-time manual/root execution or an explicit user-approved, tag-and-time-scoped exception
may cross either create gate; the exact generated input remains the request authority.

### Staged operator order

No step below has been executed merely because these files exist.

1. As root, review and create a stack named `mr-lister-phase6-agentcore-bootstrap` from
   `direct-codezip-bootstrap.json` in `us-west-2` with `CAPABILITY_NAMED_IAM` and the three exact
   parameters. Read back the retained role, its trust/inline policy/tags, both temporary policies'
   only group attachment, the initially unattached retained freeze policy, and `NotAfter` before
   leaving root.
2. As the dev profile, print the verified upload plan before uploading. This reads and verifies the
   sealed deployment descriptor, extracted release, ZIP SHA-256/size, and deterministic ZIP bytes:

   ```shell
   .venv/bin/python -m tools.render_phase6_agentcore_direct_codezip \
     --account-id <12-digit-account-id> \
     --release-fingerprint <64-lowercase-hex-release-fingerprint> \
     --agentcore-archive-sha256 <64-lowercase-hex-agentcore-archive-sha256> \
     --show-upload-plan
   ```

   Before the Put, capture `sts get-caller-identity` and require the exact ARN
   `arn:aws:iam::<account>:user/mr-lister-dev`. Capture bucket versioning `Enabled` and
   `BucketOwnerEnforced` ownership through the plan's exact-owner requests. Another IAM user, an
   assumed role, a suspended/unversioned bucket, or a different owner is a stop condition.
3. Execute the plan's single-part `s3api put-object` request exactly, including expected bucket
   owner, `--if-none-match '*'`, supplied full-object SHA-256, exact release metadata, and AES256.
   Capture only response fields the installed API emits: `ChecksumSHA256`, `ChecksumType`, `ETag`,
   `ServerSideEncryption`, and the non-null literal `VersionId`. `ChecksumType` must be
   `FULL_OBJECT`; an existing object, missing field, or mismatched value is a release conflict.
4. With that exact `VersionId`, capture checksum-enabled HeadObject and a complete exact-prefix
   ListObjectVersions response. Head must prove local size/base64 SHA-256, metadata, encryption,
   ETag, and VersionId. The untruncated list must contain exactly that current version and no delete
   marker. This is the first byte-identity and collision-hygiene readback; a bare VersionId never
   satisfies it.
5. Still as the exact `mr-lister-dev` user, capture the complete group-policy attachment list,
   attach only `mr-lister-phase6-agentcore-release-freeze-dev`, detach only
   `mr-lister-phase6-agentcore-direct-uploader-dev`, and capture the complete attachment list
   again. The only set change may be uploader-out/freeze-in; the evidence-reader and all unrelated
   policies must be unchanged. Read back the freeze policy and default version and prove its exact
   key-scoped Deny for `PutObject`, `DeleteObject`, and `DeleteObjectVersion`. Run unpaginated
   `iam get-group` and prove that the caller's exact ARN/UserId/UserName is a current member of the
   exact `mr-lister-developers` group.
6. Repeat the exact conditional Put with the same caller and body/checksum/metadata. It must fail
   with `AccessDenied`/403. `PreconditionFailed`/412 proves only collision detection and fails the
   authority-revocation gate. Then repeat the exact-version HeadObject and complete version list;
   both normalized responses must equal the first readback.
7. Project those captures into the closed canonical v2 shape enforced by
   `tools.verify_phase6_s3_release_object`: bucket state, Put request/response and caller, first
   Head/List, attachment-before/after, live freeze document, exact group membership, denied probe,
   and repeated Head/List. Use ISO UTC capture timestamps in strict phase order, canonical
   sorted/indented JSON bytes, and store it only under `.mr_lister_private`, for example
   `.mr_lister_private/phase6-agentcore-s3-object-binding-evidence.json`. Unknown, missing, moving,
   placeholder, duplicate, noncanonical, or symlinked evidence fails closed.
8. Supply that complete evidence—not a VersionId—and exclusively write the ignored runtime-stage
   inputs, then verify them byte-for-byte:

   ```shell
   .venv/bin/python -m tools.render_phase6_agentcore_direct_codezip \
     --account-id <12-digit-account-id> \
     --release-fingerprint <64-lowercase-hex-release-fingerprint> \
     --agentcore-archive-sha256 <64-lowercase-hex-agentcore-archive-sha256> \
     --object-binding-evidence .mr_lister_private/phase6-agentcore-s3-object-binding-evidence.json \
     --write-runtime

   .venv/bin/python -m tools.render_phase6_agentcore_direct_codezip \
     --account-id <12-digit-account-id> \
     --release-fingerprint <64-lowercase-hex-release-fingerprint> \
     --agentcore-archive-sha256 <64-lowercase-hex-agentcore-archive-sha256> \
     --object-binding-evidence .mr_lister_private/phase6-agentcore-s3-object-binding-evidence.json \
     --verify-runtime
   ```

   `create-agent-runtime.local.json` pins `codeConfiguration.code.s3.versionId`, Python 3.12,
   `main.py`, `PUBLIC`, `HTTP`, 900/3600-second lifecycle settings, the existing reviewed Phase 6
   environment variables, the retained role, and the exact four release tags.
9. Complete read-only bucket/versioning, role, Nova inference-profile, and Nova/Gemma model
   preflights. A missing role grant, model, object version, or exact tag is a stop condition.
10. Cross the explicitly blocked authorization gate only through the separately approved method and
   submit `create-agent-runtime.local.json` directly to
   `bedrock-agentcore-control create-agent-runtime`. Capture the complete response; a name
   collision, non-v1 result, or create failure is fail closed. Do not call `UpdateAgentRuntime`.
11. Use only the runtime ID returned by that create response to call `get-agent-runtime` with
   explicit `--agent-runtime-version 1` until it is `READY`, then call `list-tags-for-resource` for
   the returned runtime ARN. Do not use a list operation, infer an ID from a name, or route traffic
   through `DEFAULT`. Preserve the complete AWS CLI responses and project them into canonical
   sorted/indented JSON with this closed top-level contract:

   - `format` = `mr-lister-phase6-agentcore-runtime-v1-evidence-v1`, exact account and region;
   - `createAgentRuntime.inputSHA256` plus the complete create response;
   - `getAgentRuntime.request` containing that exact ID and version `1`, plus the complete Get
     response;
   - `listTagsForResource.request` containing that exact runtime ARN, plus the complete tags
     response;
   - the exact runtime-render-manifest SHA-256 and closed AgentCore S3 object-evidence SHA-256.

   The Get response must reproduce the sealed create input's runtime name, CodeZip S3
   bucket/prefix/literal VersionId, Python runtime and entry point, role ARN, environment,
   lifecycle, network, protocol, and description; it must join the create response's ID, ARN,
   version, timestamps, and workload identity, be `READY`, and contain exactly
   `metadataConfiguration.requireMMDSV2=true`. Missing, null, or false MMDSv2 configuration is an
   unconditional stop before endpoint rendering or core/full SAM staging. This slice does not
   authorize `UpdateAgentRuntime`: if a new `CreateAgentRuntime` does not return MMDSv2 enabled,
   stop and design a separately reviewed v2 update flow. Because Create is asynchronous and AWS
   does not define cross-operation timestamp equality, both timestamps remain verbatim evidence
   and must satisfy `Create.createdAt <= Get.createdAt <= Get.lastUpdatedAt`. The ListTags response
   must equal the four sealed release tags. Other documented unconfigured optional Get fields may
   only be absent or their safe empty/default value. Missing configured fields, unknown fields, a
   different runtime, a noncanonical file, or a symlink fails closed. Store the result outside Git,
   for example
   `.mr_lister_private/phase6-agentcore-runtime-v1-evidence.json`.
12. Supply that joined evidence—not a runtime ID—and exclusively render/verify the custom endpoint
   input:

   ```shell
   .venv/bin/python -m tools.render_phase6_agentcore_direct_codezip \
     --account-id <12-digit-account-id> \
     --release-fingerprint <64-lowercase-hex-release-fingerprint> \
     --agentcore-archive-sha256 <64-lowercase-hex-agentcore-archive-sha256> \
     --object-binding-evidence .mr_lister_private/phase6-agentcore-s3-object-binding-evidence.json \
     --runtime-v1-evidence .mr_lister_private/phase6-agentcore-runtime-v1-evidence.json \
     --write-endpoint

   .venv/bin/python -m tools.render_phase6_agentcore_direct_codezip \
     --account-id <12-digit-account-id> \
     --release-fingerprint <64-lowercase-hex-release-fingerprint> \
     --agentcore-archive-sha256 <64-lowercase-hex-agentcore-archive-sha256> \
     --object-binding-evidence .mr_lister_private/phase6-agentcore-s3-object-binding-evidence.json \
     --runtime-v1-evidence .mr_lister_private/phase6-agentcore-runtime-v1-evidence.json \
     --verify-endpoint
   ```

   After a second separately approved create crossing, submit
   `create-agent-runtime-endpoint.local.json`. It names only `phase6_v1_dev` and pins version `1`.
   Do not call `UpdateAgentRuntimeEndpoint`.
13. Wait for that custom endpoint to become `READY`, then read it back and prove its runtime ID,
   exact v1 target, tags, and ARN. A moving or `DEFAULT` qualifier is not acceptance evidence.
14. Resolve the single standard Phase 6 v1 runtime log group through a separately reviewed read,
    apply exactly `retentionInDays=14` through a separately reviewed write, and read 14 days back.
    The default temporary policy cannot perform or weaken this setting.
15. Run the same-job canary only through `phase6_v1_dev`, inspect sanitized exact-group logs, stop
    the canary session, and use only the tagged endpoint/runtime deletes if rollback is required.
16. Preserve the complete closed S3 proof, rendered manifests, role/model/bucket readback, create
    responses, READY/exact-target evidence, 14-day retention, canary, and rollback decision outside
    Git.
17. As root, delete only `mr-lister-phase6-agentcore-bootstrap`. Prove both temporary managed
    policies no longer exist or attach to `mr-lister-developers`, while the retained runtime role,
    its exact inline policy, and the retained exact-key freeze Deny remain. Do not delete the
    foundation stack or retained foundation role.

Runtime-stage outputs are ignored local files under `direct-codezip/`:

- `upload-binding-plan.local.json`;
- `create-agent-runtime.local.json`;
- `authorization-residuals.local.json`;
- `runtime-render-manifest.local.json`.

Endpoint-stage outputs are `create-agent-runtime-endpoint.local.json` and
`endpoint-render-manifest.local.json`. Writes reject every pre-existing output or symlink. Verify
mode rejects drift in any descriptor, ZIP, release, S3 version, reviewed environment, runtime
create/Get/tag evidence, or rendered bytes. There is no raw runtime-ID renderer or CLI argument.

Before any authorized deploy, validate the rendered AgentCore JSON against the CLI schema from the
project root:

```shell
cd infra/agentcore/mrlisterphase6
../../../node_modules/.bin/agentcore validate --json
```

The checked CLI schema reference at `../mrlisterphase3/agentcore/.llm-context` remains the local
format authority. This slice was authored against that schema: endpoint names are
CloudFormation-safe, the endpoint version is an integer, and all enum values use their exact schema
spellings. The reference is read-only and is not copied into or modified by this slice.

## External deployment gates

This slice is not evidence of a deploy. An explicitly authorized deployment operator must still:

1. Build and inspect the Linux ARM64 dependency artifacts, seal the Phase 6 release, and preserve
   the exact verified CodeZip bytes through AgentCore packaging. The current CLI/CDK packaging
   output must be compared with the sealed deployment inventory before upload.
2. Confirm the target account owns the exact Phase 6 DynamoDB table and versioned private artwork
   bucket named in the rendered policy, and that the bucket objects required by a canary have a
   non-null pinned version.
3. Confirm account access to the Nova 2 Lite US inference profile and direct Gemma 3 27B model in
   `us-west-2`.
4. Create the named execution role from the rendered trust policy, attach only the rendered runtime
   policy, inspect any permission boundary or organization policy, and grant a separate deploy
   principal the minimum control-plane, artifact-staging, CloudFormation/CDK, and `iam:PassRole`
   permissions required by the chosen deployment path. Those deployer permissions are deliberately
   not granted here.
5. Deploy the runtime, wait for the requested immutable runtime version to become ready, create the
   rendered custom endpoint, and verify that the endpoint is ready and targets that exact version.
   A pre-existing version or endpoint-name collision is a fail-closed release conflict.
6. After the custom endpoint log group exists, identify the single log group matching the rendered
   runtime-ID and endpoint pattern. Using a principal with only the rendered log-retention policy,
   apply `logs:PutRetentionPolicy` with `retentionInDays=14`, then read the group configuration back.
   The deployment plan marks verified 14-day retention as mandatory before traffic; the runtime role
   cannot change it.
7. Bind the observed runtime ARN, custom endpoint ARN, exact version/qualifier, release fingerprint,
   and canonical runtime-binding fingerprint into the separate Phase 6 stack deployment. This
   configuration slice does not modify or deploy that stack.
8. Run an explicitly authorized same-job live canary through the custom endpoint, inspect sanitized
   logs, verify no broader role grants, and record rollback evidence before enabling seller traffic.

The physical runtime ID and log-group name are assigned by AgentCore and therefore cannot be
truthfully committed here. The rendered deployment plan carries a version- and environment-bound
selection pattern; resolving that pattern to exactly one observed resource, setting retention, and
recording the observation remain deployment-time gates.

## AWS references

- [Direct Python CodeZip deployment and `/var/task`](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-get-started-code-deploy-python.html)
- [AWS CLI `create-agent-runtime` S3 `versionId`](https://docs.aws.amazon.com/cli/latest/reference/bedrock-agentcore-control/create-agent-runtime.html)
- [AWS CLI `get-agent-runtime`](https://docs.aws.amazon.com/cli/latest/reference/bedrock-agentcore-control/get-agent-runtime.html)
- [AWS CLI `list-tags-for-resource`](https://docs.aws.amazon.com/cli/latest/reference/bedrock-agentcore-control/list-tags-for-resource.html)
- [AgentCore Runtime execution-role permissions](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-permissions.html)
- [Immutable runtime versions and custom endpoints](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/agent-runtime-versioning.html)
- [AgentCore runtime log-group naming](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/diagnose-evaluation-skill-source.html)
- [AgentCore actions, resources, and condition keys](https://docs.aws.amazon.com/service-authorization/latest/reference/list_bedrock-agentcore.html)
- [S3 conditional-write enforcement](https://docs.aws.amazon.com/AmazonS3/latest/userguide/conditional-writes-enforce.html)
- [CloudWatch Logs actions, resources, and condition keys](https://docs.aws.amazon.com/service-authorization/latest/reference/list_logs.html)
