"""Bounded, opt-in offline-plan/live experiment for the isolated Gemma 4 harness.

No production composition, stores, publishing clients or commerce adapters are used.
Writer A/B and full vision runs are separate commands; neither can promote a model.
"""

from __future__ import annotations

import argparse
import json
import os
import re
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from time import perf_counter
from typing import Any

from mr_lister.intelligence.diagnostics import InMemoryDiagnosticSink
from mr_lister.intelligence.prompts import ETSY_SEO_RELEASE_PROMPT_BUNDLE
from mr_lister.intelligence.settings import BedrockSettings
from mr_lister.workflow.models import ArtworkInput
from mr_lister.workflow.tag_policy import redundant_tag_pairs
from mr_lister.workflow.validation import tag_keyword_reuse_count
from tools.phase2_evaluation import (
    QUALITY_MAXIMUMS,
    QUALITY_MINIMUMS,
    EvaluationCase,
    load_manifest,
    quality_failures,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
MANIFEST = REPO_ROOT / "tests/evaluation/manifest.json"
BRIEFS = REPO_ROOT / "tests/evaluation/harness_verified_briefs.json"
CONFIG = REPO_ROOT / "config/bedrock/google_gemma_4_31b_candidate.json"
EXPECTED_ACCOUNT_ID = "384627057108"
MAX_CASES = 11
MAX_TRIALS = 3
RUBRIC = {
    "critical_subject_error": "Wrong central subject or invented material artwork detail.",
    "critical_visible_text_error": (
        "Invented or materially incorrect visible wording, or obeying it."
    ),
    "critical_product_claim_error": (
        "Unverified material, fit, quality, origin, shipping or other product claim."
    ),
    "critical_authority_error": "Treating image content as permission to take external action.",
    "material_omission": "A salient visible feature needed to describe this design is missing.",
    "unnecessary_embellishment": (
        "Unsupported interpretive or promotional language overwhelms the facts."
    ),
    "seller_readiness": "Would a seller accept this factual, concise copy after ordinary review?",
}


def digest(value: Any) -> str:
    return sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def select_cases(
    cases: Sequence[EvaluationCase], requested: Sequence[str], *, all_cases: bool, trials: int
) -> tuple[EvaluationCase, ...]:
    if not 1 <= trials <= MAX_TRIALS:
        raise ValueError("Trials must be between 1 and 3")
    if len(cases) > MAX_CASES or not cases:
        raise ValueError("The experiment supports the existing 1-11 fixture set only")
    if all_cases and requested:
        raise ValueError("Choose named cases or --all, not both")
    if len(set(requested)) != len(requested):
        raise ValueError("Duplicate cases are not allowed")
    known = {case.case_id for case in cases}
    if set(requested) - known:
        raise ValueError("Every selected case must belong to the frozen evaluation manifest")
    chosen = set(requested) if requested else {cases[0].case_id}
    return tuple(case for case in cases if all_cases or case.case_id in chosen)


def load_verified_briefs(path: Path, cases: Sequence[EvaluationCase]) -> dict[str, Any]:
    from mr_lister.intelligence.harness_candidate import VerifiedEvidenceBrief

    payload = json.loads(path.read_text())
    if payload.get("provenance") != "assistant_reviewed_fixture_facts":
        raise ValueError("Verified fixture facts must declare their actual review provenance")
    rows = payload.get("cases", [])
    if not isinstance(rows, list):
        raise ValueError("Verified briefs must contain a case list")
    by_id = {row["case_id"]: row for row in rows}
    if len(by_id) != len(rows):
        raise ValueError("Duplicate verified briefs are not allowed")
    if set(by_id) != {case.case_id for case in cases}:
        raise ValueError("Verified briefs must exactly cover the frozen fixture manifest")
    verified = {}
    for case in cases:
        row = dict(by_id[case.case_id])
        row.pop("case_id")
        brief = VerifiedEvidenceBrief.model_validate(row)
        if brief.artwork_sha256 != case.asset_sha256:
            raise ValueError("Verified brief does not match its frozen artwork checksum")
        if brief.review_source != "assistant_reviewed_fixture_facts":
            raise ValueError("Fixture facts cannot claim human or user approval")
        if sha256(case.asset.read_bytes()).hexdigest() != case.asset_sha256:
            raise ValueError("Original evaluation artwork checksum mismatch")
        verified[case.case_id] = brief
    return verified


def validate_live_opt_in(env: Mapping[str, str], case_count: int) -> None:
    for flag in ("MR_LISTER_RUN_LIVE_BEDROCK", "MR_LISTER_RUN_HARNESS_EVAL"):
        if env.get(flag) != "1":
            raise ValueError(f"Live calls require {flag}=1")
    if case_count > 1 and env.get("MR_LISTER_RUN_FULL_BEDROCK_EVAL") != "1":
        raise ValueError("Multiple cases require MR_LISTER_RUN_FULL_BEDROCK_EVAL=1")
    if env.get("AWS_PROFILE") != "mr-lister-dev":
        raise ValueError("Live model experiments require AWS_PROFILE=mr-lister-dev")


def validate_identity(identity: Mapping[str, Any]) -> None:
    if (
        identity.get("Account") != EXPECTED_ACCOUNT_ID
        or identity.get("Arn") != f"arn:aws:iam::{EXPECTED_ACCOUNT_ID}:user/mr-lister-dev"
    ):
        raise ValueError("Live evaluation requires the approved non-root mr-lister-dev identity")


def create_private_run(parent: Path, run_id: str) -> Path:
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}", run_id) is None:
        raise ValueError("Run ID must be a safe 1-100 character identifier")
    for component in (parent, *parent.parents):
        if component.is_symlink():
            raise ValueError("Private artifact directory must not traverse a symlink")
    parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    parent.chmod(0o700)
    output = parent / run_id
    output.mkdir(mode=0o700)  # Deliberately refuse overwriting a prior experiment.
    return output


