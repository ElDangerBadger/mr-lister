"""Render the exact Phase 6 walkthrough AgentCore hotfix without calling AWS.

This tracked renderer is intentionally specific to source ``f6a643b``, its sealed artifact, and
the accepted development runtime v3. It has no account, Region, runtime, version, role, model,
product, source, or output overrides. Runtime-update inputs are emitted only after fresh v3 and
retained-role observations plus closed common-v2 S3 object evidence verify. Endpoint inputs are a
second stage and additionally require joined update/get evidence showing runtime v4 READY.
"""

from __future__ import annotations

import argparse
import json
import re
import stat
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path
from typing import Final

from mr_lister.review_profile import FilesystemReviewProductAuthority
from tools.render_phase6_agentcore_direct_codezip import (
    Phase6AgentCoreDirectCodeZipBinding,
    VerifiedAgentCoreArchive,
    verify_phase6_agentcore_direct_codezip_artifact,
)
from tools.verify_phase6_s3_release_object import (
    Phase6S3ReleaseObjectExpectation,
    VerifiedPhase6S3ReleaseObject,
    verify_phase6_s3_release_object_evidence,
)

ROOT: Final = Path(__file__).resolve().parents[1]

ACCOUNT_ID: Final = "384627057108"
REGION: Final = "us-west-2"
ENVIRONMENT: Final = "dev"
RUNTIME_NAME: Final = "mr_lister_phase6"
RUNTIME_ID: Final = "mr_lister_phase6-4HoPmq2hCI"
RUNTIME_ARN: Final = f"arn:aws:bedrock-agentcore:{REGION}:{ACCOUNT_ID}:runtime/{RUNTIME_ID}"
ROLE_NAME: Final = "mr-lister-phase6-agentcore-runtime-dev"
ROLE_ARN: Final = f"arn:aws:iam::{ACCOUNT_ID}:role/{ROLE_NAME}"
ROLE_POLICY_NAME: Final = "RunOnlyMrListerPhase6Preparation"

CURRENT_VERSION: Final = "3"
TARGET_VERSION: Final = "4"
CURRENT_ENDPOINT_NAME: Final = "phase6_v3_dev"
TARGET_ENDPOINT_NAME: Final = "phase6_v4_dev"
CURRENT_ENDPOINT_ARN: Final = f"{RUNTIME_ARN}/runtime-endpoint/{CURRENT_ENDPOINT_NAME}"
TARGET_ENDPOINT_ARN: Final = f"{RUNTIME_ARN}/runtime-endpoint/{TARGET_ENDPOINT_NAME}"

PREDECESSOR_RELEASE_FINGERPRINT: Final = (
    "f34ab73042014fccce2cb3733624f005a4ccc10bb065b39c3e20befd3c33923f"
)
PREDECESSOR_ARCHIVE_SHA256: Final = (
    "443f62fe01a2ebd54c8ff4b551eab94c829a878b42333973a6731e1cdd105f8b"
)
PREDECESSOR_S3_VERSION_ID: Final = "MvUWP5rf7CcMOeGrpJlM_YiUsy0wSCrJ"

SOURCE_COMMIT: Final = "f6a643b19e47f02784e9b590949fddde1cf9c107"
HOTFIX_RELEASE_FINGERPRINT: Final = (
    "9bc5e1727cfcf68b40847d1a2e416300640779898c9bf884f6f9e442b0225d9e"
)
HOTFIX_ARCHIVE_SHA256: Final = "5a2821b40e39cf7fcdf77421ed6bce1b3b76907af6221930ea29dc5c6210c7a6"
HOTFIX_ARCHIVE_SIZE: Final = 96_310_907

ARTIFACT_BUCKET: Final = f"mr-lister-phase6-artifacts-dev-{ACCOUNT_ID}-{REGION}"
PREDECESSOR_KEY: Final = (
    "private/deployments/agentcore/releases/"
    f"{PREDECESSOR_RELEASE_FINGERPRINT}/"
    f"phase6-agentcore-{PREDECESSOR_ARCHIVE_SHA256}.zip"
)
HOTFIX_KEY: Final = (
    "private/deployments/agentcore/releases/"
    f"{HOTFIX_RELEASE_FINGERPRINT}/phase6-agentcore-{HOTFIX_ARCHIVE_SHA256}.zip"
)

PYTHON_RUNTIME: Final = "PYTHON_3_12"
ENTRY_POINT: Final = ["main.py"]
NETWORK_CONFIGURATION: Final = {"networkMode": "PUBLIC"}
PROTOCOL_CONFIGURATION: Final = {"serverProtocol": "HTTP"}
LIFECYCLE_CONFIGURATION: Final = {
    "idleRuntimeSessionTimeout": 900,
    "maxLifetime": 3600,
}
METADATA_CONFIGURATION: Final = {"requireMMDSV2": True}
CONTROLLER_MODEL_ID: Final = "us.amazon.nova-2-lite-v1:0"
GEMMA_MODEL_ID: Final = "google.gemma-3-27b-it"
GEMMA_CONFIG_PATH: Final = "/var/task/config/bedrock/google_gemma_3_27b_it.json"
GEMMA_CONFIG_FINGERPRINT: Final = "f036b77edad91d9923f844d0f4db9725b89574d698cc5ce6fcdee23101f9e929"
PRODUCT_PROFILE_ID: Final = "gildan_64000_swiftpod"
PRODUCT_PROFILE_VERSION: Final = "2"
PRODUCT_PROFILE_PATH: Final = "/var/task/config/product_profiles/gildan_64000_swiftpod.json"
PRODUCT_PROFILE_FINGERPRINT: Final = (
    "5de1257141cfdacb1731f68bb9113712957483b33d5b0f7115afdba86eb7476c"
)

EVIDENCE_DIRECTORY: Final = Path("evidence")
OBJECT_EVIDENCE_FILE: Final = EVIDENCE_DIRECTORY / "agentcore-s3-object-evidence.json"
PREDECESSOR_EVIDENCE_FILE: Final = EVIDENCE_DIRECTORY / "agentcore-runtime-v3-observation.json"
ROLE_EVIDENCE_FILE: Final = EVIDENCE_DIRECTORY / "agentcore-runtime-role-v4-observation.json"
TARGET_EVIDENCE_FILE: Final = EVIDENCE_DIRECTORY / "agentcore-runtime-v4-update-evidence.json"
OUTPUT_DIRECTORY: Final = Path("agentcore-v4-deployment")
RUNTIME_UPDATE_FILE: Final = OUTPUT_DIRECTORY / "update-agent-runtime-v4.local.json"
RUNTIME_MANIFEST_FILE: Final = OUTPUT_DIRECTORY / "runtime-update-manifest.local.json"
ENDPOINT_CREATE_FILE: Final = OUTPUT_DIRECTORY / "create-agent-runtime-endpoint-v4.local.json"
ENDPOINT_MANIFEST_FILE: Final = OUTPUT_DIRECTORY / "endpoint-create-manifest.local.json"

