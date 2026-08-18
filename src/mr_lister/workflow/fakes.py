"""Deterministic Phase 1 adapters with no network or external writes."""

from pathlib import PurePath

from mr_lister.contracts import ArtworkAnalysis, ListingIntelligence, ProductProfile
from mr_lister.workflow.models import ArtworkInput


class FakeIntelligenceAdapter:
    def inspect_artwork(self, artwork: ArtworkInput) -> ArtworkAnalysis:
        subject = PurePath(artwork.filename).stem.replace("_", " ").replace("-", " ").strip()
        return ArtworkAnalysis(
            subject=subject or "untitled artwork",
            styles=("synthetic fixture",),
            themes=("print on demand",),
            confidence=0.9,
        )

    def draft_listing(
        self, artwork: ArtworkInput, analysis: ArtworkAnalysis
    ) -> ListingIntelligence:
        subject = analysis.subject.title()
        return ListingIntelligence(
            title=f"{subject} Graphic Tee",
            description=(
                f"A synthetic Phase 1 listing draft inspired by {analysis.subject}. "
                "Review every field before approving publication."
            ),
            tags=(
                "graphic tee",
                "art shirt",
                "pod design",
                "gift for artists",
                "casual shirt",
                "unisex tee",
                "statement shirt",
                "creative gift",
                "printed apparel",
                "everyday tee",
                "unique artwork",
                "modern graphic",
                "seller fixture",
            ),
            audience=("art lovers", "gift shoppers"),
            title_rationale="Uses the interpreted subject and identifies the product.",
            tag_rationale="Synthetic tags cover product, artwork, and buyer intent.",
        )


class FakeProductionAdapter:
    def __init__(self) -> None:
        self.drafts: dict[str, tuple[str, str]] = {}
        self.publications: dict[str, str] = {}
        self.create_calls = 0
        self.publish_calls = 0

    def create_draft(
        self,
        *,
        job_id: str,
        artwork: ArtworkInput,
        listing: ListingIntelligence,
        profile: ProductProfile,
    ) -> tuple[str, str]:
        del artwork, listing, profile
        if job_id not in self.drafts:
            self.create_calls += 1
            self.drafts[job_id] = (f"fake-image-{job_id}", f"fake-product-{job_id}")
        return self.drafts[job_id]

    def publish(self, *, job_id: str, product_id: str) -> str:
        del product_id
        if job_id not in self.publications:
            self.publish_calls += 1
            self.publications[job_id] = f"fake-listing-{job_id}"
        return self.publications[job_id]
