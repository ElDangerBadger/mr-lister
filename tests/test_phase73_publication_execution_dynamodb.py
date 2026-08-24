"""Offline DynamoDB parity tests for Phase 7.3 publication execution."""

from __future__ import annotations

import ast
import json
from pathlib import Path
from typing import Any

import pytest
from test_phase71_publication_dynamodb import (
    TABLE_NAME,
    MemoryPublicationDynamoClient,
)
from test_phase71_publication_service import ProfileAuthority, _authority
from test_phase71_publication_store import OWNER_ID
from test_phase72_publication_execution import Harness

from mr_lister.publication.dynamodb import DynamoDBPublicationStore
from mr_lister.publication.errors import PublicationConflictError, PublicationErrorCode
from mr_lister.publication.evidence_provenance import build_provider_evidence_commit
from mr_lister.publication.execution_commands import (
    DispatchPublicationWorkCommand,
    RecordPublicationPostOutcomeCommand,
    RecordPublicationPreflightCommand,
    RecordPublicationProductObservationCommand,
    SettlePublicationDeadlineCommand,
)
from mr_lister.publication.execution_dynamodb import (
    FROZEN_MAX_EXECUTION_AUTHORITY_ITEMS,
    MAX_EXECUTION_AUTHORITY_ITEMS,
    MAX_EXECUTION_TRANSACTION_ITEMS,
    DynamoDBPublicationExecutionStore,
)
from mr_lister.publication.execution_models import (
    PublicationCallPurpose,
    PublicationExecutionOperation,
)
from mr_lister.publication.execution_service import PublicationExecutionService
from mr_lister.publication.execution_store import (
    PublicationExecutionCommit,
    PublicationExecutionCommitResult,
)
from mr_lister.publication.models import PublicationAggregate


class ExecutionMemoryDynamoClient(MemoryPublicationDynamoClient):
    """Low-level fake that understands generic exact-payload CAS and strong queries."""

    def __init__(self) -> None:
        super().__init__()
        self.query_requests: list[dict[str, Any]] = []
        self.query_page_size: int | None = None

    def query(self, **request: Any) -> dict[str, Any]:
        self.query_requests.append(request)
        partition_key = request["ExpressionAttributeValues"][":pk"]["S"]
        items = sorted(
            (
                item
                for (current_partition, _), item in self.items.items()
                if current_partition == partition_key
            ),
            key=lambda item: item["SK"]["S"],
        )
        cursor = request.get("ExclusiveStartKey")
        if cursor is not None:
            cursor_key = (cursor["PK"]["S"], cursor["SK"]["S"])
            items = [item for item in items if self._key(item) > cursor_key]
        limit = request["Limit"]
        if self.query_page_size is not None:
            limit = min(limit, self.query_page_size)
        page = items[:limit]
        if len(items) > limit:
            return {
                "Items": page,
                "LastEvaluatedKey": {
                    "PK": {"S": partition_key},
                    "SK": page[-1]["SK"],
                },
            }
        return {"Items": page}

    def _condition_holds(self, action: dict[str, Any]) -> bool:
        operation = action.get("Put") or action.get("ConditionCheck")
        assert operation is not None
        values = operation.get("ExpressionAttributeValues", {})
        if ":expected_entity_type" not in values:
            return super()._condition_holds(action)
        item = operation.get("Item")
        key = operation.get("Key")
        lookup = self._key(item) if item is not None else (key["PK"]["S"], key["SK"]["S"])
        existing = self.items.get(lookup)
        return existing is not None and (
            existing.get("entity_type") == values[":expected_entity_type"]
            and existing.get("contract_version") == values[":expected_contract_version"]
            and existing.get("payload") == values[":expected_payload"]
        )


class ExecutionCommitCapture:
    def __init__(self, delegate: DynamoDBPublicationExecutionStore) -> None:
        self._delegate = delegate
        self.commit: PublicationExecutionCommit | None = None

    def __getattr__(self, name: str) -> Any:
        return getattr(self._delegate, name)

    def commit_execution(
        self,
        commit: PublicationExecutionCommit,
    ) -> PublicationExecutionCommitResult:
        self.commit = commit
        return PublicationExecutionCommitResult(receipt=commit.receipt)


