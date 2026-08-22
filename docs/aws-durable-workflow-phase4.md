# Phase 4 durable AWS workflow

## Completed implementation checkpoint

Phase 4 keeps binary artwork in private S3 and operational records in one DynamoDB table. The
workflow uses the same application-owned `JobStore` contract for its in-memory and DynamoDB
adapters. Step Functions calls application commands; it does not write table items or assign job
states directly.

The current checkpoint implements durable intake, restartable preparation, guarded external-write
claims, version-bound approval waits, atomic job transitions, and the Standard Step
Functions/Lambda command layer. The bootstrap and application stacks are deployed in `us-west-2`,
and both the interrupted/resumed execution and a fresh untouched AWS canary reached `VERIFIED`.

## Single-table keys

The table uses string partition and sort keys named `PK` and `SK`. It is on-demand, encrypted with
an AWS-owned key, retained when the stack is deleted or replaced, and has TTL enabled on
`expires_at` for future approval-wait records.

| Item | PK | SK | Purpose |
| --- | --- | --- | --- |
| Job | `JOB#{job_id}` | `META` | Current state, record version, event sequence, and job contract |
| Artwork | `JOB#{job_id}` | `ARTWORK` | S3 pointer metadata, checksum, and selected profile |
| Analysis | `JOB#{job_id}` | `ANALYSIS` | Immutable artwork-analysis checkpoint |
| Listing checkpoint | `JOB#{job_id}` | `LISTING_CHECKPOINT` | Immutable generated-listing checkpoint |
| Review | `JOB#{job_id}` | `REVIEW#{version}` | Versioned review contract and immutable-content fingerprint |
| Event | `JOB#{job_id}` | `EVENT#{sequence}` | Ordered, sanitized workflow event |
| Write | `JOB#{job_id}` | `WRITE#{sha256(idempotency_key)}` | Claimed, completed, or reconciliation-required write |
| Approval wait | `JOB#{job_id}` | `APPROVAL_WAIT` | Review-bound callback token, status, and TTL |
| Intake claim | `IDEMPOTENCY#{sha256(key)}` | `CLAIM` | Request fingerprint to job mapping |

Raw idempotency keys are not used in DynamoDB keys. Artwork bytes, prompts, credentials, model
responses, and callback tokens are not stored in serialized operational payloads.

The approval task token is held in its dedicated wait item, encrypted at rest by DynamoDB, hidden
from model representations, and omitted from the sanitized payload copy. Only the approval command
and future callback Lambda require access to it.

## Transaction boundaries

Intake creation is one transaction containing:

1. the idempotency claim;
2. the initial job record;
3. artwork metadata and profile selection; and
4. the first audit event.

Every job transition is one transaction containing:

1. a replacement job record conditioned on the expected state, `record_version`, and
   `event_sequence`;
2. the next ordered event, conditioned not to exist; and
3. when applicable, the corresponding review version.

The application increments `record_version` and `event_sequence` exactly once. The persistence
adapter rejects changes to job identity and rejects transitions outside the canonical application
state graph before sending a transaction to DynamoDB. DynamoDB then provides the atomic
compare-and-set enforcement. A cancelled conditional transaction becomes the stable application
error `CONCURRENT_MODIFICATION`.

A review item's fingerprint excludes only approval status and the prepared-product identifier.
This permits those two lifecycle fields to be attached to the same review version while preventing
the listing, analysis, validation result, or product policy from being replaced in place.

## Restart and external-write behavior

Artwork analysis and generated listing output are persisted before review creation. A reconstructed
workflow skips any existing immutable checkpoint and continues from the first incomplete state.
Review validation, fake-draft creation, and the final transition into approval are independently
resumable.

Before calling a production adapter, the application creates a conditional write claim. A completed
claim contains the external result needed to resume a missed state transition without repeating the
call. If the adapter raises after dispatch, the claim becomes `reconciliation_required`; retries
stop safely rather than guessing whether a non-idempotent call succeeded. Phase 5 will supply the
provider-specific reconciliation operation.

Approval registration is bound to the current review version and expires through the table TTL.
Approval changes the job, review status, ordered event, and wait status in one transaction. A stale,
expired, or replayed callback cannot release a different review version; a replay of an already
consumed matching approval is harmless.

## Step Functions and Lambda command layer

The Standard workflow carries only `job_id`, `review_version`, and the callback token at the one
registration boundary that requires it. Binary artwork, listing content, prompts, credentials, and
raw provider output never enter execution input or history. Execution-data logging is disabled;
state-machine and Lambda log groups have 14-day retention.

The orchestration path is:

1. `PrepareJob` asks the application to resume from its first incomplete durable checkpoint.
2. `WaitForApproval` registers a review-bound callback token and pauses for up to seven days.
3. The separately invoked approval Lambda atomically validates/approves the exact review and
   consumes its wait record before calling `SendTaskSuccess`.
4. `FakePublish` performs or resumes the idempotency-claimed fake publication through `PUBLISHED`.
5. `FakeVerify` independently advances a persisted publication through `VERIFIED`.

A restarted execution routes existing `APPROVED`, `PUBLISHING`, `PUBLISHED`, and `VERIFIED` jobs to
the first command still needed. Those routes request work only: application contracts and DynamoDB
conditions remain the authority for every transition. Retry rules recognize only the sanitized
application retry exception and documented transient Lambda service errors; all other errors flow
to a static, sanitized failure state.

