from __future__ import annotations

import base64
import json
from copy import deepcopy
from hashlib import sha256
from pathlib import Path
from typing import Any

import pytest

import tools.verify_phase715c_operations_deployment as verifier
from tools.render_phase715c_operations_update import (
    BASE_TEMPLATE,
    RECOVERY_HANDLER,
    RETENTION_HANDLER,
    render_operations_update_template,
)
from tools.verify_phase715c_operations_deployment import (
    ACCOUNT_ID,
    PREDECESSOR_ARCHIVE_SHA256,
    PREDECESSOR_ARCHIVE_SIZE_BYTES,
    PREDECESSOR_CODE_S3_OBJECT_VERSION,
    PREDECESSOR_RELEASE_FINGERPRINT,
    REGION,
    STACK_NAME,
    Phase715cOperationsDeploymentError,
    verify_change_set_observation,
    verify_operations_lambda_readback,
    verify_phase6_application_release_observation,
    verify_predecessor_rollback_readback,
    verify_processed_template_delta,
    verify_s3_head_observation,
    verify_safety_readback,
    verify_stack_transition,
)

OPERATIONS_RELEASE = "a" * 64
APPLICATION_RELEASE = "b" * 64
BUCKET = "mr-lister-phase7-artifacts-dev-384627057108-us-west-2"
OBJECT_VERSION = "operations.v1-token"
CHANGE_SET_NAME = "phase715c-operations-update-a1"
STREAM_ARN = (
    f"arn:aws:dynamodb:{REGION}:{ACCOUNT_ID}:table/mr-lister-phase6-dev/"
    "stream/2026-09-01T00:00:00.000"
)


def _processed_templates() -> tuple[dict[str, Any], dict[str, Any]]:
    predecessor_source = json.loads(BASE_TEMPLATE.read_bytes())
    target_source = json.loads(render_operations_update_template())
    return _pseudo_process(predecessor_source), _pseudo_process(target_source)


def _pseudo_process(source: dict[str, Any]) -> dict[str, Any]:
    processed = deepcopy(source)
    processed.pop("Transform")
    globals_properties = processed.pop("Globals")["Function"]
    resources = processed["Resources"]
    for resource in resources.values():
        if resource["Type"] == "AWS::Serverless::Function":
            resource["Type"] = "AWS::Lambda::Function"
            properties = resource["Properties"]
            merged = deepcopy(globals_properties)
            global_environment = merged.pop("Environment", {"Variables": {}})
            function_environment = properties.get("Environment", {"Variables": {}})
            variables = {
                **global_environment["Variables"],
                **function_environment["Variables"],
            }
            global_tags = merged.pop("Tags", {})
            merged.update(properties)
            merged["Environment"] = {"Variables": variables}
            merged["Tags"] = global_tags
            code_uri = merged.pop("CodeUri")
            merged["Code"] = {
                "S3Bucket": code_uri["Bucket"],
                "S3Key": code_uri["Key"],
                "S3ObjectVersion": code_uri["Version"],
            }
            merged.pop("Events", None)
            resource["Properties"] = merged
        elif resource["Type"] == "AWS::Serverless::StateMachine":
            resource["Type"] = "AWS::StepFunctions::StateMachine"
    recovery = source["Resources"]["PublicationRecoveryFunction"]["Properties"]["Events"][
        "RecoveryQueue"
    ]["Properties"]
    resources["PublicationRecoveryFunctionRecoveryQueue"] = {
        "Condition": "InstantiateProductionCandidate",
        "Type": "AWS::Lambda::EventSourceMapping",
        "Properties": {
            "BatchSize": recovery["BatchSize"],
            "Enabled": recovery["Enabled"],
            "EventSourceArn": recovery["Queue"],
            "FunctionName": {"Ref": "PublicationRecoveryFunction"},
            "FunctionResponseTypes": recovery["FunctionResponseTypes"],
        },
    }
    return processed


