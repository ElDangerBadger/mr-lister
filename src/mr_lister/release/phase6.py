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

import base64
import csv
import json
import re
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from email.parser import Parser
from hashlib import sha256
from io import StringIO
from pathlib import Path, PurePosixPath
from typing import Any, Literal, cast

RELEASE_MANIFEST_FILENAME = "release-manifest.json"
DEPLOYMENT_MANIFEST_FILENAME = "deployment-manifest.json"
DEPENDENCY_BUILD_REQUEST_FILENAME = "dependency-build-request.json"
DEPENDENCY_ARTIFACT_FILENAME = "dependency-artifact.json"
SOURCE_MANIFEST_FILENAME = "source-manifest.json"

LEGACY_BUILD_REQUEST_FORMAT = "phase6-dependency-build-request-v1"
LOCKED_BUILD_REQUEST_FORMAT = "phase6-dependency-build-request-v2"
LEGACY_DEPENDENCY_FORMAT = "phase6-linux-arm64-dependencies-v1"
LOCKED_DEPENDENCY_FORMAT = "phase6-linux-arm64-dependencies-v2"
WHEEL_AUTHORITY_FORMAT = "phase6-wheel-authority-v1"

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
_BASELINE_ARM64_PLATFORMS = frozenset({"manylinux2014_aarch64", "manylinux_2_17_aarch64"})
_REQUIRED_NATIVE_PATHS = {
    "awscrt": re.compile(r"^_awscrt(?:\.[A-Za-z0-9_-]+)*\.so$"),
    "pillow": re.compile(r"^PIL/_imaging(?:\.[A-Za-z0-9_-]+)*\.so$"),
    "pydantic-core": re.compile(r"^pydantic_core/_pydantic_core(?:\.[A-Za-z0-9_-]+)*\.so$"),
}
_MAX_MANIFEST_BYTES = 4 * 1024 * 1024
_GENERIC_ERROR = "Phase 6 release authority is invalid"
_IMPORT_TIME_HOOK_MODULES = frozenset({"sitecustomize", "usercustomize", "site"})
_UNSUPPORTED_WHEEL_DATA_SCHEMES = frozenset({"platlib", "purelib"})
_STDLIB_TOP_LEVEL = frozenset(sys.stdlib_module_names)
_REQUIRED_DISTRIBUTIONS = {
    "lambda": frozenset({"awscrt", "boto3", "botocore", "pillow", "pydantic"}),
    "agentcore": frozenset(
        {
            "awscrt",
            "bedrock-agentcore",
            "boto3",
            "botocore",
            "fastapi",
            "pillow",
            "pydantic",
            "strands-agents",
            "uvicorn",
        }
    ),
}


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


def normalize_wheel_authority(
    value: Mapping[str, object],
    *,
    component: Literal["agentcore", "lambda"],
) -> dict[str, object]:
    """Validate and canonicalize one externally reviewed wheel/tree authority.

    The authority is deliberately data rather than a package resolver.  A controlled
    capture must supply exact wheel filenames, versions, SHA256 values, and the SHA256
    of the deterministically extracted tree.  This module never discovers or updates
    those values from a package index.
    """

    try:
        if not isinstance(value, Mapping):
            raise ValueError
        _require_exact_keys(
            value,
            {"algorithm", "component", "dependency_tree_sha256", "format", "target", "wheels"},
        )
        if (
            value["algorithm"] != "sha256"
            or value["component"] != component
            or value["format"] != WHEEL_AUTHORITY_FORMAT
            or value["target"] != LINUX_ARM64_TARGET
        ):
            raise ValueError
        dependency_tree = _nonzero_fingerprint(value["dependency_tree_sha256"])
        wheel_value = value["wheels"]
        if not isinstance(wheel_value, list) or not wheel_value:
            raise ValueError
        wheels: list[dict[str, str]] = []
        prior_name = ""
        filenames: set[str] = set()
        for item in wheel_value:
            if not isinstance(item, Mapping):
                raise ValueError
            _require_exact_keys(item, {"filename", "name", "sha256", "version"})
            name = _normalize_distribution(_exact_string(item["name"]))
            version = _exact_string(item["version"])
            filename = _exact_string(item["filename"])
            fingerprint = _nonzero_fingerprint(item["sha256"])
            if (
                _VERSION.fullmatch(version) is None
                or name <= prior_name
                or filename.casefold() in filenames
                or not filename.isascii()
                or filename != Path(filename).name
                or not filename.endswith(".whl")
            ):
                raise ValueError
            prior_name = name
            filenames.add(filename.casefold())
            wheels.append(
                {
                    "filename": filename,
                    "name": name,
                    "sha256": fingerprint,
                    "version": version,
                }
            )
        names = {wheel["name"] for wheel in wheels}
        if not _REQUIRED_DISTRIBUTIONS[component].issubset(names):
            raise ValueError
        return {
            "algorithm": "sha256",
            "component": component,
            "dependency_tree_sha256": dependency_tree,
            "format": WHEEL_AUTHORITY_FORMAT,
            "target": dict(LINUX_ARM64_TARGET),
            "wheels": wheels,
        }
    except Phase6ReleaseAuthorityError:
        raise
    except Exception:
        raise Phase6ReleaseAuthorityError(_GENERIC_ERROR) from None


