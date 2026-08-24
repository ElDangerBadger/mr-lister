"""Render fail-closed, immutable Phase 6 AgentCore direct-CodeZip inputs.

The module is deliberately offline.  It verifies the already sealed deployment archive and its
descriptor, renders AWS CLI input documents that bind an exact S3 VersionId, and never constructs
an AWS client or invokes the AgentCore packager.
"""

from __future__ import annotations

import argparse
import base64
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import Final

from tools.build_phase66_source_bundles import verify_phase6_deployment_artifacts
from tools.render_phase6_agentcore_deployment import (
    AGENTCORE_OUTPUT,
    Phase6AgentCoreDeploymentBinding,
    render_phase6_agentcore_deployment,
)
from tools.verify_phase6_s3_release_object import (
    EVIDENCE_FORMAT,
    Phase6S3ReleaseObjectExpectation,
    VerifiedPhase6S3ReleaseObject,
    verify_phase6_s3_release_object_evidence,
)

ROOT: Final = Path(__file__).resolve().parents[1]
DEFAULT_DEPLOYMENT_ROOT: Final = ROOT / ".mr_lister_private/phase6-deployment"
DEFAULT_ARTIFACT_ROOT: Final = ROOT / ".mr_lister_private/phase6-artifacts"

DIRECT_OUTPUT_ROOT: Final = Path("infra/agentcore/mrlisterphase6/direct-codezip")
UPLOAD_PLAN_OUTPUT: Final = DIRECT_OUTPUT_ROOT / "upload-binding-plan.local.json"
RUNTIME_CREATE_OUTPUT: Final = DIRECT_OUTPUT_ROOT / "create-agent-runtime.local.json"
AUTHORIZATION_RESIDUAL_OUTPUT: Final = DIRECT_OUTPUT_ROOT / "authorization-residuals.local.json"
RUNTIME_MANIFEST_OUTPUT: Final = DIRECT_OUTPUT_ROOT / "runtime-render-manifest.local.json"
ENDPOINT_CREATE_OUTPUT: Final = DIRECT_OUTPUT_ROOT / "create-agent-runtime-endpoint.local.json"
ENDPOINT_MANIFEST_OUTPUT: Final = DIRECT_OUTPUT_ROOT / "endpoint-render-manifest.local.json"
RUNTIME_V1_EVIDENCE_FORMAT: Final = "mr-lister-phase6-agentcore-runtime-v1-evidence-v1"

_REGION: Final = "us-west-2"
_ENVIRONMENT: Final = "dev"
_RUNTIME_NAME: Final = "mr_lister_phase6"
_RUNTIME_VERSION: Final = "1"
_ENDPOINT_NAME: Final = "phase6_v1_dev"
_ARCHIVE_FILENAME: Final = "phase6-agentcore.zip"
_DESCRIPTOR_FILENAME: Final = "deployment-descriptor.json"
_DEPLOYMENT_CLASS: Final = "AGENTCORE_DIRECT_CODEZIP"
_ROLE_NAME: Final = "mr-lister-phase6-agentcore-runtime-dev"
_ACCOUNT_ID = re.compile(r"^[0-9]{12}$")
_HEX_64 = re.compile(r"^[a-f0-9]{64}$")
_RUNTIME_ID = re.compile(r"^mr_lister_phase6-[A-Za-z0-9]{10}$")
_PLACEHOLDER = re.compile(
    r"<[A-Z][A-Z0-9_]*>|\$\{[^}\r\n]+}|__[A-Z][A-Z0-9_]*__|"
    r"\b(?:PLACEHOLDER|REPLACE_ME|CHANGEME)\b",
    re.IGNORECASE,
)
_GENERIC_ERROR: Final = "Phase 6 AgentCore direct-CodeZip deployment input is invalid"


class Phase6AgentCoreDirectCodeZipError(RuntimeError):
    """A value-free failure for invalid, drifting, or incompletely bound deployment input."""


@dataclass(frozen=True, slots=True)
class Phase6AgentCoreDirectCodeZipBinding:
    """Immutable local account, release, and archive identity."""

    account_id: str
    release_fingerprint: str
    agentcore_archive_sha256: str

    def __post_init__(self) -> None:
        try:
            values = (
                self.account_id,
                self.release_fingerprint,
                self.agentcore_archive_sha256,
            )
            if (
                not all(isinstance(value, str) for value in values)
                or _ACCOUNT_ID.fullmatch(self.account_id) is None
                or self.account_id == "0" * 12
                or _HEX_64.fullmatch(self.release_fingerprint) is None
                or self.release_fingerprint == "0" * 64
                or _HEX_64.fullmatch(self.agentcore_archive_sha256) is None
                or self.agentcore_archive_sha256 == "0" * 64
                or _PLACEHOLDER.search("\n".join(values))
            ):
                raise ValueError
        except Exception:
            raise Phase6AgentCoreDirectCodeZipError(_GENERIC_ERROR) from None

    @property
    def bucket(self) -> str:
        return f"mr-lister-phase6-artifacts-dev-{self.account_id}-us-west-2"

    @property
    def key(self) -> str:
        return (
            f"private/deployments/agentcore/releases/{self.release_fingerprint}/"
            f"phase6-agentcore-{self.agentcore_archive_sha256}.zip"
        )

    @property
    def role_arn(self) -> str:
        return f"arn:aws:iam::{self.account_id}:role/{_ROLE_NAME}"

    @property
    def tags(self) -> dict[str, str]:
        return {
            "DeploymentClass": _DEPLOYMENT_CLASS,
            "Environment": _ENVIRONMENT,
            "Project": "MrLister",
            "ReleaseFingerprint": self.release_fingerprint,
        }


@dataclass(frozen=True, slots=True)
class VerifiedAgentCoreArchive:
    """Observed local bytes after descriptor and packaged-release verification."""

    sha256: str
    size_bytes: int
    checksum_sha256_base64: str
    descriptor_sha256: str

    def __post_init__(self) -> None:
        if (
            _HEX_64.fullmatch(self.sha256) is None
            or not isinstance(self.size_bytes, int)
            or isinstance(self.size_bytes, bool)
            or self.size_bytes <= 0
            or not isinstance(self.checksum_sha256_base64, str)
            or not self.checksum_sha256_base64
            or _HEX_64.fullmatch(self.descriptor_sha256) is None
        ):
            raise Phase6AgentCoreDirectCodeZipError(_GENERIC_ERROR)


@dataclass(frozen=True, slots=True)
class VerifiedAgentCoreRuntimeV1:
    """One READY runtime v1 proven to be the result of the sealed create authority."""

    runtime_id: str
    runtime_arn: str
    evidence_sha256: str
    runtime_create_input_sha256: str
    runtime_render_manifest_sha256: str
    remote_object_evidence_sha256: str

    def __post_init__(self) -> None:
        try:
            _validate_runtime_id(self.runtime_id)
            if (
                not isinstance(self.runtime_arn, str)
                or not self.runtime_arn.endswith(f":runtime/{self.runtime_id}")
                or any(
                    _HEX_64.fullmatch(value) is None
                    for value in (
                        self.evidence_sha256,
                        self.runtime_create_input_sha256,
                        self.runtime_render_manifest_sha256,
                        self.remote_object_evidence_sha256,
                    )
                )
            ):
                raise ValueError
        except Exception:
            raise Phase6AgentCoreDirectCodeZipError(_GENERIC_ERROR) from None


