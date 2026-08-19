# Phase 2 intelligence evaluation

This directory defines Mr Lister's small, repository-owned acceptance set. Its eight cases span
calibration, regression, and frozen holdout splits. They exercise illustrated subjects,
typography, ambiguous abstract art, pale artwork on transparency, and visible prompt injection.
The rubric separates deterministic contract checks from subjective grounding signals and
provider telemetry.

All eight PNGs are original project fixtures; never replace them with active marketplace
listings. Three were created with the built-in ImageGen tool, and five are reproducibly generated
by `tools/generate_phase2_text_assets.py`. The three `holdout_` cases were frozen after prompt
version `2026-08-18.5` was exercised and must not be used to tune that version before its first live
holdout run. Check readiness without contacting AWS:

```shell
python -m tools.phase2_evaluation
python -m tools.phase2_evaluation --check-assets
```

The live canary is disabled unless `MR_LISTER_RUN_LIVE_BEDROCK=1` is present. It uses
the normal AWS credential chain (including `AWS_PROFILE`) and defaults to the Nova 2 Lite
development configuration. It sends the accepted result through the complete listing workflow.
Production remains `FakeProductionAdapter`, the product profile has publication disabled, and
the canary asserts that the job stops at human approval.

```shell
MR_LISTER_RUN_LIVE_BEDROCK=1 \
  AWS_PROFILE=mr-lister-dev \
  .venv/bin/python -m pytest -m live_bedrock -s tests/evaluation/test_live_bedrock.py
```

Set `MR_LISTER_BEDROCK_CONFIG=config/bedrock/claude_sonnet_4_6.json` only for an explicitly
cost-bearing Claude benchmark after its Marketplace prerequisites have been accepted. Ordinary
development should retain the Nova default.

Every live run writes redacted, permission-restricted diagnostics under the gitignored
`.mr_lister_private/bedrock-live/` directory. For a deliberately authorized troubleshooting run,
set `MR_LISTER_CAPTURE_RAW_BEDROCK=1` to retain raw model responses in that private directory.
Never commit or paste those artifacts into an issue, log, or public chat.

That permission/model canary has two logical stages: artwork inspection and listing drafting.
The Nova profile may make up to two semantic-repair requests per stage when validation or the
tag-diversity target fails, and the AWS SDK may separately retry transient failures. Run all eight
cases only with a second explicit cost switch:

```shell
MR_LISTER_RUN_LIVE_BEDROCK=1 \
MR_LISTER_RUN_FULL_BEDROCK_EVAL=1 \
AWS_PROFILE=mr-lister-dev \
  .venv/bin/python -m pytest -m live_bedrock -s tests/evaluation/test_live_bedrock.py
```

Use `MR_LISTER_EVAL_SPLIT=calibration`, `regression`, or `holdout` to run one split. Set
`MR_LISTER_EVAL_TRIALS` from 1 through 3 to measure stability, and give comparable runs a stable,
filesystem-safe `MR_LISTER_EVAL_RUN_ID`. `MR_LISTER_EVAL_CASE` can select one exact manifest case
without rerunning the rest of its split. For example, the first untouched holdout baseline is:

```shell
MR_LISTER_RUN_LIVE_BEDROCK=1 \
MR_LISTER_RUN_FULL_BEDROCK_EVAL=1 \
MR_LISTER_EVAL_SPLIT=holdout \
MR_LISTER_EVAL_TRIALS=3 \
MR_LISTER_EVAL_RUN_ID=nova-v5-holdout \
AWS_PROFILE=mr-lister-dev \
  .venv/bin/python -m pytest -m live_bedrock -s tests/evaluation/test_live_bedrock.py
```

Each accepted score and its validated synthetic-evaluation contracts are written under the gitignored
`.mr_lister_private/evaluation-results/<run-id>/` directory. Compare provider or repeated-trial
summaries without contacting AWS:

```shell
.venv/bin/python -m tools.compare_phase2_evaluations
```

For a deliberately cost-bearing Claude comparison, select
`MR_LISTER_BEDROCK_CONFIG=config/bedrock/claude_sonnet_4_6.json` and use a distinct run ID. Keep the
same cases, prompt version, split, and trial count so the comparison is meaningful.

Running ordinary `pytest` collects this file but skips every live invocation. A live
run fails clearly if any required asset is missing. The printed JSON lines are ephemeral
review evidence; redirect them only into the gitignored `.mr_lister_private/` directory
if retention is needed.

Every live case must pass the explicit Phase 2 quality floor: all case-specific title terms, at
least half of the expected visual and visible-text signals, at least one-third of expected tag
concepts, zero repeated meaningful tag keywords, valid contracts, and no more than two semantic
repairs across both stages. Visual anchors and tag concepts may define reviewed synonym groups;
one matching alias credits one concept without changing the denominator or threshold. Phase 2
v5 closes only after the full eight-case run passes; the one-case canary proves access and request
wiring but not representative quality. Exact duplicate tags remain a hard contract failure.
Repeated normalized keywords across otherwise distinct tags fail deterministic acceptance and the
evaluation floor, not the schema: a reviewer may keep justified overlap without replacing
it with irrelevant filler.
