# Phase 6 create-only foundation deployment

This runbook deploys only the retained Phase 6 operational table, private artifact bucket, and
bucket policy. It does not deploy Lambda, API Gateway, CloudFront, Cognito, Step Functions,
AgentCore, provider credentials, schedules, or publication capability. The resulting stack reports
`FOUNDATION_ONLY` and is not a usable application runtime.

The foundation uses the future application stack name, `mr-lister-phase6-dev`. Its first change set
must be `CREATE`; the foundation verifier rejects `UPDATE`, `IMPORT`, replacement, reapplication,
an existing stack, or any resource beyond the exact three-resource contract.

## Separate the administrator and developer evidence

An administrator signed in as root applies `infra/phase6/bootstrap.json` once through the
CloudFormation console:

- stack: `mr-lister-phase6-foundation-bootstrap`
- `NotAfter`: a near-term UTC timestamp long enough to create and verify the foundation
- capability acknowledgement: `CAPABILITY_NAMED_IAM`

Retain the bootstrap stack's template, events, and the three outputs
`CloudFormationExecutionRoleArn`, `DeveloperDeploymentPolicyArn`, and
`DeveloperDeploymentPolicyNotAfter` as administrator evidence. Keep that evidence outside the
foundation evidence directory. Sign out of root after the bootstrap reaches `CREATE_COMPLETE`.
Root must not create the foundation stack.

The bootstrap's developer authority expires at `NotAfter`, including readback and termination
protection. Complete every remaining step in that window. The exact execution-role output for
`dev` must be:

```text
arn:aws:iam::<ACCOUNT_ID>:role/mr-lister-phase6-foundation-cfn-dev
```

All foundation deployment evidence is then captured using the non-root `mr-lister-dev` profile.
The verifier pins its exact caller ARN and rejects a root caller.

## Local gate

From the repository root, verify the frozen source before making an AWS change:

```shell
sam validate --lint --template-file infra/phase6/foundation.json
.venv/bin/python -m tools.verify_phase6_foundation_deployment \
  template \
  --template infra/phase6/foundation.json
```

The expected semantic fingerprint is:

```text
689897c254c9db97aa75d508f140980f9b6a5129c0c1fa0121eb8d6ef1e64874
```

Create a private evidence directory. The examples below call it `<EVIDENCE_DIR>`; replace every
angle-bracketed value explicitly rather than placing credentials or callback tokens in files.

## Prove the stack is absent

Refresh the developer session and capture its exact identity:

```shell
aws login --profile mr-lister-dev
aws sts get-caller-identity \
  --profile mr-lister-dev \
  --region us-west-2 \
  --output json > <EVIDENCE_DIR>/caller-identity.json
```

Run the exact lookup and retain its unedited standard error:

```shell
aws cloudformation describe-stacks \
  --stack-name mr-lister-phase6-dev \
  --profile mr-lister-dev \
  --region us-west-2 \
  --output json 2> <EVIDENCE_DIR>/stack-absence.stderr
```

Stop if that command succeeds. Proceed only when it fails with CloudFormation `ValidationError`
because the exact stack does not exist. Normalize that observed error as
`<EVIDENCE_DIR>/stack-absence.json`:

```json
{
  "error_code": "ValidationError",
  "format": "mr-lister-cloudformation-stack-absence-v1",
  "http_status_code": 400,
  "operation": "DescribeStacks",
  "stack_name": "mr-lister-phase6-dev"
}
```

Verify the normalized observation before creating a change set:

```shell
.venv/bin/python -m tools.verify_phase6_foundation_deployment \
  absence \
  --observation <EVIDENCE_DIR>/stack-absence.json \
  --account-id <ACCOUNT_ID> \
  --region us-west-2 \
  --environment-name dev \
  --stack-name mr-lister-phase6-dev \
  --execution-role-arn arn:aws:iam::<ACCOUNT_ID>:role/mr-lister-phase6-foundation-cfn-dev \
  --deployer-arn arn:aws:iam::<ACCOUNT_ID>:user/mr-lister-dev
```

Do not manufacture an absence observation after an access-denied, network, expired-session, or
other failure. The raw error remains part of the audit evidence even though the verifier consumes
only the value-free normalization.

## Create and review the one permitted change set

The bootstrap policy requires every request field below. In particular, use the exact stack and
change-set names—not an ARN—and submit all three resource types and all three stack tags.

