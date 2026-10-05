"""Render a prep/query-only Gemma 4 release from captured live documents, entirely offline.

The caller supplies verified sealed archive identities and exact S3 VersionIds. This tool
does not establish that those objects exist, execute AWS operations, or grant approval.
Read back the real change set and every live function before execution; direct deployment
drift may exist outside CloudFormation and must not be reconciled by this release.
"""

from __future__ import annotations

import argparse
import json
import os
import re
from copy import deepcopy
from hashlib import sha256
from pathlib import Path
from typing import Any

from mr_lister.agent.runtime_binding import agentcore_runtime_binding_fingerprint

TARGETS = ("PreparationDispatchFunction", "ReviewQueryApiFunction")
RELEASE_ENV = "MR_LISTER_RELEASE_FINGERPRINT"
HARNESS_REVISION = "v3"
HARNESS_FINGERPRINT = "0fda08954b93aa25c8b3b8ff158cbba124bc8d37cb30e311a229c328be64e7b9"
CONFIG_PATH = "/var/task/config/bedrock/google_gemma_4_31b.json"
RUNTIME_FIELDS = (
    "agentRuntimeArtifact",
    "roleArn",
    "networkConfiguration",
    "description",
    "authorizerConfiguration",
    "requestHeaderConfiguration",
    "protocolConfiguration",
    "environmentVariables",
    "lifecycleConfiguration",
    "metadataConfiguration",
)
PARAMETER_SUFFIXES = {
    "AgentCoreRuntimeEndpointArn": "ENDPOINT_ARN",
    "AgentCoreRuntimeQualifier": "QUALIFIER",
    "AgentCoreRuntimeVersion": "VERSION",
    "AgentCoreRuntimeBindingFingerprint": "BINDING_FINGERPRINT",
}


def _require(value: object, message: str) -> None:
    if not value:
        raise ValueError(message)


def _fingerprint(value: Any) -> bool:
    return isinstance(value, str) and bool(re.fullmatch(r"(?!0{64}$)[a-f0-9]{64}", value))


def _canonical(value: Any) -> bytes:
    return (json.dumps(value, sort_keys=True, indent=2, allow_nan=False) + "\n").encode()


def _digest(value: Any) -> str:
    return sha256(_canonical(value)).hexdigest()


def _archive(value: dict[str, Any], component: str, bucket: str) -> dict[str, Any]:
    _require(
        set(value)
        == {
            "bucket",
            "key",
            "version",
            "sha256",
            "size_bytes",
            "release_fingerprint",
        },
        "Archive identity fields differ",
    )
    _require(value["bucket"] == bucket, "Archive bucket differs from the captured runtime")
    _require(
        _fingerprint(value["sha256"]) and _fingerprint(value["release_fingerprint"]),
        "Archive fingerprints are invalid",
    )
    _require(
        type(value["size_bytes"]) is int and value["size_bytes"] > 0, "Archive byte size is invalid"
    )
    version = value["version"]
    _require(
        isinstance(version, str)
        and version != "null"
        and re.fullmatch(r"[\x21-\x7e]{1,1024}", version),
        "Exact S3 version is required",
    )
    expected = (
        f"private/deployments/{component}/releases/{value['release_fingerprint']}/"
        f"phase6-{component}-{value['sha256']}.zip"
    )
    _require(value["key"] == expected, "Archive key is not bound to its component and digest")
    return deepcopy(value)


def _binding(runtime_arn: str, environment: str, version: str, release: str) -> dict[str, str]:
    qualifier = f"phase6_v{version}_{environment.replace('-', '_')}"
    endpoint = f"{runtime_arn}/runtime-endpoint/{qualifier}"
    return {
        "AgentCoreRuntimeEndpointArn": endpoint,
        "AgentCoreRuntimeQualifier": qualifier,
        "AgentCoreRuntimeVersion": version,
        "AgentCoreRuntimeBindingFingerprint": agentcore_runtime_binding_fingerprint(
            runtime_arn=runtime_arn,
            endpoint_arn=endpoint,
            qualifier=qualifier,
            runtime_version=version,
            release_fingerprint=release,
        ),
    }


