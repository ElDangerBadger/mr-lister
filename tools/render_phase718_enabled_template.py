"""Render the separately reviewed Phase 7.18 general-availability stack update.

The renderer starts from the exact P7.15C production-disabled template.  It does not create or
modify a Phase 6 resource: the existing table, stream, Cognito authority, and seller HTTP API are
accepted only as parameters.  The delta enables the six already-separated Phase 7 roles,
functions, bounded workflow and recovery/retention triggers, and adds two authenticated routes to
the existing seller API.  Only the worker receives the exact Printify secret capability.
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Mapping, Sequence
from copy import deepcopy
from hashlib import sha256
from pathlib import Path
from typing import Any, Final, cast

ROOT: Final = Path(__file__).resolve().parents[1]
BASE_TEMPLATE: Final = ROOT / "infra/phase7/production-disabled-template.json"
BASE_TEMPLATE_SHA256: Final = "2a98ab2a7cf3fb04590f9f8cd3a30cc6c2e373421e70c70220be419b80ca7df2"
TOPOLOGY_AUTHORITY: Final = ROOT / "infra/phase7/phase718-enabled-topology.json"
TOPOLOGY_AUTHORITY_SHA256: Final = (
    "676409cbe5a64276227fc370bf653076ea873033b1b020a791e534359ddbe2b8"
)
WORKFLOW_DEFINITION: Final = ROOT / "infra/phase7/statemachine/publication.asl.json"
WORKFLOW_DEFINITION_SHA256: Final = (
    "9a6112c85b35e775d1e60681a0ca14e6740cd0aea82b2ac33b5aa74b86fc3098"
)
DEFAULT_OUTPUT: Final = ROOT / ".mr_lister_private/phase718-enabled/enabled-template.json"

ACTIVATION_MODE: Final = "GENERAL_AVAILABILITY"
CONTRACT_VERSION: Final = "7.1.0"
CONTRACT_FINGERPRINT: Final = "5172926cb89f8c046247922d8311c3f8b6361a9d67a719aa3a19a1c0ef1ed678"
PROFILE_FINGERPRINT: Final = "5de1257141cfdacb1731f68bb9113712957483b33d5b0f7115afdba86eb7476c"
ENABLED_CONDITION: Final = "EnableGeneralAvailability"
ENABLED_ARCHIVE_KEY: Final = "phase7/releases/${EnabledReleaseFingerprint}/enabled.zip"

_FINGERPRINT_PATTERN: Final = "^(?!0{64}$)[a-f0-9]{64}$"
_OBJECT_VERSION_PATTERN: Final = "^(?!null$)[A-Za-z0-9._+=/-]{1,1024}$"
_BUCKET_PATTERN: Final = "^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$"
_API_ID_PATTERN: Final = "^[a-z0-9]{10}$"
_AUTHORIZER_ID_PATTERN: Final = "^[A-Za-z0-9]{1,64}$"
_GENERIC_ERROR: Final = "Phase 7.18 enabled template is invalid"

_FUNCTIONS: Final = (
    "Query",
    "Request",
    "Dispatcher",
    "Worker",
    "Recovery",
    "Retention",
)
_RULES: Final = (
    "PublicationDueWorkSweepRule",
    "PublicationRecoverySweepRule",
    "PublicationWorkflowFailureRule",
)
_EXPLICIT_MAPPINGS: Final = (
    "PublicationDispatcherStreamMapping",
    "PublicationRetentionStreamMapping",
)


class Phase718EnabledTemplateError(RuntimeError):
    """Value-free refusal for a drifting or capability-expanded enabled topology."""


def render_phase718_enabled_template(
    *,
    base_template_path: Path = BASE_TEMPLATE,
    topology_authority_path: Path = TOPOLOGY_AUTHORITY,
) -> bytes:
    """Return canonical bytes for the exact enabled successor to P7.15C."""

    try:
        base_raw = _exact_file(base_template_path)
        authority_raw = _exact_file(topology_authority_path)
        workflow_raw = _exact_file(WORKFLOW_DEFINITION)
        if (
            sha256(base_raw).hexdigest() != BASE_TEMPLATE_SHA256
            or sha256(authority_raw).hexdigest() != TOPOLOGY_AUTHORITY_SHA256
            or sha256(workflow_raw).hexdigest() != WORKFLOW_DEFINITION_SHA256
        ):
            raise ValueError
        base = _mapping(json.loads(base_raw))
        authority = _mapping(json.loads(authority_raw))
        workflow = _mapping(json.loads(workflow_raw))
        _verify_authority(authority)
        enabled = deepcopy(cast(dict[str, Any], base))
        _apply_enabled_delta(enabled, authority, workflow)
        _verify_enabled_template(base, enabled, authority)
        return _canonical(enabled)
    except Phase718EnabledTemplateError:
        raise
    except Exception:
        raise Phase718EnabledTemplateError(_GENERIC_ERROR) from None


def verify_phase718_enabled_template(template_path: Path) -> str:
    """Require byte-exact canonical output and return its SHA-256."""

    try:
        observed = _exact_file(template_path)
        expected = render_phase718_enabled_template()
        if observed != expected:
            raise ValueError
        return sha256(observed).hexdigest()
    except Phase718EnabledTemplateError:
        raise
    except Exception:
        raise Phase718EnabledTemplateError(_GENERIC_ERROR) from None


def write_phase718_enabled_template(destination: Path) -> tuple[Path, str]:
    """Create, but never overwrite, one private generated deployment template."""

    try:
        output = destination.resolve(strict=False)
        if destination.is_symlink() or output.exists():
            raise ValueError
        output.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if output.parent.is_symlink():
            raise ValueError
        payload = render_phase718_enabled_template()
        output.write_bytes(payload)
        return output, sha256(payload).hexdigest()
    except Phase718EnabledTemplateError:
        raise
    except Exception:
        raise Phase718EnabledTemplateError(_GENERIC_ERROR) from None


def _apply_enabled_delta(
    template: dict[str, Any],
    authority: Mapping[str, Any],
    workflow: Mapping[str, Any],
) -> None:
    template["Description"] = (
        "Phase 7.18 enabled one-shot seller publication; Phase 6 resources remain externally "
        "referenced and unchanged"
    )
    parameters = cast(dict[str, Any], template["Parameters"])
    for name in (
        "CandidateCodeS3Bucket",
        "CandidateReleaseFingerprint",
        "CandidateCodeS3ObjectVersion",
    ):
        parameters.pop(name)
    parameters["ActivationMode"] = {
        "Type": "String",
        "AllowedValues": [ACTIVATION_MODE],
    }
    parameters.update(
        {
            "ApplicationReleaseFingerprint": {
                "Type": "String",
                "AllowedPattern": _FINGERPRINT_PATTERN,
            },
            "CanaryEvidenceFingerprint": {
                "Type": "String",
                "AllowedPattern": _FINGERPRINT_PATTERN,
            },
            "EnabledCodeS3Bucket": {
                "Type": "String",
                "AllowedPattern": _BUCKET_PATTERN,
            },
            "EnabledCodeS3ObjectVersion": {
                "Type": "String",
                "AllowedPattern": _OBJECT_VERSION_PATTERN,
            },
            "EnabledReleaseFingerprint": {
                "Type": "String",
                "AllowedPattern": _FINGERPRINT_PATTERN,
            },
            "EnablementEvidenceFingerprint": {
                "Type": "String",
                "AllowedPattern": _FINGERPRINT_PATTERN,
            },
            "PrintifySecretArn": {
                "Type": "String",
                "AllowedPattern": (
                    "^arn:(aws|aws-us-gov|aws-cn):secretsmanager:[a-z0-9-]+:[0-9]{12}:"
                    "secret:mr-lister/[A-Za-z0-9/_-]+-[A-Za-z0-9]{6}$"
                ),
            },
            "SellerHttpApiAuthorizerId": {
                "Type": "String",
                "AllowedPattern": _AUTHORIZER_ID_PATTERN,
            },
            "SellerHttpApiId": {
                "Type": "String",
                "AllowedPattern": _API_ID_PATTERN,
            },
        }
    )

    template["Conditions"] = {
        ENABLED_CONDITION: {"Fn::Equals": [{"Ref": "ActivationMode"}, ACTIVATION_MODE]}
    }
    resources = cast(dict[str, dict[str, Any]], template["Resources"])
    for resource in resources.values():
        resource["Condition"] = ENABLED_CONDITION

    common_environment = {
        "MR_LISTER_COGNITO_CLIENT_ID": {"Ref": "SellerUserPoolClientId"},
        "MR_LISTER_COGNITO_GROUP": "seller",
        "MR_LISTER_COGNITO_ISSUER": {
            "Fn::Sub": ("https://cognito-idp.${AWS::Region}.${AWS::URLSuffix}/${SellerUserPoolId}")
        },
        "MR_LISTER_COGNITO_SCOPE": "mr-lister-api/seller",
        "MR_LISTER_PHASE7_ACTIVATION_MODE": ACTIVATION_MODE,
        "MR_LISTER_PHASE7_CANARY_EVIDENCE_FINGERPRINT": {"Ref": "CanaryEvidenceFingerprint"},
        "MR_LISTER_PHASE7_CONTRACT_FINGERPRINT": CONTRACT_FINGERPRINT,
        "MR_LISTER_PHASE7_CONTRACT_VERSION": CONTRACT_VERSION,
        "MR_LISTER_PHASE7_DISPATCHER_ENABLED": "true",
        "MR_LISTER_PHASE7_ENABLED_RELEASE_FINGERPRINT": {"Ref": "EnabledReleaseFingerprint"},
        "MR_LISTER_PHASE7_ENABLEMENT_EVIDENCE_FINGERPRINT": {
            "Ref": "EnablementEvidenceFingerprint"
        },
        "MR_LISTER_PHASE7_PUBLICATION_ENABLED": "true",
        "MR_LISTER_PHASE7_QUERY_ENABLED": "true",
        "MR_LISTER_PHASE7_RECOVERY_ENABLED": "true",
        "MR_LISTER_PHASE7_REQUEST_ENABLED": "true",
        "MR_LISTER_PHASE7_RETENTION_ENABLED": "true",
        "MR_LISTER_PHASE7_SCAFFOLD_ONLY": "false",
        "MR_LISTER_PHASE7_WORKER_ENABLED": "true",
        "MR_LISTER_PRODUCT_PROFILE_FINGERPRINT": PROFILE_FINGERPRINT,
        "MR_LISTER_PRODUCT_PROFILE_ID": "gildan_64000_swiftpod",
        "MR_LISTER_PRODUCT_PROFILE_PATH": (
            "/var/task/config/product_profiles/gildan_64000_swiftpod.json"
        ),
        "MR_LISTER_PRODUCT_PROFILE_VERSION": "2",
        "MR_LISTER_RELEASE_FINGERPRINT": {"Ref": "ApplicationReleaseFingerprint"},
        "MR_LISTER_STATE_TABLE": {"Fn::Sub": "mr-lister-phase6-${EnvironmentName}"},
    }
    function_globals = cast(dict[str, Any], template["Globals"])["Function"]
    function_globals["ReservedConcurrentExecutions"] = 1
    function_globals["Environment"] = {"Variables": common_environment}
    function_globals["Tags"] = {"Project": "MrLister", "Phase": "7.18-enabled"}

    functions = _mapping(authority["functions"])
    code_uri = {
        "Bucket": {"Ref": "EnabledCodeS3Bucket"},
        "Key": {"Fn::Sub": ENABLED_ARCHIVE_KEY},
        "Version": {"Ref": "EnabledCodeS3ObjectVersion"},
    }
    for logical_id, raw_spec in functions.items():
        spec = _mapping(raw_spec)
        properties = resources[logical_id]["Properties"]
        properties["CodeUri"] = deepcopy(code_uri)
        properties["Handler"] = spec["handler"]
        properties["Timeout"] = spec["timeout"]

    resources["PublicationDispatcherFunction"]["Properties"]["Environment"] = {
        "Variables": {
            "MR_LISTER_PUBLICATION_RECOVERY_QUEUE_URL": {"Ref": "PublicationWorkflowRecoveryQueue"},
            "MR_LISTER_PUBLICATION_WORKFLOW_ARN": {"Ref": "PublicationWorkflowStateMachine"},
        }
    }
    resources["PublicationRecoveryFunction"]["Properties"]["Environment"] = {
        "Variables": {
            "MR_LISTER_PUBLICATION_WORKFLOW_ARN": {"Ref": "PublicationWorkflowStateMachine"}
        }
    }
    resources["PublicationWorkerFunction"]["Properties"]["Environment"] = {
        "Variables": {"MR_LISTER_PRINTIFY_SECRET_ARN": {"Ref": "PrintifySecretArn"}}
    }
    workflow_properties = resources["PublicationWorkflowStateMachine"]["Properties"]
    workflow_properties.pop("DefinitionUri")
    workflow_properties["Definition"] = deepcopy(workflow)

    worker_statements = resources["PublicationWorkerRole"]["Properties"]["Policies"][0][
        "PolicyDocument"
    ]["Statement"]
    worker_statements.append(
        {
            "Sid": "ReadExactPublicationCredential",
            "Effect": "Allow",
            "Action": "secretsmanager:GetSecretValue",
            "Resource": {"Ref": "PrintifySecretArn"},
        }
    )

    resources["PublicationDispatcherStreamMapping"]["Properties"]["Enabled"] = True
    resources["PublicationRetentionStreamMapping"]["Properties"]["Enabled"] = True
    resources["PublicationRecoveryFunction"]["Properties"]["Events"]["RecoveryQueue"]["Properties"][
        "Enabled"
    ] = True
    for name in _RULES:
        resources[name]["Properties"]["State"] = "ENABLED"

    _add_seller_routes(resources)
    template["Outputs"] = {
        "DeploymentReadiness": {"Value": "GENERAL_AVAILABILITY"},
        "EnabledReleaseFingerprint": {"Value": {"Ref": "EnabledReleaseFingerprint"}},
        "ProviderMutationEnabled": {"Value": "true"},
        "PublicationQueryRegistered": {"Value": "true"},
        "PublicationRequestRegistered": {"Value": "true"},
        "PublicationWorkerTriggered": {"Value": "true"},
        "SellerPublicationEnabled": {"Value": "true"},
    }


def _add_seller_routes(resources: dict[str, dict[str, Any]]) -> None:
    source_prefix = (
        "arn:${AWS::Partition}:execute-api:${AWS::Region}:${AWS::AccountId}:${SellerHttpApiId}/*"
    )
    for name, method, path, function in (
        (
            "PublicationQuery",
            "GET",
            "/v1/jobs/{job_id}/publication",
            "PublicationQueryFunction",
        ),
        (
            "PublicationRequest",
            "POST",
            "/v1/jobs/{job_id}/publish",
            "PublicationRequestFunction",
        ),
    ):
        integration = f"{name}Integration"
        resources[integration] = {
            "Type": "AWS::ApiGatewayV2::Integration",
            "Condition": ENABLED_CONDITION,
            "Properties": {
                "ApiId": {"Ref": "SellerHttpApiId"},
                "IntegrationMethod": "POST",
                "IntegrationType": "AWS_PROXY",
                "IntegrationUri": {
                    "Fn::Join": [
                        "",
                        [
                            "arn:",
                            {"Ref": "AWS::Partition"},
                            ":apigateway:",
                            {"Ref": "AWS::Region"},
                            ":lambda:path/2015-03-31/functions/",
                            {"Fn::GetAtt": [function, "Arn"]},
                            "/invocations",
                        ],
                    ]
                },
                "PayloadFormatVersion": "2.0",
                "TimeoutInMillis": 30000,
            },
        }
        resources[f"{name}Route"] = {
            "Type": "AWS::ApiGatewayV2::Route",
            "Condition": ENABLED_CONDITION,
            "Properties": {
                "ApiId": {"Ref": "SellerHttpApiId"},
                "AuthorizationScopes": ["mr-lister-api/seller"],
                "AuthorizationType": "JWT",
                "AuthorizerId": {"Ref": "SellerHttpApiAuthorizerId"},
                "RouteKey": f"{method} {path}",
                "Target": {"Fn::Join": ["/", ["integrations", {"Ref": integration}]]},
            },
        }
        resources[f"{name}InvokePermission"] = {
            "Type": "AWS::Lambda::Permission",
            "Condition": ENABLED_CONDITION,
            "Properties": {
                "Action": "lambda:InvokeFunction",
                "FunctionName": {"Ref": function},
                "Principal": "apigateway.amazonaws.com",
                "SourceAccount": {"Ref": "AWS::AccountId"},
                "SourceArn": {
                    "Fn::Sub": f"{source_prefix}/{method}{path.replace('{job_id}', '*')}"
                },
            },
        }


def _verify_authority(authority: Mapping[str, Any]) -> None:
    if (
        authority.get("format") != "mr-lister-phase7.18-enabled-topology-v1"
        or authority.get("activation_mode") != ACTIVATION_MODE
        or _mapping(authority.get("contract"))
        != {"version": CONTRACT_VERSION, "fingerprint": CONTRACT_FINGERPRINT}
        or _mapping(authority.get("predecessor"))
        != {
            "template": "infra/phase7/production-disabled-template.json",
            "sha256": BASE_TEMPLATE_SHA256,
        }
        or set(_mapping(authority.get("functions")))
        != {f"Publication{name}Function" for name in _FUNCTIONS}
        or authority.get("enabled_eventbridge_rules") != list(_RULES)
    ):
        raise ValueError


def _verify_enabled_template(
    base: Mapping[str, Any],
    enabled: Mapping[str, Any],
    authority: Mapping[str, Any],
) -> None:
    resources = _mapping(enabled.get("Resources"))
    if (
        enabled.get("Transform") != "AWS::Serverless-2016-10-31"
        or enabled.get("Conditions")
        != {ENABLED_CONDITION: {"Fn::Equals": [{"Ref": "ActivationMode"}, ACTIVATION_MODE]}}
        or any(_mapping(item).get("Condition") != ENABLED_CONDITION for item in resources.values())
    ):
        raise ValueError
    functions = {
        name: _mapping(resource)
        for name, resource in resources.items()
        if _mapping(resource).get("Type") == "AWS::Serverless::Function"
    }
    if set(functions) != {f"Publication{name}Function" for name in _FUNCTIONS}:
        raise ValueError
    global_function = _mapping(_mapping(enabled.get("Globals")).get("Function"))
    variables = _mapping(_mapping(global_function.get("Environment")).get("Variables"))
    if (
        global_function.get("ReservedConcurrentExecutions") != 1
        or variables.get("MR_LISTER_PHASE7_CONTRACT_VERSION") != CONTRACT_VERSION
        or variables.get("MR_LISTER_PHASE7_CONTRACT_FINGERPRINT") != CONTRACT_FINGERPRINT
        or variables.get("MR_LISTER_PHASE7_ACTIVATION_MODE") != ACTIVATION_MODE
        or any(
            variables.get(name) != "true"
            for name in (
                "MR_LISTER_PHASE7_QUERY_ENABLED",
                "MR_LISTER_PHASE7_REQUEST_ENABLED",
                "MR_LISTER_PHASE7_PUBLICATION_ENABLED",
                "MR_LISTER_PHASE7_WORKER_ENABLED",
                "MR_LISTER_PHASE7_DISPATCHER_ENABLED",
                "MR_LISTER_PHASE7_RECOVERY_ENABLED",
                "MR_LISTER_PHASE7_RETENTION_ENABLED",
            )
        )
        or variables.get("MR_LISTER_PHASE7_SCAFFOLD_ONLY") != "false"
    ):
        raise ValueError
    for logical_id, raw_spec in _mapping(authority["functions"]).items():
        properties = _mapping(_mapping(resources[logical_id]).get("Properties"))
        spec = _mapping(raw_spec)
        if properties.get("Handler") != spec.get("handler"):
            raise ValueError
    for name in _EXPLICIT_MAPPINGS:
        if _mapping(_mapping(resources[name]).get("Properties")).get("Enabled") is not True:
            raise ValueError
    workflow_properties = _mapping(
        _mapping(resources["PublicationWorkflowStateMachine"]).get("Properties")
    )
    if "DefinitionUri" in workflow_properties or workflow_properties.get("Definition") != _mapping(
        json.loads(_exact_file(WORKFLOW_DEFINITION))
    ):
        raise ValueError
    recovery_events = _mapping(
        _mapping(_mapping(resources["PublicationRecoveryFunction"]).get("Properties")).get("Events")
    )
    if _mapping(_mapping(recovery_events.get("RecoveryQueue")).get("Properties")).get(
        "Enabled"
    ) is not True or any(
        _mapping(_mapping(resources[name]).get("Properties")).get("State") != "ENABLED"
        for name in _RULES
    ):
        raise ValueError
    phase6_types = {
        "AWS::Cognito::UserPool",
        "AWS::Cognito::UserPoolClient",
        "AWS::DynamoDB::Table",
        "AWS::Serverless::HttpApi",
    }
    if any(_mapping(resource).get("Type") in phase6_types for resource in resources.values()):
        raise ValueError
    for prefix, function in (
        ("PublicationQuery", "PublicationQueryFunction"),
        ("PublicationRequest", "PublicationRequestFunction"),
    ):
        integration = _mapping(_mapping(resources[f"{prefix}Integration"]).get("Properties"))
        expected_uri = {
            "Fn::Join": [
                "",
                [
                    "arn:",
                    {"Ref": "AWS::Partition"},
                    ":apigateway:",
                    {"Ref": "AWS::Region"},
                    ":lambda:path/2015-03-31/functions/",
                    {"Fn::GetAtt": [function, "Arn"]},
                    "/invocations",
                ],
            ]
        }
        if (
            integration.get("IntegrationMethod") != "POST"
            or integration.get("IntegrationUri") != expected_uri
        ):
            raise ValueError
    worker = json.dumps(
        {
            "function": resources["PublicationWorkerFunction"],
            "role": resources["PublicationWorkerRole"],
        },
        sort_keys=True,
    )
    non_worker = json.dumps(
        {
            name: resource
            for name, resource in resources.items()
            if name not in {"PublicationWorkerFunction", "PublicationWorkerRole"}
        },
        sort_keys=True,
    )
    if (
        "MR_LISTER_PRINTIFY_SECRET_ARN" not in worker
        or "secretsmanager:GetSecretValue" not in worker
        or "MR_LISTER_PRINTIFY_SECRET_ARN" in non_worker
        or "secretsmanager:GetSecretValue" in non_worker
    ):
        raise ValueError
    outputs = _mapping(enabled.get("Outputs"))
    if any(
        _mapping(outputs.get(name)).get("Value") != "true"
        for name in (
            "ProviderMutationEnabled",
            "PublicationQueryRegistered",
            "PublicationRequestRegistered",
            "PublicationWorkerTriggered",
            "SellerPublicationEnabled",
        )
    ):
        raise ValueError
    base_resources = _mapping(base.get("Resources"))
    if not set(base_resources).issubset(resources):
        raise ValueError
    serialized = json.dumps(enabled, sort_keys=True).casefold()
    for forbidden in ("unpublish", "deleteproduct", "orders:", "fulfillment"):
        if forbidden in serialized:
            raise ValueError


def _mapping(value: object) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError
    return cast(Mapping[str, Any], value)


def _exact_file(path: Path) -> bytes:
    resolved = path.resolve(strict=True)
    if path.is_symlink() or not resolved.is_file() or not 1 <= resolved.stat().st_size <= 8 << 20:
        raise ValueError
    return resolved.read_bytes()


def _canonical(value: object) -> bytes:
    return (
        json.dumps(value, ensure_ascii=True, allow_nan=False, indent=2, sort_keys=False) + "\n"
    ).encode("utf-8")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--verify", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        if arguments.verify is not None:
            digest = verify_phase718_enabled_template(arguments.verify)
            result = {"mode": "verify", "sha256": digest, "status": "passed"}
        else:
            output, digest = write_phase718_enabled_template(arguments.output)
            result = {
                "mode": "render",
                "path": str(output),
                "sha256": digest,
                "status": "created",
            }
        print(json.dumps(result, sort_keys=True))
        return 0
    except Phase718EnabledTemplateError:
        print(json.dumps({"status": "refused"}, sort_keys=True))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "ACTIVATION_MODE",
    "BASE_TEMPLATE",
    "BASE_TEMPLATE_SHA256",
    "CONTRACT_FINGERPRINT",
    "CONTRACT_VERSION",
    "DEFAULT_OUTPUT",
    "ENABLED_ARCHIVE_KEY",
    "ENABLED_CONDITION",
    "Phase718EnabledTemplateError",
    "render_phase718_enabled_template",
    "verify_phase718_enabled_template",
    "write_phase718_enabled_template",
]
