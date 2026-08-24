"""Capability-free Phase 7.4 application composition seams.

This module deliberately contains no SDK, HTTP transport, credential resolver, workflow starter,
or provider boundary.  It provides only three application-owned joins needed before a future
runtime may be enabled:

* an exact-false activation gate;
* an owner-first adapter from durable execution authority to the seller-safe projection graph;
* a read-only guard that proves current Phase 6 approval/profile authority still matches the
  immutable publication snapshot before an outer coordinator may attempt provider work.

The current Phase 7 contract remains in ``offline_implementation``.  Nothing here makes a seller
route or a provider call reachable.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from mr_lister.control.errors import NotFoundError
from mr_lister.control.fingerprints import canonical_fingerprint as control_fingerprint
from mr_lister.control.models import ControlJobRecord
from mr_lister.publication.contract import (
    PublicationActivationPhaseName,
    PublicationState,
    phase7_publication_contract,
)
from mr_lister.publication.errors import PublicationNotFoundError
from mr_lister.publication.execution_models import PublicationExecutionAuthority
from mr_lister.publication.models import PublicationAggregate
from mr_lister.publication.profile_eligibility import (
    PublicationProfileEligibilityAuthority,
    require_exact_publication_profile_eligibility,
)
from mr_lister.publication.projection import PublicationProjectionAuthority
from mr_lister.publication.store import (
    PublicationRequestAuthority,
    validate_publication_request_authority,
)
from mr_lister.review_profile import ExactReviewProductProfile


class Phase7RuntimeDisabledError(RuntimeError):
    """Value-free refusal emitted by every Phase 7.4 runtime entrypoint."""


class PublicationPreCallAuthorityError(RuntimeError):
    """Current durable authority no longer matches the immutable publication snapshot."""


class PublicationRuntimeActivation(BaseModel):
    """The only activation value accepted by the Phase 7.4 application slice."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    request_enabled: Literal[False] = False
    publication_enabled: Literal[False] = False
    query_enabled: Literal[False] = False
    scaffold_only: Literal[True] = True

    @field_validator(
        "request_enabled",
        "publication_enabled",
        "query_enabled",
        mode="before",
    )
    @classmethod
    def disabled_flags_are_exact_false(cls, value: object) -> object:
        if value is not False:
            raise ValueError("Phase 7.4 activation flags must be exact")
        return value

    @field_validator("scaffold_only", mode="before")
    @classmethod
    def scaffold_flag_is_exact_true(cls, value: object) -> object:
        if value is not True:
            raise ValueError("Phase 7.4 scaffold flag must be exact")
        return value

    @model_validator(mode="after")
    def contract_is_still_offline_and_disabled(self) -> PublicationRuntimeActivation:
        contract = phase7_publication_contract()
        if (
            contract.publication_enabled is not False
            or contract.current_activation_phase
            is not PublicationActivationPhaseName.OFFLINE_IMPLEMENTATION
        ):
            raise ValueError("Phase 7.4 requires the frozen disabled offline contract")
        return self

    def deny_runtime(self) -> None:
        """Fail before reading an invocation identity or constructing a capability."""

        raise Phase7RuntimeDisabledError("Phase 7 publication runtime is disabled")


class OwnerFirstJobStore(Protocol):
    def get_job_for_owner(self, owner_id: str, job_id: str) -> ControlJobRecord: ...


class ProjectionExecutionStore(Protocol):
    def load_execution_authority(
        self,
        owner_id: str,
        aggregate_id: str,
    ) -> PublicationExecutionAuthority: ...


class PublicationExecutionSourceStore(ProjectionExecutionStore, Protocol):
    def load_source_authority(
        self,
        owner_id: str,
        aggregate_id: str,
    ) -> PublicationRequestAuthority: ...


class PublicationProfileAuthority(Protocol):
    def get_exact(
        self,
        *,
        profile_id: str,
        profile_version: int,
    ) -> ExactReviewProductProfile: ...


