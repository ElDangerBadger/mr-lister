# Phase 7.6 private read-only publication guard runtime

This directory is a separate Phase 7 SAM application. It does not modify the Phase 6 stack and
does not activate a seller publication surface.

The template preserves two deliberately isolated function definitions:

- the Phase 7.4 publication-status query scaffold remains unregistered, exact-disabled, and is
  conditioned out of every valid deployment; and
- a private, direct-IAM-invoke guard verifier loads the sealed Phase 7 bundle and re-reads the
  exact approval, snapshot, shop, pricing, profile, release, and eligibility authority through
  `DurablePublicationPreCallGuard`.

The source-retained Phase 7.4 query scaffold deliberately registers no API Gateway, Function URL,
schedule, stream, queue, or other event source. Its thin shim still names only
`mr_lister.cloud.phase7_entrypoints.publication_query_api_handler` and still refuses the exact
disabled tuple before constructing a client. The template condition for its role, log group,
function, and alarms can become true only for the all-zero guard release fingerprint, while the
required guard fingerprint parameter rejects that value. There is intentionally no query
`SCAFFOLD_ONLY=false` path. That scaffold still omits the release fingerprint and the required
Cognito issuer/client/scope/group authority. Without those values and packaged-source parity, it
does not claim that it can compose the application. The deployed stack therefore instantiates
only the private guard runtime and its nine support resources; it does not enable, instantiate, or
register the query.

The guard verifier has no API Gateway, Function URL, Lambda permission, schedule, stream, queue,
state machine, dispatcher, or browser route. Its role can write only to its predeclared log group
and can perform only `dynamodb:GetItem` and `dynamodb:Query` against the same-account, same-region
`mr-lister-phase6-${EnvironmentName}` table. Leading keys are limited to `JOB#*` and
`PUBLICATION#*`. It has no DynamoDB write/transaction/scan, secret, object-store, workflow,
invocation, provider, VPC, or network-management permission.

The active guard tuple is separate from seller activation:

- `MR_LISTER_PHASE7_SCAFFOLD_ONLY=false`
- `MR_LISTER_PHASE7_GUARD_ENABLED=true`
- `MR_LISTER_PHASE7_GUARD_MODE=approval_version_read_only`
- `MR_LISTER_PHASE7_QUERY_ENABLED=false`
- `MR_LISTER_PHASE7_REQUEST_ENABLED=false`
- `MR_LISTER_PHASE7_PUBLICATION_ENABLED=false`

The guard returns only a fingerprinted, identifier-free closed attestation: sealed configuration,
current authority, or rejected authority. Even a current-authority result reports zero external
calls authorized. It cannot mint a permit, resolve a credential, stage evidence, or call Printify.

## Sealed code authority

The guard `CodeUri` is an exact S3 bucket/key/object-version triple supplied during deployment.
The key is bound to the release fingerprint as
`phase7/releases/<release-fingerprint>/guard.zip`. The bundle contains a canonical source,
Linux-ARM64 dependency, deployment, and release manifest. On cold start the entrypoint recomputes
and verifies the release fingerprint and every packaged byte before it loads configuration or
constructs a DynamoDB client.

The template fixes the checked product profile ID/version/fingerprint and absolute packaged path.
The deployment supplies only the nonzero release fingerprint and immutable S3 code coordinates;
the guard-specific startup fingerprint and application release fingerprint are the same exact
template value, and none has a default. The Lambda has reserved concurrency one, a thirty-second timeout, structured
error-level logs, and dedicated error, throttle, and duration alarms on the retained encrypted
Phase 7 operational topic.

## Deployment and verification stop line

Deployment of this stack proves only that the sealed read-only guard runtime is present and fails
closed. It does not advance contract 7.0.1 from `offline_implementation` to
`deployed_read_only_validation`. That later phase still requires Phase 6 deployed non-destructive
acceptance, immutable release/AgentCore binding, Linux ARM64 artifact inspection, and a separately
approved read-only Etsy preflight.

A status invocation proves the sealed bundle and exact-disabled activation tuple without reading
DynamoDB. An authority invocation requires an already-existing exact Phase 7 aggregate; unknown,
foreign, stale, or malformed authority returns the same identifier-free rejection. Creating a
synthetic aggregate in the shared Phase 6 table is a separate explicit test-data authorization and
must never be inferred from deployment alone.

