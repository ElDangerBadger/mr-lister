from __future__ import annotations

import importlib.util
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest

from mr_lister.cloud import (
    phase6_entrypoints,
    phase6_execution_recovery_entrypoint,
    phase6_operational_cleanup_entrypoint,
)
from mr_lister.cloud.phase6_machine import Phase6MachineExecutionError

ROOT = Path(__file__).parents[1]
SHIM = ROOT / "infra/phase6/lambda/phase6_lambda.py"


class FakeHandler:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def __call__(self, event: dict[str, Any], context: object | None = None) -> dict[str, Any]:
        del context
        self.events.append(event)
        return {"ok": True}


def _load_shim(name: str = "phase6_lambda_entrypoint_test") -> Any:
    spec = importlib.util.spec_from_file_location(name, SHIM)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_lazy_handler_builds_once_across_concurrent_warm_invocations() -> None:
    builds = 0
    delegate = FakeHandler()

    def build() -> FakeHandler:
        nonlocal builds
        builds += 1
        return delegate

    lazy = phase6_entrypoints._LazyHandler(build, public_http=False)
    event = {"job_id": "job_1", "work_request_id": "work_1"}

    with ThreadPoolExecutor(max_workers=8) as executor:
        results = tuple(executor.map(lambda _: lazy(event), range(24)))

    assert builds == 1
    assert results == ({"ok": True},) * 24
    assert len(delegate.events) == 24


def test_lazy_handler_never_retains_request_identity() -> None:
    delegate = FakeHandler()
    lazy = phase6_entrypoints._LazyHandler(lambda: delegate, public_http=False)

    first = {"job_id": "job_a", "work_request_id": "work_a"}
    second = {"job_id": "job_b", "work_request_id": "work_b"}
    lazy(first)
    lazy(second)

    assert delegate.events == [first, second]
    assert not any(slot for slot in lazy.__slots__ if "event" in slot or "owner" in slot)


def test_public_composition_failure_is_generic_and_value_free() -> None:
    def fail() -> FakeHandler:
        raise RuntimeError("credential secret and table identity")

    lazy = phase6_entrypoints._LazyHandler(fail, public_http=True)
    response = lazy(
        {
            "routeKey": "GET /v1/jobs",
            "requestContext": {"requestId": "request-safe"},
        }
    )

    assert response["statusCode"] == 500
    assert response["headers"]["Cache-Control"] == "no-store"
    assert "credential" not in response["body"]
    assert "table" not in response["body"]
    assert json.loads(response["body"])["error"]["code"] == "INTERNAL_ERROR"


def test_machine_composition_failure_is_detached_and_value_free() -> None:
    def fail() -> FakeHandler:
        raise RuntimeError("secret machine dependency")

    lazy = phase6_entrypoints._LazyHandler(fail, public_http=False)

    with pytest.raises(Phase6MachineExecutionError) as captured:
        lazy({"job_id": "job_1", "work_request_id": "work_1"})

    assert "secret machine dependency" not in str(captured.value)
    assert captured.value.__cause__ is None


def test_direct_production_entrypoint_environment_is_release_verified(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, str]] = []
    monkeypatch.setenv("MR_LISTER_RELEASE_FINGERPRINT", "a" * 64)
    monkeypatch.setattr(
        phase6_entrypoints,
        "verify_phase6_packaged_release",
        lambda environment, *, component: calls.append(
            (component, str(environment["MR_LISTER_RELEASE_FINGERPRINT"]))
        ),
    )

    environment = phase6_entrypoints._environment()

    assert environment["MR_LISTER_RELEASE_FINGERPRINT"] == "a" * 64
    assert calls == [("lambda", "a" * 64)]


@pytest.mark.parametrize("marker", [None, "true", "TRUE", "False", "0", ""])
def test_lambda_shim_remains_fail_closed_for_every_nonexact_marker(
    monkeypatch: pytest.MonkeyPatch,
    marker: str | None,
) -> None:
    if marker is None:
        monkeypatch.delenv("MR_LISTER_PHASE6_SCAFFOLD_ONLY", raising=False)
    else:
        monkeypatch.setenv("MR_LISTER_PHASE6_SCAFFOLD_ONLY", marker)
    module = _load_shim(f"phase6_lambda_marker_{marker!r}")

    with pytest.raises(module.Phase6ScaffoldNotReady):
        module.preparation_dispatch_handler(
            {"job_id": "job_1", "work_request_id": "work_1"},
            None,
        )


