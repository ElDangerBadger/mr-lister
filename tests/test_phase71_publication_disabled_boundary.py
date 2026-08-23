from __future__ import annotations

import ast
import inspect
import json
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any, get_args

from mr_lister.cloud.browser_contracts import browser_contract_schema
from mr_lister.control import dynamodb as control_dynamodb
from mr_lister.control.models import CONTROL_NEW_WORK_BY_STATE, WorkType
from mr_lister.publication import __all__ as publication_exports
from mr_lister.publication.contract import (
    PublicationActivationPhaseName,
    PublicationPermitState,
    phase7_publication_contract,
)
from mr_lister.publication.dynamodb import DynamoDBPublicationStore
from mr_lister.publication.models import PublicationPermit
from mr_lister.publication.store import InMemoryPublicationStore, PublicationStore
from tools.build_phase66_source_bundles import build_source_bundles

ROOT = Path(__file__).resolve().parents[1]
PUBLICATION_ROOT = ROOT / "src" / "mr_lister" / "publication"
PHASE6_TEMPLATE = ROOT / "infra" / "phase6" / "template.json"
PHASE6_LAMBDA = ROOT / "infra" / "phase6" / "lambda" / "phase6_lambda.py"

EXPECTED_PHASE6_WORK_TYPES = {
    "prepare",
    "synchronize_product",
    "reconcile_product",
    "refresh_economics",
}

PHASE72_EXECUTION_ORACLE_FILES = {
    "execution_commands.py",
    "execution_fingerprints.py",
    "execution_models.py",
    "execution_service.py",
    "execution_store.py",
}

EXPECTED_OFFLINE_PUBLICATION_FILES = {
    "__init__.py",
    "commands.py",
    "contract.py",
    "dynamodb.py",
    "errors.py",
    "fingerprints.py",
    "models.py",
    "provider_boundary.py",
    "service.py",
    "store.py",
} | PHASE72_EXECUTION_ORACLE_FILES


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    return imported


def _python_definitions(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
    }


def _walk(value: object) -> Iterator[object]:
    yield value
    if isinstance(value, dict):
        for key, nested in value.items():
            yield key
            yield from _walk(nested)
    elif isinstance(value, list):
        for nested in value:
            yield from _walk(nested)


def _template() -> dict[str, Any]:
    return json.loads(PHASE6_TEMPLATE.read_text(encoding="utf-8"))


def test_checked_phase71_authority_remains_publish_disabled() -> None:
    contract = phase7_publication_contract()
    checked_contract = json.loads(
        (ROOT / "contracts" / "publication" / "phase7.0.1.json").read_text(encoding="utf-8")
    )
    checked_profile = json.loads(
        (ROOT / "config" / "product_profiles" / "gildan_64000_swiftpod.json").read_text(
            encoding="utf-8"
        )
    )

    assert contract.publication_enabled is False
    assert (
        contract.current_activation_phase is PublicationActivationPhaseName.OFFLINE_IMPLEMENTATION
    )
    assert checked_contract["publication_enabled"] is False
    assert checked_contract["current_activation_phase"] == "offline_implementation"
    assert checked_profile["publish_enabled"] is False


def test_phase6_http_surface_has_no_publish_route_or_handler() -> None:
    template = _template()
    browser_routes = set(browser_contract_schema()["x-mr-lister-routes"]) - {"*"}
    deployed_routes: set[str] = set()
    deployed_handlers: set[str] = set()

    for resource in template["Resources"].values():
        if resource.get("Type") != "AWS::Serverless::Function":
            continue
        properties = resource["Properties"]
        deployed_handlers.add(properties["Handler"])
        for event in properties.get("Events", {}).values():
            if event.get("Type") != "HttpApi":
                continue
            event_properties = event["Properties"]
            deployed_routes.add(f"{event_properties['Method']} {event_properties['Path']}")

    assert deployed_routes == browser_routes
    assert all("/publish" not in route.casefold() for route in deployed_routes)
    assert all("publish" not in handler.casefold() for handler in deployed_handlers)

    lambda_definitions = _python_definitions(PHASE6_LAMBDA)
    entrypoint_definitions = _python_definitions(
        ROOT / "src" / "mr_lister" / "cloud" / "phase6_entrypoints.py"
    )
    assert not {
        name
        for name in lambda_definitions | entrypoint_definitions
        if "publish" in name.casefold() or "publication" in name.casefold()
    }


