from __future__ import annotations

import pytest
from pydantic import ValidationError

from mr_lister.contracts.presentation import ProductMockupEvidence
from mr_lister.review_security import is_safe_mockup_url, is_safe_preview_url


@pytest.mark.parametrize(
    "value",
    (
        "http://images.printify.com/mockup.png",
        "https://images-api.printify.com/mockup.png",
        "https://images.printify.com.evil.test/mockup.png",
        "https://images.printify.com@evil.test/mockup.png",
        "https://images.printify.com:443/mockup.png",
        "//images.printify.com/mockup.png",
        "https://images.printify.com/mockup.png#fragment",
        "https://images.printify.com\\@evil.test/mockup.png",
        "https://images.printify.com/%not-hex.png",
        "https://images.printify.com/",
        "https://IMAGES.PRINTIFY.COM/mockup.png",
    ),
)
def test_hostile_mockup_urls_fail_closed(value: str) -> None:
    assert is_safe_mockup_url(value) is False


def test_exact_mockup_origin_with_query_is_accepted() -> None:
    assert is_safe_mockup_url("https://images.printify.com/mockup/front.png?width=1200&quality=90")


def test_preview_requires_the_exact_configured_origin() -> None:
    assert is_safe_preview_url(
        (
            "https://review.mr-lister.test/v1/jobs/job_projection/artwork-preview"
            "?grant=opaque_preview_grant_12345"
        ),
        exact_origin="https://review.mr-lister.test",
        job_id="job_projection",
    )
    assert not is_safe_preview_url(
        (
            "https://review.mr-lister.test.evil.test/v1/jobs/job_projection/artwork-preview"
            "?grant=opaque_preview_grant_12345"
        ),
        exact_origin="https://review.mr-lister.test",
        job_id="job_projection",
    )


@pytest.mark.parametrize(
    "value",
    (
        (
            "https://review.mr-lister.test/private/owners/owner/jobs/job_projection/"
            "source/source.png?grant=opaque_preview_grant_12345"
        ),
        (
            "https://review.mr-lister.test/v1/jobs/job_projection/artwork-preview%2Fprivate"
            "%2Fowners%2Fowner?grant=opaque_preview_grant_12345"
        ),
        (
            "https://review.mr-lister.test/v1/jobs/job_projection/artwork-preview"
            "?grant=private%2Fowners%2Fowner"
        ),
        (
            "https://review.mr-lister.test/v1/jobs/another_job/artwork-preview"
            "?grant=opaque_preview_grant_12345"
        ),
    ),
)
def test_preview_rejects_storage_coordinates_and_wrong_job(value: str) -> None:
    assert not is_safe_preview_url(
        value,
        exact_origin="https://review.mr-lister.test",
        job_id="job_projection",
    )


def test_mockup_position_is_bounded_before_seller_projection() -> None:
    with pytest.raises(ValidationError):
        ProductMockupEvidence(
            url="https://images.printify.com/mockup.png",
            position="a" * 65,
        )
