"""Render and verify the release-bound Phase 6 AgentCore deployment configuration.

This module is deliberately local-only.  It reads reviewed JSON templates, verifies a sealed
AgentCore CodeZip artifact when writing deployable files, and never constructs an AWS client.
"""

from __future__ import annotations

import argparse
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ARTIFACT_ROOT = ROOT / ".mr_lister_private/phase6-deployment/agentcore"

AGENTCORE_OUTPUT = Path("infra/agentcore/mrlisterphase6/agentcore/agentcore.json")
AWS_TARGETS_OUTPUT = Path("infra/agentcore/mrlisterphase6/agentcore/aws-targets.json")
DEPLOYMENT_PLAN_OUTPUT = Path("infra/agentcore/mrlisterphase6/deployment-plan.local.json")
RUNTIME_POLICY_OUTPUT = Path("infra/iam/phase6-agentcore-runtime-policy.local.json")
RUNTIME_TRUST_POLICY_OUTPUT = Path("infra/iam/phase6-agentcore-runtime-trust-policy.local.json")
LOG_RETENTION_POLICY_OUTPUT = Path("infra/iam/phase6-agentcore-log-retention-policy.local.json")
RENDER_MANIFEST_OUTPUT = Path("infra/agentcore/mrlisterphase6/render-manifest.local.json")

_TEMPLATE_BY_OUTPUT = {
    AGENTCORE_OUTPUT: Path("infra/agentcore/mrlisterphase6/agentcore/agentcore.json.tmpl"),
    AWS_TARGETS_OUTPUT: Path("infra/agentcore/mrlisterphase6/agentcore/aws-targets.json.tmpl"),
    DEPLOYMENT_PLAN_OUTPUT: Path("infra/agentcore/mrlisterphase6/deployment-plan.json.tmpl"),
    RUNTIME_POLICY_OUTPUT: Path("infra/iam/phase6-agentcore-runtime-policy.json.tmpl"),
    RUNTIME_TRUST_POLICY_OUTPUT: Path("infra/iam/phase6-agentcore-runtime-trust-policy.json.tmpl"),
    LOG_RETENTION_POLICY_OUTPUT: Path("infra/iam/phase6-agentcore-log-retention-policy.json.tmpl"),
}

_ACCOUNT_ID = re.compile(r"^[0-9]{12}$")
_ENVIRONMENT = re.compile(r"^[a-z][a-z0-9-]{1,15}$")
_FINGERPRINT = re.compile(r"^[a-f0-9]{64}$")
_RUNTIME_VERSION = re.compile(r"^[1-9][0-9]{0,4}$")
_PLACEHOLDER = re.compile(
    r"<[A-Z][A-Z0-9_]*>|\$\{[^}\r\n]+\}|__[A-Z][A-Z0-9_]*__|"
    r"\b(?:PLACEHOLDER|REPLACE_ME|CHANGEME)\b",
    re.IGNORECASE,
)
_GENERIC_ERROR = "Phase 6 AgentCore deployment configuration is invalid"

_RUNTIME_NAME = "mr_lister_phase6"
_CODE_LOCATION = "../../../.mr_lister_private/phase6-deployment/agentcore"
_REGION = "us-west-2"
_PYTHON_RUNTIME = "PYTHON_3_12"
_CONTROLLER_MODEL = "us.amazon.nova-2-lite-v1:0"
_GEMMA_MODEL = "google.gemma-3-27b-it"
_PROFILE_FINGERPRINT = "5de1257141cfdacb1731f68bb9113712957483b33d5b0f7115afdba86eb7476c"
_GEMMA_CONFIG_FINGERPRINT = "f036b77edad91d9923f844d0f4db9725b89574d698cc5ce6fcdee23101f9e929"


class Phase6AgentCoreDeploymentError(RuntimeError):
    """A value-free failure for malformed, drifting, or unsealed deployment input."""


