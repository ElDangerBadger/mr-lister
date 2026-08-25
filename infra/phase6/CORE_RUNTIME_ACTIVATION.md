# Phase 6 core-runtime activation

This runbook activates only the already deployed, domain-independent Phase 6 backend. It uses two
and only two CloudFormation updates:

1. `capacity-released-inert`: remove the three zero-concurrency guards while scaffold mode and all
   five asynchronous triggers remain inert.
2. `backend-active-draft-only`: turn scaffold mode off and enable exactly those five reviewed
   backend triggers.

The first update must be proven live before the second change set is created. Each change set is
created for review but is not executable authority: execution requires a separate, explicit human
approval naming its ARN, target, and template SHA-256. Do not combine the updates, skip an evidence
gate, use `sam deploy`, or deploy `infra/phase6/template.json` directly.

This sequence does not create a web surface. It grants no provider publication, order, or
fulfillment authority. The second state permits the existing provider worker to create or update
drafts only when reviewed backend work exists; the idle smoke in this runbook deliberately creates
no such work.

## Fixed authority and states

| State | Concurrency on three maintenance functions | Scaffold marker | Reviewed triggers | Readiness |
| --- | --- | --- | --- | --- |
| Current staged | `0` | `true` | all disabled | `CORE_RELEASE_BOUND_STAGED` |
| Update 1 | absent | `true` | all disabled | `CORE_CAPACITY_RELEASED_INERT` |
| Update 2 | absent | `false` | all enabled | `CORE_RUNTIME_ACTIVE_DRAFT_ONLY` |

The exact seven functions are `DispatcherFunction`, `PreparationDispatchFunction`,
`ProviderDraftFunction`, `SettlementFunction`, `SourceVersionRetentionFunction`,
`StuckExecutionRecoveryFunction`, and `TerminalOperationalCleanupFunction`. The three maintenance
functions are the final three in that list.

The exact five triggers are:

- `DispatcherFunction.Events.DueWorkSweep`;
- `DispatcherFunction.Events.OperationalStateChanges`;
- `SourceVersionRetentionFunction.Events.SourceVersionRetentionSweep`;
- `TerminalOperationalCleanupFunction.Events.TerminalOperationalCleanupSweep`; and
- `StuckExecutionRecoveryScheduleRule`.

The existing CloudFormation service role is the only deployment execution authority:

```text
arn:aws:iam::<ACCOUNT_ID>:role/mr-lister-phase6-runtime-cfn-dev
```

It is already restricted to the named Phase 6 backend resources, the exact versioned Lambda
artifact, and the regional SAM transform. Its concurrency lifecycle authority is restricted to
the three maintenance functions. Passing this ARN in every change-set request is mandatory;
CloudFormation must never execute with root's ambient authority.

The root/bootstrap profile has two distinct uses:

- **read-only:** identity and all live-state, template, change-set, S3, log, metric, and queue
  observations;
- **mutation:** one create-only S3 template upload, change-set creation, and—only after explicit
  approval—execution of the exact reviewed change set.

If the profile is expired, the caller is not the expected account root, the stack ID or service
role differs, or termination protection is not enabled, stop. This runbook never starts an
interactive login.

## Operator variables

Run from the repository root. Populate every placeholder from the checked private evidence and
the current stack; do not use `latest`, omit a VersionId, or paste a secret value. The
`PrintifySecretArn` is an ARN of the existing secret, not the secret's value, and is read without
echo below.

