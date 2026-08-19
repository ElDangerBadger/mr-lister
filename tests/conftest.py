from __future__ import annotations

from base64 import b64decode
from datetime import UTC, datetime
from pathlib import Path

import pytest

from mr_lister.contracts import (
    ArtworkAnalysis,
    ListingIntelligence,
    Placement,
    ProductProfile,
    ValidationResult,
)
from mr_lister.workflow.fakes import FakeIntelligenceAdapter, FakeProductionAdapter
from mr_lister.workflow.profiles import ProductProfileRepository
from mr_lister.workflow.service import ListingWorkflow
from mr_lister.workflow.store import InMemoryJobStore

SYNTHETIC_PNG = b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAIAAAACCAYAAABytg0kAAAAFUlEQVR4nGNU07X8z8DAwMAEIkAYABbVAY+Z/lCyAAAAAElFTkSuQmCC"
)


@pytest.fixture
def artwork_analysis() -> ArtworkAnalysis:
    return ArtworkAnalysis(subject="A geometric badger", styles=("low poly",), confidence=0.92)


@pytest.fixture
def listing() -> ListingIntelligence:
    return ListingIntelligence(
        title="Geometric Badger Graphic Tee",
        description="A synthetic listing fixture for contract tests.",
        tags=tuple(f"badger tag {index}" for index in range(1, 14)),
        audience=("wildlife art fans",),
        title_rationale="Names the subject and product clearly.",
        tag_rationale="Covers subject, style, product, and audience intent.",
    )


@pytest.fixture
def product_profile() -> ProductProfile:
    return ProductProfile(
        profile_id="synthetic_gildan_5000",
        profile_version=1,
        blueprint_id=900001,
        print_provider_id=900002,
        variant_ids=(900101, 900102, 900103),
        retail_price_cents=2499,
        placement=Placement(x=0.5, y=0.3183, scale=0.65),
    )


@pytest.fixture
def valid_result() -> ValidationResult:
    return ValidationResult(passed=True)


@pytest.fixture
def now() -> datetime:
    return datetime(2026, 8, 18, 12, 0, tzinfo=UTC)


@pytest.fixture
def production() -> FakeProductionAdapter:
    return FakeProductionAdapter()


@pytest.fixture
def workflow(production: FakeProductionAdapter, now: datetime) -> ListingWorkflow:
    return ListingWorkflow(
        store=InMemoryJobStore(),
        profiles=ProductProfileRepository(Path("config/product_profiles")),
        intelligence=FakeIntelligenceAdapter(),
        production=production,
        clock=lambda: now,
        job_id_factory=lambda: "job_phase1_fixture",
    )
