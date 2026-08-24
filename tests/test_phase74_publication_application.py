"""Phase 7.4 capability-free activation, guard, and projection joins."""

from __future__ import annotations

from dataclasses import dataclass, replace

import pytest
from pydantic import ValidationError

from mr_lister.control.errors import NotFoundError
from mr_lister.control.models import ControlJobRecord
from mr_lister.publication.application import (
    DurablePublicationPreCallGuard,
    DynamoPublicationProjectionStore,
    Phase7RuntimeDisabledError,
    PublicationPreCallAuthorityError,
    PublicationRuntimeActivation,
)
from mr_lister.publication.projection import SellerPublicationProjectionService
from mr_lister.publication.projection_models import SellerPublicationStage
from tests.test_phase71_publication_service import ProfileAuthority, _authority
from tests.test_phase71_publication_store import OWNER_ID
from tests.test_phase72_publication_execution import Harness


@dataclass
class OwnerFirstJobs:
    jobs: dict[str, ControlJobRecord]
    reads: int = 0

    def get_job_for_owner(self, owner_id: str, job_id: str) -> ControlJobRecord:
        self.reads += 1
        job = self.jobs.get(job_id)
        if job is None or job.owner_id != owner_id:
            raise NotFoundError
        return job


class RecordingExecutionStore:
    def __init__(self, delegate: object) -> None:
        self.delegate = delegate
        self.reads = 0

    def load_execution_authority(self, owner_id: str, aggregate_id: str):  # type: ignore[no-untyped-def]
        self.reads += 1
        return self.delegate.load_execution_authority(owner_id, aggregate_id)  # type: ignore[attr-defined]


class DriftingSourceStore:
    def __init__(self, delegate: object) -> None:
        self.delegate = delegate

    def load_execution_authority(self, owner_id: str, aggregate_id: str):  # type: ignore[no-untyped-def]
        return self.delegate.load_execution_authority(owner_id, aggregate_id)  # type: ignore[attr-defined]

    def load_source_authority(self, owner_id: str, aggregate_id: str):  # type: ignore[no-untyped-def]
        source = self.delegate.load_source_authority(owner_id, aggregate_id)  # type: ignore[attr-defined]
        return replace(
            source,
            current_job=source.current_job.model_copy(
                update={"record_version": source.current_job.record_version + 1}
            ),
        )


class CoDriftingVersionStore:
    def __init__(self, delegate: object) -> None:
        self.delegate = delegate

    def load_execution_authority(self, owner_id: str, aggregate_id: str):  # type: ignore[no-untyped-def]
        execution = self.delegate.load_execution_authority(owner_id, aggregate_id)  # type: ignore[attr-defined]
        return execution.model_copy(
            update={"phase6_record_version": execution.phase6_record_version + 1}
        )

    def load_source_authority(self, owner_id: str, aggregate_id: str):  # type: ignore[no-untyped-def]
        source = self.delegate.load_source_authority(owner_id, aggregate_id)  # type: ignore[attr-defined]
        return replace(
            source,
            current_job=source.current_job.model_copy(
                update={"record_version": source.current_job.record_version + 1}
            ),
        )


class ForgedEligibilityAuthority:
    def __init__(self, delegate: object) -> None:
        self.delegate = delegate

    def get_exact(self, **values: object):  # type: ignore[no-untyped-def]
        exact = self.delegate.get_exact(**values)  # type: ignore[attr-defined]
        return exact.model_copy(update={"provider_mutation_enabled": True})


def _job_store(harness: Harness) -> OwnerFirstJobs:
    return OwnerFirstJobs(dict(harness.store.jobs))


def _guard(
    harness: Harness,
    *,
    store: object | None = None,
    eligibility: object | None = None,
) -> DurablePublicationPreCallGuard:
    _, exact = _authority()
    return DurablePublicationPreCallGuard(
        store=store or harness.store,  # type: ignore[arg-type]
        profiles=ProfileAuthority(exact),
        eligibility=eligibility or harness.profile_eligibility,  # type: ignore[arg-type]
        release_manifest_fingerprint="b" * 64,
    )


