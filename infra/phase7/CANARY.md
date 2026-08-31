# Phase 7 isolated direct-invoke canary

`canary-template.json` declares the smallest AWS boundary needed to run one separately reviewed
Phase 7 canary. It is source-only at this checkpoint: no artifact has been uploaded, no change set
has been created or executed, no caller has been granted invoke authority, and no Lambda has been
invoked.

The canary is deliberately separate from both the Phase 6 application stack and the deployed
Phase 7.6 read-only guard stack. Its complete resource inventory is one retained 14-day log group,
one dedicated inline-policy execution role, and one Python 3.12 ARM64 Lambda. The function has
reserved concurrency one and accepts only synchronous, identity-policy-authorized direct
invocations. There is no API, Function URL, Lambda resource policy, event source, schedule,
stream, queue, state machine, dead-letter queue, asynchronous invoke configuration, layer, VPC,
alias, or published version.

## Exact authority

The code location is an immutable S3 bucket/key/object-version triple. The key shape is
`phase7/releases/<canary-release-fingerprint>/canary.zip`. A future sealed release verifier must
cross-check three distinct fingerprints before constructing any AWS client:

- `MR_LISTER_PHASE7_CANARY_RELEASE_FINGERPRINT` identifies the exact runtime archive;
- `MR_LISTER_PHASE7_CANARY_BINDING_FINGERPRINT` identifies the separately reviewed, packaged
  owner/aggregate canary binding; and
- `MR_LISTER_RELEASE_FINGERPRINT` is the application release already frozen in the publication
  snapshot.

The exact `MR_LISTER_PHASE7_CANARY_MODE` must match the packaged binding. The runtime leaves the
global scaffold (`MR_LISTER_PHASE7_SCAFFOLD_ONLY=false`) only through the isolated seam explicitly
selected by `MR_LISTER_PHASE7_CANARY_ENABLED=true`. The seller-facing query, request, and general
publication flags remain false. The pinned Phase 6 product profile also remains draft-safe
(`publish_enabled=false`); the isolated binding and one-shot durable permit provide the separate
canary authority.

The packaged binding must contain only its sanitized digests and authority fingerprints. Raw
owner and aggregate IDs are supplied only in the private direct-invoke payload and are checked
against that binding before durable work begins. The deployed handler must never mint a binding
from invocation data or current state.

`tools/prepare_phase712_canary_request.py` is the held operator seam for creating that binding.
Its read-only `inspect` pass validates one exact approved Phase 6 authority and writes a sanitized
SHA-bound plan alongside a mode-0600 private command. The plan identifies the target by a safe
operator label and binds the complete repository source closure used by request creation. Its
separately gated `execute` pass is the only request-creation step: it delegates one atomic
transaction to the existing request service, strongly re-reads the result through the durable
guard, and writes `canary-binding.json` plus an exact `{owner_id, aggregate_id}`
`invocation.local.json`. A delayed readback still emits those recovery artifacts while explicitly
reporting whether enough of the 30-minute verification window remains for deployment. Neither pass
constructs provider, Secrets Manager, Lambda, S3, or publication-POST capability.

## Execution-role boundary

The Lambda execution role may:

- create a stream and write events only in its predeclared log group;
- strongly read and transactionally update only the existing
  `mr-lister-phase6-${EnvironmentName}` table, with every key limited to `JOB#*` or
  `PUBLICATION#*`; and
- read only the one exact owner-bound Printify secret ARN supplied to the stack.

It has no standalone DynamoDB put/update/delete/scan authority, S3 runtime authority, KMS,
Step Functions, Lambda invoke, API Gateway, Bedrock, order, fulfillment, or infrastructure
management permission. HTTPS egress remains narrowed in application code to the sealed Printify
publication boundary.

The operator's ability to call the function is not part of this execution role. A later,
separately reviewed identity policy may grant `lambda:InvokeFunction` only on the exact canary
function ARN. The template intentionally creates no `AWS::Lambda::Permission` resource.

## Held deployment sequence

All live steps remain held for separate approval:

1. Pre-stage and verify the checked Linux ARM64 dependency tree, exact IAM seams, active sessions,
   versioned artifact bucket, three-resource template, and absence of any deployed publication
   worker, trigger, or consumer before starting the request's immutable 30-minute verification
   window.
2. Run the read-only operator inspection and review its exact plan SHA. Only after approval, run
   the gated request creation and strong readback to produce the sanitized binding and private
   invocation envelope.
3. Seal and verify a Linux ARM64 runtime ZIP containing the reviewed entrypoint, exact composed
   store/coordinator graph, packaged canary binding, credential adapter, provider boundary, and
   pinned dependencies.
4. Upload those exact bytes to a versioned same-region S3 key and read back the version, checksum,
   size, encryption, and release metadata.
5. Create and inspect a CloudFormation change set for a separate canary stack. Confirm its exact
   three-resource inventory, code coordinates, environment, concurrency, and IAM policy before
   execution.
6. Grant one operator the exact identity-based invoke permission only after the binding and mode
   receive explicit approval.
7. Invoke synchronously one coordinator step at a time. The durable permit and provider-call
   ledger remain authoritative; deployment never implies permission to publish.
8. Verify the terminal graph and sanitized evidence, then remove the temporary caller permission
   or canary stack under a separate cleanup approval.

Nothing in this file authorizes any of those live actions.

## Local validation

From the repository root:

```shell
python -m json.tool infra/phase7/canary-template.json >/dev/null
.venv/bin/python -m pytest -q \
  tests/test_phase711_publication_canary_infrastructure.py \
  tests/test_phase712_canary_operator_preparation.py
```
