from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pytest

from mr_lister.cloud.phase6_operational_cleanup_composition import (
    TERMINAL_OPERATIONAL_CLEANUP_EVENT,
    Phase6OperationalCleanupConfigurationError,
    Phase6OperationalCleanupExecutionError,
    Phase6TerminalOperationalCleanupHandler,
    build_terminal_operational_cleanup_handler,
    load_operational_cleanup_configuration,
)
from mr_lister.cloud.phase6_operational_cleanup_entrypoint import (
    _LazyOperationalCleanupHandler,
)
from mr_lister.production.operational_cleanup import OperationalCleanupResult

REGION = "us-west-2"
ENVIRONMENT_NAME = "dev"
TABLE = "mr-lister-phase6-dev"


def _environment() -> dict[str, str]:
    return {
        "AWS_REGION": REGION,
        "MR_LISTER_ENVIRONMENT": ENVIRONMENT_NAME,
        "MR_LISTER_STATE_TABLE": TABLE,
    }


class ClosedDynamoClient:
    def scan(self, **kwargs: Any) -> dict[str, Any]:
        raise AssertionError(kwargs)

    def query(self, **kwargs: Any) -> dict[str, Any]:
        raise AssertionError(kwargs)

    def get_item(self, **kwargs: Any) -> dict[str, Any]:
        raise AssertionError(kwargs)

    def put_item(self, **kwargs: Any) -> dict[str, Any]:
        raise AssertionError(kwargs)

    def transact_write_items(self, **kwargs: Any) -> dict[str, Any]:
        raise AssertionError(kwargs)


class RecordingFactory:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.dynamodb = ClosedDynamoClient()

    def __call__(self, service_name: str, *, region_name: str) -> object:
        self.calls.append((service_name, region_name))
        if service_name == "dynamodb":
            return self.dynamodb
        raise AssertionError(service_name)


class RecordingSweeper:
    def __init__(self, result: OperationalCleanupResult | Exception) -> None:
        self.result = result
        self.calls = 0

    def sweep(self) -> OperationalCleanupResult:
        self.calls += 1
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def _result() -> OperationalCleanupResult:
    return OperationalCleanupResult(
        scan_pages=2,
        records_scanned=17,
        jobs_observed=3,
        approved_jobs_preserved=1,
        nonterminal_jobs_preserved=1,
        recent_terminal_jobs_preserved=0,
        authority_changes_preserved=0,
        terminal_jobs_completed=1,
        assignment_pages=2,
        records_examined_for_expiry=4,
        records_assigned_expiry=3,
        scan_complete=True,
    )


def test_configuration_is_exactly_region_environment_and_table_bound() -> None:
    configuration = load_operational_cleanup_configuration(
        {
            **_environment(),
            "MR_LISTER_PRINTIFY_SECRET_ARN": "must-be-ignored",
            "MR_LISTER_AGENTCORE_RUNTIME_ARN": "must-be-ignored",
            "MR_LISTER_TERMINAL_RETENTION_DAYS": "1",
        }
    )

    assert configuration.region == REGION
    assert configuration.environment_name == ENVIRONMENT_NAME
    assert configuration.state_table == TABLE
    assert not hasattr(configuration, "retention_days")


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("AWS_REGION", "US-WEST-2"),
        ("MR_LISTER_ENVIRONMENT", "Dev"),
        ("MR_LISTER_STATE_TABLE", "mr-lister-phase6-prod"),
    ],
)
def test_configuration_drift_fails_value_free(name: str, value: str) -> None:
    environment = _environment()
    environment[name] = value

    with pytest.raises(Phase6OperationalCleanupConfigurationError) as captured:
        load_operational_cleanup_configuration(environment)

    assert str(captured.value) == "Phase 6 operational cleanup configuration is invalid"
    assert value not in str(captured.value)
    assert captured.value.__cause__ is None


def test_composition_constructs_one_closed_dynamodb_client_without_calls() -> None:
    factory = RecordingFactory()

    handler = build_terminal_operational_cleanup_handler(
        _environment(),
        client_factory=factory,
    )

    assert isinstance(handler, Phase6TerminalOperationalCleanupHandler)
    assert factory.calls == [("dynamodb", REGION)]
    assert not hasattr(factory.dynamodb, "delete_item")
    assert not hasattr(factory.dynamodb, "batch_write_item")
    assert not hasattr(factory.dynamodb, "transact_get_items")
    assert not hasattr(factory.dynamodb, "execute_statement")


