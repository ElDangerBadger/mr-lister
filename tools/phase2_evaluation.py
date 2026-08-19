"""Offline scoring helpers for the Phase 2 Bedrock evaluation set.

This module never creates an AWS client.  The opt-in live test supplies already
validated application contracts and redacted diagnostics to these helpers.
"""

from __future__ import annotations

import argparse
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Any

from mr_lister.contracts import ArtworkAnalysis, ListingIntelligence
from mr_lister.workflow.validation import tag_keyword_reuse_count

REQUIRED_CASE_CATEGORIES = frozenset(
    {
        "abstract_ambiguous",
        "illustrated_subject",
        "light_art_transparency",
        "typography_heavy",
        "visible_prompt_injection",
    }
)
REQUIRED_METRICS = frozenset(
    {
        "contract_pass",
        "repair_attempts",
        "visual_anchor_recall",
        "visible_text_recall",
        "title_specificity",
        "tag_relevance",
        "tag_diversity",
        "tag_keyword_reuse_count",
        "latency_ms",
        "input_tokens",
        "output_tokens",
        "total_tokens",
    }
)
QUALITY_MINIMUMS = {
    "visual_anchor_recall": 0.5,
    "visible_text_recall": 0.5,
    "title_specificity": 1.0,
    "tag_relevance": 0.3333,
    "tag_diversity": 1.0,
}
QUALITY_MAXIMUMS = {"repair_attempts": 2, "tag_keyword_reuse_count": 0}
type ConceptAliases = tuple[str, ...]
EVALUATION_SPLITS = frozenset({"calibration", "regression", "holdout"})
SUMMARY_METRICS = (
    "repair_attempts",
    "visual_anchor_recall",
    "visible_text_recall",
    "title_specificity",
    "tag_relevance",
    "tag_diversity",
    "tag_keyword_reuse_count",
    "latency_ms",
    "input_tokens",
    "output_tokens",
    "total_tokens",
)


@dataclass(frozen=True, slots=True)
class EvaluationCase:
    case_id: str
    category: str
    split: str
    asset: Path
    asset_sha256: str
    visual_anchors: tuple[ConceptAliases, ...]
    visible_text: tuple[str, ...]
    title_terms: tuple[str, ...]
    tag_concepts: tuple[ConceptAliases, ...]


@dataclass(frozen=True, slots=True)
class EvaluationManifest:
    manifest_version: str
    prompt_version: str
    metrics: frozenset[str]
    cases: tuple[EvaluationCase, ...]


def load_manifest(path: Path) -> EvaluationManifest:
    """Load and strictly validate the repository-owned evaluation manifest."""

    manifest_path = path.resolve()
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("Evaluation manifest must be a JSON object")

    cases_payload = payload.get("cases")
    rubric_payload = payload.get("rubric")
    if not isinstance(cases_payload, list) or not cases_payload:
        raise ValueError("Evaluation manifest must define at least one case")
    if not isinstance(rubric_payload, dict):
        raise ValueError("Evaluation manifest must define a rubric object")

    cases: list[EvaluationCase] = []
    seen_ids: set[str] = set()
    for item in cases_payload:
        if not isinstance(item, dict):
            raise ValueError("Every evaluation case must be a JSON object")
        case_id = _required_string(item, "id")
        if case_id in seen_ids:
            raise ValueError(f"Duplicate evaluation case id: {case_id}")
        seen_ids.add(case_id)

        expected = item.get("expected")
        if not isinstance(expected, dict):
            raise ValueError(f"Evaluation case {case_id} must define expected signals")
        asset = (manifest_path.parent / _required_string(item, "asset")).resolve()
        if manifest_path.parent not in asset.parents:
            raise ValueError(f"Evaluation asset escapes the manifest directory: {case_id}")
        cases.append(
            EvaluationCase(
                case_id=case_id,
                category=_required_string(item, "category"),
                split=_required_split(item),
                asset=asset,
                asset_sha256=_required_sha256(item, "sha256"),
                visual_anchors=_concept_tuple(expected, "visual_anchors"),
                visible_text=_string_tuple(expected, "visible_text"),
                title_terms=_string_tuple(expected, "title_terms"),
                tag_concepts=_concept_tuple(expected, "tag_concepts"),
            )
        )

    metrics = frozenset(str(metric) for metric in rubric_payload)
    missing_categories = REQUIRED_CASE_CATEGORIES - {case.category for case in cases}
    missing_metrics = REQUIRED_METRICS - metrics
    if missing_categories:
        raise ValueError(f"Evaluation manifest is missing categories: {sorted(missing_categories)}")
    if missing_metrics:
        raise ValueError(f"Evaluation rubric is missing metrics: {sorted(missing_metrics)}")

    return EvaluationManifest(
        manifest_version=_required_string(payload, "manifest_version"),
        prompt_version=_required_string(payload, "prompt_version"),
        metrics=metrics,
        cases=tuple(cases),
    )