```shell
aws cloudformation create-change-set \
  --stack-name mr-lister-phase6-dev \
  --change-set-name mr-lister-phase6-dev-foundation-create-689897c254c9 \
  --change-set-type CREATE \
  --template-body file://infra/phase6/foundation.json \
  --parameters ParameterKey=EnvironmentName,ParameterValue=dev \
  --role-arn arn:aws:iam::<ACCOUNT_ID>:role/mr-lister-phase6-foundation-cfn-dev \
  --resource-types AWS::DynamoDB::Table AWS::S3::Bucket AWS::S3::BucketPolicy \
  --tags Key=DeploymentClass,Value=FOUNDATION_ONLY Key=Environment,Value=dev Key=Project,Value=MrLister \
  --description "Mr Lister Phase 6 create-only foundation 689897c254c9db97aa75d508f140980f9b6a5129c0c1fa0121eb8d6ef1e64874" \
  --on-stack-failure DO_NOTHING \
  --profile mr-lister-dev \
  --region us-west-2
```

Wait for generation, then capture both the change set and its original template:

```shell
aws cloudformation wait change-set-create-complete \
  --stack-name mr-lister-phase6-dev \
  --change-set-name mr-lister-phase6-dev-foundation-create-689897c254c9 \
  --profile mr-lister-dev \
  --region us-west-2

aws cloudformation describe-change-set \
  --stack-name mr-lister-phase6-dev \
  --change-set-name mr-lister-phase6-dev-foundation-create-689897c254c9 \
  --profile mr-lister-dev \
  --region us-west-2 \
  --output json > <EVIDENCE_DIR>/change-set.json

aws cloudformation get-template \
  --stack-name mr-lister-phase6-dev \
  --change-set-name mr-lister-phase6-dev-foundation-create-689897c254c9 \
  --template-stage Original \
  --profile mr-lister-dev \
  --region us-west-2 \
  --output json > <EVIDENCE_DIR>/change-set-template.json
```

Run the mandatory offline review gate. It rechecks the prior absence, original template, exact
execution role, `CREATE` type, three `Add` actions, no replacement, and no fourth resource:

```shell
.venv/bin/python -m tools.verify_phase6_foundation_deployment \
  change-set \
  --template infra/phase6/foundation.json \
  --absence-observation <EVIDENCE_DIR>/stack-absence.json \
  --observation <EVIDENCE_DIR>/change-set.json \
  --template-observation <EVIDENCE_DIR>/change-set-template.json \
  --account-id <ACCOUNT_ID> \
  --region us-west-2 \
  --environment-name dev \
  --stack-name mr-lister-phase6-dev \
  --execution-role-arn arn:aws:iam::<ACCOUNT_ID>:role/mr-lister-phase6-foundation-cfn-dev \
  --deployer-arn arn:aws:iam::<ACCOUNT_ID>:user/mr-lister-dev
```

Do not execute unless that command succeeds and a human review agrees that the three changes are
adds for `OperationalStateTable`, `PrivateArtifactBucket`, and `PrivateArtifactBucketPolicy`.

## Execute once and enable termination protection

```shell
aws cloudformation execute-change-set \
  --stack-name mr-lister-phase6-dev \
  --change-set-name mr-lister-phase6-dev-foundation-create-689897c254c9 \
  --profile mr-lister-dev \
  --region us-west-2

aws cloudformation wait stack-create-complete \
  --stack-name mr-lister-phase6-dev \
  --profile mr-lister-dev \
  --region us-west-2

aws cloudformation update-termination-protection \
  --enable-termination-protection \
  --stack-name mr-lister-phase6-dev \
  --profile mr-lister-dev \
  --region us-west-2
```

Never rerun the foundation command. Once this stack exists, the create-only verifier deliberately
refuses another foundation application.

## Capture post-create observations

Capture the stack, its exact resources, and the active DynamoDB controls:

```shell
aws cloudformation describe-stacks --stack-name mr-lister-phase6-dev --profile mr-lister-dev --region us-west-2 --output json > <EVIDENCE_DIR>/stack.json
aws cloudformation list-stack-resources --stack-name mr-lister-phase6-dev --profile mr-lister-dev --region us-west-2 --output json > <EVIDENCE_DIR>/stack-resources.json
aws dynamodb describe-table --table-name mr-lister-phase6-dev --profile mr-lister-dev --region us-west-2 --output json > <EVIDENCE_DIR>/table.json
aws dynamodb describe-time-to-live --table-name mr-lister-phase6-dev --profile mr-lister-dev --region us-west-2 --output json > <EVIDENCE_DIR>/table-ttl.json
aws dynamodb describe-continuous-backups --table-name mr-lister-phase6-dev --profile mr-lister-dev --region us-west-2 --output json > <EVIDENCE_DIR>/table-backups.json
aws dynamodb list-tags-of-resource --resource-arn arn:aws:dynamodb:us-west-2:<ACCOUNT_ID>:table/mr-lister-phase6-dev --profile mr-lister-dev --region us-west-2 --output json > <EVIDENCE_DIR>/table-tags.json
```

TTL and point-in-time recovery are asynchronous. If either capture reports an enabling state, wait
and recapture it; do not weaken or edit the verifier expectation.

Capture the bucket controls, substituting the exact account ID in the bucket name:

