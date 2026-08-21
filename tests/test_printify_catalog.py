from __future__ import annotations

import json
from base64 import b64decode
from pathlib import Path
from urllib.parse import urlparse

import pytest
from pydantic import ValidationError

from mr_lister.production.printify import (
    PrintifyAuthenticationError,
    PrintifyCatalogClient,
    PrintifyCatalogMismatchError,
    PrintifyHttpResponse,
    PrintifyInputError,
    PrintifyProductionClient,
    PrintifyProductProfile,
    PrintifyUnavailableError,
)

PROFILE_PATH = Path("config/product_profiles/gildan_64000_swiftpod.json")
COLORS = ("Black", "Charcoal", "Dark Chocolate", "Navy", "Sand")
SIZES = ("S", "M", "L", "XL", "2XL", "3XL")
DIMENSIONS = {
    "S": (3021, 3927),
    "M": (3356, 4364),
    "L": (3692, 4800),
    "XL": (3692, 4800),
    "2XL": (3692, 4800),
    "3XL": (3692, 4800),
}


class FakeTransport:
    def __init__(self, responses: dict[str, tuple[int, object]]) -> None:
        self.responses = responses
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
        status, payload = self.responses[urlparse(url).path]
        return PrintifyHttpResponse(status=status, body=json.dumps(payload).encode())


def load_profile() -> PrintifyProductProfile:
    return PrintifyProductProfile.model_validate_json(PROFILE_PATH.read_text())


def variant_payload() -> dict[str, object]:
    variants = []
    variant_id = 1000
    for color in COLORS:
        for size in SIZES:
            width, height = DIMENSIONS[size]
            variants.append(
                {
                    "id": variant_id,
                    "title": f"{color} / {size}",
                    "options": {"color": color, "size": size},
                    "placeholders": [
                        {
                            "position": "front",
                            "decoration_method": "dtg",
                            "width": width,
                            "height": height,
                        },
                        {
                            "position": "back",
                            "decoration_method": "dtg",
                            "width": width,
                            "height": height,
                        },
                    ],
                }
            )
            variant_id += 1
    return {"id": 39, "title": "Fixture Provider", "variants": variants}


def responses(*, variants: dict[str, object] | None = None) -> dict[str, tuple[int, object]]:
    return {
        "/v1/shops.json": (200, [{"id": 42, "title": "Fixture Shop"}]),
        "/v1/catalog/blueprints.json": (
            200,
            [{"id": 145, "title": "Unisex Softstyle T-Shirt"}],
        ),
        "/v1/catalog/blueprints/145/print_providers.json": (
            200,
            [{"id": 39, "title": "SwiftPOD"}],
        ),
        "/v1/catalog/blueprints/145/print_providers/39/variants.json": (
            200,
            variants or variant_payload(),
        ),
    }


def test_verified_profile_contains_seller_defaults_and_three_canvas_groups() -> None:
    profile = load_profile()

    assert profile.profile_id == "gildan_64000_swiftpod"
    assert profile.blueprint_id == 145
    assert profile.print_provider_id == 39
    assert profile.colors == COLORS
    assert profile.sizes == SIZES
    assert profile.retail_price_cents == 2999
    assert profile.buyer_shipping_cents == 0
    assert profile.profile_version == 2
    assert [group.group_id for group in profile.placement_groups] == [
        "small",
        "medium",
        "large",
    ]
    assert all(group.placement.x == 0.5 for group in profile.placement_groups)
    assert all(group.placement.y == 0.25 for group in profile.placement_groups)
    assert all(group.placement.scale == 0.65 for group in profile.placement_groups)
    assert profile.publish_enabled is False


def test_profile_rejects_incomplete_or_overlapping_size_groups() -> None:
    payload = load_profile().model_dump()
    payload["placement_groups"][2]["sizes"] = ("M", "L", "XL", "2XL", "3XL")

    with pytest.raises(ValidationError, match="exactly one placement group"):
        PrintifyProductProfile.model_validate(payload)


def test_preflight_resolves_exactly_thirty_variants_in_profile_order() -> None:
    transport = FakeTransport(responses())
    client = PrintifyCatalogClient(
        token_provider=lambda: "private-token",
        transport=transport,
    )

    resolved = client.preflight(shop_id=42, profile=load_profile())

    assert len(resolved.variants) == 30
    assert len({variant.variant_id for variant in resolved.variants}) == 30
    assert resolved.variants[0].color == "Black"
    assert resolved.variants[0].size == "S"
    assert resolved.variants[-1].color == "Sand"
    assert resolved.variants[-1].size == "3XL"
    assert {variant.retail_price_cents for variant in resolved.variants} == {2999}
    assert {variant.placement_group_id for variant in resolved.variants} == {
        "small",
        "medium",
        "large",
    }
    assert len(transport.calls) == 4
    assert all(
        call["headers"]["Authorization"] == "Bearer private-token" for call in transport.calls
    )


def test_preflight_fails_closed_when_a_selected_variant_disappears() -> None:
    catalog = variant_payload()
    catalog["variants"] = catalog["variants"][:-1]
    client = PrintifyCatalogClient(
        token_provider=lambda: "private-token",
        transport=FakeTransport(responses(variants=catalog)),
    )

    with pytest.raises(PrintifyCatalogMismatchError, match="Sand / 3XL"):
        client.preflight(shop_id=42, profile=load_profile())


