"""Build one deterministic Linux ARM64 Phase 7.18 enabled runtime artifact.

The builder performs local file operations only.  It derives the complete local import closure,
copies the frozen 7.1.0 contract and Phase 6 product profile, binds the runtime to reviewed canary
and enablement evidence, reuses the checked Phase 6 Lambda wheel authority, and emits an immutable
``enabled.zip``.  It never resolves packages, opens the network, calls AWS, or deploys a stack.
"""

from __future__ import annotations

import argparse
import ast
import importlib.util
import json
import shutil
import sys
import zipfile
from collections import deque
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from hashlib import sha256
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Final, cast

from mr_lister.release.phase6 import (
    DEPENDENCY_ARTIFACT_FILENAME,
    DEPENDENCY_BUILD_REQUEST_FILENAME,
    LINUX_ARM64_TARGET,
    LOCKED_BUILD_REQUEST_FORMAT,
    normalize_wheel_authority,
    render_locked_requirements,
    render_manifest,
    verify_dependency_build_request,
    verify_linux_arm64_dependency_artifact,
)
from mr_lister.release.phase718 import (
    PHASE718_BINDING_FILENAME,
    PHASE718_CONTRACT_FINGERPRINT,
    PHASE718_CONTRACT_PATH,
    PHASE718_CONTRACT_VERSION,
    PHASE718_DEPLOYMENT_MANIFEST_FILENAME,
    PHASE718_ENTRYPOINTS,
    PHASE718_PROFILE_FILE_FINGERPRINT,
    PHASE718_PROFILE_FINGERPRINT,
    PHASE718_PROFILE_PATH,
    PHASE718_RELEASE_MANIFEST_FILENAME,
    PHASE718_SOURCE_MANIFEST_FILENAME,
    verify_phase718_runtime_release,
)
from tools.build_phase66_source_bundles import (
    LAMBDA_DEPENDENCY_DIRECTORY_NAME,
)
from tools.build_phase66_source_bundles import (
    build_linux_arm64_dependencies_from_wheelhouse as build_phase6_dependencies,
)
from tools.build_phase66_source_bundles import (
    write_linux_arm64_dependency_manifest as write_phase6_dependency_manifest,
)
from tools.render_phase718_enabled_template import (
    TOPOLOGY_AUTHORITY_SHA256,
    render_phase718_enabled_template,
)

ROOT: Final = Path(__file__).resolve().parents[1]
CHECKED_LAMBDA_WHEEL_AUTHORITY: Final = (
    ROOT / "config/release/phase6/phase6-lambda-wheel-authority.json"
)
CHECKED_LAMBDA_WHEEL_AUTHORITY_SHA256: Final = (
    "145ae4affca308e4268120e5f5f725d1e91ba3194db76585e53cba32d29eefbd"
)

DEFAULT_SOURCE_DESTINATION: Final = ROOT / ".mr_lister_private/phase718-enabled-source"
DEFAULT_DEPENDENCY_DESTINATION: Final = (
    ROOT / ".mr_lister_private" / LAMBDA_DEPENDENCY_DIRECTORY_NAME
)
DEFAULT_DEPLOYMENT_DESTINATION: Final = ROOT / ".mr_lister_private/phase718-enabled-deployment"
DEFAULT_ARTIFACT_DESTINATION: Final = ROOT / ".mr_lister_private/phase718-enabled-artifact"

ENABLED_SOURCE_DIRECTORY_NAME: Final = "phase718-enabled-source"
ENABLED_DEPENDENCY_DIRECTORY_NAME: Final = LAMBDA_DEPENDENCY_DIRECTORY_NAME
ENABLED_DEPLOYMENT_DIRECTORY_NAME: Final = "phase718-enabled-deployment"
ENABLED_ARTIFACT_DIRECTORY_NAME: Final = "phase718-enabled-artifact"
ENABLED_ARCHIVE_FILENAME: Final = "enabled.zip"
ENABLED_DESCRIPTOR_FILENAME: Final = "deployment-descriptor.json"

