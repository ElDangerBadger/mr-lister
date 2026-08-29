"""Assemble closed Phase 6.6 post-hotfix edge revalidation evidence.

The tool consumes two already-sanitized JSON authorities from the repository-private Phase 6.6
workspace and writes one deterministic five-file evidence fragment.  It has no AWS, browser,
HTTP, storage, job, or provider client.  Raw identities, job identifiers, credentials, URLs,
storage coordinates, local paths, and free-form observations have no field in either input model.

Caller aliases are restricted opaque tokens.  They are used only to derive fresh run-scoped
actor, job, and correlation digests and are never retained in emitted evidence.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import stat
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import Annotated, Any, Final, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    StringConstraints,
    model_validator,
)

from mr_lister.acceptance.evidence_set import Phase66ArtifactFile
from mr_lister.acceptance.phase6 import (
    AcceptanceEvidenceClass,
    AcceptanceOutcome,
    ArtifactFormat,
    ArtifactKind,
    phase66_acceptance_manifest,
    phase66_manifest_digest,
    validate_phase66_evidence,
)

REPOSITORY_ROOT: Final = Path(__file__).resolve().parents[1]
PRIVATE_WORKSPACE_ROOT: Final = REPOSITORY_ROOT / ".mr_lister_private" / "phase66-acceptance"

SOURCE_COMMIT: Final = "e130292db7124425840c2768a94475417f94f2e5"
SOURCE_COMMIT_DIGEST: Final = "40e7186ae67d9f6cd7ae630381ff8ed59c09afde0e2022d4b0a3ecbced2277cd"
DEPLOYMENT_AUTHORITY_FORMAT: Final = "phase6.6-sanitized-deployment-authority-v1"
OBSERVATION_FORMAT: Final = "phase6.6-sanitized-edge-auth-owner-observation-v1"
GATE_ID: Final = "deployed.edge_auth_owner_smoke"

DEPLOYMENT_SNAPSHOT_FILENAME: Final = "deployment_snapshot.json"
CANARY_SUMMARY_FILENAME: Final = "canary_summary.json"
LOG_AUDIT_FILENAME: Final = "log_audit.json"
RECORDS_FILENAME: Final = "records.json"
ARTIFACT_FILES_FILENAME: Final = "artifact-files.json"

_OUTPUT_FILENAMES: Final = (
    DEPLOYMENT_SNAPSHOT_FILENAME,
    CANARY_SUMMARY_FILENAME,
    LOG_AUDIT_FILENAME,
    RECORDS_FILENAME,
    ARTIFACT_FILES_FILENAME,
)
_EXPECTED_ASSERTIONS: Final = (
    "health_and_readiness_pass",
    "pkce_and_token_matrix_pass",
    "two_actor_owner_matrix_passes",
    "security_headers_and_cors_pass",
    "strong_review_etag_is_preserved",
    "provider_call_count_is_zero",
)
_LAMBDA_LOGICAL_IDS: Final = (
    "DispatcherFunction",
    "PreparationDispatchFunction",
    "ProviderDraftFunction",
    "ReviewQueryApiFunction",
    "SellerCommandApiFunction",
    "SettlementFunction",
    "SourceVersionRetentionFunction",
    "StuckExecutionRecoveryFunction",
    "TerminalOperationalCleanupFunction",
    "UploadApiFunction",
)
_MAX_INPUT_BYTES = 4 * 1024 * 1024
_UTC_TIMESTAMP = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$")

type Digest = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
type OpaqueAlias = Annotated[
    str,
    StringConstraints(pattern=r"^alias_[0-9a-f]{32,64}$", min_length=38, max_length=70),
]
type CanonicalTimestamp = Annotated[
    str,
    StringConstraints(pattern=r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$"),
]


class Phase66EdgeRevalidationError(RuntimeError):
    """A closed input, confinement, validation, or output operation failed."""


class _ClosedModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class _DeploymentStack(_ClosedModel):
    incomplete_resource_count: Literal[0]
    output_count: StrictInt = Field(ge=1, le=128)
    outputs_digest: Digest
    resource_count: StrictInt = Field(ge=1, le=1_000)
    resource_inventory_digest: Digest
    stack_status: Literal["UPDATE_COMPLETE"]
    tags_digest: Digest
    template_digest: Digest
    termination_protection: StrictBool


class _DeploymentLambda(_ClosedModel):
    code_sha256: Digest
    configuration_digest: Digest
    last_update_status: Literal["Successful"]
    logical_id: Literal[
        "DispatcherFunction",
        "PreparationDispatchFunction",
        "ProviderDraftFunction",
        "ReviewQueryApiFunction",
        "SellerCommandApiFunction",
        "SettlementFunction",
        "SourceVersionRetentionFunction",
        "StuckExecutionRecoveryFunction",
        "TerminalOperationalCleanupFunction",
        "UploadApiFunction",
    ]
    release_fingerprint_digest: Digest
    state: Literal["Active"]


class _DeploymentCognito(_ClosedModel):
    browser_client_configuration_digest: Digest
    browser_client_secret_present: Literal[False]
    confirmed_user_count: StrictInt = Field(ge=2, le=1_000_000)
    enabled_user_count: StrictInt = Field(ge=2, le=1_000_000)
    mfa_configuration: Literal["ON"]
    pool_configuration_digest: Digest
    seller_group_member_count: StrictInt = Field(ge=2, le=1_000_000)
    software_token_mfa_user_count: StrictInt = Field(ge=0, le=1_000_000)
    user_count: StrictInt = Field(ge=2, le=1_000_000)


class _DeploymentWebEdge(_ClosedModel):
    alias_count: StrictInt = Field(ge=1, le=100)
    api_configuration_digest: Digest
    api_protocol: Literal["HTTP"]
    application_body_digest: Digest
    application_status_code: Literal[200]
    cors_headers_digest: Digest
    cors_passed: Literal[True]
    cors_status_code: Literal[200, 204]
    distribution_configuration_digest: Digest
    distribution_enabled: Literal[True]
    distribution_status: Literal["Deployed"]
    health_body_digest: Digest
    health_passed: Literal[True]
    health_status_code: Literal[200]
    origin_count: StrictInt = Field(ge=1, le=100)
    route_count: StrictInt = Field(ge=1, le=1_000)
    security_header_count: StrictInt = Field(ge=7, le=100)
    security_headers_digest: Digest
    security_headers_passed: Literal[True]


class _DeploymentAuthority(_ClosedModel):
    account_binding_digest: Digest
    cognito: _DeploymentCognito
    lambdas: list[_DeploymentLambda] = Field(min_length=10, max_length=10)
    readiness: Literal["WEB_EDGE_ACTIVE_DRAFT_ONLY"]
    region: Literal["us-west-2"]
    source_commit_digest: Literal[SOURCE_COMMIT_DIGEST]
    stack: _DeploymentStack
    stack_name: Literal["mr-lister-phase6-dev"]
    web_edge: _DeploymentWebEdge

    @model_validator(mode="after")
    def lambda_inventory_is_exact(self) -> _DeploymentAuthority:
        logical_ids = tuple(record.logical_id for record in self.lambdas)
        if len(set(logical_ids)) != len(logical_ids) or set(logical_ids) != set(
            _LAMBDA_LOGICAL_IDS
        ):
            raise ValueError("Deployment Lambda inventory is not exact")
        return self


class _DeploymentAuthorityDocument(_ClosedModel):
    authority: _DeploymentAuthority
    captured_at: CanonicalTimestamp
    deployment_digest: Digest
    format: Literal[DEPLOYMENT_AUTHORITY_FORMAT]

    @model_validator(mode="after")
    def digest_binds_authority(self) -> _DeploymentAuthorityDocument:
        if self.deployment_digest != _digest(self.authority.model_dump(mode="json")):
            raise ValueError("Deployment digest does not bind the sanitized authority")
        _parse_timestamp(self.captured_at)
        return self


class _ScopedAuthority(_ClosedModel):
    alias: OpaqueAlias
    authority_digest: Digest


class _ActorAObservation(_ScopedAuthority):
    visible_job_count: Literal[2]
    known_review_ready: Literal[True]
    known_preview_ready: Literal[True]


class _ActorBObservation(_ScopedAuthority):
    visible_job_count: Literal[0]
    actor_a_job_absent: Literal[True]
    unknown_job_absent: Literal[True]


class _EdgeMatrix(_ClosedModel):
    health_passed: Literal[True]
    readiness_passed: Literal[True]
    security_headers_passed: Literal[True]
    cors_passed: Literal[True]
    pkce_authorization_passed: Literal[True]
    pkce_callback_passed: Literal[True]
    token_exchange_passed: Literal[True]
    unauthenticated_access_rejected: Literal[True]


class _ReviewObservation(_ClosedModel):
    access_path: Literal["direct_deployed_review_lambda"]
    invocation_count: Literal[2]
    review_ready: Literal[True]
    preview_ready: Literal[True]
    etag_type: Literal["strong"]
    first_etag_digest: Digest
    second_etag_digest: Digest

    @model_validator(mode="after")
    def strong_etag_is_stable(self) -> _ReviewObservation:
        if self.first_etag_digest != self.second_etag_digest:
            raise ValueError("Review ETag observations do not match")
        return self


class _ZeroDeltas(_ClosedModel):
    provider_call_delta: Literal[0]
    provider_record_delta: Literal[0]
    work_item_delta: Literal[0]
    workflow_execution_delta: Literal[0]


class _EdgeObservation(_ClosedModel):
    format: Literal[OBSERVATION_FORMAT]
    recorded_at: CanonicalTimestamp
    run: _ScopedAuthority
    actor_a: _ActorAObservation
    actor_b: _ActorBObservation
    known_job: _ScopedAuthority
    correlation: _ScopedAuthority
    matrix: _EdgeMatrix
    review: _ReviewObservation
    deltas: _ZeroDeltas

    @model_validator(mode="after")
    def scoped_authorities_are_distinct(self) -> _EdgeObservation:
        authorities = (self.run, self.actor_a, self.actor_b, self.known_job, self.correlation)
        aliases = tuple(item.alias for item in authorities)
        digests = tuple(item.authority_digest for item in authorities)
        if len(set(aliases)) != len(aliases) or len(set(digests)) != len(digests):
            raise ValueError("Run-scoped authorities must be distinct")
        _parse_timestamp(self.recorded_at)
        return self


def _canonical(value: object) -> bytes:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _render(value: object) -> bytes:
    return _canonical(value) + b"\n"


def _digest(value: object) -> str:
    return sha256(_canonical(value)).hexdigest()


def _parse_timestamp(value: str) -> datetime:
    if _UTC_TIMESTAMP.fullmatch(value) is None:
        raise ValueError("Timestamp is not canonical UTC text")
    parsed = datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    return parsed


def _reject_json_constant(_value: str) -> None:
    raise ValueError("Non-finite JSON constant")


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, nested in pairs:
        if key in value:
            raise ValueError("Duplicate JSON member")
        value[key] = nested
    return value


def _confined(path: Path) -> Path:
    candidate = Path(os.path.abspath(path))
    try:
        relative = candidate.relative_to(PRIVATE_WORKSPACE_ROOT)
    except ValueError:
        raise Phase66EdgeRevalidationError(
            "Phase 6.6 revalidation paths must stay in the repository-private workspace"
        ) from None
    if not relative.parts:
        raise Phase66EdgeRevalidationError("A run or input path must name a private child")
    return candidate


def _validate_private_parents(path: Path) -> None:
    candidate = _confined(path)
    current = REPOSITORY_ROOT
    for component in candidate.parent.relative_to(REPOSITORY_ROOT).parts:
        current /= component
        try:
            metadata = current.lstat()
        except OSError:
            raise Phase66EdgeRevalidationError("A private input parent is unavailable") from None
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or stat.S_ISLNK(metadata.st_mode)
            or metadata.st_mode & 0o077 != 0
        ):
            raise Phase66EdgeRevalidationError("A private input parent is not confined")


def _read_private_json(path: Path) -> object:
    candidate = _confined(path)
    _validate_private_parents(candidate)
    descriptor: int | None = None
    try:
        descriptor = os.open(candidate, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or before.st_mode & 0o077 != 0
            or not 1 <= before.st_size <= _MAX_INPUT_BYTES
        ):
            raise OSError
        chunks: list[bytes] = []
        remaining = before.st_size
        while remaining:
            chunk = os.read(descriptor, min(1024 * 1024, remaining))
            if not chunk:
                raise OSError
            chunks.append(chunk)
            remaining -= len(chunk)
        after = os.fstat(descriptor)
    except OSError:
        raise Phase66EdgeRevalidationError(
            "A revalidation input must be one stable owner-only regular file"
        ) from None
    finally:
        if descriptor is not None:
            os.close(descriptor)
    if (
        before.st_dev,
        before.st_ino,
        before.st_size,
        before.st_mtime_ns,
        before.st_ctime_ns,
    ) != (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
        after.st_ctime_ns,
    ):
        raise Phase66EdgeRevalidationError("A revalidation input changed during its read")
    try:
        return json.loads(
            b"".join(chunks),
            object_pairs_hook=_unique_object,
            parse_constant=_reject_json_constant,
        )
    except (RecursionError, UnicodeDecodeError, ValueError, json.JSONDecodeError):
        raise Phase66EdgeRevalidationError("A revalidation input must be strict JSON") from None


def _ensure_private_run_root(run_root: Path) -> Path:
    candidate = _confined(run_root)
    current = REPOSITORY_ROOT
    for component in candidate.relative_to(REPOSITORY_ROOT).parts:
        current /= component
        try:
            metadata = current.lstat()
        except FileNotFoundError:
            try:
                current.mkdir(mode=0o700)
            except OSError:
                raise Phase66EdgeRevalidationError(
                    "The private revalidation run root could not be created"
                ) from None
            metadata = current.lstat()
        if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
            raise Phase66EdgeRevalidationError("The private run root is not confined")
        try:
            current.chmod(0o700)
        except OSError:
            raise Phase66EdgeRevalidationError(
                "The private revalidation run root could not be secured"
            ) from None
    return candidate


def _read_existing_output(path: Path) -> bytes:
    descriptor: int | None = None
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or metadata.st_mode & 0o077 != 0
            or metadata.st_size > _MAX_INPUT_BYTES
        ):
            raise OSError
        chunks: list[bytes] = []
        remaining = metadata.st_size
        while remaining:
            chunk = os.read(descriptor, min(1024 * 1024, remaining))
            if not chunk:
                raise OSError
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)
    except OSError:
        raise Phase66EdgeRevalidationError("An existing evidence output is not private") from None
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _write_once(path: Path, contents: bytes) -> None:
    if path.exists() or path.is_symlink():
        if _read_existing_output(path) != contents:
            raise Phase66EdgeRevalidationError(
                "Existing evidence output differs from the closed revalidation"
            )
        return
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(12)}.tmp")
    descriptor: int | None = None
    try:
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
            0o600,
        )
        with os.fdopen(descriptor, "wb", closefd=True) as stream:
            descriptor = None
            stream.write(contents)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, path, follow_symlinks=False)
        temporary.unlink()
        path.chmod(0o600)
    except FileExistsError:
        if _read_existing_output(path) != contents:
            raise Phase66EdgeRevalidationError(
                "Concurrent evidence output differs from the closed revalidation"
            ) from None
    except OSError:
        raise Phase66EdgeRevalidationError(
            "A private revalidation evidence file could not be written"
        ) from None
    finally:
        if descriptor is not None:
            os.close(descriptor)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _derived_digest(kind: str, run_digest: str, authority: _ScopedAuthority) -> str:
    payload = b"\0".join(
        (
            b"phase6.6-edge-revalidation-scoped-digest-v1",
            kind.encode("ascii"),
            run_digest.encode("ascii"),
            authority.alias.encode("ascii"),
            authority.authority_digest.encode("ascii"),
        )
    )
    return sha256(payload).hexdigest()


def _assertion_observation_digest(
    assertion_id: str,
    run_digest: str,
    value: object,
) -> str:
    return _digest(
        {
            "assertion_id": assertion_id,
            "run_digest": run_digest,
            "value": value,
        }
    )


def _gate_authority() -> Any:
    matches = tuple(gate for gate in phase66_acceptance_manifest().gates if gate.gate_id == GATE_ID)
    if len(matches) != 1 or matches[0].required_assertions != _EXPECTED_ASSERTIONS:
        raise Phase66EdgeRevalidationError("The frozen edge revalidation gate has drifted")
    if matches[0].required_artifact_kinds != (
        ArtifactKind.DEPLOYMENT_SNAPSHOT,
        ArtifactKind.CANARY_SUMMARY,
        ArtifactKind.LOG_AUDIT,
    ):
        raise Phase66EdgeRevalidationError("The frozen edge artifact contract has drifted")
    return matches[0]


def _validate_source_authority(source_commit: str, source_commit_digest: str) -> None:
    if (
        source_commit != SOURCE_COMMIT
        or source_commit_digest != SOURCE_COMMIT_DIGEST
        or sha256(source_commit.encode("ascii")).hexdigest() != source_commit_digest
    ):
        raise Phase66EdgeRevalidationError("The exact Phase 6 source authority is required")


def _validated_inputs(
    deployment_value: object,
    observation_value: object,
) -> tuple[_DeploymentAuthorityDocument, _EdgeObservation]:
    try:
        deployment = _DeploymentAuthorityDocument.model_validate(deployment_value)
        observation = _EdgeObservation.model_validate(observation_value)
    except ValueError:
        raise Phase66EdgeRevalidationError(
            "Revalidation authorities do not match the closed sanitized contracts"
        ) from None
    if _parse_timestamp(observation.recorded_at) < _parse_timestamp(deployment.captured_at):
        raise Phase66EdgeRevalidationError(
            "The revalidation observation predates deployment authority"
        )
    return deployment, observation


def _build_outputs(
    deployment: _DeploymentAuthorityDocument,
    observation: _EdgeObservation,
) -> tuple[dict[str, bytes], dict[str, object]]:
    gate = _gate_authority()
    manifest_digest = phase66_manifest_digest()
    deployment_authority_digest = _digest(deployment.model_dump(mode="json"))
    observation_digest = _digest(observation.model_dump(mode="json"))
    run_digest = _digest(
        {
            "contract": "phase6.6-edge-revalidation-run-v1",
            "deployment_authority_digest": deployment_authority_digest,
            "deployment_digest": deployment.deployment_digest,
            "observation_digest": observation_digest,
            "run_alias": observation.run.alias,
            "run_authority_digest": observation.run.authority_digest,
            "source_commit_digest": SOURCE_COMMIT_DIGEST,
        }
    )
    actor_a_digest = _derived_digest("actor", run_digest, observation.actor_a)
    actor_b_digest = _derived_digest("actor", run_digest, observation.actor_b)
    job_digest = _derived_digest("job", run_digest, observation.known_job)
    correlation_digest = _derived_digest("correlation", run_digest, observation.correlation)
    review_lambda = next(
        record
        for record in deployment.authority.lambdas
        if record.logical_id == "ReviewQueryApiFunction"
    )

    common = {
        "gate": GATE_ID,
        "manifest_digest": manifest_digest,
        "result": "passed",
        "run_digest": run_digest,
        "deployment_digest": deployment.deployment_digest,
    }
    deployment_snapshot = {
        **common,
        "artifact_contract": "phase6.6-sanitized-deployment-snapshot-v1",
        "captured_at": deployment.captured_at,
        "source_commit_digest": SOURCE_COMMIT_DIGEST,
        "deployment_authority_digest": deployment_authority_digest,
        "stack_status": deployment.authority.stack.stack_status.casefold(),
        "readiness": deployment.authority.readiness.casefold(),
        "resource_count": deployment.authority.stack.resource_count,
        "incomplete_resource_count": deployment.authority.stack.incomplete_resource_count,
        "lambda_count": len(deployment.authority.lambdas),
        "review_code_digest": review_lambda.code_sha256,
        "review_configuration_digest": review_lambda.configuration_digest,
        "review_release_fingerprint_digest": review_lambda.release_fingerprint_digest,
        "review_state": review_lambda.state.casefold(),
        "health_status_code": deployment.authority.web_edge.health_status_code,
        "health_passed": deployment.authority.web_edge.health_passed,
        "security_headers_passed": deployment.authority.web_edge.security_headers_passed,
        "cors_passed": deployment.authority.web_edge.cors_passed,
        "pkce_public_client": not deployment.authority.cognito.browser_client_secret_present,
        "mfa_mode": deployment.authority.cognito.mfa_configuration.casefold(),
    }
    canary_summary = {
        **common,
        "artifact_contract": "phase6.6-sanitized-edge-revalidation-canary-v1",
        "recorded_at": observation.recorded_at,
        "actor_digest_scheme": "run_scoped_alias_authority_sha256_v1",
        "actor_digests": [actor_a_digest, actor_b_digest],
        "job_digest": job_digest,
        "correlation_digest": correlation_digest,
        "actor_a_visible_job_count": observation.actor_a.visible_job_count,
        "actor_a_known_review_ready": observation.actor_a.known_review_ready,
        "actor_a_known_preview_ready": observation.actor_a.known_preview_ready,
        "actor_b_visible_job_count": observation.actor_b.visible_job_count,
        "actor_b_actor_a_job_absent": observation.actor_b.actor_a_job_absent,
        "actor_b_unknown_job_absent": observation.actor_b.unknown_job_absent,
        "health_passed": observation.matrix.health_passed,
        "readiness_passed": observation.matrix.readiness_passed,
        "security_headers_passed": observation.matrix.security_headers_passed,
        "cors_passed": observation.matrix.cors_passed,
        "pkce_authorization_passed": observation.matrix.pkce_authorization_passed,
        "pkce_callback_passed": observation.matrix.pkce_callback_passed,
        "token_exchange_passed": observation.matrix.token_exchange_passed,
        "unauthenticated_access_rejected": (observation.matrix.unauthenticated_access_rejected),
        "review_access_path": observation.review.access_path,
        "review_invocation_count": observation.review.invocation_count,
        "review_ready": observation.review.review_ready,
        "preview_ready": observation.review.preview_ready,
        "strong_review_etag_digest": observation.review.first_etag_digest,
        "strong_review_etag_preserved": True,
        **observation.deltas.model_dump(mode="json"),
    }
    log_audit = {
        **common,
        "artifact_contract": "phase6.6-sanitized-edge-revalidation-log-audit-v1",
        "recorded_at": observation.recorded_at,
        "deployment_authority_digest": deployment_authority_digest,
        "observation_digest": observation_digest,
        "direct_review_invocation_count": observation.review.invocation_count,
        **observation.deltas.model_dump(mode="json"),
        "forbidden_field_match_count": 0,
        "sensitive_value_match_count": 0,
        "free_text_value_count": 0,
    }

    artifact_documents: tuple[tuple[ArtifactKind, str, dict[str, object]], ...] = (
        (ArtifactKind.DEPLOYMENT_SNAPSHOT, DEPLOYMENT_SNAPSHOT_FILENAME, deployment_snapshot),
        (ArtifactKind.CANARY_SUMMARY, CANARY_SUMMARY_FILENAME, canary_summary),
        (ArtifactKind.LOG_AUDIT, LOG_AUDIT_FILENAME, log_audit),
    )
    outputs: dict[str, bytes] = {}
    artifact_evidence: list[dict[str, object]] = []
    artifact_files: list[dict[str, object]] = []
    for kind, filename, document in artifact_documents:
        contents = _render(document)
        artifact_digest = sha256(contents).hexdigest()
        outputs[filename] = contents
        evidence = {
            "kind": kind.value,
            "artifact_format": ArtifactFormat.JSON.value,
            "artifact_digest": artifact_digest,
            "byte_count": len(contents),
            "redaction_verified": True,
        }
        artifact_evidence.append(evidence)
        artifact_file = Phase66ArtifactFile.model_validate(
            {
                "artifact_digest": artifact_digest,
                "kind": kind,
                "artifact_format": ArtifactFormat.JSON,
                "relative_path": filename,
            }
        )
        artifact_files.append(artifact_file.model_dump(mode="json"))

    assertion_values: Mapping[str, tuple[object, int]] = {
        "health_and_readiness_pass": (
            {
                "health": observation.matrix.health_passed,
                "readiness": observation.matrix.readiness_passed,
            },
            2,
        ),
        "pkce_and_token_matrix_pass": (
            {
                "authorization": observation.matrix.pkce_authorization_passed,
                "callback": observation.matrix.pkce_callback_passed,
                "exchange": observation.matrix.token_exchange_passed,
                "unauthenticated_rejected": observation.matrix.unauthenticated_access_rejected,
            },
            4,
        ),
        "two_actor_owner_matrix_passes": (
            {
                "actor_a_digest": actor_a_digest,
                "actor_a_visible_job_count": observation.actor_a.visible_job_count,
                "actor_b_digest": actor_b_digest,
                "actor_b_visible_job_count": observation.actor_b.visible_job_count,
                "foreign_absent": observation.actor_b.actor_a_job_absent,
                "unknown_absent": observation.actor_b.unknown_job_absent,
                "job_digest": job_digest,
            },
            2,
        ),
        "security_headers_and_cors_pass": (
            {
                "cors": observation.matrix.cors_passed,
                "security_headers": observation.matrix.security_headers_passed,
            },
            2,
        ),
        "strong_review_etag_is_preserved": (
            {
                "access_path": observation.review.access_path,
                "etag_digest": observation.review.first_etag_digest,
                "invocation_count": observation.review.invocation_count,
            },
            2,
        ),
        "provider_call_count_is_zero": (
            observation.deltas.model_dump(mode="json"),
            0,
        ),
    }
    assertions = []
    for assertion_id in gate.required_assertions:
        value, count = assertion_values[assertion_id]
        assertions.append(
            {
                "assertion_id": assertion_id,
                "passed": True,
                "observation_digest": _assertion_observation_digest(
                    assertion_id,
                    run_digest,
                    value,
                ),
                "observed_count": count,
            }
        )
    record = validate_phase66_evidence(
        {
            "schema_version": "6.6.0",
            "manifest_digest": manifest_digest,
            "run_digest": run_digest,
            "source_commit_digest": SOURCE_COMMIT_DIGEST,
            "gate_id": GATE_ID,
            "evidence_class": AcceptanceEvidenceClass.DEPLOYED_NON_DESTRUCTIVE.value,
            "outcome": AcceptanceOutcome.PASSED.value,
            "recorded_at": observation.recorded_at,
            "job_digest": job_digest,
            "work_digest": None,
            "correlation_digest": correlation_digest,
            "assertions": assertions,
            "artifacts": artifact_evidence,
            "privacy": {
                "sanitizer_contract": "phase6.6-sanitized-evidence-v1",
                "forbidden_field_match_count": 0,
                "sensitive_value_match_count": 0,
                "free_text_value_count": 0,
            },
            "deployment_digest": deployment.deployment_digest,
            "actor_digests": [actor_a_digest, actor_b_digest],
            "provider_gate_attestation": None,
            "provider_call_summary": None,
            "moderated_session": None,
        }
    )
    record_value = record.model_dump(mode="json")
    outputs[RECORDS_FILENAME] = _render([record_value])
    outputs[ARTIFACT_FILES_FILENAME] = _render(artifact_files)
    if set(outputs) != set(_OUTPUT_FILENAMES):
        raise Phase66EdgeRevalidationError("The closed evidence output set drifted")
    return outputs, {
        "artifact_count": len(artifact_documents),
        "deployment_digest": deployment.deployment_digest,
        "record_digest": _digest(record_value),
        "result": "passed",
        "run_digest": run_digest,
    }


def prepare_phase66_edge_revalidation(
    *,
    run_root: Path,
    source_commit: str,
    source_commit_digest: str,
    deployment_authority_path: Path,
    observation_path: Path,
) -> dict[str, object]:
    """Validate sanitized authorities and atomically create the closed evidence fragment."""

    _validate_source_authority(source_commit, source_commit_digest)
    output_root = _confined(run_root)
    deployment_path = _confined(deployment_authority_path)
    observation_input = _confined(observation_path)
    output_paths = {output_root / filename for filename in _OUTPUT_FILENAMES}
    if deployment_path in output_paths or observation_input in output_paths:
        raise Phase66EdgeRevalidationError("Evidence outputs cannot replace input authorities")
    deployment, observation = _validated_inputs(
        _read_private_json(deployment_path),
        _read_private_json(observation_input),
    )
    outputs, summary = _build_outputs(deployment, observation)
    aliases = (
        observation.run.alias,
        observation.actor_a.alias,
        observation.actor_b.alias,
        observation.known_job.alias,
        observation.correlation.alias,
    )
    for contents in outputs.values():
        if any(alias.encode("ascii") in contents for alias in aliases):
            raise Phase66EdgeRevalidationError("An opaque input alias reached evidence output")
    output_root = _ensure_private_run_root(output_root)
    for filename, contents in outputs.items():
        target = output_root / filename
        if target.exists() or target.is_symlink():
            if _read_existing_output(target) != contents:
                raise Phase66EdgeRevalidationError(
                    "Existing evidence output differs from the closed revalidation"
                )
    for filename in _OUTPUT_FILENAMES:
        _write_once(output_root / filename, outputs[filename])
    return summary


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-root", required=True, type=Path)
    parser.add_argument("--source-commit", required=True)
    parser.add_argument("--source-commit-digest", required=True)
    parser.add_argument("--deployment-authority", required=True, type=Path)
    parser.add_argument("--observation", required=True, type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        summary = prepare_phase66_edge_revalidation(
            run_root=arguments.run_root,
            source_commit=arguments.source_commit,
            source_commit_digest=arguments.source_commit_digest,
            deployment_authority_path=arguments.deployment_authority,
            observation_path=arguments.observation,
        )
    except Phase66EdgeRevalidationError as error:
        _parser().error(str(error))
    print(_render(summary).decode("utf-8"), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