No live Printify GET or POST, provider secret read, seller route, mutation IAM, request service,
coordinator runtime, deployment-time fixture write, or general activation is part of this slice.

## Local validation

Run from the repository root:

```shell
sam validate --lint --template-file infra/phase7/template.json
.venv/bin/python tools/export_phase7_publication_contract.py --check
.venv/bin/python -m pytest -q tests/test_phase74_read_only_infrastructure.py \\
  tests/test_phase76_publication_guard_infrastructure.py
```

## Sealed guard build

The build is provider-free. It emits a source closure first, then requires the builder to extract
the exact SHA-256-pinned wheel set into a Linux CPython 3.12 ARM64 dependency tree. The dependency
inspector requires every byte to be SHA-256/size-owned by exactly one trusted wheel `RECORD`,
rejects import hooks and standard-library shadows, and inspects native files as AArch64 ELF. Run in
a new temporary directory so an earlier artifact cannot be overwritten:

```shell
export PHASE76_WORK_ROOT="$(mktemp -d /tmp/mr-lister-phase76.XXXXXX)"
export PHASE76_SOURCE="$PHASE76_WORK_ROOT/phase7-guard-source"
export PHASE76_WHEELHOUSE="$PHASE76_WORK_ROOT/linux-arm64-wheelhouse"
export PHASE76_DEPENDENCIES="$PHASE76_WORK_ROOT/linux-arm64-dependencies"
export PHASE76_DEPLOYMENT="$PHASE76_WORK_ROOT/phase7-guard-deployment"
export PHASE76_ARTIFACT="$PHASE76_WORK_ROOT/phase7-guard-artifact"

.venv/bin/python tools/build_phase76_guard_bundle.py \
  --source-destination "$PHASE76_SOURCE"
mkdir -p "$PHASE76_WHEELHOUSE"
.venv/bin/python -m pip download \
  --requirement "$PHASE76_SOURCE/requirements.txt" \
  --dest "$PHASE76_WHEELHOUSE" \
  --platform manylinux2014_aarch64 \
  --implementation cp \
  --python-version 3.12 \
  --abi cp312 \
  --only-binary=:all: \
  --require-hashes \
  --no-deps
.venv/bin/python tools/build_phase76_guard_bundle.py \
  --build-dependencies-from-wheelhouse "$PHASE76_WHEELHOUSE" \
  --dependency-destination "$PHASE76_DEPENDENCIES" \
  --build-request "$PHASE76_SOURCE/dependency-build-request.json"
.venv/bin/python tools/build_phase76_guard_bundle.py \
  --seal-source-release "$PHASE76_SOURCE" \
  --dependencies "$PHASE76_DEPENDENCIES" \
  --deployment-destination "$PHASE76_DEPLOYMENT" \
  --artifact-destination "$PHASE76_ARTIFACT"
.venv/bin/python -m tools.verify_phase76_guard_deployment \
  --deployment "$PHASE76_DEPLOYMENT" \
  --archive "$PHASE76_ARTIFACT/phase7-guard.zip" \
  --descriptor "$PHASE76_ARTIFACT/deployment-descriptor.json"
```

Do not install from an unpinned resolver result, extract wheels with another tool, copy the host
virtual environment, add a Lambda layer, or edit the dependency/deployment tree after sealing. The
builder admits only the exact named wheel hashes and requires the extracted 2,310-file Merkle-style
inventory fingerprint to match the checked release authority; any change invalidates the release.

## Versioned upload and reviewed deployment

These commands are live. They require explicit operator approval, a logged-in `mr-lister-dev`
profile, and an existing same-region bucket with versioning enabled. Replace the bucket placeholder
before running. `sam deploy --confirm-changeset` pauses for review before it executes the change
set.

