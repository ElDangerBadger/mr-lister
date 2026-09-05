# Phase 7 production-disabled operations runbook

## Current authority

This runbook governs the deployed Phase 7.15C production-disabled release. It does not authorize a
seller publication route, a publication request, a provider credential read, a Printify
publication POST, or an Etsy listing. Contract `7.0.1` remains frozen at
`offline_implementation` with `publication_enabled=false`.

The template may instantiate resources only with CloudFormation
`ActivationMode=PRODUCTION_DISABLED`. Inside every Lambda, the separate application value remains
`MR_LISTER_PHASE7_ACTIVATION_MODE=SOURCE_ONLY_DISABLED`; scaffold-only is true and query, request,
publication, and production-candidate flags are false. All six functions retain reserved
concurrency zero, all DynamoDB/SQS mappings and EventBridge rules remain disabled, the seller query
and request functions have no API or Function URL, and the packaged entrypoints refuse before
reading the invocation. General availability is not an allowed template mode.

## Deterministic local build

From source checkpoint `7c933dd2cfd76e418d57ce1e25d9f6ffe3c69d3f`, with the reviewed private
14-wheel Lambda wheelhouse present:

```bash
.venv/bin/python -m tools.build_phase715_production_disabled_release
.venv/bin/python -m tools.build_phase715_production_disabled_release \
  --build-dependencies-from-wheelhouse .mr_lister_private/phase6-lambda-wheelhouse \
  --build-request .mr_lister_private/phase7-production-disabled-source/dependency-build-request.json
.venv/bin/python -m tools.build_phase715_production_disabled_release \
  --verify-dependency-artifact .mr_lister_private/phase6-lambda-dependencies \
  --build-request .mr_lister_private/phase7-production-disabled-source/dependency-build-request.json
.venv/bin/python -m tools.build_phase715_production_disabled_release \
  --seal-source-release .mr_lister_private/phase7-production-disabled-source \
  --dependencies .mr_lister_private/phase6-lambda-dependencies
.venv/bin/python -m tools.build_phase715_production_disabled_release \
  --verify-deployment .mr_lister_private/phase7-production-disabled-deployment \
  --archive .mr_lister_private/phase7-production-disabled-artifact/production-disabled.zip \
  --descriptor .mr_lister_private/phase7-production-disabled-artifact/deployment-descriptor.json
```

The dependency build already writes and verifies `dependency-artifact.json`; do not invoke the
separate manifest-writing action on that sealed directory. The build was repeated in an independent
temporary directory and both archive and descriptor compared byte-for-byte.

| Authority | SHA-256 / value |
| --- | --- |
| Release manifest | `9c4deca1813e5d1e8cc3f6747681b2194265f9c0b51b64fd9cf6b8afeb823c46` |
| Archive | `43721a48802bd3bbc946671aff938b6df030b495975c8bc59839db18986da88f` |
| Archive size | `62,982,212` bytes |
| Deployment manifest | `068a9956609c70ab01059e0a7c08b8499dbf1edc67dedcb4032bd0a6dd3459ab` |
| Source manifest | `58fb8526f8767549347a1a90452cd144867a593fbd3a7e78218ebe84b6ebc4f9` |
| Dependency manifest | `4945e5c68931676783932eb33b933f40e107765296fcfdd9f2ea6363ef6ce04f` |
| Topology binding | `f26d28b96664415facbb153c74364b9b0e4b2478af1a20431918b96e498de3b8` |
| Deployment descriptor | `fdfc0797b3fc7b3b750108a76ae38f854c65cf0dfd6ac9abdb653b2456ea2708` |
| Disabled template | `2a98ab2a7cf3fb04590f9f8cd3a30cc6c2e373421e70c70220be419b80ca7df2` |
| Publication workflow | `9a6112c85b35e775d1e60681a0ca14e6740cd0aea82b2ac33b5aa74b86fc3098` |

## Live deployment record

This exact release is deployed in account `384627057108`, region `us-west-2`, through profile
`mr-lister-dev`:

| Authority | Value |
| --- | --- |
| Versioned archive | `phase7/candidates/9c4deca1813e5d1e8cc3f6747681b2194265f9c0b51b64fd9cf6b8afeb823c46/production-disabled.zip`, version `6ix.miylQqgEZyV392IenODAlQvbAp4F` |
| Packaged template SHA-256 | `2a6f45a790e554e3680e23c4d35abf4d8a2a99611a20e301c66d2a61a284b9db` |
| Versioned packaged template | `phase7/sam/templates/2a6f45a790e554e3680e23c4d35abf4d8a2a99611a20e301c66d2a61a284b9db.yaml`, version `fvTXvRtq9r.JtdyorhIzV.PZGLei9w4D` |
| Stack | `mr-lister-phase7-dev`, `CREATE_COMPLETE` |
| Stack ID | `arn:aws:cloudformation:us-west-2:384627057108:stack/mr-lister-phase7-dev/e3ee3330-a671-11f1-8e50-02219a1c6639` |
| Change set | `mr-lister-phase7-dev-production-disabled-create-9c4deca1813e` |
| CI | GitHub Actions run `33580123287`, green |

