from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from mr_lister.contracts import ProductProfile
from mr_lister.production.printify import (
    PrintifyAuthenticationError,
    PrintifyCatalogMismatchError,
    PrintifyHttpResponse,
    PrintifyUnavailableError,
)
from mr_lister.production.printify_shipping import (
    MAX_STANDARD_SHIPPING_RESPONSE_BYTES,
    PrintifyV2StandardShippingClient,
    assert_printify_v2_standard_url,
    parse_standard_us_shipping,
)

NOW = datetime(2026, 8, 22, 12, 0, tzinfo=UTC)
PROFILE_PATH = Path("config/product_profiles/gildan_64000_swiftpod.json")


def _resource(
    variant_id: int,
    *,
    first_item: int = 399,
    additional_item: int = 219,
    country_code: str = "US",
    resource_type: str = "variant_shipping_standard_us",
    shipping_plan_id: str = "65a7c0825b50fcd56a018e02",
) -> dict[str, object]:
    # This intentionally mirrors Printify's documented V2 standard response shape.
    return {
        "type": resource_type,
        "id": str(variant_id),
        "attributes": {
            "shippingType": "standard",
            "country": {"code": country_code},
            "variantId": variant_id,
            "shippingPlanId": shipping_plan_id,
            "handlingTime": {"from": 4, "to": 8},
            "shippingCost": {
                "firstItem": {"amount": first_item, "currency": "USD"},
                "additionalItems": {"amount": additional_item, "currency": "USD"},
            },
        },
    }


def _envelope(variant_ids: tuple[int, ...]) -> dict[str, object]:
    return {"data": [_resource(variant_id) for variant_id in variant_ids]}


class FakeTransport:
    def __init__(self, *, status: int = 200, payload: object | None = None) -> None:
        self.status = status
        self.payload = payload if payload is not None else _envelope((23494,))
        self.calls: list[dict[str, object]] = []

    def request(
        self,
        *,
        method: str,
        url: str,
        headers: dict[str, str],
        body: bytes | None,
        timeout_seconds: float,
    ) -> PrintifyHttpResponse:
        self.calls.append(
            {
                "method": method,
                "url": url,
                "headers": headers,
                "body": body,
                "timeout_seconds": timeout_seconds,
            }
        )
        return PrintifyHttpResponse(status=self.status, body=json.dumps(self.payload).encode())


def test_client_uses_only_the_documented_v2_standard_get() -> None:
    transport = FakeTransport(payload=_envelope((23494, 23495)))
    client = PrintifyV2StandardShippingClient(
        token_provider=lambda: "private-token",
        transport=transport,
        clock=lambda: NOW,
    )

    evidence = client.get_us_standard_shipping(
        blueprint_id=145,
        print_provider_id=39,
        variant_ids=(23495, 23494),
    )

    assert [item.variant_id for item in evidence.variants] == [23495, 23494]
    assert evidence.variants[0].first_item_cents == 399
    assert evidence.variants[0].additional_item_cents == 219
    assert evidence.observed_at == NOW
    assert len(evidence.fingerprint) == 64
    assert transport.calls == [
        {
            "method": "GET",
            "url": (
                "https://api.printify.com/v2/catalog/blueprints/145/"
                "print_providers/39/shipping/standard.json"
            ),
            "headers": {
                "Accept": "application/json",
                "Authorization": "Bearer private-token",
                "User-Agent": "MrLister",
            },
            "body": None,
            "timeout_seconds": 15.0,
        }
    ]
    assert not hasattr(client, "create_draft")
    assert not hasattr(client, "publish")
    assert not hasattr(client, "create_order")


