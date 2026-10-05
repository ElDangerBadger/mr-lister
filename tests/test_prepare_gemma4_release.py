"""Offline release scope checks against representative SAM and processed shapes."""

from __future__ import annotations

import json
from copy import deepcopy

import pytest

from tools import prepare_gemma4_release as release

ACCOUNT = "123456789012"
REGION = "us-west-2"
ENVIRONMENT = "test"
BUCKET = f"mr-lister-phase6-artifacts-{ENVIRONMENT}-{ACCOUNT}-{REGION}"
RUNTIME_ID = "mr_lister_phase6-ABCDEFGHIJ"
RUNTIME_ARN = f"arn:aws:bedrock-agentcore:{REGION}:{ACCOUNT}:runtime/{RUNTIME_ID}"
OLD_RELEASE = "a" * 64
NEW_RELEASE = "b" * 64


def archive(component):
    digest = ("c" if component == "lambda" else "d") * 64
    return {
        "bucket": BUCKET,
        "key": f"private/deployments/{component}/releases/{NEW_RELEASE}/"
        f"phase6-{component}-{digest}.zip",
        "version": f"immutable-{component}-version",
        "sha256": digest,
        "size_bytes": 123456,
        "release_fingerprint": NEW_RELEASE,
    }


@pytest.fixture
def inputs():
    old_binding = release._binding(RUNTIME_ARN, ENVIRONMENT, "8", OLD_RELEASE)
    env = {
        "AWS_REGION": REGION,
        "MR_LISTER_AWS_ACCOUNT_ID": ACCOUNT,
        "MR_LISTER_ENVIRONMENT": ENVIRONMENT,
        "MR_LISTER_ARTIFACT_BUCKET": BUCKET,
        release.RELEASE_ENV: OLD_RELEASE,
        "MR_LISTER_GEMMA_CONFIG_PATH": "/var/task/config/bedrock/google_gemma_3_27b_it.json",
        "MR_LISTER_GEMMA_CONFIG_FINGERPRINT": "e" * 64,
        "MR_LISTER_STRANDS_CONTROLLER_MODEL_ID": "us.amazon.nova-2-lite-v1:0",
        "MR_LISTER_JUDGE_PRICING_POLICY": '{"default_price_cents":3500}',
    }
    runtime = {
        "agentRuntimeId": RUNTIME_ID,
        "agentRuntimeArn": RUNTIME_ARN,
        "agentRuntimeVersion": "8",
        "status": "READY",
        "environmentVariables": env,
        "roleArn": f"arn:aws:iam::{ACCOUNT}:role/existing-runtime",
        "networkConfiguration": {"networkMode": "PUBLIC"},
        "protocolConfiguration": {"serverProtocol": "HTTP"},
        "lifecycleConfiguration": {"idleRuntimeSessionTimeout": 900, "maxLifetime": 3600},
        "agentRuntimeArtifact": {
            "codeConfiguration": {
                "runtime": "PYTHON_3_12",
                "entryPoint": ["main.py"],
                "code": {
                    "s3": {
                        "bucket": BUCKET,
                        "prefix": "old-runtime.zip",
                        "versionId": "old-version",
                    }
                },
            }
        },
        "ResponseMetadata": {"HTTPStatusCode": 200},
    }
    resources = {
        "SellerHttpApi": {
            "Type": "AWS::Serverless::HttpApi",
            "Properties": {
                "DefinitionBody": {"paths": {"/v1/jobs": {"post": {"authorization": "JWT"}}}},
            },
        },
        "UploadApiFunction": {
            "Type": "AWS::Serverless::Function",
            "Properties": {
                "CodeUri": {
                    "Bucket": BUCKET,
                    "Key": "template-old-upload.zip",
                    "Version": "template-version",
                },
                "Environment": {"Variables": {release.RELEASE_ENV: "f" * 64}},
            },
        },
        "HistoryClearRoute": {
            "Type": "AWS::ApiGatewayV2::Route",
            "Properties": {
                "RouteKey": "POST /v1/jobs/recent/clear",
                "AuthorizationType": "JWT",
            },
        },
        "PreparationDispatchFunctionRole": {
            "Type": "AWS::IAM::Role",
            "Properties": {
                "Policies": [
                    {
                        "PolicyDocument": {
                            "Statement": [
                                {
                                    "Resource": [
                                        {"Ref": "AgentCoreRuntimeArn"},
                                        {"Ref": "AgentCoreRuntimeEndpointArn"},
                                    ]
                                }
                            ]
                        }
                    }
                ],
            },
        },
    }
    for name in release.TARGETS:
        fn_env = {release.RELEASE_ENV: OLD_RELEASE if name == release.TARGETS[0] else "f" * 64}
        if name == release.TARGETS[0]:
            fn_env.update(
                {
                    "MR_LISTER_AGENTCORE_RUNTIME_" + suffix: {"Ref": key}
                    for key, suffix in release.PARAMETER_SUFFIXES.items()
                }
            )
        resources[name] = {
            "Type": "AWS::Serverless::Function",
            "Properties": {
                "CodeUri": {"Bucket": BUCKET, "Key": "old.zip", "Version": "old-version"},
                "Environment": {"Variables": fn_env},
                "Events": {"Existing": {"Type": "HttpApi", "Properties": {"Path": "/v1/jobs"}}},
                "MemorySize": 1024,
                "Timeout": 600,
            },
        }
    original = {
        "Transform": "AWS::Serverless-2016-10-31",
        "Globals": {
            "Function": {
                "Environment": {"Variables": {release.RELEASE_ENV: "9" * 64}},
            }
        },
        "Parameters": {
            key: {"Type": "String", "Default": value, "AllowedValues": [value]}
            for key, value in old_binding.items()
        },
        "Resources": resources,
        "Metadata": {"prior": "preserved"},
        "Outputs": {"Old": 42},
    }
    processed = deepcopy(original)
    processed.pop("Transform")
    processed.pop("Globals")
    for name in release.TARGETS:
        resource = processed["Resources"][name]
        resource["Type"] = "AWS::Lambda::Function"
        props = resource["Properties"]
        code = props.pop("CodeUri")
        props["Code"] = dict(
            zip(("S3Bucket", "S3Key", "S3ObjectVersion"), code.values(), strict=True)
        )
        props.pop("Events")
    return {
        "original": original,
        "processed": processed,
        "runtime": runtime,
        "lambda_archive": archive("lambda"),
        "agentcore_archive": archive("agentcore"),
        "runtime_version": "9",
        "config_path": release.CONFIG_PATH,
        "config_fingerprint": "8" * 64,
        "harness_revision": "v3",
        "harness_fingerprint": release.HARNESS_FINGERPRINT,
    }


