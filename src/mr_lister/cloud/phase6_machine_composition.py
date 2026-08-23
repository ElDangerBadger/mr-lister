"""Least-capability AWS composition for Phase 6 machine Lambda roles.

Each builder creates only the clients reachable by its Lambda role.  Configuration is
closed, release-bound, and validated before any AWS operation.  Importing this module has
no side effect; deployment entrypoints may safely construct each handler lazily.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Protocol, cast

from mr_lister.agent.runtime_binding import load_agentcore_runtime_binding
from mr_lister.cloud.phase6_composition import (
    PinnedProfileConfiguration,
    PinnedReviewProductAuthority,
)
from mr_lister.cloud.phase6_machine import (
    Phase6DispatcherHandler,
    Phase6PreparationHandler,
    Phase6ProviderHandler,
    Phase6SettlementHandler,
)
from mr_lister.control.agentcore import AgentCorePreparationBridge
from mr_lister.control.dispatch import WorkDispatcher
from mr_lister.control.dynamodb import DynamoDBSellerControlStore
from mr_lister.control.models import WorkType
from mr_lister.control.service import SellerControlService
from mr_lister.control.settlement import PreparationFailureReconciler
from mr_lister.control.worker_service import WorkerControlService
from mr_lister.production.phase6_worker import Phase6ProductMachineWorker
from mr_lister.production.provider_resources import OwnerBoundProviderDraftResources
from mr_lister.production.provider_secrets import (
    SecretsManagerOwnerPrintifyConnectionResolver,
)
from mr_lister.review_profile import FilesystemReviewProductAuthority

MachineAwsService = Literal[
    "bedrock-agentcore",
    "dynamodb",
    "s3",
    "secretsmanager",
    "stepfunctions",
]

_REGION = re.compile(r"^[a-z]{2}(?:-[a-z0-9]+){1,2}-[1-9][0-9]?$|^us-gov-[a-z]+-[1-9]$")
_ENVIRONMENT = re.compile(r"^[a-z][a-z0-9-]{1,15}$")
_ACCOUNT_ID = re.compile(r"^[0-9]{12}$")
_FINGERPRINT = re.compile(r"^[a-f0-9]{64}$")
_PROFILE_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{1,63}$")
_STATE_MACHINE_ARN = re.compile(
    r"^arn:(?P<partition>aws|aws-us-gov|aws-cn):states:(?P<region>[a-z0-9-]+):"
    r"(?P<account>[0-9]{12}):stateMachine:(?P<name>[A-Za-z0-9_-]{1,80})$"
)
_GENERIC_CONFIGURATION_ERROR = "Phase 6 machine configuration is invalid"


class Phase6MachineConfigurationError(RuntimeError):
    """A value-free failure for missing, malformed, or drifting machine settings."""


class MachineAwsClientFactory(Protocol):
    def __call__(self, service_name: MachineAwsService, *, region_name: str) -> object: ...


@dataclass(frozen=True, slots=True)
class MachineCommonConfiguration:
    region: str
    environment_name: str
    account_id: str
    state_table: str
    release_fingerprint: str


@dataclass(frozen=True, slots=True)
class DispatcherConfiguration:
    common: MachineCommonConfiguration
    state_machine_arns: Mapping[WorkType, str]


@dataclass(frozen=True, slots=True)
class PreparationConfiguration:
    common: MachineCommonConfiguration
    runtime_arn: str
    runtime_endpoint_arn: str
    runtime_qualifier: str
    runtime_version: str
    runtime_binding_fingerprint: str


@dataclass(frozen=True, slots=True)
class ProviderConfiguration:
    common: MachineCommonConfiguration
    artifact_bucket: str
    secret_arn: str
    profile: PinnedProfileConfiguration


@dataclass(frozen=True, slots=True)
class SettlementConfiguration:
    common: MachineCommonConfiguration


def load_dispatcher_configuration(environment: Mapping[str, object]) -> DispatcherConfiguration:
    try:
        common = _common(environment)
        suffix_by_type = {
            WorkType.PREPARE: "prepare",
            WorkType.SYNCHRONIZE_PRODUCT: "synchronize-product",
            WorkType.RECONCILE_PRODUCT: "reconcile-product",
            WorkType.REFRESH_ECONOMICS: "refresh-economics",
        }
        name_by_type = {
            WorkType.PREPARE: "MR_LISTER_PREPARE_MACHINE_ARN",
            WorkType.SYNCHRONIZE_PRODUCT: "MR_LISTER_SYNCHRONIZE_PRODUCT_MACHINE_ARN",
            WorkType.RECONCILE_PRODUCT: "MR_LISTER_RECONCILE_PRODUCT_MACHINE_ARN",
            WorkType.REFRESH_ECONOMICS: "MR_LISTER_REFRESH_ECONOMICS_MACHINE_ARN",
        }
        arns: dict[WorkType, str] = {}
        for work_type, variable_name in name_by_type.items():
            arn = _required(environment, variable_name)
            _require_exact_arn(
                arn,
                pattern=_STATE_MACHINE_ARN,
                common=common,
                expected_name=(
                    f"mr-lister-phase6-{common.environment_name}-{suffix_by_type[work_type]}"
                ),
            )
            arns[work_type] = arn
        return DispatcherConfiguration(common=common, state_machine_arns=arns)
    except Exception:
        pass
    raise Phase6MachineConfigurationError(_GENERIC_CONFIGURATION_ERROR)


def load_preparation_configuration(environment: Mapping[str, object]) -> PreparationConfiguration:
    try:
        common = _common(environment)
        binding = load_agentcore_runtime_binding(
            environment,
            region=common.region,
            account_id=common.account_id,
            environment_name=common.environment_name,
            release_fingerprint=common.release_fingerprint,
        )
        return PreparationConfiguration(
            common=common,
            runtime_arn=binding.runtime_arn,
            runtime_endpoint_arn=binding.endpoint_arn,
            runtime_qualifier=binding.qualifier,
            runtime_version=binding.runtime_version,
            runtime_binding_fingerprint=binding.binding_fingerprint,
        )
    except Exception:
        pass
    raise Phase6MachineConfigurationError(_GENERIC_CONFIGURATION_ERROR)


def load_provider_configuration(environment: Mapping[str, object]) -> ProviderConfiguration:
    try:
        common = _common(environment)
        artifact_bucket = _required(environment, "MR_LISTER_ARTIFACT_BUCKET")
        expected_bucket = (
            f"mr-lister-phase6-artifacts-{common.environment_name}-"
            f"{common.account_id}-{common.region}"
        )
        if artifact_bucket != expected_bucket or len(artifact_bucket) > 63:
            raise ValueError
        secret_arn = _required(environment, "MR_LISTER_PRINTIFY_SECRET_ARN")
        # The resolver owns the exact resource-shape validation; bind partition/region/account here.
        resolver_match = re.fullmatch(
            r"arn:(aws|aws-us-gov|aws-cn):secretsmanager:([a-z0-9-]+):"
            r"([0-9]{12}):secret:mr-lister/[A-Za-z0-9/_-]+-[A-Za-z0-9]{6}",
            secret_arn,
        )
        if (
            resolver_match is None
            or resolver_match.group(1) != _partition(common.region)
            or resolver_match.group(2) != common.region
            or resolver_match.group(3) != common.account_id
        ):
            raise ValueError
        profile = _profile(environment)
        return ProviderConfiguration(
            common=common,
            artifact_bucket=artifact_bucket,
            secret_arn=secret_arn,
            profile=profile,
        )
    except Exception:
        pass
    raise Phase6MachineConfigurationError(_GENERIC_CONFIGURATION_ERROR)


def load_settlement_configuration(environment: Mapping[str, object]) -> SettlementConfiguration:
    try:
        return SettlementConfiguration(common=_common(environment))
    except Exception:
        pass
    raise Phase6MachineConfigurationError(_GENERIC_CONFIGURATION_ERROR)


def compose_dispatcher_handler(
    configuration: DispatcherConfiguration,
    *,
    client_factory: MachineAwsClientFactory,
) -> Phase6DispatcherHandler:
    dynamodb = _client(
        client_factory,
        "dynamodb",
        configuration.common.region,
        required_methods=("get_item", "put_item", "query"),
    )
    step_functions = _client(
        client_factory,
        "stepfunctions",
        configuration.common.region,
        required_methods=("describe_execution", "start_execution"),
    )
    store = DynamoDBSellerControlStore(
        client=dynamodb,
        table_name=configuration.common.state_table,
    )
    dispatcher = WorkDispatcher(
        store=store,
        step_functions=step_functions,
        state_machine_arns=configuration.state_machine_arns,
    )
    return Phase6DispatcherHandler(dispatcher=dispatcher)


def compose_preparation_handler(
    configuration: PreparationConfiguration,
    *,
    client_factory: MachineAwsClientFactory,
) -> Phase6PreparationHandler:
    dynamodb = _client(
        client_factory,
        "dynamodb",
        configuration.common.region,
        required_methods=("get_item",),
    )
    agentcore = _client(
        client_factory,
        "bedrock-agentcore",
        configuration.common.region,
        required_methods=("invoke_agent_runtime",),
    )
    store = DynamoDBSellerControlStore(
        client=dynamodb,
        table_name=configuration.common.state_table,
    )
    bridge = AgentCorePreparationBridge(
        store=store,
        agentcore=agentcore,
        runtime_arn=configuration.runtime_arn,
        runtime_qualifier=configuration.runtime_qualifier,
        runtime_version=configuration.runtime_version,
    )
    return Phase6PreparationHandler(preparation=bridge)


def compose_provider_handler(
    configuration: ProviderConfiguration,
    *,
    client_factory: MachineAwsClientFactory,
) -> Phase6ProviderHandler:
    common = configuration.common
    dynamodb = _client(
        client_factory,
        "dynamodb",
        common.region,
        required_methods=("get_item", "put_item", "transact_write_items"),
    )
    s3 = _client(
        client_factory,
        "s3",
        common.region,
        required_methods=("get_object",),
    )
    secrets = _client(
        client_factory,
        "secretsmanager",
        common.region,
        required_methods=("get_secret_value",),
    )
    store = DynamoDBSellerControlStore(client=dynamodb, table_name=common.state_table)
    worker_control = WorkerControlService(store=store)
    resolver = SecretsManagerOwnerPrintifyConnectionResolver(
        client=cast(Any, secrets),
        secret_arn=configuration.secret_arn,
    )
    resources = OwnerBoundProviderDraftResources(
        connection_resolver=resolver,
        s3_client=cast(Any, s3),
        artifact_bucket=configuration.artifact_bucket,
        bucket_owner_account_id=common.account_id,
    )
    worker = Phase6ProductMachineWorker(
        store=store,
        control=worker_control,
        profiles=PinnedReviewProductAuthority(configuration.profile.exact),
        resources=resources,
    )
    return Phase6ProviderHandler(provider=worker)


def compose_settlement_handler(
    configuration: SettlementConfiguration,
    *,
    client_factory: MachineAwsClientFactory,
) -> Phase6SettlementHandler:
    dynamodb = _client(
        client_factory,
        "dynamodb",
        configuration.common.region,
        required_methods=("get_item", "put_item", "transact_write_items"),
    )
    store = DynamoDBSellerControlStore(
        client=dynamodb,
        table_name=configuration.common.state_table,
    )
    control = SellerControlService(store=store)
    preparation = PreparationFailureReconciler(store=store, control=control)
    return Phase6SettlementHandler(
        store=store,
        control=control,
        preparation_settlement=preparation,
    )


def build_dispatcher_handler(
    environment: Mapping[str, object],
    *,
    client_factory: MachineAwsClientFactory | None = None,
) -> Phase6DispatcherHandler:
    return compose_dispatcher_handler(
        load_dispatcher_configuration(environment),
        client_factory=client_factory or default_machine_client_factory,
    )


def build_preparation_handler(
    environment: Mapping[str, object],
    *,
    client_factory: MachineAwsClientFactory | None = None,
) -> Phase6PreparationHandler:
    return compose_preparation_handler(
        load_preparation_configuration(environment),
        client_factory=client_factory or default_machine_client_factory,
    )


def build_provider_handler(
    environment: Mapping[str, object],
    *,
    client_factory: MachineAwsClientFactory | None = None,
) -> Phase6ProviderHandler:
    return compose_provider_handler(
        load_provider_configuration(environment),
        client_factory=client_factory or default_machine_client_factory,
    )


def build_settlement_handler(
    environment: Mapping[str, object],
    *,
    client_factory: MachineAwsClientFactory | None = None,
) -> Phase6SettlementHandler:
    return compose_settlement_handler(
        load_settlement_configuration(environment),
        client_factory=client_factory or default_machine_client_factory,
    )


def default_machine_client_factory(
    service_name: MachineAwsService,
    *,
    region_name: str,
) -> object:
    import boto3
    from botocore.config import Config

    if service_name not in {
        "bedrock-agentcore",
        "dynamodb",
        "s3",
        "secretsmanager",
        "stepfunctions",
    }:
        raise ValueError("Unsupported Phase 6 machine AWS client")
    return boto3.client(
        service_name,
        region_name=region_name,
        config=Config(
            connect_timeout=10,
            read_timeout=600 if service_name == "bedrock-agentcore" else 60,
            retries={"mode": "standard", "max_attempts": 3},
            signature_version="s3v4" if service_name == "s3" else None,
            s3={"addressing_style": "virtual"} if service_name == "s3" else None,
        ),
    )


def _common(environment: Mapping[str, object]) -> MachineCommonConfiguration:
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
    return MachineCommonConfiguration(
        region=region,
        environment_name=environment_name,
        account_id=account_id,
        state_table=state_table,
        release_fingerprint=release_fingerprint,
    )


def _profile(environment: Mapping[str, object]) -> PinnedProfileConfiguration:
    profile_id = _required(environment, "MR_LISTER_PRODUCT_PROFILE_ID")
    if _PROFILE_ID.fullmatch(profile_id) is None:
        raise ValueError
    version_text = _required(environment, "MR_LISTER_PRODUCT_PROFILE_VERSION")
    if re.fullmatch(r"[1-9][0-9]{0,5}", version_text) is None:
        raise ValueError
    fingerprint = _required(environment, "MR_LISTER_PRODUCT_PROFILE_FINGERPRINT")
    if _FINGERPRINT.fullmatch(fingerprint) is None or fingerprint == "0" * 64:
        raise ValueError
    path_text = _required(environment, "MR_LISTER_PRODUCT_PROFILE_PATH")
    if not path_text.isascii() or "\\" in path_text:
        raise ValueError
    path = Path(path_text)
    if (
        not path.is_absolute()
        or path.as_posix() != path_text
        or path.name != f"{profile_id}.json"
        or path.resolve(strict=True) != path
        or not path.is_file()
        or not 1 <= path.stat().st_size <= 1024 * 1024
    ):
        raise ValueError
    exact = FilesystemReviewProductAuthority(profile_directory=path.parent).get_exact(
        profile_id=profile_id,
        profile_version=int(version_text),
    )
    if exact.fingerprint != fingerprint:
        raise ValueError
    return PinnedProfileConfiguration(profile_path=path, exact=exact)


def _require_exact_arn(
    value: str,
    *,
    pattern: re.Pattern[str],
    common: MachineCommonConfiguration,
    expected_name: str,
) -> None:
    match = pattern.fullmatch(value)
    if (
        match is None
        or match.group("partition") != _partition(common.region)
        or match.group("region") != common.region
        or match.group("account") != common.account_id
        or match.group("name") != expected_name
    ):
        raise ValueError


def _partition(region: str) -> str:
    if region.startswith("cn-"):
        return "aws-cn"
    if region.startswith("us-gov-"):
        return "aws-us-gov"
    return "aws"


def _required(environment: Mapping[str, object], name: str) -> str:
    value = environment.get(name)
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 4096
        or value != value.strip()
        or "\x00" in value
    ):
        raise ValueError
    return value


def _client(
    factory: MachineAwsClientFactory,
    service_name: MachineAwsService,
    region: str,
    *,
    required_methods: tuple[str, ...],
) -> Any:
    client = factory(service_name, region_name=region)
    if any(not callable(getattr(client, method, None)) for method in required_methods):
        raise RuntimeError("Phase 6 machine dependency is unavailable")
    return client


__all__ = [
    "DispatcherConfiguration",
    "MachineAwsClientFactory",
    "MachineAwsService",
    "MachineCommonConfiguration",
    "Phase6MachineConfigurationError",
    "PreparationConfiguration",
    "ProviderConfiguration",
    "SettlementConfiguration",
    "build_dispatcher_handler",
    "build_preparation_handler",
    "build_provider_handler",
    "build_settlement_handler",
    "compose_dispatcher_handler",
    "compose_preparation_handler",
    "compose_provider_handler",
    "compose_settlement_handler",
    "default_machine_client_factory",
    "load_dispatcher_configuration",
    "load_preparation_configuration",
    "load_provider_configuration",
    "load_settlement_configuration",
]
