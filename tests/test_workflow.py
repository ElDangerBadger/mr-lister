from __future__ import annotations

from base64 import b64decode

import pytest

from mr_lister.contracts import ApprovalStatus, ArtworkAnalysis, JobState
from mr_lister.workflow.errors import (
    IdempotencyConflictError,
    IntelligenceConfigurationError,
    IntelligenceUnavailableError,
    InvalidArtworkError,
    InvalidGeneratedOutputError,
    InvalidStateError,
    ProfileNotFoundError,
    StaleApprovalError,
)
from mr_lister.workflow.fakes import FakeProductionAdapter
from mr_lister.workflow.models import ListingRevisionRequest
from mr_lister.workflow.service import ListingWorkflow

SYNTHETIC_PNG = b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAIAAAACCAYAAABytg0kAAAAFUlEQVR4nGNU07X8z8DAwMAEIkAYABbVAY+Z/lCyAAAAAElFTkSuQmCC"
)


def submit(
    workflow: ListingWorkflow,
    *,
    key: str = "intake-001",
    content: bytes = SYNTHETIC_PNG,
    profile_id: str = "synthetic_gildan_5000",
):
    return workflow.submit(
        filename="geometric_badger.png",
        content_type="image/png",
        content=content,
        idempotency_key=key,
        profile_id=profile_id,
    )


def revision_from_workflow(workflow: ListingWorkflow, job_id: str) -> ListingRevisionRequest:
    listing = workflow.get_review(job_id).listing
    payload = listing.model_dump(exclude={"contract_version"})
    payload["title"] = "Revised Geometric Badger Graphic Tee"
    return ListingRevisionRequest.model_validate(payload)


def test_submit_prepares_one_reviewable_fake_draft(
    workflow: ListingWorkflow, production: FakeProductionAdapter
) -> None:
    job = submit(workflow)
    review = workflow.get_review(job.job_id)

    assert job.state is JobState.AWAITING_APPROVAL
    assert job.review_version == 1
    assert job.printify_product_id == f"fake-product-{job.job_id}"
    assert review.validation.passed is True
    assert review.validation.issues == ()
    assert review.approval_status is ApprovalStatus.PENDING
    assert production.create_calls == 1


def test_repeated_tag_keywords_are_a_deterministic_validation_error(
    listing,
) -> None:
    from mr_lister.workflow.validation import validate_listing

    result = validate_listing(listing)

    assert result.passed is False
    assert [issue.code for issue in result.issues] == ["TAG_KEYWORD_REPETITION"]
    assert result.issues[0].severity.value == "error"
    assert "badger" in result.issues[0].message


def test_tag_keyword_validation_normalizes_simple_plural_variants() -> None:
    from mr_lister.workflow.validation import (
        find_repeated_tag_keyword_locations,
        find_repeated_tag_keywords,
    )

    tags = ("artist present", "gifts for artists")

    assert find_repeated_tag_keywords(tags) == ("artist",)
    assert find_repeated_tag_keyword_locations(tags) == {"artist": (1, 2)}


def test_repeated_model_keywords_stop_before_production_and_require_revision(
    workflow: ListingWorkflow,
    production: FakeProductionAdapter,
    listing,
) -> None:
    class RepeatingIntelligenceAdapter:
        def inspect_artwork(self, _artwork, _content):
            return ArtworkAnalysis(subject="Badger", confidence=0.9)

        def draft_listing(self, _artwork, _content, _analysis):
            return listing

    workflow.intelligence = RepeatingIntelligenceAdapter()

    job = submit(workflow)
    review = workflow.get_review(job.job_id)

    assert job.state is JobState.NEEDS_REVISION
    assert review.validation.passed is False
    assert [issue.code for issue in review.validation.issues] == ["TAG_KEYWORD_REPETITION"]
    assert production.create_calls == 0
    assert workflow.store.external_writes[job.job_id] == []
    with pytest.raises(InvalidStateError, match="awaiting approval"):
        workflow.approve(job.job_id, review.review_version)


