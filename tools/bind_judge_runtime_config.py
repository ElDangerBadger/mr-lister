"""Prepare a separate, public-safe /judge/ runtime config from verified stack captures.

Local only: no AWS calls, subprocesses, uploads, or static-build changes. Both captures
use the canonical four-field StackId/StackName/StackStatus/Outputs format accepted by
the seller binder. The judge capture must include the enabled IdP output and match an
explicitly supplied deployed stack ARN. Secret values are never accepted or emitted.
"""

from __future__ import annotations

import argparse
import json
import re
import stat
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path

from tools.bind_phase6_runtime_config import (
    _CREDENTIAL_MATERIAL,
    _PLACEHOLDER,
    APPLICATION_ORIGIN,
    COGNITO_ORIGIN,
    PRIVATE_ROOT,
    ROOT,
    RUNTIME_CONFIG_CACHE_CONTROL,
    RUNTIME_CONFIG_CONTENT_TYPE,
    _canonical_json,
    _path_has_symlink_component,
    _prepare_private_directory,
    _unique_json_object,
    load_phase6_web_stack_outputs,
    render_phase6_runtime_config,
)

OBJECT_KEY = "judge/runtime-config.json"
MANIFEST_FORMAT = "mr-lister-judge-runtime-config-upload-v1"
JUDGE_OUTPUT_KEYS = frozenset(
    {
        "JudgeUserPoolId",
        "JudgeBrokerClientId",
        "JudgeBrokerClientSecretArn",
        "JudgeSignInOrigin",
        "JudgeIssuer",
        "PrimaryBrokerCallback",
        "JudgeApplicationCallback",
        "JudgePrimaryLogoutRelay",
        "JudgeSignedOutDestination",
        "IdentityProviderName",
    }
)
_STACK_ID = re.compile(
    r"^arn:aws:cloudformation:us-west-2:384627057108:stack/"
    r"(?P<name>mr-lister-[A-Za-z0-9-]{1,100})/"
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)
_POOL_ID = re.compile(r"^us-west-2_[A-Za-z0-9]{1,55}$")
_CLIENT_ID = re.compile(r"^[a-z0-9]{8,128}$")
_SIGN_IN_ORIGIN = re.compile(
    r"^https://[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
    r"\.auth\.us-west-2\.amazoncognito\.com$"
)
_SECRET_ARN = re.compile(
    r"^arn:aws:secretsmanager:us-west-2:384627057108:secret:"
    r"mr-lister/dev/judge-access/broker-client-[A-Za-z0-9]{6}$"
)
_JOB_ID = re.compile(r"^job_[a-z0-9]{32}$")
_ERROR = "Judge runtime configuration binding is invalid"


class JudgeRuntimeConfigBindingError(RuntimeError):
    """Value-free error; captures and any rejected material remain private."""


@dataclass(frozen=True, slots=True)
class JudgeRuntimeConfigArtifact:
    runtime_config_path: Path
    upload_manifest_path: Path
    sha256: str
    size_bytes: int
    object_key: str = OBJECT_KEY
    content_type: str = RUNTIME_CONFIG_CONTENT_TYPE
    cache_control: str = RUNTIME_CONFIG_CACHE_CONTROL


def _judge_outputs(
    capture_path: Path, *, repository: Path, expected_stack_id: str
) -> tuple[dict[str, str], bytes]:
    match = _STACK_ID.fullmatch(expected_stack_id)
    capture = capture_path if capture_path.is_absolute() else repository / capture_path
    if (
        match is None
        or ".." in capture.parts
        or _path_has_symlink_component(repository, capture)
        or not capture.is_file()
        or not 0 < capture.stat().st_size <= 131_072
    ):
        raise ValueError
    raw = capture.read_bytes()
    document = json.loads(raw, object_pairs_hook=_unique_json_object)
    if (
        not isinstance(document, dict)
        or set(document) != {"StackId", "StackName", "StackStatus", "Outputs"}
        or document["StackId"] != expected_stack_id
        or document["StackName"] != match["name"]
        or document["StackStatus"] not in {"CREATE_COMPLETE", "UPDATE_COMPLETE"}
        or raw != _canonical_json(document)
        or not isinstance(document["Outputs"], list)
        or len(document["Outputs"]) != len(JUDGE_OUTPUT_KEYS)
    ):
        raise ValueError
    outputs: dict[str, str] = {}
    for record in document["Outputs"]:
        if not isinstance(record, dict) or set(record) != {"OutputKey", "OutputValue"}:
            raise ValueError
        key, value = record["OutputKey"], record["OutputValue"]
        if (
            not isinstance(key, str)
            or key not in JUDGE_OUTPUT_KEYS
            or key in outputs
            or not isinstance(value, str)
            or not value
            or value != value.strip()
            or not value.isascii()
            or len(value) > 2048
            or _PLACEHOLDER.search(value)
            or _CREDENTIAL_MATERIAL.search(value)
        ):
            raise ValueError
        outputs[key] = value
    if set(outputs) != JUDGE_OUTPUT_KEYS or list(outputs) != sorted(outputs):
        raise ValueError
    return outputs, raw