def render_locked_requirements(authority: Mapping[str, object]) -> str:
    """Render the exact hash-locked requirements represented by an authority."""

    try:
        component = authority.get("component") if isinstance(authority, Mapping) else None
        if component not in {"agentcore", "lambda"}:
            raise ValueError
        normalized = normalize_wheel_authority(
            authority,
            component=cast(Literal["agentcore", "lambda"], component),
        )
        wheels = cast(Sequence[Mapping[str, str]], normalized["wheels"])
        return "".join(
            f"{wheel['name']}=={wheel['version']} --hash=sha256:{wheel['sha256']}\n"
            for wheel in wheels
        )
    except Phase6ReleaseAuthorityError:
        raise
    except Exception:
        raise Phase6ReleaseAuthorityError(_GENERIC_ERROR) from None


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
        verified_dependency = verify_linux_arm64_dependency_artifact(
            root,
            build_request_path=root / DEPENDENCY_BUILD_REQUEST_FILENAME,
            allow_extra_files=True,
        )
        if verified_dependency.get("format") != LOCKED_DEPENDENCY_FORMAT:
            raise ValueError
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
    allow_packaged_source: bool = False,
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
        if allow_packaged_source:
            _source_bytes, source_manifest = _read_canonical_manifest(
                artifact_root / SOURCE_MANIFEST_FILENAME
            )
            source_records = source_manifest.get("files")
            if not isinstance(source_records, list):
                raise ValueError
            source_paths = {
                cast(str, record.get("path"))
                for record in source_records
                if isinstance(record, Mapping) and isinstance(record.get("path"), str)
            }
            if len(source_paths) != len(source_records):
                raise ValueError
            files = [
                record
                for record in files
                if record["path"] not in source_paths
                and record["path"]
                not in {
                    SOURCE_MANIFEST_FILENAME,
                    DEPLOYMENT_MANIFEST_FILENAME,
                    RELEASE_MANIFEST_FILENAME,
                }
            ]
        if not files:
            raise ValueError
        distributions = _inspect_distributions(artifact_root)
        requirements = cast(Mapping[str, object], request["requirements"])
        required = requirements["required_distributions"]
        installed = {cast(str, record["name"]) for record in distributions}
        runtime_required = set(required) if isinstance(required, list) else set()
        component = cast(str, request["component"])
        if request["format"] == LOCKED_BUILD_REQUEST_FORMAT:
            wheels = _locked_wheel_records(requirements)
            expected_installed = {
                (cast(str, wheel["name"]), cast(str, wheel["version"])) for wheel in wheels
            }
            actual_installed = {
                (cast(str, record["name"]), cast(str, record["version"]))
                for record in distributions
            }
            tree_fingerprint = _dependency_tree_fingerprint(files)
            if (
                required != sorted(installed)
                or actual_installed != expected_installed
                or tree_fingerprint != requirements["dependency_tree_sha256"]
            ):
                raise ValueError
            record_owners = _verify_record_owned_dependency_files(
                artifact_root,
                distributions,
                files,
            )
            dependency_format = LOCKED_DEPENDENCY_FORMAT
        else:
            if "pydantic" in runtime_required:
                runtime_required.add("pydantic-core")
            if not isinstance(required, list) or not runtime_required.issubset(installed):
                raise ValueError
            wheels = []
            record_owners = {}
            tree_fingerprint = _dependency_tree_fingerprint(files)
            dependency_format = LEGACY_DEPENDENCY_FORMAT
        if not _REQUIRED_DISTRIBUTIONS[component].issubset(installed):
            raise ValueError
        native_files = _inspect_native_files(artifact_root, files)
        if record_owners:
            _require_native_file_ownership(
                distributions=distributions,
                native_files=native_files,
                owners=record_owners,
            )
        _require_native_runtime_files(
            distributions=distributions,
            native_files=native_files,
        )
        result: dict[str, object] = {
            "algorithm": "sha256",
            "build_request_sha256": sha256(request_bytes).hexdigest(),
            "dependency_tree_sha256": tree_fingerprint,
            "distributions": distributions,
            "files": files,
            "format": dependency_format,
            "target": dict(LINUX_ARM64_TARGET),
        }
        if wheels:
            result["wheel_artifacts"] = [dict(wheel) for wheel in wheels]
        return result
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


