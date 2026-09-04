"""Release-first verifier for the Phase 7.18 enabled runtime.

Only standard-library file inspection and the established Phase 6 manifest renderer run before
the entrypoint is allowed to import boto3 or construct a runtime graph.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path, PurePosixPath
from typing import cast

from mr_lister.release.phase6 import LINUX_ARM64_TARGET, render_manifest

PHASE718_ENTRYPOINTS = (
    "mr_lister.cloud.phase718_entrypoints.publication_query_handler",
    "mr_lister.cloud.phase718_entrypoints.publication_request_handler",
    "mr_lister.cloud.phase718_entrypoints.publication_dispatcher_handler",
    "mr_lister.cloud.phase718_entrypoints.publication_worker_handler",
    "mr_lister.cloud.phase718_entrypoints.publication_recovery_handler",
    "mr_lister.cloud.phase718_entrypoints.publication_retention_handler",
)

PHASE718_RELEASE_FINGERPRINT_ENV = "MR_LISTER_PHASE7_ENABLED_RELEASE_FINGERPRINT"
APPLICATION_RELEASE_FINGERPRINT_ENV = "MR_LISTER_RELEASE_FINGERPRINT"
CONTRACT_FINGERPRINT_ENV = "MR_LISTER_PHASE7_CONTRACT_FINGERPRINT"
CANARY_EVIDENCE_FINGERPRINT_ENV = "MR_LISTER_PHASE7_CANARY_EVIDENCE_FINGERPRINT"
ENABLEMENT_EVIDENCE_FINGERPRINT_ENV = "MR_LISTER_PHASE7_ENABLEMENT_EVIDENCE_FINGERPRINT"
PROFILE_FINGERPRINT_ENV = "MR_LISTER_PRODUCT_PROFILE_FINGERPRINT"
PROFILE_PATH_ENV = "MR_LISTER_PRODUCT_PROFILE_PATH"
STATE_TABLE_ENV = "MR_LISTER_STATE_TABLE"

PHASE718_CONTRACT_VERSION = "7.1.0"
PHASE718_CONTRACT_FINGERPRINT = "5172926cb89f8c046247922d8311c3f8b6361a9d67a719aa3a19a1c0ef1ed678"
PHASE718_CONTRACT_PATH = "contracts/publication/phase7.1.0.json"
PHASE718_PROFILE_PATH = "config/product_profiles/gildan_64000_swiftpod.json"
PHASE718_PROFILE_FINGERPRINT = "5de1257141cfdacb1731f68bb9113712957483b33d5b0f7115afdba86eb7476c"
PHASE718_PROFILE_FILE_FINGERPRINT = (
    "eb9b7769e0049f7b270da70caa06fb321d56e1a4c37a280bb6265ba3544aae40"
)

PHASE718_BINDING_FILENAME = "phase718-enabled-binding.json"
PHASE718_SOURCE_MANIFEST_FILENAME = "phase718-enabled-source-manifest.json"
PHASE718_DEPLOYMENT_MANIFEST_FILENAME = "phase718-enabled-deployment-manifest.json"
PHASE718_RELEASE_MANIFEST_FILENAME = "phase718-enabled-release-manifest.json"
DEPENDENCY_MANIFEST_FILENAME = "dependency-artifact.json"

_COMPONENT = "phase718-enabled-lambda"
_BINDING_FORMAT = "phase718-enabled-binding-v1"
_DEPLOYMENT_FORMAT = "phase718-enabled-deployment-v1"
_RELEASE_FORMAT = "phase718-enabled-release-v1"
_FINGERPRINT = re.compile(r"^[a-f0-9]{64}$")
_MAX_MANIFEST_BYTES = 4 * 1024 * 1024
_GENERIC_ERROR = "Phase 7.18 enabled release authority is invalid"


class Phase718ReleaseAuthorityError(RuntimeError):
    """Value-free refusal for malformed, drifting, or unsealed enabled bytes."""


@dataclass(frozen=True, slots=True)
class Phase718ReleaseBinding:
    component: str
    entrypoint: str
    release_fingerprint: str
    application_release_fingerprint: str
    contract_fingerprint: str
    profile_fingerprint: str
    canary_evidence_fingerprint: str
    enablement_evidence_fingerprint: str
    state_table: str
    binding_fingerprint: str
    source_manifest_fingerprint: str
    dependency_manifest_fingerprint: str
    deployment_manifest_fingerprint: str


def verify_phase718_runtime_release(
    environment: Mapping[str, object],
    *,
    expected_entrypoint: str,
    bundle_root: Path | None = None,
) -> Phase718ReleaseBinding:
    """Authenticate the exact handler, activation evidence, and every packaged byte."""

    try:
        if not isinstance(environment, Mapping) or expected_entrypoint not in PHASE718_ENTRYPOINTS:
            raise ValueError
        expected_release = _required_fingerprint(environment, PHASE718_RELEASE_FINGERPRINT_ENV)
        expected_application = _required_fingerprint(
            environment,
            APPLICATION_RELEASE_FINGERPRINT_ENV,
        )
        expected_canary = _required_fingerprint(
            environment,
            CANARY_EVIDENCE_FINGERPRINT_ENV,
        )
        expected_enablement = _required_fingerprint(
            environment,
            ENABLEMENT_EVIDENCE_FINGERPRINT_ENV,
        )
        root = _exact_directory(bundle_root or Path(__file__).resolve().parents[2])

        release_bytes, release = _read_canonical(root / PHASE718_RELEASE_MANIFEST_FILENAME)
        if sha256(release_bytes).hexdigest() != expected_release:
            raise ValueError
        _require_exact_keys(
            release,
            {
                "algorithm",
                "application_release_fingerprint",
                "binding_sha256",
                "canary_evidence_fingerprint",
                "component",
                "contract_fingerprint",
                "dependency_manifest_sha256",
                "deployment_manifest_sha256",
                "enablement_evidence_fingerprint",
                "entrypoints",
                "format",
                "profile_fingerprint",
                "source_manifest_sha256",
                "state_table",
                "target",
            },
        )
        if (
            release["algorithm"] != "sha256"
            or release["application_release_fingerprint"] != expected_application
            or release["canary_evidence_fingerprint"] != expected_canary
            or release["component"] != _COMPONENT
            or release["contract_fingerprint"] != PHASE718_CONTRACT_FINGERPRINT
            or release["enablement_evidence_fingerprint"] != expected_enablement
            or release["entrypoints"] != list(PHASE718_ENTRYPOINTS)
            or release["format"] != _RELEASE_FORMAT
            or release["profile_fingerprint"] != PHASE718_PROFILE_FINGERPRINT
            or release["state_table"] != environment.get(STATE_TABLE_ENV)
            or release["target"] != LINUX_ARM64_TARGET
        ):
            raise ValueError

        binding_fingerprint = _nonzero_fingerprint(release["binding_sha256"])
        source_fingerprint = _nonzero_fingerprint(release["source_manifest_sha256"])
        dependency_fingerprint = _nonzero_fingerprint(release["dependency_manifest_sha256"])
        deployment_fingerprint = _nonzero_fingerprint(release["deployment_manifest_sha256"])
        binding_bytes, binding = _read_canonical(root / PHASE718_BINDING_FILENAME)
        source_bytes, _source = _read_canonical(root / PHASE718_SOURCE_MANIFEST_FILENAME)
        dependency_bytes, _dependency = _read_canonical(root / DEPENDENCY_MANIFEST_FILENAME)
        deployment_bytes, deployment = _read_canonical(root / PHASE718_DEPLOYMENT_MANIFEST_FILENAME)
        if (
            sha256(binding_bytes).hexdigest() != binding_fingerprint
            or sha256(source_bytes).hexdigest() != source_fingerprint
            or sha256(dependency_bytes).hexdigest() != dependency_fingerprint
            or sha256(deployment_bytes).hexdigest() != deployment_fingerprint
        ):
            raise ValueError
        _verify_binding(binding, environment=environment)
        _verify_deployment(root, deployment)
        _verify_contract(root)
        _verify_profile(root, environment=environment)

        return Phase718ReleaseBinding(
            component=_COMPONENT,
            entrypoint=expected_entrypoint,
            release_fingerprint=expected_release,
            application_release_fingerprint=expected_application,
            contract_fingerprint=PHASE718_CONTRACT_FINGERPRINT,
            profile_fingerprint=PHASE718_PROFILE_FINGERPRINT,
            canary_evidence_fingerprint=expected_canary,
            enablement_evidence_fingerprint=expected_enablement,
            state_table=cast(str, release["state_table"]),
            binding_fingerprint=binding_fingerprint,
            source_manifest_fingerprint=source_fingerprint,
            dependency_manifest_fingerprint=dependency_fingerprint,
            deployment_manifest_fingerprint=deployment_fingerprint,
        )
    except Phase718ReleaseAuthorityError:
        raise
    except Exception:
        raise Phase718ReleaseAuthorityError(_GENERIC_ERROR) from None


def _verify_binding(
    binding: Mapping[str, object],
    *,
    environment: Mapping[str, object],
) -> None:
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
    expected = {
        "application_release_fingerprint": environment.get(APPLICATION_RELEASE_FINGERPRINT_ENV),
        "canary_evidence_fingerprint": environment.get(CANARY_EVIDENCE_FINGERPRINT_ENV),
        "contract_fingerprint": PHASE718_CONTRACT_FINGERPRINT,
        "contract_version": PHASE718_CONTRACT_VERSION,
        "enablement_evidence_fingerprint": environment.get(ENABLEMENT_EVIDENCE_FINGERPRINT_ENV),
        "entrypoints": list(PHASE718_ENTRYPOINTS),
        "format": _BINDING_FORMAT,
        "profile_fingerprint": PHASE718_PROFILE_FINGERPRINT,
        "state_table": environment.get(STATE_TABLE_ENV),
    }
    if binding != expected:
        raise ValueError


def _verify_deployment(root: Path, deployment: Mapping[str, object]) -> None:
    _require_exact_keys(
        deployment,
        {"algorithm", "component", "entrypoints", "files", "format", "target"},
    )
    if (
        deployment["algorithm"] != "sha256"
        or deployment["component"] != _COMPONENT
        or deployment["entrypoints"] != list(PHASE718_ENTRYPOINTS)
        or deployment["format"] != _DEPLOYMENT_FORMAT
        or deployment["target"] != LINUX_ARM64_TARGET
        or deployment["files"]
        != _inventory(
            root,
            excluded=frozenset(
                {
                    PHASE718_DEPLOYMENT_MANIFEST_FILENAME,
                    PHASE718_RELEASE_MANIFEST_FILENAME,
                }
            ),
        )
    ):
        raise ValueError


def _verify_contract(root: Path) -> None:
    raw, contract = _read_canonical(root / PHASE718_CONTRACT_PATH)
    if (
        sha256(raw).hexdigest() != PHASE718_CONTRACT_FINGERPRINT
        or contract.get("contract_version") != PHASE718_CONTRACT_VERSION
        or contract.get("current_activation_phase") != "general_availability"
        or contract.get("publication_enabled") is not True
        or contract.get("phase6_runtime_unchanged") is not True
    ):
        raise ValueError


def _verify_profile(root: Path, *, environment: Mapping[str, object]) -> None:
    path = root / PHASE718_PROFILE_PATH
    if (
        path.is_symlink()
        or not path.is_file()
        or sha256(path.read_bytes()).hexdigest() != PHASE718_PROFILE_FILE_FINGERPRINT
        or environment.get(PROFILE_FINGERPRINT_ENV) != PHASE718_PROFILE_FINGERPRINT
        or environment.get(PROFILE_PATH_ENV) != path.as_posix()
    ):
        raise ValueError


def _inventory(root: Path, *, excluded: frozenset[str]) -> list[dict[str, object]]:
    files: list[dict[str, object]] = []
    for path in sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix()):
        if path.is_symlink():
            raise ValueError
        if not path.is_file():
            continue
        relative = path.relative_to(root).as_posix()
        _safe_relative(relative)
        if relative in excluded:
            continue
        raw = path.read_bytes()
        files.append({"path": relative, "sha256": sha256(raw).hexdigest(), "size_bytes": len(raw)})
    return files


def _read_canonical(path: Path) -> tuple[bytes, Mapping[str, object]]:
    if path.is_symlink() or not path.is_file():
        raise ValueError
    raw = path.read_bytes()
    if not raw or len(raw) > _MAX_MANIFEST_BYTES:
        raise ValueError
    payload = json.loads(
        raw,
        object_pairs_hook=_unique_object,
        parse_constant=_reject_constant,
    )
    if not isinstance(payload, dict) or render_manifest(payload) != raw:
        raise ValueError
    return raw, payload


def _required_fingerprint(environment: Mapping[str, object], name: str) -> str:
    return _nonzero_fingerprint(environment.get(name))


def _nonzero_fingerprint(value: object) -> str:
    if not isinstance(value, str) or _FINGERPRINT.fullmatch(value) is None or value == "0" * 64:
        raise ValueError
    return value


def _require_exact_keys(value: Mapping[str, object], keys: set[str]) -> None:
    if set(value) != keys:
        raise ValueError


def _exact_directory(path: Path) -> Path:
    if path.is_symlink() or not path.is_dir() or path.resolve(strict=True) != path:
        raise ValueError
    return path


def _safe_relative(value: str) -> None:
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or not path.parts:
        raise ValueError


def _unique_object(pairs: Sequence[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    del value
    raise ValueError


__all__ = [
    "PHASE718_BINDING_FILENAME",
    "PHASE718_CONTRACT_FINGERPRINT",
    "PHASE718_CONTRACT_PATH",
    "PHASE718_DEPLOYMENT_MANIFEST_FILENAME",
    "PHASE718_ENTRYPOINTS",
    "PHASE718_PROFILE_FILE_FINGERPRINT",
    "PHASE718_PROFILE_FINGERPRINT",
    "PHASE718_PROFILE_PATH",
    "PHASE718_RELEASE_MANIFEST_FILENAME",
    "PHASE718_SOURCE_MANIFEST_FILENAME",
    "Phase718ReleaseAuthorityError",
    "Phase718ReleaseBinding",
    "verify_phase718_runtime_release",
]
