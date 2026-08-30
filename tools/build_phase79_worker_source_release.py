"""Build and verify the triggerless Phase 7.9 publication-worker source checkpoint.

This is deliberately not a Lambda release. It seals the current local import closure, checked
draft-safe profile, disabled activation tuple, and reviewed dependency baseline into one
deterministic source ZIP. It emits no runtime entrypoint or handler binding, trigger, SAM/IAM
resource, S3 binding, production credential resolver, dependency bytes, or live AWS operation.
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
from typing import cast

from mr_lister.release.phase7 import (
    GUARD_PROFILE_FINGERPRINT,
    LINUX_ARM64_TARGET,
    PINNED_GUARD_DEPENDENCY_TREE_FINGERPRINT,
    PINNED_GUARD_WHEELS,
    inventory,
    render_manifest,
)
from mr_lister.review_profile import FilesystemReviewProductAuthority

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SOURCE_DESTINATION = ROOT / ".mr_lister_private" / "phase7-worker-source"
DEFAULT_ARCHIVE_DESTINATION = ROOT / ".mr_lister_private" / "phase7-worker-offline.zip"

WORKER_COMPOSITION_ROOT = "mr_lister.cloud.phase7_worker_composition"
WORKER_SOURCE_MANIFEST_FILENAME = "worker-source-manifest.json"
WORKER_PROFILE_RELATIVE_PATH = Path("config/product_profiles/gildan_64000_swiftpod.json")
WORKER_PROFILE_ID = "gildan_64000_swiftpod"
WORKER_PROFILE_VERSION = 2

_COMPONENT = "phase7-worker-triggerless-source"
_FORMAT = "phase7-worker-triggerless-source-v1"
_ARTIFACT_KIND = "offline_source_oracle"
_CONTRACT_VERSION = "7.0.1"
_CONTRACT_FINGERPRINT = "548b710230618e73c20a509f2121799c415b50070e1e2ae7e1b82fe3c37e2981"
_CAPABILITY_FREE_INITIALIZERS = frozenset(
    {
        "mr_lister",
        "mr_lister.cloud",
        "mr_lister.control",
        "mr_lister.publication",
        "mr_lister.workflow",
    }
)
_REQUIRED_THIRD_PARTY_IMPORT_ROOTS = ("PIL", "botocore", "pydantic")
_FORBIDDEN_MODULE_PREFIXES = (
    "mr_lister.agent",
    "mr_lister.api",
    "mr_lister.cloud.phase6",
    "mr_lister.cloud.phase7_composition",
    "mr_lister.cloud.phase7_entrypoints",
    "mr_lister.cloud.phase7_guard_entrypoint",
    "mr_lister.cloud.phase7_provider_credentials",
    "mr_lister.production",
    "mr_lister.release",
    "mr_lister.workflow.artifacts",
    "mr_lister.workflow.ports",
    "mr_lister.workflow.service",
    "mr_lister.workflow.store",
)
_FORBIDDEN_PATH_PARTS = frozenset(
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
_MANIFEST_KEYS = {
    "activation",
    "algorithm",
    "artifact_kind",
    "component",
    "composition_roots",
    "contract",
    "dependencies",
    "deployable",
    "files",
    "format",
    "profile",
}
_ACTIVATION = {
    "publication_enabled": False,
    "query_enabled": False,
    "request_enabled": False,
    "scaffold_only": True,
}
_GENERIC_ERROR = "Phase 7 worker source release is invalid"
_ZIP_TIMESTAMP = (1980, 1, 1, 0, 0, 0)


class Phase79WorkerSourceReleaseError(RuntimeError):
    """Value-free failure for unsafe, drifting, or non-triggerless source authority."""


@dataclass(frozen=True, slots=True)
class Phase79WorkerSourceArtifact:
    source_root: Path
    archive_path: Path
    manifest_fingerprint: str
    archive_fingerprint: str
    profile_fingerprint: str


def build_worker_source_release(
    destination: Path = DEFAULT_SOURCE_DESTINATION,
    *,
    archive_path: Path = DEFAULT_ARCHIVE_DESTINATION,
    repository_root: Path = ROOT,
) -> Phase79WorkerSourceArtifact:
    """Create one deterministic, source-only, explicitly non-deployable worker checkpoint."""

    created_root: Path | None = None
    created_archive: Path | None = None
    try:
        repository = repository_root.resolve(strict=True)
        source_root = _new_directory(destination)
        created_root = source_root
        archive = _new_file_destination(archive_path)
        modules = resolve_worker_import_closure(repository)
        for module, source in modules.items():
            relative = source.relative_to(repository / "src")
            raw = b"" if module in _CAPABILITY_FREE_INITIALIZERS else source.read_bytes()
            _write_bytes(source_root / relative, raw)

        profile_source = repository / WORKER_PROFILE_RELATIVE_PATH
        authority = FilesystemReviewProductAuthority(
            profile_directory=profile_source.parent
        ).get_exact(
            profile_id=WORKER_PROFILE_ID,
            profile_version=WORKER_PROFILE_VERSION,
        )
        if (
            authority.profile.publish_enabled is not False
            or authority.fingerprint != GUARD_PROFILE_FINGERPRINT
        ):
            raise ValueError
        _write_bytes(source_root / WORKER_PROFILE_RELATIVE_PATH, profile_source.read_bytes())
        _assert_source_hygiene(source_root, modules=modules)

        manifest = _expected_manifest(
            source_root,
            profile_fingerprint=authority.fingerprint,
            third_party_import_roots=_third_party_import_roots(modules),
        )
        manifest_path = source_root / WORKER_SOURCE_MANIFEST_FILENAME
        _write_bytes(manifest_path, render_manifest(manifest))
        archive_bytes = render_worker_source_zip(source_root)
        _write_bytes(archive, archive_bytes)
        created_archive = archive
        binding = verify_worker_source_release(
            source_root,
            archive_path=archive,
            repository_root=repository,
        )
        return Phase79WorkerSourceArtifact(
            source_root=source_root,
            archive_path=archive,
            manifest_fingerprint=binding.manifest_fingerprint,
            archive_fingerprint=binding.archive_fingerprint,
            profile_fingerprint=binding.profile_fingerprint,
        )
    except Exception as error:
        if created_archive is not None and created_archive.exists():
            created_archive.unlink()
        if created_root is not None and created_root.exists():
            shutil.rmtree(created_root)
        if isinstance(error, Phase79WorkerSourceReleaseError):
            raise
        raise Phase79WorkerSourceReleaseError(_GENERIC_ERROR) from None


def verify_worker_source_release(
    source_root: Path,
    *,
    archive_path: Path,
    repository_root: Path = ROOT,
) -> Phase79WorkerSourceArtifact:
    """Verify canonical authority, current-checkout bytes, and deterministic archive identity."""

    try:
        repository = repository_root.resolve(strict=True)
        root = source_root.resolve(strict=True)
        if source_root.is_symlink() or not root.is_dir():
            raise ValueError
        manifest_path = root / WORKER_SOURCE_MANIFEST_FILENAME
        if manifest_path.is_symlink() or not manifest_path.is_file():
            raise ValueError
        manifest_raw = manifest_path.read_bytes()
        value = json.loads(manifest_raw)
        if (
            not isinstance(value, Mapping)
            or render_manifest(cast(Mapping[str, object], value)) != manifest_raw
            or set(value) != _MANIFEST_KEYS
        ):
            raise ValueError
        manifest = cast(Mapping[str, object], value)
        if (
            manifest["algorithm"] != "sha256"
            or manifest["artifact_kind"] != _ARTIFACT_KIND
            or manifest["component"] != _COMPONENT
            or manifest["composition_roots"] != [WORKER_COMPOSITION_ROOT]
            or manifest["format"] != _FORMAT
            or manifest["deployable"] is not False
            or manifest["activation"] != _ACTIVATION
            or manifest["contract"] != _contract_authority()
            or manifest["dependencies"] != _dependency_authority()
        ):
            raise ValueError

        expected_inventory = inventory(
            root,
            excluded=frozenset({WORKER_SOURCE_MANIFEST_FILENAME}),
        )
        if manifest["files"] != expected_inventory:
            raise ValueError
        modules = resolve_worker_import_closure(repository)
        expected_paths = sorted(
            [source.relative_to(repository / "src").as_posix() for source in modules.values()]
            + [WORKER_PROFILE_RELATIVE_PATH.as_posix()]
        )
        actual_paths = [
            cast(str, record["path"])
            for record in cast(Sequence[Mapping[str, object]], expected_inventory)
        ]
        if actual_paths != expected_paths:
            raise ValueError
        for module, source in modules.items():
            relative = source.relative_to(repository / "src")
            expected = b"" if module in _CAPABILITY_FREE_INITIALIZERS else source.read_bytes()
            packaged = root / relative
            if packaged.is_symlink() or not packaged.is_file() or packaged.read_bytes() != expected:
                raise ValueError

        profile = manifest["profile"]
        if not isinstance(profile, Mapping) or profile != _profile_authority():
            raise ValueError
        profile_path = root / WORKER_PROFILE_RELATIVE_PATH
        exact = FilesystemReviewProductAuthority(profile_directory=profile_path.parent).get_exact(
            profile_id=WORKER_PROFILE_ID,
            profile_version=WORKER_PROFILE_VERSION,
        )
        if (
            exact.fingerprint != GUARD_PROFILE_FINGERPRINT
            or exact.profile.publish_enabled is not False
        ):
            raise ValueError
        _assert_source_hygiene(root, modules=modules)
        if _third_party_import_roots(modules) != _REQUIRED_THIRD_PARTY_IMPORT_ROOTS:
            raise ValueError

        archive = archive_path.resolve(strict=True)
        if archive_path.is_symlink() or not archive.is_file():
            raise ValueError
        archive_raw = archive.read_bytes()
        if archive_raw != render_worker_source_zip(root):
            raise ValueError
        return Phase79WorkerSourceArtifact(
            source_root=root,
            archive_path=archive,
            manifest_fingerprint=sha256(manifest_raw).hexdigest(),
            archive_fingerprint=sha256(archive_raw).hexdigest(),
            profile_fingerprint=exact.fingerprint,
        )
    except Phase79WorkerSourceReleaseError:
        raise
    except Exception:
        raise Phase79WorkerSourceReleaseError(_GENERIC_ERROR) from None


def resolve_worker_import_closure(repository_root: Path = ROOT) -> dict[str, Path]:
    """Resolve the exact local import closure rooted at the worker composition oracle."""

    try:
        repository = repository_root.resolve(strict=True)
        source_root = repository / "src"
        queue: deque[str] = deque([WORKER_COMPOSITION_ROOT])
        resolved: dict[str, Path] = {}
        while queue:
            module = queue.popleft()
            if module in resolved:
                continue
            _reject_module(module)
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
            WORKER_COMPOSITION_ROOT not in resolved
            or "mr_lister.publication.provider_runtime" not in resolved
            or "mr_lister.cloud.phase7_configuration" not in resolved
        ):
            raise ValueError
        return dict(sorted(resolved.items()))
    except Phase79WorkerSourceReleaseError:
        raise
    except Exception:
        raise Phase79WorkerSourceReleaseError(_GENERIC_ERROR) from None


def render_worker_source_zip(source_root: Path) -> bytes:
    """Render sorted, uncompressed source bytes with fixed cross-run ZIP metadata."""

    root = source_root.resolve(strict=True)
    output = BytesIO()
    with zipfile.ZipFile(
        output,
        "w",
        compression=zipfile.ZIP_STORED,
        strict_timestamps=True,
    ) as archive:
        for record in inventory(root, excluded=frozenset()):
            relative = cast(str, record["path"])
            info = zipfile.ZipInfo(relative, date_time=_ZIP_TIMESTAMP)
            info.compress_type = zipfile.ZIP_STORED
            info.create_system = 3
            info.external_attr = 0o100644 << 16
            info.flag_bits = 0
            archive.writestr(info, (root / relative).read_bytes())
    return output.getvalue()


def _expected_manifest(
    root: Path,
    *,
    profile_fingerprint: str,
    third_party_import_roots: tuple[str, ...],
) -> dict[str, object]:
    if (
        profile_fingerprint != GUARD_PROFILE_FINGERPRINT
        or third_party_import_roots != _REQUIRED_THIRD_PARTY_IMPORT_ROOTS
    ):
        raise ValueError
    return {
        "activation": dict(_ACTIVATION),
        "algorithm": "sha256",
        "artifact_kind": _ARTIFACT_KIND,
        "component": _COMPONENT,
        "composition_roots": [WORKER_COMPOSITION_ROOT],
        "contract": _contract_authority(),
        "dependencies": _dependency_authority(),
        "deployable": False,
        "files": inventory(root, excluded=frozenset({WORKER_SOURCE_MANIFEST_FILENAME})),
        "format": _FORMAT,
        "profile": _profile_authority(),
    }


def _contract_authority() -> dict[str, object]:
    return {
        "fingerprint": _CONTRACT_FINGERPRINT,
        "version": _CONTRACT_VERSION,
    }


def _profile_authority() -> dict[str, object]:
    return {
        "fingerprint": GUARD_PROFILE_FINGERPRINT,
        "path": WORKER_PROFILE_RELATIVE_PATH.as_posix(),
        "profile_id": WORKER_PROFILE_ID,
        "profile_version": WORKER_PROFILE_VERSION,
        "publish_enabled": False,
    }


def _dependency_authority() -> dict[str, object]:
    return {
        "additional_unsealed_distributions": [
            {"import_root": "PIL", "name": "Pillow"},
        ],
        "required_import_roots": list(_REQUIRED_THIRD_PARTY_IMPORT_ROOTS),
        "reviewed_guard_baseline": {
            "distributions": [
                {
                    "filename": filename,
                    "name": name,
                    "sha256": fingerprint,
                    "version": version,
                }
                for name, version, filename, fingerprint in PINNED_GUARD_WHEELS
            ],
            "tree_sha256": PINNED_GUARD_DEPENDENCY_TREE_FINGERPRINT,
        },
        "runtime_bytes_included": False,
        "target": dict(LINUX_ARM64_TARGET),
    }


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


def _parsed(source: Path) -> ast.Module:
    return ast.parse(source.read_text(encoding="utf-8"), filename=source.as_posix())


def _import_candidates(module: str, source: Path) -> list[str]:
    package = module if source.name == "__init__.py" else module.rpartition(".")[0]
    candidates: list[str] = []
    for node in ast.walk(_parsed(source)):
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
    return candidates


def _local_imports(source_root: Path, module: str, source: Path) -> set[str]:
    imports: set[str] = set()
    for candidate in _import_candidates(module, source):
        if not candidate.startswith("mr_lister"):
            continue
        _reject_module(candidate)
        if _module_path(source_root, candidate) is not None:
            imports.add(candidate)
    return imports


def _third_party_import_roots(modules: Mapping[str, Path]) -> tuple[str, ...]:
    imports: set[str] = set()
    for module, source in modules.items():
        if module in _CAPABILITY_FREE_INITIALIZERS:
            continue
        for candidate in _import_candidates(module, source):
            root = candidate.partition(".")[0]
            if (
                root
                and root != "mr_lister"
                and root != "__future__"
                and root not in sys.stdlib_module_names
            ):
                imports.add(root)
    return tuple(sorted(imports))


def _reject_module(module: str) -> None:
    if any(
        module == forbidden or module.startswith(f"{forbidden}.")
        for forbidden in _FORBIDDEN_MODULE_PREFIXES
    ) or "browser" in module.split("."):
        raise ValueError


def _assert_source_hygiene(root: Path, *, modules: Mapping[str, Path]) -> None:
    if any(
        module == forbidden or module.startswith(f"{forbidden}.")
        for module in modules
        for forbidden in _FORBIDDEN_MODULE_PREFIXES
    ):
        raise ValueError
    for path in root.rglob("*"):
        relative = path.relative_to(root)
        if path.is_symlink() or any(part in _FORBIDDEN_PATH_PARTS for part in relative.parts):
            raise ValueError
        if path.is_file() and path.suffix not in {".json", ".py"}:
            raise ValueError
    for module in _CAPABILITY_FREE_INITIALIZERS:
        source = _module_path(root, module)
        if source is None or source.read_bytes() != b"":
            raise ValueError


def _new_directory(destination: Path) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)
    parent = destination.parent.resolve(strict=True)
    target = parent / destination.name
    if destination.is_symlink() or target.exists():
        raise ValueError
    target.mkdir(mode=0o700)
    return target


def _new_file_destination(destination: Path) -> Path:
    parent = destination.parent.resolve(strict=True)
    target = parent / destination.name
    if destination.is_symlink() or target.exists():
        raise ValueError
    return target


def _write_bytes(path: Path, raw: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink() or path.exists():
        raise ValueError
    path.write_bytes(raw)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE_DESTINATION)
    parser.add_argument("--archive", type=Path, default=DEFAULT_ARCHIVE_DESTINATION)
    parser.add_argument("--verify", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    artifact = (
        verify_worker_source_release(arguments.source, archive_path=arguments.archive)
        if arguments.verify
        else build_worker_source_release(arguments.source, archive_path=arguments.archive)
    )
    print(
        json.dumps(
            {
                "archive_sha256": artifact.archive_fingerprint,
                "manifest_sha256": artifact.manifest_fingerprint,
                "profile_sha256": artifact.profile_fingerprint,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "DEFAULT_ARCHIVE_DESTINATION",
    "DEFAULT_SOURCE_DESTINATION",
    "WORKER_COMPOSITION_ROOT",
    "WORKER_SOURCE_MANIFEST_FILENAME",
    "Phase79WorkerSourceArtifact",
    "Phase79WorkerSourceReleaseError",
    "build_worker_source_release",
    "render_worker_source_zip",
    "resolve_worker_import_closure",
    "verify_worker_source_release",
]
