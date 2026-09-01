# Historical simple root-assisted Phase 6 inert-core deployment

> [!WARNING]
> This is a historical initial inert-core and rollback-recovery runbook. Do not execute sections
> 1–4 for the current closure candidate. Use the
> [authoritative Phase 6 release-state record](../../docs/phase6-release-state.md) for current status
> and authorization. The current offline target renderers are
> [`render_phase6_artwork_closure.py`](../../tools/render_phase6_artwork_closure.py) and
> [`render_phase6_agentcore_closure_update.py`](../../tools/render_phase6_agentcore_closure_update.py);
> neither authorizes deployment. Etsy publication remains disabled and belongs to Phase 7.

This was the preferred one-off demo path for the inert Phase 6 core runtime. It deliberately avoided
temporary developer upload policies, group-policy detachment, upload-authority revocation, and an
isolated deployer role.

Root is used only for the two actions that require broad control:

1. upload the exact sealed Lambda archive and the rendered core template; and
2. create the small CloudFormation execution-role bootstrap and the reviewed core change set.

CloudFormation still runs through `mr-lister-phase6-runtime-cfn-dev`, not with root's ambient
authority. That retained role can manage only the named Phase 6 dev backend resources. At rest it
can read one exact live Lambda object key and S3 VersionId. Immediately before an update it can be
expanded to one separately paired exact candidate key and VersionId, and it is contracted again
after the application stack reaches a terminal state. Its sole
CloudFormation action is `CreateChangeSet` on the exact regional
`AWS::Serverless-2016-10-31` transform so CloudFormation can expand the staged SAM template.

## 1. Upload and read back the Lambda archive

Use the already sealed local `.mr_lister_private/phase6-artifacts/phase6-lambda.zip`. Before upload,
reverify its local SHA-256 and size against the sealed deployment descriptor. Upload it as root to
the content-addressed key derived by `Phase6S3ReleaseObjectExpectation`, with:

- the explicit SHA-256 checksum;
- `AES256` server-side encryption;
- the four exact `mr-lister-*` metadata values;
- the expected bucket owner; and
- `If-None-Match: *` so the command cannot replace an existing object at that key.

Capture bucket versioning and ownership controls before the upload. After upload, capture a
checksum-enabled `HeadObject` for the returned non-null VersionId and an unpaginated
`ListObjectVersions` for the exact key prefix.

Normalize those captures as canonical
`mr-lister-phase6-s3-manual-root-lambda-evidence-v1`. The shared verifier requires:

- bucket versioning `Enabled` and `BucketOwnerEnforced` ownership;
- exact account, Region, release, bucket, content-addressed key, and literal VersionId;
- exact local size, SHA-256 checksum, metadata, AES256 encryption, ETag, and VersionId in
  `HeadObject`; and
- one current version for the exact key, no delete marker, and no pagination.

This proves the rendered stack is bound to the uploaded local bytes. Unlike the optional
audit-grade path in `RUNTIME_UPDATE_REVIEW.md`, it does not claim IAM revocation or a retained
group-level object freeze.

## 2. Render the inert core template

Run `tools.render_phase6_core_sam_staging` with the accepted Lambda evidence, the retained
AgentCore v1 evidence, the READY `phase6_v1_dev` endpoint observation, the existing Printify
secret ARN, and the intended future lowercase HTTPS application origin.

DNS and ACM are not required for this inert core deployment. The origin is used only for the
private artifact bucket's exact CORS rule at this stage. The output remains scaffold-only and all
five asynchronous triggers remain disabled. The renderer also requires exactly the three checked
maintenance-function concurrency caps at `1`, rewrites them to `0`, and rejects any cap on the
other four functions. This adds a no-capacity guard without reducing the account's unreserved
concurrency pool.

Write the template once, then rerun the renderer with `--verify-staged` before uploading it.

## 3. Create the execution role

