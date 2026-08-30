"""Containment oracle for the not-yet-composed Phase 7 publication worker.

The worker composition may be developed as source-only, dependency-injected code.  Until a later
reviewed activation slice, it must not become a handler, route, deployment resource, credential
resolver, provider client, or Phase 6/browser dependency merely by existing in the repository.
"""

from __future__ import annotations

import ast
import json
import re
from collections.abc import Iterable
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
PHASE7_TEMPLATE = ROOT / "infra" / "phase7" / "template.json"
PHASE6_TEMPLATE = ROOT / "infra" / "phase6" / "template.json"
PHASE6_BUNDLE_BUILDER = ROOT / "tools" / "build_phase66_source_bundles.py"
PHASE7_WORKER_COMPOSITION = ROOT / "src" / "mr_lister" / "cloud" / "phase7_worker_composition.py"
PHASE7_PROVIDER_RUNTIME = ROOT / "src" / "mr_lister" / "publication" / "provider_runtime.py"

CONDITIONED_QUERY_RESOURCES = {
    "PublicationStatusQueryFunctionRole",
    "PublicationStatusQueryLogGroup",
    "PublicationStatusQueryFunction",
    "PublicationStatusQueryErrorsAlarm",
    "PublicationStatusQueryThrottlesAlarm",
    "PublicationStatusQueryDurationAlarm",
}

ACTIVE_GUARD_RESOURCES = {
    "PublicationGuardVerificationFunctionRole",
    "PublicationGuardVerificationLogGroup",
    "PublicationGuardVerificationFunction",
    "PublicationGuardVerificationErrorsAlarm",
    "PublicationGuardVerificationThrottlesAlarm",
    "PublicationGuardVerificationDurationAlarm",
    "PublicationStatusAlarmTopicKey",
    "PublicationStatusAlarmTopic",
    "PublicationStatusAlarmTopicPolicy",
}

PHASE6_HTTP_ROUTES = {
    "GET /health",
    "GET /v1/jobs",
    "GET /v1/jobs/{job_id}",
    "GET /v1/jobs/{job_id}/artwork-preview",
    "GET /v1/jobs/{job_id}/review",
    "GET /v1/uploads/{upload_id}",
    "POST /v1/jobs/{job_id}/approve",
    "POST /v1/jobs/{job_id}/cancel",
    "POST /v1/jobs/{job_id}/economics/refresh",
    "POST /v1/jobs/{job_id}/retry",
    "POST /v1/uploads",
    "POST /v1/uploads/{upload_id}/authorize",
    "POST /v1/uploads/{upload_id}/cancel",
    "POST /v1/uploads/{upload_id}/complete",
    "PUT /v1/jobs/{job_id}/review/listing",
}


def _json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _tree(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _imports(tree: ast.AST) -> set[str]:
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            imported.add(node.module)
    return imported


def _call_name(call: ast.Call) -> str:
    parts: list[str] = []
    current: ast.expr = call.func
    while isinstance(current, ast.Attribute):
        parts.append(current.attr)
        current = current.value
    if isinstance(current, ast.Name):
        parts.append(current.id)
    return ".".join(reversed(parts))


def _calls(expressions: Iterable[ast.expr | None]) -> set[str]:
    return {
        _call_name(node)
        for expression in expressions
        if expression is not None
        for node in ast.walk(expression)
        if isinstance(node, ast.Call)
    }


def _module_scope_calls(tree: ast.Module) -> set[str]:
    expressions: list[ast.expr | None] = []
    for node in tree.body:
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            expressions.append(node.value)
        elif isinstance(node, ast.Expr):
            expressions.append(node.value)
        elif isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            expressions.extend(node.decorator_list)
    return _calls(expressions)


def _public_exports(path: Path) -> list[str]:
    for node in _tree(path).body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "__all__" for target in node.targets
        ):
            value = ast.literal_eval(node.value)
            assert isinstance(value, list) and all(isinstance(item, str) for item in value)
            return value
    raise AssertionError(f"{path.relative_to(ROOT)} must declare a closed __all__ surface")