PREDECESSOR_EVIDENCE_FORMAT: Final = "mr-lister-phase6-agentcore-v3-observation-v1"
ROLE_EVIDENCE_FORMAT: Final = "mr-lister-phase6-agentcore-runtime-role-v4-observation-v1"
TARGET_EVIDENCE_FORMAT: Final = "mr-lister-phase6-agentcore-v4-update-evidence-v1"
RUNTIME_MANIFEST_FORMAT: Final = "mr-lister-phase6-agentcore-v4-update-render-v1"
ENDPOINT_MANIFEST_FORMAT: Final = "mr-lister-phase6-agentcore-v4-endpoint-render-v1"

MAX_OBSERVATION_AGE: Final = timedelta(minutes=15)
MAX_FUTURE_SKEW: Final = timedelta(minutes=2)
_HOTFIX_ROOT = re.compile(
    rf"^phase6-walkthrough-hotfix-{SOURCE_COMMIT[:7]}-[0-9]{{8}}T[0-9]{{6}}Z$"
)
_HEX_64 = re.compile(r"^[a-f0-9]{64}$")
_ROLE_ID = re.compile(r"^AROA[A-Z0-9]{12,64}$")
_ENDPOINT_ID = re.compile(r"^[A-Za-z0-9][-A-Za-z0-9_]{1,63}$")
_PLACEHOLDER = re.compile(
    r"<[A-Z][A-Z0-9_]*>|\$\{[^}\r\n]+}|__[A-Z][A-Z0-9_]*__|"
    r"\b(?:PLACEHOLDER|REPLACE_ME|CHANGEME)\b",
    re.IGNORECASE,
)
_GENERIC_ERROR: Final = "Phase 6 AgentCore walkthrough hotfix update input is invalid"


class Phase6AgentCoreWalkthroughHotfixUpdateError(RuntimeError):
    """A value-free failure for incomplete, drifting, stale, or unsafe hotfix input."""


@dataclass(frozen=True, slots=True)
class VerifiedObservation:
    """One canonical, recent observation joined by its exact byte digest."""

    captured_at: datetime
    evidence_sha256: str

    def __post_init__(self) -> None:
        if (
            not isinstance(self.captured_at, datetime)
            or self.captured_at.tzinfo is None
            or self.captured_at.utcoffset() != UTC.utcoffset(self.captured_at)
            or _HEX_64.fullmatch(self.evidence_sha256) is None
        ):
            raise Phase6AgentCoreWalkthroughHotfixUpdateError(_GENERIC_ERROR)


@dataclass(frozen=True, slots=True)
class HotfixContext:
    """Verified local archive and exact remote S3 object identity."""

    hotfix_root: Path
    archive: VerifiedAgentCoreArchive
    remote: VerifiedPhase6S3ReleaseObject


def _environment(release_fingerprint: str) -> dict[str, str]:
    return {
        "AWS_REGION": REGION,
        "MR_LISTER_ARTIFACT_BUCKET": ARTIFACT_BUCKET,
        "MR_LISTER_AWS_ACCOUNT_ID": ACCOUNT_ID,
        "MR_LISTER_ENVIRONMENT": ENVIRONMENT,
        "MR_LISTER_GEMMA_CONFIG_FINGERPRINT": GEMMA_CONFIG_FINGERPRINT,
        "MR_LISTER_GEMMA_CONFIG_PATH": GEMMA_CONFIG_PATH,
        "MR_LISTER_PRODUCT_PROFILE_FINGERPRINT": PRODUCT_PROFILE_FINGERPRINT,
        "MR_LISTER_PRODUCT_PROFILE_ID": PRODUCT_PROFILE_ID,
        "MR_LISTER_PRODUCT_PROFILE_PATH": PRODUCT_PROFILE_PATH,
        "MR_LISTER_PRODUCT_PROFILE_VERSION": PRODUCT_PROFILE_VERSION,
        "MR_LISTER_RELEASE_FINGERPRINT": release_fingerprint,
        "MR_LISTER_STATE_TABLE": "mr-lister-phase6-dev",
        "MR_LISTER_STRANDS_CONTROLLER_MODEL_ID": CONTROLLER_MODEL_ID,
    }


def verify_phase6_agentcore_walkthrough_hotfix_context(
    hotfix_root: Path,
    *,
    repository_root: Path = ROOT,
) -> HotfixContext:
    """Verify the source-bound hotfix root, sealed archive, config, and remote object proof."""

    try:
        repository, closure = _resolve_hotfix_root(repository_root, hotfix_root)
        deployment_root = closure / "phase6-deployment"
        artifact_root = closure / "phase6-artifacts"
        binding = _binding()
        archive = verify_phase6_agentcore_direct_codezip_artifact(
            binding,
            deployment_root=deployment_root,
            artifact_root=artifact_root,
        )
        if archive.sha256 != HOTFIX_ARCHIVE_SHA256 or archive.size_bytes != HOTFIX_ARCHIVE_SIZE:
            raise ValueError
        _verify_embedded_configuration(deployment_root / "agentcore")
        evidence_path = closure / OBJECT_EVIDENCE_FILE
        _require_private_file(repository, closure, evidence_path)
        remote = verify_phase6_s3_release_object_evidence(
            Phase6S3ReleaseObjectExpectation(
                account_id=ACCOUNT_ID,
                region=REGION,
                environment=ENVIRONMENT,
                component="agentcore",
                release_fingerprint=HOTFIX_RELEASE_FINGERPRINT,
                archive_sha256=HOTFIX_ARCHIVE_SHA256,
                size_bytes=HOTFIX_ARCHIVE_SIZE,
            ),
            evidence_path=evidence_path,
        )
        if (
            remote.account_id != ACCOUNT_ID
            or remote.region != REGION
            or remote.bucket != ARTIFACT_BUCKET
            or remote.key != HOTFIX_KEY
            or remote.release_fingerprint != HOTFIX_RELEASE_FINGERPRINT
            or remote.archive_sha256 != HOTFIX_ARCHIVE_SHA256
            or remote.size_bytes != HOTFIX_ARCHIVE_SIZE
        ):
            raise ValueError
        return HotfixContext(hotfix_root=closure, archive=archive, remote=remote)
    except Phase6AgentCoreWalkthroughHotfixUpdateError:
        raise
    except Exception:
        raise Phase6AgentCoreWalkthroughHotfixUpdateError(_GENERIC_ERROR) from None