@dataclass(frozen=True, slots=True)
class DynamoPublicationProjectionStore:
    """Join owner-scoped job reads to the exact durable publication execution graph.

    Despite the historical name, this adapter owns no SDK client.  Its injected stores perform the
    reads, which keeps the application join deterministic and lets future IAM give the read-only
    function only ``GetItem`` and bounded strongly consistent ``Query`` authority.
    """

    jobs: OwnerFirstJobStore
    execution: ProjectionExecutionStore

    def get_job_for_owner(self, owner_id: str, job_id: str) -> ControlJobRecord:
        try:
            job = self.jobs.get_job_for_owner(owner_id, job_id)
            parsed = ControlJobRecord.model_validate(job.model_dump(mode="python"))
        except NotFoundError:
            raise NotFoundError from None
        except Exception:
            raise ValueError("Publication job projection authority is invalid") from None
        if parsed.owner_id != owner_id or parsed.job_id != job_id:
            raise NotFoundError from None
        return parsed

    def get_publication_authority(
        self,
        owner_id: str,
        aggregate_id: str,
    ) -> PublicationProjectionAuthority:
        try:
            execution = self.execution.load_execution_authority(owner_id, aggregate_id)
            execution = PublicationExecutionAuthority.model_validate(
                execution.model_dump(mode="python")
            )
            job = self.get_job_for_owner(owner_id, execution.snapshot.job_id)
            if (
                execution.aggregate.aggregate_id != aggregate_id
                or job.publication_aggregate_id != aggregate_id
            ):
                raise ValueError

            pristine = isinstance(execution.expected_aggregate, PublicationAggregate)
            authority = PublicationProjectionAuthority(
                job=job,
                snapshot=execution.snapshot,
                aggregate=(execution.expected_aggregate if pristine else execution.aggregate),
                attempt=execution.expected_attempt if pristine else execution.attempt,
                permit=execution.expected_permit if pristine else execution.permit,
                work=execution.expected_work if pristine else execution.work,
                mutation_claim=execution.mutation_claim,
                post_observation=execution.post_observation,
                observation=execution.last_product_observation,
                result=execution.result,
                notification=execution.notification,
                report=execution.report,
            )
            return PublicationProjectionAuthority.model_validate(
                authority.model_dump(mode="python")
            )
        except PublicationNotFoundError:
            raise NotFoundError from None
        except NotFoundError:
            raise
        except Exception:
            raise ValueError("Publication projection authority is invalid") from None