@dataclass(frozen=True, slots=True)
class Phase6AgentCoreDeploymentBinding:
    """The five external values that identify one immutable environment release."""

    account_id: str
    region: str
    environment: str
    release_fingerprint: str
    runtime_version: str | int

    def __post_init__(self) -> None:
        try:
            runtime_version = _runtime_version_text(self.runtime_version)
            if (
                not isinstance(self.account_id, str)
                or _ACCOUNT_ID.fullmatch(self.account_id) is None
                or self.account_id == "0" * 12
                or self.region != _REGION
                or not isinstance(self.environment, str)
                or _ENVIRONMENT.fullmatch(self.environment) is None
                or any(
                    token in self.environment.casefold()
                    for token in ("default", "phase3", "placeholder")
                )
                or not isinstance(self.release_fingerprint, str)
                or _FINGERPRINT.fullmatch(self.release_fingerprint) is None
                or self.release_fingerprint == "0" * 64
                or _PLACEHOLDER.search(
                    "\n".join(
                        (
                            self.account_id,
                            self.region,
                            self.environment,
                            self.release_fingerprint,
                            runtime_version,
                        )
                    )
                )
            ):
                raise ValueError
            object.__setattr__(self, "runtime_version", runtime_version)
        except Exception:
            raise Phase6AgentCoreDeploymentError(_GENERIC_ERROR) from None

    @property
    def endpoint_name(self) -> str:
        return f"phase6_v{self.runtime_version}_{self.environment.replace('-', '_')}"

    @property
    def execution_role_arn(self) -> str:
        return (
            f"arn:aws:iam::{self.account_id}:role/"
            f"mr-lister-phase6-agentcore-runtime-{self.environment}"
        )


def render_phase6_agentcore_deployment(
    binding: Phase6AgentCoreDeploymentBinding,
    *,
    repository_root: Path = ROOT,
) -> dict[Path, bytes]:
    """Return canonical, byte-deterministic rendered documents and their manifest."""

    try:
        if not isinstance(binding, Phase6AgentCoreDeploymentBinding):
            raise ValueError
        root = repository_root.resolve(strict=True)
        documents: dict[Path, bytes] = {}
        values: dict[str, object] = {
            "<AWS_ACCOUNT_ID>": binding.account_id,
            "<AWS_REGION>": binding.region,
            "<ENVIRONMENT>": binding.environment,
            "<ENVIRONMENT_UNDERSCORE>": binding.environment.replace("-", "_"),
            "<RELEASE_FINGERPRINT>": binding.release_fingerprint,
            "<RUNTIME_VERSION>": binding.runtime_version,
            "<RUNTIME_VERSION_NUMBER>": int(binding.runtime_version),
        }
        for output_path, relative_template in _TEMPLATE_BY_OUTPUT.items():
            template_path = root / relative_template
            if template_path.is_symlink() or not template_path.is_file():
                raise ValueError
            raw_template = template_path.read_text(encoding="utf-8")
            parsed = json.loads(raw_template)
            rendered = _replace_tokens(parsed, values)
            documents[output_path] = _canonical_json(rendered)

        _validate_rendered_documents(binding, documents)
        manifest = {
            "binding": {
                "accountId": binding.account_id,
                "endpointName": binding.endpoint_name,
                "environment": binding.environment,
                "region": binding.region,
                "releaseFingerprint": binding.release_fingerprint,
                "runtimeName": _RUNTIME_NAME,
                "runtimeVersion": binding.runtime_version,
            },
            "bindingFingerprint": _binding_fingerprint(binding),
            "documents": {
                path.as_posix(): sha256(content).hexdigest()
                for path, content in sorted(documents.items(), key=lambda item: item[0].as_posix())
            },
            "format": "mr-lister-phase6-agentcore-render-v1",
            "logRetentionInDays": 14,
        }
        documents[RENDER_MANIFEST_OUTPUT] = _canonical_json(manifest)
        _reject_unresolved_or_forbidden_rendered_content(documents)
        return dict(sorted(documents.items(), key=lambda item: item[0].as_posix()))
    except Phase6AgentCoreDeploymentError:
        raise
    except Exception:
        raise Phase6AgentCoreDeploymentError(_GENERIC_ERROR) from None


