"""Fail-closed authority for the sealed Phase 7 read-only guard release.

The guard release is intentionally independent from the Phase 6 deployment release.  It contains
one Linux ARM64 Lambda artifact, has no provider credential or mutation capability, and binds its
runtime environment to every packaged byte through canonical manifests.  This module only reads
local files; it never resolves dependencies, opens a network connection, or constructs an AWS
client.
"""

from __future__ import annotations

import base64
import csv
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from email.parser import Parser
from hashlib import sha256
from io import StringIO
from pathlib import Path, PurePosixPath
from typing import Any, cast

GUARD_ENTRYPOINT = "mr_lister.cloud.phase7_guard_entrypoint.publication_guard_verification_handler"
GUARD_RELEASE_FINGERPRINT_ENV = "MR_LISTER_PHASE7_GUARD_RELEASE_FINGERPRINT"
SHARED_RELEASE_FINGERPRINT_ENV = "MR_LISTER_RELEASE_FINGERPRINT"

SOURCE_MANIFEST_FILENAME = "source-manifest.json"
DEPENDENCY_BUILD_REQUEST_FILENAME = "dependency-build-request.json"
DEPENDENCY_ARTIFACT_FILENAME = "dependency-artifact.json"
DEPLOYMENT_MANIFEST_FILENAME = "deployment-manifest.json"
RELEASE_MANIFEST_FILENAME = "release-manifest.json"

CAPABILITY_FREE_CLOUD_INIT_PATH = "mr_lister/cloud/__init__.py"
CAPABILITY_FREE_CLOUD_INIT_BYTES = b""
CAPABILITY_FREE_PACKAGE_INIT_PATHS = (
    "mr_lister/__init__.py",
    CAPABILITY_FREE_CLOUD_INIT_PATH,
    "mr_lister/release/__init__.py",
)
GUARD_PROFILE_FINGERPRINT = "5de1257141cfdacb1731f68bb9113712957483b33d5b0f7115afdba86eb7476c"