def verify_phase6_agentcore_v3_observation(
    path: Path,
    *,
    now: datetime | None = None,
) -> VerifiedObservation:
    """Verify a recent exact READY v3 runtime and immutable v3 endpoint readback."""

    try:
        raw, evidence = _load_canonical_mapping(path)
        if set(evidence) != {
            "accountId",
            "capturedAt",
            "format",
            "getAgentRuntime",
            "getAgentRuntimeEndpoint",
            "region",
        }:
            raise ValueError
        captured = _fresh_timestamp(evidence.get("capturedAt"), now=now)
        if (
            evidence.get("format") != PREDECESSOR_EVIDENCE_FORMAT
            or evidence.get("accountId") != ACCOUNT_ID
            or evidence.get("region") != REGION
        ):
            raise ValueError
        runtime = _require_operation(
            evidence.get("getAgentRuntime"),
            request={"agentRuntimeId": RUNTIME_ID, "agentRuntimeVersion": CURRENT_VERSION},
        )
        _validate_runtime_response(
            runtime,
            version=CURRENT_VERSION,
            release_fingerprint=PREDECESSOR_RELEASE_FINGERPRINT,
            archive_sha256=PREDECESSOR_ARCHIVE_SHA256,
            version_id=PREDECESSOR_S3_VERSION_ID,
            required_status="READY",
        )
        endpoint = _require_operation(
            evidence.get("getAgentRuntimeEndpoint"),
            request={"agentRuntimeId": RUNTIME_ID, "endpointName": CURRENT_ENDPOINT_NAME},
        )
        _validate_endpoint_response(
            endpoint,
            name=CURRENT_ENDPOINT_NAME,
            version=CURRENT_VERSION,
            arn=CURRENT_ENDPOINT_ARN,
        )
        return VerifiedObservation(captured_at=captured, evidence_sha256=sha256(raw).hexdigest())
    except Phase6AgentCoreWalkthroughHotfixUpdateError:
        raise
    except Exception:
        raise Phase6AgentCoreWalkthroughHotfixUpdateError(_GENERIC_ERROR) from None


def verify_phase6_agentcore_runtime_role_observation(
    path: Path,
    *,
    now: datetime | None = None,
    repository_root: Path = ROOT,
) -> VerifiedObservation:
    """Verify the retained runtime role has only the reviewed target-release inline policy."""

    try:
        raw, evidence = _load_canonical_mapping(path)
        if set(evidence) != {
            "accountId",
            "capturedAt",
            "format",
            "getRole",
            "getRolePolicy",
            "listAttachedRolePolicies",
            "listRolePolicies",
            "region",
        }:
            raise ValueError
        captured = _fresh_timestamp(evidence.get("capturedAt"), now=now)
        if (
            evidence.get("format") != ROLE_EVIDENCE_FORMAT
            or evidence.get("accountId") != ACCOUNT_ID
            or evidence.get("region") != REGION
        ):
            raise ValueError
        expected_trust, expected_policy, expected_tags = _expected_role_documents(repository_root)
        role_response = _require_operation(evidence.get("getRole"), request={"RoleName": ROLE_NAME})
        role_document = _require_mapping(
            _require_exact_mapping(role_response, {"Role"}).get("Role")
        )
        required_role_keys = {
            "Arn",
            "AssumeRolePolicyDocument",
            "CreateDate",
            "MaxSessionDuration",
            "Path",
            "RoleId",
            "RoleName",
            "Tags",
        }
        if (
            not required_role_keys <= set(role_document)
            or set(role_document) - required_role_keys - {"Description", "RoleLastUsed"}
            or "PermissionsBoundary" in role_document
            or role_document.get("Arn") != ROLE_ARN
            or role_document.get("RoleName") != ROLE_NAME
            or role_document.get("Path") != "/"
            or role_document.get("MaxSessionDuration") != 3600
            or _ROLE_ID.fullmatch(str(role_document.get("RoleId"))) is None
            or role_document.get("AssumeRolePolicyDocument") != expected_trust
        ):
            raise ValueError
        observed_tags = role_document.get("Tags")
        if not isinstance(observed_tags, list) or any(
            not isinstance(tag, Mapping) or set(tag) != {"Key", "Value"} for tag in observed_tags
        ):
            raise ValueError
        observed_tag_map = {tag["Key"]: tag["Value"] for tag in observed_tags}
        expected_tag_map = {tag["Key"]: tag["Value"] for tag in expected_tags}
        if len(observed_tag_map) != len(observed_tags) or observed_tag_map != expected_tag_map:
            raise ValueError
        _parse_timestamp(role_document.get("CreateDate"))

        inline = _require_operation(
            evidence.get("listRolePolicies"), request={"RoleName": ROLE_NAME}
        )
        if inline != {"IsTruncated": False, "PolicyNames": [ROLE_POLICY_NAME]}:
            raise ValueError
        attached = _require_operation(
            evidence.get("listAttachedRolePolicies"), request={"RoleName": ROLE_NAME}
        )
        if attached != {"AttachedPolicies": [], "IsTruncated": False}:
            raise ValueError
        policy = _require_operation(
            evidence.get("getRolePolicy"),
            request={"PolicyName": ROLE_POLICY_NAME, "RoleName": ROLE_NAME},
        )
        if policy != {
            "PolicyDocument": expected_policy,
            "PolicyName": ROLE_POLICY_NAME,
            "RoleName": ROLE_NAME,
        }:
            raise ValueError
        _validate_exact_role_s3_boundary(expected_policy)
        return VerifiedObservation(captured_at=captured, evidence_sha256=sha256(raw).hexdigest())
    except Phase6AgentCoreWalkthroughHotfixUpdateError:
        raise
    except Exception:
        raise Phase6AgentCoreWalkthroughHotfixUpdateError(_GENERIC_ERROR) from None


def render_phase6_agentcore_runtime_update_documents(
    hotfix_root: Path,
    *,
    repository_root: Path = ROOT,
    now: datetime | None = None,
) -> dict[Path, bytes]:
    """Render exact v4 update bytes after every local, S3, v3, and role prerequisite verifies."""

    try:
        context = verify_phase6_agentcore_walkthrough_hotfix_context(
            hotfix_root, repository_root=repository_root
        )
        predecessor_path = context.hotfix_root / PREDECESSOR_EVIDENCE_FILE
        role_path = context.hotfix_root / ROLE_EVIDENCE_FILE
        _require_private_file(repository_root, context.hotfix_root, predecessor_path)
        _require_private_file(repository_root, context.hotfix_root, role_path)
        predecessor = verify_phase6_agentcore_v3_observation(predecessor_path, now=now)
        role = verify_phase6_agentcore_runtime_role_observation(
            role_path,
            now=now,
            repository_root=repository_root,
        )
        runtime_input = _runtime_update_input(context, predecessor, role)
        runtime_bytes = _canonical_json(runtime_input)
        manifest = {
            "artifact": {
                "archiveSHA256": context.archive.sha256,
                "descriptorSHA256": context.archive.descriptor_sha256,
                "s3Bucket": context.remote.bucket,
                "s3Key": context.remote.key,
                "s3VersionId": context.remote.version_id,
                "sizeBytes": context.archive.size_bytes,
            },
            "authorization": "BLOCKED_UNTIL_SEPARATELY_REVIEWED",
            "binding": _binding_document(),
            "documents": {RUNTIME_UPDATE_FILE.as_posix(): sha256(runtime_bytes).hexdigest()},
            "format": RUNTIME_MANIFEST_FORMAT,
            "preconditions": {
                "exactS3ObjectEvidenceSHA256": context.remote.evidence_sha256,
                "readyRuntimeV3EvidenceSHA256": predecessor.evidence_sha256,
                "runtimeRoleEvidenceSHA256": role.evidence_sha256,
            },
            "requiredNextEvidence": TARGET_EVIDENCE_FILE.as_posix(),
        }
        documents = {
            RUNTIME_MANIFEST_FILE: _canonical_json(manifest),
            RUNTIME_UPDATE_FILE: runtime_bytes,
        }
        _validate_runtime_documents(context, predecessor, role, documents)
        _reject_unresolved(documents)
        return dict(sorted(documents.items(), key=lambda item: item[0].as_posix()))
    except Phase6AgentCoreWalkthroughHotfixUpdateError:
        raise
    except Exception:
        raise Phase6AgentCoreWalkthroughHotfixUpdateError(_GENERIC_ERROR) from None