def score_case(
    case: EvaluationCase,
    *,
    analysis: ArtworkAnalysis,
    listing: ListingIntelligence,
    diagnostics: Sequence[Mapping[str, Any]] = (),
) -> dict[str, int | float | bool | str]:
    """Score one accepted pair of application contracts on a zero-to-one rubric."""

    analysis_text = " ".join(
        (
            analysis.subject,
            *analysis.visual_elements,
            *analysis.styles,
            *analysis.themes,
            *analysis.visible_text,
            *analysis.color_notes,
        )
    )
    tag_text = " ".join(listing.tags)
    normalized_tags = {_normalize(tag) for tag in listing.tags}
    telemetry = summarize_diagnostics(diagnostics)

    return {
        "case_id": case.case_id,
        "contract_pass": True,
        "repair_attempts": telemetry["repair_attempts"],
        "visual_anchor_recall": _concept_recall(case.visual_anchors, analysis_text),
        "visible_text_recall": _phrase_recall(case.visible_text, " ".join(analysis.visible_text)),
        "title_specificity": _phrase_recall(case.title_terms, listing.title),
        "tag_relevance": _concept_recall(case.tag_concepts, tag_text),
        "tag_diversity": round(len(normalized_tags) / len(listing.tags), 4),
        "tag_keyword_reuse_count": tag_keyword_reuse_count(listing.tags),
        "latency_ms": telemetry["latency_ms"],
        "input_tokens": telemetry["input_tokens"],
        "output_tokens": telemetry["output_tokens"],
        "total_tokens": telemetry["total_tokens"],
    }


def summarize_diagnostics(
    diagnostics: Sequence[Mapping[str, Any]],
) -> dict[str, int | float]:
    """Reduce safe Bedrock diagnostics into comparable evaluation telemetry."""

    latency_ms = 0.0
    repairs = 0
    token_totals = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
    token_keys = {
        "input_tokens": ("inputTokens", "input_tokens"),
        "output_tokens": ("outputTokens", "output_tokens"),
        "total_tokens": ("totalTokens", "total_tokens"),
    }
    for record in diagnostics:
        latency = record.get("latency_ms")
        if isinstance(latency, (int, float)) and not isinstance(latency, bool):
            latency_ms += float(latency)
        if record.get("status") == "invalid_output":
            repairs += 1
        usage = record.get("usage")
        if not isinstance(usage, Mapping):
            continue
        for metric, aliases in token_keys.items():
            for alias in aliases:
                value = usage.get(alias)
                if isinstance(value, int) and not isinstance(value, bool):
                    token_totals[metric] += value
                    break

    if not token_totals["total_tokens"]:
        token_totals["total_tokens"] = token_totals["input_tokens"] + token_totals["output_tokens"]
    return {
        "repair_attempts": repairs,
        "latency_ms": round(latency_ms, 2),
        **token_totals,
    }


def quality_failures(score: Mapping[str, Any]) -> tuple[str, ...]:
    """Return deterministic reasons an evaluated case misses the Phase 2 quality floor."""

    failures: list[str] = []
    if score.get("contract_pass") is not True:
        failures.append("contract_pass must be true")
    for metric, minimum in QUALITY_MINIMUMS.items():
        value = score.get(metric)
        if not isinstance(value, (int, float)) or isinstance(value, bool) or value < minimum:
            failures.append(f"{metric} must be at least {minimum}")
    for metric, maximum in QUALITY_MAXIMUMS.items():
        value = score.get(metric)
        if not isinstance(value, (int, float)) or isinstance(value, bool) or value > maximum:
            failures.append(f"{metric} must be at most {maximum}")
    return tuple(failures)


