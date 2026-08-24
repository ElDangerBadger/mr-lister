"""Release-verified private entrypoint for the Phase 7 approval guard."""

from __future__ import annotations

import os
import sys
from collections.abc import Callable, Mapping
from threading import Lock
from typing import Any, Protocol

# Prevent an extracted sealed bundle from creating executable, unmanifested cache files.  This is
# set before any release or application module can be imported.
sys.dont_write_bytecode = True


class Phase7GuardHandler(Protocol):
    def __call__(
        self,
        event: Mapping[str, Any],
        context: object | None = None,
    ) -> dict[str, Any]: ...


class Phase7GuardRuntimeError(RuntimeError):
    """Value-free failure when the sealed guard runtime cannot be proven available."""


class _LazyPublicationGuardEntrypoint:
    """Cache only a release-verified immutable handler, never invocation authority."""

    __slots__ = ("_builder", "_delegate", "_lock")

    def __init__(self, builder: Callable[[], Phase7GuardHandler]) -> None:
        self._builder = builder
        self._delegate: Phase7GuardHandler | None = None
        self._lock = Lock()

    def __call__(
        self,
        event: Mapping[str, Any],
        context: object | None = None,
    ) -> dict[str, Any]:
        try:
            return self._get()(event, context)
        except Exception:
            raise Phase7GuardRuntimeError("Phase 7 approval guard is unavailable") from None

    def _get(self) -> Phase7GuardHandler:
        delegate = self._delegate
        if delegate is not None:
            return delegate
        with self._lock:
            if self._delegate is None:
                self._delegate = self._builder()
            return self._delegate


def _environment() -> Mapping[str, object]:
    """Return one detached environment view for release and composition verification."""

    return dict(os.environ)


def _build_release_verified_handler() -> Phase7GuardHandler:
    environment = _environment()
    from mr_lister.release.phase7 import verify_phase7_guard_release

    binding = verify_phase7_guard_release(environment)
    profile_fingerprint = environment.get("MR_LISTER_PRODUCT_PROFILE_FINGERPRINT")
    if (
        type(profile_fingerprint) is not str
        or len(profile_fingerprint) != 64
        or profile_fingerprint == "0" * 64
        or any(character not in "0123456789abcdef" for character in profile_fingerprint)
        or getattr(binding, "profile_fingerprint", None) != profile_fingerprint
    ):
        raise ValueError("Phase 7 guard release profile binding is invalid")
    from mr_lister.cloud.phase7_guard_composition import build_publication_guard_handler

    return build_publication_guard_handler(environment)


_publication_guard = _LazyPublicationGuardEntrypoint(_build_release_verified_handler)


def publication_guard_verification_handler(
    event: Mapping[str, Any],
    context: object | None = None,
) -> dict[str, Any]:
    """Attest sealed status or verify one exact owner/aggregate authority by direct invoke."""

    return _publication_guard(event, context)


__all__ = [
    "Phase7GuardRuntimeError",
    "publication_guard_verification_handler",
]