def _descriptor(archive: bytes) -> dict[str, Any]:
    return {
        "application_release_fingerprint": APPLICATION_RELEASE,
        "archive": {
            "path": "phase715c-operations.zip",
            "sha256": sha256(archive).hexdigest(),
            "size_bytes": len(archive),
        },
        "release_fingerprint": OPERATIONS_RELEASE,
        "s3_binding": {
            "application_release_fingerprint_parameter": "ApplicationReleaseFingerprint",
            "archive_sha256_metadata_key": "mr-lister-archive-sha256",
            "bucket_parameter": "OperationsCodeS3Bucket",
            "key_template": ("phase7/operations/{release_fingerprint}/phase715c-operations.zip"),
            "object_version_parameter": "OperationsCodeS3ObjectVersion",
            "release_fingerprint_metadata_key": "mr-lister-release-fingerprint",
            "release_fingerprint_parameter": "OperationsReleaseFingerprint",
            "server_side_encryption": "AES256",
        },
    }


def _predecessor_parameters() -> dict[str, str]:
    return {
        "ActivationMode": "PRODUCTION_DISABLED",
        "CandidateCodeS3Bucket": BUCKET,
        "CandidateCodeS3ObjectVersion": PREDECESSOR_CODE_S3_OBJECT_VERSION,
        "CandidateReleaseFingerprint": PREDECESSOR_RELEASE_FINGERPRINT,
        "EnvironmentName": "dev",
        "SellerUserPoolClientId": "client123",
        "SellerUserPoolId": "us-west-2_pool123",
        "StateTableArn": (f"arn:aws:dynamodb:{REGION}:{ACCOUNT_ID}:table/mr-lister-phase6-dev"),
        "StateTableStreamArn": STREAM_ARN,
    }


def _operations_parameters() -> dict[str, str]:
    return {
        **_predecessor_parameters(),
        "ApplicationReleaseFingerprint": APPLICATION_RELEASE,
        "OperationsCodeS3Bucket": BUCKET,
        "OperationsCodeS3ObjectVersion": OBJECT_VERSION,
        "OperationsReleaseFingerprint": OPERATIONS_RELEASE,
    }


def _closed_outputs() -> dict[str, str]:
    return {
        "DeploymentReadiness": "PRODUCTION_DISABLED",
        "ProviderMutationEnabled": "false",
        "PublicationQueryRegistered": "false",
        "PublicationRequestRegistered": "false",
        "PublicationWorkerTriggered": "false",
        "ResourceInstantiationPossible": "true",
        "SellerPublicationEnabled": "false",
    }


def _stack(*, operations: bool, status: str) -> dict[str, Any]:
    parameters = _operations_parameters() if operations else _predecessor_parameters()
    outputs = _closed_outputs()
    if operations:
        outputs = {
            **outputs,
            "OperationsReleaseFingerprint": OPERATIONS_RELEASE,
            "OperationsRuntimeReadiness": "PROVIDER_FREE_OPERATIONS_DIRECT_INVOKE_ONLY",
        }
    return {
        "Stacks": [
            {
                "EnableTerminationProtection": False,
                "NotificationARNs": [],
                "Outputs": [
                    {"OutputKey": key, "OutputValue": value} for key, value in outputs.items()
                ],
                "Parameters": [
                    {"ParameterKey": key, "ParameterValue": value}
                    for key, value in parameters.items()
                ],
                "StackId": (
                    f"arn:aws:cloudformation:{REGION}:{ACCOUNT_ID}:stack/{STACK_NAME}/stack-id"
                ),
                "StackName": STACK_NAME,
                "StackStatus": status,
                "Tags": [
                    {"Key": "Phase", "Value": "7.15C-production-disabled"},
                    {"Key": "Project", "Value": "MrLister"},
                ],
            }
        ]
    }


def _phase6_stack(
    *,
    application_release: str = APPLICATION_RELEASE,
    status: str = "UPDATE_COMPLETE",
) -> dict[str, Any]:
    return {
        "Stacks": [
            {
                "Parameters": [
                    {"ParameterKey": "EnvironmentName", "ParameterValue": "dev"},
                    {
                        "ParameterKey": "ReleaseFingerprint",
                        "ParameterValue": application_release,
                    },
                ],
                "StackId": (
                    f"arn:aws:cloudformation:{REGION}:{ACCOUNT_ID}:"
                    "stack/mr-lister-phase6-dev/stack-id"
                ),
                "StackName": "mr-lister-phase6-dev",
                "StackStatus": status,
            }
        ]
    }


