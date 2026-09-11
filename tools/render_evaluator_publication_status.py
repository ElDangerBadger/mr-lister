"""Render/check the local evaluator-only, read-only publication-status SAM overlay.

The source is the exact reviewed Phase 6 scaffold. This is NOT a deployable release:
scaffold mode, handlers, code, IAM and activation settings stay byte-for-byte equivalent.
An evaluator release still needs dedicated credentials, source sealing, immutable runtime
bindings, staged evidence and a separately reviewed activation path. The existing Phase 6
staging renderer pins the original source hash and does not accept this overlay.

Inputs are the same nonsecret identifier files used by prepare_evaluator_deployment;
all evaluator identifiers must be assigned. No secret, AWS or provider client is accessed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from copy import deepcopy
from pathlib import Path
from typing import Any

from tools.prepare_evaluator_deployment import EvaluatorPlanError, build_plan, read_json

ROOT = Path(__file__).resolve().parents[1]
SOURCE_TEMPLATE_SHA256 = "1f9772e04ace5a035febeea14b92417866326e5f07a0495a981648dac625fd09"
POLICY_SETTING = "MR_LISTER_EVALUATOR_PUBLICATION_STATUS"
EVENT_NAME = "EvaluatorPublicationStatus"


def render_evaluator_status_template(
    production: dict[str, Any],
    evaluator: dict[str, Any],
    *,
    repository: Path = ROOT,
) -> dict[str, Any]:
    """Add only the evaluator binding pins, authenticated GET and read-only policy."""

    source = repository / "infra/phase6/template.json"
    try:
        raw_source = source.read_bytes()
        if hashlib.sha256(raw_source).hexdigest() != SOURCE_TEMPLATE_SHA256:
            raise EvaluatorPlanError("Reviewed Phase 6 scaffold changed; overlay review required")
    except OSError:
        raise EvaluatorPlanError("Reviewed Phase 6 scaffold is unavailable") from None
    template = deepcopy(json.loads(raw_source))
    plan = build_plan(production, evaluator, repository=repository)
    if (
        plan["template_sha256"]
        != hashlib.sha256(
            json.dumps(template, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
    ):
        raise EvaluatorPlanError("Source changed during evaluator validation")
    if plan["unassigned_identifiers"]:
        raise EvaluatorPlanError("Assign all isolated evaluator identifiers before rendering")
    target = plan["evaluator_identifiers"]
    for parameter, field in {
        "EnvironmentName": "environment_name",
        "ApplicationOrigin": "application_origin",
        "PrintifySecretArn": "printify_secret_arn",
    }.items():
        template["Parameters"][parameter].update(
            {"Default": target[field], "AllowedValues": [target[field]]}
        )
    # Prevent a CLI parameter override or different AWS deployment target from undoing the pins.
    template.setdefault("Rules", {})["EvaluatorDeploymentTarget"] = {
        "Assertions": [
            {
                "Assert": {"Fn::Equals": [{"Ref": pseudo}, target[field]]},
                "AssertDescription": "Use only the assigned evaluator deployment target.",
            }
            for pseudo, field in {"AWS::AccountId": "account_id", "AWS::Region": "region"}.items()
        ]
    }
    query = template["Resources"]["ReviewQueryApiFunction"]["Properties"]
    query["Environment"]["Variables"][POLICY_SETTING] = "read_only"
    query["Events"][EVENT_NAME] = {
        "Type": "HttpApi",
        "Properties": {
            "ApiId": {"Ref": "SellerHttpApi"},
            "Path": "/v1/jobs/{job_id}/publication",
            "Method": "GET",
            "PayloadFormatVersion": "2.0",
            "Auth": {
                "Authorizer": "SellerJwtAuthorizer",
                "AuthorizationScopes": ["mr-lister-api/seller"],
            },
        },
    }
    template.setdefault("Metadata", {})["MrListerEvaluatorPublication"] = {
        "ContractVersion": "evaluator-publication-v1",
        "PublicationEnabled": False,
        "DeploymentReady": False,
        "SourceTemplateSHA256": SOURCE_TEMPLATE_SHA256,
        "RuntimeBindingAndActivationRequired": True,
    }
    return template


def verify_evaluator_status_template(
    candidate: dict[str, Any],
    production: dict[str, Any],
    evaluator: dict[str, Any],
    *,
    repository: Path = ROOT,
) -> None:
    """Reject every difference from the exact allowed derivation, including new ingress/IAM."""

    expected = render_evaluator_status_template(production, evaluator, repository=repository)
    # Canonical JSON distinguishes booleans from equal-looking Python integers.
    if json.dumps(candidate, sort_keys=True, separators=(",", ":"), allow_nan=False) != json.dumps(
        expected, sort_keys=True, separators=(",", ":"), allow_nan=False
    ):
        raise EvaluatorPlanError("Evaluator overlay differs from the exact reviewed derivation")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--production-identifiers", required=True, type=Path)
    parser.add_argument("--evaluator-identifiers", required=True, type=Path)
    parser.add_argument("--repository", type=Path, default=ROOT)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--output", type=Path)
    action.add_argument("--verify-template", type=Path)
    args = parser.parse_args(argv)
    try:
        production = read_json(args.production_identifiers)
        evaluator = read_json(args.evaluator_identifiers)
        if args.verify_template is not None:
            verify_evaluator_status_template(
                read_json(args.verify_template), production, evaluator, repository=args.repository
            )
        else:
            template = render_evaluator_status_template(
                production, evaluator, repository=args.repository
            )
            descriptor = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "w") as output:
                output.write(json.dumps(template, indent=2) + "\n")
    except (EvaluatorPlanError, OSError, ValueError) as error:
        message = str(error) if isinstance(error, EvaluatorPlanError) else "Input or output invalid"
        parser.exit(2, f"Evaluator overlay refused: {message}\n")
    print("Evaluator scaffold overlay checked; no deployment or live verification performed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
