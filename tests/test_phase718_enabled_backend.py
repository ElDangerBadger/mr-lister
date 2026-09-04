"""Focused contract, configuration, projection, and composition gates for P7.18."""

from __future__ import annotations

import ast
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from mr_lister.cloud import phase718_composition as composition
from mr_lister.cloud import phase718_entrypoints
from mr_lister.cloud.phase718_configuration import (
    Phase718ConfigurationError,
    load_phase718_enabled_configuration,
)
from mr_lister.control.errors import NotFoundError
from mr_lister.publication.contract import PublicationState, phase7_publication_contract_bytes
from mr_lister.publication.enabled_api import (
    Phase718PublicationQueryApiAdapter,
    Phase718PublicationRequestApiAdapter,
)
from mr_lister.publication.enabled_contract import (
    PHASE718_ENABLED_CONTRACT_VERSION,
    phase718_enabled_publication_contract,
    phase718_enabled_publication_contract_bytes,
    phase718_enabled_publication_contract_digest,
)
from mr_lister.publication.enabled_projection import (
    Phase718SellerPublicationProjection,
    Phase718SellerPublicationProjectionService,
)
from mr_lister.publication.provider_coordinator import (
    PublicationProviderCoordinatorAction,
    PublicationProviderCoordinatorResult,
)
from mr_lister.publication.query_api import PUBLICATION_QUERY_ROUTE
from tests.test_phase71_publication_store import OWNER_ID
from tests.test_phase72_publication_execution import Harness
from tests.test_phase73_publication_projection import ProjectionStore, _authority

ROOT = Path(__file__).resolve().parents[1]
PROFILE_PATH = (ROOT / "config/product_profiles/gildan_64000_swiftpod.json").resolve()
PROFILE_FINGERPRINT = "5de1257141cfdacb1731f68bb9113712957483b33d5b0f7115afdba86eb7476c"


def exact_environment() -> dict[str, object]:
    region = "us-west-2"
    return {
        "AWS_REGION": region,
        "MR_LISTER_STATE_TABLE": "mr-lister-phase6-dev",
        "MR_LISTER_RELEASE_FINGERPRINT": "a" * 64,
        "MR_LISTER_COGNITO_ISSUER": (
            f"https://cognito-idp.{region}.amazonaws.com/{region}_Phase718Pool"
        ),
        "MR_LISTER_COGNITO_CLIENT_ID": "phase718client123",
        "MR_LISTER_COGNITO_SCOPE": "mr-lister-api/seller",
        "MR_LISTER_COGNITO_GROUP": "seller",
        "MR_LISTER_PRODUCT_PROFILE_ID": "gildan_64000_swiftpod",
        "MR_LISTER_PRODUCT_PROFILE_VERSION": "2",
        "MR_LISTER_PRODUCT_PROFILE_FINGERPRINT": PROFILE_FINGERPRINT,
        "MR_LISTER_PRODUCT_PROFILE_PATH": str(PROFILE_PATH),
        "MR_LISTER_PHASE7_CONTRACT_VERSION": "7.1.0",
        "MR_LISTER_PHASE7_CONTRACT_FINGERPRINT": (phase718_enabled_publication_contract_digest()),
        "MR_LISTER_PHASE7_ENABLED_RELEASE_FINGERPRINT": "b" * 64,
        "MR_LISTER_PHASE7_CANARY_EVIDENCE_FINGERPRINT": "c" * 64,
        "MR_LISTER_PHASE7_ENABLEMENT_EVIDENCE_FINGERPRINT": "d" * 64,
        "MR_LISTER_PHASE7_ACTIVATION_MODE": "GENERAL_AVAILABILITY",
        "MR_LISTER_PHASE7_SCAFFOLD_ONLY": "false",
        "MR_LISTER_PHASE7_QUERY_ENABLED": "true",
        "MR_LISTER_PHASE7_REQUEST_ENABLED": "true",
        "MR_LISTER_PHASE7_PUBLICATION_ENABLED": "true",
        "MR_LISTER_PHASE7_WORKER_ENABLED": "true",
        "MR_LISTER_PHASE7_DISPATCHER_ENABLED": "true",
        "MR_LISTER_PHASE7_RECOVERY_ENABLED": "true",
        "MR_LISTER_PHASE7_RETENTION_ENABLED": "true",
    }


