from __future__ import annotations

import base64
import json
from copy import deepcopy
from hashlib import sha256
from pathlib import Path

import pytest

import tools.build_phase718_enabled_release as builder
import tools.verify_phase718_enabled_deployment as deployment_verifier
from mr_lister.release.phase6 import DEPENDENCY_ARTIFACT_FILENAME, render_manifest
from tools.build_phase718_enabled_release import (
    ENABLED_ARTIFACT_DIRECTORY_NAME,
    ENABLED_DEPENDENCY_DIRECTORY_NAME,
    ENABLED_DEPLOYMENT_DIRECTORY_NAME,
    ENABLED_SOURCE_DIRECTORY_NAME,
    build_enabled_source_bundle,
    seal_enabled_release,
)
from tools.render_phase718_enabled_template import (
    BASE_TEMPLATE,
    CONTRACT_FINGERPRINT,
    PROFILE_FINGERPRINT,
    WORKFLOW_DEFINITION,
    render_phase718_enabled_template,
)
from tools.verify_phase718_enabled_deployment import (
    ACCOUNT_ID,
    REGION,
    STACK_NAME,
    Phase718EnabledDeploymentError,
    verify_change_set_observation,
    verify_enabled_deployment_readback,
    verify_predecessor_rollback_readback,
)

APPLICATION_RELEASE = "a" * 64
CANARY_EVIDENCE = "b" * 64
ENABLEMENT_EVIDENCE = "c" * 64


def _artifact(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):  # type: ignore[no-untyped-def]
    source = build_enabled_source_bundle(
        tmp_path / "source" / ENABLED_SOURCE_DIRECTORY_NAME,
        application_release_fingerprint=APPLICATION_RELEASE,
        canary_evidence_fingerprint=CANARY_EVIDENCE,
        enablement_evidence_fingerprint=ENABLEMENT_EVIDENCE,
        state_table="mr-lister-phase6-dev",
    )
    dependencies = tmp_path / "dependencies" / ENABLED_DEPENDENCY_DIRECTORY_NAME
    dependencies.mkdir(parents=True)
    (dependencies / "synthetic.py").write_text("VALUE = 1\n", encoding="utf-8")
    (dependencies / DEPENDENCY_ARTIFACT_FILENAME).write_bytes(
        render_manifest({"format": "synthetic-phase718-test-v1"})
    )
    monkeypatch.setattr(builder, "verify_linux_arm64_dependency_artifact", lambda *_a, **_k: None)
    return seal_enabled_release(
        source,
        dependencies=dependencies,
        deployment_destination=tmp_path / "sealed" / ENABLED_DEPLOYMENT_DIRECTORY_NAME,
        artifact_destination=tmp_path / "sealed" / ENABLED_ARTIFACT_DIRECTORY_NAME,
    )


def _parameters(artifact) -> dict[str, str]:  # type: ignore[no-untyped-def]
    return {
        "ActivationMode": "GENERAL_AVAILABILITY",
        "ApplicationReleaseFingerprint": APPLICATION_RELEASE,
        "CanaryEvidenceFingerprint": CANARY_EVIDENCE,
        "EnabledCodeS3Bucket": "mr-lister-phase6-artifacts-dev-384627057108-us-west-2",
        "EnabledCodeS3ObjectVersion": "enabled-version-1",
        "EnabledReleaseFingerprint": artifact.release_fingerprint,
        "EnablementEvidenceFingerprint": ENABLEMENT_EVIDENCE,
        "EnvironmentName": "dev",
        "PrintifySecretArn": (
            f"arn:aws:secretsmanager:{REGION}:{ACCOUNT_ID}:secret:mr-lister/printify-AbCd12"
        ),
        "SellerHttpApiAuthorizerId": "authorizer123",
        "SellerHttpApiId": "a1b2c3d4e5",
        "SellerUserPoolClientId": "client123",
        "SellerUserPoolId": "us-west-2_pool123",
        "StateTableArn": f"arn:aws:dynamodb:{REGION}:{ACCOUNT_ID}:table/mr-lister-phase6-dev",
        "StateTableStreamArn": (
            f"arn:aws:dynamodb:{REGION}:{ACCOUNT_ID}:table/mr-lister-phase6-dev/"
            "stream/2026-09-03T00:00:00.000"
        ),
    }


def _stack(name: str, *, status: str, parameters: dict[str, str], outputs: dict[str, str]):
    return {
        "Stacks": [
            {
                "CreationTime": "2026-08-01T00:00:00+00:00",
                "EnableTerminationProtection": False,
                "LastUpdatedTime": "2026-09-03T00:00:00+00:00",
                "Outputs": [
                    {"OutputKey": key, "OutputValue": value} for key, value in outputs.items()
                ],
                "Parameters": [
                    {"ParameterKey": key, "ParameterValue": value}
                    for key, value in parameters.items()
                ],
                "StackId": f"arn:aws:cloudformation:{REGION}:{ACCOUNT_ID}:stack/{name}/id",
                "StackName": name,
                "StackStatus": status,
                "Tags": [{"Key": "Project", "Value": "MrLister"}],
            }
        ]
    }