def verify_phase6_agentcore_v4_update_evidence(
    path: Path,
    *,
    runtime_documents: Mapping[Path, bytes],
    context: HotfixContext,
    predecessor: VerifiedObservation,
    role: VerifiedObservation,
    now: datetime | None = None,
) -> VerifiedObservation:
    """Verify update response and READY v4 readback against the exact rendered update bytes."""

    try:
        raw, evidence = _load_canonical_mapping(path)
        if set(evidence) != {
            "accountId",
            "capturedAt",
            "format",
            "getAgentRuntime",
            "objectEvidenceSHA256",
            "predecessorEvidenceSHA256",
            "region",
            "roleEvidenceSHA256",
            "runtimeUpdateManifestSHA256",
            "updateAgentRuntime",
        }:
            raise ValueError
        captured = _fresh_timestamp(evidence.get("capturedAt"), now=now)
        runtime_bytes = runtime_documents[RUNTIME_UPDATE_FILE]
        manifest_bytes = runtime_documents[RUNTIME_MANIFEST_FILE]
        if (
            evidence.get("format") != TARGET_EVIDENCE_FORMAT
            or evidence.get("accountId") != ACCOUNT_ID
            or evidence.get("region") != REGION
            or evidence.get("objectEvidenceSHA256") != context.remote.evidence_sha256
            or evidence.get("predecessorEvidenceSHA256") != predecessor.evidence_sha256
            or evidence.get("roleEvidenceSHA256") != role.evidence_sha256
            or evidence.get("runtimeUpdateManifestSHA256") != sha256(manifest_bytes).hexdigest()
        ):
            raise ValueError
        update = _require_mapping(evidence.get("updateAgentRuntime"))
        if (
            set(update) != {"inputSHA256", "response"}
            or update.get("inputSHA256") != sha256(runtime_bytes).hexdigest()
        ):
            raise ValueError
        _validate_update_response(update.get("response"))
        runtime = _require_operation(
            evidence.get("getAgentRuntime"),
            request={"agentRuntimeId": RUNTIME_ID, "agentRuntimeVersion": TARGET_VERSION},
        )
        _validate_runtime_response(
            runtime,
            version=TARGET_VERSION,
            release_fingerprint=HOTFIX_RELEASE_FINGERPRINT,
            archive_sha256=HOTFIX_ARCHIVE_SHA256,
            version_id=context.remote.version_id,
            required_status="READY",
        )
        return VerifiedObservation(captured_at=captured, evidence_sha256=sha256(raw).hexdigest())
    except Phase6AgentCoreWalkthroughHotfixUpdateError:
        raise
    except Exception:
        raise Phase6AgentCoreWalkthroughHotfixUpdateError(_GENERIC_ERROR) from None


def render_phase6_agentcore_endpoint_documents(
    hotfix_root: Path,
    *,
    repository_root: Path = ROOT,
    now: datetime | None = None,
) -> dict[Path, bytes]:
    """Render immutable v4 endpoint bytes only after the exact update is observed READY."""

    try:
        context = verify_phase6_agentcore_walkthrough_hotfix_context(
            hotfix_root, repository_root=repository_root
        )
        predecessor_path = context.hotfix_root / PREDECESSOR_EVIDENCE_FILE
        role_path = context.hotfix_root / ROLE_EVIDENCE_FILE
        target_path = context.hotfix_root / TARGET_EVIDENCE_FILE
        for path in (predecessor_path, role_path, target_path):
            _require_private_file(repository_root, context.hotfix_root, path)
        predecessor = verify_phase6_agentcore_v3_observation(predecessor_path, now=now)
        role = verify_phase6_agentcore_runtime_role_observation(
            role_path,
            now=now,
            repository_root=repository_root,
        )
        runtime_documents = render_phase6_agentcore_runtime_update_documents(
            context.hotfix_root,
            repository_root=repository_root,
            now=now,
        )
        _verify_written_documents(
            context.hotfix_root,
            runtime_documents,
            exact_entries=(
                set(runtime_documents),
                {
                    *runtime_documents,
                    ENDPOINT_CREATE_FILE,
                    ENDPOINT_MANIFEST_FILE,
                },
            ),
        )
        target = verify_phase6_agentcore_v4_update_evidence(
            target_path,
            runtime_documents=runtime_documents,
            context=context,
            predecessor=predecessor,
            role=role,
            now=now,
        )
        endpoint_input = {
            "agentRuntimeId": RUNTIME_ID,
            "agentRuntimeVersion": TARGET_VERSION,
            "clientToken": _client_token(
                "endpoint",
                context.remote,
                predecessor.evidence_sha256,
                role.evidence_sha256,
                target.evidence_sha256,
            ),
            "description": "Immutable Phase 6 dev endpoint pinned to runtime version 4",
            "name": TARGET_ENDPOINT_NAME,
            "tags": _release_tags(),
        }
        endpoint_bytes = _canonical_json(endpoint_input)
        manifest = {
            "authorization": "BLOCKED_UNTIL_SEPARATELY_REVIEWED",
            "binding": _binding_document(),
            "documents": {ENDPOINT_CREATE_FILE.as_posix(): sha256(endpoint_bytes).hexdigest()},
            "format": ENDPOINT_MANIFEST_FORMAT,
            "runtimeUpdateEvidenceSHA256": target.evidence_sha256,
            "runtimeUpdateManifestSHA256": sha256(
                runtime_documents[RUNTIME_MANIFEST_FILE]
            ).hexdigest(),
        }
        documents = {
            ENDPOINT_CREATE_FILE: endpoint_bytes,
            ENDPOINT_MANIFEST_FILE: _canonical_json(manifest),
        }
        _validate_endpoint_documents(context, predecessor, role, target, documents)
        _reject_unresolved(documents)
        return dict(sorted(documents.items(), key=lambda item: item[0].as_posix()))
    except Phase6AgentCoreWalkthroughHotfixUpdateError:
        raise
    except Exception:
        raise Phase6AgentCoreWalkthroughHotfixUpdateError(_GENERIC_ERROR) from None


def write_phase6_agentcore_runtime_update_documents(
    hotfix_root: Path,
    *,
    repository_root: Path = ROOT,
    now: datetime | None = None,
) -> tuple[Path, ...]:
    """Create the private runtime-update outputs exclusively; never overwrite."""

    documents = render_phase6_agentcore_runtime_update_documents(
        hotfix_root, repository_root=repository_root, now=now
    )
    _, closure = _resolve_hotfix_root(repository_root, hotfix_root)
    return _write_new_documents(closure, documents, require_output_absent=True)


