"""Render the immutable, release-bound Phase 6 SAM staging template.

This tool is deliberately local and two-stage.  It verifies the already sealed Lambda and
AgentCore deployment/artifact set, the closed AgentCore S3 upload/readback/revocation evidence,
one accepted exact-version Lambda evidence format, the exact
CreateAgentRuntime/GetAgentRuntime/ListTags runtime-v1 join, and a canonical READY endpoint
observation.  It then replaces every scaffold ``CodeUri`` with the proven exact-version Lambda
archive coordinate and binds only the proven runtime and endpoint identity.  A bare S3 VersionId
or raw runtime ID is never deployment evidence.  Staging never enables runtime execution: the
rendered template keeps ``MR_LISTER_PHASE6_SCAFFOLD_ONLY=true`` and advertises only
``RELEASE_BOUND_STAGED``.  All four SAM schedule/stream events and the standalone recovery rule
are disabled, and the exact three maintenance functions are zero-throttled.  The default HTTP API
endpoint and CloudFront distribution are disabled as well, so the retained web infrastructure and
routes are not externally served by this staged output.

Activation is intentionally unavailable here.  A later activation gate must consume independent
live evidence that binds the reviewed stack, this rendered template and its proof hashes, the
release, both S3 object versions, and a freshly observed READY immutable AgentCore endpoint before
it may render ``scaffold=false``.

No operation builds an artifact, opens a network connection, or constructs an AWS client.
"""

from __future__ import annotations

import argparse
import base64
import json
import re
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path, PurePosixPath
from typing import NoReturn

from mr_lister.agent.runtime_binding import (
    load_agentcore_runtime_binding,
)
from mr_lister.release.phase6 import LINUX_ARM64_TARGET, render_manifest
from tools.build_phase66_source_bundles import (
    AGENTCORE_ARCHIVE_FILENAME,
    DEPLOYMENT_DESCRIPTOR_FILENAME,
    LAMBDA_ARCHIVE_FILENAME,
    verify_phase6_deployment_artifacts,
)
from tools.render_phase6_agentcore_direct_codezip import (
    Phase6AgentCoreDirectCodeZipBinding,
    VerifiedAgentCoreArchive,
    VerifiedAgentCoreRuntimeV1,
    verify_phase6_agentcore_runtime_v1_evidence,
)
from tools.verify_phase6_agentcore_endpoint_observation import (
    verify_phase6_agentcore_endpoint_observation,
)
from tools.verify_phase6_s3_release_object import (
    Phase6S3ReleaseObjectExpectation,
    VerifiedPhase6S3ReleaseObject,
    validate_phase6_s3_version_id,
    verify_phase6_lambda_release_object_evidence,
)
from tools.verify_phase6_s3_release_object import (
    verify_phase6_s3_release_object_evidence as _verify_phase6_closed_s3_release_object_evidence,
)

ROOT = Path(__file__).resolve().parents[1]
SOURCE_TEMPLATE = Path("infra/phase6/template.json")
STAGED_TEMPLATE_OUTPUT = Path(
    ".mr_lister_private/phase6-sam/template.release-bound-staged.local.json"
)
DEFAULT_DEPLOYMENT_ROOT = ROOT / ".mr_lister_private/phase6-deployment"
DEFAULT_ARTIFACT_ROOT = ROOT / ".mr_lister_private/phase6-artifacts"

_SOURCE_TEMPLATE_SHA256 = "9a110b3e813ed23102033ace67341d9cb4015274d7acc9f0fff6c08439c57ed7"
_GENERIC_ERROR = "Phase 6 SAM staged deployment configuration is invalid"


def verify_phase6_s3_release_object_evidence(
    expectation: Phase6S3ReleaseObjectExpectation,
    *,
    evidence_path: Path,
) -> VerifiedPhase6S3ReleaseObject:
    """Use the explicit Lambda-only manual path without weakening AgentCore evidence."""

    verifier = (
        verify_phase6_lambda_release_object_evidence
        if expectation.component == "lambda"
        else _verify_phase6_closed_s3_release_object_evidence
    )
    return verifier(expectation, evidence_path=evidence_path)


_ACTIVATION_ERROR = (
    "Phase 6 SAM activation requires a separate verified staged-deployment evidence gate"
)
_REGION = "us-west-2"
_ACCOUNT_ID = re.compile(r"^[0-9]{12}$")
_ENVIRONMENT = re.compile(r"^[a-z][a-z0-9-]{1,15}$")
_FINGERPRINT = re.compile(r"^[a-f0-9]{64}$")
_S3_BUCKET = re.compile(r"^[a-z0-9][a-z0-9-]{1,61}[a-z0-9]$")
_ORIGIN = re.compile(r"^https://[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?$")
_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_PLACEHOLDER = re.compile(
    r"<[A-Z][A-Z0-9_]*>|__[A-Z][A-Z0-9_]*__|"
    r"\b(?:PLACEHOLDER|REPLACE_ME|CHANGEME)\b",
    re.IGNORECASE,
)
_MOVING_VALUES = frozenset({"current", "default", "latest", "mutable", "null", "unversioned"})

_FUNCTION_HANDLERS = {
    "DispatcherFunction": "phase6_lambda.dispatcher_handler",
    "PreparationDispatchFunction": "phase6_lambda.preparation_dispatch_handler",
    "ProviderDraftFunction": "phase6_lambda.provider_draft_handler",
    "ReviewQueryApiFunction": "phase6_lambda.review_query_api_handler",
    "SellerCommandApiFunction": "phase6_lambda.seller_command_api_handler",
    "SettlementFunction": "phase6_lambda.settlement_handler",
    "SourceVersionRetentionFunction": "phase6_lambda.source_version_retention_handler",
    "StuckExecutionRecoveryFunction": "phase6_lambda.stuck_execution_recovery_handler",
    "TerminalOperationalCleanupFunction": "phase6_lambda.terminal_operational_cleanup_handler",
    "UploadApiFunction": "phase6_lambda.upload_api_handler",
}

_SOURCE_RESERVED_CONCURRENCY = {
    "SourceVersionRetentionFunction": 1,
    "StuckExecutionRecoveryFunction": 1,
    "TerminalOperationalCleanupFunction": 1,
}
_STAGED_RESERVED_CONCURRENCY = {logical_id: 0 for logical_id in _SOURCE_RESERVED_CONCURRENCY}

_DISABLED_SAM_TRIGGER_SPECS = (
    ("DispatcherFunction", "DueWorkSweep", "Schedule"),
    ("DispatcherFunction", "OperationalStateChanges", "DynamoDB"),
    ("SourceVersionRetentionFunction", "SourceVersionRetentionSweep", "Schedule"),
    (
        "TerminalOperationalCleanupFunction",
        "TerminalOperationalCleanupSweep",
        "Schedule",
    ),
)
_DISABLED_EVENT_RULE_SPECS = ("StuckExecutionRecoveryScheduleRule",)
_HTTP_API_RESOURCE_TYPES = frozenset(
    {
        "AWS::ApiGateway::RestApi",
        "AWS::ApiGatewayV2::Api",
        "AWS::Serverless::Api",
        "AWS::Serverless::HttpApi",
    }
)

_PARAMETER_FIELDS = {
    "AgentCoreRuntimeArn": "agentcore_runtime_arn",
    "AgentCoreRuntimeBindingFingerprint": "agentcore_runtime_binding_fingerprint",
    "AgentCoreRuntimeEndpointArn": "agentcore_runtime_endpoint_arn",
    "AgentCoreRuntimeQualifier": "agentcore_runtime_qualifier",
    "AgentCoreRuntimeVersion": "agentcore_runtime_version",
    "ApplicationCertificateArn": "application_certificate_arn",
    "ApplicationOrigin": "application_origin",
    "EnvironmentName": "environment",
    "PrintifySecretArn": "printify_secret_arn",
    "ReleaseFingerprint": "release_fingerprint",
}