def test_only_prep_query_code_release_and_existing_binding_parameters_change(inputs):
    snapshot = deepcopy(inputs)
    result = release.prepare_release(**inputs)
    assert inputs == snapshot
    for input_key, output_key in (
        ("original", "candidate-original.json"),
        ("processed", "expected-processed.json"),
    ):
        before, after = inputs[input_key], result[output_key]
        assert set(before) == set(after)
        for key in before.keys() - {"Resources", "Parameters"}:
            assert after[key] == before[key]
        assert set(after["Resources"]) == set(before["Resources"])
        for name, value in before["Resources"].items():
            if name not in release.TARGETS:
                assert after["Resources"][name] == value
                continue
            old, new = value["Properties"], after["Resources"][name]["Properties"]
            code_key = "CodeUri" if value["Type"] == "AWS::Serverless::Function" else "Code"
            for prop in old.keys() - {code_key, "Environment"}:
                assert new[prop] == old[prop]
            expected_env = deepcopy(old["Environment"])
            expected_env["Variables"][release.RELEASE_ENV] = NEW_RELEASE
            assert new["Environment"] == expected_env
        assert after["Parameters"]["AgentCoreRuntimeVersion"]["AllowedValues"] == ["9"]
    assert result["manifest.json"]["aws_calls"] == 0
    assert result["manifest.json"]["lambda_targets"] == list(release.TARGETS)


