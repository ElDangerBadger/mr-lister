from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from typing import Any

import pytest

from mr_lister.cloud.api import (
    ReviewQueryApiAdapter,
    SellerCommandApiAdapter,
    UploadApiAdapter,
)
from mr_lister.cloud.http import PROTECTED_ROUTE_KEYS
from mr_lister.control.commands import (
    ApproveReviewCommand,
    CancelJobCommand,
    RecordWorkerFailureCommand,
    RetryJobCommand,
    WorkerFailureCode,
)
from mr_lister.control.errors import (
    ConcurrentControlModificationError,
    IdempotencyConflictError,
    NotFoundError,
)
from mr_lister.control.models import CommandResponse, ControlJobState, WorkRequestStatus
from mr_lister.control.service import SellerControlService
from mr_lister.control.store import CommandCommit, InMemorySellerControlStore, OwnerJobPage
from tests import test_phase6_cloud_api as cloud_api
from tests import test_phase6_control_service as control_service
from tests import test_phase6_upload_service as upload_service


class _ThreeWayCommitBarrierStore(InMemorySellerControlStore):
    """Force all three seller commands to build from the same persisted authority."""

    def __init__(self) -> None:
        super().__init__()
        self._authority_gate = Barrier(3)
        self._authority_race_armed = False

    def arm_authority_race(self) -> None:
        self._authority_race_armed = True

    def commit_command(self, commit: CommandCommit):  # type: ignore[no-untyped-def]
        if self._authority_race_armed:
            self._authority_gate.wait(timeout=5)
        return super().commit_command(commit)


def test_revise_approve_cancel_barrier_has_exactly_one_authority_winner() -> None:
    store = _ThreeWayCommitBarrierStore()
    job, review, sync, pricing = control_service.seed_reviewable(store)
    service = SellerControlService(store=store, clock=lambda: control_service.NOW)
    revise = control_service.revision_command(
        job,
        review,
        sync,
        pricing,
        key="phase66-race-revise",
    )
    approve = ApproveReviewCommand(
        job_id=job.job_id,
        owner_id=control_service.OWNER,
        expected_record_version=job.record_version,
        expected_review_version=review.review_version,
        expected_review_fingerprint=review.fingerprint,
        expected_review_etag=control_service.current_etag(job, review, sync, pricing),
        idempotency_key="phase66-race-approve",
    )
    cancel = CancelJobCommand(
        job_id=job.job_id,
        owner_id=control_service.OWNER,
        expected_record_version=job.record_version,
        idempotency_key="phase66-race-cancel",
    )
    events_before = len(store.list_events(job.job_id))
    review_decisions_before = len(store.list_review_decisions(job.job_id))
    cancellations_before = len(store.list_cancellation_decisions(job.job_id))
    work_ids_before = {work.work_request_id for work in store.list_work_requests(job.job_id)}
    store.arm_authority_race()

    calls = {
        "revise": lambda: service.revise_listing(revise),
        "approve": lambda: service.approve_review(approve),
        "cancel": lambda: service.cancel_job(cancel),
    }

    def invoke(name: str) -> tuple[str, CommandResponse | BaseException]:
        try:
            return name, calls[name]()
        except BaseException as error:  # returned for exact winner/loser assertions
            return name, error

    with ThreadPoolExecutor(max_workers=3) as executor:
        results = dict(executor.map(invoke, calls))

    winners = {
        name: result for name, result in results.items() if isinstance(result, CommandResponse)
    }
    conflicts = [
        result
        for result in results.values()
        if isinstance(result, ConcurrentControlModificationError)
    ]
    assert len(winners) == 1
    assert len(conflicts) == 2

    winner, response = next(iter(winners.items()))
    persisted = store.get_job(job.job_id)
    expected_state = {
        "revise": ControlJobState.PRODUCT_DRAFT_SYNCING,
        "approve": ControlJobState.APPROVED,
        "cancel": ControlJobState.CANCELLED,
    }[winner]
    assert response.state is persisted.state is expected_state
    assert persisted.record_version == job.record_version + 1
    assert persisted.event_sequence == job.event_sequence + 1
    assert len(store.list_events(job.job_id)) == events_before + 1

    new_review_decisions = len(store.list_review_decisions(job.job_id)) - review_decisions_before
    new_cancellations = len(store.list_cancellation_decisions(job.job_id)) - cancellations_before
    assert new_review_decisions + new_cancellations == 1
    assert new_cancellations == (winner == "cancel")
    assert new_review_decisions == (winner in {"revise", "approve"})

    new_work_ids = {
        work.work_request_id for work in store.list_work_requests(job.job_id)
    } - work_ids_before
    assert len(new_work_ids) == (winner == "revise")
    if winner == "revise":
        assert new_work_ids == {response.work_request_id}


