"""Evaluate one new PNG or JPEG with the frozen harness; offline plan unless explicitly enabled.

No fixture answers, commerce clients, application jobs or production writes are involved.
A successful model response is an evaluation result, never an approval or promotion.
"""

from __future__ import annotations

import argparse
import json
import os
import re
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from hashlib import sha256
from io import BytesIO
from pathlib import Path
from time import perf_counter
from typing import Any, Literal

from PIL import Image, UnidentifiedImageError
from pydantic import Field

from mr_lister.control.models import PHASE6_MAX_SOURCE_ARTWORK_BYTES
from mr_lister.intelligence.diagnostics import InMemoryDiagnosticSink
from mr_lister.intelligence.settings import BedrockSettings
from mr_lister.workflow.errors import InvalidArtworkError
from mr_lister.workflow.models import ArtworkInput
from mr_lister.workflow.validation import MAX_ARTWORK_PIXELS, PNG_SIGNATURE, validate_artwork
from tools.evaluate_harness_candidate import (
    RUBRIC,
    create_private_run,
    digest,
    summarize_telemetry,
    validate_identity,
    validate_live_opt_in,
    write_private,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
CONFIG = REPO_ROOT / "config/bedrock/google_gemma_4_31b_candidate.json"
MAX_SOURCE_BYTES = PHASE6_MAX_SOURCE_ARTWORK_BYTES


class IndependentArtworkInput(ArtworkInput):
    """Evaluator-only input: production persisted PNG contracts remain unchanged.

    The frozen harness binds size/hash and derives its own inspection PNG from source
    bytes. Its normal production caller already normalizes JPEG in the browser; this
    diagnostic keeps the user's untouched JPEG bytes and original media type instead.
    """

    content_type: Literal["image/png", "image/jpeg"]
    size_bytes: int = Field(gt=0, le=MAX_SOURCE_BYTES)


def validate_independent_artwork(content: bytes) -> IndependentArtworkInput:
    if not content or len(content) > MAX_SOURCE_BYTES:
        raise InvalidArtworkError("Artwork must be nonempty and at most 5 MiB")
    if content.startswith(PNG_SIGNATURE):
        validated = validate_artwork(
            filename="artwork.png", content_type="image/png", content=content
        )
        return IndependentArtworkInput.model_validate(validated.model_dump())
    if not content.startswith(b"\xff\xd8\xff"):
        raise InvalidArtworkError("Independent artwork must be PNG or JPEG")
    try:
        with Image.open(BytesIO(content)) as image:
            width, height = image.size
            if image.format != "JPEG":
                raise InvalidArtworkError("Artwork does not contain a valid JPEG")
            if (
                width <= 0
                or height <= 0
                or width > 20_000
                or height > 20_000
                or width * height > MAX_ARTWORK_PIXELS
            ):
                raise InvalidArtworkError("Artwork exceeds the safe decoded-pixel limit")
            image.verify()
        with Image.open(BytesIO(content)) as image:
            image.load()
    except (OSError, SyntaxError, UnidentifiedImageError, ValueError) as error:
        raise InvalidArtworkError("Artwork contains corrupt or incomplete JPEG data") from error
    return IndependentArtworkInput(
        filename="artwork.jpg",
        content_type="image/jpeg",
        content_sha256=sha256(content).hexdigest(),
        size_bytes=len(content),
    )


def load_artwork(path: Path) -> tuple[IndependentArtworkInput, bytes]:
    """Validate a bounded immutable byte snapshot; never resize or overwrite the source."""
    if not path.is_file():
        raise ValueError("Artwork must be a local PNG or JPEG file")
    if path.stat().st_size > MAX_SOURCE_BYTES:
        raise ValueError("Artwork exceeds the current 5 MiB upload limit")
    with path.open("rb") as handle:
        content = handle.read(MAX_SOURCE_BYTES + 1)
    if len(content) > MAX_SOURCE_BYTES:
        raise ValueError("Artwork exceeds the current 5 MiB upload limit")
    # Keep source filenames and user-supplied subject labels out of model input.
    artwork = validate_independent_artwork(content)
    return artwork, content


def validate_options(*, revision: str, trials: int, run_id: str) -> None:
    if revision not in {"v1", "v2", "v3"}:
        raise ValueError("Revision must be v1, v2 or v3")
    if isinstance(trials, bool) or not 1 <= trials <= 3:
        raise ValueError("Trials must be between 1 and 3")
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}", run_id) is None:
        raise ValueError("Run ID must be a safe 1-100 character identifier")