def test_phase6_infrastructure_has_no_publication_iam_or_state_machine() -> None:
    template = _template()
    resources = template["Resources"]

    assert all(
        "publish" not in logical_id.casefold() and "publication" not in logical_id.casefold()
        for logical_id in resources
    )

    inline_policy_names: set[str] = set()
    statement_ids: set[str] = set()
    iam_actions: set[str] = set()
    for resource in resources.values():
        if resource.get("Type") != "AWS::IAM::Role":
            continue
        for policy in resource.get("Properties", {}).get("Policies", []):
            inline_policy_names.add(policy["PolicyName"])
            for statement in policy["PolicyDocument"]["Statement"]:
                statement_ids.add(statement.get("Sid", ""))
                action = statement["Action"]
                iam_actions.update((action,) if isinstance(action, str) else action)

    assert not {
        value
        for value in inline_policy_names | statement_ids
        if "publish" in value.casefold() or "publication" in value.casefold()
    }
    assert {action for action in iam_actions if action.casefold().endswith(":publish")} <= {
        "sns:Publish"
    }

    state_machines = {
        logical_id
        for logical_id, resource in resources.items()
        if resource.get("Type") == "AWS::Serverless::StateMachine"
    }
    assert state_machines == {
        "PrepareStateMachine",
        "SynchronizeProductStateMachine",
        "ReconcileProductStateMachine",
        "RefreshEconomicsStateMachine",
    }
    assert not any(
        isinstance(value, str)
        and ("MR_LISTER_PUBLICATION" in value or "MR_LISTER_PUBLISH" in value)
        for value in _walk(template)
    )


def test_seller_web_has_no_publish_route_or_control() -> None:
    source_paths = [
        path
        for path in (ROOT / "web" / "src").rglob("*")
        if path.suffix in {".js", ".jsx", ".ts", ".tsx"}
    ]
    combined = "\n".join(path.read_text(encoding="utf-8") for path in source_paths)

    assert re.search(r"/[A-Za-z0-9_{}./-]*publish", combined, re.IGNORECASE) is None
    assert "publish_exact_approved_listing" not in combined
    assert "requestPublication" not in combined
    assert "publishListing" not in combined
    for button in re.findall(r"<button\b.*?</button>", combined, re.IGNORECASE | re.DOTALL):
        assert re.search(r"(?<!un)\bpublish(?:ing)?\b", button, re.IGNORECASE) is None


def test_phase6_dispatcher_cannot_select_publication_work() -> None:
    assert {work_type.value for work_type in WorkType} == EXPECTED_PHASE6_WORK_TYPES
    assert {work_type.value for work_type in CONTROL_NEW_WORK_BY_STATE.values()} <= (
        EXPECTED_PHASE6_WORK_TYPES
    )

    lambda_source = PHASE6_LAMBDA.read_text(encoding="utf-8")
    assert '"publication"' not in lambda_source
    assert '"publish"' not in lambda_source
    for work_type in EXPECTED_PHASE6_WORK_TYPES:
        assert f'"{work_type}": "MR_LISTER_' in lambda_source

    phase6_due_query = inspect.getsource(control_dynamodb.DynamoDBSellerControlStore.list_due_work)
    publication_work_item = inspect.getsource(
        __import__(
            "mr_lister.publication.dynamodb",
            fromlist=["_work_item"],
        )._work_item
    )
    assert '"WORK_DUE#0"' in phase6_due_query
    assert '"PUBLICATION_WORK_DUE#0"' in publication_work_item
    assert 'sort_key=f"PUBLICATION_WORK#' in publication_work_item

    dispatcher = _template()["Resources"]["DispatcherFunction"]["Properties"]
    stream_filter = dispatcher["Events"]["OperationalStateChanges"]["Properties"]["FilterCriteria"][
        "Filters"
    ][0]["Pattern"]
    assert json.loads(stream_filter) == {"dynamodb": {"Keys": {"SK": {"S": [{"prefix": "WORK#"}]}}}}


