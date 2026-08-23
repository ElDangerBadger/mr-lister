"""Closed AWS composition for the dedicated Phase 6 execution-recovery Lambda."""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Literal, Protocol

from mr_lister.control.dynamodb import DynamoDBSellerControlStore
from mr_lister.control.execution_recovery import (
    ExecutionRecoveryExecutionError,
    ExecutionRecoveryHandler,
    ExecutionRecoveryInvocationError,
    ExecutionRecoverySweepResult,
    StuckExecutionRecoverySweeper,
)
from mr_lister.control.execution_recovery_aws import (
    DynamoDBExecutionRecoveryAuthority,
    EmbeddedExecutionRecoveryMetrics,
    StepFunctionsExactExecutionObserver,
)
from mr_lister.control.models import WorkType
from mr_lister.control.service import SellerControlService
from mr_lister.control.settlement import PreparationFailureReconciler

ExecutionRecoveryAwsService = Literal["dynamodb", "stepfunctions"]

_REGION = re.compile(r"^[a-z]{2}(?:-[a-z0-9]+){1,2}-[1-9][0-9]?$|^us-gov-[a-z]+-[1-9]$")
_ENVIRONMENT = re.compile(r"^[a-z][a-z0-9-]{1,15}$")
_ACCOUNT_ID = re.compile(r"^[0-9]{12}$")
_FINGERPRINT = re.compile(r"^[a-f0-9]{64}$")
_POSITIVE_INTEGER = re.compile(r"^[1-9][0-9]{0,5}$")
_STATE_MACHINE_ARN = re.compile(
    r"^arn:(?P<partition>aws|aws-us-gov|aws-cn):states:"
    r"(?P<region>[a-z0-9-]+):(?P<account>[0-9]{12}):"
    r"stateMachine:(?P<name>[A-Za-z0-9_-]{1,80})$"
)
_GENERIC_CONFIGURATION_ERROR = "Phase 6 execution recovery configuration is invalid"


class Phase6ExecutionRecoveryConfigurationError(RuntimeError):
    """Value-free configuration failure for the isolated recovery role."""


class ExecutionRecoveryAwsClientFactory(Protocol):
    def __call__(
        self,
        service_name: ExecutionRecoveryAwsService,
        *,
        region_name: str,
    ) -> object: ...


@dataclass(frozen=True, slots=True)
class ExecutionRecoveryConfiguration:
    region: str
    environment_name: str
    account_id: str
    state_table: str
    release_fingerprint: str
    state_machine_arns: Mapping[WorkType, str]
    stale_after: timedelta
    batch_limit: int
    maximum_cas_rechecks: int


class InstrumentedExecutionRecoveryHandler:
    """Emit sanitized metrics after the exact recovery boundary returns sanitized counters."""

    __slots__ = ("_boundary", "_metrics")

    def __init__(
        self,
        *,
        boundary: ExecutionRecoveryHandler,
        metrics: EmbeddedExecutionRecoveryMetrics,
    ) -> None:
        self._boundary = boundary
        self._metrics = metrics

    def __call__(
        self,
        event: Mapping[str, Any],
        context: object | None = None,
    ) -> dict[str, Any]:
        try:
            payload = self._boundary(event, context)
            result = ExecutionRecoverySweepResult.model_validate(payload)
            self._metrics.emit(result)
            return payload
        except (ExecutionRecoveryInvocationError, ExecutionRecoveryExecutionError):
            raise
        except Exception:
            raise ExecutionRecoveryExecutionError(
                "Stuck-execution recovery failed safely"
            ) from None