@pytest.mark.parametrize(
    ("method", "url"),
    [
        (
            "POST",
            "https://api.printify.com/v2/catalog/blueprints/145/"
            "print_providers/39/shipping/standard.json",
        ),
        (
            "GET",
            "https://evil.example/v2/catalog/blueprints/145/"
            "print_providers/39/shipping/standard.json",
        ),
        (
            "GET",
            "https://api.printify.com/v2/catalog/blueprints/145/"
            "print_providers/39/shipping/priority.json",
        ),
        (
            "GET",
            "https://api.printify.com/v2/catalog/blueprints/145/"
            "print_providers/39/shipping/standard.json?token=bad",
        ),
    ],
)
def test_route_guard_rejects_every_nonstandard_or_nonread_route(method: str, url: str) -> None:
    with pytest.raises(PrintifyCatalogMismatchError, match="read-only"):
        assert_printify_v2_standard_url(method=method, url=url)


def test_parser_accepts_the_exact_thirty_profile_variants_among_a_larger_catalog() -> None:
    profile = ProductProfile.model_validate_json(PROFILE_PATH.read_text())
    configured_count = len(profile.colors) * len(profile.sizes)
    configured_ids = tuple(range(10_000, 10_000 + configured_count))
    payload = {
        "data": [
            _resource(99_999),
            *[_resource(variant_id) for variant_id in reversed(configured_ids)],
        ]
    }

    evidence = parse_standard_us_shipping(
        payload,
        blueprint_id=profile.blueprint_id,
        print_provider_id=profile.print_provider_id,
        expected_variant_ids=configured_ids,
        observed_at=NOW,
    )

    assert configured_count == 30
    assert tuple(item.variant_id for item in evidence.variants) == configured_ids
    assert len(evidence.variants) == 30


def test_parser_selects_us_resources_from_the_all_country_standard_catalog() -> None:
    payload = {
        "data": [
            _resource(
                23494,
                country_code="CA",
                resource_type="variant_shipping_standard_ca",
            ),
            _resource(
                23495,
                country_code="REST_OF_THE_WORLD",
                resource_type="variant_shipping_standard_rest_of_the_world",
            ),
            _resource(23495, first_item=425, shipping_plan_id="plan-b"),
            _resource(23494, first_item=399, shipping_plan_id="plan-b"),
            _resource(23495, first_item=425, shipping_plan_id="plan-a"),
            _resource(23494, first_item=399, shipping_plan_id="plan-a"),
        ]
    }

    evidence = parse_standard_us_shipping(
        payload,
        blueprint_id=145,
        print_provider_id=39,
        expected_variant_ids=(23494, 23495),
        observed_at=NOW,
    )
    reversed_evidence = parse_standard_us_shipping(
        {"data": list(reversed(payload["data"]))},
        blueprint_id=145,
        print_provider_id=39,
        expected_variant_ids=(23494, 23495),
        observed_at=NOW,
    )

    assert [item.variant_id for item in evidence.variants] == [23494, 23495]
    assert [item.first_item_cents for item in evidence.variants] == [399, 425]
    assert [item.shipping_plan_id for item in evidence.variants] == ["plan-a", "plan-a"]
    assert evidence.fingerprint == reversed_evidence.fingerprint


def test_shipping_fingerprint_is_independent_of_configured_variant_order() -> None:
    payload = _envelope((23494, 23495))
    first = parse_standard_us_shipping(
        payload,
        blueprint_id=145,
        print_provider_id=39,
        expected_variant_ids=(23494, 23495),
        observed_at=NOW,
    )
    second = parse_standard_us_shipping(
        payload,
        blueprint_id=145,
        print_provider_id=39,
        expected_variant_ids=(23495, 23494),
        observed_at=NOW,
    )

    assert first.fingerprint == second.fingerprint


def test_parser_rejects_a_missing_configured_variant() -> None:
    with pytest.raises(PrintifyCatalogMismatchError, match="omitted"):
        parse_standard_us_shipping(
            _envelope((23494,)),
            blueprint_id=145,
            print_provider_id=39,
            expected_variant_ids=(23494, 23495),
            observed_at=NOW,
        )


