"""Closed AWS composition for the Phase 6 source-version retention sweep.

The retention Lambda can list only the fixed private source prefix, inspect or replace
one exact version's lifecycle tag, strongly read job/source authority, and CAS one
checkpoint row.  Object bytes, deletion, secrets, provider calls, and orchestration are
not represented by this composition surface.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal, Protocol, cast

from mr_lister.production.retention import (
    SOURCE_VERSION_RETENTION_CONTRACT_VERSION,
    ReferenceAwareSourceVersionSweeper,
    RetentionSweepResult,
)
from mr_lister.production.retention_aws import (
    DynamoDBRetentionCheckpointStore,
    DynamoDBStrongSourceAuthorityReader,
    S3SourceVersionInventory,
    S3SourceVersionTagStore,
)

RetentionAwsService = Literal["dynamodb", "s3"]

SOURCE_VERSION_RETENTION_EVENT = {
    "contract_version": SOURCE_VERSION_RETENTION_CONTRACT_VERSION,
    "source": "source-version-retention-sweeper",
}

_REGION = re.compile(r"^[a-z]{2}(?:-[a-z0-9]+){1,2}-[1-9][0-9]?$|^us-gov-[a-z]+-[1-9]$")
_ENVIRONMENT = re.compile(r"^[a-z][a-z0-9-]{1,15}$")
_ACCOUNT_ID = re.compile(r"^[0-9]{12}$")
_GENERIC_CONFIGURATION_ERROR = "Phase 6 source retention configuration is invalid"
_GENERIC_EXECUTION_ERROR = "Phase 6 source retention sweep failed safely"


class Phase6RetentionConfigurationError(RuntimeError):
    """Value-free failure for missing, malformed, or cross-stack retention settings."""


class Phase6RetentionExecutionError(RuntimeError):
    """Value-free failure at the scheduled Lambda boundary."""


class RetentionAwsClientFactory(Protocol):
    def __call__(self, service_name: RetentionAwsService, *, region_name: str) -> object: ...


class SourceVersionSweeper(Protocol):
    def sweep(self) -> RetentionSweepResult: ...


@dataclass(frozen=True, slots=True)
class Phase6RetentionConfiguration:
    region: str
    environment_name: str
    account_id: str
    state_table: str
    artifact_bucket: str


@dataclass(frozen=True, slots=True)
class Phase6SourceVersionRetentionHandler:
    """Accept only the configured constant schedule input and return sanitized counters."""

    sweeper: SourceVersionSweeper

    def __call__(
        self,
        event: Mapping[str, Any],
        context: object | None = None,
    ) -> dict[str, Any]:
        del context
        try:
            if not isinstance(event, Mapping) or dict(event) != SOURCE_VERSION_RETENTION_EVENT:
                raise ValueError
            result = self.sweeper.sweep()
            if not isinstance(result, RetentionSweepResult):
                raise TypeError
            return result.model_dump(mode="json")
        except Exception:
            raise Phase6RetentionExecutionError(_GENERIC_EXECUTION_ERROR) from None


def load_retention_configuration(
    environment: Mapping[str, object],
) -> Phase6RetentionConfiguration:
    try:
        if not isinstance(environment, Mapping):
            raise ValueError
        region = _required(environment, "AWS_REGION")
        environment_name = _required(environment, "MR_LISTER_ENVIRONMENT")
        account_id = _required(environment, "MR_LISTER_AWS_ACCOUNT_ID")
        state_table = _required(environment, "MR_LISTER_STATE_TABLE")
        artifact_bucket = _required(environment, "MR_LISTER_ARTIFACT_BUCKET")
        if (
            _REGION.fullmatch(region) is None
            or _ENVIRONMENT.fullmatch(environment_name) is None
            or _ACCOUNT_ID.fullmatch(account_id) is None
            or account_id == "0" * 12
            or state_table != f"mr-lister-phase6-{environment_name}"
            or artifact_bucket
            != (f"mr-lister-phase6-artifacts-{environment_name}-{account_id}-{region}")
            or len(artifact_bucket) > 63
        ):
            raise ValueError
        return Phase6RetentionConfiguration(
            region=region,
            environment_name=environment_name,
            account_id=account_id,
            state_table=state_table,
            artifact_bucket=artifact_bucket,
        )
    except Exception:
        pass
    raise Phase6RetentionConfigurationError(_GENERIC_CONFIGURATION_ERROR) from None


def compose_source_version_retention_handler(
    configuration: Phase6RetentionConfiguration,
    *,
    client_factory: RetentionAwsClientFactory,
) -> Phase6SourceVersionRetentionHandler:
    s3 = _client(
        client_factory,
        "s3",
        configuration.region,
        required_methods=(
            "get_object_tagging",
            "list_object_versions",
            "put_object_tagging",
        ),
    )
    dynamodb = _client(
        client_factory,
        "dynamodb",
        configuration.region,
        required_methods=("get_item", "put_item", "transact_get_items"),
    )
    inventory = S3SourceVersionInventory(
        client=cast(Any, s3),
        artifact_bucket=configuration.artifact_bucket,
        bucket_owner_account_id=configuration.account_id,
    )
    tags = S3SourceVersionTagStore(
        client=cast(Any, s3),
        artifact_bucket=configuration.artifact_bucket,
        bucket_owner_account_id=configuration.account_id,
    )
    authority = DynamoDBStrongSourceAuthorityReader(
        client=cast(Any, dynamodb),
        table_name=configuration.state_table,
    )
    checkpoints = DynamoDBRetentionCheckpointStore(
        client=cast(Any, dynamodb),
        table_name=configuration.state_table,
    )
    return Phase6SourceVersionRetentionHandler(
        sweeper=ReferenceAwareSourceVersionSweeper(
            inventory=inventory,
            tags=tags,
            authority=authority,
            checkpoints=checkpoints,
            artifact_bucket=configuration.artifact_bucket,
        )
    )


def build_source_version_retention_handler(
    environment: Mapping[str, object],
    *,
    client_factory: RetentionAwsClientFactory | None = None,
) -> Phase6SourceVersionRetentionHandler:
    return compose_source_version_retention_handler(
        load_retention_configuration(environment),
        client_factory=client_factory or default_retention_client_factory,
    )


def default_retention_client_factory(
    service_name: RetentionAwsService,
    *,
    region_name: str,
) -> object:
    import boto3
    from botocore.config import Config

    if service_name not in {"dynamodb", "s3"}:
        raise ValueError("Unsupported Phase 6 retention AWS client")
    configuration: dict[str, Any] = {
        "connect_timeout": 10,
        "read_timeout": 60,
        "retries": {"mode": "standard", "max_attempts": 3},
    }
    if service_name == "s3":
        configuration.update(
            signature_version="s3v4",
            s3={"addressing_style": "virtual"},
        )
    return boto3.client(
        service_name,
        region_name=region_name,
        config=Config(**configuration),
    )


def _client(
    factory: RetentionAwsClientFactory,
    service_name: RetentionAwsService,
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
    raise Phase6RetentionConfigurationError(_GENERIC_CONFIGURATION_ERROR) from None


def _required(environment: Mapping[str, object], name: str) -> str:
    value = environment.get(name)
    if not isinstance(value, str) or not value or value.strip() != value or not value.isascii():
        raise ValueError
    return value


__all__ = [
    "SOURCE_VERSION_RETENTION_EVENT",
    "Phase6RetentionConfiguration",
    "Phase6RetentionConfigurationError",
    "Phase6RetentionExecutionError",
    "Phase6SourceVersionRetentionHandler",
    "RetentionAwsClientFactory",
    "build_source_version_retention_handler",
    "compose_source_version_retention_handler",
    "default_retention_client_factory",
    "load_retention_configuration",
]