def load_execution_recovery_configuration(
    environment: Mapping[str, object],
) -> ExecutionRecoveryConfiguration:
    """Load one closed same-account configuration with bounded numeric controls."""

    try:
        if not isinstance(environment, Mapping):
            raise ValueError
        region = _required(environment, "AWS_REGION")
        if _REGION.fullmatch(region) is None:
            raise ValueError
        environment_name = _required(environment, "MR_LISTER_ENVIRONMENT")
        if _ENVIRONMENT.fullmatch(environment_name) is None:
            raise ValueError
        account_id = _required(environment, "MR_LISTER_AWS_ACCOUNT_ID")
        if _ACCOUNT_ID.fullmatch(account_id) is None or account_id == "0" * 12:
            raise ValueError
        state_table = _required(environment, "MR_LISTER_STATE_TABLE")
        if state_table != f"mr-lister-phase6-{environment_name}":
            raise ValueError
        release_fingerprint = _required(environment, "MR_LISTER_RELEASE_FINGERPRINT")
        if _FINGERPRINT.fullmatch(release_fingerprint) is None or release_fingerprint == "0" * 64:
            raise ValueError
        stale_seconds = _bounded_integer(
            environment,
            "MR_LISTER_EXECUTION_RECOVERY_STALE_SECONDS",
            minimum=1_200,
            maximum=86_400,
        )
        batch_limit = _bounded_integer(
            environment,
            "MR_LISTER_EXECUTION_RECOVERY_BATCH_LIMIT",
            minimum=1,
            maximum=100,
        )
        maximum_cas_rechecks = _bounded_integer(
            environment,
            "MR_LISTER_EXECUTION_RECOVERY_MAX_CAS_RECHECKS",
            minimum=1,
            maximum=3,
        )
        suffix_by_type = {
            WorkType.PREPARE: "prepare",
            WorkType.SYNCHRONIZE_PRODUCT: "synchronize-product",
            WorkType.RECONCILE_PRODUCT: "reconcile-product",
            WorkType.REFRESH_ECONOMICS: "refresh-economics",
        }
        variable_by_type = {
            WorkType.PREPARE: "MR_LISTER_PREPARE_MACHINE_ARN",
            WorkType.SYNCHRONIZE_PRODUCT: "MR_LISTER_SYNCHRONIZE_PRODUCT_MACHINE_ARN",
            WorkType.RECONCILE_PRODUCT: "MR_LISTER_RECONCILE_PRODUCT_MACHINE_ARN",
            WorkType.REFRESH_ECONOMICS: "MR_LISTER_REFRESH_ECONOMICS_MACHINE_ARN",
        }
        state_machine_arns: dict[WorkType, str] = {}
        for work_type, variable_name in variable_by_type.items():
            arn = _required(environment, variable_name)
            match = _STATE_MACHINE_ARN.fullmatch(arn)
            if (
                match is None
                or match.group("partition") != _partition(region)
                or match.group("region") != region
                or match.group("account") != account_id
                or match.group("name")
                != f"mr-lister-phase6-{environment_name}-{suffix_by_type[work_type]}"
            ):
                raise ValueError
            state_machine_arns[work_type] = arn
        return ExecutionRecoveryConfiguration(
            region=region,
            environment_name=environment_name,
            account_id=account_id,
            state_table=state_table,
            release_fingerprint=release_fingerprint,
            state_machine_arns=state_machine_arns,
            stale_after=timedelta(seconds=stale_seconds),
            batch_limit=batch_limit,
            maximum_cas_rechecks=maximum_cas_rechecks,
        )
    except Exception:
        raise Phase6ExecutionRecoveryConfigurationError(_GENERIC_CONFIGURATION_ERROR) from None