def wheel_authority_from_build_request(path: Path) -> dict[str, object]:
    """Recover the exact reviewed authority embedded in one locked v2 request."""

    try:
        request = verify_dependency_build_request(path)
        if request["format"] != LOCKED_BUILD_REQUEST_FORMAT:
            raise ValueError
        component = request["component"]
        requirements = request["requirements"]
        if component not in {"agentcore", "lambda"} or not isinstance(requirements, Mapping):
            raise ValueError
        return normalize_wheel_authority(
            {
                "algorithm": "sha256",
                "component": component,
                "dependency_tree_sha256": requirements["dependency_tree_sha256"],
                "format": WHEEL_AUTHORITY_FORMAT,
                "target": dict(LINUX_ARM64_TARGET),
                "wheels": requirements["wheel_artifacts"],
            },
            component=cast(Literal["agentcore", "lambda"], component),
        )
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
        _manifest_bytes, manifest = _read_canonical_manifest(
            artifact_root / DEPENDENCY_ARTIFACT_FILENAME
        )
        expected = inspect_linux_arm64_dependency_artifact(
            artifact_root,
            build_request_path=build_request_path,
            allow_packaged_source=allow_extra_files,
        )
        if manifest != expected:
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
    component = request["component"]
    if (
        request["algorithm"] != "sha256"
        or component not in {"agentcore", "lambda"}
        or request["format"] not in {LEGACY_BUILD_REQUEST_FORMAT, LOCKED_BUILD_REQUEST_FORMAT}
        or request["target"] != LINUX_ARM64_TARGET
    ):
        raise ValueError
    requirements = request["requirements"]
    if not isinstance(requirements, Mapping):
        raise ValueError
    locked = request["format"] == LOCKED_BUILD_REQUEST_FORMAT
    keys = {"path", "required_distributions", "sha256"}
    if locked:
        keys.update({"dependency_tree_sha256", "wheel_artifacts"})
    _require_exact_keys(requirements, keys)
    if requirements["path"] != "requirements.txt":
        raise ValueError
    expected_fingerprint = _manifest_fingerprint(requirements["sha256"])
    requirements_path = request_root / "requirements.txt"
    if requirements_path.is_symlink() or not requirements_path.is_file():
        raise ValueError
    raw = requirements_path.read_bytes()
    if not raw or len(raw) > 64 * 1024 or sha256(raw).hexdigest() != expected_fingerprint:
        raise ValueError
    decoded = raw.decode("utf-8")
    if locked:
        wheels = _locked_wheel_records(requirements)
        authority = normalize_wheel_authority(
            {
                "algorithm": "sha256",
                "component": component,
                "dependency_tree_sha256": requirements["dependency_tree_sha256"],
                "format": WHEEL_AUTHORITY_FORMAT,
                "target": dict(LINUX_ARM64_TARGET),
                "wheels": [dict(record) for record in wheels],
            },
            component=cast(Literal["agentcore", "lambda"], component),
        )
        if decoded != render_locked_requirements(authority) or requirements[
            "required_distributions"
        ] != [cast(str, record["name"]) for record in wheels]:
            raise ValueError
    else:
        names = _requirement_names(decoded)
        if requirements["required_distributions"] != names:
            raise ValueError


def _locked_wheel_records(requirements: Mapping[str, object]) -> list[Mapping[str, object]]:
    tree_fingerprint = _nonzero_fingerprint(requirements.get("dependency_tree_sha256"))
    del tree_fingerprint
    value = requirements.get("wheel_artifacts")
    if not isinstance(value, list) or not value:
        raise ValueError
    records: list[Mapping[str, object]] = []
    for item in value:
        if not isinstance(item, Mapping):
            raise ValueError
        _require_exact_keys(item, {"filename", "name", "sha256", "version"})
        records.append(item)
    return records


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


