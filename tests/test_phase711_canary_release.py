"""Deterministic, exact-bound Phase 7.11 canary release checks."""

from __future__ import annotations

import ast
import base64
import copy
import csv
import json
import subprocess
import sys
import zipfile
from datetime import UTC, datetime
from hashlib import sha256
from io import StringIO
from pathlib import Path
from typing import Any

import pytest

import mr_lister.release.phase6 as phase6_release
from mr_lister.publication.canary_runtime import (
    PublicationCanaryBinding,
    PublicationCanaryMode,
)
from mr_lister.publication.execution_fingerprints import execution_record_fingerprint
from mr_lister.release.phase6 import (
    DEPENDENCY_BUILD_REQUEST_FILENAME,
    render_manifest,
    verify_dependency_build_request,
    wheel_authority_from_build_request,
)
from mr_lister.release.phase7_canary import (
    APPLICATION_RELEASE_FINGERPRINT_ENV,
    CANARY_BINDING_FILENAME,
    CANARY_BINDING_FINGERPRINT_ENV,
    CANARY_ENTRYPOINT,
    CANARY_PROFILE_FINGERPRINT,
    CANARY_RELEASE_FINGERPRINT_ENV,
    PHASE6_LAMBDA_WHEEL_AUTHORITY_FINGERPRINT,
    SOURCE_MANIFEST_FILENAME,
    Phase7CanaryReleaseAuthorityError,
    inventory,
    verify_phase7_canary_release,
    verify_phase7_canary_source_manifest,
)
from tools.build_phase66_source_bundles import LAMBDA_DEPENDENCY_DIRECTORY_NAME
from tools.build_phase711_canary_release import (
    CANARY_ARCHIVE_FILENAME,
    CANARY_ARTIFACT_DIRECTORY_NAME,
    CANARY_DEPENDENCY_DIRECTORY_NAME,
    CANARY_DEPLOYMENT_DIRECTORY_NAME,
    CANARY_SOURCE_DIRECTORY_NAME,
    Phase711CanaryReleaseError,
    build_canary_source_bundle,
    resolve_canary_import_closure,
    seal_canary_release,
    verify_canary_deployment_artifact,
    write_linux_arm64_dependency_manifest,
)
from tools.verify_phase716_canary_deployment import (
    Phase716CanaryDeploymentError,
    verify_canary_deployment_observations,
)

ROOT = Path(__file__).resolve().parents[1]


def _binding(
    tmp_path: Path,
    *,
    mode: PublicationCanaryMode = PublicationCanaryMode.READ_ONLY_PREFLIGHT,
) -> tuple[Path, PublicationCanaryBinding]:
    proof = "b" * 64 if mode is PublicationCanaryMode.PUBLISH_ONCE else None
    values: dict[str, object] = {
        "mode": mode,
        "owner_id_digest": "1" * 64,
        "aggregate_id_digest": "2" * 64,
        "job_id_digest": "3" * 64,
        "snapshot_fingerprint": "4" * 64,
        "permit_id_digest": "5" * 64,
        "work_request_id_digest": "6" * 64,
        "work_input_fingerprint": "7" * 64,
        "release_manifest_fingerprint": "8" * 64,
        "verification_deadline": datetime(2030, 1, 2, 3, 4, 5, tzinfo=UTC),
        "required_preflight_proof_fingerprint": proof,
    }
    binding = PublicationCanaryBinding(
        **values,
        fingerprint=execution_record_fingerprint("publication_canary_binding", values),
    )
    path = tmp_path / CANARY_BINDING_FILENAME
    path.write_bytes(render_manifest(binding.model_dump(mode="json")))
    return path, binding


def _source(tmp_path: Path, name: str, binding_path: Path) -> Path:
    return build_canary_source_bundle(
        tmp_path / name / CANARY_SOURCE_DIRECTORY_NAME,
        canary_binding_path=binding_path,
    )


def _synthetic_arm64_elf() -> bytes:
    value = bytearray(4_096)
    value[:7] = b"\x7fELF\x02\x01\x01"
    value[16:18] = (3).to_bytes(2, "little")
    value[18:20] = (183).to_bytes(2, "little")
    value[20:24] = (1).to_bytes(4, "little")
    value[52:54] = (64).to_bytes(2, "little")
    return bytes(value)