def verify_phase6_agentcore_direct_codezip_artifact(
    binding: Phase6AgentCoreDirectCodeZipBinding,
    *,
    deployment_root: Path = DEFAULT_DEPLOYMENT_ROOT,
    artifact_root: Path = DEFAULT_ARTIFACT_ROOT,
) -> VerifiedAgentCoreArchive:
    """Verify extracted bundles, descriptor, and exact sealed AgentCore ZIP bytes."""

    try:
        if not isinstance(binding, Phase6AgentCoreDirectCodeZipBinding):
            raise ValueError
        if deployment_root.is_symlink() or artifact_root.is_symlink():
            raise ValueError
        deployment = deployment_root.resolve(strict=True)
        artifacts = artifact_root.resolve(strict=True)
        if deployment.name != "phase6-deployment" or artifacts.name != "phase6-artifacts":
            raise ValueError
        descriptor_path = artifacts / _DESCRIPTOR_FILENAME
        archive_path = artifacts / _ARCHIVE_FILENAME
        if (
            descriptor_path.is_symlink()
            or archive_path.is_symlink()
            or not descriptor_path.is_file()
            or not archive_path.is_file()
        ):
            raise ValueError

        descriptor = verify_phase6_deployment_artifacts(
            deployment,
            artifact_root=artifacts,
            verify_current_source=False,
        )
        if not isinstance(descriptor, dict) or descriptor.get("release_fingerprint") != (
            binding.release_fingerprint
        ):
            raise ValueError
        components = descriptor.get("components")
        if not isinstance(components, dict) or set(components) != {"agentcore", "lambda"}:
            raise ValueError
        agentcore = components.get("agentcore")
        if not isinstance(agentcore, dict) or set(agentcore) != {
            "archive",
            "architecture",
            "component",
            "deployment_manifest_sha256",
            "package_format",
            "runtime",
        }:
            raise ValueError
        archive = agentcore.get("archive")
        if not isinstance(archive, dict) or archive != {
            "path": _ARCHIVE_FILENAME,
            "sha256": binding.agentcore_archive_sha256,
            "size_bytes": archive_path.stat().st_size,
        }:
            raise ValueError
        if (
            agentcore.get("architecture") != "arm64"
            or agentcore.get("component") != "agentcore"
            or agentcore.get("package_format") != "zip"
            or agentcore.get("runtime") != "python3.12"
        ):
            raise ValueError

        archive_digest = _digest_file(archive_path)
        if archive_digest.hexdigest() != binding.agentcore_archive_sha256:
            raise ValueError
        descriptor_digest = _digest_file(descriptor_path).hexdigest()
        return VerifiedAgentCoreArchive(
            sha256=archive_digest.hexdigest(),
            size_bytes=archive_path.stat().st_size,
            checksum_sha256_base64=base64.b64encode(archive_digest.digest()).decode("ascii"),
            descriptor_sha256=descriptor_digest,
        )
    except Phase6AgentCoreDirectCodeZipError:
        raise
    except Exception:
        raise Phase6AgentCoreDirectCodeZipError(_GENERIC_ERROR) from None


def render_phase6_agentcore_upload_plan(
    binding: Phase6AgentCoreDirectCodeZipBinding,
    archive: VerifiedAgentCoreArchive,
) -> bytes:
    """Render the exact conditional-write, revocation, and readback contract."""

    try:
        if not isinstance(binding, Phase6AgentCoreDirectCodeZipBinding) or archive.sha256 != (
            binding.agentcore_archive_sha256
        ):
            raise ValueError
        expectation = _remote_expectation(binding, archive)
        plan: dict[str, object] = {
            "accountId": binding.account_id,
            "artifact": {
                "checksumSHA256Base64": archive.checksum_sha256_base64,
                "descriptorSHA256": archive.descriptor_sha256,
                "localPath": ".mr_lister_private/phase6-artifacts/phase6-agentcore.zip",
                "sha256": archive.sha256,
                "sizeBytes": archive.size_bytes,
            },
            "conditionalWrite": {
                "checksumAlgorithm": "SHA256",
                "expectedBucketOwner": binding.account_id,
                "ifNoneMatch": "*",
                "metadata": expectation.metadata,
                "required": True,
                "serverSideEncryption": "AES256",
            },
            "format": "mr-lister-phase6-agentcore-upload-binding-v1",
            "postUploadRenderStatus": "BLOCKED_UNTIL_CLOSED_OBJECT_EVIDENCE_VERIFIES",
            "region": _REGION,
            "releaseFingerprint": binding.release_fingerprint,
            "s3": {
                "bucket": binding.bucket,
                "key": binding.key,
            },
            "putObjectArguments": [
                "s3api",
                "put-object",
                "--region",
                _REGION,
                "--bucket",
                binding.bucket,
                "--key",
                binding.key,
                "--body",
                ".mr_lister_private/phase6-artifacts/phase6-agentcore.zip",
                "--expected-bucket-owner",
                binding.account_id,
                "--if-none-match",
                "*",
                "--checksum-algorithm",
                "SHA256",
                "--checksum-sha256",
                archive.checksum_sha256_base64,
                "--server-side-encryption",
                "AES256",
                "--metadata",
                json.dumps(expectation.metadata, separators=(",", ":"), sort_keys=True),
            ],
            "requiredClosedEvidence": {
                "bucketOwnershipArguments": [
                    "s3api",
                    "get-bucket-ownership-controls",
                    "--region",
                    _REGION,
                    "--bucket",
                    binding.bucket,
                    "--expected-bucket-owner",
                    binding.account_id,
                ],
                "bucketVersioningArguments": [
                    "s3api",
                    "get-bucket-versioning",
                    "--region",
                    _REGION,
                    "--bucket",
                    binding.bucket,
                    "--expected-bucket-owner",
                    binding.account_id,
                ],
                "byteIdentityRequirements": {
                    "exactVersionHeadRepeatedAfterUploadAuthorityRevocation": True,
                    "fullObjectChecksumSHA256Base64": archive.checksum_sha256_base64,
                    "metadata": expectation.metadata,
                    "serverSideEncryption": "AES256",
                    "sizeBytes": archive.size_bytes,
                },
                "collisionHygieneRequirements": {
                    "completeExactPrefixVersionListing": True,
                    "conditionalCreate": True,
                    "exactKeyDeleteAndDeleteVersionExplicitlyDenied": True,
                    "noDeleteMarkers": True,
                    "singletonCurrentVersion": True,
                    "uploadAuthorityDetachedAndAccessDenied": True,
                },
                "format": EVIDENCE_FORMAT,
                "getCallerIdentityArguments": ["sts", "get-caller-identity"],
                "headObjectArgumentsBeforeRecordedVersionId": [
                    "s3api",
                    "head-object",
                    "--region",
                    _REGION,
                    "--bucket",
                    binding.bucket,
                    "--key",
                    binding.key,
                    "--checksum-mode",
                    "ENABLED",
                    "--expected-bucket-owner",
                    binding.account_id,
                    "--version-id",
                ],
                "listObjectVersionsArguments": [
                    "s3api",
                    "list-object-versions",
                    "--region",
                    _REGION,
                    "--bucket",
                    binding.bucket,
                    "--prefix",
                    binding.key,
                    "--expected-bucket-owner",
                    binding.account_id,
                ],
                "putObjectResponseFields": [
                    "ChecksumSHA256",
                    "ChecksumType",
                    "ETag",
                    "ServerSideEncryption",
                    "VersionId",
                ],
                "requiredUploadCallerArn": (
                    f"arn:aws:iam::{binding.account_id}:user/mr-lister-dev"
                ),
                "sameCallerIdentityRequiredForPutAndDenyProbe": True,
                "uploadAuthorityRevocation": {
                    "denyProbeExpected": {
                        "ErrorCode": "AccessDenied",
                        "HTTPStatusCode": 403,
                    },
                    "groupName": "mr-lister-developers",
                    "getGroupMembershipArguments": [
                        "iam",
                        "get-group",
                        "--group-name",
                        "mr-lister-developers",
                    ],
                    "attachFreezePolicyArguments": [
                        "iam",
                        "attach-group-policy",
                        "--group-name",
                        "mr-lister-developers",
                        "--policy-arn",
                        expectation.freeze_policy_arn,
                    ],
                    "freezePolicyArn": expectation.freeze_policy_arn,
                    "getFreezePolicyArguments": [
                        "iam",
                        "get-policy",
                        "--policy-arn",
                        expectation.freeze_policy_arn,
                    ],
                    "getFreezePolicyVersionArgumentsBeforeDefaultVersionId": [
                        "iam",
                        "get-policy-version",
                        "--policy-arn",
                        expectation.freeze_policy_arn,
                        "--version-id",
                    ],
                    "listAttachmentsArguments": [
                        "iam",
                        "list-attached-group-policies",
                        "--group-name",
                        "mr-lister-developers",
                    ],
                    "readbackPolicyArn": expectation.readback_policy_arn,
                    "detachUploadPolicyArguments": [
                        "iam",
                        "detach-group-policy",
                        "--group-name",
                        "mr-lister-developers",
                        "--policy-arn",
                        expectation.upload_policy_arn,
                    ],
                    "uploadPolicyArn": expectation.upload_policy_arn,
                },
            },
        }
        return _canonical_json(plan)
    except Phase6AgentCoreDirectCodeZipError:
        raise
    except Exception:
        raise Phase6AgentCoreDirectCodeZipError(_GENERIC_ERROR) from None