def test_valid_human_revision_recovers_a_repeated_keyword_draft(
    workflow: ListingWorkflow,
    production: FakeProductionAdapter,
    listing,
) -> None:
    class RepeatingIntelligenceAdapter:
        def inspect_artwork(self, _artwork, _content):
            return ArtworkAnalysis(subject="Badger", confidence=0.9)

        def draft_listing(self, _artwork, _content, _analysis):
            return listing

    workflow.intelligence = RepeatingIntelligenceAdapter()
    job = submit(workflow)
    revision_payload = listing.model_dump(exclude={"contract_version"})
    revision_payload["tags"] = (
        "badger portrait",
        "woodland explorer",
        "compass artwork",
        "forest adventure",
        "vintage illustration",
        "outdoor apparel",
        "nature lover gift",
        "crescent moon",
        "pine silhouette",
        "earthy palette",
        "camping keepsake",
        "wildlife design",
        "retro shirt",
    )

    revised = workflow.revise_listing(
        job.job_id,
        ListingRevisionRequest.model_validate(revision_payload),
    )

    assert revised.validation.passed is True
    assert workflow.get_job(job.job_id).state is JobState.AWAITING_APPROVAL
    assert production.create_calls == 1


def test_repeated_intake_returns_same_job_without_duplicate_draft(
    workflow: ListingWorkflow, production: FakeProductionAdapter
) -> None:
    first = submit(workflow)
    repeated = submit(workflow)

    assert repeated.job_id == first.job_id
    assert production.create_calls == 1


def test_reused_intake_key_with_other_artwork_is_rejected(workflow: ListingWorkflow) -> None:
    submit(workflow)

    with pytest.raises(IdempotencyConflictError):
        submit(workflow, content=SYNTHETIC_PNG + b"different")


def test_reused_intake_key_with_other_profile_is_rejected(workflow: ListingWorkflow) -> None:
    submit(workflow)

    with pytest.raises(IdempotencyConflictError):
        submit(workflow, profile_id="another_profile")


def test_profile_id_cannot_escape_configuration_directory(workflow: ListingWorkflow) -> None:
    with pytest.raises(ProfileNotFoundError):
        submit(workflow, profile_id="../../private")


def test_invalid_artwork_fails_before_job_creation(workflow: ListingWorkflow) -> None:
    with pytest.raises(InvalidArtworkError):
        submit(workflow, content=b"not-a-png")

    assert not workflow.store.jobs


def test_truncated_png_fails_before_job_creation(workflow: ListingWorkflow) -> None:
    with pytest.raises(InvalidArtworkError, match="corrupt or incomplete"):
        submit(workflow, content=SYNTHETIC_PNG[:-8])

    assert not workflow.store.jobs


def test_fully_transparent_png_fails_before_job_creation(workflow: ListingWorkflow) -> None:
    from io import BytesIO

    from PIL import Image

    image = Image.new("RGBA", (8, 8), (255, 255, 255, 0))
    output = BytesIO()
    image.save(output, format="PNG")

    with pytest.raises(InvalidArtworkError, match="fully transparent"):
        submit(workflow, content=output.getvalue())

    assert not workflow.store.jobs


def test_fully_transparent_palette_png_fails_before_job_creation(
    workflow: ListingWorkflow,
) -> None:
    from io import BytesIO

    from PIL import Image

    image = Image.new("P", (8, 8), 0)
    image.putpalette([255, 255, 255] + [0, 0, 0] * 255)
    output = BytesIO()
    image.save(output, format="PNG", transparency=0)

    with pytest.raises(InvalidArtworkError, match="fully transparent"):
        submit(workflow, content=output.getvalue())

    assert not workflow.store.jobs


def test_invalid_generated_output_fails_job_predictably(workflow: ListingWorkflow) -> None:
    class MalformedIntelligenceAdapter:
        def inspect_artwork(self, _artwork, _content):
            return ArtworkAnalysis(subject="Badger", confidence=0.9)

        def draft_listing(self, _artwork, _content, _analysis):
            return {
                "title": "Incomplete listing",
                "description": "Only twelve tags are returned.",
                "tags": [f"tag {index}" for index in range(12)],
                "title_rationale": "Synthetic failure",
                "tag_rationale": "Synthetic failure",
            }

    workflow.intelligence = MalformedIntelligenceAdapter()

    with pytest.raises(InvalidGeneratedOutputError):
        submit(workflow)

    failed_job = next(iter(workflow.store.jobs.values()))
    assert failed_job.state is JobState.FAILED_TERMINAL
    assert workflow.store.artwork_contents[failed_job.job_id] == SYNTHETIC_PNG


