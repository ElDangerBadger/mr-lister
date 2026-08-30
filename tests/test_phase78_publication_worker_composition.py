"""Exact-disabled Phase 7.8 worker composition checks."""

from __future__ import annotations

import ast
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

import mr_lister.cloud.phase7_worker_composition as composition
from mr_lister.publication.application import Phase7RuntimeDisabledError
from mr_lister.publication.execution_dynamodb import DynamoDBPublicationExecutionStore
from mr_lister.publication.guard_verification import (
    DurablePublicationPreCallGuard,
    PublicationGuardSourceAuthority,
)
from mr_lister.publication.provider_coordinator import PublicationProviderCoordinator
from mr_lister.publication.provider_runtime import PublicationProviderRuntimeFactory
from tests.test_phase71_publication_store import OWNER_ID
from tests.test_phase72_publication_execution import Harness

ROOT = Path(__file__).resolve().parents[1]
PROFILE_PATH = (ROOT / "config/product_profiles/gildan_64000_swiftpod.json").resolve()
PROFILE_FINGERPRINT = "5de1257141cfdacb1731f68bb9113712957483b33d5b0f7115afdba86eb7476c"
REGION = "us-west-2"
NOW = datetime(2026, 8, 30, 12, 0, tzinfo=UTC)


def exact_environment() -> dict[str, object]:
    return {
        "AWS_REGION": REGION,
        "MR_LISTER_STATE_TABLE": "mr-lister-phase6-dev",
        "MR_LISTER_RELEASE_FINGERPRINT": "a" * 64,
        "MR_LISTER_COGNITO_ISSUER": (
            f"https://cognito-idp.{REGION}.amazonaws.com/{REGION}_Phase78Pool"
        ),
        "MR_LISTER_COGNITO_CLIENT_ID": "phase78client123",
        "MR_LISTER_COGNITO_SCOPE": "mr-lister-api/seller",
        "MR_LISTER_COGNITO_GROUP": "seller",
        "MR_LISTER_PRODUCT_PROFILE_ID": "gildan_64000_swiftpod",
        "MR_LISTER_PRODUCT_PROFILE_VERSION": "2",
        "MR_LISTER_PRODUCT_PROFILE_FINGERPRINT": PROFILE_FINGERPRINT,
        "MR_LISTER_PRODUCT_PROFILE_PATH": str(PROFILE_PATH),
        "MR_LISTER_PHASE7_SCAFFOLD_ONLY": "true",
        "MR_LISTER_PHASE7_QUERY_ENABLED": "false",
        "MR_LISTER_PHASE7_REQUEST_ENABLED": "false",
        "MR_LISTER_PHASE7_PUBLICATION_ENABLED": "false",
    }


class InertDynamoClient:
    def __init__(self) -> None:
        self.operations: list[str] = []

    def get_item(self, **_kwargs: object) -> object:
        self.operations.append("get_item")
        raise AssertionError("Worker composition must not read state")

    def query(self, **_kwargs: object) -> object:
        self.operations.append("query")
        raise AssertionError("Worker composition must not query state")

    def transact_write_items(self, **_kwargs: object) -> object:
        self.operations.append("transact_write_items")
        raise AssertionError("Worker composition must not write state")


class InertCredentials:
    def __init__(self) -> None:
        self.calls = 0

    def resolve_exact(self, **_kwargs: object) -> object:
        self.calls += 1
        raise AssertionError("Worker composition must not resolve a secret")


class InertTransport:
    def __init__(self) -> None:
        self.calls = 0

    def request(self, **_kwargs: object) -> object:
        self.calls += 1
        raise AssertionError("Worker composition must not call the provider")


