from __future__ import annotations

import json
from copy import deepcopy
from hashlib import sha256
from pathlib import Path

import pytest

from tools.render_phase718_enabled_template import (
    BASE_TEMPLATE,
    BASE_TEMPLATE_SHA256,
    CONTRACT_FINGERPRINT,
    ENABLED_ARCHIVE_KEY,
    Phase718EnabledTemplateError,
    render_phase718_enabled_template,
    verify_phase718_enabled_template,
    write_phase718_enabled_template,
)


def _template() -> dict[str, object]:
    return json.loads(render_phase718_enabled_template())


def test_enabled_topology_preserves_base_inventory_and_references_phase6() -> None:
    base = json.loads(BASE_TEMPLATE.read_bytes())
    enabled = _template()
    base_resources = base["Resources"]
    resources = enabled["Resources"]

    assert sha256(BASE_TEMPLATE.read_bytes()).hexdigest() == BASE_TEMPLATE_SHA256
    assert set(base_resources).issubset(resources)
    assert {
        name: resource["Type"] for name, resource in resources.items() if name in base_resources
    } == {name: resource["Type"] for name, resource in base_resources.items()}
    assert set(resources) - set(base_resources) == {
        "PublicationQueryIntegration",
        "PublicationQueryInvokePermission",
        "PublicationQueryRoute",
        "PublicationRequestIntegration",
        "PublicationRequestInvokePermission",
        "PublicationRequestRoute",
    }
    assert [
        resource["Type"] for name, resource in resources.items() if name not in base_resources
    ].count("AWS::ApiGatewayV2::Integration") == 2
    assert [
        resource["Type"] for name, resource in resources.items() if name not in base_resources
    ].count("AWS::ApiGatewayV2::Route") == 2
    assert [
        resource["Type"] for name, resource in resources.items() if name not in base_resources
    ].count("AWS::Lambda::Permission") == 2
    assert not {
        "AWS::Cognito::UserPool",
        "AWS::Cognito::UserPoolClient",
        "AWS::DynamoDB::Table",
        "AWS::Serverless::HttpApi",
        "AWS::CloudFront::Distribution",
    }.intersection(resource["Type"] for resource in resources.values())
    assert "ApplicationOrigin" not in enabled["Parameters"]
    assert "SellerHttpApiId" in enabled["Parameters"]
    assert "SellerHttpApiAuthorizerId" in enabled["Parameters"]


def test_six_role_separated_functions_use_one_enabled_immutable_artifact() -> None:
    enabled = _template()
    resources = enabled["Resources"]
    function_globals = enabled["Globals"]["Function"]
    assert function_globals["ReservedConcurrentExecutions"] == 1
    assert function_globals["Environment"]["Variables"] == {
        "MR_LISTER_COGNITO_CLIENT_ID": {"Ref": "SellerUserPoolClientId"},
        "MR_LISTER_COGNITO_GROUP": "seller",
        "MR_LISTER_COGNITO_ISSUER": {
            "Fn::Sub": ("https://cognito-idp.${AWS::Region}.${AWS::URLSuffix}/${SellerUserPoolId}")
        },
        "MR_LISTER_COGNITO_SCOPE": "mr-lister-api/seller",
        "MR_LISTER_PHASE7_ACTIVATION_MODE": "GENERAL_AVAILABILITY",
        "MR_LISTER_PHASE7_CANARY_EVIDENCE_FINGERPRINT": {"Ref": "CanaryEvidenceFingerprint"},
        "MR_LISTER_PHASE7_CONTRACT_FINGERPRINT": CONTRACT_FINGERPRINT,
        "MR_LISTER_PHASE7_CONTRACT_VERSION": "7.1.0",
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
        "MR_LISTER_PRODUCT_PROFILE_FINGERPRINT": (
            "5de1257141cfdacb1731f68bb9113712957483b33d5b0f7115afdba86eb7476c"
        ),
        "MR_LISTER_PRODUCT_PROFILE_ID": "gildan_64000_swiftpod",
        "MR_LISTER_PRODUCT_PROFILE_PATH": (
            "/var/task/config/product_profiles/gildan_64000_swiftpod.json"
        ),
        "MR_LISTER_PRODUCT_PROFILE_VERSION": "2",
        "MR_LISTER_RELEASE_FINGERPRINT": {"Ref": "ApplicationReleaseFingerprint"},
        "MR_LISTER_STATE_TABLE": {"Fn::Sub": "mr-lister-phase6-${EnvironmentName}"},
    }
    expected = {
        "Query": "publication_query_handler",
        "Request": "publication_request_handler",
        "Dispatcher": "publication_dispatcher_handler",
        "Worker": "publication_worker_handler",
        "Recovery": "publication_recovery_handler",
        "Retention": "publication_retention_handler",
    }
    functions = {
        name: resource
        for name, resource in resources.items()
        if resource["Type"] == "AWS::Serverless::Function"
    }
    assert set(functions) == {f"Publication{name}Function" for name in expected}
    for name, handler in expected.items():
        properties = functions[f"Publication{name}Function"]["Properties"]
        assert properties["Handler"] == f"mr_lister.cloud.phase718_entrypoints.{handler}"
        assert properties["CodeUri"] == {
            "Bucket": {"Ref": "EnabledCodeS3Bucket"},
            "Key": {"Fn::Sub": ENABLED_ARCHIVE_KEY},
            "Version": {"Ref": "EnabledCodeS3ObjectVersion"},
        }