def _dependency_tree_fingerprint(files: Sequence[Mapping[str, object]]) -> str:
    return sha256(render_manifest({"files": [dict(record) for record in files]})).hexdigest()


def _verify_record_owned_dependency_files(
    root: Path,
    distributions: Sequence[Mapping[str, object]],
    files: Sequence[Mapping[str, object]],
) -> Mapping[str, str]:
    """Require every dependency byte to be owned by exactly one wheel RECORD."""

    expected_paths = {cast(str, record["path"]) for record in files}
    if len(expected_paths) != len(files):
        raise ValueError
    owners: dict[str, str] = {}
    for distribution in distributions:
        name = cast(str, distribution["name"])
        dist_info = cast(str, distribution["dist_info"])
        record_relative = f"{dist_info}/RECORD"
        record_path = root / record_relative
        if (
            record_relative not in expected_paths
            or record_path.is_symlink()
            or not record_path.is_file()
        ):
            raise ValueError
        raw = record_path.read_bytes()
        if not raw or len(raw) > _MAX_MANIFEST_BYTES or b"\x00" in raw:
            raise ValueError
        rows = csv.reader(StringIO(raw.decode("utf-8"), newline=""), strict=True)
        saw_self = False
        for row in rows:
            if len(row) != 3:
                raise ValueError
            relative, encoded_hash, encoded_size = row
            _require_safe_relative(relative)
            _reject_import_time_dependency_path(relative)
            if relative not in expected_paths or relative in owners:
                raise ValueError
            target = root / relative
            if target.is_symlink() or not target.is_file():
                raise ValueError
            if relative == record_relative:
                if encoded_hash or encoded_size or saw_self:
                    raise ValueError
                saw_self = True
            else:
                fingerprint, size = _file_fingerprint_and_size(target)
                encoded = base64.urlsafe_b64encode(bytes.fromhex(fingerprint)).decode("ascii")
                expected_hash = encoded.rstrip("=")
                if (
                    encoded_hash != f"sha256={expected_hash}"
                    or not encoded_size.isascii()
                    or not encoded_size.isdecimal()
                    or encoded_size != str(size)
                ):
                    raise ValueError
            owners[relative] = name
        if not saw_self:
            raise ValueError
    if set(owners) != expected_paths:
        raise ValueError
    return owners


def _reject_import_time_dependency_path(relative: str) -> None:
    path = PurePosixPath(relative)
    filename = path.name.casefold()
    top_level = path.parts[0].casefold()
    if top_level.endswith(".data"):
        # Raw wheel extraction intentionally does not implement the wheel install
        # relocation scheme.  Code placed under purelib/platlib would therefore be
        # absent from its declared runtime location.  Other schemes remain inert in
        # their namespaced .data directory; notably jmespath's scripts/jp.py is not
        # placed on sys.path or PATH by this builder.
        if len(path.parts) > 1 and path.parts[1].casefold() in _UNSUPPORTED_WHEEL_DATA_SCHEMES:
            raise ValueError
        return
    if top_level.endswith(".dist-info"):
        return
    module_name = top_level
    for suffix in (".py", ".pyc", ".so"):
        if module_name.endswith(suffix):
            module_name = module_name[: -len(suffix)]
            break
    module_name = module_name.split(".", 1)[0]
    # ``site`` imports sitecustomize/usercustomize by module name, so both a
    # top-level module and a same-named package can execute during interpreter
    # startup.  Nested same-named modules are ordinary package implementation
    # files, such as OpenTelemetry's auto-instrumentation sitecustomize.py.
    if (
        module_name in _IMPORT_TIME_HOOK_MODULES
        or module_name in _STDLIB_TOP_LEVEL
        or (len(path.parts) == 1 and filename.endswith((".pth", ".egg-link")))
    ):
        raise ValueError


