from __future__ import annotations

import json
from copy import deepcopy
from unittest.mock import Mock

import pytest
from botocore.exceptions import ClientError

from mr_lister.control.dynamodb import _job_item
from mr_lister.judge_cleanup.aws import DynamoCleanupSource, DynamoCleanupStore, s
from mr_lister.judge_cleanup.handler import lambda_handler
from mr_lister.judge_cleanup.service import CleanupRunError, DiscoveryPage
from tests.test_judge_cleanup import config_for, finish_publication, record_for, setup_service
from tests.test_judge_cleanup import evidence as evidence
from tests.test_phase73_publication_execution_dynamodb import _dynamo_harness


class CleanupDynamo:
    def __init__(self):
        self.items, self.calls = {}, []
        self.stale_index = None

    @staticmethod
    def key(item):
        return item["PK"]["S"], item["SK"]["S"]

    def get_item(self, **request):
        self.calls.append(("get", request))
        item = self.items.get(self.key(request["Key"]))
        return {} if item is None else {"Item": deepcopy(item)}

    def transact_write_items(self, **request):
        self.calls.append(("transact", request))
        for action in request["TransactItems"]:
            put = action["Put"]
            item = put["Item"]
            old = self.items.get(self.key(item))
            if put["ConditionExpression"] == "attribute_not_exists(PK)":
                passed = old is None
            else:
                passed = (
                    old is not None and old["payload"] == put["ExpressionAttributeValues"][":old"]
                )
            if not passed:
                raise ClientError(
                    {
                        "Error": {"Code": "TransactionCanceledException"},
                        "CancellationReasons": [{"Code": "ConditionalCheckFailed"}],
                    },
                    "TransactWriteItems",
                )
        for action in request["TransactItems"]:
            item = action["Put"]["Item"]
            self.items[self.key(item)] = deepcopy(item)
        return {}

    def query(self, **request):
        self.calls.append(("query", request))
        if self.stale_index is not None:
            return {"Items": deepcopy(self.stale_index)}
        values = request["ExpressionAttributeValues"]
        return {
            "Items": deepcopy(
                [
                    item
                    for item in self.items.values()
                    if item.get("due_pk") == values[":campaign"]
                    and item.get("due_sk", s("~"))["S"] <= values[":now"]["S"]
                ][: request["Limit"]]
            )
        }

    def put_item(self, **request):
        self.calls.append(("put", request))
        item = request["Item"]
        old = self.items.get(self.key(item))
        values = request.get("ExpressionAttributeValues", {})
        if old is not None and not (
            old.get("campaign_fingerprint") == values.get(":campaign")
            and old.get("payload") == values.get(":old")
        ):
            raise ClientError({"Error": {"Code": "ConditionalCheckFailedException"}}, "PutItem")
        self.items[self.key(item)] = deepcopy(item)
        return {}


def test_state_and_audit_are_atomic_exact_cas_and_source_table_is_never_written(evidence):
    service, _, source, provider, now, record = setup_service(evidence)
    client = CleanupDynamo()
    store = DynamoCleanupStore(client=client, config=service.config)
    service.store = store
    assert store.register(record)
    assert not store.register(record)
    assert service.process(record) == "deleted"
    assert service.process(record) == "skipped"
    provider.delete.assert_called_once()
    assert store.get(evidence.job.job_id).evidence == evidence
    audit = [item for (_, sk), item in client.items.items() if sk.startswith("AUDIT#")]
    assert len(audit) == 3
    assert json.loads(audit[-1]["payload"]["S"])["outcome"] == "provider_deleted"
    for operation, request in client.calls:
        if operation == "transact":
            assert len(request["TransactItems"]) == 2
            assert all(
                action["Put"]["TableName"] == service.config.cleanup_table_name
                for action in request["TransactItems"]
            )
        else:
            assert request["TableName"] == service.config.cleanup_table_name


def test_stale_due_index_is_checked_against_strong_current_state(evidence):
    service, _, source, provider, now, record = setup_service(evidence)
    client = CleanupDynamo()
    store = DynamoCleanupStore(client=client, config=service.config)
    store.register(record)
    client.stale_index = [deepcopy(client.items[(f"JOB#{evidence.job.job_id}", "STATE")])]
    service.store = store
    assert service.process(record) == "deleted"
    assert store.due(now[0], 1) == ()
    assert all(request["ConsistentRead"] for op, request in client.calls if op == "get")