def test_enabled_contract_is_new_deterministic_authority_without_rewriting_701() -> None:
    enabled_path = ROOT / "contracts/publication/phase7.1.0.json"
    disabled_path = ROOT / "contracts/publication/phase7.0.1.json"

    assert PHASE718_ENABLED_CONTRACT_VERSION == "7.1.0"
    assert enabled_path.read_bytes() == phase718_enabled_publication_contract_bytes()
    assert disabled_path.read_bytes() == phase7_publication_contract_bytes()
    enabled = phase718_enabled_publication_contract()
    assert enabled.publication_enabled is True
    assert enabled.phase6_runtime_unchanged is True
    assert enabled.maximum_root_attempts_per_job == enabled.maximum_publish_posts_per_job == 1
    assert [(route.method, route.route) for route in enabled.seller_routes] == [
        ("GET", "/v1/jobs/{job_id}/publication"),
        ("POST", "/v1/jobs/{job_id}/publish"),
    ]


def test_enabled_contract_rejects_any_broader_capability() -> None:
    payload = json.loads(phase718_enabled_publication_contract_bytes())
    payload["maximum_publish_posts_per_job"] = 2
    with pytest.raises(ValidationError):
        type(phase718_enabled_publication_contract()).model_validate(payload)

    payload = json.loads(phase718_enabled_publication_contract_bytes())
    payload["forbidden_capabilities_preserved"].remove("unpublish")
    with pytest.raises(ValidationError):
        type(phase718_enabled_publication_contract()).model_validate(payload)


def test_enabled_configuration_requires_all_evidence_and_exact_true_tuple() -> None:
    configured = load_phase718_enabled_configuration(exact_environment())

    assert configured.application_release_fingerprint == "a" * 64
    assert configured.enabled_release_fingerprint == "b" * 64
    assert configured.canary_evidence_fingerprint == "c" * 64
    assert configured.enablement_evidence_fingerprint == "d" * 64
    assert configured.activation.publication_enabled is True
    assert configured.activation.scaffold_only is False
    assert configured.foundation.activation.publication_enabled is False

    for name, value in (
        ("MR_LISTER_PHASE7_QUERY_ENABLED", "false"),
        ("MR_LISTER_PHASE7_PUBLICATION_ENABLED", "false"),
        ("MR_LISTER_PHASE7_CANARY_EVIDENCE_FINGERPRINT", "0" * 64),
        ("MR_LISTER_PHASE7_CONTRACT_FINGERPRINT", "e" * 64),
    ):
        environment = exact_environment()
        environment[name] = value
        with pytest.raises(Phase718ConfigurationError) as captured:
            load_phase718_enabled_configuration(environment)
        assert str(captured.value) == "Phase 7.18 enabled configuration is invalid"
        assert captured.value.__cause__ is None


def test_enabled_projection_is_requestable_only_for_pristine_approved_job() -> None:
    harness = Harness()
    approved = harness.transaction.authority.current_job
    store = ProjectionStore(approved)

    projection = Phase718SellerPublicationProjectionService(store).get(
        owner_id=OWNER_ID,
        job_id=approved.job_id,
    )

    assert projection.contract_version == "7.1.0"
    assert projection.publication_enabled is True
    assert projection.request_enabled is True
    assert projection.request_disabled_reason is None
    assert projection.state == "not_requested"
    assert store.job_reads == 1
    assert store.publication_reads == 0

    legacy = approved.model_copy(update={"approval_decision_id": None})
    unavailable = Phase718SellerPublicationProjectionService(ProjectionStore(legacy)).get(
        owner_id=OWNER_ID,
        job_id=legacy.job_id,
    )
    assert unavailable.request_enabled is False
    assert unavailable.request_disabled_reason == "PUBLICATION_NOT_ELIGIBLE"


def test_enabled_projection_disables_repeat_request_and_remains_owner_first() -> None:
    harness = Harness()
    job = harness.store.jobs[harness.authority.snapshot.job_id]
    store = ProjectionStore(job, _authority(harness, use_stored_rows=False))
    projection = Phase718SellerPublicationProjectionService(store).get(
        owner_id=OWNER_ID,
        job_id=job.job_id,
    )

    assert projection.request_enabled is False
    assert projection.request_disabled_reason == "PUBLICATION_ALREADY_REQUESTED"
    assert store.job_reads == store.publication_reads == 1

    with pytest.raises(NotFoundError):
        Phase718SellerPublicationProjectionService(store).get(
            owner_id="e" * 64,
            job_id=job.job_id,
        )
    assert store.publication_reads == 1


class _Authenticator:
    def authenticate(self, event: Mapping[str, Any]) -> str:
        del event
        return OWNER_ID


class _ProjectionPort:
    def __init__(self, projection: Phase718SellerPublicationProjection) -> None:
        self.projection = projection

    def get(self, *, owner_id: str, job_id: str) -> Phase718SellerPublicationProjection:
        assert owner_id == OWNER_ID
        assert job_id == self.projection.job_id
        return self.projection