def _dependencies(
    tmp_path: Path,
    source: Path,
    name: str,
    monkeypatch: pytest.MonkeyPatch,
) -> Path:
    root = tmp_path / name / CANARY_DEPENDENCY_DIRECTORY_NAME
    root.mkdir(parents=True)
    request_path = source / DEPENDENCY_BUILD_REQUEST_FILENAME
    request = verify_dependency_build_request(request_path)
    requirements = request["requirements"]
    assert isinstance(requirements, dict)
    wheels = requirements["wheel_artifacts"]
    assert isinstance(wheels, list)
    import_roots = {
        "annotated-types": "annotated_types",
        "awscrt": "awscrt",
        "boto3": "boto3",
        "botocore": "botocore",
        "jmespath": "jmespath",
        "pillow": "PIL",
        "pydantic": "pydantic",
        "pydantic-core": "pydantic_core",
        "python-dateutil": "dateutil",
        "s3transfer": "s3transfer",
        "six": "six.py",
        "typing-extensions": "typing_extensions.py",
        "typing-inspection": "typing_inspection",
        "urllib3": "urllib3",
    }
    native_names = {"awscrt", "pillow", "pydantic-core"}
    owned: dict[str, list[Path]] = {}
    dist_infos: dict[str, Path] = {}
    for wheel in wheels:
        assert isinstance(wheel, dict)
        distribution = wheel["name"]
        version = wheel["version"]
        assert isinstance(distribution, str) and isinstance(version, str)
        package = root / import_roots[distribution]
        if package.suffix == ".py":
            package.write_text("VERSION = 'test'\n", encoding="utf-8")
            package_files = [package]
        else:
            package.mkdir()
            initializer = package / "__init__.py"
            initializer.write_text("VERSION = 'test'\n", encoding="utf-8")
            package_files = [initializer]
        dist_info = root / f"{distribution.replace('-', '_')}-{version}.dist-info"
        dist_info.mkdir()
        metadata = dist_info / "METADATA"
        metadata.write_text(
            f"Metadata-Version: 2.4\nName: {distribution}\nVersion: {version}\n\n",
            encoding="utf-8",
        )
        wheel_metadata = dist_info / "WHEEL"
        tag = (
            "cp312-cp312-manylinux_2_17_aarch64.manylinux2014_aarch64"
            if distribution in native_names
            else "py3-none-any"
        )
        wheel_metadata.write_text(
            "Wheel-Version: 1.0\nGenerator: phase711-test\n"
            f"Root-Is-Purelib: {'false' if distribution in native_names else 'true'}\n"
            f"Tag: {tag}\n\n",
            encoding="utf-8",
        )
        owned[distribution] = [*package_files, metadata, wheel_metadata]
        dist_infos[distribution] = dist_info

    native_paths = {
        "awscrt": root / "_awscrt.abi3.so",
        "pillow": root / "PIL/_imaging.cpython-312-aarch64-linux-gnu.so",
        "pydantic-core": (root / "pydantic_core/_pydantic_core.cpython-312-aarch64-linux-gnu.so"),
    }
    for distribution, native in native_paths.items():
        native.write_bytes(_synthetic_arm64_elf())
        owned[distribution].append(native)

    for distribution in sorted(owned):
        record = dist_infos[distribution] / "RECORD"
        rows: list[list[str]] = []
        for path in sorted(owned[distribution]):
            raw = path.read_bytes()
            encoded = base64.urlsafe_b64encode(sha256(raw).digest()).decode().rstrip("=")
            rows.append([path.relative_to(root).as_posix(), f"sha256={encoded}", str(len(raw))])
        rows.append([record.relative_to(root).as_posix(), "", ""])
        output = StringIO(newline="")
        csv.writer(output, lineterminator="\n").writerows(rows)
        record.write_text(output.getvalue(), encoding="utf-8")

    expected_tree = requirements["dependency_tree_sha256"]
    assert isinstance(expected_tree, str)
    monkeypatch.setattr(
        phase6_release,
        "_dependency_tree_fingerprint",
        lambda _files: expected_tree,
    )
    write_linux_arm64_dependency_manifest(root, build_request_path=request_path)
    return root


def _sealed(
    tmp_path: Path,
    name: str,
    binding_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):  # type: ignore[no-untyped-def]
    source = _source(tmp_path, f"{name}-source", binding_path)
    dependencies = _dependencies(tmp_path, source, f"{name}-deps", monkeypatch)
    return seal_canary_release(
        source,
        dependencies=dependencies,
        deployment_destination=tmp_path / name / CANARY_DEPLOYMENT_DIRECTORY_NAME,
        artifact_destination=tmp_path / name / CANARY_ARTIFACT_DIRECTORY_NAME,
    )