class Phase6SamStagingError(RuntimeError):
    """A value-free failure for mutable, drifting, or unsealed staging input."""


@dataclass(frozen=True, slots=True)
class Phase6SamStagingBinding:
    """All external identities needed to render one exact staged deployment."""

    account_id: str
    region: str
    environment: str
    release_fingerprint: str
    agentcore_runtime_arn: str
    agentcore_runtime_endpoint_arn: str
    agentcore_runtime_version: str
    agentcore_runtime_qualifier: str
    agentcore_runtime_binding_fingerprint: str
    printify_secret_arn: str
    application_origin: str
    application_certificate_arn: str
    lambda_artifact_bucket: str
    lambda_artifact_key: str
    lambda_artifact_version: str

    def __post_init__(self) -> None:
        try:
            values = tuple(getattr(self, field) for field in self.__dataclass_fields__)
            if any(not _is_exact_input(value) for value in values):
                raise ValueError
            expected_artifact_bucket = (
                f"mr-lister-phase6-artifacts-{self.environment}-{self.account_id}-{self.region}"
            )
            if (
                _ACCOUNT_ID.fullmatch(self.account_id) is None
                or self.account_id == "0" * 12
                or self.region != _REGION
                or _ENVIRONMENT.fullmatch(self.environment) is None
                or self.environment.casefold() in _MOVING_VALUES
                or _FINGERPRINT.fullmatch(self.release_fingerprint) is None
                or self.release_fingerprint == "0" * 64
                or not _valid_s3_bucket(self.lambda_artifact_bucket)
                or self.lambda_artifact_bucket != expected_artifact_bucket
                or not _valid_s3_key(self.lambda_artifact_key)
                or _ORIGIN.fullmatch(self.application_origin) is None
                or ".." in self.application_origin.removeprefix("https://")
            ):
                raise ValueError
            validate_phase6_s3_version_id(self.lambda_artifact_version)

            runtime_environment = {
                "MR_LISTER_AGENTCORE_RUNTIME_ARN": self.agentcore_runtime_arn,
                "MR_LISTER_AGENTCORE_RUNTIME_BINDING_FINGERPRINT": (
                    self.agentcore_runtime_binding_fingerprint
                ),
                "MR_LISTER_AGENTCORE_RUNTIME_ENDPOINT_ARN": self.agentcore_runtime_endpoint_arn,
                "MR_LISTER_AGENTCORE_RUNTIME_QUALIFIER": self.agentcore_runtime_qualifier,
                "MR_LISTER_AGENTCORE_RUNTIME_VERSION": self.agentcore_runtime_version,
            }
            verified_runtime = load_agentcore_runtime_binding(
                runtime_environment,
                region=self.region,
                account_id=self.account_id,
                environment_name=self.environment,
                release_fingerprint=self.release_fingerprint,
            )
            if not self.agentcore_runtime_endpoint_arn.endswith(
                f"/runtime-endpoint/{verified_runtime.qualifier}"
            ):
                raise ValueError

            expected_secret_prefix = (
                f"arn:aws:secretsmanager:{self.region}:{self.account_id}:secret:mr-lister/"
            )
            secret_suffix = self.printify_secret_arn.removeprefix(expected_secret_prefix)
            if (
                not self.printify_secret_arn.startswith(expected_secret_prefix)
                or re.fullmatch(r"[A-Za-z0-9/_-]+-[A-Za-z0-9]{6}", secret_suffix) is None
            ):
                raise ValueError

            expected_certificate_prefix = f"arn:aws:acm:us-east-1:{self.account_id}:certificate/"
            certificate_id = self.application_certificate_arn.removeprefix(
                expected_certificate_prefix
            )
            if (
                not self.application_certificate_arn.startswith(expected_certificate_prefix)
                or _UUID.fullmatch(certificate_id) is None
            ):
                raise ValueError
        except Exception:
            raise Phase6SamStagingError(_GENERIC_ERROR) from None


@dataclass(frozen=True, slots=True)
class _VerifiedArtifactSet:
    descriptor_sha256: str
    agentcore_archive: VerifiedAgentCoreArchive
    agentcore_object: VerifiedPhase6S3ReleaseObject
    lambda_archive_sha256: str
    lambda_archive_size_bytes: int
    lambda_object: VerifiedPhase6S3ReleaseObject


@dataclass(frozen=True, slots=True)
class _EndpointEvidence:
    sha256: str
    document: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class _StateMachineAuthority:
    path: Path
    sha256: str
    start_at: str
    states: frozenset[str]
    substitutions: frozenset[str]


@dataclass(frozen=True, slots=True)
class _LoadedStateMachineDefinition:
    authority: _StateMachineAuthority
    definition: Mapping[str, object]


_STATE_MACHINE_AUTHORITIES = {
    "PrepareStateMachine": _StateMachineAuthority(
        path=Path("infra/phase6/statemachine/prepare.asl.json"),
        sha256="c8ad39e393fa82e00d08d68aab684315167d5bed08e7bceb248bbef9f3826031",
        start_at="RunPreparationDispatcher",
        states=frozenset(
            {
                "RunPreparationDispatcher",
                "SettlePreparationFailure",
                "SettlePreparationSuccess",
            }
        ),
        substitutions=frozenset({"PreparationDispatchFunctionArn", "SettlementFunctionArn"}),
    ),
    "ReconcileProductStateMachine": _StateMachineAuthority(
        path=Path("infra/phase6/statemachine/reconcile-product.asl.json"),
        sha256="da9de08270b43e5a4a05814ab084463c939ccbd512649020388e41318f0bc097",
        start_at="ReadProviderDraftEvidence",
        states=frozenset(
            {
                "ReadProviderDraftEvidence",
                "SettleReconciliationFailure",
                "SettleReconciliationSuccess",
            }
        ),
        substitutions=frozenset({"ProviderDraftFunctionArn", "SettlementFunctionArn"}),
    ),
    "RefreshEconomicsStateMachine": _StateMachineAuthority(
        path=Path("infra/phase6/statemachine/refresh-economics.asl.json"),
        sha256="c105021f581ad84a55526bf6713b63dbbfb55eda6a80fc04b085f3afdefe534d",
        start_at="ReadProviderEconomics",
        states=frozenset(
            {
                "ReadProviderEconomics",
                "SettleEconomicsFailure",
                "SettleEconomicsSuccess",
            }
        ),
        substitutions=frozenset({"ProviderDraftFunctionArn", "SettlementFunctionArn"}),
    ),
    "SynchronizeProductStateMachine": _StateMachineAuthority(
        path=Path("infra/phase6/statemachine/synchronize-product.asl.json"),
        sha256="7d439e439e325a118fdf5e899bc70bf67a729efa321a90060d231007f5e1b86d",
        start_at="RunProviderDraftWorker",
        states=frozenset(
            {
                "RunProviderDraftWorker",
                "SettleProductSyncFailure",
                "SettleProductSyncSuccess",
            }
        ),
        substitutions=frozenset({"ProviderDraftFunctionArn", "SettlementFunctionArn"}),
    ),
}


