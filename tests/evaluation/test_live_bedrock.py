"""Explicitly opt-in Bedrock-to-workflow canary.

Module imports and ordinary pytest collection make no AWS calls.  The adapter and AWS
session are created only inside the environment-gated test body.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from time import perf_counter
from typing import Any

import boto3
import pytest

from mr_lister.contracts import ArtworkAnalysis, JobState, ListingIntelligence
from mr_lister.workflow.models import ArtworkInput
from mr_lister.workflow.ports import IntelligencePort
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
EVALUATION_PROMPT_VERSION = os.getenv("MR_LISTER_EVAL_PROMPT_VERSION", "2026-08-18.7")
EVALUATION_EXECUTION = os.getenv("MR_LISTER_EVAL_EXECUTION", "two_call")
if EVALUATION_EXECUTION not in {"two_call", "one_call"}:
    raise ValueError("MR_LISTER_EVAL_EXECUTION must be two_call or one_call")

pytestmark = [
    pytest.mark.live_bedrock,
    pytest.mark.skipif(
        not LIVE_BEDROCK_ENABLED,
        reason="set MR_LISTER_RUN_LIVE_BEDROCK=1 to permit AWS calls",
    ),
]


class _EvaluationIntelligence:
    """Time the existing port, or adapt one real unified result to its two methods."""

    def __init__(
        self,
        *,
        delegate: IntelligencePort | None = None,
        agent: Any = None,
        transport_prompt_fingerprint: str | None = None,
    ) -> None:
        if (delegate is None) == (agent is None):
            raise ValueError("Evaluation requires exactly one intelligence execution path")
        self.delegate = delegate
        self.agent = agent
        self.transport_prompt_fingerprint = transport_prompt_fingerprint
        self.wall_clock_ms = 0.0
        self.prepared: tuple[ArtworkInput, ArtworkAnalysis, ListingIntelligence] | None = None

    def inspect_artwork(self, artwork: ArtworkInput, content: bytes) -> ArtworkAnalysis:
        started = perf_counter()
        try:
            if self.delegate is not None:
                return self.delegate.inspect_artwork(artwork, content)
            from mr_lister.intelligence.unified import prepare_unified_review

            if self.prepared is not None or sha256(content).hexdigest() != artwork.content_sha256:
                raise ValueError("Unified evaluation source changed or was already inspected")
            analysis, listing = prepare_unified_review(self.agent, artwork, content)
            self.prepared = (artwork, analysis, listing)
            return analysis
        finally:
            self.wall_clock_ms += (perf_counter() - started) * 1_000

    def draft_listing(
        self, artwork: ArtworkInput, content: bytes, analysis: ArtworkAnalysis
    ) -> ListingIntelligence:
        started = perf_counter()
        try:
            if self.delegate is not None:
                return self.delegate.draft_listing(artwork, content, analysis)
            if (
                self.prepared is None
                or artwork != self.prepared[0]
                or sha256(content).hexdigest() != artwork.content_sha256
                or analysis != self.prepared[1]
            ):
                raise ValueError("Unified evaluation listing must match its exact inspection")
            return self.prepared[2]
        finally:
            self.wall_clock_ms += (perf_counter() - started) * 1_000

    def telemetry(self, legacy_diagnostics: list[dict[str, Any]]) -> dict[str, Any]:
        if self.agent is None:
            return {
                "execution": "two_call",
                "intelligence_wall_clock_ms": round(self.wall_clock_ms, 3),
                "model_calls": len(legacy_diagnostics),
                "strands_cycles": None,  # The legacy local workflow does not run Strands.
                "transport_prompt_fingerprint": self.transport_prompt_fingerprint,
            }
        summary = self.agent.event_loop_metrics.get_summary()
        return {
            "execution": "one_call",
            "intelligence_wall_clock_ms": round(self.wall_clock_ms, 3),
            "model_calls": self.agent.model.request_count,
            "strands_cycles": summary["total_cycles"],
            "repair_attempts": self.agent.model.request_count - 1,
            "accumulated_usage": summary["accumulated_usage"],
            "transport_prompt_fingerprint": self.transport_prompt_fingerprint,
            "provider_responses": self.agent.model.response_metadata,
        }


def _one_call_intelligence(
    session: Any, settings: Any, prompt_bundle: Any
) -> _EvaluationIntelligence:
    """Use production's native request boundary; observe only response metadata here."""

    from botocore.config import Config
    from strands import Agent
    from strands.agent.conversation_manager import NullConversationManager

    from mr_lister.intelligence.prompts import ETSY_SEO_RELEASE_PROMPT_BUNDLE
    from mr_lister.intelligence.schema import bedrock_output_schema
    from mr_lister.intelligence.unified import (
        MAX_UNIFIED_OUTPUT_TOKENS,
        UNIFIED_SYSTEM_PROMPT,
        NativeJsonBedrockModel,
        UnifiedArtworkListing,
        unified_review_prompt,
    )

    if prompt_bundle != ETSY_SEO_RELEASE_PROMPT_BUNDLE:
        raise ValueError("One-call evaluation requires the exact released SEO reference")
    if settings.model_id != "google.gemma-3-27b-it" or settings.output_mode != "native_json_schema":
        raise ValueError("One-call evaluation requires the released native Gemma configuration")

    class EvaluationNativeJsonModel(NativeJsonBedrockModel):
        def __init__(self, **kwargs: Any) -> None:
            self.response_metadata: list[dict[str, Any]] = []
            super().__init__(**kwargs)

        def convert_non_streaming_to_streaming(
            self, response: dict[str, Any], **kwargs: Any
        ) -> Iterable[Any]:
            self.response_metadata.append(
                {
                    "latency_ms": response.get("metrics", {}).get("latencyMs"),
                    "usage": dict(response.get("usage", {})),
                }
            )
            yield from super().convert_non_streaming_to_streaming(response, **kwargs)

    model = EvaluationNativeJsonModel(
        boto_session=session,
        boto_client_config=Config(
            connect_timeout=10,
            read_timeout=300,
            retries={"mode": "standard", "max_attempts": 0},
        ),
        model_id=settings.model_id,
        max_tokens=min(settings.max_tokens, MAX_UNIFIED_OUTPUT_TOKENS),
        temperature=settings.temperature,
        streaming=False,
        use_native_token_count=False,
    )
    transport = {
        "system": UNIFIED_SYSTEM_PROMPT,
        "prompt": unified_review_prompt(),
        "repair": prompt_bundle.repair,
        "output_schema": bedrock_output_schema(UnifiedArtworkListing),
    }
    return _EvaluationIntelligence(
        agent=Agent(
            model=model,
            system_prompt=UNIFIED_SYSTEM_PROMPT,
            retry_strategy=None,
            conversation_manager=NullConversationManager(),
            callback_handler=None,
        ),
        transport_prompt_fingerprint=sha256(
            json.dumps(transport, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest(),
    )


def _apply_one_call_telemetry(score: dict[str, Any], telemetry: Mapping[str, Any]) -> None:
    """Use real SDK cumulative usage and the authorized request count for repairs."""

    if telemetry["execution"] != "one_call":
        return
    score["repair_attempts"] = telemetry["repair_attempts"]
    if any(record["latency_ms"] is None for record in telemetry["provider_responses"]):
        score["latency_ms"] = None  # Missing provider timing is unknown, not zero milliseconds.
    usage = telemetry["accumulated_usage"]
    for name, key in (
        ("input_tokens", "inputTokens"),
        ("output_tokens", "outputTokens"),
        ("total_tokens", "totalTokens"),
    ):
        score[name] = usage[key]


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
    from mr_lister.intelligence.prompts import (
        ETSY_SEO_RELEASE_PROMPT_BUNDLE,
        PROMPT_VERSION,
        prompt_bundle_for,
    )
    from mr_lister.intelligence.settings import BedrockSettings
    from mr_lister.workflow.fakes import FakeProductionAdapter
    from mr_lister.workflow.profiles import ProductProfileRepository
    from mr_lister.workflow.service import ListingWorkflow
    from mr_lister.workflow.store import InMemoryJobStore

    manifest = load_manifest(MANIFEST)
    assert manifest.prompt_version == PROMPT_VERSION
    prompt_bundle = prompt_bundle_for(EVALUATION_PROMPT_VERSION)
    if EVALUATION_EXECUTION == "one_call":
        assert prompt_bundle == ETSY_SEO_RELEASE_PROMPT_BUNDLE
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
    intelligence = (
        _one_call_intelligence(session, settings, prompt_bundle)
        if EVALUATION_EXECUTION == "one_call"
        else _EvaluationIntelligence(
            delegate=build_bedrock_adapter(
                settings,
                session=session,
                diagnostics=CompositeDiagnosticSink(diagnostics, private_diagnostics),
                prompt_bundle=prompt_bundle,
            ),
            transport_prompt_fingerprint=prompt_bundle.fingerprint,
        )
    )
    workflow = ListingWorkflow(
        store=InMemoryJobStore(),
        profiles=profiles,
        intelligence=intelligence,
        production=production,
        job_id_factory=lambda: f"job_eval_{case.case_id}_trial_{trial_index + 1}",
    )
    content = case.asset.read_bytes()
    job = workflow.submit(
        filename=case.asset.name,
        content_type="image/png",
        content=content,
        idempotency_key=(
            f"eval:{prompt_bundle.version}:{settings.model_id}:{case.case_id}:{trial_index + 1}"
        ),
        profile_id="synthetic_gildan_5000",
    )
    review = workflow.get_review(job.job_id)

    assert review.profile.publish_enabled is False
    assert production.publish_calls == 0
    telemetry = intelligence.telemetry(diagnostics.records)
    score = score_case(
        case,
        analysis=review.artwork_analysis,
        listing=review.listing,
        diagnostics=telemetry.get("provider_responses", diagnostics.records),
    )
    _apply_one_call_telemetry(score, telemetry)
    score_artifact = {
        "run_id": EVALUATION_RUN_ID,
        "model_id": settings.model_id,
        "prompt_version": prompt_bundle.version,
        "prompt_fingerprint": prompt_bundle.fingerprint,
        "intelligence_execution": telemetry,
        "fixture_baseline_prompt_version": manifest.prompt_version,
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
            "unified_max_model_calls": 2 if EVALUATION_EXECUTION == "one_call" else None,
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
