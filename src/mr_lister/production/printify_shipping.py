"""Read-only Printify V2 standard-US shipping evidence.

The Phase 6 economics path needs current fulfillment shipping without acquiring any
provider mutation capability.  This module therefore exposes exactly one Printify
operation: the documented V2 standard-shipping ``GET`` for a blueprint/provider pair.
It validates the complete response before selecting the configured variant IDs.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from typing import Annotated, Any, Literal
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, OpenerDirector, Request, build_opener

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    ValidationError,
    model_validator,
)

from mr_lister.control.fingerprints import canonical_fingerprint
from mr_lister.production.printify import (
    PrintifyAuthenticationError,
    PrintifyCatalogMismatchError,
    PrintifyHttpResponse,
    PrintifyTransport,
    PrintifyUnavailableError,
)

PRINTIFY_V2_API_ORIGIN = "https://api.printify.com"
PRINTIFY_V2_API_BASE_URL = f"{PRINTIFY_V2_API_ORIGIN}/v2/"
MAX_STANDARD_SHIPPING_RESPONSE_BYTES = 2 * 1024 * 1024

_STANDARD_SHIPPING_PATH = re.compile(
    r"^/v2/catalog/blueprints/([1-9][0-9]*)/print_providers/"
    r"([1-9][0-9]*)/shipping/standard\.json$"
)
_ShippingPlanId = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=128),
]
_ResourceId = Annotated[str, StringConstraints(pattern=r"^[1-9][0-9]*$")]
_Fingerprint = Annotated[str, StringConstraints(pattern=r"^[a-f0-9]{64}$")]


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)


class _PrintifyMoney(_StrictModel):
    amount: int = Field(ge=0, strict=True)
    currency: Literal["USD"]


class _PrintifyCountry(_StrictModel):
    code: Literal["US"]


class _PrintifyHandlingTime(_StrictModel):
    from_days: int = Field(alias="from", ge=0, strict=True)
    to_days: int = Field(alias="to", ge=0, strict=True)

    @model_validator(mode="after")
    def range_is_ordered(self) -> _PrintifyHandlingTime:
        if self.to_days < self.from_days:
            raise ValueError("Printify handling-time range is reversed")
        return self


class _PrintifyShippingCost(_StrictModel):
    first_item: _PrintifyMoney = Field(alias="firstItem")
    additional_items: _PrintifyMoney = Field(alias="additionalItems")


class _PrintifyStandardAttributes(_StrictModel):
    shipping_type: Literal["standard"] = Field(alias="shippingType")
    country: _PrintifyCountry
    variant_id: int = Field(alias="variantId", gt=0, strict=True)
    shipping_plan_id: _ShippingPlanId = Field(alias="shippingPlanId")
    handling_time: _PrintifyHandlingTime = Field(alias="handlingTime")
    shipping_cost: _PrintifyShippingCost = Field(alias="shippingCost")


class _PrintifyStandardResource(_StrictModel):
    type: Literal["variant_shipping_standard_us"]
    id: _ResourceId
    attributes: _PrintifyStandardAttributes

    @model_validator(mode="after")
    def resource_id_matches_variant(self) -> _PrintifyStandardResource:
        if self.id != str(self.attributes.variant_id):
            raise ValueError("Printify shipping resource ID does not match its variant ID")
        return self


class _PrintifyStandardEnvelope(_StrictModel):
    data: tuple[_PrintifyStandardResource, ...] = Field(min_length=1)


class StandardUsVariantShipping(_StrictModel):
    """Normalized evidence for one configured Printify variant."""

    variant_id: int = Field(gt=0, strict=True)
    resource_id: _ResourceId
    shipping_plan_id: _ShippingPlanId
    handling_from_days: int = Field(ge=0, strict=True)
    handling_to_days: int = Field(ge=0, strict=True)
    first_item_cents: int = Field(ge=0, strict=True)
    additional_item_cents: int = Field(ge=0, strict=True)
    currency: Literal["USD"] = "USD"


class StandardUsShippingEvidence(_StrictModel):
    """Validated, configured-variant subset of one live Printify V2 response."""

    evidence_version: Literal["1.0.0"] = "1.0.0"
    blueprint_id: int = Field(gt=0, strict=True)
    print_provider_id: int = Field(gt=0, strict=True)
    shipping_method: Literal["standard"] = "standard"
    destination_country: Literal["US"] = "US"
    source_path: str = Field(pattern=_STANDARD_SHIPPING_PATH.pattern)
    observed_at: datetime
    variants: tuple[StandardUsVariantShipping, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def evidence_is_coherent(self) -> StandardUsShippingEvidence:
        if self.observed_at.tzinfo is None or self.observed_at.utcoffset() is None:
            raise ValueError("Shipping evidence timestamp must be timezone-aware")
        variant_ids = tuple(item.variant_id for item in self.variants)
        if len(variant_ids) != len(set(variant_ids)):
            raise ValueError("Shipping evidence variants must be unique")
        expected_path = standard_us_shipping_path(
            blueprint_id=self.blueprint_id,
            print_provider_id=self.print_provider_id,
        )
        if self.source_path != expected_path:
            raise ValueError("Shipping evidence path does not match its catalog authority")
        return self

    @property
    def fingerprint(self) -> str:
        payload = self.model_dump(mode="json")
        payload["variants"] = sorted(payload["variants"], key=lambda item: item["variant_id"])
        return canonical_fingerprint(payload)


def _positive_id(value: int, *, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{label} must be a positive integer")
    return value


def standard_us_shipping_path(*, blueprint_id: int, print_provider_id: int) -> str:
    """Build the only relative Printify route authorized by this module."""

    blueprint_id = _positive_id(blueprint_id, label="blueprint_id")
    print_provider_id = _positive_id(print_provider_id, label="print_provider_id")
    return (
        f"/v2/catalog/blueprints/{blueprint_id}/print_providers/"
        f"{print_provider_id}/shipping/standard.json"
    )


def assert_printify_v2_standard_url(*, method: str, url: str) -> None:
    """Reject redirects, alternate origins, queries, and every non-GET provider route."""

    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError as error:
        raise PrintifyCatalogMismatchError("Printify V2 shipping URL was malformed") from error
    if (
        method != "GET"
        or parsed.scheme != "https"
        or parsed.netloc != "api.printify.com"
        or parsed.hostname != "api.printify.com"
        or port is not None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or _STANDARD_SHIPPING_PATH.fullmatch(parsed.path) is None
    ):
        raise PrintifyCatalogMismatchError(
            "Printify request escaped the read-only V2 standard-shipping boundary"
        )


def parse_standard_us_shipping(
    payload: Any,
    *,
    blueprint_id: int,
    print_provider_id: int,
    expected_variant_ids: Iterable[int],
    observed_at: datetime,
) -> StandardUsShippingEvidence:
    """Validate a documented V2 response and select every expected variant exactly once."""

    blueprint_id = _positive_id(blueprint_id, label="blueprint_id")
    print_provider_id = _positive_id(print_provider_id, label="print_provider_id")
    expected = tuple(expected_variant_ids)
    if not expected:
        raise PrintifyCatalogMismatchError("Configured Printify variant set is empty")
    if any(
        isinstance(variant_id, bool) or not isinstance(variant_id, int) or variant_id <= 0
        for variant_id in expected
    ):
        raise PrintifyCatalogMismatchError("Configured Printify variant ID was malformed")
    if len(expected) != len(set(expected)):
        raise PrintifyCatalogMismatchError("Configured Printify variant IDs are not unique")
    try:
        envelope = _PrintifyStandardEnvelope.model_validate(payload)
    except ValidationError as error:
        raise PrintifyCatalogMismatchError(
            "Printify V2 standard-shipping response was malformed"
        ) from error

    by_variant: dict[int, _PrintifyStandardResource] = {}
    for resource in envelope.data:
        variant_id = resource.attributes.variant_id
        if variant_id in by_variant:
            raise PrintifyCatalogMismatchError(
                "Printify V2 standard-shipping response repeated a variant ID"
            )
        by_variant[variant_id] = resource
    missing = [variant_id for variant_id in expected if variant_id not in by_variant]
    if missing:
        raise PrintifyCatalogMismatchError(
            "Printify V2 standard shipping omitted a configured variant"
        )

    normalized = tuple(
        StandardUsVariantShipping(
            variant_id=variant_id,
            resource_id=by_variant[variant_id].id,
            shipping_plan_id=by_variant[variant_id].attributes.shipping_plan_id,
            handling_from_days=by_variant[variant_id].attributes.handling_time.from_days,
            handling_to_days=by_variant[variant_id].attributes.handling_time.to_days,
            first_item_cents=(by_variant[variant_id].attributes.shipping_cost.first_item.amount),
            additional_item_cents=(
                by_variant[variant_id].attributes.shipping_cost.additional_items.amount
            ),
        )
        for variant_id in expected
    )
    try:
        return StandardUsShippingEvidence(
            blueprint_id=blueprint_id,
            print_provider_id=print_provider_id,
            source_path=standard_us_shipping_path(
                blueprint_id=blueprint_id,
                print_provider_id=print_provider_id,
            ),
            observed_at=observed_at,
            variants=normalized,
        )
    except ValidationError as error:
        raise PrintifyCatalogMismatchError("Shipping evidence metadata was malformed") from error


class _RejectRedirectHandler(HTTPRedirectHandler):
    def redirect_request(
        self,
        req: Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> None:
        del req, fp, code, msg, headers, newurl
        return None


class ReadOnlyPrintifyV2Transport:
    """Default transport that cannot forward the bearer credential through a redirect."""

    def __init__(self, *, opener: OpenerDirector | None = None) -> None:
        self._opener = opener or build_opener(_RejectRedirectHandler())

    def request(
        self,
        *,
        method: str,
        url: str,
        headers: dict[str, str],
        body: bytes | None,
        timeout_seconds: float,
    ) -> PrintifyHttpResponse:
        assert_printify_v2_standard_url(method=method, url=url)
        if body is not None:
            raise PrintifyCatalogMismatchError("Read-only Printify request cannot carry a body")
        request = Request(url=url, headers=headers, data=None, method="GET")
        try:
            with self._opener.open(request, timeout=timeout_seconds) as response:
                return PrintifyHttpResponse(
                    status=response.status,
                    body=response.read(MAX_STANDARD_SHIPPING_RESPONSE_BYTES + 1),
                )
        except HTTPError as error:
            return PrintifyHttpResponse(
                status=error.code,
                body=error.read(MAX_STANDARD_SHIPPING_RESPONSE_BYTES + 1),
            )
        except (URLError, TimeoutError) as error:
            raise PrintifyUnavailableError("Printify shipping request did not complete") from error


class PrintifyV2StandardShippingClient:
    """Authenticated client with one public operation and no mutation methods."""

    def __init__(
        self,
        *,
        token_provider: Callable[[], str],
        user_agent: str = "MrLister",
        timeout_seconds: float = 15.0,
        transport: PrintifyTransport | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        normalized_user_agent = user_agent.strip()
        if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._ /-]{0,127}", normalized_user_agent) is None:
            raise ValueError("A safe non-empty Printify User-Agent is required")
        if timeout_seconds <= 0:
            raise ValueError("Printify timeout must be positive")
        self._token_provider = token_provider
        self._user_agent = normalized_user_agent
        self._timeout_seconds = timeout_seconds
        self._transport = transport or ReadOnlyPrintifyV2Transport()
        self._clock = clock or (lambda: datetime.now(UTC))

    def get_us_standard_shipping(
        self,
        *,
        blueprint_id: int,
        print_provider_id: int,
        variant_ids: Iterable[int],
    ) -> StandardUsShippingEvidence:
        path = standard_us_shipping_path(
            blueprint_id=blueprint_id,
            print_provider_id=print_provider_id,
        )
        url = f"{PRINTIFY_V2_API_ORIGIN}{path}"
        assert_printify_v2_standard_url(method="GET", url=url)
        try:
            token = self._token_provider()
        except Exception:
            raise PrintifyAuthenticationError("Printify credential could not be loaded") from None
        if not isinstance(token, str) or not token.strip() or "\r" in token or "\n" in token:
            raise PrintifyAuthenticationError("Printify credential is invalid")
        response = self._transport.request(
            method="GET",
            url=url,
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {token.strip()}",
                "User-Agent": self._user_agent,
            },
            body=None,
            timeout_seconds=self._timeout_seconds,
        )
        if response.status in {401, 403}:
            raise PrintifyAuthenticationError("Printify rejected the configured credential")
        if response.status == 429 or response.status >= 500:
            raise PrintifyUnavailableError(
                f"Printify shipping request failed with HTTP {response.status}"
            )
        if response.status < 200 or response.status >= 300:
            raise PrintifyCatalogMismatchError(
                f"Printify shipping request failed with HTTP {response.status}"
            )
        if len(response.body) > MAX_STANDARD_SHIPPING_RESPONSE_BYTES:
            raise PrintifyCatalogMismatchError(
                "Printify V2 standard-shipping response exceeded the bounded envelope"
            )
        try:
            payload = json.loads(response.body)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise PrintifyCatalogMismatchError(
                "Printify V2 standard-shipping response was invalid JSON"
            ) from error
        return parse_standard_us_shipping(
            payload,
            blueprint_id=blueprint_id,
            print_provider_id=print_provider_id,
            expected_variant_ids=variant_ids,
            observed_at=self._clock(),
        )


__all__ = [
    "PRINTIFY_V2_API_BASE_URL",
    "MAX_STANDARD_SHIPPING_RESPONSE_BYTES",
    "PrintifyV2StandardShippingClient",
    "ReadOnlyPrintifyV2Transport",
    "StandardUsShippingEvidence",
    "StandardUsVariantShipping",
    "assert_printify_v2_standard_url",
    "parse_standard_us_shipping",
    "standard_us_shipping_path",
]
