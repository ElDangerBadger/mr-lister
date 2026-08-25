# Simple root-assisted Phase 6 core deployment

This is the preferred one-off demo path for the inert Phase 6 core runtime. It deliberately avoids
temporary developer upload policies, group-policy detachment, upload-authority revocation, and an
isolated deployer role.

Root is used only for the two actions that require broad control:

1. upload the exact sealed Lambda archive and the rendered core template; and
2. create the small CloudFormation execution-role bootstrap and the reviewed core change set.

CloudFormation still runs through `mr-lister-phase6-runtime-cfn-dev`, not with root's ambient
authority. That retained role can manage only the named Phase 6 dev backend resources and can read
only the exact Lambda object key and S3 VersionId supplied to its bootstrap. Its sole
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
five asynchronous triggers remain disabled.

Write the template once, then rerun the renderer with `--verify-staged` before uploading it.

## 3. Create the execution role

After the Lambda VersionId is known, apply `runtime-role-bootstrap.json` manually as root in
`us-west-2`, using stack name `mr-lister-phase6-runtime-role-bootstrap-dev` and
`CAPABILITY_NAMED_IAM`. Supply the exact release fingerprint, Lambda archive SHA-256, and Lambda
VersionId.

The bootstrap contains exactly one retained IAM role. It does not create or attach a managed
policy, reference `mr-lister-developers`, grant upload authority, create a deployer identity, or
use a temporary approval window. The role's transform permission is pinned to
`arn:${AWS::Partition}:cloudformation:us-west-2:aws:transform/Serverless-2016-10-31`; it does not
grant `cloudformation:*`, a wildcard CloudFormation resource, or stack/change-set authority.

Read back the `CoreRuntimeExecutionRoleArn` output and require:

```text
arn:aws:iam::<account-id>:role/mr-lister-phase6-runtime-cfn-dev
```

## 4. Upload, review, and deploy the core template

Upload the byte-verified rendered template as root to:

```text
private/deployments/cloudformation/core/releases/<release>/core-template.json
```

Use an explicit SHA-256 checksum, AES256, the expected bucket owner, and `If-None-Match: *`.
Record the returned non-null VersionId and perform an exact-version checksum-enabled readback.

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

After review, execute the exact change set manually. Verify stack completion, role identity,
Lambda code VersionIds, disabled triggers, function environment bindings, and log retention.
This does not activate intake, publish approval, or the seller web surface; those remain separate
later gates.

## Optional audit-grade path

`runtime-update-bootstrap.json` and `RUNTIME_UPDATE_REVIEW.md` remain the stricter alternative.
They add temporary developer upload/readback/deployer authority, group-attached freeze policies,
CloudTrail joins, and a nineteen-input offline verifier. They are not required by this simple
one-off path.
