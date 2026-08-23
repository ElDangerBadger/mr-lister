from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pytest

from mr_lister.cloud.phase6_retention_composition import (
    SOURCE_VERSION_RETENTION_EVENT,
    Phase6RetentionConfigurationError,
    Phase6RetentionExecutionError,
    Phase6SourceVersionRetentionHandler,
    build_source_version_retention_handler,
    load_retention_configuration,
)
from mr_lister.cloud.phase6_retention_entrypoint import _LazyRetentionHandler
from mr_lister.production.retention import RetentionSweepResult

REGION = "us-west-2"
ENVIRONMENT_NAME = "dev"
ACCOUNT_ID = "123456789012"
TABLE = "mr-lister-phase6-dev"
BUCKET = "mr-lister-phase6-artifacts-dev-123456789012-us-west-2"


def _environment() -> dict[str, str]:
    return {
        "AWS_REGION": REGION,
        "MR_LISTER_ENVIRONMENT": ENVIRONMENT_NAME,
        "MR_LISTER_AWS_ACCOUNT_ID": ACCOUNT_ID,
        "MR_LISTER_STATE_TABLE": TABLE,
        "MR_LISTER_ARTIFACT_BUCKET": BUCKET,
    }


class ClosedS3Client:
    def list_object_versions(self, **kwargs: Any) -> dict[str, Any]:
        raise AssertionError(kwargs)

    def get_object_tagging(self, **kwargs: Any) -> dict[str, Any]:
        raise AssertionError(kwargs)

    def put_object_tagging(self, **kwargs: Any) -> dict[str, Any]:
        raise AssertionError(kwargs)


class ClosedDynamoClient:
    def transact_get_items(self, **kwargs: Any) -> dict[str, Any]:
        raise AssertionError(kwargs)

    def get_item(self, **kwargs: Any) -> dict[str, Any]:
        raise AssertionError(kwargs)

    def put_item(self, **kwargs: Any) -> dict[str, Any]:
        raise AssertionError(kwargs)


class RecordingFactory:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.s3 = ClosedS3Client()
        self.dynamodb = ClosedDynamoClient()

    def __call__(self, service_name: str, *, region_name: str) -> object:
        self.calls.append((service_name, region_name))
        if service_name == "s3":
            return self.s3
        if service_name == "dynamodb":
            return self.dynamodb
        raise AssertionError(service_name)


class RecordingSweeper:
    def __init__(self, result: RetentionSweepResult | Exception) -> None:
        self.result = result
        self.calls = 0

    def sweep(self) -> RetentionSweepResult:
        self.calls += 1
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def _result() -> RetentionSweepResult:
    return RetentionSweepResult(
        pages_scanned=1,
        versions_scanned=2,
        delete_markers_skipped=1,
        versions_reasserted_pinned=1,
        versions_released_to_staged=1,
        staged_versions_unchanged=0,
        scan_complete=True,
    )


def test_configuration_is_exactly_stack_account_region_and_resource_bound() -> None:
    configuration = load_retention_configuration(
        {
            **_environment(),
            "MR_LISTER_PRINTIFY_SECRET_ARN": "must-be-ignored",
            "MR_LISTER_AGENTCORE_RUNTIME_ARN": "must-be-ignored",
        }
    )

    assert configuration.region == REGION
    assert configuration.environment_name == ENVIRONMENT_NAME
    assert configuration.account_id == ACCOUNT_ID
    assert configuration.state_table == TABLE
    assert configuration.artifact_bucket == BUCKET


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("AWS_REGION", "US-WEST-2"),
        ("MR_LISTER_ENVIRONMENT", "Dev"),
        ("MR_LISTER_AWS_ACCOUNT_ID", "000000000000"),
        ("MR_LISTER_STATE_TABLE", "mr-lister-phase6-prod"),
        ("MR_LISTER_ARTIFACT_BUCKET", "mr-lister-phase6-artifacts-dev"),
    ],
)
def test_configuration_drift_fails_value_free(name: str, value: str) -> None:
    environment = _environment()
    environment[name] = value

    with pytest.raises(Phase6RetentionConfigurationError) as captured:
        load_retention_configuration(environment)

    assert str(captured.value) == "Phase 6 source retention configuration is invalid"
    assert value not in str(captured.value)
    assert captured.value.__cause__ is None


