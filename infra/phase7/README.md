# Phase 7.4 disabled publication-status query infrastructure

This directory is a separate SAM application. It does not modify the Phase 6 stack and does not
activate publication.

The template declares exactly one reserved-concurrency Lambda boundary for a future owner-scoped
publication-status query. It deliberately registers no API Gateway, Function URL, schedule,
stream, queue, or other event source. Its role can write only to the function's predeclared log
group and can perform only `dynamodb:GetItem` and `dynamodb:Query` against the same-account,
same-region `mr-lister-phase6-${EnvironmentName}` operational table. It has no DynamoDB write or
scan permission and no secret, object-store, workflow, invocation, provider, VPC, or network
management permission.

All four capability markers are fixed in the template rather than exposed as parameters:

- `MR_LISTER_PHASE7_SCAFFOLD_ONLY=true`
- `MR_LISTER_PHASE7_PUBLICATION_ENABLED=false`
- `MR_LISTER_PHASE7_REQUEST_ENABLED=false`
- `MR_LISTER_PHASE7_QUERY_ENABLED=false`

The thin scaffold shim checks that exact tuple before attempting the future entrypoint
`mr_lister.cloud.phase7_entrypoints.publication_query_api_handler`. The checked `CodeUri` contains
no application package, so a direct invocation fails closed. Even in a later sealed source bundle,
contract 7.0.1 requires the named cloud entrypoint to refuse before adapter construction or a
DynamoDB read while query enablement remains false. There is intentionally no
`SCAFFOLD_ONLY=false` path in this slice.

The scaffold also intentionally omits the release fingerprint, pinned product-profile authority,
and Cognito issuer/client/scope/group environment required by the application composition. Adding
those exact values, proving packaged-source parity with the tested runtime, and binding them into a
sealed release are later gates. This infrastructure therefore does not claim that it can compose
the application merely because the future entrypoint name and read-only IAM shape are present.

The function has reserved concurrency of one, a ten-second timeout, structured error-level logs,
and a retained log group with fourteen-day retention. Dedicated error, throttle, and duration
alarms publish only to this stack's encrypted operational topic. The topic has no subscription;
deployment owners can attach one through a separately reviewed operational change.

## Absent capabilities

This stack contains no seller publication request, coordinator, dispatcher, provider worker,
state machine, mutation route, browser control, secret reference, or external provider transport.
Creating this stack cannot make a publication-status route addressable and cannot perform a
Printify request.

## Deployment gate

`DeploymentReadiness=SCAFFOLD_ONLY`, `PublicationStatusQueryRegistered=false`, and every Phase 7
enablement output remain fixed and inspectable. A later reviewed slice must provide a narrow,
sealed source bundle, close its release verification, explicitly change the query-only gate, and
register an authenticated owner-scoped read route before the query may read application state.
Those changes must not imply request or provider-mutation authority.

## Local validation

Run from the repository root:

```shell
sam validate --lint --template-file infra/phase7/template.json
sam build --template-file infra/phase7/template.json --build-dir .aws-sam/phase7-build
.venv/bin/python -m pytest -q tests/test_phase74_read_only_infrastructure.py
```