class MalformedCursorDynamoClient(ExecutionMemoryDynamoClient):
    def __init__(self, mode: str) -> None:
        super().__init__()
        self._mode = mode
        self._first_cursor: dict[str, Any] | None = None
        self.query_page_size = 1

    def query(self, **request: Any) -> dict[str, Any]:
        response = super().query(**request)
        if "ExclusiveStartKey" not in request:
            self._first_cursor = response.get("LastEvaluatedKey")
            return response
        assert self._first_cursor is not None
        if self._mode == "empty_nonprogress":
            return {"Items": [], "LastEvaluatedKey": self._first_cursor}
        response["LastEvaluatedKey"] = self._first_cursor
        return response


class RootMutationDynamoClient(ExecutionMemoryDynamoClient):
    def __init__(self) -> None:
        super().__init__()
        self.query_page_size = 4
        self.mutated = False

    def query(self, **request: Any) -> dict[str, Any]:
        response = super().query(**request)
        if not self.mutated:
            root = next(
                (item for item in response["Items"] if item["SK"]["S"] == "META"),
                None,
            )
            if root is not None:
                key = self._key(root)
                self.items[key] = {
                    **self.items[key],
                    "payload": {"S": self.items[key]["payload"]["S"] + " "},
                }
                self.mutated = True
        return response


def _dynamo_harness(
    client: ExecutionMemoryDynamoClient | None = None,
) -> tuple[
    Harness,
    DynamoDBPublicationExecutionStore,
    ExecutionMemoryDynamoClient,
]:
    harness = Harness()
    client = client or ExecutionMemoryDynamoClient()
    client.seed_authority(harness.transaction.authority)
    DynamoDBPublicationStore(client=client, table_name=TABLE_NAME).commit_request(
        harness.transaction
    )
    store = DynamoDBPublicationExecutionStore(client=client, table_name=TABLE_NAME)
    _, exact = _authority()
    harness.store = store  # type: ignore[assignment]
    harness.service = PublicationExecutionService(
        store,
        profiles=ProfileAuthority(exact),
        release_manifest_fingerprint="b" * 64,
        clock=harness.clock,
    )
    return harness, store, client


def test_pristine_rows_load_owner_first_with_one_bounded_strong_query() -> None:
    harness, store, client = _dynamo_harness()
    client.get_requests.clear()

    loaded = store.load_execution_authority(OWNER_ID, harness.aggregate_id)

    assert isinstance(loaded.expected_aggregate, PublicationAggregate)
    assert loaded.aggregate.aggregate_id == harness.aggregate_id
    assert client.get_requests[0] == {
        "TableName": TABLE_NAME,
        "Key": {
            "PK": {"S": f"PUBLICATION#{harness.aggregate_id}"},
            "SK": {"S": "META"},
        },
        "ConsistentRead": True,
    }
    assert client.query_requests[-1]["ConsistentRead"] is True
    assert client.query_requests[-1]["Limit"] == MAX_EXECUTION_AUTHORITY_ITEMS + 1
    assert store.load_source_authority(OWNER_ID, harness.aggregate_id).current_job == (
        harness.transaction.updated_job
    )
    with pytest.raises(Exception) as hidden:
        store.load_execution_authority("f" * 64, harness.aggregate_id)
    assert hidden.type.__name__ == "PublicationNotFoundError"