def test_printify_secret_is_worker_only_and_transaction_iam_is_preserved() -> None:
    base = json.loads(BASE_TEMPLATE.read_bytes())
    enabled = _template()
    resources = enabled["Resources"]
    serialized_by_resource = {
        name: json.dumps(resource, sort_keys=True) for name, resource in resources.items()
    }
    secret_holders = {
        name
        for name, serialized in serialized_by_resource.items()
        if "PrintifySecretArn" in serialized or "secretsmanager:GetSecretValue" in serialized
    }
    assert secret_holders == {"PublicationWorkerFunction", "PublicationWorkerRole"}

    for role_name in (
        "PublicationQueryRole",
        "PublicationRequestRole",
        "PublicationDispatcherRole",
        "PublicationRecoveryRole",
        "PublicationRetentionRole",
        "PublicationWorkflowRole",
    ):
        expected = deepcopy(base["Resources"][role_name])
        expected["Condition"] = "EnableGeneralAvailability"
        assert resources[role_name] == expected

    worker_statements = resources["PublicationWorkerRole"]["Properties"]["Policies"][0][
        "PolicyDocument"
    ]["Statement"]
    base_worker_statements = base["Resources"]["PublicationWorkerRole"]["Properties"]["Policies"][
        0
    ]["PolicyDocument"]["Statement"]
    assert worker_statements[:-1] == base_worker_statements
    assert worker_statements[-1] == {
        "Sid": "ReadExactPublicationCredential",
        "Effect": "Allow",
        "Action": "secretsmanager:GetSecretValue",
        "Resource": {"Ref": "PrintifySecretArn"},
    }

    all_actions = [
        action
        for name, resource in resources.items()
        if resource["Type"] == "AWS::IAM::Role"
        for statement in resource["Properties"]
        .get("Policies", [{}])[0]
        .get("PolicyDocument", {})
        .get("Statement", [])
        for action in (
            statement["Action"] if isinstance(statement["Action"], list) else [statement["Action"]]
        )
    ]
    assert "dynamodb:TransactWriteItems" not in all_actions
    for role_name, sid in (
        ("PublicationRequestRole", "CommitRequestPuts"),
        ("PublicationWorkerRole", "CommitPublicationPuts"),
        ("PublicationRecoveryRole", "SettleRecoveryPuts"),
        ("PublicationRetentionRole", "CommitTerminalRetention"),
    ):
        statements = resources[role_name]["Properties"]["Policies"][0]["PolicyDocument"][
            "Statement"
        ]
        statement = next(item for item in statements if item["Sid"] == sid)
        assert statement["Condition"]["ForAnyValue:StringEquals"] == {
            "dynamodb:EnclosingOperation": ["TransactWriteItems"]
        }


def test_routes_are_jwt_authenticated_and_trigger_topology_is_enabled() -> None:
    resources = _template()["Resources"]
    cases = {
        "PublicationQuery": (
            "GET /v1/jobs/{job_id}/publication",
            "arn:${AWS::Partition}:execute-api:${AWS::Region}:${AWS::AccountId}:"
            "${SellerHttpApiId}/*/GET/v1/jobs/*/publication",
        ),
        "PublicationRequest": (
            "POST /v1/jobs/{job_id}/publish",
            "arn:${AWS::Partition}:execute-api:${AWS::Region}:${AWS::AccountId}:"
            "${SellerHttpApiId}/*/POST/v1/jobs/*/publish",
        ),
    }
    for prefix, (route_key, source_arn) in cases.items():
        function = f"{prefix}Function"
        integration = resources[f"{prefix}Integration"]["Properties"]
        assert integration["IntegrationMethod"] == "POST"
        assert integration["IntegrationUri"] == {
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
        route = resources[f"{prefix}Route"]["Properties"]
        assert route["ApiId"] == {"Ref": "SellerHttpApiId"}
        assert route["AuthorizationType"] == "JWT"
        assert route["AuthorizationScopes"] == ["mr-lister-api/seller"]
        assert route["AuthorizerId"] == {"Ref": "SellerHttpApiAuthorizerId"}
        assert route["RouteKey"] == route_key
        permission = resources[f"{prefix}InvokePermission"]["Properties"]
        assert permission["SourceAccount"] == {"Ref": "AWS::AccountId"}
        assert permission["SourceArn"] == {"Fn::Sub": source_arn}

    assert resources["PublicationDispatcherStreamMapping"]["Properties"]["Enabled"] is True
    assert resources["PublicationRetentionStreamMapping"]["Properties"]["Enabled"] is True
    recovery = resources["PublicationRecoveryFunction"]["Properties"]["Events"]
    assert recovery["RecoveryQueue"]["Properties"]["Enabled"] is True
    for name in (
        "PublicationDueWorkSweepRule",
        "PublicationRecoverySweepRule",
        "PublicationWorkflowFailureRule",
    ):
        assert resources[name]["Properties"]["State"] == "ENABLED"

    workflow = resources["PublicationWorkflowStateMachine"]["Properties"]
    assert "DefinitionUri" not in workflow
    assert workflow["Definition"] == json.loads(
        (BASE_TEMPLATE.parent / "statemachine/publication.asl.json").read_bytes()
    )


def test_renderer_refuses_drift_and_never_overwrites(tmp_path: Path) -> None:
    output = tmp_path / "enabled-template.json"
    created, fingerprint = write_phase718_enabled_template(output)
    assert created == output.resolve()
    assert verify_phase718_enabled_template(output) == fingerprint

    changed = json.loads(output.read_bytes())
    changed["Outputs"]["ProviderMutationEnabled"]["Value"] = "false"
    output.write_text(json.dumps(changed), encoding="utf-8")
    with pytest.raises(Phase718EnabledTemplateError):
        verify_phase718_enabled_template(output)

    output.write_text("keep", encoding="utf-8")
    with pytest.raises(Phase718EnabledTemplateError):
        write_phase718_enabled_template(output)
    assert output.read_text(encoding="utf-8") == "keep"