def _phase6_stack() -> dict[str, object]:
    return _stack(
        "mr-lister-phase6-dev",
        status="UPDATE_COMPLETE",
        parameters={
            "EnvironmentName": "dev",
            "PrintifySecretArn": (
                f"arn:aws:secretsmanager:{REGION}:{ACCOUNT_ID}:secret:mr-lister/printify-AbCd12"
            ),
            "ReleaseFingerprint": APPLICATION_RELEASE,
        },
        outputs={
            "ArtifactBucketName": "mr-lister-phase6-artifacts-dev-384627057108-us-west-2",
            "SellerApiOrigin": "https://a1b2c3d4e5.execute-api.us-west-2.amazonaws.com",
            "SellerUserPoolClientId": "client123",
            "SellerUserPoolId": "us-west-2_pool123",
            "StateTableArn": (f"arn:aws:dynamodb:{REGION}:{ACCOUNT_ID}:table/mr-lister-phase6-dev"),
            "StateTableName": "mr-lister-phase6-dev",
        },
    )


def _phase6_before_capture(parameters: dict[str, str]) -> dict[str, object]:
    return {
        "api": {
            "authorizer": {
                "AuthorizerId": parameters["SellerHttpApiAuthorizerId"],
                "AuthorizerType": "JWT",
                "IdentitySource": ["$request.header.Authorization"],
                "JwtConfiguration": {
                    "Audience": [parameters["SellerUserPoolClientId"]],
                    "Issuer": (
                        f"https://cognito-idp.{REGION}.amazonaws.com/"
                        f"{parameters['SellerUserPoolId']}"
                    ),
                },
            },
            "configuration": {
                "ApiId": parameters["SellerHttpApiId"],
                "ProtocolType": "HTTP",
            },
        },
        "stack": _phase6_stack(),
        "table": {
            "Table": {
                "LatestStreamArn": parameters["StateTableStreamArn"],
                "StreamSpecification": {
                    "StreamEnabled": True,
                    "StreamViewType": "KEYS_ONLY",
                },
                "TableArn": parameters["StateTableArn"],
                "TableName": "mr-lister-phase6-dev",
                "TableStatus": "ACTIVE",
            }
        },
    }


def _s3_head(artifact) -> dict[str, object]:  # type: ignore[no-untyped-def]
    return {
        "ChecksumSHA256": base64.b64encode(
            sha256(artifact.archive_path.read_bytes()).digest()
        ).decode("ascii"),
        "ContentLength": artifact.archive_path.stat().st_size,
        "ContentType": "application/zip",
        "DeleteMarker": False,
        "Metadata": {
            "mr-lister-archive-sha256": artifact.archive_fingerprint,
            "mr-lister-release-fingerprint": artifact.release_fingerprint,
        },
        "ServerSideEncryption": "AES256",
        "VersionId": "enabled-version-1",
    }


def _rule_observations(names: dict[str, str], *, state: str) -> dict[str, object]:
    lambda_prefix = f"arn:aws:lambda:{REGION}:{ACCOUNT_ID}:function:mr-lister-phase7-dev-"
    sqs_prefix = f"arn:aws:sqs:{REGION}:{ACCOUNT_ID}:mr-lister-phase7-dev-"
    workflow_arn = (
        f"arn:aws:states:{REGION}:{ACCOUNT_ID}:stateMachine:mr-lister-phase7-dev-publication"
    )
    dlq = f"{sqs_prefix}publication-operations-dlq"
    retry = {"MaximumEventAgeInSeconds": 3600, "MaximumRetryAttempts": 2}
    return {
        "PublicationDueWorkSweepRule": {
            "rule": {
                "Name": names["PublicationDueWorkSweepRule"],
                "ScheduleExpression": "rate(1 minute)",
                "State": state,
            },
            "targets": {
                "Targets": [
                    {
                        "Arn": f"{lambda_prefix}publication-dispatcher",
                        "DeadLetterConfig": {"Arn": dlq},
                        "Id": "PublicationDispatcher",
                        "Input": '{"kind":"publication_due_sweep"}',
                        "RetryPolicy": retry,
                    }
                ]
            },
        },
        "PublicationRecoverySweepRule": {
            "rule": {
                "Name": names["PublicationRecoverySweepRule"],
                "ScheduleExpression": "rate(1 minute)",
                "State": state,
            },
            "targets": {
                "Targets": [
                    {
                        "Arn": f"{lambda_prefix}publication-recovery",
                        "DeadLetterConfig": {"Arn": dlq},
                        "Id": "PublicationRecovery",
                        "Input": '{"kind":"publication_recovery_sweep"}',
                        "RetryPolicy": retry,
                    }
                ]
            },
        },
        "PublicationWorkflowFailureRule": {
            "rule": {
                "EventPattern": json.dumps(
                    {
                        "detail": {
                            "stateMachineArn": [workflow_arn],
                            "status": ["FAILED", "TIMED_OUT", "ABORTED"],
                        },
                        "detail-type": ["Step Functions Execution Status Change"],
                        "source": ["aws.states"],
                    }
                ),
                "Name": names["PublicationWorkflowFailureRule"],
                "State": state,
            },
            "targets": {
                "Targets": [
                    {
                        "Arn": f"{sqs_prefix}publication-recovery",
                        "DeadLetterConfig": {"Arn": dlq},
                        "Id": "PublicationRecoveryQueue",
                        "InputTransformer": {
                            "InputPathsMap": {
                                "execution_arn": "$.detail.executionArn",
                                "machine_arn": "$.detail.stateMachineArn",
                                "status": "$.detail.status",
                            },
                            "InputTemplate": (
                                '{"execution_arn":<execution_arn>,"machine_arn":<machine_arn>,'
                                '"status":<status>}'
                            ),
                        },
                        "RetryPolicy": retry,
                    }
                ]
            },
        },
    }