def test_runtime_preserves_judge_nova_role_network_and_only_changes_model_pins_and_archive(inputs):
    result = release.prepare_release(**inputs)
    update = result["runtime-update.json"]
    before = inputs["runtime"]
    assert "status" not in update and "ResponseMetadata" not in update
    for key in (
        "roleArn",
        "networkConfiguration",
        "protocolConfiguration",
        "lifecycleConfiguration",
    ):
        assert update[key] == before[key]
    expected_env = deepcopy(before["environmentVariables"])
    expected_env.update(
        {
            release.RELEASE_ENV: NEW_RELEASE,
            "MR_LISTER_GEMMA_CONFIG_PATH": release.CONFIG_PATH,
            "MR_LISTER_GEMMA_CONFIG_FINGERPRINT": inputs["config_fingerprint"],
            "MR_LISTER_HARNESS_REVISION": "v3",
            "MR_LISTER_HARNESS_PROMPT_FINGERPRINT": release.HARNESS_FINGERPRINT,
        }
    )
    assert update["environmentVariables"] == expected_env
    assert result["endpoint-create.json"]["name"] == "phase6_v9_test"
    rollback = result["rollback.json"]
    assert rollback["preserve_endpoint"] == "phase6_v8_test"
    assert rollback["target_resources_original"] == {
        name: inputs["original"]["Resources"][name] for name in release.TARGETS
    }


def test_model_grant_is_separate_exact_model_region_and_has_no_commerce_or_old_policy_mutation(
    inputs,
):
    result = release.prepare_release(**inputs)
    (statement,) = result["model-grant.json"]["Statement"]
    assert statement["Action"] == "bedrock-mantle:CreateInference"
    assert statement["Resource"] == "*"
    assert statement["Condition"] == {
        "StringEquals": {
            "bedrock-mantle:Model": "google.gemma-4-31b",
            "aws:RequestedRegion": "us-west-2",
        }
    }


@pytest.mark.parametrize("version", ["DEFAULT", "8", "7", "0", 9, "9 ", "0009"])
def test_mutable_or_nonadvancing_runtime_version_rejected(inputs, version):
    inputs["runtime_version"] = version
    with pytest.raises(ValueError):
        release.prepare_release(**inputs)


@pytest.mark.parametrize(
    "field,value",
    [
        ("version", "null"),
        ("key", "private/foreign.zip"),
        ("bucket", "another-account"),
        ("sha256", "0" * 64),
        ("release_fingerprint", "c" * 64),
        ("size_bytes", True),
    ],
)
def test_candidate_archive_identity_cannot_drift(inputs, field, value):
    inputs["lambda_archive"][field] = value
    with pytest.raises(ValueError):
        release.prepare_release(**inputs)


@pytest.mark.parametrize(
    "field,value",
    [
        ("harness_revision", "v2"),
        ("harness_fingerprint", "1" * 64),
        ("config_path", "/var/task/config/bedrock/unknown.json"),
        ("config_fingerprint", "bad"),
    ],
)
def test_unfrozen_model_selection_rejected(inputs, field, value):
    inputs[field] = value
    with pytest.raises(ValueError):
        release.prepare_release(**inputs)


def test_mismatched_runtime_or_parameter_baseline_is_rejected(inputs):
    inputs["original"]["Parameters"]["AgentCoreRuntimeQualifier"]["Default"] = "DEFAULT"
    with pytest.raises(ValueError, match="not pinned"):
        release.prepare_release(**inputs)


def test_materialized_lambda_original_shape_is_supported(inputs):
    inputs["original"] = deepcopy(inputs["processed"])
    result = release.prepare_release(**inputs)
    assert result["candidate-original.json"] == result["expected-processed.json"]


def test_cli_writes_private_reproducible_artifacts_and_refuses_overwrite(inputs, tmp_path):
    source, output = tmp_path / "inputs.json", tmp_path / "release-plan"
    source.write_text(json.dumps(inputs))
    assert release.main(["--input", str(source), "--output", str(output)]) == 0
    assert output.stat().st_mode & 0o777 == 0o700
    expected = release.prepare_release(**inputs)
    for name, value in expected.items():
        assert json.loads((output / name).read_text()) == value
        assert (output / name).stat().st_mode & 0o777 == 0o600
    with pytest.raises(ValueError, match="Output must be new"):
        release.main(["--input", str(source), "--output", str(output)])
