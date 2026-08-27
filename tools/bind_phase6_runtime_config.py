"""Bind one canonical Phase 6 browser runtime config from captured stack outputs.

This tool is local-only.  It reads a normalized CloudFormation output capture, verifies the exact
deployed Phase 6 stack and public browser configuration, then creates two private files: the six-
field ``runtime-config.json`` object and a companion upload manifest.  It never imports an AWS SDK,
starts a subprocess, or uploads an object.
"""

from __future__ import annotations

import argparse
import json
import re
import stat
from collections.abc import Mapping
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PRIVATE_ROOT = Path(".mr_lister_private")

STACK_ID = (
    "arn:aws:cloudformation:us-west-2:384627057108:stack/"
    "mr-lister-phase6-dev/f3456970-9fdc-11f1-b448-06b81627db1d"
)
STACK_NAME = "mr-lister-phase6-dev"
STACK_STATUS = "UPDATE_COMPLETE"
APPLICATION_ORIGIN = "https://massskutiny.com"
COGNITO_ORIGIN = "https://mr-lister-dev-384627057108.auth.us-west-2.amazoncognito.com"
RUNTIME_CONFIG_OBJECT_KEY = "runtime-config.json"
RUNTIME_CONFIG_FILENAME = "runtime-config.json"
UPLOAD_MANIFEST_FILENAME = "runtime-config.upload.json"
UPLOAD_MANIFEST_FORMAT = "mr-lister-phase6-runtime-config-upload-v1"
RUNTIME_CONFIG_CONTENT_TYPE = "application/json"
RUNTIME_CONFIG_CACHE_CONTROL = "private, no-store, max-age=0"

EXPECTED_CLOUDFORMATION_OUTPUT_KEYS = frozenset(
    {
        "ArtifactBucketBrowserOrigin",
        "ArtifactBucketName",
        "DeploymentReadiness",
        "OperationalAlarmTopicArn",
        "PrepareStateMachineArn",
        "ReconcileProductStateMachineArn",
        "RefreshEconomicsStateMachineArn",
        "SellerApiOrigin",
        "SellerApplicationOrigin",
        "SellerRuntimeConfig",
        "SellerRuntimeConfigObjectKey",
        "SellerSignInOrigin",
        "SellerUserPoolClientId",
        "SellerUserPoolId",
        "SellerWebAssetBucketName",
        "SellerWebDistributionDomainName",
        "SellerWebDistributionId",
        "StateTableName",
        "SynchronizeProductStateMachineArn",
    }
)

_RUNTIME_FIELDS = frozenset(
    {
        "cognito_authorize_url",
        "cognito_token_url",
        "cognito_logout_url",
        "client_id",
        "redirect_uri",
        "scopes",
    }
)
_CAPTURE_FIELDS = frozenset({"Outputs", "StackId", "StackName", "StackStatus"})
_CLIENT_ID = re.compile(r"^[a-z0-9]{8,128}$")
_PLACEHOLDER = re.compile(
    r"<[A-Z][A-Z0-9_]*>|__[A-Z][A-Z0-9_]*__|"
    r"\b(?:PLACEHOLDER|REPLACE_ME|CHANGEME)\b|"
    r"replace-at-deploy-time|example\.com",
    re.IGNORECASE,
)
_CREDENTIAL_MATERIAL = re.compile(
    r"(?:AKIA[0-9A-Z]{16}|ASIA[0-9A-Z]{16}|-----BEGIN [A-Z ]*PRIVATE KEY-----|"
    r"\bBearer\s+[A-Za-z0-9._~+/=-]+)",
    re.IGNORECASE,
)
_FORBIDDEN_RUNTIME_KEYS = frozenset(
    {
        "access_token",
        "authorization",
        "client_secret",
        "cookie",
        "id_token",
        "password",
        "refresh_token",
        "secret",
    }
)
_GENERIC_ERROR = "Phase 6 runtime configuration binding is invalid"


class Phase6RuntimeConfigBindingError(RuntimeError):
    """Value-free failure for malformed, unsafe, or drifting public configuration."""


@dataclass(frozen=True, slots=True)
class Phase6RuntimeConfigArtifact:
    """Paths and immutable upload metadata for one bound runtime config."""

    runtime_config_path: Path
    upload_manifest_path: Path
    sha256: str
    size_bytes: int
    content_type: str = RUNTIME_CONFIG_CONTENT_TYPE
    cache_control: str = RUNTIME_CONFIG_CACHE_CONTROL
    object_key: str = RUNTIME_CONFIG_OBJECT_KEY


