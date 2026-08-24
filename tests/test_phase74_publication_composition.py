"""Disabled Phase 7.4 read-only composition and containment gates."""

from __future__ import annotations

import ast
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

import mr_lister.cloud.phase7_composition as composition
from mr_lister.control.dynamodb import DynamoDBSellerControlStore
from mr_lister.publication.application import (
    DynamoPublicationProjectionStore,
    Phase7RuntimeDisabledError,
)
from mr_lister.publication.execution_dynamodb import DynamoDBPublicationExecutionStore
from mr_lister.publication.projection import SellerPublicationProjectionService
from mr_lister.publication.query_api import PublicationQueryApiAdapter

ROOT = Path(__file__).resolve().parents[1]
PROFILE_PATH = (ROOT / "config/product_profiles/gildan_64000_swiftpod.json").resolve()
PROFILE_FINGERPRINT = "5de1257141cfdacb1731f68bb9113712957483b33d5b0f7115afdba86eb7476c"
REGION = "us-west-2"
ISSUER = f"https://cognito-idp.{REGION}.amazonaws.com/{REGION}_Phase74Pool"


def exact_environment() -> dict[str, object]:
    return {
        "AWS_REGION": REGION,
        "MR_LISTER_STATE_TABLE": "mr-lister-phase6-dev",
        "MR_LISTER_RELEASE_FINGERPRINT": "a" * 64,
        "MR_LISTER_COGNITO_ISSUER": ISSUER,
        "MR_LISTER_COGNITO_CLIENT_ID": "phase74client123",
        "MR_LISTER_COGNITO_SCOPE": composition.SELLER_SCOPE,
        "MR_LISTER_COGNITO_GROUP": composition.SELLER_GROUP,
        "MR_LISTER_PRODUCT_PROFILE_ID": "gildan_64000_swiftpod",
        "MR_LISTER_PRODUCT_PROFILE_VERSION": "2",
        "MR_LISTER_PRODUCT_PROFILE_FINGERPRINT": PROFILE_FINGERPRINT,
        "MR_LISTER_PRODUCT_PROFILE_PATH": str(PROFILE_PATH),
        "MR_LISTER_PHASE7_SCAFFOLD_ONLY": "true",
        "MR_LISTER_PHASE7_QUERY_ENABLED": "false",
        "MR_LISTER_PHASE7_REQUEST_ENABLED": "false",
        "MR_LISTER_PHASE7_PUBLICATION_ENABLED": "false",
    }


class ReadOnlyDynamoClient:
    def __init__(self) -> None:
        self.operations: list[str] = []

    def get_item(self, **_kwargs: object) -> object:
        self.operations.append("get_item")
        raise AssertionError("No DynamoDB read was expected during composition")

    def query(self, **_kwargs: object) -> object:
        self.operations.append("query")
        raise AssertionError("No DynamoDB read was expected during composition")


class RecordingClientFactory:
    def __init__(self) -> None:
        self.client = ReadOnlyDynamoClient()
        self.calls: list[tuple[str, str]] = []

    def __call__(self, service_name: str, *, region_name: str) -> object:
        self.calls.append((service_name, region_name))
        assert service_name == "dynamodb"
        return self.client


def test_configuration_binds_checked_draft_safe_profile_to_disabled_eligibility() -> None:
    configured = composition.load_phase7_read_configuration(exact_environment())

    assert configured.profile.exact.profile.publish_enabled is False
    assert configured.profile.exact.fingerprint == PROFILE_FINGERPRINT
    assert configured.eligibility.profile_fingerprint == PROFILE_FINGERPRINT
    assert configured.eligibility.release_manifest_fingerprint == "a" * 64
    assert configured.eligibility.publication_eligible is True
    assert configured.eligibility.seller_request_enabled is False
    assert configured.eligibility.provider_mutation_enabled is False
    assert configured.activation.request_enabled is False
    assert configured.activation.publication_enabled is False
    assert configured.activation.query_enabled is False
    assert configured.activation.scaffold_only is True


def test_offline_query_composition_creates_one_get_query_only_dynamo_dependency() -> None:
    factory = RecordingClientFactory()
    configured = composition.load_phase7_read_configuration(exact_environment())

    adapter = composition.compose_publication_query_adapter(
        configured,
        client_factory=factory,
    )

    assert isinstance(adapter, PublicationQueryApiAdapter)
    assert isinstance(adapter._projections, SellerPublicationProjectionService)
    projection_store = adapter._projections._store
    assert isinstance(projection_store, DynamoPublicationProjectionStore)
    assert isinstance(projection_store.jobs, DynamoDBSellerControlStore)
    assert isinstance(projection_store.execution, DynamoDBPublicationExecutionStore)
    assert projection_store.jobs._client is factory.client
    assert projection_store.execution._client is factory.client
    assert factory.calls == [("dynamodb", REGION)]
    assert not factory.client.operations
    assert not hasattr(factory.client, "transact_write_items")
    assert not hasattr(factory.client, "put_item")


