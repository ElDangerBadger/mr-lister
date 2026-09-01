# Phase 6 release state

## Decision

**PHASE 6 COMPLETE AND SEALED**

Phase 6 is functionally complete for the hackathon demo as of 2026-09-01. The accepted product
boundary is an authenticated seller preparing one or more artworks, receiving one independent
unpublished Printify draft per accepted file, reviewing the generated work and live economics,
and recording an approval. The flow stops at approval. It cannot publish to Etsy.

This decision follows the 2026-09-01 stabilization instruction to keep the green runtime fixed,
use the minimum honest demo acceptance criteria, and avoid changing the runtime only to collect
more evidence. The frozen [`phase6.6.manifest.json`](../contracts/acceptance/phase6.6.manifest.json)
is unchanged. Its exhaustive deployed-canary and moderated-research artifact set remains a
strict post-demo hardening program; it is not represented here as completed. The seal therefore
means **functional Phase 6 demo release**, not completion of every artifact demanded by that
stricter assurance program.

## Authoritative release identity

| Item | Sealed authority |
| --- | --- |
| Runtime source commit | `15a4f2a657e4cf5809de7066d267455d65c8c835` |
| Deployment renderer commit | `6937f5fde2856bf396f44a5926e5229f8e5cd0e6` |
| Package version | `mr-lister 0.1.0` |
| Application stack | `mr-lister-phase6-dev`, `us-west-2`, `UPDATE_COMPLETE` at `2026-09-01T18:01:13Z` |
| Deployment readiness | `WEB_EDGE_ACTIVE_DRAFT_ONLY` |
| Application template | SHA-256 `ee2941498cadbaf365c703b1694ec791c93ed9fdb9c2631a8d3117a6b11bd4a3`, S3 VersionId `0Ct1y8MH62B6XSA_sMVUSEjnITvkT7uY` |
| Provider component release | `748e5c4a1e46c500215118685d1f70231b7f28b8bfe8e67cc804da1c33e7c347` |
| Provider archive | SHA-256 `0a12ede1b86c47069fda57938d1b0d50aeab6acca5d04ab536446d2f414de405`, S3 VersionId `w60dzk3jW5BZVxy0_SfoKWYVy_3.oL8g` |
| Preparation component | release `9bc5e1727cfcf68b40847d1a2e416300640779898c9bf884f6f9e442b0225d9e`, code SHA-256 (base64) `2xedxftXVGGUgrE1BfeJlGnpPoILvhhRSVOEmsG5Wcc=` |
| Strands runtime | AgentCore runtime `mr_lister_phase6-4HoPmq2hCI`, endpoint/qualifier `phase6_v4_dev`, version `4`, binding `e1403259a1a1a67ce47b725f0bec2d9a5aa38673fad338924f12b9360880b922` |
| Seller publication | Disabled; Etsy publication remains Phase 7 |

This is intentionally a component-bound deployment tuple. The ProviderDraft hotfix changed only
that function; the application stack's older global `ReleaseFingerprint` parameter and the
unchanged Preparation and AgentCore component identities were retained. CloudFormation events
confirmed that only `ProviderDraftFunction` updated. The retained deployment role was contracted
after success to one exact-version read for the new provider archive, and termination protection
remained enabled.

## Frozen artwork input contract

The canonical contract is
[`contracts/artwork/phase6.0.0.json`](../contracts/artwork/phase6.0.0.json):

- one submission accepts one through five files through the picker, single-file drag/drop, or
  multiple-file drag/drop;
- every accepted file follows the same ingestion/normalization path and creates an independent
  job;
- PNG and safe self-contained SVG are required formats; JPG/JPEG is included through the same
  bounded browser ingestion boundary;
- PNG bytes are validated and preserved; SVG and JPG/JPEG are normalized before upload into
  canonical PNG;
- square, portrait, and landscape artwork are valid; placement sizing is width-driven and height
  follows the native aspect ratio;
- artwork is not cropped, distorted, padded, or forced square;
- transparent and opaque/background-filled artwork are both valid seller choices; and
- per-file validation and recovery isolate an invalid file without blocking valid siblings.

Downstream storage, workers, Strands orchestration, Printify draft creation, and seller review
consume the canonical representation and do not branch on the original file type. PDF remains a
nonblocking future ingestion format. The smallest future PDF contract is one page normalized to
canonical PNG; multi-page document processing is outside Phase 6.

## Functional acceptance

The final real-seller walkthrough used the public `massskutiny.com` application and the existing
MassSkutiny seller account. It reused one private job and one private provider product throughout;
their exact identifiers remain only in access-controlled evidence:

- authenticated upload: transparent PNG, `1,693,269` bytes, accepted through the production web
  and API path;