def _environment(*, operations: bool) -> dict[str, str]:
    result = {
        "MR_LISTER_COGNITO_CLIENT_ID": "client123",
        "MR_LISTER_COGNITO_GROUP": "seller",
        "MR_LISTER_COGNITO_ISSUER": (
            "https://cognito-idp.us-west-2.amazonaws.com/us-west-2_pool123"
        ),
        "MR_LISTER_COGNITO_SCOPE": "mr-lister-api/seller",
        "MR_LISTER_PHASE7_ACTIVATION_MODE": "SOURCE_ONLY_DISABLED",
        "MR_LISTER_PHASE7_CONTRACT_FINGERPRINT": (
            "548b710230618e73c20a509f2121799c415b50070e1e2ae7e1b82fe3c37e2981"
        ),
        "MR_LISTER_PHASE7_CONTRACT_VERSION": "7.0.1",
        "MR_LISTER_PHASE7_PRODUCTION_CANDIDATE_ENABLED": "false",
        "MR_LISTER_PHASE7_PRODUCTION_RELEASE_FINGERPRINT": (PREDECESSOR_RELEASE_FINGERPRINT),
        "MR_LISTER_PHASE7_PUBLICATION_ENABLED": "false",
        "MR_LISTER_PHASE7_QUERY_ENABLED": "false",
        "MR_LISTER_PHASE7_REQUEST_ENABLED": "false",
        "MR_LISTER_PHASE7_SCAFFOLD_ONLY": "true",
        "MR_LISTER_PRODUCT_PROFILE_FINGERPRINT": (
            "5de1257141cfdacb1731f68bb9113712957483b33d5b0f7115afdba86eb7476c"
        ),
        "MR_LISTER_PRODUCT_PROFILE_ID": "gildan_64000_swiftpod",
        "MR_LISTER_PRODUCT_PROFILE_PATH": (
            "/var/task/config/product_profiles/gildan_64000_swiftpod.json"
        ),
        "MR_LISTER_PRODUCT_PROFILE_VERSION": "2",
        "MR_LISTER_RELEASE_FINGERPRINT": (
            APPLICATION_RELEASE if operations else PREDECESSOR_RELEASE_FINGERPRINT
        ),
        "MR_LISTER_STATE_TABLE": "mr-lister-phase6-dev",
    }
    if operations:
        result.update(
            {
                "MR_LISTER_PHASE715C_OPERATIONS_MODE": "PROVIDER_FREE_OPERATIONS",
                "MR_LISTER_PHASE715C_OPERATIONS_RELEASE_FINGERPRINT": OPERATIONS_RELEASE,
                "MR_LISTER_PHASE7_DISPATCHER_ENABLED": "false",
                "MR_LISTER_PHASE7_WORKER_ENABLED": "false",
                "MR_LISTER_PUBLICATION_WORKFLOW_ARN": (
                    f"arn:aws:states:{REGION}:{ACCOUNT_ID}:stateMachine:"
                    "mr-lister-phase7-dev-publication"
                ),
            }
        )
    return result


