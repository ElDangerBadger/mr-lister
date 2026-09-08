"""Immutable v1 copy references; original candidate pools were not captured."""

from __future__ import annotations

import json
from hashlib import sha256
from pathlib import Path

import pytest

from mr_lister.contracts import ArtworkAnalysis, ListingIntelligence
from mr_lister.intelligence.prompts import prompt_bundle_for

REFERENCE_PATH = Path(__file__).parent / "fixtures" / "etsy_seo_v1_reference.json"
REFERENCE = json.loads(REFERENCE_PATH.read_text(encoding="utf-8"))
EXPECTED_REFERENCE_SHA256 = "035e451c5ef528dc51b8be220e8d94724de6e4f3870d1748245ec46f077f8e01"
EXPECTED_PROMPT_VERSION = "2026-09-08.1-etsy-seo-candidate"
EXPECTED_PROMPT_FINGERPRINT = "d72948fe5a7ea155f6fa5283428ffcd26011e1e871342086ecf89e27398c56c2"
EXPECTED_PROSE_SHA256 = {
    "illustrated_badger_subject": (
        "0f683d9371cdf4447618b42daaf2dfdf9b2ec0f64fc423be1478aee6d4d3a6e2"
    ),
    "typography_maker_motto": "c1f6226b0149f104738c00417fcf41a362dc68d8af711ed767e467863b2bd477",
    "abstract_wave_mountain": "1b1eb07ef9fdf93aa1bd7a1aef6ff46d51bdabb3c5258a7b8c2c419a81b25e21",
    "transparent_moon_moth": "8dd6c1366a874d46d42225916adb0fb4850ff0aa322809a311bdf967b884a7ef",
    "visible_prompt_injection_robot": (
        "efc3582976f8b50cf6ae2c24965e353ebc8905c5759010ce427a4ce3037be636"
    ),
    "holdout_owl_lantern": "2f0a35febd2b8737614bffb5aa3a44a27d5c4a9e159a29e8a90861afb4a65f44",
    "holdout_gardening_motto": "1bc179b984fce2494240ed8e01531c5c40fca48769d126cc93963bb052ea2382",
    "holdout_transparent_jellyfish": (
        "5561694011e38f762de87434541dc9562d20ea4c62faa763a618908e89dc1e69"
    ),
    "holdout_v6_fox_telescope": "45e17ae08846b8977bc80e10c523dbefdaad74ce15f484563730044b9ed7c1fc",
    "holdout_v6_bloom_motto": "9df40404cc3de67c33b24f407bbb43c344d4bc758eb7fc8ad41f561af1b2515c",
    "holdout_v6_transparent_seahorse": (
        "17db494e5ee8e72af46e236d6a7215dd48a539666c2de80ef6b9f94a50e86f90"
    ),
}


def test_historical_reference_bytes_remain_unchanged() -> None:
    # Pins all recorded fields, including historical tags and source artifact hashes.
    assert sha256(REFERENCE_PATH.read_bytes()).hexdigest() == EXPECTED_REFERENCE_SHA256


def test_reference_preserves_all_eleven_existing_artwork_cases() -> None:
    manifest_path = Path(__file__).parent / "evaluation" / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    expected_artworks = {case["id"]: case["sha256"] for case in manifest["cases"]}
    cases = REFERENCE["cases"]

    assert len(cases) == 11
    assert {case["case_id"] for case in cases} == set(EXPECTED_PROSE_SHA256)
    assert {case["case_id"]: case["artwork_sha256"] for case in cases} == expected_artworks
    for case in cases:
        assert case["source_file"] == f"{case['case_id']}-trial-1.json"
        assert len(case["source_file_sha256"]) == 64
        assert int(case["source_file_sha256"], 16) >= 0


def test_reference_is_bound_to_the_unchanged_candidate_v1_bundle() -> None:
    provenance = REFERENCE["provenance"]

    assert provenance["run_id"] == "gemma-etsy-seo-candidate-20260908"
    assert provenance["model_id"] == "google.gemma-3-27b-it"
    assert provenance["prompt_version"] == EXPECTED_PROMPT_VERSION
    assert provenance["prompt_fingerprint"] == EXPECTED_PROMPT_FINGERPRINT
    assert prompt_bundle_for(EXPECTED_PROMPT_VERSION).fingerprint == EXPECTED_PROMPT_FINGERPRINT


@pytest.mark.parametrize("case_id", EXPECTED_PROSE_SHA256)
def test_reference_preserves_all_analysis_and_non_tag_listing_fields(case_id: str) -> None:
    case = next(case for case in REFERENCE["cases"] if case["case_id"] == case_id)
    accepted = case["accepted_output"]
    prose = {
        "analysis": accepted["analysis"],
        "listing": {key: value for key, value in accepted["listing"].items() if key != "tags"},
    }
    serialized = json.dumps(prose, sort_keys=True, separators=(",", ":"), ensure_ascii=False)

    assert sha256(serialized.encode("utf-8")).hexdigest() == EXPECTED_PROSE_SHA256[case_id]
    assert case["prose_sha256"] == EXPECTED_PROSE_SHA256[case_id]
    validated_analysis = ArtworkAnalysis.model_validate(accepted["analysis"])
    assert validated_analysis.model_dump(mode="json") == accepted["analysis"]


def test_reference_retains_original_final_tags_without_claiming_new_selector_results() -> None:
    assert REFERENCE["reference_role"] == "candidate_v1_qualitative_copy_reference"
    assert REFERENCE["candidate_pools_available"] is False
    assert REFERENCE["comparison_status"] == "unavailable_without_original_candidate_pools"

    for case in REFERENCE["cases"]:
        accepted = case["accepted_output"]
        listing = ListingIntelligence.model_validate(accepted["listing"])
        assert list(listing.tags) == accepted["listing"]["tags"]
        assert len(listing.tags) == 13
        assert "tag_candidates" not in accepted["listing"]
        assert "tag_candidates" not in case
        assert "new_selector_tags" not in case