```shell
aws s3api get-bucket-encryption --bucket mr-lister-phase6-artifacts-dev-<ACCOUNT_ID>-us-west-2 --profile mr-lister-dev --region us-west-2 --output json > <EVIDENCE_DIR>/bucket-encryption.json
aws s3api get-bucket-versioning --bucket mr-lister-phase6-artifacts-dev-<ACCOUNT_ID>-us-west-2 --profile mr-lister-dev --region us-west-2 --output json > <EVIDENCE_DIR>/bucket-versioning.json
aws s3api get-public-access-block --bucket mr-lister-phase6-artifacts-dev-<ACCOUNT_ID>-us-west-2 --profile mr-lister-dev --region us-west-2 --output json > <EVIDENCE_DIR>/bucket-public-access-block.json
aws s3api get-bucket-ownership-controls --bucket mr-lister-phase6-artifacts-dev-<ACCOUNT_ID>-us-west-2 --profile mr-lister-dev --region us-west-2 --output json > <EVIDENCE_DIR>/bucket-ownership-controls.json
aws s3api get-bucket-lifecycle-configuration --bucket mr-lister-phase6-artifacts-dev-<ACCOUNT_ID>-us-west-2 --profile mr-lister-dev --region us-west-2 --output json > <EVIDENCE_DIR>/bucket-lifecycle.json
aws s3api get-bucket-tagging --bucket mr-lister-phase6-artifacts-dev-<ACCOUNT_ID>-us-west-2 --profile mr-lister-dev --region us-west-2 --output json > <EVIDENCE_DIR>/bucket-tags.json
aws s3api get-bucket-policy --bucket mr-lister-phase6-artifacts-dev-<ACCOUNT_ID>-us-west-2 --profile mr-lister-dev --region us-west-2 --output json > <EVIDENCE_DIR>/bucket-policy.json
aws s3api get-bucket-policy-status --bucket mr-lister-phase6-artifacts-dev-<ACCOUNT_ID>-us-west-2 --profile mr-lister-dev --region us-west-2 --output json > <EVIDENCE_DIR>/bucket-policy-status.json
```

The foundation intentionally has no browser CORS rule. Run `get-bucket-cors`, retain its unedited
standard error, and proceed only for `NoSuchCORSConfiguration` with HTTP 404. Normalize that result
as `<EVIDENCE_DIR>/bucket-cors-absence.json`:

```json
{
  "bucket_name": "mr-lister-phase6-artifacts-dev-<ACCOUNT_ID>-us-west-2",
  "error_code": "NoSuchCORSConfiguration",
  "format": "mr-lister-s3-cors-absence-v1",
  "http_status_code": 404,
  "operation": "GetBucketCors"
}
```

## Final deployed gate and downstream handoff

The evidence directory must now contain these JSON files:

```text
caller-identity.json
stack-absence.json
change-set.json
change-set-template.json
stack.json
stack-resources.json
table.json
table-ttl.json
table-backups.json
table-tags.json
bucket-encryption.json
bucket-versioning.json
bucket-public-access-block.json
bucket-ownership-controls.json
bucket-lifecycle.json
bucket-tags.json
bucket-policy.json
bucket-policy-status.json
bucket-cors-absence.json
```

Run the final offline gate and retain its stdout as `foundation-binding.json`:

```shell
.venv/bin/python -m tools.verify_phase6_foundation_deployment \
  deployed \
  --evidence-directory <EVIDENCE_DIR> \
  --template infra/phase6/foundation.json \
  --account-id <ACCOUNT_ID> \
  --region us-west-2 \
  --environment-name dev \
  --stack-name mr-lister-phase6-dev \
  --execution-role-arn arn:aws:iam::<ACCOUNT_ID>:role/mr-lister-phase6-foundation-cfn-dev \
  --deployer-arn arn:aws:iam::<ACCOUNT_ID>:user/mr-lister-dev \
  > <EVIDENCE_DIR>/foundation-binding.json
```

That descriptor is the seam for later deployment work:

- the AgentCore release verifier must consume the exact account, Region, environment, table,
  stream, and bucket binding rather than rediscovering or renaming them;
- the full Phase 6 SAM verifier must target the same stack ID with a separately authorized and
  reviewed `UPDATE`, prove that these three logical resources are neither removed nor replaced,
  and replace `FOUNDATION_ONLY` only when the runtime release gates pass;
- the Phase 7 read-only guard may bind to this table only after its own sealed artifact and IAM
  verifier pass.

Those later transitions are intentionally outside this create-only tool. A successful foundation
gate proves durable storage, not AgentCore, SAM runtime, provider, browser, or publication readiness.

After the final gate succeeds and the evidence is retained, root should delete only the
`mr-lister-phase6-foundation-bootstrap` stack through the console. That detaches and removes the
temporary developer managed policy. The CloudFormation execution role has `DeletionPolicy: Retain`
because its ARN is permanently recorded on the foundation stack; do not manually delete that role.
