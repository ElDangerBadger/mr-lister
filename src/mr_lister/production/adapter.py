"""Application-facing Printify production adapter."""

from __future__ import annotations

from collections.abc import Callable

from mr_lister.contracts import ListingIntelligence, ProductProfile
from mr_lister.production.printify import (
    PrintifyAuthenticationError,
    PrintifyCatalogMismatchError,
    PrintifyInputError,
    PrintifyProductionClient,
    PrintifyUnavailableError,
)
from mr_lister.workflow.errors import (
    ProductionConfigurationError,
    ProductionInputError,
    ProductionUnavailableError,
)
from mr_lister.workflow.models import ArtworkInput


class PrintifyProductionAdapter:
    """Upload source artwork and create an unpublished Printify product."""

    def __init__(
        self,
        *,
        client: PrintifyProductionClient,
        shop_id: int,
        large_artwork_url_provider: Callable[[ArtworkInput], str] | None = None,
    ) -> None:
        if shop_id <= 0:
            raise ValueError("Printify shop ID must be positive")
        self._client = client
        self._shop_id = shop_id
        self._large_artwork_url_provider = large_artwork_url_provider

    def upload_artwork(self, *, job_id: str, artwork: ArtworkInput, content: bytes) -> str:
        del job_id
        suffix = "svg" if artwork.content_type == "image/svg+xml" else "png"
        file_name = f"mr-lister-{artwork.content_sha256}.{suffix}"
        try:
            if len(content) <= self._client.MAX_BASE64_SOURCE_BYTES:
                upload = self._client.upload_artwork_contents(
                    file_name=file_name, content_type=artwork.content_type, content=content
                )
            elif self._large_artwork_url_provider is not None:
                upload = self._client.upload_artwork_url(
                    file_name=file_name,
                    content_type=artwork.content_type,
                    url=self._large_artwork_url_provider(artwork),
                )
            else:
                raise ProductionInputError(
                    "Large artwork requires a configured private HTTPS upload URL"
                )
        except Exception as error:
            self._translate(error)
        return upload.image_id

    def create_product_draft(
        self,
        *,
        job_id: str,
        artwork: ArtworkInput,
        listing: ListingIntelligence,
        profile: ProductProfile,
        image_id: str,
    ) -> str:
        del job_id, artwork
        if profile.publish_enabled:
            raise ProductionInputError("Automatic publication is disabled in Phase 5")
        try:
            resolved = self._client.preflight(shop_id=self._shop_id, profile=profile)
            product = self._client.create_unpublished_product(
                listing=listing, profile=profile, resolved=resolved, image_id=image_id
            )
        except Exception as error:
            self._translate(error)
        return product.product_id

    def publish(self, *, job_id: str, product_id: str) -> str:
        del job_id, product_id
        raise ProductionInputError("Printify publication is not enabled in Phase 5")

    @staticmethod
    def _translate(error: Exception) -> None:
        if isinstance(error, ProductionInputError):
            raise error
        if isinstance(error, PrintifyAuthenticationError):
            raise ProductionConfigurationError(
                "Printify rejected the configured account credential"
            ) from error
        if isinstance(error, PrintifyUnavailableError):
            raise ProductionUnavailableError("Printify did not complete the request") from error
        if isinstance(error, (PrintifyCatalogMismatchError, PrintifyInputError)):
            raise ProductionInputError(
                "Printify rejected the configured production input"
            ) from error
        raise error