def _manifest(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_bytes())
    assert isinstance(value, dict)
    return value


def _deployment_observations(artifact, binding: PublicationCanaryBinding) -> dict[str, Any]:  # type: ignore[no-untyped-def]
    environment = "dev"
    region = "us-west-2"
    account = "123456789012"
    stack_name = "mr-lister-phase7-canary-dev"
    function_name = "mr-lister-phase7-dev-publication-canary"
    role_name = f"{function_name}-role"
    bucket = "mr-lister-phase7-artifacts-dev"
    key = f"phase7/releases/{artifact.release_fingerprint}/canary.zip"
    version = "v1.token-2"
    secret_arn = f"arn:aws:secretsmanager:{region}:{account}:secret:mr-lister/printify/dev-Ab12Cd"
    archive = artifact.archive_path.read_bytes()
    function_arn = f"arn:aws:lambda:{region}:{account}:function:{function_name}"
    parameters = {
        "ApplicationReleaseFingerprint": artifact.application_release_fingerprint,
        "CanaryBindingFingerprint": artifact.binding_fingerprint,
        "CanaryCodeS3Bucket": bucket,
        "CanaryCodeS3Key": key,
        "CanaryCodeS3ObjectVersion": version,
        "CanaryMode": artifact.binding_mode,
        "CanaryReleaseFingerprint": artifact.release_fingerprint,
        "EnvironmentName": environment,
        "PrintifySecretArn": secret_arn,
    }
    outputs = {
        "ApplicationReleaseFingerprint": artifact.application_release_fingerprint,
        "PublicationCanaryBindingFingerprint": artifact.binding_fingerprint,
        "PublicationCanaryFunctionArn": function_arn,
        "PublicationCanaryMode": artifact.binding_mode,
        "PublicationCanaryReleaseFingerprint": artifact.release_fingerprint,
        "SellerPublicationEnabled": "false",
    }
    table_arn = f"arn:aws:dynamodb:{region}:{account}:table/mr-lister-phase6-dev"
    leading_keys = {"ForAllValues:StringLike": {"dynamodb:LeadingKeys": ["JOB#*", "PUBLICATION#*"]}}
    policy = {
        "Statement": [
            {
                "Action": ["logs:CreateLogStream", "logs:PutLogEvents"],
                "Effect": "Allow",
                "Resource": (
                    f"arn:aws:logs:{region}:{account}:log-group:/aws/lambda/{function_name}:*"
                ),
                "Sid": "WritePublicationCanaryLogs",
            },
            {
                "Action": ["dynamodb:GetItem", "dynamodb:Query"],
                "Condition": leading_keys,
                "Effect": "Allow",
                "Resource": table_arn,
                "Sid": "ReadExactPublicationAuthority",
            },
            {
                "Action": "dynamodb:ConditionCheckItem",
                "Condition": leading_keys,
                "Effect": "Allow",
                "Resource": table_arn,
                "Sid": "CommitExactPublicationAuthorityConditionChecks",
            },
            {
                "Action": "dynamodb:PutItem",
                "Condition": {
                    **leading_keys,
                    "ForAnyValue:StringEquals": {
                        "dynamodb:EnclosingOperation": ["TransactWriteItems"]
                    },
                },
                "Effect": "Allow",
                "Resource": table_arn,
                "Sid": "CommitExactPublicationAuthorityPuts",
            },
            {
                "Action": "secretsmanager:GetSecretValue",
                "Effect": "Allow",
                "Resource": secret_arn,
                "Sid": "ReadExactPublicationCredential",
            },
        ],
        "Version": "2012-10-17",
    }
    environment_variables = {
        "MR_LISTER_AWS_ACCOUNT_ID": account,
        "MR_LISTER_ENVIRONMENT": environment,
        "MR_LISTER_PHASE7_CANARY_BINDING_FINGERPRINT": artifact.binding_fingerprint,
        "MR_LISTER_PHASE7_CANARY_ENABLED": "true",
        "MR_LISTER_PHASE7_CANARY_MODE": artifact.binding_mode,
        "MR_LISTER_PHASE7_CANARY_RELEASE_FINGERPRINT": artifact.release_fingerprint,
        "MR_LISTER_PHASE7_PUBLICATION_ENABLED": "false",
        "MR_LISTER_PHASE7_QUERY_ENABLED": "false",
        "MR_LISTER_PHASE7_REQUEST_ENABLED": "false",
        "MR_LISTER_PHASE7_SCAFFOLD_ONLY": "false",
        "MR_LISTER_PRINTIFY_SECRET_ARN": secret_arn,
        "MR_LISTER_PRODUCT_PROFILE_FINGERPRINT": CANARY_PROFILE_FINGERPRINT,
        "MR_LISTER_PRODUCT_PROFILE_ID": "gildan_64000_swiftpod",
        "MR_LISTER_PRODUCT_PROFILE_PATH": (
            "/var/task/config/product_profiles/gildan_64000_swiftpod.json"
        ),
        "MR_LISTER_PRODUCT_PROFILE_VERSION": "2",
        "MR_LISTER_RELEASE_FINGERPRINT": artifact.application_release_fingerprint,
        "MR_LISTER_STATE_TABLE": "mr-lister-phase6-dev",
    }
    assert binding.fingerprint == artifact.binding_fingerprint
    return {
        "deployment_root": artifact.deployment_root,
        "archive_path": artifact.archive_path,
        "descriptor_path": artifact.descriptor_path,
        "head_observation": {
            "ChecksumSHA256": base64.b64encode(sha256(archive).digest()).decode("ascii"),
            "ContentLength": len(archive),
            "ContentType": "application/zip",
            "Metadata": {
                "mr-lister-archive-sha256": artifact.archive_fingerprint,
                "mr-lister-release-fingerprint": artifact.release_fingerprint,
            },
            "ServerSideEncryption": "AES256",
            "VersionId": version,
        },
        "stack_observation": {
            "Stacks": [
                {
                    "Outputs": [
                        {"OutputKey": name, "OutputValue": value} for name, value in outputs.items()
                    ],
                    "Parameters": [
                        {"ParameterKey": name, "ParameterValue": value}
                        for name, value in parameters.items()
                    ],
                    "StackId": (
                        f"arn:aws:cloudformation:{region}:{account}:stack/{stack_name}/stack-id"
                    ),
                    "StackName": stack_name,
                    "StackStatus": "CREATE_COMPLETE",
                }
            ]
        },
        "stack_resources_observation": {
            "StackResourceSummaries": [
                {
                    "LogicalResourceId": "PublicationCanaryLogGroup",
                    "PhysicalResourceId": f"/aws/lambda/{function_name}",
                    "ResourceStatus": "CREATE_COMPLETE",
                    "ResourceType": "AWS::Logs::LogGroup",
                },
                {
                    "LogicalResourceId": "PublicationCanaryFunctionRole",
                    "PhysicalResourceId": role_name,
                    "ResourceStatus": "CREATE_COMPLETE",
                    "ResourceType": "AWS::IAM::Role",
                },
                {
                    "LogicalResourceId": "PublicationCanaryFunction",
                    "PhysicalResourceId": function_name,
                    "ResourceStatus": "CREATE_COMPLETE",
                    "ResourceType": "AWS::Lambda::Function",
                },
            ]
        },
        "lambda_configuration_observation": {
            "Architectures": ["arm64"],
            "CodeSha256": base64.b64encode(sha256(archive).digest()).decode("ascii"),
            "CodeSize": len(archive),
            "Environment": {"Variables": environment_variables},
            "FunctionArn": function_arn,
            "FunctionName": function_name,
            "Handler": CANARY_ENTRYPOINT,
            "LastUpdateStatus": "Successful",
            "LoggingConfig": {
                "ApplicationLogLevel": "ERROR",
                "LogFormat": "JSON",
                "LogGroup": f"/aws/lambda/{function_name}",
                "SystemLogLevel": "WARN",
            },
            "MemorySize": 512,
            "PackageType": "Zip",
            "Role": f"arn:aws:iam::{account}:role/{role_name}",
            "Runtime": "python3.12",
            "State": "Active",
            "Timeout": 60,
            "Version": "$LATEST",
        },
        "lambda_concurrency_observation": {"ReservedConcurrentExecutions": 1},
        "role_observation": {
            "Role": {
                "Arn": f"arn:aws:iam::{account}:role/{role_name}",
                "AssumeRolePolicyDocument": {
                    "Statement": [
                        {
                            "Action": "sts:AssumeRole",
                            "Effect": "Allow",
                            "Principal": {"Service": "lambda.amazonaws.com"},
                        }
                    ],
                    "Version": "2012-10-17",
                },
                "CreateDate": "2026-09-02T12:00:00+00:00",
                "Description": "",
                "MaxSessionDuration": 3600,
                "Path": "/",
                "RoleId": "A" * 20,
                "RoleName": role_name,
                "Tags": [
                    {"Key": "Project", "Value": "MrLister"},
                    {"Key": "Environment", "Value": environment},
                    {"Key": "Phase", "Value": "7-isolated-canary"},
                ],
            }
        },
        "inline_policy_observation": {
            "PolicyDocument": policy,
            "PolicyName": "ExactBoundPublicationCanary",
            "RoleName": role_name,
        },
        "inline_policy_list_observation": {
            "IsTruncated": False,
            "PolicyNames": ["ExactBoundPublicationCanary"],
        },
        "attached_policy_list_observation": {
            "AttachedPolicies": [],
            "IsTruncated": False,
        },
        "event_source_observation": {"EventSourceMappings": []},
        "versions_observation": {
            "Versions": [{"FunctionName": function_name, "Version": "$LATEST"}]
        },
        "aliases_observation": {"Aliases": []},
        "url_configs_observation": {"FunctionUrlConfigs": []},
        "absence_observation": {
            "function_name": function_name,
            "get_function_event_invoke_config": {
                "error_code": "ResourceNotFoundException",
                "http_status_code": 404,
            },
            "get_function_url_config": {
                "error_code": "ResourceNotFoundException",
                "http_status_code": 404,
            },
            "get_policy": {
                "error_code": "ResourceNotFoundException",
                "http_status_code": 404,
            },
        },
        "expected_mode": artifact.binding_mode,
        "stack_name": stack_name,
        "environment_name": environment,
        "bucket": bucket,
        "key": key,
        "version_id": version,
        "printify_secret_arn": secret_arn,
        "region": region,
        "account_id": account,
    }