The current prepare Lambda intentionally uses the deterministic fake intelligence/production
adapters. It proves durability without adding paid inference or real marketplace effects. Wiring
the already-proven AgentCore preparation runtime is an opt-in configuration: a blank runtime ARN
keeps deterministic preparation, while an exact ARN enables the contract-validating invocation
bridge and adds only `bedrock-agentcore:InvokeAgentRuntime` for that resource. The existing Phase 3
synthetic runtime must be upgraded to use the Phase 4 DynamoDB/S3 stores before that switch is
enabled. Artwork intake also remains outside Step Functions: sending a supported
25 MiB source PNG through Lambda or execution input would violate those services' payload limits.
The later upload surface should stage the binary directly in private S3 and submit identifiers.

## SAM build and gated deployment

SAM 1.165 or newer validates and builds this stack with the checked-in `phase4-dev` configuration:

```bash
SAM_CLI_TELEMETRY=0 sam validate --config-env phase4-dev
SAM_CLI_TELEMETRY=0 sam build --config-env phase4-dev
```

The Makefile builder uses SAM's supported in-source mode but copies only the `mr_lister` package,
the Lambda bridge, product profiles, and runtime dependencies into each artifact. Binary wheels are
explicitly resolved for CPython 3.13 on Linux ARM64; the host Mac's Python version is irrelevant.

The developer identity does not receive general CloudFormation or IAM administration. Before the
first deployment, an administrator applies `infra/phase4/bootstrap.json` once in `us-west-2` with
stack name `mr-lister-phase4-bootstrap` and acknowledges `CAPABILITY_NAMED_IAM`. The bootstrap:

- creates a private, versioned SAM artifact bucket;
- creates a CloudFormation execution role limited to named Phase 4 resources; and
- attaches a developer-group policy that can manage only the Phase 4 stack, its artifact prefix,
  and `iam:PassRole` for that one execution role.

Afterward, deployments use the bootstrap outputs rather than broad developer permissions:

```bash
sam deploy --config-env phase4-dev \
  --s3-bucket <DeploymentArtifactBucketName> \
  --role-arn <CloudFormationExecutionRoleArn> \
  --no-execute-changeset
```

That command uploads artifacts and creates a reviewable change set; it does not execute it.

Deployment is intentionally review-gated. `sam deploy --config-env phase4-dev` packages the built
artifacts and presents a CloudFormation change set before execution. After deployment, the canary
requires a separate write gate:

```bash
AWS_PROFILE=mr-lister-dev \
AWS_DEFAULT_REGION=us-west-2 \
MR_LISTER_RUN_PHASE4_AWS_CANARY=1 \
.venv/bin/python -m tools.phase4_aws_canary
```

The canary refuses root credentials, uses synthetic artwork and fake production, waits for the
server-side approval record without reading its task token, reconstructs the application service,
and requires `VERIFIED` plus exactly one fake draft and one fake publication record.

## Live acceptance evidence

The deployed stack reached `CREATE_COMPLETE`, accepted two in-place `UPDATE_COMPLETE` code/IAM
corrections, and the normal SAM update path subsequently reported no changes to deploy. No update
replaced the DynamoDB table, private S3 bucket, or state machine.

The first live execution proved process independence more strongly than a simple happy-path run:
the local canary process ended while the Standard workflow was paused, the Lambda package and two
command roles were updated, and a replay-safe approval invocation resumed the original execution.
It completed at `VERIFIED` with a consumed version-1 approval, 14 ordered events, and one completed
draft-sync plus one completed fake-publication write.

A second untouched canary completed without manual intervention:

- job `job_3f7b082f274e453fbb31dc27d07cdba8`;
- state `verified`, record version 11, and event sequence 14;
- exactly two completed external-write checkpoints; and
- AgentCore disabled, fake production only, and no paid model inference.

A boolean-only scan of its 27-event Step Functions history found no PNG bytes, generated listing
body, prompt text, or credential indicators. The expected opaque `task_token` field was present at
the callback registration boundary; execution-history access is therefore restricted to the
Phase 4 developer/operator policy. Execution-data logging remains disabled, so the token and state
payloads are not copied into CloudWatch Logs.

Live testing also exposed and closed three deployment/runtime gaps: SAM's implicit broad Lambda
logging policy was replaced with explicit roles, optional Strands imports were made lazy so the
deterministic Lambdas do not require that SDK, and transactional DynamoDB `Put` authorization was
added to approval and verification roles. Provider `ClientError` messages are now converted to
sanitized retryable or terminal command exceptions. Final offline verification is 177 passing
tests, with 11 paid/live Bedrock cases still explicitly gated.

## Secrets Manager boundary

Phase 4 includes a `SecretReader` port, a narrow Secrets Manager adapter, and a policy template that
allows only `DescribeSecret` and `GetSecretValue` on one exact supplied ARN. No secret is created,
no placeholder credential is manufactured, and no Lambda receives this permission yet. Phase 5
will provide the legitimate Printify secret ARN and attach the policy only to the real marketplace
adapter's function.

## Phase 6 successor topology

The seven-day task-token wait remains valid Phase 4 durability evidence. It is not the Phase 6
seller-control topology. ADR 0009 ends bounded preparation after application state reaches human
review, persists the human pause in DynamoDB, and uses version-checked application commands for
revision, approval, and cancellation. Phase 7 will start a separate guarded publication execution
from an immutable approved snapshot. This successor decision preserves the Phase 4 evidence rather
than rewriting or migrating its ownerless records.