def _lambda_configuration(
    *,
    logical_id: str,
    operations: bool,
    archive_sha256: str,
    archive_size: int,
) -> dict[str, Any]:
    recovery = logical_id == "PublicationRecoveryFunction"
    suffix = "recovery" if recovery else "retention"
    if operations:
        handler = RECOVERY_HANDLER if recovery else RETENTION_HANDLER
    else:
        handler = f"mr_lister.cloud.phase7_production_entrypoints.publication_{suffix}_handler"
    function_name = f"mr-lister-phase7-dev-publication-{suffix}"
    return {
        "Architectures": ["arm64"],
        "CodeSha256": base64.b64encode(bytes.fromhex(archive_sha256)).decode("ascii"),
        "CodeSize": archive_size,
        "DeadLetterConfig": {},
        "Environment": {"Variables": _environment(operations=operations)},
        "FileSystemConfigs": [],
        "FunctionArn": f"arn:aws:lambda:{REGION}:{ACCOUNT_ID}:function:{function_name}",
        "FunctionName": function_name,
        "Handler": handler,
        "LastUpdateStatus": "Successful",
        "Layers": [],
        "LoggingConfig": {
            "ApplicationLogLevel": "ERROR",
            "LogFormat": "JSON",
            "LogGroup": f"/aws/lambda/{function_name}",
            "SystemLogLevel": "WARN",
        },
        "MemorySize": 512,
        "PackageType": "Zip",
        "Role": f"arn:aws:iam::{ACCOUNT_ID}:role/{function_name}-role",
        "Runtime": "python3.12",
        "State": "Active",
        "Timeout": 60,
        "Version": "$LATEST",
        "VpcConfig": {},
    }


def _lambda_readbacks(
    *,
    operations: bool,
    archive_sha256: str,
    archive_size: int,
) -> tuple[dict[str, Any], dict[str, Any]]:
    configurations = {
        logical_id: _lambda_configuration(
            logical_id=logical_id,
            operations=operations,
            archive_sha256=archive_sha256,
            archive_size=archive_size,
        )
        for logical_id in ("PublicationRecoveryFunction", "PublicationRetentionFunction")
    }
    concurrency = 1 if operations else 0
    concurrencies = {
        logical_id: {"ReservedConcurrentExecutions": concurrency} for logical_id in configurations
    }
    return configurations, concurrencies


def _safety() -> dict[str, Any]:
    lambda_prefix = f"arn:aws:lambda:{REGION}:{ACCOUNT_ID}:function:"
    sqs_prefix = f"arn:aws:sqs:{REGION}:{ACCOUNT_ID}:"
    return {
        "event_source_mappings": [
            {
                "batch_size": 25,
                "enabled": False,
                "event_source_arn": STREAM_ARN,
                "function_name": "mr-lister-phase7-dev-publication-dispatcher",
                "logical_id": "PublicationDispatcherStreamMapping",
                "state": "Disabled",
            },
            {
                "batch_size": 1,
                "enabled": False,
                "event_source_arn": (f"{sqs_prefix}mr-lister-phase7-dev-publication-recovery"),
                "function_name": "mr-lister-phase7-dev-publication-recovery",
                "logical_id": "PublicationRecoveryFunctionRecoveryQueue",
                "state": "Disabled",
            },
            {
                "batch_size": 1,
                "enabled": False,
                "event_source_arn": STREAM_ARN,
                "function_name": "mr-lister-phase7-dev-publication-retention",
                "logical_id": "PublicationRetentionStreamMapping",
                "state": "Disabled",
            },
        ],
        "eventbridge_rules": [
            {
                "logical_id": "PublicationDueWorkSweepRule",
                "name": "mr-lister-phase7-dev-publication-due-sweep",
                "state": "DISABLED",
                "target_arn": (f"{lambda_prefix}mr-lister-phase7-dev-publication-dispatcher"),
                "target_id": "PublicationDispatcher",
            },
            {
                "logical_id": "PublicationRecoverySweepRule",
                "name": "mr-lister-phase7-dev-publication-recovery-sweep",
                "state": "DISABLED",
                "target_arn": (f"{lambda_prefix}mr-lister-phase7-dev-publication-recovery"),
                "target_id": "PublicationRecovery",
            },
            {
                "logical_id": "PublicationWorkflowFailureRule",
                "name": "mr-lister-phase7-dev-publication-workflow-failure",
                "state": "DISABLED",
                "target_arn": (f"{sqs_prefix}mr-lister-phase7-dev-publication-recovery"),
                "target_id": "PublicationRecoveryQueue",
            },
        ],
        "function_urls_absent": [
            "mr-lister-phase7-dev-publication-query",
            "mr-lister-phase7-dev-publication-request",
            "mr-lister-phase7-dev-publication-dispatcher",
            "mr-lister-phase7-dev-publication-worker",
            "mr-lister-phase7-dev-publication-recovery",
            "mr-lister-phase7-dev-publication-retention",
        ],
        "provider_credential_environment_name_count": 0,
        "provider_mutation_enabled": False,
        "registered_route_count": 0,
        "seller_publication_enabled": False,
    }


