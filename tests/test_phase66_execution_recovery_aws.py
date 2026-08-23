from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from mr_lister.control.dispatch import (
    deterministic_execution_name,
    execution_arn_for,
    work_input_fingerprint,
)
from mr_lister.control.execution_recovery import (
    ExecutionRecoveryBoundaryInvalidError,
    ExecutionRecoveryDependencyUnavailableError,
    ExecutionRecoverySweepResult,
    ExecutionStatus,
)
from mr_lister.control.execution_recovery_aws import (
    DynamoDBExecutionRecoveryAuthority,
    EmbeddedExecutionRecoveryMetrics,
    StepFunctionsExactExecutionObserver,
)
from mr_lister.control.models import (
    ControlJobRecord,
    ControlJobState,
    WorkRequest,
    WorkRequestStatus,
    WorkType,
)

NOW = datetime(2026, 8, 23, 17, 0, tzinfo=UTC)
CUTOFF = NOW - timedelta(minutes=20)
OWNER_ID = "a" * 64
JOB_ID = "job_recovery_aws"
WORK_ID = "work_recovery_aws"
TABLE = "mr-lister-phase6-dev"
MACHINE_ARN = (
    "arn:aws:states:us-west-2:123456789012:stateMachine:mr-lister-phase6-dev-synchronize-product"
)


def _work() -> WorkRequest:
    name = deterministic_execution_name(WORK_ID)
    return WorkRequest(
        work_request_id=WORK_ID,
        owner_id=OWNER_ID,
        job_id=JOB_ID,
        receipt_id="receipt_recovery_aws",
        work_type=WorkType.SYNCHRONIZE_PRODUCT,
        review_version=1,
        input_fingerprint=work_input_fingerprint(
            work_type=WorkType.SYNCHRONIZE_PRODUCT,
            job_id=JOB_ID,
            work_request_id=WORK_ID,
        ),
        execution_name=name,
        status=WorkRequestStatus.DISPATCHED,
        attempt_count=1,
        next_dispatch_at=CUTOFF - timedelta(minutes=10),
        execution_arn=execution_arn_for(MACHINE_ARN, name),
        created_at=CUTOFF - timedelta(minutes=11),
        updated_at=CUTOFF - timedelta(minutes=10),
    )


def _job() -> ControlJobRecord:
    return ControlJobRecord(
        owner_id=OWNER_ID,
        job_id=JOB_ID,
        record_version=7,
        event_sequence=7,
        state=ControlJobState.PRODUCT_DRAFT_SYNCING,
        review_version=1,
        review_fingerprint="b" * 64,
        active_work_request_id=WORK_ID,
        created_at=CUTOFF - timedelta(hours=1),
        updated_at=CUTOFF - timedelta(minutes=10),
    )


def _candidate_item(*, recovery_sk: str | None = None) -> dict[str, Any]:
    return {
        "PK": {"S": f"JOB#{JOB_ID}"},
        "SK": {"S": f"WORK#{WORK_ID}"},
        "recovery_pk": {"S": "WORK_RECOVERY#0"},
        "recovery_sk": {
            "S": recovery_sk or f"{int(_work().updated_at.timestamp()):020d}#{WORK_ID}"
        },
    }


def _record_item(
    record: ControlJobRecord | WorkRequest,
    *,
    sort_key: str,
    entity_type: str,
    payload: str | None = None,
) -> dict[str, Any]:
    return {
        "PK": {"S": f"JOB#{JOB_ID}"},
        "SK": {"S": sort_key},
        "entity_type": {"S": entity_type},
        "payload": {"S": payload or record.model_dump_json()},
    }