# This is the complete wheel authority accepted by the guard runtime.  The hashes are the exact
# PyPI wheel artifacts selected for CPython 3.12/Linux ARM64; sdists and alternate wheels are not
# authority.  The extracted-tree fingerprint below independently binds all 2,310 installed paths.
PINNED_GUARD_WHEELS = (
    (
        "annotated-types",
        "0.8.0",
        "annotated_types-0.8.0-py3-none-any.whl",
        "f072f4d804ea359e4eaf198b1af7a8b0943881a87f31bb764f8bf219bb9419e0",
    ),
    (
        "boto3",
        "1.43.73",
        "boto3-1.43.73-py3-none-any.whl",
        "5b54da301c387abe30c5b3a5335652f1ebd73e814c725ba805d27a2d477ce547",
    ),
    (
        "botocore",
        "1.43.73",
        "botocore-1.43.73-py3-none-any.whl",
        "068433028e011ccbeab1dd7c46b1090c24e378397693c66e67ca571176498daa",
    ),
    (
        "jmespath",
        "1.1.0",
        "jmespath-1.1.0-py3-none-any.whl",
        "a5663118de4908c91729bea0acadca56526eb2698e83de10cd116ae0f4e97c64",
    ),
    (
        "pydantic",
        "2.13.4",
        "pydantic-2.13.4-py3-none-any.whl",
        "45a282cde31d808236fd7ea9d919b128653c8b38b393d1c4ab335c62924d9aba",
    ),
    (
        "pydantic-core",
        "2.46.4",
        "pydantic_core-2.46.4-cp312-cp312-manylinux_2_17_aarch64.manylinux2014_aarch64.whl",
        "8233f2947cf85404441fd7e0085f53b10c93e0ee78611099b5c7237e36aacbf7",
    ),
    (
        "python-dateutil",
        "2.9.0.post0",
        "python_dateutil-2.9.0.post0-py2.py3-none-any.whl",
        "a8b2bc7bffae282281c8140a97d3aa9c14da0b136dfe83f850eea9a5f7470427",
    ),
    (
        "s3transfer",
        "0.19.2",
        "s3transfer-0.19.2-py3-none-any.whl",
        "d8168eccca828cbb2cd573675333f3bddd254313a9c42494b84c76b539e8ba25",
    ),
    (
        "six",
        "1.17.0",
        "six-1.17.0-py2.py3-none-any.whl",
        "4721f391ed90541fddacab5acf947aa0d3dc7d27b2e1e8eda2be8970586c3274",
    ),
    (
        "typing-extensions",
        "4.16.0",
        "typing_extensions-4.16.0-py3-none-any.whl",
        "481caa481374e813c1b176ada14e97f1f67a4539ce9cfeb3f350d78d6370c2e8",
    ),
    (
        "typing-inspection",
        "0.4.4",
        "typing_inspection-0.4.4-py3-none-any.whl",
        "65b8397ba37ccbce054456aaccddfc91e6e3083c92824df348d96ca832f3f147",
    ),
    (
        "urllib3",
        "2.7.0",
        "urllib3-2.7.0-py3-none-any.whl",
        "9fb4c81ebbb1ce9531cce37674bbc6f1360472bc18ca9a553ede278ef7276897",
    ),
)
PINNED_GUARD_DISTRIBUTIONS = tuple(
    (name, version) for name, version, _filename, _fingerprint in PINNED_GUARD_WHEELS
)
PINNED_GUARD_REQUIREMENTS = "".join(
    f"{name}=={version} --hash=sha256:{fingerprint}\n"
    for name, version, _filename, fingerprint in PINNED_GUARD_WHEELS
)
PINNED_GUARD_DEPENDENCY_TREE_FINGERPRINT = (
    "8a5f828d9de52400eac64fc6b3092a6d4290cea07407908e3724af0c94308e9a"
)
GUARD_SOURCE_PATHS = (
    "config/product_profiles/gildan_64000_swiftpod.json",
    DEPENDENCY_BUILD_REQUEST_FILENAME,
    "mr_lister/__init__.py",
    "mr_lister/cloud/__init__.py",
    "mr_lister/cloud/phase7_guard_composition.py",
    "mr_lister/cloud/phase7_guard_entrypoint.py",
    "mr_lister/contracts/__init__.py",
    "mr_lister/contracts/models.py",
    "mr_lister/contracts/presentation.py",
    "mr_lister/control/__init__.py",
    "mr_lister/control/economics.py",
    "mr_lister/control/fingerprints.py",
    "mr_lister/control/models.py",
    "mr_lister/publication/__init__.py",
    "mr_lister/publication/contract.py",
    "mr_lister/publication/errors.py",
    "mr_lister/publication/evidence_provenance.py",
    "mr_lister/publication/execution_fingerprints.py",
    "mr_lister/publication/execution_models.py",
    "mr_lister/publication/fingerprints.py",
    "mr_lister/publication/guard_verification.py",
    "mr_lister/publication/models.py",
    "mr_lister/publication/profile_eligibility.py",
    "mr_lister/publication/retention_locator.py",
    "mr_lister/release/__init__.py",
    "mr_lister/release/phase7.py",
    "mr_lister/review_profile.py",
    "requirements.txt",
)

LINUX_ARM64_TARGET = {
    "architecture": "arm64",
    "implementation": "cpython",
    "platform": "manylinux2014_aarch64",
    "python_abi": "cp312",
    "python_version": "3.12",
}

_COMPONENT = "phase7-guard-lambda"
_SOURCE_FORMAT = "phase7-guard-source-v1"
_BUILD_REQUEST_FORMAT = "phase7-guard-dependency-build-request-v1"
_DEPENDENCY_FORMAT = "phase7-guard-linux-arm64-dependencies-v2"
_DEPLOYMENT_FORMAT = "phase7-guard-deployment-v1"
_RELEASE_FORMAT = "phase7-guard-release-v1"

