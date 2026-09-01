# Phase 6.6 acceptance hardening contract

Phase 6.6 freezes how acceptance is partitioned and what may be retained as evidence. The offline
exact-bundle browser matrix and the evidence-set verifier have passed locally. Sanitized private
records also show individual passes for the four offline gates and three deployed non-destructive
gates against earlier source/deployment bindings. Those records are historical and cannot be
combined to close a changed stabilization release: the verifier requires exact source, manifest,
and deployment bindings plus its gate-specific run, actor, and job joins. No provider-destructive
or moderated first-time-seller gate is claimed as passed here.

Current-release scope note (2026-09-01): an authenticated walkthrough has now completed the
same-job AgentCore/Strands, unpublished Printify review, economics recovery, and explicit approval
path on the exact deployed draft-only release. The owner accepted that result as the functional
hackathon-demo seal. It does not rewrite this manifest or claim its exact sample, count, artifact,
two-actor, concurrency, cancellation, accessibility, or moderated-session requirements. In this
document, “Phase 6 exit” continues to mean full formal Phase 6.6 evidence closure; the authoritative
release record distinguishes that hardening target from demo acceptance.

The source manifest is
[`contracts/acceptance/phase6.6.manifest.json`](../contracts/acceptance/phase6.6.manifest.json).
The exact moderated first-time-seller task is frozen separately in
[`contracts/acceptance/phase6.6.first-time-seller-task.json`](../contracts/acceptance/phase6.6.first-time-seller-task.json).
The checked-in closed structural evidence schema is
[`contracts/acceptance/phase6.6.evidence.schema.json`](../contracts/acceptance/phase6.6.evidence.schema.json).
The strict runtime models, authoritative semantic validator, and schema function live in
[`src/mr_lister/acceptance/phase6.py`](../src/mr_lister/acceptance/phase6.py), with reproducible
export and drift checking in
[`tools/export_phase66_acceptance_contract.py`](../tools/export_phase66_acceptance_contract.py).
JSON Schema validates transport shape, types, closed fields, and local constraints; it is not a
standalone acceptance decision. Every record must pass `validate_phase66_evidence`, which enforces
the frozen gate's exact assertion order, required artifact kinds, cross-field authority joins, and
provider-specific invariants. A candidate record set must also pass the full-set verifier in
[`src/mr_lister/acceptance/evidence_set.py`](../src/mr_lister/acceptance/evidence_set.py). It
semantically validates the gate-11 moderated-session artifact, its exact frozen task digest, and its
same-source/deployment/run/job/provider/session joins. Evidence binds the
canonical manifest through its SHA-256 digest, so records from another gate revision cannot be
silently counted toward Phase 6 exit.

## Evidence classes

| Class | Permitted environment effect | Required authority |
| --- | --- | --- |
| `offline` | No deployed or provider call | Exact source-commit and manifest digests |
| `deployed_non_destructive` | Named AWS acceptance resources; zero provider calls | Deployment digest and one or two actor digests |
| `provider_destructive` | Only the bounded unpublished Printify draft mutations named by the gate | Separate run-gate and provider-write-gate digests, root rejection, exact count limits |
| `moderated_user` | Observed user-session evidence | Participant, consent, exact frozen task, session, deployment, actor, and gate-specific job authority |

Provider mutation is forbidden in offline and deployed-non-destructive records. A moderated user
record is also never provider-write authority. If a moderated session uses the real provider, its
provider effects require a separate double-gated provider-destructive record bound to the same
sanitized run and job digests. The blocking gate-11 record must bind the same sanitized source,
deployment, run, job, actor, work, and Strands-correlation authority as its primary provider record.
Only the nonblocking five-session target may omit job authority when a session is not used to close
the first-time-seller exit gate.

The first successful, intervention-free first-time-seller session is a Phase 6 exit gate. The
five-session target remains separate and nonblocking for technical Phase 6 closure; it is the
evidence target for the open seller-trust product hypothesis.

