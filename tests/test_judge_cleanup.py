from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import RLock
from unittest.mock import Mock

import pytest

from mr_lister.judge_cleanup.models import CleanupConfig, CleanupRecord, PublicationEvidence
from mr_lister.judge_cleanup.service import (
    CleanupRunError,
    DiscoveryPage,
    JudgeCleanupService,
    ProviderMismatchError,
)
from mr_lister.publication.execution_commands import (
    RecordPublicationPostOutcomeCommand,
    RecordPublicationProductObservationCommand,
)
from mr_lister.publication.execution_models import PublicationCallPurpose
from tests.test_phase72_publication_execution import Harness

SECRET_ARN = "arn:aws:secretsmanager:us-west-2:123456789012:secret:mr-lister/test/owner-AbCdEf"


def finish_publication(harness):
    harness.dispatch_and_reconstruct()
    harness.complete_preflight()
    _, claim = harness.claim_publish()
    harness.service.record_post_outcome(
        harness.command(
            RecordPublicationPostOutcomeCommand,
            "cleanup_post",
            evidence=harness.publish_evidence(claim, accepted=True),
        )
    )
    _, claim = harness.claim_product(PublicationCallPurpose.VERIFICATION)
    harness.service.record_product_observation(
        harness.command(
            RecordPublicationProductObservationCommand,
            "cleanup_positive",
            evidence=harness.product_evidence(claim, positive=True),
        )
    )
    authority = harness.authority
    return PublicationEvidence(
        job=harness.store.load_linked_job(authority.snapshot.owner_id, harness.aggregate_id),
        aggregate=authority.aggregate,
        snapshot=authority.snapshot,
        provider=authority.provider_authority,
        observation=authority.last_product_observation,
        result=authority.result,
    )


@pytest.fixture
def evidence():
    return finish_publication(Harness())


def config_for(evidence, *, active=True, **changes):
    config = CleanupConfig(
        campaign_id="judge-demo",
        judge_owner_id=evidence.job.owner_id,
        primary_owner_id="b" * 64,
        printify_shop_id=evidence.snapshot.printify_shop_id,
        campaign_started_at=evidence.snapshot.requested_at,
        source_table_name="source-table",
        cleanup_table_name="cleanup-table",
        printify_secret_arn=SECRET_ARN,
        **changes,
    )
    if active:
        return CleanupConfig.model_validate(
            {
                **config.model_dump(),
                "dry_run": False,
                "activation_fingerprint": config.campaign_fingerprint,
            }
        )
    return config


def record_for(evidence, config):
    return CleanupRecord(
        campaign_id=config.campaign_id,
        campaign_fingerprint=config.campaign_fingerprint,
        evidence=evidence,
        evidence_fingerprint=evidence.fingerprint,
        deadline=evidence.deadline,
        next_attempt_at=evidence.deadline,
        updated_at=evidence.result.verified_at,
    )


class MemoryStore:
    def __init__(self):
        self.records, self.audit = {}, []
        self.lock = RLock()
        self.saved_cursor = None

    def get(self, job_id):
        with self.lock:
            return self.records.get(job_id)

    def register(self, record):
        with self.lock:
            if record.evidence.job.job_id in self.records:
                return False
            self.records[record.evidence.job.job_id] = record
            self.audit.append(record)
            return True

    def compare_and_swap(self, old, new):
        with self.lock:
            if self.records.get(old.evidence.job.job_id) != old:
                return False
            self.records[old.evidence.job.job_id] = new
            self.audit.append(new)
            return True

    def due(self, now, limit):
        return tuple(
            r
            for r in self.records.values()
            if r.status in {"pending", "retry", "leased"} and r.next_attempt_at <= now
        )[:limit]

    def cursor(self):
        return self.saved_cursor

    def advance_cursor(self, old, new):
        if self.saved_cursor == old:
            self.saved_cursor = new

    def discovery_failure(self, job_id, now):
        self.audit.append((job_id, now, "source_unavailable"))


