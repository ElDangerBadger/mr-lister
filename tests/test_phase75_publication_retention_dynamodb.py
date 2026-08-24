"""Offline bounded-adversarial tests for Phase 7.5 operational retention."""

from __future__ import annotations

import copy
from typing import Any

import pytest
from test_phase71_publication_dynamodb import TABLE_NAME
from test_phase71_publication_store import OWNER_ID
from test_phase72_publication_execution import Harness
from test_phase73_publication_execution_dynamodb import (
    ExecutionMemoryDynamoClient,
    _dynamo_harness,
)

from mr_lister.control.publication_retention import (
    PUBLICATION_RETENTION_SORT_KEY,
    publication_operational_expiry_epoch,
)
from mr_lister.publication.execution_commands import (
    RecordPublicationPostOutcomeCommand,
    RecordPublicationPreflightCommand,
    RecordPublicationProductObservationCommand,
    SettlePublicationDeadlineCommand,
)
from mr_lister.publication.execution_models import PublicationCallPurpose
from mr_lister.publication.retention import (
    PublicationOperationalRetentionService,
    PublicationRetentionBoundaryInvalidError,
    PublicationRetentionConflictError,
    PublicationRetentionDependencyUnavailableError,
)
from mr_lister.publication.retention_dynamodb import (
    MAX_RETENTION_ASSIGNMENTS,
    MAX_RETENTION_INITIAL_ADDITIONAL_ITEMS,
    MAX_RETENTION_PARTITION_ITEMS,
    MAX_RETENTION_TRANSACTION_ITEMS,
    DynamoDBPublicationOperationalRetentionStore,
)


class RetentionMemoryDynamoClient(ExecutionMemoryDynamoClient):
    """Execution fake extended only for exact-item TTL puts and marker conditions."""

    def __init__(self) -> None:
        super().__init__()
        self.raise_after_marker_put = False
        self.fail_ttl_transaction_number: int | None = None
        self.ttl_transaction_count = 0
        self.drift_locator_before_marker = False

    def transact_write_items(self, **request: Any) -> None:
        marker_put = any(
            action.get("Put", {}).get("Item", {}).get("SK") == {"S": PUBLICATION_RETENTION_SORT_KEY}
            for action in request["TransactItems"]
        )
        ttl_fanout = not marker_put and any(
            "expires_at" in action.get("Put", {}).get("Item", {})
            for action in request["TransactItems"]
        )
        if ttl_fanout:
            self.ttl_transaction_count += 1
            if self.ttl_transaction_count == self.fail_ttl_transaction_number:
                self.fail_ttl_transaction_number = None
                self.transactions.append(copy.deepcopy(request))
                raise RuntimeError("synthetic later TTL batch failure")
        if marker_put and self.drift_locator_before_marker:
            self.drift_locator_before_marker = False
            locator_key = next(key for key in self.items if key[1] == "REQUEST_RECEIPT_LOCATOR")
            self.items[locator_key]["payload"] = {
                "S": self.items[locator_key]["payload"]["S"] + " "
            }
        super().transact_write_items(**request)
        if self.raise_after_marker_put and marker_put:
            self.raise_after_marker_put = False
            raise RuntimeError("synthetic uncertain marker response")

    def _condition_holds(self, action: dict[str, Any]) -> bool:
        operation = action.get("Put") or action.get("ConditionCheck")
        assert operation is not None
        names = operation.get("ExpressionAttributeNames", {})
        if "#f0" not in names:
            return super()._condition_holds(action)
        item = operation.get("Item")
        key = operation.get("Key")
        lookup = self._key(item) if item is not None else (key["PK"]["S"], key["SK"]["S"])
        existing = self.items.get(lookup)
        if existing is None:
            return False
        values = operation["ExpressionAttributeValues"]
        for name_token, field_name in names.items():
            if name_token == "#ttl":
                continue
            value_token = f":v{name_token.removeprefix('#f')}"
            if existing.get(field_name) != values[value_token]:
                return False
        expected_ttl = values.get(":ttl")
        if expected_ttl is None:
            return True
        actual_ttl = existing.get("expires_at")
        if "Put" in action:
            return actual_ttl is None or actual_ttl == expected_ttl
        return actual_ttl == expected_ttl