def verify_phase6_agentcore_runtime_update_documents(
    hotfix_root: Path,
    *,
    repository_root: Path = ROOT,
    now: datetime | None = None,
) -> None:
    """Reject any drift in the private runtime-update outputs or their prerequisites."""

    documents = render_phase6_agentcore_runtime_update_documents(
        hotfix_root, repository_root=repository_root, now=now
    )
    _, closure = _resolve_hotfix_root(repository_root, hotfix_root)
    _verify_written_documents(closure, documents, exact_entries=set(documents))


def write_phase6_agentcore_endpoint_documents(
    hotfix_root: Path,
    *,
    repository_root: Path = ROOT,
    now: datetime | None = None,
) -> tuple[Path, ...]:
    """Create endpoint outputs exclusively after re-verifying the complete runtime stage."""

    documents = render_phase6_agentcore_endpoint_documents(
        hotfix_root, repository_root=repository_root, now=now
    )
    _, closure = _resolve_hotfix_root(repository_root, hotfix_root)
    return _write_new_documents(closure, documents, require_output_absent=False)


def verify_phase6_agentcore_endpoint_documents(
    hotfix_root: Path,
    *,
    repository_root: Path = ROOT,
    now: datetime | None = None,
) -> None:
    """Reject drift in both runtime and endpoint outputs and all joined evidence."""

    endpoint = render_phase6_agentcore_endpoint_documents(
        hotfix_root, repository_root=repository_root, now=now
    )
    runtime = render_phase6_agentcore_runtime_update_documents(
        hotfix_root, repository_root=repository_root, now=now
    )
    _, closure = _resolve_hotfix_root(repository_root, hotfix_root)
    expected = {**runtime, **endpoint}
    _verify_written_documents(closure, expected, exact_entries=set(expected))


def _binding() -> Phase6AgentCoreDirectCodeZipBinding:
    return Phase6AgentCoreDirectCodeZipBinding(
        account_id=ACCOUNT_ID,
        release_fingerprint=HOTFIX_RELEASE_FINGERPRINT,
        agentcore_archive_sha256=HOTFIX_ARCHIVE_SHA256,
    )


def _runtime_update_input(
    context: HotfixContext,
    predecessor: VerifiedObservation,
    role: VerifiedObservation,
) -> dict[str, object]:
    return {
        "agentRuntimeArtifact": {
            "codeConfiguration": {
                "code": {
                    "s3": {
                        "bucket": ARTIFACT_BUCKET,
                        "prefix": HOTFIX_KEY,
                        "versionId": context.remote.version_id,
                    }
                },
                "entryPoint": ENTRY_POINT,
                "runtime": PYTHON_RUNTIME,
            }
        },
        "agentRuntimeId": RUNTIME_ID,
        "clientToken": _client_token(
            "runtime",
            context.remote,
            predecessor.evidence_sha256,
            role.evidence_sha256,
        ),
        "description": "Release-bound Phase 6 Strands preparation runtime",
        "environmentVariables": _environment(HOTFIX_RELEASE_FINGERPRINT),
        "lifecycleConfiguration": LIFECYCLE_CONFIGURATION,
        "metadataConfiguration": METADATA_CONFIGURATION,
        "networkConfiguration": NETWORK_CONFIGURATION,
        "protocolConfiguration": PROTOCOL_CONFIGURATION,
        "roleArn": ROLE_ARN,
    }


def _binding_document() -> dict[str, object]:
    return {
        "accountId": ACCOUNT_ID,
        "archiveSHA256": HOTFIX_ARCHIVE_SHA256,
        "controllerModelId": CONTROLLER_MODEL_ID,
        "currentEndpointArn": CURRENT_ENDPOINT_ARN,
        "currentEndpointName": CURRENT_ENDPOINT_NAME,
        "currentVersion": CURRENT_VERSION,
        "gemmaConfigFingerprint": GEMMA_CONFIG_FINGERPRINT,
        "gemmaModelId": GEMMA_MODEL_ID,
        "lifecycleConfiguration": LIFECYCLE_CONFIGURATION,
        "metadataConfiguration": METADATA_CONFIGURATION,
        "networkConfiguration": NETWORK_CONFIGURATION,
        "productProfileFingerprint": PRODUCT_PROFILE_FINGERPRINT,
        "productProfileId": PRODUCT_PROFILE_ID,
        "productProfileVersion": PRODUCT_PROFILE_VERSION,
        "protocolConfiguration": PROTOCOL_CONFIGURATION,
        "predecessorArchiveSHA256": PREDECESSOR_ARCHIVE_SHA256,
        "predecessorReleaseFingerprint": PREDECESSOR_RELEASE_FINGERPRINT,
        "predecessorS3VersionId": PREDECESSOR_S3_VERSION_ID,
        "region": REGION,
        "releaseFingerprint": HOTFIX_RELEASE_FINGERPRINT,
        "roleArn": ROLE_ARN,
        "runtimeArn": RUNTIME_ARN,
        "runtimeId": RUNTIME_ID,
        "sourceCommit": SOURCE_COMMIT,
        "targetEndpointArn": TARGET_ENDPOINT_ARN,
        "targetEndpointName": TARGET_ENDPOINT_NAME,
        "targetVersion": TARGET_VERSION,
    }


def _release_tags() -> dict[str, str]:
    return {
        "DeploymentClass": "AGENTCORE_DIRECT_CODEZIP",
        "Environment": ENVIRONMENT,
        "Project": "MrLister",
        "ReleaseFingerprint": HOTFIX_RELEASE_FINGERPRINT,
    }


def _verify_embedded_configuration(agentcore_root: Path) -> None:
    gemma_path = agentcore_root / "config/bedrock/google_gemma_3_27b_it.json"
    profile_path = agentcore_root / "config/product_profiles/gildan_64000_swiftpod.json"
    for path in (gemma_path, profile_path):
        if path.is_symlink() or not path.is_file():
            raise ValueError
    gemma_raw = gemma_path.read_bytes()
    gemma = json.loads(gemma_raw, object_pairs_hook=_unique_json_object)
    if (
        sha256(gemma_raw).hexdigest() != GEMMA_CONFIG_FINGERPRINT
        or gemma.get("model_id") != GEMMA_MODEL_ID
        or gemma.get("region") != REGION
        or gemma.get("output_mode") != "native_json_schema"
    ):
        raise ValueError
    profile = FilesystemReviewProductAuthority(profile_directory=profile_path.parent).get_exact(
        profile_id=PRODUCT_PROFILE_ID,
        profile_version=int(PRODUCT_PROFILE_VERSION),
    )
    if profile.fingerprint != PRODUCT_PROFILE_FINGERPRINT:
        raise ValueError


