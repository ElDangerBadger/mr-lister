from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from mr_lister.contracts import ProductProfile
from mr_lister.production.economics import (
    EconomicsEvidenceError,
    EconomicsEvidenceStaleError,
    ProductCostEvidence,
    ProductVariantCostEvidence,
    estimate_etsy_us_standard_proceeds,
    percentage_fee_half_up_cents,
)
from mr_lister.production.printify_shipping import parse_standard_us_shipping

NOW = datetime(2026, 8, 22, 12, 0, tzinfo=UTC)
PROFILE_PATH = Path("config/product_profiles/gildan_64000_swiftpod.json")
SYNC_FINGERPRINT = "a" * 64


def _shipping_resource(variant_id: int, first_item_cents: int) -> dict[str, object]:
    return {
        "type": "variant_shipping_standard_us",
        "id": str(variant_id),
        "attributes": {
            "shippingType": "standard",
            "country": {"code": "US"},
            "variantId": variant_id,
            "shippingPlanId": f"plan-{variant_id}",
            "handlingTime": {"from": 4, "to": 8},
            "shippingCost": {
                "firstItem": {"amount": first_item_cents, "currency": "USD"},
                "additionalItems": {"amount": 219, "currency": "USD"},
            },
        },
    }


def _shipping(
    costs: dict[int, int],
    *,
    observed_at: datetime = NOW - timedelta(minutes=1),
    expected_order: tuple[int, ...] | None = None,
):
    return parse_standard_us_shipping(
        {"data": [_shipping_resource(variant_id, cost) for variant_id, cost in costs.items()]},
        blueprint_id=145,
        print_provider_id=39,
        expected_variant_ids=expected_order or tuple(costs),
        observed_at=observed_at,
    )


def _product_costs(
    costs: dict[int, tuple[int, int]],
    *,
    observed_at: datetime = NOW - timedelta(minutes=2),
) -> ProductCostEvidence:
    return ProductCostEvidence(
        product_sync_fingerprint=SYNC_FINGERPRINT,
        observed_at=observed_at,
        variants=tuple(
            ProductVariantCostEvidence(
                variant_id=variant_id,
                retail_price_cents=retail,
                production_cost_cents=production,
            )
            for variant_id, (retail, production) in costs.items()
        ),
    )


def test_integer_half_up_percentage_never_uses_bankers_rounding_or_float() -> None:
    assert percentage_fee_half_up_cents(basis_cents=100, basis_points=650) == 7
    assert percentage_fee_half_up_cents(basis_cents=50, basis_points=300) == 2
    assert percentage_fee_half_up_cents(basis_cents=2999, basis_points=650) == 195
    assert percentage_fee_half_up_cents(basis_cents=2999, basis_points=300) == 90


def test_frozen_policy_calculates_exact_fee_components_and_proceeds_range() -> None:
    estimate = estimate_etsy_us_standard_proceeds(
        product_costs=_product_costs(
            {
                1000: (2999, 1100),
                1001: (2999, 1275),
            }
        ),
        shipping=_shipping({1000: 399, 1001: 499}),
        calculated_at=NOW,
    )

    assert estimate.policy.policy_id == "etsy-us-standard-v1"
    assert estimate.policy.listing_fee_cents == 20
    assert estimate.policy.transaction_fee_basis_points == 650
    assert estimate.policy.payment_processing_basis_points == 300
    assert estimate.policy.payment_processing_fixed_cents == 25
    assert estimate.policy.verified_on.isoformat() == "2026-08-21"
    assert estimate.blueprint_id == 145
    assert estimate.print_provider_id == 39
    assert estimate.shipping_source_path.endswith("/shipping/standard.json")
    first, second = estimate.variants
    assert first.variant_id == 1000
    assert first.transaction_fee_cents == 195
    assert first.payment_processing_percentage_cents == 90
    assert first.payment_processing_fee_cents == 115
    assert first.total_marketplace_fees_cents == 330
    assert first.estimated_proceeds_cents == 1170
    assert second.estimated_proceeds_cents == 895
    assert estimate.proceeds_range.minimum_cents == 895
    assert estimate.proceeds_range.minimum_variant_ids == (1001,)
    assert estimate.proceeds_range.maximum_cents == 1170
    assert estimate.proceeds_range.maximum_variant_ids == (1000,)
    assert estimate.fresh_until == NOW - timedelta(minutes=2) + timedelta(hours=24)
    assert len(estimate.fingerprint) == 64