The processed change set contained 49 additions, no modifications or removals, and no Phase 6
resource. Exact artifact, Lambda, IAM, mapping, rule, queue, workflow, log, metric-filter, alarm,
SNS, KMS, and stack-output readback passed. All six functions have reserved concurrency zero, all
six Function URL lookups return not found, every event source and schedule is disabled, the worker
has no provider credential or mutation authority, and publication-related stack outputs remain
false. SNS subscription counters are zero; direct subscription enumeration was denied and is not
claimed. The Phase 6 stack remained unchanged.

Two idle samples from `2026-09-02T02:06:27Z` through `02:16:59Z` proved empty due/recovery
partitions, empty queues, no Step Functions execution, no Lambda invocation datapoints, no stored
log data, and no change to Phase 6. These close the production-disabled deployment/readback/idle
checkpoint; they do not authorize invocation or close the separate non-provider operations drills
below.

## Required order

1. Build the deterministic production-disabled source, dependency, deployment, release, archive,
   and descriptor authorities from a clean checkout.
2. Verify the archive against the current checkout and bind its release fingerprint to the
   derived S3 key `phase7/candidates/<release-fingerprint>/production-disabled.zip` and one exact,
   non-null S3 object version.
3. Run the read-only operations preflight while every Phase 7 writer and trigger is disabled.
4. Create and review a CloudFormation change set for the separate Phase 7 stack. Replacement or
   mutation of the Phase 6 table, stream, Cognito resources, APIs, functions, roles, or web edge is
   a hard stop.
5. Deploy only the production-disabled mode and capture exact processed-template, resource,
   Lambda, IAM, trigger, queue, workflow, log, alarm, and artifact readback.
6. Prove an idle interval: zero Lambda invocations, zero Step Functions executions, zero provider
   calls, empty Phase 7 due/recovery partitions, and no seller route.
7. Do not exercise recovery or retention handlers through this artifact: its entrypoints refuse
   and concurrency is zero. Review and seal a separate non-provider operations runtime before
   those live drills, without enabling request intake or provider capability.
8. Exercise only that separately authorized non-provider recovery, DLQ, alarm-delivery, retention,
   and rollback matrix.
9. Advance to deployed read-only validation only after those observations are sealed. A live
   publication canary remains a later, explicit, one-listing authorization.

## Index preflight

`tools/phase715c_operations_preflight.py` is an injected, read-only library boundary, not a
standalone AWS CLI. It constructs no AWS client and exposes only `DescribeTable` and `Query` in
its client protocol. A narrow deployed adapter remains required. A pass requires:

- the exact active Phase 6 table ARN and primary key;
- the existing `DueWorkIndex` (`dispatch_pk`, `dispatch_sk`, projection `ALL`);
- the existing `ExecutionRecoveryIndex` (`recovery_pk`, `recovery_sk`, projection `KEYS_ONLY`);
- the exact `KEYS_ONLY` table stream;
- two empty, cursor-free `COUNT` queries against only `PUBLICATION_WORK_DUE#0` and
  `PUBLICATION_WORK_RECOVERY#0`; and
- for local use, explicit source-only status; or, for any deployed claim, the complete exact
  readback showing every expected mapping and EventBridge rule disabled.

The tool never scans. Any row, cursor, schema drift, unexpected trigger, malformed response, or
unknown index is a hard stop. Do not skip or delete a poison row automatically. Record only the
sanitized evidence digest publicly; keep any key-level investigation in ignored private evidence.
The two immediate eventually consistent reads are schema/emptiness checks, not an idle-interval
proof. A deployed operator must run temporally separated observations with the full trigger tuple
and separately capture CloudWatch/Step Functions/provider inactivity.

## Recovery behavior

Before the first durable dispatch commit, recurring due-work inspection distinguishes an exact
running execution from an exact failed execution. `FAILED`, `TIMED_OUT`, or `ABORTED` produces only
the canonical three-field workflow-failure envelope for the encrypted recovery queue. A successful
workflow with still-pending durable authority is a conflict, not success and not a mutation.

After dispatch, active `DISPATCHED`, `VERIFYING`, and `RECONCILING` work uses base keys
`PK=PUBLICATION#{aggregate_id}` and `SK=PUBLICATION_WORK#{work_request_id}`, plus
`recovery_pk=PUBLICATION_WORK_RECOVERY#0` and
`recovery_sk={updated_epoch:020d}#{aggregate_id}#{work_request_id}`. Pending and terminal rows omit
the recovery attributes. The scheduled recovery sweep reads at most 25 ordered KEYS_ONLY hints,
strongly reloads and revalidates each exact authority graph, and may describe or redrive only the
deterministically derived execution ARN. It cannot start an execution or construct a provider.
Terminal rows and ordinary stale GSI hints are no-ops. Deadline settlement uses the existing
idempotent durable transition. Identity conflicts, missing/success-without-terminal executions,
dependency failures, non-redrivable work, and batch saturation surface through the
Lambda/EventBridge failure and DLQ alarm path only after every candidate returned in that bounded
page has been considered.

