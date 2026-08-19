from __future__ import annotations

from hashlib import sha256
from pathlib import Path

import pytest

from mr_lister.contracts import ArtworkAnalysis, ListingIntelligence
from mr_lister.intelligence.prompts import PROMPT_VERSION
from tools.phase2_evaluation import (
    REQUIRED_CASE_CATEGORIES,
    REQUIRED_METRICS,
    load_manifest,
    quality_failures,
    score_case,
    summarize_score_documents,
)

MANIFEST = Path(__file__).with_name("manifest.json")


def test_manifest_covers_phase_2_acceptance_categories_and_metrics() -> None:
    manifest = load_manifest(MANIFEST)

    assert {case.category for case in manifest.cases} == REQUIRED_CASE_CATEGORIES
    assert manifest.prompt_version == PROMPT_VERSION
    assert manifest.metrics >= REQUIRED_METRICS
    assert len({case.case_id for case in manifest.cases}) == 8
    assert {case.split for case in manifest.cases} == {"calibration", "regression", "holdout"}
    assert all(case.asset.parent == MANIFEST.parent / "assets" for case in manifest.cases)
    assert all(
        sha256(case.asset.read_bytes()).hexdigest() == case.asset_sha256 for case in manifest.cases
    )


def test_offline_score_combines_grounding_contract_and_safe_telemetry() -> None:
    case = load_manifest(MANIFEST).cases[0]
    analysis = ArtworkAnalysis(
        subject="A badger explorer holding a compass in a forest",
        styles=("storybook illustration",),
        themes=("woodland adventure",),
        confidence=0.93,
    )
    listing = ListingIntelligence(
        title="Badger Explorer Forest Graphic Tee",
        description="A woodland adventure illustration prepared for human review.",
        tags=(
            "badger shirt",
            "adventure tee",
            "woodland art",
            "forest animal",
            "nature lover",
            "explorer design",
            "rustic gift",
            "wildlife graphic",
            "unisex apparel",
            "storybook style",
            "outdoor motif",
            "modern creature",
            "unique illustration",
        ),
        audience=("wildlife art fans",),
        title_rationale="Names the subject, setting, style, and product.",
        tag_rationale="Covers subject, style, setting, product, and audience.",
    )
    diagnostics = (
        {
            "status": "invalid_output",
            "latency_ms": 100,
            "usage": {"inputTokens": 20, "outputTokens": 5, "totalTokens": 25},
        },
        {
            "status": "accepted",
            "latency_ms": 150.5,
            "usage": {"inputTokens": 30, "outputTokens": 10, "totalTokens": 40},
        },
    )

    result = score_case(case, analysis=analysis, listing=listing, diagnostics=diagnostics)

    assert result["contract_pass"] is True
    assert result["repair_attempts"] == 1
    assert result["visual_anchor_recall"] == 1.0
    assert result["title_specificity"] == 1.0
    assert result["tag_relevance"] == 1.0
    assert result["tag_diversity"] == 1.0
    assert result["tag_keyword_reuse_count"] == 0
    assert result["latency_ms"] == 250.5
    assert result["input_tokens"] == 50
    assert result["output_tokens"] == 15
    assert result["total_tokens"] == 65
    assert quality_failures(result) == ()


def test_quality_floor_rejects_stale_generic_evaluation_output() -> None:
    failures = quality_failures(
        {
            "contract_pass": True,
            "repair_attempts": 0,
            "visual_anchor_recall": 0.0,
            "visible_text_recall": 0.0,
            "title_specificity": 0.0,
            "tag_relevance": 0.0,
            "tag_diversity": 1.0,
            "tag_keyword_reuse_count": 0,
        }
    )

    assert "visual_anchor_recall must be at least 0.5" in failures
    assert "tag_relevance must be at least 0.3333" in failures


def test_concept_aliases_count_once_without_lowering_the_typography_floor() -> None:
    case = load_manifest(MANIFEST).cases[1]
    analysis = ArtworkAnalysis(
        subject="A bold typographic motivational quote",
        visible_text=("MAKE GOOD THINGS",),
        confidence=0.9,
    )
    listing = ListingIntelligence(
        title="Make Good Things Graphic Tee",
        description="A motivational design prepared for human review.",
        tags=(
            "design motif",
            "positive message",
            "bold statement",
            "inspiring apparel",
            "uplifting gift",
            "optimist style",
            "daily encouragement",
            "good things tee",
            "thoughtful present",
            "modern graphic",
            "joyful clothing",
            "hopeful mindset",
            "wearable words",
        ),
        title_rationale="Uses the exact visible phrase.",
        tag_rationale="Uses diverse design and motivation concepts.",
    )

    result = score_case(case, analysis=analysis, listing=listing)

    assert result["visual_anchor_recall"] == 0.5
    assert result["tag_relevance"] == 0.3333
    assert result["tag_keyword_reuse_count"] == 0
    assert quality_failures(result) == ()


def test_score_artifact_summary_keeps_runs_and_models_separate() -> None:
    base_score = {
        "contract_pass": True,
        "repair_attempts": 0,
        "visual_anchor_recall": 1.0,
        "visible_text_recall": 1.0,
        "title_specificity": 1.0,
        "tag_relevance": 1.0,
        "tag_diversity": 1.0,
        "tag_keyword_reuse_count": 0,
        "latency_ms": 100,
        "input_tokens": 20,
        "output_tokens": 10,
        "total_tokens": 30,
    }
    documents = (
        {"run_id": "nova-run", "model_id": "nova", "score": base_score},
        {
            "run_id": "nova-run",
            "model_id": "nova",
            "score": {**base_score, "latency_ms": 200},
        },
        {"run_id": "claude-run", "model_id": "claude", "score": base_score},
    )

    summaries = summarize_score_documents(documents)

    assert [(item["run_id"], item["model_id"]) for item in summaries] == [
        ("claude-run", "claude"),
        ("nova-run", "nova"),
    ]
    assert summaries[1]["score_count"] == 2
    assert summaries[1]["pass_rate"] == 1.0
    assert summaries[1]["averages"]["latency_ms"] == 150.0


def test_manifest_rejects_asset_path_escape(tmp_path: Path) -> None:
    payload = MANIFEST.read_text(encoding="utf-8").replace(
        "assets/illustrated_badger_subject.png", "../outside.png"
    )
    candidate = tmp_path / "manifest.json"
    candidate.write_text(payload, encoding="utf-8")

    with pytest.raises(ValueError, match="escapes"):
        load_manifest(candidate)
