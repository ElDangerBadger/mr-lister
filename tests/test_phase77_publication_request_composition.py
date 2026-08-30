"""Exact-disabled Phase 7.7 publication-request composition checks."""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

import pytest

import mr_lister.cloud.phase7_request_composition as composition
from mr_lister.control.dynamodb import DynamoDBSellerControlStore
from mr_lister.publication.application import Phase7RuntimeDisabledError
from mr_lister.publication.dynamodb import DynamoDBPublicationStore
from mr_lister.publication.request_api import PublicationRequestApiAdapter
from mr_lister.publication.service import PublicationRequestService

ROOT = Path(__file__).resolve().parents[1]
PROFILE_PATH = (ROOT / "config/product_profiles/gildan_64000_swiftpod.json").resolve()
PROFILE_FINGERPRINT = "5de1257141cfdacb1731f68bb9113712957483b33d5b0f7115afdba86eb7476c"
REGION = "us-west-2"


def exact_environment() -> dict[str, object]:
    return {
        "AWS_REGION": REGION,
        "MR_LISTER_STATE_TABLE": "mr-lister-phase6-dev",
        "MR_LISTER_RELEASE_FINGERPRINT": "a" * 64,
        "MR_LISTER_COGNITO_ISSUER": (
            f"https://cognito-idp.{REGION}.amazonaws.com/{REGION}_Phase77Pool"
        ),
        "MR_LISTER_COGNITO_CLIENT_ID": "phase77client123",
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


class RequestDynamoClient:
    def __init__(self) -> None:
        self.operations: list[str] = []

    def get_item(self, **_kwargs: object) -> object:
        self.operations.append("get_item")
        raise AssertionError("Composition must not read DynamoDB")

    def transact_write_items(self, **_kwargs: object) -> object:
        self.operations.append("transact_write_items")
        raise AssertionError("Composition must not write DynamoDB")


class RecordingClientFactory:
    def __init__(self) -> None:
        self.client = RequestDynamoClient()
        self.calls: list[tuple[str, str]] = []

    def __call__(self, service_name: str, *, region_name: str) -> object:
        self.calls.append((service_name, region_name))
        return self.client


def test_offline_oracle_composes_one_request_only_dynamo_graph_without_io() -> None:
    factory = RecordingClientFactory()

    adapter = composition.compose_publication_request_adapter(
        exact_environment(),
        client_factory=factory,
    )

    assert isinstance(adapter, PublicationRequestApiAdapter)
    assert isinstance(adapter._approvals, DynamoDBSellerControlStore)
    assert isinstance(adapter._requests, PublicationRequestService)
    assert isinstance(adapter._requests.store, DynamoDBPublicationStore)
    assert adapter._approvals._client is factory.client
    assert adapter._requests.store._client is factory.client
    assert factory.calls == [("dynamodb", REGION)]
    assert factory.client.operations == []


def test_disabled_handler_refuses_before_factory_or_event_observation() -> None:
    factory = RecordingClientFactory()
    handler = composition.build_disabled_publication_request_handler(
        exact_environment(),
        client_factory=factory,
    )
    event: dict[str, Any] = {
        "routeKey": "POST /v1/jobs/private_job/publish",
        "body": "private seller material",
    }

    with pytest.raises(Phase7RuntimeDisabledError) as captured:
        handler(event)

    assert str(captured.value) == "Phase 7 publication runtime is disabled"
    assert factory.calls == []
    assert not any(
        word in slot
        for slot in handler.__slots__
        for word in ("event", "request", "claim", "owner", "job")
    )


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("MR_LISTER_PHASE7_SCAFFOLD_ONLY", "false"),
        ("MR_LISTER_PHASE7_QUERY_ENABLED", "true"),
        ("MR_LISTER_PHASE7_REQUEST_ENABLED", "true"),
        ("MR_LISTER_PHASE7_PUBLICATION_ENABLED", "true"),
        ("MR_LISTER_RELEASE_FINGERPRINT", "0" * 64),
    ],
)
def test_enabled_or_drifting_configuration_fails_before_client_creation(
    name: str,
    value: object,
) -> None:
    environment = exact_environment()
    environment[name] = value
    factory = RecordingClientFactory()

    with pytest.raises(Exception) as captured:
        composition.compose_publication_request_adapter(
            environment,
            client_factory=factory,
        )

    assert str(captured.value) == "Phase 7 read-only composition configuration is invalid"
    assert captured.value.__cause__ is None
    assert factory.calls == []


def test_request_composition_imports_no_provider_workflow_or_secret_capability() -> None:
    path = ROOT / "src/mr_lister/cloud/phase7_request_composition.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    imports = {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module is not None
    }
    forbidden = {
        "mr_lister.production",
        "mr_lister.publication.execution_service",
        "mr_lister.publication.provider_boundary",
        "mr_lister.publication.provider_coordinator",
        "mr_lister.publication.provider_credentials",
        "mr_lister.workflow",
    }

    assert not any(
        imported == capability or imported.startswith(f"{capability}.")
        for imported in imports
        for capability in forbidden
    )
