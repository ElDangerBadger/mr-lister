"""Release-first startup tests for the private Phase 7 canary entrypoint."""

from __future__ import annotations

import ast
import os
import sys
from collections.abc import Iterator, Mapping
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

from mr_lister.cloud import phase7_canary_entrypoint as entrypoint
from mr_lister.publication.canary_runtime import (
    PublicationCanaryBinding,
    PublicationCanaryMode,
    build_publication_canary_binding,
)
from tests.test_phase72_publication_execution import Harness


class _PoisonModule(ModuleType):
    def __getattr__(self, name: str) -> object:
        raise AssertionError(f"application module accessed before release verification: {name}")


class _PoisonEvent(Mapping[str, Any]):
    def __getitem__(self, key: str) -> Any:
        raise AssertionError(f"event accessed before startup authority: {key}")

    def __iter__(self) -> Iterator[str]:
        raise AssertionError("event iterated before startup authority")

    def __len__(self) -> int:
        raise AssertionError("event sized before startup authority")


class _TrackingRuntimeModule(ModuleType):
    def __init__(self, order: list[str]) -> None:
        super().__init__("mr_lister.publication.canary_runtime")
        self._order = order

    def __getattr__(self, name: str) -> object:
        if name == "PublicationCanaryBinding":
            self._order.append("binding-model")
            return PublicationCanaryBinding
        raise AttributeError(name)


def _release_module(verifier: Any) -> ModuleType:
    module = ModuleType("mr_lister.release.phase7_canary")
    module.verify_phase7_canary_release = verifier  # type: ignore[attr-defined]
    return module


def _binding():  # type: ignore[no-untyped-def]
    harness = Harness()
    return build_publication_canary_binding(
        harness.authority,
        mode=PublicationCanaryMode.READ_ONLY_PREFLIGHT,
    )


def _environment(binding: PublicationCanaryBinding) -> dict[str, object]:
    return {
        "MR_LISTER_PHASE7_CANARY_RELEASE_FINGERPRINT": "c" * 64,
        "MR_LISTER_PRODUCT_PROFILE_FINGERPRINT": (
            "5de1257141cfdacb1731f68bb9113712957483b33d5b0f7115afdba86eb7476c"
        ),
        "MR_LISTER_PHASE7_CANARY_BINDING_FINGERPRINT": binding.fingerprint,
        "MR_LISTER_PHASE7_CANARY_MODE": binding.mode.value,
        "MR_LISTER_RELEASE_FINGERPRINT": binding.release_manifest_fingerprint,
    }


def _verified(binding: PublicationCanaryBinding) -> object:
    return SimpleNamespace(
        release_fingerprint="c" * 64,
        profile_fingerprint=("5de1257141cfdacb1731f68bb9113712957483b33d5b0f7115afdba86eb7476c"),
        binding_fingerprint=binding.fingerprint,
        binding_mode=binding.mode.value,
        application_release_fingerprint=binding.release_manifest_fingerprint,
        binding_payload=binding.model_dump(mode="json"),
    )