```shell
export PHASE76_AWS_PROFILE="mr-lister-dev"
export PHASE76_REGION="us-west-2"
export PHASE76_ENVIRONMENT="dev"
export PHASE76_STACK="mr-lister-phase7-dev"
export PHASE76_FUNCTION="mr-lister-phase7-dev-guard-verification"
export PHASE76_ROLE="mr-lister-phase7-dev-guard-verification-role"
export PHASE76_LEGACY_QUERY_FUNCTION="mr-lister-phase7-dev-publication-status-query"
export PHASE76_LEGACY_QUERY_ROLE="mr-lister-phase7-dev-publication-status-query-role"
export PHASE76_LEGACY_QUERY_LOG_GROUP="/aws/lambda/mr-lister-phase7-dev-publication-status-query"
export PHASE76_BUCKET="REPLACE_WITH_VERSIONED_ARTIFACT_BUCKET"
export PHASE76_CAPTURE="$PHASE76_WORK_ROOT/deployment-captures"
mkdir -p "$PHASE76_CAPTURE"

aws login --profile "$PHASE76_AWS_PROFILE"
aws sts get-caller-identity \
  --profile "$PHASE76_AWS_PROFILE" \
  --region "$PHASE76_REGION" > "$PHASE76_CAPTURE/caller-identity.json"
export PHASE76_ACCOUNT_ID="$(jq -er .Account "$PHASE76_CAPTURE/caller-identity.json")"
aws s3api get-bucket-versioning \
  --bucket "$PHASE76_BUCKET" \
  --profile "$PHASE76_AWS_PROFILE" \
  --region "$PHASE76_REGION" | jq -e '.Status == "Enabled"'

export PHASE76_RELEASE_FINGERPRINT="$(
  jq -er .release_fingerprint "$PHASE76_ARTIFACT/deployment-descriptor.json"
)"
export PHASE76_ARCHIVE_SHA256="$(
  jq -er .archive.sha256 "$PHASE76_ARTIFACT/deployment-descriptor.json"
)"
export PHASE76_ARCHIVE_SHA256_BASE64="$(
  openssl dgst -sha256 -binary "$PHASE76_ARTIFACT/phase7-guard.zip" | openssl base64 -A
)"
export PHASE76_KEY="phase7/releases/$PHASE76_RELEASE_FINGERPRINT/guard.zip"

aws s3api put-object \
  --bucket "$PHASE76_BUCKET" \
  --key "$PHASE76_KEY" \
  --body "$PHASE76_ARTIFACT/phase7-guard.zip" \
  --checksum-algorithm SHA256 \
  --checksum-sha256 "$PHASE76_ARCHIVE_SHA256_BASE64" \
  --content-type application/zip \
  --server-side-encryption AES256 \
  --metadata \
    "mr-lister-archive-sha256=$PHASE76_ARCHIVE_SHA256,mr-lister-release-fingerprint=$PHASE76_RELEASE_FINGERPRINT" \
  --profile "$PHASE76_AWS_PROFILE" \
  --region "$PHASE76_REGION" > "$PHASE76_CAPTURE/put-object.json"
export PHASE76_VERSION_ID="$(
  jq -er '.VersionId | select(. != "null" and length > 0)' \
    "$PHASE76_CAPTURE/put-object.json"
)"

sam deploy \
  --template-file infra/phase7/template.json \
  --stack-name "$PHASE76_STACK" \
  --s3-bucket "$PHASE76_BUCKET" \
  --s3-prefix phase7/sam \
  --capabilities CAPABILITY_NAMED_IAM \
  --parameter-overrides \
    "EnvironmentName=$PHASE76_ENVIRONMENT" \
    "GuardCodeS3Bucket=$PHASE76_BUCKET" \
    "GuardCodeS3Key=$PHASE76_KEY" \
    "GuardCodeS3ObjectVersion=$PHASE76_VERSION_ID" \
    "GuardReleaseFingerprint=$PHASE76_RELEASE_FINGERPRINT" \
  --confirm-changeset \
  --profile "$PHASE76_AWS_PROFILE" \
  --region "$PHASE76_REGION"
```

## Read-only deployed capture and offline verification

Capture the immutable object, exact instantiated stack inventory, Lambda
code/configuration/concurrency, exact IAM policy surface, and absence of every legacy query or
event-source surface. None of these commands writes cloud state:

```shell
aws s3api head-object \
  --bucket "$PHASE76_BUCKET" \
  --key "$PHASE76_KEY" \
  --version-id "$PHASE76_VERSION_ID" \
  --checksum-mode ENABLED \
  --profile "$PHASE76_AWS_PROFILE" \
  --region "$PHASE76_REGION" > "$PHASE76_CAPTURE/head-object.json"
aws cloudformation describe-stacks \
  --stack-name "$PHASE76_STACK" \
  --profile "$PHASE76_AWS_PROFILE" \
  --region "$PHASE76_REGION" > "$PHASE76_CAPTURE/stack.json"
aws cloudformation list-stack-resources \
  --stack-name "$PHASE76_STACK" \
  --profile "$PHASE76_AWS_PROFILE" \
  --region "$PHASE76_REGION" > "$PHASE76_CAPTURE/stack-resources.json"
aws lambda get-function-configuration \
  --function-name "$PHASE76_FUNCTION" \
  --profile "$PHASE76_AWS_PROFILE" \
  --region "$PHASE76_REGION" > "$PHASE76_CAPTURE/lambda-configuration.json"
aws lambda get-function-concurrency \
  --function-name "$PHASE76_FUNCTION" \
  --profile "$PHASE76_AWS_PROFILE" \
  --region "$PHASE76_REGION" > "$PHASE76_CAPTURE/lambda-concurrency.json"
aws iam get-role \
  --role-name "$PHASE76_ROLE" \
  --profile "$PHASE76_AWS_PROFILE" \
  --region "$PHASE76_REGION" > "$PHASE76_CAPTURE/iam-role.json"
aws iam get-role-policy \
  --role-name "$PHASE76_ROLE" \
  --policy-name ReadOnlyApprovalPublicationGuard \
  --profile "$PHASE76_AWS_PROFILE" \
  --region "$PHASE76_REGION" > "$PHASE76_CAPTURE/iam-inline-policy.json"
aws iam list-role-policies \
  --role-name "$PHASE76_ROLE" \
  --profile "$PHASE76_AWS_PROFILE" \
  --region "$PHASE76_REGION" > "$PHASE76_CAPTURE/iam-inline-policy-list.json"
aws iam list-attached-role-policies \
  --role-name "$PHASE76_ROLE" \
  --profile "$PHASE76_AWS_PROFILE" \
  --region "$PHASE76_REGION" > "$PHASE76_CAPTURE/iam-attached-policy-list.json"
aws lambda list-event-source-mappings \
  --function-name "$PHASE76_FUNCTION" \
  --profile "$PHASE76_AWS_PROFILE" \
  --region "$PHASE76_REGION" > "$PHASE76_CAPTURE/event-source-mappings.json"
aws lambda list-versions-by-function \
  --function-name "$PHASE76_FUNCTION" \
  --profile "$PHASE76_AWS_PROFILE" \
  --region "$PHASE76_REGION" > "$PHASE76_CAPTURE/lambda-versions.json"
aws lambda list-aliases \
  --function-name "$PHASE76_FUNCTION" \
  --profile "$PHASE76_AWS_PROFILE" \
  --region "$PHASE76_REGION" > "$PHASE76_CAPTURE/lambda-aliases.json"
aws lambda list-function-url-configs \
  --function-name "$PHASE76_FUNCTION" \
  --profile "$PHASE76_AWS_PROFILE" \
  --region "$PHASE76_REGION" > "$PHASE76_CAPTURE/lambda-url-configs.json"
aws cloudwatch describe-alarms \
  --alarm-names \
    "mr-lister-phase7-${PHASE76_ENVIRONMENT}-publication-status-errors" \
    "mr-lister-phase7-${PHASE76_ENVIRONMENT}-publication-status-throttles" \
    "mr-lister-phase7-${PHASE76_ENVIRONMENT}-publication-status-duration" \
  --profile "$PHASE76_AWS_PROFILE" \
  --region "$PHASE76_REGION" > "$PHASE76_CAPTURE/legacy-query-alarms.json"
aws logs describe-log-groups \
  --log-group-name-prefix "$PHASE76_LEGACY_QUERY_LOG_GROUP" \
  --profile "$PHASE76_AWS_PROFILE" \
  --region "$PHASE76_REGION" > "$PHASE76_CAPTURE/legacy-query-log-groups.json"
```

The checked repository records no prior Phase 7 deployment, so this gate requires the legacy log
group result to be empty even though the old resource declaration used `DeletionPolicy: Retain`.
An unexpected retained group blocks verification for separate operator investigation and explicit
authorization; neither the capture commands nor the verifier deletes it.

