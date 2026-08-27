# Phase 6 active-core-preserving web-edge transition

This runbook defines the narrow deployment boundary for adding the Phase 6 seller web edge to the
already active, draft-only backend. It does not authorize publication, provider mutation, seller
creation, static-site upload, or DNS changes.

The checked bootstrap is
[`web-edge-role-bootstrap.json`](web-edge-role-bootstrap.json). It temporarily attaches three
expiring managed policies to the retained `mr-lister-phase6-runtime-cfn-dev` CloudFormation
execution role. The bootstrap does not create, replace, or take ownership of that retained role.
Deleting the bootstrap stack detaches and deletes the temporary policies while leaving both the
application stack and its retained execution role intact.

## Required target shape

The complete source `template.json` remains a scaffold source, not a direct deployment target. Its
global scaffold value is `true`. The web renderer must instead start from the exact deployed
`CORE_RUNTIME_ACTIVE_DRAFT_ONLY` predecessor, preserve its global scaffold value of `false`, and
graft only the checked web, identity, API, and observability resources.

After the SAM transform, the expected change-set closure is exactly 78 additions:

| Count | Resource type |
| ---: | --- |
| 1 | `AWS::ApiGatewayV2::Api` |
| 1 | `AWS::ApiGatewayV2::Stage` |
| 3 | `AWS::CloudFront::CachePolicy` |
| 1 | `AWS::CloudFront::Distribution` |
| 2 | `AWS::CloudFront::Function` |
| 1 | `AWS::CloudFront::OriginAccessControl` |
| 2 | `AWS::CloudFront::ResponseHeadersPolicy` |
| 32 | `AWS::CloudWatch::Alarm` |
| 5 | Cognito user-pool resources |
| 3 | API Lambda IAM roles |
| 1 | `AWS::KMS::Key` |
| 3 | API Lambda functions |
| 15 | API Lambda permissions |
| 4 | CloudWatch Logs groups |
| 1 | private web S3 bucket |
| 1 | private web S3 bucket policy |
| 1 | SNS topic |
| 1 | SNS topic policy |

The accepted change set has zero modifications, removals, imports, or replacements. In particular,
it must not modify any existing Lambda function, role, event source, EventBridge rule, state
machine, queue, table, private artifact bucket, or active-core metadata.

The target binds these public identifiers exactly:

- application origin: `https://massskutiny.com`;
- certificate:
  `arn:aws:acm:us-east-1:384627057108:certificate/28b8cddb-a0d7-4dc8-98de-26fd87cb5b79`;
- canonical target SHA-256:
  `74560fb066f66759f5baa8a3be15c6370e20bfa884a50e0b4b7e0457592ebff4`;
- change-set name: `mr-lister-phase6-dev-web-edge-74560fb066f6`.

Both custom no-store cache policies use minimum/default/maximum TTLs of `0/0/1`. The positive
one-second ceiling keeps CloudFront from applying its restricted all-zero caching-disabled shape,
while the zero minimum/default and origin `no-store` contract preserve the intended no-cache
behavior and the exact API header/query forwarding rules.

## What the temporary bootstrap grants

The retained execution role already has the exact release-bound Lambda archive read and the
control-plane permissions needed for named Phase 6 Lambda functions, their inline roles, Lambda
log groups, and Lambda permissions. The temporary policies add only the missing web closure:

- creation/configuration/rollback of the exact private
  `mr-lister-phase6-web-dev-384627057108-us-west-2` bucket, with no object read, write, or delete;
- CloudFront distribution, function, OAC, cache-policy, and response-header-policy control plane;
- exact certificate description, tagged Cognito user-pool control plane, HTTP API control plane
  including tag-on-create authorization for SAM's generated `$default` stage, the named API
  access-log group, and regional log-delivery configuration;
- a tagged KMS alarm key, exact SNS alarm topic, and only alarms named
  `mr-lister-phase6-dev-*`.

Tagged Cognito and KMS creation requires the three exact resource tags while allowing only the
closed tag-key set formed by those tags, the preserved `DeploymentClass` stack tag, and
CloudFormation's three `aws:cloudformation:*` system tags. The policy contains no tag wildcard.
Tag-on-create permission is colocated with the corresponding create action; post-create authority
still requires the exact project, environment, and data-classification resource tags.