def render_phase6_runtime_config(
    capture_path: Path,
    *,
    repository_root: Path = ROOT,
) -> tuple[bytes, bytes]:
    """Return canonical runtime-config and companion upload-manifest bytes."""

    try:
        outputs = load_phase6_web_stack_outputs(
            capture_path,
            repository_root=repository_root,
        )
        runtime = _runtime_config(outputs)
        runtime_raw = _canonical_json(runtime)
        upload_manifest = {
            "algorithm": "sha256",
            "cache_control": RUNTIME_CONFIG_CACHE_CONTROL,
            "content_type": RUNTIME_CONFIG_CONTENT_TYPE,
            "format": UPLOAD_MANIFEST_FORMAT,
            "object_key": RUNTIME_CONFIG_OBJECT_KEY,
            "sha256": sha256(runtime_raw).hexdigest(),
            "size_bytes": len(runtime_raw),
        }
        return runtime_raw, _canonical_json(upload_manifest)
    except Phase6RuntimeConfigBindingError:
        raise
    except Exception:
        raise Phase6RuntimeConfigBindingError(_GENERIC_ERROR) from None


def load_phase6_web_stack_outputs(
    capture_path: Path,
    *,
    repository_root: Path = ROOT,
) -> dict[str, str]:
    """Load the exact identity-bearing, canonical Phase 6 stack-output capture."""

    try:
        repository = repository_root.resolve(strict=True)
        capture = capture_path.resolve(strict=True)
        if (
            repository_root.is_symlink()
            or not repository.is_dir()
            or capture_path.is_symlink()
            or not capture.is_file()
            or not capture.is_relative_to(repository)
            or _path_has_symlink_component(repository, capture)
            or capture.stat().st_size <= 0
            or capture.stat().st_size > 131_072
        ):
            raise ValueError
        raw = capture.read_bytes()
        document = json.loads(raw, object_pairs_hook=_unique_json_object)
        if (
            not isinstance(document, Mapping)
            or set(document) != _CAPTURE_FIELDS
            or document.get("StackId") != STACK_ID
            or document.get("StackName") != STACK_NAME
            or document.get("StackStatus") != STACK_STATUS
            or raw != _canonical_json(document)
        ):
            raise ValueError

        records = document.get("Outputs")
        if not isinstance(records, list) or len(records) != len(
            EXPECTED_CLOUDFORMATION_OUTPUT_KEYS
        ):
            raise ValueError
        outputs: dict[str, str] = {}
        for record in records:
            if not isinstance(record, Mapping) or set(record) != {"OutputKey", "OutputValue"}:
                raise ValueError
            key = record.get("OutputKey")
            value = record.get("OutputValue")
            if (
                not isinstance(key, str)
                or key in outputs
                or not isinstance(value, str)
                or not value
                or value != value.strip()
                or not value.isascii()
                or len(value) > 16_384
                or _PLACEHOLDER.search(value) is not None
                or _CREDENTIAL_MATERIAL.search(value) is not None
            ):
                raise ValueError
            outputs[key] = value
        if set(outputs) != EXPECTED_CLOUDFORMATION_OUTPUT_KEYS or [
            record["OutputKey"] for record in records
        ] != sorted(outputs):
            raise ValueError
        _validate_output_bindings(outputs)
        return outputs
    except Phase6RuntimeConfigBindingError:
        raise
    except Exception:
        raise Phase6RuntimeConfigBindingError(_GENERIC_ERROR) from None


def write_phase6_runtime_config(
    capture_path: Path,
    destination_directory: Path,
    *,
    repository_root: Path = ROOT,
) -> Phase6RuntimeConfigArtifact:
    """Exclusively create and read back one runtime config plus upload manifest."""

    try:
        repository = repository_root.resolve(strict=True)
        private_root = repository / PRIVATE_ROOT
        destination = (
            destination_directory
            if destination_directory.is_absolute()
            else repository / destination_directory
        ).resolve(strict=False)
        if destination == private_root or not destination.is_relative_to(private_root):
            raise ValueError
        runtime_path = destination / RUNTIME_CONFIG_FILENAME
        manifest_path = destination / UPLOAD_MANIFEST_FILENAME
        if any(path.exists() or path.is_symlink() for path in (runtime_path, manifest_path)):
            raise ValueError

        runtime_raw, manifest_raw = render_phase6_runtime_config(
            capture_path,
            repository_root=repository,
        )
        _prepare_private_directory(repository, private_root)
        _prepare_private_directory(private_root, destination)
        for path, raw in ((runtime_path, runtime_raw), (manifest_path, manifest_raw)):
            with path.open("xb") as stream:
                stream.write(raw)
            path.chmod(0o600)
        if (
            runtime_path.read_bytes() != runtime_raw
            or manifest_path.read_bytes() != manifest_raw
            or stat.S_IMODE(runtime_path.stat().st_mode) != 0o600
            or stat.S_IMODE(manifest_path.stat().st_mode) != 0o600
        ):
            raise ValueError
        return Phase6RuntimeConfigArtifact(
            runtime_config_path=runtime_path,
            upload_manifest_path=manifest_path,
            sha256=sha256(runtime_raw).hexdigest(),
            size_bytes=len(runtime_raw),
        )
    except Phase6RuntimeConfigBindingError:
        raise
    except Exception:
        raise Phase6RuntimeConfigBindingError(_GENERIC_ERROR) from None