def test_phase7_template_remains_the_exact_disabled_nine_resource_guard_topology() -> None:
    template = _json(PHASE7_TEMPLATE)
    resources = template["Resources"]

    assert set(resources) == ACTIVE_GUARD_RESOURCES | CONDITIONED_QUERY_RESOURCES
    assert len(ACTIVE_GUARD_RESOURCES) == 9
    assert {
        logical_id for logical_id, resource in resources.items() if "Condition" not in resource
    } == ACTIVE_GUARD_RESOURCES
    assert all(
        resources[logical_id].get("Condition") == "DeployPublicationStatusQueryScaffold"
        for logical_id in CONDITIONED_QUERY_RESOURCES
    )

    variables = template["Globals"]["Function"]["Environment"]["Variables"]
    assert variables["MR_LISTER_PHASE7_QUERY_ENABLED"] == "false"
    assert variables["MR_LISTER_PHASE7_REQUEST_ENABLED"] == "false"
    assert variables["MR_LISTER_PHASE7_PUBLICATION_ENABLED"] == "false"
    outputs = template["Outputs"]
    assert outputs["PublicationStatusQueryRegistered"]["Value"] == "false"
    assert outputs["PublicationStatusQueryEnabled"]["Value"] == "false"
    assert outputs["PublicationRequestEnabled"]["Value"] == "false"
    assert outputs["PublicationEnabled"]["Value"] == "false"
    assert outputs["PublicationGuardExternalCallsEnabled"]["Value"] == "false"

    functions = {
        logical_id: resource
        for logical_id, resource in resources.items()
        if resource["Type"] == "AWS::Serverless::Function"
    }
    assert set(functions) == {
        "PublicationStatusQueryFunction",
        "PublicationGuardVerificationFunction",
    }
    assert {function["Properties"]["Handler"] for function in functions.values()} == {
        "phase7_lambda.publication_query_api_handler",
        "mr_lister.cloud.phase7_guard_entrypoint.publication_guard_verification_handler",
    }
    for function in functions.values():
        properties = function["Properties"]
        assert "Events" not in properties
        assert "FunctionUrlConfig" not in properties
        assert "EventInvokeConfig" not in properties

    assert {
        logical_id
        for logical_id, resource in resources.items()
        if resource["Type"] == "AWS::IAM::Role"
    } == {
        "PublicationStatusQueryFunctionRole",
        "PublicationGuardVerificationFunctionRole",
    }
    forbidden_resource_types = {
        "AWS::ApiGateway::RestApi",
        "AWS::ApiGatewayV2::Api",
        "AWS::Lambda::EventSourceMapping",
        "AWS::Lambda::Permission",
        "AWS::Lambda::Url",
        "AWS::SecretsManager::Secret",
        "AWS::Serverless::Api",
        "AWS::Serverless::HttpApi",
        "AWS::Serverless::StateMachine",
        "AWS::StepFunctions::StateMachine",
    }
    assert not {resource["Type"] for resource in resources.values()} & forbidden_resource_types

    serialized = json.dumps(template, sort_keys=True).casefold()
    for forbidden in (
        "boto3.client",
        "functionurlconfig",
        "printify",
        "publish.json",
        "secretsmanager:",
        "states:",
    ):
        assert forbidden not in serialized


def test_no_publication_worker_handler_or_route_is_registered() -> None:
    phase7_entrypoint = ROOT / "src" / "mr_lister" / "cloud" / "phase7_entrypoints.py"
    guard_entrypoint = ROOT / "src" / "mr_lister" / "cloud" / "phase7_guard_entrypoint.py"
    scaffold = ROOT / "infra" / "phase7" / "lambda" / "phase7_lambda.py"

    assert _public_exports(phase7_entrypoint) == ["publication_query_api_handler"]
    assert _public_exports(guard_entrypoint) == [
        "Phase7GuardRuntimeError",
        "publication_guard_verification_handler",
    ]
    assert _public_exports(scaffold) == [
        "PRODUCTION_ENTRYPOINT",
        "REQUIRED_DISABLED_ENVIRONMENT",
        "Phase7ReadOnlyScaffoldNotReady",
        "publication_query_api_handler",
    ]

    forbidden_modules = {
        "mr_lister.cloud.phase7_worker_composition",
        "mr_lister.publication.provider_runtime",
    }
    registration_paths = (
        phase7_entrypoint,
        guard_entrypoint,
        scaffold,
        ROOT / "src/mr_lister/cloud/__init__.py",
    )
    for path in registration_paths:
        assert _imports(_tree(path)).isdisjoint(forbidden_modules), path.relative_to(ROOT)


