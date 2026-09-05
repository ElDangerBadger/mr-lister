# Phase 6 release state

## Decision

**PHASE 6 COMPLETE AND SEALED**

Phase 6 was functionally accepted for the hackathon demo on 2026-09-01, resealed on 2026-09-03
after a narrowly scoped provider-reconciliation correction, and resealed again on 2026-09-04
after the real seller revision path exposed two narrow provider-synchronization defects. The
accepted product boundary is an authenticated seller preparing one or more artworks, receiving
one independent unpublished Printify draft per accepted file, reviewing and revising the
generated work and live economics, and recording an approval. The flow stops at approval. It
cannot publish to Etsy.

This decision preserves the 2026-09-01 stabilization instruction to use the minimum honest demo
acceptance criteria and avoid changing the runtime only to collect more evidence. The 2026-09-03
and 2026-09-04 provider-boundary repairs fixed real seller failures without expanding the Phase 6
product boundary. The frozen
[`phase6.6.manifest.json`](../contracts/acceptance/phase6.6.manifest.json) is unchanged. Its
exhaustive deployed-canary and moderated-research artifact set remains a strict post-demo
hardening program; it is not represented here as completed. The seal therefore means
**functional Phase 6 demo release**, not completion of every artifact demanded by that stricter
assurance program.

## Authoritative release identity

| Item | Sealed authority |
| --- | --- |
| Runtime source commit | `06484524ed8ff8b9211c5f5bd1f0bcc4d4f540bc` |
| Package version | `mr-lister 0.1.0` |
| Application stack | `mr-lister-phase6-dev`, `us-west-2`, `UPDATE_COMPLETE` at `2026-09-05T00:02:47Z` |
| Application release binding | `0c6211a5b0244e9c86d635e6c02e7bc49e5e948d68895b4aaa982c0b0b2e187b` (unchanged) |
| Deployment readiness | `WEB_EDGE_ACTIVE_DRAFT_ONLY` |
| Application template | SHA-256 `f5ec8379ff0db54ae05be5f202e8e24850610b8edd66200d2a69e73be09d1012`, S3 VersionId `Zx5tLGxyXNrzi7KkvzjlNGJuEauU2_q_` |
| Provider component release | `a4f00b79d7b6f4ef676981b05a4cc369645d09f53921d8939e06a851e7e9b8f5` |
| Provider archive | SHA-256 `bb1fe74de98793de8650fb7f62da7f0bb76332ead7897819da537a6efe55d5e6`, S3 VersionId `nt5jDNEp.AMytDDRDalZWrjwuQhYPo9a` |
| Provider Lambda readback | code SHA-256 (base64) `ux/nTemHk96GUPt/Ytp/C7djMurXiXgZ2lN6bv5V1eY=`, `62,711,296` bytes |
| Preparation component | release `9bc5e1727cfcf68b40847d1a2e416300640779898c9bf884f6f9e442b0225d9e`, code SHA-256 (base64) `2xedxftXVGGUgrE1BfeJlGnpPoILvhhRSVOEmsG5Wcc=` |
| Strands runtime | AgentCore runtime `mr_lister_phase6-4HoPmq2hCI`, endpoint/qualifier `phase6_v4_dev`, version `4`, binding `e1403259a1a1a67ce47b725f0bec2d9a5aa38673fad338924f12b9360880b922` |
| Seller publication | Disabled; Etsy publication remains Phase 7 |

This is intentionally a component-bound deployment tuple. The latest ProviderDraft hotfix changed
only that function; the application stack's older global `ReleaseFingerprint` parameter and the
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

The accepted 2026-09-01 real-seller walkthrough used the public `massskutiny.com` application and
the existing MassSkutiny seller account. It reused one private job and one private provider
product throughout; their exact identifiers remain only in access-controlled evidence:

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

A later real upload exposed a separate reconciliation defect: the Printify Media Library can
legitimately contain JPEG and other unrelated rows, but upload reconciliation parsed every row as
a canonical Phase 6 PNG/SVG candidate before comparing its deterministic job filename. One
unrelated row could therefore stop an otherwise valid upload after the image reached Printify but
before product creation. The 2026-09-03 correction filters raw list results by exact filename
before strict parsing; exact-name matches still receive all schema, duplicate, identity, MIME,
geometry, and source-binding checks.

The post-deployment MassSkutiny verification used a different artwork and a fresh private job.
It passed authenticated upload, same-job Strands preparation, Printify image upload, exact image
readback, unpublished product creation, exact product readback, and live economics. The durable
result reached validated seller review with five mockups, all 30 configured variants, no active
work or failure, and `provider_locked=false` / `provider_published=false`. It stopped cleanly at
`awaiting_approval`, so it proves that the deployed provider component still completes the normal
path through seller review after the correction, without claiming a second approval action. The
narrow uncertain-upload reconciliation branch remains hermetically regression-tested; it was not
deliberately fault-injected against the live provider. The unchanged approval control remains
established by the 2026-09-01 walkthrough above.

