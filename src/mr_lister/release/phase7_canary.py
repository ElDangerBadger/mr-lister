"""Fail-closed authority for one exact-bound Phase 7 canary release.

The verifier deliberately depends only on the standard library and the reviewed Phase 6
dependency authority.  In particular, it does not import publication runtime code before every
packaged byte and the sanitized canary binding have been authenticated.  The entrypoint may
strict-parse ``canary_binding_payload`` only after this verifier returns.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
from pathlib import Path, PurePosixPath
from typing import cast

from mr_lister.release.phase6 import (
    DEPENDENCY_ARTIFACT_FILENAME,
    DEPENDENCY_BUILD_REQUEST_FILENAME,
    LINUX_ARM64_TARGET,
    Phase6ReleaseAuthorityError,
    render_manifest,
    verify_linux_arm64_dependency_artifact,
    wheel_authority_from_build_request,
)

CANARY_ENTRYPOINT = "mr_lister.cloud.phase7_canary_entrypoint.publication_canary_handler"
CANARY_RELEASE_FINGERPRINT_ENV = "MR_LISTER_PHASE7_CANARY_RELEASE_FINGERPRINT"
APPLICATION_RELEASE_FINGERPRINT_ENV = "MR_LISTER_RELEASE_FINGERPRINT"
CANARY_BINDING_FINGERPRINT_ENV = "MR_LISTER_PHASE7_CANARY_BINDING_FINGERPRINT"

CANARY_BINDING_FILENAME = "canary-binding.json"
SOURCE_MANIFEST_FILENAME = "source-manifest.json"
DEPLOYMENT_MANIFEST_FILENAME = "deployment-manifest.json"
RELEASE_MANIFEST_FILENAME = "release-manifest.json"

CANARY_PROFILE_PATH = "config/product_profiles/gildan_64000_swiftpod.json"
CANARY_PROFILE_ID = "gildan_64000_swiftpod"
CANARY_PROFILE_VERSION = 2
CANARY_PROFILE_FINGERPRINT = "5de1257141cfdacb1731f68bb9113712957483b33d5b0f7115afdba86eb7476c"
CANARY_PROFILE_FILE_FINGERPRINT = "eb9b7769e0049f7b270da70caa06fb321d56e1a4c37a280bb6265ba3544aae40"
PHASE6_LAMBDA_WHEEL_AUTHORITY_FINGERPRINT = (
    "145ae4affca308e4268120e5f5f725d1e91ba3194db76585e53cba32d29eefbd"
)

CANARY_MODES = frozenset({"publish_once", "read_only_preflight"})

_COMPONENT = "phase7-canary-lambda"
_SOURCE_FORMAT = "phase7-canary-source-v1"
_DEPLOYMENT_FORMAT = "phase7-canary-deployment-v1"
_RELEASE_FORMAT = "phase7-canary-release-v1"
_FINGERPRINT = re.compile(r"^[a-f0-9]{64}$")
_UTC_TIMESTAMP = re.compile(
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}"
    r"(?:\.[0-9]{1,6})?Z$"
)
_MAX_MANIFEST_BYTES = 4 * 1024 * 1024
_GENERIC_ERROR = "Phase 7 canary release authority is invalid"
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
_BINDING_KEYS = {
    "aggregate_id_digest",
    "contract_version",
    "fingerprint",
    "job_id_digest",
    "mode",
    "owner_id_digest",
    "permit_id_digest",
    "release_manifest_fingerprint",
    "required_preflight_proof_fingerprint",
    "snapshot_fingerprint",
    "verification_deadline",
    "work_input_fingerprint",
    "work_request_id_digest",
}


class Phase7CanaryReleaseAuthorityError(RuntimeError):
    """A value-free refusal for malformed, drifting, or unsealed canary bytes."""


@dataclass(frozen=True, slots=True)
class Phase7CanaryReleaseBinding:
    """Verified identities and the still-unparsed sanitized runtime binding."""

    component: str
    entrypoint: str
    release_fingerprint: str
    application_release_fingerprint: str
    deployment_manifest_fingerprint: str
    source_manifest_fingerprint: str
    dependency_manifest_fingerprint: str
    profile_fingerprint: str
    binding_fingerprint: str
    binding_mode: str
    binding_payload: Mapping[str, object]

    @property
    def canary_binding(self) -> Mapping[str, object]:
        """Compatibility alias for callers that use the shorter binding name."""

        return self.binding_payload

    @property
    def canary_binding_payload(self) -> Mapping[str, object]:
        return self.binding_payload

    @property
    def canary_binding_fingerprint(self) -> str:
        return self.binding_fingerprint

    @property
    def canary_mode(self) -> str:
        return self.binding_mode


def verify_phase7_canary_release(
    environment: Mapping[str, object],
    *,
    bundle_root: Path | None = None,
) -> Phase7CanaryReleaseBinding:
    """Authenticate the release, application identity, and every packaged byte."""

    try:
        if not isinstance(environment, Mapping):
            raise ValueError
        expected_release = _required_fingerprint(environment, CANARY_RELEASE_FINGERPRINT_ENV)
        expected_application = _required_fingerprint(
            environment,
            APPLICATION_RELEASE_FINGERPRINT_ENV,
        )
        expected_binding = _required_fingerprint(
            environment,
            CANARY_BINDING_FINGERPRINT_ENV,
        )
        root = _exact_directory(bundle_root or Path(__file__).resolve().parents[2])

        release_bytes, release = _read_canonical_manifest(root / RELEASE_MANIFEST_FILENAME)
        release_fingerprint = sha256(release_bytes).hexdigest()
        if release_fingerprint != expected_release:
            raise ValueError
        _require_exact_keys(
            release,
            {
                "algorithm",
                "application_release_fingerprint",
                "binding_fingerprint",
                "binding_mode",
                "binding_sha256",
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
            or release["entrypoint"] != CANARY_ENTRYPOINT
            or release["format"] != _RELEASE_FORMAT
            or release["target"] != LINUX_ARM64_TARGET
            or release["binding_mode"] not in CANARY_MODES
        ):
            raise ValueError

        application_fingerprint = _nonzero_fingerprint(release["application_release_fingerprint"])
        binding_fingerprint = _nonzero_fingerprint(release["binding_fingerprint"])
        binding_sha256 = _nonzero_fingerprint(release["binding_sha256"])
        deployment_fingerprint = _nonzero_fingerprint(release["deployment_manifest_sha256"])
        source_fingerprint = _nonzero_fingerprint(release["source_manifest_sha256"])
        dependency_fingerprint = _nonzero_fingerprint(release["dependency_manifest_sha256"])
        profile_fingerprint = _nonzero_fingerprint(release["profile_fingerprint"])
        if (
            application_fingerprint != expected_application
            or binding_fingerprint != expected_binding
            or profile_fingerprint != CANARY_PROFILE_FINGERPRINT
        ):
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
        binding_payload = _verify_source_manifest(root, source, allow_dependency_files=True)
        verified_dependency = verify_linux_arm64_dependency_artifact(
            root,
            build_request_path=root / DEPENDENCY_BUILD_REQUEST_FILENAME,
            allow_extra_files=True,
        )
        if verified_dependency.get("format") != "phase6-linux-arm64-dependencies-v2":
            raise ValueError
        _verify_checked_dependency_build_request(root / DEPENDENCY_BUILD_REQUEST_FILENAME)

        source_binding = cast(Mapping[str, object], source["binding"])
        source_profile = cast(Mapping[str, object], source["profile"])
        if (
            binding_payload["fingerprint"] != binding_fingerprint
            or binding_payload["mode"] != release["binding_mode"]
            or binding_payload["release_manifest_fingerprint"] != application_fingerprint
            or source_binding["fingerprint"] != binding_fingerprint
            or source_binding["mode"] != release["binding_mode"]
            or source_binding["release_manifest_fingerprint"] != application_fingerprint
            or source_binding["sha256"] != binding_sha256
            or source_profile["fingerprint"] != profile_fingerprint
        ):
            raise ValueError

        return Phase7CanaryReleaseBinding(
            component=_COMPONENT,
            entrypoint=CANARY_ENTRYPOINT,
            release_fingerprint=release_fingerprint,
            application_release_fingerprint=application_fingerprint,
            deployment_manifest_fingerprint=deployment_fingerprint,
            source_manifest_fingerprint=source_fingerprint,
            dependency_manifest_fingerprint=dependency_fingerprint,
            profile_fingerprint=profile_fingerprint,
            binding_fingerprint=binding_fingerprint,
            binding_mode=cast(str, release["binding_mode"]),
            binding_payload=dict(binding_payload),
        )
    except (Phase6ReleaseAuthorityError, Phase7CanaryReleaseAuthorityError):
        raise Phase7CanaryReleaseAuthorityError(_GENERIC_ERROR) from None
    except Exception:
        raise Phase7CanaryReleaseAuthorityError(_GENERIC_ERROR) from None


def verify_phase7_canary_source_manifest(
    root: Path,
    *,
    allow_dependency_files: bool = False,
) -> Mapping[str, object]:
    """Verify a source stage and return its canonical sanitized binding payload."""

    try:
        source_root = _exact_directory(root)
        _raw, source = _read_canonical_manifest(source_root / SOURCE_MANIFEST_FILENAME)
        return _verify_source_manifest(
            source_root,
            source,
            allow_dependency_files=allow_dependency_files,
        )
    except Phase7CanaryReleaseAuthorityError:
        raise
    except Exception:
        raise Phase7CanaryReleaseAuthorityError(_GENERIC_ERROR) from None


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


def _verify_deployment_manifest(root: Path, manifest: Mapping[str, object]) -> None:
    _require_exact_keys(
        manifest,
        {"algorithm", "component", "entrypoint", "files", "format", "target"},
    )
    if (
        manifest["algorithm"] != "sha256"
        or manifest["component"] != _COMPONENT
        or manifest["entrypoint"] != CANARY_ENTRYPOINT
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
) -> Mapping[str, object]:
    _require_exact_keys(
        manifest,
        {"algorithm", "binding", "entrypoint", "files", "format", "profile", "target"},
    )
    if (
        manifest["algorithm"] != "sha256"
        or manifest["entrypoint"] != CANARY_ENTRYPOINT
        or manifest["format"] != _SOURCE_FORMAT
        or manifest["target"] != LINUX_ARM64_TARGET
    ):
        raise ValueError
    files = manifest["files"]
    _verify_inventory_records(root, files)
    if not allow_dependency_files and files != inventory(
        root,
        excluded=frozenset({SOURCE_MANIFEST_FILENAME}),
    ):
        raise ValueError

    binding_record = manifest["binding"]
    if not isinstance(binding_record, Mapping):
        raise ValueError
    _require_exact_keys(
        binding_record,
        {"fingerprint", "mode", "path", "release_manifest_fingerprint", "sha256"},
    )
    if binding_record["path"] != CANARY_BINDING_FILENAME:
        raise ValueError
    binding_raw, binding = _read_canonical_manifest(root / CANARY_BINDING_FILENAME)
    _verify_binding_payload(binding)
    if (
        binding_record["fingerprint"] != binding["fingerprint"]
        or binding_record["mode"] != binding["mode"]
        or binding_record["release_manifest_fingerprint"] != binding["release_manifest_fingerprint"]
        or binding_record["sha256"] != sha256(binding_raw).hexdigest()
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
    profile_path = root / CANARY_PROFILE_PATH
    if (
        profile
        != {
            "file_sha256": CANARY_PROFILE_FILE_FINGERPRINT,
            "fingerprint": CANARY_PROFILE_FINGERPRINT,
            "path": CANARY_PROFILE_PATH,
            "profile_id": CANARY_PROFILE_ID,
            "profile_version": CANARY_PROFILE_VERSION,
            "publish_enabled": False,
        }
        or profile_path.is_symlink()
        or not profile_path.is_file()
        or sha256(profile_path.read_bytes()).hexdigest() != CANARY_PROFILE_FILE_FINGERPRINT
    ):
        raise ValueError
    _verify_checked_dependency_build_request(root / DEPENDENCY_BUILD_REQUEST_FILENAME)
    _verify_source_hygiene(cast(Sequence[Mapping[str, object]], files))
    return binding


def _verify_binding_payload(binding: Mapping[str, object]) -> None:
    _require_exact_keys(binding, _BINDING_KEYS)
    if binding["contract_version"] != "7.0.1" or binding["mode"] not in CANARY_MODES:
        raise ValueError
    for key in _BINDING_KEYS - {
        "contract_version",
        "mode",
        "required_preflight_proof_fingerprint",
        "verification_deadline",
    }:
        _nonzero_fingerprint(binding[key])
    proof = binding["required_preflight_proof_fingerprint"]
    if binding["mode"] == "read_only_preflight":
        if proof is not None:
            raise ValueError
    elif _nonzero_fingerprint(proof) != proof:
        raise ValueError
    deadline = binding["verification_deadline"]
    if not isinstance(deadline, str) or _UTC_TIMESTAMP.fullmatch(deadline) is None:
        raise ValueError
    parsed = datetime.fromisoformat(deadline.removesuffix("Z") + "+00:00")
    if parsed.utcoffset() is None or parsed.utcoffset().total_seconds() != 0:
        raise ValueError


def _verify_checked_dependency_build_request(path: Path) -> None:
    authority = wheel_authority_from_build_request(path)
    if (
        authority.get("component") != "lambda"
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
        CANARY_BINDING_FILENAME,
        CANARY_PROFILE_PATH,
        DEPENDENCY_BUILD_REQUEST_FILENAME,
        "requirements.txt",
        "mr_lister/cloud/phase7_canary_entrypoint.py",
        "mr_lister/release/phase6.py",
        "mr_lister/release/phase7_canary.py",
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
    "APPLICATION_RELEASE_FINGERPRINT_ENV",
    "CANARY_BINDING_FILENAME",
    "CANARY_BINDING_FINGERPRINT_ENV",
    "CANARY_ENTRYPOINT",
    "CANARY_MODES",
    "CANARY_PROFILE_FILE_FINGERPRINT",
    "CANARY_PROFILE_FINGERPRINT",
    "CANARY_PROFILE_ID",
    "CANARY_PROFILE_PATH",
    "CANARY_PROFILE_VERSION",
    "CANARY_RELEASE_FINGERPRINT_ENV",
    "DEPENDENCY_ARTIFACT_FILENAME",
    "DEPENDENCY_BUILD_REQUEST_FILENAME",
    "DEPLOYMENT_MANIFEST_FILENAME",
    "LINUX_ARM64_TARGET",
    "PHASE6_LAMBDA_WHEEL_AUTHORITY_FINGERPRINT",
    "RELEASE_MANIFEST_FILENAME",
    "SOURCE_MANIFEST_FILENAME",
    "Phase7CanaryReleaseAuthorityError",
    "Phase7CanaryReleaseBinding",
    "inventory",
    "render_manifest",
    "verify_phase7_canary_release",
    "verify_phase7_canary_source_manifest",
]
