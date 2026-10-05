"""Paired, bounded image-detail experiment for the frozen v3 harness.

An offline plan is the default. Explicitly gated live runs use the same model, prompts,
request ceiling, repairs and source images; only the inspection PNG byte budget varies.
No application jobs, commerce adapters or production writes are involved.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from hashlib import sha256
from io import BytesIO
from pathlib import Path
from time import perf_counter
from typing import Any

from PIL import Image

from mr_lister.intelligence.harness_candidate import (
    HarnessCandidateAdapter,
    VerifiedProductContext,
)
from mr_lister.intelligence.images import BedrockImage, prepare_bedrock_image
from mr_lister.intelligence.mantle import (
    MAX_REQUEST_BYTES,
    MantleClient,
    SigV4MantleClient,
    _encoded_request,
)
from mr_lister.intelligence.settings import BedrockSettings
from mr_lister.workflow.models import ArtworkInput
from tools.evaluate_harness_artwork import (
    CONFIG,
    artwork_fingerprints,
    load_artwork,
    run_artwork,
    validate_options,
)
from tools.evaluate_harness_candidate import (
    create_private_run,
    validate_identity,
    validate_live_opt_in,
    write_private,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
REVISION = "v3"
FROZEN_FULL_FINGERPRINT = "0fda08954b93aa25c8b3b8ff158cbba124bc8d37cb30e311a229c328be64e7b9"
PROFILES = ("standard", "detail")
MAX_CASES = 6
MAX_TRIALS = 2
DETAIL_IMAGE_BYTES = 2_300_000


def prepare_profile(artwork: ArtworkInput, content: bytes, profile: str) -> BedrockImage:
    """Preserve parent binding validation and the untouched standard rendition."""
    if profile not in PROFILES:
        raise ValueError("Profile must be standard or detail")
    standard = HarnessCandidateAdapter._source_image(artwork, content)
    if profile == "standard":
        return standard
    return prepare_bedrock_image(content, max_side=1600, max_bytes=DETAIL_IMAGE_BYTES)


class DetailHarnessCandidateAdapter(HarnessCandidateAdapter):
    """Experiment-only rendition override; all model behavior stays in the frozen parent."""

    def __init__(self, *, profile: str = "standard", **kwargs: Any) -> None:
        if profile not in PROFILES:
            raise ValueError("Profile must be standard or detail")
        if kwargs.get("revision", REVISION) != REVISION:
            raise ValueError("The detail experiment requires frozen v3")
        kwargs["revision"] = REVISION
        super().__init__(**kwargs)
        self._profile = profile

    def _source_image(self, artwork: ArtworkInput, content: bytes) -> BedrockImage:
        return prepare_profile(artwork, content, self._profile)


def _image_metadata(content: bytes) -> dict[str, Any]:
    with Image.open(BytesIO(content)) as image:
        if image.format != "PNG":
            raise ValueError("Measured inspection image must be PNG")
        width, height = image.size
        image.verify()
    return {
        "image_bytes": len(content),
        "image_sha256": sha256(content).hexdigest(),
        "width": width,
        "height": height,
    }


def _request_images(request: Mapping[str, Any]) -> list[dict[str, Any]]:
    images = []
    for message in request.get("messages", []):
        blocks = message.get("content")
        if not isinstance(blocks, list):
            continue
        for block in blocks:
            if block.get("type") != "image_url":
                continue
            uri = block.get("image_url", {}).get("url", "")
            prefix = "data:image/png;base64,"
            if not isinstance(uri, str) or not uri.startswith(prefix):
                raise ValueError("Measured image must use an inline PNG")
            content = base64.b64decode(uri[len(prefix) :], validate=True)
            images.append(_image_metadata(content))
    return images


class MeasuringClient:
    """Keep safe sizes and image fingerprints, never raw requests or credentials."""

    def __init__(self, delegate: MantleClient) -> None:
        self.delegate = delegate
        self.records: list[dict[str, Any]] = []

    def complete(self, request: Mapping[str, Any]) -> dict[str, Any]:
        metric: dict[str, Any] = {
            "attempt": len(self.records) + 1,
            "request_bytes": None,
            "request_limit_bytes": MAX_REQUEST_BYTES,
            "images": [],
            "delegate_called": False,
            "elapsed_ms": None,
            "error_type": None,
        }
        self.records.append(metric)
        try:
            # Includes schemas, base64 image and repair history under the existing cap.
            metric["request_bytes"] = len(_encoded_request(request))
            metric["images"] = _request_images(request)
        except Exception as error:
            metric["error_type"] = type(error).__name__
            raise
        started = perf_counter()
        metric["delegate_called"] = True
        try:
            return self.delegate.complete(request)
        except Exception as error:
            metric["error_type"] = type(error).__name__
            raise
        finally:
            # Transport/model round trip only; detail preparation also computes standard.
            metric["elapsed_ms"] = round((perf_counter() - started) * 1000, 3)


def trial_schedule(case_count: int, trials: int) -> list[tuple[int, int, str]]:
    """Return zero-based case index, one-based outer trial and alternated profile."""
    if isinstance(case_count, bool) or not 1 <= case_count <= MAX_CASES:
        raise ValueError("The detail experiment requires 1 to 6 artworks")
    if isinstance(trials, bool) or not 1 <= trials <= MAX_TRIALS:
        raise ValueError("Detail trials must be 1 or 2")
    return [
        (case_index, trial, profile)
        for trial in range(1, trials + 1)
        for case_index in range(case_count)
        for profile in (PROFILES if (case_index + trial - 1) % 2 == 0 else PROFILES[::-1])
    ]


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artwork", action="append", type=Path, required=True)
    parser.add_argument("--trials", type=int, default=2)
    parser.add_argument("--run-id", default=datetime.now(UTC).strftime("detail-%Y%m%dT%H%M%SZ"))
    parser.add_argument("--live", action="store_true")
    args = parser.parse_args(argv)
    schedule = trial_schedule(len(args.artwork), args.trials)
    validate_options(revision=REVISION, trials=args.trials, run_id=args.run_id)
    settings = BedrockSettings.model_validate_json(CONFIG.read_text())
    if (settings.transport, settings.model_id, settings.region) != (
        "mantle",
        "google.gemma-4-31b",
        "us-west-2",
    ):
        raise ValueError("The detail experiment requires Gemma 4 31B Mantle in us-west-2")
    fingerprints = artwork_fingerprints(settings, revision=REVISION)
    if fingerprints["candidate_prompts"]["full"]["fingerprint"] != FROZEN_FULL_FINGERPRINT:
        raise ValueError("The frozen v3 prompt fingerprint changed")
    fingerprints["detail_evaluator_source_sha256"] = sha256(Path(__file__).read_bytes()).hexdigest()
    # Snapshot each source once before any AWS session or model call. Paths and filenames
    # are deliberately absent from plans, inference inputs and metadata.
    snapshots = [load_artwork(path) for path in args.artwork]
    cases = []
    for index, (artwork, content) in enumerate(snapshots):
        renditions = {}
        for profile in PROFILES:
            image = prepare_profile(artwork, content, profile)
            renditions[profile] = {
                **_image_metadata(image.content),
                "source_width": image.source_width,
                "source_height": image.source_height,
                "transparency_composited": image.transparency_composited,
            }
        cases.append(
            {
                "case_id": f"case-{index + 1:02d}",
                "artwork_sha256": artwork.content_sha256,
                "artwork_size_bytes": artwork.size_bytes,
                "artwork_content_type": artwork.content_type,
                "profiles": renditions,
                "identical_input_control": (
                    renditions["standard"]["image_sha256"] == renditions["detail"]["image_sha256"]
                ),
            }
        )
    plan = {
        "mode": "paired_image_detail",
        "revision": REVISION,
        "cases": cases,
        "trials": args.trials,
        "arm_count": len(schedule),
        "schedule": [
            {"case_id": cases[index]["case_id"], "trial": trial, "profile": profile}
            for index, trial, profile in schedule
        ],
        "maximum_invocation_attempts": len(schedule)
        * (2 + settings.max_repair_attempts + min(settings.max_repair_attempts, 1)),
        "request_limit_bytes": MAX_REQUEST_BYTES,
        "settings": settings.model_dump(mode="json"),
        "fingerprints": fingerprints,
        "timing_note": "Wire elapsed is delegate round-trip time, excluding image preparation. "
        "Detail preparation also computes the standard rendition for parent validation.",
        "control_note": "Byte-identical profiles are repeat controls, not a resolution increase.",
        "live": args.live,
        "commerce_writes": 0,
        "promotion_allowed": False,
    }
    if not args.live:
        print(json.dumps(plan, indent=2))
        return 0
    # Both profiles count as separate experiment arms, including a single-artwork run.
    validate_live_opt_in(os.environ, len(schedule))
    import boto3

    session = boto3.Session(profile_name="mr-lister-dev", region_name=settings.region)
    validate_identity(session.client("sts").get_caller_identity())
    output = create_private_run(REPO_ROOT / ".mr_lister_private/detail-experiments", args.run_id)
    write_private(output / "plan.json", json.dumps(plan, indent=2) + "\n")
    records = []
    for index, trial, profile in schedule:
        case = cases[index]
        artwork, content = snapshots[index]
        arm_id = f"{case['case_id']}-t{trial}-{profile}"
        arm_output = create_private_run(output, arm_id)
        arm_fingerprints = {
            **fingerprints,
            "profile": profile,
            "rendition": case["profiles"][profile],
            "experiment_case_id": case["case_id"],
            "experiment_trial": trial,
            "identical_input_control": case["identical_input_control"],
        }
        measuring = MeasuringClient(SigV4MantleClient(session=session, region=settings.region))
        try:
            rows = run_artwork(
                artwork=artwork,
                content=content,
                revision=REVISION,
                trials=1,
                settings=settings,
                fingerprints=arm_fingerprints,
                output=arm_output,
                adapter_factory=lambda sink, client=measuring, selected=profile: (
                    DetailHarnessCandidateAdapter(
                        client=client,
                        settings=settings,
                        product_context=VerifiedProductContext(),
                        diagnostics=sink,
                        revision=REVISION,
                        profile=selected,
                    )
                ),
            )
        finally:
            write_private(
                arm_output / "wire-metrics.json",
                json.dumps({"attempts": measuring.records}, indent=2) + "\n",
            )
        records.extend(
            {
                **row,
                "case_id": case["case_id"],
                "inner_trial": row["trial"],
                "trial": trial,
                "profile": profile,
                "identical_input_control": case["identical_input_control"],
                "arm_directory": arm_id,
                "wire_metrics": measuring.records,
            }
            for row in rows
        )
        # Preserve completed arms if a subsequent execution is interrupted.
        write_private(output / f"completed-{arm_id}.json", json.dumps(records[-1], indent=2) + "\n")
        print(f"Completed {arm_id}: {rows[0]['outcome']}", flush=True)
    write_private(
        output / "review-pack.json",
        json.dumps({"plan": plan, "records": records, "promotion_allowed": False}, indent=2) + "\n",
    )
    print(f"Detail experiment complete; manual review required: {output / 'review-pack.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
