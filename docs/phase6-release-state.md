# Phase 6 release state

## Decision

**PHASE 6 NOT YET COMPLETE**

This is the authoritative current-state record for Phase 6 as of 2026-08-31. The implementation
and clean-checkout verification baseline are green, but the deployed application stack requires
recovery and the final deployment-bound, provider-write, accessibility, and moderated-seller gates
remain open. Phase 6 must not be described as sealed until every blocking item below is attached to
one source/deployment authority.

The frozen gate definition remains
[`phase6.6.manifest.json`](../contracts/acceptance/phase6.6.manifest.json). The reconciled artwork
matrix and other additive product requirements remain authoritative in the
[`Phase 6 seller-control contract`](phase6-seller-control-contract.md). This record is the authority
for their current pass/open status and supersedes present-tense deployment claims in historical
checkpoint documents.

## Release identity and deployment state

| Item | Current authority |
| --- | --- |
| Closure implementation source | `7c4d668b2c322f1e4cad1802105567d4a38ee2c8` |
| Source-commit digest | `e016852a0ab39e2e454878c5ee6030257bcd07faaaaf8d0a3841e5b5f67c4b11` |
| Offline-evidence binding commit | `75f607eb9f7aa25e58eea746f1a335e567c902e1` |
| Package version | `mr-lister 0.1.0` |
| Phase 6 closure release/version | Closure candidate; not deployed, accepted, or sealed |
| Application stack | `mr-lister-phase6-dev` in `us-west-2` / `dev`, last authenticated observation `UPDATE_ROLLBACK_FAILED` (last updated `2026-08-31T20:11:17Z`) |
| Certifiable deployed version | None while the stack is nonterminal; the observed functions form the two-component mosaic below |
| Affected-function predecessor/rollback tuple | release `0c6211a5b0244e9c86d635e6c02e7bc49e5e948d68895b4aaa982c0b0b2e187b`; archive `baf152b732ce8574b6a6925bae7ab4ff849c1b83d4137076c52c6682553f9d48`; VersionId `pHutjLzKNpukwJ75Qs9s8YzXUAvgxZuS` |
| Review-query hotfix tuple | release `6e32d16ce16371a65815e2931e0a897a34bbbce5526300438d4fc29061813571`; archive `122958c1df7ed916de122ca95c5cf9b8a34c385a45b706f396d2907c29cb8f9c`; VersionId `zFS0yxHW0Jm0qZrHjirfQCwYyZwXAeVc` |
| Failed candidate attempt | release `2c1b2b5e47994832f0cd1be9fb8088e2b0b7e7be7aa18a117c09f5530fb7c549`; archive `ba6fbe0c46226918694dafc16d902c4d37228a58202e462c18f332e055e156c8`; VersionId `YIKUgrblt9kZxIwtaGysI0Wv480K6pJ3`; not accepted |
| Deployment health | Unproven after the failed update; the latest public `/health` attempt timed out |
| Seller publication | Disabled; Etsy publication remains Phase 7 |

The candidate update shown above failed because the retained CloudFormation
role could not read the exact candidate archive. Rollback then failed because the same role no
longer retained read authority for the predecessor archive. The affected Lambda configurations
were last observed back on the predecessor code, but the stack itself was not terminal and the
deployment is not accepted. `ReviewQueryApiFunction` remains on its separate immutable hotfix, so
the deployed functions are a deliberate code mosaic rather than one closure release.

The source templates now support two separately paired, exact S3 key/VersionId statements during
an update—one live rollback archive and one candidate. They enforce complete tuples, distinct
expanded bindings, scalar key/version pairing, and nonempty Stage B authority. The runbooks require
both reads to remain until the application stack becomes terminal, then contract to one live
statement; they prohibit premature contraction and rollback resource skipping. That repair is
locally verified but has not been applied to AWS.

AWS discovery initially used valid credentials, but a later readback reported an expired or
invalid AWS CLI login grant. No recovery mutation was attempted. Before any live recovery command,
renew the exact bootstrap profile with:

```shell
aws login --profile mr-lister-bootstrap
```