def test_source_bundle_is_deterministic_sanitized_and_narrow(tmp_path: Path) -> None:
    binding_path, binding = _binding(tmp_path)
    first = _source(tmp_path, "first", binding_path)
    second = _source(tmp_path, "second", binding_path)
    closure = resolve_canary_import_closure()

    assert (first / SOURCE_MANIFEST_FILENAME).read_bytes() == (
        second / SOURCE_MANIFEST_FILENAME
    ).read_bytes()
    payload = verify_phase7_canary_source_manifest(first)
    assert payload == binding.model_dump(mode="json")
    manifest = _manifest(first / SOURCE_MANIFEST_FILENAME)
    assert manifest["entrypoint"] == CANARY_ENTRYPOINT
    assert manifest["binding"] == {
        "fingerprint": binding.fingerprint,
        "mode": binding.mode.value,
        "path": CANARY_BINDING_FILENAME,
        "release_manifest_fingerprint": binding.release_manifest_fingerprint,
        "sha256": sha256((first / CANARY_BINDING_FILENAME).read_bytes()).hexdigest(),
    }
    assert manifest["profile"]["fingerprint"] == CANARY_PROFILE_FINGERPRINT
    assert manifest["profile"]["publish_enabled"] is False
    assert manifest["files"] == inventory(
        first,
        excluded=frozenset({SOURCE_MANIFEST_FILENAME}),
    )
    assert not any(module.startswith("mr_lister.production") for module in closure)
    assert not any(
        module.startswith(prefix)
        for module in closure
        for prefix in ("mr_lister.agent", "mr_lister.api", "mr_lister.intelligence")
    )
    assert "mr_lister.release.phase6" in closure
    assert "mr_lister.release.phase7_canary" in closure
    capability_free_initializers = {
        "mr_lister/__init__.py",
        "mr_lister/cloud/__init__.py",
        "mr_lister/control/__init__.py",
        "mr_lister/publication/__init__.py",
        "mr_lister/release/__init__.py",
        "mr_lister/workflow/__init__.py",
    }
    assert all((first / relative).read_bytes() == b"" for relative in capability_free_initializers)

    combined = b"".join(path.read_bytes() for path in sorted(first.rglob("*")) if path.is_file())
    assert str(ROOT).encode() not in combined
    assert b"/Users/" not in combined
    assert b"owner-canary-raw" not in combined


