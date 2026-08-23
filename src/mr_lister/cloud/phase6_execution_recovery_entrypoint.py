"""Lazy release-verified entrypoint for the isolated execution-recovery Lambda."""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from threading import Lock
from typing import Any, Protocol

from mr_lister.cloud.phase6_execution_recovery_composition import (
    build_execution_recovery_handler,
)
from mr_lister.control.execution_recovery import (
    ExecutionRecoveryExecutionError,
    ExecutionRecoveryInvocationError,
)
from mr_lister.release.phase6 import verify_phase6_packaged_release


class ExecutionRecoveryLambdaHandler(Protocol):
    def __call__(
        self,
        event: Mapping[str, Any],
        context: object | None = None,
    ) -> dict[str, Any]: ...


class _LazyExecutionRecoveryHandler:
    """Build once per warm environment without retaining invocation identity."""

    __slots__ = ("_builder", "_delegate", "_lock")

    def __init__(self, builder: Callable[[], ExecutionRecoveryLambdaHandler]) -> None:
        self._builder = builder
        self._delegate: ExecutionRecoveryLambdaHandler | None = None
        self._lock = Lock()

    def __call__(
        self,
        event: Mapping[str, Any],
        context: object | None = None,
    ) -> dict[str, Any]:
        try:
            return self._get()(event, context)
        except (ExecutionRecoveryInvocationError, ExecutionRecoveryExecutionError):
            raise
        except Exception:
            raise ExecutionRecoveryExecutionError(
                "Stuck-execution recovery failed safely"
            ) from None

    def _get(self) -> ExecutionRecoveryLambdaHandler:
        delegate = self._delegate
        if delegate is not None:
            return delegate
        with self._lock:
            if self._delegate is None:
                self._delegate = self._builder()
            return self._delegate


def _environment() -> Mapping[str, object]:
    # Freeze a shallow copy, then bind it to every byte in the sealed Lambda release.
    environment: dict[str, object] = dict(os.environ)
    verify_phase6_packaged_release(environment, component="lambda")
    return environment


_recovery = _LazyExecutionRecoveryHandler(lambda: build_execution_recovery_handler(_environment()))


def execution_recovery_handler(
    event: Mapping[str, Any],
    context: object | None = None,
) -> dict[str, Any]:
    return _recovery(event, context)


__all__ = ["ExecutionRecoveryLambdaHandler", "execution_recovery_handler"]
