"""Deterministic Phase 6 estimated-proceeds calculation.

Provider observations enter through strict, provider-neutral evidence contracts owned by
the control layer. This module only joins those observations and applies the frozen
``etsy-us-standard-v1`` policy using integer minor units.
"""

from __future__ import annotations

from datetime import datetime

from mr_lister.control.economics import (
    ECONOMICS_FRESHNESS,
    ETSY_US_STANDARD_POLICY_ID,
    EstimatedProceedsRange,
    EtsyUsStandardEstimate,
    EtsyUsStandardFeePolicy,
    ProductCostEvidence,
    ProductVariantCostEvidence,
    VariantProceedsEvidence,
)
from mr_lister.production.printify_shipping import StandardUsShippingEvidence


class EconomicsEvidenceError(ValueError):
    """The supplied provider evidence cannot produce an authoritative estimate."""


class EconomicsEvidenceStaleError(EconomicsEvidenceError):
    """At least one provider component is already outside the 24-hour window."""


def _is_aware(value: datetime) -> bool:
    return value.tzinfo is not None and value.utcoffset() is not None


def percentage_fee_half_up_cents(*, basis_cents: int, basis_points: int) -> int:
    """Round a non-negative percentage to cents with integer half-up arithmetic."""

    if isinstance(basis_cents, bool) or not isinstance(basis_cents, int) or basis_cents < 0:
        raise ValueError("Fee basis must be a non-negative integer number of cents")
    if (
        isinstance(basis_points, bool)
        or not isinstance(basis_points, int)
        or not 0 <= basis_points <= 10_000
    ):
        raise ValueError("Fee rate must be integer basis points between zero and 10000")
    numerator = basis_cents * basis_points
    return (numerator + 5_000) // 10_000


def estimate_etsy_us_standard_proceeds(
    *,
    product_costs: ProductCostEvidence,
    shipping: StandardUsShippingEvidence,
    calculated_at: datetime,
    buyer_shipping_cents: int = 0,
) -> EtsyUsStandardEstimate:
    """Join exact variant sets and calculate the frozen US/USD estimate."""

    if not _is_aware(calculated_at):
        raise EconomicsEvidenceError("Calculation timestamp must be timezone-aware")
    if isinstance(buyer_shipping_cents, bool) or not isinstance(buyer_shipping_cents, int):
        raise EconomicsEvidenceError("Buyer shipping must be integer cents")
    if buyer_shipping_cents != 0:
        raise EconomicsEvidenceError("etsy-us-standard-v1 requires buyer-facing free shipping")
    if product_costs.observed_at > calculated_at or shipping.observed_at > calculated_at:
        raise EconomicsEvidenceError("Provider evidence cannot come from the future")

    costs_by_id = {item.variant_id: item for item in product_costs.variants}
    shipping_by_id = {item.variant_id: item for item in shipping.variants}
    if set(costs_by_id) != set(shipping_by_id):
        raise EconomicsEvidenceError(
            "Product-cost and standard-shipping evidence must cover the same variants"
        )

    fresh_until = min(
        product_costs.observed_at + ECONOMICS_FRESHNESS,
        shipping.observed_at + ECONOMICS_FRESHNESS,
    )
    if fresh_until <= calculated_at:
        raise EconomicsEvidenceStaleError("Provider economics evidence is already stale")

    policy = EtsyUsStandardFeePolicy()
    variants: list[VariantProceedsEvidence] = []
    for variant_id in sorted(costs_by_id):
        cost = costs_by_id[variant_id]
        delivery = shipping_by_id[variant_id]
        fee_basis_cents = cost.retail_price_cents + buyer_shipping_cents
        transaction_fee = percentage_fee_half_up_cents(
            basis_cents=fee_basis_cents,
            basis_points=policy.transaction_fee_basis_points,
        )
        payment_percentage = percentage_fee_half_up_cents(
            basis_cents=fee_basis_cents,
            basis_points=policy.payment_processing_basis_points,
        )
        payment_fee = payment_percentage + policy.payment_processing_fixed_cents
        marketplace_fees = policy.listing_fee_cents + transaction_fee + payment_fee
        proceeds = (
            cost.retail_price_cents
            + buyer_shipping_cents
            - cost.production_cost_cents
            - delivery.first_item_cents
            - marketplace_fees
        )
        variants.append(
            VariantProceedsEvidence(
                variant_id=variant_id,
                retail_price_cents=cost.retail_price_cents,
                production_cost_cents=cost.production_cost_cents,
                production_shipping_cents=delivery.first_item_cents,
                shipping_plan_id=delivery.shipping_plan_id,
                handling_from_days=delivery.handling_from_days,
                handling_to_days=delivery.handling_to_days,
                transaction_fee_cents=transaction_fee,
                payment_processing_percentage_cents=payment_percentage,
                payment_processing_fee_cents=payment_fee,
                total_marketplace_fees_cents=marketplace_fees,
                estimated_proceeds_cents=proceeds,
            )
        )

    minimum = min(item.estimated_proceeds_cents for item in variants)
    maximum = max(item.estimated_proceeds_cents for item in variants)
    proceeds_range = EstimatedProceedsRange(
        minimum_cents=minimum,
        maximum_cents=maximum,
        minimum_variant_ids=tuple(
            item.variant_id for item in variants if item.estimated_proceeds_cents == minimum
        ),
        maximum_variant_ids=tuple(
            item.variant_id for item in variants if item.estimated_proceeds_cents == maximum
        ),
    )
    return EtsyUsStandardEstimate(
        policy=policy,
        product_sync_fingerprint=product_costs.product_sync_fingerprint,
        product_cost_evidence_fingerprint=product_costs.fingerprint,
        shipping_evidence_fingerprint=shipping.fingerprint,
        blueprint_id=shipping.blueprint_id,
        print_provider_id=shipping.print_provider_id,
        shipping_source_path=shipping.source_path,
        product_cost_observed_at=product_costs.observed_at,
        shipping_observed_at=shipping.observed_at,
        calculated_at=calculated_at,
        fresh_until=fresh_until,
        variants=tuple(variants),
        proceeds_range=proceeds_range,
    )


__all__ = [
    "ECONOMICS_FRESHNESS",
    "ETSY_US_STANDARD_POLICY_ID",
    "EconomicsEvidenceError",
    "EconomicsEvidenceStaleError",
    "EstimatedProceedsRange",
    "EtsyUsStandardEstimate",
    "EtsyUsStandardFeePolicy",
    "ProductCostEvidence",
    "ProductVariantCostEvidence",
    "VariantProceedsEvidence",
    "estimate_etsy_us_standard_proceeds",
    "percentage_fee_half_up_cents",
]
