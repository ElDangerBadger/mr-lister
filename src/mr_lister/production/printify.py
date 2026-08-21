"""Read-only Printify catalog boundary used before any marketplace write.

The preflight resolves seller-owned color and size choices into current Printify
variant IDs. It deliberately has no upload, product-create, publish, or order method.
"""

from __future__ import annotations

import json
from base64 import b64encode
from collections.abc import Callable
from dataclasses import dataclass
from typing import Annotated, Any, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlparse
from urllib.request import Request, urlopen
from xml.etree import ElementTree

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from mr_lister.contracts import (
    ListingIntelligence,
)
from mr_lister.contracts import (
    PlacementGroup as PrintifyPlacementGroup,
)
from mr_lister.contracts import (
    ProductProfile as PrintifyProductProfile,
)

DEFAULT_PRINTIFY_BASE_URL = "https://api.printify.com/v1/"

ProfileId = Annotated[str, StringConstraints(pattern=r"^[a-z0-9][a-z0-9_-]+$")]
NonEmptyName = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class PrintifyError(Exception):
    """Base class for stable Printify adapter failures."""


class PrintifyAuthenticationError(PrintifyError):
    """The supplied credential cannot access the requested Printify resource."""


class PrintifyUnavailableError(PrintifyError):
    """Printify or the network failed in a way that may be retried safely."""


class PrintifyCatalogMismatchError(PrintifyError):
    """The live catalog no longer satisfies the seller's product profile."""


class PrintifyInputError(PrintifyError):
    """Application input cannot safely be sent to Printify."""


class PrintifyProfileModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class PrintifyResolvedVariant(PrintifyProfileModel):
    variant_id: int = Field(gt=0)
    color: NonEmptyName
    size: NonEmptyName
    placement_group_id: ProfileId
    canvas_width: int = Field(gt=0)
    canvas_height: int = Field(gt=0)
    retail_price_cents: int = Field(gt=0)


class PrintifyResolvedProfile(PrintifyProfileModel):
    profile_id: ProfileId
    profile_version: int = Field(ge=1)
    shop_id: int = Field(gt=0)
    blueprint_id: int = Field(gt=0)
    print_provider_id: int = Field(gt=0)
    variants: tuple[PrintifyResolvedVariant, ...] = Field(min_length=1)


class PrintifyUploadedImage(PrintifyProfileModel):
    image_id: NonEmptyName
    file_name: NonEmptyName
    width: int = Field(gt=0)
    height: int = Field(gt=0)
    size_bytes: int = Field(gt=0)
    mime_type: str = Field(pattern=r"^image/(png|svg|svg\+xml)$")


class PrintifyDraftProduct(PrintifyProfileModel):
    product_id: NonEmptyName
    mockup_urls: tuple[NonEmptyName, ...] = ()


@dataclass(frozen=True)
class PrintifyHttpResponse:
    status: int
    body: bytes


class PrintifyTransport(Protocol):
    def request(
        self,
        *,
        method: str,
        url: str,
        headers: dict[str, str],
        body: bytes | None,
        timeout_seconds: float,
    ) -> PrintifyHttpResponse: ...


class UrllibPrintifyTransport:
    """Small standard-library transport so the Lambda needs no new HTTP dependency."""

    def request(
        self,
        *,
        method: str,
        url: str,
        headers: dict[str, str],
        body: bytes | None,
        timeout_seconds: float,
    ) -> PrintifyHttpResponse:
        request = Request(url=url, headers=headers, data=body, method=method)
        try:
            with urlopen(request, timeout=timeout_seconds) as response:  # noqa: S310
                return PrintifyHttpResponse(status=response.status, body=response.read())
        except HTTPError as error:
            return PrintifyHttpResponse(status=error.code, body=error.read())
        except (URLError, TimeoutError) as error:
            raise PrintifyUnavailableError("Printify request did not complete") from error