def test_exact_false_marker_delegates_without_import_time_application_work(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MR_LISTER_PHASE6_SCAFFOLD_ONLY", "false")
    module = _load_shim("phase6_lambda_production_delegate")
    monkeypatch.setattr(module, "_require_release_authority", lambda: None)
    calls: list[tuple[dict[str, Any], object | None]] = []

    def handler(event: dict[str, Any], context: object | None = None) -> dict[str, Any]:
        calls.append((event, context))
        return {"delegated": True}

    monkeypatch.setattr(phase6_entrypoints, "preparation_dispatch_handler", handler)
    event = {"job_id": "job_1", "work_request_id": "work_1"}

    assert module.preparation_dispatch_handler(event, None) == {"delegated": True}
    assert calls == [(event, None)]


def test_exact_false_health_delegates_but_scaffold_health_stays_503(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    event = {"routeKey": "GET /health"}
    monkeypatch.setenv("MR_LISTER_PHASE6_SCAFFOLD_ONLY", "true")
    scaffold = _load_shim("phase6_lambda_scaffold_health")
    assert scaffold.review_query_api_handler(event, None)["statusCode"] == 503

    monkeypatch.setenv("MR_LISTER_PHASE6_SCAFFOLD_ONLY", "false")
    production = _load_shim("phase6_lambda_production_health")
    monkeypatch.setattr(production, "_require_release_authority", lambda: None)
    monkeypatch.setattr(
        phase6_entrypoints,
        "review_query_api_handler",
        lambda _event, _context=None: {"statusCode": 200, "body": '{"status":"ok"}'},
    )
    assert production.review_query_api_handler(event, None)["statusCode"] == 200


def test_exact_false_still_fails_closed_without_a_sealed_release(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MR_LISTER_PHASE6_SCAFFOLD_ONLY", "false")
    monkeypatch.setenv("MR_LISTER_RELEASE_FINGERPRINT", "a" * 64)
    module = _load_shim("phase6_lambda_unsealed_release")

    with pytest.raises(module.Phase6ScaffoldNotReady, match="release authority"):
        module.preparation_dispatch_handler(
            {"job_id": "job_1", "work_request_id": "work_1"},
            None,
        )


def test_terminal_cleanup_shim_is_scaffolded_and_release_verified_before_delegate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_shim("phase6_lambda_terminal_cleanup")
    event = {"contract_version": "1.0.0", "source": "terminal-operational-record-cleanup"}
    monkeypatch.setenv("MR_LISTER_PHASE6_SCAFFOLD_ONLY", "true")
    with pytest.raises(module.Phase6ScaffoldNotReady):
        module.terminal_operational_cleanup_handler(event, None)

    calls: list[dict[str, Any]] = []
    monkeypatch.setenv("MR_LISTER_PHASE6_SCAFFOLD_ONLY", "false")
    monkeypatch.setattr(module, "_require_release_authority", lambda: None)
    monkeypatch.setattr(
        phase6_operational_cleanup_entrypoint,
        "terminal_operational_cleanup_handler",
        lambda received, _context=None: calls.append(received) or {"delegated": True},
    )

    assert module.terminal_operational_cleanup_handler(event, None) == {"delegated": True}
    assert calls == [event]


def test_execution_recovery_shim_is_scaffolded_and_release_verified_before_delegate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_shim("phase6_lambda_execution_recovery")
    event = {"source": "stuck-execution-sweeper"}
    monkeypatch.setenv("MR_LISTER_PHASE6_SCAFFOLD_ONLY", "true")
    with pytest.raises(module.Phase6ScaffoldNotReady):
        module.stuck_execution_recovery_handler(event, None)

    calls: list[dict[str, Any]] = []
    monkeypatch.setenv("MR_LISTER_PHASE6_SCAFFOLD_ONLY", "false")
    monkeypatch.setattr(module, "_require_release_authority", lambda: None)
    monkeypatch.setattr(
        phase6_execution_recovery_entrypoint,
        "execution_recovery_handler",
        lambda received, _context=None: calls.append(received) or {"delegated": True},
    )

    assert module.stuck_execution_recovery_handler(event, None) == {"delegated": True}
    assert calls == [event]