def test_release_failure_prevents_application_import_and_event_access(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_release(environment: object) -> object:
        del environment
        raise RuntimeError("unsealed bytes")

    monkeypatch.setitem(
        sys.modules,
        "mr_lister.release.phase7_canary",
        _release_module(fail_release),
    )
    monkeypatch.setitem(
        sys.modules,
        "mr_lister.publication.canary_runtime",
        _PoisonModule("mr_lister.publication.canary_runtime"),
    )
    monkeypatch.setitem(
        sys.modules,
        "mr_lister.cloud.phase7_canary_composition",
        _PoisonModule("mr_lister.cloud.phase7_canary_composition"),
    )
    runtime = entrypoint._LazyPublicationCanaryEntrypoint(
        entrypoint._build_release_verified_handler
    )

    with pytest.raises(entrypoint.Phase7CanaryEntrypointError) as captured:
        runtime(_PoisonEvent())

    assert str(captured.value) == "Phase 7 canary runtime is unavailable"
    assert captured.value.__cause__ is None


def test_verified_bytes_precede_binding_parse_clients_and_composition(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    binding = _binding()
    environment = _environment(binding)
    order: list[str] = []

    def verify(value: object) -> object:
        assert value is environment
        order.append("release")
        return _verified(binding)

    expected_handler = lambda event, context=None: {"action": "terminal"}  # noqa: E731

    def build(value: object, *, binding: object) -> object:
        assert value is environment
        assert binding == _binding_value
        order.append("composition")
        return expected_handler

    _binding_value = binding
    composition = ModuleType("mr_lister.cloud.phase7_canary_composition")
    composition.build_publication_canary_handler = build  # type: ignore[attr-defined]
    monkeypatch.setitem(
        sys.modules,
        "mr_lister.release.phase7_canary",
        _release_module(verify),
    )
    monkeypatch.setitem(
        sys.modules,
        "mr_lister.publication.canary_runtime",
        _TrackingRuntimeModule(order),
    )
    monkeypatch.setitem(
        sys.modules,
        "mr_lister.cloud.phase7_canary_composition",
        composition,
    )
    monkeypatch.setattr(entrypoint, "_environment", lambda: environment)

    assert entrypoint._build_release_verified_handler() is expected_handler
    assert order == ["release", "binding-model", "composition"]


def test_release_binding_drift_prevents_composition_import(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    binding = _binding()
    environment = _environment(binding)
    environment["MR_LISTER_PHASE7_CANARY_MODE"] = "publish_once"
    monkeypatch.setitem(
        sys.modules,
        "mr_lister.release.phase7_canary",
        _release_module(lambda _environment: _verified(binding)),
    )
    monkeypatch.setitem(
        sys.modules,
        "mr_lister.cloud.phase7_canary_composition",
        _PoisonModule("mr_lister.cloud.phase7_canary_composition"),
    )
    monkeypatch.setattr(entrypoint, "_environment", lambda: environment)

    with pytest.raises(ValueError, match="release binding is invalid"):
        entrypoint._build_release_verified_handler()


def test_lazy_entrypoint_builds_one_verified_delegate_under_concurrency() -> None:
    calls = 0

    def build() -> Any:
        nonlocal calls
        calls += 1
        return lambda event, context=None: {
            "action": event["action"],
            "aggregate_state": "publication_requested",
        }

    runtime = entrypoint._LazyPublicationCanaryEntrypoint(build)
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = tuple(pool.map(lambda _: runtime({"action": "terminal"}), range(32)))

    assert calls == 1
    assert results == ({"action": "terminal", "aggregate_state": "publication_requested"},) * 32


def test_environment_is_detached_and_entrypoint_top_level_is_stdlib_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MR_LISTER_PHASE710_DETACHED_TEST", "before")
    detached = entrypoint._environment()
    monkeypatch.setenv("MR_LISTER_PHASE710_DETACHED_TEST", "after")

    assert detached["MR_LISTER_PHASE710_DETACHED_TEST"] == "before"
    assert detached is not os.environ
    path = Path(entrypoint.__file__ or "")
    tree = ast.parse(path.read_text(encoding="utf-8"))
    imports = [
        node
        for node in tree.body
        if isinstance(node, (ast.Import, ast.ImportFrom))
        and not (isinstance(node, ast.ImportFrom) and node.module == "__future__")
    ]
    assert all(
        not (
            isinstance(node, ast.ImportFrom)
            and node.module is not None
            and node.module.startswith("mr_lister")
        )
        and not (
            isinstance(node, ast.Import)
            and any(alias.name.startswith("mr_lister") for alias in node.names)
        )
        for node in imports
    )
    assert sys.dont_write_bytecode is True
    assert entrypoint.__all__ == [
        "Phase7CanaryEntrypointError",
        "publication_canary_handler",
    ]