def render_phase6_sam_staged_template(
    binding: Phase6SamStagingBinding,
    *,
    agentcore_endpoint_observation_path: Path,
    agentcore_object_evidence_path: Path,
    agentcore_runtime_v1_evidence_path: Path,
    lambda_object_evidence_path: Path,
    repository_root: Path = ROOT,
    deployment_root: Path = DEFAULT_DEPLOYMENT_ROOT,
    artifact_root: Path = DEFAULT_ARTIFACT_ROOT,
) -> bytes:
    """Verify sealed artifacts and return one canonical fail-closed staged template."""

    try:
        if not isinstance(binding, Phase6SamStagingBinding):
            raise ValueError
        repository = repository_root.resolve(strict=True)
        source = _load_scaffold_template(repository)
        state_machine_definitions = _load_state_machine_definitions(repository, source)
        artifact_set = _verify_artifact_set(
            binding,
            deployment_root=deployment_root,
            artifact_root=artifact_root,
            agentcore_object_evidence_path=agentcore_object_evidence_path,
            lambda_object_evidence_path=lambda_object_evidence_path,
        )
        runtime = _load_ready_runtime_v1_evidence(
            agentcore_runtime_v1_evidence_path,
            binding,
            artifact_set,
            repository,
        )
        endpoint = _load_ready_endpoint_observation(
            agentcore_endpoint_observation_path,
            binding,
            runtime,
        )
        rendered = _render_staged_document(
            binding,
            source,
            artifact_set,
            runtime,
            endpoint,
            state_machine_definitions,
        )
        _validate_staged_document(
            binding,
            rendered,
            artifact_set,
            runtime,
            endpoint,
            state_machine_definitions,
        )
        return _canonical_json(rendered)
    except Phase6SamStagingError:
        raise
    except Exception:
        raise Phase6SamStagingError(_GENERIC_ERROR) from None


def write_phase6_sam_staged_template(
    binding: Phase6SamStagingBinding,
    *,
    agentcore_endpoint_observation_path: Path,
    agentcore_object_evidence_path: Path,
    agentcore_runtime_v1_evidence_path: Path,
    lambda_object_evidence_path: Path,
    repository_root: Path = ROOT,
    deployment_root: Path = DEFAULT_DEPLOYMENT_ROOT,
    artifact_root: Path = DEFAULT_ARTIFACT_ROOT,
) -> Path:
    """Create the ignored local staged template, refusing every preexisting destination."""

    try:
        repository = repository_root.resolve(strict=True)
        destination = _staged_destination(repository)
        if destination.exists() or destination.is_symlink():
            raise ValueError
        content = render_phase6_sam_staged_template(
            binding,
            agentcore_endpoint_observation_path=agentcore_endpoint_observation_path,
            agentcore_object_evidence_path=agentcore_object_evidence_path,
            agentcore_runtime_v1_evidence_path=agentcore_runtime_v1_evidence_path,
            lambda_object_evidence_path=lambda_object_evidence_path,
            repository_root=repository,
            deployment_root=deployment_root,
            artifact_root=artifact_root,
        )
        _prepare_private_parent(repository, destination.parent)
        with destination.open("xb") as stream:
            stream.write(content)
        destination.chmod(0o600)
        return destination
    except Phase6SamStagingError:
        raise
    except Exception:
        raise Phase6SamStagingError(_GENERIC_ERROR) from None


def verify_rendered_phase6_sam_staged_template(
    binding: Phase6SamStagingBinding,
    *,
    agentcore_endpoint_observation_path: Path,
    agentcore_object_evidence_path: Path,
    agentcore_runtime_v1_evidence_path: Path,
    lambda_object_evidence_path: Path,
    repository_root: Path = ROOT,
    deployment_root: Path = DEFAULT_DEPLOYMENT_ROOT,
    artifact_root: Path = DEFAULT_ARTIFACT_ROOT,
) -> None:
    """Reject a missing or byte-drifted staged template and any sealed-artifact drift."""

    try:
        repository = repository_root.resolve(strict=True)
        destination = _staged_destination(repository)
        expected = render_phase6_sam_staged_template(
            binding,
            agentcore_endpoint_observation_path=agentcore_endpoint_observation_path,
            agentcore_object_evidence_path=agentcore_object_evidence_path,
            agentcore_runtime_v1_evidence_path=agentcore_runtime_v1_evidence_path,
            lambda_object_evidence_path=lambda_object_evidence_path,
            repository_root=repository,
            deployment_root=deployment_root,
            artifact_root=artifact_root,
        )
        if (
            destination.is_symlink()
            or not destination.is_file()
            or destination.read_bytes() != expected
        ):
            raise ValueError
    except Phase6SamStagingError:
        raise
    except Exception:
        raise Phase6SamStagingError(_GENERIC_ERROR) from None


def reject_phase6_sam_activation() -> NoReturn:
    """Fail closed until a separate live staged-deployment evidence verifier exists."""

    raise Phase6SamStagingError(_ACTIVATION_ERROR)


def verify_phase6_sam_staged_inertness(document: Mapping[str, object]) -> None:
    """Require exact zero concurrency, closed triggers, and external serving."""

    try:
        _require_exact_staged_reserved_concurrency(document)
        _require_exact_disabled_triggers(document)
        _require_exact_disabled_external_serving(document)
    except Exception:
        raise Phase6SamStagingError(_GENERIC_ERROR) from None