`get-function-event-invoke-config`, `get-function-url-config`, and `get-policy` prove absence by
returning a checked 404 rather than a success document. This read-only capture normalizes only
those three expected AWS exceptions; it fails if any surface exists or a different error occurs:

```shell
PHASE76_AWS_PROFILE="$PHASE76_AWS_PROFILE" \
PHASE76_REGION="$PHASE76_REGION" \
PHASE76_FUNCTION="$PHASE76_FUNCTION" \
.venv/bin/python - <<'PY' > "$PHASE76_CAPTURE/lambda-absence.json"
import json
import os

import boto3

client = boto3.Session(profile_name=os.environ["PHASE76_AWS_PROFILE"]).client(
    "lambda", region_name=os.environ["PHASE76_REGION"]
)
function_name = os.environ["PHASE76_FUNCTION"]


def require_absent(method_name: str) -> dict[str, object]:
    try:
        getattr(client, method_name)(FunctionName=function_name)
    except client.exceptions.ResourceNotFoundException as error:
        response = error.response
        if (
            response.get("Error", {}).get("Code") != "ResourceNotFoundException"
            or response.get("ResponseMetadata", {}).get("HTTPStatusCode") != 404
        ):
            raise
        return {"error_code": "ResourceNotFoundException", "http_status_code": 404}
    raise RuntimeError(f"unexpected Lambda surface exists: {method_name}")


print(
    json.dumps(
        {
            "function_name": function_name,
            "get_function_event_invoke_config": require_absent(
                "get_function_event_invoke_config"
            ),
            "get_function_url_config": require_absent("get_function_url_config"),
            "get_policy": require_absent("get_policy"),
        },
        sort_keys=True,
    )
)
PY
```

The conditioned-out Phase 7.4 query Lambda and its role must also be physically absent, including
after an update from an earlier template. This second read-only capture normalizes only their exact
not-found responses:

```shell
PHASE76_AWS_PROFILE="$PHASE76_AWS_PROFILE" \
PHASE76_REGION="$PHASE76_REGION" \
PHASE76_LEGACY_QUERY_FUNCTION="$PHASE76_LEGACY_QUERY_FUNCTION" \
PHASE76_LEGACY_QUERY_ROLE="$PHASE76_LEGACY_QUERY_ROLE" \
.venv/bin/python - <<'PY' > "$PHASE76_CAPTURE/legacy-query-absence.json"
import json
import os

import boto3

session = boto3.Session(profile_name=os.environ["PHASE76_AWS_PROFILE"])
region = os.environ["PHASE76_REGION"]
lambda_client = session.client("lambda", region_name=region)
iam_client = session.client("iam", region_name=region)
function_name = os.environ["PHASE76_LEGACY_QUERY_FUNCTION"]
role_name = os.environ["PHASE76_LEGACY_QUERY_ROLE"]


def require_absent(client, method_name, expected_code, **arguments):
    try:
        getattr(client, method_name)(**arguments)
    except Exception as error:
        response = getattr(error, "response", {})
        if (
            response.get("Error", {}).get("Code") != expected_code
            or response.get("ResponseMetadata", {}).get("HTTPStatusCode") != 404
        ):
            raise
        return {"error_code": expected_code, "http_status_code": 404}
    raise RuntimeError(f"unexpected legacy query surface exists: {method_name}")


print(
    json.dumps(
        {
            "function_name": function_name,
            "get_function": require_absent(
                lambda_client,
                "get_function",
                "ResourceNotFoundException",
                FunctionName=function_name,
            ),
            "get_role": require_absent(
                iam_client,
                "get_role",
                "NoSuchEntity",
                RoleName=role_name,
            ),
            "role_name": role_name,
        },
        sort_keys=True,
    )
)
PY
```

Finally, directly invoke only the identifier-free startup status and one known-missing authority.
The invoke metadata must contain no `FunctionError`; the response must keep publication disabled
and `provider_calls_authorized=0`:

