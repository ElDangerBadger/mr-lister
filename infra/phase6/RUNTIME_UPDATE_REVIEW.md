# Phase 6 core-runtime UPDATE review gate

> This is the optional audit-grade deployment path. The current one-off demo uses the smaller
> root-assisted procedure in `SIMPLE_ROOT_RUNTIME_DEPLOYMENT.md`, which preserves exact artifact
> versions, a least-privilege CloudFormation execution role, and change-set review without
> attaching temporary policies to the developer group.

This runbook stages and reviews the first inert core-runtime update of the retained Phase 6
foundation stack. It does not deploy the seller web surface, enable a trigger, authorize
publication, or execute a change set. The final verifier is offline and proves only its captures.

Keep these roles distinct:

| Authority | Exact dev identity | Meaning |
| --- | --- | --- |
| Pre-update stack role | `mr-lister-phase6-foundation-cfn-dev` | Historical foundation proof only |
| Update execution role | `mr-lister-phase6-runtime-cfn-dev` | New CloudFormation service role |
| Change-set caller | `mr-lister-phase6-runtime-update-deployer-dev` | Expiring isolated creator/reader |

The bootstrap never edits or broadens the foundation role. The deployer cannot call
`cloudformation:ExecuteChangeSet`. Its caller-side permission to use the SAM transform is distinct
from the runtime execution role's own exact `cloudformation:CreateChangeSet` permission on
`arn:${AWS::Partition}:cloudformation:us-west-2:aws:transform/Serverless-2016-10-31`; both are
required for transformed-template review, and neither grants change-set execution.

## Root-applied two-stage bootstrap

Manually apply `runtime-update-bootstrap.json` as root in `us-west-2`, using stack name
`mr-lister-phase6-runtime-update-bootstrap-dev` and `CAPABILITY_NAMED_IAM`. Creating this local file
does not itself mutate AWS. Validate it first:

```shell
sam validate --lint --template-file infra/phase6/runtime-update-bootstrap.json
```

### Stage A: independently seal Lambda, then the template

Create the bootstrap stack with exact `FoundationStackId`, `ReleaseFingerprint`,
`LambdaArchiveSha256`, and a near-term Stage A `NotAfter`. Leave `LambdaVersionId`,
`CoreTemplateVersionId`, `CoreTemplateVersionIdUrlEncoded`, `TargetTemplateFingerprint`, and
`ExactChangeSetName` at `PENDING`; the template requires all five to move together.

Read back the root-created resources. Stage A contains the retained runtime execution role; the
common-v2 Lambda uploader, evidence reader, and retained freeze; and separate template uploader,
evidence reader, and retained freeze. It does not contain the deployer yet.

Complete the canonical `mr-lister-phase6-s3-release-object-evidence-v2` lifecycle for component
`lambda` and this exact key:

```text
private/deployments/lambda/releases/<release>/phase6-lambda-<archive-sha256>.zip
```

Use the exact checksum-bearing, AES256, `If-None-Match: *` Put and complete bucket/Head/List/IAM
captures enforced by `tools.verify_phase6_s3_release_object.py`. The attachment order matters:

1. capture Lambda attachment-before while both template policies are unchanged;
2. attach only `mr-lister-phase6-lambda-release-freeze-dev`;
3. detach only `mr-lister-phase6-lambda-direct-uploader-dev`;
4. capture Lambda attachment-after before attaching the template freeze;
5. finish the Lambda freeze readback, membership proof, denied repeat Put, and identical
   post-revocation Head/List proof.

Do not reattach the Lambda uploader. Keep the separate template uploader live. Render and
byte-verify `target-template.json` through `tools.render_phase6_core_sam_staging` using the accepted
Lambda evidence and literal VersionId. The core template must lock the complete nine-parameter set
with exact `Default` and singleton `AllowedValues`:

- `AgentCoreRuntimeArn`, `AgentCoreRuntimeBindingFingerprint`,
  `AgentCoreRuntimeEndpointArn`, `AgentCoreRuntimeQualifier`, `AgentCoreRuntimeVersion`;
- `ApplicationOrigin`, `EnvironmentName`, `PrintifySecretArn`, `ReleaseFingerprint`.

Conditionally upload the exact target bytes to the noncircular, release-bound key:

```text
private/deployments/cloudformation/core/releases/<release>/core-template.json
```