def _runtime_config(outputs: Mapping[str, str]) -> dict[str, object]:
    raw = outputs["SellerRuntimeConfig"]
    try:
        value = json.loads(raw, object_pairs_hook=_unique_json_object)
    except (TypeError, ValueError):
        raise ValueError from None
    if not isinstance(value, Mapping) or set(value) != _RUNTIME_FIELDS:
        raise ValueError
    if set(value).intersection(_FORBIDDEN_RUNTIME_KEYS):
        raise ValueError
    expected = _expected_runtime_config(outputs["SellerUserPoolClientId"])
    if value != expected or raw != _cloudformation_runtime_json(expected):
        raise ValueError
    return dict(value)


def _expected_runtime_config(client_id: str) -> dict[str, object]:
    return {
        "cognito_authorize_url": f"{COGNITO_ORIGIN}/oauth2/authorize",
        "cognito_token_url": f"{COGNITO_ORIGIN}/oauth2/token",
        "cognito_logout_url": f"{COGNITO_ORIGIN}/logout",
        "client_id": client_id,
        "redirect_uri": f"{APPLICATION_ORIGIN}/auth/callback",
        "scopes": ["openid", "mr-lister-api/seller"],
    }


def _validate_output_bindings(outputs: Mapping[str, str]) -> None:
    client_id = outputs.get("SellerUserPoolClientId")
    if (
        outputs.get("DeploymentReadiness") != "WEB_EDGE_ACTIVE_DRAFT_ONLY"
        or outputs.get("SellerApplicationOrigin") != APPLICATION_ORIGIN
        or outputs.get("SellerSignInOrigin") != COGNITO_ORIGIN
        or outputs.get("SellerRuntimeConfigObjectKey") != RUNTIME_CONFIG_OBJECT_KEY
        or not isinstance(client_id, str)
        or _CLIENT_ID.fullmatch(client_id) is None
        or outputs.get("ArtifactBucketName")
        != "mr-lister-phase6-artifacts-dev-384627057108-us-west-2"
        or outputs.get("SellerWebAssetBucketName")
        != "mr-lister-phase6-web-dev-384627057108-us-west-2"
        or outputs.get("StateTableName") != STACK_NAME
    ):
        raise ValueError
    _runtime_config(outputs)


def _cloudformation_runtime_json(value: Mapping[str, object]) -> str:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=True,
        separators=(",", ":"),
    )


def _prepare_private_directory(root: Path, directory: Path) -> None:
    if not directory.is_relative_to(root):
        raise ValueError
    current = root
    for component in directory.relative_to(root).parts:
        current = current / component
        if current.exists():
            if current.is_symlink() or not current.is_dir():
                raise ValueError
        else:
            current.mkdir(mode=0o700)


def _path_has_symlink_component(root: Path, path: Path) -> bool:
    if not path.is_relative_to(root):
        return True
    current = root
    for component in path.relative_to(root).parts:
        current = current / component
        if current.is_symlink():
            return True
    return False


def _unique_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError
        value[key] = item
    return value


def _canonical_json(value: object) -> bytes:
    return (
        json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--capture", required=True, type=Path)
    parser.add_argument("--destination", required=True, type=Path)
    arguments = parser.parse_args()
    try:
        artifact = write_phase6_runtime_config(arguments.capture, arguments.destination)
        print(artifact.runtime_config_path)
        print(artifact.upload_manifest_path)
    except Phase6RuntimeConfigBindingError as error:
        parser.exit(2, f"{error}\n")


__all__ = [
    "APPLICATION_ORIGIN",
    "COGNITO_ORIGIN",
    "EXPECTED_CLOUDFORMATION_OUTPUT_KEYS",
    "Phase6RuntimeConfigArtifact",
    "Phase6RuntimeConfigBindingError",
    "RUNTIME_CONFIG_CACHE_CONTROL",
    "RUNTIME_CONFIG_CONTENT_TYPE",
    "RUNTIME_CONFIG_OBJECT_KEY",
    "STACK_ID",
    "STACK_NAME",
    "STACK_STATUS",
    "UPLOAD_MANIFEST_FORMAT",
    "load_phase6_web_stack_outputs",
    "render_phase6_runtime_config",
    "write_phase6_runtime_config",
]


if __name__ == "__main__":
    main()