A full 25-row page is deliberately a failed invocation and operator alert. The single-partition,
stateless query has no durable continuation cursor, so it does not claim fair traversal of later
rows while the first page persists. Closing that guarantee requires reviewed cursor/shard or
bounded continuation authority and corresponding IAM/state; do not silently increase the query
or add Scan.

## DLQ triage

`tools/phase715c_dlq_triage.py` is also an injected library rather than a default AWS CLI. It
receives at most one message and returns a body-free plan containing only hashes, counts, a closed
classification, and blockers. A narrow AWS adapter remains required. Raw bodies and receipt
handles may be written only as create-once, owner-only files beneath
`.mr_lister_private/phase715c-operations`.

- Never use `StartMessageMoveTask` or bulk redrive on the shared operations DLQ.
- Only the two canonical recovery envelopes are eligible for an exact resend.
- Resend requires the exact plan digest, body digest, and explicit action confirmation; queue
  encryption/redrive authority and the SQS body MD5 are revalidated.
- A resend retains the source DLQ message. Delete authority is intentionally absent until a
  separate durable-recovery readback can prove settlement.
- Event-source artifacts, malformed messages, and unknown messages are classifier-only and are
  never resent or deleted.

Due-sweep delivery artifacts do not replay stale wake-ups; prove a later sweep succeeded. Stream
delivery artifacts require source-specific strong readback before a future narrow replay tool is
designed. Retention artifacts require an exact terminal-graph rebind. No DLQ procedure grants a
provider call or publication POST.

## Retention activation order

Retention must be operational before any publication request can be accepted. For a clean first
activation, both Phase 7 index partitions must pass the empty preflight twice, then the disabled
retention path and marker-last transaction must be live-tested without provider mutation before
request intake is considered. If any terminal Phase 7 authority already exists, complete and
verify a bounded terminal-link/TTL backfill first. Never enable a request route while retention is
disabled, lagging, or partially backfilled.

## Remaining live evidence

The deployed production-disabled checkpoint does not yet claim the following observations:

- a tested rollback tuple for the current successful stack;
- same-ARN Step Functions redrive and SQS visibility/redrive timing;
- EventBridge target exhaustion reaching the encrypted DLQ and alarm notification delivery;
- recovery-index loss simulation, batch saturation, and poison-row diagnosis;
- retention duration, marker-last completion, and any required backfill;
- a narrow deployed adapter for the injected preflight and DLQ libraries; and
- durable continuation authority before automatic recovery fairness beyond a saturated first page
  is claimed.

If the deployment identity lacks an exact required read or change-set permission, stop at that API
and report the profile, action, and resource. Do not substitute a root/bootstrap identity without
explicit authorization.

## Local verification record

The source checkpoint passed 3,864 Python tests with 11 credential-gated live Bedrock tests
skipped, 131 web tests, Ruff lint and format checks, all three contract drift checks, all Phase 6
and Phase 7 SAM validations, Python package build, web lint/typecheck/production build, and npm
high-severity audit with zero vulnerabilities.

The active-work index and strong aggregate rebind deliberately changed the historical P7.9
triggerless worker-source closure. Its manifest/archive were reviewed and resealed from
`ca44431e5cfe3b0222560bc8bd8f6d7aa58468257760eae2768de5234a3412e8` /
`9f8a6916b4bf6cc3c7fe384de1d5b5d6a7a3b8244e5d2701b8cdf1e5c47cfa2e` to
`7dd3d5b7c2e2cf691aa9a2d7234cd211d7fb5d8419710724a7bbb5a4788d361c` /
`747cb0719a9242cb85531f2152746b60fefcca9e79e145cd415f9e03ab064c96`.

The subsequent narrow Printify readback-normalization fix was reviewed and deterministically
resealed from that manifest/archive to
`eb506fee3a0deb9d2cc9077af51094f4754fe8e83ca54e737762152566e7746f` /
`a8b54e21e287d3a1fb4236b37612f56e0edfa5e8df736f898fe96904b6ebb5ad`.
It changes provider response normalization only; it does not enable publication or add a trigger.

The subsequent Etsy-safe SKU normalization was reviewed and deterministically resealed from that
manifest/archive to
`6ef0ad14098598337a739608e5a55f70ca8622077d30098c7b736655f5fa0789` /
`50ef471e9f20e90ccfbe08e2393c35bace29da0b4d8707a3c83e8cbc682aca4e`.
It changes only the provider SKU wire/readback representation and adds no publication authority,
route, trigger, or credential capability.

## Irreversible boundary

The production-disabled archive must never be relabeled or reused as an enabled release. A
one-listing MassSkutiny canary requires a separately sealed canary binding and explicit approval
of the exact job/listing. A generally enabled seller flow requires a later reviewed contract and a
new release. Infrastructure rollback cannot unpublish an external Etsy listing.
