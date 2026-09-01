"""Release-first refusal-only entrypoints for the Phase 7 production-disabled candidate.

The module imports only the standard library.  Each exported handler authenticates the sealed
artifact, frozen contract, checked profile, exact handler identity, and disabled environment
before constructing a local refusal object.  No application module, AWS SDK, credential, secret,
transport, provider graph, or invocation field is loaded or inspected.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Callable, Mapping
from threading import Lock
from typing import Any, Protocol

# An extracted sealed bundle must never create executable bytes absent from its manifest.
sys.dont_write_bytecode = True

_RELEASE_FINGERPRINT_ENV = "MR_LISTER_PHASE7_PRODUCTION_RELEASE_FINGERPRINT"
_APPLICATION_RELEASE_FINGERPRINT_ENV = "MR_LISTER_RELEASE_FINGERPRINT"
_CONTRACT_FINGERPRINT_ENV = "MR_LISTER_PHASE7_CONTRACT_FINGERPRINT"
_CONTRACT_VERSION_ENV = "MR_LISTER_PHASE7_CONTRACT_VERSION"
_ACTIVATION_MODE_ENV = "MR_LISTER_PHASE7_ACTIVATION_MODE"
_PROFILE_ID_ENV = "MR_LISTER_PRODUCT_PROFILE_ID"
_PROFILE_VERSION_ENV = "MR_LISTER_PRODUCT_PROFILE_VERSION"
_PROFILE_FINGERPRINT_ENV = "MR_LISTER_PRODUCT_PROFILE_FINGERPRINT"
_PROFILE_PATH_ENV = "MR_LISTER_PRODUCT_PROFILE_PATH"
_SCAFFOLD_ONLY_ENV = "MR_LISTER_PHASE7_SCAFFOLD_ONLY"
_QUERY_ENABLED_ENV = "MR_LISTER_PHASE7_QUERY_ENABLED"
_REQUEST_ENABLED_ENV = "MR_LISTER_PHASE7_REQUEST_ENABLED"
_PUBLICATION_ENABLED_ENV = "MR_LISTER_PHASE7_PUBLICATION_ENABLED"
_PRODUCTION_CANDIDATE_ENABLED_ENV = "MR_LISTER_PHASE7_PRODUCTION_CANDIDATE_ENABLED"
_REGION_ENV = "AWS_REGION"
_STATE_TABLE_ENV = "MR_LISTER_STATE_TABLE"
_COGNITO_ISSUER_ENV = "MR_LISTER_COGNITO_ISSUER"
_COGNITO_CLIENT_ID_ENV = "MR_LISTER_COGNITO_CLIENT_ID"
_COGNITO_SCOPE_ENV = "MR_LISTER_COGNITO_SCOPE"
_COGNITO_GROUP_ENV = "MR_LISTER_COGNITO_GROUP"

_ENVIRONMENT_NAMES = (
    _RELEASE_FINGERPRINT_ENV,
    _APPLICATION_RELEASE_FINGERPRINT_ENV,
    _CONTRACT_FINGERPRINT_ENV,
    _CONTRACT_VERSION_ENV,
    _ACTIVATION_MODE_ENV,
    _PROFILE_ID_ENV,
    _PROFILE_VERSION_ENV,
    _PROFILE_FINGERPRINT_ENV,
    _PROFILE_PATH_ENV,
    _SCAFFOLD_ONLY_ENV,
    _QUERY_ENABLED_ENV,
    _REQUEST_ENABLED_ENV,
    _PUBLICATION_ENABLED_ENV,
    _PRODUCTION_CANDIDATE_ENABLED_ENV,
    _REGION_ENV,
    _STATE_TABLE_ENV,
    _COGNITO_ISSUER_ENV,
    _COGNITO_CLIENT_ID_ENV,
    _COGNITO_SCOPE_ENV,
    _COGNITO_GROUP_ENV,
)
_FORBIDDEN_CAPABILITY_ENVIRONMENT_NAMES = frozenset(
    {
        "MR_LISTER_ETSY_API_KEY",
        "MR_LISTER_ETSY_API_SECRET",
        "MR_LISTER_ETSY_TOKEN",
        "MR_LISTER_PRINTIFY_API_KEY",
        "MR_LISTER_PRINTIFY_SECRET_ARN",
        "MR_LISTER_PUBLICATION_RECOVERY_QUEUE_URL",
        "MR_LISTER_PUBLICATION_WORKFLOW_ARN",
    }
)

_QUERY_ENTRYPOINT = "mr_lister.cloud.phase7_production_entrypoints.publication_query_handler"
_REQUEST_ENTRYPOINT = "mr_lister.cloud.phase7_production_entrypoints.publication_request_handler"
_DISPATCHER_ENTRYPOINT = (
    "mr_lister.cloud.phase7_production_entrypoints.publication_dispatcher_handler"
)
_WORKER_ENTRYPOINT = "mr_lister.cloud.phase7_production_entrypoints.publication_worker_handler"
_RECOVERY_ENTRYPOINT = "mr_lister.cloud.phase7_production_entrypoints.publication_recovery_handler"
_RETENTION_ENTRYPOINT = (
    "mr_lister.cloud.phase7_production_entrypoints.publication_retention_handler"
)


class Phase7ProductionDisabledEntrypointError(RuntimeError):
    """Value-free refusal for every disabled candidate invocation."""


class _ProductionDisabledHandler(Protocol):
    def __call__(
        self,
        event: Mapping[str, Any],
        context: object | None = None,
    ) -> dict[str, Any]: ...


class _RefuseWithoutObservation:
    """A capability-free handler that discards opaque arguments without reading them."""

    __slots__ = ()

    def __call__(
        self,
        event: Mapping[str, Any],
        context: object | None = None,
    ) -> dict[str, Any]:
        del event, context
        raise Phase7ProductionDisabledEntrypointError("Phase 7 production candidate is disabled")


class _LazyReleaseVerifiedRefusal:
    """Cache only an authenticated refusal object, never invocation or capability data."""

    __slots__ = ("_builder", "_delegate", "_lock")

    def __init__(self, builder: Callable[[], _ProductionDisabledHandler]) -> None:
        self._builder = builder
        self._delegate: _ProductionDisabledHandler | None = None
        self._lock = Lock()

    def __call__(
        self,
        event: Mapping[str, Any],
        context: object | None = None,
    ) -> dict[str, Any]:
        try:
            return self._get()(event, context)
        except Exception:
            raise Phase7ProductionDisabledEntrypointError(
                "Phase 7 production candidate is disabled"
            ) from None

    def _get(self) -> _ProductionDisabledHandler:
        delegate = self._delegate
        if delegate is not None:
            return delegate
        with self._lock:
            if self._delegate is None:
                self._delegate = self._builder()
            return self._delegate


def _environment() -> Mapping[str, object]:
    """Read only the non-secret release binding and reject capability-bearing app names."""

    if any(name in os.environ for name in _FORBIDDEN_CAPABILITY_ENVIRONMENT_NAMES):
        raise ValueError("Phase 7 production-disabled environment is invalid")
    return {name: os.environ.get(name) for name in _ENVIRONMENT_NAMES}


def _build_release_verified_refusal(entrypoint: str) -> _ProductionDisabledHandler:
    environment = _environment()
    from mr_lister.release.phase7_production_disabled import (
        verify_phase7_production_disabled_release,
    )

    verified = verify_phase7_production_disabled_release(
        environment,
        expected_entrypoint=entrypoint,
    )
    if getattr(verified, "entrypoint", None) != entrypoint:
        raise ValueError("Phase 7 production-disabled handler binding is invalid")
    return _RefuseWithoutObservation()


_publication_query = _LazyReleaseVerifiedRefusal(
    lambda: _build_release_verified_refusal(_QUERY_ENTRYPOINT)
)
_publication_request = _LazyReleaseVerifiedRefusal(
    lambda: _build_release_verified_refusal(_REQUEST_ENTRYPOINT)
)
_publication_dispatcher = _LazyReleaseVerifiedRefusal(
    lambda: _build_release_verified_refusal(_DISPATCHER_ENTRYPOINT)
)
_publication_worker = _LazyReleaseVerifiedRefusal(
    lambda: _build_release_verified_refusal(_WORKER_ENTRYPOINT)
)
_publication_recovery = _LazyReleaseVerifiedRefusal(
    lambda: _build_release_verified_refusal(_RECOVERY_ENTRYPOINT)
)
_publication_retention = _LazyReleaseVerifiedRefusal(
    lambda: _build_release_verified_refusal(_RETENTION_ENTRYPOINT)
)


def publication_query_handler(
    event: Mapping[str, Any],
    context: object | None = None,
) -> dict[str, Any]:
    """Refuse the sealed but disabled publication-query candidate."""

    return _publication_query(event, context)


def publication_request_handler(
    event: Mapping[str, Any],
    context: object | None = None,
) -> dict[str, Any]:
    """Refuse the sealed but disabled publication-request candidate."""

    return _publication_request(event, context)


def publication_dispatcher_handler(
    event: Mapping[str, Any],
    context: object | None = None,
) -> dict[str, Any]:
    """Refuse the sealed but disabled publication-dispatcher candidate."""

    return _publication_dispatcher(event, context)


def publication_worker_handler(
    event: Mapping[str, Any],
    context: object | None = None,
) -> dict[str, Any]:
    """Refuse the sealed but disabled publication-worker candidate."""

    return _publication_worker(event, context)


def publication_recovery_handler(
    event: Mapping[str, Any],
    context: object | None = None,
) -> dict[str, Any]:
    """Refuse the sealed but disabled publication-recovery candidate."""

    return _publication_recovery(event, context)


def publication_retention_handler(
    event: Mapping[str, Any],
    context: object | None = None,
) -> dict[str, Any]:
    """Refuse the sealed but disabled publication-retention candidate."""

    return _publication_retention(event, context)


__all__ = [
    "Phase7ProductionDisabledEntrypointError",
    "publication_dispatcher_handler",
    "publication_query_handler",
    "publication_recovery_handler",
    "publication_request_handler",
    "publication_retention_handler",
    "publication_worker_handler",
]