def _change_set() -> dict[str, Any]:
    return {
        "ChangeSetId": (
            f"arn:aws:cloudformation:{REGION}:{ACCOUNT_ID}:changeSet/"
            f"{CHANGE_SET_NAME}/change-set-id"
        ),
        "ChangeSetName": CHANGE_SET_NAME,
        "ChangeSetType": "UPDATE",
        "Changes": [
            {
                "ResourceChange": {
                    "Action": "Modify",
                    "Details": [
                        {
                            "Target": {
                                "Attribute": "Properties",
                                "Name": "Code",
                                "RequiresRecreation": "Never",
                            }
                        }
                    ],
                    "LogicalResourceId": logical_id,
                    "PhysicalResourceId": (
                        "mr-lister-phase7-dev-publication-"
                        + ("recovery" if "Recovery" in logical_id else "retention")
                    ),
                    "Replacement": "False",
                    "ResourceType": "AWS::Lambda::Function",
                    "Scope": ["Properties"],
                },
                "Type": "Resource",
            }
            for logical_id in (
                "PublicationRecoveryFunction",
                "PublicationRetentionFunction",
            )
        ],
        "ExecutionStatus": "AVAILABLE",
        "IncludeNestedStacks": False,
        "Parameters": [
            {"ParameterKey": key, "ParameterValue": value}
            for key, value in _operations_parameters().items()
        ],
        "StackId": (f"arn:aws:cloudformation:{REGION}:{ACCOUNT_ID}:stack/{STACK_NAME}/stack-id"),
        "StackName": STACK_NAME,
        "Status": "CREATE_COMPLETE",
    }


def test_processed_update_is_exact_and_rejects_any_extra_delta() -> None:
    predecessor, target = _processed_templates()
    before = {"StagesAvailable": ["Original", "Processed"], "TemplateBody": predecessor}
    after = {"StagesAvailable": ["Original", "Processed"], "TemplateBody": target}
    fingerprint = verify_processed_template_delta(before, after)
    assert len(fingerprint) == 64

    enabled = deepcopy(after)
    enabled["TemplateBody"]["Resources"]["PublicationRecoverySweepRule"]["Properties"]["State"] = (
        "ENABLED"
    )
    with pytest.raises(Phase715cOperationsDeploymentError):
        verify_processed_template_delta(before, enabled)

    extra = deepcopy(after)
    extra["TemplateBody"]["Resources"]["Unexpected"] = {
        "Type": "AWS::SNS::Topic",
        "Properties": {},
    }
    with pytest.raises(Phase715cOperationsDeploymentError):
        verify_processed_template_delta(before, extra)


