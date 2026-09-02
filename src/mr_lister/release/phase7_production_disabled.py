"""Fail-closed authority for the sealed Phase 7 production-disabled candidate.

This verifier is deliberately standard-library-only apart from the already sealed Phase 6
release authority.  It authenticates every packaged byte, the frozen Phase 7.0.1 contract, the
checked product profile, and the exact disabled environment before a handler may be constructed.
It never imports application code, an AWS SDK, a provider integration, or a credential boundary.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path, PurePosixPath
from typing import cast
from urllib.parse import urlsplit

from mr_lister.release.phase6 import (
    DEPENDENCY_ARTIFACT_FILENAME,
    DEPENDENCY_BUILD_REQUEST_FILENAME,
    LINUX_ARM64_TARGET,
    Phase6ReleaseAuthorityError,
    render_manifest,
    verify_linux_arm64_dependency_artifact,
    wheel_authority_from_build_request,
)

PRODUCTION_DISABLED_ENTRYPOINTS = (
    "mr_lister.cloud.phase7_production_entrypoints.publication_query_handler",
    "mr_lister.cloud.phase7_production_entrypoints.publication_request_handler",
    "mr_lister.cloud.phase7_production_entrypoints.publication_dispatcher_handler",
    "mr_lister.cloud.phase7_production_entrypoints.publication_worker_handler",
    "mr_lister.cloud.phase7_production_entrypoints.publication_recovery_handler",
    "mr_lister.cloud.phase7_production_entrypoints.publication_retention_handler",
)

PRODUCTION_DISABLED_RELEASE_FINGERPRINT_ENV = "MR_LISTER_PHASE7_PRODUCTION_RELEASE_FINGERPRINT"
APPLICATION_RELEASE_FINGERPRINT_ENV = "MR_LISTER_RELEASE_FINGERPRINT"
CONTRACT_FINGERPRINT_ENV = "MR_LISTER_PHASE7_CONTRACT_FINGERPRINT"
CONTRACT_VERSION_ENV = "MR_LISTER_PHASE7_CONTRACT_VERSION"
ACTIVATION_MODE_ENV = "MR_LISTER_PHASE7_ACTIVATION_MODE"
PROFILE_ID_ENV = "MR_LISTER_PRODUCT_PROFILE_ID"
PROFILE_VERSION_ENV = "MR_LISTER_PRODUCT_PROFILE_VERSION"
PROFILE_FINGERPRINT_ENV = "MR_LISTER_PRODUCT_PROFILE_FINGERPRINT"
PROFILE_PATH_ENV = "MR_LISTER_PRODUCT_PROFILE_PATH"

SCAFFOLD_ONLY_ENV = "MR_LISTER_PHASE7_SCAFFOLD_ONLY"
QUERY_ENABLED_ENV = "MR_LISTER_PHASE7_QUERY_ENABLED"
REQUEST_ENABLED_ENV = "MR_LISTER_PHASE7_REQUEST_ENABLED"
PUBLICATION_ENABLED_ENV = "MR_LISTER_PHASE7_PUBLICATION_ENABLED"
PRODUCTION_CANDIDATE_ENABLED_ENV = "MR_LISTER_PHASE7_PRODUCTION_CANDIDATE_ENABLED"
REGION_ENV = "AWS_REGION"
STATE_TABLE_ENV = "MR_LISTER_STATE_TABLE"
COGNITO_ISSUER_ENV = "MR_LISTER_COGNITO_ISSUER"
COGNITO_CLIENT_ID_ENV = "MR_LISTER_COGNITO_CLIENT_ID"
COGNITO_SCOPE_ENV = "MR_LISTER_COGNITO_SCOPE"
COGNITO_GROUP_ENV = "MR_LISTER_COGNITO_GROUP"

PRODUCTION_DISABLED_ENVIRONMENT_NAMES = (
    PRODUCTION_DISABLED_RELEASE_FINGERPRINT_ENV,
    APPLICATION_RELEASE_FINGERPRINT_ENV,
    CONTRACT_FINGERPRINT_ENV,
    CONTRACT_VERSION_ENV,
    ACTIVATION_MODE_ENV,
    PROFILE_ID_ENV,
    PROFILE_VERSION_ENV,
    PROFILE_FINGERPRINT_ENV,
    PROFILE_PATH_ENV,
    SCAFFOLD_ONLY_ENV,
    QUERY_ENABLED_ENV,
    REQUEST_ENABLED_ENV,
    PUBLICATION_ENABLED_ENV,
    PRODUCTION_CANDIDATE_ENABLED_ENV,
    REGION_ENV,
    STATE_TABLE_ENV,
    COGNITO_ISSUER_ENV,
    COGNITO_CLIENT_ID_ENV,
    COGNITO_SCOPE_ENV,
    COGNITO_GROUP_ENV,
)

# These application-owned names would grant or locate runtime capability.  The disabled
# entrypoint checks presence without reading their values and refuses before release verification.
FORBIDDEN_PRODUCTION_CAPABILITY_ENVIRONMENT_NAMES = frozenset(
    {
        "MR_LISTER_ETSY_API_KEY",
        "MR_LISTER_ETSY_API_SECRET",
        "MR_LISTER_ETSY_TOKEN",
        "MR_LISTER_PRINTIFY_API_KEY",
        "MR_LISTER_PRINTIFY_SECRET_ARN",
        "MR_LISTER_PUBLICATION_RECOVERY_QUEUE_URL",
        "MR_LISTER_PUBLICATION_WORKFLOW_ARN",
    }
)

CONTRACT_PATH = "contracts/publication/phase7.0.1.json"
CONTRACT_VERSION = "7.0.1"
CONTRACT_FINGERPRINT = "548b710230618e73c20a509f2121799c415b50070e1e2ae7e1b82fe3c37e2981"
ACTIVATION_MODE = "SOURCE_ONLY_DISABLED"
PROFILE_PATH = "config/product_profiles/gildan_64000_swiftpod.json"
PROFILE_ID = "gildan_64000_swiftpod"
PROFILE_VERSION = 2
PROFILE_FINGERPRINT = "5de1257141cfdacb1731f68bb9113712957483b33d5b0f7115afdba86eb7476c"
PROFILE_FILE_FINGERPRINT = "eb9b7769e0049f7b270da70caa06fb321d56e1a4c37a280bb6265ba3544aae40"
TOPOLOGY_BINDING_FILENAME = "phase7-topology-binding.json"
PRODUCTION_DISABLED_TEMPLATE_PATH = "infra/phase7/production-disabled-template.json"
PRODUCTION_DISABLED_TEMPLATE_FINGERPRINT = (
    "2a98ab2a7cf3fb04590f9f8cd3a30cc6c2e373421e70c70220be419b80ca7df2"
)
PUBLICATION_WORKFLOW_PATH = "infra/phase7/statemachine/publication.asl.json"
PUBLICATION_WORKFLOW_FINGERPRINT = (
    "9a6112c85b35e775d1e60681a0ca14e6740cd0aea82b2ac33b5aa74b86fc3098"
)
PRODUCTION_THIRD_PARTY_IMPORT_ROOTS = ("PIL", "boto3", "botocore", "pydantic")
PHASE6_LAMBDA_WHEEL_AUTHORITY_FINGERPRINT = (
    "145ae4affca308e4268120e5f5f725d1e91ba3194db76585e53cba32d29eefbd"
)

SOURCE_MANIFEST_FILENAME = "source-manifest.json"
DEPLOYMENT_MANIFEST_FILENAME = "deployment-manifest.json"
RELEASE_MANIFEST_FILENAME = "release-manifest.json"

_COMPONENT = "phase7-production-disabled-lambda"
_SOURCE_FORMAT = "phase7-production-disabled-source-v1"
_DEPLOYMENT_FORMAT = "phase7-production-disabled-deployment-v1"
_RELEASE_FORMAT = "phase7-production-disabled-release-v1"
_FINGERPRINT = re.compile(r"^[a-f0-9]{64}$")
_REGION = re.compile(r"^[a-z]{2}(?:-[a-z0-9]+){1,2}-[1-9][0-9]?$|^us-gov-[a-z]+-[1-9]$")
_STATE_TABLE = re.compile(r"^mr-lister-phase6-(?P<environment>[a-z][a-z0-9-]{1,15})$")
_CLIENT_ID = re.compile(r"^[A-Za-z0-9]{1,128}$")
_USER_POOL_ID = re.compile(r"^[a-z0-9-]+_[A-Za-z0-9]{1,64}$")
_MAX_MANIFEST_BYTES = 4 * 1024 * 1024
_GENERIC_ERROR = "Phase 7 production-disabled release authority is invalid"
_FORBIDDEN_SOURCE_PARTS = frozenset(
    {
        ".DS_Store",
        ".env",
        ".git",
        ".mr_lister_private",
        ".playwright-mcp",
        "__pycache__",
        "browser",
        "docs",
        "infra",
        "tests",
        "web",
    }
)
_FORBIDDEN_SOURCE_SUFFIXES = frozenset(
    {".dylib", ".dll", ".key", ".map", ".pem", ".pyc", ".pyd", ".whl", ".zip"}
)


class Phase7ProductionDisabledReleaseAuthorityError(RuntimeError):
    """Value-free refusal for malformed, drifting, or unsealed candidate bytes."""


@dataclass(frozen=True, slots=True)
class Phase7ProductionDisabledReleaseBinding:
    """Exact identities authenticated before a refusal handler is constructed."""

    component: str
    entrypoint: str
    release_fingerprint: str
    application_release_fingerprint: str
    deployment_manifest_fingerprint: str
    source_manifest_fingerprint: str
    dependency_manifest_fingerprint: str
    contract_fingerprint: str
    profile_fingerprint: str
    topology_binding_fingerprint: str
    production_disabled_template_fingerprint: str
    publication_workflow_fingerprint: str


def verify_phase7_production_disabled_release(
    environment: Mapping[str, object],
    *,
    expected_entrypoint: str,
    bundle_root: Path | None = None,
) -> Phase7ProductionDisabledReleaseBinding:
    """Authenticate one exact handler, the disabled environment, and every packaged byte."""

    try:
        if not isinstance(environment, Mapping):
            raise ValueError
        _verify_disabled_environment_shape(environment)
        if expected_entrypoint not in PRODUCTION_DISABLED_ENTRYPOINTS:
            raise ValueError
        expected_release = _required_fingerprint(
            environment,
            PRODUCTION_DISABLED_RELEASE_FINGERPRINT_ENV,
        )
        expected_application = _required_fingerprint(
            environment,
            APPLICATION_RELEASE_FINGERPRINT_ENV,
        )
        root = _exact_directory(bundle_root or Path(__file__).resolve().parents[2])
        if environment.get(PROFILE_PATH_ENV) != (root / PROFILE_PATH).as_posix():
            raise ValueError

        release_bytes, release = _read_canonical_manifest(root / RELEASE_MANIFEST_FILENAME)
        release_fingerprint = sha256(release_bytes).hexdigest()
        if release_fingerprint != expected_release:
            raise ValueError
        _require_exact_keys(
            release,
            {
                "algorithm",
                "component",
                "contract_fingerprint",
                "dependency_manifest_sha256",
                "deployment_manifest_sha256",
                "entrypoints",
                "format",
                "profile_fingerprint",
                "production_disabled_template_sha256",
                "publication_workflow_sha256",
                "source_manifest_sha256",
                "target",
                "topology_binding_sha256",
            },
        )
        if (
            release["algorithm"] != "sha256"
            or release["component"] != _COMPONENT
            or release["contract_fingerprint"] != CONTRACT_FINGERPRINT
            or release["entrypoints"] != list(PRODUCTION_DISABLED_ENTRYPOINTS)
            or release["format"] != _RELEASE_FORMAT
            or release["profile_fingerprint"] != PROFILE_FINGERPRINT
            or release["production_disabled_template_sha256"]
            != PRODUCTION_DISABLED_TEMPLATE_FINGERPRINT
            or release["publication_workflow_sha256"] != PUBLICATION_WORKFLOW_FINGERPRINT
            or release["target"] != LINUX_ARM64_TARGET
        ):
            raise ValueError

        deployment_fingerprint = _nonzero_fingerprint(release["deployment_manifest_sha256"])
        source_fingerprint = _nonzero_fingerprint(release["source_manifest_sha256"])
        dependency_fingerprint = _nonzero_fingerprint(release["dependency_manifest_sha256"])
        topology_fingerprint = _nonzero_fingerprint(release["topology_binding_sha256"])
        if expected_application != release_fingerprint:
            raise ValueError

        deployment_bytes, deployment = _read_canonical_manifest(root / DEPLOYMENT_MANIFEST_FILENAME)
        source_bytes, source = _read_canonical_manifest(root / SOURCE_MANIFEST_FILENAME)
        dependency_bytes, _dependency = _read_canonical_manifest(
            root / DEPENDENCY_ARTIFACT_FILENAME
        )
        if (
            sha256(deployment_bytes).hexdigest() != deployment_fingerprint
            or sha256(source_bytes).hexdigest() != source_fingerprint
            or sha256(dependency_bytes).hexdigest() != dependency_fingerprint
        ):
            raise ValueError

        _verify_deployment_manifest(root, deployment)
        _verify_source_manifest(root, source, allow_dependency_files=True)
        if (
            sha256((root / TOPOLOGY_BINDING_FILENAME).read_bytes()).hexdigest()
            != topology_fingerprint
        ):
            raise ValueError
        verified_dependency = verify_linux_arm64_dependency_artifact(
            root,
            build_request_path=root / DEPENDENCY_BUILD_REQUEST_FILENAME,
            allow_extra_files=True,
        )
        if verified_dependency.get("format") != "phase6-linux-arm64-dependencies-v2":
            raise ValueError
        _verify_checked_dependency_build_request(root / DEPENDENCY_BUILD_REQUEST_FILENAME)

        return Phase7ProductionDisabledReleaseBinding(
            component=_COMPONENT,
            entrypoint=expected_entrypoint,
            release_fingerprint=release_fingerprint,
            application_release_fingerprint=release_fingerprint,
            deployment_manifest_fingerprint=deployment_fingerprint,
            source_manifest_fingerprint=source_fingerprint,
            dependency_manifest_fingerprint=dependency_fingerprint,
            contract_fingerprint=CONTRACT_FINGERPRINT,
            profile_fingerprint=PROFILE_FINGERPRINT,
            topology_binding_fingerprint=topology_fingerprint,
            production_disabled_template_fingerprint=(PRODUCTION_DISABLED_TEMPLATE_FINGERPRINT),
            publication_workflow_fingerprint=PUBLICATION_WORKFLOW_FINGERPRINT,
        )
    except (Phase6ReleaseAuthorityError, Phase7ProductionDisabledReleaseAuthorityError):
        raise Phase7ProductionDisabledReleaseAuthorityError(_GENERIC_ERROR) from None
    except Exception:
        raise Phase7ProductionDisabledReleaseAuthorityError(_GENERIC_ERROR) from None


def verify_phase7_production_disabled_source_manifest(root: Path) -> None:
    """Verify the deterministic source stage before dependency overlay."""

    try:
        source_root = _exact_directory(root)
        _raw, source = _read_canonical_manifest(source_root / SOURCE_MANIFEST_FILENAME)
        _verify_source_manifest(source_root, source, allow_dependency_files=False)
    except Phase7ProductionDisabledReleaseAuthorityError:
        raise
    except Exception:
        raise Phase7ProductionDisabledReleaseAuthorityError(_GENERIC_ERROR) from None


def inventory(root: Path, *, excluded: frozenset[str]) -> list[dict[str, object]]:
    """Return a sorted, path-safe SHA256 inventory without following links."""

    files: list[dict[str, object]] = []
    for path in sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix()):
        if path.is_symlink():
            raise ValueError
        if not path.is_file():
            continue
        relative = path.relative_to(root).as_posix()
        _require_safe_relative(relative)
        if relative in excluded:
            continue
        raw = path.read_bytes()
        files.append(
            {
                "path": relative,
                "sha256": sha256(raw).hexdigest(),
                "size_bytes": len(raw),
            }
        )
    return files


def _verify_disabled_environment_shape(environment: Mapping[str, object]) -> None:
    region = _required_string(environment, REGION_ENV)
    state_table = _required_string(environment, STATE_TABLE_ENV)
    issuer = _required_string(environment, COGNITO_ISSUER_ENV)
    client_id = _required_string(environment, COGNITO_CLIENT_ID_ENV)
    scope = _required_string(environment, COGNITO_SCOPE_ENV)
    group = _required_string(environment, COGNITO_GROUP_ENV)
    if (
        _REGION.fullmatch(region) is None
        or _STATE_TABLE.fullmatch(state_table) is None
        or _CLIENT_ID.fullmatch(client_id) is None
        or scope != "mr-lister-api/seller"
        or group != "seller"
        or environment.get(CONTRACT_FINGERPRINT_ENV) != CONTRACT_FINGERPRINT
        or environment.get(CONTRACT_VERSION_ENV) != CONTRACT_VERSION
        or environment.get(ACTIVATION_MODE_ENV) != ACTIVATION_MODE
        or environment.get(PROFILE_ID_ENV) != PROFILE_ID
        or environment.get(PROFILE_VERSION_ENV) != str(PROFILE_VERSION)
        or environment.get(PROFILE_FINGERPRINT_ENV) != PROFILE_FINGERPRINT
        or environment.get(SCAFFOLD_ONLY_ENV) != "true"
        or environment.get(QUERY_ENABLED_ENV) != "false"
        or environment.get(REQUEST_ENABLED_ENV) != "false"
        or environment.get(PUBLICATION_ENABLED_ENV) != "false"
        or environment.get(PRODUCTION_CANDIDATE_ENABLED_ENV) != "false"
        or any(name in environment for name in FORBIDDEN_PRODUCTION_CAPABILITY_ENVIRONMENT_NAMES)
    ):
        raise ValueError
    _validate_cognito_issuer(issuer, region)


def _validate_cognito_issuer(issuer: str, region: str) -> None:
    parsed = urlsplit(issuer)
    suffix = "amazonaws.com.cn" if region.startswith("cn-") else "amazonaws.com"
    expected_host = f"cognito-idp.{region}.{suffix}"
    pool_id = parsed.path.removeprefix("/")
    if (
        parsed.scheme != "https"
        or parsed.hostname != expected_host
        or parsed.netloc != expected_host
        or parsed.path != f"/{pool_id}"
        or parsed.query
        or parsed.fragment
        or _USER_POOL_ID.fullmatch(pool_id) is None
        or not pool_id.startswith(f"{region}_")
    ):
        raise ValueError


def _required_string(environment: Mapping[str, object], name: str) -> str:
    value = environment.get(name)
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 4_096
        or value != value.strip()
        or "\x00" in value
    ):
        raise ValueError
    return value


def _verify_deployment_manifest(root: Path, manifest: Mapping[str, object]) -> None:
    _require_exact_keys(
        manifest,
        {"algorithm", "component", "entrypoints", "files", "format", "target"},
    )
    if (
        manifest["algorithm"] != "sha256"
        or manifest["component"] != _COMPONENT
        or manifest["entrypoints"] != list(PRODUCTION_DISABLED_ENTRYPOINTS)
        or manifest["format"] != _DEPLOYMENT_FORMAT
        or manifest["target"] != LINUX_ARM64_TARGET
        or manifest["files"]
        != inventory(
            root,
            excluded=frozenset({DEPLOYMENT_MANIFEST_FILENAME, RELEASE_MANIFEST_FILENAME}),
        )
    ):
        raise ValueError


def _verify_source_manifest(
    root: Path,
    manifest: Mapping[str, object],
    *,
    allow_dependency_files: bool,
) -> None:
    _require_exact_keys(
        manifest,
        {
            "algorithm",
            "contract",
            "entrypoints",
            "files",
            "format",
            "profile",
            "target",
            "third_party_import_roots",
            "topology",
        },
    )
    if (
        manifest["algorithm"] != "sha256"
        or manifest["entrypoints"] != list(PRODUCTION_DISABLED_ENTRYPOINTS)
        or manifest["format"] != _SOURCE_FORMAT
        or manifest["target"] != LINUX_ARM64_TARGET
        or manifest["third_party_import_roots"] != list(PRODUCTION_THIRD_PARTY_IMPORT_ROOTS)
    ):
        raise ValueError
    files = manifest["files"]
    _verify_inventory_records(root, files)
    if not allow_dependency_files and files != inventory(
        root,
        excluded=frozenset({SOURCE_MANIFEST_FILENAME}),
    ):
        raise ValueError

    contract = manifest["contract"]
    if not isinstance(contract, Mapping):
        raise ValueError
    _require_exact_keys(
        contract,
        {
            "contract_version",
            "current_activation_phase",
            "path",
            "publication_enabled",
            "sha256",
            "status",
        },
    )
    contract_path = root / CONTRACT_PATH
    contract_raw, contract_payload = _read_canonical_manifest(contract_path)
    if (
        contract
        != {
            "contract_version": CONTRACT_VERSION,
            "current_activation_phase": "offline_implementation",
            "path": CONTRACT_PATH,
            "publication_enabled": False,
            "sha256": CONTRACT_FINGERPRINT,
            "status": "frozen",
        }
        or sha256(contract_raw).hexdigest() != CONTRACT_FINGERPRINT
        or contract_payload.get("phase") != "7"
        or contract_payload.get("contract_version") != CONTRACT_VERSION
        or contract_payload.get("current_activation_phase") != "offline_implementation"
        or contract_payload.get("publication_enabled") is not False
        or contract_payload.get("status") != "frozen"
    ):
        raise ValueError

    profile = manifest["profile"]
    if not isinstance(profile, Mapping):
        raise ValueError
    _require_exact_keys(
        profile,
        {
            "file_sha256",
            "fingerprint",
            "path",
            "profile_id",
            "profile_version",
            "publish_enabled",
        },
    )
    profile_path = root / PROFILE_PATH
    profile_payload = _read_json_mapping(profile_path)
    if (
        profile
        != {
            "file_sha256": PROFILE_FILE_FINGERPRINT,
            "fingerprint": PROFILE_FINGERPRINT,
            "path": PROFILE_PATH,
            "profile_id": PROFILE_ID,
            "profile_version": PROFILE_VERSION,
            "publish_enabled": False,
        }
        or sha256(profile_path.read_bytes()).hexdigest() != PROFILE_FILE_FINGERPRINT
        or profile_payload.get("profile_id") != PROFILE_ID
        or profile_payload.get("profile_version") != PROFILE_VERSION
        or profile_payload.get("publish_enabled") is not False
    ):
        raise ValueError

    topology = manifest["topology"]
    if not isinstance(topology, Mapping):
        raise ValueError
    _require_exact_keys(
        topology,
        {
            "binding_sha256",
            "path",
            "production_disabled_template_sha256",
            "publication_workflow_sha256",
        },
    )
    topology_raw, topology_payload = _read_canonical_manifest(root / TOPOLOGY_BINDING_FILENAME)
    expected_topology = {
        "format": "phase7-production-disabled-topology-v1",
        "production_disabled_template": {
            "path": PRODUCTION_DISABLED_TEMPLATE_PATH,
            "sha256": PRODUCTION_DISABLED_TEMPLATE_FINGERPRINT,
        },
        "publication_workflow": {
            "path": PUBLICATION_WORKFLOW_PATH,
            "sha256": PUBLICATION_WORKFLOW_FINGERPRINT,
        },
    }
    if topology_payload != expected_topology or topology != {
        "binding_sha256": sha256(topology_raw).hexdigest(),
        "path": TOPOLOGY_BINDING_FILENAME,
        "production_disabled_template_sha256": (PRODUCTION_DISABLED_TEMPLATE_FINGERPRINT),
        "publication_workflow_sha256": PUBLICATION_WORKFLOW_FINGERPRINT,
    }:
        raise ValueError

    _verify_checked_dependency_build_request(root / DEPENDENCY_BUILD_REQUEST_FILENAME)
    _verify_source_hygiene(cast(Sequence[Mapping[str, object]], files))


def _verify_checked_dependency_build_request(path: Path) -> None:
    authority = wheel_authority_from_build_request(path)
    if (
        authority.get("component") != "lambda"
        or len(cast(Sequence[object], authority.get("wheels"))) != 14
        or sha256(render_manifest(authority)).hexdigest()
        != PHASE6_LAMBDA_WHEEL_AUTHORITY_FINGERPRINT
    ):
        raise ValueError


def _verify_source_hygiene(records: Sequence[Mapping[str, object]]) -> None:
    paths: set[str] = set()
    for record in records:
        relative = record.get("path")
        if not isinstance(relative, str):
            raise ValueError
        path = PurePosixPath(relative)
        if (
            relative in paths
            or any(part in _FORBIDDEN_SOURCE_PARTS for part in path.parts)
            or path.suffix.casefold() in _FORBIDDEN_SOURCE_SUFFIXES
        ):
            raise ValueError
        paths.add(relative)
    required = {
        CONTRACT_PATH,
        PROFILE_PATH,
        DEPENDENCY_BUILD_REQUEST_FILENAME,
        "requirements.txt",
        TOPOLOGY_BINDING_FILENAME,
        "mr_lister/cloud/phase7_composition.py",
        "mr_lister/cloud/phase7_operations.py",
        "mr_lister/cloud/phase7_operations_composition.py",
        "mr_lister/cloud/phase7_provider_credentials.py",
        "mr_lister/cloud/phase7_production_entrypoints.py",
        "mr_lister/cloud/phase7_request_composition.py",
        "mr_lister/cloud/phase7_worker_composition.py",
        "mr_lister/release/phase6.py",
        "mr_lister/release/phase7_production_disabled.py",
    }
    if not required.issubset(paths):
        raise ValueError


def _verify_inventory_records(root: Path, value: object) -> None:
    if not isinstance(value, list) or not value:
        raise ValueError
    prior = ""
    for item in value:
        if not isinstance(item, Mapping):
            raise ValueError
        _require_exact_keys(item, {"path", "sha256", "size_bytes"})
        relative = item["path"]
        if not isinstance(relative, str):
            raise ValueError
        _require_safe_relative(relative)
        if relative <= prior:
            raise ValueError
        prior = relative
        expected = _nonzero_fingerprint(item["sha256"])
        size = item["size_bytes"]
        path = root / relative
        if (
            not isinstance(size, int)
            or isinstance(size, bool)
            or size < 0
            or path.is_symlink()
            or not path.is_file()
        ):
            raise ValueError
        raw = path.read_bytes()
        if len(raw) != size or sha256(raw).hexdigest() != expected:
            raise ValueError


def _read_canonical_manifest(path: Path) -> tuple[bytes, Mapping[str, object]]:
    if (
        path.is_symlink()
        or not path.is_file()
        or not 1 <= path.stat().st_size <= _MAX_MANIFEST_BYTES
    ):
        raise ValueError
    raw = path.read_bytes()
    value = json.loads(raw)
    if not isinstance(value, Mapping) or render_manifest(cast(Mapping[str, object], value)) != raw:
        raise ValueError
    return raw, cast(Mapping[str, object], value)


def _read_json_mapping(path: Path) -> Mapping[str, object]:
    if (
        path.is_symlink()
        or not path.is_file()
        or not 1 <= path.stat().st_size <= _MAX_MANIFEST_BYTES
    ):
        raise ValueError
    value = json.loads(path.read_bytes())
    if not isinstance(value, Mapping):
        raise ValueError
    return cast(Mapping[str, object], value)


def _require_safe_relative(value: str) -> None:
    path = PurePosixPath(value)
    if (
        not value
        or not value.isascii()
        or "\\" in value
        or path.is_absolute()
        or path.as_posix() != value
        or any(part in {"", ".", "..", ".git", "__pycache__"} for part in path.parts)
    ):
        raise ValueError


def _exact_directory(path: Path) -> Path:
    resolved = path.resolve(strict=True)
    if path.is_symlink() or not resolved.is_dir():
        raise ValueError
    return resolved


def _required_fingerprint(environment: Mapping[str, object], name: str) -> str:
    return _nonzero_fingerprint(environment.get(name))


def _nonzero_fingerprint(value: object) -> str:
    if not isinstance(value, str) or _FINGERPRINT.fullmatch(value) is None or value == "0" * 64:
        raise ValueError
    return value


def _require_exact_keys(value: Mapping[object, object], keys: set[str]) -> None:
    if set(value) != keys:
        raise ValueError


__all__ = [
    "ACTIVATION_MODE",
    "ACTIVATION_MODE_ENV",
    "APPLICATION_RELEASE_FINGERPRINT_ENV",
    "CONTRACT_FINGERPRINT",
    "CONTRACT_FINGERPRINT_ENV",
    "CONTRACT_PATH",
    "CONTRACT_VERSION",
    "CONTRACT_VERSION_ENV",
    "COGNITO_CLIENT_ID_ENV",
    "COGNITO_GROUP_ENV",
    "COGNITO_ISSUER_ENV",
    "COGNITO_SCOPE_ENV",
    "DEPLOYMENT_MANIFEST_FILENAME",
    "FORBIDDEN_PRODUCTION_CAPABILITY_ENVIRONMENT_NAMES",
    "PHASE6_LAMBDA_WHEEL_AUTHORITY_FINGERPRINT",
    "PRODUCTION_DISABLED_ENTRYPOINTS",
    "PRODUCTION_DISABLED_ENVIRONMENT_NAMES",
    "PRODUCTION_DISABLED_RELEASE_FINGERPRINT_ENV",
    "PRODUCTION_DISABLED_TEMPLATE_FINGERPRINT",
    "PRODUCTION_DISABLED_TEMPLATE_PATH",
    "PRODUCTION_THIRD_PARTY_IMPORT_ROOTS",
    "PROFILE_FILE_FINGERPRINT",
    "PROFILE_FINGERPRINT",
    "PROFILE_FINGERPRINT_ENV",
    "PROFILE_ID",
    "PROFILE_ID_ENV",
    "PROFILE_PATH",
    "PROFILE_PATH_ENV",
    "PROFILE_VERSION",
    "PROFILE_VERSION_ENV",
    "PRODUCTION_CANDIDATE_ENABLED_ENV",
    "PUBLICATION_ENABLED_ENV",
    "PUBLICATION_WORKFLOW_FINGERPRINT",
    "PUBLICATION_WORKFLOW_PATH",
    "QUERY_ENABLED_ENV",
    "REGION_ENV",
    "RELEASE_MANIFEST_FILENAME",
    "REQUEST_ENABLED_ENV",
    "SCAFFOLD_ONLY_ENV",
    "SOURCE_MANIFEST_FILENAME",
    "STATE_TABLE_ENV",
    "TOPOLOGY_BINDING_FILENAME",
    "Phase7ProductionDisabledReleaseAuthorityError",
    "Phase7ProductionDisabledReleaseBinding",
    "inventory",
    "render_manifest",
    "verify_phase7_production_disabled_release",
    "verify_phase7_production_disabled_source_manifest",
]
