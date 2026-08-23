"""Closed AWS composition for Phase 6 terminal operational-record cleanup.

This surface constructs one DynamoDB client with projected scan/query, strong-read, checkpoint,
and transactional TTL-assignment capabilities.  Direct deletion, S3, secrets, provider calls,
Bedrock, AgentCore, and orchestration are not represented.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal, Protocol, cast

from mr_lister.production.operational_cleanup import (
    OPERATIONAL_CLEANUP_CONTRACT_VERSION,
    OperationalCleanupResult,
    TerminalOperationalRecordCleanup,
)
from mr_lister.production.operational_cleanup_aws import (
    DynamoDBOperationalCleanupCheckpointStore,
    DynamoDBOperationalJobInventory,
    DynamoDBTerminalOperationalExpiryStore,
)

OperationalCleanupAwsService = Literal["dynamodb"]

TERMINAL_OPERATIONAL_CLEANUP_EVENT = {
    "contract_version": OPERATIONAL_CLEANUP_CONTRACT_VERSION,
    "source": "terminal-operational-record-cleanup",
}

_REGION = re.compile(r"^[a-z]{2}(?:-[a-z0-9]+){1,2}-[1-9][0-9]?$|^us-gov-[a-z]+-[1-9]$")
_ENVIRONMENT = re.compile(r"^[a-z][a-z0-9-]{1,15}$")
_GENERIC_CONFIGURATION_ERROR = "Phase 6 operational cleanup configuration is invalid"
_GENERIC_EXECUTION_ERROR = "Phase 6 operational cleanup failed safely"


class Phase6OperationalCleanupConfigurationError(RuntimeError):
    """Value-free failure for missing, malformed, or cross-stack settings."""


class Phase6OperationalCleanupExecutionError(RuntimeError):
    """Value-free failure at the scheduled Lambda boundary."""


class OperationalCleanupAwsClientFactory(Protocol):
    def __call__(
        self,
        service_name: OperationalCleanupAwsService,
        *,
        region_name: str,
    ) -> object: ...


class OperationalCleanupSweeper(Protocol):
    def sweep(self) -> OperationalCleanupResult: ...


@dataclass(frozen=True, slots=True)
class Phase6OperationalCleanupConfiguration:
    region: str
    environment_name: str
    state_table: str


@dataclass(frozen=True, slots=True)
class Phase6TerminalOperationalCleanupHandler:
    """Accept one exact schedule input and return identifier-free counters."""

    sweeper: OperationalCleanupSweeper

    def __call__(
        self,
        event: Mapping[str, Any],
        context: object | None = None,
    ) -> dict[str, Any]:
        del context
        try:
            if not isinstance(event, Mapping) or dict(event) != TERMINAL_OPERATIONAL_CLEANUP_EVENT:
                raise ValueError
            result = self.sweeper.sweep()
            if not isinstance(result, OperationalCleanupResult):
                raise TypeError
            return result.model_dump(mode="json")
        except Exception:
            raise Phase6OperationalCleanupExecutionError(_GENERIC_EXECUTION_ERROR) from None


def load_operational_cleanup_configuration(
    environment: Mapping[str, object],
) -> Phase6OperationalCleanupConfiguration:
    try:
        if not isinstance(environment, Mapping):
            raise ValueError
        region = _required(environment, "AWS_REGION")
        environment_name = _required(environment, "MR_LISTER_ENVIRONMENT")
        state_table = _required(environment, "MR_LISTER_STATE_TABLE")
        if (
            _REGION.fullmatch(region) is None
            or _ENVIRONMENT.fullmatch(environment_name) is None
            or state_table != f"mr-lister-phase6-{environment_name}"
        ):
            raise ValueError
        return Phase6OperationalCleanupConfiguration(
            region=region,
            environment_name=environment_name,
            state_table=state_table,
        )
    except Exception:
        raise Phase6OperationalCleanupConfigurationError(_GENERIC_CONFIGURATION_ERROR) from None


def compose_terminal_operational_cleanup_handler(
    configuration: Phase6OperationalCleanupConfiguration,
    *,
    client_factory: OperationalCleanupAwsClientFactory,
) -> Phase6TerminalOperationalCleanupHandler:
    dynamodb = _client(
        client_factory,
        "dynamodb",
        configuration.region,
        required_methods=(
            "get_item",
            "put_item",
            "query",
            "scan",
            "transact_write_items",
        ),
    )
    inventory = DynamoDBOperationalJobInventory(
        client=cast(Any, dynamodb),
        table_name=configuration.state_table,
    )
    expiry_store = DynamoDBTerminalOperationalExpiryStore(
        client=cast(Any, dynamodb),
        table_name=configuration.state_table,
    )
    checkpoints = DynamoDBOperationalCleanupCheckpointStore(
        client=cast(Any, dynamodb),
        table_name=configuration.state_table,
    )
    return Phase6TerminalOperationalCleanupHandler(
        sweeper=TerminalOperationalRecordCleanup(
            inventory=inventory,
            expiry_store=expiry_store,
            checkpoints=checkpoints,
        )
    )


def build_terminal_operational_cleanup_handler(
    environment: Mapping[str, object],
    *,
    client_factory: OperationalCleanupAwsClientFactory | None = None,
) -> Phase6TerminalOperationalCleanupHandler:
    return compose_terminal_operational_cleanup_handler(
        load_operational_cleanup_configuration(environment),
        client_factory=client_factory or default_operational_cleanup_client_factory,
    )


def default_operational_cleanup_client_factory(
    service_name: OperationalCleanupAwsService,
    *,
    region_name: str,
) -> object:
    import boto3
    from botocore.config import Config

    if service_name != "dynamodb":
        raise ValueError("Unsupported Phase 6 operational cleanup AWS client")
    return boto3.client(
        service_name,
        region_name=region_name,
        config=Config(
            connect_timeout=10,
            read_timeout=60,
            retries={"mode": "standard", "max_attempts": 3},
        ),
    )


def _client(
    factory: OperationalCleanupAwsClientFactory,
    service_name: OperationalCleanupAwsService,
    region: str,
    *,
    required_methods: tuple[str, ...],
) -> object:
    try:
        client = factory(service_name, region_name=region)
    except Exception:
        pass
    else:
        if all(callable(getattr(client, method, None)) for method in required_methods):
            return client
    raise Phase6OperationalCleanupConfigurationError(_GENERIC_CONFIGURATION_ERROR) from None


def _required(environment: Mapping[str, object], name: str) -> str:
    value = environment.get(name)
    if not isinstance(value, str) or not value or value.strip() != value or not value.isascii():
        raise ValueError
    return value


__all__ = [
    "TERMINAL_OPERATIONAL_CLEANUP_EVENT",
    "OperationalCleanupAwsClientFactory",
    "Phase6OperationalCleanupConfiguration",
    "Phase6OperationalCleanupConfigurationError",
    "Phase6OperationalCleanupExecutionError",
    "Phase6TerminalOperationalCleanupHandler",
    "build_terminal_operational_cleanup_handler",
    "compose_terminal_operational_cleanup_handler",
    "default_operational_cleanup_client_factory",
    "load_operational_cleanup_configuration",
]