```bash
umask 077

MR6_PROFILE="mr-lister-bootstrap"
MR6_REGION="us-west-2"
MR6_ACCOUNT_ID="<EXPECTED_ACCOUNT_ID>"
MR6_ENVIRONMENT="dev"
MR6_STACK_NAME="mr-lister-phase6-dev"
MR6_STACK_ID="<EXACT_EXISTING_STACK_ID>"
MR6_EXECUTION_ROLE_ARN="arn:aws:iam::${MR6_ACCOUNT_ID}:role/mr-lister-phase6-runtime-cfn-dev"
MR6_RELEASE_FINGERPRINT="<SEALED_RELEASE_FINGERPRINT>"
MR6_SOURCE_COMMIT="678ea4f60ad5fd0aba0c8da6e5530959a1bcbb93"

MR6_FOUNDATION_BINDING="<CANONICAL_FOUNDATION_BINDING_PATH>"
MR6_AGENTCORE_ENDPOINT_OBSERVATION="<CANONICAL_READY_ENDPOINT_OBSERVATION_PATH>"
MR6_AGENTCORE_OBJECT_EVIDENCE="<CANONICAL_AGENTCORE_OBJECT_EVIDENCE_PATH>"
MR6_AGENTCORE_RUNTIME_V1_EVIDENCE="<CANONICAL_AGENTCORE_RUNTIME_V1_EVIDENCE_PATH>"
MR6_LAMBDA_OBJECT_EVIDENCE="<CANONICAL_LAMBDA_OBJECT_EVIDENCE_PATH>"

MR6_AGENTCORE_RUNTIME_ARN="<EXACT_RUNTIME_ARN>"
MR6_AGENTCORE_ENDPOINT_ARN="<EXACT_ENDPOINT_ARN>"
MR6_AGENTCORE_RUNTIME_VERSION="1"
MR6_AGENTCORE_QUALIFIER="phase6_v1_dev"
MR6_AGENTCORE_BINDING_FINGERPRINT="<SEALED_BINDING_FINGERPRINT>"

MR6_APPLICATION_ORIGIN="https://massskutiny.com"
MR6_LAMBDA_ARTIFACT_BUCKET="<EXACT_ARTIFACT_BUCKET>"
MR6_LAMBDA_ARTIFACT_KEY="<EXACT_CONTENT_ADDRESSED_LAMBDA_KEY>"
MR6_LAMBDA_ARTIFACT_VERSION_ID="<EXACT_LAMBDA_VERSION_ID>"

read -r -s -p "Existing Printify secret ARN: " MR6_PRINTIFY_SECRET_ARN
printf '\n'

MR6_EVIDENCE_ROOT=".mr_lister_private/phase6-core-activation"
mkdir -p -m 700 "$MR6_EVIDENCE_ROOT"
```

Reject any unresolved placeholder before an AWS mutation. Also require every referenced evidence
file to be a regular, non-symlink file and require `MR6_PRINTIFY_SECRET_ARN` to be nonempty and to
belong to the expected account and Region. Never print it or include it in a review transcript.
`MR6_SOURCE_COMMIT` is the exact source commit of the deployed sealed Lambda release, not the
current branch `HEAD`; activation changes only templates and evidence, not Lambda bytes.

## Canonical live-state gate

`tools/verify_phase6_core_live_state.py` is the offline authority for the three live modes. It
imports no AWS SDK, starts no subprocess, and accepts only canonical
`mr-lister-phase6-core-live-state-v1` JSON. Capture raw AWS responses privately, normalize only the
closed fields accepted by the verifier, write sorted/two-space JSON with one trailing newline, and
then run the verifier. Do not hand-edit a verified document afterward.

Every capture must include all of these read-only observations:

- root caller identity; exact stack ID, `UPDATE_COMPLETE`, service role, output readiness,
  termination protection, the three exact tags, original template resource count, 47 live
  resources, zero non-complete resources, and deployed release source commit
  `678ea4f60ad5fd0aba0c8da6e5530959a1bcbb93`;
- all seven function names, physical IDs, code SHA-256, release fingerprint, runtime,
  architecture, state/update state, scaffold marker, and reserved concurrency state, without
  capturing or printing the provider secret binding;
- all four EventBridge rule states and the one DynamoDB event-source mapping state, UUID, exact
  `WORK#` filter, `LATEST` start position, batch size 25, bisect enabled, retry count 3, and no
  pagination;
- a strongly consistent DynamoDB `COUNT` scan and no item content;
- all four state machines as `ACTIVE`/`STANDARD`, ERROR-only logging without execution data, and
  zero `RUNNING` executions with no pagination token;
- the recovery queue's visible, delayed, and in-flight counts, retention, and managed encryption;
- all eleven log groups and their 14-day retention;
- AgentCore runtime `READY` at version `1` and endpoint `READY` with live version `1`;
- Lambda regional concurrency quota and current unreserved concurrency;
- the private foundation table and bucket controls; and
- absence of ACM, API Gateway, CloudFront, Cognito, and Route 53 resources in this stack.

The table safety observation must use this exact request shape. It can return only counts and an
optional pagination key; acceptance requires both counts to be zero and the key to be absent,
normalized as explicit JSON `null`:

```bash
aws dynamodb scan \
  --profile "$MR6_PROFILE" \
  --region "$MR6_REGION" \
  --table-name "$MR6_STACK_NAME" \
  --consistent-read \
  --select COUNT \
  --no-paginate \
  --query '{Count:Count,ScannedCount:ScannedCount,LastEvaluatedKey:LastEvaluatedKey}' \
  --output json
```