def test_missing_exact_client_capability_fails_before_sweep() -> None:
    class IncompleteFactory(RecordingFactory):
        def __call__(self, service_name: str, *, region_name: str) -> object:
            self.calls.append((service_name, region_name))
            return object()

    factory = IncompleteFactory()
    with pytest.raises(Phase6OperationalCleanupConfigurationError) as captured:
        build_terminal_operational_cleanup_handler(
            _environment(),
            client_factory=factory,
        )

    assert factory.calls == [("dynamodb", REGION)]
    assert str(captured.value) == "Phase 6 operational cleanup configuration is invalid"
    assert captured.value.__cause__ is None


@pytest.mark.parametrize(
    "event",
    [
        {},
        {"source": "terminal-operational-record-cleanup"},
        {**TERMINAL_OPERATIONAL_CLEANUP_EVENT, "extra": True},
        {**TERMINAL_OPERATIONAL_CLEANUP_EVENT, "contract_version": "2.0.0"},
        {**TERMINAL_OPERATIONAL_CLEANUP_EVENT, "source": "source-version-retention-sweeper"},
    ],
)
def test_handler_rejects_every_nonexact_schedule_envelope_without_sweeping(
    event: dict[str, Any],
) -> None:
    sweeper = RecordingSweeper(_result())
    handler = Phase6TerminalOperationalCleanupHandler(sweeper=sweeper)

    with pytest.raises(Phase6OperationalCleanupExecutionError) as captured:
        handler(event)

    assert sweeper.calls == 0
    assert str(captured.value) == "Phase 6 operational cleanup failed safely"
    assert captured.value.__cause__ is None


def test_handler_returns_only_sanitized_counter_contract() -> None:
    sweeper = RecordingSweeper(_result())
    response = Phase6TerminalOperationalCleanupHandler(sweeper=sweeper)(
        dict(TERMINAL_OPERATIONAL_CLEANUP_EVENT)
    )

    assert response == _result().model_dump(mode="json")
    assert sweeper.calls == 1
    serialized = repr(response).casefold()
    assert "owner_id" not in serialized
    assert "job_id" not in serialized
    assert "payload" not in serialized
    assert "expires_at" not in serialized


def test_handler_detaches_dependency_errors_and_private_values() -> None:
    handler = Phase6TerminalOperationalCleanupHandler(
        sweeper=RecordingSweeper(RuntimeError("private owner, terminal job, and record payload"))
    )

    with pytest.raises(Phase6OperationalCleanupExecutionError) as captured:
        handler(dict(TERMINAL_OPERATIONAL_CLEANUP_EVENT))

    assert str(captured.value) == "Phase 6 operational cleanup failed safely"
    assert "owner" not in str(captured.value)
    assert "payload" not in str(captured.value)
    assert captured.value.__cause__ is None


def test_lazy_entrypoint_builds_once_under_concurrency_and_retains_no_event() -> None:
    builds = 0
    delegate = Phase6TerminalOperationalCleanupHandler(sweeper=RecordingSweeper(_result()))

    def build() -> Phase6TerminalOperationalCleanupHandler:
        nonlocal builds
        builds += 1
        return delegate

    lazy = _LazyOperationalCleanupHandler(build)
    with ThreadPoolExecutor(max_workers=8) as executor:
        results = tuple(
            executor.map(
                lambda _: lazy(dict(TERMINAL_OPERATIONAL_CLEANUP_EVENT)),
                range(24),
            )
        )

    assert builds == 1
    assert results == (_result().model_dump(mode="json"),) * 24
    assert not any(
        slot for slot in lazy.__slots__ if "event" in slot or "owner" in slot or "job" in slot
    )


def test_lazy_builder_failure_is_generic_and_retryable() -> None:
    calls = 0

    def fail() -> Phase6TerminalOperationalCleanupHandler:
        nonlocal calls
        calls += 1
        raise RuntimeError("secret environment value")

    lazy = _LazyOperationalCleanupHandler(fail)
    for _ in range(2):
        with pytest.raises(Phase6OperationalCleanupExecutionError) as captured:
            lazy(dict(TERMINAL_OPERATIONAL_CLEANUP_EVENT))
        assert str(captured.value) == "Phase 6 operational cleanup failed safely"
        assert "secret" not in str(captured.value)
        assert captured.value.__cause__ is None
    assert calls == 2