class PrintifyCatalogClient:
    """Authenticated, read-only client for catalog and shop preflight."""

    def __init__(
        self,
        *,
        token_provider: Callable[[], str],
        user_agent: str = "MrLister",
        base_url: str = DEFAULT_PRINTIFY_BASE_URL,
        timeout_seconds: float = 15.0,
        transport: PrintifyTransport | None = None,
    ) -> None:
        if not user_agent.strip():
            raise ValueError("A non-empty Printify User-Agent is required")
        if timeout_seconds <= 0:
            raise ValueError("Printify timeout must be positive")
        self._token_provider = token_provider
        self._user_agent = user_agent
        self._base_url = base_url.rstrip("/") + "/"
        self._timeout_seconds = timeout_seconds
        self._transport = transport or UrllibPrintifyTransport()

    def list_shops(self) -> tuple[dict[str, Any], ...]:
        payload = self._get_json("shops.json")
        if not isinstance(payload, list):
            raise PrintifyCatalogMismatchError("Printify shops response was not a list")
        return tuple(self._require_mapping(item, "shop") for item in payload)

    def list_blueprints(self) -> tuple[dict[str, Any], ...]:
        payload = self._get_json("catalog/blueprints.json")
        if not isinstance(payload, list):
            raise PrintifyCatalogMismatchError("Printify blueprints response was not a list")
        return tuple(self._require_mapping(item, "blueprint") for item in payload)

    def list_print_providers(self, blueprint_id: int) -> tuple[dict[str, Any], ...]:
        payload = self._get_json(f"catalog/blueprints/{blueprint_id}/print_providers.json")
        if not isinstance(payload, list):
            raise PrintifyCatalogMismatchError("Printify providers response was not a list")
        return tuple(self._require_mapping(item, "print provider") for item in payload)

    def get_variant_catalog(self, blueprint_id: int, print_provider_id: int) -> dict[str, Any]:
        payload = self._get_json(
            f"catalog/blueprints/{blueprint_id}/print_providers/{print_provider_id}/variants.json"
        )
        return self._require_mapping(payload, "variant catalog")

    def preflight(
        self,
        *,
        shop_id: int,
        profile: PrintifyProductProfile,
    ) -> PrintifyResolvedProfile:
        if not any(self._positive_int(shop.get("id")) == shop_id for shop in self.list_shops()):
            raise PrintifyCatalogMismatchError("Configured Printify shop was not found")
        if not any(
            self._positive_int(blueprint.get("id")) == profile.blueprint_id
            for blueprint in self.list_blueprints()
        ):
            raise PrintifyCatalogMismatchError("Configured Printify blueprint was not found")
        if not any(
            self._positive_int(provider.get("id")) == profile.print_provider_id
            for provider in self.list_print_providers(profile.blueprint_id)
        ):
            raise PrintifyCatalogMismatchError("Configured Printify provider was not found")

        catalog = self.get_variant_catalog(profile.blueprint_id, profile.print_provider_id)
        raw_variants = catalog.get("variants")
        if not isinstance(raw_variants, list):
            raise PrintifyCatalogMismatchError("Printify variant catalog omitted variants")

        expected_pairs = {(color, size) for color in profile.colors for size in profile.sizes}
        resolved_by_pair: dict[tuple[str, str], PrintifyResolvedVariant] = {}
        group_by_size = {size: group for group in profile.placement_groups for size in group.sizes}
        for raw_variant in raw_variants:
            variant = self._require_mapping(raw_variant, "variant")
            options = variant.get("options")
            if not isinstance(options, dict):
                continue
            color = options.get("color")
            size = options.get("size")
            pair = (color, size)
            if pair not in expected_pairs:
                continue
            if pair in resolved_by_pair:
                raise PrintifyCatalogMismatchError(
                    f"Printify returned duplicate variant for {color} / {size}"
                )
            variant_id = self._positive_int(variant.get("id"))
            if variant_id is None:
                raise PrintifyCatalogMismatchError(
                    f"Printify variant for {color} / {size} has no valid ID"
                )
            group = group_by_size[size]
            self._verify_placeholder(variant, group, color=color, size=size)
            resolved_by_pair[pair] = PrintifyResolvedVariant(
                variant_id=variant_id,
                color=color,
                size=size,
                placement_group_id=group.group_id,
                canvas_width=group.canvas_width,
                canvas_height=group.canvas_height,
                retail_price_cents=profile.retail_price_cents,
            )

        missing = sorted(expected_pairs - resolved_by_pair.keys())
        if missing:
            missing_text = ", ".join(f"{color} / {size}" for color, size in missing)
            raise PrintifyCatalogMismatchError(f"Printify catalog is missing: {missing_text}")

        variants = tuple(
            resolved_by_pair[(color, size)] for color in profile.colors for size in profile.sizes
        )
        return PrintifyResolvedProfile(
            profile_id=profile.profile_id,
            profile_version=profile.profile_version,
            shop_id=shop_id,
            blueprint_id=profile.blueprint_id,
            print_provider_id=profile.print_provider_id,
            variants=variants,
        )

    def _get_json(self, path: str) -> Any:
        return self._request_json(method="GET", path=path)

    def _request_json(
        self, *, method: str, path: str, payload: dict[str, Any] | None = None
    ) -> Any:
        token = self._token_provider()
        if not isinstance(token, str) or not token.strip():
            raise PrintifyAuthenticationError("Printify credential is empty")
        body = None if payload is None else json.dumps(payload, separators=(",", ":")).encode()
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {token.strip()}",
            "User-Agent": self._user_agent,
        }
        if body is not None:
            headers["Content-Type"] = "application/json;charset=utf-8"
        response = self._transport.request(
            method=method,
            url=urljoin(self._base_url, path),
            headers=headers,
            body=body,
            timeout_seconds=self._timeout_seconds,
        )
        if response.status in {401, 403}:
            raise PrintifyAuthenticationError("Printify rejected the configured credential")
        if response.status == 429 or response.status >= 500:
            raise PrintifyUnavailableError(f"Printify request failed with HTTP {response.status}")
        if response.status < 200 or response.status >= 300:
            raise PrintifyCatalogMismatchError(
                f"Printify request failed with HTTP {response.status}"
            )
        try:
            return json.loads(response.body)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise PrintifyCatalogMismatchError("Printify returned invalid JSON") from error

    @staticmethod
    def _require_mapping(value: Any, label: str) -> dict[str, Any]:
        if not isinstance(value, dict):
            raise PrintifyCatalogMismatchError(f"Printify {label} was not an object")
        return value

    @staticmethod
    def _positive_int(value: Any) -> int | None:
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            return None
        return value

    @staticmethod
    def _verify_placeholder(
        variant: dict[str, Any],
        group: PrintifyPlacementGroup,
        *,
        color: str,
        size: str,
    ) -> None:
        placeholders = variant.get("placeholders")
        if not isinstance(placeholders, list):
            raise PrintifyCatalogMismatchError(
                f"Printify variant {color} / {size} omitted print placeholders"
            )
        matches = [
            placeholder
            for placeholder in placeholders
            if isinstance(placeholder, dict)
            and placeholder.get("position") == group.position
            and placeholder.get("decoration_method") == group.decoration_method
        ]
        if len(matches) != 1:
            raise PrintifyCatalogMismatchError(
                f"Printify variant {color} / {size} has no unique "
                f"{group.decoration_method} {group.position} placeholder"
            )
        placeholder = matches[0]
        dimensions = (placeholder.get("width"), placeholder.get("height"))
        expected = (group.canvas_width, group.canvas_height)
        if dimensions != expected:
            raise PrintifyCatalogMismatchError(
                f"Printify print area changed for {color} / {size}: "
                f"expected {expected[0]}x{expected[1]}"
            )


