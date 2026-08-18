from __future__ import annotations

import json
from pathlib import Path

from mr_lister.contracts import ProductProfile

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "synthetic_product_profile.json"


def test_synthetic_product_profile_matches_contract() -> None:
    profile = ProductProfile.model_validate_json(FIXTURE_PATH.read_text())

    assert profile.profile_id == "synthetic_gildan_5000"
    assert profile.retail_price_cents == 2499
    assert profile.publish_enabled is False


def test_fixture_identifiers_are_clearly_non_live() -> None:
    payload = json.loads(FIXTURE_PATH.read_text())

    assert payload["profile_id"].startswith("synthetic_")
    assert all(identifier >= 900000 for identifier in payload["variant_ids"])
