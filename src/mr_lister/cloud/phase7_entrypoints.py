"""Unregistered exact-disabled Phase 7.4 publication-status entrypoint.

The isolated scaffold may import this module to prove its source boundary, but contract 7.0.1 has
no enabled route.  Every invocation validates the checked disabled environment and then refuses
before constructing a DynamoDB client or reading request identity.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from threading import Lock
from typing import Any

from mr_lister.cloud.phase7_composition import (
    Phase7QueryHandler,
    build_disabled_publication_query_handler,
)
from mr_lister.publication.application import Phase7RuntimeDisabledError


class _LazyDisabledQueryEntrypoint:
    """Cache immutable configuration only; never retain an event, claim, owner, or job."""

    __slots__ = ("_builder", "_delegate", "_lock")

    def __init__(self, builder: Callable[[], Phase7QueryHandler]) -> None:
        self._builder = builder
        self._delegate: Phase7QueryHandler | None = None
        self._lock = Lock()

    def __call__(
        self,
        event: Mapping[str, Any],
        context: object | None = None,
    ) -> dict[str, Any]:
        try:
            return self._get()(event, context)
        except Exception:
            raise Phase7RuntimeDisabledError("Phase 7 publication runtime is disabled") from None

    def _get(self) -> Phase7QueryHandler:
        delegate = self._delegate
        if delegate is not None:
            return delegate
        with self._lock:
            if self._delegate is None:
                self._delegate = self._builder()
            return self._delegate


def _environment() -> Mapping[str, object]:
    """Return a fresh copy so a warm runtime cannot retain a mutable environment view."""

    return dict(os.environ)


_publication_query = _LazyDisabledQueryEntrypoint(
    lambda: build_disabled_publication_query_handler(_environment())
)


def publication_query_api_handler(
    event: Mapping[str, Any],
    context: object | None = None,
) -> dict[str, Any]:
    """Refuse the unregistered Phase 7 status route under contract 7.0.1."""

    return _publication_query(event, context)


__all__ = ["publication_query_api_handler"]