def test_change_set_s3_stack_lambda_and_safety_readbacks_are_exact(tmp_path: Path) -> None:
    archive = b"sealed operations archive"
    archive_path = tmp_path / "phase715c-operations.zip"
    archive_path.write_bytes(archive)
    descriptor = _descriptor(archive)
    verify_change_set_observation(
        _change_set(),
        descriptor,
        bucket=BUCKET,
        object_version=OBJECT_VERSION,
        change_set_name=CHANGE_SET_NAME,
    )
    head = {
        "ChecksumSHA256": base64.b64encode(sha256(archive).digest()).decode("ascii"),
        "ContentLength": len(archive),
        "ContentType": "application/zip",
        "Metadata": {
            "mr-lister-archive-sha256": sha256(archive).hexdigest(),
            "mr-lister-release-fingerprint": OPERATIONS_RELEASE,
        },
        "ServerSideEncryption": "AES256",
        "VersionId": OBJECT_VERSION,
    }
    verify_s3_head_observation(
        descriptor,
        head,
        archive_path=archive_path,
        bucket=BUCKET,
        object_version=OBJECT_VERSION,
    )
    assert len(verify_phase6_application_release_observation(descriptor, _phase6_stack())) == 64
    parameters = verify_stack_transition(
        _stack(operations=False, status="CREATE_COMPLETE"),
        _stack(operations=True, status="UPDATE_COMPLETE"),
        descriptor,
        bucket=BUCKET,
        object_version=OBJECT_VERSION,
    )
    configurations, concurrencies = _lambda_readbacks(
        operations=True,
        archive_sha256=sha256(archive).hexdigest(),
        archive_size=len(archive),
    )
    assert (
        len(
            verify_operations_lambda_readback(
                descriptor,
                configurations,
                concurrencies,
                stack_parameters=parameters,
            )
        )
        == 64
    )
    assert len(verify_safety_readback(_safety(), stack_parameters=parameters)) == 64

    wrong_head = deepcopy(head)
    wrong_head["VersionId"] = "other"
    with pytest.raises(Phase715cOperationsDeploymentError):
        verify_s3_head_observation(
            descriptor,
            wrong_head,
            archive_path=archive_path,
            bucket=BUCKET,
            object_version=OBJECT_VERSION,
        )
    leaked = deepcopy(configurations)
    leaked["PublicationRecoveryFunction"]["Environment"]["Variables"][
        "MR_LISTER_PRINTIFY_SECRET_ARN"
    ] = "secret"
    with pytest.raises(Phase715cOperationsDeploymentError):
        verify_operations_lambda_readback(
            descriptor,
            leaked,
            concurrencies,
            stack_parameters=parameters,
        )
    unsafe = deepcopy(_safety())
    unsafe["eventbridge_rules"][1]["state"] = "ENABLED"
    with pytest.raises(Phase715cOperationsDeploymentError):
        verify_safety_readback(unsafe, stack_parameters=parameters)


@pytest.mark.parametrize(
    "drift",
    ("release", "environment", "stack_name", "stack_account", "stack_status", "duplicate"),
)
def test_phase6_application_release_readback_rejects_every_binding_drift(drift: str) -> None:
    descriptor = _descriptor(b"archive")
    observation = _phase6_stack()
    stack = observation["Stacks"][0]
    if drift == "release":
        stack["Parameters"][1]["ParameterValue"] = "c" * 64
    elif drift == "environment":
        stack["Parameters"][0]["ParameterValue"] = "prod"
    elif drift == "stack_name":
        stack["StackName"] = "mr-lister-phase6-prod"
    elif drift == "stack_account":
        stack["StackId"] = stack["StackId"].replace(ACCOUNT_ID, "999999999999")
    elif drift == "stack_status":
        stack["StackStatus"] = "UPDATE_IN_PROGRESS"
    else:
        stack["Parameters"].append(
            {"ParameterKey": "ReleaseFingerprint", "ParameterValue": APPLICATION_RELEASE}
        )

    with pytest.raises(Phase715cOperationsDeploymentError):
        verify_phase6_application_release_observation(descriptor, observation)


def test_change_set_rejects_a_third_resource_or_replacement() -> None:
    descriptor = _descriptor(b"archive")
    extra = _change_set()
    extra["Changes"].append(deepcopy(extra["Changes"][0]))
    extra["Changes"][-1]["ResourceChange"]["LogicalResourceId"] = "PublicationWorkerFunction"
    with pytest.raises(Phase715cOperationsDeploymentError):
        verify_change_set_observation(
            extra,
            descriptor,
            bucket=BUCKET,
            object_version=OBJECT_VERSION,
            change_set_name=CHANGE_SET_NAME,
        )

    replacement = _change_set()
    replacement["Changes"][0]["ResourceChange"]["Replacement"] = "True"
    with pytest.raises(Phase715cOperationsDeploymentError):
        verify_change_set_observation(
            replacement,
            descriptor,
            bucket=BUCKET,
            object_version=OBJECT_VERSION,
            change_set_name=CHANGE_SET_NAME,
        )


