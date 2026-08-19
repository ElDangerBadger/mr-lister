# Phase 2 AWS and Bedrock runbook

This runbook is deliberately narrow. Mr Lister's live-development path calls Amazon Nova 2 Lite
through the Bedrock Converse API in `us-west-2`, using the US cross-Region inference profile
`us.amazon.nova-2-lite-v1:0`. The profile can route to `us-east-1`, `us-east-2`, or `us-west-2`.
The default application and ordinary test suite still use the local fake adapter and make no AWS
calls. OpenAI GPT-5.6 Luna and Claude Sonnet 4.6 are optional, explicitly selected quality
benchmarks.

## Least-privilege development policy

The default development policy template is
[`infra/iam/bedrock-nova-2-lite-invoke-policy.json.tmpl`](../infra/iam/bedrock-nova-2-lite-invoke-policy.json.tmpl).
The optional Claude benchmark has a separate
[`infra/iam/bedrock-claude-sonnet-4-6-invoke-policy.json.tmpl`](../infra/iam/bedrock-claude-sonnet-4-6-invoke-policy.json.tmpl).
The Luna benchmark uses
[`infra/iam/bedrock-openai-gpt-5-6-luna-invoke-policy.json.tmpl`](../infra/iam/bedrock-openai-gpt-5-6-luna-invoke-policy.json.tmpl).
The Gemma 3 27B comparison uses the deployable, account-independent
[`infra/iam/bedrock-google-gemma-3-27b-it-invoke-policy.json`](../infra/iam/bedrock-google-gemma-3-27b-it-invoke-policy.json).
Each grants only `bedrock:InvokeModel` for one selected inference profile and its three destination
foundation models. The conditions prevent direct invocation outside the profile. Luna additionally
requires invocation permission on the account's `project/default` resource, as documented on its
model card; it does not require project administration permissions.

Gemma is the exception to that profile description: it grants `bedrock:InvokeModel` only on the
single `us-west-2` foundation-model ARN because the model is invoked directly in-region.

It intentionally does **not** grant:

- `bedrock:*`, model administration, streaming, batch, or provisioning actions;
- AWS Marketplace subscribe, unsubscribe, or view-subscription actions; or
- IAM, billing, or account-management actions.

The `.tmpl` suffix is intentional. `<AWS_ACCOUNT_ID>` keeps the committed file valid JSON but is
not a deployable IAM policy value. Render a local copy with the 12-digit account ID before pasting
it into IAM:

```bash
MR_LISTER_ACCOUNT_ID="$(aws sts get-caller-identity \
  --profile mr-lister-dev \
  --query Account \
  --output text)"
test "${#MR_LISTER_ACCOUNT_ID}" -eq 12
sed "s/<AWS_ACCOUNT_ID>/${MR_LISTER_ACCOUNT_ID}/g" \
  infra/iam/bedrock-nova-2-lite-invoke-policy.json.tmpl \
  > /tmp/mr-lister-bedrock-policy.json
.venv/bin/python -m json.tool /tmp/mr-lister-bedrock-policy.json >/dev/null
```

During the short account-administration session, create a customer-managed IAM policy from the
rendered JSON and attach it to the `mr-lister-developers` group. A useful policy name is
`MrListerBedrockNova2LiteInvoke`. Do not attach `AmazonBedrockFullAccess` to the developer group.

For the Luna comparison, render the Luna template instead and use a distinct name such as
`MrListerBedrockOpenAIGPT56LunaInvoke`. OpenAI's Bedrock models are not Marketplace products, so
there is no vendor subscription or Marketplace bootstrap policy.

If AWS later changes the destinations for a new inference profile, review and update this policy
explicitly. Do not replace the destination ARNs with wildcards.

## Nova access and optional Anthropic activation

Nova 2 Lite is an Amazon model and is not sold through AWS Marketplace. It therefore avoids the
third-party subscription and Anthropic first-time-use steps. Invocations are still usage-priced;
AWS account credits may cover them, but Nova is not an unlimited free service.

Do not activate Claude for routine Phase 2 development. If an optional Claude comparison is later
worth its cost, complete the following once with a delegated account administrator:

Do this once with a delegated account administrator. For this bootstrap account, where root is
currently the only account administrator, keep the root session to this checklist and sign out as
soon as it is complete.

1. Open Amazon Bedrock in `us-west-2`, then open **Model catalog** and select **Anthropic Claude
   Sonnet 4.6**.
2. Complete Anthropic's First Time Use form if it appears. It is required once per account (or at
   the AWS Organizations management account). A personal GitHub or project URL is acceptable for
   an individual developer.
3. Review the provider terms and usage pricing. The model's Marketplace product ID is
   `prod-ffvjxvh4ltq64`.
4. Complete the subscription/enablement prompt. If the console relies on automatic enablement,
   make one minimal playground invocation while still in the administrator session.
5. Allow up to 15 minutes for the initial subscription to settle. A short period of
   `AccessDeniedException` can continue after prerequisites are corrected.
6. Create and attach the least-privilege developer policy described above, then sign out of root.

AWS requires Marketplace permissions and a valid payment method for the first third-party model
subscription. Those are one-time account setup capabilities; do not give them to `mr-lister-dev`.
After activation, attach the separate Claude invocation policy only for the benchmark window.

### Free Plan guardrail

Model invocation is usage-priced. The normal path is fake adapter first, then minimal Nova canary
traffic paid from available AWS credits. Do not select an **Upgrade account** action merely to
enable Claude or clear a third-party access error. If AWS says the account plan must be upgraded to
subscribe, stop: that is an explicit owner decision, not a Phase 2 troubleshooting step.

## Local profile expectations

`aws login` requires AWS CLI v2.32.0 or newer and uses temporary browser-authenticated console
credentials; no access key should be created for this workflow.

