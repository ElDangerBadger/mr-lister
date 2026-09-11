"""Generate a non-deployable, local-only plan for an isolated evaluator environment.

This does not create resources, resolve secrets, render CloudFormation, or establish
live isolation. Both input objects contain only these exact nonsecret identifiers:
environment_name, account_id, region, application_origin, owner_id,
printify_shop_id, printify_secret_arn. The evaluator's last three fields may be null
until separately assigned. Production identifiers must be complete and trustworthy.

Example: python -m tools.prepare_evaluator_deployment --production-identifiers BASE.json
  --evaluator-identifiers TARGET.json --output PLAN.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
FIELDS = frozenset(
    {
        "environment_name",
        "account_id",
        "region",
        "application_origin",
        "owner_id",
        "printify_shop_id",
        "printify_secret_arn",
    }
)
PENDING_FIELDS = frozenset({"owner_id", "printify_shop_id", "printify_secret_arn"})
EXPECTED_ROUTES = frozenset(
    {
        "GET /health",
        "POST /v1/uploads",
        "GET /v1/uploads/{upload_id}",
        "POST /v1/uploads/{upload_id}/authorize",
        "POST /v1/uploads/{upload_id}/complete",
        "POST /v1/uploads/{upload_id}/cancel",
        "GET /v1/jobs",
        "GET /v1/jobs/{job_id}",
        "GET /v1/jobs/{job_id}/review",
        "GET /v1/jobs/{job_id}/artwork-preview",
        "PUT /v1/jobs/{job_id}/review/listing",
        "POST /v1/jobs/{job_id}/economics/refresh",
        "POST /v1/jobs/{job_id}/approve",
        "POST /v1/jobs/{job_id}/cancel",
        "POST /v1/jobs/{job_id}/retry",
    }
)


class EvaluatorPlanError(ValueError):
    """A deliberately value-free validation error."""


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise EvaluatorPlanError("Duplicate JSON field")
        result[key] = value
    return result


def _reject_constant(_value: str) -> None:
    raise EvaluatorPlanError("Nonstandard JSON value")


def read_json(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as source:
            raw = source.read(2_000_001)
        if len(raw) > 2_000_000:
            raise EvaluatorPlanError("Input exceeds size limit")
        result = json.loads(raw, object_pairs_hook=_unique_object, parse_constant=_reject_constant)
        if not isinstance(result, dict):
            raise EvaluatorPlanError("Input must be a JSON object")
        return result
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise EvaluatorPlanError("Input is unavailable or invalid JSON") from None


def _matches(value: object, pattern: str) -> bool:
    return isinstance(value, str) and re.fullmatch(pattern, value) is not None


def _template_authority(template: dict[str, Any]) -> tuple[dict[str, str], list[str]]:
    try:
        parameters = template["Parameters"]
        patterns = {
            field: parameters[parameter]["AllowedPattern"]
            for field, parameter in {
                "environment_name": "EnvironmentName",
                "application_origin": "ApplicationOrigin",
                "printify_secret_arn": "PrintifySecretArn",
            }.items()
        }
        resources = template["Resources"]
        api = resources["SellerHttpApi"]["Properties"]
        if api["DefinitionBody"]["paths"] != {} or "DefinitionUri" in api:
            raise EvaluatorPlanError("Embedded API routes changed; review required")
        if api["Auth"]["DefaultAuthorizer"] != "SellerJwtAuthorizer":
            raise EvaluatorPlanError("Default API authentication changed")
        pool = resources["SellerUserPool"]["Properties"]
        if pool["AdminCreateUserConfig"]["AllowAdminCreateUserOnly"] is not True:
            raise EvaluatorPlanError("Expected invited seller authentication")
        routes = []
        for name, resource in resources.items():
            resource_type = resource.get("Type", "")
            if (
                resource_type.startswith("AWS::ApiGateway")
                or resource_type == "AWS::Serverless::Api"
            ):
                raise EvaluatorPlanError("Alternate API resources require review")
            if resource_type == "AWS::Serverless::HttpApi" and name != "SellerHttpApi":
                raise EvaluatorPlanError("Additional HTTP API requires review")
            if "FunctionUrlConfig" in resource.get("Properties", {}):
                raise EvaluatorPlanError("Function URL ingress requires review")
            for event in resource.get("Properties", {}).get("Events", {}).values():
                if event.get("Type") == "Api":
                    raise EvaluatorPlanError("Alternate API events require review")
                if event.get("Type") == "HttpApi":
                    properties = event["Properties"]
                    if properties["ApiId"] != {"Ref": "SellerHttpApi"}:
                        raise EvaluatorPlanError("Seller route API binding changed")
                    route = f"{properties['Method']} {properties['Path']}"
                    routes.append(route)
                    if route != "GET /health" and properties.get("Auth") != {
                        "Authorizer": "SellerJwtAuthorizer",
                        "AuthorizationScopes": ["mr-lister-api/seller"],
                    }:
                        raise EvaluatorPlanError("Seller route authentication changed")
        if len(routes) != len(EXPECTED_ROUTES) or set(routes) != EXPECTED_ROUTES:
            raise EvaluatorPlanError("Phase 6 route surface changed; review required")
        return patterns, sorted(routes)
    except (KeyError, TypeError, AttributeError):
        raise EvaluatorPlanError("Phase 6 template authority is incomplete") from None


def _validate_identifiers(
    value: dict[str, Any],
    patterns: dict[str, str],
    *,
    pending_allowed: bool,
) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != FIELDS:
        raise EvaluatorPlanError("Identifier schema differs; secret values are not accepted")
    for field, item in value.items():
        if item is None and pending_allowed and field in PENDING_FIELDS:
            continue
        if field in patterns:
            valid = _matches(item, patterns[field])
        elif field == "account_id":
            valid = _matches(item, r"[0-9]{12}") and item != "0" * 12
        elif field == "region":
            valid = _matches(item, r"[a-z]{2}(?:-[a-z]+)+-[0-9]")
        elif field == "owner_id":
            valid = _matches(item, r"[a-f0-9]{64}") and item != "0" * 64
        else:
            valid = type(item) is int and item > 0
        if not valid:
            raise EvaluatorPlanError(f"Invalid nonsecret identifier: {field}")
    secret = value["printify_secret_arn"]
    if secret is not None:
        arn_parts = secret.split(":", 6)
        if arn_parts[3:5] != [value["region"], value["account_id"]]:
            raise EvaluatorPlanError("Secret account or region differs from target")
        if pending_allowed and not arn_parts[6].startswith(
            f"mr-lister/{value['environment_name']}/"
        ):
            raise EvaluatorPlanError("Evaluator secret must use its own environment namespace")
    derived = dict(value)
    derived["state_table"] = f"mr-lister-phase6-{value['environment_name']}"
    derived["artifact_bucket"] = (
        f"mr-lister-phase6-artifacts-{value['environment_name']}-"
        f"{value['account_id']}-{value['region']}"
    )
    if len(derived["artifact_bucket"]) > 63:
        raise EvaluatorPlanError("Derived artifact bucket exceeds current runtime limit")
    return derived


def build_plan(
    production: dict[str, Any],
    evaluator: dict[str, Any],
    *,
    repository: Path = ROOT,
) -> dict[str, Any]:
    template = read_json(repository / "infra/phase6/template.json")
    patterns, routes = _template_authority(template)
    baseline = _validate_identifiers(production, patterns, pending_allowed=False)
    target = _validate_identifiers(evaluator, patterns, pending_allowed=True)
    for field in (
        "environment_name",
        "application_origin",
        "state_table",
        "artifact_bucket",
        "owner_id",
        "printify_shop_id",
        "printify_secret_arn",
    ):
        if target[field] is not None and target[field] == baseline[field]:
            raise EvaluatorPlanError(f"Evaluator reuses a production identifier: {field}")
    pending = sorted(field for field in PENDING_FIELDS if target[field] is None)
    return {
        "format": "mr-lister-evaluator-deployment-plan-v1",
        "status": "planning_only_runtime_unverified",
        "deployment_ready": False,
        "cloud_operations_performed": False,
        "credentials_read": False,
        "provider_mode": "isolated_real_printify_drafts",
        "publication_enabled": False,
        "template_sha256": hashlib.sha256(
            json.dumps(template, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest(),
        "production_identifiers": baseline,
        "evaluator_identifiers": target,
        "unassigned_identifiers": pending,
        "phase6_route_inventory": routes,
        "excluded_deployment": "Phase 7 publication stack, routes, worker and credentials",
        "missing_runtime_bindings": [
            "Dedicated Cognito pool/client, invited seller/MFA and issuer-plus-subject owner",
            "Dedicated HTTP API, state table, private buckets, roles and HTTPS origin/certificate",
            "Independent owner-bound Printify credential and shop readback; no production token",
            "Sealed source/profile artifacts and exact-version S3 readback evidence",
            "Evaluator AgentCore runtime/endpoint/version/qualifier and binding fingerprint",
            "Reviewed Phase 6 activation and web runtime configuration for evaluator origin",
            "Deploy and verify evaluator disabled-publication status and matching web contract",
            "Deployed cross-owner tests, route/IAM inventory and bounded real draft smoke test",
        ],
        "limitations": [
            "Supplied identifiers only; does not attest the production baseline or live resources",
            "Different secret ARNs do not prove different tokens; this tool never reads either",
            "No CloudFormation parameters or executable deployment commands are emitted",
            "The Phase 6 scaffold is inert; existing staged-render tooling cannot activate it",
            "A second production login or browser flag cannot create evaluator isolation",
            "Simulated publication is not implemented or represented as a real Etsy result",
        ],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--production-identifiers", required=True, type=Path)
    parser.add_argument("--evaluator-identifiers", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--repository", type=Path, default=ROOT)
    args = parser.parse_args(argv)
    try:
        plan = build_plan(
            read_json(args.production_identifiers),
            read_json(args.evaluator_identifiers),
            repository=args.repository,
        )
        data = (json.dumps(plan, indent=2, sort_keys=True) + "\n").encode()
        descriptor = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as output:
            output.write(data)
    except (EvaluatorPlanError, OSError) as error:
        message = (
            str(error) if isinstance(error, EvaluatorPlanError) else "Output unavailable or exists"
        )
        parser.exit(2, f"Evaluator plan refused: {message}\n")
    print("Local evaluator plan written; no deployment or live verification performed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
