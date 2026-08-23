"""Atomic persistence seam and in-memory oracle for Phase 7.2 execution."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from threading import Lock, RLock
from types import MappingProxyType
from typing import Protocol

from pydantic import model_validator

from mr_lister.control.fingerprints import publication_terminal_summary_fingerprint
from mr_lister.control.models import ControlJobRecord, ControlJobState
from mr_lister.publication.errors import (
    PublicationConflictError,
    PublicationErrorCode,
    PublicationIdempotencyConflictError,
    PublicationNotFoundError,
)
from mr_lister.publication.execution_fingerprints import execution_record_fingerprint
from mr_lister.publication.execution_models import (
    ExecutionPublicationAggregate,
    ExecutionPublicationAttempt,
    ExecutionPublicationPermit,
    ExecutionPublicationWork,
    PublicationAggregateTombstone,
    PublicationCallClaim,
    PublicationExecutionAuthority,
    PublicationExecutionEvent,
    PublicationExecutionOperation,
    PublicationExecutionReceipt,
    PublicationModel,
    PublicationMutationClaim,
    PublicationNotification,
    PublicationPostObservation,
    PublicationPreflightProof,
    PublicationProductObservation,
    PublicationProviderAuditBinding,
    PublicationProviderAuditDecision,
    PublicationProviderAuditRecord,
    PublicationProviderAuthority,
    PublicationResult,
    PublicationTerminalJobLink,
    PublicationTerminalReport,
)
from mr_lister.publication.models import (
    PublicationAggregate,
    PublicationAttempt,
    PublicationDomainEvent,
    PublicationJobLink,
    PublicationPermit,
    PublicationSnapshot,
    PublicationWorkRequest,
)
from mr_lister.publication.store import (
    PublicationRequestAuthority,
    PublicationRequestTransaction,
    validate_publication_request_authority,
)


class PublicationExecutionCommit(PublicationModel):
    """Complete all-or-nothing execution transition.

    The expected authority retains the exact raw Phase 7.1 rows for the mandatory dispatch
    normalization transaction.  Every later transition expects evolved execution rows.
    """

    expected: PublicationExecutionAuthority
    updated_aggregate: ExecutionPublicationAggregate
    updated_attempt: ExecutionPublicationAttempt
    updated_permit: ExecutionPublicationPermit
    updated_work: ExecutionPublicationWork
    new_call_claim: PublicationCallClaim | None = None
    new_provider_authority: PublicationProviderAuthority | None = None
    new_preflight_proof: PublicationPreflightProof | None = None
    new_mutation_claim: PublicationMutationClaim | None = None
    new_post_observation: PublicationPostObservation | None = None
    new_product_observation: PublicationProductObservation | None = None
    new_result: PublicationResult | None = None
    new_notification: PublicationNotification | None = None
    new_report: PublicationTerminalReport | None = None
    new_tombstone: PublicationAggregateTombstone | None = None
    terminal_job_update: PublicationTerminalJobUpdate | None = None
    event: PublicationExecutionEvent
    receipt: PublicationExecutionReceipt

    @model_validator(mode="after")
    def records_share_one_transition_identity(self) -> PublicationExecutionCommit:
        aggregate_id = self.expected.aggregate.aggregate_id
        owner_id = self.expected.snapshot.owner_id
        job_id = self.expected.snapshot.job_id
        if any(
            record.aggregate_id != aggregate_id
            for record in (
                self.updated_aggregate,
                self.updated_attempt,
                self.updated_permit,
                self.updated_work,
                self.event,
                self.receipt,
            )
        ):
            raise ValueError("Execution commit must bind one aggregate")
        if (
            self.updated_aggregate.owner_id != owner_id
            or self.updated_aggregate.job_id != job_id
            or self.updated_attempt.owner_id != owner_id
            or self.updated_attempt.job_id != job_id
            or self.updated_permit.owner_id != owner_id
            or self.updated_permit.job_id != job_id
            or self.updated_work.owner_id != owner_id
            or self.updated_work.job_id != job_id
            or self.event.owner_id != owner_id
            or self.event.job_id != job_id
            or self.receipt.owner_id != owner_id
            or self.receipt.job_id != job_id
        ):
            raise ValueError("Execution commit must bind one owner and job")
        if (
            self.event.operation_id != self.receipt.operation_id
            or self.receipt.operation is not self._operation()
        ):
            raise ValueError("Execution event and receipt must bind the exact operation")
        return self

    def _operation(self) -> PublicationExecutionOperation:
        return self.receipt.operation


class PublicationTerminalJobUpdate(PublicationModel):
    """Exact current/updated Phase 6 job rows wrapped around the pure summary link."""

    expected_job: ControlJobRecord
    updated_job: ControlJobRecord
    link: PublicationTerminalJobLink

    @model_validator(mode="after")
    def update_changes_only_terminal_publication_summary(self) -> PublicationTerminalJobUpdate:
        current = self.expected_job
        link = self.link
        if (
            current.owner_id != link.owner_id
            or current.job_id != link.job_id
            or current.state is not ControlJobState.APPROVED
            or current.publication_aggregate_id != link.aggregate_id
            or current.record_version != link.expected_record_version
            or current.event_sequence != link.expected_event_sequence
            or current.publication_terminal_state is not None
        ):
            raise ValueError("Terminal publication link differs from current Phase 6 authority")
        expected_updated = ControlJobRecord.model_validate(
            {
                **current.model_dump(mode="python"),
                "record_version": link.result_record_version,
                "publication_terminal_state": link.terminal_state.value,
                "publication_terminal_at": link.terminal_at,
                "publication_source_release_eligible_at": link.source_release_eligible_at,
                "publication_operational_expires_at": link.operational_expires_at,
                "publication_report_id": link.report_id,
                "publication_result_id": link.result_id,
                "publication_terminal_summary_fingerprint": link.terminal_summary_fingerprint,
                "updated_at": link.terminal_at,
            }
        )
        if self.updated_job != expected_updated:
            raise ValueError("Terminal publication job update changed Phase 6 authority")
        return self


PublicationExecutionCommit.model_rebuild()


class PublicationProviderAuditCommit(PublicationModel):
    """Exact claim-conditioned append of one sanitized boundary decision."""

    expected_aggregate: ExecutionPublicationAggregate
    updated_aggregate: ExecutionPublicationAggregate
    binding: PublicationProviderAuditBinding

    @model_validator(mode="after")
    def binding_is_allowed(self) -> PublicationProviderAuditCommit:
        if self.binding.audit_record.decision is not PublicationProviderAuditDecision.ALLOWED:
            raise ValueError("Aggregate audit commit accepts only allowed decisions")
        before = self.expected_aggregate
        after = self.updated_aggregate
        if (
            self.binding.aggregate_id != before.aggregate_id
            or after.provider_audit_record_version != before.provider_audit_record_version + 1
        ):
            raise ValueError("Provider audit must increment the exact root watermark once")
        expected = ExecutionPublicationAggregate.model_validate(
            {
                **before.model_dump(
                    mode="python",
                    exclude={"contract_version", "fingerprint"},
                ),
                "provider_audit_record_version": after.provider_audit_record_version,
                "fingerprint": after.fingerprint,
            }
        )
        if expected != after:
            raise ValueError("Provider audit may change only its root watermark")
        return self


def build_provider_audit_commit(
    authority: PublicationExecutionAuthority,
    call_claim: PublicationCallClaim,
    audit_record: PublicationProviderAuditRecord,
) -> PublicationProviderAuditCommit:
    """Build the exact root-watermark CAS used by a future store-backed audit sink."""

    claim = next(
        (
            current
            for current in authority.call_claims
            if current.authorization_id == call_claim.authorization_id
        ),
        None,
    )
    if claim != call_claim or authority.aggregate.terminal_at is not None:
        raise PublicationConflictError(
            PublicationErrorCode.INVALID_AUTHORITY,
            "Provider audit requires the exact current nonterminal durable call claim",
        )
    binding_values = {
        "aggregate_id": authority.aggregate.aggregate_id,
        "call_claim_id": claim.authorization_id,
        "call_claim_fingerprint": claim.fingerprint,
        "durable_call_sequence": claim.resulting_attempt_record_version,
        "audit_record": audit_record,
    }
    binding = PublicationProviderAuditBinding(
        **binding_values,
        fingerprint=execution_record_fingerprint("provider_audit_binding", binding_values),
    )
    aggregate_values = {
        **authority.aggregate.model_dump(
            mode="python",
            exclude={"contract_version", "fingerprint"},
        ),
        "provider_audit_record_version": (authority.aggregate.provider_audit_record_version + 1),
    }
    updated_aggregate = ExecutionPublicationAggregate(
        **aggregate_values,
        fingerprint=execution_record_fingerprint("execution_aggregate", aggregate_values),
    )
    return PublicationProviderAuditCommit(
        expected_aggregate=authority.aggregate,
        updated_aggregate=updated_aggregate,
        binding=binding,
    )


_FRESH_GRANT_SEAL = object()


class FreshPublicationCallGrant:
    """Single-use, nonserializable wire authority minted only after a fresh winning CAS."""

    __slots__ = ("_claim_fingerprint", "_lock", "_used")

    def __init__(self, claim_fingerprint: str, *, _seal: object) -> None:
        if _seal is not _FRESH_GRANT_SEAL:
            raise TypeError("Fresh publication grants can only be minted by the execution store")
        self._claim_fingerprint = claim_fingerprint
        self._used = False
        self._lock = Lock()

    @classmethod
    def _mint(cls, claim: PublicationCallClaim) -> FreshPublicationCallGrant:
        return cls(claim.fingerprint, _seal=_FRESH_GRANT_SEAL)

    def consume_once(self, claim: PublicationCallClaim) -> None:
        with self._lock:
            if self._used:
                raise PublicationConflictError(
                    PublicationErrorCode.CONCURRENT_WRITE,
                    "Fresh provider call authority was already consumed",
                )
            if claim.fingerprint != self._claim_fingerprint:
                raise PublicationConflictError(
                    PublicationErrorCode.INVALID_AUTHORITY,
                    "Fresh provider call authority does not match the durable claim",
                )
            self._used = True

    def __copy__(self) -> FreshPublicationCallGrant:
        raise TypeError("Fresh publication grants cannot be copied")

    def __deepcopy__(self, memo: object) -> FreshPublicationCallGrant:
        raise TypeError("Fresh publication grants cannot be copied")

    def __reduce__(self) -> object:
        raise TypeError("Fresh publication grants cannot be serialized")


class FreshPublicationMutationGrant(FreshPublicationCallGrant):
    """Fresh-only one-shot POST authority; durable records alone never authorize mutation."""

    __slots__ = ("_mutation_claim_fingerprint",)

    def __init__(
        self,
        claim_fingerprint: str,
        mutation_claim_fingerprint: str,
        *,
        _seal: object,
    ) -> None:
        super().__init__(claim_fingerprint, _seal=_seal)
        self._mutation_claim_fingerprint = mutation_claim_fingerprint

    @classmethod
    def _mint(
        cls,
        claim: PublicationCallClaim,
        mutation_claim: PublicationMutationClaim,
    ) -> FreshPublicationMutationGrant:
        return cls(
            claim.fingerprint,
            mutation_claim.fingerprint,
            _seal=_FRESH_GRANT_SEAL,
        )

    def consume_once(
        self,
        claim: PublicationCallClaim,
        mutation_claim: PublicationMutationClaim,
    ) -> None:
        if mutation_claim.fingerprint != self._mutation_claim_fingerprint:
            raise PublicationConflictError(
                PublicationErrorCode.INVALID_AUTHORITY,
                "Fresh mutation authority does not match the durable mutation claim",
            )
        super().consume_once(claim)


@dataclass(frozen=True, slots=True)
class PublicationExecutionCommitResult:
    receipt: PublicationExecutionReceipt
    fresh_call_grant: FreshPublicationCallGrant | FreshPublicationMutationGrant | None = None


class PublicationExecutionStore(Protocol):
    def resolve_execution_receipt(
        self,
        owner_id: str,
        aggregate_id: str,
        operation_id: str,
    ) -> PublicationExecutionReceipt | None: ...

    def load_execution_authority(
        self,
        owner_id: str,
        aggregate_id: str,
    ) -> PublicationExecutionAuthority: ...

    def load_source_authority(
        self,
        owner_id: str,
        aggregate_id: str,
    ) -> PublicationRequestAuthority: ...

    def load_linked_job(
        self,
        owner_id: str,
        aggregate_id: str,
    ) -> ControlJobRecord: ...

    def commit_execution(
        self,
        commit: PublicationExecutionCommit,
    ) -> PublicationExecutionCommitResult: ...

    def commit_provider_audit(
        self,
        commit: PublicationProviderAuditCommit,
    ) -> PublicationProviderAuditBinding: ...


class InMemoryPublicationExecutionStore:
    """Thread-safe exact-CAS oracle; it performs no provider or Phase 6 mutation."""

    def __init__(self, requests: Iterable[PublicationRequestTransaction] = ()) -> None:
        self._lock = RLock()
        self._snapshots: dict[str, PublicationSnapshot] = {}
        self._jobs: dict[str, ControlJobRecord] = {}
        self._request_job_links: dict[str, PublicationJobLink] = {}
        self._source_authorities: dict[str, PublicationRequestAuthority] = {}
        self._aggregates: dict[str, PublicationAggregate | ExecutionPublicationAggregate] = {}
        self._attempts: dict[str, PublicationAttempt | ExecutionPublicationAttempt] = {}
        self._permits: dict[str, PublicationPermit | ExecutionPublicationPermit] = {}
        self._work: dict[str, PublicationWorkRequest | ExecutionPublicationWork] = {}
        self._initial_events: dict[str, PublicationDomainEvent] = {}
        self._events: dict[tuple[str, int], PublicationExecutionEvent] = {}
        self._call_claims: dict[tuple[str, str], PublicationCallClaim] = {}
        self._provider_audits: dict[tuple[str, int], PublicationProviderAuditBinding] = {}
        self._preflight: dict[str, PublicationPreflightProof] = {}
        self._provider_authorities: dict[str, PublicationProviderAuthority] = {}
        self._mutation_claims: dict[str, PublicationMutationClaim] = {}
        self._post_observations: dict[str, PublicationPostObservation] = {}
        self._product_observations: dict[tuple[str, str], PublicationProductObservation] = {}
        self._results: dict[str, PublicationResult] = {}
        self._notifications: dict[str, PublicationNotification] = {}
        self._reports: dict[str, PublicationTerminalReport] = {}
        self._tombstones: dict[str, PublicationAggregateTombstone] = {}
        self._terminal_job_links: dict[str, PublicationTerminalJobLink] = {}
        self._receipts: dict[tuple[str, str], PublicationExecutionReceipt] = {}
        for request in requests:
            self.seed_request(request)

    @property
    def aggregates(self) -> Mapping[str, PublicationAggregate | ExecutionPublicationAggregate]:
        return MappingProxyType(self._aggregates)

    @property
    def attempts(self) -> Mapping[str, PublicationAttempt | ExecutionPublicationAttempt]:
        return MappingProxyType(self._attempts)

    @property
    def permits(self) -> Mapping[str, PublicationPermit | ExecutionPublicationPermit]:
        return MappingProxyType(self._permits)

    @property
    def work(self) -> Mapping[str, PublicationWorkRequest | ExecutionPublicationWork]:
        return MappingProxyType(self._work)

    @property
    def call_claims(self) -> Mapping[tuple[str, str], PublicationCallClaim]:
        return MappingProxyType(self._call_claims)

    @property
    def receipts(self) -> Mapping[tuple[str, str], PublicationExecutionReceipt]:
        return MappingProxyType(self._receipts)

    @property
    def provider_audits(self) -> Mapping[tuple[str, int], PublicationProviderAuditBinding]:
        return MappingProxyType(self._provider_audits)

    @property
    def jobs(self) -> Mapping[str, ControlJobRecord]:
        return MappingProxyType(self._jobs)

    def seed_request(self, transaction: PublicationRequestTransaction) -> None:
        request = transaction.commit
        aggregate_id = request.aggregate.aggregate_id
        with self._lock:
            if aggregate_id in self._aggregates or transaction.updated_job.job_id in self._jobs:
                raise ValueError("Publication request was already seeded")
            self._jobs[transaction.updated_job.job_id] = transaction.updated_job
            self._source_authorities[aggregate_id] = transaction.authority
            self._snapshots[aggregate_id] = request.snapshot
            self._request_job_links[aggregate_id] = request.job_link
            self._aggregates[aggregate_id] = request.aggregate
            self._attempts[aggregate_id] = request.attempt
            self._permits[aggregate_id] = request.permit
            self._work[aggregate_id] = request.work_request
            self._initial_events[aggregate_id] = request.event

    def resolve_execution_receipt(
        self,
        owner_id: str,
        aggregate_id: str,
        operation_id: str,
    ) -> PublicationExecutionReceipt | None:
        with self._lock:
            receipt = self._receipts.get((aggregate_id, operation_id))
            if receipt is None or receipt.owner_id != owner_id:
                return None
            return receipt

    def load_execution_authority(
        self,
        owner_id: str,
        aggregate_id: str,
    ) -> PublicationExecutionAuthority:
        with self._lock:
            snapshot = self._snapshots.get(aggregate_id)
            request_job_link = self._request_job_links.get(aggregate_id)
            raw_aggregate = self._aggregates.get(aggregate_id)
            raw_attempt = self._attempts.get(aggregate_id)
            raw_permit = self._permits.get(aggregate_id)
            raw_work = self._work.get(aggregate_id)
            current_job = self._jobs.get(snapshot.job_id) if snapshot is not None else None
            if (
                snapshot is None
                or request_job_link is None
                or raw_aggregate is None
                or raw_aggregate.owner_id != owner_id
                or raw_attempt is None
                or raw_permit is None
                or raw_work is None
                or current_job is None
                or current_job.owner_id != owner_id
            ):
                raise PublicationNotFoundError()
            if isinstance(raw_aggregate, PublicationAggregate):
                aggregate = ExecutionPublicationAggregate.from_request(raw_aggregate, snapshot)
                assert isinstance(raw_attempt, PublicationAttempt)
                assert isinstance(raw_permit, PublicationPermit)
                assert isinstance(raw_work, PublicationWorkRequest)
                attempt = ExecutionPublicationAttempt.from_request(raw_attempt)
                permit = ExecutionPublicationPermit.from_request(
                    raw_permit,
                    snapshot.verification_deadline,
                )
                work = ExecutionPublicationWork.from_request(raw_work)
            else:
                aggregate = raw_aggregate
                assert isinstance(raw_attempt, ExecutionPublicationAttempt)
                assert isinstance(raw_permit, ExecutionPublicationPermit)
                assert isinstance(raw_work, ExecutionPublicationWork)
                attempt = raw_attempt
                permit = raw_permit
                work = raw_work
            claims = tuple(
                sorted(
                    (
                        claim
                        for (claim_aggregate_id, _), claim in self._call_claims.items()
                        if claim_aggregate_id == aggregate_id
                    ),
                    key=lambda value: value.resulting_attempt_record_version,
                )
            )
            product_observations = tuple(
                sorted(
                    (
                        observation
                        for (
                            observation_aggregate_id,
                            _,
                        ), observation in self._product_observations.items()
                        if observation_aggregate_id == aggregate_id
                    ),
                    key=lambda value: (
                        value.resulting_aggregate_record_version,
                        value.observation_id,
                    ),
                )
            )
            provider_audits = tuple(
                binding
                for (audit_aggregate_id, _), binding in sorted(self._provider_audits.items())
                if audit_aggregate_id == aggregate_id
            )
            return PublicationExecutionAuthority(
                snapshot=snapshot,
                request_job_link=request_job_link,
                phase6_record_version=current_job.record_version,
                phase6_event_sequence=current_job.event_sequence,
                expected_aggregate=raw_aggregate,
                expected_attempt=raw_attempt,
                expected_permit=raw_permit,
                expected_work=raw_work,
                aggregate=aggregate,
                attempt=attempt,
                permit=permit,
                work=work,
                call_claims=claims,
                provider_audits=provider_audits,
                provider_authority=self._provider_authorities.get(aggregate_id),
                preflight_proof=self._preflight.get(aggregate_id),
                mutation_claim=self._mutation_claims.get(aggregate_id),
                post_observation=self._post_observations.get(aggregate_id),
                product_observations=product_observations,
                last_product_observation=(
                    product_observations[-1] if product_observations else None
                ),
                result=self._results.get(aggregate_id),
                notification=self._notifications.get(aggregate_id),
                report=self._reports.get(aggregate_id),
                tombstone=self._tombstones.get(aggregate_id),
                terminal_job_link=self._terminal_job_links.get(aggregate_id),
            )

    def load_source_authority(
        self,
        owner_id: str,
        aggregate_id: str,
    ) -> PublicationRequestAuthority:
        with self._lock:
            source = self._source_authorities.get(aggregate_id)
            if source is None or source.current_job.owner_id != owner_id:
                raise PublicationNotFoundError()
            current_job = self._jobs.get(source.current_job.job_id)
            if current_job is None or current_job.owner_id != owner_id:
                raise PublicationNotFoundError()
            authority = PublicationRequestAuthority(
                current_job=current_job,
                review=source.review,
                approval_decision=source.approval_decision,
                source=source.source,
                product_sync=source.product_sync,
                pricing_snapshot=source.pricing_snapshot,
                pricing_evidence=source.pricing_evidence,
            )
            validate_publication_request_authority(authority)
            return authority

    def load_linked_job(
        self,
        owner_id: str,
        aggregate_id: str,
    ) -> ControlJobRecord:
        with self._lock:
            aggregate = self._aggregates.get(aggregate_id)
            if aggregate is None or aggregate.owner_id != owner_id:
                raise PublicationNotFoundError()
            job = self._jobs.get(aggregate.job_id)
            if (
                job is None
                or job.owner_id != owner_id
                or job.publication_aggregate_id != aggregate_id
            ):
                raise PublicationNotFoundError()
            return job

    def commit_execution(
        self,
        commit: PublicationExecutionCommit,
    ) -> PublicationExecutionCommitResult:
        validate_execution_commit(commit)
        aggregate_id = commit.expected.aggregate.aggregate_id
        receipt = commit.receipt
        with self._lock:
            receipt_key = (aggregate_id, receipt.operation_id)
            existing = self._receipts.get(receipt_key)
            if existing is not None:
                if existing.request_fingerprint == receipt.request_fingerprint:
                    return PublicationExecutionCommitResult(receipt=existing)
                raise PublicationIdempotencyConflictError()
            current = self.load_execution_authority(receipt.owner_id, aggregate_id)
            if current != commit.expected:
                raise PublicationConflictError(
                    PublicationErrorCode.CONCURRENT_WRITE,
                    "Publication execution authority changed before commit",
                )
            self._assert_new_records_absent(commit)
            updated_job = self._terminal_job_update(commit)
            self._aggregates[aggregate_id] = commit.updated_aggregate
            self._attempts[aggregate_id] = commit.updated_attempt
            self._permits[aggregate_id] = commit.updated_permit
            self._work[aggregate_id] = commit.updated_work
            self._events[(aggregate_id, commit.event.sequence)] = commit.event
            if commit.new_call_claim is not None:
                self._call_claims[(aggregate_id, commit.new_call_claim.authorization_id)] = (
                    commit.new_call_claim
                )
            if commit.new_preflight_proof is not None:
                self._preflight[aggregate_id] = commit.new_preflight_proof
            if commit.new_provider_authority is not None:
                self._provider_authorities[aggregate_id] = commit.new_provider_authority
            if commit.new_mutation_claim is not None:
                self._mutation_claims[aggregate_id] = commit.new_mutation_claim
            if commit.new_post_observation is not None:
                self._post_observations[aggregate_id] = commit.new_post_observation
            if commit.new_product_observation is not None:
                self._product_observations[
                    (aggregate_id, commit.new_product_observation.observation_id)
                ] = commit.new_product_observation
            if commit.new_result is not None:
                self._results[aggregate_id] = commit.new_result
            if commit.new_notification is not None:
                self._notifications[aggregate_id] = commit.new_notification
            if commit.new_report is not None:
                self._reports[aggregate_id] = commit.new_report
            if commit.new_tombstone is not None:
                self._tombstones[aggregate_id] = commit.new_tombstone
            if commit.terminal_job_update is not None:
                self._terminal_job_links[aggregate_id] = commit.terminal_job_update.link
                assert updated_job is not None
                self._jobs[updated_job.job_id] = updated_job
            self._receipts[receipt_key] = receipt
            if commit.new_call_claim is None:
                return PublicationExecutionCommitResult(receipt=receipt)
            if commit.new_mutation_claim is not None:
                grant: FreshPublicationCallGrant = FreshPublicationMutationGrant._mint(
                    commit.new_call_claim,
                    commit.new_mutation_claim,
                )
            else:
                grant = FreshPublicationCallGrant._mint(commit.new_call_claim)
            return PublicationExecutionCommitResult(receipt=receipt, fresh_call_grant=grant)

    def commit_provider_audit(
        self,
        commit: PublicationProviderAuditCommit,
    ) -> PublicationProviderAuditBinding:
        revalidated = PublicationProviderAuditCommit.model_validate(
            commit.model_dump(mode="python")
        )
        if revalidated != commit:
            raise PublicationConflictError(
                PublicationErrorCode.INVALID_AUTHORITY,
                "Provider audit commit failed strict revalidation",
            )
        binding = commit.binding
        with self._lock:
            key = (binding.aggregate_id, binding.durable_call_sequence)
            existing = self._provider_audits.get(key)
            if existing is not None:
                if existing == binding:
                    return existing
                raise PublicationConflictError(
                    PublicationErrorCode.CONCURRENT_WRITE,
                    "Provider audit sequence already exists",
                )
            raw_aggregate = self._aggregates.get(binding.aggregate_id)
            claim = self._call_claims.get((binding.aggregate_id, binding.call_claim_id))
            if (
                not isinstance(raw_aggregate, ExecutionPublicationAggregate)
                or raw_aggregate != commit.expected_aggregate
                or raw_aggregate.terminal_at is not None
                or raw_aggregate.report_id is not None
                or claim is None
                or claim.fingerprint != binding.call_claim_fingerprint
                or claim.resulting_attempt_record_version != binding.durable_call_sequence
            ):
                raise PublicationConflictError(
                    PublicationErrorCode.CONCURRENT_WRITE,
                    "Provider audit lost its exact nonterminal call authority",
                )
            current = self.load_execution_authority(
                raw_aggregate.owner_id,
                binding.aggregate_id,
            )
            claim_state = claim.call_kind.value
            purpose = claim.purpose.value
            if claim_state == "shop_get":
                compatible = (
                    current.aggregate.state.value == "publication_requested"
                    and current.permit.status.value == "available"
                    and current.preflight_proof is None
                    and current.mutation_claim is None
                    and current.post_observation is None
                )
            elif purpose == "product_preflight":
                compatible = (
                    current.aggregate.state.value == "publication_requested"
                    and current.permit.status.value == "available"
                    and current.preflight_proof is None
                    and current.mutation_claim is None
                    and current.post_observation is None
                )
            elif purpose == "positive_verification":
                compatible = (
                    current.aggregate.state.value == "publication_verifying"
                    and current.permit.status.value == "consumed"
                    and current.post_observation is not None
                )
            elif purpose == "reconciliation":
                compatible = (
                    current.aggregate.state.value == "publication_reconciling"
                    and current.permit.status.value == "consumed"
                    and current.post_observation is not None
                )
            else:
                compatible = (
                    claim_state == "publish_post"
                    and current.aggregate.state.value == "publication_requested"
                    and current.permit.status.value == "consumed"
                    and current.mutation_claim is not None
                    and current.mutation_claim.call_claim_id == claim.authorization_id
                    and current.post_observation is None
                )
            if not compatible:
                raise PublicationConflictError(
                    PublicationErrorCode.CONCURRENT_WRITE,
                    "Provider audit claim is no longer live in the execution state",
                )
            try:
                PublicationExecutionAuthority.model_validate(
                    {
                        **current.model_dump(mode="python"),
                        "expected_aggregate": commit.updated_aggregate,
                        "aggregate": commit.updated_aggregate,
                        "provider_audits": tuple(
                            sorted(
                                (*current.provider_audits, binding),
                                key=lambda value: value.durable_call_sequence,
                            )
                        ),
                    }
                )
            except ValueError:
                raise PublicationConflictError(
                    PublicationErrorCode.INVALID_AUTHORITY,
                    "Provider audit does not match the durable call claim",
                ) from None
            self._aggregates[binding.aggregate_id] = commit.updated_aggregate
            self._provider_audits[key] = binding
            return binding

    def _terminal_job_update(
        self,
        commit: PublicationExecutionCommit,
    ) -> ControlJobRecord | None:
        update = commit.terminal_job_update
        if update is None:
            return None
        link = update.link
        current = self._jobs.get(link.job_id)
        if (
            current is None
            or current.owner_id != link.owner_id
            or current.state is not ControlJobState.APPROVED
            or current.publication_aggregate_id != link.aggregate_id
            or current.record_version != link.expected_record_version
            or current.event_sequence != link.expected_event_sequence
            or current.publication_terminal_state is not None
        ):
            raise PublicationConflictError(
                PublicationErrorCode.CONCURRENT_WRITE,
                "Phase 6 terminal publication summary changed before settlement",
            )
        expected_summary_fingerprint = publication_terminal_summary_fingerprint(
            aggregate_id=link.aggregate_id,
            terminal_state=link.terminal_state.value,
            terminal_at=link.terminal_at,
            source_release_eligible_at=link.source_release_eligible_at,
            operational_expires_at=link.operational_expires_at,
            report_id=link.report_id,
            result_id=link.result_id,
        )
        if link.terminal_summary_fingerprint != expected_summary_fingerprint:
            raise PublicationConflictError(
                PublicationErrorCode.INVALID_AUTHORITY,
                "Terminal job summary fingerprint is invalid",
            )
        if update.expected_job != current:
            raise PublicationConflictError(
                PublicationErrorCode.CONCURRENT_WRITE,
                "Phase 6 job changed before terminal settlement",
            )
        return update.updated_job

    def _assert_new_records_absent(self, commit: PublicationExecutionCommit) -> None:
        aggregate_id = commit.expected.aggregate.aggregate_id
        checks: tuple[tuple[object | None, Mapping[object, object], object], ...] = (
            (
                commit.new_call_claim,
                self._call_claims,
                (
                    aggregate_id,
                    commit.new_call_claim.authorization_id
                    if commit.new_call_claim is not None
                    else "",
                ),
            ),
            (commit.new_preflight_proof, self._preflight, aggregate_id),
            (commit.new_provider_authority, self._provider_authorities, aggregate_id),
            (commit.new_mutation_claim, self._mutation_claims, aggregate_id),
            (commit.new_post_observation, self._post_observations, aggregate_id),
            (commit.new_result, self._results, aggregate_id),
            (commit.new_notification, self._notifications, aggregate_id),
            (commit.new_report, self._reports, aggregate_id),
            (commit.new_tombstone, self._tombstones, aggregate_id),
            (commit.terminal_job_update, self._terminal_job_links, aggregate_id),
        )
        if any(record is not None and key in mapping for record, mapping, key in checks):
            raise PublicationConflictError(
                PublicationErrorCode.CONCURRENT_WRITE,
                "Immutable publication execution evidence already exists",
            )
        if (
            commit.new_product_observation is not None
            and (
                aggregate_id,
                commit.new_product_observation.observation_id,
            )
            in self._product_observations
        ):
            raise PublicationConflictError(
                PublicationErrorCode.CONCURRENT_WRITE,
                "Publication product observation already exists",
            )


def validate_execution_commit(commit: PublicationExecutionCommit) -> None:
    """Defensively validate CAS shape before any adapter performs writes."""

    # Deep revalidation closes frozen-model ``model_copy`` bypasses.
    revalidated = PublicationExecutionCommit.model_validate(commit.model_dump(mode="python"))
    if revalidated != commit:
        raise PublicationConflictError(
            PublicationErrorCode.INVALID_AUTHORITY,
            "Publication execution commit failed strict revalidation",
        )
    current = commit.expected
    if (
        commit.updated_aggregate.record_version != current.aggregate.record_version + 1
        or commit.updated_aggregate.event_sequence != current.aggregate.event_sequence + 1
        or commit.event.sequence != commit.updated_aggregate.event_sequence
        or commit.receipt.aggregate_record_version != commit.updated_aggregate.record_version
        or commit.receipt.attempt_record_version != commit.updated_attempt.record_version
        or commit.receipt.permit_record_version != commit.updated_permit.record_version
        or commit.receipt.work_record_version != commit.updated_work.record_version
        or commit.receipt.aggregate_state is not commit.updated_aggregate.state
    ):
        raise PublicationConflictError(
            PublicationErrorCode.INVALID_AUTHORITY,
            "Execution versions and event sequence are inconsistent",
        )
    operation = commit.receipt.operation
    if isinstance(current.expected_aggregate, PublicationAggregate) and operation not in {
        PublicationExecutionOperation.DISPATCH,
        PublicationExecutionOperation.SETTLE_DEADLINE,
    }:
        raise PublicationConflictError(
            PublicationErrorCode.INVALID_AUTHORITY,
            "Dispatch is the mandatory pristine-to-execution normalization boundary",
        )
    if (
        not isinstance(current.expected_aggregate, PublicationAggregate)
        and operation is PublicationExecutionOperation.DISPATCH
    ):
        raise PublicationConflictError(
            PublicationErrorCode.INVALID_AUTHORITY,
            "Publication work can be dispatched only once",
        )

    _validate_stable_execution_fields(commit)
    _validate_operation_write_set(commit)

    resulting_claims = current.call_claims + (
        (commit.new_call_claim,) if commit.new_call_claim is not None else ()
    )
    try:
        resulting = PublicationExecutionAuthority(
            snapshot=current.snapshot,
            request_job_link=current.request_job_link,
            phase6_record_version=(
                commit.terminal_job_update.link.result_record_version
                if commit.terminal_job_update is not None
                else current.phase6_record_version
            ),
            phase6_event_sequence=current.phase6_event_sequence,
            expected_aggregate=commit.updated_aggregate,
            expected_attempt=commit.updated_attempt,
            expected_permit=commit.updated_permit,
            expected_work=commit.updated_work,
            aggregate=commit.updated_aggregate,
            attempt=commit.updated_attempt,
            permit=commit.updated_permit,
            work=commit.updated_work,
            call_claims=resulting_claims,
            provider_audits=current.provider_audits,
            provider_authority=commit.new_provider_authority or current.provider_authority,
            preflight_proof=commit.new_preflight_proof or current.preflight_proof,
            mutation_claim=commit.new_mutation_claim or current.mutation_claim,
            post_observation=commit.new_post_observation or current.post_observation,
            last_product_observation=(
                commit.new_product_observation or current.last_product_observation
            ),
            product_observations=current.product_observations
            + (
                (commit.new_product_observation,)
                if commit.new_product_observation is not None
                else ()
            ),
            result=commit.new_result or current.result,
            notification=commit.new_notification or current.notification,
            report=commit.new_report or current.report,
            tombstone=commit.new_tombstone or current.tombstone,
            terminal_job_link=(
                commit.terminal_job_update.link
                if commit.terminal_job_update is not None
                else current.terminal_job_link
            ),
        )
    except ValueError:
        raise PublicationConflictError(
            PublicationErrorCode.INVALID_AUTHORITY,
            "Publication execution transition does not produce a valid authority graph",
        ) from None
    if resulting.aggregate.fingerprint != commit.updated_aggregate.fingerprint:
        raise PublicationConflictError(
            PublicationErrorCode.INVALID_AUTHORITY,
            "Publication execution resulting graph changed during validation",
        )


def _record_material(record: PublicationModel, mutable: frozenset[str]) -> dict[str, object]:
    return record.model_dump(
        mode="python",
        exclude={"contract_version", "fingerprint", *mutable},
    )


def _validate_stable_execution_fields(commit: PublicationExecutionCommit) -> None:
    current = commit.expected
    operation = commit.receipt.operation
    aggregate_mutable = {"record_version", "event_sequence", "updated_at"}
    attempt_mutable: set[str] = set()
    permit_mutable: set[str] = set()
    work_mutable = {"record_version", "updated_at"}

    if operation is PublicationExecutionOperation.DISPATCH:
        work_mutable.update({"status", "attempt_count", "next_dispatch_at", "dispatched_at"})
    elif operation is PublicationExecutionOperation.CLAIM_SHOP_GET:
        attempt_mutable.update({"record_version", "shop_get_call_count"})
    elif operation is PublicationExecutionOperation.CLAIM_PRODUCT_GET:
        attempt_mutable.update({"record_version", "product_get_call_count"})
    elif operation is PublicationExecutionOperation.CLAIM_PUBLISH:
        attempt_mutable.update({"record_version", "publish_post_call_count"})
        permit_mutable.update({"status", "record_version", "consumed_at", "mutation_claim_id"})
    elif operation in {
        PublicationExecutionOperation.RECORD_POST_OUTCOME,
        PublicationExecutionOperation.RECOVER_CONSUMED_CLAIM,
    }:
        aggregate_mutable.update({"state", "last_observation_fingerprint"})
        work_mutable.add("status")
    elif operation is PublicationExecutionOperation.RECORD_PRODUCT_OBSERVATION:
        aggregate_mutable.add("last_observation_fingerprint")

    terminal = commit.new_report is not None
    if terminal:
        aggregate_mutable.update(
            {
                "state",
                "terminal_at",
                "source_release_eligible_at",
                "operational_expires_at",
                "last_observation_fingerprint",
                "result_id",
                "notification_id",
                "report_id",
                "tombstone_id",
            }
        )
        attempt_mutable.update({"status", "terminal_at"})
        work_mutable.update({"status", "terminal_at", "next_dispatch_at"})
        if current.permit.status.value == "available":
            permit_mutable.update({"status", "record_version", "retired_at", "retirement_reason"})

    comparisons = (
        (
            current.aggregate,
            commit.updated_aggregate,
            frozenset(aggregate_mutable),
        ),
        (
            current.attempt,
            commit.updated_attempt,
            frozenset(attempt_mutable),
        ),
        (
            current.permit,
            commit.updated_permit,
            frozenset(permit_mutable),
        ),
        (
            current.work,
            commit.updated_work,
            frozenset(work_mutable),
        ),
    )
    if any(
        _record_material(before, mutable) != _record_material(after, mutable)
        for before, after, mutable in comparisons
    ):
        raise PublicationConflictError(
            PublicationErrorCode.INVALID_AUTHORITY,
            "Execution transition changed immutable root authority",
        )


def _validate_operation_write_set(commit: PublicationExecutionCommit) -> None:
    operation = commit.receipt.operation
    current = commit.expected
    optional_records = {
        "call": commit.new_call_claim,
        "provider_authority": commit.new_provider_authority,
        "preflight": commit.new_preflight_proof,
        "mutation": commit.new_mutation_claim,
        "post": commit.new_post_observation,
        "product": commit.new_product_observation,
        "result": commit.new_result,
        "notification": commit.new_notification,
        "report": commit.new_report,
        "tombstone": commit.new_tombstone,
        "job_link": commit.terminal_job_update,
    }
    terminal_names = {"report", "tombstone", "job_link"}
    if (
        commit.new_post_observation is not None
        and commit.new_post_observation.outcome.value == "definitive_synchronous_rejection"
    ):
        raise PublicationConflictError(
            PublicationErrorCode.INVALID_AUTHORITY,
            "No closed definitive-rejection classifier exists in the offline slice",
        )
    expected_required: set[str]
    expected_allowed: set[str]
    if operation is PublicationExecutionOperation.DISPATCH:
        expected_required = expected_allowed = set()
    elif operation is PublicationExecutionOperation.RECONSTRUCT_AUTHORITY:
        expected_required = expected_allowed = {"provider_authority"}
    elif operation in {
        PublicationExecutionOperation.CLAIM_SHOP_GET,
        PublicationExecutionOperation.CLAIM_PRODUCT_GET,
    }:
        expected_required = expected_allowed = {"call"}
    elif operation is PublicationExecutionOperation.RECORD_PREFLIGHT:
        expected_required = expected_allowed = {"preflight"}
    elif operation is PublicationExecutionOperation.CLAIM_PUBLISH:
        expected_required = expected_allowed = {"call", "mutation"}
    elif operation is PublicationExecutionOperation.RECORD_POST_OUTCOME:
        expected_required = expected_allowed = {"post"}
    elif operation is PublicationExecutionOperation.RECOVER_CONSUMED_CLAIM:
        expected_required = expected_allowed = {"post"}
    elif operation is PublicationExecutionOperation.RECORD_PRODUCT_OBSERVATION:
        expected_required = {"product"}
        expected_allowed = {
            "product",
            "result",
            "notification",
            *terminal_names,
        }
    else:
        expected_required = expected_allowed = terminal_names
    present = {name for name, record in optional_records.items() if record is not None}
    if not expected_required.issubset(present) or not present.issubset(expected_allowed):
        raise PublicationConflictError(
            PublicationErrorCode.INVALID_AUTHORITY,
            "Execution operation has an invalid atomic write set",
        )
    terminal_present = bool(present & terminal_names)
    if terminal_present and not terminal_names.issubset(present):
        raise PublicationConflictError(
            PublicationErrorCode.INVALID_AUTHORITY,
            "Terminal report, tombstone, and job summary must commit together",
        )

    call_increment = 1 if commit.new_call_claim is not None else 0
    if commit.updated_attempt.record_version != current.attempt.record_version + call_increment:
        raise PublicationConflictError(
            PublicationErrorCode.INVALID_AUTHORITY,
            "Attempt version must increase exactly once per durable call claim",
        )
    expected_counts = [
        current.attempt.shop_get_call_count,
        current.attempt.product_get_call_count,
        current.attempt.publish_post_call_count,
    ]
    if commit.new_call_claim is not None:
        kind_index = {
            "shop_get": 0,
            "product_get": 1,
            "publish_post": 2,
        }[commit.new_call_claim.call_kind.value]
        expected_counts[kind_index] += 1
    if expected_counts != [
        commit.updated_attempt.shop_get_call_count,
        commit.updated_attempt.product_get_call_count,
        commit.updated_attempt.publish_post_call_count,
    ]:
        raise PublicationConflictError(
            PublicationErrorCode.INVALID_AUTHORITY,
            "Execution operation changed a call counter without a matching claim",
        )
    permit_increment = (
        1
        if operation is PublicationExecutionOperation.CLAIM_PUBLISH
        or (
            operation is PublicationExecutionOperation.SETTLE_DEADLINE
            and current.permit.status.value == "available"
        )
        else 0
    )
    if commit.updated_permit.record_version != current.permit.record_version + permit_increment:
        raise PublicationConflictError(
            PublicationErrorCode.INVALID_AUTHORITY,
            "Permit version differs from the sole legal transition",
        )
    if commit.updated_work.record_version != current.work.record_version + 1:
        raise PublicationConflictError(
            PublicationErrorCode.INVALID_AUTHORITY,
            "Every execution transition must advance publication work exactly once",
        )
    _validate_operation_event_and_authority(commit)


def _validate_operation_event_and_authority(commit: PublicationExecutionCommit) -> None:
    """Bind each operation to its sole event, state, claim class, and receipt pointer."""

    operation = commit.receipt.operation
    current = commit.expected
    claim = commit.new_call_claim
    authority_record: PublicationModel
    event_name: object
    state = commit.updated_aggregate.state

    pre_dispatch_expiry = (
        operation is PublicationExecutionOperation.SETTLE_DEADLINE
        and current.work.status.value == "pending"
        and current.work.attempt_count == 0
        and current.work.next_dispatch_at is not None
        and current.work.dispatched_at is None
        and commit.updated_work.status.value == "failed"
        and commit.updated_work.attempt_count == 0
        and commit.updated_work.next_dispatch_at is None
        and commit.updated_work.dispatched_at is None
    )
    dispatch_metadata_changed = (
        commit.updated_work.attempt_count != current.work.attempt_count
        or commit.updated_work.next_dispatch_at != current.work.next_dispatch_at
        or commit.updated_work.dispatched_at != current.work.dispatched_at
    )
    if (
        operation is not PublicationExecutionOperation.DISPATCH
        and not pre_dispatch_expiry
        and dispatch_metadata_changed
    ):
        raise PublicationConflictError(
            PublicationErrorCode.INVALID_AUTHORITY,
            "Only dispatch may change immutable publication dispatch metadata",
        )

    if operation is PublicationExecutionOperation.DISPATCH:
        authority_record = commit.updated_work
        record_id = commit.updated_work.work_request_id
        event_name = "PUBLICATION_WORK_DISPATCHED"
        valid = (
            state.value == "publication_requested"
            and commit.updated_work.status.value == "dispatched"
            and commit.updated_work.attempt_count == 1
            and commit.updated_work.dispatched_at == commit.event.occurred_at
        )
    elif operation is PublicationExecutionOperation.RECONSTRUCT_AUTHORITY:
        assert commit.new_provider_authority is not None
        authority_record = commit.new_provider_authority
        record_id = commit.new_provider_authority.provider_authority_id
        event_name = "PUBLICATION_PROVIDER_AUTHORITY_RECONSTRUCTED"
        valid = (
            state == current.aggregate.state
            and commit.new_provider_authority.reconstructed_at == commit.event.occurred_at
        )
    elif operation is PublicationExecutionOperation.CLAIM_SHOP_GET:
        assert claim is not None
        authority_record = claim
        record_id = claim.authorization_id
        event_name = "PUBLICATION_PROVIDER_CALL_AUTHORIZED"
        valid = (
            claim.call_kind.value == "shop_get"
            and claim.purpose.value == "etsy_shop_preflight"
            and current.aggregate.state.value == "publication_requested"
            and state == current.aggregate.state
            and current.work.status.value == "dispatched"
            and commit.updated_work.status == current.work.status
            and current.permit.status.value == "available"
            and commit.updated_permit == current.permit
            and current.preflight_proof is None
            and current.mutation_claim is None
            and current.post_observation is None
        )
    elif operation is PublicationExecutionOperation.CLAIM_PRODUCT_GET:
        assert claim is not None
        authority_record = claim
        record_id = claim.authorization_id
        event_name = "PUBLICATION_PROVIDER_CALL_AUTHORIZED"
        expected_purpose = {
            "publication_requested": "product_preflight",
            "publication_verifying": "positive_verification",
            "publication_reconciling": "reconciliation",
        }.get(current.aggregate.state.value)
        preflight = (
            current.aggregate.state.value == "publication_requested"
            and current.work.status.value == "dispatched"
            and current.permit.status.value == "available"
            and current.preflight_proof is None
            and current.mutation_claim is None
            and current.post_observation is None
        )
        verification = (
            current.aggregate.state.value == "publication_verifying"
            and current.work.status.value == "verifying"
            and current.permit.status.value == "consumed"
        )
        reconciliation = (
            current.aggregate.state.value == "publication_reconciling"
            and current.work.status.value == "reconciling"
            and current.permit.status.value == "consumed"
        )
        valid = (
            claim.call_kind.value == "product_get"
            and claim.purpose.value == expected_purpose
            and state == current.aggregate.state
            and commit.updated_work.status == current.work.status
            and commit.updated_permit == current.permit
            and (preflight or verification or reconciliation)
        )
    elif operation is PublicationExecutionOperation.RECORD_PREFLIGHT:
        assert commit.new_preflight_proof is not None
        authority_record = commit.new_preflight_proof
        record_id = commit.new_preflight_proof.proof_id
        event_name = "PUBLICATION_PREFLIGHT_PROVEN"
        valid = (
            current.aggregate.state.value == "publication_requested"
            and state == current.aggregate.state
            and current.work.status.value == "dispatched"
            and commit.updated_work.status == current.work.status
            and current.permit.status.value == "available"
            and commit.updated_permit == current.permit
            and current.preflight_proof is None
            and current.mutation_claim is None
            and current.post_observation is None
            and commit.new_preflight_proof.proven_at == commit.event.occurred_at
        )
    elif operation is PublicationExecutionOperation.CLAIM_PUBLISH:
        assert claim is not None and commit.new_mutation_claim is not None
        authority_record = commit.new_mutation_claim
        record_id = commit.new_mutation_claim.mutation_claim_id
        event_name = "PUBLICATION_PUBLISH_CLAIMED"
        valid = (
            claim.call_kind.value == "publish_post"
            and claim.purpose.value == "one_shot_connected_channel_publication"
            and current.aggregate.state.value == "publication_requested"
            and state == current.aggregate.state
            and current.work.status.value == "dispatched"
            and commit.updated_work.status == current.work.status
            and current.permit.status.value == "available"
            and current.preflight_proof is not None
            and current.mutation_claim is None
            and current.post_observation is None
            and commit.updated_permit.status.value == "consumed"
        )
    elif operation in {
        PublicationExecutionOperation.RECORD_POST_OUTCOME,
        PublicationExecutionOperation.RECOVER_CONSUMED_CLAIM,
    }:
        assert commit.new_post_observation is not None
        observation = commit.new_post_observation
        authority_record = observation
        record_id = observation.observation_id
        post_base = (
            current.aggregate.state.value == "publication_requested"
            and current.work.status.value == "dispatched"
            and current.permit.status.value == "consumed"
            and commit.updated_permit == current.permit
            and current.mutation_claim is not None
            and current.post_observation is None
        )
        if observation.outcome.value == "definitely_accepted":
            event_name = "PUBLICATION_VERIFYING"
            valid = (
                operation is PublicationExecutionOperation.RECORD_POST_OUTCOME
                and post_base
                and state.value == "publication_verifying"
                and commit.updated_work.status.value == "verifying"
            )
        else:
            event_name = "PUBLICATION_RECONCILING"
            recovery = (
                observation.response_category.value
                == "consumed_claim_without_durable_boundary_observation"
            )
            valid = (
                post_base
                and state.value == "publication_reconciling"
                and commit.updated_work.status.value == "reconciling"
                and recovery == (operation is PublicationExecutionOperation.RECOVER_CONSUMED_CLAIM)
            )
    elif operation is PublicationExecutionOperation.RECORD_PRODUCT_OBSERVATION:
        assert commit.new_product_observation is not None
        observation = commit.new_product_observation
        authority_record = observation
        record_id = observation.observation_id
        product_base = (
            current.aggregate.state.value in {"publication_verifying", "publication_reconciling"}
            and current.work.status.value
            == {
                "publication_verifying": "verifying",
                "publication_reconciling": "reconciling",
            }[current.aggregate.state.value]
            and current.permit.status.value == "consumed"
            and commit.updated_permit == current.permit
            and current.mutation_claim is not None
            and current.post_observation is not None
            and current.report is None
        )
        if (
            observation.resulting_aggregate_record_version
            != commit.updated_aggregate.record_version
        ):
            valid = False
            event_name = "PUBLICATION_OBSERVED"
        elif state.value == "published":
            event_name = "PUBLISHED"
            valid = (
                product_base
                and observation.outcome.value == "positive_publication_proof"
                and commit.new_result is not None
                and commit.new_notification is not None
            )
        elif state.value == "publication_outcome_unknown":
            event_name = "PUBLICATION_OUTCOME_UNKNOWN"
            valid = product_base and observation.outcome.value != "positive_publication_proof"
        else:
            event_name = "PUBLICATION_OBSERVED"
            valid = (
                product_base
                and state == current.aggregate.state
                and observation.outcome.value != "positive_publication_proof"
            )
    else:
        assert commit.new_report is not None
        authority_record = commit.new_report
        record_id = commit.new_report.report_id
        if state.value == "publication_failed":
            event_name = "PUBLICATION_FAILED"
            valid = (
                current.aggregate.state.value == "publication_requested"
                and current.permit.status.value == "available"
                and commit.updated_permit.status.value == "retired"
                and commit.updated_permit.retirement_reason is not None
                and commit.updated_permit.retirement_reason.value == "pre_call_deadline_expired"
                and commit.new_report.terminal_reason.value == "pre_call_deadline_expired"
                and commit.updated_aggregate.terminal_at >= current.aggregate.verification_deadline
                and commit.updated_aggregate.terminal_at == commit.event.occurred_at
                and commit.updated_permit.retired_at == commit.event.occurred_at
            )
        else:
            event_name = "PUBLICATION_OUTCOME_UNKNOWN"
            valid = (
                state.value == "publication_outcome_unknown"
                and current.aggregate.state.value
                in {"publication_verifying", "publication_reconciling"}
                and current.permit.status.value == "consumed"
                and commit.updated_permit == current.permit
                and commit.new_report.terminal_reason.value
                == "fixed_deadline_without_positive_proof"
                and commit.updated_aggregate.terminal_at >= current.aggregate.verification_deadline
            )

    if (
        not valid
        or commit.event.name.value != event_name
        or commit.event.state is not state
        or commit.event.authority_fingerprint != authority_record.fingerprint
        or commit.receipt.authority_record_id != record_id
        or commit.receipt.authority_fingerprint != authority_record.fingerprint
        or commit.event.occurred_at != commit.receipt.created_at
        or commit.event.occurred_at != commit.updated_aggregate.updated_at
        or commit.event.occurred_at != commit.updated_work.updated_at
        or commit.updated_aggregate.updated_at < current.aggregate.updated_at
        or commit.updated_work.updated_at < current.work.updated_at
    ):
        raise PublicationConflictError(
            PublicationErrorCode.INVALID_AUTHORITY,
            "Execution operation differs from its exact event, state, or authority record",
        )
    if claim is not None and (
        claim.operation_id != commit.receipt.operation_id
        or claim.authorized_at != commit.event.occurred_at
    ):
        raise PublicationConflictError(
            PublicationErrorCode.INVALID_AUTHORITY,
            "Durable call claim differs from its winning operation and timestamp",
        )
    if commit.new_mutation_claim is not None and (
        commit.new_mutation_claim.authorized_at != commit.event.occurred_at
        or commit.updated_permit.consumed_at != commit.event.occurred_at
    ):
        raise PublicationConflictError(
            PublicationErrorCode.INVALID_AUTHORITY,
            "Mutation claim and permit consumption must share the winning timestamp",
        )
    if (
        commit.new_post_observation is not None
        and commit.new_post_observation.observed_at > commit.event.occurred_at
    ) or (
        commit.new_product_observation is not None
        and commit.new_product_observation.observed_at > commit.event.occurred_at
    ):
        raise PublicationConflictError(
            PublicationErrorCode.INVALID_AUTHORITY,
            "Provider evidence cannot postdate its durable application settlement",
        )