def _inspect_distributions(root: Path) -> list[dict[str, object]]:
    records: list[dict[str, object]] = []
    for path in sorted(root.glob("*.dist-info")):
        if path.is_symlink() or not path.is_dir() or _DIST_INFO.fullmatch(path.name) is None:
            raise ValueError
        metadata = _parse_headers(path / "METADATA")
        wheel = _parse_headers(path / "WHEEL")
        name = _normalize_distribution(_one_header(metadata, "Name"))
        version = _one_header(metadata, "Version")
        expected_dist_info = (
            f"{_wheel_filename_component(name)}-{_wheel_filename_component(version)}.dist-info"
        )
        if _VERSION.fullmatch(version) is None or path.name != expected_dist_info:
            raise ValueError
        tags = sorted(set(wheel.get_all("Tag", [])))
        if (
            not tags
            or any(not _valid_wheel_tag(tag) for tag in tags)
            or not any(_python312_compatible_wheel_tag(tag) for tag in tags)
        ):
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
        value in {"py2-none-any", "py2.py3-none-any", "py3-none-any"}
        or _NATIVE_ARM64_TAG.fullmatch(value) is not None
    )


def _python312_compatible_wheel_tag(value: object) -> bool:
    if not isinstance(value, str):
        return False
    if value in {"py2.py3-none-any", "py3-none-any"}:
        return True
    if _NATIVE_ARM64_TAG.fullmatch(value) is None:
        return False
    platforms = value.rsplit("-", 1)[-1].split(".")
    return bool(_BASELINE_ARM64_PLATFORMS.intersection(platforms))


def _inspect_native_files(
    root: Path,
    records: Sequence[Mapping[str, object]],
) -> frozenset[str]:
    native_files: set[str] = set()
    for record in records:
        relative = cast(str, record["path"])
        lowered = relative.casefold()
        if lowered.endswith((".dylib", ".dll", ".pyd", ".whl", ".zip", ".pyc")):
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
            _python312_compatible_wheel_tag(tag)
            and isinstance(tag, str)
            and tag not in {"py2.py3-none-any", "py3-none-any"}
            for tag in tags
        ):
            raise ValueError
        if not any(path_pattern.fullmatch(path) is not None for path in native_files):
            raise ValueError


def _require_native_file_ownership(
    *,
    distributions: Sequence[Mapping[str, object]],
    native_files: frozenset[str],
    owners: Mapping[str, str],
) -> None:
    by_name = {cast(str, record["name"]): record for record in distributions}
    for relative in native_files:
        owner = owners.get(relative)
        record = by_name.get(owner) if owner is not None else None
        tags = record.get("tags") if record is not None else None
        if not isinstance(tags, list) or not any(
            _python312_compatible_wheel_tag(tag)
            and isinstance(tag, str)
            and tag not in {"py2.py3-none-any", "py3-none-any"}
            for tag in tags
        ):
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


def _wheel_filename_component(value: str) -> str:
    normalized = re.sub(r"[^A-Za-z0-9_.]+", "_", value)
    if not normalized or not normalized.isascii():
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


def _nonzero_fingerprint(value: object) -> str:
    fingerprint = _manifest_fingerprint(value)
    if fingerprint == "0" * 64:
        raise ValueError
    return fingerprint


def _exact_string(value: object) -> str:
    if not isinstance(value, str) or not value or value != value.strip() or not value.isascii():
        raise ValueError
    return value


def _required_fingerprint(environment: Mapping[str, object], name: str) -> str:
    return _nonzero_fingerprint(environment.get(name))


def _require_exact_keys(value: Mapping[object, object], keys: set[str]) -> None:
    if set(value) != keys:
        raise ValueError


__all__ = [
    "DEPENDENCY_ARTIFACT_FILENAME",
    "DEPENDENCY_BUILD_REQUEST_FILENAME",
    "DEPLOYMENT_MANIFEST_FILENAME",
    "LINUX_ARM64_TARGET",
    "LEGACY_BUILD_REQUEST_FORMAT",
    "LEGACY_DEPENDENCY_FORMAT",
    "LOCKED_BUILD_REQUEST_FORMAT",
    "LOCKED_DEPENDENCY_FORMAT",
    "RELEASE_MANIFEST_FILENAME",
    "SOURCE_MANIFEST_FILENAME",
    "WHEEL_AUTHORITY_FORMAT",
    "Phase6ReleaseAuthorityError",
    "Phase6ReleaseBinding",
    "inspect_linux_arm64_dependency_artifact",
    "normalize_wheel_authority",
    "render_manifest",
    "render_locked_requirements",
    "verify_dependency_build_request",
    "verify_linux_arm64_dependency_artifact",
    "verify_phase6_packaged_release",
    "wheel_authority_from_build_request",
]