def test_source_uses_exact_checked_phase6_lambda_wheel_authority(tmp_path: Path) -> None:
    binding_path, _binding_value = _binding(tmp_path)
    source = _source(tmp_path, "authority", binding_path)
    authority = wheel_authority_from_build_request(source / DEPENDENCY_BUILD_REQUEST_FILENAME)

    assert authority["component"] == "lambda"
    assert sha256(render_manifest(authority)).hexdigest() == (
        PHASE6_LAMBDA_WHEEL_AUTHORITY_FINGERPRINT
    )
    assert (source / "requirements.txt").read_text(encoding="utf-8").count("--hash=sha256:") == 14
    assert CANARY_DEPENDENCY_DIRECTORY_NAME == LAMBDA_DEPENDENCY_DIRECTORY_NAME


def test_release_is_byte_deterministic_and_separates_application_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    binding_path, binding = _binding(tmp_path)
    first = _sealed(tmp_path, "first", binding_path, monkeypatch)
    second = _sealed(tmp_path, "second", binding_path, monkeypatch)

    assert first.release_fingerprint == second.release_fingerprint
    assert first.archive_fingerprint == second.archive_fingerprint
    assert first.archive_path.read_bytes() == second.archive_path.read_bytes()
    assert first.release_fingerprint != binding.release_manifest_fingerprint
    assert first.application_release_fingerprint == binding.release_manifest_fingerprint
    assert first.binding_fingerprint == binding.fingerprint
    assert first.binding_mode == binding.mode.value
    assert first.profile_fingerprint == CANARY_PROFILE_FINGERPRINT

    verified = verify_phase7_canary_release(
        {
            CANARY_RELEASE_FINGERPRINT_ENV: first.release_fingerprint,
            APPLICATION_RELEASE_FINGERPRINT_ENV: binding.release_manifest_fingerprint,
            CANARY_BINDING_FINGERPRINT_ENV: binding.fingerprint,
        },
        bundle_root=first.deployment_root,
    )
    assert verified.binding_payload == binding.model_dump(mode="json")
    assert verified.binding_fingerprint == binding.fingerprint
    assert verified.binding_mode == binding.mode.value
    assert type(verified.binding_payload) is dict

    raw_descriptor = first.descriptor_path.read_bytes()
    assert str(ROOT).encode() not in raw_descriptor
    assert b"/Users/" not in raw_descriptor
    verify_canary_deployment_artifact(
        first.deployment_root,
        archive_path=first.archive_path,
        descriptor_path=first.descriptor_path,
    )
    with zipfile.ZipFile(first.archive_path) as archive:
        members = archive.infolist()
        assert archive.filename == first.archive_path.as_posix()
        names = [member.filename for member in members]
        assert names == sorted(names)
        assert not any(name.startswith("mr_lister/production/") for name in names)
        packaged_bytes = b"".join(archive.read(member) for member in members)
        assert str(ROOT).encode() not in packaged_bytes
        assert b"/Users/" not in packaged_bytes
        assert all(member.date_time == (1980, 1, 1, 0, 0, 0) for member in members)
        assert all(member.compress_type == zipfile.ZIP_STORED for member in members)
        assert all(member.external_attr == 0o100644 << 16 for member in members)
        assert CANARY_ARCHIVE_FILENAME == first.archive_path.name