def artwork_fingerprints(settings: BedrockSettings, *, revision: str) -> dict[str, Any]:
    from mr_lister.intelligence import harness_candidate as candidate
    from mr_lister.intelligence.listing_draft import ListingCandidateDraft
    from mr_lister.intelligence.prompts import ETSY_SEO_RELEASE_PROMPT_BUNDLE
    from mr_lister.intelligence.schema import bedrock_output_schema
    from tools import evaluate_harness_candidate as shared

    classes = (candidate.EvidenceBrief, ListingCandidateDraft, candidate.VerificationListingDraft)
    return {
        "revision": revision,
        "evaluator_source_sha256": sha256(Path(__file__).read_bytes()).hexdigest(),
        "shared_evaluator_source_sha256": sha256(Path(shared.__file__).read_bytes()).hexdigest(),
        "harness_source_sha256": sha256(Path(candidate.__file__).read_bytes()).hexdigest(),
        "settings_sha256": digest(settings.model_dump(mode="json")),
        "current_prompt_version": ETSY_SEO_RELEASE_PROMPT_BUNDLE.version,
        "current_prompt_fingerprint": ETSY_SEO_RELEASE_PROMPT_BUNDLE.fingerprint,
        "candidate_prompts": {
            key: {"version": bundle.version, "fingerprint": bundle.fingerprint}
            for key, bundle in candidate.candidate_prompt_bundles(revision=revision).items()
        },
        "application_schema_sha256": {
            cls.__name__: digest(cls.model_json_schema()) for cls in classes
        },
        "provider_schema_sha256": {
            cls.__name__: digest(bedrock_output_schema(cls)) for cls in classes
        },
    }


def run_artwork(
    *,
    artwork: IndependentArtworkInput,
    content: bytes,
    revision: str,
    trials: int,
    settings: BedrockSettings,
    fingerprints: Mapping[str, Any],
    output: Path,
    adapter_factory: Callable[[InMemoryDiagnosticSink], Any],
) -> list[dict[str, Any]]:
    """Call only the frozen prepare boundary; no gold facts or commerce ports exist here."""
    validate_options(revision=revision, trials=trials, run_id=output.name)
    validated = validate_independent_artwork(content)
    if len(content) > MAX_SOURCE_BYTES or validated != artwork:
        raise ValueError("Artwork snapshot does not match its validated source binding")
    # Private, byte-for-byte source copy makes later visual review reproducible.
    with open(output / artwork.filename, "xb", opener=lambda p, f: os.open(p, f, 0o600)) as handle:
        handle.write(content)
    records = []
    for trial in range(1, trials + 1):
        diagnostics = InMemoryDiagnosticSink()
        adapter = adapter_factory(diagnostics)
        started = perf_counter()
        result = None
        error_type = None
        try:
            result = adapter.prepare(artwork, content)
        except Exception as exc:
            # Provider error text can echo request data; save only its safe class name.
            error_type = type(exc).__name__
        elapsed_ms = (perf_counter() - started) * 1000
        record = {
            "mode": "independent_artwork",
            "revision": revision,
            "trial": trial,
            "artwork_sha256": artwork.content_sha256,
            "artwork_size_bytes": artwork.size_bytes,
            "source_copy": artwork.filename,
            "input_filename": artwork.filename,
            "artwork_content_type": artwork.content_type,
            "source_path_kind": "direct_jpeg_diagnostic"
            if artwork.content_type == "image/jpeg"
            else "production_png_boundary",
            "settings": settings.model_dump(mode="json"),
            "fingerprints": dict(fingerprints),
            "provenance": "model_image_observation",
            "telemetry": summarize_telemetry(diagnostics.records, elapsed_ms),
            "outcome": result.state if result else "error",
            "contract_valid": True if result else None,
            "listing_available": result is not None and result.listing is not None,
            "result": result.model_dump(mode="json") if result else None,
            "error_type": error_type,
            "diagnostics": diagnostics.records,
            "manual_review": {
                "status": "pending",
                "reviewer": None,
                **dict.fromkeys(RUBRIC),
                "notes": "",
            },
            "commerce_writes": 0,
            "promotion_allowed": False,
        }
        records.append(record)
        write_private(output / f"trial-{trial}.json", json.dumps(record, indent=2) + "\n")
    write_private(
        output / "review-pack.json",
        json.dumps(
            {
                "rubric": RUBRIC,
                "records": records,
                "promotion_allowed": False,
            },
            indent=2,
        )
        + "\n",
    )
    write_private(
        output / "review-pack.md", render_artwork_review(records, output / artwork.filename)
    )
    return records