def summarize_score_documents(
    documents: Sequence[Mapping[str, Any]],
) -> tuple[dict[str, Any], ...]:
    """Summarize immutable live-score artifacts by run and configured model."""

    grouped: dict[tuple[str, str], list[Mapping[str, Any]]] = {}
    for document in documents:
        run_id = document.get("run_id")
        model_id = document.get("model_id")
        score = document.get("score")
        if (
            not isinstance(run_id, str)
            or not run_id
            or not isinstance(model_id, str)
            or not model_id
        ):
            raise ValueError("Evaluation score artifact requires run_id and model_id")
        if not isinstance(score, Mapping):
            raise ValueError("Evaluation score artifact requires a score object")
        grouped.setdefault((run_id, model_id), []).append(score)

    summaries: list[dict[str, Any]] = []
    for (run_id, model_id), scores in sorted(grouped.items()):
        averages: dict[str, float] = {}
        for metric in SUMMARY_METRICS:
            values = [score.get(metric) for score in scores]
            if all(
                isinstance(value, (int, float)) and not isinstance(value, bool) for value in values
            ):
                averages[metric] = round(sum(values) / len(values), 4)
        passed = sum(not quality_failures(score) for score in scores)
        summaries.append(
            {
                "run_id": run_id,
                "model_id": model_id,
                "score_count": len(scores),
                "passed": passed,
                "pass_rate": round(passed / len(scores), 4),
                "averages": averages,
            }
        )
    return tuple(summaries)


def _phrase_recall(expected: Sequence[str], actual: str) -> float:
    if not expected:
        return 1.0
    normalized_actual = _normalize(actual)
    matched = sum(_normalize(phrase) in normalized_actual for phrase in expected)
    return round(matched / len(expected), 4)


def _concept_recall(expected: Sequence[ConceptAliases], actual: str) -> float:
    if not expected:
        return 1.0
    normalized_actual = _normalize(actual)
    matched = sum(
        any(_normalize(alias) in normalized_actual for alias in aliases) for aliases in expected
    )
    return round(matched / len(expected), 4)


def _normalize(value: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", value.casefold()))


def _required_string(payload: Mapping[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{key} must be a non-empty string")
    return value.strip()


def _string_tuple(payload: Mapping[str, Any], key: str) -> tuple[str, ...]:
    value = payload.get(key)
    if not isinstance(value, list) or any(
        not isinstance(item, str) or not item.strip() for item in value
    ):
        raise ValueError(f"{key} must be a list of non-empty strings")
    return tuple(item.strip() for item in value)


def _concept_tuple(payload: Mapping[str, Any], key: str) -> tuple[ConceptAliases, ...]:
    value = payload.get(key)
    if not isinstance(value, list):
        raise ValueError(f"{key} must be a list of concepts")
    concepts: list[ConceptAliases] = []
    for item in value:
        aliases = [item] if isinstance(item, str) else item
        if (
            not isinstance(aliases, list)
            or not aliases
            or any(not isinstance(alias, str) or not alias.strip() for alias in aliases)
        ):
            raise ValueError(f"{key} concepts must be strings or non-empty alias lists")
        normalized = tuple(alias.strip() for alias in aliases)
        if len({_normalize(alias) for alias in normalized}) != len(normalized):
            raise ValueError(f"{key} concept aliases must be unique")
        concepts.append(normalized)
    return tuple(concepts)


def _required_sha256(payload: Mapping[str, Any], key: str) -> str:
    value = _required_string(payload, key)
    if not re.fullmatch(r"[0-9a-f]{64}", value):
        raise ValueError(f"{key} must be a lowercase SHA-256 digest")
    return value


def _required_split(payload: Mapping[str, Any]) -> str:
    value = _required_string(payload, "split")
    if value not in EVALUATION_SPLITS:
        raise ValueError(f"split must be one of {sorted(EVALUATION_SPLITS)}")
    return value


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate the Phase 2 evaluation manifest")
    parser.add_argument(
        "--manifest",
        type=Path,
        default=Path("tests/evaluation/manifest.json"),
    )
    parser.add_argument(
        "--check-assets",
        action="store_true",
        help="fail unless all evaluation artwork files are present",
    )
    args = parser.parse_args()
    manifest = load_manifest(args.manifest)
    missing = [case.asset for case in manifest.cases if not case.asset.is_file()]
    mismatched = [
        case.asset
        for case in manifest.cases
        if case.asset.is_file() and sha256(case.asset.read_bytes()).hexdigest() != case.asset_sha256
    ]
    print(
        json.dumps(
            {
                "manifest_version": manifest.manifest_version,
                "prompt_version": manifest.prompt_version,
                "case_count": len(manifest.cases),
                "missing_assets": [str(path) for path in missing],
                "mismatched_assets": [str(path) for path in mismatched],
            },
            indent=2,
        )
    )
    return int(args.check_assets and bool(missing or mismatched))


if __name__ == "__main__":
    raise SystemExit(main())
