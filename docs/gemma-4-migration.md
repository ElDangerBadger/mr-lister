# Gemma 4 migration plan

## Current boundary

After reviewing the fresh-image results, the user accepted this milestone as an editable draft
generator and explicitly authorized freezing, merging, pushing and deploying it. The release
selects Gemma 4 31B with the frozen v3 evidence/image-aware-writing harness and the **standard**
750,000-byte inspection PNG allowance. The higher-detail experiment is not selected. This is a
product acceptance decision for human-reviewed drafts, not a claim that the semantic defects
below disappeared or that schema validation proves image accuracy.

Production activation must still pass runtime integration, packaging, narrow IAM, immutable
endpoint and deployment verification. The account/store-setup preview remains on its separate
branch; judge retirement is a later milestone. No account-plan upgrade is needed based on the
successful development inference. See the release record for the verified deployed version;
the historical observations below do not themselves constitute deployment evidence.

### Historical baseline before production integration

The released worker uses `google.gemma-3-27b-it` through Bedrock Converse. It performs artwork
inspection followed by listing drafting; Nova 2 Lite remains the separate controller. The
candidate uses `google.gemma-4-31b` through Bedrock Mantle in `us-west-2`, with AWS role/profile
credentials and SigV4. It retains the existing application-owned analysis/listing contracts,
prompts, tag selection, bounded repair, private diagnostics, and human approval boundary.
`google.gemma-4-26b-a4b` is an optional later latency comparison, not another default.

Existing JSON configurations are unchanged and default to `transport: converse`. The production
composition explicitly requires Converse, the existing Gemma 3 identity/configuration, and its
exact file fingerprint. The source bundle still packages the Gemma 3 config. No candidate config
is selected by the deployed application.

### Readiness and evaluation evidence — October 4, 2026

- The renewed bootstrap session works. `freetier:GetAccountPlanState` reports an active Free
  account plan. No billing upgrade was performed.
- A SigV4-signed `GET /v1/models/google.gemma-4-31b` to the Oregon Mantle endpoint returned
  HTTP 200 with `status: available`. This verifies model discovery and signing for that read
  operation, not permission to invoke the model or acceptance of the candidate inference schema.
- `bedrock:GetFoundationModelAvailability` rejected this Mantle-only model identifier. That
  control-plane error is not evidence of a paid-plan requirement; use Mantle model discovery.
- The development login was renewed and verified as the expected non-root IAM user. AWS Access
  Analyzer returned no findings for the narrow candidate policy. The IAM user's existing inline
  policy size limit prevented adding another inline policy, so a dedicated managed policy was
  attached only to this user, restricted to Gemma 4 31B in Oregon and expiring after two hours.
  Its contents were read back and its effective inference permission verified before testing.
- All inference used that development identity. A one-image, two-call native JSON-schema canary
  passed in 12.81 seconds with no repair and stopped at human approval in fake commerce.
  Subsequent comparison and configuration checks also completed without a paid-plan rejection.
  Gemma 4 inference therefore works on this account's current Free plan; this does not establish
  an entitlement for every AWS account, future invocation, or other model.
- After evaluation, the temporary managed policy was detached and deleted. Readback confirmed
  no attachment and `NoSuchEntity` for the policy. No persistent evaluation permission remains.

### Initial qualification outcome before the user's draft-milestone acceptance

Both models used the same eleven frozen images, released SEO prompt, two-call workflow and one
trial per image. Every result passed typed validation and stopped at human approval; all commerce
was simulated. The semantic quality checks distinguish valid JSON from correct artwork interpretation.

| Model | Existing quality checks passed | Median measured intelligence time | Input / output tokens |
| --- | --- | --- | --- |
| Gemma 3 27B | 10 / 11 | 9.74 seconds | 21,878 / 8,761 |
| Gemma 4 31B, initial candidate | 9 / 11 | 11.47 seconds | 26,310 / 6,449 |

These are descriptive single-trial observations, not statistical performance claims or complete
upload-to-review timings. Mantle does not supply the provider latency field consumed by the legacy
score collector; its `score.latency_ms=0` is missing telemetry, not zero latency. Use
`intelligence_execution.intelligence_wall_clock_ms` for this comparison.

Assistant visual review found a material subject regression: Gemma 4 described the stylized seahorse as a
bird and carried that identity into its title and tags. It also missed the pencil/wrench subjects
in the maker graphic and described the textured badger illustration as vector/cel-shaded art.
The mountain/wave title failed a literal-word rubric check while retaining the main visual
subjects; that is weaker evidence than the seahorse error. Gemma 3 also has defects: it describes
the inspection checkerboard as printed artwork on transparent fixtures, which Gemma 4 avoids.
Decoded image bytes and user prompts were identical between transports for the examined cases,
so the local rendition step does not explain the regressions.

Two bounded follow-ups each inspected the same three difficult/control images without changing
their prompts or leaking fixture labels: explicit `reasoning_effort=high`, then separately AWS's
recommended `temperature=1.0, top_p=0.95`. Neither resolved the seahorse identity or missing tools.
The reasoning run took 34.73, 51.26 and 12.49 seconds for inspection alone; raising effort is not
an established quality or latency fix here. These experimental settings are not in the candidate
configuration and did not touch production.