def _verify_artifact_set(
    binding: Phase6SamStagingBinding,
    *,
    deployment_root: Path,
    artifact_root: Path,
    agentcore_object_evidence_path: Path,
    lambda_object_evidence_path: Path,
) -> _VerifiedArtifactSet:
    if deployment_root.is_symlink() or artifact_root.is_symlink():
        raise ValueError
    deployment = deployment_root.resolve(strict=True)
    artifacts = artifact_root.resolve(strict=True)
    if (
        deployment.name != "phase6-deployment"
        or artifacts.name != "phase6-artifacts"
        or not deployment.is_dir()
        or not artifacts.is_dir()
    ):
        raise ValueError

    descriptor = verify_phase6_deployment_artifacts(
        deployment,
        artifact_root=artifacts,
        verify_current_source=True,
    )
    descriptor_path = artifacts / DEPLOYMENT_DESCRIPTOR_FILENAME
    if descriptor_path.is_symlink() or not descriptor_path.is_file():
        raise ValueError
    raw_descriptor = descriptor_path.read_bytes()
    parsed = json.loads(raw_descriptor)
    if (
        not isinstance(descriptor, Mapping)
        or not isinstance(parsed, Mapping)
        or dict(descriptor) != dict(parsed)
        or render_manifest(parsed) != raw_descriptor
        or set(parsed) != {"algorithm", "components", "format", "release_fingerprint", "target"}
        or parsed.get("algorithm") != "sha256"
        or parsed.get("format") != "phase6-deployment-artifacts-v1"
        or parsed.get("release_fingerprint") != binding.release_fingerprint
        or parsed.get("target") != LINUX_ARM64_TARGET
    ):
        raise ValueError

    components = parsed.get("components")
    if not isinstance(components, Mapping) or set(components) != {"agentcore", "lambda"}:
        raise ValueError
    expected_names = {
        "agentcore": AGENTCORE_ARCHIVE_FILENAME,
        "lambda": LAMBDA_ARCHIVE_FILENAME,
    }
    for component, filename in expected_names.items():
        record = components.get(component)
        if not isinstance(record, Mapping) or set(record) != {
            "archive",
            "architecture",
            "component",
            "deployment_manifest_sha256",
            "package_format",
            "runtime",
        }:
            raise ValueError
        archive = record.get("archive")
        if (
            not isinstance(archive, Mapping)
            or set(archive) != {"path", "sha256", "size_bytes"}
            or archive.get("path") != filename
            or record.get("architecture") != "arm64"
            or record.get("component") != component
            or record.get("package_format") != "zip"
            or record.get("runtime") != "python3.12"
            or _FINGERPRINT.fullmatch(str(record.get("deployment_manifest_sha256"))) is None
            or _FINGERPRINT.fullmatch(str(archive.get("sha256"))) is None
            or not isinstance(archive.get("size_bytes"), int)
            or isinstance(archive.get("size_bytes"), bool)
            or archive.get("size_bytes", 0) <= 0
        ):
            raise ValueError

    lambda_record = components["lambda"]
    assert isinstance(lambda_record, Mapping)
    lambda_archive = lambda_record["archive"]
    assert isinstance(lambda_archive, Mapping)
    archive_path = artifacts / LAMBDA_ARCHIVE_FILENAME
    if archive_path.is_symlink() or not archive_path.is_file():
        raise ValueError
    raw_archive = archive_path.read_bytes()
    archive_sha256 = sha256(raw_archive).hexdigest()
    archive_size = len(raw_archive)
    if (
        lambda_archive.get("sha256") != archive_sha256
        or lambda_archive.get("size_bytes") != archive_size
    ):
        raise ValueError

    agentcore_record = components["agentcore"]
    assert isinstance(agentcore_record, Mapping)
    agentcore_archive_record = agentcore_record["archive"]
    assert isinstance(agentcore_archive_record, Mapping)
    agentcore_archive_path = artifacts / AGENTCORE_ARCHIVE_FILENAME
    if agentcore_archive_path.is_symlink() or not agentcore_archive_path.is_file():
        raise ValueError
    raw_agentcore_archive = agentcore_archive_path.read_bytes()
    agentcore_archive_sha256 = sha256(raw_agentcore_archive).hexdigest()
    agentcore_archive_size = len(raw_agentcore_archive)
    if (
        agentcore_archive_record.get("sha256") != agentcore_archive_sha256
        or agentcore_archive_record.get("size_bytes") != agentcore_archive_size
    ):
        raise ValueError
    descriptor_sha256 = sha256(raw_descriptor).hexdigest()
    verified_agentcore_archive = VerifiedAgentCoreArchive(
        sha256=agentcore_archive_sha256,
        size_bytes=agentcore_archive_size,
        checksum_sha256_base64=base64.b64encode(sha256(raw_agentcore_archive).digest()).decode(
            "ascii"
        ),
        descriptor_sha256=descriptor_sha256,
    )

    expectation = Phase6S3ReleaseObjectExpectation(
        account_id=binding.account_id,
        region=binding.region,
        environment=binding.environment,
        component="lambda",
        release_fingerprint=binding.release_fingerprint,
        archive_sha256=archive_sha256,
        size_bytes=archive_size,
    )
    object_binding = verify_phase6_s3_release_object_evidence(
        expectation,
        evidence_path=lambda_object_evidence_path,
    )
    if (
        object_binding.account_id != binding.account_id
        or object_binding.region != binding.region
        or object_binding.environment != binding.environment
        or object_binding.component != "lambda"
        or object_binding.release_fingerprint != binding.release_fingerprint
        or object_binding.bucket != binding.lambda_artifact_bucket
        or object_binding.bucket != expectation.bucket
        or object_binding.key != binding.lambda_artifact_key
        or object_binding.key != expectation.key
        or object_binding.version_id != binding.lambda_artifact_version
        or object_binding.archive_sha256 != archive_sha256
        or object_binding.archive_sha256 != expectation.archive_sha256
        or object_binding.size_bytes != archive_size
        or object_binding.size_bytes != expectation.size_bytes
        or object_binding.checksum_sha256_base64 != expectation.checksum_sha256_base64
        or _FINGERPRINT.fullmatch(object_binding.evidence_sha256) is None
        or object_binding.evidence_sha256 == "0" * 64
    ):
        raise ValueError
    agentcore_expectation = Phase6S3ReleaseObjectExpectation(
        account_id=binding.account_id,
        region=binding.region,
        environment=binding.environment,
        component="agentcore",
        release_fingerprint=binding.release_fingerprint,
        archive_sha256=agentcore_archive_sha256,
        size_bytes=agentcore_archive_size,
    )
    agentcore_object = verify_phase6_s3_release_object_evidence(
        agentcore_expectation,
        evidence_path=agentcore_object_evidence_path,
    )
    if (
        agentcore_object.account_id != binding.account_id
        or agentcore_object.region != binding.region
        or agentcore_object.environment != binding.environment
        or agentcore_object.component != "agentcore"
        or agentcore_object.release_fingerprint != binding.release_fingerprint
        or agentcore_object.bucket != binding.lambda_artifact_bucket
        or agentcore_object.bucket != agentcore_expectation.bucket
        or agentcore_object.key != agentcore_expectation.key
        or agentcore_object.archive_sha256 != agentcore_archive_sha256
        or agentcore_object.size_bytes != agentcore_archive_size
        or agentcore_object.checksum_sha256_base64 != agentcore_expectation.checksum_sha256_base64
        or _FINGERPRINT.fullmatch(agentcore_object.evidence_sha256) is None
        or agentcore_object.evidence_sha256 == "0" * 64
    ):
        raise ValueError
    return _VerifiedArtifactSet(
        descriptor_sha256=descriptor_sha256,
        agentcore_archive=verified_agentcore_archive,
        agentcore_object=agentcore_object,
        lambda_archive_sha256=archive_sha256,
        lambda_archive_size_bytes=archive_size,
        lambda_object=object_binding,
    )


def _load_ready_runtime_v1_evidence(
    path: Path,
    binding: Phase6SamStagingBinding,
    artifacts: _VerifiedArtifactSet,
    repository: Path,
) -> VerifiedAgentCoreRuntimeV1:
    direct_binding = Phase6AgentCoreDirectCodeZipBinding(
        account_id=binding.account_id,
        release_fingerprint=binding.release_fingerprint,
        agentcore_archive_sha256=artifacts.agentcore_archive.sha256,
    )
    runtime = verify_phase6_agentcore_runtime_v1_evidence(
        direct_binding,
        artifacts.agentcore_archive,
        artifacts.agentcore_object,
        runtime_v1_evidence_path=path,
        repository_root=repository,
    )
    if (
        runtime.runtime_arn != binding.agentcore_runtime_arn
        or binding.agentcore_runtime_version != "1"
    ):
        raise ValueError
    return runtime


def _load_ready_endpoint_observation(
    path: Path,
    binding: Phase6SamStagingBinding,
    runtime: VerifiedAgentCoreRuntimeV1,
) -> _EndpointEvidence:
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
    if (
        not isinstance(document, Mapping)
        or _canonical_json(document) != raw
        or binding.agentcore_runtime_arn != runtime.runtime_arn
        or binding.agentcore_runtime_version != "1"
        or binding.agentcore_runtime_qualifier != "phase6_v1_dev"
    ):
        raise ValueError
    runtime_binding = load_agentcore_runtime_binding(
        {
            "MR_LISTER_AGENTCORE_RUNTIME_ARN": binding.agentcore_runtime_arn,
            "MR_LISTER_AGENTCORE_RUNTIME_BINDING_FINGERPRINT": (
                binding.agentcore_runtime_binding_fingerprint
            ),
            "MR_LISTER_AGENTCORE_RUNTIME_ENDPOINT_ARN": binding.agentcore_runtime_endpoint_arn,
            "MR_LISTER_AGENTCORE_RUNTIME_QUALIFIER": binding.agentcore_runtime_qualifier,
            "MR_LISTER_AGENTCORE_RUNTIME_VERSION": binding.agentcore_runtime_version,
        },
        region=binding.region,
        account_id=binding.account_id,
        environment_name=binding.environment,
        release_fingerprint=binding.release_fingerprint,
    )
    verify_phase6_agentcore_endpoint_observation(runtime_binding, document)
    return _EndpointEvidence(sha256=sha256(raw).hexdigest(), document=document)