def _failed_terminal(
    client: RetentionMemoryDynamoClient | None = None,
) -> tuple[Harness, RetentionMemoryDynamoClient]:
    current = client or RetentionMemoryDynamoClient()
    harness, _, _ = _dynamo_harness(current)
    harness.clock.now = harness.authority.snapshot.verification_deadline
    harness.service.settle_deadline(
        harness.command(SettlePublicationDeadlineCommand, "phase75_pre_dispatch_deadline")
    )
    return harness, current


def _published_terminal() -> tuple[Harness, RetentionMemoryDynamoClient]:
    harness, _, client = _dynamo_harness(RetentionMemoryDynamoClient())
    harness.dispatch_and_reconstruct()
    harness.complete_preflight()
    _, post_claim = harness.claim_publish()
    post_evidence = harness.publish_evidence(post_claim, accepted=True)
    harness.clock.tick()
    harness.service.record_post_outcome(
        harness.command(
            RecordPublicationPostOutcomeCommand,
            "phase75_post_outcome",
            evidence=post_evidence,
        )
    )
    harness.clock.tick()
    _, product_claim = harness.claim_product(PublicationCallPurpose.VERIFICATION)
    evidence = harness.product_evidence(product_claim, positive=True)
    harness.clock.tick()
    harness.service.record_product_observation(
        harness.command(
            RecordPublicationProductObservationCommand,
            "phase75_positive_observation",
            evidence=evidence,
        )
    )
    return harness, client


def _retention(
    harness: Harness,
    client: RetentionMemoryDynamoClient,
) -> tuple[DynamoDBPublicationOperationalRetentionStore, PublicationOperationalRetentionService]:
    store = DynamoDBPublicationOperationalRetentionStore(
        client=client,
        table_name=TABLE_NAME,
    )
    service = PublicationOperationalRetentionService(store, clock=harness.clock)
    return store, service


def test_assigns_every_exact_row_and_writes_nine_action_marker_last() -> None:
    harness, client = _failed_terminal()
    client.query_page_size = 3
    store, service = _retention(harness, client)
    transaction_count = len(client.transactions)

    completion = service.assign(owner_id=OWNER_ID, aggregate_id=harness.aggregate_id)

    assert MAX_RETENTION_PARTITION_ITEMS == 943
    assert MAX_RETENTION_ASSIGNMENTS == 945
    assert MAX_RETENTION_INITIAL_ADDITIONAL_ITEMS == 22
    expiry = publication_operational_expiry_epoch(completion.operational_expires_at)
    publication_pk = f"PUBLICATION#{harness.aggregate_id}"
    publication_items = [
        item for (partition_key, _), item in client.items.items() if partition_key == publication_pk
    ]
    assert completion.publication_row_count == len(publication_items) == 12
    assert completion.ttl_assignment_count == 14
    assert all(item["expires_at"] == {"N": str(expiry)} for item in publication_items)
    job_id = harness.transaction.updated_job.job_id
    assert client.items[(f"JOB#{job_id}", "META")]["expires_at"] == {"N": str(expiry)}
    locator = store.load_terminal_retention_authority(
        OWNER_ID,
        harness.aggregate_id,
    ).receipt_locator
    assert client.items[(locator.owner_receipt_partition_key, locator.owner_receipt_sort_key)][
        "expires_at"
    ] == {"N": str(expiry)}
    assert "expires_at" not in client.items[(f"JOB#{job_id}", "SOURCE")]
    marker = client.items[(f"JOB#{job_id}", PUBLICATION_RETENTION_SORT_KEY)]
    assert marker["expires_at"] == {"N": str(expiry)}
    retention_transactions = client.transactions[transaction_count:]
    assert all(
        len(transaction["TransactItems"]) <= MAX_RETENTION_TRANSACTION_ITEMS
        for transaction in retention_transactions
    )
    marker_actions = retention_transactions[-1]["TransactItems"]
    assert all(
        len(transaction["TransactItems"]) <= 24 for transaction in retention_transactions[:-1]
    )
    assert len(marker_actions) == 9
    assert len([action for action in marker_actions if "ConditionCheck" in action]) == 8
    assert marker_actions[-1]["Put"]["Item"] == marker
    assert len(client.query_requests) > 1


