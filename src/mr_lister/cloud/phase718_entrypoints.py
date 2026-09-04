"""Lazy functional entrypoints for the Phase 7.18 enabled successor runtime.

The module registers no route, trigger, or IAM authority.  Infrastructure may bind these handlers
only under the reviewed 7.1.0 release.  Every graph validates the complete enabled configuration
before constructing an AWS client.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from collections.abc import Callable, Mapping
from threading import Lock
from typing import Any, Protocol

# Verification must not create an unmanifested cache file in an extracted bundle.
sys.dont_write_bytecode = True

_REJECTED_AUDIT_LOGGER = logging.getLogger("mr_lister.phase718.rejected_audit")
_QUERY_ENTRYPOINT = "mr_lister.cloud.phase718_entrypoints.publication_query_handler"
_REQUEST_ENTRYPOINT = "mr_lister.cloud.phase718_entrypoints.publication_request_handler"
_DISPATCHER_ENTRYPOINT = "mr_lister.cloud.phase718_entrypoints.publication_dispatcher_handler"
_WORKER_ENTRYPOINT = "mr_lister.cloud.phase718_entrypoints.publication_worker_handler"
_RECOVERY_ENTRYPOINT = "mr_lister.cloud.phase718_entrypoints.publication_recovery_handler"
_RETENTION_ENTRYPOINT = "mr_lister.cloud.phase718_entrypoints.publication_retention_handler"
_ENVIRONMENT_NAMES = (
    "AWS_REGION",
    "MR_LISTER_STATE_TABLE",
    "MR_LISTER_RELEASE_FINGERPRINT",
    "MR_LISTER_COGNITO_ISSUER",
    "MR_LISTER_COGNITO_CLIENT_ID",
    "MR_LISTER_COGNITO_SCOPE",
    "MR_LISTER_COGNITO_GROUP",
    "MR_LISTER_PRODUCT_PROFILE_ID",
    "MR_LISTER_PRODUCT_PROFILE_VERSION",
    "MR_LISTER_PRODUCT_PROFILE_FINGERPRINT",
    "MR_LISTER_PRODUCT_PROFILE_PATH",
    "MR_LISTER_PHASE7_CONTRACT_VERSION",
    "MR_LISTER_PHASE7_CONTRACT_FINGERPRINT",
    "MR_LISTER_PHASE7_ENABLED_RELEASE_FINGERPRINT",
    "MR_LISTER_PHASE7_CANARY_EVIDENCE_FINGERPRINT",
    "MR_LISTER_PHASE7_ENABLEMENT_EVIDENCE_FINGERPRINT",
    "MR_LISTER_PHASE7_ACTIVATION_MODE",
    "MR_LISTER_PHASE7_SCAFFOLD_ONLY",
    "MR_LISTER_PHASE7_QUERY_ENABLED",
    "MR_LISTER_PHASE7_REQUEST_ENABLED",
    "MR_LISTER_PHASE7_PUBLICATION_ENABLED",
    "MR_LISTER_PHASE7_WORKER_ENABLED",
    "MR_LISTER_PHASE7_DISPATCHER_ENABLED",
    "MR_LISTER_PHASE7_RECOVERY_ENABLED",
    "MR_LISTER_PHASE7_RETENTION_ENABLED",
    "MR_LISTER_PUBLICATION_WORKFLOW_ARN",
    "MR_LISTER_PUBLICATION_RECOVERY_QUEUE_URL",
    "MR_LISTER_PRINTIFY_SECRET_ARN",
)


class Phase718EntrypointError(RuntimeError):
    """Value-free startup failure for an enabled entrypoint."""


class Phase718Handler(Protocol):
    def __call__(
        self,
        event: Mapping[str, Any],
        context: object | None = None,
    ) -> dict[str, Any]: ...


class _VerifiedConfiguration(Protocol):
    region: str
    state_table: str
    application_release_fingerprint: str
    foundation: Any


class _LazyEnabledEntrypoint:
    __slots__ = ("_builder", "_delegate", "_lock")

    def __init__(self, builder: Callable[[], Phase718Handler]) -> None:
        self._builder = builder
        self._delegate: Phase718Handler | None = None
        self._lock = Lock()

    def __call__(
        self,
        event: Mapping[str, Any],
        context: object | None = None,
    ) -> dict[str, Any]:
        return self._get()(event, context)

    def _get(self) -> Phase718Handler:
        delegate = self._delegate
        if delegate is not None:
            return delegate
        with self._lock:
            if self._delegate is None:
                try:
                    self._delegate = self._builder()
                except Exception:
                    raise Phase718EntrypointError("Phase 7.18 runtime is unavailable") from None
            return self._delegate


def _environment() -> Mapping[str, object]:
    return {name: os.environ.get(name) for name in _ENVIRONMENT_NAMES}


def _configuration(expected_entrypoint: str) -> _VerifiedConfiguration:
    environment = _environment()
    _verify_release(environment, expected_entrypoint=expected_entrypoint)
    from mr_lister.cloud.phase718_configuration import load_phase718_enabled_configuration

    return load_phase718_enabled_configuration(environment)


def _verified_configuration(
    expected_entrypoint: str,
) -> tuple[Mapping[str, object], _VerifiedConfiguration]:
    environment = _environment()
    _verify_release(environment, expected_entrypoint=expected_entrypoint)
    from mr_lister.cloud.phase718_configuration import load_phase718_enabled_configuration

    return environment, load_phase718_enabled_configuration(environment)


def _verify_release(environment: Mapping[str, object], *, expected_entrypoint: str) -> None:
    from mr_lister.release.phase718 import verify_phase718_runtime_release

    verify_phase718_runtime_release(environment, expected_entrypoint=expected_entrypoint)


def _dynamodb_factory(service_name: str, *, region_name: str) -> object:
    if service_name != "dynamodb":
        raise ValueError
    import boto3

    return boto3.client("dynamodb", region_name=region_name)


def _build_query() -> Phase718Handler:
    configuration = _configuration(_QUERY_ENTRYPOINT)
    from mr_lister.cloud.phase718_composition import compose_phase718_query_handler

    return compose_phase718_query_handler(
        configuration,  # type: ignore[arg-type]
        client_factory=_dynamodb_factory,
    )


def _build_request() -> Phase718Handler:
    configuration = _configuration(_REQUEST_ENTRYPOINT)
    from mr_lister.cloud.phase718_composition import compose_phase718_request_handler

    return compose_phase718_request_handler(
        configuration,  # type: ignore[arg-type]
        client_factory=_dynamodb_factory,
    )


def _build_worker() -> Phase718Handler:
    environment, configuration = _verified_configuration(_WORKER_ENTRYPOINT)
    secret_arn = _required(environment, "MR_LISTER_PRINTIFY_SECRET_ARN")
    import boto3

    from mr_lister.cloud.phase7_provider_credentials import (
        build_phase7_publication_provider_credential_authority,
    )
    from mr_lister.cloud.phase718_composition import compose_phase718_worker_handler
    from mr_lister.publication.provider_boundary import RedirectSafePublicationTransport

    dynamodb = boto3.client("dynamodb", region_name=configuration.region)
    secrets = boto3.client("secretsmanager", region_name=configuration.region)
    credentials = build_phase7_publication_provider_credential_authority(
        client=secrets,  # type: ignore[arg-type]
        secret_arn=secret_arn,
    )
    return compose_phase718_worker_handler(
        configuration,  # type: ignore[arg-type]
        dynamodb=dynamodb,
        credentials=credentials,
        transport=RedirectSafePublicationTransport(),
        rejected_audit_writer=_write_rejected_audit,
    )


def _build_dispatcher() -> Phase718Handler:
    environment, configuration = _verified_configuration(_DISPATCHER_ENTRYPOINT)
    from mr_lister.cloud.phase7_operations_composition import (
        compose_publication_dispatcher_handler,
    )

    dynamodb = _aws_client("dynamodb", region=configuration.region)
    step_functions = _aws_client("stepfunctions", region=configuration.region)
    sqs = _aws_client("sqs", region=configuration.region)
    delegate = compose_publication_dispatcher_handler(
        state_table=configuration.state_table,
        state_machine_arn=_required(environment, "MR_LISTER_PUBLICATION_WORKFLOW_ARN"),
        dynamodb=dynamodb,
        step_functions=step_functions,
        sqs=sqs,
        recovery_queue_url=_required(
            environment,
            "MR_LISTER_PUBLICATION_RECOVERY_QUEUE_URL",
        ),
    )
    return _Phase718VersionedOperationsHandler(delegate)


def _build_recovery() -> Phase718Handler:
    environment, configuration = _verified_configuration(_RECOVERY_ENTRYPOINT)
    from mr_lister.cloud.phase7_operations_composition import (
        compose_publication_recovery_handler,
        compose_publication_recovery_sweep_handler,
    )

    dynamodb = _aws_client("dynamodb", region=configuration.region)
    step_functions = _aws_client("stepfunctions", region=configuration.region)
    values = {
        "state_table": configuration.state_table,
        "state_machine_arn": _required(
            environment,
            "MR_LISTER_PUBLICATION_WORKFLOW_ARN",
        ),
        "release_manifest_fingerprint": configuration.application_release_fingerprint,
        "exact_profile": configuration.foundation.profile.exact,
        "eligibility": configuration.foundation.eligibility,
        "dynamodb": dynamodb,
        "step_functions": step_functions,
    }
    return _Phase718VersionedOperationsHandler(
        _Phase718UnifiedRecoveryHandler(
            queue=compose_publication_recovery_handler(**values),
            sweep=compose_publication_recovery_sweep_handler(**values),
        )
    )


def _build_retention() -> Phase718Handler:
    configuration = _configuration(_RETENTION_ENTRYPOINT)
    from mr_lister.cloud.phase7_operations_composition import (
        compose_publication_retention_handler,
    )

    dynamodb = _aws_client("dynamodb", region=configuration.region)
    return _Phase718VersionedOperationsHandler(
        compose_publication_retention_handler(
            state_table=configuration.state_table,
            dynamodb=dynamodb,
        )
    )


def _aws_client(service_name: str, *, region: str) -> object:
    if service_name not in {"dynamodb", "stepfunctions", "sqs"}:
        raise ValueError
    import boto3

    return boto3.client(service_name, region_name=region)


class _Phase718UnifiedRecoveryHandler:
    __slots__ = ("_queue", "_sweep")

    def __init__(self, *, queue: Phase718Handler, sweep: Phase718Handler) -> None:
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
        raise RuntimeError("Phase 7.18 recovery invocation is invalid")


class _Phase718VersionedOperationsHandler:
    __slots__ = ("_delegate",)

    def __init__(self, delegate: Phase718Handler) -> None:
        self._delegate = delegate

    def __call__(
        self,
        event: Mapping[str, Any],
        context: object | None = None,
    ) -> dict[str, Any]:
        result = self._delegate(event, context)
        if result.get("contract_version") == "7.0.1":
            result = {**result, "contract_version": "7.1.0"}
        return result


def _write_rejected_audit(record: object) -> None:
    try:
        payload = record.model_dump(mode="json")  # type: ignore[attr-defined]
        _REJECTED_AUDIT_LOGGER.warning(json.dumps(payload, sort_keys=True, separators=(",", ":")))
    except Exception:
        _REJECTED_AUDIT_LOGGER.warning('{"decision":"rejected"}')


def _required(environment: Mapping[str, object], name: str) -> str:
    value = environment.get(name)
    if not isinstance(value, str) or not value or value != value.strip() or "\x00" in value:
        raise ValueError
    return value


_publication_query = _LazyEnabledEntrypoint(_build_query)
_publication_request = _LazyEnabledEntrypoint(_build_request)
_publication_dispatcher = _LazyEnabledEntrypoint(_build_dispatcher)
_publication_worker = _LazyEnabledEntrypoint(_build_worker)
_publication_recovery = _LazyEnabledEntrypoint(_build_recovery)
_publication_retention = _LazyEnabledEntrypoint(_build_retention)


def publication_query_handler(
    event: Mapping[str, Any],
    context: object | None = None,
) -> dict[str, Any]:
    return _publication_query(event, context)


def publication_request_handler(
    event: Mapping[str, Any],
    context: object | None = None,
) -> dict[str, Any]:
    return _publication_request(event, context)


def publication_worker_handler(
    event: Mapping[str, Any],
    context: object | None = None,
) -> dict[str, Any]:
    return _publication_worker(event, context)


def publication_dispatcher_handler(
    event: Mapping[str, Any],
    context: object | None = None,
) -> dict[str, Any]:
    return _publication_dispatcher(event, context)


def publication_recovery_handler(
    event: Mapping[str, Any],
    context: object | None = None,
) -> dict[str, Any]:
    return _publication_recovery(event, context)


def publication_retention_handler(
    event: Mapping[str, Any],
    context: object | None = None,
) -> dict[str, Any]:
    return _publication_retention(event, context)


__all__ = [
    "Phase718EntrypointError",
    "publication_dispatcher_handler",
    "publication_query_handler",
    "publication_recovery_handler",
    "publication_request_handler",
    "publication_retention_handler",
    "publication_worker_handler",
]