```bash
aws --version
aws login --profile mr-lister-dev
aws configure set region us-west-2 --profile mr-lister-dev
aws configure list --profile mr-lister-dev
aws sts get-caller-identity --profile mr-lister-dev
```

Expected results:

- `aws configure list` reports `login` as the credential source and `us-west-2` as the Region;
- the caller account is the intended AWS account;
- the caller ARN identifies `mr-lister-dev` and never ends in `:root`; and
- no `aws_access_key_id` or `aws_secret_access_key` was added to a shared credentials file.

For the current terminal, make the profile selection explicit so boto3 uses the same identity:

```bash
export AWS_PROFILE=mr-lister-dev
export AWS_DEFAULT_REGION=us-west-2
```

The login cache refreshes short-term credentials during the session, for up to the IAM principal's
configured session duration (maximum 12 hours). When it expires, run `aws login` again. End it
early with:

```bash
aws logout --profile mr-lister-dev
```

## Canary sequence

First prove the default suite remains offline:

```bash
.venv/bin/python -m pytest -m "not live_bedrock"
```

Then verify the principal immediately before permitting model traffic:

```bash
aws sts get-caller-identity --profile mr-lister-dev
```

Run the explicitly gated, cost-bearing Nova canary only after identity and the Nova policy are
green:

```bash
AWS_PROFILE=mr-lister-dev \
AWS_DEFAULT_REGION=us-west-2 \
MR_LISTER_RUN_LIVE_BEDROCK=1 \
.venv/bin/python -m pytest -m live_bedrock -q
```

Select Luna without changing the default configuration:

```bash
AWS_PROFILE=mr-lister-dev \
AWS_DEFAULT_REGION=us-west-2 \
MR_LISTER_RUN_LIVE_BEDROCK=1 \
MR_LISTER_BEDROCK_CONFIG=config/bedrock/openai_gpt_5_6_luna.json \
MR_LISTER_EVAL_RUN_ID=luna-v6-canary \
  .venv/bin/python -m pytest -m live_bedrock -s tests/evaluation/test_live_bedrock.py
```

Luna supports image input and Converse on `bedrock-runtime`, but not native structured outputs.
Mr Lister therefore uses prompted JSON and applies its unchanged application-owned contracts and
bounded repair path, just as it does for Nova.

Gemma 3 27B is invoked directly in `us-west-2` as `google.gemma-3-27b-it`; it does not use a
cross-Region inference profile. Select it with
`MR_LISTER_BEDROCK_CONFIG=config/bedrock/google_gemma_3_27b_it.json`. Its Bedrock Runtime path
supports image input, Converse, and native structured outputs, while application validation
remains authoritative.

That command permits only the first calibration case. The full eleven-case suite requires the
additional `MR_LISTER_RUN_FULL_BEDROCK_EVAL=1` cost gate. The evaluation guide documents split
selection, one-to-three repeated trials, immutable private score artifacts, and like-for-like
Nova/Claude comparisons: [`tests/evaluation/README.md`](../tests/evaluation/README.md).

Nova does not use Bedrock native structured output. Mr Lister includes the compatible JSON schema
in the prompt and strictly validates the response against the full application contract. The Nova
development profile allows at most two semantic repairs per stage so a second pass can finish a
global tag-diversity cleanup; the evaluation quality floor still permits no more than two repairs
across the complete two-stage case.

The canary has two logical model stages. Each stage can make up to two additional semantic-repair
requests, while the AWS SDK can independently retry transient failures. Treat two requests as the
nominal happy path, not a hard billing ceiling.

### Expected failure classes

- `ExpiredToken`, missing credentials, or an unexpected caller ARN: local login/profile problem.
  Re-run `aws login --profile mr-lister-dev` and inspect `aws configure list`.
- `AccessDeniedException`: configuration is not ready. For Nova, check the exact caller and Nova
  IAM policy. For the optional Claude benchmark, also check Anthropic FTU, Marketplace activation,
  and propagation delay. Do not use an account upgrade as the first remedy.
- `ResourceNotFoundException`: wrong model/profile ID or source Region.
- `ValidationException`: request or structured-output schema is incompatible with Bedrock.
- `ThrottlingException`, `ModelNotReadyException`, `ModelTimeoutException`,
  `ServiceUnavailableException`, `InternalServerException`, `ModelErrorException`, or
  `ServiceQuotaExceededException`: transient/provider-capacity class; preserve the job and retry
  safely rather than changing credentials or permissions.
- A valid Bedrock response that fails Mr Lister's application schema after its bounded repair is a
  terminal generated-output failure, not permission or transport failure.

Default test runs must not invoke Bedrock. If the environment gate is absent, the live canary must
skip even when the pytest marker is selected.

## AWS references

- [Nova 2 Lite model card](https://docs.aws.amazon.com/bedrock/latest/userguide/model-card-amazon-nova-2-lite.html)
- [Claude Sonnet 4.6 model card](https://docs.aws.amazon.com/bedrock/latest/userguide/model-card-anthropic-claude-sonnet-4-6.html)
- [OpenAI GPT-5.6 Luna model card](https://docs.aws.amazon.com/bedrock/latest/userguide/model-card-openai-gpt-56-luna.html)
- [Google Gemma 3 27B model card](https://docs.aws.amazon.com/bedrock/latest/userguide/model-card-google-gemma-3-27b-pt.html)
- [Bedrock model access and Anthropic FTU](https://docs.aws.amazon.com/bedrock/latest/userguide/model-access.html)
- [Inference profile IAM prerequisites](https://docs.aws.amazon.com/bedrock/latest/userguide/inference-profiles-prereq.html)
- [AWS CLI browser login](https://docs.aws.amazon.com/cli/latest/userguide/cli-configure-sign-in.html)
