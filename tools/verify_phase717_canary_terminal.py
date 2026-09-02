#!/usr/bin/env python3
"""Verify one exact Phase 7.17 canary reached a complete published terminal graph.

The verifier consumes the create-once ``publish_once`` binding and private invocation, strongly
reads the full execution graph, current Phase 6 source authority, and seller projection from the
fixed development table, and emits fingerprints and counts only.  It has no provider, secret,
Lambda, S3, DynamoDB write, or publication invocation surface.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import stat
from collections.abc import Callable, Mapping, Sequence
from hashlib import sha256
from pathlib import Path
from typing import Any, Final, Protocol

from mr_lister.control.dynamodb import DynamoDBSellerControlStore
from mr_lister.publication.application import DynamoPublicationProjectionStore
from mr_lister.publication.canary_runtime import (
    PublicationCanaryBinding,
    PublicationCanaryInvocation,
    PublicationCanaryMode,
)
from mr_lister.publication.contract import PublicationPermitState, PublicationState
from mr_lister.publication.execution_dynamodb import DynamoDBPublicationExecutionStore
from mr_lister.publication.execution_fingerprints import safe_identity_digest
from mr_lister.publication.execution_models import (
    PublicationCallKind,
    PublicationExecutionAuthority,
    PublicationExecutionWorkStatus,
    PublicationReadOutcome,
    PublicationTerminalReason,
)
from mr_lister.publication.projection import SellerPublicationProjectionService
from mr_lister.publication.projection_models import (
    SellerPublicationProjection,
    SellerPublicationStage,
)
from mr_lister.publication.store import (
    PublicationRequestAuthority,
    validate_publication_request_authority,
)

REPOSITORY_ROOT: Final = Path(__file__).resolve().parents[1]
PRIVATE_ROOT: Final = REPOSITORY_ROOT / ".mr_lister_private" / "phase717-publish-once"

PROFILE: Final = "mr-lister-dev"
ACCOUNT_ID: Final = "384627057108"
REGION: Final = "us-west-2"
CALLER_ARN: Final = f"arn:aws:iam::{ACCOUNT_ID}:user/{PROFILE}"
STATE_TABLE: Final = "mr-lister-phase6-dev"
STATE_TABLE_ARN: Final = f"arn:aws:dynamodb:{REGION}:{ACCOUNT_ID}:table/{STATE_TABLE}"

BINDING_FILENAME: Final = "canary-binding.json"
INVOCATION_FILENAME: Final = "invocation.local.json"
MAX_PRIVATE_BYTES: Final = 128 * 1024

_DIGEST = re.compile(r"^[a-f0-9]{64}$")
_CANONICAL_ETSY_URL = re.compile(r"https://www\.etsy\.com/listing/[1-9][0-9]{0,12}")


class Phase717CanaryTerminalVerificationError(RuntimeError):
    """Value-free refusal for incomplete, foreign, or non-successful terminal authority."""


class TerminalReadBackend(Protocol):
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

    def load_seller_projection(
        self,
        owner_id: str,
        job_id: str,
    ) -> SellerPublicationProjection: ...


class _StrongReadDynamoDBClient:
    """Expose only the strongly consistent operations used by the read stores."""

    __slots__ = ("_client",)

    def __init__(self, client: object) -> None:
        self._client = client

    def get_item(self, **values: object) -> object:
        return self._client.get_item(**values)  # type: ignore[attr-defined, no-any-return]

    def query(self, **values: object) -> object:
        return self._client.query(**values)  # type: ignore[attr-defined, no-any-return]


class AwsTerminalReadBackend:
    """Fixed-account read composition with no DynamoDB mutation method."""

    __slots__ = ("_dynamodb", "_execution", "_projections", "_sts", "_validated")

    def __init__(self) -> None:
        import boto3

        session = boto3.Session(profile_name=PROFILE, region_name=REGION)
        self._sts = session.client("sts", region_name=REGION)
        self._dynamodb = session.client("dynamodb", region_name=REGION)
        reads = _StrongReadDynamoDBClient(self._dynamodb)
        self._execution = DynamoDBPublicationExecutionStore(
            client=reads,
            table_name=STATE_TABLE,
        )
        jobs = DynamoDBSellerControlStore(client=reads, table_name=STATE_TABLE)
        projection_store = DynamoPublicationProjectionStore(
            jobs=jobs,
            execution=self._execution,
        )
        self._projections = SellerPublicationProjectionService(projection_store)
        self._validated = False

    def _require_fixed_authority(self) -> None:
        if self._validated:
            return
        identity = _mapping(self._sts.get_caller_identity())
        table = _mapping(self._dynamodb.describe_table(TableName=STATE_TABLE)).get("Table")
        table = _mapping(table)
        if (
            identity.get("Account") != ACCOUNT_ID
            or identity.get("Arn") != CALLER_ARN
            or table.get("TableName") != STATE_TABLE
            or table.get("TableArn") != STATE_TABLE_ARN
            or table.get("TableStatus") != "ACTIVE"
        ):
            raise ValueError
        self._validated = True

    def load_execution_authority(
        self,
        owner_id: str,
        aggregate_id: str,
    ) -> PublicationExecutionAuthority:
        self._require_fixed_authority()
        return self._execution.load_execution_authority(owner_id, aggregate_id)

    def load_source_authority(
        self,
        owner_id: str,
        aggregate_id: str,
    ) -> PublicationRequestAuthority:
        self._require_fixed_authority()
        return self._execution.load_source_authority(owner_id, aggregate_id)

    def load_seller_projection(
        self,
        owner_id: str,
        job_id: str,
    ) -> SellerPublicationProjection:
        self._require_fixed_authority()
        return self._projections.get(owner_id=owner_id, job_id=job_id)


def verify_terminal(
    *,
    publish_once_root: Path,
    publish_once_binding_sha256: str,
    private_invocation_sha256: str,
    backend_factory: Callable[[], TerminalReadBackend],
) -> dict[str, object]:
    """Strongly verify one exact successful canary and return sanitized evidence."""

    try:
        if (
            _DIGEST.fullmatch(publish_once_binding_sha256) is None
            or _DIGEST.fullmatch(private_invocation_sha256) is None
        ):
            raise ValueError
        source_root = _existing_private_directory(publish_once_root)
        binding_raw = _read_private_file(source_root, BINDING_FILENAME)
        invocation_raw = _read_private_file(source_root, INVOCATION_FILENAME)
        if (
            _digest(binding_raw) != publish_once_binding_sha256
            or _digest(invocation_raw) != private_invocation_sha256
        ):
            raise ValueError
        binding = _binding(binding_raw)
        invocation = _invocation(invocation_raw)
        _require_invocation_binding(binding, invocation)

        backend = backend_factory()
        execution = _exact_execution(
            backend.load_execution_authority(invocation.owner_id, invocation.aggregate_id)
        )
        _require_execution(binding, invocation, execution)
        source = backend.load_source_authority(invocation.owner_id, invocation.aggregate_id)
        _require_source_rebind(invocation, execution, source)
        projection = _exact_projection(
            backend.load_seller_projection(invocation.owner_id, execution.snapshot.job_id)
        )
        _require_projection(execution, source, projection)

        proof = execution.preflight_proof
        mutation = execution.mutation_claim
        post = execution.post_observation
        observation = execution.last_product_observation
        result = execution.result
        notification = execution.notification
        report = execution.report
        tombstone = execution.tombstone
        link = execution.terminal_job_link
        assert all(
            value is not None
            for value in (
                proof,
                mutation,
                post,
                observation,
                result,
                notification,
                report,
                tombstone,
                link,
            )
        )
        output = {
            "aggregate_fingerprint": execution.aggregate.fingerprint,
            "notification_fingerprint": notification.fingerprint,  # type: ignore[union-attr]
            "positive_observation_fingerprint": observation.fingerprint,  # type: ignore[union-attr]
            "preflight_proof_fingerprint": proof.fingerprint,  # type: ignore[union-attr]
            "product_get_call_count": execution.attempt.product_get_call_count,
            "product_observation_count": len(execution.product_observations),
            "provider_audit_count": len(execution.provider_audits),
            "publish_post_call_count": execution.attempt.publish_post_call_count,
            "report_fingerprint": report.fingerprint,  # type: ignore[union-attr]
            "result_fingerprint": result.fingerprint,  # type: ignore[union-attr]
            "seller_projection_etag": projection.etag,
            "shop_get_call_count": execution.attempt.shop_get_call_count,
            "snapshot_fingerprint": execution.snapshot.fingerprint,
            "status": "verified_published",
            "terminal_job_link_fingerprint": link.fingerprint,  # type: ignore[union-attr]
            "tombstone_fingerprint": tombstone.fingerprint,  # type: ignore[union-attr]
        }
        _require_sanitized_output(output, execution, source)
        return output
    except Phase717CanaryTerminalVerificationError:
        raise
    except Exception:
        raise Phase717CanaryTerminalVerificationError(
            "Phase 7.17 canary terminal verification refused safely"
        ) from None


def _require_execution(
    binding: PublicationCanaryBinding,
    invocation: PublicationCanaryInvocation,
    execution: PublicationExecutionAuthority,
) -> None:
    snapshot = execution.snapshot
    proof = execution.preflight_proof
    mutation = execution.mutation_claim
    observation = execution.last_product_observation
    result = execution.result
    notification = execution.notification
    report = execution.report
    tombstone = execution.tombstone
    link = execution.terminal_job_link
    publish_claims = tuple(
        claim
        for claim in execution.call_claims
        if claim.call_kind is PublicationCallKind.PUBLISH_POST
    )
    publish_audits = tuple(
        audit
        for audit in execution.provider_audits
        if publish_claims and audit.call_claim_id == publish_claims[0].authorization_id
    )
    positive = (
        observation is not None
        and observation.outcome is PublicationReadOutcome.POSITIVE_PROOF
        and observation.exact_shop
        and observation.exact_product
        and observation.unlocked
        and observation.visible
        and observation.canonical_content_match
        and observation.single_etsy_external_reference
        and observation.no_conflicting_external_reference
        and observation.observed_at < snapshot.verification_deadline
    )
    if (
        execution.aggregate.state is not PublicationState.PUBLISHED
        or execution.work.status is not PublicationExecutionWorkStatus.SUCCEEDED
        or execution.permit.status is not PublicationPermitState.CONSUMED
        or execution.attempt.publish_post_call_count != 1
        or len(publish_claims) != 1
        or len(publish_audits) != 1
        or publish_claims[0].method != "POST"
        or not publish_claims[0].mutation_authorized
        or proof is None
        or binding.required_preflight_proof_fingerprint != proof.fingerprint
        or mutation is None
        or mutation.preflight_proof_id != proof.proof_id
        or mutation.preflight_proof_fingerprint != proof.fingerprint
        or execution.post_observation is None
        or not positive
        or result is None
        or observation is None
        or result.observation_id != observation.observation_id
        or result.observation_fingerprint != observation.fingerprint
        or result.verified_at != observation.observed_at
        or _CANONICAL_ETSY_URL.fullmatch(result.safe_listing_url) is None
        or notification is None
        or report is None
        or report.terminal_reason is not PublicationTerminalReason.POSITIVE_PUBLICATION_PROOF
        or tombstone is None
        or link is None
        or snapshot.owner_id != invocation.owner_id
        or execution.aggregate.aggregate_id != invocation.aggregate_id
        or safe_identity_digest("job_id", snapshot.job_id) != binding.job_id_digest
        or snapshot.fingerprint != binding.snapshot_fingerprint
        or safe_identity_digest("publication_permit_id", execution.permit.permit_id)
        != binding.permit_id_digest
        or safe_identity_digest("publication_work_request_id", execution.work.work_request_id)
        != binding.work_request_id_digest
        or execution.work.input_fingerprint != binding.work_input_fingerprint
        or snapshot.release_manifest_fingerprint != binding.release_manifest_fingerprint
        or snapshot.verification_deadline != binding.verification_deadline
    ):
        raise ValueError


def _require_source_rebind(
    invocation: PublicationCanaryInvocation,
    execution: PublicationExecutionAuthority,
    source: PublicationRequestAuthority,
) -> None:
    validate_publication_request_authority(source)
    snapshot = execution.snapshot
    job = source.current_job
    review = source.review
    decision = source.approval_decision
    artifact = source.source
    sync = source.product_sync
    pricing = source.pricing_snapshot
    evidence = source.pricing_evidence
    link = execution.terminal_job_link
    report = execution.report
    result = execution.result
    if (
        link is None
        or report is None
        or result is None
        or job.owner_id != invocation.owner_id
        or job.job_id != snapshot.job_id
        or job.publication_aggregate_id != invocation.aggregate_id
        or job.record_version != execution.phase6_record_version
        or job.record_version != link.result_record_version
        or job.event_sequence != execution.phase6_event_sequence
        or job.event_sequence != link.result_event_sequence
        or job.publication_terminal_state != PublicationState.PUBLISHED.value
        or job.publication_terminal_at != link.terminal_at
        or job.publication_source_release_eligible_at != link.source_release_eligible_at
        or job.publication_operational_expires_at != link.operational_expires_at
        or job.publication_report_id != report.report_id
        or job.publication_result_id != result.result_id
        or job.publication_terminal_summary_fingerprint != link.terminal_summary_fingerprint
        or job.approval_decision_id != snapshot.approval_decision_id
        or decision.decision_id != snapshot.approval_decision_id
        or job.approval_fingerprint != snapshot.approval_fingerprint
        or decision.approval_fingerprint != snapshot.approval_fingerprint
        or review.review_version != snapshot.review_version
        or review.fingerprint != snapshot.review_fingerprint
        or sync.sync_id != snapshot.product_sync_id
        or sync.fingerprint != snapshot.product_sync_fingerprint
        or sync.printify_shop_id != snapshot.printify_shop_id
        or sync.product_id != snapshot.printify_product_id
        or sync.image_id != snapshot.printify_image_id
        or sync.payload_fingerprint != snapshot.product_payload_fingerprint
        or pricing.snapshot_id != snapshot.pricing_snapshot_id
        or pricing.fingerprint != snapshot.pricing_snapshot_fingerprint
        or evidence.fingerprint != snapshot.pricing_evidence_fingerprint
        or artifact.product_profile_id != snapshot.profile_id
        or artifact.product_profile_version != snapshot.profile_version
        or artifact.product_profile_fingerprint != snapshot.profile_fingerprint
    ):
        raise ValueError


def _require_projection(
    execution: PublicationExecutionAuthority,
    source: PublicationRequestAuthority,
    projection: SellerPublicationProjection,
) -> None:
    result = execution.result
    report = execution.report
    if (
        result is None
        or report is None
        or projection.job_id != source.current_job.job_id
        or projection.state is not PublicationState.PUBLISHED
        or projection.stage is not SellerPublicationStage.COMPLETE
        or projection.aggregate_record_version != execution.aggregate.record_version
        or projection.attempt_status is not execution.attempt.status
        or projection.verification_deadline != execution.snapshot.verification_deadline
        or projection.safe_listing_url != result.safe_listing_url
        or _CANONICAL_ETSY_URL.fullmatch(projection.safe_listing_url or "") is None
        or projection.verified_at != result.verified_at
        or projection.report_id != report.report_id
        or projection.terminal_at != execution.aggregate.terminal_at
        or not projection.notification_available
        or projection.updated_at != source.current_job.updated_at
        or projection.publication_enabled is not False
        or projection.request_enabled is not False
    ):
        raise ValueError


def _require_invocation_binding(
    binding: PublicationCanaryBinding,
    invocation: PublicationCanaryInvocation,
) -> None:
    if (
        binding.mode is not PublicationCanaryMode.PUBLISH_ONCE
        or binding.required_preflight_proof_fingerprint is None
        or safe_identity_digest("owner_id", invocation.owner_id) != binding.owner_id_digest
        or safe_identity_digest("publication_aggregate_id", invocation.aggregate_id)
        != binding.aggregate_id_digest
    ):
        raise ValueError


def _exact_execution(value: object) -> PublicationExecutionAuthority:
    if not isinstance(value, PublicationExecutionAuthority):
        raise TypeError
    exact = PublicationExecutionAuthority.model_validate(value.model_dump(mode="python"))
    if exact != value:
        raise ValueError
    return exact


def _exact_projection(value: object) -> SellerPublicationProjection:
    if not isinstance(value, SellerPublicationProjection):
        raise TypeError
    exact = SellerPublicationProjection.model_validate(value.model_dump(mode="python"))
    if exact != value:
        raise ValueError
    return exact


def _require_sanitized_output(
    output: Mapping[str, object],
    execution: PublicationExecutionAuthority,
    source: PublicationRequestAuthority,
) -> None:
    allowed_non_fingerprints = {
        "product_get_call_count",
        "product_observation_count",
        "provider_audit_count",
        "publish_post_call_count",
        "shop_get_call_count",
        "status",
    }
    if set(output) - allowed_non_fingerprints != {
        "aggregate_fingerprint",
        "notification_fingerprint",
        "positive_observation_fingerprint",
        "preflight_proof_fingerprint",
        "report_fingerprint",
        "result_fingerprint",
        "seller_projection_etag",
        "snapshot_fingerprint",
        "terminal_job_link_fingerprint",
        "tombstone_fingerprint",
    }:
        raise ValueError
    raw = _canonical_json(output)
    private_values = {
        execution.snapshot.owner_id,
        execution.snapshot.job_id,
        execution.aggregate.aggregate_id,
        execution.snapshot.printify_product_id,
        execution.snapshot.printify_image_id,
        str(execution.snapshot.printify_shop_id),
        str(execution.result.numeric_listing_id) if execution.result is not None else "",
        source.approval_decision.decision_id,
    }
    if any(value and value.encode() in raw for value in private_values):
        raise ValueError


def _binding(raw: bytes) -> PublicationCanaryBinding:
    _strict_json(raw)
    binding = PublicationCanaryBinding.model_validate_json(raw, strict=True)
    if raw != _canonical_json(binding.model_dump(mode="json"), pretty=True):
        raise ValueError
    return binding


def _invocation(raw: bytes) -> PublicationCanaryInvocation:
    value = _strict_json(raw)
    if not isinstance(value, Mapping) or set(value) != {"aggregate_id", "owner_id"}:
        raise ValueError
    invocation = PublicationCanaryInvocation.model_validate_json(raw, strict=True)
    if raw != _canonical_json(
        {"aggregate_id": invocation.aggregate_id, "owner_id": invocation.owner_id},
        pretty=True,
    ):
        raise ValueError
    return invocation


def _canonical_json(value: object, *, pretty: bool = False) -> bytes:
    return (
        json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=True,
            indent=2 if pretty else None,
            separators=None if pretty else (",", ":"),
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")


def _strict_json(raw: bytes) -> object:
    def reject_constant(_value: str) -> None:
        raise ValueError

    def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError
            result[key] = value
        return result

    return json.loads(raw, parse_constant=reject_constant, object_pairs_hook=unique_object)


def _mapping(value: object) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError
    return value


def _digest(value: bytes) -> str:
    return sha256(value).hexdigest()


def _existing_private_directory(path: Path) -> Path:
    candidate = Path(os.path.abspath(path))
    root = Path(os.path.abspath(PRIVATE_ROOT))
    try:
        relative = candidate.relative_to(root)
    except ValueError:
        raise ValueError from None
    if (
        len(relative.parts) != 1
        or root.parent.is_symlink()
        or root.is_symlink()
        or candidate.is_symlink()
    ):
        raise ValueError
    for directory in (root, candidate):
        metadata = directory.stat()
        if not stat.S_ISDIR(metadata.st_mode) or stat.S_IMODE(metadata.st_mode) != 0o700:
            raise ValueError
    return candidate


def _read_private_file(directory: Path, name: str) -> bytes:
    if name not in {BINDING_FILENAME, INVOCATION_FILENAME}:
        raise ValueError
    directory_descriptor: int | None = None
    descriptor: int | None = None
    try:
        directory_descriptor = os.open(
            directory,
            os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY | os.O_NOFOLLOW,
        )
        descriptor = os.open(
            name,
            os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW,
            dir_fd=directory_descriptor,
        )
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or stat.S_IMODE(before.st_mode) != 0o600
            or not 1 <= before.st_size <= MAX_PRIVATE_BYTES
        ):
            raise OSError
        chunks: list[bytes] = []
        remaining = before.st_size
        while remaining:
            chunk = os.read(descriptor, min(remaining, 64 * 1024))
            if not chunk:
                raise OSError
            chunks.append(chunk)
            remaining -= len(chunk)
        if os.read(descriptor, 1):
            raise OSError
        after = os.fstat(descriptor)
        if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        ):
            raise OSError
        return b"".join(chunks)
    except OSError:
        raise ValueError from None
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if directory_descriptor is not None:
            os.close(directory_descriptor)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--publish-once-root", type=Path, required=True)
    parser.add_argument("--publish-once-binding-sha256", required=True)
    parser.add_argument("--private-invocation-sha256", required=True)
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    backend_factory: Callable[[], TerminalReadBackend] = AwsTerminalReadBackend,
) -> int:
    arguments = _parser().parse_args(argv)
    result = verify_terminal(
        publish_once_root=arguments.publish_once_root,
        publish_once_binding_sha256=arguments.publish_once_binding_sha256,
        private_invocation_sha256=arguments.private_invocation_sha256,
        backend_factory=backend_factory,
    )
    print(_canonical_json(result).decode("utf-8"), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