def test_packaged_release_verifier_imports_no_publication_code_before_verification(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    binding_path, _binding_value = _binding(tmp_path)
    artifact = _sealed(tmp_path, "release-first", binding_path, monkeypatch)
    source = artifact.deployment_root / "mr_lister/release/phase7_canary.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))
    imported = {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module is not None
    }
    assert not any(name.startswith("mr_lister.publication") for name in imported)

    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sys; import mr_lister.release.phase7_canary; "
                "assert not any(n.startswith('mr_lister.publication') for n in sys.modules)"
            ),
        ],
        cwd=tmp_path,
        env={"PYTHONPATH": str(artifact.deployment_root)},
        capture_output=True,
        check=False,
        text=True,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("drift", ["release", "application", "binding", "source"])
def test_release_refuses_environment_or_byte_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    drift: str,
) -> None:
    binding_path, binding = _binding(tmp_path)
    artifact = _sealed(tmp_path, drift, binding_path, monkeypatch)
    environment = {
        CANARY_RELEASE_FINGERPRINT_ENV: artifact.release_fingerprint,
        APPLICATION_RELEASE_FINGERPRINT_ENV: binding.release_manifest_fingerprint,
        CANARY_BINDING_FINGERPRINT_ENV: binding.fingerprint,
    }
    if drift == "release":
        environment[CANARY_RELEASE_FINGERPRINT_ENV] = "a" * 64
    elif drift == "application":
        environment[APPLICATION_RELEASE_FINGERPRINT_ENV] = "a" * 64
    elif drift == "binding":
        environment[CANARY_BINDING_FINGERPRINT_ENV] = "a" * 64
    else:
        target = artifact.deployment_root / "mr_lister/publication/canary_runtime.py"
        target.write_bytes(target.read_bytes() + b"\n# drift\n")

    with pytest.raises(Phase7CanaryReleaseAuthorityError) as captured:
        verify_phase7_canary_release(environment, bundle_root=artifact.deployment_root)
    assert str(captured.value) == "Phase 7 canary release authority is invalid"
    assert captured.value.__cause__ is None