For each of the four exact state-machine ARNs, use `list-executions` with
`--status-filter RUNNING --max-results 1 --no-paginate`; acceptance is an empty execution list and
no `nextToken`. Never call `scan` without `--select COUNT`, and never capture an item, execution
input, execution output, secret value, or full Lambda environment.

The canonical schema separates current safety proof from lineage:

- `preflight` contains `captured_at`, the COUNT-only table scan, and the four zero-running
  observations. For the top-level evidence being used as authority now, both `capture_time` and
  `preflight.captured_at` must be no more than 15 minutes old. The preflight may not be later than
  its document or more than 15 minutes before it.
- `predecessor_evidence` is either null for staged mode or exactly `{mode, captured_at,
  evidence_sha256}`. Supplied predecessor and staged-ancestor files may be arbitrarily old, but
  must remain canonical, fully mode-valid, hash/time-linked, and temporally no later than their
  successors. If an old predecessor is verified directly as the top-level current-state file, the
  normal 15-minute freshness rule applies again.

This permits deliberate human review between updates without weakening current-state proof.

## 1. Capture and verify fresh staged evidence

Use root/bootstrap only for read-only calls. First capture identity and compare its `Account` and
`Arn` to the expected root identity. Then capture the closed inventory above and write it to a
create-exclusive timestamped path that is retained immutably:

```bash
MR6_STAGED_EVIDENCE="$MR6_EVIDENCE_ROOT/staged-live-$(date -u +%Y%m%dT%H%M%SZ).json"
```

The staged document must prove `CORE_RELEASE_BOUND_STAGED`, scaffold `true`, all five triggers
disabled, reserved concurrency exactly `0` on the three maintenance functions and absent on the
other four, the COUNT-only empty scan, zero running executions, zero recovery-queue counts, and no
public web resources.

```bash
.venv/bin/python -m tools.verify_phase6_core_live_state \
  "$MR6_STAGED_EVIDENCE"
```

Record the verifier's canonical SHA-256. Any mismatch is a stop. Re-capture into a new path and
re-verify if this top-level evidence will be older than 15 minutes at Update 1 execution; never
overwrite an earlier capture.

## 2. Render and verify `capacity-released-inert`

The renderer always reconstructs the sealed staged source and permits only the closed target
transition. It has no AWS client and cannot upload or deploy. Define the common arguments once:

```bash
MR6_RENDER_ARGS=(
  --account-id "$MR6_ACCOUNT_ID"
  --region "$MR6_REGION"
  --environment "$MR6_ENVIRONMENT"
  --foundation-stack-id "$MR6_STACK_ID"
  --foundation-binding "$MR6_FOUNDATION_BINDING"
  --release-fingerprint "$MR6_RELEASE_FINGERPRINT"
  --agentcore-runtime-arn "$MR6_AGENTCORE_RUNTIME_ARN"
  --agentcore-runtime-endpoint-arn "$MR6_AGENTCORE_ENDPOINT_ARN"
  --agentcore-runtime-version "$MR6_AGENTCORE_RUNTIME_VERSION"
  --agentcore-runtime-qualifier "$MR6_AGENTCORE_QUALIFIER"
  --agentcore-runtime-binding-fingerprint "$MR6_AGENTCORE_BINDING_FINGERPRINT"
  --agentcore-endpoint-observation "$MR6_AGENTCORE_ENDPOINT_OBSERVATION"
  --agentcore-object-evidence "$MR6_AGENTCORE_OBJECT_EVIDENCE"
  --agentcore-runtime-v1-evidence "$MR6_AGENTCORE_RUNTIME_V1_EVIDENCE"
  --printify-secret-arn "$MR6_PRINTIFY_SECRET_ARN"
  --application-origin "$MR6_APPLICATION_ORIGIN"
  --lambda-artifact-bucket "$MR6_LAMBDA_ARTIFACT_BUCKET"
  --lambda-artifact-key "$MR6_LAMBDA_ARTIFACT_KEY"
  --lambda-artifact-version "$MR6_LAMBDA_ARTIFACT_VERSION_ID"
  --lambda-object-evidence "$MR6_LAMBDA_OBJECT_EVIDENCE"
)

.venv/bin/python -m tools.render_phase6_core_runtime_transition \
  "${MR6_RENDER_ARGS[@]}" \
  --target capacity-released-inert \
  --write

.venv/bin/python -m tools.render_phase6_core_runtime_transition \
  "${MR6_RENDER_ARGS[@]}" \
  --target capacity-released-inert \
  --verify

MR6_CAPACITY_TEMPLATE=".mr_lister_private/phase6-core-runtime-transition/template.core-capacity-released-inert.local.json"
```

