from __future__ import annotations

import json
from collections.abc import Callable
from copy import deepcopy
from datetime import timedelta
from typing import Any

import pytest
from botocore.exceptions import ClientError

from mr_lister.cloud.api import ReviewQueryApiAdapter, SellerCommandApiAdapter
from mr_lister.cloud.workspace_history import DynamoWorkspaceHistory, HistoryFilteredJobQuery
from mr_lister.control.errors import NotFoundError
from mr_lister.control.store import OwnerJobPage, encode_owner_job_cursor
from tests.test_phase6_cloud_api import (
    JOB_ID,
    NOW,
    OTHER_OWNER,
    OWNER,
    POLICY,
    CommandSpy,
    PreviewSpy,
    QueryStoreSpy,
    ReviewSpy,
    api_event,
    job_record,
    response_body,
)

ROUTE = "POST /v1/jobs/recent/clear"
PATH = "/v1/jobs/recent/clear"


class AtomicDynamo:
    """Exercise Dynamo's all-or-nothing conditional-write behavior without a network."""

    def __init__(self) -> None:
        self.rows: dict[tuple[str, str], dict[str, Any]] = {}
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.before_transaction: Callable[[], object] | None = None
        self.lose_acknowledgment = False

    def get_item(self, **request: Any) -> dict[str, Any]:
        self.calls.append(("get", request))
        assert request["ConsistentRead"] is True
        key = request["Key"]
        row = self.rows.get((key["PK"]["S"], key["SK"]["S"]))
        return {} if row is None else {"Item": deepcopy(row)}

    def transact_write_items(self, **request: Any) -> dict[str, Any]:
        self.calls.append(("transaction", deepcopy(request)))
        callback, self.before_transaction = self.before_transaction, None
        if callback is not None:
            callback()
        puts = [operation["Put"] for operation in request["TransactItems"]]
        reasons = []
        for put in puts:
            row = put["Item"]
            old = self.rows.get((row["PK"]["S"], row["SK"]["S"]))
            if put["ConditionExpression"] == "attribute_not_exists(PK)":
                passes = old is None
            else:
                assert put["ConditionExpression"] == "cleared_before = :previous"
                passes = (
                    old is not None
                    and old["cleared_before"] == (put["ExpressionAttributeValues"][":previous"])
                )
            reasons.append({"Code": "None" if passes else "ConditionalCheckFailed"})
        if any(reason["Code"] == "ConditionalCheckFailed" for reason in reasons):
            raise ClientError(
                {
                    "Error": {"Code": "TransactionCanceledException", "Message": "race"},
                    "CancellationReasons": reasons,
                },
                "TransactWriteItems",
            )
        for put in puts:
            row = put["Item"]
            self.rows[(row["PK"]["S"], row["SK"]["S"])] = deepcopy(row)
        if self.lose_acknowledgment:
            self.lose_acknowledgment = False
            raise TimeoutError("Response lost after commit")
        return {}


def history(client: AtomicDynamo, when=NOW) -> DynamoWorkspaceHistory:
    return DynamoWorkspaceHistory(client=client, table_name="history-test", clock=lambda: when)


def clear_event(**overrides: Any) -> dict[str, Any]:
    event = api_event(
        ROUTE,
        body={},
        headers={"content-type": "application/json", "idempotency-key": "clear-1"},
    )
    event["rawPath"] = PATH
    event.update(overrides)
    return event


def test_clear_persists_across_service_instances_and_replays_without_advancing() -> None:
    client = AtomicDynamo()
    assert history(client).cleared_before(OWNER) is None
    assert history(client).clear(owner_id=OWNER, idempotency_key="clear-1") == NOW
    assert (
        history(client, NOW + timedelta(days=2)).clear(owner_id=OWNER, idempotency_key="clear-1")
        == NOW
    )
    assert history(client).cleared_before(OWNER) == NOW
    assert history(client).cleared_before(OTHER_OWNER) is None
    assert len([name for name, _ in client.calls if name == "transaction"]) == 1
    assert len(client.rows) == 2
    assert all(
        "expires_at" not in row and "owner_jobs_pk" not in row for row in client.rows.values()
    )


def test_distinct_keys_advance_monotonically_even_if_a_server_clock_is_behind() -> None:
    client = AtomicDynamo()
    later = NOW + timedelta(minutes=1)
    history(client, later).clear(owner_id=OWNER, idempotency_key="first")
    assert history(client).clear(owner_id=OWNER, idempotency_key="second") == later
    assert history(client).cleared_before(OWNER) == later
    # The same browser request key is independent in a different authenticated account.
    assert history(client).clear(owner_id=OTHER_OWNER, idempotency_key="first") == NOW