The 2026-09-04 walkthrough then exercised the full revision path. It first showed that a completed
provider-write attempt from review 1 must not become the attempt authority for a new review 2;
only an unconfirmed outcome may retain that reconciliation authority. After that correction, the
same run exposed that a listing-text update must use Printify's partial-update boundary instead of
resending the product-creation payload. The provider can expand its stored variant representation,
and resending only the 30 selected variants together with placement fields produced an ambiguous
update whose readback remained at the exact prior review. The final correction sends only the
seller-editable `title`, `description`, and `tags` fields on PUT while retaining the full canonical
desired fingerprint and exact before/after readback.

The final fresh MassSkutiny canary started from an authenticated exact-byte PNG upload. Review 1
created one editable, unpublished Printify product with five mockups, all 30 configured variants,
and `$8.53–$11.45` estimated proceeds. A controlled title-only revision synchronized that same
product to review 2, retained the five mockups and 30 variants, and refreshed the economics. The
seller then confirmed **Approve draft — keep unpublished**; the job reached `Complete / Approved`
while **Unpublished — not on Etsy** remained visible. Exact job and product identifiers and the
full-page approval capture are retained only in the ignored, access-controlled release evidence.

## Acceptance-gate classification

| Gate or concern | Classification at seal | Demo decision |
| --- | --- | --- |
| Offline replay, concurrency, cross-owner, and three-browser matrices | Provable now | Passed; source-bound automated evidence is green |
| Artwork type/shape/background parity, single/multiple handling, per-file errors, retry, and accessibility basics | Provable now | Passed through contract, unit, component, and browser coverage; the real flow additionally proved a transparent PNG |
| Authenticated full flow, same-job Strands, unpublished Printify draft, live economics, seller review, live revision, and approval stop | Manually provable | Passed again in the September 4 fresh MassSkutiny canary: review 1 created the draft, review 2 updated the same product, and explicit unpublished approval reached `Complete` |
| Exact deployed edge/upload/outbox manifest artifact bundle | Manually provable with existing canary tooling | Deferred; nonessential for the demo after the real authenticated flow and green offline coverage |
| Exact five-MiB provider ledger canary | Manually provable with a separately authorized provider mutation | Deferred; the functional same-job provider outcome passed, but the frozen five-MiB/ledger artifact gate is not claimed |
| Live revise/approve/cancel concurrency and separate cancellation canaries | Manually provable with deliberately competing/provider-mutating runs | Deferred; nonessential demo stress evidence, with the behavior already proved offline |
| Manual screen-reader/contrast matrix | Manually provable | Deferred usability hardening; semantic, keyboard, focus, reflow, reduced-motion, and axe coverage are green |
| Moderated first-time-seller and five-session study | Manually provable research | Deferred and nonessential for the hackathon demo; the MassSkutiny run is not misrepresented as intervention-free research |
| Runtime changes solely to emit stricter evidence | Would require runtime modification | Not authorized and not required; no such change was made |

No functional demo blocker remains. The deferred rows are explicit future assurance work and do
not reopen this sealed runtime.

## Verification and CI

- full Python suite: **4,008 passed**, 11 explicitly gated live-Bedrock skips;
- web suite: ESLint, strict TypeScript, **143 tests**, production build, and artifact verification
  passed;
- Ruff lint and formatting, three contract drift checks, six Phase 6 SAM lint validations,
  Python sdist/wheel builds, compile checks, and diff hygiene passed;
- locked dependency audit reported zero high-severity vulnerabilities; and
- GitHub Actions run
  [`33930965365`](https://github.com/ElDangerBadger/mr-lister/actions/runs/33930965365)
  passed both `verify` and `web` from a fresh checkout of `main` at runtime source commit
  `06484524`.

Authenticated browser evidence is retained outside Git under the ignored private release root
with restrictive permissions. Normal and clean-checkout verification do not depend on that
private evidence.

## Rollback point

The immediate predecessor remains available as an exact-version rollback:

- source commit `a7c83657a997d4391cf51c8ec2ff78c151314425`;
- Provider release `2fb6392d836735058d28c7a5ce6b37bed5d407039fa37817a92799419cc540fb`;
- Provider archive SHA-256
  `bd5620e323c6a4381810b452e08384674e46b45b3f4ae46e03a932008bed396d`;
- Provider archive S3 VersionId `2qh0.z5.dlhGxbBvtW2mpLwIDxEZW.F4`;
- exact predecessor application template SHA-256
  `ff1168485a2fdd94b4f8b7a2f31b462cf3a166294285a0c067411d00f6b9758b`, retained in the
  access-controlled deployment evidence; and
- AgentCore v4 remains unchanged during rollback.

## Phase boundary

Phase 6 is sealed at seller approval. Subsequent Phase 7 work remains isolated in its own
publication-disabled control plane and has not granted publication capability to the Phase 6
runtime. Etsy publication is part of the final product, but it belongs to Phase 7 and remains
disabled until separately authorized, deployed, and accepted.