def setup_service(evidence, *, active=True):
    config = config_for(evidence, active=active)
    store = MemoryStore()
    source = Mock()
    source.load.return_value = evidence
    source.discover.return_value = DiscoveryPage((evidence.job.job_id,), None)
    provider = Mock()
    provider.present.side_effect = [True, False]
    now = [evidence.deadline]
    service = JudgeCleanupService(
        config=config, source=source, store=store, provider=provider, clock=lambda: now[0]
    )
    record = record_for(evidence, config)
    store.register(record)
    return service, store, source, provider, now, record


def test_configuration_requires_explicit_exact_activation_and_distinct_tables_owners(evidence):
    config = config_for(evidence, active=False)
    assert config.dry_run
    for changes in [
        {"dry_run": False},
        {"activation_fingerprint": "0" * 64},
        {"judge_owner_id": config.primary_owner_id},
        {"cleanup_table_name": config.source_table_name},
        {"printify_shop_id": True},
        {"max_jobs_per_run": 4},
        {"dry_run": "false"},
    ]:
        with pytest.raises(ValueError):
            CleanupConfig.model_validate({**config.model_dump(), **changes})


def test_dry_run_keeps_provider_untouched_and_registers_fixed_deadline(evidence):
    service, store, source, provider, now, record = setup_service(evidence, active=False)
    store.records.clear()
    assert service.sweep()["registered"] == 1
    assert service.sweep()["due"] == 1
    assert store.get(evidence.job.job_id).deadline == evidence.result.verified_at + timedelta(
        seconds=1800
    )
    assert provider.mock_calls == []


def test_old_judge_draft_newly_published_is_eligible_but_old_publication_is_not(evidence):
    assert evidence.job.created_at < evidence.snapshot.requested_at
    evidence.require_campaign(config_for(evidence))
    config = config_for(evidence, active=False)
    later = CleanupConfig.model_validate(
        {
            **config.model_dump(),
            "campaign_started_at": evidence.snapshot.requested_at + timedelta(seconds=1),
        }
    )
    with pytest.raises(ValueError):
        evidence.require_campaign(later)
    service, store, source, provider, now, record = setup_service(evidence, active=False)
    service.config = later
    store.records.clear()
    assert service.sweep()["registered"] == 0


@pytest.mark.parametrize("changes", [{"judge_owner_id": "c" * 64}, {"printify_shop_id": 1}])
def test_wrong_owner_or_shop_never_reaches_provider(evidence, changes):
    service, store, source, provider, now, record = setup_service(evidence)
    config = config_for(evidence, active=False)
    service.config = CleanupConfig.model_validate({**config.model_dump(), **changes})
    with pytest.raises((ValueError, CleanupRunError)):
        service.process(record)
    assert provider.mock_calls == []


def test_preparation_and_tampered_publication_cannot_become_cleanup_evidence(evidence):
    for changes in [
        {"publication_terminal_state": "publication_failed"},
        {"owner_id": "c" * 64},
        {"product_id": "different_product"},
    ]:
        raw = evidence.model_dump(mode="json")
        raw["job"].update(changes)
        with pytest.raises(ValueError):
            PublicationEvidence.model_validate_json(json.dumps(raw))
    service, store, source, provider, now, record = setup_service(evidence)
    source.load.return_value = evidence.model_copy(
        update={"job": evidence.job.model_copy(update={"product_id": "other"})}
    )
    assert service.process(record) == "blocked"
    assert provider.mock_calls == []


def test_exact_deadline_then_delete_and_readback_is_idempotent(evidence):
    service, store, source, provider, now, record = setup_service(evidence)
    now[0] -= timedelta(microseconds=1)
    assert service.process(record) == "skipped"
    assert provider.mock_calls == []
    now[0] += timedelta(microseconds=1)
    assert service.process(record) == "deleted"
    assert [c[0] for c in provider.mock_calls] == ["present", "delete", "present"]
    final = store.get(evidence.job.job_id)
    assert final.evidence == evidence and final.deadline == record.deadline
    assert final.etsy_removal_verified is False
    assert [r.last_outcome for r in store.audit] == ["registered", "claimed", "provider_deleted"]
    assert service.process(final) == "skipped"
    assert service.process(record) == "skipped"
    provider.delete.assert_called_once_with(evidence)


