"""Fail-closed authority for a packaged Phase 6 release.

The source builder is intentionally not a dependency builder.  A Linux ARM64 build
environment must populate an installed dependency tree and then ask this module to
inspect it.  Only a subsequently sealed deployment directory can satisfy
``verify_phase6_packaged_release``.

All operations are local and deterministic.  This module never resolves packages,
opens a network connection, calls AWS, or treats the developer workstation as a Linux
artifact authority.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from email.parser import Parser
from hashlib import sha256
from pathlib import Path, PurePosixPath
from typing import Any, Literal, cast

RELEASE_MANIFEST_FILENAME = "release-manifest.json"
DEPLOYMENT_MANIFEST_FILENAME = "deployment-manifest.json"
DEPENDENCY_BUILD_REQUEST_FILENAME = "dependency-build-request.json"
DEPENDENCY_ARTIFACT_FILENAME = "dependency-artifact.json"
SOURCE_MANIFEST_FILENAME = "source-manifest.json"

LINUX_ARM64_TARGET = {
    "architecture": "arm64",
    "implementation": "cpython",
    "platform": "manylinux2014_aarch64",
    "python_abi": "cp312",
    "python_version": "3.12",
}

_FINGERPRINT = re.compile(r"^[a-f0-9]{64}$")
_NORMALIZED_DISTRIBUTION = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_VERSION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.!+_-]{0,127}$")
_DIST_INFO = re.compile(r"^[A-Za-z0-9_.+-]+\.dist-info$")
_REQUIREMENT = re.compile(
    r"^(?P<name>[A-Za-z0-9][A-Za-z0-9._-]*)(?:\[(?P<extras>[A-Za-z0-9_,.-]+)\])?"
    r"(?:(?:===|==|!=|~=|>=|<=|>|<)[A-Za-z0-9][A-Za-z0-9.*+!_-]*"
    r"(?:,(?:===|==|!=|~=|>=|<=|>|<)[A-Za-z0-9][A-Za-z0-9.*+!_-]*)*)?$"
)
_NATIVE_ARM64_PLATFORM = r"(?:manylinux(?:2014|_2_17|_2_[0-9]+)|linux)_aarch64"
_NATIVE_ARM64_TAG = re.compile(
    r"^(?:cp312-cp312|cp3(?:[89]|1[0-2])-abi3|cp312-none|py3-none)-"
    + _NATIVE_ARM64_PLATFORM
    + rf"(?:\.{_NATIVE_ARM64_PLATFORM})*$"
)
_REQUIRED_NATIVE_PATHS = {
    "awscrt": re.compile(r"^_awscrt(?:\.[A-Za-z0-9_-]+)*\.so$"),
    "pillow": re.compile(r"^PIL/_imaging(?:\.[A-Za-z0-9_-]+)*\.so$"),
    "pydantic-core": re.compile(r"^pydantic_core/_pydantic_core(?:\.[A-Za-z0-9_-]+)*\.so$"),
}
_MAX_MANIFEST_BYTES = 4 * 1024 * 1024
_GENERIC_ERROR = "Phase 6 release authority is invalid"


class Phase6ReleaseAuthorityError(RuntimeError):
    """A value-free failure for malformed, drifting, or unsealed release bytes."""


@dataclass(frozen=True, slots=True)
class Phase6ReleaseBinding:
    """The verified local identity of one component in the sealed release."""

    component: Literal["agentcore", "lambda"]
    release_fingerprint: str
    deployment_manifest_fingerprint: str
    source_manifest_fingerprint: str
    dependency_manifest_fingerprint: str


def render_manifest(value: Mapping[str, object]) -> bytes:
    """Render the one canonical JSON representation used by all release manifests."""

    return (
        json.dumps(value, allow_nan=False, indent=2, sort_keys=True, separators=(",", ": ")) + "\n"
    ).encode("utf-8")


def verify_phase6_packaged_release(
    environment: Mapping[str, object],
    *,
    component: Literal["agentcore", "lambda"],
    bundle_root: Path | None = None,
) -> Phase6ReleaseBinding:
    """Bind an environment fingerprint to every byte in one sealed deployment tree."""

    try:
        if not isinstance(environment, Mapping):
            raise ValueError
        expected_release = _required_fingerprint(environment, "MR_LISTER_RELEASE_FINGERPRINT")
        root = _exact_directory(bundle_root or Path(__file__).resolve().parents[2])
        release_path = root / RELEASE_MANIFEST_FILENAME
        release_bytes, release = _read_canonical_manifest(release_path)
        release_fingerprint = sha256(release_bytes).hexdigest()
        if release_fingerprint != expected_release:
            raise ValueError
        _require_exact_keys(
            release,
            {"algorithm", "components", "format", "target"},
        )
        if (
            release["algorithm"] != "sha256"
            or release["format"] != "phase6-release-v1"
            or release["target"] != LINUX_ARM64_TARGET
        ):
            raise ValueError
        components = release["components"]
        if not isinstance(components, Mapping) or set(components) != {"agentcore", "lambda"}:
            raise ValueError
        record = components.get(component)
        if not isinstance(record, Mapping):
            raise ValueError
        _require_exact_keys(
            record,
            {
                "dependency_manifest_sha256",
                "deployment_manifest_sha256",
                "source_manifest_sha256",
            },
        )
        dependency_fingerprint = _manifest_fingerprint(record["dependency_manifest_sha256"])
        deployment_fingerprint = _manifest_fingerprint(record["deployment_manifest_sha256"])
        source_fingerprint = _manifest_fingerprint(record["source_manifest_sha256"])

        deployment_bytes, deployment = _read_canonical_manifest(root / DEPLOYMENT_MANIFEST_FILENAME)
        if sha256(deployment_bytes).hexdigest() != deployment_fingerprint:
            raise ValueError
        _verify_deployment_manifest(root, deployment, component=component)

        source_bytes, _source = _read_canonical_manifest(root / SOURCE_MANIFEST_FILENAME)
        dependency_bytes, _dependency = _read_canonical_manifest(
            root / DEPENDENCY_ARTIFACT_FILENAME
        )
        if (
            sha256(source_bytes).hexdigest() != source_fingerprint
            or sha256(dependency_bytes).hexdigest() != dependency_fingerprint
        ):
            raise ValueError
        verify_linux_arm64_dependency_artifact(
            root,
            build_request_path=root / DEPENDENCY_BUILD_REQUEST_FILENAME,
            allow_extra_files=True,
        )
        _verify_source_records(root, _source)
        return Phase6ReleaseBinding(
            component=component,
            release_fingerprint=release_fingerprint,
            deployment_manifest_fingerprint=deployment_fingerprint,
            source_manifest_fingerprint=source_fingerprint,
            dependency_manifest_fingerprint=dependency_fingerprint,
        )
    except Phase6ReleaseAuthorityError:
        raise
    except Exception:
        raise Phase6ReleaseAuthorityError(_GENERIC_ERROR) from None


def inspect_linux_arm64_dependency_artifact(
    root: Path,
    *,
    build_request_path: Path,
) -> dict[str, object]:
    """Inspect an installed dependency tree and return its deterministic manifest.

    The caller must run this inside (or against output from) the controlled Linux ARM64
    dependency build.  Platform metadata and native objects are inspected here; this
    function never claims that the current host produced the tree.
    """

    try:
        artifact_root = _exact_directory(root)
        request_bytes, request = _read_canonical_manifest(build_request_path)
        _verify_build_request(request, request_root=build_request_path.parent)
        files = _inventory(
            artifact_root,
            excluded=frozenset({DEPENDENCY_ARTIFACT_FILENAME}),
        )
        if not files:
            raise ValueError
        distributions = _inspect_distributions(artifact_root)
        required = cast(Mapping[str, object], request["requirements"])["required_distributions"]
        installed = {record["name"] for record in distributions}
        runtime_required = set(required) if isinstance(required, list) else set()
        if "pydantic" in runtime_required:
            runtime_required.add("pydantic-core")
        if not isinstance(required, list) or not runtime_required.issubset(installed):
            raise ValueError
        native_files = _inspect_native_files(artifact_root, files)
        _require_native_runtime_files(
            distributions=distributions,
            native_files=native_files,
        )
        return {
            "algorithm": "sha256",
            "build_request_sha256": sha256(request_bytes).hexdigest(),
            "distributions": distributions,
            "files": files,
            "format": "phase6-linux-arm64-dependencies-v1",
            "target": dict(LINUX_ARM64_TARGET),
        }
    except Phase6ReleaseAuthorityError:
        raise
    except Exception:
        raise Phase6ReleaseAuthorityError(_GENERIC_ERROR) from None


def verify_dependency_build_request(path: Path) -> Mapping[str, object]:
    """Verify one canonical source-stage request for a Linux ARM64 dependency build."""

    try:
        _raw, request = _read_canonical_manifest(path)
        _verify_build_request(request, request_root=path.parent)
        return request
    except Phase6ReleaseAuthorityError:
        raise
    except Exception:
        raise Phase6ReleaseAuthorityError(_GENERIC_ERROR) from None


def verify_linux_arm64_dependency_artifact(
    root: Path,
    *,
    build_request_path: Path,
    allow_extra_files: bool = False,
) -> Mapping[str, object]:
    """Verify an inspected dependency tree without resolving or installing anything."""

    try:
        artifact_root = _exact_directory(root)
        manifest_bytes, manifest = _read_canonical_manifest(
            artifact_root / DEPENDENCY_ARTIFACT_FILENAME
        )
        del manifest_bytes
        expected = inspect_linux_arm64_dependency_artifact(
            artifact_root,
            build_request_path=build_request_path,
        )
        if allow_extra_files:
            # A sealed deployment overlays the source tree.  Dependency records must
            # still name exact bytes, but source files are intentionally additional.
            _require_exact_keys(
                manifest,
                {
                    "algorithm",
                    "build_request_sha256",
                    "distributions",
                    "files",
                    "format",
                    "target",
                },
            )
            if any(manifest.get(key) != expected.get(key) for key in expected if key != "files"):
                raise ValueError
            _verify_inventory_records(artifact_root, manifest.get("files"))
        elif manifest != expected:
            raise ValueError
        return manifest
    except Phase6ReleaseAuthorityError:
        raise
    except Exception:
        raise Phase6ReleaseAuthorityError(_GENERIC_ERROR) from None


def _verify_deployment_manifest(
    root: Path,
    manifest: Mapping[str, object],
    *,
    component: str,
) -> None:
    _require_exact_keys(manifest, {"algorithm", "component", "files", "format", "target"})
    if (
        manifest["algorithm"] != "sha256"
        or manifest["component"] != component
        or manifest["format"] != "phase6-deployment-v1"
        or manifest["target"] != LINUX_ARM64_TARGET
    ):
        raise ValueError
    expected = _inventory(
        root,
        excluded=frozenset({DEPLOYMENT_MANIFEST_FILENAME, RELEASE_MANIFEST_FILENAME}),
    )
    if manifest["files"] != expected:
        raise ValueError


def _verify_source_records(root: Path, manifest: Mapping[str, object]) -> None:
    _require_exact_keys(manifest, {"algorithm", "files", "format"})
    if manifest["algorithm"] != "sha256" or manifest["format"] != "phase6-source-v1":
        raise ValueError
    _verify_inventory_records(root, manifest["files"])


def _verify_build_request(request: Mapping[str, object], *, request_root: Path) -> None:
    _require_exact_keys(request, {"algorithm", "component", "format", "requirements", "target"})
    if (
        request["algorithm"] != "sha256"
        or request["component"] not in {"agentcore", "lambda"}
        or request["format"] != "phase6-dependency-build-request-v1"
        or request["target"] != LINUX_ARM64_TARGET
    ):
        raise ValueError
    requirements = request["requirements"]
    if not isinstance(requirements, Mapping):
        raise ValueError
    _require_exact_keys(requirements, {"path", "required_distributions", "sha256"})
    if requirements["path"] != "requirements.txt":
        raise ValueError
    expected_fingerprint = _manifest_fingerprint(requirements["sha256"])
    requirements_path = request_root / "requirements.txt"
    if requirements_path.is_symlink() or not requirements_path.is_file():
        raise ValueError
    raw = requirements_path.read_bytes()
    if not raw or len(raw) > 64 * 1024 or sha256(raw).hexdigest() != expected_fingerprint:
        raise ValueError
    names = _requirement_names(raw.decode("utf-8"))
    if requirements["required_distributions"] != names:
        raise ValueError


def _requirement_names(value: str) -> list[str]:
    names: set[str] = set()
    for line in value.splitlines():
        text = line.strip()
        if not text or text.startswith("#"):
            continue
        if text.startswith(("-", ".", "/")) or ";" in text or " @ " in text:
            raise ValueError
        match = _REQUIREMENT.match(text)
        if match is None:
            raise ValueError
        name = _normalize_distribution(match.group("name"))
        names.add(name)
        extras = {extra.casefold() for extra in (match.group("extras") or "").split(",") if extra}
        if name == "botocore" and "crt" in extras:
            names.add("awscrt")
    if not names:
        raise ValueError
    return sorted(names)


def _inspect_distributions(root: Path) -> list[dict[str, object]]:
    records: list[dict[str, object]] = []
    for path in sorted(root.glob("*.dist-info")):
        if path.is_symlink() or not path.is_dir() or _DIST_INFO.fullmatch(path.name) is None:
            raise ValueError
        metadata = _parse_headers(path / "METADATA")
        wheel = _parse_headers(path / "WHEEL")
        name = _normalize_distribution(_one_header(metadata, "Name"))
        version = _one_header(metadata, "Version")
        if _VERSION.fullmatch(version) is None:
            raise ValueError
        tags = sorted(set(wheel.get_all("Tag", [])))
        if not tags or any(not _valid_wheel_tag(tag) for tag in tags):
            raise ValueError
        records.append(
            {
                "dist_info": path.name,
                "name": name,
                "tags": tags,
                "version": version,
            }
        )
    if not records or len({record["name"] for record in records}) != len(records):
        raise ValueError
    if records != sorted(records, key=lambda record: cast(str, record["name"])):
        records.sort(key=lambda record: cast(str, record["name"]))
    return records


def _parse_headers(path: Path) -> Any:
    if path.is_symlink() or not path.is_file() or not 1 <= path.stat().st_size <= 1024 * 1024:
        raise ValueError
    raw = path.read_text(encoding="utf-8")
    return Parser().parsestr(raw, headersonly=True)


def _one_header(headers: Any, name: str) -> str:
    values = headers.get_all(name, [])
    if len(values) != 1 or not isinstance(values[0], str) or values[0] != values[0].strip():
        raise ValueError
    return cast(str, values[0])


def _valid_wheel_tag(value: object) -> bool:
    return isinstance(value, str) and (
        value == "py3-none-any" or _NATIVE_ARM64_TAG.fullmatch(value) is not None
    )


def _inspect_native_files(
    root: Path,
    records: Sequence[Mapping[str, object]],
) -> frozenset[str]:
    native_files: set[str] = set()
    for record in records:
        relative = cast(str, record["path"])
        lowered = relative.casefold()
        if lowered.endswith((".dylib", ".dll", ".pyd", ".whl", ".zip")):
            raise ValueError
        path = root / relative
        with path.open("rb") as stream:
            header = stream.read(64)
        if header[:4] != b"\x7fELF" and ".so" not in Path(relative).name:
            continue
        if (
            path.stat().st_size < 4_096
            or len(header) < 64
            or header[:4] != b"\x7fELF"
            or header[4] != 2
            or header[5] != 1
            or header[6] != 1
            or int.from_bytes(header[16:18], "little") != 3
            or int.from_bytes(header[18:20], "little") != 183
            or int.from_bytes(header[20:24], "little") != 1
            or int.from_bytes(header[52:54], "little") != 64
        ):
            raise ValueError
        native_files.add(relative)
    return frozenset(native_files)


def _require_native_runtime_files(
    *,
    distributions: Sequence[Mapping[str, object]],
    native_files: frozenset[str],
) -> None:
    by_name = {cast(str, record["name"]): record for record in distributions}
    for distribution, path_pattern in _REQUIRED_NATIVE_PATHS.items():
        record = by_name.get(distribution)
        if record is None:
            raise ValueError
        tags = record.get("tags")
        if not isinstance(tags, list) or not any(
            isinstance(tag, str) and _NATIVE_ARM64_TAG.fullmatch(tag) is not None for tag in tags
        ):
            raise ValueError
        if not any(path_pattern.fullmatch(path) is not None for path in native_files):
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


def _inventory(root: Path, *, excluded: frozenset[str]) -> list[dict[str, object]]:
    files: list[dict[str, object]] = []
    paths = sorted(root.rglob("*"), key=lambda path: path.relative_to(root).as_posix())
    for path in paths:
        if path.is_symlink():
            raise ValueError
        if not path.is_file():
            continue
        relative = path.relative_to(root).as_posix()
        _require_safe_relative(relative)
        if relative in excluded:
            continue
        fingerprint, size = _file_fingerprint_and_size(path)
        files.append(
            {
                "path": relative,
                "sha256": fingerprint,
                "size_bytes": size,
            }
        )
    return files


def _verify_inventory_records(root: Path, value: object) -> None:
    if not isinstance(value, list) or not value:
        raise ValueError
    prior = ""
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, Mapping):
            raise ValueError
        _require_exact_keys(item, {"path", "sha256", "size_bytes"})
        relative = item["path"]
        if not isinstance(relative, str):
            raise ValueError
        _require_safe_relative(relative)
        if relative <= prior or relative in seen:
            raise ValueError
        prior = relative
        seen.add(relative)
        expected = _manifest_fingerprint(item["sha256"])
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
        fingerprint, actual_size = _file_fingerprint_and_size(path)
        if actual_size != size or fingerprint != expected:
            raise ValueError


def _file_fingerprint_and_size(path: Path) -> tuple[str, int]:
    digest = sha256()
    size = 0
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            size += len(chunk)
            digest.update(chunk)
    return digest.hexdigest(), size


def _require_safe_relative(value: str) -> None:
    path = PurePosixPath(value)
    if (
        not value
        or not value.isascii()
        or "\\" in value
        or path.is_absolute()
        or path.as_posix() != value
        or any(part in {"", ".", ".."} for part in path.parts)
        or any(part in {".git", "__pycache__"} for part in path.parts)
    ):
        raise ValueError


def _normalize_distribution(value: str) -> str:
    normalized = re.sub(r"[-_.]+", "-", value).casefold()
    if _NORMALIZED_DISTRIBUTION.fullmatch(normalized) is None:
        raise ValueError
    return normalized


def _exact_directory(path: Path) -> Path:
    resolved = path.resolve(strict=True)
    if path.is_symlink() or not resolved.is_dir():
        raise ValueError
    return resolved


def _manifest_fingerprint(value: object) -> str:
    if not isinstance(value, str) or _FINGERPRINT.fullmatch(value) is None:
        raise ValueError
    return value


def _required_fingerprint(environment: Mapping[str, object], name: str) -> str:
    value = _manifest_fingerprint(environment.get(name))
    if value == "0" * 64:
        raise ValueError
    return value


def _require_exact_keys(value: Mapping[object, object], keys: set[str]) -> None:
    if set(value) != keys:
        raise ValueError


__all__ = [
    "DEPENDENCY_ARTIFACT_FILENAME",
    "DEPENDENCY_BUILD_REQUEST_FILENAME",
    "DEPLOYMENT_MANIFEST_FILENAME",
    "LINUX_ARM64_TARGET",
    "RELEASE_MANIFEST_FILENAME",
    "SOURCE_MANIFEST_FILENAME",
    "Phase6ReleaseAuthorityError",
    "Phase6ReleaseBinding",
    "inspect_linux_arm64_dependency_artifact",
    "render_manifest",
    "verify_dependency_build_request",
    "verify_linux_arm64_dependency_artifact",
    "verify_phase6_packaged_release",
]
