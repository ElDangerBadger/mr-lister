"""Interfaces around the Phase 1 external boundaries."""

from __future__ import annotations

from typing import Protocol

from mr_lister.contracts import ArtworkAnalysis, ListingIntelligence, ProductProfile
from mr_lister.workflow.models import ArtworkInput


class IntelligencePort(Protocol):
    def inspect_artwork(self, artwork: ArtworkInput) -> ArtworkAnalysis: ...

    def draft_listing(
        self, artwork: ArtworkInput, analysis: ArtworkAnalysis
    ) -> ListingIntelligence: ...


class ProductionPort(Protocol):
    def create_draft(
        self,
        *,
        job_id: str,
        artwork: ArtworkInput,
        listing: ListingIntelligence,
        profile: ProductProfile,
    ) -> tuple[str, str]: ...

    def publish(self, *, job_id: str, product_id: str) -> str: ...