def test_all_thirty_configured_variants_receive_per_variant_evidence() -> None:
    profile = ProductProfile.model_validate_json(PROFILE_PATH.read_text())
    configured_count = len(profile.colors) * len(profile.sizes)
    variant_ids = tuple(range(10_000, 10_000 + configured_count))
    product_costs = {
        variant_id: (profile.retail_price_cents, 1100 + offset)
        for offset, variant_id in enumerate(reversed(variant_ids))
    }
    shipping_costs = {
        variant_id: 399 + (offset % 3) * 50 for offset, variant_id in enumerate(variant_ids)
    }

    estimate = estimate_etsy_us_standard_proceeds(
        product_costs=_product_costs(product_costs),
        shipping=_shipping(shipping_costs),
        calculated_at=NOW,
    )

    assert configured_count == 30
    assert len(estimate.variants) == 30
    assert tuple(item.variant_id for item in estimate.variants) == variant_ids
    assert all(item.buyer_shipping_cents == 0 for item in estimate.variants)


def test_logically_identical_variant_order_produces_the_same_fingerprint() -> None:
    forward_costs = _product_costs({1000: (2999, 1100), 1001: (2999, 1200)})
    reverse_costs = _product_costs({1001: (2999, 1200), 1000: (2999, 1100)})
    forward_shipping = _shipping({1000: 399, 1001: 499})
    reverse_shipping = _shipping(
        {1000: 399, 1001: 499},
        expected_order=(1001, 1000),
    )

    first = estimate_etsy_us_standard_proceeds(
        product_costs=forward_costs,
        shipping=forward_shipping,
        calculated_at=NOW,
    )
    second = estimate_etsy_us_standard_proceeds(
        product_costs=reverse_costs,
        shipping=reverse_shipping,
        calculated_at=NOW,
    )

    assert forward_costs.fingerprint == reverse_costs.fingerprint
    assert forward_shipping.fingerprint == reverse_shipping.fingerprint
    assert first == second
    assert first.fingerprint == second.fingerprint


def test_variant_set_mismatch_fails_closed() -> None:
    with pytest.raises(EconomicsEvidenceError, match="same variants"):
        estimate_etsy_us_standard_proceeds(
            product_costs=_product_costs({1000: (2999, 1100), 1001: (2999, 1200)}),
            shipping=_shipping({1000: 399}),
            calculated_at=NOW,
        )


def test_stale_evidence_cannot_be_materialized_as_a_fresh_estimate() -> None:
    with pytest.raises(EconomicsEvidenceStaleError, match="stale"):
        estimate_etsy_us_standard_proceeds(
            product_costs=_product_costs(
                {1000: (2999, 1100)},
                observed_at=NOW - timedelta(hours=24),
            ),
            shipping=_shipping({1000: 399}),
            calculated_at=NOW,
        )


def test_future_evidence_and_nonzero_buyer_shipping_fail_closed() -> None:
    with pytest.raises(EconomicsEvidenceError, match="free shipping"):
        estimate_etsy_us_standard_proceeds(
            product_costs=_product_costs({1000: (2999, 1100)}),
            shipping=_shipping({1000: 399}),
            calculated_at=NOW,
            buyer_shipping_cents=1,
        )

    with pytest.raises(EconomicsEvidenceError, match="future"):
        estimate_etsy_us_standard_proceeds(
            product_costs=_product_costs(
                {1000: (2999, 1100)},
                observed_at=NOW + timedelta(seconds=1),
            ),
            shipping=_shipping({1000: 399}),
            calculated_at=NOW,
        )


def test_duplicate_product_cost_variant_is_rejected_at_the_dto_boundary() -> None:
    variant = ProductVariantCostEvidence(
        variant_id=1000,
        retail_price_cents=2999,
        production_cost_cents=1100,
    )

    with pytest.raises(ValidationError, match="must be unique"):
        ProductCostEvidence(
            product_sync_fingerprint=SYNC_FINGERPRINT,
            observed_at=NOW,
            variants=(variant, variant),
        )


@pytest.mark.parametrize(
    ("basis_cents", "basis_points"),
    [
        (-1, 650),
        (True, 650),
        (100, -1),
        (100, 10_001),
        (100, True),
    ],
)
def test_fee_helper_rejects_non_integer_or_out_of_policy_inputs(
    basis_cents: int,
    basis_points: int,
) -> None:
    with pytest.raises(ValueError):
        percentage_fee_half_up_cents(
            basis_cents=basis_cents,
            basis_points=basis_points,
        )