- same-job Strands evidence: framework `strands-agents`, agent `mr-lister-preparation`, review
  version `1`, and tool `record_prepared_review`;
- Printify result: one editable and unpublished product, with five front mockups and all 30
  configured variants;
- live economics: connected production readback plus standard U.S. shipping produced an
  estimated proceeds range of `$8.53–$11.45` across 30 variants;
- seller decision: the explicit **Approve draft — keep unpublished** confirmation moved the same
  job to `Complete / Approved`; and
- terminal boundary: the page continued to state **Unpublished — not on Etsy**, and no publish,
  order, or fulfillment action was exposed or invoked.

The walkthrough exposed one real defect before closure: Printify's shipping endpoint returned a
bounded but much larger multinational catalog with duplicate, equivalent U.S. plan rows. The
provider ingestion boundary now selects the exact standard-U.S. resource type, accepts duplicate
rows only when their cost and handling terms agree, chooses deterministically, and still fails
closed on conflicting data. The same job's single retry passed after that targeted deployment; no
second upload or product was created.

## Acceptance-gate classification

| Gate or concern | Classification at seal | Demo decision |
| --- | --- | --- |
| Offline replay, concurrency, cross-owner, and three-browser matrices | Provable now | Passed; source-bound automated evidence is green |
| Artwork type/shape/background parity, single/multiple handling, per-file errors, retry, and accessibility basics | Provable now | Passed through contract, unit, component, and browser coverage; the real flow additionally proved a transparent PNG |
| Authenticated full flow, same-job Strands, unpublished Printify draft, live economics, seller review, and approval stop | Manually provable | Passed in the MassSkutiny walkthrough above |
| Exact deployed edge/upload/outbox manifest artifact bundle | Manually provable with existing canary tooling | Deferred; nonessential for the demo after the real authenticated flow and green offline coverage |
| Exact five-MiB provider ledger canary | Manually provable with a separately authorized provider mutation | Deferred; the functional same-job provider outcome passed, but the frozen five-MiB/ledger artifact gate is not claimed |
| Live revise/approve/cancel concurrency and separate cancellation canaries | Manually provable with deliberately competing/provider-mutating runs | Deferred; nonessential demo stress evidence, with the behavior already proved offline |
| Manual screen-reader/contrast matrix | Manually provable | Deferred usability hardening; semantic, keyboard, focus, reflow, reduced-motion, and axe coverage are green |
| Moderated first-time-seller and five-session study | Manually provable research | Deferred and nonessential for the hackathon demo; the MassSkutiny run is not misrepresented as intervention-free research |
| Runtime changes solely to emit stricter evidence | Would require runtime modification | Not authorized and not required; no such change was made |

No functional demo blocker remains. The deferred rows are explicit future assurance work and do
not reopen this sealed runtime.

## Verification and CI

- full Python suite: **3,674 passed**, 11 explicitly gated live-Bedrock skips;
- web suite: ESLint, strict TypeScript, **114 tests**, production build, and artifact verification
  passed;
- Ruff lint and formatting, three contract drift checks, six Phase 6 SAM lint validations,
  Python sdist/wheel builds, compile checks, and diff hygiene passed;
- locked dependency audit reported zero high-severity vulnerabilities; and
- GitHub Actions run
  [`33540053154`](https://github.com/ElDangerBadger/mr-lister/actions/runs/33540053154)
  passed both `verify` and `web` from a fresh checkout of `main` at deployment renderer commit
  `6937f5f`.

Authenticated browser evidence is retained outside Git under the ignored private release root
with restrictive permissions. Normal and clean-checkout verification do not depend on that
private evidence.

## Rollback point

The immediate predecessor remains available as an exact-version rollback:

- Provider release `3bfccee40d144f284827da221df40a787f7d09242698d177b51bbcc1414b7308`;
- Provider archive SHA-256
  `e5f17ee5063a2a6aaf490df899d7c98d5a79c97b6d29911260214a6ac599cc9b`;
- Provider archive S3 VersionId `yKXCfg0bAMU0Kyu6xAoM2hXtS3KxxHkC`;
- application template SHA-256
  `4fac9ab9cfa5712c521644ad6934f775e1283cd9d604b674f68a62cbfcbaeda6`;
- application template S3 VersionId `2IqLmyJ_0OWLXypdjH258FmXQ2YrmtmD`; and
- AgentCore v4 remains unchanged during rollback.

## Phase boundary

Phase 6 is sealed at seller approval. No new Phase 7 implementation was started by this closure
pass. Etsy publication is part of the final product, but it belongs to Phase 7 and remains disabled
until separately authorized, deployed, and accepted.