def compose_execution_recovery_handler(
    configuration: ExecutionRecoveryConfiguration,
    *,
    client_factory: ExecutionRecoveryAwsClientFactory,
    metric_logger: Callable[[str], object] = print,
    clock: Callable[[], datetime] | None = None,
) -> InstrumentedExecutionRecoveryHandler:
    """Create only DynamoDB and Step Functions DescribeExecution dependencies."""

    dynamodb = _client(
        client_factory,
        "dynamodb",
        configuration.region,
        required_methods=(
            "get_item",
            "put_item",
            "query",
            "transact_get_items",
            "transact_write_items",
        ),
    )
    step_functions = _client(
        client_factory,
        "stepfunctions",
        configuration.region,
        required_methods=("describe_execution",),
    )
    store = DynamoDBSellerControlStore(
        client=dynamodb,
        table_name=configuration.state_table,
    )
    authority = DynamoDBExecutionRecoveryAuthority(
        client=dynamodb,
        table_name=configuration.state_table,
    )
    observer = StepFunctionsExactExecutionObserver(client=step_functions)
    control = SellerControlService(store=store, clock=clock)
    preparation = PreparationFailureReconciler(
        store=store,
        control=control,
        maximum_cas_rechecks=configuration.maximum_cas_rechecks,
    )
    sweeper = StuckExecutionRecoverySweeper(
        inventory=authority,
        authority=authority,
        executions=observer,
        control=control,
        preparation_settlement=preparation,
        state_machine_arns=configuration.state_machine_arns,
        clock=clock,
        stale_after=configuration.stale_after,
        batch_limit=configuration.batch_limit,
        maximum_cas_rechecks=configuration.maximum_cas_rechecks,
    )
    boundary = ExecutionRecoveryHandler(sweeper=sweeper)
    metrics = EmbeddedExecutionRecoveryMetrics(
        environment_name=configuration.environment_name,
        logger=metric_logger,
        clock=clock,
    )
    return InstrumentedExecutionRecoveryHandler(boundary=boundary, metrics=metrics)


def build_execution_recovery_handler(
    environment: Mapping[str, object],
    *,
    client_factory: ExecutionRecoveryAwsClientFactory | None = None,
    metric_logger: Callable[[str], object] = print,
    clock: Callable[[], datetime] | None = None,
) -> InstrumentedExecutionRecoveryHandler:
    return compose_execution_recovery_handler(
        load_execution_recovery_configuration(environment),
        client_factory=client_factory or default_execution_recovery_client_factory,
        metric_logger=metric_logger,
        clock=clock,
    )


def default_execution_recovery_client_factory(
    service_name: ExecutionRecoveryAwsService,
    *,
    region_name: str,
) -> object:
    import boto3
    from botocore.config import Config

    if service_name not in {"dynamodb", "stepfunctions"}:
        raise ValueError("Unsupported execution recovery AWS client")
    return boto3.client(
        service_name,
        region_name=region_name,
        config=Config(
            connect_timeout=10,
            read_timeout=30,
            retries={"mode": "standard", "max_attempts": 3},
        ),
    )


def _required(environment: Mapping[str, object], name: str) -> str:
    value = environment.get(name)
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 4_096
        or value != value.strip()
        or "\x00" in value
    ):
        raise ValueError
    return value


def _bounded_integer(
    environment: Mapping[str, object],
    name: str,
    *,
    minimum: int,
    maximum: int,
) -> int:
    text = _required(environment, name)
    if _POSITIVE_INTEGER.fullmatch(text) is None:
        raise ValueError
    value = int(text)
    if not minimum <= value <= maximum:
        raise ValueError
    return value


def _partition(region: str) -> str:
    if region.startswith("cn-"):
        return "aws-cn"
    if region.startswith("us-gov-"):
        return "aws-us-gov"
    return "aws"


def _client(
    factory: ExecutionRecoveryAwsClientFactory,
    service_name: ExecutionRecoveryAwsService,
    region: str,
    *,
    required_methods: tuple[str, ...],
) -> Any:
    client = factory(service_name, region_name=region)
    if any(not callable(getattr(client, method, None)) for method in required_methods):
        raise RuntimeError("Execution recovery dependency is unavailable")
    return client


__all__ = [
    "ExecutionRecoveryAwsClientFactory",
    "ExecutionRecoveryAwsService",
    "ExecutionRecoveryConfiguration",
    "InstrumentedExecutionRecoveryHandler",
    "Phase6ExecutionRecoveryConfigurationError",
    "build_execution_recovery_handler",
    "compose_execution_recovery_handler",
    "default_execution_recovery_client_factory",
    "load_execution_recovery_configuration",
]