def test_source_rejects_a_canonical_but_semantically_invalid_binding(tmp_path: Path) -> None:
    binding_path, binding = _binding(tmp_path)
    payload = binding.model_dump(mode="json")
    payload["fingerprint"] = "a" * 64
    binding_path.write_bytes(render_manifest(payload))

    with pytest.raises(Phase711CanaryReleaseError):
        _source(tmp_path, "invalid-binding", binding_path)


def test_read_only_canary_deployment_readback_is_exact_and_sanitized(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    binding_path, binding = _binding(tmp_path)
    artifact = _sealed(tmp_path, "deployment-readback", binding_path, monkeypatch)
    observations = _deployment_observations(artifact, binding)

    verified = verify_canary_deployment_observations(**observations)

    assert verified.stack_name == "mr-lister-phase7-canary-dev"
    assert verified.function_name == "mr-lister-phase7-dev-publication-canary"
    assert verified.release_fingerprint == artifact.release_fingerprint
    assert verified.application_release_fingerprint == artifact.application_release_fingerprint
    assert verified.binding_fingerprint == artifact.binding_fingerprint
    assert verified.binding_mode == PublicationCanaryMode.READ_ONLY_PREFLIGHT.value
    assert verified.archive_fingerprint == artifact.archive_fingerprint
    assert observations["printify_secret_arn"] not in repr(verified)


def test_read_only_canary_deployment_readback_rejects_every_authority_expansion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    binding_path, binding = _binding(tmp_path)
    artifact = _sealed(tmp_path, "deployment-adversarial", binding_path, monkeypatch)
    pristine = _deployment_observations(artifact, binding)

    for drift in (
        "s3_version",
        "mutable_version_label",
        "s3_checksum",
        "s3_metadata",
        "shared_stack_name",
        "stack_binding",
        "stack_publication_output",
        "fourth_api_resource",
        "release_environment",
        "application_environment",
        "binding_environment",
        "mode_environment",
        "expected_mode",
        "concurrency",
        "cross_account_secret",
        "role_description",
        "role_boundary",
        "iam_action",
        "attached_policy",
        "event_source",
        "function_url",
        "resource_policy",
    ):
        observations = copy.deepcopy(pristine)
        if drift == "s3_version":
            observations["head_observation"]["VersionId"] = "different-version"
        elif drift == "mutable_version_label":
            observations["version_id"] = "latest"
            observations["head_observation"]["VersionId"] = "latest"
            parameters = observations["stack_observation"]["Stacks"][0]["Parameters"]
            next(
                item for item in parameters if item["ParameterKey"] == "CanaryCodeS3ObjectVersion"
            )["ParameterValue"] = "latest"
        elif drift == "s3_checksum":
            observations["head_observation"]["ChecksumSHA256"] = "wrong"
        elif drift == "s3_metadata":
            observations["head_observation"]["Metadata"]["mr-lister-release-fingerprint"] = "a" * 64
        elif drift == "shared_stack_name":
            observations["stack_name"] = "mr-lister-phase7-dev"
            stack = observations["stack_observation"]["Stacks"][0]
            stack["StackName"] = "mr-lister-phase7-dev"
            stack["StackId"] = (
                "arn:aws:cloudformation:us-west-2:123456789012:stack/mr-lister-phase7-dev/stack-id"
            )
        elif drift == "stack_binding":
            parameters = observations["stack_observation"]["Stacks"][0]["Parameters"]
            next(item for item in parameters if item["ParameterKey"] == "CanaryBindingFingerprint")[
                "ParameterValue"
            ] = "a" * 64
        elif drift == "stack_publication_output":
            outputs = observations["stack_observation"]["Stacks"][0]["Outputs"]
            next(item for item in outputs if item["OutputKey"] == "SellerPublicationEnabled")[
                "OutputValue"
            ] = "true"
        elif drift == "fourth_api_resource":
            observations["stack_resources_observation"]["StackResourceSummaries"].append(
                {
                    "LogicalResourceId": "UnexpectedApi",
                    "PhysicalResourceId": "api-id",
                    "ResourceStatus": "CREATE_COMPLETE",
                    "ResourceType": "AWS::ApiGatewayV2::Api",
                }
            )
        elif drift in {
            "release_environment",
            "application_environment",
            "binding_environment",
            "mode_environment",
        }:
            variable = {
                "release_environment": "MR_LISTER_PHASE7_CANARY_RELEASE_FINGERPRINT",
                "application_environment": "MR_LISTER_RELEASE_FINGERPRINT",
                "binding_environment": "MR_LISTER_PHASE7_CANARY_BINDING_FINGERPRINT",
                "mode_environment": "MR_LISTER_PHASE7_CANARY_MODE",
            }[drift]
            observations["lambda_configuration_observation"]["Environment"]["Variables"][
                variable
            ] = "publish_once" if drift == "mode_environment" else "a" * 64
        elif drift == "expected_mode":
            observations["expected_mode"] = "publish_once"
        elif drift == "concurrency":
            observations["lambda_concurrency_observation"]["ReservedConcurrentExecutions"] = 0
        elif drift == "cross_account_secret":
            foreign_secret = observations["printify_secret_arn"].replace(
                ":123456789012:", ":999999999999:"
            )
            observations["printify_secret_arn"] = foreign_secret
            parameters = observations["stack_observation"]["Stacks"][0]["Parameters"]
            next(item for item in parameters if item["ParameterKey"] == "PrintifySecretArn")[
                "ParameterValue"
            ] = foreign_secret
            observations["lambda_configuration_observation"]["Environment"]["Variables"][
                "MR_LISTER_PRINTIFY_SECRET_ARN"
            ] = foreign_secret
            observations["inline_policy_observation"]["PolicyDocument"]["Statement"][4][
                "Resource"
            ] = foreign_secret
        elif drift == "role_description":
            observations["role_observation"]["Role"]["Description"] = "unexpected"
        elif drift == "role_boundary":
            observations["role_observation"]["Role"]["PermissionsBoundary"] = {
                "PermissionsBoundaryArn": "arn:aws:iam::123456789012:policy/unreviewed",
                "PermissionsBoundaryType": "Policy",
            }
        elif drift == "iam_action":
            observations["inline_policy_observation"]["PolicyDocument"]["Statement"][1][
                "Action"
            ].append("dynamodb:Scan")
        elif drift == "attached_policy":
            observations["attached_policy_list_observation"]["AttachedPolicies"].append(
                {
                    "PolicyArn": "arn:aws:iam::aws:policy/AdministratorAccess",
                    "PolicyName": "AdministratorAccess",
                }
            )
        elif drift == "event_source":
            observations["event_source_observation"]["EventSourceMappings"].append(
                {"UUID": "unexpected-trigger"}
            )
        elif drift == "function_url":
            observations["url_configs_observation"]["FunctionUrlConfigs"].append(
                {"AuthType": "NONE", "FunctionUrl": "https://example.invalid"}
            )
        else:
            observations["absence_observation"]["get_policy"] = {
                "error_code": "Success",
                "http_status_code": 200,
            }

        with pytest.raises(Phase716CanaryDeploymentError) as captured:
            verify_canary_deployment_observations(**observations)
        assert str(captured.value) == "Phase 7 canary deployment observation is invalid"
        assert captured.value.__cause__ is None
        assert pristine["printify_secret_arn"] not in str(captured.value)


def test_canary_deployment_verifier_requires_the_explicit_exact_publish_once_mode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    binding_path, binding = _binding(tmp_path, mode=PublicationCanaryMode.PUBLISH_ONCE)
    artifact = _sealed(tmp_path, "publish-once-deployment", binding_path, monkeypatch)
    observations = _deployment_observations(artifact, binding)

    verified = verify_canary_deployment_observations(**observations)
    assert verified.binding_mode == PublicationCanaryMode.PUBLISH_ONCE.value

    observations["expected_mode"] = PublicationCanaryMode.READ_ONLY_PREFLIGHT.value
    with pytest.raises(Phase716CanaryDeploymentError) as captured:
        verify_canary_deployment_observations(**observations)
    assert str(captured.value) == "Phase 7 canary deployment observation is invalid"
    assert captured.value.__cause__ is None