@pytest.mark.parametrize("same_key", [False, True])
def test_concurrent_clears_retry_atomically_and_never_regress_the_marker(same_key: bool) -> None:
    client = AtomicDynamo()
    later = NOW + timedelta(seconds=1)
    client.before_transaction = lambda: history(client, later).clear(
        owner_id=OWNER, idempotency_key="first" if same_key else "second"
    )
    assert history(client).clear(owner_id=OWNER, idempotency_key="first") == later
    assert history(client).cleared_before(OWNER) == later
    assert len(client.rows) == (2 if same_key else 3)
    assert (
        history(client, later + timedelta(days=1)).clear(owner_id=OWNER, idempotency_key="first")
        == later
    )


def test_lost_response_is_reconciled_using_receipt_without_clearing_new_uploads() -> None:
    client = AtomicDynamo()
    client.lose_acknowledgment = True
    with pytest.raises(TimeoutError):
        history(client).clear(owner_id=OWNER, idempotency_key="retry-me")
    assert (
        history(client, NOW + timedelta(hours=1)).clear(owner_id=OWNER, idempotency_key="retry-me")
        == NOW
    )
    assert len([name for name, _ in client.calls if name == "transaction"]) == 1


def test_clear_writes_only_owner_preference_and_receipt_preserving_listing_authority() -> None:
    client = AtomicDynamo()
    preserved = {
        ("JOB#job_existing", "META"): {"payload": {"S": "immutable job"}},
        ("JOB#job_existing", "REVIEW#1"): {"payload": {"S": "approved review"}},
        ("JOB#job_existing", "PUBLICATION"): {"payload": {"S": "publication authority"}},
        ("CLEANUP#job_existing", "META"): {"payload": {"S": "active cleanup timer"}},
    }
    client.rows.update(deepcopy(preserved))
    history(client).clear(owner_id=OWNER, idempotency_key="clear-1")
    assert {key: client.rows[key] for key in preserved} == preserved
    writes = next(request for name, request in client.calls if name == "transaction")
    assert len(writes["TransactItems"]) == 2
    for operation in writes["TransactItems"]:
        assert set(operation) == {"Put"}
        row = operation["Put"]["Item"]
        assert row["PK"] == {"S": f"OWNER#{OWNER}"}
        assert row["SK"]["S"].startswith("WORKSPACE_HISTORY")
        assert operation["Put"]["ConditionExpression"] == "attribute_not_exists(PK)"


@pytest.mark.parametrize("drift", ["owner", "key", "naive", "extra", "type"])
def test_malformed_or_cross_owner_preference_fails_without_writing(drift: str) -> None:
    client = AtomicDynamo()
    history(client).clear(owner_id=OWNER, idempotency_key="clear-1")
    row = client.rows[(f"OWNER#{OWNER}", "WORKSPACE_HISTORY")]
    if drift == "owner":
        row["owner_id"] = {"S": OTHER_OWNER}
    elif drift == "key":
        row["PK"] = {"S": f"OWNER#{OTHER_OWNER}"}
    elif drift == "naive":
        row["cleared_before"] = {"S": NOW.replace(tzinfo=None).isoformat()}
    elif drift == "extra":
        row["expires_at"] = {"N": "100"}
    else:
        row["entity_type"] = {"S": "CONTROL_JOB"}
    before = deepcopy(client.rows)
    with pytest.raises(ValueError, match="preference"):
        history(client).clear(owner_id=OWNER, idempotency_key="new-key")
    assert client.rows == before


def test_filter_uses_creation_time_retains_raw_cursor_and_preserves_direct_access() -> None:
    client = AtomicDynamo()
    store = QueryStoreSpy()
    old = store.jobs[0].model_copy(update={"updated_at": NOW + timedelta(hours=1)})
    new = job_record(job_id="job_new", updated_at=NOW + timedelta(seconds=1)).model_copy(
        update={"created_at": NOW + timedelta(seconds=1)}
    )
    boundary = job_record(job_id="job_boundary").model_copy(update={"created_at": NOW})
    raw_cursor = encode_owner_job_cursor(old)

    class PagedStore(QueryStoreSpy):
        def list_jobs_for_owner(self, owner_id, *, limit=25, cursor=None):
            assert owner_id == OWNER
            assert limit == 2
            if cursor is None:
                return OwnerJobPage(jobs=(old, boundary), next_cursor=raw_cursor)
            assert cursor == raw_cursor
            return OwnerJobPage(jobs=(new,))

    underlying = PagedStore()
    history(client).clear(owner_id=OWNER, idempotency_key="clear-1")
    wrapped = HistoryFilteredJobQuery(store=underlying, history=history(client))
    page = wrapped.list_jobs_for_owner(OWNER, limit=2)
    assert page.jobs == ()
    assert page.next_cursor == raw_cursor
    assert wrapped.list_jobs_for_owner(OWNER, limit=2, cursor=page.next_cursor).jobs == (new,)
    assert wrapped.get_job_for_owner(OWNER, JOB_ID) is underlying.jobs[0]
    # The operational store and its raw data remain unfiltered for other consumers.
    assert underlying.list_jobs_for_owner(OWNER, limit=2).jobs == (old, boundary)