def test_parser_rejects_duplicate_provider_variant_with_conflicting_shipping_terms() -> None:
    with pytest.raises(PrintifyCatalogMismatchError, match="conflicting shipping terms"):
        parse_standard_us_shipping(
            {"data": [_resource(23494), _resource(23494, first_item=400)]},
            blueprint_id=145,
            print_provider_id=39,
            expected_variant_ids=(23494,),
            observed_at=NOW,
        )


def test_parser_rejects_duplicate_configured_variant_ids() -> None:
    with pytest.raises(PrintifyCatalogMismatchError, match="not unique"):
        parse_standard_us_shipping(
            _envelope((23494,)),
            blueprint_id=145,
            print_provider_id=39,
            expected_variant_ids=(23494, 23494),
            observed_at=NOW,
        )


@pytest.mark.parametrize(
    "mutation",
    [
        lambda resource: resource["attributes"]["shippingCost"]["firstItem"].update(
            {"amount": "399"}
        ),
        lambda resource: resource["attributes"]["country"].update({"code": "CA"}),
        lambda resource: resource["attributes"]["shippingCost"]["firstItem"].update(
            {"currency": "EUR"}
        ),
        lambda resource: resource.update({"id": "99999"}),
        lambda resource: resource["attributes"].update({"unexpected": True}),
    ],
)
def test_parser_rejects_malformed_or_non_usd_standard_evidence(mutation) -> None:
    resource = _resource(23494)
    mutation(resource)

    with pytest.raises(PrintifyCatalogMismatchError, match="malformed"):
        parse_standard_us_shipping(
            {"data": [resource]},
            blueprint_id=145,
            print_provider_id=39,
            expected_variant_ids=(23494,),
            observed_at=NOW,
        )


@pytest.mark.parametrize(
    ("status", "error_type"),
    [
        (401, PrintifyAuthenticationError),
        (403, PrintifyAuthenticationError),
        (429, PrintifyUnavailableError),
        (503, PrintifyUnavailableError),
        (400, PrintifyCatalogMismatchError),
    ],
)
def test_http_failures_do_not_expose_credentials_or_provider_body(
    status: int,
    error_type: type[Exception],
) -> None:
    transport = FakeTransport(
        status=status,
        payload={"message": "provider detail must stay internal"},
    )
    client = PrintifyV2StandardShippingClient(
        token_provider=lambda: "private-token",
        transport=transport,
        clock=lambda: NOW,
    )

    with pytest.raises(error_type) as raised:
        client.get_us_standard_shipping(
            blueprint_id=145,
            print_provider_id=39,
            variant_ids=(23494,),
        )

    assert "private-token" not in str(raised.value)
    assert "provider detail" not in str(raised.value)


def test_client_rejects_header_injection_and_oversized_provider_evidence() -> None:
    client = PrintifyV2StandardShippingClient(
        token_provider=lambda: "private-token\r\nX-Injected: yes",
        transport=FakeTransport(),
        clock=lambda: NOW,
    )
    with pytest.raises(PrintifyAuthenticationError, match="invalid") as raised:
        client.get_us_standard_shipping(
            blueprint_id=145,
            print_provider_id=39,
            variant_ids=(23494,),
        )
    assert "X-Injected" not in str(raised.value)

    class OversizedTransport(FakeTransport):
        def request(self, **request) -> PrintifyHttpResponse:
            self.calls.append(request)
            return PrintifyHttpResponse(
                status=200,
                body=b"x" * (MAX_STANDARD_SHIPPING_RESPONSE_BYTES + 1),
            )

    bounded_client = PrintifyV2StandardShippingClient(
        token_provider=lambda: "private-token",
        transport=OversizedTransport(),
        clock=lambda: NOW,
    )
    with pytest.raises(PrintifyCatalogMismatchError, match="bounded"):
        bounded_client.get_us_standard_shipping(
            blueprint_id=145,
            print_provider_id=39,
            variant_ids=(23494,),
        )
