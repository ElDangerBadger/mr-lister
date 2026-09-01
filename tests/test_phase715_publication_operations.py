"""Strict boundary tests for source-only Phase 7 publication operations handlers."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from mr_lister.cloud.phase7_operations import (
    PUBLICATION_DUE_SWEEP_EVENT,
    Phase7OperationsExecutionError,
    Phase7OperationsInvocationError,
    Phase7PublicationDispatcherHandler,
    Phase7PublicationRecoveryHandler,
    Phase7PublicationRetentionHandler,
    build_disabled_phase7_operations_handler,
)
from mr_lister.control.publication_retention import (
    PublicationRetentionCompletionAuthority,
    publication_retention_completion_fingerprint,
)
from mr_lister.publication.application import (
    Phase7RuntimeDisabledError,
    PublicationRuntimeActivation,
)
from mr_lister.publication.orchestration import (
    PublicationDispatchDisposition,
    PublicationDispatchResult,
)
from mr_lister.publication.orchestration_dynamodb import PublicationTerminalIdentity
from mr_lister.publication.orchestration_recovery import (
    PublicationPreDispatchDeadlineEnvelope,
    PublicationRecoveryDisposition,
    PublicationRecoveryResult,
)

NOW = datetime(2026, 9, 1, 18, 0, tzinfo=UTC)
OWNER_ID = "a" * 64
AGGREGATE_ID = "publication_one"
WORK_ID = "publication_work_one"
MACHINE_ARN = "arn:aws:states:us-west-2:123456789012:stateMachine:mr-lister-phase7-dev-publication"
EXECUTION_ARN = (
    "arn:aws:states:us-west-2:123456789012:"
    "execution:mr-lister-phase7-dev-publication:publication_execution_one"
)


class RecordingDispatcher:
    def __init__(
        self,
        results: tuple[PublicationDispatchResult, ...] = (),
        error: Exception | None = None,
    ) -> None:
        self.results = results
        self.error = error
        self.calls: list[int] = []

    def dispatch_due(self, *, limit: int) -> tuple[PublicationDispatchResult, ...]:
        self.calls.append(limit)
        if self.error is not None:
            raise self.error
        return self.results


class RecordingDeadlineSink:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.envelopes: list[PublicationPreDispatchDeadlineEnvelope] = []

    def send(self, envelope: PublicationPreDispatchDeadlineEnvelope) -> None:
        self.envelopes.append(envelope)
        if self.error is not None:
            raise self.error


class RecordingRecovery:
    def __init__(
        self,
        result: PublicationRecoveryResult,
        error: Exception | None = None,
    ) -> None:
        self.result = result
        self.error = error
        self.envelopes: list[object] = []
        self.deadline_envelopes: list[object] = []

    def recover(self, envelope: object) -> PublicationRecoveryResult:
        self.envelopes.append(envelope)
        if self.error is not None:
            raise self.error
        return self.result

    def settle_pre_dispatch_deadline(self, envelope: object) -> PublicationRecoveryResult:
        self.deadline_envelopes.append(envelope)
        if self.error is not None:
            raise self.error
        return self.result


class RecordingResolver:
    def __init__(self, identity: PublicationTerminalIdentity) -> None:
        self.identity = identity
        self.calls: list[str] = []

    def resolve_terminal_identity(self, aggregate_id: str) -> PublicationTerminalIdentity:
        self.calls.append(aggregate_id)
        return self.identity


class RecordingRetention:
    def __init__(self, completion: PublicationRetentionCompletionAuthority) -> None:
        self.completion = completion
        self.calls: list[tuple[str, str]] = []

    def assign(
        self,
        *,
        owner_id: str,
        aggregate_id: str,
    ) -> PublicationRetentionCompletionAuthority:
        self.calls.append((owner_id, aggregate_id))
        return self.completion


class Explosive:
    def __call__(self, *_args: object, **_kwargs: object) -> object:
        raise AssertionError("disabled wrapper constructed or invoked its graph")

    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"unexpected dependency access: {name}")


def _stream_record(*, terminal: bool = False, event_id: str = "event_one") -> dict[str, Any]:
    return {
        "eventID": event_id,
        "eventName": "INSERT",
        "eventSource": "aws:dynamodb",
        "dynamodb": {
            "Keys": {
                "PK": {"S": f"PUBLICATION#{AGGREGATE_ID}"},
                "SK": {"S": "TERMINAL_JOB_LINK" if terminal else f"PUBLICATION_WORK#{WORK_ID}"},
            },
            "StreamViewType": "KEYS_ONLY",
        },
    }


def _sqs_event(*, body: dict[str, Any] | None = None) -> dict[str, Any]:
    payload = body or {
        "execution_arn": EXECUTION_ARN,
        "machine_arn": MACHINE_ARN,
        "status": "FAILED",
    }
    return {
        "Records": [
            {
                "messageId": "message_one",
                "eventSource": "aws:sqs",
                "body": json.dumps(payload, sort_keys=True, separators=(",", ":")),
            }
        ]
    }


def _completion() -> PublicationRetentionCompletionAuthority:
    values: dict[str, Any] = {
        "job_id": "job_one",
        "aggregate_id": AGGREGATE_ID,
        "job_record_version": 3,
        "terminal_state": "publication_failed",
        "terminal_at": NOW,
        "terminal_summary_fingerprint": "b" * 64,
        "source_artifact_fingerprint": "c" * 64,
        "aggregate_fingerprint": "d" * 64,
        "report_id": "report_one",
        "report_fingerprint": "e" * 64,
        "tombstone_fingerprint": "f" * 64,
        "terminal_job_link_fingerprint": "1" * 64,
        "source_release_eligible_at": NOW + timedelta(days=30),
        "operational_expires_at": NOW + timedelta(days=90),
        "expires_at_epoch_seconds": int((NOW + timedelta(days=90)).timestamp()),
        "publication_row_count": 12,
        "ttl_assignment_count": 14,
        "inventory_fingerprint": "2" * 64,
        "completed_at": NOW,
    }
    draft = PublicationRetentionCompletionAuthority.model_construct(
        **values,
        fingerprint="0" * 64,
    )
    values["fingerprint"] = publication_retention_completion_fingerprint(draft)
    return PublicationRetentionCompletionAuthority(**values)


def test_dispatcher_accepts_exact_schedule_and_stream_as_one_bounded_wakeup() -> None:
    results = (
        PublicationDispatchResult(
            PublicationDispatchDisposition.STARTED,
            OWNER_ID,
            AGGREGATE_ID,
            WORK_ID,
            NOW,
            "arn:started",
        ),
        PublicationDispatchResult(
            PublicationDispatchDisposition.CONFIRMED_EXISTING,
            OWNER_ID,
            AGGREGATE_ID,
            WORK_ID,
            NOW,
            "arn:existing",
        ),
        PublicationDispatchResult(
            PublicationDispatchDisposition.DEADLINE_EXPIRED,
            OWNER_ID,
            AGGREGATE_ID,
            WORK_ID,
            NOW,
            None,
        ),
    )
    dispatcher = RecordingDispatcher(results)
    deadline_sink = RecordingDeadlineSink()
    handler = Phase7PublicationDispatcherHandler(
        dispatcher=dispatcher,
        deadline_sink=deadline_sink,
    )

    scheduled = handler(PUBLICATION_DUE_SWEEP_EVENT)
    streamed = handler({"Records": [_stream_record(), _stream_record(event_id="event_two")]})

    assert dispatcher.calls == [25, 25]
    assert scheduled == {
        "contract_version": "7.0.1",
        "source": "due_sweep",
        "candidate_count": 3,
        "started_count": 1,
        "confirmed_existing_count": 1,
        "deadline_expired_count": 1,
    }
    assert streamed == {**scheduled, "source": "dynamodb_stream"}
    assert len(deadline_sink.envelopes) == 2
    assert deadline_sink.envelopes[0] == PublicationPreDispatchDeadlineEnvelope(
        owner_id=OWNER_ID,
        aggregate_id=AGGREGATE_ID,
        work_request_id=WORK_ID,
        verification_deadline=NOW,
    )


@pytest.mark.parametrize(
    "event",
    [
        {},
        {"kind": "publication_due_sweep", "extra": True},
        {"Records": []},
        {"Records": [_stream_record(event_id="same"), _stream_record(event_id="same")]},
        {"Records": [{**_stream_record(), "eventSource": "aws:s3"}]},
        {
            "Records": [
                {
                    **_stream_record(),
                    "dynamodb": {
                        **_stream_record()["dynamodb"],
                        "StreamViewType": "NEW_IMAGE",
                    },
                }
            ]
        },
    ],
)
def test_dispatcher_rejects_malformed_wakeups_before_due_query(event: dict[str, Any]) -> None:
    dispatcher = RecordingDispatcher()
    with pytest.raises(Phase7OperationsInvocationError):
        Phase7PublicationDispatcherHandler(
            dispatcher=dispatcher,
            deadline_sink=RecordingDeadlineSink(),
        )(event)
    assert dispatcher.calls == []


def test_dispatcher_dependency_failure_is_identifier_free() -> None:
    handler = Phase7PublicationDispatcherHandler(
        dispatcher=RecordingDispatcher(error=RuntimeError(AGGREGATE_ID)),
        deadline_sink=RecordingDeadlineSink(),
    )
    with pytest.raises(Phase7OperationsExecutionError) as captured:
        handler(PUBLICATION_DUE_SWEEP_EVENT)
    assert captured.value.__cause__ is None
    assert AGGREGATE_ID not in str(captured.value)


def test_dispatcher_retries_when_expired_work_cannot_reach_recovery_queue() -> None:
    expired = PublicationDispatchResult(
        PublicationDispatchDisposition.DEADLINE_EXPIRED,
        OWNER_ID,
        AGGREGATE_ID,
        WORK_ID,
        NOW,
        None,
    )
    handler = Phase7PublicationDispatcherHandler(
        dispatcher=RecordingDispatcher((expired,)),
        deadline_sink=RecordingDeadlineSink(error=RuntimeError(AGGREGATE_ID)),
    )
    with pytest.raises(Phase7OperationsExecutionError) as captured:
        handler(PUBLICATION_DUE_SWEEP_EVENT)
    assert captured.value.__cause__ is None
    assert AGGREGATE_ID not in str(captured.value)


def test_dispatcher_delivers_independent_results_before_aggregate_retry() -> None:
    results = (
        PublicationDispatchResult(
            PublicationDispatchDisposition.DEADLINE_EXPIRED,
            OWNER_ID,
            AGGREGATE_ID,
            WORK_ID,
            NOW,
            None,
        ),
        PublicationDispatchResult(
            PublicationDispatchDisposition.RETRY_REQUIRED,
            OWNER_ID,
            "publication_retry",
            "publication_work_retry",
            NOW + timedelta(minutes=1),
            None,
        ),
        PublicationDispatchResult(
            PublicationDispatchDisposition.STARTED,
            OWNER_ID,
            "publication_later",
            "publication_work_later",
            NOW + timedelta(minutes=1),
            "arn:later",
        ),
    )
    sink = RecordingDeadlineSink()
    handler = Phase7PublicationDispatcherHandler(
        dispatcher=RecordingDispatcher(results),
        deadline_sink=sink,
    )

    with pytest.raises(Phase7OperationsExecutionError):
        handler(PUBLICATION_DUE_SWEEP_EVENT)

    assert len(sink.envelopes) == 1
    assert sink.envelopes[0].aggregate_id == AGGREGATE_ID


@pytest.mark.parametrize(
    ("disposition", "failed"),
    [
        (PublicationRecoveryDisposition.REDRIVEN, False),
        (PublicationRecoveryDisposition.RUNNING, False),
        (PublicationRecoveryDisposition.PENDING_REDRIVE, False),
        (PublicationRecoveryDisposition.TERMINAL, False),
        (PublicationRecoveryDisposition.DEADLINE_SETTLED, False),
        (PublicationRecoveryDisposition.NON_REDRIVABLE, True),
    ],
)
def test_recovery_returns_only_exact_sqs_partial_batch_contract(
    disposition: PublicationRecoveryDisposition,
    failed: bool,
) -> None:
    recovery = RecordingRecovery(PublicationRecoveryResult(disposition, 0))
    response = Phase7PublicationRecoveryHandler(recovery=recovery)(_sqs_event())
    assert response == {
        "batchItemFailures": ([{"itemIdentifier": "message_one"}] if failed else [])
    }
    assert len(recovery.envelopes) == 1
    assert recovery.deadline_envelopes == []


def test_recovery_routes_pre_dispatch_deadline_without_workflow_recovery() -> None:
    recovery = RecordingRecovery(
        PublicationRecoveryResult(PublicationRecoveryDisposition.DEADLINE_SETTLED, 0)
    )
    response = Phase7PublicationRecoveryHandler(recovery=recovery)(
        _sqs_event(
            body={
                "kind": "pre_dispatch_deadline_elapsed",
                "owner_id": OWNER_ID,
                "aggregate_id": AGGREGATE_ID,
                "work_request_id": WORK_ID,
                "verification_deadline": NOW.isoformat().replace("+00:00", "Z"),
            }
        )
    )
    assert response == {"batchItemFailures": []}
    assert recovery.envelopes == []
    assert recovery.deadline_envelopes == [
        PublicationPreDispatchDeadlineEnvelope(
            owner_id=OWNER_ID,
            aggregate_id=AGGREGATE_ID,
            work_request_id=WORK_ID,
            verification_deadline=NOW,
        )
    ]


def test_recovery_malformed_event_fails_before_boundary_and_dependency_retries_message() -> None:
    recovery = RecordingRecovery(
        PublicationRecoveryResult(PublicationRecoveryDisposition.TERMINAL, 0)
    )
    handler = Phase7PublicationRecoveryHandler(recovery=recovery)
    with pytest.raises(Phase7OperationsInvocationError):
        handler(_sqs_event(body={"status": "FAILED"}))
    assert recovery.envelopes == []

    failed = Phase7PublicationRecoveryHandler(
        recovery=RecordingRecovery(
            PublicationRecoveryResult(PublicationRecoveryDisposition.TERMINAL, 0),
            error=RuntimeError(AGGREGATE_ID),
        )
    )(_sqs_event())
    assert failed == {"batchItemFailures": [{"itemIdentifier": "message_one"}]}


def test_retention_resolves_owner_strongly_and_returns_only_sanitized_counts() -> None:
    resolver = RecordingResolver(PublicationTerminalIdentity(AGGREGATE_ID, OWNER_ID))
    retention = RecordingRetention(_completion())
    response = Phase7PublicationRetentionHandler(
        resolver=resolver,
        retention=retention,
    )({"Records": [_stream_record(terminal=True)]})

    assert resolver.calls == [AGGREGATE_ID]
    assert retention.calls == [(OWNER_ID, AGGREGATE_ID)]
    assert response == {
        "contract_version": "7.0.1",
        "source": "terminal_stream",
        "retention_assigned": True,
        "publication_row_count": 12,
        "ttl_assignment_count": 14,
    }
    assert OWNER_ID not in json.dumps(response)
    assert AGGREGATE_ID not in json.dumps(response)


def test_retention_rejects_non_key_stream_and_cross_bound_identity() -> None:
    event = {"Records": [_stream_record(terminal=True)]}
    event["Records"][0]["dynamodb"]["NewImage"] = {"private": {"S": "payload"}}
    # Extra DynamoDB fields are tolerated, but KEYS_ONLY remains mandatory and only Keys are read.
    resolver = RecordingResolver(PublicationTerminalIdentity("foreign", OWNER_ID))
    handler = Phase7PublicationRetentionHandler(
        resolver=resolver,
        retention=RecordingRetention(_completion()),
    )
    with pytest.raises(Phase7OperationsExecutionError):
        handler(event)

    malformed = {"Records": [_stream_record(terminal=True)]}
    malformed["Records"][0]["dynamodb"]["StreamViewType"] = "NEW_AND_OLD_IMAGES"
    with pytest.raises(Phase7OperationsInvocationError):
        handler(malformed)


def test_disabled_wrapper_refuses_before_event_observation_or_builder() -> None:
    handler = build_disabled_phase7_operations_handler(
        PublicationRuntimeActivation(),
        builder=Explosive(),
    )
    with pytest.raises(Phase7RuntimeDisabledError):
        handler(Explosive())  # type: ignore[arg-type]