def _load_scaffold_template(repository: Path) -> dict[str, object]:
    path = repository / SOURCE_TEMPLATE
    if path.is_symlink() or not path.is_file():
        raise ValueError
    raw = path.read_bytes()
    if sha256(raw).hexdigest() != _SOURCE_TEMPLATE_SHA256:
        raise ValueError
    document = json.loads(raw)
    if not isinstance(document, dict):
        raise ValueError
    functions = _function_resources(document)
    if set(functions) != set(_FUNCTION_HANDLERS):
        raise ValueError
    for logical_id, handler in _FUNCTION_HANDLERS.items():
        properties = functions[logical_id].get("Properties")
        if (
            not isinstance(properties, Mapping)
            or properties.get("Handler") != handler
            or properties.get("CodeUri") != "lambda/"
        ):
            raise ValueError
    if _reserved_concurrency_inventory(document) != _SOURCE_RESERVED_CONCURRENCY:
        raise ValueError
    state_machines = _state_machine_resources(document)
    if set(state_machines) != set(_STATE_MACHINE_AUTHORITIES):
        raise ValueError
    for logical_id, authority in _STATE_MACHINE_AUTHORITIES.items():
        properties = state_machines[logical_id].get("Properties")
        if not isinstance(properties, Mapping):
            raise ValueError
        expected_uri = authority.path.relative_to(SOURCE_TEMPLATE.parent).as_posix()
        substitutions = properties.get("DefinitionSubstitutions")
        if (
            properties.get("DefinitionUri") != expected_uri
            or "Definition" in properties
            or not isinstance(substitutions, Mapping)
            or set(substitutions) != authority.substitutions
        ):
            raise ValueError
    parameters = document.get("Parameters")
    globals_value = document.get("Globals")
    outputs = document.get("Outputs")
    if (
        not isinstance(parameters, Mapping)
        or set(parameters) != set(_PARAMETER_FIELDS)
        or not isinstance(globals_value, Mapping)
        or not isinstance(outputs, Mapping)
        or _global_variables(document).get("MR_LISTER_PHASE6_SCAFFOLD_ONLY") != "true"
        or outputs.get("DeploymentReadiness")
        != {
            "Description": (
                "This stack is an infrastructure scaffold until the Phase 6 package handlers "
                "replace the fail-closed shims."
            ),
            "Value": "SCAFFOLD_ONLY",
        }
        or "Metadata" in document
    ):
        raise ValueError
    return document


def _load_state_machine_definitions(
    repository: Path,
    source: Mapping[str, object],
) -> dict[str, _LoadedStateMachineDefinition]:
    state_machines = _state_machine_resources(source)
    if set(state_machines) != set(_STATE_MACHINE_AUTHORITIES):
        raise ValueError
    loaded: dict[str, _LoadedStateMachineDefinition] = {}
    for logical_id, authority in _STATE_MACHINE_AUTHORITIES.items():
        path = repository / authority.path
        if _path_has_symlink_component(repository, authority.path) or not path.is_file():
            raise ValueError
        raw = path.read_bytes()
        if sha256(raw).hexdigest() != authority.sha256:
            raise ValueError
        definition = json.loads(raw, object_pairs_hook=_unique_json_object)
        if not isinstance(definition, Mapping):
            raise ValueError
        _validate_state_machine_definition(definition, authority)
        loaded[logical_id] = _LoadedStateMachineDefinition(
            authority=authority,
            definition=definition,
        )
    return loaded


def _path_has_symlink_component(root: Path, relative: Path) -> bool:
    current = root
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            return True
    return False


def _validate_state_machine_definition(
    definition: Mapping[str, object],
    authority: _StateMachineAuthority,
) -> None:
    states = definition.get("States")
    if (
        set(definition) != {"Comment", "StartAt", "States", "TimeoutSeconds"}
        or not isinstance(definition.get("Comment"), str)
        or not definition.get("Comment")
        or definition.get("StartAt") != authority.start_at
        or definition.get("TimeoutSeconds") != 900
        or isinstance(definition.get("TimeoutSeconds"), bool)
        or not isinstance(states, Mapping)
        or set(states) != authority.states
    ):
        raise ValueError

    substitutions: set[str] = set()
    for state_name, state in states.items():
        if not isinstance(state_name, str) or not isinstance(state, Mapping):
            raise ValueError
        if state.get("Type") != "Task" or state.get("Resource") != "arn:aws:states:::lambda:invoke":
            raise ValueError
        has_next = "Next" in state
        has_end = state.get("End") is True
        if has_next == has_end:
            raise ValueError
        if has_next and state.get("Next") not in states:
            raise ValueError
        catches = state.get("Catch", [])
        if not isinstance(catches, list):
            raise ValueError
        for catch in catches:
            if not isinstance(catch, Mapping) or catch.get("Next") not in states:
                raise ValueError
        substitutions.update(_definition_substitution_tokens(state))
    if substitutions != set(authority.substitutions):
        raise ValueError


def _definition_substitution_tokens(value: object) -> set[str]:
    found: set[str] = set()
    if isinstance(value, str):
        match = re.fullmatch(r"\$\{([A-Za-z][A-Za-z0-9]*)\}", value)
        if match is not None:
            found.add(match.group(1))
    elif isinstance(value, list):
        for item in value:
            found.update(_definition_substitution_tokens(item))
    elif isinstance(value, Mapping):
        for item in value.values():
            found.update(_definition_substitution_tokens(item))
    return found


def _unique_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError
        value[key] = item
    return value


def _render_staged_document(
    binding: Phase6SamStagingBinding,
    source: Mapping[str, object],
    artifact_set: _VerifiedArtifactSet,
    runtime: VerifiedAgentCoreRuntimeV1,
    endpoint: _EndpointEvidence,
    state_machine_definitions: Mapping[str, _LoadedStateMachineDefinition],
) -> dict[str, object]:
    rendered = deepcopy(dict(source))
    parameters = rendered["Parameters"]
    assert isinstance(parameters, dict)
    for parameter_name, field_name in _PARAMETER_FIELDS.items():
        definition = parameters[parameter_name]
        if not isinstance(definition, dict):
            raise ValueError
        value = getattr(binding, field_name)
        definition["AllowedValues"] = [value]
        definition["Default"] = value

    code_uri = {
        "Bucket": binding.lambda_artifact_bucket,
        "Key": binding.lambda_artifact_key,
        "Version": binding.lambda_artifact_version,
    }
    for logical_id, function in _function_resources(rendered).items():
        properties = function["Properties"]
        assert isinstance(properties, dict)
        properties["CodeUri"] = deepcopy(code_uri)
        if logical_id in _STAGED_RESERVED_CONCURRENCY:
            properties["ReservedConcurrentExecutions"] = _STAGED_RESERVED_CONCURRENCY[logical_id]

    rendered_state_machines = _state_machine_resources(rendered)
    if set(rendered_state_machines) != set(state_machine_definitions):
        raise ValueError
    for logical_id, loaded in state_machine_definitions.items():
        properties = rendered_state_machines[logical_id].get("Properties")
        if not isinstance(properties, dict) or properties.pop("DefinitionUri", None) is None:
            raise ValueError
        properties["Definition"] = deepcopy(dict(loaded.definition))

    _disable_staging_triggers(rendered)
    _disable_external_serving(rendered)

    _global_variables(rendered)["MR_LISTER_PHASE6_SCAFFOLD_ONLY"] = "true"
    outputs = rendered["Outputs"]
    assert isinstance(outputs, dict)
    outputs["DeploymentReadiness"] = {
        "Description": (
            "The exact sealed Phase 6 artifacts and READY AgentCore v1 are staged; runtime and "
            "web activation remain fail-closed."
        ),
        "Value": "RELEASE_BOUND_STAGED",
    }
    rendered["Metadata"] = {
        "MrListerPhase6StagedDeployment": {
            "ArtifactDescriptorSha256": artifact_set.descriptor_sha256,
            "AgentCore": {
                "BindingFingerprint": binding.agentcore_runtime_binding_fingerprint,
                "EndpointArn": binding.agentcore_runtime_endpoint_arn,
                "EndpointObservationSha256": endpoint.sha256,
                "Qualifier": binding.agentcore_runtime_qualifier,
                "RuntimeArn": binding.agentcore_runtime_arn,
                "RuntimeCreateInputSha256": runtime.runtime_create_input_sha256,
                "RuntimeEvidenceSha256": runtime.evidence_sha256,
                "RuntimeRenderManifestSha256": runtime.runtime_render_manifest_sha256,
                "Status": "READY",
                "Version": binding.agentcore_runtime_version,
            },
            "AgentCoreArtifact": {
                "Bucket": artifact_set.agentcore_object.bucket,
                "ChecksumSHA256Base64": artifact_set.agentcore_object.checksum_sha256_base64,
                "Key": artifact_set.agentcore_object.key,
                "ObjectEvidenceSha256": artifact_set.agentcore_object.evidence_sha256,
                "Sha256": artifact_set.agentcore_archive.sha256,
                "SizeBytes": artifact_set.agentcore_archive.size_bytes,
                "Version": artifact_set.agentcore_object.version_id,
            },
            "DisabledExternalServing": _expected_disabled_external_serving_metadata(),
            "DisabledTriggers": _expected_disabled_trigger_metadata(),
            "Format": "mr-lister-phase6-sam-staged-v1",
            "LambdaArtifact": {
                **code_uri,
                "ChecksumSHA256Base64": artifact_set.lambda_object.checksum_sha256_base64,
                "ObjectEvidenceSha256": artifact_set.lambda_object.evidence_sha256,
                "Sha256": artifact_set.lambda_archive_sha256,
                "SizeBytes": artifact_set.lambda_archive_size_bytes,
            },
            "Mode": "STAGED_FAIL_CLOSED",
            "ReleaseFingerprint": binding.release_fingerprint,
            "SourceTemplateSha256": _SOURCE_TEMPLATE_SHA256,
            "StateMachineDefinitions": _state_machine_metadata(state_machine_definitions),
            "Target": {
                "AccountId": binding.account_id,
                "Environment": binding.environment,
                "Region": binding.region,
            },
        }
    }
    return rendered


