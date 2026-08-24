from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from mr_lister.contracts import ListingIntelligence
from mr_lister.production.draft_sync import build_canonical_draft
from mr_lister.production.printify import PrintifyResolvedProfile, PrintifyResolvedVariant
from mr_lister.publication.profile_eligibility import (
    PinnedPublicationProfileEligibilityAuthority,
    PublicationProfileEligibility,
    PublicationProfileEligibilityError,
    build_publication_profile_eligibility,
    require_exact_publication_profile_eligibility,
)
from mr_lister.review_profile import FilesystemReviewProductAuthority

ROOT = Path(__file__).resolve().parents[1]
PROFILE_ID = "gildan_64000_swiftpod"
PROFILE_VERSION = 2
RELEASE_FINGERPRINT = "a" * 64


def _checked_profile():
    return FilesystemReviewProductAuthority(
        profile_directory=ROOT / "config" / "product_profiles"
    ).get_exact(profile_id=PROFILE_ID, profile_version=PROFILE_VERSION)


def _eligibility() -> PublicationProfileEligibility:
    exact = _checked_profile()
    return build_publication_profile_eligibility(
        profile_id=exact.profile.profile_id,
        profile_version=exact.profile.profile_version,
        profile_fingerprint=exact.fingerprint,
        release_manifest_fingerprint=RELEASE_FINGERPRINT,
        phase6_profile_publish_enabled=exact.profile.publish_enabled,
    )


def test_checked_phase6_profile_is_eligible_without_enabling_any_runtime_capability() -> None:
    exact = _checked_profile()

    assert exact.profile.publish_enabled is False
    eligibility = _eligibility()

    assert eligibility.profile_id == PROFILE_ID
    assert eligibility.profile_version == PROFILE_VERSION
    assert eligibility.profile_fingerprint == exact.fingerprint
    assert eligibility.expected_sales_channel == "etsy"
    assert eligibility.release_manifest_fingerprint == RELEASE_FINGERPRINT
    assert eligibility.phase6_profile_publish_enabled is False
    assert eligibility.publication_eligible is True
    assert eligibility.seller_request_enabled is False
    assert eligibility.provider_mutation_enabled is False
    assert "eligibility_id" not in PublicationProfileEligibility.model_fields
    assert "eligibility_fingerprint" not in PublicationProfileEligibility.model_fields


def test_same_checked_draft_safe_profile_crosses_phase6_and_phase7_eligibility() -> None:
    exact = _checked_profile()
    profile = exact.profile
    group_by_size = {size: group for group in profile.placement_groups for size in group.sizes}
    variants = tuple(
        PrintifyResolvedVariant(
            variant_id=index,
            color=color,
            size=size,
            placement_group_id=group_by_size[size].group_id,
            canvas_width=group_by_size[size].canvas_width,
            canvas_height=group_by_size[size].canvas_height,
            retail_price_cents=profile.retail_price_cents,
        )
        for index, (color, size) in enumerate(
            ((color, size) for color in profile.colors for size in profile.sizes),
            start=1,
        )
    )
    resolved = PrintifyResolvedProfile(
        profile_id=profile.profile_id,
        profile_version=profile.profile_version,
        shop_id=42,
        blueprint_id=profile.blueprint_id,
        print_provider_id=profile.print_provider_id,
        variants=variants,
    )
    listing = ListingIntelligence(
        title="Geometric Badger Graphic Tee",
        description="A checked-profile cross-phase authority fixture.",
        tags=tuple(f"badger tag {index}" for index in range(1, 14)),
        title_rationale="Names the subject and product.",
        tag_rationale="Covers the exact listing intent.",
    )

    draft = build_canonical_draft(
        job_id="job_phase74_cross_phase",
        listing=listing,
        profile=profile,
        resolved=resolved,
        image_id="image_phase74",
    )
    eligibility = _eligibility()

    assert len(draft.variants) == len(profile.colors) * len(profile.sizes)
    assert draft.blueprint_id == profile.blueprint_id
    assert eligibility.profile_fingerprint == exact.fingerprint
    assert eligibility.phase6_profile_publish_enabled is False
    assert eligibility.publication_eligible is True
    assert eligibility.seller_request_enabled is False
    assert eligibility.provider_mutation_enabled is False