_FINGERPRINT = re.compile(r"^[a-f0-9]{64}$")
_NORMALIZED_DISTRIBUTION = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_VERSION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.!+_-]{0,127}$")
_DIST_INFO = re.compile(r"^[A-Za-z0-9_.+-]+\.dist-info$")
_NATIVE_ARM64_PLATFORM = r"(?:manylinux(?:2014|_2_17|_2_[0-9]+)|linux)_aarch64"
_NATIVE_ARM64_TAG = re.compile(
    r"^(?:cp312-cp312|cp3(?:[89]|1[0-2])-abi3|cp312-none|py3-none)-"
    + _NATIVE_ARM64_PLATFORM
    + rf"(?:\.{_NATIVE_ARM64_PLATFORM})*$"
)
_PYDANTIC_CORE_PATH = re.compile(r"^pydantic_core/_pydantic_core(?:\.[A-Za-z0-9_-]+)*\.so$")
_MAX_MANIFEST_BYTES = 4 * 1024 * 1024
_GENERIC_ERROR = "Phase 7 guard release authority is invalid"
_SOURCE_OWNED_ROOTS = frozenset({"config", "mr_lister"})
_SOURCE_OWNED_FILES = frozenset({DEPENDENCY_BUILD_REQUEST_FILENAME, "requirements.txt"})
_DEPENDENCY_IMPORT_ROOTS = {
    "annotated-types": frozenset({"annotated_types"}),
    "boto3": frozenset({"boto3"}),
    "botocore": frozenset({"botocore"}),
    "jmespath": frozenset({"jmespath"}),
    "pydantic": frozenset({"pydantic"}),
    "pydantic-core": frozenset({"pydantic_core"}),
    "python-dateutil": frozenset({"dateutil"}),
    "s3transfer": frozenset({"s3transfer"}),
    "six": frozenset({"six.py"}),
    "typing-extensions": frozenset({"typing_extensions.py"}),
    "typing-inspection": frozenset({"typing_inspection"}),
    "urllib3": frozenset({"urllib3"}),
}
_IMPORT_TIME_HOOK_FILENAMES = frozenset({"sitecustomize.py", "usercustomize.py", "site.py"})


class Phase7GuardReleaseAuthorityError(RuntimeError):
    """Value-free failure for malformed, drifting, or unsealed guard bytes."""


@dataclass(frozen=True, slots=True)
class Phase7GuardReleaseBinding:
    """The verified local identity of one sealed guard runtime."""

    component: str
    entrypoint: str
    release_fingerprint: str
    deployment_manifest_fingerprint: str
    source_manifest_fingerprint: str
    dependency_manifest_fingerprint: str
    profile_fingerprint: str


def render_manifest(value: Mapping[str, object]) -> bytes:
    """Render the sole canonical JSON representation accepted for release authority."""

    return (
        json.dumps(value, allow_nan=False, indent=2, sort_keys=True, separators=(",", ": ")) + "\n"
    ).encode("utf-8")