def _expected_role_documents(
    repository_root: Path,
) -> tuple[Mapping[str, object], Mapping[str, object], list[dict[str, str]]]:
    repository = repository_root.resolve(strict=True)
    template_path = repository / "infra/agentcore/mrlisterphase6/direct-codezip-bootstrap.json"
    if template_path.is_symlink() or not template_path.is_file():
        raise ValueError
    template = json.loads(template_path.read_bytes(), object_pairs_hook=_unique_json_object)
    properties = template["Resources"]["AgentCoreRuntimeExecutionRole"]["Properties"]
    if properties.get("RoleName") != ROLE_NAME or len(properties.get("Policies", [])) != 1:
        raise ValueError
    replacements = {
        "AWS::AccountId": ACCOUNT_ID,
        "AWS::Partition": "aws",
        "AgentCoreArchiveSha256": HOTFIX_ARCHIVE_SHA256,
        "ReleaseFingerprint": HOTFIX_RELEASE_FINGERPRINT,
    }
    trust = _resolve_template_value(properties["AssumeRolePolicyDocument"], replacements)
    policy = _resolve_template_value(properties["Policies"][0]["PolicyDocument"], replacements)
    tags = _resolve_template_value(properties["Tags"], replacements)
    if (
        not isinstance(trust, Mapping)
        or not isinstance(policy, Mapping)
        or not isinstance(tags, list)
    ):
        raise ValueError
    policy = _transition_role_policy(policy)
    return trust, policy, tags


def _resolve_template_value(value: object, replacements: Mapping[str, str]) -> object:
    if isinstance(value, list):
        return [_resolve_template_value(item, replacements) for item in value]
    if isinstance(value, dict):
        if set(value) == {"Ref"}:
            reference = value["Ref"]
            if not isinstance(reference, str) or reference not in replacements:
                raise ValueError
            return replacements[reference]
        if set(value) == {"Fn::Sub"}:
            source = value["Fn::Sub"]
            if not isinstance(source, str):
                raise ValueError
            rendered = source
            for name, replacement in replacements.items():
                rendered = rendered.replace(f"${{{name}}}", replacement)
            if "${" in rendered:
                raise ValueError
            return rendered
        return {key: _resolve_template_value(item, replacements) for key, item in value.items()}
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    raise ValueError


def _archive_read_statement(*, sid: str, key: str) -> dict[str, object]:
    return {
        "Action": ["s3:GetObject", "s3:GetObjectVersion"],
        "Effect": "Allow",
        "Resource": f"arn:aws:s3:::{ARTIFACT_BUCKET}/{key}",
        "Sid": sid,
    }


def _transition_role_policy(policy: Mapping[str, object]) -> Mapping[str, object]:
    """Pair the live v3 and candidate v4 archives without a multi-resource grant."""

    if set(policy) != {"Statement", "Version"} or policy.get("Version") != "2012-10-17":
        raise ValueError
    statements = policy.get("Statement")
    if not isinstance(statements, list):
        raise ValueError
    target_template = {
        "Action": ["s3:GetObject", "s3:GetObjectVersion"],
        "Effect": "Allow",
        "Resource": f"arn:aws:s3:::{ARTIFACT_BUCKET}/{HOTFIX_KEY}",
        "Sid": "ReadOnlyExactAgentCoreDeploymentArchive",
    }
    matches = [index for index, statement in enumerate(statements) if statement == target_template]
    if len(matches) != 1:
        raise ValueError
    index = matches[0]
    transition_statements = [
        *statements[:index],
        _archive_read_statement(
            sid="ReadOnlyPredecessorAgentCoreDeploymentArchive",
            key=PREDECESSOR_KEY,
        ),
        _archive_read_statement(
            sid="ReadOnlyTargetAgentCoreDeploymentArchive",
            key=HOTFIX_KEY,
        ),
        *statements[index + 1 :],
    ]
    return {"Statement": transition_statements, "Version": "2012-10-17"}


def _validate_exact_role_s3_boundary(policy: Mapping[str, object]) -> None:
    statements = policy.get("Statement")
    if not isinstance(statements, list):
        raise ValueError
    s3_statements: list[Mapping[str, object]] = []
    for value in statements:
        statement = _require_mapping(value)
        actions = statement.get("Action")
        action_values = actions if isinstance(actions, list) else [actions]
        if any(isinstance(action, str) and action.startswith("s3:") for action in action_values):
            s3_statements.append(statement)
    by_sid = {str(statement.get("Sid")): statement for statement in s3_statements}
    if len(by_sid) != len(s3_statements) or set(by_sid) != {
        "ReadOnlyPredecessorAgentCoreDeploymentArchive",
        "ReadOnlyPinnedPhase6SourceVersions",
        "ReadOnlyTargetAgentCoreDeploymentArchive",
    }:
        raise ValueError
    if by_sid["ReadOnlyPredecessorAgentCoreDeploymentArchive"] != _archive_read_statement(
        sid="ReadOnlyPredecessorAgentCoreDeploymentArchive",
        key=PREDECESSOR_KEY,
    ):
        raise ValueError
    if by_sid["ReadOnlyTargetAgentCoreDeploymentArchive"] != _archive_read_statement(
        sid="ReadOnlyTargetAgentCoreDeploymentArchive",
        key=HOTFIX_KEY,
    ):
        raise ValueError
    if by_sid["ReadOnlyPinnedPhase6SourceVersions"] != {
        "Action": "s3:GetObjectVersion",
        "Effect": "Allow",
        "Resource": (f"arn:aws:s3:::{ARTIFACT_BUCKET}/private/owners/*/jobs/*/source/source.png"),
        "Sid": "ReadOnlyPinnedPhase6SourceVersions",
    }:
        raise ValueError


def _validate_runtime_response(
    value: object,
    *,
    version: str,
    release_fingerprint: str,
    archive_sha256: str,
    version_id: str,
    required_status: str,
) -> None:
    document = _require_mapping(value)
    required = {
        "agentRuntimeArn",
        "agentRuntimeArtifact",
        "agentRuntimeId",
        "agentRuntimeName",
        "agentRuntimeVersion",
        "createdAt",
        "description",
        "environmentVariables",
        "lastUpdatedAt",
        "lifecycleConfiguration",
        "metadataConfiguration",
        "networkConfiguration",
        "protocolConfiguration",
        "roleArn",
        "status",
        "workloadIdentityDetails",
    }
    optional = {
        "authorizerConfiguration",
        "capacityProviderConfiguration",
        "failureReason",
        "filesystemConfigurations",
        "requestHeaderConfiguration",
    }
    expected_key = PREDECESSOR_KEY if version == CURRENT_VERSION else HOTFIX_KEY
    if (
        not required <= set(document)
        or set(document) - required - optional
        or document.get("agentRuntimeArn") != RUNTIME_ARN
        or document.get("agentRuntimeId") != RUNTIME_ID
        or document.get("agentRuntimeName") != RUNTIME_NAME
        or document.get("agentRuntimeVersion") != version
        or document.get("agentRuntimeArtifact")
        != {
            "codeConfiguration": {
                "code": {
                    "s3": {
                        "bucket": ARTIFACT_BUCKET,
                        "prefix": expected_key,
                        "versionId": version_id,
                    }
                },
                "entryPoint": ENTRY_POINT,
                "runtime": PYTHON_RUNTIME,
            }
        }
        or document.get("description") != "Release-bound Phase 6 Strands preparation runtime"
        or document.get("environmentVariables") != _environment(release_fingerprint)
        or document.get("lifecycleConfiguration") != LIFECYCLE_CONFIGURATION
        or document.get("metadataConfiguration") != METADATA_CONFIGURATION
        or document.get("networkConfiguration") != NETWORK_CONFIGURATION
        or document.get("protocolConfiguration") != PROTOCOL_CONFIGURATION
        or document.get("roleArn") != ROLE_ARN
        or document.get("status") != required_status
        or document.get("failureReason") not in (None, "")
        or document.get("authorizerConfiguration") is not None
        or document.get("capacityProviderConfiguration") is not None
        or document.get("filesystemConfigurations") not in (None, [])
        or document.get("requestHeaderConfiguration") not in (None, {"requestHeaderAllowlist": []})
        or archive_sha256 not in str(document["agentRuntimeArtifact"])
    ):
        raise ValueError
    _parse_timestamp(document.get("createdAt"))
    _parse_timestamp(document.get("lastUpdatedAt"))
    identity = _require_exact_mapping(
        document.get("workloadIdentityDetails"), {"workloadIdentityArn"}
    )
    identity_arn = identity.get("workloadIdentityArn")
    if (
        not isinstance(identity_arn, str)
        or not identity_arn.startswith(f"arn:aws:bedrock-agentcore:{REGION}:{ACCOUNT_ID}:")
        or _PLACEHOLDER.search(identity_arn)
    ):
        raise ValueError


