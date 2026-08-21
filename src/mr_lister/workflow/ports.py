"""Interfaces around the Phase 1 external boundaries."""

from __future__ import annotations

from typing import Protocol

from mr_lister.contracts import ArtworkAnalysis, ListingIntelligence, ProductProfile
from mr_lister.workflow.models import ArtworkInput


class IntelligencePort(Protocol):
    def inspect_artwork(self, artwork: ArtworkInput, content: bytes) -> ArtworkAnalysis: ...

    def draft_listing(
        self, artwork: ArtworkInput, content: bytes, analysis: ArtworkAnalysis
    ) -> ListingIntelligence: ...


class ProductionPort(Protocol):
    def upload_artwork(
        self,
        *,
        job_id: str,
        artwork: ArtworkInput,
        content: bytes,
    ) -> str: ...

    def create_product_draft(
        self,
        *,
        job_id: str,
        artwork: ArtworkInput,
        listing: ListingIntelligence,
        profile: ProductProfile,
        image_id: str,
    ) -> str: ...

    def publish(self, *, job_id: str, product_id: str) -> str: ...
