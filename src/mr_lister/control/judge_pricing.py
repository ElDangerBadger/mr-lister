"""Optional, exact-owner commercial policy for the temporary judge workspace."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass

from mr_lister.control.pricing import ReviewPricing

ENVIRONMENT_KEY = "MR_LISTER_JUDGE_PRICING_POLICY"


class JudgePricingPolicyError(ValueError):
    """The review needs an explicit seller revision before it may proceed."""


@dataclass(frozen=True)
class JudgePricingPolicy:
    owner_id: str
    default_price_cents: int = 3500
    minimum_price_cents: int | None = None

    def __post_init__(self) -> None:
        if (
            not isinstance(self.owner_id, str)
            or re.fullmatch(r"[a-f0-9]{64}", self.owner_id) is None
        ):
            raise ValueError("Judge pricing policy requires one exact owner ID")
        values = [self.default_price_cents]
        if self.minimum_price_cents is not None:
            values.append(self.minimum_price_cents)
        for value in values:
            if type(value) is not int or not 1 <= value <= 999_999:
                raise ValueError("Judge pricing policy prices must be bounded integer cents")
        if (
            self.minimum_price_cents is not None
            and self.default_price_cents < self.minimum_price_cents
        ):
            raise ValueError("Judge default price must satisfy the minimum price")

    def defaults_for(self, owner_id: str) -> ReviewPricing | None:
        if owner_id != self.owner_id:
            return None
        return ReviewPricing(retail_price_cents=self.default_price_cents, free_shipping=False)

    def require_allowed(self, owner_id: str, pricing: ReviewPricing | None) -> None:
        if owner_id != self.owner_id:
            return
        if pricing is None or pricing.free_shipping:
            raise JudgePricingPolicyError(
                "Judge listings require standard shipping. Turn off free shipping and save "
                "before approval. If already approved, start a new listing."
            )
        if self.minimum_price_cents is not None and any(
            value < self.minimum_price_cents
            for value in (
                pricing.retail_price_cents,
                *(variant.retail_price_cents for variant in pricing.variant_prices),
            )
        ):
            raise JudgePricingPolicyError(
                f"Judge listing and variant prices must be at least "
                f"${self.minimum_price_cents / 100:.2f}. Save compliant pricing before approval "
                "or publication."
            )


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Judge pricing policy cannot contain duplicate fields")
        result[key] = value
    return result


def load_judge_pricing_policy(environment: Mapping[str, object]) -> JudgePricingPolicy | None:
    """Absence disables the policy; malformed present configuration fails startup."""

    if ENVIRONMENT_KEY not in environment:
        return None
    raw = environment[ENVIRONMENT_KEY]
    if not isinstance(raw, str) or not 1 <= len(raw) <= 4096:
        raise ValueError("Judge pricing policy must be a JSON object")
    try:
        payload = json.loads(raw, object_pairs_hook=_unique_object)
    except (TypeError, ValueError) as exc:
        raise ValueError("Judge pricing policy must be a strict JSON object") from exc
    if (
        not isinstance(payload, dict)
        or "owner_id" not in payload
        or set(payload) - {"owner_id", "default_price_cents", "minimum_price_cents"}
    ):
        raise ValueError("Judge pricing policy has missing or unsupported fields")
    return JudgePricingPolicy(**payload)