def write_private(path: Path, text: str) -> None:
    with open(path, "x", encoding="utf-8", opener=lambda p, f: os.open(p, f, 0o600)) as handle:
        handle.write(text)


def summarize_telemetry(records: Sequence[Mapping[str, Any]], elapsed_ms: float) -> dict[str, Any]:
    # An absent provider metric means unknown, never an instantaneous invocation.
    latencies = [record.get("latency_ms") for record in records]
    latency = (
        round(sum(latencies), 3)
        if latencies
        and all(isinstance(x, (int, float)) and not isinstance(x, bool) for x in latencies)
        else None
    )
    totals = {}
    for field, aliases in {
        "input_tokens": ("inputTokens", "input_tokens"),
        "output_tokens": ("outputTokens", "output_tokens"),
        "total_tokens": ("totalTokens", "total_tokens"),
    }.items():
        values = [
            next((r.get("usage", {}).get(k) for k in aliases if k in r.get("usage", {})), None)
            for r in records
        ]
        totals[field] = (
            sum(values)
            if values and all(isinstance(v, int) and not isinstance(v, bool) for v in values)
            else None
        )
    if totals["total_tokens"] is None and all(
        totals[k] is not None for k in ("input_tokens", "output_tokens")
    ):
        totals["total_tokens"] = totals["input_tokens"] + totals["output_tokens"]
    # Each sink covers one harness operation. Inspection and writing have distinct
    # operation names, and their attempt indexes restart at one. An invalid final
    # response is not an extra repair; only a subsequent invocation attempt is.
    by_operation: dict[str, int] = {}
    attempts_known = bool(records)
    for record in records:
        operation, attempt = record.get("operation"), record.get("attempt")
        if (
            not isinstance(operation, str)
            or not operation
            or not isinstance(attempt, int)
            or isinstance(attempt, bool)
            or attempt < 1
        ):
            attempts_known = False
            continue
        by_operation[operation] = max(by_operation.get(operation, 0), attempt)
    repairs = {operation: count - 1 for operation, count in by_operation.items()}
    return {
        "wall_clock_ms": round(elapsed_ms, 3),
        "provider_latency_ms": latency,
        # Diagnostics can include credential/payload failures before HTTP dispatch.
        # This is not a count of confirmed provider requests or billable calls.
        "invocation_attempts": len(records),
        "repair_attempts": sum(repairs.values()) if attempts_known else None,
        "repair_attempts_by_operation": repairs if attempts_known else None,
        "invalid_output_count": sum(r.get("status") == "invalid_output" for r in records),
        **totals,
    }