Before promotion, improve general subject interpretation and calibrated uncertainty, test another
Gemma 4 variant if warranted, and evaluate fresh artwork beyond the now-observed eleven fixtures.
Do not weaken the rubric, insert fixture answers into prompts, or promote solely because the API
accepts a request. The blocker is qualification quality, not a demonstrated paid-plan requirement.

The subsequent [ordered harness experiment](gemma-4-harness-experiments.md) tested checked-fact
writer A/B before the full evidence-brief/image-aware-writer flow. It improved the seahorse case
to explicit review-required abstention, but did not recover missing tools/lantern detail and
produced more generic copy in several cases. That candidate remains experimental too. Fresh
artwork qualification and production promotion have not passed; the original baseline evidence
above is retained rather than replaced by the changed-prompt results.

[Harness v2](gemma-4-harness-v2-experiment.md) subsequently improved the writing comparison
(five of six blinded assistant preferences) and produced useful drafts from a new user photo
in three trials. Visual omissions, unreported ambiguity and provenance/OCR overclaiming remain.
The v2 prompts are opt-in; production is still unchanged and qualification is not complete.

[Harness v3](gemma-4-harness-v3-experiment.md) incorporates the user's thematic-tag feedback
and distinguishes unknown origin from uncertain visual content. Its twenty-case retained
regression run restores the linked-circle listing and improves some details, but confidently
mislabels the seahorse and adds incorrect material/direction claims to the marching figures.
All drafts were structurally valid. At that experimental checkpoint the semantic regressions
blocked promotion and no production model or application setting had changed. The later user
acceptance above explicitly recognizes the draft-only workload and retained review boundary;
it does not relabel those regression results as passes.

The subsequent [image-detail experiment](gemma-4-image-detail-experiment.md) ran five fresh user
images plus a known regression twice under each image budget. Larger images did not reliably
improve accuracy. The user accepted the strong single-subject results as a useful progress point,
with lettering and complex-scene limitations documented, and requested the standard-budget v3
release. No further prompt tuning is part of this freeze.

Private evidence remains under `.mr_lister_private/evaluation-results/` in these runs:
`gemma4-31b-canary-20261004T1610`, `gemma4-31b-comparison-20261004-a`,
`gemma3-27b-baseline-20261004-a`, `gemma4-reasoning-inspection-20261004-a`, and
`gemma4-sampling-inspection-20261004-a`. Do not commit raw accepted outputs or private AWS captures.

The current deployment was read back without alteration: AgentCore version 8 and its
`phase6_v8_dev` endpoint are READY, and the preparation Lambda/runtime release, binding and
exact-version S3 archive checksums agree. The shared API retains 18 routes and 18 integrations,
including publication and history clearing. The runtime role still grants Gemma 3/Nova inference
only. Captures are in `.mr_lister_private/gemma4-readiness-20261004/current-release/`.

## Stage 1: candidate, qualification, and official migration

1. **Readiness:** confirm actual account eligibility, requested region, quota, and the exact AWS
   retirement notice. Account upgrade is a separate deliberate action; no billing change is
   necessary to prepare the candidate locally. Confirm authentication with the authorized
   deployment profile, but retain the existing `mr-lister-dev` evaluator identity gate.
2. **Offline boundary:** test SigV4 requests, HTTP failures/timeouts, bounded retries, finish
   reasons, schema/typed validation and repair exhaustion, image integrity/request byte limits,
   and safe diagnostics. Each inspection or listing operation has a shared 300-second deadline
   across its bounded application repairs. Each request uses one HTTP attempt with no hidden
   transport retries, a 10-second connection timeout, and the remaining operation budget for
   response reading. The deadline is checked before and after calls; socket timeouts do not
   provide hard wall-clock cancellation of a slowly streaming peer. Offline verification used
   botocore 1.43.73; verify per-request timeout support in the exact deployment artifact.
   The candidate signing and schema request passed the canary; deployed runtime compatibility
   and semantic qualification remain open. Only an inspection
   rendition may shrink: original print artwork and its checksum remain untouched.
   A 3.5 MB total request ceiling includes base64 expansion,
   JSON, prompts, schema and repair history; a per-image limit alone is insufficient.
3. **One-image live canary:** after a reviewed narrow grant and authorization for inference,
   run the current two-call workflow against one original fixture, using fake commerce only.
   Confirm that Mantle accepts the exact JSON-schema request and that valid final JSON, image
   interpretation, usage metadata, and termination semantics survive our adapter. The candidate
   requests `native_json_schema`; the October 4 canary confirmed request acceptance and valid output.
   Never silently downgrade to unconstrained output; document/review any necessary protocol
   change while retaining application validation.
4. **Representative comparison:** run the eleven existing original fixtures, then fresh images
   covering small text, pale transparency, clutter, abstract art, and prompt injection. The
   historical holdouts have already been opened. Compare Gemma 3 and 4 with the same current
   SEO prompt, fixture set and trial count; keep the one-call optimization out of this change.
   Review semantic grounding alongside deterministic contract/tag checks. Record cold/warm
   wall clock, model failures and repairs, usage and per-listing cost. Do not claim end-to-end
   speed from the model-only harness: Nova, Printify and mockups add work afterward.