def test_activation_is_strictly_scaffolded_and_all_runtime_flags_are_false() -> None:
    activation = PublicationRuntimeActivation(
        request_enabled=False,
        publication_enabled=False,
        query_enabled=False,
        scaffold_only=True,
    )

    with pytest.raises(Phase7RuntimeDisabledError) as captured:
        activation.deny_runtime()
    assert str(captured.value) == "Phase 7 publication runtime is disabled"

    for field, value in (
        ("request_enabled", True),
        ("publication_enabled", True),
        ("query_enabled", True),
        ("scaffold_only", False),
        ("query_enabled", 0),
    ):
        with pytest.raises(ValidationError):
            PublicationRuntimeActivation.model_validate(
                {
                    "request_enabled": False,
                    "publication_enabled": False,
                    "query_enabled": False,
                    "scaffold_only": True,
                    field: value,
                }
            )


def test_projection_adapter_uses_owner_first_job_read_before_execution_graph() -> None:
    harness = Harness()
    jobs = _job_store(harness)
    execution = RecordingExecutionStore(harness.store)
    store = DynamoPublicationProjectionStore(jobs=jobs, execution=execution)

    with pytest.raises(NotFoundError):
        SellerPublicationProjectionService(store).get(
            owner_id="b" * 64,
            job_id=harness.authority.snapshot.job_id,
        )

    assert jobs.reads == 1
    assert execution.reads == 0


def test_projection_adapter_joins_pristine_and_evolved_execution_rows_exactly() -> None:
    harness = Harness()
    jobs = _job_store(harness)
    store = DynamoPublicationProjectionStore(jobs=jobs, execution=harness.store)
    projections = SellerPublicationProjectionService(store)

    queued = projections.get(
        owner_id=OWNER_ID,
        job_id=harness.authority.snapshot.job_id,
    )
    assert queued.stage is SellerPublicationStage.QUEUED
    assert queued.publication_enabled is False
    assert queued.request_enabled is False

    harness.dispatch_and_reconstruct()
    preflight = projections.get(
        owner_id=OWNER_ID,
        job_id=harness.authority.snapshot.job_id,
    )
    assert preflight.stage is SellerPublicationStage.PREFLIGHT
    assert preflight.aggregate_record_version > queued.aggregate_record_version
    assert preflight.etag != queued.etag


def test_pre_call_guard_accepts_exact_current_release_eligibility_and_shop_binding() -> None:
    harness = Harness()

    guarded = _guard(harness).require_current(
        owner_id=OWNER_ID,
        aggregate_id=harness.aggregate_id,
    )

    assert guarded == harness.authority
    source = harness.store.load_source_authority(OWNER_ID, harness.aggregate_id)
    assert guarded.snapshot.printify_shop_id == source.product_sync.printify_shop_id


def test_pre_call_guard_rejects_stale_phase6_authority_before_any_outer_delegate() -> None:
    harness = Harness()
    guard = _guard(harness, store=DriftingSourceStore(harness.store))

    with pytest.raises(PublicationPreCallAuthorityError) as captured:
        guard.require_current(owner_id=OWNER_ID, aggregate_id=harness.aggregate_id)

    assert str(captured.value) == "Publication pre-call authority is invalid"
    assert captured.value.__cause__ is None


def test_pre_call_guard_rejects_co_drifted_live_versions_against_immutable_link() -> None:
    harness = Harness()
    guard = _guard(harness, store=CoDriftingVersionStore(harness.store))

    with pytest.raises(PublicationPreCallAuthorityError) as captured:
        guard.require_current(owner_id=OWNER_ID, aggregate_id=harness.aggregate_id)

    assert str(captured.value) == "Publication pre-call authority is invalid"
    assert captured.value.__cause__ is None


def test_pre_call_guard_deep_reparses_returned_eligibility_and_rejects_model_copy() -> None:
    harness = Harness()
    forged = ForgedEligibilityAuthority(harness.profile_eligibility)

    with pytest.raises(PublicationPreCallAuthorityError) as captured:
        _guard(harness, eligibility=forged).require_current(
            owner_id=OWNER_ID,
            aggregate_id=harness.aggregate_id,
        )

    assert "provider_mutation" not in str(captured.value)
    assert captured.value.__cause__ is None