def _processed_predecessor() -> dict[str, object]:
    source = json.loads(BASE_TEMPLATE.read_bytes())
    processed = deepcopy(source)
    processed.pop("Transform")
    globals_properties = processed.pop("Globals")["Function"]
    for resource in processed["Resources"].values():
        if resource["Type"] == "AWS::Serverless::Function":
            resource["Type"] = "AWS::Lambda::Function"
            properties = {**deepcopy(globals_properties), **resource["Properties"]}
            global_variables = globals_properties["Environment"]["Variables"]
            local_variables = resource["Properties"].get("Environment", {}).get("Variables", {})
            properties["Environment"] = {"Variables": {**global_variables, **local_variables}}
            code_uri = properties.pop("CodeUri")
            properties["Code"] = {
                "S3Bucket": code_uri["Bucket"],
                "S3Key": code_uri["Key"],
                "S3ObjectVersion": code_uri["Version"],
            }
            properties.pop("Events", None)
            resource["Properties"] = properties
        elif resource["Type"] == "AWS::Serverless::StateMachine":
            resource["Type"] = "AWS::StepFunctions::StateMachine"
            resource["Properties"].pop("DefinitionUri")
            resource["Properties"]["DefinitionS3Location"] = {
                "Bucket": "mr-lister-phase6-artifacts-dev-384627057108-us-west-2",
                "Key": "phase7/sam/frozen-definition",
                "Version": "definition-version-1",
            }
    recovery = source["Resources"]["PublicationRecoveryFunction"]["Properties"]["Events"][
        "RecoveryQueue"
    ]["Properties"]
    processed["Resources"]["PublicationRecoveryFunctionRecoveryQueue"] = {
        "Condition": "InstantiateProductionCandidate",
        "Properties": {
            "BatchSize": recovery["BatchSize"],
            "Enabled": recovery["Enabled"],
            "EventSourceArn": recovery["Queue"],
            "FunctionName": {"Ref": "PublicationRecoveryFunction"},
            "FunctionResponseTypes": recovery["FunctionResponseTypes"],
        },
        "Type": "AWS::Lambda::EventSourceMapping",
    }
    return processed