class FakeDynamo:
    def __init__(self) -> None:
        self.query_response: object = {"Items": [], "Count": 0}
        self.transact_response: object = {"Responses": [{}, {}]}
        self.query_error: Exception | None = None
        self.transact_error: Exception | None = None
        self.query_calls: list[dict[str, Any]] = []
        self.transact_calls: list[dict[str, Any]] = []

    def query(self, **kwargs: Any) -> Any:
        self.query_calls.append(kwargs)
        if self.query_error is not None:
            raise self.query_error
        return self.query_response

    def transact_get_items(self, **kwargs: Any) -> Any:
        self.transact_calls.append(kwargs)
        if self.transact_error is not None:
            raise self.transact_error
        return self.transact_response


class FakeStepFunctions:
    def __init__(self, response: object = None, error: Exception | None = None) -> None:
        self.response = response
        self.error = error
        self.calls: list[dict[str, Any]] = []

    def describe_execution(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return self.response


class AwsStyleError(Exception):
    def __init__(self, code: str, message: str = "private dependency detail") -> None:
        super().__init__(message)
        self.response = {"Error": {"Code": code, "Message": message}}


def test_candidate_inventory_queries_only_exact_gsi_cutoff_and_projection() -> None:
    client = FakeDynamo()
    client.query_response = {"Items": [_candidate_item()], "Count": 1, "ScannedCount": 1}
    adapter = DynamoDBExecutionRecoveryAuthority(client=client, table_name=TABLE)

    candidates = adapter.list_stranded_execution_candidates(
        dispatched_before=CUTOFF,
        limit=25,
    )

    assert [(item.job_id, item.work_request_id) for item in candidates] == [(JOB_ID, WORK_ID)]
    assert client.query_calls == [
        {
            "TableName": TABLE,
            "IndexName": "ExecutionRecoveryIndex",
            "KeyConditionExpression": (
                "recovery_pk = :recovery_pk AND recovery_sk <= :recovery_sk"
            ),
            "ExpressionAttributeValues": {
                ":recovery_pk": {"S": "WORK_RECOVERY#0"},
                ":recovery_sk": {"S": f"{int(CUTOFF.timestamp()):020d}#~"},
            },
            "ProjectionExpression": "PK, SK, recovery_pk, recovery_sk",
            "ScanIndexForward": True,
            "Limit": 25,
        }
    ]


@pytest.mark.parametrize(
    "response",
    [
        {"Items": [_candidate_item()], "Count": 2},
        {"Items": [{**_candidate_item(), "payload": {"S": "private"}}]},
        {
            "Items": [
                _candidate_item(
                    recovery_sk=f"{int((CUTOFF + timedelta(seconds=1)).timestamp()):020d}#{WORK_ID}"
                )
            ]
        },
        {"Items": "not-a-sequence"},
    ],
)
def test_candidate_inventory_rejects_false_or_overbroad_evidence(response: object) -> None:
    client = FakeDynamo()
    client.query_response = response
    adapter = DynamoDBExecutionRecoveryAuthority(client=client, table_name=TABLE)

    with pytest.raises(ExecutionRecoveryBoundaryInvalidError) as raised:
        adapter.list_stranded_execution_candidates(dispatched_before=CUTOFF, limit=25)

    assert str(raised.value) == "Execution recovery AWS response is invalid"
    assert "private" not in str(raised.value)


def test_candidate_inventory_dependency_error_is_identifier_free() -> None:
    client = FakeDynamo()
    client.query_error = RuntimeError(f"private {TABLE} detail")
    adapter = DynamoDBExecutionRecoveryAuthority(client=client, table_name=TABLE)

    with pytest.raises(ExecutionRecoveryDependencyUnavailableError) as raised:
        adapter.list_stranded_execution_candidates(dispatched_before=CUTOFF, limit=25)

    assert str(raised.value) == "Execution recovery AWS dependency is unavailable"
    assert TABLE not in str(raised.value)


def test_authority_uses_one_paired_transact_get_and_strict_json_roundtrip() -> None:
    client = FakeDynamo()
    client.transact_response = {
        "Responses": [
            {"Item": _record_item(_job(), sort_key="META", entity_type="CONTROL_JOB")},
            {
                "Item": _record_item(
                    _work(),
                    sort_key=f"WORK#{WORK_ID}",
                    entity_type="WORK_REQUEST",
                )
            },
        ]
    }
    adapter = DynamoDBExecutionRecoveryAuthority(client=client, table_name=TABLE)

    snapshot = adapter.read_execution_authority_strong(
        job_id=JOB_ID,
        work_request_id=WORK_ID,
    )

    assert snapshot.job == _job()
    assert snapshot.work == _work()
    assert client.transact_calls == [
        {
            "TransactItems": [
                {
                    "Get": {
                        "TableName": TABLE,
                        "Key": {"PK": {"S": f"JOB#{JOB_ID}"}, "SK": {"S": "META"}},
                        "ProjectionExpression": "PK, SK, entity_type, payload",
                    }
                },
                {
                    "Get": {
                        "TableName": TABLE,
                        "Key": {
                            "PK": {"S": f"JOB#{JOB_ID}"},
                            "SK": {"S": f"WORK#{WORK_ID}"},
                        },
                        "ProjectionExpression": "PK, SK, entity_type, payload",
                    }
                },
            ]
        }
    ]


def test_authority_strict_json_rejects_scalar_coercion() -> None:
    job_payload = _job().model_dump(mode="json")
    job_payload["record_version"] = "7"
    client = FakeDynamo()
    client.transact_response = {
        "Responses": [
            {
                "Item": _record_item(
                    _job(),
                    sort_key="META",
                    entity_type="CONTROL_JOB",
                    payload=json.dumps(job_payload),
                )
            },
            {
                "Item": _record_item(
                    _work(),
                    sort_key=f"WORK#{WORK_ID}",
                    entity_type="WORK_REQUEST",
                )
            },
        ]
    }
    adapter = DynamoDBExecutionRecoveryAuthority(client=client, table_name=TABLE)

    with pytest.raises(ExecutionRecoveryBoundaryInvalidError):
        adapter.read_execution_authority_strong(job_id=JOB_ID, work_request_id=WORK_ID)


def test_authority_rejects_half_missing_transaction_instead_of_inventing_absence() -> None:
    client = FakeDynamo()
    client.transact_response = {
        "Responses": [
            {"Item": _record_item(_job(), sort_key="META", entity_type="CONTROL_JOB")},
            {},
        ]
    }
    adapter = DynamoDBExecutionRecoveryAuthority(client=client, table_name=TABLE)

    with pytest.raises(ExecutionRecoveryBoundaryInvalidError):
        adapter.read_execution_authority_strong(job_id=JOB_ID, work_request_id=WORK_ID)

    client.transact_response = {"Responses": [{}, {}]}
    assert (
        adapter.read_execution_authority_strong(
            job_id=JOB_ID,
            work_request_id=WORK_ID,
        ).job
        is None
    )


def test_authority_dependency_failure_is_sanitized() -> None:
    client = FakeDynamo()
    client.transact_error = RuntimeError(f"private {JOB_ID}")
    adapter = DynamoDBExecutionRecoveryAuthority(client=client, table_name=TABLE)

    with pytest.raises(ExecutionRecoveryDependencyUnavailableError) as raised:
        adapter.read_execution_authority_strong(job_id=JOB_ID, work_request_id=WORK_ID)

    assert JOB_ID not in str(raised.value)


def _execution_response(*, status: str = "FAILED") -> dict[str, Any]:
    work = _work()
    response: dict[str, Any] = {
        "executionArn": work.execution_arn,
        "stateMachineArn": MACHINE_ARN,
        "name": work.execution_name,
        "status": status,
        "startDate": work.updated_at,
        "input": json.dumps(
            {"job_id": JOB_ID, "work_request_id": WORK_ID},
            sort_keys=True,
            separators=(",", ":"),
        ),
    }
    if status != "RUNNING":
        response["stopDate"] = NOW - timedelta(minutes=5)
    return response


def test_step_functions_observer_describes_only_the_exact_arn() -> None:
    client = FakeStepFunctions(_execution_response(status="TIMED_OUT"))
    observer = StepFunctionsExactExecutionObserver(client=client)

    observation = observer.describe_exact_execution(execution_arn=_work().execution_arn)

    assert observation is not None
    assert observation.status is ExecutionStatus.TIMED_OUT
    assert observation.execution_arn == _work().execution_arn
    assert client.calls == [{"executionArn": _work().execution_arn}]
    assert set(observer.__slots__) == {"_client"}


def test_only_exact_execution_does_not_exist_maps_to_absence() -> None:
    missing = StepFunctionsExactExecutionObserver(
        client=FakeStepFunctions(error=AwsStyleError("ExecutionDoesNotExist"))
    )
    assert missing.describe_exact_execution(execution_arn=_work().execution_arn) is None

    unavailable = StepFunctionsExactExecutionObserver(
        client=FakeStepFunctions(error=AwsStyleError("AccessDeniedException", JOB_ID))
    )
    with pytest.raises(ExecutionRecoveryDependencyUnavailableError) as raised:
        unavailable.describe_exact_execution(execution_arn=_work().execution_arn)
    assert JOB_ID not in str(raised.value)


@pytest.mark.parametrize(
    "response",
    [
        {**_execution_response(), "executionArn": _work().execution_arn + "-other"},
        _execution_response(status="UNKNOWN"),
        {**_execution_response(), "input": {"job_id": JOB_ID}},
        {key: value for key, value in _execution_response().items() if key != "stopDate"},
    ],
)
def test_step_functions_observer_rejects_false_positive_shapes(response: object) -> None:
    observer = StepFunctionsExactExecutionObserver(client=FakeStepFunctions(response))

    with pytest.raises(ExecutionRecoveryBoundaryInvalidError):
        observer.describe_exact_execution(execution_arn=_work().execution_arn)


def _result() -> ExecutionRecoverySweepResult:
    return ExecutionRecoverySweepResult(
        candidates_scanned=3,
        already_settled=0,
        not_due=0,
        running_past_bound=1,
        recovered_completion=0,
        failure_settled=1,
        reconciliation_routed=0,
        cancellation_settled=0,
        authority_conflicts=1,
        dependency_unavailable=0,
        settlement_exhausted=0,
        terminal_executions_observed=1,
        executions_missing=0,
        batch_limit=25,
        batch_limit_reached=False,
        alarm_signal_count=2,
        requires_operator_attention=True,
    )


def test_emf_metrics_use_dedicated_namespace_and_no_authority_dimensions() -> None:
    lines: list[str] = []
    emitter = EmbeddedExecutionRecoveryMetrics(
        environment_name="dev",
        logger=lines.append,
        clock=lambda: NOW,
    )

    emitter.emit(_result())

    assert len(lines) == 1
    document = json.loads(lines[0])
    directive = document["_aws"]["CloudWatchMetrics"][0]
    names = {metric["Name"] for metric in directive["Metrics"]}
    assert directive["Namespace"] == "MrLister/Phase6/ExecutionRecovery"
    assert directive["Dimensions"] == [["Environment"]]
    assert {
        "AlarmSignals",
        "AuthorityConflicts",
        "DependencyUnavailable",
        "SettlementExhausted",
        "RunningPastBound",
        "BatchLimitReached",
    }.issubset(names)
    assert document["AlarmSignals"] == 2
    assert document["Environment"] == "dev"
    assert JOB_ID not in lines[0]
    assert WORK_ID not in lines[0]
    assert "executionArn" not in lines[0]


def test_emf_logger_failure_is_identifier_free() -> None:
    def fail(_line: str) -> None:
        raise RuntimeError(f"private {JOB_ID}")

    emitter = EmbeddedExecutionRecoveryMetrics(
        environment_name="dev",
        logger=fail,
        clock=lambda: NOW,
    )
    with pytest.raises(ExecutionRecoveryDependencyUnavailableError) as raised:
        emitter.emit(_result())
    assert JOB_ID not in str(raised.value)