def _validate_staged_document(
    binding: Phase6SamStagingBinding,
    document: Mapping[str, object],
    artifact_set: _VerifiedArtifactSet,
    runtime: VerifiedAgentCoreRuntimeV1,
    endpoint: _EndpointEvidence,
    state_machine_definitions: Mapping[str, _LoadedStateMachineDefinition],
) -> None:
    functions = _function_resources(document)
    code_uri = {
        "Bucket": binding.lambda_artifact_bucket,
        "Key": binding.lambda_artifact_key,
        "Version": binding.lambda_artifact_version,
    }
    if (
        set(functions) != set(_FUNCTION_HANDLERS)
        or any(
            not isinstance(function.get("Properties"), Mapping)
            or function["Properties"].get("CodeUri") != code_uri  # type: ignore[union-attr]
            for function in functions.values()
        )
        or _global_variables(document).get("MR_LISTER_PHASE6_SCAFFOLD_ONLY") != "true"
        or _global_variables(document).get("MR_LISTER_RELEASE_FINGERPRINT")
        != {"Ref": "ReleaseFingerprint"}
    ):
        raise ValueError
    _require_exact_staged_reserved_concurrency(document)
    _require_exact_disabled_triggers(document)
    _require_exact_disabled_external_serving(document)
    state_machines = _state_machine_resources(document)
    if set(state_machines) != set(state_machine_definitions):
        raise ValueError
    for logical_id, loaded in state_machine_definitions.items():
        properties = state_machines[logical_id].get("Properties")
        if (
            not isinstance(properties, Mapping)
            or "DefinitionUri" in properties
            or properties.get("Definition") != loaded.definition
            or not isinstance(properties.get("DefinitionSubstitutions"), Mapping)
            or set(properties["DefinitionSubstitutions"])  # type: ignore[arg-type]
            != loaded.authority.substitutions
        ):
            raise ValueError
    _reject_local_deployment_references(document)
    parameters = document.get("Parameters")
    if not isinstance(parameters, Mapping) or set(parameters) != set(_PARAMETER_FIELDS):
        raise ValueError
    for parameter_name, field_name in _PARAMETER_FIELDS.items():
        definition = parameters.get(parameter_name)
        expected = getattr(binding, field_name)
        if (
            not isinstance(definition, Mapping)
            or definition.get("Default") != expected
            or definition.get("AllowedValues") != [expected]
        ):
            raise ValueError
    outputs = document.get("Outputs")
    if not isinstance(outputs, Mapping) or outputs.get("DeploymentReadiness") != {
        "Description": (
            "The exact sealed Phase 6 artifacts and READY AgentCore v1 are staged; runtime and "
            "web activation remain fail-closed."
        ),
        "Value": "RELEASE_BOUND_STAGED",
    }:
        raise ValueError
    expected_metadata = {
        "MrListerPhase6StagedDeployment": {
            "ArtifactDescriptorSha256": artifact_set.descriptor_sha256,
            "AgentCore": {
                "BindingFingerprint": binding.agentcore_runtime_binding_fingerprint,
                "EndpointArn": binding.agentcore_runtime_endpoint_arn,
                "EndpointObservationSha256": endpoint.sha256,
                "Qualifier": binding.agentcore_runtime_qualifier,
                "RuntimeArn": binding.agentcore_runtime_arn,
                "RuntimeCreateInputSha256": runtime.runtime_create_input_sha256,
                "RuntimeEvidenceSha256": runtime.evidence_sha256,
                "RuntimeRenderManifestSha256": runtime.runtime_render_manifest_sha256,
                "Status": "READY",
                "Version": binding.agentcore_runtime_version,
            },
            "AgentCoreArtifact": {
                "Bucket": artifact_set.agentcore_object.bucket,
                "ChecksumSHA256Base64": artifact_set.agentcore_object.checksum_sha256_base64,
                "Key": artifact_set.agentcore_object.key,
                "ObjectEvidenceSha256": artifact_set.agentcore_object.evidence_sha256,
                "Sha256": artifact_set.agentcore_archive.sha256,
                "SizeBytes": artifact_set.agentcore_archive.size_bytes,
                "Version": artifact_set.agentcore_object.version_id,
            },
            "DisabledExternalServing": _expected_disabled_external_serving_metadata(),
            "DisabledTriggers": _expected_disabled_trigger_metadata(),
            "Format": "mr-lister-phase6-sam-staged-v1",
            "LambdaArtifact": {
                **code_uri,
                "ChecksumSHA256Base64": artifact_set.lambda_object.checksum_sha256_base64,
                "ObjectEvidenceSha256": artifact_set.lambda_object.evidence_sha256,
                "Sha256": artifact_set.lambda_archive_sha256,
                "SizeBytes": artifact_set.lambda_archive_size_bytes,
            },
            "Mode": "STAGED_FAIL_CLOSED",
            "ReleaseFingerprint": binding.release_fingerprint,
            "SourceTemplateSha256": _SOURCE_TEMPLATE_SHA256,
            "StateMachineDefinitions": _state_machine_metadata(state_machine_definitions),
            "Target": {
                "AccountId": binding.account_id,
                "Environment": binding.environment,
                "Region": binding.region,
            },
        }
    }
    if document.get("Metadata") != expected_metadata:
        raise ValueError


def _disable_staging_triggers(document: Mapping[str, object]) -> None:
    resources = document.get("Resources")
    if (
        not isinstance(resources, Mapping)
        or _automatic_trigger_inventory(document) != _expected_active_trigger_inventory()
    ):
        raise ValueError
    for logical_id, event_name, event_type in _DISABLED_SAM_TRIGGER_SPECS:
        resource = resources.get(logical_id)
        if not isinstance(resource, Mapping):
            raise ValueError
        properties = resource.get("Properties")
        if not isinstance(properties, dict):
            raise ValueError
        events = properties.get("Events")
        if not isinstance(events, Mapping):
            raise ValueError
        event = events.get(event_name)
        if not isinstance(event, Mapping) or event.get("Type") != event_type:
            raise ValueError
        event_properties = event.get("Properties")
        if not isinstance(event_properties, dict):
            raise ValueError
        event_properties["Enabled"] = False
    for logical_id in _DISABLED_EVENT_RULE_SPECS:
        resource = resources.get(logical_id)
        if not isinstance(resource, Mapping):
            raise ValueError
        properties = resource.get("Properties")
        if not isinstance(properties, dict):
            raise ValueError
        properties["State"] = "DISABLED"
    _require_exact_disabled_triggers(document)