def _check_template(
    template: dict[str, Any],
    *,
    processed: bool,
    baseline_binding: dict[str, str],
    runtime_release: str,
) -> None:
    for name in TARGETS:
        resource = template["Resources"][name]
        allowed = (
            {"AWS::Lambda::Function"}
            if processed
            else {
                "AWS::Lambda::Function",
                "AWS::Serverless::Function",
            }
        )
        _require(resource["Type"] in allowed, "Unsupported captured function type")
        props = resource["Properties"]
        _require(
            _fingerprint(props["Environment"]["Variables"][RELEASE_ENV]),
            "Captured function release is invalid",
        )
        code_key = "CodeUri" if resource["Type"] == "AWS::Serverless::Function" else "Code"
        code = props[code_key]
        keys = (
            {"Bucket", "Key", "Version"}
            if code_key == "CodeUri"
            else {
                "S3Bucket",
                "S3Key",
                "S3ObjectVersion",
            }
        )
        _require(
            isinstance(code, dict) and set(code) == keys,
            "Captured function code must be an exact S3 version",
        )
        _require(
            all(isinstance(v, str) and v and v != "null" for v in code.values()),
            "Captured function code is invalid",
        )
    prep = template["Resources"][TARGETS[0]]["Properties"]["Environment"]["Variables"]
    _require(prep[RELEASE_ENV] == runtime_release, "Preparation/runtime releases differ")
    for name, suffix in PARAMETER_SUFFIXES.items():
        parameter = template["Parameters"][name]
        _require(
            parameter.get("Default") == baseline_binding[name]
            and parameter.get("AllowedValues") == [baseline_binding[name]],
            "Captured endpoint parameter is not pinned to the runtime",
        )
        _require(
            prep.get("MR_LISTER_AGENTCORE_RUNTIME_" + suffix) == {"Ref": name},
            "Preparation endpoint settings must use existing parameters",
        )