@pytest.mark.parametrize(
    ("error_type", "expected_state", "expected_event"),
    [
        (
            IntelligenceUnavailableError,
            JobState.FAILED_RETRYABLE,
            "intelligence_temporarily_unavailable",
        ),
        (
            IntelligenceConfigurationError,
            JobState.FAILED_TERMINAL,
            "intelligence_configuration_rejected",
        ),
    ],
)
def test_intelligence_failures_finish_in_explicit_sanitized_states(
    workflow: ListingWorkflow,
    production: FakeProductionAdapter,
    error_type: type[Exception],
    expected_state: JobState,
    expected_event: str,
) -> None:
    sensitive_detail = "provider request body and credential detail"

    class FailingIntelligenceAdapter:
        def inspect_artwork(self, _artwork, _content):
            raise error_type(sensitive_detail)

        def draft_listing(self, _artwork, _content, _analysis):
            raise AssertionError("drafting must not run after inspection fails")

    workflow.intelligence = FailingIntelligenceAdapter()

    with pytest.raises(error_type):
        submit(workflow)

    failed_job = next(iter(workflow.store.jobs.values()))
    events = workflow.store.events[failed_job.job_id]
    assert failed_job.state is expected_state
    assert events[-1].name == expected_event
    assert events[-1].details == {}
    assert all(sensitive_detail not in repr(event.details) for event in events)
    assert failed_job.job_id not in workflow.store.reviews
    assert not workflow.store.external_writes[failed_job.job_id]
    assert production.create_calls == 0


def test_publish_requires_approval(workflow: ListingWorkflow) -> None:
    job = submit(workflow)

    with pytest.raises(InvalidStateError):
        workflow.publish(job.job_id)


def test_publish_requires_profile_permission(workflow: ListingWorkflow) -> None:
    job = submit(workflow)
    review = workflow.get_review(job.job_id)
    workflow.store.reviews[job.job_id] = review.model_copy(
        update={"profile": review.profile.model_copy(update={"publish_enabled": False})}
    )
    workflow.approve(job.job_id, 1)

    with pytest.raises(InvalidStateError, match="does not permit publication"):
        workflow.publish(job.job_id)


def test_revision_invalidates_approval_and_rejects_stale_version(
    workflow: ListingWorkflow,
) -> None:
    job = submit(workflow)
    workflow.approve(job.job_id, 1)

    revised = workflow.revise_listing(job.job_id, revision_from_workflow(workflow, job.job_id))

    assert revised.review_version == 2
    assert revised.approval_status is ApprovalStatus.INVALIDATED
    assert workflow.get_job(job.job_id).state is JobState.AWAITING_APPROVAL
    with pytest.raises(StaleApprovalError):
        workflow.approve(job.job_id, 1)
    with pytest.raises(InvalidStateError):
        workflow.publish(job.job_id)


def test_approved_publication_is_idempotent(
    workflow: ListingWorkflow, production: FakeProductionAdapter
) -> None:
    job = submit(workflow)
    workflow.approve(job.job_id, 1)

    published = workflow.publish(job.job_id)
    repeated = workflow.publish(job.job_id)

    assert published.state is JobState.VERIFIED
    assert repeated.published_listing_id == published.published_listing_id
    assert production.publish_calls == 1


def test_report_contains_traceable_state_events(workflow: ListingWorkflow) -> None:
    job = submit(workflow)
    workflow.approve(job.job_id, 1)
    workflow.publish(job.job_id)

    report = workflow.get_report(job.job_id)

    assert report.job.state is JobState.VERIFIED
    assert report.artwork.filename == "geometric_badger.png"
    assert [write.operation for write in report.external_writes] == [
        "sync_product_draft",
        "publish_listing",
    ]
    assert report.events[0].name == "artwork_uploaded"
    assert report.events[-1].name == "publication_verified"