CloudWatch Logs authority remains limited to the exact seller API access-log name. It includes
both AWS-documented IAM representations of that one log group: the base ARN required by tagging
actions and its `:*` companion required by other log-group operations during tagged creation.

Every temporary statement expires at the exclusive UTC `NotAfter` value. The only global
CloudFront create permissions are actions whose resource IDs do not exist until creation. They
remain bounded by all of the following together:

1. the execution role trusts only CloudFormation;
2. the developer can pass only that retained execution role to CloudFormation;
3. the deployer can create only the exact named change set from the exact versioned template URL;
4. the template object key contains the sealed release fingerprint and target template SHA-256;
5. execution is absent in `PREPARE` and requires a separate root-applied `EXECUTE` stage;
6. every permission expires and the bootstrap is deleted after evidence capture.

API Gateway resource paths, regional CloudWatch Logs delivery, and most CloudFront resources also
receive physical IDs only during creation. Their temporary control-plane permissions therefore
cannot all be pre-scoped to final ARNs. API stage tag-on-create authority is isolated in a separate
statement limited to the regional `/apis/*/stages` and `/apis/*/stages/*` paths. It requires the
exact project, environment, and deployment-class request tags, permits only the closed SAM and
CloudFormation tag-key set, and expires with the same `NotAfter` boundary. Those permissions exist
only on the CloudFormation service role, which only CloudFormation can assume; the developer-facing
role can prepare only the exact versioned target. When root changes the bootstrap to `EXECUTE`, that
role's control policy removes
all create/delete/pass-role/template-read authority and replaces it with `ExecuteChangeSet` on the
exact parent stack ARN only when `cloudformation:ChangeSetName` equals the reviewed full change-set
ARN. The exact target, immutable object version, change-set verifier, short expiry, and bootstrap
deletion form one compositional boundary.

The bootstrap grants none of the following:

- S3 object upload, read, deletion, or static-site synchronization;
- DynamoDB item reads or writes;
- Lambda invocation or Step Functions execution;
- Secrets Manager, Bedrock, AgentCore, Printify, publication, order, or fulfillment access;
- Cognito seller administration or invitation;
- Route 53 changes or CloudFront invalidations;
- direct `CreateStack`, `UpdateStack`, or `DeleteStack` authority.

Rollback permissions can delete only resources created by this web update. KMS rollback uses
`ScheduleKeyDeletion` for the newly created, project-tagged alarm key. It does not grant arbitrary
key deletion or cryptographic data-plane operations.

## Approval boundaries

Each row is a distinct approval boundary. Approval of one row does not authorize any later row.

| Boundary | Identity | Effect |
| --- | --- | --- |
| Upload the rendered template | separately authorized administrator/release path | Writes one immutable template object and returns its exact S3 VersionId. The bootstrap does not grant this upload. |
| Create/update bootstrap in `PREPARE` | root | Temporarily attaches execution policies and creates the exact deployer role. This is an IAM mutation, but cannot execute the application change set. |
| Create exact application change set | `mr-lister-dev` through the deployer role | Creates an `UPDATE` change set only; it does not deploy resources. |
| Review and verify change set | read-only | Confirms the exact target bytes and the 78-add, zero-modify/remove shape; the bootstrap policy separately enforces the service role and immutable versioned template URL. |
| Update bootstrap to `EXECUTE` | root | Atomically removes change-set preparation authority and grants `ExecuteChangeSet` on the fixed parent stack only when the change-set condition equals the supplied full reviewed ARN. This is the explicit deployment approval. |
| Execute exact application change set | `mr-lister-dev` through the deployer role | Creates the live CloudFront, Cognito, HTTP API, API Lambda, web bucket, and alarm surfaces. |
| Delete bootstrap | root | Detaches all temporary authority after stack completion and evidence capture. |
| Upload website/runtime config | separate future approval | Writes reviewed build objects to the private web bucket. |
| Create Route 53 A/AAAA aliases | separate future approval | Routes `massskutiny.com` to the verified CloudFront distribution. |
| Invite a seller and run live acceptance | separate future approval | Creates the initial Cognito seller and begins authenticated end-to-end testing. |

