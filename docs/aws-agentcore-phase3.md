# Phase 3 AgentCore runbook

Mr Lister uses the official `bedrock-agentcore` Python SDK and current `@aws/agentcore` CLI. The
runtime serves non-streaming `POST /invocations` and `GET /ping` on `0.0.0.0:8080`. WebSocket,
MCP, A2A, memory, Gateway, and autonomous publication are intentionally deferred.

## Proven baseline

- Amazon Nova 2 Lite is the Phase 3 controller; Gemma 3 27B remains the image/listing worker.
- The controller is capped at four turns, 2,500 output tokens, and 12,000 cumulative tokens.
- Intake and preparation are separate idempotent workflow operations.
- The application binds one trusted job to each invocation; job IDs are absent from tool schemas.
- No agent mode exposes approval or publication.
- Audit records contain a correlation digest, timings, token counts, tool names, and stable codes;
  they exclude raw session/job IDs, prompts, artwork, credentials, and provider exception text.
- Automatic OpenTelemetry prompt/tool capture is disabled until its redaction behavior is
  separately accepted. AgentCore's service envelope can still log its runtime session ID.
- The narrow CodeZip archive is built from an explicit private staging bundle rather than the
  repository root.

The first-look Nova/Gemma evidence is in
[`phase3-controller-evaluation.md`](phase3-controller-evaluation.md).

## Local verification

```bash
.venv/bin/python -m pytest -m "not live_bedrock"
.venv/bin/ruff check .
.venv/bin/ruff format --check .
```

The official SDK entry point is `agentcore_runtime.py`. It can be smoked locally with the synthetic
job and fake intelligence/production adapters. That smoke makes real Nova controller calls, so keep
the same explicit profile and cost discipline used by the controller comparison.

## Render private deployment configuration

Account-specific files are ignored. Render them locally from the committed templates:

```bash
MR_LISTER_ACCOUNT_ID="$(aws sts get-caller-identity \
  --profile mr-lister-dev --query Account --output text)"
test "${#MR_LISTER_ACCOUNT_ID}" -eq 12

sed "s/<AWS_ACCOUNT_ID>/${MR_LISTER_ACCOUNT_ID}/g" \
  infra/agentcore/mrlisterphase3/agentcore/agentcore.json.tmpl \
  > infra/agentcore/mrlisterphase3/agentcore/agentcore.json
sed "s/<AWS_ACCOUNT_ID>/${MR_LISTER_ACCOUNT_ID}/g" \
  infra/agentcore/mrlisterphase3/agentcore/aws-targets.json.tmpl \
  > infra/agentcore/mrlisterphase3/agentcore/aws-targets.json
```

Build only the explicit runtime inputs, validate the schema, and package Linux ARM64 dependencies:

```bash
.venv/bin/python -m tools.build_agentcore_bundle
cd infra/agentcore/mrlisterphase3
../../../node_modules/.bin/agentcore validate --json
PATH="$(cd ../../.. && pwd)/.venv/bin:$PATH" \
  ../../../node_modules/.bin/agentcore package --directory . --runtime mr_lister_phase3
```

## One-time administrator gate

The runtime worker must not be an infrastructure administrator. During one short delegated-admin
or root session:

1. Create `mr-lister-agentcore-runtime-canary` with the rendered
   [`agentcore-runtime-trust-policy.json.tmpl`](../infra/iam/agentcore-runtime-trust-policy.json.tmpl).
   Its confused-deputy conditions trust only this account's AgentCore resources in `us-west-2`.
2. Render and attach
   [`agentcore-runtime-policy.json.tmpl`](../infra/iam/agentcore-runtime-policy.json.tmpl) as an
   attached customer-managed policy. It grants the documented AgentCore runtime log path plus only
   `bedrock:InvokeModel` for the Nova profile and destinations. OTel/X-Ray permissions remain absent.