def _query_event(job_id: str) -> dict[str, Any]:
    path = f"/v1/jobs/{job_id}/publication"
    return {
        "version": "2.0",
        "routeKey": PUBLICATION_QUERY_ROUTE,
        "rawPath": path,
        "rawQueryString": "",
        "pathParameters": {"job_id": job_id},
        "requestContext": {
            "requestId": "phase718-request",
            "http": {"method": "GET", "path": path},
        },
        "isBase64Encoded": False,
    }


def test_enabled_query_and_request_edges_expose_710_only() -> None:
    harness = Harness()
    approved = harness.transaction.authority.current_job
    projection = Phase718SellerPublicationProjectionService(ProjectionStore(approved)).get(
        owner_id=OWNER_ID,
        job_id=approved.job_id,
    )
    response = Phase718PublicationQueryApiAdapter(
        authenticator=_Authenticator(),
        projections=_ProjectionPort(projection),
    )(_query_event(approved.job_id))
    assert response["statusCode"] == 200
    assert json.loads(response["body"])["contract_version"] == "7.1.0"

    class Delegate:
        def __call__(self, event: object, context: object) -> dict[str, Any]:
            del event, context
            return {
                "statusCode": 202,
                "headers": {"x-request-id": "phase718"},
                "body": '{"contract_version":"7.0.1","job_id":"job"}',
                "isBase64Encoded": False,
            }

    rewritten = Phase718PublicationRequestApiAdapter(Delegate())({}, None)  # type: ignore[arg-type]
    assert rewritten["statusCode"] == 202
    assert json.loads(rewritten["body"]) == {
        "contract_version": "7.1.0",
        "job_id": "job",
    }


def test_enabled_api_has_only_the_exact_edge_adapter_dependencies() -> None:
    path = ROOT / "src/mr_lister/publication/enabled_api.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    imported = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    } | {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module is not None
    }
    assert imported == {
        "__future__",
        "collections.abc",
        "json",
        "mr_lister.cloud.auth",
        "mr_lister.control.errors",
        "mr_lister.publication",
        "mr_lister.publication.enabled_projection",
        "mr_lister.publication.projection",
        "mr_lister.publication.query_api",
        "mr_lister.publication.request_api",
        "pydantic",
        "re",
        "typing",
    }

    called = {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    } | {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    assert called.isdisjoint(
        {
            "client",
            "create_unpublished_product",
            "describe_execution",
            "get_secret_value",
            "publish",
            "resource",
            "start_execution",
        }
    )


class _Dynamo:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def get_item(self, **kwargs: object) -> object:
        self.calls.append("get_item")
        raise AssertionError(kwargs)

    def query(self, **kwargs: object) -> object:
        self.calls.append("query")
        raise AssertionError(kwargs)

    def transact_write_items(self, **kwargs: object) -> object:
        self.calls.append("transact_write_items")
        raise AssertionError(kwargs)


class _Factory:
    def __init__(self) -> None:
        self.client = _Dynamo()
        self.calls: list[tuple[str, str]] = []

    def __call__(self, service_name: str, *, region_name: str) -> object:
        self.calls.append((service_name, region_name))
        return self.client


def test_enabled_query_and_request_composition_construct_no_io() -> None:
    configured = load_phase718_enabled_configuration(exact_environment())
    query_factory = _Factory()
    request_factory = _Factory()

    query = composition.compose_phase718_query_handler(
        configured,
        client_factory=query_factory,
    )
    request = composition.compose_phase718_request_handler(
        configured,
        client_factory=request_factory,
    )

    assert isinstance(query, Phase718PublicationQueryApiAdapter)
    assert isinstance(request, Phase718PublicationRequestApiAdapter)
    assert query_factory.calls == request_factory.calls == [("dynamodb", "us-west-2")]
    assert query_factory.client.calls == request_factory.client.calls == []


class _Credentials:
    def __init__(self) -> None:
        self.calls = 0

    def resolve_exact(self, **kwargs: object) -> object:
        self.calls += 1
        raise AssertionError(kwargs)


class _Transport:
    def __init__(self) -> None:
        self.calls = 0

    def request(self, **kwargs: object) -> object:
        self.calls += 1
        raise AssertionError(kwargs)


def test_enabled_worker_composition_reuses_one_shot_graph_without_io() -> None:
    dynamodb = _Dynamo()
    credentials = _Credentials()
    transport = _Transport()
    rejected: list[object] = []

    handler = composition.build_phase718_worker_handler(
        exact_environment(),
        dynamodb=dynamodb,
        credentials=credentials,  # type: ignore[arg-type]
        transport=transport,  # type: ignore[arg-type]
        rejected_audit_writer=rejected.append,  # type: ignore[arg-type]
    )

    assert isinstance(handler, composition._Phase718PublicationWorkerHandler)
    assert handler._coordinator._store._client is dynamodb
    assert handler._coordinator._execution._store is handler._coordinator._store
    assert dynamodb.calls == []
    assert credentials.calls == transport.calls == 0
    assert rejected == []


class _Coordinator:
    def advance(self, *, owner_id: str, aggregate_id: str) -> PublicationProviderCoordinatorResult:
        assert owner_id == OWNER_ID
        assert aggregate_id == "aggregate_phase718"
        return PublicationProviderCoordinatorResult(
            action=PublicationProviderCoordinatorAction.RECORDED_PREFLIGHT,
            aggregate_state=PublicationState.PUBLICATION_REQUESTED,
        )


def test_enabled_worker_entry_boundary_accepts_only_minimal_identity() -> None:
    handler = composition._Phase718PublicationWorkerHandler(_Coordinator())  # type: ignore[arg-type]

    assert handler({"owner_id": OWNER_ID, "aggregate_id": "aggregate_phase718"}) == {
        "contract_version": "7.1.0",
        "action": "recorded_preflight",
        "aggregate_state": "publication_requested",
    }
    for invalid in (
        {"owner_id": OWNER_ID},
        {"owner_id": OWNER_ID, "aggregate_id": "bad/id"},
        {"owner_id": OWNER_ID, "aggregate_id": "aggregate_phase718", "extra": True},
    ):
        with pytest.raises(RuntimeError) as captured:
            handler(invalid)
        assert str(captured.value) == "Phase 7.18 publication step failed safely"
        assert captured.value.__cause__ is None


def test_enabled_entrypoint_module_is_standard_library_only_until_release_verification() -> None:
    path = ROOT / "src/mr_lister/cloud/phase718_entrypoints.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    top_level_imports = {
        alias.name for node in tree.body if isinstance(node, ast.Import) for alias in node.names
    } | {
        node.module
        for node in tree.body
        if isinstance(node, ast.ImportFrom) and node.module is not None
    }
    assert top_level_imports == {
        "__future__",
        "collections.abc",
        "json",
        "logging",
        "os",
        "sys",
        "threading",
        "typing",
    }