class DurablePublicationPreCallGuard:
    """Re-read and compare every approval/snapshot join before outer provider coordination.

    The guard is capability-free and intentionally not composed by Phase 7.4.  A future worker may
    call it immediately before the existing coordinator.  The coordinator's own exact command CAS
    remains authoritative for the transition; this guard prevents a stale approval or shop/profile
    binding from reaching even a read-only provider boundary.
    """

    __slots__ = ("_eligibility", "_profiles", "_release_fingerprint", "_store")

    def __init__(
        self,
        *,
        store: PublicationExecutionSourceStore,
        profiles: PublicationProfileAuthority,
        eligibility: PublicationProfileEligibilityAuthority,
        release_manifest_fingerprint: str,
    ) -> None:
        if (
            not isinstance(release_manifest_fingerprint, str)
            or len(release_manifest_fingerprint) != 64
            or any(
                character not in "0123456789abcdef" for character in release_manifest_fingerprint
            )
            or release_manifest_fingerprint == "0" * 64
        ):
            raise ValueError("A nonzero release manifest fingerprint is required")
        self._store = store
        self._profiles = profiles
        self._eligibility = eligibility
        self._release_fingerprint = release_manifest_fingerprint

    def require_current(
        self,
        *,
        owner_id: str,
        aggregate_id: str,
    ) -> PublicationExecutionAuthority:
        try:
            execution = self._store.load_execution_authority(owner_id, aggregate_id)
            execution = PublicationExecutionAuthority.model_validate(
                execution.model_dump(mode="python")
            )
            source = self._store.load_source_authority(owner_id, aggregate_id)
            validate_publication_request_authority(source)
            self._require_snapshot_match(
                execution,
                source,
                owner_id=owner_id,
                aggregate_id=aggregate_id,
            )
            exact = self._profiles.get_exact(
                profile_id=execution.snapshot.profile_id,
                profile_version=execution.snapshot.profile_version,
            )
            if not isinstance(exact, ExactReviewProductProfile):
                raise ValueError
            if (
                exact.profile.profile_id != execution.snapshot.profile_id
                or exact.profile.profile_version != execution.snapshot.profile_version
                or exact.fingerprint != control_fingerprint(exact.profile)
                or exact.fingerprint != execution.snapshot.profile_fingerprint
            ):
                raise ValueError
            eligibility = self._eligibility.get_exact(
                profile_id=exact.profile.profile_id,
                profile_version=exact.profile.profile_version,
                profile_fingerprint=exact.fingerprint,
                expected_sales_channel=execution.snapshot.expected_sales_channel,
                release_manifest_fingerprint=self._release_fingerprint,
                phase6_profile_publish_enabled=exact.profile.publish_enabled,
            )
            require_exact_publication_profile_eligibility(
                eligibility.model_dump(mode="python"),
                profile_id=exact.profile.profile_id,
                profile_version=exact.profile.profile_version,
                profile_fingerprint=exact.fingerprint,
                expected_sales_channel=execution.snapshot.expected_sales_channel,
                release_manifest_fingerprint=self._release_fingerprint,
                phase6_profile_publish_enabled=exact.profile.publish_enabled,
            )
            return execution
        except Exception:
            raise PublicationPreCallAuthorityError(
                "Publication pre-call authority is invalid"
            ) from None

    def _require_snapshot_match(
        self,
        execution: PublicationExecutionAuthority,
        source: PublicationRequestAuthority,
        *,
        owner_id: str,
        aggregate_id: str,
    ) -> None:
        snapshot = execution.snapshot
        request_link = execution.request_job_link
        job = source.current_job
        review = source.review
        decision = source.approval_decision
        sync = source.product_sync
        pricing = source.pricing_snapshot
        evidence = source.pricing_evidence
        if (
            execution.aggregate.state
            in {
                PublicationState.PUBLISHED,
                PublicationState.PUBLICATION_FAILED,
                PublicationState.PUBLICATION_OUTCOME_UNKNOWN,
            }
            or snapshot.owner_id != owner_id
            or execution.aggregate.aggregate_id != aggregate_id
            or job.owner_id != snapshot.owner_id
            or job.job_id != snapshot.job_id
            or job.publication_aggregate_id != execution.aggregate.aggregate_id
            or request_link.expected_record_version != snapshot.expected_record_version
            or request_link.result_record_version != snapshot.expected_record_version + 1
            or execution.phase6_record_version != request_link.result_record_version
            or job.record_version != execution.phase6_record_version
            or job.event_sequence != execution.phase6_event_sequence
            or job.publication_terminal_state is not None
            or job.approval_decision_id != snapshot.approval_decision_id
            or decision.decision_id != snapshot.approval_decision_id
            or job.approval_fingerprint != snapshot.approval_fingerprint
            or decision.approval_fingerprint != snapshot.approval_fingerprint
            or review.review_version != snapshot.review_version
            or review.fingerprint != snapshot.review_fingerprint
            or sync.sync_id != snapshot.product_sync_id
            or sync.fingerprint != snapshot.product_sync_fingerprint
            or sync.printify_shop_id != snapshot.printify_shop_id
            or sync.product_id != snapshot.printify_product_id
            or sync.image_id != snapshot.printify_image_id
            or sync.payload_fingerprint != snapshot.product_payload_fingerprint
            or pricing.snapshot_id != snapshot.pricing_snapshot_id
            or pricing.fingerprint != snapshot.pricing_snapshot_fingerprint
            or evidence.fingerprint != snapshot.pricing_evidence_fingerprint
            or source.source.product_profile_id != snapshot.profile_id
            or source.source.product_profile_version != snapshot.profile_version
            or source.source.product_profile_fingerprint != snapshot.profile_fingerprint
            or snapshot.release_manifest_fingerprint != self._release_fingerprint
        ):
            raise ValueError


__all__ = [
    "DurablePublicationPreCallGuard",
    "DynamoPublicationProjectionStore",
    "Phase7RuntimeDisabledError",
    "PublicationPreCallAuthorityError",
    "PublicationRuntimeActivation",
]
