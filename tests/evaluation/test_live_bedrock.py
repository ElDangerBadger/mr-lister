"""Explicitly opt-in Bedrock-to-workflow canary.

Module imports and ordinary pytest collection make no AWS calls.  The adapter and AWS
session are created only inside the environment-gated test body.
"""

from __future__ import annotations

import json
import os
import re
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path

import boto3
import pytest

from mr_lister.contracts import JobState
from tools.phase2_evaluation import EVALUATION_SPLITS, load_manifest, quality_failures, score_case

MANIFEST = Path(__file__).with_name("manifest.json")
LIVE_BEDROCK_ENABLED = os.getenv("MR_LISTER_RUN_LIVE_BEDROCK") == "1"
FULL_EVALUATION_ENABLED = os.getenv("MR_LISTER_RUN_FULL_BEDROCK_EVAL") == "1"
EVALUATION_TRIALS = int(os.getenv("MR_LISTER_EVAL_TRIALS", "1"))
if not 1 <= EVALUATION_TRIALS <= 3:
    raise ValueError("MR_LISTER_EVAL_TRIALS must be between 1 and 3")
EVALUATION_SPLIT = os.getenv("MR_LISTER_EVAL_SPLIT")
if EVALUATION_SPLIT is not None and EVALUATION_SPLIT not in EVALUATION_SPLITS:
    raise ValueError(f"MR_LISTER_EVAL_SPLIT must be one of {sorted(EVALUATION_SPLITS)}")
EVALUATION_RUN_ID = os.getenv("MR_LISTER_EVAL_RUN_ID") or datetime.now(UTC).strftime(
    "eval-%Y%m%dT%H%M%SZ"
)
if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}", EVALUATION_RUN_ID) is None:
    raise ValueError("MR_LISTER_EVAL_RUN_ID must be a safe 1-100 character identifier")
CASES = load_manifest(MANIFEST).cases
EVALUATION_CASE_ID = os.getenv("MR_LISTER_EVAL_CASE")
if EVALUATION_CASE_ID is not None and EVALUATION_CASE_ID not in {case.case_id for case in CASES}:
    raise ValueError("MR_LISTER_EVAL_CASE must name a case in the evaluation manifest")

pytestmark = [
    pytest.mark.live_bedrock,
    pytest.mark.skipif(
        not LIVE_BEDROCK_ENABLED,
        reason="set MR_LISTER_RUN_LIVE_BEDROCK=1 to permit AWS calls",
    ),
]


