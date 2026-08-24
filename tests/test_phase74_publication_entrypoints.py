"""Exact-disabled lazy Phase 7.4 publication query entrypoint tests."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import pytest

from mr_lister.cloud import phase7_entrypoints
from mr_lister.cloud.phase7_composition import build_disabled_publication_query_handler
from mr_lister.publication.application import Phase7RuntimeDisabledError
from tests.test_phase74_publication_composition import (
    RecordingClientFactory,
    exact_environment,
)


def test_lazy_entrypoint_builds_disabled_configuration_once_under_concurrency() -> None:
    builds = 0
    factory = RecordingClientFactory()

    def build():  # type: ignore[no-untyped-def]
        nonlocal builds
        builds += 1
        return build_disabled_publication_query_handler(
            exact_environment(),
            client_factory=factory,
        )

    entrypoint = phase7_entrypoints._LazyDisabledQueryEntrypoint(build)

    def invoke(_: int) -> str:
        with pytest.raises(Phase7RuntimeDisabledError) as captured:
            entrypoint({"seller_material": "must_not_be_retained"})
        return str(captured.value)

    with ThreadPoolExecutor(max_workers=8) as executor:
        messages = tuple(executor.map(invoke, range(24)))

    assert builds == 1
    assert messages == ("Phase 7 publication runtime is disabled",) * 24
    assert factory.calls == []


def test_entrypoint_detaches_invalid_configuration_and_dependency_values() -> None:
    def fail():  # type: ignore[no-untyped-def]
        raise RuntimeError("secret table owner provider material")

    entrypoint = phase7_entrypoints._LazyDisabledQueryEntrypoint(fail)

    with pytest.raises(Phase7RuntimeDisabledError) as captured:
        entrypoint({"job_id": "private-job"})

    assert str(captured.value) == "Phase 7 publication runtime is disabled"
    assert captured.value.__cause__ is None
    assert "secret" not in str(captured.value)
    assert "private-job" not in str(captured.value)


def test_entrypoint_retains_no_request_or_identity_slot() -> None:
    entrypoint = phase7_entrypoints._LazyDisabledQueryEntrypoint(
        lambda: build_disabled_publication_query_handler(exact_environment())
    )

    assert not any(
        word in slot
        for slot in entrypoint.__slots__
        for word in ("event", "request", "claim", "owner", "job")
    )


def test_module_entrypoint_uses_fresh_environment_and_remains_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    def environment():
        nonlocal calls
        calls += 1
        return exact_environment()

    monkeypatch.setattr(phase7_entrypoints, "_environment", environment)
    monkeypatch.setattr(
        phase7_entrypoints,
        "_publication_query",
        phase7_entrypoints._LazyDisabledQueryEntrypoint(
            lambda: build_disabled_publication_query_handler(phase7_entrypoints._environment())
        ),
    )

    for _ in range(2):
        with pytest.raises(Phase7RuntimeDisabledError):
            phase7_entrypoints.publication_query_api_handler(
                {"routeKey": "GET /v1/jobs/{job_id}/publication"}
            )

    assert calls == 1


def test_public_entrypoint_surface_has_no_request_or_provider_worker() -> None:
    assert phase7_entrypoints.__all__ == ["publication_query_api_handler"]
    assert not hasattr(phase7_entrypoints, "publication_request_api_handler")
    assert not hasattr(phase7_entrypoints, "publication_provider_handler")
    assert not hasattr(phase7_entrypoints, "publication_coordinator_handler")
