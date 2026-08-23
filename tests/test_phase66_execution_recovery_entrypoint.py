from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pytest

from mr_lister.cloud import phase6_execution_recovery_entrypoint as entrypoint
from mr_lister.control.execution_recovery import (
    ExecutionRecoveryExecutionError,
    ExecutionRecoveryInvocationError,
)


class FakeHandler:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def __call__(
        self,
        event: dict[str, Any],
        context: object | None = None,
    ) -> dict[str, Any]:
        del context
        self.events.append(event)
        return {"candidates_scanned": 0}


def test_lazy_entrypoint_builds_once_across_concurrent_warm_invocations() -> None:
    builds = 0
    delegate = FakeHandler()

    def build() -> FakeHandler:
        nonlocal builds
        builds += 1
        return delegate

    lazy = entrypoint._LazyExecutionRecoveryHandler(build)
    event = {"source": "stuck-execution-sweeper"}

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = tuple(pool.map(lambda _index: lazy(event), range(24)))

    assert builds == 1
    assert results == ({"candidates_scanned": 0},) * 24
    assert len(delegate.events) == 24


def test_lazy_entrypoint_retains_no_event_or_authority_identity() -> None:
    delegate = FakeHandler()
    lazy = entrypoint._LazyExecutionRecoveryHandler(lambda: delegate)

    lazy({"source": "stuck-execution-sweeper"})

    assert set(lazy.__slots__) == {"_builder", "_delegate", "_lock"}
    assert not any("event" in slot or "job" in slot or "work" in slot for slot in lazy.__slots__)


def test_lazy_entrypoint_masks_builder_dependency_details() -> None:
    def fail() -> FakeHandler:
        raise RuntimeError("private table and credential detail")

    lazy = entrypoint._LazyExecutionRecoveryHandler(fail)

    with pytest.raises(ExecutionRecoveryExecutionError) as raised:
        lazy({"source": "stuck-execution-sweeper"})

    assert str(raised.value) == "Stuck-execution recovery failed safely"
    assert raised.value.__cause__ is None


@pytest.mark.parametrize(
    "error",
    [
        ExecutionRecoveryInvocationError("Invalid stuck-execution recovery invocation"),
        ExecutionRecoveryExecutionError("Stuck-execution recovery failed safely"),
    ],
)
def test_lazy_entrypoint_preserves_stable_boundary_errors(error: Exception) -> None:
    class FailingHandler:
        def __call__(
            self,
            _event: dict[str, Any],
            _context: object | None = None,
        ) -> dict[str, Any]:
            raise error

    lazy = entrypoint._LazyExecutionRecoveryHandler(lambda: FailingHandler())

    with pytest.raises(type(error)) as raised:
        lazy({"source": "invalid"})

    assert str(raised.value) == str(error)


def test_environment_is_copied_and_release_verified(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, str]] = []
    monkeypatch.setenv("MR_LISTER_RELEASE_FINGERPRINT", "a" * 64)
    monkeypatch.setattr(
        entrypoint,
        "verify_phase6_packaged_release",
        lambda environment, *, component: calls.append(
            (component, str(environment["MR_LISTER_RELEASE_FINGERPRINT"]))
        ),
    )

    environment = entrypoint._environment()

    assert environment["MR_LISTER_RELEASE_FINGERPRINT"] == "a" * 64
    assert calls == [("lambda", "a" * 64)]


def test_exported_entrypoint_delegates_exact_event_and_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[dict[str, Any], object | None]] = []

    def handler(event: dict[str, Any], context: object | None = None) -> dict[str, Any]:
        calls.append((event, context))
        return {"alarm_signal_count": 0}

    monkeypatch.setattr(entrypoint, "_recovery", handler)
    event = {"source": "stuck-execution-sweeper"}
    context = object()

    assert entrypoint.execution_recovery_handler(event, context) == {"alarm_signal_count": 0}
    assert calls == [(event, context)]