def render_judge_runtime_config(
    phase6_capture_path: Path,
    judge_capture_path: Path,
    *,
    expected_judge_stack_id: str,
    prepared_job_id: str | None = None,
    repository_root: Path = ROOT,
) -> tuple[bytes, bytes]:
    """Derive the companion config, preserving primary authority and seller scopes."""
    try:
        repository = repository_root.resolve(strict=True)
        if repository_root.is_symlink() or not repository.is_dir():
            raise ValueError
        normal_raw, _ = render_phase6_runtime_config(
            phase6_capture_path, repository_root=repository
        )
        normal_outputs = load_phase6_web_stack_outputs(
            phase6_capture_path, repository_root=repository
        )
        outputs, judge_raw = _judge_outputs(
            judge_capture_path,
            repository=repository,
            expected_stack_id=expected_judge_stack_id,
        )
        pool_id = outputs["JudgeUserPoolId"]
        client_id = outputs["JudgeBrokerClientId"]
        origin = outputs["JudgeSignInOrigin"]
        if (
            not _POOL_ID.fullmatch(pool_id)
            or pool_id == normal_outputs["SellerUserPoolId"]
            or not _CLIENT_ID.fullmatch(client_id)
            or client_id == normal_outputs["SellerUserPoolClientId"]
            or not _SIGN_IN_ORIGIN.fullmatch(origin)
            or origin == COGNITO_ORIGIN
            or not _SECRET_ARN.fullmatch(outputs["JudgeBrokerClientSecretArn"])
            or outputs["JudgeIssuer"] != f"https://cognito-idp.us-west-2.amazonaws.com/{pool_id}"
            or outputs["PrimaryBrokerCallback"] != f"{COGNITO_ORIGIN}/oauth2/idpresponse"
            or outputs["JudgeApplicationCallback"] != f"{APPLICATION_ORIGIN}/judge/auth/callback"
            or outputs["JudgePrimaryLogoutRelay"] != f"{APPLICATION_ORIGIN}/judge/signout"
            or outputs["JudgeSignedOutDestination"] != f"{APPLICATION_ORIGIN}/judge/"
            or outputs["IdentityProviderName"] != "MrListerJudge"
            or (
                prepared_job_id is not None
                and (not isinstance(prepared_job_id, str) or not _JOB_ID.fullmatch(prepared_job_id))
            )
        ):
            raise ValueError
        judge_access = {
            "identity_provider": "MrListerJudge",
            "upstream_logout_url": f"{origin}/logout",
            "upstream_client_id": client_id,
        }
        if prepared_job_id is not None:
            judge_access["prepared_job_id"] = prepared_job_id
        runtime = json.loads(normal_raw)
        runtime["redirect_uri"] = outputs["JudgeApplicationCallback"]
        runtime["judge_access"] = judge_access
        runtime_raw = _canonical_json(runtime)
        manifest = {
            "algorithm": "sha256",
            "cache_control": RUNTIME_CONFIG_CACHE_CONTROL,
            "content_type": RUNTIME_CONFIG_CONTENT_TYPE,
            "format": MANIFEST_FORMAT,
            "object_key": OBJECT_KEY,
            "sha256": sha256(runtime_raw).hexdigest(),
            "size_bytes": len(runtime_raw),
            "normal_runtime_config_sha256": sha256(normal_raw).hexdigest(),
            "judge_stack_capture_sha256": sha256(judge_raw).hexdigest(),
            "judge_stack_id": expected_judge_stack_id,
        }
        return runtime_raw, _canonical_json(manifest)
    except Exception:
        raise JudgeRuntimeConfigBindingError(_ERROR) from None


def write_judge_runtime_config(
    phase6_capture_path: Path,
    judge_capture_path: Path,
    destination_directory: Path,
    *,
    expected_judge_stack_id: str,
    prepared_job_id: str | None = None,
    repository_root: Path = ROOT,
) -> JudgeRuntimeConfigArtifact:
    """Create-only private artifact and upload receipt; never touch web/dist."""
    try:
        repository = repository_root.resolve(strict=True)
        private_root = repository / PRIVATE_ROOT
        destination = (
            destination_directory
            if destination_directory.is_absolute()
            else repository / destination_directory
        )
        if (
            ".." in destination.parts
            or destination == private_root
            or not destination.is_relative_to(private_root)
            or _path_has_symlink_component(repository, destination)
        ):
            raise ValueError
        target = destination / "judge"
        runtime_path = target / "runtime-config.json"
        manifest_path = target / "runtime-config.upload.json"
        if _path_has_symlink_component(repository, target) or any(
            path.exists() or path.is_symlink() for path in (runtime_path, manifest_path)
        ):
            raise ValueError
        runtime_raw, manifest_raw = render_judge_runtime_config(
            phase6_capture_path,
            judge_capture_path,
            expected_judge_stack_id=expected_judge_stack_id,
            prepared_job_id=prepared_job_id,
            repository_root=repository_root,
        )
        _prepare_private_directory(repository, private_root)
        _prepare_private_directory(private_root, target)
        for path, raw in ((runtime_path, runtime_raw), (manifest_path, manifest_raw)):
            with path.open("xb") as stream:
                path.chmod(0o600)
                stream.write(raw)
            if path.read_bytes() != raw or stat.S_IMODE(path.stat().st_mode) != 0o600:
                raise ValueError
        return JudgeRuntimeConfigArtifact(
            runtime_path, manifest_path, sha256(runtime_raw).hexdigest(), len(runtime_raw)
        )
    except Exception:
        raise JudgeRuntimeConfigBindingError(_ERROR) from None


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--phase6-capture", required=True, type=Path)
    parser.add_argument("--judge-capture", required=True, type=Path)
    parser.add_argument("--judge-stack-id", required=True)
    parser.add_argument("--destination", required=True, type=Path)
    parser.add_argument("--prepared-job-id")
    args = parser.parse_args()
    try:
        result = write_judge_runtime_config(
            args.phase6_capture,
            args.judge_capture,
            args.destination,
            expected_judge_stack_id=args.judge_stack_id,
            prepared_job_id=args.prepared_job_id,
        )
        print(result.runtime_config_path)
        print(result.upload_manifest_path)
    except JudgeRuntimeConfigBindingError as error:
        parser.exit(2, f"{error}\n")


if __name__ == "__main__":
    main()