`--write` is create-exclusive. If the fixed output already exists, run only `--verify`; never
overwrite it. The renderer must prove that the only semantic source changes are removal of
`ReservedConcurrentExecutions: 0` from the three maintenance functions plus the closed readiness
and transition metadata. Scaffold stays `true`, all five triggers stay disabled, the seven-function
set is unchanged, and no web resource is present.

## 3. Upload immutable template bytes and read them back

Repeat this section separately for each target template. These are root mutations only at the
single `put-object` line; the bucket-state, head, list, and exact-version download calls are
read-only.

Compute the raw template SHA-256, byte count, and base64 checksum locally. The exact key is:

```text
private/deployments/cloudformation/core/releases/<release>/core-template-<template-sha256>.json
```

Before upload, prove bucket versioning is `Enabled`, ownership is `BucketOwnerEnforced`, and the
exact key has no versions or delete markers. If the key already exists, do not upload another
version: accept it only after an exact checksum/size/metadata/version readback and byte-identical
download. Otherwise use a create-only request:

```bash
MR6_TARGET="capacity-released-inert"
MR6_TEMPLATE="$MR6_CAPACITY_TEMPLATE"
MR6_TEMPLATE_SHA256="$(shasum -a 256 "$MR6_TEMPLATE" | awk '{print $1}')"
MR6_TEMPLATE_SIZE="$(wc -c < "$MR6_TEMPLATE" | tr -d ' ')"
MR6_TEMPLATE_CHECKSUM_B64="$(openssl dgst -sha256 -binary "$MR6_TEMPLATE" | openssl base64 -A)"
MR6_TEMPLATE_KEY="private/deployments/cloudformation/core/releases/${MR6_RELEASE_FINGERPRINT}/core-template-${MR6_TEMPLATE_SHA256}.json"
MR6_UPLOAD_EVIDENCE="$MR6_EVIDENCE_ROOT/${MR6_TARGET}-template-object"
mkdir -p -m 700 "$MR6_UPLOAD_EVIDENCE"

aws s3api put-object \
  --profile "$MR6_PROFILE" \
  --region "$MR6_REGION" \
  --bucket "$MR6_LAMBDA_ARTIFACT_BUCKET" \
  --key "$MR6_TEMPLATE_KEY" \
  --body "$MR6_TEMPLATE" \
  --checksum-algorithm SHA256 \
  --checksum-sha256 "$MR6_TEMPLATE_CHECKSUM_B64" \
  --server-side-encryption AES256 \
  --expected-bucket-owner "$MR6_ACCOUNT_ID" \
  --if-none-match '*' \
  --metadata "mr-lister-component=cloudformation-core,mr-lister-release-fingerprint=${MR6_RELEASE_FINGERPRINT},mr-lister-template-sha256=${MR6_TEMPLATE_SHA256},mr-lister-size-bytes=${MR6_TEMPLATE_SIZE}" \
  --output json \
  > "$MR6_UPLOAD_EVIDENCE/put-object.json"
```

Read the non-null VersionId from the private response and capture `head-object --checksum-mode
ENABLED`, an unpaginated `list-object-versions` for the exact key prefix, and `get-object` for that
literal VersionId. Require one current exact-key version, no delete marker, the same SHA-256 and
size, AES256, exact metadata, and a byte-for-byte local comparison. A moving URL, missing
VersionId, second object version, pagination token, checksum mismatch, or download mismatch stops.

Construct the CloudFormation HTTPS URL with the literal key and URL-encoded VersionId:

```bash
MR6_TEMPLATE_VERSION_ID="<NON_NULL_VERSION_ID_FROM_VERIFIED_READBACK>"
MR6_TEMPLATE_VERSION_ID_ESCAPED="$(printf '%s' "$MR6_TEMPLATE_VERSION_ID" | jq -sRr @uri)"
MR6_TEMPLATE_URL="https://${MR6_LAMBDA_ARTIFACT_BUCKET}.s3.${MR6_REGION}.amazonaws.com/${MR6_TEMPLATE_KEY}?versionId=${MR6_TEMPLATE_VERSION_ID_ESCAPED}"
```

Retain a canonical object-evidence document binding the local SHA-256, checksum, size, key,
VersionId, upload response, head response, singleton version listing, and exact-version download.

## 4. Create and review Update 1; do not execute

