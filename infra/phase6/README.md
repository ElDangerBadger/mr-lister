# Phase 6.2 AWS infrastructure scaffold

This directory is a new SAM application. It does not modify or replace the retained Phase 4 or
Phase 5 evidence stacks.

The template defines the Phase 6 operational table and due-work index, a private versioned artwork
bucket, the four bounded Standard Step Functions workflows, and four separately permissioned
Lambda boundaries. `AgentCoreRuntimeArn` and `PrintifySecretArn` are required, nonblank, exact-resource
parameters.

## Deployment gate

This stack is **not ready for cloud deployment**. The Lambda files are intentionally fail-closed
scaffold handlers. They make `sam build` verify the package topology without pretending that the
application adapters exist. The stack output `DeploymentReadiness=SCAFFOLD_ONLY` and the
`MR_LISTER_PHASE6_SCAFFOLD_ONLY=true` environment marker make this condition inspectable.

Before deployment, replace the shims with tested adapters that construct the Phase 6 DynamoDB
store, invoke the checkpointed Strands/AgentCore preparation bridge, execute the draft-only
Printify synchronizer, and settle outcomes only through application-owned worker commands. Remove
the scaffold marker and change the readiness output only after those adapters pass offline tests
and an approved canary.

## Local validation

Run from the repository root:

```shell
sam validate --lint --template-file infra/phase6/template.json
env PATH="$PWD/.venv/bin:$PATH" sam build --template-file infra/phase6/template.json --build-dir .aws-sam/phase6-build
python -m pytest -q tests/test_phase6_infrastructure.py
```