def render_phase6_agentcore_runtime_documents(
    binding: Phase6AgentCoreDirectCodeZipBinding,
    archive: VerifiedAgentCoreArchive,
    remote: VerifiedPhase6S3ReleaseObject,
    *,
    repository_root: Path = ROOT,
) -> dict[Path, bytes]:
    """Render the exact first-create input, upload binding, residuals, and manifest."""

    try:
        _validate_remote_binding(binding, archive, remote)
        version_id = remote.version_id
        runtime_input = _expected_runtime_create_input(
            binding,
            remote,
            repository_root=repository_root,
        )
        residuals = _authorization_residuals(binding, remote)
        documents: dict[Path, bytes] = {
            AUTHORIZATION_RESIDUAL_OUTPUT: _canonical_json(residuals),
            RUNTIME_CREATE_OUTPUT: _canonical_json(runtime_input),
            UPLOAD_PLAN_OUTPUT: render_phase6_agentcore_upload_plan(binding, archive),
        }
        _validate_runtime_documents(binding, archive, remote, documents)
        manifest = {
            "artifact": {
                "checksumSHA256Base64": archive.checksum_sha256_base64,
                "descriptorSHA256": archive.descriptor_sha256,
                "sha256": archive.sha256,
                "sizeBytes": archive.size_bytes,
            },
            "binding": {
                "accountId": binding.account_id,
                "bucket": binding.bucket,
                "key": binding.key,
                "region": _REGION,
                "releaseFingerprint": binding.release_fingerprint,
                "roleArn": binding.role_arn,
                "runtimeName": _RUNTIME_NAME,
                "s3VersionId": version_id,
            },
            "bindingFingerprint": _binding_fingerprint(binding, remote),
            "createAuthorization": "BLOCKED_UNTIL_SEPARATELY_REVIEWED",
            "documents": {
                path.as_posix(): sha256(content).hexdigest()
                for path, content in sorted(documents.items(), key=lambda item: item[0].as_posix())
            },
            "format": "mr-lister-phase6-agentcore-direct-codezip-runtime-render-v1",
            "proofClaims": {
                "byteIdentityBinding": "VERIFIED_AT_CAPTURE",
                "collisionHygiene": "VERIFIED_FOR_MR_LISTER_DEV_GROUP",
                "uploadAuthorityRevoked": "VERIFIED_FOR_EXACT_MR_LISTER_DEV_USER",
            },
            "remoteObjectEvidenceSHA256": remote.evidence_sha256,
        }
        documents[RUNTIME_MANIFEST_OUTPUT] = _canonical_json(manifest)
        _reject_unresolved(documents)
        return dict(sorted(documents.items(), key=lambda item: item[0].as_posix()))
    except Phase6AgentCoreDirectCodeZipError:
        raise
    except Exception:
        raise Phase6AgentCoreDirectCodeZipError(_GENERIC_ERROR) from None


def render_phase6_agentcore_endpoint_documents(
    binding: Phase6AgentCoreDirectCodeZipBinding,
    archive: VerifiedAgentCoreArchive,
    remote: VerifiedPhase6S3ReleaseObject,
    *,
    runtime_v1_evidence_path: Path,
    repository_root: Path = ROOT,
) -> dict[Path, bytes]:
    """Render the v1 endpoint only after directly verifying joined runtime evidence."""

    try:
        root = repository_root.resolve(strict=True)
        runtime = verify_phase6_agentcore_runtime_v1_evidence(
            binding,
            archive,
            remote,
            runtime_v1_evidence_path=runtime_v1_evidence_path,
            repository_root=root,
        )
        runtime_manifest_path = root / RUNTIME_MANIFEST_OUTPUT
        _reject_symlink_parents(root, runtime_manifest_path.parent)
        if runtime_manifest_path.is_symlink() or not runtime_manifest_path.is_file():
            raise ValueError
        return _render_phase6_agentcore_endpoint_documents_from_verified_runtime(
            binding,
            remote,
            runtime,
            runtime_manifest_bytes=runtime_manifest_path.read_bytes(),
        )
    except Phase6AgentCoreDirectCodeZipError:
        raise
    except Exception:
        raise Phase6AgentCoreDirectCodeZipError(_GENERIC_ERROR) from None


