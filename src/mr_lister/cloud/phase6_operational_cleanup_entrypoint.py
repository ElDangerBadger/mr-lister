"""Lazy entrypoint for scheduled Phase 6 terminal operational-record cleanup."""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from threading import Lock
from typing import Any, Protocol

from mr_lister.cloud.phase6_operational_cleanup_composition import (
    Phase6OperationalCleanupExecutionError,
    build_terminal_operational_cleanup_handler,
)


class OperationalCleanupLambdaHandler(Protocol):
    def __call__(
        self,
        event: Mapping[str, Any],
        context: object | None = None,
    ) -> dict[str, Any]: ...


class _LazyOperationalCleanupHandler:
    __slots__ = ("_builder", "_delegate", "_lock")

    def __init__(self, builder: Callable[[], OperationalCleanupLambdaHandler]) -> None:
        self._builder = builder
        self._delegate: OperationalCleanupLambdaHandler | None = None
        self._lock = Lock()

    def __call__(
        self,
        event: Mapping[str, Any],
        context: object | None = None,
    ) -> dict[str, Any]:
        try:
            return self._get()(event, context)
        except Exception:
            raise Phase6OperationalCleanupExecutionError(
                "Phase 6 operational cleanup failed safely"
            ) from None

    def _get(self) -> OperationalCleanupLambdaHandler:
        delegate = self._delegate
        if delegate is not None:
            return delegate
        with self._lock:
            if self._delegate is None:
                self._delegate = self._builder()
            return self._delegate


def _environment() -> Mapping[str, object]:
    return dict(os.environ)


_operational_cleanup = _LazyOperationalCleanupHandler(
    lambda: build_terminal_operational_cleanup_handler(_environment())
)


def terminal_operational_cleanup_handler(
    event: Mapping[str, Any],
    context: object | None = None,
) -> dict[str, Any]:
    return _operational_cleanup(event, context)


__all__ = ["terminal_operational_cleanup_handler"]
