# Historical development entrypoints

These tools reproduce earlier development paths. The current seller application is
[`web/`](../../web/README.md); production AgentCore starts from
[`agentcore_phase6_runtime.py`](../../agentcore_phase6_runtime.py).

## Phase 1 fake API

Run from the repository root after installing the Python development dependencies:

```sh
uvicorn tools.legacy.local_api:app --reload
```

The API uses in-memory state and fake intelligence and production adapters. Its `/health`
response identifies `phase-1-fake`; `/docs` describes its historical `/jobs` routes. It does not
implement the current authenticated `/v1` seller contract. It makes no model or provider calls.

## Phase 3 synthetic AgentCore canary

The former root `agentcore_runtime.py` and `tools/build_agentcore_bundle.py` are now
`agentcore_canary_runtime.py` and `build_agentcore_canary_bundle.py` in this directory.
Reproduce the source bundle with:

```sh
python -m tools.legacy.build_agentcore_canary_bundle
```

The destination remains `.mr_lister_private/agentcore-bundle`. Unlike the fake API, invoking
this historical canary makes real Nova controller calls while using fake intelligence and
production. Follow the existing [Phase 3 runbook](../../docs/aws-agentcore-phase3.md) for its
configuration and invocation requirements.

The fake API and canary remain covered by regression tests. Shared workflow contracts,
validation, and test adapters still live under `src/mr_lister`; current production code reuses
some of them. Production deployment builders retain explicit source allowlists and exclude
these developer entrypoints. Historical evidence and contract fixtures remain available.