def test_exact_replay_writes_nothing_and_uncertain_marker_success_is_resolved() -> None:
    harness, client = _failed_terminal()
    store, service = _retention(harness, client)
    client.raise_after_marker_put = True

    first = service.assign(owner_id=OWNER_ID, aggregate_id=harness.aggregate_id)
    transaction_count = len(client.transactions)
    replay = service.assign(owner_id=OWNER_ID, aggregate_id=harness.aggregate_id)

    assert replay == first
    assert len(client.transactions) == transaction_count
    assert store.load_terminal_retention_authority(OWNER_ID, harness.aggregate_id).job == (
        harness.store.load_linked_job(OWNER_ID, harness.aggregate_id)
    )


def test_later_fanout_failure_writes_no_marker_and_exact_replay_completes() -> None:
    harness, client = _published_terminal()
    _, service = _retention(harness, client)
    client.fail_ttl_transaction_number = 2
    job_id = harness.transaction.updated_job.job_id
    partition_key = f"PUBLICATION#{harness.aggregate_id}"

    with pytest.raises(PublicationRetentionDependencyUnavailableError):
        service.assign(owner_id=OWNER_ID, aggregate_id=harness.aggregate_id)

    assert (f"JOB#{job_id}", PUBLICATION_RETENTION_SORT_KEY) not in client.items
    partial = [
        "expires_at" in item for (pk, _), item in client.items.items() if pk == partition_key
    ]
    assert any(partial) and not all(partial)

    completion = service.assign(owner_id=OWNER_ID, aggregate_id=harness.aggregate_id)

    assert client.items[(f"JOB#{job_id}", PUBLICATION_RETENTION_SORT_KEY)]["payload"] == {
        "S": completion.model_dump_json()
    }
    assert all(
        "expires_at" in item for (pk, _), item in client.items.items() if pk == partition_key
    )
    assert all(
        len(transaction["TransactItems"]) <= 24
        for transaction in client.transactions
        if any(
            "expires_at" in action.get("Put", {}).get("Item", {})
            for action in transaction["TransactItems"]
        )
        and not any(
            action.get("Put", {}).get("Item", {}).get("SK") == {"S": PUBLICATION_RETENTION_SORT_KEY}
            for action in transaction["TransactItems"]
        )
    )


def test_locator_drift_at_marker_condition_never_creates_completion() -> None:
    harness, client = _failed_terminal()
    _, service = _retention(harness, client)
    client.drift_locator_before_marker = True
    job_id = harness.transaction.updated_job.job_id

    with pytest.raises(
        (PublicationRetentionBoundaryInvalidError, PublicationRetentionConflictError)
    ):
        service.assign(owner_id=OWNER_ID, aggregate_id=harness.aggregate_id)

    assert (f"JOB#{job_id}", PUBLICATION_RETENTION_SORT_KEY) not in client.items