```shell
printf '%s' '{"operation":"status"}' > "$PHASE76_CAPTURE/status-request.json"
printf '%s' \
  '{"aggregate_id":"phase76_missing_authority_smoke","operation":"verify_authority","owner_id":"dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd"}' \
  > "$PHASE76_CAPTURE/rejected-request.json"
aws lambda invoke \
  --function-name "$PHASE76_FUNCTION" \
  --invocation-type RequestResponse \
  --log-type None \
  --cli-binary-format raw-in-base64-out \
  --payload "fileb://$PHASE76_CAPTURE/status-request.json" \
  --profile "$PHASE76_AWS_PROFILE" \
  --region "$PHASE76_REGION" \
  "$PHASE76_CAPTURE/status-payload.json" > "$PHASE76_CAPTURE/status-invocation.json"
aws lambda invoke \
  --function-name "$PHASE76_FUNCTION" \
  --invocation-type RequestResponse \
  --log-type None \
  --cli-binary-format raw-in-base64-out \
  --payload "fileb://$PHASE76_CAPTURE/rejected-request.json" \
  --profile "$PHASE76_AWS_PROFILE" \
  --region "$PHASE76_REGION" \
  "$PHASE76_CAPTURE/rejected-payload.json" > "$PHASE76_CAPTURE/rejected-invocation.json"

.venv/bin/python -m tools.verify_phase76_guard_deployment \
  --deployment "$PHASE76_DEPLOYMENT" \
  --archive "$PHASE76_ARTIFACT/phase7-guard.zip" \
  --descriptor "$PHASE76_ARTIFACT/deployment-descriptor.json" \
  --head-object-json "$PHASE76_CAPTURE/head-object.json" \
  --bucket "$PHASE76_BUCKET" \
  --key "$PHASE76_KEY" \
  --version-id "$PHASE76_VERSION_ID" \
  --stack-json "$PHASE76_CAPTURE/stack.json" \
  --stack-resources-json "$PHASE76_CAPTURE/stack-resources.json" \
  --lambda-configuration-json "$PHASE76_CAPTURE/lambda-configuration.json" \
  --lambda-concurrency-json "$PHASE76_CAPTURE/lambda-concurrency.json" \
  --iam-role-json "$PHASE76_CAPTURE/iam-role.json" \
  --iam-inline-policy-json "$PHASE76_CAPTURE/iam-inline-policy.json" \
  --iam-inline-policy-list-json "$PHASE76_CAPTURE/iam-inline-policy-list.json" \
  --iam-attached-policy-list-json "$PHASE76_CAPTURE/iam-attached-policy-list.json" \
  --event-source-mappings-json "$PHASE76_CAPTURE/event-source-mappings.json" \
  --lambda-versions-json "$PHASE76_CAPTURE/lambda-versions.json" \
  --lambda-aliases-json "$PHASE76_CAPTURE/lambda-aliases.json" \
  --lambda-url-configs-json "$PHASE76_CAPTURE/lambda-url-configs.json" \
  --lambda-absence-json "$PHASE76_CAPTURE/lambda-absence.json" \
  --legacy-query-absence-json "$PHASE76_CAPTURE/legacy-query-absence.json" \
  --legacy-query-alarms-json "$PHASE76_CAPTURE/legacy-query-alarms.json" \
  --legacy-query-log-groups-json "$PHASE76_CAPTURE/legacy-query-log-groups.json" \
  --status-request-json "$PHASE76_CAPTURE/status-request.json" \
  --status-invocation-json "$PHASE76_CAPTURE/status-invocation.json" \
  --status-payload "$PHASE76_CAPTURE/status-payload.json" \
  --rejected-request-json "$PHASE76_CAPTURE/rejected-request.json" \
  --rejected-invocation-json "$PHASE76_CAPTURE/rejected-invocation.json" \
  --rejected-payload "$PHASE76_CAPTURE/rejected-payload.json" \
  --stack-name "$PHASE76_STACK" \
  --environment-name "$PHASE76_ENVIRONMENT" \
  --region "$PHASE76_REGION" \
  --account-id "$PHASE76_ACCOUNT_ID"
```

The verifier fails closed on a `null` or mismatched S3 version, key/release mismatch, checksum or
metadata drift, incomplete or expanded stack resources, any surviving legacy query function, role,
log group, or alarm, code SHA/runtime/architecture/role/environment drift, expanded IAM authority,
trigger/URL/resource-policy presence, `FunctionError`, or an altered/identifier-bearing attestation.
The rejected-authority smoke is not a positive guard-row proof; a positive case still requires
separately authorized, pre-existing exact table authority.