def test_fifty_six_product_reads_load_across_strong_query_pages() -> None:
    harness, store, client = _dynamo_harness()
    harness.dispatch_and_reconstruct()
    harness.complete_preflight()
    _, post_claim = harness.claim_publish()
    post_evidence = harness.publish_evidence(post_claim, accepted=True)
    harness.clock.tick()
    harness.service.record_post_outcome(
        harness.command(
            RecordPublicationPostOutcomeCommand,
            "paged_post_outcome",
            evidence=post_evidence,
        )
    )
    for index in range(55):
        harness.clock.tick()
        _, product_claim = harness.claim_product(PublicationCallPurpose.VERIFICATION)
        evidence = harness.product_evidence(product_claim)
        harness.clock.tick()
        harness.service.record_product_observation(
            harness.command(
                RecordPublicationProductObservationCommand,
                f"paged_observation_{index}",
                evidence=evidence,
            )
        )

    partition_key = f"PUBLICATION#{harness.aggregate_id}"
    assert sum(1 for key in client.items if key[0] == partition_key) >= 531
    assert FROZEN_MAX_EXECUTION_AUTHORITY_ITEMS == 942
    assert FROZEN_MAX_EXECUTION_AUTHORITY_ITEMS <= MAX_EXECUTION_AUTHORITY_ITEMS == 1024
    client.query_page_size = 64
    client.query_requests.clear()

    authority = store.load_execution_authority(OWNER_ID, harness.aggregate_id)

    assert authority.attempt.product_get_call_count == 56
    assert len(authority.product_observations) == 55
    assert len(client.query_requests) > 1
    assert all(request["ConsistentRead"] is True for request in client.query_requests)
    assert all("ExclusiveStartKey" in request for request in client.query_requests[1:])


def test_bounded_query_rejects_the_first_row_above_the_closed_cap() -> None:
    harness, store, client = _dynamo_harness()
    partition_key = f"PUBLICATION#{harness.aggregate_id}"
    current_count = sum(1 for key in client.items if key[0] == partition_key)
    for index in range(MAX_EXECUTION_AUTHORITY_ITEMS + 1 - current_count):
        sort_key = f"ZZZ_OVERFLOW#{index:020d}"
        client.items[(partition_key, sort_key)] = {
            "PK": {"S": partition_key},
            "SK": {"S": sort_key},
            "entity_type": {"S": "SYNTHETIC_OVERFLOW"},
            "contract_version": {"S": "7.0.1"},
            "payload": {"S": "{}"},
        }
    client.query_page_size = 127

    with pytest.raises(PublicationConflictError) as error:
        store.load_execution_authority(OWNER_ID, harness.aggregate_id)

    assert error.value.code is PublicationErrorCode.INVALID_AUTHORITY
    assert len(client.query_requests) > 1


@pytest.mark.parametrize("mode", ["empty_nonprogress", "duplicate_cursor"])
def test_bounded_query_rejects_nonprogress_and_duplicate_cursors(mode: str) -> None:
    client = MalformedCursorDynamoClient(mode)
    harness, store, _ = _dynamo_harness(client)

    with pytest.raises(PublicationConflictError) as error:
        store.load_execution_authority(OWNER_ID, harness.aggregate_id)

    assert error.value.code is PublicationErrorCode.INVALID_AUTHORITY


def test_bounded_query_rejects_root_mutation_between_strong_pages() -> None:
    client = RootMutationDynamoClient()
    harness, store, _ = _dynamo_harness(client)

    with pytest.raises(PublicationConflictError) as error:
        store.load_execution_authority(OWNER_ID, harness.aggregate_id)

    assert client.mutated is True
    assert error.value.code is PublicationErrorCode.CONCURRENT_WRITE


def test_dispatch_replaces_exact_four_roots_and_appends_event_receipt() -> None:
    harness, store, client = _dynamo_harness()
    authority = store.load_execution_authority(OWNER_ID, harness.aggregate_id)
    command = harness.command(DispatchPublicationWorkCommand, "dynamo_dispatch")

    result = harness.service.dispatch_work(command)
    replay = harness.service.dispatch_work(command)

    assert replay.receipt == result.receipt
    actions = client.transactions[-1]["TransactItems"]
    assert len(actions) == 7
    assert len(actions) <= MAX_EXECUTION_TRANSACTION_ITEMS == 25
    keys = [action["Put"]["Item"]["SK"]["S"] for action in actions if "Put" in action]
    assert keys == [
        "META",
        f"ATTEMPT#{authority.attempt.attempt_id}",
        f"PERMIT#{authority.permit.permit_id}",
        f"PUBLICATION_WORK#{authority.work.work_request_id}",
        "EVENT#00000000000000000002",
        "EXECUTION_RECEIPT#dynamo_dispatch",
    ]
    assert all(
        ":expected_payload" in action["Put"].get("ExpressionAttributeValues", {})
        for action in actions[:4]
    )
    assert actions[-1]["ConditionCheck"]["Key"]["PK"]["S"].startswith("JOB#")
    evolved = store.load_execution_authority(OWNER_ID, harness.aggregate_id)
    assert evolved.expected_aggregate == evolved.aggregate