All nine parameters are locked and must be passed as literal values—never `UsePreviousValue`:

1. `EnvironmentName`
2. `ReleaseFingerprint`
3. `AgentCoreRuntimeArn`
4. `AgentCoreRuntimeEndpointArn`
5. `AgentCoreRuntimeVersion`
6. `AgentCoreRuntimeQualifier`
7. `AgentCoreRuntimeBindingFingerprint`
8. `PrintifySecretArn`
9. `ApplicationOrigin`

The only accepted tags are exactly `Project=MrLister`, `Environment=dev`, and
`DeploymentClass=FOUNDATION_ONLY`.

Create an `UPDATE` change set with a content-bound name and the existing service role. This is a
root mutation that creates review authority but does not update the stack:

```bash
MR6_CHANGE_SET_NAME="${MR6_STACK_NAME}-capacity-release-${MR6_TEMPLATE_SHA256:0:12}"
MR6_CHANGE_TOKEN="capacity-release-${MR6_TEMPLATE_SHA256:0:32}"
MR6_CHANGE_EVIDENCE="$MR6_EVIDENCE_ROOT/capacity-change-set-${MR6_TEMPLATE_SHA256}"
test ! -e "$MR6_CHANGE_EVIDENCE"
mkdir -m 700 "$MR6_CHANGE_EVIDENCE"

MR6_PREDECESSOR_ORIGINAL="$MR6_CHANGE_EVIDENCE/predecessor-original-template.json"
MR6_PREDECESSOR_PROCESSED="$MR6_CHANGE_EVIDENCE/predecessor-processed-template.json"

aws cloudformation get-template \
  --profile "$MR6_PROFILE" \
  --region "$MR6_REGION" \
  --stack-name "$MR6_STACK_ID" \
  --template-stage Original \
  --output json | jq -S . > "$MR6_PREDECESSOR_ORIGINAL"

aws cloudformation get-template \
  --profile "$MR6_PROFILE" \
  --region "$MR6_REGION" \
  --stack-name "$MR6_STACK_ID" \
  --template-stage Processed \
  --output json | jq -S . > "$MR6_PREDECESSOR_PROCESSED"

aws cloudformation create-change-set \
  --profile "$MR6_PROFILE" \
  --region "$MR6_REGION" \
  --stack-name "$MR6_STACK_ID" \
  --change-set-name "$MR6_CHANGE_SET_NAME" \
  --change-set-type UPDATE \
  --description "Phase 6 capacity-released-inert ${MR6_TEMPLATE_SHA256}" \
  --client-token "$MR6_CHANGE_TOKEN" \
  --template-url "$MR6_TEMPLATE_URL" \
  --role-arn "$MR6_EXECUTION_ROLE_ARN" \
  --capabilities CAPABILITY_NAMED_IAM \
  --parameters \
    "ParameterKey=EnvironmentName,ParameterValue=${MR6_ENVIRONMENT}" \
    "ParameterKey=ReleaseFingerprint,ParameterValue=${MR6_RELEASE_FINGERPRINT}" \
    "ParameterKey=AgentCoreRuntimeArn,ParameterValue=${MR6_AGENTCORE_RUNTIME_ARN}" \
    "ParameterKey=AgentCoreRuntimeEndpointArn,ParameterValue=${MR6_AGENTCORE_ENDPOINT_ARN}" \
    "ParameterKey=AgentCoreRuntimeVersion,ParameterValue=${MR6_AGENTCORE_RUNTIME_VERSION}" \
    "ParameterKey=AgentCoreRuntimeQualifier,ParameterValue=${MR6_AGENTCORE_QUALIFIER}" \
    "ParameterKey=AgentCoreRuntimeBindingFingerprint,ParameterValue=${MR6_AGENTCORE_BINDING_FINGERPRINT}" \
    "ParameterKey=PrintifySecretArn,ParameterValue=${MR6_PRINTIFY_SECRET_ARN}" \
    "ParameterKey=ApplicationOrigin,ParameterValue=${MR6_APPLICATION_ORIGIN}" \
  --tags \
    Key=Project,Value=MrLister \
    Key=Environment,Value=dev \
    Key=DeploymentClass,Value=FOUNDATION_ONLY \
  --output json \
  > "$MR6_CHANGE_EVIDENCE/create.json"
```

Wait only for change-set creation. Then capture the full, unpaginated property-level change set and
both target template stages as canonical JSON. **Do not execute it.**