## Private live-evidence workspace

[`tools/phase66_live_acceptance.py`](../tools/phase66_live_acceptance.py) supplies the narrow local
plumbing for an authorized live run. It creates the exact valid 5 MiB mixed-alpha PNG required by
the frozen primary single-artwork canary and invokes the authoritative verifier over one completed
evidence bundle beneath a Git-ignored private root. That canary fixture is not the product input
contract and does not certify the full PNG/SVG, transparent/opaque, single/multiple matrix frozen in
[`phase6.0.0.json`](../contracts/artwork/phase6.0.0.json). The tool does not stage arbitrary files,
sanitize raw observations, import an AWS or provider client, or grant live authority; identity
setup, technical probes, provider writes, moderated observations, and closed evidence production
remain separately controlled activities.

```shell
.venv/bin/python -m tools.phase66_live_acceptance make-canary-png \
  .mr_lister_private/phase66-acceptance/phase66-live-YYYYMMDDTHHMMSSZ
.venv/bin/python -m tools.phase66_live_acceptance verify \
  .mr_lister_private/phase66-acceptance/phase66-live-YYYYMMDDTHHMMSSZ
```

The verifier requires `records.json`, `artifact-files.json`, and every indexed artifact beneath the
selected run root. The tool rejects paths outside `.mr_lister_private/phase66-acceptance/` and
secures generated directories and files to owner-only permissions. Published source and
documentation never contain machine-local paths, credentials, raw identifiers, or evidence
payloads.

The hermetic producer and assembly boundaries are
[`capture_phase66_deployment_authority.py`](../tools/capture_phase66_deployment_authority.py),
[`capture_phase66_agentcore_deployment_authority.py`](../tools/capture_phase66_agentcore_deployment_authority.py),
[`capture_phase66_upload_integrity_preflight.py`](../tools/capture_phase66_upload_integrity_preflight.py),
[`prepare_phase66_edge_revalidation.py`](../tools/prepare_phase66_edge_revalidation.py),
[`prepare_phase66_upload_integrity_evidence.py`](../tools/prepare_phase66_upload_integrity_evidence.py),
[`prepare_phase66_outbox_recovery_evidence.py`](../tools/prepare_phase66_outbox_recovery_evidence.py),
[`phase66_provider_acceptance.py`](../tools/phase66_provider_acceptance.py),
[`prepare_phase66_moderated_evidence.py`](../tools/prepare_phase66_moderated_evidence.py), and
[`assemble_phase66_evidence_bundle.py`](../tools/assemble_phase66_evidence_bundle.py). Their presence
and local tests do not close a gate. Only executed, authority-bound evidence in one final verified
bundle can do so.

## Sanitized evidence boundary

Evidence records have no free-text observation field and no raw identifier or payload escape
hatch. They may retain only:

- closed gate, evidence-class, outcome, artifact, and final-state enums;
- UTC timestamps and bounded integer counts;
- ordered results for every assertion frozen by the selected gate;
- SHA-256 digests for the manifest, run, source commit, deployment, actors, job, work, Strands
  correlation, participant, consent, task script, session record, and artifacts;
- zero-match privacy attestations; and
- bounded aggregate provider call counts, never methods, paths, headers, bodies, IDs, or responses.

Strict recursive validation rejects fields for raw owner identity, Cognito subject, email,
username, access/refresh/ID tokens, authorization, cookies, secrets, presigned or upload URLs,
storage coordinates, request/response bodies, and provider payloads or responses. Every object also
rejects unknown fields.

A passed record must also carry every sanitized artifact kind frozen for that gate. Offline gates
require a test report; the browser gate additionally requires a browser trace. Deployed gates
require bounded canary, deployment, and log-audit artifacts as applicable. Every provider gate
requires a provider-call ledger plus its canary summary, and the primary same-job canary also
requires a log audit. Moderated gates require a separate moderated-session record. Artifact
digests must be unique within the evidence record; assertion booleans alone cannot close a gate.

