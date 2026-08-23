"""Least-capability AWS adapters for stuck Phase 6 execution recovery.

Candidate discovery is deliberately eventually consistent and non-authoritative.  The adapter
then reads the exact Job/Work pair with ``TransactGetItems`` before the application recovery
boundary may act.  Step Functions access is DescribeExecution-only; no dispatch, stop, or
redrive operation is represented by these protocols or classes.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from typing import Any, Protocol

from pydantic import ValidationError

from mr_lister.control.execution_recovery import (
    ExecutionAuthoritySnapshot,
    ExecutionObservation,
    ExecutionRecoveryBoundaryInvalidError,
    ExecutionRecoveryDependencyUnavailableError,
    ExecutionRecoverySweepResult,
    ExecutionStatus,
    StrandedExecutionCandidate,
)
from mr_lister.control.models import ControlJobRecord, WorkRequest

EXECUTION_RECOVERY_INDEX_NAME = "ExecutionRecoveryIndex"
EXECUTION_RECOVERY_PARTITION_KEY = "WORK_RECOVERY#0"
EXECUTION_RECOVERY_METRIC_NAMESPACE = "MrLister/Phase6/ExecutionRecovery"

_BOUNDARY_INVALID = "Execution recovery AWS response is invalid"
_DEPENDENCY_UNAVAILABLE = "Execution recovery AWS dependency is unavailable"
_TABLE_NAME = re.compile(r"^[A-Za-z0-9_.-]{3,255}$")
_ENVIRONMENT = re.compile(r"^[a-z][a-z0-9-]{1,15}$")
_JOB_KEY = re.compile(r"^JOB#(?P<identifier>[A-Za-z0-9][A-Za-z0-9_-]{0,127})$")
_WORK_KEY = re.compile(r"^WORK#(?P<identifier>[A-Za-z0-9][A-Za-z0-9_-]{0,127})$")
_RECOVERY_SORT_KEY = re.compile(
    r"^(?P<epoch>[0-9]{20})#(?P<identifier>[A-Za-z0-9][A-Za-z0-9_-]{0,127})$"
)
_EXECUTION_ARN = re.compile(
    r"^arn:(aws|aws-us-gov|aws-cn):states:[a-z0-9-]+:[0-9]{12}:"
    r"execution:[A-Za-z0-9_-]{1,80}:[A-Za-z0-9_-]{1,80}$"
)
_MAX_DYNAMO_PAYLOAD_BYTES = 400 * 1024


class ExecutionRecoveryDynamoClient(Protocol):
    def query(self, **kwargs: Any) -> Mapping[str, Any]: ...

    def transact_get_items(self, **kwargs: Any) -> Mapping[str, Any]: ...


class ExecutionRecoveryStepFunctionsClient(Protocol):
    def describe_execution(self, **kwargs: Any) -> Mapping[str, Any]: ...


class DynamoDBExecutionRecoveryAuthority:
    """Discover GSI candidates and strongly rebind their exact Job/Work authority."""

    __slots__ = ("_client", "_table_name")

    def __init__(
        self,
        *,
        client: ExecutionRecoveryDynamoClient,
        table_name: str,
    ) -> None:
        if not isinstance(table_name, str) or _TABLE_NAME.fullmatch(table_name) is None:
            raise ValueError("Execution recovery table configuration is invalid")
        self._client = client
        self._table_name = table_name

    def list_stranded_execution_candidates(
        self,
        *,
        dispatched_before: datetime,
        limit: int,
    ) -> tuple[StrandedExecutionCandidate, ...]:
        if (
            not isinstance(dispatched_before, datetime)
            or dispatched_before.tzinfo is None
            or dispatched_before.utcoffset() is None
            or type(limit) is not int
            or not 1 <= limit <= 100
        ):
            raise ExecutionRecoveryBoundaryInvalidError(_BOUNDARY_INVALID)
        cutoff_epoch = int(dispatched_before.timestamp())
        if not 0 <= cutoff_epoch <= 253_402_300_799:
            raise ExecutionRecoveryBoundaryInvalidError(_BOUNDARY_INVALID)
        cutoff_sort_key = f"{cutoff_epoch:020d}#~"
        try:
            response = self._client.query(
                TableName=self._table_name,
                IndexName=EXECUTION_RECOVERY_INDEX_NAME,
                KeyConditionExpression=(
                    "recovery_pk = :recovery_pk AND recovery_sk <= :recovery_sk"
                ),
                ExpressionAttributeValues={
                    ":recovery_pk": _s(EXECUTION_RECOVERY_PARTITION_KEY),
                    ":recovery_sk": _s(cutoff_sort_key),
                },
                ProjectionExpression="PK, SK, recovery_pk, recovery_sk",
                ScanIndexForward=True,
                Limit=limit,
            )
        except Exception:
            raise ExecutionRecoveryDependencyUnavailableError(_DEPENDENCY_UNAVAILABLE) from None
        try:
            return _parse_candidate_response(
                response,
                limit=limit,
                cutoff_epoch=cutoff_epoch,
            )
        except ExecutionRecoveryBoundaryInvalidError:
            raise
        except Exception:
            raise ExecutionRecoveryBoundaryInvalidError(_BOUNDARY_INVALID) from None

    def read_execution_authority_strong(
        self,
        *,
        job_id: str,
        work_request_id: str,
    ) -> ExecutionAuthoritySnapshot:
        try:
            identity = StrandedExecutionCandidate(
                job_id=job_id,
                work_request_id=work_request_id,
            )
        except ValidationError:
            raise ExecutionRecoveryBoundaryInvalidError(_BOUNDARY_INVALID) from None
        job_key = f"JOB#{identity.job_id}"
        work_key = f"WORK#{identity.work_request_id}"
        try:
            response = self._client.transact_get_items(
                TransactItems=[
                    {
                        "Get": {
                            "TableName": self._table_name,
                            "Key": {"PK": _s(job_key), "SK": _s("META")},
                            "ProjectionExpression": "PK, SK, entity_type, payload",
                        }
                    },
                    {
                        "Get": {
                            "TableName": self._table_name,
                            "Key": {"PK": _s(job_key), "SK": _s(work_key)},
                            "ProjectionExpression": "PK, SK, entity_type, payload",
                        }
                    },
                ]
            )
        except Exception:
            raise ExecutionRecoveryDependencyUnavailableError(_DEPENDENCY_UNAVAILABLE) from None
        try:
            return _parse_authority_response(
                response,
                job_id=identity.job_id,
                work_request_id=identity.work_request_id,
            )
        except ExecutionRecoveryBoundaryInvalidError:
            raise
        except Exception:
            raise ExecutionRecoveryBoundaryInvalidError(_BOUNDARY_INVALID) from None


class StepFunctionsExactExecutionObserver:
    """Describe one exact Standard execution and expose no mutation operation."""

    __slots__ = ("_client",)

    def __init__(self, *, client: ExecutionRecoveryStepFunctionsClient) -> None:
        self._client = client

    def describe_exact_execution(self, *, execution_arn: str) -> ExecutionObservation | None:
        if not isinstance(execution_arn, str) or _EXECUTION_ARN.fullmatch(execution_arn) is None:
            raise ExecutionRecoveryBoundaryInvalidError(_BOUNDARY_INVALID)
        try:
            response = self._client.describe_execution(executionArn=execution_arn)
        except Exception as error:
            if _error_code(error) == "ExecutionDoesNotExist":
                return None
            raise ExecutionRecoveryDependencyUnavailableError(_DEPENDENCY_UNAVAILABLE) from None
        try:
            if not isinstance(response, Mapping) or response.get("executionArn") != execution_arn:
                raise ValueError
            raw_status = response.get("status")
            if not isinstance(raw_status, str):
                raise ValueError
            status = ExecutionStatus(raw_status)
            material: dict[str, object] = {
                "execution_arn": response.get("executionArn"),
                "state_machine_arn": response.get("stateMachineArn"),
                "name": response.get("name"),
                "input": response.get("input"),
                "status": status,
                "start_date": response.get("startDate"),
            }
            if "stopDate" in response:
                material["stop_date"] = response.get("stopDate")
            return ExecutionObservation(**material)
        except (TypeError, ValueError, ValidationError):
            raise ExecutionRecoveryBoundaryInvalidError(_BOUNDARY_INVALID) from None


class EmbeddedExecutionRecoveryMetrics:
    """Emit one identifier-free CloudWatch EMF document for a sanitized sweep result."""

    __slots__ = ("_clock", "_environment", "_logger")

    _FIELDS = (
        ("RecoveryRuns", None),
        ("CandidatesScanned", "candidates_scanned"),
        ("AlreadySettled", "already_settled"),
        ("NotDue", "not_due"),
        ("RunningPastBound", "running_past_bound"),
        ("RecoveredCompletion", "recovered_completion"),
        ("FailureSettled", "failure_settled"),
        ("ReconciliationRouted", "reconciliation_routed"),
        ("CancellationSettled", "cancellation_settled"),
        ("AuthorityConflicts", "authority_conflicts"),
        ("DependencyUnavailable", "dependency_unavailable"),
        ("SettlementExhausted", "settlement_exhausted"),
        ("TerminalExecutionsObserved", "terminal_executions_observed"),
        ("ExecutionsMissing", "executions_missing"),
        ("BatchLimitReached", "batch_limit_reached"),
        ("AlarmSignals", "alarm_signal_count"),
        ("RequiresOperatorAttention", "requires_operator_attention"),
    )

    def __init__(
        self,
        *,
        environment_name: str,
        logger: Callable[[str], object] = print,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if (
            not isinstance(environment_name, str)
            or _ENVIRONMENT.fullmatch(environment_name) is None
            or not callable(logger)
        ):
            raise ValueError("Execution recovery metric configuration is invalid")
        self._environment = environment_name
        self._logger = logger
        self._clock = clock or (lambda: datetime.now(UTC))

    def emit(self, result: ExecutionRecoverySweepResult) -> None:
        """Write one bounded EMF line containing counters and an environment dimension only."""

        if not isinstance(result, ExecutionRecoverySweepResult):
            raise ExecutionRecoveryBoundaryInvalidError(_BOUNDARY_INVALID)
        now = self._clock()
        if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
            raise ExecutionRecoveryBoundaryInvalidError(_BOUNDARY_INVALID)
        metrics: list[dict[str, str]] = []
        document: dict[str, object] = {
            "Environment": self._environment,
            "ContractVersion": result.contract_version,
        }
        for metric_name, field_name in self._FIELDS:
            metrics.append({"Name": metric_name, "Unit": "Count"})
            if field_name is None:
                value = 1
            else:
                raw = getattr(result, field_name)
                value = int(raw) if isinstance(raw, bool) else raw
            document[metric_name] = value
        document["_aws"] = {
            "Timestamp": int(now.timestamp() * 1000),
            "CloudWatchMetrics": [
                {
                    "Namespace": EXECUTION_RECOVERY_METRIC_NAMESPACE,
                    "Dimensions": [["Environment"]],
                    "Metrics": metrics,
                }
            ],
        }
        rendered = json.dumps(
            document,
            allow_nan=False,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
        try:
            self._logger(rendered)
        except Exception:
            raise ExecutionRecoveryDependencyUnavailableError(_DEPENDENCY_UNAVAILABLE) from None


def _parse_candidate_response(
    response: Mapping[str, Any],
    *,
    limit: int,
    cutoff_epoch: int,
) -> tuple[StrandedExecutionCandidate, ...]:
    if not isinstance(response, Mapping):
        raise ExecutionRecoveryBoundaryInvalidError(_BOUNDARY_INVALID)
    raw_items = response.get("Items")
    if (
        not isinstance(raw_items, Sequence)
        or isinstance(raw_items, (str, bytes, bytearray))
        or len(raw_items) > limit
    ):
        raise ExecutionRecoveryBoundaryInvalidError(_BOUNDARY_INVALID)
    count = response.get("Count")
    if count is not None and (type(count) is not int or count != len(raw_items)):
        raise ExecutionRecoveryBoundaryInvalidError(_BOUNDARY_INVALID)
    candidates: list[StrandedExecutionCandidate] = []
    prior_sort_key: str | None = None
    for raw_item in raw_items:
        if not isinstance(raw_item, Mapping) or set(raw_item) != {
            "PK",
            "SK",
            "recovery_pk",
            "recovery_sk",
        }:
            raise ExecutionRecoveryBoundaryInvalidError(_BOUNDARY_INVALID)
        partition_key = _string_attribute(raw_item.get("PK"))
        sort_key = _string_attribute(raw_item.get("SK"))
        recovery_pk = _string_attribute(raw_item.get("recovery_pk"))
        recovery_sk = _string_attribute(raw_item.get("recovery_sk"))
        job_match = _JOB_KEY.fullmatch(partition_key)
        work_match = _WORK_KEY.fullmatch(sort_key)
        recovery_match = _RECOVERY_SORT_KEY.fullmatch(recovery_sk)
        if (
            recovery_pk != EXECUTION_RECOVERY_PARTITION_KEY
            or job_match is None
            or work_match is None
            or recovery_match is None
            or recovery_match.group("identifier") != work_match.group("identifier")
            or int(recovery_match.group("epoch")) > cutoff_epoch
            or (prior_sort_key is not None and recovery_sk < prior_sort_key)
        ):
            raise ExecutionRecoveryBoundaryInvalidError(_BOUNDARY_INVALID)
        prior_sort_key = recovery_sk
        candidates.append(
            StrandedExecutionCandidate(
                job_id=job_match.group("identifier"),
                work_request_id=work_match.group("identifier"),
            )
        )
    return tuple(candidates)


def _parse_authority_response(
    response: Mapping[str, Any],
    *,
    job_id: str,
    work_request_id: str,
) -> ExecutionAuthoritySnapshot:
    if not isinstance(response, Mapping):
        raise ExecutionRecoveryBoundaryInvalidError(_BOUNDARY_INVALID)
    raw_responses = response.get("Responses")
    if (
        not isinstance(raw_responses, Sequence)
        or isinstance(raw_responses, (str, bytes, bytearray))
        or len(raw_responses) != 2
        or any(not isinstance(item, Mapping) for item in raw_responses)
    ):
        raise ExecutionRecoveryBoundaryInvalidError(_BOUNDARY_INVALID)
    first, second = raw_responses
    job_absent = not first
    work_absent = not second
    if job_absent and work_absent:
        return ExecutionAuthoritySnapshot()
    if job_absent or work_absent or set(first) != {"Item"} or set(second) != {"Item"}:
        raise ExecutionRecoveryBoundaryInvalidError(_BOUNDARY_INVALID)
    raw_job = first.get("Item")
    raw_work = second.get("Item")
    job = _parse_record(
        raw_job,
        expected_partition=f"JOB#{job_id}",
        expected_sort="META",
        expected_entity="CONTROL_JOB",
        model=ControlJobRecord,
    )
    work = _parse_record(
        raw_work,
        expected_partition=f"JOB#{job_id}",
        expected_sort=f"WORK#{work_request_id}",
        expected_entity="WORK_REQUEST",
        model=WorkRequest,
    )
    if (
        job.job_id != job_id
        or work.job_id != job_id
        or work.work_request_id != work_request_id
        or work.owner_id != job.owner_id
    ):
        raise ExecutionRecoveryBoundaryInvalidError(_BOUNDARY_INVALID)
    return ExecutionAuthoritySnapshot(job=job, work=work)


def _parse_record(
    raw_item: object,
    *,
    expected_partition: str,
    expected_sort: str,
    expected_entity: str,
    model: type[ControlJobRecord] | type[WorkRequest],
) -> ControlJobRecord | WorkRequest:
    if not isinstance(raw_item, Mapping) or set(raw_item) != {
        "PK",
        "SK",
        "entity_type",
        "payload",
    }:
        raise ExecutionRecoveryBoundaryInvalidError(_BOUNDARY_INVALID)
    if (
        _string_attribute(raw_item.get("PK")) != expected_partition
        or _string_attribute(raw_item.get("SK")) != expected_sort
        or _string_attribute(raw_item.get("entity_type")) != expected_entity
    ):
        raise ExecutionRecoveryBoundaryInvalidError(_BOUNDARY_INVALID)
    payload = _string_attribute(raw_item.get("payload"))
    if not 1 <= len(payload.encode("utf-8")) <= _MAX_DYNAMO_PAYLOAD_BYTES:
        raise ExecutionRecoveryBoundaryInvalidError(_BOUNDARY_INVALID)
    return model.model_validate_json(payload, strict=True)


def _string_attribute(value: object) -> str:
    if not isinstance(value, Mapping) or set(value) != {"S"}:
        raise ExecutionRecoveryBoundaryInvalidError(_BOUNDARY_INVALID)
    text = value.get("S")
    if not isinstance(text, str):
        raise ExecutionRecoveryBoundaryInvalidError(_BOUNDARY_INVALID)
    return text


def _s(value: str) -> dict[str, str]:
    return {"S": value}


def _error_code(error: Exception) -> str | None:
    response = getattr(error, "response", None)
    if not isinstance(response, Mapping):
        return None
    detail = response.get("Error")
    if not isinstance(detail, Mapping):
        return None
    code = detail.get("Code")
    return code if isinstance(code, str) else None


__all__ = [
    "DynamoDBExecutionRecoveryAuthority",
    "EXECUTION_RECOVERY_INDEX_NAME",
    "EXECUTION_RECOVERY_METRIC_NAMESPACE",
    "EXECUTION_RECOVERY_PARTITION_KEY",
    "EmbeddedExecutionRecoveryMetrics",
    "ExecutionRecoveryDynamoClient",
    "ExecutionRecoveryStepFunctionsClient",
    "StepFunctionsExactExecutionObserver",
]
