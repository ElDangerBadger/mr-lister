"""In-memory Phase 1 persistence with an explicit idempotency ledger."""

from __future__ import annotations

from collections import defaultdict

from mr_lister.contracts import JobRecord, ReviewSnapshot
from mr_lister.workflow.errors import IdempotencyConflictError, JobNotFoundError
from mr_lister.workflow.models import ArtworkInput, ExternalWriteRecord, WorkflowEvent


class InMemoryJobStore:
    def __init__(self) -> None:
        self.jobs: dict[str, JobRecord] = {}
        self.artworks: dict[str, ArtworkInput] = {}
        self.artwork_contents: dict[str, bytes] = {}
        self.profile_ids: dict[str, str] = {}
        self.reviews: dict[str, ReviewSnapshot] = {}
        self.events: dict[str, list[WorkflowEvent]] = defaultdict(list)
        self.external_writes: dict[str, list[ExternalWriteRecord]] = defaultdict(list)
        self.intake_keys: dict[str, tuple[str, str]] = {}

    def resolve_intake(self, key: str, fingerprint: str) -> str | None:
        existing = self.intake_keys.get(key)
        if existing is None:
            return None
        existing_fingerprint, job_id = existing
        if existing_fingerprint != fingerprint:
            raise IdempotencyConflictError("Idempotency key was already used for other artwork")
        return job_id

    def bind_intake(self, key: str, fingerprint: str, job_id: str) -> None:
        self.intake_keys[key] = (fingerprint, job_id)

    def get_job(self, job_id: str) -> JobRecord:
        try:
            return self.jobs[job_id]
        except KeyError as error:
            raise JobNotFoundError(f"Unknown job: {job_id}") from error

    def get_review(self, job_id: str) -> ReviewSnapshot:
        self.get_job(job_id)
        return self.reviews[job_id]
