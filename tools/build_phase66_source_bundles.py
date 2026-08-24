"""Build narrow, deterministic Phase 6 Lambda and AgentCore source bundles.

This step intentionally does not resolve platform wheels.  It creates auditable source inputs
for the later Linux ARM64 dependency build, writes content manifests, and excludes legacy API,
publication, tests, private evidence, caches, and developer files.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import re
import shutil
import sys
import zipfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from email.parser import Parser
from hashlib import sha256
from io import BytesIO
from pathlib import Path, PurePosixPath
from tempfile import TemporaryDirectory
from typing import Literal, cast

from packaging.markers import default_environment
from packaging.requirements import Requirement
from packaging.specifiers import SpecifierSet
from packaging.utils import canonicalize_name, parse_wheel_filename
from packaging.version import Version

from mr_lister.release.phase6 import (
    DEPENDENCY_ARTIFACT_FILENAME,
    DEPENDENCY_BUILD_REQUEST_FILENAME,
    DEPLOYMENT_MANIFEST_FILENAME,
    LEGACY_BUILD_REQUEST_FORMAT,
    LINUX_ARM64_TARGET,
    LOCKED_BUILD_REQUEST_FORMAT,
    RELEASE_MANIFEST_FILENAME,
    Phase6ReleaseAuthorityError,
    inspect_linux_arm64_dependency_artifact,
    normalize_wheel_authority,
    render_locked_requirements,
    render_manifest,
    verify_dependency_build_request,
    verify_linux_arm64_dependency_artifact,
    verify_phase6_packaged_release,
    wheel_authority_from_build_request,
)

ROOT = Path(__file__).resolve().parents[1]
CHECKED_AUTHORITY_DIRECTORY = ROOT / "config" / "release" / "phase6"
CHECKED_LAMBDA_WHEEL_AUTHORITY = CHECKED_AUTHORITY_DIRECTORY / "phase6-lambda-wheel-authority.json"
CHECKED_AGENTCORE_WHEEL_AUTHORITY = (
    CHECKED_AUTHORITY_DIRECTORY / "phase6-agentcore-wheel-authority.json"
)
DEFAULT_DESTINATION = ROOT / ".mr_lister_private" / "phase6-release"
DEFAULT_DEPLOYMENT_DESTINATION = ROOT / ".mr_lister_private" / "phase6-deployment"
DEFAULT_ARTIFACT_DESTINATION = ROOT / ".mr_lister_private" / "phase6-artifacts"

SOURCE_DIRECTORY_NAME = "phase6-release"
DEPLOYMENT_DIRECTORY_NAME = "phase6-deployment"
ARTIFACT_DIRECTORY_NAME = "phase6-artifacts"
LAMBDA_DEPENDENCY_DIRECTORY_NAME = "phase6-lambda-dependencies"
AGENTCORE_DEPENDENCY_DIRECTORY_NAME = "phase6-agentcore-dependencies"
LAMBDA_ARCHIVE_FILENAME = "phase6-lambda.zip"
AGENTCORE_ARCHIVE_FILENAME = "phase6-agentcore.zip"
DEPLOYMENT_DESCRIPTOR_FILENAME = "deployment-descriptor.json"
LAMBDA_WHEEL_AUTHORITY_FILENAME = "phase6-lambda-wheel-authority.json"
AGENTCORE_WHEEL_AUTHORITY_FILENAME = "phase6-agentcore-wheel-authority.json"

_DESCRIPTOR_FORMAT = "phase6-deployment-artifacts-v1"
_ZIP_TIMESTAMP = (1980, 1, 1, 0, 0, 0)
_MAX_WHEEL_MEMBER_SIZE = 128 * 1024 * 1024
_MAX_EXTRACTED_TREE_SIZE = 768 * 1024 * 1024
_NORMALIZED_DISTRIBUTION = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_VERSION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.!+_-]{0,127}$")

_COMMON_ROOT_FILES = ("__init__.py", "review_profile.py", "review_security.py")
_CAPABILITY_FREE_INIT_BYTES = b""
_LAMBDA_AGENT_FILES = (
    "__init__.py",
    "contracts.py",
    "observability.py",
    "phase6_contracts.py",
    "runtime_binding.py",
)
_LAMBDA_CLOUD_FILES = (
    "__init__.py",
    "api.py",
    "artifacts.py",
    "auth.py",
    "browser_contracts.py",
    "http.py",
    "phase6_composition.py",
    "phase6_entrypoints.py",
    "phase6_execution_recovery_composition.py",
    "phase6_execution_recovery_entrypoint.py",
    "phase6_machine.py",
    "phase6_machine_composition.py",
    "phase6_operational_cleanup_composition.py",
    "phase6_operational_cleanup_entrypoint.py",
    "preview.py",
    "phase6_retention_composition.py",
    "phase6_retention_entrypoint.py",
)
_LAMBDA_PRODUCTION_FILES = (
    "__init__.py",
    "draft_sync.py",
    "economics.py",
    "operational_cleanup.py",
    "operational_cleanup_aws.py",
    "phase6_worker.py",
    "printify.py",
    "printify_shipping.py",
    "provider_resources.py",
    "provider_secrets.py",
    "retention.py",
    "retention_aws.py",
    "settings.py",
)
_WORKFLOW_FILES = (
    "__init__.py",
    "errors.py",
    "models.py",
    "ports.py",
    "secrets.py",
    "validation.py",
)
_AGENTCORE_AGENT_FILES = (
    "__init__.py",
    "contracts.py",
    "observability.py",
    "phase6.py",
    "phase6_composition.py",
    "phase6_contracts.py",
    "phase6_producer.py",
    "runtime_contracts.py",
)

_COMPONENT_ROOT_REQUIREMENTS = {
    "lambda": (
        "boto3>=1.43,<2",
        "botocore[crt]>=1.43,<2",
        "pillow>=11.3,<13",
        "pydantic>=2.10,<3",
    ),
    "agentcore": (
        "bedrock-agentcore>=1.22,<2",
        "boto3>=1.43,<2",
        "botocore[crt]>=1.43,<2",
        "fastapi>=0.116,<1",
        "pillow>=11.3,<13",
        "pydantic>=2.10,<3",
        "strands-agents>=1.52,<2",
        "uvicorn>=0.35,<1",
    ),
}
LAMBDA_REQUIREMENTS = "".join(
    f"{requirement}\n" for requirement in _COMPONENT_ROOT_REQUIREMENTS["lambda"]
)
AGENTCORE_REQUIREMENTS = "".join(
    f"{requirement}\n" for requirement in _COMPONENT_ROOT_REQUIREMENTS["agentcore"]
)

_REQUIRED_DISTRIBUTIONS = {
    "lambda": ["awscrt", "boto3", "botocore", "pillow", "pydantic"],
    "agentcore": [
        "awscrt",
        "bedrock-agentcore",
        "boto3",
        "botocore",
        "fastapi",
        "pillow",
        "pydantic",
        "strands-agents",
        "uvicorn",
    ],
}


@dataclass(frozen=True, slots=True)
class Phase66DeploymentArtifacts:
    artifact_root: Path
    descriptor_path: Path
    lambda_archive_path: Path
    agentcore_archive_path: Path
    release_fingerprint: str


@dataclass(frozen=True, slots=True)
class _WheelMetadata:
    name: str
    version: str
    requires_python: str | None
    requires_dist: tuple[Requirement, ...]
    provides_extra: frozenset[str]


def _normalize_wheel_authorities(
    value: Mapping[str, Mapping[str, object]] | None,
) -> dict[str, dict[str, object]]:
    if value is None:
        return {}
    if not isinstance(value, Mapping) or set(value) != {"agentcore", "lambda"}:
        raise ValueError("Phase 6 locked source requires both component wheel authorities")
    return {
        component: normalize_wheel_authority(
            value[component],
            component=cast(Literal["agentcore", "lambda"], component),
        )
        for component in ("agentcore", "lambda")
    }


def _load_checked_wheel_authorities() -> dict[str, dict[str, object]]:
    """Load the only repository authority allowed to cross the deployment seal."""

    authorities = _normalize_wheel_authorities(
        {
            "agentcore": _read_wheel_authority(CHECKED_AGENTCORE_WHEEL_AUTHORITY),
            "lambda": _read_wheel_authority(CHECKED_LAMBDA_WHEEL_AUTHORITY),
        }
    )
    _verify_checked_root_requirement_versions(authorities)
    return authorities


def _verify_checked_root_requirement_versions(
    authorities: Mapping[str, Mapping[str, object]],
) -> None:
    """Bind every current direct-root constraint to its exact checked wheel version."""

    if set(authorities) != {"agentcore", "lambda"}:
        raise ValueError("Phase 6 checked wheel authority is incomplete")
    for component in ("agentcore", "lambda"):
        wheels = authorities[component].get("wheels")
        if not isinstance(wheels, list):
            raise ValueError("Phase 6 checked wheel authority is invalid")
        versions = {
            cast(str, record["name"]): cast(str, record["version"])
            for record in wheels
            if isinstance(record, Mapping)
            and isinstance(record.get("name"), str)
            and isinstance(record.get("version"), str)
        }
        if len(versions) != len(wheels):
            raise ValueError("Phase 6 checked wheel authority is invalid")
        root_names: set[str] = set()
        for value in _COMPONENT_ROOT_REQUIREMENTS[component]:
            requirement = Requirement(value)
            name, _extras = _normalize_requirement(requirement)
            version = versions.get(name)
            if (
                requirement.url is not None
                or requirement.marker is not None
                or not str(requirement.specifier)
                or name in root_names
                or version is None
                or not requirement.specifier.contains(Version(version), prereleases=True)
            ):
                raise ValueError("Phase 6 checked wheel authority violates root requirements")
            root_names.add(name)


def build_source_bundles(
    destination: Path,
    *,
    wheel_authorities: Mapping[str, Mapping[str, object]] | None = None,
    legacy_source_only: bool = False,
) -> tuple[Path, Path]:
    if legacy_source_only and wheel_authorities is not None:
        raise ValueError("Phase 6 legacy source cannot carry wheel authority")
    authorities = (
        {}
        if legacy_source_only
        else _normalize_wheel_authorities(
            wheel_authorities
            if wheel_authorities is not None
            else _load_checked_wheel_authorities()
        )
    )
    destination = destination.resolve(strict=False)
    if destination.name != SOURCE_DIRECTORY_NAME or destination.exists():
        raise ValueError("Phase 6 source destination must be a new phase6-release directory")
    destination.mkdir(mode=0o700, parents=True)
    lambda_root = destination / "lambda"
    agentcore_root = destination / "agentcore"
    lambda_root.mkdir(mode=0o700)
    agentcore_root.mkdir(mode=0o700)

    _copy_file(ROOT / "infra/phase6/lambda/phase6_lambda.py", lambda_root / "phase6_lambda.py")
    _copy_common(lambda_root)
    _copy_directory(ROOT / "src/mr_lister/contracts", lambda_root / "mr_lister/contracts")
    _copy_directory(ROOT / "src/mr_lister/control", lambda_root / "mr_lister/control")
    _copy_selected(
        ROOT / "src/mr_lister/agent",
        lambda_root / "mr_lister/agent",
        _LAMBDA_AGENT_FILES,
    )
    _copy_selected(
        ROOT / "src/mr_lister/cloud",
        lambda_root / "mr_lister/cloud",
        _LAMBDA_CLOUD_FILES,
    )
    _copy_selected(
        ROOT / "src/mr_lister/production",
        lambda_root / "mr_lister/production",
        _LAMBDA_PRODUCTION_FILES,
    )
    _copy_selected(
        ROOT / "src/mr_lister/workflow",
        lambda_root / "mr_lister/workflow",
        _WORKFLOW_FILES,
    )
    _copy_file(
        ROOT / "config/product_profiles/gildan_64000_swiftpod.json",
        lambda_root / "config/product_profiles/gildan_64000_swiftpod.json",
    )
    _write_text(
        lambda_root / "requirements.txt",
        render_locked_requirements(authorities["lambda"]) if authorities else LAMBDA_REQUIREMENTS,
    )
    _copy_release_authority(lambda_root)
    _write_dependency_build_request(
        lambda_root,
        component="lambda",
        authority=authorities.get("lambda"),
    )

    _copy_file(ROOT / "agentcore_phase6_runtime.py", agentcore_root / "main.py")
    _copy_common(agentcore_root)
    _copy_directory(ROOT / "src/mr_lister/contracts", agentcore_root / "mr_lister/contracts")
    _copy_directory(ROOT / "src/mr_lister/control", agentcore_root / "mr_lister/control")
    _copy_directory(
        ROOT / "src/mr_lister/intelligence",
        agentcore_root / "mr_lister/intelligence",
    )
    _copy_selected(
        ROOT / "src/mr_lister/agent",
        agentcore_root / "mr_lister/agent",
        _AGENTCORE_AGENT_FILES,
    )
    _copy_selected(
        ROOT / "src/mr_lister/workflow",
        agentcore_root / "mr_lister/workflow",
        _WORKFLOW_FILES,
    )
    _copy_file(
        ROOT / "config/product_profiles/gildan_64000_swiftpod.json",
        agentcore_root / "config/product_profiles/gildan_64000_swiftpod.json",
    )
    _copy_file(
        ROOT / "config/bedrock/google_gemma_3_27b_it.json",
        agentcore_root / "config/bedrock/google_gemma_3_27b_it.json",
    )
    _write_text(
        agentcore_root / "requirements.txt",
        render_locked_requirements(authorities["agentcore"])
        if authorities
        else AGENTCORE_REQUIREMENTS,
    )
    _copy_release_authority(agentcore_root)
    _write_dependency_build_request(
        agentcore_root,
        component="agentcore",
        authority=authorities.get("agentcore"),
    )

    for root in (lambda_root, agentcore_root):
        _assert_hygiene(root)
        _write_manifest(root)
        verify_source_bundle(root)
    return lambda_root, agentcore_root


def verify_source_bundle(root: Path) -> None:
    root = root.resolve(strict=True)
    manifest_path = root / "source-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if set(manifest) != {"algorithm", "files", "format"}:
        raise ValueError("Phase 6 source manifest is malformed")
    if manifest["algorithm"] != "sha256" or manifest["format"] != "phase6-source-v1":
        raise ValueError("Phase 6 source manifest authority is unsupported")
    expected = _manifest_files(root)
    if manifest["files"] != expected:
        raise ValueError("Phase 6 source manifest does not match bundle bytes")
    try:
        verify_dependency_build_request(root / DEPENDENCY_BUILD_REQUEST_FILENAME)
    except Exception:
        raise ValueError("Phase 6 dependency build request is invalid") from None
    _assert_hygiene(root)


def write_linux_arm64_dependency_manifest(
    artifact_root: Path,
    *,
    build_request_path: Path,
) -> Path:
    """Inspect and seal dependency bytes produced by a controlled Linux ARM64 build."""

    artifact_root = artifact_root.resolve(strict=True)
    manifest_path = artifact_root / DEPENDENCY_ARTIFACT_FILENAME
    if manifest_path.exists():
        raise ValueError("Phase 6 dependency artifact manifest already exists")
    manifest = inspect_linux_arm64_dependency_artifact(
        artifact_root,
        build_request_path=build_request_path,
    )
    _write_bytes(manifest_path, render_manifest(manifest))
    verify_linux_arm64_dependency_artifact(
        artifact_root,
        build_request_path=build_request_path,
    )
    return manifest_path


def capture_wheelhouse_authority_candidate(
    wheelhouse_root: Path,
    *,
    component: Literal["agentcore", "lambda"],
    output_path: Path | None = None,
) -> Mapping[str, object]:
    """Inspect a predownloaded wheelhouse and emit canonical authority for review.

    This is a proposal/capture step, not a trust shortcut.  It performs no resolution,
    download, installer execution, or import from the wheel tree.  The returned JSON must
    be reviewed and then supplied separately to ``build_source_bundles``; the deployment
    seal accepts only the resulting locked v2 source request.
    """

    try:
        wheelhouse = wheelhouse_root.resolve(strict=True)
        if wheelhouse_root.is_symlink() or not wheelhouse.is_dir():
            raise ValueError
        candidates = sorted(wheelhouse.iterdir(), key=lambda path: path.name)
        if not candidates:
            raise ValueError
        wheels: list[dict[str, str]] = []
        for wheel in candidates:
            if (
                wheel.is_symlink()
                or not wheel.is_file()
                or not wheel.name.isascii()
                or wheel.name != Path(wheel.name).name
                or not wheel.name.endswith(".whl")
            ):
                raise ValueError
            metadata = _wheel_metadata(wheel)
            wheels.append(
                {
                    "filename": wheel.name,
                    "name": metadata.name,
                    "sha256": _file_fingerprint(wheel),
                    "version": metadata.version,
                }
            )
        wheels.sort(key=lambda record: record["name"])
        if len({record["name"] for record in wheels}) != len(wheels):
            raise ValueError
        _verify_wheelhouse_dependency_closure(candidates, component=component)

        with TemporaryDirectory(
            prefix=f"mr-lister-phase66-{component}-wheel-capture-"
        ) as temporary:
            temporary_root = Path(temporary)
            dependencies = temporary_root / "dependencies"
            dependencies.mkdir(mode=0o700)
            _extract_wheel_archives(candidates, destination=dependencies)
            tree_fingerprint = sha256(
                render_manifest(
                    {"files": _manifest_files(dependencies, excluded_names=frozenset())}
                )
            ).hexdigest()
            authority = normalize_wheel_authority(
                {
                    "algorithm": "sha256",
                    "component": component,
                    "dependency_tree_sha256": tree_fingerprint,
                    "format": "phase6-wheel-authority-v1",
                    "target": dict(LINUX_ARM64_TARGET),
                    "wheels": wheels,
                },
                component=component,
            )
            request_root = temporary_root / "request"
            request_root.mkdir(mode=0o700)
            _write_text(request_root / "requirements.txt", render_locked_requirements(authority))
            _write_dependency_build_request(
                request_root,
                component=component,
                authority=authority,
            )
            inspected = inspect_linux_arm64_dependency_artifact(
                dependencies,
                build_request_path=request_root / DEPENDENCY_BUILD_REQUEST_FILENAME,
            )
            if inspected.get("dependency_tree_sha256") != tree_fingerprint:
                raise ValueError

        if output_path is not None:
            expected_name = (
                AGENTCORE_WHEEL_AUTHORITY_FILENAME
                if component == "agentcore"
                else LAMBDA_WHEEL_AUTHORITY_FILENAME
            )
            destination = output_path.resolve(strict=False)
            if destination.name != expected_name or destination.exists():
                raise ValueError
            _write_bytes(destination, render_manifest(authority))
        return authority
    except (OSError, Phase6ReleaseAuthorityError, ValueError, zipfile.BadZipFile):
        raise ValueError("Phase 6 wheelhouse authority candidate is invalid") from None


def build_linux_arm64_dependencies_from_wheelhouse(
    wheelhouse_root: Path,
    *,
    destination: Path,
    build_request_path: Path,
) -> Path:
    """Safely extract only the exact reviewed wheels without invoking an installer."""

    try:
        wheelhouse = wheelhouse_root.resolve(strict=True)
        if wheelhouse_root.is_symlink() or not wheelhouse.is_dir():
            raise ValueError
        authority = wheel_authority_from_build_request(build_request_path)
        component = cast(str, authority["component"])
        expected_destination = (
            AGENTCORE_DEPENDENCY_DIRECTORY_NAME
            if component == "agentcore"
            else LAMBDA_DEPENDENCY_DIRECTORY_NAME
        )
        dependency_root = _new_exact_directory(destination, expected_destination)
        wheel_records = cast(Sequence[Mapping[str, str]], authority["wheels"])
        expected = {record["filename"]: record["sha256"] for record in wheel_records}
        candidates = sorted(wheelhouse.iterdir(), key=lambda path: path.name)
        if [path.name for path in candidates] != sorted(expected):
            raise ValueError
        for wheel in candidates:
            if (
                wheel.is_symlink()
                or not wheel.is_file()
                or _file_fingerprint(wheel) != expected[wheel.name]
            ):
                raise ValueError
        _verify_wheelhouse_dependency_closure(
            candidates,
            component=cast(Literal["agentcore", "lambda"], component),
            expected_wheels=wheel_records,
        )

        _extract_wheel_archives(candidates, destination=dependency_root)
        return write_linux_arm64_dependency_manifest(
            dependency_root,
            build_request_path=build_request_path,
        )
    except (Phase6ReleaseAuthorityError, ValueError):
        raise ValueError("Phase 6 locked wheelhouse authority is invalid") from None


def render_deterministic_zip(deployment_root: Path) -> bytes:
    """Render a sorted, stored ZIP with fixed metadata and exact deployment bytes."""

    root = deployment_root.resolve(strict=True)
    output = BytesIO()
    with zipfile.ZipFile(
        output,
        "w",
        compression=zipfile.ZIP_STORED,
        strict_timestamps=True,
    ) as archive:
        for record in _manifest_files(root, excluded_names=frozenset()):
            relative = cast(str, record["path"])
            info = zipfile.ZipInfo(relative, date_time=_ZIP_TIMESTAMP)
            info.compress_type = zipfile.ZIP_STORED
            info.create_system = 3
            info.external_attr = 0o100644 << 16
            info.flag_bits = 0
            archive.writestr(info, (root / relative).read_bytes())
    return output.getvalue()


def build_phase6_deployment_artifacts(
    deployment_root: Path,
    *,
    artifact_destination: Path,
    expected_release_fingerprint: str,
    verify_current_source: bool = True,
) -> Phase66DeploymentArtifacts:
    """Create deterministic component ZIPs plus their canonical shared descriptor."""

    deployment = deployment_root.resolve(strict=True)
    if deployment.name != DEPLOYMENT_DIRECTORY_NAME or not _has_exact_component_directories(
        deployment
    ):
        raise ValueError("Phase 6 deployment release directory is invalid")
    component_roots = {
        "agentcore": deployment / "agentcore",
        "lambda": deployment / "lambda",
    }
    bindings = {
        component: verify_phase6_packaged_release(
            {"MR_LISTER_RELEASE_FINGERPRINT": expected_release_fingerprint},
            component=cast(Literal["agentcore", "lambda"], component),
            bundle_root=root,
        )
        for component, root in component_roots.items()
    }
    if verify_current_source:
        authorities = {
            component: wheel_authority_from_build_request(root / DEPENDENCY_BUILD_REQUEST_FILENAME)
            for component, root in component_roots.items()
        }
        _verify_current_repository_source_authority(deployment, authorities=authorities)

    artifact_root = _new_exact_directory(artifact_destination, ARTIFACT_DIRECTORY_NAME)
    archives: dict[str, dict[str, object]] = {}
    archive_paths: dict[str, Path] = {}
    for component, filename in (
        ("agentcore", AGENTCORE_ARCHIVE_FILENAME),
        ("lambda", LAMBDA_ARCHIVE_FILENAME),
    ):
        raw = render_deterministic_zip(component_roots[component])
        archive_path = artifact_root / filename
        _write_bytes(archive_path, raw)
        archive_paths[component] = archive_path
        archives[component] = {
            "archive": {
                "path": filename,
                "sha256": sha256(raw).hexdigest(),
                "size_bytes": len(raw),
            },
            "architecture": "arm64",
            "component": component,
            "deployment_manifest_sha256": bindings[component].deployment_manifest_fingerprint,
            "package_format": "zip",
            "runtime": "python3.12",
        }
    descriptor = {
        "algorithm": "sha256",
        "components": archives,
        "format": _DESCRIPTOR_FORMAT,
        "release_fingerprint": expected_release_fingerprint,
        "target": dict(LINUX_ARM64_TARGET),
    }
    descriptor_path = artifact_root / DEPLOYMENT_DESCRIPTOR_FILENAME
    _write_bytes(descriptor_path, render_manifest(descriptor))
    verify_phase6_deployment_artifacts(
        deployment,
        artifact_root=artifact_root,
        verify_current_source=verify_current_source,
    )
    return Phase66DeploymentArtifacts(
        artifact_root=artifact_root,
        descriptor_path=descriptor_path,
        lambda_archive_path=archive_paths["lambda"],
        agentcore_archive_path=archive_paths["agentcore"],
        release_fingerprint=expected_release_fingerprint,
    )


def verify_phase6_deployment_artifacts(
    deployment_root: Path,
    *,
    artifact_root: Path,
    verify_current_source: bool = True,
) -> Mapping[str, object]:
    """Verify extracted deployments, deterministic ZIPs, descriptor, and embedded verifier."""

    try:
        deployment = deployment_root.resolve(strict=True)
        artifacts = artifact_root.resolve(strict=True)
        if (
            deployment.name != DEPLOYMENT_DIRECTORY_NAME
            or artifacts.name != ARTIFACT_DIRECTORY_NAME
            or not _has_exact_component_directories(deployment)
        ):
            raise ValueError
        expected_artifact_names = {
            AGENTCORE_ARCHIVE_FILENAME,
            DEPLOYMENT_DESCRIPTOR_FILENAME,
            LAMBDA_ARCHIVE_FILENAME,
        }
        artifact_entries = list(artifacts.iterdir())
        if (
            {entry.name for entry in artifact_entries} != expected_artifact_names
            or len(artifact_entries) != len(expected_artifact_names)
            or any(entry.is_symlink() or not entry.is_file() for entry in artifact_entries)
        ):
            raise ValueError
        descriptor_path = artifacts / DEPLOYMENT_DESCRIPTOR_FILENAME
        raw_descriptor = descriptor_path.read_bytes()
        descriptor = json.loads(raw_descriptor)
        if not isinstance(descriptor, Mapping) or render_manifest(descriptor) != raw_descriptor:
            raise ValueError
        if set(descriptor) != {
            "algorithm",
            "components",
            "format",
            "release_fingerprint",
            "target",
        }:
            raise ValueError
        release = descriptor["release_fingerprint"]
        if (
            not isinstance(release, str)
            or descriptor["algorithm"] != "sha256"
            or descriptor["format"] != _DESCRIPTOR_FORMAT
            or descriptor["target"] != LINUX_ARM64_TARGET
        ):
            raise ValueError
        components = descriptor["components"]
        if not isinstance(components, Mapping) or set(components) != {"agentcore", "lambda"}:
            raise ValueError
        roots = {component: deployment / component for component in ("agentcore", "lambda")}
        authorities: dict[str, dict[str, object]] = {}
        for component, filename in (
            ("agentcore", AGENTCORE_ARCHIVE_FILENAME),
            ("lambda", LAMBDA_ARCHIVE_FILENAME),
        ):
            root = roots[component]
            binding = verify_phase6_packaged_release(
                {"MR_LISTER_RELEASE_FINGERPRINT": release},
                component=cast(Literal["agentcore", "lambda"], component),
                bundle_root=root,
            )
            record = components[component]
            if not isinstance(record, Mapping) or set(record) != {
                "archive",
                "architecture",
                "component",
                "deployment_manifest_sha256",
                "package_format",
                "runtime",
            }:
                raise ValueError
            archive = record["archive"]
            if not isinstance(archive, Mapping) or set(archive) != {
                "path",
                "sha256",
                "size_bytes",
            }:
                raise ValueError
            archive_path = artifacts / filename
            raw_archive = archive_path.read_bytes()
            if (
                record["architecture"] != "arm64"
                or record["component"] != component
                or record["deployment_manifest_sha256"] != binding.deployment_manifest_fingerprint
                or record["package_format"] != "zip"
                or record["runtime"] != "python3.12"
                or archive["path"] != filename
                or archive["sha256"] != sha256(raw_archive).hexdigest()
                or archive["size_bytes"] != len(raw_archive)
                or raw_archive != render_deterministic_zip(root)
            ):
                raise ValueError
            _verify_archive_members(root, raw_archive)
            authorities[component] = wheel_authority_from_build_request(
                root / DEPENDENCY_BUILD_REQUEST_FILENAME
            )
        if verify_current_source:
            _verify_current_repository_source_authority(deployment, authorities=authorities)
        for component, root in roots.items():
            _verify_embedded_release_authority(
                root,
                component=cast(Literal["agentcore", "lambda"], component),
                release_fingerprint=release,
            )
        return descriptor
    except (OSError, Phase6ReleaseAuthorityError, ValueError, zipfile.BadZipFile):
        raise ValueError("Phase 6 deployment artifact is invalid") from None


def seal_release_bundles(
    source_release: Path,
    *,
    lambda_dependencies: Path,
    agentcore_dependencies: Path,
    destination: Path,
    artifact_destination: Path | None = None,
) -> tuple[Path, Path, str]:
    """Overlay verified dependency trees and seal two runtime-verifiable deployments."""

    source_release = source_release.resolve(strict=True)
    destination = destination.resolve(strict=False)
    if source_release.name != SOURCE_DIRECTORY_NAME or not _has_exact_component_directories(
        source_release
    ):
        raise ValueError("Phase 6 source release directory is invalid")
    if destination.name != DEPLOYMENT_DIRECTORY_NAME or destination.exists():
        raise ValueError("Phase 6 deployment destination must be a new phase6-deployment directory")
    source_roots = {
        "lambda": source_release / "lambda",
        "agentcore": source_release / "agentcore",
    }
    dependency_roots = {
        "lambda": lambda_dependencies.resolve(strict=True),
        "agentcore": agentcore_dependencies.resolve(strict=True),
    }
    authorities: dict[str, dict[str, object]] = {}
    for component, source_root in source_roots.items():
        verify_source_bundle(source_root)
        authorities[component] = wheel_authority_from_build_request(
            source_root / DEPENDENCY_BUILD_REQUEST_FILENAME
        )
        verify_linux_arm64_dependency_artifact(
            dependency_roots[component],
            build_request_path=source_root / DEPENDENCY_BUILD_REQUEST_FILENAME,
        )
    if authorities != _load_checked_wheel_authorities():
        raise ValueError("Phase 6 source does not match checked wheel authority")
    _verify_current_repository_source_authority(source_release, authorities=authorities)

    destination.mkdir(mode=0o700, parents=True)
    deployment_roots: dict[str, Path] = {}
    component_records: dict[str, dict[str, str]] = {}
    for component in ("agentcore", "lambda"):
        deployed = destination / component
        deployed.mkdir(mode=0o700)
        _copy_tree(source_roots[component], deployed)
        _overlay_dependency_tree(dependency_roots[component], deployed)
        deployment = {
            "algorithm": "sha256",
            "component": component,
            "files": _deployment_files(deployed),
            "format": "phase6-deployment-v1",
            "target": dict(LINUX_ARM64_TARGET),
        }
        deployment_path = deployed / DEPLOYMENT_MANIFEST_FILENAME
        _write_bytes(deployment_path, render_manifest(deployment))
        deployment_roots[component] = deployed
        component_records[component] = {
            "dependency_manifest_sha256": _file_fingerprint(
                deployed / DEPENDENCY_ARTIFACT_FILENAME
            ),
            "deployment_manifest_sha256": _file_fingerprint(deployment_path),
            "source_manifest_sha256": _file_fingerprint(deployed / "source-manifest.json"),
        }

    release = {
        "algorithm": "sha256",
        "components": component_records,
        "format": "phase6-release-v1",
        "target": dict(LINUX_ARM64_TARGET),
    }
    release_bytes = render_manifest(release)
    release_fingerprint = sha256(release_bytes).hexdigest()
    for component, deployed in deployment_roots.items():
        _write_bytes(deployed / RELEASE_MANIFEST_FILENAME, release_bytes)
        verify_phase6_packaged_release(
            {"MR_LISTER_RELEASE_FINGERPRINT": release_fingerprint},
            component=component,  # type: ignore[arg-type]
            bundle_root=deployed,
        )
    artifacts = build_phase6_deployment_artifacts(
        destination,
        artifact_destination=artifact_destination or destination.parent / ARTIFACT_DIRECTORY_NAME,
        expected_release_fingerprint=release_fingerprint,
        verify_current_source=True,
    )
    if artifacts.release_fingerprint != release_fingerprint:
        raise ValueError("Phase 6 deployment artifact release binding drifted")
    return deployment_roots["lambda"], deployment_roots["agentcore"], release_fingerprint


def _verify_current_repository_source_authority(
    packaged_release: Path,
    *,
    authorities: Mapping[str, Mapping[str, object]],
) -> None:
    """Require packaged source bytes to equal a fresh build from this checkout."""

    if set(authorities) != {"agentcore", "lambda"}:
        raise ValueError("Phase 6 current source authority is incomplete")
    normalized = _normalize_wheel_authorities(authorities)
    if normalized != _load_checked_wheel_authorities():
        raise ValueError("Phase 6 source does not match checked wheel authority")
    packaged = packaged_release.resolve(strict=True)
    with TemporaryDirectory(prefix="mr-lister-phase66-current-source-") as temporary:
        current_lambda, current_agentcore = build_source_bundles(
            Path(temporary) / SOURCE_DIRECTORY_NAME,
            wheel_authorities=normalized,
        )
        current_roots = {"agentcore": current_agentcore, "lambda": current_lambda}
        for component, current in current_roots.items():
            expected = _manifest_files(current, excluded_names=frozenset())
            actual_root = packaged / component
            actual: list[dict[str, object]] = []
            for record in expected:
                relative = cast(str, record["path"])
                target = actual_root / relative
                if target.is_symlink() or not target.is_file():
                    raise ValueError("Phase 6 source does not match current repository authority")
                content = target.read_bytes()
                actual.append(
                    {
                        "path": relative,
                        "sha256": sha256(content).hexdigest(),
                        "size_bytes": len(content),
                    }
                )
            if actual != expected:
                raise ValueError("Phase 6 source does not match current repository authority")


def _verify_embedded_release_authority(
    root: Path,
    *,
    component: Literal["agentcore", "lambda"],
    release_fingerprint: str,
) -> None:
    verifier = root / "mr_lister/release/phase6.py"
    module_name = f"_mr_lister_phase66_embedded_{component}_{release_fingerprint[:12]}"
    spec = importlib.util.spec_from_file_location(module_name, verifier)
    if spec is None or spec.loader is None:
        raise ValueError
    module = importlib.util.module_from_spec(spec)
    prior_bytecode = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
        binding = module.verify_phase6_packaged_release(
            {"MR_LISTER_RELEASE_FINGERPRINT": release_fingerprint},
            component=component,
            bundle_root=root,
        )
    finally:
        sys.modules.pop(module_name, None)
        sys.dont_write_bytecode = prior_bytecode
    if binding.release_fingerprint != release_fingerprint or binding.component != component:
        raise ValueError


def _verify_archive_members(deployment: Path, raw_archive: bytes) -> None:
    expected = _manifest_files(deployment, excluded_names=frozenset())
    with zipfile.ZipFile(BytesIO(raw_archive)) as archive:
        members = archive.infolist()
        if [member.filename for member in members] != [record["path"] for record in expected]:
            raise ValueError
        for member, record in zip(members, expected, strict=True):
            if (
                member.is_dir()
                or member.date_time != _ZIP_TIMESTAMP
                or member.compress_type != zipfile.ZIP_STORED
                or member.create_system != 3
                or member.external_attr != 0o100644 << 16
                or archive.read(member) != (deployment / cast(str, record["path"])).read_bytes()
            ):
                raise ValueError


def _safe_wheel_member(member: zipfile.ZipInfo) -> str:
    value = member.filename
    relative = value[:-1] if member.is_dir() and value.endswith("/") else value
    path = PurePosixPath(relative)
    file_type = (member.external_attr >> 16) & 0o170000
    if (
        not relative
        or not relative.isascii()
        or "\x00" in relative
        or "\\" in relative
        or path.is_absolute()
        or path.as_posix() != relative
        or any(part in {"", ".", "..", ".git", "__pycache__"} for part in path.parts)
        or member.flag_bits & 0x1
        or member.compress_type not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}
        or member.file_size < 0
        or member.file_size > _MAX_WHEEL_MEMBER_SIZE
        or file_type == 0o120000
        or (not member.is_dir() and value.endswith("/"))
    ):
        raise ValueError
    return relative


def _extract_wheel_archives(wheels: Sequence[Path], *, destination: Path) -> None:
    extracted: set[str] = set()
    total_size = 0
    for wheel in wheels:
        with zipfile.ZipFile(wheel) as archive:
            for member, relative in _validated_wheel_members(archive):
                if member.is_dir():
                    continue
                total_size += member.file_size
                folded = relative.casefold()
                if total_size > _MAX_EXTRACTED_TREE_SIZE or folded in extracted:
                    raise ValueError
                extracted.add(folded)
                _write_bytes(destination / relative, archive.read(member))
    if not extracted:
        raise ValueError


def _validated_wheel_members(
    archive: zipfile.ZipFile,
) -> list[tuple[zipfile.ZipInfo, str]]:
    members = archive.infolist()
    if not members or len({member.filename for member in members}) != len(members):
        raise ValueError
    validated: list[tuple[zipfile.ZipInfo, str]] = []
    file_paths: set[str] = set()
    for member in members:
        relative = _safe_wheel_member(member)
        if not member.is_dir():
            folded = relative.casefold()
            if folded in file_paths:
                raise ValueError
            file_paths.add(folded)
        validated.append((member, relative))
    return validated


def _wheel_metadata(wheel: Path) -> _WheelMetadata:
    filename_name, filename_version, _build, _tags = parse_wheel_filename(wheel.name)
    with zipfile.ZipFile(wheel) as archive:
        metadata_members: list[zipfile.ZipInfo] = []
        for member, relative in _validated_wheel_members(archive):
            path = PurePosixPath(relative)
            if (
                not member.is_dir()
                and len(path.parts) == 2
                and path.parts[0].endswith(".dist-info")
                and path.parts[1] == "METADATA"
            ):
                metadata_members.append(member)
        if len(metadata_members) != 1:
            raise ValueError
        raw = archive.read(metadata_members[0])
        if not raw or len(raw) > 1024 * 1024 or b"\x00" in raw:
            raise ValueError
    headers = Parser().parsestr(raw.decode("utf-8"), headersonly=True)
    name_values = headers.get_all("Name", [])
    version_values = headers.get_all("Version", [])
    if len(name_values) != 1 or len(version_values) != 1:
        raise ValueError
    raw_name = name_values[0]
    version = version_values[0]
    if (
        not isinstance(raw_name, str)
        or raw_name != raw_name.strip()
        or not isinstance(version, str)
        or version != version.strip()
        or _VERSION.fullmatch(version) is None
    ):
        raise ValueError
    name = str(canonicalize_name(raw_name))
    if (
        _NORMALIZED_DISTRIBUTION.fullmatch(name) is None
        or name != str(filename_name)
        or Version(version) != filename_version
    ):
        raise ValueError
    python_values = headers.get_all("Requires-Python", [])
    if len(python_values) > 1:
        raise ValueError
    requires_python = python_values[0] if python_values else None
    if requires_python is not None:
        if (
            not isinstance(requires_python, str)
            or requires_python != requires_python.strip()
            or not requires_python.isascii()
            or not SpecifierSet(requires_python).contains(Version("3.12.0"), prereleases=True)
        ):
            raise ValueError
    extras: set[str] = set()
    for value in headers.get_all("Provides-Extra", []):
        extras.add(_canonical_extra(value))
    requirements: list[Requirement] = []
    for value in headers.get_all("Requires-Dist", []):
        if not isinstance(value, str) or value != value.strip() or not value.isascii():
            raise ValueError
        requirement = Requirement(value)
        if requirement.url is not None:
            raise ValueError
        _normalize_requirement(requirement)
        requirements.append(requirement)
    return _WheelMetadata(
        name=name,
        version=version,
        requires_python=requires_python,
        requires_dist=tuple(requirements),
        provides_extra=frozenset(extras),
    )


def _canonical_extra(value: object) -> str:
    if not isinstance(value, str) or not value or value != value.strip() or not value.isascii():
        raise ValueError
    extra = str(canonicalize_name(value))
    if _NORMALIZED_DISTRIBUTION.fullmatch(extra) is None:
        raise ValueError
    return extra


def _normalize_requirement(requirement: Requirement) -> tuple[str, frozenset[str]]:
    name = str(canonicalize_name(requirement.name))
    if _NORMALIZED_DISTRIBUTION.fullmatch(name) is None:
        raise ValueError
    extras = frozenset(_canonical_extra(extra) for extra in requirement.extras)
    return name, extras


def _phase6_marker_environment() -> dict[str, str]:
    environment = default_environment()
    environment.update(
        {
            "implementation_name": "cpython",
            "implementation_version": "3.12.0",
            "os_name": "posix",
            "platform_machine": "aarch64",
            "platform_python_implementation": "CPython",
            "platform_system": "Linux",
            "python_full_version": "3.12.0",
            "python_version": "3.12",
            "sys_platform": "linux",
        }
    )
    return environment


def _verify_wheelhouse_dependency_closure(
    wheels: Sequence[Path],
    *,
    component: Literal["agentcore", "lambda"],
    expected_wheels: Sequence[Mapping[str, str]] | None = None,
) -> None:
    """Require the selected versions to close under applicable CPython 3.12 ARM64 edges."""

    metadata_by_name: dict[str, _WheelMetadata] = {}
    metadata_by_filename: dict[str, _WheelMetadata] = {}
    filenames: set[str] = set()
    for wheel in wheels:
        folded = wheel.name.casefold()
        if folded in filenames:
            raise ValueError
        filenames.add(folded)
        metadata = _wheel_metadata(wheel)
        if metadata.name in metadata_by_name:
            raise ValueError
        metadata_by_name[metadata.name] = metadata
        metadata_by_filename[wheel.name] = metadata

    if expected_wheels is not None:
        expected = {record["filename"]: record for record in expected_wheels}
        if set(metadata_by_filename) != set(expected):
            raise ValueError
        for filename, metadata in metadata_by_filename.items():
            record = expected[filename]
            if record["name"] != metadata.name or record["version"] != metadata.version:
                raise ValueError

    environment = _phase6_marker_environment()
    active_extras = {name: set() for name in metadata_by_name}

    def require(requirement: Requirement) -> bool:
        name, extras = _normalize_requirement(requirement)
        dependency = metadata_by_name.get(name)
        if dependency is None or not requirement.specifier.contains(
            Version(dependency.version),
            prereleases=True,
        ):
            raise ValueError
        if not extras.issubset(dependency.provides_extra):
            raise ValueError
        prior = len(active_extras[name])
        active_extras[name].update(extras)
        return len(active_extras[name]) != prior

    for value in _COMPONENT_ROOT_REQUIREMENTS[component]:
        require(Requirement(value))

    changed = True
    while changed:
        changed = False
        for metadata in metadata_by_name.values():
            marker_extras = {"", *active_extras[metadata.name]}
            for requirement in metadata.requires_dist:
                if requirement.marker is not None and not any(
                    requirement.marker.evaluate({**environment, "extra": extra})
                    for extra in marker_extras
                ):
                    continue
                changed = require(requirement) or changed


def _new_exact_directory(path: Path, expected_name: str) -> Path:
    destination = path.resolve(strict=False)
    if destination.name != expected_name or destination.exists():
        raise ValueError("Phase 6 destination must be new and exactly named")
    destination.mkdir(mode=0o700, parents=True)
    return destination


def _has_exact_component_directories(root: Path) -> bool:
    entries = list(root.iterdir())
    return (
        {entry.name for entry in entries} == {"agentcore", "lambda"}
        and len(entries) == 2
        and all(not entry.is_symlink() and entry.is_dir() for entry in entries)
    )


def _copy_common(destination: Path) -> None:
    for name in _COMMON_ROOT_FILES:
        target = destination / "mr_lister" / name
        if name == "__init__.py":
            _write_bytes(target, _CAPABILITY_FREE_INIT_BYTES)
        else:
            _copy_file(ROOT / "src/mr_lister" / name, target)


def _copy_release_authority(destination: Path) -> None:
    release = destination / "mr_lister/release"
    _write_bytes(release / "__init__.py", _CAPABILITY_FREE_INIT_BYTES)
    _copy_file(ROOT / "src/mr_lister/release/phase6.py", release / "phase6.py")


def _copy_selected(source: Path, destination: Path, names: tuple[str, ...]) -> None:
    for name in names:
        _copy_file(source / name, destination / name)


def _copy_directory(source: Path, destination: Path) -> None:
    for path in sorted(source.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        _copy_file(path, destination / path.relative_to(source))


def _copy_file(source: Path, destination: Path) -> None:
    if not source.is_file() or source.is_symlink():
        raise ValueError("Phase 6 source input is unavailable")
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    shutil.copyfile(source, destination, follow_symlinks=False)
    destination.chmod(0o644)


def _write_text(destination: Path, value: str) -> None:
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    destination.write_text(value, encoding="utf-8", newline="\n")
    destination.chmod(0o644)


def _write_bytes(destination: Path, value: bytes) -> None:
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    destination.write_bytes(value)
    destination.chmod(0o644)


def _write_dependency_build_request(
    root: Path,
    *,
    component: str,
    authority: Mapping[str, object] | None,
) -> None:
    requirements = root / "requirements.txt"
    requirement_record: dict[str, object] = {
        "path": "requirements.txt",
        "required_distributions": _REQUIRED_DISTRIBUTIONS[component],
        "sha256": _file_fingerprint(requirements),
    }
    request_format = LEGACY_BUILD_REQUEST_FORMAT
    if authority is not None:
        wheels = authority["wheels"]
        if not isinstance(wheels, list):
            raise ValueError("Phase 6 wheel authority is malformed")
        requirement_record.update(
            {
                "dependency_tree_sha256": authority["dependency_tree_sha256"],
                "required_distributions": [record["name"] for record in wheels],
                "wheel_artifacts": [dict(record) for record in wheels],
            }
        )
        request_format = LOCKED_BUILD_REQUEST_FORMAT
    payload = {
        "algorithm": "sha256",
        "component": component,
        "format": request_format,
        "requirements": requirement_record,
        "target": dict(LINUX_ARM64_TARGET),
    }
    _write_bytes(root / DEPENDENCY_BUILD_REQUEST_FILENAME, render_manifest(payload))


def _copy_tree(source: Path, destination: Path) -> None:
    for path in sorted(source.rglob("*")):
        if path.is_symlink():
            raise ValueError("Phase 6 release input contains a symlink")
        if path.is_file():
            _copy_file(path, destination / path.relative_to(source))


def _overlay_dependency_tree(source: Path, destination: Path) -> None:
    for path in sorted(source.rglob("*")):
        if path.is_symlink():
            raise ValueError("Phase 6 dependency artifact contains a symlink")
        if not path.is_file():
            continue
        target = destination / path.relative_to(source)
        if target.exists():
            raise ValueError("Phase 6 dependency artifact collides with application source")
        _copy_file(path, target)


def _deployment_files(root: Path) -> list[dict[str, object]]:
    return _manifest_files(
        root,
        excluded_names=frozenset({DEPLOYMENT_MANIFEST_FILENAME, RELEASE_MANIFEST_FILENAME}),
    )


def _file_fingerprint(path: Path) -> str:
    if path.is_symlink() or not path.is_file():
        raise ValueError("Phase 6 release input is unavailable")
    return sha256(path.read_bytes()).hexdigest()


def _manifest_files(
    root: Path,
    *,
    excluded_names: frozenset[str] = frozenset({"source-manifest.json"}),
) -> list[dict[str, object]]:
    files: list[dict[str, object]] = []
    paths = sorted(root.rglob("*"), key=lambda path: path.relative_to(root).as_posix())
    for path in paths:
        if path.is_symlink():
            raise ValueError("Phase 6 release input contains a symlink")
        if not path.is_file():
            continue
        relative = path.relative_to(root).as_posix()
        if relative in excluded_names:
            continue
        content = path.read_bytes()
        files.append(
            {
                "path": relative,
                "sha256": sha256(content).hexdigest(),
                "size_bytes": len(content),
            }
        )
    return files


def _write_manifest(root: Path) -> None:
    payload = {
        "algorithm": "sha256",
        "files": _manifest_files(root),
        "format": "phase6-source-v1",
    }
    _write_text(
        root / "source-manifest.json",
        json.dumps(payload, allow_nan=False, indent=2, sort_keys=True) + "\n",
    )


def _assert_hygiene(root: Path) -> None:
    forbidden_parts = {
        ".DS_Store",
        ".env",
        ".git",
        ".mr_lister_private",
        "__pycache__",
        "api",
        "tests",
    }
    forbidden_suffixes = {".map", ".pem", ".key", ".zip"}
    for path in root.rglob("*"):
        relative = path.relative_to(root)
        if path.is_symlink() or any(part in forbidden_parts for part in relative.parts):
            raise ValueError("Phase 6 source bundle contains a forbidden path")
        if path.is_file() and path.suffix.casefold() in forbidden_suffixes:
            raise ValueError("Phase 6 source bundle contains a forbidden file")
    forbidden_files = {
        "mr_lister/production/adapter.py",
        "mr_lister/workflow/service.py",
        "mr_lister/durable/handlers.py",
    }
    if any((root / name).exists() for name in forbidden_files):
        raise ValueError("Phase 6 source bundle contains a legacy broad-capability module")


def _read_wheel_authority(path: Path) -> Mapping[str, object]:
    if path.is_symlink() or not path.is_file() or not 1 <= path.stat().st_size <= 4 * 1024 * 1024:
        raise ValueError("Phase 6 wheel authority file is invalid")
    raw = path.read_bytes()
    value = json.loads(raw)
    if not isinstance(value, Mapping) or render_manifest(value) != raw:
        raise ValueError("Phase 6 wheel authority file is invalid")
    return value


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--destination", type=Path, default=DEFAULT_DESTINATION)
    actions = parser.add_mutually_exclusive_group()
    actions.add_argument("--verify", type=Path)
    actions.add_argument("--capture-wheelhouse-authority", type=Path)
    actions.add_argument("--build-dependencies-from-wheelhouse", type=Path)
    actions.add_argument("--write-dependency-manifest", type=Path)
    actions.add_argument("--verify-dependency-artifact", type=Path)
    actions.add_argument("--seal-source-release", type=Path)
    actions.add_argument("--verify-deployment", type=Path)
    parser.add_argument("--build-request", type=Path)
    parser.add_argument("--lambda-dependencies", type=Path)
    parser.add_argument("--agentcore-dependencies", type=Path)
    parser.add_argument("--lambda-wheel-authority", type=Path)
    parser.add_argument("--agentcore-wheel-authority", type=Path)
    parser.add_argument(
        "--legacy-source-only",
        action="store_true",
        help="emit the explicit v1 source-only request that the deployment seal rejects",
    )
    parser.add_argument("--dependency-destination", type=Path)
    parser.add_argument(
        "--deployment-destination",
        type=Path,
        default=DEFAULT_DEPLOYMENT_DESTINATION,
    )
    parser.add_argument(
        "--artifact-destination",
        type=Path,
        default=DEFAULT_ARTIFACT_DESTINATION,
    )
    parser.add_argument("--artifact-root", type=Path)
    parser.add_argument("--component", choices=("agentcore", "lambda"))
    parser.add_argument("--authority-output", type=Path)
    arguments = parser.parse_args()
    if arguments.verify is not None:
        verify_source_bundle(arguments.verify)
        print(arguments.verify.resolve(strict=True))
        return
    if arguments.capture_wheelhouse_authority is not None:
        if arguments.component is None or arguments.authority_output is None:
            parser.error(
                "--capture-wheelhouse-authority requires --component and --authority-output"
            )
        capture_wheelhouse_authority_candidate(
            arguments.capture_wheelhouse_authority,
            component=arguments.component,
            output_path=arguments.authority_output,
        )
        print(arguments.authority_output.resolve(strict=True))
        return
    if arguments.build_dependencies_from_wheelhouse is not None:
        if arguments.build_request is None or arguments.dependency_destination is None:
            parser.error(
                "--build-dependencies-from-wheelhouse requires --build-request and "
                "--dependency-destination"
            )
        print(
            build_linux_arm64_dependencies_from_wheelhouse(
                arguments.build_dependencies_from_wheelhouse,
                destination=arguments.dependency_destination,
                build_request_path=arguments.build_request,
            )
        )
        return
    if arguments.write_dependency_manifest is not None:
        if arguments.build_request is None:
            parser.error("--write-dependency-manifest requires --build-request")
        manifest = write_linux_arm64_dependency_manifest(
            arguments.write_dependency_manifest,
            build_request_path=arguments.build_request,
        )
        print(manifest)
        return
    if arguments.verify_dependency_artifact is not None:
        if arguments.build_request is None:
            parser.error("--verify-dependency-artifact requires --build-request")
        verify_linux_arm64_dependency_artifact(
            arguments.verify_dependency_artifact,
            build_request_path=arguments.build_request,
        )
        print(arguments.verify_dependency_artifact.resolve(strict=True))
        return
    if arguments.seal_source_release is not None:
        if arguments.lambda_dependencies is None or arguments.agentcore_dependencies is None:
            parser.error(
                "--seal-source-release requires --lambda-dependencies and --agentcore-dependencies"
            )
        lambda_root, agentcore_root, fingerprint = seal_release_bundles(
            arguments.seal_source_release,
            lambda_dependencies=arguments.lambda_dependencies,
            agentcore_dependencies=arguments.agentcore_dependencies,
            destination=arguments.deployment_destination,
            artifact_destination=arguments.artifact_destination,
        )
        print(lambda_root)
        print(agentcore_root)
        print(fingerprint)
        return
    if arguments.verify_deployment is not None:
        if arguments.artifact_root is None:
            parser.error("--verify-deployment requires --artifact-root")
        verify_phase6_deployment_artifacts(
            arguments.verify_deployment,
            artifact_root=arguments.artifact_root,
        )
        print(arguments.verify_deployment.resolve(strict=True))
        return
    if any(
        value is not None
        for value in (
            arguments.build_request,
            arguments.lambda_dependencies,
            arguments.agentcore_dependencies,
            arguments.dependency_destination,
            arguments.artifact_root,
            arguments.component,
            arguments.authority_output,
        )
    ):
        parser.error("dependency options require an explicit dependency or seal action")
    authority_paths = (arguments.lambda_wheel_authority, arguments.agentcore_wheel_authority)
    if (authority_paths[0] is None) != (authority_paths[1] is None):
        parser.error("both --lambda-wheel-authority and --agentcore-wheel-authority are required")
    if arguments.legacy_source_only and authority_paths[0] is not None:
        parser.error("--legacy-source-only cannot carry wheel authority")
    authorities = (
        {
            "lambda": _read_wheel_authority(cast(Path, authority_paths[0])),
            "agentcore": _read_wheel_authority(cast(Path, authority_paths[1])),
        }
        if authority_paths[0] is not None
        else None
    )
    lambda_root, agentcore_root = build_source_bundles(
        arguments.destination,
        wheel_authorities=authorities,
        legacy_source_only=arguments.legacy_source_only,
    )
    print(lambda_root)
    print(agentcore_root)


if __name__ == "__main__":
    main()


__all__ = [
    "AGENTCORE_ARCHIVE_FILENAME",
    "AGENTCORE_DEPENDENCY_DIRECTORY_NAME",
    "AGENTCORE_WHEEL_AUTHORITY_FILENAME",
    "ARTIFACT_DIRECTORY_NAME",
    "DEFAULT_ARTIFACT_DESTINATION",
    "DEFAULT_DEPLOYMENT_DESTINATION",
    "DEFAULT_DESTINATION",
    "DEPLOYMENT_DESCRIPTOR_FILENAME",
    "LAMBDA_ARCHIVE_FILENAME",
    "LAMBDA_DEPENDENCY_DIRECTORY_NAME",
    "LAMBDA_WHEEL_AUTHORITY_FILENAME",
    "Phase66DeploymentArtifacts",
    "build_linux_arm64_dependencies_from_wheelhouse",
    "build_phase6_deployment_artifacts",
    "build_source_bundles",
    "capture_wheelhouse_authority_candidate",
    "render_deterministic_zip",
    "seal_release_bundles",
    "verify_phase6_deployment_artifacts",
    "verify_source_bundle",
    "write_linux_arm64_dependency_manifest",
]