def test_oracle_joins_real_worker_graph_without_state_secret_or_wire_io() -> None:
    dynamodb = InertDynamoClient()
    credentials = InertCredentials()
    transport = InertTransport()
    rejected: list[object] = []

    coordinator = composition.compose_publication_worker(
        exact_environment(),
        dynamodb=dynamodb,
        credentials=credentials,  # type: ignore[arg-type]
        transport=transport,  # type: ignore[arg-type]
        rejected_audit_writer=rejected.append,  # type: ignore[arg-type]
        clock=lambda: NOW,
    )

    assert isinstance(coordinator, PublicationProviderCoordinator)
    assert isinstance(coordinator._store, DynamoDBPublicationExecutionStore)
    assert coordinator._store._client is dynamodb
    assert coordinator._execution._store is coordinator._store
    assert isinstance(coordinator._pre_call_guard, DurablePublicationPreCallGuard)
    assert isinstance(
        coordinator._pre_call_guard._store,
        composition.PublicationGuardStoreAdapter,
    )
    assert coordinator._pre_call_guard._store._store is coordinator._store
    assert isinstance(coordinator._boundary_factory, PublicationProviderRuntimeFactory)
    assert dynamodb.operations == []
    assert credentials.calls == transport.calls == 0
    assert rejected == []


def test_disabled_wrapper_denies_before_builder_or_event_observation() -> None:
    builds = 0

    def build() -> PublicationProviderCoordinator:
        nonlocal builds
        builds += 1
        raise AssertionError("Disabled worker must not construct its graph")

    handler = composition.build_disabled_publication_worker_handler(
        exact_environment(),
        builder=build,
    )
    event: dict[str, Any] = {
        "owner_id": "private-owner",
        "aggregate_id": "private-aggregate",
    }

    with pytest.raises(Phase7RuntimeDisabledError) as captured:
        handler(event)

    assert str(captured.value) == "Phase 7 publication runtime is disabled"
    assert builds == 0
    assert not any(
        word in slot
        for slot in handler.__slots__
        for word in ("event", "request", "claim", "owner", "job", "aggregate")
    )


def test_guard_store_adapter_translates_the_request_graph_to_phase76_authority() -> None:
    harness = Harness()
    adapter = composition.PublicationGuardStoreAdapter(harness.store)  # type: ignore[arg-type]

    raw = harness.store.load_source_authority(OWNER_ID, harness.aggregate_id)
    guarded = adapter.load_source_authority(OWNER_ID, harness.aggregate_id)

    assert isinstance(guarded, PublicationGuardSourceAuthority)
    assert guarded.current_job == raw.current_job
    assert guarded.review == raw.review
    assert guarded.approval_decision == raw.approval_decision
    assert guarded.source == raw.source
    assert guarded.product_sync == raw.product_sync
    assert guarded.pricing_snapshot == raw.pricing_snapshot
    assert guarded.pricing_evidence == raw.pricing_evidence


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("MR_LISTER_PHASE7_SCAFFOLD_ONLY", "false"),
        ("MR_LISTER_PHASE7_REQUEST_ENABLED", "true"),
        ("MR_LISTER_PHASE7_PUBLICATION_ENABLED", "true"),
        ("MR_LISTER_RELEASE_FINGERPRINT", "0" * 64),
    ],
)
def test_enabled_or_drifting_configuration_fails_before_dependency_use(
    name: str,
    value: object,
) -> None:
    environment = exact_environment()
    environment[name] = value
    dynamodb = InertDynamoClient()
    credentials = InertCredentials()
    transport = InertTransport()

    with pytest.raises(Exception) as captured:
        composition.compose_publication_worker(
            environment,
            dynamodb=dynamodb,
            credentials=credentials,  # type: ignore[arg-type]
            transport=transport,  # type: ignore[arg-type]
            rejected_audit_writer=lambda _record: None,
            clock=lambda: NOW,
        )

    assert str(captured.value) == "Phase 7 read-only composition configuration is invalid"
    assert dynamodb.operations == []
    assert credentials.calls == transport.calls == 0


def test_worker_composition_has_no_sdk_or_runtime_registration_import() -> None:
    path = ROOT / "src/mr_lister/cloud/phase7_worker_composition.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    imports = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    } | {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module is not None
    }

    assert imports.isdisjoint(
        {
            "boto3",
            "botocore",
            "mr_lister.cloud.phase7_entrypoints",
            "mr_lister.production.provider_secrets",
            "mr_lister.workflow",
        }
    )