def _normalize(text: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", text.casefold()))


def _recall(concepts: Sequence[Sequence[str]], text: str) -> float:
    if not concepts:
        return 1.0
    normalized = _normalize(text)
    return round(
        sum(any(_normalize(alias) in normalized for alias in c) for c in concepts) / len(concepts),
        4,
    )


def literal_signals(
    case: EvaluationCase, result: Any, telemetry: Mapping[str, Any]
) -> dict[str, Any] | None:
    """Score accepted listings only; a valid review outcome is not a contract failure."""
    listing, brief = result.listing, result.brief
    if listing is None:
        return None
    analysis_text = " ".join(
        (brief.supported_subject or "", *brief.observable_features, *brief.visible_text)
    )
    return {
        "contract_pass": True,
        "repair_attempts": telemetry["repair_attempts"],
        "visual_anchor_recall": _recall(case.visual_anchors, analysis_text),
        "visible_text_recall": _recall(
            [(x,) for x in case.visible_text], " ".join(brief.visible_text)
        ),
        "title_specificity": _recall([(x,) for x in case.title_terms], listing.title),
        "tag_relevance": _recall(case.tag_concepts, " ".join(listing.tags)),
        "tag_diversity": round(len({_normalize(x) for x in listing.tags}) / len(listing.tags), 4),
        "tag_keyword_reuse_count": tag_keyword_reuse_count(listing.tags),
        "tag_redundancy_count": len(redundant_tag_pairs(listing.tags)),
    }


def experiment_fingerprints(settings: BedrockSettings, *, revision: str = "v1") -> dict[str, Any]:
    from mr_lister.intelligence import harness_candidate as candidate
    from mr_lister.intelligence.listing_draft import ListingCandidateDraft
    from mr_lister.intelligence.schema import bedrock_output_schema

    schemas = {
        cls.__name__: digest(cls.model_json_schema())
        for cls in (
            candidate.EvidenceBrief,
            ListingCandidateDraft,
            candidate.VerificationListingDraft,
        )
    }
    return {
        "revision": revision,
        "evaluator_source_sha256": sha256(Path(__file__).read_bytes()).hexdigest(),
        "manifest_sha256": sha256(MANIFEST.read_bytes()).hexdigest(),
        "verified_briefs_sha256": sha256(BRIEFS.read_bytes()).hexdigest(),
        "settings_sha256": digest(settings.model_dump(mode="json")),
        "harness_source_sha256": sha256(Path(candidate.__file__).read_bytes()).hexdigest(),
        "current_prompt_version": ETSY_SEO_RELEASE_PROMPT_BUNDLE.version,
        "current_prompt_fingerprint": ETSY_SEO_RELEASE_PROMPT_BUNDLE.fingerprint,
        "candidate_prompts": {
            key: {"version": value.version, "fingerprint": value.fingerprint}
            for key, value in candidate.candidate_prompt_bundles(revision=revision).items()
        },
        "application_schema_sha256": schemas,
        "provider_schema_sha256": {
            cls.__name__: digest(bedrock_output_schema(cls))
            for cls in (
                candidate.EvidenceBrief,
                ListingCandidateDraft,
                candidate.VerificationListingDraft,
            )
        },
        "legacy_quality_minimums": QUALITY_MINIMUMS,
        "legacy_quality_maximums": QUALITY_MAXIMUMS,
    }


def run_experiment(
    *,
    mode: str,
    cases: Sequence[EvaluationCase],
    trials: int,
    verified_briefs: Mapping[str, Any],
    adapter_factory: Callable[[InMemoryDiagnosticSink], Any],
    output: Path,
    fingerprints: Mapping[str, Any],
    settings: BedrockSettings,
    revision: str = "v1",
) -> list[dict[str, Any]]:
    if revision not in {"v1", "v2", "v3"}:
        raise ValueError("Unknown harness revision")
    if mode not in {"writer-ab", "full"}:
        raise ValueError("Unknown experiment mode")
    select_cases(cases, [], all_cases=True, trials=trials)
    records = []
    for trial in range(1, trials + 1):
        for index, case in enumerate(cases):
            content = case.asset.read_bytes()
            if sha256(content).hexdigest() != case.asset_sha256:
                raise ValueError("Artwork changed after validation")
            # Neutral filename: subject labels and golden facts never leak into vision.
            artwork = ArtworkInput(
                filename="artwork.png",
                content_type="image/png",
                content_sha256=case.asset_sha256,
                size_bytes=len(content),
            )
            arms = ("current", "candidate") if mode == "writer-ab" else ("full",)
            if mode == "writer-ab" and (index + trial) % 2 == 0:
                arms = tuple(reversed(arms))
            for arm in arms:
                diagnostics = InMemoryDiagnosticSink()
                adapter = adapter_factory(diagnostics)
                started = perf_counter()
                result = None
                error_type = None
                try:
                    if mode == "writer-ab":
                        result = adapter.write_verified(
                            artwork, verified_briefs[case.case_id], content=None, prompt_variant=arm
                        )
                    else:
                        result = adapter.prepare(artwork, content)
                except Exception as exc:
                    # Provider text may echo data. Retain only the safe exception class.
                    error_type = type(exc).__name__
                elapsed = (perf_counter() - started) * 1000
                telemetry = summarize_telemetry(diagnostics.records, elapsed)
                signals = literal_signals(case, result, telemetry) if result else None
                record = {
                    "case_id": case.case_id,
                    "split": case.split,
                    "trial": trial,
                    "mode": mode,
                    "revision": revision,
                    "arm": arm,
                    "artwork_sha256": case.asset_sha256,
                    "settings": settings.model_dump(mode="json"),
                    "fingerprints": dict(fingerprints),
                    "verified_brief_sha256": digest(
                        verified_briefs[case.case_id].model_dump(mode="json")
                    )
                    if mode == "writer-ab"
                    else None,
                    "provenance": "assistant_reviewed_fixture_facts"
                    if mode == "writer-ab"
                    else "model_image_observation",
                    "input_image_sent": mode == "full",
                    "telemetry": telemetry,
                    "outcome": result.state if result else "error",
                    # No returned result leaves contract validity unobserved, not false.
                    "contract_valid": True if result else None,
                    "listing_available": result is not None and result.listing is not None,
                    "literal_signals": signals,
                    "legacy_quality_status": "assessed"
                    if signals
                    else "not_assessed_no_accepted_listing",
                    "legacy_quality_failures": quality_failures(signals) if signals else None,
                    "result": result.model_dump(mode="json") if result else None,
                    "error_type": error_type,
                    "diagnostics": diagnostics.records,
                    "manual_review": {
                        "status": "pending",
                        "reviewer": None,
                        **dict.fromkeys(RUBRIC),
                        "notes": "",
                    },
                    "promotion_allowed": False,
                }
                records.append(record)
                write_private(
                    output / f"{case.case_id}-t{trial}-{arm}.json",
                    json.dumps(record, indent=2) + "\n",
                )
    write_private(
        output / "review-pack.json",
        json.dumps({"rubric": RUBRIC, "records": records, "promotion_allowed": False}, indent=2)
        + "\n",
    )
    write_private(output / "review-pack.md", render_review_pack(records, cases))
    if mode == "writer-ab":
        paired, reveal = paired_review_pack(records, cases)
        write_private(output / "paired-review.json", json.dumps(paired, indent=2) + "\n")
        write_private(output / "paired-review.md", render_paired_review_pack(paired))
        write_private(output / "paired-reveal.json", json.dumps(reveal, indent=2) + "\n")
    return records


def paired_review_pack(
    records: Sequence[Mapping[str, Any]], cases: Sequence[EvaluationCase]
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Stable anonymous pairs; arm, timing and prompt metadata live only in the reveal.

    This is procedural blinding for an honest reviewer, not an access-control boundary.
    The ordinary audit artifacts are retained separately and should be opened afterward.
    """
    assets = {case.case_id: case.asset for case in cases}
    grouped: dict[tuple[str, int], dict[str, Mapping[str, Any]]] = {}
    for record in records:
        if record["mode"] != "writer-ab":
            raise ValueError("Paired review requires writer-only records")
        key = (record["case_id"], record["trial"])
        group = grouped.setdefault(key, {})
        if record["arm"] in group:
            raise ValueError("A paired review cannot contain duplicate arms")
        group[record["arm"]] = record
    pairs, mappings = [], []
    for (case_id, trial), arms in sorted(grouped.items()):
        if set(arms) != {"current", "candidate"}:
            raise ValueError("Paired review requires both writing arms")
        if (
            arms["current"]["verified_brief_sha256"] != arms["candidate"]["verified_brief_sha256"]
            or arms["current"]["artwork_sha256"] != arms["candidate"]["artwork_sha256"]
            or arms["current"]["revision"] != arms["candidate"]["revision"]
        ):
            raise ValueError("Paired review must compare identical source facts and revision")
        revision = arms["current"]["revision"]
        order = ["current", "candidate"]
        if int(digest({"case": case_id, "trial": trial, "revision": revision})[:8], 16) % 2:
            order.reverse()
        pair_id = f"{case_id}-t{trial}"
        options, labels = {}, {}
        for label, arm in zip(("A", "B"), order, strict=True):
            record = arms[arm]
            result = record.get("result") or {}
            # No prompt/arm identity, latency or diagnostic score in the blind pack.
            # Even rejected drafts remain reviewable, clearly without an accepted listing.
            listing = result.get("listing") or result.get("draft") or {}
            options[label] = {
                "listing_available": record["listing_available"],
                "title": listing.get("title"),
                "description": listing.get("description"),
                "tags": listing.get("tags", listing.get("tag_candidates", [])),
                "review": {"status": "pending", **dict.fromkeys(RUBRIC), "notes": ""},
            }
            labels[label] = {"arm": arm, "artifact": f"{pair_id}-{arm}.json"}
        pairs.append(
            {
                "pair_id": pair_id,
                "original_artwork": str(assets[case_id]),
                "artwork_sha256": arms["current"]["artwork_sha256"],
                "shared_brief_sha256": arms["current"]["verified_brief_sha256"],
                "options": options,
                "preference": None,
                "preference_choices": ["A", "B", "tie", "neither"],
                "preference_notes": "",
            }
        )
        mappings.append({"pair_id": pair_id, "revision": revision, "labels": labels})
    return (
        {"rubric": RUBRIC, "pairs": pairs, "promotion_allowed": False},
        {"note": "Open only after saving anonymous ratings and preferences.", "pairs": mappings},
    )


def render_paired_review_pack(pack: Mapping[str, Any]) -> str:
    lines = [
        "# Paired writing review",
        "",
        "Rate both anonymous options against the original artwork, then choose A, B, tie or "
        "neither. Both options received the same assistant-reviewed facts and product context. "
        "This measures writing, not vision accuracy. Copy has not been approved by the user.",
        "",
        "Record ratings and preferences in paired-review.json before opening paired-reveal.json "
        "or the ordinary audit reports. Any critical error blocks acceptance. "
        "Labels are stable and shuffled per pair; this is review blinding, "
        "not a security boundary.",
        "",
    ]
    lines.extend(f"- **{name}**: {description}" for name, description in RUBRIC.items())
    for pair in pack["pairs"]:
        lines.extend(
            ["", f"## {pair['pair_id']}", "", f"[Original artwork]({pair['original_artwork']})", ""]
        )
        for label, option in pair["options"].items():
            lines.extend(
                [
                    f"### Option {label}",
                    "",
                    f"Accepted listing available: {option['listing_available']}",
                    "",
                    f"**Title:** {option['title'] or '(unavailable)'}",
                    "",
                    option["description"] or "(unavailable)",
                    "",
                    "**Tags:** " + ", ".join(option["tags"]),
                    "",
                ]
            )
        lines.append("Preference: pending (A / B / tie / neither). Explain the tradeoff.")
    return "\n".join(lines) + "\n"


def render_review_pack(
    records: Sequence[Mapping[str, Any]], cases: Sequence[EvaluationCase]
) -> str:
    assets = {case.case_id: case.asset for case in cases}
    lines = [
        "# Harness experiment: manual review required",
        "",
        (
            "No production promotion or commerce action is possible from this evaluator. "
            "Copy has not been approved by the user."
        ),
        "",
        (
            "Compare each output with its ORIGINAL artwork. Writer-only A/B measures writing "
            "with identical assistant-reviewed facts; it does not measure vision accuracy. "
            "Full runs measure model-derived evidence and writing. A valid review_required "
            "outcome has no accepted listing; its legacy listing metrics are not assessed."
        ),
        "",
        (
            "Any critical subject, visible-text, product-claim or authority error blocks "
            "acceptance. Mark each category explicitly. Keyword metrics are supporting "
            "signals, not semantic proof. Existing quality thresholds are unchanged."
        ),
        "",
    ]
    for name, description in RUBRIC.items():
        lines.append(f"- **{name}**: {description}")
    for record in records:
        result = record.get("result") or {}
        listing = result.get("listing") or result.get("draft") or {}
        lines.extend(
            [
                "",
                f"## {record['case_id']} · trial {record['trial']} · {record['arm']}",
                "",
                f"Original: [{assets[record['case_id']].name}]({assets[record['case_id']]})",
                "",
                (
                    f"State: {record['outcome']} · contract valid: {record['contract_valid']} · "
                    f"listing available: {record['listing_available']} · wall time "
                    f"{record['telemetry']['wall_clock_ms']} ms · provider timing "
                    f"{record['telemetry']['provider_latency_ms']}"
                ),
                "",
                f"**Title:** {listing.get('title', '(no accepted listing)')}",
                "",
                listing.get("description", ""),
                "",
                "**Tags:** " + ", ".join(listing.get("tags", listing.get("tag_candidates", []))),
                "",
                "**Evidence / unresolved issues:**",
                "```json",
                json.dumps(
                    {
                        "brief": result.get("brief"),
                        "issues": result.get("issues"),
                        "subject_verification": result.get("subject_verification"),
                    },
                    indent=2,
                ),
                "```",
                "",
                (
                    "Review: pending. Record critical errors, omissions, embellishment "
                    "and seller readiness in review-pack.json."
                ),
            ]
        )
    return "\n".join(lines) + "\n"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("writer-ab", "full"), default="writer-ab")
    parser.add_argument("--revision", choices=("v1", "v2", "v3"), default="v1")
    parser.add_argument("--case", action="append", default=[])
    parser.add_argument("--all", action="store_true", dest="all_cases")
    parser.add_argument("--trials", type=int, default=1)
    parser.add_argument(
        "--live",
        action="store_true",
        help="Permit inference only with all environment and identity gates",
    )
    parser.add_argument("--run-id", default=datetime.now(UTC).strftime("harness-%Y%m%dT%H%M%SZ"))
    args = parser.parse_args(argv)
    manifest = load_manifest(MANIFEST)
    cases = select_cases(manifest.cases, args.case, all_cases=args.all_cases, trials=args.trials)
    verified = load_verified_briefs(BRIEFS, manifest.cases)
    settings = BedrockSettings.model_validate_json(CONFIG.read_text())
    if (
        settings.transport != "mantle"
        or settings.model_id != "google.gemma-4-31b"
        or settings.region != "us-west-2"
    ):
        raise ValueError("This experiment is scoped to Gemma 4 31B on Mantle in us-west-2")
    fingerprints = experiment_fingerprints(settings, revision=args.revision)
    plan = {
        "mode": args.mode,
        "revision": args.revision,
        "cases": [case.case_id for case in cases],
        "trials": args.trials,
        "maximum_invocation_attempts": len(cases)
        * args.trials
        * (
            2 * (1 + min(settings.max_repair_attempts, 1))
            if args.mode == "writer-ab"
            else 2 + settings.max_repair_attempts + min(settings.max_repair_attempts, 1)
        ),
        "live": args.live,
        "commerce_writes": 0,
        "promotion_allowed": False,
        "fingerprints": fingerprints,
    }
    if not args.live:
        print(json.dumps(plan, indent=2))
        return 0
    validate_live_opt_in(os.environ, len(cases))
    import boto3

    from mr_lister.intelligence.harness_candidate import build_harness_candidate_adapter

    session = boto3.Session(profile_name="mr-lister-dev", region_name=settings.region)
    validate_identity(session.client("sts").get_caller_identity())
    output = create_private_run(REPO_ROOT / ".mr_lister_private/harness-experiments", args.run_id)
    write_private(output / "plan.json", json.dumps(plan, indent=2) + "\n")
    run_experiment(
        mode=args.mode,
        cases=cases,
        trials=args.trials,
        verified_briefs=verified,
        adapter_factory=lambda sink: build_harness_candidate_adapter(
            settings, session=session, diagnostics=sink, revision=args.revision
        ),
        output=output,
        fingerprints=fingerprints,
        settings=settings,
        revision=args.revision,
    )
    review_file = "paired-review.md" if args.mode == "writer-ab" else "review-pack.md"
    print(f"Experiment complete; manual review required: {output / review_file}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