def _render_phase6_agentcore_endpoint_documents_from_verified_runtime(
    binding: Phase6AgentCoreDirectCodeZipBinding,
    remote: VerifiedPhase6S3ReleaseObject,
    runtime: VerifiedAgentCoreRuntimeV1,
    *,
    runtime_manifest_bytes: bytes,
) -> dict[Path, bytes]:
    """Build endpoint bytes from the module's evidence-verifier result."""

    try:
        _validate_remote_identity(binding, remote)
        version_id = remote.version_id
        expected_runtime_arn = (
            f"arn:aws:bedrock-agentcore:{_REGION}:{binding.account_id}:runtime/{runtime.runtime_id}"
            if isinstance(runtime, VerifiedAgentCoreRuntimeV1)
            else ""
        )
        if (
            not isinstance(runtime, VerifiedAgentCoreRuntimeV1)
            or runtime.runtime_arn != expected_runtime_arn
            or runtime.remote_object_evidence_sha256 != remote.evidence_sha256
            or runtime.runtime_render_manifest_sha256 != sha256(runtime_manifest_bytes).hexdigest()
        ):
            raise ValueError
        runtime_id = runtime.runtime_id
        parsed_manifest = json.loads(runtime_manifest_bytes)
        if (
            not isinstance(parsed_manifest, dict)
            or parsed_manifest.get("bindingFingerprint") != _binding_fingerprint(binding, remote)
            or parsed_manifest.get("remoteObjectEvidenceSHA256") != remote.evidence_sha256
            or parsed_manifest.get("createAuthorization") != "BLOCKED_UNTIL_SEPARATELY_REVIEWED"
        ):
            raise ValueError
        endpoint = {
            "agentRuntimeId": runtime_id,
            "agentRuntimeVersion": _RUNTIME_VERSION,
            "clientToken": _client_token("endpoint", binding, f"{version_id}:{runtime_id}"),
            "description": "Immutable Phase 6 dev endpoint pinned to runtime version 1",
            "name": _ENDPOINT_NAME,
            "tags": binding.tags,
        }
        endpoint_bytes = _canonical_json(endpoint)
        manifest = {
            "bindingFingerprint": _binding_fingerprint(binding, remote),
            "createAuthorization": "BLOCKED_UNTIL_SEPARATELY_REVIEWED",
            "documents": {ENDPOINT_CREATE_OUTPUT.as_posix(): sha256(endpoint_bytes).hexdigest()},
            "endpointName": _ENDPOINT_NAME,
            "format": "mr-lister-phase6-agentcore-direct-codezip-endpoint-render-v1",
            "runtimeCreateManifestSHA256": sha256(runtime_manifest_bytes).hexdigest(),
            "runtimeCreateInputSHA256": runtime.runtime_create_input_sha256,
            "runtimeEvidenceSHA256": runtime.evidence_sha256,
            "runtimeArn": runtime.runtime_arn,
            "runtimeId": runtime_id,
            "runtimeVersion": _RUNTIME_VERSION,
            "remoteObjectEvidenceSHA256": remote.evidence_sha256,
        }
        documents = {
            ENDPOINT_CREATE_OUTPUT: endpoint_bytes,
            ENDPOINT_MANIFEST_OUTPUT: _canonical_json(manifest),
        }
        _validate_endpoint_documents(binding, remote, runtime_id, documents)
        _reject_unresolved(documents)
        return dict(sorted(documents.items(), key=lambda item: item[0].as_posix()))
    except Phase6AgentCoreDirectCodeZipError:
        raise
    except Exception:
        raise Phase6AgentCoreDirectCodeZipError(_GENERIC_ERROR) from None


def write_phase6_agentcore_runtime_documents(
    binding: Phase6AgentCoreDirectCodeZipBinding,
    *,
    object_binding_evidence: Path,
    repository_root: Path = ROOT,
    deployment_root: Path = DEFAULT_DEPLOYMENT_ROOT,
    artifact_root: Path = DEFAULT_ARTIFACT_ROOT,
) -> tuple[Path, ...]:
    """Verify the sealed artifact and exclusively write runtime-stage local outputs."""

    try:
        root = repository_root.resolve(strict=True)
        archive = verify_phase6_agentcore_direct_codezip_artifact(
            binding,
            deployment_root=deployment_root,
            artifact_root=artifact_root,
        )
        remote = _verify_remote_evidence(binding, archive, object_binding_evidence)
        documents = render_phase6_agentcore_runtime_documents(
            binding,
            archive,
            remote,
            repository_root=root,
        )
        return _write_new_documents(root, documents)
    except Phase6AgentCoreDirectCodeZipError:
        raise
    except Exception:
        raise Phase6AgentCoreDirectCodeZipError(_GENERIC_ERROR) from None


def verify_phase6_agentcore_runtime_documents(
    binding: Phase6AgentCoreDirectCodeZipBinding,
    *,
    object_binding_evidence: Path,
    repository_root: Path = ROOT,
    deployment_root: Path = DEFAULT_DEPLOYMENT_ROOT,
    artifact_root: Path = DEFAULT_ARTIFACT_ROOT,
) -> None:
    """Reject any artifact, descriptor, binding, or rendered-byte drift."""

    try:
        root = repository_root.resolve(strict=True)
        archive = verify_phase6_agentcore_direct_codezip_artifact(
            binding,
            deployment_root=deployment_root,
            artifact_root=artifact_root,
        )
        remote = _verify_remote_evidence(binding, archive, object_binding_evidence)
        expected = render_phase6_agentcore_runtime_documents(
            binding,
            archive,
            remote,
            repository_root=root,
        )
        _verify_documents(root, expected)
    except Phase6AgentCoreDirectCodeZipError:
        raise
    except Exception:
        raise Phase6AgentCoreDirectCodeZipError(_GENERIC_ERROR) from None


def verify_phase6_agentcore_runtime_v1_evidence(
    binding: Phase6AgentCoreDirectCodeZipBinding,
    archive: VerifiedAgentCoreArchive,
    remote: VerifiedPhase6S3ReleaseObject,
    *,
    runtime_v1_evidence_path: Path,
    repository_root: Path = ROOT,
) -> VerifiedAgentCoreRuntimeV1:
    """Join create/get/tag evidence to the exact sealed runtime-create authority."""

    try:
        root = repository_root.resolve(strict=True)
        _validate_remote_binding(binding, archive, remote)
        expected_documents = render_phase6_agentcore_runtime_documents(
            binding,
            archive,
            remote,
            repository_root=root,
        )
        _verify_documents(root, expected_documents)
        runtime_input_bytes = expected_documents[RUNTIME_CREATE_OUTPUT]
        runtime_manifest_bytes = expected_documents[RUNTIME_MANIFEST_OUTPUT]
        runtime_input = json.loads(runtime_input_bytes, object_pairs_hook=_unique_json_object)

        evidence_bytes, evidence = _load_canonical_mapping(runtime_v1_evidence_path)
        if set(evidence) != {
            "accountId",
            "createAgentRuntime",
            "format",
            "getAgentRuntime",
            "listTagsForResource",
            "region",
            "remoteObjectEvidenceSHA256",
            "runtimeRenderManifestSHA256",
        }:
            raise ValueError
        if (
            evidence.get("format") != RUNTIME_V1_EVIDENCE_FORMAT
            or evidence.get("accountId") != binding.account_id
            or evidence.get("region") != _REGION
            or evidence.get("remoteObjectEvidenceSHA256") != remote.evidence_sha256
            or evidence.get("runtimeRenderManifestSHA256")
            != sha256(runtime_manifest_bytes).hexdigest()
        ):
            raise ValueError

        create_operation = _require_exact_mapping(
            evidence.get("createAgentRuntime"),
            {"inputSHA256", "response"},
        )
        if create_operation.get("inputSHA256") != sha256(runtime_input_bytes).hexdigest():
            raise ValueError
        create_response = _validate_create_runtime_response(
            create_operation.get("response"),
            binding,
        )
        runtime_id = str(create_response["agentRuntimeId"])
        runtime_arn = str(create_response["agentRuntimeArn"])

        get_operation = _require_exact_mapping(
            evidence.get("getAgentRuntime"),
            {"request", "response"},
        )
        get_request = _require_exact_mapping(
            get_operation.get("request"),
            {"agentRuntimeId", "agentRuntimeVersion"},
        )
        if get_request != {
            "agentRuntimeId": runtime_id,
            "agentRuntimeVersion": _RUNTIME_VERSION,
        }:
            raise ValueError
        _validate_get_runtime_response(
            get_operation.get("response"),
            binding,
            runtime_input,
            create_response,
        )

        tags_operation = _require_exact_mapping(
            evidence.get("listTagsForResource"),
            {"request", "response"},
        )
        if _require_exact_mapping(tags_operation.get("request"), {"resourceArn"}) != {
            "resourceArn": runtime_arn
        }:
            raise ValueError
        if _require_exact_mapping(tags_operation.get("response"), {"tags"}) != {
            "tags": binding.tags
        }:
            raise ValueError

        return VerifiedAgentCoreRuntimeV1(
            runtime_id=runtime_id,
            runtime_arn=runtime_arn,
            evidence_sha256=sha256(evidence_bytes).hexdigest(),
            runtime_create_input_sha256=sha256(runtime_input_bytes).hexdigest(),
            runtime_render_manifest_sha256=sha256(runtime_manifest_bytes).hexdigest(),
            remote_object_evidence_sha256=remote.evidence_sha256,
        )
    except Phase6AgentCoreDirectCodeZipError:
        raise
    except Exception:
        raise Phase6AgentCoreDirectCodeZipError(_GENERIC_ERROR) from None


