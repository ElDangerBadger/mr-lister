"""Job-scoped Strands tools that cannot approve or publish listings."""

from __future__ import annotations

from typing import Any

from pydantic import ValidationError
from strands import tool

from mr_lister.workflow.errors import WorkflowError
from mr_lister.workflow.models import ListingRevisionRequest
from mr_lister.workflow.service import ListingWorkflow
from mr_lister.workflow.validation import validate_listing


class PreparationTools:
    """A capability-scoped tool provider bound to exactly one workflow job."""

    def __init__(self, workflow: ListingWorkflow, job_id: str) -> None:
        self._workflow = workflow
        self._job_id = job_id

    @tool
    def prepare_staged_listing(self) -> dict[str, Any]:
        """Create the bounded listing draft for the scoped, validated intake.

        The result is staged for human review and does not approve or publish the listing.
        """

        try:
            job = self._workflow.prepare(self._job_id)
            review = self._workflow.get_review(self._job_id)
        except WorkflowError as error:
            return _workflow_error(error)
        except KeyError:
            return _error("INTAKE_NOT_READY", "The validated intake is not available yet")
        return {
            "ok": True,
            "state": job.state.value,
            "review_version": review.review_version,
            "validation": review.validation.model_dump(mode="json"),
            "requires_human_approval": True,
            "publication_authorized": False,
        }

    @tool
    def inspect_staged_review(self) -> dict[str, Any]:
        """Inspect the current artwork analysis, listing draft, and validation state."""

        try:
            job = self._workflow.get_job(self._job_id)
            review = self._workflow.get_review(self._job_id)
        except WorkflowError as error:
            return _workflow_error(error)
        except KeyError:
            return _error("REVIEW_NOT_READY", "The staged review is not available yet")
        return {
            "ok": True,
            "job": {
                "state": job.state.value,
                "review_version": job.review_version,
            },
            "artwork_analysis": review.artwork_analysis.model_dump(mode="json"),
            "listing": review.listing.model_dump(mode="json"),
            "validation": review.validation.model_dump(mode="json"),
            "approval_status": review.approval_status.value,
        }

    @tool
    def validate_staged_listing(self) -> dict[str, Any]:
        """Re-run deterministic validation against the current staged listing."""

        try:
            review = self._workflow.get_review(self._job_id)
        except WorkflowError as error:
            return _workflow_error(error)
        except KeyError:
            return _error("REVIEW_NOT_READY", "The staged review is not available yet")
        result = validate_listing(review.listing)
        return {
            "ok": True,
            "review_version": review.review_version,
            "validation": result.model_dump(mode="json"),
        }

    @tool
    def revise_staged_listing(
        self,
        title: str,
        description: str,
        tags: list[str],
        audience: list[str],
        title_rationale: str,
        tag_rationale: str,
    ) -> dict[str, Any]:
        """Stage a complete replacement listing for deterministic validation and human review.

        This tool can revise a draft but cannot approve or publish it.

        Args:
            title: Complete replacement Etsy title.
            description: Complete replacement listing description.
            tags: Exactly 13 complete replacement Etsy tags.
            audience: Complete replacement audience hypotheses.
            title_rationale: Brief explanation of the title strategy.
            tag_rationale: Brief explanation of the tag strategy.
        """

        try:
            revision = ListingRevisionRequest(
                title=title,
                description=description,
                tags=tuple(tags),
                audience=tuple(audience),
                title_rationale=title_rationale,
                tag_rationale=tag_rationale,
            )
            review = self._workflow.revise_listing(self._job_id, revision)
            job = self._workflow.get_job(self._job_id)
        except ValidationError:
            return _error("INVALID_REVISION", "The proposed revision failed its input contract")
        except WorkflowError as error:
            return _workflow_error(error)
        return {
            "ok": True,
            "state": job.state.value,
            "review_version": review.review_version,
            "validation": review.validation.model_dump(mode="json"),
            "requires_human_approval": True,
            "publication_authorized": False,
        }


def _workflow_error(error: WorkflowError) -> dict[str, Any]:
    messages = {
        "JOB_NOT_FOUND": "The scoped job does not exist",
        "INVALID_STATE": "The scoped job cannot perform that preparation action now",
        "STALE_APPROVAL": "The scoped job approval is stale",
    }
    return _error(error.code, messages.get(error.code, "The preparation action was rejected"))


def _error(code: str, message: str) -> dict[str, Any]:
    return {"ok": False, "error": {"code": code, "message": message}}