def verify_phase6_agentcore_release(
    binding: Phase6AgentCoreDeploymentBinding,
    *,
    artifact_root: Path = DEFAULT_ARTIFACT_ROOT,
) -> None:
    """Verify every sealed CodeZip byte against this configuration's release identity."""

    try:
        if not isinstance(binding, Phase6AgentCoreDeploymentBinding):
            raise ValueError
        if artifact_root.is_symlink():
            raise ValueError
        resolved = artifact_root.resolve(strict=True)
        if not resolved.is_dir() or tuple(resolved.parts[-2:]) != (
            "phase6-deployment",
            "agentcore",
        ):
            raise ValueError
        from mr_lister.release.phase6 import verify_phase6_packaged_release

        verified = verify_phase6_packaged_release(
            {"MR_LISTER_RELEASE_FINGERPRINT": binding.release_fingerprint},
            component="agentcore",
            bundle_root=resolved,
        )
        if (
            verified.component != "agentcore"
            or verified.release_fingerprint != binding.release_fingerprint
        ):
            raise ValueError
    except Phase6AgentCoreDeploymentError:
        raise
    except Exception:
        raise Phase6AgentCoreDeploymentError(_GENERIC_ERROR) from None


def write_phase6_agentcore_deployment(
    binding: Phase6AgentCoreDeploymentBinding,
    *,
    repository_root: Path = ROOT,
    artifact_root: Path = DEFAULT_ARTIFACT_ROOT,
) -> tuple[Path, ...]:
    """Write reviewed local outputs only after the sealed artifact passes verification."""

    try:
        root = repository_root.resolve(strict=True)
        verify_phase6_agentcore_release(binding, artifact_root=artifact_root)
        documents = render_phase6_agentcore_deployment(binding, repository_root=root)
        destinations = tuple(root / relative for relative in documents)
        if any(path.exists() or path.is_symlink() for path in destinations):
            raise ValueError
        for path in destinations:
            path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            if path.parent.is_symlink():
                raise ValueError
        for relative, content in documents.items():
            destination = root / relative
            with destination.open("xb") as stream:
                stream.write(content)
            destination.chmod(0o600)
        return destinations
    except Phase6AgentCoreDeploymentError:
        raise
    except Exception:
        raise Phase6AgentCoreDeploymentError(_GENERIC_ERROR) from None


def verify_rendered_phase6_agentcore_deployment(
    binding: Phase6AgentCoreDeploymentBinding,
    *,
    repository_root: Path = ROOT,
    artifact_root: Path = DEFAULT_ARTIFACT_ROOT,
) -> None:
    """Reject any byte drift in existing rendered outputs or in the sealed artifact."""

    try:
        root = repository_root.resolve(strict=True)
        verify_phase6_agentcore_release(binding, artifact_root=artifact_root)
        expected = render_phase6_agentcore_deployment(binding, repository_root=root)
        for relative, expected_bytes in expected.items():
            path = root / relative
            if path.is_symlink() or not path.is_file() or path.read_bytes() != expected_bytes:
                raise ValueError
    except Phase6AgentCoreDeploymentError:
        raise
    except Exception:
        raise Phase6AgentCoreDeploymentError(_GENERIC_ERROR) from None


def _replace_tokens(value: object, replacements: Mapping[str, object]) -> object:
    if isinstance(value, str):
        if value in replacements and not isinstance(replacements[value], str):
            return replacements[value]
        replaced = value
        for token, replacement in replacements.items():
            if not isinstance(replacement, str):
                continue
            replaced = replaced.replace(token, replacement)
        return replaced
    if isinstance(value, list):
        return [_replace_tokens(item, replacements) for item in value]
    if isinstance(value, dict):
        rendered: dict[str, object] = {}
        for key, item in value.items():
            replaced_key = _replace_tokens(key, replacements)
            if not isinstance(replaced_key, str) or replaced_key in rendered:
                raise ValueError
            rendered[replaced_key] = _replace_tokens(item, replacements)
        return rendered
    if value is None or isinstance(value, (bool, int, float)):
        return value
    raise ValueError