def test_eligibility_is_strict_frozen_and_has_closed_semantics() -> None:
    eligibility = _eligibility()

    with pytest.raises(ValidationError):
        eligibility.seller_request_enabled = True  # type: ignore[misc]

    payload = eligibility.model_dump(mode="python")
    for field, changed in (
        ("phase6_profile_publish_enabled", True),
        ("publication_eligible", False),
        ("seller_request_enabled", True),
        ("provider_mutation_enabled", True),
        ("expected_sales_channel", "custom"),
    ):
        with pytest.raises(ValidationError):
            PublicationProfileEligibility.model_validate({**payload, field: changed})
    with pytest.raises(ValidationError):
        PublicationProfileEligibility.model_validate({**payload, "unexpected": False})


@pytest.mark.parametrize(
    ("field", "changed"),
    [
        ("profile_id", "another_profile"),
        ("profile_version", 3),
        ("profile_fingerprint", "b" * 64),
        ("release_manifest_fingerprint", "c" * 64),
        ("expected_sales_channel", "custom"),
        ("phase6_profile_publish_enabled", True),
    ],
)
def test_pinned_authority_rejects_every_changed_constituent(
    field: str,
    changed: object,
) -> None:
    eligibility = _eligibility()
    authority = PinnedPublicationProfileEligibilityAuthority(eligibility)
    request = {
        "profile_id": eligibility.profile_id,
        "profile_version": eligibility.profile_version,
        "profile_fingerprint": eligibility.profile_fingerprint,
        "release_manifest_fingerprint": eligibility.release_manifest_fingerprint,
        "expected_sales_channel": eligibility.expected_sales_channel,
        "phase6_profile_publish_enabled": eligibility.phase6_profile_publish_enabled,
    }
    request[field] = changed

    with pytest.raises(PublicationProfileEligibilityError) as captured:
        authority.get_exact(**request)  # type: ignore[arg-type]

    assert str(captured.value) == "Publication profile eligibility is unavailable"


def test_pinned_authority_returns_only_the_exact_immutable_record() -> None:
    eligibility = _eligibility()
    authority = PinnedPublicationProfileEligibilityAuthority(eligibility)

    resolved = authority.get_exact(
        profile_id=eligibility.profile_id,
        profile_version=eligibility.profile_version,
        profile_fingerprint=eligibility.profile_fingerprint,
        expected_sales_channel=eligibility.expected_sales_channel,
        release_manifest_fingerprint=eligibility.release_manifest_fingerprint,
        phase6_profile_publish_enabled=False,
    )

    assert resolved == eligibility
    assert resolved is not eligibility


@pytest.mark.parametrize(
    "updates",
    [
        {"profile_version": True},
        {"profile_fingerprint": "not-a-fingerprint"},
        {"release_manifest_fingerprint": "0" * 64},
        {"phase6_profile_publish_enabled": 0},
    ],
)
def test_builder_fails_closed_with_one_value_free_error(updates: dict[str, object]) -> None:
    exact = _checked_profile()
    values: dict[str, object] = {
        "profile_id": exact.profile.profile_id,
        "profile_version": exact.profile.profile_version,
        "profile_fingerprint": exact.fingerprint,
        "release_manifest_fingerprint": RELEASE_FINGERPRINT,
        "phase6_profile_publish_enabled": exact.profile.publish_enabled,
    }
    values.update(updates)

    with pytest.raises(PublicationProfileEligibilityError) as captured:
        build_publication_profile_eligibility(**values)  # type: ignore[arg-type]

    assert str(captured.value) == "Publication profile eligibility is unavailable"


def test_exact_reparse_rejects_tampering_without_echoing_values() -> None:
    eligibility = _eligibility()
    payload = eligibility.model_dump(mode="python")
    payload["provider_mutation_enabled"] = True

    with pytest.raises(PublicationProfileEligibilityError) as captured:
        require_exact_publication_profile_eligibility(
            payload,
            profile_id=eligibility.profile_id,
            profile_version=eligibility.profile_version,
            profile_fingerprint=eligibility.profile_fingerprint,
            expected_sales_channel="etsy",
            release_manifest_fingerprint=eligibility.release_manifest_fingerprint,
            phase6_profile_publish_enabled=False,
        )

    assert str(captured.value) == "Publication profile eligibility is unavailable"
