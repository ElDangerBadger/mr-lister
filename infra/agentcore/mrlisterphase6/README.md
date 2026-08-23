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

The execution role is external on purpose. Rendered
`phase6-agentcore-runtime-policy.local.json` grants the standard AgentCore runtime log writes plus
only these application calls:

- DynamoDB `GetItem`, `PutItem`, and `TransactWriteItems` on the exact environment table, restricted
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
- [AgentCore Runtime execution-role permissions](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-permissions.html)
- [Immutable runtime versions and custom endpoints](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/agent-runtime-versioning.html)
- [AgentCore runtime log-group naming](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/diagnose-evaluation-skill-source.html)