def _canonical_json(value: object) -> bytes:
    return (
        json.dumps(
            value,
            allow_nan=False,
            indent=2,
            separators=(",", ": "),
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")


def _binding_fingerprint(binding: Phase6AgentCoreDeploymentBinding) -> str:
    material = {
        "account_id": binding.account_id,
        "endpoint_name": binding.endpoint_name,
        "environment": binding.environment,
        "format": "mr-lister-phase6-agentcore-binding-v1",
        "region": binding.region,
        "release_fingerprint": binding.release_fingerprint,
        "runtime_name": _RUNTIME_NAME,
        "runtime_version": binding.runtime_version,
    }
    return sha256(_canonical_json(material)).hexdigest()


def _validate_rendered_documents(
    binding: Phase6AgentCoreDeploymentBinding,
    documents: Mapping[Path, bytes],
) -> None:
    if set(documents) != set(_TEMPLATE_BY_OUTPUT):
        raise ValueError
    parsed = {path: json.loads(content) for path, content in documents.items()}
    _validate_agentcore(binding, parsed[AGENTCORE_OUTPUT])
    _validate_aws_target(binding, parsed[AWS_TARGETS_OUTPUT])
    _validate_deployment_plan(binding, parsed[DEPLOYMENT_PLAN_OUTPUT])
    _validate_runtime_policy(binding, parsed[RUNTIME_POLICY_OUTPUT])
    _validate_runtime_trust(binding, parsed[RUNTIME_TRUST_POLICY_OUTPUT])
    _validate_log_retention_policy(binding, parsed[LOG_RETENTION_POLICY_OUTPUT])
    _reject_unresolved_or_forbidden_rendered_content(documents)


def _validate_agentcore(
    binding: Phase6AgentCoreDeploymentBinding,
    document: object,
) -> None:
    if not isinstance(document, dict):
        raise ValueError
    required_empty = {
        "memories",
        "knowledgeBases",
        "credentials",
        "evaluators",
        "onlineEvalConfigs",
        "agentCoreGateways",
        "policyEngines",
        "configBundles",
        "abTests",
        "harnesses",
        "datasets",
        "payments",
    }
    expected_top_level = {
        "$schema",
        "name",
        "version",
        "managedBy",
        "tags",
        "runtimes",
        *required_empty,
    }
    if (
        set(document) != expected_top_level
        or document.get("$schema") != "https://schema.agentcore.aws.dev/v1/agentcore.json"
        or document.get("name") != "mrlisterphase6"
        or document.get("version") != 1
        or document.get("managedBy") != "CDK"
        or any(document.get(name) != [] for name in required_empty)
        or set(document.get("tags", {}))
        != {
            "mr-lister:component",
            "mr-lister:environment",
            "mr-lister:release-fingerprint",
        }
        or document.get("tags")
        != {
            "mr-lister:component": "preparation-runtime",
            "mr-lister:environment": binding.environment,
            "mr-lister:release-fingerprint": binding.release_fingerprint,
        }
    ):
        raise ValueError
    runtimes = document.get("runtimes")
    if not isinstance(runtimes, list) or len(runtimes) != 1:
        raise ValueError
    runtime = runtimes[0]
    if not isinstance(runtime, dict):
        raise ValueError
    expected_runtime_keys = {
        "name",
        "description",
        "build",
        "entrypoint",
        "codeLocation",
        "runtimeVersion",
        "envVars",
        "networkMode",
        "protocol",
        "authorizerType",
        "executionRoleArn",
        "instrumentation",
        "lifecycleConfiguration",
        "endpoints",
    }
    if (
        set(runtime) != expected_runtime_keys
        or runtime.get("name") != _RUNTIME_NAME
        or runtime.get("description") != "Release-bound Phase 6 Strands preparation runtime"
        or runtime.get("build") != "CodeZip"
        or runtime.get("entrypoint") != "main.py"
        or runtime.get("codeLocation") != _CODE_LOCATION
        or runtime.get("runtimeVersion") != _PYTHON_RUNTIME
        or runtime.get("networkMode") != "PUBLIC"
        or runtime.get("protocol") != "HTTP"
        or runtime.get("authorizerType") != "AWS_IAM"
        or runtime.get("executionRoleArn") != binding.execution_role_arn
        or runtime.get("instrumentation") != {"enableOtel": False}
        or runtime.get("lifecycleConfiguration")
        != {"idleRuntimeSessionTimeout": 900, "maxLifetime": 3600}
        or runtime.get("endpoints")
        != {
            binding.endpoint_name: {
                "description": (
                    f"Immutable Phase 6 version {binding.runtime_version} endpoint "
                    f"for {binding.environment}"
                ),
                "version": int(binding.runtime_version),
            }
        }
        or any(
            key in runtime
            for key in (
                "additionalPolicies",
                "authorizerConfiguration",
                "connections",
                "filesystemConfigurations",
                "networkConfig",
            )
        )
    ):
        raise ValueError
    env_vars = runtime.get("envVars")
    if not isinstance(env_vars, list) or any(
        not isinstance(item, dict) or set(item) != {"name", "value"} for item in env_vars
    ):
        raise ValueError
    environment = {item["name"]: item["value"] for item in env_vars}
    if len(environment) != len(env_vars) or environment != _expected_environment(binding):
        raise ValueError


def _expected_environment(binding: Phase6AgentCoreDeploymentBinding) -> dict[str, str]:
    return {
        "AWS_REGION": binding.region,
        "MR_LISTER_ARTIFACT_BUCKET": (
            f"mr-lister-phase6-artifacts-{binding.environment}-"
            f"{binding.account_id}-{binding.region}"
        ),
        "MR_LISTER_AWS_ACCOUNT_ID": binding.account_id,
        "MR_LISTER_ENVIRONMENT": binding.environment,
        "MR_LISTER_GEMMA_CONFIG_FINGERPRINT": _GEMMA_CONFIG_FINGERPRINT,
        "MR_LISTER_GEMMA_CONFIG_PATH": ("/var/task/config/bedrock/google_gemma_3_27b_it.json"),
        "MR_LISTER_PRODUCT_PROFILE_FINGERPRINT": _PROFILE_FINGERPRINT,
        "MR_LISTER_PRODUCT_PROFILE_ID": "gildan_64000_swiftpod",
        "MR_LISTER_PRODUCT_PROFILE_PATH": (
            "/var/task/config/product_profiles/gildan_64000_swiftpod.json"
        ),
        "MR_LISTER_PRODUCT_PROFILE_VERSION": "2",
        "MR_LISTER_RELEASE_FINGERPRINT": binding.release_fingerprint,
        "MR_LISTER_STATE_TABLE": f"mr-lister-phase6-{binding.environment}",
        "MR_LISTER_STRANDS_CONTROLLER_MODEL_ID": _CONTROLLER_MODEL,
    }


def _validate_aws_target(
    binding: Phase6AgentCoreDeploymentBinding,
    document: object,
) -> None:
    if document != [
        {
            "account": binding.account_id,
            "description": (f"Release-bound Mr Lister Phase 6 target for {binding.environment}"),
            "name": binding.environment,
            "region": binding.region,
        }
    ]:
        raise ValueError


def _validate_deployment_plan(
    binding: Phase6AgentCoreDeploymentBinding,
    document: object,
) -> None:
    if not isinstance(document, dict):
        raise ValueError
    runtime = document.get("runtime")
    retention = document.get("logRetention")
    if (
        set(document)
        != {
            "format",
            "accountId",
            "region",
            "environment",
            "releaseFingerprint",
            "runtime",
            "logRetention",
            "requiredPostDeployChecks",
        }
        or document.get("format") != "mr-lister-phase6-agentcore-deployment-v1"
        or document.get("accountId") != binding.account_id
        or document.get("region") != binding.region
        or document.get("environment") != binding.environment
        or document.get("releaseFingerprint") != binding.release_fingerprint
        or not isinstance(runtime, dict)
        or runtime
        != {
            "name": _RUNTIME_NAME,
            "pythonRuntime": _PYTHON_RUNTIME,
            "codeLocation": _CODE_LOCATION,
            "executionRoleArn": binding.execution_role_arn,
            "immutableVersion": binding.runtime_version,
            "endpointName": binding.endpoint_name,
        }
        or not isinstance(retention, dict)
        or retention
        != {
            "retentionInDays": 14,
            "logGroupNamePattern": (
                "/aws/bedrock-agentcore/runtimes/*mr_lister_phase6-??????????-"
                f"{binding.endpoint_name}"
            ),
            "applyAction": "logs:PutRetentionPolicy",
            "verifyAction": "logs:DescribeLogGroups",
            "requiredBeforeTraffic": True,
        }
        or document.get("requiredPostDeployChecks")
        != [
            "runtime-ready",
            "custom-endpoint-ready",
            "custom-endpoint-targets-exact-version",
            "standard-log-group-retention-is-14-days",
            "sealed-release-fingerprint-matches",
        ]
    ):
        raise ValueError


def _validate_runtime_policy(
    binding: Phase6AgentCoreDeploymentBinding,
    document: object,
) -> None:
    if (
        not isinstance(document, dict)
        or set(document) != {"Version", "Statement"}
        or document.get("Version") != "2012-10-17"
    ):
        raise ValueError
    statements = document.get("Statement")
    if not isinstance(statements, list) or len(statements) != 9:
        raise ValueError
    by_sid = _statements_by_sid(statements)
    if set(by_sid) != {
        "CreateAndInspectAgentCoreRuntimeLogs",
        "ConfigureOnlyPhase6AgentCoreRuntimeLogs",
        "DiscoverOnlyAccountLogGroups",
        "WriteOnlyPhase6AgentCoreRuntimeLogs",
        "ReadAndCommitOnlyPhase6PreparationState",
        "ReadOnlyPinnedPhase6SourceVersions",
        "InvokeNova2LiteUSProfile",
        "InvokeNova2LiteOnlyThroughUSProfile",
        "InvokeOnlyGemma327BIntelligence",
    }:
        raise ValueError
    all_actions = {
        action for statement in statements for action in _actions(statement.get("Action"))
    }
    if all_actions != {
        "logs:DescribeLogStreams",
        "logs:CreateLogGroup",
        "logs:PutResourcePolicy",
        "logs:DescribeLogGroups",
        "logs:CreateLogStream",
        "logs:PutLogEvents",
        "dynamodb:GetItem",
        "dynamodb:PutItem",
        "dynamodb:TransactWriteItems",
        "s3:GetObjectVersion",
        "bedrock:InvokeModel",
    }:
        raise ValueError
    expected_log_statements = {
        "CreateAndInspectAgentCoreRuntimeLogs": {
            "actions": {"logs:DescribeLogStreams", "logs:CreateLogGroup"},
            "resource": (
                f"arn:aws:logs:{binding.region}:{binding.account_id}:log-group:"
                "/aws/bedrock-agentcore/runtimes/*"
            ),
        },
        "ConfigureOnlyPhase6AgentCoreRuntimeLogs": {
            "actions": {"logs:PutResourcePolicy"},
            "resource": (
                f"arn:aws:logs:{binding.region}:{binding.account_id}:log-group:"
                "/aws/bedrock-agentcore/runtimes/*mr_lister_phase6-*"
            ),
        },
        "DiscoverOnlyAccountLogGroups": {
            "actions": {"logs:DescribeLogGroups"},
            "resource": (f"arn:aws:logs:{binding.region}:{binding.account_id}:log-group:*"),
        },
        "WriteOnlyPhase6AgentCoreRuntimeLogs": {
            "actions": {"logs:CreateLogStream", "logs:PutLogEvents"},
            "resource": (
                f"arn:aws:logs:{binding.region}:{binding.account_id}:log-group:"
                "/aws/bedrock-agentcore/runtimes/*mr_lister_phase6-*:log-stream:*"
            ),
        },
    }
    for sid, expected in expected_log_statements.items():
        statement = by_sid[sid]
        if (
            set(_actions(statement.get("Action"))) != expected["actions"]
            or statement.get("Resource") != expected["resource"]
            or set(statement) != {"Sid", "Effect", "Action", "Resource"}
        ):
            raise ValueError
    dynamodb = by_sid["ReadAndCommitOnlyPhase6PreparationState"]
    if (
        set(_actions(dynamodb.get("Action")))
        != {
            "dynamodb:GetItem",
            "dynamodb:PutItem",
            "dynamodb:TransactWriteItems",
        }
        or dynamodb.get("Resource")
        != (
            f"arn:aws:dynamodb:{binding.region}:{binding.account_id}:"
            f"table/mr-lister-phase6-{binding.environment}"
        )
        or dynamodb.get("Condition")
        != {
            "ForAllValues:StringLike": {"dynamodb:LeadingKeys": ["JOB#*", "OWNER#*"]},
            "Null": {"dynamodb:LeadingKeys": "false"},
        }
    ):
        raise ValueError
    s3 = by_sid["ReadOnlyPinnedPhase6SourceVersions"]
    if s3.get("Action") != "s3:GetObjectVersion" or s3.get("Resource") != (
        "arn:aws:s3:::mr-lister-phase6-artifacts-"
        f"{binding.environment}-{binding.account_id}-{binding.region}/"
        "private/owners/*/jobs/*/source/source.png"
    ):
        raise ValueError
    profile_arn = (
        f"arn:aws:bedrock:{binding.region}:{binding.account_id}:"
        "inference-profile/us.amazon.nova-2-lite-v1:0"
    )
    nova_profile = by_sid["InvokeNova2LiteUSProfile"]
    nova_destinations = by_sid["InvokeNova2LiteOnlyThroughUSProfile"]
    gemma = by_sid["InvokeOnlyGemma327BIntelligence"]
    if (
        nova_profile.get("Action") != "bedrock:InvokeModel"
        or nova_profile.get("Resource") != profile_arn
        or nova_destinations.get("Action") != "bedrock:InvokeModel"
        or nova_destinations.get("Resource")
        != [
            "arn:aws:bedrock:us-east-1::foundation-model/amazon.nova-2-lite-v1:0",
            "arn:aws:bedrock:us-east-2::foundation-model/amazon.nova-2-lite-v1:0",
            "arn:aws:bedrock:us-west-2::foundation-model/amazon.nova-2-lite-v1:0",
        ]
        or nova_destinations.get("Condition")
        != {"StringEquals": {"bedrock:InferenceProfileArn": profile_arn}}
        or gemma.get("Action") != "bedrock:InvokeModel"
        or gemma.get("Resource")
        != f"arn:aws:bedrock:{binding.region}::foundation-model/{_GEMMA_MODEL}"
    ):
        raise ValueError
    if any(statement.get("Effect") != "Allow" for statement in statements):
        raise ValueError


def _validate_runtime_trust(
    binding: Phase6AgentCoreDeploymentBinding,
    document: object,
) -> None:
    expected = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "TrustOnlyPhase6AgentCoreRuntime",
                "Effect": "Allow",
                "Principal": {"Service": "bedrock-agentcore.amazonaws.com"},
                "Action": "sts:AssumeRole",
                "Condition": {
                    "StringEquals": {"aws:SourceAccount": binding.account_id},
                    "ArnLike": {
                        "aws:SourceArn": (
                            f"arn:aws:bedrock-agentcore:{binding.region}:"
                            f"{binding.account_id}:runtime/*mr_lister_phase6-*"
                        )
                    },
                },
            }
        ],
    }
    if document != expected:
        raise ValueError