def _validate_endpoint_response(value: object, *, name: str, version: str, arn: str) -> None:
    document = _require_mapping(value)
    required = {
        "agentRuntimeArn",
        "agentRuntimeEndpointArn",
        "createdAt",
        "id",
        "lastUpdatedAt",
        "liveVersion",
        "name",
        "status",
    }
    optional = {"description", "failureReason", "targetVersion"}
    if (
        not required <= set(document)
        or set(document) - required - optional
        or document.get("agentRuntimeArn") != RUNTIME_ARN
        or document.get("agentRuntimeEndpointArn") != arn
        or document.get("name") != name
        or document.get("liveVersion") != version
        or ("targetVersion" in document and document.get("targetVersion") != version)
        or document.get("status") != "READY"
        or document.get("failureReason") not in (None, "")
        or _ENDPOINT_ID.fullmatch(str(document.get("id"))) is None
    ):
        raise ValueError
    _parse_timestamp(document.get("createdAt"))
    _parse_timestamp(document.get("lastUpdatedAt"))


def _validate_update_response(value: object) -> None:
    response = _require_mapping(value)
    required = {
        "agentRuntimeArn",
        "agentRuntimeId",
        "agentRuntimeVersion",
        "createdAt",
        "lastUpdatedAt",
        "status",
        "workloadIdentityDetails",
    }
    if (
        set(response) != required
        or response.get("agentRuntimeArn") != RUNTIME_ARN
        or response.get("agentRuntimeId") != RUNTIME_ID
        or response.get("agentRuntimeVersion") != TARGET_VERSION
        or response.get("status") not in {"UPDATING", "READY"}
    ):
        raise ValueError
    _parse_timestamp(response.get("createdAt"))
    _parse_timestamp(response.get("lastUpdatedAt"))
    identity = _require_exact_mapping(
        response.get("workloadIdentityDetails"), {"workloadIdentityArn"}
    )
    arn = identity.get("workloadIdentityArn")
    if not isinstance(arn, str) or not arn.startswith(
        f"arn:aws:bedrock-agentcore:{REGION}:{ACCOUNT_ID}:"
    ):
        raise ValueError


def _validate_runtime_documents(
    context: HotfixContext,
    predecessor: VerifiedObservation,
    role: VerifiedObservation,
    documents: Mapping[Path, bytes],
) -> None:
    if set(documents) != {RUNTIME_UPDATE_FILE, RUNTIME_MANIFEST_FILE}:
        raise ValueError
    runtime = json.loads(documents[RUNTIME_UPDATE_FILE], object_pairs_hook=_unique_json_object)
    if runtime != _runtime_update_input(context, predecessor, role):
        raise ValueError
    manifest = json.loads(documents[RUNTIME_MANIFEST_FILE], object_pairs_hook=_unique_json_object)
    if (
        manifest.get("format") != RUNTIME_MANIFEST_FORMAT
        or manifest.get("authorization") != "BLOCKED_UNTIL_SEPARATELY_REVIEWED"
        or manifest.get("binding") != _binding_document()
        or manifest.get("preconditions")
        != {
            "exactS3ObjectEvidenceSHA256": context.remote.evidence_sha256,
            "readyRuntimeV3EvidenceSHA256": predecessor.evidence_sha256,
            "runtimeRoleEvidenceSHA256": role.evidence_sha256,
        }
    ):
        raise ValueError


def _validate_endpoint_documents(
    context: HotfixContext,
    predecessor: VerifiedObservation,
    role: VerifiedObservation,
    target: VerifiedObservation,
    documents: Mapping[Path, bytes],
) -> None:
    if set(documents) != {ENDPOINT_CREATE_FILE, ENDPOINT_MANIFEST_FILE}:
        raise ValueError
    endpoint = json.loads(documents[ENDPOINT_CREATE_FILE], object_pairs_hook=_unique_json_object)
    if endpoint != {
        "agentRuntimeId": RUNTIME_ID,
        "agentRuntimeVersion": TARGET_VERSION,
        "clientToken": _client_token(
            "endpoint",
            context.remote,
            predecessor.evidence_sha256,
            role.evidence_sha256,
            target.evidence_sha256,
        ),
        "description": "Immutable Phase 6 dev endpoint pinned to runtime version 4",
        "name": TARGET_ENDPOINT_NAME,
        "tags": _release_tags(),
    }:
        raise ValueError
    manifest = json.loads(documents[ENDPOINT_MANIFEST_FILE], object_pairs_hook=_unique_json_object)
    if (
        manifest.get("format") != ENDPOINT_MANIFEST_FORMAT
        or manifest.get("authorization") != "BLOCKED_UNTIL_SEPARATELY_REVIEWED"
        or manifest.get("binding") != _binding_document()
        or manifest.get("runtimeUpdateEvidenceSHA256") != target.evidence_sha256
    ):
        raise ValueError


def _client_token(
    kind: str,
    remote: VerifiedPhase6S3ReleaseObject,
    *evidence_digests: str,
) -> str:
    if kind not in {"runtime", "endpoint"} or any(
        _HEX_64.fullmatch(value) is None for value in evidence_digests
    ):
        raise ValueError
    material = _canonical_json(
        {
            "accountId": ACCOUNT_ID,
            "archiveSHA256": HOTFIX_ARCHIVE_SHA256,
            "currentVersion": CURRENT_VERSION,
            "evidenceSHA256": list(evidence_digests),
            "format": f"mr-lister-phase6-agentcore-v4-{kind}-token-v1",
            "releaseFingerprint": HOTFIX_RELEASE_FINGERPRINT,
            "runtimeId": RUNTIME_ID,
            "s3VersionId": remote.version_id,
            "sourceCommit": SOURCE_COMMIT,
            "targetVersion": TARGET_VERSION,
        }
    )
    return f"mr-lister-phase6-{kind}-v4-{sha256(material).hexdigest()[:32]}"