def test_exact_approve_replay_returns_one_authority_transition() -> None:
    store = InMemorySellerControlStore()
    job, review, sync, pricing = control_service.seed_reviewable(store)
    service = SellerControlService(store=store, clock=lambda: control_service.NOW)
    command = ApproveReviewCommand(
        job_id=job.job_id,
        owner_id=control_service.OWNER,
        expected_record_version=job.record_version,
        expected_review_version=review.review_version,
        expected_review_fingerprint=review.fingerprint,
        expected_review_etag=control_service.current_etag(job, review, sync, pricing),
        idempotency_key="phase66-approve-replay",
    )
    events_before = len(store.list_events(job.job_id))

    first = service.approve_review(command)
    replay = service.approve_review(command)

    assert replay == first
    assert first.state is ControlJobState.APPROVED
    assert len(store.list_events(job.job_id)) == events_before + 1
    assert len(store.list_review_decisions(job.job_id)) == 1
    assert store.list_cancellation_decisions(job.job_id) == ()


def test_exact_cancel_replay_returns_one_authority_transition() -> None:
    store = InMemorySellerControlStore()
    job, _review, _sync, _pricing = control_service.seed_reviewable(store)
    service = SellerControlService(store=store, clock=lambda: control_service.NOW)
    command = CancelJobCommand(
        job_id=job.job_id,
        owner_id=control_service.OWNER,
        expected_record_version=job.record_version,
        idempotency_key="phase66-cancel-replay",
    )
    events_before = len(store.list_events(job.job_id))

    first = service.cancel_job(command)
    replay = service.cancel_job(command)

    assert replay == first
    assert first.state is ControlJobState.CANCELLED
    assert len(store.list_events(job.job_id)) == events_before + 1
    assert len(store.list_cancellation_decisions(job.job_id)) == 1
    assert store.list_review_decisions(job.job_id) == ()


def test_exact_retry_replay_creates_one_recovery_work_request() -> None:
    store = InMemorySellerControlStore()
    syncing, _review, work = control_service.seed_product_syncing(store)
    service = SellerControlService(store=store, clock=lambda: control_service.NOW)
    failed = service.record_worker_failure(
        RecordWorkerFailureCommand(
            job_id=syncing.job_id,
            work_request_id=work.work_request_id,
            expected_record_version=syncing.record_version,
            code=WorkerFailureCode.PRODUCTION_UNAVAILABLE,
        )
    )
    command = RetryJobCommand(
        job_id=syncing.job_id,
        owner_id=control_service.OWNER,
        expected_record_version=failed.record_version,
        idempotency_key="phase66-retry-replay",
    )
    events_before = len(store.list_events(syncing.job_id))
    work_ids_before = {item.work_request_id for item in store.list_work_requests(syncing.job_id)}

    first = service.retry_job(command)
    replay = service.retry_job(command)

    assert replay == first
    assert first.state is ControlJobState.PRODUCT_DRAFT_SYNCING
    assert len(store.list_events(syncing.job_id)) == events_before + 1
    new_work = [
        item
        for item in store.list_work_requests(syncing.job_id)
        if item.work_request_id not in work_ids_before
    ]
    assert len(new_work) == 1
    assert new_work[0].work_request_id == first.work_request_id
    assert new_work[0].status is WorkRequestStatus.PENDING


def test_exact_upload_cancel_replay_creates_one_terminal_receipt() -> None:
    harness = upload_service._harness()
    _content, created = upload_service._create(harness)
    command = {
        "owner_id": upload_service.OWNER,
        "upload_id": created.receipt.upload_id,
        "idempotency_key": "phase66-upload-cancel-replay",
    }

    first = harness.service.cancel_upload(**command)
    replay = harness.service.cancel_upload(**command)

    assert replay == first
    assert first.receipt.status.value == "cancelled"
    assert len(harness.store._upload_intents) == 1
    matching_receipts = [
        receipt
        for receipt in harness.store._upload_receipts.values()
        if receipt.command_type.value == "cancel_upload"
    ]
    assert matching_receipts == [first.receipt]
    assert harness.store.jobs == {}


