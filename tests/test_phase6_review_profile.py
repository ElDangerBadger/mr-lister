from __future__ import annotations

from pathlib import Path

from mr_lister.control.fingerprints import canonical_fingerprint
from mr_lister.review_profile import ExactReviewProductProfile, FilesystemReviewProductAuthority


def test_exact_profile_presentation_is_bound_to_version_and_fingerprint() -> None:
    authority = FilesystemReviewProductAuthority(
        profile_directory=Path("config/product_profiles"),
    )

    exact = authority.get_exact(
        profile_id="gildan_64000_swiftpod",
        profile_version=2,
    )

    assert exact.product_name == "Product profile gildan_64000_swiftpod"
    assert exact.provider_name == "Print provider 39"
    assert exact.profile.profile_version == 2
    assert exact.fingerprint == canonical_fingerprint(exact.profile)


def test_seller_facing_labels_are_derived_only_from_fingerprinted_profile_fields() -> None:
    original = FilesystemReviewProductAuthority(
        profile_directory=Path("config/product_profiles")
    ).get_exact(profile_id="gildan_64000_swiftpod", profile_version=2)
    changed_profile = original.profile.model_copy(
        update={"profile_id": "gildan_64000_other", "print_provider_id": 40}
    )
    changed = ExactReviewProductProfile(
        profile=changed_profile,
        fingerprint=canonical_fingerprint(changed_profile),
    )

    assert changed.product_name == "Product profile gildan_64000_other"
    assert changed.provider_name == "Print provider 40"
    assert changed.fingerprint != original.fingerprint