def test_filter_does_not_hide_cross_owner_data_errors_or_change_uncleared_accounts() -> None:
    client = AtomicDynamo()
    store = QueryStoreSpy()
    wrapped = HistoryFilteredJobQuery(store=store, history=history(client))
    assert wrapped.list_jobs_for_owner(OWNER).jobs == store.jobs
    history(client).clear(owner_id=OTHER_OWNER, idempotency_key="clear-1")
    assert wrapped.list_jobs_for_owner(OWNER).jobs == store.jobs
    store.jobs = (job_record(owner_id=OTHER_OWNER),)
    with pytest.raises(NotFoundError):
        wrapped.list_jobs_for_owner(OWNER)


def test_authenticated_clear_uses_only_jwt_owner_and_returns_noncacheable_cutoff() -> None:
    client = AtomicDynamo()
    commands = CommandSpy()
    adapter = SellerCommandApiAdapter(
        claims_policy=POLICY, commands=commands, history=history(client)
    )
    response = adapter.handle(clear_event())
    assert response["statusCode"] == 200
    assert response_body(response) == {"cleared_before": NOW.isoformat().replace("+00:00", "Z")}
    assert response["headers"]["Cache-Control"] == "no-store"
    assert not commands.calls
    assert all(key[0] == f"OWNER#{OWNER}" for key in client.rows)


@pytest.mark.parametrize(
    ("change", "status"),
    [
        ({"body": '{"owner_id":"other"}'}, 422),
        ({"body": '{"cleared_before":"2099-01-01T00:00:00Z"}'}, 422),
        ({"body": "not-json"}, 400),
        ({"body": "[]"}, 400),
        ({"body": None}, 400),
        ({"headers": {"content-type": "application/json"}}, 400),
        ({"rawPath": "/v1/jobs/other/clear"}, 400),
        ({"pathParameters": {"owner_id": OTHER_OWNER}}, 400),
        ({"queryStringParameters": {"owner_id": OTHER_OWNER}}, 400),
    ],
)
def test_invalid_clear_requests_cannot_reach_storage(change: dict, status: int) -> None:
    client = AtomicDynamo()
    adapter = SellerCommandApiAdapter(
        claims_policy=POLICY, commands=CommandSpy(), history=history(client)
    )
    assert adapter.handle(clear_event(**change))["statusCode"] == status
    assert not client.calls


def test_authentication_precedes_clear_body_parsing_and_storage() -> None:
    client = AtomicDynamo()
    event = clear_event(body="bad-json", requestContext={"requestId": "unauthorized"})
    adapter = SellerCommandApiAdapter(
        claims_policy=POLICY, commands=CommandSpy(), history=history(client)
    )
    assert adapter.handle(event)["statusCode"] == 401
    assert not client.calls


def test_cleared_history_leaves_review_and_preview_requests_unchanged() -> None:
    client = AtomicDynamo()
    history(client).clear(owner_id=OWNER, idempotency_key="clear-1")
    reviews, previews = ReviewSpy(), PreviewSpy()
    adapter = ReviewQueryApiAdapter(
        claims_policy=POLICY,
        store=HistoryFilteredJobQuery(store=QueryStoreSpy(), history=history(client)),
        reviews=reviews,
        previews=previews,
    )
    listed = adapter.handle(api_event("GET /v1/jobs"))
    assert listed["statusCode"] == 200
    assert json.loads(listed["body"])["jobs"] == []
    assert adapter.handle(api_event("GET /v1/jobs/{job_id}/review"))["statusCode"] == 200
    assert adapter.handle(api_event("GET /v1/jobs/{job_id}/artwork-preview"))["statusCode"] == 302
    assert reviews.calls == [(OWNER, JOB_ID)]
    assert previews.calls == [(OWNER, JOB_ID)]