After the Lambda VersionId is known, apply `runtime-role-bootstrap.json` manually as root in
`us-west-2`, using stack name `mr-lister-phase6-runtime-role-bootstrap-dev` and
`CAPABILITY_NAMED_IAM`. Update the stack that already owns the fixed role name; do not create the
audit-grade bootstrap alongside it.

Before an application update, enumerate only the Lambda functions whose `CodeUri` changes in the
reviewed change set. Every changed existing function must have the same exact predecessor bucket,
key, and VersionId, and every changed target function must have the same exact candidate tuple.
A function whose `CodeUri` is unchanged may remain on an older archive and is not part of this
rollback read set. Stop if changed functions have more than one predecessor or candidate tuple;
split or reseal the update instead of widening IAM.

Supply that predecessor as `LiveReleaseFingerprint`, `LiveLambdaArchiveSha256`, and
`LiveLambdaVersionId`. Supply the candidate through `ReleaseFingerprint`,
`LambdaArchiveSha256`, and `LambdaVersionId`, then set
`CandidateArchiveBinding=EXPANDED`. For a true first deployment with no predecessor Lambda, leave
all three `Live*` parameters at `NONE`. The template requires the `Live*` values to be all exact or
all `NONE` and defaults the candidate read to `CONTRACTED`.

The bootstrap contains exactly one retained IAM role. It does not create or attach a managed
policy, reference `mr-lister-developers`, grant upload authority, create a deployer identity, or
use a temporary approval window. The role's transform permission is pinned to
`arn:${AWS::Partition}:cloudformation:us-west-2:aws:transform/Serverless-2016-10-31`; it does not
grant `cloudformation:*`, a wildcard CloudFormation resource, or stack/change-set authority.
The Lambda concurrency lifecycle actions are separately pinned to the three maintenance functions
that declare `ReservedConcurrentExecutions`; the other four functions receive no concurrency
authority. Staging needs `PutFunctionConcurrency` for the exact zero caps, and a later separately
reviewed activation transition needs `DeleteFunctionConcurrency` before triggers can be enabled.
S3 CORS apply and rollback both use the existing exact-bucket `s3:PutBucketCORS` permission.
After the seller edge exists, the role keeps only `cloudfront:GetDistribution` on the exact
`EXC2KQ0RRVWF0` distribution. CloudFormation needs that read to resolve the retained
`SellerWebDistributionDomainName` output during later stack updates; the role has no CloudFront
list, configuration-read, invalidation, create, update, tag, or delete authority.

Read back the `CoreRuntimeExecutionRoleArn` output and require:

```text
arn:aws:iam::<account-id>:role/mr-lister-phase6-runtime-cfn-dev
```

## 4. Upload, review, and deploy the core template

Upload the byte-verified rendered template as root to:

```text
private/deployments/cloudformation/core/releases/<release>/core-template-<raw-template-sha256>.json
```

Use an explicit SHA-256 checksum, AES256, the expected bucket owner, and `If-None-Match: *`.
Record the returned non-null VersionId and perform an exact-version checksum-enabled readback plus
an unpaginated singleton `ListObjectVersions` capture for that exact key. If a correction changes
the rendered bytes, keep the same sealed runtime release but use the new raw-template SHA-256 key;
never add another version at an earlier template key. Preserve the earlier key, VersionId, change
set, and outcome as deployment-attempt evidence. If bytes are unchanged, reuse the already proven
exact VersionId and create only a fresh change set.

Create an `UPDATE` change set for the existing `mr-lister-phase6-dev` stack with:

- the versioned template URL;
- all nine exact locked template parameters;
- `CAPABILITY_NAMED_IAM`;
- the retained `mr-lister-phase6-runtime-cfn-dev` service-role ARN; and
- the existing Phase 6 stack tags.

Do not execute immediately. Review the complete change set first. The accepted shape preserves
the three foundation resources in place and adds only the inert backend resources declared by the
staging renderer. Any removal, replacement of a retained foundation resource, web-surface
resource, active trigger, or unexpected IAM expansion stops the deployment.