```bash
MR6_CHANGE_SET_ARN="$(jq -r '.Id' "$MR6_CHANGE_EVIDENCE/create.json")"
MR6_CHANGE_SET_OBSERVATION="$MR6_CHANGE_EVIDENCE/describe-change-set.json"
MR6_CHANGE_SET_RESOURCE_OBSERVATION="$MR6_CHANGE_EVIDENCE/describe-change-set-resource-level.json"
MR6_TARGET_ORIGINAL="$MR6_CHANGE_EVIDENCE/target-original-template.json"
MR6_TARGET_PROCESSED="$MR6_CHANGE_EVIDENCE/target-processed-template.json"

aws cloudformation wait change-set-create-complete \
  --profile "$MR6_PROFILE" \
  --region "$MR6_REGION" \
  --stack-name "$MR6_STACK_ID" \
  --change-set-name "$MR6_CHANGE_SET_ARN"

aws cloudformation describe-change-set \
  --profile "$MR6_PROFILE" \
  --region "$MR6_REGION" \
  --stack-name "$MR6_STACK_ID" \
  --change-set-name "$MR6_CHANGE_SET_ARN" \
  --include-property-values \
  --no-paginate \
  --output json | jq -S . > "$MR6_CHANGE_SET_OBSERVATION"

aws cloudformation describe-change-set \
  --profile "$MR6_PROFILE" \
  --region "$MR6_REGION" \
  --stack-name "$MR6_STACK_ID" \
  --change-set-name "$MR6_CHANGE_SET_ARN" \
  --no-include-property-values \
  --no-paginate \
  --output json | jq -S . > "$MR6_CHANGE_SET_RESOURCE_OBSERVATION"

aws cloudformation get-template \
  --profile "$MR6_PROFILE" \
  --region "$MR6_REGION" \
  --stack-name "$MR6_STACK_ID" \
  --change-set-name "$MR6_CHANGE_SET_ARN" \
  --template-stage Original \
  --output json | jq -S . > "$MR6_TARGET_ORIGINAL"

aws cloudformation get-template \
  --profile "$MR6_PROFILE" \
  --region "$MR6_REGION" \
  --stack-name "$MR6_STACK_ID" \
  --change-set-name "$MR6_CHANGE_SET_ARN" \
  --template-stage Processed \
  --output json | jq -S . > "$MR6_TARGET_PROCESSED"
```

The accepted processed resource-change scope for Update 1 is exact:

- `Modify`, `Replacement=False`: `SourceVersionRetentionFunction`;
- `Modify`, `Replacement=False`: `StuckExecutionRecoveryFunction`; and
- `Modify`, `Replacement=False`: `TerminalOperationalCleanupFunction`.

Every record must have `Scope=["Properties"]` and one Static/DirectModification/Never detail at
`/Properties/ReservedConcurrentExecutions`, with change type `Remove` from `0`.

CloudFormation's resource-level view is deliberately retained as separate evidence. For Update 1,
it can conservatively report the exact ten-resource dependency closure: the three directly changed
Lambda functions; the three still-disabled EventBridge rules whose targets reference those Lambda
ARNs; the three Lambda permissions whose source ARNs reference those rules; and the recovery queue
policy whose source condition references the recovery rule. The seven dependent records must be
`Dynamic` `ResourceAttribute` propagation only, and their before/after processed resources must be
byte-identical. Reject any `Add`, `Remove`, known `Replacement=True`, direct rule `State` change,
different causing entity, or resource outside that closed set. The `--include-property-values`
observation remains the semantic authority for the resolved delta and must still contain only the
three direct concurrency removals above.

In the resolved property-value view there may be no `Add`, `Remove`, replacement, IAM change,
trigger change, function code change, role change, foundation change, or web resource. The original
target bytes must equal the verified capacity template, and the processed template must contain the
same closed 40-source/47-live resource identity after SAM expansion.

`tools/verify_phase6_core_transition_change_set.py` is the offline verifier that joins the canonical
predecessor Original/Processed observations, sealed local target, full change set, and target
Original/Processed observations. It enforces the exact stack, nine parameter values, three tags,
40 original resources, 47 processed resources, and property-level resource scope. Run it now:

```bash
.venv/bin/python -m tools.verify_phase6_core_transition_change_set \
  --target capacity-released-inert \
  --predecessor-original-template-observation "$MR6_PREDECESSOR_ORIGINAL" \
  --predecessor-processed-template-observation "$MR6_PREDECESSOR_PROCESSED" \
  --target-template "$MR6_CAPACITY_TEMPLATE" \
  --change-set-observation "$MR6_CHANGE_SET_OBSERVATION" \
  --target-original-template-observation "$MR6_TARGET_ORIGINAL" \
  --target-processed-template-observation "$MR6_TARGET_PROCESSED"
```