The commands and opt-in cost switches are in [evaluation instructions](../tests/evaluation/README.md#gemma-4-candidate-not-activated).
The evaluator requires `mr-lister-dev`, uses `FakeProductionAdapter`, disables publication in its
synthetic product profile, and asserts the human-approval stop. Score artifacts and diagnostics
stay in ignored `.mr_lister_private` paths; do not commit raw outputs, credentials or artwork.

The candidate IAM example is
[`bedrock-google-gemma-4-31b-candidate-policy.json`](../infra/iam/bedrock-google-gemma-4-31b-candidate-policy.json).
It is **not attached or rendered into production**. Mantle inference uses resource `*` with
`bedrock-mantle:Model` and `aws:RequestedRegion` conditions because this API does not accept a
foundation-model ARN in that resource field. The example grants only candidate inference, not
IAM changes, bearer-token calls, model listing, production data or commerce capability. Other
identity prerequisites must be reviewed separately; do not broaden the example to fix access
errors blindly.

### Promotion criteria

- No new deterministic contract, publication-boundary or owner-isolation failures.
- Existing case quality floors pass, with human review of grounding and no unacceptable
  hallucination/regression on fresh images. Better benchmark scores alone do not establish
  better listing copy.
- A documented latency/cost tradeoff supported by real calls, including failed/repaired calls;
  no promise that a larger model improves first-run speed.
- Compatible runtime/dependency artifacts, reviewed narrow IAM, a valid immutable endpoint
  binding and a tested current-schema rollback exist before seller traffic moves.

### Official deployment, cutover, and rollback

Build the candidate into a new immutable AgentCore runtime version and custom endpoint. Read back
its exact archive, configuration, role and binding. Preserve the currently working Gemma 3
endpoint and current-compatible release as the fallback while the old model remains available.
Use a controlled unpublished test job before directing normal new preparation jobs to the new
version. Verify normal seller and judge entry, artwork preview, saved edits/pricing, and the
explicit approval/publication boundaries. Model qualification itself requires no live publish.

Plan how active preparation jobs and retries cross the cutover: drain them or bind them to the
original runtime version. Do not regenerate approved reviews, replay provider writes or invent
new publication authority. Retain source, owner, shop/product and approval fingerprints. Record
model/prompt identity and metrics without logging artwork or credentials.

Rollback redirects new preparation to the captured current-compatible release and verifies its
permissions/bindings. It does not restore an arbitrary historical `main`: pricing records now
require compatible readers. Preserve the current frontend and persisted contracts. Do not
reimport the shared Phase 6 API; earlier reimport removed separately owned Phase 7 routes.
Read back the complete route/integration inventory. Runtime rollback never undoes a product or
listing already created; reconcile those through existing guarded processes. Gemma 3 rollback
ceases to be an option after its service retirement.

## Stage 2: retire judge access, then resume accounts and store setup

This stage begins only after the official model migration has passed its live review:

1. Retire the public judge entry and session broker, stop issuing judge sessions, and revoke the
   dedicated judge account's sessions/refresh credential and its narrow grants. Preserve the
   owner's MFA/session and Printify credentials. Remove the stored judge secret after dependent
   services have been disabled and verified; do not leave a hidden public entry path.
2. Finish or explicitly reconcile pending 30-minute judge cleanup jobs before retiring their
   worker/schedule/permissions. Confirm there are no orphaned judge products or active cleanup
   records; do not delete seller-owned products or terminate cleanup prematurely.
3. Resume the reviewed account/store-setup work: verified server-side account identity,
   per-account encrypted Printify credentials, authorized shop selection, and immutable
   connection/shop ownership for each job through retries, review and publication. New accounts
   never inherit the owner store or judge privileges. Registration/provider approval details
   remain their own milestone, not a side effect of switching models. Google/Apple login,
   customer billing, and product catalog expansion remain separate scope.

These later actions require their own concrete review and verification; Stage 1 changes none of
them. Existing release constraints:
[pricing rollback](listing-pricing-release-state.md#rollback-constraints-and-evidence),
[shared API routes](recent-history-release-state.md),
[immutable AgentCore binding](../src/mr_lister/agent/runtime_binding.py).

## AWS references checked October 4, 2026

- [Gemma 4 31B model card](https://docs.aws.amazon.com/bedrock/latest/userguide/model-card-google-gemma-4-31b.html)
- [Mantle endpoints/authentication](https://docs.aws.amazon.com/bedrock/latest/userguide/endpoints.html)
- [Mantle model-scoped IAM example](https://docs.aws.amazon.com/bedrock/latest/userguide/bedrock-managed-agents-openai-security.html)

Availability documentation alone does not prove this account has permission or quota. The
recorded canary establishes development access and contract compatibility. Production acceptance
now follows the explicitly authorized draft milestone and the deployment checks above, while
preserving the recorded quality limitations.