@pytest.mark.parametrize("forgery", ["table", "eligibility", "activation", "profile"])
def test_offline_composition_deep_reparses_configuration_before_client_creation(
    forgery: str,
) -> None:
    configured = composition.load_phase7_read_configuration(exact_environment())
    if forgery == "table":
        configured = replace(configured, state_table="mr-lister-phase6-prod")
    elif forgery == "eligibility":
        configured = replace(
            configured,
            eligibility=configured.eligibility.model_copy(
                update={"provider_mutation_enabled": True}
            ),
        )
    elif forgery == "activation":
        configured = replace(
            configured,
            activation=configured.activation.model_copy(update={"query_enabled": True}),
        )
    else:
        configured = replace(
            configured,
            profile=replace(
                configured.profile,
                exact=replace(configured.profile.exact, fingerprint="b" * 64),
            ),
        )
    factory = RecordingClientFactory()

    with pytest.raises(composition.Phase7ReadConfigurationError) as captured:
        composition.compose_publication_query_adapter(
            configured,
            client_factory=factory,
        )

    assert str(captured.value) == "Phase 7 read-only composition configuration is invalid"
    assert captured.value.__cause__ is None
    assert factory.calls == []


def test_disabled_handler_refuses_before_client_construction_or_event_observation() -> None:
    factory = RecordingClientFactory()
    handler = composition.build_disabled_publication_query_handler(
        exact_environment(),
        client_factory=factory,
    )
    event: dict[str, Any] = {
        "routeKey": "GET /v1/jobs/{job_id}/publication",
        "pathParameters": {"job_id": "must_not_be_retained"},
    }

    with pytest.raises(Phase7RuntimeDisabledError) as captured:
        handler(event)

    assert str(captured.value) == "Phase 7 publication runtime is disabled"
    assert factory.calls == []
    assert not any(
        "event" in slot or "owner" in slot or "job" in slot for slot in handler.__slots__
    )


@pytest.mark.parametrize(
    "missing",
    sorted(exact_environment()),
)
def test_every_missing_configuration_value_fails_with_one_value_free_error(
    missing: str,
) -> None:
    environment = exact_environment()
    environment.pop(missing)

    with pytest.raises(composition.Phase7ReadConfigurationError) as captured:
        composition.load_phase7_read_configuration(environment)

    assert str(captured.value) == "Phase 7 read-only composition configuration is invalid"
    assert missing not in str(captured.value)
    assert captured.value.__cause__ is None


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("MR_LISTER_PHASE7_SCAFFOLD_ONLY", "false"),
        ("MR_LISTER_PHASE7_SCAFFOLD_ONLY", "TRUE"),
        ("MR_LISTER_PHASE7_QUERY_ENABLED", "true"),
        ("MR_LISTER_PHASE7_QUERY_ENABLED", False),
        ("MR_LISTER_PHASE7_REQUEST_ENABLED", "true"),
        ("MR_LISTER_PHASE7_PUBLICATION_ENABLED", "true"),
        ("MR_LISTER_PRODUCT_PROFILE_FINGERPRINT", "b" * 64),
        ("MR_LISTER_RELEASE_FINGERPRINT", "0" * 64),
    ],
)
def test_enabled_or_drifting_configuration_fails_closed(name: str, value: object) -> None:
    environment = exact_environment()
    environment[name] = value

    with pytest.raises(composition.Phase7ReadConfigurationError) as captured:
        composition.load_phase7_read_configuration(environment)

    assert str(captured.value) == "Phase 7 read-only composition configuration is invalid"
    assert captured.value.__cause__ is None


def test_phase7_read_composition_has_no_provider_or_mutation_import() -> None:
    path = ROOT / "src/mr_lister/cloud/phase7_composition.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    imports = {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module is not None
    }
    imported_names = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in node.names
    }

    assert not any(
        module == forbidden or module.startswith(f"{forbidden}.")
        for module in imports
        for forbidden in {
            "mr_lister.production",
            "mr_lister.publication.provider_boundary",
            "mr_lister.publication.provider_coordinator",
            "mr_lister.publication.service",
            "mr_lister.publication.execution_service",
            "mr_lister.workflow",
        }
    )
    assert imported_names.isdisjoint(
        {
            "PublicationProviderCoordinator",
            "PublicationRequestService",
            "StagedPublicationProviderBoundary",
        }
    )