def verify_phase7_guard_release(
    environment: Mapping[str, object],
    *,
    bundle_root: Path | None = None,
) -> Phase7GuardReleaseBinding:
    """Verify the environment fingerprint and every byte in an extracted guard bundle."""

    try:
        if not isinstance(environment, Mapping):
            raise ValueError
        expected_release = _required_fingerprint(
            environment,
            GUARD_RELEASE_FINGERPRINT_ENV,
        )
        if _required_fingerprint(environment, SHARED_RELEASE_FINGERPRINT_ENV) != expected_release:
            raise ValueError
        root = _exact_directory(bundle_root or Path(__file__).resolve().parents[2])
        release_bytes, release = _read_canonical_manifest(root / RELEASE_MANIFEST_FILENAME)
        release_fingerprint = sha256(release_bytes).hexdigest()
        if release_fingerprint != expected_release:
            raise ValueError
        _require_exact_keys(
            release,
            {
                "algorithm",
                "component",
                "dependency_manifest_sha256",
                "deployment_manifest_sha256",
                "entrypoint",
                "format",
                "profile_fingerprint",
                "source_manifest_sha256",
                "target",
            },
        )
        if (
            release["algorithm"] != "sha256"
            or release["component"] != _COMPONENT
            or release["entrypoint"] != GUARD_ENTRYPOINT
            or release["format"] != _RELEASE_FORMAT
            or release["target"] != LINUX_ARM64_TARGET
        ):
            raise ValueError
        deployment_fingerprint = _manifest_fingerprint(release["deployment_manifest_sha256"])
        source_fingerprint = _manifest_fingerprint(release["source_manifest_sha256"])
        dependency_fingerprint = _manifest_fingerprint(release["dependency_manifest_sha256"])
        profile_fingerprint = _nonzero_fingerprint(release["profile_fingerprint"])

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
        _verify_source_manifest(
            root,
            source,
            expected_profile=profile_fingerprint,
            allow_dependency_files=True,
        )
        verify_linux_arm64_dependency_artifact(
            root,
            build_request_path=root / DEPENDENCY_BUILD_REQUEST_FILENAME,
            allow_extra_files=True,
        )
        return Phase7GuardReleaseBinding(
            component=_COMPONENT,
            entrypoint=GUARD_ENTRYPOINT,
            release_fingerprint=release_fingerprint,
            deployment_manifest_fingerprint=deployment_fingerprint,
            source_manifest_fingerprint=source_fingerprint,
            dependency_manifest_fingerprint=dependency_fingerprint,
            profile_fingerprint=profile_fingerprint,
        )
    except Phase7GuardReleaseAuthorityError:
        raise
    except Exception:
        raise Phase7GuardReleaseAuthorityError(_GENERIC_ERROR) from None