def prepare_release(
    *,
    original: dict[str, Any],
    processed: dict[str, Any],
    runtime: dict[str, Any],
    lambda_archive: dict[str, Any],
    agentcore_archive: dict[str, Any],
    runtime_version: str,
    config_path: str,
    config_fingerprint: str,
    harness_revision: str,
    harness_fingerprint: str,
) -> dict[str, Any]:
    """Return deterministic documents without altering inputs or reading AWS/filesystem state."""
    _require(
        harness_revision == HARNESS_REVISION and harness_fingerprint == HARNESS_FINGERPRINT,
        "Only the frozen v3 harness is supported",
    )
    _require(
        config_path == CONFIG_PATH and _fingerprint(config_fingerprint),
        "Gemma 4 config path or fingerprint is invalid",
    )
    _require(
        isinstance(runtime_version, str) and re.fullmatch(r"[1-9][0-9]{0,4}", runtime_version),
        "New runtime version must be explicit and immutable",
    )
    env = runtime["environmentVariables"]
    account, region, environment = (
        env["MR_LISTER_AWS_ACCOUNT_ID"],
        env["AWS_REGION"],
        env["MR_LISTER_ENVIRONMENT"],
    )
    _require(
        isinstance(account, str)
        and re.fullmatch(r"[0-9]{12}", account)
        and account != "0" * 12
        and region == "us-west-2"
        and isinstance(environment, str)
        and re.fullmatch(r"[a-z][a-z0-9-]{1,15}", environment),
        "Runtime identity is invalid",
    )
    runtime_id = runtime["agentRuntimeId"]
    _require(
        isinstance(runtime_id, str)
        and re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,47}-[A-Za-z0-9]{10}", runtime_id)
        and "phase6" in runtime_id.lower(),
        "Runtime ID is invalid",
    )
    runtime_arn = f"arn:aws:bedrock-agentcore:{region}:{account}:runtime/{runtime_id}"
    old_version = runtime["agentRuntimeVersion"]
    _require(
        runtime["agentRuntimeArn"] == runtime_arn
        and runtime["status"] == "READY"
        and isinstance(old_version, str)
        and re.fullmatch(r"[1-9][0-9]{0,4}", old_version)
        and int(runtime_version) > int(old_version),
        "Runtime baseline/version is unsupported",
    )
    old_release = env[RELEASE_ENV]
    _require(
        _fingerprint(old_release)
        and env["MR_LISTER_GEMMA_CONFIG_PATH"]
        == "/var/task/config/bedrock/google_gemma_3_27b_it.json"
        and _fingerprint(env["MR_LISTER_GEMMA_CONFIG_FINGERPRINT"]),
        "Migration requires the captured Gemma 3 rollback runtime",
    )
    artifact = runtime["agentRuntimeArtifact"]["codeConfiguration"]
    _require(
        artifact["runtime"] == "PYTHON_3_12" and artifact["entryPoint"] == ["main.py"],
        "Unsupported runtime artifact",
    )
    bucket = f"mr-lister-phase6-artifacts-{environment}-{account}-{region}"
    _require(
        env["MR_LISTER_ARTIFACT_BUCKET"] == bucket and artifact["code"]["s3"]["bucket"] == bucket,
        "Runtime artifact bucket differs",
    )
    archives = {
        "lambda": _archive(lambda_archive, "lambda", bucket),
        "agentcore": _archive(agentcore_archive, "agentcore", bucket),
    }
    release = archives["lambda"]["release_fingerprint"]
    _require(
        release == archives["agentcore"]["release_fingerprint"] and release != old_release,
        "Candidate archives must share one new sealed release",
    )
    old_binding = _binding(runtime_arn, environment, old_version, old_release)
    binding = _binding(runtime_arn, environment, runtime_version, release)
    for template, is_processed in ((original, False), (processed, True)):
        _check_template(
            template,
            processed=is_processed,
            baseline_binding=old_binding,
            runtime_release=old_release,
        )
    candidate, expected = deepcopy(original), deepcopy(processed)
    for template in (candidate, expected):
        for name in TARGETS:
            resource = template["Resources"][name]
            props = resource["Properties"]
            info = archives["lambda"]
            if resource["Type"] == "AWS::Serverless::Function":
                props["CodeUri"] = {
                    "Bucket": bucket,
                    "Key": info["key"],
                    "Version": info["version"],
                }
            else:
                props["Code"] = {
                    "S3Bucket": bucket,
                    "S3Key": info["key"],
                    "S3ObjectVersion": info["version"],
                }
            props["Environment"]["Variables"][RELEASE_ENV] = release
        for name, value in binding.items():
            template["Parameters"][name].update(Default=value, AllowedValues=[value])
    update = {key: deepcopy(runtime[key]) for key in RUNTIME_FIELDS if key in runtime}
    update.update(agentRuntimeId=runtime_id, clientToken="gemma4-runtime-" + release[:48])
    info = archives["agentcore"]
    update["agentRuntimeArtifact"]["codeConfiguration"]["code"]["s3"] = {
        "bucket": bucket,
        "prefix": info["key"],
        "versionId": info["version"],
    }
    update["environmentVariables"].update(
        {
            RELEASE_ENV: release,
            "MR_LISTER_GEMMA_CONFIG_PATH": config_path,
            "MR_LISTER_GEMMA_CONFIG_FINGERPRINT": config_fingerprint,
            "MR_LISTER_HARNESS_REVISION": harness_revision,
            "MR_LISTER_HARNESS_PROMPT_FINGERPRINT": harness_fingerprint,
        }
    )
    endpoint = {
        "agentRuntimeId": runtime_id,
        "agentRuntimeVersion": runtime_version,
        "name": binding["AgentCoreRuntimeQualifier"],
        "description": f"Gemma 4 frozen v3 preparation; immutable runtime {runtime_version}",
        "clientToken": "gemma4-endpoint-" + release[:48],
    }
    model_policy = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "InvokeOnlyGemma431BIntelligence",
                "Effect": "Allow",
                "Action": "bedrock-mantle:CreateInference",
                "Resource": "*",
                "Condition": {
                    "StringEquals": {
                        "bedrock-mantle:Model": "google.gemma-4-31b",
                        "aws:RequestedRegion": region,
                    }
                },
            }
        ],
    }
    documents = {
        "candidate-original.json": candidate,
        "expected-processed.json": expected,
        "runtime-update.json": update,
        "endpoint-create.json": endpoint,
        "binding.json": binding,
        "model-grant.json": model_policy,
        "rollback.json": {
            "runtime_id": runtime_id,
            "runtime_version": old_version,
            "preserve_endpoint": old_binding["AgentCoreRuntimeQualifier"],
            "binding": old_binding,
            "target_resources_original": {
                name: deepcopy(original["Resources"][name]) for name in TARGETS
            },
            "target_resources_processed": {
                name: deepcopy(processed["Resources"][name]) for name in TARGETS
            },
            "parameters": {
                name: deepcopy(original["Parameters"][name]) for name in PARAMETER_SUFFIXES
            },
            "note": "Restore these resources and binding parameters; preserve other live drift. "
            "Existing pricing/source records, products and publication state are not rolled back.",
        },
    }
    documents["manifest.json"] = {
        "format": "gemma4-prep-query-release-plan-v1",
        "aws_calls": 0,
        "deployment_performed": False,
        "lambda_targets": list(TARGETS),
        "archive_identities": archives,
        "baseline_sha256": {
            "original": _digest(original),
            "processed": _digest(processed),
            "runtime": _digest(runtime),
        },
        "documents_sha256": {name: _digest(value) for name, value in documents.items()},
        "required_live_checks": [
            "Fresh actual function code/environment inventory, including direct-update drift",
            "Exact versioned S3 artifact checksums and new runtime READY with matching environment",
            "Custom endpoint exact liveVersion and READY; retain predecessor endpoint",
            "Change-set scope: prep/query Lambdas and endpoint permission only; API unchanged",
            "Other function code/environment and all API routes/integrations unchanged",
            "No active preparation at cutover; unpublished controlled preparation reaches review",
        ],
    }
    return documents


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--input",
        type=Path,
        required=True,
        help="JSON object with the keyword arguments to prepare_release",
    )
    parser.add_argument("--output", type=Path, required=True, help="New private plan directory")
    args = parser.parse_args(argv)
    _require(
        args.input.is_file() and args.input.stat().st_size <= 5 * 1024**2,
        "Input must be a bounded local JSON file",
    )
    documents = prepare_release(**json.loads(args.input.read_text()))
    _require(not args.output.exists() and not args.output.is_symlink(), "Output must be new")
    _require(
        not any(p.is_symlink() for p in args.output.parents), "Output must not traverse symlinks"
    )
    args.output.mkdir(mode=0o700, parents=True)
    for name, document in documents.items():
        with os.fdopen(
            os.open(args.output / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb"
        ) as out:
            out.write(_canonical(document))
    print(f"Offline release plan prepared: {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