@pytest.mark.parametrize("case_index", range(len(CASES)), ids=[case.case_id for case in CASES])
@pytest.mark.parametrize("trial_index", range(EVALUATION_TRIALS))
def test_bedrock_evaluation_cases_reach_human_approval_with_fake_production(
    case_index: int,
    trial_index: int,
    tmp_path: Path,
) -> None:
    from mr_lister.intelligence.bedrock import build_bedrock_adapter
    from mr_lister.intelligence.diagnostics import (
        CompositeDiagnosticSink,
        FilesystemDiagnosticSink,
        InMemoryDiagnosticSink,
    )
    from mr_lister.intelligence.prompts import PROMPT_VERSION
    from mr_lister.intelligence.settings import BedrockSettings
    from mr_lister.workflow.fakes import FakeProductionAdapter
    from mr_lister.workflow.profiles import ProductProfileRepository
    from mr_lister.workflow.service import ListingWorkflow
    from mr_lister.workflow.store import InMemoryJobStore

    manifest = load_manifest(MANIFEST)
    assert manifest.prompt_version == PROMPT_VERSION
    missing = [case.asset for case in manifest.cases if not case.asset.is_file()]
    assert not missing, "Missing original evaluation assets: " + ", ".join(map(str, missing))
    mismatched = [
        case.asset
        for case in manifest.cases
        if sha256(case.asset.read_bytes()).hexdigest() != case.asset_sha256
    ]
    assert not mismatched, "Changed evaluation assets: " + ", ".join(map(str, mismatched))
    case = manifest.cases[case_index]
    if EVALUATION_SPLIT is not None and case.split != EVALUATION_SPLIT:
        pytest.skip(f"case belongs to the {case.split} split")
    if EVALUATION_CASE_ID is not None and case.case_id != EVALUATION_CASE_ID:
        pytest.skip(f"case does not match MR_LISTER_EVAL_CASE={EVALUATION_CASE_ID}")
    if (case_index or trial_index) and not FULL_EVALUATION_ENABLED:
        pytest.skip("set MR_LISTER_RUN_FULL_BEDROCK_EVAL=1 for the full evaluation set")

    config_path = Path(os.getenv("MR_LISTER_BEDROCK_CONFIG", "config/bedrock/nova_2_lite.json"))
    settings = BedrockSettings.model_validate_json(config_path.read_text(encoding="utf-8"))
    assert os.getenv("AWS_PROFILE") == "mr-lister-dev"
    session = boto3.Session(profile_name="mr-lister-dev", region_name=settings.region)
    caller = session.client("sts", region_name=settings.region).get_caller_identity()
    caller_arn = str(caller.get("Arn", ""))
    assert caller_arn.endswith(":user/mr-lister-dev")
    assert not caller_arn.endswith(":root")

    diagnostics = InMemoryDiagnosticSink()
    private_diagnostics = FilesystemDiagnosticSink(
        Path(".mr_lister_private/bedrock-live"),
        include_raw_output=os.getenv("MR_LISTER_CAPTURE_RAW_BEDROCK") == "1",
    )
    production = FakeProductionAdapter()
    configured_profiles = ProductProfileRepository(Path("config/product_profiles"))
    safe_profile = configured_profiles.get("synthetic_gildan_5000").model_copy(
        update={"publish_enabled": False}
    )
    safe_profile_directory = tmp_path / "product_profiles"
    safe_profile_directory.mkdir()
    (safe_profile_directory / "synthetic_gildan_5000.json").write_text(
        safe_profile.model_dump_json(indent=2),
        encoding="utf-8",
    )
    profiles = ProductProfileRepository(safe_profile_directory)
    assert profiles.get("synthetic_gildan_5000").publish_enabled is False
    workflow = ListingWorkflow(
        store=InMemoryJobStore(),
        profiles=profiles,
        intelligence=build_bedrock_adapter(
            settings,
            session=session,
            diagnostics=CompositeDiagnosticSink(diagnostics, private_diagnostics),
        ),
        production=production,
        job_id_factory=lambda: f"job_eval_{case.case_id}_trial_{trial_index + 1}",
    )
    content = case.asset.read_bytes()
    job = workflow.submit(
        filename=case.asset.name,
        content_type="image/png",
        content=content,
        idempotency_key=(
            f"eval:{manifest.prompt_version}:{settings.model_id}:{case.case_id}:{trial_index + 1}"
        ),
        profile_id="synthetic_gildan_5000",
    )
    review = workflow.get_review(job.job_id)

    assert review.profile.publish_enabled is False
    assert production.publish_calls == 0
    score = score_case(
        case,
        analysis=review.artwork_analysis,
        listing=review.listing,
        diagnostics=diagnostics.records,
    )
    score_artifact = {
        "run_id": EVALUATION_RUN_ID,
        "model_id": settings.model_id,
        "prompt_version": manifest.prompt_version,
        "split": case.split,
        "trial": trial_index + 1,
        "workflow": {
            "state": job.state,
            "validation_passed": review.validation.passed,
            "validation_issue_codes": [issue.code for issue in review.validation.issues],
            "production_create_calls": production.create_calls,
            "production_publish_calls": production.publish_calls,
        },
        "model_settings": {
            "output_mode": settings.output_mode,
            "temperature": settings.temperature,
            "max_tokens": settings.max_tokens,
            "max_repair_attempts": settings.max_repair_attempts,
        },
        "score": score,
        "accepted_output": {
            "analysis": review.artwork_analysis.model_dump(mode="json"),
            "listing": review.listing.model_dump(mode="json"),
        },
    }
    _write_score_artifact(score_artifact, case.case_id, trial_index + 1)
    print(json.dumps(score_artifact, sort_keys=True))
    assert job.state is JobState.AWAITING_APPROVAL
    assert review.validation.passed is True
    assert production.create_calls == 1
    assert not quality_failures(score), quality_failures(score)


def _write_score_artifact(document: dict, case_id: str, trial: int) -> None:
    directory = Path(".mr_lister_private/evaluation-results") / EVALUATION_RUN_ID
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    if os.name == "posix":
        directory.chmod(0o700)
    destination = directory / f"{case_id}-trial-{trial}.json"
    with destination.open("x", encoding="utf-8") as artifact:
        json.dump(document, artifact, indent=2, sort_keys=True)
        artifact.write("\n")
    if os.name == "posix":
        destination.chmod(0o600)