The blocking moderated artifact must carry the exact frozen task digest and semantically prove an
invited first-time seller with explicit consent and MFA, no external documentation or moderator
help, zero operator intervention, browser restart and same-job recovery, same-job Strands evidence,
an unpublished Printify draft, the required accessibility and manual journeys, human approval, and
an absent/disabled publication surface with zero publication, order, or fulfillment attempts. It
must join the exact primary provider record rather than serving as a generic passed-session artifact.

Provider-destructive evidence additionally requires both gate digests and records maximum
authorized versus observed product POST and PUT counts. The two gate digests must be distinct. The
primary same-job canary is valid only with one artwork upload, one product POST, two same-product
PUTs, at least one final product GET, exact work and Strands-correlation digests, explicit evidence
that Gemma performed the intelligence work under the Strands controller, and a final
`unpublished_unlocked` aggregate result. The one-upload assertion defines that frozen canary sample;
it is not a single-upload product restriction. Publication, order, and fulfillment attempt counts
are fixed at zero in both boundaries. The runtime validator rejects boolean/integer coercion and
owns the manifest-selected semantic rules that standard JSON Schema cannot represent.

The draft-only provider transport writes a closed audit record before every allowed request and a
single identifier-free `rejected` category for every denied route attempt. Dynamic shop, product,
image, owner, token, header, body, and query values have no representation in that ledger.

## Historical offline implementation checkpoint

The Phase 6.6 offline slice includes the frozen evidence contract and drift gate, an artifact-backed
evidence-set closure oracle, forced three-way revise/approve/cancel concurrency, exact idempotent
replay and changed-body conflict tests, all protected-route foreign/unknown equivalence checks,
fresh owner-bound Printify secret resolution, exact-version source verification, and role-isolated
API, dispatcher, preparation, provider, settlement, and retention composition roots. The
preparation root is a dedicated Phase 6 AgentCore entrypoint: Strands remains the controller and
the pinned Gemma configuration remains the image-review and listing-intelligence worker.

Reproducible narrow source manifests are generated separately for the ordinary Lambda and Phase 6
AgentCore runtime surfaces. Their import tests prove that the Lambda source bundle excludes
Strands, AgentCore, and legacy publication surfaces, while the AgentCore source bundle excludes
provider and cloud capabilities. A cross-component release manifest now binds every sealed source
and dependency byte, the Lambda verifies that authority before every production delegate, and the
preparation boundary binds an exact non-default AgentCore endpoint, immutable runtime version, and
observed `READY` deployment fingerprint. The build tooling rejects missing target-native awscrt,
Pillow, or pydantic-core extensions and non-AArch64 ELF bytes. At this original offline checkpoint,
the checked repository contained build requests rather than the controlled Linux ARM64 dependency
artifacts, and its SAM `CodeUri` still selected the fail-closed scaffold. Later controlled builds,
target-runtime import smoke, and the reviewed active draft-only deployment superseded that
checkpoint; the source template remains a fail-closed validation/build input rather than a
description of live mode.

The deterministic production bundle with digest
`c6115a4d8f3d4fec88ce9b640d760dff1599db43fe3cba10b3962a8eda16aad2` passed the same
credential-free Chromium, Firefox, and WebKit flow set. Each engine produced a nonempty trace ZIP;
the attested summary contains no URL, filesystem path, host, token, credential, cookie, owner, or
job authority. The flows prove managed-session route recovery, prominent Strands evidence, the
unpublished boundary, exact listing validation/tag count, one-shot approval with stale-readback
focus, tab recovery, route-response isolation, offline/hidden polling suppression and resumption,
forced colors, reduced motion, and 360-CSS-pixel reflow, with zero provider transport attempts.