def test_changed_listing_content_cannot_reuse_a_successful_idempotency_key() -> None:
    store = InMemorySellerControlStore()
    job, review, sync, pricing = control_service.seed_reviewable(store)
    service = SellerControlService(store=store, clock=lambda: control_service.NOW)
    command = control_service.revision_command(
        job,
        review,
        sync,
        pricing,
        key="phase66-changed-revision",
    )

    first = service.revise_listing(command)
    decisions_before = store.list_review_decisions(job.job_id)
    events_before = store.list_events(job.job_id)
    work_before = store.list_work_requests(job.job_id)

    with pytest.raises(IdempotencyConflictError):
        service.revise_listing(
            control_service.revision_command(
                job,
                review,
                sync,
                pricing,
                key="phase66-changed-revision",
                title="A Different Valid Seller Listing Title",
            )
        )

    assert first.state is ControlJobState.PRODUCT_DRAFT_SYNCING
    assert store.list_review_decisions(job.job_id) == decisions_before
    assert store.list_events(job.job_id) == events_before
    assert store.list_work_requests(job.job_id) == work_before


_FOREIGN_UPLOAD_ID = "upload_phase66_foreign"
_UNKNOWN_UPLOAD_ID = "upload_phase66_unknown"
_FOREIGN_JOB_ID = "job_phase66_foreign"
_UNKNOWN_JOB_ID = "job_phase66_unknown"
_UNKNOWN_OWNER = "1" * 64

_TARGETED_ROUTE_KINDS = (
    ("GET /v1/uploads/{upload_id}", "upload"),
    ("POST /v1/uploads/{upload_id}/authorize", "upload"),
    ("POST /v1/uploads/{upload_id}/complete", "upload"),
    ("POST /v1/uploads/{upload_id}/cancel", "upload"),
    ("GET /v1/jobs/{job_id}", "review"),
    ("GET /v1/jobs/{job_id}/review", "review"),
    ("PUT /v1/jobs/{job_id}/review/listing", "command"),
    ("POST /v1/jobs/{job_id}/economics/refresh", "command"),
    ("POST /v1/jobs/{job_id}/approve", "command"),
    ("POST /v1/jobs/{job_id}/cancel", "command"),
    ("POST /v1/jobs/{job_id}/retry", "command"),
    ("GET /v1/jobs/{job_id}/artwork-preview", "preview"),
)


def _not_found() -> None:
    raise NotFoundError("private owner or storage detail")


class _OwnerClosedUploads:
    def __init__(self) -> None:
        self.owners = {_FOREIGN_UPLOAD_ID: cloud_api.OTHER_OWNER}
        self.writes: list[str] = []
        self.presigns: list[str] = []

    def _require_owner(self, owner_id: str, upload_id: str) -> None:
        if self.owners.get(upload_id) != owner_id:
            _not_found()

    def get_upload(self, *, owner_id: str, upload_id: str):  # type: ignore[no-untyped-def]
        self._require_owner(owner_id, upload_id)
        raise AssertionError("the owner boundary unexpectedly admitted an upload read")

    def create_upload(self, **values: object):  # type: ignore[no-untyped-def]
        self.writes.append("create")
        self.presigns.append("create")
        raise AssertionError(f"identity-bearing create reached the service: {sorted(values)}")

    def authorize_upload(self, *, owner_id: str, upload_id: str, idempotency_key: str):  # type: ignore[no-untyped-def]
        del idempotency_key
        self._require_owner(owner_id, upload_id)
        self.writes.append("authorize")
        self.presigns.append("authorize")
        raise AssertionError("the owner boundary unexpectedly authorized an upload")

    def complete_upload(self, *, owner_id: str, upload_id: str, idempotency_key: str):  # type: ignore[no-untyped-def]
        del idempotency_key
        self._require_owner(owner_id, upload_id)
        self.writes.append("complete")
        raise AssertionError("the owner boundary unexpectedly completed an upload")

    def cancel_upload(self, *, owner_id: str, upload_id: str, idempotency_key: str):  # type: ignore[no-untyped-def]
        del idempotency_key
        self._require_owner(owner_id, upload_id)
        self.writes.append("cancel")
        raise AssertionError("the owner boundary unexpectedly cancelled an upload")