@pytest.mark.parametrize(
    "target",
    ["locator_entity", "locator_payload", "receipt_payload", "source_payload"],
)
def test_malformed_attribute_value_shapes_fail_as_closed_boundary_errors(target: str) -> None:
    harness, client = _failed_terminal()
    store, service = _retention(harness, client)
    authority = store.load_terminal_retention_authority(OWNER_ID, harness.aggregate_id)
    locator_key = (
        f"PUBLICATION#{harness.aggregate_id}",
        "REQUEST_RECEIPT_LOCATOR",
    )
    receipt_key = (
        authority.receipt_locator.owner_receipt_partition_key,
        authority.receipt_locator.owner_receipt_sort_key,
    )
    source_key = (f"JOB#{authority.job.job_id}", "SOURCE")
    if target == "locator_entity":
        client.items[locator_key]["entity_type"] = "malformed"
    elif target == "locator_payload":
        client.items[locator_key]["payload"] = []
    elif target == "receipt_payload":
        client.items[receipt_key]["payload"] = 7
    else:
        client.items[source_key]["payload"] = False

    with pytest.raises(PublicationRetentionBoundaryInvalidError):
        service.assign(owner_id=OWNER_ID, aggregate_id=harness.aggregate_id)


def test_existing_different_ttl_and_foreign_marker_fail_without_overwrite() -> None:
    harness, client = _failed_terminal()
    _, service = _retention(harness, client)
    publication_pk = f"PUBLICATION#{harness.aggregate_id}"
    root = client.items[(publication_pk, "META")]
    root["expires_at"] = {"N": "1"}
    before = copy.deepcopy(client.items)

    with pytest.raises(PublicationRetentionConflictError):
        service.assign(owner_id=OWNER_ID, aggregate_id=harness.aggregate_id)

    assert client.items == before

    root.pop("expires_at")
    completion = service.assign(owner_id=OWNER_ID, aggregate_id=harness.aggregate_id)
    job_id = harness.transaction.updated_job.job_id
    marker = client.items[(f"JOB#{job_id}", PUBLICATION_RETENTION_SORT_KEY)]
    marker["aggregate_id"] = {"S": "foreign_aggregate"}
    before = copy.deepcopy(client.items)
    with pytest.raises(PublicationRetentionConflictError):
        service.assign(owner_id=OWNER_ID, aggregate_id=harness.aggregate_id)
    assert client.items == before
    assert completion.aggregate_id == harness.aggregate_id


@pytest.mark.parametrize(
    ("prefix", "replacement"),
    [
        ("EVENT#00000000000000000002", "EVENT#00000000000000000002evil"),
        ("CALL_CLAIM#", "CALL_CLAIM#wrong"),
        ("PROVIDER_EVIDENCE#", "PROVIDER_EVIDENCE#wrong"),
    ],
)
def test_closed_inventory_rejects_payload_moved_to_a_different_physical_key(
    prefix: str,
    replacement: str,
) -> None:
    if prefix.startswith("EVENT#"):
        harness, client = _failed_terminal()
    else:
        harness, client = _published_terminal()
    partition_key = f"PUBLICATION#{harness.aggregate_id}"
    old_key = next(
        key for key in client.items if key[0] == partition_key and key[1].startswith(prefix)
    )
    item = client.items.pop(old_key)
    item["SK"] = {"S": replacement}
    client.items[(partition_key, replacement)] = item
    _, service = _retention(harness, client)
    transaction_count = len(client.transactions)

    with pytest.raises(PublicationRetentionBoundaryInvalidError):
        service.assign(owner_id=OWNER_ID, aggregate_id=harness.aggregate_id)

    assert len(client.transactions) == transaction_count