class PrintifyProductionClient(PrintifyCatalogClient):
    """Narrow Phase 5 writer: artwork upload and unpublished product creation only."""

    MAX_BASE64_SOURCE_BYTES = 5 * 1024 * 1024

    def upload_artwork_contents(
        self, *, file_name: str, content_type: str, content: bytes
    ) -> PrintifyUploadedImage:
        self._validate_artwork(file_name=file_name, content_type=content_type, content=content)
        if len(content) > self.MAX_BASE64_SOURCE_BYTES:
            raise PrintifyInputError("Artwork exceeds the safe base64 limit; use HTTPS URL upload")
        payload = self._request_json(
            method="POST",
            path="uploads/images.json",
            payload={"file_name": file_name, "contents": b64encode(content).decode("ascii")},
        )
        return self._parse_upload(payload)

    def upload_artwork_url(
        self, *, file_name: str, content_type: str, url: str
    ) -> PrintifyUploadedImage:
        self._validate_artwork_name(file_name=file_name, content_type=content_type)
        parsed = urlparse(url)
        if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password:
            raise PrintifyInputError("Printify URL upload requires a credential-free HTTPS URL")
        payload = self._request_json(
            method="POST",
            path="uploads/images.json",
            payload={"file_name": file_name, "url": url},
        )
        return self._parse_upload(payload)

    def create_unpublished_product(
        self,
        *,
        listing: ListingIntelligence,
        profile: PrintifyProductProfile,
        resolved: PrintifyResolvedProfile,
        image_id: str,
    ) -> PrintifyDraftProduct:
        if not image_id.strip():
            raise PrintifyInputError("A Printify image ID is required")
        self._verify_resolved_identity(profile, resolved)
        variants = [
            {
                "id": variant.variant_id,
                "price": variant.retail_price_cents,
                "is_enabled": True,
            }
            for variant in resolved.variants
        ]
        print_areas = []
        for group in profile.placement_groups:
            variant_ids = [
                variant.variant_id
                for variant in resolved.variants
                if variant.placement_group_id == group.group_id
            ]
            print_areas.append(
                {
                    "variant_ids": variant_ids,
                    "placeholders": [
                        {
                            "position": group.position,
                            "images": [
                                {
                                    "id": image_id,
                                    "x": group.placement.x,
                                    "y": group.placement.y,
                                    "scale": group.placement.scale,
                                    "angle": group.angle,
                                }
                            ],
                        }
                    ],
                }
            )
        payload = self._request_json(
            method="POST",
            path=f"shops/{resolved.shop_id}/products.json",
            payload={
                "title": listing.title,
                "description": listing.description,
                "tags": list(listing.tags),
                "blueprint_id": profile.blueprint_id,
                "print_provider_id": profile.print_provider_id,
                "variants": variants,
                "print_areas": print_areas,
            },
        )
        product = self._require_mapping(payload, "created product")
        product_id = product.get("id")
        if not isinstance(product_id, str) or not product_id.strip():
            raise PrintifyCatalogMismatchError("Printify product response omitted its ID")
        images = product.get("images", [])
        if not isinstance(images, list):
            raise PrintifyCatalogMismatchError("Printify product response had invalid mockups")
        mockup_urls = tuple(
            source
            for image in images
            if isinstance(image, dict) and isinstance((source := image.get("src")), str) and source
        )
        return PrintifyDraftProduct(product_id=product_id, mockup_urls=mockup_urls)

    @staticmethod
    def _validate_artwork(*, file_name: str, content_type: str, content: bytes) -> None:
        PrintifyProductionClient._validate_artwork_name(
            file_name=file_name, content_type=content_type
        )
        if content_type == "image/png":
            if not content.startswith(b"\x89PNG\r\n\x1a\n"):
                raise PrintifyInputError("Artwork does not contain a valid PNG signature")
            return
        PrintifyProductionClient._validate_safe_svg(content)

    @staticmethod
    def _validate_artwork_name(*, file_name: str, content_type: str) -> None:
        if not file_name or len(file_name) > 255:
            raise PrintifyInputError("Artwork filename is invalid")
        suffix = file_name.casefold().rsplit(".", maxsplit=1)[-1]
        expected_type = {"png": "image/png", "svg": "image/svg+xml"}.get(suffix)
        if expected_type is None or content_type != expected_type:
            raise PrintifyInputError("Phase 5 accepts matching PNG or SVG artwork only")

    @staticmethod
    def _validate_safe_svg(content: bytes) -> None:
        if not content or len(content) > 25 * 1024 * 1024:
            raise PrintifyInputError("SVG is empty or exceeds the artwork size limit")
        lowered = content.lower()
        if b"<!doctype" in lowered or b"<!entity" in lowered:
            raise PrintifyInputError("SVG declarations and entities are not allowed")
        try:
            root = ElementTree.fromstring(content)
        except ElementTree.ParseError as error:
            raise PrintifyInputError("Artwork contains invalid SVG XML") from error
        if root.tag.rsplit("}", maxsplit=1)[-1].casefold() != "svg":
            raise PrintifyInputError("Artwork XML root must be SVG")
        forbidden_elements = {"script", "foreignobject"}
        for element in root.iter():
            local_name = element.tag.rsplit("}", maxsplit=1)[-1].casefold()
            if local_name in forbidden_elements:
                raise PrintifyInputError(f"SVG {local_name} elements are not allowed")
            for attribute_name, value in element.attrib.items():
                attribute = attribute_name.rsplit("}", maxsplit=1)[-1].casefold()
                normalized = value.strip().casefold()
                if attribute.startswith("on"):
                    raise PrintifyInputError("SVG event handlers are not allowed")
                if attribute in {"href", "src"} and normalized and not normalized.startswith("#"):
                    raise PrintifyInputError("SVG external resources are not allowed")
                if "url(" in normalized and "url(#" not in normalized:
                    raise PrintifyInputError("SVG external resource URLs are not allowed")
            text = (element.text or "").strip().casefold()
            if "@import" in text or ("url(" in text and "url(#" not in text):
                raise PrintifyInputError("SVG style imports and external URLs are not allowed")

    @staticmethod
    def _parse_upload(payload: Any) -> PrintifyUploadedImage:
        upload = PrintifyCatalogClient._require_mapping(payload, "upload response")
        try:
            return PrintifyUploadedImage(
                image_id=upload["id"],
                file_name=upload["file_name"],
                width=upload["width"],
                height=upload["height"],
                size_bytes=upload["size"],
                mime_type=upload["mime_type"],
            )
        except (KeyError, TypeError, ValueError) as error:
            raise PrintifyCatalogMismatchError(
                "Printify returned an invalid upload record"
            ) from error

    @staticmethod
    def _verify_resolved_identity(
        profile: PrintifyProductProfile, resolved: PrintifyResolvedProfile
    ) -> None:
        if (
            resolved.profile_id != profile.profile_id
            or resolved.profile_version != profile.profile_version
            or resolved.blueprint_id != profile.blueprint_id
            or resolved.print_provider_id != profile.print_provider_id
        ):
            raise PrintifyInputError("Resolved catalog does not match the product profile")