def _validate_log_retention_policy(
    binding: Phase6AgentCoreDeploymentBinding,
    document: object,
) -> None:
    expected = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "DiscoverAgentCoreRuntimeLogGroupsForRetention",
                "Effect": "Allow",
                "Action": "logs:DescribeLogGroups",
                "Resource": "*",
            },
            {
                "Sid": "SetOnlyVersionBoundPhase6RuntimeLogRetention",
                "Effect": "Allow",
                "Action": "logs:PutRetentionPolicy",
                "Resource": (
                    f"arn:aws:logs:{binding.region}:{binding.account_id}:log-group:"
                    "/aws/bedrock-agentcore/runtimes/*mr_lister_phase6-??????????-"
                    f"{binding.endpoint_name}:*"
                ),
            },
        ],
    }
    if document != expected:
        raise ValueError


def _statements_by_sid(statements: list[object]) -> dict[str, dict[str, object]]:
    indexed: dict[str, dict[str, object]] = {}
    for statement in statements:
        if not isinstance(statement, dict):
            raise ValueError
        sid = statement.get("Sid")
        if not isinstance(sid, str) or sid in indexed:
            raise ValueError
        indexed[sid] = statement
    return indexed


def _actions(value: object) -> tuple[str, ...]:
    if isinstance(value, str):
        return (value,)
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return tuple(value)
    raise ValueError