class _OwnerClosedReviews:
    def __init__(self) -> None:
        self.owners = {_FOREIGN_JOB_ID: cloud_api.OTHER_OWNER}
        self.materializations: list[str] = []

    def get(self, *, owner_id: str, job_id: str):  # type: ignore[no-untyped-def]
        if self.owners.get(job_id) != owner_id:
            _not_found()
        self.materializations.append(job_id)
        raise AssertionError("the owner boundary unexpectedly materialized a review")


class _OwnerClosedPreviews:
    def __init__(self) -> None:
        self.owners = {_FOREIGN_JOB_ID: cloud_api.OTHER_OWNER}
        self.presigns: list[str] = []

    def authorize(self, *, owner_id: str, job_id: str):  # type: ignore[no-untyped-def]
        if self.owners.get(job_id) != owner_id:
            _not_found()
        self.presigns.append(job_id)
        raise AssertionError("the owner boundary unexpectedly presigned a preview")


class _OwnerClosedCommands:
    def __init__(self) -> None:
        self.owners = {_FOREIGN_JOB_ID: cloud_api.OTHER_OWNER}
        self.writes: list[str] = []

    def _reject(self, operation: str, command: Any):
        if self.owners.get(command.job_id) != command.owner_id:
            _not_found()
        self.writes.append(operation)
        raise AssertionError(f"the owner boundary unexpectedly admitted {operation}")

    def revise_listing(self, command: Any):  # type: ignore[no-untyped-def]
        return self._reject("revise", command)

    def refresh_economics(self, command: Any):  # type: ignore[no-untyped-def]
        return self._reject("refresh", command)

    def approve_review(self, command: Any):  # type: ignore[no-untyped-def]
        return self._reject("approve", command)

    def cancel_job(self, command: Any):  # type: ignore[no-untyped-def]
        return self._reject("cancel", command)

    def retry_job(self, command: Any):  # type: ignore[no-untyped-def]
        return self._reject("retry", command)


class _OwnerFilteredJobStore:
    def __init__(self, jobs: tuple[Any, ...]) -> None:
        self.jobs = jobs
        self.writes: list[str] = []

    def list_jobs_for_owner(
        self,
        owner_id: str,
        *,
        limit: int = 25,
        cursor: str | None = None,
    ) -> OwnerJobPage:
        del cursor
        owned = tuple(job for job in self.jobs if job.owner_id == owner_id)
        return OwnerJobPage(jobs=owned[:limit])


def _targeted_event(route: str, resource_id: str) -> dict[str, object]:
    body: object | None = None
    headers: dict[str, object] = {}
    if route.startswith("POST /v1/uploads/"):
        headers["Idempotency-Key"] = "phase66-upload-boundary"
    elif route == "PUT /v1/jobs/{job_id}/review/listing":
        body = {
            **cloud_api.review_authority_body(),
            "listing": {
                "title": "A seller title",
                "description": "A seller description",
                "tags": [f"tag {index}" for index in range(13)],
            },
        }
        headers.update(
            {
                "Content-Type": "application/json",
                "Idempotency-Key": "phase66-revise-boundary",
                "If-Match": f'"{cloud_api.REVIEW_ETAG}"',
            }
        )
    elif route in {
        "POST /v1/jobs/{job_id}/economics/refresh",
        "POST /v1/jobs/{job_id}/approve",
    }:
        body = cloud_api.review_authority_body()
        headers.update(
            {
                "Content-Type": "application/json",
                "Idempotency-Key": "phase66-review-boundary",
                "If-Match": f'"{cloud_api.REVIEW_ETAG}"',
            }
        )
    elif route in {
        "POST /v1/jobs/{job_id}/cancel",
        "POST /v1/jobs/{job_id}/retry",
    }:
        body = {"expected_record_version": 7}
        headers.update(
            {
                "Content-Type": "application/json",
                "Idempotency-Key": "phase66-record-boundary",
            }
        )

    event = cloud_api.api_event(route, body=body, headers=headers)
    if "{upload_id}" in route:
        event["pathParameters"] = {"upload_id": resource_id}
        event["rawPath"] = str(event["rawPath"]).replace(cloud_api.UPLOAD_ID, resource_id)
    else:
        event["pathParameters"] = {"job_id": resource_id}
        event["rawPath"] = str(event["rawPath"]).replace(cloud_api.JOB_ID, resource_id)
    return event


