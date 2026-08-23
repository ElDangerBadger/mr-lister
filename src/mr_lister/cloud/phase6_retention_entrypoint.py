"""Lazy entrypoint for the scheduled Phase 6 source-version retention Lambda."""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from threading import Lock
from typing import Any, Protocol

from mr_lister.cloud.phase6_retention_composition import (
    Phase6RetentionExecutionError,
    build_source_version_retention_handler,
)


class RetentionLambdaHandler(Protocol):
    def __call__(
        self,
        event: Mapping[str, Any],
        context: object | None = None,
    ) -> dict[str, Any]: ...


class _LazyRetentionHandler:
    __slots__ = ("_builder", "_delegate", "_lock")

    def __init__(self, builder: Callable[[], RetentionLambdaHandler]) -> None:
        self._builder = builder
        self._delegate: RetentionLambdaHandler | None = None
        self._lock = Lock()

    def __call__(
        self,
        event: Mapping[str, Any],
        context: object | None = None,
    ) -> dict[str, Any]:
        try:
            return self._get()(event, context)
        except Exception:
            raise Phase6RetentionExecutionError(
                "Phase 6 source retention sweep failed safely"
            ) from None

    def _get(self) -> RetentionLambdaHandler:
        delegate = self._delegate
        if delegate is not None:
            return delegate
        with self._lock:
            if self._delegate is None:
                self._delegate = self._builder()
            return self._delegate


def _environment() -> Mapping[str, object]:
    return dict(os.environ)


_retention = _LazyRetentionHandler(lambda: build_source_version_retention_handler(_environment()))


def source_version_retention_handler(
    event: Mapping[str, Any],
    context: object | None = None,
) -> dict[str, Any]:
    return _retention(event, context)


__all__ = ["source_version_retention_handler"]