Retain its canonical digest with the live-state and immutable S3-object evidence. The existing
service-role binding is proved by current live state and the literal `--role-arn` create request;
the transition verifier itself consumes only the six files above. If it or its tests do not pass,
stop before execution. Visual review is additional assurance, not a substitute.

## 5. Explicitly approve and execute Update 1

The approval must name all four values: `capacity-released-inert`, exact stack ID, exact change-set
ARN, and exact template SHA-256. Immediately before execution, re-run the staged live-state and
transition change-set verifiers. Re-capture staged evidence and repeat the join if freshness has
expired or anything changed.

Only after that explicit approval, execute the exact ARN:

```bash
aws cloudformation execute-change-set \
  --profile "$MR6_PROFILE" \
  --region "$MR6_REGION" \
  --stack-name "$MR6_STACK_ID" \
  --change-set-name "$MR6_CHANGE_SET_ARN" \
  --client-request-token "execute-${MR6_CHANGE_TOKEN}"

aws cloudformation wait stack-update-complete \
  --profile "$MR6_PROFILE" \
  --region "$MR6_REGION" \
  --stack-name "$MR6_STACK_ID"
```

Do not disable rollback or termination protection. On failure, stop and retain stack events,
change-set evidence, and template-object evidence; do not improvise a second update.

## 6. Prove post-capacity state and chain it to staged

Capture new canonical capacity evidence immediately after completion. Its
`predecessor_evidence` must hash/time-link the accepted staged document. Its separate, fresh
`preflight` must contain the current COUNT-only scan request and zero response plus the four
current zero-running observations.

Acceptance requires:

- `UPDATE_COMPLETE`, 47 live resources, zero non-complete resources, termination protection, the
  same role/tags/physical IDs, and `CORE_CAPACITY_RELEASED_INERT`;
- the same seven code hashes and release bindings;
- reserved concurrency absent on all seven functions;
- scaffold `true` on all seven functions;
- all five triggers still disabled with the exact mapping configuration;
- a fresh, strongly consistent `COUNT` scan with `Count=0`, `ScannedCount=0`, and no
  `LastEvaluatedKey`;
- zero `RUNNING` executions on all four state machines and zero recovery-queue counts; and
- AgentCore runtime/endpoint still `READY` at version `1`.

```bash
MR6_CAPACITY_EVIDENCE="$MR6_EVIDENCE_ROOT/capacity-released-inert-live-$(date -u +%Y%m%dT%H%M%SZ).json"

.venv/bin/python -m tools.verify_phase6_core_live_state \
  "$MR6_CAPACITY_EVIDENCE" \
  --predecessor-evidence "$MR6_STAGED_EVIDENCE"
```

If this fails, do not create the active change set. At this point the backend remains inert because
scaffold mode and every trigger are still disabled even though invocation capacity is available.

## 7. Render, upload, and review `backend-active-draft-only`

Render and byte-verify the second target from the same sealed authority:

```bash
.venv/bin/python -m tools.render_phase6_core_runtime_transition \
  "${MR6_RENDER_ARGS[@]}" \
  --target backend-active-draft-only \
  --write

.venv/bin/python -m tools.render_phase6_core_runtime_transition \
  "${MR6_RENDER_ARGS[@]}" \
  --target backend-active-draft-only \
  --verify

MR6_ACTIVE_TEMPLATE=".mr_lister_private/phase6-core-runtime-transition/template.core-runtime-active-draft-only.local.json"
```

Repeat the immutable, content-addressed upload/readback in section 3 with
`MR6_TARGET=backend-active-draft-only` and `MR6_TEMPLATE=$MR6_ACTIVE_TEMPLATE`. Create a new
content-bound `UPDATE` change set by repeating section 4 with an `active-draft-only` name,
description, and client token. Pass the same nine literal parameters, exact three tags,
versioned template URL, capabilities, and existing service role. Do not execute it.

Relative to the proven live capacity-released state, the only accepted processed resource-change
scope for Update 2 is:

- `Modify`, `Replacement=False`: all seven exact Lambda functions listed in the fixed authority,
  each with only `/Properties/Environment/Variables/MR_LISTER_PHASE6_SCAFFOLD_ONLY` changing from
  `true` to `false`;