def test_audit_and_evidence_stage_use_exact_claim_authority_and_audit_cas() -> None:
    harness, store, client = _dynamo_harness()
    harness.dispatch_and_reconstruct()
    _, claim = harness.claim_shop()

    audit_actions = client.transactions[-1]["TransactItems"]
    assert len(audit_actions) == 3
    assert audit_actions[0]["Put"]["Item"]["SK"] == {"S": "META"}
    assert audit_actions[1]["ConditionCheck"]["Key"]["SK"] == {
        "S": f"CALL_CLAIM#{claim.authorization_id}"
    }
    assert audit_actions[2]["Put"]["Item"]["SK"] == {
        "S": f"PROVIDER_AUDIT#{claim.resulting_attempt_record_version:020d}"
    }

    transaction_count = len(client.transactions)
    stage = harness.stage_evidence(harness.shop_evidence(claim))
    replay = harness.stage_evidence(harness.shop_evidence(claim))

    assert replay == stage
    assert len(client.transactions) == transaction_count + 1
    actions = client.transactions[-1]["TransactItems"]
    assert len(actions) == 6
    assert actions[0]["Put"]["Item"]["SK"] == {"S": "META"}
    assert actions[0]["Put"]["Item"]["provider_evidence_record_version"] == {"N": "1"}
    checked_keys = {
        (
            action["ConditionCheck"]["Key"]["PK"]["S"],
            action["ConditionCheck"]["Key"]["SK"]["S"],
        )
        for action in actions
        if "ConditionCheck" in action
    }
    assert checked_keys == {
        (
            f"PUBLICATION#{harness.aggregate_id}",
            f"CALL_CLAIM#{claim.authorization_id}",
        ),
        (
            f"PUBLICATION#{harness.aggregate_id}",
            f"PROVIDER_AUDIT#{claim.resulting_attempt_record_version:020d}",
        ),
        (f"PUBLICATION#{harness.aggregate_id}", "PROVIDER_AUTHORITY"),
        (f"JOB#{harness.transaction.updated_job.job_id}", "META"),
    }
    stage_item = actions[-1]["Put"]["Item"]
    assert stage_item["SK"] == {"S": f"PROVIDER_EVIDENCE#{stage.stage_id}"}
    assert (
        store.get_provider_evidence_stage(
            OWNER_ID,
            harness.aggregate_id,
            stage.stage_id,
        )
        == stage
    )
    assert store.list_unconsumed_provider_evidence(OWNER_ID, harness.aggregate_id) == (stage,)
    assert (
        store.load_execution_authority(
            OWNER_ID,
            harness.aggregate_id,
        ).aggregate.provider_evidence_record_version
        == 1
    )


def _stage_deadline_race() -> tuple[
    Harness,
    DynamoDBPublicationExecutionStore,
    ExecutionMemoryDynamoClient,
    Any,
    PublicationExecutionCommit,
]:
    harness, store, client = _dynamo_harness()
    harness.dispatch_and_reconstruct()
    _, claim = harness.claim_shop()
    authority = harness.authority
    audit = next(
        binding
        for binding in authority.provider_audits
        if binding.call_claim_id == claim.authorization_id
    )
    stage_commit = build_provider_evidence_commit(
        authority,
        claim,
        audit,
        harness.shop_evidence(claim),
        staged_at=harness.clock.now,
    )

    capture = ExecutionCommitCapture(store)
    _, exact = _authority()
    deadline_service = PublicationExecutionService(
        capture,  # type: ignore[arg-type]
        profiles=ProfileAuthority(exact),
        release_manifest_fingerprint="b" * 64,
        clock=harness.clock,
    )
    harness.clock.now = authority.snapshot.verification_deadline
    deadline_service.settle_deadline(
        harness.command(SettlePublicationDeadlineCommand, "deadline_race")
    )
    assert capture.commit is not None
    return harness, store, client, stage_commit, capture.commit