Capture its non-null VersionId, checksum-enabled exact-version HeadObject, and complete singleton
version list; download the exact version and require byte equality. Then attach only
`mr-lister-phase6-core-template-release-freeze-dev`, detach only
`mr-lister-phase6-core-template-direct-uploader-dev`, read back the freeze and attachments, and
repeat the exact-version reads. Reject moving, null, placeholder, whitespace-bearing, or
unversioned identities. These group freezes are not S3 Object Lock or an account-wide root deny.

### Stage B: exact versions and exact deployer

Immediately before CreateChangeSet, manually update the same bootstrap stack as root. Keep the
Stage A identities fixed and set together:

- raw `LambdaVersionId` from accepted common-v2 evidence;
- raw `CoreTemplateVersionId` and its exact RFC 3986 `CoreTemplateVersionIdUrlEncoded`;
- canonical semantic `TargetTemplateFingerprint`;
- `ExactChangeSetName` equal to `mr-lister-phase6-dev-runtime-update-` plus the first twelve target
  fingerprint characters.

Set a fresh `NotAfter` later than capture/review but no more than fifteen minutes after the
CreateChangeSet CloudTrail event. Stage B removes both upload policies, binds the runtime role to
the exact Lambda version, and creates the deployer and assume-role policy. If the window cannot be
completed, stop and open a fresh Stage B window before creating a new change set.

Read back both live roles and policies. Each role must have one exact inline policy, no attached
policies, and no boundary. The verifier derives the complete runtime execution policy from the
checked bootstrap plus the exact account and accepted Lambda object binding; any extra or missing
statement, action, resource, or condition fails even if a manifest fingerprint is recomputed. The
deployer policy must bind the exact stack, SAM transform,
`cloudformation:RoleArn`, `cloudformation:ChangeSetName`, immutable versioned
`cloudformation:TemplateUrl`, three tags, expiry, exact `iam:PassRole` with
`iam:PassedToService = cloudformation.amazonaws.com`, and exact-version template read. The runtime
policy includes the four inert SAM-generated EventBridge rules and the dispatcher event-source
mapping pinned with `lambda:FunctionArn`; unavoidable wildcard APIs are region-conditioned. The
three functions with reserved-concurrency caps have exact function-ARN-scoped Get/Put/Delete
concurrency lifecycle authority so create and rollback cannot strand the stack; no other function
receives those actions.

## Evidence and manifest

Use the already verified local `phase6-deployment` and `phase6-artifacts` sealed roots plus a new
private directory with these nineteen evidence inputs. The Lambda evidence must be the accepted,
canonical `mr-lister-phase6-s3-release-object-evidence-v2` document from Stage A, not a projection
or copied VersionId:

```text
foundation-binding.json
lambda-object-evidence.json
expected-update-manifest.json
pre-update-stack.json
pre-update-stack-resources.json
update-change-set.json
update-change-set-original-template.json
update-change-set-processed-template.json
target-template.json
caller-identity.json
cloudtrail-create-change-set.json
runtime-execution-role.json
runtime-execution-role-inline-policies.json
runtime-execution-role-attached-policies.json
runtime-execution-role-policy.json
deployer-role.json
deployer-role-inline-policies.json
deployer-role-attached-policies.json
deployer-role-policy.json
```

The closed manifest format is `mr-lister-phase6-reviewed-update-manifest-v2`. It contains exactly:

```json
{
  "account_id": "<ACCOUNT_ID>",
  "capabilities": ["CAPABILITY_NAMED_IAM"],
  "change_set_description": "Mr Lister Phase 6 reviewed UPDATE <TARGET_SHA256>",
  "change_set_name": "mr-lister-phase6-dev-runtime-update-<TARGET_PREFIX_12>",
  "changes": [],
  "client_token": "phase6-<TARGET_PREFIX_32>",
  "deployment_config": {"DisableRollback": false, "Mode": "STANDARD"},
  "deployer_policy_name": "mr-lister-phase6-runtime-update-deployer-dev",
  "deployer_role_arn": "arn:aws:iam::<ACCOUNT_ID>:role/mr-lister-phase6-runtime-update-deployer-dev",
  "deployer_session_name": "phase6-update-<TARGET_PREFIX_12>",
  "environment_name": "dev",
  "execution_role_arn": "arn:aws:iam::<ACCOUNT_ID>:role/mr-lister-phase6-runtime-cfn-dev",
  "execution_role_policy_fingerprint": "<RESOLVED_POLICY_SHA256>",
  "execution_role_policy_name": "mr-lister-phase6-runtime-execution-dev",
  "format": "mr-lister-phase6-reviewed-update-manifest-v2",
  "foundation_binding_fingerprint": "<FOUNDATION_BINDING_SHA256>",
  "lambda_release_object_evidence_fingerprint": "<CANONICAL_LAMBDA_COMMON_V2_EVIDENCE_SHA256>",
  "notification_arns": [],
  "parameters": {"<ALL_NINE_TARGET_PARAMETER_KEYS>": "<EXACT_LOCKED_VALUES>"},
  "policy_expires_at": "<NOT_AFTER_UTC>",
  "processed_template_fingerprint": "<PROCESSED_SHA256>",
  "region": "us-west-2",
  "rollback_configuration": {},
  "stack_id": "<EXACT_FOUNDATION_STACK_ID>",
  "stack_name": "mr-lister-phase6-dev",
  "tags": {"DeploymentClass": "RUNTIME_UPDATE", "Environment": "dev", "Project": "MrLister"},
  "target_template_fingerprint": "<TARGET_SHA256>",
  "template_url": "https://mr-lister-phase6-artifacts-dev-<ACCOUNT_ID>-us-west-2.s3.us-west-2.amazonaws.com/private/deployments/cloudformation/core/releases/<RELEASE>/core-template.json?versionId=<ENCODED_VERSION>"
}
```

Fingerprints are SHA-256 of parsed JSON serialized with sorted keys, compact separators, and one
final newline. The Lambda evidence fingerprint is SHA-256 over the canonical common-v2 evidence
bytes. The execution-policy fingerprint is over the resolved live IAM document and must equal the
verifier-derived complete policy, not merely a manifest assertion. Finalize
the Processed fingerprint and complete `changes` list only after capture and human review.

Every normalized change has the closed fields `action`, `after_context`, `before_context`,
`details`, `logical_resource_id`, `physical_resource_id`, `replacement`, `resource_type`, and
`scope`. A Modify requires both contexts, exact static property-value details, a physical ID,
nonempty scope, and replacement. Foundation changes must be in-place with `replacement = "False"`
and `RequiresRecreation = "Never"`; all other accepted core changes are additions. Removal,
import, dynamic evaluation, missing property context, or an unreviewed change fails.

CloudTrail intentionally logs the complete parameter-key set but not parameter values.
DescribeChangeSet is the value authority, so the manifest and target must enumerate all nine.

## Create and capture, never execute

Assume only the exact deployer with session name `phase6-update-<TARGET_PREFIX_12>` and capture
`sts get-caller-identity` to `caller-identity.json`. Capture pre-update DescribeStacks and the
complete ListStackResources using the exact stack ID. The pre-stack `RoleARN` must be the old
foundation role, and the resources must be exactly the retained table, bucket, and bucket policy.

Create one change set with the exact stack ID, name, description, client token, versioned URL,
runtime RoleARN, all nine explicit parameter values, three exact tags,
`CAPABILITY_NAMED_IAM`, type `UPDATE`, and nested stacks false. Capabilities and ResourceTypes are
mutually exclusive, so do not send ResourceTypes. Do not execute it.

After `CREATE_COMPLETE`, capture:

```shell
aws cloudformation describe-change-set \
  --stack-name "$FOUNDATION_STACK_ID" \
  --change-set-name "$EXACT_CHANGE_SET_NAME" \
  --include-property-values \
  --profile <ISOLATED_DEPLOYER_PROFILE> --region us-west-2 --no-paginate --output json \
  > <EVIDENCE_DIR>/update-change-set.json

aws cloudformation get-template --stack-name "$FOUNDATION_STACK_ID" \
  --change-set-name "$EXACT_CHANGE_SET_NAME" --template-stage Original \
  --profile <ISOLATED_DEPLOYER_PROFILE> --region us-west-2 --output json \
  > <EVIDENCE_DIR>/update-change-set-original-template.json

aws cloudformation get-template --stack-name "$FOUNDATION_STACK_ID" \
  --change-set-name "$EXACT_CHANGE_SET_NAME" --template-stage Processed \
  --profile <ISOLATED_DEPLOYER_PROFILE> --region us-west-2 --output json \
  > <EVIDENCE_DIR>/update-change-set-processed-template.json
```

