from __future__ import annotations

from datetime import timedelta

import pytest
from pydantic import ValidationError

from mr_lister.contracts import (
    ApprovalStatus,
    ArtworkAnalysis,
    JobRecord,
    JobState,
    ListingIntelligence,
    ProductProfile,
    ReviewSnapshot,
    ValidationIssue,
    ValidationResult,
    ValidationSeverity,
)


def test_contracts_reject_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        ArtworkAnalysis(subject="Badger", confidence=0.8, prompt_override="publish now")


def test_contracts_are_immutable(artwork_analysis: ArtworkAnalysis) -> None:
    with pytest.raises(ValidationError):
        artwork_analysis.subject = "Changed subject"


def test_listing_requires_exactly_thirteen_tags(listing: ListingIntelligence) -> None:
    payload = listing.model_dump()
    payload["tags"] = payload["tags"][:-1]

    with pytest.raises(ValidationError):
        ListingIntelligence.model_validate(payload)


def test_listing_rejects_normalized_duplicate_tags(listing: ListingIntelligence) -> None:
    payload = listing.model_dump()
    payload["tags"] = (*payload["tags"][:-1], "  BADGER   TAG 1 ")

    with pytest.raises(ValidationError, match="tags must be unique"):
        ListingIntelligence.model_validate(payload)


def test_product_profile_rejects_duplicate_variants(product_profile: ProductProfile) -> None:
    payload = product_profile.model_dump()
    payload["variant_ids"] = (900101, 900101)

    with pytest.raises(ValidationError, match="Variant IDs must be unique"):
        ProductProfile.model_validate(payload)


@pytest.mark.parametrize(
    ("passed", "severity"),
    [(True, ValidationSeverity.ERROR), (False, ValidationSeverity.WARNING)],
)
def test_validation_status_must_match_error_presence(
    passed: bool, severity: ValidationSeverity
) -> None:
    issue = ValidationIssue(code="TEST_ISSUE", message="Synthetic issue", severity=severity)

    with pytest.raises(ValidationError, match="passed must be true"):
        ValidationResult(passed=passed, issues=(issue,))


def test_invalid_review_cannot_be_approved(
    artwork_analysis: ArtworkAnalysis,
    listing: ListingIntelligence,
    product_profile: ProductProfile,
) -> None:
    issue = ValidationIssue(
        code="INVALID_LISTING",
        message="Synthetic invalid listing",
        severity=ValidationSeverity.ERROR,
    )
    invalid_result = ValidationResult(passed=False, issues=(issue,))

    with pytest.raises(ValidationError, match="cannot be approved"):
        ReviewSnapshot(
            review_version=1,
            artwork_analysis=artwork_analysis,
            listing=listing,
            profile=product_profile,
            validation=invalid_result,
            approval_status=ApprovalStatus.APPROVED,
        )


def test_approval_bound_job_requires_current_review_version(now) -> None:
    with pytest.raises(ValidationError, match="approval for the current review"):
        JobRecord(
            job_id="job_001",
            state=JobState.APPROVED,
            review_version=2,
            approved_review_version=1,
            idempotency_key="synthetic-key",
            artwork_object_key="synthetic/artwork.png",
            created_at=now,
            updated_at=now,
        )


def test_job_timestamps_are_monotonic(now) -> None:
    with pytest.raises(ValidationError, match="updated_at cannot precede"):
        JobRecord(
            job_id="job_001",
            state=JobState.UPLOADED,
            review_version=0,
            idempotency_key="synthetic-key",
            artwork_object_key="synthetic/artwork.png",
            created_at=now,
            updated_at=now - timedelta(seconds=1),
        )