def _resolve_hotfix_root(repository_root: Path, hotfix_root: Path) -> tuple[Path, Path]:
    if not isinstance(repository_root, Path) or not isinstance(hotfix_root, Path):
        raise Phase6AgentCoreWalkthroughHotfixUpdateError(_GENERIC_ERROR)
    repository = repository_root.resolve(strict=True)
    if repository.is_symlink() or not repository.is_dir():
        raise Phase6AgentCoreWalkthroughHotfixUpdateError(_GENERIC_ERROR)
    private = repository / ".mr_lister_private"
    if private.is_symlink() or not private.is_dir():
        raise Phase6AgentCoreWalkthroughHotfixUpdateError(_GENERIC_ERROR)
    if hotfix_root.is_symlink():
        raise Phase6AgentCoreWalkthroughHotfixUpdateError(_GENERIC_ERROR)
    closure = hotfix_root.resolve(strict=True)
    if (
        closure.parent != private.resolve(strict=True)
        or _HOTFIX_ROOT.fullmatch(closure.name) is None
        or not closure.is_dir()
    ):
        raise Phase6AgentCoreWalkthroughHotfixUpdateError(_GENERIC_ERROR)
    return repository, closure


def _require_private_file(repository_root: Path, hotfix_root: Path, path: Path) -> None:
    repository, closure = _resolve_hotfix_root(repository_root, hotfix_root)
    if not isinstance(path, Path):
        raise ValueError
    if path.is_symlink() or not path.is_file():
        raise ValueError
    resolved = path.resolve(strict=True)
    if resolved != path.absolute() or not resolved.is_relative_to(closure):
        raise ValueError
    current = closure
    for part in resolved.relative_to(closure).parts[:-1]:
        current = current / part
        if current.is_symlink():
            raise ValueError
    if not closure.is_relative_to(repository):
        raise ValueError


def _load_canonical_mapping(path: Path) -> tuple[bytes, Mapping[str, object]]:
    if (
        not isinstance(path, Path)
        or path.is_symlink()
        or not path.is_file()
        or any(parent.is_symlink() for parent in path.parents)
    ):
        raise ValueError
    raw = path.read_bytes()
    if not 1 <= len(raw) <= 2 * 1024 * 1024:
        raise ValueError
    document = json.loads(raw, object_pairs_hook=_unique_json_object)
    if not isinstance(document, Mapping) or _canonical_json(document) != raw:
        raise ValueError
    return raw, document


def _require_operation(value: object, *, request: Mapping[str, object]) -> object:
    operation = _require_exact_mapping(value, {"request", "response"})
    if operation.get("request") != request:
        raise ValueError
    return operation.get("response")


def _require_mapping(value: object) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError
    return value


def _require_exact_mapping(value: object, keys: set[str]) -> Mapping[str, object]:
    document = _require_mapping(value)
    if set(document) != keys:
        raise ValueError
    return document


def _fresh_timestamp(value: object, *, now: datetime | None) -> datetime:
    captured = _parse_timestamp(value)
    current = datetime.now(UTC) if now is None else now
    if (
        not isinstance(current, datetime)
        or current.tzinfo is None
        or current.utcoffset() != UTC.utcoffset(current)
        or captured < current - MAX_OBSERVATION_AGE
        or captured > current + MAX_FUTURE_SKEW
    ):
        raise ValueError
    return captured


def _parse_timestamp(value: object) -> datetime:
    if not isinstance(value, str) or not 1 <= len(value) <= 64 or value != value.strip():
        raise ValueError
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() != UTC.utcoffset(parsed):
        raise ValueError
    return parsed


def _canonical_json(value: object) -> bytes:
    return (
        json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=True,
            indent=2,
            separators=(",", ": "),
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")


def _unique_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError
        result[key] = value
    return result


def _reject_unresolved(documents: Mapping[Path, bytes]) -> None:
    for content in documents.values():
        text = content.decode("utf-8")
        if _PLACEHOLDER.search(text) or '"versionId": null' in text:
            raise ValueError


def _write_new_documents(
    hotfix_root: Path,
    documents: Mapping[Path, bytes],
    *,
    require_output_absent: bool,
) -> tuple[Path, ...]:
    try:
        output = hotfix_root / OUTPUT_DIRECTORY
        if require_output_absent and (output.exists() or output.is_symlink()):
            raise ValueError
        if not require_output_absent:
            if output.is_symlink() or not output.is_dir():
                raise ValueError
            existing = {path.relative_to(hotfix_root) for path in output.iterdir()}
            if existing != {RUNTIME_MANIFEST_FILE, RUNTIME_UPDATE_FILE}:
                raise ValueError
        destinations = tuple(hotfix_root / relative for relative in documents)
        if any(path.exists() or path.is_symlink() for path in destinations):
            raise ValueError
        output.mkdir(mode=0o700, parents=False, exist_ok=False) if require_output_absent else None
        output.chmod(0o700)
        for relative, content in documents.items():
            destination = hotfix_root / relative
            with destination.open("xb") as stream:
                stream.write(content)
            destination.chmod(0o600)
        return destinations
    except Phase6AgentCoreWalkthroughHotfixUpdateError:
        raise
    except Exception:
        raise Phase6AgentCoreWalkthroughHotfixUpdateError(_GENERIC_ERROR) from None


def _verify_written_documents(
    hotfix_root: Path,
    expected: Mapping[Path, bytes],
    *,
    exact_entries: set[Path] | tuple[set[Path], ...],
) -> None:
    try:
        output = hotfix_root / OUTPUT_DIRECTORY
        if output.is_symlink() or not output.is_dir():
            raise ValueError
        mode = stat.S_IMODE(output.stat().st_mode)
        entries = {path.relative_to(hotfix_root) for path in output.iterdir()}
        allowed_entries = exact_entries if isinstance(exact_entries, tuple) else (exact_entries,)
        if mode != 0o700 or entries not in allowed_entries:
            raise ValueError
        for relative, content in expected.items():
            path = hotfix_root / relative
            if (
                path.is_symlink()
                or not path.is_file()
                or stat.S_IMODE(path.stat().st_mode) != 0o600
                or path.read_bytes() != content
            ):
                raise ValueError
    except Phase6AgentCoreWalkthroughHotfixUpdateError:
        raise
    except Exception:
        raise Phase6AgentCoreWalkthroughHotfixUpdateError(_GENERIC_ERROR) from None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Render or verify the exact private Phase 6 AgentCore v3-to-v4 walkthrough "
            "hotfix inputs"
        )
    )
    parser.add_argument("--hotfix-root", required=True, type=Path)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--write-runtime-update", action="store_true")
    mode.add_argument("--verify-runtime-update", action="store_true")
    mode.add_argument("--write-endpoint", action="store_true")
    mode.add_argument("--verify-endpoint", action="store_true")
    arguments = parser.parse_args(argv)
    try:
        if arguments.write_runtime_update:
            write_phase6_agentcore_runtime_update_documents(arguments.hotfix_root)
        elif arguments.verify_runtime_update:
            verify_phase6_agentcore_runtime_update_documents(arguments.hotfix_root)
        elif arguments.write_endpoint:
            write_phase6_agentcore_endpoint_documents(arguments.hotfix_root)
        else:
            verify_phase6_agentcore_endpoint_documents(arguments.hotfix_root)
        return 0
    except Phase6AgentCoreWalkthroughHotfixUpdateError as error:
        parser.error(str(error))
    return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
