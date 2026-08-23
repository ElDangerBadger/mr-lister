"""Frozen Phase 6.6 acceptance manifest and sanitized evidence schema.

Evidence records deliberately contain only closed enums, bounded counters, timestamps, and
SHA-256 digests. Raw seller identity, credentials, storage authority, provider payloads, and
free-form observations have no representation in this contract.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from datetime import UTC, datetime
from enum import StrEnum
from hashlib import sha256
from typing import Annotated, Any, Literal

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    StringConstraints,
    TypeAdapter,
    field_validator,
    model_validator,
)

PHASE66_CONTRACT_VERSION = "6.6.0"
PHASE66_SCHEMA_ID = "https://mr-lister.invalid/contracts/acceptance/phase6.6/evidence.schema.json"
_RFC3339 = re.compile(
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}"
    r"(?:\.[0-9]{1,6})?(?:Z|[+-][0-9]{2}:[0-9]{2})$"
)
_UTC_RFC3339 = re.compile(
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,6})?Z$"
)

type Digest = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
type GateId = Annotated[
    str,
    StringConstraints(pattern=r"^(offline|deployed|provider|moderated)\.[a-z][a-z0-9_]{2,63}$"),
]
type AssertionId = Annotated[
    str,
    StringConstraints(pattern=r"^[a-z][a-z0-9_]{2,95}$"),
]


class AcceptanceEvidenceClass(StrEnum):
    OFFLINE = "offline"
    DEPLOYED_NON_DESTRUCTIVE = "deployed_non_destructive"
    PROVIDER_DESTRUCTIVE = "provider_destructive"
    MODERATED_USER = "moderated_user"


class ProviderMutationPolicy(StrEnum):
    FORBIDDEN = "forbidden"
    DOUBLE_GATED = "double_gated"
    SEPARATE_PROVIDER_EVIDENCE = "separate_provider_evidence"


class AcceptanceOutcome(StrEnum):
    PASSED = "passed"
    FAILED = "failed"
    INCONCLUSIVE = "inconclusive"


class ArtifactKind(StrEnum):
    TEST_REPORT = "test_report"
    BROWSER_TRACE = "browser_trace"
    SCREENSHOT = "screenshot"
    DEPLOYMENT_SNAPSHOT = "deployment_snapshot"
    LOG_AUDIT = "log_audit"
    PROVIDER_CALL_LEDGER = "provider_call_ledger"
    CANARY_SUMMARY = "canary_summary"
    MODERATED_SESSION_RECORD = "moderated_session_record"


class ArtifactFormat(StrEnum):
    JSON = "json"
    JUNIT_XML = "junit_xml"
    ZIP = "zip"
    PNG = "png"
    WEBM = "webm"


class ProviderFinalState(StrEnum):
    UNPUBLISHED_UNLOCKED = "unpublished_unlocked"
    NOT_CREATED = "not_created"
    UNRESOLVED = "unresolved"


class _ClosedModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class AcceptanceGate(_ClosedModel):
    gate_id: GateId
    title: str = Field(min_length=1, max_length=120)
    evidence_class: AcceptanceEvidenceClass
    provider_mutation_policy: ProviderMutationPolicy
    required_assertions: tuple[AssertionId, ...] = Field(min_length=1, max_length=24)
    required_artifact_kinds: tuple[ArtifactKind, ...] = Field(min_length=1, max_length=8)
    prerequisites: tuple[GateId, ...] = Field(default=(), max_length=12)
    minimum_evidence_records: StrictInt = Field(default=1, ge=1, le=10)
    blocking_phase6_exit: StrictBool = True
    double_gate_labels: tuple[Literal["run_gate", "provider_write_gate"], ...] = ()

    @model_validator(mode="after")
    def validate_gate_class(self) -> AcceptanceGate:
        if len(set(self.required_assertions)) != len(self.required_assertions):
            raise ValueError("Acceptance assertion identifiers must be unique")
        if len(set(self.required_artifact_kinds)) != len(self.required_artifact_kinds):
            raise ValueError("Required acceptance artifact kinds must be unique")
        if len(set(self.prerequisites)) != len(self.prerequisites):
            raise ValueError("Acceptance prerequisites must be unique")
        is_provider_gate = self.evidence_class is AcceptanceEvidenceClass.PROVIDER_DESTRUCTIVE
        if is_provider_gate != (
            self.provider_mutation_policy is ProviderMutationPolicy.DOUBLE_GATED
        ):
            raise ValueError("Provider-destructive evidence must be double gated")
        if is_provider_gate:
            if self.double_gate_labels != ("run_gate", "provider_write_gate"):
                raise ValueError("Provider-destructive evidence requires both frozen gates")
        elif self.double_gate_labels:
            raise ValueError("Only provider-destructive evidence may declare write gates")
        if self.evidence_class is AcceptanceEvidenceClass.MODERATED_USER:
            if (
                self.provider_mutation_policy
                is not ProviderMutationPolicy.SEPARATE_PROVIDER_EVIDENCE
            ):
                raise ValueError(
                    "Moderated evidence must keep provider writes in a separate record"
                )
        elif self.minimum_evidence_records != 1:
            raise ValueError("Only moderated-user gates may require multiple evidence records")
        return self


class Phase66AcceptanceManifest(_ClosedModel):
    contract_version: Literal["6.6.0"]
    phase: Literal["6.6"]
    status: Literal["frozen"]
    frozen_at: AwareDatetime
    digest_algorithm: Literal["sha256"]
    gates: tuple[AcceptanceGate, ...] = Field(min_length=1, max_length=32)
    phase6_exit_gate_ids: tuple[GateId, ...] = Field(min_length=1, max_length=32)
    forbidden_evidence_field_names: tuple[str, ...] = Field(min_length=1, max_length=64)

    @field_validator("frozen_at", mode="before")
    @classmethod
    def freeze_timestamp_is_rfc3339_text_or_datetime(cls, value: object) -> object:
        if isinstance(value, datetime):
            return value
        if type(value) is not str or _RFC3339.fullmatch(value) is None:
            raise ValueError("Manifest freeze timestamp must be RFC3339 text")
        return value

    @model_validator(mode="after")
    def validate_manifest(self) -> Phase66AcceptanceManifest:
        if self.frozen_at.utcoffset() != UTC.utcoffset(self.frozen_at):
            raise ValueError("The manifest freeze timestamp must use UTC")
        gate_ids = tuple(gate.gate_id for gate in self.gates)
        if len(set(gate_ids)) != len(gate_ids):
            raise ValueError("Acceptance gate identifiers must be unique")
        gate_id_set = set(gate_ids)
        for gate in self.gates:
            if gate.gate_id in gate.prerequisites:
                raise ValueError("An acceptance gate cannot depend on itself")
            if not set(gate.prerequisites).issubset(gate_id_set):
                raise ValueError("Every acceptance prerequisite must name a frozen gate")
        expected_exit = tuple(gate.gate_id for gate in self.gates if gate.blocking_phase6_exit)
        if self.phase6_exit_gate_ids != expected_exit:
            raise ValueError("Phase 6 exit gates must exactly match the blocking manifest gates")
        if len(set(self.forbidden_evidence_field_names)) != len(
            self.forbidden_evidence_field_names
        ):
            raise ValueError("Forbidden evidence field names must be unique")

        visiting: set[str] = set()
        visited: set[str] = set()
        prerequisites = {gate.gate_id: gate.prerequisites for gate in self.gates}

        def visit(gate_id: str) -> None:
            if gate_id in visiting:
                raise ValueError("Acceptance prerequisites must be acyclic")
            if gate_id in visited:
                return
            visiting.add(gate_id)
            for prerequisite in prerequisites[gate_id]:
                visit(prerequisite)
            visiting.remove(gate_id)
            visited.add(gate_id)

        for gate_id in gate_ids:
            visit(gate_id)
        return self


_FORBIDDEN_EVIDENCE_FIELD_NAMES = (
    "owner",
    "owner_id",
    "raw_owner",
    "sub",
    "cognito_sub",
    "email",
    "username",
    "access_token",
    "refresh_token",
    "id_token",
    "authorization",
    "authorization_header",
    "cookie",
    "secret",
    "secret_arn",
    "printify_token",
    "presigned_url",
    "upload_url",
    "object_key",
    "bucket_name",
    "provider_payload",
    "provider_response",
    "request_body",
    "response_body",
    "raw_payload",
)


def _gate(
    gate_id: str,
    title: str,
    evidence_class: AcceptanceEvidenceClass,
    assertions: tuple[str, ...],
    *,
    artifacts: tuple[ArtifactKind, ...] = (ArtifactKind.TEST_REPORT,),
    prerequisites: tuple[str, ...] = (),
    blocking: bool = True,
    minimum_records: int = 1,
) -> AcceptanceGate:
    if evidence_class is AcceptanceEvidenceClass.PROVIDER_DESTRUCTIVE:
        policy = ProviderMutationPolicy.DOUBLE_GATED
        double_gates: tuple[Literal["run_gate", "provider_write_gate"], ...] = (
            "run_gate",
            "provider_write_gate",
        )
    elif evidence_class is AcceptanceEvidenceClass.MODERATED_USER:
        policy = ProviderMutationPolicy.SEPARATE_PROVIDER_EVIDENCE
        double_gates = ()
    else:
        policy = ProviderMutationPolicy.FORBIDDEN
        double_gates = ()
    return AcceptanceGate(
        gate_id=gate_id,
        title=title,
        evidence_class=evidence_class,
        provider_mutation_policy=policy,
        required_assertions=assertions,
        required_artifact_kinds=artifacts,
        prerequisites=prerequisites,
        minimum_evidence_records=minimum_records,
        blocking_phase6_exit=blocking,
        double_gate_labels=double_gates,
    )


_GATES = (
    _gate(
        "offline.replay_matrix",
        "Exact replay and lost-response matrix",
        AcceptanceEvidenceClass.OFFLINE,
        (
            "nine_mutation_routes_replay_exactly",
            "changed_requests_conflict",
            "one_job_graph_is_created",
            "one_logical_work_graph_is_created",
            "provider_transport_is_not_invoked",
        ),
    ),
    _gate(
        "offline.concurrency_matrix",
        "Upload, command, dispatcher, and worker concurrency matrix",
        AcceptanceEvidenceClass.OFFLINE,
        (
            "revise_approve_cancel_have_one_winner",
            "identical_requests_share_one_receipt",
            "changed_requests_have_one_conflict",
            "upload_completion_and_cancel_have_one_winner",
            "dispatcher_and_worker_races_settle_once",
            "provider_transport_is_not_invoked",
        ),
        prerequisites=("offline.replay_matrix",),
    ),
    _gate(
        "offline.cross_owner_matrix",
        "All protected routes preserve owner indistinguishability",
        AcceptanceEvidenceClass.OFFLINE,
        (
            "fourteen_protected_routes_are_covered",
            "foreign_resources_match_absence",
            "foreign_job_list_is_empty",
            "foreign_commands_write_nothing",
            "identity_injection_is_rejected",
            "provider_transport_is_not_invoked",
        ),
    ),
    _gate(
        "offline.browser_matrix",
        "Exact production bundle passes the three-engine browser matrix",
        AcceptanceEvidenceClass.OFFLINE,
        (
            "chromium_flow_passes",
            "firefox_flow_passes",
            "webkit_flow_passes",
            "accessibility_matrix_passes",
            "browser_restart_and_tab_recovery_pass",
            "commerce_surface_is_absent",
            "provider_transport_is_not_invoked",
        ),
        artifacts=(ArtifactKind.TEST_REPORT, ArtifactKind.BROWSER_TRACE),
        prerequisites=(
            "offline.replay_matrix",
            "offline.concurrency_matrix",
            "offline.cross_owner_matrix",
        ),
    ),
    _gate(
        "deployed.edge_auth_owner_smoke",
        "Deployed edge, authentication, headers, and two-owner smoke",
        AcceptanceEvidenceClass.DEPLOYED_NON_DESTRUCTIVE,
        (
            "health_and_readiness_pass",
            "pkce_and_token_matrix_pass",
            "two_actor_owner_matrix_passes",
            "security_headers_and_cors_pass",
            "strong_review_etag_is_preserved",
            "provider_call_count_is_zero",
        ),
        artifacts=(
            ArtifactKind.DEPLOYMENT_SNAPSHOT,
            ArtifactKind.CANARY_SUMMARY,
            ArtifactKind.LOG_AUDIT,
        ),
        prerequisites=("offline.browser_matrix",),
    ),
    _gate(
        "deployed.upload_integrity_smoke",
        "Deployed upload expiry, integrity, and exact-version preview smoke",
        AcceptanceEvidenceClass.DEPLOYED_NON_DESTRUCTIVE,
        (
            "expired_upload_grant_is_rejected",
            "modified_upload_grant_is_rejected",
            "wrong_artwork_bytes_are_rejected",
            "preview_binds_exact_version",
            "post_finalize_overwrite_cannot_change_preview",
            "provider_call_count_is_zero",
        ),
        artifacts=(ArtifactKind.CANARY_SUMMARY, ArtifactKind.LOG_AUDIT),
        prerequisites=("deployed.edge_auth_owner_smoke",),
    ),
    _gate(
        "deployed.outbox_recovery_smoke",
        "Deployed outbox dispatch and sweep recovery smoke",
        AcceptanceEvidenceClass.DEPLOYED_NON_DESTRUCTIVE,
        (
            "committed_work_is_recovered_by_sweep",
            "deterministic_execution_starts_once",
            "logical_work_is_not_duplicated",
            "stuck_execution_recovery_passes",
            "reference_aware_retention_sweep_passes",
            "privacy_scan_passes",
            "provider_call_count_is_zero",
        ),
        artifacts=(ArtifactKind.CANARY_SUMMARY, ArtifactKind.LOG_AUDIT),
        prerequisites=("deployed.edge_auth_owner_smoke",),
    ),
    _gate(
        "provider.primary_same_job_canary",
        "One same-job Strands and unpublished-product canary",
        AcceptanceEvidenceClass.PROVIDER_DESTRUCTIVE,
        (
            "five_mib_upload_reaches_review",
            "same_job_strands_correlation_is_joined",
            "gemma_intelligence_under_strands_is_recorded",
            "one_product_post_is_recorded",
            "two_same_product_puts_are_recorded",
            "approval_ends_at_approved",
            "final_product_is_unpublished_unlocked",
            "forbidden_provider_attempt_count_is_zero",
        ),
        artifacts=(
            ArtifactKind.PROVIDER_CALL_LEDGER,
            ArtifactKind.CANARY_SUMMARY,
            ArtifactKind.LOG_AUDIT,
        ),
        prerequisites=(
            "deployed.upload_integrity_smoke",
            "deployed.outbox_recovery_smoke",
        ),
    ),
    _gate(
        "provider.concurrency_canary",
        "Deployed revise, approve, and cancel concurrency canary",
        AcceptanceEvidenceClass.PROVIDER_DESTRUCTIVE,
        (
            "three_commands_have_one_winner",
            "one_decision_or_work_effect_is_recorded",
            "at_most_one_product_put_is_recorded",
            "forbidden_provider_attempt_count_is_zero",
        ),
        artifacts=(ArtifactKind.PROVIDER_CALL_LEDGER, ArtifactKind.CANARY_SUMMARY),
        prerequisites=("provider.primary_same_job_canary",),
    ),
    _gate(
        "provider.cancellation_canary",
        "Seller cancellation without administrator authority",
        AcceptanceEvidenceClass.PROVIDER_DESTRUCTIVE,
        (
            "seller_cancel_reaches_cancelled",
            "root_credentials_are_rejected",
            "no_phase6_execution_remains_running",
            "forbidden_provider_attempt_count_is_zero",
        ),
        artifacts=(ArtifactKind.PROVIDER_CALL_LEDGER, ArtifactKind.CANARY_SUMMARY),
        prerequisites=("deployed.edge_auth_owner_smoke",),
    ),
    _gate(
        "moderated.first_time_seller_exit",
        "First-time seller completes the deployed flow without documentation",
        AcceptanceEvidenceClass.MODERATED_USER,
        (
            "invite_and_mfa_complete",
            "supported_upload_completes",
            "browser_restart_recovers_job",
            "unpublished_boundary_is_understood",
            "strands_evidence_is_found",
            "human_decision_completes_without_intervention",
        ),
        artifacts=(ArtifactKind.MODERATED_SESSION_RECORD,),
        prerequisites=(
            "deployed.edge_auth_owner_smoke",
            "provider.primary_same_job_canary",
        ),
    ),
    _gate(
        "moderated.five_session_target",
        "Five moderated first-time-seller attempts",
        AcceptanceEvidenceClass.MODERATED_USER,
        (
            "moderated_attempt_is_recorded",
            "external_documentation_is_not_used",
            "privacy_scan_passes",
        ),
        artifacts=(ArtifactKind.MODERATED_SESSION_RECORD,),
        prerequisites=("moderated.first_time_seller_exit",),
        blocking=False,
        minimum_records=5,
    ),
)

_PHASE66_MANIFEST = Phase66AcceptanceManifest(
    contract_version=PHASE66_CONTRACT_VERSION,
    phase="6.6",
    status="frozen",
    frozen_at=datetime(2026, 8, 22, 20, 0, tzinfo=UTC),
    digest_algorithm="sha256",
    gates=_GATES,
    phase6_exit_gate_ids=tuple(gate.gate_id for gate in _GATES if gate.blocking_phase6_exit),
    forbidden_evidence_field_names=_FORBIDDEN_EVIDENCE_FIELD_NAMES,
)
_GATE_INDEX = {gate.gate_id: gate for gate in _PHASE66_MANIFEST.gates}


def phase66_acceptance_manifest() -> Phase66AcceptanceManifest:
    """Return the immutable Phase 6.6 acceptance manifest."""

    return _PHASE66_MANIFEST


def _canonical_json(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


def phase66_manifest_digest() -> str:
    """Bind evidence to the exact canonical frozen manifest without embedding raw context."""

    payload = _PHASE66_MANIFEST.model_dump(mode="json")
    return sha256(_canonical_json(payload)).hexdigest()


def _reject_forbidden_evidence_keys(value: Any) -> None:
    if isinstance(value, Mapping):
        for key, nested in value.items():
            if isinstance(key, str) and key.casefold() in _FORBIDDEN_EVIDENCE_FIELD_NAMES:
                raise ValueError("Evidence contains a forbidden raw-authority field")
            _reject_forbidden_evidence_keys(nested)
    elif isinstance(value, (list, tuple)):
        for nested in value:
            _reject_forbidden_evidence_keys(nested)


class AssertionEvidence(_ClosedModel):
    assertion_id: AssertionId
    passed: StrictBool
    observation_digest: Digest | None = None
    observed_count: StrictInt | None = Field(default=None, ge=0, le=1_000_000)
    duration_ms: StrictInt | None = Field(default=None, ge=0, le=86_400_000)


class SanitizedArtifactEvidence(_ClosedModel):
    kind: ArtifactKind
    artifact_format: ArtifactFormat
    artifact_digest: Digest
    byte_count: StrictInt = Field(gt=0, le=10_000_000_000)
    redaction_verified: Literal[True]

    @field_validator("redaction_verified", mode="before")
    @classmethod
    def redaction_is_an_exact_boolean(cls, value: object) -> object:
        if type(value) is not bool:
            raise ValueError("Artifact redaction attestation must be a boolean")
        return value

    @model_validator(mode="after")
    def format_matches_artifact_kind(self) -> SanitizedArtifactEvidence:
        allowed = {
            ArtifactKind.TEST_REPORT: {ArtifactFormat.JSON, ArtifactFormat.JUNIT_XML},
            ArtifactKind.BROWSER_TRACE: {ArtifactFormat.ZIP},
            ArtifactKind.SCREENSHOT: {ArtifactFormat.PNG},
            ArtifactKind.DEPLOYMENT_SNAPSHOT: {ArtifactFormat.JSON},
            ArtifactKind.LOG_AUDIT: {ArtifactFormat.JSON},
            ArtifactKind.PROVIDER_CALL_LEDGER: {ArtifactFormat.JSON},
            ArtifactKind.CANARY_SUMMARY: {ArtifactFormat.JSON},
            ArtifactKind.MODERATED_SESSION_RECORD: {ArtifactFormat.JSON},
        }
        if self.artifact_format not in allowed[self.kind]:
            raise ValueError("Acceptance artifact kind and format do not match")
        return self


class PrivacyAttestation(_ClosedModel):
    sanitizer_contract: Literal["phase6.6-sanitized-evidence-v1"]
    forbidden_field_match_count: Literal[0]
    sensitive_value_match_count: Literal[0]
    free_text_value_count: Literal[0]

    @field_validator(
        "forbidden_field_match_count",
        "sensitive_value_match_count",
        "free_text_value_count",
        mode="before",
    )
    @classmethod
    def zero_counts_are_exact_integers(cls, value: object) -> object:
        if type(value) is not int:
            raise ValueError("Privacy counters must be exact integers")
        return value


class ProviderGateAttestation(_ClosedModel):
    run_gate_digest: Digest
    provider_write_gate_digest: Digest
    approved_scope: Literal["unpublished_draft_create_update_only"]
    root_credentials_rejected: Literal[True]
    publication_capability_absent: Literal[True]
    approved_max_product_posts: StrictInt = Field(ge=0, le=1)
    approved_max_product_puts: StrictInt = Field(ge=0, le=2)

    @field_validator(
        "root_credentials_rejected",
        "publication_capability_absent",
        mode="before",
    )
    @classmethod
    def provider_attestations_are_exact_booleans(cls, value: object) -> object:
        if type(value) is not bool:
            raise ValueError("Provider gate attestations must be booleans")
        return value

    @model_validator(mode="after")
    def gates_are_independently_authorized(self) -> ProviderGateAttestation:
        if self.run_gate_digest == self.provider_write_gate_digest:
            raise ValueError("Provider run and write gates must be independently attested")
        return self


class ProviderCallSummary(_ClosedModel):
    artwork_upload_count: StrictInt = Field(ge=0, le=1)
    product_post_count: StrictInt = Field(ge=0, le=1)
    product_put_count: StrictInt = Field(ge=0, le=2)
    product_get_count: StrictInt = Field(ge=0, le=100)
    forbidden_attempt_count: Literal[0]
    publish_attempt_count: Literal[0]
    order_attempt_count: Literal[0]
    fulfillment_attempt_count: Literal[0]
    final_state: ProviderFinalState

    @field_validator(
        "forbidden_attempt_count",
        "publish_attempt_count",
        "order_attempt_count",
        "fulfillment_attempt_count",
        mode="before",
    )
    @classmethod
    def forbidden_counts_are_exact_integers(cls, value: object) -> object:
        if type(value) is not int:
            raise ValueError("Provider forbidden counters must be exact integers")
        return value


class ModeratedSessionEvidence(_ClosedModel):
    participant_digest: Digest
    consent_record_digest: Digest
    task_script_digest: Digest
    session_record_digest: Digest
    first_time_seller: Literal[True]
    external_documentation_used: Literal[False]
    operator_intervention_count: StrictInt = Field(ge=0, le=100)
    completed_supported_flow: StrictBool
    duration_seconds: StrictInt = Field(ge=1, le=86_400)

    @field_validator("first_time_seller", "external_documentation_used", mode="before")
    @classmethod
    def moderated_literals_are_exact_booleans(cls, value: object) -> object:
        if type(value) is not bool:
            raise ValueError("Moderated-session attestations must be booleans")
        return value


class _BaseEvidenceRecord(_ClosedModel):
    schema_version: Literal["6.6.0"]
    manifest_digest: Digest
    run_digest: Digest
    source_commit_digest: Digest
    gate_id: GateId
    evidence_class: AcceptanceEvidenceClass
    outcome: AcceptanceOutcome
    recorded_at: AwareDatetime = Field(json_schema_extra={"pattern": _UTC_RFC3339.pattern})
    job_digest: Digest | None = None
    work_digest: Digest | None = None
    correlation_digest: Digest | None = None
    assertions: tuple[AssertionEvidence, ...] = Field(min_length=1, max_length=24)
    artifacts: tuple[SanitizedArtifactEvidence, ...] = Field(default=(), max_length=24)
    privacy: PrivacyAttestation

    @field_validator("recorded_at", mode="before")
    @classmethod
    def timestamp_is_canonical_utc_rfc3339(cls, value: object) -> object:
        if type(value) is not str or _UTC_RFC3339.fullmatch(value) is None:
            raise ValueError("Evidence timestamp must be canonical UTC RFC3339 text")
        return value

    @model_validator(mode="before")
    @classmethod
    def reject_raw_authority(cls, value: Any) -> Any:
        _reject_forbidden_evidence_keys(value)
        return value

    @model_validator(mode="after")
    def validate_common_authority(self) -> _BaseEvidenceRecord:
        if self.recorded_at.utcoffset() != UTC.utcoffset(self.recorded_at):
            raise ValueError("Evidence timestamps must use UTC")
        if self.manifest_digest != phase66_manifest_digest():
            raise ValueError("Evidence does not bind the frozen Phase 6.6 manifest")
        gate = _GATE_INDEX.get(self.gate_id)
        if gate is None or gate.evidence_class is not self.evidence_class:
            raise ValueError("Evidence gate and evidence class do not match the manifest")
        assertion_ids = tuple(assertion.assertion_id for assertion in self.assertions)
        if assertion_ids != gate.required_assertions:
            raise ValueError("Evidence must record every frozen assertion in manifest order")
        if self.outcome is AcceptanceOutcome.PASSED and not all(
            assertion.passed for assertion in self.assertions
        ):
            raise ValueError("Passed evidence cannot contain a failed assertion")
        if self.outcome is AcceptanceOutcome.FAILED and all(
            assertion.passed for assertion in self.assertions
        ):
            raise ValueError("Failed evidence must identify at least one failed assertion")
        artifact_digests = tuple(artifact.artifact_digest for artifact in self.artifacts)
        if len(set(artifact_digests)) != len(artifact_digests):
            raise ValueError("Acceptance artifacts must have unique digests")
        if self.outcome is AcceptanceOutcome.PASSED:
            artifact_kinds = {artifact.kind for artifact in self.artifacts}
            if not set(gate.required_artifact_kinds).issubset(artifact_kinds):
                raise ValueError("Passed evidence is missing a required sanitized artifact")
        return self


OfflineGateId = Literal[
    "offline.replay_matrix",
    "offline.concurrency_matrix",
    "offline.cross_owner_matrix",
    "offline.browser_matrix",
]
DeployedGateId = Literal[
    "deployed.edge_auth_owner_smoke",
    "deployed.upload_integrity_smoke",
    "deployed.outbox_recovery_smoke",
]
ProviderGateId = Literal[
    "provider.primary_same_job_canary",
    "provider.concurrency_canary",
    "provider.cancellation_canary",
]
ModeratedGateId = Literal[
    "moderated.first_time_seller_exit",
    "moderated.five_session_target",
]


class OfflineEvidenceRecord(_BaseEvidenceRecord):
    gate_id: OfflineGateId
    evidence_class: Literal[AcceptanceEvidenceClass.OFFLINE]
    deployment_digest: None = None
    actor_digests: tuple[()] = ()
    provider_gate_attestation: None = None
    provider_call_summary: None = None
    moderated_session: None = None


class DeployedNonDestructiveEvidenceRecord(_BaseEvidenceRecord):
    gate_id: DeployedGateId
    evidence_class: Literal[AcceptanceEvidenceClass.DEPLOYED_NON_DESTRUCTIVE]
    deployment_digest: Digest
    actor_digests: tuple[Digest, ...] = Field(min_length=1, max_length=2)
    provider_gate_attestation: None = None
    provider_call_summary: None = None
    moderated_session: None = None

    @model_validator(mode="after")
    def validate_actor_count(self) -> DeployedNonDestructiveEvidenceRecord:
        if len(set(self.actor_digests)) != len(self.actor_digests):
            raise ValueError("Deployed actor digests must be unique")
        expected = 2 if self.gate_id == "deployed.edge_auth_owner_smoke" else 1
        if len(self.actor_digests) != expected:
            raise ValueError("The deployed gate has the wrong number of actor digests")
        return self


class ProviderDestructiveEvidenceRecord(_BaseEvidenceRecord):
    gate_id: ProviderGateId
    evidence_class: Literal[AcceptanceEvidenceClass.PROVIDER_DESTRUCTIVE]
    deployment_digest: Digest
    actor_digests: tuple[Digest] = Field(min_length=1, max_length=1)
    job_digest: Digest
    provider_gate_attestation: ProviderGateAttestation
    provider_call_summary: ProviderCallSummary
    moderated_session: None = None

    @model_validator(mode="after")
    def validate_provider_gate(self) -> ProviderDestructiveEvidenceRecord:
        summary = self.provider_call_summary
        attestation = self.provider_gate_attestation
        if summary.product_post_count > attestation.approved_max_product_posts:
            raise ValueError("Provider POST count exceeds the double-gated authority")
        if summary.product_put_count > attestation.approved_max_product_puts:
            raise ValueError("Provider PUT count exceeds the double-gated authority")
        if self.gate_id == "provider.primary_same_job_canary":
            if self.work_digest is None or self.correlation_digest is None:
                raise ValueError(
                    "The primary canary must bind work and Strands correlation digests"
                )
            if (
                summary.artwork_upload_count != 1
                or summary.product_post_count != 1
                or summary.product_put_count != 2
                or summary.product_get_count < 1
                or summary.final_state is not ProviderFinalState.UNPUBLISHED_UNLOCKED
            ):
                raise ValueError(
                    "The primary canary must prove one artwork upload, one POST, "
                    "two PUTs, final GET readback, and safe state"
                )
        if self.gate_id == "provider.concurrency_canary" and summary.product_put_count > 1:
            raise ValueError("The concurrency canary may authorize at most one product PUT")
        return self


class ModeratedUserEvidenceRecord(_BaseEvidenceRecord):
    gate_id: ModeratedGateId
    evidence_class: Literal[AcceptanceEvidenceClass.MODERATED_USER]
    deployment_digest: Digest
    actor_digests: tuple[Digest] = Field(min_length=1, max_length=1)
    provider_gate_attestation: None = None
    provider_call_summary: None = None
    moderated_session: ModeratedSessionEvidence

    @model_validator(mode="after")
    def validate_moderated_gate(self) -> ModeratedUserEvidenceRecord:
        if self.gate_id == "moderated.first_time_seller_exit":
            if self.job_digest is None:
                raise ValueError("The first-time seller exit must bind the durable job digest")
            if self.outcome is AcceptanceOutcome.PASSED and (
                not self.moderated_session.completed_supported_flow
                or self.moderated_session.operator_intervention_count != 0
            ):
                raise ValueError("The first-time seller exit must complete without intervention")
        return self


type Phase66EvidenceRecord = Annotated[
    OfflineEvidenceRecord
    | DeployedNonDestructiveEvidenceRecord
    | ProviderDestructiveEvidenceRecord
    | ModeratedUserEvidenceRecord,
    Field(discriminator="evidence_class"),
]
_EVIDENCE_ADAPTER = TypeAdapter(Phase66EvidenceRecord)


def validate_phase66_evidence(value: object) -> Phase66EvidenceRecord:
    """Strictly validate one sanitized evidence record against the frozen manifest."""

    try:
        payload = json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        )
    except (TypeError, ValueError):
        raise ValueError("Evidence must be a strict JSON record") from None
    return _EVIDENCE_ADAPTER.validate_json(payload)


def evidence_record_json_schema() -> dict[str, Any]:
    """Return the closed structural schema for sanitized Phase 6.6 evidence.

    Cross-field, manifest-selected, and cross-record acceptance semantics remain exclusively
    authoritative in :func:`validate_phase66_evidence`; standard JSON Schema cannot express every
    equality, ordering, and frozen-manifest join used by that boundary.
    """

    schema = _EVIDENCE_ADAPTER.json_schema(ref_template="#/$defs/{model}")
    schema["$id"] = PHASE66_SCHEMA_ID
    schema["title"] = "Mr. Lister Phase 6.6 sanitized acceptance evidence"
    schema["$comment"] = (
        "Structural validation only. Every record MUST also pass the application-owned "
        "validate_phase66_evidence semantic boundary."
    )
    schema["x-runtime-semantic-validator"] = "mr_lister.acceptance.phase6.validate_phase66_evidence"
    return schema


__all__ = [
    "PHASE66_CONTRACT_VERSION",
    "AcceptanceEvidenceClass",
    "AcceptanceOutcome",
    "Phase66AcceptanceManifest",
    "Phase66EvidenceRecord",
    "evidence_record_json_schema",
    "phase66_acceptance_manifest",
    "phase66_manifest_digest",
    "validate_phase66_evidence",
]
