"""Offline oracle tests for the frozen Phase 7.2 publication execution domain."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timedelta
from threading import Barrier
from typing import Any

import pytest
from pydantic import ValidationError

from mr_lister.publication.contract import PublicationPermitState, PublicationState
from mr_lister.publication.errors import (
    PublicationConflictError,
)
from mr_lister.publication.execution_commands import (
    ClaimProductGetCommand,
    ClaimPublicationMutationCommand,
    ClaimShopGetCommand,
    DispatchPublicationWorkCommand,
    ReconstructPublicationAuthorityCommand,
    RecordPublicationPostOutcomeCommand,
    RecordPublicationPreflightCommand,
    RecordPublicationProductObservationCommand,
    RecoverConsumedPublicationClaimCommand,
    SettlePublicationDeadlineCommand,
)
from mr_lister.publication.execution_fingerprints import execution_record_fingerprint
from mr_lister.publication.execution_models import (
    ExecutionPublicationAggregate,
    PublicationCallClaim,
    PublicationCallKind,
    PublicationCallPurpose,
    PublicationExecutionOperation,
    PublicationExecutionWorkStatus,
    PublicationExternalEvidenceState,
    PublicationPostOutcome,
    PublicationProductReadEvidence,
    PublicationProviderAuditCategory,
    PublicationProviderAuditDecision,
    PublicationProviderAuditRecord,
    PublicationPublishEvidence,
    PublicationPublishResponseCategory,
    PublicationReadOutcome,
    PublicationShopPreflightEvidence,
    PublicationTerminalReason,
)
from mr_lister.publication.execution_service import PublicationExecutionService
from mr_lister.publication.execution_store import (
    FreshPublicationMutationGrant,
    InMemoryPublicationExecutionStore,
    PublicationExecutionCommit,
    build_provider_audit_commit,
    validate_execution_commit,
)
from mr_lister.publication.models import PublicationAggregate
from tests.test_phase71_publication_service import (
    AuthorityStore,
    CountingClock,
    ProfileAuthority,
    _authority,
)
from tests.test_phase71_publication_service import (
    _command as request_command,
)
from tests.test_phase71_publication_service import (
    _service as request_service,
)
from tests.test_phase71_publication_store import NOW, OWNER_ID, make_transaction


@dataclass
class Clock:
    now: datetime

    def __call__(self) -> datetime:
        return self.now

    def tick(self, seconds: int = 1) -> None:
        self.now += timedelta(seconds=seconds)


class CapturingExecutionStore(InMemoryPublicationExecutionStore):
    def __init__(self, requests: tuple[Any, ...]) -> None:
        super().__init__(requests)
        self.last_commit: PublicationExecutionCommit | None = None

    def commit_execution(self, commit: PublicationExecutionCommit):  # type: ignore[no-untyped-def]
        self.last_commit = commit
        return super().commit_execution(commit)


class Harness:
    def __init__(
        self,
        *,
        short_pricing_window: bool = False,
        capture_commits: bool = False,
    ) -> None:
        source, exact = _authority()
        if short_pricing_window:
            request_at = source.pricing_snapshot.fresh_until - timedelta(minutes=1)
            request_store = AuthorityStore(source)
            service, _, _ = request_service(
                request_store,
                exact,
                clock=CountingClock(request_at),
            )
            service.request_publication(request_command(source))
            assert request_store.transaction is not None
            self.transaction = request_store.transaction
            release_fingerprint = "f" * 64
            initial_time = request_at + timedelta(seconds=1)
        else:
            self.transaction = make_transaction(source)
            release_fingerprint = "b" * 64
            initial_time = NOW + timedelta(seconds=1)
        self.aggregate_id = self.transaction.commit.aggregate.aggregate_id
        self.clock = Clock(initial_time)
        store_type = (
            CapturingExecutionStore if capture_commits else InMemoryPublicationExecutionStore
        )
        self.store = store_type((self.transaction,))
        self.service = PublicationExecutionService(
            self.store,
            profiles=ProfileAuthority(exact),
            release_manifest_fingerprint=release_fingerprint,
            clock=self.clock,
        )
        self.operation_number = 0

    @property
    def authority(self):  # type: ignore[no-untyped-def]
        return self.store.load_execution_authority(OWNER_ID, self.aggregate_id)

    def command(self, command_type: type[Any], name: str, **values: object) -> Any:
        authority = self.authority
        return command_type(
            owner_id=OWNER_ID,
            aggregate_id=self.aggregate_id,
            operation_id=name,
            expected_aggregate_record_version=authority.aggregate.record_version,
            expected_attempt_record_version=authority.attempt.record_version,
            expected_permit_record_version=authority.permit.record_version,
            expected_work_record_version=authority.work.record_version,
            **values,
        )

    def next_operation(self, prefix: str) -> str:
        self.operation_number += 1
        return f"{prefix}_{self.operation_number}"

    def dispatch_and_reconstruct(self) -> None:
        self.service.dispatch_work(
            self.command(DispatchPublicationWorkCommand, self.next_operation("dispatch"))
        )
        self.clock.tick()
        self.service.reconstruct_authority(
            self.command(
                ReconstructPublicationAuthorityCommand,
                self.next_operation("reconstruct"),
            )
        )
        self.clock.tick()

    def audit(self, claim: PublicationCallClaim):
        category = {
            PublicationCallKind.SHOP_GET: PublicationProviderAuditCategory.SHOP_GET_ALLOWED,
            PublicationCallKind.PRODUCT_GET: PublicationProviderAuditCategory.PRODUCT_GET_ALLOWED,
            PublicationCallKind.PUBLISH_POST: (
                PublicationProviderAuditCategory.PUBLISH_POST_ALLOWED
            ),
        }[claim.call_kind]
        values = {
            "decision": PublicationProviderAuditDecision.ALLOWED,
            "method_category": claim.method,
            "route_template": claim.route_template,
            "category": category,
        }
        audit = PublicationProviderAuditRecord(
            **values,
            fingerprint=execution_record_fingerprint("provider_audit_record", values),
        )
        commit = build_provider_audit_commit(self.authority, claim, audit)
        return commit, self.store.commit_provider_audit(commit)

    def claim_shop(self, *, audit: bool = True):  # type: ignore[no-untyped-def]
        result = self.service.claim_shop_get(
            self.command(ClaimShopGetCommand, self.next_operation("shop"))
        )
        claim = self.authority.call_claims[-1]
        if audit:
            self.audit(claim)
        return result, claim

    def claim_product(
        self,
        purpose: PublicationCallPurpose,
        *,
        audit: bool = True,
    ):
        result = self.service.claim_product_get(
            self.command(
                ClaimProductGetCommand,
                self.next_operation("product"),
                purpose=purpose,
            )
        )
        claim = self.authority.call_claims[-1]
        if audit:
            self.audit(claim)
        return result, claim

    def shop_evidence(self, claim: PublicationCallClaim) -> PublicationShopPreflightEvidence:
        provider = self.authority.provider_authority
        assert provider is not None
        values = {
            "call_claim_id": claim.authorization_id,
            "call_claim_fingerprint": claim.fingerprint,
            "provider_authority_id": provider.provider_authority_id,
            "provider_authority_fingerprint": provider.fingerprint,
            "printify_shop_id": provider.printify_shop_id,
            "sales_channel": "etsy",
            "sanitized_response_fingerprint": "1" * 64,
            "observed_at": self.clock.now,
        }
        return PublicationShopPreflightEvidence(
            **values,
            fingerprint=execution_record_fingerprint("shop_preflight_evidence", values),
        )

    def product_evidence(
        self,
        claim: PublicationCallClaim,
        *,
        positive: bool = False,
        observed_at: datetime | None = None,
    ) -> PublicationProductReadEvidence:
        provider = self.authority.provider_authority
        assert provider is not None
        values = {
            "call_claim_id": claim.authorization_id,
            "call_claim_fingerprint": claim.fingerprint,
            "provider_authority_id": provider.provider_authority_id,
            "provider_authority_fingerprint": provider.fingerprint,
            "printify_shop_id": provider.printify_shop_id,
            "printify_product_id": provider.printify_product_id,
            "sanitized_response_fingerprint": "2" * 64,
            "product_present": True,
            "canonical_payload_fingerprint": provider.product_payload_fingerprint,
            "canonical_content_match": True,
            "exact_variant_economics": True,
            "exact_placement_image": True,
            "exact_mockups": True,
            "is_locked": False,
            "visible": positive,
            "external_evidence": (
                PublicationExternalEvidenceState.SINGLE_NUMERIC_ETSY_REFERENCE
                if positive
                else PublicationExternalEvidenceState.ABSENT
            ),
            "numeric_listing_id": 123456789 if positive else None,
            "read_outcome": (
                PublicationReadOutcome.POSITIVE_PROOF
                if positive
                else PublicationReadOutcome.NOT_YET_PROVEN
            ),
            "observed_at": observed_at or self.clock.now,
        }
        return PublicationProductReadEvidence(
            **values,
            fingerprint=execution_record_fingerprint("product_read_evidence", values),
        )

    def complete_preflight(self) -> None:
        _, shop_claim = self.claim_shop()
        shop_evidence = self.shop_evidence(shop_claim)
        self.clock.tick()
        _, product_claim = self.claim_product(PublicationCallPurpose.PRODUCT_PREFLIGHT)
        product_evidence = self.product_evidence(product_claim)
        self.clock.tick()
        self.service.record_preflight(
            self.command(
                RecordPublicationPreflightCommand,
                self.next_operation("preflight"),
                shop_evidence=shop_evidence,
                product_evidence=product_evidence,
            )
        )
        self.clock.tick()

    def claim_publish(self, *, audit: bool = True):  # type: ignore[no-untyped-def]
        proof = self.authority.preflight_proof
        assert proof is not None
        result = self.service.claim_publish(
            self.command(
                ClaimPublicationMutationCommand,
                self.next_operation("publish"),
                preflight_proof_id=proof.proof_id,
                preflight_proof_fingerprint=proof.fingerprint,
            )
        )
        claim = self.authority.call_claims[-1]
        if audit:
            self.audit(claim)
        return result, claim

    def publish_evidence(
        self,
        claim: PublicationCallClaim,
        *,
        accepted: bool,
    ) -> PublicationPublishEvidence:
        authority = self.authority
        provider = authority.provider_authority
        mutation = authority.mutation_claim
        assert provider is not None and mutation is not None
        values = {
            "call_claim_id": claim.authorization_id,
            "call_claim_fingerprint": claim.fingerprint,
            "mutation_claim_id": mutation.mutation_claim_id,
            "mutation_claim_fingerprint": mutation.fingerprint,
            "provider_authority_id": provider.provider_authority_id,
            "provider_authority_fingerprint": provider.fingerprint,
            "outcome": (
                PublicationPostOutcome.DEFINITELY_ACCEPTED
                if accepted
                else PublicationPostOutcome.AMBIGUOUS
            ),
            "response_category": (
                PublicationPublishResponseCategory.VALIDATED_2XX
                if accepted
                else PublicationPublishResponseCategory.NON_2XX
            ),
            "sanitized_response_fingerprint": "3" * 64,
            "observed_at": self.clock.now,
        }
        return PublicationPublishEvidence(
            **values,
            fingerprint=execution_record_fingerprint("publish_evidence", values),
        )


def _assert_code(error: pytest.ExceptionInfo[PublicationConflictError], code: str) -> None:
    assert error.value.code.value == code


def _refingerprint(record: Any, kind: str, **updates: object) -> Any:
    values = {
        **record.model_dump(
            mode="python",
            exclude={"contract_version", "fingerprint"},
        ),
        **updates,
    }
    return type(record)(
        **values,
        fingerprint=execution_record_fingerprint(kind, values),
    )


def test_pristine_dispatch_normalizes_same_records_and_replay_is_exact() -> None:
    harness = Harness()
    initial = harness.authority
    command = harness.command(DispatchPublicationWorkCommand, "dispatch_once")

    assert isinstance(initial.expected_aggregate, PublicationAggregate)
    assert harness.store.aggregates[harness.aggregate_id] == harness.transaction.commit.aggregate
    first = harness.service.dispatch_work(command)
    replay = harness.service.dispatch_work(command)
    current = harness.authority

    assert first.receipt == replay.receipt
    assert replay.fresh_call_grant is None
    assert isinstance(current.expected_aggregate, ExecutionPublicationAggregate)
    assert current.aggregate.record_version == 1
    assert current.aggregate.event_sequence == 2
    assert current.work.status is PublicationExecutionWorkStatus.DISPATCHED
    assert current.work.attempt_count == 1
    assert current.snapshot.verification_deadline == initial.snapshot.verification_deadline


def test_provider_authority_is_required_and_reconstructed_from_phase6_records() -> None:
    harness = Harness()
    harness.service.dispatch_work(
        harness.command(DispatchPublicationWorkCommand, "dispatch_authority")
    )
    harness.clock.tick()
    with pytest.raises(PublicationConflictError) as error:
        harness.service.claim_shop_get(
            harness.command(ClaimShopGetCommand, "claim_without_authority")
        )
    _assert_code(error, "PUBLICATION_INVALID_AUTHORITY")

    harness.service.reconstruct_authority(
        harness.command(ReconstructPublicationAuthorityCommand, "reconstruct_authority")
    )
    provider = harness.authority.provider_authority
    source = harness.transaction.authority.product_sync

    assert provider is not None
    assert provider.permit_id == harness.authority.permit.permit_id
    assert provider.work_request_id == harness.authority.work.work_request_id
    assert provider.pricing_fresh_until == harness.authority.snapshot.pricing_fresh_until
    assert tuple(item.variant_id for item in provider.expected_variant_economics) == tuple(
        item.variant_id for item in source.variants
    )
    assert provider.expected_mockup_fingerprints


def test_call_claim_grant_is_fresh_once_and_replay_never_reauthorizes_wire() -> None:
    harness = Harness()
    harness.dispatch_and_reconstruct()
    command = harness.command(ClaimShopGetCommand, "fresh_shop_claim")
    first = harness.service.claim_shop_get(command)
    claim = harness.authority.call_claims[-1]

    assert first.fresh_call_grant is not None
    first.fresh_call_grant.consume_once(claim)
    with pytest.raises(PublicationConflictError):
        first.fresh_call_grant.consume_once(claim)
    replay = harness.service.claim_shop_get(command)
    assert replay.receipt == first.receipt
    assert replay.fresh_call_grant is None
    assert harness.authority.attempt.shop_get_call_count == 1


def test_same_operation_race_mints_one_grant_and_stale_different_operation_loses() -> None:
    harness = Harness()
    harness.dispatch_and_reconstruct()
    same = harness.command(ClaimShopGetCommand, "same_operation_race")

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: harness.service.claim_shop_get(same), range(8)))
    assert sum(result.fresh_call_grant is not None for result in results) == 1
    assert len({result.receipt.fingerprint for result in results}) == 1
    assert harness.authority.attempt.shop_get_call_count == 1

    stale_one = harness.command(ClaimShopGetCommand, "different_race_one")
    stale_two = harness.command(ClaimShopGetCommand, "different_race_two")
    stale_never_submitted = harness.command(ClaimShopGetCommand, "stale_never_submitted")
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(harness.service.claim_shop_get, stale_one),
            pool.submit(harness.service.claim_shop_get, stale_two),
        ]
        outcomes: list[object] = []
        for future in futures:
            try:
                outcomes.append(future.result())
            except PublicationConflictError as error:
                outcomes.append(error)
    assert sum(not isinstance(value, PublicationConflictError) for value in outcomes) == 1
    assert sum(isinstance(value, PublicationConflictError) for value in outcomes) == 1
    assert harness.authority.attempt.shop_get_call_count == 2

    with pytest.raises(PublicationConflictError) as stale_error:
        harness.service.claim_shop_get(stale_never_submitted)
    assert stale_error.value.code.value in {
        "PUBLICATION_STALE_RECORD",
        "PUBLICATION_CONCURRENT_WRITE",
    }


def test_claim_crash_has_no_fabricated_audit_and_audit_replay_increments_once() -> None:
    harness = Harness()
    harness.dispatch_and_reconstruct()
    _, claim = harness.claim_shop(audit=False)

    assert harness.authority.aggregate.provider_audit_record_version == 0
    assert harness.authority.provider_audits == ()
    commit, first = harness.audit(claim)
    replay = harness.store.commit_provider_audit(commit)

    assert replay == first
    assert harness.authority.aggregate.provider_audit_record_version == 1
    assert len(harness.authority.provider_audits) == 1


def test_fixed_get_budgets_are_spent_before_any_wire_authority() -> None:
    shop = Harness()
    shop.dispatch_and_reconstruct()
    for _ in range(3):
        shop.claim_shop(audit=False)
        shop.clock.tick()
    with pytest.raises(PublicationConflictError) as shop_error:
        shop.claim_shop(audit=False)
    _assert_code(shop_error, "PUBLICATION_CALL_BUDGET_EXHAUSTED")
    assert shop.authority.attempt.shop_get_call_count == 3

    product = Harness()
    product.dispatch_and_reconstruct()
    for _ in range(100):
        product.claim_product(PublicationCallPurpose.PRODUCT_PREFLIGHT, audit=False)
        product.clock.tick()
    with pytest.raises(PublicationConflictError) as product_error:
        product.claim_product(PublicationCallPurpose.PRODUCT_PREFLIGHT, audit=False)
    _assert_code(product_error, "PUBLICATION_CALL_BUDGET_EXHAUSTED")
    assert product.authority.attempt.product_get_call_count == 100
    assert product.authority.attempt.record_version == 100


def test_preflight_requires_audited_provider_evidence_and_closes_more_preflight_gets() -> None:
    harness = Harness()
    harness.dispatch_and_reconstruct()
    _, shop_claim = harness.claim_shop(audit=False)
    shop_evidence = harness.shop_evidence(shop_claim)
    harness.clock.tick()
    _, product_claim = harness.claim_product(
        PublicationCallPurpose.PRODUCT_PREFLIGHT,
        audit=False,
    )
    product_evidence = harness.product_evidence(product_claim)
    harness.clock.tick()
    command = harness.command(
        RecordPublicationPreflightCommand,
        "unaudited_preflight",
        shop_evidence=shop_evidence,
        product_evidence=product_evidence,
    )
    with pytest.raises(PublicationConflictError) as error:
        harness.service.record_preflight(command)
    _assert_code(error, "PUBLICATION_INVALID_AUTHORITY")

    harness.audit(shop_claim)
    harness.audit(product_claim)
    harness.service.record_preflight(command)
    with pytest.raises(PublicationConflictError):
        harness.claim_shop()
    with pytest.raises(PublicationConflictError):
        harness.claim_product(PublicationCallPurpose.PRODUCT_PREFLIGHT)


def test_publish_claim_consumes_permit_atomically_and_never_replays_a_grant() -> None:
    harness = Harness()
    harness.dispatch_and_reconstruct()
    harness.complete_preflight()
    command = harness.command(
        ClaimPublicationMutationCommand,
        "publish_exact_once",
        preflight_proof_id=harness.authority.preflight_proof.proof_id,
        preflight_proof_fingerprint=harness.authority.preflight_proof.fingerprint,
    )
    first = harness.service.claim_publish(command)
    authority = harness.authority
    claim = authority.call_claims[-1]
    mutation = authority.mutation_claim

    assert isinstance(first.fresh_call_grant, FreshPublicationMutationGrant)
    assert mutation is not None
    assert authority.permit.status is PublicationPermitState.CONSUMED
    assert authority.permit.consumed_at == mutation.authorized_at == claim.authorized_at
    assert authority.attempt.publish_post_call_count == 1
    first.fresh_call_grant.consume_once(claim, mutation)
    with pytest.raises(PublicationConflictError):
        first.fresh_call_grant.consume_once(claim, mutation)
    replay = harness.service.claim_publish(command)
    assert replay.fresh_call_grant is None
    assert harness.authority.attempt.publish_post_call_count == 1


def test_pricing_expiry_after_preflight_never_consumes_the_permit() -> None:
    harness = Harness(short_pricing_window=True)
    harness.dispatch_and_reconstruct()
    harness.complete_preflight()
    proof = harness.authority.preflight_proof
    assert proof is not None
    harness.clock.now = harness.authority.snapshot.pricing_fresh_until

    with pytest.raises(PublicationConflictError) as error:
        harness.service.claim_publish(
            harness.command(
                ClaimPublicationMutationCommand,
                "stale_pricing_publish",
                preflight_proof_id=proof.proof_id,
                preflight_proof_fingerprint=proof.fingerprint,
            )
        )
    _assert_code(error, "PUBLICATION_PRICING_NOT_FRESH")
    assert harness.authority.permit.status is PublicationPermitState.AVAILABLE
    assert harness.authority.attempt.publish_post_call_count == 0

    harness.clock.now = harness.authority.snapshot.verification_deadline
    harness.service.settle_deadline(
        harness.command(SettlePublicationDeadlineCommand, "expire_pre_call")
    )
    terminal = harness.authority
    assert terminal.aggregate.state is PublicationState.PUBLICATION_FAILED
    assert terminal.permit.status is PublicationPermitState.RETIRED
    assert terminal.report.terminal_reason is PublicationTerminalReason.PRE_CALL_DEADLINE_EXPIRED


def test_pre_dispatch_deadline_retires_pristine_work_with_zero_calls_and_replays() -> None:
    harness = Harness(capture_commits=True)
    harness.clock.now = harness.authority.snapshot.verification_deadline
    command = harness.command(SettlePublicationDeadlineCommand, "expire_before_dispatch")

    first = harness.service.settle_deadline(command)
    replay = harness.service.settle_deadline(command)
    terminal = harness.authority

    assert replay.receipt == first.receipt
    assert terminal.aggregate.state is PublicationState.PUBLICATION_FAILED
    assert terminal.permit.status is PublicationPermitState.RETIRED
    assert terminal.attempt.shop_get_call_count == 0
    assert terminal.attempt.product_get_call_count == 0
    assert terminal.attempt.publish_post_call_count == 0
    assert terminal.attempt.record_version == 0
    assert terminal.work.status is PublicationExecutionWorkStatus.FAILED
    assert terminal.work.attempt_count == 0
    assert terminal.work.dispatched_at is None
    assert terminal.work.next_dispatch_at is None
    assert terminal.report.terminal_reason is PublicationTerminalReason.PRE_CALL_DEADLINE_EXPIRED
    assert terminal.terminal_job_link.result_record_version == (
        terminal.terminal_job_link.expected_record_version + 1
    )
    assert harness.store.jobs[terminal.snapshot.job_id].publication_terminal_state == (
        PublicationState.PUBLICATION_FAILED.value
    )

    assert isinstance(harness.store, CapturingExecutionStore)
    valid = harness.store.last_commit
    assert valid is not None
    forged_work = _refingerprint(
        valid.updated_work,
        "execution_work",
        attempt_count=1,
        dispatched_at=valid.event.occurred_at,
    )
    dispatch_tamper = PublicationExecutionCommit.model_validate(
        {
            **valid.model_dump(mode="python"),
            "updated_work": forged_work,
        }
    )
    with pytest.raises(PublicationConflictError) as dispatch_error:
        validate_execution_commit(dispatch_tamper)
    _assert_code(dispatch_error, "PUBLICATION_INVALID_AUTHORITY")

    assert valid.new_report is not None
    assert valid.new_tombstone is not None
    assert valid.terminal_job_update is not None
    early_permit = _refingerprint(
        valid.updated_permit,
        "execution_permit",
        retired_at=valid.event.occurred_at - timedelta(minutes=1),
    )
    early_report = _refingerprint(
        valid.new_report,
        "terminal_report",
        permit_fingerprint=early_permit.fingerprint,
    )
    early_tombstone = _refingerprint(
        valid.new_tombstone,
        "aggregate_tombstone",
        report_fingerprint=early_report.fingerprint,
    )
    early_link = _refingerprint(
        valid.terminal_job_update.link,
        "terminal_job_link",
        report_fingerprint=early_report.fingerprint,
    )
    early_job_update = type(valid.terminal_job_update)(
        expected_job=valid.terminal_job_update.expected_job,
        updated_job=valid.terminal_job_update.updated_job,
        link=early_link,
    )
    early_event = _refingerprint(
        valid.event,
        "execution_event",
        authority_fingerprint=early_report.fingerprint,
    )
    early_receipt = _refingerprint(
        valid.receipt,
        "execution_receipt",
        authority_fingerprint=early_report.fingerprint,
    )
    retirement_time_tamper = PublicationExecutionCommit.model_validate(
        {
            **valid.model_dump(mode="python"),
            "updated_permit": early_permit,
            "new_report": early_report,
            "new_tombstone": early_tombstone,
            "terminal_job_update": early_job_update,
            "event": early_event,
            "receipt": early_receipt,
        }
    )
    with pytest.raises(PublicationConflictError) as retirement_error:
        validate_execution_commit(retirement_time_tamper)
    _assert_code(retirement_error, "PUBLICATION_INVALID_AUTHORITY")


@pytest.mark.parametrize("audit_before_recovery", [False, True])
def test_consumed_claim_crash_reconciles_before_or_after_boundary_audit(
    audit_before_recovery: bool,
) -> None:
    harness = Harness()
    harness.dispatch_and_reconstruct()
    harness.complete_preflight()
    publish_result, claim = harness.claim_publish(audit=audit_before_recovery)
    mutation = harness.authority.mutation_claim
    assert mutation is not None and publish_result.fresh_call_grant is not None

    harness.clock.tick()
    command = harness.command(
        RecoverConsumedPublicationClaimCommand,
        f"recover_{audit_before_recovery}",
        mutation_claim_id=mutation.mutation_claim_id,
        mutation_claim_fingerprint=mutation.fingerprint,
    )
    harness.service.recover_consumed_claim(command)
    recovered = harness.authority

    assert recovered.aggregate.state is PublicationState.PUBLICATION_RECONCILING
    assert recovered.post_observation.outcome is PublicationPostOutcome.AMBIGUOUS
    assert (
        recovered.post_observation.response_category
        is PublicationPublishResponseCategory.CONSUMED_CLAIM_WITHOUT_DURABLE_BOUNDARY_OBSERVATION
    )
    assert recovered.post_observation.provider_evidence_fingerprint is None
    assert recovered.attempt.publish_post_call_count == 1
    replay = harness.service.recover_consumed_claim(command)
    assert replay.fresh_call_grant is None

    if not audit_before_recovery:
        with pytest.raises(PublicationConflictError):
            harness.audit(claim)


def test_accepted_post_and_positive_read_settle_safe_published_result() -> None:
    harness = Harness()
    harness.dispatch_and_reconstruct()
    harness.complete_preflight()
    _, post_claim = harness.claim_publish()
    publish_evidence = harness.publish_evidence(post_claim, accepted=True)
    post_observed_at = publish_evidence.observed_at
    harness.clock.tick(5)
    harness.service.record_post_outcome(
        harness.command(
            RecordPublicationPostOutcomeCommand,
            "accepted_post",
            evidence=publish_evidence,
        )
    )
    assert harness.authority.aggregate.state is PublicationState.PUBLICATION_VERIFYING
    assert harness.authority.post_observation.observed_at == post_observed_at

    harness.clock.tick()
    _, verification_claim = harness.claim_product(PublicationCallPurpose.VERIFICATION)
    positive = harness.product_evidence(verification_claim, positive=True)
    proof_observed_at = positive.observed_at
    harness.clock.tick(5)
    settlement_time = harness.clock.now
    harness.service.record_product_observation(
        harness.command(
            RecordPublicationProductObservationCommand,
            "positive_product",
            evidence=positive,
        )
    )
    terminal = harness.authority

    assert terminal.aggregate.state is PublicationState.PUBLISHED
    assert terminal.aggregate.terminal_at == settlement_time
    assert terminal.last_product_observation.observed_at == proof_observed_at
    assert terminal.result.verified_at == proof_observed_at
    assert terminal.notification.created_at == settlement_time
    assert terminal.result.safe_listing_url == "https://www.etsy.com/listing/123456789"
    assert terminal.result.verified_product_fingerprint == positive.fingerprint
    assert terminal.aggregate.source_release_eligible_at == settlement_time + timedelta(days=30)
    assert terminal.aggregate.operational_expires_at == settlement_time + timedelta(days=90)
    assert terminal.phase6_record_version == terminal.terminal_job_link.result_record_version
    assert terminal.report.sanitized_audit_record_digests == tuple(
        binding.audit_record.fingerprint for binding in terminal.provider_audits
    )
    assert terminal.aggregate.provider_audit_record_version == len(terminal.provider_audits)
    assert terminal.report.result_fingerprint == terminal.result.fingerprint


def test_ambiguous_post_and_nonpositive_read_settle_unknown_at_fixed_deadline() -> None:
    harness = Harness()
    harness.dispatch_and_reconstruct()
    harness.complete_preflight()
    _, post_claim = harness.claim_publish()
    evidence = harness.publish_evidence(post_claim, accepted=False)
    harness.clock.tick()
    harness.service.record_post_outcome(
        harness.command(
            RecordPublicationPostOutcomeCommand,
            "ambiguous_post",
            evidence=evidence,
        )
    )
    assert harness.authority.aggregate.state is PublicationState.PUBLICATION_RECONCILING

    harness.clock.tick()
    _, read_claim = harness.claim_product(PublicationCallPurpose.RECONCILIATION)
    read = harness.product_evidence(read_claim)
    harness.clock.now = harness.authority.snapshot.verification_deadline
    harness.service.record_product_observation(
        harness.command(
            RecordPublicationProductObservationCommand,
            "deadline_nonpositive",
            evidence=read,
        )
    )
    terminal = harness.authority

    assert terminal.aggregate.state is PublicationState.PUBLICATION_OUTCOME_UNKNOWN
    assert terminal.result is None
    assert terminal.notification is None
    assert (
        terminal.report.terminal_reason
        is PublicationTerminalReason.FIXED_DEADLINE_WITHOUT_POSITIVE_PROOF
    )
    assert terminal.aggregate.terminal_at == terminal.snapshot.verification_deadline


def test_consumed_requested_state_cannot_skip_recovery_at_deadline() -> None:
    harness = Harness()
    harness.dispatch_and_reconstruct()
    harness.complete_preflight()
    harness.claim_publish(audit=False)
    harness.clock.now = harness.authority.snapshot.verification_deadline

    with pytest.raises(PublicationConflictError) as error:
        harness.service.settle_deadline(
            harness.command(SettlePublicationDeadlineCommand, "skip_recovery")
        )
    _assert_code(error, "PUBLICATION_INVALID_TRANSITION")
    assert harness.authority.aggregate.state is PublicationState.PUBLICATION_REQUESTED


def test_boundary_evidence_without_matching_audit_fails_closed() -> None:
    harness = Harness()
    harness.dispatch_and_reconstruct()
    harness.complete_preflight()
    _, post_claim = harness.claim_publish(audit=False)
    evidence = harness.publish_evidence(post_claim, accepted=True)
    harness.clock.tick()

    with pytest.raises(PublicationConflictError) as error:
        harness.service.record_post_outcome(
            harness.command(
                RecordPublicationPostOutcomeCommand,
                "unaudited_post_evidence",
                evidence=evidence,
            )
        )
    _assert_code(error, "PUBLICATION_INVALID_AUTHORITY")
    assert harness.authority.aggregate.state is PublicationState.PUBLICATION_REQUESTED


def test_product_observation_order_follows_settlement_not_claim_or_clock_order() -> None:
    harness = Harness()
    harness.dispatch_and_reconstruct()
    harness.complete_preflight()
    _, post_claim = harness.claim_publish()
    accepted = harness.publish_evidence(post_claim, accepted=True)
    harness.clock.tick()
    harness.service.record_post_outcome(
        harness.command(RecordPublicationPostOutcomeCommand, "verify_state", evidence=accepted)
    )
    harness.clock.tick()
    _, claim_one = harness.claim_product(PublicationCallPurpose.VERIFICATION)
    _, claim_two = harness.claim_product(PublicationCallPurpose.VERIFICATION)
    equal_time = harness.clock.now
    evidence_one = harness.product_evidence(claim_one, observed_at=equal_time)
    evidence_two = harness.product_evidence(claim_two, observed_at=equal_time)

    harness.service.record_product_observation(
        harness.command(
            RecordPublicationProductObservationCommand,
            "response_two_first",
            evidence=evidence_two,
        )
    )
    harness.service.record_product_observation(
        harness.command(
            RecordPublicationProductObservationCommand,
            "response_one_second",
            evidence=evidence_one,
        )
    )
    reloaded = harness.authority

    assert tuple(item.call_claim_id for item in reloaded.product_observations) == (
        claim_two.authorization_id,
        claim_one.authorization_id,
    )
    assert reloaded.last_product_observation.call_claim_id == claim_one.authorization_id
    assert reloaded.product_observations[0].observed_at == equal_time
    assert reloaded.product_observations[1].observed_at == equal_time
    assert (
        reloaded.product_observations[0].resulting_aggregate_record_version
        < reloaded.product_observations[1].resulting_aggregate_record_version
    )


def test_positive_evidence_at_exact_deadline_is_expired() -> None:
    harness = Harness()
    harness.dispatch_and_reconstruct()
    harness.complete_preflight()
    _, post_claim = harness.claim_publish()
    accepted = harness.publish_evidence(post_claim, accepted=True)
    harness.clock.tick()
    harness.service.record_post_outcome(
        harness.command(
            RecordPublicationPostOutcomeCommand, "accepted_before_deadline", evidence=accepted
        )
    )
    _, claim = harness.claim_product(PublicationCallPurpose.VERIFICATION)
    harness.clock.now = harness.authority.snapshot.verification_deadline
    exact_deadline = harness.product_evidence(
        claim,
        positive=True,
        observed_at=harness.clock.now,
    )

    with pytest.raises(PublicationConflictError) as error:
        harness.service.record_product_observation(
            harness.command(
                RecordPublicationProductObservationCommand,
                "proof_at_deadline",
                evidence=exact_deadline,
            )
        )
    _assert_code(error, "PUBLICATION_DEADLINE_EXPIRED")


def test_terminal_vs_audit_race_never_omits_a_committed_audit_digest() -> None:
    harness = Harness()
    harness.dispatch_and_reconstruct()
    _, claim = harness.claim_shop(audit=False)
    audit_commit = build_provider_audit_commit(
        harness.authority,
        claim,
        PublicationProviderAuditRecord(
            decision=PublicationProviderAuditDecision.ALLOWED,
            method_category="GET",
            route_template="/v1/shops.json",
            category=PublicationProviderAuditCategory.SHOP_GET_ALLOWED,
            fingerprint=execution_record_fingerprint(
                "provider_audit_record",
                {
                    "decision": PublicationProviderAuditDecision.ALLOWED,
                    "method_category": "GET",
                    "route_template": "/v1/shops.json",
                    "category": PublicationProviderAuditCategory.SHOP_GET_ALLOWED,
                },
            ),
        ),
    )
    harness.clock.now = harness.authority.snapshot.verification_deadline
    settle = harness.command(SettlePublicationDeadlineCommand, "terminal_audit_race")
    barrier = Barrier(2)

    def append_audit() -> str:
        barrier.wait()
        try:
            harness.store.commit_provider_audit(audit_commit)
            return "audit"
        except PublicationConflictError:
            return "audit_lost"

    def settle_terminal() -> str:
        barrier.wait()
        try:
            harness.service.settle_deadline(settle)
            return "terminal"
        except PublicationConflictError:
            return "terminal_lost"

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = {pool.submit(append_audit), pool.submit(settle_terminal)}
        results = {future.result() for future in outcomes}
    if harness.authority.aggregate.terminal_at is None:
        harness.service.settle_deadline(
            harness.command(SettlePublicationDeadlineCommand, "terminal_retry")
        )
    terminal = harness.authority

    assert results in ({"audit", "terminal_lost"}, {"audit_lost", "terminal"})
    assert terminal.report.sanitized_audit_record_digests == tuple(
        binding.audit_record.fingerprint for binding in terminal.provider_audits
    )
    assert terminal.aggregate.provider_audit_record_version == len(terminal.provider_audits)


def test_store_rejects_forged_operation_kind_time_and_dispatch_metadata() -> None:
    harness = Harness(capture_commits=True)
    harness.dispatch_and_reconstruct()
    harness.service.claim_shop_get(harness.command(ClaimShopGetCommand, "captured_shop_claim"))
    assert isinstance(harness.store, CapturingExecutionStore)
    valid = harness.store.last_commit
    assert valid is not None and valid.new_call_claim is not None

    forged_work = _refingerprint(
        valid.updated_work,
        "execution_work",
        dispatched_at=valid.updated_work.dispatched_at + timedelta(microseconds=1),
    )
    dispatch_tamper = PublicationExecutionCommit.model_validate(
        {
            **valid.model_dump(mode="python"),
            "updated_work": forged_work,
        }
    )
    with pytest.raises(PublicationConflictError) as dispatch_error:
        validate_execution_commit(dispatch_tamper)
    _assert_code(dispatch_error, "PUBLICATION_INVALID_AUTHORITY")

    original_claim = valid.new_call_claim
    product_claim = _refingerprint(
        original_claim,
        "call_claim",
        call_kind=PublicationCallKind.PRODUCT_GET,
        route_template="/v1/shops/{shop_id}/products/{product_id}.json",
        purpose=PublicationCallPurpose.PRODUCT_PREFLIGHT,
        printify_product_id=valid.expected.snapshot.printify_product_id,
        call_limit=100,
    )
    product_attempt = _refingerprint(
        valid.updated_attempt,
        "execution_attempt",
        shop_get_call_count=0,
        product_get_call_count=1,
    )
    product_event = _refingerprint(
        valid.event,
        "execution_event",
        authority_fingerprint=product_claim.fingerprint,
    )
    product_receipt = _refingerprint(
        valid.receipt,
        "execution_receipt",
        authority_fingerprint=product_claim.fingerprint,
    )
    kind_tamper = PublicationExecutionCommit.model_validate(
        {
            **valid.model_dump(mode="python"),
            "updated_attempt": product_attempt,
            "new_call_claim": product_claim,
            "event": product_event,
            "receipt": product_receipt,
        }
    )
    with pytest.raises(PublicationConflictError) as kind_error:
        validate_execution_commit(kind_tamper)
    _assert_code(kind_error, "PUBLICATION_INVALID_AUTHORITY")

    future_claim = _refingerprint(
        original_claim,
        "call_claim",
        authorized_at=original_claim.authorized_at + timedelta(seconds=1),
    )
    future_event = _refingerprint(
        valid.event,
        "execution_event",
        authority_fingerprint=future_claim.fingerprint,
    )
    future_receipt = _refingerprint(
        valid.receipt,
        "execution_receipt",
        authority_fingerprint=future_claim.fingerprint,
    )
    future_tamper = PublicationExecutionCommit.model_validate(
        {
            **valid.model_dump(mode="python"),
            "new_call_claim": future_claim,
            "event": future_event,
            "receipt": future_receipt,
        }
    )
    with pytest.raises(PublicationConflictError) as future_error:
        validate_execution_commit(future_tamper)
    _assert_code(future_error, "PUBLICATION_INVALID_AUTHORITY")


def test_store_rejects_fully_refingerprinted_get_state_rollback() -> None:
    harness = Harness(capture_commits=True)
    harness.dispatch_and_reconstruct()
    harness.complete_preflight()
    _, publish_claim = harness.claim_publish()
    evidence = harness.publish_evidence(publish_claim, accepted=True)
    harness.clock.tick()
    harness.service.record_post_outcome(
        harness.command(
            RecordPublicationPostOutcomeCommand,
            "enter_verifying_for_rollback",
            evidence=evidence,
        )
    )
    harness.clock.tick()
    harness.claim_product(PublicationCallPurpose.VERIFICATION, audit=False)

    assert isinstance(harness.store, CapturingExecutionStore)
    valid = harness.store.last_commit
    assert valid is not None and valid.new_call_claim is not None
    current = valid.expected
    shop_claim = _refingerprint(
        valid.new_call_claim,
        "call_claim",
        call_kind=PublicationCallKind.SHOP_GET,
        route_template="/v1/shops.json",
        purpose=PublicationCallPurpose.SHOP_PREFLIGHT,
        printify_product_id=None,
        ordinal=current.attempt.shop_get_call_count + 1,
        call_limit=current.attempt.shop_get_call_limit,
    )
    shop_attempt = _refingerprint(
        valid.updated_attempt,
        "execution_attempt",
        shop_get_call_count=current.attempt.shop_get_call_count + 1,
        product_get_call_count=current.attempt.product_get_call_count,
    )
    rolled_aggregate = _refingerprint(
        valid.updated_aggregate,
        "execution_aggregate",
        state=PublicationState.PUBLICATION_REQUESTED,
    )
    rolled_work = _refingerprint(
        valid.updated_work,
        "execution_work",
        status=PublicationExecutionWorkStatus.DISPATCHED,
    )
    rolled_event = _refingerprint(
        valid.event,
        "execution_event",
        state=PublicationState.PUBLICATION_REQUESTED,
        authority_fingerprint=shop_claim.fingerprint,
    )
    rolled_receipt = _refingerprint(
        valid.receipt,
        "execution_receipt",
        operation=PublicationExecutionOperation.CLAIM_SHOP_GET,
        aggregate_state=PublicationState.PUBLICATION_REQUESTED,
        authority_fingerprint=shop_claim.fingerprint,
    )
    rollback = PublicationExecutionCommit.model_validate(
        {
            **valid.model_dump(mode="python"),
            "updated_aggregate": rolled_aggregate,
            "updated_attempt": shop_attempt,
            "updated_work": rolled_work,
            "new_call_claim": shop_claim,
            "event": rolled_event,
            "receipt": rolled_receipt,
        }
    )
    with pytest.raises(PublicationConflictError) as rollback_error:
        validate_execution_commit(rollback)
    _assert_code(rollback_error, "PUBLICATION_INVALID_AUTHORITY")


def test_models_reject_raw_tamper_and_nonpositive_listing_identity() -> None:
    harness = Harness()
    harness.dispatch_and_reconstruct()
    _, claim = harness.claim_product(PublicationCallPurpose.PRODUCT_PREFLIGHT, audit=False)
    evidence = harness.product_evidence(claim)
    with pytest.raises(ValidationError):
        PublicationProductReadEvidence.model_validate(
            {
                **evidence.model_dump(mode="python"),
                "numeric_listing_id": 999,
            }
        )
    with pytest.raises(ValidationError):
        PublicationProviderAuditRecord(
            decision=PublicationProviderAuditDecision.REJECTED,
            method_category="GET",
            route_template="/v1/shops.json",
            category=PublicationProviderAuditCategory.FORBIDDEN_ROUTE,
            fingerprint="0" * 64,
        )