def test_phase6_deployment_bundles_and_seller_browser_do_not_acquire_the_worker() -> None:
    template = _json(PHASE6_TEMPLATE)
    routes: set[str] = set()
    handlers: set[str] = set()
    for resource in template["Resources"].values():
        if resource.get("Type") != "AWS::Serverless::Function":
            continue
        properties = resource["Properties"]
        handlers.add(properties["Handler"])
        for event in properties.get("Events", {}).values():
            if event.get("Type") == "HttpApi":
                event_properties = event["Properties"]
                routes.add(f"{event_properties['Method']} {event_properties['Path']}")

    assert routes == PHASE6_HTTP_ROUTES
    assert all("phase7" not in handler.casefold() for handler in handlers)
    assert all("publish" not in route.casefold() for route in routes)

    builder_source = PHASE6_BUNDLE_BUILDER.read_text(encoding="utf-8")
    assert "phase7_worker_composition.py" not in builder_source
    assert "provider_runtime.py" not in builder_source

    browser_source = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (ROOT / "web" / "src").rglob("*")
        if path.suffix in {".js", ".jsx", ".ts", ".tsx"}
    )
    assert re.search(r"/[A-Za-z0-9_{}./-]*publish", browser_source, re.IGNORECASE) is None
    assert "publish_exact_approved_listing" not in browser_source
    assert "requestPublication" not in browser_source
    assert "publicationWorker" not in browser_source


def test_optional_worker_sources_have_no_default_or_import_time_capability() -> None:
    """Source-only modules may land before composition without activating themselves."""

    for path in (PHASE7_WORKER_COMPOSITION, PHASE7_PROVIDER_RUNTIME):
        if not path.exists():
            continue
        tree = _tree(path)
        imports = _imports(tree)
        forbidden_imports = {
            "aiohttp",
            "boto3",
            "botocore",
            "httpx",
            "requests",
            "socket",
            "urllib.request",
            "urllib3",
            "mr_lister.cloud.phase7_provider_credentials",
            "mr_lister.production.provider_secrets",
        }
        assert not {
            imported
            for imported in imports
            if any(
                imported == forbidden or imported.startswith(f"{forbidden}.")
                for forbidden in forbidden_imports
            )
        }, path.relative_to(ROOT)

        definitions = {
            node.name
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        # A dependency-injected, exact-disabled wrapper may be defined as an offline oracle.  The
        # entrypoint/template assertions above are what prove that no such definition is registered.
        assert not {
            name
            for name in definitions
            if name.startswith("default_") and ("client" in name or "factory" in name)
        }

        source = path.read_text(encoding="utf-8")
        assert "default_aws_client_factory" not in source
        assert "boto3.client" not in source
        assert "boto3.resource" not in source

        suspicious_import_time_tokens = {
            "client",
            "credential",
            "dispatcher",
            "handler",
            "http",
            "opener",
            "provider",
            "resource",
            "secret",
            "session",
            "store",
            "transport",
            "worker",
        }
        assert not {
            call
            for call in _module_scope_calls(tree)
            if any(token in call.casefold() for token in suspicious_import_time_tokens)
        }, path.relative_to(ROOT)

        for function in (
            node
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        ):
            defaults = [*function.args.defaults, *function.args.kw_defaults]
            assert not {
                call
                for call in _calls(defaults)
                if "client" in call.casefold()
                or "factory" in call.casefold()
                or "transport" in call.casefold()
            }, f"{path.relative_to(ROOT)}:{function.name}"