def test_preflight_fails_closed_when_print_canvas_changes() -> None:
    catalog = variant_payload()
    catalog["variants"][0]["placeholders"][0]["width"] = 9999
    client = PrintifyCatalogClient(
        token_provider=lambda: "private-token",
        transport=FakeTransport(responses(variants=catalog)),
    )

    with pytest.raises(PrintifyCatalogMismatchError, match="print area changed"):
        client.preflight(shop_id=42, profile=load_profile())


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
def test_client_maps_http_failures_without_exposing_response_body(
    status: int, error_type: type[Exception]
) -> None:
    transport = FakeTransport(
        {"/v1/shops.json": (status, {"message": "provider detail must not escape"})}
    )
    client = PrintifyCatalogClient(
        token_provider=lambda: "private-token",
        transport=transport,
    )

    with pytest.raises(error_type) as raised:
        client.list_shops()

    assert "private-token" not in str(raised.value)
    assert "provider detail" not in str(raised.value)


def test_production_client_uploads_png_bytes_without_changing_them() -> None:
    png = b"\x89PNG\r\n\x1a\nfixture"
    transport = FakeTransport(
        {
            "/v1/uploads/images.json": (
                200,
                {
                    "id": "image-1",
                    "file_name": "artwork.png",
                    "width": 4000,
                    "height": 5000,
                    "size": len(png),
                    "mime_type": "image/png",
                },
            )
        }
    )
    client = PrintifyProductionClient(token_provider=lambda: "private-token", transport=transport)

    uploaded = client.upload_artwork_contents(
        file_name="artwork.png", content_type="image/png", content=png
    )

    request_payload = json.loads(transport.calls[0]["body"])
    assert b64decode(request_payload["contents"]) == png
    assert uploaded.image_id == "image-1"
    assert transport.calls[0]["url"].endswith("/v1/uploads/images.json")


def test_production_client_passes_safe_svg_bytes_through_unchanged() -> None:
    svg = b'<svg xmlns="http://www.w3.org/2000/svg" width="100" height="100"><path d="M0 0"/></svg>'
    transport = FakeTransport(
        {
            "/v1/uploads/images.json": (
                200,
                {
                    "id": "image-svg",
                    "file_name": "artwork.svg",
                    "width": 100,
                    "height": 100,
                    "size": len(svg),
                    "mime_type": "image/svg+xml",
                },
            )
        }
    )
    client = PrintifyProductionClient(token_provider=lambda: "private-token", transport=transport)

    uploaded = client.upload_artwork_contents(
        file_name="artwork.svg", content_type="image/svg+xml", content=svg
    )

    request_payload = json.loads(transport.calls[0]["body"])
    assert b64decode(request_payload["contents"]) == svg
    assert uploaded.mime_type == "image/svg+xml"


@pytest.mark.parametrize(
    "svg",
    [
        b'<!DOCTYPE svg><svg xmlns="http://www.w3.org/2000/svg"/>',
        b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>',
        b'<svg xmlns="http://www.w3.org/2000/svg"><image href="https://bad.test/x"/></svg>',
        b'<svg xmlns="http://www.w3.org/2000/svg" onload="alert(1)"/>',
        b'<svg xmlns="http://www.w3.org/2000/svg"><style>@import "x";</style></svg>',
    ],
)
def test_production_client_rejects_active_or_external_svg(svg: bytes) -> None:
    client = PrintifyProductionClient(
        token_provider=lambda: "private-token", transport=FakeTransport({})
    )

    with pytest.raises(PrintifyInputError):
        client.upload_artwork_contents(
            file_name="artwork.svg", content_type="image/svg+xml", content=svg
        )


def test_production_client_builds_three_group_unpublished_product_payload(
    listing,
) -> None:
    response_map = responses()
    response_map["/v1/shops/42/products.json"] = (
        200,
        {
            "id": "product-1",
            "images": [
                {
                    "src": "https://images.printify.com/mockup/product-1/front.jpg",
                    "variant_ids": [1000],
                    "position": "front",
                }
            ],
        },
    )
    transport = FakeTransport(response_map)
    client = PrintifyProductionClient(token_provider=lambda: "private-token", transport=transport)
    profile = load_profile()
    resolved = client.preflight(shop_id=42, profile=profile)

    product = client.create_unpublished_product(
        listing=listing,
        profile=profile,
        resolved=resolved,
        image_id="image-1",
    )

    request = transport.calls[-1]
    payload = json.loads(request["body"])
    assert request["method"] == "POST"
    assert request["url"].endswith("/v1/shops/42/products.json")
    assert len(payload["variants"]) == 30
    assert {variant["price"] for variant in payload["variants"]} == {2999}
    assert [len(area["variant_ids"]) for area in payload["print_areas"]] == [5, 5, 20]
    assert all(
        area["placeholders"][0]["images"][0]
        == {"id": "image-1", "x": 0.5, "y": 0.25, "scale": 0.65, "angle": 0}
        for area in payload["print_areas"]
    )
    assert product.product_id == "product-1"
    assert len(product.mockup_urls) == 1
    assert not hasattr(client, "publish")
    assert not hasattr(client, "create_order")