def _disable_external_serving(document: Mapping[str, object]) -> None:
    resources = document.get("Resources")
    if (
        not isinstance(resources, Mapping)
        or _external_serving_inventory(document) != _expected_active_external_serving_inventory()
    ):
        raise ValueError
    api = resources.get("SellerHttpApi")
    distribution = resources.get("SellerWebDistribution")
    if not isinstance(api, Mapping) or not isinstance(distribution, Mapping):
        raise ValueError
    api_properties = api.get("Properties")
    distribution_properties = distribution.get("Properties")
    if not isinstance(api_properties, dict) or not isinstance(distribution_properties, Mapping):
        raise ValueError
    distribution_config = distribution_properties.get("DistributionConfig")
    if not isinstance(distribution_config, dict):
        raise ValueError
    api_properties["DisableExecuteApiEndpoint"] = True
    distribution_config["Enabled"] = False
    _require_exact_disabled_external_serving(document)


def _require_exact_disabled_triggers(document: Mapping[str, object]) -> None:
    if _automatic_trigger_inventory(document) != _expected_disabled_trigger_metadata():
        raise ValueError


def _require_exact_staged_reserved_concurrency(document: Mapping[str, object]) -> None:
    if _reserved_concurrency_inventory(document) != _STAGED_RESERVED_CONCURRENCY:
        raise ValueError


def _require_exact_disabled_external_serving(document: Mapping[str, object]) -> None:
    if _external_serving_inventory(document) != _expected_disabled_external_serving_metadata():
        raise ValueError


def _automatic_trigger_inventory(document: Mapping[str, object]) -> dict[str, dict[str, object]]:
    resources = document.get("Resources")
    if not isinstance(resources, Mapping):
        raise ValueError
    inventory: dict[str, dict[str, object]] = {}
    for logical_id, resource in resources.items():
        if not isinstance(logical_id, str) or not isinstance(resource, Mapping):
            raise ValueError
        resource_type = resource.get("Type")
        if resource_type == "AWS::Serverless::Function":
            properties = resource.get("Properties")
            if not isinstance(properties, Mapping):
                raise ValueError
            events = properties.get("Events", {})
            if not isinstance(events, Mapping):
                raise ValueError
            for event_name, event in events.items():
                if not isinstance(event_name, str) or not isinstance(event, Mapping):
                    raise ValueError
                event_type = event.get("Type")
                event_properties = event.get("Properties")
                if not isinstance(event_type, str) or not isinstance(event_properties, Mapping):
                    raise ValueError
                if event_type == "HttpApi":
                    continue
                inventory[f"{logical_id}.Events.{event_name}"] = {
                    "Enabled": event_properties.get("Enabled", "DEFAULT_ENABLED"),
                    "Type": event_type,
                }
        elif resource_type == "AWS::Events::Rule":
            properties = resource.get("Properties")
            if not isinstance(properties, Mapping):
                raise ValueError
            inventory[logical_id] = {
                "State": properties.get("State", "DEFAULT_ENABLED"),
                "Type": resource_type,
            }
    return inventory


def _external_serving_inventory(document: Mapping[str, object]) -> dict[str, dict[str, object]]:
    resources = document.get("Resources")
    if not isinstance(resources, Mapping):
        raise ValueError
    inventory: dict[str, dict[str, object]] = {}
    for logical_id, resource in resources.items():
        if not isinstance(logical_id, str) or not isinstance(resource, Mapping):
            raise ValueError
        resource_type = resource.get("Type")
        properties = resource.get("Properties")
        if resource_type in _HTTP_API_RESOURCE_TYPES:
            if not isinstance(properties, Mapping):
                raise ValueError
            inventory[logical_id] = {
                "DisableExecuteApiEndpoint": properties.get(
                    "DisableExecuteApiEndpoint", "DEFAULT_ENABLED"
                ),
                "Type": resource_type,
            }
        elif resource_type == "AWS::CloudFront::Distribution":
            if not isinstance(properties, Mapping):
                raise ValueError
            config = properties.get("DistributionConfig")
            if not isinstance(config, Mapping):
                raise ValueError
            inventory[logical_id] = {
                "Enabled": config.get("Enabled", "DEFAULT_ENABLED"),
                "Type": resource_type,
            }
    return inventory


def _expected_active_trigger_inventory() -> dict[str, dict[str, object]]:
    inventory = _expected_disabled_trigger_metadata()
    for logical_id, event_name, _event_type in _DISABLED_SAM_TRIGGER_SPECS:
        key = f"{logical_id}.Events.{event_name}"
        inventory[key]["Enabled"] = (
            "DEFAULT_ENABLED" if event_name == "OperationalStateChanges" else True
        )
    for logical_id in _DISABLED_EVENT_RULE_SPECS:
        inventory[logical_id]["State"] = "ENABLED"
    return inventory


def _expected_disabled_trigger_metadata() -> dict[str, dict[str, object]]:
    return {
        **{
            f"{logical_id}.Events.{event_name}": {
                "Enabled": False,
                "Type": event_type,
            }
            for logical_id, event_name, event_type in _DISABLED_SAM_TRIGGER_SPECS
        },
        **{
            logical_id: {"State": "DISABLED", "Type": "AWS::Events::Rule"}
            for logical_id in _DISABLED_EVENT_RULE_SPECS
        },
    }


def _expected_active_external_serving_inventory() -> dict[str, dict[str, object]]:
    inventory = _expected_disabled_external_serving_metadata()
    inventory["SellerHttpApi"]["DisableExecuteApiEndpoint"] = "DEFAULT_ENABLED"
    inventory["SellerWebDistribution"]["Enabled"] = True
    return inventory


def _expected_disabled_external_serving_metadata() -> dict[str, dict[str, object]]:
    return {
        "SellerHttpApi": {
            "DisableExecuteApiEndpoint": True,
            "Type": "AWS::Serverless::HttpApi",
        },
        "SellerWebDistribution": {
            "Enabled": False,
            "Type": "AWS::CloudFront::Distribution",
        },
    }


def _state_machine_metadata(
    definitions: Mapping[str, _LoadedStateMachineDefinition],
) -> dict[str, dict[str, str]]:
    if set(definitions) != set(_STATE_MACHINE_AUTHORITIES):
        raise ValueError
    return {
        logical_id: {
            "Path": loaded.authority.path.as_posix(),
            "Sha256": loaded.authority.sha256,
        }
        for logical_id, loaded in sorted(definitions.items())
    }


def _reject_local_deployment_references(value: object) -> None:
    if isinstance(value, list):
        for item in value:
            _reject_local_deployment_references(item)
        return
    if not isinstance(value, Mapping):
        return
    for key, item in value.items():
        if key == "DefinitionUri":
            raise ValueError
        if key == "CodeUri" and not isinstance(item, Mapping):
            raise ValueError
        _reject_local_deployment_references(item)


def _function_resources(document: Mapping[str, object]) -> dict[str, Mapping[str, object]]:
    resources = document.get("Resources")
    if not isinstance(resources, Mapping):
        raise ValueError
    functions: dict[str, Mapping[str, object]] = {}
    for name, resource in resources.items():
        if not isinstance(name, str) or not isinstance(resource, Mapping):
            raise ValueError
        if resource.get("Type") == "AWS::Serverless::Function":
            functions[name] = resource
    return functions


