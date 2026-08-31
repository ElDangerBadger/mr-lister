from __future__ import annotations

import json
from pathlib import Path

from mr_lister.control.models import PHASE6_MAX_SOURCE_ARTWORK_BYTES
from mr_lister.control.source_artwork import PHASE6_MAX_SOURCE_DIMENSION
from mr_lister.workflow.validation import MAX_ARTWORK_PIXELS

ROOT = Path(__file__).resolve().parents[1]
CONTRACT_PATH = ROOT / "contracts" / "artwork" / "phase6.0.0.json"


def test_frozen_phase6_artwork_contract_matches_runtime_limits_and_boundary() -> None:
    contract = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))

    assert contract["contract_version"] == "6.0.0"
    assert contract["status"] == "frozen"
    assert contract["canonical_artwork"] == {
        "background_policy": (
            "alpha coverage and background fill are seller choices; at least one visible pixel "
            "is required"
        ),
        "content_type": "image/png",
        "format": "png",
        "max_bytes": PHASE6_MAX_SOURCE_ARTWORK_BYTES,
        "max_dimension_pixels": PHASE6_MAX_SOURCE_DIMENSION,
        "max_decoded_pixels": MAX_ARTWORK_PIXELS,
        "normalization_boundary": "browser_before_upload_intent",
        "object_name": "source.png",
    }
    assert contract["geometry"] == {
        "crop": False,
        "distort": False,
        "force_square": False,
        "native_aspect_ratio_preserved": True,
        "pad": False,
        "placement_sizing": "width_driven_with_proportional_height",
        "tall_artwork_fit": (
            "reduce_width_only_when_the_proportional_height_would_exceed_the_print_canvas"
        ),
    }
    assert contract["submission"] == {
        "common_ingestion_path": True,
        "job_cardinality": "one_independent_job_per_file",
        "max_files": 5,
        "min_files": 1,
        "ordered": True,
    }
    assert contract["phase_boundary"] == {
        "etsy_publication": "phase7_only",
        "publication_enabled": False,
        "terminal_phase6_approval_state": "APPROVED",
    }


def test_frozen_phase6_artwork_contract_covers_required_input_matrix() -> None:
    contract = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))
    formats = {entry["format"]: entry for entry in contract["source_formats"]}

    assert set(formats) == {"png", "svg", "jpeg"}
    assert formats["png"]["phase6_closure"] == "required"
    assert formats["svg"]["phase6_closure"] == "required"
    assert formats["jpeg"]["phase6_closure"] == "included_after_low_risk_assessment"
    assert contract["acceptance_matrix"]["backgrounds"] == ["transparent", "opaque"]
    assert contract["acceptance_matrix"]["shapes"] == ["square", "portrait", "landscape"]
    assert contract["acceptance_matrix"]["input_methods"] == [
        "file_picker",
        "single_file_drag_drop",
        "multiple_file_drag_drop",
    ]
    assert contract["deferred_formats"] == [
        {
            "format": "pdf",
            "phase6_blocking": False,
            "reason": (
                "reliable rendering requires a new PDF parser or server-native renderer and a "
                "broader worker, CSP, packaging, and security surface"
            ),
            "smallest_future_contract": "single_page_artwork_pdf_normalized_to_canonical_png",
        }
    ]