def test_composition_constructs_only_s3_and_dynamodb_clients_without_calls() -> None:
    factory = RecordingFactory()

    handler = build_source_version_retention_handler(
        _environment(),
        client_factory=factory,
    )

    assert isinstance(handler, Phase6SourceVersionRetentionHandler)
    assert factory.calls == [("s3", REGION), ("dynamodb", REGION)]
    assert not hasattr(factory.s3, "get_object")
    assert not hasattr(factory.s3, "delete_object")
    assert not hasattr(factory.dynamodb, "delete_item")
    assert not hasattr(factory.dynamodb, "scan")


def test_missing_exact_client_capability_fails_before_any_sweep() -> None:
    class IncompleteFactory(RecordingFactory):
        def __call__(self, service_name: str, *, region_name: str) -> object:
            if service_name == "s3":
                return object()
            return super().__call__(service_name, region_name=region_name)

    with pytest.raises(Phase6RetentionConfigurationError) as captured:
        build_source_version_retention_handler(
            _environment(),
            client_factory=IncompleteFactory(),
        )

    assert str(captured.value) == "Phase 6 source retention configuration is invalid"
    assert captured.value.__cause__ is None


@pytest.mark.parametrize(
    "event",
    [
        {},
        {"source": "source-version-retention-sweeper"},
        {**SOURCE_VERSION_RETENTION_EVENT, "extra": True},
        {**SOURCE_VERSION_RETENTION_EVENT, "contract_version": "2.0.0"},
        {**SOURCE_VERSION_RETENTION_EVENT, "source": "due-work-sweeper"},
    ],
)
def test_handler_rejects_every_nonexact_schedule_envelope_without_sweeping(
    event: dict[str, Any],
) -> None:
    sweeper = RecordingSweeper(_result())
    handler = Phase6SourceVersionRetentionHandler(sweeper=sweeper)

    with pytest.raises(Phase6RetentionExecutionError) as captured:
        handler(event)

    assert sweeper.calls == 0
    assert str(captured.value) == "Phase 6 source retention sweep failed safely"


def test_handler_returns_only_the_sanitized_retention_contract() -> None:
    sweeper = RecordingSweeper(_result())
    handler = Phase6SourceVersionRetentionHandler(sweeper=sweeper)

    response = handler(dict(SOURCE_VERSION_RETENTION_EVENT))

    assert response == _result().model_dump(mode="json")
    assert sweeper.calls == 1
    assert "owner" not in repr(response).casefold()
    assert "job" not in repr(response).casefold()
    assert "object" not in repr(response).casefold()


def test_handler_detaches_dependency_errors_and_values() -> None:
    handler = Phase6SourceVersionRetentionHandler(
        sweeper=RecordingSweeper(RuntimeError("private bucket owner and job identity"))
    )

    with pytest.raises(Phase6RetentionExecutionError) as captured:
        handler(dict(SOURCE_VERSION_RETENTION_EVENT))

    assert "bucket" not in str(captured.value)
    assert "identity" not in str(captured.value)
    assert captured.value.__cause__ is None


def test_lazy_entrypoint_builds_once_and_retains_no_event_identity() -> None:
    builds = 0
    delegate = Phase6SourceVersionRetentionHandler(sweeper=RecordingSweeper(_result()))

    def build() -> Phase6SourceVersionRetentionHandler:
        nonlocal builds
        builds += 1
        return delegate

    lazy = _LazyRetentionHandler(build)
    with ThreadPoolExecutor(max_workers=8) as executor:
        results = tuple(
            executor.map(lambda _: lazy(dict(SOURCE_VERSION_RETENTION_EVENT)), range(24))
        )

    assert builds == 1
    assert results == (_result().model_dump(mode="json"),) * 24
    assert not any(slot for slot in lazy.__slots__ if "event" in slot or "owner" in slot)