def _reserved_concurrency_inventory(document: Mapping[str, object]) -> dict[str, object]:
    inventory: dict[str, object] = {}
    for logical_id, function in _function_resources(document).items():
        properties = function.get("Properties")
        if not isinstance(properties, Mapping):
            raise ValueError
        if "ReservedConcurrentExecutions" in properties:
            inventory[logical_id] = properties["ReservedConcurrentExecutions"]
    return inventory


def _state_machine_resources(
    document: Mapping[str, object],
) -> dict[str, Mapping[str, object]]:
    resources = document.get("Resources")
    if not isinstance(resources, Mapping):
        raise ValueError
    state_machines: dict[str, Mapping[str, object]] = {}
    for name, resource in resources.items():
        if not isinstance(name, str) or not isinstance(resource, Mapping):
            raise ValueError
        if resource.get("Type") == "AWS::Serverless::StateMachine":
            state_machines[name] = resource
    return state_machines


def _global_variables(document: Mapping[str, object]) -> dict[str, object]:
    globals_value = document.get("Globals")
    if not isinstance(globals_value, Mapping):
        raise ValueError
    function = globals_value.get("Function")
    if not isinstance(function, Mapping):
        raise ValueError
    environment = function.get("Environment")
    if not isinstance(environment, Mapping):
        raise ValueError
    variables = environment.get("Variables")
    if not isinstance(variables, dict):
        raise ValueError
    return variables


def _canonical_json(value: object) -> bytes:
    return (
        json.dumps(value, allow_nan=False, indent=2, separators=(",", ": "), sort_keys=True) + "\n"
    ).encode("utf-8")


def _is_exact_input(value: object) -> bool:
    return (
        isinstance(value, str)
        and bool(value)
        and value == value.strip()
        and len(value) <= 4096
        and "\x00" not in value
        and _PLACEHOLDER.search(value) is None
    )


def _valid_s3_bucket(value: str) -> bool:
    return (
        _S3_BUCKET.fullmatch(value) is not None
        and ".." not in value
        and not value.startswith("xn--")
        and not value.endswith("-s3alias")
        and re.fullmatch(r"[0-9]+(?:\.[0-9]+){3}", value) is None
    )


def _valid_s3_key(value: str) -> bool:
    if (
        len(value) > 1024
        or not value.isascii()
        or value.startswith("/")
        or value.endswith("/")
        or "//" in value
        or "\\" in value
        or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]*", value) is None
    ):
        return False
    parts = value.split("/")
    return (
        all(part not in {"", ".", ".."} for part in parts)
        and not any(part.casefold() in _MOVING_VALUES for part in parts)
        and PurePosixPath(value).as_posix() == value
    )


def _staged_destination(repository: Path) -> Path:
    destination = repository / STAGED_TEMPLATE_OUTPUT
    if destination.relative_to(repository).parts[0] != ".mr_lister_private":
        raise ValueError
    return destination


def _prepare_private_parent(repository: Path, parent: Path) -> None:
    current = repository
    for part in parent.relative_to(repository).parts:
        current = current / part
        if current.is_symlink():
            raise ValueError
        if current.exists() and not current.is_dir():
            raise ValueError
    parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if parent.is_symlink():
        raise ValueError


def _binding_from_arguments(arguments: argparse.Namespace) -> Phase6SamStagingBinding:
    return Phase6SamStagingBinding(
        account_id=arguments.account_id,
        region=arguments.region,
        environment=arguments.environment,
        release_fingerprint=arguments.release_fingerprint,
        agentcore_runtime_arn=arguments.agentcore_runtime_arn,
        agentcore_runtime_endpoint_arn=arguments.agentcore_runtime_endpoint_arn,
        agentcore_runtime_version=arguments.agentcore_runtime_version,
        agentcore_runtime_qualifier=arguments.agentcore_runtime_qualifier,
        agentcore_runtime_binding_fingerprint=(arguments.agentcore_runtime_binding_fingerprint),
        printify_secret_arn=arguments.printify_secret_arn,
        application_origin=arguments.application_origin,
        application_certificate_arn=arguments.application_certificate_arn,
        lambda_artifact_bucket=arguments.lambda_artifact_bucket,
        lambda_artifact_key=arguments.lambda_artifact_key,
        lambda_artifact_version=arguments.lambda_artifact_version,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--account-id", required=True)
    parser.add_argument("--region", required=True)
    parser.add_argument("--environment", required=True)
    parser.add_argument("--release-fingerprint", required=True)
    parser.add_argument("--agentcore-runtime-arn", required=True)
    parser.add_argument("--agentcore-runtime-endpoint-arn", required=True)
    parser.add_argument("--agentcore-runtime-version", required=True)
    parser.add_argument("--agentcore-runtime-qualifier", required=True)
    parser.add_argument("--agentcore-runtime-binding-fingerprint", required=True)
    parser.add_argument("--agentcore-endpoint-observation", required=True, type=Path)
    parser.add_argument("--agentcore-object-evidence", required=True, type=Path)
    parser.add_argument("--agentcore-runtime-v1-evidence", required=True, type=Path)
    parser.add_argument("--printify-secret-arn", required=True)
    parser.add_argument("--application-origin", required=True)
    parser.add_argument("--application-certificate-arn", required=True)
    parser.add_argument("--lambda-artifact-bucket", required=True)
    parser.add_argument("--lambda-artifact-key", required=True)
    parser.add_argument("--lambda-artifact-version", required=True)
    parser.add_argument("--lambda-object-evidence", required=True, type=Path)
    parser.add_argument("--deployment-root", type=Path, default=DEFAULT_DEPLOYMENT_ROOT)
    parser.add_argument("--artifact-root", type=Path, default=DEFAULT_ARTIFACT_ROOT)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--write-staged", action="store_true")
    action.add_argument("--verify-staged", action="store_true")
    action.add_argument("--activate", action="store_true")
    arguments = parser.parse_args()
    try:
        if arguments.activate:
            reject_phase6_sam_activation()
        binding = _binding_from_arguments(arguments)
        if arguments.write_staged:
            print(
                write_phase6_sam_staged_template(
                    binding,
                    agentcore_endpoint_observation_path=arguments.agentcore_endpoint_observation,
                    agentcore_object_evidence_path=arguments.agentcore_object_evidence,
                    agentcore_runtime_v1_evidence_path=arguments.agentcore_runtime_v1_evidence,
                    lambda_object_evidence_path=arguments.lambda_object_evidence,
                    deployment_root=arguments.deployment_root,
                    artifact_root=arguments.artifact_root,
                )
            )
        else:
            verify_rendered_phase6_sam_staged_template(
                binding,
                agentcore_endpoint_observation_path=arguments.agentcore_endpoint_observation,
                agentcore_object_evidence_path=arguments.agentcore_object_evidence,
                agentcore_runtime_v1_evidence_path=arguments.agentcore_runtime_v1_evidence,
                lambda_object_evidence_path=arguments.lambda_object_evidence,
                deployment_root=arguments.deployment_root,
                artifact_root=arguments.artifact_root,
            )
            print(STAGED_TEMPLATE_OUTPUT)
    except Phase6SamStagingError as error:
        parser.exit(2, f"{error}\n")


__all__ = [
    "DEFAULT_ARTIFACT_ROOT",
    "DEFAULT_DEPLOYMENT_ROOT",
    "SOURCE_TEMPLATE",
    "STAGED_TEMPLATE_OUTPUT",
    "Phase6SamStagingBinding",
    "Phase6SamStagingError",
    "reject_phase6_sam_activation",
    "render_phase6_sam_staged_template",
    "verify_phase6_sam_staged_inertness",
    "verify_rendered_phase6_sam_staged_template",
    "write_phase6_sam_staged_template",
]


if __name__ == "__main__":
    main()