def render_artwork_review(records: Sequence[Mapping[str, Any]], source: Path) -> str:
    lines = [
        "# Independent artwork review",
        "",
        f"[Original artwork]({source})",
        "",
        "Compare every claim with the original. No expected subject, fixture answers or gold "
        "brief was supplied. Model agreement is self-consistency, not proof of accuracy. "
        "Copy and production promotion remain unapproved.",
        "",
    ]
    lines.extend(f"- **{name}**: {description}" for name, description in RUBRIC.items())
    for record in records:
        result = record.get("result") or {}
        listing = result.get("listing") or result.get("draft") or {}
        lines.extend(
            [
                "",
                f"## Trial {record['trial']}",
                "",
                f"Outcome: {record['outcome']}. Contract valid: {record['contract_valid']}. "
                f"Listing available: {record['listing_available']}.",
                "",
                f"Wall time: {record['telemetry']['wall_clock_ms']} ms. "
                f"Provider latency: {record['telemetry']['provider_latency_ms']}.",
                "",
                f"**Title:** {listing.get('title', '(unavailable)')}",
                "",
                listing.get("description", "(unavailable)"),
                "",
                "**Tags:** " + ", ".join(listing.get("tags", listing.get("tag_candidates", []))),
                "",
                "**Observed evidence and unresolved issues:**",
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
                "Review pending: record judgments in review-pack.json.",
            ]
        )
    return "\n".join(lines) + "\n"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--artwork", type=Path, required=True, help="Local PNG or JPEG, at most 5 MiB"
    )
    parser.add_argument("--revision", choices=("v1", "v2", "v3"), default="v2")
    parser.add_argument("--trials", type=int, default=1)
    parser.add_argument("--run-id", default=datetime.now(UTC).strftime("artwork-%Y%m%dT%H%M%SZ"))
    parser.add_argument(
        "--live", action="store_true", help="Inference requires explicit live/dev gates"
    )
    args = parser.parse_args(argv)
    validate_options(revision=args.revision, trials=args.trials, run_id=args.run_id)
    artwork, content = load_artwork(args.artwork)
    settings = BedrockSettings.model_validate_json(CONFIG.read_text())
    if (settings.transport, settings.model_id, settings.region) != (
        "mantle",
        "google.gemma-4-31b",
        "us-west-2",
    ):
        raise ValueError("This experiment is scoped to Gemma 4 31B on Mantle in us-west-2")
    fingerprints = artwork_fingerprints(settings, revision=args.revision)
    plan = {
        "mode": "independent_artwork",
        "revision": args.revision,
        "trials": args.trials,
        "artwork_sha256": artwork.content_sha256,
        "artwork_size_bytes": artwork.size_bytes,
        "input_filename": artwork.filename,
        "artwork_content_type": artwork.content_type,
        "source_path_kind": "direct_jpeg_diagnostic"
        if artwork.content_type == "image/jpeg"
        else "production_png_boundary",
        "source_limit_bytes": MAX_SOURCE_BYTES,
        "maximum_invocation_attempts": args.trials
        * (2 + settings.max_repair_attempts + min(settings.max_repair_attempts, 1)),
        "settings": settings.model_dump(mode="json"),
        "fingerprints": fingerprints,
        "live": args.live,
        "commerce_writes": 0,
        "promotion_allowed": False,
    }
    if not args.live:
        print(json.dumps(plan, indent=2))
        return 0
    validate_live_opt_in(os.environ, 1)
    import boto3

    from mr_lister.intelligence.harness_candidate import build_harness_candidate_adapter

    session = boto3.Session(profile_name="mr-lister-dev", region_name=settings.region)
    validate_identity(session.client("sts").get_caller_identity())
    output = create_private_run(REPO_ROOT / ".mr_lister_private/artwork-experiments", args.run_id)
    write_private(output / "plan.json", json.dumps(plan, indent=2) + "\n")
    run_artwork(
        artwork=artwork,
        content=content,
        revision=args.revision,
        trials=args.trials,
        settings=settings,
        fingerprints=fingerprints,
        output=output,
        adapter_factory=lambda sink: build_harness_candidate_adapter(
            settings,
            session=session,
            diagnostics=sink,
            revision=args.revision,
        ),
    )
    print(f"Experiment complete; manual review required: {output / 'review-pack.md'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