def test_release_verification_failure_precedes_configuration_and_client_creation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client_calls = 0

    def fail(_environment: object, *, expected_entrypoint: str) -> None:
        assert expected_entrypoint.endswith(".publication_query_handler")
        raise RuntimeError("private release detail")

    def client(*_args: object, **_kwargs: object) -> object:
        nonlocal client_calls
        client_calls += 1
        raise AssertionError

    monkeypatch.setattr(phase718_entrypoints, "_verify_release", fail)
    monkeypatch.setattr(phase718_entrypoints, "_dynamodb_factory", client)
    with pytest.raises(RuntimeError, match="private release detail"):
        phase718_entrypoints._build_query()
    assert client_calls == 0


def test_lazy_enabled_entrypoint_collapses_startup_failure_without_observing_event() -> None:
    builds = 0

    def fail() -> composition.Phase718Handler:
        nonlocal builds
        builds += 1
        raise RuntimeError("private startup material")

    entrypoint = phase718_entrypoints._LazyEnabledEntrypoint(fail)
    with pytest.raises(phase718_entrypoints.Phase718EntrypointError) as captured:
        entrypoint({"private": "seller material"})

    assert builds == 1
    assert str(captured.value) == "Phase 7.18 runtime is unavailable"
    assert captured.value.__cause__ is None


def test_enabled_operations_router_is_exact_and_versions_public_counters() -> None:
    calls: list[str] = []

    class Queue:
        def __call__(self, event: object, context: object) -> dict[str, Any]:
            del event, context
            calls.append("queue")
            return {"batchItemFailures": []}

    class Sweep:
        def __call__(self, event: object, context: object) -> dict[str, Any]:
            del event, context
            calls.append("sweep")
            return {"contract_version": "7.0.1", "candidate_count": 0}

    router = phase718_entrypoints._Phase718VersionedOperationsHandler(
        phase718_entrypoints._Phase718UnifiedRecoveryHandler(
            queue=Queue(),  # type: ignore[arg-type]
            sweep=Sweep(),  # type: ignore[arg-type]
        )
    )
    assert router({"kind": "publication_recovery_sweep"}) == {
        "contract_version": "7.1.0",
        "candidate_count": 0,
    }
    assert router({"Records": [{"eventSource": "aws:sqs"}]}) == {"batchItemFailures": []}
    assert calls == ["sweep", "queue"]

    with pytest.raises(RuntimeError, match="recovery invocation is invalid"):
        router({"kind": "unknown"})