def test_closed_inventory_rejects_unknown_row_and_first_item_over_bound() -> None:
    harness, client = _failed_terminal()
    partition_key = f"PUBLICATION#{harness.aggregate_id}"
    client.items[(partition_key, "ZZ_UNKNOWN")] = {
        "PK": {"S": partition_key},
        "SK": {"S": "ZZ_UNKNOWN"},
        "entity_type": {"S": "UNKNOWN"},
        "contract_version": {"S": "7.0.1"},
        "payload": {"S": "{}"},
    }
    _, service = _retention(harness, client)
    with pytest.raises(PublicationRetentionBoundaryInvalidError):
        service.assign(owner_id=OWNER_ID, aggregate_id=harness.aggregate_id)

    client.items.pop((partition_key, "ZZ_UNKNOWN"))
    current = sum(1 for key in client.items if key[0] == partition_key)
    for index in range(MAX_RETENTION_PARTITION_ITEMS + 1 - current):
        sort_key = f"ZZ_OVERFLOW#{index:020d}"
        client.items[(partition_key, sort_key)] = {
            "PK": {"S": partition_key},
            "SK": {"S": sort_key},
            "entity_type": {"S": "UNKNOWN"},
            "contract_version": {"S": "7.0.1"},
            "payload": {"S": "{}"},
        }
    client.query_page_size = 73
    with pytest.raises(PublicationRetentionBoundaryInvalidError):
        service.assign(owner_id=OWNER_ID, aggregate_id=harness.aggregate_id)


def test_unrelated_rows_and_provider_data_are_never_mutated_or_deleted() -> None:
    harness, client = _failed_terminal()
    unrelated = {
        "PK": {"S": "JOB#unrelated"},
        "SK": {"S": "META"},
        "entity_type": {"S": "UNRELATED"},
        "payload": {"S": "{}"},
    }
    client.items[("JOB#unrelated", "META")] = copy.deepcopy(unrelated)
    _, service = _retention(harness, client)

    service.assign(owner_id=OWNER_ID, aggregate_id=harness.aggregate_id)

    assert client.items[("JOB#unrelated", "META")] == unrelated
    assert all(
        "Delete" not in action and "Update" not in action
        for transaction in client.transactions
        for action in transaction["TransactItems"]
    )


def test_legal_superseded_unconsumed_preflight_stage_is_retained_and_ttl_assigned() -> None:
    harness, _, client = _dynamo_harness(RetentionMemoryDynamoClient())
    harness.dispatch_and_reconstruct()
    _, shop_claim = harness.claim_shop()
    stale = harness.stage_evidence(harness.shop_evidence(shop_claim))
    harness.clock.tick()
    _, latest_shop_claim = harness.claim_shop()
    latest_shop = harness.shop_evidence(latest_shop_claim)
    _, product_claim = harness.claim_product(PublicationCallPurpose.PRODUCT_PREFLIGHT)
    product = harness.product_evidence(product_claim)
    harness.clock.tick()
    harness.service.record_preflight(
        harness.command(
            RecordPublicationPreflightCommand,
            "phase75_latest_preflight",
            shop_evidence=latest_shop,
            product_evidence=product,
        )
    )
    harness.clock.tick()
    _, post_claim = harness.claim_publish()
    post = harness.publish_evidence(post_claim, accepted=True)
    harness.clock.tick()
    harness.service.record_post_outcome(
        harness.command(
            RecordPublicationPostOutcomeCommand,
            "phase75_stale_stage_post",
            evidence=post,
        )
    )
    harness.clock.tick()
    _, read_claim = harness.claim_product(PublicationCallPurpose.VERIFICATION)
    read = harness.product_evidence(read_claim, positive=True)
    harness.clock.tick()
    harness.service.record_product_observation(
        harness.command(
            RecordPublicationProductObservationCommand,
            "phase75_stale_stage_result",
            evidence=read,
        )
    )
    _, service = _retention(harness, client)

    completion = service.assign(owner_id=OWNER_ID, aggregate_id=harness.aggregate_id)

    stale_key = (
        f"PUBLICATION#{harness.aggregate_id}",
        f"PROVIDER_EVIDENCE#{stale.stage_id}",
    )
    assert client.items[stale_key]["expires_at"] == {"N": str(completion.expires_at_epoch_seconds)}
    assert (
        f"PUBLICATION#{harness.aggregate_id}",
        f"PROVIDER_EVIDENCE_CONSUMED#{stale.stage_id}",
    ) not in client.items