@pytest.mark.parametrize("winner", ["stage", "deadline"])
def test_stage_and_deadline_exact_meta_cas_has_one_winner(winner: str) -> None:
    harness, store, client, stage_commit, deadline_commit = _stage_deadline_race()

    if winner == "stage":
        stage = store.stage_evidence(stage_commit)
        winning_transaction = client.transactions[-1]["TransactItems"]
        written_sort_keys = [
            action["Put"]["Item"]["SK"]["S"] for action in winning_transaction if "Put" in action
        ]
        assert written_sort_keys[:2] == [
            "META",
            f"PROVIDER_EVIDENCE#{stage.stage_id}",
        ]
        after_winner = dict(client.items)
        with pytest.raises(PublicationConflictError) as error:
            store.commit_execution(deadline_commit)
        assert error.value.code is PublicationErrorCode.CONCURRENT_WRITE
        assert client.items == after_winner
        deadline_transaction = client.transactions[-1]["TransactItems"]
        transaction_count = len(client.transactions)
        assert store.stage_evidence(stage_commit) == stage
        assert len(client.transactions) == transaction_count
        authority = store.load_execution_authority(OWNER_ID, harness.aggregate_id)
        assert authority.aggregate.terminal_at is None
        assert authority.aggregate.provider_evidence_record_version == 1
    else:
        store.commit_execution(deadline_commit)
        deadline_transaction = client.transactions[-1]["TransactItems"]
        after_winner = dict(client.items)
        with pytest.raises(PublicationConflictError) as error:
            store.stage_evidence(stage_commit)
        assert error.value.code is PublicationErrorCode.CONCURRENT_WRITE
        assert client.items == after_winner
        authority = store.load_execution_authority(OWNER_ID, harness.aggregate_id)
        assert authority.aggregate.terminal_at is not None
        assert authority.aggregate.provider_evidence_record_version == 0
        assert not any(
            sort_key.startswith("PROVIDER_EVIDENCE#")
            for partition_key, sort_key in client.items
            if partition_key == f"PUBLICATION#{harness.aggregate_id}"
        )
    expected_root_payload = json.loads(
        deadline_transaction[0]["Put"]["ExpressionAttributeValues"][":expected_payload"]["S"]
    )
    assert expected_root_payload["provider_evidence_record_version"] == 0


def test_preflight_consumes_both_stages_in_the_same_bounded_transaction() -> None:
    harness, store, client = _dynamo_harness()
    harness.dispatch_and_reconstruct()
    _, shop_claim = harness.claim_shop()
    shop_stage = harness.stage_evidence(harness.shop_evidence(shop_claim))
    harness.clock.tick()
    _, product_claim = harness.claim_product(PublicationCallPurpose.PRODUCT_PREFLIGHT)
    product_stage = harness.stage_evidence(harness.product_evidence(product_claim))
    harness.clock.tick()
    command = harness.command(
        RecordPublicationPreflightCommand,
        "consume_two_stages",
        shop_evidence_stage_id=shop_stage.stage_id,
        shop_evidence_stage_fingerprint=shop_stage.fingerprint,
        product_evidence_stage_id=product_stage.stage_id,
        product_evidence_stage_fingerprint=product_stage.fingerprint,
    )

    result = harness.service.record_preflight(command)

    assert result.receipt.operation is PublicationExecutionOperation.RECORD_PREFLIGHT
    actions = client.transactions[-1]["TransactItems"]
    assert len(actions) == 12
    assert len(actions) <= MAX_EXECUTION_TRANSACTION_ITEMS
    assert {
        action["Put"]["Item"]["SK"]["S"]
        for action in actions
        if "Put" in action
        and action["Put"]["Item"]["SK"]["S"].startswith("PROVIDER_EVIDENCE_CONSUMED#")
    } == {
        f"PROVIDER_EVIDENCE_CONSUMED#{shop_stage.stage_id}",
        f"PROVIDER_EVIDENCE_CONSUMED#{product_stage.stage_id}",
    }
    assert store.list_unconsumed_provider_evidence(OWNER_ID, harness.aggregate_id) == ()
    assert (
        store.load_execution_authority(
            OWNER_ID,
            harness.aggregate_id,
        ).preflight_proof
        is not None
    )