_ENTRYPOINT_MODULE: Final = "mr_lister.cloud.phase718_entrypoints"
_RELEASE_MODULE: Final = "mr_lister.release.phase718"
_SOURCE_FORMAT: Final = "phase718-enabled-source-v1"
_DEPLOYMENT_FORMAT: Final = "phase718-enabled-deployment-v1"
_RELEASE_FORMAT: Final = "phase718-enabled-release-v1"
_BINDING_FORMAT: Final = "phase718-enabled-binding-v1"
_DESCRIPTOR_FORMAT: Final = "phase718-enabled-deployment-descriptor-v1"
_COMPONENT: Final = "phase718-enabled-lambda"
_THIRD_PARTY_IMPORT_ROOTS: Final = ("PIL", "boto3", "botocore", "pydantic")
_CAPABILITY_FREE_INITIALIZERS: Final = frozenset(
    {
        "mr_lister",
        "mr_lister.cloud",
        "mr_lister.control",
        "mr_lister.production",
        "mr_lister.publication",
        "mr_lister.release",
        "mr_lister.workflow",
    }
)
_FORBIDDEN_PATH_PARTS: Final = frozenset(
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
_FORBIDDEN_SUFFIXES: Final = frozenset(
    {".dylib", ".dll", ".key", ".map", ".pem", ".pyc", ".pyd", ".whl", ".zip"}
)
_ZIP_TIMESTAMP: Final = (1980, 1, 1, 0, 0, 0)
_GENERIC_ERROR: Final = "Phase 7.18 enabled release build is invalid"


class Phase718EnabledReleaseError(RuntimeError):
    """Value-free failure for unsafe, drifting, or incomplete enabled release input."""


@dataclass(frozen=True, slots=True)
class Phase718EnabledArtifact:
    deployment_root: Path
    archive_path: Path
    descriptor_path: Path
    release_fingerprint: str
    archive_fingerprint: str
    application_release_fingerprint: str
    canary_evidence_fingerprint: str
    enablement_evidence_fingerprint: str
    contract_fingerprint: str
    profile_fingerprint: str
    binding_fingerprint: str
    enabled_template_fingerprint: str


def build_enabled_source_bundle(
    destination: Path,
    *,
    application_release_fingerprint: str,
    canary_evidence_fingerprint: str,
    enablement_evidence_fingerprint: str,
    state_table: str,
    repository_root: Path = ROOT,
) -> Path:
    """Copy the exact enabled closure, immutable authorities, and evidence binding."""

    created: Path | None = None
    try:
        _validate_binding_inputs(
            application_release_fingerprint=application_release_fingerprint,
            canary_evidence_fingerprint=canary_evidence_fingerprint,
            enablement_evidence_fingerprint=enablement_evidence_fingerprint,
            state_table=state_table,
        )
        repository = repository_root.resolve(strict=True)
        source_root = _new_exact_directory(destination, ENABLED_SOURCE_DIRECTORY_NAME)
        created = source_root
        modules = resolve_enabled_import_closure(repository)
        for module, source in modules.items():
            relative = source.relative_to(repository / "src")
            raw = b"" if module in _CAPABILITY_FREE_INITIALIZERS else source.read_bytes()
            _write_bytes(source_root / relative, raw)

        contract_source = repository / PHASE718_CONTRACT_PATH
        contract_raw = contract_source.read_bytes()
        contract = json.loads(contract_raw)
        if (
            contract_source.is_symlink()
            or not isinstance(contract, Mapping)
            or render_manifest(cast(Mapping[str, object], contract)) != contract_raw
            or sha256(contract_raw).hexdigest() != PHASE718_CONTRACT_FINGERPRINT
            or contract.get("contract_version") != PHASE718_CONTRACT_VERSION
            or contract.get("current_activation_phase") != "general_availability"
            or contract.get("publication_enabled") is not True
            or contract.get("phase6_runtime_unchanged") is not True
            or contract.get("status") != "frozen"
        ):
            raise ValueError
        _copy_file(contract_source, source_root / PHASE718_CONTRACT_PATH)

        profile_source = repository / PHASE718_PROFILE_PATH
        profile_raw = profile_source.read_bytes()
        profile = json.loads(profile_raw)
        if (
            profile_source.is_symlink()
            or not isinstance(profile, Mapping)
            or sha256(profile_raw).hexdigest() != PHASE718_PROFILE_FILE_FINGERPRINT
            or profile.get("profile_id") != "gildan_64000_swiftpod"
            or profile.get("profile_version") != 2
            or profile.get("publish_enabled") is not False
        ):
            raise ValueError
        _copy_file(profile_source, source_root / PHASE718_PROFILE_PATH)

        binding = {
            "application_release_fingerprint": application_release_fingerprint,
            "canary_evidence_fingerprint": canary_evidence_fingerprint,
            "contract_fingerprint": PHASE718_CONTRACT_FINGERPRINT,
            "contract_version": PHASE718_CONTRACT_VERSION,
            "enablement_evidence_fingerprint": enablement_evidence_fingerprint,
            "entrypoints": list(PHASE718_ENTRYPOINTS),
            "format": _BINDING_FORMAT,
            "profile_fingerprint": PHASE718_PROFILE_FINGERPRINT,
            "state_table": state_table,
        }
        binding_bytes = render_manifest(binding)
        _write_bytes(source_root / PHASE718_BINDING_FILENAME, binding_bytes)
        _write_checked_dependency_request(source_root, repository=repository)
        _assert_source_hygiene(source_root, modules=modules)

        source_manifest = {
            "algorithm": "sha256",
            "binding": {
                "path": PHASE718_BINDING_FILENAME,
                "sha256": sha256(binding_bytes).hexdigest(),
            },
            "contract": {
                "contract_version": PHASE718_CONTRACT_VERSION,
                "path": PHASE718_CONTRACT_PATH,
                "publication_enabled": True,
                "sha256": PHASE718_CONTRACT_FINGERPRINT,
                "status": "frozen",
            },
            "entrypoints": list(PHASE718_ENTRYPOINTS),
            "files": _inventory(
                source_root,
                excluded=frozenset({PHASE718_SOURCE_MANIFEST_FILENAME}),
            ),
            "format": _SOURCE_FORMAT,
            "profile": {
                "file_sha256": PHASE718_PROFILE_FILE_FINGERPRINT,
                "fingerprint": PHASE718_PROFILE_FINGERPRINT,
                "path": PHASE718_PROFILE_PATH,
                "profile_id": "gildan_64000_swiftpod",
                "profile_version": 2,
                "publish_enabled": False,
            },
            "target": dict(LINUX_ARM64_TARGET),
            "third_party_import_roots": list(_THIRD_PARTY_IMPORT_ROOTS),
            "topology": {
                "enabled_template_sha256": sha256(render_phase718_enabled_template()).hexdigest(),
                "topology_authority_sha256": TOPOLOGY_AUTHORITY_SHA256,
            },
        }
        _write_bytes(
            source_root / PHASE718_SOURCE_MANIFEST_FILENAME,
            render_manifest(source_manifest),
        )
        verify_enabled_source_bundle(source_root)
        return source_root
    except Exception as error:
        if created is not None and created.exists():
            shutil.rmtree(created)
        if isinstance(error, Phase718EnabledReleaseError):
            raise
        raise Phase718EnabledReleaseError(_GENERIC_ERROR) from None


def verify_enabled_source_bundle(root: Path) -> Mapping[str, object]:
    """Verify the complete canonical source inventory and its immutable binding."""

    try:
        source = root.resolve(strict=True)
        if root.is_symlink() or source.name != ENABLED_SOURCE_DIRECTORY_NAME:
            raise ValueError
        raw, manifest = _read_canonical(source / PHASE718_SOURCE_MANIFEST_FILENAME)
        del raw
        _require_exact_keys(
            manifest,
            {
                "algorithm",
                "binding",
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
            or manifest["format"] != _SOURCE_FORMAT
            or manifest["entrypoints"] != list(PHASE718_ENTRYPOINTS)
            or manifest["target"] != LINUX_ARM64_TARGET
            or manifest["third_party_import_roots"] != list(_THIRD_PARTY_IMPORT_ROOTS)
            or manifest["files"]
            != _inventory(source, excluded=frozenset({PHASE718_SOURCE_MANIFEST_FILENAME}))
        ):
            raise ValueError
        binding_raw, binding = _read_canonical(source / PHASE718_BINDING_FILENAME)
        binding_record = manifest["binding"]
        if not isinstance(binding_record, Mapping) or binding_record != {
            "path": PHASE718_BINDING_FILENAME,
            "sha256": sha256(binding_raw).hexdigest(),
        }:
            raise ValueError
        _verify_binding(binding)
        _verify_contract_and_profile(source)
        verify_dependency_build_request(source / DEPENDENCY_BUILD_REQUEST_FILENAME)
        return manifest
    except Exception as error:
        if isinstance(error, Phase718EnabledReleaseError):
            raise
        raise Phase718EnabledReleaseError(_GENERIC_ERROR) from None


def resolve_enabled_import_closure(repository_root: Path = ROOT) -> dict[str, Path]:
    """Derive all local imports reachable from the six release-first entrypoints."""

    try:
        repository = repository_root.resolve(strict=True)
        source_root = repository / "src"
        queue: deque[str] = deque((_ENTRYPOINT_MODULE, _RELEASE_MODULE))
        resolved: dict[str, Path] = {}
        while queue:
            module = queue.popleft()
            if module in resolved:
                continue
            source = _module_path(source_root, module)
            if source is None:
                raise ValueError
            resolved[module] = source
            for parent in _parent_packages(module):
                if parent not in resolved:
                    queue.append(parent)
            if module in _CAPABILITY_FREE_INITIALIZERS:
                continue
            for imported in _local_imports(source_root, module, source):
                if imported not in resolved:
                    queue.append(imported)
        if (
            not {_ENTRYPOINT_MODULE, _RELEASE_MODULE}.issubset(resolved)
            or _third_party_import_roots(resolved) != _THIRD_PARTY_IMPORT_ROOTS
        ):
            raise ValueError
        return dict(sorted(resolved.items()))
    except Exception:
        raise Phase718EnabledReleaseError(_GENERIC_ERROR) from None


def build_linux_arm64_dependencies_from_wheelhouse(
    wheelhouse_root: Path,
    *,
    destination: Path,
    build_request_path: Path,
) -> Path:
    """Safely extract only the checked Phase 6 Lambda wheel set."""

    try:
        if destination.resolve(strict=False).name != ENABLED_DEPENDENCY_DIRECTORY_NAME:
            raise ValueError
        return build_phase6_dependencies(
            wheelhouse_root,
            destination=destination,
            build_request_path=build_request_path,
        )
    except Exception:
        raise Phase718EnabledReleaseError(_GENERIC_ERROR) from None


def write_linux_arm64_dependency_manifest(
    artifact_root: Path,
    *,
    build_request_path: Path,
) -> Path:
    """Inspect and seal a controlled Linux ARM64 dependency tree."""

    try:
        if artifact_root.resolve(strict=True).name != ENABLED_DEPENDENCY_DIRECTORY_NAME:
            raise ValueError
        return write_phase6_dependency_manifest(
            artifact_root,
            build_request_path=build_request_path,
        )
    except Exception:
        raise Phase718EnabledReleaseError(_GENERIC_ERROR) from None


def seal_enabled_release(
    source_root: Path,
    *,
    dependencies: Path,
    deployment_destination: Path,
    artifact_destination: Path,
) -> Phase718EnabledArtifact:
    """Overlay verified bytes and emit one deterministic enabled ZIP and descriptor."""

    created: list[Path] = []
    try:
        source = source_root.resolve(strict=True)
        verify_enabled_source_bundle(source)
        binding_raw, binding = _read_canonical(source / PHASE718_BINDING_FILENAME)
        _verify_current_repository_source_authority(source, binding=binding)
        dependency_root = dependencies.resolve(strict=True)
        if dependencies.is_symlink() or dependency_root.name != ENABLED_DEPENDENCY_DIRECTORY_NAME:
            raise ValueError
        verify_linux_arm64_dependency_artifact(
            dependency_root,
            build_request_path=source / DEPENDENCY_BUILD_REQUEST_FILENAME,
        )

        deployment = _new_exact_directory(
            deployment_destination,
            ENABLED_DEPLOYMENT_DIRECTORY_NAME,
        )
        created.append(deployment)
        _copy_tree(source, deployment)
        _overlay_dependency_tree(dependency_root, deployment)
        deployment_manifest = {
            "algorithm": "sha256",
            "component": _COMPONENT,
            "entrypoints": list(PHASE718_ENTRYPOINTS),
            "files": _inventory(
                deployment,
                excluded=frozenset(
                    {
                        PHASE718_DEPLOYMENT_MANIFEST_FILENAME,
                        PHASE718_RELEASE_MANIFEST_FILENAME,
                    }
                ),
            ),
            "format": _DEPLOYMENT_FORMAT,
            "target": dict(LINUX_ARM64_TARGET),
        }
        deployment_bytes = render_manifest(deployment_manifest)
        _write_bytes(deployment / PHASE718_DEPLOYMENT_MANIFEST_FILENAME, deployment_bytes)

        source_fingerprint = _file_fingerprint(deployment / PHASE718_SOURCE_MANIFEST_FILENAME)
        dependency_fingerprint = _file_fingerprint(deployment / DEPENDENCY_ARTIFACT_FILENAME)
        application_release = _fingerprint(binding["application_release_fingerprint"])
        canary_evidence = _fingerprint(binding["canary_evidence_fingerprint"])
        enablement_evidence = _fingerprint(binding["enablement_evidence_fingerprint"])
        state_table = cast(str, binding["state_table"])
        release_manifest = {
            "algorithm": "sha256",
            "application_release_fingerprint": application_release,
            "binding_sha256": sha256(binding_raw).hexdigest(),
            "canary_evidence_fingerprint": canary_evidence,
            "component": _COMPONENT,
            "contract_fingerprint": PHASE718_CONTRACT_FINGERPRINT,
            "dependency_manifest_sha256": dependency_fingerprint,
            "deployment_manifest_sha256": sha256(deployment_bytes).hexdigest(),
            "enablement_evidence_fingerprint": enablement_evidence,
            "entrypoints": list(PHASE718_ENTRYPOINTS),
            "format": _RELEASE_FORMAT,
            "profile_fingerprint": PHASE718_PROFILE_FINGERPRINT,
            "source_manifest_sha256": source_fingerprint,
            "state_table": state_table,
            "target": dict(LINUX_ARM64_TARGET),
        }
        release_bytes = render_manifest(release_manifest)
        release_fingerprint = sha256(release_bytes).hexdigest()
        _write_bytes(deployment / PHASE718_RELEASE_MANIFEST_FILENAME, release_bytes)
        environment = _release_environment(
            deployment,
            release_fingerprint=release_fingerprint,
            application_release_fingerprint=application_release,
            canary_evidence_fingerprint=canary_evidence,
            enablement_evidence_fingerprint=enablement_evidence,
            state_table=state_table,
        )
        for entrypoint in PHASE718_ENTRYPOINTS:
            verify_phase718_runtime_release(
                environment,
                expected_entrypoint=entrypoint,
                bundle_root=deployment,
            )
        _verify_current_repository_source_authority(deployment, binding=binding)

        artifact = _new_exact_directory(
            artifact_destination,
            ENABLED_ARTIFACT_DIRECTORY_NAME,
        )
        created.append(artifact)
        archive_path = artifact / ENABLED_ARCHIVE_FILENAME
        archive_bytes = render_deterministic_zip(deployment)
        _write_bytes(archive_path, archive_bytes)
        archive_fingerprint = sha256(archive_bytes).hexdigest()
        enabled_template_fingerprint = sha256(render_phase718_enabled_template()).hexdigest()
        descriptor = {
            "algorithm": "sha256",
            "application_release_fingerprint": application_release,
            "archive": {
                "path": ENABLED_ARCHIVE_FILENAME,
                "sha256": archive_fingerprint,
                "size_bytes": len(archive_bytes),
            },
            "architecture": "arm64",
            "binding_sha256": sha256(binding_raw).hexdigest(),
            "canary_evidence_fingerprint": canary_evidence,
            "component": _COMPONENT,
            "contract_fingerprint": PHASE718_CONTRACT_FINGERPRINT,
            "deployment_manifest_sha256": sha256(deployment_bytes).hexdigest(),
            "enabled_template_sha256": enabled_template_fingerprint,
            "enablement_evidence_fingerprint": enablement_evidence,
            "entrypoints": list(PHASE718_ENTRYPOINTS),
            "format": _DESCRIPTOR_FORMAT,
            "profile_fingerprint": PHASE718_PROFILE_FINGERPRINT,
            "release_fingerprint": release_fingerprint,
            "runtime": "python3.12",
            "s3_binding": _expected_s3_binding(),
            "source_manifest_sha256": sha256(
                cast(bytes, (deployment / PHASE718_SOURCE_MANIFEST_FILENAME).read_bytes())
            ).hexdigest(),
            "state_table": state_table,
        }
        descriptor_path = artifact / ENABLED_DESCRIPTOR_FILENAME
        _write_bytes(descriptor_path, render_manifest(descriptor))
        verify_enabled_deployment_artifact(
            deployment,
            archive_path=archive_path,
            descriptor_path=descriptor_path,
        )
        return Phase718EnabledArtifact(
            deployment_root=deployment,
            archive_path=archive_path,
            descriptor_path=descriptor_path,
            release_fingerprint=release_fingerprint,
            archive_fingerprint=archive_fingerprint,
            application_release_fingerprint=application_release,
            canary_evidence_fingerprint=canary_evidence,
            enablement_evidence_fingerprint=enablement_evidence,
            contract_fingerprint=PHASE718_CONTRACT_FINGERPRINT,
            profile_fingerprint=PHASE718_PROFILE_FINGERPRINT,
            binding_fingerprint=sha256(binding_raw).hexdigest(),
            enabled_template_fingerprint=enabled_template_fingerprint,
        )
    except Exception as error:
        for path in reversed(created):
            if path.exists():
                shutil.rmtree(path)
        if isinstance(error, Phase718EnabledReleaseError):
            raise
        raise Phase718EnabledReleaseError(_GENERIC_ERROR) from None


def verify_enabled_deployment_artifact(
    deployment_root: Path,
    *,
    archive_path: Path,
    descriptor_path: Path,
) -> Mapping[str, object]:
    """Verify the extracted release, deterministic ZIP, and immutable upload descriptor."""

    try:
        deployment = deployment_root.resolve(strict=True)
        descriptor_file = descriptor_path.resolve(strict=True)
        archive_file = archive_path.resolve(strict=True)
        if (
            descriptor_path.is_symlink()
            or not descriptor_file.is_file()
            or descriptor_file.name != ENABLED_DESCRIPTOR_FILENAME
            or archive_path.is_symlink()
            or not archive_file.is_file()
            or archive_file.name != ENABLED_ARCHIVE_FILENAME
        ):
            raise ValueError
        descriptor_raw, descriptor = _read_canonical(descriptor_file)
        del descriptor_raw
        _require_exact_keys(
            descriptor,
            {
                "algorithm",
                "application_release_fingerprint",
                "archive",
                "architecture",
                "binding_sha256",
                "canary_evidence_fingerprint",
                "component",
                "contract_fingerprint",
                "deployment_manifest_sha256",
                "enabled_template_sha256",
                "enablement_evidence_fingerprint",
                "entrypoints",
                "format",
                "profile_fingerprint",
                "release_fingerprint",
                "runtime",
                "s3_binding",
                "source_manifest_sha256",
                "state_table",
            },
        )
        release_fingerprint = _fingerprint(descriptor["release_fingerprint"])
        application_release = _fingerprint(descriptor["application_release_fingerprint"])
        canary_evidence = _fingerprint(descriptor["canary_evidence_fingerprint"])
        enablement_evidence = _fingerprint(descriptor["enablement_evidence_fingerprint"])
        state_table = cast(str, descriptor["state_table"])
        environment = _release_environment(
            deployment,
            release_fingerprint=release_fingerprint,
            application_release_fingerprint=application_release,
            canary_evidence_fingerprint=canary_evidence,
            enablement_evidence_fingerprint=enablement_evidence,
            state_table=state_table,
        )
        for entrypoint in PHASE718_ENTRYPOINTS:
            verified = verify_phase718_runtime_release(
                environment,
                expected_entrypoint=entrypoint,
                bundle_root=deployment,
            )
            if verified.entrypoint != entrypoint:
                raise ValueError
        _, binding = _read_canonical(deployment / PHASE718_BINDING_FILENAME)
        _verify_current_repository_source_authority(deployment, binding=binding)
        archive_record = descriptor["archive"]
        if not isinstance(archive_record, Mapping):
            raise ValueError
        _require_exact_keys(archive_record, {"path", "sha256", "size_bytes"})
        archive_raw = archive_file.read_bytes()
        expected_archive = render_deterministic_zip(deployment)
        if (
            descriptor["algorithm"] != "sha256"
            or descriptor["architecture"] != "arm64"
            or descriptor["component"] != _COMPONENT
            or descriptor["contract_fingerprint"] != PHASE718_CONTRACT_FINGERPRINT
            or descriptor["entrypoints"] != list(PHASE718_ENTRYPOINTS)
            or descriptor["format"] != _DESCRIPTOR_FORMAT
            or descriptor["profile_fingerprint"] != PHASE718_PROFILE_FINGERPRINT
            or descriptor["runtime"] != "python3.12"
            or descriptor["enabled_template_sha256"]
            != sha256(render_phase718_enabled_template()).hexdigest()
            or descriptor["binding_sha256"]
            != _file_fingerprint(deployment / PHASE718_BINDING_FILENAME)
            or descriptor["source_manifest_sha256"]
            != _file_fingerprint(deployment / PHASE718_SOURCE_MANIFEST_FILENAME)
            or descriptor["deployment_manifest_sha256"]
            != _file_fingerprint(deployment / PHASE718_DEPLOYMENT_MANIFEST_FILENAME)
            or archive_record["path"] != ENABLED_ARCHIVE_FILENAME
            or archive_record["sha256"] != sha256(archive_raw).hexdigest()
            or archive_record["size_bytes"] != len(archive_raw)
            or archive_raw != expected_archive
            or descriptor["s3_binding"] != _expected_s3_binding()
        ):
            raise ValueError
        _verify_archive_members(deployment, archive_raw)
        return descriptor
    except Exception:
        raise Phase718EnabledReleaseError(_GENERIC_ERROR) from None


def render_deterministic_zip(deployment_root: Path) -> bytes:
    """Render sorted, stored bytes with fixed metadata and no host information."""

    root = deployment_root.resolve(strict=True)
    output = BytesIO()
    with zipfile.ZipFile(
        output,
        "w",
        compression=zipfile.ZIP_STORED,
        strict_timestamps=True,
    ) as archive:
        for record in _inventory(root, excluded=frozenset()):
            relative = cast(str, record["path"])
            info = zipfile.ZipInfo(relative, date_time=_ZIP_TIMESTAMP)
            info.compress_type = zipfile.ZIP_STORED
            info.create_system = 3
            info.external_attr = 0o100644 << 16
            info.flag_bits = 0
            archive.writestr(info, (root / relative).read_bytes())
    return output.getvalue()


def _release_environment(
    root: Path,
    *,
    release_fingerprint: str,
    application_release_fingerprint: str,
    canary_evidence_fingerprint: str,
    enablement_evidence_fingerprint: str,
    state_table: str,
) -> dict[str, object]:
    return {
        "MR_LISTER_PHASE7_ENABLED_RELEASE_FINGERPRINT": release_fingerprint,
        "MR_LISTER_RELEASE_FINGERPRINT": application_release_fingerprint,
        "MR_LISTER_PHASE7_CONTRACT_FINGERPRINT": PHASE718_CONTRACT_FINGERPRINT,
        "MR_LISTER_PHASE7_CANARY_EVIDENCE_FINGERPRINT": canary_evidence_fingerprint,
        "MR_LISTER_PHASE7_ENABLEMENT_EVIDENCE_FINGERPRINT": enablement_evidence_fingerprint,
        "MR_LISTER_PRODUCT_PROFILE_FINGERPRINT": PHASE718_PROFILE_FINGERPRINT,
        "MR_LISTER_PRODUCT_PROFILE_PATH": (root / PHASE718_PROFILE_PATH).as_posix(),
        "MR_LISTER_STATE_TABLE": state_table,
    }


def _write_checked_dependency_request(root: Path, *, repository: Path) -> None:
    authority_path = repository / CHECKED_LAMBDA_WHEEL_AUTHORITY.relative_to(ROOT)
    raw = authority_path.read_bytes()
    value = json.loads(raw)
    if (
        authority_path.is_symlink()
        or not isinstance(value, Mapping)
        or render_manifest(cast(Mapping[str, object], value)) != raw
        or sha256(raw).hexdigest() != CHECKED_LAMBDA_WHEEL_AUTHORITY_SHA256
    ):
        raise ValueError
    authority = normalize_wheel_authority(value, component="lambda")
    wheels = cast(Sequence[Mapping[str, str]], authority["wheels"])
    if len(wheels) != 14:
        raise ValueError
    requirements = render_locked_requirements(authority)
    _write_bytes(root / "requirements.txt", requirements.encode("utf-8"))
    request = {
        "algorithm": "sha256",
        "component": "lambda",
        "format": LOCKED_BUILD_REQUEST_FORMAT,
        "requirements": {
            "dependency_tree_sha256": authority["dependency_tree_sha256"],
            "path": "requirements.txt",
            "required_distributions": [record["name"] for record in wheels],
            "sha256": _file_fingerprint(root / "requirements.txt"),
            "wheel_artifacts": [dict(record) for record in wheels],
        },
        "target": dict(LINUX_ARM64_TARGET),
    }
    _write_bytes(root / DEPENDENCY_BUILD_REQUEST_FILENAME, render_manifest(request))


def _verify_current_repository_source_authority(
    packaged_root: Path,
    *,
    binding: Mapping[str, object],
) -> None:
    root = packaged_root.resolve(strict=True)
    with TemporaryDirectory(prefix="mr-lister-phase718-current-source-") as temporary:
        current = build_enabled_source_bundle(
            Path(temporary) / ENABLED_SOURCE_DIRECTORY_NAME,
            application_release_fingerprint=_fingerprint(
                binding["application_release_fingerprint"]
            ),
            canary_evidence_fingerprint=_fingerprint(binding["canary_evidence_fingerprint"]),
            enablement_evidence_fingerprint=_fingerprint(
                binding["enablement_evidence_fingerprint"]
            ),
            state_table=cast(str, binding["state_table"]),
        )
        expected = _inventory(current, excluded=frozenset())
        actual: list[dict[str, object]] = []
        for record in expected:
            relative = cast(str, record["path"])
            target = root / relative
            if target.is_symlink() or not target.is_file():
                raise ValueError
            raw = target.read_bytes()
            actual.append(
                {
                    "path": relative,
                    "sha256": sha256(raw).hexdigest(),
                    "size_bytes": len(raw),
                }
            )
        if actual != expected:
            raise ValueError


def _verify_binding(binding: Mapping[str, object]) -> None:
    _require_exact_keys(
        binding,
        {
            "application_release_fingerprint",
            "canary_evidence_fingerprint",
            "contract_fingerprint",
            "contract_version",
            "enablement_evidence_fingerprint",
            "entrypoints",
            "format",
            "profile_fingerprint",
            "state_table",
        },
    )
    _validate_binding_inputs(
        application_release_fingerprint=_fingerprint(binding["application_release_fingerprint"]),
        canary_evidence_fingerprint=_fingerprint(binding["canary_evidence_fingerprint"]),
        enablement_evidence_fingerprint=_fingerprint(binding["enablement_evidence_fingerprint"]),
        state_table=cast(str, binding["state_table"]),
    )
    if (
        binding["contract_fingerprint"] != PHASE718_CONTRACT_FINGERPRINT
        or binding["contract_version"] != PHASE718_CONTRACT_VERSION
        or binding["entrypoints"] != list(PHASE718_ENTRYPOINTS)
        or binding["format"] != _BINDING_FORMAT
        or binding["profile_fingerprint"] != PHASE718_PROFILE_FINGERPRINT
    ):
        raise ValueError


def _verify_contract_and_profile(root: Path) -> None:
    contract_raw, contract = _read_canonical(root / PHASE718_CONTRACT_PATH)
    profile_path = root / PHASE718_PROFILE_PATH
    profile = json.loads(profile_path.read_bytes())
    if (
        sha256(contract_raw).hexdigest() != PHASE718_CONTRACT_FINGERPRINT
        or contract.get("contract_version") != PHASE718_CONTRACT_VERSION
        or contract.get("current_activation_phase") != "general_availability"
        or contract.get("publication_enabled") is not True
        or contract.get("phase6_runtime_unchanged") is not True
        or profile_path.is_symlink()
        or not profile_path.is_file()
        or sha256(profile_path.read_bytes()).hexdigest() != PHASE718_PROFILE_FILE_FINGERPRINT
        or not isinstance(profile, Mapping)
        or profile.get("publish_enabled") is not False
    ):
        raise ValueError


def _validate_binding_inputs(
    *,
    application_release_fingerprint: str,
    canary_evidence_fingerprint: str,
    enablement_evidence_fingerprint: str,
    state_table: str,
) -> None:
    for value in (
        application_release_fingerprint,
        canary_evidence_fingerprint,
        enablement_evidence_fingerprint,
    ):
        _fingerprint(value)
    prefix = "mr-lister-phase6-"
    suffix = state_table.removeprefix(prefix) if isinstance(state_table, str) else ""
    if (
        not isinstance(state_table, str)
        or not suffix
        or state_table != state_table.strip()
        or not state_table.startswith(prefix)
        or len(state_table) > 255
        or any(character not in "abcdefghijklmnopqrstuvwxyz0123456789-" for character in suffix)
    ):
        raise ValueError


def _assert_source_hygiene(root: Path, *, modules: Mapping[str, Path]) -> None:
    if (
        not {_ENTRYPOINT_MODULE, _RELEASE_MODULE}.issubset(modules)
        or _third_party_import_roots(modules) != _THIRD_PARTY_IMPORT_ROOTS
    ):
        raise ValueError
    for path in root.rglob("*"):
        relative = path.relative_to(root)
        if path.is_symlink() or any(part in _FORBIDDEN_PATH_PARTS for part in relative.parts):
            raise ValueError
        if path.is_file() and path.suffix.casefold() in _FORBIDDEN_SUFFIXES:
            raise ValueError
    for module in _CAPABILITY_FREE_INITIALIZERS:
        initializer = root / Path(*module.split(".")) / "__init__.py"
        if initializer.read_bytes() != b"":
            raise ValueError


def _module_path(source_root: Path, module: str) -> Path | None:
    relative = Path(*module.split("."))
    candidates = (source_root / f"{relative}.py", source_root / relative / "__init__.py")
    existing = [path for path in candidates if path.is_file() and not path.is_symlink()]
    if len(existing) > 1:
        raise ValueError
    return existing[0] if existing else None


def _parent_packages(module: str) -> tuple[str, ...]:
    parts = module.split(".")
    return tuple(".".join(parts[:index]) for index in range(1, len(parts)))


def _local_imports(source_root: Path, module: str, source: Path) -> set[str]:
    tree = ast.parse(source.read_text(encoding="utf-8"), filename=source.as_posix())
    package = module if source.name == "__init__.py" else module.rpartition(".")[0]
    imports: set[str] = set()
    for node in ast.walk(tree):
        candidates: list[str] = []
        if isinstance(node, ast.Import):
            candidates.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                relative = "." * node.level + (node.module or "")
                base = importlib.util.resolve_name(relative, package)
            else:
                base = node.module or ""
            candidates.append(base)
            candidates.extend(f"{base}.{alias.name}" for alias in node.names if alias.name != "*")
        for candidate in candidates:
            if candidate.startswith("mr_lister") and _module_path(source_root, candidate):
                imports.add(candidate)
    return imports


def _third_party_import_roots(modules: Mapping[str, Path]) -> tuple[str, ...]:
    roots: set[str] = set()
    for module, source in modules.items():
        if module in _CAPABILITY_FREE_INITIALIZERS:
            continue
        tree = ast.parse(source.read_text(encoding="utf-8"), filename=source.as_posix())
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                names.append(node.module)
            for name in names:
                root = name.partition(".")[0]
                if root not in {"__future__", "mr_lister"} and root not in sys.stdlib_module_names:
                    roots.add(root)
    return tuple(sorted(roots))


def _inventory(root: Path, *, excluded: frozenset[str]) -> list[dict[str, object]]:
    files: list[dict[str, object]] = []
    for path in sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix()):
        if path.is_symlink():
            raise ValueError
        if not path.is_file():
            continue
        relative = path.relative_to(root).as_posix()
        if relative.startswith("/") or ".." in Path(relative).parts:
            raise ValueError
        if relative in excluded:
            continue
        raw = path.read_bytes()
        files.append({"path": relative, "sha256": sha256(raw).hexdigest(), "size_bytes": len(raw)})
    return files


def _read_canonical(path: Path) -> tuple[bytes, Mapping[str, object]]:
    if path.is_symlink() or not path.is_file() or not 1 <= path.stat().st_size <= 4 << 20:
        raise ValueError
    raw = path.read_bytes()
    value = json.loads(raw)
    if not isinstance(value, Mapping) or render_manifest(cast(Mapping[str, object], value)) != raw:
        raise ValueError
    return raw, cast(Mapping[str, object], value)


def _expected_s3_binding() -> dict[str, object]:
    return {
        "archive_sha256_metadata_key": "mr-lister-archive-sha256",
        "bucket_parameter": "EnabledCodeS3Bucket",
        "head_object_version_must_match": True,
        "key_template": "phase7/releases/{release_fingerprint}/enabled.zip",
        "null_object_version_forbidden": True,
        "object_version_parameter": "EnabledCodeS3ObjectVersion",
        "object_version_required": True,
        "release_fingerprint_metadata_key": "mr-lister-release-fingerprint",
        "release_fingerprint_parameter": "EnabledReleaseFingerprint",
        "server_side_encryption": "AES256",
    }


def _verify_archive_members(deployment: Path, raw_archive: bytes) -> None:
    expected = _inventory(deployment, excluded=frozenset())
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


def _new_exact_directory(path: Path, expected_name: str) -> Path:
    destination = path.resolve(strict=False)
    if path.is_symlink() or destination.name != expected_name or destination.exists():
        raise ValueError
    destination.mkdir(mode=0o700, parents=True)
    return destination


def _copy_file(source: Path, destination: Path) -> None:
    if source.is_symlink() or not source.is_file():
        raise ValueError
    _write_bytes(destination, source.read_bytes())


def _copy_tree(source: Path, destination: Path) -> None:
    for path in sorted(source.rglob("*")):
        if path.is_symlink():
            raise ValueError
        if path.is_file():
            _copy_file(path, destination / path.relative_to(source))


def _overlay_dependency_tree(source: Path, destination: Path) -> None:
    for path in sorted(source.rglob("*")):
        if path.is_symlink():
            raise ValueError
        if not path.is_file():
            continue
        target = destination / path.relative_to(source)
        if target.exists():
            raise ValueError
        _copy_file(path, target)


def _write_bytes(destination: Path, value: bytes) -> None:
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    destination.write_bytes(value)
    destination.chmod(0o644)


def _file_fingerprint(path: Path) -> str:
    if path.is_symlink() or not path.is_file():
        raise ValueError
    return sha256(path.read_bytes()).hexdigest()


def _fingerprint(value: object) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or value == "0" * 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ValueError
    return value


def _require_exact_keys(value: Mapping[object, object], keys: set[str]) -> None:
    if set(value) != keys:
        raise ValueError


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-destination", type=Path, default=DEFAULT_SOURCE_DESTINATION)
    actions = parser.add_mutually_exclusive_group()
    actions.add_argument("--verify-source", type=Path)
    actions.add_argument("--build-dependencies-from-wheelhouse", type=Path)
    actions.add_argument("--write-dependency-manifest", type=Path)
    actions.add_argument("--verify-dependency-artifact", type=Path)
    actions.add_argument("--seal-source-release", type=Path)
    actions.add_argument("--verify-deployment", type=Path)
    parser.add_argument("--application-release-fingerprint")
    parser.add_argument("--canary-evidence-fingerprint")
    parser.add_argument("--enablement-evidence-fingerprint")
    parser.add_argument("--state-table")
    parser.add_argument("--build-request", type=Path)
    parser.add_argument("--dependencies", type=Path)
    parser.add_argument(
        "--dependency-destination",
        type=Path,
        default=DEFAULT_DEPENDENCY_DESTINATION,
    )
    parser.add_argument(
        "--deployment-destination",
        type=Path,
        default=DEFAULT_DEPLOYMENT_DESTINATION,
    )
    parser.add_argument("--artifact-destination", type=Path, default=DEFAULT_ARTIFACT_DESTINATION)
    parser.add_argument("--archive", type=Path)
    parser.add_argument("--descriptor", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    arguments = parser.parse_args(argv)
    try:
        if arguments.verify_source is not None:
            verify_enabled_source_bundle(arguments.verify_source)
        elif arguments.build_dependencies_from_wheelhouse is not None:
            if arguments.build_request is None:
                parser.error("--build-request is required")
            build_linux_arm64_dependencies_from_wheelhouse(
                arguments.build_dependencies_from_wheelhouse,
                destination=arguments.dependency_destination,
                build_request_path=arguments.build_request,
            )
        elif arguments.write_dependency_manifest is not None:
            if arguments.build_request is None:
                parser.error("--build-request is required")
            write_linux_arm64_dependency_manifest(
                arguments.write_dependency_manifest,
                build_request_path=arguments.build_request,
            )
        elif arguments.verify_dependency_artifact is not None:
            if arguments.build_request is None:
                parser.error("--build-request is required")
            verify_linux_arm64_dependency_artifact(
                arguments.verify_dependency_artifact,
                build_request_path=arguments.build_request,
            )
        elif arguments.seal_source_release is not None:
            if arguments.dependencies is None:
                parser.error("--dependencies is required")
            artifact = seal_enabled_release(
                arguments.seal_source_release,
                dependencies=arguments.dependencies,
                deployment_destination=arguments.deployment_destination,
                artifact_destination=arguments.artifact_destination,
            )
            print(artifact.release_fingerprint)
        elif arguments.verify_deployment is not None:
            if arguments.archive is None or arguments.descriptor is None:
                parser.error("--archive and --descriptor are required")
            verify_enabled_deployment_artifact(
                arguments.verify_deployment,
                archive_path=arguments.archive,
                descriptor_path=arguments.descriptor,
            )
        else:
            required = (
                arguments.application_release_fingerprint,
                arguments.canary_evidence_fingerprint,
                arguments.enablement_evidence_fingerprint,
                arguments.state_table,
            )
            if any(value is None for value in required):
                parser.error(
                    "source build requires application/canary/enablement fingerprints "
                    "and state table"
                )
            source = build_enabled_source_bundle(
                arguments.source_destination,
                application_release_fingerprint=arguments.application_release_fingerprint,
                canary_evidence_fingerprint=arguments.canary_evidence_fingerprint,
                enablement_evidence_fingerprint=arguments.enablement_evidence_fingerprint,
                state_table=arguments.state_table,
            )
            print(source.name)
        return 0
    except Phase718EnabledReleaseError:
        print(_GENERIC_ERROR, file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "ENABLED_ARCHIVE_FILENAME",
    "ENABLED_ARTIFACT_DIRECTORY_NAME",
    "ENABLED_DEPENDENCY_DIRECTORY_NAME",
    "ENABLED_DEPLOYMENT_DIRECTORY_NAME",
    "ENABLED_DESCRIPTOR_FILENAME",
    "ENABLED_SOURCE_DIRECTORY_NAME",
    "Phase718EnabledArtifact",
    "Phase718EnabledReleaseError",
    "build_enabled_source_bundle",
    "build_linux_arm64_dependencies_from_wheelhouse",
    "render_deterministic_zip",
    "resolve_enabled_import_closure",
    "seal_enabled_release",
    "verify_enabled_deployment_artifact",
    "verify_enabled_source_bundle",
    "write_linux_arm64_dependency_manifest",
]
