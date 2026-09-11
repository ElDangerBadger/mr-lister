# Approved UI and isolated evaluator release

The main application uses the approved Upload → Review → Publish layout. This change
is an implementation in the existing React application and API clients, not a second
demo frontend. Production activation and live judge access are separate release steps.

## Implemented

- The supplied Mr. Lister artwork replaces the initials in the header. The landing
  page leads to the existing hosted sign-in; invited users use their assigned account.
- Upload preserves the existing ordered batch, private transfer and recovery behavior.
  Completed transfers are described as preparation started, not finished listings.
- Review shows original artwork, all representative mockups, editable title,
  description and 13 tags, estimates, and expandable product settings.
- Milestones use section readiness and current synchronization version. No percent or
  ETA is invented. After 30 seconds of active preparation, conditional copy explains
  that an initial preparation may take longer; it does not claim cold-start detection.
- Activity starts collapsed and retains up to 20 distinct snapshots observed while
  this job is open. It is not a durable event history. Failure alerts remain visible.
- Saving, conflict recovery, image-load requirements, exact-version approval, and
  separate publication confirmation retain their existing server authority.
- The evaluator has a distinct, strictly disabled publication-status response. The
  same frontend shows the server's policy and offers no publication request.

Google/Apple sign-in and self-service store connection are not implemented. The demo
uses invited accounts with a separately provisioned Printify connection. Credentials
must not be placed in browser configuration, source control, or judge instructions.

## Repository separation

The historical fake FastAPI entrypoint and synthetic AgentCore canary/builder live in
`tools/legacy/`. They are not the current application launch path. Historical evidence
and regression tests remain available. Shared domain code was retained after tracing
dependencies.

The production web build rejects imports of test, fixture, offline publication and
legacy/developer modules, including transitive and lazy imports. The existing Python
source-bundle allowlist continues to restrict deployment contents; the evaluator status
module is included in the query bundle without including the publication package.

## Evaluator setup

The target is an independent environment: a dedicated invited owner, authentication,
state table, private artwork bucket, runtime and Printify shop/token. The Phase 7
publication stack is omitted. A second login to the production environment does not
provide this separation.

Two local tools prepare reviewable configuration without contacting AWS or Printify:

```sh
.venv/bin/python -m tools.prepare_evaluator_deployment \
  --production-identifiers /private/path/production-identifiers.json \
  --evaluator-identifiers /private/path/evaluator-identifiers.json \
  --output /private/path/evaluator-plan.json

.venv/bin/python -m tools.render_evaluator_publication_status \
  --production-identifiers /private/path/production-identifiers.json \
  --evaluator-identifiers /private/path/evaluator-identifiers.json \
  --output /private/path/evaluator-scaffold.json
```

Each identifier object contains exactly `environment_name`, `account_id`, `region`,
`application_origin`, `owner_id`, `printify_shop_id`, and `printify_secret_arn`.
The planner permits the evaluator owner/shop/secret fields to be `null` while unassigned.
The scaffold renderer requires all of them. Inputs contain identifiers, never tokens.
Production identifiers must be verified before relying on collision checks.

The tools reject production identifier reuse and unrecognized fields. Outputs are
private, create-only files. The scaffold pins the evaluator environment, origin,
secret reference and deployment target, and adds only the authenticated read-only
status route. Owner and shop binding must still be sealed into the evaluator runtime
and independently checked against the actual provider connection.

The output explicitly remains **not deployment-ready**. The existing release renderer
pins the original scaffold and does not activate this overlay. Different secret names
alone cannot establish that different credentials are stored in them.

## Remaining release work

1. Designate a separate judge Printify shop and connection; assign the invited owner.
2. Seal the evaluator source/profile artifacts and exact runtime bindings, prepare
   its authentication/origin, and complete the reviewed activation path for the overlay.
3. Verify deployed route and IAM isolation, cross-owner denial, and a real upload →
   edit → approval flow against that shop. Confirm there is no publish command route.
4. Release the built UI through the existing web release process, with each environment's
   own runtime configuration. Smoke-test the deployed UI against its actual backend.
5. Record a separately authorized real-store publication for the submission video and
   clearly state that interactive judge access creates drafts but cannot publish.

No live environment, store permissions, or listings were changed by this implementation.
The local visual-check harness is temporary test data outside the repository and is
not included in the application build.

## Local verification, September 11, 2026

- Python regression suite: 4,215 passed, 11 live-AWS tests skipped by default.
- Frontend lint, typecheck, 237 tests, production build and build-content checks passed.
- Python lint/format and all three existing contract-drift checks passed.
- The generated evaluator scaffold passed SAM lint validation using synthetic identifiers.
- Source/wheel packaging passed; retired API entrypoints and developer directories are
  absent from the wheel. Production source-bundle closure tests passed.
- Browser visual checks covered desktop and 360/390-pixel widths, editable fields,
  disclosure state, preserved local text, approval blocking and confirmation, landing/sign-in,
  and the evaluator's disabled-publication message. Test data was used; no provider was called.

The retained browser-gate scripts were updated and syntax-checked. The full three-engine
compiled-bundle gate and deployed environment smoke tests have not been run in this change.