def write_phase6_agentcore_endpoint_documents(
    binding: Phase6AgentCoreDirectCodeZipBinding,
    *,
    object_binding_evidence: Path,
    runtime_v1_evidence: Path,
    repository_root: Path = ROOT,
    deployment_root: Path = DEFAULT_DEPLOYMENT_ROOT,
    artifact_root: Path = DEFAULT_ARTIFACT_ROOT,
) -> tuple[Path, ...]:
    """Verify runtime-stage bytes and exclusively write endpoint-stage local outputs."""

    try:
        verify_phase6_agentcore_runtime_documents(
            binding,
            object_binding_evidence=object_binding_evidence,
            repository_root=repository_root,
            deployment_root=deployment_root,
            artifact_root=artifact_root,
        )
        root = repository_root.resolve(strict=True)
        runtime_manifest_path = root / RUNTIME_MANIFEST_OUTPUT
        if runtime_manifest_path.is_symlink() or not runtime_manifest_path.is_file():
            raise ValueError
        archive = verify_phase6_agentcore_direct_codezip_artifact(
            binding,
            deployment_root=deployment_root,
            artifact_root=artifact_root,
        )
        remote = _verify_remote_evidence(binding, archive, object_binding_evidence)
        documents = render_phase6_agentcore_endpoint_documents(
            binding,
            archive,
            remote,
            runtime_v1_evidence_path=runtime_v1_evidence,
            repository_root=root,
        )
        return _write_new_documents(root, documents)
    except Phase6AgentCoreDirectCodeZipError:
        raise
    except Exception:
        raise Phase6AgentCoreDirectCodeZipError(_GENERIC_ERROR) from None


def verify_phase6_agentcore_endpoint_documents(
    binding: Phase6AgentCoreDirectCodeZipBinding,
    *,
    object_binding_evidence: Path,
    runtime_v1_evidence: Path,
    repository_root: Path = ROOT,
    deployment_root: Path = DEFAULT_DEPLOYMENT_ROOT,
    artifact_root: Path = DEFAULT_ARTIFACT_ROOT,
) -> None:
    """Reject endpoint-stage drift and re-verify all runtime-stage inputs first."""

    try:
        verify_phase6_agentcore_runtime_documents(
            binding,
            object_binding_evidence=object_binding_evidence,
            repository_root=repository_root,
            deployment_root=deployment_root,
            artifact_root=artifact_root,
        )
        root = repository_root.resolve(strict=True)
        runtime_manifest_path = root / RUNTIME_MANIFEST_OUTPUT
        if runtime_manifest_path.is_symlink() or not runtime_manifest_path.is_file():
            raise ValueError
        archive = verify_phase6_agentcore_direct_codezip_artifact(
            binding,
            deployment_root=deployment_root,
            artifact_root=artifact_root,
        )
        remote = _verify_remote_evidence(binding, archive, object_binding_evidence)
        expected = render_phase6_agentcore_endpoint_documents(
            binding,
            archive,
            remote,
            runtime_v1_evidence_path=runtime_v1_evidence,
            repository_root=root,
        )
        _verify_documents(root, expected)
    except Phase6AgentCoreDirectCodeZipError:
        raise
    except Exception:
        raise Phase6AgentCoreDirectCodeZipError(_GENERIC_ERROR) from None


def _remote_expectation(
    binding: Phase6AgentCoreDirectCodeZipBinding,
    archive: VerifiedAgentCoreArchive,
) -> Phase6S3ReleaseObjectExpectation:
    try:
        if (
            not isinstance(binding, Phase6AgentCoreDirectCodeZipBinding)
            or not isinstance(archive, VerifiedAgentCoreArchive)
            or archive.sha256 != binding.agentcore_archive_sha256
        ):
            raise ValueError
        return Phase6S3ReleaseObjectExpectation(
            account_id=binding.account_id,
            region=_REGION,
            environment=_ENVIRONMENT,
            component="agentcore",
            release_fingerprint=binding.release_fingerprint,
            archive_sha256=archive.sha256,
            size_bytes=archive.size_bytes,
        )
    except Exception:
        raise Phase6AgentCoreDirectCodeZipError(_GENERIC_ERROR) from None


def _verify_remote_evidence(
    binding: Phase6AgentCoreDirectCodeZipBinding,
    archive: VerifiedAgentCoreArchive,
    evidence_path: Path,
) -> VerifiedPhase6S3ReleaseObject:
    try:
        remote = verify_phase6_s3_release_object_evidence(
            _remote_expectation(binding, archive),
            evidence_path=evidence_path,
        )
        _validate_remote_binding(binding, archive, remote)
        return remote
    except Phase6AgentCoreDirectCodeZipError:
        raise
    except Exception:
        raise Phase6AgentCoreDirectCodeZipError(_GENERIC_ERROR) from None


def _validate_remote_identity(
    binding: Phase6AgentCoreDirectCodeZipBinding,
    remote: VerifiedPhase6S3ReleaseObject,
) -> None:
    if (
        not isinstance(binding, Phase6AgentCoreDirectCodeZipBinding)
        or not isinstance(remote, VerifiedPhase6S3ReleaseObject)
        or remote.account_id != binding.account_id
        or remote.region != _REGION
        or remote.environment != _ENVIRONMENT
        or remote.component != "agentcore"
        or remote.release_fingerprint != binding.release_fingerprint
        or remote.archive_sha256 != binding.agentcore_archive_sha256
        or remote.bucket != binding.bucket
        or remote.key != binding.key
    ):
        raise Phase6AgentCoreDirectCodeZipError(_GENERIC_ERROR)


def _validate_remote_binding(
    binding: Phase6AgentCoreDirectCodeZipBinding,
    archive: VerifiedAgentCoreArchive,
    remote: VerifiedPhase6S3ReleaseObject,
) -> None:
    _validate_remote_identity(binding, remote)
    if (
        not isinstance(archive, VerifiedAgentCoreArchive)
        or remote.size_bytes != archive.size_bytes
        or remote.checksum_sha256_base64 != archive.checksum_sha256_base64
    ):
        raise Phase6AgentCoreDirectCodeZipError(_GENERIC_ERROR)