def _enabled_observation(artifact) -> dict[str, object]:  # type: ignore[no-untyped-def]
    parameters = _parameters(artifact)
    archive_code_sha = base64.b64encode(bytes.fromhex(artifact.archive_fingerprint)).decode()
    common = {
        "MR_LISTER_COGNITO_CLIENT_ID": "client123",
        "MR_LISTER_COGNITO_GROUP": "seller",
        "MR_LISTER_COGNITO_ISSUER": (
            "https://cognito-idp.us-west-2.amazonaws.com/us-west-2_pool123"
        ),
        "MR_LISTER_COGNITO_SCOPE": "mr-lister-api/seller",
        "MR_LISTER_PHASE7_ACTIVATION_MODE": "GENERAL_AVAILABILITY",
        "MR_LISTER_PHASE7_CANARY_EVIDENCE_FINGERPRINT": CANARY_EVIDENCE,
        "MR_LISTER_PHASE7_CONTRACT_FINGERPRINT": CONTRACT_FINGERPRINT,
        "MR_LISTER_PHASE7_CONTRACT_VERSION": "7.1.0",
        "MR_LISTER_PHASE7_DISPATCHER_ENABLED": "true",
        "MR_LISTER_PHASE7_ENABLED_RELEASE_FINGERPRINT": artifact.release_fingerprint,
        "MR_LISTER_PHASE7_ENABLEMENT_EVIDENCE_FINGERPRINT": ENABLEMENT_EVIDENCE,
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
        "MR_LISTER_RELEASE_FINGERPRINT": APPLICATION_RELEASE,
        "MR_LISTER_STATE_TABLE": "mr-lister-phase6-dev",
    }
    workflow_arn = (
        f"arn:aws:states:{REGION}:{ACCOUNT_ID}:stateMachine:mr-lister-phase7-dev-publication"
    )
    handlers = {
        "Query": "publication_query_handler",
        "Request": "publication_request_handler",
        "Dispatcher": "publication_dispatcher_handler",
        "Worker": "publication_worker_handler",
        "Recovery": "publication_recovery_handler",
        "Retention": "publication_retention_handler",
    }
    timeouts = {
        "Query": 10,
        "Request": 15,
        "Dispatcher": 30,
        "Worker": 60,
        "Recovery": 60,
        "Retention": 60,
    }
    configurations = {}
    for component, handler in handlers.items():
        variables = dict(common)
        if component == "Dispatcher":
            variables.update(
                {
                    "MR_LISTER_PUBLICATION_RECOVERY_QUEUE_URL": (
                        "https://sqs.us-west-2.amazonaws.com/384627057108/"
                        "mr-lister-phase7-dev-publication-recovery"
                    ),
                    "MR_LISTER_PUBLICATION_WORKFLOW_ARN": workflow_arn,
                }
            )
        if component == "Recovery":
            variables["MR_LISTER_PUBLICATION_WORKFLOW_ARN"] = workflow_arn
        if component == "Worker":
            variables["MR_LISTER_PRINTIFY_SECRET_ARN"] = parameters["PrintifySecretArn"]
        configurations[component] = {
            "Architectures": ["arm64"],
            "CodeSha256": archive_code_sha,
            "Environment": {"Variables": variables},
            "FunctionName": f"mr-lister-phase7-dev-publication-{component.casefold()}",
            "Handler": f"mr_lister.cloud.phase718_entrypoints.{handler}",
            "LastUpdateStatus": "Successful",
            "MemorySize": 512,
            "Role": (
                f"arn:aws:iam::{ACCOUNT_ID}:role/"
                f"mr-lister-phase7-dev-publication-{component.casefold()}-role"
            ),
            "Runtime": "python3.12",
            "State": "Active",
            "Timeout": timeouts[component],
        }
    dlq_arn = f"arn:aws:sqs:{REGION}:{ACCOUNT_ID}:mr-lister-phase7-dev-publication-operations-dlq"
    mappings = {}
    for component in ("Dispatcher", "Recovery", "Retention"):
        mapping = {
            "BatchSize": 1 if component != "Dispatcher" else 25,
            "EventSourceArn": (
                f"arn:aws:sqs:{REGION}:{ACCOUNT_ID}:mr-lister-phase7-dev-publication-recovery"
                if component == "Recovery"
                else parameters["StateTableStreamArn"]
            ),
            "FunctionArn": (
                f"arn:aws:lambda:{REGION}:{ACCOUNT_ID}:function:"
                f"mr-lister-phase7-dev-publication-{component.casefold()}"
            ),
            "State": "Enabled",
        }
        if component == "Recovery":
            mapping["FunctionResponseTypes"] = ["ReportBatchItemFailures"]
        else:
            mapping.update(
                {
                    "BisectBatchOnFunctionError": True,
                    "DestinationConfig": {"OnFailure": {"Destination": dlq_arn}},
                    "MaximumBatchingWindowInSeconds": 1 if component == "Dispatcher" else 0,
                    "MaximumRecordAgeInSeconds": 300,
                    "MaximumRetryAttempts": 2,
                    "StartingPosition": "LATEST",
                }
            )
            mapping["FilterCriteria"] = {
                "Filters": [
                    {
                        "Pattern": (
                            '{"eventName":["INSERT","MODIFY"],"dynamodb":{"Keys":'
                            '{"SK":{"S":[{"prefix":"PUBLICATION_WORK#"}]}}}}'
                            if component == "Dispatcher"
                            else (
                                '{"eventName":["INSERT"],"dynamodb":{"Keys":{"PK":{"S":'
                                '[{"prefix":"PUBLICATION#"}]},"SK":{"S":'
                                '["TERMINAL_JOB_LINK"]}},"StreamViewType":["KEYS_ONLY"]}}'
                            )
                        )
                    }
                ]
            }
        mappings[component] = {"EventSourceMappings": [mapping]}
    rule_names = {
        "PublicationDueWorkSweepRule": "mr-lister-phase7-dev-publication-due-sweep",
        "PublicationRecoverySweepRule": "mr-lister-phase7-dev-publication-recovery-sweep",
        "PublicationWorkflowFailureRule": "mr-lister-phase7-dev-publication-workflow-failure",
    }
    integrations = []
    routes = []
    policies = {}
    for component, route_key, method, path in (
        ("Query", "GET /v1/jobs/{job_id}/publication", "GET", "/v1/jobs/*/publication"),
        ("Request", "POST /v1/jobs/{job_id}/publish", "POST", "/v1/jobs/*/publish"),
    ):
        integration_id = f"integration-{component.casefold()}"
        function_arn = (
            f"arn:aws:lambda:{REGION}:{ACCOUNT_ID}:function:"
            f"mr-lister-phase7-dev-publication-{component.casefold()}"
        )
        integrations.append(
            {
                "IntegrationId": integration_id,
                "IntegrationMethod": "POST",
                "IntegrationType": "AWS_PROXY",
                "IntegrationUri": (
                    f"arn:aws:apigateway:{REGION}:lambda:path/2015-03-31/functions/"
                    f"{function_arn}/invocations"
                ),
                "PayloadFormatVersion": "2.0",
            }
        )
        routes.append(
            {
                "AuthorizationScopes": ["mr-lister-api/seller"],
                "AuthorizationType": "JWT",
                "AuthorizerId": "authorizer123",
                "RouteKey": route_key,
                "Target": f"integrations/{integration_id}",
            }
        )
        source_arn = f"arn:aws:execute-api:{REGION}:{ACCOUNT_ID}:a1b2c3d4e5/*/{method}{path}"
        policies[component] = {
            "Policy": json.dumps(
                {
                    "Statement": [
                        {
                            "Action": "lambda:InvokeFunction",
                            "Condition": {
                                "ArnLike": {"AWS:SourceArn": source_arn},
                                "StringEquals": {"AWS:SourceAccount": ACCOUNT_ID},
                            },
                            "Effect": "Allow",
                            "Principal": {"Service": "apigateway.amazonaws.com"},
                            "Resource": function_arn,
                        }
                    ]
                }
            )
        }
    execution_role_policies = {}
    policy_names = {
        "Query": "ReadOnlyPublicationProjection",
        "Request": "AtomicPublicationRequest",
        "Dispatcher": "DispatchExactPublicationWork",
        "Worker": "OneStepPublicationWorker",
        "Recovery": "SameExecutionPublicationRecovery",
        "Retention": "TerminalPublicationRetention",
    }
    for component, policy_name in policy_names.items():
        role_name = f"mr-lister-phase7-dev-publication-{component.casefold()}-role"
        execution_role_policies[component] = {
            "attached_policies": {"AttachedPolicies": []},
            "inline_policy": {
                "PolicyDocument": deployment_verifier._expected_execution_policy(
                    component,
                    parameters=parameters,
                ),
                "PolicyName": policy_name,
                "RoleName": role_name,
            },
            "inline_policy_names": {"PolicyNames": [policy_name]},
        }
    return {
        "api": {
            "authorizer": {
                "AuthorizerId": "authorizer123",
                "AuthorizerType": "JWT",
                "IdentitySource": ["$request.header.Authorization"],
                "JwtConfiguration": {
                    "Audience": ["client123"],
                    "Issuer": ("https://cognito-idp.us-west-2.amazonaws.com/us-west-2_pool123"),
                },
            },
            "configuration": {"ApiId": "a1b2c3d4e5", "ProtocolType": "HTTP"},
            "integrations": {"Items": integrations},
            "routes": {"Items": routes},
        },
        "event_rules": _rule_observations(rule_names, state="ENABLED"),
        "event_source_mappings": mappings,
        "execution_role_policies": execution_role_policies,
        "lambda_concurrency": {
            component: {"ReservedConcurrentExecutions": 1} for component in handlers
        },
        "lambda_configurations": configurations,
        "lambda_policies": policies,
        "phase6_after": _phase6_stack(),
        "phase6_before": _phase6_stack(),
        "s3_head": _s3_head(artifact),
        "stack": _stack(
            STACK_NAME,
            status="UPDATE_COMPLETE",
            parameters=parameters,
            outputs={
                "DeploymentReadiness": "GENERAL_AVAILABILITY",
                "EnabledReleaseFingerprint": artifact.release_fingerprint,
                "ProviderMutationEnabled": "true",
                "PublicationQueryRegistered": "true",
                "PublicationRequestRegistered": "true",
                "PublicationWorkerTriggered": "true",
                "SellerPublicationEnabled": "true",
            },
        ),
        "state_machine": {
            "definition": WORKFLOW_DEFINITION.read_text(encoding="utf-8").replace(
                "${PublicationWorkerFunctionArn}",
                (
                    f"arn:aws:lambda:{REGION}:{ACCOUNT_ID}:function:"
                    "mr-lister-phase7-dev-publication-worker"
                ),
            ),
            "name": "mr-lister-phase7-dev-publication",
            "status": "ACTIVE",
            "type": "STANDARD",
        },
    }


