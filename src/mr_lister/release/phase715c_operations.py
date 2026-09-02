"""Fail-closed authority for the sealed Phase 7.15C provider-free operations release.

The verifier authenticates every packaged byte and the exact contract, profile, Phase 6 table,
Phase 7 workflow, handler identity, and capability-reduced environment before any application or
AWS SDK module may be imported by an entrypoint.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path, PurePosixPath
from typing import cast

from mr_lister.release.phase6 import (
    DEPENDENCY_ARTIFACT_FILENAME,
    DEPENDENCY_BUILD_REQUEST_FILENAME,
    LINUX_ARM64_TARGET,
    Phase6ReleaseAuthorityError,
    render_manifest,
    wheel_authority_from_build_request,
)

OPERATIONS_ENTRYPOINTS = (
    "mr_lister.cloud.phase715c_operations_entrypoints.publication_recovery_handler",
    "mr_lister.cloud.phase715c_operations_entrypoints.publication_retention_handler",
)

OPERATIONS_RELEASE_FINGERPRINT_ENV = "MR_LISTER_PHASE715C_OPERATIONS_RELEASE_FINGERPRINT"
APPLICATION_RELEASE_FINGERPRINT_ENV = "MR_LISTER_RELEASE_FINGERPRINT"
CONTRACT_FINGERPRINT_ENV = "MR_LISTER_PHASE7_CONTRACT_FINGERPRINT"
CONTRACT_VERSION_ENV = "MR_LISTER_PHASE7_CONTRACT_VERSION"
OPERATIONS_MODE_ENV = "MR_LISTER_PHASE715C_OPERATIONS_MODE"
PROFILE_ID_ENV = "MR_LISTER_PRODUCT_PROFILE_ID"
PROFILE_VERSION_ENV = "MR_LISTER_PRODUCT_PROFILE_VERSION"
PROFILE_FINGERPRINT_ENV = "MR_LISTER_PRODUCT_PROFILE_FINGERPRINT"
PROFILE_PATH_ENV = "MR_LISTER_PRODUCT_PROFILE_PATH"
REGION_ENV = "AWS_REGION"
STATE_TABLE_ENV = "MR_LISTER_STATE_TABLE"
WORKFLOW_ARN_ENV = "MR_LISTER_PUBLICATION_WORKFLOW_ARN"
QUERY_ENABLED_ENV = "MR_LISTER_PHASE7_QUERY_ENABLED"
REQUEST_ENABLED_ENV = "MR_LISTER_PHASE7_REQUEST_ENABLED"
PUBLICATION_ENABLED_ENV = "MR_LISTER_PHASE7_PUBLICATION_ENABLED"
DISPATCHER_ENABLED_ENV = "MR_LISTER_PHASE7_DISPATCHER_ENABLED"
WORKER_ENABLED_ENV = "MR_LISTER_PHASE7_WORKER_ENABLED"

OPERATIONS_ENVIRONMENT_NAMES = (
    OPERATIONS_RELEASE_FINGERPRINT_ENV,
    APPLICATION_RELEASE_FINGERPRINT_ENV,
    CONTRACT_FINGERPRINT_ENV,
    CONTRACT_VERSION_ENV,
    OPERATIONS_MODE_ENV,
    PROFILE_ID_ENV,
    PROFILE_VERSION_ENV,
    PROFILE_FINGERPRINT_ENV,
    PROFILE_PATH_ENV,
    REGION_ENV,
    STATE_TABLE_ENV,
    WORKFLOW_ARN_ENV,
    QUERY_ENABLED_ENV,
    REQUEST_ENABLED_ENV,
    PUBLICATION_ENABLED_ENV,
    DISPATCHER_ENABLED_ENV,
    WORKER_ENABLED_ENV,
)
FORBIDDEN_CAPABILITY_ENVIRONMENT_NAMES = frozenset(
    {
        "MR_LISTER_ETSY_API_KEY",
        "MR_LISTER_ETSY_API_SECRET",
        "MR_LISTER_ETSY_TOKEN",
        "MR_LISTER_PRINTIFY_API_KEY",
        "MR_LISTER_PRINTIFY_SECRET_ARN",
        "MR_LISTER_PUBLICATION_RECOVERY_QUEUE_URL",
    }
)

CONTRACT_PATH = "contracts/publication/phase7.0.1.json"
CONTRACT_VERSION = "7.0.1"
CONTRACT_FINGERPRINT = "548b710230618e73c20a509f2121799c415b50070e1e2ae7e1b82fe3c37e2981"
OPERATIONS_MODE = "PROVIDER_FREE_OPERATIONS"
PROFILE_PATH = "config/product_profiles/gildan_64000_swiftpod.json"
PROFILE_ID = "gildan_64000_swiftpod"
PROFILE_VERSION = 2
PROFILE_FINGERPRINT = "5de1257141cfdacb1731f68bb9113712957483b33d5b0f7115afdba86eb7476c"
PROFILE_FILE_FINGERPRINT = "eb9b7769e0049f7b270da70caa06fb321d56e1a4c37a280bb6265ba3544aae40"
CURRENT_REGION = "us-west-2"
CURRENT_STATE_TABLE = "mr-lister-phase6-dev"
CURRENT_PUBLICATION_WORKFLOW_ARN = (
    "arn:aws:states:us-west-2:384627057108:stateMachine:mr-lister-phase7-dev-publication"
)
PUBLICATION_WORKFLOW_PATH = "infra/phase7/statemachine/publication.asl.json"
PUBLICATION_WORKFLOW_FINGERPRINT = (
    "9a6112c85b35e775d1e60681a0ca14e6740cd0aea82b2ac33b5aa74b86fc3098"
)
OPERATIONS_BINDING_FILENAME = "phase715c-operations-binding.json"
OPERATIONS_THIRD_PARTY_IMPORT_ROOTS = ("PIL", "boto3", "botocore", "pydantic")
PHASE6_LAMBDA_WHEEL_AUTHORITY_FINGERPRINT = (
    "145ae4affca308e4268120e5f5f725d1e91ba3194db76585e53cba32d29eefbd"
)

SOURCE_MANIFEST_FILENAME = "phase715c-operations-source-manifest.json"
DEPLOYMENT_MANIFEST_FILENAME = "phase715c-operations-deployment-manifest.json"
RELEASE_MANIFEST_FILENAME = "phase715c-operations-release-manifest.json"

_COMPONENT = "phase715c-operations-lambda"
_SOURCE_FORMAT = "phase715c-operations-source-v1"
_DEPLOYMENT_FORMAT = "phase715c-operations-deployment-v1"
_RELEASE_FORMAT = "phase715c-operations-release-v1"
_BINDING_FORMAT = "phase715c-operations-binding-v1"
_FINGERPRINT = re.compile(r"^[a-f0-9]{64}$")
_MAX_MANIFEST_BYTES = 4 * 1024 * 1024
_GENERIC_ERROR = "Phase 7.15C operations release authority is invalid"
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
_FORBIDDEN_MODULE_PATHS = frozenset(
    {
        "mr_lister/cloud/phase7_canary_composition.py",
        "mr_lister/cloud/phase7_canary_entrypoint.py",
        "mr_lister/cloud/phase7_composition.py",
        "mr_lister/cloud/phase7_entrypoints.py",
        "mr_lister/cloud/phase7_guard_composition.py",
        "mr_lister/cloud/phase7_guard_entrypoint.py",
        "mr_lister/cloud/phase7_operations.py",
        "mr_lister/cloud/phase7_operations_composition.py",
        "mr_lister/cloud/phase7_production_entrypoints.py",
        "mr_lister/cloud/phase7_provider_credentials.py",
        "mr_lister/cloud/phase7_request_composition.py",
        "mr_lister/cloud/phase7_worker_composition.py",
        "mr_lister/publication/provider_boundary.py",
    }
)


class Phase715cOperationsReleaseAuthorityError(RuntimeError):
    """Value-free refusal for malformed, drifting, or unsealed operations bytes."""


@dataclass(frozen=True, slots=True)
class Phase715cOperationsReleaseBinding:
    """Exact identities authenticated before an operations graph is constructed."""

    component: str
    entrypoint: str
    release_fingerprint: str
    application_release_fingerprint: str
    source_manifest_fingerprint: str
    dependency_manifest_fingerprint: str
    deployment_manifest_fingerprint: str
    operations_binding_fingerprint: str
    contract_fingerprint: str
    profile_fingerprint: str
    profile_path: str
    state_table: str
    publication_workflow_arn: str
    publication_workflow_fingerprint: str


def verify_phase715c_operations_release(
    environment: Mapping[str, object],
    *,
    expected_entrypoint: str,
    bundle_root: Path | None = None,
) -> Phase715cOperationsReleaseBinding:
    """Authenticate one exact handler, its environment, and every packaged byte."""

    try:
        if not isinstance(environment, Mapping):
            raise ValueError
        _verify_environment(environment)
        if expected_entrypoint not in OPERATIONS_ENTRYPOINTS:
            raise ValueError
        expected_release = _required_fingerprint(
            environment,
            OPERATIONS_RELEASE_FINGERPRINT_ENV,
        )
        expected_application = _required_fingerprint(
            environment,
            APPLICATION_RELEASE_FINGERPRINT_ENV,
        )
        root = _exact_directory(bundle_root or Path(__file__).resolve().parents[2])
        profile_path = (root / PROFILE_PATH).as_posix()
        if environment.get(PROFILE_PATH_ENV) != profile_path:
            raise ValueError

        release_bytes, release = _read_canonical_manifest(root / RELEASE_MANIFEST_FILENAME)
        release_fingerprint = sha256(release_bytes).hexdigest()
        if release_fingerprint != expected_release:
            raise ValueError
        _require_exact_keys(
            release,
            {
                "algorithm",
                "application_release_fingerprint",
                "component",
                "contract_fingerprint",
                "dependency_manifest_sha256",
                "deployment_manifest_sha256",
                "entrypoints",
                "format",
                "operations_binding_sha256",
                "profile_fingerprint",
                "publication_workflow_arn",
                "publication_workflow_sha256",
                "source_manifest_sha256",
                "state_table",
                "target",
            },
        )
        if (
            release["algorithm"] != "sha256"
            or release["application_release_fingerprint"] != expected_application
            or release["component"] != _COMPONENT
            or release["contract_fingerprint"] != CONTRACT_FINGERPRINT
            or release["entrypoints"] != list(OPERATIONS_ENTRYPOINTS)
            or release["format"] != _RELEASE_FORMAT
            or release["profile_fingerprint"] != PROFILE_FINGERPRINT
            or release["publication_workflow_arn"] != CURRENT_PUBLICATION_WORKFLOW_ARN
            or release["publication_workflow_sha256"] != PUBLICATION_WORKFLOW_FINGERPRINT
            or release["state_table"] != CURRENT_STATE_TABLE
            or release["target"] != LINUX_ARM64_TARGET
        ):
            raise ValueError

        source_fingerprint = _nonzero_fingerprint(release["source_manifest_sha256"])
        dependency_fingerprint = _nonzero_fingerprint(release["dependency_manifest_sha256"])
        deployment_fingerprint = _nonzero_fingerprint(release["deployment_manifest_sha256"])
        binding_fingerprint = _nonzero_fingerprint(release["operations_binding_sha256"])
        deployment_bytes, deployment = _read_canonical_manifest(root / DEPLOYMENT_MANIFEST_FILENAME)
        source_bytes, source = _read_canonical_manifest(root / SOURCE_MANIFEST_FILENAME)
        dependency_bytes, dependency = _read_canonical_manifest(root / DEPENDENCY_ARTIFACT_FILENAME)
        if (
            sha256(deployment_bytes).hexdigest() != deployment_fingerprint
            or sha256(source_bytes).hexdigest() != source_fingerprint
            or sha256(dependency_bytes).hexdigest() != dependency_fingerprint
            or _file_fingerprint(root / OPERATIONS_BINDING_FILENAME) != binding_fingerprint
        ):
            raise ValueError
        _verify_deployment_manifest(root, deployment)
        _verify_source_manifest(root, source, allow_dependency_files=True)
        _verify_packaged_dependency(
            root,
            dependency=dependency,
            deployment=deployment,
            source=source,
        )
        _verify_checked_dependency_build_request(root / DEPENDENCY_BUILD_REQUEST_FILENAME)
        return Phase715cOperationsReleaseBinding(
            component=_COMPONENT,
            entrypoint=expected_entrypoint,
            release_fingerprint=release_fingerprint,
            application_release_fingerprint=expected_application,
            source_manifest_fingerprint=source_fingerprint,
            dependency_manifest_fingerprint=dependency_fingerprint,
            deployment_manifest_fingerprint=deployment_fingerprint,
            operations_binding_fingerprint=binding_fingerprint,
            contract_fingerprint=CONTRACT_FINGERPRINT,
            profile_fingerprint=PROFILE_FINGERPRINT,
            profile_path=profile_path,
            state_table=CURRENT_STATE_TABLE,
            publication_workflow_arn=CURRENT_PUBLICATION_WORKFLOW_ARN,
            publication_workflow_fingerprint=PUBLICATION_WORKFLOW_FINGERPRINT,
        )
    except (Phase6ReleaseAuthorityError, Phase715cOperationsReleaseAuthorityError):
        raise Phase715cOperationsReleaseAuthorityError(_GENERIC_ERROR) from None
    except Exception:
        raise Phase715cOperationsReleaseAuthorityError(_GENERIC_ERROR) from None


def verify_phase715c_operations_source_manifest(root: Path) -> None:
    """Verify the deterministic operations source stage before dependency overlay."""

    try:
        source_root = _exact_directory(root)
        _raw, source = _read_canonical_manifest(source_root / SOURCE_MANIFEST_FILENAME)
        _verify_source_manifest(source_root, source, allow_dependency_files=False)
    except Phase715cOperationsReleaseAuthorityError:
        raise
    except Exception:
        raise Phase715cOperationsReleaseAuthorityError(_GENERIC_ERROR) from None


def inventory(root: Path, *, excluded: frozenset[str]) -> list[dict[str, object]]:
    """Return a sorted path-safe SHA-256 inventory without following links."""

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
        files.append({"path": relative, "sha256": sha256(raw).hexdigest(), "size_bytes": len(raw)})
    return files


def _verify_environment(environment: Mapping[str, object]) -> None:
    if set(environment) != set(OPERATIONS_ENVIRONMENT_NAMES):
        raise ValueError
    if (
        environment.get(CONTRACT_FINGERPRINT_ENV) != CONTRACT_FINGERPRINT
        or environment.get(CONTRACT_VERSION_ENV) != CONTRACT_VERSION
        or environment.get(OPERATIONS_MODE_ENV) != OPERATIONS_MODE
        or environment.get(PROFILE_ID_ENV) != PROFILE_ID
        or environment.get(PROFILE_VERSION_ENV) != str(PROFILE_VERSION)
        or environment.get(PROFILE_FINGERPRINT_ENV) != PROFILE_FINGERPRINT
        or environment.get(REGION_ENV) != CURRENT_REGION
        or environment.get(STATE_TABLE_ENV) != CURRENT_STATE_TABLE
        or environment.get(WORKFLOW_ARN_ENV) != CURRENT_PUBLICATION_WORKFLOW_ARN
        or environment.get(QUERY_ENABLED_ENV) != "false"
        or environment.get(REQUEST_ENABLED_ENV) != "false"
        or environment.get(PUBLICATION_ENABLED_ENV) != "false"
        or environment.get(DISPATCHER_ENABLED_ENV) != "false"
        or environment.get(WORKER_ENABLED_ENV) != "false"
        or any(name in environment for name in FORBIDDEN_CAPABILITY_ENVIRONMENT_NAMES)
    ):
        raise ValueError


def _verify_deployment_manifest(root: Path, manifest: Mapping[str, object]) -> None:
    _require_exact_keys(
        manifest,
        {"algorithm", "component", "entrypoints", "files", "format", "target"},
    )
    if (
        manifest["algorithm"] != "sha256"
        or manifest["component"] != _COMPONENT
        or manifest["entrypoints"] != list(OPERATIONS_ENTRYPOINTS)
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
            "operations_binding",
            "profile",
            "target",
            "third_party_import_roots",
        },
    )
    if (
        manifest["algorithm"] != "sha256"
        or manifest["entrypoints"] != list(OPERATIONS_ENTRYPOINTS)
        or manifest["format"] != _SOURCE_FORMAT
        or manifest["target"] != LINUX_ARM64_TARGET
        or manifest["third_party_import_roots"] != list(OPERATIONS_THIRD_PARTY_IMPORT_ROOTS)
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
    expected_contract = {
        "contract_version": CONTRACT_VERSION,
        "current_activation_phase": "offline_implementation",
        "path": CONTRACT_PATH,
        "publication_enabled": False,
        "sha256": CONTRACT_FINGERPRINT,
        "status": "frozen",
    }
    contract_raw, contract_payload = _read_canonical_manifest(root / CONTRACT_PATH)
    if (
        contract != expected_contract
        or sha256(contract_raw).hexdigest() != CONTRACT_FINGERPRINT
        or contract_payload.get("phase") != "7"
        or contract_payload.get("contract_version") != CONTRACT_VERSION
        or contract_payload.get("current_activation_phase") != "offline_implementation"
        or contract_payload.get("publication_enabled") is not False
        or contract_payload.get("status") != "frozen"
    ):
        raise ValueError

    profile = manifest["profile"]
    expected_profile = {
        "file_sha256": PROFILE_FILE_FINGERPRINT,
        "fingerprint": PROFILE_FINGERPRINT,
        "path": PROFILE_PATH,
        "profile_id": PROFILE_ID,
        "profile_version": PROFILE_VERSION,
        "publish_enabled": False,
    }
    profile_path = root / PROFILE_PATH
    profile_payload = _read_json_mapping(profile_path)
    if (
        profile != expected_profile
        or _file_fingerprint(profile_path) != PROFILE_FILE_FINGERPRINT
        or profile_payload.get("profile_id") != PROFILE_ID
        or profile_payload.get("profile_version") != PROFILE_VERSION
        or profile_payload.get("publish_enabled") is not False
    ):
        raise ValueError

    binding_record = manifest["operations_binding"]
    binding_raw, binding = _read_canonical_manifest(root / OPERATIONS_BINDING_FILENAME)
    expected_binding = operations_binding_document()
    if binding != expected_binding or binding_record != {
        "path": OPERATIONS_BINDING_FILENAME,
        "sha256": sha256(binding_raw).hexdigest(),
        "state_table": CURRENT_STATE_TABLE,
        "publication_workflow_arn": CURRENT_PUBLICATION_WORKFLOW_ARN,
    }:
        raise ValueError
    _verify_checked_dependency_build_request(root / DEPENDENCY_BUILD_REQUEST_FILENAME)
    _verify_source_hygiene(cast(Sequence[Mapping[str, object]], files))


def operations_binding_document() -> dict[str, object]:
    """Return the canonical provider-free runtime binding included in the sealed release."""

    return {
        "algorithm": "sha256",
        "aws_client_allowlist": {
            "dynamodb": ["GetItem", "Query", "TransactWriteItems"],
            "stepfunctions": ["DescribeExecution", "RedriveExecution"],
        },
        "contract": {
            "contract_version": CONTRACT_VERSION,
            "current_activation_phase": "offline_implementation",
            "path": CONTRACT_PATH,
            "publication_enabled": False,
            "sha256": CONTRACT_FINGERPRINT,
            "status": "frozen",
        },
        "entrypoints": list(OPERATIONS_ENTRYPOINTS),
        "format": _BINDING_FORMAT,
        "profile": {
            "file_sha256": PROFILE_FILE_FINGERPRINT,
            "fingerprint": PROFILE_FINGERPRINT,
            "path": PROFILE_PATH,
            "profile_id": PROFILE_ID,
            "profile_version": PROFILE_VERSION,
            "publish_enabled": False,
        },
        "publication_workflow": {
            "arn": CURRENT_PUBLICATION_WORKFLOW_ARN,
            "definition_path": PUBLICATION_WORKFLOW_PATH,
            "definition_sha256": PUBLICATION_WORKFLOW_FINGERPRINT,
        },
        "region": CURRENT_REGION,
        "state_table": CURRENT_STATE_TABLE,
    }


def _verify_checked_dependency_build_request(path: Path) -> None:
    authority = wheel_authority_from_build_request(path)
    if (
        authority.get("component") != "lambda"
        or len(cast(Sequence[object], authority.get("wheels"))) != 14
        or sha256(render_manifest(authority)).hexdigest()
        != PHASE6_LAMBDA_WHEEL_AUTHORITY_FINGERPRINT
    ):
        raise ValueError


def _verify_packaged_dependency(
    root: Path,
    *,
    dependency: Mapping[str, object],
    deployment: Mapping[str, object],
    source: Mapping[str, object],
) -> None:
    """Authenticate the shared dependency artifact under operations-specific manifests."""

    _require_exact_keys(
        dependency,
        {
            "algorithm",
            "build_request_sha256",
            "dependency_tree_sha256",
            "distributions",
            "files",
            "format",
            "target",
            "wheel_artifacts",
        },
    )
    authority = wheel_authority_from_build_request(root / DEPENDENCY_BUILD_REQUEST_FILENAME)
    dependency_files = dependency["files"]
    distributions = dependency["distributions"]
    wheels = authority.get("wheels")
    if (
        dependency["algorithm"] != "sha256"
        or dependency["build_request_sha256"]
        != _file_fingerprint(root / DEPENDENCY_BUILD_REQUEST_FILENAME)
        or dependency["dependency_tree_sha256"] != authority.get("dependency_tree_sha256")
        or dependency["dependency_tree_sha256"]
        != sha256(
            render_manifest(
                {
                    "files": [
                        dict(record) for record in dependency_files if isinstance(record, Mapping)
                    ]
                }
            )
        ).hexdigest()
        or dependency["format"] != "phase6-linux-arm64-dependencies-v2"
        or dependency["target"] != LINUX_ARM64_TARGET
        or dependency["wheel_artifacts"] != wheels
        or not isinstance(dependency_files, list)
        or not isinstance(distributions, list)
        or not isinstance(wheels, list)
        or len(distributions) != len(wheels)
    ):
        raise ValueError
    _verify_inventory_records(root, dependency_files)

    expected_distributions = {
        (wheel.get("name"), wheel.get("version")) for wheel in wheels if isinstance(wheel, Mapping)
    }
    observed_distributions: set[tuple[object, object]] = set()
    for distribution in distributions:
        if not isinstance(distribution, Mapping):
            raise ValueError
        _require_exact_keys(distribution, {"dist_info", "name", "tags", "version"})
        dist_info = distribution["dist_info"]
        name = distribution["name"]
        tags = distribution["tags"]
        version = distribution["version"]
        if (
            not isinstance(dist_info, str)
            or PurePosixPath(dist_info).name != dist_info
            or not dist_info.endswith(".dist-info")
            or not isinstance(name, str)
            or not isinstance(version, str)
            or not isinstance(tags, list)
            or not tags
            or any(not isinstance(tag, str) or not tag for tag in tags)
        ):
            raise ValueError
        observed_distributions.add((name, version))
    if observed_distributions != expected_distributions:
        raise ValueError

    source_files = source.get("files")
    deployment_files = deployment.get("files")
    if not isinstance(source_files, list) or not isinstance(deployment_files, list):
        raise ValueError
    source_paths = _inventory_paths(source_files)
    dependency_paths = _inventory_paths(dependency_files)
    deployment_paths = _inventory_paths(deployment_files)
    if source_paths & dependency_paths or deployment_paths != (
        source_paths | dependency_paths | {SOURCE_MANIFEST_FILENAME, DEPENDENCY_ARTIFACT_FILENAME}
    ):
        raise ValueError


def _inventory_paths(records: Sequence[object]) -> set[str]:
    paths: set[str] = set()
    for record in records:
        if not isinstance(record, Mapping):
            raise ValueError
        path = record.get("path")
        if not isinstance(path, str) or path in paths:
            raise ValueError
        paths.add(path)
    return paths


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
        OPERATIONS_BINDING_FILENAME,
        "mr_lister/cloud/phase715c_operations_composition.py",
        "mr_lister/cloud/phase715c_operations_entrypoints.py",
        "mr_lister/cloud/phase715c_operations_handlers.py",
        "mr_lister/release/phase6.py",
        "mr_lister/release/phase715c_operations.py",
    }
    if not required.issubset(paths) or paths & _FORBIDDEN_MODULE_PATHS:
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


def _file_fingerprint(path: Path) -> str:
    if path.is_symlink() or not path.is_file():
        raise ValueError
    return sha256(path.read_bytes()).hexdigest()


def _require_exact_keys(value: Mapping[object, object], keys: set[str]) -> None:
    if set(value) != keys:
        raise ValueError


__all__ = [
    "APPLICATION_RELEASE_FINGERPRINT_ENV",
    "CONTRACT_FINGERPRINT",
    "CONTRACT_FINGERPRINT_ENV",
    "CONTRACT_PATH",
    "CONTRACT_VERSION",
    "CONTRACT_VERSION_ENV",
    "CURRENT_PUBLICATION_WORKFLOW_ARN",
    "CURRENT_REGION",
    "CURRENT_STATE_TABLE",
    "DEPLOYMENT_MANIFEST_FILENAME",
    "DISPATCHER_ENABLED_ENV",
    "FORBIDDEN_CAPABILITY_ENVIRONMENT_NAMES",
    "OPERATIONS_BINDING_FILENAME",
    "OPERATIONS_ENTRYPOINTS",
    "OPERATIONS_ENVIRONMENT_NAMES",
    "OPERATIONS_MODE",
    "OPERATIONS_MODE_ENV",
    "OPERATIONS_RELEASE_FINGERPRINT_ENV",
    "OPERATIONS_THIRD_PARTY_IMPORT_ROOTS",
    "PHASE6_LAMBDA_WHEEL_AUTHORITY_FINGERPRINT",
    "PROFILE_FILE_FINGERPRINT",
    "PROFILE_FINGERPRINT",
    "PROFILE_FINGERPRINT_ENV",
    "PROFILE_ID",
    "PROFILE_ID_ENV",
    "PROFILE_PATH",
    "PROFILE_PATH_ENV",
    "PROFILE_VERSION",
    "PROFILE_VERSION_ENV",
    "PUBLICATION_ENABLED_ENV",
    "PUBLICATION_WORKFLOW_FINGERPRINT",
    "PUBLICATION_WORKFLOW_PATH",
    "QUERY_ENABLED_ENV",
    "REGION_ENV",
    "RELEASE_MANIFEST_FILENAME",
    "REQUEST_ENABLED_ENV",
    "SOURCE_MANIFEST_FILENAME",
    "STATE_TABLE_ENV",
    "WORKER_ENABLED_ENV",
    "WORKFLOW_ARN_ENV",
    "Phase715cOperationsReleaseAuthorityError",
    "Phase715cOperationsReleaseBinding",
    "inventory",
    "operations_binding_document",
    "render_manifest",
    "verify_phase715c_operations_release",
    "verify_phase715c_operations_source_manifest",
]
