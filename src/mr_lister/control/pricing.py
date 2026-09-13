"""Seller-owned commercial choices, separate from the pinned production profile."""

from __future__ import annotations

from typing import Annotated

from pydantic import Field, StrictBool, StrictInt, StringConstraints, model_validator

from mr_lister.contracts import ContractModel, ProductProfile

RetailPriceCents = Annotated[StrictInt, Field(ge=1, le=999_999)]
VariantSelector = Annotated[str, StringConstraints(strict=True, min_length=1, max_length=120)]


class VariantPrice(ContractModel):
    color: VariantSelector
    size: VariantSelector
    retail_price_cents: RetailPriceCents


class ReviewPricing(ContractModel):
    """USD item price and sparse, exact color/size overrides for this review only."""

    retail_price_cents: RetailPriceCents
    variant_prices: tuple[VariantPrice, ...] = Field(default=(), max_length=900)
    free_shipping: StrictBool

    @model_validator(mode="after")
    def unique_sorted_overrides(self) -> ReviewPricing:
        pairs = [(item.color, item.size) for item in self.variant_prices]
        if len(set(pairs)) != len(pairs):
            raise ValueError("Variant price overrides must have unique color and size pairs")
        object.__setattr__(
            self,
            "variant_prices",
            tuple(sorted(self.variant_prices, key=lambda item: (item.color, item.size))),
        )
        return self


def effective_review_pricing(
    pricing: ReviewPricing | None, profile: ProductProfile
) -> ReviewPricing:
    """Resolve legacy defaults and reject selectors outside exact profile authority."""

    effective = pricing or ReviewPricing(
        retail_price_cents=profile.retail_price_cents,
        free_shipping=profile.buyer_shipping_cents == 0,
    )
    allowed_pairs = {(color, size) for color in profile.colors for size in profile.sizes}
    if any((item.color, item.size) not in allowed_pairs for item in effective.variant_prices):
        raise ValueError("Variant price overrides must belong to the selected product")
    return effective


def retail_price_for_variant(pricing: ReviewPricing, *, color: str, size: str) -> int:
    """Return the exact override or the item-wide fallback, in integer USD cents."""

    return next(
        (
            item.retail_price_cents
            for item in pricing.variant_prices
            if item.color == color and item.size == size
        ),
        pricing.retail_price_cents,
    )
