"""Release-first private entrypoint for one exact-bound Phase 7 canary."""

from __future__ import annotations

import importlib
import json
import os
import sys
from collections.abc import Callable, Mapping
from threading import Lock
from typing import Any, Protocol

# An extracted sealed bundle must not create executable, unmanifested cache files.  Keep this
# before any release or application import.
sys.dont_write_bytecode = True


class Phase7CanaryHandler(Protocol):
    def __call__(
        self,
        event: Mapping[str, Any],
        context: object | None = None,
    ) -> dict[str, str]: ...


class Phase7CanaryEntrypointError(RuntimeError):
    """Value-free refusal when the sealed private canary cannot be proven available."""


class _LazyPublicationCanaryEntrypoint:
    """Cache only a release-verified handler, never invocation or credential material."""

    __slots__ = ("_builder", "_delegate", "_lock")

    def __init__(self, builder: Callable[[], Phase7CanaryHandler]) -> None:
        self._builder = builder
        self._delegate: Phase7CanaryHandler | None = None
        self._lock = Lock()

    def __call__(
        self,
        event: Mapping[str, Any],
        context: object | None = None,
    ) -> dict[str, str]:
        try:
            return self._get()(event, context)
        except Exception:
            raise Phase7CanaryEntrypointError("Phase 7 canary runtime is unavailable") from None

    def _get(self) -> Phase7CanaryHandler:
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


def _build_release_verified_handler() -> Phase7CanaryHandler:
    environment = _environment()
    from mr_lister.release.phase7_canary import verify_phase7_canary_release

    verified = verify_phase7_canary_release(environment)
    payload = getattr(verified, "binding_payload", None)
    if type(payload) is not dict:
        raise ValueError("Phase 7 canary release binding is invalid")

    # Import the application model only after the stdlib-only verifier has authenticated every
    # packaged byte and its canonical binding payload.
    canary_runtime = importlib.import_module("mr_lister.publication.canary_runtime")
    PublicationCanaryBinding = canary_runtime.PublicationCanaryBinding
    binding = PublicationCanaryBinding.model_validate_json(
        json.dumps(payload, sort_keys=True, separators=(",", ":"))
    )
    if binding.model_dump(mode="json") != payload:
        raise ValueError("Phase 7 canary release binding is invalid")

    release_fingerprint = _verified_string(verified, "release_fingerprint")
    profile_fingerprint = _verified_string(verified, "profile_fingerprint")
    binding_fingerprint = _verified_string(verified, "binding_fingerprint")
    binding_mode = _verified_string(verified, "binding_mode")
    application_release = _verified_string(
        verified,
        "application_release_fingerprint",
    )
    expected = {
        "MR_LISTER_PHASE7_CANARY_RELEASE_FINGERPRINT": release_fingerprint,
        "MR_LISTER_PRODUCT_PROFILE_FINGERPRINT": profile_fingerprint,
        "MR_LISTER_PHASE7_CANARY_BINDING_FINGERPRINT": binding_fingerprint,
        "MR_LISTER_PHASE7_CANARY_MODE": binding_mode,
        "MR_LISTER_RELEASE_FINGERPRINT": application_release,
    }
    if (
        any(environment.get(name) != value for name, value in expected.items())
        or binding.fingerprint != binding_fingerprint
        or binding.mode.value != binding_mode
        or binding.release_manifest_fingerprint != application_release
    ):
        raise ValueError("Phase 7 canary release binding is invalid")

    from mr_lister.cloud.phase7_canary_composition import build_publication_canary_handler

    return build_publication_canary_handler(environment, binding=binding)


def _verified_string(value: object, name: str) -> str:
    result = getattr(value, name, None)
    if type(result) is not str or not result or result != result.strip() or len(result) > 4_096:
        raise ValueError("Phase 7 canary release binding is invalid")
    return result


_publication_canary = _LazyPublicationCanaryEntrypoint(_build_release_verified_handler)


def publication_canary_handler(
    event: Mapping[str, Any],
    context: object | None = None,
) -> dict[str, str]:
    """Advance only the packaged owner/aggregate canary by direct private invocation."""

    return _publication_canary(event, context)


__all__ = [
    "Phase7CanaryEntrypointError",
    "publication_canary_handler",
]