def _authorization_residuals(
    binding: Phase6AgentCoreDirectCodeZipBinding,
    remote: VerifiedPhase6S3ReleaseObject,
) -> dict[str, object]:
    return {
        "blockedCreateOperations": [
            {
                "action": "bedrock-agentcore:CreateAgentRuntime",
                "defaultDeveloperPolicyGrant": False,
                "unsupportedIamDimensions": ["agentRuntimeName"],
                "requiredValue": _RUNTIME_NAME,
            },
            {
                "action": "bedrock-agentcore:CreateAgentRuntimeEndpoint",
                "defaultDeveloperPolicyGrant": False,
                "unsupportedIamDimensions": ["endpointName", "agentRuntimeVersion"],
                "requiredValues": {
                    "agentRuntimeVersion": _RUNTIME_VERSION,
                    "endpointName": _ENDPOINT_NAME,
                },
            },
        ],
        "crossingRequires": [
            "separately-reviewed-one-time-manual-root-execution",
            "explicit-user-approved-tag-and-time-scoped-exception",
        ],
        "failClosedReadbackAndRetentionResiduals": [
            {
                "action": "logs:DescribeLogGroups",
                "defaultDeveloperPolicyGrant": False,
                "unsupportedIamDimensions": ["logGroupNamePrefix"],
            },
            {
                "action": "logs:PutRetentionPolicy",
                "defaultDeveloperPolicyGrant": False,
                "requiredValue": {"retentionInDays": 14},
                "unsupportedIamDimensions": ["retentionInDays"],
            },
            {
                "action": "bedrock:GetFoundationModelAvailability",
                "defaultDeveloperPolicyGrant": False,
                "unsupportedIamDimensions": ["modelId", "resource"],
            },
            {
                "actions": [
                    "bedrock-agentcore:ListAgentRuntimes",
                    "bedrock-agentcore:ListAgentRuntimeVersions",
                    "bedrock-agentcore:ListAgentRuntimeEndpoints",
                ],
                "defaultDeveloperPolicyGrant": False,
                "unsupportedIamDimensions": ["resource", "resourceTags"],
            },
        ],
        "format": "mr-lister-phase6-agentcore-authorization-residuals-v1",
        "immutableInputBinding": {
            "accountId": binding.account_id,
            "endpointName": _ENDPOINT_NAME,
            "releaseFingerprint": binding.release_fingerprint,
            "runtimeName": _RUNTIME_NAME,
            "runtimeVersion": _RUNTIME_VERSION,
            "s3": {
                "bucket": binding.bucket,
                "key": binding.key,
                "versionId": remote.version_id,
            },
            "tags": binding.tags,
        },
        "prohibited": [
            "AgentCore CLI packaging or deploy",
            "DEFAULT endpoint mutation",
            "UpdateAgentRuntime",
            "UpdateAgentRuntimeEndpoint",
            "moving S3 object references",
        ],
        "remoteObjectProtectionResiduals": [
            {
                "accountWideObjectLock": False,
                "protectedPrincipalScope": (
                    f"arn:aws:iam::{binding.account_id}:user/mr-lister-dev via mr-lister-developers"
                ),
                "retainedFreezePolicyActions": [
                    "s3:DeleteObject",
                    "s3:DeleteObjectVersion",
                    "s3:PutObject",
                ],
                "risk": (
                    "privileged principals outside mr-lister-developers remain able to delete "
                    "the exact version unless separately denied"
                ),
            }
        ],
        "status": "BLOCKED_UNTIL_SEPARATELY_REVIEWED",
    }


def _expected_runtime_create_input(
    binding: Phase6AgentCoreDirectCodeZipBinding,
    remote: VerifiedPhase6S3ReleaseObject,
    *,
    repository_root: Path,
) -> dict[str, object]:
    _validate_remote_identity(binding, remote)
    return {
        "agentRuntimeArtifact": {
            "codeConfiguration": {
                "code": {
                    "s3": {
                        "bucket": binding.bucket,
                        "prefix": binding.key,
                        "versionId": remote.version_id,
                    }
                },
                "entryPoint": ["main.py"],
                "runtime": "PYTHON_3_12",
            }
        },
        "agentRuntimeName": _RUNTIME_NAME,
        "clientToken": _client_token("runtime", binding, remote.version_id),
        "description": "Release-bound Phase 6 Strands preparation runtime",
        "environmentVariables": _existing_phase6_environment(
            binding,
            repository_root=repository_root,
        ),
        "lifecycleConfiguration": {
            "idleRuntimeSessionTimeout": 900,
            "maxLifetime": 3600,
        },
        "networkConfiguration": {"networkMode": "PUBLIC"},
        "protocolConfiguration": {"serverProtocol": "HTTP"},
        "roleArn": binding.role_arn,
        "tags": binding.tags,
    }


def _existing_phase6_environment(
    binding: Phase6AgentCoreDirectCodeZipBinding,
    *,
    repository_root: Path,
) -> dict[str, str]:
    reviewed = render_phase6_agentcore_deployment(
        Phase6AgentCoreDeploymentBinding(
            account_id=binding.account_id,
            region=_REGION,
            environment=_ENVIRONMENT,
            release_fingerprint=binding.release_fingerprint,
            runtime_version=_RUNTIME_VERSION,
        ),
        repository_root=repository_root,
    )
    config = json.loads(reviewed[AGENTCORE_OUTPUT])
    [runtime] = config["runtimes"]
    env_vars = runtime["envVars"]
    environment = {item["name"]: item["value"] for item in env_vars}
    if len(environment) != len(env_vars) or any(
        not isinstance(key, str) or not isinstance(value, str) for key, value in environment.items()
    ):
        raise ValueError
    return dict(sorted(environment.items()))


def _validate_runtime_documents(
    binding: Phase6AgentCoreDirectCodeZipBinding,
    archive: VerifiedAgentCoreArchive,
    remote: VerifiedPhase6S3ReleaseObject,
    documents: dict[Path, bytes],
) -> None:
    if set(documents) != {
        AUTHORIZATION_RESIDUAL_OUTPUT,
        RUNTIME_CREATE_OUTPUT,
        UPLOAD_PLAN_OUTPUT,
    }:
        raise ValueError
    runtime = json.loads(documents[RUNTIME_CREATE_OUTPUT])
    if not isinstance(runtime, dict) or set(runtime) != {
        "agentRuntimeArtifact",
        "agentRuntimeName",
        "clientToken",
        "description",
        "environmentVariables",
        "lifecycleConfiguration",
        "networkConfiguration",
        "protocolConfiguration",
        "roleArn",
        "tags",
    }:
        raise ValueError
    s3 = runtime["agentRuntimeArtifact"]["codeConfiguration"]["code"]["s3"]
    if s3 != {
        "bucket": binding.bucket,
        "prefix": binding.key,
        "versionId": remote.version_id,
    }:
        raise ValueError
    code = runtime["agentRuntimeArtifact"]["codeConfiguration"]
    if code.get("runtime") != "PYTHON_3_12" or code.get("entryPoint") != ["main.py"]:
        raise ValueError
    if (
        runtime.get("agentRuntimeName") != _RUNTIME_NAME
        or runtime.get("roleArn") != binding.role_arn
        or runtime.get("tags") != binding.tags
        or runtime.get("networkConfiguration") != {"networkMode": "PUBLIC"}
        or runtime.get("protocolConfiguration") != {"serverProtocol": "HTTP"}
        or runtime.get("lifecycleConfiguration")
        != {"idleRuntimeSessionTimeout": 900, "maxLifetime": 3600}
        or "authorizerConfiguration" in runtime
    ):
        raise ValueError
    upload = json.loads(documents[UPLOAD_PLAN_OUTPUT])
    if (
        upload.get("artifact", {}).get("sha256") != archive.sha256
        or upload.get("artifact", {}).get("sizeBytes") != archive.size_bytes
        or upload.get("s3")
        != {
            "bucket": binding.bucket,
            "key": binding.key,
        }
        or upload.get("conditionalWrite", {}).get("ifNoneMatch") != "*"
        or upload.get("conditionalWrite", {}).get("serverSideEncryption") != "AES256"
        or upload.get("postUploadRenderStatus") != "BLOCKED_UNTIL_CLOSED_OBJECT_EVIDENCE_VERIFIES"
        or upload.get("requiredClosedEvidence", {}).get("format") != EVIDENCE_FORMAT
    ):
        raise ValueError
    residual = json.loads(documents[AUTHORIZATION_RESIDUAL_OUTPUT])
    blocked = residual.get("blockedCreateOperations")
    if (
        residual.get("status") != "BLOCKED_UNTIL_SEPARATELY_REVIEWED"
        or not isinstance(blocked, list)
        or [entry.get("action") for entry in blocked]
        != [
            "bedrock-agentcore:CreateAgentRuntime",
            "bedrock-agentcore:CreateAgentRuntimeEndpoint",
        ]
        or any(entry.get("defaultDeveloperPolicyGrant") is not False for entry in blocked)
    ):
        raise ValueError