Then create and review a role-owner stack change set with exact predecessor and candidate reads,
execute it only after its resource diff is accepted, continue the application rollback with no
`resources-to-skip`, wait for
`UPDATE_ROLLBACK_COMPLETE`, verify function bindings and public health, and contract candidate read
authority. Follow
[`SIMPLE_ROOT_RUNTIME_DEPLOYMENT.md`](../infra/phase6/SIMPLE_ROOT_RUNTIME_DEPLOYMENT.md#recover-an-archive-read-rollback-failure).

### Rollback point

The exact rollback archive for the affected functions is:

- release fingerprint:
  `0c6211a5b0244e9c86d635e6c02e7bc49e5e948d68895b4aaa982c0b0b2e187b`;
- archive SHA-256:
  `baf152b732ce8574b6a6925bae7ab4ff849c1b83d4137076c52c6682553f9d48`;
- S3 VersionId: `pHutjLzKNpukwJ75Qs9s8YzXUAvgxZuS`.

This operational rollback point is distinct from historical acceptance evidence. Historical gates
1–7 bind source `e130292…`; deployed gates 5–7 also bind deployment digest `5f26e318…`. Those
records remain useful history but cannot accept the closure source.

## Frozen artwork input contract

The canonical contract is
[`contracts/artwork/phase6.0.0.json`](../contracts/artwork/phase6.0.0.json):

- one submission accepts one through five files through a picker, single-file drag/drop, or
  multiple-file drag/drop;
- every accepted file follows the same ingestion path and creates one independent job;
- PNG and safe self-contained SVG are required Phase 6 formats;
- JPG/JPEG is included after a low-risk assessment of the browser-side ingestion boundary;
- PNG bytes are validated and preserved; SVG and JPG/JPEG are normalized before the upload intent
  into proportional canonical PNG;
- square, portrait, and landscape artwork are valid; placement is width-driven and height follows
  the native aspect ratio;
- artwork is never cropped, distorted, padded, or forced square;
- transparent and opaque/background-filled artwork are valid seller choices, while a file with no
  visible pixel is invalid;
- per-file validation, processing, review, failure, and retry state isolate an invalid sibling
  without blocking valid files.

Downstream storage, worker, Strands, Printify draft, and review boundaries consume canonical PNG
and do not branch on source format. The browser and backend enforce the same dimensional and
visibility contract.

PDF remains nonblocking for Phase 6. Reliable rendering would add a parser or native renderer plus
worker, CSP, packaging, and security surface. The smallest future contract is a single-page artwork
PDF normalized at the ingestion boundary to canonical PNG; multi-page PDF is out of Phase 6 scope.

## Product boundary

The accepted Phase 6 flow remains:

> authenticated seller -> one or more artworks -> validation and canonical normalization ->
> artwork intelligence -> same-job Strands orchestration -> listing generation -> unpublished
> Printify draft creation -> seller review -> approval -> STOP

There is no Phase 6 publish action, publication worker activation, order, or fulfillment path.
Existing disabled/sealed Phase 7 source in repository history was not activated or extended during
this stabilization pass.

## Verification state

The normal checkout completed the CI-equivalent verification path. A detached clean worktree then
completed fresh Python and locked Node dependency installs and repeated that same path:

- Python: **3,341 passed**, 11 explicitly gated live-Bedrock skips, no failures;
- web: ESLint, strict TypeScript, **114 tests**, and production build passed;
- `npm audit --audit-level=high`: zero vulnerabilities;
- Ruff lint and formatting, `compileall`, and `git diff --check`: passed;
- all three contract exporters: passed with no drift;
- all six Phase 6 SAM templates: passed `sam validate --lint`;
- isolated sdist and wheel builds: passed and were byte-identical between normal and clean
  checkouts;
- distribution boundary: the sdist contains only the explicit tracked source/package files and no
  private, staging, deployment, or ignored workspace payload;
- exact production browser bundle: Chromium, Firefox, and WebKit passed with bundle digest
  `977f82ebdb4b0acd35f7db7081cea3a34dc71beeb2d40cd7bec79bb27aeadc21` and zero provider
  transport attempts;
- source-bound offline gates: four passed, manifest digest
  `84851fe2ed78072d077cc5e642d0e222619b9a7226367219b536b7e2aaac7d73`, record-set digest
  `8392808024da0c0f2a5083eb48c64c7dea8efc72908ca71786b628345c9d4cf9`.

GitHub CI is eligible from a clean checkout. Its actual `main` result must be recorded after this
closure line is fast-forwarded and pushed; a local green run is not represented as a remote CI run.

## Acceptance matrix

| Gate | Blocking | Current status |
| --- | --- | --- |
| `offline.replay_matrix` | Yes | Passed for source `7c4d668…` |
| `offline.concurrency_matrix` | Yes | Passed for source `7c4d668…` |
| `offline.cross_owner_matrix` | Yes | Passed for source `7c4d668…` |
| `offline.browser_matrix` | Yes | Passed for source `7c4d668…`, all three engines |
| `deployed.edge_auth_owner_smoke` | Yes | Open; historical evidence only and stack recovery required |
| `deployed.upload_integrity_smoke` | Yes | Open; historical evidence only |
| `deployed.outbox_recovery_smoke` | Yes | Open; historical evidence only |
| `provider.primary_same_job_canary` | Yes | Open; never completed for a closure release |
| `provider.concurrency_canary` | Yes | Open; never completed for a closure release |
| `provider.cancellation_canary` | Yes | Open; never completed for a closure release |
| `moderated.first_time_seller_exit` | Yes | Open; never completed |
| `moderated.five_session_target` | No | Open; five sessions remain the evidence target |

Additional blocking acceptance outside the already-frozen manifest remains open:

- execute and attach the final artwork matrix across PNG/SVG and included JPG/JPEG, picker and
  single/multiple drag/drop, geometry/background variants, per-file retry, and browser/backend
  parity against the accepted deployment;
- complete manual screen-reader and contrast evidence plus edit, refresh, cancel, retry, upload,
  and logout journeys against the final bundle.

## Smallest blocker list

1. Authenticate `mr-lister-bootstrap`, apply the reviewed dual exact-archive role-owner change,
   complete rollback without skips, contract authority, and prove stack and public health.
2. Deploy one exact closure source without enabling publication, then rerun deployed gates 5–7
   plus the additive artwork/authenticated accessibility matrix against that same deployment.
3. Open the explicit run and provider-write gates and complete unpublished Printify same-job,
   concurrency, and seller-cancellation canaries with sanitized ledgers.
4. Complete the manual accessibility journeys and one moderated first-time-seller exit.

Until all four blocker groups close against one source/deployment authority, Phase 7 work remains
paused and **Phase 6 is not sealed**.