def test_change_set_is_exact_source_bound_and_non_destructive(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    artifact = _artifact(tmp_path, monkeypatch)
    descriptor = json.loads(artifact.descriptor_path.read_bytes())
    parameters = _parameters(artifact)
    phase6_before = _phase6_before_capture(parameters)
    s3_head = _s3_head(artifact)
    original = json.loads(render_phase718_enabled_template())
    processed = deepcopy(original)
    processed["Resources"]["PublicationRecoveryFunctionRecoveryQueue"] = {
        "Type": "AWS::Lambda::EventSourceMapping"
    }
    changes = []
    required = {
        *(
            f"Publication{name}Function"
            for name in ("Query", "Request", "Dispatcher", "Worker", "Recovery", "Retention")
        ),
        "PublicationWorkerRole",
        "PublicationDispatcherStreamMapping",
        "PublicationRetentionStreamMapping",
        "PublicationRecoveryFunctionRecoveryQueue",
        "PublicationDueWorkSweepRule",
        "PublicationRecoverySweepRule",
        "PublicationWorkflowFailureRule",
        "PublicationQueryIntegration",
        "PublicationQueryInvokePermission",
        "PublicationQueryRoute",
        "PublicationRequestIntegration",
        "PublicationRequestInvokePermission",
        "PublicationRequestRoute",
    }
    for name in sorted(required):
        changes.append(
            {
                "ResourceChange": {
                    "Action": "Add"
                    if name.startswith(
                        (
                            "PublicationQueryI",
                            "PublicationQueryR",
                            "PublicationRequestI",
                            "PublicationRequestR",
                        )
                    )
                    and name
                    in {
                        "PublicationQueryIntegration",
                        "PublicationQueryInvokePermission",
                        "PublicationQueryRoute",
                        "PublicationRequestIntegration",
                        "PublicationRequestInvokePermission",
                        "PublicationRequestRoute",
                    }
                    else "Modify",
                    "LogicalResourceId": name,
                    "Replacement": "False",
                    "ResourceType": processed["Resources"][name]["Type"],
                }
            }
        )
    observation = {
        "ChangeSetName": "phase718-enabled",
        "Changes": changes,
        "ExecutionStatus": "AVAILABLE",
        "Parameters": [
            {"ParameterKey": key, "ParameterValue": value} for key, value in parameters.items()
        ],
        "StackName": STACK_NAME,
        "Status": "CREATE_COMPLETE",
    }
    assert verify_change_set_observation(
        observation,
        descriptor=descriptor,
        expected_parameters=parameters,
        phase6_before=phase6_before,
        s3_head=s3_head,
        archive_path=artifact.archive_path,
        original_template=original,
        processed_template=processed,
        change_set_name="phase718-enabled",
    ) == tuple(sorted(required))

    for parameter_name, drifted_value in {
        "ApplicationReleaseFingerprint": "f" * 64,
        "CanaryEvidenceFingerprint": "d" * 64,
        "EnabledCodeS3Bucket": "wrong-artifact-bucket",
        "EnabledCodeS3ObjectVersion": "wrong-version",
        "EnabledReleaseFingerprint": "1" * 64,
        "EnablementEvidenceFingerprint": "e" * 64,
        "PrintifySecretArn": (
            f"arn:aws:secretsmanager:{REGION}:{ACCOUNT_ID}:secret:wrong-printify-AbCd12"
        ),
        "SellerHttpApiAuthorizerId": "wrong-authorizer",
        "SellerHttpApiId": "z9y8x7w6v5",
        "SellerUserPoolClientId": "wrong-client",
        "SellerUserPoolId": "us-west-2_wrongpool",
        "StateTableStreamArn": (
            f"arn:aws:dynamodb:{REGION}:{ACCOUNT_ID}:table/mr-lister-phase6-dev/"
            "stream/2026-09-03T01:00:00.000"
        ),
    }.items():
        drifted_parameters = {**parameters, parameter_name: drifted_value}
        drifted_observation = deepcopy(observation)
        for parameter in drifted_observation["Parameters"]:
            if parameter["ParameterKey"] == parameter_name:
                parameter["ParameterValue"] = drifted_value
        with pytest.raises(Phase718EnabledDeploymentError):
            verify_change_set_observation(
                drifted_observation,
                descriptor=descriptor,
                expected_parameters=drifted_parameters,
                phase6_before=phase6_before,
                s3_head=s3_head,
                archive_path=artifact.archive_path,
                original_template=original,
                processed_template=processed,
                change_set_name="phase718-enabled",
            )

    wrong_observed_parameters = deepcopy(observation)
    wrong_observed_parameters["Parameters"][0]["ParameterValue"] = "unexpected"
    with pytest.raises(Phase718EnabledDeploymentError):
        verify_change_set_observation(
            wrong_observed_parameters,
            descriptor=descriptor,
            expected_parameters=parameters,
            phase6_before=phase6_before,
            s3_head=s3_head,
            archive_path=artifact.archive_path,
            original_template=original,
            processed_template=processed,
            change_set_name="phase718-enabled",
        )

    captures = {
        "change-set.json": observation,
        "parameters.json": parameters,
        "phase6-before.json": phase6_before,
        "s3-head.json": s3_head,
        "original-template.json": original,
        "processed-template.json": processed,
    }
    paths: dict[str, Path] = {}
    for filename, value in captures.items():
        path = tmp_path / filename
        path.write_text(json.dumps(value), encoding="utf-8")
        paths[filename] = path
    assert (
        deployment_verifier.main(
            [
                "--mode",
                "change-set",
                "--observation",
                str(paths["change-set.json"]),
                "--parameters",
                str(paths["parameters.json"]),
                "--phase6-before",
                str(paths["phase6-before.json"]),
                "--s3-head",
                str(paths["s3-head.json"]),
                "--deployment-root",
                str(artifact.deployment_root),
                "--archive",
                str(artifact.archive_path),
                "--descriptor",
                str(artifact.descriptor_path),
                "--original-template",
                str(paths["original-template.json"]),
                "--processed-template",
                str(paths["processed-template.json"]),
                "--change-set-name",
                "phase718-enabled",
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out) == {
        "change_count": len(required),
        "stack_name": STACK_NAME,
        "status": "passed",
    }

    conditional_replacement = deepcopy(observation)
    conditional_replacement["Changes"][0]["ResourceChange"]["Replacement"] = "Conditional"
    with pytest.raises(Phase718EnabledDeploymentError):
        verify_change_set_observation(
            conditional_replacement,
            descriptor=descriptor,
            expected_parameters=parameters,
            phase6_before=phase6_before,
            s3_head=s3_head,
            archive_path=artifact.archive_path,
            original_template=original,
            processed_template=processed,
            change_set_name="phase718-enabled",
        )

    observation["Changes"][0]["ResourceChange"]["Action"] = "Remove"
    with pytest.raises(Phase718EnabledDeploymentError):
        verify_change_set_observation(
            observation,
            descriptor=descriptor,
            expected_parameters=parameters,
            phase6_before=phase6_before,
            s3_head=s3_head,
            archive_path=artifact.archive_path,
            original_template=original,
            processed_template=processed,
            change_set_name="phase718-enabled",
        )


def test_enabled_readback_binds_artifact_routes_triggers_and_phase6(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifact = _artifact(tmp_path, monkeypatch)
    observation = _enabled_observation(artifact)
    binding = verify_enabled_deployment_readback(
        observation,
        deployment_root=artifact.deployment_root,
        archive_path=artifact.archive_path,
        descriptor_path=artifact.descriptor_path,
        expected_parameters=_parameters(artifact),
    )
    assert binding.release_fingerprint == artifact.release_fingerprint
    assert binding.routes == (
        "GET /v1/jobs/{job_id}/publication",
        "POST /v1/jobs/{job_id}/publish",
    )

    changed = deepcopy(observation)
    changed["lambda_configurations"]["Query"]["Environment"]["Variables"][
        "MR_LISTER_PRINTIFY_SECRET_ARN"
    ] = _parameters(artifact)["PrintifySecretArn"]
    with pytest.raises(Phase718EnabledDeploymentError):
        verify_enabled_deployment_readback(
            changed,
            deployment_root=artifact.deployment_root,
            archive_path=artifact.archive_path,
            descriptor_path=artifact.descriptor_path,
            expected_parameters=_parameters(artifact),
        )

    phase6_drift = deepcopy(observation)
    phase6_drift["phase6_after"]["Stacks"][0]["LastUpdatedTime"] = "2026-09-03T00:00:01+00:00"
    with pytest.raises(Phase718EnabledDeploymentError):
        verify_enabled_deployment_readback(
            phase6_drift,
            deployment_root=artifact.deployment_root,
            archive_path=artifact.archive_path,
            descriptor_path=artifact.descriptor_path,
            expected_parameters=_parameters(artifact),
        )

    phase6_binding_drift = deepcopy(observation)
    for output in phase6_binding_drift["phase6_after"]["Stacks"][0]["Outputs"]:
        if output["OutputKey"] == "SellerApiOrigin":
            output["OutputValue"] = "https://z9y8x7w6v5.execute-api.us-west-2.amazonaws.com"
    phase6_binding_drift["phase6_before"] = deepcopy(phase6_binding_drift["phase6_after"])
    with pytest.raises(Phase718EnabledDeploymentError):
        verify_enabled_deployment_readback(
            phase6_binding_drift,
            deployment_root=artifact.deployment_root,
            archive_path=artifact.archive_path,
            descriptor_path=artifact.descriptor_path,
            expected_parameters=_parameters(artifact),
        )

    authorizer_drift = deepcopy(observation)
    authorizer_drift["api"]["authorizer"]["JwtConfiguration"]["Audience"] = ["other"]
    with pytest.raises(Phase718EnabledDeploymentError):
        verify_enabled_deployment_readback(
            authorizer_drift,
            deployment_root=artifact.deployment_root,
            archive_path=artifact.archive_path,
            descriptor_path=artifact.descriptor_path,
            expected_parameters=_parameters(artifact),
        )

    role_drift = deepcopy(observation)
    role_drift["execution_role_policies"]["Query"]["attached_policies"]["AttachedPolicies"] = [
        {"PolicyArn": "arn:aws:iam::aws:policy/SecretsManagerReadWrite"}
    ]
    with pytest.raises(Phase718EnabledDeploymentError):
        verify_enabled_deployment_readback(
            role_drift,
            deployment_root=artifact.deployment_root,
            archive_path=artifact.archive_path,
            descriptor_path=artifact.descriptor_path,
            expected_parameters=_parameters(artifact),
        )

    broad_invoke = deepcopy(observation)
    query_policy = json.loads(broad_invoke["lambda_policies"]["Query"]["Policy"])
    query_policy["Statement"].append(
        {
            "Action": "lambda:InvokeFunction",
            "Effect": "Allow",
            "Principal": {"Service": "apigateway.amazonaws.com"},
            "Resource": (
                f"arn:aws:lambda:{REGION}:{ACCOUNT_ID}:function:"
                "mr-lister-phase7-dev-publication-query"
            ),
        }
    )
    broad_invoke["lambda_policies"]["Query"]["Policy"] = json.dumps(query_policy)
    with pytest.raises(Phase718EnabledDeploymentError):
        verify_enabled_deployment_readback(
            broad_invoke,
            deployment_root=artifact.deployment_root,
            archive_path=artifact.archive_path,
            descriptor_path=artifact.descriptor_path,
            expected_parameters=_parameters(artifact),
        )

    shared_integration = deepcopy(observation)
    shared_integration["api"]["routes"]["Items"].append(
        {
            "AuthorizationScopes": ["mr-lister-api/seller"],
            "AuthorizationType": "JWT",
            "AuthorizerId": "authorizer123",
            "RouteKey": "GET /unexpected",
            "Target": "integrations/integration-query",
        }
    )
    with pytest.raises(Phase718EnabledDeploymentError):
        verify_enabled_deployment_readback(
            shared_integration,
            deployment_root=artifact.deployment_root,
            archive_path=artifact.archive_path,
            descriptor_path=artifact.descriptor_path,
            expected_parameters=_parameters(artifact),
        )

    mapping_drift = deepcopy(observation)
    mapping_drift["event_source_mappings"]["Dispatcher"]["EventSourceMappings"][0][
        "EventSourceArn"
    ] = "arn:aws:dynamodb:us-west-2:384627057108:table/other/stream/one"
    with pytest.raises(Phase718EnabledDeploymentError):
        verify_enabled_deployment_readback(
            mapping_drift,
            deployment_root=artifact.deployment_root,
            archive_path=artifact.archive_path,
            descriptor_path=artifact.descriptor_path,
            expected_parameters=_parameters(artifact),
        )

    rule_drift = deepcopy(observation)
    rule_drift["event_rules"]["PublicationDueWorkSweepRule"]["targets"]["Targets"][0]["Arn"] = (
        f"arn:aws:lambda:{REGION}:{ACCOUNT_ID}:function:other"
    )
    with pytest.raises(Phase718EnabledDeploymentError):
        verify_enabled_deployment_readback(
            rule_drift,
            deployment_root=artifact.deployment_root,
            archive_path=artifact.archive_path,
            descriptor_path=artifact.descriptor_path,
            expected_parameters=_parameters(artifact),
        )

    descriptor = json.loads(artifact.descriptor_path.read_bytes())
    descriptor["state_table"] = "mr-lister-phase6-qa"
    with pytest.raises(ValueError):
        deployment_verifier._enabled_parameters(_parameters(artifact), descriptor=descriptor)


def test_rollback_readback_requires_exact_predecessor_and_no_enabled_routes() -> None:
    parameters = {
        "ActivationMode": "PRODUCTION_DISABLED",
        "CandidateCodeS3Bucket": "bucket-name",
        "CandidateCodeS3ObjectVersion": "version-1",
        "CandidateReleaseFingerprint": "d" * 64,
        "EnvironmentName": "dev",
        "SellerUserPoolClientId": "client123",
        "SellerUserPoolId": "us-west-2_pool123",
        "StateTableArn": f"arn:aws:dynamodb:{REGION}:{ACCOUNT_ID}:table/mr-lister-phase6-dev",
        "StateTableStreamArn": (
            f"arn:aws:dynamodb:{REGION}:{ACCOUNT_ID}:table/mr-lister-phase6-dev/stream/one"
        ),
    }
    configurations = {
        component: {
            "Architectures": ["arm64"],
            "CodeSha256": "predecessor-code-sha",
            "Environment": {"Variables": {"MR_LISTER_PHASE7_PUBLICATION_ENABLED": "false"}},
            "FunctionName": f"mr-lister-phase7-dev-publication-{component.casefold()}",
            "Handler": (
                "mr_lister.cloud.phase7_production_entrypoints."
                f"publication_{component.casefold()}_handler"
            ),
            "LastUpdateStatus": "Successful",
            "MemorySize": 512,
            "Role": (
                f"arn:aws:iam::{ACCOUNT_ID}:role/"
                f"mr-lister-phase7-dev-publication-{component.casefold()}-role"
            ),
            "Runtime": "python3.12",
            "State": "Active",
            "Timeout": 30,
        }
        for component in ("Query", "Request", "Dispatcher", "Worker", "Recovery", "Retention")
    }
    rule_names = {
        "PublicationDueWorkSweepRule": "mr-lister-phase7-dev-publication-due-sweep",
        "PublicationRecoverySweepRule": "mr-lister-phase7-dev-publication-recovery-sweep",
        "PublicationWorkflowFailureRule": "mr-lister-phase7-dev-publication-workflow-failure",
    }
    dlq_arn = f"arn:aws:sqs:{REGION}:{ACCOUNT_ID}:mr-lister-phase7-dev-publication-operations-dlq"
    rollback_mappings = {}
    for component in ("Dispatcher", "Recovery", "Retention"):
        mapping = {
            "BatchSize": 1 if component != "Dispatcher" else 25,
            "EventSourceArn": (
                f"arn:aws:sqs:{REGION}:{ACCOUNT_ID}:mr-lister-phase7-dev-publication-recovery"
                if component == "Recovery"
                else parameters["StateTableStreamArn"]
            ),
            "FunctionArn": (
                f"arn:aws:lambda:{REGION}:{ACCOUNT_ID}:function:"
                f"mr-lister-phase7-dev-publication-{component.casefold()}"
            ),
            "State": "Disabled",
        }
        if component == "Recovery":
            mapping["FunctionResponseTypes"] = ["ReportBatchItemFailures"]
        else:
            mapping.update(
                {
                    "BisectBatchOnFunctionError": True,
                    "DestinationConfig": {"OnFailure": {"Destination": dlq_arn}},
                    "MaximumBatchingWindowInSeconds": 1 if component == "Dispatcher" else 0,
                    "MaximumRecordAgeInSeconds": 300,
                    "MaximumRetryAttempts": 2,
                    "StartingPosition": "LATEST",
                }
            )
            mapping["FilterCriteria"] = {
                "Filters": [
                    {
                        "Pattern": (
                            '{"eventName":["INSERT","MODIFY"],"dynamodb":{"Keys":'
                            '{"SK":{"S":[{"prefix":"PUBLICATION_WORK#"}]}}}}'
                            if component == "Dispatcher"
                            else (
                                '{"eventName":["INSERT"],"dynamodb":{"Keys":{"PK":{"S":'
                                '[{"prefix":"PUBLICATION#"}]},"SK":{"S":'
                                '["TERMINAL_JOB_LINK"]}},"StreamViewType":["KEYS_ONLY"]}}'
                            )
                        )
                    }
                ]
            }
        rollback_mappings[component] = {"EventSourceMappings": [mapping]}
    observation = {
        "api": {"routes": {"Items": [{"RouteKey": "GET /v1/jobs/{job_id}"}]}},
        "event_rules": _rule_observations(rule_names, state="DISABLED"),
        "event_source_mappings": rollback_mappings,
        "lambda_concurrency": {
            component: {"ReservedConcurrentExecutions": 0} for component in configurations
        },
        "lambda_configurations": configurations,
        "predecessor_lambda_concurrency": {
            component: {"ReservedConcurrentExecutions": 0} for component in configurations
        },
        "predecessor_lambda_configurations": deepcopy(configurations),
        "predecessor_processed_template": {
            "StagesAvailable": ["Original", "Processed"],
            "TemplateBody": _processed_predecessor(),
        },
        "predecessor_stack": _stack(
            STACK_NAME,
            status="UPDATE_ROLLBACK_COMPLETE",
            parameters=parameters,
            outputs={
                "DeploymentReadiness": "PRODUCTION_DISABLED",
                "ProviderMutationEnabled": "false",
                "PublicationQueryRegistered": "false",
                "PublicationRequestRegistered": "false",
                "PublicationWorkerTriggered": "false",
                "ResourceInstantiationPossible": "true",
                "SellerPublicationEnabled": "false",
            },
        ),
        "predecessor_template_authority": {
            "packaged_template_s3_key": (
                "phase7/sam/templates/"
                "2a6f45a790e554e3680e23c4d35abf4d8a2a99611a20e301c66d2a61a284b9db.yaml"
            ),
            "packaged_template_s3_object_version": "fvTXvRtq9r.JtdyorhIzV.PZGLei9w4D",
            "packaged_template_sha256": (
                "2a6f45a790e554e3680e23c4d35abf4d8a2a99611a20e301c66d2a61a284b9db"
            ),
        },
        "phase6_after": _phase6_stack(),
        "phase6_before": _phase6_stack(),
        "stack": _stack(
            STACK_NAME,
            status="UPDATE_COMPLETE",
            parameters=parameters,
            outputs={
                "DeploymentReadiness": "PRODUCTION_DISABLED",
                "ProviderMutationEnabled": "false",
                "PublicationQueryRegistered": "false",
                "PublicationRequestRegistered": "false",
                "PublicationWorkerTriggered": "false",
                "ResourceInstantiationPossible": "true",
                "SellerPublicationEnabled": "false",
            },
        ),
        "rollback_processed_template": {
            "StagesAvailable": ["Original", "Processed"],
            "TemplateBody": _processed_predecessor(),
        },
    }
    verify_predecessor_rollback_readback(observation, expected_parameters=parameters)

    observation["api"]["routes"]["Items"].append({"RouteKey": "POST /v1/jobs/{job_id}/publish"})
    with pytest.raises(Phase718EnabledDeploymentError):
        verify_predecessor_rollback_readback(observation, expected_parameters=parameters)