def _validate_endpoint_documents(
    binding: Phase6AgentCoreDirectCodeZipBinding,
    remote: VerifiedPhase6S3ReleaseObject,
    runtime_id: str,
    documents: dict[Path, bytes],
) -> None:
    if set(documents) != {ENDPOINT_CREATE_OUTPUT, ENDPOINT_MANIFEST_OUTPUT}:
        raise ValueError
    endpoint = json.loads(documents[ENDPOINT_CREATE_OUTPUT])
    if endpoint != {
        "agentRuntimeId": runtime_id,
        "agentRuntimeVersion": _RUNTIME_VERSION,
        "clientToken": _client_token(
            "endpoint",
            binding,
            f"{remote.version_id}:{runtime_id}",
        ),
        "description": "Immutable Phase 6 dev endpoint pinned to runtime version 1",
        "name": _ENDPOINT_NAME,
        "tags": binding.tags,
    }:
        raise ValueError


def _require_exact_mapping(value: object, keys: set[str]) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or set(value) != keys:
        raise ValueError
    return value


def _validate_create_runtime_response(
    value: object,
    binding: Phase6AgentCoreDirectCodeZipBinding,
) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError
    required = {
        "agentRuntimeArn",
        "agentRuntimeId",
        "agentRuntimeVersion",
        "createdAt",
        "status",
    }
    if set(value) not in (required, required | {"workloadIdentityDetails"}):
        raise ValueError
    runtime_id = value.get("agentRuntimeId")
    _validate_runtime_id(str(runtime_id))
    runtime_arn = f"arn:aws:bedrock-agentcore:{_REGION}:{binding.account_id}:runtime/{runtime_id}"
    if (
        value.get("agentRuntimeArn") != runtime_arn
        or value.get("agentRuntimeVersion") != _RUNTIME_VERSION
        or value.get("status") not in {"CREATING", "READY"}
    ):
        raise ValueError
    _parse_utc_timestamp(value.get("createdAt"))
    if "workloadIdentityDetails" in value:
        _validate_workload_identity(value["workloadIdentityDetails"], binding)
    return value


def _validate_get_runtime_response(
    value: object,
    binding: Phase6AgentCoreDirectCodeZipBinding,
    runtime_input: Mapping[str, object],
    create_response: Mapping[str, object],
) -> None:
    if not isinstance(value, Mapping):
        raise ValueError
    configured = {
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
        "networkConfiguration",
        "protocolConfiguration",
        "roleArn",
        "status",
        "workloadIdentityDetails",
    }
    documented_optional = {
        "authorizerConfiguration",
        "capacityProviderConfiguration",
        "failureReason",
        "filesystemConfigurations",
        "metadataConfiguration",
        "requestHeaderConfiguration",
    }
    if not configured <= set(value) or set(value) - configured - documented_optional:
        raise ValueError
    if value.get("failureReason") is not None:
        raise ValueError
    if value.get("authorizerConfiguration") is not None:
        raise ValueError
    if value.get("capacityProviderConfiguration") is not None:
        raise ValueError
    if value.get("filesystemConfigurations") not in (None, []):
        raise ValueError
    if value.get("requestHeaderConfiguration") not in (
        None,
        {"requestHeaderAllowlist": []},
    ):
        raise ValueError
    if value.get("metadataConfiguration") != {"requireMMDSV2": True}:
        raise ValueError

    runtime_id = create_response["agentRuntimeId"]
    runtime_arn = create_response["agentRuntimeArn"]
    exact_fields = {
        "agentRuntimeArn": runtime_arn,
        "agentRuntimeArtifact": runtime_input["agentRuntimeArtifact"],
        "agentRuntimeId": runtime_id,
        "agentRuntimeName": runtime_input["agentRuntimeName"],
        "agentRuntimeVersion": _RUNTIME_VERSION,
        "description": runtime_input["description"],
        "environmentVariables": runtime_input["environmentVariables"],
        "lifecycleConfiguration": runtime_input["lifecycleConfiguration"],
        "networkConfiguration": runtime_input["networkConfiguration"],
        "protocolConfiguration": runtime_input["protocolConfiguration"],
        "roleArn": runtime_input["roleArn"],
        "status": "READY",
    }
    if any(value.get(name) != expected for name, expected in exact_fields.items()):
        raise ValueError
    create_response_created_at = _parse_utc_timestamp(create_response.get("createdAt"))
    get_response_created_at = _parse_utc_timestamp(value.get("createdAt"))
    last_updated_at = _parse_utc_timestamp(value.get("lastUpdatedAt"))
    if not create_response_created_at <= get_response_created_at <= last_updated_at:
        raise ValueError
    _validate_workload_identity(value.get("workloadIdentityDetails"), binding)
    if (
        "workloadIdentityDetails" in create_response
        and value.get("workloadIdentityDetails") != create_response["workloadIdentityDetails"]
    ):
        raise ValueError


def _validate_workload_identity(
    value: object,
    binding: Phase6AgentCoreDirectCodeZipBinding,
) -> None:
    document = _require_exact_mapping(value, {"workloadIdentityArn"})
    arn = document.get("workloadIdentityArn")
    prefix = f"arn:aws:bedrock-agentcore:{_REGION}:{binding.account_id}:"
    if (
        not isinstance(arn, str)
        or not arn.startswith(prefix)
        or not 1 <= len(arn) <= 1024
        or arn != arn.strip()
        or _PLACEHOLDER.search(arn)
        or any(ord(character) < 32 for character in arn)
    ):
        raise ValueError


def _parse_utc_timestamp(value: object) -> datetime:
    if not isinstance(value, str) or not 1 <= len(value) <= 64 or value != value.strip():
        raise ValueError
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() != UTC.utcoffset(parsed):
        raise ValueError
    return parsed