def _reject_unresolved_or_forbidden_rendered_content(
    documents: Mapping[Path, bytes],
) -> None:
    raw = b"\n".join(documents.values()).decode("utf-8")
    lowered = raw.casefold()
    if (
        _PLACEHOLDER.search(raw) is not None
        or "default" in lowered
        or "phase3" in lowered
        or any(
            forbidden in lowered
            for forbidden in (
                "secretsmanager:",
                "states:",
                "execute-api:",
                "apigateway:",
                "printify",
                "etsy",
                "publication",
                "order",
            )
        )
    ):
        raise ValueError


def _runtime_version_text(value: object) -> str:
    if isinstance(value, bool):
        raise ValueError
    if isinstance(value, int):
        text = str(value)
    elif isinstance(value, str):
        text = value
    else:
        raise ValueError
    if _RUNTIME_VERSION.fullmatch(text) is None:
        raise ValueError
    return text


def _binding_from_arguments(arguments: argparse.Namespace) -> Phase6AgentCoreDeploymentBinding:
    return Phase6AgentCoreDeploymentBinding(
        account_id=arguments.account_id,
        region=arguments.region,
        environment=arguments.environment,
        release_fingerprint=arguments.release_fingerprint,
        runtime_version=arguments.runtime_version,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--account-id", required=True)
    parser.add_argument("--region", required=True)
    parser.add_argument("--environment", required=True)
    parser.add_argument("--release-fingerprint", required=True)
    parser.add_argument("--runtime-version", required=True)
    parser.add_argument("--artifact-root", type=Path, default=DEFAULT_ARTIFACT_ROOT)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--write", action="store_true")
    action.add_argument("--verify", action="store_true")
    arguments = parser.parse_args()
    try:
        binding = _binding_from_arguments(arguments)
        if arguments.write:
            paths = write_phase6_agentcore_deployment(
                binding,
                artifact_root=arguments.artifact_root,
            )
            for path in paths:
                print(path)
        else:
            verify_rendered_phase6_agentcore_deployment(
                binding,
                artifact_root=arguments.artifact_root,
            )
            print(RENDER_MANIFEST_OUTPUT)
    except Phase6AgentCoreDeploymentError as error:
        parser.exit(2, f"{error}\n")


__all__ = [
    "AGENTCORE_OUTPUT",
    "AWS_TARGETS_OUTPUT",
    "DEFAULT_ARTIFACT_ROOT",
    "DEPLOYMENT_PLAN_OUTPUT",
    "LOG_RETENTION_POLICY_OUTPUT",
    "RENDER_MANIFEST_OUTPUT",
    "RUNTIME_POLICY_OUTPUT",
    "RUNTIME_TRUST_POLICY_OUTPUT",
    "Phase6AgentCoreDeploymentBinding",
    "Phase6AgentCoreDeploymentError",
    "render_phase6_agentcore_deployment",
    "verify_phase6_agentcore_release",
    "verify_rendered_phase6_agentcore_deployment",
    "write_phase6_agentcore_deployment",
]


if __name__ == "__main__":
    main()