3. Render
   [`agentcore-phase3-deployer-policy.json.tmpl`](../infra/iam/agentcore-phase3-deployer-policy.json.tmpl)
   and attach it to `mr-lister-dev` or `mr-lister-developers`. It grants runtime inspection,
   invocation/session stop, and read-only access to the standard and temporary synthetic-canary
   log paths.
4. Sign out of the administrator session.

The runtime role is a worker, not an infrastructure administrator. The developer policy has no
general S3 write, IAM administration, approval, or publication authority. Account-specific rendered
policies stay ignored; only reviewed templates are committed.

The external runtime role is intentional. The AgentCore console's default role and the current L3
construct can synthesize broader Bedrock access than this canary needs; IAM policies are additive,
so a narrow policy alongside a broad default would not create least privilege.

## Direct CodeZip deployment and update

Phase 3 deliberately used the AgentCore console's direct CodeZip path rather than making CDK
bootstrap a prerequisite for one disposable canary:

1. Open AgentCore Runtime in `us-west-2` and create or edit `mr_lister_phase3`.
2. Choose microVM, S3 source, and **Upload to S3**; upload
   `agentcore/mr_lister_phase3.zip` from the packaged project.
3. Select Python 3.13 with entry point `main.py`.
4. Use the existing `mr-lister-agentcore-runtime-canary` role; do not create a default role.
5. Keep public network mode, 15-minute idle timeout, and one-hour maximum lifetime for this canary.
6. Wait for both the runtime and `DEFAULT` endpoint to report `READY` before invoking.

Invoke only the synthetic job with an explicit profile, Region, unique 33-or-more-character session
ID, no SDK retries, and a bounded client timeout. A minimal SDK example is:

```python
client.invoke_agent_runtime(
    agentRuntimeArn=runtime_arn,
    runtimeSessionId="phase3-canary-session-0000000000000000000000000001",
    payload=json.dumps(
        {
            "job_id": "job_agentcore_synthetic_canary",
            "mode": "review",
            "instruction": "Inspect and validate the staged listing, then recommend the next human action.",
        }
    ).encode(),
)
```

Acceptance requires a structured decision with `requires_human_approval=true` and
`publication_authorized=false`, expected safe tool calls, a digest-only application audit record,
and no raw prompt, artwork bytes, credential, or provider exception in application logs.

## Accepted deployment evidence

Runtime version 2 and its `DEFAULT` endpoint reached `READY` in `us-west-2`. The accepted deployed
review completed successfully in 2.541 seconds, selected inspection and validation tools, required
human approval, and did not authorize publication. The sanitized application audit reported 3
cycles, 4,942 input tokens, 195 output tokens, the safe tool names, and a one-way correlation digest.
It contained no raw session/job ID, prompt, artwork, provider response, credential, or exception.

Temporary AWS vended `APPLICATION_LOGS` captured complete request payloads during synthetic-only
diagnosis and was removed before the Phase 3 freeze. A zero-model-cost follow-up produced the
expected standard runtime status record and no vended payload event. The standard
`/aws/bedrock-agentcore/runtimes/...` stream remains enabled and carries the accepted sanitized
application audit. Automatic OTel prompt/tool tracing remains disabled.

The deployed artifact is intentionally a synthetic controller canary with in-memory state, fake
intelligence, and fake production. It proves packaging, runtime/model/tool integration, and
authority boundaries; it is not the complete production deployment. Reducing the current
5,137-token controller baseline is recorded as follow-on refinement rather than a Phase 3 blocker.

The pinned AWS Node CLI currently reports two high-severity advisories in transitive development
dependencies, and its generated CDK project reports one. They are not included in the deployed
Python CodeZip. `npm audit fix` cannot apply them safely because the affected versions are pinned
inside AWS's packages; update the direct AgentCore CLI/CDK packages when AWS releases corrected
dependency pins.

Official references:

- [AgentCore Runtime HTTP protocol contract](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-http-protocol-contract.html)
- [Direct code deployment for Python](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-get-started-code-deploy-python.html)
- [AgentCore CLI quickstart](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-get-started-cli.html)
