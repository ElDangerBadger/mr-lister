from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from mr_lister.cloud.phase6_execution_recovery_composition import (
    Phase6ExecutionRecoveryConfigurationError,
    build_execution_recovery_handler,
    load_execution_recovery_configuration,
)
from mr_lister.control.execution_recovery import ExecutionRecoveryInvocationError
from mr_lister.control.models import WorkType

NOW = datetime(2026, 8, 23, 17, 30, tzinfo=UTC)
ACCOUNT = "123456789012"
REGION = "us-west-2"
ENVIRONMENT = "dev"
RELEASE = "d" * 64


def _environment() -> dict[str, object]:
    prefix = f"arn:aws:states:{REGION}:{ACCOUNT}:stateMachine:mr-lister-phase6-{ENVIRONMENT}"
    return {
        "AWS_REGION": REGION,
        "MR_LISTER_ENVIRONMENT": ENVIRONMENT,
        "MR_LISTER_AWS_ACCOUNT_ID": ACCOUNT,
        "MR_LISTER_STATE_TABLE": f"mr-lister-phase6-{ENVIRONMENT}",
        "MR_LISTER_RELEASE_FINGERPRINT": RELEASE,
        "MR_LISTER_PREPARE_MACHINE_ARN": f"{prefix}-prepare",
        "MR_LISTER_SYNCHRONIZE_PRODUCT_MACHINE_ARN": f"{prefix}-synchronize-product",
        "MR_LISTER_RECONCILE_PRODUCT_MACHINE_ARN": f"{prefix}-reconcile-product",
        "MR_LISTER_REFRESH_ECONOMICS_MACHINE_ARN": f"{prefix}-refresh-economics",
        "MR_LISTER_EXECUTION_RECOVERY_STALE_SECONDS": "1200",
        "MR_LISTER_EXECUTION_RECOVERY_BATCH_LIMIT": "25",
        "MR_LISTER_EXECUTION_RECOVERY_MAX_CAS_RECHECKS": "2",
    }


class FakeDynamo:
    def __init__(self) -> None:
        self.query_calls: list[dict[str, Any]] = []

    def query(self, **kwargs: Any) -> dict[str, Any]:
        self.query_calls.append(kwargs)
        return {"Items": [], "Count": 0}

    def transact_get_items(self, **kwargs: Any) -> dict[str, Any]:
        del kwargs
        return {"Responses": [{}, {}]}

    def get_item(self, **kwargs: Any) -> dict[str, Any]:
        del kwargs
        return {}

    def put_item(self, **kwargs: Any) -> dict[str, Any]:
        del kwargs
        return {}

    def transact_write_items(self, **kwargs: Any) -> dict[str, Any]:
        del kwargs
        return {}


class DescribeOnlyStepFunctions:
    def describe_execution(self, **kwargs: Any) -> dict[str, Any]:
        del kwargs
        return {}


class RecordingFactory:
    def __init__(self) -> None:
        self.dynamodb = FakeDynamo()
        self.step_functions = DescribeOnlyStepFunctions()
        self.calls: list[tuple[str, str]] = []

    def __call__(self, service_name: str, *, region_name: str) -> object:
        self.calls.append((service_name, region_name))
        return {
            "dynamodb": self.dynamodb,
            "stepfunctions": self.step_functions,
        }[service_name]