CloudFront and the API Gateway default endpoint are live as soon as the application change set
finishes, even before the final Route 53 aliases exist. For that reason, `EXECUTE` is a meaningful
public-surface approval and is never implied by `PREPARE`.

## Preconditions for `PREPARE`

Before applying the bootstrap, retain read-only evidence that:

1. the caller is in account `384627057108` and region `us-west-2`;
2. the exact application stack is `UPDATE_COMPLETE` with termination protection enabled;
3. the live backend is still `CORE_RUNTIME_ACTIVE_DRAFT_ONLY`;
4. all active triggers remain enabled, every scaffold value remains `false`, the provider-draft
   boundary remains draft-only, and the AgentCore v1 endpoint remains `READY`;
5. the rendered target has a recorded canonical SHA-256 and its S3 readback matches byte-for-byte;
6. the Lambda archive key, SHA-256, and S3 VersionId remain the frozen Phase 6 bindings;
7. the ACM certificate is `ISSUED` in `us-east-1` and covers `massskutiny.com`.

Apply the bootstrap only as stack `mr-lister-phase6-web-edge-bootstrap-dev` in `us-west-2`, with
`CAPABILITY_NAMED_IAM`, an explicit near-term `NotAfter`, `BootstrapStage=PREPARE`, and
`ReviewedChangeSetId=PREPARE_NOT_REVIEWED`. Supply every other parameter explicitly; none of the
identity, version, fingerprint, origin, certificate, or stage parameters has a permissive default.
After every bootstrap create or update, allow IAM propagation, acquire a fresh role session, and
read back the attached default policy versions before using them. Simulate the Cognito and KMS
create context with the complete closed tag-key set and require tagged create/readback authority;
a readable managed-policy version alone is not a live authorization result.

## Repository-controlled artifact sequence

All repository tools in this sequence are offline. They accept only repository-contained inputs,
write only beneath `.mr_lister_private`, and do not call AWS, a browser, or a provider.

1. Run `.venv/bin/python -m tools.render_phase6_web_edge_transition --write` with the exact frozen
   foundation, release, Lambda, AgentCore, origin, and certificate bindings. The only deployment
   target is
   `.mr_lister_private/phase6-web-edge-transition/template.web-edge-active-draft-only.local.json`;
   its canonical SHA-256 must be
   `74560fb066f66759f5baa8a3be15c6370e20bfa884a50e0b4b7e0457592ebff4`.
2. Re-run the same renderer with `--verify` before uploading that target. Uploading the template is
   its own approval boundary; retain the returned object VersionId and bind it into the bootstrap.
3. After the `PREPARE` change set reaches `CREATE_COMPLETE`, normalize the original template,
   processed template, and change-set observations into repository-private evidence. Run
   `.venv/bin/python -m tools.verify_phase6_web_edge_change_set` with exactly these six inputs:
   `--predecessor-original-template-observation`,
   `--predecessor-processed-template-observation`, `--target-template`,
   `--change-set-observation`, `--target-original-template-observation`, and
   `--target-processed-template-observation`. Do not advance to `EXECUTE` unless the verifier
   prints its canonical success record for the exact 78-add closure.
4. Build the already-tested web application locally and run
   `.venv/bin/python -m tools.prepare_phase6_web_release` against `web/dist`. The tool accepts only
   the four digest-bound browser-gate files and writes a create-only private release manifest. It
   does not upload them.
5. After the application stack reaches `UPDATE_COMPLETE`, capture its exact 19 outputs in the
   canonical repository-private form required by
   `.venv/bin/python -m tools.bind_phase6_runtime_config`. The binder creates the six-field public
   `runtime-config.json` and a separate upload manifest; neither file contains credentials.
6. Within one 15-minute capture window, normalize the read-only stack, ACM, CloudFront, S3,
   Cognito, and API observations into one closed evidence document and run
   `.venv/bin/python -m tools.verify_phase6_web_live_state EVIDENCE_PATH`. Retain the canonical
   success record and its digest. Asset upload remains a distinct approval after this
   infrastructure readback passes.