def inspect_linux_arm64_dependency_artifact(
    root: Path,
    *,
    build_request_path: Path,
    allow_packaged_source: bool = False,
) -> dict[str, object]:
    """Inspect, but do not build, an installed Linux ARM64 dependency tree."""

    try:
        artifact_root = _exact_directory(root)
        request_bytes, request = _read_canonical_manifest(build_request_path)
        _verify_build_request(request, request_root=build_request_path.parent)
        files = inventory(
            artifact_root,
            excluded=frozenset({DEPENDENCY_ARTIFACT_FILENAME}),
        )
        if allow_packaged_source:
            files = [
                record
                for record in files
                if not _source_owned_path(cast(str, record["path"]))
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
        pinned = dict(PINNED_GUARD_DISTRIBUTIONS)
        installed = {
            cast(str, record["name"]): cast(str, record["version"]) for record in distributions
        }
        if required != sorted(pinned) or installed != pinned:
            raise ValueError
        tree_fingerprint = _dependency_tree_fingerprint(files)
        if tree_fingerprint != PINNED_GUARD_DEPENDENCY_TREE_FINGERPRINT:
            raise ValueError
        _verify_record_owned_dependency_files(artifact_root, distributions, files)
        native_files = _inspect_native_files(artifact_root, files)
        if not allow_packaged_source and any(
            _source_owned_path(cast(str, record["path"])) for record in files
        ):
            raise ValueError
        _require_pydantic_core_native(distributions, native_files)
        return {
            "algorithm": "sha256",
            "build_request_sha256": sha256(request_bytes).hexdigest(),
            "dependency_tree_sha256": tree_fingerprint,
            "distributions": distributions,
            "files": files,
            "format": _DEPENDENCY_FORMAT,
            "target": dict(LINUX_ARM64_TARGET),
            "wheel_artifacts": [
                {
                    "filename": filename,
                    "name": name,
                    "sha256": fingerprint,
                    "version": version,
                }
                for name, version, filename, fingerprint in PINNED_GUARD_WHEELS
            ],
        }
    except Phase7GuardReleaseAuthorityError:
        raise
    except Exception:
        raise Phase7GuardReleaseAuthorityError(_GENERIC_ERROR) from None


def verify_dependency_build_request(path: Path) -> Mapping[str, object]:
    """Verify one canonical request for a controlled guard dependency build."""

    try:
        _raw, request = _read_canonical_manifest(path)
        _verify_build_request(request, request_root=path.parent)
        return request
    except Phase7GuardReleaseAuthorityError:
        raise
    except Exception:
        raise Phase7GuardReleaseAuthorityError(_GENERIC_ERROR) from None


def verify_linux_arm64_dependency_artifact(
    root: Path,
    *,
    build_request_path: Path,
    allow_extra_files: bool = False,
) -> Mapping[str, object]:
    """Verify a previously inspected dependency tree without installing packages."""

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
    except Phase7GuardReleaseAuthorityError:
        raise
    except Exception:
        raise Phase7GuardReleaseAuthorityError(_GENERIC_ERROR) from None


def inventory(root: Path, *, excluded: frozenset[str]) -> list[dict[str, object]]:
    """Return the canonical byte inventory used by the guard builder and verifier."""

    files: list[dict[str, object]] = []
    paths = sorted(root.rglob("*"), key=lambda path: path.relative_to(root).as_posix())
    for path in paths:
        if path.is_symlink():
            raise ValueError("Guard release input contains a symlink")
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


def verify_source_manifest(root: Path) -> Mapping[str, object]:
    """Verify a source-stage guard manifest before dependency construction."""

    try:
        source_root = _exact_directory(root)
        _raw, manifest = _read_canonical_manifest(source_root / SOURCE_MANIFEST_FILENAME)
        profile = manifest.get("profile")
        if not isinstance(profile, Mapping):
            raise ValueError
        fingerprint = _nonzero_fingerprint(profile.get("fingerprint"))
        _verify_source_manifest(
            source_root,
            manifest,
            expected_profile=fingerprint,
            allow_dependency_files=False,
        )
        _raw_request, request = _read_canonical_manifest(
            source_root / DEPENDENCY_BUILD_REQUEST_FILENAME
        )
        _verify_build_request(request, request_root=source_root)
        return manifest
    except Phase7GuardReleaseAuthorityError:
        raise
    except Exception:
        raise Phase7GuardReleaseAuthorityError(_GENERIC_ERROR) from None


def _verify_deployment_manifest(root: Path, manifest: Mapping[str, object]) -> None:
    _require_exact_keys(
        manifest,
        {"algorithm", "component", "entrypoint", "files", "format", "target"},
    )
    if (
        manifest["algorithm"] != "sha256"
        or manifest["component"] != _COMPONENT
        or manifest["entrypoint"] != GUARD_ENTRYPOINT
        or manifest["format"] != _DEPLOYMENT_FORMAT
        or manifest["target"] != LINUX_ARM64_TARGET
    ):
        raise ValueError
    expected = inventory(
        root,
        excluded=frozenset({DEPLOYMENT_MANIFEST_FILENAME, RELEASE_MANIFEST_FILENAME}),
    )
    if manifest["files"] != expected:
        raise ValueError


def _verify_source_manifest(
    root: Path,
    manifest: Mapping[str, object],
    *,
    expected_profile: str,
    allow_dependency_files: bool,
) -> None:
    _require_exact_keys(
        manifest,
        {"algorithm", "entrypoint", "files", "format", "profile"},
    )
    if (
        manifest["algorithm"] != "sha256"
        or manifest["entrypoint"] != GUARD_ENTRYPOINT
        or manifest["format"] != _SOURCE_FORMAT
    ):
        raise ValueError
    _verify_inventory_records(root, manifest["files"])
    manifest_paths = [
        cast(str, record["path"])
        for record in cast(Sequence[Mapping[str, object]], manifest["files"])
    ]
    if manifest_paths != list(GUARD_SOURCE_PATHS):
        raise ValueError
    complete_inventory = inventory(root, excluded=frozenset({SOURCE_MANIFEST_FILENAME}))
    expected_inventory = (
        [record for record in complete_inventory if _source_owned_path(cast(str, record["path"]))]
        if allow_dependency_files
        else complete_inventory
    )
    if manifest["files"] != expected_inventory:
        raise ValueError
    profile = manifest["profile"]
    if not isinstance(profile, Mapping):
        raise ValueError
    _require_exact_keys(
        profile,
        {"fingerprint", "path", "profile_id", "profile_version", "publish_enabled"},
    )
    if (
        _nonzero_fingerprint(profile["fingerprint"]) != expected_profile
        or profile["path"] != "config/product_profiles/gildan_64000_swiftpod.json"
        or profile["profile_id"] != "gildan_64000_swiftpod"
        or profile["profile_version"] != 2
        or profile["publish_enabled"] is not False
    ):
        raise ValueError
    for relative in CAPABILITY_FREE_PACKAGE_INIT_PATHS:
        initializer = root / relative
        if (
            initializer.is_symlink()
            or not initializer.is_file()
            or initializer.read_bytes() != CAPABILITY_FREE_CLOUD_INIT_BYTES
        ):
            raise ValueError
    profile_path = root / cast(str, profile["path"])
    if profile_path.is_symlink() or not profile_path.is_file():
        raise ValueError
    profile_value = json.loads(profile_path.read_bytes())
    if not isinstance(profile_value, Mapping):
        raise ValueError
    canonical_profile = dict(profile_value)
    canonical_profile.setdefault("placement", None)
    calculated_profile_fingerprint = sha256(
        json.dumps(
            canonical_profile,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    if (
        calculated_profile_fingerprint != profile["fingerprint"]
        or calculated_profile_fingerprint != GUARD_PROFILE_FINGERPRINT
        or profile_value.get("profile_id") != profile["profile_id"]
        or profile_value.get("profile_version") != profile["profile_version"]
        or profile_value.get("publish_enabled") is not False
    ):
        raise ValueError


def _verify_build_request(request: Mapping[str, object], *, request_root: Path) -> None:
    _require_exact_keys(request, {"algorithm", "component", "format", "requirements", "target"})
    if (
        request["algorithm"] != "sha256"
        or request["component"] != _COMPONENT
        or request["format"] != _BUILD_REQUEST_FORMAT
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
    decoded = raw.decode("utf-8")
    if decoded != PINNED_GUARD_REQUIREMENTS or requirements["required_distributions"] != sorted(
        name for name, _version in PINNED_GUARD_DISTRIBUTIONS
    ):
        raise ValueError


def _dependency_tree_fingerprint(files: Sequence[Mapping[str, object]]) -> str:
    records = [dict(record) for record in files]
    return sha256(render_manifest({"files": records})).hexdigest()


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
    records.sort(key=lambda record: cast(str, record["name"]))
    if not records or len({record["name"] for record in records}) != len(records):
        raise ValueError
    return records


def _verify_record_owned_dependency_files(
    root: Path,
    distributions: Sequence[Mapping[str, object]],
    files: Sequence[Mapping[str, object]],
) -> None:
    """Require every dependency byte to be owned by exactly one pinned wheel RECORD."""

    expected_paths = {cast(str, record["path"]) for record in files}
    if len(expected_paths) != len(files):
        raise ValueError
    owners: dict[str, str] = {}
    for distribution in distributions:
        name = cast(str, distribution["name"])
        version = cast(str, distribution["version"])
        dist_info = cast(str, distribution["dist_info"])
        expected_dist_info = f"{name.replace('-', '_')}-{version}.dist-info"
        if dist_info != expected_dist_info or name not in _DEPENDENCY_IMPORT_ROOTS:
            raise ValueError
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
            path = PurePosixPath(relative)
            top_level = path.parts[0]
            allowed_roots = {
                *_DEPENDENCY_IMPORT_ROOTS[name],
                dist_info,
                f"{name.replace('-', '_')}-{version}.data",
            }
            if (
                top_level.casefold() in _IMPORT_TIME_HOOK_FILENAMES
                or top_level.casefold().endswith((".pth", ".egg-link"))
                or top_level not in allowed_roots
            ):
                raise ValueError
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
                expected_hash = (
                    base64.urlsafe_b64encode(bytes.fromhex(fingerprint)).decode("ascii").rstrip("=")
                )
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


def _parse_headers(path: Path) -> Any:
    if path.is_symlink() or not path.is_file() or not 1 <= path.stat().st_size <= 1024 * 1024:
        raise ValueError
    return Parser().parsestr(path.read_text(encoding="utf-8"), headersonly=True)


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


def _require_pydantic_core_native(
    distributions: Sequence[Mapping[str, object]],
    native_files: frozenset[str],
) -> None:
    by_name = {cast(str, record["name"]): record for record in distributions}
    record = by_name.get("pydantic-core")
    if record is None:
        raise ValueError
    tags = record.get("tags")
    if not isinstance(tags, list) or not any(
        isinstance(tag, str) and _NATIVE_ARM64_TAG.fullmatch(tag) is not None for tag in tags
    ):
        raise ValueError
    if not any(_PYDANTIC_CORE_PATH.fullmatch(path) is not None for path in native_files):
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
        or any(part in {"", ".", "..", ".git", "__pycache__"} for part in path.parts)
    ):
        raise ValueError


def _source_owned_path(relative: str) -> bool:
    path = PurePosixPath(relative)
    return (
        relative in _SOURCE_OWNED_FILES or bool(path.parts) and path.parts[0] in _SOURCE_OWNED_ROOTS
    )


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


def _nonzero_fingerprint(value: object) -> str:
    fingerprint = _manifest_fingerprint(value)
    if fingerprint == "0" * 64:
        raise ValueError
    return fingerprint


def _required_fingerprint(environment: Mapping[str, object], name: str) -> str:
    return _nonzero_fingerprint(environment.get(name))


def _require_exact_keys(value: Mapping[object, object], keys: set[str]) -> None:
    if set(value) != keys:
        raise ValueError


__all__ = [
    "CAPABILITY_FREE_CLOUD_INIT_BYTES",
    "CAPABILITY_FREE_CLOUD_INIT_PATH",
    "CAPABILITY_FREE_PACKAGE_INIT_PATHS",
    "DEPENDENCY_ARTIFACT_FILENAME",
    "DEPENDENCY_BUILD_REQUEST_FILENAME",
    "DEPLOYMENT_MANIFEST_FILENAME",
    "GUARD_ENTRYPOINT",
    "GUARD_PROFILE_FINGERPRINT",
    "GUARD_RELEASE_FINGERPRINT_ENV",
    "GUARD_SOURCE_PATHS",
    "LINUX_ARM64_TARGET",
    "PINNED_GUARD_DEPENDENCY_TREE_FINGERPRINT",
    "PINNED_GUARD_DISTRIBUTIONS",
    "PINNED_GUARD_REQUIREMENTS",
    "PINNED_GUARD_WHEELS",
    "RELEASE_MANIFEST_FILENAME",
    "SOURCE_MANIFEST_FILENAME",
    "SHARED_RELEASE_FINGERPRINT_ENV",
    "Phase7GuardReleaseAuthorityError",
    "Phase7GuardReleaseBinding",
    "inspect_linux_arm64_dependency_artifact",
    "inventory",
    "render_manifest",
    "verify_dependency_build_request",
    "verify_linux_arm64_dependency_artifact",
    "verify_phase7_guard_release",
    "verify_source_manifest",
]
