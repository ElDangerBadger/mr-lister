"""Release-first entrypoints for the sealed Phase 7.15C provider-free operations drill.

This module imports only the standard library until the complete packaged release has been
authenticated.  It then constructs one capability-reduced handler with DynamoDB plus, only for
recovery, Step Functions DescribeExecution/RedriveExecution.  There is no StartExecution, SQS
send, provider, secret, HTTP, seller route, dispatcher, query, request, or worker capability.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Callable, Mapping
from pathlib import Path
from threading import Lock
from typing import Any, Literal, Protocol

# An extracted sealed bundle must not create executable bytes absent from its manifest.
sys.dont_write_bytecode = True

_RELEASE_FINGERPRINT_ENV = "MR_LISTER_PHASE715C_OPERATIONS_RELEASE_FINGERPRINT"
_APPLICATION_RELEASE_FINGERPRINT_ENV = "MR_LISTER_RELEASE_FINGERPRINT"
_CONTRACT_FINGERPRINT_ENV = "MR_LISTER_PHASE7_CONTRACT_FINGERPRINT"
_CONTRACT_VERSION_ENV = "MR_LISTER_PHASE7_CONTRACT_VERSION"
_OPERATIONS_MODE_ENV = "MR_LISTER_PHASE715C_OPERATIONS_MODE"
_PROFILE_ID_ENV = "MR_LISTER_PRODUCT_PROFILE_ID"
_PROFILE_VERSION_ENV = "MR_LISTER_PRODUCT_PROFILE_VERSION"
_PROFILE_FINGERPRINT_ENV = "MR_LISTER_PRODUCT_PROFILE_FINGERPRINT"
_PROFILE_PATH_ENV = "MR_LISTER_PRODUCT_PROFILE_PATH"
_REGION_ENV = "AWS_REGION"
_STATE_TABLE_ENV = "MR_LISTER_STATE_TABLE"
_WORKFLOW_ARN_ENV = "MR_LISTER_PUBLICATION_WORKFLOW_ARN"
_QUERY_ENABLED_ENV = "MR_LISTER_PHASE7_QUERY_ENABLED"
_REQUEST_ENABLED_ENV = "MR_LISTER_PHASE7_REQUEST_ENABLED"
_PUBLICATION_ENABLED_ENV = "MR_LISTER_PHASE7_PUBLICATION_ENABLED"
_DISPATCHER_ENABLED_ENV = "MR_LISTER_PHASE7_DISPATCHER_ENABLED"
_WORKER_ENABLED_ENV = "MR_LISTER_PHASE7_WORKER_ENABLED"

_ENVIRONMENT_NAMES = (
    _RELEASE_FINGERPRINT_ENV,
    _APPLICATION_RELEASE_FINGERPRINT_ENV,
    _CONTRACT_FINGERPRINT_ENV,
    _CONTRACT_VERSION_ENV,
    _OPERATIONS_MODE_ENV,
    _PROFILE_ID_ENV,
    _PROFILE_VERSION_ENV,
    _PROFILE_FINGERPRINT_ENV,
    _PROFILE_PATH_ENV,
    _REGION_ENV,
    _STATE_TABLE_ENV,
    _WORKFLOW_ARN_ENV,
    _QUERY_ENABLED_ENV,
    _REQUEST_ENABLED_ENV,
    _PUBLICATION_ENABLED_ENV,
    _DISPATCHER_ENABLED_ENV,
    _WORKER_ENABLED_ENV,
)
_FORBIDDEN_CAPABILITY_ENVIRONMENT_NAMES = frozenset(
    {
        "MR_LISTER_ETSY_API_KEY",
        "MR_LISTER_ETSY_API_SECRET",
        "MR_LISTER_ETSY_TOKEN",
        "MR_LISTER_PRINTIFY_API_KEY",
        "MR_LISTER_PRINTIFY_SECRET_ARN",
        "MR_LISTER_PUBLICATION_RECOVERY_QUEUE_URL",
    }
)

_RECOVERY_ENTRYPOINT = (
    "mr_lister.cloud.phase715c_operations_entrypoints.publication_recovery_handler"
)
_RETENTION_ENTRYPOINT = (
    "mr_lister.cloud.phase715c_operations_entrypoints.publication_retention_handler"
)

Phase715cAwsService = Literal["dynamodb", "stepfunctions"]


class Phase715cOperationsHandler(Protocol):
    def __call__(
        self,
        event: Mapping[str, Any],
        context: object | None = None,
    ) -> dict[str, Any]: ...


class Phase715cAwsClientFactory(Protocol):
    def __call__(
        self,
        service_name: Phase715cAwsService,
        *,
        region_name: str,
    ) -> object: ...


class Phase715cOperationsRuntimeError(RuntimeError):
    """Value-free failure when the sealed operations runtime cannot be proven available."""


class _DynamoDbOperationsClient:
    """Expose only the three DynamoDB methods required by recovery and retention."""

    __slots__ = ("_client",)

    def __init__(self, client: object) -> None:
        if any(
            not callable(getattr(client, name, None))
            for name in ("get_item", "query", "transact_write_items")
        ):
            raise ValueError("Phase 7.15C DynamoDB client is invalid")
        self._client = client

    def get_item(self, **request: Any) -> Any:
        return self._client.get_item(**request)

    def query(self, **request: Any) -> Any:
        return self._client.query(**request)

    def transact_write_items(self, **request: Any) -> Any:
        return self._client.transact_write_items(**request)


class _StepFunctionsRecoveryClient:
    """Expose same-execution observation/redrive and structurally omit StartExecution."""

    __slots__ = ("_client",)

    def __init__(self, client: object) -> None:
        if any(
            not callable(getattr(client, name, None))
            for name in ("describe_execution", "redrive_execution")
        ):
            raise ValueError("Phase 7.15C Step Functions client is invalid")
        self._client = client

    def describe_execution(self, **request: Any) -> Any:
        return self._client.describe_execution(**request)

    def redrive_execution(self, **request: Any) -> Any:
        return self._client.redrive_execution(**request)


class _LazyOperationsEntrypoint:
    """Cache one release-verified handler without caching invocation material."""

    __slots__ = ("_builder", "_delegate", "_lock")

    def __init__(self, builder: Callable[[], Phase715cOperationsHandler]) -> None:
        self._builder = builder
        self._delegate: Phase715cOperationsHandler | None = None
        self._lock = Lock()

    def __call__(
        self,
        event: Mapping[str, Any],
        context: object | None = None,
    ) -> dict[str, Any]:
        try:
            return self._get()(event, context)
        except Exception:
            raise Phase715cOperationsRuntimeError(
                "Phase 7.15C operations runtime is unavailable"
            ) from None

    def _get(self) -> Phase715cOperationsHandler:
        delegate = self._delegate
        if delegate is not None:
            return delegate
        with self._lock:
            if self._delegate is None:
                self._delegate = self._builder()
            return self._delegate


class _UnifiedRecoveryHandler:
    """Route only the exact schedule or one-message SQS shape to prebuilt closed handlers."""

    __slots__ = ("_queue", "_sweep")

    def __init__(
        self,
        *,
        queue: Phase715cOperationsHandler,
        sweep: Phase715cOperationsHandler,
    ) -> None:
        self._queue = queue
        self._sweep = sweep

    def __call__(
        self,
        event: Mapping[str, Any],
        context: object | None = None,
    ) -> dict[str, Any]:
        if isinstance(event, Mapping) and dict(event) == {"kind": "publication_recovery_sweep"}:
            return self._sweep(event, context)
        if isinstance(event, Mapping) and set(event) == {"Records"}:
            records = event.get("Records")
            if (
                isinstance(records, list)
                and len(records) == 1
                and isinstance(records[0], Mapping)
                and records[0].get("eventSource") == "aws:sqs"
            ):
                return self._queue(event, context)
        raise ValueError("Phase 7.15C recovery invocation is invalid")


def _environment() -> Mapping[str, object]:
    """Read only non-secret release inputs and reject capability-bearing names by presence."""

    if any(name in os.environ for name in _FORBIDDEN_CAPABILITY_ENVIRONMENT_NAMES):
        raise ValueError("Phase 7.15C operations environment is invalid")
    return {name: os.environ.get(name) for name in _ENVIRONMENT_NAMES}


def _build_release_verified_handler(
    entrypoint: str,
    *,
    client_factory: Phase715cAwsClientFactory | None = None,
) -> Phase715cOperationsHandler:
    environment = _environment()
    from mr_lister.release.phase715c_operations import verify_phase715c_operations_release

    binding = verify_phase715c_operations_release(
        environment,
        expected_entrypoint=entrypoint,
    )
    if (
        getattr(binding, "entrypoint", None) != entrypoint
        or getattr(binding, "state_table", None) != environment.get(_STATE_TABLE_ENV)
        or getattr(binding, "publication_workflow_arn", None) != environment.get(_WORKFLOW_ARN_ENV)
        or getattr(binding, "profile_fingerprint", None)
        != environment.get(_PROFILE_FINGERPRINT_ENV)
    ):
        raise ValueError("Phase 7.15C operations release binding is invalid")

    # Application and SDK imports begin only after all packaged bytes and environment bindings pass.
    from mr_lister.cloud.phase715c_operations_composition import (
        compose_publication_recovery_queue_handler,
        compose_publication_recovery_sweep_handler,
        compose_publication_retention_handler,
    )
    from mr_lister.publication.profile_eligibility import (
        PinnedPublicationProfileEligibilityAuthority,
        build_publication_profile_eligibility,
    )
    from mr_lister.review_profile import FilesystemReviewProductAuthority

    profile_path = getattr(binding, "profile_path", None)
    if not isinstance(profile_path, str):
        raise ValueError("Phase 7.15C operations profile binding is invalid")
    authority = FilesystemReviewProductAuthority(profile_directory=Path(profile_path).parent)
    profile = authority.get_exact(
        profile_id=str(environment[_PROFILE_ID_ENV]),
        profile_version=int(str(environment[_PROFILE_VERSION_ENV])),
    )
    if profile.fingerprint != getattr(binding, "profile_fingerprint", None):
        raise ValueError("Phase 7.15C operations profile binding is invalid")
    application_release = getattr(binding, "application_release_fingerprint", None)
    if not isinstance(application_release, str):
        raise ValueError("Phase 7.15C operations application binding is invalid")
    eligibility = build_publication_profile_eligibility(
        profile_id=profile.profile.profile_id,
        profile_version=profile.profile.profile_version,
        profile_fingerprint=profile.fingerprint,
        release_manifest_fingerprint=application_release,
        phase6_profile_publish_enabled=profile.profile.publish_enabled,
    )
    eligibility = PinnedPublicationProfileEligibilityAuthority(eligibility).get_exact(
        profile_id=eligibility.profile_id,
        profile_version=eligibility.profile_version,
        profile_fingerprint=eligibility.profile_fingerprint,
        expected_sales_channel=eligibility.expected_sales_channel,
        release_manifest_fingerprint=eligibility.release_manifest_fingerprint,
        phase6_profile_publish_enabled=eligibility.phase6_profile_publish_enabled,
    )

    factory = client_factory or _default_aws_client_factory
    region = str(environment[_REGION_ENV])
    state_table = str(environment[_STATE_TABLE_ENV])
    workflow_arn = str(environment[_WORKFLOW_ARN_ENV])
    dynamodb = _DynamoDbOperationsClient(factory("dynamodb", region_name=region))
    if entrypoint == _RETENTION_ENTRYPOINT:
        return compose_publication_retention_handler(
            state_table=state_table,
            dynamodb=dynamodb,
        )
    step_functions = _StepFunctionsRecoveryClient(factory("stepfunctions", region_name=region))
    if entrypoint == _RECOVERY_ENTRYPOINT:
        queue = compose_publication_recovery_queue_handler(
            state_table=state_table,
            state_machine_arn=workflow_arn,
            release_manifest_fingerprint=application_release,
            exact_profile=profile,
            eligibility=eligibility,
            dynamodb=dynamodb,
            step_functions=step_functions,
        )
        sweep = compose_publication_recovery_sweep_handler(
            state_table=state_table,
            state_machine_arn=workflow_arn,
            release_manifest_fingerprint=application_release,
            exact_profile=profile,
            eligibility=eligibility,
            dynamodb=dynamodb,
            step_functions=step_functions,
        )
        return _UnifiedRecoveryHandler(queue=queue, sweep=sweep)
    raise ValueError("Phase 7.15C operations entrypoint is invalid")


def _default_aws_client_factory(
    service_name: Phase715cAwsService,
    *,
    region_name: str,
) -> object:
    """Create only a regional DynamoDB or Step Functions client after release verification."""

    if service_name not in {"dynamodb", "stepfunctions"}:
        raise ValueError("Unsupported Phase 7.15C operations AWS client")
    import boto3

    return boto3.client(service_name, region_name=region_name)


_publication_recovery = _LazyOperationsEntrypoint(
    lambda: _build_release_verified_handler(_RECOVERY_ENTRYPOINT)
)
_publication_retention = _LazyOperationsEntrypoint(
    lambda: _build_release_verified_handler(_RETENTION_ENTRYPOINT)
)


def publication_recovery_handler(
    event: Mapping[str, Any],
    context: object | None = None,
) -> dict[str, Any]:
    """Process one exact recovery-queue envelope or one exact scheduled recovery sweep."""

    return _publication_recovery(event, context)


def publication_retention_handler(
    event: Mapping[str, Any],
    context: object | None = None,
) -> dict[str, Any]:
    """Assign exact terminal retention through the marker-last transaction."""

    return _publication_retention(event, context)


__all__ = [
    "publication_recovery_handler",
    "publication_retention_handler",
]