The reference-aware source-retention core and its exact-prefix AWS adapters are implemented without
object-byte or deletion authority. It traverses lifecycle delete-marker pages, keeps its durable
cursor history below the DynamoDB item ceiling, binds application time to the inventory adapter's
trusted S3 observation time, and never releases a recent pre-commit pin within the two-day safety
window. The SAM template includes a bounded schedule, singleton concurrency, durable checkpoint,
strong DynamoDB authority reads, and least-capability version-tag IAM. A separate daily cleanup
boundary now conditionally assigns the table's 90-day TTL to exact `CANCELLED` and
`FAILED_TERMINAL` job partitions and matching owner receipts; completed upload intents and all
upload receipts also have bounded 90-day TTLs, while open/cancelled reservations retain their
one-day cleanup. Neither cleanup boundary has object-byte, deletion, provider, or secret authority.

A five-minute execution-recovery boundary indexes only dispatched work, strongly rebinds the exact
job/work pair, and calls only `DescribeExecution`. It never starts, stops, or redrives an execution.
Terminal or missing executions converge through the existing CAS/idempotent settlement commands;
running or pending-redrive observations emit alarms only. The isolated function has singleton
concurrency, an encrypted DLQ, bounded retries, identifier-free embedded metrics, and corresponding
Lambda, EventBridge, Step Functions, DynamoDB, API, retention, cleanup, and recovery alarms. At
this offline checkpoint, the scaffold marker prevented these handlers from executing in AWS. The
later active draft-only transition enabled the reviewed Phase 6 handlers; current-release
formal evidence remains source- and deployment-bound and is not inferred from the functional demo
walkthrough.
The current pass/open decision and deployment-recovery boundary are recorded in the
[`authoritative Phase 6 release state`](phase6-release-state.md); historical checkpoints in this
document do not override it.

## Frozen gate order

The manifest requires:

1. offline replay, concurrency, all-route ownership, and exact-bundle three-engine browser gates;
2. deployed non-destructive edge/auth/ownership, upload-integrity/preview, and outbox-recovery
   smokes, including stuck-execution recovery and the reference-aware retention sweep;
3. double-gated primary same-job, concurrency, and seller-cancellation provider canaries; and
4. one moderated first-time seller completing the deployed flow without external documentation or
   operator intervention.

Prerequisites form a validated acyclic graph. A passed record must include every required assertion
in frozen manifest order and may not contain a failed assertion. Failed evidence must identify at
least one failed assertion. Inconclusive evidence remains recordable but cannot close a gate.

## Historical checkpoint verification

Run the focused credential-free contract gate with:

```shell
.venv/bin/python -m pytest -q tests/test_phase66_acceptance_contract.py
.venv/bin/python -m pytest -q tests/test_phase66_evidence_set.py tests/test_phase66_source_bundles.py
.venv/bin/python tools/export_phase66_acceptance_contract.py --check
.venv/bin/ruff check src/mr_lister/acceptance tests/test_phase66_acceptance_contract.py
.venv/bin/ruff format --check src/mr_lister/acceptance tests/test_phase66_acceptance_contract.py tools/export_phase66_acceptance_contract.py
.venv/bin/python -m tools.phase66_browser.run_gate
```

At this checkpoint, the full Python suite passed 1,419 tests with 11 explicitly gated live-Bedrock
skips; the Phase 6 and Phase 6.6 subsets passed 697 and 490 tests respectively. The web gate passed
62 tests, lint, strict typecheck, production build, and release-artifact checks. SAM lint/build,
Ruff lint/format, `compileall`, both contract drift checks, `npm audit`, and `git diff --check`
passed.

The Python contract/offline tests prove deterministic manifest drift detection, strict
discriminated evidence classes, closed structural JSON Schema objects, mandatory runtime semantic
validation, digest-only authority references, artifact confinement and rehashing, provider
double-gate/count rules, moderated-session isolation, and recursive rejection of raw authority
fields. They invoke no AWS, Bedrock, AgentCore, Cognito, S3, Printify, publication, order, or
fulfillment capability. The separate browser command launches only local engines against the
credential-free fixture server; it makes no AWS or provider request.