def test_configuration_binds_exact_release_table_machines_and_bounds() -> None:
    configuration = load_execution_recovery_configuration(_environment())

    assert configuration.region == REGION
    assert configuration.environment_name == ENVIRONMENT
    assert configuration.account_id == ACCOUNT
    assert configuration.state_table == "mr-lister-phase6-dev"
    assert configuration.release_fingerprint == RELEASE
    assert configuration.stale_after == timedelta(minutes=20)
    assert configuration.batch_limit == 25
    assert configuration.maximum_cas_rechecks == 2
    assert set(configuration.state_machine_arns) == set(WorkType)


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("AWS_REGION", "US-WEST-2"),
        ("MR_LISTER_ENVIRONMENT", "Dev"),
        ("MR_LISTER_AWS_ACCOUNT_ID", "000000000000"),
        ("MR_LISTER_STATE_TABLE", "other-table"),
        ("MR_LISTER_RELEASE_FINGERPRINT", "0" * 64),
        ("MR_LISTER_EXECUTION_RECOVERY_STALE_SECONDS", "1199"),
        ("MR_LISTER_EXECUTION_RECOVERY_STALE_SECONDS", "1200.0"),
        ("MR_LISTER_EXECUTION_RECOVERY_BATCH_LIMIT", "0"),
        ("MR_LISTER_EXECUTION_RECOVERY_BATCH_LIMIT", "025"),
        ("MR_LISTER_EXECUTION_RECOVERY_MAX_CAS_RECHECKS", "4"),
        ("MR_LISTER_EXECUTION_RECOVERY_MAX_CAS_RECHECKS", 2),
    ],
)
def test_configuration_rejects_drift_without_echoing_values(name: str, value: object) -> None:
    environment = _environment()
    environment[name] = value

    with pytest.raises(Phase6ExecutionRecoveryConfigurationError) as raised:
        load_execution_recovery_configuration(environment)

    assert str(raised.value) == "Phase 6 execution recovery configuration is invalid"
    assert str(value) not in str(raised.value)


def test_configuration_rejects_cross_account_or_wrong_machine_name() -> None:
    for changed in (
        ("arn:aws:states:us-west-2:999999999999:stateMachine:mr-lister-phase6-dev-prepare"),
        ("arn:aws:states:us-west-2:123456789012:stateMachine:mr-lister-phase6-dev-publish"),
    ):
        environment = _environment()
        environment["MR_LISTER_PREPARE_MACHINE_ARN"] = changed
        with pytest.raises(Phase6ExecutionRecoveryConfigurationError):
            load_execution_recovery_configuration(environment)


def test_composition_creates_only_dynamo_and_describe_only_step_functions_clients() -> None:
    factory = RecordingFactory()
    metrics: list[str] = []
    handler = build_execution_recovery_handler(
        _environment(),
        client_factory=factory,
        metric_logger=metrics.append,
        clock=lambda: NOW,
    )

    response = handler({"source": "stuck-execution-sweeper"})

    assert factory.calls == [("dynamodb", REGION), ("stepfunctions", REGION)]
    assert not hasattr(factory.step_functions, "start_execution")
    assert not hasattr(factory.step_functions, "stop_execution")
    assert not hasattr(factory.step_functions, "redrive_execution")
    assert response["candidates_scanned"] == 0
    assert response["alarm_signal_count"] == 0
    assert len(metrics) == 1
    metric = json.loads(metrics[0])
    assert metric["_aws"]["CloudWatchMetrics"][0]["Namespace"] == (
        "MrLister/Phase6/ExecutionRecovery"
    )
    assert metric["RecoveryRuns"] == 1
    assert factory.dynamodb.query_calls[0]["IndexName"] == "ExecutionRecoveryIndex"


def test_invalid_scheduled_event_is_rejected_before_metrics_or_aws() -> None:
    factory = RecordingFactory()
    metrics: list[str] = []
    handler = build_execution_recovery_handler(
        _environment(),
        client_factory=factory,
        metric_logger=metrics.append,
        clock=lambda: NOW,
    )

    with pytest.raises(ExecutionRecoveryInvocationError):
        handler({"source": "stuck-execution-sweeper", "limit": 100})

    assert metrics == []
    assert factory.dynamodb.query_calls == []


def test_composition_rejects_broad_or_incomplete_clients() -> None:
    class IncompleteFactory(RecordingFactory):
        def __call__(self, service_name: str, *, region_name: str) -> object:
            if service_name == "stepfunctions":
                return object()
            return super().__call__(service_name, region_name=region_name)

    with pytest.raises(RuntimeError, match="dependency is unavailable"):
        build_execution_recovery_handler(
            _environment(),
            client_factory=IncompleteFactory(),
            clock=lambda: NOW,
        )