- `Modify`, `Replacement=False`: `DispatcherFunctionDueWorkSweep`,
  `SourceVersionRetentionFunctionSourceVersionRetentionSweep`,
  `StuckExecutionRecoveryScheduleRule`, and
  `TerminalOperationalCleanupFunctionTerminalOperationalCleanupSweep`, each with only
  `/Properties/State` changing from `DISABLED` to `ENABLED`; and
- `Modify`, `Replacement=False`: `DispatcherFunctionOperationalStateChanges`, with only
  `/Properties/Enabled` changing from `false` to `true`.

All twelve records must have `Scope=["Properties"]` and exactly one
Static/DirectModification/Never detail at the named path.

There may be no `Add`, `Remove`, replacement, code or role change, IAM expansion, concurrency
change, state-machine definition change, foundation change, or web resource. The transition
change-set verifier must accept the current capacity Original/Processed template observations and
enforce this exact scope. Separately, the live-state verifier must accept a fresh capacity document
whose immutable lineage links to staged.

Run the same six-file verifier invocation from section 4 with
`--target backend-active-draft-only`, `--target-template "$MR6_ACTIVE_TEMPLATE"`, and the newly
captured capacity predecessor, active change-set, and active target template paths. Retain its
canonical success record before requesting execution approval.

## 8. Explicitly approve and execute Update 2

The second approval must independently name `backend-active-draft-only`, the exact stack ID,
change-set ARN, and template SHA-256. Immediately before execution, re-run:

- the capacity live-state verifier chained to staged;
- the active template renderer in `--verify` mode; and
- `tools/verify_phase6_core_transition_change_set.py` for the exact active change set.

Immediately before approval execution, re-capture the current capacity mode into a new canonical
file with a fresh preflight, hash-link it to the immutable staged evidence, and verify it as the
top-level file. The older staged lineage remains valid; only this current capacity authority must
be within 15 minutes. Then execute only the exact active change-set ARN using the command shape in
section 5 and wait for `stack-update-complete`.

## 9. Prove active state and run an idle/no-work smoke

Capture active evidence immediately. It must hash-link the freshly accepted capacity evidence,
whose own link must still identify the immutable staged evidence. Its own preflight must be fresh.

```bash
MR6_ACTIVE_EVIDENCE="$MR6_EVIDENCE_ROOT/backend-active-draft-only-live-$(date -u +%Y%m%dT%H%M%SZ).json"

.venv/bin/python -m tools.verify_phase6_core_live_state \
  "$MR6_ACTIVE_EVIDENCE" \
  --predecessor-evidence "$MR6_CAPACITY_EVIDENCE" \
  --staged-ancestor-evidence "$MR6_STAGED_EVIDENCE"
```

Acceptance requires `CORE_RUNTIME_ACTIVE_DRAFT_ONLY`, scaffold `false` on all seven functions, no
reserved concurrency on any function, exactly four EventBridge rules enabled, the exact DynamoDB
mapping enabled, unchanged code/roles/physical IDs, empty table, zero running executions, zero
recovery-queue counts, and AgentCore still READY at version `1`.

For the idle smoke, create no item and invoke no function or state machine manually. Record an
observation start time, wait through at least one due-work interval and one recovery interval
(minimum six minutes), then capture read-only evidence that:

- the dispatcher and recovery schedules invoked without Lambda error or throttle;
- EventBridge has no failed invocation;
- the provider-draft function has zero invocation delta;
- no Step Functions execution started and all four RUNNING counts remain zero;
- a second strongly consistent COUNT-only scan is still exactly empty; and
- visible, delayed, and in-flight recovery-queue counts remain zero.

CloudWatch metrics are eventually consistent; an absent datapoint is not affirmative zero. Retain
the metric window and wait for it to settle, or corroborate with the exact log streams, before
accepting the smoke. Do not create a provider draft as part of this no-work check. The source
retention schedule's 15-minute cycle may be observed separately; the daily terminal-cleanup cycle
is not a prerequisite for this idle gate.

## Boundary and stop line

Successful completion leaves Phase 6 at a backend-active, draft-only, domain-independent
checkpoint. `ApplicationOrigin` remains a locked bucket-CORS binding; this runbook neither proves
DNS resolution nor serves that origin.

> **STOP HERE — do not request or create a `us-east-1` ACM certificate, change DNS or Route 53,
> deploy CloudFront, Cognito, API Gateway, seller-web/static assets, activate web traffic, or begin
> Phase 7 publication under this runbook.**

The web edge is a later, separately rendered, reviewed, and approved deployment after domain
readiness. Publication, order, and fulfillment remain separate future authorities even after the
web surface exists.