def _binding_fingerprint(
    binding: Phase6AgentCoreDirectCodeZipBinding,
    remote: VerifiedPhase6S3ReleaseObject,
) -> str:
    return sha256(
        _canonical_json(
            {
                "accountId": binding.account_id,
                "agentCoreArchiveSHA256": binding.agentcore_archive_sha256,
                "bucket": binding.bucket,
                "format": "mr-lister-phase6-agentcore-direct-codezip-binding-v1",
                "key": binding.key,
                "releaseFingerprint": binding.release_fingerprint,
                "remoteObjectEvidenceSHA256": remote.evidence_sha256,
                "s3VersionId": remote.version_id,
            }
        )
    ).hexdigest()


def _client_token(
    kind: str,
    binding: Phase6AgentCoreDirectCodeZipBinding,
    suffix: str,
) -> str:
    material = (
        f"mr-lister-phase6-agentcore-{kind}-v1\n{binding.account_id}\n"
        f"{binding.release_fingerprint}\n{binding.agentcore_archive_sha256}\n{suffix}\n"
    )
    return sha256(material.encode("utf-8")).hexdigest()


def _validate_runtime_id(value: str) -> None:
    if not isinstance(value, str) or _RUNTIME_ID.fullmatch(value) is None:
        raise Phase6AgentCoreDirectCodeZipError(_GENERIC_ERROR)


def _digest_file(path: Path):  # type: ignore[no-untyped-def]
    digest = sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest


def _canonical_json(value: object) -> bytes:
    return (
        json.dumps(value, allow_nan=False, indent=2, separators=(",", ": "), sort_keys=True) + "\n"
    ).encode("utf-8")


def _load_canonical_mapping(path: Path) -> tuple[bytes, Mapping[str, object]]:
    if (
        not isinstance(path, Path)
        or any(candidate.is_symlink() for candidate in (path, *path.parents))
        or not path.is_file()
    ):
        raise ValueError
    raw = path.read_bytes()
    if not 1 <= len(raw) <= 1024 * 1024:
        raise ValueError
    document = json.loads(raw, object_pairs_hook=_unique_json_object)
    if not isinstance(document, Mapping) or _canonical_json(document) != raw:
        raise ValueError
    return raw, document


def _unique_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    document: dict[str, object] = {}
    for key, value in pairs:
        if key in document:
            raise ValueError
        document[key] = value
    return document


def _reject_unresolved(documents: dict[Path, bytes]) -> None:
    for content in documents.values():
        text = content.decode("utf-8")
        if _PLACEHOLDER.search(text) or '"versionId": null' in text:
            raise ValueError


def _write_new_documents(root: Path, documents: dict[Path, bytes]) -> tuple[Path, ...]:
    destinations = tuple(root / relative for relative in documents)
    if any(path.exists() or path.is_symlink() for path in destinations):
        raise Phase6AgentCoreDirectCodeZipError(_GENERIC_ERROR)
    for destination in destinations:
        parent = destination.parent
        _reject_symlink_parents(root, parent)
        parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        _reject_symlink_parents(root, parent)
    for relative, content in documents.items():
        destination = root / relative
        with destination.open("xb") as stream:
            stream.write(content)
        destination.chmod(0o600)
    return destinations


def _reject_symlink_parents(root: Path, parent: Path) -> None:
    relative = parent.relative_to(root)
    current = root
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise Phase6AgentCoreDirectCodeZipError(_GENERIC_ERROR)


def _verify_documents(root: Path, expected: dict[Path, bytes]) -> None:
    for relative, expected_bytes in expected.items():
        path = root / relative
        _reject_symlink_parents(root, path.parent)
        if path.is_symlink() or not path.is_file() or path.read_bytes() != expected_bytes:
            raise Phase6AgentCoreDirectCodeZipError(_GENERIC_ERROR)


def _binding_from_arguments(arguments: argparse.Namespace) -> Phase6AgentCoreDirectCodeZipBinding:
    return Phase6AgentCoreDirectCodeZipBinding(
        account_id=arguments.account_id,
        release_fingerprint=arguments.release_fingerprint,
        agentcore_archive_sha256=arguments.agentcore_archive_sha256,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Render or verify immutable Phase 6 AgentCore direct-CodeZip CLI inputs"
    )
    parser.add_argument("--account-id", required=True)
    parser.add_argument("--release-fingerprint", required=True)
    parser.add_argument("--agentcore-archive-sha256", required=True)
    parser.add_argument("--object-binding-evidence", type=Path)
    parser.add_argument("--runtime-v1-evidence", type=Path)
    parser.add_argument("--repository-root", type=Path, default=ROOT)
    parser.add_argument("--deployment-root", type=Path, default=DEFAULT_DEPLOYMENT_ROOT)
    parser.add_argument("--artifact-root", type=Path, default=DEFAULT_ARTIFACT_ROOT)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--show-upload-plan", action="store_true")
    mode.add_argument("--write-runtime", action="store_true")
    mode.add_argument("--verify-runtime", action="store_true")
    mode.add_argument("--write-endpoint", action="store_true")
    mode.add_argument("--verify-endpoint", action="store_true")
    arguments = parser.parse_args(argv)
    try:
        binding = _binding_from_arguments(arguments)
        if arguments.show_upload_plan:
            if (
                arguments.runtime_v1_evidence is not None
                or arguments.object_binding_evidence is not None
            ):
                raise Phase6AgentCoreDirectCodeZipError(_GENERIC_ERROR)
            archive = verify_phase6_agentcore_direct_codezip_artifact(
                binding,
                deployment_root=arguments.deployment_root,
                artifact_root=arguments.artifact_root,
            )
            print(render_phase6_agentcore_upload_plan(binding, archive).decode("utf-8"), end="")
        elif arguments.write_runtime:
            if (
                arguments.object_binding_evidence is None
                or arguments.runtime_v1_evidence is not None
            ):
                raise Phase6AgentCoreDirectCodeZipError(_GENERIC_ERROR)
            write_phase6_agentcore_runtime_documents(
                binding,
                object_binding_evidence=arguments.object_binding_evidence,
                repository_root=arguments.repository_root,
                deployment_root=arguments.deployment_root,
                artifact_root=arguments.artifact_root,
            )
        elif arguments.verify_runtime:
            if (
                arguments.object_binding_evidence is None
                or arguments.runtime_v1_evidence is not None
            ):
                raise Phase6AgentCoreDirectCodeZipError(_GENERIC_ERROR)
            verify_phase6_agentcore_runtime_documents(
                binding,
                object_binding_evidence=arguments.object_binding_evidence,
                repository_root=arguments.repository_root,
                deployment_root=arguments.deployment_root,
                artifact_root=arguments.artifact_root,
            )
        else:
            if arguments.object_binding_evidence is None or arguments.runtime_v1_evidence is None:
                raise Phase6AgentCoreDirectCodeZipError(_GENERIC_ERROR)
            if arguments.write_endpoint:
                write_phase6_agentcore_endpoint_documents(
                    binding,
                    object_binding_evidence=arguments.object_binding_evidence,
                    runtime_v1_evidence=arguments.runtime_v1_evidence,
                    repository_root=arguments.repository_root,
                    deployment_root=arguments.deployment_root,
                    artifact_root=arguments.artifact_root,
                )
            else:
                verify_phase6_agentcore_endpoint_documents(
                    binding,
                    object_binding_evidence=arguments.object_binding_evidence,
                    runtime_v1_evidence=arguments.runtime_v1_evidence,
                    repository_root=arguments.repository_root,
                    deployment_root=arguments.deployment_root,
                    artifact_root=arguments.artifact_root,
                )
        return 0
    except Phase6AgentCoreDirectCodeZipError as error:
        parser.error(str(error))
    return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
