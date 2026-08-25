# Phase 6 core-runtime staging

`tools/render_phase6_core_sam_staging.py` produces a domain-independent backend staging
template. It is an intermediate deployment surface, not the final web application and not a
traffic-activation mechanism.

The rendered resource set is intentionally fixed:

- the retained operational table, private artifact bucket, and bucket policy;
- seven non-web Lambda functions with their exact roles and log groups;
- four Step Functions state machines with their exact roles and log groups;
- the execution-recovery schedule, Lambda permission, DLQ, and DLQ policy.

CloudFront, Cognito, the HTTP API, seller-web assets, and the ACM certificate parameter are not
present. `ApplicationOrigin` remains because the authoritative private bucket CORS property uses
it. It may be the intended future lowercase HTTPS hostname; this staging segment does not create,
resolve, validate, or serve that hostname and does not require an ACM certificate.

## Fail-closed authorities

The renderer accepts only these checked local authorities:

- `infra/phase6/template.json`, SHA-256
  `9a110b3e813ed23102033ace67341d9cb4015274d7acc9f0fff6c08439c57ed7`;
- all four `infra/phase6/statemachine/*.asl.json` definitions at their hardcoded SHA-256 values;
- the canonical restored `.mr_lister_private/phase6-deployment` and
  `.mr_lister_private/phase6-artifacts` trees, verified against the current checkout;
- the canonical deployed-foundation binding for the exact existing stack, table, and bucket;
- the byte-exact ignored AgentCore runtime-create input and render manifest produced by
  `tools.render_phase6_agentcore_direct_codezip`;
- the canonical `mr-lister-phase6-agentcore-runtime-v1-evidence-v1` document joining the complete
  `CreateAgentRuntime` response to `GetAgentRuntime` requested explicitly at version `1` and
  `ListTagsForResource` for that exact runtime ARN. The Get response must reproduce the sealed
  CodeZip S3 bucket/key/VersionId, role, environment, lifecycle, network, protocol, runtime,
  entrypoint, name, and description, and must contain exactly
  `metadataConfiguration.requireMMDSV2=true`; missing, null, or false MMDSv2 state blocks this core
  renderer through the shared runtime verifier. If a new v1 create does not return that state, the
  v1-only flow stops before endpoint/core staging and requires a separately reviewed v2 update
  design; the tag response must equal the sealed four-tag set;
- a canonical normalized AgentCore endpoint observation that preserves whether AWS omitted its
  optional version/failure fields. It must bind the exact proven runtime ARN, endpoint ARN, and
  `phase6_v1_dev` name, report `READY`, and have `liveVersion` exactly `1`. At stable `READY`, AWS
  may omit `targetVersion` and `failureReason`; when present, `targetVersion` must equal `1` and
  `failureReason` must be null or empty. The canonical observation preserves either field's absence
  rather than synthesizing it;
- canonical AgentCore and Lambda S3 release-object evidence documents accepted by
  `tools/verify_phase6_s3_release_object.py`. AgentCore retains the complete common-v2
  upload/readback/revocation proof. Lambda may use either that proof or the narrower, Lambda-only
  `mr-lister-phase6-s3-manual-root-lambda-evidence-v1` readback used by
  `SIMPLE_ROOT_RUNTIME_DEPLOYMENT.md`. Both formats bind the exact account, Region, bucket,
  content-addressed key, nonmoving VersionId, checksum-enabled `HeadObject` result, and a
  singleton/current `ListObjectVersions` result. The decoded S3 `ChecksumSHA256` must equal the
  corresponding local sealed archive SHA-256 and `ContentLength` must equal its exact size.

A VersionId or runtime ID by itself is not sufficient. The renderer does not build, reseal,
package, upload, call an AWS client, or start a subprocess.

## Render and verify

Supply real exact values; placeholder tokens and moving values such as `latest`, `current`, or
`DEFAULT` are rejected. The relevant invocation shape is:

```text
.venv/bin/python -m tools.render_phase6_core_sam_staging \
  --account-id "$ACCOUNT_ID" \
  --region us-west-2 \
  --environment dev \
  --foundation-stack-id "$FOUNDATION_STACK_ID" \
  --foundation-binding "$FOUNDATION_BINDING" \
  --release-fingerprint "$RELEASE_FINGERPRINT" \
  --agentcore-runtime-arn "$AGENTCORE_RUNTIME_ARN" \
  --agentcore-runtime-endpoint-arn "$AGENTCORE_RUNTIME_ENDPOINT_ARN" \
  --agentcore-runtime-version "$AGENTCORE_RUNTIME_VERSION" \
  --agentcore-runtime-qualifier "$AGENTCORE_RUNTIME_QUALIFIER" \
  --agentcore-runtime-binding-fingerprint "$AGENTCORE_BINDING_FINGERPRINT" \
  --agentcore-endpoint-observation "$AGENTCORE_READY_OBSERVATION" \
  --agentcore-object-evidence "$AGENTCORE_OBJECT_EVIDENCE" \
  --agentcore-runtime-v1-evidence "$AGENTCORE_RUNTIME_V1_EVIDENCE" \
  --printify-secret-arn "$PRINTIFY_SECRET_ARN" \
  --application-origin "$FUTURE_APPLICATION_ORIGIN" \
  --lambda-artifact-bucket "$LAMBDA_ARTIFACT_BUCKET" \
  --lambda-artifact-key "$LAMBDA_ARTIFACT_KEY" \
  --lambda-artifact-version "$LAMBDA_ARTIFACT_VERSION_ID" \
  --lambda-object-evidence "$LAMBDA_OBJECT_EVIDENCE" \
  --write-staged
```

The only output path is:

```text
.mr_lister_private/phase6-core-sam/template.core-release-bound-staged.local.json
```

It is ignored, created exclusively, and never overwritten. Re-run the same command with
`--verify-staged` instead of `--write-staged` to recompute every authority and require byte-for-byte
identity. Delete or archive the local output deliberately before rendering a different reviewed
binding.

The output keeps `MR_LISTER_PHASE6_SCAFFOLD_ONLY=true` and reports
`CORE_RELEASE_BOUND_STAGED`. Metadata records the hashes of the joined runtime evidence, sealed
runtime-create input and manifest, AgentCore object evidence, and exact AgentCore object version,
so a later review can reproduce the full runtime-to-artifact join. It also records and validates
one exact closed disabled-trigger set:

- `SourceVersionRetentionFunction.Events.SourceVersionRetentionSweep` has `Enabled: false`;
- `TerminalOperationalCleanupFunction.Events.TerminalOperationalCleanupSweep` has
  `Enabled: false`;
- `DispatcherFunction.Events.DueWorkSweep` has `Enabled: false`;
- `DispatcherFunction.Events.OperationalStateChanges` has `Enabled: false`;
- `StuckExecutionRecoveryScheduleRule` has `State: DISABLED`.

The checked source authority intentionally remains activation-ready, but rendering changes all
five trigger states to inert values. Missing state fields, active values, omitted triggers, and
additional asynchronous triggers are rejected, and the exact set is repeated in template
metadata. Therefore creating this staging segment cannot start scheduled or DynamoDB-stream work.

Staging also changes `ReservedConcurrentExecutions` from `1` to `0` on exactly these three
maintenance functions, while rejecting a missing cap or a cap on any other function:

- `SourceVersionRetentionFunction`;
- `TerminalOperationalCleanupFunction`; and
- `StuckExecutionRecoveryFunction`.

Zero reserved concurrency gives those functions no invocation capacity without consuming from the
account's unreserved concurrency pool. This is independent fail-closed protection in addition to
the disabled triggers, and lets the inert stack deploy in accounts whose concurrency quota has no
headroom above Lambda's required unreserved minimum. The checked source intentionally keeps the
three singleton values at `1`; both staging renderers prove the exact `1` to `0` transition rather
than silently accepting source drift.

`--activate` always fails. The separate reviewed transition renderer, live-state verifier,
change-set verifier, and two-update operator sequence are defined in
[`CORE_RUNTIME_ACTIVATION.md`](CORE_RUNTIME_ACTIVATION.md). They first remove reserved concurrency
from exactly these three functions while scaffold mode and every trigger remain inert, verify that
the live concurrency configuration is absent, and only then enable execution in a separately
reviewed update. If strict singleton execution is required instead, increase account quota
headroom and restore the reviewed value `1` before enabling triggers.
