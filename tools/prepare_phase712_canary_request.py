"""Prepare one exact Phase 7 read-only canary request without provider mutation.

``inspect`` strongly reads an approved Phase 6 job and writes a sanitized, SHA-bound plan plus
one private exact command. ``execute`` accepts only that exact plan, performs the existing atomic
15-action publication-request transaction, strongly re-reads the pristine graph, and writes a
sanitized read-only canary binding plus a private direct-invoke envelope.

The tool has no provider client, Secrets Manager client, Lambda client, S3 client, or publication
POST capability. Live execution remains separately gated and never invokes the canary.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import stat
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path
from typing import Any, Final, Protocol

from mr_lister.cloud.phase7_configuration import PinnedPublicationProfileAuthority
from mr_lister.control.fingerprints import review_etag
from mr_lister.publication.canary_runtime import (
    PublicationCanaryMode,
    build_publication_canary_binding,
)
from mr_lister.publication.commands import (
    PublicationCommandReceipt,
    RequestPublicationCommand,
)
from mr_lister.publication.contract import (
    PublicationPermitState,
    PublicationState,
    phase7_publication_contract,
    phase7_publication_contract_digest,
)
from mr_lister.publication.dynamodb import DynamoDBPublicationStore
from mr_lister.publication.execution_dynamodb import DynamoDBPublicationExecutionStore
from mr_lister.publication.execution_models import (
    PublicationExecutionAuthority,
    PublicationExecutionWorkStatus,
)
from mr_lister.publication.fingerprints import idempotency_key_digest
from mr_lister.publication.guard_verification import (
    DurablePublicationPreCallGuard,
    PublicationGuardSourceAuthority,
)
from mr_lister.publication.profile_eligibility import (
    PinnedPublicationProfileEligibilityAuthority,
    build_publication_profile_eligibility,
)
from mr_lister.publication.service import PublicationRequestService
from mr_lister.publication.store import (
    PublicationRequestAuthority,
    PublicationRequestTransaction,
)
from mr_lister.release.phase7_canary import (
    CANARY_PROFILE_FINGERPRINT,
    CANARY_PROFILE_ID,
    CANARY_PROFILE_PATH,
    CANARY_PROFILE_VERSION,
)
from mr_lister.review_profile import ExactReviewProductProfile, FilesystemReviewProductAuthority

REPOSITORY_ROOT: Final = Path(__file__).resolve().parents[1]
PRIVATE_ROOT: Final = REPOSITORY_ROOT / ".mr_lister_private" / "phase712-canary-operator"

REGION: Final = "us-west-2"
ACCOUNT_ID: Final = "384627057108"
PROFILE: Final = "mr-lister-dev"
CALLER_ARN: Final = f"arn:aws:iam::{ACCOUNT_ID}:user/{PROFILE}"
ENVIRONMENT_NAME: Final = "dev"
STATE_TABLE: Final = "mr-lister-phase6-dev"
STACK_NAME: Final = "mr-lister-phase6-dev"

TARGET_FORMAT: Final = "phase7.12-canary-target-v1"
PLAN_FORMAT: Final = "phase7.12-canary-request-plan-v1"
PLAN_FILENAME: Final = "prepared.json"
COMMAND_FILENAME: Final = "command.local.json"
BINDING_FILENAME: Final = "canary-binding.json"
INVOCATION_FILENAME: Final = "invocation.local.json"

LIVE_ENVIRONMENT_SWITCH: Final = "MR_LISTER_RUN_PHASE712_CANARY_REQUEST_CREATE"
LIVE_ENVIRONMENT_VALUE: Final = "I_ACCEPT_THE_EXACT_PHASE712_REQUEST_PLAN"
EXECUTION_CONFIRMATION: Final = "create_exact_phase7_read_only_canary_request"

MAX_PRIVATE_BYTES: Final = 128 * 1024
PLAN_TTL: Final = timedelta(minutes=10)
MINIMUM_PRICING_AT_INSPECTION: Final = timedelta(minutes=50)
MINIMUM_DEPLOYMENT_WINDOW_AFTER_READBACK: Final = timedelta(minutes=15)
_FINGERPRINT = re.compile(r"^[a-f0-9]{64}$")
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
_OWNER_ID = re.compile(r"^[a-f0-9]{64}$")
_TARGET_LABEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{2,63}$")


class Phase712CanaryOperatorError(RuntimeError):
    """Value-free refusal for unsafe, stale, drifting, or incomplete operator input."""


@dataclass(frozen=True, slots=True)
class DeploymentAuthority:
    account_id: str
    caller_arn: str
    region: str
    table_name: str
    table_arn: str
    stack_id: str
    stack_status: str
    release_manifest_fingerprint: str


class InspectBackend(Protocol):
    def deployment_authority(self) -> DeploymentAuthority: ...

    def load_request_authority(
        self,
        owner_id: str,
        job_id: str,
    ) -> PublicationRequestAuthority: ...


class OperatorBackend(InspectBackend, Protocol):
    def resolve_request_receipt(
        self,
        owner_id: str,
        job_id: str,
        key_digest: str,
    ) -> PublicationCommandReceipt | None: ...

    def commit_request(
        self,
        transaction: PublicationRequestTransaction,
    ) -> PublicationCommandReceipt: ...

    def load_execution_authority(
        self,
        owner_id: str,
        aggregate_id: str,
    ) -> PublicationExecutionAuthority: ...

    def load_source_authority(
        self,
        owner_id: str,
        aggregate_id: str,
    ) -> PublicationRequestAuthority: ...


class _ReadOnlyDynamoDBClient:
    """Expose only the one operation used by request inspection."""

    def __init__(self, client: object) -> None:
        self._client = client

    def get_item(self, **values: object) -> object:
        return self._client.get_item(**values)  # type: ignore[attr-defined, no-any-return]


class AwsInspectBackend:
    """Exact regional read adapter with no transaction method on its request client."""

    def __init__(self) -> None:
        import boto3

        session = boto3.Session(profile_name=PROFILE, region_name=REGION)
        self._sts = session.client("sts", region_name=REGION)
        self._cloudformation = session.client("cloudformation", region_name=REGION)
        self._dynamodb = session.client("dynamodb", region_name=REGION)
        self._requests = DynamoDBPublicationStore(
            client=_ReadOnlyDynamoDBClient(self._dynamodb),
            table_name=STATE_TABLE,
        )

    def deployment_authority(self) -> DeploymentAuthority:
        identity = self._sts.get_caller_identity()
        table = self._dynamodb.describe_table(TableName=STATE_TABLE).get("Table", {})
        stacks = self._cloudformation.describe_stacks(StackName=STACK_NAME).get("Stacks", [])
        if len(stacks) != 1 or not isinstance(stacks[0], Mapping):
            raise Phase712CanaryOperatorError("Phase 7.12 deployment authority is invalid")
        stack = stacks[0]
        parameters = {
            item.get("ParameterKey"): item.get("ParameterValue")
            for item in stack.get("Parameters", [])
            if isinstance(item, Mapping)
        }
        return DeploymentAuthority(
            account_id=str(identity.get("Account", "")),
            caller_arn=str(identity.get("Arn", "")),
            region=REGION,
            table_name=str(table.get("TableName", "")),
            table_arn=str(table.get("TableArn", "")),
            stack_id=str(stack.get("StackId", "")),
            stack_status=str(stack.get("StackStatus", "")),
            release_manifest_fingerprint=str(parameters.get("ReleaseFingerprint", "")),
        )

    def load_request_authority(
        self,
        owner_id: str,
        job_id: str,
    ) -> PublicationRequestAuthority:
        return self._requests.load_request_authority(owner_id, job_id)


class AwsOperatorBackend(AwsInspectBackend):
    """Add only the exact request transaction and strong execution readback surfaces."""

    def __init__(self) -> None:
        super().__init__()
        self._requests = DynamoDBPublicationStore(
            client=self._dynamodb,
            table_name=STATE_TABLE,
        )
        self._execution = DynamoDBPublicationExecutionStore(
            client=self._dynamodb,
            table_name=STATE_TABLE,
        )

    def resolve_request_receipt(
        self,
        owner_id: str,
        job_id: str,
        key_digest: str,
    ) -> PublicationCommandReceipt | None:
        return self._requests.resolve_request_receipt(owner_id, job_id, key_digest)

    def commit_request(
        self,
        transaction: PublicationRequestTransaction,
    ) -> PublicationCommandReceipt:
        return self._requests.commit_request(transaction)

    def load_execution_authority(
        self,
        owner_id: str,
        aggregate_id: str,
    ) -> PublicationExecutionAuthority:
        return self._execution.load_execution_authority(owner_id, aggregate_id)

    def load_source_authority(
        self,
        owner_id: str,
        aggregate_id: str,
    ) -> PublicationRequestAuthority:
        return self._execution.load_source_authority(owner_id, aggregate_id)


class _PreviewStore:
    """Run the exact request validation and transaction builder while discarding the write."""

    def __init__(self, authority: PublicationRequestAuthority) -> None:
        self._authority = authority
        self.transaction: PublicationRequestTransaction | None = None

    def resolve_request_receipt(
        self,
        owner_id: str,
        job_id: str,
        key_digest: str,
    ) -> None:
        del owner_id, job_id, key_digest
        return None

    def load_request_authority(
        self,
        owner_id: str,
        job_id: str,
    ) -> PublicationRequestAuthority:
        if (
            owner_id != self._authority.current_job.owner_id
            or job_id != self._authority.current_job.job_id
        ):
            raise Phase712CanaryOperatorError("Phase 7.12 request authority is invalid")
        return self._authority

    def commit_request(
        self,
        transaction: PublicationRequestTransaction,
    ) -> PublicationCommandReceipt:
        self.transaction = transaction
        return transaction.commit.receipt


class _GuardStore:
    def __init__(self, backend: OperatorBackend) -> None:
        self._backend = backend

    def load_execution_authority(
        self,
        owner_id: str,
        aggregate_id: str,
    ) -> PublicationExecutionAuthority:
        return self._backend.load_execution_authority(owner_id, aggregate_id)

    def load_source_authority(
        self,
        owner_id: str,
        aggregate_id: str,
    ) -> PublicationGuardSourceAuthority:
        source = self._backend.load_source_authority(owner_id, aggregate_id)
        return PublicationGuardSourceAuthority(
            current_job=source.current_job,
            review=source.review,
            approval_decision=source.approval_decision,
            source=source.source,
            product_sync=source.product_sync,
            pricing_snapshot=source.pricing_snapshot,
            pricing_evidence=source.pricing_evidence,
        )


def inspect_target(
    *,
    target_path: Path,
    output_root: Path,
    backend: InspectBackend,
    clock: Callable[[], datetime] | None = None,
) -> Mapping[str, object]:
    """Strong-read and freeze one request plan without calling ``commit_request``."""

    now = _utc_now(clock)
    target_raw = _read_private_file(target_path, "target")
    target = _target(target_raw)
    deployment = _validated_deployment(backend.deployment_authority())
    owner_id = target["owner_id"]
    job_id = target["job_id"]
    authority = backend.load_request_authority(owner_id, job_id)
    if authority.current_job.publication_aggregate_id is not None:
        raise Phase712CanaryOperatorError("Phase 7.12 target already has publication authority")
    if authority.pricing_snapshot.fresh_until < now + MINIMUM_PRICING_AT_INSPECTION:
        raise Phase712CanaryOperatorError("Phase 7.12 pricing window is too short")

    profile, eligibility = _profile_authorities(deployment.release_manifest_fingerprint)
    command = _request_command(authority, deployment=deployment)
    preview = _PreviewStore(authority)
    response = PublicationRequestService(
        store=preview,
        profiles=PinnedPublicationProfileAuthority(profile),
        profile_eligibility=eligibility,
        release_manifest_fingerprint=deployment.release_manifest_fingerprint,
        clock=lambda: now,
    ).request_publication(command)
    if preview.transaction is None:
        raise Phase712CanaryOperatorError("Phase 7.12 request preview is incomplete")
    contract = phase7_publication_contract()
    if response.verification_deadline != now + timedelta(
        seconds=contract.verification_deadline_seconds
    ):
        raise Phase712CanaryOperatorError("Phase 7.12 request preview is invalid")

    command_payload = _canonical_json(command.model_dump(mode="json"), pretty=True)
    target_sha = _digest(target_raw)
    command_sha = _digest(command_payload)
    execute_not_after = min(
        now + PLAN_TTL,
        authority.pricing_snapshot.fresh_until - timedelta(minutes=40),
    )
    if execute_not_after <= now:
        raise Phase712CanaryOperatorError("Phase 7.12 pricing window is too short")
    plan = _plan(
        target_sha256=target_sha,
        command_sha256=command_sha,
        command=command,
        authority=authority,
        deployment=deployment,
        profile=profile,
        generated_at=now,
        execute_not_after=execute_not_after,
        target_label=target["label"],
    )
    plan_payload = _canonical_json(plan, pretty=True)
    _assert_sanitized(plan_payload, _raw_authority_values(authority))

    directory = _fresh_output_directory(output_root)
    _write_once(directory, COMMAND_FILENAME, command_payload)
    _write_once(directory, PLAN_FILENAME, plan_payload)
    summary = {
        "execute_not_after": execute_not_after.isoformat(),
        "mode": "inspect",
        "mutations": 0,
        "plan_sha256": _digest(plan_payload),
        "provider_calls": 0,
        "status": "ready",
        "target_label": target["label"],
    }
    return summary


def execute_prepared(
    *,
    prepared_root: Path,
    approval_binding_sha256: str,
    confirmation: str,
    output_root: Path,
    backend_factory: Callable[[], OperatorBackend],
    clock: Callable[[], datetime] | None = None,
    environment: Mapping[str, str] | None = None,
) -> Mapping[str, object]:
    """Commit the exact approved request, then mint only a read-only canary binding."""

    if confirmation != EXECUTION_CONFIRMATION:
        raise Phase712CanaryOperatorError("Phase 7.12 execution confirmation is invalid")
    if _FINGERPRINT.fullmatch(approval_binding_sha256) is None:
        raise Phase712CanaryOperatorError("Phase 7.12 approval binding is invalid")
    selected_environment = os.environ if environment is None else environment
    if selected_environment.get(LIVE_ENVIRONMENT_SWITCH) != LIVE_ENVIRONMENT_VALUE:
        raise Phase712CanaryOperatorError("Phase 7.12 live execution gate is closed")

    root = _existing_output_directory(prepared_root)
    plan_raw = _read_private_file(root / PLAN_FILENAME, "plan")
    if _digest(plan_raw) != approval_binding_sha256:
        raise Phase712CanaryOperatorError("Phase 7.12 approval binding is invalid")
    plan = _strict_mapping(_strict_json(plan_raw, "plan"), "plan")
    _validate_plan_shape(plan)
    if plan.get("source_closure_sha256") != _source_closure_sha256():
        raise Phase712CanaryOperatorError("Phase 7.12 source closure changed after inspection")
    if plan.get("contract_sha256") != phase7_publication_contract_digest():
        raise Phase712CanaryOperatorError("Phase 7.12 contract changed after inspection")

    command_raw = _read_private_file(root / COMMAND_FILENAME, "command")
    if _digest(command_raw) != plan.get("command_sha256"):
        raise Phase712CanaryOperatorError("Phase 7.12 private command changed after inspection")
    try:
        command = RequestPublicationCommand.model_validate_json(command_raw, strict=True)
    except Exception:
        raise Phase712CanaryOperatorError("Phase 7.12 private command is invalid") from None
    initial_now = _utc_now(clock)
    execute_not_after = _utc_text(plan.get("execute_not_after"), "execute_not_after")
    if plan.get("idempotency_key_digest") != idempotency_key_digest(command.idempotency_key):
        raise Phase712CanaryOperatorError("Phase 7.12 request idempotency changed")
    if plan.get("owner_id_digest") != _digest_text(command.owner_id) or plan.get(
        "job_id_digest"
    ) != _digest_text(command.job_id):
        raise Phase712CanaryOperatorError("Phase 7.12 private command identity changed")

    # Prove the destination is fresh and writable before constructing any live backend.
    destination = _fresh_output_directory(output_root)
    backend = backend_factory()
    deployment = _validated_deployment(backend.deployment_authority())
    if _deployment_binding(deployment) != plan.get("deployment"):
        raise Phase712CanaryOperatorError("Phase 7.12 deployment authority drifted")

    profile, eligibility = _profile_authorities(deployment.release_manifest_fingerprint)
    if _profile_binding(profile) != plan.get("profile"):
        raise Phase712CanaryOperatorError("Phase 7.12 checked profile drifted")

    existing = backend.resolve_request_receipt(
        command.owner_id,
        command.job_id,
        idempotency_key_digest(command.idempotency_key),
    )
    request_now = _utc_now(clock)
    if request_now < initial_now:
        raise Phase712CanaryOperatorError("Phase 7.12 clock moved backwards")
    if existing is None and request_now > execute_not_after:
        raise Phase712CanaryOperatorError("Phase 7.12 request plan expired")
    if existing is None:
        current = backend.load_request_authority(command.owner_id, command.job_id)
        if current.current_job.publication_aggregate_id is not None:
            raise Phase712CanaryOperatorError("Phase 7.12 target has foreign publication authority")
        if _authority_binding(current) != plan.get("authority"):
            raise Phase712CanaryOperatorError("Phase 7.12 approved authority drifted")
        expected_command = _request_command(current, deployment=deployment)
        if expected_command != command:
            raise Phase712CanaryOperatorError("Phase 7.12 private command is not server-derived")

    response = PublicationRequestService(
        store=backend,
        profiles=PinnedPublicationProfileAuthority(profile),
        profile_eligibility=eligibility,
        release_manifest_fingerprint=deployment.release_manifest_fingerprint,
        clock=lambda: request_now,
    ).request_publication(command)

    guard = DurablePublicationPreCallGuard(
        store=_GuardStore(backend),
        profiles=PinnedPublicationProfileAuthority(profile),
        eligibility=eligibility,
        release_manifest_fingerprint=deployment.release_manifest_fingerprint,
    )
    execution = guard.require_current(
        owner_id=command.owner_id,
        aggregate_id=response.publication_aggregate_id,
    )
    readback_now = _utc_now(clock)
    if readback_now < request_now:
        raise Phase712CanaryOperatorError("Phase 7.12 clock moved backwards")
    _require_pristine(execution)
    binding = build_publication_canary_binding(
        execution,
        mode=PublicationCanaryMode.READ_ONLY_PREFLIGHT,
    )
    remaining_seconds = int(
        (execution.snapshot.verification_deadline - readback_now).total_seconds()
    )
    binding_payload = _canonical_json(binding.model_dump(mode="json"), pretty=True)
    invocation = {
        "aggregate_id": response.publication_aggregate_id,
        "owner_id": command.owner_id,
    }
    invocation_payload = _canonical_json(invocation, pretty=True)
    _assert_sanitized(binding_payload, _raw_authority_values(execution))
    _write_once(destination, BINDING_FILENAME, binding_payload)
    _write_once(destination, INVOCATION_FILENAME, invocation_payload)
    return {
        "binding_sha256": _digest(binding_payload),
        "deployment_window_sufficient": (
            remaining_seconds >= int(MINIMUM_DEPLOYMENT_WINDOW_AFTER_READBACK.total_seconds())
        ),
        "invocation_sha256": _digest(invocation_payload),
        "mode": "execute",
        "plan_sha256": approval_binding_sha256,
        "provider_calls": 0,
        "publication_request_transactions": 0 if existing is not None else 1,
        "status": "bound_read_only_preflight",
        "verification_window_remaining_seconds": remaining_seconds,
    }


def _request_command(
    authority: PublicationRequestAuthority,
    *,
    deployment: DeploymentAuthority,
) -> RequestPublicationCommand:
    job = authority.current_job
    review = authority.review
    decision = authority.approval_decision
    key_material = {
        "account_id": deployment.account_id,
        "job_id": job.job_id,
        "owner_id": job.owner_id,
        "release_manifest_fingerprint": deployment.release_manifest_fingerprint,
        "table": deployment.table_name,
    }
    idempotency_key = f"phase712-read-only-canary-{_digest(_canonical_json(key_material))}"
    return RequestPublicationCommand(
        owner_id=job.owner_id,
        job_id=job.job_id,
        expected_record_version=job.record_version,
        expected_review_version=review.review_version,
        expected_review_fingerprint=review.fingerprint,
        expected_review_etag=review_etag(
            job_id=job.job_id,
            review_version=review.review_version,
            review_fingerprint=review.fingerprint,
            product_id=authority.product_sync.product_id,
            product_sync_fingerprint=authority.product_sync.fingerprint,
            pricing_snapshot_id=authority.pricing_snapshot.snapshot_id,
            pricing_snapshot_fingerprint=authority.pricing_snapshot.fingerprint,
        ),
        expected_approval_decision_id=decision.decision_id,
        expected_approval_fingerprint=decision.approval_fingerprint,
        confirmation="publish_exact_approved_listing",
        idempotency_key=idempotency_key,
    )


def _plan(
    *,
    target_sha256: str,
    command_sha256: str,
    command: RequestPublicationCommand,
    authority: PublicationRequestAuthority,
    deployment: DeploymentAuthority,
    profile: ExactReviewProductProfile,
    generated_at: datetime,
    execute_not_after: datetime,
    target_label: str,
) -> dict[str, object]:
    return {
        "authority": _authority_binding(authority),
        "budget": {
            "dynamodb_transaction_actions": 15,
            "dynamodb_transactions": 1,
            "lambda_invocations": 0,
            "provider_calls": 0,
            "provider_posts": 0,
            "s3_writes": 0,
            "secret_reads": 0,
        },
        "command_sha256": command_sha256,
        "contract_sha256": phase7_publication_contract_digest(),
        "deployment": _deployment_binding(deployment),
        "execute_not_after": execute_not_after.isoformat(),
        "format": PLAN_FORMAT,
        "generated_at": generated_at.isoformat(),
        "idempotency_key_digest": idempotency_key_digest(command.idempotency_key),
        "job_id_digest": _digest_text(command.job_id),
        "mode": PublicationCanaryMode.READ_ONLY_PREFLIGHT.value,
        "owner_id_digest": _digest_text(command.owner_id),
        "profile": _profile_binding(profile),
        "source_closure_sha256": _source_closure_sha256(),
        "target_label": target_label,
        "target_sha256": target_sha256,
    }


def _authority_binding(authority: PublicationRequestAuthority) -> dict[str, object]:
    job = authority.current_job
    return {
        "approval_decision_id_digest": _digest_text(authority.approval_decision.decision_id),
        "approval_fingerprint": authority.approval_decision.approval_fingerprint,
        "event_sequence": job.event_sequence,
        "pricing_evidence_fingerprint": authority.pricing_evidence.fingerprint,
        "pricing_fresh_until": authority.pricing_snapshot.fresh_until.isoformat(),
        "pricing_snapshot_fingerprint": authority.pricing_snapshot.fingerprint,
        "product_sync_fingerprint": authority.product_sync.fingerprint,
        "record_version": job.record_version,
        "review_fingerprint": authority.review.fingerprint,
        "review_version": authority.review.review_version,
        "source_fingerprint": authority.source.fingerprint,
    }


def _deployment_binding(authority: DeploymentAuthority) -> dict[str, object]:
    return {
        "account_id": authority.account_id,
        "caller_arn_digest": _digest_text(authority.caller_arn),
        "environment": ENVIRONMENT_NAME,
        "region": authority.region,
        "release_manifest_fingerprint": authority.release_manifest_fingerprint,
        "stack_id_digest": _digest_text(authority.stack_id),
        "table": authority.table_name,
        "table_arn_digest": _digest_text(authority.table_arn),
    }


def _profile_binding(profile: ExactReviewProductProfile) -> dict[str, object]:
    return {
        "fingerprint": profile.fingerprint,
        "profile_id": profile.profile.profile_id,
        "profile_version": profile.profile.profile_version,
        "publish_enabled": profile.profile.publish_enabled,
    }


def _validated_deployment(authority: DeploymentAuthority) -> DeploymentAuthority:
    expected_table_arn = f"arn:aws:dynamodb:{REGION}:{ACCOUNT_ID}:table/{STATE_TABLE}"
    expected_stack_prefix = f"arn:aws:cloudformation:{REGION}:{ACCOUNT_ID}:stack/{STACK_NAME}/"
    if (
        authority.account_id != ACCOUNT_ID
        or authority.region != REGION
        or authority.table_name != STATE_TABLE
        or authority.table_arn != expected_table_arn
        or not authority.stack_id.startswith(expected_stack_prefix)
        or authority.stack_status not in {"CREATE_COMPLETE", "UPDATE_COMPLETE"}
        or _FINGERPRINT.fullmatch(authority.release_manifest_fingerprint) is None
        or authority.release_manifest_fingerprint == "0" * 64
        or authority.caller_arn != CALLER_ARN
    ):
        raise Phase712CanaryOperatorError("Phase 7.12 deployment authority is invalid")
    return authority


def _profile_authorities(
    release_manifest_fingerprint: str,
) -> tuple[ExactReviewProductProfile, PinnedPublicationProfileEligibilityAuthority]:
    exact = FilesystemReviewProductAuthority(
        profile_directory=(REPOSITORY_ROOT / CANARY_PROFILE_PATH).parent
    ).get_exact(
        profile_id=CANARY_PROFILE_ID,
        profile_version=CANARY_PROFILE_VERSION,
    )
    if (
        exact.fingerprint != CANARY_PROFILE_FINGERPRINT
        or exact.profile.publish_enabled is not False
    ):
        raise Phase712CanaryOperatorError("Phase 7.12 checked profile is invalid")
    eligibility = build_publication_profile_eligibility(
        profile_id=exact.profile.profile_id,
        profile_version=exact.profile.profile_version,
        profile_fingerprint=exact.fingerprint,
        release_manifest_fingerprint=release_manifest_fingerprint,
        phase6_profile_publish_enabled=exact.profile.publish_enabled,
    )
    return exact, PinnedPublicationProfileEligibilityAuthority(eligibility)


def _require_pristine(authority: PublicationExecutionAuthority) -> None:
    empty_optional = (
        authority.provider_authority,
        authority.preflight_proof,
        authority.mutation_claim,
        authority.post_observation,
        authority.last_product_observation,
        authority.result,
        authority.notification,
        authority.report,
        authority.tombstone,
        authority.terminal_job_link,
    )
    if (
        authority.aggregate.state is not PublicationState.PUBLICATION_REQUESTED
        or authority.permit.status is not PublicationPermitState.AVAILABLE
        or authority.work.status is not PublicationExecutionWorkStatus.PENDING
        or authority.attempt.shop_get_call_count != 0
        or authority.attempt.product_get_call_count != 0
        or authority.attempt.publish_post_call_count != 0
        or authority.call_claims
        or authority.provider_audits
        or authority.product_observations
        or any(value is not None for value in empty_optional)
    ):
        raise Phase712CanaryOperatorError("Phase 7.12 readback authority is not pristine")


def _target(payload: bytes) -> dict[str, str]:
    value = _strict_mapping(_strict_json(payload, "target"), "target")
    if set(value) != {"format", "job_id", "label", "owner_id"}:
        raise Phase712CanaryOperatorError("Phase 7.12 target is invalid")
    owner = value.get("owner_id")
    job = value.get("job_id")
    label = value.get("label")
    if (
        value.get("format") != TARGET_FORMAT
        or not isinstance(owner, str)
        or _OWNER_ID.fullmatch(owner) is None
        or not isinstance(job, str)
        or _SAFE_ID.fullmatch(job) is None
        or not isinstance(label, str)
        or _TARGET_LABEL.fullmatch(label) is None
    ):
        raise Phase712CanaryOperatorError("Phase 7.12 target is invalid")
    return {"owner_id": owner, "job_id": job, "label": label}


def _validate_plan_shape(plan: Mapping[str, Any]) -> None:
    expected = {
        "authority",
        "budget",
        "command_sha256",
        "contract_sha256",
        "deployment",
        "execute_not_after",
        "format",
        "generated_at",
        "idempotency_key_digest",
        "job_id_digest",
        "mode",
        "owner_id_digest",
        "profile",
        "source_closure_sha256",
        "target_label",
        "target_sha256",
    }
    fingerprints = (
        "command_sha256",
        "contract_sha256",
        "idempotency_key_digest",
        "job_id_digest",
        "owner_id_digest",
        "source_closure_sha256",
        "target_sha256",
    )
    if (
        set(plan) != expected
        or plan.get("format") != PLAN_FORMAT
        or plan.get("mode") != PublicationCanaryMode.READ_ONLY_PREFLIGHT.value
        or not isinstance(plan.get("target_label"), str)
        or _TARGET_LABEL.fullmatch(str(plan.get("target_label"))) is None
        or any(
            not isinstance(plan.get(name), str)
            or _FINGERPRINT.fullmatch(str(plan.get(name))) is None
            for name in fingerprints
        )
        or plan.get("budget")
        != {
            "dynamodb_transaction_actions": 15,
            "dynamodb_transactions": 1,
            "lambda_invocations": 0,
            "provider_calls": 0,
            "provider_posts": 0,
            "s3_writes": 0,
            "secret_reads": 0,
        }
    ):
        raise Phase712CanaryOperatorError("Phase 7.12 request plan is invalid")
    _utc_text(plan.get("generated_at"), "generated_at")
    _utc_text(plan.get("execute_not_after"), "execute_not_after")


def _raw_authority_values(authority: object) -> tuple[str, ...]:
    if isinstance(authority, PublicationRequestAuthority):
        return (
            authority.current_job.owner_id,
            authority.current_job.job_id,
            authority.approval_decision.decision_id,
            authority.product_sync.product_id,
            authority.product_sync.image_id,
            str(authority.product_sync.printify_shop_id),
        )
    if isinstance(authority, PublicationExecutionAuthority):
        return (
            authority.snapshot.owner_id,
            authority.snapshot.job_id,
            authority.aggregate.aggregate_id,
            authority.snapshot.approval_decision_id,
            authority.snapshot.printify_product_id,
            authority.snapshot.printify_image_id,
            str(authority.snapshot.printify_shop_id),
        )
    raise TypeError


def _assert_sanitized(payload: bytes, raw_values: Sequence[str]) -> None:
    if any(value and value.encode() in payload for value in raw_values):
        raise Phase712CanaryOperatorError("Phase 7.12 sanitized output contains private identity")


def _source_closure_sha256() -> str:
    """Bind the operator and every repository module that its live request may import."""

    sources = [
        Path(__file__).resolve(),
        *sorted((REPOSITORY_ROOT / "src" / "mr_lister").rglob("*.py")),
        REPOSITORY_ROOT / CANARY_PROFILE_PATH,
        REPOSITORY_ROOT / "contracts" / "publication" / "phase7.0.1.json",
    ]
    records: list[dict[str, object]] = []
    for source in sources:
        exact = source.resolve(strict=True)
        if source.is_symlink() or not exact.is_file():
            raise Phase712CanaryOperatorError("Phase 7.12 source closure is invalid")
        try:
            relative = exact.relative_to(REPOSITORY_ROOT).as_posix()
        except ValueError:
            raise Phase712CanaryOperatorError("Phase 7.12 source closure is invalid") from None
        raw = exact.read_bytes()
        records.append({"path": relative, "sha256": _digest(raw), "size": len(raw)})
    return _digest(
        _canonical_json(
            {
                "files": records,
                "format": "phase7.12-canary-request-source-closure-v1",
            }
        )
    )


def _canonical_json(value: object, *, pretty: bool = False) -> bytes:
    return (
        json.dumps(
            value,
            sort_keys=True,
            indent=2 if pretty else None,
            separators=None if pretty else (",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        )
        + "\n"
    ).encode()


def _strict_json(payload: bytes, label: str) -> object:
    def reject_constant(_value: str) -> None:
        raise ValueError

    def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError
            result[key] = value
        return result

    try:
        return json.loads(
            payload,
            parse_constant=reject_constant,
            object_pairs_hook=unique_object,
        )
    except Exception:
        raise Phase712CanaryOperatorError(f"Phase 7.12 {label} is not strict JSON") from None


def _strict_mapping(value: object, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or any(not isinstance(key, str) for key in value):
        raise Phase712CanaryOperatorError(f"Phase 7.12 {label} is invalid")
    return value


def _utc_text(value: object, label: str) -> datetime:
    try:
        if not isinstance(value, str):
            raise ValueError
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
            raise ValueError
        return parsed.astimezone(UTC)
    except ValueError:
        raise Phase712CanaryOperatorError(f"Phase 7.12 {label} is invalid") from None


def _utc_now(clock: Callable[[], datetime] | None) -> datetime:
    value = (clock or (lambda: datetime.now(UTC)))()
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise Phase712CanaryOperatorError("Phase 7.12 clock is invalid")
    return value.astimezone(UTC)


def _digest(value: bytes) -> str:
    return sha256(value).hexdigest()


def _digest_text(value: str) -> str:
    return _digest(value.encode())


def _exact_private_path(path: Path) -> Path:
    candidate = Path(os.path.abspath(path))
    root = Path(os.path.abspath(PRIVATE_ROOT))
    try:
        relative = candidate.relative_to(root)
    except ValueError:
        raise Phase712CanaryOperatorError(
            "Phase 7.12 files must stay in the repository-private operator directory"
        ) from None
    if not 1 <= len(relative.parts) <= 2:
        raise Phase712CanaryOperatorError(
            "Phase 7.12 files must stay in the repository-private operator directory"
        )
    return candidate


def _ensure_private_root() -> Path:
    parent = PRIVATE_ROOT.parent
    if parent.is_symlink():
        raise Phase712CanaryOperatorError("Phase 7.12 private root is invalid")
    parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if PRIVATE_ROOT.is_symlink():
        raise Phase712CanaryOperatorError("Phase 7.12 private root is invalid")
    PRIVATE_ROOT.mkdir(mode=0o700, exist_ok=True)
    PRIVATE_ROOT.chmod(0o700)
    metadata = PRIVATE_ROOT.stat()
    if not stat.S_ISDIR(metadata.st_mode) or metadata.st_mode & 0o077:
        raise Phase712CanaryOperatorError("Phase 7.12 private root is invalid")
    return PRIVATE_ROOT


def _read_private_file(path: Path, label: str) -> bytes:
    candidate = _exact_private_path(path)
    if candidate.parent.is_symlink() or candidate.is_symlink():
        raise Phase712CanaryOperatorError(f"Phase 7.12 {label} file is invalid")
    descriptor: int | None = None
    try:
        descriptor = os.open(candidate, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or before.st_mode & 0o077
            or not 1 <= before.st_size <= MAX_PRIVATE_BYTES
        ):
            raise OSError
        chunks: list[bytes] = []
        remaining = before.st_size
        while remaining:
            chunk = os.read(descriptor, remaining)
            if not chunk:
                raise OSError
            chunks.append(chunk)
            remaining -= len(chunk)
        after = os.fstat(descriptor)
    except OSError:
        raise Phase712CanaryOperatorError(
            f"Phase 7.12 {label} must be one stable mode-0600 private file"
        ) from None
    finally:
        if descriptor is not None:
            os.close(descriptor)
    if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
    ):
        raise Phase712CanaryOperatorError(f"Phase 7.12 {label} changed while read")
    return b"".join(chunks)


def _fresh_output_directory(path: Path) -> Path:
    root = _ensure_private_root()
    candidate = _exact_private_path(path)
    if (
        candidate.parent != root
        or candidate == root
        or candidate.exists()
        or candidate.is_symlink()
    ):
        raise Phase712CanaryOperatorError("Phase 7.12 output must be one fresh private directory")
    try:
        candidate.mkdir(mode=0o700)
    except OSError:
        raise Phase712CanaryOperatorError(
            "Phase 7.12 output must be one fresh private directory"
        ) from None
    return candidate


def _existing_output_directory(path: Path) -> Path:
    root = _ensure_private_root()
    candidate = _exact_private_path(path)
    if candidate.parent != root or candidate.is_symlink() or not candidate.is_dir():
        raise Phase712CanaryOperatorError("Phase 7.12 prepared directory is invalid")
    metadata = candidate.stat()
    if metadata.st_mode & 0o077:
        raise Phase712CanaryOperatorError("Phase 7.12 prepared directory is invalid")
    return candidate


def _write_once(directory: Path, name: str, payload: bytes) -> None:
    if directory.is_symlink() or name not in {
        PLAN_FILENAME,
        COMMAND_FILENAME,
        BINDING_FILENAME,
        INVOCATION_FILENAME,
    }:
        raise Phase712CanaryOperatorError("Phase 7.12 private output is invalid")
    destination = directory / name
    descriptor: int | None = None
    try:
        descriptor = os.open(
            destination,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
            0o600,
        )
        with os.fdopen(descriptor, "wb", closefd=True) as output:
            descriptor = None
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
    except OSError:
        raise Phase712CanaryOperatorError(
            "Phase 7.12 private output could not be written"
        ) from None
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subcommands = parser.add_subparsers(dest="operation", required=True)
    inspect = subcommands.add_parser("inspect", help="read and freeze an exact request plan")
    inspect.add_argument("--target", type=Path, required=True)
    inspect.add_argument("--output-root", type=Path, required=True)
    execute = subcommands.add_parser("execute", help="commit one exact approved request")
    execute.add_argument("--prepared-root", type=Path, required=True)
    execute.add_argument("--approval-binding-sha256", required=True)
    execute.add_argument("--confirmation", required=True)
    execute.add_argument("--output-root", type=Path, required=True)
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    inspect_backend_factory: Callable[[], InspectBackend] = AwsInspectBackend,
    execute_backend_factory: Callable[[], OperatorBackend] = AwsOperatorBackend,
    clock: Callable[[], datetime] | None = None,
) -> int:
    arguments = _parser().parse_args(argv)
    try:
        if arguments.operation == "inspect":
            result = inspect_target(
                target_path=arguments.target,
                output_root=arguments.output_root,
                backend=inspect_backend_factory(),
                clock=clock,
            )
        else:
            result = execute_prepared(
                prepared_root=arguments.prepared_root,
                approval_binding_sha256=arguments.approval_binding_sha256,
                confirmation=arguments.confirmation,
                output_root=arguments.output_root,
                backend_factory=execute_backend_factory,
                clock=clock,
            )
    except Phase712CanaryOperatorError:
        raise
    except Exception:
        raise Phase712CanaryOperatorError("Phase 7.12 canary operator preparation failed") from None
    print(_canonical_json(result).decode(), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