def test_exact_predecessor_rollback_tuple_requires_full_restoration() -> None:
    predecessor, _target = _processed_templates()
    processed = {"StagesAvailable": ["Original", "Processed"], "TemplateBody": predecessor}
    predecessor_stack = _stack(operations=False, status="CREATE_COMPLETE")
    rollback_stack = _stack(operations=False, status="UPDATE_COMPLETE")
    predecessor_configurations, predecessor_concurrencies = _lambda_readbacks(
        operations=False,
        archive_sha256=PREDECESSOR_ARCHIVE_SHA256,
        archive_size=PREDECESSOR_ARCHIVE_SIZE_BYTES,
    )
    rollback_configurations = deepcopy(predecessor_configurations)
    rollback_concurrencies = deepcopy(predecessor_concurrencies)
    result = verify_predecessor_rollback_readback(
        processed,
        deepcopy(processed),
        predecessor_stack,
        rollback_stack,
        predecessor_configurations,
        rollback_configurations,
        predecessor_concurrencies,
        rollback_concurrencies,
        _safety(),
    )
    assert result["result"] == "passed"
    assert result["predecessor"]["release_fingerprint"] == (PREDECESSOR_RELEASE_FINGERPRINT)
    assert (
        result["predecessor"]["two_function_configuration_sha256"]
        == result["readback"]["two_function_configuration_sha256"]
    )
    assert len(result["evidence_sha256"]) == 64

    drifted = deepcopy(rollback_configurations)
    drifted["PublicationRetentionFunction"]["Handler"] = RETENTION_HANDLER
    with pytest.raises(Phase715cOperationsDeploymentError):
        verify_predecessor_rollback_readback(
            processed,
            deepcopy(processed),
            predecessor_stack,
            rollback_stack,
            predecessor_configurations,
            drifted,
            predecessor_concurrencies,
            rollback_concurrencies,
            _safety(),
        )


def test_deployment_cli_requires_and_forwards_the_raw_phase6_stack_capture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    capture = tmp_path / "capture.json"
    capture.write_text("{}", encoding="utf-8")
    received: dict[str, object] = {}

    def verify(**values: object) -> dict[str, object]:
        received.update(values)
        return {"result": "passed"}

    monkeypatch.setattr(verifier, "verify_operations_deployment_evidence", verify)
    assert (
        verifier.main(
            [
                "deployment",
                "--deployment",
                str(tmp_path),
                "--archive",
                str(capture),
                "--descriptor",
                str(capture),
                "--update-template",
                str(capture),
                "--predecessor-processed-json",
                str(capture),
                "--target-processed-json",
                str(capture),
                "--change-set-json",
                str(capture),
                "--head-object-json",
                str(capture),
                "--predecessor-stack-json",
                str(capture),
                "--target-stack-json",
                str(capture),
                "--phase6-stack-json",
                str(capture),
                "--lambda-configurations-json",
                str(capture),
                "--lambda-concurrencies-json",
                str(capture),
                "--safety-json",
                str(capture),
                "--bucket",
                BUCKET,
                "--object-version",
                OBJECT_VERSION,
                "--change-set-name",
                CHANGE_SET_NAME,
            ]
        )
        == 0
    )
    assert received["phase6_stack_observation"] == {}
    assert json.loads(capsys.readouterr().out) == {"result": "passed"}


def test_rollback_cli_forwards_every_exact_readback_capture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    capture = tmp_path / "capture.json"
    capture.write_text("{}", encoding="utf-8")
    received: list[object] = []

    def verify(*values: object) -> dict[str, object]:
        received.extend(values)
        return {"result": "passed"}

    monkeypatch.setattr(verifier, "verify_predecessor_rollback_readback", verify)
    assert (
        verifier.main(
            [
                "rollback",
                "--predecessor-processed-json",
                str(capture),
                "--rollback-processed-json",
                str(capture),
                "--predecessor-stack-json",
                str(capture),
                "--rollback-stack-json",
                str(capture),
                "--predecessor-lambda-configurations-json",
                str(capture),
                "--rollback-lambda-configurations-json",
                str(capture),
                "--predecessor-lambda-concurrencies-json",
                str(capture),
                "--rollback-lambda-concurrencies-json",
                str(capture),
                "--safety-json",
                str(capture),
            ]
        )
        == 0
    )
    assert received == [{}] * 9
    assert json.loads(capsys.readouterr().out) == {"result": "passed"}
