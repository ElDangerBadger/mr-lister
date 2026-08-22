"""Provider-neutral, immutable Phase 6 economics evidence contracts."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from mr_lister.control.fingerprints import canonical_fingerprint

ETSY_US_STANDARD_POLICY_ID = "etsy-us-standard-v1"
ECONOMICS_FRESHNESS = timedelta(hours=24)

_Fingerprint = Annotated[str, StringConstraints(pattern=r"^[a-f0-9]{64}$")]


def _is_aware(value: datetime) -> bool:
    return value.tzinfo is not None and value.utcoffset() is not None


class _EconomicsModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ProductVariantCostEvidence(_EconomicsModel):
    """Current production cost for one enabled synchronized variant."""

    variant_id: int = Field(gt=0, strict=True)
    retail_price_cents: int = Field(gt=0, strict=True)
    production_cost_cents: int = Field(ge=0, strict=True)


class ProductCostEvidence(_EconomicsModel):
    """Current product-cost observation bound to an immutable product synchronization."""

    evidence_version: Literal["1.0.0"] = "1.0.0"
    product_sync_fingerprint: _Fingerprint
    observed_at: datetime
    variants: tuple[ProductVariantCostEvidence, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def evidence_is_coherent(self) -> ProductCostEvidence:
        if not _is_aware(self.observed_at):
            raise ValueError("Product-cost evidence timestamp must be timezone-aware")
        variant_ids = tuple(item.variant_id for item in self.variants)
        if len(variant_ids) != len(set(variant_ids)):
            raise ValueError("Product-cost evidence variants must be unique")
        return self

    @property
    def fingerprint(self) -> str:
        payload = self.model_dump(mode="json")
        payload["variants"] = sorted(payload["variants"], key=lambda item: item["variant_id"])
        return canonical_fingerprint(payload)


class EtsyUsStandardFeePolicy(_EconomicsModel):
    """Frozen and reviewable fee assumptions; constants are not caller-configurable."""

    policy_id: Literal["etsy-us-standard-v1"] = ETSY_US_STANDARD_POLICY_ID
    currency: Literal["USD"] = "USD"
    seller_bank_country: Literal["US"] = "US"
    buyer_destination_country: Literal["US"] = "US"
    listing_fee_cents: Literal[20] = 20
    transaction_fee_basis_points: Literal[650] = 650
    payment_processing_basis_points: Literal[300] = 300
    payment_processing_fixed_cents: Literal[25] = 25
    verified_on: date = date(2026, 8, 21)

    @model_validator(mode="after")
    def verification_date_is_frozen(self) -> EtsyUsStandardFeePolicy:
        if self.verified_on != date(2026, 8, 21):
            raise ValueError("Fee-policy verification date changed")
        return self

    @property
    def fingerprint(self) -> str:
        return canonical_fingerprint(self)


class VariantProceedsEvidence(_EconomicsModel):
    """Complete integer-cent calculation for one enabled product variant."""

    variant_id: int = Field(gt=0, strict=True)
    currency: Literal["USD"] = "USD"
    retail_price_cents: int = Field(gt=0, strict=True)
    buyer_shipping_cents: Literal[0] = 0
    production_cost_cents: int = Field(ge=0, strict=True)
    production_shipping_cents: int = Field(ge=0, strict=True)
    shipping_plan_id: str = Field(min_length=1, max_length=128)
    handling_from_days: int = Field(ge=0, strict=True)
    handling_to_days: int = Field(ge=0, strict=True)
    listing_fee_cents: Literal[20] = 20
    transaction_fee_cents: int = Field(ge=0, strict=True)
    payment_processing_percentage_cents: int = Field(ge=0, strict=True)
    payment_processing_fixed_cents: Literal[25] = 25
    payment_processing_fee_cents: int = Field(ge=0, strict=True)
    total_marketplace_fees_cents: int = Field(ge=0, strict=True)
    estimated_proceeds_cents: int = Field(strict=True)

    @model_validator(mode="after")
    def arithmetic_is_self_consistent(self) -> VariantProceedsEvidence:
        if self.handling_to_days < self.handling_from_days:
            raise ValueError("Variant handling-time range is reversed")
        if self.payment_processing_fee_cents != (
            self.payment_processing_percentage_cents + self.payment_processing_fixed_cents
        ):
            raise ValueError("Payment-processing fee components do not add up")
        if self.total_marketplace_fees_cents != (
            self.listing_fee_cents + self.transaction_fee_cents + self.payment_processing_fee_cents
        ):
            raise ValueError("Marketplace fee components do not add up")
        expected_proceeds = (
            self.retail_price_cents
            + self.buyer_shipping_cents
            - self.production_cost_cents
            - self.production_shipping_cents
            - self.total_marketplace_fees_cents
        )
        if self.estimated_proceeds_cents != expected_proceeds:
            raise ValueError("Estimated proceeds do not match their evidence")
        return self


class EstimatedProceedsRange(_EconomicsModel):
    minimum_cents: int = Field(strict=True)
    maximum_cents: int = Field(strict=True)
    minimum_variant_ids: tuple[int, ...] = Field(min_length=1)
    maximum_variant_ids: tuple[int, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def range_is_coherent(self) -> EstimatedProceedsRange:
        if self.maximum_cents < self.minimum_cents:
            raise ValueError("Estimated-proceeds range is reversed")
        if len(set(self.minimum_variant_ids)) != len(self.minimum_variant_ids):
            raise ValueError("Minimum proceeds variants must be unique")
        if len(set(self.maximum_variant_ids)) != len(self.maximum_variant_ids):
            raise ValueError("Maximum proceeds variants must be unique")
        return self


class EtsyUsStandardEstimate(_EconomicsModel):
    """Immutable evidence ready to be fingerprinted into a pricing snapshot."""

    evidence_version: Literal["1.0.0"] = "1.0.0"
    policy: EtsyUsStandardFeePolicy
    product_sync_fingerprint: _Fingerprint
    product_cost_evidence_fingerprint: _Fingerprint
    shipping_evidence_fingerprint: _Fingerprint
    blueprint_id: int = Field(gt=0, strict=True)
    print_provider_id: int = Field(gt=0, strict=True)
    shipping_source_path: str = Field(min_length=1, max_length=300)
    product_cost_observed_at: datetime
    shipping_observed_at: datetime
    calculated_at: datetime
    fresh_until: datetime
    variants: tuple[VariantProceedsEvidence, ...] = Field(min_length=1)
    proceeds_range: EstimatedProceedsRange

    @model_validator(mode="after")
    def estimate_is_coherent(self) -> EtsyUsStandardEstimate:
        timestamps = (
            self.product_cost_observed_at,
            self.shipping_observed_at,
            self.calculated_at,
            self.fresh_until,
        )
        if any(not _is_aware(value) for value in timestamps):
            raise ValueError("Economics timestamps must be timezone-aware")
        if self.product_cost_observed_at > self.calculated_at:
            raise ValueError("Product-cost evidence cannot come from the future")
        if self.shipping_observed_at > self.calculated_at:
            raise ValueError("Shipping evidence cannot come from the future")
        if self.fresh_until <= self.calculated_at:
            raise ValueError("Economics evidence must still be fresh when calculated")
        variant_ids = tuple(item.variant_id for item in self.variants)
        if variant_ids != tuple(sorted(variant_ids)) or len(variant_ids) != len(set(variant_ids)):
            raise ValueError("Estimated variants must be unique and canonically ordered")
        proceeds = {item.variant_id: item.estimated_proceeds_cents for item in self.variants}
        minimum = min(proceeds.values())
        maximum = max(proceeds.values())
        if self.proceeds_range.minimum_cents != minimum:
            raise ValueError("Minimum proceeds does not match variant evidence")
        if self.proceeds_range.maximum_cents != maximum:
            raise ValueError("Maximum proceeds does not match variant evidence")
        if self.proceeds_range.minimum_variant_ids != tuple(
            variant_id for variant_id, value in proceeds.items() if value == minimum
        ):
            raise ValueError("Minimum variant set does not match variant evidence")
        if self.proceeds_range.maximum_variant_ids != tuple(
            variant_id for variant_id, value in proceeds.items() if value == maximum
        ):
            raise ValueError("Maximum variant set does not match variant evidence")
        return self

    @property
    def fingerprint(self) -> str:
        return canonical_fingerprint(self)


__all__ = [
    "ECONOMICS_FRESHNESS",
    "ETSY_US_STANDARD_POLICY_ID",
    "EstimatedProceedsRange",
    "EtsyUsStandardEstimate",
    "EtsyUsStandardFeePolicy",
    "ProductCostEvidence",
    "ProductVariantCostEvidence",
    "VariantProceedsEvidence",
]