Before execution of an update with an existing Lambda predecessor, read back the role's one inline
policy and require two distinct `s3:GetObjectVersion` statements: one predecessor key paired only
with its VersionId and one candidate key paired only with its VersionId. A true first deployment
has only the candidate statement. Resource or VersionId lists are forbidden because they form a
cross-product rather than separate exact pairs.

After review, execute the exact change set manually and keep both reads until the application
stack is terminal. On `UPDATE_COMPLETE`, update the bootstrap so the candidate tuple becomes the
three `Live*` values and set `CandidateArchiveBinding=CONTRACTED`. On
`UPDATE_ROLLBACK_COMPLETE`, leave the predecessor `Live*` values unchanged and set
`CandidateArchiveBinding=CONTRACTED`. Read the inline policy back and require exactly one live
archive statement before considering deployment authority closed. Never contract while the stack
is `*_IN_PROGRESS`, and never skip a rollback resource to work around a missing archive read.

Verify stack completion, role identity, Lambda code VersionIds, disabled triggers, function
environment bindings, and log retention.
This does not activate intake, publish approval, or the seller web surface; those remain separate
later gates.

## Historical archive-read rollback recovery

The incident below has been recovered and is preserved solely as forensic deployment history. Do
not replay it unless the same exact state is independently verified and current deployment authority
explicitly calls for this procedure.

If the application stack is already `UPDATE_ROLLBACK_FAILED` because the retained role cannot read
the predecessor archive, do not retry the application update. First identify which bootstrap stack
owns `mr-lister-phase6-runtime-cfn-dev`; the simple and audit bootstraps are mutually exclusive
owners of that fixed name. Update only that owner as root with:

```text
LiveReleaseFingerprint=0c6211a5b0244e9c86d635e6c02e7bc49e5e948d68895b4aaa982c0b0b2e187b
LiveLambdaArchiveSha256=baf152b732ce8574b6a6925bae7ab4ff849c1b83d4137076c52c6682553f9d48
LiveLambdaVersionId=pHutjLzKNpukwJ75Qs9s8YzXUAvgxZuS
ReleaseFingerprint=<full attempted 2c1b... release fingerprint>
LambdaArchiveSha256=<full attempted ba6f... archive SHA-256>
LambdaVersionId=<exact attempted archive VersionId from sealed evidence>
CandidateArchiveBinding=EXPANDED
```

For this recovery, the functions being rolled back share the `0c621.../baf152...` predecessor and
the `2c1b.../ba6f...` candidate. `ReviewQueryApiFunction` remains unchanged on its older
`6e32.../122958...` object, so that third object is not added to the rollback policy.

Wait for IAM propagation, acquire a fresh role session, and read back the one inline policy. Require
both exact statements and verify that swapping either VersionId or either resource fails the
pairing check. Then continue the existing rollback with the retained role and no skipped resources:

```shell
aws cloudformation continue-update-rollback \
  --stack-name <EXACT_APPLICATION_STACK_ID> \
  --role-arn arn:aws:iam::<ACCOUNT_ID>:role/mr-lister-phase6-runtime-cfn-dev \
  --region us-west-2
```

Monitor events to `UPDATE_ROLLBACK_COMPLETE`, verify the affected functions have the exact
predecessor `CodeUri`, and then set `CandidateArchiveBinding=CONTRACTED` while preserving the
three `Live*` values. Preserve both immutable objects and the failed attempt evidence.

## Optional audit-grade path

`runtime-update-bootstrap.json` and `RUNTIME_UPDATE_REVIEW.md` remain the stricter alternative.
They add temporary developer upload/readback/deployer authority, group-attached freeze policies,
CloudTrail joins, and a nineteen-input offline verifier. They were not required by this historical
simple one-off path and are not current closure instructions.