def test_store_rejects_terminal_resurrection_and_changed_deadline(evidence):
    service, _, source, provider, now, record = setup_service(evidence)
    client = CleanupDynamo()
    store = DynamoCleanupStore(client=client, config=service.config)
    service.store = store
    store.register(record)
    service.process(record)
    terminal = store.get(evidence.job.job_id)
    retry = service._change(terminal, status="retry", last_outcome="dependency_unavailable")
    with pytest.raises(ValueError):
        store.compare_and_swap(terminal, retry)
    with pytest.raises(ValueError):
        store.register(terminal)
    provider.delete.assert_called_once()


def test_cleanup_row_tamper_and_campaign_change_are_rejected(evidence):
    config = config_for(evidence)
    client = CleanupDynamo()
    store = DynamoCleanupStore(client=client, config=config)
    record = record_for(evidence, config)
    store.register(record)
    key = (f"JOB#{evidence.job.job_id}", "STATE")
    original = deepcopy(client.items[key])
    for change in [
        {"campaign_fingerprint": s("0" * 64)},
        {"due_pk": s("CAMPAIGN#other")},
        {"PK": s("JOB#other")},
        {"unexpected": s("field")},
    ]:
        client.items[key] = {**deepcopy(original), **change}
        with pytest.raises(CleanupRunError):
            store.get(evidence.job.job_id)


def test_current_source_load_uses_existing_full_strict_graph_and_strong_reads():
    harness, _, client = _dynamo_harness()
    evidence = finish_publication(harness)
    config = config_for(evidence)
    source = DynamoCleanupSource(client=client, config=config)
    client.get_requests.clear()
    client.query_requests.clear()
    assert source.load(evidence.job.job_id) == evidence
    assert all(request["ConsistentRead"] for request in client.get_requests)
    assert all(request["ConsistentRead"] for request in client.query_requests)
    assert all(request["TableName"] == config.source_table_name for request in client.get_requests)
    assert client.query_requests[0]["ExpressionAttributeValues"][":pk"] == s(
        f"PUBLICATION#{evidence.aggregate.aggregate_id}"
    )


def test_malformed_discovery_rows_do_not_hide_later_valid_jobs_or_stall_cursor(evidence):
    config = config_for(evidence)
    client = Mock()
    valid = _job_item(evidence.job)
    poisoned = {**deepcopy(valid), "payload": s("not JSON")}
    last = {field: valid[field] for field in ["PK", "SK", "owner_jobs_pk", "owner_jobs_sk"]}
    client.query.return_value = {"Items": [poisoned, valid], "LastEvaluatedKey": last}
    page = DynamoCleanupSource(client=client, config=config).discover(None, 5)
    assert page.job_ids == (evidence.job.job_id,) and page.invalid_records == 1
    assert page.next_cursor is not None
    request = client.query.call_args.kwargs
    assert request["IndexName"] == "OwnerJobsIndex" and request["Limit"] == 5
    assert request["ExpressionAttributeValues"] == {":owner": s(f"OWNER#{config.judge_owner_id}")}
    client.query.return_value["LastEvaluatedKey"]["owner_jobs_pk"] = s(
        f"OWNER#{config.primary_owner_id}"
    )
    with pytest.raises(CleanupRunError):
        DynamoCleanupSource(client=client, config=config).discover(None, 5)


def test_failed_candidate_is_audited_and_page_advances_after_registering_later_job(evidence):
    service, store, source, provider, now, record = setup_service(evidence, active=False)
    store.records.clear()
    source.discover.return_value = DiscoveryPage(("job_bad", evidence.job.job_id), "next-page")
    source.load.side_effect = [ValueError("private malformed graph"), evidence]
    with pytest.raises(CleanupRunError, match="recorded a failure"):
        service.sweep()
    assert store.get(evidence.job.job_id) is not None
    assert store.saved_cursor == "next-page"
    assert store.audit[-2][0] == "job_bad"
    assert provider.mock_calls == []


def test_dedicated_discovery_error_receipt_is_idempotent_and_contains_no_source_payload(evidence):
    config = config_for(evidence)
    client = CleanupDynamo()
    store = DynamoCleanupStore(client=client, config=config)
    store.discovery_failure("job_bad", evidence.deadline)
    store.discovery_failure("job_bad", evidence.deadline)
    assert len(client.items) == 1
    item = next(iter(client.items.values()))
    assert item["PK"] == s("JOB#job_bad")
    assert json.loads(item["payload"]["S"]) == {
        "at": evidence.deadline.isoformat(),
        "outcome": "source_unavailable",
    }


@pytest.mark.parametrize(
    "event",
    [
        {"job_id": "seller_product"},
        {"dry_run": False},
        {"source": "aws.events", "detail-type": "Scheduled Event", "detail": {"product": "other"}},
        [],
        None,
    ],
)
def test_handler_refuses_event_selected_authority_without_creating_aws_clients(event):
    with pytest.raises(CleanupRunError, match="scheduled event"):
        lambda_handler(event, Mock())