def test_phase71_permit_and_stores_expose_no_consumption_api() -> None:
    status_annotation = PublicationPermit.model_fields["status"].annotation

    assert get_args(status_annotation) == (PublicationPermitState.AVAILABLE,)
    assert PublicationPermit.model_fields["status"].default is PublicationPermitState.AVAILABLE
    assert not {
        "consumed_at",
        "consumed_work_request_id",
        "retired_at",
    }.intersection(PublicationPermit.model_fields)

    forbidden_operations = {
        "consume",
        "consume_permit",
        "dispatch",
        "dispatch_due",
        "dispatch_one",
        "publish",
        "retire_permit",
    }
    for store_type in (PublicationStore, InMemoryPublicationStore, DynamoDBPublicationStore):
        public_operations = {name for name in vars(store_type) if not name.startswith("_")}
        assert public_operations.isdisjoint(forbidden_operations)


def test_phase6_source_bundles_exclude_publication(tmp_path: Path) -> None:
    lambda_root, agentcore_root = build_source_bundles(
        tmp_path / "disabled-boundary" / "phase6-release"
    )

    for bundle_root in (lambda_root, agentcore_root):
        assert not (bundle_root / "mr_lister" / "publication").exists()
        manifest = json.loads((bundle_root / "source-manifest.json").read_text(encoding="utf-8"))
        paths = {record["path"] for record in manifest["files"]}
        assert not any("publication" in Path(path).parts for path in paths)
        for source_path in bundle_root.rglob("*.py"):
            assert not any(
                module == "mr_lister.publication" or module.startswith("mr_lister.publication.")
                for module in _imports(source_path)
            ), source_path.relative_to(bundle_root)


def test_phase72_offline_publication_runtime_is_not_composed() -> None:
    publication_files = {path.name for path in PUBLICATION_ROOT.glob("*.py")}
    assert publication_files == EXPECTED_OFFLINE_PUBLICATION_FILES
    assert set(publication_exports) == {
        "PHASE7_PUBLICATION_CONTRACT_VERSION",
        "PublicationState",
        "phase7_publication_contract",
        "phase7_publication_contract_digest",
    }
    assert _imports(PUBLICATION_ROOT / "__init__.py") == {"mr_lister.publication.contract"}

    runtime_paths = [
        ROOT / "agentcore_phase6_runtime.py",
        PHASE6_LAMBDA,
        *(
            path
            for path in (ROOT / "src" / "mr_lister").rglob("*.py")
            if PUBLICATION_ROOT not in path.parents
        ),
    ]
    for path in runtime_paths:
        assert not any(
            module == "mr_lister.publication" or module.startswith("mr_lister.publication.")
            for module in _imports(path)
        ), path.relative_to(ROOT)


def test_phase72_execution_oracle_has_no_provider_or_runtime_capability() -> None:
    forbidden_imports = {
        "boto3",
        "botocore",
        "httpx",
        "requests",
        "urllib",
        "mr_lister.agent",
        "mr_lister.cloud",
        "mr_lister.intelligence",
        "mr_lister.production",
        "mr_lister.publication.provider_boundary",
        "mr_lister.workflow",
    }

    for filename in PHASE72_EXECUTION_ORACLE_FILES:
        imports = _imports(PUBLICATION_ROOT / filename)
        assert not any(
            module == forbidden or module.startswith(f"{forbidden}.")
            for module in imports
            for forbidden in forbidden_imports
        ), filename


def test_phase72_provider_boundary_owns_transport_but_not_execution_commands() -> None:
    imports = _imports(PUBLICATION_ROOT / "provider_boundary.py")

    assert any(module == "urllib" or module.startswith("urllib.") for module in imports)
    assert not {
        "mr_lister.publication.execution_commands",
        "mr_lister.publication.execution_service",
    }.intersection(imports)
    assert not any(
        module == runtime or module.startswith(f"{runtime}.")
        for module in imports
        for runtime in {
            "mr_lister.agent",
            "mr_lister.cloud",
            "mr_lister.production",
            "mr_lister.workflow",
        }
    )
