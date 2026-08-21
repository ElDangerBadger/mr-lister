"""Explicitly gated, read-only Printify catalog preflight."""

from __future__ import annotations

import json
import os
from pathlib import Path

from mr_lister.production import PrintifyCatalogClient, PrintifyProductProfile

PROFILE_PATH = Path("config/product_profiles/gildan_64000_swiftpod.json")
LIVE_GATE = "MR_LISTER_RUN_LIVE_PRINTIFY"


def main() -> int:
    if os.environ.get(LIVE_GATE) != "1":
        raise SystemExit(f"Refusing live preflight unless {LIVE_GATE}=1")
    token = os.environ.get("PRINTIFY_API_TOKEN", "")
    shop_id_text = os.environ.get("PRINTIFY_SHOP_ID", "")
    if not token:
        raise SystemExit("PRINTIFY_API_TOKEN is not loaded")
    try:
        shop_id = int(shop_id_text)
    except ValueError as error:
        raise SystemExit("PRINTIFY_SHOP_ID must be a positive integer") from error
    if shop_id <= 0:
        raise SystemExit("PRINTIFY_SHOP_ID must be a positive integer")

    profile = PrintifyProductProfile.model_validate_json(PROFILE_PATH.read_text())
    client = PrintifyCatalogClient(token_provider=lambda: token)
    resolved = client.preflight(shop_id=shop_id, profile=profile)
    print(
        json.dumps(
            {
                "profile_id": resolved.profile_id,
                "profile_version": resolved.profile_version,
                "shop_id": resolved.shop_id,
                "blueprint_id": resolved.blueprint_id,
                "print_provider_id": resolved.print_provider_id,
                "variant_count": len(resolved.variants),
                "variant_ids": [variant.variant_id for variant in resolved.variants],
                "placement_groups": sorted(
                    {variant.placement_group_id for variant in resolved.variants}
                ),
                "retail_price_cents": sorted(
                    {variant.retail_price_cents for variant in resolved.variants}
                ),
                "write_capability": False,
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
