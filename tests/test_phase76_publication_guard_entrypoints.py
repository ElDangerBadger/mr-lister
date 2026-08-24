"""Release-first startup tests for the private Phase 7.6 Lambda entrypoint."""

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

from mr_lister.cloud import phase7_guard_entrypoint as entrypoint

PROFILE_FINGERPRINT = "c" * 64


class _PoisonComposition(ModuleType):
    def __getattr__(self, name: str) -> object:
        raise AssertionError(f"composition accessed before release verification: {name}")


class _PoisonEvent(Mapping[str, Any]):
    def __getitem__(self, key: str) -> Any:
        raise AssertionError(f"event accessed before startup authority: {key}")

    def __iter__(self) -> Iterator[str]:
        raise AssertionError("event iterated before startup authority")

    def __len__(self) -> int:
        raise AssertionError("event sized before startup authority")


def _release_module(verifier: Any) -> ModuleType:
    module = ModuleType("mr_lister.release.phase7")
    module.verify_phase7_guard_release = verifier  # type: ignore[attr-defined]
    return module


def test_release_failure_prevents_composition_import_and_event_access(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_release(environment: object) -> object:
        del environment
        raise RuntimeError("unsealed bytes")

    monkeypatch.setitem(sys.modules, "mr_lister.release.phase7", _release_module(fail_release))
    monkeypatch.setitem(
        sys.modules,
        "mr_lister.cloud.phase7_guard_composition",
        _PoisonComposition("mr_lister.cloud.phase7_guard_composition"),
    )
    monkeypatch.setattr(
        entrypoint,
        "_environment",
        lambda: {"MR_LISTER_PRODUCT_PROFILE_FINGERPRINT": PROFILE_FINGERPRINT},
    )
    runtime = entrypoint._LazyPublicationGuardEntrypoint(entrypoint._build_release_verified_handler)

    with pytest.raises(entrypoint.Phase7GuardRuntimeError) as captured:
        runtime(_PoisonEvent())

    assert str(captured.value) == "Phase 7 approval guard is unavailable"
    assert captured.value.__cause__ is None


def test_release_profile_mismatch_prevents_composition_access(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(
        sys.modules,
        "mr_lister.release.phase7",
        _release_module(lambda environment: SimpleNamespace(profile_fingerprint="d" * 64)),
    )
    monkeypatch.setitem(
        sys.modules,
        "mr_lister.cloud.phase7_guard_composition",
        _PoisonComposition("mr_lister.cloud.phase7_guard_composition"),
    )
    monkeypatch.setattr(
        entrypoint,
        "_environment",
        lambda: {"MR_LISTER_PRODUCT_PROFILE_FINGERPRINT": PROFILE_FINGERPRINT},
    )

    with pytest.raises(ValueError, match="release profile binding is invalid"):
        entrypoint._build_release_verified_handler()


def test_verified_binding_precedes_composition_and_passes_one_detached_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    order: list[str] = []
    environment = {"MR_LISTER_PRODUCT_PROFILE_FINGERPRINT": PROFILE_FINGERPRINT}

    def verify(value: object) -> object:
        assert value is environment
        order.append("release")
        return SimpleNamespace(profile_fingerprint=PROFILE_FINGERPRINT)

    expected_handler = lambda event, context=None: {"ok": True}  # noqa: E731

    def build(value: object) -> object:
        assert value is environment
        order.append("composition")
        return expected_handler

    composition = ModuleType("mr_lister.cloud.phase7_guard_composition")
    composition.build_publication_guard_handler = build  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "mr_lister.release.phase7", _release_module(verify))
    monkeypatch.setitem(sys.modules, "mr_lister.cloud.phase7_guard_composition", composition)
    monkeypatch.setattr(entrypoint, "_environment", lambda: environment)

    assert entrypoint._build_release_verified_handler() is expected_handler
    assert order == ["release", "composition"]


def test_lazy_entrypoint_builds_one_verified_delegate_under_concurrency() -> None:
    calls = 0

    def build() -> Any:
        nonlocal calls
        calls += 1
        return lambda event, context=None: {"operation": event["operation"]}

    runtime = entrypoint._LazyPublicationGuardEntrypoint(build)
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = tuple(pool.map(lambda _: runtime({"operation": "status"}), range(32)))

    assert calls == 1
    assert results == ({"operation": "status"},) * 32


def test_environment_is_detached_and_entrypoint_top_level_is_stdlib_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MR_LISTER_PHASE76_DETACHED_TEST", "before")
    detached = entrypoint._environment()
    monkeypatch.setenv("MR_LISTER_PHASE76_DETACHED_TEST", "after")
    assert detached["MR_LISTER_PHASE76_DETACHED_TEST"] == "before"
    assert detached is not os.environ

    source_path = Path(entrypoint.__file__ or "")
    tree = ast.parse(source_path.read_text(encoding="utf-8"))
    top_level_imports = [
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
        for node in top_level_imports
    )
    assert sys.dont_write_bytecode is True
    assert entrypoint.publication_guard_verification_handler.__module__ == (
        "mr_lister.cloud.phase7_guard_entrypoint"
    )