def _targeted_adapter(kind: str):  # type: ignore[no-untyped-def]
    uploads = _OwnerClosedUploads()
    reviews = _OwnerClosedReviews()
    previews = _OwnerClosedPreviews()
    commands = _OwnerClosedCommands()
    store = _OwnerFilteredJobStore(())
    if kind == "upload":
        adapter = UploadApiAdapter(claims_policy=cloud_api.POLICY, uploads=uploads)
    elif kind in {"review", "preview"}:
        adapter = ReviewQueryApiAdapter(
            claims_policy=cloud_api.POLICY,
            store=store,
            reviews=reviews,
            previews=previews,
        )
    else:
        adapter = SellerCommandApiAdapter(
            claims_policy=cloud_api.POLICY,
            commands=commands,
        )
    return adapter, uploads, reviews, previews, commands, store


@pytest.mark.parametrize(("route", "kind"), _TARGETED_ROUTE_KINDS)
def test_targeted_cloud_routes_make_foreign_and_unknown_resources_indistinguishable(
    route: str,
    kind: str,
) -> None:
    adapter, uploads, reviews, previews, commands, store = _targeted_adapter(kind)
    if "{upload_id}" in route:
        foreign_id, unknown_id = _FOREIGN_UPLOAD_ID, _UNKNOWN_UPLOAD_ID
    else:
        foreign_id, unknown_id = _FOREIGN_JOB_ID, _UNKNOWN_JOB_ID

    foreign = adapter.handle(_targeted_event(route, foreign_id))
    unknown = adapter.handle(_targeted_event(route, unknown_id))

    assert foreign == unknown
    assert foreign["statusCode"] == 404
    assert cloud_api.response_body(foreign)["error"]["code"] == "NOT_FOUND"
    assert _FOREIGN_UPLOAD_ID not in str(foreign)
    assert _FOREIGN_JOB_ID not in str(foreign)
    assert cloud_api.OTHER_OWNER not in str(foreign)
    assert uploads.writes == []
    assert uploads.presigns == []
    assert reviews.materializations == []
    assert previews.presigns == []
    assert commands.writes == []
    assert store.writes == []


def test_upload_collection_rejects_any_caller_owner_before_write_or_presign() -> None:
    uploads = _OwnerClosedUploads()
    adapter = UploadApiAdapter(claims_policy=cloud_api.POLICY, uploads=uploads)

    def request(owner_id: str) -> dict[str, object]:
        return adapter.handle(
            cloud_api.api_event(
                "POST /v1/uploads",
                body={**cloud_api.create_upload_body(), "owner_id": owner_id},
                headers={
                    "Content-Type": "application/json",
                    "Idempotency-Key": "phase66-create-boundary",
                },
            )
        )

    foreign = request(cloud_api.OTHER_OWNER)
    unknown = request(_UNKNOWN_OWNER)

    assert foreign == unknown
    assert foreign["statusCode"] == 422
    assert uploads.writes == []
    assert uploads.presigns == []


def test_job_collection_hides_foreign_jobs_exactly_like_an_empty_owner_index() -> None:
    foreign_job = cloud_api.job_record(
        owner_id=cloud_api.OTHER_OWNER,
        job_id=_FOREIGN_JOB_ID,
    )
    foreign_store = _OwnerFilteredJobStore((foreign_job,))
    empty_store = _OwnerFilteredJobStore(())

    def request(store: _OwnerFilteredJobStore) -> dict[str, object]:
        return ReviewQueryApiAdapter(
            claims_policy=cloud_api.POLICY,
            store=store,
            reviews=_OwnerClosedReviews(),
            previews=_OwnerClosedPreviews(),
        ).handle(cloud_api.api_event("GET /v1/jobs"))

    foreign = request(foreign_store)
    unknown = request(empty_store)

    assert foreign == unknown
    assert foreign["statusCode"] == 200
    assert cloud_api.response_body(foreign) == {"jobs": [], "next_cursor": None}
    assert _FOREIGN_JOB_ID not in str(foreign)
    assert cloud_api.OTHER_OWNER not in str(foreign)
    assert foreign_store.writes == empty_store.writes == []


def test_phase66_owner_boundary_matrix_covers_every_protected_cloud_route() -> None:
    covered = {route for route, _kind in _TARGETED_ROUTE_KINDS} | {
        "POST /v1/uploads",
        "GET /v1/jobs",
    }

    assert covered == PROTECTED_ROUTE_KEYS