Original must equal the semantic target; Processed is separately reviewed and bound. Do not add
synthetic `RoleARN` or `ChangeSetType` to DescribeChangeSet. AWS-omitted optional success fields are
canonically normalized to null; extra fields and pagination fail closed.

Capture GetRole, ListRolePolicies, ListAttachedRolePolicies, and GetRolePolicy for both exact roles
into the named evidence files. Use the same deployer session throughout.

Search CloudTrail Event history in a narrow UTC interval for CreateChangeSet, inspect candidates,
then recapture the chosen event by exact EventId. The final
`cloudtrail-create-change-set.json` must contain exactly `{"Events": [<ONE_EVENT>]}` and no
`NextToken`. The gate requires a successful management event with no error fields and exactly joins
account, Region, caller role/session/access key, times, stack and change-set IDs/names, `UPDATE`,
runtime RoleARN, template URL, client token, capabilities, complete parameter keys, tags, and
response IDs.

## Offline verification and immediate recapture

```shell
.venv/bin/python -m tools.verify_phase6_runtime_update \
  --deployment-root .mr_lister_private/phase6-deployment \
  --artifact-root .mr_lister_private/phase6-artifacts \
  --lambda-object-evidence <EVIDENCE_DIR>/lambda-object-evidence.json \
  --foundation-binding <EVIDENCE_DIR>/foundation-binding.json \
  --expected-manifest <EVIDENCE_DIR>/expected-update-manifest.json \
  --pre-stack-observation <EVIDENCE_DIR>/pre-update-stack.json \
  --pre-stack-resources-observation <EVIDENCE_DIR>/pre-update-stack-resources.json \
  --change-set-observation <EVIDENCE_DIR>/update-change-set.json \
  --original-template-observation <EVIDENCE_DIR>/update-change-set-original-template.json \
  --processed-template-observation <EVIDENCE_DIR>/update-change-set-processed-template.json \
  --target-template <EVIDENCE_DIR>/target-template.json \
  --caller-identity-observation <EVIDENCE_DIR>/caller-identity.json \
  --cloudtrail-observation <EVIDENCE_DIR>/cloudtrail-create-change-set.json \
  --execution-role-observation <EVIDENCE_DIR>/runtime-execution-role.json \
  --execution-role-inline-policies-observation <EVIDENCE_DIR>/runtime-execution-role-inline-policies.json \
  --execution-role-attached-policies-observation <EVIDENCE_DIR>/runtime-execution-role-attached-policies.json \
  --execution-role-policy-observation <EVIDENCE_DIR>/runtime-execution-role-policy.json \
  --deployer-role-observation <EVIDENCE_DIR>/deployer-role.json \
  --deployer-role-inline-policies-observation <EVIDENCE_DIR>/deployer-role-inline-policies.json \
  --deployer-role-attached-policies-observation <EVIDENCE_DIR>/deployer-role-attached-policies.json \
  --deployer-role-policy-observation <EVIDENCE_DIR>/deployer-role-policy.json \
  > <EVIDENCE_DIR>/review-descriptor.json
```

Before accepting any AWS observation, the gate re-verifies the local deployment descriptor and
sealed Lambda ZIP, runs the shared common-v2 S3 evidence verifier, and requires its exact release,
archive SHA-256, size, checksum, bucket, key, and VersionId in every target function `CodeUri` and
the closed runtime execution policy. Success emits canonical
`mr-lister-phase6-reviewed-update-v2` with those values and the canonical Lambda evidence
fingerprint, plus
`verification_scope = OFFLINE_CAPTURE_ONLY` and
`availability_claim = CAPTURE_ONLY_RECAPTURE_REQUIRED`. It does not claim current live state.

Immediately before any separately authorized execution decision, recapture the complete nineteen
evidence inputs, reverify the same sealed roots, rerun the gate, require the new canonical
descriptor to be byte-for-byte identical, and
require current time before `recapture_contract.execute_before`. Any changed evidence fingerprint,
policy, role inventory, template, change context, change-set state, CloudTrail join, descriptor
byte, or expired window invalidates the review. Execution remains a separate manual/root action.