def test_positive_terminal_transaction_persists_report_result_and_job_link() -> None:
    harness, store, client = _dynamo_harness()
    harness.dispatch_and_reconstruct()
    harness.complete_preflight()
    _, post_claim = harness.claim_publish()
    post_evidence = harness.publish_evidence(post_claim, accepted=True)
    harness.clock.tick()
    harness.service.record_post_outcome(
        harness.command(
            RecordPublicationPostOutcomeCommand,
            "dynamo_post_outcome",
            evidence=post_evidence,
        )
    )
    harness.clock.tick()
    _, product_claim = harness.claim_product(PublicationCallPurpose.VERIFICATION)
    product_evidence = harness.product_evidence(product_claim, positive=True)
    harness.clock.tick()

    result = harness.service.record_product_observation(
        harness.command(
            RecordPublicationProductObservationCommand,
            "dynamo_positive_verification",
            evidence=product_evidence,
        )
    )

    actions = client.transactions[-1]["TransactItems"]
    assert len(actions) == 15
    assert len(actions) <= MAX_EXECUTION_TRANSACTION_ITEMS
    sort_keys = {action["Put"]["Item"]["SK"]["S"] for action in actions if "Put" in action}
    assert {
        "RESULT",
        "NOTIFICATION",
        "REPORT",
        "TOMBSTONE",
        "TERMINAL_JOB_LINK",
        f"EXECUTION_RECEIPT#{result.receipt.operation_id}",
    }.issubset(sort_keys)
    authority = store.load_execution_authority(OWNER_ID, harness.aggregate_id)
    assert authority.result is not None
    assert authority.notification is not None
    assert authority.report is not None
    assert authority.tombstone is not None
    assert authority.terminal_job_link is not None
    assert (
        store.load_linked_job(
            OWNER_ID,
            harness.aggregate_id,
        ).publication_terminal_state
        == "published"
    )


def test_stale_exact_child_payload_aborts_without_partial_execution_writes() -> None:
    source = Harness(capture_commits=True)
    source.service.dispatch_work(
        source.command(DispatchPublicationWorkCommand, "captured_dispatch")
    )
    commit = source.store.last_commit  # type: ignore[attr-defined]
    assert commit is not None

    client = ExecutionMemoryDynamoClient()
    client.seed_authority(source.transaction.authority)
    DynamoDBPublicationStore(client=client, table_name=TABLE_NAME).commit_request(
        source.transaction
    )
    store = DynamoDBPublicationExecutionStore(client=client, table_name=TABLE_NAME)
    attempt_key = (
        f"PUBLICATION#{source.aggregate_id}",
        f"ATTEMPT#{commit.expected.attempt.attempt_id}",
    )
    client.items[attempt_key] = {
        **client.items[attempt_key],
        "payload": {"S": client.items[attempt_key]["payload"]["S"] + " "},
    }
    before = dict(client.items)

    with pytest.raises(PublicationConflictError) as error:
        store.commit_execution(commit)

    assert error.value.code is PublicationErrorCode.CONCURRENT_WRITE
    assert client.items == before
    assert not any(
        sort_key.startswith("EXECUTION_RECEIPT#")
        for partition_key, sort_key in client.items
        if partition_key == f"PUBLICATION#{source.aggregate_id}"
    )


def test_adapter_has_no_runtime_or_provider_capability_imports() -> None:
    path = Path(__file__).parents[1] / "src/mr_lister/publication/execution_dynamodb.py"
    tree = ast.parse(path.read_text())
    imports = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    } | {node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}

    assert not any(
        name.startswith(
            (
                "boto3",
                "mr_lister.cloud",
                "mr_lister.composition",
                "mr_lister.publication.provider_boundary",
            )
        )
        for name in imports
    )