7. Only after the verified distribution is deployed, run
   `.venv/bin/python -m tools.render_phase6_dns_alias_change --write` with the independently
   confirmed hosted-zone ID, canonical `get-hosted-zone` observation, CloudFront domain, and
   canonical stack-output capture. The observation must prove the exact public `massskutiny.com`
   zone and that its Route 53 delegation set matches the four name servers independently confirmed
   at the registrar. Independently verify that the public parent delegation resolves to that same
   set before applying DNS. Review the fail-closed, apex-only `CREATE` request for `A` and `AAAA`
   before separately approving any Route 53 mutation; an existing record makes the request fail
   instead of replacing prior hosting.

Generated private artifacts are evidence and deployment inputs, not source files. Do not commit
them, substitute the raw scaffold template, or reuse captures from an earlier stack state.

The post-deploy verifier is a fresh critical-boundary readback, not an independent replacement for
the pre-execution template and change-set proof and not a general-purpose drift detector. Target
lineage comes from re-verifying the immutable change set immediately after the `EXECUTE` bootstrap
update and executing only its full ARN. The live gate then cross-joins the resulting stack,
certificate, distribution/OAC, private bucket policy, Cognito client, API authorization surface,
routes, and outputs.

## Change-set review gate

Create the application change set with all of these exact request bindings:

- stack ID: the supplied `FoundationStackId`;
- type: `UPDATE`;
- name: `mr-lister-phase6-dev-web-edge-74560fb066f6`;
- service role: `mr-lister-phase6-runtime-cfn-dev`;
- template URL: the bootstrap-computed HTTPS URL including the exact encoded S3 VersionId;
- capability: `CAPABILITY_NAMED_IAM`;
- tags: exactly the existing stack tags: `DeploymentClass=FOUNDATION_ONLY`, `Environment=dev`,
  and `Project=MrLister`. Preserving them avoids turning a nominally additive update into existing
  resource tag modifications; the temporary bootstrap and evidence record carry the web-edge
  deployment classification separately.

Reject and delete the unexecuted change set if any of these checks fails:

- original or processed template readback differs from the sealed target;
- any change is not `Add`;
- the transformed closure is not exactly the documented 78 resources;
- any replacement is requested;
- any existing physical ID, code binding, role, trigger, timeout, concurrency, workflow, bucket,
  table, queue, or active-core parameter changes;
- the application origin or certificate differs from the exact values above;
- CloudFormation reports an unexpected capability, hook, policy action, or dynamic evaluation.

`PREPARE` deliberately has no `cloudformation:ExecuteChangeSet`. After the review evidence is
accepted, root may update only `BootstrapStage` from `PREPARE` to `EXECUTE` and
`ReviewedChangeSetId` from the sentinel to the verifier-accepted full change-set ARN, keeping every
other parameter byte-for-byte unchanged. The same managed policy changes in place: its `EXECUTE`
branch contains no create/delete/pass-role or template-object read action. Re-capture and rerun the
change-set verifier after that update before execution.

## Completion and cleanup

Wait for the application stack to reach `UPDATE_COMPLETE`. Capture the new physical IDs and verify
that the core runtime is still active and draft-only before doing anything with static assets or
DNS. If CloudFormation rolls back, wait for rollback to finish and inspect events before cleanup;
do not extend the expiry or broaden permissions merely to force progress.

After a rollback, inventory every attempted logical resource before preparing another change set.
Any resource protected by `DeletionPolicy: Retain` can survive outside the stack and collide with
the corrected retry. Before the normal additive retry continues, prove that each retained resource
is absent or separately approved for removal. If adoption or import is required, stop and reseal a
separately reviewed recovery closure; the normal verifier allows no import or modification. Render
and verify the immutable target again. Corrected bytes require a new SHA-256 fingerprint; unchanged
bytes retain their canonical fingerprint. In either case, use a fresh uploaded object VersionId and
a freshly created change-set ARN. Never execute an already consumed or failed change set.

Once the application update and readback evidence are complete, delete only
`mr-lister-phase6-web-edge-bootstrap-dev` and wait for `DELETE_COMPLETE`. Confirm that all three
temporary execution-role policies, the stage-switched change-set control policy, the web-edge
deployer role, and the developer assume policy are gone. The retained
`mr-lister-phase6-runtime-cfn-dev` role and the `mr-lister-phase6-dev` application stack must
remain.

Expiration and bootstrap deletion revoke deployment authority; they do not disable or delete the
successfully deployed web resources. Static assets, DNS aliases, seller invitation, and live
browser acceptance remain later, separately reviewed operations.
