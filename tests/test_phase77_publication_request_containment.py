"""Containment checks for the unregistered Phase 7.7 publication-request seam."""

from __future__ import annotations

import ast
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from mr_lister.cloud import api as phase6_api
from mr_lister.cloud import phase7_entrypoints
from mr_lister.cloud.browser_contracts import browser_contract_schema
from mr_lister.cloud.http import ALL_ROUTE_KEYS
from tools.build_phase66_source_bundles import build_source_bundles

ROOT = Path(__file__).resolve().parents[1]
PHASE6_TEMPLATE = ROOT / "infra" / "phase6" / "template.json"
PHASE7_TEMPLATE = ROOT / "infra" / "phase7" / "template.json"
CHECKED_BROWSER_SCHEMA = ROOT / "contracts" / "browser" / "phase6.5.schema.json"
PHASE6_BROWSER_CLIENT_FILES = (
    ROOT / "web" / "src" / "api" / "client.ts",
    ROOT / "web" / "src" / "contracts.ts",
)
PHASE7_REQUEST_MODULES = (
    ROOT / "src" / "mr_lister" / "publication" / "request_api.py",
    ROOT / "src" / "mr_lister" / "cloud" / "phase7_request_composition.py",
)

PUBLICATION_REQUEST_ROUTE = "POST /v1/jobs/{job_id}/publish"
PUBLICATION_REQUEST_PATH = "/v1/jobs/{job_id}/publish"

_FORBIDDEN_REQUEST_IMPORT_PREFIXES = {
    "httpx",
    "requests",
    "urllib",
    "mr_lister.cloud.phase7_provider_credentials",
    "mr_lister.production",
    "mr_lister.publication.execution_service",
    "mr_lister.publication.provider_boundary",
    "mr_lister.publication.provider_coordinator",
    "mr_lister.publication.provider_credentials",
    "mr_lister.workflow",
}
_FORBIDDEN_AWS_SERVICES = {"iam", "lambda", "secretsmanager", "states", "stepfunctions"}
_FORBIDDEN_CAPABILITY_CALLS = {
    # Provider mutation.
    "publish",
    "publish_exact_approved_listing",
    "publish_exact_product",
    "publish_listing",
    # Workflow dispatch.
    "send_task_failure",
    "send_task_success",
    "start_execution",
    # Secret reads or mutation.
    "create_secret",
    "get_secret_value",
    "put_secret_value",
    "update_secret",
    # IAM mutation.
    "attach_role_policy",
    "create_policy",
    "create_policy_version",
    "create_role",
    "delete_policy",
    "delete_role",
    "delete_role_policy",
    "detach_role_policy",
    "put_role_policy",
    "set_default_policy_version",
    "update_assume_role_policy",
}


def _load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _template_routes(template: dict[str, Any]) -> set[str]:
    routes: set[str] = set()
    for resource in template.get("Resources", {}).values():
        properties = resource.get("Properties", {})
        if resource.get("Type") == "AWS::ApiGatewayV2::Route":
            route_key = properties.get("RouteKey")
            if isinstance(route_key, str):
                routes.add(route_key)
        for event in properties.get("Events", {}).values():
            if event.get("Type") not in {"Api", "HttpApi"}:
                continue
            event_properties = event.get("Properties", {})
            method = event_properties.get("Method")
            path = event_properties.get("Path")
            if isinstance(method, str) and isinstance(path, str):
                routes.add(f"{method.upper()} {path}")
    return routes


def _walk(value: object) -> Iterator[object]:
    yield value
    if isinstance(value, dict):
        for key, nested in value.items():
            yield key
            yield from _walk(nested)
    elif isinstance(value, list):
        for nested in value:
            yield from _walk(nested)


def _imports(tree: ast.AST) -> set[str]:
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    return imported


def _called_names(tree: ast.AST) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        function = node.func
        if isinstance(function, ast.Name):
            names.add(function.id)
        elif isinstance(function, ast.Attribute):
            names.add(function.attr)
    return names


def _string_literals(tree: ast.AST) -> set[str]:
    return {
        node.value.casefold()
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }


def test_phase6_route_and_browser_contract_inventories_exclude_request_route() -> None:
    route_inventories = (
        ALL_ROUTE_KEYS,
        phase6_api._UPLOAD_ROUTES,
        phase6_api._QUERY_ROUTES,
        phase6_api._COMMAND_ROUTES,
    )
    for routes in route_inventories:
        assert PUBLICATION_REQUEST_ROUTE not in routes

    generated_routes = browser_contract_schema()["x-mr-lister-routes"]
    checked_routes = _load_json(CHECKED_BROWSER_SCHEMA)["x-mr-lister-routes"]
    assert PUBLICATION_REQUEST_ROUTE not in generated_routes
    assert PUBLICATION_REQUEST_ROUTE not in checked_routes


def test_phase6_browser_client_and_template_do_not_register_request_route() -> None:
    for path in PHASE6_BROWSER_CLIENT_FILES:
        source = path.read_text(encoding="utf-8")
        assert "/publish" not in source, path.relative_to(ROOT)

    template = _load_json(PHASE6_TEMPLATE)
    assert PUBLICATION_REQUEST_ROUTE not in _template_routes(template)
    assert PUBLICATION_REQUEST_PATH not in {
        value for value in _walk(template) if isinstance(value, str)
    }


def test_phase6_source_bundles_exclude_phase77_request_seam(tmp_path: Path) -> None:
    lambda_root, agentcore_root = build_source_bundles(
        tmp_path / "phase77-containment" / "phase6-release"
    )

    for bundle_root in (lambda_root, agentcore_root):
        manifest = _load_json(bundle_root / "source-manifest.json")
        paths = {record["path"] for record in manifest["files"]}
        assert "mr_lister/publication/request_api.py" not in paths
        assert "mr_lister/cloud/phase7_request_composition.py" not in paths
        assert not any("publication" in Path(path).parts for path in paths)
        assert not any("phase7" in Path(path).name.casefold() for path in paths)
        for source_path in bundle_root.rglob("*.py"):
            source = source_path.read_text(encoding="utf-8")
            assert PUBLICATION_REQUEST_PATH not in source
            assert "mr_lister.publication.request_api" not in source
            assert "phase7_request_composition" not in source


def test_phase7_sam_has_no_route_function_url_or_invocation_trigger() -> None:
    template = _load_json(PHASE7_TEMPLATE)
    resources = template["Resources"]

    assert PUBLICATION_REQUEST_ROUTE not in _template_routes(template)
    for resource in resources.values():
        properties = resource.get("Properties", {})
        if resource.get("Type") == "AWS::Serverless::Function":
            assert "Events" not in properties
            assert "FunctionUrlConfig" not in properties

    forbidden_trigger_types = {
        "AWS::ApiGateway::RestApi",
        "AWS::ApiGatewayV2::Api",
        "AWS::ApiGatewayV2::Integration",
        "AWS::ApiGatewayV2::Route",
        "AWS::Events::Rule",
        "AWS::Lambda::EventSourceMapping",
        "AWS::Lambda::Permission",
        "AWS::Lambda::Url",
        "AWS::Serverless::Api",
        "AWS::Serverless::HttpApi",
        "AWS::Serverless::StateMachine",
    }
    assert not {resource["Type"] for resource in resources.values()} & forbidden_trigger_types


def test_phase7_entrypoint_exports_remain_request_free() -> None:
    assert phase7_entrypoints.__all__ == ["publication_query_api_handler"]
    assert not hasattr(phase7_entrypoints, "publication_request_api_handler")
    assert not hasattr(phase7_entrypoints, "publication_provider_handler")
    assert not hasattr(phase7_entrypoints, "publication_workflow_handler")


def test_unregistered_request_modules_have_no_external_mutation_capability() -> None:
    """Apply immediately when the two seam modules land, while allowing either to land first."""

    for path in PHASE7_REQUEST_MODULES:
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except FileNotFoundError:
            continue

        imports = _imports(tree)
        assert not {
            imported
            for imported in imports
            if any(
                imported == forbidden or imported.startswith(f"{forbidden}.")
                for forbidden in _FORBIDDEN_REQUEST_IMPORT_PREFIXES
            )
        }, path.relative_to(ROOT)
        assert _called_names(tree).isdisjoint(_FORBIDDEN_CAPABILITY_CALLS), path.relative_to(ROOT)
        assert _string_literals(tree).isdisjoint(_FORBIDDEN_AWS_SERVICES), path.relative_to(ROOT)