def test_concurrent_sweeps_authorize_only_one_delete(evidence):
    service, store, source, provider, now, record = setup_service(evidence)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: service.process(record), range(2)))
    assert sorted(results) == ["deleted", "skipped"]
    provider.delete.assert_called_once()


def test_source_change_after_provider_get_blocks_delete(evidence):
    service, store, source, provider, now, record = setup_service(evidence)
    source.load.side_effect = [evidence, ValueError("private source mismatch")]
    assert service.process(record) == "blocked"
    provider.delete.assert_not_called()
    assert "private" not in store.get(evidence.job.job_id).model_dump_json()


def test_source_change_is_rechecked_even_with_an_unchanged_product(evidence):
    service, store, source, provider, now, record = setup_service(evidence)
    changed_job = evidence.job.model_copy(
        update={"record_version": evidence.job.record_version + 1}
    )
    source.load.side_effect = [evidence, evidence.model_copy(update={"job": changed_job})]
    assert service.process(record) == "blocked"
    provider.delete.assert_not_called()


def test_missing_product_never_issues_delete_or_claims_etsy_verification(evidence):
    service, store, source, provider, now, record = setup_service(evidence)
    provider.present.side_effect = [False]
    assert service.process(record) == "absent"
    provider.delete.assert_not_called()
    assert store.get(evidence.job.job_id).etsy_removal_verified is False


def test_ambiguous_delete_retries_by_get_without_repeating_successful_deletion(evidence):
    service, store, source, provider, now, record = setup_service(evidence)
    provider.present.side_effect = [True, False]
    provider.delete.side_effect = RuntimeError("secret-token must not be retained")
    assert service.process(record) == "retry"
    retry = store.get(evidence.job.job_id)
    assert retry.deadline == record.deadline and retry.attempts == 1
    assert "secret-token" not in retry.model_dump_json()
    assert service.process(retry) == "skipped"
    now[0] = retry.next_attempt_at
    assert service.process(retry) == "absent"
    provider.delete.assert_called_once()


def test_expired_lease_can_be_recovered_but_live_lease_cannot(evidence):
    service, store, source, provider, now, record = setup_service(evidence)
    leased = service._change(
        record,
        status="leased",
        attempts=1,
        lease_id="lost-worker",
        lease_until=now[0] + timedelta(seconds=90),
        next_attempt_at=now[0] + timedelta(seconds=90),
        updated_at=now[0],
        last_outcome="claimed",
    )
    store.compare_and_swap(record, leased)
    assert service.process(leased) == "skipped"
    now[0] = leased.lease_until
    assert service.process(leased) == "deleted"
    assert store.get(evidence.job.job_id).attempts == 2


def test_lost_lease_before_delete_prevents_mutation(evidence):
    service, store, source, provider, now, record = setup_service(evidence)
    original_get = store.get
    store.get = lambda _: record
    assert service.process(record) == "retry"
    provider.delete.assert_not_called()
    store.get = original_get


def test_failure_is_durable_then_surfaces_for_alarm(evidence):
    service, store, source, provider, now, record = setup_service(evidence)
    provider.present.side_effect = ProviderMismatchError("do not copy provider detail")
    with pytest.raises(CleanupRunError, match="recorded a failure"):
        service.sweep()
    assert store.get(evidence.job.job_id).status == "blocked"
    assert store.audit[-1].last_outcome == "provider_mismatch"


def test_retry_limit_is_finite_and_insufficient_time_never_deletes(evidence):
    service, store, source, provider, now, record = setup_service(evidence)
    provider.present.side_effect = RuntimeError("offline")
    for attempt in range(8):
        assert service.process(record) == ("exhausted" if attempt == 7 else "retry")
        record = store.get(evidence.job.job_id)
        now[0] = record.next_attempt_at
    assert record.attempts == 8 and service.process(record) == "skipped"
    provider.delete.assert_not_called()
    service.remaining = lambda: 10
    assert service.sweep()["due"] == 0
